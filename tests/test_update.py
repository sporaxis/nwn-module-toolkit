"""
Tests for updating the toolkit (nwn_update.py and its dashboard parts): bringing work across from another copy
(found next to this one, copied - never moved - with the other copy left byte-for-byte unchanged), the version stamp
on analyses with "Analyse again", and the guard that stops an older version building or rewriting work a newer
version saved.
    python tests/test_update.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_palette  # noqa: E402
import nwn_progress  # noqa: E402
import nwn_update as U  # noqa: E402
import test_bpchanges as TB  # noqa: E402 - its small module with blueprints, palettes and a hak
from _harness import check  # noqa: E402


def write(p, data):
    """Write text or bytes to p, making folders."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
        fh.write(data)


def fake_copy(folder, version="vault 1.2.0 abc1234", analyses=("ModA", "ModB"), compiler=True):
    """A toolkit copy as a user has it: VERSION.txt, nwn_dashboard.py, a workspace with settings, analyses (with
    edits, a build and hidden undo history), hak projects, a timings file, and tools/ with the compiler."""
    write(os.path.join(folder, "nwn_dashboard.py"), "# stand-in\n")
    if version:
        write(os.path.join(folder, "VERSION.txt"), version + "\n")
    ws = os.path.join(folder, "nwn_workspace")
    for a in analyses:
        write(os.path.join(ws, a, "report.json"), json.dumps(dict(summary=dict(module_name=a))))
        write(os.path.join(ws, a, "index.sqlite"), os.urandom(5000))
        write(os.path.join(ws, a, "edits", "x.nss"), "void main(){}\n")
        write(os.path.join(ws, a, "edits", ".history", "discarded", "t1", "y.nss"), "old\n")
        write(os.path.join(ws, a, "build", "audit.json"), "{}")
        write(os.path.join(ws, a, "palette_moves.json"), json.dumps(dict(version=1, moves={"uti": {"a": {"to": 2}}})))
    write(os.path.join(ws, "_haks", "proj1", "project.json"), "{}")
    write(os.path.join(ws, "_to_delete", "junk.txt"), "x")
    write(os.path.join(ws, "timings.json"), "[]")
    comp = os.path.join(folder, "tools", "nwn_script_comp")
    settings = dict(compiler=comp if compiler else "", nwn_root="/games/nwn", nwn_user="/docs/nwn", log_paths=[],
                    last_folder="", external_tools=[dict(name="Explorer", path="/apps/ex.exe", exts=["hak"])])
    write(os.path.join(ws, "settings.json"), json.dumps(settings))
    write(os.path.join(folder, "tools", "README.txt"), "readme\n")
    if compiler:
        write(comp, b"\x7fELF compiler")
        os.chmod(comp, 0o755)
    return folder


def tree_hash(folder):
    """{relative path: sha256} of every file under folder - to prove a folder was not changed."""
    out = {}
    for dp, _d, fs in os.walk(folder):
        for f in fs:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, folder)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def versions():
    """Release labels and version numbers; what analysing again would add."""
    with h.tempdir() as t:
        write(os.path.join(t, "VERSION.txt"), "vault 1.5.0 1a2b3c4\n")
        check("version: a release folder's label", U.version_label(t) == "vault 1.5.0" and U.release_info(t)[2] == "1a2b3c4")
        check("version: no VERSION.txt is a development copy", U.version_label(os.path.join(t, "x")) == "development copy")
    check("version: numbers compare as numbers", U.version_tuple("vault 1.10.0") > U.version_tuple("1.9.2") and
          U.version_tuple("development copy") is None)
    check("re-analysis: a report without the 1.4.0 and 1.5.2 checks lists them; a current one lists nothing",
          [f["key"] for f in U.missing_features({"summary": {}})] == ["asset_duplicates", "merge_rules", "noise_rules"] and
          U.missing_features({"asset_duplicates": [], "merge_rules": 2, "noise_rules": 1}) == [])


