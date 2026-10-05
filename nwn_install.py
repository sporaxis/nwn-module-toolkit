"""
nwn_install.py - "Add to game folders": copy a finished clean build into the game's modules and hak folders, and undo it.

Why this exists
---------------
A clean build lives in the workspace (<analysis>/build). To play or serve it, its .mod has to go into the user's
modules folder and every rebuilt (lean) hak into the hak folder. Doing that by hand is where mistakes happen: copying
over a file of the same name, copying half a file, or forgetting which files were added. This module does that copy
as a controlled change, only when the user presses the button and confirms (the toolkit's first rule allows writes
outside the workspace only that way):
    plan_install   works out what would be copied where and refuses anything unsafe      read-only
    install        copies, verifies and records each file; never replaces anything       writes the two game folders
    undo_install   removes exactly the files it added that are still unchanged            deletes only those files

What is copied (plan_install)
    build/<name>.mod             -> <NWN user folder>/modules/<name>.mod
    build/haks/<hak>_rN.hak      -> <NWN user folder>/hak/<hak>_rN.hak     (only haks the build REBUILT; a hak the
                                    build left alone is the original the game already has)
    the custom .tlk              -> not copied: the build copies the original talk table unchanged, so the game
                                    already has it (said in the plan's notes)
Everything a build writes already has a NEW name (the clean module can't take the original's name, rebuilt haks are
<hak>_r1...), so nothing here can replace an original - and the plan is refused outright if ANY target name exists.

Safety choices
    * Refused (ValueError, one plain sentence) when: the NWN user folder is not set or its modules / hak folder is
      missing; the build has no audit or the audit's verdict is FAIL (unless allow_fail=True, which the dashboard
      offers only behind a second confirmation); a source file is missing or its SHA-256 differs from the one the
      build recorded (changes.json: mod_sha256, lean_haks[...].sha256) - the build folder changed since; any target
      name already exists in its folder (compared without regard to capitals: Windows and macOS treat Foo.mod and
      foo.mod as one file).
    * install() copies each file to a temporary name in the target folder (O_EXCL, nwnlib.copy_verified), checks the
      copy's SHA-256 against the build's record, then gives it its final name without ever replacing a file
      (nwnlib.place_new_file: a hard link fails atomically if the name exists). The same two helpers the Hak editor's
      Add to hak folder uses.
    * The record <analysis>/build/installed.json is written BEFORE the first copy and updated after each file, so a
      crash leaves an accurate list. On any failure only this run's temp file is removed; files already placed stay
      and are listed (Undo removes them).
    * undo_install() deletes only paths listed as "placed" in installed.json, only inside the two folders recorded
      there, only .mod / .hak files, and only when the file's SHA-256 still matches the record: a file you changed or
      replaced since is left alone and listed. Nothing else is ever deleted.

Reads: <analysis>/build/changes.json and audit.json (written by nwn_build / nwn_audit), the build's .mod and haks.
Writes: the user's modules and hak folders (only new files, only in install), <analysis>/build/installed.json (and
renames an earlier, not-undone record to installed_<time>.json so it is never lost).

Limits: the target folders are the standard ones inside the NWN user folder (modules, hak); a server or a second
install elsewhere needs the files copied by hand. A build made by an older toolkit (no recorded hashes) must be built
again first.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import nwnlib as n

INSTALLED = "installed.json"     # the record of the last Add to game folders, in <analysis>/build


def _load_json(path):
    """A JSON file as a dict, or None when it is missing or unreadable. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    """Write data as JSON through a .tmp file and os.replace, so a crash part-way never leaves a broken record."""
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def _norm(p):
    """A path in a form two spellings of the same place compare equal in (links resolved, case on Windows)."""
    return os.path.normcase(os.path.realpath(p))


def _names_in(folder):
    """Lower-case names of the entries in folder (empty set when it can't be listed). Read-only."""
    try:
        return {f.lower() for f in os.listdir(folder)}
    except OSError:
        return set()


