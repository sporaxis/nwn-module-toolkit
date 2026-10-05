"""
_harness.py - what every test suite shares: checks and their summary, temporary folders, an isolated workspace,
a stand-in script compiler, a fake game install and a few binary-format builders.

Why this exists: each suite used to carry its own copy of check() (three slightly different dialects), its own
temporary-folder handling (some leaked on failure) and its own way of keeping away from the real workspace. One copy
here keeps the rules the same everywhere:

  * Import this module FIRST in a suite, before any toolkit module. On import it
      - points NWN_WORKSPACE at a new, not yet created folder inside this run's temporary folder: nwn_dashboard and
        nwn_analyse fix their workspace when they are imported, so a suite can never read or write the real
        nwn_workspace (settings, analyses) whatever order its tests run in;
      - points NWN_SCRIPT_COMP at NO_COMPILER, a path that does not exist. The toolkit also looks in tools/ and on
        PATH, so a suite that needs "no compiler" passes compiler=NO_COMPILER (nwn_compile.find_compiler accepts only
        an explicitly given path, and None when it is missing) - a real compiler installed on the machine can then
        never change a result;
      - puts the toolkit folder, tests/ and tests/scenarios/ on sys.path.
  * check(name, cond, detail) prints PASS/FAIL; a failure prints its detail (evaluated only then if it is a
    function). run(fn, ...) runs one group of checks: an exception inside it is printed with its traceback, counted
    as a failed check, and the suite goes on with the next group.
  * skip(name, why) prints "SKIP <name> - <why>" and is counted: a check that cannot run on this system (no symbolic
    links, no execute bit on Windows, ...) never passes silently.
  * timing(name, value, limit) checks a time or memory limit only when NWN_TEST_TIMING=1 (such limits depend on how
    busy the machine is); otherwise it is a SKIP that still shows the value measured.
  * summary() prints "K skipped" and, as the last line, "N/M checks passed"; it returns the exit code (0 only if
    every check passed and at least one ran). tests/run_all.py reads those two lines.

Writes only inside this run's temporary folder (tempfile.gettempdir()/nwn_test_*), which is removed when the process
ends (atexit), also after a failure; tempdir() folders are removed as soon as their block ends.
Limits: a process killed from outside (kill -9) cannot clean up - tests/run_all.py reports such leftovers.
"""
import atexit
import contextlib
import gc
import os
import shutil
import struct
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (os.path.join(HERE, "scenarios"), HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ------------------------------------------------------------------------------------------------ isolation
SESSION = tempfile.mkdtemp(prefix="nwn_test_")
WORKSPACE = os.path.join(SESSION, "ws")                  # not created: some checks prove nothing was written there
NO_COMPILER = os.path.join(SESSION, "no_compiler", "nwn_script_comp")
os.environ["NWN_WORKSPACE"] = WORKSPACE
os.environ["NWN_SCRIPT_COMP"] = NO_COMPILER
TIMING = os.environ.get("NWN_TEST_TIMING") == "1"


def _rmtree(path):
    """Remove a folder tree, retrying a few times: on Windows a file closed a moment ago (a child process, an SQLite
    connection waiting for the garbage collector) can still be locked. Read-only files are made writable first."""
    def fix(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    for attempt in range(5):
        if not os.path.exists(path):
            return
        gc.collect()                                      # closes connections nothing refers to any more
        if sys.version_info >= (3, 12):                   # onerror is deprecated from 3.12; onexc replaces it
            shutil.rmtree(path, onexc=fix)
        else:
            shutil.rmtree(path, onerror=fix)
        if not os.path.exists(path):
            return
        time.sleep(0.2 * (attempt + 1))


atexit.register(_rmtree, SESSION)


@contextlib.contextmanager
def tempdir(prefix="nwn_t_"):
    """A new temporary folder (inside this run's folder) that is removed when the block ends, whatever happens in
    it. Yields its path."""
    d = tempfile.mkdtemp(prefix=prefix, dir=SESSION)
    try:
        yield d
    finally:
        _rmtree(d)


# ------------------------------------------------------------------------------------------------ checks
PASSED, FAILED, SKIPPED = [], [], []
MAX_DETAIL = 2000


def _show(detail):
    """Text of a failure's detail: a function is called now (only failures pay for it), long text is cut."""
    try:
        if callable(detail):
            detail = detail()
        text = detail if isinstance(detail, str) else repr(detail)
    except Exception:  # noqa - the detail itself failed; say so instead of hiding the failure
        text = "(the detail could not be worked out)\n" + traceback.format_exc()
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL] + f" ... ({len(text) - MAX_DETAIL} more characters)"
    return text


def check(name, cond, detail=""):
    """Record and print one check. cond: any value (true = pass), or a function that returns one - an exception
    raised by that function is a failure that prints its traceback. detail: shown only on failure (a value or a
    function returning one). Returns True when the check passed."""
    try:
        ok = bool(cond() if callable(cond) else cond)
    except Exception:  # noqa
        ok, detail = False, "the check raised:\n" + traceback.format_exc()
    (PASSED if ok else FAILED).append(name)
    if ok:
        print("PASS " + name)
    else:
        shown = _show(detail) if detail != "" else ""
        print("FAIL " + name + (f"  -> {shown}" if shown else ""))
    sys.stdout.flush()
    return ok


def skip(name, why):
    """A check that cannot run here: printed as SKIP with the reason and counted in the summary (never a PASS)."""
    SKIPPED.append(name)
    print(f"SKIP {name} - {why}")
    sys.stdout.flush()


def timing(name, value, limit, unit="s"):
    """A measured limit (time taken, memory used): checked (value < limit) only with NWN_TEST_TIMING=1, because it
    depends on how busy the machine is; otherwise a SKIP that still shows the value. unit: "s" or "MB"."""
    shown = f"{value:.2f} {unit} (limit {limit} {unit})"
    if TIMING:
        return check(name, value < limit, shown)
    skip(name, f"{shown}; measured limits are checked only with NWN_TEST_TIMING=1")
    return True


def run(fn, *args, **kwargs):
    """Run one group of checks. Returns fn's result, or None when it raised: the exception is printed with its
    traceback and counted as the failed check '<fn>: ran to the end', and the suite continues."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa - one crashed group must not hide the results of the others
        check(f"{fn.__name__}: ran to the end", False, traceback.format_exc())
        return None


def summary():
    """Print the summary (failed names, 'K skipped', then 'N/M checks passed' as the last line). Returns the exit
    code: 0 when every check passed and at least one ran, else 1."""
    total = len(PASSED) + len(FAILED)
    print()
    if FAILED:
        print(f"{len(FAILED)} failed:")
        for nm in FAILED:
            print(f"  {nm}")
    if SKIPPED:
        print(f"{len(SKIPPED)} skipped")
    print(f"{len(PASSED)}/{total} checks passed")
    sys.stdout.flush()
    return 0 if total and not FAILED else 1


# ------------------------------------------------------------------------------------------------ narrow patches
class Proxy:
    """Stands in for a module (or any object) inside ONE other module: the attributes given here replace the real
    ones, everything else is read from the real object. Used with unittest.mock.patch.object(mod, "os",
    Proxy(os, replace=boom)) so that only `mod` sees the change - other modules and other threads keep the real
    os, time, subprocess ..."""

    def __init__(self, real, **attrs):
        self.__dict__["_real"] = real
        self.__dict__.update(attrs)

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)


def raiser(exc):
    """A function that raises `exc` whatever it is called with (a simulated failure)."""
    def fn(*_a, **_k):
        raise exc
    return fn


# ------------------------------------------------------------------------------------------------ toolkit helpers
def analyse(mod, out, haks=(), nwn_root=None, **index_kw):
    """Index and analyse a module (folder or .mod) into `out` without printing; returns the report dict.
    index_kw goes to nwn_index.run_index (tlk=, nwn_user=, write_json= ...)."""
    import io
    import nwn_analysis
    import nwn_index
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, haks=list(haks), out=out, verbose=False, nwn_root=nwn_root, **index_kw)
        return nwn_analysis.run_analysis(out, verbose=False)


def pack_hak(folder, path, extra=(), description=None):
    """Pack the files of `folder` (sorted by name; only types the game knows) plus `extra` [(resref, ext, bytes)]
    into a .hak at `path`. description: {language: text} for the hak's description block, or None."""
    import nwnlib as n
    files = []
    for fn in sorted(os.listdir(folder)):
        b, _, e = fn.rpartition(".")
        if e in n.EXT_TO_RESTYPE:
            with open(os.path.join(folder, fn), "rb") as fh:
                files.append((b, e, fh.read()))
    files += list(extra)
    n.write_erf(path, files, file_type="HAK ", description=description)


# Base-game resource names the fixture modules refer to: a fake install that lists them (fake_nwn_root) makes them
# "base game" instead of "missing".
BASE = ["nw_c2_default1.ncs", "x2_mod_def_act.ncs", "nw_waypoint001.utw", "door_iron.utd", "store_gen.utm",
        "po_hero_h.tga", "nw_crewpsp.uti"]


def fake_nwn_root(folder, names):
    """A minimal 'game install' whose data/nwn_base.key lists the given base-game resource names (no data)."""
    import nwnlib as n
    os.makedirs(os.path.join(folder, "data"), exist_ok=True)
    keys = b""
    for i, nm in enumerate(names):
        rr, ext = nm.rsplit(".", 1)
        keys += rr.encode().ljust(16, b"\0") + struct.pack("<HI", n.EXT_TO_RESTYPE[ext], i)
    # KEY V1 header: no bif files, len(names) keys starting at offset 64 (layout as in nwnlib.BaseGame._read_key)
    hdr = b"KEY V1  " + struct.pack("<6I", 0, len(names), 64, 64, 126, 1) + b"\0" * 32
    with open(os.path.join(folder, "data", "nwn_base.key"), "wb") as fh:
        fh.write(hdr + keys)
    return folder


# A stand-in for nwn_script_comp: the same command line as the real one (-c -y [-s] [-d OUT] --dirs a,b FILES or a
# folder). A script compiles unless its text or an include it reaches contains BROKEN, or an include is missing - then
# it prints an error line in the real compiler's format. With -d each compiled script gets OUT/<name>.ncs whose bytes
# depend on the script AND its includes (so a changed include changes the .ncs); -s (simulate) writes nothing.
FAKE_COMPILER = r'''
import hashlib, os, re, sys
a = sys.argv[1:]
out = a[a.index("-d") + 1] if "-d" in a else None
dirs = a[a.index("--dirs") + 1].split(",") if "--dirs" in a else []
files = [x for x in a if x.lower().endswith(".nss")]
if not files and a and os.path.isdir(a[-1]):        # compile_module passes the folder of scripts
    files = [os.path.join(a[-1], f) for f in sorted(os.listdir(a[-1])) if f.lower().endswith(".nss")]
def load(nm, seen):
    for d in dirs:
        p = os.path.join(d, nm + ".nss")
        if os.path.isfile(p):
            src = open(p, encoding="latin-1").read()
            return src + "".join(load(i.lower(), seen | {nm}) for i in re.findall(r'#include\s+"(\w+)"', src)
                                 if i.lower() not in seen)
    return "<missing " + nm + ">"
for f in files:
    nm = os.path.basename(f)[:-4].lower()
    if os.path.dirname(f) not in dirs:
        dirs.insert(0, os.path.dirname(f))
    full = load(nm, set())
    if "BROKEN" in full or "<missing" in full:
        print(f"E [00:00:00] {f}: ERROR: could not compile")
        continue
    if out:
        open(os.path.join(out, nm + ".ncs"), "wb").write(b"NCS V1.0" + hashlib.sha256(full.encode()).digest())
'''


def fake_compiler(folder, body=None, name="nwn_script_comp"):
    """Write a stand-in compiler program into `folder` and return the path to pass as compiler=.

    body: the Python code it runs (default FAKE_COMPILER above); sys.argv holds the compiler's arguments. The code
    goes into <name>_impl.py; the program itself is a launcher that runs it with this Python: <name>.cmd on Windows
    (a .cmd file can be started directly, like an .exe), elsewhere a /bin/sh script with the execute bit set
    (nwn_compile.find_compiler accepts only an executable file). The launcher uses exec, so a signal that ends the
    Python process is what the toolkit sees."""
    os.makedirs(folder, exist_ok=True)
    impl = os.path.join(folder, name + "_impl.py")
    with open(impl, "w", encoding="utf-8") as fh:
        fh.write(body if body is not None else FAKE_COMPILER)
    if os.name == "nt":
        prog = os.path.join(folder, name + ".cmd")
        with open(prog, "w") as fh:
            fh.write(f'@"{sys.executable}" "%~dp0{name}_impl.py" %*\r\n')
    else:
        prog = os.path.join(folder, name)
        with open(prog, "w") as fh:
            fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n')
        os.chmod(prog, 0o755)
    return prog


def can_symlink(folder):
    """True when this system lets the test make a symbolic link (Windows needs a privilege or developer mode)."""
    target, link = os.path.join(folder, ".link_target"), os.path.join(folder, ".link_probe")
    try:
        with open(target, "w") as fh:
            fh.write("x")
        os.symlink(target, link)
        os.remove(link)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        if os.path.exists(target):
            os.remove(target)


# ------------------------------------------------------------------------------------------------ binary builders
def raw_zstd(data):
    """A zstd frame holding one raw (stored) block - valid zstd without needing a compressor. Frame header
    descriptor 0x20 = single segment with a 1-byte content size, so len(data) must be under 256 (RFC 8878 3.1.1)."""
    fhd = 0x20
    return struct.pack("<I", 0xFD2FB528) + bytes([fhd, len(data)]) + \
        (1 | (0 << 1) | (len(data) << 3)).to_bytes(3, "little") + data      # last block, type 0 (raw), its size


def e1_erf(path, files):
    """Write a minimal EE "E1.0" ERF (a hak whose entries may be compressed).
    files = [(resref, ext, payload, compressed_flag, uncompressed_size)]. Layout as read by nwnlib.Erf: 160-byte
    header, 44-byte key entries, 16-byte resource entries (offset, size, compression flag, uncompressed size)."""
    import nwnlib as n
    hdr_len, n_ = 160, len(files)
    key_off = hdr_len; res_off = key_off + 44 * n_; data_off = res_off + 16 * n_
    keys, res, blob = b"", b"", b""
    for i, (rr, ext, data, comp, usize) in enumerate(files):
        keys += rr.encode().ljust(16, b"\0") + struct.pack("<IHH", i, n.EXT_TO_RESTYPE[ext], 0) + b"\0" * 20
        res += struct.pack("<4I", data_off + len(blob), len(data), 1 if comp else 0, usize)
        blob += data
    head = b"HAK E1.0" + struct.pack("<9I", 0, 0, n_, hdr_len, key_off, res_off, 126, 1, 0xFFFFFFFF)
    with open(path, "wb") as fh:
        fh.write(head.ljust(hdr_len, b"\0") + keys + res + blob)


def open_handles(path):
    """How many file descriptors of this process point at `path` (Linux /proc only; None elsewhere)."""
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        return None
    target = os.path.realpath(path)
    cnt = 0
    for fd in os.listdir(fd_dir):
        try:
            if os.path.realpath(os.readlink(os.path.join(fd_dir, fd))) == target:
                cnt += 1
        except OSError:
            pass
    return cnt
