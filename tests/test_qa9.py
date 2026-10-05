"""
Regressions found by independent testers who tried to break the toolkit on a large persistent world: names built at
run time, hostile files (GFF bombs, lying ERF headers, links, pickles), Find & replace corner cases, build progress,
facts limits, server settings and secrets, campaign databases, log lines, compiled scripts (.ncs), include
recompiles, heartbeats, and rules of the dashboard page.
    python tests/test_qa9.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import re
import shutil
import struct
import sys
import time
from unittest import mock

import nwnlib as n  # noqa: E402
import nwn_index  # noqa: E402
import nwn_analysis  # noqa: E402
from _gff import st, root, loc, R, X, LST, DW, B, I, w, stand_in_ncs  # noqa: E402
from _harness import check, skip, raw_zstd as _raw_zstd, e1_erf as _e1_erf  # noqa: E402

ROOT = h.ROOT
NO_COMP = dict(compiler=h.NO_COMPILER)          # audit_kwargs: the audit must not find a compiler installed here


TOK = '''const int QS_STR_LEN = 20;
const int QS_ANTS = 3;
const int QS_WOLF = 4;
const int QS_NEXT = 5; // added for the next quest, not used yet
void SetQToken(object oPC, int iPos = 0, string s = "1") { SetLocalString(oPC, "sQ", s); }
string GetQToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "sQ"), iPos, 1); }
'''
TOK2 = '''const int QS_STR_LEN = 10;
void SetQToken(object oPC, int iPos = 0, string s = "1") { SetLocalString(oPC, "sQ2", s); }
string GetQToken(object oPC, int iPos = 0) { return GetSubString(GetLocalString(oPC, "sQ2"), iPos, 1); }
'''
LONG = "x" * 320
SCRIPTS = {
    "inc_tok": TOK, "inc_tok2": TOK2,
    # a constant copied into a variable, then extended with += (the name is built at run time)
    "inc_names": 'const string CONV_PREFIX = "oldmine";\n',
    "mod_load": '#include "inc_names"\nvoid main()\n{\n    object oPC = GetFirstPC();\n    string sConv = CONV_PREFIX;\n'
                '    sConv += GetLocalString(oPC, "CONV_SUFFIX");\n    ActionStartConversation(oPC, sConv);\n}\n',
    # an item created through a helper function
    "inc_give": 'void GiveHide(object oPC, string sRef)\n{\n    CreateItemOnObject(sRef, oPC, 1);\n}\n',
    "q_give": '#include "inc_give"\nvoid main()\n{\n    GiveHide(GetPCSpeaker(), "hide_gob");\n    SetQToken(GetPCSpeaker(), QS_ANTS, "1");\n}\n',
    "q_ants": '#include "inc_tok"\nvoid main()\n{\n    SetQToken(GetPCSpeaker(), QS_ANTS, "1");\n}\n',
    "q_ants_sc": '#include "inc_tok"\nint StartingConditional()\n{\n    return GetQToken(GetPCSpeaker(), QS_ANTS) == "1" && GetQToken(GetPCSpeaker(), QS_WOLF) == "1";\n}\n',
    # an unused script checks a value nobody sets - dead code must not make the quest broken
    "old_ants_chk": '#include "inc_tok"\nint StartingConditional()\n{\n    return GetQToken(GetPCSpeaker(), QS_ANTS) == "7";\n}\n',
    # calls a function two includes define; it includes the second one
    "uses_tok2": '#include "inc_tok2"\nvoid main()\n{\n    SetQToken(GetFirstPC(), 1, "1");\n}\n',
    # CreateObject of a placeable
    "spawn_plc": 'void main()\n{\n    CreateObject(OBJECT_TYPE_PLACEABLE, "zz_gone_plc", GetLocation(OBJECT_SELF));\n}\n',
    # comparing text with a conversation's name is not starting it
    "chat_cmd": 'void main()\n{\n    string sCmd = GetPCChatMessage();\n    if (sCmd == "INNKEEPER") SpeakString("hi");\n}\n',
    # a local string constant started as a conversation (exists / missing)
    "start_const": 'void main()\n{\n    string sDlg = "innkeeper";\n    BeginConversation(sDlg);\n}\n',
    "start_missing": 'void main()\n{\n    string sDlg = "no_such_dlg";\n    BeginConversation(sDlg);\n}\n',
    # a very long line holding the name to rename
    "long_line": f'void main()\n{{\n    string s = "{LONG}"; int NEEDLE_X = 1; SetLocalInt(OBJECT_SELF, "NEEDLE_X", NEEDLE_X);\n}}\n',
    "needle_def": 'const int NEEDLE_X_UNUSED = 2;\nvoid main()\n{\n    int NEEDLE_X = 3;\n}\n',
    # a variable read under a near-typo of the one set
    # factions
    "join_bandits": 'void main()\n{\n    ChangeFaction(GetPCSpeaker(), GetObjectByTag("BANDIT_HOLDER"));\n}\n',
    "join_ghosts": 'void main()\n{\n    ChangeFaction(GetPCSpeaker(), GetObjectByTag("NO_HOLDER"));\n}\n',
    "go_hostile": 'void main()\n{\n    ChangeToStandardFaction(OBJECT_SELF, STANDARD_FACTION_HOSTILE);\n}\n',
    "use_nwnx": '#include "nwnx_creature"\nvoid main()\n{\n    SetModuleSwitch(MODULE_SWITCH_ENABLE_UMD_SCROLLS, TRUE);\n}\n',
    "traps_on": 'void main()\n{\n    SetLocalInt(GetArea(OBJECT_SELF), "UseSeTraps", 1);\n}\n',
    "traps_rd": 'void main()\n{\n    if (GetLocalInt(GetArea(OBJECT_SELF), "UseSeTrap")) SpeakString("x");\n    '
                'string s = GetLocalString(OBJECT_SELF, "anyname"); SetLocalInt(OBJECT_SELF, s, 1);\n}\n',
}


def build_module(mod):
    os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    for nm, src in SCRIPTS.items():
        w(mod, nm + ".nss", src)
        if "main" in src or "StartingConditional" in src:
            w(mod, nm + ".ncs", stand_in_ncs(nm))
    g("module.ifo", root("IFO ", Mod_Name=loc("QA9"), Mod_Entry_Area=(R, "town"), Mod_OnModLoad=(R, "mod_load"),
                         Mod_OnPlrChat=(R, "chat_cmd"), Mod_OnClientEntr=(R, "use_nwnx"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "town"))])))

    def entry(txt, script="", active=""):
        return st(0, Text=loc(txt), Speaker=(X, ""), Script=(R, script), Quest=(X, ""), QuestEntry=(DW, 0),
                  RepliesList=(LST, []))
    for d_ in ("oldmine_dlg", "innkeeper", "chief", "dead_dlg"):
        g(f"{d_}.dlg", root("DLG ", EndConversation=(R, ""), EndConverAbort=(R, ""),
                            EntryList=(LST, [entry("Hello", "q_ants" if d_ == "chief" else "")]), ReplyList=(LST, []),
                            StartingList=(LST, [st(0, Index=(DW, 0), Active=(R, "q_ants_sc" if d_ == "chief" else ""))])))
    names = ["PC", "Hostile", "Commoner", "Merchant", "Defender", "Bandits"]
    reps = {(1, 0): 0, (5, 0): 0, (2, 0): 50, (5, 4): 0, (4, 5): 0}          # (feeler, about) -> rep
    g("repute.fac", root("FAC ", FactionList=(LST, [st(0, FactionName=(X, nm), FactionGlobal=(DW, 0),
                                                         FactionParentID=(DW, 1 if nm == "Bandits" else 0xFFFFFFFF))
                                                      for nm in names]),
                         RepList=(LST, [st(0, FactionID1=(DW, about), FactionID2=(DW, feeler), FactionRep=(DW, v))
                                        for (feeler, about), v in reps.items()])))
    g("bandit.utc", root("UTC ", TemplateResRef=(R, "bandit"), Tag=(X, "BANDIT"), FirstName=loc("Bandit"), FactionID=(DW, 5)))
    g("chief.utc", root("UTC ", TemplateResRef=(R, "chief"), Tag=(X, "CHIEF"), FirstName=loc("Chief"), Conversation=(R, "chief")))
    g("hide_gob.uti", root("UTI ", TemplateResRef=(R, "hide_gob"), Tag=(X, "HIDE_GOB"), LocalizedName=loc("Hide"),
                           BaseItem=(I, 73), Cost=(DW, 1), StackSize=(B, 1)))
    g("my_boulder.utp", root("UTP ", TemplateResRef=(R, "my_boulder"), Tag=(X, "BOULDER"), LocName=loc("Boulder")))
    g("town.are", root("ARE ", Name=loc("Town"), Tag=(X, "TOWN"), ResRef=(R, "town"), Tileset=(R, "tcn01"),
                       Width=(I, 4), Height=(I, 4), OnEnter=(R, "traps_rd")))
    # a placed chief with its OWN conversation (the blueprint's is 'chief'); placeables with variables
    g("town.git", root("GIT ", **{"Creature List": (LST, [st(4, TemplateResRef=(R, "chief"), Tag=(X, "CHIEF2"),
                                                              FirstName=loc("Other"), Conversation=(R, "innkeeper")),
                                                          st(4, TemplateResRef=(R, "chief"), Tag=(X, "CHIEF3"),
                                                             FirstName=loc("Ghost"), Conversation=(R, "gone_dlg")),
                                                          st(4, TemplateResRef=(R, "bandit"), Tag=(X, "BANDIT_HOLDER"),
                                                             FirstName=loc("Holder"), FactionID=(DW, 5)),
                                                          st(4, TemplateResRef=(R, "bandit"), Tag=(X, "BROKEN"),
                                                             FirstName=loc("Broken"), FactionID=(DW, 77))]),
                                  "Placeable List": (LST, [st(9, TemplateResRef=(R, "plc_x"), Tag=(X, "VFXROCK"), Static=(B, 0),
                                                              Useable=(B, 0), LocName=loc("Rock"),
                                                              VarTable=(LST, [st(0, Name=(X, "ApplyVfx"), Type=(DW, 1), Value=(I, 5))]))])}))


def analyse(src, out):
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(src, [], [], None, out, False, False)
        return nwn_analysis.run_analysis(out, verbose=False)


def test_analysis(tmp):
    mod, out = os.path.join(tmp, "mod"), os.path.join(tmp, "ws", "qa9")
    build_module(mod)
    rep = analyse(mod, out)
    st_ = {d["node"]: d["status"] for d in rep["deletions"]}
    check("a name built from a constant copied into a variable (s = PFX; s += x) keeps matching resources off Safe",
          st_.get("dlg:oldmine_dlg") in (None, "Review"), st_.get("dlg:oldmine_dlg"))
    items = {i["resref"]: i for i in rep["items"]}
    check("an item created through a helper function counts as created", items.get("hide_gob", {}).get("created_by"),
          items.get("hide_gob"))
    iss = rep["issues"]
    mb = [i for i in iss if i["category"] == "missing_blueprint"]
    check("CreateObject(OBJECT_TYPE_PLACEABLE, ...) of a missing name is reported as a placeable (.utp)",
          any(i["node"] == "bp:zz_gone_plc.utp" for i in mb) and not any(i["node"] == "bp:zz_gone_plc.utc" for i in mb), mb)
    mi = [i for i in iss if i["category"] == "missing_include" and i["node"] == "script:uses_tok2"]
    check("a function defined by two includes is found in the one the script includes", not mi, mi)
    iq = {q["name"]: q for q in rep["inferred_quests"]["quests"]}
    ants = iq.get("QS_ANTS", {})
    check("a never-true check in an unused script doesn't make the quest broken",
          ants.get("health") != "broken" and any(p["level"] == "info" and "dead code" in p["text"] for p in ants.get("problems", [])),
          ants.get("problems"))
    tk = next((t for t in rep["var_audit"]["tokens"] if t["defined_in"] == "inc_tok"), None)
    named = {x for r in (tk or {}).get("slots", []) if r["status"] == "named, unused" for x in r["names"]}
    check("a slot name added after the last used one is 'named, unused', not free", "QS_NEXT" in named, tk and tk["free"][:5])
    conv = {c["name"]: c for c in rep["conversations"]}
    check("a text comparison (if (s == \"INNKEEPER\")) is not a 'started by'",
          not any(x["script"] == "chat_cmd" for x in conv["innkeeper"].get("started_by", [])), conv["innkeeper"].get("started_by"))
    check("a local string constant passed to BeginConversation is resolved",
          any(x["script"] == "start_const" and x["how"] == "starts it" for x in conv["innkeeper"].get("started_by", [])) and
          not any(x["script"] == "start_const" for x in rep.get("conversation_dynamic_starts", [])), conv["innkeeper"])
    check("... and a missing conversation started that way is reported",
          any(i["category"] == "missing_conversation" and "no_such_dlg" in i["detail"] for i in iss))
    check("a placed copy with its own conversation doesn't count as speaking the blueprint's",
          not conv["chief"].get("areas"), conv["chief"].get("areas"))
    va = {(v["name"]): v for v in rep["var_audit"]["variables"]}
    check("a name built entirely at run time doesn't hide a near-typo read (UseSeTrap vs UseSeTraps)",
          va.get("UseSeTrap", {}).get("level") == "warning", va.get("UseSeTrap"))
    check("a placeable carrying variables is never a Static candidate",
          rep["pw_performance"]["summary"].get("static_candidates") == 0, rep["pw_performance"]["summary"])
    fa = rep.get("factions") or {}
    F = {f["name"]: f for f in fa.get("factions", [])}
    b = F.get("Bandits", {})
    check("factions: repute.fac read with parent, feelings about players and members",
          b.get("parent_name") == "Hostile" and b.get("to_pc") == 0 and b.get("creatures_placed") == 1 and
          b.get("creature_blueprints") == 1, b)
    check("factions: how a faction feels about another is read the right way round",
          F["Bandits"]["feels"].get(4) == 0 and F["Commoner"]["to_pc"] == 50, (F["Bandits"]["feels"], F["Commoner"]["to_pc"]))
    sc = {x["script"]: x for x in fa.get("scripts", [])}
    check("factions: ChangeFaction through a holder tag is resolved to the faction",
          sc.get("join_bandits", {}).get("factions") == ["Bandits"], sc.get("join_bandits"))
    check("factions: ChangeToStandardFaction is resolved", sc.get("go_hostile", {}).get("factions") == ["Hostile"], sc.get("go_hostile"))
    fi = {i["category"] for i in iss}
    check("factions: a holder tag no creature has, and a faction number that doesn't exist, are reported",
          "faction_holder_missing" in fi and "faction_invalid" in fi, fi)
    check("every issue carries a suggested fix", all(i.get("fix") for i in iss),
          [i for i in iss if not i.get("fix")][:2])
    return mod, out, rep


def test_incomplete(tmp):
    src = os.path.join(tmp, "mod")
    noifo = os.path.join(tmp, "noifo")
    shutil.copytree(src, noifo)
    os.remove(os.path.join(noifo, "module.ifo"))
    rep = analyse(noifo, os.path.join(tmp, "ws", "noifo"))
    check("a module without module.ifo is reported and nothing is Safe",
          any(i["category"] == "module_incomplete" for i in rep["issues"]) and
          all(d["status"] == "Review" for d in rep["deletions"]), [d for d in rep["deletions"] if d["status"] != "Review"][:3])


def test_hostile(tmp):
    # GFF bombs and lying headers fail fast and small
    hdr = struct.pack("<4s4s12I", b"IFO ", b"V3.2", 56, 1, 68, 0, 68, 0xFFFFFFFF, 68, 0, 68, 0, 68, 0)
    t0 = time.time()
    try:
        n.read_gff(hdr + b"\0" * 64); ok_ = False
    except n.GffError as ex:
        ok_ = "does not fit" in str(ex)
    dt = time.time() - t0
    check("a GFF header with an absurd label count is refused", ok_)
    h.timing("a GFF header with an absurd label count is refused at once", dt, 1)
    # a list that reuses the same struct over and over (expands exponentially)
    root_ = n.GffRoot("UTI ", "V3.2")
    inner = n.GffStruct(0)
    inner.fields["X"] = n.GffField(n.INT, 1)
    root_.fields["L"] = n.GffField(n.LIST, [inner])
    data = bytearray(n.write_gff(root_))
    (s_off, s_cnt, f_off, f_cnt, l_off, l_cnt, fd_off, fd_len, fi_off, fi_len, li_off, li_len) = struct.unpack_from("<12I", data, 8)
    # point the list at struct 0 (the root itself) - a cycle; and a second copy pointing at struct 1 twice
    lst = struct.unpack_from("<2I", data, li_off)
    struct.pack_into("<2I", data, li_off, 1, 0)
    try:
        n.read_gff(bytes(data)); ok_ = False
    except n.GffError:
        ok_ = True
    check("a GFF whose list points back at itself is refused", ok_)
    bad = os.path.join(tmp, "count.mod")
    hdr = b"MOD V1.0" + struct.pack("<9I", 0, 0, 50_000_000, 160, 160, 160, 0, 0, 0) + b"\0" * 116
    with open(bad, "wb") as fh:
        fh.write(hdr)
    try:
        n.Erf(bad); ok_ = False
    except ValueError as ex:
        ok_ = "damaged" in str(ex)
    check("an ERF claiming 50 million entries is refused as damaged", ok_)
    empty = os.path.join(tmp, "empty.mod")
    open(empty, "wb").close()
    try:
        n.Erf(empty); ok_ = False
    except ValueError as ex:
        ok_ = "empty" in str(ex)
    check("an empty .mod says it is empty", ok_)


def test_refactor(tmp, out):
    import nwn_refactor as RF
    import nwn_edit
    # long lines are compared in full
    r = RF.find(out, "NEEDLE_X", "word", case=True, kinds=["script"])
    ids = [h["id"] for h in r["hits"]]
    p = RF.plan(out, "NEEDLE_X", "NEEDLE_Y", ids, "word", True, ["script"])
    check("a match on a line longer than 300 characters can be changed", p["refused"] == 0 and p["ok"] == len(ids),
          [c.get("why") for c in p["changes"] if not c.get("ok")])
    # your edited/new scripts are searched as they are now
    nwn_edit.save_text(out, "my_new.nss", "void main()\n{\n    SetLocalInt(OBJECT_SELF, \"NEEDLE_X\", 1);\n}\n", is_new=True)
    r = RF.find(out, "NEEDLE_X", "word", case=True, kinds=["script"])
    check("Find & replace searches your new/edited scripts too", any(h["file"] == "my_new.nss" for h in r["hits"]),
          [h["file"] for h in r["hits"]])
    # capitals differ -> refused for names
    nwn_edit.save_text(out, "caps.nss", "void main()\n{\n    SetLocalInt(OBJECT_SELF, \"needle_x\", 1);\n}\n", is_new=True)
    r = RF.find(out, "NEEDLE_X", "word", case=False, kinds=["script"])
    p = RF.plan(out, "NEEDLE_X", "NEEDLE_Z", [h["id"] for h in r["hits"] if h["file"] == "caps.nss"], "word", False, ["script"])
    check("a name with different capitals is not silently renamed", p["ok"] == 0 and "capitals" in p["changes"][0].get("why", ""),
          p["changes"])
    # line endings are kept
    nwn_edit.save_text(out, "crlf.nss", "x", is_new=True)
    with open(os.path.join(out, "edits", "crlf.nss"), "wb") as fh:
        fh.write(b"void main()\r\n{\r\n    SetLocalInt(OBJECT_SELF, \"NEEDLE_Q\", 1);\r\n}\r\n")
    r = RF.find(out, "NEEDLE_Q", "word", case=True, kinds=["script"])
    RF.apply(out, "NEEDLE_Q", "NEEDLE_R", [h["id"] for h in r["hits"]], "word", True, ["script"])
    data = open(os.path.join(out, "edits", "crlf.nss"), "rb").read()
    check("a replace keeps the file's CRLF line endings", b"NEEDLE_R" in data and data.count(b"\r\n") == 4, data)
    # a blueprint's own TemplateResRef can't be pointed at another name
    r = RF.find(out, "chief", "exact", case=True, kinds=["object"])
    own = [h for h in r["hits"] if h["file"] == "chief.utc" and h["where"] == "TemplateResRef"]
    p = RF.plan(out, "chief", "hide_gob", [h["id"] for h in own], "exact", True, ["object"])
    check("changing a blueprint's own TemplateResRef alone is refused", own and p["ok"] == 0, p["changes"])
    # rename to an existing name is refused in the preview already
    r = RF.find(out, "chief", "exact", case=True, kinds=["filename"])
    p = RF.plan(out, "chief", "innkeeper", [h["id"] for h in r["hits"] if h["file"] == "chief.dlg"], "exact", True, ["filename"])
    check("the preview refuses a rename onto an existing file", p["ok"] == 0 and "already exists" in p["changes"][0]["why"],
          p["changes"])
    # a new file with an impossible name is refused
    try:
        nwn_edit.save_text(out, "a b.nss", "void main(){}", is_new=True); ok_ = False
    except ValueError:
        ok_ = True
    check("a new resource name with a space is refused", ok_)
    # .new markers are not listed as edits; discarding a renamed file brings the old one back
    ed = nwn_edit.list_edits(out)
    check("My edits doesn't list .new marker files", not any(k.endswith(".new") for k in ed), list(ed))


def test_build_and_audit(tmp, mod, out):
    import nwn_build
    import nwn_edit
    nwn_edit.save_text(out, "q_ants.nss", SCRIPTS["q_ants"].replace('"1"', '"2"'))
    real_git, longest = nwn_build._git, []

    def spy(repo, *args):
        longest.append(sum(len(a) + 3 for a in args))
        return real_git(repo, *args)
    import nwn_progress
    tr = nwn_progress.BuildTracker(dict(index_s=10, post_s=3, analysis_s=6), lean=False)
    snaps = []
    orig_event = tr.event
    def ev(e):
        orig_event(e); snaps.append(tr.snapshot())
    nwn_progress.set_hook(ev)
    try:
        with mock.patch.object(nwn_build, "_git", spy), contextlib.redirect_stdout(io.StringIO()):
            log = nwn_build.build(out, {"delete": [], "merge": []}, verbose=False, progress=tr, audit_kwargs=NO_COMP)
    finally:
        nwn_progress.set_hook(None)
    tr.finish()
    st = {x["key"]: x["status"] for x in tr.snapshot()["steps"]}
    pcts = [x["pct"] for x in snaps if x.get("pct") is not None]
    check("build progress: every step is reported (files, pack, git, audit re-read, re-analysis, checks)",
          all(st.get(k) in ("done", "skipped") for k in st) and all(st.get(k) == "done" for k in
          ("files", "pack", "audit_index", "audit_analysis", "audit_checks")), st)
    check("build progress: % only goes up and stays under 100 until finished, with a time-left text",
          pcts and all(b >= a - 0.5 for a, b in zip(pcts, pcts[1:])) and max(pcts) < 100 and
          all("left" in x["eta_text"] or "longer" in x["eta_text"] for x in snaps), pcts[:40])
    check("build: git is never given a command line Windows would refuse (WinError 206)",
          longest and max(longest) < 8000 and log.get("git", {}).get("commit"), (max(longest or [0]), log.get("git")))
    ec = next((c for c in log["audit"]["checks"] if c["id"] == "edits_compiled"), {})
    check("the audit warns when an edited script still has the original compiled .ncs",
          ec.get("status") == "WARN" and any("ORIGINAL compiled" in e for e in ec.get("evidence", [])), ec)
    try:
        nwn_build.build(out, {"delete": [], "merge": []}, out_dir=os.path.dirname(mod), name=os.path.basename(mod), verbose=False)
        ok_ = False
    except ValueError:
        ok_ = True
    check("a build can never target the original module", ok_ and os.path.isfile(os.path.join(mod, "module.ifo")))


def test_facts_mcp(tmp, out):
    import nwn_facts
    ws = os.path.dirname(out)
    f = nwn_facts.Facts("qa9", workspace=ws)
    e = f.exists("gone_dlg")
    check("a conversation that is only referenced is not 'found'", not e["found"] and e["referenced_but_missing"], e)
    e = f.exists("QS_ANTS")
    check("exists knows script constants", any(m["kind"] == "constant" and m["value"] == "3" for m in e["matches"]), e)
    t0 = time.time()
    try:
        f.grep(r"(x+x+)+y", timeout=3); ok_ = False        # backtracks exponentially on the 320-x line
    except ValueError as ex:
        ok_ = "longer than" in str(ex)
    dt = time.time() - t0
    check("a pathological grep pattern is stopped by its time limit (no hang)", ok_)
    h.timing("a grep stopped by a 3 s time limit returns within 15 s", dt, 15)
    try:
        nwn_facts.Facts(os.path.abspath(out), workspace=os.path.join(tmp, "elsewhere")); ok_ = False
    except ValueError:
        ok_ = True
    check("the facts tools don't open analyses outside the workspace", ok_)
    try:
        f.compiled("../../etc/passwd"); ok_ = False
    except ValueError:
        ok_ = True
    check("compile status refuses path-like script names", ok_)
    f.close()


def test_housekeep(tmp):
    import nwn_housekeep as HK
    folder = os.path.join(tmp, "mods"); os.makedirs(folder)
    ws = os.path.join(tmp, "hkws"); os.makedirs(ws)
    old = time.time() - 30 * 86400
    for nm, data, age in (("Live.mod", b"MOD A", 10), ("Live_old.mod", b"MOD B", 20), ("Test Server.mod", b"MOD A", 5)):
        p = os.path.join(folder, nm)
        open(p, "wb").write(data)
        os.utime(p, (old - age * 86400, old - age * 86400))
    r = HK.scan(folder, ws, keep_newest=1)
    live = next(f for f in r["files"] if f["name"] == "Live.mod")
    check("the newest file of a family is never suggested, even when an identical copy exists", not live["suggest"], live)
    b = HK.move(folder, ["Live_old.mod"], ws)
    man = os.path.join(b["dest"], "manifest.json")
    j = json.load(open(man))
    victim = os.path.join(tmp, "victim.txt"); open(victim, "w").write("secret")
    j["entries"].append(dict(name="evil.mod", src=os.path.join(tmp, "stolen.mod"), dst=victim, size=6, status="moved"))
    json.dump(j, open(man, "w"))
    HK.undo(man, ws)
    check("Undo ignores paths written into the manifest", os.path.isfile(victim) and not os.path.exists(os.path.join(tmp, "stolen.mod"))
          and os.path.isfile(os.path.join(folder, "Live_old.mod")))


def test_settings(tmp, out):
    import nwn_modsettings as M
    import nwnlib as n_
    rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    db = n_.sqlite_ro(os.path.join(out, "index.sqlite"))
    try:
        ms = M.module_summary(db, rep)
        check("settings: module.ifo settings and events are listed",
              any(x["key"] == "Mod_Name" for x in ms["settings"]) and any(e["script"] == "mod_load" for e in ms["events"]), ms["events"])
        check("settings: switches set by scripts are listed", any(x["name"] == "MODULE_SWITCH_ENABLE_UMD_SCROLLS" for x in ms["script_sets"]),
              ms["script_sets"])
        env = M.parse_server_file("services:\n  nw:\n    environment:\n      - NWN_ELC=1\n      - NWN_DMPASSWORD=hunter2\n"
                                  "      - NWNX_CORE_SKIP_ALL=y\n", "docker-compose.yml")
        check("settings: secrets are never kept", env["values"]["NWN_DMPASSWORD"] == "(set - hidden)" and
              "hunter2" not in json.dumps(env), env)
        check("settings: YAML headings are not read as settings", "environment" not in env["values"], env["values"])
        tml2 = M.parse_server_file('[[db]]\nname = "a"\npassword = "hunter3"\n[server]\n'
                                   'url = "mysql://nwn:hunter4@db.local:3306/pw"\n', "settings.tml")
        check("settings: secrets inside a list of tables ([[x]]) and inside URLs are never kept",
              "hunter3" not in json.dumps(tml2) and "hunter4" not in json.dumps(tml2) and
              "db.local" in json.dumps(tml2), tml2["values"])
        tml = M.parse_server_file("[server]\npvp-mode = 2\n[ruleset]\nenforce-legal-characters = true\n", "settings.tml")
        r = M.interpret([tml, env], db, rep["summary"], M.unused_scripts(rep))
        rows = {x["setting"]: x["value"] for x in r["rows"]}
        check("settings: settings.tml and the environment are read in plain words",
              rows.get("Player vs player") == "full PvP" and rows.get("Enforce legal characters (ELC)") == "on", rows)
        check("settings: a NWNX plugin the scripts use but the server switches off is reported",
              any("CREATURE" in f["text"].upper() and f["severity"] == "warning" for f in r["findings"]), r["findings"])
        check("settings: plugins that are off are one finding, not one per plugin",
              sum(1 for f in r["findings"] if "NWNX plugin" in f["text"]) == 1, r["findings"])
        r2 = M.interpret([tml, env], db, rep["summary"], M.unused_scripts(rep) | {"use_nwnx"})
        check("settings: a plugin used only by scripts nothing uses is not a warning",
              not any(f["severity"] == "warning" for f in r2["findings"]) and
              any(p_["plugin"] == "CREATURE" and p_["unused_scripts"] == 1 and p_["scripts"] == 0 for p_ in r2["nwnx"]), r2)
    finally:
        db.close()


def _cpdb(data):
    z = _raw_zstd(data)
    return b"CPDB" + struct.pack("<IIIII", 3, 2, len(data), 1, 0) + z


def _dbf(path, rows):
    """A minimal BioWare/FoxPro campaign .DBF + .FPT pair."""
    fields = [("VARNAME", "C", 32, 0), ("PLAYERID", "C", 32, 0), ("TIMESTAMP", "C", 16, 0), ("VARTYPE", "C", 1, 0),
              ("INT", "N", 10, 0), ("DBL1", "N", 20, 10), ("MEMO", "M", 10, 0)]
    rlen = 1 + sum(f[2] for f in fields)
    hlen = 32 + 32 * len(fields) + 1
    head = struct.pack("<BBBBIHH", 0xF5, 126, 1, 1, len(rows), hlen, rlen) + b"\0" * 20
    fd, pos = b"", 1
    for name, t, ln, dec in fields:
        fd += name.encode().ljust(11, b"\0") + t.encode() + struct.pack("<I", pos) + bytes([ln, dec]) + b"\0" * 14
        pos += ln
    memo = bytearray(struct.pack(">IxxH", 1, 64) + b"\0" * 56)
    recs = b""
    for vn, pid, vt, val in rows:
        mi = b""
        if vt == "S":
            idx = len(memo) // 64
            body = struct.pack(">II", 1, len(val)) + val.encode()
            memo += body + b"\0" * (-len(body) % 64)
            mi = str(idx).encode()
        recs += b" " + vn.encode().ljust(32) + pid.encode().ljust(32) + b"10/05/1917:21:37" + vt.encode() + \
            (str(val).encode().rjust(10) if vt == "I" else b" " * 10) + b" " * 20 + mi.rjust(10)
    open(path, "wb").write(head + fd + b"\x0d" + recs + b"\x1a")
    open(os.path.splitext(path)[0] + ".FPT", "wb").write(bytes(memo))


def test_database(tmp):
    import hashlib
    import sqlite3
    import nwn_database as D
    import nwn_zstd
    check("zstd: a stored-block frame decodes", nwn_zstd._py_decompress(_raw_zstd(b"hello world"), 1000) == b"hello world")
    rle = struct.pack("<I", 0xFD2FB528) + bytes([0x20, 50]) + (1 | (1 << 1) | (50 << 3)).to_bytes(3, "little") + b"z"
    check("zstd: an RLE block decodes", nwn_zstd._py_decompress(rle, 1000) == b"z" * 50)
    try:
        import zstandard
        src = open(os.path.join(ROOT, "nwn_database.py"), "rb").read()
        ok_ = all(nwn_zstd._py_decompress(zstandard.compress(src[:k], lv), 1 << 22) == src[:k]
                  for k in (10, 999, 30000, len(src)) for lv in (1, 9, 19))
        check("zstd: matches the reference library on real text (all levels)", ok_)
    except ImportError:
        skip("zstd: matches the reference library on real text (all levels)",
             "the zstandard package is not installed here (pip install zstandard)")
    bad = bytearray(_raw_zstd(b"abcdef")); bad[6] |= 0b110      # reserved block type
    try:
        nwn_zstd._py_decompress(bytes(bad), 1000); failed = False
    except nwn_zstd.ZstdError:
        failed = True
    check("zstd: damaged data is refused, not guessed", failed)

    folder = os.path.join(tmp, "database")
    os.makedirs(folder)
    ee = os.path.join(folder, "qa9db.sqlite3")
    con = sqlite3.connect(ee)
    con.execute("CREATE TABLE db (varname varchar not null, playerid varchar not null, vartype tinyint not null, "
                "payload blob not null, compressed bool not null default false, unique(varname, playerid))")
    con.execute("CREATE TABLE meta (key varchar, value varchar)")
    con.execute("INSERT INTO meta VALUES ('import_done', 'yes')")
    item = n.write_gff(root("UTI ", LocalizedName=loc("Magic Sword"), Tag=(X, "SWORD1"), TemplateResRef=(R, "nw_wswls001")))
    rows = [("done", "pcA", 73, 3, 0), ("title", "pcA", 83, _cpdb(b"Hero of Oakvale"), 1),
            ("stash", "pcA", 79, _cpdb(item), 1), ("admin_password", "", 83, b"hunter2", 0),
            ("old_value", "", 73, 1, 0)]
    con.executemany("INSERT INTO db VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE TABLE bank (owner text, gold int)")
    con.execute("INSERT INTO bank VALUES ('pcA', 100)")
    con.commit(); con.close()
    _dbf(os.path.join(folder, "QA9DB.DBF"), [("done", "pcA", "I", 2)])
    _dbf(os.path.join(folder, "OTHERMOD.DBF"), [("note", "", "S", "left by another module")])
    before = {f: hashlib.sha256(open(os.path.join(folder, f), "rb").read()).hexdigest() for f in os.listdir(folder)}
    snap = D.read_path(folder)
    after = {f: hashlib.sha256(open(os.path.join(folder, f), "rb").read()).hexdigest() for f in os.listdir(folder)}
    check("database: reading never changes or adds files in the folder", before == after, sorted(after))
    v = snap["campaign"]["qa9db"]["vars"]
    check("database: EE values are read (int, compressed string, stored object)",
          v["done"]["examples"][0]["value"] == 3 and v["title"]["examples"][0]["value"] == "Hero of Oakvale"
          and "Magic Sword" in v["stash"]["examples"][0]["value"] and "SWORD1" in v["stash"]["examples"][0]["value"], v)
    check("database: an old .DBF copy of the same database is not counted twice", v["done"]["rows"] == 1, v["done"])
    check("database: secrets are never kept", v["admin_password"]["examples"][0]["value"] == "(set - hidden)" and
          "hunter2" not in json.dumps(snap, default=str))
    check("database: old BioWare .DBF files are read (memo text)",
          snap["campaign"]["othermod"]["vars"]["note"]["examples"][0]["value"] == "left by another module", snap["campaign"].get("othermod"))
    check("database: SQL tables in a campaign database are listed", snap["tables"]["qa9db"]["bank"]["rows"] == 1, snap["tables"])

    idx = sqlite3.connect(":memory:")
    idx.execute("CREATE TABLE scripts (name TEXT, source TEXT)")
    srcs = {
        "qa_store": 'void main(){ object oPC = GetPCSpeaker(); SetCampaignInt("QA9DB", "done", 3, oPC);\n'
                    'StoreCampaignObject("QA9DB", "stash", GetItemPossessedBy(oPC, "x"), oPC); }',
        "qa_read": 'void main(){ object oPC = GetPCSpeaker(); if (GetCampaignInt("QA9DB", "done", oPC) == 3) {}\n'
                   'string s = GetCampaignString("QA9DB", "done", oPC);\n'
                   'int n = GetCampaignInt("QA9DB", "d0ne", oPC); // typo\n'
                   'int t = GetCampaignInt("QA9DB", "MYQUEST", oPC); }',
        "qa_inc": 'const int MYQUEST = 5;\nconst string QA_DB = "QA9DB";\n'
                  'void SaveFlag(string sDB, string sVar, object oPC) { SetCampaignInt(sDB, sVar, 1, oPC); }',
        "qa_helper": '#include "qa_inc"\nvoid main(){ SaveFlag(QA_DB, "via_helper", GetPCSpeaker()); '
                     'if (GetCampaignInt(QA_DB, "via_helper", GetPCSpeaker())) {} }',
        "qa_sql": 'void main(){ sqlquery q = SqlPrepareQueryCampaign("QA9DB", "SELECT gold FROM bank WHERE owner = @o"); '
                  'sqlquery r = SqlPrepareQueryCampaign("QA9DB", "SELECT x FROM vault"); }',
    }
    idx.executemany("INSERT INTO scripts VALUES (?,?)", srcs.items())
    uses = D.script_uses(idx)
    r = D.cross_check(snap, uses, set(), D.int_constants(idx))
    cats = {(f["category"], f["text"].split("'")[1] if "'" in f["text"] else "") for f in r["findings"]}
    check("database: a value read but never written is a warning", ("db_read_never_written", "d0ne") in cats, cats)
    check("database: written as int, read as string is a warning", ("db_type_mismatch", "done") in cats, cats)
    check("database: names passed through a helper function are followed (no false warning)",
          not any(c[1] == "via_helper" for c in cats) and any(u.get("via") == "qa_inc" and u["var"] == "via_helper"
                                                               for u in uses), [u for u in uses if u.get("via")])
    tok = next((f for f in r["findings"] if "MYQUEST" in f["text"]), {})
    check("database: a read that matches a quest token constant says so, with the module's token getter as the fix",
          "token getter" in tok.get("fix", "") and "MYQUEST" in tok.get("fix", ""), tok)
    check("database: stored values no script uses are reported as info",
          any(f["category"] == "db_stored_unused" and "old_value" in f["examples"] for f in r["findings"]), r["findings"])
    check("database: a database only another module uses is info, not a warning",
          any(f["category"] == "db_not_used" and f["db"] == "othermod" and f["severity"] == "info" for f in r["findings"]))
    check("database: the old .DBF copy next to an imported .sqlite3 is explained",
          any(f["category"] == "db_legacy_copy" and "import done" in f["text"] for f in r["findings"]))
    sq = next((x for x in r["sql"] if x["target"] == "qa9db"), {})
    check("database: SQL tables are read from queries and compared with the file",
          "bank" in sq.get("tables", []) and sq.get("missing") == ["vault"], r["sql"])
    r2 = D.cross_check(snap, uses, {"qa_read"}, D.int_constants(idx))
    check("database: scripts nothing runs don't raise warnings",
          not any(f["severity"] == "warning" for f in r2["findings"]), r2["findings"])


def test_logs(tmp):
    import nwn_logs
    lines = [
        "E [00:40:16] ScriptVM (nwsvmachinecommands.cpp:264:ReportError): Script error: ms_onmodload OID: 00000000, Tag: , ERROR: IP OUT OF CODE SEGMENT",
        "E [00:40:16] ScriptVM (scriptvmcore.cpp:2189:RunScript): script ms_onmodload at level 0 on object 0x00000000 took 57830 us, error: -646",
        "E [00:40:17] ScriptVM (nwsvmachinecommands.cpp:264:ReportError): Script error: update_world OID: 00000000, Tag: , SCRIPT_ABORT: An internal NWNX function was called without NWNX running.",
        "E [00:40:37] (twodimarray.cpp:940:Load2DArray): Failed to demand gosslulncomm.2da",
        "E [00:40:37] (twodimarray.cpp:940:Load2DArray): Failed to demand gosslulncomm.2da",
        "I [00:39:27] (exosoundinternal_openal.cpp:116:AL_ERROR_CHECK): Sound/OpenAL Error: 40964",
        "D [00:39:34] ASSERT (nui.cpp:83:~Window) #0: !(m_initialized) stack[15]{00007FF64478FEAC}",
        "snaf_hl_ticker.nss: ERROR: NO FUNCTION MAIN() IN SCRIPT",
    ]
    d = os.path.join(tmp, "logs"); os.makedirs(d)
    open(os.path.join(d, "nwengineLog.txt"), "w").write("\n".join(lines) + "\n")
    m = nwn_logs.LogMonitor(None, [d]); m.poll(); g = m.report()["groups"]
    by = {(x["kind"], x.get("script") or x.get("resource")): x for x in g}
    check("logs: EE 'Script error: <name> OID:' lines name the real script (not 'error')",
          ("script_error", "ms_onmodload") in by and not any(x.get("script") == "error" for x in g), list(by))
    check("logs: the '-646' follow-up line is tied to its script and explained",
          (by.get(("script_stopped", "ms_onmodload")) or {}).get("explain", {}).get("title", "").startswith("The script stopped"), by.keys())
    check("logs: every error row says what it means, whether it matters, and how to fix it",
          all(x.get("explain") and x["explain"].get("fix") and x["explain"].get("matters") for x in g),
          [(x["message"], bool(x.get("explain"))) for x in g])
    check("logs: missing 2da tables are read ('Failed to demand') and counted",
          (by.get(("missing_resource", "gosslulncomm.2da")) or {}).get("count") == 2, by.keys())
    check("logs: sound driver / client self-checks / include files are marked harmless",
          all(x["severity"] == "info" for x in g if "40964" in x["message"] or "nui.cpp" in x["message"] or
              "NO FUNCTION MAIN" in x["message"]), [(x["message"][:30], x["severity"]) for x in g])


def test_editor_haks(tmp):
    import sqlite3
    import nwn_build
    hd = os.path.join(tmp, "hakdir"); os.makedirs(hd)
    orig = os.path.join(hd, "acpv41.hak"); new = os.path.join(hd, "acpv41_r1.hak")
    n.write_erf(orig, [("a", "txt", b"1")], file_type="HAK "); n.write_erf(new, [("a", "txt", b"2")], file_type="HAK ")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('haks', ?)", (json.dumps([orig, os.path.join(hd, "other.hak")]),))
    ifo = n.GffRoot("IFO ")
    lst = []
    for hk in ("acpv41_r1", "other"):
        st_ = n.GffStruct(8); st_.set("Mod_Hak", n.CEXOSTRING, hk); lst.append(st_)
    ifo.set("Mod_HakList", n.LIST, lst)
    log = {"warnings": []}
    ed, repl = nwn_build._editor_haks([("module", "ifo", n.write_gff(ifo))], db, log)
    check("build: a Hak-editor rebuild listed in module.ifo replaces the analysed hak (not slimmed, audited with it)",
          ed == {"acpv41": new} and repl == {"acpv41"} and not log["warnings"], (ed, repl, log))


def test_db_upload(out):
    import nwn_dashboard as D
    with mock.patch.object(D, "WORKSPACE", os.path.dirname(out)):
        a = os.path.basename(out)
        bad = []
        for nm in ("../evil.sqlite3", "notes.txt", "..\\x.dbf", ".hidden.db"):
            try:
                D.upload_database_file({"a": a, "name": nm, "start": "1"}, b"x"); bad.append(nm)
            except ValueError:
                pass
        check("database upload: only database file names, never a path", not bad, bad)
        import sqlite3
        tmpdb = os.path.join(out, "t.sqlite3")
        c = sqlite3.connect(tmpdb); c.execute("CREATE TABLE db (varname, playerid, vartype, payload, compressed)")
        c.execute("INSERT INTO db VALUES ('x', '', 73, 7, 0)"); c.commit(); c.close()
        D.upload_database_file({"a": a, "name": "qa9db.sqlite3", "start": "1"}, open(tmpdb, "rb").read())
        os.remove(tmpdb)
        r = D.load_database({"a": a, "uploaded": True})
        check("database upload: picked files are read, then their copies removed",
              r["summary"]["files"] == 1 and not os.path.exists(os.path.join(out, D.DB_UPLOAD)), r["summary"])
        D.load_database({"a": a, "clear": True})


def test_ncs_dashboard(out):
    """The script panel's Compiled code reply, against the QA9 analysis (its module has stand-in .ncs files)."""
    import nwn_dashboard as D
    with mock.patch.object(D, "WORKSPACE", os.path.dirname(out)):
        a = os.path.basename(out)
        c = D.compiled_code(a, "mod_load")
        check("Compiled code: a module script's .ncs is found, checked and (without nwn_asm) explained",
              c.get("found") and c.get("ok") and c.get("instructions") == 3 and
              (c.get("listing") or "nwn_asm" in (c.get("listing_error") or "")), c)
        bad = [x for x in ("../x", "a b", "x" * 17, "") if not D.compiled_code(a, x).get("error")]
        check("Compiled code: only plain script names are accepted", not bad, bad)
        check("Compiled code: a script with no .ncs says it can't run", "can't run" in D.compiled_code(a, "zz_none")["error"])


