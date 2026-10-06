"""
The tools around an analysis: variable / tag / token audit, quest sets, icons, look-alike duplicates, the read-only
facts API, accepted issues, modules-folder housekeeping, analysis archiving, module diff and snapshots, persistent-world
performance checks, Find & replace and the edits overlay, external tools, the official compiler on each platform,
dashboard platform behaviour, the command-line tools, and smaller modules (factions, server settings, log reader,
quick scan, run lock, fix texts) checked on fixtures of their own.
    python tests/test_tools.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from unittest import mock

import nwnlib as n  # noqa: E402
import nwn_index  # noqa: E402
import nwn_analysis  # noqa: E402
from _gff import st, root, loc, R, X, LST, DW, B, I, w, item_fields, tga, stand_in_ncs  # noqa: E402
from _harness import check, skip  # noqa: E402

ROOT = h.ROOT


INC_A = '''const int QA_STR_LEN = 20;
const int QA_BEARS = 3; // bear hunt
const int QA_WOLVES = 4;
const int QA_SPARE = 5;
const int QA_OWLS = 6;
void SetQuestToken(object oPC, int iPos = 0, string s = "1") { SetLocalString(oPC, "sA", s); }
string GetQuestToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "sA"), iPos, 1); }
'''
INC_B = '''const int QB_STR_LEN = 10;
const int QB_TUTORIAL = 3; // tutorial - same slot number, different string
void SetQuestToken(object oPC, int iPos = 0, string s = "1") { SetLocalString(oPC, "sB", s); }
string GetQuestToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "sB"), iPos, 1); }
'''
SCRIPTS = {
    "inc_a": INC_A, "inc_b": INC_B,
    "a_bears": '#include "inc_a"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QA_BEARS, "1");\n    SetQuestToken(GetPCSpeaker(), QA_OWLS, "1");\n}\n',
    "a_bears_sc": '#include "inc_a"\nint StartingConditional()\n{\n    return GetQuestToken(GetPCSpeaker(), QA_BEARS) == "1" && GetQuestToken(GetPCSpeaker(), QA_WOLVES) == "1";\n}\n',
    "b_tut": '#include "inc_b"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QB_TUTORIAL, "1");\n}\n',
    "b_tut_sc": '#include "inc_b"\nint StartingConditional()\n{\n    return GetQuestToken(GetPCSpeaker(), QB_TUTORIAL) == "1";\n}\n',
    # variables
    "v_set": 'void main()\n{\n    object oPC = GetPCSpeaker();\n    SetLocalInt(oPC, "nDoneKobolds", 1);\n    SetLocalInt(oPC, "nAge", 30);\n    SetLocalInt(oPC, "nWritten", 1);\n}\n',
    "v_read": 'int StartingConditional()\n{\n    object oPC = GetPCSpeaker();\n    return GetLocalInt(oPC, "nDonekobolds") == 1 && GetLocalString(oPC, "nAge") == "30"\n        && GetLocalInt(oPC, "nNeverSet") == 1 && GetLocalInt(OBJECT_SELF, "nFromToolset") == 2\n        && GetLocalInt(oPC, "X2_ENGINE_THING") == 1;\n}\n',
    # conversations started by scripts
    "c_start": 'void main()\n{\n    AssignCommand(GetObjectByTag("MARSHAL"), ActionStartConversation(GetLastUsedBy(), "marshal"));\n}\n',
    "c_dyn": 'void main()\n{\n    object oPC = GetLastUsedBy();\n    ActionStartConversation(oPC, GetLocalString(OBJECT_SELF, "sConv"), TRUE);\n}\n',
    # tags
    "t_look": 'void main()\n{\n    object a = GetObjectByTag("MARSHAL");\n    object b = GetObjectByTag("marshal");\n    object c = GetObjectByTag("NOSUCH_TAG");\n    object d = GetItemPossessedBy(GetPCSpeaker(), "IT_UNUSED");\n    object e = GetObjectByTag("RT_TAG");\n    object f = GetItemPossessedBy(GetPCSpeaker(), "IT_SOLD");\n}\n',
    "t_make": 'void main()\n{\n    CreateObject(OBJECT_TYPE_PLACEABLE, "plc_chest1", GetLocation(OBJECT_SELF), FALSE, "RT_TAG");\n}\n',
    # performance
    "hb_busy": 'void main()\n{\n    object o = GetFirstObjectInArea(GetArea(OBJECT_SELF));\n    while (GetIsObjectValid(o))\n    {\n        DelayCommand(1.0, AssignCommand(o, ActionRandomWalk()));\n        o = GetNextObjectInArea(GetArea(OBJECT_SELF));\n    }\n}\n',
    "hb_self": 'void Tick()\n{\n    SpeakString("tick");\n    DelayCommand(6.0, Tick());\n}\nvoid main()\n{\n    Tick();\n}\n',
    "hb_quiet": 'void main()\n{\n}\n',
}


def build(mod):
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    for nm, src in SCRIPTS.items():
        w(mod, nm + ".nss", src)
        if "main" in src or "StartingConditional" in src:
            w(mod, nm + ".ncs", stand_in_ncs(nm))
    g("module.ifo", root("IFO ", Mod_Name=loc("Tools Test"), Mod_Entry_Area=(R, "town"),
                         Mod_OnClientEntr=(R, "t_make"), Mod_OnHeartbeat=(R, "hb_self"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, a)) for a in ("town", "busy")])))

    def entry(txt, script="", replies=()):
        return st(0, Text=loc(txt), Speaker=(X, ""), Script=(R, script), Quest=(X, ""), QuestEntry=(DW, 0),
                  RepliesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in replies]))

    def reply(txt, script="", entries=()):
        return st(0, Text=loc(txt), Script=(R, script), Quest=(X, ""),
                  EntriesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in entries]))
    g("marshal.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("Hi", replies=[(0, "a_bears_sc"), (1, "b_tut_sc"), (2, "v_read")]),
                         entry("Bears", script="a_bears"), entry("Tut", script="b_tut"), entry("Vars", script="v_set"),
                         entry("Tags", script="t_look")]),
        ReplyList=(LST, [reply("a", entries=[(1, ""), (4, "")]), reply("b", entries=[(2, "")]), reply("c", entries=[(3, "")])]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))
    vt = (LST, [st(0, Name=(X, "nFromToolset"), Type=(DW, 1), Value=(I, 2))])
    g("marshal.utc", root("UTC ", TemplateResRef=(R, "marshal"), Tag=(X, "MARSHAL"), FirstName=loc("Marshal"),
                          Conversation=(R, "marshal"), VarTable=vt))
    g("goblin.utc", root("UTC ", TemplateResRef=(R, "goblin"), Tag=(X, "GOBLIN"), FirstName=loc("Goblin"),
                         ScriptHeartbeat=(R, "hb_busy")))
    g("unused.uti", root("UTI ", **item_fields("unused", "IT_UNUSED", "Never placed")))
    g("sold.uti", root("UTI ", **item_fields("sold", "IT_SOLD", "Sold in a store")))
    g("junk.uti", root("UTI ", **item_fields("junk", "IT_JUNK", "Junk")))
    # icons: base item 1 = a 3-part weapon, 20 = a simple item (potion), 16 = armour (default icon only)
    # (the game numbers 2da rows by line position, so rows 2-15 and 17-19 are written out as empty lines of ****)
    used_rows = {0: "Short it_short 2 ishort", 1: "LongSword WSwLs 2 iwswls", 16: "Armor it_armor 3 iit_armor_00",
                 20: "Potion it_potion 0 iit_potion_00"}
    w(mod, "baseitems.2da", "2DA V2.0\n\n   Label ItemClass ModelType DefaultIcon\n" +
      "".join(f"{i} {used_rows.get(i, '**** **** **** ****')}\n" for i in range(21)))
    red = lambda x, y: (200, 30, 30)  # noqa: E731
    for stem in ("iwswls_b_011", "iwswls_m_011", "iwswls_t_011", "iit_potion_005", "iit_armor_00"):
        w(mod, stem + ".tga", tga(4, 8, red))
    g("potion.uti", root("UTI ", **dict(item_fields("potion", "IT_POTION", "Potion", base=20), ModelPart1=(B, 5))))
    g("nopic.uti", root("UTI ", **dict(item_fields("nopic", "IT_NOPIC", "No picture", base=20), ModelPart1=(B, 99))))
    g("robe_red.uti", root("UTI ", **dict(item_fields("robe_red", "IT_ROBE", "Robe", base=16, cost=50), Cloth1Color=(B, 10))))
    g("robe_blue.uti", root("UTI ", **dict(item_fields("robe_blue", "IT_ROBE", "Robe", base=16, cost=50), Cloth1Color=(B, 22))))
    g("shop.utm", root("UTM ", ResRef=(R, "shop"), Tag=(X, "SHOP"), LocName=loc("Shop"),
                       StoreList=(LST, [st(0, ItemList=(LST, [st(0, InventoryRes=(R, "sold"))] +
                                                        [st(0, InventoryRes=(R, "junk")) for _ in range(120)]))])))
    for a in ("town", "busy"):
        g(f"{a}.are", root("ARE ", Name=loc(a), Tag=(X, a.upper()), ResRef=(R, a), Tileset=(R, "tcn01"),
                           Width=(I, 4), Height=(I, 4), OnHeartbeat=(R, "hb_quiet" if a == "town" else "hb_busy")))
        if a == "town":
            crs = [st(4, TemplateResRef=(R, "marshal"), Tag=(X, "MARSHAL"), FirstName=loc("Marshal"),
                      Conversation=(R, "marshal"), VarTable=vt)]
            plcs = [st(9, TemplateResRef=(R, "plc_chest1"), Tag=(X, f"CHEST{i}"), Static=(B, 0), Useable=(B, 0),
                       LocName=loc("Crate")) for i in range(30)]
        else:
            crs = [st(4, TemplateResRef=(R, "goblin"), Tag=(X, "GOBLIN"), FirstName=loc("Goblin"),
                      ScriptHeartbeat=(R, "hb_busy")) for _ in range(90)]
            plcs = [st(9, TemplateResRef=(R, "plc_chest1"), Tag=(X, "BIGCHEST"), Static=(B, 0), Useable=(B, 1),
                       HasInventory=(B, 1), LocName=loc("Hoard"),
                       ItemList=(LST, [st(0, InventoryRes=(R, "junk"), TemplateResRef=(R, "junk")) for _ in range(80)]))]
        g(f"{a}.git", root("GIT ", **{"Creature List": (LST, crs), "Placeable List": (LST, plcs),
                                      "StoreList": (LST, [st(11, ResRef=(R, "shop"), Tag=(X, "SHOP"))] if a == "town" else [])}))


def analyse(tmp):
    """The tools fixture, analysed into the test workspace (the dashboard functions find it there by name)."""
    mod, out = os.path.join(tmp, "mod"), os.path.join(h.WORKSPACE, "toolstest")
    build(mod)
    rep = h.analyse(mod, out, write_json=False)
    return mod, out, rep


def test_varaudit(rep):
    va = rep.get("var_audit") or {}
    check("audit ran without error", va and not va.get("summary", {}).get("error"), va.get("summary"))
    V = {(v["system"], v["name"], v["vtype"]): v for v in va.get("variables", [])}
    v = V.get(("local", "nDonekobolds", "int"), {})
    check("read under different capitals is a case_twin warning", v.get("status") == "case_twin" and v.get("level") == "warning", v)
    v = V.get(("local", "nAge", "string"), {})
    check("set as int, read as string is a type mismatch", v.get("status") == "type_mismatch", v)
    v = V.get(("local", "nNeverSet", "int"), {})
    check("read but never set anywhere is a warning", v.get("status") == "read_never_set" and v.get("level") == "warning", v)
    v = V.get(("local", "nFromToolset", "int"), {})
    check("a variable typed into the toolset counts as set", v.get("status") == "ok" and v.get("toolset"), v)
    v = V.get(("local", "X2_ENGINE_THING", "int"), {})
    check("engine-prefixed names are only info", v.get("level") == "info", v)
    v = V.get(("local", "nWritten", "int"), {})
    check("set but never read is info", v.get("status") == "set_never_read" and v.get("level") == "info", v)
    T = {t["tag"]: t for t in va.get("tags", [])}
    check("tag of a placed object is fine", T.get("MARSHAL", {}).get("status") == "placed", T.get("MARSHAL"))
    check("tag with other capitals is flagged (tags are case-sensitive)", T.get("marshal", {}).get("status") == "case_twin", T.get("marshal"))
    check("tag nobody has is missing", T.get("NOSUCH_TAG", {}).get("status") == "missing", T.get("NOSUCH_TAG"))
    check("tag only on an unused blueprint is flagged", T.get("IT_UNUSED", {}).get("status") == "blueprint_only", T.get("IT_UNUSED"))
    check("tag of an item a store sells counts as obtainable", T.get("IT_SOLD", {}).get("status") == "spawned", T.get("IT_SOLD"))
    check("tag given at run time (CreateObject new tag) is fine", T.get("RT_TAG", {}).get("status") == "runtime", T.get("RT_TAG"))
    K = {t["system"]: t for t in va.get("tokens", [])}
    check("two includes with the same helper names are two token systems", len(K) == 2 and "QuestToken" in K, list(K))
    a = K.get("QuestToken", {}); slots = {r["slot"]: r for r in a.get("slots", [])}
    check("token slot used by one name is 'used', not shared", slots.get(3, {}).get("status") == "used", slots.get(3))
    check("token string length from its own include", a.get("length") == 20, a.get("length"))
    check("slot read but never set", slots.get(4, {}).get("status") == "read, never set", slots.get(4))
    check("slot named in the include but unused is shown", slots.get(5, {}).get("status") == "named, unused"
          and slots[5]["names"] == ["QA_SPARE"], slots.get(5))
    check("free slots listed", 0 in a.get("free", []) and 19 in a.get("free", []), a.get("free"))
    b = next((t for s_, t in K.items() if s_ != "QuestToken"), {})
    check("second system has its own length", b.get("length") == 10, b.get("length"))
    iss = {i["category"] for i in rep["issues"]}
    check("case and type problems reach the Issues list", {"variable_case_twin", "variable_type_mismatch", "tag_case"} <= iss, iss)
    iq = {q["id"]: q for q in rep["inferred_quests"]["quests"]}
    check("quests: slot 3 in two systems is not 'shared'", not any("overwrite each other" in p["text"]
                                                                  for q in iq.values() for p in q["problems"]),
          [q["problems"] for q in iq.values() if q["problems"]])


def test_facts(tmp, mod, out, rep):
    import nwn_facts
    ws = os.path.dirname(out)
    f = nwn_facts.Facts("toolstest", workspace=ws)
    cv = next(c for c in rep["conversations"] if c["name"] == "marshal")
    check("conversations: each lists the areas where its speaker stands", [x.split(" [")[0] for x in cv["areas"]] == ["town"], cv.get("areas"))
    sb = cv.get("started_by", [])
    check("conversations: a script that starts one is listed with its line", any(x["script"] == "c_start" and x["how"] == "starts it"
                                                                                and x["line"] == 3 for x in sb), sb)
    ds = rep.get("conversation_dynamic_starts", [])
    check("conversations: a start from a name held in a variable is listed", any(x["script"] == "c_dyn" and x["line"] == 4 for x in ds), ds)
    check("conversations: a literal start is not listed as a variable start", not any(x["script"] == "c_start" for x in ds), ds)
    sm = f.summary()["quests"]
    check("facts: summary counts journal and script quests", sm["total"] == sm["journal"] + sm["from_scripts"]
          and sm["from_scripts"] == rep["summary"]["inferred_quests"] >= 1, sm)
    check("facts: analyses listed", [a["name"] for a in nwn_facts.list_analyses(ws)] == ["toolstest"])
    e = f.exists("a_bears")
    check("facts: a script exists (as .nss and .ncs)", e["found"] and {m.get("type") for m in e["matches"]} >= {"nss"}, e)
    e = f.exists("marshal")
    check("facts: exists reports a case-only tag near miss", "MARSHAL" in e["case_differs"], e)
    check("facts: something that isn't there is not found", not f.exists("zz_no_such_thing")["found"])
    u = f.who_uses("script:a_bears")
    check("facts: who uses a script (the conversation)", any(x["node"] == "dlg:marshal" and x["kind"] == "dlg_script"
                                                            for x in u["users"]), u)
    u = f.who_uses("hb_busy")
    check("facts: a plain name finds the node too", u["total"] >= 2 and "script:hb_busy" in u["nodes"], u)
    sc = f.script("a_bears", 1, 3)
    check("facts: script source with line numbers and includes", sc["found"] and sc["includes"] == ["inc_a"]
          and sc["text"].startswith("    1  #include"), sc)
    g_ = f.grep(r"SetQuestToken\(.*QA_BEARS")
    check("facts: grep finds script:line", [(m["script"], m["line"]) for m in g_["matches"]] == [("a_bears", 4)], g_)
    v = f.variable("nAge")
    check("facts: variable shows both types it is used as", {x["vtype"] for x in v["audit"]} == {"int", "string"}, v)
    t = f.tag("MARSHAL")
    check("facts: tag lists placed objects", t["placed"] >= 1 and t["lookups"]["status"] == "placed", t)
    q = f.quest("QA_BEARS")
    check("facts: quest by slot constant", q["inferred"] and q["inferred"][0]["key"] == "3", q)
    check("facts: fields of a file", any(x["path"] == "Tag" and x["value"] == "MARSHAL" for x in f.fields("marshal.utc")["fields"]))
    check("facts: issues filter", f.issues("variable_case_twin")["total"] >= 1)
    check("facts: compile status without a compile run says so", "not compiled" in f.compiled("a_bears")["last_compile"])
    try:
        f.db.execute("DELETE FROM files")
        wrote = True
    except sqlite3.OperationalError:
        wrote = False
    check("facts: the index is opened read-only (a write is refused)", not wrote)
    try:
        nwn_facts.Facts("../etc", workspace=ws)
        bad = False
    except ValueError:
        bad = True
    check("facts: analysis names can't walk out of the workspace", bad)
    f.close()


def test_housekeep(tmp, mod, out, rep):
    import nwn_housekeep as H
    check("housekeep: families from real backup file names", H.family_of("PW_Main_202202_8193_34_1f_head_1a.mod") == "pw_main"
          and H.family_of("20081105_Dark Shore - Lost Coast.mod") == "dark_shore_lost_coast"
          and H.family_of("PW_Main-2024-03-08_BUILDER.mod") == "pw_main"
          and H.family_of("Aeon_PW_302b.mod") == "aeon_pw")
    folder, ws = os.path.join(tmp, "modules"), os.path.join(tmp, "hkws")
    os.makedirs(folder); os.makedirs(os.path.join(folder, "temp0"))
    old = time.time() - 30 * 86400
    files = {"PW_Main_2023-12-21.mod": b"A" * 1000, "PW_Main_2023-12-31.mod": b"B" * 1000,
             "PW_Main_2024-01-05.mod": b"C" * 1001, "PW_Main_2024-02-15.mod": b"D" * 1002,
             "PW_Main_2024-02-16.mod": b"D" * 1002, "PW_Main_2024-02-16.BackupMod": b"d" * 1002,
             "PW_Main_2023-12-21.BackupMod": b"a" * 999, "PW_Main_2202_2j.mod": b"",
             "PW_Main.mod": b"E" * 1100, "lokafk.mod": b"L" * 10, "keepme_2020_01.mod": b"K" * 5,
             "keepme_2019_01.mod": b"k" * 5, "notes.rar": b"R"}
    for i, (nm, data) in enumerate(sorted(files.items(), key=lambda kv: kv[0])):
        with open(os.path.join(folder, nm), "wb") as fh:
            fh.write(data)
    order = ["PW_Main_2023-12-21.BackupMod", "PW_Main_2023-12-21.mod", "PW_Main_2023-12-31.mod",
             "PW_Main_2024-01-05.mod", "PW_Main_2202_2j.mod", "PW_Main_2024-02-15.mod",
             "PW_Main_2024-02-16.BackupMod", "PW_Main_2024-02-16.mod", "PW_Main.mod",
             "lokafk.mod", "keepme_2019_01.mod", "keepme_2020_01.mod", "notes.rar"]
    for i, nm in enumerate(order):
        os.utime(os.path.join(folder, nm), (old + i * 3600, old + i * 3600))
    r = H.scan(folder, ws, keep_newest=2, keep_names=["keepme_2019_01.mod"])
    F = {f["name"]: f for f in r["files"]}
    check("housekeep: only .mod / .BackupMod are listed (no .rar, no folders)", "notes.rar" not in F and "temp0" in r["summary"]["folders_left_alone"])
    check("housekeep: newest of a family is kept", not F["PW_Main.mod"]["suggest"])
    check("housekeep: keep-newest N respected", not F["PW_Main_2024-02-16.mod"]["suggest"])
    check("housekeep: older versions suggested", F["PW_Main_2023-12-21.mod"]["suggest"] and F["PW_Main_2024-01-05.mod"]["suggest"])
    check("housekeep: identical copy found by content", F["PW_Main_2024-02-15.mod"].get("identical_to") == "PW_Main_2024-02-16.mod"
          and F["PW_Main_2024-02-15.mod"]["suggest"], F["PW_Main_2024-02-15.mod"])
    check("housekeep: empty (0-byte) module suggested", F["PW_Main_2202_2j.mod"]["suggest"] and "empty" in F["PW_Main_2202_2j.mod"]["archive_reasons"][0])
    check("housekeep: old .BackupMod suggested", F["PW_Main_2023-12-21.BackupMod"]["suggest"])
    check("housekeep: keep list honoured", not F["keepme_2019_01.mod"]["suggest"] and "on your keep list" in F["keepme_2019_01.mod"]["keep_reasons"])
    names = [f["name"] for f in r["files"] if f["suggest"]]
    before = set(os.listdir(folder))
    b = H.move(folder, names, ws)
    moved = {e["name"] for e in b["entries"] if e["status"] == "moved"}
    check("housekeep: ticked files moved into the archive folder", moved == set(names) and all(
        os.path.isfile(os.path.join(b["dest"], x)) and not os.path.exists(os.path.join(folder, x)) for x in names), b["entries"])
    check("housekeep: manifest written in the archive", os.path.isfile(os.path.join(b["dest"], "manifest.json")))
    check("housekeep: nothing else in the folder touched", set(os.listdir(folder)) == (before - set(names)) | {H.ARCHIVE_DIR})
    try:
        H.move(folder, ["../x.mod"], ws); bad = False
    except ValueError:
        bad = True
    check("housekeep: names with paths are refused", bad)
    try:
        H.move(folder, ["lokafk.mod"], ws, dest=folder); bad2 = False
    except ValueError:
        bad2 = True
    check("housekeep: the modules folder itself can't be the archive", bad2)
    # a file that reappeared is not overwritten by undo
    with open(os.path.join(folder, names[0]), "wb") as fh:
        fh.write(b"NEW")
    u = H.undo(b["manifest"] if "manifest" in b else os.path.join(b["dest"], "manifest.json"), ws)
    back = [e for e in u["entries"] if e["status"] == "put back"]
    check("housekeep: undo puts files back", len(back) == len(names) - 1, [e["status"] for e in u["entries"]])
    check("housekeep: undo never overwrites a file that came back", open(os.path.join(folder, names[0]), "rb").read() == b"NEW"
          and os.path.isfile(os.path.join(b["dest"], names[0])))
    check("housekeep: batches recorded", len(H.list_batches(ws)) == 1)
    # Stop pressed during a move (the progress callback raises): the moved file must still be undoable
    names2 = ["lokafk.mod", "keepme_2020_01.mod"]
    calls = []

    def boom(pct, msg):
        calls.append(msg)
        raise RuntimeError("Stop")
    b2 = H.move(folder, names2, ws, progress=boom)
    st2 = {e["name"]: e["status"] for e in b2["entries"]}
    check("housekeep: Stop during a move stops after the current file, and records it", st2 == {"lokafk.mod": "moved",
          "keepme_2020_01.mod": "not moved (stopped)"} and len(H.list_batches(ws)) == 2, st2)
    u2 = H.undo(os.path.join(b2["dest"], "manifest.json"), ws)
    check("housekeep: ...and Undo puts it back", os.path.isfile(os.path.join(folder, "lokafk.mod")) and u2["undone"], u2["entries"])
    # a manifest left 'pending' (interrupted between the rename and the save) is still undone from the disk state
    b3 = H.move(folder, ["lokafk.mod"], ws)
    m3 = os.path.join(b3["dest"], "manifest.json")
    j3 = json.load(open(m3)); j3["entries"][0]["status"] = "pending"; json.dump(j3, open(m3, "w"))
    H.undo(m3, ws)
    check("housekeep: an interrupted 'pending' entry is put back too", os.path.isfile(os.path.join(folder, "lokafk.mod")))
    try:
        H.move(folder, ["lokafk.mod"], ws, dest=b3["dest"]); reused = True
    except ValueError:
        reused = False
    check("housekeep: a folder that already holds a batch can't be reused", not reused)
    b4 = H.move(folder, ["lokafk.mod"], ws, expected={"lokafk.mod": (999, 0)})
    check("housekeep: a file that changed since the scan is skipped", b4["entries"][0]["status"].startswith("skipped: it changed"), b4["entries"])
    # backup newer than, and identical to, the live module: the live .mod is kept
    f2 = os.path.join(tmp, "modules2"); os.makedirs(f2)
    for nm, t in (("Keep.mod", old), ("Keep.BackupMod", old + 60)):
        with open(os.path.join(f2, nm), "wb") as fh:
            fh.write(b"SAME" * 100)
        os.utime(os.path.join(f2, nm), (t, t))
    r2 = H.scan(f2, os.path.join(tmp, "hkws2"))
    F2 = {f["name"]: f for f in r2["files"]}
    check("housekeep: the live .mod is never archived in favour of its identical backup", not F2["Keep.mod"]["suggest"]
          and F2["Keep.BackupMod"]["suggest"], F2)


def test_accept(tmp, mod, out, rep):
    import nwn_dashboard                       # its workspace is the test workspace, where `out` is
    import nwn_facts
    i = next(x for x in rep["issues"] if x["category"] == "variable_case_twin")
    key = f"{i['category']}|{i['node']}|{i['label']}"
    import re as _re
    sig = _re.sub(r"\d+", "#", i["detail"])
    data = nwn_dashboard.accept_issue(dict(a="toolstest", key=key, sig=sig, accepted=True, note="DM tool sets it"))
    check("accept: issue stored as by design with its note", data[key]["note"] == "DM tool sets it")
    f = nwn_facts.Facts("toolstest", workspace=os.path.dirname(out))
    got = next(x for x in f.issues("variable_case_twin")["issues"] if x["label"] == i["label"])
    check("accept: the facts API reports it as accepted (so it isn't raised again)", got.get("accepted_by_design") is True, got)
    with open(os.path.join(out, "accepted_issues.json"), encoding="utf-8") as fh:
        stored = json.load(fh)
    stored[key]["sig"] = "something else"
    with open(os.path.join(out, "accepted_issues.json"), "w", encoding="utf-8") as fh:
        json.dump(stored, fh)
    got = next(x for x in f.issues("variable_case_twin")["issues"] if x["label"] == i["label"])
    check("accept: an accepted issue whose text changed counts as open again", not got.get("accepted_by_design"), got)
    try:
        nwn_dashboard.accept_issue(dict(a="toolstest", key="nonsense", accepted=True)); bad = False
    except ValueError:
        bad = True
    check("accept: malformed keys refused", bad)
    check("accept: Clear keeps accepted issues (they are your work)", "accepted_issues.json" in nwn_dashboard.USER_PARTS["descriptions"]
          and "accepted_issues.json" not in nwn_dashboard.DATA_FILES)
    nwn_dashboard.accept_issue(dict(a="toolstest", key=key, accepted=False))
    check("accept: un-ticking removes it", key not in json.load(open(os.path.join(out, "accepted_issues.json"), encoding="utf-8")))
    f.close()


def test_archive(tmp, mod, out, rep):
    import nwn_archive as A
    d = os.path.join(os.path.dirname(out), "arccopy")
    shutil.copytree(out, d)
    os.makedirs(os.path.join(d, "edits"), exist_ok=True)
    with open(os.path.join(d, "edits", "mine.nss"), "w") as fh:
        fh.write("void main(){}")
    with open(os.path.join(d, "descriptions.json"), "w") as fh:
        fh.write("{}")
    before = {}
    for root_, _ds, fs in os.walk(d):
        for f_ in fs:
            p_ = os.path.join(root_, f_)
            before[os.path.relpath(p_, d)] = open(p_, "rb").read()
    info = A.archive(d)
    left = sorted(os.listdir(d))
    check("archive: only the zip, the stub and your own work remain", set(left) == {"analysis_archive.zip", "archive.json", "edits", "descriptions.json"}
          or set(left) == {"analysis_archive.zip", "archive.json", "edits", "descriptions.json", "accepted_issues.json"}, left)
    check("archive: smaller than before", info["archive_bytes"] < info["original_bytes"], info)
    check("archive: is_archived", A.is_archived(d))
    import nwn_facts
    check("archive: facts API lists it as archived", any(x["name"] == "arccopy" and "archived" in x["status"]
                                                         for x in nwn_facts.list_analyses(os.path.dirname(out))))
    try:
        A.archive(d); again = False
    except ValueError:
        again = True
    check("archive: can't archive twice", again)
    A.unarchive(d)
    after = {}
    for root_, _ds, fs in os.walk(d):
        for f_ in fs:
            p_ = os.path.join(root_, f_)
            after[os.path.relpath(p_, d)] = open(p_, "rb").read()
    check("archive: unpack gives back every file byte for byte", after == before,
          sorted(set(before) ^ set(after))[:10])
    # tampering and hostile zips
    A.archive(d)
    with open(os.path.join(d, "analysis_archive.zip"), "ab") as fh:
        fh.write(b"x")
    try:
        A.unarchive(d); tampered = False
    except OSError:
        tampered = True
    check("archive: a changed zip is not unpacked (SHA-256 check)", tampered and A.is_archived(d))
    evil = os.path.join(d, "analysis_archive.zip")
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escaped.txt", "x")
    stub = json.load(open(os.path.join(d, "archive.json")))
    stub["sha256"] = A._sha256(evil)
    json.dump(stub, open(os.path.join(d, "archive.json"), "w"))
    try:
        A.unarchive(d); evil_ok = True
    except OSError:
        evil_ok = False
    check("archive: entries that would land outside the folder are refused", not evil_ok
          and not os.path.exists(os.path.join(os.path.dirname(d), "escaped.txt")))
    d2 = os.path.join(os.path.dirname(out), "arcbusy")
    shutil.copytree(out, d2)
    open(os.path.join(d2, ".incomplete"), "w").close()
    try:
        A.archive(d2); busy = False
    except ValueError:
        busy = True
    check("archive: an unfinished analysis can't be archived", busy)
    shutil.rmtree(d2)


def test_diff(tmp, mod, out, rep):
    import nwn_diff as D
    ws = os.path.dirname(out)
    new = os.path.join(tmp, "mod_v2")
    shutil.copytree(mod, new)
    with open(os.path.join(new, "a_bears.nss"), "a") as fh:
        fh.write("// changed\n")
    r_ = n.read_gff(open(os.path.join(new, "marshal.utc"), "rb").read())
    r_.set("Tag", r_.type_of("Tag"), "MARSHAL2")
    with open(os.path.join(new, "marshal.utc"), "wb") as fh:
        fh.write(n.write_gff(r_))
    os.remove(os.path.join(new, "hb_busy.nss"))
    with open(os.path.join(new, "zz_new.nss"), "w") as fh:
        fh.write("void main(){}\n")
    r = D.compare(mod, new, "toolstest", ws)
    C = {c["file"]: c for c in r["changes"]}
    check("diff: added / removed / changed found", C.get("zz_new.nss", {}).get("change") == "added" and C.get("hb_busy.nss", {}).get("change") == "removed"
          and C.get("a_bears.nss", {}).get("change") == "changed", sorted(C))
    check("diff: unchanged files are not listed", "v_set.nss" not in C and "town.are" not in C)
    check("diff: script change shown line by line", any(x.startswith("+// changed") for x in C["a_bears.nss"].get("diff", [])), C["a_bears.nss"].get("diff"))
    f = C.get("marshal.utc", {}).get("fields", [])
    check("diff: GFF change shown field by field", f == [dict(path="Tag", old="MARSHAL", new="MARSHAL2")], f)
    check("diff: impact from the analysis", C["a_bears.nss"].get("impact") in ("Low", "Medium", "High", "Critical"), C["a_bears.nss"])
    check("diff: a removed file that things use is flagged", (C["hb_busy.nss"].get("used_by") or 0) >= 2, C["hb_busy.nss"])
    snap = D.take_snapshot(mod, ws, "before")
    check("diff: snapshot saved in the workspace", os.path.isfile(snap["path"]) and D.list_snapshots(ws))
    r2 = D.compare(snap["path"], new, None, ws)
    C2 = {c["file"]: c for c in r2["changes"]}
    check("diff: comparing against a snapshot finds the same files", set(C2) == set(C), sorted(set(C2) ^ set(C)))
    check("diff: snapshot keeps script text for line diffs", any(x.startswith("+// changed") for x in C2["a_bears.nss"].get("diff", [])))
    check("diff: snapshot says GFF detail needs the .mod", "needs the older .mod" in C2["marshal.utc"].get("note", ""),
          C2.get("marshal.utc"))
    erf_old, erf_new = os.path.join(tmp, "old.mod"), os.path.join(tmp, "new.mod")
    for folder, path in ((mod, erf_old), (new, erf_new)):
        files = []
        for fn in sorted(os.listdir(folder)):
            rr, ext = fn.rsplit(".", 1)
            files.append((rr, ext, open(os.path.join(folder, fn), "rb").read()))
        n.write_erf(path, files)
    r3 = D.compare(erf_old, erf_new)
    check("diff: works on .mod files too", {c["file"] for c in r3["changes"]} == set(C), sorted({c["file"] for c in r3["changes"]} ^ set(C)))


def test_perf(tmp, mod, out, rep):
    pp = rep.get("pw_performance") or {}
    check("perf: ran without error", pp and not pp["summary"].get("error"), pp.get("summary"))
    F = {}
    for f in pp.get("findings", []):
        F.setdefault(f["category"], []).append(f)
    A = {a["node"]: a for a in pp.get("areas", [])}
    busy = A.get("area:busy", {})
    check("perf: creatures and custom heartbeats counted per area", busy.get("creatures") == 90 and busy.get("custom_heartbeats") >= 90, busy)
    check("perf: crowded area flagged", any(f["node"] == "area:busy" for f in F.get("perf_spawn_density", [])), F.get("perf_spawn_density"))
    check("perf: area with many custom heartbeats flagged", any(f["node"] == "area:busy" for f in F.get("perf_area_heartbeats", [])))
    check("perf: heartbeat that walks every object is flagged", any(f["node"] == "script:hb_busy" for f in F.get("perf_heavy_heartbeat", [])),
          F.get("perf_heavy_heartbeat"))
    check("perf: DelayCommand in a loop on a heartbeat is a warning", any(f["node"] == "script:hb_busy" and f["severity"] == "warning"
                                                                         for f in F.get("perf_delay_in_loop", [])), F.get("perf_delay_in_loop"))
    check("perf: self-rescheduling function found", any("Tick() re-schedules itself every 6.0" in f["detail"] for f in F.get("perf_self_reschedule", [])),
          F.get("perf_self_reschedule"))
    check("perf: empty heartbeat script noted", any(f["node"] == "script:hb_quiet" for f in F.get("perf_empty_heartbeat", [])), F.get("perf_empty_heartbeat"))
    check("perf: container with 80 items flagged", any(f["count"] == 80 for f in F.get("perf_inventory", [])), F.get("perf_inventory"))
    check("perf: store under the store limit not flagged", not any(f["count"] == 121 for f in F.get("perf_inventory", [])))
    town = A.get("area:town", {})
    check("perf: plain crates counted as could-be-Static", town.get("static_candidates") == 30 and busy.get("static_candidates") == 0,
          (town.get("static_candidates"), busy.get("static_candidates")))
    check("perf: warnings reach the Issues list", any(i["category"] == "perf_spawn_density" for i in rep["issues"]))
    check("perf: CSVs written", os.path.isfile(os.path.join(out, "reports", "performance_findings.csv")))


def test_rename(tmp, mod, out, rep):
    import nwn_refactor as RF
    import nwn_edit
    d = os.path.join(os.path.dirname(out), "rncopy")
    shutil.copytree(out, d)
    shutil.rmtree(os.path.join(d, "edits"), ignore_errors=True)
    r = RF.find(d, "MARSHAL", "exact", case=True)
    kinds = {h["kind"] for h in r["hits"]}
    check("find: a tag is found in scripts, blueprints and placed objects", {"script", "object"} <= kinds, r["by_kind"])
    wh = {(h["file"], h["where"]) for h in r["hits"]}
    check("find: each hit says where (script line / field path)", ("t_look.nss", "line 3") in wh and ("marshal.utc", "Tag") in wh
          and ("town.git", "Creature List[0]/Tag") in wh, sorted(wh)[:12])
    check("find: exact + capitals excludes 'marshal'", not any(h["file"] == "t_look.nss" and h["line"] == 4 for h in r["hits"]))
    placed = next(h for h in r["hits"] if h["file"] == "town.git")
    check("find: a placed object's hit names its owner object", placed["node"] == "inst:town:Creature List[0]", placed)
    r2 = RF.find(d, "kobold", "contains", case=False, kinds=["conversation", "script", "object", "2da", "filename"])
    check("find: a part-word search ignoring capitals finds both spellings (nDoneKobolds / nDonekobolds)",
          {"v_set.nss", "v_read.nss"} <= {x["file"] for x in r2["hits"]}, [(x["file"], x["where"]) for x in r2["hits"]])
    r3 = RF.find(d, "hb_quiet", "exact")
    check("find: file names found", any(h["kind"] == "filename" and h["file"] == "hb_quiet.nss" for h in r3["hits"]), r3["hits"])
    # plan + apply: retag MARSHAL -> MARSHAL_NEW everywhere in the module
    ids = [h["id"] for h in r["hits"]]
    pl = RF.plan(d, "MARSHAL", "MARSHAL_NEW", ids, "exact", True)
    check("replace: plan shows before and after for every hit", pl["ok"] == len(ids) and all(c["after"] for c in pl["changes"]), pl["changes"][:3])
    sl = next(c for c in pl["changes"] if c["file"] == "t_look.nss")
    check("replace: in scripts only the whole string literal changes", sl["after"].count('"MARSHAL_NEW"') == 1 and '"marshal"' not in sl["after"], sl)
    bad = RF.plan(d, "MARSHAL", "x" * 40, ids, "exact", True)
    check("replace: a tag over 32 characters is refused", bad["ok"] == 0 and "32" in bad["changes"][0]["why"], bad["changes"][0])
    res = RF.apply(d, "MARSHAL", "MARSHAL_NEW", ids, "exact", True)
    E = nwn_edit.list_edits(d)
    check("replace: changed files written to edits only", {"t_look.nss", "marshal.utc", "town.git"} <= set(E), E)
    g_ = n.read_gff(open(os.path.join(d, "edits", "town.git"), "rb").read())
    check("replace: placed object's tag changed in the edited area", g_.get("Creature List")[0].get("Tag") == "MARSHAL_NEW")
    check("replace: the original module is untouched", b"MARSHAL_NEW" not in open(os.path.join(mod, "town.git"), "rb").read())
    # rename a script file and what points at it
    r4 = RF.find(d, "hb_quiet", "exact")
    res2 = RF.apply(d, "hb_quiet", "hb_calm", [h["id"] for h in r4["hits"]], "exact")
    E = nwn_edit.list_edits(d)
    check("rename: new file added, old one marked removed", E.get("hb_calm.nss", {}).get("new") and E.get("hb_quiet.nss", {}).get("kind") == "deleted", E)
    check("rename: the area's heartbeat now points at the new name",
          n.read_gff(open(os.path.join(d, "edits", "town.are"), "rb").read()).get("OnHeartbeat") == "hb_calm")
    # build + audit with the renames
    import nwn_build
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(d, {"delete": [], "merge": []}, verbose=False,
                              audit_kwargs=dict(compiler=h.NO_COMPILER))
    bad_checks = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED", "WARN")]
    check("rename: the clean build applies it and the audit passes", not bad_checks and
          any(x["file"] == "hb_quiet.nss" for x in log["deleted"]) and "hb_calm.nss" in log["added"], bad_checks or log["deleted"])
    # a file edited since the analysis: the search's field paths must not hit a different object
    r5 = RF.find(d, "CHEST5", "exact", case=True)
    g5 = n.read_gff(open(os.path.join(mod, "town.git"), "rb").read())
    del g5.get("Placeable List")[0]                        # the edited area has one crate fewer: paths shift
    with open(os.path.join(d, "edits", "town.git"), "wb") as fh:
        fh.write(n.write_gff(g5))
    try:
        RF.apply(d, "CHEST5", "CHEST_X", [h["id"] for h in r5["hits"]], "exact", True); stale = False
    except ValueError as ex:
        stale = "since" in str(ex)
    g5b = n.read_gff(open(os.path.join(d, "edits", "town.git"), "rb").read())
    check("replace: a field path that now points at a different object is refused", stale and
          all(p_.get("Tag") != "CHEST_X" for p_ in g5b.get("Placeable List")))
    check("find: hits in files you've edited are marked", any(h.get("edited") for h in RF.find(d, "CHEST5", "exact", True)["hits"]))
    pl6 = RF.plan(d, "junk", "", [h["id"] for h in RF.find(d, "junk", "exact")["hits"]], "exact")
    check("replace: an empty new resource name is refused", pl6["ok"] == 0, pl6["changes"][:2])
    # undo refuses when a file was edited after the change
    r7 = RF.find(d, "IT_SOLD", "exact", True)
    res7 = RF.apply(d, "IT_SOLD", "IT_SOLD2", [h["id"] for h in r7["hits"]], "exact", True)
    with open(os.path.join(d, "edits", "t_look.nss"), "a") as fh:
        fh.write("// my later edit\n")
    try:
        RF.undo(d, res7["id"]); lost = True
    except ValueError:
        lost = False
    check("undo: refused when the file was edited afterwards (your later edit is kept)", not lost
          and open(os.path.join(d, "edits", "t_look.nss")).read().endswith("// my later edit\n"))
    with open(os.path.join(d, "edits", "t_look.nss"), "r") as fh:
        txt = fh.read()
    with open(os.path.join(d, "edits", "t_look.nss"), "w") as fh:
        fh.write(txt.replace("// my later edit\n", ""))
    RF.undo(d, res7["id"])
    os.remove(os.path.join(d, "edits", "town.git"))
    # undo: newest first
    try:
        RF.undo(d, res["id"]); order_ok = False
    except ValueError:
        order_ok = True
    check("undo: an older change can't be undone before a newer one", order_ok)
    RF.undo(d, res2["id"])
    E = nwn_edit.list_edits(d)
    check("undo: rename undone (new file and removed-marker gone)", "hb_calm.nss" not in E and "hb_quiet.nss" not in E, E)
    RF.undo(d, res["id"])
    E = nwn_edit.list_edits(d)
    check("undo: retag undone", "t_look.nss" not in E and "town.git" not in E, E)


def test_dupdiff(tmp, mod, out, rep):
    grp = [d for d in rep["duplicates"] if {"bp:robe_red.uti", "bp:robe_blue.uti"} <= {m["node"] for m in d["members"]}]
    check("dupes: two robes that differ only in colour are grouped", grp, [d["category"] for d in rep["duplicates"]])
    if grp:
        d = grp[0]
        check("dupes: a colour-only difference is never auto-mergeable", not d["mergeable"], d)
        other = next(m for m in d["members"] if m["node"] != d["keeper"])
        check("dupes: the difference is shown as 'look'", (other.get("differs") or {}).get("counts", {}).get("look") == 1
              and other["differs"]["examples"][0]["path"] == "Cloth1Color", other.get("differs"))
    import nwn_dupdiff
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    diffs = nwn_dupdiff.compare(db, "bp:robe_red.uti", "bp:robe_blue.uti")
    check("dupes: field comparison ignores the resref and lists the colour", [(x["path"], x["a"], x["b"], x["cls"]) for x in diffs]
          == [("Cloth1Color", "10", "22", "look")], diffs)
    db.close()


def test_external_tools(tmp, mod, out, rep):
    import nwn_dashboard
    clean = nwn_dashboard.clean_tools([{"name": "Hak viewer", "path": "C:/x.exe", "exts": ["hak", ".erf", "bad ext!"]},
                                       {"name": "", "path": "C:/y.exe"}, "junk"])
    check("tools: settings keep only well-formed tools", clean == [{"name": "Hak viewer", "path": "C:/x.exe", "exts": ["hak", "erf"]}], clean)
    # owner's rule (2026-10-05): no dependence on, or ready-made entries for, any particular third-party tool - the
    # user adds their own. The page, manual and install guide name none.
    named = [f for f in ("dashboard.html", "docs/MANUAL.md", "INSTALL.md")
             if re.search(r"Aurora (Hak|TLK)|NWN Explorer|TOOL_PRESETS|data-preset",
                          open(os.path.join(ROOT, f), encoding="utf-8").read())]
    check("tools: no particular third-party tool is named or preset - users add their own", named == [], named)
    try:
        nwn_dashboard.open_in_tool("Not configured", mod); bad = False
    except ValueError:
        bad = True
    check("tools: a program that isn't in your Settings is never started", bad)
    # the "tool" is this Python, and the file it opens is a tiny script that records how it was started
    marker = os.path.join(tmp, "tool_ran.txt")
    probe = f"import sys\nopen({marker!r}, 'w').write(sys.argv[0])\n"
    s_ = nwn_dashboard.settings(); s_["external_tools"] = [dict(name="Interp", path=sys.executable, exts=["py"])]
    nwn_dashboard.save_settings(s_)
    warn = nwn_dashboard.settings_warnings(nwn_dashboard.settings())
    check("tools: a script interpreter (python, cmd, bash...) set as a tool is named in a settings warning",
          any("Interp" in w_ and "runs scripts" in w_ for w_ in warn), warn)
    s_["external_tools"] = [dict(name="Fake", path=sys.executable, exts=["py"])]
    nwn_dashboard.save_settings(s_)
    for bad_file in ("", tmp):
        try:
            nwn_dashboard.open_in_tool("Fake", bad_file); ok_ = False
        except ValueError:
            ok_ = True
        check(f"tools: an empty path or a folder is refused ({bad_file!r:.20})", ok_)
    try:
        nwn_dashboard.open_in_tool("Fake", os.path.join(mod, "town.are")); wrong_type = False
    except ValueError:
        wrong_type = True
    check("tools: a tool is only offered for its file types", wrong_type)
    def run_probe(fn):
        pth = os.path.join(tmp, fn)
        with open(pth, "w") as fh:
            fh.write(probe)
        if os.path.exists(marker):
            os.remove(marker)
        nwn_dashboard.open_in_tool("Fake", pth)
        for _ in range(100):
            if os.path.exists(marker) and open(marker).read():
                break
            time.sleep(0.1)
        return pth, (open(marker).read() if os.path.exists(marker) else "")
    pth, got = run_probe("probe.py")
    check("tools: the tool is started with the file as its argument", os.path.abspath(got) == os.path.abspath(pth), got)
    pth, got = run_probe("a & echo pwned ; touch pwned.py")
    check("tools: odd characters in file names are passed as-is, never run through a shell",
          os.path.abspath(got) == os.path.abspath(pth) and not os.path.exists(os.path.join(tmp, "pwned")), got)


def test_icons(tmp, mod, out, rep):
    import nwn_icons
    ir = nwn_icons.IconResolver(out)
    ic = ir.icon("bp:sold.uti")        # base item 1 (longsword), ModelPart1..3 -> 11 / 0 / 0
    check("icons: 3-part weapon uses bottom/middle/top pictures", [l_["name"] for l_ in ic["layers"]] == ["iwswls_b_011.tga"]
          and set(ic["missing"]) == {"iwswls_m_000.tga", "iwswls_t_000.tga"} and ic["layers"][0]["png"].startswith("data:image/png"), ic)
    ic = ir.icon("bp:potion.uti")
    check("icons: simple item uses i<class>_<part>", ic["found"] and [l_["name"] for l_ in ic["layers"]] == ["iit_potion_005.tga"], ic)
    ic = ir.icon("bp:nopic.uti")
    check("icons: a missing picture is reported (and no default here)", not ic["found"] and ic["missing"] == ["iit_potion_099.tga"], ic)
    ic = ir.icon("bp:robe_red.uti")
    check("icons: armour shows its default icon, with a note", ic["found"] and ic["layers"][0]["name"] == "iit_armor_00.tga" and "armour" in ic["note"], ic)
    inst = next((o["node"] for o in rep["orphan_instances"] if o.get("cls") == "item"), None)
    placed = [nd for nd in ("inst:busy:Placeable List[0]/ItemList[0]",)]
    ic = ir.icon(placed[0])
    check("icons: works for items placed in containers too", ic.get("base_item") is not None, ic)
    ir.close()


def test_questsets(tmp, mod, out, rep):
    import nwn_questsets as QS
    iq = rep["inferred_quests"]
    fams = {f["name"]: f for f in iq.get("families", [])}
    tok = [f for f in fams.values() if f["name"].startswith("INC_A quest tokens")]
    check("quest sets: token quests form a family named after their include", tok and tok[0]["kind"] == "quests", list(fams))
    check("quest sets: the family lists slots named in the include but never used", tok and
          any("QA_SPARE" in x["names"] for x in tok[0].get("named_unused", [])), tok and tok[0].get("named_unused"))
    check("quest sets: two token systems are two families", any(n.startswith("INC_B quest tokens") for n in fams), list(fams))
    bw = [q for q in iq["quests"] if q.get("name") == "X2_ENGINE_THING"]
    check("quest sets: a BioWare X2_ variable is the BioWare package, not a quest",
          bw and bw[0]["family_kind"] == "package" and bw[0]["family"].startswith("BioWare"), bw and bw[0].get("family"))
    check("quest sets: the summary separates real quests from systems' state",
          rep["summary"]["inferred_real_quests"] + rep["summary"]["inferred_system_state"] == rep["summary"]["inferred_quests"])
    # unit: names, packages, sets
    mk = lambda name, **k: dict(dict(id=name, kind="local", system="local", scope="pc", key=name, key_exprs=[name], label=name,  # noqa: E731
                                     scripts=[], conversations=[], givers=[], hub="(no conversation)"), **k)
    qs = [mk("QT_DONEANTS", kind="token", system="QuestToken", key="31"), mk("QT_DONEKOBOLDS", kind="token", system="QuestToken", key="33"),
          mk("DUKESARMY", kind="token", system="QuestToken", key="15"), mk("DUKESWIZARD", kind="token", system="QuestToken", key="19"),
          mk("hobgoblin quest for the marshal (QT_DONEHOBS)", kind="token", system="QuestToken", key="51", key_exprs=["QT_DONEHOBS"]),
          mk("dmfi_buff_level", scripts=["dmfi_execute", "dmfi_init_inc"]), mk("AI_VALID_SPELLS", scripts=["j_ai_ondamaged"]),
          mk("nSpookyDone", scripts=["j_ai_ondamaged", "zz_spooky", "zz_spooky2"], conversations=["dlg:spooky"], givers=["Ghost"]),
          mk("nWeather", scope="module", scripts=["mod_weather"]),
          mk("nRangerA", conversations=["dlg:cf_ranger"], hub="dlg:cf_ranger"), mk("gotSpores", conversations=["dlg:cf_ranger"], hub="dlg:cf_ranger")]
    va = dict(tokens=[dict(system="QuestToken", defined_in="pq_include", setter="SetQuestToken", length=100, free=[1, 2],
                           slots=[dict(slot=1, status="named, unused", names=["SPARE_RING"])])])
    out_ = QS.classify(dict(quests=qs, summary=dict(adapters=[])), va, lambda n: "Quest Token Include File" if n == "pq_include" else "")
    by = {q["name"]: q for q in qs}
    check("quest sets: token quests are named from the include", by["QT_DONEANTS"]["family"] == "PQ quest tokens (pq_include)",
          by["QT_DONEANTS"]["family"])
    check("quest sets: the constant name is the key, not the comment", by["QT_DONEHOBS"]["set"] == "QT_*", by["QT_DONEHOBS"])
    check("quest sets: names without separators share a letter prefix", by["DUKESARMY"]["set"] == "DUKES*", by["DUKESARMY"]["set"])
    check("quest sets: DMFI and Jasperre's AI are packages",
          by["dmfi_buff_level"]["family"].startswith("DMFI") and by["AI_VALID_SPELLS"]["family"] == "Jasperre's AI")
    check("quest sets: one package script among several does not make a quest a package",
          by["nSpookyDone"]["family"] == "Module quests (player variables)", by["nSpookyDone"]["family"])
    check("quest sets: module-wide state is a module system", by["nWeather"]["family_kind"] == "system", by["nWeather"])
    check("quest sets: quests sharing a quest-giver conversation form a set", by["nRangerA"]["set"] == by["gotSpores"]["set"] == "given in cf_ranger",
          (by["nRangerA"]["set"], by["gotSpores"]["set"]))
    fam = next(f for f in out_["families"] if f["name"].startswith("PQ "))
    check("quest sets: families are listed quests first and carry unused named slots",
          out_["families"][0]["kind"] == "quests" and fam["named_unused"][0]["names"] == ["SPARE_RING"], out_["families"])


def test_compiler_platform(tmp, mod, out, rep):
    """The official compiler on each platform: a compiler that dies, a file that can't run, file-name case. The
    stand-in compilers are small Python programs started through h.fake_compiler's launcher."""
    import nwn_compile
    d = os.path.join(tmp, "plat_comp"); src = os.path.join(d, "src"); outd = os.path.join(d, "out")
    os.makedirs(src); os.makedirs(outd)
    w(src, "broken.nss", "void main() { this is broken }\n")
    name = "compiler: one killed with no output (macOS Gatekeeper) is an error with a quarantine hint, never 'all compile'"
    if os.name == "nt":
        skip(name, "Windows has no SIGKILL to end the stand-in compiler with")
    else:
        killed = h.fake_compiler(d, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n", "killed")
        r = nwn_compile.compile_module(src, killed)
        check(name, r["status"] == "error" and "quarantine" in r.get("reason", "") and not r["results"], r)
    silent = h.fake_compiler(d, "import sys\nsys.exit(1)\n", "silent")
    r = nwn_compile.compile_files(src, ["broken"], outd, silent)
    check("compiler: a failed run with no error line (compile_files) is an error, never 'compiled'",
          r["status"] == "error" and "exited with code 1" in r.get("reason", ""), r)
    errline = h.fake_compiler(d, "import os, sys\nprint('E [00:00] ' + os.path.join(sys.argv[-1], 'broken.nss') + "
                                 "': syntax error')\nsys.exit(1)\n", "errline")
    r = nwn_compile.compile_module(src, errline)
    check("compiler: exit code 1 with a script error line is still a normal run that names the script",
          r["status"] == "ran" and r["failed"] == 1 and r["results"].get("broken"), r)
    with mock.patch.dict(os.environ, NWN_SCRIPT_COMP=silent):
        check("compiler: a compiler path that was given but is missing is not replaced by another one",
              nwn_compile.find_compiler(os.path.join(d, "nope")) is None)
    name = "compiler: a file without execute permission is not used, and the reason says chmod +x"
    if os.name == "nt":
        skip(name, "Windows has no execute bit: any existing file can be started")
    else:
        plain = os.path.join(d, "nwn_script_comp"); w(d, "nwn_script_comp", "#!/bin/sh\nexit 0\n")
        os.chmod(plain, 0o644)
        reason = nwn_compile.compile_module(src, plain).get("reason", "")
        check(name, nwn_compile.find_compiler(plain) is None and "chmod +x" in reason, reason)
    # writes <name>.ncs keeping the case of the .nss it was given, like a compiler on a case-sensitive disk might
    namer = h.fake_compiler(d, "import os, sys\na = sys.argv[1:]; o = a[a.index('-d') + 1]\n"
                               "[open(os.path.join(o, os.path.basename(f)[:-4] + '.ncs'), 'wb').write(b'NCS') "
                               "for f in a if f.endswith('.nss')]\n", "namer")
    mixed = os.path.join(d, "mixed"); os.makedirs(mixed); outm = os.path.join(d, "outm"); os.makedirs(outm)
    w(mixed, "MyScript.nss", "void main() {}\n")
    r = nwn_compile.compile_files(mixed, ["myscript", "gone"], outm, namer)
    check("compiler: an edited script whose file name has capitals is compiled to <name>.ncs; a missing one is listed",
          r["written"] == ["myscript"] and r["missing"] == ["gone"] and
          os.listdir(outm) == ["myscript.ncs"], (r, os.listdir(outm)))


def test_dashboard_platform(tmp, mod, out, rep):
    """Dashboard behaviour that differs by platform or by what the user typed: tools, game folder, settings,
    symbolic links, request errors, start-up errors, version, caches."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    import nwn_dashboard as D
    a = os.path.basename(out)
    # -- external tools: a macOS .app bundle is a folder, started with `open -a`
    app = os.path.join(tmp, "Viewer.app"); os.makedirs(os.path.join(app, "Contents"))
    s_ = D.settings(); s_["external_tools"] = [dict(name="Viewer", path=app, exts=[])]; D.save_settings(s_)
    started = []
    target = os.path.join(mod, "town.are")
    # only the dashboard's own view of sys and subprocess changes: nothing is really started
    no_start = h.Proxy(subprocess, Popen=lambda argv, **kw: started.append(argv))
    with mock.patch.object(D, "subprocess", no_start):
        with mock.patch.object(D, "sys", h.Proxy(sys, platform="darwin")):
            D.open_in_tool("Viewer", target)
        with mock.patch.object(D, "sys", h.Proxy(sys, platform="linux")):
            try:
                D.open_in_tool("Viewer", target); refused = False
            except ValueError:
                refused = True
    check("tools: on macOS a .app tool is started with /usr/bin/open -a; elsewhere a folder is refused",
          started == [["/usr/bin/open", "-a", app, os.path.abspath(target)]] and refused, (started, refused))
    name = "tools: a program file without execute permission is refused with a chmod hint"
    if os.name == "nt":
        skip(name, "Windows has no execute bit (a tool there must be an .exe)")
    else:
        prog = os.path.join(tmp, "viewer_noexec"); w(tmp, "viewer_noexec", "#!/bin/sh\n"); os.chmod(prog, 0o644)
        s_["external_tools"] = [dict(name="NoExec", path=prog, exts=[])]; D.save_settings(s_)
        try:
            D.open_in_tool("NoExec", target); msg = ""
        except ValueError as ex:
            msg = str(ex)
        check(name, "chmod +x" in msg, msg)
    s_["external_tools"] = []; D.save_settings(s_)

    # -- game install detection: Steam and Beamdog Client layouts, only folders with data/ and lang/
    home = os.path.join(tmp, "home"); pf = os.path.join(tmp, "pf86")
    if sys.platform == "darwin":
        steam = os.path.join(home, "Library", "Application Support", "Steam", "steamapps", "common")
        bdc = os.path.join(home, "Library", "Application Support", "Beamdog Client")
    elif os.name == "nt":
        steam = os.path.join(pf, "Steam", "steamapps", "common")
        bdc = os.path.join(home, "AppData", "Roaming", "Beamdog Client")
    else:
        steam = os.path.join(home, ".local", "share", "Steam", "steamapps", "common")
        bdc = os.path.join(home, ".config", "Beamdog Client")
    steam_root = os.path.join(steam, "Neverwinter Nights")
    lib = os.path.join(tmp, "BeamdogLib")
    for p in (os.path.join(steam_root, "data"), os.path.join(steam_root, "lang"), os.path.join(lib, "00785", "data"),
              os.path.join(lib, "00785", "lang"), os.path.join(lib, "00829", "data"), bdc):
        os.makedirs(p, exist_ok=True)
    with open(os.path.join(bdc, "settings.json"), "w") as fh:
        json.dump({"folders": [lib]}, fh)
    with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "ProgramFiles(x86)": pf}):
        os.environ.pop("NWN_ROOT", None)                 # patch.dict puts it back afterwards
        found = D.default_nwn_roots()
    check("game folder: Steam and Beamdog Client installs are found; a folder without lang/ is not",
          found == [steam_root, os.path.join(lib, "00785")], found)

    # -- settings warnings (shown after Save)
    good_user = os.path.join(tmp, "nwnuser"); os.makedirs(os.path.join(good_user, "modules"))
    empty = os.path.join(tmp, "plat_empty"); os.makedirs(empty)
    warn_bad = D.settings_warnings(dict(nwn_root=empty, nwn_user=empty, external_tools=[]))
    warn_ok = D.settings_warnings(dict(nwn_root=steam_root, nwn_user=good_user, external_tools=[]))
    check("settings: an install folder without data/lang and a user folder without modules/ are warned about",
          len(warn_bad) == 2 and "data and lang" in warn_bad[0] and "modules" in warn_bad[1] and warn_ok == [],
          (warn_bad, warn_ok))

    # -- a folder module holding a symbolic link: never read through
    name = "read_module_file: a symbolic link in a folder module is never followed"
    if not h.can_symlink(tmp):
        skip(name, "this system does not let the test make a symbolic link (Windows needs developer mode)")
    else:
        secret = os.path.join(tmp, "outside_secret.txt"); w(tmp, "outside_secret.txt", "SECRET")
        victim = os.path.join(mod, "town.are")
        os.rename(victim, victim + ".keep")
        os.symlink(secret, victim)
        try:
            data = D.read_module_file(a, "town.are")
        finally:
            os.remove(victim); os.rename(victim + ".keep", victim)
        check(name, data is None, data)

    # -- HTTP: request errors, CSP, version
    srv = ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def req(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, data, {"X-NWN-Token": D.TOKEN, "Content-Type": "application/json"})
        r = c.getresponse(); raw = r.read(); c.close()
        try:
            return r.status, json.loads(raw), r
        except ValueError:
            return r.status, raw, r
    try:
        code, body, _r = req("POST", "/api/analyse", {"module": os.path.join(tmp, "no_such.mod")})
        check("analyse: a module path that does not exist is a 400 at once, not a failed job",
              code == 400 and "Not found" in body.get("error", ""), (code, body))
        code, body, _r = req("POST", "/api/description", {"script": "x"})
        code2, body2, _r = req("POST", "/api/description", {"a": a, "script": 5})
        check("requests: a missing or wrongly typed field is a 400 that names it",
              code == 400 and "'a'" in body["error"] and code2 == 400 and "'script'" in body2["error"], (body, body2))
        with mock.patch.dict(D.POST_ROUTES, {"/api/description": lambda b: {}["bug"]}):
            code, body, _r = req("POST", "/api/description", {"a": a})
        check("requests: a bug (KeyError inside the toolkit) is a 500 with traceback lines, not a 'bad request'",
              code == 500 and body.get("trace"), (code, body))
        code, body, r = req("GET", "/")
        csp = r.getheader("Content-Security-Policy") or ""
        check("page: CSP also sets base-uri, form-action, frame-ancestors and object-src",
              all(x in csp for x in ("base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
                                     "object-src 'none'")), csp)
        code, body, r = req("GET", "/api/state")
        check("version: /api/state has it and the Server header names the toolkit",
              body.get("version") == D.VERSION and (r.getheader("Server") or "").startswith("NWNToolkit/"),
              (body.get("version"), r.getheader("Server")))
    finally:
        srv.shutdown(); srv.server_close()
    vdir = os.path.join(tmp, "verdir"); os.makedirs(vdir); w(vdir, "VERSION.txt", "vault 1.0 1a2b3c4\n")
    with mock.patch.object(D, "HERE", vdir):
        parts = D._version_parts()
    check("version: a release's VERSION.txt gives edition, version and commit", parts == ("vault", "1.0", "1a2b3c4"), parts)

    # -- jobs: a plain refusal shows its message without a traceback; a bug keeps the traceback
    def refuse(job):
        raise ValueError("Not found: x.mod")

    def bug(job):
        return {}["missing"]
    jobs = [D.run_job("test", refuse), D.run_job("test", bug)]
    for _ in range(100):
        if all(D.JOBS[j].status != "running" for j in jobs):
            break
        time.sleep(0.05)
    lines = [D.JOBS[j].lines for j in jobs]
    check("jobs: a ValueError shows only its message; any other error also shows the traceback",
          lines[0] == ["ERROR: Not found: x.mod"] and any("Traceback" in x or "File " in x for x in lines[1]), lines)

    # -- report.json parsed once per change; release_analysis forgets it
    p1, p2 = D.report_parts(a), D.report_parts(a)
    D.release_analysis(a)
    p3 = D.report_parts(a)
    check("caches: report.json is parsed once until it changes, and release_analysis drops the cached copy",
          p1 is p2 and p3 is not p1 and p3["summary"] == p1["summary"])

    # -- start-up: port already in use, workspace that can't be written
    import socket
    busy = socket.socket(); busy.bind(("127.0.0.1", 0)); busy.listen(1)
    port = busy.getsockname()[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = D.main(["--port", str(port), "--no-browser"])
    finally:
        busy.close()
    check("start-up: a port already in use is one plain sentence and exit code 1",
          rc == 1 and "already in use" in buf.getvalue() and "Traceback" not in buf.getvalue(), (rc, buf.getvalue()))
    blocker = os.path.join(tmp, "a_file"); w(tmp, "a_file", "x")
    with mock.patch.object(D, "WORKSPACE", os.path.join(blocker, "ws")), \
            contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = D.main(["--no-browser"])
    check("start-up: a workspace that can't be written is one plain sentence and exit code 1",
          rc == 1 and "cannot write" in buf.getvalue(), (rc, buf.getvalue()))


def test_cli_errors(tmp, mod, out, rep):
    """Command-line tools: a missing input is one line and exit code 2; --help is real help and exit code 0."""
    env = dict(os.environ, NWN_WORKSPACE=os.path.join(tmp, "cli_ws"), PYTHONIOENCODING="utf-8")
    missing = os.path.join(tmp, "no_such_module.mod")
    runs = {"nwn_analyse.py": [missing], "nwn_diff.py": [missing, missing], "nwn_archive.py": ["archive", missing]}
    bad = {}
    for tool, args in runs.items():
        r = subprocess.run([sys.executable, os.path.join(ROOT, tool)] + args, capture_output=True, text=True, env=env,
                           timeout=120)
        text = (r.stdout + r.stderr).strip()
        if r.returncode != 2 or len(text.splitlines()) != 1 or "Traceback" in text:
            bad[tool] = (r.returncode, text[-300:])
        h = subprocess.run([sys.executable, os.path.join(ROOT, tool), "--help"], capture_output=True, text=True,
                           env=env, timeout=120)
        if h.returncode != 0 or "usage" not in h.stdout.lower() or len(h.stdout.splitlines()) < 5:
            bad[tool + " --help"] = (h.returncode, h.stdout[-300:])
    check("command line: a missing input is one line with exit code 2; --help prints help and exits 0", not bad, bad)
    # one check per tool: a missing input is one plain line, exit code 2, no traceback
    empty = os.path.join(tmp, "cli_empty_folder")
    os.makedirs(empty, exist_ok=True)
    more = [("nwn_quickscan.py", [missing], "a module that does not exist"),
            ("nwn_logs.py", [os.path.join(tmp, "no_such_analysis")], "an analysis folder that does not exist"),
            ("nwn_logs.py", [empty], "a folder that is not an analysis"),
            ("nwn_logs.py", ["--logs", os.path.join(tmp, "no_such_logs")], "a --logs folder that does not exist"),
            ("nwn_housekeep.py", [os.path.join(tmp, "no_such_modules_folder")], "a modules folder that does not exist"),
            ("nwn_audit.py", [os.path.join(tmp, "no_such_analysis"), os.path.join(tmp, "no_build"), "x_clean"],
             "an analysis folder that does not exist"),
            ("nwn_audit.py", [empty, os.path.join(tmp, "no_build"), "x_clean"], "a folder that is not an analysis"),
            ("nwn_audit.py", [out, os.path.join(tmp, "no_build"), "x_clean"], "a build folder without changes.json"),
            ("nwn_dashboard.py", ["--export", "no_such_analysis"], "--export of an analysis that does not exist")]
    for tool, args, what in more:
        r = subprocess.run([sys.executable, os.path.join(ROOT, tool)] + args, capture_output=True, text=True, env=env,
                           timeout=120)
        text = (r.stdout + r.stderr).strip()
        check(f"command line: {tool} with {what} prints one line and exits 2",
              r.returncode == 2 and len(text.splitlines()) == 1 and "Traceback" not in text, (r.returncode, text[-300:]))
    check("command line: nwn_audit.py on a mistyped folder leaves no empty index.sqlite behind",
          not os.path.exists(os.path.join(empty, "index.sqlite")))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_logs.py"), "--logs", empty], capture_output=True,
                       text=True, env=env, timeout=120)
    text = (r.stdout + r.stderr).strip()
    check("command line: nwn_logs.py on an existing folder without log files says so and exits 0",
          r.returncode == 0 and "No log files found" in text and len(text.splitlines()) == 1 and "Traceback" not in text,
          (r.returncode, text[-300:]))
    v = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_analyse.py"), "--version"], capture_output=True,
                       text=True, env=env, timeout=120)
    check("command line: nwn_analyse.py --version names the toolkit version",
          v.returncode == 0 and "NWN Module Toolkit" in v.stdout, (v.returncode, v.stdout, v.stderr[-300:]))


# ================================================================ smaller modules on fixtures of their own
# ---------------------------------------------------------------- factions, settings, logs, archive, quick scan
def repute(names, parents=None):
    """A repute.fac GFF root: FactionList of `names` (parents: {name: parent id}), one RepList row."""
    parents = parents or {}
    return root("FAC ", FactionList=(LST, [st(0, FactionName=(X, nm), FactionGlobal=(DW, 0),
                                               FactionParentID=(DW, parents.get(nm, 0xFFFFFFFF))) for nm in names]),
                RepList=(LST, [st(0, FactionID1=(DW, 0), FactionID2=(DW, 1), FactionRep=(DW, 0))]))


FACTION_SCRIPTS = {
    # a ChangeFaction call inside a block comment must not be counted; the real call on line 6 must (with its line)
    "fac_commented": 'void main()\n{\n    /* old code:\n    ChangeFaction(OBJECT_SELF, GetObjectByTag("OLD_HOLDER"));\n    */\n'
                     '    ChangeFaction(OBJECT_SELF, GetObjectByTag("REAL_HOLDER"));\n}\n',
    # a DelayCommand-in-loop inside a block comment (nwn_perf reads comment-free code)
    "hb_commented": 'void main()\n{\n    /* int i; for (i = 0; i < 3; i++) DelayCommand(1.0, SpeakString("x")); */\n}\n',
    # a tag that exists only on a blueprint (nothing places or spawns it)
    "find_bp_only": 'void main()\n{\n    object o = GetObjectByTag("BP_ONLY_TAG");\n}\n',
    # a token system (slot helpers), so the variable audit has a token row
    "inc_tok": 'const int QS_STR_LEN = 20;\nconst int QS_ANTS = 3;\n'
               'void SetQToken(object oPC, int iPos = 0, string s = "1") { SetLocalString(oPC, "sQ", s); }\n'
               'string GetQToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "sQ"), iPos, 1); }\n',
    "q_ants": '#include "inc_tok"\nvoid main()\n{\n    SetQToken(GetPCSpeaker(), QS_ANTS, "1");\n}\n',
    # more module switches than the 400 cap (one per line)
    "many_switches": "void main()\n{\n" + "".join(f'    SetModuleSwitch("SW_{i:03d}", TRUE);\n' for i in range(450)) + "}\n",
}


def build_faction_module(mod, hak):
    """A tiny module (its own repute.fac, a hak with another repute.fac) in folder `mod`, the hak at path `hak`."""
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    for nm, src in FACTION_SCRIPTS.items():
        w(mod, nm + ".nss", src)
        w(mod, nm + ".ncs", stand_in_ncs(nm))
    g("module.ifo", root("IFO ", Mod_Name=loc("Factions"), Mod_Entry_Area=(R, "town"), Mod_OnModLoad=(R, "many_switches"),
                         Mod_OnClientEntr=(R, "fac_commented"), Mod_OnHeartbeat=(R, "hb_commented"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "town"))])))
    # the module's own repute.fac: "ModFac" with parent 0 (the PC faction) - the hak's copy must win
    g("repute.fac", repute(["PC", "Hostile", "Commoner", "Merchant", "Defender", "ModFac"], {"ModFac": 0}))
    g("bp_only.utp", root("UTP ", TemplateResRef=(R, "bp_only"), Tag=(X, "BP_ONLY_TAG"), LocName=loc("Only a blueprint")))
    g("town.are", root("ARE ", Name=loc("Town"), Tag=(X, "TOWN"), ResRef=(R, "town"), Tileset=(R, "tcn01")))
    g("town.git", root("GIT ", **{"Creature List": (LST, [st(4, TemplateResRef=(R, "holder"), Tag=(X, "REAL_HOLDER"),
                                                              FirstName=loc("Holder"), FactionID=(DW, 5))])}))
    n.write_erf(hak, [("repute", "fac", n.write_gff(repute(["PC", "Hostile", "Commoner", "Merchant", "Defender", "HakFac"],
                                                                {"HakFac": 0})))], file_type="HAK ")


def index_faction_module(mod, hak, out):
    """Index (no analysis) and return the index opened read-only."""
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, [hak], [], None, out, False, False)
    return n.sqlite_ro(os.path.join(out, "index.sqlite"))


def varaudit_without_graph(db):
    import nwn_varaudit
    try:
        va = nwn_varaudit.run(db, None)
        crash = None
    except Exception as ex:  # noqa
        va, crash = None, repr(ex)
    tag = next((t for t in (va or {}).get("tags", []) if t["tag"] == "BP_ONLY_TAG"), None)
    check("varaudit: run(db, g=None) does not crash when a tag exists only on blueprints",
          crash is None and tag is not None and tag["status"] == "blueprint_only", crash or tag)
    toks = (va or {}).get("tokens", [])
    check("varaudit: token rows no longer carry the always-None prefix",
          toks and not any("prefix" in t for t in toks), toks)


def factions_load_order(db):
    import nwn_factions
    r = nwn_factions.analyse(db)
    names = r.get("names", {})
    check("factions: a hak's repute.fac wins over the module's own (the game's order)",
          names.get(5) == "HakFac", names)
    f5 = next((f for f in r["factions"] if f["id"] == 5), {})
    check("factions: a parent faction of 0 (PC) is read as PC, not as no parent",
          f5.get("parent") == 0 and f5.get("parent_name") == "PC", f5)
    calls = [(s["line"], s["detail"]) for s in r["scripts"] if s["script"] == "fac_commented"]
    check("factions: a call inside a /* block comment */ is not counted; the real call keeps its line",
          calls == [(6, "the faction of 'REAL_HOLDER'")], calls)


def perf_ignores_comments(db):
    import nwn_perf
    r = nwn_perf.run(db, None, {"script:hb_commented"})
    check("perf: a DelayCommand loop inside a /* block comment */ is not reported (lex_nss strips comments)",
          not any(f["category"] == "perf_delay_in_loop" for f in r["findings"]), r["findings"])


def questsets_trailing_digits():
    import nwn_questsets as QS
    # 70 Q_ names: "Q_" alone is shared by too many (over 60) to be a set, so the next prefix must do - and for
    # Q_RATS1..3 that is the word before the trailing digits
    many = [f"Q_W{i}X" for i in range(67)]
    sets = QS.name_sets(["Q_RATS1", "Q_RATS2", "Q_RATS3", "OTHER_A", "OTHER_B", "ALONE"] + many)
    check("questsets: trailing digits are the varying part, so Q_RATS1..3 form the set Q_RATS*",
          sets["Q_RATS1"] == sets["Q_RATS3"] == "Q_RATS*" and sets[many[0]] is None, {k: v for k, v in sets.items() if "W" not in k})
    check("questsets: the usual word-prefix sets still work", sets["OTHER_A"] == "OTHER_*" and sets["ALONE"] is None, sets)


def modsettings_cap(db):
    import nwn_modsettings
    ms = nwn_modsettings.module_summary(db, {})
    check("modsettings: the 400 cap on script settings stops the whole scan, not just one line",
          len(ms["script_sets"]) == 400, len(ms["script_sets"]))


def logs_without_index(tmp):
    import nwn_logs
    d = os.path.join(tmp, "logs_an"); os.makedirs(d)
    with open(os.path.join(d, "report.json"), "w") as fh:
        fh.write("{not json")
    try:
        m = nwn_logs.LogMonitor(d, paths=[])
        ctx = m.ctx; err = None
    except Exception as ex:  # noqa
        ctx, err = "raised", repr(ex)
    check("logs: a broken report.json gives no context instead of a ValueError", ctx is None, err or ctx)
    rep = dict(scripts=[dict(name="s1")], breadcrumbs={}, labels={}, files=[])
    with open(os.path.join(d, "report.json"), "w") as fh:
        json.dump(rep, fh)
    m = nwn_logs.LogMonitor(d, paths=[])
    check("logs: a missing index.sqlite is not created (read-only open) and the report still gives context",
          not os.path.exists(os.path.join(d, "index.sqlite")) and m.ctx and "s1" in m.ctx["scripts"],
          (os.listdir(d), m.ctx and list(m.ctx)))


def archive_resume(tmp, out):
    import nwn_archive as A
    d = os.path.join(tmp, "arc"); shutil.copytree(out, d)
    # the index alone is not an analysis: add a report and a reports/ folder, as a finished run leaves them
    with open(os.path.join(d, "report.json"), "w") as fh:
        fh.write('{"summary": {"module_name": "Factions", "module_path": "x", "indexed_at": "2026"}}')
    os.makedirs(os.path.join(d, "reports"))
    with open(os.path.join(d, "reports", "issues.csv"), "w") as fh:
        fh.write("a,b\n1,2\n")
    before = {}
    for root_, _ds, fs in os.walk(d):
        for f_ in fs:
            before[os.path.relpath(os.path.join(root_, f_), d)] = open(os.path.join(root_, f_), "rb").read()
    A.archive(d)
    zp = os.path.join(d, A.ZIP)
    sha = A._sha256(zp)
    # a half-done Archive: the zip and archive.json are written but a packed file could not be removed
    with zipfile.ZipFile(zp) as z:
        z.extract("report.json", d)
    try:
        A.archive(d); err = None
    except Exception as ex:  # noqa
        err = repr(ex)
    check("archive: Archive finishes removing leftovers when archive.json already exists (zip untouched)",
          err is None and not os.path.exists(os.path.join(d, "report.json")) and A._sha256(zp) == sha, err)
    # a leftover the zip does not hold is never removed
    with open(os.path.join(d, "report.json"), "w") as fh:
        fh.write("{}")
    try:
        A.archive(d); err = None
    except (OSError, ValueError) as ex:
        err = str(ex)
    check("archive: a leftover that is not in the zip is refused, not removed",
          err and os.path.isfile(os.path.join(d, "report.json")) and A._sha256(zp) == sha, err)
    os.remove(os.path.join(d, "report.json"))
    # a half-done Unpack: some files are already in place and match the zip
    with zipfile.ZipFile(zp) as z:
        z.extract("report.json", d)
        z.extract("reports/issues.csv", d)
    try:
        A.unarchive(d); err = None
    except Exception as ex:  # noqa
        err = repr(ex)
    after = {}
    for root_, _ds, fs in os.walk(d):
        for f_ in fs:
            after[os.path.relpath(os.path.join(root_, f_), d)] = open(os.path.join(root_, f_), "rb").read()
    check("archive: Unpack resumes when files already in place match the zip, and gives back every byte",
          err is None and after == before, err or sorted(set(before) ^ set(after))[:10])
    # a file in place that differs from the zip still stops Unpack
    A.archive(d)
    with open(os.path.join(d, "report.json"), "w") as fh:
        fh.write("{}")
    try:
        A.unarchive(d); err = None
    except ValueError as ex:
        err = str(ex)
    check("archive: Unpack refuses a file in place that differs from the zip", err and "differs" in err and os.path.isfile(zp), err)


def run_lock_release(tmp):
    import nwn_progress
    d = os.path.join(tmp, "lockdir")
    fh = nwn_progress.acquire_run_lock(d)
    nwn_progress.release_run_lock(fh, d)
    kept = os.path.exists(os.path.join(d, nwn_progress.RUN_LOCK))
    held = nwn_progress.run_lock_held(d)
    fh2 = nwn_progress.acquire_run_lock(d)
    nwn_progress.release_run_lock(fh2, d)
    check("progress: release keeps .run.lock (only unlocks), the lock is free and can be taken again", kept and not held)


def housekeep_cache_and_relative_undo(tmp):
    import nwn_housekeep as HK
    folder = os.path.join(tmp, "mods"); os.makedirs(folder)
    ws = os.path.join(tmp, "hkws"); os.makedirs(ws)
    old = time.time() - 30 * 86400
    for nm, data in (("A.mod", b"MOD AAA"), ("B.mod", b"MOD BBB"), ("C.mod", b"MOD AAA")):
        p = os.path.join(folder, nm)
        open(p, "wb").write(data)
        os.utime(p, ns=(int(old) * 10 ** 9, int(old) * 10 ** 9))
    HK.scan(folder, ws, keep_newest=1)
    # rewrite B with the same size, one second later: its hash must be recomputed. (A whole second, because file
    # systems keep times in different steps: NTFS 100 ns, FAT 2 s, ext4/APFS 1 ns.)
    p = os.path.join(folder, "B.mod")
    open(p, "wb").write(b"MOD AAA")
    os.utime(p, ns=((int(old) + 1) * 10 ** 9, (int(old) + 1) * 10 ** 9))
    r = HK.scan(folder, ws, keep_newest=1)
    shas = {f["name"]: f["sha256"] for f in r["files"]}
    check("housekeep: the hash cache sees a same-size rewrite with a new modification time (mtime in the key)",
          shas["B.mod"] == shas["A.mod"], shas)
    b = HK.move(folder, ["C.mod"], ws)
    man = os.path.join(b["dest"], "manifest.json")
    cwd = os.getcwd()
    os.chdir(b["dest"])
    try:
        HK.undo("manifest.json", ws)        # a relative manifest path
    finally:
        os.chdir(cwd)
    rec = next(x for x in HK.list_batches(ws) if os.path.abspath(x["manifest"]) == os.path.abspath(man))
    check("housekeep: undo given a relative manifest path still marks the batch undone in batches.json",
          rec.get("undone") is True and os.path.isfile(os.path.join(folder, "C.mod")), rec)


def quickscan_previous_analysis(tmp):
    import nwn_quickscan
    ws = os.path.join(tmp, "qsws", "an1"); os.makedirs(ws)
    upper = os.path.join(tmp, "Mods", "MyMod")
    db = sqlite3.connect(os.path.join(ws, "index.sqlite"))
    db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('module_path', ?), ('indexed_at', '2026')", (upper,))
    db.commit(); db.close()
    same = nwn_quickscan._previous_analysis(os.path.dirname(ws), upper)
    other = nwn_quickscan._previous_analysis(os.path.dirname(ws), upper.lower())
    expect_match = os.path.normcase("A") == os.path.normcase("a")       # Windows: case-insensitive paths
    check("quickscan: the previous-analysis match uses os.path.normcase (case matters on Linux)",
          same and same["name"] == "an1" and (bool(other) == expect_match), (same, other))


def quickscan_event_names(tmp):
    """Module events are named as the toolset shows them (OnModuleLoad ...), with the same map as dashboard.html."""
    import re
    import nwn_quickscan
    m2 = os.path.join(tmp, "qs_events"); os.makedirs(m2)
    w(m2, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Ev"), Mod_OnModLoad=(R, "zz_gone_load"),
                                          Mod_OnActvtItem=(R, "zz_gone_act"), Mod_OnZzNew=(R, "zz_gone_new"))))
    r = nwn_quickscan.scan(m2)
    texts = " | ".join(x["text"] for x in r["warnings"])
    check("quickscan: missing event scripts are reported under the toolset's event names",
          "module event OnModuleLoad runs 'zz_gone_load'" in texts and "module event OnActivateItem runs 'zz_gone_act'" in texts
          and "module event OnZzNew runs" in texts, texts)
    out = nwn_quickscan.format_text(r)
    check("quickscan: the text report lists events by their toolset names", "OnModuleLoad=zz_gone_load" in out, out)
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    js = re.search(r"const MOD_EVENTS = \{(.*?)\};", html, re.S).group(1)
    page = dict(re.findall(r'(\w+): "(\w+)"', js))
    check("quickscan: its event-name map is the same as the dashboard page's", page == nwn_quickscan.MOD_EVENTS,
          set(page.items()) ^ set(nwn_quickscan.MOD_EVENTS.items()))


def quickscan_event_sources(tmp):
    """A module event script that the base game or a hak supplies (x2_mod_def_act is the game's default OnActivateItem)
    gets no note; one found nowhere is a warning, and an error once the game install was read. A tester saw
    x2_mod_def_act flagged although it is a valid base-game script."""
    import nwn_quickscan
    m3 = os.path.join(tmp, "qs_src"); os.makedirs(m3)
    hk = os.path.join(tmp, "qs_src_hak"); os.makedirs(hk)
    w(hk, "ev_from_hak.ncs", b"NCS V1.0")
    h.pack_hak(hk, os.path.join(tmp, "qs_src.hak"))
    w(m3, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Src"), Mod_OnActvtItem=(R, "x2_mod_def_act"),
                                          Mod_OnModLoad=(R, "ev_from_hak"), Mod_OnHeartbeat=(R, "zz_nowhere"))))

    def notes(**kw):
        r = nwn_quickscan.scan(m3, haks=[os.path.join(tmp, "qs_src.hak")], **kw)
        return {x["text"].split("'")[1]: x["level"] for x in r["warnings"] if x["text"].startswith("module event")}
    got = notes()
    check("quickscan: no install folder - a base-game name and a hak's script get no note; one found nowhere is a warning",
          got == {"zz_nowhere": "warning"}, got)
    game = os.path.join(tmp, "qs_game")
    h.fake_nwn_root(game, ["x2_mod_def_act.ncs", "nw_c2_default1.ncs"])
    got = notes(nwn_root=game)
    check("quickscan: with the game install read, x2_mod_def_act is the base game's; one found nowhere is an error",
          got == {"zz_nowhere": "error"}, got)


def neutral_names():
    """Examples and test labels use neutral names (gen_c_*, no personal names)."""
    import re
    import nwn_varaudit
    src = open(nwn_varaudit.__file__, encoding="utf-8").read()
    check("varaudit: the generic conversation-script example is gen_c_*",
          "gen_c_dobounty" in src and not re.search(r"\btt" r"c_", src))
    # built from pieces, so this file does not itself contain the names it looks for
    names_re = r"\bttc" r"_c_|\bK" r"en\b|Spor" r"axis"
    bad = []
    for root_, _d, files in os.walk(h.HERE):
        if "__pycache__" in root_:
            continue
        for fn in files:
            if fn.endswith((".py", ".md", ".nss")):
                p = os.path.join(root_, fn)
                for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                    if re.search(names_re, line) and "re.search(" not in line:
                        bad.append(f"{os.path.relpath(p, h.HERE)}:{i}")
    check("tests: no example names with the old builder prefix, and no owner's name in test labels", not bad, bad)
    # Code, tests and the dashboard read as a general NWN tool: no names copied from one persistent world (its module
    # name, include and script prefixes, places and NPCs). Markdown is left out (a world-specific guide may exist);
    # so are the two developer-only release files, which must name these words to catch them.
    world_re = re.compile("|".join([r"my" r"stara", r"\bda" r"mr", r"\bqt" r"ut", r"\btt" r"c_", r"\bzz" r"g_",
                                     r"na" r"ssus", r"za" r"imbak", r"fen" r"hound", r"ylar" r"uam", r"thy" r"atis",
                                     r"karam" r"eikos", r"alph" r"atia", r"mir" r"ros\b", r"\bcf_ca" r"p"]), re.I)
    skip = {"make_release.py", os.path.join("tests", "dev_test_release.py")}
    bad_w = []
    # the whole toolkit (h.ROOT), not only the tests folder
    for root_, dirs, files in os.walk(h.ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "nwn_workspace")]
        for fn in files:
            p = os.path.join(root_, fn)
            if fn.endswith((".py", ".html")) and os.path.relpath(p, h.ROOT) not in skip:
                for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                    if world_re.search(line):
                        bad_w.append(f"{os.path.relpath(p, h.ROOT)}:{i}")
    check("code, tests and dashboard use neutral example names, not one world's", not bad_w, bad_w[:40])


# ---------------------------------------------------------------- the official compiler, fix texts
def compile_log_and_leftovers(tmp):
    import nwn_compile
    m = nwn_compile.LINE_RE.search(r"E [12:00:01] C:\mods\src\foo.nss: Undefined identifier [0.4ms]")
    check("compile: LINE_RE accepts a Windows drive letter in the path",
          m and m.group("path").endswith("foo.nss") and m.group("msg") == "Undefined identifier", m and m.groupdict())
    m = nwn_compile.LINE_RE.search("E [12:00:01] /tmp/x/bar.nss: oops")
    check("compile: LINE_RE still matches a plain path", m and m.group("path") == "/tmp/x/bar.nss" and m.group("msg") == "oops")
    # a fake compiler that writes nothing: a leftover .ncs from an earlier run must not count as written
    fake = h.fake_compiler(os.path.join(tmp, "comp_quiet"), "pass\n")
    folder = os.path.join(tmp, "comp_src"); out = os.path.join(tmp, "comp_out"); os.makedirs(folder); os.makedirs(out)
    w(folder, "a.nss", "void main() {}\n")
    w(out, "a.ncs", b"old")
    r = nwn_compile.compile_files(folder, ["a"], out, compiler=fake)
    check("compile: a leftover .ncs is not reported as written", r["status"] == "ran" and r["written"] == [], r)
    # a module that can't be opened: no temp folder is left behind and the result says why
    bad = os.path.join(tmp, "notamodule.mod"); w(tmp, "notamodule.mod", b"junk")
    tdir = tempfile.gettempdir()
    before = {f for f in os.listdir(tdir) if f.startswith("nwn_comp")}
    r = nwn_compile.compile_module(bad, compiler=fake)
    after = {f for f in os.listdir(tdir) if f.startswith("nwn_comp")}
    check("compile: an unreadable module leaves no temp folder and returns status error",
          r["status"] == "error" and after == before, (r.get("status"), after - before))


def compile_skips_links(tmp):
    """A folder module's script that is a link is not copied for the compiler (links are never followed)."""
    import nwn_compile
    fake = h.fake_compiler(os.path.join(tmp, "comp_links"), "pass\n")
    mod = os.path.join(tmp, "linkmod"); os.makedirs(mod)
    w(mod, "real_one.nss", "void main() {}\n")
    outside = os.path.join(tmp, "outside_secret.nss")
    w(tmp, "outside_secret.nss", "void main() {}\n")
    name = "compile: a folder module's linked script is not copied for the compiler"
    if not h.can_symlink(tmp):
        skip(name, "this system does not let the test make a symbolic link (Windows needs developer mode)")
        return
    os.symlink(outside, os.path.join(mod, "linked.nss"))
    r = nwn_compile.compile_module(mod, compiler=fake)
    check(name, r["status"] == "ran" and set(r["results"]) == {"real_one"}, r)


def fix_texts():
    import nwn_fixes
    st_ = nwn_fixes.fix_for({"category": "ncs_stale"})
    check("fixes: ncs_stale says to compare first (Show compiled code) and when to leave it",
          "Show compiled code" in st_ and "Compare first" in st_ and "leave it" in st_, st_)
    generic = nwn_fixes.fix_for({"category": "zz_no_such_category"})
    for cat, word in (("2da_row_label", "Renumber"), ("symlink_skipped", "links are never followed")):
        f = nwn_fixes.fix_for({"category": cat})
        check(f"fixes: {cat} has its own fix", f != generic and word in f, f)
    for cat in ("tlk_not_found", "base_game_not_found"):
        f = nwn_fixes.fix_for({"category": cat})
        check(f"fixes: {cat} writes paths with forward slashes", "\\" not in f and "/" in f, f)
    doc = nwn_index._nwn_folders.__doc__
    check("index: the hak/tlk folder search names the Windows/macOS and Linux user folders",
          "macOS" in doc and "~/.local/share" in doc, doc)



# ---------------------------------------------------------------- edits overlay, refactor, facts, diff
# the same script and the same blueprint live in the module AND in a hak; the game runs the hak's copy
MOD_SHARED = 'void main()\n{\n    SpeakString("module copy");\n}\n'
HAK_SHARED = 'void main()\n{\n    SpeakString("hak copy");\n}\n'
SHARED_SCRIPTS = {
    "shared": MOD_SHARED,
    "inc_a": "int A() { return 1; }\n",
    "inc_b": "int B() { return 2; }\n",
    "two_inc": '#include "inc_a"\n#include "inc_b"\nvoid main()\n{\n    int x = A() + B();\n}\n',
    "mod_load": 'void main()\n{\n    ExecuteScript("shared", OBJECT_SELF);\n}\n',
}


def build_shared_module(mod, hak):
    """A module whose script 'shared' and blueprint shared.uti are also in a hak (the hak's copies win)."""
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    for nm, src in SHARED_SCRIPTS.items():
        w(mod, nm + ".nss", src)
        if "main" in src:
            w(mod, nm + ".ncs", stand_in_ncs(nm))
    g("module.ifo", root("IFO ", Mod_Name=loc("Shared Copies"), Mod_Entry_Area=(R, "town"), Mod_OnModLoad=(R, "mod_load"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "town"))])))
    g("town.are", root("ARE ", Name=loc("Town"), Tag=(X, "TOWN"), ResRef=(R, "town"), Tileset=(R, "tcn01")))
    g("town.git", root("GIT ", **{"Creature List": (LST, []), "Placeable List": (LST, [])}))
    g("shared.uti", root("UTI ", **item_fields("shared", "MODULE_TAG", "Shared Item")))
    g("plain.uti", root("UTI ", **item_fields("plain", "PLAIN", "Plain Item")))
    n.write_erf(hak, [("shared", "nss", HAK_SHARED.encode("cp1252")), ("shared", "ncs", stand_in_ncs("hakshared")),
                      ("shared", "uti", n.write_gff(root("UTI ", **item_fields("shared", "HAK_TAG", "Shared Item"))))],
                file_type="HAK ")


