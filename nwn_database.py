"""Campaign databases: read what is stored, and check it against what the scripts store and read.

NWN keeps "campaign" data (SetCampaignInt/String/..., StoreCampaignObject) in the game's (or server's) database
folder, one database per name: EE uses `<name>.sqlite3` (SQLite, table `db`; most text and objects are zstd-compressed
"CPDB" values), older versions used BioWare's `<NAME>.DBF/.CDX/.FPT` (FoxPro). EE also lets scripts run their own SQL
on a campaign database (SqlPrepareQueryCampaign) and NWNX adds a server database (NWNX_SQL).

Why this exists: a script that reads a campaign variable no script ever writes, or reads it with the wrong type, gets
0 / "" with no error in game. Comparing the scripts with each other (and with what a server has actually stored) finds
those silent bugs.

Safety: everything here is READ-ONLY.
  - SQLite files (.sqlite3/.sqlite/.db) are copied, with their -wal/-shm/-journal side files, to a temporary folder
    and only the copy is opened: SQLite may write side files even when it only reads, and a copy can't lock or change a
    live server's database. The temporary folder is deleted afterwards.
  - Old .DBF/.FPT files and .sql dumps are read straight from disk with a plain read-only open() (no database engine,
    nothing written).
  - Values of variables or columns whose names look secret (SECRET_RE: pass, token, auth...) are replaced by
    "(set - hidden)" before they leave this module.
  - Size limits: files over MAX_FILE are listed, not read; at most MAX_ROWS rows per file; values cut to MAX_VALUE
    characters; a compressed value may expand to 16 MB at most.

    read_path(path)                        -> snapshot {folder, files[], campaign{db: {vars{}}}, tables{db: {table: ...}}}
    script_uses(db_index)                  -> [{script, line, action, vtype, db, var, per_player, tables, ...}]
    int_constants(db_index)                -> {upper-case names of `const int` in the scripts}
    cross_check(snapshot, uses, unused)    -> {summary, databases[], findings[], tables[], sql[]}
db_index is the analysis index (index.sqlite, a sqlite3.Connection with a `scripts(name, source)` table).

Limits: script_uses reads NWScript with regular expressions, not a parser. Database and variable names built at run
time are kept as patterns ("prefix_*") or marked unknown, never guessed; helper functions that pass the names through
are followed two calls deep.
"""
import fnmatch
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import time
from collections import defaultdict

import nwn_zstd
# names whose values are hidden, any case: e.g. "AdminPassword", "api_key", "AuthToken" - the same list the server
# settings use (also catches harmless names such as quest "token" variables - hiding too much is the safe side)
from nwn_modsettings import SECRET_RE

MAX_ROWS = 50000               # rows read per database file
MAX_FILE = 512 << 20           # files bigger than this are listed, not read
MAX_VALUE = 300                # characters of a value kept
EXAMPLES = 25                  # values kept per variable
DB_EXTS = (".sqlite3", ".sqlite", ".db", ".dbf", ".sql")
# EE table `db`, column `vartype`: read as the character code of the same one-letter type the old .DBF VARTYPE column
# uses (73 = ord("I") int, 70 "F" float, 83 "S" string, 86 "V" vector, 76 "L" location, 79 "O" object)
VT = {73: "int", 70: "float", 83: "string", 86: "vector", 76: "location", 79: "object"}
LEGACY_VT = {"I": "int", "F": "float", "S": "string", "V": "vector", "L": "location", "O": "object"}


# ================================================================================================ reading files
def _short(v):
    """The value as text, cut to MAX_VALUE characters (with "...")."""
    v = str(v)
    return v if len(v) <= MAX_VALUE else v[:MAX_VALUE] + "..."


def _object_text(blob):
    """A stored object is a GFF (item/creature...). Describe it: kind, name, tag, blueprint.
    Never raises: anything unreadable becomes "(object, N bytes)"."""
    try:
        import nwnlib as n
        g = n.read_gff(bytes(blob))                      # read_gff refuses damaged / oversized tables itself
        kind = {"UTI ": "item", "UTC ": "creature", "BIC ": "character", "UTP ": "placeable", "UTM ": "store"}.get(
            g.file_type, g.file_type.strip().lower())
        name = g.get("LocalizedName") or g.get("FirstName")
        name = name.text() if hasattr(name, "text") else (name or "")
        if g.get("LastName") and hasattr(g.get("LastName"), "text"):
            name = (name + " " + g.get("LastName").text()).strip()
        bits = [f"{kind} '{name}'" if name else kind]
        if g.get("Tag"):
            bits.append(f"tag {g.get('Tag')}")
        if g.get("TemplateResRef"):
            bits.append(f"blueprint {g.get('TemplateResRef')}")
        items = g.get("ItemList")
        if isinstance(items, list) and items:
            bits.append(f"carrying {len(items)} item(s)")
        return "(object) " + ", ".join(bits)
    except Exception:
        return f"(object, {len(blob)} bytes)"


def _decode(vtype, payload, compressed=False):
    """Turn a stored value into text for display.

    vtype: "int", "float", "string", "vector", "location", "object" or "" (unknown). payload: what the column held
    (number, text, bytes or None). compressed: the row's `compressed` flag. Returns a number for int/float values,
    else text of at most MAX_VALUE characters. Never raises: an unreadable value becomes a description of itself."""
    if payload is None:
        return ""
    if isinstance(payload, (int, float)):
        return payload
    if isinstance(payload, str):
        raw = payload.encode("utf-8", "replace")
    else:
        raw = bytes(payload)
    # trust either the row's flag or the buffer's own "CPDB" label; 16 MB caps what one value may expand to
    if compressed or raw[:4] == b"CPDB":
        try:
            raw = nwn_zstd.decompress_cpdb(raw, 16 << 20)
        except Exception as ex:
            return f"(compressed value, could not be read: {ex})"
    # a stored object is a GFF file; recognise it by its 4-byte file type even when the row's type is unknown
    if vtype == "object" or raw[:4] in (b"UTI ", b"UTC ", b"BIC ", b"UTP ", b"UTM "):
        return _object_text(raw)
    if vtype in ("int", "float"):
        try:
            return (int if vtype == "int" else float)(raw.decode("ascii").strip())
        except ValueError:
            pass
    txt = raw.decode("cp1252", "replace")                # NWN's text encoding
    # more than 3 control characters (other than tab/newline/CR...) in the first 200: show it as hex, not text
    if sum(1 for ch in txt[:200] if ord(ch) < 9 or 13 < ord(ch) < 32) > 3:
        return f"(binary, {len(raw)} bytes) " + raw[:24].hex()
    return _short(txt)


