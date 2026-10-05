"""
Editors: the 2da engine, the Hak editor (open, stage, rebuild, verify, add under a new name, restore an old backup,
extract), and the clean build around them (talk-table copy, build names,
include recompiles, audit options, lean-hak names, the build command line).
    python tests/test_editors.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from unittest import mock

import nwnlib as n  # noqa: E402
import nwn_2da  # noqa: E402
import nwn_hakedit as H  # noqa: E402
import nwn_index  # noqa: E402
import nwn_analysis  # noqa: E402
import nwn_audit  # noqa: E402
import nwn_build  # noqa: E402
import nwn_hakslim  # noqa: E402
import make_test_module as mtm  # noqa: E402
from make_hakconflict_module import build as build_hak  # noqa: E402
from _gff import root, st, R, LST  # noqa: E402
from _harness import check, skip, pack_hak  # noqa: E402

TMP = None          # this run's temporary folder, set by main()


def pack(folder, path, extra=()):
    """The folder's files as a .hak with a description (the editor must keep it)."""
    pack_hak(folder, path, extra, description={0: "Test hak description"})


def entries(path):
    e = n.Erf(path)
    try:
        return {x.filename: e.read(x) for x in e.entries}
    finally:
        e.close()


def peak_rss():
    """Peak memory of this process in bytes (Windows and Linux/macOS)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


def _try(fn):
    try:
        fn()
        return "ok"
    except ValueError as ex:
        return str(ex)


def hak_editor_files():
    """Extract writes all or nothing and refuses a file as its folder; the hak is opened once for many files;
    unfinished install copies are cleared when a project opens; a folder added to a hak never follows a link."""
    import struct
    d = os.path.join(TMP, "hef")
    os.makedirs(d)
    hak = os.path.join(d, "hef.hak")
    n.write_erf(hak, [("a", "txt", b"one"), ("b", "txt", b"two"), ("c", "txt", b"three")], "HAK ")
    he = H.HakEditor(os.path.join(TMP, "ws_hef"))
    pid = he.open(hak)
    opened = []
    def counting(path):
        opened.append(path)
        return n.Erf(path)
    # only the hak editor's own view of nwnlib gets the counting Erf
    with mock.patch.object(H, "n", h.Proxy(n, Erf=counting)):
        r = he.extract(pid, dest=os.path.join(d, "out"))
    check("extract opens the hak once for all its files", r["files"] == 3 and len(opened) == 1, (r, opened))
    afile = os.path.join(d, "a_file.txt")
    open(afile, "w").write("x")
    check("extract refuses a file as its destination", "is a file" in _try(lambda: he.extract(pid, dest=afile)))
    # damage entry c: its size now runs past the end of the hak
    raw = bytearray(open(hak, "rb").read())
    res_off = struct.unpack_from("<I", raw, 28)[0]
    struct.pack_into("<I", raw, res_off + 2 * 8 + 4, 10_000)
    bad = os.path.join(d, "bad.hak")
    open(bad, "wb").write(bytes(raw))
    pid_b = he.open(bad)
    dest = os.path.join(d, "partial")
    err = _try(lambda: he.extract(pid_b, dest=dest))
    check("extract of a hak with a damaged entry leaves no half-filled folder",
          err != "ok" and not os.path.exists(dest) and not [f for f in os.listdir(d) if ".extracting-" in f], (err, os.listdir(d)))
    # unfinished install copies next to the hak: only this toolkit's names, other processes', older than 10 minutes
    old = os.path.join(d, "hef_r1.hak.installing-999999-deadbeef")
    mine = os.path.join(d, f"hef_r2.hak.installing-{os.getpid()}-0badf00d")
    fresh = os.path.join(d, "hef_r3.hak.installing-999998-cafebabe")
    other = os.path.join(d, "notes.installing.txt")
    for p_ in (old, mine, fresh, other):
        open(p_, "w").write("x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    os.utime(mine, (time.time() - 3600, time.time() - 3600))
    he.open(hak)
    hist = json.load(open(os.path.join(he.root, pid, "project.json")))["history"]
    check("opening a hak clears unfinished install copies this toolkit left (only stale ones from other runs)",
          not os.path.exists(old) and all(os.path.exists(p_) for p_ in (mine, fresh, other)) and
          any(h.get("files") == [os.path.basename(old)] for h in hist), (os.listdir(d), hist))
    name = "adding a folder to a hak never follows a symbolic link"
    if not h.can_symlink(d):
        skip(name, "this system does not let the test make a symbolic link (Windows needs developer mode)")
    else:
        src = os.path.join(d, "addme")
        os.makedirs(src)
        open(os.path.join(src, "good.txt"), "w").write("g")
        secret = os.path.join(d, "secret.txt")
        open(secret, "w").write("private")
        os.symlink(secret, os.path.join(src, "leak.txt"))
        r = he.add_path(pid, src)
        check(name, r["added"] == ["good.txt"] and any(x["why"].startswith("symbolic link") for x in r["skipped"]), r)


def editors_core():
    """The 2da engine, the Hak editor end to end, the dashboard's 2da endpoints and its HTTP save rules."""
    # ------------------------------------------------------------ 2da engine
    txt = '2DA V2.0\n\n   LABEL   RACE  Name   MODEL\n0  Dwarf   D     100   "my model"\n1  Elf     E\n3  Human   H     16777300 x\n4 Rat r S 7 8\n'
    tab, iss = nwn_2da.parse(txt)
    check("2da: short row = warning, long row = error, with line numbers",
          any(i["level"] == "warning" and i["line"] == 5 for i in iss) and any(i["level"] == "error" and i["line"] == 7 for i in iss), iss)
    out = nwn_2da.write(tab)
    tab2, iss2 = nwn_2da.parse(out)
    check("2da: write -> parse round trip is exact (quotes, ****)", tab2["rows"] == tab["rows"] and not iss2, (tab2["rows"], iss2))
    v = nwn_2da.validate(tab)
    check("2da: row labels out of step -> renumber hint", any(c.get("fix") == "renumber" for c in v))
    check("2da: text in a talk-table column is flagged", any("talk-table number" in c["msg"] for c in v))
    check("2da: renumber fixes labels", [r[0] for r in nwn_2da.renumber(tab)["rows"]] == ["0", "1", "2", "3"])
    orig = nwn_2da.parse(txt)[0]
    ed = dict(tab, rows=[tab["rows"][0]] + tab["rows"][2:], origin=[0, 2, 3])
    v = nwn_2da.validate(ed, dict(original=orig, row_usage={"2": ["guard.utc"]}, usage_known=True))
    check("2da: deleting a row above a used row blocks (usage) and names the users",
          any(c.get("blocking") == "usage" and "guard.utc" in c["msg"] for c in v), v)
    ed = dict(tab, rows=tab["rows"] + [["4", "New", None, None, None]], origin=[0, 1, 2, 3, None])
    v = nwn_2da.validate(ed, dict(original=orig, row_usage={"2": ["guard.utc"]}, usage_known=True))
    check("2da: adding at the end moves nothing (no usage block)", not nwn_2da.blocking(v), v)
    ed = dict(tab, rows=[tab["rows"][0], tab["rows"][1], ["2", None, None, None, None], tab["rows"][3]], origin=[0, 1, 2, 3])
    v = nwn_2da.validate(ed, dict(original=orig, row_usage={"2": ["guard.utc"]}, usage_known=True))
    check("2da: blanking a used row warns but does not block", not nwn_2da.blocking(v) and any("now blank" in c["msg"] for c in v), v)
    bad = dict(columns=["A", "a"], rows=[["0", 'x"y', None]], origin=[0])
    v = nwn_2da.validate(bad)
    check("2da: duplicate column and \" in a value are hard blocks",
          sum(1 for c in v if c.get("blocking") is True) == 2, v)
    long = dict(columns=["ImpactScript"], rows=[["0", "a_script_name_that_is_long"]], origin=[0])
    v = nwn_2da.validate(long, dict(known_names={"x.ncs"}))
    check("2da: names over 16 characters in name columns are errors", any("16" in c["msg"] and c["level"] == "error" for c in v), v)

    # ------------------------------------------------------------ fixtures: analysed module with two haks + fake game
    mod, ha, hb, groot = (os.path.join(TMP, x) for x in ("mod", "hak_a", "hak_b", "game"))
    build_hak(mod, ha, hb, groot)
    open(os.path.join(ha, "plc_chest.mdl"), "w").write("newmodel plc_chest\nsetsupermodel plc_chest NULL\nnode trimesh b\n bitmap tex_chest\nendnode\n")
    open(os.path.join(ha, "tex_chest.tga"), "wb").write(bytes([0, 0, 2] + [0] * 9 + [2, 0, 2, 0, 24, 0x20]) + b"\xff\0\0" * 4)
    user = os.path.join(TMP, "user")
    os.makedirs(os.path.join(user, "hak"))
    hak_a, hak_b = os.path.join(user, "hak", "hak_a.hak"), os.path.join(user, "hak", "hak_b.hak")
    pack(ha, hak_a)
    pack(hb, hak_b, extra=[("plc_chest", "mdl", b"hak b copy")])
    out = os.path.join(h.WORKSPACE, "hc")             # the dashboard's workspace: its 2da endpoints read it
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, [], [], None, out, False, False, nwn_root=groot, nwn_user=user)
        nwn_analysis.run_analysis(out, verbose=False)
    ctx = H.Context(out, hak_a)
    check("context: hak found in the analysis, 2da with used rows counts as used",
          ctx.in_module and ctx.used.get("appearance.2da") is True and ctx.tables["appearance"].get("2"), (ctx.used, ctx.tables.get("appearance")))
    ctx_b = H.Context(out, hak_b)
    check("context: hak_b's files hidden by hak_a (listed first)", ctx_b.hidden_by.get("appearance.2da") == "hak_a.hak", ctx_b.hidden_by)

    # ------------------------------------------------------------ hak editor
    he = H.HakEditor(h.WORKSPACE)                    # the same projects the dashboard sees
    pid = he.open(hak_a, "hc")
    st = he.state(pid, ctx)
    check("open: all files listed, nothing changed, no errors", len(st["entries"]) == 5 and not st["changes"]
          and not [c for c in st["checks"] if c["level"] == "error"], st["checks"])
    before = open(hak_a, "rb").read()
    he.remove(pid, ["appearance.2da"])
    st = he.state(pid, ctx)
    check("removing a used file is an error with a 'remove anyway' fix", any(c["level"] == "error" and c.get("fix") == "force"
                                                                             and c["name"] == "appearance.2da" for c in st["checks"]), st["checks"])
    try:
        he.build(pid, ctx, log=lambda *a: None); built = True
    except ValueError:
        built = False
    check("rebuild refused while there are errors", not built)
    he.undo(pid, ["appearance.2da"])
    for bad_name in ("this_is_far_too_long.tga", "bad name.tga", "x.qqq", "noext"):
        try:
            he.put(pid, bad_name, b"x"); ok = False
        except ValueError:
            ok = True
        if not ok:
            break
    check("bad names refused (too long, spaces, unknown type, no extension)", ok, bad_name)
    he.put(pid, "tex_chest.tga", bytes([0, 0, 2] + [0] * 9 + [2, 0, 2, 0, 24, 0x20]) + b"\0\xff\0" * 4)
    he.put(pid, "new_model.mdl", b"newmodel other_name\nsetsupermodel new_model base_sup\nnode trimesh b\n bitmap no_such_tex\nendnode\n")
    he.put(pid, "plc_chest.mdl", open(os.path.join(ha, "plc_chest.mdl"), "rb").read())   # same content, staged
    st = he.state(pid, ctx)
    msgs = " | ".join(c["msg"] for c in st["checks"])
    check("model checks: internal name mismatch, missing texture, missing supermodel",
          "named 'other_name'" in msgs and "no_such_tex" in msgs and "base_sup" in msgs, msgs)
    ctx_b_pid = he.open(hak_b, "hc")
    he.put(ctx_b_pid, "appearance.2da", b"2DA V2.0\n\n LABEL\n0 x\n")
    st_b = he.state(ctx_b_pid, ctx_b)
    check("adding a file a higher hak already has warns it will never load",
          any("never be used" in c["msg"] for c in st_b["checks"]), st_b["checks"])
    he.put(ctx_b_pid, "broken.2da", b"2DA V2.0\n\n A B\n0 1 2 3\n")
    st_b = he.state(ctx_b_pid, ctx_b)
    check("a broken 2da blocks the rebuild with an 'open in 2da editor' fix",
          any(c["level"] == "error" and c.get("fix") == "edit2da" for c in st_b["checks"]), st_b["checks"])
    he.rename(pid, "hak_a_marker.txt", "marker2.txt")
    try:
        he.rename(pid, "marker2.txt", "marker2.2da"); ok = False
    except ValueError:
        ok = True
    check("rename can't change the file type", ok)
    ch = {c["name"]: c["change"] for c in he.state(pid, ctx)["changes"]}
    check("changes list: replaced / added / renamed", ch.get("tex_chest.tga") == "replaced" and ch.get("new_model.mdl") == "added"
          and ch.get("marker2.txt") == "renamed", ch)
    # 2da edit through the hak project
    tab, piss, orig = he.load_2da(pid, "appearance.2da")
    tab["rows"].append([str(len(tab["rows"])), "Kobold", "K", "P"]); tab["origin"].append(None)
    he.save_2da(pid, "appearance.2da", tab)
    tab2, _pi, orig2 = he.load_2da(pid, "appearance.2da")
    check("2da saved into the hak project; original kept for comparison", len(tab2["rows"]) == len(orig2["rows"]) + 1)
    # build + verify
    logs = []
    info = he.build(pid, ctx, log=logs.append)
    got = entries(info["file"])
    check("rebuild verified; new files byte-for-byte", info["verified"] and got["new_model.mdl"].startswith(b"newmodel other_name")
          and "marker2.txt" in got and "hak_a_marker.txt" not in got, (info["problems"], sorted(got)))
    check("rebuild keeps the hak's description strings", n.erf_description_block(info["file"]) == n.erf_description_block(hak_a))
    check("original hak untouched by rebuild", open(hak_a, "rb").read() == before)
    # tampering is detected
    bad = os.path.join(TMP, "tampered.hak")
    shutil.copy(info["file"], bad)
    with open(bad, "r+b") as fh:
        fh.seek(-3, 2); fh.write(b"zzz")
    v = H.HakEditor.verify(bad, [(k, __import__("hashlib").sha256(x).hexdigest()) for k, x in got.items()])
    check("verify detects a changed byte", not v["ok"], v)
    # install = add under a NEW name next to the original (never a replacement)
    check("a rebuilt hak gets a new name (<hak>_r1)", os.path.basename(info["file"]).lower() ==
          os.path.splitext(os.path.basename(hak_a))[0].lower() + "_r1.hak", info["file"])
    r = he.install(pid, info["id"])
    check("install adds the rebuild next to the original under its new name; the original is untouched",
          os.path.dirname(r["installed"]) == os.path.dirname(hak_a) and r["installed"] != hak_a and
          open(hak_a, "rb").read() == before and entries(r["installed"]).keys() == got.keys(), r)
    check("after install the project continues from the new hak (no staged changes)",
          not he.state(pid, ctx)["changes"] and he._load(pid)[1]["source"] == r["installed"])
    he.put(pid, "yy.txt", b"y")
    info3 = he.build(pid, ctx, log=lambda *a: None)
    check("the next rebuild takes the next free name (_r2)", os.path.basename(info3["file"]).lower().endswith("_r2.hak"),
          info3["file"])
    blocker = os.path.join(os.path.dirname(hak_a), os.path.basename(info3["file"]))
    open(blocker, "wb").write(b"someone else's file")
    try:
        he.install(pid, info3["id"]); refused = False
    except ValueError as ex:
        refused = "nothing was overwritten" in str(ex)
    check("install never overwrites a file that already has the new name", refused and
          open(blocker, "rb").read() == b"someone else's file")
    os.remove(blocker)
    he.undo(pid, ["yy.txt"])
    src_now = r["installed"]
    before_now = open(src_now, "rb").read()
    # stale project refuses install
    he.put(pid, "zz.txt", b"x")
    info2 = he.build(pid, ctx, log=lambda *a: None)
    time.sleep(1.1)
    with open(src_now, "ab") as fh:
        fh.write(b"\0")          # someone else changed the hak
    try:
        he.install(pid, info2["id"]); refused = False
    except ValueError:
        refused = True
    check("install refused when the hak changed on disk since opening", refused)
    check("...and the checks say so", any("changed on disk" in c["msg"] for c in he.state(pid, ctx)["checks"]))
    with open(src_now, "wb") as fh:
        fh.write(before_now)
    # extract never overwrites
    dest = os.path.join(TMP, "x_out")
    os.makedirs(dest)
    open(os.path.join(dest, "keep.txt"), "w").write("mine")
    try:
        he.extract(pid, None, dest); refused = False
    except ValueError:
        refused = True
    check("extract refuses a non-empty folder", refused and open(os.path.join(dest, "keep.txt")).read() == "mine")
    r = he.extract(pid, ["appearance.2da"])
    check("extract to the workspace writes the file", r["files"] == 1 and os.path.isfile(os.path.join(r["folder"], "appearance.2da")))
    # add a folder: good, bad and duplicate names reported
    fold = os.path.join(TMP, "addme"); os.makedirs(os.path.join(fold, "sub"))
    open(os.path.join(fold, "a1.tga"), "wb").write(b"1" * 20)
    open(os.path.join(fold, "sub", "a1.tga"), "wb").write(b"2" * 20)
    open(os.path.join(fold, "readme.docx"), "wb").write(b"x")
    r = he.add_path(pid, fold)
    check("add folder: added, duplicate and unknown types skipped with reasons",
          r["added"] == ["a1.tga"] and len(r["skipped"]) == 2 and all(s["why"] for s in r["skipped"]), r)

    # ------------------------------------------------------------ streaming: memory stays low on a big hak
    big = os.path.join(TMP, "big.hak")
    blob = os.urandom(4 * 1024 * 1024)
    n.write_erf_stream(big, [(f"t{i:04d}", "tga", len(blob), (lambda: blob)) for i in range(60)], "HAK ")
    rss0 = peak_rss()
    bp = he.open(big)
    he.put(bp, "small.txt", b"hi")
    info = he.build(bp, None, log=lambda *a: None)
    rss1 = peak_rss()
    check("a 240 MB hak rebuilds and verifies (streamed)", info["verified"], info.get("problems"))
    h.timing("a 240 MB hak rebuild adds under 80 MB to the peak memory (it is never loaded whole)",
             (rss1 - rss0) / (1 << 20), 80, "MB")

    # ------------------------------------------------------------ dashboard endpoints (2da save rules, module 2da)
    import nwn_dashboard as dash
    os.makedirs(os.path.join(dash.WORKSPACE), exist_ok=True)
    dash.save_settings(dict(dash.settings(), nwn_root=groot, nwn_user=user))
    b = dict(src="hak", p=pid, name="appearance.2da")
    load, save, cx, label, _cur = dash.twoda_source(b)
    tab, _pi, orig = load()
    moved = dict(tab, rows=[tab["rows"][0]] + tab["rows"][2:], origin=[0] + list(range(2, len(tab["rows"]))))
    checks = dash.twoda_checks(cx, "appearance", moved, orig)
    check("dashboard 2da checks use the analysis (row 2 used by guard/noble)", any(c.get("blocking") == "usage" for c in checks), checks)
    check("base game comparison runs when the NWN install is set", any("installed game" in c["msg"] for c in checks), checks)
    # module 2da through the edits overlay
    open(os.path.join(mod, "mymod.2da"), "w").write("2DA V2.0\n\n LABEL\n0 a\n1 b\n")
    with contextlib.redirect_stdout(io.StringIO()):
        nwn_index.run_index(mod, [], [], None, out, False, False, nwn_root=groot, nwn_user=user)
        nwn_analysis.run_analysis(out, verbose=False)
    dash.release_analysis("hc")
    load, save, cx, label, _cur = dash.twoda_source(dict(src="edit", a="hc", file="mymod.2da"))
    tab, _pi, orig = load()
    tab["rows"].append(["2", "c"]); tab["origin"].append(None)
    save(tab)
    check("module 2da saved to the edits overlay, original module untouched",
          os.path.isfile(os.path.join(out, "edits", "mymod.2da")) and "c" not in open(os.path.join(mod, "mymod.2da")).read())
    tab2 = load()[0]
    check("...and reloads with the edit", len(tab2["rows"]) == 3)

    # ------------------------------------------------------------ 2da quoting, duplicate entries, install rules, busy lock
    t, _i = nwn_2da.parse("2DA V2.0\nDEFAULT: \"a b\"\n  LABEL NAME ICON\n0 Mage's_Armor Mordenkainen's_Sword ic_a\\b\n")
    w = nwn_2da.write(t)
    check("2da: apostrophes and backslashes are plain characters (no merged/shifted columns)",
          t["rows"][0] == ["0", "Mage's_Armor", "Mordenkainen's_Sword", "ic_a\\b"] and nwn_2da.parse(w)[0]["rows"] == t["rows"], t["rows"])
    check("2da: DEFAULT with spaces survives, column names stay on line 3",
          w.split("\r\n")[1] == 'DEFAULT: "a b"' and w.split("\r\n")[2].split() == ["LABEL", "NAME", "ICON"], w)
    check("analysis 2da reader agrees (nwnlib.read_2da)", n.read_2da(w).rows[0][1] == ["Mage's_Armor", "Mordenkainen's_Sword", "ic_a\\b"])
    # duplicates keep the first copy; strref and description carried over; unsafe names left out
    dup = os.path.join(TMP, "dup.hak")
    n.write_erf(dup, [("same", "txt", b"FIRST"), ("same", "txt", b"second"), ("other", "txt", b"o")], file_type="HAK ")
    with open(dup, "r+b") as fh:
        fh.seek(40); fh.write((1234).to_bytes(4, "little"))   # description strref
    dp = he.open(dup)
    check("duplicate names: the first copy is the one kept (what the game reads)", he.read(dp, "same.txt") == b"FIRST")
    he.put(dp, "desktop2.txt", b"x")
    info = he.build(dp, None, log=lambda *a: None)
    check("rebuild keeps the description strref", n.Erf(info["file"]).strref == 1234 and entries(info["file"])["same.txt"] == b"FIRST")
    # install refused when something was staged after the build
    he.put(dp, "late.txt", b"late")
    try:
        he.install(dp, info["id"]); refused = False
    except ValueError as ex:
        refused = "after this build" in str(ex)
    check("install refused if you staged changes after the build (nothing staged is lost)", refused)
    he.undo(dp, ["late.txt"])
    r = he.install(dp, info["id"])
    bk = r["installed"]
    # rebaseline after an outside change keeps staged adds; discard keeps the added hak
    he.put(dp, "keepme.txt", b"k")
    time.sleep(1.1)
    n.write_erf(r["installed"], [("other", "txt", b"changed by someone else")], file_type="HAK ")
    check("outside change detected", any("changed on disk" in c["msg"] for c in he.state(dp)["checks"]))
    rb = he.rebaseline(dp)
    st = he.state(dp)
    check("re-baseline: file on disk is the new start, staged adds kept, no 'changed' error",
          rb["kept_staged"] == ["keepme.txt"] and not any("changed on disk" in c["msg"] for c in st["checks"])
          and {c["name"] for c in st["changes"]} == {"keepme.txt"}, (rb, st["changes"]))
    he.discard(dp)
    check("discarding a project keeps the hak it added (and the original)", os.path.isfile(bk) and os.path.isfile(dup))
    # busy lock: a second change while one is running fails fast with a clear message
    lp = he.open(hak_b)
    H._LOCKS[lp].acquire()
    try:
        res = []
        th = threading.Thread(target=lambda: res.append(_try(lambda: he.put(lp, "x.txt", b"x"))))
        th.start(); th.join(5)
    finally:
        H._LOCKS[lp].release()
    check("a change while a rebuild/install runs is refused as busy (no lost update)", res and "busy" in res[0], res)
    # system files skipped when adding a folder
    sysf = os.path.join(TMP, "sysf"); os.makedirs(sysf)
    open(os.path.join(sysf, "desktop.ini"), "w").write("[x]")
    open(os.path.join(sysf, "ok.txt"), "w").write("x")
    r = he.add_path(lp, sysf)
    check("desktop.ini and similar are skipped when adding a folder", r["added"] == ["ok.txt"] and "system file" in r["skipped"][0]["why"], r)

    # ------------------------------------------------------------ HTTP: 2da save rules end to end
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-NWN-Token": dash.TOKEN, "Content-Type": "application/json", "Host": "127.0.0.1"})
        try:
            with urllib.request.urlopen(req) as r_:
                return r_.status, json.loads(r_.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    pid2 = he.open(hak_a, "hc")
    code, d = call(f"/api/twoda?src=hak&p={pid2}&name=appearance.2da")
    t = d["table"]
    moved = dict(t, rows=[t["rows"][0]] + t["rows"][2:], origin=[0] + list(range(2, len(t["rows"]))))
    code, r = call("/api/twoda/save", dict(src="hak", p=pid2, name="appearance.2da", table=moved, base_sha=d["base_sha"]))
    check("HTTP: saving a change that moves used rows -> 409 needs_confirm", code == 409 and r.get("needs_confirm"), (code, r))
    code, r = call("/api/twoda/save", dict(src="hak", p=pid2, name="appearance.2da", table=moved, base_sha="stale"))
    check("HTTP: a save based on an out-of-date copy is refused", code == 409 and "changed elsewhere" in r["error"], (code, r))
    bad = dict(t, origin=t["origin"][:-1])
    code, r = call("/api/twoda/save", dict(src="hak", p=pid2, name="appearance.2da", table=bad, base_sha=d["base_sha"]))
    check("HTTP: rows/origins that don't line up are rejected", code == 400, (code, r))
    code, r = call("/api/twoda/save", dict(src="hak", p=pid2, name="appearance.2da", table=moved, base_sha=d["base_sha"], allow_risky=True))
    check("HTTP: confirmed risky save goes through", code == 200 and r.get("saved"), (code, r))
    srv.shutdown()
    srv.server_close()


# ------------------------------------------------------------------ clean build around the editors
def analyse_test_module(tmp, sub):
    """The synthetic test module (it has a custom tlk) indexed and analysed into <tmp>/<sub>_analysis."""
    mod = os.path.join(tmp, sub)
    mtm.build(mod)
    out = os.path.join(tmp, f"{sub}_analysis")
    h.analyse(mod, out, tlk=os.path.join(mod, "testmod_custom.tlk"))
    return mod, out


def build_targets(tmp):
    """The talk-table copy next to the build, build names, and the rewriter re-pointing an area's own script."""
    mod, out = analyse_test_module(tmp, "b1")
    plan = {"delete": [], "merge": []}
    # ---- tlk copy: a stranger's file of the tlk's name in the output folder is never overwritten
    foreign_out = os.path.join(tmp, "foreign_out")
    os.makedirs(foreign_out)
    foreign = os.path.join(foreign_out, "testmod_custom.tlk")
    open(foreign, "wb").write(b"not the module's tlk")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_build.build(out, plan, out_dir=foreign_out, run_audit=False, verbose=False)
        refused = False
    except ValueError as ex:
        refused = "talk table" in str(ex)
    check("tlk copy: build refuses to overwrite a file of the tlk's name it did not write",
          refused and open(foreign, "rb").read() == b"not the module's tlk" and
          not os.path.exists(os.path.join(foreign_out, "b1_clean")), os.listdir(foreign_out))
    # a normal build copies the tlk; a second build into the same folder replaces its own copy
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, plan, run_audit=False, verbose=False)
        log2 = nwn_build.build(out, plan, run_audit=False, verbose=False)
    tdest = os.path.join(out, "build", "testmod_custom.tlk")
    check("tlk copy: travels with the build and a rebuild may replace the previous build's copy",
          log.get("tlk_file") == tdest and log2.get("tlk_file") == tdest and
          open(tdest, "rb").read() == open(os.path.join(mod, "testmod_custom.tlk"), "rb").read(), (log.get("tlk_file"), log2.get("tlk_file")))
    # the tlk's own folder as output: same file, nothing to copy (and no SameFileError)
    same = nwn_build.check_tlk_target(os.path.join(mod, "testmod_custom.tlk"), mod)
    check("tlk copy: output folder = the tlk's own folder is 'nothing to copy', not an error",
          same == (os.path.join(mod, "testmod_custom.tlk"), False), same)
    # ---- build names: a trailing space is refused (Windows drops it; elsewhere it makes a look-alike folder)
    try:
        nwn_build.check_target(mod, os.path.join(tmp, "o"), "clean ")
        refused = False
    except ValueError:
        refused = True
    try:
        nwn_build.check_target(mod, os.path.join(tmp, "o"), "clean")
        ok_name = True
    except ValueError as ex:
        ok_name = ex
    check("build name: a trailing space is refused, the same name without it is fine", refused and ok_name is True, ok_name)
    # ---- Rewriter: an area .git's own fields (not only its instances') are re-pointed
    rw = nwn_build.Rewriter({"old_hb": "new_hb"}, {}, {}, set())
    g = root("GIT ", OnHeartbeat=(R, "old_hb"), **{"Creature List": (LST, [st(4, TemplateResRef=(R, "goblin"))])})
    changed = rw.rewrite("x.git", "git", g)
    check("Rewriter: an area .git's own script field is re-pointed", changed and g.get("OnHeartbeat") == "new_hb",
          (changed, rw.changes))
    return mod, out


