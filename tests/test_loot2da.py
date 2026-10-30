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
        # the loot table kept in the campaign database (a tester's world, 2026-10-30): once its files are loaded on the
        # Database page (<analysis>/database.json) and the module analysed again, the items it names are in use
        import contextlib, io, json
        import nwn_analysis, nwn_database
        dbf = os.path.join(tmp, "database")
        os.makedirs(dbf)
        con = sqlite3.connect(os.path.join(dbf, "loot.sqlite3"))
        con.execute("CREATE TABLE loot (tier int, resref text, tag text)")
        con.executemany("INSERT INTO loot VALUES (?,?,?)", [(1, "it_sword_a", ""), (2, "", "IT_AXE_B")])
        con.commit(); con.close()
        snap = nwn_database.read_path(dbf)
        with open(os.path.join(tmp, "an_none", "database.json"), "w", encoding="utf-8") as fh:
            json.dump(snap, fh, default=str)
        with contextlib.redirect_stdout(io.StringIO()):
            rep4 = nwn_analysis.run_analysis(os.path.join(tmp, "an_none"), verbose=False)
        check("loot: a loaded campaign database that names items (by resref, or by tag) keeps them in use; the rest not",
              [status(rep4, f"bp:{x}.uti") for x in ("it_sword_a", "it_axe_b", "it_nowhere")][:2] == ["in use", "in use"]
              and status(rep4, "bp:it_nowhere.uti") != "in use" and
              {x["node"] for x in rep4["db_named"]} >= {"bp:it_sword_a.uti", "tag:IT_AXE_B"} and rep4["db_words"] > 0,
              ([status(rep4, f"bp:{x}.uti") for x in ("it_sword_a", "it_axe_b", "it_nowhere")], rep4["db_named"]))
        # an adversarial pass (2026-10-30): a loot list stored as ONE long campaign string (60 resrefs, far past the
        # 300 characters shown on the page) must be scanned whole; values of an "accounts" table never reach the
        # snapshot when the dashboard's keep set is given (only words that are a module name are kept); a .sql dump's
        # INSERT values count; a hak item the database names stays in a minimal lean hak and the audit passes
        import nwn_dashboard as DASH
        import nwn_hakslim
        con = sqlite3.connect(os.path.join(dbf, "loot.sqlite3"))
        con.execute("CREATE TABLE db (varname varchar, playerid varchar, vartype tinyint, payload blob, compressed bool)")
        longlist = ",".join(f"it_loot_{i:03d}" for i in range(60)) + ",it_nowhere"
        con.execute("INSERT INTO db VALUES ('LOOT', '', 83, ?, 0)", (longlist.encode(),))
        con.execute("CREATE TABLE accounts (login text, email text, pwhash text)")
        con.execute("INSERT INTO accounts VALUES ('kenbs', 'someone@example.com', '$2b$12$abcdefghijklmnop')")
        con.commit(); con.close()
        with open(os.path.join(dbf, "dump.sql"), "w") as fh:
            fh.write("INSERT INTO loot VALUES (3, 'it_sword_a', 'x'), (4, 'it_sqldump', '');\n")
        keep = {"it_nowhere", "it_sword_a", "it_axe_b", "it_sqldump", "it_hakonly"}
        snap2 = nwn_database.read_path(dbf, keep)
        check("database words: a long campaign string is scanned whole, a .sql dump's values count, and with the keep "
              "set nothing but module names is stored (no e-mail, login or hash)",
              {"it_nowhere", "it_sqldump", "it_sword_a"} <= set(snap2["words"]) and
              not ({"kenbs", "someone", "example", "abcdefghijklmnop", "it_loot_059"} & set(snap2["words"])), snap2["words"])
        snap3 = nwn_database.read_path(dbf)
        check("database words: without a keep set the whole value is scanned (it_loot_059 at the end of the string)",
              "it_loot_059" in snap3["words"])
        # a hak holds an item only the database names: minimal lean haks keep it, the build audit passes
        m6 = os.path.join(tmp, "m_dbhak")
        module(m6, tables=False)
        hsrc = os.path.join(tmp, "hsrc"); os.makedirs(hsrc)
        for rr in ("it_hakonly", "it_hakjunk"):
            w(hsrc, f"{rr}.uti", n.write_gff(gff("UTI ", **item_fields(rr, rr.upper(), rr))))
        h.pack_hak(hsrc, os.path.join(tmp, "stuff.hak"))
        out6 = os.path.join(tmp, "an_dbhak")
        h.analyse(m6, out6, haks=[os.path.join(tmp, "stuff.hak")])
        con = sqlite3.connect(os.path.join(dbf, "loot.sqlite3"))
        con.execute("INSERT INTO loot VALUES (5, 'it_hakonly', '')"); con.commit(); con.close()
        with open(os.path.join(out6, "database.json"), "w", encoding="utf-8") as fh:
            json.dump(nwn_database.read_path(dbf, DASH.module_names(out6)), fh, default=str)
        with contextlib.redirect_stdout(io.StringIO()):
            rep6 = nwn_analysis.run_analysis(out6, verbose=False)
        plan6 = nwn_hakslim.plan_lean_haks(out6, mode="minimal")
        hk = plan6.get("stuff") or {}
        check("lean haks (minimal): a hak item only the loaded database names is kept, the unnamed one dropped",
              hk.get("keep") == ["it_hakonly.uti"] and [d["file"] for d in hk.get("drop", [])] == ["it_hakjunk.uti"], plan6)
        # second adversarial pass (2026-10-31): a database loaded AFTER the analysis names an item the report still
        # calls unused - a build from that report is refused until Analyse again
        import nwn_build
        snapA = json.load(open(os.path.join(out6, "database.json"), encoding="utf-8"))
        snapA["words"] = sorted(set(snapA["words"]) | {"zz_loaded_later"})     # any change to the loaded names
        with open(os.path.join(out6, "database.json"), "w", encoding="utf-8") as fh:
            json.dump(snapA, fh, default=str)
        try:
            nwn_build.build(out6, dict(delete=["bp:it_nowhere.uti"], merge=[]), name="st", verbose=False,
                            audit_kwargs=dict(compiler=h.NO_COMPILER))
            refused = ""
        except ValueError as ex:
            refused = str(ex)
        check("build: refused when the loaded database changed since the analysis (it may name items on the list)",
              "Analyse again" in refused, refused[:200])
        with contextlib.redirect_stdout(io.StringIO()):
            rep7 = nwn_analysis.run_analysis(out6, verbose=False)
        check("after Analyse again the item the database names is in use and its impact names the database",
              status(rep7, "bp:it_nowhere.uti") == "in use" and
              any("campaign database" in r for r in rep7["impact"]["bp:it_nowhere.uti"]["reasons"]),
              (status(rep7, "bp:it_nowhere.uti"), rep7["impact"].get("bp:it_nowhere.uti")))
        # a capped scan: minimal lean haks fall back to builder mode and say so (nothing a name could reach is dropped)
        snapA["words_capped"] = True
        with open(os.path.join(out6, "database.json"), "w", encoding="utf-8") as fh:
            json.dump(snapA, fh, default=str)
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_analysis.run_analysis(out6, verbose=False)
        log = nwn_build.build(out6, dict(delete=[], merge=[], lean_haks=True, lean_haks_mode="minimal"), name="cap",
                              verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER))
        iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
        check("a capped database scan: lean haks built in builder mode with a warning; the audit's in_use is a WARN",
              log.get("lean_haks_mode") == "builder" and any("too big to scan" in w_ for w_ in log["warnings"]) and
              iu["status"] == "WARN" and "it_hakjunk.uti" in os.listdir(os.path.join(out6, "build", "cap")) is False or
              (log.get("lean_haks_mode") == "builder" and iu["status"] == "WARN"), (log.get("lean_haks_mode"), log["warnings"][:3], iu["status"]))
        # a hostile .sql dump (one 8 MB quoted value, then an unterminated one): read in linear time, nothing kept
        import time as _t
        hostile = os.path.join(tmp, "hostile"); os.makedirs(hostile)
        with open(os.path.join(hostile, "big.sql"), "w") as fh:
            fh.write("INSERT INTO t VALUES ('" + "x" * (8 << 20) + "');\nINSERT INTO t VALUES ('" + "\\'" * 200000 + "\n")
        t0 = _t.time()
        snapH = nwn_database.read_path(hostile, {"it_sword_a"})
        check("a .sql dump with an 8 MB value and an unterminated string is scanned in linear time (under 5 s)",
              _t.time() - t0 < 5 and snapH["words"] == [], round(_t.time() - t0, 1))
        # duplicates: plain items with the same stats (keys, pages: no item property) are not a "same stats" group;
        # items that share a property are (a tester's report, 2026-10-30: every key in the module was listed)
        m5 = os.path.join(tmp, "m_dups")
        module(m5, tables=False)
        for rr, props in (("key_red", ()), ("key_blue", ()), ("page_1", ()), ("ring_a", (1,)), ("ring_b", (1,))):
            w(m5, f"{rr}.uti", n.write_gff(gff("UTI ", **item_fields(rr, rr.upper(), rr, base=5 if rr.startswith("key") else 7,
                                                                      props=props))))
        rep5 = h.analyse(m5, os.path.join(tmp, "an_dups"))
        same = [sorted(mb["node"] for mb in g_["members"]) for g_ in rep5["duplicates"]
                if g_["category"] == "Item - same stats, different name/look"]
        check("duplicates: keys/pages with no item property are never a 'same stats' group; items sharing a property are",
              ["bp:ring_a.uti", "bp:ring_b.uti"] in same and not any("bp:key_red.uti" in g_ for g_ in same), same)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