def _copy(path, tmp):
    """Copy a database file (and SQLite's side files) so the original is never opened by SQLite.
    Returns the copy's path inside `tmp`."""
    dst = os.path.join(tmp, os.path.basename(path))
    shutil.copyfile(path, dst)
    # recent changes may still sit in the write-ahead log (-wal) of a running server: copy it so the copy is complete
    for side in ("-wal", "-shm", "-journal"):
        if os.path.isfile(path + side):
            shutil.copyfile(path + side, dst + side)
    return dst


def _sqlite_file(path, tmp):
    """Read an SQLite database (via a copy in `tmp`): the EE campaign table `db` row by row, and for every other table
    its columns, row count and first 20 rows. Returns dict(kind, rows, tables, meta, total_rows); only SELECT and
    PRAGMA statements are run, and only on the copy."""
    local = _copy(path, tmp)
    con = sqlite3.connect(local)            # a private copy: read-only in practice, WAL replayed into the copy
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out = dict(kind="sqlite", tables={}, rows=[], meta={})
        # table names come from the file itself (untrusted): each is a quoted SQL identifier, with any " inside
        # doubled as SQL requires, so a name like a"b stays one name; execute() runs one statement only
        quoted = {t: '"' + t.replace('"', '""') + '"' for t in tables}
        cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({quoted[t]})")] for t in tables}
        # an EE campaign database has a table `db` with these columns (one row per variable and player)
        is_campaign = "db" in cols and {"varname", "playerid", "vartype", "payload"} <= set(cols["db"])
        if is_campaign:
            out["kind"] = "campaign (EE)"
            has_c = "compressed" in cols["db"]           # without the column, the "CPDB" label alone decides (_decode)
            q = "SELECT varname, playerid, vartype, payload" + (", compressed" if has_c else ", 0") + " FROM db"
            for vn, pid, vt, pl, comp in con.execute(q + f" LIMIT {MAX_ROWS}"):
                t = VT.get(vt, str(vt))
                out["rows"].append(dict(var=vn or "", player=pid or "", type=t,
                                        value="(set - hidden)" if SECRET_RE.search(vn or "") else _decode(t, pl, comp)))
            out["total_rows"] = con.execute("SELECT count(*) FROM db").fetchone()[0]
            if "meta" in cols:
                out["meta"] = {str(k): str(v) for k, v in con.execute("SELECT key, value FROM meta")}
        for t in tables:
            if is_campaign and t in ("db", "meta", "migrations"):
                continue                                 # the campaign's own tables, not tables made by scripts
            n = con.execute(f"SELECT count(*) FROM {quoted[t]}").fetchone()[0]
            sample = []
            for r in con.execute(f"SELECT * FROM {quoted[t]} LIMIT 20"):
                sample.append(["(set - hidden)" if SECRET_RE.search(cols[t][i]) else
                               _short(x if not isinstance(x, bytes) else _decode("", x)) for i, x in enumerate(r)])
            out["tables"][t] = dict(columns=cols[t], rows=n, sample=sample)
        return out
    finally:
        con.close()


def _dbf_file(path, tmp):
    """BioWare's FoxPro campaign database (.DBF with .FPT memo file).

    Reads the file (and the .FPT next to it) with a read-only open; `tmp` is not used. Returns the same shape as
    _sqlite_file. Raises ValueError when the file is not a campaign .DBF.

    Layout used (dBASE/FoxPro): header bytes 4-7 record count, 8-9 header length, 10-11 record length (little-endian);
    from byte 32, one 32-byte descriptor per field (name in bytes 0-10, type letter at 11, length at 16) until a 0x0D
    byte; each record starts with a deletion flag byte ('*' = deleted), then the fields in order."""
    b = open(path, "rb").read()
    if len(b) < 32:
        raise ValueError("too short for a .DBF file")
    n_rec, hlen, rlen = struct.unpack_from("<IHH", b, 4)
    fields, off, pos = [], 32, 1                         # pos 1: field data starts after the deletion flag byte
    while off + 32 <= hlen and b[off] != 0x0D:
        name = b[off:off + 11].split(b"\0")[0].decode("ascii", "replace")
        ln = b[off + 16]
        fields.append((name, chr(b[off + 11]), pos, ln))    # (name, type letter, offset in record, length)
        pos += ln
        off += 32
    fmap = {f[0]: f for f in fields}
    if not {"VARNAME", "PLAYERID", "VARTYPE"} <= set(fmap):
        raise ValueError("not a campaign database (.DBF without VARNAME/PLAYERID/VARTYPE)")
    memo = None
    base = os.path.splitext(path)[0]
    for ext in (".FPT", ".fpt"):
        if os.path.isfile(base + ext):
            memo = open(base + ext, "rb").read()
            break
    # .FPT memo file: block size is a big-endian uint16 at byte 6 of its header (64 if the file is missing or short)
    bsize = struct.unpack_from(">H", memo, 6)[0] if memo and len(memo) >= 8 else 64

    def fld(rec, name):
        """The raw bytes of field `name` in record `rec` (b"" if the .DBF has no such field)."""
        f = fmap.get(name)
        return rec[f[2]:f[2] + f[3]] if f else b""

    def memo_at(idx_bytes):
        """The memo text a MEMO field points to: its block number is a 4-byte little-endian integer or, in a longer
        field, ASCII digits. A memo block holds a big-endian type and length (8 bytes), then the data (capped at
        16 MB). Returns b"" for an empty or out-of-range pointer."""
        try:
            idx = int(idx_bytes.decode("ascii").strip() or 0) if len(idx_bytes) != 4 else struct.unpack("<I", idx_bytes)[0]
        except ValueError:
            return b""
        if not memo or idx <= 0 or idx * bsize + 8 > len(memo):
            return b""
        ln = struct.unpack_from(">I", memo, idx * bsize + 4)[0]
        return memo[idx * bsize + 8: idx * bsize + 8 + min(ln, 16 << 20)]

    rows = []
    for i in range(min(n_rec, MAX_ROWS)):
        rec = b[hlen + i * rlen: hlen + (i + 1) * rlen]
        if len(rec) < rlen or rec[:1] == b"*":        # deleted record (or the file is cut off)
            continue
        vn = fld(rec, "VARNAME").decode("cp1252", "replace").strip()
        pid = fld(rec, "PLAYERID").decode("cp1252", "replace").strip()
        t = LEGACY_VT.get(fld(rec, "VARTYPE").decode("ascii", "replace").strip(), "?")
        # where this reader takes each type's value from: INT, DBL1 (float), DBL1-3 (vector), DBL1-4 + MEMO (location),
        # MEMO for strings and objects
        if t == "int":
            val = _decode("int", fld(rec, "INT"))
        elif t == "float":
            val = _decode("float", fld(rec, "DBL1"))
        elif t == "vector":
            val = ", ".join(fld(rec, k).decode("ascii", "replace").strip() for k in ("DBL1", "DBL2", "DBL3"))
        elif t == "location":
            val = ", ".join(fld(rec, k).decode("ascii", "replace").strip() for k in ("DBL1", "DBL2", "DBL3", "DBL4")) + \
                  " " + _decode("string", memo_at(fld(rec, "MEMO")))
        else:
            val = _decode(t, memo_at(fld(rec, "MEMO")))
        rows.append(dict(var=vn, player=pid, type=t, value="(set - hidden)" if SECRET_RE.search(vn) else val,
                         when=fld(rec, "TIMESTAMP").decode("ascii", "replace").strip()))
    return dict(kind="campaign (old BioWare .DBF)", rows=rows, tables={}, meta={}, total_rows=n_rec)