def build_include_precedence(tmp):
    """_compile_sources: the include copy the build compiles against is the one the toolset would use - the first
    listed hak's, over the module's own copy - whatever order SQLite returns rows in, and never the override's."""
    for order in ("ab", "ba"):
        mod = os.path.join(tmp, f"inc_{order}")
        mtm.build(mod)
        open(os.path.join(mod, "inc_shared.nss"), "w").write("// copy from the module\nvoid Shared() { }\n")
        user = os.path.join(tmp, f"inc_{order}_user")
        os.makedirs(os.path.join(user, "override"))
        open(os.path.join(user, "override", "inc_shared.nss"), "w").write("// copy from override\nvoid Shared() { }\n")
        open(os.path.join(user, "override", "inc_ovr.nss"), "w").write("// only in override\n")
        haks = []
        for hk in "ab":
            hd = os.path.join(tmp, f"inc_{order}_hak_{hk}")
            os.makedirs(hd)
            open(os.path.join(hd, "inc_shared.nss"), "w").write(f"// copy from hak {hk}\nvoid Shared() {{ }}\n")
            haks.append(hd)
        if order == "ba":
            haks.reverse()
        out = os.path.join(tmp, f"inc_{order}_analysis")
        with contextlib.redirect_stdout(io.StringIO()):
            nwn_index.run_index(mod, haks=haks, out=out, verbose=False, nwn_user=user)
        db = sqlite3.connect(os.path.join(out, "index.sqlite"))
        d = nwn_build._compile_sources(db, mod, set())
        db.close()
        got = open(os.path.join(d, "inc_shared.nss")).read()
        no_ovr = not os.path.exists(os.path.join(d, "inc_ovr.nss"))
        shutil.rmtree(d, ignore_errors=True)
        check(f"recompiles use the first listed hak's include over the module's and never the override's (order {order})",
              f"copy from hak {order[0]}" in got and no_ovr, (got, no_ovr))


