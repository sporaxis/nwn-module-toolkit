"""
nwn_ncs.py - read compiled NWScript (.ncs) without any external tool, and check it against its source.

Why this exists
---------------
The game runs the compiled .ncs, never the .nss source. When the two drift apart - the source was edited but never
recompiled, or the .ncs was damaged in a copy or a bad hak build - the game runs code nobody can see in the toolset.
This module catches both cases during an analysis:

    damaged  - wrong header or size, an unknown instruction, cut off part-way, or a jump/call whose target is not the
               start of an instruction (at run time the engine stops the script with "IP OUT OF CODE SEGMENT")
    stale    - the compiled code contains text that appears nowhere in the script's source or its includes, so it was
               compiled from a different version of the source

File format (as documented by neverwinter.nim's nwasm.nim, which is the reference for every size below)
-------------------------------------------------------------------------------------------------------
    bytes 0-7    "NCS V1.0"
    byte  8      'B'  (the "program" marker)
    bytes 9-12   total file size, uint32 big-endian
    then         instructions: opcode (1 byte), auxcode (1 byte), operands (0..n bytes; size depends on both)
All multi-byte numbers are big-endian. Jump operands are signed offsets from the start of the jump instruction.

Public functions
----------------
    parse(data)                 -> dict(ok, problem, instructions, strings, actions, size)    never raises
    source_literals(texts, ...) -> (literals, normalise)   the text a source can legitimately produce
    check_module(db, ...)       -> (issues, stats)         run both checks over every .ncs in the analysed module
    disassemble(path, ...)      -> (text, error)           readable listing from nwn_asm (nwn_asm.exe on Windows), optional

Limits (deliberate)
-------------------
The stale check compares text constants only. A change that alters numbers or logic but no text is not detected;
a reported stale script is always a real difference, but "not stale" is not a guarantee. Scripts whose includes
can't all be read are skipped rather than guessed at (see check_module).
"""
from __future__ import annotations

import os
import re
import struct
import subprocess

import nwnlib

HEADER = b"NCS V1.0"
HEADER_LEN = 13                                          # "NCS V1.0" + 'B' + uint32 size

# Opcodes whose operand size is fixed, whatever the auxcode. CONST (0x04) and EQUAL/NEQUAL (0x0b/0x0c) vary with the
# auxcode and are handled in _operand_size. Every opcode not listed here and not special-cased has no operands.
FIXED = {0x1d: 4, 0x1e: 4, 0x1f: 4, 0x25: 4,            # JMP JSR JZ JNZ          int32 relative offset
         0x2c: 8, 0x1b: 4, 0x05: 3,                       # STORE_STATE (2 x int32), MOVSP (int32), ACTION (uint16 + uint8)
         0x03: 6, 0x27: 6, 0x01: 6, 0x26: 6,              # CPTOPSP CPTOPBP CPDOWNSP CPDOWNBP   int32 + int16
         0x23: 4, 0x24: 4, 0x28: 4, 0x29: 4,              # DECSP INCSP DECBP INCBP              int32
         0x21: 6}                                         # DE_STRUCT                            3 x int16
KNOWN = set(range(0x01, 0x2e))                            # 0x01 ASSIGNMENT .. 0x2d NO_OPERATION
JUMPS = {0x1d, 0x1e, 0x1f, 0x25}                          # instructions whose operand is a jump target
OP_CONST, OP_ACTION = 0x04, 0x05
AUX_STRING = 0x05


def _operand_size(op, aux, data, pos):
    """Number of operand bytes for the instruction (op, aux) whose operands start at data[pos]."""
    if op == OP_CONST:
        if aux in (0x03, 0x04, 0x06, 0x12):              # int, float, object id, location preset: 4 bytes
            return 4
        if aux in (AUX_STRING, 0x17):                    # string, json: uint16 length, then that many bytes
            if pos + 2 > len(data):
                raise ValueError("cut off inside a text constant")
            return 2 + struct.unpack_from(">H", data, pos)[0]
        return 0
    if op in (0x0b, 0x0c):                               # EQUAL / NEQUAL: comparing two structs carries their size
        return 2 if aux == 0x24 else 0
    return FIXED.get(op, 0)


