"""
Builds a small synthetic NWN module folder with known problems, so the toolkit
can be checked end to end. Every planted problem is listed in EXPECTED.
    python tests/make_test_module.py <out_folder>
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nwnlib as n  # noqa: E402
# the GFF helpers live in _gff.py (shared with the scenario builders); they are re-exported here because older
# fixture code imports them from this module
from _gff import (S, L, R, X, LS, B, I, DW, F, LST, STR, WORD, st, root, loc, item_fields,  # noqa: E402,F401
                  inst_item, tga, w, stand_in_ncs)


def build(out):
    os.makedirs(out, exist_ok=True)
    g = lambda fn, r: w(out, fn, n.write_gff(r))  # noqa: E731

    # ---- module.ifo
    n.write_tlk(os.path.join(out, "testmod_custom.tlk"), {0: "Blade of the Test", 1: "Never referenced", 3: "Custom Rat"})
    g("module.ifo", root("IFO ", Mod_Name=loc("Test Module"), Mod_CustomTlk=(X, "testmod_custom"),
                         Mod_OnModLoad=(R, "mod_load"), Mod_OnActvtItem=(R, "x2_mod_def_act"),
                         Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001")), st(6, Area_Name=(R, "area002"))]),
                         Mod_HakList=(LST, [])))

    # ---- journal
    g("module.jrl", root("JRL ", Categories=(LST, [st(0, Tag=(X, "q_rats"), Name=loc("Rat Problem"),
                                                         EntryList=(LST, [st(0, ID=(DW, 1), Text=loc("Kill rats"), End=(n.WORD, 0)),
                                                                          st(0, ID=(DW, 2), Text=loc("Done"), End=(n.WORD, 1))]))])))

    # ---- item blueprints
    g("sword_a.uti", root("UTI ", **item_fields("sword_a", "SWORD_A", "Long Sword", props=(6,))))
    g("sword_b.uti", root("UTI ", **item_fields("sword_b", "SWORD_A", "Long Sword", props=(6,))))   # identical to a
    g("sword_c.uti", root("UTI ", **item_fields("sword_c", "SWORD_C", "Fine Sword", props=(6,))))   # functional dup
    tl = item_fields("tlk_blade", "TLK_BLADE", "", base=1)
    tl["LocalizedName"] = (LS, L(strref=0x01000000, entries={}))          # name from the custom tlk
    tl["Description"] = (LS, L(strref=0x01000007, entries={}))            # broken: entry 7 does not exist
    g("tlk_blade.uti", root("UTI ", **tl))
    g("key_rusty.uti", root("UTI ", **item_fields("key_rusty", "KEY_RUSTY", "Rusty Key", base=24, cost=1)))
    g("key_rusty2.uti", root("UTI ", **item_fields("key_rusty2", "KEY_RUSTY", "Old Key", base=24, cost=2)))  # tag clash
    g("potion.uti", root("UTI ", **item_fields("potion", "POTION", "Healing Potion", base=49, cost=50)))
    g("unused_helm.uti", root("UTI ", **item_fields("unused_helm", "UNUSED_HELM", "Dusty Helm", base=17)))
    g("magic_ring.uti", root("UTI ", **item_fields("magic_ring", "magic_ring", "Ring of Tags", base=52)))  # tag-based script
    g("rat_hide.uti", root("UTI ", **item_fields("rat_hide", "RAT_HIDE", "Rat Hide", base=42, props=(1, 2))))   # creature skin
    g("q_relic.uti", root("UTI ", **item_fields("q_relic", "Q_RELIC", "Lost Relic", base=52)))  # quest item never placed

    # ---- other blueprints
    g("npc_merchant.utc", root("UTC ", TemplateResRef=(R, "npc_merchant"), Tag=(X, "NPC_MERCHANT"),
                               FirstName=loc("Bob"), LastName=loc("Trader"), Conversation=(R, "merchant_dlg"),
                               ScriptHeartbeat=(R, "nw_c2_default1"), ScriptSpawn=(R, "not_here"),
                               ItemList=(LST, [st(0, InventoryRes=(R, "potion"))])))
    g("rat.utc", root("UTC ", TemplateResRef=(R, "rat"), Tag=(X, "RAT"), FirstName=loc("Rat"), Appearance_Type=(n.WORD, 0),
                      ScriptDeath=(R, "rat_death"),
                      Equip_ItemList=(LST, [st(131072, EquippedRes=(R, "rat_hide")), st(32768, EquippedRes=(R, "nw_crewpsp"))])))
    g("statue_npc.utc", root("UTC ", TemplateResRef=(R, "statue_npc"), Tag=(X, "STATUE_NPC"), FirstName=loc("Statue Guard"),
                             ScriptAttacked=(R, ""), ScriptDamaged=(R, "noop_dmg"), ScriptOnNotice=(R, "nw_c2_default2"),
                             ScriptEndRound=(R, "nw_c2_default3"), ScriptHeartbeat=(R, "nw_c2_default1")))
    g("chest.utp", root("UTP ", TemplateResRef=(R, "chest"), Tag=(X, "CHEST"), LocName=loc("Chest"),
                        OnOpen=(R, "")))

    # ---- areas
    g("area001.are", root("ARE ", Name=loc("Town"), Tag=(X, "AREA_TOWN"), ResRef=(R, "area001"),
                          OnEnter=(R, "area_enter"), Tileset=(R, "tcn01")))
    g("area001.git", root("GIT ",
        **{"Creature List": (LST, [st(4, TemplateResRef=(R, "npc_merchant"), Tag=(X, "NPC_MERCHANT"),
                                        FirstName=loc("Bob"), LastName=loc("Trader"), XPosition=(F, 10.0),
                                        YPosition=(F, 12.0), ZPosition=(F, 0.0),
                                        Conversation=(R, "merchant_dlg"), ScriptHeartbeat=(R, "nw_c2_default1"),
                                        ItemList=(LST, [inst_item("sword_a", "SWORD_A", "Long Sword")]))]),
           "Placeable List": (LST, [st(9, TemplateResRef=(R, "chest"), Tag=(X, "CHEST"), LocName=loc("Chest"),
                                         X=(F, 3.0), Y=(F, 4.0), Z=(F, 0.0),
                                         ItemList=(LST, [inst_item("potion", "POTION", "Healing Potion"),
                                                         inst_item("sword_b", "SWORD_A", "Long Sword")]))]),
           "List": (LST, [inst_item("key_rusty", "KEY_RUSTY", "Rusty Key", XPosition=(F, 5.0), YPosition=(F, 5.0),
                                    ZPosition=(F, 0.0))]),
           "Door List": (LST, [st(8, TemplateResRef=(R, "door_iron"), Tag=(X, "DOOR_CELLAR"), LocName=loc("Cellar Door"),
                                    KeyName=(X, "KEY_RUSTY"), LinkedTo=(X, "WP_DUNGEON"), OnOpen=(R, "door_open"),
                                    OnFailToOpen=(R, "bad_cond"))]),
           "StoreList": (LST, [st(11, TemplateResRef=(R, "store_gen"), Tag=(X, "STORE_GEN"), LocName=loc("Shop"),
                                    StoreList=(LST, [st(0, ItemList=(LST, [inst_item("sword_c", "SWORD_C", "Fine Sword")]))]))]),
           }))
    g("area002.are", root("ARE ", Name=loc("Dungeon"), Tag=(X, "AREA_DUNGEON"), ResRef=(R, "area002")))
    g("area002.git", root("GIT ", **{
        "WaypointList": (LST, [st(5, TemplateResRef=(R, "nw_waypoint001"), Tag=(X, "WP_DUNGEON"),
                                   LocalizedName=loc("Dungeon entrance"))]),
        "Creature List": (LST, [st(4, TemplateResRef=(R, "rat"), Tag=(X, "RAT"), FirstName=loc("Rat"),
                                    ScriptDeath=(R, "rat_death"))])}))
    # area003: not in module area list, nothing transitions to it
    g("area003.are", root("ARE ", Name=loc("Old Test Area"), Tag=(X, "AREA_OLD"), ResRef=(R, "area003"),
                          OnEnter=(R, "old_enter")))
    g("area003.git", root("GIT ", **{"Creature List": (LST, [])}))

    # ---- conversation
    E = lambda txt, script="", quest="", qe=0, replies=(): st(0, Text=loc(txt), Speaker=(X, ""), Script=(R, script),  # noqa
                                                                 Quest=(X, quest), QuestEntry=(DW, qe),
                                                                 RepliesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in replies]))
    Rp = lambda txt, entries=(), script="": st(0, Text=loc(txt), Script=(R, script), Quest=(X, ""),  # noqa
                                               EntriesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in entries]))
    g("oldmine_dlg.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [E("The old mine is closed.", script="at_oldmine", quest="q_oldmine", qe=1)]),
        ReplyList=(LST, []), StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, "sc_oldmine"))])))
    g("merchant_dlg.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [E("Welcome, traveller!", replies=[(0, ""), (1, "sc_has_key"), (2, "q_relic_check")]),
                         E("Thanks for clearing the rats. Take this.", script="at_give_reward", quest="q_rats", qe=2)]),
        ReplyList=(LST, [Rp("Goodbye."), Rp("I found this key in the cellar.", entries=[(1, "")]), Rp("I have the relic.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    # ---- scripts
    scripts = {
        "mod_load": '#include "inc_common"\nvoid main() { SetTownOpen(); }\n',
        "inc_common": 'void SetTownOpen() { SetLocalInt(GetModule(), "TOWN_OPEN", 1); }\n',
        "area_enter": 'void main() {\n object oPC = GetEnteringObject();\n'
                      ' if (GetLocalInt(GetModule(), "TOWN_OPEN")) AddJournalQuestEntry("q_rats", 1, oPC);\n'
                      ' ExecuteScript("dyn_" + IntToString(1), oPC);\n}\n',
        "door_open": 'void main() { object o = GetItemPossessedBy(GetLastOpenedBy(), "KEY_RUSTY"); }\n',
        "sc_has_key": 'int StartingConditional() { return GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "KEY_RUSTY")); }\n',
        "at_give_reward": '// reward\nvoid main() {\n  CreateItemOnObject("sword_a", GetPCSpeaker());\n'
                          '  AddJournalQuestEntry("q_rats", 2, GetPCSpeaker());\n}\n',
        "at_reward_copy": '/* old copy */ void main()\n{\n CreateItemOnObject("sword_a", GetPCSpeaker());\n'
                               ' AddJournalQuestEntry("q_rats", 2, GetPCSpeaker()); // same\n}\n',
        "rat_death": 'void main() { }\n',
        "old_unused": '#include "dead_lib"\nvoid main() { Dead(); }\n',
        "dead_lib": 'void Dead() { }\n',
        "dyn_1": 'void main() { }\n',
        "bad_cond": 'int StartingConditional() { return TRUE; }\n',
        "magic_ring": 'void main() { }\n',
        "old_enter": 'void main() { }\n',
        "noop_dmg": 'void main() { SetLocalInt(OBJECT_SELF, "HIT", 1); }\n',   # custom OnDamaged that never fights back
        # legacy quest leftovers: a removed quest 'q_oldmine' - dialogue + scripts nothing uses
        "sc_oldmine": 'int StartingConditional() { return GetLocalInt(GetPCSpeaker(), "OLDMINE") == 1; }\n',
        "at_oldmine": 'void main() { AddJournalQuestEntry("q_oldmine", 2, GetPCSpeaker()); CreateItemOnObject("mine_deed", GetPCSpeaker()); }\n',
        "q_oldmine_chk": 'void main() { object o = GetItemPossessedBy(GetFirstPC(), "MINE_KEY"); }\n',
        # live quest that needs an item that exists but is never obtainable
        "q_relic_check": 'int StartingConditional() { AddJournalQuestEntry("q_rats", 1, GetPCSpeaker()); return GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "Q_RELIC")); }\n',
    }
    for name, src in scripts.items():
        w(out, f"{name}.nss", src)
        if name not in ("rat_death", "dyn_1"):
            w(out, f"{name}.ncs", stand_in_ncs(name))   # stand-in compiled file
    w(out, "orphan_only.ncs", stand_in_ncs("orphan_only"))  # compiled script with no source

    # ---- assets
    w(out, "appearance.2da", '2DA V2.0\n\n   LABEL   RACE\n0  Rat     c_rat\n1  Bear    ****\n')
    w(out, "c_rat.mdl", b"\0\0\0\0binarymodel c_rat_tex \0\0")
    w(out, "c_rat_tex.tga", tga(64, 64, lambda x, y: (120 + x, 90 + y // 2, 60)))
    w(out, "sword.mdl", "newmodel sword\nsetsupermodel sword NULL\nnode trimesh blade\n  bitmap sword_tex\nendnode\n")
    w(out, "sword_tex.tga", tga(64, 64, lambda x, y: (x * 4, y * 4, 200) if (x // 8 + y // 8) % 2 else (230, 230, 240)))
    w(out, "sword_tex.txi", "mipmap 0\n")
    w(out, "orphan_tex.tga", b"\0" * 64)
    w(out, "po_hero_h.tga", b"\0" * 64)
    w(out, "unused_model.mdl", "newmodel unused_model\n  bitmap orphan2_tex\n")
    w(out, "unused_model.wok", "walkmesh\n")
    w(out, "orphan2_tex.tga", b"\0" * 64)


EXPECTED = """
Planted problems / facts the reports must find:
 duplicates : sword_a == sword_b (identical); sword_c same function, different name;
              key_rusty & key_rusty2 share tag KEY_RUSTY; at_give_reward == at_reward_copy
 quest link : KEY_RUSTY is used by door_open, sc_has_key (merchant_dlg) -> q_rats via dlg
 missing    : merchant ScriptSpawn 'not_here' (error); nw_c2_default1, x2_mod_def_act (base game)
 wrong type : bad_cond used as door OnFailToOpen but only has StartingConditional
 not compiled: rat_death, dyn_1 ; compiled w/o source: orphan_only
 impact     : inc_common (include of module OnLoad) -> Critical; area_enter -> quest q_rats
 safe delete: unused_helm.uti (palette only), orphan_tex.tga, old_unused+dead_lib (group),
              unused_model.mdl+wok+orphan2_tex (group), orphan_only.ncs,
              at_reward_copy (duplicate, unreferenced)
 review     : dyn_1 (dynamic ExecuteScript("dyn_"+...)), po_hero_h.tga (portrait naming),
              area003 + old_enter (area not in module list)
 kept       : sword.mdl? (not referenced by anything -> safe), c_rat.mdl/c_rat_tex via appearance.2da
"""

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "test_module")
    print(EXPECTED)
