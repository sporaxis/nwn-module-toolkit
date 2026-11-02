"""
Text fields holding both a talk-table number and their own text (a tester's report, 2026-11-02, nwn-issues #370):
the game shows the embedded text to players whose language has it and the talk-table line to everyone else. Same
text as the line -> remove the text; different -> set the number to -1. Covered: same and different text, white space
and line ends not counting as a difference, a string in another language than the talk table (not judged), a line
the table lacks (broken_strref's business, not this), one note per file plus one warning for the module, hak files
not reported, and an index made before the table existed.
    python tests/test_locstrings.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import sqlite3

import nwnlib as n  # noqa: E402
import nwn_analyse  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_index  # noqa: E402
from _harness import check  # noqa: E402
from _gff import root as gff, loc, w, R, X, LS, LST, L, item_fields  # noqa: E402

C = n.CUSTOM_TLK_BIT
A = "locstrtest"


def item(folder, resref, strref, entries):
    """An item blueprint whose name is talk-table number `strref` plus `entries` ({language*2+gender: text})."""
    f = item_fields(resref, resref.upper(), "x")
    f["LocalizedName"] = (LS, L(strref=strref, entries=entries))
    w(folder, f"{resref}.uti", n.write_gff(gff("UTI ", **f)))


def main():
    with h.tempdir("nwn_locstr_") as tmp:
        user = os.path.join(tmp, "user")
        os.makedirs(os.path.join(user, "tlk"))
        game = os.path.join(tmp, "game")                # a game folder holding only dialog.tlk (English)
        os.makedirs(game)
        n.write_tlk(os.path.join(game, "dialog.tlk"), {0: "Blade of Testing", 1: "Old Name", 2: "Two\nlines"})
        n.write_tlk(os.path.join(user, "tlk", "loctlk.tlk"), {0: "Custom Blade", 1: "Custom Old"})
        mod = os.path.join(tmp, "mod")
        os.makedirs(mod)
        w(mod, "module.ifo", n.write_gff(gff("IFO ", Mod_Name=loc("L"), Mod_CustomTlk=(X, "loctlk"),
                                             Mod_Entry_Area=(R, "a1"), Mod_Area_list=(LST, []))))
        item(mod, "it_same", 0, {0: "Blade of Testing"})                # same as the game's line: remove the text
        item(mod, "it_diff", 1, {0: "New Name"})                        # renamed: set the number to -1
        item(mod, "it_ws", 2, {0: "Two\r\nlines  "})                    # same apart from line ends / spaces
        item(mod, "it_other_lang", 0, {4: "Lama di prova"})             # Italian text, English table: not judged
        item(mod, "it_noline", 9, {0: "Whatever"})                      # the table has no line 9: broken_strref
        item(mod, "it_plain", 0, {})                                    # number only: fine
        item(mod, "it_gender", 0, {0: "Blade of Testing", 1: "Blade of Testing (her)"})   # gendered on purpose
        item(mod, "it_cust_same", C + 0, {0: "Custom Blade"})           # custom line, same: the fallback, kept
        item(mod, "it_cust_diff", C + 1, {0: "Custom New"})             # custom line, different: reported
        out = os.path.join(D.WORKSPACE, A)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = nwn_analyse.main([mod, "--name", A, "--no-json", "--nwn-user", user, "--nwn-root", game])
        rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
        ls = [i for i in rep["issues"] if i["category"] == "locstring_both"]
        per = {i["label"]: i for i in ls if i["node"] != "module"}
        check("analysis ran", not rc, rc)
        check("same text as the line: an info note saying to remove the text",
              per.get("it_same.uti", {}).get("severity") == "info" and "remove the text" in per["it_same.uti"]["detail"],
              per.get("it_same.uti"))
        check("different text: an info note saying to set the number to -1, showing both texts",
              "set the number to -1" in per.get("it_diff.uti", {}).get("detail", "")
              and "New Name" in per["it_diff.uti"]["detail"] and "Old Name" in per["it_diff.uti"]["detail"],
              per.get("it_diff.uti"))
        check("line ends and spaces at the ends don't count as a difference",
              "remove the text" in per.get("it_ws.uti", {}).get("detail", "")
              and "-1" not in per["it_ws.uti"]["detail"], per.get("it_ws.uti"))
        check("a custom line repeated by its text is not reported (the text is the fallback when the custom .tlk "
              "can't be loaded); a custom line with different text is", "it_cust_same.uti" not in per
              and "set the number to -1" in per.get("it_cust_diff.uti", {}).get("detail", ""), sorted(per))
        check("a female text that differs from the male one is gendered on purpose: not judged",
              "it_gender.uti" not in per, per.get("it_gender.uti"))
        check("text only in another language than the talk table: not judged; number only: nothing; a missing line: "
              "left to broken_strref", not ({"it_other_lang.uti", "it_plain.uti", "it_noline.uti"} & set(per))
              and any(i["category"] == "broken_strref" and i["label"] == "it_noline.uti" for i in rep["issues"]),
              sorted(per))
        mod_rows = [i for i in ls if i["node"] == "module"]
        check("one warning for the module counting the fields and files",
              len(mod_rows) == 1 and mod_rows[0]["severity"] == "warning"
              and mod_rows[0]["detail"].startswith("4 text field(s) in 4 file(s)"), mod_rows)
        check("the report carries locstring_rules (older reports are offered Analyse again)",
              rep.get("locstring_rules") == 1)

        # a German game talk table and German text: compared in the table's own language
        de = os.path.join(tmp, "de_dialog.tlk")
        n.write_tlk(de, {0: "Testklinge"}, language=2)
        tl = n.TlkSet(n.Tlk(de), None)
        v = n.LocString(strref=0, entries={4: "Testklinge"})
        check("compared in the talk table's language (German table, German text, same)",
              nwn_index.locstring_both(v, tl) == (1, "Testklinge", "Testklinge"), nwn_index.locstring_both(v, tl))
        v = n.LocString(strref=0, entries={0: "Test blade"})
        check("English text with a German table: not judged", nwn_index.locstring_both(v, tl) is None)

        # an index made before 1.5.9 (no locstrings table) still analyses, reporting nothing of this kind
        db = sqlite3.connect(os.path.join(out, "index.sqlite"))
        db.execute("DROP TABLE locstrings")
        db.commit()
        db.close()
        import nwn_analysis
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_analysis.run_analysis(out, verbose=False)
        rep2 = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
        import nwn_update
        check("an older index without the table: the report says the check is missing, so Analyse again is offered",
              rep2.get("locstring_rules") is None
              and "locstring_rules" in [f["key"] for f in nwn_update.missing_features(rep2)], rep2.get("locstring_rules"))
        check("an older index without the table: analysed, no locstring_both rows",
              not [i for i in rep2["issues"] if i["category"] == "locstring_both"])

        # build option (owner's choice C, 2026-11-02): only the repeated texts are removed, the number is kept, and the
        # audit proves it. Analyse again first: the older-index probe above dropped the table.
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_analyse.main([mod, "--name", A, "--no-json", "--nwn-user", user, "--nwn-root", game])
        rep = json.load(open(os.path.join(out, "report.json"), encoding="utf-8"))
        check("report: counts for the Build page (2 same, 2 different)",
              rep.get("locstring_counts") == {"same": 2, "different": 2}, rep.get("locstring_counts"))
        import nwn_build
        import nwn_audit
        with contextlib.redirect_stdout(io.StringIO()):
            log = nwn_build.build(out, dict(delete=[], merge=[], locstring_fix=True), name="loc", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER))
        clean = os.path.join(out, "build", "loc")

        def name_of(rr):
            return n.read_gff_file(os.path.join(clean, rr + ".uti")).get("LocalizedName")
        same, ws, diff, oth = (name_of(x) for x in ("it_same", "it_ws", "it_diff", "it_other_lang"))
        check("build: a repeated text is removed and the number kept (also when only line ends/spaces differ)",
              same.strref == 0 and same.entries == {} and ws.strref == 2 and ws.entries == {}, (same, ws))
        check("build: a text that differs from its line, and one in another language, are untouched",
              diff.strref == 1 and diff.entries == {0: "New Name"} and oth.entries == {4: "Lama di prova"},
              (diff, oth))
        check("build: a custom line's repeated text (the fallback) and a gendered text are kept",
              name_of("it_cust_same").entries == {0: "Custom Blade"} and len(name_of("it_gender").entries) == 2)
        lf = {c["id"]: c for c in log["audit"]["checks"]}.get("locstring_fix")
        refs = {c["id"]: c for c in log["audit"]["checks"]}["references"]
        check("audit: locstring_fix PASS, and the build passes", lf and lf["status"] == "PASS"
              and log["audit"]["verdict"].startswith("PASS") and log.get("locstring_fix") == {"fields": 2, "files": 2},
              (lf, log["audit"]["verdict"], log.get("locstring_fix")))
        check("audit: fewer such texts after the fix is not a 'NEW warning' (references PASS)",
              refs["status"] == "PASS", refs)
        check("change log: a section for the removed texts", "## Localised texts that repeated their talk-table line"
              in open(os.path.join(out, "build", "changes.md"), encoding="utf-8").read())
        # off by default: a plain build changes none of them
        with contextlib.redirect_stdout(io.StringIO()):
            log0 = nwn_build.build(out, dict(delete=[], merge=[]), name="loc0", verbose=False,
                                   audit_kwargs=dict(compiler=h.NO_COMPILER))
        clean0 = os.path.join(out, "build", "loc0")
        check("off unless ticked: a plain build keeps every text, and has no locstring_fix check",
              n.read_gff_file(os.path.join(clean0, "it_same.uti")).get("LocalizedName").entries == {0: "Blade of Testing"}
              and "locstring_fix" not in {c["id"] for c in log0["audit"]["checks"]})
        # tampering: the clean copy's number changed and the change log made to agree - "modified" passes, this
        # check must not
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_build.build(out, dict(delete=[], merge=[], locstring_fix=True), name="loc", verbose=False,
                            run_audit=False)
        cj = os.path.join(out, "build", "changes.json")
        ch = json.load(open(cj, encoding="utf-8"))
        root_ = n.read_gff_file(os.path.join(clean, "it_same.uti"))
        bad = n.LocString(strref=1, entries={})
        root_.fields["LocalizedName"].value = bad
        with open(os.path.join(clean, "it_same.uti"), "wb") as fh:
            fh.write(n.write_gff(root_))
        for c in ch["field_changes"]:
            if c["file"] == "it_same.uti":
                c["new"] = str(bad)
        json.dump(ch, open(cj, "w", encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            a2 = nwn_audit.audit(out, os.path.join(out, "build"), "loc", compiler=h.NO_COMPILER, verbose=False)
        lf2 = {c["id"]: c for c in a2["checks"]}.get("locstring_fix")
        check("audit: a changed talk-table number fails locstring_fix even when the log was made to agree",
              lf2 and lf2["status"] == "FAIL" and any("number changed" in e for e in lf2["evidence"]), lf2)

        def tamper(fn, new_value, drop_keys=(), why=None):
            """Rebuild (no audit), put new_value into fn's name, make the change log agree, audit; returns checks."""
            with contextlib.redirect_stdout(io.StringIO()):
                nwn_build.build(out, dict(delete=[], merge=[], locstring_fix=True), name="loc", verbose=False,
                                run_audit=False)
            ch_ = json.load(open(cj, encoding="utf-8"))
            r_ = n.read_gff_file(os.path.join(clean, fn))
            r_.fields["LocalizedName"].value = new_value
            with open(os.path.join(clean, fn), "wb") as fh:
                fh.write(n.write_gff(r_))
            ch_["field_changes"].append(dict(file=fn, field="LocalizedName", old="x", new=str(new_value),
                                             why=why or "localised text removed: it repeats its talk-table line"))
            for c in ch_["field_changes"]:
                for k in drop_keys:
                    c.pop(k, None)
            if fn not in ch_["rewritten"]:
                ch_["rewritten"].append(fn)
            json.dump(ch_, open(cj, "w", encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                a_ = nwn_audit.audit(out, os.path.join(out, "build"), "loc", compiler=h.NO_COMPILER, verbose=False)
            return {c["id"]: c for c in a_["checks"]}
        # a different text removed and logged WITHOUT the strref key (so the old locstring_fix never saw it)
        ck = tamper("it_diff.uti", n.LocString(strref=1, entries={}), drop_keys=("strref",))
        check("audit: a localised text changed with no rule behind it fails 'modified' (whatever the log says)",
              ck["modified"]["status"] == "FAIL" and any("localised text" in e for e in ck["modified"]["evidence"]),
              ck["modified"])
        # a file the log calls "edited" with no edited copy in edits/ is not waved through
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_build.build(out, dict(delete=[], merge=[], locstring_fix=True), name="loc", verbose=False,
                            run_audit=False)
        ch_ = json.load(open(cj, encoding="utf-8"))
        r_ = n.read_gff_file(os.path.join(clean, "it_diff.uti"))
        r_.fields["LocalizedName"].value = n.LocString(strref=1, entries={})
        with open(os.path.join(clean, "it_diff.uti"), "wb") as fh:
            fh.write(n.write_gff(r_))
        ch_["edited"].append("it_diff.uti")
        json.dump(ch_, open(cj, "w", encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            a4 = nwn_audit.audit(out, os.path.join(out, "build"), "loc", compiler=h.NO_COMPILER, verbose=False)
        m4 = {c["id"]: c for c in a4["checks"]}["modified"]
        check("audit: a file the log calls edited must match the edited copy in edits/ (none there: FAIL)",
              m4["status"] == "FAIL" and any("no edited copy" in e for e in m4["evidence"]), m4)
        # the game's talk table gone at audit time: nothing can be proven -> FAIL, not WARN
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_build.build(out, dict(delete=[], merge=[], locstring_fix=True), name="loc", verbose=False,
                            run_audit=False)
        os.rename(os.path.join(game, "dialog.tlk"), os.path.join(game, "dialog.tlk.away"))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                a3 = nwn_audit.audit(out, os.path.join(out, "build"), "loc", compiler=h.NO_COMPILER, verbose=False)
        finally:
            os.rename(os.path.join(game, "dialog.tlk.away"), os.path.join(game, "dialog.tlk"))
        lf3 = {c["id"]: c for c in a3["checks"]}.get("locstring_fix")
        check("audit: without the game's talk table the removals can't be proven - FAIL, not a warning",
              lf3 and lf3["status"] == "FAIL" and any("dialog.tlk" in e for e in lf3["evidence"]), lf3)
    return h.summary()


if __name__ == "__main__":
    raise SystemExit(main())
