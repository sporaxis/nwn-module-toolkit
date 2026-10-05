"""
Tests for duplicate models and textures across the module and its haks (report.json "asset_duplicates"): files with
different names but the same content - head models that differ only in their own name, byte-identical textures -
and what is NOT a duplicate (the same name carried by two haks, models that really differ).
    python tests/test_assetdups.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import csv
import os
import sys

import nwnlib as n  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, w, tga, R, X, LST  # noqa: E402


def head(name, extra=""):
    """An ASCII head model that names itself (newmodel, supermodel line, root node, its texture) like real heads."""
    return (f"newmodel {name}\nsetsupermodel {name} NULL\nbeginmodelgeom {name}\nnode dummy {name}\n  parent NULL\n"
            f"endnode\nnode trimesh head\n  parent {name}\n  bitmap {name}\n{extra}endnode\nendmodelgeom {name}\n"
            f"donemodel {name}\n")


def binary(name):
    """A minimal 'binary' model: 4 zero bytes, then the model name in a 64-byte field padded with zeros (the layout
    compiled models use for names), then the same body."""
    return b"\0\0\0\0" + name.encode().ljust(64, b"\0") + b"BODY" * 8 + name.encode().ljust(32, b"\0")


def fixture(tmp):
    mod = os.path.join(tmp, "mod"); os.makedirs(mod)
    ha = os.path.join(tmp, "ha"); os.makedirs(ha)
    hb = os.path.join(tmp, "hb"); os.makedirs(hb)
    w(mod, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("Dup assets"), Mod_Entry_Area=(R, "area001"),
                                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "area001"))]))))
    w(mod, "area001.are", n.write_gff(gff("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                          Tileset=(R, "tcn01"))))
    w(mod, "area001.git", n.write_gff(gff("GIT ")))
    red = tga(4, 4, lambda x, y: (200, 10, 10))
    # hak A: head 12 (model + texture), head 77 a copy under another name (model differs only in its name)
    w(ha, "pmh0_head012.mdl", head("pmh0_head012"))
    w(ha, "pmh0_head012.tga", red)
    w(ha, "pmh0_head077.mdl", head("pmh0_head077"))
    w(ha, "pmh0_head077.tga", red)
    w(ha, "pmh0_head013.mdl", head("pmh0_head013", "  verts 1\n"))      # a different head: not a duplicate
    w(ha, "bin_a.mdl", binary("bin_a"))
    w(ha, "bin_longer_nm.mdl", binary("bin_longer_nm"))                  # binary, longer name, same content
    # hak B: the same head 12 texture again (same name = load order, not a duplicate) and a third copy of it
    w(hb, "pmh0_head012.tga", red)
    w(hb, "pfh0_head200.tga", red)
    w(mod, "my_red.tga", red)                                              # the module has a copy too
    a, b = os.path.join(tmp, "dup_a.hak"), os.path.join(tmp, "dup_b.hak")
    h.pack_hak(ha, a)
    h.pack_hak(hb, b)
    out = os.path.join(tmp, "analysis")
    rep = h.analyse(mod, out, haks=[a, b])
    return rep, out


def groups(tmp):
    rep, out = fixture(tmp)
    ad = rep.get("asset_duplicates") or []
    names = [sorted(m["name"] for m in g["members"]) for g in ad]
    tex = next((g for g in ad if g["kind"] == "texture"), None)
    check("textures: byte-identical textures under different names are one group, across the module and both haks",
          tex is not None and sorted(m["name"] for m in tex["members"]) ==
          ["my_red.tga", "pfh0_head200.tga", "pmh0_head012.tga", "pmh0_head077.tga"], names)
    m12 = next((m for m in (tex or {}).get("members", []) if m["name"] == "pmh0_head012.tga"), {})
    check("textures: a name carried by two haks is one member, listing both (load order picks one)",
          m12.get("sources") == ["dup_a", "dup_b"], m12)
    mdl = [sorted(m["name"] for m in g["members"]) for g in ad if g["kind"] == "model"]
    check("models: head models that differ only in their own name are a group; a model that really differs is not",
          ["pmh0_head012.mdl", "pmh0_head077.mdl"] in mdl and not any("pmh0_head013.mdl" in x for x in mdl), mdl)
    check("models: binary models whose names differ in length still match (the padded name field is ignored)",
          ["bin_a.mdl", "bin_longer_nm.mdl"] in mdl, mdl)
    g12 = next((g for g in ad if g["kind"] == "model" and any(m["name"] == "pmh0_head012.mdl" for m in g["members"])), {})
    check("heads: a head model's number is shown (heads are picked by number, so both may be needed)",
          sorted(m.get("head") for m in g12.get("members", [])) == [12, 77] and "number" in g12.get("note", ""), g12)
    check("size: each group says how many bytes the extra copies take", tex and tex["bytes"] == len(tga(4, 4, lambda x, y: (0, 0, 0)))
          and tex["extra_bytes"] == 3 * tex["bytes"], tex)
    check("not mergeable: these are for review (models and textures are picked by name or number)",
          all(not g.get("mergeable") for g in ad))
    check("the old module-only 'Asset - identical file' groups are not repeated in Duplicates",
          not any(d["category"].startswith("Asset (tga)") for d in rep["duplicates"]), [d["category"] for d in rep["duplicates"]])
    p = os.path.join(out, "reports", "asset_duplicates.csv")
    rows = list(csv.DictReader(open(p, encoding="utf-8-sig", newline=""))) if os.path.isfile(p) else []
    check("reports/asset_duplicates.csv lists one row per member with its group",
          len(rows) == sum(len(g["members"]) for g in ad) and {"group", "kind", "name", "sources", "bytes"} <= set(rows[0] if rows else {}),
          rows[:3])


def page_script():
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the Duplicates page shows duplicate models and textures (from the module and its haks)",
          "asset_duplicates" in html and "Models and textures with the same content" in html)


def main():
    with h.tempdir("nwn_adup_") as tmp:
        h.run(groups, tmp)
    h.run(page_script)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
