"""
Tests for reading a nasher project (nwn_nasher.py): GFF files kept as <name>.<ext>.json (neverwinter.nim's nwn_gff
JSON) in sub-folders give the same analysis as the module they describe; the build from it writes an ordinary module
folder and .mod; the project folder is never written; unreadable JSON and a name held twice are reported; the quick
scan and the dashboard's folder browser recognise a project.
    python tests/test_nasher.py          (last line "N/M checks passed"; exit code 1 on a failure)
Works only in temporary folders and the test workspace (see tests/_harness.py).
"""
import _harness as h  # noqa: F401 - first: isolates the workspace and the compiler before toolkit modules load
import json
import os
import sys

import nwnlib as n  # noqa: E402
import nwn_build  # noqa: E402
import nwn_dashboard as D  # noqa: E402
import nwn_nasher  # noqa: E402
import nwn_quickscan  # noqa: E402
import make_test_module  # noqa: E402
from _harness import check  # noqa: E402
from test_update import tree_hash  # noqa: E402


def nasherise(mod, proj):
    """A nasher-style copy of the module folder `mod`: GFF files as src/<ext>/<name>.<ext>.json, everything else as
    src/<ext>/<name>.<ext>, plus nasher.cfg and a README (not game files)."""
    for fn in sorted(os.listdir(mod)):
        base, _, ext = fn.rpartition(".")
        ext = ext.lower()
        d = os.path.join(proj, "src", ext)
        os.makedirs(d, exist_ok=True)
        data = open(os.path.join(mod, fn), "rb").read()
        if ext in n.GFF_EXTENSIONS:
            with open(os.path.join(d, fn + ".json"), "w", encoding="utf-8") as fh:
                json.dump(n.gff_to_json(n.read_gff(data)), fh, indent=2)
        else:
            with open(os.path.join(d, fn), "wb") as fh:
                fh.write(data)
    with open(os.path.join(proj, "nasher.cfg"), "w") as fh:
        fh.write('[package]\nname = "Test"\n\n[target]\nname = "default"\nfile = "testmod.mod"\n')
    with open(os.path.join(proj, "README.md"), "w") as fh:
        fh.write("# test\n")
    return proj


def key(rep):
    """What must match between the two analyses: counts, issues, duplicate groups, deletion candidates."""
    s = rep["summary"]
    counts = {k: s.get(k) for k in ("module_files", "areas", "scripts", "items", "blueprints", "instances",
                                    "conversations", "quests")}
    return dict(counts=counts,
                issues=sorted((i["severity"], i["category"], i["node"]) for i in rep["issues"]),
                dups=sorted((d["category"], tuple(sorted(m["node"] for m in d["members"])), d["mergeable"])
                            for d in rep["duplicates"]),
                deletions=sorted((d["node"], d["status"]) for d in rep["deletions"]))


def recognise(tmp):
    """Which folders count as a nasher project."""
    mod = os.path.join(tmp, "plain")
    make_test_module.build(mod)
    proj = nasherise(mod, os.path.join(tmp, "proj"))
    check("detect: a project with nasher.cfg and src/**/module.ifo.json", nwn_nasher.is_project(proj))
    check("detect: an unpacked module folder (binary module.ifo) is not", not nwn_nasher.is_project(mod))
    check("detect: a folder with no project marker on top is never searched", not nwn_nasher.is_project(tmp))
    check("detect: a file is not", not nwn_nasher.is_project(os.path.join(proj, "nasher.cfg")))
    check("names: sword.uti.json is the item sword; x.nss stays; other .json and files are not resources",
          nwn_nasher.resource_name("Sword.UTI.json") == ("sword", "uti", True) and
          nwn_nasher.resource_name("x.nss") == ("x", "nss", False) and nwn_nasher.resource_name("package.json") is None
          and nwn_nasher.resource_name("nasher.cfg") is None and nwn_nasher.resource_name("x.zz.json") is None)
    check("browser: the dashboard's folder browser offers a project as a module (and not its parent)",
          D.GET_ROUTES["/api/ls"]({"path": proj}).get("is_module") is True and
          not D.GET_ROUTES["/api/ls"]({"path": tmp}).get("is_module"))
    q_mod, q_proj = nwn_quickscan.scan(mod), nwn_quickscan.scan(proj)
    check("quick scan: same name, areas, scripts and events as the module; kind 'nasher project'",
          q_proj["module"]["kind"] == "nasher project" and
          all(q_proj["module"].get(k) == q_mod["module"].get(k) for k in ("name", "areas", "scripts", "events", "conversations")),
          (q_proj["module"], q_mod["module"]))
    check("quick scan: nasher.cfg and README are listed as non-game files",
          any("non-game file" in w_["text"] for w_ in q_proj["warnings"]), q_proj["warnings"])


