"""
Synthetic NWN:EE persistent-world module for testing nwn-toolkit.
    python3 make_dave_module.py <out_folder> <hak_folder>
"""
import os
import sys

TOOLKIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, TOOLKIT)
import nwnlib as n  # noqa: E402
sys.path.insert(0, os.path.join(TOOLKIT, "tests"))
# the GFF helpers are shared with the other fixture builders (tests/_gff.py); re-exported for the code that imports
# them from here
from _gff import (S, L, R, X, LS, B, I, DW, F, LST, STR, WORD, st, root, loc, item_fields,  # noqa: E402,F401
                  inst_item, tga, w, stand_in_ncs)


def creature(resref, tag, first, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), FirstName=loc(first), LastName=loc(""),
              XPosition=(F, 1.0), YPosition=(F, 1.0), ZPosition=(F, 0.0))
    f.update(extra)
    return st(4, **f)


def placeable(resref, tag, name, x, y, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocName=loc(name), X=(F, x), Y=(F, y), Z=(F, 0.0))
    f.update(extra)
    return st(9, **f)


def door(resref, tag, name, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocName=loc(name), X=(F, 1.0), Y=(F, 1.0), Z=(F, 0.0))
    f.update(extra)
    return st(8, **f)


def waypoint(resref, tag, name, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocalizedName=loc(name),
              X=(F, 1.0), Y=(F, 1.0), Z=(F, 0.0))
    f.update(extra)
    return st(5, **f)


def trigger(resref, tag, name, script_enter, linked_to=None, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocalizedName=loc(name),
              ScriptOnEnter=(R, script_enter), Type=(I, 1))
    if linked_to:
        f["LinkedTo"] = (X, linked_to)
        f["LinkedToFlags"] = (B, 1)
        f["LinkedToModule"] = (X, "")
    f.update(extra)
    return st(1, **f)


def store(resref, tag, name, items, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocName=loc(name),
              StoreList=(LST, [st(0, ItemList=(LST, items))]))
    f.update(extra)
    return st(11, **f)


def encounter(resref, tag, name, creature_resrefs, **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), LocalizedName=loc(name),
              Active=(B, 1), Difficulty=(I, 0),
              CreatureList=(LST, [st(14, ResRef=(R, cr)) for cr in creature_resrefs]))
    f.update(extra)
    return st(7, **f)


def var(name, value_str=None, value_int=None):
    if value_int is not None:
        return st(0, Name=(X, name), Type=(DW, 1), Value=(X, str(value_int)))
    return st(0, Name=(X, name), Type=(DW, 3), Value=(X, value_str))


