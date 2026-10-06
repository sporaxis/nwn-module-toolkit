"""
nwn_quickscan.py - TL;DR of a module in seconds, without the full analysis.

    python nwn_quickscan.py "<module folder or .mod>" [--nwn-root <install>] [--nwn-user <Documents\\Neverwinter Nights>]

Reads only module.ifo, the module's file list and each hak's header/table of contents (no file contents),
so it is quick even for a 5 GB hak set. It tells you:
  - what the module is (name, entry area, areas/scripts/conversations/blueprints, module events, NWNX hints)
  - every hak in load order: found or MISSING, size, what it carries (2das, models, textures, scripts, tilesets)
  - talk tables (base dialog.tlk and the custom tlk from module.ifo) and the user override folder
  - resource clashes: the same file in several haks (first listed wins), module files hidden by a hak, and
    override-folder files the module or a hak hides (haks > module > override, nwnlib.source_rank)
  - warnings (missing haks/tlk, unreadable or compressed haks, over-long names)
  - how big a full analysis is and roughly how long it will take on this PC

Nothing is written anywhere. The dashboard shows the same thing on the Modules page (Quick scan).

Reads: module.ifo (parsed), the module's file names and sizes, each hak's ERF header and key/resource tables
(nwnlib.Erf reads only the tables; no entry is opened), the override folder's file list, the talk-table locations,
nwn_workspace/timings.json (for the time estimate) and each analysis's index.sqlite meta table (read-only, to say
whether this module was analysed before).

Public entry points
-------------------
    scan(module_path, haks, nwn_root, nwn_user, workspace)  -> dict for the dashboard / --json
    plan_sources(...)    the list of everything a full index will read, with cost units (the indexer uses it too)
    format_text(result)  the plain-text report the command line prints

Limits: the time estimate is only as good as the timing history on this PC (see nwn_progress). Clashes are worked
out from file names alone; two haks carrying the same name are reported even if the files are identical.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nwnlib as n  # noqa: E402
import nwn_index  # noqa: E402
import nwn_nasher  # noqa: E402
import nwn_progress  # noqa: E402

# file extension -> the group it is counted in when showing what a hak "carries"; anything else is "other"
CATEGORY = {
    "2da": "2da", "mdl": "models", "wok": "models", "pwk": "models", "dwk": "models",
    "tga": "textures", "dds": "textures", "plt": "textures", "txi": "textures", "mtr": "textures",
    "nss": "scripts", "ncs": "scripts", "set": "tilesets", "wav": "sounds", "bmu": "sounds",
    "ssf": "sounds", "are": "areas", "git": "areas", "gic": "areas", "dlg": "conversations",
    "uti": "blueprints", "utc": "blueprints", "utp": "blueprints", "utd": "blueprints", "ute": "blueprints",
    "utm": "blueprints", "uts": "blueprints", "utt": "blueprints", "utw": "blueprints", "itp": "palettes",
    "bik": "movies", "tlk": "tlk",
}
BP_NAMES = {"uti": "items", "utc": "creatures", "utp": "placeables", "utd": "doors", "ute": "encounters",
            "utm": "stores", "uts": "sounds", "utt": "triggers", "utw": "waypoints"}


# ------------------------------------------------------------------ file lists (names + sizes only)
def list_module(module_path, info=None):
    """[(resref, ext, size)], skipped non-game files, ifo root (or None), error text.

    module_path: an unpacked module folder (walked as nwnlib.walk_folder: hidden folders such as .git and symbolic
    links skipped) or a .mod file.
    Only file names and sizes are read, plus module.ifo itself. A problem is returned as error text, not raised
    (except a .mod whose ERF header is damaged, which raises ValueError from nwnlib.Erf)."""
    items, skipped, root, err = [], [], None, None
    if nwn_nasher.is_project(module_path):
        # a nasher project: GFF files as <name>.<ext>.json, listed under the game names they become
        outside = [0]
        res, skipped = nwn_nasher.list_resources(module_path, outside)
        if info is not None:
            info.update(nasher_outside=outside[0], nasher_sources=nwn_nasher.sources(module_path))
        items = [(r, e, size) for r, e, size, _rel, _j in res]
        try:
            root = nwn_nasher.read_ifo(module_path)
        except ValueError as ex:
            err = f"module.ifo.json could not be read: {ex}"
        return items, skipped, root, err
    if os.path.isdir(module_path):
        # the indexer's walk: hidden folders (.git of a nasher / build folder) and symbolic links are skipped
        for full, rel in n.walk_folder(module_path):
            fn = os.path.basename(full)
            base, dot, ext = fn.rpartition(".")
            # only extensions the game has a resource type for can be packed into a module
            if fn.startswith(".") or not dot or ext.lower() not in n.EXT_TO_RESTYPE:
                skipped.append(rel)
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            items.append((base.lower(), ext.lower(), size))
        ifo = os.path.join(module_path, "module.ifo")
        if os.path.isfile(ifo):
            try:
                root = n.read_gff_file(ifo)
            except Exception as ex:  # noqa
                err = f"module.ifo could not be read: {ex}"
        else:
            err = "no module.ifo in this folder - is it the unpacked module folder?"
    elif os.path.isfile(module_path):
        erf = n.Erf(module_path)
        try:
            items = [(e.resref, e.ext, e.size) for e in erf.entries]
            e = next((e for e in erf.entries if e.filename == "module.ifo"), None)
            if e is None:
                err = "no module.ifo inside this .mod"
            else:
                try:
                    root = n.read_gff(erf.read(e))
                except Exception as ex:  # noqa
                    err = f"module.ifo could not be read: {ex}"
        finally:
            erf.close()
    else:
        err = f"not found: {module_path}"
    return items, skipped, root, err


def ifo_haks(root):
    """Hak names (lower case, no extension) that module.ifo lists, in its order; [] when root is None."""
    if root is None:
        return []
    haks = [h.get("Mod_Hak") for h in (root.get("Mod_HakList", []) or []) if h.get("Mod_Hak")]
    # a single top-level Mod_Hak field is the older way of naming one hak; it is added after the list
    if root.get("Mod_Hak"):
        haks.append(root.get("Mod_Hak"))
    return [h.lower() for h in haks]


def read_erf_toc(path):
    """Table of contents of a hak: entries, version, compressed count. Never reads file contents.

    Returns dict(version="V1.0" | "E1.0", entries=[(resref, ext, size, compressed)], bad_names=[unsafe resrefs]).
    Raises ValueError for a file that is not a valid ERF (callers report it as a hak error)."""
    erf = n.Erf(path)
    try:
        return dict(version=erf.version, entries=[(e.resref, e.ext, e.size, e.compressed) for e in erf.entries],
                    bad_names=list(erf.bad_names))
    finally:
        erf.close()


def list_folder(path):
    """[(resref, ext, size)] of game files at the top of `path` (the override folder). [] if it can't be read."""
    out = []
    try:
        for de in os.scandir(path):
            if de.is_file(follow_symlinks=False):          # a link is not read by the index either
                base, dot, ext = de.name.rpartition(".")
                if dot and ext.lower() in n.EXT_TO_RESTYPE:
                    out.append((base.lower(), ext.lower(), de.stat().st_size))
    except OSError:
        pass
    return out


