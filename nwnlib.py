"""
nwnlib.py - pure-Python readers/writers for Neverwinter Nights: Enhanced Edition files.

No third-party packages needed (Python 3.8+ standard library only).

Why this exists: every other part of the toolkit works on modules, haks and game data through these readers, so the
toolkit can run on any machine with plain Python and never needs the game's own tools to read a file.

Formats covered:
  * GFF v3.2  (.are .git .gic .ifo .uti .utc .utp .utd .ute .utm .uts .utt .utw
               .dlg .jrl .fac .itp .bic ...)   read + write (read_gff / write_gff, JSON via gff_to_json)
  * ERF V1.0 / E1.0 (.mod .hak .erf .nwm)      read (E1.0 compressed entries through nwn_zstd); write V1.0 only
  * 2DA V2.0 text                              read (the 2da editor uses nwn_2da instead)
  * TLK V3.0                                   read + write
  * KEY / BIF V1 (the game's key files)        read: which names the base game ships, and their bytes (BaseGame)
  * TGA (uncompressed / RLE)                   decode to a PNG thumbnail for the dashboard
  * ASCII .mdl                                 light scan (model name, supermodel, textures)

The JSON produced by gff_to_json() uses the same layout as the official
neverwinter.nim `nwn_gff` tool, so files can be round-tripped with either tool.

Sources for the binary layouts: BioWare's Aurora file-format documents (GFF, ERF, TLK, KEY/BIF) and, for the EE
additions (ERF E1.0, compressed entries), neverwinter.nim (erf.nim, compressedbuf.nim). All numbers in these files are
little-endian.

What it writes: only the files a caller asks for (write_erf, write_erf_stream, write_tlk, write_key_bif,
Erf.extract_all, copy_verified / place_new_file), at the path the caller gives. Readers never modify the file they read.

Also here, because every module needs the same answer: source_rank / source_rank_sql (which copy of a resource the
game uses when several sources carry it), walk_folder / read_file_inside (reading a module or hak folder without
following links out of it), and copy_verified / place_new_file / sha256_file (putting a file into a user's folder
without ever replacing one - the Hak editor's Add to hak folder and nwn_install's Add to game folders).

Safety choices (the files may come from anywhere, so they are treated as untrusted):
  * Every GFF/ERF table must fit inside the file, and a GFF may not reuse structs, so a damaged or hostile header
    can't make the reader allocate gigabytes or loop for ever.
  * ERF entry names that could escape a folder or are Windows device names (safe_resref) are renamed
    "invalid_name_<n>" and listed in Erf.bad_names; the writers refuse them outright.
  * A compressed entry may not declare a larger size than the ERF's own table gives it, and that table may not give
    it more than MAX_ENTRY_SIZE (see Erf.read for the limits); no entry may reach past the end of the file.
  * A GFF nested deeper than MAX_GFF_DEPTH structs is a GffError (read, write and JSON), not a RecursionError.
  * A symbolic link in a module or hak folder is never followed (walk_folder, read_file_inside).
  * The one external program (neverwinter.nim's nwn_erf, a fallback for compressed entries) runs with an argument
    list (no shell) in a temporary folder that is deleted afterwards.

Known limits: binary (compiled) .mdl files are only recognised, not parsed; KEY/BIF files in neverwinter.nim's "E1"
layout are not read; .dds and colour-mapped TGA have no preview.
"""
from __future__ import annotations

import base64
import io
import os
import re
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

ENCODING = "cp1252"  # NWN's on-disk text encoding for western languages
import codecs as _codecs


def _nwn_decode_err(e):
    # bytes cp1252 leaves undefined (0x81 0x8D 0x8F 0x90 0x9D) -> private-use chars, so text round-trips
    return chr(0xF700 + e.object[e.start]), e.start + 1


def _nwn_encode_err(e):
    # the reverse: a private-use char U+F780..U+F7FF goes back to its original byte; anything else cp1252 can't hold
    # becomes "?"
    o = ord(e.object[e.start])
    if 0xF780 <= o <= 0xF7FF:
        return bytes([o - 0xF700]), e.start + 1
    return b"?", e.start + 1


_codecs.register_error("nwn_lossless_dec", _nwn_decode_err)
_codecs.register_error("nwn_lossless_enc", _nwn_encode_err)


def sqlite_ro(path, **kw):
    """Open an SQLite file READ-ONLY. The path goes through a proper file: URI (pathlib escapes #, ? and %), so a
    folder name with those characters can't turn it into a read-write open that creates a new file elsewhere.

    path: the database file; **kw is passed to sqlite3.connect. Returns a sqlite3.Connection on which every write
    fails ("mode=ro"). Raises sqlite3.OperationalError if the file can't be opened."""
    import pathlib
    import sqlite3
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True, **kw)


def rmtree_force(path, ignore_errors=False):
    """Delete a folder tree, also on Windows where read-only files (git marks its object files read-only) make
    shutil.rmtree fail with 'Access is denied': clear the read-only flag (and the parent folder's) and retry.

    ignore_errors=True: never raises (anything left behind stays). Deletes only `path` and what is inside it."""
    import shutil
    import stat
    import sys

    def fix(func, p, _exc):
        # called by rmtree when deleting p failed: make p and its folder writable, then try the same call again
        # (a second failure ends the rmtree and is raised below unless ignore_errors)
        for q in (os.path.dirname(p), p):
            try:
                os.chmod(q, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(q) else 0))
            except OSError:
                pass
        func(p)
    try:
        if sys.version_info >= (3, 12):                  # onerror is deprecated from 3.12; onexc replaces it
            shutil.rmtree(path, onexc=fix)
        else:
            shutil.rmtree(path, onerror=fix)
    except OSError:
        if not ignore_errors:
            raise


def sha256_file(path, chunk=1 << 20):
    """SHA-256 (hex) of a file, read 1 MB at a time so a large hak never sits in memory whole. Raises OSError."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --- placing a file in a folder the user owns (Hak editor "Add to hak folder", "Add to game folders") ------------
# Shared so both buttons follow exactly the same steps: copy to a unique temp name, check its SHA-256, then give it the
# final name without ever replacing a file that already has it.

# the temp-file name copy_verified gives a copy on its way into a user folder: <final name>.installing-<pid>-<8 hex>
INSTALLING_RE = re.compile(r"^.+\.installing-(\d+)-[0-9a-f]{8}$")


def copy_verified(src, dest, sha, mismatch):
    """Copy src to a new temp file next to dest and check the copy's SHA-256 is sha. Returns the temp path.

    The temp file sits in dest's folder (same drive, so the caller's rename into place is one step) under a unique
    name; O_EXCL: fail rather than open a file that already exists; O_BINARY (Windows only): no newline translation.
    Raises ValueError(mismatch) when the copy doesn't match (the temp file is removed); OSError (PermissionError
    included) passes through for the caller to explain. The caller renames the temp file into place and removes it
    if it is still there (remove_quietly)."""
    import shutil
    tmp = f"{dest}.installing-{os.getpid()}-{os.urandom(4).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out, 1 << 20)
        if sha256_file(tmp) != sha:              # the copy is checked, not just the source
            raise ValueError(mismatch)
    except BaseException:
        remove_quietly(tmp)
        raise
    return tmp


def place_new_file(tmp, dest, refused=None):
    """Give the finished file `tmp` the name `dest` without ever replacing a file that already has that name.

    On Windows os.rename refuses an existing destination by itself. On Linux/macOS it would silently replace it, and
    an exists() check followed by a rename leaves a gap in which another process could create dest; so there a hard
    link is made instead (os.link fails atomically with FileExistsError when dest exists) and tmp is unlinked
    afterwards. File systems without hard links (some network shares) fall back to the checked rename.
    refused: the message of the ValueError raised when dest exists (default: the Hak editor's wording).
    Raises that ValueError (nothing written) when dest exists. The caller removes tmp if it is still there."""
    refused = ValueError(refused or f"{os.path.basename(dest)} already exists in the hak folder - nothing was "
                                    "overwritten. Rebuild to get the next free name.")
    if os.path.exists(dest):
        raise refused
    if os.name == "nt":
        os.rename(tmp, dest)
        return
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise refused
    except OSError:                 # no hard links here: the exists() check above is the only guard
        os.rename(tmp, dest)
        return
    os.unlink(tmp)


def remove_quietly(p):
    """Delete file p if it exists; never raises (a leftover temp file is harmless and cleaned up later)."""
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _is_link(p):
    # a Windows junction is a folder link that os.path.islink does not report (os.path.isjunction exists from 3.12)
    return os.path.islink(p) or getattr(os.path, "isjunction", lambda _p: False)(p)


def walk_folder(folder, links=None):
    """Yield (full path, path relative to folder) for every file under a module/hak/override folder, in os.walk order.

    Hidden folders (.git ...) are not entered. Symbolic links and junctions, to files or folders, are never followed:
    a link can point anywhere on the PC, and its target would then be indexed, shown and packed into a build as if
    it were part of the module. links: a list; the relative path of each link skipped is appended to it. Read-only."""
    for root, dirs, files in os.walk(folder):
        keep = []
        for d in dirs:
            if d.startswith("."):
                continue
            if _is_link(os.path.join(root, d)):
                if links is not None:
                    links.append(os.path.relpath(os.path.join(root, d), folder))
                continue
            keep.append(d)
        dirs[:] = keep                                   # in-place: os.walk then enters only these
        for fn in files:
            full = os.path.join(root, fn)
            if _is_link(full):
                if links is not None:
                    links.append(os.path.relpath(full, folder))
                continue
            yield full, os.path.relpath(full, folder)


def read_file_inside(folder, relpath):
    """The bytes of folder/relpath. Raises ValueError (nothing read) when that path is a link or its real location
    is outside folder - the read-time half of walk_folder's rule, for a path listed earlier (an index row, a plan)
    that may have been swapped for a link since. Read-only."""
    p = os.path.join(folder, relpath)
    root = os.path.realpath(folder)
    if _is_link(p) or os.path.commonpath([os.path.realpath(p), root]) != root:
        raise ValueError(f"{relpath}: a symbolic link or a path outside {folder} - not read")
    with open(p, "rb") as fh:
        return fh.read()


def decode_text(b: bytes) -> str:
    """Lossless cp1252 decode: every byte sequence survives decode_text -> encode_text unchanged."""
    return b.decode(ENCODING, "nwn_lossless_dec")


def encode_text(s: str) -> bytes:
    """Text -> NWN bytes (cp1252). The reverse of decode_text; a character cp1252 can't hold is written as "?"."""
    return s.encode(ENCODING, "nwn_lossless_enc")


# an unsafe resource name contains a path separator (\ /), a character Windows forbids in file names (: * ? " < > |)
# or a control character, contains "..", starts with "." or white space, or ends with white space.
# e.g. "..\\evil", "a/b", ".hidden" and "name " are unsafe; "x_door01" is fine.
_BAD_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]|\.\.|^\.|^\s|\s$")
# names Windows treats as devices whatever the extension ("con.nss" opens the console, not a file)
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def safe_resref(name: str) -> bool:
    """True if a resource name is safe to use as a file name (no paths, no reserved device names).
    `name` is the resref without its extension; the check is a security boundary for names read from ERF files."""
    return bool(name) and not _BAD_NAME.search(name) and name.lower() not in WINDOWS_RESERVED


