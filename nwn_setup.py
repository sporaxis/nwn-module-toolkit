"""
Installing and updating the toolkit: a folder of its own for the program and your work, found again by every later
version.

Why this exists
---------------
A release is a zip. Run straight from wherever it was unzipped, every new version starts with an empty workspace and
your work stays behind in the old folder. Installing puts the toolkit in one install folder you choose (a default is
suggested) laid out as:

    <install folder>/
        install.json             which version is installed, and every install/update (history)
        toolkit/                 the program - replaced by each update
        nwn_workspace/           your work: settings, analyses, edits, builds ... - never touched by an update
        previous/<version>/      the program files an update replaced (to go back; delete them when you like)
        Start NWN Toolkit.bat    (Windows) / Start NWN Toolkit.command (macOS) / start-nwn-toolkit.sh (Linux)

An unzipped release offers to install itself (dashboard Modules page). Before that it looks for what you already
have: installs it made before (remembered in a small list in your user settings folder, plus the default folder) and
older unzipped copies holding work (nwn_update.find_copies, and Downloads / Desktop / Documents). Installing over an
existing install is an update: the program is swapped, your work stays where it is. Work in an older unzipped copy
can be brought across in the same step (nwn_update.run_import - a copy; the old copy is never changed).

What it reads and writes
------------------------
Reads: this program folder, the install folder, the other copies it is asked about (read only).
Writes (only when you press Install): the install folder (a staging folder _installing/ first, then renames), the
remembered-installs list (installs.json in your user settings folder), and - for "bring my work" - the install's
workspace (see nwn_update). The previous program folder is renamed into previous/, never deleted. Nothing in the
game, your modules or haks; never deletes anything but its own staging folder.

Limits
------
An update refuses while the installed toolkit is running (its dashboard holds a lock in its workspace) or an analysis
is running there. Versions before 1.5.0 hold no such lock: close their console window before updating from them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import nwn_progress
import nwn_update as U

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "NWN Module Toolkit"
STAGING = "_installing"
PREVIOUS_DIR = "previous"
DASHBOARD_LOCK = ".dashboard.lock"   # held (OS lock) in its workspace by a running dashboard, for its whole life
# Left out of the program copy: your work, caches, version control and anything hidden.
SKIP_TOP = {U.WORKSPACE_DIR, "__pycache__", STAGING, PREVIOUS_DIR}
SPARE_BYTES = 200 * 1024 * 1024


# ============================================================================================ where things go

def default_root():
    """The suggested install folder: <home>/NWN Module Toolkit (Linux: ~/nwn-module-toolkit). Not Documents or the
    Desktop: OneDrive and iCloud often sync those, and a sync client copying a live database can lock or damage it."""
    home = os.path.expanduser("~")
    return os.path.join(home, "nwn-module-toolkit" if sys.platform.startswith("linux") else APP_NAME)


def registry_path():
    """installs.json in your user settings folder: the install folders made here, so a later version finds an install
    in a folder you chose yourself. NWN_SETUP_REGISTRY overrides it (tests)."""
    if os.environ.get("NWN_SETUP_REGISTRY"):
        return os.environ["NWN_SETUP_REGISTRY"]
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, APP_NAME, "installs.json")


def remembered():
    """The remembered install folders (strings), as stored. Never raises."""
    try:
        with open(registry_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return [r for r in d.get("installs", []) if isinstance(r, str)] if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def remember(root):
    """Add root to the remembered installs (first in the list). Writes only installs.json."""
    roots = [root] + [r for r in remembered() if os.path.normcase(r) != os.path.normcase(root)]
    p = registry_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(dict(installs=roots[:20]), fh, indent=1)
    os.replace(p + ".tmp", p)


def install_info(root):
    """dict(root, version, analyses, installed_at, running) for an install folder, or None when root is not one.
    Read-only; never raises."""
    try:
        with open(os.path.join(root, U.INSTALL_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    ws = os.path.join(root, U.WORKSPACE_DIR)
    return dict(root=os.path.abspath(root), version=d.get("version") or "unknown", installed_at=d.get("installed_at"),
                analyses=U.analyses_in(ws), running=dashboard_running(ws))


def _history(root):
    """The install.json history of an install folder ([] when unreadable). Never raises."""
    try:
        with open(os.path.join(root, U.INSTALL_FILE), encoding="utf-8") as fh:
            h = json.load(fh).get("history")
        return h if isinstance(h, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def state(here=HERE):
    """This copy as the page needs it: dict(release (a release, not a development copy), version, installed, root)."""
    root = U.install_root(here)
    return dict(release=U.release_info(here) is not None, version=U.version_label(here), installed=root is not None,
                root=root, default_root=default_root())


# ============================================================================================ dashboard lock

def hold_dashboard_lock(workspace):
    """Called by a starting dashboard: take an OS lock on <workspace>/.dashboard.lock and keep the handle open for the
    dashboard's life, so an installer can tell it is running (the OS releases it when the process ends, even after a
    crash). Returns the handle (keep a reference), or None when it can't be taken (never stops the dashboard)."""
    try:
        os.makedirs(workspace, exist_ok=True)
        fh = open(os.path.join(workspace, DASHBOARD_LOCK), "a+b")
    except OSError:
        return None
    try:
        nwn_progress._lock(fh)
        return fh
    except OSError:
        fh.close()
        return None


