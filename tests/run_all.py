"""
run_all.py - run every test suite, each in its own process, and print one table.

    python tests/run_all.py                    every suite (tests/run_tests.py and tests/test_*.py)
    python tests/run_all.py --only tools       only test_tools.py (--only may be repeated, or given "a,b")
    python tests/run_all.py --fast             leave out the slow suite (test_progress.py)
    python tests/run_all.py --timing           also check the time and memory limits (NWN_TEST_TIMING=1)

Why this exists: one command that proves the whole toolkit still works, judged the same way every time. Each suite
runs in a new Python process with:
  - NWN_WORKSPACE pointing at an empty temporary folder, and NWN_SCRIPT_COMP at a path that does not exist, so no
    suite can touch the real nwn_workspace or use a script compiler installed on this computer (the suites also set
    both themselves, through tests/_harness.py);
  - its own temporary folder as TMPDIR / TEMP / TMP: whatever is still in it when the suite ends was left behind,
    and that fails the suite.
A suite passes only when it exits 0, prints its summary line ("N/M checks passed", from tests/_harness.py) with
N == M and M > 0, finishes within the time limit, and leaves no temporary files. SKIP lines (checks that cannot run
on this system, or limits checked only with --timing) are listed under the table; they do not fail a run.

Exit code: 0 when every suite passed, 1 otherwise (also when --only matches nothing). make_release.py runs this file
inside a release copy and judges the release by its exit code.
Writes only temporary folders, removed again at the end. Standard library only (Python 3.8+).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SLOW = {"test_progress"}                 # left out by --fast (it analyses, stops and resumes an 8000-file module)
SUMMARY_RE = re.compile(r"^(\d+)/(\d+) checks passed\s*$", re.M)


def suites():
    """The suite files, in run order: run_tests.py first (the end-to-end self-test), then test_*.py by name."""
    found = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    return (["run_tests.py"] if os.path.isfile(os.path.join(HERE, "run_tests.py")) else []) + found


def run_suite(fn, timing, timeout):
    """Run one suite file in its own process. Returns a dict: name, ok, passed, total, skips [lines], fails [lines],
    seconds, note (why it failed), tail (the end of its output, for a failure)."""
    name = fn[:-3]
    box = tempfile.mkdtemp(prefix=f"nwn_runall_{name}_")
    tmp, ws = os.path.join(box, "tmp"), os.path.join(box, "ws")
    os.makedirs(tmp)
    env = dict(os.environ, NWN_WORKSPACE=ws, NWN_SCRIPT_COMP=os.path.join(box, "no_compiler", "nwn_script_comp"),
               TMPDIR=tmp, TEMP=tmp, TMP=tmp, PYTHONIOENCODING="utf-8")
    env.pop("NWN_TEST_TIMING", None)
    if timing:
        env["NWN_TEST_TIMING"] = "1"
    t0 = time.time()
    try:
        # no shell; a fixed argument list; the toolkit folder as working folder (as a user would run it)
        p = subprocess.run([sys.executable, os.path.join(HERE, fn)], cwd=ROOT, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out, err, rc, timed_out = p.stdout or "", p.stderr or "", p.returncode, False
    except subprocess.TimeoutExpired as ex:
        def text(b):
            return b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        out, err, rc, timed_out = text(ex.stdout), text(ex.stderr), None, True
    secs = time.time() - t0
    left = []
    for dp, dns, fns in os.walk(tmp):
        left += [os.path.relpath(os.path.join(dp, x), tmp) for x in dns + fns]
    shutil.rmtree(box, ignore_errors=True)
    m = SUMMARY_RE.findall(out)
    passed, total = (int(m[-1][0]), int(m[-1][1])) if m else (None, None)
    notes = []
    if timed_out:
        notes.append(f"stopped after {timeout} s")
    elif rc != 0:
        notes.append(f"exit code {rc}")
    if total is None:
        notes.append("no 'N/M checks passed' line")
    elif total == 0:
        notes.append("no checks ran")
    elif passed != total:
        notes.append(f"{total - passed} check(s) failed")
    if left:
        top = sorted({x.split(os.sep)[0] for x in left})
        notes.append("left temporary files: " + ", ".join(top[:3]) + (" ..." if len(top) > 3 else ""))
    lines = out.splitlines()
    return dict(name=name, ok=not notes, passed=passed, total=total, seconds=secs, note="; ".join(notes),
                skips=[x[5:] for x in lines if x.startswith("SKIP ")],
                fails=[x[5:] for x in lines if x.startswith("FAIL ")],
                tail=(lines[-15:] + ["--- stderr:"] + err.strip().splitlines()[-15:]) if notes else [])


def main(argv=None):
    """Command line (see the module docstring). Returns the exit code."""
    try:
        sys.stdout.reconfigure(errors="replace")      # a check name the console can't show must not stop the run
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Run every test suite and print one table.")
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="run only this suite (test_tools, tools or test_tools.py); repeat or use a,b for several")
    ap.add_argument("--fast", action="store_true", help="leave out the slow suite (test_progress)")
    ap.add_argument("--timing", action="store_true", help="also check time and memory limits (NWN_TEST_TIMING=1)")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds one suite may take (default 3600)")
    a = ap.parse_args(argv)
    want = {w.strip().lower().replace(".py", "") for x in a.only for w in x.split(",") if w.strip()}
    todo = []
    for fn in suites():
        name = fn[:-3]
        if want and not ({name, name[5:] if name.startswith("test_") else name} & want):
            continue
        if a.fast and name in SLOW:
            continue
        todo.append(fn)
    if not todo:
        print("No suite matches " + ", ".join(sorted(want)) + ". Suites: " + ", ".join(f[:-3] for f in suites()))
        return 1
    results = []
    for fn in todo:
        print(f"running {fn} ...", flush=True)
        results.append(run_suite(fn, a.timing, a.timeout))
    width = max(len(r["name"]) for r in results)
    print()
    print(f"{'suite':{width}}  result  checks     skipped  seconds  note")
    print(f"{'-' * width}  ------  ---------  -------  -------  ----")
    for r in results:
        checks = f"{r['passed']}/{r['total']}" if r["total"] is not None else "-"
        print(f"{r['name']:{width}}  {'ok' if r['ok'] else 'FAIL':6}  {checks:9}  {len(r['skips']):7}  "
              f"{r['seconds']:7.1f}  {r['note']}")
    passed = sum(r["passed"] or 0 for r in results)
    total = sum(r["total"] or 0 for r in results)
    skipped = sum(len(r["skips"]) for r in results)
    print(f"{'total':{width}}  {'':6}  {f'{passed}/{total}':9}  {skipped:7}  {sum(r['seconds'] for r in results):7.1f}")
    if skipped:
        print("\nSkipped (not run here, see the reason):")
        for r in results:
            for x in r["skips"]:
                print(f"  {r['name']}: {x}")
    bad = [r for r in results if not r["ok"]]
    for r in bad:
        print(f"\n{r['name']} FAILED: {r['note']}")
        for x in r["fails"][:30]:
            print(f"  FAIL {x[:300]}")
        print("  last lines:")
        for x in r["tail"]:
            print(f"    {x[:300]}")
    print()
    if bad:
        print(f"FAILED: {', '.join(r['name'] for r in bad)}")
        return 1
    print(f"ALL {len(results)} SUITES PASSED ({passed}/{total} checks, {skipped} skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
