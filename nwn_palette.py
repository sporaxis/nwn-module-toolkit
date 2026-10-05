"""
nwn_palette.py - the toolset's Custom palette, rebuilt from an analysis: every custom blueprint (module and haks),
the palette category it sits in, and every other copy of it that load order hides.

    python nwn_palette.py <analysis folder>      -> palette.json + reports/palette.csv

Called by nwn_analysis after the hak catalogue; the dashboard's Blueprints page shows palette.json (and builds it on
first use for analyses made before this file existed).

How the toolset places a blueprint (checked against a real 7,700-item module)
-----------------------------------------------------------------------------
- Each blueprint has a PaletteID (one byte) - a store's is called ID (BioWare's store blueprint format; seen in a real
  module, where all 109 stores use ID). It is the ID of the palette category the blueprint sits in.
- <type>palcus.itp (e.g. itempalcus.itp, normally in the module) holds the category tree: each node has a STRREF
  (talk-table number; custom tlk when bit 24 is set) or a NAME, the categories that hold blueprints have an ID, and
  LIST holds sub-categories plus the toolset's cached copy of which blueprints sit where (RESREF + NAME).
- <type>pal.itp (base game, or a hak replacing it) is the toolset's skeleton for that tree: the same IDs and an
  English label (DELETE_ME) on every category - used here when there is no palcus file, and for names the talk
  tables can't give.
- The cached listing can go stale (a blueprint moved, renamed or deleted outside the toolset). This module places a
  blueprint by its own PaletteID and reports where the palette file's list disagrees; which of the two the toolset
  reads on opening a module is not confirmed, so a palette move (nwn_build) sets both.

What palette.json holds
-----------------------
    sources     ["module", "<hak name>", ...]; a blueprint's "src" is an index into it
    types       per blueprint type (uti, utc, ...): label, tree_source, notes, categories
                [{name, parent, id, path}], blueprints [{node, resref, name, tag, src, pid, cat, cr?, stale?,
                hidden?}], missing [{resref, cat}] (listed by the palette file, no such blueprint)
    summary     blueprints, unplaced (in no category), stale, hidden (copies load order hides)
"cat" is an index into categories, or null when no category has the blueprint's PaletteID.

Waiting moves (palette_moves.json)
----------------------------------
The Blueprints page can move module blueprints to another existing category. Nothing changes then: the moves wait in
<analysis>/palette_moves.json ({"version": 1, "moves": {ext: {resref: {"to": ID, "from": ID}}}}) until the next
Build & audit applies them (nwn_build) and the audit checks them (nwn_audit). load_moves / set_moves / undo_moves.

Reads and writes
----------------
Reads the analysis index (index.sqlite, opened read-only) and the talk tables the index recorded (meta tlk_base /
tlk). Writes only palette.json and reports/palette.csv (write=False writes nothing) and, when the page moves
blueprints, palette_moves.json - all in the analysis folder; never the module, the haks or the index.

Limits
------
Only the module and its haks count, as in a clean build: the builder's own override folder is not part of the module.
The base game's Standard palette is not listed. Category names need dialog.tlk (the NWN install folder in Settings) or
a hak skeleton carrying English labels; otherwise a category shows as "#<talk-table number>".
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict

import nwn_csvio
import nwn_update
import nwnlib as n

# blueprint type: (palette file stem, page label). The stems match nwn_build.PALETTE_EXT; the order is the page's.
TYPES = {"uti": ("item", "Items"), "utc": ("creature", "Creatures"), "utp": ("placeable", "Placeables"),
         "utd": ("door", "Doors"), "utm": ("store", "Stores"), "utt": ("trigger", "Triggers"),
         "ute": ("encounter", "Encounters"), "uts": ("sound", "Sounds"), "utw": ("waypoint", "Waypoints")}
# the field holding a blueprint's palette number: PaletteID, except in stores (.utm), which call it ID
PALETTE_FIELD = {ext: ("ID" if ext == "utm" else "PaletteID") for ext in TYPES}
SEP = " › "                # between category names in a palette path, as the rest of the dashboard writes paths
VERSION = 2                # of palette.json; the dashboard rebuilds a file with another version (2: base items)


def _source_name(kind, path):
    """How a source is shown: "module", or the hak's file name without .hak."""
    return "module" if kind == "module" else os.path.splitext(os.path.basename(path))[0]


