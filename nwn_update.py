"""
Updating the toolkit: bring your work across from another installed copy, and the version checks that keep your
work safe when versions change.

Why this exists
---------------
A new version of the toolkit is unzipped into a NEW folder (never over the old one). Everything that is yours lives
inside the old folder: nwn_workspace/ (settings, analyses, edits, builds, accepted issues, waiting palette moves and
field changes, hak projects) and the programs you copied into tools/ (the script compiler). This module lets the
new copy bring all of that across in one step, and records which version did what, so that:

  (a) Bring my work across - plan_import / run_import COPY the old copy's workspace and tools programs into this
      one. The old copy is only read, never changed: it stays a complete, working fallback until you delete it.
  (b) Version stamp - every analysis records the version that made it (report.json "toolkit"), and
      missing_features() lists what analysing again would add (checks that version did not have yet).
  (c) Newer-work guard - work that a build applies (palette moves, field changes, and whatever later versions add)
      stamps the analysis with WORK_FORMAT in toolkit.json. An older version that knows this stamp refuses to build
      or to rewrite that work, instead of silently building without changes it does not understand.

What it reads and writes
------------------------
Reads: the other copy's folder (VERSION.txt, nwn_workspace/, tools/) - only reads, never opens a database.
Writes: only this copy's workspace (a staging folder _importing/ that is moved into place piece by piece, then
imported_from.json) and this copy's tools/ folder (programs that are not there yet; nothing is replaced), and
<analysis>/toolkit.json (the work stamp). Never deletes anything except its own staging folder.

Limits
------
Copies are checked by size, not by hash (a hash would read every byte twice). Programs copied into tools/ keep their
permission bits; on macOS a quarantine mark is not copied (the copy is a new file this program wrote).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time

import nwn_progress

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = "nwn_workspace"     # the workspace folder inside a toolkit folder (when NWN_WORKSPACE is not set)
# An INSTALLED toolkit (nwn_setup.py) is laid out as <install folder>/install.json, <install folder>/toolkit/ (the
# program, replaced by each update) and <install folder>/nwn_workspace/ (your work, never touched by an update).
INSTALL_FILE = "install.json"
PROGRAM_DIR = "toolkit"


def install_root(here=HERE):
    """The install folder when `here` is an installed toolkit's program folder (<root>/toolkit with
    <root>/install.json), else None. Never raises."""
    root = os.path.dirname(os.path.abspath(here))
    if os.path.basename(os.path.abspath(here)) == PROGRAM_DIR and os.path.isfile(os.path.join(root, INSTALL_FILE)):
        return root
    return None


def workspace_dir(here=HERE):
    """Where a toolkit keeps your work - the one rule every program uses: NWN_WORKSPACE when set; for an installed
    toolkit <install folder>/nwn_workspace (beside the program, so an update that replaces the program never touches
    it); otherwise nwn_workspace inside the toolkit folder (an unzipped or development copy). Never raises."""
    env = os.environ.get("NWN_WORKSPACE")
    if env:
        return env
    root = install_root(here)
    return os.path.join(root, WORKSPACE_DIR) if root else os.path.join(os.path.abspath(here), WORKSPACE_DIR)

# --- (c) the saved-work format -------------------------------------------------------------------------------
# Bump WORK_FORMAT whenever a version adds a new kind of saved work that a build applies, or extends an existing one
# (a new field in blueprint_changes.json...). Every function that saves such work calls claim_work() first.
#   1 = toolkit 1.5.0: palette_moves.json (palette moves) and blueprint_changes.json (name/tag/description/faction)
WORK_FORMAT = 1
WORK_FILE = "toolkit.json"          # <analysis>/toolkit.json: {"work_format", "saved_with", "saved_at"}

# --- (b) what analysing again adds --------------------------------------------------------------------------
# One entry per check an older analysis cannot have: the report.json key it fills, the version that added it, and
# what you get, in the words the pages use. Checks the dashboard works out on demand (the palette) are not listed:
# they need no new analysis.
REANALYSE = [
    dict(key="asset_duplicates", since="1.4.0",
         what="Models and textures with the same content under different names (Duplicates page)"),
    # merge_rules 2: a blueprint's entry in the custom palette (.itp) no longer holds back merging identical copies
    dict(key="merge_rules", since="1.5.2",
         what="Identical blueprints listed in your custom palette can be merged (Duplicates page)"),
    # noise_rules 1: item tag scripts in use; problems only in unused content as info; hak layering as info; Review
    # items held back only by general reasons counted apart as "likely in use"; 2: a tester's report - CreateObject's
    # new-tag argument is not a blueprint, condition scripts in action slots, static placeables / OnConversation,
    # creature heartbeats as notes, where a mismatched variable is set
    dict(key="noise_rules", since="1.5.2", value=2,
         what="Fewer false alarms: item scripts counted as used, problems in unused content and hak layering as notes, "
              "a CreateObject's new tag no longer read as a blueprint, harmless missing scripts as notes"),
]

# --- (a) bringing work across -------------------------------------------------------------------------------
IMPORT_RECORD = "imported_from.json"   # in the workspace: [{from, version, at, analyses, files, bytes}, ...]
STAGING = "_importing"                 # in the workspace: copies land here first, then move into place
# Top-level workspace entries never brought across: the staging folder, files you put aside to delete, caches,
# and the other copy's own import record (this copy keeps its own).
SKIP_TOP = {STAGING, "_to_delete", "__pycache__", IMPORT_RECORD}
# Top-level folders shared by all analyses (hak projects, Modules-folder Move records, comparisons, snapshots): when
# this copy already has one, the other copy's entries are added to it one by one instead of skipping the folder.
SHARED_DIRS = {"_haks", "_housekeeping", "_diffs", "_snapshots"}
# Files the toolkit writes by itself about its own runs (learnt timings): taken only when this copy has none.
SETTINGS = "settings.json"
ANALYSIS_NAME = re.compile(r"[A-Za-z0-9_\-]+")      # the names the dashboard accepts for an analysis folder
SPARE_BYTES = 200 * 1024 * 1024                      # free space to leave on the disk after a copy
SCAN_LIMIT = 400                                     # folders looked at per level when searching for other copies


# ============================================================================================ versions

def release_info(folder=HERE):
    """(edition, version, commit) from <folder>/VERSION.txt (written by make_release.py), or None for a development
    copy or a folder without one. Never raises."""
    try:
        with open(os.path.join(folder, "VERSION.txt"), encoding="utf-8") as fh:
            parts = fh.read().split()
    except OSError:
        return None
    return tuple(parts[:3]) if len(parts) >= 3 else None


def version_label(folder=HERE):
    """"vault 1.5.0" for a release, "development copy" otherwise. Never raises."""
    info = release_info(folder)
    return f"{info[0]} {info[1]}" if info else "development copy"


def version_tuple(v):
    """(1, 5, 0) from "1.5.0" or "vault 1.5.0"; None when v holds no dotted version ("development copy"). A date-style
    version compares as numbers too: "2026.10.01" -> (2026, 10, 1). Never raises."""
    m = re.search(r"(\d+(?:\.\d+)+)", v or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def report_stamp():
    """What an analysis records about the toolkit that made it (report.json "toolkit")."""
    return dict(version=version_label(), work_format=WORK_FORMAT)


def missing_features(report):
    """The REANALYSE entries a report lacks: what analysing the module again would add. [] for a current report.
    An entry with a `value` also counts as lacking when the report's number for it is lower (rules revised within a
    version, e.g. noise_rules 1 -> 2)."""
    r = report or {}

    def lacks(f):
        if f["key"] not in r:
            return True
        v = r[f["key"]]
        return "value" in f and isinstance(v, int) and v < f["value"]
    return [dict(f) for f in REANALYSE if lacks(f)]


# ============================================================================================ (c) newer-work guard

def read_work(analysis):
    """<analysis>/toolkit.json as a dict ({} when missing or unreadable). Never raises."""
    try:
        with open(os.path.join(analysis, WORK_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def newer_work(analysis):
    """None, or one plain sentence when the analysis holds work saved by a newer toolkit than this one (its
    work_format is higher than WORK_FORMAT), naming that version. Never raises."""
    w = read_work(analysis)
    fmt = w.get("work_format")
    if isinstance(fmt, int) and fmt > WORK_FORMAT:
        return (f"changes in this analysis were saved by a newer version of the toolkit ({w.get('saved_with') or 'unknown'}) "
                f"that this version ({version_label()}) can't fully apply - use that version or a newer one")
    return None


def check_work(analysis):
    """Raise ValueError (one sentence, see newer_work) when the analysis holds work saved by a newer toolkit.
    Called before a build and before saving such work. Writes nothing."""
    msg = newer_work(analysis)
    if msg:
        raise ValueError(msg)


def claim_work(analysis):
    """Before saving work a build applies: refuse (ValueError) if a newer toolkit saved work here, else record that
    this version saved some (work_format never goes down). Writes only <analysis>/toolkit.json, through a temporary
    file."""
    check_work(analysis)
    w = read_work(analysis)
    fmt = w.get("work_format") if isinstance(w.get("work_format"), int) else 0
    if fmt == WORK_FORMAT and w.get("saved_with") == version_label():
        return                                   # already stamped by this version: no write on every change
    w.update(work_format=max(fmt, WORK_FORMAT), saved_with=version_label(), saved_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = os.path.join(analysis, WORK_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(w, fh, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(analysis, WORK_FILE))


# ============================================================================================ (a) finding other copies

def _source(folder):
    """(toolkit folder or None, workspace folder) for a folder you picked: a toolkit folder (it has
    nwn_dashboard.py and nwn_workspace/), or a workspace folder itself (when the toolkit was run with NWN_WORKSPACE,
    or you picked nwn_workspace). Raises ValueError when it is neither."""
    folder = os.path.abspath((folder or "").strip().strip('"'))
    if not os.path.isdir(folder):
        raise ValueError(f"not a folder: {folder}")
    # an installed toolkit: its install folder, or its program folder
    root = folder if os.path.isfile(os.path.join(folder, INSTALL_FILE)) else install_root(folder)
    if root:
        return os.path.join(root, PROGRAM_DIR), os.path.join(root, WORKSPACE_DIR)
    if os.path.isfile(os.path.join(folder, "nwn_dashboard.py")):
        ws = os.path.join(folder, WORKSPACE_DIR)
        if not os.path.isdir(ws):
            raise ValueError(f"{folder} is a copy of the toolkit with no {WORKSPACE_DIR} folder - nothing to bring across")
        return folder, ws
    parent = os.path.dirname(folder)
    if os.path.basename(folder) == WORKSPACE_DIR and os.path.isfile(os.path.join(parent, "nwn_dashboard.py")):
        return parent, folder
    if os.path.isfile(os.path.join(folder, SETTINGS)) or any(_is_analysis(os.path.join(folder, e)) for e in _ls(folder)):
        return None, folder
    raise ValueError(f"{folder} is not a copy of the toolkit (no nwn_dashboard.py) or its {WORKSPACE_DIR} folder")


def _ls(d, limit=None):
    """Names in folder d (sorted), [] when it can't be listed; at most limit names. Never raises."""
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return names[:limit] if limit else names