def work_guard(out):
    """(c) Work a build applies is stamped; work saved by a newer toolkit stops the build and a rewrite."""
    nwn_palette.set_moves(out, "uti", ["sword"], 1, nwn_palette.build_palette(out, write=False))
    w = U.read_work(out)
    check("guard: saving a palette move stamps the analysis with this version's work format",
          w.get("work_format") == U.WORK_FORMAT and w.get("saved_with") == U.version_label(), w)
    check("guard: nothing newer -> no warning", U.newer_work(out) is None)
    w.update(work_format=U.WORK_FORMAT + 1, saved_with="vault 9.9.0")
    write(os.path.join(out, U.WORK_FILE), json.dumps(w))
    msg = U.newer_work(out) or ""
    check("guard: work from a newer version is named, with what to do", "vault 9.9.0" in msg and "use that version" in msg, msg)
    before = open(os.path.join(out, nwn_palette.MOVES_FILE), encoding="utf-8").read()
    try:
        nwn_palette.undo_moves(out)
        ok = False
    except ValueError:
        ok = True
    check("guard: this version refuses to rewrite that work (the file is unchanged)",
          ok and open(os.path.join(out, nwn_palette.MOVES_FILE), encoding="utf-8").read() == before)
    try:
        nwn_build.build(out, {}, out_dir=os.path.join(out, "nobuild"), run_audit=False, verbose=False)
        ok = False
    except ValueError as ex:
        ok = "newer version" in str(ex)
    check("guard: the build refuses before writing anything", ok and not os.path.exists(os.path.join(out, "nobuild")))
    os.remove(os.path.join(out, U.WORK_FILE))
    nwn_palette.undo_moves(out)


def finding():
    """(a) Copies next to this one are found - also inside the doubled folder Windows' Extract All makes."""
    with h.tempdir() as t:
        new = os.path.join(t, "nwn-toolkit-1.5.0", "nwn-toolkit-1.5.0")
        write(os.path.join(new, "nwn_dashboard.py"), "#\n")
        fake_copy(os.path.join(t, "nwn-toolkit-1.2.0", "nwn-toolkit-1.2.0"))
        fake_copy(os.path.join(t, "toolkit-old"), version="vault 1.1.0 x", analyses=("Only",))
        os.makedirs(os.path.join(t, "empty-copy", "nwn_workspace"))
        write(os.path.join(t, "empty-copy", "nwn_dashboard.py"), "#\n")
        found = U.find_copies(new)
        names = sorted(os.path.basename(d["path"]) for d in found)
        check("find: both older copies with work are found (nested and plain); an empty one and this one are not",
              names == ["nwn-toolkit-1.2.0", "toolkit-old"], found)
        d = next(x for x in found if x["version"] == "vault 1.2.0")
        check("find: version, analyses, settings and the compiler are described",
              d["analyses"] == ["ModA", "ModB"] and d["settings"] and d["tools"] == ["nwn_script_comp"], d)
        try:
            U.describe_copy(os.path.join(t, "empty-copy", "nope"))
            ok = False
        except ValueError:
            ok = True
        check("find: a folder that is not a copy is refused in words", ok)


