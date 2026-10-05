"""Pure-Python Zstandard decompressor (RFC 8878), standard library only.

Why this exists
---------------
NWN:EE stores most campaign-database strings and objects zstd-compressed ("CPDB" buffers), and EE's compressed
.hak/.erf entries (ERF "E1.0") use the same wrapper with the label "XRES". Python has no zstd in the standard library
before 3.14, so this reads it without installs. It is a straight, careful reading of the RFC (no dictionaries;
checksums are skipped, not verified) and is fast enough for database values and single hak entries (kilobytes to a
few megabytes, not gigabytes).

It only decompresses bytes it is given: it reads no files and writes nothing (apart from the speed hint below).

Public functions
----------------
    decompress(data, max_size=64 MB) -> bytes        one or more zstd frames (skippable frames ignored)
    decompress_buf(blob, max_size, magics) -> bytes  NWN's "compressed buffer" (CPDB / XRES): none, zlib or zstd
    decompress_cpdb(blob) -> bytes                   decompress_buf for campaign-database values ("CPDB" only)
All raise ZstdError (a ValueError) on damaged, truncated or unsupported input, or output over the cap.

How the decoder is laid out (section numbers are RFC 8878's)
-------------------------------------------------------------
    _py_decompress   error wrapper around _frames: frames (3.1.1), frame header (3.1.1.1), blocks (3.1.1.2),
                     skippable frames (3.1.2)
    _block           one compressed block (3.1.1.3): literals, then sequences, then sequence execution (3.1.1.4)
                     and the repeat-offset rules (3.1.1.5)
    _literals        literals section (3.1.1.3.1), including the 4-stream jump table
    _table           how each sequence code table is chosen (sequences section header, 3.1.1.3.2)
    _read_fse_table  FSE table description (4.1.1);  _build_fse  turns it into a decoding table
    _read_huffman    Huffman tree description (4.2.1);  _huf_stream  decodes one Huffman-coded stream (4.2.2)
The upstream description (zstd's doc/zstd_compression_format.md) has the same content and was used to check the code.

Limits
------
- No dictionaries (NWN does not use them: its buffers always store dictionary id 0).
- The content checksum is skipped, not verified; the frame's declared content size and window size are not enforced.
  A match may reach back to any byte the current frame has produced, never into an earlier frame.
- Output is capped by max_size (a guard against "decompression bombs": a small input that expands to gigabytes).
  The cap is enforced block by block in the pure-Python decoder and by an output limit in the faster libraries;
  decompress_buf applies the same cap to zlib data.

If the `zstandard` package or Python 3.14's `compression.zstd` is installed they are used instead (faster); the result
is the same. Which one is chosen once, when this module is imported (BACKEND). The pure-Python path prints one line to
stderr the first time it has handled more than SLOW_HINT_BYTES in a run, suggesting the zstandard package.
"""
import struct
import sys

MAGIC = 0xFD2FB528                                       # zstd frame magic number (RFC 8878 3.1.1), little-endian

# The decompressor, picked once here rather than by a failing import on every call (a campaign database can hold
# hundreds of thousands of values): Python 3.14's compression.zstd, else the zstandard package, else pure Python.
try:
    import compression.zstd as _cz                       # Python 3.14+
except ImportError:
    _cz = None
_zstandard = None
if _cz is None:
    try:
        import zstandard as _zstandard
    except ImportError:
        _zstandard = None
BACKEND = "compression.zstd" if _cz is not None else ("zstandard" if _zstandard is not None else "python")
SLOW_HINT_BYTES = 10 << 20     # pure-Python output in one run after which the speed hint is printed (once)
_SLOW = dict(bytes=0, told=False)


class ZstdError(ValueError):
    """Raised for damaged, truncated or unsupported compressed data (or output over the size cap)."""
    pass


def _hb(x):
    """Highest set bit (x > 0)."""
    return x.bit_length() - 1


