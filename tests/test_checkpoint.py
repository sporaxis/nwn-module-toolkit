"""
Chunked (time-boxed) indexing must give the same result as one uninterrupted run: the index is stopped at its time
budget many times (a fake clock makes every file "slow") and resumed from its checkpoint until it finishes, then
every table is compared with a single run's.
    python tests/test_checkpoint.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in a temporary folder that is removed at the end.
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import os
import sqlite3
import sys
import time
from unittest import mock

import nwn_analysis  # noqa: E402
import nwn_index  # noqa: E402
from make_test_module import build  # noqa: E402
from make_hakconflict_module import build as build_hak  # noqa: E402
from _harness import check  # noqa: E402

TABLES = ("SELECT relpath, resref, ext, sha256, status FROM files",
          "SELECT src, dst, kind, via FROM edges", "SELECT node, type, name, in_module FROM nodes",
          "SELECT severity, category, node FROM issues", "SELECT name, has_ncs FROM scripts",
          "SELECT file_id, path, value FROM fields", "SELECT key, value FROM meta WHERE key<>'indexed_at'",
          "SELECT path, kind, priority FROM sources")


def rows(db, q):
    """All rows of query q on the index at path db, sorted (row order is not part of the result)."""
    con = sqlite3.connect(db)
    try:
        return sorted(map(tuple, con.execute(q).fetchall()))
    finally:
        con.close()


def run_case(tmp, with_haks):
    """Index the test module (or the two-hak module) once in one go and once in many time-boxed chunks; compare."""
    label = " (with haks)" if with_haks else ""
    mod = os.path.join(tmp, "mod")
    if with_haks:
        build_hak(mod, os.path.join(tmp, "hak_a"), os.path.join(tmp, "hak_b"))
        haks = [os.path.join(tmp, "hak_a"), os.path.join(tmp, "hak_b")]
    else:
        build(mod); haks = []
    single = os.path.join(tmp, "single"); chunked = os.path.join(tmp, "chunked")
    nwn_index.run_index(mod, haks, [], None, single, verbose=False)
    # force many checkpoints: a 2 s budget and a clock that moves 0.6 s per reading. Only nwn_index's own `time` is
    # replaced, so nothing else in the process sees the fake clock.
    base = time.time()
    calls = [0]

    def fake():
        calls[0] += 1
        return base + calls[0] * 0.6
    runs, finished, state_kept = 0, False, True
    with mock.patch.object(nwn_index, "time", h.Proxy(time, time=fake)):
        while runs < 500 and not finished:
            runs += 1
            finished = bool(nwn_index.run_index(mod, haks, [], None, chunked, verbose=False, time_budget=2))
            if not finished and not os.path.exists(os.path.join(chunked, "index.state")):
                state_kept = False
    check(f"chunked run{label}: finishes, and keeps index.state after every stop ({runs} runs)",
          finished and state_kept and runs > 1, (finished, state_kept, runs))
    for q in TABLES:
        a, b = rows(os.path.join(single, "index.sqlite"), q), rows(os.path.join(chunked, "index.sqlite"), q)
        check(f"chunked == single{label}: {q.split(' FROM ')[1]} ({len(a)} vs {len(b)}, {runs} runs)", a == b,
              (len(a), len(b)))
    check(f"chunked run{label}: index.state is removed once the index is complete",
          not os.path.exists(os.path.join(chunked, "index.state")))
    nwn_analysis.run_analysis(chunked, verbose=False)
    check(f"analysis runs on the chunked index{label}", os.path.isfile(os.path.join(chunked, "report.json")))


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    for with_haks in (False, True):
        with h.tempdir("nwn_ckpt_") as tmp:
            h.run(run_case, tmp, with_haks)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
