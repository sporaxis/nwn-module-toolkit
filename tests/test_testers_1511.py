"""
A tester's reports on 1.5.10 (2026-11-03):
1. "These are the CODI Core AI scripts which compile fine": 545 missing_include errors on an AI library
   (no_lib_actions) calling functions another library defines without including it. NWScript pastes includes into
   the script being compiled, so a library is judged by the scripts that compile with it: if each of them also
   includes the defining library, nothing is wrong.
2. "These scripts were literally just compiled before I analyzed the module": 3 ncs_stale ('PRC_DEBUG'). Text any
   copy of the script or its includes holds (module, haks, override) is not "text the source no longer has" - the
   .ncs may have been compiled from the module's copy or against another copy of an include.
    python tests/test_testers_1511.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import sqlite3
import struct

import nwnlib as n  # noqa: E402
from _harness import check  # noqa: E402
from _gff import root, st, loc, w, R, X, LST, stand_in_ncs  # noqa: E402


def ifo(**extra):
    return n.write_gff(root("IFO ", Mod_Name=loc("T"), Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []), **extra))


def library_case(tmp):
    mod = os.path.join(tmp, "libmod")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "ai_one"), Mod_OnHeartbeat=(R, "half_ok"), Mod_OnClientEntr=(R, "half_bad"),
                             Mod_OnClientLeav=(R, "ai_two")))
    w(mod, "lib_analysis.nss", "int GetCombatConcentration() { return 1; }\n")
    w(mod, "lib_actions.nss", "void DoAct() { int n = GetCombatConcentration(); }\n")
    w(mod, "lib_spare.nss", "void Spare() { int n = GetCombatConcentration(); }\n")     # nothing includes it
    w(mod, "lib_half.nss", "void Half() { int n = GetCombatConcentration(); }\n")
    for nm, body in (("ai_one", '#include "lib_analysis"\n#include "lib_actions"\nvoid main() { DoAct(); }\n'),
                     ("ai_two", '#include "lib_analysis"\n#include "lib_actions"\nvoid main() { DoAct(); }\n'),
                     ("half_ok", '#include "lib_analysis"\n#include "lib_half"\nvoid main() { Half(); }\n'),
                     ("half_bad", '#include "lib_half"\nvoid main() { Half(); }\n')):
        w(mod, nm + ".nss", body)
        w(mod, nm + ".ncs", stand_in_ncs(nm))
    rep = h.analyse(mod, os.path.join(tmp, "libmod_an"))
    mi = {i["node"]: i for i in rep["issues"] if i["category"] == "missing_include"}
    check("a library whose every including script also includes the defining library: no missing_include",
          "script:lib_actions" not in mi, mi.get("script:lib_actions"))
    check("a library one including script lacks it for: an error naming that script",
          mi.get("script:lib_half", {}).get("severity") == "error" and "half_bad" in mi["script:lib_half"]["detail"]
          and "half_ok" not in mi["script:lib_half"]["detail"], mi.get("script:lib_half"))
    check("a library nothing compiles with: a note", mi.get("script:lib_spare", {}).get("severity") == "info",
          mi.get("script:lib_spare"))
    # include ORDER: the defining library must be pasted first (the official compiler fails otherwise)
    w(mod, "half_bad.nss", '#include "lib_half"\n#include "lib_analysis"\nvoid main() { Half(); }\n')
    rep_o = h.analyse(mod, os.path.join(tmp, "libmod_an2"))
    mo = {i["node"]: i for i in rep_o["issues"] if i["category"] == "missing_include"}
    check("a script that includes the defining library AFTER the library still lacks it (paste order)",
          mo.get("script:lib_half", {}).get("severity") == "error" and "half_bad" in mo["script:lib_half"]["detail"]
          and mo["script:lib_half"].get("lacking") == ["half_bad"], mo.get("script:lib_half"))
    check("the finding keeps a script's own missing-include wording up to '; used by' (stable for ticks and audit)",
          mo["script:lib_half"]["detail"].startswith("calls GetCombatConcentration() which is defined in "
                                                     "'lib_analysis' but does not #include it"), mo["script:lib_half"])
    sc = {x["name"]: x for x in rep["scripts"]}
    check("the Scripts page doesn't call a library call every user is fine with an error",
          "NOT included" not in " ".join([sc["lib_actions"]["summary"]] + list(sc["lib_actions"]["details"])),
          sc["lib_actions"])
    # only scripts nothing runs lack it: a note
    mod2 = os.path.join(tmp, "libmod2")
    os.makedirs(mod2)
    w(mod2, "module.ifo", ifo(Mod_OnModLoad=(R, "ok_user")))
    w(mod2, "lib_analysis.nss", "int GetCombatConcentration() { return 1; }\n")
    w(mod2, "lib_half.nss", "void Half() { int n = GetCombatConcentration(); }\n")
    w(mod2, "ok_user.nss", '#include "lib_analysis"\n#include "lib_half"\nvoid main() { Half(); }\n')
    w(mod2, "dead_user.nss", '#include "lib_half"\nvoid main() { Half(); }\n')
    for nm in ("ok_user", "dead_user"):
        w(mod2, nm + ".ncs", stand_in_ncs(nm))
    rep2 = h.analyse(mod2, os.path.join(tmp, "libmod2_an"))
    m2 = next((i for i in rep2["issues"] if i["category"] == "missing_include" and i["node"] == "script:lib_half"), {})
    check("when only scripts nothing runs lack it: a note, not an error", m2.get("severity") == "info"
          and "dead_user" in m2.get("detail", ""), m2)
    import nwn_fixes
    check("the error's fix says to add the include to the scripts named",
          "In each script named" in nwn_fixes.fix_for(mi["script:lib_half"]), nwn_fixes.fix_for(mi["script:lib_half"]))


def _ncs_with(texts):
    """A small valid .ncs holding these text constants (CONST.S each), then RET."""
    body = b"".join(b"\x04\x05" + struct.pack(">H", len(t)) + t for t in texts) + b"\x20\x00"
    return b"NCS V1.0B" + struct.pack(">I", 13 + len(body)) + body


def stale_case(tmp):
    mod, hakd = os.path.join(tmp, "ncsmod"), os.path.join(tmp, "ncshak")
    os.makedirs(mod)
    os.makedirs(hakd)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "aura3"), Mod_HakList=(LST, [st(8, Mod_Hak=(X, "ncshak"))])))
    # the module's copy of the include has the switch name; the hak's (which wins in load order) doesn't
    w(mod, "inc_dbg.nss", 'int DEBUG = GetLocalInt(GetModule(), "PRC_DEBUG");\n')
    w(hakd, "inc_dbg.nss", "int DEBUG = FALSE;\n")
    hak = os.path.join(tmp, "ncshak.hak")
    h.pack_hak(hakd, hak)
    w(mod, "aura3.nss", '#include "inc_dbg"\nvoid main() { if (DEBUG) SpeakString("aura"); }\n')
    w(mod, "aura3.ncs", _ncs_with([b"PRC_DEBUG", b"aura"]))
    w(mod, "aura4.nss", '#include "inc_dbg"\nvoid main() { SpeakString("new text"); }\n')
    w(mod, "aura4.ncs", _ncs_with([b"old text"]))                    # really stale: no copy has "old text"
    out = os.path.join(tmp, "ncsmod_an")
    h.analyse(mod, out, haks=[hak])
    import nwn_ncs
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    issues, stats = nwn_ncs.check_module(db)
    stale = {i["node"] for i in issues if i["category"] == "ncs_stale"}
    a3 = next((i for i in issues if i["node"] == "script:aura3"), {})
    check("text another module/hak copy of an include holds is a note naming it, not a stale warning",
          a3.get("severity") == "info" and "inc_dbg" in a3.get("detail", "") and stats["other_copy"] == 1, issues)
    # an old copy in an override folder (the builder's or a server's) does not excuse a stale .ncs
    ov = os.path.join(tmp, "ncs_ovr")
    os.makedirs(ov)
    w(ov, "aura4.nss", '#include "inc_dbg"\nvoid main() { SpeakString("old text"); }\n')
    out2 = os.path.join(tmp, "ncsmod_an2")
    h.analyse(mod, out2, haks=[hak], extra_overrides=[ov])
    db2 = sqlite3.connect(os.path.join(out2, "index.sqlite"))
    issues2, _st = nwn_ncs.check_module(db2)
    check("an old copy in an override folder doesn't excuse a stale .ncs",
          any(i["node"] == "script:aura4" and i["severity"] == "warning" for i in issues2), issues2)
    check("text no copy holds is still stale", "script:aura4" in stale, (issues, stats))


def audit_case(tmp):
    """Build audit: a library finding is new when a script joins its `lacking` list; an original analysed by 1.5.10
    (older wording, no list) is not a NEW error."""
    import contextlib
    import io
    import json
    import nwn_build
    mod = os.path.join(tmp, "aud")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "u_ok"), Mod_OnHeartbeat=(R, "u_bad"), Mod_OnClientEntr=(R, "u_two")))
    w(mod, "lib_def.nss", "int Helper() { return 1; }\n")
    w(mod, "lib_use.nss", "void Use() { int n = Helper(); }\n")
    w(mod, "u_ok.nss", '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n')
    w(mod, "u_two.nss", '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n')
    w(mod, "u_bad.nss", '#include "lib_use"\nvoid main() { Use(); }\n')
    for nm in ("u_ok", "u_two", "u_bad"):
        w(mod, nm + ".ncs", stand_in_ncs(nm))
    out = os.path.join(tmp, "aud_an")
    h.analyse(mod, out)
    os.makedirs(os.path.join(out, "edits"), exist_ok=True)
    w(os.path.join(out, "edits"), "u_two.nss", '#include "lib_use"\nvoid main() { Use(); }\n')   # breaks u_two
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, dict(delete=[], merge=[]), name="aud1", verbose=False,
                              audit_kwargs=dict(compiler=h.NO_COMPILER))
    r = {c["id"]: c for c in log["audit"]["checks"]}["references"]
    check("audit: an edit that makes another script lack the library's include is a NEW error",
          r["status"] == "FAIL" and any("lib_use" in e for e in r["evidence"]), r)
    os.remove(os.path.join(out, "edits", "u_two.nss"))
    # the original as 1.5.10 wrote it: the library's own wording, no `lacking` list, no library_include_rules
    rp = os.path.join(out, "report.json")
    rep = json.load(open(rp, encoding="utf-8"))
    for i in rep["issues"]:
        if i["category"] == "missing_include" and i["node"] == "script:lib_use":
            i["detail"] = ("calls Helper() which is defined in 'lib_def' but does not #include it (will not compile "
                           "unless the function exists elsewhere).")
            i.pop("lacking", None)
    rep.pop("library_include_rules", None)
    json.dump(rep, open(rp, "w", encoding="utf-8"))
    with contextlib.redirect_stdout(io.StringIO()):
        log2 = nwn_build.build(out, dict(delete=[], merge=[]), name="aud2", verbose=False,
                               audit_kwargs=dict(compiler=h.NO_COMPILER))
    r2 = {c["id"]: c for c in log2["audit"]["checks"]}["references"]
    check("audit: an original analysed by 1.5.10 (older wording) is not a NEW error for the same library",
          r2["status"] == "PASS", r2)
    import nwn_sheets
    old_entry = {"sig": "calls Helper() which is defined in 'lib_def' but does not #include it (will not compile "
                        "unless the function exists elsewhere).", "note": ""}
    new_i = next(i for i in h.analyse(mod, os.path.join(tmp, "aud_an3"))["issues"]
                 if i["category"] == "missing_include" and i["node"] == "script:lib_use")
    check("a 1.5.10 'by design' tick on the library still matches",
          nwn_sheets.accepted_match(old_entry, new_i["detail"]) is not None, new_i["detail"])


def _noop_build(out, name, delete=()):
    """Build the analysis with a plan (no compiler); returns the audit's checks by id."""
    import contextlib
    import io
    import nwn_build
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, dict(delete=list(delete), merge=[]), name=name, verbose=False,
                              audit_kwargs=dict(compiler=h.NO_COMPILER))
    return {c["id"]: c for c in log["audit"]["checks"]}


