"""
nwn_2da.py - read, write and check 2DA tables for the 2da editor.

A table is a plain dict (so it travels to the browser as JSON unchanged):
    {"columns": ["LABEL", "RACE", ...],
     "rows":    [["0", "Dwarf", "D", ...], ...],      # first cell = row label, then one value per column
     "default": None or "text",                        # optional DEFAULT: line
     "origin":  [0, 1, None, ...]}                     # which original row each row came from (None = new)
A value of None means **** (empty).

Why "origin" matters: NWN refers to 2da rows by POSITION. Deleting or inserting a row in the middle moves every row
after it, so every creature/item/placeable that pointed at those rows silently points at a different one. The editor
keeps track of where each row came from so the checks can say exactly which used rows would move.

What it reads and writes
------------------------
Text only: parse() takes the file's text and write() returns new text. Nothing here opens, saves or deletes a file;
the hak editor and the dashboard decide where the text goes (always a copy in the workspace, never the original).

Public functions
----------------
    parse(text)              -> (table, issues)    never raises for content problems
    write(table)             -> text               aligned like the toolset, CRLF line ends
    validate(table, ctx)     -> [issue]            every check below, in plain words
    row_move_checks / base_compare                 parts of validate, also usable alone
    blocking(issues), renumber(table), label_of(table, row)

An issue is dict(level="error"|"warning"|"info", msg, row?, col?, line?, fix?, blocking?). `blocking` marks an issue
that should stop a save ("usage" = it would break rows the module uses).

Limits: column kinds (talk-table number, resource name, script) are guessed from column names, so a column with an
unusual name is not checked.
"""
from __future__ import annotations

import re

import nwnlib as n

CUSTOM = n.CUSTOM_TLK_BIT          # talk-table numbers from 0x01000000 up point into the module's custom .tlk
# columns that hold talk-table numbers (compared lower-case)
STRREF_COLS = {"name", "description", "strref", "string_ref", "plural", "lower", "convername", "convernamelower",
               "descstrref", "spelldesc", "featstrref", "gamestrref", "tlkstrref", "strref_name", "altmessage",
               "nameref", "desc", "shortname", "tooltip"}
# columns that hold resource names (<= 16 characters). A name heuristic, any case: the name contains resref, model,
# icon, script, sound, texture, portrait or bmp, starts with wav, env or wing, or is exactly race or tail
# (e.g. "ModelA", "ICON", "ImpactScript", "WING_Model"; "RACE" in appearance.2da holds a model name)
RESREF_COL_RE = re.compile(r"resref|model|icon|script|sound|texture|portrait|^wav|bmp|^env|^race$|^tail$|^wing", re.I)
SCRIPT_COL_RE = re.compile(r"script", re.I)            # resource-name columns that name a script
LABEL_COLS = ("label", "LABEL", "Label")


# ------------------------------------------------------------------ parse / write
_TOKEN = re.compile(r'"[^"]*"|\S+')                    # a "quoted value" (may hold spaces) or a run of non-spaces


def _split(line):
    """NWN's rule: values are separated by spaces/tabs; a value with spaces is wrapped in double quotes.
    Apostrophes and backslashes are ordinary characters (shlex would treat them as quoting/escapes).
    Returns (values, error): error is a message when the line has an odd number of quote characters, else None."""
    toks = [t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t for t in _TOKEN.findall(line)]
    err = "unbalanced quote - a value that contains spaces must be in \"quotes\"" if line.count('"') % 2 else None
    return toks, err


