"""
Scenario regression tests: two realistic synthetic modules written by mock testers ('Dave' - persistent world;
'Marta' - messy legacy campaign), an edit/generate flow, 2da conflicts between haks, lean haks, deletion rules,
the impact engine, what a rebuild keeps from the original .mod, and index/analysis edge cases (load order, resume
after a hard kill, hak-name matching, tag-based scripts).
    python tests/test_scenarios.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from unittest import mock

import nwn_analysis  # noqa: E402
import nwn_build  # noqa: E402
import nwn_edit  # noqa: E402
import nwn_catalog  # noqa: E402
import nwn_index  # noqa: E402
import nwnlib as n  # noqa: E402
from _harness import BASE, check, fake_nwn_root, skip  # noqa: E402
from _gff import st, root, loc, R, X, LST, w  # noqa: E402

HERE = h.HERE
NO_COMP = dict(compiler=h.NO_COMPILER)          # audit_kwargs for builds whose audit must not find a real compiler


def gen(script, tmp):
    tag = script.replace("make_", "").replace(".py", "")
    mod, hak = os.path.join(tmp, tag), os.path.join(tmp, tag + "_hak")
    subprocess.run([sys.executable, os.path.join(HERE, "scenarios", script), mod, hak], check=True,
                   capture_output=True)
    return mod, hak


def status_map(rep):
    return {d["node"]: d["status"] for d in rep["deletions"]}


def dave(tmp, root):
    mod, hak = gen("make_dave_module.py", tmp)
    out = os.path.join(tmp, "dave")
    nwn_index.run_index(mod, haks=[hak], out=out, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    st = status_map(rep)
    iss = {(i["category"], i["node"]) for i in rep["issues"]}
    check("dave: missing hak reported", ("hak_not_loaded", "hak:pw_hak2_missing") in iss)
    why = {d["node"]: d["reason"] for d in rep["deletions"]}
    # Dave's module has no item palette file, so unused item blueprints are Review for that reason alone
    check("dave: missing hak does NOT downgrade unrelated items to Review",
          "pw_hak2_missing" not in why.get("bp:unused_item_a.uti", "x") and "palette" in why.get("bp:unused_item_a.uti", ""),
          why.get("bp:unused_item_a.uti"))
    check("dave: unused key blueprint is Review only because no palette shows whether DMs can spawn it",
          why.get("bp:door_key_unused.uti", "").startswith("the module has no item palette file"), why.get("bp:door_key_unused.uti"))
    check("dave: dynamic dyn_event never Safe", st.get("script:dyn_event") in (None, "Review"), st.get("script:dyn_event"))
    for keep in ("script:pw_inc", "script:tb_healkit", "script:door_custom", "bp:key_dungeon2.uti", "bp:reward_item_vc.uti", "bp:store_only_item.uti"):
        check(f"dave: in-use {keep} not deletable", keep not in st, st.get(keep))
    imp = rep["impact"]
    check("dave: pw_inc Critical", imp["script:pw_inc"]["level"] == "Critical", imp["script:pw_inc"])
    check("dave: orphan area impact None (unreachable)", imp["area:abandoned_test"]["level"] == "None", imp["area:abandoned_test"])
    stubs = [g for g in rep["duplicates"] if "script:rat_death" in {m["node"] for m in g["members"]}]
    check("dave: stub-script mega group is not auto-mergeable", stubs and not stubs[0]["mergeable"], stubs and stubs[0]["blocked"])
    sc = {s["name"]: s for s in rep["scripts"]}
    ls = [s for s in rep["scripts"] if s["details"] and any("[Place:" in d for d in s["details"])]
    check("dave: Lilac Soul placement hint captured", ls, [s["name"] for s in rep["scripts"]][:5])
    check("dave: 17-char script name flagged", any(c == "name_too_long" for c, _ in iss))
    plan = {"delete": [d["node"] for d in rep["deletions"] if d["status"] != "Review"],
            "merge": [g["id"] for g in rep["duplicates"] if g["mergeable"]]}
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    bad = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED")]
    # the 17-char name blocks the .mod on purpose; everything else must pass
    check("dave: audit passes with haks carried through (only .mod blocked by 17-char name)",
          [c["id"] for c in bad] == ["mod"], bad)


def marta(tmp, root):
    mod, hak = gen("make_marta_module.py", tmp)
    out = os.path.join(tmp, "marta")
    nwn_index.run_index(mod, haks=[hak], out=out, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    st = status_map(rep)
    iss = {(i["category"], i["node"]) for i in rep["issues"]}
    cats = {i["category"] for i in rep["issues"]}
    check("marta: include cycle detected", "include_cycle" in cats, cats)
    check("marta: missing #include for called library function", "missing_include" in cats, cats)
    check("marta: missing journal quest detected", "missing_quest" in cats, cats)
    check("marta: resource conflict module vs hak detected", "resource_conflict" in cats, cats)
    cat = json.load(open(os.path.join(out, "catalog.json"), encoding="utf-8"))
    hk = next(iter(cat["haks"].values()), None)
    check("marta: hak catalogue lists the hak with used/unused files and shadowing", hk and hk["files"] > 0 and cat["summary"]["shadowed"] > 0,
          (hk and {k: len(v) for k, v in hk["inventory"].items()}, cat["summary"]))
    check("marta: base-game shadow script never Safe", st.get("script:nw_c2_default9") in (None, "Review"), st.get("script:nw_c2_default9"))
    sc = {s["name"]: s for s in rep["scripts"]}
    bad = next((s for s in rep["scripts"] if "NOT included" in s["summary"]), None)
    check("marta: description says un-included call is a compile error", bad is not None, [s["summary"] for s in rep["scripts"] if "inc_c" in s["summary"]][:2])
    a8 = next((a for a in rep["areas"] if a["resref"] == "area08"), None)
    check("marta: scripted jump to secret area shows as a transition", a8 and any(t.get("kind") == "script" for t in
          [t for a in rep["areas"] for t in a["transitions"] if t.get("to_area") == "area08"]),
          [t for a in rep["areas"] for t in a["transitions"] if t.get("to_area") == "area08"])
    big = open(os.path.join(out, "conversations", "big_dlg.md"), encoding="utf-8").read()
    check("marta: conversation rendering lists unreached nodes", "not reached" in big.lower() or "unreached" in big.lower(), big[-300:])
    plan = {"delete": [d["node"] for d in rep["deletions"] if d["status"] != "Review"],
            "merge": [g["id"] for g in rep["duplicates"] if g["mergeable"]]}
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    bad = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED")]
    check("marta: build audit passes with haks carried through (only .mod blocked by 17+-char names)",
          [c["id"] for c in bad] == ["mod"], bad)
    return out


def edit_flow(tmp, root):
    """Edit a script, edit an item's cost via JSON, generate a new script; build; audit passes."""
    import make_test_module
    mod = os.path.join(tmp, "editmod"); make_test_module.build(mod)
    out = os.path.join(tmp, "edit_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    nwn_edit.save_text(out, "rat_death.nss", 'void main() { SpeakString("squeak"); }\n')
    j = json.loads(open(os.path.join(out, "json", "potion.uti.json"), encoding="utf-8").read())
    j["Cost"]["value"] = 999
    nwn_edit.save_gff_json(out, "potion.uti", json.dumps(j))
    try:
        nwn_edit.save_gff_json(out, "potion.uti", json.dumps({**j, "TemplateResRef": {"type": "resref", "value": "x" * 20}}))
        check("edit: over-long resref rejected", False)
    except Exception:
        check("edit: over-long resref rejected", True)
    r = nwn_edit.generate_script(dict(name="gen_gate", event="OnUsed", description="test",
                                      conditions=[dict(key="has_item", values=dict(tag="KEY_RUSTY"))],
                                      actions=[dict(key="unlock", values=dict(tag="DOOR_CELLAR")), dict(key="once")]),
                                 {"SetTownOpen": "inc_common"})
    nwn_edit.save_text(out, "gen_gate.nss", r["source"], is_new=True)
    r2 = nwn_edit.generate_script(dict(name="gen_lib", event="OnEnter", actions=[dict(key="execute", values=dict(script="x"))]), {"SetTownOpen": "inc_common"})
    check("generator: no #include when no library function used", '#include' not in r2["source"], r2["source"])
    src3 = nwn_edit.generate_script(dict(name="gen_lib2", event="OnEnter", actions=[dict(key="speak", values=dict(text="SetTownOpen()"))]), {"SetTownOpen": "inc_common"})["source"]
    check("generator: adds #include for a module library function", '#include "inc_common"' in src3, src3)
    plan = {"delete": [], "merge": []}
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    clean = log["output"]
    check("edit: edited script in clean build", 'squeak' in open(os.path.join(clean, "rat_death.nss")).read())
    pot = n.read_gff_file(os.path.join(clean, "potion.uti"))
    check("edit: item cost edited via JSON in clean build", pot.get("Cost") == 999, pot.get("Cost"))
    check("edit: generated script added to clean build and .mod", os.path.isfile(os.path.join(clean, "gen_gate.nss")) and
          any(e.filename == "gen_gate.nss" for e in n.Erf(log["mod_file"]).entries))
    bad = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED") and c["id"] != "edits_compiled"]
    check("edit: audit passes with edits + new file logged", not bad, bad)
    ec = next(c for c in log["audit"]["checks"] if c["id"] == "edits_compiled")
    check("edit: audit reports edited scripts need compiling (no compiler here)", ec["status"] == "WARN", ec)
    check("edit: change log lists edits", set(log["edited"]) == {"rat_death.nss", "potion.uti"} and log["added"] == ["gen_gate.nss"], (log["edited"], log["added"]))


def hakconflict(tmp, root_unused):
    import make_hakconflict_module as mk
    base = os.path.join(tmp, "hc_root")
    mod, ha, hb = os.path.join(tmp, "hc"), os.path.join(tmp, "hc_hak_a"), os.path.join(tmp, "hc_hak_b")
    mk.build(mod, ha, hb, base)
    out = os.path.join(tmp, "hc_analysis")
    nwn_index.run_index(mod, haks=[ha, hb], out=out, verbose=False, nwn_root=base)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    cat = json.load(open(os.path.join(out, "catalog.json"), encoding="utf-8"))
    cx = {(c["table"], c["kind"]): c for c in cat["twoda_conflicts"]}
    hv = cx.get(("appearance", "hak-vs-hak"))
    check("2da: hak_b's appearance rows 5-6 reported as lost to hak_a", hv and {r["row"] for r in hv["lost_rows"]} == {"5", "6"}
          and hv["winner"] == "hc_hak_a", hv)
    stale = cx.get(("baseitems", "stale"))
    check("2da: hak baseitems is stale vs the game (row 3 newpatchitem hidden; row 1 customised)",
          stale and [r["row"] for r in stale["missing_rows"]] == ["3"] and "1" in stale["custom_rows"], stale)
    cust = cx.get(("appearance", "stale")) or cx.get(("appearance", "customised"))
    check("2da: hak appearance adds row 4 vs game (customised, not stale)", cust and cust["kind"] == "customised" and cust["custom_rows"] == ["4"], cust)
    hd = {h["model"]: h for h in cat["heads"]}
    check("heads: guard's pmh0_head001 exists in base game", hd.get("pmh0_head001.mdl", {}).get("status") == "ok", hd)
    check("heads: noble's pfh0_head077 reported MISSING", hd.get("pfh0_head077.mdl", {}).get("status") == "MISSING", hd)
    cats = {i["category"] for i in rep["issues"]}
    check("issues: 2da_stale, 2da_hak_vs_hak and missing_head_model raised", {"2da_stale", "2da_hak_vs_hak", "missing_head_model"} <= cats, cats)
    app = cat["tables"]["appearance"]
    check("the catalogue lists only the winning copy's 2da rows (hak_b's rows 5-6 never load)",
          app["provided"] == 5 and not {r["row"] for r in app["rows"]} & {"5", "6"}, [r["row"] for r in app["rows"]])
    # a hand-merged table whose labels drift from the line positions: the game uses the position
    w_ = lambda fn, txt: open(os.path.join(mod, fn), "w").write(txt)  # noqa: E731
    w_("drift.2da", "2DA V2.0\n\n   Label  Model\n0  a  m_a\n2  b  m_b\n2  c  m_c\n")
    out2 = os.path.join(tmp, "hc_analysis2")
    nwn_index.run_index(mod, haks=[ha, hb], out=out2, verbose=False, nwn_root=base)
    db = sqlite3.connect(os.path.join(out2, "index.sqlite"))
    got = db.execute("SELECT row, label, value FROM twoda WHERE name='drift' AND col='Label' ORDER BY CAST(row AS INT)").fetchall()
    iss = [r[0] for r in db.execute("SELECT detail FROM issues WHERE category='2da_row_label'")]
    db.close()
    check("2da rows are stored by line position, with the label kept separately and a warning where they differ",
          got == [("0", "0", "a"), ("1", "2", "b"), ("2", "2", "c")] and any("line 1 labelled '2'" in d for d in iss), (got, iss))


def lean_haks(tmp, root):
    """A hak with used and unused content: the lean hak keeps what the module reaches (+ engine-named files)."""
    import make_test_module as mtm
    mod = os.path.join(tmp, "lh_mod"); hak = os.path.join(tmp, "lh_hak"); os.makedirs(hak)
    mtm.build(mod)
    wh = lambda fn, data: open(os.path.join(hak, fn), "wb").write(data if isinstance(data, bytes) else data.encode())  # noqa
    wh("c_rat.mdl", b"\0\0\0\0binarymodel hak_rat_tex \0\0")          # hak copy of a used model (mentions its texture)
    wh("hak_rat_tex.tga", mtm.tga(8, 8, lambda x, y: (1, 2, 3)))
    wh("unused_big.tga", b"\0" * 5000)                                  # nothing mentions it
    wh("po_hero_h.tga", b"\0" * 64)                                     # portrait - engine naming, kept
    wh("hak_only.2da", "2DA V2.0\n\n LABEL\n0 x\n")                   # 2da always kept
    wh("hak_inc.nss", "void HakFn() { }\n")                             # unused script in hak - builders may use it: KEEP
    wh("unused_bp.uti", open(os.path.join(mod, "unused_helm.uti"), "rb").read())   # palette content: KEEP
    wh("potion.uti", open(os.path.join(mod, "potion.uti"), "rb").read())           # identical copy of a module file: the hak's copy is the one the game loads, so the lean hak keeps it
    wh("dup_tex.tga", b"\0" * 5000)                                     # nothing anywhere mentions it: dead end
    # big-PW / CEP cases: names the engine builds from 2da rows, never mentioned by anything
    wh("pfh14_robe117.mdl", b"\0\0\0\0binarymodel\0")                   # two-digit CEP phenotype body part
    wh("helm_128.plt", b"\0" * 900)                                      # row-numbered helmet texture
    wh("alt_fa49.mdl", b"\0\0\0\0binarymodel\0")                        # ACP animation supermodel
    wh("pmh0_chest001.mdl", b"# ascii\nnewmodel pmh0_chest001\nsetsupermodel pmh0_chest001 acp_super\n")
    wh("acp_super.mdl", b"# ascii\nnewmodel acp_super\n")               # only reached as a supermodel
    wh("lonely_thing.mdl", b"\0\0\0\0binarymodel\0")                    # no convention, nothing mentions it: review, not dropped
    wh("irongolem.mdl", b"\0\0\0\0binarymodel irongolem\0")            # model whose texture has its own name
    wh("irongolem.tga", b"\0" * 900)
    wh("tcge0_msc-fence.mdl", b"\0\0\0\0binarymodel tcge0_msc-fence\0")
    wh("tcge0_msc-fence.dds", b"\0" * 900)                               # hyphenated name
    wh("iit_cloak_188.tga", b"\0" * 900)                                 # item icon convention
    wh("ghost.wok", b"\0" * 700)                                         # walkmesh without a model: dropped
    os.remove(os.path.join(mod, "c_rat.mdl"))                            # so the hak's copy is the one used
    out = os.path.join(tmp, "lh_analysis")
    nwn_index.run_index(mod, haks=[hak], out=out, verbose=False, nwn_root=root, tlk=os.path.join(mod, "testmod_custom.tlk"))
    rep = nwn_analysis.run_analysis(out, verbose=False)
    plan = {"delete": [d["node"] for d in rep["deletions"] if d["status"] != "Review"],
            "merge": [g["id"] for g in rep["duplicates"] if g["mergeable"]], "lean_haks": True, "lean_haks_mode": "builder"}
    log = nwn_build.build(out, plan, verbose=False, audit_kwargs=NO_COMP)
    lh = log.get("lean_haks", {}).get("lh_hak")
    check("lean hak (builder mode) written with size accounting", lh and lh["dropped"] == 1 and lh["bytes_in"] > 0 and lh["bytes_out"] > 0, (lh, log.get("sizes")))
    newh = (log.get("renamed_haks") or {}).get("lh_hak")
    check("rebuilt hak gets a NEW name (can't overwrite the original when copied)", newh == "lh_hak_r1" and
          not os.path.exists(os.path.join(out, "build", "haks", "lh_hak.hak")), log.get("renamed_haks"))
    ifo = n.read_gff(n.Erf(log["mod_file"]).read(next(e for e in n.Erf(log["mod_file"]).entries if e.filename == "module.ifo")))
    check("the audit accepts the renamed hak (same hak, new name)",
          not [c for c in log["audit"]["checks"] if c["status"] in ("WARN", "FAIL")],
          (log["audit"]["verdict"], [c for c in log["audit"]["checks"] if c["status"] != "PASS"]))
    # module.ifo with a hak list: every rebuilt hak is listed by its new name, others untouched, change logged
    ifo_root = n.GffRoot("IFO ")
    lst = []
    for hk in ("lh_hak", "other_hak"):
        st_ = n.GffStruct(8); st_.set("Mod_Hak", n.CEXOSTRING, hk); lst.append(st_)
    ifo_root.set("Mod_HakList", n.LIST, lst); ifo_root.set("Mod_Hak", n.CEXOSTRING, "LH_HAK")
    class _RW:
        changes = []
    lg = {"rewritten": []}
    tdir = os.path.join(tmp, "ifo_t"); os.makedirs(tdir, exist_ok=True)
    pk = nwn_build._point_module_at_haks([("module", "ifo", n.write_gff(ifo_root))], tdir, {"lh_hak": "lh_hak_r1"}, _RW, lg)
    g2 = n.read_gff(pk[0][2])
    check("the clean module's hak list points at the new hak names (others untouched) and the change is logged",
          [x.get("Mod_Hak") for x in g2.get("Mod_HakList")] == ["lh_hak_r1", "other_hak"] and g2.get("Mod_Hak") == "lh_hak_r1"
          and len(_RW.changes) == 2 and lg["rewritten"] == ["module.ifo"], (g2.get("Mod_HakList"), _RW.changes))
    import nwn_hakslim as hs_
    nm_ = hs_.new_hak_names(["a_sixteen_chars_", "short"], taken={"short_r1"})
    check("new hak names stay within 16 characters and skip names already in the hak folder",
          nm_ == {"a_sixteen_chars_": "a_sixteen_cha_r1", "short": "short_r2"}, nm_)
    try:
        nwn_build.build(out, {"delete": [], "merge": []}, name=os.path.basename(mod), verbose=False); refused = False
    except ValueError:
        refused = True
    check("a clean module can't take the original module's name", refused)
    names = {e.filename for e in n.Erf(os.path.join(out, "build", "haks", f"{newh}.hak")).entries}
    check("builder mode keeps palette content (script, blueprint, portrait, 2da, used model+texture)",
          {"c_rat.mdl", "hak_rat_tex.tga", "hak_only.2da", "po_hero_h.tga", "hak_inc.nss", "unused_bp.uti"} <= names, names)
    check("builder mode keeps unexplained textures (listed for review), drops duplicates/orphan walkmeshes only",
          {"unused_big.tga", "dup_tex.tga"} <= names and "ghost.wok" not in names, names)
    check("builder mode keeps row-numbered parts, two-digit phenotypes, supermodels and unexplained models",
          {"pfh14_robe117.mdl", "helm_128.plt", "alt_fa49.mdl", "acp_super.mdl", "lonely_thing.mdl"} <= names, names)
    import nwn_hakslim as hs
    bp = hs.plan_lean_haks(out, mode="builder")["lh_hak"]
    check("unexplained model is listed for review (kept)", any(r["file"] == "lonely_thing.mdl" for r in bp["review"]), bp["review"])
    linked = {"irongolem.tga", "tcge0_msc-fence.dds"}
    check("a model's own-name texture and a hyphenated name are linked to their model (kept, not 'unexplained')",
          linked <= names and not linked & {r["file"] for r in bp["review"]}, (sorted(linked - names), bp["review"]))
    ap = hs.plan_lean_haks(out, mode="builder", aggressive=True)["lh_hak"]
    adrop = {d["file"] for d in ap["drop"]}
    check("aggressive mode drops the unexplained model/textures but never a supermodel, own-name texture, hyphenated or icon name",
          {"lonely_thing.mdl", "unused_big.tga", "dup_tex.tga"} <= adrop and
          not adrop & {"acp_super.mdl", "irongolem.tga", "tcge0_msc-fence.dds", "iit_cloak_188.tga"}, adrop)
    import nwn_hakslim
    mp = nwn_hakslim.plan_lean_haks(out, mode="minimal")["lh_hak"]
    check("minimal mode also drops the unused script/blueprint", {"hak_inc.nss", "unused_bp.uti"} <= {d["file"] for d in mp["drop"]}, mp["drop"])
    bad = [c for c in log["audit"]["checks"] if c["status"] not in ("PASS", "SKIPPED") and c["id"] != "edits_compiled"]
    check("audit passes against the lean haks (incl. 2da identity)", not bad and any(c["id"] == "lean_haks" for c in log["audit"]["checks"]), bad)


def deletion_rules(tmp, root):
    """Things the game can run or create without any reference a file shows: they must be Review (or kept), never
    Safe - engine-named part models, tag-based scripts with a prefix set either standard way, scripts named only in
    the server's settings, blueprints recreated from saved resrefs, and blueprints DMs can spawn from the palette."""
    from _gff import st, root as gff, R, X, LST, loc, w, stand_in_ncs, item_fields
    mod = os.path.join(tmp, "dr_mod"); os.makedirs(mod)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731

    def script(name, src):
        w(mod, f"{name}.nss", src); w(mod, f"{name}.ncs", stand_in_ncs(name))
    helm = item_fields("myhelm", "MYHELM", "Horned Helm", base=17); helm["ModelPart1"] = (n.BYTE, 15)
    g("module.ifo", gff("IFO ", Mod_Name=loc("DR"), Mod_Entry_Area=(R, "area001"), Mod_OnModLoad=(R, "mod_load"),
                        Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))])))
    g("area001.are", gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    g("area001.git", gff("GIT ", **{"List": (LST, [st(0, **helm), st(0, **item_fields("wand", "dmw", "DM wand"))]),
                                    "Creature List": (LST, [st(4, TemplateResRef=(R, "ogre"), Tag=(X, "OGRE"),
                                                                FirstName=loc("Ogre"), Phenotype=(n.INT, 14))])}))
    g("myhelm.uti", gff("UTI ", **helm))
    g("wand.uti", gff("UTI ", **item_fields("wand", "dmw", "DM wand")))
    g("wand2.uti", gff("UTI ", **item_fields("wand2", "dmw", "DM wand")))       # identical copy of a placed item
    g("ogre.utc", gff("UTC ", TemplateResRef=(R, "ogre"), Tag=(X, "OGRE"), FirstName=loc("Ogre"), Phenotype=(n.INT, 14)))
    for nm in ("helm_015", "pmh14_chest001"):
        w(mod, nm + ".mdl", f"newmodel {nm}\nsetsupermodel {nm} NULL\ndonemodel {nm}\n")
    script("mod_load", '#include "x2_inc_switches"\n#include "nwnx_sql"\nvoid main(){\n SetUserDefinedItemEventPrefix("item_");\n'
                       ' object o = GetFirstPC(); string s = NWNX_SQL_ReadDataInActiveRow(0); CreateItemOnObject(s, o);\n}\n')
    script("nwnx_sql", 'string NWNX_SQL_ReadDataInActiveRow(int n){ return ""; }\n')
    script("item_dmw", 'void main(){ SpeakString("dm wand"); }\n')
    script("pw_preload", 'void main(){ WriteTimestampedLogEntry("preload"); }\n')
    script("never_used", 'void main(){ }\n')
    g("pw_heirloom.uti", gff("UTI ", **item_fields("pw_heirloom", "PW_HEIRLOOM", "Ring")))
    g("old_chest.utp", gff("UTP ", TemplateResRef=(R, "old_chest"), Tag=(X, "OLD_CHEST")))
    g("dm_chest.utp", gff("UTP ", TemplateResRef=(R, "dm_chest"), Tag=(X, "DM_CHEST")))
    g("placeablepalcus.itp", gff("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), LIST=(LST, [
        st(0, RESREF=(R, "dm_chest"), NAME=(X, "DM chest"))]))])))
    out = os.path.join(tmp, "dr_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    st_ = status_map(rep)
    why = {d["node"]: d["reason"] for d in rep["deletions"]}
    check("helmet and two-digit phenotype models are Review, never Safe",
          st_.get("model:helm_015") == "Review" and st_.get("model:pmh14_chest001") == "Review",
          (why.get("model:helm_015"), why.get("model:pmh14_chest001")))
    check("SetUserDefinedItemEventPrefix(\"item_\") makes item_<tag> a tag-based script (kept)", "script:item_dmw" not in st_,
          why.get("script:item_dmw"))
    check("an item blueprint is Review when a script creates items from a name read at run time",
          "run time" in why.get("bp:pw_heirloom.uti", "") and "mod_load line" in why.get("bp:pw_heirloom.uti", ""),
          why.get("bp:pw_heirloom.uti"))
    grp = next((d for d in rep["duplicates"] if "bp:wand2.uti" in {m["node"] for m in d["members"]}), None)
    check("an item merge is blocked when a script creates items from a name read at run time",
          grp and not grp["mergeable"] and any("run time" in b for b in grp["blocked"]), grp and grp["blocked"])
    check("a blueprint the toolset palette lists is Review: DMs can spawn it",
          "only in the toolset palette (DMs can spawn it)" in why.get("bp:dm_chest.utp", ""), why.get("bp:dm_chest.utp"))
    check("a blueprint missing from its type's palette, placed and created nowhere, is Safe", st_.get("bp:old_chest.utp") == "Safe",
          (st_.get("bp:old_chest.utp"), why.get("bp:old_chest.utp")))
    check("a module that includes nwnx_* with no server settings loaded: unused scripts are Review",
          st_.get("script:pw_preload") == "Review" and "server settings" in why.get("script:pw_preload", ""),
          why.get("script:pw_preload"))
    # the server's environment names pw_preload: once loaded it is a root (kept), and the NWNX reason goes away
    with open(os.path.join(out, "server_config.json"), "w", encoding="utf-8") as fh:
        json.dump([dict(kind="environment", name="env", values={"NWNX_UTIL_PRE_MODULE_START_SCRIPT": "pw_preload"})], fh)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    st_ = status_map(rep)
    check("a script named in the loaded server settings is kept (a root)", "script:pw_preload" not in st_, st_.get("script:pw_preload"))
    check("with server settings loaded, an unused script is no longer Review for NWNX", st_.get("script:never_used") == "Safe",
          [d["reason"] for d in rep["deletions"] if d["node"] == "script:never_used"])
    # a prefix set from a variable can't be known: scripts ending in an item tag become Review
    script("mod_load", 'void main(){ string p = GetLocalString(GetModule(), "pfx"); SetUserDefinedItemEventPrefix(p); }\n')
    out2 = os.path.join(tmp, "dr_analysis2")
    nwn_index.run_index(mod, out=out2, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out2, verbose=False)
    why = {d["node"]: d["reason"] for d in rep["deletions"]}
    check("an unreadable item-event prefix makes <something><item tag> scripts Review",
          "tag-based item script" in why.get("script:item_dmw", ""), why.get("script:item_dmw"))


def lean_hak_rules(tmp, root_unused):
    """What a lean hak may drop: never a file only because the builder's own override folder has a copy (players
    don't have it), never an engine-named part model in minimal mode, never a walkmesh for a base-game model, never a
    copy that only a hak the module no longer loads duplicates; and the hak's description survives."""
    from _gff import st, root as gff, R, X, LST, loc, w, tga, item_fields
    base = fake_nwn_root(os.path.join(tmp, "lr_root"), BASE + ["plc_basegame.mdl"])
    mod = os.path.join(tmp, "lr_mod"); user = os.path.join(tmp, "lr_user")
    hakd, ovr = os.path.join(user, "hak"), os.path.join(user, "override")
    for d in (mod, hakd, ovr):
        os.makedirs(d)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    helm = item_fields("myhelm", "MYHELM", "Horned Helm", base=17); helm["ModelPart1"] = (n.BYTE, 15)
    g("module.ifo", gff("IFO ", Mod_Name=loc("LR"), Mod_Entry_Area=(R, "area001"),
                        Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]),
                        Mod_HakList=(LST, [st(8, Mod_Hak=(X, "lr_a")), st(8, Mod_Hak=(X, "lr_b"))])))
    g("area001.are", gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    g("area001.git", gff("GIT ", **{"List": (LST, [st(0, **helm)]),
                                    "Placeable List": (LST, [st(9, TemplateResRef=(R, "statue"), Tag=(X, "STATUE"))])}))
    g("myhelm.uti", gff("UTI ", **helm))
    g("statue.utp", gff("UTP ", TemplateResRef=(R, "statue"), Tag=(X, "STATUE")))
    p2da = b"2DA V2.0\n\n   Label ModelName\n0 a plc_a01\n1 Statue zep_statue\n"
    tex = tga(4, 4, lambda x, y: (200, 10, 10))
    mdl = lambda nm: (f"newmodel {nm}\nsetsupermodel {nm} NULL\nnode trimesh b\n  bitmap {nm}\nendnode\n").encode()  # noqa
    n.write_erf(os.path.join(hakd, "lr_a.hak"), [("placeables", "2da", p2da), ("zep_stat_tx", "tga", tex),
                                                  ("helm_015", "mdl", mdl("helm_015")), ("helm_015", "plt", b"PLT V1  " + b"\0" * 16),
                                                  ("pmh14_chest001", "mdl", mdl("pmh14_chest001")),
                                                  ("plc_basegame", "pwk", b"\0" * 40), ("shared", "tga", tex),
                                                  # a walkmesh with no model anywhere: dropped, so lr_a is rebuilt
                                                  ("lr_ghost", "wok", b"\0" * 40)],
                "HAK ", description={0: "LR test hak"})
    n.write_erf(os.path.join(hakd, "lr_b.hak"), [("shared", "tga", tex), ("other", "tga", tga(2, 2, lambda x, y: (1, 1, 1)))], "HAK ")
    w(ovr, "zep_stat_tx.tga", tex)            # the builder's override has an identical copy of a hak file
    w(ovr, "placeables.2da", p2da.replace(b"Statue", b"Edited"))     # and an old edit of the hak's 2da
    out = os.path.join(tmp, "lr_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=base, nwn_user=user)
    nwn_analysis.run_analysis(out, verbose=False)
    import nwn_hakslim
    bd = {d["file"] for d in nwn_hakslim.plan_lean_haks(out, mode="builder")["lr_a"]["drop"]}
    check("lean haks never drop a file because the builder's override has a copy",
          not bd & {"placeables.2da", "zep_stat_tx.tga"}, bd)
    check("a walkmesh for a base-game model is kept", "plc_basegame.pwk" not in bd, bd)
    bplan = nwn_hakslim.plan_lean_haks(out, mode="builder")
    check("an identical copy in a later-listed hak is dropped (the first-listed hak wins)",
          "shared.tga" in {d["file"] for d in bplan["lr_b"]["drop"]} and "shared.tga" not in bd, bplan["lr_b"]["drop"])
    sk = nwn_hakslim.plan_lean_haks(out, mode="builder", skip=["lr_a"])
    check("a hak the module no longer loads never makes another hak's copy a duplicate",
          "lr_a" not in sk and not sk["lr_b"]["drop"], sk)
    md = {d["file"] for d in nwn_hakslim.plan_lean_haks(out, mode="minimal")["lr_a"]["drop"]}
    check("minimal mode keeps helmet and two-digit phenotype part models", not md & {"helm_015.mdl", "helm_015.plt",
                                                                                   "pmh14_chest001.mdl"}, md)
    log = nwn_build.build(out, {"delete": [], "merge": [], "lean_haks": True, "git": False}, verbose=False,
                          audit_kwargs=NO_COMP)
    lean = os.path.join(out, "build", "haks", "lr_a_r1.hak")
    names = {e.filename for e in n.Erf(lean).entries}
    check("the lean hak still carries the 2da and texture players need", {"placeables.2da", "zep_stat_tx.tga"} <= names, names)
    check("a lean hak keeps the original hak's description",
          n.erf_description_block(lean) == n.erf_description_block(os.path.join(hakd, "lr_a.hak")), n.erf_description_block(lean))
    cdb = os.path.join(out, "build", os.path.basename(log["output"]) + "_analysis", "index.sqlite")
    kinds = {r[0] for r in sqlite3.connect(cdb).execute("SELECT kind FROM sources")} if os.path.isfile(cdb) else None
    check("the audit re-indexes the clean module without the builder's override folder",
          kinds is not None and "override" not in kinds and log["audit"]["verdict"] != "FAIL", (cdb, kinds))
    lh = next((c for c in log["audit"]["checks"] if c["id"] == "lean_haks"), {})
    check("the audit's 2da comparison uses the copy players load, not the builder's override edit",
          lh.get("status") == "PASS", lh)


def lean_haks_untouched(tmp, root_unused):
    """Only haks the lean plan changes are rebuilt and renamed: a hak with nothing to drop is left alone (no copy in
    build/haks, its own name in the clean module.ifo, the audit reads it from the user's hak folder), and a rebuild
    never treats that original as its own output."""
    from _gff import tga
    base = fake_nwn_root(os.path.join(tmp, "ut_root"), BASE)
    mod = os.path.join(tmp, "ut mod"); user = os.path.join(tmp, "ut user"); hakd = os.path.join(user, "hak")
    for d in (mod, hakd):
        os.makedirs(d)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("UT"), Mod_Entry_Area=(R, "area001"),
                                          Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]),
                                          Mod_HakList=(LST, [st(8, Mod_Hak=(X, "ut_a")), st(8, Mod_Hak=(X, "ut_b"))]))))
    w(mod, "area001.are", n.write_gff(root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                           Tileset=(R, "tcn01"))))
    w(mod, "area001.git", n.write_gff(root("GIT ", **{"Placeable List": (LST, [st(9, TemplateResRef=(R, "statue"),
                                                                                  Tag=(X, "STATUE"))])})))
    w(mod, "statue.utp", n.write_gff(root("UTP ", TemplateResRef=(R, "statue"), Tag=(X, "STATUE"))))
    p2da = b"2DA V2.0\n\n   Label ModelName\n0 a plc_a01\n"
    # ut_a: a walkmesh with no model anywhere (a dead end the lean plan drops); ut_b: nothing to drop
    n.write_erf(os.path.join(hakd, "ut_a.hak"), [("ut_tex", "tga", tga(2, 2, lambda x, y: (1, 2, 3))),
                                                 ("ut_ghost", "wok", b"\0" * 40)], "HAK ")
    n.write_erf(os.path.join(hakd, "ut_b.hak"), [("placeables", "2da", p2da),
                                                 ("ut_btex", "tga", tga(2, 2, lambda x, y: (4, 5, 6)))], "HAK ")
    b_hak = os.path.join(hakd, "ut_b.hak")
    b_sha = hashlib.sha256(open(b_hak, "rb").read()).hexdigest()
    out = os.path.join(tmp, "ut_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=base, nwn_user=user)
    nwn_analysis.run_analysis(out, verbose=False)
    log = nwn_build.build(out, {"delete": [], "merge": [], "lean_haks": True, "git": False}, verbose=False,
                          audit_kwargs=dict(NO_COMP, nwn_root=base, nwn_user=user))
    hd = os.path.join(out, "build", "haks")
    check("lean haks: only the hak that loses a file is written and renamed",
          log.get("renamed_haks") == {"ut_a": "ut_a_r1"} and log.get("unchanged_haks") == ["ut_b"] and
          sorted(f for f in os.listdir(hd) if f.endswith(".hak")) == ["ut_a_r1.hak"], (log.get("renamed_haks"),
                                                                                       os.listdir(hd)))
    lh = log.get("lean_haks", {})
    check("lean haks: the untouched hak is recorded as unchanged, with its original path and size",
          lh.get("ut_b", {}).get("unchanged") is True and lh["ut_b"]["file"] == b_hak and
          lh["ut_b"]["bytes_out"] == lh["ut_b"]["bytes_in"] and not lh.get("ut_a", {}).get("unchanged") and
          log.get("lean_haks_summary", {}).get("rebuilt") == 1 and log["lean_haks_summary"]["unchanged"] == 1, lh)
    e_ = n.Erf(log["mod_file"])
    ifo = n.read_gff(e_.read(next(x for x in e_.entries if x.filename == "module.ifo")))
    e_.close()
    check("the clean module.ifo lists the rebuilt hak by its new name and the untouched one by its own",
          [x.get("Mod_Hak") for x in ifo.get("Mod_HakList")] == ["ut_a_r1", "ut_b"],
          [x.get("Mod_Hak") for x in ifo.get("Mod_HakList")])
    md = open(os.path.join(out, "build", "changes.md"), encoding="utf-8").read()
    check("changes.md says how many haks were rebuilt and how many were left as they are",
          "1 rebuilt" in md and "1 left as they are" in md and "ut_a.hak → **ut_a_r1.hak**" in md and
          "## Haks left as they are" in md and "- ut_b.hak" in md and "ut_b.hak → " not in md, md[-1500:])
    checks = {c["id"]: c for c in log["audit"]["checks"]}
    cdb = os.path.join(out, "build", os.path.basename(log["output"]) + "_analysis", "index.sqlite")
    srcs = {os.path.normcase(os.path.abspath(r[0])) for r in sqlite3.connect(cdb).execute(
        "SELECT path FROM sources WHERE kind='hak'")} if os.path.isfile(cdb) else set()
    check("the audit passes and its clean index read the untouched hak from the hak folder, the rebuilt one from build/haks",
          log["audit"]["verdict"] != "FAIL" and checks.get("lean_haks", {}).get("status") == "PASS" and
          checks.get("in_use", {}).get("status") == "PASS" and checks.get("references", {}).get("status") == "PASS" and
          srcs == {os.path.normcase(os.path.abspath(b_hak)), os.path.normcase(os.path.abspath(os.path.join(hd, "ut_a_r1.hak")))},
          (log["audit"]["verdict"], srcs, [c for c in checks.values() if c["status"] not in ("PASS", "SKIPPED")]))
    # a second build removes only its own earlier output: the untouched original is never "ours", wherever it sits
    log2 = nwn_build.build(out, {"delete": [], "merge": [], "lean_haks": True, "git": False}, verbose=False,
                           run_audit=False)
    check("a rebuild leaves the untouched original hak exactly as it was",
          os.path.isfile(b_hak) and hashlib.sha256(open(b_hak, "rb").read()).hexdigest() == b_sha and
          log2.get("renamed_haks") == {"ut_a": "ut_a_r1"}, log2.get("renamed_haks"))
    import nwn_hakslim
    shutil.copy(b_hak, os.path.join(hd, "ut_b.hak"))       # someone puts a hak of that name into build/haks
    try:
        nwn_hakslim.existing_lean_haks(hd); refused = False
    except ValueError:
        refused = True
    check("an 'unchanged' manifest entry never makes a hak of that name in build/haks look like the build's own", refused)


def impact_engine(tmp, root):
    """The report's impact levels come from Graph.impact_all (shared closures); the dashboard and nwn_facts work an
    item's full chain out with Graph.impact. Both must agree with each other and with the report, for every item."""
    mod, hak = gen("make_dave_module.py", tmp)
    out = os.path.join(h.WORKSPACE, "impact_dave")           # the dashboard reads it from its workspace
    nwn_index.run_index(mod, haks=[hak], out=out, verbose=False, nwn_root=root)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    check("impact: the analysis stores no per-item impact detail in index.sqlite (worked out on demand)",
          not db.execute("SELECT 1 FROM sqlite_master WHERE name='impact_detail'").fetchone())
    g = nwn_analysis.Graph(db)
    live = g.live()
    targets = [nid for nid, nd in g.nodes.items() if nd["in_module"] and nd["type"] in nwn_analysis.IMPACT_TYPES]
    walked = {t: g.impact(t, detail=False, live=live) for t in targets}
    small = nwn_analysis._Reach.SMALL
    try:
        nwn_analysis._Reach.SMALL = 0       # every closure a bit set, so this small module takes the fast path too
        fast = g.impact_all(targets, live)
    finally:
        nwn_analysis._Reach.SMALL = small
    bad = [t for t in targets if fast[t] != walked[t]]
    check(f"impact: the whole-module pass gives exactly what the walk gives, for all {len(targets)} items",
          targets and not bad, [(t, fast[t], walked[t]) for t in bad[:2]])
    hit = [t for t in targets if any(r.startswith("Runs from a module-wide event") for r in walked[t]["reasons"])]
    check("impact: the module-event reason is covered (several events reach the same scripts)", len(hit) > 3, hit)
    import nwn_dashboard
    shown = {t: nwn_dashboard.impact_detail("impact_dave", t) for t in rep["impact"]}
    differ = [t for t, d in shown.items() if (d["level"], d["reasons"]) != (rep["impact"][t]["level"], rep["impact"][t]["reasons"])
              or {k: v for k, v in d["counts"].items() if v} != rep["impact"][t]["counts"]]
    check("impact: the dashboard's detail of every item has the report's level, reasons and counts", not differ,
          [(t, shown[t]["level"], rep["impact"][t]["level"]) for t in differ[:3]])
    check("impact: an area outside the module's area list is None in the dashboard detail too",
          shown["area:abandoned_test"]["level"] == "None" and "affected" in shown["area:abandoned_test"],
          shown["area:abandoned_test"]["level"])
    nwn_dashboard.release_analysis("impact_dave")


def module_file_details(tmp, root_unused):
    """What a rebuild keeps from the original .mod (its description, the first of two same-named entries), what the
    build says when git is missing, sounds a custom talk table plays, and how the game's own files are layered."""
    import struct
    from _gff import st, root as gff, R, X, LST, loc, stand_in_ncs
    d = os.path.join(tmp, "mfd"); os.makedirs(d)
    ifo = n.write_gff(gff("IFO ", Mod_Name=loc("MFD"), Mod_Entry_Area=(R, "area001"), Mod_OnModLoad=(R, "mod_load"),
                          Mod_CustomTlk=(X, "mfd_tlk"), Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))])))
    files = [("module", "ifo", ifo),
             ("area001", "are", n.write_gff(gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                                Tileset=(R, "tcn01")))),
             ("area001", "git", n.write_gff(gff("GIT "))),
             ("mod_load", "nss", b'void main(){ SpeakString("first"); }'), ("mod_load", "ncs", stand_in_ncs("a")),
             ("MOD_LOAD", "nss", b'void main(){ SpeakString("second"); }'),
             ("vo_greet", "wav", b"RIFF" + b"\0" * 40), ("vo_unused", "wav", b"RIFF" + b"\1" * 40)]
    mod = os.path.join(d, "mfd.mod")
    n.write_erf(mod, files, "MOD ", description={0: "A module description players see"})
    # custom talk table: entry 0 has text and plays vo_greet (flags bit 1 = has a sound)
    user = os.path.join(d, "user"); os.makedirs(os.path.join(user, "tlk"))
    tlk = os.path.join(user, "tlk", "mfd_tlk.tlk")
    n.write_tlk(tlk, {0: "Hello there"})
    raw = bytearray(open(tlk, "rb").read())
    struct.pack_into("<I", raw, 20, 1 | 2)
    raw[24:40] = b"vo_greet".ljust(16, b"\0")
    open(tlk, "wb").write(bytes(raw))
    out = os.path.join(d, "an")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_user=user)
    rep = nwn_analysis.run_analysis(out, verbose=False)
    st_ = status_map(rep)
    check("a sound a custom talk-table entry plays is in use (not offered for deletion)",
          "asset:vo_greet.wav" not in st_ and "asset:vo_unused.wav" in st_, sorted(st_))
    # a PC without git: only nwn_build's own view of shutil.which changes
    no_git = h.Proxy(shutil, which=lambda x: None if x == "git" else shutil.which(x))
    with mock.patch.object(nwn_build, "shutil", no_git):
        log = nwn_build.build(out, {"delete": [], "merge": []}, out_dir=os.path.join(d, "build"), verbose=False,
                              run_audit=False)
    clean = log["output"]
    check("a .mod holding one name twice: the clean module keeps the first entry and says so",
          "first" in open(os.path.join(clean, "mod_load.nss")).read() and log["mod_file"] and
          any("more than once" in w_ for w_ in log["warnings"]), log["warnings"])
    check("the rebuilt .mod keeps the module description from the original",
          n.erf_description_block(log["mod_file"]) == n.erf_description_block(mod), n.erf_description_block(log["mod_file"]))
    md = open(os.path.join(d, "build", "changes.md"), encoding="utf-8").read()
    check("without git the change log says there is no version history", "Git: not installed" in md, md[:400])
    check("the build summary reads as a sentence", nwn_build.summary_sentence(log["summary"]).startswith(
        f"{log['summary']['files_in']} files in, "), nwn_build.summary_sentence(log["summary"]))
    # the game's own files: the EE key files in order (later wins), language folders, ovr/ over the keys
    groot = os.path.join(d, "game")
    n.write_key_bif(groot, {("same", "2da"): b"base copy", ("onlybase", "2da"): b"b"})
    other = os.path.join(d, "game2")
    n.write_key_bif(other, {("same", "2da"): b"retail copy"})
    key = open(os.path.join(other, "data", "nwn_base.key"), "rb").read().replace(b"data/base.bif", b"data/rtal.bif")
    open(os.path.join(groot, "data", "nwn_retail.key"), "wb").write(key)
    shutil.copy(os.path.join(other, "data", "base.bif"), os.path.join(groot, "data", "rtal.bif"))
    for sub, txt in (("ovr", b"ovr copy"), (os.path.join("data", "ovr"), b"data/ovr copy")):
        os.makedirs(os.path.join(groot, sub), exist_ok=True)
        open(os.path.join(groot, sub, "ovr_only.2da"), "wb").write(txt)
    os.makedirs(os.path.join(groot, "lang", "en", "data", "ovr"))
    open(os.path.join(groot, "lang", "en", "data", "ovr", "lang_only.txt"), "wb").write(b"en")
    bg = n.BaseGame(groot)
    check("base game: a later key file wins (nwn_retail over nwn_base), ovr/ and lang/<language>/data/ovr are read, "
          "data/ovr is not", bg.get("same.2da") == b"retail copy" and bg.get("onlybase.2da") == b"b" and
          bg.get("ovr_only.2da") == b"ovr copy" and bg.get("lang_only.txt") == b"en", sorted(bg.names()))