def second_pass(tmp):
    """The second adversarial pass on 1.5.11's library rule (each case was a reproducible probe; include layouts were
    checked against the official compiler): which definer is named, the copy judged, include cycles, prototypes."""
    import json
    import nwn_sheets
    mi_of = lambda rep, node: [i for i in rep["issues"] if i["category"] == "missing_include"  # noqa: E731
                               and i["node"] == node]
    # F1: two definers - the library is named before a script with a main(), and deleting one is not a new finding
    mod = os.path.join(tmp, "twodef")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "u3")))
    w(mod, "aa_test.nss", "int Two() { return 1; }\nvoid main() { Two(); }\n")
    w(mod, "two_b.nss", "int Two() { return 2; }\n")
    w(mod, "lib_multi.nss", "int M() { return Two(); }\n")
    w(mod, "u3.nss", '#include "lib_multi"\nvoid main() { M(); }\n')
    w(mod, "u3.ncs", stand_in_ncs("u3"))
    out = os.path.join(tmp, "twodef_an")
    rep = h.analyse(mod, out)
    f1 = mi_of(rep, "script:lib_multi")
    check("two definers: the include library is named first (stable text)",
          len(f1) == 1 and "defined in 'two_b' or 'aa_test'" in f1[0]["detail"], f1)
    ref = _noop_build(out, "c_twodef", delete=["script:aa_test"])["references"]
    check("deleting one of two definers is not a NEW library finding in the audit", ref["status"] == "PASS", ref)
    check("a 'by design' tick survives a change of the definer named",
          nwn_sheets.accepted_match({"sig": nwn_sheets.issue_sig(f1[0]["detail"]), "note": ""},
                                    f1[0]["detail"].replace("'two_b' or 'aa_test'", "'two_b'")) is not None, f1)
    # F2: the copy judged is the module's (players' copy), not an override copy that includes more - or less
    for tag, mod_u1, ov_u1, want in (
            ("mask", '#include "lib_use"\nvoid main() { Use(); }\n',
             '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n', "error"),
            ("rev", '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n',
             '#include "lib_use"\nvoid main() { Use(); }\n', None)):
        mod = os.path.join(tmp, "ov_" + tag)
        od = os.path.join(tmp, "ov_" + tag + "_o", "override")
        os.makedirs(mod)
        os.makedirs(od)
        w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "u1")))
        w(mod, "lib_def.nss", "int Helper() { return 1; }\n")
        w(mod, "lib_use.nss", "void Use() { int n = Helper(); }\n")
        w(mod, "u1.nss", mod_u1)
        w(mod, "u1.ncs", stand_in_ncs("u1"))
        w(od, "u1.nss", ov_u1)
        out = os.path.join(tmp, f"ov_{tag}_an")
        got = mi_of(h.analyse(mod, out, extra_overrides=[od]), "script:lib_use")
        check(f"the module's copy of an including script is judged, not an override copy ({tag})",
              [i["severity"] for i in got] == ([want] if want else []), got)
        ref = _noop_build(out, "c_ov_" + tag)["references"]
        check(f"... and a build changing nothing passes the audit ({tag})", ref["status"] == "PASS", ref)
    # F3: an include cycle is reported on its alphabetically first member; the audit compares cycles by members
    mod = os.path.join(tmp, "cyc")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo())
    w(mod, "zz_b.nss", '#include "mm_a"\nvoid B() { }\n')
    w(mod, "mm_a.nss", '#include "zz_b"\nvoid A() { }\n')
    out = os.path.join(tmp, "cyc_an")
    cy = [i for i in h.analyse(mod, out)["issues"] if i["category"] == "include_cycle"]
    check("an include cycle: one issue, on its first member by name, written from there",
          len(cy) == 1 and cy[0]["node"] == "script:mm_a" and cy[0]["detail"].startswith(
              "#include cycle: mm_a -> zz_b -> mm_a"), cy)
    rp = os.path.join(out, "report.json")
    r_ = json.load(open(rp, encoding="utf-8"))
    for i in r_["issues"]:                    # as 1.5.10 may have written it: the other member, the other rotation
        if i["category"] == "include_cycle":
            i["node"], i["detail"] = "script:zz_b", i["detail"].replace("mm_a -> zz_b -> mm_a", "zz_b -> mm_a -> zz_b")
    json.dump(r_, open(rp, "w", encoding="utf-8"))
    ref = _noop_build(out, "c_cyc")["references"]
    check("... and the same cycle reported on another member by an older version is not NEW in the audit",
          ref["status"] == "PASS", ref)
    # F5: prototypes (each layout's verdict is the official compiler's)
    mod = os.path.join(tmp, "proto")
    os.makedirs(mod)
    users = {
        # the script's own prototype above the #include: compiles (the definition comes later)
        "p_own": 'int Helper();\n#include "lib_use"\n#include "lib_def"\nvoid main() { Use(); }\n',
        # the definer after the library, no prototype: does not compile
        "p_late": '#include "lib_use"\n#include "lib_def"\nvoid main() { Use(); }\n',
        # a prototype nothing calls is no call: needs no include
        "p_decl": 'int Helper();\nvoid main() { }\n',
        # a library holding only a prototype, its function unused by main(): compiles without a definition
        "p_unrun": '#include "lib_pro"\nvoid main() { }\n',
        # ... and run by main(): UNDEFINED IDENTIFIER
        "p_run": '#include "lib_pro"\nvoid main() { Pro(); }\n'}
    ev = ["Mod_OnModLoad", "Mod_OnHeartbeat", "Mod_OnClientEntr", "Mod_OnClientLeav", "Mod_OnAcquirItem"]
    w(mod, "module.ifo", ifo(**{e: (R, u) for e, u in zip(ev, users)}))
    w(mod, "lib_def.nss", "int Helper() { return 1; }\n")
    w(mod, "lib_use.nss", "void Use() { int n = Helper(); }\n")
    w(mod, "lib_pro.nss", "int Helper();\nvoid Pro() { int n = Helper(); }\n")
    for u, text in users.items():
        w(mod, u + ".nss", text)
        w(mod, u + ".ncs", stand_in_ncs(u))
    rep = h.analyse(mod, os.path.join(tmp, "proto_an"))
    use = mi_of(rep, "script:lib_use")
    check("prototypes: the script's own prototype above the include is fine; the definer pasted after is not",
          len(use) == 1 and use[0].get("lacking") == ["p_late"], use)
    check("a prototype nothing calls needs no include", not mi_of(rep, "script:p_decl"), mi_of(rep, "script:p_decl"))
    pro = mi_of(rep, "script:lib_pro")
    check("a prototype-only library: an error naming only the script whose main() runs the undefined function",
          len(pro) == 1 and pro[0]["severity"] == "error" and pro[0].get("lacking") == ["p_run"], pro)
    check("the finding keeps lacking_count (the list is capped)", use and use[0].get("lacking_count") == 1, use)
    # F7: a library finding that is only a note is not "NOT included" on the Scripts page
    mod = os.path.join(tmp, "note")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "ok_user")))
    w(mod, "lib_def.nss", "int Helper() { return 1; }\n")
    w(mod, "lib_use.nss", "void Use() { int n = Helper(); }\n")
    w(mod, "ok_user.nss", '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n')
    w(mod, "dead_user.nss", '#include "lib_use"\nvoid main() { Use(); }\n')
    rep = h.analyse(mod, os.path.join(tmp, "note_an"))
    sc = {x["name"]: x for x in rep["scripts"]}
    check("a library finding that is only a note: the Scripts page doesn't call it a missing include",
          [i["severity"] for i in mi_of(rep, "script:lib_use")] == ["info"] and "NOT included" not in " ".join(
              [sc["lib_use"]["summary"]] + list(sc["lib_use"]["details"])), sc.get("lib_use"))
    # F9: a library in a hak and in the module is judged once (the copy the game loads)
    mod = os.path.join(tmp, "twocopy")
    hakd = os.path.join(tmp, "twocopy_hak")
    os.makedirs(mod)
    os.makedirs(hakd)
    w(hakd, "lib_use.nss", "void Use() { int n = Helper(); }\n")
    hak = os.path.join(tmp, "twocopy.hak")
    h.pack_hak(hakd, hak)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "u1"), Mod_HakList=(LST, [st(0, Mod_Hak=(X, "twocopy"))])))
    w(mod, "lib_def.nss", "int Helper() { return 1; }\n")
    w(mod, "lib_use.nss", "void Use() { int n = Helper(); }\n")
    w(mod, "u1.nss", '#include "lib_use"\nvoid main() { Use(); }\n')
    w(mod, "u1.ncs", stand_in_ncs("u1"))
    rep = h.analyse(mod, os.path.join(tmp, "twocopy_an"), haks=[hak])
    check("a library in a hak and in the module: one finding", len(mi_of(rep, "script:lib_use")) == 1,
          mi_of(rep, "script:lib_use"))
    check("... and the Scripts page lists the script once", [x["name"] for x in rep["scripts"]].count("lib_use") == 1,
          [x["name"] for x in rep["scripts"]].count("lib_use"))


