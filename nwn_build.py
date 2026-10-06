"""
nwn_build.py - write a CLEAN copy of the module.

Never modifies the original. Produces, in the output folder:
    <name>/            the clean module as an unpacked folder (toolset/nasher friendly), a git repository
    <name>.mod         the same content packed as a .mod file
    haks/              lean haks with NEW names (only when the plan asks for lean haks; see nwn_hakslim.py)
    changes.md/.json   every deletion and every rewritten reference, with the reason
Then runs the audit (nwn_audit.py) comparing original vs clean.

A plan (JSON) says what to do - the dashboard writes it for you:
    {"delete": ["<node>", ...],          # from the Safe-to-delete list
     "merge":  [<duplicate group id>, ...],  # only groups marked mergeable
     "allow_review": false}              # must be true to delete 'Review' items
Optional plan keys: "apply_edits" (default true: use <analysis>/edits/), "palette_moves" (default true: apply the
Blueprints page's waiting moves, <analysis>/palette_moves.json), "git" (default true), "lean_haks", "lean_haks_mode"
("builder" or "minimal") and "lean_haks_aggressive".

    python nwn_build.py <analysis folder> <plan.json> [--out DIR] [--name NAME] [--no-audit]

Never overwrite an original
---------------------------
Everything a build writes gets a NEW name, so copying a build into the game folders can never replace an original:
    * the clean module must not have the original's name (build() refuses), and check_target() refuses an output
      that is the original module, inside it, or a folder/.mod the toolkit did not make (BUILD_MARKER);
    * lean haks are written as <hak>_r1, _r2 ... and the clean module.ifo hak list is re-pointed at those names
      (_point_module_at_haks; each re-pointed field is logged as a change, so the audit accepts it). Only haks the
      lean plan changes are rebuilt: a hak with nothing to drop is left alone and keeps its name (unchanged_haks);
    * a Hak-editor rebuild that module.ifo (as edited) already lists, e.g. acpv41 -> acpv41_r1, is recognised
      (_editor_haks) and the audit checks the module against it instead of the original hak.
The original is only read: from its folder, or entry by entry from its .mod (nwnlib.Erf).

Pipeline (build(), in this order)
---------------------------------
    1. Check the plan and name; if an edited include has users and the script compiler is missing, refuse before
       anything is touched (their .ncs files would silently keep the old code).
    2. Prepare the output: remove the previous build's reports and .mod, empty the clean folder except .git.
    3. Work out deletions (refusing non-approved, Review and still-used items) and merges (rename maps).
    4. First build only (when git is installed and the plan allows it): copy the untouched original into the
       folder and commit it with git tag "original".
    5. Copy every module file: skip deleted / merged-away / renamed-in-edits files, overlay the edits from
       <analysis>/edits/, then rewrite references to merged items in GFF files and apply palette moves (Rewriter:
       the blueprint's palette number, and its entry in the module's palette file). Add new files.
    6. Compile edited/new scripts plus every script that includes an edited include (nwn_compile, into a fresh
       temp folder), and copy the new .ncs files in.
    7. Hak names: detect Hak-editor rebuilds; write lean haks and re-point module.ifo (optional).
    8. Pack the .mod (unless a name is too long or repeated), work out sizes, copy the tlk, write changes.md.
    9. Commit the clean folder to git (message passed via a file), write changes.json.
   10. Run the deterministic audit (nwn_audit.audit), unless run_audit=False.

Known limits: GFF files that can't be parsed are copied unchanged (with a warning); scripts that include an edited
include but have no .nss source can't be recompiled (a warning only - the audit's include_users check covers the
scripts that have a source).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import defaultdict

import nwnlib as n
import nwn_edit
from nwn_palette import PALETTE_FIELD, TYPES as PALETTE_TYPES
import nwn_bpchanges
import nwn_update
from nwn_index import GIT_LIST_CLASS, SCRIPT_LABEL_RE, DLG_SCRIPT_LABELS


# palette file stem (after removing "pal", "palcus" or "palstd", e.g. creaturepalcus -> creature) -> the blueprint
# type it lists
PALETTE_EXT = {"creature": "utc", "door": "utd", "encounter": "ute", "item": "uti", "placeable": "utp",
               "sound": "uts", "store": "utm", "trigger": "utt", "waypoint": "utw"}


def _node_ref(node):
    """node id -> (kind, resref, ext) used for reference rewriting."""
    if node.startswith("script:"):
        return "script", node[7:], None
    if node.startswith("dlg:"):
        return "dlg", node[4:], "dlg"
    if node.startswith("bp:"):
        r, _, e = node[3:].rpartition(".")
        return "bp", r, e
    return "other", node, None


class Rewriter:
    """Rewrites resref fields in GFF data according to rename maps.

    Used when duplicates are merged: every reference to a merged-away script, conversation or blueprint is
    re-pointed at the group's keeper, and palette (.itp) entries of removed blueprints are dropped. Also applies
    palette moves (Blueprints page): a moved blueprint's palette number is set, and its entry in the module's palette
    file is moved to the new category. Every change is appended to self.changes as dict(file, field, old, new, why);
    the audit's "modified" check later requires every changed GFF field in the clean module to match one of these
    records. Works on parsed GFF trees in memory (nwnlib); writes no files."""

    def __init__(self, script_map, dlg_map, bp_map, removed_bps, palette_moves=None, move_tree_exts=()):
        self.script_map = script_map      # old -> new
        self.dlg_map = dlg_map
        self.bp_map = bp_map              # (ext, old) -> new
        self.removed_bps = removed_bps    # {(ext, resref)} deleted outright (for palettes)
        self.palette_moves = palette_moves or {}    # (ext, resref) -> new palette number
        self.move_tree_exts = set(move_tree_exts)   # types whose palette file the game uses is the module's own
        self.moved_bps = set()            # (ext, resref) whose palette number was set
        self.moved_entries = set()        # (ext, resref) the module's palette file now lists under the new category
        # bulk blueprint changes (Blueprints page; set by build()): (ext, resref) -> {label: (old, new, field)}. The
        # blueprint gets the new values; a placed copy (same TemplateResRef, in an area) gets each one whose field
        # still holds the blueprint's old value - a copy the builder gave its own name keeps it
        self.bp_changes = {}
        self.bc_applied = set()           # (ext, resref) whose blueprint got its changes
        self.bc_placed = defaultdict(int)  # (ext, resref, field) -> placed copies changed
        self.changes = []

    def rewrite(self, fname, ext, root):
        """Rewrite one parsed GFF file in place. fname: its path in the module (for the change log); ext: its
        type; root: the nwnlib GffStruct. Returns True when anything changed."""
        self.cur = fname
        before = len(self.changes)
        if ext == "itp":
            pal_ext = PALETTE_EXT.get(re.sub(r"pal(cus|std)?$", "", os.path.splitext(os.path.basename(fname))[0].lower()))
            if pal_ext:  # only touch entries of the palette's own blueprint type
                self._prune_palette(root, "", pal_ext)
                # only the custom palette file (<type>palcus.itp) lists blueprints; skeletons (<type>pal.itp) don't
                if pal_ext in self.move_tree_exts and os.path.basename(fname).lower().endswith("palcus.itp"):
                    self._move_palette_entries(root, pal_ext)
        elif ext == "git":
            # an area's .git holds one list per object type (Creature List, Door List ...); each list tells the
            # walker which blueprint type a TemplateResRef inside it refers to
            for lab, (cls, bpext) in GIT_LIST_CLASS.items():
                for i, s in enumerate(root.get(lab, []) or []):
                    self._walk(s, f"{lab}[{i}]", bpext)
            # the area's own top-level fields (none of them is a TemplateResRef, so no blueprint context)
            self._walk_fields(root, "", None)
        else:
            # a blueprint file: its own type is the context; is_root=True so its own TemplateResRef (the
            # blueprint's name) is never rewritten, only references inside it
            self._walk(root, "", ext if ext in ("uti", "utc", "utp", "utd", "ute", "utm", "uts", "utt", "utw") else None,
                       is_root=True)
            key = (ext, os.path.splitext(os.path.basename(fname))[0].lower())
            if key in self.palette_moves:
                self._move_blueprint(root, key)
            if key in self.bp_changes:
                for label, (_old, new, field) in self.bp_changes[key].items():
                    if label in root.fields:
                        if root.get(label) != new:
                            self._set_value(root, label, new, "", "blueprint change (Blueprints page)")
                    else:                     # the field is missing (a hand-made blueprint): add it
                        root.set(label, BC_TYPE[field], new)
                        self.changes.append(dict(file=self.cur, field=label, old=None, new=_logval(new),
                                                 why="blueprint change (Blueprints page)"))
                self.bc_applied.add(key)
        return len(self.changes) > before

    def _set(self, s, label, new, path, why):
        """Set one field and log the change (path is the GFF field path used by the audit)."""
        old = s.get(label)
        s.fields[label].value = new
        self.changes.append(dict(file=self.cur, field=f"{path}/{label}".strip("/"), old=old, new=new, why=why))

    def _walk_fields(self, s, path, ctx_ext, is_root=False):
        """Re-point the resref fields of one struct (not its children). ctx_ext: blueprint type that a
        TemplateResRef here refers to (None = don't touch TemplateResRef)."""
        for label, f in list(s.fields.items()):
            if f.type == n.RESREF and f.value:
                v = f.value.lower()
                if (SCRIPT_LABEL_RE.match(label) or label in DLG_SCRIPT_LABELS) and v in self.script_map:
                    self._set(s, label, self.script_map[v], path, "script merged into duplicate keeper")
                elif label == "Conversation" and v in self.dlg_map:
                    self._set(s, label, self.dlg_map[v], path, "conversation merged into duplicate keeper")
                elif label == "TemplateResRef" and ctx_ext and not is_root and (ctx_ext, v) in self.bp_map:
                    self._set(s, label, self.bp_map[(ctx_ext, v)], path, "blueprint merged into duplicate keeper")
                elif label in ("InventoryRes", "EquippedRes") and ("uti", v) in self.bp_map:
                    self._set(s, label, self.bp_map[("uti", v)], path, "item blueprint merged into duplicate keeper")
                elif label == "ResRef" and "CreatureList" in path and ("utc", v) in self.bp_map:
                    self._set(s, label, self.bp_map[("utc", v)], path, "creature blueprint merged into duplicate keeper")

    def _walk(self, s, path, ctx_ext, is_root=False):
        """_walk_fields on s and, recursively, every struct and list below it. A struct that is a placed copy of a
        blueprint with waiting bulk changes (TemplateResRef + the list it sits in say which) gets them here."""
        self._walk_fields(s, path, ctx_ext, is_root=is_root)
        if self.bp_changes and ctx_ext and not is_root and "TemplateResRef" in s.fields:
            tr = (s.get("TemplateResRef") or "").lower()
            for label, (old, new, field) in self.bp_changes.get((ctx_ext, tr), {}).items():
                if label in s.fields and s.get(label) == old:
                    self._set_value(s, label, new, path, "placed copy follows its blueprint (Blueprints page)")
                    self.bc_placed[(ctx_ext, tr, field)] += 1
        for label, f in s.fields.items():
            if f.type == n.STRUCT:
                self._walk(f.value, f"{path}/{label}", ctx_ext)
            elif f.type == n.LIST:
                # inventories hold items and encounter CreatureLists hold creatures, whatever the parent's type
                child_ext = "uti" if label in ("ItemList", "Equip_ItemList") else (
                    "utc" if label == "CreatureList" else ctx_ext)
                for i, c in enumerate(f.value):
                    self._walk(c, f"{path}/{label}[{i}]", child_ext)

    def _set_value(self, s, label, new, path, why):
        """Set any field (also localised texts) and log it; a localised text is logged as its text form, which is
        what the audit compares the clean file's value with."""
        old = s.get(label)
        s.fields[label].value = new
        self.changes.append(dict(file=self.cur, field=f"{path}/{label}".strip("/"), old=_logval(old), new=_logval(new),
                                 why=why))

    def _move_blueprint(self, root, key):
        """Set a moved blueprint's palette number (PaletteID; a store's is called ID) and log it."""
        ext, _resref = key
        label, new = PALETTE_FIELD[ext], self.palette_moves[key]
        why = "palette move (Blueprints page)"
        if label in root.fields:
            if root.get(label) != new:
                self._set(root, label, new, "", why)
        else:                                 # no palette number at all (a hand-made blueprint): add the field
            root.set(label, n.BYTE, new)
            self.changes.append(dict(file=self.cur, field=label, old=None, new=new, why=why))
        self.moved_bps.add(key)

    def _move_palette_entries(self, root, pal_ext):
        """Move the palette file's entries of moved blueprints into the category with their new number.

        Categories are structs with an ID; blueprint entries are structs with a RESREF, inside a category's LIST. An
        entry is taken out of its list and appended to the target category's LIST (made if the category had none):
        nothing is added or dropped, so the audit's palette check (no new value, no entry lost) still holds. Each
        move is logged with the entry's old path. An entry already in the right category is only noted as listed."""
        want = {rr: to for (e, rr), to in self.palette_moves.items() if e == pal_ext}
        if not want:
            return
        cats, found = {}, []                  # cats: ID -> category struct; found: (list field, entry, path, parent)

        def walk(s, path):
            for label, f in s.fields.items():
                if f.type != n.LIST:
                    continue
                for i, c in enumerate(f.value):
                    rr = (c.get("RESREF") or "").lower()
                    if rr:
                        if rr in want:
                            found.append((f, c, f"{path}/{label}[{i}]".strip("/"), s, rr))
                    else:
                        if c.get("ID") is not None:
                            cats.setdefault(int(c.get("ID")), c)    # IDs are meant to be unique: first one wins
                        walk(c, f"{path}/{label}[{i}]")
        walk(root, "")
        for lst, entry, path, parent, rr in found:
            target = cats.get(want[rr])
            if target is None:                # the build checked the number; a hand-edited palette may still lack it
                continue
            if target is not parent:
                lst.value = [c for c in lst.value if c is not entry]
                if "LIST" not in target.fields:
                    target.set("LIST", n.LIST, [])
                target.fields["LIST"].value.append(entry)
                self.changes.append(dict(file=self.cur, field=path, old=rr, new=f"{rr} in palette category {want[rr]}",
                                         why="palette move: entry moved to the new category"))
            self.moved_entries.add((pal_ext, rr))

    def _prune_palette(self, s, path, pal_ext):
        """Drop palette entries (anywhere in the category tree) whose RESREF is a removed or merged-away blueprint of
        the palette's own type. Each dropped entry is logged with new=None."""
        gone = {r for (e, r) in self.removed_bps if e == pal_ext} | {old for (e, old) in self.bp_map if e == pal_ext}
        for label, f in s.fields.items():
            if f.type == n.LIST:
                keep = []
                for i, c in enumerate(f.value):
                    rr = (c.get("RESREF") or "").lower()
                    if rr and rr in gone:
                        self.changes.append(dict(file=self.cur, field=f"{path}/{label}[{i}]", old=rr, new=None,
                                                 why="palette entry for removed blueprint"))
                        continue
                    self._prune_palette(c, f"{path}/{label}[{i}]", pal_ext)
                    keep.append(c)
                f.value = keep
            elif f.type == n.STRUCT:
                self._prune_palette(f.value, f"{path}/{label}", pal_ext)


def _palette_moves(analysis_dir, plan, delete_nodes, merged_away):
    """The waiting palette moves (nwn_palette.load_moves) this build will apply, checked against the analysis.

    Returns (moves {(ext, resref): new palette number}, types whose palette file the game uses is the module's own,
    log entries [{type, resref, to, to_path, from, status "moved"/"skipped", note}]). A move is skipped, with a note,
    when its blueprint is deleted or merged away in this build, is no longer a module blueprint, or its number no
    longer names a category. plan["palette_moves"] false applies none. Reads only; never raises for a bad file."""
    import nwn_palette
    waiting = nwn_palette.load_moves(analysis_dir) if plan.get("palette_moves", True) else {}
    if not waiting:
        return {}, set(), []
    pal = nwn_palette.build_palette(analysis_dir, write=False)
    moves, tree_exts, entries = {}, set(), []
    for ext, mv in sorted(waiting.items()):
        t = pal["types"].get(ext)
        if t and t["tree_source"].startswith(nwn_palette.TYPES[ext][0] + "palcus.itp (module)"):
            tree_exts.add(ext)
        paths = {c["id"]: c["path"] for c in (t["categories"] if t else []) if c["id"] is not None}
        bps = {b["resref"]: b for b in (t["blueprints"] if t else [])}
        for rr, m in sorted(mv.items()):
            to, b, node = m.get("to") if isinstance(m, dict) else None, bps.get(rr), f"bp:{rr}.{ext}"
            e = dict(type=ext, resref=rr, to=to, to_path=paths.get(to, ""), status="skipped", note="")
            if node in delete_nodes:
                e["note"] = "deleted in this build"
            elif node in merged_away:
                e["note"] = "merged into its duplicate group's keeper in this build"
            elif not b or pal["sources"][b["src"]] != "module":
                e["note"] = "not a blueprint of the module (any more)"
            elif to not in paths:
                e["note"] = f"no palette category has the number {to!r} (any more)"
            else:
                e.update(status="moved", **{"from": b["pid"]})
                moves[(ext, rr)] = to
            entries.append(e)
    return moves, tree_exts, entries


def _finish_palette_moves(move_log, rw, tree_exts):
    """After the files were copied: mark what each move really did (from the Rewriter) - skipped when the blueprint
    file never reached the build (renamed or removed in your edits); entry_moved when the module's palette file now
    lists it under the new category; otherwise a note says only the palette number changed, and why."""
    for e in move_log:
        if e["status"] != "moved":
            continue
        key = (e["type"], e["resref"])
        if key not in rw.moved_bps:
            e.update(status="skipped",
                     note="the blueprint file was not in the build (renamed or removed in your edits)")
        elif key in rw.moved_entries:
            e["entry_moved"] = True
        elif e["type"] in tree_exts:
            e["note"] = "the palette file doesn't list it, so only its palette number changed"
        else:
            e["note"] = ("the palette file the game uses is not the module's own (it comes from a hak, or there is "
                         "none), so only the palette number changed")


TYPE_LABEL = {ext: label for ext, (_stem, label) in PALETTE_TYPES.items()}    # uti -> Items (change log)
# GFF field type of each bulk-changeable field, for a blueprint that lacks the field altogether
BC_TYPE = {f: n.CEXOLOCSTRING for f in nwn_bpchanges.TEXT_FIELDS}
BC_TYPE.update(tag=n.CEXOSTRING, faction=n.WORD)


def _logval(v):
    """A field value as the change log keeps it: localised texts as their text form (json can't hold them)."""
    return str(v) if isinstance(v, n.LocString) else v


def _blueprint_changes(plan, changes, files, delete_nodes, merged_away, read):
    """The waiting bulk blueprint changes (nwn_bpchanges.load_changes) this build will apply.

    changes: the waiting changes; files: the module's files rows (relpath, resref, ext, node, sha); read(relpath):
    the bytes the build will copy (the edits overlay's copy when there is one). Each blueprint is read to learn its
    old values, which decide which placed copies follow. Returns ({(ext, resref): {label: (old, new, field)}}, log
    entries [{type, resref, field, from, to, status "changed"/"skipped", note, placed}]). plan["blueprint_changes"]
    false applies none. Never raises for a bad file: such a change is skipped with a note."""
    if not plan.get("blueprint_changes", True) or not changes:
        return {}, []
    where = {(e, (r or "").lower()): rel for rel, r, e, _nd, _sha in files}
    out, log = {}, []
    for ext, bps in sorted(changes.items()):
        for rr, fields in sorted(bps.items()):
            node = f"bp:{rr}.{ext}"
            entries = [dict(type=ext, resref=rr, field=f, to=v.get("to"), status="skipped", note="", placed=0,
                            **{"from": v.get("from")}) for f, v in sorted(fields.items())]
            log += entries
            rel = where.get((ext, rr))
            why = ("deleted in this build" if node in delete_nodes else
                   "merged into its duplicate group's keeper in this build" if node in merged_away else
                   "not a blueprint of the module (any more)" if not rel else
                   "" if ext in nwn_bpchanges.FIELDS else "unknown blueprint type")
            root = None
            if not why:
                try:
                    root = n.read_gff(read(rel))
                except (OSError, n.GffError, KeyError) as ex:
                    why = f"the blueprint could not be read ({ex})"
            for e in entries:
                label = nwn_bpchanges.FIELDS.get(ext, {}).get(e["field"])
                if why or not label:
                    e["note"] = why or f"no field '{e['field']}' on this type"
                    continue
                try:
                    new = nwn_bpchanges.new_value(e["field"], e["to"])
                except (TypeError, ValueError):
                    e["note"] = f"'{e['to']}' is not a valid value"
                    continue
                old = root.get(label) if label in root.fields else None
                out.setdefault((ext, rr), {})[label] = (old, new, e["field"])
                e["status"] = "changed"
    return out, log


def _finish_blueprint_changes(bc_log, rw):
    """After the files were copied: a change whose blueprint file never reached the build (renamed or removed in
    your edits) is marked skipped; every applied one gets how many placed copies followed."""
    for e in bc_log:
        if e["status"] != "changed":
            continue
        if (e["type"], e["resref"]) not in rw.bc_applied:
            e.update(status="skipped", note="the blueprint file was not in the build (renamed or removed in your edits)")
        else:
            e["placed"] = rw.bc_placed.get((e["type"], e["resref"], e["field"]), 0)
BUILD_MARKER = ".nwn_toolkit_build"     # written into every build folder: only folders carrying it are ever emptied
# allowed build names: starts with a letter, digit or _, then up to 63 of letters, digits, _ space . -
# (no path separators, so a name can't point outside the output folder), e.g. "mymodule_clean v2"
NAME_OK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ .-]{0,63}")


