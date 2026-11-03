"""
A tester's reports on 1.5.9 (2026-11-03):
1. "Would be awesome if this toolkit could detect when new tags are being applied to existing items":
   CreateItemOnObject(resref, target, stack, "skarn_spine") gives the new item that tag, and SetTag / CopyObject /
   CreateObject do the same for other objects. A lookup of such a tag is a note naming the script, not a warning -
   and the Issues page agrees with Variables & tags (they disagreed: "runtime" there, unknown_tag warning here).
2. "These are all vanilla portraits that were removed or never implemented": portraits.2da rows identical to the
   game's own row at that line are the game's, not reported; only rows the content added or changed are, and only
   in the copy of portraits.2da the game uses (the first-listed hak's).
    python tests/test_testers_1510.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os

import nwnlib as n  # noqa: E402
from _harness import check  # noqa: E402
from _gff import root, st, loc, w, R, X, LST, item_fields, stand_in_ncs  # noqa: E402

# the game's own portraits.2da: rows for portraits it never shipped (ghost_, xaribethe_) and one it did (hu_m_01_)
GAME_PORTRAITS = "2DA V2.0\n\n   BaseResRef\n0  hu_m_01_\n1  ghost_\n2  xaribethe_\n"


def tag_case(tmp):
    mod = os.path.join(tmp, "tagmod")
    os.makedirs(mod)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Tags"), Mod_Entry_Area=(R, "a1"),
                                          Mod_Area_list=(LST, []), Mod_OnModLoad=(R, "race_skarn"))))
    w(mod, "nw_wswss001.uti", n.write_gff(root("UTI ", **item_fields("nw_wswss001", "NW_WSWSS001", "Sickle"))))
    w(mod, "race_skarn.nss",
      'void main()\n{\n    object oItem = CreateItemOnObject("nw_wswss001", OBJECT_SELF, 1, "skarn_spine");\n'
      '    object oHas = GetItemPossessedBy(OBJECT_SELF, "skarn_spine");\n'
      '    SetTag(OBJECT_SELF, "my_new_tag");\n    object o2 = GetObjectByTag("my_new_tag");\n'
      '    object o3 = GetObjectByTag("nobody_has_this");\n}\n')
    w(mod, "race_skarn.ncs", stand_in_ncs("race_skarn"))
    rep = h.analyse(mod, os.path.join(tmp, "tagmod_an"))
    ut = {i["node"]: i for i in rep["issues"] if i["category"] == "unknown_tag"}
    va = {t["tag"]: t for t in rep["var_audit"]["tags"]}
    check("CreateItemOnObject's 4th argument is a new tag: Variables & tags says run time, naming the script",
          va.get("skarn_spine", {}).get("status") == "runtime" and "race_skarn" in va["skarn_spine"]["detail"],
          va.get("skarn_spine"))
    check("its lookup is a note naming the script on the Issues page, not a warning",
          ut.get("tag:skarn_spine", {}).get("severity") == "info"
          and "given to an object at run time by race_skarn" in ut["tag:skarn_spine"]["detail"], ut.get("tag:skarn_spine"))
    check("a tag set with SetTag: a note too (the Issues page now agrees with Variables & tags)",
          ut.get("tag:my_new_tag", {}).get("severity") == "info", ut.get("tag:my_new_tag"))
    check("a tag nothing gives any object is still a warning",
          ut.get("tag:nobody_has_this", {}).get("severity") == "warning", ut.get("tag:nobody_has_this"))
    # review findings: a giver nothing runs doesn't count; constants resolve per script; more tag-giving functions;
    # a 1.5.9 "by design" tick still matches
    mod2 = os.path.join(tmp, "tagmod2")
    os.makedirs(mod2)
    w(mod2, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Tags2"), Mod_Entry_Area=(R, "a1"),
                                           Mod_Area_list=(LST, []), Mod_OnModLoad=(R, "s_live"))))
    w(mod2, "inc_a.nss", 'const string T = "tag_a";\n')
    w(mod2, "inc_b.nss", 'const string T = "tag_b";\n')
    w(mod2, "s_x1.nss", '#include "inc_a"\nvoid main(){ string s = T; }\n')
    w(mod2, "s_x2.nss", '#include "inc_a"\nvoid main(){ string s = T; }\n')
    w(mod2, "s_live.nss", '#include "inc_b"\nvoid main()\n{\n    SetTag(OBJECT_SELF, T);\n'
                          '    object a = GetObjectByTag("tag_b");\n    object b = GetObjectByTag("deadtag");\n'
                          '    object c = GetObjectByTag("traptag");\n    object d = GetObjectByTag("areatag");\n'
                          '    object t = CreateTrapAtLocation(1, GetLocation(OBJECT_SELF), 2.0, "traptag");\n'
                          '    object ar = CreateArea("a1", "areatag");\n}\n')
    w(mod2, "old_unused.nss", 'void main(){ SetTag(OBJECT_SELF, "deadtag"); }\n')
    for sc in ("s_live", "s_x1", "s_x2", "old_unused"):
        w(mod2, sc + ".ncs", stand_in_ncs(sc))
    rep2 = h.analyse(mod2, os.path.join(tmp, "tagmod2_an"))
    ut2 = {i["node"]: i for i in rep2["issues"] if i["category"] == "unknown_tag"}
    va2 = {t["tag"]: t for t in rep2["var_audit"]["tags"]}
    check("a tag only a script nothing runs gives stays a warning, and says which script",
          ut2.get("tag:deadtag", {}).get("severity") == "warning" and "old_unused" in ut2["tag:deadtag"]["detail"]
          and va2.get("deadtag", {}).get("status") == "runtime_unused", (ut2.get("tag:deadtag"), va2.get("deadtag")))
    check("a constant resolves to the definition the giving script includes (inc_b's tag_b, not inc_a's)",
          va2.get("tag_b", {}).get("status") == "runtime"
          and ut2.get("tag:tag_b", {}).get("severity", "info") == "info", (va2.get("tag_b"), ut2.get("tag:tag_b")))
    check("CreateTrapAtLocation's and CreateArea's tag arguments count as run-time tags",
          va2.get("traptag", {}).get("status") == "runtime" and va2.get("areatag", {}).get("status") == "runtime",
          (va2.get("traptag"), va2.get("areatag")))
    # second pass: a giver Safe to delete keeps as Review (here: run through a name built at run time) may run - a
    # note saying so, not "nothing runs it"
    mod3 = os.path.join(tmp, "tagmod3")
    os.makedirs(mod3)
    w(mod3, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Tags3"), Mod_Entry_Area=(R, "a1"),
                                           Mod_Area_list=(LST, []), Mod_OnModLoad=(R, "s_load"))))
    w(mod3, "s_load.nss", 'void main()\n{\n    string sWhat = "spine";\n    ExecuteScript("give_" + sWhat, OBJECT_SELF);\n'
                          '    object o = GetItemPossessedBy(OBJECT_SELF, "skarn_spine");\n}\n')
    w(mod3, "give_spine.nss", 'void main(){ CreateItemOnObject("nw_wswss001", OBJECT_SELF, 1, "skarn_spine"); }\n')
    for sc in ("s_load", "give_spine"):
        w(mod3, sc + ".ncs", stand_in_ncs(sc))
    rep3 = h.analyse(mod3, os.path.join(tmp, "tagmod3_an"))
    u3 = next((i for i in rep3["issues"] if i["node"] == "tag:skarn_spine"), {})
    check("a giver kept as Review (run by a name built at run time) counts: a note that says it may run",
          u3.get("severity") == "info" and "give_spine (may run" in u3.get("detail", ""), u3)
    import nwn_sheets
    old_entry = {"sig": "Looked up by tag 'skarn_spine' but no object in the module has that tag (fine if created at "
                        "runtime)", "note": ""}
    check("an unknown_tag accepted as by design in 1.5.9 stays accepted (the note keeps the issue's signature)",
          nwn_sheets.accepted_match(old_entry, ut["tag:skarn_spine"]["detail"]) is not None,
          ut["tag:skarn_spine"]["detail"])
    import nwn_fixes
    fx = nwn_fixes.fix_for(ut["tag:skarn_spine"]) if hasattr(nwn_fixes, "fix_for") else ut["tag:skarn_spine"].get("fix", "")
    check("the note's fix text talks about the order of events, not about creating the object",
          "order of events" in (fx or ""), fx)


def portrait_case(tmp):
    groot = os.path.join(tmp, "game")
    n.write_key_bif(groot, {("portraits", "2da"): GAME_PORTRAITS.encode(),
                            ("po_hu_m_01_m", "tga"): b"\0" * 32})
    mod = os.path.join(tmp, "pmod")
    hakd, hak2d = os.path.join(tmp, "phak"), os.path.join(tmp, "phak2")
    for d in (mod, hakd, hak2d):
        os.makedirs(d)
    # the first-listed hak copies the game's table, adds two rows (one with pictures, one without) and changes a row
    w(hakd, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  hu_m_01_\n1  ghost_\n2  zz_changed_\n3  my_new_\n4  my_ok_\n")
    w(hakd, "po_my_ok_m.tga", b"\0" * 32)
    # a hak listed later has its own copy with a missing row: the game never reads it
    w(hak2d, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  hidden_row_\n")
    h1, h2 = os.path.join(tmp, "port.hak"), os.path.join(tmp, "port2.hak")
    h.pack_hak(hakd, h1)
    h.pack_hak(hak2d, h2)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Portraits"), Mod_Entry_Area=(R, "a1"),
                                          Mod_Area_list=(LST, []),
                                          Mod_HakList=(LST, [st(8, Mod_Hak=(X, "port")), st(8, Mod_Hak=(X, "port2"))]))))
    rep = h.analyse(mod, os.path.join(tmp, "pmod_an"), haks=[h1, h2], nwn_root=groot)
    pf = [i for i in rep["issues"] if i["category"] == "portrait_files"]
    det = pf[0]["detail"] if pf else ""
    check("portraits: rows identical to the game's own (ghost_ and xaribethe_ never shipped) are not reported",
          "ghost_" not in det and "xaribethe_" not in det, det)
    check("portraits: a row the content added, and one it changed, with no pictures are reported (2 rows)",
          len(pf) == 1 and det.startswith("2 portrait row(s)") and "my_new_" in det and "zz_changed_" in det, det)
    check("portraits: only the copy the game uses is read (a later hak's copy is ignored)", "hidden_row_" not in det,
          det)
    # a copy that inserts a row shifts the game's rows: still the game's (judged by value, not position)
    w(hakd, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  inserted_\n1  hu_m_01_\n2  ghost_\n3  xaribethe_\n")
    h.pack_hak(hakd, h1)
    rep3 = h.analyse(mod, os.path.join(tmp, "pmod_an3"), haks=[h1, h2], nwn_root=groot)
    d3 = next((i["detail"] for i in rep3["issues"] if i["category"] == "portrait_files"), "")
    check("portraits: a copy that inserts a row - the shifted game rows are still the game's; only the new row shows",
          d3.startswith("1 portrait row(s)") and "inserted_" in d3, d3)
    # the first-listed hak is missing: a note, saying the pictures or the table may be there
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Portraits"), Mod_Entry_Area=(R, "a1"),
                                          Mod_Area_list=(LST, []),
                                          Mod_HakList=(LST, [st(8, Mod_Hak=(X, "absent")), st(8, Mod_Hak=(X, "port"))]))))
    rep4 = h.analyse(mod, os.path.join(tmp, "pmod_an4"), haks=[h1], nwn_root=groot)
    p4 = [i for i in rep4["issues"] if i["category"] == "portrait_files"]
    check("portraits: with a hak not loaded it is a note that says so", p4 and p4[0]["severity"] == "info"
          and "not loaded" in p4[0]["detail"], p4)
    # a portraits.2da only in an override folder (the builder's or a server's) is not what players get: not judged
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Portraits"), Mod_Entry_Area=(R, "a1"),
                                          Mod_Area_list=(LST, []))))
    ov = os.path.join(tmp, "srv", "override")
    os.makedirs(ov)
    w(ov, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  srv_new_\n")
    rep5 = h.analyse(mod, os.path.join(tmp, "pmod_an5"), nwn_root=groot, extra_overrides=[ov])
    check("portraits: a copy only in an override folder is not judged (players don't get it)",
          not [i for i in rep5["issues"] if i["category"] == "portrait_files"])
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Portraits"), Mod_Entry_Area=(R, "a1"),
                                          Mod_Area_list=(LST, []),
                                          Mod_HakList=(LST, [st(8, Mod_Hak=(X, "port")), st(8, Mod_Hak=(X, "port2"))]))))
    w(hakd, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  hu_m_01_\n1  ghost_\n2  zz_changed_\n3  my_new_\n"
                             "4  my_ok_\n")
    h.pack_hak(hakd, h1)
    # without the game folder there is nothing to compare with: every row of the winning copy is judged, as before
    rep2 = h.analyse(mod, os.path.join(tmp, "pmod_an2"), haks=[h1, h2])
    pf2 = [i for i in rep2["issues"] if i["category"] == "portrait_files"]
    check("portraits: without the game folder every row of that copy is judged (ghost_ then shows)",
          pf2 and "ghost_" in pf2[0]["detail"] and "hidden_row_" not in pf2[0]["detail"]
          and "couldn't be read" in pf2[0]["detail"], pf2)


def audit_cases(tmp):
    """The build audit: a finding that gets worse (a note becomes a warning) or bigger (more portrait rows) is new."""
    import contextlib
    import io
    import nwn_build
    mod = os.path.join(tmp, "amod")
    hakd = os.path.join(tmp, "ahak")
    os.makedirs(mod)
    os.makedirs(hakd)
    w(hakd, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  my_new_\n")
    hk = os.path.join(tmp, "ahak.hak")
    h.pack_hak(hakd, hk)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Audit"), Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []),
                                          Mod_OnModLoad=(R, "s_give"), Mod_HakList=(LST, [st(8, Mod_Hak=(X, "ahak"))]))))
    w(mod, "nw_wswss001.uti", n.write_gff(root("UTI ", **item_fields("nw_wswss001", "NW_WSWSS001", "Sickle"))))
    w(mod, "s_give.nss", 'void main()\n{\n    CreateItemOnObject("nw_wswss001", OBJECT_SELF, 1, "skarn_spine");\n'
                         '    object o = GetItemPossessedBy(OBJECT_SELF, "skarn_spine");\n}\n')
    w(mod, "s_give.ncs", stand_in_ncs("s_give"))
    w(mod, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  x\n")
    out = os.path.join(tmp, "amod_an")
    h.analyse(mod, out, haks=[hk])
    ed = os.path.join(out, "edits")
    os.makedirs(ed, exist_ok=True)
    # an edit drops the tag the script gave: the lookup's note becomes a warning; the hak's table isn't edited, but a
    # module-level edit of portraits.2da can't change the winning hak copy - so the portrait case edits the hak table
    # through a second analysis below
    w(ed, "s_give.nss", 'void main()\n{\n    CreateItemOnObject("nw_wswss001", OBJECT_SELF, 1);\n'
                        '    object o = GetItemPossessedBy(OBJECT_SELF, "skarn_spine");\n}\n')
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, dict(delete=[], merge=[]), name="ac", verbose=False,
                              audit_kwargs=dict(compiler=h.NO_COMPILER))
    refs = {c["id"]: c for c in log["audit"]["checks"]}["references"]
    check("audit: an edit that stops giving a tag makes its lookup's note a warning - reported as NEW",
          refs["status"] != "PASS" and any("skarn_spine" in e for e in refs["evidence"]), refs)
    # portraits: an edit of the module's own table when no hak has one adds rows without pictures - NEW
    mod2 = os.path.join(tmp, "amod2")
    os.makedirs(mod2)
    w(mod2, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("Audit2"), Mod_Entry_Area=(R, "a1"),
                                           Mod_Area_list=(LST, []))))
    w(mod2, "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  one_\n")
    out2 = os.path.join(tmp, "amod2_an")
    h.analyse(mod2, out2)
    os.makedirs(os.path.join(out2, "edits"), exist_ok=True)
    w(os.path.join(out2, "edits"), "portraits.2da", "2DA V2.0\n\n   BaseResRef\n0  one_\n1  two_\n2  three_\n")
    with contextlib.redirect_stdout(io.StringIO()):
        log2 = nwn_build.build(out2, dict(delete=[], merge=[]), name="ac2", verbose=False,
                               audit_kwargs=dict(compiler=h.NO_COMPILER))
    r2 = {c["id"]: c for c in log2["audit"]["checks"]}["references"]
    check("audit: an edit adding portrait rows without pictures is NEW (the count grew)",
          r2["status"] != "PASS" and any("portrait row" in e for e in r2["evidence"]), r2)


def main():
    with h.tempdir("nwn_t1510_") as tmp:
        tag_case(tmp)
        portrait_case(tmp)
        audit_cases(tmp)
    return h.summary()


if __name__ == "__main__":
    raise SystemExit(main())