def importing():
    """(a) Plan, copy, the other copy unchanged, settings merged and repointed, tools added, Stop leaves nothing."""
    with h.tempdir() as t:
        old = fake_copy(os.path.join(t, "old"))
        new = os.path.join(t, "new")
        write(os.path.join(new, "nwn_dashboard.py"), "#\n")
        write(os.path.join(new, "tools", "README.txt"), "new readme\n")
        ws = os.path.join(new, "nwn_workspace")
        write(os.path.join(ws, "settings.json"), json.dumps(dict(nwn_root="/games/nwn-new", compiler="")))
        before = tree_hash(old)
        p = U.plan_import(old, ws, new)
        names = sorted(c["name"] for c in p["copy"])
        check("plan: analyses, hak projects and timings are copied; the old to-delete folder is not",
              names == ["ModA", "ModB", "_haks", "timings.json"] and p["settings"] == "merge" and not p["refused"], p)
        check("plan: the compiler is added to tools; README.txt is this copy's own",
              [x["name"] for x in p["tools"]] == ["nwn_script_comp"] and p["total_bytes"] > 10000, p["tools"])
        check("plan: nothing written yet", sorted(os.listdir(ws)) == ["settings.json"])
        # Stop part-way: nothing here changes, the staging folder is gone
        calls = dict(n=0)

        def stop():
            calls["n"] += 1
            return calls["n"] > 3
        try:
            U.run_import(old, ws, new, stop=stop)
            ok = False
        except InterruptedError:
            ok = True
        check("stop: stopped part-way leaves nothing behind here", ok and sorted(os.listdir(ws)) == ["settings.json"] and
              sorted(os.listdir(os.path.join(new, "tools"))) == ["README.txt"])
        seen = []
        rec = U.run_import(old, ws, new, progress=lambda pct, text: seen.append(pct))
        check("import: progress runs up to 100%", seen and seen[-1] == 100.0 and all(0 <= x <= 100 for x in seen))
        same = all(tree_hash(os.path.join(old, "nwn_workspace", a)) == tree_hash(os.path.join(ws, a)) for a in ("ModA", "ModB"))
        check("import: each analysis arrives byte-for-byte, with edits, undo history, build and waiting moves",
              same and os.path.isfile(os.path.join(ws, "ModA", "edits", ".history", "discarded", "t1", "y.nss")))
        check("import: the other copy is unchanged (every file's hash)", tree_hash(old) == before)
        comp = os.path.join(new, "tools", "nwn_script_comp")
        check("import: the compiler is copied and still runnable", os.path.isfile(comp) and (os.name == "nt" or os.access(comp, os.X_OK)))
        st = json.load(open(os.path.join(ws, "settings.json"), encoding="utf-8"))
        check("settings: a value already set here wins; the rest comes across",
              st["nwn_root"] == "/games/nwn-new" and st["nwn_user"] == "/docs/nwn" and st["external_tools"][0]["name"] == "Explorer", st)
        check("settings: the compiler path points at this copy's tools folder", st["compiler"] == comp, st["compiler"])
        check("record: imported_from.json says what came from where",
              U.imported(ws)[0]["analyses"] == ["ModA", "ModB"] and U.imported(ws)[0]["version"] == "vault 1.2.0" and
              rec["files"] >= 12, U.imported(ws))
        check("import: the staging folder is gone", not os.path.exists(os.path.join(ws, U.STAGING)))
        p2 = U.plan_import(old, ws, new)
        check("again: everything is already here - refused, and each piece says why",
              p2["refused"] and "nothing to bring" in p2["refused"] and any("already" in s["why"] for s in p2["skip"]), p2)


def refusals():
    """(a) This copy, a running analysis, too little disk space, and a newer copy."""
    with h.tempdir() as t:
        old = fake_copy(os.path.join(t, "old"))
        new = os.path.join(t, "new")
        write(os.path.join(new, "nwn_dashboard.py"), "#\n")
        ws = os.path.join(new, "nwn_workspace")
        p = U.plan_import(old, os.path.join(old, "nwn_workspace"), old)
        check("refuse: bringing a copy into itself", p["refused"] and "this copy" in p["refused"], p)
        fh = nwn_progress.acquire_run_lock(os.path.join(old, "nwn_workspace", "ModA"))
        try:
            p = U.plan_import(old, ws, new)
            check("refuse: an analysis is running in the other copy", p["refused"] and "ModA" in p["refused"], p)
        finally:
            nwn_progress.release_run_lock(fh, os.path.join(old, "nwn_workspace", "ModA"))
        real = U.shutil.disk_usage
        U.shutil.disk_usage = lambda p_: type("U", (), {"free": 1000})()
        try:
            p = U.plan_import(old, ws, new)
        finally:
            U.shutil.disk_usage = real
        check("refuse: not enough free disk space, with the sizes", p["refused"] and "free disk space" in p["refused"], p)
        newer = fake_copy(os.path.join(t, "newer"), version="vault 99.0.0 z")
        write(os.path.join(newer, "nwn_workspace", "ModA", U.WORK_FILE),
              json.dumps(dict(work_format=U.WORK_FORMAT + 1, saved_with="vault 99.0.0")))
        p = U.plan_import(newer, ws, new)
        # a development copy has no version number to compare, so only a release says the other copy is newer
        dated = U.version_tuple(U.version_label()) is not None
        check("warn: a newer copy (when this one is a release), and its analyses with newer work, are named before copying",
              not p["refused"] and any("NEWER" in w for w in p["warnings"]) == dated and any("ModA" in w for w in p["warnings"]),
              p["warnings"])
        p = U.plan_import(os.path.join(old, "nwn_workspace"), ws, new)
        check("plan: picking the old nwn_workspace folder itself works too", not p["refused"] and p["tools"], p)


