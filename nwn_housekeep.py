"""
nwn_housekeep.py - tidy a modules folder full of copies (a big persistent world can have ~150 dated copies, 25-30 GB).

    scan       lists every .mod and .BackupMod in the folder (top level only), groups them into families by name
               ("PW_Main_2026-09-01", "PW_Main (3)", "PW_Main_v12" -> "pw_main"), finds byte-identical copies
               (SHA-256, only for files of equal size; hashes are cached), and suggests what to archive:
                 - identical copies (one of each set is kept)
                 - older versions beyond the newest N of each family (optionally keeping the newest of each month)
                 - toolset backups (.BackupMod) of older versions, and empty (0-byte) module files
               It never suggests a file that is: the newest of its family, the toolset backup of the newest version,
               changed in the last 2 days, used by an analysis in the workspace, or on your keep list.
    move       moves the files you ticked into an archive folder (default <modules>/_toolkit_archive/<date>)
               and writes manifest.json there first (name, from, to, size, SHA-256). Same drive = a rename;
               another drive = copy, verify SHA-256, then remove the original. A file another program holds open
               is skipped. Nothing is ever deleted outright.
    undo       moves a batch back from its manifest (never overwrites a file that has reappeared).

The game only lists modules at the top of the modules folder, so archived copies no longer clutter the list.

Why the move is so careful
--------------------------
This is one of only two places where the toolkit changes anything in the user's own game folders, and only when the
user presses Move and types MOVE. The move therefore:
    - only acts on names the user ticked, which must be plain module file names at the top of the folder;
    - writes manifest.json (the full plan, every entry "pending") and a record in batches.json BEFORE moving anything,
      and re-saves the manifest after every file, so an interrupted batch can always be undone;
    - never deletes: a cross-drive move removes the original only after the copy's SHA-256 matches, and if that
      removal fails it removes the copy instead, so exactly one copy exists whatever happens;
    - never overwrites a file at the destination (or, on undo, back in the modules folder);
    - skips a file whose size or modified time changed since the scan.

What it reads and writes
------------------------
    reads    the modules folder (top level only; sub-folders are listed but never entered), and each analysis's
             index.sqlite (read-only) to learn which module it was made from
    writes   nwn_workspace/_housekeeping/: hash_cache.json, last_scan.json, batches.json
             <archive folder>/manifest.json and the moved files themselves (move / undo only)

Public entry points
-------------------
    family_of(filename)            family name of a copy
    scan(folder, workspace, ...)   suggestions; never moves anything
    move(folder, names, workspace, ...) / undo(manifest_path, workspace) / list_batches(workspace)
    last_scan(workspace) / load_prefs(workspace) / save_prefs(workspace, prefs)   the dashboard page's saved state
The command line runs scan only; move and undo are reached from the dashboard after the user types MOVE.

Limits
------
Families are guessed from file names only. The hash cache is keyed on path, size and whole-second modified time, so a
file rewritten within the same second at the same size would reuse its old hash.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = "_toolkit_archive"
RECENT_DAYS = 2          # a file changed this recently may be the one being worked on: never suggested
# words that mark the end of the family name, like a digit does: "PW_Main copy.mod" -> "pw_main"
VERSION_WORDS = {"copy", "backup", "bak", "old", "new", "final", "latest", "test", "wip", "save", "autosave"}
MODULE_EXTS = (".mod", ".backupmod")    # lower case; .BackupMod is the toolset's automatic backup of a module


def family_of(filename):
    """The name a set of copies shares: the words before the first date/version part.
    'PW_Main_202202_8193_34_1f_head_1a.mod' -> 'pw_main'
    '20081105_Dark Shore - Lost Coast.mod' -> 'dark_shore_lost_coast'   'Aeon_PW_302b.mod' -> 'aeon_pw'
    A name with no word before its first number keeps its whole lower-case stem ('2024.mod' -> '2024'). Never raises."""
    stem = os.path.splitext(os.path.basename(filename))[0].lower()
    # split on runs of spaces, underscores, hyphens, dots and brackets: "pw_main (3)" -> ["pw", "main", "3"]
    tokens = [t for t in re.split(r"[\s_\-.()]+", stem) if t]
    while tokens and re.search(r"\d", tokens[0]):
        tokens.pop(0)                       # a leading date: 20081105_Dark Shore...
    words = []
    for t in tokens:
        if re.search(r"\d", t) or t in VERSION_WORDS:
            break
        words.append(t)
    return "_".join(words) or stem


def _sha256(path, progress=None, stop=None):
    """SHA-256 of a file as hex, read in 4 MB pieces (module copies can be several hundred MB each).
    progress(nbytes) is called after each piece; stop() returning True raises InterruptedError. Read-only."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if stop and stop():
                raise InterruptedError("stopped")
            b = fh.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