# table name after CREATE TABLE [IF NOT EXISTS], optionally in `back-ticks`, "quotes" or [brackets]:
#   CREATE TABLE IF NOT EXISTS `pc_data` (...   ->  pc_data
SQL_TABLE_RE = re.compile(r"(?i)\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)")
# table name after INSERT [OR REPLACE | IGNORE] INTO:   INSERT OR REPLACE INTO pc_data VALUES ...  ->  pc_data
SQL_INSERT_RE = re.compile(r"(?i)\binsert\s+(?:or\s+\w+\s+|ignore\s+)?into\s+[`\"\[]?(\w+)")


def _sql_dump(path, tmp):
    """A text dump (mysqldump / sqlite .dump): tables and roughly how many rows are inserted.

    Reads at most the first 64 MB with a read-only open (`tmp` is not used); no SQL is run. Row counts are an
    estimate: one per INSERT plus one per "),(" between multi-row VALUES groups. No values are kept."""
    txt = open(path, "rb").read(64 << 20).decode("utf-8", "replace")
    tables = {}
    for m in SQL_TABLE_RE.finditer(txt):
        body = txt[m.end(): txt.find(";", m.end())]
        # split the column list on commas that end a line or are not inside (...), e.g. "price DECIMAL(10,2)" stays
        # whole; key / constraint lines are not columns; the first word of each part is the column name
        cols = [c.strip().split()[0].strip("`\"[]") for c in re.split(r",\s*\n|,(?![^()]*\))", body.strip(" (\n"))
                if c.strip() and not re.match(r"(?i)\s*(primary|unique|key|constraint|index|foreign)\b", c)]
        tables[m.group(1)] = dict(columns=cols[:40], rows=0, sample=[])
    for m in SQL_INSERT_RE.finditer(txt):
        end = txt.find(";\n", m.end())
        stmt = txt[m.end(): end if end > 0 else len(txt)]
        t = tables.setdefault(m.group(1), dict(columns=[], rows=0, sample=[]))
        t["rows"] += stmt.count("),(") + stmt.count("),\n(") + 1
    return dict(kind="SQL dump", rows=[], tables=tables, meta={}, total_rows=0)