def validate_plan(plan):
    """Check the plan's shape before anything on disk is touched. Returns a cleaned copy or raises ValueError.

    Checks only types: "delete" must be a list of strings and "merge" a list of group numbers (converted to int).
    Whether each item may really be deleted or merged is decided later in build(), against report.json."""
    if not isinstance(plan, dict):
        raise ValueError("the plan must be a JSON object")
    out = dict(plan)
    dl = plan.get("delete", [])
    if not isinstance(dl, list) or not all(isinstance(x, str) for x in dl):
        raise ValueError("plan 'delete' must be a list of node names (text)")
    mg = plan.get("merge", [])
    if not isinstance(mg, list):
        raise ValueError("plan 'merge' must be a list of duplicate group numbers")
    ids = []
    for g in mg:
        try:
            ids.append(int(g))
        except (TypeError, ValueError):
            raise ValueError(f"plan 'merge' has {g!r}, which is not a duplicate group number")
    out["merge"] = ids
    return out


def check_target(module_path, out_dir, name):
    """Refuse any output that could overwrite the original module or a folder the toolkit did not create.

    module_path: the original (.mod file or module folder); out_dir: where the build goes; name: the build name.
    Returns (clean folder path, .mod path). Raises ValueError when:
        * the name is not allowed (NAME_OK, a trailing dot or space, or "..");
        * the clean folder or .mod is the original, contains it, or (for a folder module) lies inside it;
        * the clean folder or .mod already exists and was not made by a toolkit build.
    Read-only: it only checks; build() does the emptying afterwards."""
    # trailing dots and spaces: Windows silently drops them from file names, so "x." or "x " would really be "x"
    # (and "x " would then be a second folder for the same name on Linux/macOS)
    if not isinstance(name, str) or not NAME_OK.fullmatch(name) or name.strip(". ") != name or ".." in name:
        raise ValueError(f"build name {name!r} is not allowed - use letters, digits, _ - . and spaces only")
    target = os.path.join(out_dir, name)
    mod_file = os.path.join(out_dir, f"{name}.mod")
    # realpath resolves links and ".."; normcase makes the comparison case-insensitive on Windows
    real = lambda p_: os.path.normcase(os.path.realpath(p_))  # noqa: E731
    src = real(module_path)
    for p_ in (target, mod_file):
        if real(p_) == src or src.startswith(real(p_) + os.sep) or (os.path.isdir(module_path) and real(p_).startswith(src + os.sep)):
            raise ValueError(f"refusing to build over or inside the original module ({module_path}) - choose another "
                             "folder or name")
    # ours: carries the marker, or (builds made before the marker existed) a git history next to a change log
    ours = os.path.isfile(os.path.join(target, BUILD_MARKER)) or (
        os.path.isdir(os.path.join(target, ".git")) and os.path.isfile(os.path.join(out_dir, "changes.json")))
    if os.path.exists(target) and not ours:
        if not os.path.isdir(target) or os.listdir(target):        # an empty folder is safe to use
            raise ValueError(f"{target} already exists and was not made by a toolkit build - refusing to empty it; "
                             "choose another name or folder")
    if os.path.exists(mod_file) and not ours:
        raise ValueError(f"{mod_file} already exists and was not made by a toolkit build - refusing to replace it")
    return target, mod_file


