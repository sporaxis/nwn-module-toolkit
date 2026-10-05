"""
Quick scan, progress/ETA, Stop and Clear, the run lock, Delete (housekeeping) - with real analysis child processes
started the way the dashboard starts them.
    python tests/test_progress.py          (last line "N/M checks passed"; exit code 1 on a failure)
    The slowest suite (an 8000-file module is analysed, stopped and finished); tests/run_all.py --fast leaves it out.
Works only in a temporary folder and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import os
import shutil
import subprocess
import sys
import time

import nwnlib as n  # noqa: E402
import nwn_progress  # noqa: E402
import nwn_quickscan  # noqa: E402
import nwn_index  # noqa: E402
import nwn_dashboard as dash  # noqa: E402
from make_hakconflict_module import build as build_hak  # noqa: E402
from make_test_module import build as build_mod  # noqa: E402
from _harness import check, pack_hak as pack  # noqa: E402

ROOT = h.ROOT
TMP = None          # this run's temporary folder, set by main()


def wait(job_id, timeout=120):
    t = time.time()
    while dash.JOBS[job_id].status == "running" and time.time() - t < timeout:
        time.sleep(0.1)
    return dash.JOBS[job_id]


def progress_checks():
    """Everything in order: each step uses what the one before it left (modules, analyses, jobs)."""
    # ---------------------------------------------------------------- quick scan
    mod = os.path.join(TMP, "hc_mod")
    user = os.path.join(TMP, "user")
    build_hak(mod, os.path.join(TMP, "hak_a"), os.path.join(TMP, "hak_b"))
    os.makedirs(os.path.join(user, "hak"))
    pack(os.path.join(TMP, "hak_a"), os.path.join(user, "hak", "hak_a.hak"))
    # hak_b also carries a copy of a module file -> the module's own copy is hidden in game
    shutil.copy(os.path.join(mod, "guard.utc"), os.path.join(TMP, "hak_b", "guard.utc"))
    pack(os.path.join(TMP, "hak_b"), os.path.join(user, "hak", "hak_b.hak"))
    ifo = n.read_gff_file(os.path.join(mod, "module.ifo"))
    ifo.set("Mod_CustomTlk", n.CEXOSTRING, "nosuchtlk")
    ifo.fields["Mod_HakList"].value.append(dash_st(n, "hak_gone"))
    with open(os.path.join(mod, "module.ifo"), "wb") as fh:
        fh.write(n.write_gff(ifo))
    t0 = time.time()
    r = nwn_quickscan.scan(mod, nwn_user=user, workspace=os.environ["NWN_WORKSPACE"])
    h.timing("quick scan is quick (tiny module < 2 s)", time.time() - t0, 2)
    names = [(h["name"], h["status"]) for h in r["haks"]]
    check("haks listed in module.ifo order with found/missing", names[:3] == [("hak_a", "found"), ("hak_b", "found"),
                                                                             ("hak_gone", "MISSING")], names)
    clash = {c["table"]: c for c in r["conflicts"]["twoda_in_several_haks"]}
    check("2da in two haks: first listed wins", "appearance.2da" in clash and clash["appearance.2da"]["wins"] == "hak_a"
          and clash["appearance.2da"]["hidden"] == ["hak_b"], clash)
    check("module file hidden by a hak is reported", r["conflicts"]["module_hidden"] == 1 and
          "guard.utc" in r["conflicts"]["module_hidden_examples"], r["conflicts"])
    txt = " | ".join(w["text"] for w in r["warnings"])
    check("missing hak and missing custom tlk are warnings", "hak_gone" in txt and "nosuchtlk.tlk" in txt, txt)
    check("sizes: player download = haks + tlk", r["sizes"]["player_download"] == r["sizes"]["haks"] + r["sizes"]["tlk"])
    check("text TL;DR renders", "first listed wins" in nwn_quickscan.format_text(r))
    check("quick scan wrote nothing", not os.path.exists(os.environ["NWN_WORKSPACE"]))

    # ---------------------------------------------------------------- progress events vs the real index
    nwn_progress.ENABLED = True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            nwn_index.run_index(mod, [], [], None, os.path.join(TMP, "idx"), False, False, nwn_user=user)
    finally:
        nwn_progress.ENABLED = False
    evs = [nwn_progress.parse_line(x) for x in buf.getvalue().splitlines()]
    evs = [e for e in evs if e]
    plan = next((e for e in evs if e["phase"] == "plan"), None)
    last = [e for e in evs if e["phase"] == "index"][-1]
    check("plan emitted before indexing", plan is not None and evs[0]["phase"] == "plan")
    check("planned file count == files indexed", plan and plan["files"] == last["done_files"], (plan, last))
    check("planned cost == cost indexed (within rounding)", plan and abs(plan["units"] - last["done_units"]) <= 1, (plan, last))
    check("post-processing stages reported", any(e["phase"] == "post" and "stage" in e for e in evs))
    check("progress off by default (no lines)", "@@PROGRESS" not in run_quiet(mod, user))

    # ---------------------------------------------------------------- tracker maths (fake clock)
    now = [1000.0]
    tr = nwn_progress.Tracker([], clock=lambda: now[0])
    tr.feed(dict(phase="plan", units=10000, files=1000))
    tr.feed(dict(phase="index", done_units=0, done_files=0))
    now[0] += 20; tr.feed(dict(phase="index", done_units=2500, done_files=250, source="big.hak"))
    s1 = tr.snapshot()
    now[0] += 20; tr.feed(dict(phase="index", done_units=5000, done_files=500, source="big.hak"))
    s2 = tr.snapshot()
    check("percent rises and time left falls", s2["pct"] > s1["pct"] and s2["eta_s"] < s1["eta_s"], (s1, s2))
    check("observed speed drives the estimate (40 s for half -> ~80 s index)", 60 < tr._index_total_est(now[0]) < 100,
          tr._index_total_est(now[0]))
    now[0] += 40; tr.feed(dict(phase="post", stage="Linking"))
    now[0] += 500
    s3 = tr.snapshot()
    check("overrunning phase says so and never shows 100% early", s3["over_estimate"] and s3["pct"] < 100, s3)
    tr.feed(dict(phase="analysis", stage="x")); now[0] += 10; tr.feed(dict(phase="done"))
    s4 = tr.snapshot()
    t = tr.timings()
    check("done = 100% and phase timings captured", s4["pct"] == 100 and t["index_s"] == 80 and t["post_s"] == 500
          and t["analysis_s"] == 10, (s4, t))
    ws = os.path.join(TMP, "hist")
    nwn_progress.record_run(ws, dict(module="m", units=10000, files=1000, index_s=100, post_s=20, analysis_s=50))
    e = nwn_progress.estimate(20000, nwn_progress.load_history(ws))
    check("history: estimate scales with size (2x units -> 2x time)", abs(e["total_s"] - 340) < 1 and e["runs"] == 1, e)

    # ---------------------------------------------------------------- dashboard job: finish, Stop, Clear
    small = os.path.join(TMP, "small_mod")
    build_mod(small)
    jid = dash.run_job("analyse", dash.job_analyse(small, [], "", None), key=dash.analysis_name_for(small))
    j = wait(jid)
    nm = (j.result or {}).get("name")
    check("analysis runs in a child process and finishes", j.status == "done" and nm, j.lines[-5:])
    d = os.path.join(dash.WORKSPACE, nm or "x")
    check("finished analysis is complete (no marker) and opens", not os.path.exists(os.path.join(d, dash.INCOMPLETE))
          and dash.analysis_dir(nm) == d)
    snap = j.tracker.snapshot()
    check("job progress reached 100%", snap["pct"] == 100, snap)
    check("timing history recorded", len(nwn_progress.load_history(dash.WORKSPACE)) == 1)
    try:
        dash.clear_analysis(nm); refused = False
    except ValueError:
        refused = True
    check("Clear refuses a complete analysis", refused)

    # a slower module to stop part-way: many copies of the blueprints
    slow = os.path.join(TMP, "slow_mod")
    shutil.copytree(small, slow)
    src = [f for f in os.listdir(slow) if f.endswith((".uti", ".utc", ".dlg", ".utp"))]
    for i in range(8000):
        f = src[i % len(src)]
        shutil.copy(os.path.join(slow, f), os.path.join(slow, f"cp{i:05d}.{f.rsplit('.', 1)[1]}"))
    key = dash.analysis_name_for(slow)
    sd = os.path.join(dash.WORKSPACE, key)
    os.makedirs(os.path.join(sd, "edits"), exist_ok=True)       # your own work must survive Clear
    open(os.path.join(sd, "edits", "mine.nss"), "w").write("void main(){}")
    open(os.path.join(sd, "descriptions.json"), "w").write("{}")
    jid = dash.run_job("analyse", dash.job_analyse(slow, [], "", None), key=key)
    t = time.time()
    while time.time() - t < 60 and not (dash.JOBS[jid].tracker and dash.JOBS[jid].tracker.done_files > 200):
        time.sleep(0.05)
    try:
        dash.run_job("analyse", dash.job_analyse(slow, [], "", None), key=key); second = True
    except ValueError:
        second = False
    check("a second analysis of the same module is refused while one runs", not second)
    try:
        dash.clear_analysis(key); refused = False
    except ValueError:
        refused = True
    check("Clear refuses while the analysis runs", refused)
    t_stop = time.time()
    dash.stop_job(jid)
    j = wait(jid, 15)
    check("Stop ends the analysis (status stopped)", j.status == "stopped", (j.status, j.lines[-3:]))
    h.timing("Stop ends the analysis within seconds", time.time() - t_stop, 10)
    check("stopped analysis is listed as incomplete", any(a["name"] == key and a.get("incomplete") for a in dash.list_analyses()))
    try:
        dash.analysis_dir(key); blocked = False
    except ValueError:
        blocked = True
    check("an incomplete analysis cannot be opened", blocked)
    res = dash.clear_analysis(key)
    left = sorted(os.listdir(sd))
    check("Clear removes generated files right after Stop", "index.sqlite" in res["removed"] and not res["failed"], res)
    check("Clear keeps edits/ and descriptions.json", left == ["descriptions.json", "edits"], left)

    # an unfinished analysis written by someone else a moment ago is not cleared from under them
    other = os.path.join(dash.WORKSPACE, "other_run")
    os.makedirs(other)
    open(os.path.join(other, "index.sqlite"), "w").write("x")
    try:
        dash.clear_analysis("other_run"); refused = False
    except ValueError as ex:
        refused = "still running" in str(ex)
    check("Clear refuses files another process wrote in the last minute", refused)
    old = time.time() - 600
    os.utime(os.path.join(other, "index.sqlite"), (old, old))
    r = dash.clear_analysis("other_run")
    check("...and clears them once they are stale (folder removed when empty)", r["folder_removed"], r)

    # ---------------------------------------------------------------- the .incomplete marker, locks, races, case
    jid = dash.run_job("analyse", dash.job_analyse(slow, [], "", None), key=key)
    t = time.time()
    while time.time() - t < 60 and not (dash.JOBS[jid].tracker and dash.JOBS[jid].tracker.done_files > 200):
        time.sleep(0.05)
    check("the running analysis holds the folder lock", nwn_progress.run_lock_held(sd))
    cli = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_analyse.py"), slow, "--name", key],
                         env=dict(os.environ), capture_output=True, text=True, timeout=120)
    check("a second process on the same folder is refused (exit 4)", cli.returncode == 4, cli.stdout[-300:])
    try:
        dash.run_job("analyse", dash.job_analyse(slow, [], "", None), key=key.upper()); dup = True
    except ValueError:
        dup = False
    check("job keys compare case-insensitively (Windows folders)", not dup)
    dash.stop_job(jid); wait(jid, 15)
    check("lock released when the process is killed", not nwn_progress.run_lock_held(sd))
    cli = subprocess.run([sys.executable, os.path.join(ROOT, "nwn_analyse.py"), slow, "--name", key, "--no-json"],
                         env=dict(os.environ), capture_output=True, text=True, timeout=300)
    check("finishing a stopped run from the command line marks it complete", cli.returncode == 0 and
          dash.analysis_status(sd) == "complete", (cli.returncode, cli.stdout[-300:]))
    try:
        dash.clear_analysis(key); refused = False
    except ValueError:
        refused = True
    check("...so Clear can no longer remove it", refused)

    jid = dash.run_job("analyse", dash.job_analyse(small, [], "", None), key=dash.analysis_name_for(small))
    t = time.time()
    while dash.JOBS[jid].proc is None and time.time() - t < 30:
        time.sleep(0.01)
    dash.JOBS[jid].stop_requested = True          # Stop 'arrives' but the process still finishes normally (rc 0)
    j = wait(jid)
    check("a run that finishes (exit 0) is done even if Stop came in its last moment",
          j.status == "done" and dash.analysis_status(os.path.join(dash.WORKSPACE, j.result["name"])) == "complete",
          (j.status, j.lines[-3:]))

    # watchdog: the child exits when the dashboard that started it has gone
    starter = subprocess.run([sys.executable, "-c", (
        "import subprocess, sys, os\n"
        f"p = subprocess.Popen([sys.executable, {os.path.join(ROOT, 'nwn_analyse.py')!r}, {slow!r}, '--name', 'orphan', '--progress'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, NWN_PARENT_PID=str(os.getpid())))\n"
        "print(p.pid)")], capture_output=True, text=True, env=dict(os.environ), timeout=60)
    child = int(starter.stdout.strip())
    t = time.time()
    while time.time() - t < 15 and pid_alive(child):
        time.sleep(0.2)
    check("orphaned analysis process exits by itself (parent watchdog)", not pid_alive(child), time.time() - t)

    # quick scan: loose hak-name matches keep their module.ifo position
    loose = os.path.join(TMP, "loose"); os.makedirs(loose)
    shutil.copy(os.path.join(user, "hak", "hak_a.hak"), os.path.join(loose, "hak_a_v2.hak"))
    r = nwn_quickscan.scan(mod, [os.path.join(loose, "hak_a_v2.hak")], nwn_user=os.path.join(TMP, "nouser"))
    check("loosely matched hak keeps its load-order slot", r["haks"][0]["name"] == "hak_a" and r["haks"][0]["status"] == "found"
          and r["haks"][0]["file"] == "hak_a_v2.hak", [(h["name"], h["status"]) for h in r["haks"]])

    # ---------------------------------------------------------------- Delete (housekeeping)
    u = dash.analysis_usage(key)
    check("usage splits data vs your work", u["parts"]["data"]["bytes"] > 0 and u["parts"]["edits"]["files"] == 1
          and "descriptions.json" in u["parts"]["descriptions"]["items"], u["parts"])
    os.makedirs(os.path.join(sd, "build")); open(os.path.join(sd, "build", "x.mod"), "wb").write(b"x")
    open(os.path.join(sd, "notes.txt"), "w").write("mine")
    try:
        dash.delete_analysis(key, ["edits"], confirm="wrong"); refused = False
    except ValueError:
        refused = True
    check("deleting your edits needs the analysis name typed", refused)
    r = dash.delete_analysis(key)
    check("Delete (default) removes the data of a complete analysis, keeps edits/build/descriptions/other",
          sorted(os.listdir(sd)) == ["build", "descriptions.json", "edits", "notes.txt"] and not r["failed"], os.listdir(sd))
    r = dash.delete_analysis(key, ["edits", "build", "descriptions", "other"], confirm=key)
    check("Delete with everything ticked removes the folder", r["folder_removed"] and not os.path.exists(sd), r)
    try:
        dash.delete_analysis("..", confirm=".."); refused = False
    except ValueError:
        refused = True
    check("Delete rejects path-like names", refused)

    # ---------------------------------------------------------------- regression: issues.csv after the catalogue
    import nwn_analysis
    out = os.path.join(TMP, "idx")
    with contextlib.redirect_stdout(io.StringIO()) as o:
        nwn_analysis.run_analysis(out)
    csv_text = open(os.path.join(out, "reports", "issues.csv"), encoding="utf-8-sig").read()
    check("catalogue issues reach issues.csv (no 'catalogue failed')", "2da_hak_vs_hak" in csv_text
          and "catalogue failed" not in o.getvalue(), o.getvalue()[-300:])



def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    global TMP
    with h.tempdir("nwn_prog_") as tmp:
        TMP = tmp
        h.run(progress_checks)
    return h.summary()


def dash_st(n_, hak):
    s = n_.GffStruct(8)
    s.set("Mod_Hak", n_.CEXOSTRING, hak)
    return s


def pid_alive(pid):
    """True while process `pid` is running (Windows, Linux and macOS)."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        hp = k.OpenProcess(0x00100000, False, pid)
        if not hp:
            return False
        try:
            return k.WaitForSingleObject(hp, 0) == 0x102
        finally:
            k.CloseHandle(hp)
    if os.path.isdir("/proc"):                   # Linux: a zombie (exited, not yet reaped) counts as gone
        try:
            with open(f"/proc/{pid}/status") as fh:
                return "State:\tZ" not in fh.read()
        except OSError:
            return False
    try:                                         # macOS and other POSIX systems: signal 0 only asks "does it exist?"
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:                      # it exists but belongs to someone else
        return True


def run_quiet(mod, user):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        nwn_index.run_index(mod, [], [], None, os.path.join(TMP, "idx2"), False, False, nwn_user=user)
    return buf.getvalue()


if __name__ == "__main__":
    sys.exit(main())
