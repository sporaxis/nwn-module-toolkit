"""
nwn_archive.py - pack an analysis you are not working on into one zip, and unpack it again when you need it.

A large persistent world's analysis is ~1.4 GB (the index is most of it); zipped it is ~0.25 GB. Only what an analysis
run generates is packed (index.sqlite, report.json, catalog.json, compile.json, palette.json, json/, conversations/,
reports/). Your own work - edits/, build/, descriptions.json, accepted_issues.json - stays where it is, untouched.

Archive: write analysis_archive.zip (standard zip, Deflate: Windows Explorer and 7-Zip open it) under a temporary name,
re-read it and check every file's CRC and size, rename it into place, write archive.json (what was packed, sizes,
SHA-256 of the zip), and only then remove the originals.
Unpack: check the zip's SHA-256 against archive.json, extract into a temporary folder (refusing any path that would
land outside the analysis folder), check every file, move the files into place, then remove the zip.

Safety rules the code keeps
---------------------------
    - Originals are removed only after the zip has been re-read and verified (every CRC and every size) AND
      archive.json has been written. If anything fails before that, the temporary zip is removed and the
      analysis is left exactly as it was.
    - A running or unfinished analysis (.incomplete, index.state, or a live .run.lock) is never archived.
    - Unpacking refuses an entry that is an absolute path, contains "..", would land outside the temporary folder,
      or is not one of the names this module packs ("zip slip" protection: a tampered zip cannot write elsewhere).
    - Unpacking never overwrites: a packed name already in the analysis folder is kept only when every file under
      it matches the zip (size and CRC) - then it is simply skipped, which lets an interrupted Unpack resume;
      otherwise unpacking stops.
    - A half-done Archive (zip and archive.json written, some originals not removed) is finished by pressing
      Archive again: the zip is re-checked against archive.json and only files the zip holds are removed.
    - It works only inside the analysis folder given; it never touches a module, hak or game file.

Public entry points: archive(d), unarchive(d), is_archived(d), stub(d). Command line:
    python nwn_archive.py archive|unpack <analysis folder>

Limits: the zip is checked against its own CRCs and sizes and, on unpack, against the SHA-256 recorded when it was
written; nothing can tell whether the analysis was already wrong before it was packed.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import zipfile

import nwnlib

ZIP = "analysis_archive.zip"
STUB = "archive.json"            # left in the analysis folder while it is packed: what was packed and the zip's SHA-256
# Only what an analysis run regenerates is packed; these names are also the allow-list for unpacking.
PACK_FILES = ("index.sqlite", "report.json", "catalog.json", "compile.json", "palette.json")
PACK_DIRS = ("json", "conversations", "reports")
BLOCKERS = (".incomplete", "index.state")   # markers of an unfinished or checkpointed analysis run


def _sha256(path):
    """SHA-256 of a file as hex, read in 4 MB pieces so a large zip is never held in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def is_archived(d):
    """True if analysis folder `d` is packed: archive.json is there and index.sqlite is not. Read-only."""
    return os.path.isfile(os.path.join(d, STUB)) and not os.path.isfile(os.path.join(d, "index.sqlite"))