def dashboard(out):
    """The dashboard: the first-start offer, the plan endpoint, the analysis's version and Analyse again."""
    check("state: an offer only while this copy has no analyses", D.update_offer([{"name": "x"}]) == [])
    with h.tempdir() as t:
        old = fake_copy(os.path.join(t, "old"))
        p = D.POST_ROUTES["/api/update/plan"]({"from": old})
        check("api: plan for the folder you picked", sorted(c["name"] for c in p["copy"])[:2] == ["ModA", "ModB"], p)
    name = "upd_reana"
    dst = os.path.join(D.WORKSPACE, name)
    shutil.copytree(out, dst)
    write(os.path.join(dst, "palette_moves.json"), json.dumps(dict(version=1, moves={"uti": {"sword": {"to": 1, "from": 1}}})))
    v = D.GET_ROUTES["/api/analysis/version"]({"a": name})
    check("api: an analysis's version check (nothing newer)", v["newer_work"] is None, v)
    rep = json.load(open(os.path.join(dst, "report.json"), encoding="utf-8"))
    check("stamp: a new analysis records the version that made it",
          rep.get("toolkit") == dict(version=U.version_label(), work_format=U.WORK_FORMAT), rep.get("toolkit"))
    del rep["toolkit"], rep["asset_duplicates"], rep["merge_rules"], rep["noise_rules"]
    write(os.path.join(dst, "report.json"), json.dumps(rep))           # as an older version left it
    r = D.POST_ROUTES["/api/reanalyse"]({"a": name})
    t0 = time.time()
    while D.JOBS[r["job"]].status == "running" and time.time() - t0 < 120:
        time.sleep(0.3)
    j = D.JOBS[r["job"]]
    rep = json.load(open(os.path.join(dst, "report.json"), encoding="utf-8"))
    check("analyse again: the same module into the same folder; the new checks and the stamp are back",
          j.status == "done" and "asset_duplicates" in rep and rep.get("merge_rules") == 2 and rep.get("noise_rules") == 1 and rep.get("toolkit", {}).get("work_format") == U.WORK_FORMAT,
          lambda: (j.status, j.lines[-5:]))
    check("analyse again: waiting changes stay", nwn_palette.load_moves(dst) == {"uti": {"sword": {"to": 1, "from": 1}}})
    with h.tempdir() as t:
        gone = os.path.join(D.WORKSPACE, "upd_gone")
        shutil.copytree(dst, gone)
        c = sqlite3.connect(os.path.join(gone, "index.sqlite"))
        c.execute("UPDATE meta SET value=? WHERE key='module_path'", (os.path.join(t, "gone.mod"),))
        c.commit()
        c.close()
        try:
            D.POST_ROUTES["/api/reanalyse"]({"a": "upd_gone"})
            ok = False
        except ValueError as ex:
            ok = "not there any more" in str(ex)
        check("analyse again: a module that has gone is refused in words", ok)
    D.release_analysis(name)
    D.release_analysis("upd_gone")


def page_script():
    """The page: the offer, the Settings button, the Overview notice and the Build page guard."""
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the first-start offer and 'Bring work from another copy…' in Settings",
          all(x in html for x in ('id="upd"', "function renderUpdateOffer(", "Bring my work across", 'id="bring"',
                                  "async function bringWork(", "/api/update/import")))
    check("page: the Overview shows the version and offers Analyse again", 'id="reana"' in html and "function versionNotice(" in html)
    check("page: the Build page is blocked when a newer version saved work", "Can't build with this version" in html)
    install = open(os.path.join(h.ROOT, "INSTALL.md"), encoding="utf-8").read()
    check("install guide: updating says to press Update, and how work from an old copy comes along",
          "**Update**" in install and "Bring my work from" in install)


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    h.run(versions)
    with h.tempdir() as t:
        out = TB.fixture(t)
        h.run(work_guard, out)
        h.run(dashboard, out)
    for fn in (finding, importing, refusals, page_script):
        h.run(fn)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