def same_analysis(tmp):
    """The project and the module it describes give the same analysis; the project is only read; a build works."""
    mod = os.path.join(tmp, "plain")
    make_test_module.build(mod)
    proj = nasherise(mod, os.path.join(tmp, "proj"))
    before = tree_hash(proj)
    a_mod = h.analyse(mod, os.path.join(tmp, "an_mod"))
    out = os.path.join(tmp, "an_proj")
    a_proj = h.analyse(proj, out)
    k1, k2 = key(a_mod), key(a_proj)
    for part in k1:
        check(f"same analysis: {part}", k1[part] == k2[part],
              lambda part=part: (sorted(set(map(str, k1[part])) ^ set(map(str, k2[part]))) if isinstance(k1[part], list)
                                 else (k1[part], k2[part])))
    conv = os.path.join(out, nwn_nasher.CONVERTED)
    check("convert: the analysis indexed a converted copy inside the analysis folder",
          a_proj["summary"]["module_path"] == os.path.abspath(conv) and
          a_proj["summary"]["module_source"] == os.path.abspath(proj), a_proj["summary"])
    check("convert: the copy holds each GFF file byte-for-byte as the module's binary",
          all(open(os.path.join(conv, f), "rb").read() == open(os.path.join(mod, f), "rb").read()
              for f in os.listdir(mod) if f.rpartition(".")[2].lower() in n.GFF_EXTENSIONS),
          [f for f in os.listdir(mod) if not os.path.exists(os.path.join(conv, f))])
    check("convert: non-game files (nasher.cfg, README) are not in it",
          not os.path.exists(os.path.join(conv, "nasher.cfg")) and not os.path.exists(os.path.join(conv, "README.md")))
    log = nwn_build.build(out, dict(delete=[], merge=[], allow_review=False), name="proj_clean", verbose=False,
                          audit_kwargs=dict(compiler=h.NO_COMPILER))
    bdir = os.path.join(out, "build", "proj_clean")
    check("build: an ordinary module folder (binary module.ifo, no .json) and a .mod",
          os.path.isfile(os.path.join(bdir, "module.ifo")) and not any(f.endswith(".json") for f in os.listdir(bdir))
          and log.get("mod_file") and os.path.isfile(log["mod_file"]), (log.get("mod_file"), os.listdir(bdir)[:8]))
    st = {c["id"]: c["status"] for c in log["audit"]["checks"]}
    check("build: the audit passes", "FAIL" not in st.values(), st)
    check("read-only: the project folder is byte-for-byte as it was", tree_hash(proj) == before)
    # analysing again converts afresh: a change in the project shows up
    p = os.path.join(proj, "src", "ifo", "module.ifo.json")
    j = json.load(open(p, encoding="utf-8"))
    j["Mod_Name"]["value"] = {"0": "Renamed Module"}
    json.dump(j, open(p, "w", encoding="utf-8"))
    a2 = h.analyse(proj, out)
    ifo = n.read_gff_file(os.path.join(conv, "module.ifo"))
    check("again: a change in the project's JSON reaches the converted copy",
          ifo.get("Mod_Name").text() == "Renamed Module" and a2["summary"]["module_source"] == os.path.abspath(proj),
          ifo.get("Mod_Name"))


def problems(tmp):
    """A JSON file that can't be read, and the same resource twice in the project."""
    mod = os.path.join(tmp, "plain")
    make_test_module.build(mod)
    proj = nasherise(mod, os.path.join(tmp, "proj"))
    os.makedirs(os.path.join(proj, "src", "old"))
    with open(os.path.join(proj, "src", "uti", "broken.uti.json"), "w") as fh:
        fh.write("{ not json")
    with open(os.path.join(proj, "src", "uti", "badtype.uti.json"), "w") as fh:
        fh.write('{"__data_type": "UTI ", "Tag": {"type": "nosuchtype", "value": 1}}')
    first = sorted(f for f in os.listdir(os.path.join(proj, "src", "uti")) if f.endswith(".uti.json") and
                   not f.startswith(("broken", "badtype")))[0]
    with open(os.path.join(proj, "src", "uti", first), "rb") as src, \
            open(os.path.join(proj, "src", "old", first), "wb") as dst:
        dst.write(src.read())
    rep = h.analyse(proj, os.path.join(tmp, "an_bad"))
    bad = [i for i in rep["issues"] if i["category"] == "json_unreadable"]
    check("unreadable: each bad JSON file is an error naming it; the analysis still finishes",
          len(bad) == 2 and any("broken.uti.json" in i["detail"] for i in bad) and
          any("badtype.uti.json" in i["detail"] for i in bad), bad)
    dup = [i for i in rep["issues"] if i["category"] == "json_duplicate_name"]
    check("twice: the same name in two folders is a warning; the first in path order is used",
          len(dup) == 1 and first[:-5] in dup[0]["detail"] and "src/old" in dup[0]["detail"].replace("\\", "/") and
          "used" in dup[0]["detail"], dup)
    import nwn_fixes
    check("fixes: both have their own How to fix", all(nwn_fixes.fix_for({"category": c}) != nwn_fixes.fix_for(
        {"category": "zz_none"}) for c in ("json_unreadable", "json_duplicate_name")))


