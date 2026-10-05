"""
nwn_refactor.py - find anything anywhere in a module, then rename / replace it everywhere, safely.

FIND searches, in the analysis index:
  scripts        every line of every script's source
  conversations  every field of every .dlg (lines, speakers, condition/action script slots, quests)
  objects        every field of every blueprint, placed object, area, store, encounter, module.ifo ... (tags,
                 resrefs, names, variables, inventories, event scripts)
  2da            every cell of every 2da
  file names     resource names
with filters (kind, whole value / whole word / part, capitals, module only or haks too). Each hit says exactly where
it is (script line, GFF field path, 2da row/column) and which object it belongs to.

REPLACE (rename) takes the hits you ticked, shows every change first (before -> after, and why a hit can't be
changed: in a hak, a compiled-only script, a name too long for a resref ...), then writes the changed files to the
analysis's edits (never the original module). Before writing, the current edits of those files are saved to
edits/.history/<time>/ so Undo can put them back. Renaming a file (resref) adds the renamed copy as a new file and
marks the old one removed; the clean build applies both.
Warns when a name built at run time ("spawn_" + n) could produce the old name - those uses can't be found or changed.

Reads: the analysis index (index.sqlite, opened read-only), the module's original files (read-only, for the bytes
of a file being changed) and the edits overlay. Writes: only <analysis>/edits/ (see nwn_edit.py for the overlay
layout). Never the module, a hak or the game.

Entry points: find() -> plan() (a dry run, writes nothing) -> apply() -> undo() / history(). Also used by the
dashboard: module_bytes() and read_folder_file() (a module file's original bytes, never from outside the module).

The Undo record (the "manifest") - edits/.history/<id>/
    record.json   id, when, text, new, mode, changes (count), status "writing" -> "done", "undone" (time) once undone,
                  files      [{file, from_file}] written (from_file = the old name for a rename)
                  created    overlay files that did not exist before this apply (Undo deletes them)
                  deleted_markers   old names that got a <name>.deleted marker (Undo removes the marker)
                  written_sha       {file: SHA-256 of what this apply wrote} - used to detect later edits
    <file>        a copy of each overlay file this apply overwrote or removed (Undo puts it back)
edits/.history/log.json lists every apply, oldest first (the dashboard's history list).
The record is written BEFORE any overlay file, so an apply that fails half-way can still be undone.

Undo is refused (with the reason) when:
  * a later rename/replace has not been undone - undo newest first: this undo restores copies taken before the
    later change, so going out of order would wipe out what the later change did;
  * a file this apply wrote has changed since (its SHA-256 no longer matches) - undoing would silently throw that
    later work away;
  * it was already undone, or the id is not a history id (the id is checked against a strict pattern before it is
    used in a path).

Limits: GFF files you edited are searched as they were analysed (re-analyse to search your edited text); script
edits are searched as they are now. Text that lives in the talk table (.tlk) can't be changed here. Values already
saved under an old variable name (on characters, in databases) are not migrated.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
import os
import re
import shutil

import nwnlib as n
import nwn_edit

# GFF extension -> what to call the hit's file in the results (other ut* files are "blueprint")
GFF_KIND = {"dlg": "conversation", "git": "placed object", "are": "area", "ifo": "module", "jrl": "journal",
            "fac": "factions", "gic": "area comments", "itp": "palette"}
KINDS = ("script", "conversation", "object", "2da", "filename")
LIMIT = 5000                   # most hits one search returns (the result says truncated=True past this)
TAG_MAX = 32                   # longest tag allowed (the toolset's tag limit)
_WRITE = threading.Lock()      # one rename/replace/undo at a time per process


def _open(analysis_dir):
    """The analysis index, read-only (writes raise). check_same_thread=False: the dashboard calls from its threads."""
    p = os.path.join(analysis_dir, "index.sqlite")
    return n.sqlite_ro(p, check_same_thread=False)


def matcher(text, mode="contains", case=False):
    """A compiled regex for the search: contains | word | exact (the whole value).

    text is always taken literally (re.escape), never as a pattern. 'word' means not touching a letter, digit or _
    on either side: "orc" matches "orc" and "orc chief" but not "orcish" or "q_orc". case=False ignores capitals.
    Raises ValueError for empty text."""
    if not text:
        raise ValueError("type something to search for")
    body = re.escape(text)
    if mode == "word":
        body = r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])"
    elif mode == "exact":
        body = "^" + body + "$"
    return re.compile(body, 0 if case else re.I)


def script_matcher(text, mode="contains", case=False):
    """In scripts, 'whole value' means a whole string literal: "text" (a tag, resref or variable name in quotes)."""
    if mode == "exact":
        return re.compile('"' + re.escape(text) + '"', 0 if case else re.I)
    return matcher(text, mode, case)


def _kind_of_ext(ext):
    if ext == "dlg":
        return "conversation"
    return "object"


def _owner_node(relpath, ext, file_node, path):
    """Graph node a GFF hit belongs to. In an area's .git (placed objects) the hit belongs to one placed instance,
    named by the path without its last part, e.g. "Creature List[3]/Tag" in town.git -> "inst:town:Creature List[3]".
    Hits in any other file belong to the file's own node."""
    if ext == "git" and "[" in path:
        head = path.rsplit("/", 1)[0] if "/" in path else path
        return f"inst:{relpath[:-4]}:{head}"
    return file_node


