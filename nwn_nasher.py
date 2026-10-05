"""
nwn_nasher.py - a nasher project (a module kept as JSON text files for git) read as a module.

nasher (and neverwinter.nim's nwn_gff) keep each GFF resource as JSON text: module.ifo as module.ifo.json, an item
as sword.uti.json, an area as area001.are.json + area001.git.json + area001.gic.json, usually sorted into sub-folders
(src/uti/, src/are/ ...). Scripts (.nss), tables (.2da) and other files stay as they are.

The toolkit analyses such a project by converting it into an ordinary module folder inside the analysis folder
(CONVERTED): each <name>.<ext>.json becomes the binary <name>.<ext>, every other game file is copied, all in one flat
folder, as `nasher pack` would put them into the .mod. Your project folder is only read. Builds then start from the
converted copy and give a normal module folder and .mod; analysing again converts the project afresh.

    is_project(path)          True for a project folder (nasher.cfg, module.ifo.json or src/ on top) holding
                              module.ifo.json near its top, and no module.ifo
    resource_name(filename)   ("sword", "uti", True) for sword.uti.json; ("x", "nss", False) for x.nss; None otherwise
    list_resources(path)      the resources the project holds, by game name (names and sizes only)
    read_ifo(path)            the project's module.ifo (from module.ifo.json)
    convert(path, dest)       write the converted module folder; returns what was converted, copied and skipped
    export(module, dest)      the reverse: a module folder or .mod written as a nasher project (src/<ext>/...)
"""
from __future__ import annotations

import json
import os
import shutil

import nwnlib as n

CONVERTED = "module_from_json"     # the converted copy, inside the analysis folder
IFO_JSON = "module.ifo.json"


def resource_name(filename):
    """(resref, ext, is_json) for a file a module can hold: <resref>.<gff ext>.json (nwn_gff JSON) or a plain game
    file (<resref>.<ext> with a known resource type). None for anything else (README.md, nasher.cfg, a .json that is
    not a GFF resource). resref and ext are lower case."""
    fn = filename.lower()
    if fn.startswith("."):
        return None
    if fn.endswith(".json"):
        base, dot, ext = fn[:-5].rpartition(".")
        return (base, ext, True) if dot and base and ext in n.GFF_EXTENSIONS else None
    base, dot, ext = fn.rpartition(".")
    return (base, ext, False) if dot and base and ext in n.EXT_TO_RESTYPE else None


IFO_DEPTH = 3       # how deep module.ifo.json is looked for: top, src/, src/ifo/, src/module/ifo/ ...


def _find_ifo(path):
    """Relative path of the module.ifo.json nearest the top of path (at most IFO_DEPTH folders down; hidden folders
    and links skipped), or None. Depth-limited so that browsing a big folder (a home folder) stays quick."""
    level = [""]
    for _ in range(IFO_DEPTH + 1):
        nxt = []
        for rel in sorted(level):
            try:
                entries = sorted(os.scandir(os.path.join(path, rel)), key=lambda e: e.name.lower())
            except OSError:
                continue
            for e in entries:
                if e.name.startswith(".") or e.is_symlink():
                    continue
                if e.is_file() and e.name.lower() == IFO_JSON:
                    return os.path.join(rel, e.name)
                if e.is_dir() and not n._is_link(e.path):
                    nxt.append(os.path.join(rel, e.name))
        level = nxt
    return None


def is_project(path):
    """True when path is a folder holding module.ifo.json (anywhere inside) and no module.ifo at its top - a
    folder with a binary module.ifo is an ordinary unpacked module. Never raises."""
    try:
        if not os.path.isdir(path) or os.path.isfile(os.path.join(path, "module.ifo")):
            return False
        # only a folder that looks like a project is searched (nasher.cfg, module.ifo.json or src/ at its top), so
        # browsing an ordinary folder never walks it
        top = {e.lower() for e in os.listdir(path)}
        if not top & {"nasher.cfg", IFO_JSON, "src"}:
            return False
        return _find_ifo(path) is not None
    except OSError:
        return False


