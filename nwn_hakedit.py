"""
nwn_hakedit.py - the hak editor: open a hak, stage changes (add / replace / remove / rename / edit 2das),
check them, rebuild a NEW .hak under a new name, and add it to the hak folder next to the original.

Why this exists
---------------
Haks are large ERF archives shared by many modules. Editing one in place is risky: a half-written hak, or a file the
module still needs removed, breaks every module that loads it. This module lets a builder change a hak as a
controlled change - staged, checked against an analysed module, rebuilt, verified - without touching the original.

Safety model (same as the rest of the toolkit):
- Opening a hak never changes it. Changes are *staged* in nwn_workspace/_haks/<project>/ until you rebuild.
- Rebuild writes a NEW .hak in the project's builds/ folder with a NEW name (the next free <hak>_rN, see
  nwn_hakslim.new_hak_names), then re-reads it and proves every file is byte-for-byte what was intended.
- Add to hak folder (the `install` action) is a separate, explicit step: the verified rebuild is copied into the original's
  folder under its new name. It is refused if a file of that name already exists; the original is never replaced.
  The module uses the new hak only once its module.ifo hak list is pointed at it - the dashboard does that as an
  edit of module.ifo in the analysis (nwn_dashboard.point_module_at_hak), never in the original module.
- Restore of a backup (backups were made by older toolkit versions, whose install replaced the hak) is the one
  remaining write over an existing hak. It first makes a verified safety backup of the current file (_swap_in).
- With an analysed module attached, removals of files the module uses are blocked, and 2da row edits know which
  rows are in use.
- One operation at a time per project (_busy): a second request fails at once rather than queueing.

Reads: the hak (read only), and optionally an analysis (index.sqlite opened read-only, catalog.json).
Writes: only under nwn_workspace/_haks/, plus the one new file Add to hak folder adds next to the original hak, the hak
file itself on Restore, and Extract into a folder the user names (refused unless it is empty or new).

Project folder layout (nwn_workspace/_haks/<project id>/):
    project.json   source path, name, size/mtime/fingerprint when opened, analysis, history
    state.json     {"files": {"name.ext": {"o": "orig name"} | {"s": "<sha>"}}, "force": [...]}
                   "o": the file comes from the source hak under that name (a different name = renamed);
                   "s": the file's content is staged/<sha>; a name missing from "files" = removed;
                   "force": removals the user confirmed although the module uses the file
    staged/<sha>   contents of added/replaced/edited files (named by their SHA-256, so identical content is stored once)
    builds/<id>/<new name>.hak + build.json
    extracted/<id>/...
Backups live outside the project folders, in nwn_workspace/_haks/_backups/<project id>/<id>/<name>.hak + backup.json,
so discarding a project never deletes them.

Main entry points: HakEditor (open, state, put/remove/rename/undo, build, install, restore, extract, load_2da,
save_2da) and Context (what an analysed module knows about a hak).

Known limits: the rebuilt hak is written uncompressed (EE compressed entries are read, not re-compressed); a
source hak that lists the same name twice keeps only the first copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager

import nwnlib as n
import nwn_2da

TEXT_EXTS = {"2da", "nss", "txt", "txi", "set", "ini", "mtr", "shd", "ltr"}    # shown as text in the dashboard
# plain-words file kind shown in the editor's file list
KIND = {"mdl": "model", "wok": "walkmesh", "pwk": "walkmesh", "dwk": "walkmesh", "tga": "texture", "dds": "texture",
        "plt": "texture", "txi": "texture info", "mtr": "material", "2da": "2da table", "nss": "script source",
        "ncs": "compiled script", "set": "tileset", "wav": "sound", "bmu": "music", "ssf": "soundset",
        "itp": "palette", "ltr": "name generator", "shd": "shader", "tlk": "talk table", "bik": "movie"}
SYSTEM_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}        # skipped when a folder is added (not game content)
# one lock per project id. The dashboard serves requests on several threads; the lock stops two of them changing
# the same project's state.json at once (e.g. a staged edit landing in the middle of a rebuild)
_LOCKS = defaultdict(threading.RLock)


@contextmanager
def _busy(pid):
    """One change at a time per hak project; a second request fails fast instead of waiting behind a long rebuild."""
    lk = _LOCKS[pid]
    if not lk.acquire(blocking=False):
        raise ValueError("this hak is busy (a rebuild, install or restore is running) - try again when it finishes")
    try:
        yield
    finally:
        lk.release()


def state_hash(state):
    """SHA-256 of a project's staged file list (state["files"], keys sorted so the hash is stable).

    Stored in build.json; install compares it with the current state to refuse a build made before later changes."""
    return hashlib.sha256(json.dumps(state["files"], sort_keys=True).encode()).hexdigest()


def _now_id():
    return time.strftime("%Y%m%d-%H%M%S")


def _new_dir(parent):
    """A new, never-used folder named by time (with -2, -3... if two are made in the same second)."""
    os.makedirs(parent, exist_ok=True)
    base = _now_id()
    for k in range(1, 1000):
        d = os.path.join(parent, base if k == 1 else f"{base}-{k}")
        try:
            os.makedirs(d)                  # no exist_ok: creating the folder is what claims the name
            return os.path.basename(d), d
        except FileExistsError:
            continue
    raise ValueError("could not create a new folder in " + parent)


# shared with nwn_install ("Add to game folders"), so both buttons place files exactly the same way (see nwnlib)
sha256_file = n.sha256_file
_copy_verified = n.copy_verified
_place_new_file = n.place_new_file
_remove_quietly = n.remove_quietly
_INSTALLING_RE = n.INSTALLING_RE


def fingerprint(path):
    """Cheap identity of a (possibly huge) file: size, mtime and a hash of its first/last MB.

    Used to notice that the source hak changed on disk since the project was opened (source_changed). A full hash
    of a multi-GB hak on every page refresh would be too slow; a change in the middle with the same size and
    mtime would not be noticed. Returns dict(size, mtime, head_tail). Raises OSError if the file is missing."""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(1 << 20))
        if st.st_size > 2 << 20:                # over 2 MB: the last MB doesn't overlap the first one
            fh.seek(-(1 << 20), 2)              # whence=2: 1 MB back from the end of the file
            h.update(fh.read(1 << 20))
    return dict(size=st.st_size, mtime=int(st.st_mtime), head_tail=h.hexdigest())


def split_name(fn):
    """'Folder/Name.TGA' -> ('name', 'tga'); without a dot -> (the file name lower-cased, '') - the folder part is
    dropped in both cases."""
    base, dot, ext = os.path.basename(fn).rpartition(".")
    return (base.lower(), ext.lower()) if dot else (ext.lower(), "")    # no dot: rpartition puts the name in ext


def name_problem(fn):
    """Plain-words reason a file name can't go into a hak, or None. Pure function; never raises."""
    base, ext = split_name(fn)
    if not ext:
        return "has no file extension"
    if ext not in n.EXT_TO_RESTYPE:                 # an ERF entry stores a numeric resource type, not the extension
        return f"'.{ext}' is not a file type NWN can load from a hak"
    if len(base) > 16:                              # the ERF key's name field is 16 bytes
        return f"the name is {len(base)} characters - NWN resource names are limited to 16"
    # safe_resref: no path characters or Windows device names; the regex also refuses anything but a-z 0-9 _ -
    if not n.safe_resref(base) or re.search(r"[^a-z0-9_\-]", base):
        return "the name has characters NWN can't load (use letters, digits, _ and -)"
    return None