def stub(d):
    """The archive.json record of folder `d` as a dict (archived_at, module_name, module_path, indexed_at,
    original_bytes, archive_bytes, files, sha256, members), or None if missing or unreadable. Never raises."""
    try:
        with open(os.path.join(d, STUB), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _members(d):
    """[(name inside the zip, full path)] for everything to pack. Zip names always use "/" whatever the OS."""
    out = []
    for f in PACK_FILES:
        p = os.path.join(d, f)
        if os.path.isfile(p):
            out.append((f, p))
    for sub in PACK_DIRS:
        root = os.path.join(d, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                out.append((os.path.relpath(p, d).replace(os.sep, "/"), p))
    return out


def _file_crc(path):
    """CRC-32 of a file (zlib.crc32, the same check a zip entry carries), read in 4 MB pieces."""
    import zlib
    crc = 0
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            crc = zlib.crc32(b, crc)
    return crc


def _files_under(d, top):
    """{zip-style name: full path} of the file `top` or every file inside folder `top` in analysis folder `d`."""
    p = os.path.join(d, top)
    if os.path.isfile(p):
        return {top: p}
    out = {}
    for dirpath, _dirs, files in os.walk(p):
        for fn in files:
            out[os.path.relpath(os.path.join(dirpath, fn), d).replace(os.sep, "/")] = os.path.join(dirpath, fn)
    return out


def _mismatch(d, top, packed):
    """Why the packed name `top` on disk is not what the zip holds, or None when every file under it matches an
    entry of `packed` ({zip name: ZipInfo}) in size and CRC-32 and the zip lacks none of them. Reads the files."""
    on_disk = _files_under(d, top)
    mine = {k: v for k, v in packed.items() if k.split("/")[0] == top}
    if set(on_disk) != set(mine):
        return "its files differ from the zip's"
    for name, path in on_disk.items():
        if os.path.getsize(path) != mine[name].file_size:
            return f"{name} differs from the zip (size)"
        if _file_crc(path) != mine[name].CRC:
            return f"{name} differs from the zip (content)"
    return None


def _remove_originals(d, packed, progress=None, verify=False):
    """Remove the packed files of folder `d`: the PACK_FILES / PACK_DIRS present among `packed` ({zip name: ZipInfo}
    of the verified zip). verify=True (finishing an earlier, half-done Archive) first checks each name against the
    zip (_mismatch) and leaves alone anything that differs, since the zip could not give it back.
    Raises OSError listing what was not removed (nothing else is undone). Never touches other names."""
    if progress:
        progress(96, "removing the unpacked copies")
    tops = {k.split("/")[0] for k in packed}
    left, kept = [], []
    for f in PACK_FILES + PACK_DIRS:
        p = os.path.join(d, f)
        if not os.path.exists(p):
            continue
        why = _mismatch(d, f, packed) if verify else (None if f in tops else "the zip does not hold it")
        if why:
            kept.append(f"{f} ({why})")
            continue
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                left.append(f)
        else:
            # rmtree_force also clears Windows read-only flags that make a plain rmtree fail
            nwnlib.rmtree_force(p, ignore_errors=True)
            if os.path.exists(p):
                left.append(f + "/")
    if kept:
        raise OSError("the archive is written and checked, but these were not removed because the zip could not give "
                      f"them back: {'; '.join(kept)}. Move them away (or Unpack, then Archive again).")
    if left:
        raise OSError("the archive is written and checked, but these could not be removed (open in another program - "
                      f"the dashboard, a database viewer?): {', '.join(left)}. Close it and press Archive again.")


def _summary_head(d):
    """module name etc. from the start of report.json (cheap).

    report.json can be hundreds of MB, so only its first 4000 characters are read. nwn_analysis writes it on one
    line with "summary" first and module_name, module_path, indexed_at as the summary's first keys. Returns {} if
    that shape is not found. Never raises on a missing or odd file."""
    import re
    try:
        with open(os.path.join(d, "report.json"), encoding="utf-8") as fh:
            head = fh.read(4000)
        # captures '{"module_name": "x", "module_path": "...", "indexed_at": "2026-..."' (up to the first indexed_at);
        # adding "}" turns it into a small JSON object of just those keys
        m = re.search(r'"summary":\s*(\{.*?"indexed_at":\s*"[^"]*")', head)
        return json.loads(m.group(1) + "}") if m else {}
    except (OSError, ValueError):
        return {}


def archive(d, progress=None, stop=None):
    """Pack the generated files of finished analysis folder `d` into analysis_archive.zip and remove the unpacked
    copies. edits/, build/ and the user's other files are never packed or touched.

    progress(pct, text) and stop() are optional callbacks; stop() returning True raises InterruptedError.
    Returns the archive.json record (without the member list).
    Raises ValueError if the analysis is running, unfinished, already archived or has no index.sqlite; OSError
    if the zip does not verify (nothing has been removed then) or some originals could not be removed.
    When an earlier Archive wrote the zip and archive.json but could not remove every original, this call finishes
    that removal instead of packing again (the zip's SHA-256 must still match archive.json), so a good zip is never
    replaced by one made from what was left."""
    d = os.path.abspath(d)
    import nwn_progress
    if any(os.path.exists(os.path.join(d, b)) for b in BLOCKERS) or nwn_progress.run_lock_held(d):
        raise ValueError("this analysis is running or unfinished - only finished analyses can be archived")
    info = stub(d)
    zp = os.path.join(d, ZIP)
    if info and os.path.isfile(zp) and any(os.path.exists(os.path.join(d, f)) for f in PACK_FILES + PACK_DIRS):
        # half-done: finish removing the leftovers (see the docstring)
        if progress:
            progress(2, "checking the zip")
        if info.get("sha256") and _sha256(zp) != info["sha256"]:
            raise OSError("archive.json and analysis_archive.zip do not belong together (SHA-256 differs) - not touching "
                          "anything; move one of them away")
        with zipfile.ZipFile(zp) as z:
            packed = {i.filename: i for i in z.infolist() if not i.is_dir()}
        _remove_originals(d, packed, progress, verify=True)
        if progress:
            progress(100, "archived")
        return {k: v for k, v in info.items() if k != "members"}
    if is_archived(d):
        raise ValueError("already archived")
    if not os.path.isfile(os.path.join(d, "index.sqlite")):
        raise ValueError("nothing to archive (no index.sqlite)")
    members = _members(d)
    total = sum(os.path.getsize(p) for _a, p in members) or 1
    head = _summary_head(d)
    tmp = os.path.join(d, ZIP + ".tmp")          # the real name appears only once the zip has verified
    done = 0
    try:
        # Deflate keeps it openable by Explorer and 7-Zip; level 6 is zlib's usual size/speed balance;
        # Zip64 is needed once an index passes 4 GB or the zip holds more than 65,535 files
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
            for arc, p in members:
                if stop and stop():
                    raise InterruptedError("stopped")
                z.write(p, arc)
                done += os.path.getsize(p)
                if progress:
                    progress(done / total * 90, f"packing {arc}")
        if progress:
            progress(92, "checking the zip")
        with zipfile.ZipFile(tmp) as z:
            bad = z.testzip()                          # reads everything back and checks each CRC
            if bad:
                raise OSError(f"the zip did not verify ({bad})")
            packed = {i.filename: i for i in z.infolist() if not i.is_dir()}
            sizes = {k: i.file_size for k, i in packed.items()}
        # the CRC proves each entry reads back as written; the size check also catches a file that is missing from
        # the zip or that changed on disk while it was being packed
        for arc, p in members:
            if sizes.get(arc) != os.path.getsize(p):
                raise OSError(f"the zip did not verify ({arc} size differs)")
        os.replace(tmp, os.path.join(d, ZIP))
    except BaseException:                           # also Stop and Ctrl+C: never leave a half-written zip behind
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    zp = os.path.join(d, ZIP)
    info = dict(archived_at=_dt.datetime.now().isoformat(timespec="seconds"), module_name=head.get("module_name"),
                module_path=head.get("module_path"), indexed_at=head.get("indexed_at"), original_bytes=total,
                archive_bytes=os.path.getsize(zp), files=len(members), sha256=_sha256(zp),
                members=[a for a, _p in members])
    with open(os.path.join(d, STUB + ".tmp"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1)
    os.replace(os.path.join(d, STUB + ".tmp"), os.path.join(d, STUB))
    # only now, with a verified zip and its record on disk, are the unpacked copies removed
    _remove_originals(d, packed, progress)
    if progress:
        progress(100, "archived")
    return {k: v for k, v in info.items() if k != "members"}


def _verified_in_place(d, z):
    """The top-level packed names (PACK_FILES / PACK_DIRS) already present in folder `d` whose every file matches
    the open zip `z` (same size and CRC-32, and no file the zip lacks) - what an interrupted Unpack had already moved
    into place. An empty leftover folder is removed (harmless). Raises ValueError for a name that is present but
    does not match, so nothing is ever overwritten. Read-only apart from that rmdir."""
    packed = {i.filename: i for i in z.infolist() if not i.is_dir()}
    ok = set()
    for f in PACK_FILES + PACK_DIRS:
        p = os.path.join(d, f)
        if os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)
            continue
        if not os.path.exists(p):
            continue
        why = _mismatch(d, f, packed)
        if why:
            raise ValueError(f"{f} already exists here and {why} (left over from an earlier run) - move it away, "
                             "then Unpack")
        ok.add(f)
    return ok


def unarchive(d, progress=None, stop=None):
    """Unpack analysis_archive.zip in folder `d` back into place, then remove the zip and archive.json.

    progress(pct, text) and stop() are optional callbacks; stop() returning True raises InterruptedError.
    Returns dict(unpacked=number of files, bytes=original size).
    Raises ValueError if `d` is not archived or a packed name already exists there and differs from the zip
    (nothing is overwritten; a name whose files all match the zip is left in place and skipped, so an Unpack that
    was interrupted can be run again); OSError if the zip's SHA-256 differs from archive.json, an entry is unsafe
    or unexpected, or a file unpacks to the wrong size; zipfile.BadZipFile on a CRC mismatch. The zip is removed
    only after every file is in place."""
    d = os.path.abspath(d)
    info = stub(d)
    zp = os.path.join(d, ZIP)
    if not info or not os.path.isfile(zp):
        raise ValueError("not an archived analysis")
    if progress:
        progress(2, "checking the zip")
    if info.get("sha256") and _sha256(zp) != info["sha256"]:
        raise OSError("the archive is not the one that was written (SHA-256 differs) - not unpacking")
    with zipfile.ZipFile(zp) as z:
        in_place = _verified_in_place(d, z)
    # extract into a private sub-folder first, so a failure part-way leaves no half-unpacked analysis behind
    work = os.path.join(d, ".unpack_tmp")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        with zipfile.ZipFile(zp) as z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos) or 1
            done = 0
            for i in infos:
                if stop and stop():
                    raise InterruptedError("stopped")
                name = i.filename
                if name.split("/")[0] in in_place:
                    done += i.file_size
                    continue                  # already there and verified (an earlier Unpack got this far)
                target = os.path.abspath(os.path.join(work, name))
                # zip slip: refuse "/etc/x", "\\x", "json/../../x", anything whose resolved path is not inside
                # `work` (this also catches "C:/x", which os.path.join treats as absolute on Windows), and any
                # top-level name this module never packs (e.g. "edits/x" must not replace the user's edits)
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or \
                        not target.startswith(os.path.abspath(work) + os.sep) or \
                        name.split("/")[0] not in set(PACK_FILES) | set(PACK_DIRS):
                    raise OSError(f"unexpected entry in the archive: {name!r} - not unpacking")
                if i.is_dir():
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(i) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 4 * 1024 * 1024)      # zipfile checks the CRC at the end
                if os.path.getsize(target) != i.file_size:
                    raise OSError(f"{name} unpacked to the wrong size")
                done += i.file_size
                if progress:
                    progress(5 + done / total * 90, f"unpacking {name}")
        # same drive, so each move is a rename; nothing of these names exists in `d` (checked in _verified_in_place)
        for entry in os.listdir(work):
            os.replace(os.path.join(work, entry), os.path.join(d, entry))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    os.remove(zp)
    os.remove(os.path.join(d, STUB))
    if progress:
        progress(100, "unpacked")
    return dict(unpacked=info.get("files"), bytes=info.get("original_bytes"))


def main(argv=None):
    """Command line: archive or unpack one analysis folder, printing progress and then the result as JSON. Returns
    the exit code: 0 done, 1 refused or failed (the reason is printed), 2 the folder does not exist."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["archive", "unpack"], help="archive: zip the analysis data; unpack: restore it")
    ap.add_argument("folder", help="the analysis folder (nwn_workspace/<name>)")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.folder):
        print(f"Not found: {a.folder} - give an analysis folder inside nwn_workspace")
        return 2
    fn = archive if a.action == "archive" else unarchive
    try:
        r = fn(a.folder, lambda p, m: print(f"{p:5.1f}% {m}"))
    except (ValueError, OSError) as ex:     # a refusal or a failed check is already a plain sentence
        print(f"Not done: {ex}")
        return 1
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
