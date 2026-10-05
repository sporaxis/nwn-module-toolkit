"""
nwn_diff.py - what changed between two copies of a module, and what it affects.

    python nwn_diff.py <older .mod | folder | snapshot.json.gz> <newer .mod | folder> [--analysis NAME]
    python nwn_diff.py --snapshot <module .mod | folder> [--label text]

Compares every resource by content (SHA-1): added, removed, changed. For changed resources it shows WHAT changed:
  - GFF files (areas, blueprints, placed objects, conversations, module.ifo ...): every field that differs
    (path, old value, new value)
  - scripts, 2das and other text: a line-by-line diff
When an analysis of the newer module exists, each change carries its impact level (None ... Critical) and removed
resources show whether something still uses them (a removal that breaks the module).

Snapshots: a small record of a module (the hash of every resource plus the text of every script and 2da) kept in
nwn_workspace/_snapshots/. Compare against a snapshot after you have archived or deleted the old .mod; field-level
detail for GFF files needs the .mod itself.
Nothing here changes any module.

Reads: the two modules (read-only), a snapshot file, and optionally the analysis index/report (read-only, via
nwn_facts). Writes: only take_snapshot writes, a new <module>__<time>.json.gz in nwn_workspace/_snapshots/ (via a
.tmp file and a rename, so a half-written snapshot is never left under the real name).

Entry points: compare(older, newer, ...) -> dict(summary, changes); take_snapshot(); list_snapshots(); main() (CLI).

Limits
------
Files are matched by name (lower case) at the top level of a folder; sub-folders are not read. In a .mod with two
entries of the same name only the first is compared. SHA-1 is used only to spot changed content, not for security.
Diff lists are capped (MAX_FIELDS fields, MAX_LINES diff lines per file). Impact levels and "used by" counts come
from the analysis you name, which may be of either copy (summary.analysis_is_of says which, or "another copy").
"""
from __future__ import annotations

import datetime as _dt
import difflib
import gzip
import hashlib
import json
import os
import re
import sys

import nwnlib as n

HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_EXTS = {"nss", "2da", "txt", "set", "txi", "ini", "lua", "sql", "jui", "tml", "ltr"}   # diffed line by line
MAX_FIELDS = 300        # most changed GFF fields listed per file (fields_changed still counts them all)
MAX_LINES = 400         # most unified-diff lines kept per text file (diff_cut=True when cut)
LEVELS = ["None", "Low", "Medium", "High", "Critical"]      # impact levels, lowest first (used to sort changes)


def _sha1(b):
    """Content fingerprint used to tell changed files apart (not a security use)."""
    return hashlib.sha1(b).hexdigest()


class Source:
    """A module to compare: a .mod/.erf file, an unpacked module folder, or a snapshot.

    Read-only. kind is "erf", "folder" or "snapshot". A snapshot has hashes and text but no file bytes, so data()
    returns None for it. Call close() to release an open .mod."""

    def __init__(self, path):
        """path: a .mod/.erf, a module folder, or a *.json.gz snapshot. Raises ValueError when it does not exist."""
        self.path = os.path.abspath(path)
        self.kind = None
        self._erf = None
        self._index = {}            # filename -> ErfEntry | path
        self.snapshot = None
        if os.path.isdir(self.path):
            self.kind = "folder"
            for e in os.scandir(self.path):
                if e.is_file() and "." in e.name:
                    self._index[e.name.lower()] = e.path
        elif self.path.lower().endswith(".json.gz"):
            self.kind = "snapshot"
            with gzip.open(self.path, "rt", encoding="utf-8") as fh:
                self.snapshot = json.load(fh)
        elif os.path.isfile(self.path):
            self.kind = "erf"
            self._erf = n.Erf(self.path)
            for e in self._erf.entries:
                self._index.setdefault(e.filename.lower(), e)     # first copy is the one the game reads
        else:
            raise ValueError(f"not found: {path}")

    @property
    def label(self):
        """Short name to show: the file/folder name, or "snapshot of <module> (<time>)"."""
        if self.kind == "snapshot":
            return f"snapshot of {os.path.basename(self.snapshot.get('module_path', '?'))} ({self.snapshot.get('taken_at')})"
        return os.path.basename(self.path)

    def data(self, fn):
        """The bytes of resource `fn` (lower-case "name.ext"), or None when absent or when this is a snapshot."""
        if self.kind == "snapshot":
            return None
        e = self._index.get(fn)
        if e is None:
            return None
        if self.kind == "erf":
            return self._erf.read(e)
        with open(e, "rb") as fh:
            return fh.read()

    def manifest(self, progress=None):
        """{filename: (size, sha1)} for every resource. Reads every file once, so this is the slow step for a big .mod.
        progress(percent, message), when given, is called every 500 files (percent 0-40 of the whole comparison)."""
        if self.kind == "snapshot":
            return {k: tuple(v) for k, v in self.snapshot["resources"].items()}
        out = {}
        items = list(self._index)
        for i, fn in enumerate(items):
            b = self.data(fn)
            out[fn] = (len(b), _sha1(b))
            if progress and i % 500 == 0:
                progress(i / max(1, len(items)) * 40, f"reading {self.label}: {i} of {len(items)}")
        return out

    def text(self, fn):
        """Resource `fn` as text (Windows-1252, lossless), or None. A snapshot holds text for TEXT_EXTS files only."""
        if self.kind == "snapshot":
            return (self.snapshot.get("text") or {}).get(fn)
        b = self.data(fn)
        return None if b is None else n.decode_text(b)

    def close(self):
        if self._erf:
            self._erf.close()