def dashboard_running(workspace):
    """True when a dashboard holds <workspace>/.dashboard.lock (see hold_dashboard_lock). Never raises."""
    p = os.path.join(workspace, DASHBOARD_LOCK)
    if not os.path.exists(p):
        return False
    try:
        fh = open(p, "a+b")
    except OSError:
        return True
    try:
        nwn_progress._lock(fh)
        nwn_progress._unlock(fh)
        return False
    except OSError:
        return True
    finally:
        fh.close()


# ============================================================================================ looking for what you have

def _children(d, limit=200):
    """Sub-folders of d (full paths), at most limit; [] when d can't be listed."""
    out = []
    for e in U._ls(d, limit):
        p = os.path.join(d, e)
        if os.path.isdir(p) and not os.path.islink(p) and not e.startswith("."):
            out.append(p)
    return out


def find_previous(here=HERE):
    """What is already on this computer, for the Install card: dict(installs=[install_info...], copies=[describe_copy
    of unzipped copies holding work]). Installs: the remembered ones and the default folder. Copies: those next to
    this one (nwn_update.find_copies), in Downloads, Desktop and Documents (two levels down), and this copy itself
    when it already holds work. Read-only; never raises."""
    installs, seen = [], set()
    for r in remembered() + [default_root()]:
        info = install_info(r)
        if info and os.path.normcase(info["root"]) not in seen:
            seen.add(os.path.normcase(info["root"]))
            installs.append(info)
    copies, cseen = [], set()
    # not offered again: copies whose work an install already brought across, and the unzipped copies an install
    # was made from (install.json history)
    for i in installs:
        recs = U.imported(os.path.join(i["root"], U.WORKSPACE_DIR)) + _history(i["root"])
        for rec in recs:
            if isinstance(rec, dict) and isinstance(rec.get("source"), str):
                cseen.add(os.path.normcase(os.path.abspath(rec["source"])))

    def add(d):
        key = os.path.normcase(os.path.abspath(d["path"]))
        if key not in cseen and not U.install_root(d["path"]) and key not in seen:
            cseen.add(key)
            copies.append(d)
    try:
        for d in U.find_copies(here):
            add(d)
    except OSError:
        pass
    home = os.path.expanduser("~")
    for base in ("Downloads", "Desktop", "Documents"):
        for c in _children(os.path.join(home, base)):
            for cand in [c] + _children(c, 50):
                if os.path.isfile(os.path.join(cand, "nwn_dashboard.py")) and \
                        os.path.normcase(os.path.abspath(cand)) != os.path.normcase(os.path.abspath(here)):
                    try:
                        d = U.describe_copy(cand)
                    except (ValueError, OSError):
                        continue
                    if d["analyses"] or d["settings"]:
                        add(d)
    if U.analyses_in(os.path.join(here, U.WORKSPACE_DIR)):
        try:
            add(U.describe_copy(here))
        except (ValueError, OSError):
            pass
    return dict(installs=installs, copies=copies)