def build(out, hak_dir):
    os.makedirs(out, exist_ok=True)
    os.makedirs(hak_dir, exist_ok=True)
    g = lambda fn, r: w(out, fn, n.write_gff(r))  # noqa: E731

    hak_name = os.path.basename(os.path.normpath(hak_dir))
    # a token file so the hak folder isn't empty
    w(hak_dir, "pw_hak_marker.2da", "2DA V2.0\n\n   LABEL\n0  MARKER\n")

    # =====================================================================
    # module.ifo
    # =====================================================================
    variables = [var("MODULE_VAR_TAGBASED_SCRIPT_PREFIX", value_str="tb_")]
    for i in range(1, 31):
        if i % 3 == 0:
            variables.append(var(f"PW_CFG_{i}", value_int=i))
        else:
            variables.append(var(f"PW_CFG_{i}", value_str=f"val_{i}"))

    g("module.ifo", root(
        "IFO ",
        Mod_Name=loc("Dave's Realm"),
        Mod_Entry_Area=(R, "hub"),
        Mod_OnModLoad=(R, "mod_pw_load"),
        Mod_OnClientEntr=(R, "mod_client_enter"),
        Mod_OnPlayerDeath=(R, "mod_player_death"),
        Mod_OnPlayerRest=(R, "mod_player_rest"),
        Mod_OnPlayerChat=(R, "mod_player_chat"),
        Mod_OnActvtItem=(R, "x2_mod_def_act"),
        Mod_Area_list=(LST, [st(6, Area_Name=(R, a)) for a in
                              ("hub", "dungeon1", "dungeon2", "dungeon3", "arena")]),
        Mod_HakList=(LST, [st(8, Mod_Hak=(X, hak_name)), st(8, Mod_Hak=(X, "pw_hak2_missing"))]),
        VarTable=(LST, variables),
    ))

    # =====================================================================
    # journal: 4 quests
    # =====================================================================
    def quest(tag, name, entries):
        return st(0, Tag=(X, tag), Name=loc(name),
                   EntryList=(LST, [st(0, ID=(DW, i), Text=loc(t), End=(WORD, e)) for i, t, e in entries]))

    g("module.jrl", root("JRL ", Categories=(LST, [
        quest("q_rats", "Rat Problem", [(1, "Clear the hub of rats", 0), (2, "Rats cleared", 1)]),
        quest("q_portal", "Portal Stones", [(1, "Find a portal stone", 0), (2, "Deliver the stone", 1)]),
        quest("q_dungeon", "Deep Delve", [(1, "Reach dungeon 3", 0), (2, "Defeat the boss", 1)]),
        quest("q_arena", "Arena Champion", [(1, "Enter the arena", 0), (2, "Win the bout", 1)]),
    ])))

    # =====================================================================
    # item blueprints (15)
    # =====================================================================
    g("sword_a.uti", root("UTI ", **item_fields("sword_a", "SWORD_A", "Iron Sword", props=(6,))))
    g("sword_b.uti", root("UTI ", **item_fields("sword_b", "SWORD_A", "Iron Sword", props=(6,))))  # identical
    g("item_healkit.uti", root("UTI ", **item_fields("item_healkit", "HEALKIT", "Healing Kit", base=49, cost=25)))
    g("item_portalstone.uti", root("UTI ", **item_fields("item_portalstone", "PORTALSTONE", "Portal Stone", base=1, cost=100)))
    g("key_dungeon2.uti", root("UTI ", **item_fields("key_dungeon2", "KEY_DUNGEON2", "Rusty Dungeon Key", base=24, cost=1)))
    g("store_only_item.uti", root("UTI ", **item_fields("store_only_item", "STORE_ONLY", "Shop Trinket", base=1, cost=15)))
    g("chest_only_item.uti", root("UTI ", **item_fields("chest_only_item", "CHEST_ONLY", "Old Coin", base=1, cost=5)))
    g("reward_item_vc.uti", root("UTI ", **item_fields("reward_item_vc", "REWARD_ITEM", "Reward Blade", base=1, cost=200)))
    g("unused_item_a.uti", root("UTI ", **item_fields("unused_item_a", "UNUSED_A", "Forgotten Boots", base=17)))
    g("unused_item_b.uti", root("UTI ", **item_fields("unused_item_b", "UNUSED_B", "Forgotten Cloak", base=1)))
    g("npc_carried_item.uti", root("UTI ", **item_fields("npc_carried_item", "NPC_CARRIED", "Trader's Ledger", base=1)))
    g("arena_prize_item.uti", root("UTI ", **item_fields("arena_prize_item", "ARENA_PRIZE", "Champion's Belt", base=1, cost=500)))
    g("d1_loose_item.uti", root("UTI ", **item_fields("d1_loose_item", "D1_LOOSE", "Torn Map", base=1)))
    g("vendor_item_ex.uti", root("UTI ", **item_fields("vendor_item_ex", "VENDOR_EXTRA", "Spare Rope", base=1, cost=8)))
    g("door_key_unused.uti", root("UTI ", **item_fields("door_key_unused", "DOOR_KEY_UNUSED", "Unlabeled Key", base=24)))

    # =====================================================================
    # creature blueprints used in encounters / spawner ("spawn_" + TYPE)
    # =====================================================================
    g("spawn_rat.utc", root("UTC ", TemplateResRef=(R, "spawn_rat"), Tag=(X, "SPAWN_RAT"),
                             FirstName=loc("Rat"), ScriptDeath=(R, "rat_death")))
    g("spawn_wolf.utc", root("UTC ", TemplateResRef=(R, "spawn_wolf"), Tag=(X, "SPAWN_WOLF"),
                              FirstName=loc("Wolf"), ScriptDeath=(R, "wolf_death")))
    g("npc_questgiver.utc", root("UTC ", TemplateResRef=(R, "npc_questgiver"), Tag=(X, "NPC_QUESTGIVER"),
                                  FirstName=loc("Elder"), LastName=loc("Maren"), Conversation=(R, "quest_dlg"),
                                  ScriptHeartbeat=(R, "nw_c2_default1"),
                                  ItemList=(LST, [inst_item("npc_carried_item", "NPC_CARRIED", "Trader's Ledger")])))
    g("boss_dungeon3.utc", root("UTC ", TemplateResRef=(R, "boss_dungeon3"), Tag=(X, "BOSS_D3"),
                                 FirstName=loc("Deep"), LastName=loc("Warden")))

    # =====================================================================
    # areas: hub, dungeon1, dungeon2, dungeon3, arena, abandoned_test
    # =====================================================================
    def are(resref, name, tag, on_enter=None, tileset="tcn01"):
        f = dict(Name=loc(name), Tag=(X, tag), ResRef=(R, resref), Tileset=(R, tileset))
        if on_enter:
            f["OnEnter"] = (R, on_enter)
        return root("ARE ", **f)

    # ---- hub ----
    g("hub.are", are("hub", "Town Hub", "AREA_HUB", on_enter="area_hub_enter"))
    g("hub.git", root("GIT ", **{
        "Creature List": (LST, [
            creature("npc_questgiver", "NPC_QUESTGIVER", "Elder", LastName=loc("Maren"),
                      Conversation=(R, "quest_dlg"), ScriptHeartbeat=(R, "nw_c2_default1"),
                      ItemList=(LST, [inst_item("npc_carried_item", "NPC_CARRIED", "Trader's Ledger")])),
        ]),
        "Placeable List": (LST, [
            placeable("chest_generic", "CHEST_HUB", "Storage Chest", 3.0, 4.0,
                      ItemList=(LST, [inst_item("item_healkit", "HEALKIT", "Healing Kit"),
                                       inst_item("sword_a", "SWORD_A", "Iron Sword")])),
        ]),
        "Door List": (LST, [
            door("door_arena", "DOOR_ARENA", "Arena Gate", OnOpen=(R, "door_open_arena"),
                 LinkedTo=(X, "WP_ARENA_ENTRY"), LinkedToFlags=(B, 1), LinkedToModule=(X, "")),
        ]),
        "StoreList": (LST, [
            store("store_gen", "STORE_HUB", "General Store",
                  [inst_item("store_only_item", "STORE_ONLY", "Shop Trinket"),
                   inst_item("vendor_item_ex", "VENDOR_EXTRA", "Spare Rope")]),
        ]),
        "TriggerList": (LST, [
            trigger("trig_to_d1", "TRIG_TO_D1", "To Dungeon 1", "trig_enter_d1",
                    linked_to="WP_DUNGEON1_START"),
        ]),
        "WaypointList": (LST, [
            waypoint("wp_hub_spawn", "WP_HUB_SPAWN", "Hub Spawn Point"),
        ]),
        "Encounter List": (LST, [
            encounter("enc_hub", "ENC_HUB_RATS", "Hub Rat Nest", ["spawn_rat", "spawn_rat"]),
        ]),
    }))

    # ---- dungeon1 ----
    g("dungeon1.are", are("dungeon1", "Dungeon Level 1", "AREA_DUNGEON1", on_enter="area_d1_enter"))
    g("dungeon1.git", root("GIT ", **{
        "Creature List": (LST, [
            creature("nw_goblina", "GOBLIN_1", "Goblin"),  # base-game blueprint, no local .utc
            creature("spawn_wolf", "WOLF_1", "Wolf", ScriptDeath=(R, "wolf_death")),
        ]),
        "Placeable List": (LST, [
            placeable("chest_generic", "CHEST_D1", "Old Crate", 5.0, 6.0,
                      ItemList=(LST, [inst_item("d1_loose_item", "D1_LOOSE", "Torn Map")])),
        ]),
        "List": (LST, []),
        "TriggerList": (LST, [
            trigger("trig_to_d2", "TRIG_TO_D2", "To Dungeon 2", "trig_enter_d2",
                    linked_to="WP_DUNGEON2_START"),
        ]),
        "WaypointList": (LST, [
            waypoint("wp_d1_spawn", "WP_DUNGEON1_SPAWN", "Dungeon 1 Spawn"),
            waypoint("wp_d1_start", "WP_DUNGEON1_START", "Dungeon 1 Entrance"),
        ]),
        "Encounter List": (LST, [
            encounter("enc_d1_wolves", "ENC_D1_WOLVES", "Wolf Pack", ["spawn_wolf", "spawn_wolf", "spawn_rat"]),
        ]),
    }))

    # ---- dungeon2 ----
    g("dungeon2.are", are("dungeon2", "Dungeon Level 2", "AREA_DUNGEON2", on_enter="area_d2_enter"))
    g("dungeon2.git", root("GIT ", **{
        "Creature List": (LST, [
            creature("spawn_rat", "RAT_D2", "Rat", ScriptDeath=(R, "rat_death")),
        ]),
        "Placeable List": (LST, [
            placeable("chest_generic", "CHEST_D2", "Locked Chest", 2.0, 2.0,
                      ItemList=(LST, [inst_item("item_portalstone", "PORTALSTONE", "Portal Stone"),
                                       inst_item("chest_only_item", "CHEST_ONLY", "Old Coin")])),
        ]),
        "List": (LST, [
            inst_item("key_dungeon2", "KEY_DUNGEON2", "Rusty Dungeon Key", XPosition=(F, 4.0),
                       YPosition=(F, 4.0), ZPosition=(F, 0.0)),
        ]),
        "Door List": (LST, [
            door("door_dungeon3", "DOOR_D3", "Reinforced Door", KeyName=(X, "KEY_DUNGEON2"),
                 OnOpen=(R, "door_open_d3"), OnFailToOpen=(R, "door_fail_d3"),
                 LinkedTo=(X, "WP_DUNGEON3_START"), LinkedToFlags=(B, 1), LinkedToModule=(X, "")),
        ]),
        "TriggerList": (LST, [
            trigger("trig_to_d3", "TRIG_TO_D3", "To Dungeon 3", "trig_enter_d3",
                    linked_to="WP_DUNGEON3_START"),
        ]),
        "WaypointList": (LST, [
            waypoint("wp_d2_start", "WP_DUNGEON2_START", "Dungeon 2 Entrance"),
        ]),
    }))

    # ---- dungeon3 ----
    g("dungeon3.are", are("dungeon3", "Dungeon Level 3", "AREA_DUNGEON3", on_enter="area_d3_enter"))
    g("dungeon3.git", root("GIT ", **{
        "Creature List": (LST, [
            creature("boss_dungeon3", "BOSS_D3", "Deep", LastName=loc("Warden")),
        ]),
        "WaypointList": (LST, [
            waypoint("wp_d3_start", "WP_DUNGEON3_START", "Dungeon 3 Entrance"),
            waypoint("wp_d3_spawn", "WP_DUNGEON3_SPAWN", "Dungeon 3 Spawn"),
        ]),
        "Encounter List": (LST, [
            encounter("enc_d3_boss_adds", "ENC_D3_ADDS", "Boss Adds", ["spawn_rat", "spawn_wolf"]),
        ]),
    }))

    # ---- arena (reached ONLY via door LinkedTo from hub) ----
    g("arena.are", are("arena", "Arena", "AREA_ARENA", on_enter="area_arena_enter"))
    g("arena.git", root("GIT ", **{
        "Creature List": (LST, [
            creature("spawn_wolf", "ARENA_CHAMPION", "Arena Wolf", ScriptDeath=(R, "wolf_death")),
        ]),
        "Placeable List": (LST, [
            placeable("chest_generic", "CHEST_ARENA", "Prize Chest", 6.0, 6.0,
                      ItemList=(LST, [inst_item("arena_prize_item", "ARENA_PRIZE", "Champion's Belt")])),
        ]),
        "WaypointList": (LST, [
            waypoint("wp_arena_entry", "WP_ARENA_ENTRY", "Arena Entry"),
        ]),
    }))

    # ---- abandoned_test area: NOT in Mod_Area_list, nothing transitions to it ----
    g("abandoned_test.are", are("abandoned_test", "Abandoned Test Area", "AREA_OLD", on_enter="area_aband_entr"))
    g("abandoned_test.git", root("GIT ", **{
        "Creature List": (LST, [creature("spawn_rat", "OLD_RAT", "Test Rat", ScriptDeath=(R, "rat_death"))]),
    }))

    # =====================================================================
    # conversations (3, one unused): quest_dlg, merchant_ex_dlg (unused), reward via ActionParams
    # =====================================================================
    def entry(txt, script="", quest="", qe=0, replies=(), action_params=None):
        f = dict(Text=loc(txt), Speaker=(X, ""), Script=(R, script), Quest=(X, quest), QuestEntry=(DW, qe),
                  RepliesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in replies]))
        if action_params:
            f["ActionParams"] = (LST, [st(0, Key=(X, k), Value=(X, v)) for k, v in action_params])
        return st(0, **f)

    def reply(txt, entries=(), script=""):
        return st(0, Text=loc(txt), Script=(R, script), Quest=(X, ""),
                   EntriesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in entries]))

    g("quest_dlg.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [
            entry("Welcome to town, traveller.", replies=[(0, ""), (1, "cond_has_pstone")]),
            entry("You brought the portal stone! Take this reward.", script="dlg_act_reward",
                  quest="q_portal", qe=2, action_params=[("EFFECT", "reward"), ("AMOUNT", "1")]),
        ]),
        ReplyList=(LST, [
            reply("Just passing through."),
            reply("I have the portal stone.", entries=[(1, "")]),
        ]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    g("merchant_ex_dlg.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),  # unused conversation
        EntryList=(LST, [entry("Nothing to trade today.", script="unused_convo_sc")]),
        ReplyList=(LST, [reply("Fine.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    # =====================================================================
    # scripts (~40) - Lilac-Soul-Generator style header for several
    # =====================================================================
    LILAC = ('//::///////////////////////////////////////////\n'
             '//:: {name}\n'
             '//:: Copyright (c) Dave PW\n'
             '//:://////////////////////////////////////////////\n'
             '/*\n    {desc}\n*/\n'
             '//:: Created By: Lilac Soul\'s NWN Script Generator\n'
             '//:: Put this script OnUsed\n'
             '//:://////////////////////////////////////////////\n')

    scripts = {}

    scripts["pw_inc"] = (
        'void PW_Log(string s) { }\n'
        'int PW_IsStaff(object o) { return GetLocalInt(o, "IS_STAFF"); }\n'
        'void PW_Heartbeat(object o) { }\n'
        'string PW_GetPrefix() { return "tb_"; }\n'
        'void PW_Broadcast(string s) { }\n'
    )

    def inc_user(name, body):
        return f'#include "pw_inc"\nvoid main() {{\n  PW_Log("{name}");\n{body}\n}}\n'

    scripts["mod_pw_load"] = inc_user("mod_pw_load", '  SetLocalInt(GetModule(), "PW_LOADED", 1);')
    scripts["mod_client_enter"] = inc_user("mod_client_enter",
        '  object oPC = GetEnteringObject();\n  ExecuteScript("pw_persist", oPC);')
    scripts["mod_player_death"] = inc_user("mod_player_death", '  // handle death penalty')
    scripts["mod_player_rest"] = inc_user("mod_player_rest", '  ExecuteScript("pw_persist", OBJECT_SELF);')
    scripts["mod_player_chat"] = inc_user("mod_player_chat", '  // chat filter hook')
    scripts["spawner_engine"] = inc_user("spawner_engine",
        '  object oWP = GetWaypointByTag("WP_HUB_SPAWN");\n'
        '  string sType = GetLocalString(oWP, "TYPE");\n'
        '  object oCr = CreateObject(OBJECT_TYPE_CREATURE, "spawn_" + sType, GetLocation(oWP));')
    scripts["tb_healkit"] = inc_user("tb_healkit", '  // heal the using PC\n  object oPC = GetItemActivator();')
    scripts["tb_portalstone"] = inc_user("tb_portalstone", '  // teleport the using PC\n  object oPC = GetItemActivator();')
    scripts["pw_persist"] = inc_user("pw_persist",
        '  SetCampaignInt("dave_pw", "LOGIN_COUNT", 1, OBJECT_SELF);\n'
        '  sqlquery q = SqlPrepareQueryCampaign("dave_pw", "SELECT 1");')
    scripts["area_hub_enter"] = inc_user("area_hub_enter",
        '  object oPC = GetEnteringObject();\n  AddJournalQuestEntry("q_rats", 1, oPC);')

    scripts["area_d1_enter"] = 'void main() { object oPC = GetEnteringObject(); AddJournalQuestEntry("q_dungeon", 1, oPC); }\n'
    scripts["area_d2_enter"] = 'void main() { }\n'
    scripts["area_d3_enter"] = 'void main() { object oPC = GetEnteringObject(); AddJournalQuestEntry("q_dungeon", 2, oPC); }\n'
    scripts["area_arena_enter"] = 'void main() { object oPC = GetEnteringObject(); AddJournalQuestEntry("q_arena", 1, oPC); }\n'
    scripts["area_aband_entr"] = 'void main() { /* leftover from an old test build */ }\n'

    scripts["dup_reward_a"] = 'void main()\n{\n  CreateItemOnObject("reward_item_vc", GetPCSpeaker());\n  AddJournalQuestEntry("q_portal", 2, GetPCSpeaker());\n}\n'
    scripts["dup_reward_b"] = 'void main()\n{\n  CreateItemOnObject("reward_item_vc", GetPCSpeaker());\n  AddJournalQuestEntry("q_portal", 2, GetPCSpeaker());\n}\n'
    scripts["dlg_act_reward"] = 'void main()\n{\n  CreateItemOnObject("reward_item_vc", GetPCSpeaker());\n  AddJournalQuestEntry("q_portal", 2, GetPCSpeaker());\n}\n'
    scripts["door_custom"] = 'void main() { /* wired only via SetEventScript(EVENT_SCRIPT_DOOR_ON_OPEN) in dungeon setup, not a GFF field */ }\n'
    scripts["cond_has_pstone"] = 'int StartingConditional() { return GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "PORTALSTONE")); }\n'
    scripts["unused_convo_sc"] = 'void main() { }\n'
    scripts["rat_death"] = 'void main() { }\n'
    scripts["wolf_death"] = 'void main() { }\n'
    scripts["door_open_arena"] = 'void main() { }\n'
    scripts["door_open_d3"] = (
    'void main() {\n'
    '  SetEventScript(OBJECT_SELF, EVENT_SCRIPT_DOOR_ON_OPEN, "door_custom");\n'
    '}\n')
    scripts["door_fail_d3"] = 'int StartingConditional() { return TRUE; }\n'  # wrong type: used as OnFailToOpen but is a conditional
    scripts["trig_enter_d1"] = 'void main() { object oPC = GetEnteringObject(); }\n'
    scripts["trig_enter_d2"] = 'void main() { object oPC = GetEnteringObject(); }\n'
    scripts["trig_enter_d3"] = 'void main() { object oPC = GetEnteringObject(); }\n'
    scripts["quest_update_a"] = 'void main() { AddJournalQuestEntry("q_rats", 2, OBJECT_SELF); }\n'
    scripts["quest_update_b"] = 'void main() { AddJournalQuestEntry("q_arena", 2, OBJECT_SELF); }\n'
    scripts["pw_seventeen_char"] = 'void main() { /* 17-char script name, over the 16-char ResRef limit */ }\n'
    scripts["old_unused"] = '#include "old_lib"\nvoid main() { OldFunc(); }\n'
    scripts["old_lib"] = 'void OldFunc() { }\n'
    scripts["dyn_event"] = 'void main() { }\n'
    scripts["heartbeat_hub"] = 'void main() { /* not wired to anything currently */ }\n'

    for name, src in scripts.items():
        if name in ("mod_pw_load", "spawner_engine", "tb_healkit", "tb_portalstone", "pw_persist"):
            src = LILAC.format(name=name, desc=f"Handles {name.replace('_', ' ')}.") + src
        w(out, f"{name}.nss", src)

    # scripts referenced only via literal/event-hook, no direct GFF field:
    # (door_custom already has .nss; area_hub_enter uses ExecuteScript("dyn_" + ...) pattern below)
    with open(os.path.join(out, "area_hub_enter.nss"), "a", encoding="cp1252") as fh:
        fh.write('void SecondaryHook() { ExecuteScript("dyn_" + "event", OBJECT_SELF); }\n')

    # compiled a script only as .ncs (no source at all)
    w(out, "compiled_evt.ncs", stand_in_ncs("compiled_evt"))

    # two identical scripts are already dup_reward_a/dup_reward_b (source dup); give both .ncs too
    for name in scripts:
        w(out, f"{name}.ncs", stand_in_ncs(name))
    # deliberately NOT compiled (no .ncs): rat_death, wolf_death, dyn_event
    for missing in ("rat_death.ncs", "wolf_death.ncs", "dyn_event.ncs"):
        p = os.path.join(out, missing)
        if os.path.exists(p):
            os.remove(p)

    # CRLF-line-ending script
    w(out, "pw_crlf_test.nss", "void main()\r\n{\r\n  // uses CRLF line endings\r\n}\r\n")
    w(out, "pw_crlf_test.ncs", stand_in_ncs("crlf"))

    # UTF-8 BOM script
    w(out, "pw_bom_test.nss", "﻿void main() { /* has a UTF-8 BOM */ }\n", encoding="utf-8")
    w(out, "pw_bom_test.ncs", stand_in_ncs("bom_"))

    # =====================================================================
    # textures / models
    # =====================================================================
    w(out, "c_ratm.mdl", b"\0\0\0\0binarymodel c_ratm_tex \0\0")
    w(out, "c_ratm_tex.tga", tga(64, 64, lambda x, y: (100 + x, 80 + y // 2, 50)))
    w(out, "po_dave_h.tga", tga(64, 64, lambda x, y: (200, 180, 160)))
    w(out, "unused_scen_tex.tga", b"\0" * 64)

    print(f"Built module in {out}\nHak folder: {hak_dir}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "dave_module"
    hak = sys.argv[2] if len(sys.argv) > 2 else "pw_hak1"
    build(out, hak)