# ---------------------------------------------------------------------------------------------------- bit readers
class _Fwd:
    """Little-endian bit reader, forwards (FSE table descriptions)."""
    def __init__(self, data, pos=0):
        # the whole remaining input as one Python integer: peeking n bits is then a shift and a mask
        self.v = int.from_bytes(data[pos:], "little")
        self.bit = 0
        self.nbits = (len(data) - pos) * 8

    def peek(self, n):
        return (self.v >> self.bit) & ((1 << n) - 1)

    def skip(self, n):
        self.bit += n


class _Back:
    """Backward bit reader used by Huffman and FSE streams. Reading past the start yields zeros (offset goes < 0).

    `off` is the bit position of the next unread bit; callers check it at the end to prove a stream was used up
    exactly (RFC 8878: "the bitstream shall be entirely consumed")."""
    def __init__(self, data):
        # the writer ends every backward stream with a single 1-bit then 0-7 zero bits of padding, so the last byte can
        # never be 0; reading starts just below that highest 1-bit of the last byte
        if not data or data[-1] == 0:
            raise ZstdError("bitstream without end marker")
        self.v = int.from_bytes(data, "little")
        self.off = len(data) * 8 - (8 - _hb(data[-1]))

    def read(self, n):
        if n == 0:
            return 0
        self.off -= n
        if self.off >= 0:
            return (self.v >> self.off) & ((1 << n) - 1)
        if self.off <= -n:
            return 0
        # partly past the start: the missing low bits read as zeros (the RFC's overflow rule for Huffman weights)
        return (self.v << -self.off) & ((1 << n) - 1)


# ---------------------------------------------------------------------------------------------------- FSE
def _read_fse_table(data, pos, max_al, max_sym):
    """Read an FSE table description (RFC 8878 4.1.1) starting at data[pos].

    max_al / max_sym are the largest accuracy log and symbol number allowed for this kind of table.
    Returns (probs, accuracy_log, bytes_used); probs[s] is symbol s's normalised count, -1 meaning "less than 1"."""
    r = _Fwd(data, pos)
    al = r.peek(4) + 5                                   # Accuracy_Log = low 4 bits + 5
    r.skip(4)
    if al > max_al:
        raise ZstdError("FSE accuracy log too large")
    remaining = 1 << al                                  # probability points still to hand out
    probs = []
    while remaining > 0 and len(probs) <= max_sym:
        # values 0..remaining+1 are possible, so read just enough bits for that; small values use one bit less
        # (the RFC's table "8-bit field read / Value decoded / Nb of bits consumed")
        bits = _hb(remaining + 1) + 1
        val = r.peek(bits)
        lower = (1 << (bits - 1)) - 1
        thr = (1 << bits) - 1 - (remaining + 1)          # how many values get the shorter code
        if (val & lower) < thr:
            val &= lower
            r.skip(bits - 1)
        else:
            if val > lower:
                val -= thr
            r.skip(bits)
        p = val - 1                                      # Probability = Value - 1; Value 0 means "-1" (less than 1)
        remaining -= -p if p < 0 else p                  # a "-1" still uses up one point
        probs.append(p)
        if p == 0:
            # a zero probability is followed by 2-bit repeat flags: that many more zeros; 3 means "and another flag"
            while True:
                rep = r.peek(2)
                r.skip(2)
                probs.extend([0] * rep)
                if rep != 3:
                    break
    # the points must add up exactly, the description must not run past the input, and no symbol may be out of range
    if remaining != 0 or r.bit > r.nbits or len(probs) > max_sym + 1:
        raise ZstdError("bad FSE table")
    return probs, al, (r.bit + 7) // 8                   # the description always uses whole bytes


