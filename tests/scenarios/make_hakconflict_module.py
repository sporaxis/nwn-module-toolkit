"""
Scenario: 2da/hak problems. Two haks both carry appearance.2da (the first listed wins, so the second hak's
custom rows never load), a hak baseitems.2da that is older than the game's (hides new base rows), a creature
whose head model does not exist, and a creature whose head does exist in the base game.
    python make_hakconflict_module.py <module folder> <hak folder A> [<hak folder B>] [<fake nwn root>]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(HERE, ".."))
import nwnlib as n  # noqa: E402
from _gff import st, root, R, X, LST, loc, w  # noqa: E402

BASE_APPEARANCE = "2DA V2.0\n\n   LABEL   RACE  MODELTYPE\n0  Dwarf   D     P\n1  Elf     E     P\n2  Human   H     P\n3  Rat     r     S\n"
BASE_BASEITEMS = "2DA V2.0\n\n   label      Name\n0  shortsword 100\n1  longsword  101\n2  battleaxe  102\n3  newpatchitem 103\n"


def build(mod, hak_a, hak_b=None, nwn_root=None):
    for d in (mod, hak_a, hak_b or hak_a):
        os.makedirs(d, exist_ok=True)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    haks = [st(8, Mod_Hak=(X, "hak_a"))] + ([st(8, Mod_Hak=(X, "hak_b"))] if hak_b else [])
    g("module.ifo", root("IFO ", Mod_Name=loc("Hak Conflicts"), Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]), Mod_HakList=(LST, haks)))
    g("area001.are", root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    g("area001.git", root("GIT ", **{"Creature List": (LST, [
        st(4, TemplateResRef=(R, "guard"), Tag=(X, "GUARD"), FirstName=loc("Guard"), Appearance_Type=(n.WORD, 2),
           Appearance_Head=(n.BYTE, 1), Gender=(n.BYTE, 0), Phenotype=(n.INT, 0)),
        st(4, TemplateResRef=(R, "noble"), Tag=(X, "NOBLE"), FirstName=loc("Noble"), Appearance_Type=(n.WORD, 2),
           Appearance_Head=(n.BYTE, 77), Gender=(n.BYTE, 1), Phenotype=(n.INT, 0))])}))
    g("guard.utc", root("UTC ", TemplateResRef=(R, "guard"), Tag=(X, "GUARD"), FirstName=loc("Guard"), Appearance_Type=(n.WORD, 2),
                        Appearance_Head=(n.BYTE, 1), Gender=(n.BYTE, 0), Phenotype=(n.INT, 0)))
    g("noble.utc", root("UTC ", TemplateResRef=(R, "noble"), Tag=(X, "NOBLE"), FirstName=loc("Noble"), Appearance_Type=(n.WORD, 2),
                        Appearance_Head=(n.BYTE, 77), Gender=(n.BYTE, 1), Phenotype=(n.INT, 0)))  # pfh0_head077 does not exist
    # hak A: appearance with one extra row; baseitems copied from an OLD game version (3 rows, no newpatchitem)
    w(hak_a, "appearance.2da", "2DA V2.0\n\n   LABEL   RACE  MODELTYPE\n0  Dwarf   D     P\n1  Elf     E     P\n2  Human   H     P\n3  Rat     r     S\n4  Lupin   L     P\n")
    w(hak_a, "baseitems.2da", "2DA V2.0\n\n   label      Name\n0  shortsword 100\n1  longsword  16777300\n2  battleaxe  102\n")
    w(hak_a, "hak_a_marker.txt", "a")
    if hak_b:  # hak B: its own custom appearance rows 5-6 that will NEVER load because hak A wins
        w(hak_b, "appearance.2da", "2DA V2.0\n\n   LABEL   RACE  MODELTYPE\n0  Dwarf   D     P\n1  Elf     E     P\n2  Human   H     P\n3  Rat     r     S\n4  Lupin   L     P\n5  Rakasta R     P\n6  Tortle  T     P\n")
    if nwn_root:
        n.write_key_bif(nwn_root, {("appearance", "2da"): BASE_APPEARANCE.encode(), ("baseitems", "2da"): BASE_BASEITEMS.encode(),
                                   ("pmh0_head001", "mdl"): b"\0\0\0\0", ("pfh0_head001", "mdl"): b"\0\0\0\0",
                                   ("nw_c2_default1", "ncs"): b"NCS"})


if __name__ == "__main__":
    a = sys.argv[1:]
    build(a[0], a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None)