def test_include_recompile(tmp):
    import nwn_build
    import nwn_edit
    comp = h.fake_compiler(os.path.join(tmp, "fake_comp"))     # writes .ncs that depend on the includes; BROKEN fails
    mod = os.path.join(tmp, "incmod"); os.makedirs(mod)
    srcs = {"inc_q": 'void QFlag(object o) { SetLocalInt(o, "Q", 1); }\n',
            "inc_mid": '#include "inc_q"\nvoid Mid(object o) { QFlag(o); }\n',
            "user_a": '#include "inc_q"\nvoid main() { QFlag(OBJECT_SELF); }\n',
            "user_b": '#include "inc_mid"\nvoid main() { Mid(OBJECT_SELF); }\n',
            "user_c": '#include "inc_q"\nint StartingConditional() { return TRUE; }\n',
            "other": 'void main() { }\n'}
    for nm, src in srcs.items():
        w(mod, nm + ".nss", src)
        if "main" in src or "StartingConditional" in src:
            w(mod, nm + ".ncs", b"NCS V1.0 original " + nm.encode())
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Inc"), Mod_OnModLoad=(R, "user_a"),
                                            Mod_OnClientEntr=(R, "user_b"), Mod_OnHeartbeat=(R, "other"))))
    out = os.path.join(tmp, "incmod_an")
    analyse(mod, out)
    nwn_edit.save_text(out, "inc_q.nss", srcs["inc_q"].replace('"Q", 1', '"Q", 2'))
    # no compiler: refused before anything is written
    try:
        nwn_build.build(out, {"delete": [], "merge": []}, verbose=False, audit_kwargs=dict(compiler=os.path.join(tmp, "nope")))
        refused = False
    except ValueError as ex:
        refused = "nwn_script_comp" in str(ex)
    check("build: an edited include without the compiler is refused before anything is written",
          refused and not os.path.exists(os.path.join(out, "build")), refused)
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, {"delete": [], "merge": []}, verbose=False, audit_kwargs=dict(compiler=comp))
    clean = log["output"]
    check("build: every script that includes the edited include is recompiled (directly or through another include)",
          log.get("include_users") == ["user_a", "user_b", "user_c"] and
          set(log.get("compiled", [])) == {"user_a", "user_b", "user_c"}, (log.get("include_users"), log.get("compiled")))
    check("build: the recompiled .ncs files are new; unrelated scripts keep theirs",
          all(not open(os.path.join(clean, f"{x}.ncs"), "rb").read().startswith(b"NCS V1.0 original") for x in ("user_a", "user_b", "user_c"))
          and open(os.path.join(clean, "other.ncs"), "rb").read() == b"NCS V1.0 original other")
    ck = {c["id"]: c for c in log["audit"]["checks"]}
    check("audit: 'every includer recompiled' passes", ck.get("include_users", {}).get("status") == "PASS", ck.get("include_users"))
    # a compile failure in one includer: the audit FAILs, naming it
    nwn_edit.save_text(out, "inc_q.nss", srcs["inc_q"] + "// BROKEN\n")
    with contextlib.redirect_stdout(io.StringIO()):
        log2 = nwn_build.build(out, {"delete": [], "merge": []}, verbose=False, audit_kwargs=dict(compiler=comp))
    ck2 = {c["id"]: c for c in log2["audit"]["checks"]}
    check("audit: FAIL when an includer couldn't be recompiled (it would run the old code)",
          ck2.get("include_users", {}).get("status") == "FAIL" and log2["audit"]["verdict"] == "FAIL" and
          any("user_a" in e for e in ck2["include_users"]["evidence"]), ck2.get("include_users"))