def plan_install(analysis_dir, nwn_user, allow_fail=False):
    """What "Add to game folders" would copy where, after checking it is safe. Read-only: copies and writes nothing.

    analysis_dir: the analysis folder whose build/ holds the finished build. nwn_user: the NWN user folder (the one
    with modules/ and hak/). allow_fail: True lets a build whose audit verdict is FAIL through (the dashboard asks a
    second time first).
    Returns dict(analysis, build_dir, modules_dir, hak_dir, verdict, files[{kind, source, target, bytes, sha256}],
    total_bytes, notes[plain sentences], plan_id). plan_id is a hash of the file list and allow_fail: the dashboard
    sends it back with the confirmation, so what is copied is exactly what the user was shown.
    Raises ValueError with one plain sentence for every refusal listed in the module docstring."""
    build_dir = os.path.join(analysis_dir, "build")
    if not nwn_user or not str(nwn_user).strip():
        raise ValueError("Set your NWN user folder (Documents/Neverwinter Nights, the one with the modules and hak "
                         "folders) in Settings first - the build is copied into its modules and hak folders.")
    modules_dir, hak_dir = os.path.join(nwn_user, "modules"), os.path.join(nwn_user, "hak")
    for d in (modules_dir, hak_dir):
        if not os.path.isdir(d):
            raise ValueError(f"{d} does not exist - check the NWN user folder in Settings (it must hold the game's "
                             "modules and hak folders). Nothing was copied.")
    changes = _load_json(os.path.join(build_dir, "changes.json"))
    if not changes:
        raise ValueError("there is no finished build of this module - press Build clean module first")
    audit = _load_json(os.path.join(build_dir, "audit.json"))
    # the audit must be this build's: the build removes the old audit.json before it starts, and the audit records
    # the clean folder it checked
    if not audit or not audit.get("verdict") or _norm(audit.get("clean") or "") != _norm(changes.get("output") or ""):
        raise ValueError("this build has no audit result (it was built without the audit, or the audit did not "
                         "finish) - build again with the audit before adding it to the game folders")
    verdict = audit["verdict"]
    if verdict == "FAIL" and not allow_fail:
        raise ValueError("the build's audit verdict is FAIL - fix what the audit reports and build again (adding a "
                         "failed build needs a second confirmation)")
    files, notes = [], []
    mod_file = changes.get("mod_file")
    if not mod_file:
        raise ValueError("this build has no .mod (see the warnings in changes.md) - nothing to add")
    if not changes.get("mod_sha256"):
        raise ValueError("this build was made by an older toolkit version and has no recorded checksums - build "
                         "again, then add it")
    files.append(dict(kind="module", source=mod_file, target=os.path.join(modules_dir, os.path.basename(mod_file)),
                      sha256=changes["mod_sha256"]))
    lean_dir = _norm(os.path.join(build_dir, "haks"))
    unchanged = []
    for name, info in sorted((changes.get("lean_haks") or {}).items()):
        if info.get("unchanged"):
            unchanged.append(name)            # left alone by the build: the game already has the original
            continue
        src = info.get("file") or ""
        # only files the build wrote into build/haks: a record pointing anywhere else is not copied
        if os.path.dirname(_norm(src)) != lean_dir or not info.get("sha256"):
            raise ValueError(f"the build's record of the lean hak {name} is not a file in build/haks with a checksum "
                             "- build again, then add it")
        files.append(dict(kind="hak", source=src, target=os.path.join(hak_dir, os.path.basename(src)),
                          sha256=info["sha256"]))
    taken = {modules_dir: _names_in(modules_dir), hak_dir: _names_in(hak_dir)}
    for f in files:
        if not os.path.isfile(f["source"]):
            raise ValueError(f"{f['source']} is missing - build again, then add it")
        # every source is hashed again: a file changed (or a rebuild started) since the build is never copied
        if n.sha256_file(f["source"]) != f["sha256"]:
            raise ValueError(f"{os.path.basename(f['source'])} changed since the build checked it - build again, then "
                             "add it")
        f["bytes"] = os.path.getsize(f["source"])
        name = os.path.basename(f["target"])
        if name.lower() in taken[os.path.dirname(f["target"])] or os.path.lexists(f["target"]):
            raise ValueError(f"{name} already exists in {os.path.dirname(f['target'])} - nothing is ever replaced. "
                             "Build again with another name (or, if it is an earlier add of this build, press Undo "
                             "first).")
    if unchanged:
        notes.append(f"{len(unchanged)} hak(s) were left as they are by the build (nothing to drop), so they are not "
                     "copied: the game already has them - " + ", ".join(f"{h}.hak" for h in unchanged[:20]) +
                     (" ..." if len(unchanged) > 20 else ""))
    if changes.get("editor_haks"):
        notes.append("haks your module.ifo edit points at (Hak editor rebuilds) are not copied: Add to hak folder in "
                     "the Hak editor already put them in your hak folder")
    if changes.get("tlk_file"):
        # nwn_build copies the original talk table unchanged (check_tlk_target), so there is nothing new to install
        notes.append(f"the custom talk table ({os.path.basename(changes['tlk_file'])}) is not copied: the build did "
                     "not change it, so the game already has it")
    prev = _load_json(os.path.join(build_dir, INSTALLED))
    if prev and prev.get("status") != "undone":
        notes.append("an earlier Add to game folders of this module is not undone: its record is kept as "
                     "installed_<time>.json (its files stay where they are), and Undo will then undo this add")
    if verdict == "FAIL":
        notes.append("the audit verdict is FAIL - you chose to add it anyway")
    notes.append("nothing is replaced: every file goes in under a name that does not exist yet")
    key = json.dumps([[f["source"], f["target"], f["sha256"]] for f in files] + [bool(allow_fail)])
    return dict(analysis=os.path.basename(os.path.normpath(analysis_dir)), build_dir=build_dir,
                modules_dir=modules_dir, hak_dir=hak_dir, verdict=verdict, files=files,
                total_bytes=sum(f["bytes"] for f in files), notes=notes,
                plan_id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])


