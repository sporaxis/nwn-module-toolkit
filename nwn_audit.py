"""
nwn_audit.py - deterministic audit & validation of a clean build against the original.

Why this exists
---------------
A clean build (nwn_build.py) removes, merges, re-points and recompiles things. Before anyone uses it, this module
proves - by re-analysing the clean module from scratch and comparing it with the original analysis - that only the
logged changes happened and nothing in use was lost. It does not trust the build's own log: the log is only used
to tell expected differences from unexpected ones.

Every check is evidence-based and repeatable. Verdict: PASS, PASS WITH WARNINGS, or FAIL.

What each check proves (id: FAIL / WARN conditions)
    parse          every file of the clean module (and its haks) parses; a new parse error FAILs, an error the
                   original already had WARNs
    mod            the packed .mod holds exactly the files of the clean folder, byte for byte (read back and hashed)
    removals       the module's file list lost exactly the approved files and gained nothing except files added in
                   the editor and newly compiled .ncs
    modified       every file whose bytes changed was edited in the editor, recompiled, or rewritten by the build -
                   and for rewritten GFF files every changed field equals a logged change
    blueprint_changes  (only when the build applied bulk changes) each changed blueprint in the clean module holds
                   its new value; placed copies that followed are covered by "modified" and "placements"
    palette_moves  (only when the build applied palette moves) each moved blueprint in the clean module holds its new
                   palette number, and where the build moved its palette-file entry, the file lists it there
    edits_compiled edited/new scripts have a fresh .ncs (WARN only: the old compiled code would run)
    include_users  every script that includes an edited include was recompiled (FAIL otherwise)
    references     no new error-level issue (broken reference, validation error) compared with the original; new
                   warnings WARN. Renamed haks are mapped back to their old names before comparing
    areas          every kept area keeps its object counts and its place in the module's area list
    placements     every placed object keeps its area, tag and x/y position (changes in areas you edited WARN)
    quests         journal categories and their entry IDs are unchanged
    conversations  every kept conversation has the same numbers of entries, replies and starting points
    in_use         everything reachable from the module before is still present and reachable (with lean haks:
                   reachable through the lean haks; a hak the build left alone is read from its own folder)
    lean_haks      with lean haks, every 2da cell is identical to the original's
    dangling       nothing in use still refers to a removed resource
    compile        every script compiles with the official compiler; only failures the original did not have FAIL.
                   SKIPPED when the compiler isn't installed - never a clean PASS then

Reads: the original analysis folder, the build folder (changes.json, the clean folder, the .mod, haks/), the
original module (read only, for field comparison and compiling). Writes: <build>/<name>_analysis/ (a full analysis
of the clean module), audit.json and audit.md in the build folder. Never changes the clean module or the original.
The compiler always works on temporary copies of the scripts (nwn_compile).

Limits: the conversation check compares counts, not text; "references" compares issue signatures, so a new problem
whose signature matches an existing one is not reported again.

    python nwn_audit.py <original analysis folder> <build folder> <clean module name>
                        [--compiler PATH] [--nwn-root PATH] [--nwn-user PATH]
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import re
import sys
import time
from collections import defaultdict  # noqa

import nwnlib as n
import nwn_analysis
import nwn_compile
import nwn_index


def _db(path):
    """Open an analysis folder's index.sqlite (the audit only runs SELECTs on it)."""
    return sqlite3.connect(os.path.join(path, "index.sqlite"))


def _module_files(db):
    """{relpath: sha256} of the module's own files (not haks or override)."""
    return {r[0]: r[1] for r in db.execute(
        "SELECT f.relpath, f.sha256 FROM files f JOIN sources s ON s.id=f.source_id WHERE s.kind='module'")}


def _override_only_nodes(db):
    """Graph nodes whose every file in this index comes from the user override folder (none from the module, a hak
    or the base game). Read-only."""
    kinds = {}
    for node, kind in db.execute("SELECT f.node, s.kind FROM files f JOIN sources s ON s.id=f.source_id "
                                 "WHERE f.node IS NOT NULL"):
        kinds.setdefault(node, set()).add(kind)
    return {nd for nd, ks in kinds.items() if ks == {"override"}}


def _player_2da_cells(db):
    """{(table, row, column): value} of the copy of each 2da a player's game loads: the winning hak or module copy
    (nwnlib.source_rank), whole. A copy in the builder's override folder is left out - players don't have it, and
    the clean module's re-index never reads it."""
    best = {}
    for name, sid, kind, prio in db.execute("SELECT DISTINCT t.name, s.id, s.kind, s.priority FROM twoda t JOIN files f "
                                            "ON f.id=t.file_id JOIN sources s ON s.id=f.source_id WHERE s.kind != 'override'"):
        r = (n.source_rank(kind, prio), sid)
        if name not in best or r < best[name]:
            best[name] = r
    return {(t, row, col): v for t, row, col, v, sid in db.execute(
        "SELECT t.name, t.row, t.col, t.value, f.source_id FROM twoda t JOIN files f ON f.id=t.file_id")
            if t in best and best[t][1] == sid}