def test_heartbeats(tmp):
    import nwn_perf
    import sqlite3
    mod = os.path.join(tmp, "hbmod"); os.makedirs(mod)
    srcs = {"hb_walk": 'void main() { object o = GetFirstObjectInArea(); while (GetIsObjectValid(o)) { o = GetNextObjectInArea(); } }\n',
            "hb_light": 'void main() { if (!GetIsObjectValid(GetFirstPC())) return; SetLocalInt(OBJECT_SELF, "X", 1); }\n',
            "hb_inc": 'void Scan() { object o = GetFirstObjectInShape(SHAPE_SPHERE, 5.0, GetLocation(OBJECT_SELF)); }\n',
            "hb_via": '#include "hb_inc"\nvoid main() { Scan(); }\n'}
    for nm, src in srcs.items():
        w(mod, nm + ".nss", src)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("HB"), Mod_Entry_Area=(R, "town"), Mod_OnHeartbeat=(R, "hb_light"),
                                            Mod_Area_list=(LST, [st(6, Area_Name=(R, "town"))]))))
    w(mod, "town.are", n.write_gff(root("ARE ", Name=loc("Town"), Tag=(X, "TOWN"), ResRef=(R, "town"), Tileset=(R, "tcn01"),
                                         Width=(I, 4), Height=(I, 4))))
    plcs = [st(9, TemplateResRef=(R, "plc_x"), Tag=(X, f"P{i}"), LocName=loc("P"), OnHeartbeat=(R, "hb_walk"))
            for i in range(5)] + [st(9, TemplateResRef=(R, "plc_x"), Tag=(X, "V"), LocName=loc("V"), OnHeartbeat=(R, "hb_via"))]
    w(mod, "town.git", n.write_gff(root("GIT ", **{"Placeable List": (LST, plcs)})))
    out = os.path.join(tmp, "hbmod_an")
    rep = analyse(mod, out)
    H = {h["script"]: h for h in (rep.get("pw_performance") or {}).get("heartbeats", [])}
    check("heartbeats: every heartbeat script is ranked with copies and runs per minute",
          H.get("hb_walk", {}).get("copies") == 5 and H["hb_walk"]["runs_per_min"] == 50 and "hb_light" in H, H)
    check("heartbeats: walking every object makes a run costly, and the fix says to add an early exit",
          H["hb_walk"]["weight"] > H["hb_light"]["weight"] and "early exit" in H["hb_walk"]["fix"] and
          any("walks every object" in f for f in H["hb_walk"]["factors"]), H["hb_walk"])
    check("heartbeats: a costly call inside an included function counts", any("shape" in f for f in H.get("hb_via", {}).get("factors", [])),
          H.get("hb_via"))
    check("heartbeats: an early return is recognised", H["hb_light"]["early_exit"] and not H["hb_walk"]["early_exit"])
    check("heartbeats: shares add up and the heaviest comes first",
          abs(sum(h["share"] for h in H.values()) - 100) < 1 and next(iter(H)) == "hb_walk", [(k, v["share"]) for k, v in H.items()])