def plan_sources(module_path, explicit_haks=(), overrides=None, nwn_root=None, nwn_user=None):
    """
    What a full index will read, with a cost estimate - used by the indexer for progress and by the scan.
    Returns dict(sources=[{path, kind, name, files, bytes, units, toc?}], haks_found, haks_missing, ifo, module_items, ...).

    module_path: module folder or .mod. explicit_haks: hak paths given by hand (--hak). overrides: override folders
    (None = nwn_index's defaults). nwn_root / nwn_user: where to look for haks.
    `units` are nwn_progress.cost() sums. A hak that can't be read becomes a source with `error` and 0 units
    rather than an exception. Read-only.
    """
    linfo = {}
    items, skipped, root, err = list_module(module_path, linfo)
    hak_names = ifo_haks(root)
    dirs = nwn_index.default_hak_dirs(nwn_root, nwn_user)
    found, missing = nwn_index.find_haks(hak_names, list(explicit_haks), dirs)
    # which file each module.ifo entry resolved to (the same rules as the indexer, incl. loose name matches)
    hak_paths = {}
    for h in hak_names:
        f1, _m1 = nwn_index.find_haks([h], list(explicit_haks), dirs)
        hak_paths[h] = os.path.abspath(f1[0]) if f1 else None
    for h in explicit_haks:
        if h not in found:
            found.append(h)
    overrides = nwn_index.default_overrides(nwn_user) if overrides is None else list(overrides)
    sources = [dict(path=os.path.abspath(module_path), kind="module", name=os.path.basename(os.path.normpath(module_path)),
                    files=len(items), bytes=sum(s for _, _, s in items),
                    units=sum(nwn_progress.cost(e, s) for _, e, s in items), items=items)]
    for h in found:
        if os.path.isdir(h):   # unpacked hak folder (tests, nasher layouts)
            ents, _sk, _r, _e = list_module(h)
            sources.append(dict(path=os.path.abspath(h), kind="hak", name=os.path.basename(os.path.normpath(h)).lower(),
                                files=len(ents), bytes=sum(s for _, _, s in ents),
                                units=sum(nwn_progress.cost(e, s) for _, e, s in ents), items=ents, version="folder",
                                compressed=0, bad_names=[]))
            continue
        try:
            toc = read_erf_toc(h)
            ents = [(r, e, s) for r, e, s, _c in toc["entries"]]
            # bytes = the hak file's size on disk (what players download), not the sum of its entries
            sources.append(dict(path=os.path.abspath(h), kind="hak", name=os.path.splitext(os.path.basename(h))[0].lower(),
                                files=len(ents), bytes=os.path.getsize(h), units=sum(nwn_progress.cost(e, s) for _, e, s in ents),
                                items=ents, version=toc["version"], compressed=sum(1 for *_x, c in toc["entries"] if c),
                                bad_names=toc["bad_names"]))
        except Exception as ex:  # noqa
            sources.append(dict(path=os.path.abspath(h), kind="hak", name=os.path.splitext(os.path.basename(h))[0].lower(),
                                files=0, bytes=os.path.getsize(h) if os.path.isfile(h) else 0, units=0, items=[],
                                error=str(ex)))
    for o in overrides:
        ents = list_folder(o)
        sources.append(dict(path=os.path.abspath(o), kind="override", name="override", files=len(ents),
                            bytes=sum(s for _, _, s in ents), units=sum(nwn_progress.cost(e, s) for _, e, s in ents), items=ents))
    return dict(sources=sources, hak_names=hak_names, hak_paths=hak_paths, haks_found=found, haks_missing=missing, ifo=root,
                module_error=err, skipped=skipped, **linfo,
                units=sum(s["units"] for s in sources), files=sum(s["files"] for s in sources),
                bytes=sum(s["bytes"] for s in sources))