def _is_analysis(d):
    """True for an analysis folder: a valid name and the files an analysis (complete, stopped or archived) has."""
    return (os.path.isdir(d) and not os.path.islink(d) and ANALYSIS_NAME.fullmatch(os.path.basename(d)) is not None
            and not os.path.basename(d).startswith("_")
            and any(os.path.exists(os.path.join(d, f)) for f in ("report.json", "index.sqlite", "archive.json",
                                                                   ".incomplete", "edits")))


def analyses_in(ws):
    """Names of the analysis folders in workspace ws."""
    return [e for e in _ls(ws) if _is_analysis(os.path.join(ws, e))]


def describe_copy(folder):
    """What another copy holds, for the page: dict(path, workspace, version, analyses, settings, tools, running,
    newer_work, newer_version). Read-only; raises ValueError when the folder is not a copy (see _source)."""
    tk, ws = _source(folder)
    names = analyses_in(ws)
    tools = _tool_files(tk) if tk else []
    running = [a for a in names if nwn_progress.run_lock_held(os.path.join(ws, a))]
    newer = []
    for a in names:
        w = read_work(os.path.join(ws, a))
        if isinstance(w.get("work_format"), int) and w["work_format"] > WORK_FORMAT:
            newer.append(dict(analysis=a, saved_with=w.get("saved_with") or "unknown"))
    ver = version_label(tk) if tk else "unknown"
    mine, theirs = version_tuple(version_label()), version_tuple(ver)
    return dict(path=tk or ws, workspace=ws, version=ver, analyses=names,
                settings=os.path.isfile(os.path.join(ws, SETTINGS)), tools=[t[0] for t in tools], running=running,
                newer_work=newer, newer_version=bool(mine and theirs and theirs > mine))


