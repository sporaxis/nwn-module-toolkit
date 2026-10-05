"""
Tests for the Blueprints page data (nwn_palette.py): the toolset's Custom palette rebuilt from an analysis - category
tree and names, where each blueprint sits, copies hidden by load order, a palette file that is out of date - and the
dashboard's /api/palette and Blueprints page.
    python tests/test_palette.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time

import nwnlib as n  # noqa: E402
import nwn_audit  # noqa: E402
import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_csvio  # noqa: E402
import nwn_palette  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, item_fields, w, R, X, B, DW, F, LST  # noqa: E402

HERE = h.HERE
CUSTOM = n.CUSTOM_TLK_BIT


def cat(strref=None, name=None, pid=None, children=(), delete_me=None):
    """A palette category node: named by a talk-table number (STRREF) or a NAME; pid = the ID blueprints use to
    sit in it; children = sub-categories or blueprint entries; delete_me = the skeleton's English label."""
    f = {}
    if strref is not None:
        f["STRREF"] = (DW, strref)
    if name is not None:
        f["NAME"] = (X, name)
    if delete_me is not None:
        f["DELETE_ME"] = (X, delete_me)
    if pid is not None:
        f["ID"] = (B, pid)
    if children:
        f["LIST"] = (LST, list(children))
    return st(0, **f)


def entry(resref, name):
    """A blueprint entry as the toolset caches it in a custom palette file."""
    return st(0, RESREF=(R, resref), NAME=(X, name))


def itp(*main):
    """A palette file (.itp) whose top level is the given category nodes."""
    return n.write_gff(gff("ITP ", MAIN=(LST, list(main))))


def uti(resref, name, pid, tag=None):
    """An item blueprint sitting in palette category `pid`."""
    f = item_fields(resref, tag or resref.upper(), name)
    f["PaletteID"] = (B, pid)
    return n.write_gff(gff("UTI ", **f))


def ifo():
    """A minimal module.ifo with one area (the analysis needs an entry area)."""
    return n.write_gff(gff("IFO ", Mod_Name=loc("Palette test"), Mod_Entry_Area=(R, "area001"),
                           Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))])))


def area(mod):
    w(mod, "area001.are", n.write_gff(gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                          Tileset=(R, "tcn01"))))
    w(mod, "area001.git", n.write_gff(gff("GIT ")))


def main_fixture(tmp):
    """Module + one hak covering every rule. Returns the analysis folder.

    Items (itempalcus.itp in the module):
      Armor(335) > Shields(6740) > Small Shields(6741, ID 10): lists sm_shield, shared
      Armor(335) > Helmets(6739, ID 9): lists hat - but hat's PaletteID is 10 (palette file out of date)
      Curios (custom tlk entry 3, ID 50): lists ghost - no such blueprint
      Builder stuff (NAME, ID 60): lists nothing - tool's PaletteID is 60 (not listed)
      unknown talk-table number 99999 (ID 70)
      lost.uti: PaletteID 255 - no category has that number
      shared.uti: in the module AND in the hak (the hak copy wins; the module copy is hidden)
    Creatures: no creaturepalcus.itp; the hak's creaturepal.itp skeleton gives Monsters > Giants (ID 4).
    Placeables: no palette file at all."""
    mod = os.path.join(tmp, "mod"); os.makedirs(mod)
    hak_dir = os.path.join(tmp, "hakfiles"); os.makedirs(hak_dir)
    w(mod, "module.ifo", ifo()); area(mod)
    w(mod, "itempalcus.itp", itp(
        cat(335, children=[cat(6740, children=[cat(6741, pid=10, children=[entry("sm_shield", "Small shield"),
                                                                         entry("shared", "Shared")])]),
                           cat(6739, pid=9, children=[entry("hat", "Hat")])]),
        cat(CUSTOM + 3, pid=50, children=[entry("ghost", "Ghost")]),
        cat(name="Builder stuff", pid=60),
        cat(99999, pid=70)))
    w(mod, "sm_shield.uti", uti("sm_shield", "Small shield", 10))
    w(mod, "hat.uti", uti("hat", "Hat", 10))
    w(mod, "tool.uti", uti("tool", "Tool", 60))
    w(mod, "lost.uti", uti("lost", "Lost thing", 255))
    w(mod, "shared.uti", uti("shared", "Shared (module copy)", 10, tag="SHARED_M"))
    w(mod, "twin_a.uti", uti("twin_a", "Rusty Sword", 10))
    w(mod, "twin_b.uti", uti("twin_b", "  rusty sword ", 9))
    w(mod, "ogre.utc", n.write_gff(gff("UTC ", TemplateResRef=(R, "ogre"), Tag=(X, "OGRE"), FirstName=loc("Ogre"),
                                       ChallengeRating=(F, 3.0), PaletteID=(B, 4))))
    # a store keeps its palette number in "ID" (every other blueprint type uses "PaletteID")
    w(mod, "shop.utm", n.write_gff(gff("UTM ", ResRef=(R, "shop"), Tag=(X, "SHOP"), LocName=loc("Shop"), ID=(B, 2))))
    w(mod, "storepalcus.itp", itp(cat(name="Merchants", pid=2, children=[entry("shop", "Shop")]), cat(name="Special", pid=3)))
    w(mod, "crate.utp", n.write_gff(gff("UTP ", TemplateResRef=(R, "crate"), Tag=(X, "CRATE"), LocName=loc("Crate"),
                                        PaletteID=(B, 0))))
    # the hak: a newer copy of shared.uti, and the toolset's category skeletons (English labels in DELETE_ME)
    w(hak_dir, "shared.uti", uti("shared", "Shared (hak copy)", 10, tag="SHARED_H"))
    w(hak_dir, "itempal.itp", itp(
        cat(335, delete_me="Armor", children=[cat(6740, delete_me="Shields", children=[
            cat(6741, pid=10, delete_me="Small Shields")]), cat(6739, pid=9, delete_me="Helmets")])))
    w(hak_dir, "creaturepal.itp", itp(cat(1000, delete_me="Monsters", children=[cat(1001, pid=4, delete_me="Giants")])))
    # base item names: a baseitems.2da in the hak (row 1 = longsword; the fixture's items use base item 1)
    w(hak_dir, "baseitems.2da", "2DA V2.0\n\n   label       Name\n0  shortsword  ****\n1  longsword   ****\n")
    hak = os.path.join(tmp, "testhak.hak")
    h.pack_hak(hak_dir, hak)
    tlk = os.path.join(tmp, "custom.tlk")
    n.write_tlk(tlk, {3: "Curios"})
    out = os.path.join(tmp, "analysis")
    h.analyse(mod, out, haks=[hak], tlk=tlk)
    return out


