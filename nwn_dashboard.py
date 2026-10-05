"""
nwn_dashboard.py - local dashboard for the NWN module toolkit.

    python nwn_dashboard.py            (opens your browser at http://127.0.0.1:8765)

Point it at a module folder or .mod, click Analyse, browse the reports, pick what to
clean, and build a new module (+ .mod) with a full audit. Everything runs locally:
the server listens on 127.0.0.1 only and every API call needs the session token that
is placed in the browser URL at start-up.

Why this exists
---------------
The toolkit's other modules do the work (nwn_analysis, nwn_build, nwn_edit, nwn_hakedit ...). This file is the glue
that puts them behind one local web page (dashboard.html): a small HTTP server with a JSON API, plus a background-job
runner so long tasks (analyse, build, compile, archive, compare) do not block the page.

What it reads and writes
------------------------
Reads: the module, its haks and the game install (always read-only), and the analysis folders in the workspace.
Writes, all inside the workspace (nwn_workspace/, or NWN_WORKSPACE):
    settings.json, timings.json, build_timings.json, _diffs/, _snapshots/, _housekeeping/, _haks/ (hak editor
    projects), <analysis>_report.html (--export)
    <analysis>/ : the analysis data, edits/, build/, descriptions.json, accepted_issues.json, server_config.json,
                  database.json, compile.json, and a short-lived _db_upload/ folder for database files you pick
Never writes a module or the game install. The only writes outside the workspace are the ones the user asks for
by pressing a button (the toolkit's first rule): Hak editor Add to hak folder (a rebuilt hak under a new name next to
the original, never over it), Hak editor Restore (only for backups made by older toolkit versions; backs up the
current hak first), Hak editor Extract into an empty or new folder the user names, Modules folder Move / Undo, and
Build & audit Add to game folders / Undo (the finished build's .mod and rebuilt haks copied into the user's modules
and hak folders under names that don't exist yet; Undo removes only those files, if unchanged), all done by
nwn_hakedit / nwn_housekeep / nwn_install. Temporary copies for
the compiler and nwn_asm go to the system temp folder and are removed afterwards.
Programs it starts: nwn_analyse.py (child process), the official script compiler
(nwn_compile), nwn_asm (nwn_ncs) and the external tools the user lists in Settings - each without a shell.

Security model (a local server that can read any file you can - so it must only answer this dashboard)
----------------------------------------------------------------------------------------------------
- Listens on 127.0.0.1 only (main): other machines cannot connect at all.
- Session token: a random 192-bit value made at start-up (TOKEN) and put in the URL that is printed and opened.
  Every /api call must send it (X-NWN-Token header, or ?t= for <img> and download links). A web page from another
  site cannot read it, so it cannot drive the API from the user's browser (cross-site requests). Compared in
  constant time.
- Host header must be 127.0.0.1 or localhost: blocks "DNS rebinding", where a hostile site points its own name at
  127.0.0.1 to get around the browser's same-origin rule.
- Content-Security-Policy on the page: nothing (scripts, styles, images) loads from another site and fetch() can
  only reach this server (connect-src 'self'), which closes the common ways for module text shown on the page to
  pull in outside content or send data out; no <base>, plugins, outside form targets, and no other site may frame
  the page. Inline script is allowed (the page is one self-contained file), so the page itself must still escape
  module text it shows.
- Path checks: analysis names must match [A-Za-z0-9_-]+ and exist in the workspace (analysis_dir); module files are
  only read by a relpath listed in the index, and a folder module's file is read only when its real path (links
  resolved) is inside the module folder and it is not itself a symbolic link (read_module_file); toolkit folders
  starting with "_" and symbolic links are never deleted through (delete_analysis).
- Upload names: a database file picked in the browser must be a plain file name with a database extension
  (upload_database_file); hak uploads are checked by nwn_hakedit.name_problem.
- Secrets: server settings you load (settings.tml, env files) are stored with the value of every key whose name looks
  secret (nwn_modsettings.SECRET_RE: pass, pwd, secret, token, credential, api key, private, auth, cdkey, webhook,
  dsn, connection string, and pw / key as whole words) replaced by "(set - hidden)", and a password written inside a
  URL (scheme://user:password@host) replaced by "(hidden)" whatever the key is called (nwn_modsettings._mask), so
  those values never reach server_config.json or the page. A secret in any other form under an innocent key name
  is not recognised.
- External programs always get an argument list, never a shell command line, so characters in a file name cannot
  run commands. On Windows an external tool must be an .exe (a .bat/.cmd would go through cmd.exe). The session
  token is the only boundary for the tool list: whoever holds it can save any program as a tool, so the warning
  for a script interpreter (python, cmd, bash...) is a usability hint, not a security check.
- Request size caps: JSON bodies 5 MB, uploaded files 600 MB. Request fields are type-checked (field / text_list):
  a malformed request is a 400 naming the field; any other exception is a bug and comes back as a 500.

Job and thread model
--------------------
ThreadingHTTPServer answers each request on its own thread. Long work runs in a Job (run_job): a daemon thread whose
print() output is routed into that job's log (_ThreadStdout), polled by the page through /api/job. Only one job at a
time may run per key (an analysis name, "hak:<project>", "_diff", "_housekeeping"). An analysis runs in a separate
Python process (job_analyse) so Stop can end it at once and its memory goes back to the system; other jobs stop
cooperatively at their next progress call. LOCK (re-entrant) guards JOBS, the cached sqlite connections in GRAPHS
and the check-then-delete steps; ICON_LOCK guards the icon cache; every other StampCache has its own lock;
MONITOR_LOCK serialises Log monitor polls. Jobs live in
memory only: they are lost on restart, and finished ones are forgotten after JOB_KEEP_S (6 hours).

Main entry points
-----------------
    main()            command line: start the server (or --export a static HTML report and exit)
    Handler           the HTTP handler: guard, then a lookup in GET_ROUTES / POST_ROUTES (one api_... function or
                      short lambda per endpoint, each with a docstring or comment)
    export_static()   self-contained read-only HTML report of an analysis
    default_nwn_roots()  game install folders found in the usual places (Settings default, /api/detect_nwn_root)

Limits
------
Single user, plain HTTP on the loopback interface (no TLS - nothing leaves the machine). The token is in the page
URL, so it sits in browser history until the dashboard restarts (a new token is made every start).
"""
from __future__ import annotations

import sys

# Checked before anything else is imported, so an old Python gets one plain sentence instead of a traceback.
if sys.version_info < (3, 8):
    sys.exit("The NWN Module Toolkit needs Python 3.8 or newer (this is %d.%d) - install it from python.org"
             % sys.version_info[:2])

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nwnlib as n
import nwn_2da
import nwn_analysis
import nwn_archive
import nwn_bpchanges
import nwn_build
import nwn_compile
import nwn_edit
import nwn_hakedit
import nwn_logs
import nwn_palette
import nwn_sheets
import nwn_setup
import nwn_update
import nwn_progress
import nwn_quickscan

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = nwn_update.workspace_dir(HERE)     # NWN_WORKSPACE, an install's workspace, or ./nwn_workspace


def _version_parts():
    """(edition, version, git commit) from the VERSION.txt that make_release.py writes into a release, or None in a
    development copy (no such file, or not in that form)."""
    try:
        with open(os.path.join(HERE, "VERSION.txt"), encoding="utf-8") as fh:
            parts = fh.read().split()
    except OSError:
        return None
    return tuple(parts[:3]) if len(parts) >= 3 else None


_VP = _version_parts()
# For people and bug reports (console banner, /api/state "version"): "vault 2026.10.01 (commit 1a2b3c4)".
VERSION = f"{_VP[0]} {_VP[1]} (commit {_VP[2]})" if _VP else "development copy"
SERVER_VERSION = f"NWNToolkit/{_VP[1] if _VP else 'dev'}"       # the HTTP Server header
# Session token: 24 random bytes from the OS's secure generator, new every start. See "Security model" above.
TOKEN = secrets.token_urlsafe(24)
JOBS = {}                    # job id -> Job (finished jobs are dropped after JOB_KEEP_S, see prune_jobs)
JOB_KEEP_S = 6 * 3600        # how long a finished job stays pollable; the same window /api/state shows
MONITORS = {}                # analysis name -> nwn_logs.LogMonitor (Log monitor page)
# One poll at a time: a LogMonitor keeps per-file read offsets, and two overlapping /api/logs polls (a slow one plus
# the page's next timer tick) would each read the same bytes and report every error twice.
MONITOR_LOCK = threading.Lock()
# Re-entrant, so a function holding it can call another that takes it again (e.g. delete_analysis ->
# running_job_for, read_module_file -> get_graph).
LOCK = threading.RLock()
# Re-entrant: /api/icons holds it while it gets the resolver from ICONS (whose lock it is) and then uses it.
ICON_LOCK = threading.RLock()
ANALYSIS_CACHES = []         # every StampCache keyed by analysis name: release_analysis empties them all


class StampCache:
    """Values worked out from an analysis' files, kept until those files change.

    get(key, build) returns the cached value while stamp(key) - usually a file's mtime - is unchanged, and otherwise
    calls build(key), closing the old value with close(value) when one was given (open sqlite connections). Keys
    are analysis names, or tuples that start with one. drop(analysis) forgets every entry of an analysis; with
    per_analysis (the default) the cache is in ANALYSIS_CACHES, so release_analysis drops it before an analysis'
    files are archived, deleted or replaced (Windows cannot delete an open file). lock: the lock get / drop hold
    (each cache has its own unless one is given); build runs under it, so two request threads never build the same
    value twice. keep: when given, at most that many keys are held; building a new one first drops (and closes)
    the least recently used, so values that cost a lot of memory (a dependency graph) don't pile up."""
    def __init__(self, stamp, close=None, lock=None, per_analysis=True, keep=None):
        self.stamp, self.close, self.lock, self.keep = stamp, close, lock or threading.RLock(), keep
        self.items = {}              # key -> (stamp, value), least recently used first
        if per_analysis:
            ANALYSIS_CACHES.append(self)

    def get(self, key, build):
        stamp = self.stamp(key)
        with self.lock:
            hit = self.items.pop(key, None)       # re-inserted below: the dict's order is the use order
            if hit and hit[0] == stamp:
                self.items[key] = hit
                return hit[1]
            if hit and self.close:
                self.close(hit[1])
            while self.keep and len(self.items) >= self.keep:
                old_key = next(iter(self.items))
                _stamp, old = self.items.pop(old_key)
                if self.close:
                    self.close(old)
            value = build(key)
            self.items[key] = (stamp, value)
            return value

    def drop(self, analysis):
        with self.lock:
            for k in [k for k in self.items
                      if (k[0] if isinstance(k, tuple) else k).lower() == (analysis or "").lower()]:
                _stamp, value = self.items.pop(k)
                if self.close:
                    self.close(value)


def _mtime_in(name, *files):
    """Modification times of files in an analysis' folder (0 for a missing one) - the usual StampCache stamp.
    Raises ValueError for a name that is not a usable analysis (analysis_dir)."""
    d = analysis_dir(name)
    return tuple(os.path.getmtime(os.path.join(d, f)) if os.path.exists(os.path.join(d, f)) else 0 for f in files)


# analysis name -> (nwn_analysis.Graph, open read-only sqlite3.Connection), see get_graph. A big module's graph takes
# hundreds of MB, so only the two most recently used analyses keep theirs; opening a third releases the oldest.
GRAPHS = StampCache(lambda name: _mtime_in(name, "index.sqlite"), close=lambda v: v[1].close(), lock=LOCK, keep=2)
# analysis name -> nwn_icons.IconResolver for the current game folder, see icon_resolver
ICONS = StampCache(lambda name: _mtime_in(name, "index.sqlite") + (settings().get("nwn_root") or "",),
                   close=lambda r: r.close(), lock=ICON_LOCK)
# (analysis name, hak path or None) -> nwn_hakedit.Context, see editor_context
CONTEXTS = StampCache(lambda key: _mtime_in(key[0], "index.sqlite", "catalog.json"))
# analysis name -> the parts of report.json the dashboard reads on its own (see report_parts)
REPORT_PARTS = StampCache(lambda name: _mtime_in(name, "report.json"))
# analysis name -> (script uses, int constants) for the Database page: slow to work out (see database_view)
DB_USES = StampCache(lambda name: _mtime_in(name, "index.sqlite"))
# game install folder -> nwnlib.BaseGame, or None when it could not be opened (see base_2da); not per analysis
BASEGAME = StampCache(lambda root: None, per_analysis=False)

SETTINGS_FILE = os.path.join(WORKSPACE, "settings.json")
DEFAULT_SETTINGS = dict(compiler="", nwn_root="", nwn_user="", log_paths=[], last_folder="", external_tools=[])


def clean_tools(tools):
    """[{name, path, exts:[...]}] - only well-formed entries (the launcher starts nothing else).

    tools: the external_tools list as stored or sent by the page (anything; bad entries are dropped). Names are cut
    to 60 characters, paths to 500, extensions must be 1-12 letters/digits (".tga" or "tga"); at most 30 extensions
    per tool and 20 tools. Never raises."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()[:60]
        path = str(t.get("path") or "").strip()[:500]
        exts = [e.strip().lower().lstrip(".") for e in (t.get("exts") or []) if isinstance(e, str) and
                re.fullmatch(r"\.?[A-Za-z0-9]{1,12}", e.strip())][:30]
        if name and path:
            out.append(dict(name=name, path=path, exts=exts))
    return out[:20]


def release_analysis(name):
    """Close every open handle and forget every cached value the dashboard keeps on an analysis (graph db, icon
    resolver, editor context, report parts ...) - before Archive, Delete, Clear or re-analysis (Windows can't delete
    or replace an open file)."""
    for cache in ANALYSIS_CACHES:
        cache.drop(name)


def icon_resolver(name):
    """One IconResolver per analysis (re-made when the index or the game folder changes).

    Callers use the resolver while holding ICON_LOCK, so only one request thread uses it at a time."""
    import nwn_icons
    return ICONS.get(name, lambda nm: nwn_icons.IconResolver(analysis_dir(nm), settings().get("nwn_root") or None))


# Program file names (matched against the whole base name, lower case) that run their argument as code: shells,
# script hosts and language runtimes. E.g. "python3.12.exe", "cmd.exe", "bash" match; "nwnexplorer.exe" does not.
# Used only for a warning when settings are saved (settings_warnings): such a tool runs a script file it is given.
INTERPRETERS = re.compile(r"(cmd|powershell|pwsh|wscript|cscript|mshta|rundll32|regsvr32|python[\d.]*w?|py|pyw|node|"
                          r"bash|sh|wsl|perl|ruby|java|javaw)(\.exe)?")


def _is_mac_app(path):
    """True on macOS for an application bundle (Foo.app), which is a folder, not a program file."""
    return sys.platform == "darwin" and path.lower().rstrip("/").endswith(".app") and os.path.isdir(path)


def open_in_tool(name, file_path):
    """Start one of YOUR configured external tools on a file. No shell; only programs listed in Settings.

    name: the tool's name as saved in Settings. file_path: the file to open (must exist; must have one of the tool's
    extensions when it lists any). The tool is a program file (an .exe on Windows) or, on macOS, an application
    bundle (Foo.app), which is started with /usr/bin/open -a as Finder would. Returns dict(started, file). Raises
    ValueError for an unknown tool, a missing or refused program, or a wrong file. Does not wait for the tool and
    never reads or changes the file itself."""
    tool = next((t for t in clean_tools(settings().get("external_tools")) if t["name"] == name), None)
    if not tool:
        raise ValueError(f"'{name}' is not one of your external tools (Settings)")
    exe = tool["path"]
    app = _is_mac_app(exe)
    if not app and not os.path.isfile(exe):
        raise ValueError(f"{exe} does not exist - fix the path in Settings")
    if os.name == "nt" and not exe.lower().endswith(".exe"):
        # .bat/.cmd are refused: Windows runs them through cmd.exe, which would interpret characters like & in a file name
        raise ValueError(f"{exe} is not a program (.exe)")
    if not app and not os.access(exe, os.X_OK):
        raise ValueError(f'{exe} is not executable - run: chmod +x "{exe}"')
    if not (file_path or "").strip():
        raise ValueError("no file given")
    fp = os.path.abspath(file_path)         # absolute, so it can never start with "-" and be taken as an option
    if not os.path.isfile(fp):
        raise ValueError(f"not found (or not a file): {file_path}")
    ext = fp.rsplit(".", 1)[-1].lower() if "." in os.path.basename(fp) else ""
    if tool["exts"] and ext not in tool["exts"]:
        raise ValueError(f"{tool['name']} is set up for .{', .'.join(tool['exts'])} files, not .{ext}")
    kw = dict(cwd=os.path.dirname(exe.rstrip("/")), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL)
    # Detach the tool from the dashboard: it keeps running when the dashboard stops, and a Ctrl+C in the dashboard's
    # console is not passed on to it.
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    argv = ["/usr/bin/open", "-a", exe, fp] if app else [exe, fp]
    subprocess.Popen(argv, shell=False, **kw)        # noqa: S603 - a program you configured, no shell
    return dict(started=tool["name"], file=fp)


def _typed_settings(raw):
    """Settings with the right types whatever is in the file: a bad value falls back to the default instead of
    breaking every page (the Settings page must always open so it can be fixed)."""
    out = dict(DEFAULT_SETTINGS)
    for k, default in DEFAULT_SETTINGS.items():
        v = (raw or {}).get(k, default)
        if isinstance(default, str):
            out[k] = v.strip() if isinstance(v, str) else default
        elif k == "external_tools":
            out[k] = clean_tools(v) if isinstance(v, list) else []
        elif isinstance(default, list):
            out[k] = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    return out


def settings():
    """The saved settings (settings.json in the workspace) with every key present and typed. Read fresh on every
    call, so a change saved by one request is seen by the next. Never raises: a missing or broken file gives the
    defaults."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        return _typed_settings(raw if isinstance(raw, dict) else {})
    except (OSError, ValueError):
        # _typed_settings({}) builds fresh lists: a plain dict(DEFAULT_SETTINGS) would share the default lists, so a
        # caller appending to settings()["log_paths"] would change the defaults for every later call
        return _typed_settings({})