# --------------------------------------------------------------------------
# Resource precedence: which copy of a file the game uses
# --------------------------------------------------------------------------
# When several sources carry a file of the same name, NWN:EE loads the copy from the source with the highest resman
# priority. nwn.wiki "Content Load Order" quotes the engine's RESMAN_PRIORITY_* constants: USER_HAK 31 > MODULE 20 >
# USER_OVERRIDE 12 > KEYTABLE 1, and among haks the one listed FIRST in module.ifo wins. So: haks in module.ifo order,
# then the module, then the user's override folder, then the base game.
# The override folder is on one PC only (players don't have the builder's), so anything a build decides - what a lean
# hak drops, which include a recompile uses, what the audit re-checks - must not count an override copy at all.
RANK_MODULE = 10000
RANK_OVERRIDE = 20000
RANK_BASE = 30000


def source_rank(kind, position=0):
    """Precedence of one copy of a resource: lower wins (see the rule above). Every module that decides which copy
    the game uses calls this (or source_rank_sql), so they all agree.

    kind: "hak", "module", "override" or "base". position: for a hak its place in module.ifo's hak list (1 = listed
    first; the index stores it as sources.priority); for override folders their order. Returns an int; never raises."""
    if kind == "hak":
        return position or 0
    if kind == "module":
        return RANK_MODULE
    if kind == "override":
        return RANK_OVERRIDE + (position or 0)
    return RANK_BASE


def source_rank_sql(kind_col="s.kind", position_col="s.priority"):
    """source_rank as an SQL expression over the index's sources table, for ORDER BY (lower wins)."""
    return (f"CASE {kind_col} WHEN 'hak' THEN {position_col} WHEN 'module' THEN {RANK_MODULE} "
            f"WHEN 'override' THEN {RANK_OVERRIDE} + {position_col} ELSE {RANK_BASE} END")

# --------------------------------------------------------------------------
# Resource types (ERF/KEY numeric type -> file extension)
# --------------------------------------------------------------------------
# Archives store a number, not an extension. A number missing here is shown as "t<number>" by the readers.
RESTYPES = {
    0: "res", 1: "bmp", 2: "mve", 3: "tga", 4: "wav", 5: "wfx", 6: "plt", 7: "ini",
    8: "bmu", 9: "mpg", 10: "txt", 2000: "plh", 2001: "tex", 2002: "mdl", 2003: "thg",
    2005: "fnt", 2007: "lua", 2008: "slt", 2009: "nss", 2010: "ncs", 2011: "mod",
    2012: "are", 2013: "set", 2014: "ifo", 2015: "bic", 2016: "wok", 2017: "2da",
    2018: "tlk", 2022: "txi", 2023: "git", 2024: "bti", 2025: "uti", 2026: "btc",
    2027: "utc", 2029: "dlg", 2030: "itp", 2031: "btt", 2032: "utt", 2033: "dds",
    2034: "bts", 2035: "uts", 2036: "ltr", 2037: "gff", 2038: "fac", 2039: "bte",
    2040: "ute", 2041: "btd", 2042: "utd", 2043: "btp", 2044: "utp", 2045: "dft",
    2046: "gic", 2047: "gui", 2048: "css", 2049: "ccs", 2050: "btm", 2051: "utm",
    2052: "dwk", 2053: "pwk", 2054: "btg", 2055: "utg", 2056: "jrl", 2057: "sav",
    2058: "utw", 2059: "4pc", 2060: "ssf", 2061: "hak", 2062: "nwm", 2063: "bik",
    2064: "ndb", 2065: "ptm", 2066: "ptt", 2067: "bak", 2068: "dat", 2069: "shd",
    2070: "xbc", 2071: "wbm", 2072: "mtr", 2073: "ktx", 2074: "ttf", 2075: "sql",
    2076: "tml", 2077: "sq3", 2078: "lod", 2079: "gif", 2080: "png", 2081: "jpg",
    2082: "caf", 2083: "jui", 9996: "ids", 9997: "erf", 9998: "bif", 9999: "key",
}
EXT_TO_RESTYPE = {v: k for k, v in RESTYPES.items()}

# extensions whose files are GFF (read with read_gff)
GFF_EXTENSIONS = {
    "are", "git", "gic", "ifo", "uti", "utc", "utp", "utd", "ute", "utm", "uts",
    "utt", "utw", "utg", "dlg", "jrl", "fac", "itp", "bic", "gff", "ptm", "ptt",
    "bti", "btc", "btt", "bts", "bte", "btd", "btp", "btm", "btg",
}

# Human-friendly names for resource types
TYPE_NAMES = {
    "are": "area (static)", "git": "area (instances)", "gic": "area (comments)",
    "ifo": "module info", "uti": "item blueprint", "utc": "creature blueprint",
    "utp": "placeable blueprint", "utd": "door blueprint", "ute": "encounter blueprint",
    "utm": "store blueprint", "uts": "sound blueprint", "utt": "trigger blueprint",
    "utw": "waypoint blueprint", "dlg": "conversation", "jrl": "journal",
    "fac": "factions", "itp": "palette", "nss": "script source", "ncs": "compiled script",
    "2da": "2da table", "mdl": "model", "tga": "texture", "dds": "texture",
    "wav": "sound", "ssf": "soundset", "tlk": "talk table", "set": "tileset",
    "wok": "walkmesh", "pwk": "placeable walkmesh", "dwk": "door walkmesh",
    "txi": "texture info", "plt": "layered texture", "bic": "character",
}

# --------------------------------------------------------------------------
# GFF data model
# --------------------------------------------------------------------------
# A GFF file is a tree: a root struct holding labelled fields; a field is a number, a string, a struct, or a list of
# structs. Every .uti/.utc/.are/.dlg... is one of these. In Python: GffRoot / GffStruct with {label: GffField}.
# Field type ids (BioWare GFF spec)
BYTE, CHAR, WORD, SHORT, DWORD, INT, DWORD64, INT64, FLOAT, DOUBLE = range(10)
CEXOSTRING, RESREF, CEXOLOCSTRING, VOID, STRUCT, LIST = range(10, 16)
ORIENTATION, VECTOR = 16, 17  # NWN2-era, rarely seen; supported for safety

TYPE_LABELS = {
    BYTE: "byte", CHAR: "char", WORD: "word", SHORT: "short", DWORD: "dword",
    INT: "int", DWORD64: "dword64", INT64: "int64", FLOAT: "float", DOUBLE: "double",
    CEXOSTRING: "cexostring", RESREF: "resref", CEXOLOCSTRING: "cexolocstring",
    VOID: "void", STRUCT: "struct", LIST: "list", ORIENTATION: "orientation",
    VECTOR: "vector",
}
LABEL_TYPES = {v: k for k, v in TYPE_LABELS.items()}

# "simple" types fit in the field's own 4-byte data slot; the 8-byte "complex" numbers live in the field data block
# and the slot holds their offset there (BioWare GFF spec, Field section)
_SIMPLE_FMT = {BYTE: "<B", CHAR: "<b", WORD: "<H", SHORT: "<h", DWORD: "<I",
               INT: "<i", FLOAT: "<f"}
