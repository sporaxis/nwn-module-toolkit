"""
_gff.py - small helpers that build GFF files (the game's binary structured format) for the test fixtures.

Why this exists: every fixture builder (make_test_module.py, scenarios/make_*_module.py) and many suites write
blueprints, areas and conversations by hand. These helpers keep that short and identical everywhere: a field is
written as Name=(type, value), e.g. st(4, Tag=(X, "GOBLIN"), FirstName=loc("Goblin")).

Reads nothing; w() writes the one file it is given. Limits: only what the fixtures need - the values are not
validated against the game's own blueprint rules (nwnlib.write_gff checks the format, not the meaning).
"""
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nwnlib as n  # noqa: E402

S, L = n.GffStruct, n.LocString
# field type codes, short so a fixture line stays readable: R resref, X string, LS localised string, B byte, I int,
# DW dword, F float, LST list, STR struct, WORD word
R, X, LS, B, I, DW, F, LST, STR = n.RESREF, n.CEXOSTRING, n.CEXOLOCSTRING, n.BYTE, n.INT, n.DWORD, n.FLOAT, n.LIST, n.STRUCT
WORD = n.WORD


def st(sid=0, **fields):
    """A GFF struct with struct id `sid` and the given fields (Name=(type, value))."""
    s = S(sid)
    for k, (t, v) in fields.items():
        s.set(k, t, v)
    return s


def root(ftype, **fields):
    """The top struct of a GFF file of type `ftype` ("UTI ", "ARE ", ...), with the given fields."""
    r = n.GffRoot(ftype)
    for k, (t, v) in fields.items():
        r.set(k, t, v)
    return r


def loc(txt):
    """A localised-string field value holding `txt` as language 0 (English)."""
    return (LS, L(entries={0: txt}))


def item_fields(resref, tag, name, base=1, props=(), cost=10):
    """The fields of an item blueprint (.uti) or a placed item: base item row `base`, item properties `props`
    (PropertyName numbers), a cost. Returned as a dict, so a test can change one field before building."""
    return dict(TemplateResRef=(R, resref), Tag=(X, tag), LocalizedName=loc(name),
                Description=loc(""), DescIdentified=loc(""), BaseItem=(I, base), Cost=(DW, cost),
                StackSize=(WORD, 1), Plot=(B, 0), Cursed=(B, 0), Charges=(B, 0),
                PaletteID=(B, 1), Comment=(X, ""), ModelPart1=(B, 11),
                PropertiesList=(LST, [st(0, PropertyName=(WORD, p), Subtype=(WORD, 0),
                                          CostTable=(B, 0), CostValue=(WORD, 1)) for p in props]))


def inst_item(resref, tag, name, **extra):
    """An item as it sits in an inventory or on the ground (struct id 0), with extra fields such as a position."""
    f = item_fields(resref, tag, name)
    f.update(extra)
    return st(0, **f)


def tga(w_, h_, fn):
    """Uncompressed 24-bit TGA with pixels from fn(x, y) -> (r, g, b)."""
    px = b"".join(bytes(fn(x, y)[::-1]) for y in range(h_) for x in range(w_))
    return bytes([0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0]) + struct.pack("<HH", w_, h_) + bytes([24, 0x20]) + px


def w(folder, fname, data, encoding="cp1252"):
    """Write folder/fname: bytes as they are, text encoded (cp1252 by default, the game's own text encoding)."""
    with open(os.path.join(folder, fname), "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode(encoding))


def stand_in_ncs(name=""):
    """A tiny but valid compiled script (push an int, pop it, return).

    The int is derived from the name so every stand-in has different bytes: identical files would be reported as
    duplicates, which the fixtures don't intend. Real .ncs layout: "NCS V1.0" "B" uint32-BE size, then instructions."""
    body = (b"\x04\x03" + struct.pack(">I", zlib.crc32(name.encode())) +   # CONST.I <crc of name>
            b"\x1b\x00" + struct.pack(">i", -4) +                         # MOVSP -4 (drop it again)
            b"\x20\x00")                                                  # RET
    return b"NCS V1.0B" + struct.pack(">I", 13 + len(body)) + body