def parse(data):
    """Walk an .ncs instruction by instruction and report whether it is intact.

    Returns dict(ok, problem, instructions, strings, actions, size):
        ok            True when every check passed
        problem       plain-English reason when ok is False ("" otherwise)
        instructions  number of instructions (only counted when ok)
        strings       every text constant, in order (used by the stale check and shown in the dashboard)
        actions       every engine function called, as its number in nwscript.nss
    Never raises: a damaged file is a finding, not an error."""
    out = dict(ok=False, problem="", instructions=0, strings=[], actions=[], size=len(data))
    if data[:8] != HEADER:
        out["problem"] = "not a compiled script (no 'NCS V1.0' header)"
        return out
    if len(data) < HEADER_LEN or data[8:9] != b"B":
        out["problem"] = "header is cut off"
        return out
    declared = struct.unpack_from(">I", data, 9)[0]
    if declared != len(data):
        out["problem"] = f"the header says {declared} bytes but the file has {len(data)} - cut off or padded"
        return out

    pos, count, jumps = HEADER_LEN, 0, []                 # jumps: (where the jump is, where it goes)
    try:
        while pos < len(data):
            start = pos
            if pos + 2 > len(data):
                raise ValueError(f"cut off at byte {pos}")
            op, aux = data[pos], data[pos + 1]
            if op not in KNOWN:
                raise ValueError(f"unknown instruction 0x{op:02x} at byte {pos}")
            pos += 2
            size = _operand_size(op, aux, data, pos)
            if pos + size > len(data):
                raise ValueError(f"cut off inside an instruction at byte {start}")
            if op == OP_CONST and aux == AUX_STRING:
                length = struct.unpack_from(">H", data, pos)[0]
                # NWN text is Windows-1252, decoded the same lossless way as the index decodes .nss sources
                # (nwnlib.decode_text): the five bytes cp1252 leaves undefined must compare equal on both sides
                out["strings"].append(nwnlib.decode_text(data[pos + 2:pos + 2 + length]))
            elif op == OP_ACTION:
                out["actions"].append(struct.unpack_from(">H", data, pos)[0])
            elif op in JUMPS:
                jumps.append((start, start + struct.unpack_from(">i", data, pos)[0]))
            pos += size
            count += 1
    except (ValueError, struct.error) as ex:
        out["problem"] = str(ex)
        return out

    # Second pass: every jump must land on the first byte of an instruction (or exactly at the end, which returns).
    # Done after the walk because a jump can point forward to code not yet read.
    starts = _instruction_starts(data)
    bad = [(src, dst) for src, dst in jumps if dst not in starts]
    if bad:
        src, dst = bad[0]
        out["problem"] = (f"{len(bad)} jump(s) to a place that is not an instruction (first: at byte {src} to byte "
                          f"{dst}) - at run time this stops the script with IP OUT OF CODE SEGMENT")
        return out
    out.update(ok=True, instructions=count)
    return out


def _instruction_starts(data):
    """Byte offsets where instructions begin. Only called after parse() has walked the file without error,
    so no bounds checks are needed here."""
    starts, pos = set(), HEADER_LEN
    while pos < len(data):
        starts.add(pos)
        pos += 2 + _operand_size(data[pos], data[pos + 1], data, pos + 2)
    starts.add(len(data))
    return starts


# ---------------------------------------------------------------------------------------------- source literals
_STR = re.compile(r'"((?:\\.|[^"\\\n])*)"')               # a double-quoted NWScript string literal
_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'", "0": "\0"}
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_DATE = re.compile(r"\d{4}-\d\d-\d\d")                    # what __DATE__ becomes: 2026-10-01
_TIME = re.compile(r"\d\d:\d\d:\d\d")                     # what __TIME__ becomes: 11:33:23
_DATE_MARK, _TIME_MARK = "\0D", "\0T"                    # placeholders: can't occur in real script text


def _unescape(s):
    r"""Turn a literal as written in source ("a\nb", "\x41") into the text the compiler stores."""
    return re.sub(r"\\(x[0-9a-fA-F]{2}|.)",
                  lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "x" else _ESC.get(m.group(1), m.group(1)),
                  s)


def _explained(s, lits, memo):
    """Can the compiled text s be built from source literals?

    The compiler folds constant expressions, so  "Hello " + "world"  is stored as one constant "Hello world". We
    therefore accept s when it is a literal, or when it splits into literals end to end. This is a reachability
    scan over positions in s (no recursion, so very long strings are fine). memo caches answers and, under the key
    None, the distinct literal lengths; reuse one memo only with the same lits."""
    if s in lits:                                         # the common case, and cheap
        return True
    if s in memo:
        return memo[s]
    lengths = memo.get(None)
    if lengths is None:
        lengths = memo[None] = sorted({len(x) for x in lits if x})
    reach = [False] * (len(s) + 1)                        # reach[i]: s[:i] is made of literals
    reach[0] = True
    for i in range(len(s)):
        if reach[i]:
            for ln in lengths:
                if i + ln > len(s):
                    break                                 # lengths are sorted: the rest are longer
                if s[i:i + ln] in lits:
                    reach[i + ln] = True
    memo[s] = reach[len(s)]
    return memo[s]