def parse(text: str):
    """Parse 2DA V2.0 text into a table dict (layout in the module docstring).

    Line 1 is the "2DA V2.0" header, then an optional "DEFAULT: value" line, then the column names, then one row per
    line (row label first). Blank lines are skipped. Missing values read as None (****), extra values are dropped.
    Returns (table, issues); issues carry the 1-based `line` (and `row` index for row problems).
    Never raises for content problems: every problem becomes an issue with its line number."""
    issues = []
    lines = text.replace("\r", "").split("\n")
    head = lines[0].strip() if lines else ""
    if not head.upper().startswith("2DA"):
        issues.append(dict(level="error", line=1, msg="the first line must be '2DA V2.0' - this is not a 2da file"))
    elif head.split()[1:2] != ["V2.0"]:
        issues.append(dict(level="warning", line=1, msg=f"first line is '{head}'; the game expects '2DA V2.0'"))
    i = 1
    default = None
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().upper().startswith("DEFAULT:"):
        dv = lines[i].split(":", 1)[1].strip()
        default = (dv[1:-1] if len(dv) >= 2 and dv[0] == dv[-1] == '"' else dv) or None
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    table = dict(columns=[], rows=[], default=default, origin=[])
    if i >= len(lines):
        issues.append(dict(level="error", line=i, msg="no column header line"))
        return table, issues
    cols, err = _split(lines[i])
    if err:
        issues.append(dict(level="error", line=i + 1, msg="column header: " + err))
    table["columns"] = cols
    for ln in range(i + 1, len(lines)):
        line = lines[ln]
        if not line.strip():
            continue
        parts, err = _split(line)
        if err:
            issues.append(dict(level="error", line=ln + 1, row=len(table["rows"]), msg=err))
        label, vals = parts[0], parts[1:]
        if len(vals) < len(cols):
            issues.append(dict(level="warning", line=ln + 1, row=len(table["rows"]),
                               msg=f"row {label} has {len(vals)} value(s) but there are {len(cols)} columns - "
                                   "the missing ones read as **** (the editor fills them in when you save)"))
        elif len(vals) > len(cols):
            issues.append(dict(level="error", line=ln + 1, row=len(table["rows"]),
                               msg=f"row {label} has {len(vals)} values but only {len(cols)} columns - the extra "
                                   "values are ignored by the game (usually a value with spaces that is not in quotes)"))
        vals = (vals + [None] * len(cols))[:len(cols)]          # pad short rows with None, cut long ones
        table["rows"].append([label] + [None if v in (None, "****", "") else v for v in vals])
        table["origin"].append(len(table["origin"]))            # freshly read: each row comes from its own position
    return table, issues


def _cell(v):
    """One value as 2da text: None or "" -> ****, a value containing white space -> "quoted"."""
    if v is None or v == "":
        return "****"
    v = str(v)
    return f'"{v}"' if re.search(r"\s", v) else v


def write(table) -> str:
    """Aligned text like the toolset writes it. None -> ****, values with spaces quoted.

    table: a table dict as returned by parse(); rows shorter than the column list are padded with ****.
    Returns the text with Windows (CRLF) line ends. Writes no file. A value containing a double quote is written as it
    is (validate() reports it as a blocking error first)."""
    cols = list(table["columns"])
    rows = [[_cell(r[0])] + [_cell(v) for v in (list(r[1:]) + [None] * len(cols))[:len(cols)]] for r in table["rows"]]
    wl = max([len(r[0]) for r in rows] + [1])
    widths = [max([len(c)] + [len(r[k + 1]) for r in rows]) for k, c in enumerate(cols)]
    out = ["2DA V2.0"]
    # line 2 is either DEFAULT: or blank; the column names are always on line 3
    out.append(f"DEFAULT: {_cell(table['default'])}" if table.get("default") else "")
    out.append(" " * (wl + 1) + " ".join(c.ljust(widths[k]) for k, c in enumerate(cols)).rstrip())
    for r in rows:
        out.append((r[0].ljust(wl) + " " + " ".join(v.ljust(widths[k]) for k, v in enumerate(r[1:]))).rstrip())
    return "\r\n".join(out) + "\r\n"


# ------------------------------------------------------------------ checks
def _is_int(v):
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def label_of(table, row):
    """A readable name for a row (a list: row label, then values): its Label column if it has one and it is set,
    else the first non-empty value, else ""."""
    for c in LABEL_COLS:
        if c in table["columns"]:
            v = row[1 + table["columns"].index(c)]
            if v:
                return v
    return next((v for v in row[1:] if v), "")