_COMPLEX_FMT = {DWORD64: "<Q", INT64: "<q", DOUBLE: "<d"}

BAD_STRREF = 0xFFFFFFFF          # "no talk-table entry"


@dataclass
class LocString:
    """A localised string: optional talk-table reference plus per-language text."""
    strref: int = BAD_STRREF
    entries: Dict[int, str] = field(default_factory=dict)  # key = language*2 + gender

    def text(self, tlk: Optional["Tlk"] = None) -> str:
        """The text to show: the entry for key 0 (language 0 = English, gender 0) if set, else any non-empty entry,
        else the talk-table text for strref (tlk: a Tlk or TlkSet), else ""."""
        if 0 in self.entries and self.entries[0]:
            return self.entries[0]
        for v in self.entries.values():
            if v:
                return v
        if tlk is not None and self.strref != BAD_STRREF:
            return tlk.get(self.strref)
        return ""


@dataclass
class GffField:
    """One field: its GFF type id (BYTE ... VECTOR above) and its Python value (int, float, str, LocString, bytes for
    VOID, GffStruct for STRUCT, list of GffStruct for LIST, list of floats for ORIENTATION / VECTOR)."""
    type: int
    value: object


class GffStruct:
    """Ordered collection of labelled fields. `sid` is the GFF struct id."""
    __slots__ = ("sid", "fields")

    def __init__(self, sid: int = 0):
        self.sid = sid
        self.fields: Dict[str, GffField] = {}

    def __contains__(self, label):
        return label in self.fields

    def get(self, label, default=None):
        """The value of field `label`, or `default` if the struct has no such field."""
        f = self.fields.get(label)
        return default if f is None else f.value

    def type_of(self, label):
        """The GFF type id of field `label`, or None if absent."""
        f = self.fields.get(label)
        return None if f is None else f.type

    def set(self, label, ftype, value):
        """Add or replace field `label` with type id `ftype` and `value` (in memory only)."""
        self.fields[label] = GffField(ftype, value)

    def __repr__(self):
        return f"<GffStruct sid={self.sid} fields={list(self.fields)}>"


class GffRoot(GffStruct):
    """The top struct of a GFF file. Adds the file's 4-character type ("UTI ", "ARE "...) and version ("V3.2").
    Its sid is -1 here; on disk the root's struct id is 0xFFFFFFFF."""
    __slots__ = ("file_type", "file_version")

    def __init__(self, file_type="GFF ", file_version="V3.2"):
        super().__init__(-1)
        self.file_type = file_type
        self.file_version = file_version


class GffError(Exception):
    """A GFF file is damaged, not a GFF, or holds something that can't be written back."""
    pass


# Reading, writing and converting a GFF recurse once per nested struct, so a file nested thousands deep would end in
# RecursionError instead of the GffError callers handle. Real NWN files nest well under 20 deep.
MAX_GFF_DEPTH = 200


def _too_deep(depth):
    if depth > MAX_GFF_DEPTH:
        raise GffError(f"damaged GFF: structs nested deeper than {MAX_GFF_DEPTH}")


# --------------------------------------------------------------------------
# GFF reader
# --------------------------------------------------------------------------
def read_gff(data: bytes) -> GffRoot:
    """Parse a whole GFF file held in memory and return its root struct (with every nested struct and list).

    Layout (BioWare GFF V3.2): a 56-byte header - file type (4 chars), version (4 chars), then six (offset, count)
    pairs for the struct table (12 bytes per struct), field table (12 bytes per field), label table (16 bytes per
    label), field data block, field indices block and list indices block (the last three counted in bytes).
    Raises GffError for the damage it checks. Reads only `data`."""
    if len(data) < 56:
        raise GffError("file too small to be GFF")
    ftype = data[0:4].decode("ascii", "replace")
    fver = data[4:8].decode("ascii", "replace")
    if not fver.startswith("V3"):
        raise GffError(f"unsupported GFF version {fver!r}")
    (s_off, s_cnt, f_off, f_cnt, l_off, l_cnt, fd_off, fd_len,
     fi_off, fi_len, li_off, li_len) = struct.unpack_from("<12I", data, 8)
    # every table must fit inside the file: a damaged or hostile header must not make us allocate gigabytes
    L = len(data)
    for off_, size_, what in ((s_off, s_cnt * 12, "struct"), (f_off, f_cnt * 12, "field"), (l_off, l_cnt * 16, "label"),
                              (fd_off, fd_len, "field data"), (fi_off, fi_len, "field index"), (li_off, li_len, "list index")):
        if off_ + size_ > L:
            raise GffError(f"damaged GFF: the {what} table does not fit in the file")
    if s_cnt == 0:
        raise GffError("damaged GFF: no structs")
    reads = [0]                                          # structs read so far (a list so the nested functions can add)

    # labels: 16 bytes each, NUL-padded
    labels = [data[l_off + i * 16: l_off + i * 16 + 16].split(b"\0", 1)[0]
              .decode("ascii", "replace") for i in range(l_cnt)]
    # struct: (struct id, data, field count); field: (type id, label index, data) - three uint32 each
    structs_raw = [struct.unpack_from("<3I", data, s_off + i * 12) for i in range(s_cnt)]
    fields_raw = [struct.unpack_from("<3I", data, f_off + i * 12) for i in range(f_cnt)]

    def fd(offset, n):
        """n bytes at `offset` in the field data block (bounds-checked)."""
        start = fd_off + offset
        if start + n > len(data):
            raise GffError("field data out of bounds")
        return data[start:start + n]

    def dec(b: bytes) -> str:
        return decode_text(b)

    visiting = set()                                     # structs on the current path: catches a struct inside itself

    def read_field(idx) -> Tuple[str, GffField]:
        """Field number idx as (label, GffField). Containers recurse into read_struct."""
        if idx >= f_cnt:                                 # the index came from the file, so it can point anywhere
            raise GffError("damaged GFF: field index out of range")
        ftype, lidx, dval = fields_raw[idx]
        label = labels[lidx] if lidx < len(labels) else f"__label{lidx}"
        raw4 = struct.pack("<I", dval)
        if ftype in _SIMPLE_FMT:
            # the value is in the 4-byte slot itself; a BYTE/WORD uses only its first 1/2 bytes
            val = struct.unpack(_SIMPLE_FMT[ftype], raw4[:struct.calcsize(_SIMPLE_FMT[ftype])])[0]
        elif ftype in _COMPLEX_FMT:
            val = struct.unpack(_COMPLEX_FMT[ftype], fd(dval, 8))[0]
        elif ftype == CEXOSTRING:                        # uint32 length, then the text
            (n,) = struct.unpack("<I", fd(dval, 4))
            val = dec(fd(dval + 4, n))
        elif ftype == RESREF:                            # one length byte (max 16), then the name
            n = fd(dval, 1)[0]
            val = dec(fd(dval + 1, n))  # case kept as stored; the index compares lower-case
        elif ftype == CEXOLOCSTRING:
            # uint32 total size (not counting itself), uint32 talk-table strref, uint32 count, then per language:
            # int32 id (language*2 + gender), uint32 length, text
            total, strref, count = struct.unpack("<3I", fd(dval, 12))
            pos = dval + 12
            ls = LocString(strref=strref)
            for _ in range(count):
                sid, n = struct.unpack("<iI", fd(pos, 8))
                ls.entries[sid] = dec(fd(pos + 8, n))
                pos += 8 + n
            val = ls
        elif ftype == VOID:                              # uint32 length, then raw bytes
            (n,) = struct.unpack("<I", fd(dval, 4))
            val = bytes(fd(dval + 4, n))
        elif ftype == STRUCT:                            # the slot holds a struct index
            val = read_struct(dval)
        elif ftype == LIST:
            # the slot holds a byte offset into the list indices block: uint32 count, then that many struct indices
            pos = li_off + dval
            if pos + 4 > L:
                raise GffError("damaged GFF: list out of bounds")
            (n,) = struct.unpack_from("<I", data, pos)
            if pos + 4 + 4 * n > L or n > s_cnt:
                raise GffError("damaged GFF: list longer than the file")
            idxs = struct.unpack_from(f"<{n}I", data, pos + 4)
            val = [read_struct(i) for i in idxs]
        elif ftype == ORIENTATION:
            val = list(struct.unpack("<4f", fd(dval, 16)))
        elif ftype == VECTOR:
            val = list(struct.unpack("<3f", fd(dval, 12)))
        else:
            raise GffError(f"unknown field type {ftype} for label {label!r}")
        return label, GffField(ftype, val)

    def read_struct(idx, root=False) -> GffStruct:
        """Struct number idx with all its fields. Refuses cycles, out-of-range indices and struct reuse."""
        if idx in visiting:
            raise GffError("cyclic struct reference")
        if idx >= s_cnt:
            raise GffError("damaged GFF: struct index out of range")
        _too_deep(len(visiting))                         # visiting = the structs this one is nested in
        reads[0] += 1
        if reads[0] > s_cnt:
            # in a real GFF every struct appears once; reusing them multiplies the tree (a 'GFF bomb')
            raise GffError("damaged GFF: structs are reused (the file would expand without limit)")
        visiting.add(idx)
        sid, dval, cnt = structs_raw[idx]
        s = GffRoot(ftype, fver) if root else GffStruct(sid)
        # one field: `dval` is that field's index; several: `dval` is a byte offset into the field indices block,
        # where `cnt` uint32 field indices follow
        if cnt == 1:
            fidx = [dval]
        elif cnt > 1:
            if fi_off + dval + 4 * cnt > L or cnt > f_cnt:
                raise GffError("damaged GFF: field list out of bounds")
            fidx = struct.unpack_from(f"<{cnt}I", data, fi_off + dval)
        else:
            fidx = []
        for i in fidx:
            label, f = read_field(i)
            s.fields[label] = f
        visiting.discard(idx)
        return s

    return read_struct(0, root=True)                     # struct 0 is always the root