def remove_stale_installing(folder, min_age=600):
    """Delete temp files an install or restore left in folder when the dashboard was closed part-way (the copy's
    finally never ran). Only names this toolkit makes (_INSTALLING_RE) are touched, never one made by this process
    (another project may be installing right now) and never one written in the last min_age seconds (another
    dashboard may still be copying). Returns the names removed. Never raises."""
    gone = []
    try:
        names = os.listdir(folder)
    except OSError:
        return gone
    for fn in names:
        m = _INSTALLING_RE.match(fn)
        p = os.path.join(folder, fn)
        try:
            if m and int(m.group(1)) != os.getpid() and os.path.isfile(p) and not os.path.islink(p) \
                    and time.time() - os.path.getmtime(p) > min_age:
                os.remove(p)
                gone.append(fn)
        except OSError:
            pass
    return gone


class _SourceHak:
    """A project's source hak for many reads in a row: opened (and its table indexed) on the first read, so reading N
    files costs one open, not N. close() when done. Read-only."""

    def __init__(self, path):
        self.path, self.erf, self.first = path, None, {}

    def read(self, name):
        """Bytes of entry `name`; the first copy when the hak lists a name twice (the one the game reads)."""
        if self.erf is None:
            self.erf = n.Erf(self.path)
            for e in self.erf.entries:
                self.first.setdefault(e.filename, e)
        e = self.first.get(name)
        if e is None:
            raise ValueError(f"{name} is no longer in {self.path} - the hak changed on disk; reopen it")
        return self.erf.read(e)

    def close(self):
        if self.erf is not None:
            self.erf.close()


# ------------------------------------------------------------------ analysis context
class Context:
    """What an analysed module knows about this hak: used files, hidden files, 2da row usage, known names, tlk.

    Attributes (all read from the analysis when the Context is made; nothing is written):
        analysis      the analysis folder's name
        this_sid      id of this hak in the index's sources table (None if the module doesn't load it)
        in_module     True when the analysed module loads this hak
        hidden_by     {"name.ext": label of a higher-priority source whose copy loads instead of this hak's}
        lower         {"name.ext": label of a lower-priority source whose copy this hak's copy hides}
        known         every "name.ext" in the module, its haks, override and (if indexed) the base game
        readers       {2da name: {scripts that read it}}, from the index's 2da_ref edges
        used          {"name.ext": True/False} for this hak's files, from catalog.json (empty without a catalogue)
        tables        {2da name: {row number: [what uses it]}} for rows in use, from catalog.json
        tables_known  names of the 2das the catalogue describes
        tlk           the module's talk tables (base + custom), loaded on first use
    """
    def __init__(self, analysis_dir, hak_path=None):
        """analysis_dir: path of an analysis folder (index.sqlite is opened read-only; catalog.json is optional).
        hak_path: the hak to describe; matched to the analysis's haks by full path, then by file name.
        hak_path=None: context for a file of the module itself (e.g. a module 2da)."""
        self.analysis = os.path.basename(os.path.normpath(analysis_dir))
        db = n.sqlite_ro(os.path.join(analysis_dir, 'index.sqlite'))
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            self.indexed_at = meta.get("indexed_at")
            rows = db.execute("SELECT * FROM sources").fetchall()
            sources = {sid: (p, k) for sid, p, k, _pr in rows}
            prio = {sid: pr for sid, _p, _k, pr in rows}
            if hak_path:
                me = os.path.abspath(hak_path).lower()
                my_base = os.path.basename(me)
                self.this_sid = next((s for s, (p, k) in sources.items() if k == "hak" and
                                      (os.path.abspath(p).lower() == me or os.path.basename(p).lower() == my_base)), None)
            else:
                my_base = None
                self.this_sid = next((s for s, (p, k) in sources.items() if k == "module"), None)
            self.in_module = self.this_sid is not None

            def rank(sid):
                """Load priority (lower wins), the game's order: haks in module.ifo order, then the module, then the
                override folder (nwnlib.source_rank)."""
                p, k = sources[sid]
                return n.source_rank(k, prio[sid])
            # not loaded: as if listed after every hak the module loads
            my_rank = rank(self.this_sid) if self.in_module else n.RANK_MODULE - 1
            self.hidden_by = {}      # name -> the higher-priority source that wins over this hak
            self.lower = {}          # name -> a lower-priority source this hak's copy hides
            self.known = set()
            for resref, ext, sid in db.execute("SELECT resref, ext, source_id FROM files"):
                nm = f"{resref}.{ext}"
                self.known.add(nm)
                if sid == self.this_sid:
                    continue
                r = rank(sid)
                label = "override folder" if sources[sid][1] == "override" else \
                    ("module" if sources[sid][1] == "module" else os.path.basename(sources[sid][0]))
                if r < my_rank:
                    if nm not in self.hidden_by or r < self.hidden_by[nm][0]:
                        self.hidden_by[nm] = (r, label)
                else:
                    self.lower.setdefault(nm, label)
            self.hidden_by = {k: v[1] for k, v in self.hidden_by.items()}
            if db.execute("SELECT name FROM sqlite_master WHERE name='base_resources'").fetchone():
                self.known |= {r[0] for r in db.execute("SELECT name FROM base_resources")}
            self.readers = {}
            for src, dst in db.execute("SELECT src, dst FROM edges WHERE kind='2da_ref'"):
                if src.startswith("script:"):
                    # node ids are "<type>:<name>": strip "2da:" and "script:" to get plain names
                    self.readers.setdefault(dst[4:], set()).add(src[7:])
            self.tlk_paths = (meta.get("tlk_base") or "", meta.get("tlk") or "")
        finally:
            db.close()
        self.used = {}
        self.tables = {}
        cat_path = os.path.join(analysis_dir, "catalog.json")
        if os.path.isfile(cat_path):
            with open(cat_path, encoding="utf-8") as fh:
                cat = json.load(fh)
            h = next((v for k, v in cat.get("haks", {}).items() if my_base and k.lower() == my_base), None)
            if h:
                for lst in h.get("inventory", {}).values():
                    for x in lst:
                        self.used[x["name"]] = bool(x.get("used"))
            self.tables = {k: {r["row"]: r.get("used_by") or [] for r in v.get("rows", []) if r.get("used")}
                           for k, v in cat.get("tables", {}).items()}
            self.tables_known = set(cat.get("tables", {}))
            # a 2da is "used" when the module uses any of its rows (the engine reads it by row number)
            for t, rows in self.tables.items():
                if rows and f"{t}.2da" in self.used:
                    self.used[f"{t}.2da"] = True
        else:
            self.tables_known = set()
        self._tlk = None

    @property
    def tlk(self):
        """The base dialog.tlk plus the module's custom tlk (nwnlib.TlkSet), read on first use - they are large."""
        if self._tlk is None:
            base = n.Tlk(self.tlk_paths[0]) if self.tlk_paths[0] and os.path.isfile(self.tlk_paths[0]) else None
            custom = n.Tlk(self.tlk_paths[1]) if self.tlk_paths[1] and os.path.isfile(self.tlk_paths[1]) else None
            self._tlk = n.TlkSet(base, custom)
        return self._tlk

    def describe(self):
        """Short summary for the dashboard: analysis name, when it was indexed, whether it loads this hak."""
        return dict(analysis=self.analysis, indexed_at=self.indexed_at, hak_in_module=self.in_module,
                    tables_with_usage=len(self.tables_known))


