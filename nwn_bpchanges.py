"""
nwn_bpchanges.py - bulk blueprint changes: faction, name, tag and description, waiting for the next build.

Why this exists
---------------
Re-factioning a hundred creatures or renaming a family of items one by one in the toolset is slow. On the Blueprints
page the builder ticks blueprints (or fills new_* columns in a spreadsheet) and the changes wait in
<analysis>/blueprint_changes.json:
    {"version": 1, "changes": {ext: {resref: {field: {"to": value, "from": value}}}}}
The next Build & audit applies them (nwn_build) to the blueprint and to every placed copy in the areas that still holds
the blueprint's old value - placed objects are full copies, so a copy the builder gave its own name keeps it - and the
audit proves that only those fields changed (nwn_audit). Nothing changes before the build.

What can change (FIELDS), and what is refused (plan_values / plan_csv)
--------------------------------------------------------------------
    name / first_name / last_name / description / unidentified_description   the localised texts (a new text
        replaces all languages and any talk-table reference)
    tag      at most 32 characters. Refused when scripts, conversations or fields look the blueprint's tag up, or an
             item's tag-based script is named after it (renaming would break them - Find & replace renames those
             everywhere instead), and when scripts already look the NEW tag up (they would find this object too).
    faction  creatures only; a faction of the module's repute.fac, by name (any case) or number.
Also refused: blueprints whose copy in a hak wins (the game loads the hak's), unknown blueprints / types / fields, and
spreadsheet rows that disagree about one blueprint's field (nothing is guessed).

Reads the analysis (index.sqlite read-only, report.json, the palette data for which copy wins). Writes only
blueprint_changes.json.
"""
from __future__ import annotations

import json
import os
import re

import nwn_csvio
import nwn_update
import nwnlib as n

# field -> GFF label, per blueprint type. Labels per BioWare's blueprint formats: items name themselves in
# LocalizedName and keep two descriptions (Description = unidentified, DescIdentified = identified); creatures have
# FirstName / LastName and FactionID; placeables, doors, stores and sounds use LocName; triggers, encounters and
# waypoints LocalizedName.
FIELDS = {
    "uti": {"name": "LocalizedName", "description": "DescIdentified", "unidentified_description": "Description",
            "tag": "Tag"},
    "utc": {"first_name": "FirstName", "last_name": "LastName", "description": "Description", "tag": "Tag",
            "faction": "FactionID"},
    "utp": {"name": "LocName", "description": "Description", "tag": "Tag"},
    "utd": {"name": "LocName", "description": "Description", "tag": "Tag"},
    "utm": {"name": "LocName", "tag": "Tag"},
    "uts": {"name": "LocName", "tag": "Tag"},
    "utt": {"name": "LocalizedName", "tag": "Tag"},
    "ute": {"name": "LocalizedName", "tag": "Tag"},
    "utw": {"name": "LocalizedName", "description": "Description", "tag": "Tag"},
}
TEXT_FIELDS = {"name", "first_name", "last_name", "description", "unidentified_description"}
ALL_FIELDS = ["name", "first_name", "last_name", "tag", "description", "unidentified_description", "faction"]
TAG_MAX = 32                     # the toolset's limit for a tag
CHANGES_FILE = "blueprint_changes.json"
CLASS = {"uti": "item", "utc": "creature", "utp": "placeable", "utd": "door", "ute": "encounter", "utm": "store",
         "uts": "sound", "utt": "trigger", "utw": "waypoint"}       # objects.class of each blueprint type