def by_resref(t):
    return {b["resref"]: b for b in t["blueprints"]}


def path_of(t, b):
    return t["categories"][b["cat"]]["path"] if b["cat"] is not None else None


def tree_and_names(out):
    """Category tree, names from each fallback, and where each blueprint sits."""
    pal = nwn_palette.build_palette(out, write=False)
    it = pal["types"]["uti"]
    bp = by_resref(it)
    paths = [c["path"] for c in it["categories"]]
    check("palette: nested categories make a path (base names from the hak skeleton without dialog.tlk)",
          path_of(it, bp["sm_shield"]) == "Armor › Shields › Small Shields", paths)
    check("palette: a custom talk-table number is named from the custom tlk", "Curios" in paths, paths)
    check("palette: a NAME category is named as it is", "Builder stuff" in paths, paths)
    check("palette: a talk-table number nothing can name shows as #<number>", "#99999" in paths, paths)
    check("palette: the tree comes from the module's custom palette file",
          it["tree_source"].startswith("itempalcus.itp") and "module" in it["tree_source"], it["tree_source"])
    check("palette: a blueprint whose palette number no category has is not in any category",
          bp["lost"]["cat"] is None and bp["lost"]["pid"] == 255, bp["lost"])
    check("palette: a blueprint sits under the category with its palette number (not where the file lists it)",
          path_of(it, bp["hat"]) == "Armor › Shields › Small Shields", bp["hat"])
    check("palette: listed under another category -> 'out of date' note naming where",
          "Armor › Helmets" in bp["hat"].get("stale", ""), bp["hat"])
    check("palette: not listed by the palette file -> 'out of date' note",
          "doesn't list" in bp["tool"].get("stale", ""), bp["tool"])
    check("palette: listed correctly -> no note", "stale" not in bp["sm_shield"], bp["sm_shield"])
    check("palette: an entry the palette file lists for a blueprint that doesn't exist is reported",
          [m["resref"] for m in it["missing"]] == ["ghost"] and it["categories"][it["missing"][0]["cat"]]["name"] == "Curios",
          it["missing"])
    src = pal["sources"]
    sh = bp["shared"]
    check("palette: the hak copy of a blueprint wins over the module's (game load order)",
          src[sh["src"]] == "testhak" and sh["name"] == "Shared (hak copy)" and sh["tag"] == "SHARED_H", (src, sh))
    check("palette: the hidden module copy is listed with its own name and tag",
          [(src[x["src"]], x["name"], x["tag"]) for x in sh.get("hidden", [])] == [("module", "Shared (module copy)", "SHARED_M")],
          sh.get("hidden"))
    check("palette: a blueprint the palette file lists once is not counted as hidden twice",
          sum(1 for b in it["blueprints"] if b["resref"] == "shared") == 1, it["blueprints"])
    cr = pal["types"]["utc"]
    ogre = by_resref(cr)["ogre"]
    check("palette: with no custom palette file the skeleton's tree is used, and the page says so",
          path_of(cr, ogre) == "Monsters › Giants" and "creaturepal.itp" in cr["tree_source"] and
          any("no custom palette file" in x for x in cr["notes"]), (cr["tree_source"], cr["notes"]))
    check("palette: a skeleton-only tree gives no 'out of date' notes", "stale" not in ogre, ogre)
    check("palette: creatures carry their challenge rating", ogre.get("cr") == 3.0, ogre)
    shop = by_resref(pal["types"]["utm"])["shop"]
    check("palette: a store sits in the category its ID field names (stores have no PaletteID)",
          shop["pid"] == 2 and path_of(pal["types"]["utm"], shop) == "Merchants" and "stale" not in shop, shop)
    pl = pal["types"]["utp"]
    check("palette: a type with no palette file at all: blueprints are listed, in no category, with a note",
          by_resref(pl)["crate"]["cat"] is None and not pl["categories"] and pl["notes"], pl)
    # out of date: hat (listed elsewhere), tool and the two twins (not listed); not in a category: lost, crate
    check("palette: the summary counts", pal["summary"] == dict(blueprints=10, unplaced=2, stale=4, hidden=1),
          (pal["summary"], sorted(b["resref"] for b in it["blueprints"] if b.get("stale"))))