def list_resources(path):
    """(items, skipped) for a nasher project folder. items: [(resref, ext, size, relpath, is_json)] sorted by
    relpath (the first copy of a name wins when the project holds the same name twice, as in convert()); skipped:
    relpaths of files that are not game resources (nasher.cfg, README ...). Sizes are the files' own (JSON text is
    larger than the binary it becomes). Names only: no file is opened."""
    items, skipped = [], []
    for full, rel in sorted(n.walk_folder(path), key=lambda x: x[1]):
        r = resource_name(os.path.basename(full))
        if r is None:
            skipped.append(rel)
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        items.append((r[0], r[1], size, rel, r[2]))
    return items, skipped


def read_json_gff(path, relpath):
    """The binary GFF bytes of one nwn_gff JSON file inside path. Raises ValueError (bad JSON, unknown field type,
    wrong layout) with the file's name in the message."""
    try:
        j = json.loads(n.decode_text(n.read_file_inside(path, relpath)))
        if not isinstance(j, dict):
            raise ValueError("the top level is not a JSON object")
        return n.write_gff(n.gff_from_json(j))
    except (ValueError, KeyError, TypeError, AttributeError, n.GffError) as ex:
        raise ValueError(f"{relpath}: not a readable nwn_gff JSON file ({type(ex).__name__}: {ex})") from None


def read_ifo(path):
    """The project's module.ifo as a parsed GFF (nwnlib.read_gff), or None when there is none. Raises ValueError when
    it can't be read."""
    rel = _find_ifo(path)
    return n.read_gff(read_json_gff(path, rel)) if rel else None


def convert(path, dest, stop=None):
    """Convert the project at `path` into an ordinary module folder `dest` (replaced whole; written as dest + ".part"
    first, so a failed run never leaves half a folder under the final name).

    Returns dict(source, dest, converted, copied, skipped [relpaths], clashes [(name, used, ignored)],
    errors [text]). A JSON file that can't be read is left out and listed in errors (the analysis reports it); the
    same name twice (e.g. src/uti/x.uti.json and old/x.uti.json) keeps the first in path order and lists the clash.
    stop: optional callable; True stops between files (InterruptedError). Reads `path` only; writes only `dest`."""
    items, skipped = list_resources(path)
    part = dest + ".part"
    if os.path.isdir(part):
        shutil.rmtree(part)
    os.makedirs(part)
    seen, clashes, errors = {}, [], []
    converted = copied = 0
    for resref, ext, _size, rel, is_json in items:
        if stop and stop():
            shutil.rmtree(part, ignore_errors=True)
            raise InterruptedError("stopped")
        name = f"{resref}.{ext}"
        if name in seen:
            clashes.append((name, seen[name], rel))
            continue
        try:
            data = read_json_gff(path, rel) if is_json else n.read_file_inside(path, rel)
        except (OSError, ValueError) as ex:
            errors.append(str(ex))
            continue
        seen[name] = rel
        with open(os.path.join(part, name), "wb") as fh:
            fh.write(data)
        if is_json:
            converted += 1
        else:
            copied += 1
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.replace(part, dest)
    return dict(source=os.path.abspath(path), dest=os.path.abspath(dest), converted=converted, copied=copied,
                skipped=skipped, clashes=clashes, errors=errors)


EXPORT_NAME = "{}_nasher"          # the export of build <name>, beside it in the analysis's build folder
NASHER_CFG = """# Starter nasher.cfg written by the NWN Module Toolkit export. Check it against your nasher version and
# your own layout rules before using it; nasher's documentation describes every setting.
[package]
name = "{name}"
description = ""

[package.sources]
include = "src/**/*"

[package.rules]
"*" = "src/$ext"

[target]
name = "default"
file = "{file}.mod"
description = ""
"""


def _our_export(folder):
    """True for a folder an earlier export() wrote (an empty folder counts too): its nasher.cfg starts with the
    export's own first line. Never raises."""
    try:
        if os.path.isdir(folder) and not os.listdir(folder):
            return True
        with open(os.path.join(folder, "nasher.cfg"), encoding="utf-8") as fh:
            return fh.readline() == NASHER_CFG.splitlines(True)[0]
    except OSError:
        return False


def _module_files(module):
    """[(resref, ext, loader)] of a module folder (walked as the index walks it) or a .mod (ERF entries)."""
    if os.path.isdir(module):
        out = []
        for full, rel in sorted(n.walk_folder(module), key=lambda x: x[1]):
            base, dot, ext = os.path.basename(full).rpartition(".")
            if dot and base and ext.lower() in n.EXT_TO_RESTYPE:
                out.append((base.lower(), ext.lower(), lambda r=rel: n.read_file_inside(module, r)))
        return out, None
    erf = n.Erf(module)
    return [(e.resref, e.ext, (lambda e=e: erf.read(e))) for e in erf.entries], erf


