"""
Tests for keeping noise out of the findings (feedback: "the need to reduce false positives is quite massive - the
signal can get lost in the noise"):
  - a tag-based item script is in use, with everything it reaches, even when nothing places the item (players carry
    items for years; a tester's teleport stone script was listed as unused);
  - a problem that only sits in unused content is an info note, not an error or warning;
  - haks layered on each other (the same model/texture in two haks) is an info note; the module's own copy hidden by a
    hak stays a warning;
  - an unused item held back only by general reasons (the DMs' palette, scripts creating objects from saved names) is
    "likely in use", counted apart on the Overview and hidden on Safe to delete until asked for.
    python tests/test_noise.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import sys

import nwnlib as n  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root, loc, item_fields, w, R, X, B, DW, LST, stand_in_ncs  # noqa: E402


def script(mod, name, code):
    """A script with its compiled stand-in."""
    w(mod, name + ".nss", code)
    w(mod, name + ".ncs", stand_in_ncs(name))


def dlg(action_script):
    """A one-line conversation whose line runs action_script."""
    return root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
                EntryList=(LST, [st(0, Text=loc("Where to?"), Speaker=(X, ""), Script=(R, action_script),
                                    Quest=(X, ""), QuestEntry=(DW, 0), RepliesList=(LST, []))]),
                ReplyList=(LST, []), StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))]))


def fixture(tmp):
    """The module: one area (a door whose OnOpen names a script that is gone), a teleport stone item that nothing
    places but the item palette lists (tag port_stone -> port_stone.nss opens port_dlg, whose line runs port_go), an
    old conversation nothing uses whose line names a missing script, a cloak in the palette that a script may create
    from a saved name, and two haks: both carry the same texture, one also a copy of a module script."""
    mod = os.path.join(tmp, "mod")
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Noise"), Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]),
                         Mod_OnModLoad=(R, "mod_load"), Mod_OnActvtItem=(R, "item_act"),
                         Mod_HakList=(LST, [st(8, Mod_Hak=(X, "noise_a")), st(8, Mod_Hak=(X, "noise_b"))])))
    g("area001.are", root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    g("area001.git", root("GIT ", **{"Door List": (LST, [st(8, TemplateResRef=(R, "door1"), Tag=(X, "DOOR1"),
                                                         LocName=loc("Door"), OnOpen=(R, "gone_live"))])}))
    g("port_stone.uti", root("UTI ", **item_fields("port_stone", "port_stone", "Teleport Stone")))
    g("cloak_saved.uti", root("UTI ", **item_fields("cloak_saved", "CLOAK_SAVED", "Old Cloak")))
    g("itempalcus.itp", root("ITP ", MAIN=(LST, [st(0, NAME=(X, "Misc"), ID=(B, 1), LIST=(LST, [
        st(0, RESREF=(R, "port_stone"), NAME=(X, "Teleport Stone")), st(0, RESREF=(R, "cloak_saved"), NAME=(X, "Old Cloak"))]))])))
    script(mod, "port_stone", 'void main()\n{\n    object oPC = GetItemActivator();\n'
                              '    AssignCommand(oPC, ActionStartConversation(oPC, "port_dlg", TRUE));\n}\n')
    g("port_dlg.dlg", dlg("port_go"))
    script(mod, "port_go", 'void main()\n{\n    JumpToObject(GetWaypointByTag(GetScriptParam("DEST")));\n}\n')
    g("old_dlg.dlg", dlg("gone_dead"))                                     # nothing uses it
    # a second slot naming the same missing script, and the resref-based item scripts (run by the module's
    # ExecuteScript(GetResRef(oItem)); the wand's tag is different from its resref)
    g("area001.git", root("GIT ", **{"Door List": (LST, [
        st(8, TemplateResRef=(R, "door1"), Tag=(X, "DOOR1"), LocName=loc("Door"), OnOpen=(R, "gone_live")),
        st(8, TemplateResRef=(R, "door1"), Tag=(X, "DOOR2"), LocName=loc("Back door"), OnOpen=(R, "gone_live"))])}))
    g("wand_flame.uti", root("UTI ", **item_fields("wand_flame", "WAND_OF_FLAME", "Wand of Flame")))
    script(mod, "wand_flame", 'void main()\n{\n    ApplyEffectToObject(DURATION_TYPE_INSTANT, EffectHeal(5), OBJECT_SELF);\n}\n')
    script(mod, "item_act", 'void main()\n{\n    ExecuteScript(GetResRef(GetItemActivated()), GetItemActivator());\n}\n')
    script(mod, "mod_load", 'void main()\n{\n    object oPC = GetFirstPC();\n'
                            '    CreateItemOnObject(GetCampaignString("bank", "item"), oPC);\n}\n')
    # haks: the same texture in both (layered); hak b also carries mod_load.ncs (hides the module's own)
    ha, hb = os.path.join(tmp, "ha"), os.path.join(tmp, "hb")
    os.makedirs(ha); os.makedirs(hb)
    for d in (ha, hb):
        w(d, "wall01.tga", bytes([0, 0, 2] + [0] * 9) + bytes([1, 0, 1, 0, 24, 0x20]) + bytes(3))
    w(hb, "mod_load.ncs", stand_in_ncs("other"))
    haks = [os.path.join(tmp, "noise_a.hak"), os.path.join(tmp, "noise_b.hak")]
    h.pack_hak(ha, haks[0])
    h.pack_hak(hb, haks[1])
    return mod, haks


def main_checks(tmp):
    mod, haks = fixture(tmp)
    out = os.path.join(tmp, "an")
    rep = h.analyse(mod, out, haks=haks)
    dele = {d["node"]: d for d in rep["deletions"]}
    check("item script: port_stone.nss (the stone's tag) is in use, not listed as unused", "script:port_stone" not in dele,
          dele.get("script:port_stone"))
    check("item script: and so is what it reaches (the conversation it opens, the script its line runs)",
          "dlg:port_dlg" not in dele and "script:port_go" not in dele, [k for k in dele if "port" in k])
    check("item script: the stone blueprint itself stays Review (only the palette lists it)",
          dele.get("bp:port_stone.uti", {}).get("status") == "Review", dele.get("bp:port_stone.uti"))
    live = next((i for i in rep["issues"] if i["category"] == "missing_script" and "gone_live" in i["detail"]), None)
    dead = next((i for i in rep["issues"] if i["category"] == "missing_script" and "gone_dead" in i["detail"]), None)
    check("unused content: a missing script named in a used area stays an error", live and live["severity"] == "error", live)
    check("one issue per missing script: two doors naming gone_live are one issue with two places",
          sum(1 for i in rep["issues"] if i["category"] == "missing_script" and i["node"] == "script:gone_live") == 1 and
          live["node"] == "script:gone_live" and len(live.get("places", [])) == 2 and "used by 2 place(s)" in live["detail"],
          live)
    check("resref scripts: with ExecuteScript(GetResRef(...)) in the module, wand_flame.nss (the wand's resref) is in use",
          "script:wand_flame" not in dele, dele.get("script:wand_flame"))
    check("unused content: one named only in an unused conversation is an info note saying so",
          dead and dead["severity"] == "info" and "only in unused content" in dead["detail"], dead)
    check("summary: the count of issues lowered this way", rep["summary"].get("issues_in_unused", 0) >= 1, rep["summary"].get("issues_in_unused"))
    rc = {i["node"]: i for i in rep["issues"] if i["category"] == "resource_conflict"}
    check("haks: the same texture in two haks is an info note (layered on purpose)",
          rc.get("wall01.tga", {}).get("severity") == "info" and "layered" in rc.get("wall01.tga", {}).get("detail", ""),
          rc.get("wall01.tga"))
    check("haks: a module file a hak hides stays a warning", rc.get("mod_load.ncs", {}).get("severity") == "warning",
          rc.get("mod_load.ncs"))
    cl = dele.get("bp:cloak_saved.uti", {})
    check("likely in use: an item held back only by the palette and saved-name creation is marked general",
          cl.get("status") == "Review" and cl.get("general") is True, cl)
    check("likely in use: counted apart in the summary", rep["summary"].get("review_likely_used", 0) >= 1,
          rep["summary"].get("review_likely_used"))
    page = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: one Issues tile (errors + warnings) and the Review tile counts only what is worth a look",
          ">Issues: ${fmtN(openIssues(r, \"error\"))} errors" in page and "Unused - worth a look" in page and
          "s.review_likely_used" in page and "s.issues_in_unused" in page)
    check("page: Issues can be grouped by what is missing (one group per missing script)", '"what\'s missing": i =>' in page)
    check("page: Safe to delete hides likely-in-use items until 'show likely in use' is ticked",
          "S.delGeneral" in page and "likely in use</span>" in page and 'id="dgen"' in page)


def resref_needs_runner(tmp):
    """Without a script that runs ExecuteScript(GetResRef(...)) (here only in a comment), a script named after an item's
    resref but not its tag is not linked to the item: it stays a deletion candidate."""
    mod, haks = fixture(tmp)
    w(mod, "item_act.nss", 'void main()\n{\n    // ExecuteScript(GetResRef(GetItemActivated()), GetItemActivator());\n}\n')
    rep = h.analyse(mod, os.path.join(tmp, "an2"), haks=haks)
    check("resref scripts: no runner (only a comment) - no link, wand_flame.nss is an unused candidate",
          any(d["node"] == "script:wand_flame" for d in rep["deletions"]))


def tester_fixture(tmp):
    """A module built from a tester's report on 1.5.1 (each case is real code or a real slot from it, renamed):
      - a disease script creating "plc_invisobj" with the NEW TAG "DS_DISEASE" as CreateObject's 5th argument;
      - a conversation whose action slot runs a StartingConditional script (if_takegold) - works in the game;
      - NPCs whose OnConversation names npc_nochat, which doesn't exist (the "don't turn to talk" trick), a static
        placeable whose OnUsed names a script that doesn't exist, and a door whose OnOpen does (a real error);
      - STORE read as a string but set only as an object (sh_store:3) and as a string in no place;
      - QUESTKEY read as int, set as a string on a creature blueprint's Variables (toolset);
      - GetItemPossessedBy(oPC, "ghost_key") while the item blueprint ghost_key has the tag Ghost_Key;
      - a creature blueprint whose heartbeat looks around (GetFirstObjectInShape) - spawned NPCs only."""
    mod = os.path.join(tmp, "mod")
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Tester"), Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]), Mod_OnModLoad=(R, "mod_load")))
    g("area001.are", root("ARE ", Name=loc("Hall"), Tag=(X, "HALL"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    npc = lambda tag: st(4, TemplateResRef=(R, "guard"), Tag=(X, tag), FirstName=loc(tag),  # noqa: E731
                         ScriptDialogue=(R, "npc_nochat"), Conversation=(R, "shop_dlg"))
    g("area001.git", root("GIT ", **{
        "Creature List": (LST, [npc("GUARD1"), npc("GUARD2")]),
        "Placeable List": (LST, [st(9, TemplateResRef=(R, "statue"), Tag=(X, "STATUE"), LocName=loc("Statue"),
                                    Static=(B, 1), OnUsed=(R, "statue_use"))]),
        "Door List": (LST, [st(8, TemplateResRef=(R, "door1"), Tag=(X, "DOOR1"), LocName=loc("Door"),
                                OnOpen=(R, "door_gone"))])}))
    g("guard.utc", root("UTC ", TemplateResRef=(R, "guard"), Tag=(X, "GUARD"), FirstName=loc("Guard"),
                        ScriptHeartbeat=(R, "guard_hb"),
                        VarTable=(LST, [st(0, Name=(X, "QUESTKEY"), Type=(DW, 3), Value=(X, "abc"))])))
    g("ghost_key.uti", root("UTI ", **item_fields("ghost_key", "Ghost_Key", "Ghost Key")))
    g("shop_dlg.dlg", dlg("if_takegold"))
    script(mod, "if_takegold", 'int StartingConditional()\n{\n    TakeGoldFromCreature(10, GetPCSpeaker());\n'
                               '    return TRUE;\n}\n')
    script(mod, "guard_hb", 'void main()\n{\n    object o = GetFirstObjectInShape(SHAPE_SPHERE, 10.0, GetLocation(OBJECT_SELF));\n'
                            '    while (GetIsObjectValid(o)) { o = GetNextObjectInShape(SHAPE_SPHERE, 10.0, GetLocation(OBJECT_SELF)); }\n}\n')
    script(mod, "ds_disease", 'void main()\n{\n    location l = GetLocation(OBJECT_SELF);\n    object oC = CreateObject('
                              'OBJECT_TYPE_PLACEABLE,"plc_invisobj",Location(GetAreaFromLocation(l),GetPosition(OBJECT_SELF),0.0),'
                              'FALSE,"DS_DISEASE");\n    SetLocalString(oC, "MARK", "not_a_variable_name");\n}\n')
    script(mod, "sh_store", 'void main()\n{\n    object oStore = GetNearestObject(OBJECT_TYPE_STORE);\n'
                            '    SetLocalObject(OBJECT_SELF, "STORE", oStore);\n}\n')
    script(mod, "mod_load", 'void main()\n{\n    object o = GetFirstPC();\n    ExecuteScript("ds_disease", o);\n'
                            '    ExecuteScript("sh_store", o);\n'
                            '    if (GetLocalString(o, "STORE") == "") SendMessageToPC(o, "x");\n'
                            '    if (GetLocalInt(o, "QUESTKEY")) SendMessageToPC(o, "y");\n'
                            '    if (GetIsObjectValid(GetItemPossessedBy(o, "ghost_key"))) SendMessageToPC(o, "z");\n}\n')
    return mod


def tester_feedback(tmp):
    """Each point of the tester's report: the false alarm is gone, or the finding now says where to look."""
    import nwn_sheets
    rep = h.analyse(tester_fixture(tmp), os.path.join(tmp, "an"))
    iss = rep["issues"]
    by = lambda cat, word: [i for i in iss if i["category"] == cat and word in (i["node"] + i["detail"])]  # noqa: E731
    bps = {i["node"] for i in iss if i["category"] == "missing_blueprint"}
    check("CreateObject's 5th argument is the new tag, not a blueprint: no missing ds_disease blueprint",
          not any("ds_disease" in b_ for b_ in bps), bps)
    check("... and the 2nd argument (plc_invisobj) is still the blueprint it creates", "bp:plc_invisobj.utp" in bps, bps)
    import nwn_index
    lits = nwn_index.lex_nss('void main(){ SetLocalString(o, "MARK", "v"); CreateObject(1, "bp", l, FALSE, "TAG"); }')[2]
    check("scanner: a string's argument position is kept (value / new tag are not names)",
          [(f, x) for _l, f, x, _c in lits] == [("SetLocalString", "MARK"), ("SetLocalString:arg3", "v"),
                                                 ("CreateObject", "bp"), ("CreateObject:arg5", "TAG")], lits)
    w_ = by("wrong_script_type", "if_takegold")
    check("a condition script in a conversation action slot is an info note (it works)",
          w_ and all(i["severity"] == "info" for i in w_) and "it runs" in w_[0]["detail"], w_)
    nochat = by("missing_script", "npc_nochat")
    check("a script missing only in NPCs' OnConversation is an info note (the known trick)",
          len(nochat) == 1 and nochat[0]["severity"] == "info" and "OnConversation" in nochat[0]["detail"], nochat)
    statue = by("missing_script", "statue_use")
    check("a script missing only on a static placeable is an info note (static placeables run no scripts)",
          len(statue) == 1 and statue[0]["severity"] == "info" and "static placeable" in statue[0]["detail"], statue)
    door = by("missing_script", "door_gone")
    check("a script missing in a door's OnOpen stays an error", len(door) == 1 and door[0]["severity"] == "error", door)
    d1 = nochat[0]["detail"] if nochat else ""
    check("accepting a missing script covers every object naming it, also ones added later",
          nwn_sheets.sig_matches(nwn_sheets.issue_sig(d1), d1.replace("used by 2 place(s)", "used by 3 place(s)")
                                 .replace("GUARD2", "GUARD3")), d1)
    check("an older stored signature (whole text) still matches", nwn_sheets.sig_matches(
        __import__("re").sub(r"\d+", "#", d1), d1), d1)
    page = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the same rule (sigCore) and an 'All N places' list under an issue's detail",
          'const sigCore = s => String(s || "").split("; used by ")[0];' in page and "All ${i.places.length} places" in page)
    st_ = by("variable_type_mismatch", "STORE")
    check("type mismatch says where the other type is set (script line)",
          st_ and "sh_store:4 (object)" in st_[0]["detail"] and "reused" in st_[0]["detail"], st_)
    qk = by("variable_type_mismatch", "QUESTKEY")
    check("... including a toolset variable on a blueprint, and lists every place",
          qk and "toolset variable" in qk[0]["detail"] and any(p.startswith("set:") for p in qk[0].get("places", [])), qk)
    tc = by("tag_case", "ghost_key")
    check("tag case: names what carries the other spelling and points out the resref/tag mix-up",
          tc and "Ghost Key" in tc[0]["detail"] and "lookups by tag use the tag, not the resref" in tc[0]["detail"], tc)
    hb = [f for f in rep.get("pw_performance", {}).get("findings", []) if f["category"] == "perf_heavy_heartbeat"
          and f["label"] == "guard_hb"]
    check("a creature-only heartbeat that looks around is an info note (runs only while the NPC exists)",
          hb and hb[0]["severity"] == "info", hb)
    check("... and is not an issue", not [i for i in iss if i["node"] == "script:guard_hb" and i["severity"] != "info"])


