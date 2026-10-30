"""
Tests for accepting several issues that share one key (a tester's report on 1.5.6): one script missing two includes has
two missing_include issues under one key (category|node|label). Accepting both must keep both - before 1.5.7 the
second replaced the first, which came back as "changed since you accepted it" - and the Issues page must tick the
issue whose box was clicked, not the first one with that key. Also: a missing include becomes a note naming the haks
module.ifo lists but the analysis didn't find (nwn_analysis.include_notes), and its "How to fix" fits a missing file.
    python tests/test_accept_many.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import json
import os
import shutil
import sys

import nwnlib as n  # noqa: E402
import nwn_audit  # noqa: E402
import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_facts  # noqa: E402
import nwn_sheets as SH  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, w, R, X, LST  # noqa: E402

A = "acctest"


def module(folder, haks=()):
    """A module whose library script includes two scripts no loaded source has; module.ifo lists `haks`."""
    os.makedirs(folder, exist_ok=True)
    w(folder, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("Accept"), Mod_Entry_Area=(R, "a1"),
                                            Mod_Area_list=(LST, []), Mod_OnModLoad=(R, "s1"),
                                            Mod_HakList=(LST, [st(8, Mod_Hak=(X, hk)) for hk in haks]))))
    w(folder, "_inc_lib_.nss", '#include "pkg_utility"\n#include "pkg_spells"\nint X(){ return 1; }\n')
    w(folder, "s1.nss", '#include "_inc_lib_"\nvoid main(){ X(); }\n')


def two_issues(tmp):
    """Both missing includes are errors under one key, with the advice for a missing file."""
    mod, out = os.path.join(tmp, "m1"), os.path.join(D.WORKSPACE, A)
    module(mod)
    rep = h.analyse(mod, out)
    iss = [i for i in rep["issues"] if i["category"] == "missing_include"]
    keys = {SH.issue_key(i) for i in iss}
    check("analysis: two missing includes in one script are two errors under one key",
          len(iss) == 2 and len(keys) == 1 and all(i["severity"] == "error" for i in iss), iss)
    check("fix: a missing include file gets advice about the file (not 'add #include')",
          all("No source the game loads has" in i["fix"] and "pkg_" in i["fix"] for i in iss), [i["fix"] for i in iss])
    check("report: include_notes is recorded (Analyse again is offered to older analyses)", rep.get("include_notes") == 1)
    return iss


def accept_both(iss):
    """Accept all shown, the facts API, un-ticking one, an old-format file and the spreadsheet import."""
    items = [dict(key=SH.issue_key(i), sig=SH.issue_sig(i["detail"]), category=i["category"], label=i["label"]) for i in iss]
    data = D.accept_issues_many(dict(a=A, accepted=True, note="In the package's hak.", items=items))
    e = data[SH.issue_key(iss[0])]
    check("accept all: both issues under one key stay accepted",
          all(SH.accepted_match(e, i["detail"]) == "In the package's hak." for i in iss) and len(e["sigs"]) == 2, e)
    f = nwn_facts.Facts(A, workspace=D.WORKSPACE)
    got = [x for x in f.issues("missing_include")["issues"]]
    check("facts: both are reported as accepted", len(got) == 2 and all(x.get("accepted_by_design") for x in got), got)
    one = dict(items[0])
    data = D.accept_issues_many(dict(a=A, accepted=False, items=[one]))
    e = data[SH.issue_key(iss[0])]
    check("un-tick one: only that one counts again, the other stays accepted",
          SH.accepted_match(e, iss[0]["detail"]) is None and SH.accepted_match(e, iss[1]["detail"]) is not None, e)
    data = D.accept_issues_many(dict(a=A, accepted=False, items=[dict(items[1])]))
    check("un-tick the last one: the key is gone", SH.issue_key(iss[0]) not in data, data)
    # a file written before 1.5.7: one "sig" per key, no "sigs"
    old = {items[0]["key"]: dict(sig=items[0]["sig"], note="old note", category=iss[0]["category"], label=iss[0]["label"],
                                 accepted_at="2026-10-01 10:00")}
    with open(os.path.join(D.WORKSPACE, A, "accepted_issues.json"), "w", encoding="utf-8") as fh:
        json.dump(old, fh)
    check("old file: its one accepted issue still matches", SH.accepted_match(old[items[0]["key"]], iss[0]["detail"]) == "old note")
    data = D.accept_issues_many(dict(a=A, accepted=True, note="new", items=[items[1]]))
    e = data[items[0]["key"]]
    check("old file: accepting the second keeps the first, with its own note and time; sig/note hold the latest",
          SH.accepted_match(e, iss[0]["detail"]) == "old note" and SH.accepted_match(e, iss[1]["detail"]) == "new" and
          e["sigs"][items[0]["sig"]]["accepted_at"] == "2026-10-01 10:00" and e["sig"] == items[1]["sig"] and
          e["note"] == "new", e)
    D.accept_issues_many(dict(a=A, accepted=False, items=[dict(key=items[0]["key"])]))
    # the spreadsheet: the detail column picks which issue of a key a row means
    k = items[0]["key"]
    csv_ = ("key,by_design,note,detail\n" + "".join(f'{k},yes,from sheet,"{i["detail"].replace(chr(34), chr(34) * 2)}"\n'
                                                    for i in iss)).encode()
    acc = json.load(open(os.path.join(D.WORKSPACE, A, "accepted_issues.json"), encoding="utf-8"))
    p = SH.plan_issues(iss, acc, csv_)
    check("import: rows with the same key but different details accept each issue",
          len(p["accept"]) == 2 and {a["sig"] for a in p["accept"]} == {it["sig"] for it in items}, p)
    acc = SH.apply_issue_plan(acc, p, "now")
    check("import: both accepted after applying", all(SH.accepted_match(acc[k], i["detail"]) for i in iss), acc)
    p2 = SH.plan_issues(iss, acc, f'key,by_design,detail\n{k},no,"{iss[0]["detail"].replace(chr(34), chr(34) * 2)}"\n'.encode())
    acc2 = SH.apply_issue_plan(acc, p2, "now")
    check("import: 'no' on one row un-accepts only that issue",
          [u.get("sig") for u in p2["unaccept"]] == [items[0]["sig"]] and SH.accepted_match(acc2[k], iss[0]["detail"]) is None
          and SH.accepted_match(acc2[k], iss[1]["detail"]) == "from sheet", acc2)


def review_fixes(iss):
    """From the independent review: the latest stays latest after a save and read; a detail that matches none of a
    key's issues is refused; a very long detail can be accepted and un-accepted."""
    k = SH.issue_key(iss[0])
    p = lambda sig, note: dict(accept=[dict(key=k, sig=sig, category="c", label="l", note=note)], unaccept=[])  # noqa: E731
    acc = SH.apply_issue_plan({}, p("A", "nA"), "1")
    acc = json.loads(json.dumps(SH.apply_issue_plan(acc, p("B", "nB"), "2")))       # saved and read back
    acc = json.loads(json.dumps(SH.apply_issue_plan(acc, p("C", "nC"), "3")))
    acc = SH.apply_issue_plan(acc, dict(accept=[], unaccept=[dict(key=k, sig="C")]), "4")
    check("latest: after accepting A, B, C and un-accepting C, the entry's sig/note are B's (what older versions see)",
          (acc[k]["sig"], acc[k]["note"]) == ("B", "nB") and set(acc[k]["sigs"]) == {"A", "B"}, acc)
    # a hand-damaged entry (not an object): un-accepting it removes it instead of crashing (an adversarial test)
    try:
        acc2 = SH.apply_issue_plan({k: "damaged"}, dict(accept=[], unaccept=[dict(key=k, sig="A")]), "5")
        ok_ = acc2 == {}
    except Exception as ex:  # noqa
        ok_ = str(ex)
    check("a damaged accepted entry is removed by un-accept, never a crash", ok_ is True, ok_)
    stale = f'key,by_design,detail\n{k},yes,"#include ""pkg_gone"" not found in module (line 9)"\n'.encode()
    pl = SH.plan_issues(iss, {}, stale)
    check("import: a detail none of the key's issues has is refused (never applied to the first one)",
          not pl["accept"] and pl["refused"] and "export again" in pl["refused"][0]["why"], pl)
    pl = SH.plan_issues(iss[:1], {}, stale)
    check("import: with one issue under the key, a changed detail still applies to it", len(pl["accept"]) == 1, pl)
    long_ = dict(iss[0], detail="x" * 5000 + " 12")
    sg = SH.issue_sig(long_["detail"])
    acc = SH.apply_issue_plan({}, dict(accept=[dict(key=k, sig=sg, category="c", label="l")], unaccept=[]), "1")
    ok_in = SH.accepted_match(acc[k], long_["detail"]) is not None
    acc = SH.apply_issue_plan(acc, dict(accept=[], unaccept=[dict(key=k, sig=sg)]), "2")
    check("long detail: accepted, and un-accepting removes it (one length stored and compared)", ok_in and k not in acc, acc)


