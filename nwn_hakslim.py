"""
nwn_hakslim.py - rationalised ("lean") haks.

Why this exists
---------------
On a persistent world every player downloads the module's haks, and their size is load time. Big community haks
(CEP and similar) carry many files a given module never reaches, and the same file is often carried by several haks.
This module works out which files of each hak can go, and writes slimmer copies for a clean build (nwn_build.py).

Default mode "builder": a hak is the builders' palette, so unused content stays. Only duplicates carried in
several places and dead-end content nobody can address (walkmeshes without their model; with aggressive=True also
textures/models nothing mentions) go. A copy in the builder's own override folder never counts for anything here:
players don't have that folder (and the game prefers haks and the module over it - nwnlib.source_rank).
Mode "minimal": only what the module reaches (a client download for a fixed module).

Reads: the analysis index (<analysis>/index.sqlite: sources, files, edges, models, meta, base_resources) and the
original haks (read only). Writes: <build>/haks/<new name>.hak plus haks/manifest.json listing what was dropped and
the savings. Before writing, it removes only the .hak files the previous build's haks/manifest.json lists as its own
output (existing_lean_haks); any other file in <build>/haks/ is left alone.
Never modifies the original haks. Only a hak that actually changes is rebuilt: one whose plan drops nothing (and that
has no file an ERF can't hold) is left alone - not written, not renamed - and the manifest marks it "unchanged" with
its original path, so the clean module keeps loading the original. Each rebuilt hak gets a NEW name (<hak>_r1, _r2 ...
- see new_hak_names) so copying it into a hak folder can never replace an original; nwn_build.py then points the
clean module.ifo at the new names of the rebuilt haks only.

Keep rules for mode "minimal" (conservative - when in doubt, keep):
  * every file reachable from the module through the dependency graph (blueprints, scripts, 2da mentions,
    models -> textures, .set -> tiles, etc.)
  * every file with an extension in ALWAYS_KEEP_EXT (2da, tlk, set, ini, txt, ltr, ssf, mtr, shd, itp, jui, tml,
    sql): the engine reads these by name, so no reference to them is ever recorded
  * anything matching engine naming patterns (TILE_RE and nwn_analysis.ASSET_REVIEW_PATTERNS: portraits po_*,
    part-based body parts, tiles t??##_*, load screens, music/ambient) unless aggressive=True
  * files a kept file mentions by name (transitive, within the haks)

Main entry points
-----------------
    plan_lean_haks(analysis, mode, aggressive) -> per-hak keep/drop/review lists   read-only
    write_lean_haks(analysis, out_dir, ...)    -> manifest dict (or None)          writes <out_dir>/haks/ only
                                                  (only the haks that change; see "unchanged" in its docstring)
    new_hak_names(originals, taken)            -> {old name: new name}             pure function
    taken_hak_names(paths)                     -> names already in those folders   read-only

Known limits
------------
2da row numbers (appearance, phenotype, item parts, placeables...) are not resolved to file names, so models and
textures reached only that way look unused. That is why builder mode only lists them for review, and why the
naming-convention patterns exist. A file whose name cannot be packed into an ERF (over 16 characters, unsafe or an
unknown type) is left out of the lean hak; the manifest counts and lists such files (unpackable / unpackable_files).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict, deque

import nwnlib as n
from nwn_analysis import ASSET_REVIEW_PATTERNS, PART_RE, Graph

# file types the engine (or a tool) loads by name: no reference to them is ever recorded, so minimal mode keeps them all
ALWAYS_KEEP_EXT = {"2da", "tlk", "set", "ini", "txt", "ltr", "ssf", "mtr", "shd", "itp", "jui", "tml", "sql"}
# tileset tile models/textures: "t" + 2-letter tileset code + 2 digits + "_", e.g. ttr01_a01_01 (named in the .set)
TILE_RE = re.compile(r"^t[a-z]{2}\d{2}_")


# the only file types builder mode may treat as "dead end" (everything else is kept unless it is an identical duplicate)
ADDRESSABLE_EXT = {"mdl": "model", "tga": "texture", "dds": "texture", "plt": "texture", "txi": "texture",
                   "wok": "walkmesh", "pwk": "walkmesh", "dwk": "walkmesh"}


def plan_lean_haks(analysis, mode="builder", aggressive=False, skip=()):
    """
    Decide, per hak, which files stay.
      mode="builder" (default): keep everything a builder could still use; drop only
          - duplicates: identical bytes carried in several sources (keep the winning copy)
          - walkmeshes whose model is gone
          Textures/models nothing mentions by name are LISTED (review) but kept: on real CEP content most such
          names turn out to be 2da-row conventions, supermodels, hyphenated names or material (.mtr) chains.
          aggressive=True drops them.
      mode="minimal": keep only what the module reaches (+ 2da/tlk/set/engine-named, transitive) - a client
          download for a fixed module, not a builders' palette.
    analysis: path of the analysis folder (its index.sqlite is read). skip: hak names the module no longer loads
    (replaced by a Hak-editor rebuild): not slimmed, and their copies never make another hak's copy a duplicate.
    A copy in the user's override folder never counts either: players don't have that folder, and the game prefers
    the haks and the module over it anyway (nwnlib.source_rank).
    Returns dict(hak_name -> dict(path, keep[list of relpaths], drop[list of {file, bytes, why}],
    review[list of {file, bytes, why}], bytes_in, bytes_out, mode)); drop and review are sorted largest first.
    hak_name is the hak's file name without extension. Returns {} when the module uses no haks.
    Read-only: writes nothing.
    """
    db = sqlite3.connect(os.path.join(analysis, "index.sqlite"))
    g = Graph(db)
    skip = {x.lower() for x in skip}
    sources = {sid: dict(path=p, kind=k, priority=pr) for sid, p, k, pr in db.execute("SELECT * FROM sources")
               if k != "override" and
               os.path.splitext(os.path.basename(os.path.normpath(p)))[0].lower() not in (skip if k == "hak" else ())}
    files = [r for r in db.execute("SELECT f.id, f.source_id, f.resref, f.ext, f.size, f.node, f.relpath, f.sha256 "
                                   "FROM files f") if r[1] in sources]
    hak_sids = [sid for sid, s in sources.items() if s["kind"] == "hak"]
    if not hak_sids:
        db.close()
        return {}
    base_names = {r[0] for r in db.execute("SELECT name FROM base_resources")} \
        if db.execute("SELECT name FROM sqlite_master WHERE name='base_resources'").fetchone() else set()
    node_source = defaultdict(set)
    by_name = defaultdict(list)
    for fid, sid, resref, ext, size, node, relpath, sha in files:
        node_source[node].add(sid)
        # sorted by the game's precedence below, so the first copy of a name is the one players get
        by_name[f"{resref}.{ext}"].append((n.source_rank(sources[sid]["kind"], sources[sid]["priority"]), sid, sha, relpath))
    # every node something points at (a tag_provider edge only says "this object carries tag X", not a use)
    referenced_any = {d for s_, d in db.execute("SELECT src, dst FROM edges WHERE kind NOT IN ('tag_provider')")}
    supermodels = {r[0].lower() for r in db.execute("SELECT DISTINCT supermodel FROM models WHERE supermodel<>''")}
    drop_reason = {}   # (sid, relpath) -> why
    review = {}        # (sid, relpath) -> why (kept, but listed for a builder to judge)
    if mode == "minimal":
        # roots: the module itself plus every file and 2da of the module (palettes excluded: they list every
        # blueprint, which is not a real use) - the same roots the audit's "in_use" check uses
        roots = {"module"} | {k for k, v in g.nodes.items() if v["in_module"] and
                              (v["type"] == "2da" or (k.startswith("file:") and not k.endswith(".itp")))}
        live = g.reachable(roots)
        keep_nodes, q = set(), deque()
        for fid, sid, resref, ext, size, node, relpath, sha in files:
            if sid not in hak_sids:
                continue
            keep = node in live or ext in ALWAYS_KEEP_EXT
            if not keep and not aggressive:
                # names the engine builds from 2da rows (helm_015, pmh14_chest001 ...): nothing in the module names
                # them, so "not reachable" proves nothing (ASSET_REVIEW_PATTERNS ends with PART_RE)
                keep = TILE_RE.match(resref) is not None or any(rx.search(resref) for rx, _ in ASSET_REVIEW_PATTERNS)
            if keep:
                keep_nodes.add(node); q.append(node)
        # breadth-first: anything a kept hak file points at, if a hak carries it, is kept too
        while q:
            cur = q.popleft()
            for dst, kind, via in g.fwd.get(cur, []):
                if dst not in keep_nodes and node_source.get(dst, set()) & set(hak_sids):
                    keep_nodes.add(dst); q.append(dst)
        for fid, sid, resref, ext, size, node, relpath, sha in files:
            if sid in hak_sids and node not in keep_nodes:
                drop_reason[(sid, relpath)] = "not reachable from the module (minimal client build)"
    else:
        # 1) duplicates: same name AND same bytes in several sources -> keep the winner only
        for name, lst in by_name.items():
            lst.sort()                      # by source_rank, so lst[0] is the copy the game uses
            win = lst[0]
            for r_, sid, sha, relpath in lst[1:]:
                if sid in hak_sids and sha == win[2]:
                    drop_reason[(sid, relpath)] = f"identical copy also in {os.path.basename(sources[win[1]]['path'])} (which wins)"
        # 2) dead-end content nobody can address
        for fid, sid, resref, ext, size, node, relpath, sha in files:
            if sid not in hak_sids or (sid, relpath) in drop_reason or ext not in ADDRESSABLE_EXT:
                continue
            if node in referenced_any or f"{resref}.{ext}" in base_names or PART_RE.search(resref) or TILE_RE.match(resref):
                continue
            kind = ADDRESSABLE_EXT[ext]
            if kind == "model":
                # models can be reached through 2da row numbers the toolkit does not resolve (appearance, phenotype,
                # placeables, doors, ...) and as supermodels - so in builder mode they are never dropped, only listed
                if resref in supermodels:
                    continue
                if aggressive:
                    drop_reason[(sid, relpath)] = "dead end: no 2da row, blueprint, tileset, supermodel or naming convention reaches this model"
                else:
                    review[(sid, relpath)] = "nothing mentions this model by name (2da row numbers are not resolved - kept)"
            elif kind == "walkmesh":
                # a .wok/.pwk/.dwk is only loaded together with the model of the same name (which may be the
                # base game's: a hak can add or fix collision for a game model)
                if f"{resref}.mdl" not in by_name and f"{resref}.mdl" not in base_names:
                    drop_reason[(sid, relpath)] = "dead end: walkmesh without its model"
            elif aggressive:
                drop_reason[(sid, relpath)] = "dead end: no model, 2da or txi anywhere mentions this texture"
            else:
                review[(sid, relpath)] = "nothing mentions this texture by name (kept; drop with 'aggressive')"
    out = {}
    for sid in hak_sids:
        name = os.path.splitext(os.path.basename(os.path.normpath(sources[sid]["path"])))[0]
        keep, drop, rev, bin_, bout = [], [], [], 0, 0
        for fid, sid2, resref, ext, size, node, relpath, sha in files:
            if sid2 != sid:
                continue
            bin_ += size or 0
            why = drop_reason.get((sid, relpath))
            if why:
                drop.append(dict(file=relpath, bytes=size or 0, why=why))
            else:
                keep.append(relpath); bout += size or 0
                if (sid, relpath) in review:
                    rev.append(dict(file=relpath, bytes=size or 0, why=review[(sid, relpath)]))
        out[name] = dict(path=sources[sid]["path"], keep=keep, drop=sorted(drop, key=lambda d: -d["bytes"]),
                         review=sorted(rev, key=lambda d: -d["bytes"]), bytes_in=bin_, bytes_out=bout, mode=mode)
    db.close()
    return out


def new_hak_names(originals, taken=()):
    """A NEW name for every rebuilt hak, so a lean hak can never replace the original when it is copied into a hak
    folder: <name>_r1, _r2 ... (16 characters at most - the game's limit), skipping every name in `taken` (the
    original haks and whatever already sits in their hak folders, e.g. an earlier rebuild you installed).

    originals: hak names without extension; taken: names (any case) that must not be used.
    Returns {original name as given: new lower-case name}. Two originals never get the same new name.
    Raises ValueError when _r1 to _r99 are all taken for a hak (a taken name is never returned: it could replace a
    file when the hak is copied into a hak folder).
    Pure function: touches no files. Also used by the Hak editor (nwn_hakedit) for its rebuilds."""
    taken = {t.lower() for t in taken} | {o.lower() for o in originals}
    out = {}
    for o in originals:
        base = re.sub(r"_r\d+$", "", o.lower()) or o.lower()     # a rebuild of acpv41_r1 is acpv41_r2, not _r1_r1
        for k in range(1, 100):
            sfx = f"_r{k}"
            cand = (base[:16 - len(sfx)] + sfx)          # shorten the base, never the suffix, to stay within 16
            if cand not in taken:
                break
        else:
            raise ValueError(f"no free name for a rebuild of {o}: {base}_r1 to _r99 all exist - move or delete old "
                             "rebuilds from the hak folder first")
        out[o] = cand
        taken.add(cand)
    return out


def taken_hak_names(paths):
    """Hak names already present in the folders the original haks live in.

    paths: paths of the original haks (empty entries are ignored). Returns a set of lower-case names without
    extension (.hak and .erf files). Read-only; a folder that can't be listed is skipped, never an error."""
    names = set()
    for d in {os.path.dirname(os.path.abspath(p)) for p in paths if p}:
        try:
            names |= {os.path.splitext(f)[0].lower() for f in os.listdir(d) if f.lower().endswith((".hak", ".erf"))}
        except OSError:
            pass
    return names