# ================================================================ index / analysis edge cases
# (a module with two haks - one a folder, one a .hak - listed in module.ifo in that order)
PORTRAITS_A = "2DA V2.0\n\n   BaseResRef\n0  hu_m_01_\n1  hu_m_02_\n"
PORTRAITS_B = "2DA V2.0\n\n   BaseResRef\n0  hu_m_01_\n1  zz_other\n"


def utc(resref, tag, portrait="", **extra):
    f = dict(TemplateResRef=(R, resref), Tag=(X, tag), FirstName=loc(tag.title()), Portrait=(R, portrait))
    f.update(extra)
    return root("UTC ", **f)


def build_load_order_module(tmp):
    """A module folder plus two haks (A a folder, B a .hak archive), both listed in module.ifo in that order."""
    mod = os.path.join(tmp, "mod"); hak_a = os.path.join(tmp, "hak_a"); hak_b = os.path.join(tmp, "hak_b.hak")
    os.makedirs(mod); os.makedirs(hak_a)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Load Order"), Mod_Entry_Area=(R, "area001"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]),
                         Mod_HakList=(LST, [st(8, Mod_Hak=(X, "hak_a")), st(8, Mod_Hak=(X, "hak_b"))])))
    g("module.jrl", root("JRL ", Categories=(LST, [st(0, Tag=(X, "q_one"), Name=loc("Quest One"),
                                                      EntryList=(LST, [st(0, ID=(n.DWORD, 1), Text=loc("Go"), End=(n.WORD, 1))]))])))
    g("area001.are", root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    g("area001.git", root("GIT ", **{"Creature List": (LST, [
        st(4, TemplateResRef=(R, "guard"), Tag=(X, "GUARD"), FirstName=loc("Guard"), Portrait=(R, "po_hu_m_01_"))])}))
    g("guard.utc", utc("guard", "GUARD", "po_hu_m_01_"))
    # 52 creatures whose portrait lacks the po_ prefix: more than the 50 the lint lists
    for i in range(52):
        g(f"badport{i:02d}.utc", utc(f"badport{i:02d}", f"BADPORT{i:02d}", "noprefix"))
    # the same blueprint in the module and both haks (the first-listed hak's copy is the one the game uses)
    g("dup.utc", utc("dup", "FROM_MODULE"))
    w(hak_a, "dup.utc", n.write_gff(utc("dup", "FROM_HAK_A")))
    w(hak_a, "portraits.2da", PORTRAITS_A)
    n.write_erf(hak_b, [("dup", "utc", n.write_gff(utc("dup", "FROM_HAK_B"))),
                        ("portraits", "2da", PORTRAITS_B.encode())], file_type="HAK ")
    # an unused script whose tag no object carries (orphan hint "matches nothing")
    w(mod, "old_ghost.nss", 'void main()\n{\n    object o = GetObjectByTag("GHOST_TAG");\n}\n')
    # a conversation started through a variable, with the call split over two lines
    w(mod, "start_split.nss", 'void main()\n{\n    string sConv = "cf_split";\n    ActionStartConversation(GetFirstPC(),\n'
                              '        sConv);\n}\n')
    return mod, hak_a, hak_b


def index_and_analysis_load_order(tmp):
    """The copy the game uses wins everywhere (json/, catalogue 2da rows); lint caps, orphan hints, split calls."""
    mod, hak_a, hak_b = build_load_order_module(tmp)
    out = os.path.join(tmp, "out")
    nwn_index.run_index(mod, [hak_a, hak_b], [], None, out, write_json=True, verbose=False)
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    # json/ keeps the copy the game uses (first-listed hak > module > override), not the copy read last
    j = json.load(open(os.path.join(out, "json", "dup.utc.json"), encoding="utf-8"))
    tag = j.get("Tag", {}).get("value") if isinstance(j.get("Tag"), dict) else j.get("Tag")
    check("index: json/ holds the winning copy of a file found in several sources", tag == "FROM_HAK_A", j.get("Tag"))
    # the journal file's node has the type its file gives it; nothing asks for a "journal" type
    t = db.execute("SELECT type FROM nodes WHERE node='file:module.jrl'").fetchone()
    check("index: journal node keeps the file node's type", t and t[0] == "file", t)
    db.close()
    rep = nwn_analysis.run_analysis(out, verbose=False)
    # portraits lint: the cap of 50 says how many more there are
    pp = [i for i in rep["issues"] if i["category"] == "portrait_prefix"]
    more = [i for i in pp if "more" in i["detail"] and "52" in i["detail"]]
    check("lints: portrait_prefix cap says how many more creatures are affected", len(pp) == 51 and more, (len(pp), [i["detail"] for i in pp[-2:]]))
    # orphan hint: a tag nothing carries
    orph = [o for o in rep["orphans"] if any(m["node"] == "script:old_ghost" for m in o["members"])]
    check("analysis: orphan hint names a tag that matches nothing",
          orph and any("GHOST_TAG" in h and "matches nothing" in h for h in orph[0]["hints"]), orph)
    # conversation start split over two lines is still followed to its literal
    cs = [c for c in rep["conversations"] if c["name"] == "cf_split"] if "conversations" in rep else []
    miss = [i for i in rep["issues"] if i["category"] == "missing_conversation" and "cf_split" in i["detail"]]
    check("analysis: a conversation-starting call split over lines is still read", miss and "start_split" in miss[0]["detail"], (cs, miss))
    # catalogue: 2da winner by the game's order, Portrait usage, the index left as it was, closed archives
    idx = os.path.join(out, "index.sqlite")
    idx_sha = hashlib.sha256(open(idx, "rb").read()).hexdigest()
    before = h.open_handles(hak_b)
    cat = nwn_catalog.build_catalog(out, verbose=False)
    after = h.open_handles(hak_b)
    name = "catalog: hak archives opened for 2da comparison are closed"
    if before is None:
        skip(name, "open file handles can only be counted on Linux (/proc/self/fd)")
    else:
        check(name, after == before, (before, after))
    por = cat["tables"].get("portraits", {})
    rows = {r["row"]: r for r in por.get("rows", [])}
    check("catalog: 2da rows come from the winning copy (first-listed hak), not the copy read last",
          rows.get("1", {}).get("label") == "hu_m_02_" and rows.get("0", {}).get("source") == "hak", rows)
    check("catalog: a portrait row is used by the creatures whose Portrait names it",
          rows.get("0", {}).get("used") and "guard.utc" in rows["0"]["used_by"] and not rows.get("1", {}).get("used"), rows)
    check("catalog: building the catalogue leaves index.sqlite byte for byte as it was (read only)",
          hashlib.sha256(open(idx, "rb").read()).hexdigest() == idx_sha)
    return out


def find_haks_matching(tmp):
    d = os.path.join(tmp, "explicit"); os.makedirs(d)
    v2 = os.path.join(d, "hak_a_v2.hak"); w(d, "hak_a_v2.hak", b"x")
    core = os.path.join(d, "mycore_extras.hak"); w(d, "mycore_extras.hak", b"x")
    found, missing = nwn_index.find_haks(["hak_a"], [v2], [])
    check("find_haks: a versioned name still matches loosely (hak_a -> hak_a_v2)", found == [v2] and not missing, (found, missing))
    found, missing = nwn_index.find_haks(["hak_a", "hak_a_v2"], [v2], [])
    check("find_haks: a hak that module.ifo names exactly is not also paired loosely with another entry",
          found == [v2] and missing == ["hak_a"], (found, missing))
    found, missing = nwn_index.find_haks(["core"], [core], [])
    check("find_haks: a name inside another word is not a match (core vs mycore_extras)", not found and missing == ["core"], (found, missing))


def resume_after_hard_kill(tmp):
    """Rows committed by a step whose checkpoint file was never written must not be indexed twice on resume."""
    mod, hak_a, hak_b = os.path.join(tmp, "mod"), os.path.join(tmp, "hak_a"), os.path.join(tmp, "hak_b.hak")   # from build_load_order_module
    single = os.path.join(tmp, "single"); chunked = os.path.join(tmp, "chunked")
    nwn_index.run_index(mod, [hak_a, hak_b], [], None, single, write_json=False, verbose=False)
    real_save = nwn_index.Indexer.save_state
    calls = [0]

    def killed_save(self, state_path, progress):
        calls[0] += 1
        if calls[0] == 2:
            # the rows are committed (add_source's finally did that) but the process dies before index.state is
            # replaced: the previous checkpoint is what the next run resumes from
            self.flush_fields(); self.db.commit()
            return
        real_save(self, state_path, progress)
    base = time.time(); ticks = [0]

    def fake():
        ticks[0] += 1
        return base + ticks[0] * 0.6
    # a clock that moves 0.6 s per reading, seen by nwn_index only: the 2 s budget runs out every few files
    runs, finished = 0, False
    with mock.patch.object(nwn_index.Indexer, "save_state", killed_save), \
            mock.patch.object(nwn_index, "time", h.Proxy(time, time=fake)):
        while runs < 500 and not finished:
            runs += 1
            finished = bool(nwn_index.run_index(mod, [hak_a, hak_b], [], None, chunked, write_json=False, verbose=False,
                                                time_budget=2))
    check("checkpoint: the chunked run finishes", finished, runs)
    check("checkpoint: the hard-kill simulation did skip a state write", calls[0] >= 3, calls[0])
    for q in ("SELECT relpath, resref, ext, sha256 FROM files", "SELECT path, kind, priority FROM sources",
              "SELECT path, value FROM fields", "SELECT node, tag FROM objects", "SELECT owner, item, slot FROM equips",
              "SELECT tag, entry_id FROM quest_entries"):
        a = sorted(map(tuple, sqlite3.connect(os.path.join(single, "index.sqlite")).execute(q).fetchall()))
        b = sorted(map(tuple, sqlite3.connect(os.path.join(chunked, "index.sqlite")).execute(q).fetchall()))
        check(f"checkpoint: no duplicate rows after a hard kill - {q.split(' FROM ')[1]}", a == b, (len(a), len(b), runs))


def mod_handle_closed(tmp):
    """run_index peeks at module.ifo inside a .mod: that archive must be closed again."""
    mod = os.path.join(tmp, "mod")
    files = []
    for fn in os.listdir(mod):
        r, _, e = fn.rpartition(".")
        with open(os.path.join(mod, fn), "rb") as fh:
            files.append((r, e, fh.read()))
    modfile = os.path.join(tmp, "packed.mod")
    n.write_erf(modfile, files)
    before = h.open_handles(modfile)
    nwn_index.run_index(modfile, [], [], None, os.path.join(tmp, "out_mod"), write_json=False, verbose=False)
    after = h.open_handles(modfile)
    name = "index: the .mod opened to peek at module.ifo is closed"
    if before is None:
        skip(name, "open file handles can only be counted on Linux (/proc/self/fd)")
    else:
        check(name, after == before, (before, after))


def missing_index_and_resume_step(tmp):
    # run_analysis on a folder with no index: a clear error, and no empty index.sqlite created
    empty = os.path.join(tmp, "empty"); os.makedirs(empty)
    try:
        nwn_analysis.run_analysis(empty, verbose=False)
        err = None
    except FileNotFoundError as ex:
        err = str(ex)
    check("analysis: a missing index is a clear error and no empty index.sqlite is created",
          err and "index.sqlite" in err and not os.path.exists(os.path.join(empty, "index.sqlite")), err)
    # a resumed run may repeat the step after the module on a database that already holds its rows
    ix = nwn_index.Indexer(os.path.join(tmp, "plan_twice"), None, False, False)
    info = dict(base=None, custom=None)
    try:
        p1 = nwn_index._plan_after_module(ix, [], [], None, None, info)
        p2 = nwn_index._plan_after_module(ix, [], [], None, None, info)
        rows = ix.db.execute("SELECT COUNT(*) FROM meta WHERE key='haks'").fetchone()[0]
        check("index: the step after the module can run twice on one database (resume after a kill)", p1 == p2 and rows == 1, (p1, p2, rows))
    except Exception as ex:  # noqa
        check("index: the step after the module can run twice on one database (resume after a kill)", False, repr(ex))
    ix.db.close()


def analyse_reads_override(tmp):
    """nwn_analyse (the dashboard's child process) must read the user's override folder unless told otherwise."""
    ws = h.WORKSPACE                          # nwn_analyse writes into the dashboard's workspace (the test one)
    import nwn_analyse
    mod = os.path.join(tmp, "ovr_mod"); user = os.path.join(tmp, "ovr_user"); os.makedirs(mod)
    os.makedirs(os.path.join(user, "override")); os.makedirs(os.path.join(user, "modules"))
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Ovr"), Mod_Entry_Area=(R, "yard"),
                                           Mod_Area_list=(LST, [st(6, Area_Name=(R, "yard"))]))))
    w(mod, "yard.are", n.write_gff(root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "yard"), Tileset=(R, "tcn01"))))
    w(os.path.join(user, "override"), "zz_ovr.2da", "2DA V2.0\n\n   Label\n0  a\n")
    with contextlib.redirect_stdout(io.StringIO()):
        rc = nwn_analyse.main([mod, "--name", "ovr_test", "--nwn-user", user, "--no-json"])
    db = os.path.join(ws, "ovr_test", "index.sqlite")
    kinds = {r[0] for r in sqlite3.connect(db).execute("SELECT kind FROM sources")} if os.path.isfile(db) else None
    check("analyse: a run started like the dashboard's (no override option) reads the user's override folder",
          (rc or 0) == 0 and kinds and "override" in kinds, (rc, kinds))