def find_copies(here=HERE):
    """Other copies of the toolkit next to this one that hold work, newest folder first, for the "bring my work
    across" offer. Looks at: this folder's neighbours, its parent's neighbours and their sub-folders - which covers
    "C:\\NWN\\toolkit-1.2.0" next to "C:\\NWN\\toolkit-1.5.0" and the doubled folder Windows' Extract All makes
    (Downloads\\nwn-toolkit-1.5.0\\nwn-toolkit-1.5.0). At most SCAN_LIMIT folders per level. Read-only; never raises."""
    here = os.path.abspath(here)
    parent, grand = os.path.dirname(here), os.path.dirname(os.path.dirname(here))
    cands = [os.path.join(parent, e) for e in _ls(parent, SCAN_LIMIT)]
    if grand != parent:
        for e in _ls(grand, SCAN_LIMIT):
            p = os.path.join(grand, e)
            cands.append(p)
            if os.path.isdir(p) and not os.path.isfile(os.path.join(p, "nwn_dashboard.py")):
                cands += [os.path.join(p, x) for x in _ls(p, 50)]
    out, seen = [], set()
    for c in cands:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen or key == os.path.normcase(here) or not os.path.isfile(os.path.join(c, "nwn_dashboard.py")):
            continue
        seen.add(key)
        try:
            d = describe_copy(c)
        except (ValueError, OSError):
            continue
        if d["analyses"] or d["settings"]:
            try:
                d["modified"] = os.path.getmtime(d["workspace"])
            except OSError:
                d["modified"] = 0
            out.append(d)
    return sorted(out, key=lambda d: -d["modified"])