def exporting(tmp):
    """A module (folder or .mod) written as a nasher project reads back to the same bytes; the Build page's
    Save as nasher project writes the last build beside it, inside the analysis."""
    import time
    mod = os.path.join(tmp, "plain")
    make_test_module.build(mod)
    before = tree_hash(mod)
    r = nwn_nasher.export(mod, os.path.join(tmp, "exp"), name="Test Module")
    files = [f for f in os.listdir(mod) if f.rpartition(".")[2].lower() in n.EXT_TO_RESTYPE]
    gff = [f for f in files if f.rpartition(".")[2].lower() in n.GFF_EXTENSIONS]
    check("export: every GFF file as src/<ext>/<name>.<ext>.json, the rest as src/<ext>/<name>.<ext>",
          r["json"] == len(gff) and r["files"] == len(files) - len(gff) and not r["errors"] and
          all(os.path.isfile(os.path.join(r["dest"], "src", f.rpartition(".")[2].lower(), f.lower() + ".json")) for f in gff),
          r)
    cfg = open(os.path.join(r["dest"], "nasher.cfg"), encoding="utf-8").read()
    check("export: a starter nasher.cfg names the module and the .mod", 'name = "Test Module"' in cfg and
          'file = "plain.mod"' in cfg and '"*" = "src/$ext"' in cfg, cfg)
    check("export: the result is recognised as a nasher project", nwn_nasher.is_project(r["dest"]))
    back = nwn_nasher.convert(r["dest"], os.path.join(tmp, "back"))
    check("round trip: converting the export back gives every file byte-for-byte",
          not back["errors"] and all(open(os.path.join(mod, f), "rb").read() ==
                                     open(os.path.join(tmp, "back", f.lower()), "rb").read() for f in files))
    check("read-only: the module folder is unchanged", tree_hash(mod) == before)
    mine = os.path.join(tmp, "mine")
    os.makedirs(mine)
    open(os.path.join(mine, "notes.txt"), "w").write("mine")
    for bad, why in ((mine, "already exists"), (os.path.join(mod, "x"), "inside the module")):
        try:
            nwn_nasher.export(mod, bad)
            ok_ = False
        except ValueError as ex:
            ok_ = why in str(ex)
        check(f"export: refused into a folder that is not an earlier export ({why})", ok_ and
              open(os.path.join(mine, "notes.txt")).read() == "mine" and tree_hash(mod) == before)
    check("command line: export prints what it wrote; convert refuses a folder that is not a project",
          nwn_nasher.main(["export", mod, os.path.join(tmp, "cli_exp")]) == 0 and
          nwn_nasher.main(["convert", mod, os.path.join(tmp, "cli_conv")]) == 2)
    # from a .mod, and again over an earlier export (replaced whole: a file gone from the module goes from the export)
    modfile = os.path.join(tmp, "plain.mod")
    n.write_erf(modfile, [(f.rpartition(".")[0].lower(), f.rpartition(".")[2].lower(),
                           open(os.path.join(mod, f), "rb").read()) for f in files if f != gff[0]], "MOD ")
    r2 = nwn_nasher.export(modfile, os.path.join(tmp, "exp"))
    check("export: from a .mod too, replacing the earlier export",
          r2["json"] == len(gff) - 1 and not os.path.exists(os.path.join(tmp, "exp", "src", gff[0].rpartition(".")[2].lower(),
                                                                             gff[0].lower() + ".json")), r2)
    # the dashboard
    name = "nash_api"
    out = os.path.join(D.WORKSPACE, name)
    h.analyse(mod, out)
    try:
        D.POST_ROUTES["/api/build/nasher"]({"a": name})
        refused = False
    except ValueError as ex:
        refused = "Build & audit first" in str(ex)
    check("api: no build yet - refused, saying to build first", refused)
    nwn_build.build(out, dict(delete=[], merge=[]), name="nash_clean", verbose=False, audit_kwargs=dict(compiler=h.NO_COMPILER))
    j = D.POST_ROUTES["/api/build/nasher"]({"a": name})
    t0 = time.time()
    while D.JOBS[j["job"]].status == "running" and time.time() - t0 < 60:
        time.sleep(0.2)
    job = D.JOBS[j["job"]]
    check("api: the build is saved as a nasher project beside it, in the analysis's build folder",
          job.status == "done" and j["dest"] == os.path.join(out, "build", "nash_clean_nasher") and
          nwn_nasher.is_project(j["dest"]), (job.status, job.lines[-4:], j))
    page = open(os.path.join(h.ROOT, "dashboard.html"), encoding="utf-8").read()
    check("page: the Build page has Save as nasher project; the Modules page names nasher projects",
          'id="bnash"' in page and "/api/build/nasher" in page and "a nasher project folder (JSON files)" in page)


def main():
    """Run every group of checks; returns the exit code (tests/_harness.summary)."""
    for fn in (recognise, same_analysis, problems, exporting):
        with h.tempdir() as t:
            h.run(fn, t)
    return h.summary()


if __name__ == "__main__":
    sys.exit(main())