def audit_options(tmp, mod, out):
    """--nwn-root / --nwn-user given to the audit are used; every index it opens is closed."""
    groot = os.path.join(tmp, "fake_game")
    os.makedirs(os.path.join(groot, "data"))
    opened = []
    orig_db = nwn_audit._db

    def spy(path):
        c = orig_db(path)
        opened.append(c)
        return c
    with mock.patch.object(nwn_audit, "_db", spy), contextlib.redirect_stdout(io.StringIO()):
        nwn_audit.audit(out, os.path.join(out, "build"), "b1_clean", compiler=h.NO_COMPILER, nwn_root=groot,
                        verbose=False)
    cdb = sqlite3.connect(os.path.join(out, "build", "b1_clean_analysis", "index.sqlite"))
    meta = dict(cdb.execute("SELECT key, value FROM meta").fetchall())
    cdb.close()
    check("audit: an nwn_root given by the caller is used for the clean module's analysis",
          meta.get("nwn_root") == groot, meta.get("nwn_root"))

    def closed(c):
        try:
            c.execute("SELECT 1")
            return False
        except sqlite3.ProgrammingError:
            return True
    check("audit: every index it opened is closed afterwards", opened and all(closed(c) for c in opened), len(opened))


def lean_hak_names(tmp):
    taken = [f"x_r{k}" for k in range(1, 100)]
    try:
        nwn_hakslim.new_hak_names(["x"], taken)
        err = None
    except ValueError as ex:
        err = str(ex)
    check("new_hak_names: _r1.._r99 all taken raises a clear error instead of returning a taken name",
          err and "_r99" in err, err)
    check("new_hak_names: the next free name is still chosen", nwn_hakslim.new_hak_names(["x"], taken[:3]) == {"x": "x_r4"})
    # a file the plan keeps but no ERF can hold (name over 16 characters) is counted and listed, not called "kept"
    mod = os.path.join(tmp, "lh_mod"); hak = os.path.join(tmp, "lh_hak"); os.makedirs(hak)
    mtm.build(mod)
    open(os.path.join(hak, "hak_only.2da"), "w").write("2DA V2.0\n\n LABEL\n0 x\n")
    open(os.path.join(hak, "a_table_name_that_is_far_too_long.2da"), "w").write("2DA V2.0\n\n LABEL\n0 y\n")
    out = os.path.join(tmp, "lh_analysis")
    h.analyse(mod, out, [hak], tlk=os.path.join(mod, "testmod_custom.tlk"))
    with contextlib.redirect_stdout(io.StringIO()):
        man = nwn_hakslim.write_lean_haks(out, os.path.join(tmp, "lh_out"), verbose=False)
    lh = (man or {}).get("haks", {}).get("lh_hak") or {}
    packed = len(n.Erf(lh["file"]).entries) if lh else -1
    check("lean haks: files that can't be packed are counted separately and 'kept' = what is in the hak",
          lh.get("unpackable") == 1 and lh.get("unpackable_files") == ["a_table_name_that_is_far_too_long.2da"] and
          lh.get("kept") == packed and packed >= 1, lh)