def read_path(path):
    """Read a database folder (or one file) and return a snapshot of what is stored.

    path: a folder (every .sqlite3/.sqlite/.db/.dbf/.sql in it, not sub-folders) or one such file; surrounding
    quotes and spaces are ignored. Returns dict(folder, loaded, files, campaign, tables):
        files     one entry per file: file, size, modified, db (name without extension), kind, and rows, tables,
                  import_done when it was read or a note when it was not
        campaign  {database name: {sources, vars: {variable: {types, rows, players, module_wide, examples, in_files}},
                   players (count)}}
        tables    {database name: {table: {columns, rows, sample}}} for tables other than the campaign's own
    A file that can't be read is listed with kind "unreadable" and a note; it does not stop the others.
    Raises ValueError when the path does not exist or is not a database file.

    Never writes or locks an original: SQLite files are opened only as copies in a temporary folder (deleted at the
    end); .DBF/.FPT and .sql files are only read."""
    path = os.path.abspath(str(path or "").strip().strip('"'))
    if not os.path.exists(path):
        raise ValueError(f"not found: {path}")
    if os.path.isdir(path):
        # EE files first, so an old .DBF copy of the same database doesn't count twice
        names = sorted((f for f in os.listdir(path) if f.lower().endswith(DB_EXTS)),
                       key=lambda f: (f.lower().endswith((".dbf", ".sql")), f.lower()))
        folder, files = path, [os.path.join(path, f) for f in names]
    else:
        if not path.lower().endswith(DB_EXTS):
            raise ValueError("pick a database folder, or a .sqlite3 / .sqlite / .db / .dbf / .sql file")
        folder, files = os.path.dirname(path), [path]
    snap = dict(folder=folder, loaded=time.strftime("%Y-%m-%d %H:%M"), files=[], campaign={}, tables={})
    tmp = tempfile.mkdtemp(prefix="nwn_db_")
    try:
        for f in files:
            st = os.stat(f)
            ent = dict(file=os.path.basename(f), size=st.st_size,
                       modified=time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
            stem = os.path.splitext(os.path.basename(f))[0].lower()
            ent["db"] = stem
            if st.st_size > MAX_FILE:                    # not copied, not opened
                ent.update(kind="too big to read", note=f"over {MAX_FILE >> 20} MB - listed only")
                snap["files"].append(ent)
                continue
            try:
                low = f.lower()
                r = _dbf_file(f, tmp) if low.endswith(".dbf") else _sql_dump(f, tmp) if low.endswith(".sql") \
                    else _sqlite_file(f, tmp)
            except Exception as ex:                      # one damaged file must not stop the rest
                ent.update(kind="unreadable", note=str(ex)[:300])
                snap["files"].append(ent)
                continue
            # meta key "import_done" = "yes": the toolkit takes this to mean EE has copied the old .DBF data in
            ent.update(kind=r["kind"], rows=r.get("total_rows", 0), tables=len(r["tables"]),
                       import_done=r["meta"].get("import_done") == "yes")
            snap["files"].append(ent)
            if r["rows"] or r["kind"].startswith("campaign"):
                c = snap["campaign"].setdefault(stem, dict(sources=[], vars={}, players=set()))
                c["sources"].append(dict(file=ent["file"], kind=r["kind"], rows=r.get("total_rows", 0),
                                         import_done=ent["import_done"]))
                ee = r["kind"] == "campaign (EE)"
                for row in r["rows"]:
                    v = c["vars"].setdefault(row["var"], dict(types=[], rows=0, players=0, module_wide=False,
                                                              examples=[], in_files=[]))
                    if row["type"] not in v["types"]:
                        v["types"].append(row["type"])
                    if ent["file"] not in v["in_files"]:
                        v["in_files"].append(ent["file"])
                    if not ee and any(s["kind"] == "campaign (EE)" for s in c["sources"][:-1]):
                        continue                         # the EE copy wins; old rows only mark where it was
                    v["rows"] += 1
                    if row["player"]:
                        v["players"] += 1
                        c["players"].add(row["player"])
                    else:
                        v["module_wide"] = True
                    if len(v["examples"]) < EXAMPLES:
                        v["examples"].append(dict(player=row["player"], value=row["value"], type=row["type"],
                                                  file=ent["file"]))
            if r["tables"]:
                snap["tables"][stem] = r["tables"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)           # the temporary copies, always removed
    for c in snap["campaign"].values():
        c["players"] = len(c["players"])                 # a set while collecting; a count in the snapshot (JSON)
    return snap


# ================================================================================================ scripts
# a "string literal" (with \" escapes, on one line), a // comment to end of line, or a /* block comment */.
# Strings are matched too so that "http://x" is not mistaken for the start of a comment.
_STR_OR_COMMENT = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', re.S)


def _strip_comments(src):
    """NWScript source with comments blanked out (strings kept). Newlines inside comments are kept so line numbers
    found later still match the original source."""
    return _STR_OR_COMMENT.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else
                               re.sub(r"[^\n]", " ", m.group(0)), src)


def _args(src, i):
    """src[i] is '(' - split the call's arguments (respecting brackets and strings). Returns (args, end)."""
    depth, cur, out, j, n = 0, [], [], i + 1, len(src)
    while j < n:
        ch = src[j]
        if ch == '"':
            k = j + 1
            while k < n and src[k] != '"':
                k += 2 if src[k] == "\\" else 1
            cur.append(src[j:k + 1])
            j = k + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                out.append("".join(cur).strip())
                return out, j
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            j += 1
            continue
        cur.append(ch)
        j += 1
    return out, n


# function: (action, value type, position of the player argument), positions as in nwscript.nss, e.g.
# SetCampaignInt(sCampaignName, sVarName, nInt, oPlayer) -> 3; GetCampaignInt(sCampaignName, sVarName, oPlayer) -> 2.
# The SQL functions have no variable name; their query text is searched for table names instead.
CAMPAIGN = {
    "SetCampaignInt": ("write", "int", 3), "SetCampaignFloat": ("write", "float", 3),
    "SetCampaignString": ("write", "string", 3), "SetCampaignVector": ("write", "vector", 3),
    "SetCampaignLocation": ("write", "location", 3), "StoreCampaignObject": ("write", "object", 3),
    "GetCampaignInt": ("read", "int", 2), "GetCampaignFloat": ("read", "float", 2),
    "GetCampaignString": ("read", "string", 2), "GetCampaignVector": ("read", "vector", 2),
    "GetCampaignLocation": ("read", "location", 2), "RetrieveCampaignObject": ("read", "object", 4),
    "DeleteCampaignVariable": ("delete", "", 2), "DestroyCampaignDatabase": ("destroy", "", None),
    "SqlPrepareQueryCampaign": ("sql", "", None), "SqlPrepareQueryObject": ("sql-object", "", None),
    "NWNX_SQL_PrepareQuery": ("sql-nwnx", "", None), "NWNX_SQL_ExecuteQuery": ("sql-nwnx", "", None),
    "SQLExecDirect": ("sql-nwnx", "", None),
}
CALL_RE = re.compile(r"\b(" + "|".join(CAMPAIGN) + r")\s*\(")       # a call of any function above: "SetCampaignInt ("
# the word after a keyword that names a table: "SELECT x FROM pc_data JOIN items" -> pc_data, items. Run over the
# literal pieces of a query only, so it may also catch ordinary words; SQL_WORDS removes the common false hits.
TABLE_RE = re.compile(r"(?i)\b(?:create\s+table\s+(?:if\s+not\s+exists\s+)?|insert\s+(?:or\s+\w+\s+)?into\s+|"
                      r"replace\s+into\s+|update\s+|delete\s+from\s+|from\s+|join\s+)[`\"\[]?([A-Za-z_]\w*)")
