"""
Extra override folders (owner, 2026-10-30: "could we stretch to look inside the override"): a copy of a server's override
folder read as one more source. A PW keeps loose 2da tables there (loot lists) that the server's scripts read; the items
they list must count as in use. Covered: nwn_analyse --override (saved for the analysis, so Analyse again reads it), the
Overview's list (/api/extraoverrides), a folder that has gone missing, how the folder is named where a file is in
several places, which of two override copies wins, and that builds never take anything from it.
    python tests/test_extra_override.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys

import nwnlib as n  # noqa: E402
import nwn_analyse  # noqa: E402
import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_index  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root as gff, loc, w, R, X, LST, item_fields, stand_in_ncs  # noqa: E402

A = "xovtest"
TABLE = "2DA V2.0\n\n   RESREF      STACK\n0  it_loot_a   1\n"


def module(folder):
    """A module whose load script reads the loot table loot_tbl.2da - which the module itself doesn't have."""
    os.makedirs(folder)
    w(folder, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("Xov"), Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []),
                                            Mod_OnModLoad=(R, "loot_drop"))))
    for rr in ("it_loot_a", "it_other"):
        w(folder, f"{rr}.uti", n.write_gff(gff("UTI ", **item_fields(rr, rr.upper(), rr))))
    w(folder, "itempalcus.itp", n.write_gff(gff("ITP ", MAIN=(LST, [st(0, NAME=(X, "Custom"), ID=(n.BYTE, 1))]))))
    w(folder, "loot_drop.nss", 'void main(){ string s = Get2DAString("loot_tbl", "RESREF", 0); '
                               'CreateItemOnObject(s, OBJECT_SELF); }\n')
    w(folder, "loot_drop.ncs", stand_in_ncs("loot_drop"))
    w(folder, "shared.2da", "2DA V2.0\n\n   A\n0  1\n")              # also in the server override: a conflict


def status(rep, node):
    d = {x["node"]: x for x in rep["deletions"]}
    return d[node]["status"] if node in d else "in use"


def run(argv):
    """nwn_analyse.main quietly; returns its exit code."""
    with contextlib.redirect_stdout(io.StringIO()):
        return nwn_analyse.main(argv) or 0


