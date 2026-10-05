"""
Tests for bulk blueprint changes (nwn_bpchanges.py): faction, name, tag and description changes that wait for the
next build, which applies them to the blueprint and to every placed copy still holding the old value; the audit
proves it. Also the refusals (hak blueprints, tags scripts use, unknown factions) and the spreadsheet columns.
    python tests/test_bpchanges.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import shutil
import sys

import nwnlib as n  # noqa: E402
import nwn_audit  # noqa: E402
import nwn_bpchanges as BC  # noqa: E402
import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_palette  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, item_fields, w, R, X, B, DW, LST, WORD, stand_in_ncs  # noqa: E402

NAMES = ["PC", "Hostile", "Commoner", "Merchant", "Defender", "Bandits"]


def goblin(**extra):
    f = dict(TemplateResRef=(R, "goblin"), Tag=(X, "GOB"), FirstName=loc("Goblin"), LastName=loc(""),
             Description=loc("A goblin."), FactionID=(WORD, 1), PaletteID=(B, 1))
    f.update(extra)
    return f


def fixture(tmp):
    mod = os.path.join(tmp, "mod"); os.makedirs(mod)
    hk = os.path.join(tmp, "hk"); os.makedirs(hk)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", gff("IFO ", Mod_Name=loc("Bulk"), Mod_Entry_Area=(R, "area001"),
                        Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]), Mod_OnModLoad=(R, "s1")))
    g("area001.are", gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"), Tileset=(R, "tcn01")))
    sword = item_fields("sword", "SWORD", "Sword")
    g("area001.git", gff("GIT ", **{
        "Creature List": (LST, [st(4, **goblin(XPosition=(n.FLOAT, 1.0), YPosition=(n.FLOAT, 2.0))),
                                st(4, **goblin(FirstName=loc("Goblin Chief"), Tag=(X, "GOB_BOSS"),
                                               XPosition=(n.FLOAT, 5.0), YPosition=(n.FLOAT, 6.0)))]),
        "Placeable List": (LST, [st(9, TemplateResRef=(R, "chest"), Tag=(X, "CHEST"), LocName=loc("Chest"),
                                    HasInventory=(B, 1), XPosition=(n.FLOAT, 3.0), YPosition=(n.FLOAT, 4.0),
                                    ItemList=(LST, [st(0, **sword)]))])}))
    g("goblin.utc", gff("UTC ", **goblin()))
    g("sword.uti", gff("UTI ", **sword))
    g("keyitem.uti", gff("UTI ", **item_fields("keyitem", "KEY_A", "Old key")))
    g("chest.utp", gff("UTP ", TemplateResRef=(R, "chest"), Tag=(X, "CHEST"), LocName=loc("Chest"),
                       Description=loc("")))
    g("repute.fac", gff("FAC ", FactionList=(LST, [st(0, FactionName=(X, nm), FactionGlobal=(DW, 0),
                                                       FactionParentID=(DW, 0xFFFFFFFF)) for nm in NAMES]),
                        RepList=(LST, [st(0, FactionID1=(DW, 0), FactionID2=(DW, 1), FactionRep=(DW, 0))])))
    for nm, ff in (("itempalcus.itp", "uti"), ("creaturepalcus.itp", "utc"), ("placeablepalcus.itp", "utp")):
        g(nm, gff("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), ID=(B, 1))])))
    # a script that looks up KEY_A (so that tag can't change) and TAKEN (so no blueprint may take that tag)
    w(mod, "s1.nss", 'void main(){ object o = GetObjectByTag("KEY_A"); object p = GetObjectByTag("TAKEN"); }\n')
    w(mod, "s1.ncs", stand_in_ncs("s1"))
    w(hk, "hakbp.uti", n.write_gff(gff("UTI ", **item_fields("hakbp", "HAKBP", "Hak thing"))))
    hak = os.path.join(tmp, "bulkhak.hak")
    h.pack_hak(hk, hak)
    out = os.path.join(tmp, "analysis")
    h.analyse(mod, out, haks=[hak])
    return out


def planning(out):
    """Plans from the page's Change fields… dialog: what is accepted, what is refused and why."""
    ctx = BC.load_context(out)
    check("context: factions by number and name", ctx["factions"].get(5) == "Bandits", ctx["factions"])
    p = BC.plan_values(ctx, "utc", ["goblin"], {"faction": "bandits", "first_name": "Hobgoblin"})
    ch = {(c["resref"], c["field"]): (c["from"], c["to"]) for c in p["changes"]}
    check("plan: a faction by name (any case) and a new first name",
          ch == {("goblin", "faction"): (1, 5), ("goblin", "first_name"): ("Goblin", "Hobgoblin")}, p)
    check("plan: the preview counts the placed copies that may follow", p["changes"][0]["placed"] == 2, p["changes"])
    p = BC.plan_values(ctx, "uti", ["keyitem", "sword", "hakbp", "nope"], {"tag": "TAKEN"})
    why = {r["item"]: r["why"] for r in p["refused"]}
    check("plan: a tag scripts look up can't change; a new tag scripts look up can't be taken; hak and unknown refused",
          "looked up" in why.get("uti:keyitem", "") and "already look" in why.get("uti:sword", "") and
          "hak" in why.get("uti:hakbp", "") and "no item blueprint" in why.get("uti:nope", "") and not p["changes"], why)
    for vals, bit in (({"tag": "X" * 33}, "32"), ({"faction": "Pirates"}, "faction"), ({"colour": "red"}, "field")):
        p = BC.plan_values(ctx, "utc" if "faction" in vals else "uti", ["goblin" if "faction" in vals else "sword"], vals)
        check(f"plan: refused - {bit}", not p["changes"] and bit in " ".join(r["why"] for r in p["refused"]), p)
    p = BC.plan_values(ctx, "uti", ["sword"], {"name": "Sword"})
    check("plan: a value that is already so is unchanged", not p["changes"] and p["unchanged"] == 1, p)


