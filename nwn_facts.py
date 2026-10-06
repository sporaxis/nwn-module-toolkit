"""
nwn_facts.py - verified facts about an analysed module, read-only.

Answers like "that script is only used by X", "that item exists", "that compiles" should never be guesses. Every
answer here comes from the analysis index (index.sqlite, opened READ-ONLY) and says where it came from.

    python nwn_facts.py                                   list analyses
    python nwn_facts.py MyModule summary
    python nwn_facts.py MyModule exists my_quest_done      is there a script / blueprint / tag / variable ... by that name?
    python nwn_facts.py MyModule who-uses script:my_quest_done
    python nwn_facts.py MyModule uses script:my_quest_done
    python nwn_facts.py MyModule script my_quest_done [first-last]
    python nwn_facts.py MyModule grep "SetLocalInt(.*QUEST_"
    python nwn_facts.py MyModule variable nBeenPaidThisMonth
    python nwn_facts.py MyModule tag GoblinChiefHead
    python nwn_facts.py MyModule quest QUEST_GOBLINS
    python nwn_facts.py MyModule impact script:my_quest_inc
    python nwn_facts.py MyModule fields quest_giver.utc [path-prefix]
    python nwn_facts.py MyModule issues [category]
    python nwn_facts.py MyModule compiled my_quest_done
    python nwn_facts.py MyModule faction [name]
    python nwn_facts.py MyModule settings
    python nwn_facts.py MyModule database [name]
    add --json for machine-readable output.

What each command returns as evidence (all from the last analysis - re-analyse after changing the module)
    summary     module name and path, when it was indexed, sources (module, haks...), counts, issue/impact/quest totals
    exists      every match by name: resource (file, type, module or hak), tag (+ how many objects), variable,
                quest, conversation, script constant (with the script that defines it); names referenced but missing
    who-uses    every edge pointing AT the thing: user node, how it is used (event_script, include, tag_ref ...),
                and `via` (the field path or script that makes the link)
    uses        every edge going OUT of the thing (what it uses), same shape (total / truncated too)
    script      the source with line numbers (400 lines at a time), its role, includes, functions, has a .ncs?;
                when a hak also has the script, the copy the game runs is shown and `precedence` says so
    grep        script name, line number and line text of every regex match in every script's source
    variable    the variable audit rows (system, type, where set, where read, findings) plus the graph's users
    tag         every object with that exact tag (blueprint or placed, area), lookups by scripts, capital twins
    quest       journal quests and quests inferred from scripts that match the text, and their families
    impact      the impact level and its reasons (what would break) for a node
    fields      every GFF field of one file: path, type, value (optionally under a path prefix); of the copy the game
                uses when a hak has the file too (`precedence`, `other_copies`)
    issues      the analysis's issues with their `fix`; accepted_by_design marks ones the builder accepted
    compiled    the last compile result for a script (errors, or ok), whether the module has its .ncs, edited?
    faction     reputations to players and other factions, members, scripts that change factions
    settings    module.ifo settings, events, module variables, and the loaded server settings (secrets hidden)
    database    campaign database use by scripts, checked against the loaded database files, with findings

Nothing here writes anything. Analysis names are checked against a strict pattern and resolved inside the toolkit
workspace (the command line may also give an explicit analysis folder). Long lists are capped (LIMIT); who-uses,
grep and fields say when they were cut short. Names built at run time ("x_" + n) are not in the dependency graph,
so who-uses can't list them.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nwnlib as n  # noqa: E402
import nwn_sheets  # noqa: E402
import nwn_update  # noqa: E402

WORKSPACE = nwn_update.workspace_dir(HERE)     # NWN_WORKSPACE, an install's workspace, or ./nwn_workspace
# An analysis name: one folder name, no path separators or dots, so a name can never point outside the workspace
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
LIMIT = 200      # default cap on list results, so an answer stays readable
_REPORTS = {}    # report.json path -> (mtime, data): reused between calls, re-read when it changes
# Dependency-graph node id prefixes (nwn_index.py): "script:foo", "bp:foo.uti", "tag:FOO", "var:nDone" ...
NODE_PREFIXES = ("script:", "bp:", "tag:", "var:", "dlg:", "area:", "quest:", "2da:", "asset:", "model:", "inst:",
                 "hak:", "module", "file:")


def list_analyses(workspace=WORKSPACE):
    """Every analysis folder in the workspace: [dict(name, status)]. Read-only; [] when there is no workspace.

    status: "ready" (index + report), "indexed, not analysed", "incomplete" (a stopped run left .incomplete), or
    "archived ..." (only archive.json is left - unpack it first). Folders with unsafe names are skipped."""
    out = []
    if not os.path.isdir(workspace):
        return out
    for nm in sorted(os.listdir(workspace)):
        d = os.path.join(workspace, nm)
        if not NAME_RE.match(nm) or not os.path.isfile(os.path.join(d, "index.sqlite")):
            if NAME_RE.match(nm) and os.path.isfile(os.path.join(d, "archive.json")):
                out.append(dict(name=nm, status="archived (unpack it in the dashboard to query it)"))
            continue
        status = "incomplete" if os.path.exists(os.path.join(d, ".incomplete")) else \
            ("ready" if os.path.isfile(os.path.join(d, "report.json")) else "indexed, not analysed")
        out.append(dict(name=nm, status=status))
    return out


def resolve_analysis(name_or_dir, workspace=WORKSPACE, allow_outside=False):
    """An analysis folder from a name in the workspace (or an explicit folder that holds an index.sqlite: inside the
    workspace, or anywhere when allow_outside - the command line; other callers keep to the workspace).

    A name is matched exactly, then case-insensitively. Returns the folder path; raises ValueError (with the list of
    known analyses) when there is no such analysis. The realpath check stops a symlink or "..\\" in an absolute
    path from reaching outside the workspace."""
    if not isinstance(name_or_dir, str):
        raise ValueError("the analysis must be given as text")
    if name_or_dir and os.path.isabs(name_or_dir) and os.path.isfile(os.path.join(name_or_dir, "index.sqlite")):
        real, ws = os.path.realpath(name_or_dir), os.path.realpath(workspace)
        if allow_outside or real.startswith(ws + os.sep):
            return name_or_dir
        raise ValueError("that folder is outside the toolkit workspace - give the analysis name instead")
    if not name_or_dir or not NAME_RE.match(name_or_dir):
        raise ValueError("give an analysis name (letters, digits, _ and -) - see list_analyses")
    d = os.path.join(workspace, name_or_dir)
    if not os.path.isfile(os.path.join(d, "index.sqlite")):
        names = [a["name"] for a in list_analyses(workspace)]
        match = [x for x in names if x.lower() == name_or_dir.lower()]
        if match:
            return os.path.join(workspace, match[0])
        raise ValueError(f"no analysis called {name_or_dir!r} (have: {', '.join(names) or 'none'})")
    return d


def _like(s):
    """Escape SQL LIKE wildcards (% and _) so `s` matches literally; use with ESCAPE '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _bare_script(low):
    """"script:foo.nss" -> "foo" (slicing, not str.removeprefix: the toolkit runs on Python 3.8)."""
    low = low[7:] if low.startswith("script:") else low
    return low[:-4] if low.endswith(".nss") else low