# ------------------------------------------------------------------ the scan
# Module events as the toolset names them. module.ifo stores them in short field names (Mod_OnActvtItem -> key
# "ActvtItem"); an unknown one is shown as "On" + its key. The same map as MOD_EVENTS in dashboard.html.
MOD_EVENTS = {
    "AcquirItem": "OnAcquireItem", "ActvtItem": "OnActivateItem", "ClientEntr": "OnClientEnter",
    "ClientLeav": "OnClientLeave", "CutsnAbort": "OnCutsceneAbort", "Heartbeat": "OnHeartbeat", "ModLoad": "OnModuleLoad",
    "ModStart": "OnModuleStart", "PlrChat": "OnPlayerChat", "PlrDeath": "OnPlayerDeath", "PlrDying": "OnPlayerDying",
    "PlrEqItm": "OnPlayerEquipItem", "PlrLvlUp": "OnPlayerLevelUp", "PlrRest": "OnPlayerRest",
    "PlrUnEqItm": "OnPlayerUnEquipItem", "SpawnBtnDn": "OnPlayerRespawn", "UnAqreItem": "OnUnAcquireItem",
    "UsrDefined": "OnUserDefined", "PlrTarget": "OnPlayerTarget", "PlrGuiEvt": "OnPlayerGuiEvent",
    "PlrTileAct": "OnPlayerTileAction", "NuiEvent": "OnNuiEvent"}


