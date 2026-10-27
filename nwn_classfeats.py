"""
nwn_classfeats.py - checks on the classes' feat tables (cls_feat_*.2da), the lists a character picks feats from when
levelling up.

Why it exists
-------------
Each row of classes.2da names its feat table in the FeatsTable column (cls_feat_fight, cls_feat_wiz...). Every row of
that table gives a feat by its row number in feat.2da (FeatIndex). Builders who add or merge classes and feats by hand
report that bad rows in these tables cause errors during level-up, typically at the class's 3rd level (a tester's
report, 2026-10-27): a row whose FeatIndex is empty (****), a feat number that feat.2da doesn't have or has blanked
out, and a feat with no name in the talk table. A feat that doesn't exist can't be picked, and a character who already
has it doesn't see it in the radial menu or the feat list (the same tester, 2026-10-28). Empty rows are disputed: a
large class pack has shipped them for many years without trouble (another tester), so they are only a note. The
toolset doesn't check any of this.

What it reads / writes
----------------------
Reads, through nwn_minimap.Resources, the copy of classes.2da, each class's feat table and feat.2da that the game uses
(haks in module.ifo order, the module, then the base game; the builder's override folder is left out, as players don't
have it), and the talk tables the analysis used (meta tlk_base / tlk). Writes nothing: run() returns issues for the
analysis to add to the report.

What is checked (only custom content: a table from the module or a hak; Beamdog's own files are not judged)
------------------------------------------------------------------------------------------------------------
  class_feat_table_missing  a custom classes.2da names a feat table no hak, the module or the game has (error)
  class_feat_null           a row of a custom feat table whose FeatIndex is **** (info: disputed, see above)
  class_feat_missing        a row pointing past the end of feat.2da, or at a feat row that is blank (deleted)
                            (warning) - checked in custom feat tables, and in every table when feat.2da is custom
  class_feat_no_name        the feat exists but its name (feat.2da FEAT column) is **** or not in the talk table
                            (warning) - same tables as class_feat_missing; the talk-table part only when the talk
                            table it points into was read

Limits
------
- One issue per table and kind of problem, listing the first rows (row number = position in the file, as the game
  counts them; the label in the first column is ignored, see nwn_index.index_2da).
- The level-up errors are builders' experience, not something the toolkit can prove from the files, so the rows are
  warnings (empty rows only a note). A missing feat table is an error: the class then has no feat list at all.
- While a hak the module lists is missing or the base game wasn't read, a "missing" table or feat may simply be in it:
  those findings become notes that say so (the same rule as the other "missing" checks).
"""
from __future__ import annotations

import nwnlib as n

SHOW = 8          # rows named in one issue's detail; the count says how many there are in all


def _cols(t):
    """{upper-case column name: index} - 2da column names vary in case between tables (FeatIndex, FEATINDEX...)."""
    return {c.upper(): i for i, c in enumerate(t.columns)}


def _cell(t, cols, row, name):
    """The value of column `name` in row `row` (a (label, values) tuple), or None for **** or no such column."""
    i = cols.get(name.upper())
    return row[1][i] if i is not None and i < len(row[1]) else None