def _script_name(name):
    """A script name normalised to its bare lower-case resref ("script:Foo.nss" -> "foo"); ValueError if invalid."""
    if not isinstance(name, str):
        raise ValueError("give the script name as text")
    low = _bare_script(name.strip().lower())
    if not SCRIPT_NAME_RE.match(low):
        raise ValueError(f"{name!r} is not a script name (letters, digits and _ only)")
    return low


def _grep_worker(db_path, pattern, limit, q):
    """Runs in a child process so a pathological pattern can be stopped (Python can't interrupt a regex).

    Puts exactly one dict on queue `q`: dict(matches, truncated) or dict(error). Opens the index read-only."""
    import sqlite3 as _s
    try:
        rx = re.compile(pattern)
        db = _s.connect(f"file:{db_path}?mode=ro", uri=True)
        out = []
        for nm, src in db.execute("SELECT name, source FROM scripts WHERE source IS NOT NULL"):
            for i, line in enumerate(src.replace("\r\n", "\n").split("\n"), 1):
                if rx.search(line):
                    out.append(dict(script=nm, line=i, text=line.strip()[:200]))
                    if len(out) >= limit:
                        q.put(dict(matches=out, truncated=True))
                        return
        q.put(dict(matches=out, truncated=False))
    except Exception as ex:  # noqa
        q.put(dict(error=str(ex)))


GREP_TIMEOUT = 20      # seconds before a grep is stopped (a regex with catastrophic backtracking can run for ever)


def _rank(kind, priority):
    """Load order of one copy of a resource, lowest wins (nwnlib.source_rank: haks in module.ifo order > module >
    user override)."""
    return n.source_rank(kind, priority)


def _precedence(winner, others):
    """One sentence saying which copy was shown and why, for script() and fields(): winner / others are
    dicts(source, source_path); "" when there is only one copy."""
    if not others:
        return ""
    return (f"the {winner['source']} copy ({os.path.basename(winner['source_path'] or '')}) wins: the game prefers haks "
            f"(first listed first) > module > override folder, so it is the one shown; the other copies are listed in "
            f"other_copies")