def _build_fse(probs, al):
    """Build the FSE decoding table from a normalised distribution (RFC 8878 4.1.1, "From normalized distribution
    to decoding tables"). Returns (symbol, number_of_bits, baseline, accuracy_log): for state i the decoder outputs
    symbol[i], then the next state is baseline[i] + (number_of_bits[i] bits read from the stream)."""
    size = 1 << al
    syms = [0] * size
    state_desc = [0] * len(probs)
    high = size
    # "less than 1" symbols get one row each, filled from the end of the table backwards
    for s, p in enumerate(probs):
        if p == -1:
            high -= 1
            syms[high] = s
            state_desc[s] = 1
    # the other symbols are spread over the table with this fixed step (taken from the RFC), skipping those end rows
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    pos = 0
    for s, p in enumerate(probs):
        if p <= 0:
            continue
        state_desc[s] = p
        for _ in range(p):
            syms[pos] = s
            pos = (pos + step) & mask
            while pos >= high:
                pos = (pos + step) & mask
    if pos != 0:                                         # a valid distribution fills the table exactly and wraps to 0
        raise ZstdError("bad FSE distribution")
    nb = [0] * size
    base = [0] * size
    # each symbol's rows, in state order, share out the state space: lower rows read one bit more than higher rows
    for i in range(size):
        s = syms[i]
        nsd = state_desc[s]
        state_desc[s] += 1
        nb[i] = al - _hb(nsd)
        base[i] = (nsd << nb[i]) - size
    return (syms, nb, base, al)


def _rle_table(sym):
    """Decoding table for RLE_Mode: one state that always yields `sym` and reads no bits."""
    return ([sym], [0], [0], 0)


# ---------------------------------------------------------------------------------------------------- Huffman
def _huf_weights_fse(data, pos, size):
    """Huffman weights stored FSE-compressed in data[pos:pos+size] (RFC 8878 4.2.1, "FSE compression of Huffman
    weights"): one table (accuracy log at most 6), two interleaved states taking turns. Returns the weights."""
    probs, al, used = _read_fse_table(data[:pos + size], pos, 6, 255)
    t = _build_fse(probs, al)
    syms, nb, base, _ = t
    r = _Back(data[pos + used:pos + size])
    s1 = r.read(al)
    s2 = r.read(al)
    out = []
    # the number of weights is not stored: decoding stops when a state update runs past the start of the stream,
    # and then the other state's symbol is the last one (at most 255 weights)
    while len(out) < 255:
        out.append(syms[s1])
        s1 = base[s1] + r.read(nb[s1])
        if r.off < 0:
            out.append(syms[s2])
            break
        out.append(syms[s2])
        s2 = base[s2] + r.read(nb[s2])
        if r.off < 0:
            out.append(syms[s1])
            break
    return out