def _load(path, default):
    """JSON from `path`, or `default` if the file is missing or not valid JSON. Never raises on bad files."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    """Write JSON atomically: to a temporary file in the same folder, then rename it over `path`. A crash part-way
    leaves the old file intact, which matters for manifest.json and batches.json (they are what Undo relies on)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def state_dir(workspace):
    """Folder for this module's own state files: <workspace>/_housekeeping."""
    return os.path.join(workspace, "_housekeeping")


def last_scan(workspace):
    """The last scan saved in the workspace (scan's result, with summary.folder), or None when there is none."""
    return _load(os.path.join(state_dir(workspace), "last_scan.json"), None)


def load_prefs(workspace):
    """The Modules folder page's saved choices (folder, keep_newest, keep_monthly, keep_names); {} when none."""
    return _load(os.path.join(state_dir(workspace), "prefs.json"), {})


def save_prefs(workspace, prefs):
    """Save the Modules folder page's choices (a dict) in the workspace, atomically."""
    _save(os.path.join(state_dir(workspace), "prefs.json"), prefs)


def analysed_modules(workspace):
    """Module paths that analyses in the workspace were made from (kept by default).

    Returns {normalised absolute module path: analysis folder name}. Each index.sqlite is opened read-only with a
    short timeout; an analysis whose database is locked or damaged is simply not listed. Writes nothing."""
    import sqlite3
    out = {}
    if not os.path.isdir(workspace):
        return out
    for nm in os.listdir(workspace):
        p = os.path.join(workspace, nm, "index.sqlite")
        if os.path.isfile(p):
            try:
                import nwnlib
                db = nwnlib.sqlite_ro(p, timeout=2)
                r = db.execute("SELECT value FROM meta WHERE key='module_path'").fetchone()
                db.close()
                if r and r[0]:
                    out[os.path.normcase(os.path.abspath(r[0]))] = nm
            except sqlite3.Error:
                pass
    return out