def audit_still_counts(tmp):
    """A missing include lowered to a note because a hak isn't found still fails the build audit when a build loses it."""
    mod, out = os.path.join(tmp, "m4"), os.path.join(tmp, "an4")
    os.makedirs(mod)
    w(mod, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("AuditP"), Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []),
                                         Mod_OnModLoad=(R, "s1"), Mod_HakList=(LST, [st(8, Mod_Hak=(X, "nohak_xyz"))]))))
    w(mod, "inc_real.nss", "int X(){ return 1; }\n")
    w(mod, "s1.nss", '#include "inc_real"\nvoid main(){ X(); }\n')
    h.analyse(mod, out)
    log = nwn_build.build(out, {"delete": [], "merge": []}, verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER))
    clean = os.path.join(out, "build", os.path.basename(log["output"]))
    os.remove(os.path.join(clean, "inc_real.nss"))      # what a build that drops a needed include would leave
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        a = nwn_audit.audit(out, os.path.join(out, "build"), os.path.basename(clean), compiler=h.NO_COMPILER, verbose=False)
    ref = {c["id"]: c for c in a["checks"]}["references"]
    check("audit: with a listed hak not found, a missing include the build caused still fails 'references'",
          ref["status"] == "FAIL" and any("inc_real" in str(e) for e in ref.get("evidence", [])), ref)