def validate(table, ctx=None):
    """
    Checks in plain words. ctx (all optional):
      name            table name without .2da
      original        the table as it was before editing (for row moves)
      row_usage       {"row number as text": ["what uses it", ...]}   (from the module analysis)
      usage_known     True when row_usage came from an analysis (so "no users" really means none found)
      script_readers  [script names that read this table with Get2DAString]
      tlk             nwnlib.TlkSet (base + custom) for talk-table checks; tlk_custom_known = custom tlk available
      known_names     set of "resref.ext" that exist (module + haks + override + base game) for script checks
      base            the base game's copy of this table (a table dict) for comparison
    Returns a list of dict(level, msg, row?, col?, fix?, blocking?).
    """
    ctx = ctx or {}
    out = []
    cols = table["columns"]
    seen = {}
    for k, c in enumerate(cols):
        if not c or re.search(r"\s", c):
            out.append(dict(level="error", col=k, msg=f"column {k + 1} has an empty name or a name with spaces", blocking=True))
        lc = c.lower()
        if lc in seen:
            out.append(dict(level="error", col=k, msg=f"column '{c}' appears twice (also column {seen[lc] + 1}) - the game "
                                                      "would only ever read one of them", blocking=True))
        seen.setdefault(lc, k)
    # row numbering
    wrong = [i for i, r in enumerate(table["rows"]) if str(r[0]) != str(i)]
    if wrong:
        first = wrong[0]
        out.append(dict(level="warning", row=first, fix="renumber",
                        msg=f"{len(wrong)} row number(s) don't match their position (first: position {first} is labelled "
                            f"'{table['rows'][first][0]}'). The game goes by position, not the label - use Renumber rows"))
    # cells
    strref_cols = [k for k, c in enumerate(cols) if c.lower() in STRREF_COLS]
    resref_cols = [k for k, c in enumerate(cols) if RESREF_COL_RE.search(c) and k not in strref_cols]
    tlk = ctx.get("tlk")
    bad_tlk = []
    long_names = []
    quotes = []
    for i, r in enumerate(table["rows"]):
        for k, v in enumerate(r[1:]):
            if v is None:
                continue
            if '"' in str(v):
                quotes.append((i, k))
            if k in strref_cols:
                if not _is_int(v):
                    out.append(dict(level="warning", row=i, col=k,
                                    msg=f"row {i} column {cols[k]}: '{v}' should be a talk-table number (or ****)"))
                elif tlk is not None:
                    s = int(v)
                    if s >= CUSTOM and not ctx.get("tlk_custom_known"):
                        continue                     # no custom tlk loaded: can't tell, so say nothing
                    st = tlk.status(s)
                    if st in ("custom-missing", "base-missing"):
                        bad_tlk.append((i, k, s, st))
            # a value with spaces is text, not a resource name, even in a column whose name looks like one
            if k in resref_cols and len(str(v)) > 16 and not re.search(r"\s", str(v)):
                long_names.append((i, k, v))
    # the lists below are capped (20 / 50 / 30) so one bad column can't bury every other message
    for i, k in quotes[:20]:
        out.append(dict(level="error", row=i, col=k, blocking=True,
                        msg=f"row {i} column {cols[k]}: a value can't contain the \" character"))
    for i, k, s, st in bad_tlk[:50]:
        where = "the custom talk table" if s >= CUSTOM else "the game's dialog.tlk"
        out.append(dict(level="warning", row=i, col=k,
                        msg=f"row {i} column {cols[k]}: talk-table number {s} is not in {where} - it will show blank in game"))
    if len(bad_tlk) > 50:
        out.append(dict(level="warning", msg=f"...and {len(bad_tlk) - 50} more missing talk-table numbers"))
    for i, k, v in long_names[:30]:
        out.append(dict(level="error", row=i, col=k,
                        msg=f"row {i} column {cols[k]}: '{v}' is {len(str(v))} characters - resource names over 16 "
                            "characters cannot be loaded"))
    known = ctx.get("known_names")
    if known:
        for k in [k for k in resref_cols if SCRIPT_COL_RE.search(cols[k])]:
            miss = sorted({str(r[1 + k]).lower() for r in table["rows"] if r[1 + k] and
                           f"{str(r[1 + k]).lower()}.ncs" not in known and f"{str(r[1 + k]).lower()}.nss" not in known})
            if miss:
                out.append(dict(level="warning", col=k,
                                msg=f"column {cols[k]} names {len(miss)} script(s) that don't exist in the module, its haks "
                                    f"or the game: {', '.join(miss[:6])}{' …' if len(miss) > 6 else ''}"))
    lab = next((c for c in LABEL_COLS if c in cols), None)
    if lab:
        k = cols.index(lab)
        cnt = {}
        for r in table["rows"]:
            if r[1 + k]:
                cnt[r[1 + k]] = cnt.get(r[1 + k], 0) + 1
        dups = [x for x, c in cnt.items() if c > 1]
        if dups:
            out.append(dict(level="info", col=k, msg=f"{len(dups)} label(s) are used on more than one row "
                                                      f"(e.g. {', '.join(dups[:4])}) - fine for the game, confusing in the toolset"))
    out += row_move_checks(table, ctx)
    base = ctx.get("base")
    if base:
        out += base_compare(table, base)
    return out