def read_gff_file(path) -> GffRoot:
    """read_gff() of a file on disk (opened read-only). Raises GffError or OSError."""
    with open(path, "rb") as fh:
        return read_gff(fh.read())


# --------------------------------------------------------------------------
# GFF writer (used for the "clean module" stretch goal and round-trip tests)
# --------------------------------------------------------------------------
def write_gff(root: GffRoot) -> bytes:
    """Serialise a GFF tree to bytes (the same V3.2 layout read_gff reads). Returns the bytes; writes no file.

    The struct, field and label tables are built in one walk of the tree (root = struct 0, children after their
    parent); the six blocks are then laid out after the 56-byte header in BioWare's order. Labels are stored once
    each and cut to 16 bytes. Raises GffError for a resref over 16 bytes or an unknown field type."""
    structs: List[Tuple[int, int, int]] = []
    fields: List[Tuple[int, int, int]] = []
    labels: List[str] = []
    label_idx: Dict[str, int] = {}
    field_data = io.BytesIO()
    field_indices = io.BytesIO()
    list_indices = io.BytesIO()

    def enc(s: str) -> bytes:
        return encode_text(s)

    def get_label(l):
        if l not in label_idx:
            label_idx[l] = len(labels)
            labels.append(l)
        return label_idx[l]

    def write_struct(s: GffStruct, depth=0) -> int:
        """Append struct s (and, through write_field, everything inside it); returns its struct index."""
        _too_deep(depth)
        my_idx = len(structs)
        structs.append((0, 0, 0))  # placeholder
        fidxs = [write_field(label, f, depth) for label, f in s.fields.items()]
        sid = 0xFFFFFFFF if isinstance(s, GffRoot) else s.sid & 0xFFFFFFFF
        # same rule as the reader: one field -> its index directly; otherwise an offset into the field indices
        # (0xFFFFFFFF for a struct with no fields)
        if len(fidxs) == 1:
            structs[my_idx] = (sid, fidxs[0], 1)
        else:
            off = field_indices.tell()
            field_indices.write(struct.pack(f"<{len(fidxs)}I", *fidxs))
            structs[my_idx] = (sid, off if fidxs else 0xFFFFFFFF, len(fidxs))
        return my_idx

    def write_field(label, f: GffField, depth) -> int:
        """Append one field (its data to the right block); returns its field index. depth: its struct's nesting."""
        my_idx = len(fields)
        fields.append((0, 0, 0))
        t, v = f.type, f.value
        if t in _SIMPLE_FMT:
            # small values go in the 4-byte slot itself, zero-padded
            raw = struct.pack(_SIMPLE_FMT[t], v).ljust(4, b"\0")
            d = struct.unpack("<I", raw)[0]
        elif t in _COMPLEX_FMT:
            d = field_data.tell(); field_data.write(struct.pack(_COMPLEX_FMT[t], v))
        elif t == CEXOSTRING:
            b = enc(v); d = field_data.tell()
            field_data.write(struct.pack("<I", len(b)) + b)
        elif t == RESREF:
            b = enc(v)
            if len(b) > 16:
                raise GffError(f"resref value too long (max 16): {v!r}")
            d = field_data.tell()
            field_data.write(bytes([len(b)]) + b)
        elif t == CEXOLOCSTRING:
            body = io.BytesIO()
            for sid_, txt in v.entries.items():
                b = enc(txt)
                body.write(struct.pack("<iI", sid_, len(b)) + b)
            body = body.getvalue()
            d = field_data.tell()
            # total size = strref (4) + count (4) + the entries; it does not count the size field itself
            field_data.write(struct.pack("<3I", 8 + len(body), v.strref & 0xFFFFFFFF,
                                         len(v.entries)) + body)
        elif t == VOID:
            d = field_data.tell(); field_data.write(struct.pack("<I", len(v)) + v)
        elif t == STRUCT:
            d = write_struct(v, depth + 1)
        elif t == LIST:
            idxs = [write_struct(s, depth + 1) for s in v]
            d = list_indices.tell()
            list_indices.write(struct.pack(f"<I{len(idxs)}I", len(idxs), *idxs))
        elif t == ORIENTATION:
            d = field_data.tell(); field_data.write(struct.pack("<4f", *v))
        elif t == VECTOR:
            d = field_data.tell(); field_data.write(struct.pack("<3f", *v))
        else:
            raise GffError(f"cannot write field type {t}")
        fields[my_idx] = (t, get_label(label), d)
        return my_idx

    write_struct(root)

    s_blob = b"".join(struct.pack("<3I", *s) for s in structs)
    f_blob = b"".join(struct.pack("<3I", *f) for f in fields)
    l_blob = b"".join(enc(l)[:16].ljust(16, b"\0") for l in labels)
    fd_blob, fi_blob, li_blob = field_data.getvalue(), field_indices.getvalue(), list_indices.getvalue()

    off = 56                                             # header size; the blocks follow in the order of the header
    s_off = off; off += len(s_blob)
    f_off = off; off += len(f_blob)
    l_off = off; off += len(l_blob)
    fd_off = off; off += len(fd_blob)
    fi_off = off; off += len(fi_blob)
    li_off = off
    header = (root.file_type.ljust(4)[:4].encode("ascii") + root.file_version[:4].encode("ascii") +
              struct.pack("<12I", s_off, len(structs), f_off, len(fields), l_off, len(labels),
                          fd_off, len(fd_blob), fi_off, len(fi_blob), li_off, len(li_blob)))
    return header + s_blob + f_blob + l_blob + fd_blob + fi_blob + li_blob


# --------------------------------------------------------------------------
# GFF <-> JSON (neverwinter.nim nwn_gff compatible)
# --------------------------------------------------------------------------
def gff_to_json(s: GffStruct, _depth=0) -> dict:
    """A GFF tree as a JSON-ready dict in nwn_gff's layout: {"__data_type": "UTI ", "Label": {"type": "word",
    "value": 3}, ...}. A CExoLocString value is {"0": text, ..., "id": strref}; VOID data is base64 in "value64";
    structs carry "__struct_id". gff_from_json() is the reverse. Raises GffError for a tree nested over MAX_GFF_DEPTH."""
    _too_deep(_depth)
    out = {}
    if isinstance(s, GffRoot):
        out["__data_type"] = s.file_type
    elif s.sid != -1:
        out["__struct_id"] = s.sid
    for label, f in s.fields.items():
        t, v = f.type, f.value
        entry = {"type": TYPE_LABELS[t]}
        if t == CEXOLOCSTRING:
            val = {str(k): txt for k, txt in v.entries.items()}
            if v.strref != BAD_STRREF:
                val["id"] = v.strref
            entry["value"] = val
        elif t == VOID:
            entry["value64"] = base64.b64encode(v).decode("ascii")
        elif t == STRUCT:
            entry["value"] = gff_to_json(v, _depth + 1)
            entry["__struct_id"] = v.sid
        elif t == LIST:
            entry["value"] = [gff_to_json(x, _depth + 1) for x in v]
        else:
            entry["value"] = v
        out[label] = entry
    return out


def gff_from_json(j: dict, root: bool = True, _depth=0) -> GffStruct:
    """Rebuild a GFF tree from gff_to_json()'s layout (or nwn_gff's JSON). root=True returns a GffRoot.
    Keys starting with "__" are metadata, not fields. Raises KeyError for an unknown "type" label, GffError for a
    tree nested over MAX_GFF_DEPTH."""
    _too_deep(_depth)
    if root:
        s = GffRoot(j.get("__data_type", "GFF "))
    else:
        s = GffStruct(j.get("__struct_id", 0))
    for label, entry in j.items():
        if label.startswith("__"):
            continue
        t = LABEL_TYPES[entry["type"]]
        if t == CEXOLOCSTRING:
            val = dict(entry["value"])
            strref = val.pop("id", BAD_STRREF)
            v = LocString(strref, {int(k): txt for k, txt in val.items()})
        elif t == VOID:
            v = base64.b64decode(entry.get("value64", ""))
        elif t == STRUCT:
            v = gff_from_json(entry["value"], root=False, _depth=_depth + 1)
            if "__struct_id" in entry:
                v.sid = entry["__struct_id"]
        elif t == LIST:
            v = [gff_from_json(x, root=False, _depth=_depth + 1) for x in entry["value"]]
        elif t in (FLOAT, DOUBLE):
            v = float(entry["value"])                    # JSON may write 1.0 as 1; keep it a float, as read_gff gives
        else:
            v = entry["value"]
        s.fields[label] = GffField(t, v)
    return s


