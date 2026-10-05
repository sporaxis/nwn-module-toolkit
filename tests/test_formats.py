"""
File formats: GFF, ERF (incl. EE compressed entries), KEY, TLK, MDL text scan, the zstd / zlib decoders, and finding
the helper programs (nwn_asm, nwn_erf) in tools/.
    python tests/test_formats.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in a temporary folder that is removed at the end.
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import shutil
import struct
import sys
import time
import zlib
from unittest import mock

import nwnlib as n  # noqa: E402
import nwn_zstd  # noqa: E402
from _harness import check, skip, raw_zstd, e1_erf  # noqa: E402


def raises(fn, exc):
    """True if fn() raises exactly an `exc` (the caller's expected error type), False if it returns or raises
    something else; the detail says which."""
    try:
        fn()
        return False, "no error"
    except exc:
        return True, ""
    except Exception as ex:  # noqa
        return False, f"{type(ex).__name__}: {ex}"


# ---------------------------------------------------------------------------------------------------- fixtures
def literal_block(data, last):
    """A *compressed* block (type 2) that holds only raw literals and no sequences: Size_Format 1 (12-bit size),
    then a zero byte for Number_of_Sequences. The pure-Python decoder treats it like any compressed block."""
    size = len(data)
    lit = bytes([0 | (1 << 2) | ((size & 0xF) << 4), size >> 4]) + data + b"\0"
    return ((1 if last else 0) | (2 << 1) | (len(lit) << 3)).to_bytes(3, "little") + lit


def many_block_frame(nblocks, bsize):
    """One frame of nblocks compressed blocks. Frame header: descriptor 0 (not single-segment, no content size), then
    one window-descriptor byte."""
    body = b"".join(literal_block(bytes([i & 0xFF]) * bsize, i == nblocks - 1) for i in range(nblocks))
    return struct.pack("<I", 0xFD2FB528) + bytes([0x00, 0x10]) + body


def xres(algo, size, body):
    return b"XRES" + struct.pack("<3I", 3, algo, size) + body


# ---------------------------------------------------------------------------------------------------- checks
def test_output_caps(tmp):
    # zlib: 20 MB of zeros compress to ~20 KB; the buffer claims 1000 bytes, so the stream must be refused, not cut
    bomb = zlib.compress(b"\0" * (20 << 20))
    ok, why = raises(lambda: nwn_zstd.decompress_buf(xres(1, 1000, struct.pack("<I", 1) + bomb)), nwn_zstd.ZstdError)
    check("zlib: output over the declared size is refused (not truncated)", ok, why)
    good = zlib.compress(b"abc" * 100)
    check("zlib: a stream of exactly the declared size still decodes",
          nwn_zstd.decompress_buf(xres(1, 300, struct.pack("<I", 1) + good)) == b"abc" * 100)
    # an ERF entry may not declare more than the per-entry cap, whatever its own table says
    p = os.path.join(tmp, "big.hak")
    huge = n.MAX_ENTRY_SIZE + 1
    e1_erf(p, [("bomb", "nss", xres(1, huge, struct.pack("<I", 1) + bomb), True, huge)])
    erf = n.Erf(p)
    ok, why = raises(lambda: erf.read(erf.entries[0]), ValueError)
    erf.close()
    check("Erf.read: a compressed entry declaring more than the per-entry cap raises ValueError", ok, why)
    # the pure-Python zstd cap still applies through decompress() (the libraries are not installed here)
    ok, why = raises(lambda: nwn_zstd.decompress(raw_zstd(b"x" * 100), 50), nwn_zstd.ZstdError)
    check("zstd: decompress() refuses output over max_size", ok, why)


def test_zstd_errors():
    # cut-off inputs of every kind must give ZstdError, never IndexError / struct.error
    frame = raw_zstd(b"hello world")
    rle = struct.pack("<I", 0xFD2FB528) + bytes([0x20, 50]) + (1 | (1 << 1) | (50 << 3)).to_bytes(3, "little") + b"z"
    skip = struct.pack("<I", 0x184D2A50)         # a skippable frame cut before its size field
    cases = {"frame header": frame[:5], "block header": frame[:7], "RLE byte": rle[:-1], "skippable frame": skip + b"\0\0",
             "compressed block": many_block_frame(1, 10)[:-5]}
    bad = {}
    for name, data in cases.items():
        ok, why = raises(lambda d=data: nwn_zstd._py_decompress(d, 1000), nwn_zstd.ZstdError)
        if not ok:
            bad[name] = why
    check("zstd: truncated inputs raise ZstdError, not IndexError/struct.error", not bad, bad)
    # decompress_buf on a cut-off buffer
    ok, why = raises(lambda: nwn_zstd.decompress_buf(xres(2, 11, struct.pack("<II", 1, 0) + frame[:6])), nwn_zstd.ZstdError)
    check("zstd: a cut-off compressed buffer raises ZstdError", ok, why)
    # decompress() picks a library when one is installed: it must report a cut-off frame and a frame followed by junk
    ok, why = raises(lambda: nwn_zstd.decompress(frame[:-3], 1000), nwn_zstd.ZstdError)
    ok2, why2 = raises(lambda: nwn_zstd.decompress(frame + b"junk", 1000), nwn_zstd.ZstdError)
    check("zstd: decompress() (library or pure Python) raises ZstdError on a cut-off frame or trailing junk",
          ok and ok2, (why, why2))
    check("zstd: decompress() joins two frames and skips a skippable frame",
          nwn_zstd.decompress(frame + struct.pack("<II", 0x184D2A50, 2) + b"xy" + frame, 1000) == b"hello world" * 2)


def test_zstd_linear():
    # a frame of many compressed blocks: the old decoder copied the frame's output once per block (quadratic)
    nb, bs = 8000, 1000                           # the old decoder took about 4 s on this; the fixed one 0.02 s
    data = many_block_frame(nb, bs)
    want = b"".join(bytes([i & 0xFF]) * bs for i in range(nb))
    t0 = time.time()
    got = nwn_zstd._py_decompress(data, 1 << 24)
    dt = time.time() - t0
    check("zstd: a many-block frame decodes correctly", got == want, (len(got), len(want)))
    h.timing("zstd: a many-block frame decodes in linear time (8000 blocks under 1.5 s)", dt, 1.5)


def test_gff_field_index(tmp):
    r = n.GffRoot("UTI ")
    r.set("Tag", n.CEXOSTRING, "x")
    r.set("Cost", n.DWORD, 5)
    data = bytearray(n.write_gff(r))
    # struct 0 has 2 fields, so its data slot is an offset into the field indices block; point the first index
    # past the field table
    fi_off = struct.unpack_from("<I", data, 8 + 8 * 4)[0]
    struct.pack_into("<I", data, fi_off, 999)
    ok, why = raises(lambda: n.read_gff(bytes(data)), n.GffError)
    check("GFF: a field index past the field table raises GffError, not IndexError", ok, why)


def test_mdl_textures():
    mdl = (b"newmodel c_rat\nsetsupermodel c_rat NULL\nnode trimesh body\n  bitmap c_rat_skin\n"
           b"  renderhint NormalAndSpecMapped\n  materialname c_rat_mat\n  texture1 c_rat_n\nendnode\n")
    info = n.scan_mdl(mdl)
    check("MDL: renderhint / materialname values are not reported as textures",
          info["textures"] == ["c_rat_n", "c_rat_skin"], info)


def test_key_version(tmp):
    root = os.path.join(tmp, "game")
    n.write_key_bif(root, {("a", "nss"): b"void main() {}"})
    p = os.path.join(root, "data", "nwn_base.key")
    d = bytearray(open(p, "rb").read())
    d[4:8] = b"E1  "                              # neverwinter.nim's own layout: not readable by this toolkit
    open(p, "wb").write(bytes(d))
    bg = n.BaseGame(root)
    check("KEY: BaseGame skips a key file of an unknown version instead of misreading it", bg.names() == set(), bg.names())
    ok, why = raises(lambda: bg._read_key(p), ValueError)
    check("KEY: BaseGame._read_key refuses an unknown version with ValueError", ok, why)


def test_write_erf_unknown_ext(tmp):
    ok1, why1 = raises(lambda: n.write_erf(os.path.join(tmp, "x.hak"), [("a", "zzz", b"1")], "HAK "), ValueError)
    ok2, why2 = raises(lambda: n.write_erf_stream(os.path.join(tmp, "y.hak"), [("a", "zzz", 1, lambda: b"1")]), ValueError)
    check("write_erf / write_erf_stream: an unknown extension raises ValueError in both", ok1 and ok2, (why1, why2))
    check("write_erf: nothing is written when the name is refused", not os.path.exists(os.path.join(tmp, "x.hak")))


def test_erf_short_entry(tmp):
    p = os.path.join(tmp, "cut.hak")
    n.write_erf(p, [("a", "nss", b"void main() {}\n" * 4)], "HAK ")
    data = open(p, "rb").read()
    open(p, "wb").write(data[:-20])               # the last entry now runs past the end of the file
    erf = n.Erf(p)
    ok, why = raises(lambda: erf.read(erf.entries[0]), ValueError)
    erf.close()
    check("Erf.read: an entry that runs past the end of the file raises ValueError (no short data)", ok, why)


def test_tlk_truncated(tmp):
    p = os.path.join(tmp, "t.tlk")
    n.write_tlk(p, {0: "zero", 1: "one", 2: "two"})
    data = open(p, "rb").read()
    open(p, "wb").write(data[:20 + 40 + 10])      # header + entry 0 + part of entry 1; the text block is gone
    t = n.Tlk(p)
    try:
        got = (t.has(0), t.has(1), t.has(2), t.entry(1), t.get(2))
        quiet = got == (True, False, False, "", "")
    except Exception as ex:  # noqa
        got, quiet = f"{type(ex).__name__}: {ex}", False
    check("TLK: has()/entry() on a truncated table answer False/'' instead of raising", quiet, got)
    open(p, "wb").write(data[:12])
    ok, why = raises(lambda: n.Tlk(p), ValueError)
    check("TLK: a file cut inside the header raises ValueError", ok, why)


def test_zstd_backend_and_hint():
    """The decompression library is chosen once at import; the pure-Python path says once when it gets slow."""
    import builtins
    import contextlib
    import io
    imports, real_import = [], builtins.__import__

    def spy(name, *a, **k):
        imports.append(name)
        return real_import(name, *a, **k)
    # this suite runs no other thread, so replacing __import__ for the length of one call affects nothing else
    with mock.patch.object(builtins, "__import__", spy):
        nwn_zstd.decompress(raw_zstd(b"abc"), 1000)
    check("zstd: decompress() runs no import per call (the backend is picked once, at import)",
          not imports and nwn_zstd.BACKEND in ("compression.zstd", "zstandard", "python"), (imports, nwn_zstd.BACKEND))
    big = many_block_frame(11000, 1000)             # 11 MB of output, more than the 10 MB the hint waits for
    err = io.StringIO()
    with mock.patch.object(nwn_zstd, "BACKEND", "python"), \
            mock.patch.dict(nwn_zstd._SLOW, {"bytes": 0, "told": False}), contextlib.redirect_stderr(err):
        small = nwn_zstd.decompress(raw_zstd(b"x" * 100), 1000)
        first = err.getvalue()
        got = nwn_zstd.decompress(big, 1 << 24)
        second = err.getvalue()
        nwn_zstd.decompress(big, 1 << 24)
        third = err.getvalue()
    check("zstd: no hint while the pure-Python path has handled little", small == b"x" * 100 and first == "", first)
    check("zstd: past ~10 MB the pure-Python path prints one line suggesting the zstandard package",
          len(got) == 11_000_000 and second.count("\n") == 1 and "pip install zstandard" in second, second)
    check("zstd: the hint is printed only once per run", third == second, third)


def test_find_tool(tmp):
    """find_tool only returns a program that can run; callers can say why one was not usable."""
    import nwn_ncs
    tools = os.path.join(tmp, "tools_x"); os.makedirs(tools)
    name = "zz_tool_" + str(os.getpid())
    exe = os.path.join(tools, name + (".exe" if os.name == "nt" else ""))
    with open(exe, "wb") as fh:
        fh.write(b"#!/bin/sh\nexit 0\n")
    no_exec_bit = "Windows has no execute bit: any existing file can be started"
    with mock.patch.object(n, "TOOLS_DIR", tools):
        if os.name != "nt":
            os.chmod(exe, 0o644)
            why = []
            got = n.find_tool(name, why)
            check("find_tool: a file that exists but cannot run is not returned, and the reason says so",
                  got is None and why and name in why[0] and "chmod" in why[0], (got, why))
            os.chmod(exe, 0o755)
        else:
            skip("find_tool: a file that exists but cannot run is not returned, and the reason says so", no_exec_bit)
        check("find_tool: a runnable program in the tools folder is found", n.find_tool(name) == exe, n.find_tool(name))
        names = ("disassemble: an nwn_asm that cannot run is reported as such (not as missing)",
                 "disassemble: a missing nwn_asm is named with its Windows file name too")
        if os.name == "nt":
            for nm in names:
                skip(nm, no_exec_bit)
        elif shutil.which("nwn_asm") is not None:
            for nm in names:
                skip(nm, "an nwn_asm on PATH would be found instead of the test's copy")
        else:
            asm = os.path.join(tools, "nwn_asm")
            with open(asm, "wb") as fh:
                fh.write(b"#!/bin/sh\nexit 0\n")
            os.chmod(asm, 0o644)
            text, err = nwn_ncs.disassemble(os.path.join(tmp, "x.ncs"))
            check(names[0], text is None and "chmod" in (err or ""), err)
            os.remove(asm)
            text, err = nwn_ncs.disassemble(os.path.join(tmp, "x.ncs"))
            check(names[1], text is None and "nwn_asm (nwn_asm.exe on Windows)" in (err or ""), err)
    check("nwn_ncs: the docs name nwn_asm (nwn_asm.exe on Windows)",
          "nwn_asm (nwn_asm.exe on Windows)" in nwn_ncs.__doc__ and "nwn_asm (nwn_asm.exe on Windows)" in nwn_ncs.disassemble.__doc__)
    src = open(n.__file__, encoding="utf-8").read()
    check("Erf.read: the fallback message names nwn_erf (nwn_erf.exe on Windows)", "nwn_erf (nwn_erf.exe on Windows)" in src)


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_formats_") as tmp:
        for fn, args in ((test_output_caps, (tmp,)), (test_zstd_errors, ()), (test_zstd_linear, ()),
                         (test_gff_field_index, (tmp,)), (test_mdl_textures, ()), (test_key_version, (tmp,)),
                         (test_write_erf_unknown_ext, (tmp,)), (test_erf_short_entry, (tmp,)),
                         (test_tlk_truncated, (tmp,)), (test_zstd_backend_and_hint, ()), (test_find_tool, (tmp,))):
            h.run(fn, *args)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