# ============================================================================================ plan and install

def _program_files(here):
    """[(path relative to here, bytes)] of the program: everything but SKIP_TOP, hidden entries and links."""
    out = []
    for dirpath, dirs, files in os.walk(here):
        rel_dir = os.path.relpath(dirpath, here)
        top = rel_dir == "."
        dirs[:] = [x for x in dirs if not x.startswith(".") and x != "__pycache__" and
                   not os.path.islink(os.path.join(dirpath, x)) and not (top and x in SKIP_TOP)]
        for f in files:
            p = os.path.join(dirpath, f)
            if f.startswith(".") or os.path.islink(p) or f.endswith(".pyc"):
                continue
            out.append((os.path.normpath(os.path.join(rel_dir, f)), os.path.getsize(p)))
    return sorted(out)


def plan_install(root, here=HERE, bring_from=None, allow_older=False):
    """What Install would do, without doing it.

    root: the install folder you chose. here: this program folder. bring_from: an older unzipped copy whose work to
    copy across (optional). allow_older: let an older version replace a newer installed one.
    Returns dict(root, kind ("new" | "update"), from_version, to_version, analyses (kept, for an update), program_bytes,
    carry_tools [names kept from the installed program's tools/], bring (nwn_update.plan_import or None), older (True
    when this version is older than the installed one), warnings, refused (None or one sentence)). Read-only."""
    root = os.path.abspath(os.path.expanduser((root or "").strip().strip('"')))
    to_v = U.version_label(here)
    plan = dict(root=root, kind="new", from_version=None, to_version=to_v, analyses=[], program_bytes=0,
                carry_tools=[], bring=None, older=False, warnings=[], refused=None)

    def refuse(msg):
        plan["refused"] = msg
        return plan
    if not U.release_info(here):
        return refuse("this is a development copy, not a release - install from a release zip")
    if not (root and os.path.isabs(root)) or os.path.dirname(root) == root:
        return refuse("choose a folder (not a whole drive)")
    if U.install_root(here) and os.path.normcase(U.install_root(here)) == os.path.normcase(root):
        return refuse("this IS the installed toolkit - to update it, start the NEW version's unzipped copy and install from there")
    if U._same_or_inside(here, root):
        return refuse("the install folder can't be this unzipped folder, inside it, or a folder that contains it - "
                      "choose a folder of its own (the suggested one is fine)")
    info = install_info(root)
    if info:
        plan.update(kind="update", from_version=info["version"], analyses=info["analyses"])
        if info["running"] or dashboard_running(os.path.join(root, U.WORKSPACE_DIR)):
            return refuse(f"the installed toolkit in {root} is running - close its console window, then try again")
        busy = [a for a in info["analyses"] if nwn_progress.run_lock_held(os.path.join(root, U.WORKSPACE_DIR, a))]
        if busy:
            return refuse(f"an analysis is running in the installed toolkit ({', '.join(busy)}) - let it finish first")
        mine, theirs = U.version_tuple(to_v), U.version_tuple(info["version"])
        if mine and theirs and mine < theirs:
            plan["older"] = True
            if not allow_older:
                return refuse(f"the installed version ({info['version']}) is newer than this one ({to_v}) - tick "
                              "'go back to this older version' if that is what you want")
        old_prog = os.path.join(root, U.PROGRAM_DIR)
        if os.path.isdir(old_prog):
            plan["carry_tools"] = [t for t, _ in U._tool_files(old_prog)
                                   if not os.path.exists(os.path.join(here, "tools", t))]
    elif os.path.exists(root):
        if not os.path.isdir(root):
            return refuse(f"{root} is a file - choose a folder")
        if [e for e in U._ls(root) if not e.startswith(".")]:
            return refuse(f"{root} already holds other files - choose a new or empty folder, or your existing install")
    if "onedrive" in root.lower() or "icloud" in root.lower() or "mobile documents" in root.lower():
        plan["warnings"].append("this folder looks synced (OneDrive / iCloud): syncing can lock or damage the analysis "
                                "databases - a folder outside it is safer")
    plan["program_bytes"] = sum(b for _, b in _program_files(here))
    if bring_from:
        try:
            plan["bring"] = U.plan_import(bring_from, os.path.join(root, U.WORKSPACE_DIR), os.path.join(root, U.PROGRAM_DIR))
        except ValueError as ex:
            return refuse(f"bring work from {bring_from}: {ex}")
        if plan["bring"]["refused"] and "nothing to bring" not in plan["bring"]["refused"]:
            return refuse(f"bring work from {bring_from}: {plan['bring']['refused']}")
    need = plan["program_bytes"] + ((plan["bring"] or {}).get("total_bytes") or 0)
    free = U.free_bytes(root)
    if free is not None and need + SPARE_BYTES > free:
        return refuse(f"not enough free disk space: {U._mb(need)} needed, {U._mb(free)} free")
    return plan


