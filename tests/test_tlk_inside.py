"""
A custom talk table packed inside the module or a hak (a tester's plan, 2026-10-31: "I may also pack the .tlk into
the module ... one less file that I need to send builders"). The tlk folder is where the game is documented to read a
custom tlk, so a copy found only inside the module or a hak is read (custom names resolve, no flood of "no name"
warnings) and reported as one warning asking for a check in game. Covered: a module folder, a .mod, a hak, the tlk
folder winning, nothing anywhere, a packed file that is not a talk table, Analyse again after the copy was removed
(the earlier copy is never reused), an unsafe Mod_CustomTlk name, the Quick scan, and Clear removing the copy.
    python tests/test_tlk_inside.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import json
import io
import os
import sqlite3

import nwnlib as n  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_index  # noqa: E402
import nwn_quickscan  # noqa: E402
from _harness import check  # noqa: E402
from _gff import root as gff, loc, w, R, X, LS, LST, L, st, item_fields  # noqa: E402

CUSTOM = n.CUSTOM_TLK_BIT


def tlk_bytes(tmp, entries):
    """The bytes of a TLK V3.0 holding {index: text}."""
    p = os.path.join(tmp, "_t.tlk")
    n.write_tlk(p, entries)
    with open(p, "rb") as fh:
        data = fh.read()
    os.remove(p)
    return data


def module_files(tlk_name="mytlk", haks=()):
    """(name, bytes) of a module whose item's name is custom talk-table entry 0."""
    ifo = gff("IFO ", Mod_Name=loc("Tlk"), Mod_CustomTlk=(X, tlk_name), Mod_Entry_Area=(R, "a1"),
              Mod_Area_list=(LST, []), Mod_HakList=(LST, [st(8, Mod_Hak=(X, x)) for x in haks]))
    f = item_fields("it_custom", "IT_CUSTOM", "x")
    f["LocalizedName"] = (LS, L(strref=CUSTOM + 0))
    return [("module.ifo", n.write_gff(ifo)), ("it_custom.uti", n.write_gff(gff("UTI ", **f)))]


def as_folder(folder, files):
    os.makedirs(folder, exist_ok=True)
    for name, data in files:
        w(folder, name, data)
    return folder


def as_erf(path, files, kind="MOD "):
    n.write_erf(path, [(os.path.splitext(f)[0], os.path.splitext(f)[1][1:], d) for f, d in files], kind)
    return path


def index(mod, out, haks=(), tlk=None, nwn_user=None):
    """run_index quietly; returns (issues by category, strref status of the item's name, meta)."""
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, haks=haks, tlk=tlk, out=out, write_json=False, verbose=False, nwn_user=nwn_user)
    db = sqlite3.connect(os.path.join(out, "index.sqlite"))
    issues = {}
    for sev, cat, msg in db.execute("SELECT severity, category, detail FROM issues"):
        issues.setdefault(cat, []).append((sev, msg))
    st_ = [r[0] for r in db.execute("SELECT status FROM strrefs WHERE strref=?", (CUSTOM,))]
    meta = dict(db.execute("SELECT key, value FROM meta"))
    db.close()
    return issues, st_, meta