def default_nwn_roots():
    """Game install folders found in the usual places, best first: env NWN_ROOT, the default Steam library, then the
    folders the Beamdog Client lists in its settings.json (00829 = development build, 00785 = stable). These are the
    places and the order the official compiler's own detection uses (neverwinter.nim, neverwinter/game.nim
    findNwnRoot). A folder counts only when it has data/ and lang/, which every NWN:EE install has. Not searched,
    because their layout could not be verified here: GOG installs and Steam libraries on other drives - those are
    set in Settings. Read-only; never raises."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        steam = os.path.join(home, "Library", "Application Support", "Steam", "steamapps", "common")
        bdc = os.path.join(home, "Library", "Application Support", "Beamdog Client", "settings.json")
    elif os.name == "nt":
        steam = os.path.join(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)", "Steam", "steamapps",
                             "common")
        bdc = os.path.join(home, "AppData", "Roaming", "Beamdog Client", "settings.json")
    else:
        steam = os.path.join(home, ".local", "share", "Steam", "steamapps", "common")
        bdc = os.path.join(home, ".config", "Beamdog Client", "settings.json")
    cands = [os.environ.get("NWN_ROOT"), os.path.join(steam, "Neverwinter Nights")]
    try:
        with open(bdc, encoding="utf-8") as fh:
            folders = json.load(fh).get("folders") or []
    except (OSError, ValueError, AttributeError):        # no Beamdog Client, or a settings file of another shape
        folders = []
    cands += [os.path.join(f, release) for release in ("00829", "00785") for f in folders if isinstance(f, str)]
    out = []
    for c in cands:
        if c and c not in out and os.path.isdir(os.path.join(c, "data")) and os.path.isdir(os.path.join(c, "lang")):
            out.append(c)
    return out


def settings_warnings(s):
    """Plain sentences about saved settings that look wrong, for the Settings page; [] when all looks right.

    s: typed settings. Checks that the install folder has data/ and lang/, that the user folder has modules/, and
    names external tools whose program is a script interpreter (it would run a script file it is given). Read-only."""
    out = []
    root, user = s.get("nwn_root"), s.get("nwn_user")
    if root and not (os.path.isdir(os.path.join(root, "data")) and os.path.isdir(os.path.join(root, "lang"))):
        out.append(f"NWN install folder: {root} has no data and lang folders - choose the game's install folder "
                   "(the one that contains them)")
    if user and not os.path.isdir(os.path.join(user, "modules")):
        out.append(f"NWN user folder: {user} has no modules folder - choose Documents/Neverwinter Nights "
                   "(on Linux ~/.local/share/Neverwinter Nights)")
    for t in s.get("external_tools") or []:
        prog = os.path.basename(t["path"].rstrip("/\\")).lower()
        if INTERPRETERS.fullmatch(prog):
            out.append(f"external tool {t['name']}: {prog} runs scripts - a script file opened with it is run as a "
                       "program; choose the tool's own program if it has one")
    return out


def save_settings(s):
    """Write s (a settings dict, already typed by _typed_settings) to settings.json in the workspace."""
    os.makedirs(WORKSPACE, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1)


def safe_name(s):
    """s made safe as a folder / file name: runs of anything but letters, digits, _ and - become one "_", at most
    60 characters, "module" if nothing is left. "My Module (v2)" -> "My_Module_v2"."""
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", s).strip("_")
    return s[:60] or "module"


def analysis_dir(name):
    """Resolve an analysis name to its folder - only names that exist in the workspace.

    The main path check of the API: every endpoint that takes an analysis name goes through here (or through
    _checked_analysis_folder). Raises ValueError unless the analysis is complete and usable - not archived, not
    being archived/unpacked, not incomplete."""
    # Letters, digits, _ and - only: no "/", "\" or ".." can reach os.path.join, so the result stays in the workspace.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError("bad analysis name")
    d = os.path.join(WORKSPACE, name)
    if name.lower() in ARCHIVING:
        raise ValueError(f"'{name}' is being archived or unpacked - wait for it to finish")
    if not os.path.isfile(os.path.join(d, "report.json")):
        if nwn_archive.is_archived(d):
            raise ValueError(f"analysis '{name}' is archived - press Unpack on the Modules page to use it")
        raise ValueError("analysis not found")
    if os.path.exists(os.path.join(d, INCOMPLETE)):
        raise ValueError(f"analysis '{name}' is incomplete (stopped or still running) - re-run it, or Clear it on the Modules page")
    return d


ARCHIVING = set()            # analyses being zipped/unzipped: nothing may open their index meanwhile
INCOMPLETE = ".incomplete"   # marker: an analysis was started here and has not finished (nwn_analyse.py owns it)
# Files an analysis run generates - Clear/Delete remove these. Everything else is kept unless you tick it.
DATA_FILES = ("index.sqlite", "index.sqlite-journal", "index.sqlite-wal", "index.sqlite-shm", "index.state",
              "index.state.tmp", "index.done", "report.json", "catalog.json", "compile.json", "palette.json",
              "palette.json.tmp", INCOMPLETE,
              nwn_progress.RUN_LOCK, "analysis_archive.zip", "analysis_archive.zip.tmp", "archive.json")
DATA_DIRS = ("json", "conversations", "reports", ".unpack_tmp")
# Your own work: only deleted when you tick it (and type the analysis name).
USER_PARTS = {"edits": ("edits", "palette_moves.json", "blueprint_changes.json"), "build": ("build",),
              "descriptions": ("descriptions.json", "batches", "accepted_issues.json", "server_config.json",
                               "database.json")}
ACCEPTED = "accepted_issues.json"      # issues you marked "accepted - by design" (kept across re-analysis)


def analysis_status(d):
    """'complete' | 'incomplete' | None (not an analysis folder)."""
    has_report = os.path.isfile(os.path.join(d, "report.json"))
    has_index = os.path.isfile(os.path.join(d, "index.sqlite"))
    if os.path.exists(os.path.join(d, INCOMPLETE)) or os.path.exists(os.path.join(d, "index.state")) or \
            (has_index and not has_report):
        return "incomplete"
    return "complete" if has_report else None


def running_job_for(name):
    """The running Job whose key is name (case-insensitive), or None."""
    with LOCK:
        return next((j for j in JOBS.values() if j.status == "running" and (j.key or "").lower() == name.lower()), None)


def _checked_analysis_folder(name):
    """The folder of an analysis in any state (incomplete, archived ...) for usage / Clear / Delete / Archive.

    Looser than analysis_dir (no report.json needed) but still refuses bad names, toolkit folders ("_...") and a
    folder that is a symbolic link (deleting through a link could reach outside the workspace)."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError("bad analysis name")
    if name.startswith("_"):
        # _haks (hak projects, and backups that older toolkit versions made when they replaced a hak - the only copies of
        # those originals), _housekeeping (Move/Undo records),
        # _snapshots ... are toolkit folders, not analyses
        raise ValueError(f"'{name}' is a toolkit folder, not an analysis")
    d = os.path.join(WORKSPACE, name)
    if not os.path.isdir(d) or os.path.islink(d):
        raise ValueError("analysis not found")
    return d


def _path_size(p):
    """(bytes, file count) of a file or folder tree; a link counts as nothing (it is not followed)."""
    if os.path.islink(p):
        return 0, 0
    if os.path.isfile(p):
        return os.path.getsize(p), 1
    total = count = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f)); count += 1
            except OSError:
                pass
    return total, count


def analysis_usage(name):
    """Disk use of an analysis folder by part: data (regenerable), edits, build, descriptions, other."""
    d = _checked_analysis_folder(name)
    parts = {k: dict(bytes=0, files=0, items=[]) for k in ("data", "edits", "build", "descriptions", "other")}
    owner = {x: "data" for x in DATA_FILES + DATA_DIRS}
    for k, names in USER_PARTS.items():
        for x in names:
            owner[x] = k
    for entry in sorted(os.listdir(d)):
        part = owner.get(entry, "other")
        b, c = _path_size(os.path.join(d, entry))
        parts[part]["bytes"] += b; parts[part]["files"] += c; parts[part]["items"].append(entry)
    return dict(name=name, folder=d, status=analysis_status(d), parts=parts,
                total=sum(p["bytes"] for p in parts.values()))


def _busy_reason(name, d):
    """Why the folder must not be touched right now, or None."""
    if running_job_for(name):
        return f"an analysis of '{name}' is running - press Stop first"
    if nwn_progress.run_lock_held(d):
        return (f"an analysis process still holds '{name}' (another dashboard window or a command-line run) - "
                "stop it first")
    # runs started by older toolkit versions hold no lock: fall back to 'written in the last minute'.
    # Skipped when the last writer was our own job and its process has ended (Stop -> Clear straight away).
    ours = [j for j in JOBS.values() if (j.key or "").lower() == name.lower() and j.kind == "analyse"]
    ours_ended = bool(ours) and all(j.status != "running" and (j.proc is None or j.proc.poll() is not None) for j in ours)
    if not ours_ended and not os.path.exists(os.path.join(d, nwn_progress.RUN_LOCK)):
        recent = [f for f in ("index.sqlite", "index.sqlite-journal", "index.sqlite-wal", "index.state")
                  if os.path.exists(os.path.join(d, f)) and time.time() - os.path.getmtime(os.path.join(d, f)) < 60]
        if recent:
            return (f"'{name}' was written to in the last minute ({', '.join(recent)}) - it looks like another analysis "
                    "is still running on it. Stop that first, wait a minute, then try again.")
    return None


def delete_analysis(name, include=(), confirm="", only_incomplete=False):
    """
    Remove an analysis's generated data (always), plus the parts in `include` ("edits", "build", "descriptions",
    "other") - those are your work, so they need `confirm` == the analysis name. The whole check-and-delete holds
    LOCK, so no job can start on this analysis half-way through.

    only_incomplete: True for Clear (refuses a complete analysis). Returns dict(removed, kept, failed,
    folder_removed). Raises ValueError for a bad name, a busy analysis or a missing confirmation. Only touches
    entries directly inside the analysis folder; a symbolic link is removed as a link, never followed.
    """
    include = set(include or ())
    bad = include - set(USER_PARTS) - {"other"}
    if bad:
        raise ValueError(f"unknown part(s) {sorted(bad)}")
    if include and confirm != name:
        raise ValueError(f"deleting {', '.join(sorted(include))} needs the analysis name typed exactly: {name}")
    with LOCK:
        d = _checked_analysis_folder(name)
        reason = _busy_reason(name, d)
        if reason:
            raise ValueError(reason)
        st = analysis_status(d)
        if only_incomplete and st != "incomplete":
            raise ValueError(f"'{name}' is a complete analysis - Clear only removes stopped or unfinished runs "
                             "(use Delete in the Analysed modules list to remove a finished one)")
        release_analysis(name)
        targets = list(DATA_FILES) + list(DATA_DIRS)
        for k in include & set(USER_PARTS):
            targets += USER_PARTS[k]
        if "other" in include:
            known = set(DATA_FILES) | set(DATA_DIRS) | {x for v in USER_PARTS.values() for x in v}
            targets += [e for e in os.listdir(d) if e not in known]
        removed, failed = [], []
        # the marker goes last: if anything fails part-way, the analysis stays flagged incomplete
        for t in sorted(set(targets), key=lambda x: x == INCOMPLETE):
            p = os.path.join(d, t)
            if os.path.islink(p):                # checked first: isdir() is also True for a link to a folder
                try:
                    os.unlink(p); removed.append(t)
                except OSError as ex:
                    failed.append(f"{t}: {ex.strerror or ex}")
            elif os.path.isdir(p):
                try:
                    n.rmtree_force(p); removed.append(t + "/")
                except OSError as ex:
                    failed.append(f"{t}/: {ex.strerror or ex}")
            elif os.path.isfile(p):
                if t == INCOMPLETE and failed:
                    continue
                try:
                    os.remove(p); removed.append(t)
                except OSError as ex:
                    failed.append(f"{t}: {ex.strerror or ex}")
        if failed and not os.path.exists(os.path.join(d, INCOMPLETE)) and os.path.isdir(d):
            with open(os.path.join(d, INCOMPLETE), "w", encoding="utf-8") as fh:   # partial delete = not usable
                json.dump(dict(note="delete did not finish"), fh)
        kept = sorted(os.listdir(d))
        if not kept:
            os.rmdir(d)
        return dict(removed=removed, kept=kept, failed=failed, folder_removed=not kept)


def clear_analysis(name):
    """Remove a stopped/failed/interrupted analysis's generated files. Refuses complete or busy analyses."""
    return delete_analysis(name, only_incomplete=True)


# ------------------------------------------------------------------ hak & 2da editors
HAKS = nwn_hakedit.HakEditor(WORKSPACE)        # hak editor projects live in <workspace>/_haks/


def editor_context(analysis, hak_path=None):
    """Analysis knowledge for the editors (cached in CONTEXTS until the analysis changes). None when no analysis is
    chosen."""
    if not analysis:
        return None
    key = (analysis, os.path.normcase(hak_path) if hak_path else None)
    return CONTEXTS.get(key, lambda k: nwn_hakedit.Context(analysis_dir(k[0]), k[1]))


def point_module_at_hak(analysis, old_name, new_name):
    """The module (as an edit of module.ifo in this analysis - the original is never changed) lists new_name where it
    listed old_name. The next clean build uses it. Returns what changed.

    analysis: analysis name; old_name / new_name: hak names without ".hak" (compared case-insensitively). Writes only
    <analysis>/edits/module.ifo. Raises ValueError when there is no module.ifo or it does not list old_name."""
    import nwn_refactor
    d = analysis_dir(analysis)
    db = n.sqlite_ro(os.path.join(d, "index.sqlite"))
    try:
        data, was_edited = nwn_edit.current_bytes(d, "module.ifo", lambda r: nwn_refactor.module_bytes(d, r, db))
    finally:
        db.close()
    if not data:
        raise ValueError("this analysis has no module.ifo")
    root = n.read_gff(data)
    hits = 0
    # Mod_HakList is the module's list of haks (one struct with a Mod_Hak name each). The single top-level Mod_Hak
    # field is the older one-hak form, still present in some modules, so both are updated.
    for st_ in root.get("Mod_HakList") or []:
        if isinstance(st_.get("Mod_Hak"), str) and st_.get("Mod_Hak").lower() == old_name.lower():
            st_.set("Mod_Hak", st_.type_of("Mod_Hak"), new_name); hits += 1
    if isinstance(root.get("Mod_Hak"), str) and root.get("Mod_Hak").lower() == old_name.lower():
        root.set("Mod_Hak", root.type_of("Mod_Hak"), new_name); hits += 1
    if not hits:
        raise ValueError(f"the module of '{analysis}' doesn't list {old_name}.hak - nothing changed")
    out = n.write_gff(root)
    n.read_gff(out)                         # proves the new bytes read back before they replace anything
    # Written next to edits/ first, then moved in with os.replace: a crash part-way leaves no half-written
    # module.ifo edit (the temp file lives outside edits/ so it can never be listed or built as an edit).
    tmp = os.path.join(d, "module.ifo.edit.tmp")
    with open(tmp, "wb") as fh:
        fh.write(out)
    os.replace(tmp, nwn_edit.edit_path(d, "module.ifo"))
    return dict(analysis=analysis, module_ifo="edited" if not was_edited else "edited again", old=old_name, new=new_name)


def analyses_using_hak(path):
    """Complete analyses whose module lists this hak (to offer usage checks automatically).

    path: the hak file (only its base name is compared). Returns analysis names. Opens each index read-only with a
    1-second busy timeout; an analysis whose index cannot be read is skipped. Never raises for a bad index."""
    base = os.path.basename(path).lower()
    out = []
    for a in list_analyses():
        if a.get("incomplete"):
            continue
        try:
            db = n.sqlite_ro(os.path.join(WORKSPACE, a['name'], 'index.sqlite'), timeout=1)
            haks = json.loads((db.execute("SELECT value FROM meta WHERE key='haks'").fetchone() or ["[]"])[0])
            db.close()
        except (sqlite3.Error, ValueError):
            continue
        if any(os.path.basename(h).lower() == base for h in haks):
            out.append(a["name"])
    return out


def base_2da(name):
    """The base game's version of a 2da (parsed table), for comparison in the 2da editor; None when no game folder
    is set, it cannot be opened, or the game has no such 2da. Read-only."""
    root = settings().get("nwn_root") or ""
    if not root:
        return None

    def open_game(root_):
        try:
            return n.BaseGame(root_)
        except Exception:  # noqa - a folder that is not a readable install just means "no base-game comparison"
            return None
    bg = BASEGAME.get(root, open_game)
    data = bg.get(f"{name}.2da") if bg else None
    return nwn_2da.parse(data.decode(n.ENCODING, "replace"))[0] if data else None


def twoda_source(b):
    """(loader, saver, context, display name, current) for a 2da in a hak project or in a module's edits.

    b: the request's fields - src "hak" with p (project id) and name, or src "edit" with a (analysis) and file.
    loader() -> (table, parse issues, original table); saver(table) writes the hak project's staged copy or the
    analysis' edits (never the hak or module); current() -> the bytes now in effect, hashed for the "changed
    elsewhere" check on save. Raises ValueError for an unknown source or a file that is not a 2da."""
    src = b.get("src")
    if src == "hak":
        pid, name = field(b, "p", str, ""), field(b, "name", str, "").lower()
        proj = _hak_project(pid)
        return (lambda: HAKS.load_2da(pid, name), lambda t: HAKS.save_2da(pid, name, t), _hak_context(proj),
                f"{name} in {os.path.basename(proj['source'])}", lambda: HAKS.read(pid, name))
    if src == "edit":
        a, rel = field(b, "a", str, ""), field(b, "file", str, "")
        d = analysis_dir(a)
        if not rel.lower().endswith(".2da"):
            raise ValueError("not a 2da file")

        def load():
            orig_bytes = read_module_file(a, rel)
            if orig_bytes is None:
                raise ValueError(f"{rel} is not in the module")
            cur, _edited = nwn_edit.current_bytes(d, rel, lambda _rel: orig_bytes)
            tab, issues = nwn_2da.parse(cur.decode(n.ENCODING, "replace"))
            return tab, issues, nwn_2da.parse(orig_bytes.decode(n.ENCODING, "replace"))[0]
        def current():
            ob = read_module_file(a, rel)
            return nwn_edit.current_bytes(d, rel, lambda _rel: ob)[0]
        return (load, lambda t: nwn_edit.save_text(d, rel, nwn_2da.write(t)), editor_context(a, None),
                f"{rel} (module edit)", current)
    raise ValueError("unknown 2da source")


def twoda_checks(ctx, name, table, original):
    """nwn_2da.validate's findings for an edited 2da: compared with the original and the base game, and, when an
    analysis is linked (ctx), with the rows the module uses, the scripts that read it and the talk table."""
    c = dict(name=name, original=original, base=base_2da(name))
    if ctx:
        c.update(row_usage=ctx.tables.get(name, {}), usage_known=name in ctx.tables_known,
                 script_readers=sorted(ctx.readers.get(name, [])), tlk=ctx.tlk, tlk_custom_known=bool(ctx.tlk.custom),
                 known_names=ctx.known)
    return nwn_2da.validate(table, c)


def report_parts(name):
    """The parts of an analysis' report.json that the dashboard itself reads, parsed once per report (REPORT_PARTS,
    rebuilt when report.json changes): summary, var_audit, inferred_quests, and unused_scripts (the lower-case names
    of scripts nothing uses, nwn_modsettings.unused_scripts). Only these are kept: a big module's whole report is
    tens of MB and takes about half a second to parse, which several pages used to pay on every request. The page
    itself gets the whole report as stored bytes (/api/report). Callers must not change the dict."""
    def build(nm):
        import nwn_modsettings
        with open(os.path.join(analysis_dir(nm), "report.json"), encoding="utf-8") as fh:
            rep = json.load(fh)
        return dict(summary=rep.get("summary") or {}, var_audit=rep.get("var_audit") or {},
                    inferred_quests=rep.get("inferred_quests") or {}, unused_scripts=nwn_modsettings.unused_scripts(rep))
    return REPORT_PARTS.get(name, build)