class Facts:
    """Read-only questions about one analysis. Each public method returns a JSON-ready dict.

    Holds one read-only connection to the analysis's index.sqlite (self.db) and the parsed report.json. Bad input
    raises ValueError (the command line prints it as "error: ..."). Nothing here writes. Call close() when done."""

    def __init__(self, analysis, workspace=WORKSPACE, allow_outside=False):
        """analysis: an analysis name (or folder, see resolve_analysis). allow_outside: True only for the command
        line, where the user may point at an analysis folder outside the workspace."""
        self.dir = resolve_analysis(analysis, workspace, allow_outside)
        self.name = os.path.basename(self.dir.rstrip("/\\"))
        import nwnlib
        self.db = nwnlib.sqlite_ro(os.path.join(self.dir, "index.sqlite"), check_same_thread=False)   # writes raise
        self._report = None

    def close(self):
        """Close the index connection."""
        self.db.close()

    # -- helpers
    def report(self):
        """The analysis's report.json as a dict ({} when there is none yet). Loaded once per Facts object; the
        module-level cache keeps one parsed report and re-reads it when the file's modification time changes."""
        if self._report is None:
            p = os.path.join(self.dir, "report.json")
            if not os.path.isfile(p):
                self._report = {}
            else:
                mt = os.path.getmtime(p)
                hit = _REPORTS.get(p)
                if not hit or hit[0] != mt:            # re-read after a re-analysis
                    with open(p, encoding="utf-8") as fh:
                        hit = (mt, json.load(fh))
                    _REPORTS.clear()                   # keep one report in memory
                    _REPORTS[p] = hit
                self._report = hit[1]
        return self._report

    def label(self, node):
        """The readable label the index stores for a graph node, or the node id itself when it has none."""
        r = self.db.execute("SELECT label FROM nodes WHERE node=?", (node,)).fetchone()
        return r[0] if r and r[0] else node

    def _nodes_for(self, name):
        """A node id as given ('script:foo'), or every node whose name matches.

        Name matching is exact or lower-case (up to 50 nodes: one name can be a script, a tag and a variable at once);
        a name with an extension ("goblin.utc") is also tried as bp:/asset: nodes."""
        if name.startswith(NODE_PREFIXES) and self.db.execute("SELECT 1 FROM nodes WHERE node=?", (name,)).fetchone():
            return [name]
        low = name.lower()
        rows = self.db.execute("SELECT node FROM nodes WHERE name=? OR lower(name)=? OR node=? LIMIT 50",
                               (name, low, name)).fetchall()
        if not rows and "." in name:
            rows = self.db.execute("SELECT node FROM nodes WHERE node=? OR node=?",
                                   (f"bp:{low}", f"asset:{low}")).fetchall()
        return [r[0] for r in rows]

    # -- facts
    def summary(self):
        """Overview of the analysis: module name/path, indexed_at, sources in load priority order, row counts
        (files, scripts, objects, conversations), issue and impact totals, quest counts and families."""
        meta = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        s = (self.report().get("summary") or {})
        return dict(analysis=self.name, module=meta.get("module_name") or s.get("module_name"),
                    module_path=meta.get("module_path"), indexed_at=meta.get("indexed_at"),
                    sources=[dict(kind=k, path=p) for p, k in self.db.execute("SELECT path, kind FROM sources ORDER BY priority")],
                    counts={t: self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                            for t in ("files", "scripts", "objects", "conversations")},
                    issues=s.get("issues"), impact_levels=s.get("impact_levels"),
                    quests=dict(journal=s.get("quests", 0), from_scripts=s.get("inferred_quests", 0),
                                total=(s.get("quests") or 0) + (s.get("inferred_quests") or 0),
                                real_quests=s.get("inferred_real_quests"),
                                note="real_quests = quests tracked in scripts (tokens, player variables) plus journal quests the "
                                     "scripts use; the rest of 'from_scripts' is settings/state of systems like DMFI",
                                system_and_package_state=s.get("inferred_system_state"),
                                families=[dict(name=f["name"], kind=f["kind"], quests=f["quests"])
                                          for f in (self.report().get("inferred_quests") or {}).get("families", [])]),
                    note="Facts are as of the analysis time above; re-analyse after changing the module.")

    def exists(self, name):
        """Is there anything by this name: a resource (any type), script, blueprint, tag, variable, quest, constant?

        name: "foo" (any type) or "foo.uti" (that type only). Returns dict(name, found, matches,
        referenced_but_missing, case_differs, note). Resource names are compared case-insensitively, tags and
        variables exactly (as the game does); case_differs lists tags that differ only in capitals.
        Base-game files count only when the analysis indexed the game install (the note says when it did not)."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("give a name to look for")
        name = name.strip()
        low = name.lower()
        base, _, ext = low.rpartition(".") if "." in low else (low, "", "")
        out = []
        q = "SELECT f.relpath, f.ext, s.kind, s.path FROM files f JOIN sources s ON s.id=f.source_id WHERE f.resref=?"
        args = [base or low]
        if ext:
            q += " AND f.ext=?"
            args.append(ext)
        for rel, e, kind, spath in self.db.execute(q + " LIMIT 50", args):
            out.append(dict(kind="resource", name=rel, type=e, source=kind, source_path=spath))
        base_known = bool(self.db.execute("SELECT 1 FROM base_resources LIMIT 1").fetchone())
        if not out and base_known and self.db.execute("SELECT 1 FROM base_resources WHERE name=? OR name LIKE ? ESCAPE '\\'",
                                                      (low, _like(low) + ".%")).fetchone():
            out.append(dict(kind="resource", name=low, source="base game"))
        missing_refs = []
        for tag, n_ in self.db.execute("SELECT tag, count(*) FROM objects WHERE tag=? GROUP BY tag", (name,)):
            out.append(dict(kind="tag", name=tag, objects=n_))
        twins = [t for (t,) in self.db.execute("SELECT DISTINCT tag FROM objects WHERE lower(tag)=? AND tag<>? LIMIT 5",
                                               (low, name))]
        for nd, typ, nm, inmod in self.db.execute("SELECT node, type, name, in_module FROM nodes WHERE (node=? OR node=? OR node=?)",
                                                  (f"var:{name}", f"quest:{name}", f"dlg:{low}")):
            # a conversation/quest node with in_module=0 exists only because something refers to it
            if typ in ("conversation", "quest") and not inmod:
                missing_refs.append(dict(kind=typ, name=nm, node=nd, note="referenced by the module but NOT in it"))
            else:
                out.append(dict(kind=typ, name=nm, node=nd))
        # constants defined in scripts: const int QT_DONEHOBS = 51;
        if re.fullmatch(r"[A-Za-z_]\w*", name):
            # only when the name could be an identifier; the LIKE pre-filter avoids running the regex on every script
            crx = re.compile(r"\bconst\s+(int|string|float)\s+" + re.escape(name) + r"\s*=\s*([^;]+);")
            for nm_, src in self.db.execute("SELECT name, source FROM scripts WHERE source LIKE ? ESCAPE '\\'",
                                            ("%" + _like(name) + "%",)):
                m = crx.search(src or "")
                if m:
                    out.append(dict(kind="constant", name=name, type=m.group(1), value=m.group(2).strip(), defined_in=nm_))
        for q_ in (self.report().get("inferred_quests") or {}).get("quests", []):
            if name in q_.get("key_exprs", []) or q_.get("key") == name:
                out.append(dict(kind="inferred quest", name=q_["label"], id=q_["id"], health=q_["health"]))
        notes = []
        if twins:
            notes.append("tags and variable names are case-sensitive; resource names are not")
        if not base_known:
            notes.append("the base game was not indexed (no NWN install folder), so base-game resources can't be "
                         "confirmed - 'not found' does not rule them out")
        return dict(name=name, found=bool(out), matches=out, referenced_but_missing=missing_refs,
                    case_differs=twins, note="; ".join(notes))

    def who_uses(self, name, limit=LIMIT):
        """Everything that uses `name` (incoming graph edges). name: a node id ("script:foo") or a bare name.

        Returns dict(name, nodes, total, users=[dict(node, label, kind, via, target)], truncated, note), or
        found=False when no node matches. total counts every edge even when the list is cut at `limit`."""
        nodes = self._nodes_for(name)
        if not nodes:
            return dict(name=name, found=False, users=[], note="nothing by that name in the index")
        users, total = [], 0
        for nd in nodes:
            total += self.db.execute("SELECT count(*) FROM edges WHERE dst=?", (nd,)).fetchone()[0]
            for src, kind, via in self.db.execute("SELECT src, kind, via FROM edges WHERE dst=? LIMIT ?", (nd, limit)):
                users.append(dict(node=src, label=self.label(src), kind=kind, via=via, target=nd))
        return dict(name=name, nodes=nodes, total=total, users=users[:limit], truncated=total > len(users[:limit]),
                    note="'kind' is how it is used (event_script, dlg_script, include, execute_script, template, "
                         "inventory, tag_ref, var_read ...). Names built at run time are not included.")

    def uses(self, name, limit=LIMIT):
        """Everything `name` uses (outgoing graph edges): dict(name, nodes, uses=[dict(node, label, kind, via,
        source)], total, truncated, found). As in who_uses, total counts every edge even when the list is cut at
        `limit` (truncated=True then), so a reader can tell a complete list from a cut one."""
        nodes = self._nodes_for(name)
        out, total = [], 0
        for nd in nodes:
            total += self.db.execute("SELECT count(*) FROM edges WHERE src=?", (nd,)).fetchone()[0]
            for dst, kind, via in self.db.execute("SELECT dst, kind, via FROM edges WHERE src=? LIMIT ?", (nd, limit)):
                out.append(dict(node=dst, label=self.label(dst), kind=kind, via=via, source=nd))
        out = out[:limit]
        return dict(name=name, nodes=nodes, uses=out, total=total, truncated=total > len(out), found=bool(nodes))

    def script(self, name, first=None, last=None):
        """A script's source with line numbers, lines first..last (default: the first 400 from `first`).

        When the same script is in several sources, the copy the game runs is shown (the first-listed hak, then the
        module, then the override - nwnlib.source_rank): source_of says its source kind, precedence
        says why it won, and other_copies lists the copies not shown (source, source_path). role: event/action script
        (has main), conversation condition (has StartingConditional), or include library (neither). Shows the
        analysed source - not an edit made since. found=False when unknown."""
        if not isinstance(name, str):
            raise ValueError("give the script name as text")
        low = _bare_script(name.lower())
        rows = self.db.execute("SELECT s.name, s.source, s.has_main, s.has_sc, s.lines, s.has_ncs, s.functions_defined, "
                               "src.kind, src.path, src.priority FROM scripts s JOIN files f ON f.id=s.file_id "
                               "JOIN sources src ON src.id=f.source_id WHERE lower(s.name)=?", (low,)).fetchall()
        if not rows:
            return dict(name=low, found=False)
        rows.sort(key=lambda r: _rank(r[7], r[9]))
        nm, src, has_main, has_sc, lines, has_ncs, funcs, where, spath, _prio = rows[0]
        others = [dict(source=r[7], source_path=r[8]) for r in rows[1:]]
        text = (src or "").replace("\r\n", "\n").split("\n")
        a = max(1, int(first or 1))
        b = min(len(text), int(last or min(len(text), a + 399)))
        incs = [d[7:] for (d,) in self.db.execute("SELECT dst FROM edges WHERE src=? AND kind='include'", (f"script:{low}",))]
        return dict(name=nm, found=True, source_of=where, source_path=spath, other_copies=others,
                    precedence=_precedence(dict(source=where, source_path=spath), others),
                    role="event/action script" if has_main else ("conversation condition" if has_sc else "include library"),
                    lines=lines, compiled_ncs=bool(has_ncs), includes=incs,
                    functions_defined=[f for f in (funcs or "").split(",") if f],
                    shown=f"{a}-{b}", text="\n".join(f"{i:5d}  {text[i - 1]}" for i in range(a, b + 1)))

    def grep(self, pattern, limit=LIMIT, timeout=GREP_TIMEOUT):
        """Every line of every script source (module and haks) that matches the regular expression `pattern`.

        Returns dict(pattern, matches=[dict(script, line, text)], truncated). The search runs in a separate process
        that is killed after `timeout` seconds, because a badly written regex can run for ever and Python can't
        stop it from the same process. Raises ValueError for a bad or too-slow pattern."""
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("give a regular expression as text")
        try:
            re.compile(pattern)
        except re.error as ex:
            raise ValueError(f"bad pattern: {ex}")
        import multiprocessing as mp
        q = mp.Queue()
        p = mp.Process(target=_grep_worker, args=(os.path.join(self.dir, "index.sqlite"), pattern, limit, q), daemon=True)
        p.start()
        try:
            res = q.get(timeout=timeout)
        except Exception:  # noqa - queue.Empty: the pattern is too slow (catastrophic backtracking)
            p.terminate()
            p.join(2)
            raise ValueError(f"the pattern took longer than {timeout}s and was stopped - simplify it (avoid nested "
                             "repeats like (\\w+\\s*)+)")
        p.join(2)
        if "error" in res:
            raise ValueError(res["error"])
        return dict(pattern=pattern, **res)

    def variable(self, name):
        """A variable by name: dict(name, found, audit, note, readers). audit = the variable-audit rows from the
        report (one per system/type - NWN keeps int/string/... variables apart; names that differ only in capitals
        are listed too), readers = up to 50 graph users of var:<name>."""
        va = self.report().get("var_audit") or {}
        rows = [v for v in va.get("variables", []) if v["name"] == name or v["name"].lower() == name.lower()]
        edges = self.who_uses(f"var:{name}", 50)["users"] if self._nodes_for(f"var:{name}") else []
        return dict(name=name, found=bool(rows or edges), audit=rows,
                    note="each row is one variable: NWN keeps types apart and names are case-sensitive"
                    if len(rows) > 1 else "", readers=edges)

    def tag(self, tag):
        """Objects with exactly this tag (tags are case-sensitive): dict(tag, objects=[dict(node, cls, blueprint,
        area, name)], placed (how many are placed, not blueprints), lookups (the report's tag-lookup row: scripts
        that look it up), case_differs (tags that differ only in capitals))."""
        objs = [dict(node=nd, cls=c, blueprint=bool(b), area=a, name=nm) for nd, c, b, a, nm in self.db.execute(
            "SELECT node, class, is_blueprint, area, name FROM objects WHERE tag=? LIMIT ?", (tag, LIMIT))]
        va = self.report().get("var_audit") or {}
        look = next((t for t in va.get("tags", []) if t["tag"] == tag), None)
        twins = [t for (t,) in self.db.execute("SELECT DISTINCT tag FROM objects WHERE lower(tag)=? AND tag<>? LIMIT 5",
                                               (tag.lower(), tag))]
        return dict(tag=tag, objects=objs, placed=sum(1 for o in objs if not o["blueprint"]), lookups=look,
                    case_differs=twins)

    def quest(self, text):
        """Quests matching `text` (case-insensitive): dict(query, inferred, journal, families).

        inferred: up to 10 quests inferred from scripts (label contains the text, or key / id / key expression equal
        it, or a conversation label contains it); journal: up to 10 journal (.jrl) quests whose tag or name contains
        it; families: quest families whose name contains it, with their members."""
        low = text.lower()
        iq = (self.report().get("inferred_quests") or {}).get("quests", [])
        hits = [q for q in iq if low in q["label"].lower() or low == str(q["key"]).lower() or low == q["id"].lower()
                or any(low == e.lower() for e in q.get("key_exprs", []))
                or any(low in c.lower() for c in q.get("conversation_labels", []))][:10]
        jr = [q for q in self.report().get("quests", []) if low in (q.get("tag") or "").lower() or low in (q.get("name") or "").lower()][:10]
        fam = [dict(f, members=[dict(name=q.get("name"), label=q["label"], set=q.get("set"), health=q.get("health"))
                                for q in iq if q.get("family") == f["name"]][:300])
               for f in (self.report().get("inferred_quests") or {}).get("families", []) if low in f["name"].lower()]
        return dict(query=text, inferred=hits, journal=jr, families=fam)

    def settings(self):
        """The module's settings (module.ifo, events, module variables, switches the scripts set), a count of all
        variables, and the server settings you loaded (settings.tml / environment; passwords hidden)."""
        import nwn_modsettings
        p = os.path.join(self.dir, "server_config.json")
        configs = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else []
        rep = self.report()
        out = dict(module=nwn_modsettings.module_summary(self.db, rep),
                   server=nwn_modsettings.interpret(configs, self.db, rep.get("summary"), nwn_modsettings.unused_scripts(rep)) if configs else
                   dict(note="no server settings loaded (Overview > Server settings)"))
        out["module"]["script_sets"] = out["module"]["script_sets"][:80]
        return out

    def database(self, text=""):
        """Campaign databases: what the scripts store/read (database, variable, type, per character) checked against
        the database files you loaded (Database page), with findings and fixes. `text` narrows to a database or
        variable name."""
        import nwn_database
        import nwn_modsettings
        p = os.path.join(self.dir, "database.json")
        snap = json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}
        uses = nwn_database.script_uses(self.db)
        r = nwn_database.cross_check(snap, uses, nwn_modsettings.unused_scripts(self.report()),
                                     nwn_database.int_constants(self.db))
        low = (text or "").strip().lower()
        dbs = r["databases"]
        if low:
            hit = [d for d in dbs if low in d["name"].lower()]
            if not hit:
                hit = [dict(d, vars=[v for v in d["vars"] if low in v["name"].lower()]) for d in dbs]
                hit = [d for d in hit if d["vars"]]
            dbs = hit
        for d in dbs:
            d["vars"] = d["vars"][:200]
        return dict(summary=r["summary"], findings=[f for f in r["findings"] if not low or low in
                                                    (f["db"] + f["text"]).lower()][:100],
                    databases=dbs[:40], sql=r["sql"], files_loaded=bool(snap.get("files")),
                    note="" if snap.get("files") else "no database files loaded (Database page) - script side only")

    def faction(self, text=""):
        """Factions: how each feels about players and others, members, and scripts that change them."""
        fa = self.report().get("factions") or {}
        if not fa.get("factions"):
            return dict(found=False, note=(fa.get("summary") or {}).get("error") or "re-analyse to get factions")
        low = (text or "").strip().lower()
        fs = [f for f in fa["factions"] if not low or low in f["name"].lower()]
        names = {f["id"]: f["name"] for f in fa["factions"]}
        out = []
        for f in fs[:10]:
            out.append(dict(id=f["id"], name=f["name"], parent=f.get("parent_name"), global_reputation=f.get("glob"),
                            feels_about_players=f.get("to_pc"), attitude_to_players=f.get("attitude_to_pc"),
                            hostile_towards=[names[int(j)] for j, v in f["feels"].items() if v is not None and v <= 10
                                             and int(j) not in (f["id"], 0)],
                            friendly_towards=[names[int(j)] for j, v in f["feels"].items() if v is not None and v >= 90
                                              and int(j) not in (f["id"], 0)],
                            placed_creatures=f.get("creatures_placed"), blueprints=f.get("creature_blueprints"),
                            areas=f.get("areas"), holder_tags=f.get("holders", [])[:20],
                            scripts=[dict(script=x["script"], line=x["line"], call=x["call"], detail=x["detail"])
                                     for x in fa.get("scripts", []) if f["name"] in x["factions"]][:40]))
        return dict(found=bool(out), summary=fa.get("summary"), factions=out,
                    note="reputation 0-10 = hostile, 11-89 neutral, 90-100 friendly; 'feels' = how this faction treats the other")

    def impact(self, name):
        """How much depends on `name`: dict(name, impact=[dict(node, label, detail)]) for up to 10 matching nodes.

        detail is nwn_analysis.Graph.impact(node, detail=True) worked out now from the index's dependency graph, with
        the live set of the last analysis (its loaded server settings included), so the level is the one the report
        shows: dict(level, reasons, counts, quests, areas, affected, placed_copies, uses). Read-only; loads the whole
        graph for each call (seconds on a very large module)."""
        import nwn_analysis
        g = nwn_analysis.Graph(self.db)
        live = g.live(nwn_analysis.server_config_values(self.dir)[1])
        out = [dict(node=nd, label=self.label(nd), detail=g.impact(nd, detail=True, live=live))
               for nd in self._nodes_for(name)[:10]]
        return dict(name=name, impact=out)

    def fields(self, filename, prefix="", limit=LIMIT):
        """The GFF fields of one file as analysed: dict(file, fields=[dict(path, type, value)], truncated, found,
        source_of, source_path, other_copies, precedence).

        filename: e.g. "quest_giver.utc" (any capitals). prefix: only paths starting with it, e.g. "ItemList[0]".
        If a module and a hak both have the file, only the copy the game uses is listed (the first-listed hak, then the
        module, then the override - the same rule as script()); other_copies names the rest."""
        copies = self.db.execute("SELECT f.id, s.kind, s.path, s.priority FROM files f JOIN sources s ON s.id=f.source_id "
                                 "WHERE lower(f.relpath)=?", (filename.lower(),)).fetchall()
        if not copies:
            return dict(file=filename, fields=[], truncated=False, found=False)
        copies.sort(key=lambda c: _rank(c[1], c[3]))
        fid, kind, spath, _prio = copies[0]
        others = [dict(source=c[1], source_path=c[2]) for c in copies[1:]]
        rows = self.db.execute("SELECT path, type, value FROM fields WHERE file_id=? AND path LIKE ? ESCAPE '\\' LIMIT ?",
                               (fid, _like(prefix) + "%", limit + 1)).fetchall()
        return dict(file=filename, fields=[dict(path=p, type=t, value=v) for p, t, v in rows[:limit]],
                    truncated=len(rows) > limit, found=True, source_of=kind, source_path=spath, other_copies=others,
                    precedence=_precedence(dict(source=kind, source_path=spath), others))

    def accepted(self):
        """Issues the builder accepted as by design: {"category|node|label": dict(sig, note, ...)} from
        accepted_issues.json, or {} when there is none or it can't be parsed."""
        p = os.path.join(self.dir, "accepted_issues.json")
        try:
            return json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}
        except ValueError:
            return {}

    def issues(self, category=None, severity=None, name=None, limit=LIMIT):
        """The analysis's issues, each with its `fix`, filtered by category, severity and/or a name in the text.

        Returns dict(issues (first `limit`), total (after filtering), categories (count per category, unfiltered),
        note). An issue the builder accepted gets accepted_by_design=True and their note - but only while its
        detail still matches what they accepted, so a changed problem is raised again."""
        out = []
        acc = self.accepted()
        for i in self.report().get("issues", []):
            a = acc.get(f"{i.get('category')}|{i.get('node')}|{i.get('label')}")
            # the signature is the detail text with every number replaced by #, so a changed count ("3 scripts" ->
            # "4 scripts") still matches but different wording does not
            if a and nwn_sheets.sig_matches(a.get("sig"), i.get("detail")):
                i = dict(i, accepted_by_design=True, accepted_note=a.get("note", ""))
            if category and i.get("category") != category:
                continue
            if severity and i.get("severity") != severity:
                continue
            if name and name.lower() not in (i.get("node", "") + " " + i.get("label", "") + " " + i.get("detail", "")).lower():
                continue
            out.append(i)
        cats = {}
        for i in self.report().get("issues", []):
            cats[i.get("category")] = cats.get(i.get("category"), 0) + 1
        return dict(issues=out[:limit], total=len(out), categories=cats,
                    note="accepted_by_design = the builder marked this issue as intended; don't report it as a problem")

    def compiled(self, name):
        """Did this script compile? dict(script, found, has_compiled_ncs_in_module, last_compile, edited, note).

        last_compile comes from <analysis>/compile.json (written by Validate scripts / nwn_compile.py): the error
        list and ok=True when it was empty, or a note when that run did not include the script. edited=True when an
        edited copy exists in the overlay - its compile result is not covered here."""
        low = _script_name(name)
        p = os.path.join(self.dir, "compile.json")
        res = None
        if os.path.isfile(p):
            data = json.load(open(p, encoding="utf-8"))
            results = data.get("results") or {}
            if low in results:
                res = dict(errors=results[low], ok=not results[low], compiled_at=data.get("when") or data.get("at"))
            elif data:
                res = dict(note="the last compile run did not include this script")
        r = self.db.execute("SELECT has_ncs, has_main, has_sc FROM scripts WHERE lower(name)=?", (low,)).fetchone()
        edited = os.path.isfile(os.path.join(self.dir, "edits", low + ".nss"))
        return dict(script=low, found=bool(r), has_compiled_ncs_in_module=bool(r and r[0]),
                    last_compile=res or "not compiled by the toolkit yet (run Validate scripts / nwn_compile.py)",
                    edited=edited, note="an edited copy exists in your edits - compile it from the editor" if edited else "")