def third_pass(tmp):
    """The third (narrow) adversarial pass on 1.5.11's library rule; each layout's verdict is the official compiler's."""
    import json
    mi_of = lambda rep, node: [i for i in rep["issues"] if i["category"] == "missing_include"  # noqa: E731
                               and i["node"] == node]

    def module(tag, files, events=None, ncs=True):
        mod = os.path.join(tmp, tag)
        os.makedirs(mod)
        mains = [k for k, v in files.items() if "void main()" in v or "StartingConditional" in v]
        ev = ["Mod_OnModLoad", "Mod_OnHeartbeat", "Mod_OnClientEntr", "Mod_OnClientLeav", "Mod_OnAcquirItem"]
        w(mod, "module.ifo", ifo(**{e: (R, m) for e, m in zip(ev, events if events is not None else mains)}))
        for k, v in files.items():
            w(mod, k + ".nss", v)
            if ncs and k in mains:
                w(mod, k + ".ncs", stand_in_ncs(k))
        return mod
    # a global initialiser runs a declared-only function whether or not main() calls anything (UNDEFINED IDENTIFIER)
    mod = module("glob", {"lib_def": "int Fn() { return 1; }\n",
                          "lib_use": "int Fn();\nint g_val = Fn();\nint G() { return 2; }\n",
                          "u1": '#include "lib_use"\nvoid main() { G(); }\n'})
    got = mi_of(h.analyse(mod, os.path.join(tmp, "glob_an")), "script:lib_use")
    check("a global initialiser calling a declared-only function: an error naming the script",
          [(i["severity"], i.get("lacking")) for i in got] == [("error", ["u1"])], got)
    # a prototype and a call on one line: still a call; declared before the call on that line: fine
    mod = module("line", {"lib_use": "int Fn(); int G() { return Fn(); }\n",
                          "lib_def": "int Fn() { return 1; }\n",
                          "u_none": '#include "lib_use"\nvoid main() { G(); }\n',
                          "u_def": '#include "lib_use"\n#include "lib_def"\nvoid main() { G(); }\n'})
    got = mi_of(h.analyse(mod, os.path.join(tmp, "line_an")), "script:lib_use")
    check("a prototype and a call on one line: the call counts, the prototype before it does too",
          [(i["severity"], i.get("lacking")) for i in got] == [("error", ["u_none"])], got)
    # a main script whose only call to a declared-only function is in a function main() never runs: compiles
    mod = module("dead", {"lib_def": "int Fn() { return 1; }\n",
                          "u1": "int Fn();\nint Dead() { return Fn(); }\nvoid main() { }\n"})
    check("a main script calling a declared-only function only from code main() never runs: no missing include",
          not mi_of(h.analyse(mod, os.path.join(tmp, "dead_an")), "script:u1"))
    # with a prototype above the call, an include not in the module pasted after the library may hold the definition
    mod = module("unr", {"lib_def": "int Fn() { return 1; }\n",
                         "lib_proto": "int Fn();\nint G() { return Fn(); }\n",
                         "u1": '#include "lib_proto"\n#include "x0_i0_unread"\nvoid main() { G(); }\n'})
    got = mi_of(h.analyse(mod, os.path.join(tmp, "unr_an")), "script:lib_proto")
    check("an unread include after a prototype library may define the function: a note",
          [i["severity"] for i in got] == ["info"], got)
    # the lacking list is capped in report.json; the audit compares the whole list (library_lacking.json)
    files = {"lib_def": "int Helper() { return 1; }\n", "lib_use": "void Use() { int n = Helper(); }\n",
             "zz_live": '#include "lib_def"\n#include "lib_use"\nvoid main() { Use(); }\n'}
    files.update({f"u_{k:03d}": '#include "lib_use"\nvoid main() { Use(); }\n' for k in range(205)})
    mod = module("cap", files, events=["zz_live", "u_000"])
    out = os.path.join(tmp, "cap_an")
    rep = h.analyse(mod, out)
    f = mi_of(rep, "script:lib_use")
    full = json.load(open(os.path.join(out, "library_lacking.json"), encoding="utf-8"))
    check("report.json keeps 200 names and the count; library_lacking.json keeps them all",
          len(f) == 1 and len(f[0]["lacking"]) == 200 and f[0]["lacking_count"] == 205
          and len(full.get(f[0]["lacking_key"], [])) == 205, (f and f[0].get("lacking_count")))
    os.makedirs(os.path.join(out, "edits"), exist_ok=True)
    w(os.path.join(out, "edits"), "zz_live.nss", '#include "lib_use"\nvoid main() { Use(); }\n')   # breaks zz_live
    ref = _noop_build(out, "c_cap", delete=["script:u_204"])["references"]
    check("audit: a script the build breaks is NEW even past the 200-name cut (same count)",
          ref["status"] == "FAIL" and any("lib_use" in e for e in ref["evidence"]), ref)
    # an original analysed before 1.5.11 never judged this: a warning to analyse again, not a FAIL
    mod = module("old", {"hc_def": "int GFA(string s) { return 1; }\n",
                         "hc_lib": 'int GFA(string s);\nint Use() { return GFA("x"); }\n',
                         "u1": '#include "hc_lib"\nvoid main() { Use(); }\n'})
    out = os.path.join(tmp, "old_an")
    h.analyse(mod, out)
    rp = os.path.join(out, "report.json")
    r_ = json.load(open(rp, encoding="utf-8"))
    r_["issues"] = [i for i in r_["issues"] if i["category"] != "missing_include"]   # as 1.5.10: only a note
    r_.pop("library_include_rules", None)
    json.dump(r_, open(rp, "w", encoding="utf-8"))
    ref = _noop_build(out, "c_old")["references"]
    check("audit: a library finding an older analysis never made is a WARN asking to analyse again, not a FAIL",
          ref["status"] == "WARN" and any("Analyse again" in e for e in ref["evidence"]), ref)