def scan(folder, workspace, keep_newest=3, keep_monthly=False, keep_names=(), progress=None, stop=None):
    """Look at every module file at the top of `folder` and suggest which copies could be archived.

    folder: the modules folder. workspace: nwn_workspace (hash cache, saved scan, analyses to protect).
    keep_newest: how many of the newest .mod files of each family to keep. keep_monthly: also keep the newest of
    each calendar month. keep_names: file names (any case) never to suggest.
    progress(pct, text) and stop() are optional dashboard callbacks; stop() returning True raises InterruptedError.
    Returns dict(summary, files, identical, families) and saves the same as _housekeeping/last_scan.json.
    Read-only on the modules folder: it never moves, renames or deletes anything there. Raises ValueError if
    `folder` is not a folder."""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError(f"not a folder: {folder}")
    cache_path = os.path.join(state_dir(workspace), "hash_cache.json")
    cache = _load(cache_path, {})
    files, skipped_dirs = [], []
    for e in os.scandir(folder):
        # follow_symlinks=False: a link is neither listed nor followed, so a scan can never reach outside the folder
        if e.is_file(follow_symlinks=False) and e.name.lower().endswith(MODULE_EXTS):
            st = e.stat(follow_symlinks=False)
            files.append(dict(name=e.name, path=e.path, size=st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns,
                              family=family_of(e.name), backup=e.name.lower().endswith(".backupmod")))
        elif e.is_dir(follow_symlinks=False):
            skipped_dirs.append(e.name)
    # identical copies are only possible between files of the same size: hash just those
    by_size = {}
    for f in files:
        by_size.setdefault(f["size"], []).append(f)
    to_hash = [f for grp in by_size.values() if len(grp) > 1 for f in grp if f["size"] > 0]
    total = sum(f["size"] for f in to_hash) or 1
    done = [0]

    def tick(nb):
        done[0] += nb
        if progress:
            progress(done[0] / total * 100, f"checking identical copies: {done[0] / 1e9:.1f} of {total / 1e9:.1f} GB")
    for f in to_hash:
        # hashing ~25 GB takes minutes; a file with the same path, size and modified time is assumed unchanged.
        # The time is kept to the nanosecond: a same-size rewrite within the same second must not reuse the old hash
        key = f"{os.path.normcase(f['path'])}|{f['size']}|{f['mtime_ns']}"
        if key in cache:
            f["sha256"] = cache[key]
            tick(f["size"])
        else:
            f["sha256"] = _sha256(f["path"], tick, stop)
            cache[key] = f["sha256"]
    _save(cache_path, cache)
    analysed = analysed_modules(workspace)
    keep_set = {k.lower() for k in keep_names}
    now = time.time()
    fams = {}
    for f in files:
        fams.setdefault(f["family"], []).append(f)
    for fam, members in fams.items():
        mods = sorted([f for f in members if not f["backup"]], key=lambda x: -x["mtime"])
        newest_mod = mods[0]["name"] if mods else None
        seen_months = set()
        rank = 0                                # position among the family's .mod files, newest first (no backups)
        for f in sorted(members, key=lambda x: -x["mtime"]):
            # strong = reasons that override every archive reason, including "identical copy" below
            reasons_keep, reasons_arch, strong = [], [], []
            month = _dt.datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m")
            if now - f["mtime"] < RECENT_DAYS * 86400:
                strong.append(f"changed in the last {RECENT_DAYS} days")
            if os.path.normcase(os.path.abspath(f["path"])) in analysed:
                strong.append(f"analysed as '{analysed[os.path.normcase(os.path.abspath(f['path']))]}'")
            if f["name"].lower() in keep_set:
                strong.append("on your keep list")
            if f["size"] == 0:
                reasons_arch.append("empty file (0 bytes) - a save that failed")
            elif f["backup"]:
                # the toolset names its backup after the module: "X.BackupMod" belongs to "X.mod"
                own = os.path.splitext(f["name"])[0] + ".mod"
                if newest_mod and own.lower() == newest_mod.lower():
                    reasons_keep.append("the toolset's backup of your newest version")
                else:
                    reasons_arch.append("toolset backup (.BackupMod) of an older version")
            else:
                if rank == 0:
                    reasons_keep.append("newest of its family")
                elif rank < keep_newest:
                    reasons_keep.append(f"one of the {keep_newest} newest")
                if keep_monthly and month not in seen_months:
                    reasons_keep.append(f"newest of {month}")
                seen_months.add(month)
                rank += 1
                if not reasons_keep:
                    reasons_arch.append(f"older version ({rank} of {len(mods)} in '{fam}')")
            reasons_keep += strong
            f["strong"] = strong
            if strong:
                reasons_arch = []
            f["keep_reasons"], f["archive_reasons"] = reasons_keep, reasons_arch
    # identical copies: keep the one we keep anyway (else the newest); suggest the rest
    groups = {}
    for f in files:
        if f.get("sha256"):
            groups.setdefault(f["sha256"], []).append(f)
    ident = []
    for sha, grp in groups.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda x: (x["backup"], not x["keep_reasons"], -x["mtime"]))   # a real .mod keeps over a backup
        keeper = grp[0]
        for f in grp[1:]:
            f["identical_to"] = keeper["name"]
            # the newest file of a family is never suggested - even when a byte-identical copy exists under another
            # name (e.g. 'Test Server.mod' copied from the live module): the live name is what the server loads
            newest = "newest of its family" in f["keep_reasons"]
            if not f["strong"] and not newest:   # an identical copy is kept anyway, so "one of the N newest" alone doesn't
                f["archive_reasons"] = [f"identical to {keeper['name']}"]
                f["keep_reasons"] = []
        ident.append(dict(sha256=sha, files=[x["name"] for x in grp], keeper=keeper["name"], size=keeper["size"]))
    for f in files:
        f["suggest"] = bool(f["archive_reasons"]) and not f["keep_reasons"]
        f["modified"] = _dt.datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
    files.sort(key=lambda x: (x["family"], -x["mtime"]))
    summary = dict(folder=folder, files=len(files), backups=sum(1 for f in files if f["backup"]),
                   folders_left_alone=sorted(skipped_dirs), bytes=sum(f["size"] for f in files), families=len(fams),
                   identical_sets=len(ident), suggested=sum(1 for f in files if f["suggest"]),
                   suggested_bytes=sum(f["size"] for f in files if f["suggest"]),
                   options=dict(keep_newest=keep_newest, keep_monthly=keep_monthly, keep_names=sorted(keep_set)),
                   scanned_at=_dt.datetime.now().isoformat(timespec="seconds"))
    result = dict(summary=summary, files=files, identical=ident,
                  families=[dict(family=k, count=len(v), bytes=sum(x["size"] for x in v)) for k, v in
                            sorted(fams.items(), key=lambda kv: -sum(x["size"] for x in kv[1]))])
    _save(os.path.join(state_dir(workspace), "last_scan.json"), result)
    return result