def _struct_tree(rows):
    """Turn one palette file's fields rows [(path, label, value)] into nested nodes.

    The index stores a GFF field's path like "MAIN[0]/LIST[2]/ID": everything before the last "/" is the struct the
    field belongs to. Returns the top-level nodes in file order; each node is {"f": {label: value}, "kids": [...]}.
    Structs that hold no field of their own but have children are created from their children's paths."""
    nodes = {}

    def get(sp):
        if sp not in nodes:
            nodes[sp] = {"f": {}, "kids": []}
        return nodes[sp]
    for path, label, value in rows:
        if "/" not in path:
            continue                       # a field of the file's top struct (none matter here)
        sp = path.rsplit("/", 1)[0]
        get(sp)["f"][label] = value
        while "/" in sp:                   # make sure every ancestor exists, so children can be attached
            sp = sp.rsplit("/", 1)[0]
            get(sp)
    # "MAIN[3]" -> sort key 3; children of P are P/LIST[i]; the order in the file is the order in the toolset
    idx = lambda sp: int(re.search(r"\[(\d+)\]$", sp).group(1))   # noqa: E731
    top = []
    for sp in sorted(nodes, key=lambda s: [int(x) for x in re.findall(r"\[(\d+)\]", s)]):
        parent = sp.rsplit("/", 1)[0] if "/" in sp else None
        if parent is None:
            top.append((idx(sp), nodes[sp]))
        elif parent in nodes:
            nodes[parent]["kids"].append(nodes[sp])
    return [nd for _i, nd in sorted(top, key=lambda t: t[0])]


def _int(v, default=None):
    """int(v), or default when v is missing or not a number (a damaged field never stops the page)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _talk_tables(meta):
    """TlkSet of the talk tables the index used (base dialog.tlk, custom tlk), and notes for any that can't be read."""
    tables, notes = {}, []
    for key, what in (("tlk_base", "base"), ("tlk", "custom")):
        p = meta.get(key) or ""
        if p and os.path.isfile(p):
            try:
                tables[what] = n.Tlk(p)
            except (OSError, ValueError) as ex:
                notes.append(f"talk table {os.path.basename(p)} could not be read ({ex})")
    return n.TlkSet(tables.get("base"), tables.get("custom")), notes