def generator_module_info(name):
    """The module's quest-token helper pair (e.g. SetQuestToken/GetQuestToken in a quest include) and its named slots,
    from the analysis' inferred quests - so the generator can offer 'set/check a quest token' with real names.
    Returns (adapter or None, tokens). Read-only."""
    parts = report_parts(name)
    iq = parts["inferred_quests"]
    adapters = (iq.get("summary") or {}).get("adapters") or []
    # only helpers shaped like tokens (int slot, text value) - the generator writes Set<X>(oPC, SLOT, "digit")
    shaped = [a for a in adapters if a.get("key") == "int" and a.get("value") == "string"]
    ad = next((a for a in shaped if "token" in a["setter"].lower()), shaped[0] if shaped else None)
    adapter = dict(setter=ad["setter"], getter=ad["getter"], include=ad["defined_in"]) if ad else None
    tokens = []
    if ad:
        for q in iq.get("quests", []):
            if q.get("system") == ad["setter"][3:]:     # the quest system is the setter's name without "Set"
                for e in q.get("key_exprs", []):
                    if re.fullmatch(r"[A-Za-z_]\w*", e):
                        tokens.append(dict(name=e, slot=q["key"], label=q["label"]))
    tokens.sort(key=lambda t: t["name"])
    if adapter:
        # the string length and the slot names the include defines but nothing uses yet (from the token audit)
        tk = next((t for t in parts["var_audit"].get("tokens", []) if t.get("system") == ad["setter"][3:]), None)
        if tk:
            adapter["length"] = tk.get("length")
            adapter["unused_names"] = sorted({n_ for r_ in tk.get("slots", []) if r_.get("status") == "named, unused"
                                              for n_ in r_.get("names", [])})
        adapter["names"] = sorted({t["name"] for t in tokens})
    return adapter, tokens


SERVER_CFG = "server_config.json"     # settings.tml / server environment you loaded (secrets hidden), kept across re-analysis


def mod_settings(name, configs=None):
    """The Overview's "Module settings & variables" and "Server settings" data for an analysis (read-only).

    configs: the loaded server files (as in server_config.json, already masked); None reads them from disk.
    Returns dict(module, server, configs) where configs lists only each file's name, kind, load time and value
    count - not the values."""
    import nwn_modsettings
    d = analysis_dir(name)
    p = os.path.join(d, SERVER_CFG)
    if configs is None:
        configs = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else []
    parts = report_parts(name)
    db = n.sqlite_ro(os.path.join(d, "index.sqlite"))
    try:
        mod = nwn_modsettings.module_summary(db, dict(var_audit=parts["var_audit"]))
        srv = nwn_modsettings.interpret(configs, db, parts["summary"], parts["unused_scripts"])
    finally:
        db.close()
    return dict(module=mod, server=srv, configs=[dict(name=c["name"], kind=c["kind"], loaded=c.get("loaded"),
                                                      count=len(c.get("values") or {})) for c in configs])


def load_server_config(b):
    """Add (or with b["clear"], remove) one server file - settings.tml, an env file, docker compose - for an analysis.

    b: dict(a=analysis, name=file name, text=file contents, clear=bool). The text is parsed by
    nwn_modsettings.parse_server_file, which replaces secret values with "(set - hidden)" before anything is kept,
    so passwords never reach server_config.json. Written via a .tmp file and os.replace, so a crash part-way never
    leaves a half-written file. Returns mod_settings() for the page."""
    import nwn_modsettings
    d = analysis_dir(b.get("a"))
    p = os.path.join(d, SERVER_CFG)
    configs = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else []
    name = field(b, "name", str, "")
    if b.get("clear"):
        configs = [c for c in configs if name and c["name"] != name]
    else:
        c = nwn_modsettings.parse_server_file(field(b, "text", str, ""), os.path.basename(name))
        c["loaded"] = time.strftime("%Y-%m-%d %H:%M")
        configs = [x for x in configs if x["name"] != c["name"]] + [c]
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(configs, fh, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)
    return mod_settings(b.get("a"), configs)


DB_SNAP = "database.json"     # snapshot of the campaign database files you loaded (read only), kept across re-analysis


def database_view(name, snap=None):
    """The Database page: what the module's scripts store in campaign databases, checked against loaded files.

    name: analysis name. snap: the loaded-database snapshot (database.json); None reads it from disk. Read-only.
    Script uses are capped at 20000 entries for the page."""
    import nwn_database
    d = analysis_dir(name)
    p = os.path.join(d, DB_SNAP)
    if snap is None:
        snap = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}

    def work_out(nm):
        db = n.sqlite_ro(os.path.join(analysis_dir(nm), "index.sqlite"))
        try:
            return nwn_database.script_uses(db), nwn_database.int_constants(db)
        finally:
            db.close()
    uses, consts = DB_USES.get(name, work_out)
    out = nwn_database.cross_check(snap, uses, report_parts(name)["unused_scripts"], consts)
    s_ = settings()
    guess = os.path.join(s_["nwn_user"], "database") if s_.get("nwn_user") else ""
    out["suggested_folder"] = guess if guess and os.path.isdir(guess) else ""
    out["uses"] = [dict(u, db=u["db"] if u["db_how"] in ("literal", "constant", "pattern", "fixed") else
                        f"(run time: {u['db']})") for u in uses if not u.get("via_helper")][:20000]
    return out


DB_UPLOAD = "_db_upload"      # per-analysis holding folder for picked database files; emptied once they are read


def upload_database_file(q, data):
    """One database file picked in the browser, copied into the analysis folder until it has been read.

    q: query fields a (analysis), name (file name), start ("1" on the first file of a batch, which empties the
    holding folder first). data: the file's bytes. Writes only <analysis>/_db_upload/<name>.
    Raises ValueError for a name that is not a plain database file name."""
    import nwn_database
    d = analysis_dir(q.get("a"))
    name = os.path.basename(str(q.get("name") or "")).strip()
    # Upload name check: the name must already be a bare file name (basename() changed nothing, so no folder part
    # like "..\x"), must not be hidden or "."/"..", and must have a database extension (.fpt/.cdx are the memo
    # and index files that go with an old .dbf).
    if not name or name != str(q.get("name") or "").strip() or name.startswith(".") or \
            not name.lower().endswith(nwn_database.DB_EXTS + (".fpt", ".cdx")):
        raise ValueError(f"'{name}' is not a database file (.sqlite3, .sqlite, .db, .dbf with its .fpt, .sql)")
    up = os.path.join(d, DB_UPLOAD)
    if q.get("start") == "1":
        shutil.rmtree(up, ignore_errors=True)
    os.makedirs(up, exist_ok=True)
    with open(os.path.join(up, name), "wb") as fh:
        fh.write(data)
    return dict(ok=True, name=name, bytes=len(data))


def load_database(b):
    """Read campaign database files into the analysis' snapshot (database.json), or remove it (b["clear"]).

    b: dict(a=analysis, path=folder or file) or dict(a, uploaded=True) for files sent to upload_database_file.
    nwn_database.read_path opens SQLite files through a temporary copy, so a live server database is never opened by
    SQLite or locked; .DBF/.FPT and .sql files are only read as plain files. Nothing is ever written to them.
    Returns database_view() for the page."""
    import nwn_database
    d = analysis_dir(b.get("a"))
    p = os.path.join(d, DB_SNAP)
    if b.get("clear"):
        if os.path.isfile(p):
            os.remove(p)
        return database_view(b.get("a"), {})
    if b.get("uploaded"):                       # files picked in the browser (see upload_database_file)
        up = os.path.join(d, DB_UPLOAD)
        if not os.path.isdir(up) or not os.listdir(up):
            raise ValueError("no database files were received - pick them again")
        try:
            snap = nwn_database.read_path(up)
        finally:
            shutil.rmtree(up, ignore_errors=True)   # the data is in the snapshot; the copies aren't kept
        snap["folder"] = "files you picked: " + ", ".join(f["file"] for f in snap["files"])[:400]
    else:
        path = field(b, "path", str, "")
        if not path.strip():
            raise ValueError("type or browse to the database folder (or pick the files)")
        snap = nwn_database.read_path(path)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, default=str)
    os.replace(p + ".tmp", p)
    return database_view(b.get("a"), snap)


def accept_issue(b):
    """Mark an issue 'accepted - by design' (or undo that). Keyed by category|node|label; `sig` is the detail with
    numbers masked, so an accepted issue comes back if what it says changes.

    b: dict(a, key, accepted, sig, note, category, label) from the Issues page; text fields are length-capped.
    Writes only <analysis>/accepted_issues.json (via .tmp + os.replace, under LOCK so two ticks cannot lose each
    other's change). Returns all accepted issues."""
    return accept_issues_many(dict(a=b.get("a"), accepted=b.get("accepted"), note=b.get("note"),
                                   items=[dict(key=b.get("key"), sig=b.get("sig"), category=b.get("category"),
                                               label=b.get("label"))]))


def _update_accepted(d, change):
    """Read accepted_issues.json of analysis folder d, let change(data) return the new contents, and write it back
    (.tmp + os.replace, under LOCK so two changes cannot lose each other's). Returns the new contents."""
    p = os.path.join(d, ACCEPTED)
    with LOCK:
        data = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}
        data = change(data)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, p)
    return data


def accept_issues_many(b):
    """Issues page: mark many issues 'accepted - by design' at once (Accept all shown…), or undo that.

    b: dict(a, items=[{key, sig, category, label}], accepted, note) - one note for all. Each key must look like
    category|node|label (at most 1000 characters) and there may be at most 100,000 items; a bad request is refused
    whole (ValueError -> 400) before anything is written. Writes only accepted_issues.json; returns its contents."""
    d = analysis_dir(b.get("a"))
    items = b.get("items")
    if not isinstance(items, list) or not items or len(items) > 100_000:
        raise ValueError("field 'items' must be a list of issues")
    for it in items:
        key = it.get("key") if isinstance(it, dict) else None
        if not isinstance(key, str) or not key or len(key) > 1000 or key.count("|") < 2:
            raise ValueError("bad issue key")
    plan = dict(accept=[], unaccept=[])
    for it in items:
        if b.get("accepted"):
            plan["accept"].append(dict(key=it["key"], sig=it.get("sig") or "", category=it.get("category") or "",
                                       label=it.get("label") or "", note=b.get("note") or ""))
        else:
            plan["unaccept"].append(dict(key=it["key"]))
    return _update_accepted(d, lambda data: nwn_sheets.apply_issue_plan(data, plan, time.strftime("%Y-%m-%d %H:%M")))


def api_issues_import(q, data):
    """Issues page: Import… a by-design spreadsheet. Without apply=1 only the preview (nwn_sheets.plan_issues);
    with it, the plan is worked out again from the file and stored. Writes only accepted_issues.json."""
    d = analysis_dir(q.get("a"))
    if len(data) > 20_000_000:
        raise ValueError("this file is too large (over 20 MB)")
    issues = nwn_sheets.all_issues(_json_file(os.path.join(d, "report.json"), {}),
                                   _json_file(os.path.join(d, "compile.json"), {}))
    if q.get("apply") != "1":
        return nwn_sheets.plan_issues(issues, _json_file(os.path.join(d, ACCEPTED), {}), data)
    out = {}

    def change(cur):
        out.update(nwn_sheets.plan_issues(issues, cur, data))
        return nwn_sheets.apply_issue_plan(cur, out, time.strftime("%Y-%m-%d %H:%M"))
    out["accepted"] = _update_accepted(d, change)
    return out


def api_plan_import(q, data):
    """Safe to delete / Duplicates pages: Import… a spreadsheet of delete or merge decisions. Returns the plan only
    (nwn_sheets.plan_deletions / plan_merges): those ticks live in the page, which applies it after its preview.
    kind: "delete" (allow_review=1 lets Review items be ticked) or "merge". Writes nothing."""
    d = analysis_dir(q.get("a"))
    if len(data) > 20_000_000:
        raise ValueError("this file is too large (over 20 MB)")
    report = _json_file(os.path.join(d, "report.json"), {})
    if q.get("kind") == "delete":
        return nwn_sheets.plan_deletions(report, data, q.get("allow_review") == "1")
    if q.get("kind") == "merge":
        return nwn_sheets.plan_merges(report, data)
    raise ValueError("kind must be delete or merge")


# ---------------------------------------------------------------- modules-folder housekeeping (nwn_housekeep)
def _job_progress(job):
    """A progress callback cb(pct, msg) for a job's worker: records % and message for /api/job, and raises
    JobStopped when Stop was pressed - the cooperative way non-analysis jobs stop."""
    def cb(pct, msg):
        if job.stop_requested:
            raise JobStopped()
        job.info["pct"] = pct
        job.info["msg"] = msg
    return cb


def housekeep_state():
    """The Modules folder page's state: folder to scan (last used, else <nwn_user>/modules), saved preferences, the
    last scan and the Move batches that can be undone. Read-only."""
    import nwn_housekeep
    s = settings()
    guess = os.path.join(s["nwn_user"], "modules") if s.get("nwn_user") else ""
    last = nwn_housekeep.last_scan(WORKSPACE)
    prefs = nwn_housekeep.load_prefs(WORKSPACE)
    return dict(folder=prefs.get("folder") or (last or {}).get("summary", {}).get("folder") or guess,
                prefs=prefs, last=last, batches=nwn_housekeep.list_batches(WORKSPACE))


def housekeep_post(action, b):
    """Modules folder actions, each started as a job (key "_housekeeping", so only one runs at a time):

        scan  list the folder's .mod / backup copies and suggest which to keep (only reads the folder;
              the scan is saved in the workspace)
        move  move the ticked files into an archive folder with a manifest - only after the user typed MOVE, only
              names from the last scan of this same folder; a file whose size or time changed since is skipped
        undo  move one batch back, chosen from the batches the toolkit recorded (no free-form path)
    Returns dict(job=id). Raises ValueError when a precondition fails. Files are moved, never deleted outright."""
    import nwn_housekeep
    if action == "scan":
        folder = field(b, "folder", str, "").strip()
        if not os.path.isdir(folder):
            raise ValueError(f"not a folder: {folder or '(empty)'}")
        keep_newest = max(1, min(50, int(field(b, "keep_newest", (int, float), 3))))
        keep_monthly = bool(b.get("keep_monthly"))
        keep_names = [x for x in field(b, "keep_names", list, []) if isinstance(x, str)][:500]
        nwn_housekeep.save_prefs(WORKSPACE, dict(folder=folder, keep_newest=keep_newest, keep_monthly=keep_monthly,
                                                 keep_names=keep_names))

        def fn(job):
            r = nwn_housekeep.scan(folder, WORKSPACE, keep_newest, keep_monthly, keep_names, _job_progress(job),
                                   lambda: job.stop_requested)
            print(f"{r['summary']['files']} module files scanned")
            return dict(summary=r["summary"])
        return dict(job=run_job("housekeep", fn, key="_housekeeping"))
    if action == "move":
        if field(b, "confirm", str, "").strip() != "MOVE":
            raise ValueError("type MOVE to confirm")
        last = nwn_housekeep.last_scan(WORKSPACE)
        folder = field(b, "folder", str, "")
        if not last or os.path.normcase(last["summary"]["folder"]) != os.path.normcase(os.path.abspath(folder)):
            raise ValueError("scan this folder first")
        known = {f["name"] for f in last["files"]}
        ticked = text_list(b, "names")
        names = [x for x in ticked if x in known]
        if len(names) != len(ticked):
            raise ValueError("some ticked files were not in the last scan - scan again")
        dest = field(b, "dest", str, "").strip() or None

        def fn(job):
            expected = {f["name"]: (f["size"], f["mtime"]) for f in last["files"]}
            batch = nwn_housekeep.move(last["summary"]["folder"], names, WORKSPACE, dest, _job_progress(job),
                                       lambda: job.stop_requested, expected)
            print(f"moved {batch['moved']} of {len(names)} to {batch['dest']}")
            return dict(moved=batch["moved"], moved_bytes=batch["moved_bytes"], dest=batch["dest"],
                        skipped=[e for e in batch["entries"] if e["status"] != "moved"])
        return dict(job=run_job("housekeep", fn, key="_housekeeping"))
    if action == "undo":
        man = field(b, "manifest", str, "")
        if not any(os.path.normcase(x["manifest"]) == os.path.normcase(man) for x in nwn_housekeep.list_batches(WORKSPACE)):
            raise ValueError("unknown batch")

        def fn(job):
            batch = nwn_housekeep.undo(man, WORKSPACE, _job_progress(job))
            back = sum(1 for e in batch["entries"] if e["status"].startswith("put back"))
            print(f"put back {back} file(s)")
            return dict(put_back=back, entries=batch["entries"])
        return dict(job=run_job("housekeep", fn, key="_housekeeping"))
    raise ValueError(f"unknown housekeeping action {action}")


def api_install_plan(q):
    """Build & audit page, "Add to game folders": what would be copied where (nwn_install.plan_install), for the
    confirmation dialog. allow_fail=1 only after the page's second confirmation for a FAIL verdict. Read-only;
    a refusal (no user folder set, a target name taken, FAIL, a changed build) comes back as a 400 with the reason."""
    import nwn_install
    return nwn_install.plan_install(analysis_dir(q.get("a")), settings()["nwn_user"], allow_fail=q.get("allow_fail") == "1")


PALETTE_LOCK = threading.Lock()     # one palette build at a time: two page loads must not both write palette.json


def palette_data(d):
    """The Blueprints page data of analysis folder d (nwn_palette): palette.json when it is newer than the index and
    of the current version, otherwise built from the index and saved (so analyses made before the page existed need
    no re-analysis). Writes only palette.json and reports/palette.csv in d; never the module or haks."""
    p = os.path.join(d, "palette.json")
    with PALETTE_LOCK:
        try:
            if os.path.getmtime(p) >= os.path.getmtime(os.path.join(d, "index.sqlite")):
                data = _json_file(p, None)
                if isinstance(data, dict) and data.get("version") == nwn_palette.VERSION:
                    return data
        except (OSError, ValueError):
            pass                         # missing or unreadable: build it again below
        return nwn_palette.build_palette(d)


def api_palette_move(b):
    """Blueprints page: make blueprints wait to move to another palette category (applied by the next build).
    Fields: a, ext (blueprint type, e.g. "uti"), resrefs (list of names), to (category number). Returns
    dict(moves, refused) (nwn_palette.set_moves). Writes only palette_moves.json in the analysis folder."""
    d = analysis_dir(field(b, "a"))
    resrefs = field(b, "resrefs", list)
    if not all(isinstance(x, str) for x in resrefs):
        raise ValueError("field 'resrefs' must be a list of blueprint names")
    pal = palette_data(d)                    # takes PALETTE_LOCK itself, so it is read before the lock below
    with PALETTE_LOCK:
        return nwn_palette.set_moves(d, field(b, "ext"), resrefs, field(b, "to", int), pal)