def tag_suffix_matching(tmp):
    """Tag-based scripts found through the suffix map: exactly the scripts the old scan matched."""
    mod = os.path.join(tmp, "tagmod"); os.makedirs(mod)
    tags = ["abcd", "abc", "longtagname", "x_y"]
    scripts = ["abcd", "x_abcd", "ab_abcd", "abc_abcd", "abcd_abcd", "xabcd", "_abcd", "x_abc", "abc",
               "i_longtagname", "zz_longtagname", "x_y", "q_x_y", "ab_x_y"]
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Tags"), Mod_Entry_Area=(R, "yard"),
                                           Mod_Area_list=(LST, [st(6, Area_Name=(R, "yard"))]))))
    w(mod, "yard.are", n.write_gff(root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "yard"), Tileset=(R, "tcn01"))))
    from _gff import item_fields
    for i, t in enumerate(tags):
        w(mod, f"it{i}.uti", n.write_gff(root("UTI ", **item_fields(f"it{i}", t.upper(), t))))
    for sc in scripts:
        w(mod, sc + ".nss", "void main() { }\n")
    out = os.path.join(tmp, "tagout")
    nwn_index.run_index(mod, [], [], None, out, write_json=False, verbose=False)
    got = {(s, d[7:]) for s, d in sqlite3.connect(os.path.join(out, "index.sqlite")).execute(
        "SELECT src, dst FROM edges WHERE kind='tag_based_script'")}
    # the scan the suffix map replaced, written out: exact name, or a 1-4 character prefix ending in "_" before a
    # tag of 4+ characters
    want = set()
    for i, t in enumerate(tags):
        for sc in scripts:
            if sc == t or (len(t) >= 4 and sc.endswith(t) and 1 <= len(sc) - len(t) <= 4 and sc[len(sc) - len(t) - 1] == "_"):
                want.add((f"bp:it{i}.uti", sc))
    check("index: tag-based script matching (suffix map) finds exactly what the old scan found", got == want,
          (sorted(got - want), sorted(want - got)))