def hak_palette_wins(tmp):
    """A custom palette file in a hak beats the module's, as every resource does."""
    mod = os.path.join(tmp, "mod2"); os.makedirs(mod)
    hd = os.path.join(tmp, "hak2"); os.makedirs(hd)
    w(mod, "module.ifo", ifo()); area(mod)
    w(mod, "itempalcus.itp", itp(cat(name="Module category", pid=1)))
    w(mod, "thing.uti", uti("thing", "Thing", 1))
    w(hd, "itempalcus.itp", itp(cat(name="Hak category", pid=1, children=[entry("thing", "Thing")])))
    hak = os.path.join(tmp, "palhak.hak")
    h.pack_hak(hd, hak)
    out = os.path.join(tmp, "analysis2")
    h.analyse(mod, out, haks=[hak])
    it = nwn_palette.build_palette(out, write=False)["types"]["uti"]
    b = by_resref(it)["thing"]
    check("palette: a hak's custom palette file beats the module's", path_of(it, b) == "Hak category" and
          "palhak" in it["tree_source"], (it["tree_source"], path_of(it, b)))


def writes_files(out):
    """build_palette(write=True) writes palette.json and reports/palette.csv, nothing else."""
    for p in ("palette.json", os.path.join("reports", "palette.csv")):
        if os.path.exists(os.path.join(out, p)):
            os.remove(os.path.join(out, p))
    before = {os.path.join(r, f) for r, _d, fs in os.walk(out) for f in fs}
    nwn_palette.build_palette(out)
    after = {os.path.join(r, f) for r, _d, fs in os.walk(out) for f in fs}
    check("palette: writes palette.json and reports/palette.csv only",
          sorted(os.path.relpath(p, out) for p in after - before) == sorted(["palette.json", os.path.join("reports", "palette.csv")]),
          after - before)
    with open(os.path.join(out, "reports", "palette.csv"), encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    hat = next((r for r in rows if r["resref"] == "hat"), {})
    check("palette CSV: one row per copy with type, palette path, source and the note",
          hat.get("type") == "Items" and hat.get("palette") == "Armor › Shields › Small Shields" and
          hat.get("source") == "module" and "Armor › Helmets" in hat.get("note", ""), hat)
    check("palette CSV: a value starting with = can't run as a formula", nwn_palette.csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
          and nwn_palette.csv_cell("Sword") == "Sword" and nwn_palette.csv_cell(None) == "")
    check("palette CSV: hidden copies are rows too, marked hidden",
          any(r["resref"] == "shared" and r["source"] == "module" and "hidden by testhak" in r["note"] for r in rows),
          [r for r in rows if r["resref"] == "shared"])


def analysis_writes_it(out):
    """The analysis itself leaves palette.json behind (written after the catalogue)."""
    check("analysis: palette.json is written by the analysis", os.path.isfile(os.path.join(out, "palette.json")))


def moves_storage(out):
    """Waiting moves (palette_moves.json): set, move back = cancel, hak blueprints and unknown targets refused, undo."""
    mf = os.path.join(out, nwn_palette.MOVES_FILE)
    if os.path.exists(mf):
        os.remove(mf)
    check("moves: none waiting at first", nwn_palette.load_moves(out) == {})
    pal = nwn_palette.build_palette(out, write=False)
    r = nwn_palette.set_moves(out, "uti", ["sm_shield", "tool", "shared", "nope"], 9, pal)
    mv = nwn_palette.load_moves(out)
    check("moves: module blueprints get a waiting move with where they come from",
          mv == {"uti": {"sm_shield": {"to": 9, "from": 10}, "tool": {"to": 9, "from": 60}}} and r["moves"] == mv, mv)
    why = {x["resref"]: x["why"] for x in r["refused"]}
    check("moves: a blueprint whose copy in a hak wins is refused, naming the hak", "testhak" in why.get("shared", ""), why)
    check("moves: an unknown blueprint is refused", "nope" in why, why)
    nwn_palette.set_moves(out, "uti", ["sm_shield"], 10, pal)
    check("moves: moving a blueprint back to where it sits cancels its move",
          nwn_palette.load_moves(out) == {"uti": {"tool": {"to": 9, "from": 60}}}, nwn_palette.load_moves(out))
    for bad, args in (("a number no category has", ("uti", ["tool"], 999)), ("a category without a number", ("uti", ["tool"], None)),
                      ("an unknown type", ("xyz", ["tool"], 9))):
        try:
            nwn_palette.set_moves(out, *args, pal)
            ok = False
        except ValueError:
            ok = True
        check(f"moves: {bad} is refused with nothing written", ok and nwn_palette.load_moves(out) == {"uti": {"tool": {"to": 9, "from": 60}}})
    nwn_palette.set_moves(out, "utc", ["ogre"], 4, pal)          # where it already sits: no move is stored
    nwn_palette.undo_moves(out, "uti", ["tool"])
    check("moves: undo one move (and a move to the current category stores nothing)", nwn_palette.load_moves(out) == {},
          nwn_palette.load_moves(out))
    nwn_palette.set_moves(out, "uti", ["tool"], 50, pal)
    nwn_palette.undo_moves(out)
    check("moves: undo all", nwn_palette.load_moves(out) == {} and json.load(open(mf, encoding="utf-8"))["moves"] == {})


def _clean_gff(out, name, fn):
    """A file of the clean module the last build wrote (build/<name>/<fn>), parsed."""
    return n.read_gff_file(os.path.join(out, "build", name, fn))


def _itp_entries(root):
    """{resref: ID of the category that lists it} for a palette file."""
    found = {}

    def walk(s, cid):
        for c in s.get("LIST") or s.get("MAIN") or []:
            if c.get("RESREF"):
                found[c.get("RESREF").lower()] = cid
            else:
                walk(c, c.get("ID"))
    walk(root, None)
    return found


def moves_build(out):
    """The build applies waiting moves: the palette number changes (a store's ID too), the palette file's entry moves
    to the new category, everything is logged, and the audit proves it (and catches a tampered result)."""
    pal = nwn_palette.build_palette(out, write=False)
    nwn_palette.undo_moves(out)
    nwn_palette.set_moves(out, "uti", ["sm_shield", "hat", "tool", "lost"], 50, pal)
    nwn_palette.set_moves(out, "utm", ["shop"], 3, pal)
    # hat is also edited in the editor (new tag): the move goes on top of the edit
    os.makedirs(os.path.join(out, "edits"), exist_ok=True)
    hat = n.read_gff_file(os.path.join(os.path.dirname(out), "mod", "hat.uti"))
    hat.set("Tag", X, "HAT_EDITED")
    w(os.path.join(out, "edits"), "hat.uti", n.write_gff(hat))
    rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
    lost = next((d for d in rep["deletions"] if d["node"] == "bp:lost.uti"), None)
    plan = dict(delete=["bp:lost.uti"] if lost else [], merge=[], allow_review=True)
    log = nwn_build.build(out, plan, name="moved", verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER))
    pm = {(m["type"], m["resref"]): m for m in log.get("palette_moves", [])}
    check("build: every waiting move is in the change log with what happened", len(pm) == 5, log.get("palette_moves"))
    sm = _clean_gff(out, "moved", "sm_shield.uti")
    check("build: a moved blueprint holds the new palette number", sm.get("PaletteID") == 50, sm.get("PaletteID"))
    check("build: a store's palette number (ID) is moved", _clean_gff(out, "moved", "shop.utm").get("ID") == 3)
    h_ = _clean_gff(out, "moved", "hat.uti")
    check("build: an edited blueprint gets the move on top of the edit", h_.get("PaletteID") == 50 and h_.get("Tag") == "HAT_EDITED",
          (h_.get("PaletteID"), h_.get("Tag")))
    ent = _itp_entries(_clean_gff(out, "moved", "itempalcus.itp"))
    check("build: the palette file now lists moved blueprints under their new category",
          ent.get("sm_shield") == 50 and ent.get("hat") == 50 and ent.get("shared") == 10 and ent.get("ghost") == 50, ent)
    check("build: a store's palette entry moves too", _itp_entries(_clean_gff(out, "moved", "storepalcus.itp")).get("shop") == 3)
    check("build: a blueprint the palette file doesn't list gets the new number, with a note",
          _clean_gff(out, "moved", "tool.uti").get("PaletteID") == 50 and "doesn't list" in pm.get(("uti", "tool"), {}).get("note", ""),
          pm.get(("uti", "tool")))
    if lost:
        check("build: a blueprint deleted in the same build is skipped, and the log says why",
              pm.get(("uti", "lost"), {}).get("status") == "skipped" and "deleted" in pm.get(("uti", "lost"), {}).get("note", ""),
              pm.get(("uti", "lost")))
    else:
        h.skip("build: a blueprint deleted in the same build is skipped", "lost.uti is not deletable in this fixture")
    checks = {c["id"]: c for c in log["audit"]["checks"]}
    check("audit: the moves pass the field-by-field check", checks["modified"]["status"] == "PASS", checks["modified"])
    check("audit: the palette_moves check passes", checks.get("palette_moves", {}).get("status") == "PASS", checks.get("palette_moves"))
    md = open(os.path.join(out, "build", "changes.md"), encoding="utf-8").read()
    check("build: changes.md lists the palette moves", "## Palette moves" in md and "sm_shield" in md, md[-1500:])
    check("build: the summary counts the moves", log["summary"].get("palette_moves") == 4, log["summary"])
    # tamper with the clean module: the audit must notice
    tool = _clean_gff(out, "moved", "tool.uti")
    tool.set("PaletteID", B, 9)
    w(os.path.join(out, "build", "moved"), "tool.uti", n.write_gff(tool))
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        a2 = nwn_audit.audit(out, os.path.join(out, "build"), "moved", compiler=h.NO_COMPILER, verbose=False)
    c2 = {c["id"]: c for c in a2["checks"]}
    check("audit: a blueprint whose palette number isn't the logged one fails", c2["palette_moves"]["status"] == "FAIL" and
          c2["modified"]["status"] == "FAIL", (c2["palette_moves"], c2["modified"]["evidence"][:3]))
    nwn_palette.undo_moves(out)