def main():
    with h.tempdir("nwn_tlkin_") as tmp:
        good = tlk_bytes(tmp, {0: "Blade of Testing"})
        user = os.path.join(tmp, "user")          # an NWN user folder with an empty tlk folder
        os.makedirs(os.path.join(user, "tlk"))

        # 1. packed in a module folder
        mod = as_folder(os.path.join(tmp, "m1"), module_files() + [("mytlk.tlk", good)])
        out = os.path.join(tmp, "a1")
        iss, sts, meta = index(mod, out, nwn_user=user)
        check("module folder: the packed tlk is read, so the custom name resolves", sts == ["ok"], sts)
        check("module folder: one warning tlk_inside_module, no tlk_not_found",
              [s for s, _ in iss.get("tlk_inside_module", [])] == ["warning"] and "tlk_not_found" not in iss, iss)
        check("module folder: meta tlk_inside = module; the copy is inside the analysis folder",
              meta.get("tlk_inside") == "module"
              and os.path.dirname(meta.get("tlk", "")) == os.path.join(out, nwn_index.TLK_INSIDE), meta.get("tlk"))
        check("module folder: the module's own tlk is untouched",
              open(os.path.join(mod, "mytlk.tlk"), "rb").read() == good)

        # 2. packed in a .mod
        modf = as_erf(os.path.join(tmp, "m2.mod"), module_files() + [("mytlk.tlk", good)])
        iss, sts, meta = index(modf, os.path.join(tmp, "a2"), nwn_user=user)
        check(".mod: the packed tlk is read and warned about once",
              sts == ["ok"] and len(iss.get("tlk_inside_module", [])) == 1 and meta.get("tlk_inside") == "module",
              (sts, iss.get("tlk_inside_module"), meta.get("tlk_inside")))

        # 3. packed in a hak the module lists
        hak = as_erf(os.path.join(tmp, "haks", "myhak.hak"), [("mytlk.tlk", good)], "HAK ") \
            if os.makedirs(os.path.join(tmp, "haks")) is None else None
        modh = as_folder(os.path.join(tmp, "m3"), module_files(haks=["myhak"]))
        iss, sts, meta = index(modh, os.path.join(tmp, "a3"), haks=[hak], nwn_user=user)
        check("hak: the tlk packed in a listed hak is read; the warning names the hak",
              sts == ["ok"] and meta.get("tlk_inside") == "hak:myhak"
              and "myhak" in (iss.get("tlk_inside_module") or [("", "")])[0][1], (sts, meta.get("tlk_inside"), iss))

        # 4. the tlk folder wins: no warning
        w(os.path.join(user, "tlk"), "mytlk.tlk", good)
        iss, sts, meta = index(mod, os.path.join(tmp, "a4"), nwn_user=user)
        check("tlk folder has it too: that copy is used, no warning",
              sts == ["ok"] and "tlk_inside_module" not in iss and not meta.get("tlk_inside")
              and meta.get("tlk", "").startswith(os.path.join(user, "tlk")), (iss, meta.get("tlk")))
        os.remove(os.path.join(user, "tlk", "mytlk.tlk"))

        # 5. nowhere: an error, which now says where it looked
        bare = as_folder(os.path.join(tmp, "m5"), module_files())
        iss, sts, meta = index(bare, os.path.join(tmp, "a5"), nwn_user=user)
        check("nowhere: tlk_not_found error naming the tlk folder, the module and its haks",
              [s for s, _ in iss.get("tlk_not_found", [])] == ["error"]
              and "the module or its haks" in iss["tlk_not_found"][0][1] and sts == ["no-custom-tlk"], (iss, sts))

        # 6. a packed file that is not a talk table
        junk = as_folder(os.path.join(tmp, "m6"), module_files() + [("mytlk.tlk", b"not a talk table")])
        iss, sts, meta = index(junk, os.path.join(tmp, "a6"), nwn_user=user)
        check("packed file that is not a TLK: tlk_not_found says so; nothing is copied",
              "not a talk table" in (iss.get("tlk_not_found") or [("", "")])[0][1]
              and not os.path.isdir(os.path.join(tmp, "a6", nwn_index.TLK_INSIDE)), iss)

        # 7. Analyse again passes the earlier copy as the tlk: it is ignored and the search runs afresh
        out = os.path.join(tmp, "a1")
        copy = os.path.join(out, nwn_index.TLK_INSIDE, "mytlk.tlk")
        os.remove(os.path.join(mod, "mytlk.tlk"))           # the builder took the tlk out of the module
        iss, sts, meta = index(mod, out, tlk=copy, nwn_user=user)
        check("Analyse again after the tlk left the module: the earlier copy is not reused - tlk_not_found",
              "tlk_not_found" in iss and sts == ["no-custom-tlk"], (iss, sts, meta.get("tlk")))

        # 8. an unsafe Mod_CustomTlk name: the copy stays inside tlk_from_module
        odd = as_folder(os.path.join(tmp, "m8"), module_files("..\\..\\evil") + [("..evil.tlk", good)])
        found = nwn_index.custom_tlk_inside("..\\..\\evil", odd)
        tlkset, info = nwn_index.find_tlks("..evil", None, None, user, odd, (), os.path.join(tmp, "a8"))
        written = [os.path.join(dp, f) for dp, _, fs in os.walk(tmp) for f in fs if f.endswith(".tlk")
                   and os.path.join(tmp, "a8") in dp]
        check("unsafe tlk name: a path-like name finds nothing; a dotted one is copied under a safe name inside the "
              "analysis folder", found is None and info["custom"]
              and all(os.path.dirname(p) == os.path.join(tmp, "a8", nwn_index.TLK_INSIDE) for p in written)
              and len(written) == 1, (found, info, written))

        # 9. Quick scan: the packed tlk is named as a warning, not reported missing
        m2 = as_folder(os.path.join(tmp, "m9"), module_files() + [("mytlk.tlk", good)])
        qs = nwn_quickscan.scan(m2, nwn_user=user)
        texts = [(x["level"], x["text"]) for x in qs["warnings"] if "talk table" in x["text"]]
        check("quick scan: one warning that the tlk is packed inside the module; tlk.custom_inside = module",
              len(texts) == 1 and texts[0][0] == "warning" and "packed inside the module" in texts[0][1]
              and qs["tlk"]["custom_inside"] == "module", (texts, qs["tlk"]))
        qs = nwn_quickscan.scan(modh, haks=[hak], nwn_user=user)
        check("quick scan: a tlk packed in a hak is named by the hak",
              qs["tlk"]["custom_inside"] == "hak:myhak", qs["tlk"])

        # 11. review findings (2026-11-02): a cut-off packed tlk is a finding, not a crash; a tlk in a subfolder of a
        # module folder is found (the index and quick scan walk subfolders too); a huge entry count can't stall pages
        for nm_, data_ in (("magic", b"TLK "), ("short", b"TLK V3.0" + b"\0" * 8)):
            tm = as_folder(os.path.join(tmp, "t_" + nm_), module_files() + [("mytlk.tlk", data_)])
            try:
                iss, sts, meta = index(tm, os.path.join(tmp, "at_" + nm_), nwn_user=user)
                ok_ = "damaged or not a talk table" in (iss.get("tlk_not_found") or [("", "")])[0][1] and \
                    not os.listdir(os.path.join(tmp, "at_" + nm_, nwn_index.TLK_INSIDE))
            except Exception as ex:  # noqa
                ok_, iss = False, f"{type(ex).__name__}: {ex}"
            check(f"a packed tlk cut off ({nm_}): tlk_not_found says it is damaged; no crash, no copy kept", ok_, iss)
        # the same cut-off file in the tlk FOLDER (not packed): also a finding, not a crash
        w(os.path.join(user, "tlk"), "mytlk.tlk", b"TLK V3.0" + b"\0" * 8)
        try:
            iss, sts, meta = index(bare, os.path.join(tmp, "a_dam"), nwn_user=user)
            ok_ = "damaged or not a talk table" in (iss.get("tlk_not_found") or [("", "")])[0][1]
        except Exception as ex:  # noqa
            ok_, iss = False, f"{type(ex).__name__}: {ex}"
        os.remove(os.path.join(user, "tlk", "mytlk.tlk"))
        check("a cut-off custom tlk in the tlk folder: tlk_not_found says it is damaged; no crash", ok_, iss)
        sub = as_folder(os.path.join(tmp, "m_sub"), module_files())
        as_folder(os.path.join(sub, "src"), [("MyTlk.tlk", good)])
        iss, sts, meta = index(sub, os.path.join(tmp, "a_sub"), nwn_user=user)
        check("a tlk in a subfolder of the module folder is found, as the index and quick scan see it",
              meta.get("tlk_inside") == "module" and sts == ["ok"], (iss, meta.get("tlk_inside")))
        huge = os.path.join(tmp, "huge.tlk")
        with open(huge, "wb") as fh:
            fh.write(b"TLK V3.0" + (0).to_bytes(4, "little") + (0xFFFFFFFF).to_bytes(4, "little") + (20).to_bytes(4, "little"))
        check("Tlk.entries() is bounded by what the file holds (a header claiming 4 billion entries)",
              list(n.Tlk(huge).entries()) == [])
        # a build from a module whose tlk is packed: the analysis' own copy is not put next to the build
        import nwn_build
        out_b = os.path.join(D.WORKSPACE, "tlkin_build")
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_index.run_index(modf, out=out_b, write_json=False, verbose=False, nwn_user=user)
            import nwn_analysis
            nwn_analysis.run_analysis(out_b, verbose=False)
            log = nwn_build.build(out_b, dict(delete=[], merge=[]), name="tb", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER, nwn_user=user))
        check("build: a packed tlk travels inside the .mod; no separate copy is put beside it",
              not log.get("tlk_file") and not os.path.exists(os.path.join(out_b, "build", "mytlk.tlk")),
              (log.get("tlk_file"), os.listdir(os.path.join(out_b, "build"))))

        # an original analysed by 1.5.8 says "...not found in the NWN tlk folder - ..."; 1.5.9 says where else it looked.
        # The same finding must not look NEW to the audit
        out_o = os.path.join(D.WORKSPACE, "tlkin_old")
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_index.run_index(bare, out=out_o, write_json=False, verbose=False, nwn_user=user)
            nwn_analysis.run_analysis(out_o, verbose=False)
        rp = os.path.join(out_o, "report.json")
        rep_ = json.load(open(rp, encoding="utf-8"))
        for i in rep_["issues"]:
            if i["category"] == "tlk_not_found":
                i["detail"] = ("module.ifo names custom talk table 'mytlk.tlk' but it was not found in the NWN tlk "
                               "folder - custom names/descriptions cannot be checked")
        json.dump(rep_, open(rp, "w", encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            log = nwn_build.build(out_o, dict(delete=[], merge=[]), name="to", verbose=False,
                                  audit_kwargs=dict(compiler=h.NO_COMPILER, nwn_user=user))
        refs = {c["id"]: c for c in log["audit"]["checks"]}["references"]
        check("audit: a finding worded differently by an older version is not a NEW error", refs["status"] == "PASS",
              refs)

        # 10. Clear removes the copy (a run generates it)
        check("Clear: tlk_from_module is a generated folder", nwn_index.TLK_INSIDE in D.DATA_DIRS)
    return h.summary()


if __name__ == "__main__":
    raise SystemExit(main())