def find(analysis_dir, text, mode="contains", case=False, kinds=KINDS, scope="module", limit=LIMIT):
    """Search the analysed module for `text`. Read-only.

    mode: contains | word | exact (see matcher; in scripts 'exact' means a whole "string literal").
    kinds: any of KINDS. scope: "module" (the module's own files) or "all" (haks and other sources too).
    Returns dict(text, mode, case, scope, hits, total, truncated, by_kind, files, runtime_risk, notes).
    Each hit: id (stable key used by plan/apply), kind, file, where (script line / GFF field path / 2da row+column),
    value, node + owner (what it belongs to), source (module/hak...), and edited/in_edit flags.
    Hit id formats: "filename|<file>|<source>", "script|<file>|<line>|<source>", "gff|<file>|<field path>|<source>",
    "2da|<file>|<row>|<column>|<source>" - apply() reads the line / row / column back out of the id.
    Scripts you edited are searched as they are now (the overlay); edited GFF files as they were analysed."""
    rx = matcher(text, mode, case)
    srx = script_matcher(text, mode, case)
    kinds = set(kinds or KINDS)
    db = _open(analysis_dir)
    hits = []
    src_kind = dict(db.execute("SELECT id, kind FROM sources"))
    src_path = dict(db.execute("SELECT id, path FROM sources"))
    ok_src = {sid for sid, k in src_kind.items() if scope == "all" or k == "module"}
    # SQL LIKE pre-filter (fast, case-insensitive for ASCII), then the exact regex in Python. _ and % are LIKE
    # wildcards, so they are escaped to match literally.
    like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    truncated = False

    def add(h):
        """Keep a hit; False (and truncated=True) once `limit` hits are held, so the callers stop."""
        nonlocal truncated
        if len(hits) >= limit:
            truncated = True
            return False
        hits.append(h)
        return True
    try:
        if "filename" in kinds:
            for rel, ext, sid, node in db.execute("SELECT relpath, ext, source_id, node FROM files WHERE resref LIKE ? ESCAPE '\\'",
                                                  (like,)):
                if sid in ok_src and rx.search(rel.rsplit(".", 1)[0]):
                    if not add(dict(id=f"filename|{rel}|{src_kind[sid]}", kind="filename", file=rel, where="file name",
                                    value=rel.rsplit(".", 1)[0], node=node, source=src_kind[sid],
                                    source_path=src_path[sid], field_type="resref")):
                        break
        ed_dir = os.path.join(analysis_dir, "edits")
        ed_names = set(os.listdir(ed_dir)) if os.path.isdir(ed_dir) else set()
        # your edited and new scripts are searched as they are NOW, not as analysed
        ed_scripts = {fn for fn in ed_names if fn.endswith(".nss") and os.path.isfile(os.path.join(ed_dir, fn))}
        removed = {fn[:-8] for fn in ed_names if fn.endswith(".deleted")}    # "x.nss.deleted" -> "x.nss"
        mod_path = next((src_path[i] for i, k in src_kind.items() if k == "module"), "")

        def script_hits(nm, source, skind, spath, from_edit=False):
            for i, line in enumerate((source or "").replace("\r\n", "\n").split("\n"), 1):
                if srx.search(line):
                    if not add(dict(id=f"script|{nm}.nss|{i}|{skind}", kind="script", file=f"{nm}.nss",
                                    where=f"line {i}", line=i, value=line.strip(), node=f"script:{nm}",
                                    source=skind, source_path=spath, **({"edited": True, "in_edit": True} if from_edit else {}))):
                        return False
            return True
        if "script" in kinds:
            for nm, source, sid in db.execute("SELECT s.name, s.source, f.source_id FROM scripts s JOIN files f ON f.id=s.file_id "
                                              "WHERE s.source LIKE ? ESCAPE '\\'", (like,)):
                # skip a module script that has an edit (searched from the overlay below) or a .deleted marker
                if sid not in ok_src or (src_kind[sid] == "module" and (f"{nm}.nss" in ed_scripts or f"{nm}.nss" in removed)):
                    continue
                if not script_hits(nm, source, src_kind[sid], src_path[sid]):
                    break
            for fn in sorted(ed_scripts):
                if truncated:
                    break
                with open(os.path.join(ed_dir, fn), "rb") as fh:
                    script_hits(fn[:-4], n.decode_text(fh.read()), "module", mod_path, from_edit=True)
        gff_kinds = {"conversation", "object"} & kinds
        if gff_kinds and not truncated:
            for rel, ext, fnode, sid, path, label, typ, val in db.execute(
                    "SELECT f.relpath, f.ext, f.node, f.source_id, fi.path, fi.label, fi.type, fi.value FROM fields fi "
                    "JOIN files f ON f.id=fi.file_id WHERE fi.value LIKE ? ESCAPE '\\'", (like,)):
                if sid not in ok_src or _kind_of_ext(ext) not in gff_kinds or not rx.search(val or ""):
                    continue
                if not add(dict(id=f"gff|{rel}|{path}|{src_kind[sid]}", kind=_kind_of_ext(ext), file=rel, where=path,
                                label=label, field_type=typ, value=(val or ""),
                                node=_owner_node(rel, ext, fnode, path), file_node=fnode, source=src_kind[sid],
                                source_path=src_path[sid], what=GFF_KIND.get(ext, "blueprint" if ext.startswith("ut") else ext))):
                    break
        if "2da" in kinds and not truncated:
            for name, row, col, val, rel, sid in db.execute(
                    "SELECT t.name, t.row, t.col, t.value, f.relpath, f.source_id FROM twoda t JOIN files f ON f.id=t.file_id "
                    "WHERE t.value LIKE ? ESCAPE '\\'", (like,)):
                if sid in ok_src and rx.search(val or ""):
                    if not add(dict(id=f"2da|{rel}|{row}|{col}|{src_kind[sid]}", kind="2da", file=rel, where=f"row {row}, {col}",
                                    row=row, col=col, value=val, node=f"2da:{name}", source=src_kind[sid],
                                    source_path=src_path[sid])):
                        break
        # labels for owners (one query per distinct owner, not per hit)
        owners = {h["node"] for h in hits if h.get("node")}
        labels = {}
        for nd in owners:
            r = db.execute("SELECT label FROM nodes WHERE node=?", (nd,)).fetchone()
            labels[nd] = r[0] if r and r[0] else nd
        edited = set(os.listdir(os.path.join(analysis_dir, "edits"))) if os.path.isdir(os.path.join(analysis_dir, "edits")) else set()
        for h in hits:
            h["owner"] = labels.get(h.get("node"), h.get("node"))
            if h["file"].lower() in edited:
                h["edited"] = True      # found in the analysed original; your edited copy may differ
        runtime = runtime_risk(db, text)
    finally:
        db.close()
    by_kind = {}
    for h in hits:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
    notes = []
    ed_gff = sorted(fn for fn in ed_names if "." in fn and fn.rsplit(".", 1)[1] in n.GFF_EXTENSIONS)
    if ed_gff:
        notes.append(f"{len(ed_gff)} object/conversation file(s) you edited were searched as they were analysed "
                     f"({', '.join(ed_gff[:4])}{' …' if len(ed_gff) > 4 else ''}); text you added there is not found - "
                     "re-analyse to include it")
    return dict(text=text, mode=mode, case=case, scope=scope, hits=hits, total=len(hits), truncated=truncated,
                by_kind=by_kind, files=len({h["file"] for h in hits}), runtime_risk=runtime, notes=notes)