def test_rmtree_force(tmp):
    d = os.path.join(tmp, "ro_tree", ".git", "objects", "ab"); os.makedirs(d)
    f = os.path.join(d, "cd"); open(f, "w").write("x"); os.chmod(f, 0o444)
    real, calls = os.unlink, []

    def flaky(p_, *a, **k):
        if str(p_).endswith("cd"):
            calls.append(p_)
            if len(calls) == 1:
                raise PermissionError(13, "Access is denied", p_)     # what Windows says for git's read-only files
        return real(p_, *a, **k)
    # shutil.rmtree (inside rmtree_force) calls os.unlink itself, so os.unlink is the narrowest place to fail it.
    # Nothing else runs in this suite meanwhile (no server or job threads), and the patch ends with the call.
    with mock.patch.object(os, "unlink", flaky):
        n.rmtree_force(os.path.join(tmp, "ro_tree"))
    check("delete: read-only files (git history in clean builds) no longer stop a delete on Windows",
          not os.path.exists(os.path.join(tmp, "ro_tree")) and len(calls) == 2, calls)


def test_compressed_erf(tmp):
    import zlib
    raw = b"void main() { }\n" * 10          # under 256 bytes (the test zstd frame stores a 1-byte size)

    def xres(algo, body):
        return b"XRES" + struct.pack("<3I", 3, algo, len(raw)) + body
    files = [("plain", "nss", raw, False, len(raw)),
             ("none_c", "nss", xres(0, raw), True, len(raw)),
             ("zlib_c", "nss", xres(1, struct.pack("<I", 1) + zlib.compress(raw)), True, len(raw)),
             ("zstd_c", "nss", xres(2, struct.pack("<II", 1, 0) + _raw_zstd(raw)), True, len(raw))]
    p = os.path.join(tmp, "comp.hak")
    _e1_erf(p, files)
    erf = n.Erf(p)
    got = {e.filename: erf.read(e) for e in erf.entries}
    erf.close()
    check("compressed haks (EE E1.0): none / zlib / zstd entries are read directly",
          all(v == raw for v in got.values()) and len(got) == 4, {k: len(v) for k, v in got.items()})


