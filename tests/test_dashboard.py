"""
The dashboard server (nwn_dashboard): request handling over a real loopback HTTP server, atomic JSON writes, the log
monitor lock, the read-only index cache, settings, jobs, the static report export, compiled-code lookup and the
icon resolver (nwn_icons).
    python tests/test_dashboard.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders: builds a tiny module + hak, indexes it into the test workspace, and runs the
dashboard's HTTP handler on a free loopback port.
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from unittest import mock

import nwnlib as n  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_icons  # noqa: E402
from _gff import st, root, loc, item_fields, tga, w, R, X, LST, stand_in_ncs  # noqa: E402
from _harness import check  # noqa: E402

CRASH = h.raiser(OSError("simulated crash"))


# A base item whose pictures are simple (ModelType 0): i<ItemClass>_<ModelPart1 as 3 digits>.tga
BASEITEMS = "2DA V2.0\n\n   label  ItemClass  ModelType  DefaultIcon\n0  sword  wswss      0          iit_sword\n"


def build_module(mod, hak):
    """Module + one hak. Both hold sword.uti (module: ModelPart1 11, hak: 22); the hak also has baseitems.2da and
    the two pictures, so the icon shows which copy was used."""
    os.makedirs(mod, exist_ok=True); os.makedirs(hak, exist_ok=True)
    g = lambda fn, r: w(mod, fn, n.write_gff(r))  # noqa: E731
    g("module.ifo", root("IFO ", Mod_Name=loc("Dashboard Test"), Mod_Entry_Area=(R, "yard"),
                         Mod_Area_list=(LST, [st(6, Area_Name=(R, "yard"))]),
                         Mod_HakList=(LST, [st(8, Mod_Hak=(X, "dash_hak"))])))
    g("yard.are", root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "yard"), Tileset=(R, "tcn01")))
    g("yard.git", root("GIT "))
    f = item_fields("sword", "SWORD", "Sword", base=0)
    g("sword.uti", root("UTI ", **f))
    f2 = dict(f); f2["ModelPart1"] = (n.BYTE, 22)
    w(hak, "sword.uti", n.write_gff(root("UTI ", **f2)))
    # a blueprint whose ModelPart1 is text, not a number (a hand-edited file): must not crash the icon batch
    f3 = dict(f); f3["TemplateResRef"] = (R, "odd"); f3["Tag"] = (X, "ODD"); f3["ModelPart1"] = (X, "abc")
    g("odd.uti", root("UTI ", **f3))
    w(hak, "baseitems.2da", BASEITEMS)
    for nnn in (11, 22):
        w(hak, f"iwswss_{nnn:03d}.tga", tga(4, 4, lambda x, y: (nnn, 0, 0)))
    w(mod, "hello.nss", "void main() { }\n")
    # a compiled script in both: the hak's copy is the one the game runs (haks win over the module)
    w(mod, "hello.ncs", stand_in_ncs("module_hello"))
    w(hak, "hello.ncs", stand_in_ncs("hak_hello"))


class Server:
    """The dashboard's own Handler on a free loopback port, for requests with the real token."""
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None, timeout=5):
        """(status, decoded JSON or text). body: dict -> JSON; bytes/str sent as given. headers override ours."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"X-NWN-Token": D.TOKEN, "Content-Type": "application/json"}
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        if data is not None and "Content-Length" not in (headers or {}):
            h["Content-Length"] = str(len(data))
        h.update(headers or {})
        c.putrequest(method, path, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


def test_content_length(sv):
    # -1 is the value that hung: BufferedReader.read(-1) means "until EOF", so the request thread waited for the
    # client to close (other negatives already raised a ValueError inside read)
    t0 = time.time()
    try:
        code, body = sv.req("POST", "/api/settings", b"{}", headers={"Content-Length": "-1"}, timeout=3)
    except OSError as ex:          # a timeout here is the old behaviour: the thread waits for the client to go away
        code, body = "timeout", ex
    check("Content-Length: a negative value (-1) is a 400 (not a request thread waiting for the client to close)",
          code == 400, (code, body))
    h.timing("Content-Length: the 400 for -1 is answered at once", time.time() - t0, 2.5)
    code, body = sv.req("POST", "/api/settings", b"{}", headers={"Content-Length": "abc"})
    check("Content-Length: a non-number is a 400", code == 400, (code, body))
    code, body = sv.req("POST", "/api/settings", b"{}", headers={"Content-Length": "6000000"})
    check("Content-Length: over the JSON cap is still refused before reading", code == 400 and "large" in str(body), (code, body))


def test_edit_save_base(sv, a, out):
    code, body = sv.req("POST", "/api/edit/save", {"a": a, "file": "hello.nss", "content": "void main() { int x; }\n", "format": "text"})
    check("edit save: a save without base is refused (400), nothing written",
          code == 400 and "base" in body.get("error", "") and not os.path.isfile(os.path.join(out, "edits", "hello.nss")), (code, body))
    code, body = sv.req("POST", "/api/edit/save", {"a": a, "file": "hello.nss", "content": "x", "format": "text", "base": "0" * 64})
    check("edit save: a wrong base is a 409", code == 409, (code, body))
    import hashlib
    sha = hashlib.sha256(b"void main() { }\n").hexdigest()
    code, body = sv.req("POST", "/api/edit/save", {"a": a, "file": "hello.nss", "content": "void main() { int x; }\n", "format": "text", "base": sha})
    check("edit save: the right base saves", code == 200 and body.get("ok"), (code, body))


def test_atomic_writes(sv, a, out):
    # descriptions.json: with os.replace failing (a crash at the last step), the old file must be untouched
    p = os.path.join(out, "descriptions.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"hello": "says hello"}, fh)
    # only nwn_dashboard's own `os` is replaced (the request thread runs dashboard code; nothing else sees it)
    with mock.patch.object(D, "os", h.Proxy(os, replace=CRASH)):
        code, body = sv.req("POST", "/api/description", {"a": a, "script": "hello", "text": "changed"})
    kept = json.load(open(p, encoding="utf-8"))
    check("descriptions.json: written via a temp file, so a failure leaves the old file intact",
          code >= 400 and kept == {"hello": "says hello"}, (code, body, kept))
    for f in os.listdir(out):
        if f.endswith(".tmp"):
            os.remove(os.path.join(out, f))
    code, body = sv.req("POST", "/api/description", {"a": a, "script": "hello", "text": "changed"})
    check("descriptions.json: a normal save lands and leaves no .tmp behind",
          code == 200 and json.load(open(p, encoding="utf-8")) == {"hello": "changed"} and
          not [f for f in os.listdir(out) if f.endswith(".tmp")], (code, body, os.listdir(out)))
    # compile.json (the Compile job): a crash at the last step leaves the previous result whole
    cj = os.path.join(out, "compile.json")
    with open(cj, "w", encoding="utf-8") as fh:
        json.dump({"status": "previous"}, fh)
    with mock.patch.object(D, "os", h.Proxy(os, replace=CRASH)), contextlib.redirect_stdout(io.StringIO()):
        try:
            D.job_compile(a)(None)
            crashed = False
        except OSError:
            crashed = True
    kept = json.load(open(cj, encoding="utf-8"))
    for f in os.listdir(out):
        if f.endswith(".tmp"):
            os.remove(os.path.join(out, f))
    with contextlib.redirect_stdout(io.StringIO()):
        D.job_compile(a)(None)
    new = json.load(open(cj, encoding="utf-8"))
    check("compile.json: a failure at the last step leaves the previous file whole; a normal run replaces it",
          crashed and kept == {"status": "previous"} and new.get("status") == "skipped" and
          not [f for f in os.listdir(out) if f.endswith(".tmp")], (crashed, kept, new))
    # module.ifo edit: a simulated crash leaves no edits/module.ifo (nothing half-written is picked up by the build)
    with mock.patch.object(D, "os", h.Proxy(os, replace=CRASH)):
        try:
            D.point_module_at_hak(a, "dash_hak", "dash_hak_r1")
        except OSError:
            pass
    ed = os.path.join(out, "edits")
    check("module.ifo edit: a failure part-way leaves no edit (and nothing in edits/ ending .tmp)",
          not os.path.isfile(os.path.join(ed, "module.ifo")) and not [f for f in os.listdir(ed) if f.endswith(".tmp")],
          os.listdir(ed))
    for f in os.listdir(out):
        if f.endswith(".tmp"):
            os.remove(os.path.join(out, f))
    r = D.point_module_at_hak(a, "dash_hak", "dash_hak_r1")
    ifo = n.read_gff(open(os.path.join(ed, "module.ifo"), "rb").read())
    check("module.ifo edit: the normal path writes the edit", r["old"] == "dash_hak" and
          ifo.get("Mod_HakList")[0].get("Mod_Hak") == "dash_hak_r1", r)
    os.remove(os.path.join(ed, "module.ifo"))


def test_monitor_lock(sv, a, tmp):
    log = os.path.join(tmp, "nwserverLog1.txt")
    open(log, "w").write("old line\n")
    code, body = sv.req("POST", "/api/logs/start", {"a": a, "paths": [log], "from_end": True})
    check("logs/start: works with from_end on a file that exists", code == 200 and body.get("files") == [log], (code, body))
    # the lock really serialises: two polls at once never both run poll() at the same time
    mon = D.MONITORS[a]
    running, overlap = [0], [False]
    real_poll = mon.poll

    def slow_poll():
        running[0] += 1
        if running[0] > 1:
            overlap[0] = True
        time.sleep(0.2)
        running[0] -= 1
        return real_poll()
    mon.poll = slow_poll
    ths = [threading.Thread(target=lambda: sv.req("GET", f"/api/logs?a={a}")) for _ in range(3)]
    [t.start() for t in ths]; [t.join() for t in ths]
    check("logs: overlapping /api/logs requests poll one at a time", not overlap[0])
    sv.req("POST", "/api/logs/stop", {"a": a})
    gone = os.path.join(tmp, "gone.txt")
    open(gone, "w").write("x\n")
    def getsize(p):
        if p == gone:
            raise OSError(2, "No such file")
        return os.path.getsize(p)
    with mock.patch.object(D, "os", h.Proxy(os, path=h.Proxy(os.path, getsize=getsize))):
        code, body = sv.req("POST", "/api/logs/start", {"a": a, "paths": [gone], "from_end": True})
    check("logs/start: from_end on a log that vanished is a 400, not a crash, and no monitor is left half set up",
          code == 400 and a not in D.MONITORS, (code, body, list(D.MONITORS)))


def test_read_only_index(a, out, tmp):
    g, db = D.get_graph(a)
    try:
        db.execute("CREATE TABLE zz(x)")
        wrote = True
    except Exception:  # noqa - "attempt to write a readonly database"
        wrote = False
    check("get_graph: the cached connection refuses writes", not wrote)
    # the cached connection works from another thread (request threads share it under LOCK)
    res = []
    th = threading.Thread(target=lambda: res.append(D.get_graph(a)[1].execute("SELECT count(*) FROM files").fetchone()[0]))
    th.start(); th.join()
    check("get_graph: the cached connection is usable from a request thread", res and res[0] > 0, res)
    # a mistyped folder name must not get an empty index.sqlite from the name check
    nm = D.analysis_name_for(os.path.join(tmp, "typo_module"), "typo_" + a)
    check("analysis_name_for: never creates an index for a folder that has none",
          not os.path.exists(os.path.join(D.WORKSPACE, "typo_" + a, "index.sqlite")), nm)


def test_settings_copy():
    with mock.patch.object(D, "SETTINGS_FILE", os.path.join(D.WORKSPACE, "no_such_settings.json")):
        s = D.settings()
        s["log_paths"].append("C:/x")
        s["external_tools"].append({"name": "t", "path": "p"})
        check("DEFAULT_SETTINGS: a settings() result shares nothing with the defaults",
              D.DEFAULT_SETTINGS["log_paths"] == [] and D.DEFAULT_SETTINGS["external_tools"] == [] and
              D.settings()["log_paths"] == [], D.DEFAULT_SETTINGS)


def test_jobs_pruned():
    with D.LOCK:
        old_done = D.Job("compile", "zz_old"); old_done.status = "done"; old_done.started = time.time() - 7 * 3600
        old_run = D.Job("analyse", "zz_run"); old_run.started = time.time() - 48 * 3600     # still running
        fresh = D.Job("build", "zz_new"); fresh.status = "error"
        for j in (old_done, old_run, fresh):
            D.JOBS[j.id] = j
    jid = D.run_job("compile", lambda job: "ok", key="zz_prune_test")
    for _ in range(50):
        if D.JOBS[jid].status != "running":
            break
        time.sleep(0.05)
    check("JOBS: finished jobs older than JOB_KEEP_S are dropped when the next job starts; running and recent ones stay",
          old_done.id not in D.JOBS and old_run.id in D.JOBS and fresh.id in D.JOBS and jid in D.JOBS,
          [(j.kind, j.status) for j in D.JOBS.values()])
    with D.LOCK:
        for j in (old_run, fresh):
            D.JOBS.pop(j.id, None)
        D.JOBS.pop(jid, None)




def test_icons(out):
    r = nwn_icons.IconResolver(out, None)
    try:
        ic = r.icon("bp:sword.uti")
        check("icons: a blueprint in the module and a hak uses the hak's copy (the one the game loads), not the lowest id",
              ic.get("parts", [None])[0] == 22 and ic.get("found") and ic["layers"][0]["name"] == "iwswss_022.tga", ic)
        odd = r.icon("bp:odd.uti")
        check("icons: a ModelPart that is not a number reads as 0 instead of crashing", odd.get("parts") == [0, 0, 0], odd)
        # folder source whose file goes away between the index and the read: None, not an OSError
        with mock.patch.object(nwn_icons, "os", h.Proxy(os, path=h.Proxy(os.path, isfile=lambda p: True))):
            try:
                got = r.get("hello.nss.vanished")
            except OSError as ex:
                got = ex
        check("icons: an unreadable file in a folder source is 'not found', never an OSError", got is None, got)
        # many threads on one resolver: no exception and the same answers
        errs, outs = [], []

        def worker():
            try:
                r._cache.clear()
                outs.append(r.icon("bp:sword.uti")["parts"][0])
            except Exception as ex:  # noqa
                errs.append(repr(ex))
        ths = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ths]; [t.join() for t in ths]
        check("icons: eight threads sharing one resolver get the same answer with no error", not errs and set(outs) == {22}, (errs, outs))
    finally:
        r.close()


def static_payload(html):
    """The data an exported report embeds (window.NWN_STATIC=...;), parsed."""
    m = re.search(r"window\.NWN_STATIC=(.*?);</script>", html, re.S)
    return json.loads(m.group(1)) if m else None


def test_export(a, out):
    """Static export works without the impact_detail table, within its caps, and through --export."""
    try:
        html = D.export_static(a)
        data = static_payload(html)
    except Exception as ex:  # noqa - it crashed on "no such table: impact_detail"
        html, data = repr(ex), None
    imp = (data or {}).get("impact") or {}
    some = next(iter(imp.values()), None)
    check("export: impact chains are worked out from the graph (no impact_detail table)",
          data is not None and imp and isinstance(some, dict) and "affected" in some and "level" in some, str(html)[:300])
    check("export: every exported chain has the report's level",
          data is not None and all(imp[k]["level"] == data["report"]["impact"][k]["level"] for k in imp),
          [(k, imp[k]["level"], data["report"]["impact"][k]["level"]) for k in imp][:5] if data else None)
    with mock.patch.object(D, "EXPORT_DETAIL_MAX_ITEMS", 1):
        data = static_payload(D.export_static(a))
    n_rep = len(data["report"].get("impact") or {}) if data else 0
    check("export: the detail cap (items) is applied and the report says what was left out",
          data and len(data["impact"]) == 1 and n_rep > 1 and str(n_rep - 1) in (data.get("note") or ""),
          (data and len(data["impact"]), n_rep, data and data.get("note")))
    with contextlib.redirect_stdout(io.StringIO()) as so:
        rc = D.main(["--export", a])
    path = os.path.join(D.WORKSPACE, f"{a}_report.html")
    ok = rc == 0 and os.path.isfile(path) and static_payload(open(path, encoding="utf-8").read()) is not None
    check("export: --export writes the report file and exits 0", ok, (rc, so.getvalue()[-300:]))
    if os.path.isfile(path):
        os.remove(path)


def test_graph_cache(a, out):
    """Opening a third analysis releases the oldest cached graph (only the two most recent are kept)."""
    names = [a + "_g2", a + "_g3"]
    for nm in names:
        shutil.copytree(out, os.path.join(D.WORKSPACE, nm), ignore=shutil.ignore_patterns("edits", "build"))
    try:
        _g, db1 = D.get_graph(a)
        D.get_graph(names[0])
        D.get_graph(names[1])
        keys = sorted(D.GRAPHS.items)
        try:
            db1.execute("SELECT 1")
            closed = False
        except Exception:  # noqa - sqlite3.ProgrammingError: Cannot operate on a closed database
            closed = True
        check("get_graph: only the two most recently opened analyses keep their graph; the oldest is closed",
              keys == sorted(names) and closed, (keys, closed))
        _g, db1b = D.get_graph(a)
        check("get_graph: an analysis whose graph was released opens again", db1b.execute("SELECT 1").fetchone() == (1,))
    finally:
        for nm in names:
            D.release_analysis(nm)
            shutil.rmtree(os.path.join(D.WORKSPACE, nm), ignore_errors=True)


def test_compiled_code_order(a):
    r = D.compiled_code(a, "hello")
    check("compiled code: shows the .ncs the game runs (the hak's copy wins over the module's)",
          r.get("found") and str(r.get("where", "")).endswith("(hak)"), r)
    hak = D.get_graph(a)[1].execute("SELECT path FROM sources WHERE kind='hak'").fetchone()[0]
    check("compiled code: the bytes checked are that source's copy (read_module_file with a source)",
          D.read_module_file(a, "hello.ncs", hak) == stand_in_ncs("hak_hello"))


def test_quickscan_override_text():
    html = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("dashboard.html: the quick scan's override line says the module and haks win over it, with override_hidden",
          "the module and its haks win over it" in html and "c.override_hidden" in html and "override_replaces" not in html
          and "loads on top of everything" not in html)
    check("dashboard.html: module files are 'hidden by a hak' (the override never hides them)",
          "hidden by a hak or the override" not in html and "hidden by a hak" in html)


def test_install_endpoints(sv, a, out, tmp):
    """Add to game folders over HTTP: a clear refusal with no user folder, field checks (typed ADD, the plan the user
    saw, the second confirmation for FAIL, the session token), then add and undo as jobs."""
    import nwn_build
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_build.build(out, {"delete": [], "merge": [], "git": False}, name="dash clean", verbose=False,
                        audit_kwargs=dict(compiler=h.NO_COMPILER))
    keep = D.settings()
    user = os.path.join(tmp, "dash user"); mods = os.path.join(user, "modules")
    os.makedirs(mods); os.makedirs(os.path.join(user, "hak"))

    def wait(job):
        for _ in range(200):
            code_, j = sv.req("GET", f"/api/job?id={job}")
            if j.get("status") != "running":
                return j
            time.sleep(0.05)
        return {}
    try:
        D.save_settings(dict(keep, nwn_user=""))
        code, body = sv.req("GET", f"/api/install/plan?a={a}")
        check("install plan: no NWN user folder set is a 400 that says to set it in Settings",
              code == 400 and "Settings" in body.get("error", ""), (code, body))
        D.save_settings(dict(keep, nwn_user=user))
        code, p = sv.req("GET", f"/api/install/plan?a={a}")
        check("install plan: the .mod goes to the modules folder, with a plan id",
              code == 200 and [os.path.basename(f["target"]) for f in p["files"]] == ["dash clean.mod"] and p["plan_id"],
              (code, p))
        bad = [sv.req("POST", "/api/install/add", {"a": a, "confirm": "add", "plan_id": p["plan_id"]}),
               sv.req("POST", "/api/install/add", {"a": a, "confirm": "ADD", "plan_id": "0" * 16}),
               sv.req("POST", "/api/install/add", {"a": a, "confirm": "ADD", "plan_id": p["plan_id"], "allow_fail": True}),
               sv.req("POST", "/api/install/add", {"a": a, "confirm": "ADD", "plan_id": p["plan_id"], "allow_fail": "yes"}),
               sv.req("POST", "/api/install/add", {"confirm": "ADD", "plan_id": p["plan_id"]})]
        check("install add: wrong confirmation, another plan, FAIL without its second confirmation, a bad field type and "
              "a missing analysis are all 400s, and nothing is copied",
              [c for c, _ in bad] == [400] * 5 and not os.listdir(mods), bad)
        code, body = sv.req("POST", "/api/install/add", {"a": a, "confirm": "ADD", "plan_id": p["plan_id"]},
                            headers={"X-NWN-Token": "wrong"})
        check("install add: refused without the session token", code == 401 and not os.listdir(mods), (code, body))
        code, body = sv.req("POST", "/api/install/add", {"a": a, "confirm": "ADD", "plan_id": p["plan_id"]})
        j = wait(body.get("job"))
        code2, st_ = sv.req("GET", f"/api/install/state?a={a}")
        check("install add: runs as a job, copies the .mod and records it",
              code == 200 and j.get("status") == "done" and os.listdir(mods) == ["dash clean.mod"] and
              st_.get("status") == "added", (code, body, j, st_))
        code, body = sv.req("POST", "/api/install/undo", {"a": a})
        j = wait(body.get("job"))
        code2, st_ = sv.req("GET", f"/api/install/state?a={a}")
        check("install undo: runs as a job and removes the file it added",
              j.get("status") == "done" and not os.listdir(mods) and st_.get("status") == "undone", (j, st_))
    finally:
        D.save_settings(keep)


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    a = "dashtest"
    sv = None
    with h.tempdir("nwn_dash_") as tmp:
        try:
            mod, hak = os.path.join(tmp, "mod"), os.path.join(tmp, "haks", "dash_hak")
            out = os.path.join(D.WORKSPACE, a)                # the analysis lives in the test workspace
            build_module(mod, hak)
            h.analyse(mod, out, [hak], write_json=False)
            # the Compile job reads the settings: point it at NO_COMPILER so a compiler on this PC is never used
            D.save_settings(dict(D.settings(), compiler=h.NO_COMPILER))
            sv = Server()
            for fn, args in ((test_content_length, (sv,)), (test_edit_save_base, (sv, a, out)),
                             (test_atomic_writes, (sv, a, out)), (test_monitor_lock, (sv, a, tmp)),
                             (test_read_only_index, (a, out, tmp)), (test_settings_copy, ()), (test_jobs_pruned, ()),
                             (test_icons, (out,)), (test_export, (a, out)), (test_graph_cache, (a, out)),
                             (test_compiled_code_order, (a,)), (test_quickscan_override_text, ()),
                             (test_install_endpoints, (sv, a, out, tmp))):
                h.run(fn, *args)
        finally:
            if sv:
                sv.close()
            D.release_analysis(a)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
