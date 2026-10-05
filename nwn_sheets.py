"""
nwn_sheets.py - spreadsheet round-trips for the Issues, Safe to delete and Duplicates pages.

Why this exists
---------------
On a large module some decisions are quicker in a spreadsheet: marking 300 issues "accepted - by design", or going
through 2,000 deletion candidates with a colleague. Each page exports a CSV; the builder fills one column and imports
the file again. This module turns such a file into a plan the page shows before anything is saved:
    plan_issues      by_design yes / no per issue (key column)          -> accept / unaccept
    plan_deletions   delete yes / no per artefact (node column)         -> tick / untick on Safe to delete
    plan_merges      merge yes / no per duplicate group (group column)  -> tick / untick on Duplicates
Every row of the file ends up in exactly one place - changed, unchanged (empty cell, or already so) or refused with
its row number and why - and nothing is guessed: rows for one thing that disagree, or a refused row next to a good
one, refuse that thing on every row.

The Safe to delete and Duplicates ticks live in the browser (the page's plan), so for those the page applies the
plan; issues accepted as by design live in <analysis>/accepted_issues.json, written by the dashboard
(apply_issue_plan works out the new contents). CSV reading (encodings, delimiters, row numbers) is nwn_csvio's.

Reads only the data it is given; writes nothing.
"""
from __future__ import annotations

import re

import nwn_csvio

YES = {"yes", "y", "true", "1", "x"}
NO = {"no", "n", "false", "0"}


def issue_key(i):
    """The key an accepted issue is stored under: category|node|label (the Issues page uses the same)."""
    return f"{i['category']}|{i['node']}|{i['label']}"


def issue_sig(detail):
    """The issue's detail with every number masked: an accepted issue counts again only if its text changes in
    another way after a re-analysis (the Issues page uses the same rule)."""
    return re.sub(r"\d+", "#", str(detail or ""))


def all_issues(report, compile_result=None):
    """The issues the Issues page lists: the report's, plus one per official-compiler error when the compiler ran
    (compile.json: {"status": "ran", "results": {script: [error lines]}})."""
    out = list(report.get("issues") or [])
    cr = compile_result or {}
    if cr.get("status") == "ran":
        for sc, errs in (cr.get("results") or {}).items():
            out += [dict(severity="error", category="compile_error", node="script:" + sc, label=sc, detail=e)
                    for e in errs]
    return out


def _yes_no(text):
    """'yes' -> True, 'no' -> False, '' -> None; anything else raises ValueError."""
    v = nwn_csvio.uncell(text).lower()
    if not v:
        return None
    if v in YES:
        return True
    if v in NO:
        return False
    raise ValueError(f"'{nwn_csvio.uncell(text)}' is not yes or no")


def _resolve(rows, value_col, find):
    """Shared walk of an import: for each row read value_col (yes/no/empty) and find(row) -> (thing key, thing,
    None) or (None, None, why). Returns (decisions {key: (value, thing, first row)}, refused, unchanged,
    duplicates). A thing whose rows disagree, or that has one refused row, is refused on all its rows."""
    refused, unchanged, per = [], 0, {}
    for r in rows:
        try:
            v = _yes_no(r.get(value_col, ""))
        except ValueError as ex:
            v, bad = None, str(ex)
        else:
            bad = None
        if v is None and not bad:
            unchanged += 1
            continue
        key, thing, why = find(r)
        why = bad or why
        if key is None:
            refused.append(dict(row=r["_row"], item=r.get("_label", ""), why=why))
            continue
        per.setdefault(key, []).append((r["_row"], v, why, thing))
    decisions, dupes = {}, 0
    for key, lst in per.items():
        rows_ = ", ".join(str(x[0]) for x in lst)
        bad = [x for x in lst if x[2]]
        if bad or len({x[1] for x in lst}) > 1:
            for row, _v, why, _t in lst:
                refused.append(dict(row=row, item=str(key), why=why or (
                    f"rows {rows_} disagree about this one" if not bad else
                    f"another row for this one was refused (rows {rows_})")))
            continue
        dupes += len(lst) - 1
        decisions[key] = (lst[0][1], lst[0][3], lst[0][0])
    refused.sort(key=lambda x: x["row"])
    return decisions, refused, unchanged, dupes