def check_tlk_target(tlk_path, out_dir):
    """Where the module's custom talk table will be copied to, and whether the copy is still needed.

    tlk_path: the original tlk (from the analysis meta; None/"" or a missing file = no tlk); out_dir: the build folder.
    Returns (destination path or None, copy needed). Raises ValueError when a file of that name already sits in
    out_dir and is NOT a copy the toolkit may replace: the same file, a byte-identical one (replacing it changes
    nothing) or the file the previous build's changes.json recorded as its tlk copy. Anything else was put there by
    someone else, and a build must never overwrite it (golden rule 1). Read-only."""
    if not tlk_path or not os.path.isfile(tlk_path):
        return None, False
    dest = os.path.join(out_dir, os.path.basename(tlk_path))
    if not os.path.exists(dest):
        return dest, True
    if os.path.samefile(tlk_path, dest):      # --out is the tlk's own folder: nothing to copy, nothing to replace
        return dest, False
    real = lambda p_: os.path.normcase(os.path.realpath(p_))  # noqa: E731
    try:
        with open(os.path.join(out_dir, "changes.json"), encoding="utf-8") as fh:
            prev = json.load(fh).get("tlk_file") or ""
    except (OSError, ValueError):
        prev = ""
    if prev and real(prev) == real(dest):
        return dest, True
    with open(tlk_path, "rb") as a, open(dest, "rb") as b:
        if a.read() == b.read():
            return dest, True
    raise ValueError(f"{dest} already exists and is not this toolkit's copy of the module's talk table - refusing to "
                     "overwrite it; move it away or choose another output folder")