def iter_gff_leaves(s: GffStruct, path: str = "") -> Iterator[Tuple[str, str, int, object, GffStruct]]:
    """Yield (path, label, type, value, parent_struct) for every non-container field.
    Paths look like "ItemList[2]/Tag" (list items numbered from 0, nested labels joined with "/")."""
    for label, f in s.fields.items():
        p = f"{path}/{label}" if path else label
        if f.type == STRUCT:
            yield from iter_gff_leaves(f.value, p)
        elif f.type == LIST:
            for i, child in enumerate(f.value):
                yield from iter_gff_leaves(child, f"{p}[{i}]")
        else:
            yield p, label, f.type, f.value, s


def iter_gff_structs(s: GffStruct, path: str = "") -> Iterator[Tuple[str, GffStruct]]:
    """Yield (path, struct) for the root and every nested struct."""
    yield path, s
    for label, f in s.fields.items():
        p = f"{path}/{label}" if path else label
        if f.type == STRUCT:
            yield from iter_gff_structs(f.value, p)
        elif f.type == LIST:
            for i, child in enumerate(f.value):
                yield from iter_gff_structs(child, f"{p}[{i}]")


def leaf_to_text(ftype: int, value, tlk=None) -> str:
    """A field value as display/search text: a localised string's text (tlk resolves its strref), VOID data as hex,
    floats rounded to 6 decimals, anything else str()."""
    if ftype == CEXOLOCSTRING:
        return value.text(tlk)
    if ftype == VOID:
        return value.hex()
    if isinstance(value, float):
        return repr(round(value, 6))                     # hides float32 noise: 0.1 is stored as 0.10000000149...
    return str(value)


# --------------------------------------------------------------------------
# ERF (.mod / .hak / .erf / .nwm)
# --------------------------------------------------------------------------
# the most a compressed entry may claim to hold once decompressed. The size is read from the hak's own table, so
# without this cap a hostile hak could make the reader allocate up to 4 GB for one entry; no real NWN resource comes
# anywhere near 256 MB
MAX_ENTRY_SIZE = 256 << 20


@dataclass
class ErfEntry:
    """One file inside an ERF: name (lower-case, safe), extension, where its bytes start in the ERF, and how many bytes
    it takes there (for a compressed entry that is the compressed size)."""
    resref: str
    ext: str
    offset: int
    size: int
    compressed: bool = False
    usize: int = 0              # uncompressed size (E1.0 compressed entries)

    @property
    def filename(self):
        """The name the entry would have as a loose file: "resref.ext"."""
        return f"{self.resref}.{self.ext}"


class Erf:
    """Reads .mod/.hak/.erf. Only the tables are read up front; each entry is read from disk on demand
    (a 1 GB hak costs almost no RAM).

    ERF layout (BioWare ERF doc; E1.0 from neverwinter.nim erf.nim). Header, 160 bytes:
        0 file type ("MOD ", "HAK ", "ERF ", "NWM ")   4 version ("V1.0" or "E1.0")
        8 language count   12 localised-string size   16 entry count   20 offset to localised strings
        24 offset to key list   28 offset to resource list   32 build year (since 1900)   36 build day
        40 description strref   44 reserved (E1.0 keeps a 24-character id here; not used by this reader)
    Key list entry: resref (16 bytes, NUL-padded), resource id (uint32), type (uint16), unused (uint16)
        = 24 bytes; E1.0 adds a 20-byte SHA-1 of the entry (not checked here) = 44 bytes.
    Resource list entry: offset, size (uint32 each) = 8 bytes; E1.0 adds compression type and uncompressed size
        = 16 bytes.
    path: the .mod/.hak/.erf to read. The file is opened read-only and kept open until close().
    Attributes: entries (list of ErfEntry), bad_names (unsafe names found, see safe_resref), file_type, version.
    Raises ValueError for an empty, non-ERF or damaged file."""
    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        hdr = self._fh.read(160)
        self.file_type = hdr[0:4].decode("ascii", "replace")
        self.version = hdr[4:8].decode("ascii", "replace")
        if self.version not in ("V1.0", "E1.0"):
            self._fh.close()
            if not hdr:
                raise ValueError(f"{os.path.basename(path)} is empty (0 bytes) - not a module or hak")
            raise ValueError(f"{os.path.basename(path)} is not a module/hak/erf file (its header says "
                             f"{hdr[0:8].decode('ascii', 'replace')!r}) - damaged or a different kind of file")
        (lcount, lsize, ecount, loc_off, key_off, res_off,
         self.build_year, self.build_day, self.strref) = struct.unpack_from("<9I", hdr, 8)
        e1 = self.version == "E1.0"
        rsz = 16 if e1 else 8                            # resource list entry size
        ksz = 44 if e1 else 24                           # key list entry size
        fsize = self._fsize = os.fstat(self._fh.fileno()).st_size
        # both tables must fit in the file before anything is allocated: a hostile entry count can't cost memory
        if res_off + ecount * rsz > fsize or key_off + ecount * ksz > fsize:
            self._fh.close()
            raise ValueError(f"{os.path.basename(path)} is damaged: its table lists {ecount} entries, more than the file "
                             "can hold")
        self._fh.seek(res_off); rt = self._fh.read(ecount * rsz)
        self._fh.seek(key_off); kt = self._fh.read(ecount * ksz)
        res = []
        for i in range(ecount):
            if e1:
                off, disk, comp, _unc = struct.unpack_from("<4I", rt, i * rsz)
            else:
                off, disk = struct.unpack_from("<2I", rt, i * rsz); comp = 0
            res.append((off, disk, comp, _unc if e1 else disk))
        self.entries: List[ErfEntry] = []
        self.bad_names: List[str] = []
        for i in range(ecount):
            base = i * ksz
            # resource names are case-insensitive; the toolkit uses lower case everywhere
            resref = decode_text(kt[base:base + 16].split(b"\0", 1)[0]).lower()
            if not safe_resref(resref):
                # never let a name like "..\x" reach a file path; keep a record so the user can be told
                self.bad_names.append(resref)
                resref = f"invalid_name_{i}"
            (rtype,) = struct.unpack_from("<H", kt, base + 20)
            if rtype == 0xFFFF:                          # an invalid / empty slot (neverwinter.nim skips it too)
                continue
            ext = RESTYPES.get(rtype, f"t{rtype}")
            # key entry i and resource entry i describe the same file
            off, disk, comp, unc = res[i]
            self.entries.append(ErfEntry(resref, ext, off, disk, bool(comp), unc))

    def read(self, entry: ErfEntry) -> bytes:
        """The entry's bytes (decompressed if it is a compressed E1.0 entry). Reopens the file read-only if it was
        closed. Uses one shared file handle, so one Erf must not be read from several threads at once.

        Size guard: a compressed entry's own header may not declare more than the table's uncompressed size + 16
        bytes, the table's size may not exceed MAX_ENTRY_SIZE, and the decompressed output (zlib or zstd) is capped
        at the declared size (see nwn_zstd.decompress_buf). Raises ValueError if the entry can't be read, including
        when the file ends before the entry does (no short data is ever returned)."""
        if self._fh.closed:
            self._fh = open(self.path, "rb")
        if entry.compressed and entry.usize > MAX_ENTRY_SIZE:
            raise ValueError(f"{entry.filename}: claims {entry.usize} bytes uncompressed, more than the "
                             f"{MAX_ENTRY_SIZE >> 20} MB this reader allows for one entry")
        # checked before reading: read(n) allocates n bytes up front, so a table claiming 4 GB would cost 4 GB
        if entry.offset + entry.size > self._fsize:
            raise ValueError(f"{entry.filename}: the file ends before this entry does ({entry.size} bytes at offset "
                             f"{entry.offset}, file is {self._fsize}) - damaged or cut off")
        self._fh.seek(entry.offset)
        data = self._fh.read(entry.size)
        if len(data) != entry.size:
            raise ValueError(f"{entry.filename}: the file ends before this entry does ({len(data)} of {entry.size} "
                             "bytes) - damaged or cut off")
        if entry.compressed:
            # EE compressed entry (XRES buffer: none / zlib / zstd) - read natively; if that ever fails, fall back to
            # neverwinter.nim's nwn_erf from the toolkit's tools folder
            import nwn_zstd
            try:
                return nwn_zstd.decompress_buf(data, max(entry.usize, 1) + 16, (b"XRES",))
            except Exception as ex:  # noqa
                alt = erf_tool_extract(self.path, entry.filename)
                if alt is not None:
                    return alt
                why = []
                hint = "nwn_erf could not read it either" if find_tool("nwn_erf", why) else (
                    why[0] if why else "put nwn_erf (nwn_erf.exe on Windows) from neverwinter.nim in the toolkit's "
                    "tools folder to read it")
                raise ValueError(f"{entry.filename}: compressed entry could not be read ({ex}); {hint}") from None
        return data

    def close(self):
        """Close the file handle. Never raises; read() reopens it if needed."""
        try:
            self._fh.close()
        except Exception:  # noqa
            pass

    def __del__(self):
        self.close()

    def extract_all(self, out_dir):
        """Write every entry to out_dir as resref.ext (creating the folder; existing files of the same name are
        overwritten). Names were made safe in __init__, so nothing is written outside out_dir."""
        os.makedirs(out_dir, exist_ok=True)
        for e in self.entries:
            with open(os.path.join(out_dir, e.filename), "wb") as fh:
                fh.write(self.read(e))