def _same_volume(a, b):
    """True if both paths are on the same drive (same st_dev), so a move is a plain rename. False if unsure."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _safe_progress(progress):
    """Progress callbacks may raise (the dashboard's Stop does): never let that interrupt a file half-way.

    Returns (call, state): call(pct, msg) forwards to `progress` and swallows its errors; after the first error it
    sets state["stop"], which the move loop checks before starting the next file."""
    state = {"stop": False}

    def call(pct, msg):
        if progress and not state["stop"]:
            try:
                progress(pct, msg)
            except Exception:  # noqa - treat as "stop after this file"
                state["stop"] = True
    return call, state


def _copy_verified(src, dst, stop=None):
    """Copy src to dst through a temporary name; the SHA-256 must match before dst appears. Returns the SHA-256.

    Raises OSError if the copy does not verify, InterruptedError if stopped; either way the partial copy is removed
    and src is untouched."""
    sha = _sha256(src, None, stop)
    tmp = dst + ".toolkit-partial"
    try:
        shutil.copy2(src, tmp)                    # copy2 keeps the modified time: the copy still sorts by age
        if _sha256(tmp, None, stop) != sha:
            raise OSError("the copy did not verify")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return sha


def _move_file(src, dst, same_volume, stop=None):
    """Move one file; exactly one copy exists afterwards whatever fails. Returns the SHA-256 when it was copied.

    Same drive: a rename (instant, no hash, returns None). Other drive: verified copy, then remove the original.
    Never overwrites dst (FileExistsError) and never deletes the only copy of a file."""
    if os.path.exists(dst):
        raise FileExistsError(f"{os.path.basename(dst)} already exists in the destination")
    if same_volume:
        os.rename(src, dst)                       # fails if another program has the file open
        return None
    sha = _copy_verified(src, dst, stop)
    try:
        os.remove(src)                            # safe: dst is a verified identical copy
    except OSError:
        os.remove(dst)                            # keep exactly one copy
        raise
    return sha


def move(folder, names, workspace, dest=None, progress=None, stop=None, expected=None):
    """Move the named module files (from `folder`, top level) into an archive folder. Returns the batch record.
    `expected` = {name: (size, mtime)} from the scan: a file that changed since then is skipped.

    folder: the modules folder. names: plain file names in it. workspace: nwn_workspace (batches.json lives there).
    dest: archive folder; default <folder>/_toolkit_archive/<date_time>, and it must not already hold a batch.
    progress(pct, text) / stop(): optional dashboard callbacks; Stop takes effect between files, never mid-file.
    Returns the batch dict saved as <dest>/manifest.json; each entry's status is "moved", "skipped: <why>" or
    "not moved (stopped)". One file failing does not stop the others. Raises ValueError (before anything is
    written) if a name is not a plain module file name in `folder`. Never deletes and never overwrites a file."""
    folder = os.path.abspath(folder)
    names = list(dict.fromkeys(names))          # drop repeats, keep the order
    if not names:
        raise ValueError("nothing ticked")
    for nm in names:
        # a bare name only: "..\\x.mod" or a full path could otherwise move a file from outside the modules folder
        if os.path.basename(nm) != nm or not nm.lower().endswith(MODULE_EXTS):
            raise ValueError(f"not a module file name: {nm!r}")
        if not os.path.isfile(os.path.join(folder, nm)):
            raise ValueError(f"{nm} is not in {folder}")
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    if dest:
        dest = os.path.abspath(dest)
    else:
        dest = os.path.join(folder, ARCHIVE_DIR, stamp)
        k = 2
        while os.path.exists(dest):           # two batches in the same second get separate folders
            dest = os.path.join(folder, ARCHIVE_DIR, f"{stamp}_{k}")
            k += 1
    if os.path.normcase(dest) == os.path.normcase(folder):
        raise ValueError("the archive folder must not be the modules folder itself")
    os.makedirs(dest, exist_ok=True)
    manifest_path = os.path.join(dest, "manifest.json")
    # one batch per folder: a second batch would overwrite the first manifest and break its Undo
    if os.path.exists(manifest_path) or any(e.name.lower().endswith(MODULE_EXTS) for e in os.scandir(dest)):
        raise ValueError(f"{dest} already holds an archive batch - pick an empty folder")
    entries = [dict(name=nm, src=os.path.join(folder, nm), dst=os.path.join(dest, nm),
                    size=os.path.getsize(os.path.join(folder, nm)), status="pending") for nm in names]
    batch = dict(id=stamp, folder=folder, dest=dest, created=_dt.datetime.now().isoformat(timespec="seconds"),
                 entries=entries, undone=False)
    _save(manifest_path, batch)            # the plan is on disk before anything moves
    batches = _load(os.path.join(state_dir(workspace), "batches.json"), [])
    rec = dict(id=batch["id"], folder=folder, dest=dest, manifest=manifest_path, created=batch["created"],
               moved=0, moved_bytes=0, undone=False)
    batches.append(rec)                    # recorded up front, so Undo is offered whatever happens next
    _save(os.path.join(state_dir(workspace), "batches.json"), batches)
    say, st = _safe_progress(progress)
    same = _same_volume(folder, dest)
    total = sum(e["size"] for e in entries) or 1
    done = 0
    try:
        for e in entries:
            if st["stop"] or (stop and stop()):
                e["status"] = "not moved (stopped)"
                continue
            try:
                if expected and e["name"] in expected:
                    # the user ticked what the scan showed; a file saved since then may be a new version
                    size, mt = expected[e["name"]]
                    cur = os.stat(e["src"])
                    if cur.st_size != size or int(cur.st_mtime) != int(mt):
                        raise OSError("it changed since the scan - scan again")
                e["sha256"] = _move_file(e["src"], e["dst"], same, stop)
                e["status"] = "moved"
            except (OSError, InterruptedError) as ex:
                e["status"] = f"skipped: {getattr(ex, 'strerror', None) or ex}" + \
                    (" (open in another program?)" if isinstance(ex, PermissionError) else "")
            _save(manifest_path, batch)    # before anything else can interrupt
            done += e["size"]
            say(done / total * 100, f"{e['name']}: {e['status']}")
    finally:
        # runs even if the loop is interrupted, so the manifest and the batch record always show the real outcome
        batch["moved"] = sum(1 for e in entries if e["status"] == "moved")
        batch["moved_bytes"] = sum(e["size"] for e in entries if e["status"] == "moved")
        _save(manifest_path, batch)
        rec.update(moved=batch["moved"], moved_bytes=batch["moved_bytes"])
        _save(os.path.join(state_dir(workspace), "batches.json"), batches)
        _forget_in_last_scan(workspace, folder)
    return batch


def _forget_in_last_scan(workspace, folder):
    """After a move or an undo the saved scan no longer matches the folder: drop it, so the page asks for a new scan
    instead of offering files that are gone."""
    p = os.path.join(state_dir(workspace), "last_scan.json")
    last = _load(p, None)
    if last and os.path.normcase(os.path.abspath(last.get("summary", {}).get("folder", ""))) == os.path.normcase(folder):
        try:
            os.remove(p)
        except OSError:
            pass


def list_batches(workspace):
    """Every archive batch this toolkit has recorded (from _housekeeping/batches.json), oldest first; [] if none.
    Each is dict(id, folder, dest, manifest, created, moved, moved_bytes, undone[, put_back]). Read-only."""
    return _load(os.path.join(state_dir(workspace), "batches.json"), [])


def _needs_putting_back(e):
    """True if manifest entry `e` is a file that is still in the archive folder and should go back.
    "moved (...)" statuses are ones an earlier undo could not put back, so a later undo tries them again."""
    if e["status"] == "moved" or e["status"].startswith("moved ("):
        return True
    # a move interrupted between the file and the manifest: trust the disk
    return e["status"] == "pending" and os.path.isfile(e["dst"]) and not os.path.exists(e["src"])


def undo(manifest_path, workspace, progress=None):
    """Move the files of one archive batch back into the modules folder they came from.

    manifest_path: the batch's manifest.json, exactly as recorded in batches.json. workspace: nwn_workspace.
    Returns the updated batch dict (also saved to the manifest). Each entry ends "put back", or "moved (not put
    back: <why>)" when a file of that name is already back in the modules folder or the archived file is missing.
    Never overwrites and never deletes. Raises ValueError for a manifest this toolkit did not record.
    The manifest stays in the archive folder afterwards as the record of what happened."""
    # absolute from here on: the manifest is saved back under this path, and batches.json is matched by it
    manifest_path = os.path.abspath(manifest_path)
    # only batches this toolkit recorded can be undone, and only between the folders it recorded: the manifest is a
    # file in the archive folder, so its own paths are not trusted (an edited manifest must not move other files)
    rec = next((b for b in list_batches(workspace)
                if os.path.normcase(os.path.abspath(b["manifest"])) == os.path.normcase(os.path.abspath(manifest_path))), None)
    if not rec:
        raise ValueError("that is not an archive batch recorded by this toolkit")
    batch = _load(manifest_path, None)
    if not batch:
        raise ValueError(f"no manifest at {manifest_path}")
    folder, dest = rec["folder"], rec["dest"]
    say, _st = _safe_progress(progress)
    try:
        for e in batch["entries"]:
            nm = os.path.basename(str(e.get("name") or ""))
            if not nm or nm != e.get("name") or not nm.lower().endswith(MODULE_EXTS):
                e["status"] = "moved (not put back: the manifest entry is not a module file name)"
                continue
            e["src"], e["dst"] = os.path.join(folder, nm), os.path.join(dest, nm)   # rebuilt from the recorded folders
            if not _needs_putting_back(e):
                continue
            if os.path.exists(e["src"]):
                e["status"] = "moved (not put back: a file with that name is back in the modules folder)"
            elif not os.path.isfile(e["dst"]):
                e["status"] = "moved (not put back: it is no longer in the archive folder)"
            else:
                try:
                    same = _same_volume(os.path.dirname(e["dst"]), os.path.dirname(e["src"]))
                    sha = _move_file(e["dst"], e["src"], same)
                    # a hash exists only when both moves were cross-drive copies; a difference is reported, not refused
                    if sha and e.get("sha256") and sha != e["sha256"]:
                        e["status"] = "put back (note: its content differs from when it was archived)"
                    else:
                        e["status"] = "put back"
                except OSError as ex:
                    e["status"] = f"moved (put back failed: {ex.strerror or ex})"
            _save(manifest_path, batch)
            say(None, f"{e['name']}: {e['status']}")
    finally:
        batch["undone"] = not any(_needs_putting_back(e) for e in batch["entries"])
        _save(manifest_path, batch)
        batches = list_batches(workspace)
        for b in batches:
            if os.path.normcase(os.path.abspath(b["manifest"])) == os.path.normcase(manifest_path):
                b["undone"] = batch["undone"]
                b["put_back"] = sum(1 for e in batch["entries"] if e["status"].startswith("put back"))
        _save(os.path.join(state_dir(workspace), "batches.json"), batches)
        _forget_in_last_scan(workspace, os.path.abspath(folder))
    return batch       # the manifest stays in the archive folder as the record of what happened


def main(argv=None):
    """Command line: scan a modules folder and print what could be archived (scan only - moving is done from the
    dashboard's Modules folder page). Returns the exit code: 0 scanned, 2 the folder does not exist or is not a
    folder (one line says so)."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the modules folder to scan (only read)")
    ap.add_argument("--keep-newest", type=int, default=3, help="copies to keep per family, newest first (default 3)")
    ap.add_argument("--keep-monthly", action="store_true", help="also keep the newest copy of each month")
    ap.add_argument("--workspace", default=os.environ.get("NWN_WORKSPACE", os.path.join(HERE, "nwn_workspace")),
                    help="the toolkit's workspace folder, where the scan is saved")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.folder):
        print(f"Not found: {a.folder} - give the modules folder (put it in quotes if it contains spaces)")
        return 2
    r = scan(a.folder, a.workspace, a.keep_newest, a.keep_monthly)
    s = r["summary"]
    print(f"{s['files']} modules, {s['bytes'] / 1e9:.1f} GB in {s['families']} families; {s['identical_sets']} sets of "
          f"identical copies; suggest archiving {s['suggested']} ({s['suggested_bytes'] / 1e9:.1f} GB)")
    for f in r["files"]:
        print(("ARCHIVE " if f["suggest"] else "keep    ") + f"{f['name']:50s} {f['size'] / 1e6:9.1f} MB  "
              + "; ".join(f["archive_reasons"] if f["suggest"] else f["keep_reasons"] or f["archive_reasons"]))
    print("(scan only - move files from the dashboard's Modules folder page)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