IMPORT_CSV = """type,name,resref,move_to,palette
Items,Small shield,sm_shield,Curios,x
Items,Hat,hat,armor > helmets,x
Items,Tool,tool,#9,x
Items,Lost,lost,50,x
Items,Shared,shared,Curios,x
Items,Nope,nope,Curios,x
Items,Twin B,twin_b,Armor,x
Stores,Shop,shop,Special,x
Wands,Wand,wand,Curios,x
Items,Twin A,twin_a,Armor / Shields / Small Shields,x
Creatures,Ogre,ogre,MONSTERS›giants,x
Items,Tool,tool,9,x
Items,Hat,hat,Curios,x
Items,Twin B,twin_b,,x
Placeables,Crate,crate,Nowhere,x
"""


def mass_import(out):
    """Import moves from a spreadsheet: preview first (nothing written), every row accounted for with its row number,
    then Save applies exactly that plan. Also the CSV reader (encodings, delimiters) and base item names."""
    pal = nwn_palette.build_palette(out, write=False)
    it = pal["types"]["uti"]
    check("base items: each item carries its base item, named from baseitems.2da (hak)",
          by_resref(it)["sm_shield"].get("base") == 1 and it.get("base_names", {}).get("1") == "longsword",
          (by_resref(it)["sm_shield"].get("base"), it.get("base_names")))
    nwn_palette.undo_moves(out)
    nwn_palette.set_moves(out, "uti", ["twin_a"], 9, pal)                # a waiting move the import cancels
    before = nwn_palette.load_moves(out)
    plan = nwn_palette.plan_import(pal, nwn_palette.load_moves(out), IMPORT_CSV.encode("utf-8"))
    check("import preview writes nothing", nwn_palette.load_moves(out) == before)
    mv = {(m["ext"], m["resref"]): m["to"] for m in plan["moves"]}
    check("import: moves by category name, path (any separator or case) and number",
          mv == {("uti", "sm_shield"): 50, ("uti", "tool"): 9, ("uti", "lost"): 50, ("utm", "shop"): 3}, plan["moves"])
    check("import: moving a blueprint to where it sits cancels its waiting move",
          [(c["ext"], c["resref"]) for c in plan["cancels"]] == [("uti", "twin_a")], plan["cancels"])
    check("import: blank move_to and already-there rows are unchanged", plan["unchanged"] == 2, plan["unchanged"])
    why = {r["row"]: r["why"] for r in plan["refused"]}
    check("import: refused rows carry their row number and why",
          sorted(why) == [3, 6, 7, 8, 10, 14, 16] and "hak" in why[6] and "no item blueprint" in why[7] and
          "can't hold" in why[8] and "type" in why[10] and "different" in why[3] and "different" in why[14] and
          "no category" in why[16], why)
    check("import: every row is accounted for", plan["rows"] == 15 and len(plan["moves"]) + len(plan["cancels"]) +
          plan["unchanged"] + len(plan["refused"]) + plan["duplicates"] == 15, plan)
    res = nwn_palette.apply_import(out, pal, IMPORT_CSV.encode("utf-8"))
    check("import: Save applies exactly the previewed plan", nwn_palette.load_moves(out) == {
        "uti": {"sm_shield": {"to": 50, "from": 10}, "tool": {"to": 9, "from": 60}, "lost": {"to": 50, "from": 255}},
        "utm": {"shop": {"to": 3, "from": 2}}} and res["moves"] == nwn_palette.load_moves(out), nwn_palette.load_moves(out))
    # what Excel writes: Windows-1252, semicolons, different header capitals
    excel = "Type;RESREF;Move_To\r\nItems;tool;Armor › Helmets\r\n".encode("cp1252")
    p2 = nwn_palette.plan_import(pal, {}, excel)
    check("import: Excel's Windows-1252 + semicolon CSV is read (› included)",
          [(m["resref"], m["to"]) for m in p2["moves"]] == [("tool", 9)], p2)
    p3 = nwn_palette.plan_import(pal, {}, "\ufefftype,resref,move_to\nItems,tool,#50\n".encode("utf-8"))
    check("import: a UTF-8 file with a BOM is read", [(m["resref"], m["to"]) for m in p3["moves"]] == [("tool", 50)], p3)
    for bad, data in (("no move_to column", b"type,resref\nItems,tool\n"), ("not a CSV at all", b"\x00\x01\x02"),
                      ("an empty file", b"")):
        try:
            nwn_palette.plan_import(pal, {}, data)
            ok = False
        except ValueError as ex:
            ok = bool(str(ex))
        check(f"import: {bad} is refused with a message", ok)
    amb = {"sources": ["module"], "types": {"uti": {"label": "Items", "categories": [
        dict(name="A", parent=None, id=1, path="A"), dict(name="A", parent=None, id=2, path="A")],
        "blueprints": [dict(resref="x", src=0, pid=1, cat=0, name="X", tag="X")]}}}
    p4 = nwn_palette.plan_import(amb, {}, b"type,resref,move_to\nItems,x,A\nItems,x,#2\n")
    check("import: a path two categories share is refused (use its number)",
          "number" in p4["refused"][0]["why"] and [m["to"] for m in p4["moves"]] == [] and p4["refused"][1]["row"] == 3, p4)
    # a file exactly as the page's Export CSV writes it (BOM, CRLF, extra columns, a formula-defused name), move_to filled
    page = ("\ufeffgroup,type,palette,name,tag,resref,source,palette_number,cr,note,waiting_move,move_to\r\n"
            "Rusty Sword,Items,Armor › Shields › Small Shields,'=Rusty,TWIN_A,twin_a,module,10,,,,#9\r\n")
    p5 = nwn_palette.plan_import(pal, {}, page.encode("utf-8"))
    check("import: a file saved by the page's Export CSV comes back in", [(m["resref"], m["to"]) for m in p5["moves"]] ==
          [("twin_a", 9)] and not p5["refused"], p5)
    rows = nwn_csvio.read_upload("a\tb\n1\t2\n".encode("utf-8"))
    check("csv reader: tab-separated, header lower-cased, row numbers as a spreadsheet counts them",
          rows == [{"a": "1", "b": "2", "_row": 2}], rows)
    nwn_palette.undo_moves(out)