def export(module, dest, name=None, stop=None):
    """Write the module (folder or .mod) as a nasher project in `dest` (replaced whole; written as dest + ".part"
    first): every GFF file as src/<ext>/<name>.<ext>.json (nwn_gff JSON, 2-space indent - convert() and nasher read
    it back to the same bytes), every other game file as src/<ext>/<name>.<ext>, plus a starter nasher.cfg.

    name: the module's name for nasher.cfg (default: the folder's or file's name). Returns dict(dest, json, files,
    errors [text]): a GFF file that can't be parsed is written unchanged as a plain file and listed in errors.
    stop: optional callable; True stops between files (InterruptedError). Reads `module` only; writes only `dest`."""
    if os.path.exists(dest) and not _our_export(dest):
        raise ValueError(f"{dest} already exists and is not an earlier export from this toolkit - pick a new folder "
                         "(an export replaces its folder whole)")
    src_abs, dest_abs = (os.path.normcase(os.path.abspath(x)) for x in (module, dest))
    if dest_abs == src_abs or dest_abs.startswith(src_abs + os.sep):
        raise ValueError("the export can't go inside the module it reads - pick a folder outside it")
    items, erf = _module_files(module)
    stem = os.path.splitext(os.path.basename(os.path.normpath(module)))[0]
    part = dest + ".part"
    if os.path.isdir(part):
        shutil.rmtree(part)
    os.makedirs(os.path.join(part, "src"))
    njson = nfiles = 0
    errors = []
    try:
        for resref, ext, loader in items:
            if stop and stop():
                raise InterruptedError("stopped")
            data = loader()
            d = os.path.join(part, "src", ext)
            os.makedirs(d, exist_ok=True)
            if ext in n.GFF_EXTENSIONS:
                try:
                    text = json.dumps(n.gff_to_json(n.read_gff(data)), indent=2, ensure_ascii=False) + "\n"
                except Exception as ex:  # noqa - a damaged file is kept as it is, and reported
                    errors.append(f"{resref}.{ext}: not a readable GFF file ({ex}) - copied unchanged")
                else:
                    with open(os.path.join(d, f"{resref}.{ext}.json"), "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(text)
                    njson += 1
                    continue
            with open(os.path.join(d, f"{resref}.{ext}"), "wb") as fh:
                fh.write(data)
            nfiles += 1
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    finally:
        if erf is not None:
            erf.close()
    with open(os.path.join(part, "nasher.cfg"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(NASHER_CFG.format(name=(name or stem).replace('"', "'"), file=stem))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.replace(part, dest)
    return dict(dest=os.path.abspath(dest), json=njson, files=nfiles, errors=errors)


def main(argv=None):
    """Command line: export a module as a nasher project, or convert a project into a module folder."""
    import argparse
    ap = argparse.ArgumentParser(description="nasher projects: export a module as one, or convert one to a module "
                                             "folder. The source is only read.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="module folder or .mod -> nasher project folder (new, or an earlier export)")
    e.add_argument("module")
    e.add_argument("dest")
    c = sub.add_parser("convert", help="nasher project -> module folder (new, or an earlier conversion)")
    c.add_argument("project")
    c.add_argument("dest")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            r = export(a.module, a.dest)
            print(f"{r['json']} JSON file(s) and {r['files']} other file(s) written to {r['dest']}")
        else:
            if not is_project(a.project):
                raise ValueError(f"{a.project} is not a nasher project (no module.ifo.json near its top)")
            if os.path.exists(a.dest) and os.listdir(a.dest) and not os.path.isfile(os.path.join(a.dest, "module.ifo")):
                raise ValueError(f"{a.dest} already holds other files - pick a new folder")
            r = convert(a.project, a.dest)
            print(f"{r['converted']} JSON file(s) converted and {r['copied']} copied into {r['dest']}")
            for name, used, ignored in r["clashes"]:
                print(f"  {name} twice: {used} used, {ignored} ignored")
        for x in r["errors"]:
            print("  " + x)
        return 0
    except (OSError, ValueError) as ex:
        print(f"error: {ex}")
        return 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