def _ncs(body, size=None):
    return b"NCS V1.0B" + struct.pack(">I", size if size is not None else 13 + len(body)) + body


def _ncs_prog(text, jsr=8):
    t = text.encode()
    return _ncs(b"\x1e\x00" + struct.pack(">i", jsr) + b"\x20\x00" +          # JSR main; RET
                b"\x04\x05" + struct.pack(">H", len(t)) + t +                   # CONST.S text
                b"\x05\x00" + struct.pack(">HB", 1, 1) + b"\x20\x00")          # ACTION 1 (1 arg); RET


def test_ncs(tmp):
    import sqlite3
    import nwn_ncs
    good = nwn_ncs.parse(_ncs_prog("Hello"))
    check("compiled scripts: a good .ncs reads cleanly (instructions, text, functions called)",
          good["ok"] and good["instructions"] == 5 and good["strings"] == ["Hello"] and good["actions"] == [1], good)
    bad = [("no header", b"NCS V9.9B" + b"\0" * 8), ("size", _ncs(b"\x20\x00", size=99)),
           ("unknown", _ncs(b"\x7f\x00")), ("cut off", _ncs(b"\x04\x03\x00\x00")),
           ("jump", _ncs_prog("Hello", jsr=9))]
    res = {k: nwn_ncs.parse(v) for k, v in bad}
    check("compiled scripts: damaged .ncs is caught (header, size, unknown instruction, cut off, jump into nowhere)",
          all(not r["ok"] and r["problem"] for r in res.values()) and "OUT OF CODE SEGMENT" in res["jump"]["problem"],
          {k: r["problem"] for k, r in res.items()})
    # stale: a tiny index + a module folder
    mdir = os.path.join(tmp, "ncsmod")
    os.makedirs(mdir)
    srcs = {"same": 'void main() { SpeakString("Hello"); }',
            "fold": 'void main() { SpeakString("Hel" + "lo"); }',
            "old": 'void main() { SpeakString("Goodbye"); }',
            "viainc": '#include "inc_a"\nvoid main() { }',
            "inc_a": 'string S = "Hello";',
            "nobase": '#include "x0_i0_gone"\nvoid main() { }'}
    ncs = {"same": _ncs_prog("Hello"), "fold": _ncs_prog("Hello"), "old": _ncs_prog("Hello"),
           "viainc": _ncs_prog("Hello"), "nobase": _ncs_prog("Hello"), "broken": _ncs_prog("Hello", jsr=9)}
    for k, b in ncs.items():
        with open(os.path.join(mdir, k + ".ncs"), "wb") as fh:
            fh.write(b)
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, priority INTEGER);"
                     "CREATE TABLE files(id INTEGER PRIMARY KEY, source_id INTEGER, relpath TEXT, ext TEXT);"
                     "CREATE TABLE scripts(file_id INTEGER, name TEXT, source TEXT);"
                     "CREATE TABLE edges(src TEXT, dst TEXT, kind TEXT, via TEXT);")
    db.execute("INSERT INTO sources VALUES (1, ?, 'module', 0)", (mdir,))
    for k, v in srcs.items():
        fid = db.execute("INSERT INTO files(source_id, relpath, ext) VALUES (1, ?, 'nss')", (k + ".nss",)).lastrowid
        db.execute("INSERT INTO scripts VALUES (?,?,?)", (fid, k, v))
    for k in ncs:
        db.execute("INSERT INTO files(source_id, relpath, ext) VALUES (1, ?, 'ncs')", (k + ".ncs",))
    db.executemany("INSERT INTO edges VALUES (?,?,'include','')",
                   [("script:viainc", "script:inc_a"), ("script:nobase", "script:x0_i0_gone")])
    issues, stats = nwn_ncs.check_module(db, live={"script:same", "script:fold", "script:old", "script:viainc",
                                                   "script:nobase"})
    got = {i["label"][:-4]: (i["category"], i["severity"]) for i in issues}
    check("compiled scripts: stale code is found (text the source no longer has); folded \"a\" + \"b\" and includes "
          "count; scripts with an unreadable include are not judged; unused scripts are notes",
          got == {"old": ("ncs_stale", "warning"), "broken": ("ncs_damaged", "info")} and
          stats["checked"] == 6 and stats["not_compared"] == 1 and stats["compared"] == 4, (got, stats))
    # the same module packed as a .mod
    m = os.path.join(tmp, "ncsmod.mod")
    n.write_erf(m, [(k, "ncs", b) for k, b in ncs.items()], "MOD ")
    db.execute("UPDATE sources SET path=?", (m,))
    issues2, stats2 = nwn_ncs.check_module(db)
    check("compiled scripts: read from a packed .mod as well", stats2["checked"] == 6 and
          {i["label"] for i in issues2} == {"old.ncs", "broken.ncs"}, stats2)
    lits, norm = nwn_ncs.source_literals({"dated": 'void main() { SpeakString("Built " + __DATE__ + " " + '
                                                   '__TIME__ + " in " + __FILE__); }'})
    check("compiled scripts: text the compiler fills in (__FILE__, __DATE__, __TIME__) is not taken for stale code",
          nwn_ncs._explained(norm("Built 2026-10-01 11:33:23 in dated.nss"), lits, {}) and
          not nwn_ncs._explained(norm("Built 2026-10-01 in other.nss"), lits, {}))
    long_ = "ab" * 3000
    check("compiled scripts: very long text constants don't break the check",
          nwn_ncs._explained(long_, {"a", "b"}, {}) and not nwn_ncs._explained(long_ + "c", {"a", "b"}, {}))
    text, err = nwn_ncs.disassemble(os.path.join(mdir, "same.ncs"))
    check("compiled scripts: no nwn_asm in tools -> a clear message, not a crash",
          (text is None and "nwn_asm" in err) if not n.find_tool("nwn_asm") else bool(text), err)