# ---------------------------------------------------------------- nwn_edit
def edits_overlay(out):
    """Saving over a deleted file, discarding (the copy is kept), and the 'apply' command line."""
    import nwn_edit
    ed = nwn_edit.edits_dir(out)
    # save_text over a file marked deleted: the marker must go, or the build ignores the edit
    with open(os.path.join(ed, "inc_a.nss.deleted"), "w") as fh:
        fh.write("renamed to inc_z.nss\n")
    nwn_edit.save_text(out, "inc_a.nss", "int A() { return 3; }\n")
    le = nwn_edit.list_edits(out)
    check("edit: save_text over a deleted file removes the .deleted marker (the edit reaches the build)",
          not os.path.exists(os.path.join(ed, "inc_a.nss.deleted")) and le.get("inc_a.nss", {}).get("kind") == "edited", le)
    with open(os.path.join(ed, "plain.uti.deleted"), "w") as fh:
        fh.write("removed\n")
    nwn_edit.save_gff_json(out, "plain.uti", json.dumps(n.gff_to_json(root("UTI ", **item_fields("plain", "PLAIN2", "Plain")))))
    le = nwn_edit.list_edits(out)
    check("edit: save_gff_json over a deleted file removes the .deleted marker too",
          not os.path.exists(os.path.join(ed, "plain.uti.deleted")) and le.get("plain.uti", {}).get("kind") == "edited", le)
    nwn_edit.discard(out, "inc_a.nss"); nwn_edit.discard(out, "plain.uti")
    # a discarded edit is moved to edits/.history/discarded/<time>/ (restorable by hand), not deleted
    kept = os.path.join(ed, ".history", "discarded")
    found = sorted(fn for _r, _d, fs in os.walk(kept) for fn in fs) if os.path.isdir(kept) else []
    check("edit: discard keeps the dropped copies in edits/.history/discarded",
          "inc_a.nss" in found and "plain.uti" in found and not os.path.exists(os.path.join(ed, "inc_a.nss"))
          and "inc_a.nss" not in nwn_edit.list_edits(out), found)
    nwn_edit.save_text(out, "inc_a.nss", "int A() { return 4; }\n")
    nwn_edit.discard(out, "inc_a.nss")
    found = sorted(fn for _r, _d, fs in os.walk(kept) for fn in fs)
    check("edit: discarding the same file twice keeps both copies", found.count("inc_a.nss") == 2, found)
    check("edit: discard's docstring says where the copy goes", ".history/discarded" in nwn_edit.discard.__doc__)
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    fn_text = html.split("async function discardEdit(")[1].split("\n}\n")[0]
    check("dashboard.html: the discard dialog says the copy is kept, not that it can't be undone",
          fn_text.count("kept in edits/.history/discarded") == 2 and "undone" not in fn_text, fn_text[:600])
    # the "apply" command line: a mistyped folder must not get a new empty index.sqlite
    bad = os.path.join(os.path.dirname(out), "no_index_here")
    os.makedirs(bad)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_edit.py"), "apply", bad], capture_output=True, text=True)
    check("edit: 'apply' on a folder without an index fails without creating one",
          r.returncode != 0 and not os.path.exists(os.path.join(bad, "index.sqlite")), (r.returncode, r.stdout, r.stderr))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_edit.py"), "apply", bad + "_missing"], capture_output=True, text=True)
    check("edit: 'apply' on a missing folder fails and creates nothing",
          r.returncode != 0 and not os.path.exists(bad + "_missing"), (r.returncode, r.stdout, r.stderr))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_edit.py"), "apply", out], capture_output=True, text=True)
    check("edit: 'apply' on a real analysis prints the reminder", r.returncode == 0 and "clean build" in r.stdout,
          (r.returncode, r.stdout, r.stderr))