SQL_WORDS = {"select", "where", "set", "values", "the", "a", "an", "table", "if", "not", "exists", "sqlite_master",
             "dual", "as", "on", "by", "order", "group", "limit", "and", "or"}


def _functions(src):
    """[(start, end, [param names], function name)] for each function body - to tell a passed-in name from an
    unknown one. start/end are character positions of the body in `src` (comments already blanked)."""
    out = []
    # a definition: return type, name, (parameters), then "{" - e.g. "int GetDBInt(string sDB, string sVar) {".
    # A prototype ends with ";" instead, so it does not match.
    for m in re.finditer(r"\b(?:void|int|float|string|object|location|vector|effect|itemproperty|json|sqlquery|"
                         r"talent|event|struct\s+\w+)\s+(\w+)\s*\(([^)]*)\)\s*\{", src):
        # parameter names: the second word of each "type name [= default]" item -> ["sDB", "sVar"]
        params = re.findall(r"\b\w+\s+(\w+)\s*(?:=[^,]*)?(?:,|$)", m.group(2))
        # the body ends at the matching "}" (braces inside string literals are not special-cased)
        depth, j = 1, m.end()
        while j < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            j += 1
        out.append((m.end(), j, params, m.group(1)))
    return out


def _resolve(expr, local, consts, params):
    """Resolve a string argument. Returns (text, how): how = literal | constant | pattern | passed | unknown.

    local: {variable: {values assigned from a literal in this script}} - used only when there is exactly one value.
    consts: {name: value} of `const string` across the module. params: parameter names of the enclosing function.
    A joined expression keeps its known pieces and puts * for the rest: "q_" + sName -> ("q_*", "pattern")."""
    expr = expr.strip()
    if re.fullmatch(r'"(?:\\.|[^"\\])*"', expr):
        return expr[1:-1], "literal"
    if re.fullmatch(r"\w+", expr):
        if expr in local and len(local[expr]) == 1:
            return next(iter(local[expr])), "constant"
        if expr in consts:
            return consts[expr], "constant"
        if expr in params:
            return expr, "passed"
        return expr, "unknown"
    parts = re.split(r"\s*\+\s*", expr)
    if len(parts) > 1:
        pat = []
        for p in parts:
            t, how = _resolve(p, local, consts, params)
            pat.append(t if how in ("literal", "constant") else "*")
        s = re.sub(r"\*+", "*", "".join(pat))
        return (s, "pattern") if s.strip("*") else (expr, "unknown")
    return expr, "unknown"


def _sql_text(expr, local, consts):
    """The literal pieces of a query expression, joined (for finding table names)."""
    bits = re.findall(r'"((?:\\.|[^"\\])*)"', expr)
    for w in re.findall(r"\b[A-Za-z_]\w*\b", re.sub(r'"(?:\\.|[^"\\])*"', " ", expr)):
        v = consts.get(w) or (next(iter(local[w])) if w in local and len(local[w]) == 1 else None)
        if v:
            bits.append(v)
    return " ".join(bits)