def test_ncs_precedence(tmp):
    """The stale check compares compiled text with the include copy the toolset compiled against (a hak's copy wins
    over the module's) and decodes both sides the same lossless way."""
    import sqlite3
    import nwn_ncs
    mdir = os.path.join(tmp, "ncsprec")
    os.makedirs(mdir)
    odd = b"Caf\x81 sign"                     # 0x81: one of the five bytes cp1252 leaves undefined
    ncs = {"user": _ncs_prog("HakText"), "oddtext": _ncs(b"\x04\x05" + struct.pack(">H", len(odd)) + odd + b"\x20\x00")}
    for k, b in ncs.items():
        with open(os.path.join(mdir, k + ".ncs"), "wb") as fh:
            fh.write(b)
    db = sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, priority INTEGER);"
                     "CREATE TABLE files(id INTEGER PRIMARY KEY, source_id INTEGER, relpath TEXT, ext TEXT);"
                     "CREATE TABLE scripts(file_id INTEGER, name TEXT, source TEXT);"
                     "CREATE TABLE edges(src TEXT, dst TEXT, kind TEXT, via TEXT);")
    db.execute("INSERT INTO sources VALUES (1, ?, 'module', 0)", (mdir,))
    db.execute("INSERT INTO sources VALUES (2, ?, 'hak', 1)", (os.path.join(tmp, "somehak.hak"),))
    rows = [(1, "user", '#include "inc_x"\nvoid main() { }'), (1, "inc_x", 'string S = "ModuleText";'),
            (2, "inc_x", 'string S = "HakText";'),
            (1, "oddtext", n.decode_text(b'void main() { SpeakString("' + odd + b'"); }'))]
    for sid, k, v in rows:
        fid = db.execute("INSERT INTO files(source_id, relpath, ext) VALUES (?, ?, 'nss')", (sid, k + ".nss")).lastrowid
        db.execute("INSERT INTO scripts VALUES (?,?,?)", (fid, k, v))
    for k in ncs:
        db.execute("INSERT INTO files(source_id, relpath, ext) VALUES (1, ?, 'ncs')", (k + ".ncs",))
    db.execute("INSERT INTO edges VALUES ('script:user', 'script:inc_x', 'include', '')")
    issues, stats = nwn_ncs.check_module(db, live={"script:user", "script:oddtext"})
    check("compiled scripts: a hak's include copy is the one compared (it wins over the module's), and text with a "
          "byte cp1252 leaves undefined is not called stale", not issues and stats["compared"] == 2, (issues, stats))