def existing_lean_haks(hd):
    """The .hak files in hd that an earlier toolkit build wrote (listed in its haks/manifest.json).

    Raises ValueError if hd holds any other .hak: it was not made by the toolkit, so it must not be deleted or
    overwritten. Read-only."""
    ours = set()
    try:
        with open(os.path.join(hd, "manifest.json"), encoding="utf-8") as fh:
            old = json.load(fh)
        for h in (old.get("haks") or {}).values():
            # an "unchanged" entry names the ORIGINAL hak (nothing was written for it): never ours to delete
            if isinstance(h, dict) and h.get("file") and not h.get("unchanged"):
                ours.add(os.path.basename(str(h["file"])).lower())
    except (OSError, ValueError, AttributeError):
        pass                                             # no (readable) manifest: nothing here is known to be ours
    haks = [f for f in os.listdir(hd) if f.lower().endswith(".hak")]
    foreign = sorted(f for f in haks if f.lower() not in ours)
    if foreign:
        raise ValueError(f"{hd} holds hak files a toolkit build did not write ({', '.join(foreign[:5])}"
                         f"{' ...' if len(foreign) > 5 else ''}) - refusing to replace them; build into another folder")
    return haks


def _packable(relpath):
    """True when an ERF can hold this file: a known resource type and a safe name of at most 16 characters (the ERF
    key's name field is 16 bytes and stores a numeric type, not the extension). Pure function."""
    r_, _, e_ = os.path.basename(relpath).lower().rpartition(".")
    return e_ in n.EXT_TO_RESTYPE and len(r_) <= 16 and n.safe_resref(r_)