def script_uses(db):
    """Every campaign-database and SQL call in the module's scripts (from the analysis index).

    db: the analysis index (sqlite3.Connection); only `scripts.name` / `scripts.source` are read, nothing is written.
    Returns a list of dicts, one per call: script, line, function, action (write / read / delete / destroy / sql /
    sql-object / sql-nwnx), vtype, db and db_how, var and var_how (see _resolve), per_player, and for SQL calls the
    `tables` named in the query. A call made through a helper function (the database or variable name is one of the
    helper's parameters) appears once marked via_helper=True, and again for each call of the helper, with `via` naming
    the script that holds the real call."""
    srcs = {name: _strip_comments(src) for name, src in
            db.execute("SELECT name, source FROM scripts WHERE source IS NOT NULL")}
    consts = {}
    for src in srcs.values():
        # const string DB_NAME = "pc_data";  (first definition wins if two scripts differ)
        for m in re.finditer(r'\bconst\s+string\s+(\w+)\s*=\s*"((?:\\.|[^"\\])*)"', src):
            consts.setdefault(m.group(1), m.group(2))
    uses, wrappers = [], {}
    for name, src in srcs.items():
        if not CALL_RE.search(src):
            continue
        local = defaultdict(set)
        # sDB = "pc_data";  - every literal each variable is given in this script
        for m in re.finditer(r'\b(\w+)\s*=\s*"((?:\\.|[^"\\])*)"\s*;', src):
            local[m.group(1)].add(m.group(2))
        funcs = _functions(src)
        # local string variables built from queries: sSQL = "SELECT ..." + ...; (joined for table names)
        sqlvars = defaultdict(list)
        for m in re.finditer(r"\b(\w+)\s*\+?=\s*([^;]*\"[^;]*);", src):
            sqlvars[m.group(1)].append(m.group(2))
        for m in CALL_RE.finditer(src):
            fn = m.group(1)
            action, vtype, ppos = CAMPAIGN[fn]
            args, _ = _args(src, m.end() - 1)
            line = src.count("\n", 0, m.start()) + 1
            # the function this call sits in: (its parameter names, its name), or ([], None) at top level
            fdef = next(((p, fn_) for s, e, p, fn_ in funcs if s <= m.start() < e), ([], None))
            params = fdef[0]
            u = dict(script=name, line=line, function=fn, action=action, vtype=vtype)
            if action in ("sql", "sql-object", "sql-nwnx"):
                # SqlPrepareQueryCampaign / SqlPrepareQueryObject take the query second, the NWNX calls first
                qexpr = args[1] if action in ("sql", "sql-object") and len(args) > 1 else (args[0] if args else "")
                text = _sql_text(qexpr, local, consts)
                for w in re.findall(r"\b[A-Za-z_]\w*\b", qexpr):
                    for extra in sqlvars.get(w, []):
                        text += " " + _sql_text(extra, local, consts)
                u["tables"] = sorted({t for t in TABLE_RE.findall(text) if t.lower() not in SQL_WORDS})
                if action == "sql":
                    u["db"], u["db_how"] = _resolve(args[0], local, consts, params) if args else ("", "unknown")
                elif action == "sql-object":
                    target = (args[0] if args else "").replace(" ", "")
                    u["db"], u["db_how"] = ("(module - kept in the save game)" if "GetModule()" in target else
                                            "(a player or object - kept in its character/save)"), "fixed"
                else:
                    u["db"], u["db_how"] = "(NWNX SQL - the server's own database)", "fixed"
                u["var"], u["var_how"] = "", "literal"
                uses.append(u)
                continue
            if not args:
                continue
            u["db"], u["db_how"] = _resolve(args[0], local, consts, params)
            if action == "destroy":
                u["var"], u["var_how"] = "", "literal"
            else:
                u["var"], u["var_how"] = _resolve(args[1], local, consts, params) if len(args) > 1 else ("", "unknown")
            # per player when a player argument is given and is not OBJECT_INVALID (the default = module-wide)
            pa = args[ppos].replace(" ", "") if ppos is not None and len(args) > ppos else ""
            u["per_player"] = bool(pa) and pa != "OBJECT_INVALID"
            if fdef[1] and (u["db_how"] == "passed" or u["var_how"] == "passed"):
                # a helper such as GetDMFIPersistentInt(sDB, sVar, oPC): its callers name the database / variable
                wrappers.setdefault(fdef[1], []).append(dict(
                    u, db_param=params.index(u["db"]) if u["db_how"] == "passed" else None,
                    var_param=params.index(u["var"]) if u["var_how"] == "passed" else None,
                    player_param=params.index(pa) if pa in params else None))
                u["via_helper"] = True
            uses.append(u)
    # second pass: calls of those helpers (two levels deep is plenty in practice)
    for _ in range(2):
        if not wrappers:
            break
        wre = re.compile(r"\b(" + "|".join(map(re.escape, wrappers)) + r")\s*\(")
        new_wrappers = {}
        for name, src in srcs.items():
            if not wre.search(src):
                continue
            local = defaultdict(set)
            for m in re.finditer(r'\b(\w+)\s*=\s*"((?:\\.|[^"\\])*)"\s*;', src):
                local[m.group(1)].add(m.group(2))
            funcs = _functions(src)
            for m in wre.finditer(src):
                # preceded by a type word ("int GetDBInt(") within 12 characters: a definition, not a call
                if re.search(r"\b(?:void|int|float|string|object|location|vector)\s+$", src[max(0, m.start() - 12):m.start()]):
                    continue                                     # the helper's own definition / prototype
                args, _ = _args(src, m.end() - 1)
                fdef = next(((p, fn_) for s_, e_, p, fn_ in funcs if s_ <= m.start() < e_), ([], None))
                for w in wrappers[m.group(1)]:
                    u = dict(w, script=name, line=src.count("\n", 0, m.start()) + 1, function=m.group(1),
                             via=w["script"])
                    for key, pk in (("db", "db_param"), ("var", "var_param")):
                        if w.get(pk) is not None:
                            u[key], u[key + "_how"] = _resolve(args[w[pk]], local, consts, fdef[0]) \
                                if len(args) > w[pk] else ("", "unknown")
                    if w.get("player_param") is not None:
                        pa = args[w["player_param"]].replace(" ", "") if len(args) > w["player_param"] else ""
                        u["per_player"] = bool(pa) and pa != "OBJECT_INVALID"
                    if fdef[1] and (u["db_how"] == "passed" or u["var_how"] == "passed") and fdef[1] not in wrappers:
                        new_wrappers.setdefault(fdef[1], []).append(dict(
                            u, db_param=fdef[0].index(u["db"]) if u["db_how"] == "passed" else None,
                            var_param=fdef[0].index(u["var"]) if u["var_how"] == "passed" else None,
                            player_param=None))
                    for k in ("db_param", "var_param", "player_param"):
                        u.pop(k, None)
                    uses.append(u)
        wrappers = new_wrappers
    for u in uses:
        for k in ("db_param", "var_param", "player_param"):
            u.pop(k, None)
    return uses


# ================================================================================================ cross-check
def _match(pattern, name):
    """True if a stored variable name matches a script's name or pattern ("q_*"), ignoring case."""
    return fnmatch.fnmatchcase(name.lower(), pattern.lower()) if "*" in pattern else pattern.lower() == name.lower()


def int_constants(db):
    """Upper-case names of integer constants (quest tokens such as Q_GUARD_REWARD).
    db: the analysis index (sqlite3.Connection), read only. Returns a set of names."""
    out = set()
    for (src,) in db.execute("SELECT source FROM scripts WHERE source LIKE '%const int%'"):
        out.update(x.upper() for x in re.findall(r"\bconst\s+int\s+(\w+)", src or ""))
    return out