# ---------------------------------------------------------------- nwn_refactor
def refactor_markers(out):
    """Renaming a new file moves its .new marker; undo brings both back."""
    import nwn_edit
    import nwn_refactor as RF
    ed = nwn_edit.edits_dir(out)
    # a module script saved as "new" (so it carries a .new marker), then renamed: the old .new marker must go too
    nwn_edit.save_text(out, "inc_b.nss", "int B() { return 5; }\n", is_new=True)
    r = RF.find(out, "inc_b", "exact", case=True, kinds=["filename"])
    ids = [h["id"] for h in r["hits"] if h["file"] == "inc_b.nss"]
    res = RF.apply(out, "inc_b", "inc_c", ids, "exact", True, ["filename"])
    check("refactor: renaming a new file leaves no stale <old>.new marker",
          "inc_c.nss" in res["written"] and not os.path.exists(os.path.join(ed, "inc_b.nss.new"))
          and os.path.exists(os.path.join(ed, "inc_c.nss.new")) and os.path.exists(os.path.join(ed, "inc_b.nss.deleted")),
          sorted(os.listdir(ed)))
    RF.undo(out, res["id"])
    check("refactor: undo of that rename brings the old file and its .new marker back",
          os.path.isfile(os.path.join(ed, "inc_b.nss")) and os.path.isfile(os.path.join(ed, "inc_b.nss.new"))
          and not os.path.exists(os.path.join(ed, "inc_c.nss")) and not os.path.exists(os.path.join(ed, "inc_b.nss.deleted")),
          sorted(os.listdir(ed)))
    nwn_edit.discard(out, "inc_b.nss")