def test_untrusted_input(tmp):
    """Hostile or odd input stays contained: links in a folder module are not followed, a deep GFF is a GffError,
    an ERF entry larger than the file is refused before reading, odd table names in an uploaded database don't
    break its SQL, secret-looking setting names are hidden, and index.state is data (JSON), never code."""
    import sqlite3
    import nwn_database
    import nwn_index
    import nwn_modsettings as M
    # 1 a folder module holding a link to a file outside it
    mod = os.path.join(tmp, "linkmod")
    import make_test_module
    make_test_module.build(mod)
    secret = os.path.join(tmp, "outside_secret.nss")
    with open(secret, "w") as fh:
        fh.write("// private text outside the module\nvoid main() { }\n")
    linked = h.can_symlink(tmp)
    if not linked:
        why = "this system does not let the test make a symbolic link (Windows needs developer mode)"
        skip("a folder module's symbolic link is not followed (skipped, with the reason)", why)
        skip("reading a listed module file refuses a link that leads outside the folder", why)
    else:
        os.symlink(secret, os.path.join(mod, "leak.nss"))
        out = os.path.join(tmp, "linkmod_an")
        nwn_index.run_index(mod, out=out, verbose=False)
        db = sqlite3.connect(os.path.join(out, "index.sqlite"))
        leaked = db.execute("SELECT count(*) FROM files WHERE relpath LIKE 'leak%'").fetchone()[0]
        iss = [r[0] for r in db.execute("SELECT detail FROM issues WHERE category='symlink_skipped'")]
        db.close()
        check("a folder module's symbolic link is not followed (skipped, with the reason)",
              leaked == 0 and any("leak.nss" in d for d in iss), (leaked, iss))
        try:
            n.read_file_inside(mod, "leak.nss")
            refused = False
        except ValueError:
            refused = True
        check("reading a listed module file refuses a link that leads outside the folder", refused)
    # 2 a GFF nested deeper than any real file
    root = n.GffRoot("UTI ")
    cur = root
    for _ in range(400):
        child = n.GffStruct(0)
        cur.set("Child", n.STRUCT, child)
        cur = child
    for fn, what in ((lambda: n.write_gff(root), "write"), (lambda: n.gff_to_json(root), "to JSON")):
        try:
            fn()
            err = None
        except n.GffError as ex:
            err = str(ex)
        check(f"a GFF nested hundreds deep is a GffError on {what}, not a RecursionError", err and "deeper" in err, err)
    root2 = cur = n.GffRoot("UTI ")
    for _ in range(n.MAX_GFF_DEPTH + 30):
        child = n.GffStruct(0)
        cur.set("Child", n.STRUCT, child)
        cur = child
    saved, n.MAX_GFF_DEPTH = n.MAX_GFF_DEPTH, 1000          # write the hostile file once, then read it normally
    deep = n.write_gff(root2)
    n.MAX_GFF_DEPTH = saved
    try:
        n.read_gff(deep)
        err = None
    except n.GffError as ex:
        err = str(ex)
    check("a GFF file nested hundreds deep is a GffError on read, not a RecursionError", err and "deeper" in err, err)
    # 3 an ERF entry whose size runs past the end of the file
    hak = os.path.join(tmp, "short.hak")
    n.write_erf(hak, [("a", "txt", b"abc")], "HAK ")
    raw = bytearray(open(hak, "rb").read())
    res_off = struct.unpack_from("<I", raw, 28)[0]
    struct.pack_into("<I", raw, res_off + 4, 0xFFFFFFF0)
    open(hak, "wb").write(bytes(raw))
    e = n.Erf(hak)
    try:
        e.read(e.entries[0])
        msg = ""
    except ValueError as ex:
        msg = str(ex)
    e.close()
    check("an ERF entry larger than the file is refused before reading", "file ends before" in msg, msg)
    # 4 an uploaded sqlite file with a quote in a table name
    dbp = os.path.join(tmp, "odd.sqlite3")
    con = sqlite3.connect(dbp)
    con.execute('CREATE TABLE "a""b" (x TEXT)')
    con.execute('INSERT INTO "a""b" VALUES (\'1\')')
    con.commit(); con.close()
    work = os.path.join(tmp, "odd_work"); os.makedirs(work)
    got = nwn_database._sqlite_file(dbp, work)
    check("a database table whose name contains a quote is read, not an SQL error", got["tables"].get('a"b', {}).get("rows") == 1,
          got["tables"])
    # 5 secret-looking setting names, each spelling
    names = ["DB_PWD", "ADMIN_PW", "NWN_KEY", "NWN_CDKEY", "DISCORD_WEBHOOK_URL", "SENTRY_DSN", "DB_CONNECTION_STRING",
             "CONNSTR"]
    env = M.parse_server_file("".join(f"{k}=s3cr3t-{i}\n" for i, k in enumerate(names)) + "NWN_PVP=2\nMONKEYS=3\n"
                              "DB_URL=postgres://nwn:hunter5@db:5432/x\n", "env")
    hidden = [k for k in names if env["values"][k] == "(set - hidden)"]
    check("secret-looking setting names are hidden (pwd, pw, key, cdkey, webhook, dsn, connection string)",
          hidden == names and "s3cr3t" not in json.dumps(env), env["values"])
    check("ordinary settings stay visible and a password inside a URL is still hidden",
          env["values"]["NWN_PVP"] == "2" and env["values"]["MONKEYS"] == "3" and "hunter5" not in env["values"]["DB_URL"],
          env["values"])
    # 6 index.state is JSON; a pickle (or anything else) is refused, never loaded
    st_dir = os.path.join(tmp, "statecheck")
    os.makedirs(st_dir)
    ix = nwn_index.Indexer(st_dir, verbose=False)
    ix.tags["T"].add("bp:x.uti"); ix.base_names.add("a.nss"); ix.obj_types[("script:s", 3, "x")] = "utc"
    ix.save_state(os.path.join(st_dir, "index.state"), dict(sources=[], pos=(0, 0), phase="post"))
    ix2 = nwn_index.Indexer(st_dir, verbose=False, resume=True)
    prog = ix2.load_state(os.path.join(st_dir, "index.state"))
    check("a checkpoint is written as JSON and read back with its sets and keys intact",
          json.load(open(os.path.join(st_dir, "index.state")))["format"] and ix2.tags["T"] == {"bp:x.uti"} and
          ix2.base_names == {"a.nss"} and ix2.obj_types == {("script:s", 3, "x"): "utc"} and prog["phase"] == "post",
          (ix2.tags, ix2.obj_types))
    import pickle
    with open(os.path.join(st_dir, "index.state"), "wb") as fh:
        pickle.dump({"progress": {}}, fh)
    try:
        ix2.load_state(os.path.join(st_dir, "index.state"))
        msg = ""
    except ValueError as ex:
        msg = str(ex)
    ix.db.close(); ix2.db.close()
    check("an old pickle index.state is refused with a clear message, never unpickled", "can resume" in msg, msg)