def build_palette(analysis, write=True, verbose=False):
    """Build the palette data for one analysis folder and return it (see the module docstring for its parts).

    analysis: folder holding index.sqlite. write: also write palette.json and reports/palette.csv there (False writes
    nothing). verbose: print a one-line summary. Raises sqlite3.OperationalError when the index can't be opened; never
    writes the module, the haks or the index."""
    db = n.sqlite_ro(os.path.join(analysis, "index.sqlite"))   # read-only: a mistyped folder can't get an empty index
    try:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        # only the module and its haks: the builder's override folder is not part of the module (as in builds)
        srcs = {sid: (kind, prio, path) for sid, path, kind, prio in
                db.execute("SELECT id, path, kind, priority FROM sources") if kind in ("module", "hak")}
        rank = lambda sid: (n.source_rank(srcs[sid][0], srcs[sid][1]), sid)   # noqa: E731 - lower wins
        names, src_ix = [], {}                    # display names, winning source first; "src" indexes names
        for sid in sorted(srcs, key=rank):
            src_ix[sid] = len(names)
            names.append(_source_name(srcs[sid][0], srcs[sid][2]))
        tlks, tlk_notes = _talk_tables(meta)

        # palette files: resref -> [(rank, file id, source index)], best first
        itps = defaultdict(list)
        for fid, sid, resref in db.execute("SELECT id, source_id, resref FROM files WHERE ext='itp'"):
            if sid in srcs:
                itps[(resref or "").lower()].append((rank(sid), fid, src_ix[sid]))
        for lst in itps.values():
            lst.sort()

        def read_rows(fid):
            """One palette file's fields rows (path, label, value), in the order the index stored them."""
            return db.execute("SELECT path, label, value FROM fields WHERE file_id=?", (fid,)).fetchall()

        # every blueprint copy with its palette number (and a creature's challenge rating). path=label keeps only
        # top-level fields: an inventory item inside a creature has its own PaletteID further down.
        extra = defaultdict(dict)
        for fid, label, value in db.execute("SELECT file_id, label, value FROM fields WHERE label IN "
                                            "('PaletteID', 'ID', 'ChallengeRating', 'BaseItem') AND path=label"):
            extra[fid][label] = value
        copies = defaultdict(list)                # (ext, resref) -> [(rank, row)]
        for fid, sid, node, resref, tag, name, ext in db.execute(
                "SELECT o.file_id, f.source_id, o.node, o.resref, o.tag, o.name, f.ext FROM objects o "
                "JOIN files f ON f.id=o.file_id WHERE o.is_blueprint=1"):
            if sid not in srcs or ext not in TYPES:
                continue
            ex = extra.get(fid, {})
            row = dict(node=node, resref=(resref or "").lower(), name=name or "", tag=tag or "", src=src_ix[sid],
                       pid=_int(ex.get(PALETTE_FIELD[ext])))
            if ext == "utc":
                try:
                    row["cr"] = round(float(ex["ChallengeRating"]), 2)
                except (KeyError, TypeError, ValueError):
                    pass                          # no rating stored: the column stays empty
            elif ext == "uti" and _int(ex.get("BaseItem")) is not None:
                row["base"] = _int(ex["BaseItem"])   # row of baseitems.2da (longsword, ring...): a page filter
            copies[(ext, row["resref"])].append((rank(sid), row))
        used_bases = {r["base"] for lst in copies.values() for _rk, r in lst if "base" in r}
        base_names = _base_item_names(db, srcs, rank, meta, tlks, used_bases)

        out = dict(version=VERSION, sources=names, types={}, summary=dict(blueprints=0, unplaced=0, stale=0, hidden=0))
        for ext, (stem, label) in TYPES.items():
            t = _one_type(ext, stem, label, itps, read_rows, copies, tlks, names)
            t["notes"] += tlk_notes
            if ext == "uti":
                t["base_names"] = base_names
            out["types"][ext] = t
            s = out["summary"]
            s["blueprints"] += len(t["blueprints"])
            s["unplaced"] += sum(1 for b in t["blueprints"] if b["cat"] is None)
            s["stale"] += sum(1 for b in t["blueprints"] if b.get("stale"))
            s["hidden"] += sum(len(b.get("hidden", ())) for b in t["blueprints"])
    finally:
        db.close()
    if write:
        _write(analysis, out)
    if verbose:
        s = out["summary"]
        print(f"  palette: {s['blueprints']} blueprints, {s['unplaced']} in no palette category, "
              f"{s['stale']} the palette file has out of date, {s['hidden']} hidden copies", flush=True)
    return out


def _base_item_names(db, srcs, rank, meta, tlks, used):
    """{base item number as text: name} for the base items the blueprints use, from baseitems.2da: the copy the game
    uses among the module and its haks, else the base game's (when the install folder is known). The name is the
    talk-table text of the Name column, else the label column ("longsword"), else "base item N". Read-only; a table
    that can't be read only means plainer names."""
    if not used:
        return {}
    rows = {}
    best = min(((rank(sid), fid) for fid, sid in db.execute(
        "SELECT id, source_id FROM files WHERE resref='baseitems' AND ext='2da'") if sid in srcs), default=None)
    if best:
        for row, col, value in db.execute("SELECT row, col, value FROM twoda WHERE file_id=?", (best[1],)):
            rows.setdefault(str(row), {})[(col or "").lower()] = value
    elif meta.get("nwn_root"):
        try:
            data = n.BaseGame(meta["nwn_root"]).get("baseitems.2da")
            if data:
                t2 = n.read_2da(data.decode("cp1252", errors="replace"))
                cols = [c.lower() for c in t2.columns]
                rows = {str(i): dict(zip(cols, vals)) for i, (_lbl, vals) in enumerate(t2.rows)}
        except (OSError, ValueError):
            pass
    out = {}
    for b in sorted(used):
        r = rows.get(str(b), {})
        strref = _int(r.get("name"))
        txt = tlks.get(strref) if strref is not None else ""
        label = r.get("label") if r.get("label") not in (None, "****") else ""
        out[str(b)] = txt or label or f"base item {b}"
    return out