def hakedit_names_and_placing(tmp):
    check("split_name: a name without a dot drops the folder part like a name with one",
          H.split_name("Folder/README") == ("readme", "") and H.split_name("Folder/A.TGA") == ("a", "tga"))
    d = os.path.join(tmp, "place"); os.makedirs(d)
    tmpf, dest = os.path.join(d, "x.tmp"), os.path.join(d, "x.hak")
    open(tmpf, "wb").write(b"new"); open(dest, "wb").write(b"old")
    try:
        H._place_new_file(tmpf, dest); refused = False
    except ValueError:
        refused = True
    check("install: a file that already has the new name is never replaced",
          refused and open(dest, "rb").read() == b"old" and os.path.exists(tmpf))
    os.remove(dest)
    H._place_new_file(tmpf, dest)
    check("install: otherwise the finished file gets its name and the temporary one is gone",
          open(dest, "rb").read() == b"new" and not os.path.exists(tmpf))


def build_command_line(tmp, out):
    """nwn_build's command line: plain one-line errors, real help, and warnings for scripts not found to compile."""
    nb = os.path.join(h.ROOT, "nwn_build.py")
    plan = os.path.join(tmp, "plan_cli.json")
    with open(plan, "w") as fh:
        json.dump({"delete": [], "merge": []}, fh)
    r = subprocess.run([sys.executable, nb, os.path.join(tmp, "no_such_analysis"), plan], capture_output=True, text=True)
    lines = (r.stdout + r.stderr).strip().splitlines()
    check("build CLI: a missing analysis folder prints one line and exits 2",
          r.returncode == 2 and len(lines) == 1 and "Traceback" not in r.stderr, (r.returncode, r.stdout, r.stderr[-300:]))
    r = subprocess.run([sys.executable, nb, out, os.path.join(tmp, "no_such_plan.json")], capture_output=True, text=True)
    lines = (r.stdout + r.stderr).strip().splitlines()
    check("build CLI: a missing plan file prints one line and exits 2",
          r.returncode == 2 and len(lines) == 1 and "Traceback" not in r.stderr, (r.returncode, r.stdout, r.stderr[-300:]))
    r = subprocess.run([sys.executable, nb, "--help"], capture_output=True, text=True)
    check("build CLI: --help exits 0 and explains --out, --name and --no-audit",
          r.returncode == 0 and all(re.search(o + r"[ \t]+\S", r.stdout) for o in ("--out OUT", "--name NAME", "--no-audit")),
          r.stdout[-800:])
    # an older Python gets one plain sentence before anything else runs (only nwn_build's view of sys is changed)
    with mock.patch.object(nwn_build, "sys", h.Proxy(sys, version_info=(3, 7, 9))), \
            contextlib.redirect_stdout(io.StringIO()) as buf:
        rc = nwn_build.main([os.path.join(tmp, "no_such_analysis"), plan])
    check("build CLI: on Python older than 3.8 it says so in one sentence and exits 2",
          rc == 2 and "needs Python 3.8 or newer (this is 3.7)" in buf.getvalue(), (rc, buf.getvalue()))
    # a script the compiler step was asked for but could not find gets its own warning
    import nwn_compile
    import nwn_edit
    def not_found(src, names, out_dir, *a, **k):
        return dict(status="ran", results={}, written=[], missing=list(names))
    try:
        nwn_edit.save_text(out, "zz_missing_cli.nss", "void main() { }\n", is_new=True)
        with mock.patch.object(nwn_compile, "compile_files", not_found), contextlib.redirect_stdout(io.StringIO()):
            log = nwn_build.build(out, {"delete": [], "merge": []}, run_audit=False, verbose=False)
    finally:
        nwn_edit.discard(out, "zz_missing_cli.nss")
    check("build: each script the compiler step could not find is named in a warning",
          any("zz_missing_cli" in w_ for w_ in log.get("warnings", [])), log.get("warnings"))