# ---------------------------------------------------------------- nwn_facts
def facts_winning_copy(out):
    """The facts API shows the copy the game uses (a hak's over the module's) and says which source wins."""
    import nwn_facts
    f = nwn_facts.Facts(os.path.basename(out), workspace=os.path.dirname(out))
    s = f.script("shared")
    check("facts: script shows the copy the game runs (the hak's), and says which source wins",
          s["found"] and "hak copy" in s["text"] and "module copy" not in s["text"] and s["source_of"] == "hak"
          and s.get("other_copies") and "wins" in (s.get("precedence") or ""), {k: v for k, v in s.items() if k != "text"})
    s = f.script("two_inc")
    check("facts: a script only in the module is still found", s["found"] and s["source_of"] == "module" and not s.get("other_copies"), s)
    fl = f.fields("shared.uti")
    tags = [x["value"] for x in fl["fields"] if x["path"] == "Tag"]
    check("facts: fields shows the hak's copy only (not both mixed), and says which source wins",
          tags == ["HAK_TAG"] and fl.get("source_of") == "hak" and fl.get("other_copies") and "wins" in (fl.get("precedence") or ""),
          (tags, {k: v for k, v in fl.items() if k != "fields"}))
    fl = f.fields("plain.uti")
    check("facts: fields of a module-only file", fl["found"] and fl.get("source_of") == "module", fl)
    u = f.uses("script:two_inc", limit=1)
    check("facts: uses says when its list was cut short (like who-uses)",
          u.get("truncated") is True and u.get("total", 0) >= 2 and len(u["uses"]) == 1, u)
    u = f.uses("script:two_inc")
    check("facts: uses with room for everything is not truncated", u.get("truncated") is False and u["total"] == len(u["uses"]), u)
    try:
        im = f.impact("script:shared")
    except Exception as ex:  # noqa - the index has no impact_detail table any more; this crashed
        im = repr(ex)
    det = im["impact"][0]["detail"] if isinstance(im, dict) and im.get("impact") else None
    check("facts: impact works out the chain from the graph (no impact_detail table needed)",
          isinstance(det, dict) and det.get("level") and "affected" in det and "reasons" in det, im)
    rep_level = ((f.report().get("impact") or {}).get("script:shared") or {}).get("level")
    check("facts: impact gives the level the report shows (same live set)", det and det["level"] == rep_level,
          (det and det.get("level"), rep_level))
    f.close()