def build(analysis_dir, plan, out_dir=None, name=None, run_audit=True, verbose=True, audit_kwargs=None, stop=None,
          progress=None):
    """Write the clean module described by `plan`, then audit it. The pipeline is in the module docstring.

    analysis_dir: the analysis folder of the ORIGINAL module (report.json, index.sqlite, edits/).
    plan: dict as described in the module docstring (checked by validate_plan).
    out_dir: output folder (default <analysis>/build); name: clean module name (default <module>_clean, and never
        the original's name).
    run_audit: run nwn_audit.audit at the end; audit_kwargs: passed to it, and "compiler"/"nwn_root"/"nwn_user"
        are also used to compile edited scripts.
    stop(): optional callback; returning True raises InterruptedError at the next check point (the build folder
        is then incomplete and must not be used).
    progress: optional nwn_progress.BuildTracker (steps, % and time left for the dashboard).
    Returns the change log dict (also written to changes.json); with run_audit, log["audit"] is the audit result.
    Raises ValueError for a bad plan, a bad name, an unsafe output or a missing compiler for an edited include -
    all checked before the output folder is emptied. Never writes the original module or haks; writes only
    out_dir (which it creates) and temporary folders."""
    t0 = time.time()

    class _NoProgress:                      # stands in for the tracker when none is given (command line)
        def step(self, *a, **k): pass
        def part(self, *a, **k): pass
    pg = progress or _NoProgress()
    # Work saved by a newer toolkit (a kind of change this version doesn't know) would be left out without a word:
    # refuse instead (nwn_update, "newer-work guard").
    nwn_update.check_work(analysis_dir)
    plan = validate_plan(plan)
    rep = json.load(open(os.path.join(analysis_dir, "report.json"), encoding="utf-8"))
    db = sqlite3.connect(os.path.join(analysis_dir, "index.sqlite"))
    meta_ = dict(db.execute("SELECT key, value FROM meta").fetchall())
    module_path = meta_["module_path"]
    nasher = meta_.get("module_format") == "nasher json"   # sources only: every script is compiled in step 6
    mod_base = os.path.splitext(os.path.basename(os.path.normpath(module_path)))[0]
    name = name or f"{mod_base}_clean"
    if name.lower() == mod_base.lower():
        raise ValueError(f"the clean module needs a NEW name - '{name}' is the original's name, and copying it into your "
                         "modules folder would replace the original. Pick another name (e.g. "
                         f"{mod_base[:50]}_clean).")
    # an edited include changes every script that includes it: those must be recompiled, so without the compiler the
    # build can't be correct - refuse now, before anything is touched (stale .ncs files would silently run old code)
    early_edits = nwn_edit.list_edits(analysis_dir) if plan.get("apply_edits", True) else {}
    edited_incs = _edited_includes(analysis_dir, early_edits)
    inc_users = _include_users(db, edited_incs) if edited_incs else []
    if inc_users:
        import nwn_compile
        if not nwn_compile.find_compiler((audit_kwargs or {}).get("compiler")):
            raise ValueError(f"you edited {', '.join(sorted(edited_incs)[:3])}.nss, which {len(inc_users)} script(s) "
                             "#include - they must all be recompiled, and the official script compiler (nwn_script_comp) "
                             "isn't installed. Put it in the toolkit's tools folder (see tools/README.txt) and build again. "
                             "Nothing was changed.")
    out_dir = out_dir or os.path.join(analysis_dir, "build")
    os.makedirs(out_dir, exist_ok=True)
    target, stale_mod = check_target(module_path, out_dir, name)
    meta_ = dict(db.execute("SELECT key, value FROM meta").fetchall())
    # checked now, before the previous build's reports are removed (its changes.json says whether the tlk copy is ours)
    tlk_dest, tlk_copy = check_tlk_target(meta_.get("tlk"), out_dir)
    if plan.get("lean_haks") and os.path.isdir(os.path.join(out_dir, "haks")):
        import nwn_hakslim
        nwn_hakslim.existing_lean_haks(os.path.join(out_dir, "haks"))   # refuse now if haks/ holds haks we didn't write
    stop = stop or (lambda: False)
    use_git = plan.get("git", True) and shutil.which("git") is not None    # no git installed: build without history
    # a previous build's reports must never sit next to this build (a crash would leave its PASS verdict showing)
    old_reports = ["changes.md", "changes.json", "audit.md", "audit.json", "audit_signoff.md"]
    for old in old_reports:
        if os.path.isfile(os.path.join(out_dir, old)):
            os.remove(os.path.join(out_dir, old))
    if os.path.exists(target):
        for entry in os.listdir(target):          # keep version history (.git) between builds
            if entry in (".git", BUILD_MARKER):
                continue
            p_ = os.path.join(target, entry)
            n.rmtree_force(p_) if os.path.isdir(p_) else _remove_file(p_)
    else:
        os.makedirs(target)
    with open(os.path.join(target, BUILD_MARKER), "w", encoding="utf-8") as fh:
        fh.write(f"built by the NWN toolkit from {module_path}\n")
    new_repo = use_git and not os.path.isdir(os.path.join(target, ".git"))     # first build into this folder
    if os.path.exists(stale_mod):
        os.remove(stale_mod)  # never leave a previous build's .mod next to a new build (ours - checked above)

    # step 3: decide what goes. Only items the analysis itself listed (report.json) can be deleted or merged -
    # the plan can narrow that list, never widen it
    deletable = {d["node"]: d for d in rep["deletions"]}
    groups = {g["id"]: g for g in rep["duplicates"]}
    log = dict(module=module_path, output=target, started=time.strftime("%Y-%m-%d %H:%M:%S"),
               deleted=[], merged=[], rewritten=[], refused=[], warnings=[], edited=[], added=[])
    edits = nwn_edit.list_edits(analysis_dir) if plan.get("apply_edits", True) else {}

    delete_nodes = set()
    for nid in plan.get("delete", []):
        d = deletable.get(nid)
        if not d:
            log["refused"].append(dict(node=nid, why="not on the safe-to-delete list (it is in use, or no such thing)"))
            continue
        if d["status"] == "Review" and not plan.get("allow_review"):
            log["refused"].append(dict(node=nid, why="marked Review - tick 'allow review items' to delete: " + d["reason"]))
            continue
        delete_nodes.add(nid)
    # 'Safe as group' items may only go if everything that uses them goes too (repeat until stable)
    changed = True
    while changed:
        changed = False
        for nid in sorted(delete_nodes):
            keep_users = [u for u in (deletable[nid].get("group") or []) if u not in delete_nodes]
            if keep_users:
                delete_nodes.discard(nid)
                changed = True
                log["refused"].append(dict(node=nid, why="still used by " + ", ".join(keep_users[:3]) +
                                           " which is being kept - delete those too, or keep this"))

    # merges: every non-keeper member of a mergeable group is removed and references to it are re-pointed at the
    # keeper (script_map / dlg_map: old resref -> keeper; bp_map: (ext, old resref) -> keeper)
    script_map, dlg_map, bp_map = {}, {}, {}
    merged_away = set()
    for gid in plan.get("merge", []):
        g_ = groups.get(int(gid))
        if not g_:
            log["refused"].append(dict(node=f"group {gid}", why="there is no duplicate group with this number"))
            continue
        if not g_["mergeable"]:
            log["refused"].append(dict(node=f"group {gid}", why="not mergeable: " + "; ".join(g_["blocked"]) if g_["blocked"]
                                       else "this duplicate type needs a manual decision"))
            continue
        keeper = g_["keeper"]
        kk, kres, kext = _node_ref(keeper)
        for m in g_["members"]:
            if m["node"] == keeper or m["node"] in delete_nodes:
                continue
            k, res, ext = _node_ref(m["node"])
            if k == "script":
                script_map[res] = kres
            elif k == "dlg":
                dlg_map[res] = kres
            elif k == "bp":
                bp_map[(ext, res)] = kres
            else:
                continue
            merged_away.add(m["node"])
            log["merged"].append(dict(group=gid, removed=m["label"], keeper=g_["members"][0]["label"] if
                                      g_["members"][0]["node"] == keeper else keeper, category=g_["category"]))
    keepers = {groups[int(gid)]["keeper"] for gid in plan.get("merge", []) if int(gid) in groups and
               groups[int(gid)]["mergeable"]}
    for k_ in sorted(keepers & delete_nodes):
        delete_nodes.discard(k_)
        log["refused"].append(dict(node=k_, why="it is the keeper of a duplicate group being merged, so it must stay"))
    removed_bps = set()
    for nid in delete_nodes:
        k, res, ext = _node_ref(nid)
        if k == "bp":
            removed_bps.add((ext, res))

    # palette moves the Blueprints page left waiting (palette_moves.json), checked against this analysis
    palette_moves, move_tree_exts, move_log = _palette_moves(analysis_dir, plan, delete_nodes, merged_away)
    rw = Rewriter(script_map, dlg_map, bp_map, removed_bps, palette_moves, move_tree_exts)
    # the original module's own files (not haks or override), read from the index
    files = db.execute("SELECT f.relpath, f.resref, f.ext, f.node, f.sha256 FROM files f JOIN sources s "
                       "ON s.id=f.source_id WHERE s.kind='module'").fetchall()
    erf = None if os.path.isdir(module_path) else n.Erf(module_path)
    # a .mod can list one name twice (two entries whose names differ only in capitals); the first one is kept, as
    # neverwinter.nim's ERF reader does - the copy loop below skips the later ones with a warning
    erf_entries = {}
    for e in (erf.entries if erf else []):
        erf_entries.setdefault(e.filename, e)

    def read_for_build(relpath):
        """The bytes the copy step will use for a module file: the edits overlay's copy if there is one."""
        fl = os.path.basename(relpath).lower()
        if fl in edits and edits[fl]["kind"] == "edited":
            with open(os.path.join(analysis_dir, "edits", fl), "rb") as fh:
                return fh.read()
        return erf.read(erf_entries[relpath]) if erf else n.read_file_inside(module_path, relpath)
    # bulk blueprint changes the Blueprints page left waiting (blueprint_changes.json)
    rw.bp_changes, bc_log = _blueprint_changes(plan, nwn_bpchanges.load_changes(analysis_dir), files, delete_nodes,
                                               merged_away, read_for_build)
    git_info = None
    if new_repo:
        # first build: commit the untouched original so every later change is a readable git diff
        pg.step("snapshot", f"{len(files):,} files")
        _git(target, "init", "-q")
        try:
            # keep the marker file out of the history (.git/info/exclude works like a private .gitignore)
            with open(os.path.join(target, ".git", "info", "exclude"), "a", encoding="utf-8") as fh:
                fh.write("\n" + BUILD_MARKER + "\n")
        except OSError:
            pass
        for relpath, resref, ext, node, sha in files:
            data = erf.read(erf_entries[relpath]) if erf else n.read_file_inside(module_path, relpath)
            dest = _safe_dest(target, relpath)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
        _git(target, "add", "-A")
        _git(target, "commit", "-q", "-m", f"Original module snapshot: {mod_base}\n\nSource: {module_path}")
        _git(target, "tag", "-f", "original")
        # empty the folder again (history stays in .git): step 5 writes the clean files from scratch
        for entry in os.listdir(target):
            if entry not in (".git", BUILD_MARKER):
                p_ = os.path.join(target, entry)
                n.rmtree_force(p_) if os.path.isdir(p_) else _remove_file(p_)
    # step 5: copy the module file by file. packed collects (resref, ext, bytes) for the .mod, so the folder and
    # the .mod get exactly the same bytes (the audit's "mod" check proves it)
    packed = []
    copied = set()                      # .mod entry names already copied (see erf_entries)
    pg.step("files", f"{len(files):,} files")
    for i_, (relpath, resref, ext, node, sha) in enumerate(files):
        if i_ % 200 == 0:
            pg.part(i_ / max(1, len(files)), f"{i_:,} of {len(files):,} files")
            if stop():
                raise InterruptedError("build stopped - the build folder is incomplete; build again")
        if erf and relpath in copied:
            log["warnings"].append(f"the original .mod holds {relpath} more than once (names that differ only in "
                                   "capitals); only the first entry was kept")
            continue
        copied.add(relpath)
        if resref.startswith("invalid_name_") and erf:
            # the original has an entry whose name is empty or unsafe (nwnlib renamed it on reading). The game can't
            # load a resource without a valid name, and such a name can't be written back, so the entry is left out
            # and logged as a removal (the audit then accounts for it instead of calling it unapproved).
            size = erf_entries[relpath].size
            log["deleted"].append(dict(file=relpath, node=node, status="unloadable",
                                       reason=f"entry {resref[13:]} of the original has no usable name "
                                              f"({size:,} bytes) - the game can't load it"))
            log["warnings"].append(f"left out entry {resref[13:]} of the original: it has no usable name "
                                   f"({size:,} bytes), so the game never loads it")
            continue
        if node in delete_nodes or node in merged_away:
            if node in delete_nodes:
                log["deleted"].append(dict(file=relpath, node=node, status=deletable[node]["status"],
                                           reason=deletable[node]["reason"] or deletable[node]["note"] or "unused"))
            else:
                log["deleted"].append(dict(file=relpath, node=node, status="merged", reason="duplicate merged into keeper"))
            continue
        fname_l = os.path.basename(relpath).lower()
        if edits.get(fname_l, {}).get("kind") == "deleted":
            # renamed (or removed) with Find & replace - the new name comes in as an added file below
            log["deleted"].append(dict(file=relpath, node=node, status="removed in your edits",
                                       reason="renamed or removed with Find & replace (see edits/.history)"))
            continue
        data = erf.read(erf_entries[relpath]) if erf else n.read_file_inside(module_path, relpath)
        # edits overlay: a file saved in the editor replaces the original's bytes (edits are keyed by file name)
        if fname_l in edits and edits[fname_l]["kind"] == "edited":
            with open(os.path.join(analysis_dir, "edits", fname_l), "rb") as fh:
                data = fh.read()
            log["edited"].append(relpath)
        # reference rewriting runs on the edited bytes too, and only when something was merged or removed: an
        # untouched GFF is copied byte for byte rather than re-serialised
        # (palette moves alone only need the moved blueprints and the palette files parsed)
        moved_here = palette_moves and ((ext, (resref or "").lower()) in palette_moves or ext == "itp")
        # bulk changes need the changed blueprints and the areas' placed objects (.git) parsed
        changed_here = rw.bp_changes and (ext == "git" or (ext, (resref or "").lower()) in rw.bp_changes)
        if ext in n.GFF_EXTENSIONS and (script_map or dlg_map or bp_map or removed_bps or moved_here or changed_here):
            try:
                root = n.read_gff(data)
                if rw.rewrite(relpath, ext, root):
                    data = n.write_gff(root)        # re-serialised only if a field actually changed
                    log["rewritten"].append(relpath)
            except n.GffError as ex:
                log["warnings"].append(f"{relpath}: could not parse, copied unchanged ({ex})")
        dest = _safe_dest(target, relpath)      # refuses a relpath that would escape the build folder
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(data)
        packed.append((resref, ext, data))

    _finish_palette_moves(move_log, rw, move_tree_exts)
    log["palette_moves"] = move_log
    _finish_blueprint_changes(bc_log, rw)
    log["blueprint_changes"] = bc_log
    # brand-new files created in the editor / script generator
    have = {f"{r}.{e}" for r, e, _ in packed}
    for fname_l, info in edits.items():
        if info["kind"] == "edited" and info.get("new") and fname_l not in have:
            r_, _, e_ = fname_l.rpartition(".")
            if not nwn_edit.RESREF_RE.fullmatch(r_):
                log["warnings"].append(f"{fname_l} was NOT added: '{r_}' can't be a resource name (letters, digits and _ "
                                       "only, 16 characters at most) - rename it in My edits")
                continue
            with open(os.path.join(analysis_dir, "edits", fname_l), "rb") as fh:
                data = fh.read()
            with open(_safe_dest(target, fname_l), "wb") as fh:
                fh.write(data)
            packed.append((r_, e_, data))
            log["added"].append(fname_l)
    # step 6: compile edited/new scripts - and every script that includes an edited include - into the clean build.
    # An include itself has no main() and can't be compiled on its own, so it is taken out of the list
    to_compile = sorted({os.path.basename(f).lower()[:-4] for f in log["edited"] + log["added"] if f.lower().endswith(".nss")}
                        - edited_incs)
    users = [u for u in inc_users if os.path.isfile(os.path.join(target, f"{u}.nss"))]
    no_source = sorted(set(inc_users) - set(users))
    if edited_incs:
        log["edited_includes"] = sorted(edited_incs)
        log["include_users"] = users
        if no_source:
            log["warnings"].append(f"{len(no_source)} script(s) include an edited include but have no .nss source in the "
                                   f"module, so they can't be recompiled and keep their old code: {', '.join(no_source[:8])}")
    to_compile = sorted(set(to_compile) | set(users))
    if nasher:
        # a nasher project has no .ncs (nasher compiles when it packs): compile every script with main() or
        # StartingConditional() that the clean folder has as source but not compiled
        src_only = sorted(nm for (nm,) in db.execute("SELECT name FROM scripts WHERE has_main = 1 OR has_sc = 1")
                          if os.path.isfile(os.path.join(target, f"{nm}.nss"))
                          and not os.path.isfile(os.path.join(target, f"{nm}.ncs")))
        log["nasher_compile"] = len(src_only)
        to_compile = sorted(set(to_compile) | set(src_only))
    if to_compile:
        import nwn_compile
        import tempfile
        ak = audit_kwargs or {}
        pg.step("compile", f"{len(to_compile)} script(s)")
        out_tmp = tempfile.mkdtemp(prefix="nwn_build_ncs_")      # fresh: an old .ncs can never pass as a new one
        src_tmp = _compile_sources(db, target, set(to_compile))  # the clean folder's scripts + hak copies, game order
        try:
            # compile_files runs the official compiler without a shell (argument list), in batches that keep each
            # command line under Windows' ~32,000-character limit (WinError 206)
            cr = nwn_compile.compile_files(src_tmp, to_compile, out_tmp, ak.get("compiler"), ak.get("nwn_root"),
                                           ak.get("nwn_user"))
            if cr["status"] == "ran":
                for nm in cr["written"]:
                    with open(os.path.join(out_tmp, f"{nm}.ncs"), "rb") as fh:
                        data = fh.read()
                    with open(_safe_dest(target, f"{nm}.ncs"), "wb") as fh:
                        fh.write(data)
                    # replace the old compiled copy in the pack list (or add it, for a new script)
                    packed = [(r_, e_, d_) for r_, e_, d_ in packed if not (r_ == nm and e_ == "ncs")] + [(nm, "ncs", data)]
                    log["compiled"] = log.get("compiled", []) + [nm]
                for nm, errs in cr["results"].items():
                    if errs:
                        log["warnings"].append(f"{nm}.nss did not compile: {errs[0]}" +
                                               (" (it includes an edited include)" if nm in users else ""))
                for nm in cr.get("missing") or []:
                    # asked for, but no <name>.nss was in the folder the compiler got: its .ncs is not rebuilt
                    log["warnings"].append(f"{nm}.nss was not found for compiling, so its compiled script was not "
                                           "rebuilt")
                failed_users = sorted(set(users) - set(cr["written"]))
                if failed_users:
                    log["include_users_not_compiled"] = failed_users
            elif nasher:
                log["warnings"].append(f"nasher project: {len(to_compile)} script(s) not compiled ({cr.get('reason', cr['status'])})"
                                       " - this .mod would run none of them. Set the official compiler (nwn_script_comp) "
                                       "in Settings and build again, or use Save as nasher project and nasher pack")
            else:
                log["warnings"].append(f"{len(to_compile)} edited/new script(s) not compiled: {cr.get('reason', cr['status'])} "
                                       "- compile them in the toolset or install nwn_script_comp")
                if users:
                    log["include_users_not_compiled"] = users
        finally:
            shutil.rmtree(out_tmp, ignore_errors=True)
            shutil.rmtree(src_tmp, ignore_errors=True)
    # step 7: hak names.
    # haks the (edited) module.ifo lists instead of the analysed ones - e.g. a Hak-editor rebuild (acpv41 -> acpv41_r1)
    editor_haks, replaced = _editor_haks(packed, db, log)
    if editor_haks:
        log["editor_haks"] = editor_haks
    # lean haks (written before the .mod, because the module must point at their NEW names)
    lean_man = None
    if plan.get("lean_haks"):
        import nwn_hakslim
        pg.step("lean")
        lean_man = nwn_hakslim.write_lean_haks(analysis_dir, out_dir, mode=plan.get("lean_haks_mode") or "builder",
                                               aggressive=bool(plan.get("lean_haks_aggressive")), verbose=verbose,
                                               progress=pg.part, stop=stop, skip=replaced)
        log["warnings"].extend((lean_man or {}).get("warnings", []))
        for r_ in sorted(replaced):
            log["warnings"].append(f"{r_}.hak was replaced by {os.path.basename(editor_haks[r_])} in your module.ifo edit, so "
                                   "it was not slimmed - re-analyse with the new hak to slim it")
        if lean_man:
            # {original hak name (lower case): new lean hak name} for the haks that were rebuilt; module.ifo is
            # re-pointed, logged in rw.changes. A hak with nothing to drop was left alone (unchanged): the clean module
            # keeps loading the original under its own name, so its module.ifo entry is not touched
            renamed = {k.lower(): v["new_name"] for k, v in lean_man["haks"].items() if not v.get("unchanged")}
            if renamed:
                packed = _point_module_at_haks(packed, target, renamed, rw, log)
            log["renamed_haks"] = renamed
            log["unchanged_haks"] = sorted(k for k, v in lean_man["haks"].items() if v.get("unchanged"))
    # step 8: pack the .mod. An ERF has no folders and 16-character names, so a file name over 16 characters, or the
    # same name in two subfolders of a folder module, can't be packed: the folder is still written, the .mod is not
    mod_file = os.path.join(out_dir, f"{name}.mod")
    long_names = [f"{r}.{e}" for r, e, _ in packed if len(r) > 16]
    seen_names = set(); dupes = []
    for r, e, _ in packed:
        key = f"{r}.{e}".lower()
        if key in seen_names:
            dupes.append(key)
        seen_names.add(key)
    if long_names or dupes:
        log["warnings"].append("No .mod written: " + (f"names over 16 chars: {long_names[:5]} " if long_names else "") +
                               (f"same name in several subfolders: {dupes[:5]}" if dupes else ""))
        mod_file = None
    else:
        pg.step("pack", f"{len(packed):,} files into {os.path.basename(mod_file)}")
        # the module description shown on the game's Load Module screen lives in the ERF header's localised strings
        # (BioWare ERF doc), not in a file, so it is carried over from the original .mod
        loc_count, loc_bytes = n.erf_description_block(module_path) if erf else (0, b"")
        n.write_erf_stream(mod_file, [(r, e, len(d), (lambda d=d: d)) for r, e, d in packed], "MOD ", loc_count,
                           loc_bytes, strref=erf.strref if erf else 0xFFFFFFFF)
        # the .mod's hash, so "Add to game folders" (nwn_install) can tell it is still exactly what this build wrote
        log["mod_sha256"] = n.sha256_file(mod_file)
    skipped = json.loads(dict(db.execute("SELECT key, value FROM meta").fetchall()).get("skipped_files") or "[]")
    if skipped:
        log["warnings"].append(f"{len(skipped)} non-game file(s) in the original were not copied or packed: " +
                               ", ".join(skipped[:8]) + (" …" if len(skipped) > 8 else ""))
    log["mod_file"] = mod_file
    # sizes: what this build saved
    module_in = sum(os.path.getsize(os.path.join(module_path, r)) if not erf else erf_entries[r].size for r, *_ in files)
    module_out = sum(len(d_) for _, _, d_ in packed)
    log["sizes"] = dict(module_in=module_in, module_out=module_out)
    if lean_man:
        man = lean_man
        log["lean_haks"] = {k: {kk: vv for kk, vv in v.items() if kk != "dropped_files"} for k, v in man["haks"].items()}
        log["lean_haks_mode"] = man["mode"]
        rebuilt = [v for v in man["haks"].values() if not v.get("unchanged")]
        # how many haks were rebuilt (new name, in build/haks) vs left as they are, and what the rebuilt ones saved
        log["lean_haks_summary"] = dict(rebuilt=len(rebuilt), unchanged=len(man["haks"]) - len(rebuilt),
                                        bytes_saved=sum(v["bytes_in"] - v["bytes_out"] for v in rebuilt))
        log["lean_dropped"] = {k: v["dropped_files"][:300] for k, v in man["haks"].items()}
        log["sizes"].update(haks_in=man["bytes_in"], haks_out=man["bytes_out"])
    log["sizes"]["total_in"] = log["sizes"]["module_in"] + log["sizes"].get("haks_in", 0)
    log["sizes"]["total_out"] = log["sizes"]["module_out"] + log["sizes"].get("haks_out", 0)
    if tlk_dest:
        # a copy of the module's custom talk table travels with the build (the original is only read; the
        # destination was checked by check_tlk_target before anything was written)
        if tlk_copy:
            try:
                shutil.copy2(meta_["tlk"], tlk_dest)
            except shutil.SameFileError:   # the folder changed under us to the tlk's own: nothing to copy
                pass
        log["tlk_file"] = tlk_dest
    log["field_changes"] = rw.changes
    log["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    log["summary"] = dict(files_in=len(files), files_out=len(packed), deleted=len(log["deleted"]),
                          merged=len(log["merged"]), edited=len(log["edited"]), added=len(log["added"]),
                          gff_files_rewritten=len(log["rewritten"]),
                          field_changes=len(rw.changes), refused=len(log["refused"]),
                          palette_moves=sum(1 for m in move_log if m["status"] == "moved"),
                          blueprint_changes=sum(1 for c in bc_log if c["status"] == "changed"),
                          bytes_saved=sum(os.path.getsize(os.path.join(module_path, d["file"]))
                                          for d in log["deleted"]) if not erf else None)
    if plan.get("git", True) and not use_git:
        # said in changes.md and on the Build page (via the audit's "git"), so a missing history is never a surprise
        log["git"] = dict(hint="not installed - no version history (optional, git-scm.com)")
    write_changes_md(os.path.join(out_dir, "changes.md"), log)
    # step 9: one git commit per build; the change log is the commit message
    if use_git:
        pg.step("git")
        _git(target, "add", "-A")           # -A also records files this build removed
        s_ = log["summary"]
        msg = (f"Clean build: -{s_['deleted']} files, {s_['merged']} merges, {s_['field_changes']} reference edits\n\n" +
               open(os.path.join(out_dir, "changes.md"), encoding="utf-8").read()[:60000])
        # the message goes through a file: Windows refuses command lines over ~32,000 characters (WinError 206)
        # absolute: git runs in the clean folder (-C), so a relative path (an analysis given as "tfn") would be
        # looked for there and the commit fails
        msg_file = os.path.abspath(os.path.join(out_dir, ".commit_message.txt"))
        with open(msg_file, "w", encoding="utf-8") as fh:
            fh.write(msg)
        try:
            # --allow-empty: a rebuild with no change still gets a commit, so every build has a history entry
            _git(target, "commit", "-q", "--allow-empty", "-F", msg_file)
        finally:
            os.remove(msg_file)
        head = _git(target, "rev-parse", "--short", "HEAD").strip()
        log["git"] = dict(repo=target, commit=head,
                          hint=f"git -C \"{target}\" diff original HEAD --stat   (full history: git log)")
    with open(os.path.join(out_dir, "changes.json"), "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=1, ensure_ascii=False)
    if verbose:
        print(f"  clean module: {target}\n  .mod: {mod_file}\n  {summary_sentence(log['summary'])} "
              f"({time.time() - t0:.1f}s)", flush=True)
        if log.get("lean_haks_summary"):
            print("  " + lean_sentence(log["lean_haks_summary"]), flush=True)
    # step 10: the deterministic audit re-analyses the clean module and compares it with the original
    if run_audit:
        if stop():
            raise InterruptedError("stopped before the audit - the build is NOT audited; build again before using it")
        import nwn_audit
        log["audit"] = nwn_audit.audit(analysis_dir, out_dir, name, verbose=verbose, **(audit_kwargs or {}))
    db.close()
    return log


# an NWScript file can run on its own only if it defines an entry point: "void main(" (scripts) or
# "int StartingConditional(" (conversation conditions). A file with neither is an include.
_RUNNABLE = re.compile(r"\bvoid\s+main\s*\(|\bint\s+StartingConditional\s*\(")


def _no_comments(src):
    """Source with // and /* */ comments blanked out, so a commented-out main() doesn't count. The regex matches a
    string literal ("a // b" - kept as it is), a // line comment or a /* block */ comment (each replaced by a space)."""
    return re.sub(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', lambda m: m.group(0) if m.group(0)[0] == '"' else " ", src, flags=re.S)


def _edited_includes(analysis_dir, edits):
    """Edited/new .nss files that are includes (no main / StartingConditional). Returns a set of script names
    (without .nss). edits: nwn_edit.list_edits() result. Read-only; an unreadable edit is skipped."""
    out = set()
    for fn, info in (edits or {}).items():
        if info.get("kind") != "edited" or not fn.endswith(".nss"):
            continue
        try:
            src = n.decode_text(open(os.path.join(analysis_dir, "edits", fn), "rb").read())
        except OSError:
            continue
        if not _RUNNABLE.search(_no_comments(src)):
            out.add(fn[:-4])
    return out


def _include_users(db, incs):
    """Runnable module scripts that include any of `incs`, directly or through other includes.

    db: the analysis index (sqlite3.Connection); incs: include names. Returns a sorted list of script names.
    NWScript copies an include into every script at compile time, so each of these must be recompiled."""
    # reverse include graph: include name -> scripts that #include it; then walk it upwards from the edited includes
    rev = defaultdict(set)
    for src, dst in db.execute("SELECT src, dst FROM edges WHERE kind='include'"):
        if src.startswith("script:") and dst.startswith("script:"):
            rev[dst[7:].lower()].add(src[7:].lower())
    seen, todo = set(), list(incs)
    while todo:
        x = todo.pop()
        for u in rev.get(x, ()):
            if u not in seen and u not in incs:
                seen.add(u)
                todo.append(u)
    runnable = {r[0].lower() for r in db.execute("SELECT name FROM scripts WHERE has_main=1 OR has_sc=1")}
    return sorted(seen & runnable)


def _compile_sources(db, target, own):
    """A temp folder holding the .nss sources as the toolset's compiler would see them, for compiling the build's
    edited scripts. Returns its path; the caller deletes it.

    The compiler (nwn_script_comp) looks for an #include in the compiled script's own folder before any --dirs
    folder, so a hak's include copy can't simply be passed alongside the clean folder: the module's copy would always
    win. Instead every script of the clean folder (target) is copied in, and then each hak's copy is written over it
    in the game's order (nwnlib.source_rank: the first-listed hak wins, and haks beat the module). The user override
    folder is never used: it ships nowhere, so code compiled against it would differ from what the toolset makes.
    own: names (lower case) of the scripts being compiled - their clean-folder copy is kept, it is what was edited.
    db: the analysis index; sources come from it, so the haks themselves are not opened. Writes only the temp folder."""
    import tempfile
    d = tempfile.mkdtemp(prefix="nwn_build_src_")
    for f in os.listdir(target):
        if f.lower().endswith(".nss"):
            # lower-case names: a hak copy below must replace this file, not sit next to it on a case-sensitive disk
            shutil.copyfile(os.path.join(target, f), os.path.join(d, f.lower()))
    rows = db.execute("SELECT s.name, s.source FROM scripts s JOIN files f ON f.id=s.file_id JOIN sources so "
                      "ON so.id=f.source_id WHERE so.kind = 'hak' AND s.source IS NOT NULL "
                      f"ORDER BY {n.source_rank_sql('so.kind', 'so.priority')}, so.id").fetchall()
    written = set()
    for nm, src in rows:
        nm = nm.lower()
        if nm in written or nm in own or not n.safe_resref(nm):
            continue                                  # first row wins: the copy the game would load
        written.add(nm)
        with open(os.path.join(d, f"{nm}.nss"), "wb") as fh:
            fh.write(n.encode_text(src))
    return d


def _editor_haks(packed, db, log):
    """Compare the module.ifo being packed with the analysed haks. A listed hak that isn't one of them but is the
    rebuild of one (<hak>_rN in the same folder) replaces it. Returns ({old_name: new_path}, {old names replaced}).

    packed: the (resref, ext, bytes) list being packed; db: the analysis index; log: the change log (warnings
    are added for listed haks the analysis didn't read). Old names are lower case. Writes no files."""
    ifo = next((d_ for r_, e_, d_ in packed if (r_, e_) == ("module", "ifo")), None)
    if ifo is None:
        return {}, set()
    try:
        root = n.read_gff(ifo)
    except n.GffError:
        return {}, set()
    listed = [st_.get("Mod_Hak") for st_ in root.get("Mod_HakList") or [] if isinstance(st_.get("Mod_Hak"), str)]
    analysed = json.loads(dict(db.execute("SELECT key, value FROM meta").fetchall()).get("haks") or "[]")
    by_name = {os.path.splitext(os.path.basename(os.path.normpath(h)))[0].lower(): h for h in analysed}
    out = {}
    for nm in listed:
        low = nm.lower()
        if low in by_name:
            continue
        base = re.sub(r"_r\d+$", "", low)          # "acpv41_r2" -> "acpv41" (the naming of nwn_hakslim.new_hak_names)
        orig = by_name.get(base)
        if not orig:
            log["warnings"].append(f"module.ifo lists {nm}.hak, which this analysis didn't read - re-analyse with it")
            continue
        cand = os.path.join(os.path.dirname(orig), nm + ".hak")
        if os.path.isfile(cand):
            out[base] = cand
        else:
            log["warnings"].append(f"module.ifo lists {nm}.hak, but it isn't next to {os.path.basename(orig)} - the audit "
                                   "checks the clean module against the original hak instead")
    return out, set(out)


def _point_module_at_haks(packed, target, renamed, rw, log):
    """module.ifo of the clean module lists the lean haks by their new names (Mod_HakList, and the old single Mod_Hak).

    packed: the (resref, ext, bytes) list; target: the clean folder (its module.ifo is rewritten too, so folder and
    .mod stay identical); renamed: {old hak name lower case: new name}; rw: the Rewriter, whose change list gets one
    record per re-pointed field (the audit's "modified" check requires that); log: the change log.
    Returns the new packed list. Writes only target/module.ifo."""
    out = []
    for r_, e_, d_ in packed:
        if (r_, e_) == ("module", "ifo"):
            root = n.read_gff(d_)
            # snapshot of every leaf field before the change, to log exactly the fields that differ afterwards
            before = {p_: v for p_, _, _, v, _ in n.iter_gff_leaves(root)}
            for st_ in root.get("Mod_HakList") or []:
                old = st_.get("Mod_Hak")
                if isinstance(old, str) and old.lower() in renamed:
                    st_.set("Mod_Hak", st_.type_of("Mod_Hak"), renamed[old.lower()])
            old = root.get("Mod_Hak")       # the older single-hak field, still present in some modules
            if isinstance(old, str) and old.lower() in renamed:
                root.set("Mod_Hak", root.type_of("Mod_Hak"), renamed[old.lower()])
            for p_, _, _, v, _ in n.iter_gff_leaves(root):
                if before.get(p_) != v:
                    rw.changes.append(dict(file="module.ifo", field=p_, old=before.get(p_), new=v,
                                           why="the rebuilt (lean) hak has a new name, so it can't replace the original"))
            d_ = n.write_gff(root)
            with open(_safe_dest(target, "module.ifo"), "wb") as fh:
                fh.write(d_)
            if "module.ifo" not in log["rewritten"]:
                log["rewritten"].append("module.ifo")
        out.append((r_, e_, d_))
    return out


def _remove_file(p):
    """os.remove that also works on read-only files (Windows)."""
    try:
        os.remove(p)
    except PermissionError:
        import stat
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        os.remove(p)


def _safe_dest(target, relpath):
    """Absolute path of relpath inside target. Raises ValueError if it would land outside target (a name with
    "..", an absolute path or a link), so a crafted file name in a module can't make the build write elsewhere."""
    dest = os.path.realpath(os.path.join(target, relpath))
    if not dest.startswith(os.path.realpath(target) + os.sep):
        raise ValueError(f"refusing to write outside the build folder: {relpath!r}")
    return dest


def _git(repo, *args):
    """Run one git command in repo and return its output; raises RuntimeError if git reports a failure.

    The command is an argument list with no shell, so paths and messages are never interpreted by a shell. The -c
    options set a fixed author and turn off commit signing for this call only, so commits work on a PC where git
    has no user configured and never stop to ask for a signing key; the user's git settings are not changed."""
    import subprocess
    r = subprocess.run(["git", "-c", "user.name=NWN Toolkit", "-c", "user.email=toolkit@localhost",
                        "-c", "commit.gpgsign=false", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def summary_sentence(s):
    """The build's summary dict as one plain sentence, e.g. "74 files in, 72 out: 2 deleted, 0 merged, 1 edited,
    0 added, references rewritten in 3 files (5 fields), 0 refused." Pure function."""
    return (f"{s['files_in']:,} files in, {s['files_out']:,} out: {s['deleted']} deleted, {s['merged']} merged, "
            f"{s['edited']} edited, {s['added']} added, references rewritten in {s['gff_files_rewritten']} files "
            f"({s['field_changes']} fields), {s['refused']} refused.")


def lean_sentence(ls):
    """The lean-hak counts (log["lean_haks_summary"]) as one plain sentence, e.g. "Lean haks: 3 rebuilt (new names,
    in build/haks; 12.40 MB saved), 97 left as they are (nothing to drop - not rebuilt, not renamed)." Pure function."""
    return (f"Lean haks: {ls['rebuilt']} rebuilt (new names, in build/haks; {ls['bytes_saved'] / 1e6:.2f} MB saved), "
            f"{ls['unchanged']} left as they are (nothing to drop - not rebuilt, not renamed).")


def write_changes_md(path, log):
    """Write the human-readable change log (changes.md) from the build's log dict: summary, removed files, sizes,
    new hak names, merges, edited/new files, every rewritten field, refusals and warnings. Writes only `path`."""
    L = [f"# Clean build change log", "", f"- Original: `{log['module']}`", f"- Output: `{log['output']}`",
         f"- .mod: `{log.get('mod_file')}`", f"- Built: {log['finished']}"] + \
        ([f"- Git: {log['git']['hint']}"] if (log.get("git") or {}).get("hint") else []) + ["", "## Summary", ""]
    L += [f"- {k.replace('_', ' ')}: {v}" for k, v in log["summary"].items()]
    L += ["", "## Removed files", "", "| File | Why | Status |", "|---|---|---|"]
    L += [f"| {d['file']} | {d['reason']} | {d['status']} |" for d in log["deleted"]]
    if log.get("sizes"):
        sz = log["sizes"]
        L += ["", "## Size", "", f"- module: {sz['module_in'] / 1e6:.2f} MB → {sz['module_out'] / 1e6:.2f} MB"]
        if "haks_in" in sz:
            L += [f"- haks: {sz['haks_in'] / 1e6:.2f} MB → {sz['haks_out'] / 1e6:.2f} MB (lean haks in build/haks/, mode: {log.get('lean_haks_mode')})"]
            if log.get("lean_haks_summary"):
                L += [f"- {lean_sentence(log['lean_haks_summary'])}"]
            for hk, lst in (log.get("lean_dropped") or {}).items():
                L += [f"  - {hk}: {len(lst)} file(s) dropped"] + [f"    - {d['file']} ({d['bytes']} B): {d['why']}" for d in lst[:100]]
            # files the plan kept but no ERF can hold (name too long / unsafe / unknown type) are not in the lean hak
            for hk, info in (log.get("lean_haks") or {}).items():
                if info.get("unpackable"):
                    L += [f"  - {hk}: {info['unpackable']} kept file(s) could not be packed (name over 16 characters, "
                          "unsafe or an unknown type): " + ", ".join(info.get("unpackable_files", [])[:20])]
        L += [f"- total download: {sz['total_in'] / 1e6:.2f} MB → {sz['total_out'] / 1e6:.2f} MB"]
    if log.get("renamed_haks"):
        L += ["", "## New names (nothing can overwrite an original)", "",
              f"- clean module: **{os.path.basename(log.get('mod_file') or '')}** (the original keeps its own name)",
              "- rebuilt haks have NEW names, and the clean module's hak list points at them. To use the build, copy "
              "the files in build/haks/ into your hak folder next to the originals - nothing is replaced:", ""] + \
             [f"  - {old}.hak → **{new}.hak**" for old, new in sorted(log["renamed_haks"].items())]
    if log.get("unchanged_haks"):
        L += ["", "## Haks left as they are", "",
              "Nothing to drop, so these were not rebuilt or renamed: the clean module loads the originals you already "
              "have.", ""] + [f"- {h_}.hak" for h_ in log["unchanged_haks"]]
    L += ["", "## Merged duplicates", ""] + [f"- {m['removed']} → {m['keeper']} ({m['category']})" for m in log["merged"]]
    if log.get("edited") or log.get("added"):
        L += ["", "## Edited / new files (from the editor)", ""] + [f"- edited: {f}" for f in log.get("edited", [])] + \
             [f"- new: {f}" for f in log.get("added", [])]
    if log.get("palette_moves"):
        L += ["", "## Palette moves", "", "| Type | Blueprint | To | Result |", "|---|---|---|---|"]
        L += [f"| {TYPE_LABEL.get(m['type'], m['type'])} | {m['resref']} | {m.get('to_path') or m.get('to')} | "
              f"{m['status']}{': ' + m['note'] if m.get('note') else ''} |" for m in log["palette_moves"]]
    if log.get("blueprint_changes"):
        L += ["", "## Blueprint changes", "", "| Type | Blueprint | Field | From | To | Placed copies | Result |",
              "|---|---|---|---|---|---|---|"]
        L += [f"| {TYPE_LABEL.get(c['type'], c['type'])} | {c['resref']} | {c['field']} | {c['from']} | {c['to']} | "
              f"{c.get('placed', 0)} | {c['status']}{': ' + c['note'] if c.get('note') else ''} |"
              for c in log["blueprint_changes"]]
    L += ["", "## Rewritten references", "", "| File | Field | Old | New | Why |", "|---|---|---|---|---|"]
    L += [f"| {c['file']} | {c['field']} | {c['old']} | {c['new']} | {c['why']} |" for c in log["field_changes"]]
    if log["refused"]:
        L += ["", "## Refused (not changed)", ""] + [f"- {r['node']}: {r['why']}" for r in log["refused"]]
    if log["warnings"]:
        L += ["", "## Warnings", ""] + [f"- {w}" for w in log["warnings"]]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def main(argv=None):
    """Command line: build (and audit, unless --no-audit) from an analysis folder and a plan.json.

    argv: argument strings (None = sys.argv[1:]). Returns the exit code: 0 when the build ran, 2 (with one plain
    line) when the analysis folder or the plan file is missing or the plan is not readable JSON. --help exits 0."""
    # Checked before anything else, so an old Python gets one plain sentence instead of a traceback further on.
    if sys.version_info < (3, 8):
        print("The NWN Module Toolkit needs Python 3.8 or newer (this is %d.%d) - install it from python.org"
              % sys.version_info[:2])
        return 2
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis", help="the analysis folder of the original module (nwn_workspace/<module>)")
    ap.add_argument("plan", help="the plan file (JSON, see above; the dashboard writes one)")
    ap.add_argument("--out", help="output folder (default: <analysis>/build)")
    ap.add_argument("--name", help="name of the clean module (default: <module>_clean; never the original's name)")
    ap.add_argument("--no-audit", action="store_true", help="skip the deterministic audit at the end (run nwn_audit.py "
                    "yourself before using the build)")
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.analysis, "report.json")):
        print(f"No analysis found in {a.analysis} (it has no report.json) - give the analysis folder, e.g. "
              "nwn_workspace/<module>, after analysing the module")
        return 2
    try:
        with open(a.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    except OSError as ex:
        print(f"The plan file could not be read: {a.plan} ({ex.strerror or ex})")
        return 2
    except ValueError as ex:
        print(f"The plan file is not valid JSON: {a.plan} ({ex})")
        return 2
    build(a.analysis, plan, a.out, a.name, run_audit=not a.no_audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
