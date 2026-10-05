"""
Marta's messy inherited NWN:EE module — synthetic, built for a full pass through
the toolkit. Uses the GFF helpers in tests/_gff.py.

Usage: python3 make_marta_module.py <out_folder> <hak_folder>
"""
import os
import sys

TOOLKIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
sys.path.insert(0, TOOLKIT)
sys.path.insert(0, os.path.join(TOOLKIT, "tests"))
import nwnlib as n  # noqa: E402
from _gff import st, root, w, tga, R, X, LS, B, I, DW, F, LST, loc, item_fields, stand_in_ncs, S, L  # noqa: E402

PL = "Zgroda żółta"          # Polish-ish
CY = "Привет, воин!"  # Cyrillic-ish


def build(out, hak):
    os.makedirs(out, exist_ok=True)
    os.makedirs(hak, exist_ok=True)
    g = lambda fn, r: w(out, fn, n.write_gff(r))          # noqa: E731
    gh = lambda fn, r: w(hak, fn, n.write_gff(r))          # noqa: E731

    # ================= AREAS (8) + transitions =================
    area_ids = [f"area{i:02d}" for i in range(1, 9)]
    are_fields = {}
    for idx, aid in enumerate(area_ids, start=1):
        are_fields[aid] = dict(Name=loc(f"{PL} {idx}"), Tag=(X, f"AREA_{idx}"), ResRef=(R, aid),
                                OnEnter=(R, f"ae_enter{idx}" if idx <= 7 else ""), Tileset=(R, "tcn01"))
    for aid, f in are_fields.items():
        g(f"{aid}.are", root("ARE ", **f))

    # chain area01 -> area02 -> ... -> area07 via doors; area07 -> area08 only via
    # trigger script that JumpToLocation(GetWaypointByTag("WP_SECRET")) in area08.
    def door(tag, linked_tag, key=None, on_open=""):
        f = dict(TemplateResRef=(R, f"door_{tag.lower()}"), Tag=(X, tag), LocName=loc("Door"),
                  LinkedTo=(X, linked_tag), OnOpen=(R, on_open))
        if key:
            f["KeyName"] = (X, key)
        return st(8, **f)

    def trig(tag, linked_tag, script=""):
        return st(1, TemplateResRef=(R, f"trig_{tag.lower()}"), Tag=(X, tag), LocalizedName=loc(""),
                   LinkedTo=(X, linked_tag), OnTriggered=(R, script))

    gits = {}
    for i in range(1, 8):
        aid = f"area{i:02d}"
        nxt_tag = f"WP_ENTER_{i + 1}"
        creatures, doors, triggers, waypoints, placeables = [], [], [], [], []
        if i < 7:
            doors.append(door(f"DOOR_{i}", nxt_tag, on_open="AT_GiveKey" if i == 3 else ""))
        if i == 7:
            triggers.append(trig("TRIG_SECRET", "", script="trg_secret7"))
        waypoints.append(st(5, TemplateResRef=(R, "nw_waypoint001"), Tag=(X, f"WP_ENTER_{i}"),
                             LocalizedName=loc(f"Entry {i}")))
        gits[aid] = dict(**{"Door List": (LST, doors)}, **{"TriggerList": (LST, triggers)},
                          **{"WaypointList": (LST, waypoints)}, **{"Creature List": (LST, creatures)},
                          **{"Placeable List": (LST, placeables)})
    # area08: secret, only reachable by the JumpToLocation script; has WP_SECRET
    gits["area08"] = dict(**{"WaypointList": (LST, [st(5, TemplateResRef=(R, "nw_waypoint001"), Tag=(X, "WP_SECRET"),
                                                          LocalizedName=loc("Secret"))])},
                           **{"Creature List": (LST, [])}, **{"Placeable List": (LST, [])},
                           **{"Door List": (LST, [])}, **{"TriggerList": (LST, [])})
    for aid, f in gits.items():
        g(f"{aid}.git", root("GIT ", **f))

    # ================= ITEMS (40) =================
    item_names = []
    items = {}

    def add_item(resref, tag, name, **kw):
        items[resref] = item_fields(resref, tag, name, **kw)
        item_names.append(resref)

    # 6 identical in 2 groups of 3
    for grp, base_tag, base_name in (("a", "GRP_A", "Iron Sword"), ("b", "GRP_B", "Oak Shield")):
        for j in range(3):
            add_item(f"dup_{grp}_{j}", base_tag, base_name, props=(6,), cost=15)
    # 5 same stats different name
    for j in range(5):
        add_item(f"namedup_{j}", f"NAMEDUP_{j}", f"Blade of {['Dawn', 'Dusk', 'Ash', 'Frost', 'Ember'][j]}",
                 props=(6,), cost=25)
    # 4 sharing a tag
    for j in range(4):
        add_item(f"sharedtag_{j}", "SHARED_KEY", f"Old Key {j}", base=24, cost=1)
    # plot items linked to quests
    add_item("sword_of_kings", "SWORD_OF_KINGS", "Sword of Kings", cost=500)
    items["sword_of_kings"]["TemplateResRef"] = (R, "Sword_Of_Kings")  # mixed case vs file sword_of_kings.uti
    items["sword_of_kings"]["Plot"] = (B, 1)
    add_item("amulet_ghost", "AMULET_GHOST", "Ghost Amulet", cost=300)
    items["amulet_ghost"]["Plot"] = (B, 1)
    add_item("crown_relic", "CROWN_RELIC", "Relic Crown", cost=800)
    items["crown_relic"]["Plot"] = (B, 1)
    # comment field with 0x81/0x8D bytes handled separately below (bad_comment_item)
    add_item("bad_comment_item", "BAD_COMMENT", "Cursed Trinket")
    # remaining filler unused items to reach 40
    used_so_far = len(items)
    for j in range(40 - used_so_far - 1):
        add_item(f"filler_{j}", f"FILLER_{j}", f"Filler Object {j}", cost=5)
    add_item("key_rusty", "KEY_RUSTY", "Rusty Key", base=24, cost=1)

    for resref, fields in items.items():
        r = root("UTI ", **fields)
        if resref == "bad_comment_item":
            # inject raw high bytes into the Comment CEXOSTRING field's underlying value
            r.set("Comment", n.CEXOSTRING, "Cracked \x81\x8d edges")
        g(f"{resref}.uti", r)
    # write the actual item files with lower-case filenames; sword_of_kings TemplateResRef is mixed-case
    # (already handled: file name IS sword_of_kings.uti, field value is Sword_Of_Kings)

    # ================= NPCs / creatures / placeables / store / encounter =================
    # creature with ItemList InventoryRes AND full-copy items in .git instance ItemList
    npc = root("UTC ", TemplateResRef=(R, "npc_keeper"), Tag=(X, "NPC_KEEPER"),
               FirstName=loc("Keeper"), LastName=loc(PL), Conversation=(R, "big_dlg"),
               ScriptHeartbeat=(R, "nw_c2_default1"),
               ItemList=(LST, [st(0, InventoryRes=(R, "sword_of_kings")), st(0, InventoryRes=(R, "key_rusty"))]))
    g("npc_keeper.utc", npc)

    ghost_npc = root("UTC ", TemplateResRef=(R, "npc_ghost"), Tag=(X, "NPC_GHOST"),
                     FirstName=loc("Ghost"), ScriptHeartbeat=(R, "at_ghost_hb"))
    g("npc_ghost.utc", ghost_npc)

    placeable = root("UTP ", TemplateResRef=(R, "chest_locked"), Tag=(X, "CHEST_LOCKED"), LocName=loc("Locked Chest"),
                     KeyName=(X, "SHARED_KEY"), OnOpen=(R, "pl_chest_open"))
    g("chest_locked.utp", placeable)

    store = root("UTM ", TemplateResRef=(R, "store_gen"), Tag=(X, "STORE_GEN"), LocName=loc("General Store"))
    g("store_gen.utm", store)

    encounter = root("UTE ", TemplateResRef=(R, "enc_bandits"), Tag=(X, "ENC_BANDITS"), LocalizedName=loc("Bandits"),
                     OnEntered=(R, "enc_entered"), Active=(B, 1))
    g("enc_bandits.ute", encounter)

    # drop npc_keeper, ghost, placeable, store, encounter into area01.git along with dup items in inventory
    area01 = gits["area01"]
    dup_item_copies = [st(0, **item_fields("dup_a_0", "GRP_A", "Iron Sword")),
                        st(0, **item_fields("dup_a_1", "GRP_A", "Iron Sword"))]
    g("area01.git", root("GIT ", **{
        "Door List": area01["Door List"],
        "TriggerList": area01["TriggerList"],
        "WaypointList": area01["WaypointList"],
        "Creature List": (LST, [st(4, TemplateResRef=(R, "npc_keeper"), Tag=(X, "NPC_KEEPER"),
                                    FirstName=loc("Keeper"), LastName=loc(PL), XPosition=(F, 1.0), YPosition=(F, 1.0),
                                    ZPosition=(F, 0.0), Conversation=(R, "big_dlg"),
                                    ScriptHeartbeat=(R, "nw_c2_default1"),
                                    ItemList=(LST, dup_item_copies)),
                                 st(4, TemplateResRef=(R, "npc_ghost"), Tag=(X, "NPC_GHOST"), FirstName=loc("Ghost"),
                                    XPosition=(F, 2.0), YPosition=(F, 2.0), ZPosition=(F, 0.0),
                                    ScriptHeartbeat=(R, "at_ghost_hb"))]),
        "Placeable List": (LST, [st(9, TemplateResRef=(R, "chest_locked"), Tag=(X, "CHEST_LOCKED"),
                                     LocName=loc("Locked Chest"), KeyName=(X, "SHARED_KEY"),
                                     OnOpen=(R, "pl_chest_open"), X=(F, 3.0), Y=(F, 3.0), Z=(F, 0.0),
                                     ItemList=(LST, [st(0, **item_fields("crown_relic", "CROWN_RELIC", "Relic Crown"))]))]),
        "StoreList": (LST, [st(11, TemplateResRef=(R, "store_gen"), Tag=(X, "STORE_GEN"), LocName=loc("General Store"),
                                StoreList=(LST, [st(0, ItemList=(LST, [st(0, **item_fields("namedup_0", "NAMEDUP_0", "Blade of Dawn"))]))]))]),
        "Encounter List": (LST, [st(11 if False else 12, TemplateResRef=(R, "enc_bandits"), Tag=(X, "ENC_BANDITS"),
                                     LocalizedName=loc("Bandits"), OnEntered=(R, "enc_entered"), Active=(B, 1),
                                     XPosition=(F, 5.0), YPosition=(F, 5.0), ZPosition=(F, 0.0))]),
    }))

    # ================= JOURNAL (6 quests) =================
    quest_entries = []
    for qi, (tag, name) in enumerate([
        ("q_keeper", "The Keeper's Bargain"), ("q_relic", "The Relic Crown"),
        ("q_ghost", "Voice from Beyond"), ("q_bandits", "Bandit Trouble"),
        ("q_lost", "The Lost Cellar"), ("q_never_updated", "Rumours of Gold")]):
        quest_entries.append(st(0, Tag=(X, tag), Name=loc(name),
                                 EntryList=(LST, [st(0, ID=(DW, 1), Text=loc("Started"), End=(n.WORD, 0)),
                                                  st(0, ID=(DW, 10), Text=loc("Done"), End=(n.WORD, 1))])))
    # note: q_missing_from_journal (used by big_dlg) is deliberately NOT in this list
    g("module.jrl", root("JRL ", Categories=(LST, quest_entries)))

    # ================= CONVERSATIONS =================
    def entry(txt, script="", quest="", qe=0, replies=(), comment=""):
        return st(0, Text=loc(txt), Speaker=(X, ""), Script=(R, script), Quest=(X, quest), QuestEntry=(DW, qe),
                   Comment=(X, comment),
                   RepliesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, c)) for i, a, c in replies]))

    def reply(txt, entries=(), script="", quest="", qe=0):
        return st(0, Text=loc(txt), Script=(R, script), Quest=(X, quest), QuestEntry=(DW, qe),
                   EntriesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, c)) for i, a, c in entries]))

    # 4 "other" conversations
    g("dlg_ghost.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry(CY, script="sc_ghost_cond")]),
        ReplyList=(LST, [reply("...")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))
    # dlg_ghost is only ever referenced from a script via ActionStartConversation(oPC, "dlg_ghost") — no
    # Conversation field anywhere points to it.

    g("dlg_shop.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("Browse my wares.")]),
        ReplyList=(LST, [reply("Leave.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    g("dlg_bandit.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("Hand over your gold!", quest="q_bandits", qe=1)]),
        ReplyList=(LST, [reply("Never.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    dlg_lost_root = root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("The cellar is lost to time.", quest="q_lost", qe=1)]),
        ReplyList=(LST, [reply("Tell me more.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))]))
    g("dlg_lost.dlg", dlg_lost_root)
    g("dlg_lost_copy.dlg", dlg_lost_root)  # duplicate conversation under another name

    # big conversation: 60 nodes total (entries + replies), IsChild links, StartingList 3 Active
    # conditions, quest updates, one broken Index beyond list length, one node with Script that
    # doesn't exist.
    entries_list = []
    replies_list = []
    N_PAIRS = 30  # 30 entries + 30 replies = 60 nodes
    for i in range(N_PAIRS):
        qtag = "q_keeper" if i % 5 == 0 else ("q_missing_from_journal" if i == 7 else "")
        qe = (i // 5) + 1 if qtag else 0
        script = ""
        if i == 12:
            script = "sc_doesnt_exist"  # node whose Script doesn't exist
        replies_for_entry = [(i, "", 0)]
        if i > 0:
            replies_for_entry.append((i - 1, "sc_is_child_cond", 1))  # IsChild link back
        entries_list.append(entry(f"{PL} node {i}", script=script, quest=qtag, qe=qe,
                                   replies=replies_for_entry))
    for i in range(N_PAIRS):
        entries_for_reply = [(i, "", 0)]
        if i == N_PAIRS - 1:
            entries_for_reply.append((N_PAIRS + 5, "", 0))  # broken Index beyond EntryList length
        replies_list.append(reply(f"Reply {i}: {CY}", entries=entries_for_reply))

    big_dlg = root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
                   EntryList=(LST, entries_list), ReplyList=(LST, replies_list),
                   StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, "sc_start_cond_a")),
                                        st(0, Index=(DW, 5), Active=(R, "sc_start_cond_b")),
                                        st(0, Index=(DW, 10), Active=(R, "sc_start_cond_c"))]))
    g("big_dlg.dlg", big_dlg)

    # ================= SCRIPTS (60) =================
    scripts = {}

    def add(name, src):
        scripts[name] = src

    # -- nested includes a>b>c and a diamond (d includes b and c, both include e) --
    add("inc_a", '#include "inc_b"\nvoid main() { FuncB(); }\n')
    add("inc_b", '#include "inc_c"\nvoid FuncB() { FuncC(); }\n')
    add("inc_c", 'void FuncC() { }\n')
    add("inc_d", '#include "inc_diamond_left"\n#include "inc_diamond_right"\nvoid main() { LeftFn(); RightFn(); }\n')
    add("inc_diamond_left", '#include "inc_diamond_base"\nvoid LeftFn() { BaseFn(); }\n')
    add("inc_diamond_right", '#include "inc_diamond_base"\nvoid RightFn() { BaseFn(); }\n')
    add("inc_diamond_base", 'void BaseFn() { }\n')
    # -- include cycle: inc_cycle_x <-> inc_cycle_y --
    add("inc_cycle_x", '#include "inc_cycle_y"\nvoid FuncX() { FuncY(); }\n')
    add("inc_cycle_y", '#include "inc_cycle_x"\nvoid FuncY() { }\n')
    add("uses_cycle", '#include "inc_cycle_x"\nvoid main() { FuncX(); }\n')
    # -- unused library (nobody includes it) --
    add("inc_unused_lib", 'void UnusedHelper() { }\n')
    # -- 5 orphan action scripts (compiled .ncs stand-ins, no includer, have main()) --
    for i in range(5):
        add(f"orphan_action_{i}", f'void main() {{ /* orphan {i} */ }}\n')
    # -- several identical scripts --
    identical_src = 'void main() { object oPC = GetPCSpeaker(); GiveGoldToCreature(oPC, 10); }\n'
    add("give_gold_a", identical_src)
    add("give_gold_b", identical_src)
    add("give_gold_c", identical_src)
    # -- one calls a function from an include it doesn't include (b calls FuncC without #include "inc_c") --
    add("bad_include_caller", 'void main() { FuncC(); }\n')  # FuncC defined in inc_c, not included here
    # -- comments containing fake code and fake #include; string literal "// not a comment" --
    add("tricky_comments", (
        '//:: tricky_comments\n'
        '//:: Put this script OnEnter\n'
        '/* #include "inc_fake_never_real"\n'
        '   void FakeFunc() { DestroyObject(OBJECT_SELF); } */\n'
        'void main() {\n'
        '  string s = "// not a comment";\n'
        '  SendMessageToPC(GetFirstPC(), s);\n'
        '}\n'))
    # -- ExecuteScript with a variable only, and with GetLocalString --
    add("dyn_caller", (
        'void main() {\n'
        '  string sName = "dyn_target";\n'
        '  ExecuteScript(sName, OBJECT_SELF);\n'
        '  ExecuteScript(GetLocalString(OBJECT_SELF, "NEXT"), OBJECT_SELF);\n'
        '}\n'))
    add("dyn_target", 'void main() { }\n')

    # -- Lilac Soul style headers for the event scripts, and area transition scripts --
    def lilac(hint, body):
        return f'//:: {hint}\n//:: Put this script OnEnter\n// Created by Marta\nvoid main() {{\n{body}\n}}\n'

    for i in range(1, 8):
        add(f"ae_enter{i}", lilac(f"ae_enter{i}", f'  AddJournalQuestEntry("q_keeper", 1, GetEnteringObject());'))
    add("trg_secret7", lilac("trg_secret7", (
        '  object oWP = GetWaypointByTag("WP_SECRET");\n'
        '  AssignCommand(GetEnteringObject(), JumpToLocation(GetLocationFromLocation ? GetLocation(oWP) : GetLocation(oWP)));')))
    add("pl_chest_open", 'void main() { object o = GetItemPossessedBy(GetLastOpenedBy(), "SHARED_KEY"); }\n')
    add("enc_entered", 'void main() { AddJournalQuestEntry("q_bandits", 1, GetEnteringObject()); }\n')
    add("at_ghost_hb", 'void main() { }\n')
    add("sc_ghost_cond", 'int StartingConditional() { object oPC = GetPCSpeaker();\n'
                         ' return GetIsObjectValid(GetItemPossessedBy(oPC, "AMULET_GHOST")); }\n')
    add("sc_start_cond_a", 'int StartingConditional() { return TRUE; }\n')
    add("sc_start_cond_b", 'int StartingConditional() { return GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "CROWN_RELIC")); }\n')
    add("sc_start_cond_c", 'int StartingConditional() { return GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "SWORD_OF_KINGS")); }\n')
    add("sc_is_child_cond", 'int StartingConditional() { return TRUE; }\n')
    add("summon_ghost_dlg", 'void main() { object oPC = GetFirstPC();\n'
                            ' AssignCommand(oPC, ActionStartConversation(oPC, "dlg_ghost")); }\n')
    # AT_GiveKey / at_givekey.nss — mixed-case script field usage
    add("at_givekey", 'void main() { CreateItemOnObject("key_rusty", GetPCSpeaker()); }\n')

    # base-game script name shadowed by a module copy
    add("nw_c2_default9", 'void main() { /* Marta module override of a base-game default script */ }\n')

    # fill up to ~60 scripts total with plain filler event scripts referencing nothing new
    current = len(scripts)
    for i in range(60 - current):
        add(f"filler_script_{i}", f'void main() {{ /* filler {i} */ }}\n')

    for name, src in scripts.items():
        w(out, f"{name}.nss", src)
        w(out, f"{name}.ncs", stand_in_ncs(name))
    # compiled-only orphan with no source
    w(out, "compiled_only.ncs", stand_in_ncs("compiled_only"))

    # ================= module.ifo =================
    g("module.ifo", root("IFO ", Mod_Name=loc("Marta's Inheritance"),
                         Mod_OnModLoad=(R, "mod_load_marta"), Mod_OnActvtItem=(R, "x2_mod_def_act"),
                         Mod_Entry_Area=(R, "area01"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, a)) for a in area_ids]),
                         Mod_HakList=(LST, [st(0, Mod_Hak=(X, "marta_hak"))])))
    mod_load_src = '#include "inc_a"\n#include "inc_d"\nvoid main() { FuncB(); LeftFn(); }\n'
    w(out, "mod_load_marta.nss", mod_load_src)
    w(out, "mod_load_marta.ncs", stand_in_ncs("mod_load_marta"))

    # ================= HAK conflict + item name/script case mismatch check =================
    # Put a conflicting copy of at_givekey.ncs (different content) in the hak folder.
    w(hak, "at_givekey.ncs", stand_in_ncs("at_givekey_HAK_VERSION"))
    # And a conflicting copy of one filler item.
    gh("filler_0.uti", root("UTI ", **item_fields("filler_0", "FILLER_0_HAK", "Filler Object 0 (hak copy)")))

    # XSS probe: script "not a comment"-adjacent, and an item name / dlg line containing a script tag.
    xss_item = root("UTI ", **item_fields("xss_item", "XSS_ITEM", "<script>alert(1)</script>"))
    g("xss_item.uti", xss_item)
    g("dlg_xss.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("<script>alert(1)</script>")]),
        ReplyList=(LST, [reply("ok")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))

    print(f"Built module: {out} ({len(items) + 1} items incl xss, {len(scripts) + 2} scripts, "
          f"{len(area_ids)} areas)")


GROUND_TRUTH = """
See ground_truth.md written alongside this script.
"""

if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "marta_module"
    hak_dir = sys.argv[2] if len(sys.argv) > 2 else "marta_hak"
    build(out_dir, hak_dir)