def load_context(analysis, pal=None):
    """What planning needs, read once: the blueprints (which copy wins, current field values, placed copies), the
    tags something looks up, and the factions. pal: the palette data if the caller has it (else built, not saved).
    Returns dict(pal, values {(ext, resref): {field: text}}, placed {(ext, resref): count}, used_tags {tag: why},
    factions {number: name}). Read-only."""
    import nwn_palette
    pal = pal or nwn_palette.build_palette(analysis, write=False)
    db = n.sqlite_ro(os.path.join(analysis, "index.sqlite"))
    try:
        labels = {lab for f in FIELDS.values() for lab in f.values()}
        values = {}
        q = (f"SELECT f.resref, f.ext, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id JOIN sources s "
             f"ON s.id=f.source_id WHERE s.kind='module' AND fi.path=fi.label AND fi.label IN "
             f"({','.join('?' * len(labels))}) AND f.ext IN ({','.join('?' * len(FIELDS))})")
        for rr, ext, lab, val in db.execute(q, (*labels, *FIELDS)):
            for field, l2 in FIELDS[ext].items():
                if l2 == lab:
                    values.setdefault((ext, (rr or "").lower()), {})[field] = "" if val is None else str(val)
        placed = {}
        cls_ext = {c: e for e, c in CLASS.items()}
        for tpl, cls, cnt in db.execute("SELECT lower(template), class, count(*) FROM objects WHERE is_blueprint=0 "
                                        "GROUP BY lower(template), class"):
            if cls in cls_ext:
                placed[(cls_ext[cls], tpl or "")] = cnt
        # tags something looks up: any edge into tag:<T> except the object -> tag link itself
        used = {}
        for dst, kind, src in db.execute("SELECT dst, kind, src FROM edges WHERE dst LIKE 'tag:%' AND kind <> 'tag_provider'"):
            used.setdefault(dst[4:], f"{src} ({kind.replace('_', ' ')})")
        # an item whose tag names a script (EE tag-based scripting): renaming the tag disconnects the script
        for src, dst in db.execute("SELECT src, dst FROM edges WHERE kind='tag_based_script'"):
            row = db.execute("SELECT tag FROM objects WHERE node=?", (src,)).fetchone()
            if row and row[0]:
                used.setdefault(row[0], f"{dst} (tag-based script)")
    finally:
        db.close()
    rep = {}
    try:
        with open(os.path.join(analysis, "report.json"), encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError):
        pass
    factions = {f["id"]: f["name"] for f in ((rep.get("factions") or {}).get("factions") or [])}
    return dict(pal=pal, values=values, placed=placed, used_tags=used, factions=factions)


def _faction(ctx, text):
    """(number, None) for a faction given by name (any case) or number; (None, why) otherwise."""
    t = str(text).strip()
    if re.fullmatch(r"#?\d+", t) and int(t.lstrip("#")) in ctx["factions"]:
        return int(t.lstrip("#")), None
    hit = [i for i, nm in ctx["factions"].items() if nm.strip().lower() == t.lower()]
    if len(hit) == 1:
        return hit[0], None
    if len(hit) > 1:
        return None, f"{len(hit)} factions are called '{t}' - use the number"
    return None, f"no faction '{t}' in the module (its factions: {', '.join(ctx['factions'].values()) or 'none'})"


def _check(ctx, ext, rr, field, text):
    """One wanted change -> (change dict, None), ("same", None) when it is already so, or (None, why)."""
    import nwn_palette
    if ext not in FIELDS:
        return None, f"unknown blueprint type '{ext}'"
    if field not in FIELDS[ext]:
        return None, (f"a {CLASS[ext]} has no field '{field}' that can be changed here "
                      f"({', '.join(FIELDS[ext])})")
    b, why = nwn_palette._blocked(ctx["pal"], ext, rr)
    if why:
        return None, why
    cur = ctx["values"].get((ext, rr), {}).get(field, "")
    to, note = str(text).strip(), ""
    if field == "faction":
        to, why = _faction(ctx, to)
        if why:
            return None, why
        cur = int(cur) if str(cur).isdigit() else cur
    elif field == "tag":
        if len(to) > TAG_MAX:
            return None, f"a tag has at most {TAG_MAX} characters ('{to[:40]}…' has {len(to)})"
        if not to:
            return None, "a blank tag - scripts could not find this object"
        if to != cur and cur in ctx["used_tags"]:
            return None, (f"its tag '{cur}' is looked up by {ctx['used_tags'][cur]} - rename it with Find & replace, "
                          "which changes those too")
        if to != cur and to in ctx["used_tags"]:
            return None, (f"scripts already look up the tag '{to}' ({ctx['used_tags'][to]}) - they would find this "
                          "object too")
    if to == cur:
        return "same", None
    t = ctx["pal"]["types"][ext]
    return dict(ext=ext, type=t["label"], resref=rr, name=b["name"], field=field, label=FIELDS[ext][field],
                **{"from": cur}, to=to, placed=ctx["placed"].get((ext, rr), 0), note=note), None


def plan_values(ctx, ext, resrefs, values):
    """The Change fields… dialog: the same new values for every ticked blueprint of one type.

    values: {field: text}; an empty text leaves that field alone. Returns dict(changes, refused [{item "ext:resref",
    why}], unchanged) - a preview; nothing is stored (save_plan does that)."""
    out = dict(changes=[], refused=[], unchanged=0)
    vals = {f: v for f, v in (values or {}).items() if str(v if v is not None else "").strip()}
    for rr in resrefs:
        rr = str(rr).lower()
        for field, text in vals.items():
            c, why = _check(ctx, ext, rr, field, text)
            if why:
                if not any(r["item"] == f"{ext}:{rr}" and r["why"] == why for r in out["refused"]):
                    out["refused"].append(dict(item=f"{ext}:{rr}", why=why))
            elif c == "same":
                out["unchanged"] += 1
            else:
                out["changes"].append(c)
    return out