# ============================================================================================ (a) plan and copy

def _tool_files(tk):
    """[(path relative to tools/, bytes)] of the programs in <tk>/tools - everything but README.txt and hidden or
    linked files."""
    root = os.path.join(tk, "tools")
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".") and not os.path.islink(os.path.join(dirpath, x))]
        for f in files:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root)
            if f.startswith(".") or os.path.islink(p) or rel.lower() == "readme.txt":
                continue
            out.append((rel, os.path.getsize(p)))
    return sorted(out)


def _tree_bytes(p):
    """(files, bytes) under p; links are not followed or counted (they are not copied either)."""
    if os.path.isfile(p):
        return 1, os.path.getsize(p)
    n = size = 0
    for dirpath, dirs, files in os.walk(p):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(dirpath, x))]
        for f in files:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                n += 1
                size += os.path.getsize(fp)
    return n, size


def _same_or_inside(a, b):
    """True when folders a and b are the same or one is inside the other (a copy would copy into itself)."""
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:                      # different drives on Windows
        return False
    return common in (a, b)


def plan_import(folder, workspace, here=HERE):
    """What bringing work across from `folder` would do, without doing it.

    folder: the other copy (its toolkit folder or its nwn_workspace). workspace: this copy's workspace. here: this
    copy's toolkit folder (its tools/ receives the programs). Returns dict(source, version, copy=[{name, kind,
    files, bytes}], skip=[{name, why}], tools=[{name, bytes}], settings ("copy" | "merge" | None), total_bytes,
    free_bytes, warnings=[sentences], refused (None or one sentence: nothing may be copied)). Read-only."""
    info = describe_copy(folder)
    src_tk, src_ws = _source(folder)
    plan = dict(source=info["path"], version=info["version"], copy=[], skip=[], tools=[], settings=None,
                total_bytes=0, free_bytes=None, warnings=[], refused=None)
    if _same_or_inside(src_ws, workspace) or (src_tk and _same_or_inside(src_tk, here)):
        plan["refused"] = "that is this copy of the toolkit (or a folder inside it) - choose the OLD copy's folder"
        return plan
    if info["running"]:
        plan["refused"] = (f"an analysis is running in the other copy ({', '.join(info['running'])}) - let it finish or "
                           "stop it, close that copy's console window, then try again")
        return plan
    if info["newer_version"]:
        plan["warnings"].append(f"the other copy ({info['version']}) is NEWER than this one ({version_label()}): this "
                                "version may not understand all of its work - better to bring work into the newer one")
    for w in info["newer_work"]:
        plan["warnings"].append(f"{w['analysis']}: has changes saved by {w['saved_with']}, which this version won't build "
                                "or change (it says so on the Build page)")
    for e in _ls(src_ws):
        p = os.path.join(src_ws, e)
        if e in SKIP_TOP or e.startswith(".") or e == SETTINGS:
            continue
        if os.path.islink(p):
            plan["skip"].append(dict(name=e, why="a link - links are never followed"))
            continue
        dst = os.path.join(workspace, e)
        if os.path.isdir(p) and e in SHARED_DIRS and os.path.isdir(dst):
            for c in _ls(p):
                cp = os.path.join(p, c)
                if c.startswith(".") or os.path.islink(cp):
                    continue
                if os.path.exists(os.path.join(dst, c)):
                    plan["skip"].append(dict(name=f"{e}/{c}", why="already in this copy"))
                    continue
                files, size = _tree_bytes(cp)
                plan["copy"].append(dict(name=f"{e}/{c}", kind="shared", files=files, bytes=size))
            continue
        if os.path.exists(dst):
            plan["skip"].append(dict(name=e, why="already in this copy (an analysis of that name is here)"
                                     if _is_analysis(p) else "already in this copy"))
            continue
        files, size = _tree_bytes(p)
        plan["copy"].append(dict(name=e, kind="analysis" if _is_analysis(p) else "folder" if os.path.isdir(p) else "file",
                                 files=files, bytes=size))
    if os.path.isfile(os.path.join(src_ws, SETTINGS)):
        plan["settings"] = "merge" if os.path.isfile(os.path.join(workspace, SETTINGS)) else "copy"
    if src_tk:
        for rel, size in _tool_files(src_tk):
            if os.path.exists(os.path.join(here, "tools", rel)):
                plan["skip"].append(dict(name="tools/" + rel.replace(os.sep, "/"), why="already in this copy's tools folder"))
            else:
                plan["tools"].append(dict(name=rel.replace(os.sep, "/"), bytes=size))
    plan["total_bytes"] = sum(c["bytes"] for c in plan["copy"]) + sum(t["bytes"] for t in plan["tools"])
    plan["free_bytes"] = free_bytes(workspace)
    if plan["free_bytes"] is not None and plan["total_bytes"] + SPARE_BYTES > plan["free_bytes"]:
        plan["refused"] = (f"not enough free disk space: the copy needs {_mb(plan['total_bytes'])} and the disk has "
                           f"{_mb(plan['free_bytes'])} free - free some space (or Archive big analyses in the old copy "
                           "first), then try again")
    elif not plan["copy"] and not plan["tools"] and plan["settings"] != "copy":
        # settings alone are only worth bringing when this copy has none (a merge would change nothing you set here)
        plan["refused"] = "nothing to bring across - everything in the other copy is already here"
    return plan