def flatten_gff(data):
    """A GFF file as {field path: value as text}, e.g. {"ItemList[0]/InventoryRes": "nw_it_gold001"}.
    Raises nwnlib.GffError for a damaged file."""
    root = n.read_gff(data)
    return {p: n.leaf_to_text(t, v) for p, _lab, t, v, _s in n.iter_gff_leaves(root)}


def is_gff(data):
    """True when `data` looks like a GFF file. A GFF header is a 4-character file type ("UTI ", "DLG " ...) followed
    by a 4-character version at bytes 4-7 (BioWare GFF format documentation); NWN writes "V3.2"."""
    return data is not None and len(data) > 8 and data[4:8] in (b"V3.2", b"V3.3")


def compare(older, newer, analysis=None, workspace=None, progress=None):
    """Everything that differs between two copies of a module. Read-only.

    older / newer: paths (.mod, folder, or a snapshot - normally only `older` is a snapshot).
    analysis: an analysis name in `workspace` (optional) - adds node, label, impact level and, for removed files,
    used_by (how many graph edges still point at it). An unknown analysis name is ignored, not an error.
    progress(percent, message): optional callback for the dashboard's progress bar.
    Returns dict(summary, changes). Each change: file, ext, change (added/removed/changed), sizes, and for a changed
    text file a unified diff (+ lines_added/lines_removed), for a changed GFF the differing fields (path, old, new).
    Changes are sorted by impact (Critical first), then removed / changed / added, then name."""
    a, b = Source(older), Source(newer)
    facts = None            # closed in the finally below, so a failure half-way never leaves the index open
    try:
        ma = a.manifest(progress)
        mb = b.manifest(lambda p, m: progress(40 + p / 2, m) if progress else None)
        if analysis:
            import nwn_facts
            try:
                facts = nwn_facts.Facts(analysis, workspace or nwn_facts.WORKSPACE)
            except ValueError:
                facts = None
        impact = (facts.report().get("impact") or {}) if facts else {}
        analysis_is_of = None
        if facts:
            # which copy was analysed: same full path, else same file name (the .mod may have moved since)
            mp = os.path.normcase(os.path.abspath(facts.summary().get("module_path") or ""))
            analysis_is_of = "newer" if mp == os.path.normcase(b.path) else "older" if mp == os.path.normcase(a.path) else \
                ("newer" if os.path.basename(mp) == os.path.basename(os.path.normcase(b.path)) else "another copy")
        labels = (facts.report().get("labels") or {}) if facts else {}
        node_of = {}
        if facts:
            for rel, node in facts.db.execute("SELECT f.relpath, f.node FROM files f JOIN sources s ON s.id=f.source_id "
                                              "WHERE s.kind='module'"):
                node_of[rel.lower()] = node
        changes = []
        names = sorted(set(ma) | set(mb))
        for i, fn in enumerate(names):
            if progress and i % 500 == 0:
                progress(60 + i / max(1, len(names)) * 38, f"comparing {i} of {len(names)}")
            ina, inb = fn in ma, fn in mb
            if ina and inb and ma[fn][1] == mb[fn][1]:
                continue
            ext = fn.rsplit(".", 1)[-1]
            ch = dict(file=fn, ext=ext, change="added" if not ina else "removed" if not inb else "changed",
                      size_old=ma[fn][0] if ina else None, size_new=mb[fn][0] if inb else None)
            node = node_of.get(fn)
            if not node and facts:
                # a file not in the analysed copy (e.g. added since): try the node ids its name would have.
                # fn[:-4] drops a 3-letter extension (".nss", ".dlg", ".are").
                r = facts.db.execute("SELECT node FROM nodes WHERE node IN (?,?,?,?)",
                                     (f"bp:{fn}", f"script:{fn[:-4]}", f"dlg:{fn[:-4]}", f"area:{fn[:-4]}")).fetchone()
                node = r[0] if r else None
            if node:
                ch["node"] = node
                ch["label"] = labels.get(node, node)
                im = impact.get(node) or {}
                ch["impact"] = im.get("level") or "None"
                if ch["change"] == "removed":
                    # still referenced in the analysed module: removing it would leave a broken reference
                    used = facts.db.execute("SELECT count(*) FROM edges WHERE dst=?", (node,)).fetchone()[0]
                    ch["used_by"] = used
            if ch["change"] == "changed":
                if ext in TEXT_EXTS:
                    ta, tb = a.text(fn), b.text(fn)
                    if ta is not None and tb is not None:
                        # line endings normalised first, so CRLF vs LF alone does not show every line as changed
                        diff = list(difflib.unified_diff(ta.replace("\r\n", "\n").split("\n"), tb.replace("\r\n", "\n").split("\n"),
                                                         "old", "new", n=2, lineterm=""))
                        ch["lines_added"] = sum(1 for x in diff if x.startswith("+") and not x.startswith("+++"))
                        ch["lines_removed"] = sum(1 for x in diff if x.startswith("-") and not x.startswith("---"))
                        ch["diff"] = diff[:MAX_LINES]
                        ch["diff_cut"] = len(diff) > MAX_LINES
                    elif ta is None or tb is None:
                        # only a snapshot (either side) can lack the text of a file that is in its manifest
                        ch["note"] = "the snapshot has no text for this file"
                else:
                    da, db_ = a.data(fn), b.data(fn)
                    if is_gff(da) and is_gff(db_):
                        try:
                            fa, fb = flatten_gff(da), flatten_gff(db_)
                            fields = []
                            for p in sorted(set(fa) | set(fb)):
                                if fa.get(p) != fb.get(p):
                                    fields.append(dict(path=p, old=fa.get(p), new=fb.get(p)))
                            ch["fields_changed"] = len(fields)
                            ch["fields"] = fields[:MAX_FIELDS]
                        except Exception as ex:  # noqa - a broken GFF must not stop the comparison
                            ch["note"] = f"could not read as GFF: {ex}"
                    elif a.kind == "snapshot" or b.kind == "snapshot":
                        # whichever side is the snapshot, there are no bytes to compare field by field
                        side = "older" if a.kind == "snapshot" else "newer"
                        ch["note"] = f"field-level detail needs the {side} .mod (the snapshot keeps only a fingerprint)"
            changes.append(ch)
        changes.sort(key=lambda c: (-LEVELS.index(c.get("impact", "None")), {"removed": 0, "changed": 1, "added": 2}[c["change"]],
                                    c["file"]))
        by = {}
        for c in changes:
            by.setdefault(c["change"], 0)
            by[c["change"]] += 1
        by_ext = {}
        for c in changes:
            by_ext[c["ext"]] = by_ext.get(c["ext"], 0) + 1
        summary = dict(older=a.label, newer=b.label, older_path=a.path, newer_path=b.path,
                       resources_old=len(ma), resources_new=len(mb), added=by.get("added", 0),
                       removed=by.get("removed", 0), changed=by.get("changed", 0), by_type=by_ext,
                       bytes_old=sum(v[0] for v in ma.values()), bytes_new=sum(v[0] for v in mb.values()),
                       analysis=analysis if facts else None,
                       removed_used=sum(1 for c in changes if c.get("used_by")),
                       analysis_is_of=analysis_is_of,
                       compared_at=_dt.datetime.now().isoformat(timespec="seconds"))
        return dict(summary=summary, changes=changes)
    finally:
        if facts:
            facts.close()
        a.close()
        b.close()