def _file_literals(name, text):
    """What one source file contributes: (literals, flags, identifiers). Cached by check_module, since a big include
    such as a quest-token include is shared by hundreds of scripts and scanning it once per script would be slow."""
    lits = {_unescape(m) for m in _STR.findall(text)}
    flags = {m for m in ("__FILE__", "__FUNCTION__", "__DATE__", "__TIME__") if m in text}
    idents = set(_IDENT.findall(_STR.sub("", text))) if "__FUNCTION__" in text else set()
    return lits, flags, idents


def source_literals(texts, base=(), cache=None):
    """The text constants a script (with its includes) can legitimately produce.

    texts  {script name: source text} for the script and every file it includes
    base   literals that are always allowed (default values in nwscript.nss, e.g. string sTag="")
    cache  optional dict reused across calls (see _file_literals)

    Besides string literals, the compiler fills in text for four keywords: __FILE__ (a file name), __FUNCTION__ (the
    current function's name), __DATE__ and __TIME__ (when it was compiled). Dates and times differ on every compile,
    so they are compared through placeholders: normalise() turns them into the placeholders before the comparison.
    Returns (literals, normalise)."""
    lits, flags = set(base), set()
    for name, text in texts.items():
        if cache is not None and name in cache:
            got = cache[name]
        else:
            got = _file_literals(name, text)
            if cache is not None:
                cache[name] = got
        lits |= got[0]
        flags |= got[1]
        lits |= got[2]                                     # identifiers: only non-empty when __FUNCTION__ is used
    if "__FILE__" in flags:
        lits.update(f"{x}.nss" for x in texts)
    date, time_ = "__DATE__" in flags, "__TIME__" in flags
    if date:
        lits.add(_DATE_MARK)
    if time_:
        lits.add(_TIME_MARK)

    def normalise(s):
        if date:
            s = _DATE.sub(_DATE_MARK, s)
        if time_:
            s = _TIME.sub(_TIME_MARK, s)
        return s
    return lits, normalise