def event_name(key):
    """The toolset's name of a module event from its module.ifo key ("ModLoad" or "Mod_OnModLoad" -> "OnModuleLoad")."""
    k = key[6:] if key.startswith("Mod_On") else key
    return MOD_EVENTS.get(k, "On" + k)


def _text(v):
    """A GFF value as display text: a localised string gives its text, None gives ""."""
    if isinstance(v, n.LocString):
        return v.text()
    return "" if v is None else str(v)


def scan(module_path, haks=(), nwn_root=None, nwn_user=None, workspace=None):
    """The quick scan of one module (see the module docstring for what it covers).

    module_path: module folder or .mod. haks: extra hak paths given by hand. nwn_root: game install folder (base
    talk table, hak folder). nwn_user: NWN user folder (haks, tlk, override). workspace: nwn_workspace, for the
    timing history and earlier analyses (None = defaults, no history).
    Returns dict(module, haks, haks_missing, tlk, override, conflicts, warnings, warning_counts, sizes, analysis,
    previous_analysis, nwn_root, nwn_user). Writes nothing."""
    plan = plan_sources(module_path, haks, None, nwn_root, nwn_user)
    root = plan["ifo"]
    mod = plan["sources"][0]
    ext_counts = Counter(e for _, e, _ in mod["items"])
    names = {f"{r}.{e}" for r, e, _ in mod["items"]}
    warnings = []
    if plan["module_error"]:
        warnings.append(dict(level="error", text=plan["module_error"]))

    # --- module.ifo facts
    info = {}
    events = {}
    if root is not None:
        info = dict(name=_text(root.get("Mod_Name")), description=_text(root.get("Mod_Description"))[:400],
                    entry_area=(root.get("Mod_Entry_Area") or ""), min_game_version=_text(root.get("Mod_MinGameVer")),
                    custom_tlk=(root.get("Mod_CustomTlk") or ""), areas_listed=len(root.get("Mod_Area_list", []) or []),
                    variables=len(root.get("VarTable", []) or []), xp_scale=root.get("Mod_XPScale"),
                    expansion_pack=root.get("Mod_Expan_Pack"))
        for label, f in root.fields.items():
            # module event fields are the ResRef fields named Mod_On<Event> (Mod_OnModLoad -> "ModLoad")
            if label.startswith("Mod_On") and f.type == n.RESREF and f.value:
                events[label[6:]] = f.value.lower()
        ea = info["entry_area"].lower()
        if ea and f"{ea}.are" not in names:
            warnings.append(dict(level="error", text=f"entry area '{ea}' is not in the module"))
    scripts_src = ext_counts.get("nss", 0)
    scripts_bin = ext_counts.get("ncs", 0)
    # a hint only: NWNX include scripts are conventionally named nwnx_<plugin>; their presence suggests NWNX is used
    nwnx = sorted({r for r, e, _ in mod["items"] if e in ("nss", "ncs") and r.startswith(("nwnx", "inc_nwnx"))})
    long_names = sorted(f"{r}.{e}" for r, e, _ in mod["items"] if len(r) > 16)   # a resref holds 16 characters
    if long_names:
        warnings.append(dict(level="error", text=f"{len(long_names)} module file(s) have names over 16 characters "
                             f"and cannot load: {', '.join(long_names[:5])}{' …' if len(long_names) > 5 else ''}"))
    if plan.get("nasher_outside"):
        warnings.append(dict(level="info", text=f"nasher project: {plan['nasher_outside']} file(s) outside the sources "
                             f"nasher.cfg packs ({', '.join(plan['nasher_sources']['include'])}) are not part of the "
                             "module and are not read"))
    if plan["skipped"]:
        warnings.append(dict(level="info", text=f"{len(plan['skipped'])} non-game file(s) in the module folder are ignored "
                             f"(e.g. {', '.join(plan['skipped'][:3])})"))

    module = dict(path=os.path.abspath(module_path), kind=("nasher project" if nwn_nasher.is_project(module_path) else "folder") if os.path.isdir(module_path)
                  else ".mod",
                  files=mod["files"], bytes=mod["bytes"], areas=ext_counts.get("are", 0),
                  # a nasher project's size is its JSON text: the packed module is much smaller (about a quarter)
                  bytes_note="as JSON text - the packed module is smaller" if plan.get("nasher_sources") else "",
                  scripts=max(scripts_src, scripts_bin), scripts_source=scripts_src, scripts_compiled=scripts_bin,
                  conversations=ext_counts.get("dlg", 0), journal=bool(ext_counts.get("jrl")),
                  blueprints={BP_NAMES[k]: ext_counts[k] for k in BP_NAMES if ext_counts.get(k)},
                  events=events, nwnx_scripts=nwnx[:20], nwnx=bool(nwnx),
                  file_types=dict(ext_counts.most_common()), **info)

    # --- haks in load order (first listed wins)
    hak_rows = []
    by_path = {s["path"]: s for s in plan["sources"] if s["kind"] == "hak"}
    # load order = module.ifo order (first wins); haks you added by hand that module.ifo doesn't list come last
    order = [(nm, plan["hak_paths"].get(nm)) for nm in plan["hak_names"]]
    listed_paths = {p for _, p in order if p}
    order += [(os.path.splitext(os.path.basename(os.path.normpath(h)))[0].lower(), os.path.abspath(h))
              for h in plan["haks_found"] if os.path.abspath(h) not in listed_paths]
    for i, (nm, p) in enumerate(order):
        s = by_path.get(p) if p else None
        if not s:
            hak_rows.append(dict(order=i + 1, name=nm, status="MISSING", path=None, bytes=0, files=0, carries={}))
            continue
        cats = Counter(CATEGORY.get(e, "other") for _, e, _ in s["items"])
        row = dict(order=i + 1, name=nm, status="error" if s.get("error") else "found", path=p, bytes=s["bytes"],
                   files=s["files"], carries=dict(cats.most_common()), version=s.get("version"),
                   compressed=s.get("compressed", 0), error=s.get("error"),
                   tilesets=sorted(r for r, e, _ in s["items"] if e == "set")[:12],
                   listed=nm in plan["hak_names"], file=os.path.basename(p))
        hak_rows.append(row)
        if s.get("error"):
            warnings.append(dict(level="error", text=f"hak '{nm}' could not be read: {s['error']}"))
        if s.get("compressed"):
            warnings.append(dict(level="info", text=f"hak '{nm}' has {s['compressed']} compressed entries (EE format) - "
                                 "read normally; rebuilt haks are written uncompressed"))
        if s.get("bad_names"):
            warnings.append(dict(level="warning", text=f"hak '{nm}' has {len(s['bad_names'])} entries with unsafe names"))
    for m in plan["haks_missing"]:
        warnings.append(dict(level="error", text=f"hak '{m}' is listed in module.ifo but was not found "
                             "(the full analysis will mark hak-dependent results as Review)"))

    # --- module event scripts that are not in the module: fine when a hak or the base game has them (x2_mod_def_act
    # and the other default scripts come with the game). Base game: its key file when the install folder is set,
    # else the usual base-game name prefixes.
    hak_names = {f"{r}.{e}" for s in plan["sources"] if s["kind"] == "hak" for r, e, _ in s["items"]}
    base_names = n.base_game_names(nwn_root) if events and nwn_root else set()
    for ev, sc in events.items():
        files = (f"{sc}.nss", f"{sc}.ncs")
        if any(f in names or f in hak_names for f in files):
            continue
        if any(f in base_names for f in files) or (not base_names and sc.startswith(nwn_index.BASE_GAME_PREFIXES)):
            continue
        if base_names:
            warnings.append(dict(level="error", text=f"module event {event_name(ev)} runs '{sc}', which is not in the "
                                 "module, its haks or the base game - the event does nothing"))
        else:
            warnings.append(dict(level="warning" if not plan["haks_missing"] else "info",
                                 text=f"module event {event_name(ev)} runs '{sc}', which is not in the module or its "
                                 "haks (fine if it is a base-game script; set the NWN install folder to check)"))

    # --- talk tables
    base_tlk = next((c for c in nwn_index.base_tlk_candidates(nwn_root) if os.path.isfile(c)), None)
    custom_name = module.get("custom_tlk") or ""
    custom_tlk = nwn_index.locate_custom_tlk(custom_name, nwn_root, nwn_user) if custom_name else None
    tlk = dict(base=base_tlk, custom_name=custom_name, custom=custom_tlk,
               custom_bytes=os.path.getsize(custom_tlk) if custom_tlk else 0)
    if custom_name and not custom_tlk:
        warnings.append(dict(level="error", text=f"module.ifo names custom talk table '{custom_name}.tlk' but it was not "
                             "found in the NWN tlk folder"))
    if not nwn_root:
        warnings.append(dict(level="info", text="NWN install folder not set - base-game checks will be approximate "
                             "(set it in Settings)"))

    # --- resource clashes, in the game's order (nwnlib.source_rank): haks (first listed wins) > module > override.
    # Each list is built in that order, so v[0] is the copy the game uses and the rest are hidden.
    where = defaultdict(list)   # "res.ext" -> [source label] in priority order
    for row in hak_rows:
        s = by_path.get(row.get("path") or "")
        if s:
            for r, e, _ in s["items"]:
                where[f"{r}.{e}"].append("hak:" + row["name"])
    for r, e, _ in mod["items"]:
        where[f"{r}.{e}"].append("module")
    for s in plan["sources"]:
        if s["kind"] == "override":
            for r, e, _ in s["items"]:
                where[f"{r}.{e}"].append("override")
    multi_hak = {k: v for k, v in where.items() if sum(1 for x in v if x.startswith("hak:")) > 1}
    module_hidden = sorted(k for k, v in where.items() if "module" in v and v[0] != "module")
    override_hidden = sorted(k for k, v in where.items() if "override" in v and v[0] != "override")
    twoda_clash = []
    for k, v in sorted(multi_hak.items()):
        if k.endswith(".2da"):
            haks_ = [x[4:] for x in v if x.startswith("hak:")]
            twoda_clash.append(dict(table=k, wins=haks_[0], hidden=haks_[1:]))
    per_hak_hidden = Counter()
    for k, v in multi_hak.items():
        for x in [x for x in v if x.startswith("hak:")][1:]:
            per_hak_hidden[x[4:]] += 1
    for row in hak_rows:
        row["hidden_by_higher"] = per_hak_hidden.get(row["name"], 0)
        if row["files"] and row["hidden_by_higher"] == row["files"]:
            warnings.append(dict(level="warning", text=f"every file in hak '{row['name']}' is also in a hak listed above "
                                 "it - it contributes nothing"))
    if module_hidden:
        warnings.append(dict(level="warning", text=f"{len(module_hidden)} module file(s) are replaced by a hak's copy "
                             f"(haks win over the module), so edits to them in the module have no effect in game "
                             f"(e.g. {', '.join(module_hidden[:4])})"))
    if override_hidden:
        warnings.append(dict(level="info", text=f"{len(override_hidden)} file(s) in your override folder are also in "
                             "the module or its haks; for this module the game uses their copy, not the override's "
                             f"(e.g. {', '.join(override_hidden[:4])})"))
    conflicts = dict(in_several_haks=len(multi_hak), twoda_in_several_haks=twoda_clash[:60],
                     twoda_clash_count=len(twoda_clash), module_hidden=len(module_hidden),
                     module_hidden_examples=module_hidden[:12], override_hidden=len(override_hidden),
                     override_hidden_examples=override_hidden[:12])

    override = next((dict(path=s["path"], files=s["files"], bytes=s["bytes"]) for s in plan["sources"]
                     if s["kind"] == "override"), None)

    # --- sizes and time
    hak_bytes = sum(r["bytes"] for r in hak_rows)
    download = hak_bytes + tlk["custom_bytes"]          # what a player needs besides the module: haks + custom tlk
    history = nwn_progress.load_history(workspace) if workspace else []
    # same cost units the indexer reports while it runs, converted to seconds with this PC's past speed
    est = nwn_progress.estimate(plan["units"], history)

    previous = None
    if workspace and os.path.isdir(workspace):
        previous = _previous_analysis(workspace, os.path.abspath(module_path))

    counts = Counter(w["level"] for w in warnings)
    return dict(module=module, haks=hak_rows, haks_missing=plan["haks_missing"], tlk=tlk, override=override,
                conflicts=conflicts, warnings=warnings, warning_counts=dict(counts),
                sizes=dict(module=mod["bytes"], haks=hak_bytes, tlk=tlk["custom_bytes"], player_download=download,
                           override=override["bytes"] if override else 0),
                analysis=dict(files=plan["files"], bytes=plan["bytes"], units=round(plan["units"]),
                              estimate_s=round(est["total_s"]), estimate_text=nwn_progress.fmt_duration(est["total_s"]),
                              basis=est["basis"]),
                previous_analysis=previous, nwn_root=nwn_root, nwn_user=nwn_user)


