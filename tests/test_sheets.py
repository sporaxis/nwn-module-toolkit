"""
Tests for spreadsheet round-trips on the Issues, Safe to delete and Duplicates pages (nwn_sheets.py) and their
dashboard endpoints: every row of an imported CSV is accounted for, a preview writes nothing, and nothing is guessed.
    python tests/test_sheets.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import json
import os
import shutil
import subprocess
import sys

import nwn_dashboard as D  # noqa: E402
import nwn_sheets as SH  # noqa: E402
from _harness import check  # noqa: E402

REPORT = dict(
    issues=[dict(severity="error", category="missing_script", node="bp:a.utc", label="Goblin", detail="script x9 missing"),
            dict(severity="warning", category="shared_tag", node="tag:T", label="T", detail="3 objects share tag T"),
            dict(severity="info", category="base_game", node="script:nw_x", label="nw_x", detail="base game script")],
    deletions=[dict(node="script:old1", label="old1", status="Safe", files=["old1.nss"], bytes=10),
               dict(node="script:old2", label="old2", status="Review", files=["old2.nss"], bytes=20),
               dict(node="bp:junk.uti", label="Junk", status="Safe as group", files=["junk.uti"], bytes=30)],
    duplicates=[dict(id=1, category="Script - identical code", mergeable=True, blocked=[], keeper="script:a",
                     members=[dict(node="script:a", label="a"), dict(node="script:b", label="b")]),
                dict(id=2, category="Item - same display name", mergeable=False, blocked=["needs a person"], keeper="bp:x.uti",
                     members=[dict(node="bp:x.uti", label="X"), dict(node="bp:y.uti", label="X")])])
COMPILE = dict(status="ran", results={"bad_script": ["line 3: syntax error", "line 9: undefined"]})


def issues():
    """Issues: the list (report + compiler errors), the signature, the import plan and applying it."""
    lst = SH.all_issues(REPORT, COMPILE)
    check("issues: the report's issues plus one per compiler error", len(lst) == 5 and
          sum(1 for i in lst if i["category"] == "compile_error") == 2, lst)
    check("issues: the signature masks numbers, like the page", SH.issue_sig("script x9 missing at 12") == "script x# missing at #")
    k1, k2 = SH.issue_key(lst[0]), SH.issue_key(lst[1])
    accepted = {k2: dict(sig=SH.issue_sig(lst[1]["detail"]), note="old note", category="shared_tag", label="T")}
    csv = (f"key,severity,by_design,note\n{k1},error,yes,set by DM tool\n{k2},warning,no,\n"
           f"bad|key|x,error,yes,\n{SH.issue_key(lst[2])},info,,\n{k1},error,maybe,\n").encode()
    p = SH.plan_issues(lst, accepted, csv)
    check("issues import: yes accepts (with the note), no un-accepts",
          [(a["key"], a["note"]) for a in p["accept"]] == [] and [u["key"] for u in p["unaccept"]] == [k2], p)
    why = {r["row"]: r["why"] for r in p["refused"]}
    check("issues import: an unknown key and a value that isn't yes/no are refused; rows for one issue that disagree "
          "refuse it", sorted(why) == [2, 4, 6] and "no issue" in why[4] and "yes or no" in why[6], why)
    check("issues import: blank by_design is unchanged; every row accounted for",
          p["unchanged"] == 1 and p["rows"] == 5 == len(p["accept"]) + len(p["unaccept"]) + p["unchanged"] + len(p["refused"]), p)
    p2 = SH.plan_issues(lst, accepted, f"KEY;By_Design;Note\r\n{k1};Yes;why\r\n{k2};yes;old note\r\n".encode("cp1252"))
    check("issues import: Excel's semicolon file; an issue already accepted with the same note is unchanged",
          [(a["key"], a["sig"], a["note"]) for a in p2["accept"]] == [(k1, "script x# missing", "why")] and p2["unchanged"] == 1, p2)
    new = SH.apply_issue_plan(accepted, p2, now="2026-10-05 12:00")
    check("issues apply: accepted entries carry sig, note, category, label and the time",
          new[k1] == dict(sig="script x# missing", note="why", category="missing_script", label="Goblin",
                          accepted_at="2026-10-05 12:00") and k2 in new, new)
    gone = SH.apply_issue_plan(new, SH.plan_issues(lst, new, f"key,by_design\n{k1},no\n".encode()), now="x")
    check("issues apply: no removes the acceptance", k1 not in gone and k2 in gone, gone)
    ce = [i for i in lst if i["category"] == "compile_error"]
    p3 = SH.plan_issues(lst, {}, f"key,detail,by_design\n{SH.issue_key(ce[1])},{ce[1]['detail']},yes\n".encode())
    check("issues import: of several issues sharing a key (one script's compiler errors), the row's detail picks one",
          p3["accept"][0]["sig"] == SH.issue_sig(ce[1]["detail"]), p3)


def deletions_and_merges():
    """Safe to delete and Duplicates: which ticks an import sets or clears; Review needs 'allow Review'."""
    csv = b"node,delete\nscript:old1,yes\nscript:old2,yes\nbp:junk.uti,no\nscript:gone,yes\nscript:old1,\n"
    p = SH.plan_deletions(REPORT, csv, allow_review=False)
    why = {r["row"]: r["why"] for r in p["refused"]}
    check("delete import: yes ticks, no clears, a Review item needs 'allow Review items', an unknown one is refused",
          p["tick"] == ["script:old1"] and p["untick"] == ["bp:junk.uti"] and "allow Review" in why.get(3, "") and
          "Safe to delete list" in why.get(5, "") and p["unchanged"] == 1, p)
    p2 = SH.plan_deletions(REPORT, csv, allow_review=True)
    check("delete import: with 'allow Review items' on, a Review item can be ticked", "script:old2" in p2["tick"], p2)
    m = SH.plan_merges(REPORT, b"group,merge\n1,yes\n2,yes\n9,no\n1,yes\nabc,yes\n")
    why = {r["row"]: r["why"] for r in m["refused"]}
    check("merge import: a mergeable group is ticked once; a manual-decision group, an unknown and a bad number are refused",
          m["tick"] == [1] and m["duplicates"] == 1 and "manual decision" in why.get(3, "") and "no duplicate group" in why.get(4, "")
          and "number" in why.get(6, ""), m)
    try:
        SH.plan_merges(REPORT, b"group,keep\n1,yes\n")
        ok = False
    except ValueError as ex:
        ok = "merge" in str(ex)
    check("merge import: a file without the merge column is refused, naming it", ok)


def endpoints():
    """The dashboard endpoints: bulk accept, issues import (preview / apply), plan import (preview only)."""
    a = "sheettest"
    d = os.path.join(D.WORKSPACE, a)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh)
    with open(os.path.join(d, "compile.json"), "w", encoding="utf-8") as fh:
        json.dump(COMPILE, fh)
    lst = SH.all_issues(REPORT, COMPILE)
    items = [dict(key=SH.issue_key(i), sig=SH.issue_sig(i["detail"]), category=i["category"], label=i["label"]) for i in lst[:2]]
    r = D.POST_ROUTES["/api/issues/accept_many"]({"a": a, "items": items, "accepted": True, "note": "bulk"})
    check("api: accept many at once, with one note", sorted(r) == sorted(x["key"] for x in items) and
          all(v["note"] == "bulk" for v in r.values()), r)
    r = D.POST_ROUTES["/api/issues/accept_many"]({"a": a, "items": items[:1], "accepted": False})
    check("api: un-accept many", list(r) == [items[1]["key"]], r)
    for bad in ({"a": a, "items": "x", "accepted": True}, {"a": a, "items": [{"key": "nobars"}], "accepted": True}):
        try:
            D.POST_ROUTES["/api/issues/accept_many"](bad)
            ok = False
        except ValueError:
            ok = True
        check("api: a malformed bulk accept is refused", ok)
    up = D.UPLOAD_ROUTES["/api/issues/import"]
    csv = f"key,by_design,note\n{items[0]['key']},yes,from sheet\n".encode()
    pv = up({"a": a}, csv)
    check("api: issues import preview writes nothing", len(pv["accept"]) == 1 and
          items[0]["key"] not in D.GET_ROUTES["/api/issues/accepted"]({"a": a}), pv)
    ap = up({"a": a, "apply": "1"}, csv)
    check("api: issues import with apply=1 stores it", ap["accepted"][items[0]["key"]]["note"] == "from sheet" and
          D.GET_ROUTES["/api/issues/accepted"]({"a": a}) == ap["accepted"], ap)
    pl = D.UPLOAD_ROUTES["/api/plan/import"]({"a": a, "kind": "delete", "allow_review": "1"}, b"node,delete\nscript:old2,yes\n")
    check("api: plan import (Safe to delete) returns the ticks for the page, writing nothing", pl["tick"] == ["script:old2"], pl)
    pm = D.UPLOAD_ROUTES["/api/plan/import"]({"a": a, "kind": "merge"}, b"group,merge\n1,no\n")
    check("api: plan import (Duplicates)", pm["untick"] == [1], pm)
    shutil.rmtree(d, ignore_errors=True)


def page_script():
    """The three pages in dashboard.html: bulk accept, export and import controls; the import previews first."""
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: Issues has Accept all shown, Export CSV and Import (not in the static report)",
          all(x in html for x in ('id="iacc"', 'id="icsv"', 'id="iimp"')))
    check("page: Safe to delete and Duplicates have Export CSV and Import",
          all(x in html for x in ('id="dcsv"', 'id="dimp"', 'id="gcsv"', 'id="gimp"')))
    check("page: imports go through one preview helper that applies only after OK", "async function sheetImport(" in html)
    node = shutil.which("node")
    if not node:
        h.skip("page: issue key/signature match the server's", "Node is not installed")
        return
    start = html.index("const issueKey = ")
    js = html[start:html.index("\n", html.index("const issueSig = "))] + """
const i = {category: "c", node: "n", label: "l", detail: "x 12 y 3"};
console.log(JSON.stringify([issueKey(i), issueSig(i)]));"""
    out = json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60).stdout)
    check("page: the page's issue key and signature are the server's", out ==
          [SH.issue_key(dict(category="c", node="n", label="l")), SH.issue_sig("x 12 y 3")], out)


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    for fn in (issues, deletions_and_merges, endpoints, page_script):
        h.run(fn)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