def _one_type(ext, stem, label, itps, read_rows, copies, tlks, names):
    """The palette data of one blueprint type (see the module docstring).

    ext / stem / label: the type (uti / item / Items). itps: {resref: [(rank, fid, source index)]} best first.
    read_rows(fid): a palette file's fields rows. copies: {(ext, resref): [(rank, row)]}. tlks: TlkSet. names: source
    names. Returns the type's dict; never raises for odd palette content (missing names or IDs are reported)."""
    notes = []
    custom, skeleton = itps.get(stem + "palcus", []), itps.get(stem + "pal", [])
    skel_nodes = _struct_tree(read_rows(skeleton[0][1])) if skeleton else []
    # English labels from the skeleton, by talk-table number: names for categories the talk tables can't give
    skel_label, stack = {}, list(skel_nodes)
    while stack:
        nd = stack.pop()
        s, lab = _int(nd["f"].get("STRREF")), nd["f"].get("DELETE_ME")
        if s is not None and lab:
            skel_label[s] = lab
        stack += nd["kids"]

    if custom:
        tree_nodes, kind = _struct_tree(read_rows(custom[0][1])), "custom"
        tree_source = f"{stem}palcus.itp ({names[custom[0][2]]})"
    elif skeleton:
        tree_nodes, kind = skel_nodes, "skeleton"
        tree_source = f"{stem}pal.itp ({names[skeleton[0][2]]})"
        notes.append(f"The module has no custom palette file ({stem}palcus.itp): the categories come from the "
                     f"toolset's skeleton {stem}pal.itp.")
    else:
        tree_nodes, kind, tree_source = [], "none", ""
        notes.append(f"No palette file for this type ({stem}palcus.itp or {stem}pal.itp) in the module or its haks: "
                     "the blueprints are listed without a category.")

    categories, by_id, listed = [], {}, {}       # listed: resref -> category index where the palette file lists it
    unnamed = [0]

    def name_of(f):
        """Talk table, then NAME, then the skeleton's (or this node's own) English label, then #<number>."""
        s = _int(f.get("STRREF"))
        txt = tlks.get(s) if s is not None else ""
        if txt:
            return txt
        if f.get("NAME"):
            return f["NAME"]
        lab = (skel_label.get(s) if s is not None else None) or f.get("DELETE_ME")
        if lab:
            return lab
        unnamed[0] += 1
        return f"#{s}" if s is not None else "(unnamed)"

    def walk(nodes, parent, ppath):
        for nd in nodes:
            f = nd["f"]
            if "RESREF" in f:                    # the toolset's cached entry for one blueprint
                if parent is not None:
                    listed.setdefault((f["RESREF"] or "").lower(), parent)
                continue
            nm = name_of(f)
            path = ppath + SEP + nm if ppath else nm
            ci, cid = len(categories), _int(f.get("ID"))
            categories.append(dict(name=nm, parent=parent, id=cid, path=path))
            if cid is not None:
                by_id.setdefault(cid, ci)        # IDs are meant to be unique; on a clash the first category wins
            walk(nd["kids"], ci, path)
    walk(tree_nodes, None, "")
    if unnamed[0]:
        notes.append(f"{unnamed[0]} category name(s) could not be read: set the NWN install folder in Settings "
                     "(the names are in the game's dialog.tlk) and re-analyse.")

    blueprints, have = [], set()
    for (e, resref), lst in sorted(copies.items()):
        if e != ext:
            continue
        ranked = [r for _rk, r in sorted(lst, key=lambda x: x[0])]
        win = dict(ranked[0])                    # the copy the game (and the toolset) uses
        have.add(resref)
        win["cat"] = by_id.get(win["pid"])
        # only a custom palette file caches entries; a skeleton lists none, so it can't be out of date
        if kind == "custom" and win["cat"] is not None:
            where = listed.get(resref)
            if where is None:
                win["stale"] = "the palette file doesn't list it"
            elif where != win["cat"]:
                win["stale"] = f"the palette file lists it under {categories[where]['path']}"
        if len(ranked) > 1:
            win["hidden"] = [dict(src=r["src"], name=r["name"], tag=r["tag"], pid=r["pid"], cat=by_id.get(r["pid"]))
                             for r in ranked[1:]]
        blueprints.append(win)
    missing = [dict(resref=r, cat=c) for r, c in sorted(listed.items()) if r not in have]
    return dict(label=label, tree_source=tree_source, notes=notes, categories=categories, blueprints=blueprints,
                missing=missing)