def feedback_1106(tmp):
    """Testers' comments on 1.5.11's thread (2026-11-04/05): a library that leans on its includers is one note (it
    compiles, but "it is a bad practice"); PRC scripts "literally just compiled" were stale because the analysis read
    PRC's replaced x2_inc_spellhook from the base game - the fix says so and how to show the analysis the real copy."""
    import nwn_ncs
    mod = os.path.join(tmp, "via")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "u1")))
    w(mod, "lib_def.nss", "int F1() { return 1; }\nint F2() { return 2; }\n")
    w(mod, "lib_d2.nss", "int F3() { return 3; }\n")
    w(mod, "lib_use.nss", "int Use() { return F1() + F2() + F3(); }\n")
    w(mod, "u1.nss", '#include "lib_def"\n#include "lib_d2"\n#include "lib_use"\nvoid main() { Use(); }\n')
    w(mod, "u1.ncs", stand_in_ncs("u1"))
    rep = h.analyse(mod, os.path.join(tmp, "via_an"))
    via = [i for i in rep["issues"] if i["category"] in ("include_via_users", "missing_include")]
    check("a library that compiles only because its includers include its libraries: ONE note naming them",
          [(i["category"], i["severity"], i["node"]) for i in via] == [("include_via_users", "info", "script:lib_use")]
          and "'lib_d2' (F3())" in via[0]["detail"] and "'lib_def' (F1(), F2())" in via[0]["detail"]
          and "#include lines" in (via[0].get("fix") or ""), via)
    # PRC replaces the base game's x2_inc_spellhook with one that includes its switches
    game = os.path.join(tmp, "game1106")
    n.write_key_bif(game, {("x2_inc_spellhook", "nss"): b"int X2_SPELLHOOK = 1;\n"})
    mod = os.path.join(tmp, "prcmod")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo(Mod_OnModLoad=(R, "aura3")))
    w(mod, "aura3.nss", '#include "x2_inc_spellhook"\nvoid main() { SpeakString("aura"); }\n')
    w(mod, "aura3.ncs", _ncs_with([b"PRC_DEBUG", b"aura"]))
    out = os.path.join(tmp, "prcmod_an")
    h.analyse(mod, out)
    issues, stats = nwn_ncs.check_module(sqlite3.connect(os.path.join(out, "index.sqlite")), nwn_root=game)
    st_ = [i for i in issues if i["category"] == "ncs_stale"]
    check("stale text with an include read from the base game: the fix names it and the way to show the real copy",
          len(st_) == 1 and "x2_inc_spellhook from the base game" in st_[0]["fix"]
          and "Server override folders" in st_[0]["fix"] and st_[0]["detail"].startswith(
              "the compiled script contains text its source (and includes) no longer has: 'PRC_DEBUG'"), st_)
    ov = os.path.join(tmp, "prc_src")
    os.makedirs(ov)
    w(ov, "x2_inc_spellhook.nss", '#include "prc_inc_switch"\nint X2_SPELLHOOK = 1;\n')
    w(ov, "prc_inc_switch.nss", 'const string PRC_DEBUG = "PRC_DEBUG";\n')
    out2 = os.path.join(tmp, "prcmod_an2")
    h.analyse(mod, out2, extra_overrides=[ov])
    issues2, _s = nwn_ncs.check_module(sqlite3.connect(os.path.join(out2, "index.sqlite")), nwn_root=game)
    check("... and with the package's include folder added, the same .ncs is not stale",
          not [i for i in issues2 if i["category"] == "ncs_stale"], issues2)


def dense_graph(tmp):
    """A dense include graph (each library includes every one below it) indexes quickly - the cycle search walked
    every path (26 libraries: 18 s)."""
    import time
    mod = os.path.join(tmp, "dense")
    os.makedirs(mod)
    w(mod, "module.ifo", ifo())
    for k in range(40):
        w(mod, f"lib{k:02d}.nss", "".join(f'#include "lib{j:02d}"\n' for j in range(k)) + f"void F{k}() {{ }}\n")
    t0 = time.time()
    h.analyse(mod, os.path.join(tmp, "dense_an"))
    h.timing("index: 40 libraries each including all below it", time.time() - t0, 20.0)
    check("a dense include graph of 40 libraries is analysed (no exponential walk)", time.time() - t0 < 120,
          round(time.time() - t0, 1))


def main():
    with h.tempdir("nwn_t1511_") as tmp:
        library_case(tmp)
        stale_case(tmp)
        dense_graph(tmp)
        audit_case(tmp)
        second_pass(tmp)
        third_pass(tmp)
        feedback_1106(tmp)
    return h.summary()


if __name__ == "__main__":
    raise SystemExit(main())