# ---------------------------------------------------------------- nwn_diff
def diff_closes_and_snapshot_note(tmp, mod, out):
    import nwn_diff
    import nwn_facts
    newer = os.path.join(tmp, "mod_newer")
    shutil.copytree(mod, newer)
    w(newer, "shared.uti", n.write_gff(root("UTI ", **item_fields("shared", "CHANGED_TAG", "Shared Item"))))
    w(newer, "inc_a.nss", "int A() { return 9; }\n")
    ws, name = os.path.dirname(out), os.path.basename(out)
    # facts must be closed when the comparison fails half-way (the index would stay locked on Windows)
    closed = []
    real_close = nwn_facts.Facts.close

    def boom(pct, msg):
        if pct >= 60:
            raise RuntimeError("stop here")
    with mock.patch.object(nwn_facts.Facts, "close", lambda self: (closed.append(1), real_close(self))):
        try:
            nwn_diff.compare(mod, newer, analysis=name, workspace=ws, progress=boom); raised = False
        except RuntimeError:
            raised = True
    check("diff: the analysis index is closed even when the comparison fails", raised and closed, (raised, closed))
    # the newer side a snapshot: a changed GFF file still gets a note saying why there is no field detail
    snap = nwn_diff.take_snapshot(newer, ws)["path"]
    r = nwn_diff.compare(mod, snap)
    ch = {c["file"]: c for c in r["changes"]}
    check("diff: a changed GFF gets a note when the NEWER copy is a snapshot",
          ch.get("shared.uti", {}).get("change") == "changed" and "snapshot" in ch.get("shared.uti", {}).get("note", ""),
          ch.get("shared.uti"))
    check("diff: text changes still diff against a snapshot's text", ch.get("inc_a.nss", {}).get("lines_added") == 1, ch.get("inc_a.nss"))