def free_bytes(path):
    """Free space on the disk that holds path, measured at its nearest folder that exists (a plan never creates
    folders); None when it can't be measured. Never raises."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _mb(b):
    """Bytes as a short text: "512 MB", "2.1 GB"."""
    return f"{b / 1024 ** 3:.1f} GB" if b >= 1024 ** 3 else f"{max(1, round(b / 1024 ** 2))} MB"


def _copy_tree(src, dst, tick, stop):
    """Copy file or folder src to dst (which must not exist), file by file, links skipped, each copy's size checked.
    tick(bytes) after each file; stop() -> True raises InterruptedError. Returns files copied."""
    if os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        return _copy_file(src, dst, tick, stop)
    n = 0
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(dirpath, x))]
        out = os.path.join(dst, os.path.relpath(dirpath, src))
        os.makedirs(out, exist_ok=True)
        for f in files:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                n += _copy_file(fp, os.path.join(out, f), tick, stop)
    return n


def _copy_file(src, dst, tick, stop):
    """One file: copy (with its times and permission bits), then compare sizes. Returns 1."""
    if stop and stop():
        raise InterruptedError()
    shutil.copy2(src, dst)
    if os.path.getsize(src) != os.path.getsize(dst):
        raise OSError(f"the copy of {src} has a different size - the disk may be full or failing")
    tick(os.path.getsize(dst))
    return 1


def _merge_settings(src_file, dst_file, src_tk, here):
    """The settings to keep: the other copy's, with this copy's own non-empty values winning (you may already have
    set something here), and paths into the other copy's folder pointed at this copy when the same file exists here
    (the compiler you had in the old tools folder, now copied into this one). Returns the settings dict."""
    with open(src_file, encoding="utf-8") as fh:
        old = json.load(fh)
    old = old if isinstance(old, dict) else {}
    new = {}
    if os.path.isfile(dst_file):
        try:
            with open(dst_file, encoding="utf-8") as fh:
                new = json.load(fh)
        except (OSError, ValueError):
            new = {}
        new = new if isinstance(new, dict) else {}
    merged = dict(old)
    for k, v in new.items():
        if v not in ("", [], None, {}):
            merged[k] = v

    def repoint(p):
        """A path inside the other toolkit folder, moved to the same place in this one when that exists here."""
        if not (src_tk and isinstance(p, str) and p):
            return p
        a = os.path.abspath(p)
        if _same_or_inside(src_tk, a) and os.path.normcase(a) != os.path.normcase(os.path.abspath(src_tk)):
            mine = os.path.join(here, os.path.relpath(a, src_tk))
            return mine if os.path.exists(mine) else p
        return p
    for k, v in list(merged.items()):
        if isinstance(v, str):
            merged[k] = repoint(v)
        elif isinstance(v, list):
            merged[k] = [dict(t, path=repoint(t.get("path"))) if isinstance(t, dict) else repoint(t) for t in v]
    return merged


def run_import(folder, workspace, here=HERE, progress=None, stop=None):
    """Bring the work across: copy everything plan_import lists into this copy, then record it.

    Copies first into <workspace>/_importing/ (stopped or failed: that staging folder is removed and nothing has
    changed here), then moves each piece into place (a rename on the same disk), adds tools programs that are not
    here yet, writes the settings (merged, see _merge_settings) and appends to imported_from.json.
    progress(pct, text): optional. stop(): optional; True stops before the next file (InterruptedError).
    Returns dict(source, version, analyses, files, bytes, tools, settings, skipped, warnings). Raises ValueError when
    the plan is refused. Never writes the other copy."""
    plan = plan_import(folder, workspace, here)
    if plan["refused"]:
        raise ValueError(plan["refused"])
    src_tk, src_ws = _source(folder)
    total = max(1, plan["total_bytes"])
    done = dict(bytes=0)
    say = progress or (lambda pct, text: None)

    def tick(b):
        done["bytes"] += b
        say(min(99.0, 100.0 * done["bytes"] / total), f"copied {_mb(done['bytes'])} of {_mb(total)}")
    stage = os.path.join(workspace, STAGING)
    if os.path.exists(stage):
        shutil.rmtree(stage)                 # left by an earlier attempt that was killed: always our own copies
    files = 0
    try:
        for c in plan["copy"]:
            say(100.0 * done["bytes"] / total, f"copying {c['name']}")
            files += _copy_tree(os.path.join(src_ws, *c["name"].split("/")), os.path.join(stage, *c["name"].split("/")),
                                tick, stop)
        for t in plan["tools"]:
            files += _copy_tree(os.path.join(src_tk, "tools", *t["name"].split("/")),
                                os.path.join(stage, "__tools__", *t["name"].split("/")), tick, stop)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    # Everything is copied and checked: move it into place. Each move is a rename on the same disk.
    say(99.0, "putting the copies in place")
    for c in plan["copy"]:
        dst = os.path.join(workspace, *c["name"].split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(stage, *c["name"].split("/")), dst)
    for t in plan["tools"]:
        dst = os.path.join(here, "tools", *t["name"].split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(stage, "__tools__", *t["name"].split("/")), dst)
    shutil.rmtree(stage, ignore_errors=True)
    if plan["settings"]:
        merged = _merge_settings(os.path.join(src_ws, SETTINGS), os.path.join(workspace, SETTINGS), src_tk, here)
        tmp = os.path.join(workspace, SETTINGS + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=1)
        os.replace(tmp, os.path.join(workspace, SETTINGS))
    rec = dict(source=plan["source"], version=plan["version"], at=time.strftime("%Y-%m-%d %H:%M:%S"),
               analyses=[c["name"] for c in plan["copy"] if c["kind"] == "analysis"], files=files,
               bytes=plan["total_bytes"], tools=[t["name"] for t in plan["tools"]], settings=plan["settings"],
               skipped=plan["skip"], warnings=plan["warnings"])
    hist = []
    try:
        with open(os.path.join(workspace, IMPORT_RECORD), encoding="utf-8") as fh:
            hist = json.load(fh)
    except (OSError, ValueError):
        pass
    hist = (hist if isinstance(hist, list) else []) + [rec]
    with open(os.path.join(workspace, IMPORT_RECORD), "w", encoding="utf-8") as fh:
        json.dump(hist, fh, indent=1)
    say(100.0, "done")
    return rec


def imported(workspace):
    """The import records of this workspace ([] when none). Never raises."""
    try:
        with open(os.path.join(workspace, IMPORT_RECORD), encoding="utf-8") as fh:
            h = json.load(fh)
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def main(argv=None):
    """Command line: python nwn_update.py "<old toolkit folder>" [--apply]. Without --apply it only shows the plan."""
    import argparse
    ap = argparse.ArgumentParser(description="Bring your work across from another copy of the toolkit (copies; the "
                                             "other copy is never changed).")
    ap.add_argument("folder", nargs="?", help="the other copy's folder; leave out to list copies found next to this one")
    ap.add_argument("--apply", action="store_true", help="copy (without it, only show what would be copied)")
    a = ap.parse_args(argv)
    ws = workspace_dir()
    if not a.folder:
        found = find_copies()
        for d in found:
            print(f"{d['path']}  ({d['version']}, {len(d['analyses'])} analyses)")
        if not found:
            print("No other copy with work found next to this one - give its folder.")
        return 0
    try:
        plan = plan_import(a.folder, ws)
    except ValueError as ex:
        print(ex)
        return 1
    for c in plan["copy"]:
        print(f"copy  {c['name']}  ({_mb(c['bytes'])})")
    for t in plan["tools"]:
        print(f"copy  tools/{t['name']}")
    for s in plan["skip"]:
        print(f"skip  {s['name']}: {s['why']}")
    for w in plan["warnings"]:
        print("note: " + w)
    if plan["refused"]:
        print("Refused: " + plan["refused"])
        return 1
    if not a.apply:
        print(f"{_mb(plan['total_bytes'])} to copy. Run again with --apply to copy (the other copy is not changed).")
        return 0
    try:
        rec = run_import(a.folder, ws, progress=lambda pct, text: print(f"{pct:5.1f}%  {text}", flush=True))
    except (ValueError, OSError) as ex:
        print(f"Not copied: {ex}")
        return 1
    print(f"Done: {len(rec['analyses'])} analyses, {rec['files']} files. The other copy was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