# move_to is left empty: fill it in a spreadsheet and import the file on the Blueprints page (plan_import)
CSV_COLUMNS = ["type", "palette", "name", "tag", "resref", "source", "palette_number", "cr", "note", "move_to"]


def csv_rows(pal):
    """One dict per blueprint copy (CSV_COLUMNS): the winning copy, then each copy it hides (note "hidden by X").
    palette is the category path, or "(not in any palette category)"."""
    out = []
    for ext, t in pal["types"].items():
        cats, src = t["categories"], pal["sources"]
        where = lambda c: cats[c]["path"] if c is not None else "(not in any palette category)"   # noqa: E731
        for b in t["blueprints"]:
            base = dict(type=t["label"], resref=b["resref"], cr="" if b.get("cr") is None else b["cr"])
            out.append(dict(base, palette=where(b["cat"]), name=b["name"], tag=b["tag"], source=src[b["src"]],
                            palette_number="" if b["pid"] is None else b["pid"], note=b.get("stale", ""), move_to=""))
            for x in b.get("hidden", ()):
                out.append(dict(base, palette=where(x["cat"]), name=x["name"], tag=x["tag"], source=src[x["src"]],
                                palette_number="" if x["pid"] is None else x["pid"],
                                note=f"hidden by {src[b['src']]} (the game uses that copy)", move_to=""))
    return out


# a CSV value a spreadsheet can't run as a formula (blueprint names come from haks anyone can make): nwn_csvio.cell
csv_cell = nwn_csvio.cell