def api_palette_import(q, data):
    """Blueprints page: Import moves… - a spreadsheet (CSV) of palette moves. apply=1 stores them (nwn_palette.
    apply_import, which works the plan out again from the file); otherwise only the preview (plan_import) is returned
    and nothing is written. a: the analysis. Writes only palette_moves.json; a file over 20 MB is refused."""
    d = analysis_dir(q.get("a"))
    if len(data) > 20_000_000:
        raise ValueError("this file is too large for a list of moves (over 20 MB)")
    pal = palette_data(d)                    # takes PALETTE_LOCK itself, so it is read before the lock below
    ctx = nwn_bpchanges.load_context(d, pal)
    with PALETTE_LOCK:
        # one file, two kinds of change: move_to (palette moves) and new_... columns (field changes)
        fields = nwn_bpchanges.plan_csv(ctx, data)
        if q.get("apply") == "1":
            out = nwn_palette.apply_import(d, pal, data)
            out["changes"] = nwn_bpchanges.save_plan(d, fields)
        else:
            out = nwn_palette.plan_import(pal, nwn_palette.load_moves(d), data)
        out["fields"] = fields
        return out


def _bpchange_plan(b):
    """The analysis folder and the plan for a Change fields… request (a, ext, resrefs, values {field: text})."""
    d = analysis_dir(field(b, "a"))
    resrefs, values = field(b, "resrefs", list), field(b, "values", dict)
    if not all(isinstance(x, str) for x in resrefs) or not all(isinstance(v, (str, int)) for v in values.values()):
        raise ValueError("fields 'resrefs' (names) and 'values' (texts) are needed")
    ctx = nwn_bpchanges.load_context(d, palette_data(d))
    return d, nwn_bpchanges.plan_values(ctx, field(b, "ext"), resrefs, values)


def api_bpchange_plan(b):
    """Blueprints page: Change fields… preview - what would change and what is refused. Writes nothing."""
    return _bpchange_plan(b)[1]


def api_bpchange_save(b):
    """Blueprints page: Change fields… Save - planned again here (never trusting the page's preview) and stored in
    blueprint_changes.json. Returns dict(changes=all waiting changes, saved, refused)."""
    d, plan = _bpchange_plan(b)
    with PALETTE_LOCK:
        return dict(changes=nwn_bpchanges.save_plan(d, plan), saved=len(plan["changes"]), refused=plan["refused"])


def api_bpchange_undo(b):
    """Blueprints page: cancel waiting field changes - all (no ext), a type's, some blueprints' or one field of them.
    Returns dict(changes=what still waits). Writes only blueprint_changes.json."""
    d = analysis_dir(field(b, "a"))
    resrefs = field(b, "resrefs", list, None)
    with PALETTE_LOCK:
        return dict(changes=nwn_bpchanges.undo_changes(d, field(b, "ext", str, None), resrefs,
                                                        field(b, "field", str, None)))


def api_palette_move_undo(b):
    """Blueprints page: cancel waiting moves - the given resrefs of type ext, every move of type ext (no resrefs), or
    all of them (no ext). Returns dict(moves=what still waits). Writes only palette_moves.json."""
    d = analysis_dir(field(b, "a"))
    resrefs = field(b, "resrefs", list, None)
    if resrefs is not None and not all(isinstance(x, str) for x in resrefs):
        raise ValueError("field 'resrefs' must be a list of blueprint names")
    with PALETTE_LOCK:
        return dict(moves=nwn_palette.undo_moves(d, field(b, "ext", str, None), resrefs))


def api_install_state(q):
    """Build & audit page: the record of the last Add to game folders (installed.json), or {}. Read-only."""
    import nwn_install
    return nwn_install.install_state(analysis_dir(q.get("a")))


def api_install_add(b):
    """Build & audit page: "Add to game folders", as a job on the analysis (so it can't overlap a build).

    Fields: a (analysis), confirm (must be the typed word ADD), plan_id (from /api/install/plan - the plan is worked
    out again here and must be the one the user saw), allow_fail (bool) with confirm_fail "yes" (the page's second
    confirmation for a FAIL verdict). Returns dict(job=id). Raises ValueError (a 400) when a check fails; the copy
    itself never replaces a file (nwn_install.install)."""
    import nwn_install
    a = field(b, "a")
    if field(b, "confirm", str, "").strip() != "ADD":
        raise ValueError("type ADD to confirm - nothing was copied")
    allow_fail = field(b, "allow_fail", bool, False)
    if allow_fail and field(b, "confirm_fail", str, "") != "yes":
        raise ValueError("adding a build whose audit FAILED needs the second confirmation - nothing was copied")
    plan = nwn_install.plan_install(analysis_dir(a), settings()["nwn_user"], allow_fail=allow_fail)
    if plan["plan_id"] != field(b, "plan_id", str, ""):
        raise ValueError("the build or your game folders changed since the plan was shown - press Add to game "
                         "folders again to see the new plan. Nothing was copied.")

    def fn(job):
        rec = nwn_install.install(plan, _job_progress(job))
        for f in rec["files"]:
            print(f"added {f['target']}")
        return dict(status=rec["status"], added=[f["target"] for f in rec["files"] if f["status"] == "placed"])
    return dict(job=run_job("install", fn, key=a))


def api_install_undo(b):
    """Build & audit page: Undo the last Add to game folders, as a job on the analysis. Removes only the files that
    add placed and that are still unchanged (nwn_install.undo_install); everything else is left and listed.
    Field: a (analysis). Returns dict(job=id)."""
    import nwn_install
    a = field(b, "a")
    d = analysis_dir(a)

    def fn(job):
        rec = nwn_install.undo_install(d)
        for f in rec["files"]:
            print(f"{os.path.basename(f['target'])}: {f.get('undo', '')}")
        return dict(status=rec["status"], files=[dict(target=f["target"], undo=f.get("undo")) for f in rec["files"]])
    return dict(job=run_job("install", fn, key=a))


def list_analyses():
    """Every analysis folder in the workspace for the Modules page: complete, incomplete (with whether a job is
    running on it) or archived. Read-only; never raises for a damaged report (its summary is just left empty)."""
    out = []
    if not os.path.isdir(WORKSPACE):
        return out
    for nm in sorted(os.listdir(WORKSPACE)):
        d = os.path.join(WORKSPACE, nm)
        if not os.path.isdir(d) or not re.fullmatch(r"[A-Za-z0-9_\-]+", nm):
            continue
        status = analysis_status(d)
        if status == "incomplete":
            info = {}
            try:
                with open(os.path.join(d, INCOMPLETE), encoding="utf-8") as fh:
                    info = json.load(fh)
            except (OSError, ValueError):
                pass
            j = running_job_for(nm)
            out.append(dict(name=nm, module_name=nm, module_path=info.get("module"), indexed_at=info.get("started"),
                            incomplete=True, running=bool(j), job=j.id if j else None, has_build=False))
            continue
        if nwn_archive.is_archived(d):
            info = nwn_archive.stub(d) or {}
            out.append(dict(name=nm, module_name=info.get("module_name") or nm, module_path=info.get("module_path"),
                            indexed_at=info.get("indexed_at"), archived=True, archived_at=info.get("archived_at"),
                            original_bytes=info.get("original_bytes"), archive_bytes=info.get("archive_bytes"),
                            has_build=os.path.isfile(os.path.join(d, "build", "audit.json"))))
            continue
        p = os.path.join(d, "report.json")
        if os.path.isfile(p):
            try:
                # report.json can be large, so only its start is read. "summary" is the first key that
                # nwn_analysis writes; the regex takes it from "{" up to its "indexed_at" value, and the "}" added
                # below closes it, e.g. '"summary": {"module_name": "x", ..., "indexed_at": "2026-09-30 10:00"'.
                # Keys after indexed_at are cut off; anything that fails to parse leaves the summary empty.
                with open(p, encoding="utf-8") as fh:
                    head = fh.read(4000)
                m = re.search(r'"summary":\s*(\{.*?"indexed_at":\s*"[^"]*")', head)
                summ = json.loads(m.group(1) + "}") if m else {}
            except Exception:  # noqa
                summ = {}
            out.append(dict(name=nm, module_name=summ.get("module_name", nm), module_path=summ.get("module_path"),
                            indexed_at=summ.get("indexed_at"),
                            has_build=os.path.isfile(os.path.join(WORKSPACE, nm, "build", "audit.json"))))
    return out


# ------------------------------------------------------------------ background jobs
class _ThreadStdout:
    """stdout that routes each worker thread's prints to its own job; everything else to the console.

    The toolkit's modules report progress with print(). Replacing sys.stdout once (below) lets a job's worker
    thread capture that output for the page without changing those modules. Threads a job starts itself are not
    in routes, so their prints go to the console."""
    def __init__(self, real):
        self.real = real
        self.routes = {}            # thread id -> Job; each worker adds and removes only its own entry

    def write(self, txt):
        j = self.routes.get(threading.get_ident())
        return j.write(txt) if j else self.real.write(txt)

    def flush(self):
        self.real.flush()


# Guarded so loading this module a second time (e.g. importlib.reload) does not wrap the wrapper.
if not isinstance(sys.stdout, _ThreadStdout):
    sys.stdout = _ThreadStdout(sys.stdout)


class Job:
    """One background task as the page sees it: kind ("analyse", "build", "compile", "archive", "diff",
    "housekeep", "hakbuild"), key (what it locks, usually the analysis name), status ("running", "done",
    "stopped", "error"), log lines, result and progress. Only the job's own worker thread changes status, lines and
    result; HTTP threads read them, and set stop_requested."""
    def __init__(self, kind, key=None):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.key = key
        self.status = "running"
        self.lines = []
        self.result = None
        self.started = time.time()
        self.proc = None            # child process (analysis) - Stop terminates it
        self.tracker = None         # nwn_progress.Tracker for % / time left
        self.stop_requested = False
        self.info = {}

    def write(self, s):
        """File-like write used by _ThreadStdout: keeps each non-blank line of s in the job's log."""
        for line in s.splitlines():
            if line.strip():
                self.lines.append(line)
        return len(s)

    def flush(self):
        pass


class JobStopped(Exception):
    """Raised inside a job's worker when Stop was pressed; run_job records the job as "stopped", not "error"."""
    pass


def stop_job(job_id):
    """Stop a running job at once. Analyses run in their own process, which is terminated (killed on Windows).

    Other jobs only get stop_requested set and stop at their next progress check. Waits up to 5 seconds for the
    child process, then kills it. Returns dict(status). Raises ValueError for an unknown job id."""
    j = JOBS.get(job_id)
    if not j:
        raise ValueError("no such job")
    if j.status != "running":
        return dict(status=j.status)
    j.stop_requested = True
    p = j.proc
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return dict(status="stopping")


def prune_jobs(now=None):
    """Forget finished jobs (done / stopped / error) older than JOB_KEEP_S, so a long-running dashboard does not
    keep every job's log for ever. Called under LOCK by run_job. A running job is never dropped, whatever its age.
    now: the time to measure from (tests); default time.time(). Returns the number of jobs dropped."""
    now = time.time() if now is None else now
    old = [jid for jid, j in JOBS.items() if j.status != "running" and now - j.started > JOB_KEEP_S]
    for jid in old:
        del JOBS[jid]
    return len(old)


def run_job(kind, fn, key=None):
    """Run fn in a thread. `key` (analysis name) serialises jobs on the same analysis.

    kind: label shown on the page. fn(job): the work; its return value becomes job.result. Returns the new job id
    at once. Raises ValueError when a job with the same key is still running. fn's exceptions never escape: they
    end the job as "stopped" (JobStopped / InterruptedError) or "error" with the message and the last traceback
    lines in its log."""
    # Check and register under one lock, so two requests at the same moment cannot both start a job on one key.
    with LOCK:
        prune_jobs()
        if key and any(j.status == "running" and (j.key or "").lower() == key.lower() for j in JOBS.values()):
            raise ValueError(f"another job is still running on '{key}' - wait for it to finish")
        job = Job(kind, key)
        JOBS[job.id] = job

    def target():
        """The worker thread: route this thread's prints to the job, run fn, record how it ended."""
        sys.stdout.routes[threading.get_ident()] = job
        try:
            job.result = fn(job)
            job.status = "done"
        except (JobStopped, InterruptedError) as ex:
            if job.kind == "analyse":
                job.lines.append("Stopped by you. Partial results are marked incomplete - press Clear to remove them.")
            else:
                job.lines.append("Stopped by you." + (f" {ex}" if str(ex) else " Nothing was left half-done."))
            job.status = "stopped"
        except MemoryError:
            job.lines.append("ERROR: ran out of memory - the file may be damaged or built to exhaust memory")
            job.status = "error"
        except Exception as ex:  # noqa - the job ends as "error"; the server and other jobs carry on
            job.lines.append("ERROR: " + (str(ex) or type(ex).__name__))
            # A ValueError is a refusal or a user mistake with a plain message (a missing file...): a traceback
            # would only bury it. Anything else is a bug, and the last lines help a bug report.
            if not isinstance(ex, ValueError):
                job.lines += traceback.format_exc().splitlines()[-6:]
            job.status = "error"
        finally:
            sys.stdout.routes.pop(threading.get_ident(), None)
    # daemon: a job thread never keeps Python alive after the server stops (an analysis child is ended separately
    # by stop_all_children).
    threading.Thread(target=target, daemon=True).start()
    return job.id


def analysis_name_for(module, name=None):
    """Unique, stable analysis name: folder/file name, plus a path hash when the name is generic or taken.

    module: the module's path. name: the name the user typed (optional). The same module path always gives the same
    name, so re-analysing reuses its folder. Reads the existing index (if any) only to compare module paths."""
    base = safe_name(name or os.path.splitext(os.path.basename(os.path.normpath(module)))[0])
    # SHA-1 here is only a short, stable label for the path (not security): 6 hex characters tell folders apart.
    tag = hashlib.sha1(os.path.abspath(module).lower().encode()).hexdigest()[:6]
    # temp0/temp1 are the folders the NWN toolset unpacks the module it has open into, so many different modules
    # share those names; the others are equally generic folder names.
    if base.lower() in ("temp0", "temp1", "src", "module", "modules") and not name:
        return f"{base}_{tag}"
    d = os.path.join(WORKSPACE, base)
    if os.path.isfile(os.path.join(d, "index.sqlite")):
        try:
            db = n.sqlite_ro(os.path.join(d, "index.sqlite"))      # read-only: only compares the stored path
            prev = db.execute("SELECT value FROM meta WHERE key='module_path'").fetchone()
            db.close()
            if prev and os.path.abspath(prev[0]).lower() != os.path.abspath(module).lower():
                return f"{base}_{tag}"
        except sqlite3.Error:
            pass
    return base


def analyse_command(module, haks, tlk, nm, s):
    """The nwn_analyse.py command line as an argument list (run without a shell, so paths need no quoting and
    cannot inject commands). haks: hak paths; tlk: talk table path or ""; nm: analysis name; s: settings().
    --progress makes the child print machine-readable progress lines (nwn_progress.parse_line)."""
    cmd = [sys.executable, "-u", os.path.join(HERE, "nwn_analyse.py"), module, "--name", nm, "--progress"]
    for h in haks:
        cmd += ["--hak", h]
    if tlk:
        cmd += ["--tlk", tlk]
    if s.get("nwn_root"):
        cmd += ["--nwn-root", s["nwn_root"]]
    if s.get("nwn_user"):
        cmd += ["--nwn-user", s["nwn_user"]]
    return cmd