def cross_check(snap, uses, unused=None, int_consts=None):
    """Compare what the scripts write and read with each other and with a loaded database snapshot.

    snap: read_path()'s snapshot, or None when no database folder is loaded (then only script-to-script checks run).
    uses: script_uses(). unused: names of scripts that can never run (their calls don't count as live).
    int_consts: int_constants() - lets a "read but never written" finding point at a quest token instead.
    Returns dict(summary, databases, findings, sql, files, tables); each finding has severity, category, text, fix,
    db, examples. Pure computation: reads and writes nothing."""
    unused = {x.lower() for x in (unused or ())}
    int_consts = int_consts or set()
    snap = snap or {}
    live = [u for u in uses if u["script"].lower() not in unused]
    # a helper's own call (name passed in) is represented by its callers' uses
    camp = [u for u in uses if u["action"] in ("write", "read", "delete", "destroy") and not u.get("via_helper")]
    dbs = {}

    def entry(name):
        """The working record for database `name`, keyed lower-case (so "PCData" in a script matches pcdata.sqlite3)."""
        return dbs.setdefault(name.lower(), dict(name=name, writes=0, reads=0, deletes=0, scripts=set(), vars={},
                                                 destroyed_by=[], file=None, per_player=False, patterns=set()))
    for u in camp:
        if u["db_how"] in ("literal", "constant"):
            d = entry(u["db"])
        elif u["db_how"] == "pattern":
            d = entry(u["db"])
            d["patterns"].add(u["db"])
        else:
            d = entry("(name passed in or built at run time)")
        d["scripts"].add(u["script"])
        if u["action"] == "destroy":
            d["destroyed_by"].append(u["script"])
            continue
        d[{"write": "writes", "read": "reads", "delete": "deletes"}[u["action"]]] += 1
        d["per_player"] |= u.get("per_player", False)
        key = u["var"] if u["var_how"] in ("literal", "constant", "pattern") else "(name built at run time)"
        v = d["vars"].setdefault(key, dict(name=key, pattern=u["var_how"] == "pattern" or key.startswith("("),
                                           written_by=set(), read_by=set(), deleted_by=set(), types=set(),
                                           read_types=set(), per_player=False, live=False))
        {"write": v["written_by"], "read": v["read_by"], "delete": v["deleted_by"]}[u["action"]].add(u["script"])
        if u["vtype"] and u["script"].lower() not in unused:     # types only from scripts that can run
            (v["types"] if u["action"] == "write" else v["read_types"]).add(u["vtype"])
        if u["vtype"]:
            v.setdefault("all_types", set()).add(u["vtype"])
        v["per_player"] |= u.get("per_player", False)
        v["live"] |= u["script"].lower() not in unused
    stored = snap.get("campaign") or {}
    for stem, c in stored.items():
        d = dbs.get(stem) or entry(stem)
        d["file"] = c
        for vn, sv in c["vars"].items():
            hit = [k for k in d["vars"] if not k.startswith("(") and _match(k, vn)]
            if hit:
                for k in hit:
                    d["vars"][k].setdefault("stored", []).append(dict(name=vn, **{x: sv[x] for x in
                                                                   ("types", "rows", "players", "module_wide", "examples")}))
            else:
                d.setdefault("stored_only", {})[vn] = sv

    findings = []

    def add(sev, cat, text, fix, db="", examples=None):
        findings.append(dict(severity=sev, category=cat, text=text, fix=fix, db=db, examples=(examples or [])[:12]))
    loaded = bool(snap.get("files"))
    for key, d in dbs.items():
        if key.startswith("("):
            continue
        live_scripts = sorted(s for s in d["scripts"] if s.lower() not in unused)
        if d["scripts"] and not live_scripts:
            continue
        runtime_writes = any(k.startswith("(") or v["pattern"] for k, v in d["vars"].items() if v["written_by"])
        for k, v in d["vars"].items():
            if k.startswith("(") or not v["live"]:
                continue
            if v["read_by"] and not v["written_by"] and not d["deletes"]:
                in_file = v.get("stored")
                if in_file:
                    add("info", "db_read_only_stored", f"'{k}' in database '{d['name']}' is read by "
                        f"{', '.join(sorted(v['read_by'])[:3])} but no script writes it - the value in the loaded file came "
                        "from an older version, another module or a DM tool", "Fine if something outside the module sets "
                        "it; otherwise check the name against the scripts that write this database.", d["name"],
                        sorted(v["read_by"]))
                elif not runtime_writes and not v["pattern"]:
                    token = k.upper() in int_consts
                    add("warning", "db_read_never_written", f"'{k}' in database '{d['name']}' is read by "
                        f"{', '.join(sorted(v['read_by'])[:3])} but no script writes it"
                        + (" and it is not in the loaded database" if key in stored else "")
                        + " - the read always gets 0 / an empty string"
                        + (f". The module has a quest token constant {k.upper()}: the quest looks like it moved to "
                           "the module's token helpers and this script still reads the old database value" if token else ""),
                        # the helper's name differs per module (Quest health shows it), so the fix names it generically
                        (f"Read the token instead, with the module's token getter (for example GetQuestToken(oPC, "
                         f"{k.upper()})) and compare with the string stage, e.g. \"5\"; or have the scripts that set the "
                         "token also write the database value." if token else
                         "Check the spelling against the names written to this database (see the Database page), or add "
                         "the SetCampaign... call that should store it."), d["name"], sorted(v["read_by"]))
            wt, rt = v["types"], v["read_types"]
            if wt and rt - wt and "object" not in wt | rt:
                add("warning", "db_type_mismatch", f"'{k}' in database '{d['name']}' is written as "
                    f"{'/'.join(sorted(wt))} but read as {'/'.join(sorted(rt - wt))} - a campaign variable is found only with "
                    "the same type, so the read gets 0 / empty", "Use the matching Get/SetCampaign function pair "
                    f"(e.g. SetCampaign{sorted(wt)[0].title()} with GetCampaign{sorted(wt)[0].title()}).", d["name"],
                    sorted(v["written_by"] | v["read_by"]))
            stored_types = {t for s in v.get("stored", []) for t in s["types"]}
            if rt and stored_types and rt - stored_types - wt and "object" not in rt:
                add("warning", "db_type_mismatch", f"'{k}' in database '{d['name']}' is stored as "
                    f"{'/'.join(sorted(stored_types))} in the loaded file but the scripts read it as "
                    f"{'/'.join(sorted(rt - stored_types - wt))}", "Read it with the function for the stored type, or write it again with "
                    "the new type (old rows stay under the old type).", d["name"], sorted(v["read_by"]))
        if loaded and not d["file"] and live_scripts:
            add("info", "db_no_file", f"database '{d['name']}' is used by {len(live_scripts)} script(s) but there is no "
                f"'{d['name'].lower()}.sqlite3' in the loaded folder", "Normal for a fresh server or when the data lives "
                "on the server - load the server's database folder to check what is stored.", d["name"], live_scripts)
        if d.get("stored_only"):
            names = sorted(d["stored_only"])
            if d["scripts"]:
                add("info", "db_stored_unused", f"{len(names)} variable(s) stored in '{d['name']}' are not used by any "
                    "script in this module (left by an older version, another module or a DM tool)",
                    "Harmless. To tidy, stop the server, copy the database, delete the rows with an SQLite tool, and "
                    "test on the copy - or DeleteCampaignVariable in a one-off script.", d["name"], names)
        if d["destroyed_by"]:
            add("info", "db_destroyed", f"{', '.join(sorted(set(d['destroyed_by']))[:3])} call(s) "
                f"DestroyCampaignDatabase on '{d['name']}' - that wipes every value in it, for every player",
                "Make sure that is only reachable by a DM or on purpose (a reset).", d["name"], sorted(set(d["destroyed_by"])))
    for stem, c in stored.items():
        d = dbs.get(stem)
        if not (d and d["scripts"]):
            add("info", "db_not_used", f"database file '{stem}' ({sum(v['rows'] for v in c['vars'].values())} row(s)) is not "
                "used by any script in this module - the database folder is shared by every module this game or server "
                "runs, so it may belong to another module", "Nothing to do. Remove it only if you know no module "
                "uses it (move it out while the server is stopped).", stem)
        kinds = {s["kind"] for s in c["sources"]}
        if "campaign (EE)" in kinds and len(kinds) > 1:
            done = any(s.get("import_done") for s in c["sources"])
            add("info", "db_legacy_copy", f"'{stem}' exists both as the old BioWare .DBF files and as '{stem}.sqlite3'"
                + (" - EE already copied the old data into the .sqlite3 (import done), so the .DBF/.CDX/.FPT files "
                   "are no longer read" if done else ""),
                "The .DBF/.CDX/.FPT files can be moved to an archive folder (keep a copy).", stem)
        elif kinds == {"campaign (old BioWare .DBF)"}:
            add("info", "db_legacy_only", f"'{stem}' exists only in the old BioWare format - EE converts it to "
                f"'{stem}.sqlite3' the first time a script opens it", "Nothing to do; keep the .DBF/.CDX/.FPT files "
                "until the .sqlite3 exists.", stem)
    runtime = dbs.get("(name passed in or built at run time)")
    if runtime and any(s.lower() not in unused for s in runtime["scripts"]):
        add("info", "db_runtime_name", f"{len(runtime['scripts'])} script(s) use a database whose name is passed in or "
            "built at run time (usually a library such as DMFI's database include) - those uses can't be tied to a "
            "file here", "Look at the callers of these functions to see which names they pass.", "",
            sorted(runtime["scripts"]))

    # SQL tables
    sql = []
    tables = snap.get("tables") or {}
    by_target = defaultdict(lambda: dict(scripts=set(), tables=set()))
    for u in uses:
        if u["action"].startswith("sql"):
            key = u["db"].lower() if u["db_how"] in ("literal", "constant") else u["db"]
            by_target[key]["scripts"].add(u["script"])
            by_target[key]["tables"].update(u.get("tables") or [])
    for key, t in by_target.items():
        have = {x.lower() for x in (tables.get(key) or {})}
        missing = sorted(x for x in t["tables"] if have and x.lower() not in have)
        sql.append(dict(target=key, scripts=sorted(t["scripts"]), tables=sorted(t["tables"]),
                        in_file=sorted(tables.get(key) or {}), missing=missing))
        if missing:
            add("info", "db_table_missing", f"scripts query table(s) {', '.join(missing[:5])} in '{key}' that the "
                "loaded file doesn't have yet", "Normal if a script creates them (CREATE TABLE IF NOT EXISTS) on first "
                "use; otherwise check the table name.", key, sorted(t["scripts"]))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3), f["category"], f["db"]))

    def pub(d):
        """One database's working record as JSON-ready output: sets become sorted lists, stored-only variables are
        added after the ones the scripts use."""
        vars_ = []
        for k, v in sorted(d["vars"].items()):
            vars_.append(dict(name=k, pattern=v["pattern"], written_by=sorted(v["written_by"]),
                              read_by=sorted(v["read_by"]), deleted_by=sorted(v["deleted_by"]),
                              types=sorted(v.get("all_types") or ()), per_player=v["per_player"], live=v["live"],
                              stored=v.get("stored", [])))
        for k, sv in sorted((d.get("stored_only") or {}).items()):
            vars_.append(dict(name=k, pattern=False, written_by=[], read_by=[], deleted_by=[], types=sv["types"],
                              per_player=sv["players"] > 0, live=False, stored=[dict(name=k, **{x: sv[x] for x in
                              ("types", "rows", "players", "module_wide", "examples")})], stored_only=True))
        f = d["file"]
        return dict(name=d["name"], scripts=sorted(d["scripts"]), writes=d["writes"], reads=d["reads"],
                    deletes=d["deletes"], per_player=d["per_player"], destroyed_by=sorted(set(d["destroyed_by"])),
                    file=[s for s in f["sources"]] if f else [], players=f["players"] if f else 0,
                    stored_vars=len(f["vars"]) if f else 0, vars=vars_,
                    live=any(s.lower() not in unused for s in d["scripts"]))
    databases = sorted((pub(d) for d in dbs.values()), key=lambda x: (x["name"].startswith("("), x["name"].lower()))
    summary = dict(databases_used=sum(1 for d in databases if d["scripts"] and not d["name"].startswith("(")),
                   files=len(snap.get("files") or []), stored_rows=sum(f.get("rows") or 0 for f in snap.get("files") or []),
                   script_calls=len(camp), live_calls=sum(1 for u in live if u["action"] in ("write", "read", "delete", "destroy")),
                   sql_calls=sum(1 for u in uses if u["action"].startswith("sql")),
                   warnings=sum(1 for f in findings if f["severity"] == "warning"),
                   loaded=snap.get("loaded"), folder=snap.get("folder"))
    return dict(summary=summary, databases=databases, findings=findings, sql=sql,
                files=snap.get("files") or [], tables={k: v for k, v in tables.items()})