def _write(analysis, pal):
    """Write palette.json (through a temporary file, so a reader never sees half a file) and reports/palette.csv."""
    tmp = os.path.join(analysis, "palette.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pal, fh, ensure_ascii=False)
    os.replace(tmp, os.path.join(analysis, "palette.json"))
    os.makedirs(os.path.join(analysis, "reports"), exist_ok=True)
    with open(os.path.join(analysis, "reports", "palette.csv"), "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        wr.writeheader()
        wr.writerows({k: csv_cell(v) for k, v in r.items()} for r in csv_rows(pal))


MOVES_FILE = "palette_moves.json"


def load_moves(analysis):
    """The waiting moves of an analysis: {ext: {resref: {"to": ID, "from": ID or None}}}; {} when there are none or
    the file can't be read (a damaged file never stops the page or the build - it is rewritten on the next change)."""
    try:
        with open(os.path.join(analysis, MOVES_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        mv = data.get("moves") if isinstance(data, dict) else None
        return {e: dict(v) for e, v in mv.items() if v} if isinstance(mv, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_moves(analysis, moves):
    """Write palette_moves.json through a temporary file (a reader never sees half a file). Empty types are dropped."""
    nwn_update.claim_work(analysis)      # refuses if a newer toolkit saved work here; else stamps this version
    moves = {e: v for e, v in moves.items() if v}
    tmp = os.path.join(analysis, MOVES_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(version=1, moves=moves), fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(analysis, MOVES_FILE))
    return moves


def set_moves(analysis, ext, resrefs, to_id, pal):
    """Make blueprints of type `ext` wait to move to the category numbered `to_id` (applied by the next build).

    pal: this analysis' palette data (build_palette / the dashboard's palette_data). A blueprint is refused (listed in
    the result, nothing stored for it) when no module blueprint of that type has the resref, or when its copy in a
    hak wins (the hak copy is what the game loads, so a module change would not show). Moving a blueprint to the
    category it already sits in cancels its waiting move. Raises ValueError, writing nothing, for an unknown type or
    a number that no category of that type has. Returns dict(moves=all waiting moves, refused=[{resref, why}]).
    Writes only palette_moves.json in the analysis folder."""
    if ext not in TYPES:
        raise ValueError(f"unknown blueprint type '{ext}'")
    t = pal["types"][ext]
    if isinstance(to_id, bool) or not isinstance(to_id, int) or to_id not in {c["id"] for c in t["categories"]}:
        raise ValueError(f"no {t['label'].lower()} palette category has the number {to_id!r} - pick one from the list")
    moves = load_moves(analysis)
    mine = moves.setdefault(ext, {})
    refused = []
    for rr in resrefs:
        rr = str(rr).lower()
        b, why = _blocked(pal, ext, rr)
        if why:
            refused.append(dict(resref=rr, why=why))
        elif b["pid"] == to_id:
            mine.pop(rr, None)                 # back where it started: nothing to move
        else:
            mine[rr] = {"to": to_id, "from": b["pid"]}
    return dict(moves=_save_moves(analysis, moves), refused=refused)


def _blocked(pal, ext, rr):
    """(blueprint, None) when blueprint rr of type ext may move, else (None, why). Shared by set_moves and the
    spreadsheet import, so both refuse the same things for the same reasons."""
    t = pal["types"][ext]
    b = next((x for x in t["blueprints"] if x["resref"] == rr), None)
    if b is None:
        kind = t["label"].lower()[:-1]          # "Items" -> "item"
        return None, f"no {kind} blueprint '{rr}' in the module or its haks"
    if pal["sources"][b["src"]] != "module":
        return None, (f"it lives in hak {pal['sources'][b['src']]}, and the hak copy is the one the game loads - "
                      "change it in the hak instead")
    return b, None


def _path_key(text):
    """A category path compared loosely: split at ›, > or /, each part trimmed and case-folded."""
    return tuple(p.strip().casefold() for p in re.split(r"[›>/]", text or "") if p.strip())


def _target(t, text):
    """(category number, None) for a move_to cell, else (None, why). The cell is a number ("#24" or "24") or a
    category path in any of the separators _path_key allows. Only categories with a number can hold blueprints; a
    path two such categories share must be given by number."""
    m = re.fullmatch(r"#?\s*(\d+)", text.strip())
    ids = {c["id"] for c in t["categories"] if c["id"] is not None}
    if m:
        cid = int(m.group(1))
        return (cid, None) if cid in ids else (None, f"no {t['label'].lower()} category has the number {cid}")
    hits = [c for c in t["categories"] if _path_key(c["path"]) == _path_key(text)]
    if not hits:
        return None, f"no category '{text.strip()}' in the {t['label'].lower()} palette - copy a path from the export"
    holders = [c for c in hits if c["id"] is not None]
    if not holders:
        return None, f"'{text.strip()}' can't hold blueprints - pick one of its sub-categories"
    if len(holders) > 1:
        return None, (f"{len(holders)} categories have the path '{text.strip()}' - use the number instead ("
                      + ", ".join(f"#{c['id']}" for c in holders) + ")")
    return holders[0]["id"], None


def _ext_of(value):
    """Blueprint type from a type cell: "Items", "item" or "uti" (any case) -> "uti"; None when unknown."""
    v = (value or "").strip().lower()
    for ext, (_stem, label) in TYPES.items():
        if v in (ext, label.lower(), label.lower()[:-1]):
            return ext
    return None


def plan_import(pal, moves, data):
    """What importing a spreadsheet of moves would do - a preview; writes nothing.

    pal: the palette data; moves: the waiting moves (load_moves); data: the CSV file's bytes (nwn_csvio.read_upload:
    UTF-8 or Windows-1252, comma / semicolon / tab). Only the type, resref and move_to columns are read. Every row
    lands in exactly one of: moves, cancels (move_to is where the blueprint sits and a move was waiting), unchanged
    (empty move_to, or already there), refused (with its spreadsheet row number and why), duplicates (a repeat of a
    row for the same blueprint with the same move_to). A blueprint with rows that disagree, or with one refused row,
    is refused on every row: nothing is guessed. Returns dict(rows, moves, cancels, unchanged, refused, duplicates).
    Raises ValueError for a file that can't be read, lacks type / resref, or has neither move_to nor a new_... column
    (the field changes those columns hold are nwn_bpchanges.plan_csv's)."""
    rows = nwn_csvio.read_upload(data, required=("type", "resref"))
    if rows and "move_to" not in rows[0] and not any(k.startswith("new_") for k in rows[0]):
        raise ValueError("the file has no move_to column and no new_... column - export the CSV from the Blueprints "
                         "page and fill one of those")
    unchanged, refused, per = 0, [], {}
    for r in rows:
        mt = nwn_csvio.uncell(r["move_to"])
        if not mt:
            unchanged += 1
            continue
        rr, typ = nwn_csvio.uncell(r["resref"]).lower(), nwn_csvio.uncell(r["type"])
        ext = _ext_of(typ)
        if ext is None:
            refused.append(dict(row=r["_row"], resref=rr, why=f"unknown type '{typ}' - use the type names of the "
                                                               "export (Items, Creatures, Placeables...)"))
            continue
        b, why = _blocked(pal, ext, rr)
        to = None
        if not why:
            to, why = _target(pal["types"][ext], mt)
        per.setdefault((ext, rr), []).append((r["_row"], to, why, b))
    out = dict(rows=len(rows), moves=[], cancels=[], unchanged=0, refused=refused, duplicates=0)
    for (ext, rr), lst in per.items():
        t, rows_ = pal["types"][ext], [x[0] for x in lst]
        bad = [x for x in lst if x[2]]
        if bad or len({x[1] for x in lst}) > 1:
            where = ", ".join(str(x) for x in rows_)
            for row, _to, why, _b in lst:
                refused.append(dict(row=row, resref=rr, why=why or (
                    f"different move_to values for this blueprint in rows {where}" if not bad else
                    f"another row for this blueprint was refused (rows {where})")))
            continue
        out["duplicates"] += len(lst) - 1
        row, to, _why, b = lst[0]
        waiting = (moves.get(ext) or {}).get(rr)
        path = lambda cid: next((c["path"] for c in t["categories"] if c["id"] == cid), f"#{cid}")   # noqa: E731
        item = dict(ext=ext, type=t["label"], resref=rr, name=b["name"], row=row, to=to, to_path=path(to),
                    **{"from": b["pid"], "from_path": t["categories"][b["cat"]]["path"] if b["cat"] is not None else
                       f"palette number {b['pid']}"})
        if to == b["pid"]:
            if waiting:
                out["cancels"].append(item)
            else:
                unchanged += 1
        else:
            out["moves"].append(item)
    out["unchanged"] = unchanged
    out["refused"].sort(key=lambda x: x["row"])
    for k in ("moves", "cancels"):
        out[k].sort(key=lambda x: x["row"])
    return out


def apply_import(analysis, pal, data):
    """Import a spreadsheet of moves: work out plan_import again (the file itself, never a plan the page sent) and
    store exactly its moves and cancels. Returns the plan plus moves=all waiting moves. Writes only
    palette_moves.json; raises ValueError (nothing written) when the file can't be read."""
    moves = load_moves(analysis)
    plan = plan_import(pal, moves, data)
    for m in plan["moves"]:
        moves.setdefault(m["ext"], {})[m["resref"]] = {"to": m["to"], "from": m["from"]}
    for c in plan["cancels"]:
        (moves.get(c["ext"]) or {}).pop(c["resref"], None)
    return dict(plan, moves=_save_moves(analysis, moves))


def undo_moves(analysis, ext=None, resrefs=None):
    """Cancel waiting moves: the given resrefs of type ext, every move of type ext (resrefs None), or all (ext None).
    Returns the moves still waiting. Writes only palette_moves.json."""
    moves = load_moves(analysis)
    if ext is None:
        moves = {}
    elif resrefs is None:
        moves.pop(ext, None)
    else:
        for rr in resrefs:
            moves.get(ext, {}).pop(str(rr).lower(), None)
    return _save_moves(analysis, moves)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip())
        sys.exit(2)
    build_palette(sys.argv[1], verbose=True)