def storage_and_build(out):
    """Store, undo, then build: blueprint and matching placed copies change; the audit proves it."""
    ctx = BC.load_context(out)
    BC.undo_changes(out)
    BC.save_plan(out, BC.plan_values(ctx, "utc", ["goblin"], {"faction": "Bandits", "first_name": "Hobgoblin"}))
    BC.save_plan(out, BC.plan_values(ctx, "uti", ["sword"], {"name": "Rusty Blade", "tag": "SWORD2"}))
    BC.save_plan(out, BC.plan_values(ctx, "uti", ["keyitem"], {"description": "Opens the old gate."}))
    ch = BC.load_changes(out)
    check("store: waiting changes per blueprint and field, with where they come from",
          ch["utc"]["goblin"]["faction"] == {"to": 5, "from": 1} and ch["uti"]["sword"]["tag"] == {"to": "SWORD2", "from": "SWORD"}, ch)
    BC.undo_changes(out, "uti", ["keyitem"])
    check("store: undo one blueprint's changes", "keyitem" not in BC.load_changes(out).get("uti", {}), BC.load_changes(out))
    log = nwn_build.build(out, dict(delete=[], merge=[]), name="bulk", verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER))
    clean = os.path.join(out, "build", "bulk")
    gb = n.read_gff_file(os.path.join(clean, "goblin.utc"))
    check("build: the blueprint gets the new faction and name",
          gb.get("FactionID") == 5 and gb.get("FirstName").text() == "Hobgoblin", (gb.get("FactionID"), gb.get("FirstName")))
    git = n.read_gff_file(os.path.join(clean, "area001.git"))
    cr = git.get("Creature List")
    check("build: a placed copy still holding the old values follows its blueprint",
          cr[0].get("FactionID") == 5 and cr[0].get("FirstName").text() == "Hobgoblin", cr[0].get("FirstName"))
    check("build: a placed copy with its own name keeps it, but takes the faction it still shared",
          cr[1].get("FirstName").text() == "Goblin Chief" and cr[1].get("FactionID") == 5, cr[1].get("FirstName"))
    it = git.get("Placeable List")[0].get("ItemList")[0]
    check("build: an item inside a placed container follows its blueprint (name and tag)",
          it.get("LocalizedName").text() == "Rusty Blade" and it.get("Tag") == "SWORD2", (it.get("LocalizedName"), it.get("Tag")))
    sw = n.read_gff_file(os.path.join(clean, "sword.uti"))
    check("build: a new name replaces the text (no talk-table reference left)", sw.get("LocalizedName").strref == n.BAD_STRREF and
          sw.get("LocalizedName").entries == {0: "Rusty Blade"}, sw.get("LocalizedName"))
    bc = log.get("blueprint_changes") or []
    check("build: the change log lists every change with how many placed copies followed",
          {(c["type"], c["resref"], c["field"]) for c in bc} == {("utc", "goblin", "faction"), ("utc", "goblin", "first_name"),
                                                                ("uti", "sword", "name"), ("uti", "sword", "tag")} and
          next(c for c in bc if c["field"] == "faction")["placed"] == 2 and
          next(c for c in bc if c["field"] == "first_name")["placed"] == 1, bc)
    checks = {c["id"]: c for c in log["audit"]["checks"]}
    for cid in ("modified", "placements", "blueprint_changes"):
        check(f"audit: {cid} passes", checks.get(cid, {}).get("status") == "PASS", checks.get(cid))
    md = open(os.path.join(out, "build", "changes.md"), encoding="utf-8").read()
    check("changes.md has a Blueprint changes section", "## Blueprint changes" in md and "Hobgoblin" in md, md[-1200:])
    # tamper: the audit must notice a blueprint that doesn't hold the logged value
    gb.set("FactionID", WORD, 3)
    w(clean, "goblin.utc", n.write_gff(gb))
    with contextlib.redirect_stdout(io.StringIO()):
        a2 = nwn_audit.audit(out, os.path.join(out, "build"), "bulk", compiler=h.NO_COMPILER, verbose=False)
    c2 = {c["id"]: c for c in a2["checks"]}
    check("audit: a blueprint that doesn't hold its logged change fails", c2["blueprint_changes"]["status"] == "FAIL", c2["blueprint_changes"])
    BC.undo_changes(out)