def dashboard_api(out):
    """/api/palette builds palette.json when it is missing, older than the index or of another version, and serves
    the saved file otherwise; the static report embeds the same data."""
    a = "paltest"
    d = os.path.join(D.WORKSPACE, a)
    shutil.copytree(out, d)
    p = os.path.join(d, "palette.json")
    try:
        os.remove(p)
        got = D.GET_ROUTES["/api/palette"]({"a": a})
        check("api: palette.json missing (an older analysis) -> built from the index and saved",
              got["types"]["uti"]["blueprints"] and os.path.isfile(p), sorted(got))
        saved = json.load(open(p, encoding="utf-8"))
        saved["marker"] = 1
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        check("api: a current palette.json is served as saved", D.GET_ROUTES["/api/palette"]({"a": a}).get("marker") == 1)
        later = time.time() + 5
        os.utime(os.path.join(d, "index.sqlite"), (later, later))      # the analysis was run again
        check("api: a palette.json older than the index is built again",
              "marker" not in D.GET_ROUTES["/api/palette"]({"a": a}))
        saved["version"] = -1
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(saved, fh)
        os.utime(p, (later + 5, later + 5))
        check("api: a palette.json from another version is built again",
              D.GET_ROUTES["/api/palette"]({"a": a}).get("version") == nwn_palette.VERSION)
        html = D.export_static(a)
        m = re.search(r"window\.NWN_STATIC=(.*?);</script>", html, re.S)
        data = json.loads(m.group(1)) if m else {}
        check("export: the static report embeds the palette data",
              (data.get("palette") or {}).get("types", {}).get("uti", {}).get("blueprints"), sorted(data))
        check("clear: palette.json is generated data (Clear / Delete remove it)", "palette.json" in D.DATA_FILES)
        # waiting moves through the API (the page's Move selected / Undo)
        r = D.POST_ROUTES["/api/palette/move"]({"a": a, "ext": "uti", "resrefs": ["tool", "shared"], "to": 9})
        check("api: move -> the waiting moves, with the refused ones and why",
              r["moves"] == {"uti": {"tool": {"to": 9, "from": 60}}} and [x["resref"] for x in r["refused"]] == ["shared"], r)
        check("api: the waiting moves can be read back", D.GET_ROUTES["/api/palette/moves"]({"a": a}) == r["moves"])
        for bad in ({"a": a, "ext": "uti", "resrefs": "tool", "to": 9}, {"a": a, "ext": "uti", "resrefs": ["tool"], "to": "9"},
                    {"a": a, "ext": "uti", "resrefs": ["tool"], "to": 999}):
            try:
                D.POST_ROUTES["/api/palette/move"](bad)
                ok = False
            except ValueError:
                ok = True
            check(f"api: a bad move request is refused ({bad['resrefs']!r} to {bad['to']!r})", ok)
        check("api: undo all", D.POST_ROUTES["/api/palette/move/undo"]({"a": a}) == {"moves": {}})
        up = D.UPLOAD_ROUTES["/api/palette/import"]
        pv = up({"a": a}, b"type,resref,move_to\nItems,tool,#9\n")
        check("api: Import moves without apply only previews (nothing stored)",
              [(m["resref"], m["to"]) for m in pv["moves"]] == [("tool", 9)] and
              D.GET_ROUTES["/api/palette/moves"]({"a": a}) == {}, pv)
        ap = up({"a": a, "apply": "1"}, b"type,resref,move_to\nItems,tool,#9\n")
        check("api: Import moves with apply=1 stores them",
              D.GET_ROUTES["/api/palette/moves"]({"a": a}) == {"uti": {"tool": {"to": 9, "from": 60}}} == ap["moves"], ap)
        try:
            up({"a": a}, b"x" * 20_000_001)
            ok = False
        except ValueError:
            ok = True
        check("api: an import file over 20 MB is refused", ok)
        D.POST_ROUTES["/api/palette/move/undo"]({"a": a})
        check("delete: palette_moves.json is your own work (kept unless edits are ticked)",
              "palette_moves.json" in D.USER_PARTS["edits"] and "palette_moves.json" not in D.DATA_FILES)
    finally:
        D.release_analysis(a)