def ini_alias(tmp):
    """nwn.ini [Alias] moves the hak and tlk folders (a tester's pre-EE layout): haks and tlk are found there."""
    import nwn_index
    user = os.path.join(tmp, "user")
    old = os.path.join(tmp, "NeverwinterNights", "NWN")
    os.makedirs(os.path.join(old, "hak")); os.makedirs(os.path.join(old, "tlk")); os.makedirs(user)
    w(os.path.join(old, "hak"), "my_hak.hak", b"HAK V1.0")
    w(os.path.join(old, "tlk"), "my_tlk.tlk", b"TLK V3.0")
    with open(os.path.join(user, "nwn.ini"), "w", encoding="latin-1") as f:
        f.write("[Game Options]\nX=1\n[Alias]\nHAK=%s\nTLK=..\\NeverwinterNights\\NWN\\tlk\n"
                % os.path.join(old, "hak").replace("/", "\\"))
    al = nwn_index.ini_aliases(user)
    check("nwn.ini: [Alias] HAK (absolute) and TLK (relative to nwn.ini's folder) are read",
          os.path.normpath(al.get("HAK", "")).endswith(os.path.join("NWN", "hak")) and
          os.path.isdir(al.get("TLK", "")), al)
    found, missing = nwn_index.find_haks(["my_hak"], (), nwn_index.default_hak_dirs(None, user))
    check("nwn.ini: a hak in the moved hak folder is found", len(found) == 1 and not missing, (found, missing))
    check("nwn.ini: a custom tlk in the moved tlk folder is found",
          (nwn_index.locate_custom_tlk("my_tlk", None, user) or "").endswith("my_tlk.tlk"))
    check("nwn.ini: none or no [Alias] -> nothing", nwn_index.ini_aliases(tmp) == {})


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    with h.tempdir() as t:
        h.run(main_checks, t)
    with h.tempdir() as t:
        h.run(resref_needs_runner, t)
    with h.tempdir() as t:
        h.run(tester_feedback, t)
    with h.tempdir() as t:
        h.run(ini_alias, t)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())