# A name built from an object's tag in a script: a string literal joined to GetTag(...), on either side.
# Matches  "HATE:" + GetTag(oNPC)   (group 1 = "HATE:")  and  GetTag(oItem) + "_used"   (group 2 = "_used").
TAG_BUILT_RE = re.compile(r'"([^"\n]*)"\s*\+\s*GetTag\s*\(|GetTag\s*\([^)]*\)\s*\+\s*"([^"\n]*)"')


def runtime_risk(db, text):
    """Names built at run time whose pieces could produce `text` ("spawn_" + n could be spawn_orc), and names built
    from an object's TAG ("HATE:" + GetTag(o)) - renaming a tag silently changes those names.

    db: the analysis index (sqlite3.Connection). Returns a list of dict(script, line, piece, func[, tag_built]),
    at most 20 of each kind. Read-only. A warning list, not proof: a listed script may never build that name."""
    out = []
    low = text.lower()
    # concat_literals: string pieces the indexer found being used to build names at run time (ExecuteScript("x_" + n),
    # and prefix/suffix values in toolset variables)
    for script, line, func, lit in db.execute("SELECT script, line, func, literal FROM concat_literals"):
        piece = (lit or "").lower()
        # a piece of 2+ characters that starts or ends the searched name could build it; 1 character is too common
        if len(piece) >= 2 and (low.startswith(piece) or low.endswith(piece)) and piece != low:
            out.append(dict(script=script, line=line, piece=lit, func=func))
            if len(out) >= 20:
                break
    if db.execute("SELECT 1 FROM objects WHERE tag=? LIMIT 1", (text,)).fetchone():
        # only when the text is an existing tag: then every tag-built name in any script is listed (the script can't
        # tell which object's tag it will read)
        n_ = 0
        for nm, src in db.execute("SELECT name, source FROM scripts WHERE source LIKE '%GetTag%'"):
            for i, line in enumerate((src or "").split("\n"), 1):
                m = TAG_BUILT_RE.search(line)
                if m and not line.lstrip().startswith("//"):
                    out.append(dict(script=nm, line=i, piece=(m.group(1) or "") + "<tag>" + (m.group(2) or ""),
                                    func="name built from a tag", tag_built=True))
                    n_ += 1
                    if n_ >= 20:
                        break
            if n_ >= 20:
                break
    return out