def _previous_analysis(workspace, module_path):
    """The first analysis in the workspace made from module_path: dict(name, indexed_at, complete), or None.
    Each index.sqlite is opened read-only with a short timeout; a locked or unreadable one is skipped."""
    import sqlite3
    for nm in sorted(os.listdir(workspace)):
        db_path = os.path.join(workspace, nm, "index.sqlite")
        if not os.path.isfile(db_path):
            continue
        try:
            db = n.sqlite_ro(db_path, timeout=0.5)
            meta = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('module_path','indexed_at')").fetchall())
            db.close()
        except Exception:  # noqa - locked by a running analysis, or incomplete
            continue
        # normcase: case-insensitive on Windows only - on Linux two paths that differ in case are different files
        if os.path.normcase(os.path.abspath(meta.get("module_path", ""))) == os.path.normcase(module_path):
            complete = os.path.isfile(os.path.join(workspace, nm, "report.json")) and \
                not os.path.exists(os.path.join(workspace, nm, ".incomplete"))
            return dict(name=nm, indexed_at=meta.get("indexed_at"), complete=complete)
    return None


# ------------------------------------------------------------------ text output
def fmt_bytes(b):
    """Byte count as short text in 1024 steps: "512 B", "1.5 MB", "2.3 GB"."""
    b = float(b or 0)
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:.0f} {u}" if u == "B" else f"{b:.1f} {u}"
        b /= 1024


