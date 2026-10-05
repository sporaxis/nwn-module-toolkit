"""
Second synthetic module with the hard cases found by the independent review:
runtime-built script names (const prefix, suffix, 2-letter prefix, tag-based prefix variable),
a blueprint named only in a waypoint variable, identical scripts where only one is compiled,
a merge keeper that is otherwise unused, non-cp1252 bytes in a name, and palettes of two types.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nwnlib as n  # noqa: E402
from _gff import stand_in_ncs, st, root, R, X, DW, LST, loc, item_fields  # noqa: E402


def build(mod):
    os.makedirs(mod, exist_ok=True)

    def w(fn, data):
        with open(os.path.join(mod, fn), "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("cp1252"))

    g = lambda fn, r: w(fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Tricky"), Mod_OnModLoad=(R, "mod_load"),
                         Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]), Mod_HakList=(LST, [])))
    g("area001.are", root("ARE ", Name=loc("Town"), Tag=(X, "AREA_TOWN"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    gob = lambda rr: root("UTC ", TemplateResRef=(R, rr), Tag=(X, "GOBLIN"), FirstName=loc("Goblin"))  # noqa: E731
    g("goblin_a.utc", gob("goblin_a"))
    g("goblin_b.utc", gob("goblin_b"))          # only named in a spawner waypoint's variable
    g("goblin_b.uti", root("UTI ", **item_fields("goblin_b", "GOB_EAR", "Goblin ear", base=24, cost=3)))
    g("creaturepalcus.itp", root("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), LIST=(LST, [
        st(0, RESREF=(R, "goblin_a"), NAME=(X, "Gob")), st(0, RESREF=(R, "goblin_b"), NAME=(X, "Gob B"))]))])))
    g("itempalcus.itp", root("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), LIST=(LST, [
        st(0, RESREF=(R, "goblin_b"), NAME=(X, "Goblin ear"))]))])))
    g("dead_bp1.utp", root("UTP ", TemplateResRef=(R, "dead_bp1"), Tag=(X, "D1"), OnHeartbeat=(R, "hb_a")))
    g("area001.git", root("GIT ", **{
        "Creature List": (LST, [st(4, TemplateResRef=(R, "goblin_a"), Tag=(X, "GOBLIN"), FirstName=loc("QQQQ"))]),
        "Placeable List": (LST, [st(9, TemplateResRef=(R, "lamp"), Tag=(X, "LAMP"), OnHeartbeat=(R, "hb_b"),
                                    OnUsed=(R, "ev_b")),
                                 st(9, TemplateResRef=(R, "lamp"), Tag=(X, "LAMP2"), OnUsed=(R, "ev_a")),
                                 st(9, TemplateResRef=(R, "lamp"), Tag=(X, "LAMP3"), OnUsed=(R, "ev_a"))]),
        "WaypointList": (LST, [st(5, TemplateResRef=(R, "nw_waypoint001"), Tag=(X, "WP_SPAWN"),
                                  VarTable=(LST, [st(0, Name=(X, "SPAWN_RESREF"), Type=(DW, 3), Value=(X, "goblin_b"))]))]),
        "List": (LST, [st(0, **item_fields("wand", "magicwand", "Wand"))]),
    }))
    g("wand.uti", root("UTI ", **item_fields("wand", "magicwand", "Wand")))
    p = os.path.join(mod, "area001.git")
    b = open(p, "rb").read().replace(b"QQQQ", b"Q\x81\x8dQ")   # bytes cp1252 leaves undefined
    open(p, "wb").write(b)
    scripts = {
        "mod_load": 'void main() {\n SetLocalString(GetModule(), "MODULE_VAR_TAGBASED_SCRIPT_PREFIX", "i_");\n'
                    ' ExecuteScript("dispatch", OBJECT_SELF);\n}\n',
        "dispatch": 'const string PFX = "evt_";\nvoid main() {\n int n = 1;\n'
                    ' ExecuteScript(PFX + IntToString(n), OBJECT_SELF);\n'
                    ' ExecuteScript(GetTag(OBJECT_SELF) + "_ud", OBJECT_SELF);\n'
                    ' ExecuteScript("s_" + IntToString(n), OBJECT_SELF);\n}\n',
        "i_magicwand": 'void main() { }\n', "evt_1": 'void main() { }\n', "guard_ud": 'void main() { }\n',
        "s_1": 'void main() { }\n',
        "hb_a": 'void main() { SpeakString("hb"); }\n', "hb_b": 'void main() { SpeakString("hb"); }\n',
        "ev_a": 'void main() { SpeakString("used"); }\n', "ev_b": 'void main() { SpeakString("used"); }\n',
    }
    for k, v in scripts.items():
        w(k + ".nss", v)
        if k != "ev_a":
            w(k + ".ncs", stand_in_ncs(k))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "tricky_module")