def hak_not_found(tmp):
    """With a hak module.ifo lists not found, a missing include is a note naming the hak."""
    mod = os.path.join(tmp, "m2")
    module(mod, haks=["pkg_scripts"])
    rep = h.analyse(mod, os.path.join(tmp, "out2"))
    iss = [i for i in rep["issues"] if i["category"] == "missing_include"]
    check("hak not found: the missing includes are info notes naming the hak (remembering they were errors)",
          len(iss) == 2 and all(i["severity"] == "info" and i.get("was") == "error" and "pkg_scripts" in i["detail"] and
                                "may be in one of them" in i["detail"] for i in iss), iss)
    check("hak not found: the advice says to put the hak in the hak folder and analyse again",
          all("hak folder" in i["fix"] and "Analyse again" in i["fix"] for i in iss), [i["fix"] for i in iss])
    # with the hak found and holding one of the two includes: that one is resolved, the other is an error again
    hk = os.path.join(tmp, "hk"); os.makedirs(hk)
    w(hk, "pkg_utility.nss", "int U(){ return 2; }\n")
    hak = os.path.join(tmp, "pkg_scripts.hak")
    h.pack_hak(hk, hak)
    rep = h.analyse(mod, os.path.join(tmp, "out3"), haks=[hak])
    iss = [i for i in rep["issues"] if i["category"] == "missing_include"]
    check("hak found: the include it holds is no longer missing; the one nobody has is an error",
          [(i["severity"], "pkg_spells" in i["detail"]) for i in iss] == [("error", True)], iss)


def page():
    """The Issues page keeps several accepted issues per key and ticks the issue whose box was clicked."""
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: an entry's accepted signatures are all read (sigs, and the old sig), in an object with no prototype",
          "const accSigs" in html and "a.sigs" in html and "function accNote" in html and "Object.create(null)" in html)
    check("page: a By design box carries its issue's signature, and the click finds the issue by key and signature",
          'data-sig="${esc(issueSig(i))}"' in html and "issueKey(x) === cb.dataset.acc && issueSig(x) === cb.dataset.sig" in html)
    check("page: 'changed since you accepted it' only when an accepted text no longer matches any issue of that key",
          "function acceptedChanged(i){ const a = (S.accepted || {})[issueKey(i)]; if (!a || isAccepted(i)) return false;" in html)


def main():
    with h.tempdir("nwn_acc_") as tmp:
        try:
            iss = h.run(two_issues, tmp)
            if iss:
                h.run(accept_both, iss)
                h.run(review_fixes, iss)
            h.run(hak_not_found, tmp)
            h.run(audit_still_counts, tmp)
        finally:
            D.release_analysis(A)
            shutil.rmtree(os.path.join(D.WORKSPACE, A), ignore_errors=True)
    h.run(page)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