LAUNCHERS = {
    "nt": ("Start NWN Toolkit.bat",
           '@echo off\r\nREM Starts the NWN Module Toolkit installed in this folder. Your work is in nwn_workspace.\r\n'
           'cd /d "%~dp0toolkit"\r\ncall run_dashboard.bat\r\n'),
    "darwin": ("Start NWN Toolkit.command",
               '#!/bin/bash\n# Starts the NWN Module Toolkit installed in this folder. Your work is in nwn_workspace.\n'
               'cd "$(dirname "$0")/toolkit" || exit 1\nexec /bin/bash ./run_dashboard.command\n'),
    "other": ("start-nwn-toolkit.sh",
              '#!/bin/sh\n# Starts the NWN Module Toolkit installed in this folder. Your work is in nwn_workspace.\n'
              'cd "$(dirname "$0")/toolkit" || exit 1\nexec python3 nwn_dashboard.py\n'),
}


def launcher_for(platform=None):
    """(file name, text) of the start file for this computer (Windows .bat, macOS .command, else .sh)."""
    p = platform or ("nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "other")
    return LAUNCHERS[p]


def run_install(root, here=HERE, bring_from=None, allow_older=False, progress=None, stop=None):
    """Install (or update) the toolkit in root - the steps plan_install describes.

    1. Copy this program into <root>/_installing (size-checked; a Stop or failure removes it: nothing changed).
    2. Update only: copy in the compiler and other programs from the installed program's tools/ that this one lacks,
       then rename the installed program folder to previous/<version>-<time> (refused if it is in use).
    3. Rename _installing to toolkit (if that fails, the previous program is renamed back).
    4. Write the start file, install.json (with the history) and remember root.
    5. With bring_from: copy that copy's work into <root>/nwn_workspace (nwn_update.run_import).
    progress(pct, text) / stop(): optional. Returns dict(root, kind, version, from_version, previous, launcher,
    brought). Raises ValueError for a refused plan or a folder in use."""
    plan = plan_install(root, here, bring_from, allow_older)
    if plan["refused"]:
        raise ValueError(plan["refused"])
    root = plan["root"]
    say = progress or (lambda pct, text: None)
    bring_bytes = (plan["bring"] or {}).get("total_bytes") or 0
    total = max(1, plan["program_bytes"] + bring_bytes)
    done = dict(b=0)
    os.makedirs(root, exist_ok=True)
    stage = os.path.join(root, STAGING)
    if os.path.exists(stage):
        shutil.rmtree(stage)                     # left by an attempt that was killed: always our own copy
    prog = os.path.join(root, U.PROGRAM_DIR)
    try:
        for rel, _size in _program_files(here):
            if stop and stop():
                raise InterruptedError()
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(here, rel), dst)
            if os.path.getsize(dst) != os.path.getsize(os.path.join(here, rel)):
                raise OSError(f"the copy of {rel} has a different size - the disk may be full or failing")
            done["b"] += os.path.getsize(dst)
            say(min(95.0, 100.0 * done["b"] / total), f"copying the program ({U._mb(done['b'])})")
        for t in plan["carry_tools"]:
            dst = os.path.join(stage, "tools", t)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(prog, "tools", t), dst)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    previous = None
    if os.path.isdir(prog):
        label = (plan["from_version"] or "unknown").replace(" ", "-")
        previous = os.path.join(root, PREVIOUS_DIR, f"{label}-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.dirname(previous), exist_ok=True)
        try:
            os.rename(prog, previous)
        except OSError:
            shutil.rmtree(stage, ignore_errors=True)
            raise ValueError(f"the installed program in {prog} is in use - close the installed toolkit's console window "
                             "(and any window open in that folder), then try again")
    try:
        os.rename(stage, prog)
    except OSError:
        if previous:
            os.rename(previous, prog)            # put the installed program back as it was
        shutil.rmtree(stage, ignore_errors=True)
        raise
    os.makedirs(os.path.join(root, U.WORKSPACE_DIR), exist_ok=True)
    name, text = launcher_for()
    lp = os.path.join(root, name)
    with open(lp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    if os.name != "nt":
        os.chmod(lp, 0o755)
    hist = []
    try:
        with open(os.path.join(root, U.INSTALL_FILE), encoding="utf-8") as fh:
            hist = json.load(fh).get("history") or []
    except (OSError, ValueError, AttributeError):
        pass
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    hist.append(dict(at=now, kind=plan["kind"], version=plan["to_version"], from_version=plan["from_version"],
                     previous=os.path.relpath(previous, root) if previous else None, source=os.path.abspath(here)))
    with open(os.path.join(root, U.INSTALL_FILE + ".tmp"), "w", encoding="utf-8") as fh:
        json.dump(dict(layout=1, version=plan["to_version"], installed_at=now, history=hist), fh, indent=1)
    os.replace(os.path.join(root, U.INSTALL_FILE + ".tmp"), os.path.join(root, U.INSTALL_FILE))
    try:
        remember(root)
    except OSError:
        pass                                     # finding it again is a convenience; the install itself is done
    brought = None
    if bring_from and plan["bring"] and not plan["bring"]["refused"]:
        base = 100.0 * done["b"] / total
        brought = U.run_import(bring_from, os.path.join(root, U.WORKSPACE_DIR), prog, stop=stop,
                               progress=lambda pct, t: say(base + (100.0 - base) * pct / 100.0, t))
    say(100.0, "installed")
    return dict(root=root, kind=plan["kind"], version=plan["to_version"], from_version=plan["from_version"],
                previous=previous, launcher=lp, brought=brought)


def launch(root):
    """Start the installed toolkit in its own window, as its start file would: Windows opens the .bat (a new console
    window), macOS opens the .command in Terminal. Returns True when started; False on other systems (start it with
    the start file). Never raises: a failure returns False."""
    name, _ = launcher_for()
    lp = os.path.join(root, name)
    if not os.path.isfile(lp):
        return False
    try:
        if os.name == "nt":
            os.startfile(lp)                     # noqa: S606 - our own start file, a fixed path, no arguments
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/open", lp], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
    except OSError:
        return False
    return False