# ---------------------------------------------------------------------------------------------- the module check
def check_module(db, module_path=None, nwscript_text="", nwn_root=None, live=None):
    """Check every compiled script in the analysed module. Returns (issues, stats).

    db             the analysis index (index.sqlite): tables sources, files, scripts, edges
    module_path    the .mod file or unpacked module folder; default: the module source recorded in the index
    nwscript_text  the game's nwscript.nss, whose default parameter values (string s="") are legitimate constants
    nwn_root       the game install: includes that ship with the game (x0_i0_*, nw_i0_*) are read from its data
    live           node ids of scripts something in the module runs; others get "info" notes instead of warnings

    A script is only judged stale when its source AND every include can be read. If an include is missing (a base-
    game include with no nwn_root, or a hak not found) the literals it would add are unknown, so the script would
    look stale for the wrong reason - it is counted in stats["not_compared"] instead.

    stats: checked (all .ncs read), compared (stale check ran), damaged, stale, not_compared."""
    import nwnlib as n
    if module_path is None:
        row = db.execute("SELECT path FROM sources WHERE kind='module' LIMIT 1").fetchone()
        module_path = row[0] if row else None
    stats = dict(checked=0, compared=0, damaged=0, stale=0, not_compared=0)
    if not module_path or not os.path.exists(module_path):
        return [], stats

    # Script sources by name: the copy the toolset compiled against, which is the one the game loads - the first-
    # listed hak's, then the module's, then the override's (nwnlib.source_rank). Rows come in that order and the
    # first copy of a name is kept.
    sources = {}
    for name, src in db.execute("SELECT sc.name, sc.source FROM scripts sc JOIN files f ON f.id=sc.file_id "
                                "JOIN sources s ON s.id=f.source_id WHERE sc.source IS NOT NULL "
                                f"ORDER BY {n.source_rank_sql()}, s.id"):
        sources.setdefault(name.lower(), src)
    includes = {}                                          # script -> scripts it #includes directly
    for src, dst in db.execute("SELECT src, dst FROM edges WHERE kind='include'"):
        if src.startswith("script:") and dst.startswith("script:"):
            includes.setdefault(src[7:].lower(), set()).add(dst[7:].lower())
    base_lits = {_unescape(x) for x in _STR.findall(nwscript_text or "")}
    game = None
    if nwn_root:
        try:
            game = n.BaseGame(nwn_root)
        except Exception:  # noqa - the game's data is optional; without it base-game includes are "not compared"
            game = None

    def source_of(name):
        """Source text of a script or include, from the index or else the base game; None when not available."""
        if name not in sources:
            text = None
            if game is not None:
                try:
                    raw = game.get(name + ".nss")
                    text = n.decode_text(raw) if raw else None
                except Exception:  # noqa - an unreadable game resource just means "not available"
                    text = None
            sources[name] = text                          # cache misses too, so the game data is asked once
        return sources[name]

    ncs_files = [r[0] for r in db.execute("SELECT f.relpath FROM files f JOIN sources s ON s.id=f.source_id "
                                          "WHERE s.kind='module' AND f.ext='ncs'")]
    # A packed .mod is opened once for the whole run (and always closed: an open handle locks the file on Windows).
    erf = None if os.path.isdir(module_path) else n.Erf(module_path)
    entries = {e.filename.lower(): e for e in erf.entries} if erf else {}
    literal_cache = {}
    issues = []
    try:
        for rel in ncs_files:
            name = os.path.basename(rel).lower()[:-4]
            try:
                if erf:
                    data = erf.read(entries[os.path.basename(rel).lower()])
                else:
                    data = n.read_file_inside(module_path, rel)    # never through a link out of the module folder
            except (OSError, KeyError, ValueError):
                continue                                  # unreadable entries are reported by the index already
            result = parse(data)
            stats["checked"] += 1
            used = live is None or f"script:{name}" in live
            unused_note = "" if used else " (nothing in the module runs it)"

            if not result["ok"]:
                stats["damaged"] += 1
                issues.append(dict(severity="error" if used else "info", category="ncs_damaged",
                                   node=f"script:{name}", label=f"{name}.ncs",
                                   detail=f"the compiled script is damaged: {result['problem']}{unused_note}",
                                   fix="Recompile the script (toolset Build > Compile, or the toolkit's compile check). If "
                                       "there is no .nss source, get a good copy of the .ncs from a backup."))
                continue
            if sources.get(name) is None:
                continue                                  # compiled only: there is no source to compare with

            # The script plus everything it includes, directly or through other includes.
            closure, todo = set(), [name]
            while todo:
                x = todo.pop()
                if x not in closure:
                    closure.add(x)
                    todo.extend(includes.get(x, ()))
            texts = {x: source_of(x) for x in closure}
            if any(t is None for t in texts.values()):
                stats["not_compared"] += 1
                continue
            stats["compared"] += 1
            lits, normalise = source_literals(texts, base_lits, literal_cache)
            memo = {}
            unexplained = sorted({s for s in result["strings"] if s and not _explained(normalise(s), lits, memo)})
            if unexplained:
                stats["stale"] += 1
                shown = ", ".join(repr(s[:40]) for s in unexplained[:4])
                more = " ..." if len(unexplained) > 4 else ""
                issues.append(dict(severity="warning" if used else "info", category="ncs_stale",
                                   node=f"script:{name}", label=f"{name}.ncs",
                                   detail=f"the compiled script contains text its source (and includes) no longer has: "
                                          f"{shown}{more} - it was compiled from a different version of the source, and "
                                          f"the game runs the compiled version{unused_note}",
                                   fix="Compare first: Script panel > Compiled code shows what the game runs now. "
                                       "If that text is old, recompile the script; if it is right, the .ncs may have "
                                       "been compiled against a hak or a nwscript.nss this analysis didn't load - "
                                       "then leave it."))
    finally:
        if erf:
            erf.close()
    return issues, stats


# ---------------------------------------------------------------------------------------------- readable listing
def disassemble(ncs_path, nwn_root=None, nwn_user=None, timeout=60):
    """Readable listing of an .ncs from neverwinter.nim's nwn_asm (nwn_asm.exe on Windows), from the toolkit's tools
    folder or PATH. Returns (text, error).

    nwn_asm needs the game's nwscript.nss to print engine function names; it finds it through --root (game install)
    and --userdirectory. Without a game folder we retry with -L (no language spec): the listing then shows function
    numbers instead of names. Arguments are passed as a list (no shell), and the file is a temporary copy, so nothing
    in the module is touched."""
    import nwnlib as n
    why = []
    exe = n.find_tool("nwn_asm", why)
    if not exe:
        return None, why[0] if why else "nwn_asm (nwn_asm.exe on Windows) isn't in the toolkit's tools folder"
    # Flags (from nwn_asm's own usage text): -d disassemble to stdout; --no-color and --term-width 0 give plain,
    # unwrapped text; -n 60 prints up to 60 characters of each string (default 15); -G skips the .ndb debug file,
    # which modules don't ship.
    cmd = [exe, "-d", ncs_path, "--no-color", "--term-width", "0", "-n", "60", "-G"]
    if nwn_root:
        cmd += ["--root", nwn_root]
    if nwn_user:
        cmd += ["--userdirectory", nwn_user]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as ex:
        return None, str(ex)
    if proc.returncode != 0 and not nwn_root:
        try:
            proc = subprocess.run(cmd + ["-L"], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as ex:
            return None, str(ex)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "nwn_asm failed").strip()[-600:]
    return proc.stdout, None