def real_module_audit(tmp, root_unused):
    """Three things a real persistent-world build ran into, each of which made a correct build FAIL its audit:
    an entry with no name in the .mod, resources the original only found in the builder's override folder, and a hak
    named like a file inside it (or starting with '+') that the build renames."""
    from _gff import tga
    base = fake_nwn_root(os.path.join(tmp, "ra_root"), BASE)
    user = os.path.join(tmp, "ra_user"); hakd, ovr = os.path.join(user, "hak"), os.path.join(user, "override")
    for d in (hakd, ovr, os.path.join(user, "modules")):
        os.makedirs(d)
    files = [("module", "ifo", n.write_gff(root("IFO ", Mod_Name=loc("RA"), Mod_Entry_Area=(R, "area001"),
                                                 Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]),
                                                 Mod_HakList=(LST, [st(8, Mod_Hak=(X, "rdme")),
                                                                    st(8, Mod_Hak=(X, "+plus")),
                                                                    st(8, Mod_Hak=(X, "ra_c")),
                                                                    st(8, Mod_Hak=(X, "ra_d"))])))),
             ("area001", "are", n.write_gff(root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                                 Tileset=(R, "tcn01")))),
             ("area001", "git", n.write_gff(root("GIT ", **{"Placeable List": (LST, [st(9, TemplateResRef=(R, "statue"),
                                                                                         Tag=(X, "STATUE"))])}))),
             # the blueprint names a texture that only the builder's override folder has
             ("statue", "utp", n.write_gff(root("UTP ", TemplateResRef=(R, "statue"), Tag=(X, "STATUE"),
                                                Comment=(X, "ovr_tex")))),
             ("zzunnamedentry", "res", b"some bytes the game can never load")]
    mod = os.path.join(user, "modules", "ra.mod")
    n.write_erf(mod, files, "MOD ")
    raw = bytearray(open(mod, "rb").read())               # blank the last entry's name, as a damaged .mod has it
    at = raw.find(b"zzunnamedentry")
    raw[at:at + 16] = b"\0" * 16
    open(mod, "wb").write(bytes(raw))
    w(ovr, "ovr_tex.tga", tga(2, 2, lambda x, y: (9, 9, 9)))
    # each hak also has a walkmesh with no model anywhere: the lean plan drops it, so every hak is rebuilt and renamed
    # (a hak with nothing to drop is left alone under its own name - lean_haks_untouched covers that)
    ghost = ("ra_ghost", "wok", b"\0" * 40)
    n.write_erf(os.path.join(hakd, "rdme.hak"), [("rdme", "txt", b"read me"), ("conf", "tga", tga(2, 2, lambda x, y: (1, 2, 3))), ghost], "HAK ")
    n.write_erf(os.path.join(hakd, "+plus.hak"), [("conf", "tga", tga(2, 2, lambda x, y: (4, 5, 6))), ghost], "HAK ")
    n.write_erf(os.path.join(hakd, "ra_c.hak"), [("conf", "tga", tga(2, 2, lambda x, y: (7, 8, 9))), ghost], "HAK ")
    # ra_d repeats the winning copy (rdme's, first listed) exactly: lean haks drop it there, so the clean module has
    # the conflict in fewer places
    n.write_erf(os.path.join(hakd, "ra_d.hak"), [("conf", "tga", tga(2, 2, lambda x, y: (1, 2, 3))),
                                                 ("other", "tga", tga(2, 2, lambda x, y: (3, 3, 3)))], "HAK ")
    out = os.path.join(tmp, "ra_analysis")
    nwn_index.run_index(mod, out=out, verbose=False, nwn_root=base, nwn_user=user)
    nwn_analysis.run_analysis(out, verbose=False)
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    mention = db.execute("SELECT COUNT(*) FROM edges WHERE src='module' AND kind='name_ref' AND dst LIKE '%rdme.txt'").fetchone()[0]
    db.close()
    check("a hak's name in module.ifo is not a mention of a file inside that hak", mention == 0, mention)
    log = nwn_build.build(out, {"delete": [], "merge": [], "lean_haks": True, "git": False}, verbose=False,
                          audit_kwargs=dict(NO_COMP, nwn_root=base, nwn_user=user))
    checks = {c["id"]: c for c in log["audit"]["checks"]}
    unloadable = [d for d in log["deleted"] if d.get("status") == "unloadable"]
    check("an entry with no name in the .mod is left out and logged, so the removal audit passes",
          len(unloadable) == 1 and checks.get("removals", {}).get("status") == "PASS",
          (unloadable, checks.get("removals")))
    iu = checks.get("in_use", {})
    check("a resource only the builder's override folder provided is a note, not a lost resource",
          iu.get("status") == "PASS" and any("override folder" in e and "ovr_tex" in e for e in iu.get("evidence", [])), iu)
    ref = checks.get("references", {})
    check("renamed haks (also one whose name starts with '+') make no conflict look new, and a conflict lean haks "
          "left in fewer places is not new",
          not any("NEW" in e and "conf" in e for e in ref.get("evidence", [])), ref)


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_scen_") as tmp:
        root = fake_nwn_root(os.path.join(tmp, "nwnroot"), BASE + ["nw_c2_default9.ncs", "nw_goblina.utc"])
        for fn in (dave, marta, edit_flow, hakconflict, lean_haks, deletion_rules, lean_hak_rules, lean_haks_untouched,
                   impact_engine, module_file_details, real_module_audit):
            h.run(fn, tmp, root)
        lo = os.path.join(tmp, "load_order"); os.makedirs(lo)
        h.run(index_and_analysis_load_order, lo)       # builds lo/mod, lo/hak_a, lo/hak_b.hak used by the next two
        for fn in (resume_after_hard_kill, mod_handle_closed):
            h.run(fn, lo)
        for fn in (find_haks_matching, missing_index_and_resume_step, analyse_reads_override, tag_suffix_matching):
            own = os.path.join(tmp, fn.__name__); os.makedirs(own)
            h.run(fn, own)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