def page_script():
    """The Blueprints page in dashboard.html: registered and in the menu, the nine palettes plus All types, and the
    pure helpers (rows, same-name key, CSV cell) behave - run under Node when it is installed (skipped otherwise)."""
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: VIEWS.blueprints exists and the menu lists it after Items",
          "VIEWS.blueprints = async" in html and '["items","Items"],["blueprints","Blueprints"]' in html)
    check("page: the type tabs are the nine palettes, in nwn_palette's order",
          re.search(r'const BP_TYPES = \[([^\]]*)\]', html).group(1).replace('"', "").replace(" ", "").split(",") ==
          list(nwn_palette.TYPES))
    check("page: All types is offered in the Same name view only", 'st.view === "same" && ["all", "All types"]' in html)
    check("page: the Items page has a Palette column and a palette grouping",
          '{key:"palette", label:"Palette"' in html and '"palette category": i =>' in html)
    check("page: re-opening an analysis reloads the palette data", "S.pal = null;" in html)
    check("page: moves are saved and undone through the API, never written by the page itself",
          'post("/api/palette/move", {a: S.cur, ext: x, resrefs: rrs, to: chosen})' in html and '"/api/palette/move/undo"' in html)
    check("page: the picker offers only categories that can hold blueprints (they have a number)",
          "const targets = t.categories.filter(c => c.id != null);" in html)
    check("page: the static report shows no tick boxes or move buttons",
          '...(STATIC ? [] : [{key: "sel"' in html and '(STATIC ? "" :\n      ` <button class="btn" id="bpimp"' in html)
    check("page: the Build page shows waiting palette moves", 'id="gpm"' in html)
    check("page: Select all shown, Import moves and per-category / per-group select buttons exist (not in the static report)",
          all(x in html for x in ('id="bpall"', 'id="bpimp"', 'data-selcat="${i}"', 'data-selgroup="${esc(k)}"')))
    check("page: Import moves previews first and sends the file again only on Save",
          "let r = await send(false);" in html and "r = await send(true);" in html)
    check("page: Export CSV carries waiting_move and an empty move_to column", '"waiting_move", "move_to",' in html)
    node = shutil.which("node")
    if not node:
        h.skip("page: helper functions under Node", "Node is not installed")
        return
    pick = lambda start, end: html[html.index(start):html.index(end, html.index(start))]  # noqa: E731
    js = "const has = (q, ...vals) => !q || vals.some(v => String(v ?? '').toLowerCase().includes(q));\n" + \
        pick("function palRows(", "function downloadCsv(") + """
const pal = {sources: ["module", "hakx"], types: {uti: {label: "Items", categories: [{name: "A", parent: null, id: 1, path: "A"}],
  blueprints: [{node: "bp:a.uti", resref: "a", name: " Rusty  Sword", tag: "T", src: 1, pid: 1, cat: 0,
                hidden: [{src: 0, name: "rusty sword", tag: "T2", pid: 9, cat: null}]},
               {node: "bp:b.uti", resref: "b", name: "", tag: "", src: 0, pid: 255, cat: null, stale: "x"}]}}};
const all = palRows(pal, "uti", true), win = palRows(pal, "uti", false);
const mvd = palRows({...pal, sources: ["hakx", "module"]}, "uti", true, {uti: {a: {to: 1, from: 1}}});
const cats = [{name: "A", parent: null, id: null, path: "A"}, {name: "B", parent: 0, id: 5, path: "A › B"}, {name: "C", parent: null, id: 6, path: "C"}];
const fr = [{resref: "x", name: "Sword", tag: "T", cat: 1, src: "module", base: 1, note: "", moveTo: null, movable: true, ext: "uti"},
            {resref: "y", name: "Ring", tag: "R", cat: 2, src: "hakx", base: 52, note: "stale", moveTo: 6, movable: false, ext: "uti"},
            {resref: "z", name: "Lost", tag: "L", cat: null, src: "module", base: 1, note: "", moveTo: null, movable: true, ext: "uti"},
            {resref: "x", name: "Sword", tag: "T", cat: 1, src: "hakx", hiddenBy: "module", movable: false, ext: "uti"}];
const F = f => bpFilter(fr, Object.assign({q: "", cat: "", src: "", base: ""}, f), cats).map(r => r.resref + (r.hiddenBy ? "*" : ""));
const flt = {catA: F({cat: "0"}), src: F({src: "hakx"}), base: F({base: "1"}), none: F({none: true}), stale: F({stale: true}),
             waiting: F({waiting: true}), q: F({q: "rin"})};
const sel = new Set(["uti:z"]), sres = bpSelect(sel, fr);
console.log(JSON.stringify({n_all: all.length, n_win: win.length, hidden: all[1], keys: all.map(sameNameKey), mvd, flt, sres, sel: [...sel].sort(),
  cells: ["=1+1", "-x", "@a", "plain", 'say "hi", ok'].map(csvCell)}));
"""
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
    try:
        o = json.loads(r.stdout)
    except ValueError:
        check("page: helper functions run under Node", False, r.stderr[-800:])
        return
    check("page: palRows lists the winning copies, and with hidden=true each hidden copy too", o["n_win"] == 2 and o["n_all"] == 3, o)
    hid = o["hidden"]
    check("page: a hidden copy row keeps its own name, source and palette spot, and names the winning source",
          hid["name"] == "rusty sword" and hid["src"] == "module" and hid["path"] is None and hid["hiddenBy"] == "hakx", hid)
    check("page: same-name key ignores case and extra spaces; no name -> the resref",
          o["keys"] == ["name:rusty sword", "name:rusty sword", "resref:b"], o["keys"])
    check("page: filters - a category includes its sub-categories; source, base item, no category, out of date, waiting, search",
          o["flt"] == {"catA": ["x", "x*"], "src": ["y", "x*"], "base": ["x", "z"], "none": ["z"], "stale": ["y"],
                       "waiting": ["y"], "q": ["y"]}, o["flt"])
    check("page: select all ticks movable rows, counts hak copies as skipped, ignores hidden copies and already-ticked ones",
          o["sres"] == {"added": 1, "skipped": 1} and o["sel"] == ["uti:x", "uti:z"], (o["sres"], o["sel"]))
    mvd = o["mvd"]
    check("page: only the module's own copy can be ticked (not a hak's, not a hidden copy)",
          [r["movable"] for r in mvd] == [True, False, False], [(r["resref"], r["src"], r["movable"]) for r in mvd])
    check("page: a waiting move shows on its row with the target's path (not on its hidden copy)",
          mvd[0]["moveTo"] == 1 and mvd[0]["movePath"] == "A" and mvd[1]["moveTo"] is None, mvd[:2])
    check("page: CSV cells can't run as formulas, and quotes/commas are escaped",
          o["cells"] == ["'=1+1", "'-x", "'@a", "plain", '"say ""hi"", ok"'], o["cells"])


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    with h.tempdir("nwn_pal_") as tmp:
        out = h.run(main_fixture, tmp)
        if out:
            h.run(analysis_writes_it, out)
            h.run(tree_and_names, out)
            h.run(writes_files, out)
            h.run(dashboard_api, out)
            h.run(moves_storage, out)
            h.run(moves_build, out)
            h.run(mass_import, out)
        h.run(hak_palette_wins, tmp)
        h.run(page_script)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
