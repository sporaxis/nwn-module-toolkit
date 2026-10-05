"""
End-to-end self-test. Builds the synthetic module (tests/make_test_module.py), runs the
whole pipeline and checks every planted problem is found. Then tampers with a clean
build and checks the audit FAILS. A second module (tests/make_tricky_module.py) holds the hard cases.

    python tests/run_tests.py            (prints PASS/FAIL per check; last line "N/M checks passed"; exit code 1
                                          on a failure)
    python tests/run_all.py              runs this and every other suite
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import json
import os
import sqlite3
import sys

import make_test_module  # noqa: E402
import nwn_analysis  # noqa: E402
import nwn_audit  # noqa: E402
import nwn_build  # noqa: E402
import nwn_index  # noqa: E402
import nwn_logs  # noqa: E402
import nwnlib as n  # noqa: E402
from _harness import BASE, check, fake_nwn_root  # noqa: E402,F401 - BASE/fake_nwn_root: older imports read them here

NO_COMP = dict(compiler=h.NO_COMPILER)          # audit_kwargs: the audit must not find a compiler installed here


def index_and_analyse(tmp):
    """The test module indexed with a fake game install, its custom tlk and a user override folder; returns
    (mod, out, root, report)."""
    mod = os.path.join(tmp, "testmod")
    make_test_module.build(mod)
    out = os.path.join(tmp, "analysis")
    root = fake_nwn_root(os.path.join(tmp, "nwnroot"), BASE)
    check("base-game KEY index reads", n.base_game_names(root) == set(BASE), n.base_game_names(root))
    tlk_path = os.path.join(mod, "testmod_custom.tlk")
    # a user folder with an override: the game loads it on top of everything, so the index must too
    user = os.path.join(tmp, "user"); os.makedirs(os.path.join(user, "override"))
    open(os.path.join(user, "override", "ovr_only.tga"), "wb").write(b"\0" * 32)
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=root, tlk=tlk_path, nwn_user=user)
    r = nwn_analysis.run_analysis(out, verbose=False)
    srcs = sqlite3.connect(os.path.join(out, "index.sqlite")).execute("SELECT kind, path FROM sources").fetchall()
    check("user override folder is indexed automatically (override source present)",
          any(k == "override" and p.endswith("override") for k, p in srcs), srcs)
    return mod, out, root, r


def findings(mod, out, r):
    """Every planted problem is found: issues, duplicates, impact, deletion status, quests, skins, AI, tlk, catalogue."""
    # --- format round trips
    for fn in sorted(os.listdir(mod)):
        ext = fn.rsplit(".", 1)[-1]
        if ext in n.GFF_EXTENSIONS:
            data = open(os.path.join(mod, fn), "rb").read()
            if n.gff_to_json(n.read_gff(n.write_gff(n.read_gff(data)))) != n.gff_to_json(n.read_gff(data)):
                check(f"GFF round trip {fn}", False)
                break
    else:
        check("GFF read/write round trip on every GFF file", True)

    iss = {(i["category"], i["node"]) for i in r["issues"]}
    det = " | ".join(i["detail"] for i in r["issues"])
    check("missing script 'not_here' found", ("missing_script", "bp:npc_merchant.utc") in iss, det)
    check("wrong script type bad_cond found", any(c == "wrong_script_type" for c, _ in iss), det)
    check("uncompiled rat_death found", ("not_compiled", "script:rat_death") in iss, det)
    check("compiled-without-source orphan_only found", ("compiled_without_source", "script:orphan_only") in iss, det)
    check("base-game scripts reported as info, not error",
          all(i["severity"] == "info" for i in r["issues"] if "nw_c2_default1" in i["detail"] and i["category"] != "resource_conflict"))
    check("dynamic ExecuteScript prefix not reported as missing", not any("'dyn_'" in i["detail"] for i in r["issues"]), det)

    cats = {g["category"]: g for g in r["duplicates"]}
    members = lambda cat: [{m["node"] for m in g["members"]} for g in r["duplicates"] if g["category"] == cat]  # noqa
    check("identical items sword_a/sword_b", {"bp:sword_a.uti", "bp:sword_b.uti"} in members("Item (uti) - identical"))
    check("same-stats item group includes sword_c", any("bp:sword_c.uti" in s for s in members("Item - same stats, different name/look")))
    check("shared tag KEY_RUSTY", {"bp:key_rusty.uti", "bp:key_rusty2.uti"} in members("Item - shared tag"))
    check("identical scripts at_give_reward/at_reward_copy",
          {"script:at_give_reward", "script:at_reward_copy"} in members("Script - identical code"))
    blocked = [g for g in r["duplicates"] if "script:dyn_1" in {m["node"] for m in g["members"]}]
    check("dyn_1 merge blocked (runtime-built name)", blocked and not blocked[0]["mergeable"])

    imp = r["impact"]
    check("inc_common is Critical (module OnLoad include)", imp["script:inc_common"]["level"] == "Critical", imp["script:inc_common"])
    check("area_enter is High (quest)", imp["script:area_enter"]["level"] == "High", imp["script:area_enter"])
    check("key_rusty item is High (quest via door + dialogue)", imp["bp:key_rusty.uti"]["level"] == "High")
    check("unused at_reward_copy is None", imp["script:at_reward_copy"]["level"] == "None", imp["script:at_reward_copy"])
    check("areas are not Critical just for being listed", imp["area:area002"]["level"] != "Critical", imp["area:area002"])

    dl = {d["node"]: d["status"] for d in r["deletions"]}
    for node, want in [("bp:unused_helm.uti", "Review"), ("asset:orphan_tex.tga", "Safe"), ("script:old_unused", "Safe"),
                       ("script:dead_lib", "Safe as group"), ("asset:unused_model.wok", "Safe as group"),
                       ("script:orphan_only", "Safe"), ("script:dyn_1", "Review"), ("asset:po_hero_h.tga", "Review"),
                       ("area:area003", "Review")]:
        check(f"delete status {node} = {want}", dl.get(node) == want, dl.get(node))
    for keep in ("script:inc_common", "model:c_rat", "asset:c_rat_tex.tga", "bp:key_rusty.uti", "dlg:merchant_dlg",
                 "script:sc_has_key", "bp:potion.uti"):
        check(f"in-use {keep} not offered for deletion", keep not in dl)

    it = {i["resref"]: i for i in r["items"]}
    check("key_rusty quest link q_rats", "q_rats" in it["key_rusty"]["quests"], it["key_rusty"])
    check("sword_b location is chest in Town", any("Town" in l["where"] and "Chest" in l["where"] for l in it["sword_b"]["locations"]),
          it["sword_b"]["locations"])
    sc = {s["name"]: s for s in r["scripts"]}
    check("description: sc_has_key mentions KEY_RUSTY", "KEY_RUSTY" in sc["sc_has_key"]["summary"], sc["sc_has_key"]["summary"])
    check("description: mod_load mentions library call", "SetTownOpen" in sc["mod_load"]["summary"], sc["mod_load"]["summary"])

    # --- quest health / orphans / skins
    qh = {q["tag"]: q for q in r["quests"]}
    check("quest: q_oldmine reported as legacy (referenced, not in journal, unused)", qh.get("q_oldmine", {}).get("health") == "legacy", qh.get("q_oldmine"))
    check("quest: q_rats needs Q_RELIC which is unobtainable", any(n_["name"] == "Q_RELIC" and n_["status"] == "unobtainable" for n_ in qh["q_rats"]["needs"]), qh["q_rats"]["needs"])
    check("quest: missing item resref 'mine_deed' flagged", any(n_["name"] == "mine_deed" and n_["status"] == "missing" for n_ in qh["q_oldmine"]["needs"]), qh["q_oldmine"]["needs"])
    oc = next((o for o in r["orphans"] if "q_oldmine" in o["quests"]), None)
    check("orphans: oldmine dialogue + scripts clustered as one legacy quest",
          oc and oc["kind"] == "legacy quest" and {m["node"] for m in oc["members"]} >= {"dlg:oldmine_dlg", "script:sc_oldmine", "script:at_oldmine", "script:q_oldmine_chk"}, oc)
    sk = {x["resref"]: x for x in r["skins"]}
    check("skins: rat_hide found with 2 properties, worn in hide slot by rat", sk.get("rat_hide") and sk["rat_hide"]["property_count"] == 2 and
          any(w["slot"] == "creature hide" for w in sk["rat_hide"]["worn_by"]), sk.get("rat_hide"))
    check("skins: base-game creature weapon nw_crewpsp not reported missing", "nw_crewpsp" not in sk or not sk["nw_crewpsp"]["type"].startswith("MISSING"), sk.get("nw_crewpsp"))
    check("skins: rat_hide not offered for deletion", "bp:rat_hide.uti" not in dl)

    # --- creature AI
    ai = {a["node"]: a for a in r["ai_findings"]}
    st_ = ai.get("bp:statue_npc.utc")
    check("creature AI: empty OnPhysicalAttacked and non-AI OnDamaged flagged", st_ and len(st_["problems"]) == 2 and
          any("OnPhysicalAttacked" in p_ for p_ in st_["problems"]) and any("noop_dmg" in p_ for p_ in st_["problems"]), st_)
    check("creature AI: rat with base-game scripts not flagged", "bp:rat.utc" not in ai, ai.get("bp:rat.utc"))

    # --- talk table
    tk = json.load(open(os.path.join(out, "catalog.json"), encoding="utf-8"))["tlk"]
    check("tlk: custom entry 0 resolves as the item name", any(i["resref"] == "tlk_blade" and i["name"] == "Blade of the Test" for i in r["items"]),
          [i for i in r["items"] if i["resref"] == "tlk_blade"])
    check("tlk: used/unused custom entries counted", tk["custom_entries"] == 3 and tk["custom_used"] == 1 and
          {u["index"] for u in tk["unused_custom"]} == {1, 3}, tk)
    check("tlk: broken reference #7 reported", any(b["strref"] == 0x01000007 for b in tk["broken"]) and
          any(i["category"] == "broken_strref" for i in r["issues"]), (tk["broken"], [i for i in r["issues"] if i["category"] == "broken_strref"]))

    # --- hak/content catalogue
    cat = json.load(open(os.path.join(out, "catalog.json"), encoding="utf-8"))
    ap = cat["tables"].get("appearance", {})
    check("catalogue: appearance row 0 (Rat) used by rat.utc, row 1 (Bear) unused",
          ap and [r["used"] for r in ap["rows"]] == [True, False] and "rat.utc" in ap["rows"][0]["used_by"], ap)
    check("catalogue: tileset tcn01 used by area001 (base game)", cat["tilesets"].get("tcn01", {}).get("used") and
          "area001.are" in cat["tilesets"]["tcn01"]["areas"], cat["tilesets"])



def log_monitor(tmp):
    """Repeated game-log errors are grouped and linked to the impact of the script behind them."""
    out = os.path.join(tmp, "analysis")
    logs = os.path.join(tmp, "logs.0"); os.makedirs(logs)
    with open(os.path.join(logs, "nwserverLog1.txt"), "w") as fh:
        fh.write("[Mon Sep 28 13:00:05] Script area_enter, OID: 80000010, Tag: AREA_TOWN, ERROR: TOO MANY INSTRUCTIONS\n"
                 "[Mon Sep 28 13:00:09] Script area_enter, OID: 80000010, Tag: AREA_TOWN, ERROR: TOO MANY INSTRUCTIONS\n")
    mon = nwn_logs.LogMonitor(out, [logs]); mon.poll()
    g = mon.report()["groups"]
    check("log monitor groups repeated errors and links impact", g and g[0]["count"] == 2 and g[0]["impact"] == "High", g)


def build_and_tamper(out, r):
    """A clean build passes its audit (compile skipped: no compiler); removing an in-use file makes it FAIL."""
    # --- build + audit (good)
    plan = {"delete": [d["node"] for d in r["deletions"]], "merge": [x["id"] for x in r["duplicates"] if x["mergeable"]]}
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    bad_checks = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED")]
    check("clean build audit passes every check (compile skipped: no compiler here)", not bad_checks, bad_checks)
    check("build carries the custom tlk next to the .mod", log.get("tlk_file") and os.path.isfile(log["tlk_file"]), log.get("tlk_file"))
    check("skipped compile never gives a clean PASS", log["audit"]["verdict"] == "PASS WITH WARNINGS", log["audit"]["verdict"])
    check("Review items refused without allow_review", any(x["node"] == "area:area003" for x in log["refused"]))
    check("group rule: old_enter kept because area003 kept", any(x["node"] == "script:old_enter" for x in log["refused"]))
    check("merge rewrote chest's sword_b -> sword_a",
          any(c["old"] == "sword_b" and c["new"] == "sword_a" for c in log["field_changes"]), log["field_changes"])
    check(".mod readable", n.Erf(log["mod_file"]).entries)

    # --- tamper: remove an in-use file from the clean build, audit must FAIL
    clean = os.path.join(out, "build", os.path.basename(log["output"]))
    os.remove(os.path.join(clean, "key_rusty.uti"))
    a = nwn_audit.audit(out, os.path.join(out, "build"), os.path.basename(clean), compiler=h.NO_COMPILER, verbose=False)
    failed = {c["id"] for c in a["checks"] if c["status"] == "FAIL"}
    check("tampered build FAILS audit", a["verdict"] == "FAIL", a["verdict"])
    check("tamper detected by removals + .mod + in-use checks", {"removals", "mod", "in_use"} <= failed, failed)


def without_game_install(tmp, mod):
    """Without a configured game install, unused assets are Review (they could be base-game overrides)."""
    out2 = os.path.join(tmp, "analysis_noroot")
    nwn_index.run_index(mod, out=out2, verbose=False)
    r2 = nwn_analysis.run_analysis(out2, verbose=False)
    st2 = {d["node"]: d["status"] for d in r2["deletions"]}
    check("no install configured: unused texture is Review", st2.get("asset:orphan_tex.tga") == "Review", st2.get("asset:orphan_tex.tga"))


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_selftest_") as tmp:
        got = h.run(index_and_analyse, tmp)
        if got:
            mod, out, root, r = got
            h.run(findings, mod, out, r)
            h.run(log_monitor, tmp)
            h.run(build_and_tamper, out, r)
            h.run(without_game_install, tmp, mod)
            h.run(tricky_tests, tmp, root)
    return h.summary()



def tricky_tests(tmp, root):
    """Cases found by the independent review: runtime-built names, unrewritable references, keepers,
    non-Western text, palettes, unsafe names in a .mod, stale .mod files."""
    import make_tricky_module
    mod = os.path.join(tmp, "tricky")
    make_tricky_module.build(mod)
    out = os.path.join(tmp, "tricky_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=root)
    r = nwn_analysis.run_analysis(out, verbose=False)
    st = {d["node"]: d["status"] for d in r["deletions"]}
    for sc in ("script:i_magicwand", "script:evt_1", "script:guard_ud", "script:s_1"):
        check(f"runtime-built/tag-based {sc} is never plain Safe", st.get(sc) in (None, "Review"), st.get(sc))
    grp = {frozenset(m["node"] for m in g_["members"]): g_ for g_ in r["duplicates"]}
    gob = next(g_ for k, g_ in grp.items() if "bp:goblin_b.utc" in k)
    check("merge blocked: goblin_b named in a waypoint variable", not gob["mergeable"], gob)
    ev = next(g_ for k, g_ in grp.items() if "script:ev_b" in k)
    check("compiled script chosen as keeper (ev_b over uncompiled ev_a)", ev["keeper"] == "script:ev_b", ev["keeper"])
    hb = next(g_ for k, g_ in grp.items() if "script:hb_b" in k)
    # keeper deletion is refused
    plan = dict(delete=[hb["keeper"]] + [d["node"] for d in r["deletions"] if d["status"] != "Review"],
                merge=[g_["id"] for g_ in r["duplicates"] if g_["mergeable"]])
    os.makedirs(os.path.join(out, "build"), exist_ok=True)
    open(os.path.join(out, "build", "tricky_clean.mod"), "wb").write(b"stale")
    try:
        nwn_build.build(out, plan, verbose=False)
        refused = False
    except ValueError as ex:
        refused = "not made by a toolkit build" in str(ex)
    check("a .mod the toolkit did not make is never replaced", refused and
          open(os.path.join(out, "build", "tricky_clean.mod"), "rb").read() == b"stale")
    for bad_name, why in (("..", "name"), ("x/y", "name"), (os.path.basename(mod), "original")):
        try:
            nwn_build.build(out, plan, out_dir=os.path.dirname(mod), name=bad_name, verbose=False)
            ok_ = False
        except ValueError as ex:
            ok_ = True
        check(f"build refuses a dangerous target ({why}: {bad_name!r})", ok_ and os.path.isdir(mod))
    try:
        nwn_build.build(out, dict(merge=["abc"]), verbose=False)
        ok_ = False
    except ValueError as ex:
        ok_ = "not a duplicate group number" in str(ex)
    check("a bad plan is refused before anything is touched", ok_ and os.path.exists(os.path.join(out, "build", "tricky_clean.mod")))
    os.makedirs(os.path.join(out, "build", "tricky_clean"), exist_ok=True)
    open(os.path.join(out, "build", "tricky_clean", nwn_build.BUILD_MARKER), "w").write("x")   # a previous build of ours
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    check("keeper of a merge is never deleted", hb["keeper"] not in {d["node"] for d in log["deleted"]}, log["deleted"])
    check("keeper is the in-use member (hb_b)", hb["keeper"] == "script:hb_b", hb["keeper"])
    clean = log["output"]
    ob = open(os.path.join(mod, "area001.git"), "rb").read()
    cb = open(os.path.join(clean, "area001.git"), "rb").read()
    check("non-cp1252 bytes survive a rewrite losslessly", b"Q\x81\x8dQ" in cb and b"Q\x81\x8dQ" in ob)
    pal = n.read_gff_file(os.path.join(clean, "itempalcus.itp"))
    rr = [x for _, lab, _, x, _ in n.iter_gff_leaves(pal) if lab == "RESREF"]
    check("palette cleanup respects blueprint type (item goblin_b kept)", "goblin_b" in [x.lower() for x in rr], rr)
    check("stale .mod replaced by this build", open(log["mod_file"], "rb").read(4) == b"MOD ")
    bad = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED")]
    check("tricky build audit passes", not bad, bad)

    # unsafe names inside a .mod are neutralised (no writing outside the build folder)
    evil = os.path.join(tmp, "evil.mod")
    good = [(e_, x_, d_) for e_, x_, d_ in [("module", "ifo", n.write_gff(n.GffRoot("IFO ")))]]
    n.write_erf(evil, good, "MOD ")
    data = bytearray(open(evil, "rb").read())
    data[160:176] = b"../../../pwn1".ljust(16, b"\0")  # corrupt the first key entry's name
    open(evil, "wb").write(bytes(data))
    e = n.Erf(evil)
    check("path-like names in a .mod are rejected", e.bad_names and all(n.safe_resref(x.resref) for x in e.entries),
          (e.bad_names, [x.resref for x in e.entries]))


if __name__ == "__main__":
    sys.exit(main())