def test_lean_hak_guard(tmp):
    """Lean-hak cleanup removes only haks an earlier toolkit build listed in haks/manifest.json."""
    import nwn_hakslim
    hd = os.path.join(tmp, "leanguard", "haks")
    os.makedirs(hd)
    open(os.path.join(hd, "players_own.hak"), "wb").write(b"HAK V1.0")
    try:
        nwn_hakslim.existing_lean_haks(hd); refused = False
    except ValueError as ex:
        refused = "players_own.hak" in str(ex)
    check("lean haks: a .hak the toolkit didn't write is never deleted (refused instead)", refused and
          os.path.isfile(os.path.join(hd, "players_own.hak")))
    os.remove(os.path.join(hd, "players_own.hak"))
    open(os.path.join(hd, "x_r1.hak"), "wb").write(b"HAK V1.0")
    json.dump({"haks": {"x": {"file": os.path.join(hd, "x_r1.hak")}}, "mode": "builder"},
              open(os.path.join(hd, "manifest.json"), "w"))
    check("lean haks: an earlier build's own lean haks are recognised for replacing",
          nwn_hakslim.existing_lean_haks(hd) == ["x_r1.hak"])


def test_hak_editor(tmp):
    import nwn_hakedit
    m = os.path.join(tmp, "some.mod")
    n.write_erf(m, [("x", "nss", b"void main(){}")], "MOD ")
    try:
        nwn_hakedit.HakEditor(os.path.join(tmp, "hakws")).open(m); ok_ = False
    except ValueError as ex:
        ok_ = "not a hak" in str(ex)
    check("the Hak editor refuses to open a module", ok_)


def test_dashboard_static():
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    names = re.findall(r"(?m)^(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", html)
    dup = sorted({x for x in names if names.count(x) > 1})
    check("no two page functions share a name (a duplicate silently replaces the first)", not dup, dup)
    import nwn_dashboard
    s = nwn_dashboard._typed_settings({"compiler": ["a"], "nwn_root": 5, "log_paths": "abc", "external_tools": "x"})
    check("bad setting types fall back to defaults", s["compiler"] == "" and s["nwn_root"] == "" and
          s["log_paths"] == [] and s["external_tools"] == [], s)
    test_dashboard_page_rules(html, nwn_dashboard)


def test_dashboard_page_rules(html, nwn_dashboard):
    """Rules of dashboard.html that can be read from its text (the behaviour itself was checked in a browser)."""
    script = html[html.index("<script>"):]
    # every API path the page calls is one the server answers
    routes = (set(nwn_dashboard.GET_ROUTES) | set(nwn_dashboard.POST_ROUTES) | set(nwn_dashboard.UPLOAD_ROUTES))
    prefixes = tuple(nwn_dashboard.POST_PREFIX_ROUTES)
    called = set(re.findall(r"""["`](/api/[a-z_/]*[a-z_])[?"`]""", script))
    unknown = sorted(p for p in called if p not in routes and not p.startswith(prefixes))
    check("every API path the dashboard page calls is a server route", called and not unknown, unknown)
    # filters: a drop-down's value is compared as it is (lower-casing it made "Critical" never match "Critical")
    check("filter drop-downs keep their value's capitals; only the search text is lower-cased",
          'd.type === "search" ? e.value.trim().toLowerCase()' in script
          and not re.search(r"toLowerCase\(\)\s*===\s*f0?\.", script))
    # unsaved work: Escape in a text box never closes the side panel, and leaving asks first
    check("Escape while typing never closes the side panel",
          'e.key === "Escape" && !e.target.closest("textarea,input,select")' in script)
    check("unsaved 2da or side-panel edits are guarded on page change, panel change and closing the tab",
          'addEventListener("beforeunload"' in script and "if (TD.dirty && $(\"#tdGrid\")){ leaveTwoda()" in script
          and "if (!await leaveDrawer()) return;" in script)
    # every discard of an edit goes through the one function that asks first
    check("discarding an edit always asks first (one place posts /api/edit/discard)",
          script.count('"/api/edit/discard"') == 1 and "async function discardEdit(" in script)
    # errors: none is silent, and a closed console window is named as the cause
    check("a failed call with no handler of its own is still shown (unhandledrejection)",
          'addEventListener("unhandledrejection"' in script)
    check("the page fetches only through fetchServer, which names a stopped dashboard",
          script.count("await fetch(") == 1 and "The dashboard is not running - start it again" in script)
    # keyboard: resource links take focus; no browser pop-ups; every dialog goes through openModal
    check("resource links (nl) are focusable links", 'class="nl" href="#" data-node=' in script)
    check("no browser pop-ups (confirm / alert / prompt)", not re.search(r"(?<![\w.])(confirm|alert|prompt)\(", script))
    check("every dialog is added by openModal (focus, Tab and Escape handled)",
          script.count("document.body.appendChild(md)") == 1)
    # the manual's code spans keep their * (MOD_WEATHER_*) and \* is a literal star
    check("Help: code spans are set aside before bold and italics", "codes.push(c)" in script and r"\\\*" in script)
    # wording: the hak editor no longer installs over a hak
    check("no text describes the removed in-place hak install",
          not re.search(r"rebuild and install|made by Install|before every install|label: \"Installed\"", script))
    # platform: version, settings warnings, Detect, both kinds of tool path
    check("version is shown in the sidebar and on the Help page", script.count("S.version") >= 3)
    check("settings warnings are shown next to Saved.", "saved.warnings" in script)
    check("the NWN install folder has a Detect button", 'id="s_detect"' in html and "/api/detect_nwn_root" in script)
    check("tool and folder examples are given for Windows and macOS",
          r"C:\\Tools\\Tool.exe or /Applications/Tool.app" in script and "~/Documents/Neverwinter Nights/modules" in script)
    check("module events are named as the toolset names them", 'ActvtItem: "OnActivateItem"' in script)
    check("muted text meets WCAG AA contrast (--ink-3 #6b6a65)", "--ink-3:#6b6a65" in html)


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_qa9_") as tmp:
        got = h.run(test_analysis, tmp)
        if got:
            mod, out, rep = got
            h.run(test_incomplete, tmp)
            h.run(test_refactor, tmp, out)
            h.run(test_build_and_audit, tmp, mod, out)
            h.run(test_facts_mcp, tmp, out)
            h.run(test_settings, tmp, out)
            h.run(test_db_upload, out)
            h.run(test_ncs_dashboard, out)
        for fn in (test_hostile, test_database, test_logs, test_editor_haks, test_include_recompile, test_heartbeats,
                   test_rmtree_force, test_compressed_erf, test_ncs, test_ncs_precedence, test_untrusted_input,
                   test_lean_hak_guard, test_housekeep, test_hak_editor):
            h.run(fn, tmp)
        h.run(test_dashboard_static)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