def format_text(r):
    """The scan() result as the plain-text report printed on the command line."""
    m = r["module"]
    out = [f"TL;DR - {m.get('name') or os.path.basename(m['path'])}",
           f"  {m['kind']}: {m['path']}",
           f"  {m['files']:,} files ({fmt_bytes(m['bytes'])}{', ' + m['bytes_note'] if m.get('bytes_note') else ''}) - "
           f"{m['areas']} areas, {m['scripts']} scripts, "
           f"{m['conversations']} conversations, " +
           ", ".join(f"{v} {k}" for k, v in m['blueprints'].items()),
           f"  entry area: {m.get('entry_area') or '?'}   custom tlk: {m.get('custom_tlk') or 'none'}   "
           f"journal: {'yes' if m['journal'] else 'no'}   NWNX: {'yes' if m['nwnx'] else 'no sign of it'}"]
    if m.get("events"):
        out.append("  module events: " + ", ".join(f"{event_name(k)}={v}" for k, v in m["events"].items()))
    out.append(f"\nHaks ({len(r['haks'])}, first listed wins):")
    for h in r["haks"]:
        if h["status"] == "MISSING":
            out.append(f"  {h['order']:>3}. {h['name']:<24} MISSING")
            continue
        carries = ", ".join(f"{v} {k}" for k, v in list(h["carries"].items())[:5])
        hid = f"  ({h['hidden_by_higher']} hidden by haks above)" if h.get("hidden_by_higher") else ""
        out.append(f"  {h['order']:>3}. {h['name']:<24} {fmt_bytes(h['bytes']):>9}  {h['files']:>6} files  {carries}{hid}")
    t = r["tlk"]
    out.append(f"\nTalk tables: base {'found' if t['base'] else 'not found'}; custom "
               f"{(t['custom_name'] + ('.tlk found' if t['custom'] else '.tlk MISSING')) if t['custom_name'] else 'none'}")
    if r["override"]:
        out.append(f"Override folder: {r['override']['files']} files ({fmt_bytes(r['override']['bytes'])}) - the module and "
                   "its haks win over it; it replaces only base-game files")
    c = r["conflicts"]
    out.append(f"\nClashes: {c['in_several_haks']:,} files in more than one hak ({c['twoda_clash_count']} of them 2das); "
               f"{c['module_hidden']} module files hidden by haks; {c['override_hidden']} override files hidden by the "
               "module or haks")
    for x in c["twoda_in_several_haks"][:10]:
        out.append(f"   {x['table']}: {x['wins']} wins over {', '.join(x['hidden'])}")
    s = r["sizes"]
    out.append(f"\nSizes: module {fmt_bytes(s['module'])}{' (' + m['bytes_note'] + ')' if m.get('bytes_note') else ''}, haks {fmt_bytes(s['haks'])}, tlk {fmt_bytes(s['tlk'])} "
               f"-> player download {fmt_bytes(s['player_download'])}")
    a = r["analysis"]
    out.append(f"Full analysis: {a['files']:,} files / {fmt_bytes(a['bytes'])} to read - roughly {a['estimate_text']} "
               f"({a['basis']})")
    if r.get("previous_analysis"):
        p = r["previous_analysis"]
        out.append(f"Previous analysis: '{p['name']}' {p.get('indexed_at') or ''} "
                   f"({'complete' if p['complete'] else 'INCOMPLETE'})")
    if r["warnings"]:
        out.append("\nWarnings:")
        for w in r["warnings"]:
            out.append(f"  [{w['level']}] {w['text']}")
    return "\n".join(out)


def main(argv=None):
    """Command line: scan one module and print the text report (or JSON with --json). Install folders not given
    on the command line are taken from the dashboard's saved settings (nwn_workspace/settings.json), if any.
    Returns the exit code: 0 scanned, 2 the module or a --hak file does not exist (one line says which)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module")
    ap.add_argument("--hak", action="append", default=[])
    ap.add_argument("--nwn-root")
    ap.add_argument("--nwn-user")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    a = ap.parse_args(argv)
    # a path typed by hand that does not exist: one plain line and exit code 2, as nwn_analyse.py does (a hak the
    # module LISTS but that can't be found is a finding of the scan, not an error, and is reported as MISSING)
    gone = [p for p in [a.module] + a.hak if not os.path.exists(p)]
    if gone:
        print(f"Not found: {gone[0]} - check the path (put it in quotes if it contains spaces)")
        return 2
    import nwn_update
    ws = nwn_update.workspace_dir(HERE)
    st = {}
    try:
        st = json.load(open(os.path.join(ws, "settings.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    r = scan(a.module, a.hak, a.nwn_root or st.get("nwn_root") or None, a.nwn_user or st.get("nwn_user") or None, ws)
    print(json.dumps(r, indent=1, default=str) if a.json else format_text(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