# A script line that reads/writes a variable or a database value, e.g. SetLocalInt(, GetCampaignString(,
# GetPersistentInt( - used to warn that saved values keep their old name after a rename.
VAR_CALL_RE = re.compile(r"\b(Set|Get|Delete)(Local|Campaign|PLocal|Persistent)\w*\s*\(")


def _match_case(src, new):
    """Keep the capitals of what was found: MARSHAL -> CAPTAIN, Marshal -> Captain, marshal -> captain."""
    if src.isupper():
        return new.upper()
    if src[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


# ------------------------------------------------------------------ replace
def _new_value(old_value, rx, new, mode):
    """The value after replacing (exact: the whole value becomes `new`). The lambda stops re.sub reading \\1 or \\g<>
    in `new` as a back-reference - the replacement is always literal."""
    if mode == "exact":
        return new
    return rx.sub(lambda _m: new, old_value)


def _is_resref(h):
    """True when the hit is a resource name: a file name, a ResRef-typed GFF field, or a field that holds one."""
    return h["kind"] == "filename" or h.get("field_type") == "resref" or \
        h.get("label") in ("TemplateResRef", "ResRef", "InventoryRes")


def _validate(h, after, resref_rule=False, tag_rule=False, new=None):
    """Why a new value can't be stored (None when fine). A rename is all-or-nothing: when any hit is a resref (or a
    tag), the new name must suit that kind everywhere, or scripts and objects would disagree."""
    if _is_resref(h) or resref_rule:
        v = after if _is_resref(h) else new
        if not v:
            return "a resource name can't be empty"
        if not nwn_edit.RESREF_RE.fullmatch(v.lower()):
            return f"'{v}' can't be a resource name (letters, digits, _ only; 16 characters at most)"
    if h.get("label") == "Tag" or tag_rule:
        v = after if h.get("label") == "Tag" else new
        if not v and h.get("label") == "Tag":
            return "an empty tag can't be looked up - pick a new tag"
        if v and len(v) > TAG_MAX:
            return f"tags are {TAG_MAX} characters at most"
    if h["kind"] == "script" and "\n" in after:
        return "the replacement can't contain a line break"
    return None


def _case_differs(h, text, rx):
    """For names (scripts, resrefs, tags, variable names) capitals matter in NWN: 'nDone' and 'NDone' are two variables."""
    if (h.get("field_type") or "").lower() == "cexolocstring":
        return None
    for m in rx.finditer(h["value"] or ""):
        if m.group(0) != text and m.group(0).lower() == text.lower():
            return m.group(0)
    return None


def plan(analysis_dir, text, new, hit_ids, mode="contains", case=False, kinds=KINDS):
    """Every change the replace would make, for review. Nothing is written. Everything Apply checks is checked here,
    so what the preview calls OK is what Apply does.

    text / new: what to find and what to put instead; hit_ids: the ids of the hits the user ticked (from find()).
    Returns dict(text, new, mode, case, changes, ok, refused, runtime_risk, notes, files). Each change has
    before/after and ok=True, or ok=False with `why` (in a hak, capitals differ, not a valid resref or tag, the
    blueprint's own TemplateResRef, the target name already exists, the field changed since the search ...).
    Searches with scope="all" so ticked hits in haks are reported as refused rather than silently dropped.
    Unless case=True, capitals are kept as found (MARSHAL -> CAPTAIN, see _match_case)."""
    res = find(analysis_dir, text, mode, case, kinds, scope="all")
    want = set(hit_ids)
    rx = matcher(text, mode, case)
    chosen = [h for h in res["hits"] if h["id"] in want]
    # an exact rename that touches a resref (or a tag) anywhere must give a name valid as one everywhere (see _validate)
    resref_rule = mode == "exact" and any(_is_resref(h) for h in chosen)
    tag_rule = mode == "exact" and any(h.get("label") == "Tag" for h in chosen)
    renaming = {h["file"].lower() for h in chosen if h["kind"] == "filename"}
    changes = []
    for h in res["hits"]:
        if h["id"] not in want:
            continue
        c = dict(id=h["id"], kind=h["kind"], file=h["file"], where=h["where"], owner=h.get("owner"), node=h.get("node"),
                 before=h["value"])
        twin = None if case else _case_differs(h, text, rx)
        if h["source"] != "module":            # haks are changed only through the Hak editor (a new hak name)
            c.update(ok=False, why=f"in a {h['source']} ({os.path.basename(h.get('source_path') or '')}) - edit it with the "
                                   "Hak editor; only the module's own files are changed here")
        elif twin and (h["kind"] in ("script", "filename") or (h.get("field_type") or "").lower() in ("resref", "cexostring")):
            c.update(ok=False, why=f"the capitals differ ('{twin}') - names are case-sensitive in NWN, so this is probably a "
                                   "different variable/tag/script; search for it separately if it should change too")
        elif h["kind"] == "script":
            srx = script_matcher(text, mode, case)
            after = srx.sub(lambda _m: f'"{new}"' if mode == "exact" else (new if case else _match_case(_m.group(0), new)),
                            h["value"])
            c.update(after=after, ok=True)
        else:
            after = new if mode == "exact" else rx.sub(lambda _m: new if case else _match_case(_m.group(0), new), h["value"])
            c.update(after=after, ok=True)
        if c.get("ok"):
            why = _validate(h, c["after"], resref_rule, tag_rule, new)
            # a blueprint's top-level TemplateResRef is its own name (the toolset keeps it equal to the file name),
            # so it may only change together with a rename of the file
            if not why and h.get("label") == "TemplateResRef" and h["where"] == "TemplateResRef" and \
                    h["file"].lower() not in renaming and c["after"].lower() != h["file"].rsplit(".", 1)[0].lower():
                why = ("this is the blueprint's own name - changing it here makes the file claim to be "
                       f"'{c['after']}' (two blueprints with one name); rename the file instead (tick its file-name hit)")
            if why:
                c.update(ok=False, why=why)
            elif c["after"] == c["before"]:
                c.update(ok=False, why="nothing would change")
        changes.append(c)
    missing = want - {c["id"] for c in changes}         # ticked hits a fresh search no longer finds
    for mid in sorted(missing):
        changes.append(dict(id=mid, ok=False, why="no longer found - search again", file=mid.split("|")[1], where="", before=""))
    _dry_run(analysis_dir, text, new, [c for c in changes if c.get("ok")], mode, case)
    notes = list(res.get("notes") or [])
    if any(c.get("ok") and c["kind"] == "script" and VAR_CALL_RE.search(c["before"]) for c in changes) or \
            any(c.get("ok") and "VarTable" in (c.get("where") or "") for c in changes):
        notes.append("This renames a variable (or database key). Values already saved under the old name - on players' "
                     "items and characters, or in the campaign database - are NOT renamed: existing players keep the old "
                     "value under the old name, so their progress looks reset unless you migrate it.")
    if any(c.get("ok") and c["kind"] in ("script", "filename") and c["file"].endswith(".nss") for c in changes):
        notes.append("Changed scripts must be compiled again (toolset Build > Compile, or the official compiler in "
                     "Settings) - until then the game runs the old compiled .ncs. The build audit warns about this.")
    return dict(text=text, new=new, mode=mode, case=case, changes=changes, ok=sum(1 for c in changes if c.get("ok")),
                refused=sum(1 for c in changes if not c.get("ok")), runtime_risk=res["runtime_risk"], notes=notes,
                files=sorted({c["file"] for c in changes if c.get("ok")}))


def _dry_run(analysis_dir, text, new, oks, mode, case):
    """Run apply's per-file checks without writing: a change apply would refuse is marked refused here, with why."""
    if not oks:
        return
    rx = matcher(text, mode, case)
    db = _open(analysis_dir)
    try:
        by_file = {}
        for c in oks:
            by_file.setdefault(c["file"], []).append(c)
        for rel, cs in by_file.items():
            ext = rel.rsplit(".", 1)[-1].lower()
            for c in cs:
                if c["kind"] == "filename":
                    target = f"{c['after'].lower()}.{ext}"
                    if os.path.exists(nwn_edit.edit_path(analysis_dir, target)) or \
                            db.execute("SELECT 1 FROM files WHERE lower(relpath)=?", (target,)).fetchone():
                        c.update(ok=False, why=f"a file called {target} already exists - pick another name")
            # GFF changes are tried on an in-memory copy of the current file (edited copy if any); nothing is saved
            body = [c for c in cs if c["kind"] not in ("filename", "script", "2da") and c.get("ok")]
            if not body:
                continue
            try:
                data, _e = nwn_edit.current_bytes(analysis_dir, rel, lambda r: module_bytes(analysis_dir, r, db))
                root = n.read_gff(data)
            except Exception as ex:  # noqa - reported per change
                for c in body:
                    c.update(ok=False, why=f"can't read {rel}: {ex}")
                continue
            for c in body:
                try:
                    _set_path(root, c["where"], new, rx, mode, case)
                except (ValueError, TypeError, IndexError, AttributeError) as ex:
                    c.update(ok=False, why=str(ex))
    finally:
        db.close()


def read_folder_file(folder, relpath):
    """The bytes of relpath inside a folder module, or None when reading it would leave the folder: a relpath with
    ".." or an absolute one, a symbolic link (a module folder from someone else may hold a link to any file on this
    machine), or a sub-folder that is a link to elsewhere. The check compares real paths (links resolved), with a
    trailing separator so "C:/mod2" never passes as inside "C:/mod". May raise OSError for an unreadable file."""
    root = os.path.realpath(folder)
    p = os.path.join(folder, relpath)
    if os.path.islink(p) or not os.path.realpath(p).startswith(os.path.join(root, "")):
        return None
    with open(p, "rb") as fh:
        return fh.read()


def module_bytes(analysis_dir, relpath, db):
    """The original bytes of `relpath` from the module (an unpacked folder or a .mod), or None. Read-only.
    analysis_dir: the analysis folder; db: its index (sqlite3 connection). Haks are never read here: only files
    whose source is the module itself. A folder module's file is read through read_folder_file."""
    row = db.execute("SELECT s.path FROM files f JOIN sources s ON s.id=f.source_id WHERE f.relpath=? AND s.kind='module'",
                     (relpath,)).fetchone()
    if not row:
        return None
    src = row[0]
    if os.path.isdir(src):
        return read_folder_file(src, relpath)
    erf = n.Erf(src)
    try:
        for e in erf.entries:
            if e.filename == relpath:
                return erf.read(e)
    finally:
        erf.close()
    return None


def _set_path(root, path, new, rx, mode, case=True):
    """Set the GFF leaf at `path` (e.g. 'Creature List[0]/ItemList[2]/Tag'). Returns (old text, new text).
    Refuses when the field no longer holds what was searched (the file was edited since the analysis).

    root: a parsed GFF (nwnlib.read_gff), changed in place. Only text fields can change: CExoString, ResRef (stored
    lower case) and CExoLocString (every language entry that matches). Raises ValueError with the reason otherwise."""
    node = root
    parts = path.split("/")
    for part in parts[:-1]:
        m = re.fullmatch(r"(.+)\[(\d+)\]", part)     # a list element, e.g. "ItemList[2]" -> ("ItemList", 2)
        if m:
            node = node.get(m.group(1))[int(m.group(2))]
        else:
            node = node.get(part)
    label = parts[-1]
    if node is None or label not in node.fields:
        raise ValueError(f"{path} no longer exists - the file was edited since the search; search again")
    f = node.fields[label]
    if f.type == n.CEXOLOCSTRING:
        old = f.value.text()
        if not any(v and rx.search(v) for v in f.value.entries.values()):
            # a localised string with a talk-table number and no text of its own shows the .tlk line in the game
            if f.value.strref != n.BAD_STRREF and not any(f.value.entries.values()):
                raise ValueError(f"{path}: this text lives in the talk table (.tlk), not in the module - change it there")
            raise ValueError(f"{path} no longer holds '{old}' as searched - the file was edited since; search again")
        for k, v in list(f.value.entries.items()):
            f.value.entries[k] = (new if mode == "exact" else rx.sub(lambda _m: new if case else _match_case(_m.group(0), new), v)) \
                if v and rx.search(v) else v
        return old, f.value.text()
    if f.type in (n.CEXOSTRING, n.RESREF):
        old = f.value
        if not rx.search(old or ""):
            raise ValueError(f"{path} now holds '{old}', not what was searched - the file was edited since; search again")
        val = _new_value(old, rx, new, mode)
        if f.type == n.RESREF:
            val = val.lower()               # resrefs kept lower case: in EE capitals cause trouble (see resref_case)
        f.value = val
        return old, val
    raise ValueError(f"{path}: only text fields can be replaced here")


def apply(analysis_dir, *a, **kw):
    """Make the replace/rename: same arguments as plan(). Writes only to <analysis>/edits/ (plus its .history).

    Re-plans first, so only changes the preview called OK are made. All-or-nothing for renames: in 'exact' or
    'word' mode any refused hit stops the whole apply, so the module never ends up using two names.
    Every new file is computed in memory before the first write. Holds _WRITE so two applies/undos can't interleave.
    Returns dict(id (history id for undo), written, changes, refused, runtime_risk). Raises ValueError, writing
    nothing, when nothing can change or a file changed since the search."""
    with _WRITE:
        return _apply(analysis_dir, *a, **kw)


def _apply(analysis_dir, text, new, hit_ids, mode="contains", case=False, kinds=KINDS):
    """Write the planned changes to the edits overlay (with a history copy for Undo). Returns the result."""
    p = plan(analysis_dir, text, new, hit_ids, mode, case, kinds)
    todo = [c for c in p["changes"] if c.get("ok")]
    bad = [c for c in p["changes"] if not c.get("ok")]
    if bad and mode in ("exact", "word"):
        # a rename must change every use it was asked to change, or the module ends up using two names
        raise ValueError(f"{len(bad)} of the ticked change(s) can't be made - first: {bad[0]['file']} {bad[0].get('where', '')}: "
                         f"{bad[0]['why']}. Nothing was changed. Untick those hits (if leaving them is really what you want) "
                         "or fix them first.")
    if not todo:
        raise ValueError("nothing to change")
    rx = matcher(text, mode, case)
    by_file = {}
    for c in todo:
        by_file.setdefault(c["file"], []).append(c)
    # the history id: fixed width (date_time_microseconds), so ids sort by time as plain text (undo relies on this)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ed = nwn_edit.edits_dir(analysis_dir)
    hist = os.path.join(ed, ".history", stamp)
    record = dict(id=stamp, when=_dt.datetime.now().isoformat(timespec="seconds"), text=text, new=new, mode=mode,
                  files=[], created=[], deleted_markers=[], changes=len(todo))
    db = _open(analysis_dir)
    written = []
    try:
        new_files = {}                  # target file name -> (original name, new bytes, renamed?)
        for rel, cs in sorted(by_file.items()):
            ext = rel.rsplit(".", 1)[-1].lower()
            renames = [c for c in cs if c["kind"] == "filename"]
            body = [c for c in cs if c["kind"] != "filename"]
            data, _edited = nwn_edit.current_bytes(analysis_dir, rel, lambda r: module_bytes(analysis_dir, r, db))
            if data is None:
                raise ValueError(f"{rel}: can't read it from the module")
            if body:
                if ext == "nss" or ext in nwn_edit.TEXT_EXTS and ext != "2da":
                    raw_text = n.decode_text(data)
                    eol = "\r\n" if "\r\n" in raw_text else "\n"      # keep the file's own line endings
                    lines = raw_text.replace("\r\n", "\n").split("\n")
                    srx = script_matcher(text, mode, case)
                    for c in body:
                        i = int(c["id"].split("|")[2]) - 1      # "script|<file>|<line>|<source>", line is 1-based
                        if i >= len(lines) or lines[i].strip() != c["before"]:
                            raise ValueError(f"{rel} line {i + 1} has changed since the search (the file was edited) - "
                                             "search again")
                        lines[i] = srx.sub(lambda _m: f'"{new}"' if mode == "exact" else
                                           (new if case else _match_case(_m.group(0), new)), lines[i])
                    data = n.encode_text(eol.join(lines))
                elif ext == "2da":
                    import nwn_2da
                    table, _iss = nwn_2da.parse(n.decode_text(data))
                    for c in body:
                        _k, _rel, row, col, _s = c["id"].split("|", 4)
                        if col not in table["columns"]:
                            raise ValueError(f"{rel}: column {col} is gone - search again")
                        ci = table["columns"].index(col) + 1        # +1: cell 0 of each row is the row label
                        rows_ = [r_ for r_ in table["rows"] if str(r_[0]) == str(row)]
                        if len(rows_) != 1 or not rx.search(rows_[0][ci] or ""):
                            raise ValueError(f"{rel} row {row} {col} has changed since the search - search again")
                        # an empty result becomes None, which nwn_2da writes as **** (the 2da "no value" mark)
                        rows_[0][ci] = _new_value(rows_[0][ci] or "", rx, new, mode) or None
                    data = n.encode_text(nwn_2da.write(table))
                else:
                    root = n.read_gff(data)
                    for c in body:
                        _set_path(root, c["where"], new, rx, mode, case)
                    data = n.write_gff(root)
                    n.read_gff(data)                # must read back, as for an editor save
            target = rel
            if renames:
                c = renames[0]
                target = f"{c['after'].lower()}.{ext}"
                if os.path.exists(nwn_edit.edit_path(analysis_dir, target)) or \
                        db.execute("SELECT 1 FROM files WHERE lower(relpath)=?", (target,)).fetchone():
                    raise ValueError(f"can't rename {rel} to {target}: a file with that name already exists")
            if target in new_files:
                raise ValueError(f"two files would both be called {target} - rename them one at a time")
            new_files[target] = (rel, data, bool(renames))
        # everything computed - now write: the record first (so a failure half-way can still be undone), then files
        os.makedirs(hist)           # only now: a refused apply leaves no empty history folder
        record["status"] = "writing"
        record["written_sha"] = {t.lower(): hashlib.sha256(d_).hexdigest() for t, (_r, d_, _x) in new_files.items()}
        with open(os.path.join(hist, "record.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1)
        _add_log(ed, dict(id=stamp, when=record["when"], text=text, new=new, files=len(new_files), changes=len(todo)))
        for target, (rel, data, renamed) in new_files.items():
            # back up every overlay file this step will overwrite or remove; anything not there yet is listed as
            # "created" so Undo deletes it
            for fn in {target, rel} | ({rel + ".deleted"} if renamed else set()):
                ep = os.path.join(ed, fn.lower())
                if os.path.isfile(ep):
                    shutil.copy2(ep, os.path.join(hist, fn.lower()))
                    if os.path.isfile(ep + ".new"):
                        shutil.copy2(ep + ".new", os.path.join(hist, fn.lower() + ".new"))
                else:
                    record["created"].append(fn.lower())
            ep = nwn_edit.edit_path(analysis_dir, target)
            with open(ep, "wb") as fh:
                fh.write(data)
            if renamed:
                open(ep + ".new", "w").close()
                record["created"].append(target.lower() + ".new")
                # the old name is dropped from the clean build; its edit (if any) is kept in history
                old_ep = os.path.join(ed, rel.lower())
                if os.path.isfile(old_ep):
                    os.remove(old_ep)
                if os.path.isfile(old_ep + ".new"):
                    # the old name was itself a new file: its .new marker goes with it (it was backed up above, so
                    # Undo brings both back); left behind it would mark the old name "new" if it is ever saved again
                    os.remove(old_ep + ".new")
                with open(old_ep + ".deleted", "w", encoding="utf-8") as fh:
                    fh.write(f"renamed to {target}\n")
                record["deleted_markers"].append(rel.lower())
            record["files"].append(dict(file=target, from_file=rel if renamed else None))
            written.append(target)
    finally:
        db.close()
    record["status"] = "done"
    with open(os.path.join(hist, "record.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
    return dict(id=stamp, written=written, changes=len(todo), refused=p["refused"], runtime_risk=p["runtime_risk"])


def _add_log(ed, entry):
    """Append one entry to edits/.history/log.json (called under _WRITE, so no two writers race)."""
    log = os.path.join(ed, ".history", "log.json")
    entries = json.load(open(log, encoding="utf-8")) if os.path.isfile(log) else []
    entries.append(entry)
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1)


def history(analysis_dir):
    """Every rename/replace so far, oldest first: [dict(id, when, text, new, files, changes[, undone])].
    Reads edits/.history/log.json; [] when there is none."""
    log = os.path.join(nwn_edit.edits_dir(analysis_dir), ".history", "log.json")
    return json.load(open(log, encoding="utf-8")) if os.path.isfile(log) else []


def undo(analysis_dir, hid):
    """Undo one rename/replace by its history id: put back the overlay as it was before it.

    Refused (ValueError, nothing changed) when the id is malformed, it is already undone, a later one is not undone
    yet, or a file it wrote has been edited since - see the module docstring for why. Writes only inside
    <analysis>/edits/. Returns dict(undone=id, files=[files that apply had written])."""
    with _WRITE:
        return _undo(analysis_dir, hid)


def _undo(analysis_dir, hid):
    # strict id pattern (as made by _apply): the id becomes a folder name, so nothing like "..\\x" may get through
    if not re.fullmatch(r"\d{8}_\d{6}_\d{6}", hid or ""):
        raise ValueError("bad history id")
    ed = nwn_edit.edits_dir(analysis_dir)
    hist = os.path.join(ed, ".history", hid)
    rec = json.load(open(os.path.join(hist, "record.json"), encoding="utf-8"))
    if rec.get("undone"):
        raise ValueError("already undone")
    later = [e for e in history(analysis_dir) if e["id"] > hid and not e.get("undone")]   # text order = time order
    if later:
        raise ValueError("undo the later rename/replace first (newest first), so nothing done since is lost")
    # is each file still exactly what this apply wrote? (a file that is missing - discarded since - is fine)
    changed = []
    for fn, sha in (rec.get("written_sha") or {}).items():
        p = os.path.join(ed, fn)
        if os.path.isfile(p) and hashlib.sha256(open(p, "rb").read()).hexdigest() != sha:
            changed.append(fn)
    if changed:
        raise ValueError(f"{', '.join(changed)} changed after this rename/replace (edited since) - undoing would lose that; "
                         "discard or save those edits elsewhere first")
    # 1) remove what the apply created, 2) remove the markers it added, 3) copy back what it overwrote
    for fn in rec["created"]:
        p = os.path.join(ed, fn)
        if os.path.isfile(p):
            os.remove(p)
    for old in rec.get("deleted_markers", []):
        p = os.path.join(ed, old + ".deleted")
        if os.path.isfile(p):
            os.remove(p)
    for fn in os.listdir(hist):
        if fn == "record.json":
            continue
        shutil.copy2(os.path.join(hist, fn), os.path.join(ed, fn))
    rec["undone"] = _dt.datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(hist, "record.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=1)
    log = os.path.join(ed, ".history", "log.json")
    entries = json.load(open(log, encoding="utf-8")) if os.path.isfile(log) else []
    for e in entries:
        if e["id"] == hid:
            e["undone"] = rec["undone"]
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=1)
    return dict(undone=hid, files=[f["file"] for f in rec["files"]])
