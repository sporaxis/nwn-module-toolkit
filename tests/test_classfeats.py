"""
Tests for the classes' feat table checks (nwn_classfeats; a tester's report, 2026-10-27: "check cls_feat_*.2da
validity - **** lines, feats that don't exist, are deleted or have no NAME tlk reference ... cause random errors during
levelling up, typically at 3rd level of the class"):
  - a custom feat table: an empty row (FeatIndex ****, only a note: disputed by another tester, 2026-10-28), a feat past the end of feat.2da, a blank (deleted) feat row,
    a feat with no name, a feat whose name is not in dialog.tlk - one issue per kind, naming the rows;
  - a custom classes.2da naming a feat table nobody has (error; a note while the base game isn't read);
  - the game's own tables against the game's own feat.2da are not judged, but are when a hak changes feat.2da (and
    even then an empty row in the game's table is not ours to report);
  - every issue has a fix; the analysis is marked class_feats (no "Analyse again" for it);
  - 2da row labels: no warning where the game's own copy has the same label, or for a bonus-feat table numbered from 1.
    python tests/test_classfeats.py      (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import sys

import nwnlib as n  # noqa: E402
import nwn_update  # noqa: E402
from _harness import check  # noqa: E402
from _gff import st, root, loc, R, X, LST  # noqa: E402

# feat.2da of the fake game: row 2 is blank (a deleted feat), row 3 has no name, row 1's name 9999 is not in dialog.tlk
FEAT_BASE = ("2DA V2.0\n\n   LABEL        FEAT\n0  Alertness    100\n1  Ambidex      9999\n2  ****         ****\n"
             "3  NoName       ****\n4  Toughness    101\n")
CLASSES_BASE = ("2DA V2.0\n\n   Label    FeatsTable\n0  Fighter  CLS_FEAT_FIGHT\n1  Wizard   CLS_FEAT_WIZ\n")
# the game's fighter table has an empty row: Beamdog's own data is not judged against Beamdog's own feat.2da
CLS_FIGHT = "2DA V2.0\n\n   FeatLabel  FeatIndex  List\n0  Alertness  0          0\n1  ****       ****       ****\n"
CLS_WIZ = "2DA V2.0\n\n   FeatLabel  FeatIndex  List\n0  Toughness  4          0\n"
# the hak: two more classes - Psion (a bad feat table) and Ghost (a feat table nobody has)
CLASSES_HAK = CLASSES_BASE + "2  Psion    cls_feat_psi\n3  Ghost    cls_feat_none\n"
CLS_PSI = ("2DA V2.0\n\n   FeatLabel  FeatIndex  List\n0  Alertness  0          0\n1  ****       ****       ****\n"
           "2  Gone       99         0\n3  Deleted    2          0\n4  NoName     3          0\n5  Ambidex    1          0\n")


def fixture(tmp):
    """(module folder, hak path, hak with a changed feat.2da, game folder)."""
    mod = os.path.join(tmp, "mod"); hakd = os.path.join(tmp, "hakfiles"); featd = os.path.join(tmp, "featfiles")
    groot = os.path.join(tmp, "game")
    for d in (mod, hakd, featd):
        os.makedirs(d)
    n.write_key_bif(groot, {("classes", "2da"): CLASSES_BASE.encode(), ("feat", "2da"): FEAT_BASE.encode(),
                            ("cls_feat_fight", "2da"): CLS_FIGHT.encode(), ("cls_feat_wiz", "2da"): CLS_WIZ.encode()})
    os.makedirs(os.path.join(groot, "lang", "en", "data"))
    n.write_tlk(os.path.join(groot, "lang", "en", "data", "dialog.tlk"), {100: "Alertness", 101: "Toughness"})
    for name, text in (("classes", CLASSES_HAK), ("cls_feat_psi", CLS_PSI)):
        with open(os.path.join(hakd, name + ".2da"), "w", encoding="utf-8") as fh:
            fh.write(text)
    hak = os.path.join(tmp, "classes.hak")
    h.pack_hak(hakd, hak)
    # a second hak that blanks feat 0 (Alertness): the game's own fighter table now points at a deleted feat
    with open(os.path.join(featd, "feat.2da"), "w", encoding="utf-8") as fh:
        fh.write(FEAT_BASE.replace("0  Alertness    100", "0  ****         ****"))
    feathak = os.path.join(tmp, "feats.hak")
    h.pack_hak(featd, feathak)
    g = lambda fn, r: open(os.path.join(mod, fn), "wb").write(n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Class test"), Mod_Entry_Area=(R, "start"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "start"))]),
                         Mod_HakList=(LST, [st(8, Mod_Hak=(X, "classes"))])))
    g("start.are", root("ARE ", Name=loc("Start"), Tag=(X, "START"), ResRef=(R, "start"), Tileset=(R, "tcn01")))
    g("start.git", root("GIT "))
    return mod, hak, feathak, groot


def by(rep, cat):
    return [i for i in rep["issues"] if i["category"] == cat]


def checks(tmp):
    mod, hak, feathak, groot = fixture(tmp)
    rep = h.analyse(mod, os.path.join(tmp, "an"), haks=[hak], nwn_root=groot)
    nl = by(rep, "class_feat_null")
    # a note, not a warning: testers disagree whether empty rows break level-up (2026-10-28)
    check("null: an empty row (FeatIndex ****) in the hak's feat table is a note naming the row and the class",
          len(nl) == 1 and nl[0]["severity"] == "info" and nl[0]["node"] == "2da:cls_feat_psi"
          and "row 1" in nl[0]["detail"] and "Psion" in nl[0]["detail"] and "classes.hak" in nl[0]["detail"], nl)
    ms = by(rep, "class_feat_missing")
    check("missing: a feat past the end of feat.2da and a blank (deleted) feat row are one warning naming both rows",
          len(ms) == 1 and ms[0]["severity"] == "warning" and "row 2 (feat 99: feat.2da has 5 rows)" in ms[0]["detail"]
          and "radial menu" in ms[0]["detail"]
          and "row 3 (feat 2 is a blank row in feat.2da - deleted)" in ms[0]["detail"], ms)
    nn = by(rep, "class_feat_no_name")
    check("no name: a feat with FEAT **** and one whose name is not in dialog.tlk, one warning naming both",
          len(nn) == 1 and nn[0]["severity"] == "warning" and "row 4 (feat 3 NoName: no name)" in nn[0]["detail"]
          and "row 5 (feat 1 Ambidex: name 9999 not in the dialog.tlk)" in nn[0]["detail"], nn)
    tm = by(rep, "class_feat_table_missing")
    check("table missing: a class in the hak's classes.2da whose feat table nobody has is an error on classes.2da",
          len(tm) == 1 and tm[0]["severity"] == "error" and tm[0]["node"] == "2da:classes"
          and "cls_feat_none" in tm[0]["detail"] and "Ghost" in tm[0]["detail"], tm)
    allcf = nl + ms + nn + tm
    check("scope: the game's own tables against the game's own feat.2da are not judged (fighter's empty row)",
          not [i for i in allcf if "cls_feat_fight" in i["node"] + i["detail"]], allcf)
    check("fix: every class feat issue says how to fix it", allcf and all(i.get("fix") for i in allcf))
    check("report: class_feats marks the analysis and the summary counts the tables",
          rep.get("class_feats") == 1 and not [x for x in nwn_update.missing_features(rep) if x["key"] == "class_feats"]
          and rep.get("class_feats_summary", {}).get("tables") == 4, rep.get("class_feats_summary"))

    # a hak that changes feat.2da: the game's tables are checked against it - but their empty rows stay Beamdog's
    rep2 = h.analyse(mod, os.path.join(tmp, "an2"), haks=[feathak, hak], nwn_root=groot)
    fight = [i for i in by(rep2, "class_feat_missing") if i["node"] == "2da:cls_feat_fight"]
    check("feat.2da changed by a hak: the game's fighter table pointing at the feat it blanked is reported",
          len(fight) == 1 and "row 0 (feat 0 is a blank row" in fight[0]["detail"] and "feats.hak" in fight[0]["detail"], fight)
    check("feat.2da changed by a hak: the game's own empty row is still not reported",
          not [i for i in by(rep2, "class_feat_null") if i["node"] == "2da:cls_feat_fight"])

    # the base game not read: the missing table may be in the game - a note that says so
    rep3 = h.analyse(mod, os.path.join(tmp, "an3"), haks=[hak])
    tm3 = by(rep3, "class_feat_table_missing")
    check("no base game: a feat table nobody has is a note that says the base game was not read",
          tm3 and all(i["severity"] == "info" and "base game was not read" in i["detail"] for i in tm3), tm3)
    check("no base game: the hak's own empty row is still reported (it is in the hak, whatever the game has)",
          [i["severity"] for i in by(rep3, "class_feat_null")] == ["info"], by(rep3, "class_feat_null"))


def labels(tmp):
    """2da_row_label (a tester's report, 2026-10-28: "this is vanilla behavior"): labels the game's own copy of a 2da
    has at the same line are not reported, nor a bonus-feat table (cls_bfeat_*) numbered 1, 2, 3...; other drift is."""
    t2 = lambda labs, col="Bonus": "2DA V2.0\n\n   " + col + "\n" + "".join(f"{l}  {i}\n" for i, l in enumerate(labs))  # noqa: E731
    groot = os.path.join(tmp, "game2"); mod = os.path.join(tmp, "mod2")
    os.makedirs(mod)
    # the game's own tables: a bonus-feat table counting from 1, and a table with one drifting label (line 2 = '5')
    n.write_key_bif(groot, {("cls_bfeat_shiftr", "2da"): t2(["1", "2", "3"]).encode(),
                            ("appearance", "2da"): t2(["0", "1", "5"], "Label").encode()})
    for fn, txt in (("cls_bfeat_shiftr.2da", t2(["1", "2", "3"])),          # the game's copy, unchanged
                    ("cls_bfeat_psi.2da", t2(["1", "2", "3", "4"])),        # custom, numbered from 1
                    ("appearance.2da", t2(["0", "1", "5", "7"], "Label")),  # line 2 as the game has it; line 3 drifts
                    ("drift.2da", t2(["0", "2", "2"]))):                    # custom, hand-merged
        with open(os.path.join(mod, fn), "w", encoding="utf-8") as fh:
            fh.write(txt)
    g = lambda fn, r: open(os.path.join(mod, fn), "wb").write(n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Label test"), Mod_Entry_Area=(R, "start"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "start"))])))
    g("start.are", root("ARE ", Name=loc("Start"), Tag=(X, "START"), ResRef=(R, "start"), Tileset=(R, "tcn01")))
    g("start.git", root("GIT "))
    rep = h.analyse(mod, os.path.join(tmp, "an_labels"), nwn_root=groot)
    lab = {i["node"]: i["detail"] for i in by(rep, "2da_row_label")}
    check("labels: a bonus-feat table numbered as the game numbers its own is not reported (vanilla behaviour)",
          "2da:cls_bfeat_shiftr" not in lab, lab)
    check("labels: a custom bonus-feat table numbered 1, 2, 3... is not reported (the builders' convention)",
          "2da:cls_bfeat_psi" not in lab, lab)
    check("labels: a label the game's own copy has at that line is left out; the line that drifts is reported",
          "line 3 labelled '7'" in lab.get("2da:appearance", "") and "line 2" not in lab.get("2da:appearance", ""), lab)
    check("labels: a hand-merged custom table is still reported", "line 1 labelled '2'" in lab.get("2da:drift", ""), lab)


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    with h.tempdir() as t:
        h.run(checks, t)
        h.run(labels, t)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
