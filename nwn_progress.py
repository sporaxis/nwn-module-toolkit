"""
nwn_progress.py - progress, time estimates and timing history for long analysis runs.

How it works (plain words):
- Before indexing, the toolkit adds up a *cost* for every file it is about to read (module + haks +
  override). GFF files (areas, blueprints, conversations...) are expensive to parse; big binary files
  (models, textures) mostly cost their size. Cost units are rough milliseconds on a "typical" PC.
- While indexing, the child process prints progress lines (`@@PROGRESS {...}`). The dashboard turns them
  into a percentage and a time-left estimate using the *real* speed seen so far, so the estimate corrects
  itself as the run goes.
- Post-processing and the analysis pass have no file count. Their length is estimated as a ratio of the
  indexing time, learnt from previous runs (`nwn_workspace/timings.json`). With no history, defaults are used.

Two ways an event reaches the screen
------------------------------------
    child process   nwn_analyse.py --progress sets ENABLED; emit() prints "@@PROGRESS {json}" lines on stdout and
                    the dashboard reads them back with parse_line() into a Tracker.
    in-process      a build runs inside the dashboard's own process (its audit re-indexes and re-analyses the clean
                    module). set_hook() routes emit() calls made on that thread straight to a BuildTracker.
When neither is set, emit() does nothing, so the command-line tools print no progress noise.

Run lock and watchdog
---------------------
The process analysing a folder holds an OS lock on <folder>/.run.lock for its whole life. The dashboard checks it
(run_lock_held) before Clear/Delete so it never removes files a live run is writing. The OS drops the lock if the
process dies, so a crash cannot leave a folder locked for ever. watch_parent() makes a child analysis exit when the
dashboard that started it has gone.

What it reads and writes
------------------------
    reads/writes  nwn_workspace/timings.json      (last HISTORY_KEEP finished runs; written via a .tmp + rename)
    creates       <analysis folder>/.run.lock     (empty lock file; stays after release, see release_run_lock)
It never reads or writes a module, hak or game file.

Public entry points
-------------------
    cost(ext, size)                          relative cost of indexing one file (also used by nwn_quickscan)
    emit / stage / set_hook / hooked         send progress events
    acquire_run_lock / release_run_lock / run_lock_held / watch_parent
    load_history / record_run / learnt / estimate / fmt_duration
    Tracker        % and time left for an analysis (parent side)
    BuildTracker   % and time left plus a step list for a build
    parse_line     turn one child output line back into an event

Limits
------
Estimates are rough. Cost units are tuned by hand, and history is per PC, not per module (except build step times,
which the dashboard keeps per analysis). When a phase runs past its estimate the time left is replaced by
"taking longer than estimated" rather than a made-up number.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time

MARK = "@@PROGRESS "    # prefix that tells the dashboard a stdout line is an event, not text for the log pane
ENABLED = False          # set by nwn_analyse.py --progress; every call below is a no-op otherwise

# Extensions stored in BioWare's GFF binary format (every field is decoded and saved, hence the higher cost below).
GFF_EXTS = {
    "are", "git", "gic", "ifo", "uti", "utc", "utp", "utd", "ute", "utm", "uts",
    "utt", "utw", "utg", "dlg", "jrl", "fac", "itp", "bic", "gff", "ptm", "ptt",
    "bti", "btc", "btt", "bts", "bte", "btd", "btp", "btm", "btg",
}
TEXT_EXTS = {"nss", "2da", "set", "txi", "mdl"}   # parsed / token-scanned text-ish files

# Post-processing and analysis time as a fraction of indexing time, when there is no history yet.
DEFAULT_RATIOS = dict(post=0.30, analysis=0.60)
# Default speed (seconds per cost unit) when there is no history. 1 unit ~ 1 ms.
DEFAULT_SEC_PER_UNIT = 0.001
HISTORY_KEEP = 30        # only the most recent runs are kept: the PC or the toolkit may have got faster since


def cost(ext: str, size: int) -> float:
    """Rough relative cost of indexing one file.

    ext:  file extension without the dot, any case ("are", "TGA"); None or "" is treated as "other".
    size: file size in bytes (None counts as 0).
    Returns a fixed cost per file type (GFF 6, text 2, other 0.4) plus 12 units per MB. Pure arithmetic, never raises.
    The numbers are hand-tuned; learnt() converts units to seconds from real runs."""
    ext = (ext or "").lower()
    per_mb = 12.0
    if ext in GFF_EXTS:
        base = 6.0
    elif ext in TEXT_EXTS:
        base = 2.0
    else:
        base = 0.4
    return base + (size or 0) / 1_000_000 * per_mb


# The hook is per thread on purpose: the dashboard runs each job on its own thread in one process, so a hook set by
# a build must only receive events emitted by that build's thread, never those of another job running alongside it.
_LOCAL = threading.local()


def set_hook(cb):
    """In-process progress: events emitted on THIS thread go to cb(event) (used by builds, whose audit re-reads and
    re-analyses the clean module in the dashboard's own process). None removes it.

    cb: a callable taking one dict, e.g. BuildTracker.event. The caller must remove the hook (set_hook(None)) in a
    finally block, as nwn_dashboard.job_build does; otherwise later work on the same thread keeps reporting to it."""
    _LOCAL.cb = cb


def hooked():
    """True if an in-process hook is set on the calling thread (so callers should compute and emit events even though
    ENABLED is False)."""
    return getattr(_LOCAL, "cb", None) is not None


def emit(**ev):
    """Send one progress event, e.g. emit(phase="index", done_units=12.5, done_files=40, source="module").

    If this thread has a hook, the event (with a time stamp "t" added) goes to it and nothing is printed; an error
    in the hook is swallowed. Otherwise, when ENABLED, it is printed as one "@@PROGRESS {json}" line and flushed at
    once so the dashboard sees it straight away. Otherwise it does nothing. Never raises from the hook path."""
    cb = getattr(_LOCAL, "cb", None)
    if cb is not None:
        try:
            cb(dict(ev, t=round(time.time(), 2)))
        except Exception:  # noqa - progress must never break the work
            pass
        return
    if ENABLED:
        ev.setdefault("t", round(time.time(), 2))
        print(MARK + json.dumps(ev, ensure_ascii=False), flush=True)


def stage(phase: str, text: str):
    """Shorthand for emit(phase=phase, stage=text): announce a named step inside a phase ("post", "analysis")."""
    emit(phase=phase, stage=text)


# ------------------------------------------------------------------ run lock + parent watchdog
RUN_LOCK = ".run.lock"   # held (OS lock) by the process analysing a folder, for its whole life


def _lock(fh):
    """Take an exclusive, non-blocking OS lock on the open file. Raises OSError at once if another process holds it.

    Windows: msvcrt.locking locks byte 0 (one byte from the current position, hence the seek(0)).
    Elsewhere: fcntl.flock on the whole file. Both locks are released by the OS when the process exits or dies."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh):
    """Release the lock taken by _lock (same byte on Windows)."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def acquire_run_lock(folder):
    """Lock <folder>/.run.lock for as long as this process lives (the OS drops it if the process dies).
    Returns the open file - keep it. Raises RuntimeError if another process holds it.

    folder: the analysis folder (created if missing). Writes only the empty lock file in it."""
    os.makedirs(folder, exist_ok=True)
    # "a+b" creates the file if needed but never truncates it, so opening it cannot disturb another holder
    fh = open(os.path.join(folder, RUN_LOCK), "a+b")
    try:
        _lock(fh)
    except OSError:
        fh.close()
        raise RuntimeError(f"another analysis is already running on {folder}")
    return fh


def release_run_lock(fh, folder):
    """Release the lock from acquire_run_lock and close the file. The empty .run.lock file is left in place on
    purpose: removing it would open a gap on Linux, where a process that opened the file just before the remove
    locks the old (unlinked) file while the next one creates and locks a new file - two runs on the same folder.
    Never raises on OS errors. `folder` is kept for callers that pass it."""
    try:
        _unlock(fh)
    except OSError:
        pass
    fh.close()


def run_lock_held(folder):
    """True if some live process holds <folder>/.run.lock.

    Tests by trying to take the lock and releasing it at once. A lock file left behind by a crashed run is not held
    (the OS released it), so it returns False. If the file cannot even be opened it answers True: when in doubt,
    treat the folder as busy so Clear/Delete leave it alone."""
    p = os.path.join(folder, RUN_LOCK)
    if not os.path.exists(p):
        return False
    try:
        fh = open(p, "a+b")
    except OSError:
        return True
    try:
        _lock(fh)
        _unlock(fh)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def _parent_alive(pid):
    """True while the process `pid` (our parent, the dashboard) is still running.

    Windows: open the process for SYNCHRONIZE and poll it with a zero timeout. Any open failure other than
    "no such process" (e.g. access denied) counts as alive, so the child never quits on a doubtful answer.
    Elsewhere: when a parent dies its children are re-parented (to init or a subreaper), so getppid() changes."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)            # SYNCHRONIZE
        if not h:
            return k.GetLastError() != 87                      # ERROR_INVALID_PARAMETER = no such process
        try:
            return k.WaitForSingleObject(h, 0) == 0x102        # WAIT_TIMEOUT = still running
        finally:
            k.CloseHandle(h)
    return os.getppid() == pid


def watch_parent(pid, interval=3.0):
    """Child side: exit when the dashboard that started us has gone (closing its window must not leave us running).

    pid: the dashboard's process id (nwn_analyse.py passes NWN_PARENT_PID). interval: seconds between checks.
    Starts a daemon thread and returns at once."""
    def run():
        while True:
            time.sleep(interval)
            if not _parent_alive(pid):
                # os._exit, not sys.exit: sys.exit in a thread only ends that thread. The OS then drops .run.lock,
                # and the .incomplete marker stays, so the half-done analysis is shown as unfinished.
                os._exit(3)
    threading.Thread(target=run, daemon=True, name="parent-watchdog").start()


# ------------------------------------------------------------------ timing history
def history_path(workspace):
    """Path of the timing history file in the workspace folder (nwn_workspace/timings.json)."""
    return os.path.join(workspace, "timings.json")


def load_history(workspace):
    """Past runs as a list of dicts (see record_run). Records without units or index_s are dropped, because both are
    divisors in learnt(). Returns [] if the file is missing or unreadable. Read-only, never raises on bad files."""
    try:
        with open(history_path(workspace), encoding="utf-8") as fh:
            h = json.load(fh)
        return [r for r in h if isinstance(r, dict) and r.get("units") and r.get("index_s")]
    except (OSError, ValueError):
        return []


def record_run(workspace, rec):
    """Append one finished run {module, units, files, index_s, post_s, analysis_s}.

    Adds a time stamp "at", keeps only the last HISTORY_KEEP runs and writes nwn_workspace/timings.json.
    Failing to save is ignored: timing history is a nicety and must never fail an analysis."""
    h = load_history(workspace)
    rec = dict(rec, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    h.append(rec)
    h = h[-HISTORY_KEEP:]
    try:
        os.makedirs(workspace, exist_ok=True)
        # write a temporary file, then rename over the old one: a crash mid-write cannot leave a half-written history
        with open(history_path(workspace) + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(h, fh, indent=1)
        os.replace(history_path(workspace) + ".tmp", history_path(workspace))
    except OSError:
        pass


def learnt(history):
    """Speed and phase ratios from past runs (medians, so one odd run does not skew it).

    history: list from load_history(). Returns dict(sec_per_unit, post, analysis, basis, runs), where post and
    analysis are each phase's length as a fraction of the indexing time and basis is a sentence for the screen.
    A median of 0 (no post or analysis timings recorded) falls back to the defaults."""
    if not history:
        return dict(sec_per_unit=DEFAULT_SEC_PER_UNIT, **DEFAULT_RATIOS, basis="defaults (no previous runs yet)", runs=0)
    spu = statistics.median(r["index_s"] / r["units"] for r in history)
    post = statistics.median((r.get("post_s") or 0) / r["index_s"] for r in history)
    ana = [r["analysis_s"] / r["index_s"] for r in history if r.get("analysis_s")]
    return dict(sec_per_unit=spu, post=post or DEFAULT_RATIOS["post"],
                analysis=statistics.median(ana) if ana else DEFAULT_RATIOS["analysis"],
                basis=f"{len(history)} previous run{'s' if len(history) != 1 else ''} on this PC", runs=len(history))


def estimate(units, history):
    """Up-front estimate (seconds) for a full analysis of `units` cost units.

    units: the sum of cost() over every file to be read. history: list from load_history().
    Returns dict(index_s, post_s, analysis_s, total_s, basis, runs). Used by the quick scan before any run starts."""
    L = learnt(history)
    idx = units * L["sec_per_unit"]
    post, ana = idx * L["post"], idx * L["analysis"]
    return dict(index_s=idx, post_s=post, analysis_s=ana, total_s=idx + post + ana, basis=L["basis"], runs=L["runs"])


def fmt_duration(s):
    """Seconds as short text: "45s", "3m 07s", "1h 05m"; None gives "?"."""
    if s is None:
        return "?"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


# ------------------------------------------------------------------ parent side
class Tracker:
    """Turns progress events into % complete and time left. Feed events with feed(); read snapshot().

    The dashboard feeds events on the job's reader thread while the web server calls snapshot() from another thread,
    with no lock. That is why _enter() records the new phase's start time before switching self.phase."""
    PHASES = ("starting", "index", "post", "analysis", "done")

    def __init__(self, history=(), clock=time.time):
        """history: past runs from load_history(). clock: time source (tests pass a fake clock)."""
        self.clock = clock
        self.L = learnt(list(history))
        self.t0 = clock()
        self.phase = "starting"
        self.stage = "Starting…"
        self.total_units = self.total_files = 0
        self.done_units = self.done_files = 0
        self.source = ""
        self.sources = []
        self.phase_t = {"starting": self.t0}
        self.index_s = self.post_s = self.analysis_s = None

    # -- events
    def feed(self, ev: dict):
        """Take one event dict (from parse_line). "plan" sets the totals; a new phase closes the previous one and
        records its duration; "index" events update the files/units done so far. Unknown phases are ignored."""
        now = self.clock()
        ph = ev.get("phase")
        if ph == "plan":
            self.total_units = ev.get("units") or 0
            self.total_files = ev.get("files") or 0
            self.sources = ev.get("sources") or []
            return
        if ph and ph != self.phase and ph in self.PHASES:
            self._enter(ph, now)
        if "stage" in ev:
            self.stage = ev["stage"]
        if ph == "index":
            self.done_units = ev.get("done_units", self.done_units)
            self.done_files = ev.get("done_files", self.done_files)
            self.source = ev.get("source", self.source)
            self.stage = f"Reading files - {self.source}" if self.source else "Reading files"

    def _enter(self, ph, now):
        """Close the current phase (store how long it took) and start phase `ph` at time `now`."""
        # close the previous phase
        if self.phase == "index":
            self.index_s = now - self.phase_t.get("index", now)
        elif self.phase == "post":
            self.post_s = now - self.phase_t.get("post", now)
        elif self.phase == "analysis":
            self.analysis_s = now - self.phase_t.get("analysis", now)
        self.phase_t[ph] = now       # before self.phase: a concurrent snapshot() must find it
        self.phase = ph
        if ph == "done":
            self.stage = "Done"

    # -- estimates
    def _index_total_est(self, now):
        """Estimated total indexing seconds (observed speed once there is enough data)."""
        if self.index_s is not None:
            return self.index_s
        if not self.total_units:
            return None
        spent = now - self.phase_t.get("index", now) if self.phase == "index" else 0
        frac = self.done_units / self.total_units if self.total_units else 0
        prior = self.total_units * self.L["sec_per_unit"]
        # only trust the observed speed after 2% of the work and 5 seconds: the first files are not typical
        # (module.ifo, small GFFs) and a few seconds of timing noise would swing the estimate wildly
        if self.phase == "index" and frac > 0.02 and spent > 5:
            observed = spent / frac
            # blend: trust observation more as the run goes on (fully from 25% of the units onwards)
            w = min(1.0, frac * 4)
            return w * observed + (1 - w) * prior
        return prior

    def snapshot(self):
        """Current state for the dashboard: dict(phase, stage, pct, elapsed_s, eta_s, eta_text, done_files,
        total_files, basis, over_estimate). pct is None until the plan event gives a size, and is capped at 99 until
        the "done" event (which adds the real timings). Read-only: never changes the tracker."""
        now = self.clock()
        elapsed = now - self.t0
        idx_tot = self._index_total_est(now)
        if idx_tot is None:
            return dict(phase=self.phase, stage=self.stage, pct=None, elapsed_s=elapsed, eta_s=None,
                        eta_text="working out the size…", done_files=self.done_files, total_files=self.total_files,
                        basis=self.L["basis"])
        post_tot = self.post_s if self.post_s is not None else idx_tot * self.L["post"]
        ana_tot = self.analysis_s if self.analysis_s is not None else idx_tot * self.L["analysis"]
        over = False
        if self.phase in ("starting",):
            done = 0.0
        elif self.phase == "index":
            frac = self.done_units / self.total_units if self.total_units else 0
            done = min(idx_tot * frac, idx_tot)
        elif self.phase == "post":
            spent = now - self.phase_t["post"]
            # past 95% of its estimate, stretch the estimate so the phase always looks 95% done: the bar stops
            # creeping towards 100% and the text says "taking longer" instead of showing 0s left
            if spent > post_tot * 0.95:
                over = True
                post_tot = spent / 0.95
            done = idx_tot + spent
        elif self.phase == "analysis":
            spent = now - self.phase_t["analysis"]
            if spent > ana_tot * 0.95:
                over = True
                ana_tot = spent / 0.95
            done = idx_tot + post_tot + spent
        else:  # done
            total = elapsed
            return dict(phase="done", stage="Done", pct=100.0, elapsed_s=elapsed, eta_s=0, eta_text="finished",
                        done_files=self.done_files, total_files=self.total_files, basis=self.L["basis"],
                        timings=dict(index_s=self.index_s, post_s=self.post_s, analysis_s=self.analysis_s, total_s=total))
        total = idx_tot + post_tot + ana_tot
        pct = max(0.0, min(99.0, 100.0 * done / total)) if total else None
        eta = max(0.0, total - done)
        warm = self.phase != "index" or (self.total_units and self.done_units / self.total_units > 0.02
                                           and now - self.phase_t.get("index", now) > 5)
        if over:
            txt = "taking longer than estimated - still working"
        elif not warm and self.L["runs"] == 0:
            txt = f"about {fmt_duration(eta)} left (first guess - firms up in a few seconds)"
        else:
            txt = f"about {fmt_duration(eta)} left"
        return dict(phase=self.phase, stage=self.stage, pct=round(pct, 1) if pct is not None else None,
                    elapsed_s=elapsed, eta_s=eta, eta_text=txt, done_files=self.done_files,
                    total_files=self.total_files, basis=self.L["basis"], over_estimate=over)

    def timings(self):
        """The record to save with record_run(): total units/files and each phase's measured seconds (None for a
        phase that never finished)."""
        return dict(units=self.total_units, files=self.total_files, index_s=self.index_s,
                    post_s=self.post_s, analysis_s=self.analysis_s)


def parse_line(line: str):
    """Parent side: return the event dict if `line` is a progress line, else None.

    A line that starts with the marker but holds broken JSON (e.g. cut off) also gives None. Never raises."""
    if not line.startswith(MARK):
        return None
    try:
        return json.loads(line[len(MARK):])
    except ValueError:
        return None


# ------------------------------------------------------------------ builds
# (key, text shown to the user), in the order nwn_build runs them. The keys must match the progress.step() calls
# in nwn_build and the phase mapping in BuildTracker.event().
BUILD_STEPS = [
    ("snapshot", "Saving the untouched original as version 1 (first build only)"),
    ("files", "Copying the module's files and rewriting references"),
    ("compile", "Compiling edited scripts (and every script that includes an edited include)"),
    ("lean", "Building lean haks (new names)"),
    ("pack", "Packing the clean .mod"),
    ("git", "Saving the version history (git)"),
    ("audit_index", "Audit: re-reading the clean module and its haks"),
    ("audit_post", "Audit: linking everything together"),
    ("audit_analysis", "Audit: re-analysing the clean module"),
    ("audit_checks", "Audit: comparing it with the original, check by check"),
]


class BuildTracker:
    """% done, time left and a plain step list for a build. Step lengths are estimated from the module's last analysis
    (the audit re-reads and re-analyses the clean module, which is most of the time) and from earlier builds of it."""

    def __init__(self, analysis_s=None, prior=None, lean=True, clock=time.time):
        """analysis_s: the module's last analysis record from timings.json (index_s, post_s, analysis_s), or None.
        prior: {step key: seconds} from this analysis's last build, or None; these replace the guesses.
        lean: whether lean haks are built (otherwise that step is left out). clock: time source for tests."""
        self.clock = clock
        self.t0 = clock()
        a = analysis_s or {}
        idx, post, ana = a.get("index_s") or 60.0, a.get("post_s") or 20.0, a.get("analysis_s") or 40.0
        # The audit steps repeat the analysis on the clean module, so they take about as long as the last analysis.
        # The other steps scale with the module's size (indexing time as a proxy); the multipliers are rough guesses.
        guess = dict(snapshot=max(5.0, idx * 0.25), files=max(5.0, idx * 0.15), compile=10.0, lean=max(5.0, idx * 0.4) if lean else 0.0, pack=max(3.0, idx * 0.1),
                     git=max(3.0, idx * 0.15), audit_index=idx, audit_post=post, audit_analysis=ana,
                     audit_checks=max(5.0, ana * 0.2))
        self.est = dict(guess, **{k: v for k, v in (prior or {}).items() if isinstance(v, (int, float))})
        self.based_on = "earlier builds of this module" if prior else (
            "this module's analysis time" if a else "defaults (first build)")
        self.steps = [dict(key=k, text=t, status="waiting", secs=None) for k, t in BUILD_STEPS
                      if k != "lean" or lean]
        self.cur, self.frac, self.detail, self.t_step = None, 0.0, "", self.t0
        self.units = 0

    def step(self, key, detail=""):
        """Start step `key` (a BUILD_STEPS key, or "__done__" from finish()). Repeating the current key only updates
        the detail text. The previous step is marked done with its time; earlier steps never started are "skipped"."""
        now = self.clock()
        if self.cur == key:
            self.detail = detail or self.detail
            return
        for s_ in self.steps:
            if s_["key"] == self.cur:
                s_["status"], s_["secs"] = "done", round(now - self.t_step, 1)
            if s_["key"] == key:
                s_["status"] = "running"
        # steps that were skipped (e.g. no git) count as done
        for s_ in self.steps:
            if s_["key"] == key:
                break
            if s_["status"] == "waiting":
                s_["status"] = "skipped"
        self.cur, self.frac, self.detail, self.t_step = key, 0.0, detail, now

    def part(self, frac, detail=None):
        """Report how far through the current step the build is: frac 0..1 (clamped), optional detail text."""
        self.frac = max(0.0, min(1.0, frac))
        if detail:
            self.detail = detail

    def event(self, ev):
        """Progress events from the audit's re-index / re-analysis (nwn_progress.emit on this thread).

        Installed with set_hook(tracker.event). Maps the analysis phases onto the audit_* build steps; the "checks"
        phase is emitted by nwn_audit itself."""
        ph = ev.get("phase")
        if ph == "plan":
            self.units = ev.get("units") or 0
        elif ph == "index":
            self.step("audit_index", ev.get("source") and f"reading {ev['source']}")
            if self.units:
                self.part((ev.get("done_units") or 0) / self.units)
        elif ph == "post":
            self.step("audit_post", ev.get("stage", ""))
        elif ph == "analysis":
            self.step("audit_analysis", ev.get("stage", ""))
        elif ph == "checks":
            self.step("audit_checks", ev.get("stage", ""))

    def finish(self):
        """Mark the build finished: the running step is done, anything still waiting is skipped."""
        self.step("__done__")
        for s_ in self.steps:
            if s_["status"] in ("running", "waiting"):
                s_["status"] = "done" if s_["status"] == "running" else "skipped"
        self.cur = "__done__"

    def timings(self):
        """{step key: seconds} for the steps that ran; the dashboard saves this as the next build's `prior`."""
        return {s_["key"]: s_["secs"] for s_ in self.steps if s_["secs"] is not None}

    def snapshot(self):
        """Current state for the dashboard: dict(pct, stage, detail, eta_s, eta_text, elapsed_s, steps, step_no,
        step_count). pct is capped at 99 until finish(). Read-only."""
        now = self.clock()
        elapsed = now - self.t0
        if self.cur == "__done__":
            return dict(pct=100.0, stage="Done", eta_text="finished", elapsed_s=elapsed, steps=self.steps, basis=self.based_on)
        keys = [s_["key"] for s_ in self.steps]
        i = keys.index(self.cur) if self.cur in keys else -1
        total = sum(self.est[k] for k in keys)
        done_before = sum(self.est[k] for k in keys[:i]) if i >= 0 else 0.0
        spent = now - self.t_step
        est = self.est.get(self.cur, 0.0)
        over = False
        if i >= 0:
            if self.frac > 0.05 and spent > 3:
                cur_total = spent / self.frac                  # observed speed within this step
            else:
                # no usable fraction: use the estimate, stretched (as in Tracker) once the step runs past 95% of it
                cur_total = max(est, spent / 0.95 if spent > est * 0.95 else est)
            if spent > est * 1.2 and self.frac <= 0.05:
                over = True
            cur_done = min(spent, cur_total)
            left = (cur_total - cur_done) + sum(self.est[k] for k in keys[i + 1:])
            # the bar is measured in estimated seconds, so the current step adds its own share (est) in proportion
            # to how far through it is; a step that runs long cannot push the bar past its share
            done = done_before + min(est, cur_done * est / cur_total if cur_total else 0)
        else:
            left, done = total, 0.0
        pct = max(0.0, min(99.0, 100.0 * done / total)) if total else None
        name = next((s_["text"] for s_ in self.steps if s_["key"] == self.cur), "Starting")
        txt = ("taking longer than estimated - still working" if over else f"about {fmt_duration(left)} left") + \
              (f" (estimate from {self.based_on})" if self.based_on else "")
        return dict(pct=round(pct, 1) if pct is not None else None, stage=name, detail=self.detail, eta_s=left,
                    eta_text=txt, elapsed_s=elapsed, steps=self.steps, step_no=i + 1, step_count=len(keys))