def _counts_sentence(counts):
    """{'error': 5, 'warning': 7, 'info': 8} -> "5 errors, 7 warnings, 8 info" (none -> "none")."""
    words = {"error": ("error", "errors"), "warning": ("warning", "warnings"), "info": ("info", "info")}
    parts = [f"{counts[k]} {words.get(k, (k, k))[counts[k] != 1]}" for k in ("error", "warning", "info") if counts.get(k)]
    parts += [f"{v} {k}" for k, v in counts.items() if k not in words and v]
    return ", ".join(parts) or "none"


def _logval(v):
    """A field value as the change log writes it (localised texts as their text form)."""
    return str(v) if isinstance(v, n.LocString) else v


def _palette_listing(root):
    """{resref: ID of the category that lists it} for a parsed palette file (None -> {})."""
    found = {}

    def walk(s, cid):
        for f in s.fields.values():
            if f.type == n.LIST:
                for c in f.value:
                    rr = (c.get("RESREF") or "").lower()
                    if rr:
                        found.setdefault(rr, cid)
                    else:
                        walk(c, c.get("ID"))
    if root is not None:
        walk(root, None)
    return found


def audit(orig_dir, build_dir, name, compiler=None, nwn_root=None, nwn_user=None, dirs=(), verbose=True):
    """Audit the clean build <build_dir>/<name> against the original analysis and write audit.json / audit.md.

    orig_dir: the ORIGINAL module's analysis folder. build_dir: the build folder (holds changes.json, <name>/ and
    <name>.mod). name: the clean module's name.
    compiler: path of nwn_script_comp (None = look in the usual places). dirs: extra include folders for it.
    nwn_root / nwn_user: game install / user folder for the clean module's analysis and the compiler. None (the
    default) = the values the original analysis recorded (see the comment there).
    Returns dict(verdict, checks[{id, title, status, evidence, explain}], when, original, clean, mod, summary, git).
    Writes only inside build_dir (the clean module's analysis and the reports); never changes the clean module
    or the original. Each check is described in the module docstring."""
    t0 = time.time()
    clean_folder = os.path.join(build_dir, name)
    clean_dir = os.path.join(build_dir, f"{name}_analysis")
    changes = json.load(open(os.path.join(build_dir, "changes.json"), encoding="utf-8"))
    with contextlib.closing(_db(orig_dir)) as odb0:      # closed at once: an open handle locks the file on Windows
        ometa = dict(odb0.execute("SELECT key, value FROM meta").fetchall())
    # the clean module is analysed with the original's haks, swapped for the haks the build actually points at:
    # Hak-editor rebuilds first, then lean haks - so the checks below test the haks a player would really load
    haks = json.loads(ometa.get("haks") or "[]")
    ed = {k.lower(): v for k, v in (changes.get("editor_haks") or {}).items()}
    if ed:                                   # haks your module.ifo edit points at instead (Hak-editor rebuilds)
        haks = [ed.get(os.path.splitext(os.path.basename(os.path.normpath(h)))[0].lower(), h) for h in haks]
    lean_dir = os.path.join(build_dir, "haks")
    if changes.get("lean_haks") and os.path.isdir(lean_dir):
        lean = []
        by_name = {k.lower(): v for k, v in changes["lean_haks"].items()}
        for h in haks:
            nm = os.path.splitext(os.path.basename(os.path.normpath(h)))[0]
            info = by_name.get(nm.lower()) or {}
            if info.get("unchanged"):
                # nothing to drop, so the build left this hak alone: the clean module loads the original, from where
                # it is (the user's hak folder), under its own name
                lean.append(h)
                continue
            lp = info.get("file") or os.path.join(lean_dir, f"{nm}.hak")   # new name (or an older build's)
            lean.append(lp if lp and os.path.isfile(lp) else h)
        haks = lean  # the audit's reference checks now prove the lean haks are sufficient
    # by default the clean module is checked with the SAME game folder the original was analysed with - otherwise every
    # base-game resource would look new (or newly missing) just because the setting changed. A folder given by the
    # caller (--nwn-root / --nwn-user, or the dashboard's settings) is used as given
    nwn_root = nwn_root or ometa.get("nwn_root") or None
    nwn_user = nwn_user or ometa.get("nwn_user") or None
    # a full, independent analysis of the clean module into <build>/<name>_analysis (nothing from the build reused).
    # overrides=[]: the builder's own override folder is left out - players don't have it, so a check that passes
    # only because a file sits there (a 2da or texture a lean hak dropped) would hide a broken build
    nwn_index.run_index(clean_folder, haks=haks, overrides=[], tlk=ometa.get("tlk") or None, out=clean_dir,
                        write_json=False, verbose=False, nwn_root=nwn_root, nwn_user=nwn_user)
    nwn_analysis.run_analysis(clean_dir, verbose=False)
    import nwn_progress
    nwn_progress.emit(phase="checks", stage="comparing the clean module with the original")
    o_rep = json.load(open(os.path.join(orig_dir, "report.json"), encoding="utf-8"))
    c_rep = json.load(open(os.path.join(clean_dir, "report.json"), encoding="utf-8"))
    odb, cdb = _db(orig_dir), _db(clean_dir)
    checks = []

    def check(cid, title, status, evidence, explain):
        """Record one check result; evidence is capped at 200 lines."""
        checks.append(dict(id=cid, title=title, status=status, evidence=evidence[:200], explain=explain))

    deleted_files = {d["file"] for d in changes["deleted"]}
    removed_nodes = {d["node"] for d in changes["deleted"]}

    # 1 parse - proves every file (module and haks) re-reads; only errors the original did not have FAIL
    o_err = {r[0] for r in odb.execute("SELECT relpath FROM files WHERE status='error'")}
    c_err = [r[0] for r in cdb.execute("SELECT relpath FROM files WHERE status='error'")]
    new_err = [f for f in c_err if f not in o_err]
    check("parse", "Every file in the clean module can be read",
          "FAIL" if new_err else ("WARN" if c_err else "PASS"),
          [f"new parse error: {f}" for f in new_err] + [f"pre-existing parse error: {f}" for f in c_err if f in o_err],
          "Each GFF/2DA/script file was re-read by the parser.")

    # 2 .mod integrity - proves the packed .mod is exactly the clean folder: same file names, same SHA-256 per file
    mod = changes.get("mod_file")
    if mod and os.path.exists(mod):
        erf = n.Erf(mod)
        ev = []
        folder = {}
        for root, dirs_, fs in os.walk(clean_folder):
            dirs_[:] = [d_ for d_ in dirs_ if not d_.startswith(".")]     # skip .git (in-place edit prunes os.walk)
            for fn in fs:
                p = os.path.join(root, fn)
                ext_ = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                if fn.startswith(".") or ext_ not in n.EXT_TO_RESTYPE:
                    continue  # non-game files are not packed (listed in the change log)
                folder[fn.lower()] = p  # ERF entries have no folders
        names = {e.filename for e in erf.entries}
        for fn in sorted(set(folder) - names):
            ev.append(f"in folder but not in .mod: {fn}")
        for fn in sorted(names - set(folder)):
            ev.append(f"in .mod but not in folder: {fn}")
        for e in erf.entries:
            if e.filename in folder:
                if hashlib.sha256(erf.read(e)).hexdigest() != hashlib.sha256(open(folder[e.filename], "rb").read()).hexdigest():
                    ev.append(f"content differs: {e.filename}")
        check("mod", ".mod file matches the clean folder byte-for-byte", "FAIL" if ev else "PASS",
              ev or [f"{len(erf.entries)} resources verified"], "The packed .mod was read back and hashed.")
    else:
        check("mod", ".mod file matches the clean folder byte-for-byte", "FAIL",
              ["no .mod written: " + "; ".join(w for w in changes.get("warnings", []) if "mod" in w.lower())[:300]],
              "The build could not pack a .mod - fix the listed names/files and rebuild.")

    # 3 removal accounting - proves the file list changed only as approved: every missing file is in the change log's
    # deletions, every logged deletion is really gone, and no file appeared that the log doesn't explain
    ofiles, cfiles = _module_files(odb), _module_files(cdb)
    missing = set(ofiles) - set(cfiles)
    unexpected_missing = sorted(missing - deleted_files)
    not_removed = sorted(deleted_files - missing)
    compiled_new = {f"{nm}.ncs" for nm in changes.get("compiled", [])}
    added = sorted(set(cfiles) - set(ofiles) - {a.lower() for a in changes.get("added", [])} - compiled_new)
    ev = [f"removed without approval: {f}" for f in unexpected_missing] + \
         [f"approved for removal but still present: {f}" for f in not_removed] + [f"unexpected new file: {f}" for f in added]
    check("removals", "Only the approved files were removed, nothing added", "FAIL" if ev else "PASS",
          ev or [f"{len(missing)} files removed, all on the approved list"], "Original vs clean file lists compared.")

    # 4 modified files - every changed FIELD must be a logged change. Files are compared by SHA-256 first; only
    # files whose bytes differ are opened and compared field by field (leaf path -> (type, value))
    changed = sorted(f for f in set(ofiles) & set(cfiles) if ofiles[f] != cfiles[f])
    logged = defaultdict(dict)
    for c in changes["field_changes"]:
        logged[c["file"]][c["field"]] = c
    ev = []
    o_src = odb.execute("SELECT path FROM sources WHERE kind='module'").fetchone()[0]
    o_erf = None if os.path.isdir(o_src) else n.Erf(o_src)
    o_entries = {e.filename: e for e in o_erf.entries} if o_erf else {}
    edited = {e.lower() for e in changes.get("edited", [])}
    for f in changed:
        if f.lower() in edited or f.lower() in compiled_new:
            continue  # deliberately edited in the editor / recompiled - listed in the change log
        if f not in set(changes["rewritten"]):
            ev.append(f"changed without a logged reason: {f}"); continue
        ext = f.rsplit(".", 1)[-1].lower()
        if ext not in n.GFF_EXTENSIONS:
            ev.append(f"non-GFF file changed: {f}"); continue
        ob = o_erf.read(o_entries[f]) if o_erf else open(os.path.join(o_src, f), "rb").read()
        cb = open(os.path.join(clean_folder, f), "rb").read()
        ol = {p_: (t, v) for p_, lab, t, v, _ in n.iter_gff_leaves(n.read_gff(ob))}
        cl = {p_: (t, v) for p_, lab, t, v, _ in n.iter_gff_leaves(n.read_gff(cb))}
        if ext == "itp":  # palette: only logged entries may disappear, nothing may change or appear
            removed = {c["old"] for c in logged[f].values()}
            ovals = sorted(str(v[1]).lower() for v in ol.values())
            cvals = sorted(str(v[1]).lower() for v in cl.values())
            extra = [v for v in cvals if v not in ovals]
            gone_rr = {str(v[1]).lower() for p_, v in ol.items() if p_.endswith("RESREF")} - \
                      {str(v[1]).lower() for p_, v in cl.items() if p_.endswith("RESREF")}
            if extra or not gone_rr <= {str(r).lower() for r in removed}:
                ev.append(f"{f}: palette changed beyond the logged removals")
            continue
        for p_ in sorted(set(ol) | set(cl)):
            if ol.get(p_) != cl.get(p_):
                lg = logged[f].get(p_)
                # the field must be logged AND hold the logged new value (resrefs compare case-insensitively)
                if not lg or str(cl.get(p_, (0, None))[1]).lower() != str(lg["new"]).lower():
                    ev.append(f"{f}: field {p_} changed but not logged ({ol.get(p_, (0, None))[1]!r} -> "
                              f"{cl.get(p_, (0, None))[1]!r})")
                    break                   # one unlogged field is enough to fail this file
    check("modified", "Every changed field is exactly a logged change", "FAIL" if ev else "PASS",
          ev or [f"{len(changed)} file(s) changed; every changed field matches the change log "
                 f"({len(changes['field_changes'])} field edits)"],
          "Changed files are compared field by field with the original.")

    # 4b palette moves (Blueprints page) - proves each applied move landed: the blueprint holds its new number, and
    # an entry the build moved is listed under the category with that number. "modified" above already proved
    # nothing else changed in those files.
    applied = [m for m in changes.get("palette_moves") or [] if m.get("status") == "moved"]
    if changes.get("palette_moves"):
        ev, by_name = [], {os.path.basename(f).lower(): f for f in cfiles}
        import nwn_palette

        def clean_gff(fn):
            rel = by_name.get(fn.lower())
            return n.read_gff_file(os.path.join(clean_folder, rel)) if rel else None
        listed = {}                            # ext -> {resref: ID of the category listing it}, read once per type
        for m in applied:
            ext, rr, to = m["type"], m["resref"], m["to"]
            try:
                bp = clean_gff(f"{rr}.{ext}")
                have = bp.get(nwn_palette.PALETTE_FIELD[ext]) if bp is not None else None
                if have != to:
                    ev.append(f"{rr}.{ext}: palette number is {have!r}, the change log says {to}")
                if m.get("entry_moved"):
                    if ext not in listed:
                        listed[ext] = _palette_listing(clean_gff(nwn_palette.TYPES[ext][0] + "palcus.itp"))
                    if listed[ext].get(rr) != to:
                        ev.append(f"{rr}.{ext}: the palette file lists it under category {listed[ext].get(rr)!r}, "
                                  f"not {to}")
            except (OSError, n.GffError) as ex:
                ev.append(f"{rr}.{ext}: could not be read ({ex})")
        skipped = len(changes["palette_moves"]) - len(applied)
        check("palette_moves", "Palette moves landed where the change log says", "FAIL" if ev else "PASS",
              ev or [f"{len(applied)} blueprint(s) moved to their new palette category" +
                     (f"; {skipped} skipped (see the change log)" if skipped else "")],
              "Each moved blueprint in the clean module holds its new palette number, and the palette file lists it "
              "under that category.")

    # 4c bulk blueprint changes (Blueprints page) - proves each applied change is in the clean blueprint
    if changes.get("blueprint_changes"):
        import nwn_bpchanges
        ev, done = [], [c for c in changes["blueprint_changes"] if c.get("status") == "changed"]
        by_name = {os.path.basename(f).lower(): f for f in cfiles}
        roots = {}
        for c in done:
            fn = f"{c['resref']}.{c['type']}"
            try:
                if fn not in roots:
                    rel = by_name.get(fn)
                    roots[fn] = n.read_gff_file(os.path.join(clean_folder, rel)) if rel else None
                bp = roots[fn]
                label = nwn_bpchanges.FIELDS[c["type"]][c["field"]]
                have = bp.get(label) if bp is not None else None
                want = nwn_bpchanges.new_value(c["field"], c["to"])
                if have != want:
                    ev.append(f"{fn}: {c['field']} is {_logval(have)!r}, the change log says {c['to']!r}")
            except (OSError, n.GffError, KeyError, ValueError) as ex:
                ev.append(f"{fn}: could not be checked ({ex})")
        skipped = len(changes["blueprint_changes"]) - len(done)
        check("blueprint_changes", "Bulk blueprint changes landed where the change log says", "FAIL" if ev else "PASS",
              ev or [f"{len(done)} field change(s) in {len({(c['type'], c['resref']) for c in done})} blueprint(s), "
                     f"{sum(c.get('placed', 0) for c in done)} placed cop(ies) followed" +
                     (f"; {skipped} skipped (see the change log)" if skipped else "")],
              "Each changed blueprint in the clean module holds its new value; placed copies that followed are logged "
              "changes, checked field by field above.")

    # 5 references - proves the clean module has no NEW issue (broken reference, validation error) compared with the
    # original. Issues are compared by signature (category, node, detail). A rebuilt hak's new name in a detail is
    # mapped back to the old name (back: new name -> old name), or every issue naming that hak would look new
    back = {v.lower(): k.lower() for k, v in (changes.get("renamed_haks") or {}).items()}
    back.update({os.path.splitext(os.path.basename(v))[0].lower(): k.lower()
                 for k, v in (changes.get("editor_haks") or {}).items()})
    # matches any new hak name as a whole name, e.g. "acpv41_r1" in "acpv41_r1.hak is missing" but not inside a longer
    # name. Hak names may start or end with characters \b doesn't treat as word characters ("+sic11", "bvi80_v1.0"),
    # so the edges are spelled out: no name character (letters, digits, _ + - .) directly before or after.
    back_re = re.compile(r"(?<![\w+.\-])(" + "|".join(map(re.escape, sorted(back, key=len, reverse=True))) +
                         r")(?![\w+\-]|\.\w)", re.I) if back else None

    def sig(i):
        """Comparable signature of an issue: (category, node, detail without its 'used by ...' tail)."""
        # ignore the 'used by ...' tail: deleting one user of a missing resource is not a new problem
        d = re.sub(r"[;,]?\s*used by .*$", "", i["detail"])
        if back_re:                     # a rebuilt hak has a new name - it is still the same hak
            d = back_re.sub(lambda m: back[m.group(1).lower()], d)
        if i["category"] == "resource_conflict":
            # the clean module is re-indexed without the builder's override folder (players don't have it), so the
            # override is left out of both sides' list of places; a "conflict" left with one place is no conflict
            places = [p_ for p_ in d.split(": ", 1)[-1].split(" | ") if p_.strip() and p_.strip() != "override"]
            d = " | ".join(places) if len(places) > 1 else ""
        return (i["category"], i["node"], d)
    o_sig = {sig(i) for i in o_rep["issues"]}
    edited_scripts = {"script:" + os.path.basename(f).lower()[:-4] for f in changes.get("edited", []) + changes.get("added", [])
                      if f.lower().endswith(".nss")}
    # a conflict is only new if the clean module has a place the original didn't: lean haks drop identical copies,
    # so the same resource in FEWER places is the build working, not a new problem
    o_places = {}
    for i in o_rep["issues"]:
        if i["category"] == "resource_conflict":
            o_places.setdefault(i["node"], set()).update(p_ for p_ in sig(i)[2].split(" | ") if p_)

    def is_new(i):
        s_ = sig(i)
        if s_[2] == "":                          # a resource_conflict that only involved the override
            return False
        if i["category"] == "resource_conflict" and i["node"] in o_places:
            return not set(s_[2].split(" | ")) <= o_places[i["node"]]
        return s_ not in o_sig
    new_issues = [i for i in c_rep["issues"] if i["severity"] in ("error", "warning") and is_new(i)]
    # "not compiled" on a script you edited is reported by edits_compiled below (a WARN), not as a broken reference
    uncompiled_edits = [i for i in new_issues if i["category"] == "not_compiled" and i["node"] in edited_scripts]
    new_issues = [i for i in new_issues if i not in uncompiled_edits]
    new_errors = [i for i in new_issues if i["severity"] == "error"]
    # an edited script whose .ncs was carried over from the original: the game runs the OLD compiled code
    compiled_now = {c.lower() for c in changes.get("compiled", [])}
    stale_ncs = sorted(sn[7:] for sn in edited_scripts
                       if sn[7:] not in compiled_now and os.path.isfile(os.path.join(clean_folder, sn[7:] + ".ncs")))
    if uncompiled_edits or stale_ncs:
        check("edits_compiled", "Edited/new scripts were compiled", "WARN",
              [f"{i['node'][7:]}.nss has no compiled .ncs - it will not run until compiled (install nwn_script_comp "
               "or compile in the toolset)" for i in uncompiled_edits] +
              [f"{x}.nss was edited but {x}.ncs is still the ORIGINAL compiled version - the game runs the old code until "
               "you compile it (toolset: Build > Compile, or install nwn_script_comp)" for x in stale_ncs],
              "Scripts changed in the editor need compiling.")
    elif edited_scripts:
        check("edits_compiled", "Edited/new scripts were compiled", "PASS",
              [f"{len(edited_scripts)} edited/new script(s) have compiled .ncs files"], "")
    # include_users - proves every script that #includes an edited include got a new .ncs in this build
    # (include_users lists only scripts with a .nss source; the build warns about the others)
    if changes.get("edited_includes"):
        users = changes.get("include_users") or []
        stale = sorted(set(users) - compiled_now)
        check("include_users", "Every script that includes an edited include was recompiled",
              "FAIL" if stale else "PASS",
              ([f"{x}.ncs is still the ORIGINAL compiled version although it includes an edited include "
                f"({', '.join(changes['edited_includes'])}) - the game would run the old code" for x in stale] or
               [f"{len(users)} script(s) recompiled because they include {', '.join(changes['edited_includes'])}"]),
              "An include is copied into every script that uses it when that script is compiled, so editing an include "
              "changes nothing in game until all of them are recompiled.")
    check("references", "No new broken references or validation errors",
          "FAIL" if new_errors else ("WARN" if new_issues else "PASS"),
          [f"NEW {i['severity']}: {i.get('label') or i['node']}: {i['detail']}" for i in new_issues] or
          [f"clean module issues: {_counts_sentence(c_rep['summary']['issues'])} (original: "
           f"{_counts_sentence(o_rep['summary']['issues'])})"],
          "Full validation re-run on the clean module and compared with the original.")

    # 6 areas / placed objects - proves every kept area keeps its listing in module.ifo and its object counts per type
    def area_counts(rep):
        return {a["node"]: (a["in_module_list"], a["counts"]) for a in rep["areas"]}
    oa, ca = area_counts(o_rep), area_counts(c_rep)
    ev = []
    for a, v in oa.items():
        if a in removed_nodes:
            continue
        if a not in ca:
            ev.append(f"area missing: {a}")
        elif ca[a] != v:
            ev.append(f"{a}: objects/listing changed {v} -> {ca[a]}")
    check("areas", "Every kept area has the same placed objects", "FAIL" if ev else "PASS",
          ev or [f"{len([a for a in oa if a not in removed_nodes])} area(s) identical in object counts"],
          "Per-area counts of creatures, doors, placeables, items, stores, triggers, waypoints, encounters.")

    # 6b placements: every kept placed object is exactly where it was (position, and its whole record apart from logged rewrites)
    def placements(db_):
        return {r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in db_.execute(
            "SELECT node, area, template, tag, x, y FROM objects WHERE is_blueprint=0")}
    op, cp = placements(odb), placements(cdb)
    olabels = o_rep.get("labels") or {}
    lbl = lambda nid: olabels.get(nid, nid)  # noqa: E731
    # areas whose .git (placed objects) you changed in the editor: differences there are deliberate, so WARN not FAIL
    edited_areas = {f.lower()[:-4] for f in changes.get("edited", []) if f.lower().endswith(".git")}
    # tag changes the build logged on placed objects (bulk blueprint changes): (area, object path) -> new tag
    logged_tags = {(os.path.splitext(os.path.basename(c["file"]))[0].lower(), c["field"][:-4]): c["new"]
                   for c in changes.get("field_changes", []) if c["file"].lower().endswith(".git") and
                   str(c.get("field", "")).endswith("/Tag")}
    moved, gone, by_edit = [], [], []
    for nid, (area, tpl, tag, x, y) in op.items():
        if f"area:{area}" in removed_nodes:
            continue
        if nid not in cp:
            (by_edit if (area or "").lower() in edited_areas else gone).append(nid); continue
        carea, ctpl, ctag, cx, cy = cp[nid]
        if ctag != tag and logged_tags.get(((area or "").lower(), nid.split(":", 2)[2])) == ctag:
            tag = ctag                        # a logged bulk tag change (it followed its blueprint)
        if (cx, cy) != (x, y) or ctag != tag or carea != area:
            what = f"{lbl(nid)}: ({x}, {y}) -> ({cx}, {cy})" + (f", tag {tag} -> {ctag}" if ctag != tag else "")
            (by_edit if (area or "").lower() in edited_areas else moved).append(what)
    check("placements", "Every placed object keeps its position, tag and area",
          "FAIL" if (moved or gone) else ("WARN" if by_edit else "PASS"),
          [f"moved: {m}" for m in moved[:10]] + [f"missing: {lbl(g_)}" for g_ in gone[:10]] +
          [f"changed by your edits (deliberate - check it): {m}" for m in by_edit[:10]] or
          [f"{len(op)} placed objects verified (only blueprint links were re-pointed where logged)"],
          "Positions and identity of all placed creatures, doors, placeables, items, stores, triggers, waypoints, encounters compared.")

    # 7 quests - proves the journal (module.jrl) keeps every category and the same entry IDs in the same order
    oq = {q["tag"]: [e["id"] for e in q["entries"]] for q in o_rep["quests"] if q.get("in_journal", True)}
    cq = {q["tag"]: [e["id"] for e in q["entries"]] for q in c_rep["quests"] if q.get("in_journal", True)}
    ev = [f"quest changed/missing: {t}" for t in oq if cq.get(t) != oq[t]]
    check("quests", "Journal quests and entries preserved", "FAIL" if ev else "PASS",
          ev or [f"{len(oq)} quest(s) identical"], "module.jrl categories and entry IDs compared.")

    # 8 conversations - proves every kept conversation has the same entry, reply and start counts (structure, not text)
    oc = {c["name"]: (c["entries"], c["replies"], c["starts"]) for c in o_rep["conversations"]}
    cc = {c["name"]: (c["entries"], c["replies"], c["starts"]) for c in c_rep["conversations"]}
    ev = [f"conversation changed/missing: {nm}" for nm in oc if f"dlg:{nm}" not in removed_nodes and cc.get(nm) != oc[nm]]
    check("conversations", "Conversations preserved node-for-node", "FAIL" if ev else "PASS",
          ev or [f"{len(cc)} conversation(s) identical in structure"], "Entry/reply/start counts compared.")

    # 9 everything that was in use is still present and in use - reachability over each dependency graph
    og = nwn_analysis.Graph(odb)
    cg = nwn_analysis.Graph(cdb)

    def live(g):
        """Module nodes reachable from the module and its own files (palettes excluded: they list every blueprint,
        which is not a real use)."""
        roots = {"module"} | {k for k, v in g.nodes.items() if v["in_module"] and
                              (v["type"] == "2da" or (k.startswith("file:") and not k.endswith(".itp")))}
        return {k for k in g.reachable(roots) if g.nodes.get(k, {}).get("in_module")}
    o_live, c_live = live(og), live(cg)
    # The original's analysis read the builder's override folder; the clean module is re-indexed without it (players
    # don't have it). A resource that only the override provided is "in use" in the original and absent from the clean
    # index, but the build did not remove it - players never had it. Those are reported as a note, not as lost.
    override_only = _override_only_nodes(odb)
    # tag: and var: nodes are names used at run time (object tags, local variables), not files: not counted here
    lost_all = sorted(k for k in o_live - c_live if k not in removed_nodes and not k.startswith(("tag:", "var:")))
    lost = [k for k in lost_all if k not in override_only]
    from_override = [k for k in lost_all if k in override_only]
    note = ([f"not counted: {len(from_override)} resource(s) the original found only in your override folder "
             f"(players don't get them, with or without this build), e.g. "
             + ", ".join(og.label(k) for k in from_override[:5])] if from_override else [])
    check("in_use", "Everything that was in use is still present and reachable", "FAIL" if lost else "PASS",
          ([f"no longer present/reachable: {og.label(k)}" for k in lost] or [f"{len(c_live)} in-use resources verified"])
          + note,
          "Reachability from module.ifo recomputed on both modules" + (" (against the lean haks)" if changes.get("lean_haks") else "") + ".")
    if changes.get("lean_haks"):
        # every 2da row the original used must still be identical with the lean haks in place (in practice: every
        # cell of the copy of each 2da the game loads, compared by (table, row, column))
        o2, c2 = _player_2da_cells(odb), _player_2da_cells(cdb)
        unchanged = sorted(k for k, v in changes["lean_haks"].items() if v.get("unchanged"))
        diff = [k for k in o2 if o2[k] != c2.get(k)]
        check("lean_haks", "Lean haks keep every 2da table identical", "FAIL" if diff else "PASS",
              [f"{k[0]}.2da row {k[1]} {k[2]} changed" for k in diff[:10]] or
              [f"{sum(1 for v in changes['lean_haks'].values() if not v.get('unchanged'))} rebuilt lean hak(s): " +
               (", ".join(f"{k} → {v.get('new_name', k)} {v['bytes_in'] / 1e6:.1f}→{v['bytes_out'] / 1e6:.1f} MB"
                          for k, v in changes["lean_haks"].items() if not v.get("unchanged")) or "none")] +
              ([f"{len(unchanged)} hak(s) left as they are (nothing to drop), checked from their own folder: " +
                ", ".join(unchanged[:20])] if unchanged else []),
              "2da tables compared cell by cell between original haks and lean haks.")

    # 9b nothing kept still points at something removed (catches refs the rewriter could not re-point).
    # Walks the ORIGINAL graph's incoming references of each removed node and reports one only if the clean graph
    # still has the same reference (same source, same kind) and its source is a kept, in-module, in-use resource
    merged_nodes = {d["node"] for d in changes["deleted"] if d["status"] == "merged"}
    rewritten_files = set(changes["rewritten"])
    kept = set(og.nodes) - removed_nodes
    dangling = []
    for nid in removed_nodes:
        for s_, k, v in og.rev.get(nid, []):
            if s_ in removed_nodes or s_ not in kept or s_.startswith(("tag:", "var:")) or k == "tag_provider":
                continue
            if not og.nodes.get(s_, {}).get("in_module"):
                continue
            if nid in merged_nodes and k in nwn_analysis.REWRITABLE_KINDS | {"spawns", "name_ref"}:
                continue  # re-pointed by the builder (verified by the 'modified' and 'references' checks)
            if s_ not in o_live and nid not in merged_nodes:
                continue  # only unused files still point at it
            if not any(src == s_ and kind == k for src, kind, _v in cg.rev.get(nid, [])):
                continue  # the clean module no longer has this reference (re-pointed in your edits)
            dangling.append(f"{og.label(s_)} still refers to removed {og.label(nid)} ({k}{': ' + v if v else ''})")
    check("dangling", "Nothing in use still points at a removed resource", "FAIL" if dangling else "PASS",
          dangling or ["no remaining references to removed resources"],
          "Every reference in the original dependency graph to a removed file was checked.")

    # 10 official compiler - proves every script of the clean module compiles. The original is compiled too (only
    # when the clean compile ran), so failures it already had are WARN and only new ones FAIL. compile_module copies
    # the scripts to a temp folder and runs the compiler there, without a shell, so neither module is written
    comp = nwn_compile.compile_module(clean_folder, compiler, nwn_root, dirs, nwn_user=nwn_user)
    if comp["status"] != "ran":
        check("compile", "All scripts compile with the official compiler",
              "SKIPPED" if comp["status"] == "skipped" else "WARN",
              [("compiler could not run: " if comp["status"] != "skipped" else "") + (comp.get("reason") or "")[:400]],
              "Install nwn_script_comp (neverwinter.nim) and set the NWN install folder on the Modules page.")
    else:
        orig = nwn_compile.compile_module(odb.execute("SELECT value FROM meta WHERE key='module_path'").fetchone()[0],
                                          compiler, nwn_root, dirs, nwn_user=nwn_user)
        o_fail = {k for k, v in orig.get("results", {}).items() if v}
        c_fail = {k: v for k, v in comp["results"].items() if v}
        new_fail = {k: v for k, v in c_fail.items() if k not in o_fail}
        check("compile", "All scripts compile with the official compiler",
              "FAIL" if new_fail else ("WARN" if c_fail else "PASS"),
              [f"NEW failure {k}: {v[0]}" for k, v in new_fail.items()] +
              [f"pre-existing failure {k}: {v[0]}" for k, v in c_fail.items() if k in o_fail] or
              [f"{comp['ok']} script(s) compiled"], f"Compiler: {comp.get('compiler')}")

    statuses = [c["status"] for c in checks]
    # a skipped compile means scripts are unverified: never a clean PASS
    verdict = "FAIL" if "FAIL" in statuses else \
        ("PASS WITH WARNINGS" if ("WARN" in statuses or "SKIPPED" in statuses) else "PASS")
    result = dict(verdict=verdict, checks=checks, when=time.strftime("%Y-%m-%d %H:%M:%S"),
                  original=o_rep["summary"]["module_path"], clean=clean_folder, mod=mod,
                  summary=changes["summary"], git=(changes.get("git") or {}).get("hint"))
    with open(os.path.join(build_dir, "audit.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False)
    write_audit_md(os.path.join(build_dir, "audit.md"), result)
    if verbose:
        print(f"  AUDIT: {verdict}  " + "  ".join(f"{c['id']}={c['status']}" for c in checks) +
              f"  ({time.time() - t0:.1f}s)", flush=True)
    odb.close(); cdb.close()
    return result




def write_audit_md(path, r):
    """Write audit.md (verdict, then one table row per check with up to 6 evidence lines) from the audit result r."""
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "SKIPPED": "⏭️"}
    L = [f"# Audit: {r['verdict']}", "", f"- Original: `{r['original']}`", f"- Clean: `{r['clean']}`",
         f"- .mod: `{r['mod']}`", f"- Run: {r['when']}", ""]
    L += ["| Check | Result | Evidence |", "|---|---|---|"]
    for c in r["checks"]:
        L.append(f"| {c['title']} | {icon.get(c['status'], '')} {c['status']} | {'<br>'.join(c['evidence'][:6])} |")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")




def main(argv=None):
    """Command line: re-run the deterministic audit of an existing build.

    python nwn_audit.py <original analysis folder> <build folder> <clean module name> [--compiler X] [--nwn-root X]
    Returns the exit code: 0 the audit ran (its verdict is in audit.md / audit.json), 2 an input is missing - the
    original analysis (no index.sqlite), the build's changes.json or the clean module folder - with one line saying
    which. Checked first because sqlite3 would otherwise create an empty index.sqlite in a mistyped folder."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", help="the ORIGINAL module's analysis folder (nwn_workspace/<module>)")
    ap.add_argument("build", help="the build folder (holds changes.json, <name>/ and <name>.mod)")
    ap.add_argument("name", help="the clean module's name (e.g. <module>_clean)")
    ap.add_argument("--compiler")
    ap.add_argument("--nwn-root")
    ap.add_argument("--nwn-user")
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.original, "index.sqlite")):
        print(f"No analysis found in {a.original} (it has no index.sqlite) - give the original module's analysis "
              "folder, e.g. nwn_workspace/<module>")
        return 2
    if not os.path.isfile(os.path.join(a.build, "changes.json")):
        print(f"No build found in {a.build} (it has no changes.json) - give the folder Build & audit wrote, e.g. "
              "nwn_workspace/<module>/build")
        return 2
    if not os.path.isdir(os.path.join(a.build, a.name)):
        print(f"Not found: {os.path.join(a.build, a.name)} - check the clean module's name")
        return 2
    audit(a.original, a.build, a.name, a.compiler, a.nwn_root, a.nwn_user)
    return 0


if __name__ == "__main__":
    sys.exit(main())