def write_lean_haks(analysis, out_dir, mode="builder", aggressive=False, verbose=True, rename=True, progress=None,
                    stop=None, skip=()):
    """Plan (plan_lean_haks) and write the lean haks into <out_dir>/haks/, with manifest.json.

    out_dir: the build folder (nwn_build passes <analysis>/build or --out). Any .hak an earlier build wrote into
        <out_dir>/haks/ is deleted first, so only this build's lean haks are there.
    mode, aggressive: see plan_lean_haks.
    rename: True (the default) gives every rebuilt hak a NEW name from new_hak_names, so it can't replace an original.
    progress(fraction, text), stop(): optional callbacks from the build; stop() returning True raises
        InterruptedError.
    skip: hak names the module no longer uses (e.g. replaced by a Hak-editor rebuild) - not slimmed.
    Only haks that change are written. A hak whose plan drops nothing and keeps nothing an ERF can't hold would come
    out with the same content under a new name - gigabytes of copies on a big world, and no smaller download - so it
    is left alone: no file is written, it keeps its name, and its manifest entry has unchanged=True, file = original =
    the original hak's path, new_name = its own name and bytes_out = bytes_in.
    Returns the manifest dict (haks{name: {file, new_name, original, kept, dropped, unpackable, bytes_in, bytes_out,
    dropped_files, review_files, unpackable_files, then sha256 for a rebuilt hak or unchanged=True}}, rebuilt[names],
    unchanged[names], bytes_in, bytes_out, mode, aggressive), or None when there is no hak to slim. `kept` counts the files written to the lean
    hak (for an unchanged hak: the files it has); `unpackable` the files the plan would keep but no ERF can hold (name
    over 16 characters, unsafe, or an unknown type) - those are listed in unpackable_files and are NOT in the lean hak,
    so a reviewer can see what the slimmed hak lost that way.
    Reads the original haks; never writes them. The written haks are uncompressed ERF V1.0 (nwnlib.write_erf_stream,
    one file in memory at a time) and keep the original's description; a lean hak that ends up larger than its
    compressed original is flagged (grew=True) and listed in manifest["warnings"]."""
    plan = plan_lean_haks(analysis, mode, aggressive, skip)
    if not plan:
        return None
    unpack = {name: [r for r in sorted(p["keep"]) if not _packable(r)] for name, p in plan.items()}
    rebuild = [name for name, p in plan.items() if p["drop"] or unpack[name]]
    # every analysed hak's name counts as taken (an unchanged hak keeps its name, so no rebuild may take it)
    names = new_hak_names(rebuild, taken_hak_names(p["path"] for p in plan.values()) | {k.lower() for k in plan}) \
        if rename else {k: k for k in rebuild}
    hd = os.path.join(out_dir, "haks")
    os.makedirs(hd, exist_ok=True)
    # Remove an earlier build's lean haks - but only haks that build's manifest.json lists as its own output. Any other
    # .hak here was not written by the toolkit (for example --out pointed at a game folder), so refuse rather than
    # delete or overwrite it (golden rule 1: originals are never touched).
    for f in existing_lean_haks(hd):
        os.remove(os.path.join(hd, f))
    manifest = dict(haks={}, rebuilt=list(rebuild), unchanged=[k for k in plan if k not in names], bytes_in=0,
                    bytes_out=0, mode=mode, aggressive=aggressive)
    for name in manifest["unchanged"]:
        # left alone: the clean module keeps loading the original hak under its own name, so nothing is written
        p = plan[name]
        manifest["haks"][name] = dict(file=p["path"], new_name=name, original=p["path"], unchanged=True,
                                      kept=len(p["keep"]), dropped=0, unpackable=0, bytes_in=p["bytes_in"],
                                      bytes_out=p["bytes_in"], dropped_files=[], review_files=p.get("review", [])[:5000],
                                      unpackable_files=[])
        manifest["bytes_in"] += p["bytes_in"]; manifest["bytes_out"] += p["bytes_in"]
        if verbose:
            print(f"  lean hak {name}.hak: nothing to drop - left as it is (not rebuilt, not renamed)")
    total_in = sum(plan[k]["bytes_in"] for k in rebuild) or 1
    done_in = 0
    for name in rebuild:
        p = plan[name]
        if progress:
            progress(done_in / total_in, f"{name}.hak ({p['bytes_in'] / 1e6:.0f} MB)")
        if stop and stop():
            raise InterruptedError("build stopped while writing lean haks - build again")
        done_in += p["bytes_in"]
        src = p["path"]
        # a "hak" can also be a folder of loose files (an unpacked hak). write_erf_stream reads one entry at a time
        # through these read functions, so a large hak is never held in memory whole
        erf = None if os.path.isdir(src) else n.Erf(src)
        entries = {}
        for e in (erf.entries if erf else []):
            entries.setdefault(e.filename, e)       # a name listed twice: the first copy, the one the game reads
        packed = []           # (resref, ext, size, read function) for write_erf_stream
        unpackable = unpack[name]   # kept by the plan, but no ERF can hold them: counted separately, never as "kept"
        for relpath in sorted(p["keep"]):
            if not _packable(relpath):
                continue
            r_, _, e_ = os.path.basename(relpath).lower().rpartition(".")
            if erf:
                e = entries[relpath]
                # a compressed (E1.0) entry is written uncompressed, so its size in the new hak is the unpacked size
                packed.append((r_, e_, e.usize if e.compressed else e.size, (lambda e=e: erf.read(e))))
            else:
                packed.append((r_, e_, os.path.getsize(os.path.join(src, relpath)),
                               (lambda r=relpath: n.read_file_inside(src, r))))
        dest = os.path.join(hd, f"{names[name]}.hak")
        # the hak's description (shown by tools such as the toolset) and its strref travel with the lean copy
        loc_count, loc_bytes = n.erf_description_block(src) if erf else (0, b"")
        n.write_erf_stream(dest, packed, "HAK ", loc_count, loc_bytes, strref=erf.strref if erf else 0xFFFFFFFF)
        if erf:
            erf.close()
        # file lists are capped at 5000 entries each to keep manifest.json (and changes.json) a readable size
        manifest["haks"][name] = dict(file=dest, new_name=names[name], original=p["path"], kept=len(packed),
                                      dropped=len(p["drop"]), unpackable=len(unpackable), bytes_in=p["bytes_in"],
                                      bytes_out=os.path.getsize(dest), dropped_files=p["drop"][:5000],
                                      review_files=p.get("review", [])[:5000], unpackable_files=unpackable[:5000],
                                      # the file's hash, so "Add to game folders" (nwn_install) can tell that the
                                      # lean hak is still exactly what this build wrote
                                      sha256=n.sha256_file(dest))
        manifest["bytes_in"] += p["bytes_in"]; manifest["bytes_out"] += os.path.getsize(dest)
        if erf and os.path.getsize(dest) > os.path.getsize(src):
            # an E1.0 hak's compressed entries are written uncompressed: dropping a few files may not make up for it
            manifest["haks"][name]["grew"] = True
            manifest.setdefault("warnings", []).append(
                f"{names[name]}.hak is larger than {os.path.basename(src)} ({os.path.getsize(src) / 1e6:.1f} MB -> "
                f"{os.path.getsize(dest) / 1e6:.1f} MB): the original stores files compressed and the lean copy "
                "can't - use the original hak instead")
        if verbose:
            print(f"  lean hak {name}.hak -> {names[name]}.hak: kept {len(packed)}, dropped {len(p['drop'])}"
                  + (f", {len(unpackable)} could not be packed" if unpackable else "") +
                  f" ({p['bytes_in'] / 1e6:.1f} MB -> {os.path.getsize(dest) / 1e6:.1f} MB)")
    with open(os.path.join(hd, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    return manifest