def hak_project_api(tmp):
    """HakEditor.project / set_analysis: the public way to read a project and attach an analysis."""
    hk = os.path.join(tmp, "proj_api.hak")
    n.write_erf(hk, [("a", "2da", b"2DA V2.0\n\n   Label\n0  x\n")], file_type="HAK ")
    ed = H.HakEditor(os.path.join(tmp, "ws_proj"))
    pid = ed.open(hk)
    p = ed.project(pid)
    check("hak editor: project(pid) returns the project dict", p["source"] == os.path.abspath(hk) and p["analysis"] is None, p)
    ed.set_analysis(pid, "some_analysis")
    a1 = ed.project(pid)["analysis"]
    ed.set_analysis(pid, "")
    a2 = ed.project(pid)["analysis"]
    check("hak editor: set_analysis attaches an analysis and '' detaches it", a1 == "some_analysis" and a2 is None, (a1, a2))
    try:
        ed.project("../x"); bad = False
    except ValueError:
        bad = True
    check("hak editor: project() refuses a malformed id", bad)
    src = open(H.__file__, encoding="utf-8").read()
    check("hak editor: permission messages name the system, not Windows, and say what may hold the file",
          "Windows would not let" not in src and src.count("The system would not let the toolkit") >= 2
          and "held by antivirus or a sync folder" in src)