# ---------------------------------------------------------------- script-description batches


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_tools_") as tmp:
        ana = h.run(analyse, tmp)
        if ana:
            mod, out, rep = ana
            h.run(test_varaudit, rep)
            for fn in (test_questsets, test_icons, test_dupdiff, test_facts, test_accept, test_housekeep, test_archive,
                       test_diff, test_perf, test_rename, test_external_tools, test_compiler_platform,
                       test_dashboard_platform, test_cli_errors):
                h.run(fn, tmp, mod, out, rep)
        # factions, settings, logs, archive, quick scan ... on a small module with a hak of its own
        fmod, fhak, fout = os.path.join(tmp, "fac_mod"), os.path.join(tmp, "fixe.hak"), os.path.join(tmp, "fac_ws", "fixe")
        build_faction_module(fmod, fhak)
        db = index_faction_module(fmod, fhak, fout)
        try:
            for fn in (varaudit_without_graph, factions_load_order, perf_ignores_comments, modsettings_cap):
                h.run(fn, db)
        finally:
            db.close()
        h.run(questsets_trailing_digits)
        for fn in (logs_without_index, run_lock_release, housekeep_cache_and_relative_undo, quickscan_previous_analysis,
                   quickscan_event_names, quickscan_event_sources, compile_log_and_leftovers, compile_skips_links, archive_resume):
            own = os.path.join(tmp, fn.__name__)          # each group its own folder: no name can clash
            os.makedirs(own)
            h.run(fn, own, fout) if fn is archive_resume else h.run(fn, own)
        h.run(fix_texts)
        h.run(neutral_names)
        # edits overlay, refactor, facts and diff on a module whose script and blueprint a hak overrides
        smod, shak, sout = os.path.join(tmp, "shared_mod"), os.path.join(tmp, "shared_hak.hak"), os.path.join(tmp, "shared_ws", "shared")
        build_shared_module(smod, shak)
        h.analyse(smod, sout, [shak])
        for fn in (edits_overlay, refactor_markers, facts_winning_copy):
            h.run(fn, sout)
        h.run(diff_closes_and_snapshot_note, os.path.join(tmp, "shared_ws"), smod, sout)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
