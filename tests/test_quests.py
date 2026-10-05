"""
Quest inference (quests without a journal) and the known-pitfall checks.
    python tests/test_quests.py          (last line "N/M checks passed"; exit code 1 on a failure)
Fixture: a small module built like a big persistent world - a quest-token include, a quest-giver conversation whose
conditions/actions read and write tokens, locals and the campaign DB, quest items, and some deliberate mistakes.
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import os
import sys

import nwnlib as n  # noqa: E402
import nwn_index  # noqa: E402
import nwn_analysis  # noqa: E402
from _gff import st, root, loc, R, X, LST, DW, B, w, item_fields, stand_in_ncs  # noqa: E402
from _harness import check  # noqa: E402

HERE = h.HERE


INCLUDE = r'''
const int QT_STR_LEN = 100; // Default length of PC strings
const int QT_DONEKOBOLDS = 33;
const int QT_DONEHOBS = 51; // 181019 jdoe - hobgoblin quest for the marshal
const int QT_CLASH = 51; // someone reused slot 51
const int QT_TOOFAR = 120; // past the end
const string PC_TAKEN = "1"; // started
const string PC_DONE = "2"; // finished

void SetQuestToken(object oPC, int iInsertPos = 0, string sQuestToken = "1");
string GetQuestToken(object oPC, int iRetrievePos = 0);

void SetQuestToken(object oPC, int iInsertPos = 0, string sQuestToken = "1")
{
    SetLocalString(oPC, "PCSTR", GetStringLeft(GetLocalString(oPC, "PCSTR"), iInsertPos) + sQuestToken);
}
string GetQuestToken(object oPC, int iRetrievePos = 0)
{
    return GetSubString(GetLocalString(oPC, "PCSTR"), iRetrievePos, 1);
}
'''

SCRIPTS = {
    "tok_inc": INCLUDE,
    # kobold job: offered, taken, finished with the skull
    "kob_sc_notdone": '#include "tok_inc"\nint StartingConditional()\n{\n    if(GetQuestToken(GetPCSpeaker(), QT_DONEKOBOLDS) == PC_TAKEN) return FALSE;\n    return TRUE;\n}\n',
    "kob_at_take": '#include "tok_inc"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QT_DONEKOBOLDS, PC_TAKEN);\n}\n',
    "kob_sc_skull": '#include "nw_i0_tool"\nint StartingConditional()\n{\n    if(!HasItem(GetPCSpeaker(), "IT_KOBSKULL")) return FALSE;\n    return TRUE;\n}\n',
    "kob_at_done": '#include "tok_inc"\nvoid main()\n{\n    object oPC = GetPCSpeaker();\n    DestroyObject(GetItemPossessedBy(oPC, "IT_KOBSKULL"));\n    SetQuestToken(oPC, QT_DONEKOBOLDS, PC_DONE);\n    GiveGoldToCreature(oPC, 250);\n    GiveXPToCreature(oPC, 500);\n}\n',
    "kob_sc_finished": '#include "tok_inc"\nint StartingConditional()\n{\n    return GetQuestToken(GetPCSpeaker(), QT_DONEKOBOLDS) == PC_DONE;\n}\n',
    # hobgoblin job: checks stage 3 which nothing sets; slot 51 also used as QT_CLASH
    "hob_at_take": '#include "tok_inc"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QT_DONEHOBS);\n}\n',
    "hob_sc_stage3": '#include "tok_inc"\nint StartingConditional()\n{\n    return GetQuestToken(GetPCSpeaker(), QT_DONEHOBS) == "3";\n}\n',
    "clash_at": '#include "tok_inc"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QT_CLASH, "1");\n}\n',
    "far_at": '#include "tok_inc"\nvoid main()\n{\n    SetQuestToken(GetPCSpeaker(), QT_TOOFAR, "1");\n}\n',
    # pay: locals on the PC, Lilac Soul style
    "pay_at": 'void main()\n{\n    SetLocalInt(GetPCSpeaker(), "nBeenPaid", 1);\n    GiveGoldToCreature(GetPCSpeaker(), 30);\n}\n',
    "pay_sc": 'int StartingConditional()\n{\n    if(!(GetLocalInt(GetPCSpeaker(), "nBeenPaid") == 1))\n        return FALSE;\n    return TRUE;\n}\n',
    # campaign DB, and an item nobody can get
    "db_at": 'void main()\n{\n    SetCampaignInt("pwdata", "RUNE_Q", 2, GetPCSpeaker());\n}\n',
    "db_sc": 'int StartingConditional()\n{\n    return GetCampaignInt("pwdata", "RUNE_Q", GetPCSpeaker()) == 2 && GetIsObjectValid(GetItemPossessedBy(GetPCSpeaker(), "IT_RUNE"));\n}\n',
    # read but never set, and a dynamic name we can't follow
    "ghost_sc": 'int StartingConditional()\n{\n    return GetLocalInt(GetPCSpeaker(), "nGhostSeen") == 1;\n}\n',
    "dyn_at": 'void main()\n{\n    string s = "Q_" + GetTag(OBJECT_SELF);\n    SetLocalInt(GetPCSpeaker(), s, 1);\n}\n',
    # a legacy quest: only an unused script touches it
    "old_at": '#include "tok_inc"\nvoid main()\n{\n    SetLocalInt(GetPCSpeaker(), "nOldQuest", 1);\n}\n',
    # journal
    "jr_at": 'void main()\n{\n    AddJournalQuestEntry("q_rats", 2, GetPCSpeaker());\n}\n',
    # generic scripts configured per conversation node with EE script parameters
    "inc_quest": 'void SetHideVar(object oPC, string sVar, int nVal)\n{\n    object oHide = GetItemInSlot(INVENTORY_SLOT_CARMOUR, oPC);\n'
                 '    SetLocalInt(oHide, sVar, nVal);\n}\n',
    "gen_c_dobounty": '#include "inc_quest"\nvoid main()\n{\n    string sVar = GetScriptParam("VAR_NAME");\n'
                      '    string sItm = GetScriptParam("Q_ITEM");\n    int nVal = StringToInt(GetScriptParam("VALUE"));\n'
                      '    object oPC = GetPCSpeaker();\n    if (sItm != "") DestroyObject(GetItemPossessedBy(oPC, sItm));\n'
                      '    if (sVar != "") SetHideVar(oPC, sVar, nVal);\n}\n',
    "gen_c_gethideint": 'int StartingConditional()\n{\n    string sVar = GetScriptParam("VAR_NAME");\n'
                        '    int nVal = StringToInt(GetScriptParam("VALUE"));\n    string sComp = GetScriptParam("COMPARE");\n'
                        '    object oHide = GetItemInSlot(INVENTORY_SLOT_CARMOUR, GetPCSpeaker());\n'
                        '    if (sComp == "" || sComp == "==") { if (GetLocalInt(oHide, sVar) == nVal) return TRUE; else return FALSE; }\n'
                        '    else if (sComp == "!=") { if (GetLocalInt(oHide, sVar) != nVal) return TRUE; else return FALSE; }\n'
                        '    else if (sComp == ">=") { if (GetLocalInt(oHide, sVar) >= nVal) return TRUE; else return FALSE; }\n'
                        '    return FALSE;\n}\n',
}


def build(mod):
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    for nm, src in SCRIPTS.items():
        w(mod, nm + ".nss", src)
        if "main" in src:
            w(mod, nm + ".ncs", stand_in_ncs(nm))
    # deep include chain for the include-depth pitfall
    for i in range(50):
        w(mod, f"deep{i:02d}.nss", f'#include "deep{i + 1:02d}"\n' if i < 49 else "int DEEP_END = 1;\n")
    w(mod, "deep_main.nss", '#include "deep00"\nvoid main() { }\n')
    w(mod, "deep_main.ncs", stand_in_ncs("deep_main"))
    g("module.ifo", root("IFO ", Mod_Name=loc("Quest Test"), Mod_Entry_Area=(R, "town"),
                         Mod_OnClientEntr=(R, "deep_main"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, a)) for a in ("town", "cave", "cave2", "noname")])))

    def entry(txt, script="", replies=()):
        return st(0, Text=loc(txt), Speaker=(X, ""), Script=(R, script), Quest=(X, ""), QuestEntry=(DW, 0),
                  RepliesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in replies]))

    def reply(txt, script="", entries=()):
        return st(0, Text=loc(txt), Script=(R, script), Quest=(X, ""),
                  EntriesList=(LST, [st(0, Index=(DW, i), Active=(R, a), IsChild=(B, 0)) for i, a in entries]))
    g("marshal.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [entry("Need work?", replies=[(0, "kob_sc_notdone"), (1, "kob_sc_skull"), (2, "pay_sc"), (3, "hob_sc_stage3"), (4, "db_sc"), (5, "ghost_sc"), (6, "kob_sc_finished")]),
                         entry("Kill the kobold chief.", script="kob_at_take"),
                         entry("Well done.", script="kob_at_done"),
                         entry("Here is your pay.", script="pay_at"),
                         entry("Hobgoblins.", script="hob_at_take"),
                         entry("Runes.", script="db_at"),
                         entry("Other.", script="clash_at"),
                         entry("Far.", script="far_at"),
                         entry("Dyn.", script="dyn_at"),
                         entry("Rats.", script="jr_at")]),
        ReplyList=(LST, [reply("Kobolds?", entries=[(1, "")]), reply("I have the skull.", entries=[(2, "")]),
                         reply("Pay?", entries=[(3, "")]), reply("Hobgoblins?", entries=[(4, ""), (6, ""), (7, "")]),
                         reply("Runes?", entries=[(5, ""), (8, "")]), reply("Ghost?", entries=[(9, "")]),
                         reply("Done.")]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))
    g("marshal.utc", root("UTC ", TemplateResRef=(R, "marshal"), Tag=(X, "MARSHAL"), FirstName=loc("Marshal Brennan"),
                          Conversation=(R, "marshal"), Portrait=(R, "hu_m_99_")))    # no po_ prefix
    g("kobskull.uti", root("UTI ", **item_fields("kobskull", "IT_KOBSKULL", "Kobold skull")))
    g("rune.uti", root("UTI ", **item_fields("rune", "IT_RUNE", "Rune")))           # never placed or created
    g("Kobold_Chief.utc", root("UTC ", TemplateResRef=(R, "Kobold_Chief"), Tag=(X, "KOBCHIEF"), FirstName=loc("Chief"),
                               ItemList=(LST, [st(0, InventoryRes=(R, "kobskull"), Repos_PosX=(DW, 0), Repos_Posy=(DW, 0))])))
    for a, tag in (("town", "TOWN"), ("cave", "CAVE"), ("cave2", "CAVE"), ("noname", "")):
        g(f"{a}.are", root("ARE ", Name=loc(a), Tag=(X, tag), ResRef=(R, a), Tileset=(R, "tcn01")))
        crs = [st(4, TemplateResRef=(R, "marshal"), Tag=(X, "MARSHAL"), FirstName=loc("Marshal Brennan"),
                  Conversation=(R, "marshal")),
               st(4, TemplateResRef=(R, "orcboss"), Tag=(X, "ORCBOSS"), FirstName=loc("Captain Bors"),
                  Conversation=(R, "orcboss"))] if a == "town" else []
        if a == "cave2":
            crs.append(st(4, TemplateResRef=(R, "Kobold_Chief"), Tag=(X, "ORCCHIEF"), FirstName=loc("Orc chief"),
                          ItemList=(LST, [st(0, InventoryRes=(R, "orchead"))])))
        if a == "cave":
            crs.append(st(4, TemplateResRef=(R, "Kobold_Chief"), Tag=(X, "KOBCHIEF"), FirstName=loc("Chief"),
                          ItemList=(LST, [st(0, InventoryRes=(R, "kobskull"))])))
        g(f"{a}.git", root("GIT ", **{"Creature List": (LST, crs)}))
    # generic-script conversation: every quest step is the same two scripts, told what to do by node parameters
    P = lambda **kv: [st(0, Key=(X, k), Value=(X, v)) for k, v in kv.items()]  # noqa: E731
    g("orcboss.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [
            st(0, Text=loc("Orcs in the mine."), Speaker=(X, ""), Script=(R, ""), Quest=(X, ""), QuestEntry=(DW, 0),
               RepliesList=(LST, [st(0, Index=(DW, 0), Active=(R, "gen_c_gethideint"), IsChild=(B, 0),
                                     ConditionParams=(LST, P(VAR_NAME="QUEST_T_ORC", VALUE="1", COMPARE="!="))),
                                  st(0, Index=(DW, 1), Active=(R, ""), IsChild=(B, 0))])),
            st(0, Text=loc("Here's your pay."), Speaker=(X, ""), Script=(R, "gen_c_dobounty"), Quest=(X, ""), QuestEntry=(DW, 0),
               ActionParams=(LST, P(Q_ITEM="IT_ORCHEAD", VAR_NAME="QUEST_T_ORC", VALUE="1")),
               RepliesList=(LST, [])),
            st(0, Text=loc("Already paid."), Speaker=(X, ""), Script=(R, "gen_c_dobounty"), Quest=(X, ""), QuestEntry=(DW, 0),
               ActionParams=(LST, P(Q_ITEM="IT_ORCHEAD")), RepliesList=(LST, []))]),
        ReplyList=(LST, [st(0, Text=loc("Tell me about the orcs."), Script=(R, ""), Quest=(X, ""), EntriesList=(LST, [])),
                         st(0, Text=loc("I have the head."), Script=(R, ""), Quest=(X, ""),
                            EntriesList=(LST, [st(0, Index=(DW, 2), Active=(R, "gen_c_gethideint"), IsChild=(B, 0),
                                                  ConditionParams=(LST, P(VAR_NAME="QUEST_T_ORC", VALUE="1"))),
                                               st(0, Index=(DW, 1), Active=(R, ""), IsChild=(B, 0))]))]),
        StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, ""))])))
    # a second conversation reuses the same scripts for a different variable, with a >= check
    g("seer.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
        EntryList=(LST, [st(0, Text=loc("The book?"), Speaker=(X, ""), Script=(R, "gen_c_dobounty"), Quest=(X, ""),
                            QuestEntry=(DW, 0), ActionParams=(LST, P(VAR_NAME="Q_T_BOOK", VALUE="2")), RepliesList=(LST, [])),
                         st(0, Text=loc("Thanks again."), Speaker=(X, ""), Script=(R, ""), Quest=(X, ""), QuestEntry=(DW, 0),
                            RepliesList=(LST, []))]),
        ReplyList=(LST, []),
        StartingList=(LST, [st(0, Index=(DW, 1), Active=(R, "gen_c_gethideint"),
                               ConditionParams=(LST, P(VAR_NAME="Q_T_BOOK", VALUE="2", COMPARE=">="))),
                            st(0, Index=(DW, 0), Active=(R, ""))])))
    g("orcboss.utc", root("UTC ", TemplateResRef=(R, "orcboss"), Tag=(X, "ORCBOSS"), FirstName=loc("Captain Bors"),
                          Conversation=(R, "orcboss"), Portrait=(R, "po_hu_m_99_")))
    g("orchead.uti", root("UTI ", **item_fields("orchead", "IT_ORCHEAD", "Orc head")))
    g("module.jrl", root("JRL ", Categories=(LST, [st(0, Tag=(X, "q_rats"), Name=loc("Rats"),
                                                        EntryList=(LST, [st(0, ID=(DW, 2), Text=loc("Rats cleared"), End=(n.WORD, 1))]))])))


def quest_inference(tmp):
    """Quests read from the fixture's scripts and conversations, the known-pitfall lints, and the script generator."""
    mod, out = os.path.join(tmp, "mod"), os.path.join(tmp, "out")
    build(mod)
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, [], [], None, out, False, False)
        rep = nwn_analysis.run_analysis(out, verbose=False)
    iq = rep["inferred_quests"]
    Q = {q["id"]: q for q in iq["quests"]}
    tok = lambda slot: Q.get(f"QuestToken::{slot}")  # noqa: E731
    check("quest-token helper pair detected from the include", any(a["setter"] == "SetQuestToken" and a["defined_in"] == "tok_inc"
                                                                  for a in iq["summary"]["adapters"]), iq["summary"]["adapters"])
    k = tok("33")
    check("token quest found by slot, named from its constant", k and k["label"] == "QT_DONEKOBOLDS" and k["kind"] == "token", k and k["label"])
    check("stages: 1 set when taken, 2 when finished", k and k["values_set"] == ["1", "2"], k and k["values_set"])
    st1 = {s["value"]: s for s in k["stages"]} if k else {}
    check("each stage lists who sets and who checks it", "kob_at_take" in [x["script"] for x in st1.get("1", {}).get("set_by", [])]
          and "kob_sc_notdone" in [x["script"] for x in st1.get("1", {}).get("checked_by", [])])
    check("giver NPC and conversation found", k and k["conversation_labels"] and any("Marshal Brennan" in gv for gv in k["givers"]), k and k["givers"])
    check("quest item taken, and it is obtainable (kobold chief carries it)",
          k and any(i["name"] == "IT_KOBSKULL" and i["role"] == "takes" and i["status"] == "ok" for i in k["items"]), k and k["items"])
    check("rewards found (250 gold, 500 xp)", k and {("gold", "250"), ("xp", "500")} <= {(r["kind"], r["amount"]) for r in k["rewards"]}, k and k["rewards"])
    check("healthy quest has no problems", k and k["health"] == "ok", k and k["problems"])
    h = tok("51")
    txt = " | ".join(p["text"] for p in (h or {}).get("problems", []))
    check("label from the constant's comment, date/author stripped", h and h["label"].startswith("hobgoblin quest for the marshal"), h and h["label"])
    check("check '== 3' that nothing sets is reported as never true", "can never be true" in txt and "== 3" in txt, txt)
    check("the setter's default value (\"1\") counts as a stage", h and "1" in h["values_set"], h and h["values_set"])
    check("slot 51 used under two names is an error", "overwrite each other" in txt, txt)
    f = tok("120")
    check("slot past the end of the token string is an error", f and any("past the end" in p["text"] for p in f["problems"]), f and f["problems"])
    pay = Q.get("local:pc:nBeenPaid")
    check("PC local variable becomes a quest with a set and a check", pay and pay["values_set"] == ["1"] and pay["health"] == "ok", pay)
    db = Q.get("campaign::pwdata/RUNE_Q")
    check("campaign DB key followed", db and db["values_set"] == ["2"], db)
    # a check for an item is not always a requirement - flagged for checking, not "broken"
    check("an item nobody can obtain is flagged (warning, not broken)", db and db["health"] == "warning" and
          any("IT_RUNE" in p["text"] and "can't get it" in p["text"] and p["level"] == "warning" for p in db["problems"]),
          db and db["problems"])
    gh = Q.get("local:pc:nGhostSeen")
    check("read but never set: the check can never be true", gh and gh["health"] == "broken", gh and gh["problems"])
    check("dynamic names are counted as not followed, not guessed", iq["summary"]["not_followed"] >= 1)
    old = Q.get("local:pc:nOldQuest")
    check("state touched only by unused scripts is marked legacy", old and old["health"] == "legacy", old and old["health"])
    jr = Q.get("journal::q_rats")
    check("journal updates are included too", jr and jr["values_set"] == ["2"])
    # ---- quests configured by conversation script parameters (generic scripts, EE GetScriptParam)
    orc = Q.get("local:pc:QUEST_T_ORC")
    check("SP: a variable named only in a node's script parameters becomes a quest", orc is not None, sorted(Q)[:40])
    if orc:
        so = {s_["value"]: s_ for s_ in orc["stages"]}
        sets = so.get("1", {}).get("set_by", [])
        check("SP: the hand-in node sets it to 1 (VALUE parameter)", orc["values_set"] == ["1"] and
              any("orcboss" in w_ and "EntryList[1]" in w_ for x in sets for w_ in x["where"]), (orc["values_set"], sets))
        checks = {c["test"] for c in so.get("1", {}).get("checked_by", [])}
        check("SP: COMPARE picks the branch: offer is '!= 1', hand-in reply is '== 1'", checks == {"!= 1", "== 1"}, checks)
        check("SP: only this quest's nodes count, not every conversation using the script",
              orc["conversation_labels"] == ["orcboss"] and any("Captain Bors" in g_ for g_ in orc["givers"]),
              (orc["conversation_labels"], orc["givers"]))
        check("SP: the item named in Q_ITEM is taken, and the orc chief carries it",
              any(i["name"] == "IT_ORCHEAD" and i["status"] == "ok" for i in orc["items"]), orc["items"])
        check("SP: stored on the hide = the player's", orc["scope"] == "pc")
        check("SP: healthy", orc["health"] == "ok", orc["problems"])
    va_vars = {v["name"]: v for v in (rep.get("var_audit") or {}).get("variables", [])}
    vo = va_vars.get("QUEST_T_ORC")
    check("SP: the Variables page sees parameter-named variables too, with the node", vo and vo["n_sets"] >= 1 and
          vo["n_reads"] >= 2 and any("orcboss" in r_.get("where", "") for r_ in vo["sets"]), vo)
    bk = Q.get("local:pc:Q_T_BOOK")
    check("SP: the same scripts in another conversation make a separate quest (only there)",
          bk and bk["conversation_labels"] == ["seer"] and bk["values_set"] == ["2"]
          and {c["test"] for s_ in bk["stages"] for c in s_["checked_by"]} == {">= 2"}, bk)
    check("SP: parameter scripts are not counted as 'not followed'",
          iq["summary"].get("from_script_parameters", 0) >= 5 and "gen_c_dobounty" in iq["summary"].get("parameter_scripts", []),
          (iq["summary"].get("from_script_parameters"), iq["summary"].get("parameter_scripts")))
    hubs = iq["hubs"]
    check("quests grouped under their giver's conversation", hubs and hubs[0]["node"] == "dlg:marshal" and len(hubs[0]["quests"]) >= 5, hubs[:1])
    check("inferred quests CSV written", os.path.isfile(os.path.join(out, "reports", "inferred_quests.csv")))
    check("summary counts inferred quests", rep["summary"]["inferred_quests"] == len(iq["quests"]))

    # ------------------------------------------------------------ pitfall checks
    cats = {}
    for i in rep["issues"]:
        cats.setdefault(i["category"], []).append(i)
    check("empty area tag is an error", any(i["severity"] == "error" and "noname" in i["detail"] for i in cats.get("area_tag", [])), cats.get("area_tag"))
    check("shared area tag is a warning", any("share the tag 'CAVE'" in i["detail"] for i in cats.get("area_tag", [])))
    check("resref with capitals flagged (GetResRef case in EE)", any("Kobold_Chief" in i["detail"] for i in cats.get("resref_case", [])), cats.get("resref_case"))
    check("portrait without po_ flagged", any("hu_m_99_" in i["detail"] for i in cats.get("portrait_prefix", [])), cats.get("portrait_prefix"))
    check("deep include chain (50) warned before the 64 limit", any("deep_main" in i["label"] for i in cats.get("include_depth", [])), cats.get("include_depth"))
    check("no pitfall check crashed", "lint_failed" not in cats, cats.get("lint_failed"))

    # ------------------------------------------------------------ script generator (EE upgrade)
    import nwn_edit
    groot = os.path.join(tmp, "game"); os.makedirs(os.path.join(groot, "ovr"))
    with open(os.path.join(groot, "ovr", "nwscript.nss"), "w") as fh:
        fh.write("int TRUE = 1;\nint ABILITY_STRENGTH = 0;\nint ABILITY_DEXTERITY = 1;\nint DURATION_TYPE_TEMPORARY = 1;\n"
                 "// Create an Ability Increase effect.\neffect EffectAbilityIncrease(int nAbilityToIncrease, int nModifyBy);\n"
                 "void ApplyEffectToObject(int nDurationType, effect eEffect, object oTarget, float fDuration=0.0f);\n"
                 "object GetPCSpeaker();\nobject GetEnteringObject();\n")
    nws = nwn_edit.load_nwscript(groot, None)
    check("generator reads the game's nwscript.nss (ovr folder)", nws["found"] and "EffectAbilityIncrease" in nws["functions"]
          and nws["functions"]["EffectAbilityIncrease"]["doc"].startswith("Create an Ability"), nws.get("where"))
    cat = nwn_edit.catalog(nws, None, [])
    check("constant pickers come from nwscript.nss", cat["constants"].get("ABILITY_") == ["ABILITY_DEXTERITY", "ABILITY_STRENGTH"])
    r = nwn_edit.generate_script(dict(name="g_boost", event="DlgAction", actions=[dict(key="ability_boost", values=dict(
        ability="ABILITY_STRENGTH", amount="2", secs="60"))]), nwscript=nws)
    check("EE action generates and passes the build check", "EffectAbilityIncrease(ABILITY_STRENGTH, 2), oPC, 60.0" in r["source"]
          and not r["warnings"], (r["source"], r["warnings"]))
    r = nwn_edit.generate_script(dict(name="g_bad", event="DlgAction", actions=[dict(key="ability_boost", values=dict(
        ability="ABILITY_STRENGHT", amount="2", secs="60"))]), nwscript=nws)
    check("a constant your build doesn't have is flagged", any("ABILITY_STRENGHT" in w_ for w_ in r["warnings"]), r["warnings"])
    try:
        nwn_edit.generate_script(dict(name="g_tok", event="DlgAction", actions=[dict(key="token_set", values=dict(slot="QT_X", value="1"))]))
        refused = False
    except ValueError:
        refused = True
    check("token actions need the module's token system", refused)
    ad = dict(setter="SetQuestToken", getter="GetQuestToken", include="tok_inc")
    r = nwn_edit.generate_script(dict(name="g_tok", event="DlgCondition", conditions=[dict(key="token_eq", values=dict(slot="QT_DONEKOBOLDS", value="2"))]), adapter=ad)
    check("token condition uses the module's helper and #includes it", '#include "tok_inc"' in r["source"] and
          'GetQuestToken(oPC, QT_DONEKOBOLDS) == "2"' in r["source"], r["source"])
    r = nwn_edit.generate_script(dict(name="g_sql", event="OnUsed", actions=[dict(key="sql_player_set", values=dict(key="k", value="v"))]))
    check("EE SQLite action brings its helper function", "void TkSqlSet(object oOwner" in r["source"] and "SqlPrepareQueryObject" in r["source"])
    ev_ok = all(nwn_edit.generate_script(dict(name="g_ev", event=ev))["source"] for ev in nwn_edit.EVENTS)
    check(f"all {len(nwn_edit.EVENTS)} events generate", ev_ok)

    return nwn_edit


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_quest_") as tmp:
        nwn_edit = h.run(quest_inference, tmp)
        if nwn_edit:
            h.run(inference_edge_cases, nwn_edit)
        h.run(script_param_units)
    return h.summary()