# ------------------------------------------------------------------ hak Restore (the one in-place write left)
def hak_restore(tmp):
    """Restore of a backup an older toolkit made: the backup is verified first, the current file is saved as a
    verified safety backup (so a restore can be undone), nothing else in the hak folder changes, and a damaged
    backup is refused with the hak untouched."""
    hd = os.path.join(tmp, "restore_haks"); os.makedirs(hd)
    hak, neighbour = os.path.join(hd, "rs.hak"), os.path.join(hd, "other.hak")
    n.write_erf(hak, [("a", "txt", b"current")], file_type="HAK ")
    n.write_erf(neighbour, [("b", "txt", b"someone else's hak")], file_type="HAK ")
    he = H.HakEditor(os.path.join(tmp, "restore_ws"))
    pid = he.open(hak)
    # a backup as an older toolkit's install left it: <backups>/<project>/<id>/rs.hak + backup.json
    bid = "20240101-000000"
    bd = os.path.join(he.backup_root, pid, bid); os.makedirs(bd)
    bfile = os.path.join(bd, "rs.hak")
    n.write_erf(bfile, [("a", "txt", b"the old version")], file_type="HAK ")
    old_bytes = open(bfile, "rb").read()
    with open(os.path.join(bd, "backup.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(id=bid, file=bfile, of=hak, bytes=len(old_bytes), sha256=H.sha256_file(bfile),
                       made="2024-01-01 00:00:00", reason="before install (older toolkit)"), fh)
    current, other = open(hak, "rb").read(), open(neighbour, "rb").read()
    listing = sorted(os.listdir(hd))
    r = he.restore(pid, bid)
    sb = r["safety_backup"]
    check("restore: the hak gets the backup's bytes", open(hak, "rb").read() == old_bytes)
    check("restore: the current file is first saved as a verified safety backup, listed with the project's backups",
          open(sb["file"], "rb").read() == current and H.sha256_file(sb["file"]) == sb["sha256"] and
          any(x["id"] == sb["id"] for x in he.backups(pid)), sb)
    check("restore: nothing else in the hak folder is added or changed",
          sorted(os.listdir(hd)) == listing and open(neighbour, "rb").read() == other, os.listdir(hd))
    st_ = he.state(pid)
    check("restore: the project continues from the restored file (nothing staged, not 'changed on disk')",
          not st_["changes"] and not any("changed on disk" in c["msg"] for c in st_["checks"]), st_["checks"])
    he.restore(pid, sb["id"])
    check("restore: restoring the safety backup undoes the restore", open(hak, "rb").read() == current)
    # damage the old backup: one byte changed
    raw = bytearray(old_bytes); raw[-1] ^= 0xFF
    with open(bfile, "wb") as fh:
        fh.write(bytes(raw))
    count = len(he.backups(pid))
    try:
        he.restore(pid, bid); msg = ""
    except ValueError as ex:
        msg = str(ex)
    check("restore: a damaged backup is refused, the hak is untouched and no safety backup is made",
          "damaged" in msg and open(hak, "rb").read() == current and len(he.backups(pid)) == count, msg)
    try:
        he.restore(pid, "../../x"); refused = False
    except ValueError as ex:
        refused = "no such backup" in str(ex)
    check("restore: a backup id with path characters never reaches outside the backups folder", refused)