def install(plan, progress=None):
    """Copy the files of a plan (from plan_install) into the game folders. Never replaces a file.

    plan: the dict plan_install returned. progress(pct, text): optional callback (0-100) called before each file; an
    exception it raises (the dashboard's Stop) ends the add like any other failure.
    Writes <build>/installed.json first (status "adding", every file "pending"), then for each file: status
    "copying", the verified copy (temp name, SHA-256 checked, placed under the final name only if that name is still
    free), status "placed" with the time. At the end the record's status is "added".
    Returns the record. On a failure the record's status is "failed", the file that failed says why, the files already
    placed stay (listed in the error and the record; Undo removes them), and ValueError is raised with a plain
    sentence. Only this run's temp file is ever removed."""
    build_dir = plan["build_dir"]
    rec_path = os.path.join(build_dir, INSTALLED)
    prev = _load_json(rec_path)
    if prev and prev.get("status") != "undone":
        # an earlier add is still in the game folders: keep its record under another name rather than lose it
        os.replace(rec_path, os.path.join(build_dir, f"installed_{time.strftime('%Y%m%d-%H%M%S')}.json"))
    rec = dict(status="adding", started=time.strftime("%Y-%m-%d %H:%M:%S"), analysis=plan.get("analysis"),
               verdict=plan.get("verdict"), modules_dir=plan["modules_dir"], hak_dir=plan["hak_dir"],
               files=[dict(kind=f["kind"], source=f["source"], target=f["target"], bytes=f["bytes"],
                           sha256=f["sha256"], status="pending") for f in plan["files"]])
    _write_json(rec_path, rec)                    # the record exists before anything is copied
    total = sum(f["bytes"] for f in rec["files"]) or 1
    done = 0
    for f in rec["files"]:
        tmp = None
        try:
            if progress:
                progress(done / total * 100, f"copying {os.path.basename(f['target'])} ({f['bytes'] / 1e6:.0f} MB)")
            if os.path.dirname(f["target"]) not in (rec["modules_dir"], rec["hak_dir"]):
                raise ValueError(f"{f['target']} is not in the modules or hak folder - not copied")
            f["status"] = "copying"
            _write_json(rec_path, rec)
            tmp = n.copy_verified(f["source"], f["target"], f["sha256"],
                                  f"the copy of {os.path.basename(f['source'])} did not match the build - not added")
            n.place_new_file(tmp, f["target"], refused=(
                f"{os.path.basename(f['target'])} appeared in {os.path.dirname(f['target'])} after the plan was made - "
                "nothing was replaced"))
            f["status"] = "placed"
            f["placed"] = time.strftime("%Y-%m-%d %H:%M:%S")
            done += f["bytes"]
            _write_json(rec_path, rec)
        except BaseException as ex:
            # this file was not added (place_new_file never replaces): record why, keep the ones already placed
            f["status"] = "failed: " + (str(ex) or type(ex).__name__)
            rec["status"] = "failed"
            rec["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _write_json(rec_path, rec)
            placed = [x["target"] for x in rec["files"] if x["status"] == "placed"]
            if isinstance(ex, PermissionError):
                why = (f"the system would not let the toolkit write into {os.path.dirname(f['target'])} (read-only, "
                       "or held by the game, antivirus or a sync folder)")
            elif isinstance(ex, (ValueError, OSError)):
                why = str(ex)
            else:
                raise                                   # Stop or an interrupt: the record already says what happened
            raise ValueError(f"Add to game folders stopped: {why}. " +
                             (f"Already added (Undo removes them): {', '.join(placed)}" if placed else
                              "Nothing was added."))
        finally:
            n.remove_quietly(tmp)                         # only this run's temp file; gone already after placing
    rec["status"] = "added"
    rec["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(rec_path, rec)
    if progress:
        progress(100, "done")
    return rec


def install_state(analysis_dir):
    """The record of the last Add to game folders (installed.json) for the dashboard, or {} when there is none.
    Read-only."""
    return _load_json(os.path.join(analysis_dir, "build", INSTALLED)) or {}


def undo_install(analysis_dir):
    """Undo the last Add to game folders: remove the files it placed that are still exactly as it placed them.

    analysis_dir: the analysis folder (its build/installed.json is the record). A file is removed only when the
    record lists it as "placed", it sits in one of the two folders the record names, it is a .mod or .hak, and its
    SHA-256 still matches the record. Everything else is left alone and listed: a file you changed or replaced, one
    already gone, one the add was still copying when it stopped (it may not be ours - check it yourself).
    Returns the record, now with status "undone" (or "undo incomplete" when a file could not be removed - Undo can
    be pressed again), the time, and each file's undo result ("removed", or why it was left). Raises ValueError when there is no record or it is already undone. Deletes nothing but those files."""
    rec_path = os.path.join(analysis_dir, "build", INSTALLED)
    rec = _load_json(rec_path)
    if not rec:
        raise ValueError("there is nothing to undo - this build was not added to the game folders")
    if rec.get("status") == "undone":
        raise ValueError("the last Add to game folders was already undone")
    folders = {_norm(rec.get("modules_dir") or ""), _norm(rec.get("hak_dir") or "")}
    for f in rec.get("files") or []:
        t = f.get("target") or ""
        if f.get("status") == "copying":
            f["undo"] = "left alone: the add stopped while copying it, so it may not be the toolkit's - check it yourself"
        elif f.get("status") != "placed":
            f["undo"] = "nothing to remove: it was never added"
        elif _norm(os.path.dirname(t)) not in folders or os.path.splitext(t)[1].lower() not in (".mod", ".hak"):
            f["undo"] = "left alone: not a .mod/.hak in the modules or hak folder the add used"
        elif not os.path.isfile(t) or os.path.islink(t):
            f["undo"] = "already gone" if not os.path.lexists(t) else "left alone: it is no longer a plain file"
        elif n.sha256_file(t) != f.get("sha256"):
            f["undo"] = "left alone: it changed since it was added (yours now)"
        else:
            try:
                os.remove(t)
                f["undo"] = "removed"
            except OSError as ex:
                f["undo"] = f"left alone: could not remove it ({ex.strerror or ex}) - close the game or toolset and try again"
    # a file that could not be removed (open in the game) keeps the undo open, so pressing Undo again retries it
    rec["status"] = "undo incomplete" if any(str(f.get("undo", "")).startswith("left alone: could not remove")
                                             for f in rec.get("files") or []) else "undone"
    rec["undone"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_json(rec_path, rec)
    return rec
