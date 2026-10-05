"""
nwn_analyse.py - one command: index + analyse a module (no dashboard needed).

    python nwn_analyse.py <module folder or .mod> [--hak X.hak ...] [--tlk dialog.tlk] [--name NAME]

Results go to nwn_workspace/<name>/ (report.json, reports/*.csv, conversations/, json/),
the same place the dashboard uses, so you can open them there afterwards.

Why this exists: an analysis is two passes - nwn_index.run_index (phase 1: read every file into index.sqlite) and
nwn_analysis.run_analysis (phase 2: turn the index into findings). This script runs both, and it is also the child
process the dashboard starts (--progress), so the dashboard and the command line produce identical folders.

Reads the module, haks, talk table and the user folder's override folder (when a user folder is known: --nwn-user
or Settings) read-only (nwn_index never writes to them). Writes only inside
nwn_workspace/<name>/, including two bookkeeping files:
    .incomplete   created when a run starts, removed only when the analysis pass finishes. The dashboard refuses to
                  open an analysis that still has it (a stopped or crashed run).
    index.done    only with --budget: the index pass finished in an earlier run, so the next run goes straight to the
                  analysis pass.
The whole run holds <folder>/.run.lock (nwn_progress.acquire_run_lock) so the dashboard's Clear/Delete cannot remove
files from under it and two runs cannot write to the same folder.

Exit codes: 0 done; 2 the module (or a --hak / --tlk file) does not exist; 3 not finished yet (a --budget run
stopped at a checkpoint - run the same command again); 4 another run already holds the folder's lock.

Limits: --budget only checkpoints the index pass; the analysis pass then runs in one go. It is fast (a module with
about 60,000 graph nodes analyses in about 20 seconds, the impact step in seconds), so it fits a command time limit
of a few minutes for most modules; a very large one can still exceed such a limit. Then copy index.sqlite to a
machine without that limit and run nwn_analysis.run_analysis there.
"""
import sys

# Checked before anything else is imported, so an old Python gets one plain sentence instead of a traceback.
if sys.version_info < (3, 8):
    sys.exit("The NWN Module Toolkit needs Python 3.8 or newer (this is %d.%d) - install it from python.org"
             % sys.version_info[:2])

import argparse
import os

import nwn_analysis
import nwn_index
import nwn_progress

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    """Command-line entry point: parse arguments, take the folder's run lock, then index and analyse.

    argv: list of argument strings (None = sys.argv[1:]).
    Returns the exit code (see the module docstring; None counts as 0). The lock is always released, even when the
    run fails. Never writes outside nwn_workspace (or $NWN_WORKSPACE when set)."""
    # Set before nwn_dashboard is imported: it reads NWN_WORKSPACE once, at import time, to fix WORKSPACE.
    import nwn_update
    os.environ.setdefault("NWN_WORKSPACE", nwn_update.workspace_dir(HERE))
    import nwn_dashboard  # shares naming, saved settings and the version with the dashboard
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module", help="the module: an unpacked module folder or a .mod file")
    ap.add_argument("--hak", action="append", default=[], help="a hak the module uses (repeat for each); haks the "
                    "module lists are also looked up in the NWN folders")
    ap.add_argument("--tlk", help="the module's custom talk table (.tlk)")
    ap.add_argument("--name", help="analysis name (default: from the module's file or folder name)")
    ap.add_argument("--nwn-root", help="NWN install folder: exact base-game checks and hak lookup")
    ap.add_argument("--nwn-user", help="NWN user folder (Documents/Neverwinter Nights): hak lookup")
    ap.add_argument("--budget", type=int, help="seconds per run: stop cleanly and checkpoint; run again to resume "
                    "(for sandboxes that kill long jobs). Prints CHECKPOINT when more runs are needed")
    ap.add_argument("--no-json", action="store_true", help="skip per-file JSON (faster, smaller)")
    ap.add_argument("--progress", action="store_true", help="print machine-readable progress lines (used by the dashboard)")
    ap.add_argument("--version", action="version", version=f"NWN Module Toolkit {nwn_dashboard.VERSION}")
    a = ap.parse_args(argv)
    gone = [p for p in [a.module] + a.hak + ([a.tlk] if a.tlk else []) if not os.path.exists(p)]
    if gone:
        print(f"Not found: {gone[0]} - check the path (put it in quotes if it contains spaces)")
        return 2
    if a.progress:
        nwn_progress.ENABLED = True
    name = nwn_dashboard.analysis_name_for(a.module, a.name)
    out = os.path.join(nwn_dashboard.WORKSPACE, name)
    # Started by the dashboard: exit on our own if it goes away, so closing its window never leaves a run behind.
    if a.progress and os.environ.get("NWN_PARENT_PID", "").isdigit():
        nwn_progress.watch_parent(int(os.environ["NWN_PARENT_PID"]))
    try:
        lock = nwn_progress.acquire_run_lock(out)
    except RuntimeError as ex:
        print(f"ERROR: {ex} - wait for it to finish or stop it first")
        return 4
    try:
        return _run(a, name, out)
    finally:
        nwn_progress.release_run_lock(lock, out)


def _run(a, name, out):
    """Run the index pass (unless a --budget run already finished it) and then the analysis pass.

    a: the parsed arguments; name: the analysis name; out: its folder. Returns 3 when another run is needed
    (checkpoint), otherwise None. The caller holds the run lock."""
    import json
    import time
    import nwn_dashboard
    st = nwn_dashboard.settings()
    marker = os.path.join(out, nwn_dashboard.INCOMPLETE)   # unfinished until the analysis pass completes
    if not os.path.exists(marker):          # a resumed --budget run keeps the first run's marker (and start time)
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump(dict(module=os.path.abspath(a.module), started=time.strftime("%Y-%m-%d %H:%M:%S")), fh)
    done = os.path.join(out, "index.done")
    if not (a.budget and os.path.exists(done)):
        # The install/user folders saved in the dashboard's Settings fill in whatever the command line leaves out.
        # run_index returns None when the time budget ran out; it has saved its state to resume from next time.
        # overrides=None: the user folder's override folder is read when it has files (the game loads it too);
        # [] would mean "no override folder", which only the clean-build audit wants.
        r = nwn_index.run_index(a.module, a.hak, None, a.tlk, out, write_json=not a.no_json,
                                nwn_root=a.nwn_root or st["nwn_root"] or None,
                                nwn_user=a.nwn_user or st["nwn_user"] or None, time_budget=a.budget)
        if r is None:
            print("CHECKPOINT - run the same command again to continue")
            return 3
        if a.budget:
            # Stop here so the analysis pass gets a whole fresh time slice of its own on the next run.
            with open(done, "w") as fh:
                fh.write("ok")
            print("INDEX DONE - run again for the analysis pass")
            return 3
    nwn_analysis.run_analysis(out)
    if os.path.exists(done):
        os.remove(done)
    os.remove(marker)
    nwn_progress.emit(phase="done", name=name)
    print(f"\nDone. Reports: {os.path.join(out, 'reports')}\nOpen the dashboard (run_dashboard) to browse '{name}'.")


if __name__ == "__main__":
    sys.exit(main() or 0)