def _qi(scripts):
    import nwn_quests
    qi = nwn_quests.QuestInference(scripts, {}, {}, {f"script:{k}" for k in scripts}, {}, {}, {}, ())
    res = qi.run()
    return {q["id"]: q for q in res["quests"]}, qi


TOK = ('const int QT_STR_LEN = 100;\nconst int QT_A = 60;\n'
       'void SetQuestToken(object oPC, int iPos = 0, string s = PC_TAKEN) { SetLocalString(oPC, "S", s); }\n'
       'string GetQuestToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "S"), iPos, 1); }\n'
       'const string PC_TAKEN = "1";\n')


def inference_edge_cases(nwn_edit):
    """Edge cases of quest inference and the lints, each reproduced here before it was fixed."""
    import nwn_lints
    import nwn_quests
    # 1. party-wide flag set on oMember (from GetFirstFactionMember) is the player; oNPC is not
    q, _ = _qi({"at": 'void main(){ object oMember = GetFirstFactionMember(GetPCSpeaker());\n'
                      ' while(GetIsObjectValid(oMember)){ SetLocalInt(oMember, "Q_WOLF", 2); oMember = GetNextFactionMember(GetPCSpeaker()); } }',
                "sc": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q_WOLF") == 2; }'})
    check("R7: a flag set on each party member counts as set on the player", q.get("local:pc:Q_WOLF", {}).get("health") == "ok",
          q.get("local:pc:Q_WOLF", {}).get("problems"))
    q, _ = _qi({"a": 'void main(){ object oPC = GetPCSpeaker(); object oHide = GetItemInSlot(INVENTORY_SLOT_CARMOUR, oPC);\n'
                     ' SetLocalInt(oHide, "bank_account", 5); }',
                "b": 'int StartingConditional(){ object oHide = GetPCHide(GetPCSpeaker()); return GetLocalInt(oHide, "bank_account") == 5; }'})
    check("R7: state kept on the player's hide counts as the player's", q.get("local:pc:bank_account", {}).get("health") == "ok", list(q))
    check("R7: oNPC is not taken for the player", nwn_quests.QuestInference.scope("oNPC") == "other" and
          nwn_quests.QuestInference.scope("oPC") == "pc")
    # 2. int and string variables of the same name are separate; campaign strings start as ""
    q, _ = _qi({"a": 'void main(){ SetCampaignString("db", "RUNE", "done", GetPCSpeaker()); }',
                "b": 'int StartingConditional(){ return GetCampaignString("db", "RUNE", GetPCSpeaker()) == ""; }'})
    check("R7: campaign string check == \"\" is possible (not broken)", all(x["health"] != "broken" for x in q.values()), q)
    q, _ = _qi({"a": 'void main(){ SetLocalInt(GetPCSpeaker(), "Q", 1); }',
                "b": 'int StartingConditional(){ return GetLocalString(GetPCSpeaker(), "Q") == ""; }'})
    check("R7: an int and a string local with one name are two variables", "local:pc:Q" in q and "local:pc:Q:string" in q
          and q["local:pc:Q:string"]["health"] != "broken", list(q))
    # 3. token length only from the token helpers' include
    q, qi = _qi({"tok": TOK, "zchat": 'const int CHAT_STR_LEN = 40;\nvoid main(){}',
                 "a": '#include "tok"\nvoid main(){ SetQuestToken(GetPCSpeaker(), QT_A, "1"); }',
                 "b": '#include "tok"\nint StartingConditional(){ return GetQuestToken(GetPCSpeaker(), QT_A) == "1"; }'})
    check("R7: token length comes from the token include, not CHAT_STR_LEN", qi.string_len == 100, qi.string_len)
    # 4. the helper's default value may be a constant; 6. an unset slot may read as ""
    q, _ = _qi({"tok": TOK, "a": '#include "tok"\nvoid main(){ SetQuestToken(GetPCSpeaker(), QT_A); }',
                "b": '#include "tok"\nint StartingConditional(){ return GetQuestToken(GetPCSpeaker(), QT_A) == "1"; }',
                "c": '#include "tok"\nint StartingConditional(){ return GetQuestToken(GetPCSpeaker(), QT_A) == ""; }'})
    t = q.get("QuestToken::60", {})
    check("R7: default value given as a constant (PC_TAKEN) resolves to \"1\"", "1" in t.get("values_set", []), t.get("values_set"))
    check("R7: a token check == \"\" (not started) is possible", t.get("health") == "ok", t.get("problems"))
    # 5. a wrapper function passing the name on is followed
    q, qi = _qi({"lib": 'void SetStage(object oPC, string sQ, int n){ SetLocalInt(oPC, sQ, n); }',
                 "a": '#include "lib"\nvoid main(){ SetStage(GetPCSpeaker(), "Q_BEAR", 2); }',
                 "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q_BEAR") == 2; }'})
    check("R7: writes through a helper function (SetStage) are followed", q.get("local:pc:Q_BEAR", {}).get("health") == "ok"
          and qi.not_followed == 0, (q.get("local:pc:Q_BEAR", {}).get("problems"), qi.not_followed))
    q, _ = _qi({"a": 'void main(){ string s = "Q_" + GetTag(OBJECT_SELF); SetLocalInt(GetPCSpeaker(), s, 1); }',
                "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q_WOLF") == 1; }',
                "c": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "OTHER") == 1; }'})
    check("R7: a run-time name that could match downgrades 'never true' to a warning",
          q["local:pc:Q_WOLF"]["health"] == "warning" and q["local:pc:OTHER"]["health"] == "broken",
          (q["local:pc:Q_WOLF"]["health"], q["local:pc:OTHER"]["health"]))
    # 7. constants in /* */ don't count; hex decoded
    q, qi = _qi({"aaa_old": '/*\nconst int QT_A = 99;\n*/\nvoid main(){}', "tok": TOK,
                 "h": 'const int Q_DONE = 0x2;\nvoid main(){ SetLocalInt(GetPCSpeaker(), "Q", 2); }',
                 "a": '#include "tok"\nvoid main(){ SetQuestToken(GetPCSpeaker(), QT_A, "1"); }',
                 "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q") == Q_DONE; }'})
    check("R7: a commented-out constant is ignored", qi.consts["QT_A"][1] == "60", qi.consts["QT_A"])
    check("R7: hex constants are decoded", q.get("local:pc:Q", {}).get("health") == "ok", q.get("local:pc:Q", {}).get("problems"))
    # 14/15. escaped quotes; arithmetic after the call
    calls = list(nwn_quests.iter_calls('SetLocalString(o, "M", "a \\" ("); SetLocalInt(o, "X", 1);', {"SetLocalString", "SetLocalInt"}))
    check("R7: an escaped quote inside a string doesn't derail the call parser", [c[0] for c in calls] == ["SetLocalString", "SetLocalInt"], calls)
    code = 'if (3 == GetLocalInt(o, "Q") + 1) {}'
    (_fn, _a, s_, e_), = list(nwn_quests.iter_calls(code, {"GetLocalInt"}))
    check("R7: `3 == Get(...) + 1` is not read as `== 3`", nwn_quests.comparison_after(code, e_, s_) == (None, None))
    # 16. a needed tag not found is a warning (base-game item?), not broken
    qi = nwn_quests.QuestInference({"a": 'void main(){ object o = GetItemPossessedBy(GetPCSpeaker(), "NW_IT_GEM001"); SetLocalInt(GetPCSpeaker(), "QG", 1); }',
                                    "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "QG") == 1; }'},
                                   {}, {}, {"script:a", "script:b"}, {}, {}, {}, ())
    qg = {x["id"]: x for x in qi.run()["quests"]}.get("local:pc:QG", {})
    check("R7: a needed item tag that isn't in the module is a warning, not broken", qg.get("health") == "warning", qg.get("problems"))
    # generator: 8 text fields only inside quotes; 9 one-digit stages; 10 same action twice; 13 newlines; 18 digits
    def refuses(**spec):
        try:
            nwn_edit.generate_script(dict(name="g_x", event="DlgAction", **spec),
                                     adapter=dict(setter="SetQuestToken", getter="GetQuestToken", include="tok"))
            return False
        except ValueError:
            return True
    check("R7: faction can't carry code", refuses(actions=[dict(key="faction", values=dict(tag="t", faction="STANDARD_FACTION_HOSTILE); DestroyObject(GetFirstPC()"))]))
    check("R7: class condition can't carry code", refuses(conditions=[dict(key="class", values=dict(cls="CLASS_TYPE_FIGHTER, oPC) > 0 || TRUE || (1"))]))
    check("R7: token stage must be one digit", refuses(actions=[dict(key="token_set", values=dict(slot="QT_A", value="10"))])
          and refuses(actions=[dict(key="token_set", values=dict(slot="QT_A", value=""))]))
    check("R7: text with a line break is refused", refuses(actions=[dict(key="message", values=dict(text="a\nb"))]))
    check("R7: non-ASCII or 64-bit numbers are refused", refuses(actions=[dict(key="gold", values=dict(amount="\u0661\u0662"))])
          and refuses(actions=[dict(key="gold", values=dict(amount="99999999999"))]))
    r = nwn_edit.generate_script(dict(name="g_two", event="OnUsed", actions=[dict(key="unlock", values=dict(tag="A")),
                                                                              dict(key="unlock", values=dict(tag="B"))]))
    check("R7: the same action twice keeps its variables in separate blocks", r["source"].count("{\n        object oLock") == 2, r["source"])
    r = nwn_edit.generate_script(dict(name="g_lib", event="DlgCondition", description="calls MyFn( in the comment only",
                                      conditions=[dict(key="gold", values=dict(amount="5"))]), {"MyFn": "mylib", "Other": "olib"})
    check("R7: library #include only for functions the code calls", "mylib" not in r["source"], r["source"])
    r = nwn_edit.generate_script(dict(name="g_lib2", event="DlgCondition", conditions=[dict(key="gold", values=dict(amount="5"))]),
                                 {"GetGold": "fake_lib"})
    check("R7: condition scripts get library #includes too", '#include "fake_lib"' in r["source"], r["source"])
    nws = dict(found=True, functions={"SendMessageToPC": {}}, constants={})
    check("R7: brackets inside a text value don't trigger a false warning",
          nwn_edit.verify_script('void main(){ SendMessageToPC(oPC, "Hello( \\" World("); }', nws) == [])
    # lints 17: cycles don't poison the depth memo
    class G:
        fwd = {"script:a": [("script:c", "include", "")], "script:b": [("script:c", "include", ""), ("script:z", "include", "")],
               "script:c": [("script:b", "include", "")], "script:z": [("script:z1", "include", "")],
               "script:z1": [("script:z2", "include", "")]}
    r1 = {i["label"]: i["detail"] for i in nwn_lints.include_depth(G, ["b", "a"], limit=5)}
    r2 = {i["label"]: i["detail"] for i in nwn_lints.include_depth(G, ["a", "b"], limit=5)}
    check("R7: include depth doesn't depend on which script is checked first", r1 == r2 and "5 deep" in r1.get("a", ""), (r1, r2))


def script_param_units():
    """Generic scripts configured by conversation node parameters (VAR_NAME, VALUE ...)."""
    import nwn_quests
    getloc = ('int StartingConditional(){ string sVar = GetScriptParam("VAR_NAME");\n'
              ' int nVal = StringToInt(GetScriptParam("VALUE")); int bSelf = StringToInt(GetScriptParam("SELF"));\n'
              ' object oPC = GetPCSpeaker();\n if (bSelf == 1) oPC = OBJECT_SELF;\n'
              ' if (GetLocalInt(oPC, sVar) == nVal) return TRUE; return FALSE; }')
    setloc = ('void main(){ string sVar = GetScriptParam("VAR_NAME"); int nVal = StringToInt(GetScriptParam("VALUE"));\n'
              ' SetLocalInt(GetPCSpeaker(), sVar, nVal); }')
    trig = {"getloc": [("dlg_script", "dlg:a", "EntryList[0]/RepliesList[0]/Active"),
                       ("dlg_script", "dlg:a", "EntryList[0]/RepliesList[1]/Active")],
            "setloc": [("dlg_script", "dlg:a", "EntryList[1]/Script")]}
    params = {("dlg:a", "EntryList[0]/RepliesList[0]/Active"): {"VAR_NAME": "Q_PLAYER", "VALUE": "1"},
              ("dlg:a", "EntryList[0]/RepliesList[1]/Active"): {"VAR_NAME": "NPC_MOOD", "VALUE": "1", "SELF": "1"},
              ("dlg:a", "EntryList[1]/Script"): {"VAR_NAME": "Q_PLAYER", "VALUE": "1"}}
    qi = nwn_quests.QuestInference({"getloc": getloc, "setloc": setloc}, trig, {"dlg:a": "a"},
                                   {"script:getloc", "script:setloc"}, {}, {}, {}, (), params)
    q = {x["id"]: x for x in qi.run()["quests"]}
    check("SP: SELF=1 on a node means the NPC's variable, not the player's", "local:pc:NPC_MOOD" not in q and
          q.get("local:pc:Q_PLAYER", {}).get("health") == "ok", (list(q), q.get("local:pc:Q_PLAYER", {}).get("problems")))
    q, _ = _qi({"a": 'void main(){ object oPC = GetPCSpeaker(); string sVar = "Q_SEER"; int nVal = 1;\n'
                     ' object oHide = GetItemInSlot(INVENTORY_SLOT_CARMOUR, oPC); SetLocalInt(oHide, sVar, nVal); }',
                "b": 'int StartingConditional(){ object oHide = GetPCHide(GetPCSpeaker()); return GetLocalInt(oHide, "Q_SEER") == 1; }'})
    check("SP: a name held in a local string (string sVar = \"Q_SEER\") is followed, value too",
          q.get("local:pc:Q_SEER", {}).get("values_set") == ["1"] and q["local:pc:Q_SEER"]["health"] == "ok",
          q.get("local:pc:Q_SEER"))
    q, _ = _qi({"a": 'void main(){ object oW = GetLocalObject(GetPCSpeaker(), "ACTIVATED_WIDGET"); SetLocalInt(oW, "Saddle", 1); }'})
    check("SP: state on an object remembered on the player (a widget) is not the player's", "local:pc:Saddle" not in q, list(q))
    qi = nwn_quests.QuestInference({"a": 'void main(){ object oPC = GetPCSpeaker(); DestroyObject(GetItemPossessedBy(oPC, "QITEM_HEAD")); SetLocalInt(oPC, "QH", 1); }',
                                    "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "QH") == 1; }'},
                                   {}, {}, {"script:a", "script:b"}, {}, {}, {}, (), {},
                                   {"QITEM_HEAD": ["cave.git Creature List[0]"]})
    qh = {x["id"]: x for x in qi.run()["quests"]}["local:pc:QH"]
    check("SP: an item copy retagged in an area counts as obtainable", qh["health"] == "ok" and
          any(i["name"] == "QITEM_HEAD" and i["status"] == "ok" and "cave.git" in i["detail"] for i in qh["items"]), qh["items"])

    # ---- edge cases found by an independent review (each reproduced first)
    sethide = open(os.path.join(HERE, "scenarios", "gen_c_sethideint.nss")).read() if os.path.isfile(
        os.path.join(HERE, "scenarios", "gen_c_sethideint.nss")) else SETHIDE
    def one(params_list, extra=None, live_dlg=False):
        trig = {"seth": [("dlg_script", "dlg:b", f"EntryList[{i}]/Script") for i in range(len(params_list))]}
        params = {("dlg:b", f"EntryList[{i}]/Script"): p_ for i, p_ in enumerate(params_list)}
        scripts = dict({"seth": sethide}, **(extra or {}))
        live = {f"script:{k}" for k in scripts} | ({"dlg:b"} if live_dlg else set())
        qi = nwn_quests.QuestInference(scripts, trig, {"dlg:b": "b"}, live, {}, {}, {}, (), params)
        return {x["id"]: x for x in qi.run()["quests"]}
    q = one([{"VAR_NAME": "Q_B", "VALUE": "1"}])
    check("R10 H1: sethideint VALUE=1 gives only stage 1 (not the INC or delete branches)",
          q.get("local:pc:Q_B", {}).get("values_set") == ["1"], q.get("local:pc:Q_B", {}).get("values_set"))
    q = one([{"VAR_NAME": "Q_B", "VALUE": "0"}])
    check("R10 H1: VALUE=0 runs the delete branch (stage 0)", q.get("local:pc:Q_B", {}).get("values_set") == ["0"],
          q.get("local:pc:Q_B", {}).get("values_set"))
    q = one([{"VAR_NAME": "Q_B", "VALUE": "1", "INC": "1"}])
    check("R10 H1: INC=1 adds to the old value (computed)", q.get("local:pc:Q_B", {}).get("values_set") == ["*"],
          q.get("local:pc:Q_B", {}).get("values_set"))
    q = one([{"VAR_NAME": "Q_B", "VALUE": "1"}], {"chk": 'int StartingConditional(){ object oHide = GetPCHide(GetPCSpeaker());'
                                                          ' return GetLocalInt(oHide, "Q_B") == 5; }'})
    check("R10 H1: a check that can never be true is caught again (no false '*')", q.get("local:pc:Q_B", {}).get("health") == "broken",
          q.get("local:pc:Q_B", {}).get("problems"))
    q, _ = _qi({"a": 'void main(){ object oPC = GetPCSpeaker(); int n = 1; n++; SetLocalInt(oPC, "Q_CNT", n); }',
                "b": 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q_CNT") == 2; }'})
    check("R10 M2: a counter (n++) is not taken for its first value", q.get("local:pc:Q_CNT", {}).get("health") != "broken",
          q.get("local:pc:Q_CNT", {}).get("problems"))
    q, qi = _qi({"inc": '//**************************************\n//  banner\n//**************************************\n'
                        'const int REGION_A = 1;\n/* real\n block */\nvoid main(){}',
                 "a": '#include "inc"\nvoid main(){ SetLocalInt(GetPCSpeaker(), "Q_R", REGION_A); }'})
    check("R10 M5: a //**** banner doesn't hide the constants below it", qi.consts.get("REGION_A", (0, ""))[1] == "1",
          qi.consts.get("REGION_A"))
    # H2 + M1: a generic script with a literal give of a missing blueprint keeps its warning; REWARD_ITEM gives count
    buy = ('void main(){ string sG = GetScriptParam("GUILD"); object oPC = GetPCSpeaker();\n'
           ' SetLocalInt(oPC, "CREDIT", 1);\n if (sG == "SMITH") CreateItemOnObject("letter_000", oPC); }')
    rew = ('void RewardItem(object oPC, string sRes){ CreateItemOnObject(sRes, oPC); }\n'
           'void main(){ string sR = GetScriptParam("REWARD_ITEM"); string sVar = GetScriptParam("VAR_NAME");\n'
           ' SetLocalInt(GetPCSpeaker(), sVar, 1); if (sR != "") RewardItem(GetPCSpeaker(), sR); }')
    chk = 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "CREDIT") == 1 && GetLocalInt(GetPCSpeaker(), "Q_L") == 1; }'
    need = ('void main(){ object oPC = GetPCSpeaker(); if (GetIsObjectValid(GetItemPossessedBy(oPC, "IT_LETTER")))'
            ' SetLocalInt(oPC, "Q_KB", 1); }')
    chk2 = 'int StartingConditional(){ return GetLocalInt(GetPCSpeaker(), "Q_KB") == 1; }'
    trig = {"buy": [("dlg_script", "dlg:c", "EntryList[0]/Script"), ("dlg_script", "dlg:c", "EntryList[1]/Script")],
            "rew": [("dlg_script", "dlg:c", "EntryList[2]/Script")]}
    params = {("dlg:c", "EntryList[0]/Script"): {"GUILD": "SMITH"}, ("dlg:c", "EntryList[1]/Script"): {"GUILD": "LUMBER"},
              ("dlg:c", "EntryList[2]/Script"): {"VAR_NAME": "Q_L", "REWARD_ITEM": "it_letter"}}
    letter = dict(resref="it_letter", tag="IT_LETTER", placed=False, carried_by=[], created_by=[])
    qi = nwn_quests.QuestInference({"buy": buy, "rew": rew, "chk": chk, "need": need, "chk2": chk2}, trig, {"dlg:c": "c"},
                                   {"script:buy", "script:rew", "script:chk", "script:need", "script:chk2"},
                                   {"IT_LETTER": [letter]}, {"it_letter": letter}, {}, (), params)
    q = {x["id"]: x for x in qi.run()["quests"]}
    cr = q.get("local:pc:CREDIT", {})
    check("R10 H2: a generic script's own literal give of a missing blueprint is still reported",
          any("letter_000" in p_["text"] for p_ in cr.get("problems", [])), cr.get("problems"))
    ql = q.get("local:pc:Q_L", {})
    check("R10 M1: REWARD_ITEM shows as given at that node", any(i["name"] == "it_letter" and i["role"] == "gives"
                                                                for i in ql.get("items", [])), ql.get("items"))
    kb = q.get("local:pc:Q_KB", {})
    check("R10 M1: an item handed out by REWARD_ITEM counts as obtainable", kb.get("health") == "ok", kb.get("problems"))
    # M3: the game's copy of a conversation wins even when it has no parameters
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, priority INTEGER);"
                     "CREATE TABLE files(id INTEGER PRIMARY KEY, source_id INTEGER, ext TEXT, node TEXT);"
                     "CREATE TABLE fields(file_id INTEGER, path TEXT, label TEXT, type TEXT, value TEXT);"
                     "INSERT INTO sources VALUES (1,'m','module',1000),(2,'h','hak',1);"
                     "INSERT INTO files VALUES (10,1,'dlg','dlg:x'),(11,2,'dlg','dlg:x');"
                     "INSERT INTO fields VALUES (10,'EntryList[0]/ActionParams[0]/Key','Key','',"
                     "'VAR_NAME'),(10,'EntryList[0]/ActionParams[0]/Value','Value','','Q_OLD');")
    check("R10 M3: parameters come only from the copy the game loads (the hak's, here without any)",
          nwn_quests.dialogue_params(db) == {}, nwn_quests.dialogue_params(db))


SETHIDE = (
    'void main()\n{\n    string sVar = GetScriptParam("VAR_NAME");\n    int nVal = StringToInt(GetScriptParam("VALUE"));\n'
    '    int bInc = StringToInt(GetScriptParam("INC"));\n    object oPC = GetPCSpeaker();\n'
    '    object oHide = GetItemInSlot(INVENTORY_SLOT_CARMOUR, oPC);\n    if (bInc == 1)\n    {\n'
    '        int nOldVal = GetLocalInt(oHide, sVar);\n        SetLocalInt(oHide, sVar, (nOldVal+nVal));\n    }\n'
    '    else if (nVal!=0) SetLocalInt(oHide, sVar, nVal);\n    //  If it is 0, delete the int\n'
    '    else DeleteLocalInt(oHide, sVar);\n}\n')


if __name__ == "__main__":
    sys.exit(main())