# ------------------------------------------------------------------ CLI
# command -> (number of required arguments, function(facts, args) -> result dict)
COMMANDS = {
    "summary": (0, lambda f, a: f.summary()), "exists": (1, lambda f, a: f.exists(a[0])),
    "who-uses": (1, lambda f, a: f.who_uses(a[0])), "uses": (1, lambda f, a: f.uses(a[0])),
    "script": (1, lambda f, a: f.script(a[0], *(a[1].split("-", 1) if len(a) > 1 else ()))),
    "grep": (1, lambda f, a: f.grep(a[0])), "variable": (1, lambda f, a: f.variable(a[0])),
    "tag": (1, lambda f, a: f.tag(a[0])), "quest": (1, lambda f, a: f.quest(a[0])), "faction": (0, lambda f, a: f.faction(a[0] if a else "")), "settings": (0, lambda f, a: f.settings()), "database": (0, lambda f, a: f.database(a[0] if a else "")),
    "impact": (1, lambda f, a: f.impact(a[0])), "fields": (1, lambda f, a: f.fields(a[0], a[1] if len(a) > 1 else "")),
    "issues": (0, lambda f, a: f.issues(a[0] if a else None)), "compiled": (1, lambda f, a: f.compiled(a[0])),
}


def _print(obj, indent=0):
    """Readable text output for the command line: nested keys indented, empty values left out, and a multi-line
    "text" (a script listing) printed as is after the other keys."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if "text" in obj and isinstance(obj["text"], str) and "\n" in obj["text"]:
            for k, v in obj.items():
                if k != "text":
                    print(f"{pad}{k}: {v}")
            print(obj["text"])
            return
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _print(v, indent + 1)
            elif v not in (None, "", [], {}):
                print(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict):
                print(pad + "- " + ", ".join(f"{k}={v}" for k, v in x.items() if v not in (None, "", [], {})))
            else:
                print(pad + "- " + str(x))
    else:
        print(pad + str(obj))


def main(argv=None):
    """Command line: nwn_facts.py [analysis command args...] [--json]. Returns the exit code (0 ok, 2 usage/error)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        res = list_analyses()
        print(json.dumps(res, indent=1)) if as_json else _print(res or ["no analyses in " + WORKSPACE])
        return 0
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 2
    nargs, fn = COMMANDS[argv[1]]
    if len(argv) - 2 < nargs:
        print(f"{argv[1]} needs {nargs} argument(s)")
        return 2
    try:
        f = Facts(argv[0], allow_outside=True)
        res = fn(f, argv[2:])
    except ValueError as ex:
        print("error:", ex)
        return 2
    print(json.dumps(res, indent=1, ensure_ascii=False)) if as_json else _print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
