"""
nwn_catalog.py - the hak/content catalogue: what the module's haks (and the module itself) provide,
which of it the module actually uses, and what wins when several sources define the same thing.

Used two ways:
  * cleanup - "which hak content is carried but never used"
  * build-from-spec - new content may only reference rows/resrefs in this catalogue

    python nwn_catalog.py <analysis folder>      -> catalog.json + reports/hak_catalogue.csv

Called by nwn_analysis after the index is built; the dashboard's Hak catalogue page shows catalog.json.

What catalog.json holds
-----------------------
    haks          per hak / override folder: every file it carries, its size, whether a module file references
                  it, and whether this copy is the one the game uses ("wins")
    shadowed      files hidden by a higher-precedence copy of the same name in another source
    tables        rows of well-known 2da tables (TABLES) and which blueprints/areas use each row
    tilesets      .set files and the areas built on them
    tlk           custom talk table: entries provided, used, unused, and broken references
    twoda_conflicts  the same 2da in several sources (rows lost or different), or older than the installed game
    heads         head models that creatures ask for, and whether they exist
    summary       the counts shown at the top of the page

Reads and writes
----------------
Reads the analysis index (index.sqlite, opened read-only), the 2da files again from the module / hak /
override sources (to compare whole tables), the custom .tlk, and the base game's archives when the install folder
is known. Writes only catalog.json and reports/hak_catalogue.csv in the analysis folder; never the module or haks.

Limits
------
"Used" means referenced directly by a file that comes from the module (one step in the graph), so a hak file used
only by another hak file (a texture of a hak model) shows as not used. 2da rows count as used only through the
blueprint/area fields listed in TABLES, not through scripts. Precedence follows the game (nwnlib.source_rank): haks
(module.ifo order, first listed wins) > module > user override > base game; a 2da's rows are the winning copy's only.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict

# 2da table -> (label column candidates, how blueprints refer to a row: (file ext, field label))
# The field's value is the row number, e.g. a creature's Appearance_Type 6 = row 6 of appearance.2da.
# A name ending in "*" matches every table with that prefix (iprp_*: the item property tables).
TABLES = {
    "appearance":  (["LABEL", "STRING_REF"], [("utc", "Appearance_Type")]),
    "placeables":  (["Label"], [("utp", "Appearance")]),
    "baseitems":   (["label"], [("uti", "BaseItem")]),
    "portraits":   (["BaseResRef"], [("utc", "PortraitId"), ("utp", "PortraitId"), ("utd", "PortraitId")]),
    "soundset":    (["LABEL", "RESREF"], [("utc", "SoundSetFile")]),
    "classes":     (["Label"], [("utc", "Class")]),
    "racialtypes": (["Label"], [("utc", "Race")]),
    "feat":        (["LABEL"], [("utc", "Feat")]),
    "spells":      (["Label"], [("utc", "Spell"), ("uti", "Subtype")]),
    "skills":      (["Label"], [("utc", "Skill")]),
    "genericdoors": (["Label"], [("utd", "GenericType")]),
    "doortypes":   (["Label"], [("utd", "Appearance")]),
    "tailmodel":   (["LABEL"], [("utc", "Tail_New")]),
    "wingmodel":   (["LABEL"], [("utc", "Wings_New")]),
    "loadscreens": (["Label", "BMPResRef"], [("are", "LoadScreenID")]),
    "ambientmusic": (["Description", "Resource"], [("are", "MusicDay"), ("are", "MusicNight"), ("are", "MusicBattle")]),
    "ambientsound": (["Label", "Resource"], [("are", "AmbientSndDay"), ("are", "AmbientSndNight")]),
    "creaturespeed": (["Label"], [("utc", "WalkRate")]),
    "phenotype":   (["Label"], [("utc", "Phenotype")]),
    "iprp_*":      (["Label"], []),
}
# file extension -> the category it is listed under in a hak's inventory (anything else: its extension)
ASSET_KINDS = {"mdl": "models", "tga": "textures", "dds": "textures", "plt": "textures", "wav": "sounds", "bmu": "music",
               "set": "tilesets", "ssf": "soundsets", "2da": "2da tables", "nss": "scripts", "ncs": "scripts",
               "uti": "item blueprints", "utc": "creature blueprints", "utp": "placeable blueprints",
               "utd": "door blueprints", "dlg": "conversations", "tlk": "talk table", "txi": "texture info",
               "wok": "walkmeshes", "pwk": "walkmeshes", "dwk": "walkmeshes", "ltr": "name generators",
               "mtr": "materials", "shd": "shaders", "itp": "palettes", "are": "areas"}


def build_catalog(analysis, verbose=True):
    """Build the catalogue for one analysis folder, write catalog.json and reports/hak_catalogue.csv, and return
    the catalogue dict (see the module docstring for its parts). analysis: folder holding index.sqlite.
    verbose: print a one-line summary. Never writes the module, haks or index."""
    import nwnlib as n
    db = n.sqlite_ro(os.path.join(analysis, "index.sqlite"))   # read-only: a mistyped folder can't get an empty index
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    sources = {sid: dict(path=p, kind=k, priority=pr) for sid, p, k, pr in db.execute("SELECT * FROM sources")}
    files = db.execute("SELECT f.id, f.source_id, f.resref, f.ext, f.size, f.node FROM files f").fetchall()

    # ---- precedence: haks (module.ifo order: first listed wins) > module > override > base game
    def rank(sid):
        """Lower wins: nwnlib.source_rank of the source's kind and position."""
        return n.source_rank(sources[sid]["kind"], sources[sid]["priority"])
    by_res = defaultdict(list)
    for fid, sid, resref, ext, size, node in files:
        by_res[f"{resref}.{ext}"].append((rank(sid), sid, fid, size, node))
    winners = {}
    shadowed = []
    for res, lst in by_res.items():
        lst.sort()                 # by rank, then source id: the first entry is the copy the game uses
        winners[res] = lst[0]
        for r_, sid, fid, size, node in lst[1:]:
            shadowed.append(dict(resource=res, loser=os.path.basename(sources[sid]["path"]),
                                 winner=os.path.basename(sources[lst[0][1]]["path"]), loser_kind=sources[sid]["kind"]))

    # ---- which files does the module reach (graph edges from module-sourced nodes)
    # One step only: dst is "referenced" when a node that has a file in the module source points at it.
    # tag_provider edges are left out: they point from a tag to its holder, not from a user to what it uses.
    module_nodes = {node for fid, sid, resref, ext, size, node in files if sources[sid]["kind"] == "module"}
    referenced = set()
    for src, dst in db.execute("SELECT src, dst FROM edges WHERE kind != 'tag_provider'"):
        if src in module_nodes or src == "module":
            referenced.add(dst)
    # 2da tables are read by the engine, count them as used when any row is used (below)

    # ---- per-hak inventory
    haks = {}
    for sid, s in sources.items():
        if s["kind"] not in ("hak", "override"):
            continue
        inv = defaultdict(list)
        used = 0
        for fid, sid2, resref, ext, size, node in files:
            if sid2 != sid:
                continue
            kind = ASSET_KINDS.get(ext, ext)
            is_used = node in referenced
            inv[kind].append(dict(name=f"{resref}.{ext}", size=size, used=is_used, node=node,
                                  wins=winners[f"{resref}.{ext}"][1] == sid))
            used += is_used
        total = sum(len(v) for v in inv.values())
        haks[os.path.basename(s["path"])] = dict(path=s["path"], kind=s["kind"], files=total,
                                                 bytes=sum(x["size"] or 0 for v in inv.values() for x in v),
                                                 used_files=used, inventory={k: sorted(v, key=lambda x: x["name"]) for k, v in inv.items()})

    # ---- 2da rows: provided vs used by blueprints
    twoda_src = {}
    for name, sid in db.execute("SELECT t.name, f.source_id FROM twoda t JOIN files f ON f.id=t.file_id GROUP BY t.name, f.source_id"):
        twoda_src.setdefault(name, []).append(sid)
    # the game loads one copy of a table whole - the winning source's - and never mixes in rows or cells of the
    # copies it hides, so only that copy's rows are "provided". The losing copies' differences are reported by
    # twoda_conflicts below. table -> row -> {column: value, "__source": source id}
    table_winner = {name: min(sids, key=lambda x: (rank(x), x)) for name, sids in twoda_src.items()}
    rows_by_table = defaultdict(dict)
    for name, row, col, value, sid in db.execute(
            "SELECT t.name, t.row, t.col, t.value, f.source_id FROM twoda t JOIN files f ON f.id=t.file_id"):
        if sid == table_winner[name]:
            rows_by_table[name].setdefault(row, {"__source": sid})[col] = value
    field_use = defaultdict(lambda: defaultdict(set))   # (ext,label) -> value -> {file relpath}
    for ext, label, value, relpath in db.execute(
            "SELECT f.ext, fi.label, fi.value, f.relpath FROM fields fi JOIN files f ON f.id=fi.file_id "
            "WHERE fi.label IN ('Appearance_Type','Appearance','BaseItem','PortraitId','SoundSetFile','Class','Race',"
            "'Feat','Spell','Skill','GenericType','Tail_New','Wings_New','LoadScreenID','MusicDay','MusicNight',"
            "'MusicBattle','AmbientSndDay','AmbientSndNight','WalkRate','Phenotype','Subtype','Portrait')"):
        field_use[(ext, label)][str(value)].add(relpath)
    # Portrait fields name a portrait by resref ("po_hu_m_01_"); portraits.2da rows carry the name without the
    # po_ prefix (BaseResRef "hu_m_01_"), so the files are keyed by the stripped, lower-cased name
    portrait_users = defaultdict(set)
    for (ext, fl), m in field_use.items():
        if fl == "Portrait":
            for v, rps in m.items():
                v = v.lower()
                portrait_users[v[3:] if v.startswith("po_") else v] |= rps
    tables = {}
    for tname, (label_cols, refs) in TABLES.items():
        names = [t for t in rows_by_table if (t == tname or (tname.endswith("*") and t.startswith(tname[:-1])))]
        for name in names:
            rows = rows_by_table[name]
            src_kinds = {sources[s]["kind"] for s in twoda_src.get(name, [])}
            out_rows = []
            used_count = 0
            # rows in numeric order (row labels are text)
            for row, cols in sorted(rows.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
                label = next((cols.get(c) for c in label_cols if cols.get(c)), None)
                if label is None:
                    continue  # **** row
                users = set()
                for ext, fl in refs:
                    users |= field_use[(ext, fl)].get(row, set())
                if name == "portraits":  # portraits are also referenced by resref (Portrait = po_<BaseResRef>)
                    users |= portrait_users.get(str(label).lower(), set())
                used_count += bool(users)
                out_rows.append(dict(row=row, label=label, used_by=sorted(users)[:8], used=bool(users),
                                     source=sources[cols["__source"]]["kind"]))
            tables[name] = dict(rows=out_rows, provided=len(out_rows), used=used_count, sources=sorted(src_kinds),
                                references=[f"{e}.{l}" for e, l in refs])

    # ---- tilesets: .set files and which areas use them (an area's Tileset field names the .set)
    tilesets = {}
    area_ts = defaultdict(list)
    for value, relpath in db.execute("SELECT fi.value, f.relpath FROM fields fi JOIN files f ON f.id=fi.file_id "
                                     "WHERE f.ext='are' AND fi.label='Tileset'"):
        area_ts[value.lower()].append(relpath)
    for fid, sid, resref, ext, size, node in files:
        if ext == "set":
            tilesets[resref] = dict(source=sources[sid]["kind"], hak=os.path.basename(sources[sid]["path"]),
                                    areas=area_ts.get(resref, []), used=bool(area_ts.get(resref)))
    for ts, areas in area_ts.items():     # a tileset no source carries: assumed to come with the game
        tilesets.setdefault(ts, dict(source="base game (not in module/haks)", hak="", areas=areas, used=True))

    # ---- talk tables: what the custom tlk provides, what is referenced, what is broken
    import nwnlib as n
    tlk = dict(custom_name=meta.get("tlk_custom_name") or "", custom_path=meta.get("tlk") or "",
               base_path=meta.get("tlk_base") or "", custom_entries=0, custom_used=0, refs_total=0, refs_custom=0,
               broken=[], unused_custom=[], usage={})
    refs = db.execute("SELECT s.file_id, f.relpath, s.path, s.strref, s.custom, s.has_text, s.status FROM strrefs s "
                      "JOIN files f ON f.id=s.file_id").fetchall()
    used_custom = defaultdict(list)
    for fid, relpath, path, strref, custom, has_text, status in refs:
        tlk["refs_total"] += 1
        if custom:
            tlk["refs_custom"] += 1
            # custom strrefs are entry index + 0x01000000 (nwnlib.CUSTOM_TLK_BIT); keep the plain entry index
            used_custom[strref - n.CUSTOM_TLK_BIT].append(f"{relpath}:{path}")
        # broken: the entry is not in its talk table, or the custom tlk isn't available and the file carries no
        # text of its own to fall back on
        if status in ("custom-missing", "base-missing") or (status == "no-custom-tlk" and not has_text):
            tlk["broken"].append(dict(file=relpath, field=path, strref=strref, custom=bool(custom), status=status,
                                      fallback_text=bool(has_text)))
    # 2da columns holding strrefs (hak tables: class/race/feat/spell names + descriptions)
    # The column names are matched exactly (2da column names vary in case between tables).
    for name, row, col, value, sid in db.execute(
            "SELECT t.name, t.row, t.col, t.value, f.source_id FROM twoda t JOIN files f ON f.id=t.file_id "
            "WHERE t.col IN ('Name','Description','StrRef','STRING_REF','Plural','Lower','ConverName','ConverNameLower',"
            "'DescStrref','SpellDesc','TOOLSCATEGORIES','DESCRIPTION','NAME','FeatStrRef')"):
        try:
            v = int(value)
        except (TypeError, ValueError):
            continue
        if v >= n.CUSTOM_TLK_BIT:
            used_custom[v - n.CUSTOM_TLK_BIT].append(f"{name}.2da row {row} {col}")
            tlk["refs_custom"] += 1
    if tlk["custom_path"] and os.path.isfile(tlk["custom_path"]):
        try:
            ct = n.Tlk(tlk["custom_path"])
            entries = dict(ct.entries())
            tlk["custom_entries"] = len(entries)
            tlk["custom_used"] = sum(1 for i in entries if i in used_custom)
            tlk["unused_custom"] = [dict(index=i, text=t[:80]) for i, t in entries.items() if i not in used_custom][:2000]
            # strrefs from GFF files that point at missing entries are already in "broken" (strrefs.status); here
            # the 2da references to entries the custom tlk doesn't have are added
            for i, users in used_custom.items():
                if i not in entries:
                    for u in users:
                        if u.endswith(tuple(f" {c}" for c in ("Name", "Description", "StrRef", "STRING_REF"))) or ".2da" in u:
                            tlk["broken"].append(dict(file=u, field="", strref=i + n.CUSTOM_TLK_BIT, custom=True,
                                                      status="custom-missing", fallback_text=False))
        except ValueError as ex:
            tlk["error"] = str(ex)
    tlk["usage"] = {str(i): u[:6] for i, u in list(used_custom.items())[:5000]}   # capped to keep the JSON small
    tlk["broken_count"] = len(tlk["broken"])

    # ---- 2da conflicts: hak vs hak (row level), stale vs base game, and head models
    # one read of the game's key files serves both checks
    base = n.BaseGame(meta["nwn_root"]) if meta.get("nwn_root") else None
    conflicts_2da = twoda_conflicts(db, sources, rank, base)
    heads = head_check(db, sources, base, rows_by_table)

    catalog = dict(module=meta.get("module_name"), haks_order=json.loads(meta.get("haks") or "[]"), haks_missing=json.loads(meta.get("haks_missing") or "[]"),
                   custom_tlk=meta.get("custom_tlk") or "", tlk=tlk, haks=haks, tables=tables, tilesets=tilesets,
                   twoda_conflicts=conflicts_2da, heads=heads,
                   shadowed=sorted(shadowed, key=lambda x: x["resource"]),
                   summary=dict(haks=len(haks), hak_files=sum(h["files"] for h in haks.values()),
                                hak_files_used=sum(h["used_files"] for h in haks.values()),
                                hak_bytes=sum(h["bytes"] for h in haks.values()),
                                twoda_conflicts=len(conflicts_2da), stale_2da=sum(1 for c in conflicts_2da if c["kind"] == "stale"),
                                heads_missing=sum(1 for h in heads if not h["exists"]),
                                tables=len(tables), rows_provided=sum(t["provided"] for t in tables.values()),
                                rows_used=sum(t["used"] for t in tables.values()), tilesets=len(tilesets),
                                shadowed=len(shadowed)))
    with open(os.path.join(analysis, "catalog.json"), "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False)
    d = os.path.join(analysis, "reports")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "hak_catalogue.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "table/hak", "row/file", "label", "used", "used by", "source", "wins"])
        for hname, h in haks.items():
            for kind, lst in h["inventory"].items():
                for x in lst:
                    w.writerow(["hak file", hname, x["name"], kind, x["used"], "", h["kind"], x["wins"]])
        for tname, t in tables.items():
            for r in t["rows"]:
                w.writerow(["2da row", tname, r["row"], r["label"], r["used"], "; ".join(r["used_by"]), r["source"], ""])
        for ts, t in tilesets.items():
            w.writerow(["tileset", t["hak"], ts, "", t["used"], "; ".join(t["areas"]), t["source"], ""])
        for sdw in shadowed:
            w.writerow(["shadowed", sdw["loser"], sdw["resource"], "", "", f"beaten by {sdw['winner']}", sdw["loser_kind"], False])
        for c2 in conflicts_2da:
            w.writerow(["2da conflict", c2["table"], c2["kind"], c2["summary"], "", c2.get("advice", ""), c2.get("loser", ""), ""])
        for hd in heads:
            if not hd["exists"]:
                w.writerow(["missing head", hd["model"], hd["creature"], f"head {hd['head']}", False, hd["where"], "", ""])
        for b in tlk["broken"]:
            w.writerow(["tlk broken ref", tlk["custom_name"], b["strref"], b["field"], False, b["file"], b["status"], ""])
        for u in tlk["unused_custom"]:
            w.writerow(["tlk unused entry", tlk["custom_name"], u["index"], u["text"], False, "", "custom", ""])
    if verbose:
        s = catalog["summary"]
        print(f"  catalogue: {s['haks']} hak(s), {s['hak_files_used']}/{s['hak_files']} hak files referenced, "
              f"{s['rows_used']}/{s['rows_provided']} 2da rows used, {s['tilesets']} tileset(s), {s['shadowed']} shadowed, "
              f"tlk: {tlk['custom_used']}/{tlk['custom_entries']} custom entries used, {tlk['broken_count']} broken refs")
    db.close()
    return catalog


def _rows(text):
    """Parse 2da text into (columns, {row number: {column: value}}). The row number is the line's position as text
    ("0", "1" ...), which is what the game uses; the first-column label is ignored, as the game does. Raises
    ValueError for a file that isn't a 2da."""
    import nwnlib as n
    t = n.read_2da(text)
    return t.columns, {str(i): dict(zip(t.columns, v)) for i, (_label, v) in enumerate(t.rows)}


def twoda_conflicts(db, sources, rank, base):
    """Row-level differences where the same 2da exists in several sources, and staleness vs the base game.

    db: the index (sqlite3 connection). sources: {source id: dict(path, kind, priority)}. rank: build_catalog's
    precedence key. base: nwnlib.BaseGame of the install, or None (skips the base-game comparison). Returns a list of dicts with
    table, kind ("duplicate", "hak-vs-hak", "override", "stale" or "customised"), winner, loser, summary and
    advice (plus row lists). Each 2da is read again from its source; a copy that can't be read is skipped.
    Read-only."""
    import nwnlib as n
    out = []
    by_name = defaultdict(list)
    for fid, sid, resref, relpath in db.execute("SELECT f.id, f.source_id, f.resref, f.relpath FROM files f WHERE f.ext='2da'"):
        by_name[resref].append((rank(sid), sid, fid, relpath))

    def load(sid, relpath):
        """Text of one 2da copy from its source folder or archive, or None if it can't be read."""
        s = sources[sid]
        p = s["path"]
        try:
            if os.path.isdir(p):
                return n.decode_text(n.read_file_inside(p, relpath))
            erf = n.Erf(p)
            try:
                e = next((x for x in erf.entries if x.filename == relpath), None)
                return n.decode_text(erf.read(e)) if e else None
            finally:
                erf.close()       # an open archive keeps the hak locked on Windows
        except Exception:  # noqa
            return None
    for name, lst in by_name.items():
        lst.sort()
        parsed = []
        for r_, sid, fid, relpath in lst:
            txt = load(sid, relpath)
            if txt is None:
                continue
            try:
                cols, rows = _rows(txt)
            except ValueError:
                continue
            # parsed stays in precedence order (lst was sorted by rank), so parsed[0] is the copy the game uses
            parsed.append((sid, os.path.basename(sources[sid]["path"]), sources[sid]["kind"], cols, rows))
        if not parsed:
            continue
        wsid, wname, wkind, wcols, wrows = parsed[0]
        for sid, lname, lkind, lcols, lrows in parsed[1:]:
            # lost: rows only the losing copy has (the game never sees them); differ: same row, different values
            lost = [r for r in lrows if r not in wrows]
            differ = [r for r in lrows if r in wrows and lrows[r] != wrows[r]]
            same = len(lrows) - len(lost) - len(differ)
            if not lost and not differ:
                out.append(dict(table=name, kind="duplicate", winner=wname, loser=lname, summary=f"{lname} carries an identical copy - harmless, remove to save space", advice="remove the copy from " + lname))
                continue
            adv = []
            if lost:
                adv.append(f"{len(lost)} row(s) exist only in {lname} and never load")
            if differ:
                adv.append(f"{len(differ)} row(s) differ - {wname}'s version is what the game uses")
            fix = ("swapping the hak order would make " + lname + "'s version win - check its rows cover what " + wname + " has"
                   if lkind == "hak" and wkind == "hak" else "merge the rows into the winning copy")
            out.append(dict(table=name, kind="hak-vs-hak" if lkind == "hak" and wkind == "hak" else "override",
                            winner=wname, loser=lname, lost_rows=[dict(row=r, label=lrows[r].get(next((c for c in lcols if c.lower() in ("label", "name", "baseresref")), lcols[0]), "")) for r in lost][:200],
                            differ_rows=differ[:200], summary=f"{wname} beats {lname} on {name}.2da: " + "; ".join(adv),
                            advice=fix))
        # staleness vs base game: a hak 2da made for an older game version hides rows/columns the game added since
        if base:
            bt = base.get(f"{name}.2da")
            if bt:
                try:
                    bcols, brows = _rows(n.decode_text(bt))
                except ValueError:
                    bcols, brows = None, None
                if brows is not None:
                    missing_rows = [r for r in brows if r not in wrows]
                    missing_cols = [c for c in bcols if c not in wcols]
                    custom = [r for r in wrows if r not in brows or wrows[r] != brows[r]]
                    if missing_rows or missing_cols:
                        out.append(dict(table=name, kind="stale", winner=wname, loser="base game",
                                        summary=f"{wname}'s {name}.2da is older than the installed game: it hides "
                                                f"{len(missing_rows)} base-game row(s)" + (f" and lacks column(s) {', '.join(missing_cols[:6])}" if missing_cols else "") +
                                                f"; it customises {len(custom)} row(s)",
                                        advice="take a fresh copy of the game's 2da and re-apply only the customised rows",
                                        missing_rows=[dict(row=r, label=next((brows[r].get(c) for c in ("LABEL", "Label", "label", "Name", "BaseResRef") if brows[r].get(c)), "")) for r in missing_rows][:200],
                                        missing_cols=missing_cols, custom_rows=custom[:400]))
                    elif custom:
                        out.append(dict(table=name, kind="customised", winner=wname, loser="base game",
                                        summary=f"{wname}'s {name}.2da customises {len(custom)} row(s) vs the installed game and is otherwise current",
                                        advice="", custom_rows=custom[:400]))
    return out


def head_check(db, sources, base_game, rows_by_table):
    """Does the head model each creature asks for exist (base game, haks or module)?

    For every .utc/.bic with a part-based appearance (appearance.2da MODELTYPE "P" and a one-letter RACE), the
    head model name is built as the game names it: p<gender m/f><race><phenotype>_head<nnn>.mdl, e.g.
    pmh0_head012.mdl. Returns a list of dict(creature, node, head, model, exists, status, where); status is "ok",
    "MISSING", or "unknown (set NWN install folder)" when the base game could not be checked. base_game: a
    nwnlib.BaseGame, or None. Read-only."""
    present = {f"{r}.{e}" for r, e in db.execute("SELECT resref, ext FROM files")}
    base = base_game.names() if base_game else set()
    app = rows_by_table.get("appearance", {})
    pheno = rows_by_table.get("phenotype", {})
    creatures = {}
    for relpath, node, label, value in db.execute(
            "SELECT f.relpath, f.node, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
            "WHERE f.ext IN ('utc','bic') AND fi.path=fi.label AND fi.label IN ('Appearance_Type','Appearance_Head','Gender','Phenotype')"):
        creatures.setdefault((relpath, node), {})[label] = value
    heads = []
    for (relpath, node), v in creatures.items():
        if "Appearance_Head" not in v or "Appearance_Type" not in v:
            continue
        row = app.get(str(v["Appearance_Type"]))
        race = (row or {}).get("RACE") or ""
        if not row or len(race) != 1 or row.get("MODELTYPE", "P").upper() != "P":
            continue  # not a part-based (player-style) appearance
        gender = "f" if str(v.get("Gender", "0")) == "1" else "m"      # GFF Gender: 0 male, 1 female
        ph = str(v.get("Phenotype", "0"))
        model = f"p{gender}{race.lower()}{ph}_head{int(v['Appearance_Head']):03d}.mdl"
        exists = model in present or model in base
        if not base and not exists:
            # without the game's KEY index we cannot tell a base-game head from a missing one (EE ships heads
            # well past 100 - one large module's "missing" 140/143 were base game): never call it MISSING
            status = "unknown (set NWN install folder)"
        else:
            status = "ok" if exists else "MISSING"
        heads.append(dict(creature=relpath, node=node, head=int(v["Appearance_Head"]), model=model, exists=exists or status.startswith("unknown"),
                          status=status, where=node))
    return [h for h in heads]


if __name__ == "__main__":
    build_catalog(sys.argv[1])          # argument: the analysis folder
