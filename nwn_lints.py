"""
nwn_lints.py - cheap checks for known NWN:EE pitfalls (evidence in docs/COMMUNITY_RESEARCH.md).

  resref_case     blueprint/instance resrefs with capital letters: in EE, GetResRef() returns the name as typed
                  (1.69 always returned lower case), so scripts comparing it to lower-case text stop matching
                  (Beamdog nwn-issues #132)
  portrait_prefix creatures whose portrait resref doesn't start with po_ (the game adds/expects it), and rows of a
                  custom portraits.2da whose po_<name><size> textures don't exist
  area_tag        areas with an empty tag (reported to crash saved-character reloads on a PW) or a tag shared by
                  several areas (GetObjectByTag finds only one)
  include_depth   #include chains close to / over the compiler's nesting limit (64)
  identifier_load scripts whose includes bring in a very large number of names, close to the compiler's
                  identifier table limit (approximate count)
  hak_size        haks over (or near) the 2 GB ERF limit
Each check returns issue dicts shaped like the analysis' issues: severity, category, node, label, detail.

Entry point: run_all(db, g, base_names), called by nwn_analysis.run_analysis near the end of the analysis pass.
Reads the analysis index (sources, files, fields, twoda, scripts tables), the dependency graph's #include edges, and -
for hak_size only - the size of each hak file on disk (os.path.getsize; the hak is never opened). Writes nothing:
the caller adds the returned issues to the report.

Limits: the include and identifier counts are approximations from the index (see each function). resref_case,
portrait_prefix and area_tag look at the module's own files only, not haks.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

import nwnlib as n

INCLUDE_LIMIT = 64                # the nesting limit this check uses (see the module docstring)
IDENTIFIER_LIMIT = 16384          # EE raised the compiler's identifier table from 8K (nwn.wiki, Major Changes in EE)
# A declaration that starts in column 0: optional const, a type, then the declared name followed by =, ; or (.
#   "const int MAX_LEVEL = 40;" -> MAX_LEVEL    "void DoSpawn(object o)" -> DoSpawn    "struct pc_data gData;" -> gData
# Indented lines (locals inside functions) don't match, so this roughly counts the names a file adds at top level.
TOP_DECL = re.compile(r"^(?:const\s+)?(?:int|float|string|object|location|vector|effect|itemproperty|talent|event|"
                      r"json|sqlquery|cassowary|struct\s+\w+|void)\s+(\w+)\s*(?:=|;|\()", re.M)


def _module_sid(db):
    """The sources.id of the module itself (haks and overrides are other sources), or None."""
    r = db.execute("SELECT id FROM sources WHERE kind='module'").fetchone()
    return r[0] if r else None


def resref_case(db):
    """One warning listing the module's blueprint/instance resrefs (TemplateResRef / ResRef fields) that contain
    capital letters, or [] when there are none. db: the analysis index, sqlite3.Connection. Read-only."""
    sid = _module_sid(db)
    rows = db.execute("SELECT f.relpath, fi.path, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
                      "WHERE f.source_id=? AND fi.type='resref' AND fi.label IN ('TemplateResRef','ResRef') "
                      "AND fi.value <> lower(fi.value)", (sid,)).fetchall()
    if not rows:
        return []
    names = sorted({v for _r, _p, v in rows})
    return [dict(severity="warning", category="resref_case", node="module", label="resrefs with capital letters",
                 detail=f"{len(names)} blueprint resref(s) contain capital letters (e.g. {', '.join(names[:6])}). In EE, "
                        "GetResRef() returns them as typed (1.69 returned lower case), so a script comparing "
                        "GetResRef(o) == \"lowercase\" no longer matches. Rename to lower case, or compare with "
                        "GetStringLowerCase(GetResRef(o)).")]


def _base_portrait_names(db):
    """The BaseResRef values (lower case) of the game's own portraits.2da, from the install recorded in the index (meta
    nwn_root); None without one or when it can't be read (then every row is judged, as before). A value the game's own
    table lists is the game's business wherever a copy puts it - the game ships rows for portraits that were cut or
    never made (ghost_, xaribethe_ ...), and a copy that inserts a row only shifts them. Never raises."""
    try:
        row_ = db.execute("SELECT value FROM meta WHERE key='nwn_root'").fetchone()
        if not row_ or not row_[0]:
            return None
        data = n.BaseGame(row_[0]).get("portraits.2da")
        if not data:
            return None
        t = n.read_2da(n.decode_text(data))
        col = next((i for i, c in enumerate(t.columns) if c.lower() == "baseresref"), None)
        if col is None:
            return None
        return {(vals[col] or "").lower() for _label, vals in t.rows if col < len(vals) and vals[col]}
    except Exception:  # noqa - a damaged or missing install only means nothing to compare with
        return None


def portraits(db, all_names):
    """portrait_prefix and portrait_files warnings.

    db: the analysis index, sqlite3.Connection. all_names: set of "resref.ext" names available anywhere (module, haks,
    override, base game). Checks the Portrait field of the module's creatures (.utc) and characters (.bic): the first
    50 get an issue each, then one more issue says how many others there are. Also the BaseResRef rows of the
    portraits.2da players get whose value the game's own table doesn't list (_base_portrait_names). Read-only."""
    out = []
    sid = _module_sid(db)
    # In SQL LIKE "_" matches any one character, so it is escaped ('po\_%') to mean a literal "po_" prefix.
    bad = db.execute("SELECT f.relpath, f.node, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
                     "WHERE f.source_id=? AND f.ext IN ('utc','bic') AND fi.label='Portrait' AND fi.path='Portrait' "
                     "AND fi.value <> '' AND lower(fi.value) NOT LIKE 'po\\_%' ESCAPE '\\'", (sid,)).fetchall()
    for relpath, node, val in bad[:50]:
        out.append(dict(severity="warning", category="portrait_prefix", node=node, label=relpath,
                        detail=f"portrait '{val}' doesn't start with po_ - portrait files must be named "
                               f"po_<name><size>.tga (e.g. po_{val}m.tga) or the portrait shows blank"))
    if len(bad) > 50:
        # the cap keeps the issue list readable; one summary row says the problem is bigger than what is listed
        out.append(dict(severity="warning", category="portrait_prefix", node="module", label="module",
                        detail=f"{len(bad) - 50} more creature(s)/character(s) have a portrait that doesn't start "
                               f"with po_ ({len(bad)} in all; only the first 50 are listed above)"))
    # portraits.2da rows whose textures are missing - in the copy players get (the first-listed hak's, else the
    # module's: nwnlib.source_rank; an override folder's copy is the builder's or the server's, never the players'),
    # leaving out values the game's own table lists (_base_portrait_names; a tester's report)
    win = db.execute("SELECT f.id FROM files f JOIN sources s ON s.id=f.source_id WHERE lower(f.resref)='portraits' "
                     f"AND f.ext='2da' AND s.kind IN ('hak', 'module') ORDER BY {n.source_rank_sql()}, s.id "
                     "LIMIT 1").fetchone()
    rows = db.execute("SELECT row, value FROM twoda WHERE file_id=? AND name='portraits' AND lower(col)='baseresref' "
                      "ORDER BY CAST(row AS INTEGER)", (win[0],)).fetchall() if win else []
    game = _base_portrait_names(db) if rows else None
    missing, seen = [], set()
    for row, base in rows:
        if row in seen:                    # a table with two BaseResRef columns: the first one counts, once per row
            continue
        seen.add(row)
        if not base or base == "****":    # "****" is the 2da notation for an empty cell
            continue
        b = base.lower()
        if game is not None and b in game:
            continue
        # the game appends a size letter (h, l, m, s, t) to po_<BaseResRef>; any one of them counts as present
        if not any(f"po_{b}{sz}.{e}" in all_names for sz in "hlmst" for e in ("tga", "dds")):
            missing.append((row, b))
    if missing:
        import json
        try:
            gone = json.loads((db.execute("SELECT value FROM meta WHERE key='haks_missing'").fetchone() or ["[]"])[0])
        except ValueError:
            gone = []
        detail = (f"{len(missing)} portrait row(s) have no po_<name><size> texture in the module, haks, override or "
                  f"game (e.g. " + ", ".join(f"row {r} {b}" for r, b in missing[:6]) + "): those portraits show blank")
        if game is None:
            detail += (" (the game's own portraits.2da couldn't be read, so rows the game itself ships are included - "
                       "set the NWN install folder in Settings)")
        if gone:
            detail += f" ({len(gone)} hak(s) not loaded - the pictures, or the copy of the table the game uses, may be there)"
        out.append(dict(severity="info" if gone else "warning", category="portrait_files", node="2da:portraits",
                        label="portraits.2da", detail=detail))
    return out


def area_tags(db):
    """area_tag issues for the module's areas (.are): an error per empty tag, a warning per tag shared by several
    areas. db: the analysis index, sqlite3.Connection. Read-only."""
    out = []
    sid = _module_sid(db)
    rows = db.execute("SELECT f.resref, f.node, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
                      "WHERE f.source_id=? AND f.ext='are' AND fi.path='Tag'", (sid,)).fetchall()
    by_tag = defaultdict(list)
    for resref, node, tag in rows:
        if not (tag or "").strip():
            out.append(dict(severity="error", category="area_tag", node=node, label=resref,
                            detail=f"area {resref} has an empty tag - builders report this crashing saved-character "
                                   "reloads on persistent worlds, and GetObjectByTag can't find it"))
        else:
            by_tag[tag].append(resref)
    for tag, areas in by_tag.items():
        if len(areas) > 1:
            out.append(dict(severity="warning", category="area_tag", node=f"area:{areas[0]}", label=tag,
                            detail=f"{len(areas)} areas share the tag '{tag}' ({', '.join(areas[:6])}) - "
                                   "GetObjectByTag and transitions by tag only ever find one of them"))
    return out


def include_depth(g, scripts_with_main, limit=INCLUDE_LIMIT):
    """include_depth issues: an error when a script's longest #include chain is `limit` or deeper, a warning from
    75% of `limit`.

    g: the dependency graph (nwn_analysis.Graph or anything with the same `fwd` map). scripts_with_main: names of the
    scripts to check (those the compiler builds on their own). The depth is the number of #include steps in the
    longest chain; include loops are cut where they repeat. The result does not depend on the order of
    scripts_with_main (tests/test_quests.py checks this). Read-only."""
    inc = defaultdict(list)
    for src, lst in g.fwd.items():
        if src.startswith("script:"):
            for dst, kind, _via in lst:
                if kind == "include":
                    inc[src].append(dst)
    memo = {}
    CAP = 200     # stop walking at this depth: far past the limit already, and keeps the recursion shallow

    def depth(nd, stack):
        """(longest #include chain below nd, touched a cycle?). Results that passed through a cycle depend on where
        the walk started, so they are not remembered."""
        if nd in memo:
            return memo[nd], False
        if nd in stack:
            return 0, True
        if len(stack) >= CAP:
            return CAP, True
        kids = inc.get(nd)
        if not kids:
            memo[nd] = 0
            return 0, False
        best, cyc = 0, False
        stack.add(nd)
        for x in kids:
            d_, c_ = depth(x, stack)
            best, cyc = max(best, d_ + 1), cyc or c_
        stack.discard(nd)
        if not cyc:
            memo[nd] = best
        return best, cyc
    out = []
    for s in scripts_with_main:
        d, _cyc = depth(f"script:{s}", set())
        if d >= limit:
            shown = f"over {CAP}" if d >= CAP else str(d)
            out.append(dict(severity="error", category="include_depth", node=f"script:{s}", label=s,
                            detail=f"#include chain is {shown} deep - over the compiler's limit of {limit} "
                                   "(the toolset compiler can crash)"))
        elif d >= int(limit * 0.75):
            out.append(dict(severity="warning", category="include_depth", node=f"script:{s}", label=s,
                            detail=f"#include chain is {d} deep (limit {limit})"))
    return out


def identifier_load(g, sources, scripts_with_main):
    """Approximate number of names each compilable script pulls in through its includes.

    g: the dependency graph (uses its #include edges). sources: {script name: source text}. scripts_with_main: names
    to check. Counts the distinct top-level names (TOP_DECL) in the script and everything it includes, directly or
    not; an include with no source in the index adds nothing, so the count can be low. Returns identifier_load issues:
    an error at IDENTIFIER_LIMIT or more, a warning from 75% of it. Read-only."""
    names = {s: set(TOP_DECL.findall(_strip_comments(src))) for s, src in sources.items()}
    inc = defaultdict(set)
    for src, lst in g.fwd.items():
        if src.startswith("script:"):
            for dst, kind, _via in lst:
                if kind == "include":
                    inc[src[7:]].add(dst[7:])
    closure_cache = {}

    def closure(s):
        """The script plus every script it includes, directly or not (cached: big libraries are shared)."""
        if s in closure_cache:
            return closure_cache[s]
        seen, todo = set(), [s]
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            todo += list(inc.get(x, ()))
        closure_cache[s] = seen
        return seen
    out = []
    for s in scripts_with_main:
        total = set()
        for x in closure(s):
            total |= names.get(x, set())
        n = len(total)
        if n >= IDENTIFIER_LIMIT:
            out.append(dict(severity="error", category="identifier_load", node=f"script:{s}", label=s,
                            detail=f"about {n:,} names come in through its includes - over the compiler's limit "
                                   f"({IDENTIFIER_LIMIT:,}); expect 'IDENTIFIER LIST FULL'"))
        elif n >= int(IDENTIFIER_LIMIT * 0.75):
            out.append(dict(severity="warning", category="identifier_load", node=f"script:{s}", label=s,
                            detail=f"about {n:,} names come in through its includes (limit {IDENTIFIER_LIMIT:,})"))
    return out


def _strip_comments(src):
    """NWScript source without /* block */ and // line comments (so commented-out declarations are not counted).
    Simple text removal: a "//" inside a string literal also cuts the rest of that line."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def hak_size(db):
    """hak_size issues for every hak the index read: an error at 2**31 bytes (2 GiB) or more, a warning from
    1.8e9 bytes. Only the file size is read (os.path.getsize); a hak that has moved since indexing is skipped."""
    out = []
    for path, kind in db.execute("SELECT path, kind FROM sources WHERE kind='hak'"):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        name = os.path.basename(path)
        if size >= 2 ** 31:
            out.append(dict(severity="error", category="hak_size", node=f"hak:{os.path.splitext(name)[0].lower()}", label=name,
                            detail=f"{name} is {size / 1e9:.2f} GB - over the 2 GB ERF limit; split it"))
        elif size >= 1.8e9:
            out.append(dict(severity="warning", category="hak_size", node=f"hak:{os.path.splitext(name)[0].lower()}", label=name,
                            detail=f"{name} is {size / 1e9:.2f} GB - close to the 2 GB ERF limit"))
    return out


def run_all(db, g, base_names=()):
    """Run every check and return their issue dicts in one list.

    db: the analysis index, sqlite3.Connection. g: the dependency graph (nwn_analysis.Graph). base_names: names
    ("resref.ext") the base game ships, when the NWN install is configured (empty otherwise). Never raises: a check
    that fails becomes one info issue (category lint_failed) and the other checks still run. Read-only."""
    all_names = {f"{r}.{e}" for r, e in db.execute("SELECT resref, ext FROM files")} | set(base_names)
    srcs = {nm: s for nm, s in db.execute("SELECT name, source FROM scripts") if s}
    # scripts the compiler builds on their own: a main() or a StartingConditional() (include files have neither)
    mains = [nm for nm, m, sc in db.execute("SELECT name, has_main, has_sc FROM scripts") if m or sc]
    out = []
    for fn in (lambda: resref_case(db), lambda: portraits(db, all_names), lambda: area_tags(db),
               lambda: include_depth(g, mains), lambda: identifier_load(g, srcs, mains), lambda: hak_size(db)):
        try:
            out += fn()
        except Exception as ex:  # noqa - a lint must never stop the analysis
            out.append(dict(severity="info", category="lint_failed", node="module", label="lint", detail=str(ex)))
    return out