def _int(v):
    """int(v) or None (a damaged cell is reported, never raised)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rows_text(rows, total):
    """'row 3 (feat 912), row 7 (feat ****)' for the first SHOW rows, plus how many more."""
    txt = ", ".join(rows[:SHOW])
    return txt + (f" and {total - SHOW} more" if total > SHOW else "")


def check(res, tlks, hint=""):
    """The class feat table findings.

    res: an nwn_minimap.Resources (where() / read() of the winning copy). tlks: nwnlib.TlkSet of the talk tables the
    analysis used (either table may be None). hint: text added to "missing" findings when a listed hak wasn't found
    or the base game wasn't read; when it is not empty those findings are notes (info).
    Returns (issues, summary): issue dicts (severity, category, node, label, detail) and counts for report.json.
    Never raises on odd table content; reads only through res."""
    out = []
    summary = dict(tables=0, checked=0, rows=0)

    def load(name):
        """(TwoDA, where it came from) of the winning copy of name.2da, or (None, None)."""
        data, _e = res.read(name, ("2da",))
        if data is None:
            return None, None
        try:
            t = n.read_2da(n.decode_text(data))
        except ValueError:
            return None, None
        w = res.where(name, ("2da",))
        return t, (w[0] if w else "?")

    classes, cls_src = load("classes")
    if classes is None:
        return out, summary
    feats, feat_src = load("feat")
    feat_custom = feat_src not in (None, "base game")
    ccols = _cols(classes)
    tables = {}                                       # feat table name -> [class labels that use it]
    for i, row in enumerate(classes.rows):
        tbl = _cell(classes, ccols, row, "FeatsTable")
        if tbl:
            tables.setdefault(tbl.lower(), []).append(_cell(classes, ccols, row, "Label") or f"class {i}")
    summary["tables"] = len(tables)
    fcols = _cols(feats) if feats else {}

    for tbl, users in sorted(tables.items()):
        who = ", ".join(users[:3]) + ("..." if len(users) > 3 else "")
        node, label = f"2da:{tbl}", f"{tbl}.2da"
        t, src = load(tbl)
        if t is None:
            if cls_src != "base game":                # only a custom classes.2da is ours to judge
                out.append(dict(severity="info" if hint else "error", category="class_feat_table_missing",
                                node="2da:classes", label="classes.2da",
                                detail=f"classes.2da (from {cls_src}) gives {who} the feat table {tbl}.2da, but no hak, "
                                       "the module or the game has it (or it can't be read): the class has no feat "
                                       "list to pick from when levelling up." + hint))
            continue
        custom = src != "base game"
        if not custom and not feat_custom:
            continue                                  # Beamdog's table against Beamdog's feat.2da: not judged
        summary["checked"] += 1
        tcols = _cols(t)
        nulls, missing, nameless = [], [], []
        for r, row in enumerate(t.rows):
            summary["rows"] += 1
            raw = _cell(t, tcols, row, "FeatIndex")
            if raw is None:
                if custom:
                    nulls.append(f"row {r}")
                continue
            fi = _int(raw)
            if fi is None:
                missing.append(f"row {r} (feat '{raw}' is not a number)")
                continue
            if feats is None:
                continue                              # feat.2da not found anywhere: the missing-table rule covers it
            if fi < 0 or fi >= len(feats.rows):
                missing.append(f"row {r} (feat {fi}: feat.2da has {len(feats.rows)} rows)")
                continue
            frow = feats.rows[fi]
            flabel, fname = _cell(feats, fcols, frow, "LABEL"), _cell(feats, fcols, frow, "FEAT")
            if flabel is None and fname is None:
                missing.append(f"row {r} (feat {fi} is a blank row in feat.2da - deleted)")
                continue
            sref, fl = _int(fname), (f" {flabel}" if flabel else "")
            if sref is None:
                nameless.append(f"row {r} (feat {fi}{fl}: no name)")
            elif tlks is not None and tlks.status(sref) in ("custom-missing", "base-missing"):
                where = "custom talk table" if sref >= n.CUSTOM_TLK_BIT else "dialog.tlk"
                nameless.append(f"row {r} (feat {fi}{fl}: name {sref} not in the {where})")
        tail = f" Class: {who}. Table from {src}" + (f"; feat.2da from {feat_src}." if feats else ".")
        if nulls:
            # a note: builders disagree - some link empty rows to a rare level-up error, while big class packs have
            # shipped them for years without trouble (testers' reports, 2026-10-28)
            out.append(dict(severity="info", category="class_feat_null", node=node, label=label,
                            detail=f"{len(nulls)} row(s) of {tbl}.2da have no feat (FeatIndex ****): "
                                   f"{_rows_text(nulls, len(nulls))}. Empty rows are common in class packs and not "
                                   "known to break anything for certain; some builders link them to a rare level-up "
                                   "error that relogging clears." + tail))
        if missing:
            # with a listed hak missing, the feat.2da read may not be the one players get: a note, saying so
            out.append(dict(severity="info" if hint else "warning", category="class_feat_missing", node=node,
                            label=label,
                            detail=f"{len(missing)} row(s) of {tbl}.2da point at a feat that doesn't exist: "
                                   f"{_rows_text(missing, len(missing))}. The feat can't be picked when levelling up, and a "
                                   "character who already has it doesn't see it in the radial menu or the feat list; "
                                   "builders also report errors during level-up." + tail + hint))
        if nameless:
            out.append(dict(severity="warning", category="class_feat_no_name", node=node, label=label,
                            detail=f"{len(nameless)} row(s) of {tbl}.2da give a feat with no name: "
                                   f"{_rows_text(nameless, len(nameless))}. The feat shows blank in the level-up "
                                   "list, and builders report such rows causing level-up errors." + tail))
    return out, summary


def run(db, meta):
    """check() for an analysis: opens the analysis' Resources and talk tables, returns (issues, summary).
    db: the analysis index (sqlite3 connection); meta: its meta table as a dict. Never raises: a failure becomes
    summary["error"] and no issues."""
    import nwn_minimap
    import nwn_palette
    try:
        res = nwn_minimap.Resources(db, meta.get("nwn_root") or None)
        try:
            tlks, _notes = nwn_palette._talk_tables(meta)    # the same talk tables the palette names come from
            hint = ""
            missing = nwn_minimap._haks_missing(db)
            if missing:
                hint += (f" {len(missing)} hak(s) the module lists were not found ({', '.join(missing[:4])}"
                         f"{'...' if len(missing) > 4 else ''}) - the missing file may be in them.")
            if not res.base_known:
                hint += (" The base game was not read (set the game folder in Settings and analyse again) - the missing "
                         "file may be there.")
            return check(res, tlks if (tlks.base or tlks.custom) else None, hint)
        finally:
            res.close()
    except Exception as ex:  # noqa - a check must never stop the analysis
        return [], dict(error=f"class feat checks failed: {ex}")
