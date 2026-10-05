"""
Tests for installing and updating the toolkit (nwn_setup.py) and the workspace rule it relies on
(nwn_update.workspace_dir): a new install, an update that swaps only the program (your work and the compiler stay,
the old program goes to previous/), what is found on the computer beforehand, the refusals, and the dashboard parts.
    python tests/test_setup.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders: HOME and the remembered-installs list point into the test's own folder.
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import hashlib
import json
import os
import sys

import nwn_dashboard as D  # noqa: E402
import nwn_progress  # noqa: E402
import nwn_setup as S  # noqa: E402
import nwn_update as U  # noqa: E402
from _harness import check  # noqa: E402
from test_update import fake_copy, write, tree_hash  # noqa: E402


def release(folder, version="vault 1.5.0 abc1234"):
    """An unzipped release: VERSION.txt, program files, tools/README.txt - plus things that must NOT be installed: its
    own nwn_workspace, a __pycache__, a .git folder."""
    write(os.path.join(folder, "VERSION.txt"), version + "\n")
    write(os.path.join(folder, "nwn_dashboard.py"), f"# dashboard {version}\n")
    write(os.path.join(folder, "nwn_update.py"), "# update\n")
    write(os.path.join(folder, "docs", "MANUAL.md"), f"manual {version}\n")
    write(os.path.join(folder, "tools", "README.txt"), "readme\n")
    write(os.path.join(folder, "run_dashboard.bat"), "@echo off\n")
    write(os.path.join(folder, "__pycache__", "x.pyc"), b"\0")
    write(os.path.join(folder, ".git", "HEAD"), "ref\n")
    write(os.path.join(folder, "nwn_workspace", "settings.json"), "{}")
    return folder


def with_home(t):
    """Point HOME (and USERPROFILE) and the remembered-installs list into t, so nothing outside the test is read or
    written. Returns a function that puts them back."""
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "NWN_SETUP_REGISTRY")}
    os.environ.update(HOME=t, USERPROFILE=t, NWN_SETUP_REGISTRY=os.path.join(t, "cfg", "installs.json"))

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def workspace_rule():
    """Where each kind of copy keeps its work."""
    saved = os.environ.pop("NWN_WORKSPACE", None)
    try:
        with h.tempdir() as t:
            prog = os.path.join(t, "inst", "toolkit")
            os.makedirs(prog)
            check("workspace: an unzipped copy keeps it inside its folder",
                  U.workspace_dir(prog) == os.path.join(prog, "nwn_workspace"))
            write(os.path.join(t, "inst", "install.json"), "{}")
            check("workspace: an installed copy keeps it beside the program (updates never touch it)",
                  U.workspace_dir(prog) == os.path.join(t, "inst", "nwn_workspace") and U.install_root(prog) == os.path.join(t, "inst"))
            os.environ["NWN_WORKSPACE"] = os.path.join(t, "elsewhere")
            check("workspace: NWN_WORKSPACE still wins", U.workspace_dir(prog) == os.path.join(t, "elsewhere"))
    finally:
        if saved is None:
            os.environ.pop("NWN_WORKSPACE", None)
        else:
            os.environ["NWN_WORKSPACE"] = saved


def installing():
    """A new install (bringing work from an old unzipped copy), then an update, then the refusals."""
    with h.tempdir() as t:
        restore = with_home(t)
        try:
            check("default: a folder in your user folder, not Documents", os.path.dirname(S.default_root()) == t and
                  "Documents" not in S.default_root())
            new = release(os.path.join(t, "Downloads", "nwn-toolkit-1.5.0", "nwn-toolkit-1.5.0"))
            old = fake_copy(os.path.join(t, "Downloads", "nwn-toolkit-1.2.0"))
            before_old = tree_hash(old)
            found = S.find_previous(new)
            check("scan: no install yet; the old unzipped copy with work is found",
                  found["installs"] == [] and [c["version"] for c in found["copies"]] == ["vault 1.2.0"], found)
            root = S.default_root()
            p = S.plan_install(root, new, bring_from=old)
            check("plan: a new install, bringing two analyses across", p["kind"] == "new" and not p["refused"] and
                  sorted(c["name"] for c in p["bring"]["copy"] if c["kind"] == "analysis") == ["ModA", "ModB"], p)
            check("plan: nothing written yet", not os.path.exists(root))
            rec = S.run_install(root, new, bring_from=old)
            prog = os.path.join(root, "toolkit")
            files = sorted(os.path.relpath(os.path.join(dp, f), prog) for dp, _d, fs in os.walk(prog) for f in fs)
            check("install: the program, without the unzipped copy's workspace, caches or .git",
                  "nwn_dashboard.py" in files and "VERSION.txt" in files and os.path.join("docs", "MANUAL.md") in files and
                  not any(x.startswith(("nwn_workspace", "__pycache__", ".git")) for x in files), files)
            ws = os.path.join(root, "nwn_workspace")
            check("install: the old copy's work is in the install's workspace; the compiler in its tools",
                  sorted(U.analyses_in(ws)) == ["ModA", "ModB"] and os.path.isfile(os.path.join(prog, "tools", "nwn_script_comp")))
            check("install: the old copy is unchanged", tree_hash(old) == before_old)
            name, _ = S.launcher_for()
            lp = os.path.join(root, name)
            check("install: a start file next to the program (runnable)", os.path.isfile(lp) and (os.name == "nt" or os.access(lp, os.X_OK)))
            info = S.install_info(root)
            check("install: install.json records the version; the install is remembered",
                  info["version"] == "vault 1.5.0" and S.remembered() == [root], (info, S.remembered()))
            saved = os.environ.pop("NWN_WORKSPACE", None)
            check("install: the installed program finds its workspace beside it", U.workspace_dir(prog) == ws)
            if saved is not None:
                os.environ["NWN_WORKSPACE"] = saved
            st = json.load(open(os.path.join(ws, "settings.json"), encoding="utf-8"))
            check("install: the compiler setting points at the installed program's tools folder",
                  st["compiler"] == os.path.join(prog, "tools", "nwn_script_comp"), st["compiler"])
            # ---- an update: a newer release, unzipped somewhere else
            write(os.path.join(ws, "ModA", "edits", "mine.nss"), "// my edit\n")
            newer = release(os.path.join(t, "Desktop", "nwn-toolkit-1.6.0"), "vault 1.6.0 def5678")
            found = S.find_previous(newer)
            check("scan: the install is found (remembered), with its version and analyses",
                  [(i["root"], i["version"], len(i["analyses"])) for i in found["installs"]] == [(root, "vault 1.5.0", 2)], found)
            check("scan: the old copy whose work was brought across is not offered again", found["copies"] == [], found["copies"])
            p = S.plan_install(root, newer)
            check("plan: an update keeps the work and the compiler", p["kind"] == "update" and p["from_version"] == "vault 1.5.0"
                  and p["carry_tools"] == ["nwn_script_comp"] and len(p["analyses"]) == 2 and not p["refused"], p)
            fh = S.hold_dashboard_lock(ws)
            try:
                p2 = S.plan_install(root, newer)
                check("refuse: the installed toolkit is running", p2["refused"] and "running" in p2["refused"], p2)
            finally:
                fh.close()
            lk = nwn_progress.acquire_run_lock(os.path.join(ws, "ModB"))
            try:
                p2 = S.plan_install(root, newer)
                check("refuse: an analysis is running there", p2["refused"] and "ModB" in p2["refused"], p2)
            finally:
                nwn_progress.release_run_lock(lk, os.path.join(ws, "ModB"))
            ws_before = tree_hash(ws)          # after the lock checks above (their lock files are theirs, not the update's)
            calls = dict(n=0)
            try:
                S.run_install(root, newer, stop=lambda: (calls.__setitem__("n", calls["n"] + 1), calls["n"] > 2)[1])
                ok = False
            except InterruptedError:
                ok = True
            check("stop: stopped part-way, the installed program is untouched",
                  ok and "1.5.0" in open(os.path.join(prog, "VERSION.txt")).read() and not os.path.exists(os.path.join(root, S.STAGING)))
            rec = S.run_install(root, newer)
            check("update: the new program is in place", "1.6.0" in open(os.path.join(prog, "VERSION.txt")).read() and rec["kind"] == "update")
            check("update: your work is byte-for-byte as it was", tree_hash(ws) == ws_before)
            check("update: the compiler came with it", os.path.isfile(os.path.join(prog, "tools", "nwn_script_comp")))
            prev = rec["previous"]
            check("update: the old program is kept in previous/ (to go back)", os.path.dirname(prev) == os.path.join(root, "previous")
                  and "1.5.0" in open(os.path.join(prev, "VERSION.txt")).read(), prev)
            hist = json.load(open(os.path.join(root, "install.json"), encoding="utf-8"))
            check("update: install.json keeps the history", [x["kind"] for x in hist["history"]] == ["new", "update"] and
                  hist["version"] == "vault 1.6.0", hist)
            # ---- going back to an older version needs a yes
            p = S.plan_install(root, new)
            check("refuse: an older version over a newer one, unless you say so",
                  p["refused"] and "newer" in p["refused"] and S.plan_install(root, new, allow_older=True)["refused"] is None, p)
            # ---- other refusals
            busy = os.path.join(t, "busy")
            write(os.path.join(busy, "photo.jpg"), b"x")
            check("refuse: a folder that already holds other files", "already holds" in (S.plan_install(busy, newer)["refused"] or ""))
            check("refuse: inside the unzipped copy", "of its own" in (S.plan_install(os.path.join(newer, "inst"), newer)["refused"] or ""))
            check("refuse: from the installed program itself", "IS the installed" in (S.plan_install(root, prog)["refused"] or ""))
            dev = os.path.join(t, "dev")
            write(os.path.join(dev, "nwn_dashboard.py"), "#\n")
            check("refuse: a development copy (no VERSION.txt)", "development copy" in (S.plan_install(os.path.join(t, "x"), dev)["refused"] or ""))
            check("launch: only Windows and macOS start it themselves", S.launch(os.path.join(t, "nowhere")) is False)
        finally:
            restore()


def dashboard():
    """The dashboard: state, the Install card's look-up, plan, and the page."""
    # These run in the source folder (a development copy) and, from make_release, inside a release copy.
    is_release = U.release_info(h.ROOT) is not None
    with h.tempdir() as t:
        restore = with_home(t)                 # the look-up below then reads only this test's folder
        try:
            st = D.GET_ROUTES["/api/state"]({})
            check("state: a release offers to install itself; a development copy doesn't",
                  st["setup"]["release"] is is_release and st["setup"]["installed"] is False)
            s = D.GET_ROUTES["/api/setup/state"]({})
            check("setup state: the look-up runs only for a release", ("installs" in s) is is_release, s)
            target = os.path.join(t, "inst")
            p = D.POST_ROUTES["/api/setup/plan"]({"root": target})
            check("api plan: a release plans a new install (writing nothing); a development copy is refused",
                  (p["kind"] == "new" and not p["refused"] and not os.path.exists(target)) if is_release
                  else "development copy" in (p["refused"] or ""), p)
        finally:
            restore()
    try:
        D.POST_ROUTES["/api/setup/launch"]({"root": h.SESSION})
        ok = False
    except ValueError:
        ok = True
    check("api launch: only for a folder that holds an install", ok)
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the Install card with a folder, Browse, bring-work and Install/Update",
          all(x in html for x in ("async function renderSetup(", 'id="sroot"', 'id="sbrowse"', 'id="sbring"', 'id="sgo"',
                                  "async function installToolkit(", "/api/setup/launch")))


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    for fn in (workspace_rule, installing, dashboard):
        h.run(fn)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