def job_analyse(module, haks, tlk, name):
    """Index + analyse in a separate Python process, so Stop can end it instantly and its memory is freed.

    Returns the job function for run_job. Reads the module, haks and tlk (never writes them); writes the analysis
    folder in the workspace (through the child), timings.json and last_folder in settings.json."""
    def fn(job):
        if not os.path.exists(module):
            raise ValueError(f"Not found: {module}")
        nm = analysis_name_for(module, name)
        out = os.path.join(WORKSPACE, nm)
        job.info = dict(analysis=nm, module=module)
        with LOCK:  # release the cached database first (Windows cannot delete an open file)
            release_analysis(nm)
        os.makedirs(out, exist_ok=True)
        # Marker first: if the dashboard or the child dies, the half-made analysis is known as incomplete.
        with open(os.path.join(out, INCOMPLETE), "w", encoding="utf-8") as fh:
            json.dump(dict(module=os.path.abspath(module), started=time.strftime("%Y-%m-%d %H:%M:%S")), fh)
        s = settings()
        job.tracker = nwn_progress.Tracker(nwn_progress.load_history(WORKSPACE))
        print(f"Analysing {module}")
        if job.stop_requested:
            raise JobStopped()
        # Same workspace as ours; UTF-8 and unbuffered output so progress lines arrive at once and intact;
        # NWN_PARENT_PID lets the child exit by itself if this dashboard goes away (nwn_progress.watch_parent).
        env = dict(os.environ, NWN_WORKSPACE=WORKSPACE, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   NWN_PARENT_PID=str(os.getpid()))
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW: no console window pops up
        job.proc = subprocess.Popen(analyse_command(module, haks, tlk, nm, s), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=HERE,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        if job.stop_requested:        # Stop pressed while the process was being started
            job.proc.terminate()
        for line in job.proc.stdout:
            line = line.rstrip("\r\n")
            ev = nwn_progress.parse_line(line)
            if ev is not None:
                job.tracker.feed(ev)
            elif line.strip():
                job.lines.append(line)
                if len(job.lines) > 5000:            # cap memory on a long run; the page shows the last 400
                    del job.lines[:1000]
        rc = job.proc.wait()
        if rc != 0:
            if job.stop_requested:
                raise JobStopped()
            raise RuntimeError(f"the analysis process stopped with exit code {rc} - see the log above")
        # rc == 0: the run finished (even if Stop was pressed in its last moment); nwn_analyse removed the marker
        if os.path.exists(os.path.join(out, INCOMPLETE)):
            os.remove(os.path.join(out, INCOMPLETE))
        t = job.tracker.timings()
        if t["units"] and t["index_s"]:
            nwn_progress.record_run(WORKSPACE, dict(module=os.path.basename(os.path.normpath(module)), **t))
        release_analysis(nm)
        s = settings(); s["last_folder"] = os.path.dirname(os.path.normpath(module)); save_settings(s)
        print("Done.")
        return dict(name=nm)
    return fn


BUILD_TIMES = "build_timings.json"      # per analysis: how long each build step took last time (for time left)


def _build_estimates(name, d):
    """The module's last analysis timings (the audit repeats most of that work) and its last build's step times."""
    analysis_s = None
    try:
        mod = os.path.basename(os.path.normpath(report_parts(name)["summary"]["module_path"]))
        runs = [r for r in nwn_progress.load_history(WORKSPACE) if r.get("module") == mod]
        if runs:
            analysis_s = runs[-1]
    except (OSError, ValueError, KeyError):     # no estimate from a damaged or older report
        pass
    try:
        prior = json.load(open(os.path.join(WORKSPACE, BUILD_TIMES), encoding="utf-8")).get(name)
    except (OSError, ValueError):
        prior = None
    return analysis_s, prior


def job_build(name, plan, out_name):
    """Job function: build the clean module from plan (the ticked deletions/merges, lean_haks ...) into
    <analysis>/build and run the deterministic audit (nwn_build.build). out_name: the clean module's name or None
    for the default. Never writes the original module or haks; remembers step times in build_timings.json."""
    def fn(job):
        d = analysis_dir(name)
        s = settings()
        analysis_s, prior = _build_estimates(name, d)
        tr = nwn_progress.BuildTracker(analysis_s, prior, lean=bool(plan.get("lean_haks")))
        job.tracker = tr
        nwn_progress.set_hook(tr.event)          # the audit's re-read / re-analysis reports into the same tracker
        print("Building clean module ...")
        try:
            log = nwn_build.build(d, plan, os.path.join(d, "build"), out_name or None, run_audit=True, verbose=True,
                                  audit_kwargs=dict(compiler=s["compiler"] or None, nwn_root=s["nwn_root"] or None,
                                                    nwn_user=s["nwn_user"] or None),
                                  stop=lambda: job.stop_requested, progress=tr)
        finally:
            nwn_progress.set_hook(None)
        tr.finish()
        try:                                     # remember step times: the next build's estimate starts from them
            p_ = os.path.join(WORKSPACE, BUILD_TIMES)
            allt = json.load(open(p_, encoding="utf-8")) if os.path.isfile(p_) else {}
            allt[name] = {k: v for k, v in tr.timings().items() if k != "snapshot"}
            with open(p_ + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(allt, fh, indent=1)
            os.replace(p_ + ".tmp", p_)
        except (OSError, ValueError):
            pass
        print(f"Audit verdict: {log['audit']['verdict']}")
        return dict(summary=log["summary"], verdict=log["audit"]["verdict"], renamed_haks=log.get("renamed_haks"),
                    unchanged_haks=log.get("unchanged_haks"), mod_file=log.get("mod_file"))
    return fn


def job_compile(name):
    """Job function: compile every script of the analysed module with the official compiler (nwn_compile) and save
    the result as <analysis>/compile.json. The module is only read: nwn_compile works on temporary copies."""
    def fn(job):
        d = analysis_dir(name)
        s = settings()
        mp = report_parts(name)["summary"]["module_path"]
        print("Compiling all scripts with the official compiler ...")
        r = nwn_compile.compile_module(mp, s["compiler"] or None, s["nwn_root"] or None, nwn_user=s["nwn_user"] or None)
        # .tmp then os.replace, like the other JSON files: a crash part-way never leaves a broken compile.json
        p = os.path.join(d, "compile.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=1)
        os.replace(p + ".tmp", p)
        print(f"{r['status']}: {r.get('ok', 0)} ok, {r.get('failed', 0)} failed. {r.get('reason', '')}")
        return dict(status=r["status"], ok=r.get("ok"), failed=r.get("failed"), reason=r.get("reason"))
    return fn


# ------------------------------------------------------------------ data helpers
def compile_one(analysis, relpath):
    """Compile a single (edited/new) script with the official compiler against the module's scripts.

    analysis: the analysis FOLDER (not its name). relpath: the script's file name ("foo.nss"). Every script source
    from the index, overlaid with the edits, is written to a fresh temp folder so includes resolve; the folder is
    removed afterwards. Returns dict(status, errors for that script, reason). Writes nothing in the workspace.
    (nwn_compile compiles every script in the temp folder; only relpath's errors are returned.)"""
    s = settings()
    comp = nwn_compile.find_compiler(s["compiler"] or None)
    if not comp:
        return dict(status="skipped", reason="official compiler not installed (see Modules page)")
    g, db = get_graph(name_of(analysis))
    with LOCK:
        rows = db.execute("SELECT name, source FROM scripts").fetchall()
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    work = tempfile.mkdtemp(prefix="nwn_edit_comp_")
    try:
        for nm, src in rows:  # includes must resolve
            with open(os.path.join(work, nm + ".nss"), "wb") as fh:
                fh.write(n.encode_text(src))
        for fn in os.listdir(os.path.join(analysis, "edits")):
            if fn.endswith(".nss"):
                shutil.copy(os.path.join(analysis, "edits", fn), os.path.join(work, fn))
        r = nwn_compile.compile_module(work, comp, s["nwn_root"] or meta.get("nwn_root") or None,
                                       nwn_user=s["nwn_user"] or meta.get("nwn_user") or None)
        errs = r.get("results", {}).get(os.path.basename(relpath).lower()[:-4], [])
        return dict(status=r["status"], errors=errs, reason=r.get("reason"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def name_of(analysis_path):
    """The analysis name of an analysis folder path (its last part)."""
    return os.path.basename(analysis_path.rstrip("/\\"))


def get_graph(name):
    """(Graph, sqlite3.Connection) for an analysis, opened once and cached in GRAPHS until release_analysis, until
    index.sqlite changes, or until two other analyses have been opened since (GRAPHS keeps the two most recent).

    The one connection is shared by all request threads (check_same_thread=False); callers run their queries
    while holding LOCK, because a sqlite3 connection must not be used by two threads at once.
    Opened read-only (nwnlib.sqlite_ro): the dashboard only reads the index, and a read-only open can never create
    an empty index.sqlite in a folder that has none."""
    def open_graph(nm):
        db = n.sqlite_ro(os.path.join(analysis_dir(nm), "index.sqlite"), check_same_thread=False)
        return nwn_analysis.Graph(db), db
    return GRAPHS.get(name, open_graph)


def impact_detail(name, node):
    """The impact chain of one node (level, reasons, affected list with paths, placed copies, uses), worked out now
    from the in-memory graph with the analysis' live set, so the level is the one the report shows. Raises
    ValueError for a node the graph does not have. Read-only."""
    g, _db = get_graph(name)
    if node not in g.nodes:
        raise ValueError("unknown node")
    _loaded, server_values = nwn_analysis.server_config_values(analysis_dir(name))
    return g.impact(node, detail=True, live=g.live(server_values))


def find_objects(name, q, limit=60):
    """Placed objects / blueprints / scripts whose name, tag or template contains q (for big modules the browser
    no longer holds every instance).

    Returns dict(hits=[{node, label, breadcrumb}]), at most limit; fewer than 2 characters finds nothing (too many
    hits). Read-only. q goes into SQL only as a bound parameter (?), never into the SQL text; % and _ in q act as
    LIKE wildcards."""
    g, db = get_graph(name)
    q = (q or "").strip().lower()
    if len(q) < 2:
        return dict(hits=[])
    like = f"%{q}%"
    hits, seen = [], set()
    with LOCK:
        rows = db.execute("SELECT node FROM objects WHERE lower(name) LIKE ? OR lower(tag) LIKE ? OR lower(template) LIKE ? "
                          "LIMIT ?", (like, like, like, limit * 3)).fetchall()
        rows += db.execute("SELECT node FROM nodes WHERE node NOT LIKE 'inst:%' AND (lower(label) LIKE ? OR lower(name) LIKE ?) "
                           "LIMIT ?", (like, like, limit)).fetchall()
    for (nd,) in rows:
        if nd in seen or nd not in g.nodes:
            continue
        seen.add(nd)
        hits.append(dict(node=nd, label=g.label(nd), breadcrumb=g.breadcrumb(nd)))
        if len(hits) >= limit:
            break
    return dict(hits=hits)


def find_files(name, q, limit=200):
    """Files in the module or any hak whose name contains q.

    Returns dict(files=[{relpath, kind, size, node, source}], more): module files first (source priority), at most
    limit; more is True when there were more. Read-only; q is a bound SQL parameter."""
    g, db = get_graph(name)
    q = (q or "").strip().lower()
    if len(q) < 2:
        return dict(files=[], more=False)
    with LOCK:
        rows = db.execute("SELECT f.relpath, f.kind, f.size, f.node, s.kind, s.path FROM files f JOIN sources s ON s.id=f.source_id "
                          "WHERE lower(f.relpath) LIKE ? ORDER BY s.priority, f.relpath LIMIT ?", (f"%{q}%", limit + 1)).fetchall()
    files = [dict(relpath=r[0], kind=r[1], size=r[2], node=r[3],
                  source=r[4] if r[4] == "module" else os.path.basename(r[5])) for r in rows[:limit]]
    return dict(files=files, more=len(rows) > limit)


def node_details(name, node):
    """Everything the detail panel shows for one node.

    name: analysis name; node: graph node id ("script:foo", "bp:bar.uti", "inst:...", "dlg:...", "model:..."). Returns
    the node's impact, who uses it / what it uses (300 each at most), its files (with edit state, a SHA-256 of the
    current bytes for the editor's "changed since you opened it" check, GFF as JSON, text) and type-specific extras.
    Read-only. Raises ValueError for an unknown node. If the original module cannot be reached (an analysis copied
    from another PC) the panel still works without the raw file views (module_unreachable says why)."""
    g, db = get_graph(name)
    d = analysis_dir(name)
    nd = g.nodes.get(node)
    if not nd:
        raise ValueError("unknown node")
    out = dict(node=node, label=nd["label"], type=nd["type"], in_module=nd["in_module"],
               breadcrumb=g.breadcrumb(node), impact=impact_detail(name, node),
               used_by=[dict(node=s, label=g.label(s), kind=k, via=v) for s, k, v in g.rev.get(node, [])][:300],
               uses=[dict(node=t, label=g.label(t), kind=k, via=v) for t, k, v in g.fwd.get(node, [])][:300])
    with LOCK:
        files = db.execute("SELECT relpath, ext, size, status, error FROM files WHERE node=?", (node,)).fetchall()
        out["files"] = [dict(relpath=r[0], ext=r[1], size=r[2], status=r[3], error=r[4]) for r in files]
        if node.startswith("script:"):
            row = db.execute("SELECT source FROM scripts WHERE name=?", (node[7:],)).fetchone()
            out["source"] = row[0] if row else None
        if node.startswith("inst:"):
            row = db.execute("SELECT class, template, tag, name, area, x, y, z FROM objects WHERE node=?", (node,)).fetchone()
            if row:
                out["object"] = dict(zip(("class", "template", "tag", "name", "area", "x", "y", "z"), row))
        if node.startswith("model:"):
            row = db.execute("SELECT format, supermodel, textures FROM models WHERE name=?", (node[6:],)).fetchone()
            if row:
                out["model"] = dict(format=row[0], supermodel=row[1], textures=[t for t in (row[2] or "").split(",") if t])
    if node.startswith("dlg:"):
        # node comes from the graph (checked above), not straight from the request, so node[4:] is a resref
        p = os.path.join(d, "conversations", node[4:] + ".md")
        if os.path.isfile(p):
            out["conversation_md"] = open(p, encoding="utf-8").read()
    edits = nwn_edit.list_edits(d)
    for f in out["files"]:
        fl = os.path.basename(f["relpath"]).lower()
        f["edited"] = fl in edits
        f["editable"] = f["ext"] in nwn_edit.TEXT_EXTS or f["ext"] in n.GFF_EXTENSIONS or f["ext"] == "mdl"
        try:
            raw, is_edit = nwn_edit.current_bytes(d, f["relpath"], lambda rp: read_module_file(name, rp))
        except (OSError, ValueError) as ex:   # analysis copied from another machine: the module itself is not here
            raw, is_edit = None, False
            out["module_unreachable"] = f"original module not reachable from this machine ({ex.__class__.__name__}); " \
                                        "raw file views, previews and edits need the module path in Settings"
        if raw is not None:
            f["sha"] = hashlib.sha256(raw).hexdigest()    # the editor sends it back: saving refuses if it changed since
        if f["ext"] in n.GFF_EXTENSIONS:
            if is_edit:
                try:
                    f["json"] = json.dumps(n.gff_to_json(n.read_gff(raw)), indent=1, ensure_ascii=False)
                except n.GffError as ex:
                    f["json"] = f"(edited file cannot be read: {ex})"
            else:
                jp = os.path.join(d, "json", f"{fl}.json")
                if os.path.isfile(jp) and os.path.getsize(jp) < 3_000_000:     # bigger ones would stall the page
                    f["json"] = open(jp, encoding="utf-8").read()
        elif f["ext"] == "nss" and is_edit:
            out["source"] = n.decode_text(raw)
        if f["ext"] in nwn_edit.TEXT_EXTS - {"nss"} or (f["ext"] == "mdl" and out.get("model", {}).get("format") == "ascii"):
            if raw is not None and len(raw) < 2_000_000:
                f["text"] = n.decode_text(raw)
        if f["ext"] in nwn_analysis.IMAGE_EXTS:
            f["preview"] = True
    descs = os.path.join(d, "descriptions.json")
    if node.startswith("script:") and os.path.isfile(descs):
        out["description"] = json.load(open(descs, encoding="utf-8")).get(node[7:])
    return out


def read_module_file(name, relpath, source=None):
    """The original bytes of one file of the analysed module or one of its haks, or None if the index has no such
    relpath. name: analysis name; relpath: the file's path as stored in the index's files table. source: a source
    path from the index's sources table, to read that source's copy when several have the file (None: any copy).

    Read-only: a .mod/.hak is opened with nwnlib.Erf and closed again, a folder module's file is read through
    nwn_refactor.read_folder_file, which returns None for a file outside the module folder or a symbolic link.
    Only relpaths listed in the index can be read, which is the main defence against reading arbitrary files.
    May raise OSError when the module itself is not reachable (e.g. an analysis copied from another PC)."""
    import nwn_refactor
    analysis_dir(name)
    with LOCK:
        g, db = get_graph(name)
        if source is None:
            row = db.execute("SELECT s.path, s.kind FROM files f JOIN sources s ON s.id=f.source_id WHERE f.relpath=?",
                             (relpath,)).fetchone()
        else:
            row = db.execute("SELECT s.path, s.kind FROM files f JOIN sources s ON s.id=f.source_id "
                             "WHERE f.relpath=? AND s.path=?", (relpath, source)).fetchone()
    if not row:
        return None
    src, _ = row
    if os.path.isdir(src):
        return nwn_refactor.read_folder_file(src, relpath)
    erf = n.Erf(src)
    try:                                   # close it: an open handle would keep the .mod locked on Windows
        for e in erf.entries:
            if e.filename == relpath:
                return erf.read(e)
    finally:
        erf.close()
    return None


def compiled_code(name, script):
    """What the game actually runs for a script: the .ncs checked (nwn_ncs) and listed by nwn_asm (tools folder).

    name: analysis name; script: script resref. Uses the copy the game runs (nwnlib.source_rank: a hak's .ncs wins
    over the module's).
    Returns dict(found, ok, problem, strings (first 200), listing (first 400000 characters) or listing_error ...),
    or dict(error) for a bad name. The disassembler only ever sees a temporary copy, deleted afterwards; the module
    is never touched. Never raises for a damaged .ncs (that is reported in ok / problem)."""
    import nwn_ncs
    script = (script or "").lower()
    # NWN resource names (resrefs) are at most 16 characters. This also keeps the name safe to use in a file path.
    if not re.fullmatch(r"[a-z0-9_]{1,16}", script):
        return dict(error="not a script name")
    with LOCK:
        _g, db = get_graph(name)
        # the copy the game runs: the same precedence as everywhere else (haks in module.ifo order, then the module,
        # then the override folder - nwnlib.source_rank)
        row = db.execute("SELECT f.relpath, s.kind, s.path FROM files f JOIN sources s ON s.id=f.source_id "
                         f"WHERE f.resref=? AND f.ext='ncs' ORDER BY {n.source_rank_sql()}, s.id LIMIT 1",
                         (script,)).fetchone()
    if not row:
        return dict(script=script, found=False,
                    error=f"there is no compiled {script}.ncs in the module or its haks - the game can't run this "
                          "script until it is compiled")
    relpath, kind, src = row
    data = read_module_file(name, relpath, src)        # that source's copy, not whichever the index lists first
    if data is None:
        return dict(script=script, found=False, error=f"{relpath} could not be read from {src}")
    info = nwn_ncs.parse(data)
    out = dict(script=script, found=True, where=f"{os.path.basename(src)} ({kind})", bytes=len(data),
               ok=info["ok"], problem=info["problem"], instructions=info["instructions"],
               strings=info["strings"][:200], listing=None, listing_error=None)
    s_ = settings()
    tmpd = tempfile.mkdtemp(prefix="nwn_ncs_")
    try:
        p = os.path.join(tmpd, script + ".ncs")
        with open(p, "wb") as fh:
            fh.write(data)
        text, err = nwn_ncs.disassemble(p, s_.get("nwn_root") or None, s_.get("nwn_user") or None)
        out["listing"] = text[:400000] if text else None
        out["listing_error"] = err
        out["listing_truncated"] = bool(text and len(text) > 400000)
    finally:
        n.rmtree_force(tmpd, ignore_errors=True)
    return out


def list_dir(path):
    """The folder browser: sub-folders and NWN files (.mod .hak .erf .tlk) in path, whether path itself looks like
    an unpacked module (has module.ifo), and its parent. Empty path = the drive list on Windows, else the home
    folder. Hidden (".") entries are left out. Read-only; an unreadable folder gives an error field, never raises.
    Any folder the user can read can be listed: the session token is what keeps this to the dashboard."""
    if not path:
        if os.name == "nt":
            drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
            return dict(path="", parent=None, dirs=drives, mods=[])
        path = os.path.expanduser("~")
    path = os.path.abspath(path)
    dirs, mods = [], []
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                dirs.append(e.name)
            elif e.name.lower().endswith((".mod", ".hak", ".erf", ".tlk")):
                mods.append(e.name)
    except OSError as ex:
        return dict(path=path, parent=os.path.dirname(path), dirs=[], mods=[], error=str(ex))
    looks_like_module = os.path.isfile(os.path.join(path, "module.ifo"))
    return dict(path=path, parent=os.path.dirname(path) if os.path.dirname(path) != path else "", dirs=dirs,
                mods=mods, is_module=looks_like_module)


# Caps on the impact chains a static export embeds, most important first (see export_static): how many, how long to
# spend working them out, and how many characters of JSON in all.
EXPORT_DETAIL_MAX_ITEMS = 2000
EXPORT_DETAIL_MAX_SECONDS = 20
EXPORT_DETAIL_MAX_BYTES = 60_000_000


def export_static(name, demo_note=None, logs=None, embed_limit=4_000_000):
    """Self-contained read-only HTML report (no server needed): report, impact trees, script source,
    conversations, last build/audit, optional log snapshot, and small texture previews/raw JSON.

    Impact trees are worked out from the graph, most important first, up to EXPORT_DETAIL_MAX_ITEMS trees,
    EXPORT_DETAIL_MAX_SECONDS of work and EXPORT_DETAIL_MAX_BYTES of JSON; the report's note says how many were left out.

    name: analysis name. demo_note: a note shown at the top. logs: a log monitor report to include.
    embed_limit: character budget for previews, 2da/text files and raw JSON together. Returns the HTML text;
    writes nothing (main() saves it for --export, /api/export sends it as a download)."""
    import base64
    d = analysis_dir(name)
    rep = json.load(open(os.path.join(d, "report.json"), encoding="utf-8"))
    g, db = get_graph(name)
    # impact chains: most important first, worked out now from the graph (as the dashboard's detail panel does),
    # within a size budget (a big module's full set is hundreds of MB) and a count/time cap (each chain is a graph
    # walk: tens of thousands of them would take many minutes)
    order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "None": 4}
    ranked = sorted(rep.get("impact", {}).items(), key=lambda kv: order.get(kv[1].get("level"), 5))
    details, left, skipped_detail = {}, EXPORT_DETAIL_MAX_BYTES, 0
    with LOCK:
        live = g.live(nwn_analysis.server_config_values(d)[1])     # once: the same live set for every chain
        sources = {r[0]: r[1] for r in db.execute("SELECT name, source FROM scripts")}
    # The walks only read the in-memory graph (never the connection), so they run without LOCK and other requests
    # are not held up for the seconds this can take.
    t0 = time.time()
    for k, _v in ranked:
        if k not in g.nodes:
            continue
        if len(details) >= EXPORT_DETAIL_MAX_ITEMS or time.time() - t0 > EXPORT_DETAIL_MAX_SECONDS:
            skipped_detail += 1
            continue
        det = g.impact(k, detail=True, live=live)
        size = len(json.dumps(det))
        if size > left:
            skipped_detail += 1
            continue
        details[k] = det
        left -= size
    if skipped_detail:
        cut = (f"This exported report leaves out the detailed impact chains of {skipped_detail} lower-impact item(s) "
               f"(it keeps at most {EXPORT_DETAIL_MAX_ITEMS:,}, {EXPORT_DETAIL_MAX_SECONDS} seconds of work and "
               f"{EXPORT_DETAIL_MAX_BYTES // 1_000_000} MB) to keep the file a manageable size; the dashboard has them all.")
        demo_note = f"{demo_note} {cut}" if demo_note else cut
    dlgs = {}
    cd = os.path.join(d, "conversations")
    if os.path.isdir(cd):
        for fn in os.listdir(cd):
            dlgs[fn[:-3]] = open(os.path.join(cd, fn), encoding="utf-8").read()
    build = {}
    for nm in ("changes", "audit"):
        p = os.path.join(d, "build", nm + ".json")
        if os.path.isfile(p):
            build[nm] = json.load(open(p, encoding="utf-8"))
    previews, raw, texts, budget = {}, {}, {}, embed_limit
    for f in rep["files"]:
        if f["preview"] and f["ext"] == "tga" and budget > 0:
            data = read_module_file(name, f["relpath"])
            png = n.tga_to_png(data, 128) if data else None
            if png:
                previews[f["relpath"]] = "data:image/png;base64," + base64.b64encode(png).decode()
                budget -= len(previews[f["relpath"]])
        if f["ext"] in ("2da", "txi", "set", "mdl") and (f["size"] or 0) < 200_000 and budget > 0:
            data = read_module_file(name, f["relpath"])
            # a compiled (binary) model starts with 4 zero bytes (as nwnlib.scan_mdl checks); only text models embed
            if data and not (f["ext"] == "mdl" and data[:4] == b"\0\0\0\0"):
                texts[f["relpath"]] = data.decode("cp1252", "replace")
                budget -= len(texts[f["relpath"]])
        jp = os.path.join(d, "json", f"{os.path.basename(f['relpath']).lower()}.json")
        if os.path.isfile(jp) and os.path.getsize(jp) < budget:
            raw[f["relpath"]] = open(jp, encoding="utf-8").read()
            budget -= len(raw[f["relpath"]])
    cp = os.path.join(d, "catalog.json")
    catalog = json.load(open(cp, encoding="utf-8")) if os.path.isfile(cp) else None
    try:
        palette = palette_data(d)
    except Exception:  # noqa - the Blueprints page then says there is no palette data; the rest still exports
        palette = None
    payload = json.dumps(dict(report=rep, impact=details, sources=sources, dlgs=dlgs, build=build, logs=logs, catalog=catalog,
                              palette=palette,
                              previews=previews, raw=raw, texts=texts, note=demo_note), ensure_ascii=False)
    payload = payload.replace("<", "\\u003c")  # nothing in module text can end the script block
    # The same dashboard.html is used; window.NWN_STATIC tells it to read this embedded data instead of the API.
    html = open(os.path.join(HERE, "dashboard.html"), encoding="utf-8").read()
    return html.replace("<!--STATIC_DATA-->", f"<script>window.NWN_STATIC={payload};</script>")


# ------------------------------------------------------------------ HTTP
class Reply:
    """A response other than "200 with this JSON body": a route function returns one for another status code, a
    file, or a page. code: HTTP status; body: dict/list (JSON), str or bytes; ctype: Content-Type; extra: headers."""
    def __init__(self, code, body, ctype="application/json", extra=None):
        self.code, self.body, self.ctype, self.extra = code, body, ctype, extra


_REQUIRED = object()
_KIND_NAMES = {str: "text", list: "a list", dict: "an object", bool: "true or false", int: "a number", float: "a number"}


def field(b, key, kind=str, default=_REQUIRED):
    """b[key] from a JSON request body, checked to be of type kind (a type or a tuple of types).

    Missing (or null): default when one is given, else ValueError "missing field". Present with another type:
    ValueError naming the field. Both become a 400 that says what to fix, so a TypeError or KeyError deeper down
    is always a bug in the toolkit (a 500 with its traceback), never a malformed request."""
    v = b.get(key)
    if v is None:
        if default is _REQUIRED:
            raise ValueError(f"missing field '{key}'")
        return default
    kinds = kind if isinstance(kind, tuple) else (kind,)
    # bool is a subclass of int in Python; a true/false is never accepted where a number is expected
    if not isinstance(v, kinds) or (isinstance(v, bool) and bool not in kinds):
        names = []
        for k in kinds:
            if _KIND_NAMES.get(k, k.__name__) not in names:
                names.append(_KIND_NAMES.get(k, k.__name__))
        raise ValueError(f"field '{key}' must be {' or '.join(names)}")
    return v


def text_list(b, key):
    """b[key] as a list of strings ([] when missing); ValueError when it is not a list or holds anything else."""
    v = field(b, key, list, [])
    if any(not isinstance(x, str) for x in v):
        raise ValueError(f"field '{key}' must be a list of text")
    return v


def _query(u):
    """The first value of each query-string parameter of a parsed URL, as a dict of strings."""
    return {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}


def page():
    """GET /: the dashboard page. It needs no token: it holds no data and reads the token from its own URL (?t=).
    CSP: scripts, styles, images etc. may come only from this server, the page's own inline code, or data:/blob:
    URLs (previews and downloads the page makes itself) - never from another site; connect-src 'self': fetch() can
    only talk to this server; base-uri / form-action / frame-ancestors / object-src close the remaining ways to
    point the page elsewhere or to frame it inside another site's page."""
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as fh:
        html = fh.read()
    return Reply(200, html, "text/html; charset=utf-8",
                 {"Content-Security-Policy": "default-src 'self' 'unsafe-inline' data: blob:; "
                                             "img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; "
                                             "form-action 'self'; frame-ancestors 'none'; object-src 'none'"})


# ---- GET routes: fn(q) where q holds the query parameters as strings; "a" is always an analysis name.

def api_state(q):
    """Start-up data: analyses, settings, workspace, analysis jobs running or started in the last 6 hours, whether
    the compiler was found, the default log folders, the game install folder found in the usual places, and the
    toolkit's version."""
    with LOCK:
        jobs = [dict(id=j.id, kind=j.kind, status=j.status, **j.info) for j in JOBS.values()
                if j.kind == "analyse" and (j.status == "running" or time.time() - j.started < 6 * 3600)]
    s = settings()
    found = default_nwn_roots()
    analyses = list_analyses()
    setup = nwn_setup.state(HERE)
    return dict(analyses=analyses, settings=s, workspace=WORKSPACE, jobs=jobs,
                compiler_found=nwn_compile.find_compiler(s["compiler"] or None),
                default_logs=nwn_logs.default_log_dirs(), default_nwn_root=found[0] if found else "",
                version=VERSION, reanalyse=nwn_update.REANALYSE, work_format=nwn_update.WORK_FORMAT,
                setup=setup,
                # an unzipped release offers to install itself instead (the Install card can bring work across)
                update_offer=[] if setup["release"] and not setup["installed"] else update_offer(analyses))


def update_offer(analyses):
    """Other copies of the toolkit next to this one that hold work, offered on the Modules page while this copy has
    no analyses and has never brought work across - the first start of a new version. [] otherwise. Read-only."""
    if analyses or nwn_update.imported(WORKSPACE):
        return []
    try:
        return nwn_update.find_copies(HERE)
    except OSError:
        return []


def api_update_find(q):
    """Settings "Bring work from another copy": the copies found next to this one (nwn_update.find_copies)."""
    return dict(copies=nwn_update.find_copies(HERE), imported=nwn_update.imported(WORKSPACE))


def api_update_plan(b):
    """What bringing work across from the folder you picked would copy and skip (nothing is written)."""
    return nwn_update.plan_import(field(b, "from"), WORKSPACE, HERE)


def api_update_import(b):
    """Bring work across (nwn_update.run_import) as a job: copies the other copy's workspace and tools programs into
    this one; the other copy is only read. One at a time (job key "_update")."""
    src = field(b, "from")
    plan = nwn_update.plan_import(src, WORKSPACE, HERE)       # a refusal is a plain 400 at once, not a failed job
    if plan["refused"]:
        raise ValueError(plan["refused"])

    def job_fn(job):
        rec = nwn_update.run_import(src, WORKSPACE, HERE, progress=_job_progress(job), stop=lambda: job.stop_requested)
        print(f"Copied {len(rec['analyses'])} analyses ({rec['files']} files) from {rec['source']}")
        return rec
    return dict(job=run_job("update", job_fn, key="_update"))


# Set by /api/setup/launch: the install folder to start once this dashboard has stopped (it frees the port first).
RELAUNCH = {"root": None}
SERVER = {"srv": None}


def api_setup_state(q):
    """The Install card: this copy's state and what is already on this computer (nwn_setup.find_previous - looked up
    only for an unzipped release, the only kind that offers to install itself)."""
    st = nwn_setup.state(HERE)
    if st["release"] and not st["installed"]:
        st.update(nwn_setup.find_previous(HERE))
    return st


def api_setup_plan(b):
    """What Install would do (nwn_setup.plan_install); writes nothing."""
    return nwn_setup.plan_install(field(b, "root"), HERE, field(b, "bring_from", str, "") or None,
                                  bool(b.get("allow_older")))


def api_setup_install(b):
    """Install / update as a job (nwn_setup.run_install). A refusal is a plain 400 at once, not a failed job."""
    root, src, older = field(b, "root"), field(b, "bring_from", str, "") or None, bool(b.get("allow_older"))
    plan = nwn_setup.plan_install(root, HERE, src, older)
    if plan["refused"]:
        raise ValueError(plan["refused"])

    def job_fn(job):
        rec = nwn_setup.run_install(root, HERE, src, older, progress=_job_progress(job), stop=lambda: job.stop_requested)
        print(f"Installed {rec['version']} in {rec['root']}")
        return rec
    return dict(job=run_job("setup", job_fn, key="_setup"))


def api_setup_launch(b):
    """After Install: stop this dashboard and start the installed one in its own window (nwn_setup.launch), so the
    port is free for it. Returns whether this system can start it (Windows, macOS); elsewhere the page says to use
    the start file. Only for a folder that holds an install."""
    info = nwn_setup.install_info(field(b, "root"))
    if not info:
        raise ValueError("that folder holds no installed toolkit")
    name, _ = nwn_setup.launcher_for()
    can = os.name == "nt" or sys.platform == "darwin"
    if can and SERVER["srv"] is not None:
        RELAUNCH["root"] = info["root"]
        # after this reply has been sent: stop serving; main() then frees the port and starts the installed copy
        threading.Timer(0.6, SERVER["srv"].shutdown).start()
    return dict(starting=can, launcher=os.path.join(info["root"], name))


def api_reanalyse(b):
    """Overview "Analyse again": the same module, haks and talk table the analysis was made with (read from its
    index), into the same analysis folder, so your edits, accepted issues and waiting changes stay with it."""
    a = field(b, "a")
    d = analysis_dir(a)
    db = n.sqlite_ro(os.path.join(d, "index.sqlite"))
    try:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    finally:
        db.close()
    module = meta.get("module_path") or ""
    if not module or not os.path.exists(module):
        raise ValueError(f"the module this analysis was made from is not there any more ({module or 'unknown'}) - "
                         "analyse it from the Modules page")
    try:
        haks = [h for h in json.loads(meta.get("haks") or "[]") if isinstance(h, str) and os.path.isfile(h)]
    except ValueError:
        haks = []
    tlk = meta.get("tlk") or ""
    tlk = tlk if tlk and os.path.isfile(tlk) else ""
    if analysis_name_for(module, a) != a:
        raise ValueError(f"'{a}' would not be re-used for {module} - analyse it from the Modules page")
    return dict(job=run_job("analyse", job_analyse(module, haks, tlk, a), key=a), module=module)


def api_analysis_version(q):
    """Which toolkit made an analysis and saved its work: the report's stamp is in the report; this adds the
    newer-work check (nwn_update.newer_work: a sentence, or None) and the work stamp itself."""
    d = analysis_dir(q.get("a"))
    return dict(newer_work=nwn_update.newer_work(d), work=nwn_update.read_work(d), this_version=VERSION)


def api_detect_nwn_root(q):
    """Settings "Detect": every game install folder found in the usual places (see default_nwn_roots)."""
    return dict(roots=default_nwn_roots())


def api_job(q):
    """A job's status, last 400 log lines, result and progress (% / time left), polled while it runs."""
    j = JOBS.get(q.get("id", ""))
    if not j:
        return Reply(404, {"error": "no such job"})
    return dict(status=j.status, lines=j.lines[-400:], result=j.result, kind=j.kind,
                progress=j.tracker.snapshot() if j.tracker else None, elapsed_s=time.time() - j.started, **j.info)


def api_hak_list(q):
    """Hak editor: the projects, the haks the chosen analysis' module lists, and the usable analyses."""
    module_haks = []
    if q.get("a"):
        d = analysis_dir(q["a"])
        db = n.sqlite_ro(os.path.join(d, 'index.sqlite'), timeout=2)
        try:
            module_haks = json.loads((db.execute("SELECT value FROM meta WHERE key='haks'").fetchone() or ["[]"])[0])
        finally:
            db.close()
    return dict(projects=HAKS.list(), module_haks=module_haks,
                analyses=[a["name"] for a in list_analyses() if not a.get("incomplete")])


def _hak_project(pid):
    """A hak project's project.json as a dict (source, name, analysis)."""
    return HAKS.project(pid)


def _hak_context(proj):
    """The editor context of a hak project's linked analysis, or None when it has none."""
    return editor_context(proj.get("analysis"), proj["source"]) if proj.get("analysis") else None


def api_hak_state(q):
    """Hak editor: one project's files and pending changes, checked against its linked analysis."""
    proj = _hak_project(q.get("p", ""))
    st = HAKS.state(q.get("p", ""), _hak_context(proj))
    st["analyses_using"] = analyses_using_hak(proj["source"])
    return st


def api_hak_file(q):
    """Hak editor: one file of a project - as a download (dl=1), an image preview, or text / model / GFF details
    (each capped at 400000 characters). HAKS.read only serves names the project lists, so the name used in
    Content-Disposition is one of those."""
    pid, name = q.get("p", ""), q.get("name", "").lower()
    data = HAKS.read(pid, name)
    ext = name.rsplit(".", 1)[-1]
    if q.get("dl"):
        return Reply(200, data, "application/octet-stream", {"Content-Disposition": f'attachment; filename="{name}"'})
    if ext == "tga":
        png = n.tga_to_png(data, 256)
        return Reply(200, png, "image/png") if png else Reply(415, {"error": "no preview for this TGA"})
    if ext in ("png", "jpg", "bmp"):
        return Reply(200, data, {"png": "image/png", "jpg": "image/jpeg", "bmp": "image/bmp"}[ext])
    info = dict(name=name, size=len(data))
    # a .mdl that does not start with 4 zero bytes is a text (ASCII) model
    if ext in nwn_hakedit.TEXT_EXTS or (ext == "mdl" and data[:4] != b"\0\0\0\0"):
        info["text"] = data[:400_000].decode(n.ENCODING, "replace")
        info["truncated"] = len(data) > 400_000
    if ext == "mdl":
        info["model"] = n.scan_mdl(data)
    if ext in n.GFF_EXTENSIONS:
        try:
            info["gff"] = json.dumps(n.gff_to_json(n.read_gff(data)), indent=1)[:400_000]
        except Exception as ex:  # noqa - any damage in a hak's file is shown in the panel, not a failed request
            info["error"] = str(ex)
    return info


def api_twoda(q):
    """2da editor: open a table (from a hak project or a module edit) with its checks, row usage and the hash of
    the current bytes that a save must send back (see api_twoda_save)."""
    load, _save, ctx, label, current = twoda_source(q)
    tab, parse_issues, orig = load()
    name = os.path.splitext(os.path.basename(q.get("name") or q.get("file") or ""))[0].lower()
    usage = ctx.tables.get(name, {}) if ctx else {}
    return dict(table=tab, original=orig, parse_issues=parse_issues, label=label, name=name,
                base_sha=hashlib.sha256(current()).hexdigest(), checks=twoda_checks(ctx, name, tab, orig), usage=usage,
                usage_known=bool(ctx and name in ctx.tables_known),
                readers=sorted(ctx.readers.get(name, [])) if ctx else [], context=ctx.describe() if ctx else None)


def api_manual(q):
    """Help page: docs/MANUAL.md as Markdown."""
    with open(os.path.join(HERE, "docs", "MANUAL.md"), encoding="utf-8") as fh:
        return dict(markdown=fh.read())


def api_quickscan(q):
    """Quick scan: a module's haks found/missing, clashes, size and time estimate in seconds (nwn_quickscan).
    haks is a "|"-separated list of extra hak paths."""
    path = (q.get("path") or "").strip().strip('"')
    if not path or not os.path.exists(path):
        raise ValueError(f"Not found: {path}")
    s = settings()
    haks = [h for h in (q.get("haks") or "").split("|") if h.strip()]
    return nwn_quickscan.scan(path, haks, s["nwn_root"] or None, s["nwn_user"] or None, WORKSPACE)


def api_report(q):
    """The analysis' report.json, sent as stored (bytes, not re-encoded): what most pages show."""
    with open(os.path.join(analysis_dir(q.get("a")), "report.json"), "rb") as fh:
        return Reply(200, fh.read())


def api_preview(q):
    """Image preview of a module/hak file: TGA converted to PNG (256 px), PNG/JPG/BMP/GIF as they are. Any other
    type is refused, so this cannot be used to send an arbitrary file to the browser as a page."""
    data = read_module_file(q.get("a"), q.get("file", ""))
    if data is None:
        return Reply(404, {"error": "not found"})
    ext = q.get("file", "").rsplit(".", 1)[-1].lower()
    if ext == "tga":
        png = n.tga_to_png(data, 256)
        return Reply(200, png, "image/png") if png else Reply(415, {"error": "unsupported TGA variant"})
    ctype = {"png": "image/png", "jpg": "image/jpeg", "bmp": "image/bmp", "gif": "image/gif"}.get(ext)
    return Reply(200, data, ctype) if ctype else Reply(415, {"error": "no preview for this type"})


def _json_file(p, default):
    """The JSON in file p, or default when there is no such file."""
    if not os.path.isfile(p):
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def api_build_result(q):
    """Build & audit page: the last build's changes.json / audit.json and where its reports are."""
    d = os.path.join(analysis_dir(q.get("a")), "build")
    out = {nm: _json_file(os.path.join(d, nm + ".json"), None) for nm in ("changes", "audit")}
    out = {k: v for k, v in out.items() if v is not None}
    out.update(build_dir=d, changes_md=os.path.join(d, "changes.md"), audit_md=os.path.join(d, "audit.md"))
    return out


def api_generator(q):
    """Script generator: the building blocks it offers (engine functions from nwscript.nss, plus the module's
    quest-token helper and slot names when an analysis is chosen)."""
    st_ = settings()
    nws = nwn_edit.load_nwscript(st_["nwn_root"] or None, st_["nwn_user"] or None)
    adapter, tokens = generator_module_info(q.get("a")) if q.get("a") else (None, [])
    return nwn_edit.catalog(nws, adapter, tokens)


def api_dupdiff(q):
    """Duplicates page: how two look-alike blueprints x and y (node ids) differ field by field (nwn_dupdiff); found
    is False when either has no file."""
    import nwn_dupdiff
    _g, db = get_graph(q.get("a"))
    with LOCK:
        diffs = nwn_dupdiff.compare(db, q.get("x", ""), q.get("y", ""))
    return dict(diffs=diffs or [], found=diffs is not None)


def api_diff_state(q):
    """Compare modules page: saved snapshots, analyses to compare with, and whether a last result exists."""
    import nwn_diff
    return dict(snapshots=nwn_diff.list_snapshots(WORKSPACE),
                analyses=[dict(name=a_["name"], module_path=a_.get("module_path")) for a_ in list_analyses()
                          if not a_.get("incomplete") and not a_.get("archived")],
                has_result=os.path.isfile(os.path.join(WORKSPACE, "_diffs", "last.json")))


def api_logs(q):
    """Log monitor: read whatever the watched logs gained since the last poll and return the grouped report."""
    mon = MONITORS.get(q.get("a"))
    if not mon:
        return dict(running=False)
    with MONITOR_LOCK:
        mon.poll()
        return dict(running=True, **mon.report())


def api_export(q):
    """The static HTML report as a download (see export_static). a has passed analysis_dir inside export_static,
    so it is safe inside the Content-Disposition header."""
    a = q.get("a")
    return Reply(200, export_static(a), "text/html; charset=utf-8",
                 {"Content-Disposition": f'attachment; filename="{a}_report.html"'})


def _refactor_history(q):
    """Find & replace: the changes applied so far, each of which can be undone."""
    import nwn_refactor
    return nwn_refactor.history(analysis_dir(q.get("a")))


GET_ROUTES = {
    "/api/state": api_state,
    "/api/detect_nwn_root": api_detect_nwn_root,
    "/api/ls": lambda q: list_dir(q.get("path", "")),                   # folder browser (see list_dir)
    "/api/job": api_job,
    "/api/hak/list": api_hak_list,
    "/api/hak/state": api_hak_state,
    "/api/hak/file": api_hak_file,
    "/api/twoda": api_twoda,
    "/api/manual": api_manual,
    "/api/analysis/usage": lambda q: analysis_usage(q.get("a", "")),    # disk use by part, before Delete
    "/api/quickscan": api_quickscan,
    "/api/report": api_report,
    "/api/analysis/version": api_analysis_version,
    "/api/update/find": api_update_find,
    "/api/setup/state": api_setup_state,
    "/api/node": lambda q: node_details(q.get("a"), q.get("node", "")),  # detail panel for one node
    "/api/find": lambda q: find_objects(q.get("a"), q.get("q", "")),     # objects / blueprints / scripts by name
    "/api/files": lambda q: find_files(q.get("a"), q.get("q", "")),      # module and hak files by file name
    "/api/ncs": lambda q: compiled_code(q.get("a"), q.get("script", "")),  # script panel "Compiled code"
    "/api/preview": api_preview,
    "/api/build_result": api_build_result,
    "/api/install/plan": api_install_plan,                              # Build page: Add to game folders - the plan
    "/api/install/state": api_install_state,                            # the last Add to game folders (installed.json)
    # Hak catalogue page: catalog.json ({} when the analysis has none)
    "/api/catalog": lambda q: _json_file(os.path.join(analysis_dir(q.get("a")), "catalog.json"), {}),
    # Blueprints page: every custom blueprint and its palette category (palette.json, built on first use)
    "/api/palette": lambda q: palette_data(analysis_dir(q.get("a"))),
    # Blueprints page: moves waiting for the next build (palette_moves.json; {} when none)
    "/api/palette/moves": lambda q: nwn_palette.load_moves(analysis_dir(q.get("a"))),
    # Blueprints page: bulk field changes waiting for the next build (blueprint_changes.json; {} when none)
    "/api/bpchange/list": lambda q: nwn_bpchanges.load_changes(analysis_dir(q.get("a"))),
    "/api/edits": lambda q: nwn_edit.list_edits(analysis_dir(q.get("a"))),   # files in the edits overlay
    "/api/generator": api_generator,
    "/api/database": lambda q: database_view(q.get("a")),
    "/api/modsettings": lambda q: mod_settings(q.get("a")),   # Overview: module and loaded server settings
    # Issues page: the issues marked "accepted - by design"
    "/api/issues/accepted": lambda q: _json_file(os.path.join(analysis_dir(q.get("a")), ACCEPTED), {}),
    "/api/dupdiff": api_dupdiff,
    "/api/refactor/history": _refactor_history,
    "/api/diff/state": api_diff_state,
    "/api/diff/result": lambda q: _json_file(os.path.join(WORKSPACE, "_diffs", "last.json"), {}),
    "/api/housekeep/state": lambda q: housekeep_state(),
    # the last whole-module compile's results
    "/api/compile_result": lambda q: _json_file(os.path.join(analysis_dir(q.get("a")), "compile.json"), {}),
    "/api/logs": api_logs,
    "/api/export": api_export,
}


# ---- POST routes: fn(b) where b is the JSON body (a dict); the two uploads get (q, data) instead.

def hak_post(action, b):
    """Hak editor actions (POST /api/hak/<action>); b["p"] is the project id. All work on the project copy in
    <workspace>/_haks/ through nwn_hakedit.HakEditor, never on the hak itself, except where noted:

        open           start a project from a hak path (and link the first analysis that uses it, if none given)
        analysis       link the project to an analysis (for usage checks) or unlink it
        remove / undo / undo_all / rename / add_path    change the project's pending file list
        extract        copy files out of the hak into a new or empty folder (outside the workspace if one is given)
        build          job: write the rebuilt hak under a new name in the project's builds/<id>/ folder
        install        "Add to hak folder": copy a build into the hak folder next to the original (never over it)
                       and point the linked analysis' module.ifo edit at the new name
        use_in_module  only the module.ifo edit (see point_module_at_hak)
        restore        put a backup back in place of the hak (in-place write, by request only; itself backed up)
        discard        delete the project (staged changes, builds, extracted files); backups are kept
        rebaseline     the hak changed on disk: take it as the new starting point
    Returns the action's result dict. Raises ValueError for an unknown action, a refused one or a malformed field."""
    pid = field(b, "p", str, "")
    if action == "open":
        path = field(b, "path", str, "")
        a = field(b, "analysis", str, None)
        if a is None:   # pick an analysis that uses this hak, automatically
            using = analyses_using_hak(os.path.abspath(path.strip().strip('"')))
            a = using[0] if using else None
        return dict(p=HAKS.open(path, a))
    if action == "analysis":
        a = field(b, "analysis", str, "")
        if a:
            analysis_dir(a)          # a real analysis name, or ValueError
        HAKS.set_analysis(pid, a)
        return dict(ok=True)
    if action == "remove":
        return HAKS.remove(pid, text_list(b, "names"), bool(b.get("force")))
    if action == "undo":
        return HAKS.undo(pid, text_list(b, "names"))
    if action == "undo_all":
        return HAKS.undo_all(pid)
    if action == "rename":
        return HAKS.rename(pid, field(b, "old", str, "").lower(), field(b, "new", str, ""))
    if action == "add_path":
        return HAKS.add_path(pid, field(b, "path", str, ""))
    if action == "extract":
        return HAKS.extract(pid, text_list(b, "names") or None, field(b, "dest", str, "") or None)
    if action == "build":
        proj = _hak_project(pid)
        ctx = _hak_context(proj)

        def fn(job):
            job.info = dict(hak=proj["name"])
            info = HAKS.build(pid, ctx, log=print)
            return dict(id=info["id"], verified=info["verified"], bytes=info["bytes"], files=info["files"])
        return dict(job=run_job("hakbuild", fn, key="hak:" + pid))
    if action == "install":
        if running_job_for("hak:" + pid):
            raise ValueError("a rebuild of this hak is still running")
        proj = _hak_project(pid)
        r = HAKS.install(pid, field(b, "build", str, ""))
        # the module must point at the new name: done as an edit of module.ifo in the chosen analysis
        if proj.get("analysis"):
            try:
                r["module"] = point_module_at_hak(proj["analysis"], r["old_name"], r["new_name"])
            except (ValueError, OSError, n.GffError) as ex:
                r["module_error"] = str(ex)
        return r
    if action == "use_in_module":
        return point_module_at_hak(field(b, "analysis", str, ""), field(b, "old", str, ""), field(b, "new", str, ""))
    if action == "restore":
        return HAKS.restore(pid, field(b, "backup", str, ""))
    if action == "discard":
        return HAKS.discard(pid)
    if action == "rebaseline":
        return HAKS.rebaseline(pid)
    raise ValueError("unknown hak action")


def api_twoda_check(b, save=False):
    """2da editor: check a table, or (save=True) save it. Save is refused (409) when the file changed since it was
    opened (base_sha), when a check blocks it, or when a "used row deleted or moved" check is not confirmed with
    allow_risky."""
    _load, save_fn, ctx, label, current = twoda_source(b)
    _tab, _pi, orig = _load()
    name = os.path.splitext(os.path.basename(b.get("name") or b.get("file") or ""))[0].lower()
    table = field(b, "table", dict)
    # Shape checks before anything uses the table: each row is [label] + one text cell per column, and "origin"
    # (which original row each row came from) has one entry per row.
    if not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list) or \
            not isinstance(table.get("origin"), list) or len(table["origin"]) != len(table["rows"]):
        raise ValueError("bad table (rows and their origins don't line up) - reload the 2da editor")
    if any(not isinstance(r, list) or len(r) != len(table["columns"]) + 1 or not all(isinstance(c, str) for c in r)
           for r in table["rows"]):
        raise ValueError("bad table (a row has the wrong number of cells) - reload the 2da editor")
    checks = twoda_checks(ctx, name, table, orig)
    if not save:
        return dict(checks=checks)
    if b.get("base_sha") != hashlib.sha256(current()).hexdigest():
        return Reply(409, dict(error="not saved: this 2da was changed elsewhere (another tab or tool) since you opened "
                                     "it - reload it and make your change again", checks=checks))
    block = nwn_2da.blocking(checks)
    # "usage" blocks (a row the module uses is deleted or would move) can be overridden by the user; others cannot
    risky = [c for c in block if c.get("blocking") == "usage"]
    hard = [c for c in block if c not in risky]
    if hard or (risky and not b.get("allow_risky")):
        return Reply(409, dict(error="not saved: " + (hard or risky)[0]["msg"], checks=checks,
                               needs_confirm=bool(risky and not hard)))
    r = save_fn(table)
    return dict(saved=True, result=r if isinstance(r, dict) else str(r), checks=checks)


def api_analyse(b):
    """Start an analysis job (module path, extra haks, tlk, optional name). Paths pasted with quotes are unquoted.
    The module path is checked here, so a typo is a plain 400 at once instead of a failed job. The job key is the
    analysis name, so one module cannot be analysed twice at once."""
    module = field(b, "module").strip().strip('"')
    if not module or not os.path.exists(module):
        raise ValueError(f"Not found: {module or '(empty)'} - check the module path")
    haks = [h.strip().strip('"') for h in text_list(b, "haks") if h.strip()]
    tlk = field(b, "tlk", str, "").strip().strip('"')
    name = field(b, "name", str, "") or None
    jid = run_job("analyse", job_analyse(module, haks, tlk, name), key=analysis_name_for(module, name))
    return dict(job=jid)


def api_analysis_delete(b):
    """Modules page: Delete an analysis's data; your own work only when ticked and confirmed by name."""
    return delete_analysis(field(b, "a", str, ""), text_list(b, "include"), field(b, "confirm", str, ""))


def api_build(b):
    """Start a build & audit job from the plan the user approved (the analysis is checked first)."""
    a = field(b, "a")
    analysis_dir(a)
    name = field(b, "name", str, "")
    return dict(job=run_job("build", job_build(a, field(b, "plan", dict), safe_name(name) if name else None), key=a))


def api_archive(b, unpack=False):
    """Modules page: Archive (zip the analysis data to save space) or, with unpack, Unpack it again, as a job.
    While it runs the name is in ARCHIVING, so analysis_dir refuses it and nothing opens its index meanwhile."""
    name = field(b, "a", str, "")
    with LOCK:
        d = _checked_analysis_folder(name)
        reason = None if unpack else _busy_reason(name, d)
        if reason:
            raise ValueError(reason)
        release_analysis(name)
    fn_ = nwn_archive.unarchive if unpack else nwn_archive.archive

    def job_fn(job):
        try:
            with LOCK:                       # again, in case a request reopened it before the job ran
                release_analysis(name)
            r = fn_(d, _job_progress(job), lambda: job.stop_requested)
            print("done")
            return r
        finally:
            ARCHIVING.discard(name.lower())
    ARCHIVING.add(name.lower())
    try:
        jid = run_job("archive", job_fn, key=name)
    except Exception:
        ARCHIVING.discard(name.lower())     # the job never started: don't leave the analysis locked
        raise
    return dict(job=jid)


def _diff_source(pth):
    """pth if it exists; a snapshot file (.json.gz) is accepted only from the toolkit's own _snapshots folder, so
    only snapshots the toolkit made are ever read as snapshots."""
    import nwn_diff
    pth = pth.strip()
    if not pth or not os.path.exists(pth):
        raise ValueError(f"not found: {pth or '(empty)'}")
    snapdir = os.path.normcase(os.path.abspath(nwn_diff.snapshot_dir(WORKSPACE)))
    if pth.lower().endswith(".json.gz") and not os.path.normcase(os.path.abspath(pth)).startswith(snapdir + os.sep):
        raise ValueError("snapshots are read only from the toolkit's _snapshots folder")
    return pth


def api_diff_snapshot(b):
    """Compare modules: take a snapshot of a module, as a job under the key "_diff"."""
    import nwn_diff
    mod_ = _diff_source(field(b, "module", str, ""))
    label = str(b.get("label") or "")[:200]

    def job_fn(job):
        r = nwn_diff.take_snapshot(mod_, WORKSPACE, label, _job_progress(job))
        print(f"snapshot of {r['resources']} resources saved")
        return r
    return dict(job=run_job("diff", job_fn, key="_diff"))


def api_diff_compare(b):
    """Compare modules: compare two modules / snapshots as a job under the key "_diff"; the result goes to
    _diffs/last.json."""
    import nwn_diff
    older, newer = _diff_source(field(b, "older", str, "")), _diff_source(field(b, "newer", str, ""))
    an = field(b, "analysis", str, "") or None
    if an:
        analysis_dir(an)

    def job_fn(job):
        r = nwn_diff.compare(older, newer, an, WORKSPACE, _job_progress(job))
        os.makedirs(os.path.join(WORKSPACE, "_diffs"), exist_ok=True)
        tmp = os.path.join(WORKSPACE, "_diffs", "last.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(WORKSPACE, "_diffs", "last.json"))
        print(f"{r['summary']['changed']} changed, {r['summary']['added']} added, {r['summary']['removed']} removed")
        return r["summary"]
    return dict(job=run_job("diff", job_fn, key="_diff"))


def api_refactor(b, step):
    """Find & replace (nwn_refactor): step "search" finds text, "plan" previews a rename/replace, "apply" writes it
    into the edits overlay (only with confirm "yes"), "undo" reverses one applied change. Never writes the module.
    Unknown kinds/modes fall back to safe defaults."""
    import nwn_refactor
    d = analysis_dir(b.get("a"))
    kinds = [k for k in (field(b, "kinds", list, None) or nwn_refactor.KINDS) if k in nwn_refactor.KINDS]
    mode = b.get("mode") if b.get("mode") in ("contains", "word", "exact") else "contains"
    text = str(b.get("text") or "")
    if step == "search":
        return nwn_refactor.find(d, text, mode, bool(b.get("case")), kinds,
                                 "all" if b.get("scope") == "all" else "module")
    if step == "undo":
        return nwn_refactor.undo(d, str(b.get("id") or ""))
    ids = [x for x in field(b, "ids", list, []) if isinstance(x, str)][:20000]
    new = str(b.get("new") or "")
    if step == "plan":
        return nwn_refactor.plan(d, text, new, ids, mode, bool(b.get("case")), kinds)
    if field(b, "confirm", str, "") != "yes":
        raise ValueError("confirm the change first")
    return nwn_refactor.apply(d, text, new, ids, mode, bool(b.get("case")), kinds)


def api_compile(b):
    """Start a job that compiles every script of the module (see job_compile)."""
    a = field(b, "a")
    analysis_dir(a)
    return dict(job=run_job("compile", job_compile(a), key=a))


def api_icons(b):
    """Item inventory icons for up to 300 nodes at once (nwn_icons: found in override, haks, module or the game, as
    the game would). POST only because the node list can be long. The resolver is used under ICON_LOCK (one
    request thread at a time)."""
    nodes = [x for x in field(b, "nodes", list, []) if isinstance(x, str)][:300]
    with ICON_LOCK:
        r_ = icon_resolver(b.get("a"))
        return {nd: r_.icon(nd) for nd in nodes}


def api_settings(b):
    """Save settings. Only known keys are taken and each must have the right type; external tools that are not well
    formed are dropped. Returns the saved settings plus warnings: a list of plain sentences (folders that do not
    look like what they should be, dropped or doubtful tools); empty when all is well."""
    s = settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k in b:
            s[k] = field(b, k, type(default))
    dropped = [t for t in s["external_tools"] if isinstance(t, dict) and not clean_tools([t])]
    s = _typed_settings(s)
    save_settings(s)
    warnings = settings_warnings(s)
    if dropped:
        warnings.append(f"{len(dropped)} external tool(s) were not saved: each needs a name and the path of a program")
    return dict(s, warnings=warnings)


def api_logs_start(b):
    """Log monitor: start watching the game/server logs for an analysis (given paths, or the default log folders);
    from_end skips what the logs already hold. Log files are only read."""
    unknown = set(b) - {"a", "paths", "from_end"}
    if unknown:
        raise ValueError(f"unknown parameter(s) {sorted(unknown)}; expected: a, paths (list), from_end")
    a = field(b, "a")
    paths = [p for p in text_list(b, "paths") if p] or None
    mon = nwn_logs.LogMonitor(analysis_dir(a), paths, settings().get("nwn_user") or None)
    if b.get("from_end"):
        for p in mon.paths:
            try:
                mon.offsets[p] = os.path.getsize(p)
            except OSError as ex:       # listed a moment ago, gone now: the request's problem (400)
                raise ValueError(f"log file can't be read: {p} ({ex.strerror or ex})")
    with MONITOR_LOCK:                  # registered only once complete, so a poll never sees it half set up
        MONITORS[a] = mon
    return dict(files=mon.paths)


def api_edit_save(b):
    """Editor: save one file into the analysis' edits overlay (GFF as JSON, or text), never the module.
    "base" is the SHA-256 the detail panel sent when the file was opened: a different hash now means another tab or
    Find & replace changed it, and the save is refused (409) rather than overwriting. base is required: without it
    the check could not run, so a save would overwrite blindly (400). compile: also compile an edited .nss straight
    away (see compile_one)."""
    a, rel = field(b, "a"), field(b, "file")
    d = analysis_dir(a)
    content = field(b, "content")
    if not field(b, "base", str, ""):
        raise ValueError("missing field 'base' (the file's checksum from when it was opened) - close the file and "
                         "open it again")
    cur, _e = nwn_edit.current_bytes(d, rel, lambda rp: read_module_file(a, rp))
    if cur is not None and hashlib.sha256(cur).hexdigest() != b["base"]:
        return Reply(409, dict(error=f"{rel} changed since you opened it (another tab, or Find & replace) - close it, "
                                     "open it again and redo your change"))
    if b.get("format") == "json":
        nwn_edit.save_gff_json(d, rel, content)
    else:
        nwn_edit.save_text(d, rel, content, is_new=bool(b.get("new")))
    result = dict(ok=True, edits=nwn_edit.list_edits(d))
    if rel.lower().endswith(".nss") and b.get("compile"):
        result["compile"] = compile_one(d, rel)
    return result


def api_edit_discard(b):
    """Editor: drop one file's edit, so the next build uses the module's original again."""
    d = analysis_dir(field(b, "a"))
    nwn_edit.discard(d, field(b, "file"))
    return dict(ok=True, edits=nwn_edit.list_edits(d))


def api_generate(b):
    """Script generator: make a script from the page's spec (nwn_edit.generate_script); with save, write it into the
    edits overlay (409 + needs_confirm if that would replace a module script or an existing edit and overwrite was
    not ticked); with compile, compile it too."""
    a = field(b, "a")
    d = analysis_dir(a)
    spec = field(b, "spec", dict)
    libs = {}
    _g, db = get_graph(a)
    with LOCK:
        # Include files are scripts with neither main() nor StartingConditional() (has_main / has_sc 0). libs maps
        # each function they define to the first include that defines it, so the generator can add the #include.
        for nm, defined in db.execute("SELECT name, functions_defined FROM scripts WHERE has_main=0 AND has_sc=0"):
            for fn in (defined or "").split(","):
                if fn:
                    libs.setdefault(fn, nm)
    st_ = settings()
    nws = nwn_edit.load_nwscript(st_["nwn_root"] or None, st_["nwn_user"] or None)
    adapter, tokens = generator_module_info(a)
    with LOCK:
        consts = set()
        # Names of upper-case constants the includes declare, e.g. "const int QUEST_DONE = 3;" gives QUEST_DONE
        # (one per line, at the start of the line).
        for (src_,) in db.execute("SELECT source FROM scripts WHERE has_main=0 AND has_sc=0"):
            consts |= set(re.findall(r"^\s*const\s+\w+\s+([A-Z][A-Z0-9_]*)", src_ or "", re.M))
    r = nwn_edit.generate_script(spec, libs, nws, adapter, consts)
    if b.get("save"):
        fname = r["name"] + ".nss"
        with LOCK:
            in_module = db.execute("SELECT 1 FROM scripts WHERE lower(name)=?", (r["name"],)).fetchone()
        in_edits = os.path.isfile(nwn_edit.edit_path(d, fname))
        if (in_module or in_edits) and not b.get("overwrite"):
            r.update(needs_confirm=True, error=(
                f"{fname} already exists in the module - saving would replace the module's script in the next build"
                if in_module else f"{fname} is already in your edits - saving replaces it"))
            return Reply(409, r)
        nwn_edit.save_text(d, fname, r["source"], is_new=not in_module)
        r["saved"] = True
        if b.get("compile"):
            r["compile"] = compile_one(d, r["name"] + ".nss")
    return r


def api_description(b):
    """Save (or, with empty text, remove) the description of one script in descriptions.json."""
    d = analysis_dir(field(b, "a"))
    script, text = field(b, "script"), field(b, "text", str, "")
    p = os.path.join(d, "descriptions.json")
    data = _json_file(p, {})
    if text:
        data[script] = text
    else:
        data.pop(script, None)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:   # .tmp + os.replace: never a half-written file
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)
    return dict(ok=True)


def api_logs_stop(b):
    """Log monitor: stop watching."""
    MONITORS.pop(field(b, "a", str, ""), None)
    return dict(ok=True)


POST_ROUTES = {
    "/api/twoda/check": api_twoda_check,
    "/api/twoda/save": lambda b: api_twoda_check(b, save=True),
    "/api/analyse": api_analyse,
    "/api/reanalyse": api_reanalyse,
    "/api/setup/plan": api_setup_plan,
    "/api/setup/install": api_setup_install,
    "/api/setup/launch": api_setup_launch,
    "/api/update/plan": api_update_plan,
    "/api/update/import": api_update_import,
    "/api/database/load": load_database,               # read database files into the snapshot, or clear it
    "/api/serverconfig": load_server_config,            # load or remove a server settings file (secrets masked)
    "/api/job/stop": lambda b: stop_job(field(b, "id")),
    # Clear a stopped/unfinished analysis's generated files
    "/api/analysis/clear": lambda b: clear_analysis(field(b, "a", str, "")),
    "/api/analysis/delete": api_analysis_delete,
    "/api/build": api_build,
    "/api/palette/move": api_palette_move,              # Blueprints page: move to another palette category (next build)
    "/api/palette/move/undo": api_palette_move_undo,    # cancel waiting palette moves
    "/api/bpchange/plan": api_bpchange_plan,            # Change fields… preview (nothing stored)
    "/api/bpchange/save": api_bpchange_save,            # Change fields… Save (planned again, then stored)
    "/api/bpchange/undo": api_bpchange_undo,            # cancel waiting field changes
    "/api/install/add": api_install_add,                # Add to game folders (typed ADD; never replaces a file)
    "/api/install/undo": api_install_undo,              # remove only the files that add placed, if unchanged
    "/api/analysis/archive": api_archive,
    "/api/analysis/unarchive": lambda b: api_archive(b, unpack=True),
    "/api/diff/snapshot": api_diff_snapshot,
    "/api/diff/compare": api_diff_compare,
    "/api/search": lambda b: api_refactor(b, "search"),
    "/api/refactor/plan": lambda b: api_refactor(b, "plan"),
    "/api/refactor/apply": lambda b: api_refactor(b, "apply"),
    "/api/refactor/undo": lambda b: api_refactor(b, "undo"),
    "/api/issues/accept": accept_issue,                 # Issues page: tick / untick "accepted - by design"
    "/api/issues/accept_many": accept_issues_many,      # Issues page: Accept all shown… / Un-accept all shown
    "/api/compile": api_compile,
    "/api/icons": api_icons,
    # open a file in one of the user's configured external tools (see open_in_tool: no shell)
    "/api/tools/open": lambda b: open_in_tool(str(b.get("tool") or ""), str(b.get("file") or "")),
    "/api/settings": api_settings,
    "/api/logs/start": api_logs_start,
    "/api/logs/stop": api_logs_stop,
    "/api/edit/save": api_edit_save,
    "/api/edit/discard": api_edit_discard,
    "/api/generate": api_generate,
    "/api/description": api_description,
}
# Routes that end in an action name: /api/hak/<action> (hak editor) and /api/housekeep/<action> (Modules folder).
POST_PREFIX_ROUTES = {"/api/hak/": hak_post, "/api/housekeep/": housekeep_post}
# File uploads: the raw file is the body and its details are in the query string.
UPLOAD_ROUTES = {
    "/api/database/upload": upload_database_file,       # one database file picked in the browser
    # Hak editor: add or replace one file in a project (staged in the project, never in the hak). The file name is
    # checked by nwn_hakedit.name_problem (NWN type, 16-character resref, safe characters).
    "/api/hak/upload": lambda q, data: HAKS.put(q.get("p", ""), q.get("name", ""), data),
    # Blueprints page: Import moves… (preview, or apply=1 to store them in palette_moves.json)
    "/api/palette/import": api_palette_import,
    # Issues page: Import… by-design decisions (preview, or apply=1 to store them)
    "/api/issues/import": api_issues_import,
    # Safe to delete / Duplicates: Import… decisions (a preview the page applies to its plan)
    "/api/plan/import": api_plan_import,
}


class Handler(BaseHTTPRequestHandler):
    """One HTTP request (ThreadingHTTPServer runs each on its own thread).

    GET / serves the page; every other path is /api/... and must pass _guard (Host + session token) first, then is
    looked up in GET_ROUTES / POST_ROUTES / POST_PREFIX_ROUTES / UPLOAD_ROUTES. Errors come back as JSON
    {"error": ...}: 400 for a bad request or a refused action (ValueError), 404 for an unknown endpoint, 409 when a
    save was refused (changed elsewhere / needs confirmation), 500 with the last traceback lines for anything else
    (a bug) - the server itself keeps running."""
    server_version = SERVER_VERSION

    def log_message(self, fmt, *args):  # keep console quiet
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        """Send one complete response. body: dict/list (sent as JSON), str (UTF-8) or bytes. extra: more headers.
        Every response gets no-store (the browser keeps no copy of module data) and nosniff (the browser must
        trust Content-Type, so e.g. a module file sent as an image is never run as a page or script)."""
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        """True when the request may use the API; otherwise sends 403 (bad Host) or 401 (bad token) and returns
        False. Every /api endpoint, GET and POST, calls this first. See "Security model" in the module docstring."""
        # Host check against DNS rebinding: a page from evil.example whose name now points at 127.0.0.1 still
        # sends "Host: evil.example", so it is refused here even though it reached our port.
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "bad host"}); return False
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # The page sends the token as a header on fetch(); <img src> and download links cannot set headers, so
        # they carry it as ?t= instead.
        tok = self.headers.get("X-NWN-Token") or (q.get("t") or [""])[0]
        try:
            # compare_digest takes the same time whether the first or the last character differs, so the token
            # cannot be guessed character by character from response times.
            good = secrets.compare_digest(tok.encode("utf-8", "replace"), TOKEN.encode("utf-8"))
        except (TypeError, AttributeError):
            good = False
        if not good:
            self._send(401, {"error": "missing or wrong session token - reopen the link printed by nwn_dashboard.py"})
            return False
        return True

    def _length(self, limit, too_big):
        """The request's Content-Length as an int between 0 and limit. Raises ValueError (-> 400) otherwise: a
        non-number, or a negative value - read(-1) would wait for the client to close the connection, holding the
        request thread until then."""
        ln = int(self.headers.get("Content-Length") or 0)
        if ln < 0:
            raise ValueError("bad Content-Length")
        if ln > limit:
            raise ValueError(too_big)
        return ln

    def _raw_body(self, limit=600_000_000):
        """The request body as bytes (file uploads). Refused (ValueError) above limit before anything is read; the
        whole body is held in memory."""
        return self.rfile.read(self._length(limit, "file too large"))

    def _body(self):
        """The JSON request body as a dict ({} when empty). Raises ValueError over 5 MB or when it is not an object."""
        ln = self._length(5_000_000, "request too large")
        b = json.loads(self.rfile.read(ln) or b"{}")
        if not isinstance(b, dict):
            raise ValueError("the request body must be a JSON object")
        return b

    def _answer(self, call):
        """Run call() (a route) and send what it returns: a Reply as given, anything else as 200 JSON. ValueError
        (a bad request or a refused action) -> 400 with its message; MemoryError -> 400; any other exception is a
        bug -> 500 with the last traceback lines, which help a bug report and only ever go to this local page."""
        try:
            r = call()
        except ValueError as ex:
            r = Reply(400, {"error": str(ex)})
        except MemoryError:
            r = Reply(400, {"error": "ran out of memory - the file may be damaged or built to exhaust memory"})
        except Exception as ex:  # noqa - reported to the page as a 500, the server keeps running
            r = Reply(500, {"error": str(ex) or type(ex).__name__, "trace": traceback.format_exc().splitlines()[-3:]})
        if not isinstance(r, Reply):
            r = Reply(200, r)
        self._send(r.code, r.body, r.ctype, r.extra)

    def do_GET(self):
        """Endpoints that fetch data for the page (GET_ROUTES); actions that change files are POST requests."""
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._answer(page)
        if not self._guard():
            return
        fn = GET_ROUTES.get(u.path)
        if fn is None:
            return self._send(404, {"error": "unknown endpoint"})
        q = _query(u)
        self._answer(lambda: fn(q))

    def do_POST(self):
        """Endpoints that act: start jobs, save edits and settings, change hak projects, delete or archive analyses.
        All need the session token. Bodies are JSON objects except the file uploads (UPLOAD_ROUTES), which send the
        raw file with its details in the query string."""
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path in UPLOAD_ROUTES:
            fn = UPLOAD_ROUTES[u.path]
            return self._answer(lambda: fn(_query(u), self._raw_body()))
        prefix = next((p for p in POST_PREFIX_ROUTES if u.path.startswith(p)), None)
        fn = POST_ROUTES.get(u.path)
        if fn is None and prefix is None:
            return self._send(404, {"error": "unknown endpoint"})
        if fn is None:
            return self._answer(lambda: POST_PREFIX_ROUTES[prefix](u.path[len(prefix):], self._body()))
        self._answer(lambda: fn(self._body()))


def stop_all_children():
    """The dashboard is closing: end any analysis it started (they would otherwise run on, holding files)."""
    for j in list(JOBS.values()):
        p = j.proc
        if p is not None and p.poll() is None:
            j.stop_requested = True
            try:
                p.terminate()
            except OSError:
                pass


def main(argv=None):
    """Command line. Default: serve the dashboard on 127.0.0.1:<port> (8765) and open the browser at the URL with
    the session token. --export ANALYSIS: write <workspace>/<ANALYSIS>_report.html and exit (no server).
    --no-browser: only print the URL. Runs until Ctrl+C, then stops any analysis child it started. Returns the exit
    code: 0, 1 with one plain sentence when the workspace cannot be written or the port cannot be used, or 2 with
    one plain sentence when the --export analysis does not exist (or is archived, incomplete or a bad name)."""
    import atexit
    atexit.register(stop_all_children)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 to listen on (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="only print the dashboard's address, open no browser")
    ap.add_argument("--export", metavar="ANALYSIS", help="write a static HTML report for an analysis and exit")
    ap.add_argument("--version", action="version", version=f"NWN Module Toolkit {VERSION}")
    a = ap.parse_args(argv)
    try:
        os.makedirs(WORKSPACE, exist_ok=True)
        tempfile.TemporaryFile(dir=WORKSPACE).close()        # can files be created there (not only listed)?
    except OSError as ex:
        print(f"The toolkit cannot write to its workspace folder {WORKSPACE} ({ex.strerror or ex}): move the toolkit "
              "folder somewhere you can write to (not Program Files or Applications), or set NWN_WORKSPACE to such "
              "a folder.")
        return 1
    if "onedrive" in WORKSPACE.lower():
        # a sync client copying a live sqlite file can lock it ("database is locked") or upload a half-written copy
        print(f"Note: the workspace {WORKSPACE} is inside a OneDrive folder. OneDrive syncing can lock or damage "
              "the analysis databases - a folder outside OneDrive is safer.")
    if a.export:
        try:
            analysis_dir(a.export)                 # the same checks the dashboard makes; each refusal is one sentence
        except ValueError as ex:
            print(f"Cannot export '{a.export}': {ex} - give the name of an analysis folder in {WORKSPACE} "
                  "(the Modules page lists them)")
            return 2
        out = os.path.join(WORKSPACE, f"{a.export}_report.html")
        html = export_static(a.export)             # built first: a failure leaves no empty file behind
        with open(out + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(out + ".tmp", out)
        print(out)
        return 0
    # Loopback address only - never "" or "0.0.0.0", which would let other machines on the network connect.
    # For a remote server the dashboard is reached through an SSH tunnel instead (docs/REMOTE_SERVER.md).
    try:
        srv = _Server(("127.0.0.1", a.port), Handler)
    except OSError as ex:
        if ex.errno == errno.EADDRINUSE:
            print(f"Port {a.port} is already in use. The dashboard is probably already running - look for its window - "
                  f"or start it on another port: nwn_dashboard.py --port {a.port + 1}")
        else:
            print(f"The dashboard could not listen on 127.0.0.1:{a.port} ({ex.strerror or ex}).")
        return 1
    url = f"http://127.0.0.1:{a.port}/?t={TOKEN}"
    SERVER["srv"] = srv
    # held for the whole run: tells an installer this toolkit is running (nwn_setup.dashboard_running)
    lock = nwn_setup.hold_dashboard_lock(WORKSPACE)     # noqa: F841 - the open handle IS the lock
    print(f"NWN Module Toolkit dashboard running ({VERSION}).\n  Open: {url}\n  Workspace: {WORKSPACE}\n"
          "  Press Ctrl+C to stop.")
    if not a.no_browser:
        # a short delay so the server is listening before the browser asks for the page
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop_all_children()
        print("Stopped.")
        return 0
    # serve_forever returned: /api/setup/launch asked to hand over to the installed toolkit
    stop_all_children()
    srv.server_close()                    # free the port before the installed copy starts
    if RELAUNCH["root"]:
        name, _ = nwn_setup.launcher_for()
        started = nwn_setup.launch(RELAUNCH["root"])
        print(f"Installed in {RELAUNCH['root']}. " + ("It is starting in its own window - this window can be closed."
              if started else f"Start it with {os.path.join(RELAUNCH['root'], name)}."))
    return 0


class _Server(ThreadingHTTPServer):
    """The dashboard's HTTP server. On Windows, SO_REUSEADDR lets a second program bind a port another one is
    already listening on (on Linux and macOS it only allows a quick restart), so a second dashboard would start
    silently on the same port; there the option is left off and the bind fails with "address in use"."""
    allow_reuse_address = os.name != "nt"


if __name__ == "__main__":
    sys.exit(main())