def _read_huffman(data, pos):
    """Read a Huffman tree description (RFC 8878 4.2.1) at data[pos] and build a lookup table.

    Returns ((symbols, nbits, max_bits), bytes_used). The table has 2**max_bits slots and is indexed by the next
    max_bits bits of the stream: symbols[slot] is the decoded byte, nbits[slot] how many of those bits its code used."""
    hdr = data[pos]
    if hdr < 128:                                        # header byte < 128: weights are FSE-compressed, hdr bytes long
        weights = _huf_weights_fse(data, pos + 1, hdr)
        used = 1 + hdr
    else:                                                # else hdr-127 weights as 4-bit values, high nibble first
        n = hdr - 127
        used = 1 + (n + 1) // 2
        weights = []
        for i in range(n):
            b = data[pos + 1 + i // 2]
            weights.append(b >> 4 if i % 2 == 0 else b & 15)
    # the last symbol's weight is not stored: it is whatever brings the sum of 2**(weight-1) up to a power of two
    total = sum(1 << (w - 1) for w in weights if w)
    if total == 0:
        raise ZstdError("empty Huffman tree")
    max_bits = _hb(total) + 1
    left = (1 << max_bits) - total
    if left & (left - 1):
        raise ZstdError("bad Huffman weights")
    weights.append(_hb(left) + 1)
    if max_bits > 11:                                    # the format's maximum code length
        raise ZstdError("Huffman code too long")
    # Number_of_Bits = Max_Number_of_Bits + 1 - Weight (weight 0 = symbol absent)
    bits = [(max_bits + 1 - w) if w else 0 for w in weights]
    count = [0] * (max_bits + 2)
    for b in bits:
        if b:
            count[b] += 1
    size = 1 << max_bits
    sym_t = [0] * size
    nb_t = [0] * size
    # codes are assigned in ascending order starting with the longest ones (RFC 4.2.1, "Conversion from weights to
    # Huffman prefix codes"), so each code length owns one block of slots, longest codes at the bottom; a symbol with
    # an n-bit code fills 2**(max_bits - n) consecutive slots. rank[n] is where the next n-bit code starts.
    rank = [0] * (max_bits + 2)
    rank[max_bits] = 0
    for i in range(max_bits, 0, -1):
        rank[i - 1] = rank[i] + count[i] * (1 << (max_bits - i))
        for j in range(rank[i], rank[i - 1]):
            nb_t[j] = i
    for s, b in enumerate(bits):
        if b:
            code = rank[b]
            ln = 1 << (max_bits - b)
            for j in range(code, code + ln):
                sym_t[j] = s
            rank[b] += ln
    return (sym_t, nb_t, max_bits), used


def _huf_stream(data, table, n, out):
    """Decode n bytes from one Huffman-coded stream (RFC 8878 4.2.2) and append them to `out` (a bytearray)."""
    sym_t, nb_t, mb = table
    r = _Back(data)
    state = r.read(mb)                                   # a window of the next max_bits bits
    mask = (1 << mb) - 1
    for _ in range(n):
        out.append(sym_t[state])
        b = nb_t[state]
        state = ((state << b) + r.read(b)) & mask        # drop the bits this code used, pull in as many new ones
    # the window always holds max_bits bits ahead, so a stream used up exactly ends max_bits before its start
    if r.off != -mb:
        raise ZstdError("Huffman stream size mismatch")


# ---------------------------------------------------------------------------------------------------- tables
# Literal-length and match-length codes: code c means BASE[c] + (BITS[c] extra bits read from the stream)
# (RFC 8878 3.1.1.3.2, "Literals length codes" / "Match length codes").
# The *_DEF lists are the predefined distributions used by Predefined_Mode ("Default Distributions"), accuracy log 6
# for literal and match lengths, 5 for offsets.
LL_BASE =list(range(16)) + [16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384,
                             32768, 65536]
LL_BITS = [0] * 16 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
ML_BASE = list(range(3, 35)) + [35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051, 4099, 8195,
                                16387, 32771, 65539]
ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
LL_DEF = [4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
          -1, -1, -1, -1]
ML_DEF = [1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
          1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1]
OF_DEF = [1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1]
_DEFAULTS = {}                                           # built once, on first use


def _default(kind):
    """The predefined decoding table for "ll" (literal lengths), "ml" (match lengths) or "of" (offsets)."""
    if kind not in _DEFAULTS:
        _DEFAULTS[kind] = {"ll": lambda: _build_fse(LL_DEF, 6), "ml": lambda: _build_fse(ML_DEF, 6),
                           "of": lambda: _build_fse(OF_DEF, 5)}[kind]()
    return _DEFAULTS[kind]


# ---------------------------------------------------------------------------------------------------- blocks
class _Frame:
    """State carried from one compressed block to the next within a frame: the last Huffman table (for treeless
    literals), the last sequence tables (for Repeat_Mode) and the three repeat offsets (RFC 8878 3.1.1.5)."""
    def __init__(self):
        self.huf = None
        self.tables = {"ll": None, "ml": None, "of": None}
        self.rep = [1, 4, 8]                             # the RFC's starting repeat offsets


def _literals(block, fr):
    """Decode the literals section (RFC 8878 3.1.1.3.1) at the start of a compressed block.

    Returns (literals bytes, position in `block` where the sequences section starts). Types (low 2 bits of byte 0):
    0 raw, 1 RLE (one byte repeated), 2 Huffman-compressed with a new tree, 3 "treeless" (reuses the frame's last tree).
    """
    b0 = block[0]
    ltype = b0 & 3
    sf = (b0 >> 2) & 3                                   # Size_Format: how many header bytes hold the sizes
    if ltype in (0, 1):
        # raw / RLE: only the regenerated size, in 5, 12 or 20 bits (1, 2 or 3 header bytes)
        if sf in (0, 2):
            size, hl = b0 >> 3, 1
        elif sf == 1:
            size, hl = (b0 >> 4) + (block[1] << 4), 2
        else:
            size, hl = (b0 >> 4) + (block[1] << 4) + (block[2] << 12), 3
        if ltype == 0:
            lit = block[hl:hl + size]
            if len(lit) != size:
                raise ZstdError("truncated literals")
            return bytes(lit), hl + size
        return bytes([block[hl]]) * size, hl + 1
    # compressed / treeless: regenerated and compressed sizes, each 10, 14 or 18 bits, after the 4 type/format bits
    if sf in (0, 1):
        hl, nbits = 3, 10
    elif sf == 2:
        hl, nbits = 4, 14
    else:
        hl, nbits = 5, 18
    h = int.from_bytes(block[:hl], "little")
    mask = (1 << nbits) - 1
    regen = (h >> 4) & mask
    comp = (h >> (4 + nbits)) & mask                     # includes the Huffman tree description when there is one
    streams = 1 if sf == 0 else 4
    pos = hl
    end = hl + comp
    if end > len(block):
        raise ZstdError("truncated literals")
    if ltype == 2:
        fr.huf, used = _read_huffman(block, pos)
        pos += used
    elif fr.huf is None:
        raise ZstdError("treeless literals without a previous table")
    out = bytearray()
    if streams == 1:
        _huf_stream(block[pos:end], fr.huf, regen, out)
    else:
        # 4 streams: a 6-byte jump table gives the compressed sizes of streams 1-3; stream 4 takes the rest.
        # Streams 1-3 each regenerate (regen + 3) // 4 bytes, stream 4 the remainder.
        s1, s2, s3 = struct.unpack_from("<HHH", block, pos)
        pos += 6
        each = (regen + 3) // 4
        cuts = [pos, pos + s1, pos + s1 + s2, pos + s1 + s2 + s3, end]
        if cuts[3] > end:
            raise ZstdError("bad jump table")
        for i in range(4):
            n = each if i < 3 else regen - 3 * each
            _huf_stream(block[cuts[i]:cuts[i + 1]], fr.huf, n, out)
    return bytes(out), end


def _table(block, pos, mode, kind, fr, max_al, max_sym):
    """Pick the decoding table for one sequence code ("ll", "of" or "ml") from its 2-bit compression mode
    (RFC 8878 3.1.1.3.2, Sequences_Section_Header): 0 predefined, 1 RLE (one symbol byte follows), 2 an FSE table
    description follows, 3 repeat the table this frame used last. Returns (table, position after any table bytes)."""
    if mode == 0:
        t = _default(kind)
    elif mode == 1:
        t = _rle_table(block[pos])
        pos += 1
    elif mode == 2:
        probs, al, used = _read_fse_table(block, pos, max_al, max_sym)
        t = _build_fse(probs, al)
        pos += used
    else:
        t = fr.tables[kind]
        if t is None:
            raise ZstdError("repeat table without a previous one")
    fr.tables[kind] = t                                  # remembered for a later Repeat_Mode
    return t, pos


def _block(block, fr, out, max_size):
    """Decode one compressed block (RFC 8878 3.1.1.3) and append its output to `out`.

    `out` must hold everything this frame has produced so far, because matches copy from earlier output.
    Raises ZstdError when damaged or when `out` grows past max_size."""
    lit, pos = _literals(block, fr)
    b0 = block[pos]
    if b0 == 0:                                          # no sequences: the block is just its literals
        out += lit
        return
    # Number_of_Sequences: 1, 2 or 3 bytes depending on the first byte's value
    if b0 < 128:
        nseq, pos = b0, pos + 1
    elif b0 < 255:
        nseq, pos = ((b0 - 128) << 8) + block[pos + 1], pos + 2
    else:
        nseq, pos = block[pos + 1] + (block[pos + 2] << 8) + 0x7F00, pos + 3
    # one byte of modes: bits 7-6 literal lengths, 5-4 offsets, 3-2 match lengths (bits 1-0 reserved)
    modes = block[pos]
    pos += 1
    # the limits are the RFC's: accuracy log at most 9 / 8 / 9; codes 0-35 (lengths), 0-52 (matches); offsets up
    # to code 31 is this decoder's own limit (the RFC lets a decoder reject larger ones)
    ll_t, pos = _table(block, pos, modes >> 6, "ll", fr, 9, 35)
    of_t, pos = _table(block, pos, (modes >> 4) & 3, "of", fr, 8, 31)
    ml_t, pos = _table(block, pos, (modes >> 2) & 3, "ml", fr, 9, 52)
    r = _Back(block[pos:])                               # the rest of the block is one backward bitstream
    lls, llnb, llb, llal = ll_t
    ofs, ofnb, ofb, ofal = of_t
    mls, mlnb, mlb, mlal = ml_t
    # starting states, in this order: literal length, offset, match length
    lst = r.read(llal)
    ost = r.read(ofal)
    mst = r.read(mlal)
    rep = fr.rep
    lp = 0                                               # how many literals have been copied out so far
    for i in range(nseq):
        oc = ofs[ost]
        lc = lls[lst]
        mc = mls[mst]
        if oc > 31 or lc > 35 or mc > 52:
            raise ZstdError("bad sequence code")
        # extra bits are read offset first, then match length, then literal length (RFC "Decoding a sequence")
        offv = (1 << oc) + r.read(oc)
        ml = ML_BASE[mc] + r.read(ML_BITS[mc])
        ll = LL_BASE[lc] + r.read(LL_BITS[lc])
        if i != nseq - 1:                                # states are updated (ll, ml, of) except after the last one
            lst = llb[lst] + r.read(llnb[lst])
            mst = mlb[mst] + r.read(mlnb[mst])
            ost = ofb[ost] + r.read(ofnb[ost])
        # offset values above 3 are real offsets (value - 3); 1-3 pick a repeat offset (RFC 8878 3.1.1.5)
        if offv > 3:
            off = offv - 3
            rep[2], rep[1], rep[0] = rep[1], rep[0], off
        else:
            # with no literals before the match the choices shift by one, and "3" then means repeat offset 1 minus 1
            idx = offv - 1 + (1 if ll == 0 else 0)
            if idx == 0:
                off = rep[0]
            else:
                off = rep[idx] if idx < 3 else rep[0] - 1
                if idx > 1:
                    rep[2] = rep[1]
                rep[1] = rep[0]
                rep[0] = off
        # sequence execution (RFC 8878 3.1.1.4): copy ll literals, then ml bytes from `off` bytes back
        if lp + ll > len(lit):
            raise ZstdError("literal length past the literals")
        out += lit[lp:lp + ll]
        lp += ll
        if off == 0 or off > len(out):
            raise ZstdError("match offset before the start")
        start = len(out) - off
        if off >= ml:
            out += out[start:start + ml]
        else:
            # the match overlaps the bytes it is producing (e.g. offset 1 repeats one byte): copy byte by byte
            for k in range(ml):
                out.append(out[start + k])
        if len(out) > max_size:
            raise ZstdError("output larger than allowed")
    if r.off != 0:                                       # the bitstream must be used up exactly
        raise ZstdError("sequence stream size mismatch")
    out += lit[lp:]                                      # literals left after the last sequence end the block


def _py_decompress(data, max_size):
    """Decode every frame in `data` (RFC 8878 3.1) and return the joined output; raises ZstdError past max_size.
    Every failure is a ZstdError: an input cut off anywhere makes a reader run past the end of its bytes, and that
    IndexError / struct.error is turned into one here rather than checked at each of the dozens of reads."""
    try:
        return _frames(bytes(data), max_size)
    except (IndexError, struct.error):
        raise ZstdError("truncated or damaged data") from None


def _frames(data, max_size):
    """The frame loop of _py_decompress (data is bytes)."""
    out = bytearray()
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ZstdError("truncated frame")
        magic = struct.unpack_from("<I", data, pos)[0]
        if 0x184D2A50 <= magic <= 0x184D2A5F:          # skippable frame (3.1.2): magic, 4-byte size, user data
            size = struct.unpack_from("<I", data, pos + 4)[0]
            pos += 8 + size
            continue
        if magic != MAGIC:
            raise ZstdError("not a zstd frame")
        pos += 4
        # Frame_Header_Descriptor (3.1.1.1.1): bits 7-6 content-size flag, 5 single segment, 3 reserved,
        # 2 checksum present, 1-0 dictionary-id flag
        fhd = data[pos]
        pos += 1
        fcs_flag, single, checksum, did_flag = fhd >> 6, (fhd >> 5) & 1, (fhd >> 2) & 1, fhd & 3
        if fhd & 8:
            raise ZstdError("reserved bit set")
        if not single:
            pos += 1                                     # window descriptor (not needed: the window is not enforced)
        did_size = (0, 1, 2, 4)[did_flag]
        if did_size and int.from_bytes(data[pos:pos + did_size], "little"):
            raise ZstdError("dictionaries are not supported")
        pos += did_size
        # Frame_Content_Size field: 0/1, 2, 4 or 8 bytes (flag 0 means 1 byte only for a single-segment frame); skipped
        pos += (1 if single else 0, 2, 4, 8)[fcs_flag]
        fr = _Frame()
        # the frame's blocks decode into one buffer of their own (a match may reach back into any earlier block of
        # the same frame, never into an earlier frame); it is joined to `out` once the frame ends, so a frame of many
        # blocks costs one copy, not one copy per block
        frame_out = bytearray()
        budget = max_size - len(out)                     # what this frame may add before the cap is passed
        while True:
            # block header (3.1.1.2): 3 bytes little-endian; bit 0 last block, bits 1-2 type, bits 3-23 size
            if pos + 3 > len(data):
                raise ZstdError("truncated block")
            bh = int.from_bytes(data[pos:pos + 3], "little")
            pos += 3
            last, btype, bsize = bh & 1, (bh >> 1) & 3, bh >> 3
            if btype == 0:                               # raw block: bsize bytes copied as they are
                if pos + bsize > len(data):
                    raise ZstdError("truncated raw block")
                frame_out += data[pos:pos + bsize]
                pos += bsize
            elif btype == 1:                             # RLE block: one byte, repeated bsize times
                frame_out += bytes([data[pos]]) * bsize
                pos += 1
            elif btype == 2:                             # compressed block
                if pos + bsize > len(data):
                    raise ZstdError("truncated block")
                _block(data[pos:pos + bsize], fr, frame_out, budget)
                pos += bsize
            else:
                raise ZstdError("reserved block type")
            if len(frame_out) > budget:
                raise ZstdError("output larger than allowed")
            if last:
                break
        out += frame_out
        if checksum:
            pos += 4                                     # 32-bit content checksum: skipped, not verified
    return bytes(out)


def decompress(data, max_size=64 << 20):
    """Decompress zstd data (one or more frames) and return the bytes.

    max_size caps the output: every path raises ZstdError when the output would pass it (a small input that expands
    to gigabytes must never be read whole). With Python 3.14's compression.zstd each frame goes through a decompressor
    object with an output limit; with the `zstandard` package the input is fed in slices and the output measured as it
    grows. Both libraries' own errors, and a cut-off input, are raised as ZstdError. The library is the one chosen
    at import (BACKEND)."""
    data = bytes(data)
    if BACKEND == "compression.zstd":
        cz = _cz
        # cz.decompress() has no output limit, so one decompressor object per frame with max_length: a frame that
        # took all its input is finished (eof); what it did not use is the next frame
        out = bytearray()
        try:
            while data:
                d = cz.ZstdDecompressor()
                out += d.decompress(data, max_size - len(out) + 1)
                if len(out) > max_size:
                    raise ZstdError("output larger than allowed")
                if not d.eof:
                    raise ZstdError("truncated frame")
                data = d.unused_data
        except cz.ZstdError as ex:
            raise ZstdError(str(ex)) from None
        return bytes(out)
    if BACKEND == "zstandard":
        zstandard = _zstandard
        # zstandard's one-shot decompress() ignores max_output_size when the frame declares its content size, and
        # its stream reader returns a cut-off frame without complaint, so the input is fed to a decompressor object
        # in small slices and the output measured as it grows (one 1 KB slice can expand to at most ~32 MB: an RLE
        # block is 4 bytes of input for 128 KB of output). A finished frame sets eof; what it did not use is the next
        # frame (skippable frames count as finished frames with no output).
        out = bytearray()
        rest = data
        try:
            while rest:
                d = zstandard.ZstdDecompressor().decompressobj()
                pos = 0
                while pos < len(rest) and not d.eof:
                    out += d.decompress(rest[pos:pos + 1024])
                    pos += 1024
                    if len(out) > max_size:
                        raise ZstdError("output larger than allowed")
                if not d.eof:
                    raise ZstdError("truncated frame")
                rest = d.unused_data + rest[pos:]
        except zstandard.ZstdError as ex:
            raise ZstdError(str(ex)) from None
        return bytes(out)
    out = _py_decompress(data, max_size)
    _note_slow(len(out))
    return out


def _note_slow(n):
    """Count pure-Python output; the first time a run passes SLOW_HINT_BYTES, print one line to stderr (stdout may
    carry machine-readable output, e.g. the dashboard's progress lines). Never raises."""
    _SLOW["bytes"] += n
    if not _SLOW["told"] and _SLOW["bytes"] > SLOW_HINT_BYTES:
        _SLOW["told"] = True
        try:
            print("Note: compressed data is being read in pure Python, which is slow - installing the zstandard "
                  "package makes this much faster: pip install zstandard", file=sys.stderr)
        except Exception:  # noqa - a closed or missing stderr must not break reading
            pass


def decompress_buf(blob, max_size=64 << 20, magics=(b"CPDB", b"XRES")):
    """NWN:EE "compressed buffer": 4-byte label (CPDB in campaign databases, XRES in compressed .hak/.erf entries),
    header version 3, algorithm (0 none, 1 zlib, 2 zstd), uncompressed size, then the data (zlib: version + stream;
    zstd: version + dictionary id + frame). Same layout as neverwinter.nim's compressedbuf.

    Layout (all uint32 little-endian):
        0  label ("CPDB" / "XRES")    4  header version (3; not checked here)
        8  algorithm                  12 uncompressed size        16 body
    blob: the whole buffer (bytes-like). max_size: refuse a buffer whose declared size is larger (bomb guard).
    magics: the labels accepted. Returns exactly `uncompressed size` bytes; raises ZstdError otherwise."""
    blob = bytes(blob)
    if blob[:4] not in magics:
        raise ZstdError("not a compressed buffer")
    if len(blob) < 16:
        raise ZstdError("truncated compressed buffer")
    _ver, algo, size = struct.unpack_from("<3I", blob, 4)
    if size > max_size:                                  # checked before any work, from the declared size
        raise ZstdError("value larger than allowed")
    if size == 0:
        return b""
    body = blob[16:]
    if algo == 0:
        out = body[:size]
    elif algo == 1:
        import zlib
        # body = uint32 zlib header version, then a zlib stream. A decompressor object with a length limit stops
        # producing output at size + 1 bytes, so a stream that would expand past the declared size is refused
        # without ever being read whole (zlib.decompress has no such limit)
        try:
            out = zlib.decompressobj().decompress(body[4:], size + 1)
        except zlib.error as ex:
            raise ZstdError(f"damaged zlib data: {ex}") from None
        if len(out) > size:
            raise ZstdError("output larger than allowed")
    elif algo == 2:
        # body = uint32 zstd header version, uint32 dictionary id (NWN always writes 0), then the zstd frame;
        # the declared size doubles as the output cap
        if int.from_bytes(body[4:8], "little"):
            raise ZstdError("zstd dictionaries are not supported")
        out = decompress(body[8:], size)
    else:
        raise ZstdError(f"unknown compression algorithm {algo}")
    if len(out) < size:
        raise ZstdError("data shorter than the size it claims")
    return out[:size]


def decompress_cpdb(blob, max_size=64 << 20):
    """Campaign-database value ('CPDB' compressed buffer): the uncompressed bytes. Raises ZstdError on any other
    label, damage, or a declared size over max_size."""
    return decompress_buf(blob, max_size, (b"CPDB",))