def _tree(folder):
    """{relative path: sha256} of every file under folder (to prove what an operation did and did not touch)."""
    out = {}
    for dp, _dn, fns in os.walk(folder):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, folder)] = n.sha256_file(p)
    return out


def install_to_game_folders(tmp):
    """Add to game folders (nwn_install): the plan refuses an existing target, a FAIL verdict and a changed build;
    install copies and verifies, records every file first, never replaces a file created after the plan; undo removes
    only unchanged files it placed; nothing outside the modules and hak folders is touched. Paths have spaces."""
    import nwn_install as I
    from _gff import st as st_, loc, X, w, tga
    base = h.fake_nwn_root(os.path.join(tmp, "ig root"), h.BASE)
    user = os.path.join(tmp, "ig user folder"); mods, hakd = os.path.join(user, "modules"), os.path.join(user, "hak")
    mod = os.path.join(tmp, "ig my module")
    for d in (mods, hakd, mod, os.path.join(user, "override")):
        os.makedirs(d)
    w(mod, "module.ifo", n.write_gff(root("IFO ", Mod_Name=loc("IG"), Mod_Entry_Area=(R, "area001"),
                                          Mod_Area_list=(LST, [st_(6, Area_Name=(R, "area001"))]),
                                          Mod_HakList=(LST, [st_(8, Mod_Hak=(X, "ig_a")), st_(8, Mod_Hak=(X, "ig_b"))]))))
    w(mod, "area001.are", n.write_gff(root("ARE ", Name=loc("Yard"), Tag=(X, "YARD"), ResRef=(R, "area001"),
                                           Tileset=(R, "tcn01"))))
    w(mod, "area001.git", n.write_gff(root("GIT ")))
    # ig_a loses a dead-end walkmesh (rebuilt as ig_a_r1); ig_b has nothing to drop (left alone, not copied)
    n.write_erf(os.path.join(hakd, "ig_a.hak"), [("ig_tex", "tga", tga(2, 2, lambda x, y: (1, 2, 3))),
                                                 ("ig_ghost", "wok", b"\0" * 40)], "HAK ")
    n.write_erf(os.path.join(hakd, "ig_b.hak"), [("ig_btex", "tga", tga(2, 2, lambda x, y: (4, 5, 6)))], "HAK ")
    out = os.path.join(tmp, "ig analysis")
    h.analyse(mod, out, nwn_root=base, nwn_user=user)
    with contextlib.redirect_stdout(io.StringIO()):
        log = nwn_build.build(out, {"delete": [], "merge": [], "lean_haks": True, "git": False}, name="ig clean",
                              verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER, nwn_root=base, nwn_user=user))
    before_user, before_mod = _tree(user), _tree(mod)
    try:
        I.plan_install(out, ""); refused = ""
    except ValueError as ex:
        refused = str(ex)
    check("add to game folders: no NWN user folder set -> a clear refusal naming Settings", "Settings" in refused, refused)
    p = I.plan_install(out, user)
    want = {(os.path.join(out, "build", "ig clean.mod"), os.path.join(mods, "ig clean.mod")),
            (os.path.join(out, "build", "haks", "ig_a_r1.hak"), os.path.join(hakd, "ig_a_r1.hak"))}
    check("plan: the .mod goes to modules, only the REBUILT hak to hak; the untouched hak and the tlk are explained",
          {(f["source"], f["target"]) for f in p["files"]} == want and log["audit"]["verdict"] != "FAIL" and
          any("ig_b.hak" in x and "not copied" in x for x in p["notes"]) and
          any("nothing is replaced" in x for x in p["notes"]) and _tree(user) == before_user, (p, log["audit"]["verdict"]))

    def refusal(fn):
        try:
            fn(); return ""
        except ValueError as ex:
            return str(ex)
    taken = os.path.join(mods, "IG CLEAN.mod")              # same name, other capitals: still taken
    open(taken, "wb").write(b"someone else's module")
    msg = refusal(lambda: I.plan_install(out, user))
    os.remove(taken)
    check("plan: refused when a target name already exists (any capitals), nothing copied",
          "already exists" in msg and not os.path.exists(os.path.join(mods, "ig clean.mod")), msg)
    ap = os.path.join(out, "build", "audit.json")
    aud = json.load(open(ap, encoding="utf-8"))
    json.dump(dict(aud, verdict="FAIL"), open(ap, "w", encoding="utf-8"))
    msg = refusal(lambda: I.plan_install(out, user))
    pf = I.plan_install(out, user, allow_fail=True)
    json.dump(aud, open(ap, "w", encoding="utf-8"))
    check("plan: refused when the audit verdict is FAIL, unless allow_fail (then said in the notes)",
          "FAIL" in msg and any("FAIL" in x for x in pf["notes"]) and pf["plan_id"] != p["plan_id"], msg)
    lean = os.path.join(out, "build", "haks", "ig_a_r1.hak")
    keep = open(lean, "rb").read()
    open(lean, "ab").write(b"x")
    msg = refusal(lambda: I.plan_install(out, user))
    open(lean, "wb").write(keep)
    check("plan: refused when a build file changed since the build (checksum)", "changed since" in msg, msg)
    # a file with a target's name appears between the plan and the copy: refused, nothing replaced
    p = I.plan_install(out, user)
    sneaky = os.path.join(hakd, "ig_a_r1.hak")
    open(sneaky, "wb").write(b"mine")
    msg = refusal(lambda: I.install(p))
    rec = json.load(open(os.path.join(out, "build", "installed.json"), encoding="utf-8"))
    left = [f for d in (mods, hakd) for f in os.listdir(d) if ".installing-" in f]
    check("install: a target created after the plan is never replaced; the files already placed are listed",
          open(sneaky, "rb").read() == b"mine" and "Already added" in msg and "ig clean.mod" in msg and
          rec["status"] == "failed" and [f["status"][:6] for f in rec["files"]] == ["placed", "failed"] and not left,
          (msg, rec, left))
    u = I.undo_install(out)
    check("undo after a failed add removes the placed .mod and never the file it did not add",
          not os.path.exists(os.path.join(mods, "ig clean.mod")) and open(sneaky, "rb").read() == b"mine" and
          u["status"] == "undone" and [f["undo"] for f in u["files"]] == ["removed", "nothing to remove: it was never added"],
          u)
    os.remove(sneaky)
    rec = I.install(I.plan_install(out, user))
    ok = all(n.sha256_file(f["target"]) == n.sha256_file(f["source"]) == f["sha256"] and
             f["bytes"] == os.path.getsize(f["target"]) and f["status"] == "placed" for f in rec["files"])
    disk = json.load(open(os.path.join(out, "build", "installed.json"), encoding="utf-8"))
    check("install: every file copied byte for byte, recorded with path, size, checksum and time",
          ok and disk["status"] == "added" and len(disk["files"]) == 2 and all(f.get("placed") for f in disk["files"]), disk)
    msg = refusal(lambda: I.plan_install(out, user))
    check("plan: a second add of the same build is refused (the names exist now) - Undo first", "already exists" in msg, msg)
    open(os.path.join(hakd, "ig_a_r1.hak"), "ab").write(b"changed by you")
    u = I.undo_install(out)
    check("undo: removes an unchanged added file, leaves (and lists) one changed since",
          not os.path.exists(os.path.join(mods, "ig clean.mod")) and os.path.isfile(os.path.join(hakd, "ig_a_r1.hak"))
          and [f["undo"][:12] for f in u["files"]] == ["removed", "left alone: "] and "changed" in u["files"][1]["undo"], u)
    check("undo: a second undo is refused", "already undone" in refusal(lambda: I.undo_install(out)))
    os.remove(os.path.join(hakd, "ig_a_r1.hak"))
    check("nothing outside the files it added was touched (originals, other folders, the module)",
          _tree(user) == before_user and _tree(mod) == before_mod, (sorted(set(_tree(user)) ^ set(before_user))))


def main():
    """Run every group of checks in a temporary folder; returns the exit code (tests/_harness.summary)."""
    global TMP
    with h.tempdir("nwn_edit_") as tmp:
        TMP = tmp
        h.run(editors_core)
        h.run(hak_editor_files)
        h.run(hak_restore, tmp)
        built = h.run(build_targets, tmp)
        h.run(build_include_precedence, tmp)
        if built:
            mod, out = built
            h.run(audit_options, tmp, mod, out)
            h.run(build_command_line, tmp, out)
        h.run(lean_hak_names, tmp)
        h.run(hakedit_names_and_placing, tmp)
        h.run(install_to_game_folders, tmp)
        h.run(hak_project_api, tmp)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