# ------------------------------------------------------------------ projects
class HakEditor:
    """All hak projects under <workspace>/_haks/. One instance serves the whole dashboard.

    Every method that takes a pid (project id) validates it (_dir) before using it in a path, so a request can't
    reach a folder outside _haks/. Methods that change a project hold its _busy lock; a concurrent second change
    raises ValueError("this hak is busy ...") at once."""

    def __init__(self, workspace):
        """workspace: the toolkit's nwn_workspace folder. Nothing is created until a hak is opened."""
        self.root = os.path.join(workspace, "_haks")
        # backups live OUTSIDE the project folders, so discarding a project never deletes them
        self.backup_root = os.path.join(self.root, "_backups")

    # -- project housekeeping
    def _dir(self, pid):
        """Folder of project pid. Raises ValueError for a malformed id or a project that doesn't exist."""
        # pid comes from the browser: only a-z 0-9 _ - are allowed, so it can't contain "..", "/" or "\"
        if not pid or not re.fullmatch(r"[a-z0-9_\-]+", pid):
            raise ValueError("bad project id")
        d = os.path.join(self.root, pid)
        if not os.path.isfile(os.path.join(d, "project.json")):
            raise ValueError("hak project not found")
        return d

    def project(self, pid):
        """The project's project.json as a dict (id, source, name, analysis, history ...). Read-only. Raises
        ValueError for a malformed id or a project that doesn't exist."""
        return self._load(pid)[1]

    def set_analysis(self, pid, name):
        """Attach the analysis `name` to the project for usage checks ("" or None detaches it). The name is stored as
        given (the caller checks it is a real analysis). Writes only the project's project.json, under its busy lock."""
        self._dir(pid)                          # validate the id before it becomes a lock key
        with _busy(pid):
            d, proj, _state = self._load(pid)
            proj["analysis"] = name or None
            self._save(d, proj)

    def _load(self, pid):
        """(folder, project dict, state dict) of project pid; raises ValueError for a bad or unknown id."""
        d = self._dir(pid)
        with open(os.path.join(d, "project.json"), encoding="utf-8") as fh:
            proj = json.load(fh)
        with open(os.path.join(d, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        return d, proj, state

    def _save(self, d, proj=None, state=None):
        """Write project.json and/or state.json (whichever is given)."""
        for nm, obj in (("project.json", proj), ("state.json", state)):
            if obj is not None:
                tmp = os.path.join(d, nm + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh, indent=1)
                # write-then-rename: a crash mid-write leaves the old file intact, never a half-written one
                os.replace(tmp, os.path.join(d, nm))

    def open(self, path, analysis=None):
        """Open a .hak/.erf as a project (or return the existing project for the same path).

        path: the hak file (surrounding quotes from a pasted Windows path are removed).
        analysis: name of an analysis to attach for usage checks; "" detaches it, None leaves it unchanged.
        Returns the project id. Raises ValueError for a missing file, a non-ERF file or a module (.mod etc.).
        Read-only for the hak: only the project folder is created. Unfinished install copies this toolkit left next
        to the hak (see remove_stale_installing) are deleted and noted in the project's history."""
        path = os.path.abspath(path.strip().strip('"'))
        if not os.path.isfile(path):
            raise ValueError(f"not found: {path}")
        erf = n.Erf(path)   # raises for anything that isn't an ERF
        try:
            if erf.file_type.strip().upper() not in ("HAK", "ERF") or \
                    os.path.splitext(path)[1].lower() in (".mod", ".nwm", ".sav", ".backupmod"):
                # modules are never edited in place (golden rule 1): analyse them and use edits + Build instead
                raise ValueError(f"{os.path.basename(path)} is a module ('{erf.file_type.strip()}'), not a hak - the hak "
                                 "editor only opens .hak/.erf files. Change a module with the editors and Build instead")
            # nwnlib.Erf renames an entry whose stored name is unsafe as a file name to "invalid_name_<index>" and
            # lists the real name in bad_names; such entries are left out of the project (and the rebuild)
            bad = list(erf.bad_names)
            names = [e.filename for i, e in enumerate(erf.entries) if not (bad and e.resref.startswith("invalid_name_"))]
            compressed = sum(1 for e in erf.entries if e.compressed)
            ftype, version = erf.file_type, erf.version
        finally:
            erf.close()
        del compressed      # EE compressed entries are read normally; the rebuild is written uncompressed
        name = os.path.splitext(os.path.basename(path))[0]
        # project id: readable hak name + a short hash of the full path, so the same path always maps to the same
        # project and two haks of the same name in different folders don't collide (SHA-1 only for naming)
        pid = re.sub(r"[^a-z0-9_\-]+", "_", name.lower())[:40] + "_" + hashlib.sha1(path.lower().encode()).hexdigest()[:6]
        d = os.path.join(self.root, pid)
        # an install interrupted by closing the dashboard leaves <name>.installing-... next to the hak
        stale = remove_stale_installing(os.path.dirname(path))
        if os.path.isfile(os.path.join(d, "project.json")):
            _d, proj, _s = self._load(pid)
            if analysis is not None:
                proj["analysis"] = analysis or None
            if stale:
                proj["history"].append(dict(at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                            action="removed unfinished install copies", files=stale))
            if analysis is not None or stale:
                self._save(d, proj)
            return pid
        os.makedirs(os.path.join(d, "staged"), exist_ok=True)
        dup = [k for k, c in Counter(names).items() if c > 1]
        proj = dict(id=pid, source=path, name=name, file_type=ftype, version=version, opened=_now_id(),
                    source_fp=fingerprint(path), analysis=analysis or None, duplicates_in_source=dup,
                    unsafe_in_source=bad, history=[dict(at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                                        action="removed unfinished install copies", files=stale)]
                    if stale else [])
        state = dict(files={nm: {"o": nm} for nm in names}, force=[])
        self._save(d, proj, state)
        return pid

    def list(self):
        """Every project: id, name, source, analysis, number of staged changes, opened, number of builds.
        Unreadable project folders are skipped. Read-only (opens each source hak to count changes)."""
        out = []
        if not os.path.isdir(self.root):
            return out
        for pid in sorted(os.listdir(self.root)):
            try:
                d, proj, state = self._load(pid)
            except (ValueError, OSError):
                continue
            ch = self._changes(proj, state)
            out.append(dict(id=pid, name=proj["name"], source=proj["source"], analysis=proj.get("analysis"),
                            changes=len(ch), opened=proj["opened"],
                            builds=len(os.listdir(os.path.join(d, "builds"))) if os.path.isdir(os.path.join(d, "builds")) else 0))
        return out

    def discard(self, pid):
        """Delete the project (staged changes, builds, extracted files). Backups are kept (they live elsewhere)."""
        with _busy(pid):
            d = self._dir(pid)
            n.rmtree_force(d)
        return dict(ok=True, backups_kept=os.path.join(self.backup_root, pid))

    def rebaseline(self, pid):
        """The hak changed on disk (another tool, or an interrupted install): take the file on disk as the new
        starting point. Files you added or replaced stay staged; removals and renames are reset. Backups are kept."""
        with _busy(pid):
            d, proj, state = self._load(pid)
            toc = self._toc(proj)
            files = {nm: {"o": nm} for nm in toc}
            kept = []
            for nm, f in state["files"].items():
                if "s" in f:
                    files[nm] = f
                    kept.append(nm)
            proj["source_fp"] = fingerprint(proj["source"])
            proj["history"].append(dict(at=time.strftime("%Y-%m-%d %H:%M:%S"), action="re-baselined"))
            state = dict(files=files, force=[])
            self._save(d, proj, state)
            self._gc(d, state)
            return dict(ok=True, kept_staged=kept)

    # -- reading
    def _toc(self, proj):
        """Name -> entry. If the hak lists a name twice, the FIRST copy is kept (that is the one the game reads)."""
        erf = n.Erf(proj["source"])
        try:
            toc = {}
            bad = bool(erf.bad_names)
            for e in erf.entries:
                if bad and e.resref.startswith("invalid_name_"):
                    continue
                toc.setdefault(e.filename, e)
            return toc
        finally:
            erf.close()

    def source_changed(self, proj):
        """True when the source hak's fingerprint differs from the one recorded when the project was opened (or
        the file can't be read). Never raises."""
        try:
            return fingerprint(proj["source"]) != proj["source_fp"]
        except OSError:
            return True

    def read(self, pid, name):
        """Current bytes of `name` as the project stands: the staged copy if there is one, else the source hak's.
        Raises ValueError if the name is not in the project or no longer in the source hak. Read-only.
        Opens the project and the hak for this one file; loops (extract, checks) use _read_with instead."""
        d, proj, state = self._load(pid)
        src = _SourceHak(proj["source"])
        try:
            return self._read_with(d, state, src, name)
        finally:
            src.close()

    @staticmethod
    def _read_with(d, state, src, name):
        """read() for a project already loaded (folder d, its state) with the source hak src (a _SourceHak, which
        opens the hak once, on the first file that comes from it)."""
        f = state["files"].get(name)
        if f is None:
            raise ValueError(f"{name} is not in the hak")
        if "s" in f:
            with open(os.path.join(d, "staged", f["s"]), "rb") as fh:
                return fh.read()
        return src.read(f["o"])

    # -- changes
    def _stage_blob(self, d, data):
        """Store data as staged/<sha256> (once per distinct content; written via .tmp + rename). Returns the sha."""
        sha = hashlib.sha256(data).hexdigest()
        p = os.path.join(d, "staged", sha)
        if not os.path.exists(p):
            with open(p + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(p + ".tmp", p)
        return sha

    def put(self, pid, filename, data):
        """Add a new file, or replace one with the same name (staged only; the hak is not touched).

        filename: the name to use in the hak (any folder part is ignored; lower-cased). data: the file's bytes.
        Returns dict(name, action="added"|"replaced", bytes). Raises ValueError for a name a hak can't hold."""
        prob = name_problem(filename)
        if prob:
            raise ValueError(prob)
        with _busy(pid):
            return self._put(pid, filename, data)

    def _put(self, pid, filename, data):
        d, proj, state = self._load(pid)
        base, ext = split_name(filename)
        nm = f"{base}.{ext}"
        existed = nm in state["files"]
        sha = self._stage_blob(d, data)
        state["files"][nm] = {"s": sha}
        self._save(d, state=state)
        self._gc(d, state)
        return dict(name=nm, action="replaced" if existed else "added", bytes=len(data))

    def add_path(self, pid, path):
        """Add a file, or every file in a folder (and its subfolders). Returns added/replaced/skipped lists.

        Hidden files and folders (starting with "."), symbolic links, system files (Thumbs.db...) and names a hak can't
        hold are skipped with a reason. A hak has no folders, so two files of the same name in different subfolders clash:
        the first one found is kept. The source files are only read."""
        path = os.path.abspath(path.strip().strip('"'))
        files, links = [], []
        if os.path.isfile(path):
            files = [path]
        elif os.path.isdir(path):
            # nwnlib.walk_folder skips hidden folders and never follows a link (its target could be anything on the
            # PC); files keep the walk's folder order, sorted by name within each folder
            folders = {}
            for full, _rel in n.walk_folder(path, links):
                if not os.path.basename(full).startswith("."):
                    files.append(full)
                    folders.setdefault(os.path.dirname(full), len(folders))
            files.sort(key=lambda f: (folders[os.path.dirname(f)], os.path.basename(f)))
        else:
            raise ValueError(f"not found: {path}")
        res = dict(added=[], replaced=[], skipped=[dict(file=x, why="symbolic link - not followed") for x in links])
        seen = {}
        for f in files:
            if os.path.basename(f).lower() in SYSTEM_FILES:
                res["skipped"].append(dict(file=os.path.relpath(f, path) if f != path else os.path.basename(f),
                                           why="Windows/macOS system file, not game content"))
                continue
            prob = name_problem(f)
            base, ext = split_name(f)
            nm = f"{base}.{ext}"
            if prob:
                res["skipped"].append(dict(file=os.path.relpath(f, path) if f != path else os.path.basename(f), why=prob))
                continue
            if nm in seen:
                res["skipped"].append(dict(file=os.path.relpath(f, path), why=f"same name as {seen[nm]} (first one kept)"))
                continue
            seen[nm] = os.path.relpath(f, path) if f != path else os.path.basename(f)
            with open(f, "rb") as fh:
                r = self.put(pid, f, fh.read())
            res[r["action"]].append(nm)
        return res

    def remove(self, pid, names, force=False):
        """Stage the removal of names. force=True records that the user removes them although the module uses
        them (the check then becomes a warning instead of an error). Returns dict(removed=[names])."""
        with _busy(pid):
            return self._remove(pid, names, force)

    def _remove(self, pid, names, force):
        d, proj, state = self._load(pid)
        gone = []
        for nm in names:
            if state["files"].pop(nm, None) is not None:
                gone.append(nm)
                if force and nm not in state["force"]:
                    state["force"].append(nm)
        self._save(d, state=state)
        self._gc(d, state)
        return dict(removed=gone)

    def rename(self, pid, old, new):
        """Stage a rename of `old` to `new` (same file type only). Returns dict(old, new).
        Raises ValueError for a bad new name, a missing old name or a new name already in the hak."""
        prob = name_problem(new)
        if prob:
            raise ValueError(f"{new}: {prob}")
        d, proj, state = self._load(pid)
        base, ext = split_name(new)
        new = f"{base}.{ext}"
        if old not in state["files"]:
            raise ValueError(f"{old} is not in the hak")
        if new in state["files"]:
            raise ValueError(f"{new} already exists in the hak - remove it first")
        if split_name(old)[1] != ext:
            raise ValueError("renaming can't change the file type (.%s -> .%s)" % (split_name(old)[1], ext))
        with _busy(pid):
            # checked again under the lock: another request may have changed the file list since the checks above
            d, proj, state = self._load(pid)
            if old not in state["files"] or new in state["files"]:
                raise ValueError("the file list changed - reload and try again")
            state["files"][new] = state["files"].pop(old)
            self._save(d, state=state)
        return dict(old=old, new=new)

    def undo(self, pid, names):
        """Put names back the way they are in the original hak (a renamed file goes back to its old name, an added
        file is dropped, a replaced or removed one returns to the original). Returns dict(undone=[names])."""
        with _busy(pid):
            return self._undo(pid, names)

    def _undo(self, pid, names):
        d, proj, state = self._load(pid)
        toc = self._toc(proj)
        done = []
        for nm in names:
            cur = state["files"].get(nm)
            if cur and "o" in cur and cur["o"] != nm:           # renamed: move back
                state["files"].pop(nm)
                if cur["o"] not in state["files"]:
                    state["files"][cur["o"]] = {"o": cur["o"]}
                done.append(nm)
            elif nm in toc:                                     # replaced/edited/removed original
                state["files"][nm] = {"o": nm}
                done.append(nm)
            elif cur is not None:                               # added
                state["files"].pop(nm)
                done.append(nm)
            if nm in state["force"]:
                state["force"].remove(nm)
        self._save(d, state=state)
        self._gc(d, state)
        return dict(undone=done)

    def undo_all(self, pid):
        """Drop every staged change: the project matches the source hak again. Returns dict(ok=True)."""
        with _busy(pid):
            return self._undo_all(pid)

    def _undo_all(self, pid):
        d, proj, state = self._load(pid)
        state = dict(files={nm: {"o": nm} for nm in self._toc(proj)}, force=[])
        self._save(d, state=state)
        self._gc(d, state)
        return dict(ok=True)

    def _gc(self, d, state):
        """Delete staged blobs no file refers to any more. A ".tmp" may be a write in progress, so it is left."""
        keep = {f["s"] for f in state["files"].values() if "s" in f}
        sd = os.path.join(d, "staged")
        for f in os.listdir(sd):
            if f not in keep and not f.endswith(".tmp"):
                try:
                    os.remove(os.path.join(sd, f))
                except OSError:
                    pass

    def _changes(self, proj, state, toc=None):
        """Staged changes compared with the source hak: [{name, change: renamed|replaced|added|removed, ...}]."""
        toc = toc if toc is not None else (self._toc(proj) if os.path.isfile(proj["source"]) else {})
        out = []
        renamed_from = {f["o"]: nm for nm, f in state["files"].items() if "o" in f and f["o"] != nm}
        for nm, f in sorted(state["files"].items()):
            if "o" in f and f["o"] != nm:
                out.append(dict(name=nm, change="renamed", detail=f"from {f['o']}"))
            elif "s" in f:
                out.append(dict(name=nm, change="replaced" if nm in toc else "added"))
        for nm in sorted(toc):
            if nm not in state["files"] and nm not in renamed_from:
                out.append(dict(name=nm, change="removed", forced=nm in state.get("force", [])))
        return out

    # -- the full picture
    def state(self, pid, ctx=None):
        """Everything the dashboard's Hak editor page shows for a project.

        ctx: optional Context of an attached analysis (adds "used" and "hidden_by" per file, and usage checks).
        Returns dict(project, entries, changes, checks, sizes, builds, backups, context). sizes["new"] is an
        estimate of the rebuilt hak's size. Read-only."""
        d, proj, state = self._load(pid)
        toc = self._toc(proj)
        changes = self._changes(proj, state, toc)
        chmap = {c["name"]: c for c in changes}
        staged_sizes = {}
        entries = []
        for nm, f in sorted(state["files"].items()):
            if "s" in f:
                size = staged_sizes.setdefault(f["s"], os.path.getsize(os.path.join(d, "staged", f["s"])))
            else:
                e = toc.get(f["o"])
                # the rebuild writes entries uncompressed, so a compressed entry counts at its uncompressed size
                size = (e.usize if e.compressed else e.size) if e else 0
            base, ext = split_name(nm)
            row = dict(name=nm, ext=ext, kind=KIND.get(ext, n.TYPE_NAMES.get(ext, ext)), size=size,
                       change=chmap.get(nm, {}).get("change", ""))
            if ctx:
                row["used"] = ctx.used.get(nm if "s" in f or f["o"] == nm else f["o"])
                row["hidden_by"] = ctx.hidden_by.get(nm)
            entries.append(row)
        checks = self.checks(pid, ctx, _loaded=(d, proj, state, toc, entries, changes))
        orig_bytes = os.path.getsize(proj["source"]) if os.path.isfile(proj["source"]) else 0
        # ERF V1.0 layout (as written by nwnlib.write_erf_stream): 160-byte header, then per file a 24-byte key
        # entry and an 8-byte resource entry, then the description block and the file data
        est = 160 + len(entries) * 32 + sum(e["size"] for e in entries) + len(n.erf_description_block(proj["source"])[1])
        return dict(project=dict(proj, source_changed=self.source_changed(proj)), entries=entries, changes=changes,
                    checks=checks, sizes=dict(original=orig_bytes, new=est, delta=est - orig_bytes,
                                              files_original=len(toc), files_new=len(entries)),
                    builds=self.builds(pid), backups=self.backups(pid),
                    context=ctx.describe() if ctx else None)

    def checks(self, pid, ctx=None, _loaded=None):
        """Everything worth knowing before a rebuild, in plain words. level: error (blocks rebuild) / warning / info.

        Returns [dict(level, msg, name, [fix])]; fix names the dashboard action that resolves it (force, undo,
        edit2da). _loaded must be the tuple state() passes in (d, proj, state, toc, entries, changes); calling
        this without it fails. Read-only."""
        d, proj, state, toc, entries, changes = _loaded
        out = []

        def add(level, msg, name=None, **kw):
            out.append(dict(level=level, msg=msg, name=name, **kw))
        if self.source_changed(proj):
            add("error", f"{os.path.basename(proj['source'])} changed on disk since you opened it here (another program "
                         "or an install). Close this project and open the hak again so changes aren't made on a stale copy.")
        if not entries:
            add("error", "the hak would be empty")
        for nm in (proj.get("duplicates_in_source") or []):
            add("warning", f"the original hak lists {nm} more than once; the rebuilt hak keeps the first copy "
                           "(the one the game reads)", nm)
        bad = proj.get("unsafe_in_source") or []
        if bad:
            add("warning", f"the original hak has {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} with names that can't be "
                           f"used safely ({', '.join(map(repr, bad[:4]))}{' …' if len(bad) > 4 else ''}); the rebuilt hak "
                           "leaves them out")
        for e in entries:
            prob = name_problem(e["name"])
            if prob:
                add("error", f"{e['name']}: {prob}", e["name"])
        chmap = {c["name"]: c for c in changes}
        effective = {e["name"] for e in entries}
        for c in changes:
            nm = c["name"]
            if c["change"] == "removed":
                if ctx and ctx.used.get(nm):
                    if c.get("forced"):
                        add("warning", f"{nm} is used by the module but you chose to remove it anyway", nm)
                    else:
                        add("error", f"{nm} is used by the module ({ctx.analysis}) - removing it breaks whatever uses it. "
                                     "Undo, or remove it with 'remove anyway' if you know it is not needed", nm, fix="force")
                elif ctx and nm in ctx.lower:
                    add("info", f"{nm} removed: the copy in {ctx.lower[nm]} will load instead", nm)
                elif not ctx:
                    add("info", f"{nm} removed - attach an analysed module to check whether anything uses it", nm)
            if c["change"] in ("added", "replaced", "renamed") and ctx and nm in ctx.hidden_by:
                add("warning", f"{nm}: {ctx.hidden_by[nm]} has a file with the same name that loads first, so this "
                               "copy will never be used", nm)
            # detail is "from <old name>" (see _changes): [5:] is the old name
            if c["change"] == "renamed" and ctx and ctx.used.get(c["detail"][5:]):
                add("error", f"{c['detail'][5:]} is used by the module; renaming it to {nm} breaks whatever uses the old name",
                    nm, fix="undo")
        # content checks on changed files only (the original content is what it is); the hak is opened once
        src = _SourceHak(proj["source"])
        try:
            self._content_checks(d, state, src, chmap, effective, ctx, add)
        finally:
            src.close()
        total = sum(e["size"] for e in entries)
        # ERF offsets and sizes are unsigned 32-bit numbers; warn well before the size where tools start to fail
        if total > 1_900_000_000:
            add("warning", f"the hak would be {total / 1e9:.1f} GB - very large haks are slow to download and some tools "
                           "fail above 2 GB; consider splitting it")
        if not ctx:
            add("info", "no analysed module attached - usage checks (files and 2da rows the module uses) are off")
        elif not ctx.in_module:
            add("info", f"the attached analysis ({ctx.analysis}) does not use this hak, so usage checks don't apply")
        return out

    def _content_checks(self, d, state, src, chmap, effective, ctx, add):
        """The part of checks() that reads files: each added, replaced or renamed file must parse (GFF, 2da) and an
        ASCII model's name, textures and supermodel must make sense. src: the open _SourceHak; add: checks()'s add."""
        for nm, c in chmap.items():
            if c["change"] not in ("added", "replaced", "renamed"):
                continue
            base, ext = split_name(nm)
            try:
                data = self._read_with(d, state, src, nm)
            except ValueError as ex:
                add("error", str(ex), nm)
                continue
            if ext in n.GFF_EXTENSIONS:
                try:
                    n.read_gff(data)
                except Exception as ex:  # noqa
                    add("error", f"{nm} is not a valid {ext.upper()} file: {ex}", nm)
            elif ext == "2da":
                tab, iss = nwn_2da.parse(n.decode_text(data))
                errs = [i for i in iss if i["level"] == "error"]
                if errs:
                    add("error", f"{nm}: {errs[0]['msg']} (line {errs[0].get('line')})" +
                        (f" and {len(errs) - 1} more" if len(errs) > 1 else "") + " - open it in the 2da editor", nm, fix="edit2da")
            elif ext == "mdl":
                info = n.scan_mdl(data)
                if info.get("format") == "ascii":           # binary (compiled) models are not parsed
                    if info.get("model") and info["model"] != base:
                        add("warning", f"{nm}: the model inside is named '{info['model']}' but the file is '{base}' - "
                                       "the game expects them to match", nm)
                    pool = effective | (ctx.known if ctx else set())
                    missing = [t for t in info.get("textures", [])
                               if not any(f"{t}.{x}" in pool for x in ("tga", "dds", "plt"))]
                    if missing:
                        where = "this hak, the module's other haks or the game" if ctx else "this hak"
                        add("warning" if ctx else "info", f"{nm} uses texture(s) not found in {where}: "
                            f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}", nm)
                    sup = info.get("supermodel")
                    if sup and f"{sup}.mdl" not in pool:
                        add("warning" if ctx else "info", f"{nm} needs supermodel '{sup}' which is not in "
                            f"{'this hak or anything the module loads' if ctx else 'this hak'}", nm)
            elif ext in ("tga",) and len(data) < 18:        # a TGA file starts with an 18-byte header
                add("error", f"{nm} is too small to be a texture", nm)

    # -- build / verify
    def build(self, pid, ctx=None, log=print):
        """Rebuild the hak with every staged change, under a NEW name, in the project's builds/<id>/ folder.

        ctx: optional Context (usage checks). log: callable for progress lines.
        Refuses (ValueError) while any check is an error. After writing, the new hak is re-read and every file's
        SHA-256 compared with what was intended (verify); the result is in build.json ("verified").
        Returns the build.json dict (id, file, files, bytes, sha256, verified, problems, changes, built, source_fp,
        state_hash). Never writes the source hak or anything outside the project folder."""
        with _busy(pid):
            return self._build(pid, ctx, log)

    def _build(self, pid, ctx, log):
        st = self.state(pid, ctx)
        errs = [c for c in st["checks"] if c["level"] == "error"]
        if errs:
            raise ValueError(f"{len(errs)} problem(s) must be fixed before rebuilding - first: {errs[0]['msg']}")
        d, proj, state = self._load(pid)
        bid, bd = _new_dir(os.path.join(d, "builds"))
        # a rebuilt hak always gets a NEW name (the next free <hak>_rN in the hak's folder), so it can never replace
        # the original, and the module has to be pointed at it on purpose
        # taken = every hak/erf name in the source's folder + names of this project's builds not yet installed
        # (so two uninstalled rebuilds never share a name)
        import nwn_hakslim
        stem, ext_ = os.path.splitext(os.path.basename(proj["source"]))
        taken = nwn_hakslim.taken_hak_names([proj["source"]]) | {os.path.splitext(os.path.basename(b["file"]))[0].lower()
                                                                  for b in self.builds(pid) if not b.get("installed")}
        new_stem = nwn_hakslim.new_hak_names([stem], taken)[stem]
        out = os.path.join(bd, new_stem + (ext_.lower() or ".hak"))
        erf = n.Erf(proj["source"])
        toc = {}
        for e in erf.entries:
            toc.setdefault(e.filename, e)           # first copy of a repeated name, as in _toc
        entries = []
        try:
            # entries are (resref, ext, size, read_fn): write_erf_stream calls read_fn one file at a time, so a
            # multi-GB hak is never held in memory. p=p / e=e bind the current value (a plain lambda would see
            # only the loop's last value)
            for nm, f in sorted(state["files"].items()):
                base, ext = split_name(nm)
                if "s" in f:
                    p = os.path.join(d, "staged", f["s"])
                    entries.append((base, ext, os.path.getsize(p), (lambda p=p: open(p, "rb").read())))
                else:
                    e = toc[f["o"]]
                    entries.append((base, ext, e.usize if e.compressed else e.size, (lambda e=e: erf.read(e))))
            lcount, lbytes = n.erf_description_block(proj["source"])     # keep the hak's description text
            log(f"Writing {len(entries)} files to {out} ...")
            last = [0.0]                        # a list so the nested function can update it

            def prog(i, total):
                if time.time() - last[0] > 2 or i == total:     # at most one progress line every 2 seconds
                    last[0] = time.time()
                    log(f"  {i}/{total} files written")
            # written as .tmp and renamed only when complete, so a half-written hak never carries the final name
            shas = n.write_erf_stream(out + ".tmp", entries, proj.get("file_type") or "HAK ", lcount, lbytes, prog,
                                      strref=erf.strref)
            os.replace(out + ".tmp", out)
        finally:
            erf.close()
            if os.path.exists(out + ".tmp"):
                os.remove(out + ".tmp")
        log("Verifying: re-reading the new hak and comparing every file ...")
        v = self.verify(out, [(f"{b}.{x}", s) for (b, x, _sz, _fn), s in zip(entries, shas)])
        info = dict(id=bid, file=out, files=len(entries), bytes=os.path.getsize(out), sha256=sha256_file(out),
                    verified=v["ok"], problems=v["problems"][:50], changes=st["changes"], built=time.strftime("%Y-%m-%d %H:%M:%S"),
                    source_fp=proj["source_fp"], state_hash=state_hash(state))
        with open(os.path.join(bd, "build.json"), "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        log(("VERIFIED: " if v["ok"] else "VERIFY FAILED: ") + f"{len(entries)} files, {info['bytes']:,} bytes")
        return info

    @staticmethod
    def verify(path, expected):
        """expected = [(name, sha256)] - the new hak must contain exactly these, byte for byte.

        path: the hak to check (re-read from disk, so this tests what was written, not what was in memory).
        Returns dict(ok, problems[list of plain-words strings]). Read-only."""
        problems = []
        erf = n.Erf(path)
        try:
            got = {}
            for e in erf.entries:
                got[e.filename] = hashlib.sha256(erf.read(e)).hexdigest()
            if len(erf.entries) != len(expected):
                problems.append(f"expected {len(expected)} files, found {len(erf.entries)}")
        finally:
            erf.close()
        for nm, sha in expected:
            if got.get(nm) != sha:
                problems.append(f"{nm}: {'missing' if nm not in got else 'content differs'}")
        return dict(ok=not problems, problems=problems)

    def builds(self, pid):
        """The project's rebuilds, newest first (summary of each build.json). Read-only."""
        d = self._dir(pid)
        bd = os.path.join(d, "builds")
        out = []
        if os.path.isdir(bd):
            for b in sorted(os.listdir(bd), reverse=True):
                p = os.path.join(bd, b, "build.json")
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as fh:
                        j = json.load(fh)
                    out.append(dict(id=j["id"], file=j["file"], files=j["files"], bytes=j["bytes"], verified=j["verified"],
                                    built=j["built"], changes=len(j.get("changes", [])), installed=j.get("installed")))
        return out

    def backups(self, pid):
        """The project's backups (backup.json records), newest first. Read-only."""
        self._dir(pid)                          # validates pid before it is used in a path
        bd = os.path.join(self.backup_root, pid)
        out = []
        if os.path.isdir(bd):
            for b in sorted(os.listdir(bd), reverse=True):
                p = os.path.join(bd, b, "backup.json")
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as fh:
                        out.append(json.load(fh))
        return out

    # -- install / restore (with Extract to a chosen folder, the only operations that write outside the workspace)
    def _swap_in(self, d, proj, new_file, expected_sha, reason):
        """Back up the current hak (verified), then replace it with new_file, whose bytes must hash to expected_sha
        (checked on the copy right before the swap). Returns (backup record, expected_sha).

        Used only by restore. This is the one place the hak editor overwrites an existing hak; it refuses any
        file that isn't .hak/.erf, so it can never replace a module."""
        target = proj["source"]
        if os.path.splitext(target)[1].lower() not in (".hak", ".erf"):
            raise ValueError(f"{os.path.basename(target)} is not a .hak/.erf file - the hak editor never replaces modules")
        if not os.path.isfile(target):
            raise ValueError(f"{target} no longer exists")
        bid, bd = _new_dir(os.path.join(self.backup_root, proj["id"]))
        bfile = os.path.join(bd, os.path.basename(target))
        shutil.copy2(target, bfile)
        src_sha = sha256_file(target)
        if sha256_file(bfile) != src_sha:
            shutil.rmtree(bd, ignore_errors=True)
            raise ValueError("the backup copy did not verify - nothing was changed")
        rec = dict(id=bid, file=bfile, of=target, bytes=os.path.getsize(bfile), sha256=src_sha,
                   made=time.strftime("%Y-%m-%d %H:%M:%S"), reason=reason)
        with open(os.path.join(bd, "backup.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        tmp = None
        try:
            tmp = _copy_verified(new_file, target, expected_sha,
                                 "the copied hak did not match the verified build - the original was not replaced")
            os.replace(tmp, target)
        except PermissionError:
            raise ValueError(f"The system would not let the toolkit replace {os.path.basename(target)} (the file is "
                             "open in the game, toolset or server, read-only, or held by antivirus or a sync folder). "
                             "Close them and try again. The original was not replaced (a backup was still made).")
        finally:
            _remove_quietly(tmp)
        return rec, expected_sha

    def install(self, pid, build_id):
        """Add to hak folder: copy a verified build next to the original under its new name. See _install.
        Returns dict(installed, previous, old_name, new_name). Raises ValueError when refused."""
        with _busy(pid):
            return self._install(pid, build_id)

    def _install(self, pid, build_id):
        """Copy the verified rebuild into the hak's folder under its NEW name. Nothing is replaced: if any file already
        has that name, nothing is written. The original hak stays as it is; the module uses the new hak only once it is
        pointed at it (use_in_module).

        Refused unless: the source hak is unchanged since the project was opened, the build verified, it was made
        from this version of the source and this exact staged file list, and its file still has the verified hash.
        On success the project continues from the new hak (its next rebuild becomes _r2 ...)."""
        d, proj, state = self._load(pid)
        if self.source_changed(proj):
            raise ValueError("the hak changed on disk since this project was opened - install refused. Open it again.")
        # build ids are timestamps (digits and "-"): anything else is stripped, so the id can't point outside builds/
        bd = os.path.join(d, "builds", re.sub(r"[^0-9\-]", "", str(build_id)))
        p = os.path.join(bd, "build.json")
        if not os.path.isfile(p):
            raise ValueError("no such build")
        with open(p, encoding="utf-8") as fh:
            b = json.load(fh)
        if not b.get("verified"):
            raise ValueError("that build did not pass verification - rebuild first")
        if b.get("source_fp") != proj["source_fp"]:
            raise ValueError("that build was made from an older version of the hak - rebuild first")
        if b.get("state_hash") and b["state_hash"] != state_hash(state):
            raise ValueError("you changed the file list after this build was made - rebuild first (or undo those "
                             "changes) so nothing staged is lost")
        if sha256_file(b["file"]) != b["sha256"]:
            raise ValueError("the build file changed since it was verified - rebuild first")
        new_name = os.path.basename(b["file"])
        if new_name.lower() == os.path.basename(proj["source"]).lower():
            raise ValueError("this build has the original's name (made by an older toolkit) - rebuild it to get a new name")
        dest = os.path.join(os.path.dirname(proj["source"]), new_name)
        tmp = None
        try:
            tmp = _copy_verified(b["file"], dest, b["sha256"], "the copy did not match the verified build - nothing was added")
            _place_new_file(tmp, dest)
        except PermissionError:
            raise ValueError(f"The system would not let the toolkit write {new_name} into the hak folder (the file is "
                             "open in the game, toolset or server, read-only, or held by antivirus or a sync folder). "
                             "Nothing was changed.")
        finally:
            _remove_quietly(tmp)
        b["installed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        b["installed_as"] = dest
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(b, fh, indent=1)
        old_source = proj["source"]
        proj.setdefault("original", old_source)
        # the project now continues from the new hak (the next rebuild becomes _r2); the original is untouched
        proj["source"] = dest
        proj["name"] = os.path.splitext(new_name)[0]
        proj["source_fp"] = fingerprint(dest)
        proj["history"].append(dict(at=b["installed"], action="added", build=b["id"], file=dest, previous=old_source))
        state = dict(files={nm: {"o": nm} for nm in self._toc(proj)}, force=[])
        self._save(d, proj, state)
        self._gc(d, state)
        return dict(installed=dest, previous=old_source, old_name=os.path.splitext(os.path.basename(old_source))[0],
                    new_name=proj["name"])

    def restore(self, pid, backup_id):
        """Put a backup back in place of the project's current hak. The current file is first backed up and
        verified (_swap_in), so a restore can itself be undone. The one in-place write the hak editor still makes.
        Returns dict(restored=backup id, safety_backup=record). Raises ValueError when refused."""
        with _busy(pid):
            return self._restore(pid, backup_id)

    def _restore(self, pid, backup_id):
        d, proj, state = self._load(pid)
        # same id clean-up as _install: only digits and "-" can reach the path
        bd = os.path.join(self.backup_root, pid, re.sub(r"[^0-9\-]", "", str(backup_id)))
        p = os.path.join(bd, "backup.json")
        if not os.path.isfile(p):
            raise ValueError("no such backup")
        with open(p, encoding="utf-8") as fh:
            b = json.load(fh)
        if sha256_file(b["file"]) != b["sha256"]:
            raise ValueError("that backup file is damaged (checksum differs) - not restored")
        rec, _sha = self._swap_in(d, proj, b["file"], b["sha256"], f"before restoring backup {b['id']}")
        proj["source_fp"] = fingerprint(proj["source"])
        proj["history"].append(dict(at=time.strftime("%Y-%m-%d %H:%M:%S"), action="restored", backup=b["id"],
                                    safety_backup=rec["id"]))
        state = dict(files={nm: {"o": nm} for nm in self._toc(proj)}, force=[])
        self._save(d, proj, state)
        self._gc(d, state)
        return dict(restored=b["id"], safety_backup=rec)

    # -- extract
    def extract(self, pid, names=None, dest=None):
        """Write files of the project (as they stand, staged changes included) to a folder.

        names: which files (default: all). dest: a folder of your choice, refused unless empty or new (and refused
        if it is a file); default a new extracted/<id>/ folder in the project. Only names in the project's file list
        (plain names, no folders) that a hak can hold are written, so a file name can't lead outside dest. Everything
        is written into a temporary folder next to dest first and renamed into place when complete, so a damaged
        entry leaves no half-filled folder behind (the error is raised, dest is as before).
        Returns dict(folder, files=count)."""
        d, proj, state = self._load(pid)
        if dest:
            dest = os.path.abspath(dest.strip().strip('"'))
            if os.path.isfile(dest):
                raise ValueError(f"{dest} is a file - choose an empty or new folder")
            if os.path.exists(dest) and os.listdir(dest):
                raise ValueError(f"{dest} is not empty - choose an empty or new folder (nothing is overwritten)")
        else:
            _x, dest = _new_dir(os.path.join(d, "extracted"))
        tmp = f"{dest.rstrip(os.sep)}.extracting-{os.getpid()}-{os.urandom(4).hex()}"
        os.makedirs(tmp)
        src = _SourceHak(proj["source"])
        count = 0
        try:
            for nm in names or sorted(state["files"]):
                if nm not in state["files"] or name_problem(nm):
                    continue
                with open(os.path.join(tmp, nm), "wb") as fh:
                    fh.write(self._read_with(d, state, src, nm))
                count += 1
            src.close()
            if os.path.isdir(dest):
                os.rmdir(dest)                  # empty (checked above, or just made by _new_dir); fails if not
            os.rename(tmp, dest)
        except BaseException:
            src.close()
            n.rmtree_force(tmp, ignore_errors=True)
            raise
        return dict(folder=dest, files=count)

    # -- 2da helpers
    def load_2da(self, pid, name):
        """Open a 2da of the project for the 2da editor.

        Returns (table, issues, original_table): the current table and its parse issues (nwn_2da.parse), and the
        table as it is in the source hak (the current one when there is no staged change). Read-only."""
        d, proj, state = self._load(pid)
        text = n.decode_text(self.read(pid, name))
        tab, issues = nwn_2da.parse(text)
        f = state["files"][name]
        orig_text = None
        if "s" in f:
            toc = self._toc(proj)
            if name in toc:
                erf = n.Erf(proj["source"])
                try:
                    orig_text = n.decode_text(erf.read(toc[name]))
                finally:
                    erf.close()
        orig = nwn_2da.parse(orig_text)[0] if orig_text else tab
        return tab, issues, orig

    def save_2da(self, pid, name, table):
        """Stage an edited 2da table (written as text by nwn_2da.write). Same result as put()."""
        return self.put(pid, name, n.encode_text(nwn_2da.write(table)))