# The toolkit's tools folder (third-party programs such as neverwinter.nim's), searched by find_tool before PATH.
TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")


def find_tool(name, why=None):
    """A neverwinter.nim program from the toolkit's tools folder (or PATH) that can be run, or None.

    name: the program name without ".exe" (added on Windows). why: optional list; when a file of that name exists but
    cannot be run (no execute permission, e.g. lost when a zip was unpacked), one plain sentence saying so is appended
    to it, so a caller can report that instead of "not found". Returns the full path; runs nothing."""
    import shutil
    c = os.path.join(TOOLS_DIR, name + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(c):
        if os.access(c, os.X_OK):
            return c
        if why is not None:
            why.append(f"{c} is there but cannot be run (no execute permission) - run: chmod +x \"{c}\" (on macOS "
                       f"also: xattr -d com.apple.quarantine \"{c}\")")
    # shutil.which only returns files that can be run
    w = shutil.which(name)
    return w if w and os.path.isfile(w) else None


def erf_tool_extract(erf_path, filename):
    """Extract one entry with nwn_erf (-x -f ERF <file>) into a temp folder; returns its bytes or None.

    The ERF is only read. nwn_erf runs with an argument list (no shell, so names can't inject commands), inside a new
    temporary folder (its working directory, where it writes the extracted file), with a 10-minute limit; the folder is
    deleted afterwards. Never raises: a missing tool, a failure or a timeout all return None."""
    import subprocess
    import tempfile
    exe = find_tool("nwn_erf")
    if not exe:
        return None
    d = tempfile.mkdtemp(prefix="nwn_erf_")
    try:
        subprocess.run([exe, "-x", "-f", os.path.abspath(erf_path), filename], cwd=d, capture_output=True, timeout=600)
        p = os.path.join(d, filename)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                return fh.read()
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        rmtree_force(d, ignore_errors=True)


def write_erf(path, files: List[Tuple[str, str, bytes]], file_type="MOD ",
              description: Optional[Dict[int, str]] = None):
    """Write an ERF V1.0 from bytes held in memory: files = [(resref, ext, data), ...]. A thin wrapper over
    write_erf_stream (same layout and checks); for large archives call that directly with read functions.

    path: the file to create (overwritten if it exists; the toolkit's builds pass a path in their own output folder).
    file_type: "MOD ", "HAK ", "ERF "... description: {language id: text} for the localised description.
    Raises ValueError for a name over 16 characters, an unsafe name or an unknown extension (nothing is written
    then)."""
    description = description or {}
    # localised strings: int32 language id, uint32 length, text
    loc = b"".join(struct.pack("<iI", k, len(b)) + b for k, b in ((k, encode_text(v)) for k, v in description.items()))
    write_erf_stream(path, [(r, e, len(b), (lambda b=b: b)) for r, e, b in files], file_type, len(description), loc)


def write_erf_stream(path, entries, file_type="HAK ", loc_count=0, loc_bytes=b"", progress=None, strref=0xFFFFFFFF):
    """
    Write an ERF V1.0 without holding the contents in memory (a 1 GB hak needs ~one file's worth of RAM).
    entries = [(resref, ext, size, read_fn)] where read_fn() returns that file's bytes.
    loc_count/loc_bytes: the description strings block copied raw from an existing ERF (kept as-is).
    progress: optional callback progress(done, total) after each entry. strref: the description strref to keep.
    path is created (overwritten if it exists); the hak editor writes to a temporary name and renames it on success.
    Returns [sha256 hex of each entry, in order]. Raises ValueError on a bad name or a size mismatch.
    """
    import hashlib
    nent = len(entries)
    loc_off = 160
    key_off = loc_off + len(loc_bytes)
    res_off = key_off + nent * 24
    data_off = res_off + nent * 8
    keys, reslist = io.BytesIO(), io.BytesIO()
    pos = data_off
    # first pass: the tables, from the sizes the caller promises (no data read yet), so they can be written first
    for i, (resref, ext, size, _fn) in enumerate(entries):
        if len(resref) > 16:
            raise ValueError(f"'{resref}.{ext}': resource names are limited to 16 characters")
        if not safe_resref(resref):
            raise ValueError(f"unsafe resource name: {resref!r}")
        if ext not in EXT_TO_RESTYPE:
            raise ValueError(f"'{resref}.{ext}': unknown file type .{ext}")
        keys.write(encode_text(resref).ljust(16, b"\0") + struct.pack("<IHH", i, EXT_TO_RESTYPE[ext], 0))
        reslist.write(struct.pack("<2I", pos, size))
        pos += size
    import datetime
    now = datetime.date.today()
    # header fields in the order listed in the Erf docstring; build date = today; 116 reserved zero bytes make it 160
    header = (file_type.ljust(4)[:4].encode("ascii") + b"V1.0" +
              struct.pack("<9I", loc_count, len(loc_bytes), nent, loc_off, key_off, res_off,
                          now.year - 1900, now.timetuple().tm_yday, strref) + b"\0" * 116)
    shas = []
    with open(path, "wb") as fh:
        fh.write(header + loc_bytes + keys.getvalue() + reslist.getvalue())
        # second pass: one entry in memory at a time; a size that differs from the promise would corrupt every
        # offset after it, so it stops the build
        for i, (resref, ext, size, fn) in enumerate(entries):
            data = fn()
            if len(data) != size:
                raise ValueError(f"'{resref}.{ext}' changed size while building ({size} -> {len(data)} bytes)")
            fh.write(data)
            shas.append(hashlib.sha256(data).hexdigest())
            if progress:
                progress(i + 1, nent)
    return shas


def erf_description_block(path):
    """(loc_count, raw description bytes) of an existing ERF, to carry over into a rebuilt one.
    Reads only the header and that block (read-only). (0, b"") when there is none or it claims more than 1 MB."""
    with open(path, "rb") as fh:
        hdr = fh.read(160)
        # header bytes 8-23: language count, localised-string size, entry count, offset to localised strings
        lcount, lsize, _e, loc_off = struct.unpack_from("<4I", hdr, 8)
        if not lcount or not lsize or lsize > 1_000_000:
            return 0, b""
        fh.seek(loc_off)
        return lcount, fh.read(lsize)


# --------------------------------------------------------------------------
# 2DA
# --------------------------------------------------------------------------
@dataclass
class TwoDA:
    """A parsed 2da table: column names, rows as (row label, values), and the optional DEFAULT: value."""
    columns: List[str]
    rows: List[Tuple[str, List[Optional[str]]]]  # (row label, values) ; None = ****
    default: Optional[str] = None


def _split_2da_line(line: str) -> List[str]:
    """Space-separated values; "double quotes" around values with spaces. Apostrophes and backslashes are plain
    characters in NWN 2das (shlex treated them as quotes/escapes and merged or mangled columns)."""
    return [t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t for t in re.findall(r'"[^"]*"|\S+', line)]


def read_2da(text: str) -> TwoDA:
    """Parse 2DA V2.0 text (already decoded). Missing values read as None, extra values are dropped, blank lines are
    skipped. Raises ValueError if the first line does not start with "2DA". (nwn_2da.parse is the editor's stricter
    reader that reports problems instead.)"""
    lines = text.replace("\r", "").split("\n")
    if not lines or not lines[0].strip().upper().startswith("2DA"):
        raise ValueError("not a 2DA V2.0 file")
    i = 1
    default = None
    # Optional DEFAULT line, then blank lines, then the column header
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().upper().startswith("DEFAULT:"):
        default = lines[i].split(":", 1)[1].strip()
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    if i >= len(lines):
        return TwoDA([], [], default)
    columns = _split_2da_line(lines[i])
    rows = []
    for line in lines[i + 1:]:
        if not line.strip():
            continue
        parts = _split_2da_line(line)
        label, vals = parts[0], parts[1:]
        vals = (vals + [None] * len(columns))[:len(columns)]
        rows.append((label, [None if v in (None, "****") else v for v in vals]))
    return TwoDA(columns, rows, default)


# --------------------------------------------------------------------------
# TLK (talk table: dialog.tlk and custom tlks)
# --------------------------------------------------------------------------
# NWN's rule: a talk-table number with this bit set (16777216 and up) refers to the module's custom .tlk, entry
# (number - 0x01000000); a smaller number refers to the game's dialog.tlk
CUSTOM_TLK_BIT = 0x01000000


class Tlk:
    """A talk table. `get(strref)` handles both base (dialog.tlk) and custom (bit 24 set) numbers.

    TLK V3.0 layout (BioWare TLK doc): 20-byte header - "TLK ", "V3.0", language id, string count, offset to the
    string data; then one 40-byte entry per string - flags (bit 0 = has text, bit 1 = has a sound), sound resref (16 bytes), volume
    variance, pitch variance, offset of the text (from the string data offset), text length, sound length (float).
    The whole file is read into memory (read-only); nothing is written."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        if self.data[0:4] != b"TLK ":
            raise ValueError(f"{path}: not a TLK file")
        if len(self.data) < 20:
            raise ValueError(f"{path}: cut off inside the TLK header")
        self.language, self.count, self.str_off = struct.unpack_from("<3I", self.data, 8)

    def has(self, index: int) -> bool:
        """True if the entry exists and has text. An entry the file is too short to hold (a cut-off tlk) counts as
        missing, so a damaged table never raises."""
        if index < 0 or index >= self.count or 20 + index * 40 + 40 > len(self.data):
            return False
        flags = struct.unpack_from("<I", self.data, 20 + index * 40)[0]
        return bool(flags & 1)

    def entry(self, index: int) -> str:
        """The text of entry `index` (a plain index, no custom bit), or "" if it has none."""
        if not self.has(index):
            return ""
        off, size = struct.unpack_from("<2I", self.data, 20 + index * 40 + 28)   # text offset and length
        return decode_text(self.data[self.str_off + off: self.str_off + off + size])

    def get(self, strref: int) -> str:
        """The text for a talk-table number; the custom bit, if set, is removed first."""
        return self.entry(strref - CUSTOM_TLK_BIT if strref >= CUSTOM_TLK_BIT else strref)

    def entries(self):
        """Yield (index, text) for every entry that has text."""
        for i in range(self.count):
            if self.has(i):
                yield i, self.entry(i)

    def sounds(self):
        """Yield (index, sound resref, lower case) for every entry whose flags say it has a sound (bit 1, 0x2): the
        game plays that .wav when the line is spoken, so the file is used although no GFF field names it."""
        for i in range(min(self.count, max(0, (len(self.data) - 20) // 40))):
            pos = 20 + i * 40
            if struct.unpack_from("<I", self.data, pos)[0] & 2:
                name = decode_text(self.data[pos + 4:pos + 20].split(b"\0", 1)[0]).strip().lower()
                if name:
                    yield i, name


class TlkSet:
    """Base dialog.tlk + optional custom tlk, resolved the way the game does."""

    def __init__(self, base=None, custom=None):
        self.base = base
        self.custom = custom

    def get(self, strref: int) -> str:
        """The text the game would show for this number ("" if none or the table isn't loaded)."""
        if strref == BAD_STRREF or strref < 0:
            return ""
        if strref >= CUSTOM_TLK_BIT:
            return self.custom.get(strref) if self.custom else ""
        return self.base.get(strref) if self.base else ""

    def status(self, strref: int) -> str:
        """'ok' | 'custom-missing' | 'no-custom-tlk' | 'base-missing' | 'no-base-tlk' | 'none'"""
        if strref == BAD_STRREF or strref < 0:
            return "none"
        if strref >= CUSTOM_TLK_BIT:
            if not self.custom:
                return "no-custom-tlk"
            return "ok" if self.custom.has(strref - CUSTOM_TLK_BIT) else "custom-missing"
        if not self.base:
            return "no-base-tlk"
        return "ok" if self.base.has(strref) else "base-missing"


def write_tlk(path, entries: dict, language: int = 0):
    """Write a TLK V3.0 from {index: text}. Gaps become empty entries.
    path is created (overwritten if it exists). Entries get no sound; language is the header's language id."""
    count = (max(entries) + 1) if entries else 0
    table, blob = io.BytesIO(), io.BytesIO()
    # each 40-byte entry as described in Tlk: flags, 16-byte sound resref (empty), volume, pitch, text offset,
    # text length, sound length
    for i in range(count):
        txt = entries.get(i)
        if txt is None:
            table.write(struct.pack("<I", 0) + b"\0" * 16 + struct.pack("<4If", 0, 0, 0, 0, 0.0))
            continue
        b = encode_text(txt)
        table.write(struct.pack("<I", 1) + b"\0" * 16 + struct.pack("<4If", 0, 0, blob.tell(), len(b), 0.0))
        blob.write(b)
    with open(path, "wb") as fh:
        fh.write(b"TLK V3.0" + struct.pack("<3I", language, count, 20 + count * 40) + table.getvalue() + blob.getvalue())


# --------------------------------------------------------------------------
# MDL light scan
# --------------------------------------------------------------------------
# ASCII model lines, any case, at the start of a line (after spaces):
#   "  bitmap c_rat" / "texture0 tx_wall01"  -> group 2 is the texture name
# "renderhint" (a keyword such as NormalAndSpecMapped) and "materialname" (a .mtr material, not a texture) are left
# out on purpose: they are not texture files, so listing them made real models look as if textures were missing
_MDL_TEX = re.compile(r"^\s*(bitmap|texture\d?)\s+(\S+)", re.I | re.M)
_MDL_SUPER = re.compile(r"^\s*setsupermodel\s+(\S+)\s+(\S+)", re.I | re.M)   # "setsupermodel c_rat c_dog" -> c_dog
_MDL_NEW = re.compile(r"^\s*newmodel\s+(\S+)", re.I | re.M)                  # "newmodel c_rat" -> c_rat


def scan_mdl(data: bytes) -> dict:
    """Binary models start with 4 zero bytes; only ASCII models are scanned.

    Returns {"format": "binary"} or {"format": "ascii", "model", "supermodel" (None for NULL), "textures"} with names
    lower-cased; "NULL" texture values are left out. Reads only `data`."""
    if data[:4] == b"\0\0\0\0":
        return {"format": "binary"}
    txt = data.decode(ENCODING, "replace")
    sup = _MDL_SUPER.search(txt)
    new = _MDL_NEW.search(txt)
    textures = sorted({m.group(2).lower() for m in _MDL_TEX.finditer(txt)
                       if m.group(2).lower() not in ("null", "")})
    return {"format": "ascii",
            "model": new.group(1).lower() if new else None,
            "supermodel": sup.group(2).lower() if sup and sup.group(2).upper() != "NULL" else None,
            "textures": textures}


# --------------------------------------------------------------------------
# Texture preview: TGA -> PNG (for the dashboard; no image libraries needed)
# --------------------------------------------------------------------------
def tga_to_png(data: bytes, max_size: int = 256) -> Optional[bytes]:
    """Decode uncompressed/RLE truecolor or greyscale TGA and return a PNG thumbnail.

    data: the .tga bytes. max_size: the thumbnail's longest side is about this many pixels (every n-th pixel is
    kept, no smoothing). Returns PNG bytes, or None for anything it can't show (colour-mapped or 16-bit images,
    images over 4096 x 4096, cut-off data). Never raises for bad input; writes nothing.

    TGA header (18 bytes): 0 id length, 1 colour-map type, 2 image type (2 truecolour, 3 greyscale, 10 / 11 the
    same RLE-compressed), 5 colour-map length (uint16), 7 colour-map entry bits, 12 width, 14 height (uint16 each),
    16 bits per pixel, 17 descriptor (bit 5 set = rows stored top to bottom). Pixels are stored as B, G, R(, A)."""
    import zlib
    if len(data) < 18:
        return None
    id_len, cmap_type, img_type = data[0], data[1], data[2]
    cmap_len = struct.unpack_from("<H", data, 5)[0]
    cmap_bits = data[7]
    w, h = struct.unpack_from("<HH", data, 12)
    bpp, desc = data[16], data[17]
    # the size cap keeps a hostile header from making us decode a huge image
    if img_type not in (2, 3, 10, 11) or w == 0 or h == 0 or w * h > 4096 * 4096:
        return None
    px = bpp // 8                                        # bytes per pixel: 1 grey, 3 BGR, 4 BGRA
    if px not in (1, 3, 4):
        return None
    # pixel data starts after the header, the image id and any colour map (skipped)
    pos = 18 + id_len + (cmap_len * ((cmap_bits + 7) // 8) if cmap_type else 0)
    need = w * h * px
    if img_type in (2, 3):
        raw = data[pos:pos + need]
    else:  # RLE
        # packets: a header byte, count = low 7 bits + 1; high bit set = one pixel repeated count times,
        # clear = count literal pixels. Stops once the image is full, so a bad packet can't grow it past `need`.
        out = bytearray()
        while len(out) < need and pos < len(data):
            hdr = data[pos]; pos += 1
            cnt = (hdr & 0x7F) + 1
            if hdr & 0x80:
                out += data[pos:pos + px] * cnt; pos += px
            else:
                out += data[pos:pos + cnt * px]; pos += cnt * px
        raw = bytes(out)
    if len(raw) < need:
        return None
    top_down = bool(desc & 0x20)                         # otherwise the first stored row is the bottom one
    step = max(1, max(w, h) // max_size)
    tw, th = (w + step - 1) // step, (h + step - 1) // step
    rows = []
    for ty in range(th):
        y = ty * step
        sy = y if top_down else (h - 1 - y)
        row = bytearray(b"\0")                           # PNG: each row starts with a filter byte (0 = none)
        base = sy * w * px
        for tx in range(tw):
            i = base + tx * step * px
            # to RGBA: grey copied to R, G and B; BGR(A) reversed; alpha 255 when the image has none
            if px == 1:
                v = raw[i]; row += bytes((v, v, v, 255))
            elif px == 3:
                row += bytes((raw[i + 2], raw[i + 1], raw[i], 255))
            else:
                row += bytes((raw[i + 2], raw[i + 1], raw[i], raw[i + 3]))
        rows.append(bytes(row))

    def chunk(t, d):
        """A PNG chunk: length, type, data, CRC-32 of type + data (all big-endian)."""
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    # PNG signature, IHDR (width, height, 8 bits per channel, colour type 6 = RGBA, no interlace), pixel data, end
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", tw, th, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# Base game resources: KEY/BIF reading (which names the game ships, its own 2das, whether a model exists)
# --------------------------------------------------------------------------
# The key files the game loads, in order; a later one wins for a name both list (neverwinter.nim game.nim
# DefaultKeyfiles and newDefaultResMan; its resman lets the last added container win). Each is taken from
# lang/<language>/data/ when the language folder has it, else from data/.
BASE_KEYS = ("nwn_base", "nwn_base_loc", "nwn_retail", "nwn_retail_loc")


def base_game_names(nwn_root) -> set:
    """All resource names the base game provides (BaseGame(nwn_root).names()). An empty set when the install is not
    set, not found, or has no readable key file: a few loose files are not "the base game", and treating them as
    such would make every base-game resource look missing. Read-only."""
    bg = BaseGame(nwn_root)
    return bg.names() if bg.keys_read else set()


class BaseGame:
    """Index of the installed game's resources, loaded the way the game does: the BASE_KEYS key files in order, then
    loose files in ovr/ and lang/<language>/data/ovr/ (each later source replaces an earlier copy of a name).

    nwn_root: the game install folder (None or a missing folder gives an empty index). language: the lang/ subfolder
    (the game's default is "en"). Only the key files are read up front; a BIF's table is read the first time
    something in it is asked for. Attribute keys_read: how many key files were read. Read-only: the game install is
    never written. Damaged key files, and key files of a version this reader doesn't know, are skipped."""

    def __init__(self, nwn_root, language="en"):
        self.root = nwn_root
        self.index = {}   # "resref.ext" -> (bif path or None, entry number, None) or ("file", path)
        self.keys_read = 0
        if not nwn_root or not os.path.isdir(nwn_root):
            return
        self.lang_root = os.path.join(nwn_root, "lang", language)
        keys = []
        for k in BASE_KEYS:
            for d in (os.path.join(self.lang_root, "data"), os.path.join(nwn_root, "data")):
                if os.path.isfile(os.path.join(d, k + ".key")):
                    keys.append(os.path.join(d, k + ".key"))
                    break
        if not keys:
            # an install without the EE key names (an older or hand-made layout): every data/*.key, by name
            data = os.path.join(nwn_root, "data")
            keys = [os.path.join(data, fn) for fn in sorted(os.listdir(data)) if fn.lower().endswith(".key")] \
                if os.path.isdir(data) else []
        for path in keys:
            try:
                self._read_key(path)
                self.keys_read += 1
            except (ValueError, struct.error, OSError):
                pass
        for d in (os.path.join(nwn_root, "ovr"), os.path.join(self.lang_root, "data", "ovr")):
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if "." in fn:
                        self.index[fn.lower()] = ("file", os.path.join(d, fn))

    def _read_key(self, path):
        """Add one KEY file's names to the index; a name already there from an earlier key is replaced.

        KEY layout (BioWare KEY/BIF doc): 64-byte header - "KEY ", version, BIF count, key count, offset to the BIF
        file table, offset to the key table, build year, build day, 32 reserved bytes. Key entry: resref (16 bytes),
        type (uint16), resource id (uint32) = 22 bytes. Version "V1.1" has 32-byte names. neverwinter.nim's "E1  "
        layout is different and would be misread, so it is refused (ValueError) and __init__ skips it."""
        with open(path, "rb") as fh:
            d = fh.read()
        if d[:4] != b"KEY " or d[4:8] not in (b"V1  ", b"V1.1"):
            raise ValueError(f"{path}: not a KEY V1 file")
        v11 = d[4:8] == b"V1.1"
        bif_count, key_count, file_off, key_off = struct.unpack_from("<4I", d, 8)
        bifs = []
        # BIF file table: size (uint32), name offset (uint32), name length (uint16), drives (uint16) = 12 bytes each;
        # names are relative to the install folder, written with Windows "\". A language's own copy of a BIF sits
        # in lang/<language>/data/ and is used instead (as neverwinter.nim does).
        for i in range(bif_count):
            size, name_off, name_len, drives = struct.unpack_from("<IIHH", d, file_off + i * 12)
            name = d[name_off:name_off + name_len].split(b"\0", 1)[0].decode("latin-1").replace("\\", "/")
            lang_copy = os.path.join(self.lang_root, "data", os.path.basename(name))
            bifs.append(lang_copy if os.path.isfile(lang_copy) else os.path.join(self.root, name))
        rsz = 32 if v11 else 16
        for i in range(key_count):
            base = key_off + i * (rsz + 6)
            rr = d[base:base + rsz].split(b"\0", 1)[0].decode("latin-1").lower()
            rt, rid = struct.unpack_from("<HI", d, base + rsz)
            # resource id: top 12 bits = which BIF in the file table, low 20 bits = which entry inside that BIF.
            # A name whose BIF is not in the table is still a name the game lists; get() returns None for it.
            bi, ri = rid >> 20, rid & 0xFFFFF
            self.index[f"{rr}.{RESTYPES.get(rt, f't{rt}')}"] = (bifs[bi] if bi < len(bifs) else None, ri, None)

    def names(self):
        """Every "resref.ext" the installed game provides (a new set)."""
        return set(self.index)

    def _bif_table(self, bif):
        """{entry number: (offset, size)} for one BIF file, read once and cached; {} if it can't be read.

        BIF V1 layout: 20-byte header - "BIFF", version, variable-resource count, fixed-resource count, offset to the
        variable-resource table; each table entry is id, offset, size, type (uint32 each, 16 bytes)."""
        if not hasattr(self, "_bt"):
            self._bt = {}
        if bif not in self._bt:
            try:
                with open(bif, "rb") as fh:
                    hdr = fh.read(20)
                    if hdr[:4] != b"BIFF":
                        self._bt[bif] = {}
                        return self._bt[bif]
                    var_count, fix_count, var_off = struct.unpack_from("<3I", hdr, 8)
                    fh.seek(var_off)
                    tbl = fh.read(var_count * 16)
                ent = {}
                for i in range(var_count):
                    rid, off, size, rt = struct.unpack_from("<4I", tbl, i * 16)
                    ent[rid & 0xFFFFF] = (off, size)     # matched with the key's low 20 bits
                self._bt[bif] = ent
            except OSError:
                self._bt[bif] = {}
        return self._bt[bif]

    def get(self, name) -> Optional[bytes]:
        """The bytes of a base-game resource ("resref.ext", any case), or None if the game doesn't have it.
        Reads from the override file or the BIF (read-only); BIF entries are returned as stored."""
        e = self.index.get(name.lower())
        if not e:
            return None
        if e[0] == "file":
            with open(e[1], "rb") as fh:
                return fh.read()
        bif, ri, _ = e
        if bif is None:
            return None
        tbl = self._bif_table(bif)
        if ri not in tbl:
            return None
        off, size = tbl[ri]
        with open(bif, "rb") as fh:
            fh.seek(off)
            return fh.read(size)


def write_key_bif(root, files):
    """Test/tool helper: write data/nwn_base.key + data/base.bif holding {(resref, ext): bytes}.
    Creates root/data if needed and overwrites those two files; used to build a fake game install for the tests.
    The layouts are the ones BaseGame reads (one BIF, so every resource id is just its index)."""
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    items = sorted(files.items())
    var = io.BytesIO(); blob = io.BytesIO()
    var_off = 20
    data_off = var_off + 16 * len(items)
    for i, ((rr, ext), data) in enumerate(items):
        var.write(struct.pack("<4I", i, data_off + blob.tell(), len(data), EXT_TO_RESTYPE[ext]))
        blob.write(data)
    with open(os.path.join(root, "data", "base.bif"), "wb") as fh:
        fh.write(b"BIFFV1  " + struct.pack("<3I", len(items), 0, var_off) + var.getvalue() + blob.getvalue())
    bifname = b"data/base.bif"
    file_off = 64
    name_off = file_off + 12
    key_off = name_off + len(bifname) + 1
    keys = b"".join(rr.encode().ljust(16, b"\0") + struct.pack("<HI", EXT_TO_RESTYPE[ext], i) for i, ((rr, ext), _) in enumerate(items))
    hdr = b"KEY V1  " + struct.pack("<4I", 1, len(items), file_off, key_off) + struct.pack("<2I", 126, 1) + b"\0" * 32
    with open(os.path.join(root, "data", "nwn_base.key"), "wb") as fh:
        fh.write(hdr + struct.pack("<IIHH", os.path.getsize(os.path.join(root, "data", "base.bif")), name_off, len(bifname), 1)
                 + bifname + b"\0" + keys)