def snapshot_dir(workspace):
    """Folder that holds the snapshots: <workspace>/_snapshots (not created here)."""
    return os.path.join(workspace, "_snapshots")


def take_snapshot(module, workspace, label="", progress=None):
    """Record a module's state for later comparison: every resource's size + SHA-1, and the text of TEXT_EXTS files.

    module: a .mod or module folder (read-only); label: free text kept in the snapshot.
    Writes one gzip JSON file to <workspace>/_snapshots/ (the only write in this module), named from the module
    name (unsafe characters replaced by _) and the time. Returns dict(path, resources, bytes, label, taken_at).
    GFF files are kept as fingerprints only, so a later comparison can say THAT they changed but not which fields."""
    s = Source(module)
    if s.kind == "snapshot":
        raise ValueError("that is already a snapshot")
    try:
        man = s.manifest(progress)
        text = {}
        for fn in man:
            if fn.rsplit(".", 1)[-1] in TEXT_EXTS:
                t = s.text(fn)
                if t is not None:
                    text[fn] = t
        name = re.sub(r"[^A-Za-z0-9_\-]+", "_", os.path.splitext(os.path.basename(s.path))[0])[:60]
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        os.makedirs(snapshot_dir(workspace), exist_ok=True)
        path = os.path.join(snapshot_dir(workspace), f"{name}__{stamp}.json.gz")
        data = dict(module_path=s.path, taken_at=_dt.datetime.now().isoformat(timespec="seconds"), label=label,
                    resources={k: list(v) for k, v in man.items()}, text=text)
        with gzip.open(path + ".tmp", "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(path + ".tmp", path)         # atomic: the snapshot appears complete or not at all
        return dict(path=path, resources=len(man), bytes=os.path.getsize(path), label=label, taken_at=data["taken_at"])
    finally:
        s.close()


def list_snapshots(workspace):
    """Every snapshot, oldest name first: [dict(path, name, module, taken, bytes)]. module and taken are read from
    the file name ("<module>__<time>.json.gz"). Read-only."""
    d = snapshot_dir(workspace)
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json.gz"):
                p = os.path.join(d, fn)
                mod, _, stamp = fn[:-8].rpartition("__")
                out.append(dict(path=p, name=fn, module=mod, taken=stamp, bytes=os.path.getsize(p)))
    return out


def main(argv=None):
    """Command line (see the module docstring). Prints a summary line and one line per change, or JSON with --json.
    Returns the exit code: 0 done, 2 when two paths were not given or one does not exist (one line says which)."""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("older", nargs="?", help="the older copy: a .mod, an unpacked module folder or a snapshot .json.gz")
    ap.add_argument("newer", nargs="?", help="the newer copy: a .mod or an unpacked module folder")
    ap.add_argument("--analysis", help="analysis name in the workspace: adds impact levels and 'still used by'")
    ap.add_argument("--snapshot", metavar="MODULE", help="save a snapshot of this module instead of comparing")
    ap.add_argument("--label", default="", help="a note stored with the snapshot")
    ap.add_argument("--workspace", default=os.environ.get("NWN_WORKSPACE", os.path.join(HERE, "nwn_workspace")),
                    help="the toolkit's workspace folder (default: nwn_workspace next to this file)")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    a = ap.parse_args(argv)
    gone = [p for p in ([a.snapshot] if a.snapshot else [a.older, a.newer]) if p and not os.path.exists(p)]
    if gone:
        print(f"Not found: {gone[0]} - check the path (put it in quotes if it contains spaces)")
        return 2
    if a.snapshot:
        print(json.dumps(take_snapshot(a.snapshot, a.workspace, a.label), indent=1))
        return 0
    if not (a.older and a.newer):
        ap.print_help()
        return 2
    r = compare(a.older, a.newer, a.analysis, a.workspace)
    if a.json:
        print(json.dumps(r, indent=1, ensure_ascii=False))
        return 0
    s = r["summary"]
    print(f"{s['older']} -> {s['newer']}: {s['added']} added, {s['removed']} removed, {s['changed']} changed")
    for c in r["changes"]:
        extra = f" fields:{c.get('fields_changed')}" if c.get("fields_changed") is not None else \
            (f" +{c.get('lines_added')}/-{c.get('lines_removed')} lines" if c.get("diff") is not None else "")
        warn = f"  used by {c['used_by']} in the analysis ({s['analysis_is_of']} copy)" if c.get("used_by") else ""
        print(f"{c['change']:8s} {c.get('impact', ''):8s} {c['file']}{extra}{warn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