def plan_issues(issues, accepted, data):
    """What importing a by-design spreadsheet would do (a preview; writes nothing).

    issues: all_issues(); accepted: the current accepted_issues.json dict; data: the CSV bytes, with columns key and
    by_design (yes / no / empty), optionally note and detail (detail picks between issues that share a key, e.g. one
    script's compiler errors). Returns dict(rows, accept [{key, sig, category, label, note, row}], unaccept [{key,
    label, row}], unchanged, refused [{row, item, why}], duplicates). Raises ValueError for an unreadable file or a
    missing column."""
    rows = nwn_csvio.read_upload(data, required=("key", "by_design"))
    by_key = {}
    for i in issues:
        by_key.setdefault(issue_key(i), []).append(i)

    def find(r):
        k = nwn_csvio.uncell(r["key"])
        r["_label"] = k
        lst = by_key.get(k)
        if not lst:
            return None, None, "no issue with this key in the analysis (export again: it may have been re-run)"
        det = issue_sig(nwn_csvio.uncell(r.get("detail", "")))
        pick = next((i for i in lst if issue_sig(i["detail"]) == det), lst[0]) if det else lst[0]
        return k, (pick, nwn_csvio.uncell(r.get("note", ""))), None
    dec, refused, unchanged, dupes = _resolve(rows, "by_design", find)
    out = dict(rows=len(rows), accept=[], unaccept=[], refused=refused, duplicates=dupes)
    for k, (v, (i, note), row) in dec.items():
        cur = accepted.get(k)
        sig = issue_sig(i["detail"])
        if v:
            if cur and cur.get("sig") == sig and (cur.get("note") or "") == (note or cur.get("note") or ""):
                unchanged += 1
            else:
                out["accept"].append(dict(key=k, sig=sig, category=i["category"], label=i["label"],
                                          note=note or (cur or {}).get("note", ""), row=row))
        elif cur:
            out["unaccept"].append(dict(key=k, label=i["label"], row=row))
        else:
            unchanged += 1
    out["unchanged"] = unchanged
    for k in ("accept", "unaccept"):
        out[k].sort(key=lambda x: x["row"])
    return out


def apply_issue_plan(accepted, plan, now):
    """The accepted_issues.json contents after a plan: accept entries added (text fields length-capped as the
    single-issue tick does), unaccept entries removed. now: the time text to record. Returns a new dict."""
    out = dict(accepted)
    for a in plan["accept"]:
        out[a["key"]] = dict(sig=str(a["sig"])[:4000], note=str(a.get("note") or "")[:2000],
                             category=str(a["category"])[:200], label=str(a["label"])[:500], accepted_at=now)
    for u in plan["unaccept"]:
        out.pop(u["key"], None)
    return out


def plan_deletions(report, data, allow_review):
    """What importing a Safe-to-delete spreadsheet would do: columns node and delete (yes / no / empty). Review
    items can be ticked only when allow_review is on (as on the page). Returns dict(rows, tick [node], untick [node],
    unchanged, refused, duplicates); the page applies tick/untick to its plan. Writes nothing."""
    rows = nwn_csvio.read_upload(data, required=("node", "delete"))
    known = {d["node"]: d for d in report.get("deletions") or []}

    def find(r):
        nd = nwn_csvio.uncell(r["node"])
        r["_label"] = nd
        d = known.get(nd)
        if not d:
            return None, None, "not on the Safe to delete list (it is in use, or the module was analysed again)"
        return nd, d, None
    dec, refused, unchanged, dupes = _resolve(rows, "delete", find)
    out = dict(rows=len(rows), tick=[], untick=[], refused=refused, duplicates=dupes, unchanged=unchanged)
    for nd, (v, d, row) in dec.items():
        if v and d["status"] == "Review" and not allow_review:
            refused.append(dict(row=row, item=nd, why="marked Review - tick 'allow Review items' on Safe to delete "
                                                      "first: " + (d.get("reason") or "")[:200]))
        else:
            out["tick" if v else "untick"].append(nd)
    refused.sort(key=lambda x: x["row"])
    return out


def plan_merges(report, data):
    """What importing a Duplicates spreadsheet would do: columns group and merge (yes / no / empty). Only groups
    the analysis marked mergeable can be ticked. Returns dict(rows, tick [group id], untick [group id], unchanged,
    refused, duplicates); the page applies it. Writes nothing."""
    rows = nwn_csvio.read_upload(data, required=("group", "merge"))
    groups = {g["id"]: g for g in report.get("duplicates") or []}

    def find(r):
        txt = nwn_csvio.uncell(r["group"]).lstrip("#")
        r["_label"] = txt
        if not txt.isdigit():
            return None, None, f"'{txt}' is not a group number"
        g = groups.get(int(txt))
        if not g:
            return None, None, f"no duplicate group #{txt} (the module may have been analysed again)"
        return g["id"], g, None
    dec, refused, unchanged, dupes = _resolve(rows, "merge", find)
    out = dict(rows=len(rows), tick=[], untick=[], refused=refused, duplicates=dupes, unchanged=unchanged)
    for gid, (v, g, row) in dec.items():
        if v and not g.get("mergeable"):
            refused.append(dict(row=row, item=f"#{gid}", why="this group needs a manual decision: " +
                                ("; ".join(g.get("blocked") or []) or "it can't be merged automatically")[:300]))
        else:
            out["tick" if v else "untick"].append(gid)
    refused.sort(key=lambda x: x["row"])
    return out