def row_move_checks(table, ctx):
    """What happens to rows the module uses when rows are deleted, inserted or moved.

    Compares table["origin"] (original row number of each current row, None for a new row) with ctx["original"];
    uses ctx row_usage / usage_known / script_readers as described in validate(). Returns a list of issues
    ([] when there is no original to compare with)."""
    orig = ctx.get("original")
    if not orig:
        return []
    out = []
    usage = ctx.get("row_usage") or {}
    origin = table.get("origin") or list(range(len(table["rows"])))
    now_at = {o: i for i, o in enumerate(origin) if o is not None}      # original row number -> current position
    n_orig = len(orig["rows"])
    gone = [o for o in range(n_orig) if o not in now_at]
    moved = [(o, now_at[o]) for o in range(n_orig) if o in now_at and now_at[o] != o]
    blank_used = []
    for o, i in now_at.items():
        if str(o) in usage and o < n_orig and all(v is None for v in table["rows"][i][1:]) and \
                any(v is not None for v in orig["rows"][o][1:]):
            blank_used.append(o)
    readers = ctx.get("script_readers") or []
    for o in gone:
        users = usage.get(str(o))
        if users:
            out.append(dict(level="error", row=None, blocking="usage",
                            msg=f"row {o} ({label_of(orig, orig['rows'][o])}) is deleted but is used by {len(users)} thing(s) "
                                f"(e.g. {', '.join(users[:3])}) - they would lose it. Blank the values instead, or keep the row"))
    moved_used = [(o, i) for o, i in moved if usage.get(str(o))]
    for o, i in moved_used[:20]:
        users = usage[str(o)]
        out.append(dict(level="error", blocking="usage",
                        msg=f"row {o} ({label_of(orig, orig['rows'][o])}) would become row {i}: the {len(users)} thing(s) using it "
                            f"(e.g. {', '.join(users[:3])}) would silently point at a different row. Undo the insert/delete "
                            "above it, or add new rows at the end"))
    if len(moved_used) > 20:
        out.append(dict(level="error", blocking="usage", msg=f"...and {len(moved_used) - 20} more used rows would move"))
    if moved and not moved_used:
        who = "No module content uses the moved rows" if ctx.get("usage_known") else \
            "Row usage is unknown (open this with an analysed module to check)"
        out.append(dict(level="warning",
                        msg=f"{len(moved)} row(s) change number (rows are referenced by position). {who}; other haks, "
                            "scripts and player characters may still refer to them by number. Safer: add new rows at the end"))
    if gone and not moved and not any(usage.get(str(o)) for o in gone):
        out.append(dict(level="info", msg=f"{len(gone)} row(s) removed from the end of the table"))
    for o in blank_used[:20]:
        out.append(dict(level="warning", msg=f"row {o} is now blank (****) but {len(usage[str(o)])} thing(s) use it "
                                             f"(e.g. {', '.join(usage[str(o)][:3])})"))
    if readers and (gone or moved):
        out.append(dict(level="warning", msg=f"scripts read this table by row number at runtime ({', '.join(readers[:5])}"
                                             f"{' …' if len(readers) > 5 else ''}) - check them after moving rows"))
    removed_cols = [c for c in orig["columns"] if c not in table["columns"]]
    if removed_cols:
        out.append(dict(level="warning", msg=f"column(s) removed: {', '.join(removed_cols)} - the game or scripts may "
                                             "read them by name"))
    return out


def base_compare(table, base):
    """Compare with the base game's copy of the table (`base`, a table dict). Columns are matched by name ignoring
    case, rows by position. Returns warnings for missing columns or rows and one info line counting changed rows.

    A 2da in a hak or module replaces the game's whole table, which is why fewer rows means game rows are hidden."""
    out = []
    missing_cols = [c for c in base["columns"] if c.lower() not in {x.lower() for x in table["columns"]}]
    nb, nt = len(base["rows"]), len(table["rows"])
    if missing_cols:
        out.append(dict(level="warning", msg=f"the installed game's version has column(s) this one lacks: "
                                             f"{', '.join(missing_cols[:8])} - the game may expect them"))
    if nt < nb:
        out.append(dict(level="warning", msg=f"the installed game's version has {nb} rows; this has {nt}. While this copy "
                                             f"loads, game rows {nt}-{nb - 1} are hidden (this copy is older than your game)"))
    bcols = base["columns"]
    idx = {c.lower(): k for k, c in enumerate(table["columns"])}
    changed = 0
    for i in range(min(nb, nt)):
        b = base["rows"][i]
        t = table["rows"][i]
        if any((b[1 + k] or None) != (t[1 + idx[c.lower()]] if c.lower() in idx else None) for k, c in enumerate(bcols)):
            changed += 1
    out.append(dict(level="info", msg=f"compared with the installed game: {changed} of the first {min(nb, nt)} rows are "
                                      f"customised; {max(0, nt - nb)} row(s) are added after the game's rows"))
    return out


def blocking(issues):
    """The issues that should stop a save (those with a true `blocking` value)."""
    return [i for i in issues if i.get("blocking")]


def renumber(table):
    """A copy of the table whose row labels are 0, 1, 2... by position (the game ignores the labels and goes by
    position; this makes them agree). Values and `origin` are unchanged; the input table is not modified."""
    t = dict(table)
    t["rows"] = [[str(i)] + list(r[1:]) for i, r in enumerate(table["rows"])]
    return t
