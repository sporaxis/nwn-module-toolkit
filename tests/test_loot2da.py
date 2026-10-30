"""
Items listed in 2da tables count as in use (a tester's loot system, 2026-10-28: "the unused item thing will need to
check if the resrefs appear in any 2da files packed in the module"). Loot and random-gear systems keep item resrefs in
2da cells and read them with Get2DAString(table, "RESREF", row), often from a table whose name is built at run time
("trs" + tier + "_" + category + ...). A 2da in the module, a hak or the override folder keeps every blueprint its cells
name in use; the column argument is never taken for a table name.
    python tests/test_loot2da.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import sqlite3
import sys

import nwnlib as n  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, w, R, X, LST, item_fields, stand_in_ncs  # noqa: E402

LOOT_NSS = '''string Tier(int n){ return "lt" + IntToString(n); }
string Cat(){ int r = d10(); if (r <= 4) return "1hnd"; if (r <= 7) return "2hnd"; return "rngd"; }
void main(){ string sList = Tier(Random(7)) + "_" + Cat() + "_" + "swrd" + "_" + "g";
 int nStack = StringToInt(Get2DAString(sList, "STACK", 0));
 string s = Get2DAString(sList, "RESREF", Random(Get2DARowCount(sList))); CreateItemOnObject(s, OBJECT_SELF, nStack); }
'''


def module(folder, tables=True):
    """Three item blueprints; loot tables named at run time list two of them (when `tables`); a script reads them."""
    os.makedirs(folder)
    w(folder, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("Loot"), Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []),
                                            Mod_OnModLoad=(R, "loot_drop"))))
    for rr in ("it_sword_a", "it_axe_b", "it_nowhere"):
        w(folder, f"{rr}.uti", n.write_gff(gff("UTI ", **item_fields(rr, rr.upper(), rr))))
    w(folder, "itempalcus.itp", n.write_gff(gff("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), ID=(n.BYTE, 1))]))))
    w(folder, "loot_drop.nss", LOOT_NSS)
    w(folder, "loot_drop.ncs", stand_in_ncs("loot_drop"))
    if tables:
        w(folder, "lt3_1hnd_swrd_g.2da", "2DA V2.0\n\n   RESREF      STACK\n0  it_sword_a  1\n")
        w(folder, "lt1_2hnd_axes_s.2da", "2DA V2.0\n\n   RESREF      STACK\n0  it_axe_b    1\n")


def status(rep, node):
    """'in use', or the Safe to delete status ('Review', 'Safe' ...) of a node."""
    d = {x["node"]: x for x in rep["deletions"]}
    return d[node]["status"] if node in d else "in use"


def main():
    with h.tempdir("nwn_loot_") as tmp:
        m = os.path.join(tmp, "m")
        module(m)
        out = os.path.join(tmp, "an")
        rep = h.analyse(m, out)
        check("loot: items a module 2da lists are in use, also when the table's name is built at run time",
              [status(rep, f"bp:{x}.uti") for x in ("it_sword_a", "it_axe_b")] == ["in use", "in use"],
              [status(rep, f"bp:{x}.uti") for x in ("it_sword_a", "it_axe_b")])
        check("loot: an item no table, script or area names is not in use", status(rep, "bp:it_nowhere.uti") != "in use")
        db = sqlite3.connect(os.path.join(out, "index.sqlite"))
        bogus = [r[0] for r in db.execute("SELECT dst FROM edges WHERE kind='2da_ref'")]
        db.close()
        check("loot: Get2DAString's column (\"RESREF\", \"STACK\") is never taken for a table name", not any(
            b in ("2da:resref", "2da:stack") for b in bogus), bogus)
        # the same tables outside the module: in a hak, and in the user's override folder (loose files)
        for where in ("hak", "override"):
            m2 = os.path.join(tmp, "m_" + where)
            module(m2, tables=False)
            kw = {}
            src = os.path.join(tmp, "t_" + where)
            os.makedirs(src)
            w(src, "lt3_1hnd_swrd_g.2da", "2DA V2.0\n\n   RESREF      STACK\n0  it_sword_a  1\n")
            if where == "hak":
                h.pack_hak(src, os.path.join(tmp, "loot.hak"))
                kw["haks"] = [os.path.join(tmp, "loot.hak")]
            else:
                u = os.path.join(tmp, "user")
                os.makedirs(os.path.join(u, "modules"))
                os.rename(src, os.path.join(u, "override"))
                kw["nwn_user"] = u
            rep2 = h.analyse(m2, os.path.join(tmp, "an_" + where), **kw)
            check(f"loot: a table in {'a hak' if where == 'hak' else 'the override folder'} keeps its items in use",
                  status(rep2, "bp:it_sword_a.uti") == "in use" and status(rep2, "bp:it_nowhere.uti") != "in use",
                  (status(rep2, "bp:it_sword_a.uti"), status(rep2, "bp:it_nowhere.uti")))
        m3 = os.path.join(tmp, "m_none")
        module(m3, tables=False)
        rep3 = h.analyse(m3, os.path.join(tmp, "an_none"))
        check("loot: without the tables the toolkit can't know - those items are not counted as in use",
              status(rep3, "bp:it_sword_a.uti") != "in use")
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