def plan_csv(ctx, data):
    """The Blueprints page's Import…: the new_<field> columns of a spreadsheet (type and resref say which blueprint).

    data: CSV bytes (nwn_csvio.read_upload). Empty cells are left alone. Rows that disagree about one blueprint's field
    refuse that field on all of them. Returns dict(rows, changes [.. row], refused [{row, item, why}], unchanged,
    duplicates); {} changes when the file has no new_* column. Raises ValueError for an unreadable file."""
    import nwn_palette
    rows = nwn_csvio.read_upload(data, required=("type", "resref"))
    cols = [f for f in ALL_FIELDS if rows and f"new_{f}" in rows[0]]
    out = dict(rows=len(rows), changes=[], refused=[], unchanged=0, duplicates=0)
    per = {}
    for r in rows:
        ext = nwn_palette._ext_of(nwn_csvio.uncell(r["type"]))
        rr = nwn_csvio.uncell(r["resref"]).lower()
        for f in cols:
            v = nwn_csvio.uncell(r.get(f"new_{f}", ""))
            if not v:
                continue
            if ext is None:
                out["refused"].append(dict(row=r["_row"], item=rr, why=f"unknown type '{nwn_csvio.uncell(r['type'])}'"))
                break
            per.setdefault((ext, rr, f), []).append((r["_row"], v))
    for (ext, rr, f), lst in per.items():
        if len({v for _r, v in lst}) > 1:
            where = ", ".join(str(r_) for r_, _v in lst)
            out["refused"] += [dict(row=r_, item=f"{ext}:{rr}", why=f"rows {where} give different new_{f} values")
                               for r_, _v in lst]
            continue
        out["duplicates"] += len(lst) - 1
        c, why = _check(ctx, ext, rr, f, lst[0][1])
        if why:
            out["refused"].append(dict(row=lst[0][0], item=f"{ext}:{rr}", why=why))
        elif c == "same":
            out["unchanged"] += 1
        else:
            out["changes"].append(dict(c, row=lst[0][0]))
    out["refused"].sort(key=lambda x: x["row"])
    out["changes"].sort(key=lambda x: x["row"])
    return out


def load_changes(analysis):
    """The waiting changes {ext: {resref: {field: {"to", "from"}}}}; {} when none or the file can't be read."""
    try:
        with open(os.path.join(analysis, CHANGES_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        ch = data.get("changes") if isinstance(data, dict) else None
        return {e: v for e, v in ch.items() if v} if isinstance(ch, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(analysis, changes):
    """Write blueprint_changes.json through a temporary file; empty blueprints and types are dropped."""
    nwn_update.claim_work(analysis)      # refuses if a newer toolkit saved work here; else stamps this version
    changes = {e: {r: f for r, f in v.items() if f} for e, v in changes.items()}
    changes = {e: v for e, v in changes.items() if v}
    tmp = os.path.join(analysis, CHANGES_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(version=1, changes=changes), fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(analysis, CHANGES_FILE))
    return changes


def save_plan(analysis, plan):
    """Add a plan's changes to the waiting ones (a later value for the same field replaces the earlier). Returns all
    waiting changes. Writes only blueprint_changes.json."""
    ch = load_changes(analysis)
    for c in plan["changes"]:
        ch.setdefault(c["ext"], {}).setdefault(c["resref"], {})[c["field"]] = {"to": c["to"], "from": c["from"]}
    return _save(analysis, ch)


def undo_changes(analysis, ext=None, resrefs=None, field=None):
    """Cancel waiting changes: all (ext None), one type's, some blueprints' (resrefs), or one field of them. Returns
    the changes still waiting. Writes only blueprint_changes.json."""
    ch = load_changes(analysis)
    if ext is None:
        ch = {}
    elif resrefs is None:
        ch.pop(ext, None)
    else:
        for rr in resrefs:
            bp = ch.get(ext, {}).get(str(rr).lower())
            if bp is None:
                continue
            if field:
                bp.pop(field, None)
            else:
                ch[ext].pop(str(rr).lower(), None)
    return _save(analysis, ch)


def new_value(field, to):
    """The GFF value a change writes: a localised text (English, no talk-table reference) for text fields, the text
    for a tag, the number for a faction."""
    if field in TEXT_FIELDS:
        return n.LocString(strref=n.BAD_STRREF, entries={0: str(to)})
    return int(to) if field == "faction" else str(to)