def spreadsheet_and_api(out):
    """The Blueprints page's one Import… reads move_to and the new_* columns; the API stores field changes."""
    a = "bctest"
    d = os.path.join(D.WORKSPACE, a)
    shutil.copytree(out, d)
    try:
        csv = ("type,resref,move_to,new_first_name,new_faction,new_tag\n"
               "Creatures,goblin,,Hobgoblin,Defender,\nItems,keyitem,,,,KEY_B\nItems,sword,,,,\n").encode()
        pv = D.UPLOAD_ROUTES["/api/palette/import"]({"a": a}, csv)
        fc = {(c["resref"], c["field"]): c["to"] for c in pv["fields"]["changes"]}
        check("import: new_* columns become field changes (preview only)", fc == {("goblin", "first_name"): "Hobgoblin",
              ("goblin", "faction"): 4} and BC.load_changes(d) == {}, pv["fields"])
        check("import: a refused field row names its row and why", [r["row"] for r in pv["fields"]["refused"]] == [3] and
              "looked up" in pv["fields"]["refused"][0]["why"], pv["fields"]["refused"])
        check("import: rows without move_to don't count as refused moves", not pv["refused"] and pv["unchanged"] == 3, pv)
        D.UPLOAD_ROUTES["/api/palette/import"]({"a": a, "apply": "1"}, csv)
        check("import: apply stores the field changes", BC.load_changes(d)["utc"]["goblin"]["faction"]["to"] == 4, BC.load_changes(d))
        r = D.POST_ROUTES["/api/bpchange/plan"]({"a": a, "ext": "uti", "resrefs": ["sword"], "values": {"name": "Blade"}})
        check("api: plan from the dialog (nothing stored)", [c["to"] for c in r["changes"]] == ["Blade"] and
              "sword" not in BC.load_changes(d).get("uti", {}), r)
        r = D.POST_ROUTES["/api/bpchange/save"]({"a": a, "ext": "uti", "resrefs": ["sword"], "values": {"name": "Blade"}})
        check("api: save stores it (planned again on the server)", r["changes"]["uti"]["sword"]["name"]["to"] == "Blade", r)
        check("api: waiting changes can be read", D.GET_ROUTES["/api/bpchange/list"]({"a": a}) == r["changes"])
        r = D.POST_ROUTES["/api/bpchange/undo"]({"a": a})
        check("api: undo all", r == {"changes": {}}, r)
        check("delete: blueprint_changes.json is your own work", "blueprint_changes.json" in D.USER_PARTS["edits"])
    finally:
        D.release_analysis(a)


def page_script():
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: Change fields… on the Blueprints page, planned on the server and saved only after the preview",
          'id="bpchg"' in html and '"/api/bpchange/plan"' in html and '"/api/bpchange/save"' in html)
    check("page: Export CSV has the new_* columns", '"new_name", "new_first_name"' in html)
    check("page: the Build page counts waiting blueprint changes", "/api/bpchange/list" in html and 'id="gbc"' in html)


def main():
    with h.tempdir("nwn_bc_") as tmp:
        out = h.run(fixture, tmp)
        if out:
            for fn in (planning, storage_and_build, spreadsheet_and_api):
                h.run(fn, out)
    h.run(page_script)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