def main():
    with h.tempdir("nwn_xov_") as tmp:
        mod = os.path.join(tmp, "mod")
        module(mod)
        srv = os.path.join(tmp, "live-server", "override")       # the copy of the server's override folder
        os.makedirs(srv)
        w(srv, "loot_tbl.2da", TABLE)
        w(srv, "shared.2da", "2DA V2.0\n\n   A\n0  2\n")
        out = os.path.join(D.WORKSPACE, A)
        try:
            # 1. without the server's override the toolkit can't know the table: the item isn't in use
            check("cli: --override that doesn't exist is refused (exit 2)",
                  run([mod, "--name", A, "--override", os.path.join(tmp, "nope")]) == 2)
            rc = run([mod, "--name", A, "--no-json"])
            rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
            check("without the server override: the loot item is not counted as in use",
                  rc == 0 and status(rep, "bp:it_loot_a.uti") != "in use", status(rep, "bp:it_loot_a.uti"))
            # 2. --override: read, and saved for the analysis
            rc = run([mod, "--name", A, "--no-json", "--override", srv])
            rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
            check("--override: the table there keeps the item it lists in use; an unlisted one stays unused",
                  rc == 0 and status(rep, "bp:it_loot_a.uti") == "in use" and status(rep, "bp:it_other.uti") != "in use",
                  (status(rep, "bp:it_loot_a.uti"), status(rep, "bp:it_other.uti")))
            check("--override is saved for the analysis", nwn_index.load_extra_overrides(out) == [os.path.abspath(srv)])
            db = sqlite3.connect(os.path.join(out, "index.sqlite"))
            meta = json.loads(db.execute("SELECT value FROM meta WHERE key='extra_overrides'").fetchone()[0])
            conf = dict(db.execute("SELECT * FROM conflicts"))
            db.close()
            check("meta records the folders read", meta == [os.path.abspath(srv)], meta)
            check("a file in the module and the copy: named 'override:<folder above>/override', so two override folders "
                  "can be told apart", conf.get("shared.2da") == "module | override:live-server/override", conf)
            # 3. Analyse again (no --override): the saved folder is read again
            run([mod, "--name", A, "--no-json"])
            rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
            check("Analyse again reads the saved folder too", status(rep, "bp:it_loot_a.uti") == "in use")
            # 4. the Overview card: list, read status, refusals, remove
            v = D.mod_settings(A)["extra_overrides"]
            check("overview: the folder with its file and 2da counts, read by this analysis",
                  v == [dict(path=os.path.abspath(srv), exists=True, files=2, tables=2, read=True, capped=False)], v)
            try:
                D.set_extra_overrides(dict(a=A, folders=[srv, os.path.join(tmp, "not-there")]))
                refused = ""
            except ValueError as ex:
                refused = str(ex)
            check("overview: a path that is not a folder is refused, naming it (nothing saved)",
                  "not-there" in refused and nwn_index.load_extra_overrides(out) == [os.path.abspath(srv)], refused)
            other = os.path.join(tmp, "older-copy")
            os.makedirs(other)
            v = D.set_extra_overrides(dict(a=A, folders=[srv, other, srv]))["extra_overrides"]
            check("overview: a folder added is listed as 'Analyse again to read it'; repeats are dropped",
                  [(x["path"], x["read"]) for x in v] == [(os.path.abspath(srv), True), (os.path.abspath(other), False)], v)
            check("delete: extra_overrides.json is your own work (Clear keeps it)",
                  "extra_overrides.json" in D.USER_PARTS["descriptions"])
            # folders that must never be read as an override: the workspace, the module itself, a drive root
            for bad, why in ((D.WORKSPACE, "workspace"), (out, "workspace"), (mod, "module"), (tmp, "module"),
                             (os.path.abspath(os.sep), "drive")):
                try:
                    D.set_extra_overrides(dict(a=A, folders=[srv, other, bad]))
                    refused = ""
                except ValueError as ex:
                    refused = str(ex)
                check(f"overview: refuses {why} ({os.path.basename(bad) or bad}) with a reason",
                      why in refused and nwn_index.load_extra_overrides(out) == [os.path.abspath(srv), os.path.abspath(other)],
                      refused)
            # 5. a saved folder that has gone: a warning, and the rest still read
            os.rmdir(other)
            run([mod, "--name", A, "--no-json"])
            rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
            miss = [i for i in rep["issues"] if i["category"] == "override_folder_missing"]
            check("a saved folder that is gone: one warning naming it, and the other folder is still read",
                  len(miss) == 1 and "older-copy" in miss[0]["detail"] and status(rep, "bp:it_loot_a.uti") == "in use", miss)
            check("a missing issue says how to fix it", "remove" in (miss[0].get("fix") or ""), miss[0].get("fix"))
            third = os.path.join(tmp, "third")
            os.makedirs(third)
            v = D.set_extra_overrides(dict(a=A, folders=[srv, other, third]))["extra_overrides"]
            check("overview: a saved folder that has gone does not block adding another (it stays listed as not found)",
                  [(os.path.basename(x["path"]), x["exists"]) for x in v] == [("override", True), ("older-copy", False), ("third", True)], v)
            v = D.set_extra_overrides(dict(a=A, folders=[srv, other]))["extra_overrides"]
            check("overview: ... nor removing another", [os.path.basename(x["path"]) for x in v] == ["override", "older-copy"], v)
            D.set_extra_overrides(dict(a=A, folders=[srv]))
            # an archive left in the override copy (a server backup) is not read: the game doesn't load it either
            w(srv, "old_backup.hak", b"HAK V1.0" + b"\0" * 152)
            run([mod, "--name", A, "--no-json"])
            db = sqlite3.connect(os.path.join(out, "index.sqlite"))
            hak_rows = db.execute("SELECT COUNT(*) FROM files WHERE ext='hak'").fetchone()[0]
            db.close()
            check("a .hak inside the override copy is skipped (never read as loaded content)", hak_rows == 0, hak_rows)
            os.remove(os.path.join(srv, "old_backup.hak"))
            # 6. builds never take anything from it
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            clean = os.path.join(out, "build", "xov")
            check("build: nothing from the server override goes into the clean module; the audit passes",
                  not os.path.exists(os.path.join(clean, "loot_tbl.2da")) and
                  open(os.path.join(clean, "shared.2da")).read().strip().endswith("0  1") and
                  log["audit"]["verdict"].startswith("PASS"), (log["audit"]["verdict"], os.listdir(clean)))
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: the loot item, in use only through the override's table, is a note - not lost",
                  iu["status"] == "PASS" and any("in use only through override files" in e and "it_loot_a" in e
                                                 for e in iu["evidence"]), iu)
            # a real loss of such an item (gone from the clean module, not by the plan) still fails
            os.remove(os.path.join(clean, "it_loot_a.uti"))
            import nwn_audit
            with contextlib.redirect_stdout(io.StringIO()):
                a2 = nwn_audit.audit(out, os.path.join(out, "build"), "xov", compiler=h.NO_COMPILER, verbose=False)
            iu2 = {c["id"]: c for c in a2["checks"]}["in_use"]
            check("audit: the same item missing from the clean module is still lost (FAIL)",
                  iu2["status"] == "FAIL" and any("it_loot_a" in e and "no longer" in e for e in iu2["evidence"]), iu2)
            # the module has its own copy of the table and the server's loose copy lists one item more: that item is in
            # use only through the override (a note), and the item the module's copy lists is a real loss if removed
            w(mod, "it_loot_b.uti", n.write_gff(gff("UTI ", **item_fields("it_loot_b", "IT_LOOT_B", "it_loot_b"))))
            w(mod, "loot_tbl.2da", TABLE)
            w(srv, "loot_tbl.2da", TABLE + "1  it_loot_b   1\n")
            run([mod, "--name", A, "--no-json"])
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov2", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: a table in both the module and the override copy - the extra item the server's copy lists is a "
                  "note, not a false FAIL (reviewer's probe)",
                  iu["status"] == "PASS" and any("it_loot_b" in e and "through override" in e for e in iu["evidence"]), iu)
            os.remove(os.path.join(out, "build", "xov2", "it_loot_a.uti"))
            with contextlib.redirect_stdout(io.StringIO()):
                a3 = nwn_audit.audit(out, os.path.join(out, "build"), "xov2", compiler=h.NO_COMPILER, verbose=False)
            iu3 = {c["id"]: c for c in a3["checks"]}["in_use"]
            check("audit: the item the module's own copy lists is still a loss when it goes missing",
                  iu3["status"] == "FAIL" and any("it_loot_a" in e and "no longer" in e for e in iu3["evidence"]), iu3)
            # an adversarial test (2026-10-30): the module's AND the server's copy both list it_loot_b; an edit drops it
            # from the module's table. The reference is gone from the clean module, so the audit must FAIL - not call
            # the item "in use only through override files"
            import nwn_edit
            w(mod, "loot_tbl.2da", TABLE + "1  it_loot_b   1\n")
            run([mod, "--name", A, "--no-json"])
            nwn_edit.save_text(out, "loot_tbl.2da", TABLE)
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov3", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: an edit that drops a reference from a table the override also holds is a FAIL, not a note",
                  iu["status"] == "FAIL" and any("it_loot_b" in e and "no longer" in e for e in iu["evidence"]), iu)
            shutil.rmtree(os.path.join(out, "edits"), ignore_errors=True)
            # second adversarial test: the reference is a tag in a script the override also holds (no file name to
            # back it up); the edit removes the lookup -> FAIL. And a tag only the override copy of an item carries
            # must not make the module's script of that name "lost" on an honest build (tag-based scripts).
            w(mod, "it_loot_b.uti", n.write_gff(gff("UTI ", **item_fields("it_loot_b", "LOOTB_TAG", "it_loot_b"))))
            w(mod, "loot_drop.nss", 'void main(){ string s = Get2DAString("loot_tbl", "RESREF", 0); '
                                    'CreateItemOnObject(s, OBJECT_SELF); GetObjectByTag("LOOTB_TAG"); }\n')
            w(srv, "loot_drop.nss", open(os.path.join(mod, "loot_drop.nss")).read())
            w(mod, "loot_tbl.2da", TABLE)
            w(srv, "loot_tbl.2da", TABLE)
            run([mod, "--name", A, "--no-json"])
            nwn_edit.save_text(out, "loot_drop.nss", 'void main(){ string s = Get2DAString("loot_tbl", "RESREF", 0); '
                                                     'CreateItemOnObject(s, OBJECT_SELF); }\n')
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov4", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: an edit that drops a tag lookup from a script the override also holds is a FAIL",
                  iu["status"] == "FAIL" and any("it_loot_b" in e and "no longer" in e for e in iu["evidence"]), iu)
            shutil.rmtree(os.path.join(out, "edits"), ignore_errors=True)
            w(mod, "it_x.uti", n.write_gff(gff("UTI ", **item_fields("it_x", "foo_tag", "it_x"))))
            w(srv, "it_x.uti", n.write_gff(gff("UTI ", **item_fields("it_x", "bar_tag", "it_x"))))
            w(mod, "bar_tag.nss", "void main(){ }\n")
            w(mod, "bar_tag.ncs", stand_in_ncs("bar_tag"))
            w(mod, "loot_tbl.2da", TABLE + "1  it_x        1\n")       # it_x is in use (the module's table lists it)
            w(srv, "loot_tbl.2da", TABLE + "1  it_x        1\n")
            run([mod, "--name", A, "--no-json"])
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov5", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: a tag only the override's copy of an item carries does not fail an honest build (its "
                  "tag-based script is a note)",
                  iu["status"] == "PASS" and any("bar_tag" in e and "through override" in e for e in iu["evidence"]), iu)
            # third adversarial test: things only the server's copies refer to that are no file - a tag only the
            # server's item carries while a module script looks it up, and an NPC the server's copy of an area adds -
            # must not fail an honest build (the module.ifo lists the area so the server's a1.git copy is read)
            w(mod, "a1.are", n.write_gff(gff("ARE ", Name=loc("A1"), Tag=(X, "A1"), ResRef=(R, "a1"), Tileset=(R, "tcn01"))))
            npc = lambda t: st(4, TemplateResRef=(R, t), Tag=(X, t.upper()), XPosition=(n.FLOAT, 1.0),  # noqa: E731
                               YPosition=(n.FLOAT, 1.0), ZPosition=(n.FLOAT, 0.0))
            w(mod, "a1.git", n.write_gff(gff("GIT ", **{"Creature List": (LST, [npc("mod_npc")])})))
            w(srv, "a1.git", n.write_gff(gff("GIT ", **{"Creature List": (LST, [npc("mod_npc"), npc("mod_npc")])})))
            w(mod, "mod_npc.utc", n.write_gff(gff("UTC ", TemplateResRef=(R, "mod_npc"), Tag=(X, "MOD_NPC"), FirstName=loc("npc"))))
            w(mod, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("Xov"), Mod_Entry_Area=(R, "a1"),
                                                   Mod_Area_list=(LST, [st(6, Area_Name=(R, "a1"))]), Mod_OnModLoad=(R, "loot_drop"))))
            w(mod, "loot_drop.nss", 'void main(){ string s = Get2DAString("loot_tbl", "RESREF", 0); '
                                    'CreateItemOnObject(s, OBJECT_SELF); GetObjectByTag("SRV_ONLY_TAG"); }\n')
            w(srv, "loot_drop.nss", open(os.path.join(mod, "loot_drop.nss")).read())
            w(srv, "it_x.uti", n.write_gff(gff("UTI ", **item_fields("it_x", "SRV_ONLY_TAG", "it_x"))))
            run([mod, "--name", A, "--no-json"])
            log = nwn_build.build(out, dict(delete=[], merge=[]), name="xov6", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
            bad = [(c["id"], c["evidence"][:2]) for c in log["audit"]["checks"] if c["status"] == "FAIL"]
            iu = {c["id"]: c for c in log["audit"]["checks"]}["in_use"]
            check("audit: a tag only the server's item carries, and an NPC only the server's area copy places, pass an "
                  "honest build (areas, placements and in_use)",
                  not bad and iu["status"] == "PASS", (bad, iu["evidence"]))
            # the same analysis built in checkpointed steps attributes the same (no PASS/FAIL that depends on how it ran)
            for _ in range(12):
                rc = run([mod, "--name", A, "--no-json", "--budget", "1"])
                if rc == 0:
                    break
            db = sqlite3.connect(os.path.join(out, "index.sqlite"))
            pairs = set(db.execute("SELECT src, dst FROM override_edges"))
            db.close()
            check("resumed run: the override-only references are the same as in one go",
                  rc == 0 and ("tag:SRV_ONLY_TAG", "bp:it_x.uti") in pairs and ("tag:MOD_NPC", "inst:a1:Creature List[1]") in pairs
                  and ("script:loot_drop", "tag:SRV_ONLY_TAG") not in pairs and ("area:a1", "inst:a1:Creature List[0]") not in pairs,
                  (rc, sorted(pairs)[:6]))
        finally:
            D.release_analysis(A)
            shutil.rmtree(out, ignore_errors=True)
        # 7. the server's copy is read before the user folder's own override (its file wins between the two)
        user = os.path.join(tmp, "user")
        os.makedirs(os.path.join(user, "override"))
        os.makedirs(os.path.join(user, "modules"))
        w(os.path.join(user, "override"), "loot_tbl.2da", TABLE.replace("it_loot_a", "it_other "))
        out2 = os.path.join(tmp, "an2")
        h.analyse(mod, out2, nwn_user=user, extra_overrides=[srv])
        db = sqlite3.connect(os.path.join(out2, "index.sqlite"))
        prio = {os.path.basename(os.path.dirname(p_)): pr for p_, pr in
                db.execute("SELECT path, priority FROM sources WHERE kind='override'")}
        db.close()
        check("two override folders: the server's copy is read first (wins), the user folder's own after it",
              prio.get("live-server", 999) < prio.get("user", -1), prio)
    page = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the Overview card with the steps, Add, remove and Analyse again",
          'id="xovr"' in page and "Server override folders" in page and '"/api/extraoverrides"' in page and
          "press Analyse again to read it" in page and 'id="xoAdd"' in page)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
