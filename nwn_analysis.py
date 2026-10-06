"""
nwn_analysis.py - Phase 2: turn the index into findings.

    python nwn_analysis.py <analysis folder>      (normally run via nwn_analyse.py / the dashboard)

Produces report.json (everything the dashboard shows) and reports/*.csv (for Excel):
  * validation issues (missing/wrong-type scripts, uncompiled scripts, broken refs ...)
  * impact level (Low / Medium / High / Critical) for every script, blueprint, conversation,
    area, quest, asset - with the cascade path that explains it
  * safe-to-delete artefacts (Safe / Safe as group / Review) using reachability from module.ifo
  * duplicates (items, all blueprints, scripts, conversations, assets) with keeper suggestion
  * item locations and quest links, areas, conversations, quests, script descriptions
  * object hierarchy (Module > Area > Object > Inventory) and breadcrumbs

Why this exists
---------------
Phase 1 (nwn_index.py) records facts: every file, every field, every script and the links between them. This phase
answers the questions a builder asks before changing a module: what breaks if I change X (impact), what is never used
(safe to delete), what is duplicated (merge), and what is already broken (issues, each with a suggested fix).

Reads   <analysis>/index.sqlite, <analysis>/descriptions.json when present (extra script descriptions, shown as
        each script's description), and <analysis>/server_config.json when present (server settings loaded on the
        Overview page: scripts they name are kept).
Writes  <analysis>/report.json and <analysis>/reports/*.csv; drops the impact_detail table older versions stored in
        index.sqlite (an item's full impact is now worked out when asked for: Graph.impact). The hak catalogue step
        (nwn_catalog) also writes catalog.json and reports/hak_catalogue.csv; the palette step (nwn_palette) writes
        palette.json and reports/palette.csv.
Never   reads or writes the module, its haks or the game install: everything comes from the index.

Entry points: run_analysis(out_dir) (the whole pass), Graph (also used by the dashboard, nwn_audit and nwn_hakslim),
slim_report, write_csvs.

The dependency graph
--------------------
The index stores a graph: nodes (everything that can be used) and edges "src uses dst" with a kind and a "via" (the
field or script call that made the link). Node ids start with their type:
    module                  module.ifo                      area:<resref>          an area (.are/.git/.gic)
    script:<name>           .nss and/or .ncs                bp:<resref>.<ext>      a blueprint (.uti, .utc ...)
    dlg:<resref>            a conversation                  inst:<area>:<path>     an object placed in an area's .git
    quest:<tag>, tag:<tag>, var:<name>                      names used by fields/scripts (no file of their own)
    2da:, model:, hak:, asset:<name.ext>, file:<name.ext>   other files (file: = other GFF files, e.g. a palette .itp)
A node's in_module flag means a file for it was loaded (module, hak or override folder); a tag counts when some
object carries it, and variables always count. Edge kinds come in three groups
(nwn_index.CARRIER_KINDS / REFERENCE_KINDS / SOFT_KINDS):
    carrier    src runs, holds or creates dst (event slot, #include, ExecuteScript, conversation, inventory, spawn,
               area contains object ...). If dst changes, src's behaviour changes too, so impact travels along
               these edges any number of steps.
    reference  src names dst (tag lookup, journal quest, template of a placed copy ...). Matters only when dst
               itself is renamed or removed, so impact follows these one step from the changed thing (or its tag).
    soft       variables: var_set edges run var -> the script that sets it, var_read edges script -> var.

"Live" (reachability)
---------------------
A node is live when it can be reached by following edges forward ("uses") from the roots: module.ifo, every loaded
2da, every other loaded GFF file except palettes (.itp list every blueprint, which is not a real use), and every
script a loaded server setting names (server_config.json, see server_config_values). Every edge
kind is followed, so an object whose tag a live script looks up is live, and so is a script that sets a variable a
live script reads. Live decides deletions (only non-live module files are candidates) and caps impact (see below).

Impact levels (Graph.impact, thresholds CRITICAL/HIGH/MEDIUM below)
--------------------------------------------------------------------
"If this changes or disappears, what else is affected?" Walk backwards from the item: carrier edges any number of
steps, reference edges one step, then a soft pass through shared variables. Then:
    Critical  runs from a module-wide event, or reaches >= 10 areas, or >= 25 other scripts
    High      touches a quest (a quest tag used by it or by anything it affects), or reaches >= 3 areas, or >= 8
              other scripts
    Medium    reaches an area, or >= 3 things in all
    Low       anything else is affected (including placed copies or soft links only)
    None      nothing depends on it - or (areas, scripts, blueprints, conversations) it is not live and nothing it
              affects is live (Graph.unreachable_rule, applied whenever the live set is given)
The report's levels come from Graph.impact_all, which gives the same results as Graph.impact for every item at once
(see _Reach for how).

Deletion statuses (the "safe to delete" stage)
----------------------------------------------
Candidates: module files whose node is not live, except module.ifo and the types never offered (.ifo .jrl .fac .itp
.2da .hak .tlk). Each candidate then gets:
    Review         at least one reason for a person to look: its name matches a piece of a name scripts build at
                   run time, it overrides a base-game file (or, for an asset, that can't be checked), the module was
                   not read completely, a hak that could refer to it was not loaded, it is a whole area, it is an
                   asset whose name the engine builds itself (portraits, helmets and other part models, phenotype
                   body parts, tiles ... - ASSET_REVIEW_PATTERNS), something outside the candidates mentions it, or it
                   is used by a candidate that is itself Review. Also, for scripts: its name ends in an item tag while
                   a tag-based prefix is set from a value the index could not read, or the module includes nwnx_*
                   and no server settings are loaded (NWNX can run a script named only there). For blueprints: a
                   script creates that object type from a name read at run time (a database, a variable), the
                   toolset palette lists it (DMs can spawn it), or the module has no palette of its type to check.
                   Scripts the loaded server settings name are roots (live), so they are never candidates.
    Safe as group  no reason to review, but other candidates use it: delete it together with them
    Safe           no reason to review and nothing uses it
Anything uncertain becomes Review, never Safe: when it is unclear whether something is used, it is kept.

Stages, in order (each one is a nwn_progress stage the dashboard shows)
-----------------------------------------------------------------------
    load            graph, file list, base-game names, runtime name pieces, descriptions.json
    issues          the index's issues, plus missing conversations/quests/blueprints/tags, broken strrefs, conflicts
    reachability    the live set
    impact          level + reasons for every loaded script, blueprint, conversation, area, quest, model, asset, 2da,
                    placed object and the module (the full chain is worked out on demand)
    scripts         role and summary (nwn_describe), includes, triggers, missing #include issues
    objects/items   item blueprints with locations, holders, quest links; placed objects whose blueprint is not in
                    the module
    duplicates      groups with a keeper, mergeable or blocked (and why), how look-alikes differ (nwn_dupdiff)
    safe to delete  the statuses above
    areas, conversations, quests   per-area objects/transitions; who starts each conversation; journal quest health
    inferred quests (nwn_quests), orphan clusters, creature AI, skins and hides, load and performance
    other checks    variables/tags (nwn_varaudit), factions, campaign databases, compiled scripts (nwn_ncs), quest
                    families (nwn_questsets), PW performance (nwn_perf), known EE pitfalls (nwn_lints)
    report          summary, a fix for every issue (nwn_fixes), CSVs, the slimmed report.json, the hak catalogue
The checks that live in other modules run inside try/except: a failure leaves that section empty (most record the
error in their summary) and never stops the analysis.

Limits: names built at run time and assets reached through 2da row numbers can't be resolved, so they are kept or
marked Review rather than guessed at. An item's impact detail lists at most MAX_AFFECTED_LISTED affected items.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque

import nwn_describe
import nwn_index
import nwn_progress
from nwn_index import BASE_GAME_PREFIXES, CARRIER_KINDS, REFERENCE_KINDS

LEVELS = ["None", "Low", "Medium", "High", "Critical"]     # lowest first; the index is used to sort by level
# Thresholds for impact levels - tune to taste.
CRITICAL = dict(areas=10, scripts=25)
HIGH = dict(areas=3, scripts=8)
MEDIUM = dict(areas=1, total=3)
MAX_AFFECTED_LISTED = 300           # cap on the "affected" list of an item's impact detail
# node types that get an impact level in the report (when loaded: module, hak or override folder)
IMPACT_TYPES = ("script", "blueprint", "conversation", "area", "quest", "model", "asset", "2da", "module", "instance")

IMAGE_EXTS = {"tga", "png", "jpg", "bmp", "gif"}
# Engine naming conventions: names the game builds from 2da row numbers, so no file ever "mentions" them.
#   p<m/f><race><phenotype 1-2 digits>_  body parts (CEP phenotypes 14, 17, 27, 34 ...)
#   helm_128 / cloak_256 / robe_117 / <part>_NNN   row-numbered part models and their .plt textures
#   *_b_001 / *_m_001 / *_t_001            item model parts;  i*_001 item icons;  t??01_ tiles; po_ portraits
#   and the prefixes of creature, placeable, GUI, effect, loading-screen, music and sound files
# Used for the safe-to-delete statuses below and by nwn_hakslim (lean haks keep what it matches).
PART_RE = re.compile(r"^p[mf][a-z]\d{1,2}_|^[a-z]{1,3}_[a-z]{1,2}_\d{3}$|^i[a-z0-9]{1,4}_\d{3}$|^i[a-z]{1,3}_[a-z]+_\d{3}$|_[bmt]_\d{3}$|^[a-z]+_\d{3}$"
                     r"|^t[a-z]{2}\d{2}_|^po_|^c_|^n_|^plc_|^gui_|^fx_|^vfx_|^load_|^mus_|^as_|^al_|^a_|^alt_|^dbh_")
# Assets/models the engine finds by building their name itself (from a 2da row, a size letter, a model number), so
# no file in the module names them and "unused" can't be proved. Matched against the name without its extension; the
# first match gives the Review reason. Names the first five patterns match: po_hu_m_01_, pfh0_chest001 / pmh14_chest001,
# wswls_b_011, iit_gem_001, helm_015. The last entry (PART_RE) catches the remaining conventions with a general reason.
ASSET_REVIEW_PATTERNS = [
    (re.compile(r"^po_"), "portrait - engine builds names from portraits.2da + size letter"),
    (re.compile(r"^p[mf][a-z]\d{1,2}_"), "player body part - engine builds names from appearance + phenotype rows"),
    (re.compile(r"_[bmt]_\d{3}$"), "item model part - engine builds names from baseitems.2da + model numbers"),
    (re.compile(r"^i[a-z_]*_\d{3}$"), "item icon - engine builds names from baseitems.2da"),
    (re.compile(r"^[a-z]+_\d{3}$"), "row-numbered part model (helm_015, cloak_012 ...) - engine builds the name from "
                                    "an item's model number"),
    (re.compile(r"^(c_|n_)"), "creature model/texture - usually referenced through appearance.2da"),
    (re.compile(r"^(t[a-z]{2}\d{2}_|tno01|tcn01|tde01)"), "tile model/texture - used by tilesets"),
    (re.compile(r"^(load_|lscreen)"), "loading screen - referenced by loadscreens.2da"),
    (re.compile(r"^(mus_|as_|al_)"), "music/ambient sound - referenced by ambientmusic/ambientsound.2da"),
    (PART_RE, "engine naming convention (body part, placeable, GUI, effect ...) - such files are found through 2da "
              "rows, not through a name another file mentions"),
]
# Item fields left out of an item's function signature (func_sig), so items that differ only in name, tag or looks
# still group as "same stats, different name/look" duplicates.
ITEM_COSMETIC = {"TemplateResRef", "Tag", "LocalizedName", "Description", "DescIdentified", "Comment",
                 "PaletteID", "ModelPart1", "ModelPart2", "ModelPart3", "Cloth1Color", "Cloth2Color",
                 "Leather1Color", "Leather2Color", "Metal1Color", "Metal2Color", "Identified", "Stolen"}
# Never offered for deletion: module-wide files (module info, journal, factions, palettes) and tables/archives that
# the game or haks can use without any reference this analysis can see.
NEVER_DELETE_EXTS = {"ifo", "jrl", "fac", "itp", "2da", "hak", "tlk"}
# Incoming reference kinds the clean-build Rewriter can re-point to a merge keeper (from GFF resref fields)
REWRITABLE_KINDS = {"template", "inventory", "event_script", "dlg_script", "uses_conversation", "module_event"}


class Graph:
    """The dependency graph from the index, held in memory for fast walks.

    db: the analysis index, sqlite3.Connection (kept as self.db). Attributes:
        nodes   {node id: dict(type, name, label, file_id, in_module)}
        fwd     {src: [(dst, kind, via)]}   what each node uses
        rev     {dst: [(src, kind, via)]}   who uses each node
        parent  {child: parent}              from "contains" edges (area -> placed object -> its inventory)
    Read-only: never writes to the index. Memory grows with the graph (about 85 MB for 60,000 nodes and 236,000
    edges; a very large module has hundreds of thousands of nodes)."""

    def __init__(self, db):
        """Load every node and edge from `db` (read-only)."""
        self.db = db
        self.nodes = {}
        for nid, t, name, label, fid, inmod in db.execute("SELECT * FROM nodes"):
            self.nodes[nid] = dict(type=t, name=name, label=label, file_id=fid, in_module=inmod)
        self.fwd = defaultdict(list)
        self.rev = defaultdict(list)
        self.parent = {}
        # sqlite returns a new string object for every value of every row, and each edge is held twice (fwd and rev):
        # sharing one object per distinct id, kind and via keeps a big graph's memory down
        same = {nid: nid for nid in self.nodes}
        for s, d, k, v in db.execute("SELECT * FROM edges"):
            s, d = same.setdefault(s, s), same.setdefault(d, d)
            k, v = same.setdefault(k, k), same.setdefault(v, v)
            self.fwd[s].append((d, k, v))
            self.rev[d].append((s, k, v))
            if k == "contains":
                self.parent[d] = s
        self._live = (None, None)       # (server_values, live set) of the last live() call

    def label(self, nid):
        """Readable label of a node, or the id itself for a node the graph doesn't have."""
        nd = self.nodes.get(nid)
        return nd["label"] if nd else nid

    def type(self, nid):
        """Node type ("script", "area" ...); for an unknown node, the id's prefix (the text before the first ':')."""
        nd = self.nodes.get(nid)
        return nd["type"] if nd else nid.split(":", 1)[0]

    # ------------------------------------------------------------------ impact
    def impact(self, start, detail=True, live=None):
        """Who is affected if `start` changes (or is removed/renamed).

        start: a node id. detail: also return the affected list, placed copies and what start uses (slower, bigger).
        live: the live set (Graph.live); when given, the level is lowered for items nothing in use leads to, as in the
        report (see unreachable_rule). Returns dict(level, reasons, counts, quests, areas) plus, with detail, affected
        (each with how, kind, via and the path back to start), placed_copies and uses. Levels and thresholds: see the
        module docstring. Read-only, never raises for an unknown node (it just finds nothing)."""
        hard, ref_nodes, placed = self._walk(start)
        soft = self._soft(hard)
        # tag and variable nodes are stepping stones, not things a builder can open: leave them out of the counts
        real = [n_ for n_ in hard if n_ != start and not n_.startswith(("tag:", "var:"))]
        softreal = [n_ for n_ in soft if not n_.startswith(("tag:", "var:"))]
        by_type = defaultdict(int)
        for n_ in real:
            by_type[self.type(n_)] += 1
        areas = sorted(n_ for n_ in hard if n_.startswith("area:"))     # start too, when it is an area
        quests = {dst[6:] for n_ in hard for dst, kind, via in self.fwd.get(n_, []) if kind == "quest_ref"}
        if start.startswith("quest:"):
            quests.add(start[6:])
        # the module is only 'hit' through a module-wide event script; being listed in module.ifo is a reference
        hit = start != "module" and "module" in hard and hard["module"][1] == "module_event"
        out = self._judge(start, len(real), by_type, areas, quests, len(placed), len(softreal),
                          hard["module"][2] if hit else None)
        if live is not None:
            self.unreachable_rule(start, out, live, any(n_ in live for n_ in real + softreal))
        if detail:
            def path(n_, table):
                """The chain of (node, edge kind, via) from n_ back to start, at most 12 steps."""
                chain = []
                while n_ is not None and len(chain) < 12:
                    par, kind, via = table.get(n_) or hard.get(n_) or (None, "", "")
                    chain.append(dict(node=n_, label=self.label(n_), kind=kind, via=via))
                    n_ = par
                return chain
            aff = []
            for n_ in real[:MAX_AFFECTED_LISTED]:
                kind, via = hard[n_][1:]
                aff.append(dict(node=n_, label=self.label(n_), type=self.type(n_),
                                how="reference" if n_ in ref_nodes else "cascade", kind=kind, via=via,
                                path=path(n_, hard)))
            for n_ in softreal[:100]:
                kind, via = soft[n_][1:]
                aff.append(dict(node=n_, label=self.label(n_), type=self.type(n_), how="soft", kind=kind,
                                via=via, path=path(n_, {**hard, **soft})))
            out["affected"] = aff
            out["placed_copies"] = [dict(node=p, label=self.label(p), where=self.breadcrumb(p)) for p in placed[:200]]
            out["uses"] = [dict(node=d, label=self.label(d), kind=k, via=v)
                           for d, k, v in self.fwd.get(start, [])][:200]
        return out

    def impact_all(self, targets, live):
        """impact(t, detail=False, live=live) for every node id in `targets`, as {t: result} in the same order.

        The results are the same as calling impact one by one (tests/test_scenarios.py impact_engine compares them),
        but the shared part of the walks is worked out once (see _Reach), so a module with tens of thousands of
        targets takes seconds instead of the better part of an hour. Read-only."""
        reach = _Reach(self, live)
        return {t: reach.impact(t) for t in targets}

    def unreachable_rule(self, start, out, live, affects_live):
        """Lower an impact result to level None when `start` can't break the running module: an area, script,
        blueprint or conversation that is not in `live` and affects nothing in it (e.g. a script run only by an area
        that is not in the module's area list). out: the dict impact() returns, changed in place. affects_live: True
        when any affected node (cascade, reference or soft) is live. Used by impact and impact_all."""
        if start not in live and self.type(start) in ("area", "script", "blueprint", "conversation") \
                and not affects_live:
            out["level"] = "None"
            out["reasons"] = ["Not reachable from the module (nothing in use leads here)"] + \
                [r for r in out["reasons"] if "Nothing" not in r]

    def _one_step(self, kind, src, start):
        """True when the walk follows a reference edge of this kind from src one step to start (or to start's own
        tag node): src names start, so it is affected when start is renamed or removed. Placed copies ("template")
        are counted separately, never walked."""
        if kind not in REFERENCE_KINDS or kind == "template":
            return False
        # a name found in an area/blueprint/conversation/module/placed object that also links to start through a
        # typed field (event slot, conversation ...): the same link, already counted by that edge
        return not (kind == "name_ref" and self.type(src) in ("area", "blueprint", "conversation", "module", "instance")
                    and self.type(start) in ("script", "blueprint", "conversation"))

    def _first_step(self, start):
        """The first step of the impact walk, from start itself. Returns (first, placed): first maps each node
        reached to (edge kind, via), in walk order - things that run or hold start (carrier edges), things that name
        it (reference edges) and start's own tag node ("own_tag": scripts that look start up by tag are affected by a
        rename); placed lists start's placed copies (one per template edge, in edge order)."""
        first, placed = {}, []
        for src, kind, via in self.rev.get(start, []):
            if src == start or src in first:
                continue
            if kind in CARRIER_KINDS or self._one_step(kind, src, start):
                first[src] = (kind, via)
            elif kind == "template" and src.startswith("inst:"):
                # A placed copy keeps its own full set of fields: editing the blueprint does not change it
                placed.append(src)
        for src, kind, via in self.rev.get(start, []):
            if kind == "tag_provider" and src != start and src not in first:
                first[src] = ("own_tag", "Tag")
        return first, placed

    def _walk(self, start, until=None):
        """The impact walk backwards from start: carrier edges any number of steps, reference edges one step from
        start or from its own tag. Returns (hard, ref_nodes, placed): hard maps every node reached (start included)
        to (the node it was reached from, edge kind, via) - following these parents back gives the path that explains
        why it is affected; ref_nodes are the nodes reached by a reference edge; placed lists start's placed copies.
        until: stop as soon as this node is reached (hard is then partial; used by _Reach for the module's edge)."""
        first, placed = self._first_step(start)
        hard = {start: (None, "start", "")}
        ref_nodes = set()
        for src, (kind, via) in first.items():
            hard[src] = (start, kind, via)
            if kind not in CARRIER_KINDS and kind != "own_tag":
                ref_nodes.add(src)
        if until in first:
            return hard, ref_nodes, placed
        q = deque(first)
        while q:
            cur = q.popleft()
            # start's own tag: things that name the tag are one reference step from start (a script that looks up
            # a tag is affected by a rename; things further away are not)
            near = cur.startswith("tag:") and hard[cur][0] == start
            for src, kind, via in self.rev.get(cur, []):
                if src in hard:
                    continue
                if kind in CARRIER_KINDS:
                    hard[src] = (cur, kind, via)    # src runs/holds cur, so it changes with cur: keep walking
                elif near and kind == "template":
                    if src.startswith("inst:"):
                        placed.append(src)
                    continue
                elif near and self._one_step(kind, src, start):
                    hard[src] = (cur, kind, via)
                    ref_nodes.add(src)
                else:
                    continue
                if src == until:
                    return hard, ref_nodes, placed
                q.append(src)
        return hard, ref_nodes, placed

    def _soft(self, hard):
        """The soft pass after a walk: scripts that read variables written by affected scripts, and what runs them.
        Returns {node: (the node it was reached from, edge kind, via)} for nodes not already in hard.
        (var_set edges run var -> setter, so rev[setter] gives the variables it sets; rev[var] then gives readers.)"""
        soft = {}
        sq = deque()
        for nd in hard:
            for src, kind, via in self.rev.get(nd, []):
                if kind == "var_set" and src not in hard and src not in soft:
                    soft[src] = (nd, kind, via)
                    sq.append(src)
        while sq:
            cur = sq.popleft()
            for src, kind, via in self.rev.get(cur, []):
                if src in hard or src in soft:
                    continue
                if kind == "var_read" or kind in CARRIER_KINDS:
                    soft[src] = (cur, kind, via)
                    sq.append(src)
        return soft

    def _judge(self, start, n_real, by_type, areas, quests, n_placed, n_soft, module_via):
        """Level and reasons from what the walk found (shared by impact and _Reach, so both give the same words).
        n_real: affected nodes other than start, tags and variables; by_type: those counted by node type; areas: the
        sorted area ids; quests: quest tags; n_placed: placed copies; n_soft: soft-linked nodes; module_via: the event
        slot when a module-wide event runs it, else None. Returns dict(level, reasons, counts, quests, areas)."""
        n_scripts = by_type.get("script", 0)
        reasons = []
        if module_via is not None:
            reasons.append(f"Runs from a module-wide event ({module_via})")
        inc_users = sum(1 for s, k, v in self.rev.get(start, []) if k == "include")
        if inc_users:
            reasons.append(f"#included by {inc_users} script(s)")
        if quests:
            reasons.append("Touches quest(s): " + ", ".join(sorted(quests)[:5]))
        if areas:
            reasons.append(f"Reaches {len(areas)} area(s)")
        if n_scripts:
            reasons.append(f"{n_scripts} other script(s) affected")
        if n_placed:
            reasons.append(f"{n_placed} placed copy(ies) exist (they do NOT auto-update from the blueprint)")
        if n_soft:
            reasons.append(f"{n_soft} more linked via shared variables (soft)")

        if module_via is not None or len(areas) >= CRITICAL["areas"] or n_scripts >= CRITICAL["scripts"]:
            level = "Critical"
        elif quests or len(areas) >= HIGH["areas"] or n_scripts >= HIGH["scripts"]:
            level = "High"
        elif len(areas) >= MEDIUM["areas"] or n_real >= MEDIUM["total"]:
            level = "Medium"
        elif n_real or n_placed or n_soft:
            level = "Low"
        else:
            level = "None"
            reasons.append("Nothing in the module depends on it")
        # a script/conversation/blueprint nobody uses: say so plainly; a quest it would touch is only a note
        if not n_real and not n_placed and not n_soft and start.startswith(("script:", "dlg:", "bp:")):
            level = "None"
            reasons = ["Nothing in the module runs or references it"] + \
                ([f"(would touch quest(s) {', '.join(sorted(quests))} if it were used)"] if quests else [])
        return dict(level=level, reasons=reasons,
                    counts=dict(total=n_real, soft=n_soft, areas=len(areas), quests=len(quests),
                                placed_copies=n_placed, **dict(sorted(by_type.items()))),
                    quests=sorted(quests), areas=areas)

    # --------------------------------------------------------------- hierarchy
    def breadcrumb(self, nid):
        """Where a node lives, as text. A placed object gets the chain of its containers from "contains" parents
        (at most 10 levels; an item in a chest: "Module › <area> › <chest>"), an area gets "Module", anything else
        the dashboard section it belongs to ("Scripts", "Palette (blueprint)" ...)."""
        chain = []
        cur = nid
        while cur in self.parent and len(chain) < 10:
            cur = self.parent[cur]
            chain.append(self.short_label(cur))
        chain.reverse()
        if chain or nid.startswith("area:"):
            return " › ".join(["Module"] + chain)
        t = self.type(nid)
        return {"blueprint": "Palette (blueprint)", "script": "Scripts", "conversation": "Conversations",
                "quest": "Journal", "model": "Models", "asset": "Assets", "2da": "2DA tables"}.get(t, t)

    def short_label(self, nid):
        """Label without the " in <area>" ending that placed objects' labels carry (used inside breadcrumbs)."""
        nd = self.nodes.get(nid, {})
        if nid.startswith("area:"):
            return nd.get("label", nid)
        lab = nd.get("label", nid)
        return lab.split(" in ")[0] if nid.startswith("inst:") else lab

    def hierarchy(self):
        """The object tree for the dashboard: Module > areas (in module.ifo's area list order) > objects grouped by
        class > what they contain. Areas that exist but are not in the area list go under a separate group.
        Returns nested dicts (id, label, type, children). Depth is capped at 8 levels."""
        def build(nid, depth=0):
            kids = [d for d, k, v in self.fwd.get(nid, []) if k == "contains"]
            nd = dict(id=nid, label=self.short_label(nid), type=self.type(nid))
            if kids and depth < 8:
                groups = defaultdict(list)
                for k_ in kids:
                    # placed objects are labelled "<class> '<name>' in <area>", so the first word is the class
                    cls = self.nodes.get(k_, {}).get("label", "").split(" ")[0] or "object"
                    groups[cls].append(build(k_, depth + 1))
                if nid.startswith("area:"):
                    nd["children"] = [dict(id=f"{nid}#{g}", label=f"{g.title()}s ({len(v)})", type="group", children=v)
                                      for g, v in sorted(groups.items())]
                else:
                    nd["children"] = [c for v in groups.values() for c in v]
            return nd
        listed = [d for d, k, v in self.fwd.get("module", []) if k == "module_area"]
        seen = set()
        areas = []
        for a in listed:
            if a not in seen and a in self.nodes:
                seen.add(a); areas.append(build(a))
        unlisted = [nid for nid, nd in self.nodes.items() if nd["type"] == "area" and nd["in_module"] and nid not in seen]
        root = dict(id="module", label=self.label("module") if "module" in self.nodes else "Module",
                    type="module", children=areas)
        if unlisted:
            root["children"].append(dict(id="unlisted", label=f"Areas NOT in module area list ({len(unlisted)})",
                                         type="group", children=[build(a) for a in sorted(unlisted)]))
        return root

    # ------------------------------------------------------------ reachability
    def reachable(self, roots):
        """Every node reachable from `roots` (an iterable of node ids) by following edges forward, of any kind.
        Palette files (.itp) are not walked through. Returns a set that includes the roots. Read-only."""
        seen = set(roots)
        q = deque(roots)
        while q:
            cur = q.popleft()
            if cur.startswith("file:") and cur.endswith(".itp"):
                continue  # palettes list every blueprint - not a real use
            for dst, kind, via in self.fwd.get(cur, []):
                if dst not in seen:
                    seen.add(dst); q.append(dst)
        return seen

    def live(self, server_values=()):
        """The live set (see the module docstring): every node reachable from the roots - module.ifo, every loaded 2da,
        every other loaded GFF file except palettes (journal, factions ...), every tag-based item script and the loaded
        scripts the server's
        settings name (server_values: the setting values from server_config_values; NWNX_UTIL_PRE_MODULE_START_SCRIPT
        =pw_preload runs pw_preload although no module file names it). The last result is kept, so the dashboard
        can ask for it on every item it shows. Read-only."""
        key = frozenset(server_values)
        if self._live[0] != key:
            roots = {"module"}
            for nid, nd in self.nodes.items():
                if nd["in_module"] and (nd["type"] == "2da" or (nid.startswith("file:") and not nid.endswith(".itp"))):
                    roots.add(nid)
            roots |= {f"script:{v}" for v in key if self.nodes.get(f"script:{v}", {}).get("in_module")}
            # tag-based item scripts: the game runs <tag>.nss when a player uses an item with that tag - and players
            # carry items no module file places (bought, looted, DM-given, kept in their characters for years), so
            # such a script is in use even when nothing in the module places the item (a tester's teleport stone)
            roots |= {nid for nid, nd in self.nodes.items() if nd["type"] == "script" and nd["in_module"] and
                      any(k == "tag_based_script" for _s, k, _v in self.rev.get(nid, []))}
            self._live = (key, self.reachable(roots))
        return self._live[1]



# Issues about what content does when it runs (a script slot naming a missing script, a tag nothing has, a heavy
# heartbeat ...). When the content holding the problem is not in use - nothing in the module reaches it - the problem
# can't happen in play, so it is an info note ("in unused content"), not an error or warning: on big old modules most
# of such findings sit in leftovers (a large public world: 2,005 of 4,960 missing scripts) and buried the real ones.
# File-level problems (a name too long to load, a damaged file, hak conflicts ...) keep their severity.
UNUSED_DOWNGRADE = {"missing_script", "missing_conversation", "missing_blueprint", "missing_quest", "unknown_tag",
                    "wrong_script_type", "missing_include", "missing_creature_item", "faction_invalid",
                    "broken_strref", "creature_ai", "perf_area_heartbeats", "perf_spawn_density", "perf_inventory",
                    "perf_heavy_heartbeat", "area_tag", "tag_case", "missing_head_model", "not_compiled"}
# Review reasons that hold for every candidate of a kind at once (see the deletions loop): an item with only these is
# "likely in use" (deletions[].general) and the Safe to delete page lists it apart from the ones worth a look
GENERAL_REVIEW = ("the module uses NWNX", "hak(s) not loaded", "a script creates ", "only in the toolset palette",
                  "the module has no ", "same name as a base-game resource", "NWN install not configured",
                  "module not read completely")
UNUSED_SUFFIX = " - only in unused content (nothing in the module reaches it)"


def missing_script_issues(db, g, not_loaded_hint=""):
    """One missing_script issue per missing script, from the index's one-per-slot rows ("<field> -> 'name' not found in
    module", node = the object holding the slot). A script named in 22 event slots is one problem with 22 places to
    fix, not 22 problems: a big old module had 2,955 such rows for 900 scripts. The issue's node is the missing script
    (script:<name>; its detail panel lists every user), its detail names the script, then says how many places name
    it and lists the first ones ("<object>: <field>"); places carries them all. Read-only."""
    # Two kinds of slot where a missing script does no harm (a tester's report on 1.5.1):
    #  - a static placeable: the game bakes it into the area and never creates it as an object, so none of its
    #    scripts (heartbeat included) ever run;
    #  - a creature's OnConversation (ScriptDialogue): naming a script that doesn't exist is a known builder's trick -
    #    the NPC then neither speaks nor turns to the player when clicked.
    # A script missing only in such slots is an info note; one missing in any other slot stays an error.
    obj = {}
    try:
        for node_, fid, path_, cls in db.execute("SELECT node, file_id, path, class FROM objects"):
            obj[node_] = (fid, path_ or "", cls)
        static = {(fid, path_.rsplit("/", 1)[0] if "/" in path_ else "")
                  for fid, path_ in db.execute("SELECT file_id, path FROM fields WHERE label='Static' AND value='1'")}
    except sqlite3.Error:
        static = set()

    def harmless(node_, field):
        """Why a missing script in this slot never matters ('' when it may): see above."""
        fid, path_, cls = obj.get(node_, (None, "", ""))
        if cls == "placeable" and (fid, path_) in static:
            return "static placeable"
        if cls == "creature" and field.rsplit("/", 1)[-1] == "ScriptDialogue":
            return "OnConversation"
        return ""
    by_name = defaultdict(list)
    quiet = defaultdict(int)
    for _s, _c, n_, d in db.execute("SELECT * FROM issues WHERE category='missing_script'"):
        m = re.match(r"(.*?) -> '([^']+)' not found", d)
        if m:
            why = harmless(n_, m.group(1))
            by_name[m.group(2).lower()].append(f"{g.label(n_)}: {m.group(1)}" + (f" ({why})" if why else ""))
            quiet[m.group(2).lower()] += bool(why)
    out = []
    for name, places in sorted(by_name.items()):
        listed = f"used by {len(places)} place(s): {'; '.join(places[:6])}{' …' if len(places) > 6 else ''}"
        if quiet[name] == len(places):
            out.append(dict(severity="info", category="missing_script", node=f"script:{name}", label=name,
                            detail=f"Script '{name}' not found in module, named only where that does no harm - static "
                                   "placeables run no scripts, and a missing OnConversation script is a known way to "
                                   f"stop an NPC speaking or turning to the player; {listed}", places=places[:200]))
            continue
        out.append(dict(severity="error", category="missing_script", node=f"script:{name}", label=name,
                        detail=f"Script '{name}' not found in module{not_loaded_hint}; {listed}",
                        places=places[:200]))
    return out


def downgrade_unused(issues, g, live):
    """Lower errors and warnings of the UNUSED_DOWNGRADE kinds to info when nothing in use holds them: the issue's own
    node (a placed object counts as its area) is not live, or - for a missing resource, whose node is the thing that
    is missing - none of the things that name it is live. Changes issues in place; returns how many were lowered."""
    def holder_live(n):
        return n in live or (n.startswith("inst:") and "area:" + n.split(":")[1] in live)
    lowered = 0
    for i in issues:
        if i["severity"] == "info" or i["category"] not in UNUSED_DOWNGRADE:
            continue
        n = i["node"]
        nd = g.nodes.get(n)
        if nd is not None and not nd["in_module"]:          # a missing thing: judged by who names it
            used = any(holder_live(s) for s, k, v in g.rev.get(n, []) if k != "tag_provider")
        elif nd is not None or n.startswith(("inst:", "area:")):
            used = holder_live(n)
        else:
            continue                                         # not a graph node (a file name): left as it is
        if not used:
            i["severity"] = "info"
            i["detail"] += UNUSED_SUFFIX
            lowered += 1
    return lowered


def _mask(bits):
    """A bit set (a Python int) with the given bit numbers set."""
    bits = list(bits)
    if not bits:
        return 0
    buf = bytearray(max(bits) // 8 + 1)
    for i in bits:
        buf[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(buf, "little")


def _bit_numbers(x):
    """The numbers of the bits set in x, lowest first (meant for small sets: each step costs the size of x)."""
    while x:
        low = x & -x
        yield low.bit_length() - 1
        x ^= low


# int.bit_count arrived in Python 3.10; counting the "1"s of bin() gives the same answer on 3.8 and 3.9
_popcount = getattr(int, "bit_count", None) or (lambda x: bin(x).count("1"))


class _Reach:
    """Graph.impact_all's engine: the impact walk of every target at once, with the same results as Graph.impact.

    Why: impact walks backwards from a node along carrier edges any number of steps. In a persistent world almost every
    script reaches almost every other (includes, ExecuteScript), so one walk per target costs targets x graph: about
    35 minutes for a module of 60,000 nodes. But those walks repeat each other. The nodes a carrier walk from X reaches
    (X's "closure") are X's strongly connected component (SCC: the nodes that reach each other) plus the closures of
    the SCCs that carry it, so Tarjan's algorithm, which lists every SCC after the SCCs it reaches, fills all closures
    in one pass. A closure is a Python int used as a bit set (bit i = node number i): OR is union, AND keeps the
    nodes of a kind, and counting the 1 bits counts them.

    What a walk adds to that (the rules are Graph._first_step / _walk / _soft; each line below follows one):
      * reference edges one step from start and from start's own tag node: those nodes are extra starting points
        ("seeds"), so the walk's nodes = the union of the seeds' closures;
      * the soft pass: variables set by a walked node, then the scripts that read them and what carries those;
      * the module's edge: the report says which module event runs the item, and that is the module edge the walk
        meets first. Breadth-first order is fixed by distance, so the module edge into the nearest candidate wins;
        when two candidates are equally near and give different answers, the walk itself is run up to the module.
    Two rare cases depend on the walk's order in ways the bit sets can't show, and are answered by Graph.impact
    itself: a variable node that something reads inside the walked set (the soft pass stops at walked nodes), and a
    placed copy linked to start's tag that the walk also reaches by another edge.

    Memory: a closure is stored as (bit set, extra node numbers): a small closure keeps its nodes as a tuple on top of
    the bit set of the one SCC it extends (0 when none), so the many small closures (placed objects, leaf scripts)
    don't each cost a bit set as wide as the graph; targets whose whole walk is that small are walked directly."""

    SMALL = 64      # nodes a closure may keep as a tuple before it is turned into a bit set

    def __init__(self, g, live):
        """Number the nodes and work out every SCC's closure. g: the Graph; live: the live set (for the reachability
        rule, Graph.unreachable_rule)."""
        self.g = g
        self.live = live
        rev, fwd = g.rev, g.fwd
        ids = set(g.nodes) | {n for n, lst in fwd.items() if lst} | {n for n, lst in rev.items() if lst}

        # The nodes the summaries decode get the lowest bit numbers, so decoding them only reads the low end of a bit
        # set: the module (bit 0), areas, nodes that name a quest, scripts that set a variable (var_set edges run
        # variable -> setter), variables something reads. The rest follow, grouped by type.
        def flags(n):
            return (n == "module", n.startswith("area:"), any(k == "quest_ref" for d, k, v in fwd.get(n, ())),
                    any(k == "var_set" for s, k, v in rev.get(n, ())),
                    any(k == "var_read" for s, k, v in rev.get(n, ())))
        flag = {n: flags(n) for n in ids}
        order = sorted(ids, key=lambda n: (not flag[n][0], not any(flag[n]), str(g.type(n)), n))
        self.ids = order
        self.idx = idx = {n: i for i, n in enumerate(order)}
        has_module = "module" in idx

        def mask_of(test):
            return _mask(i for i, n in enumerate(order) if test(n))
        self.area_mask = mask_of(lambda n: flag[n][1])
        self.quest_mask = mask_of(lambda n: flag[n][2])
        self.setter_mask = mask_of(lambda n: flag[n][3])
        self.read_mask = mask_of(lambda n: flag[n][4])
        self.low_mask = (1 << sum(1 for n in order if any(flag[n]))) - 1
        # counted nodes: everything but tag and variable nodes (stepping stones, see Graph.impact)
        counted = [i for i, n in enumerate(order) if not n.startswith(("tag:", "var:"))]
        by_type = defaultdict(list)
        for i in counted:
            by_type[g.type(order[i])].append(i)
        self.type_masks = [(t, _mask(b)) for t, b in by_type.items()]
        self.counted_mask = _mask(counted)
        self.live_mask = _mask(i for i in counted if order[i] in live)
        self.quest_names = {idx[n]: [d[6:] for d, k, v in fwd[n] if k == "quest_ref"] for n in order if flag[n][2]}
        self.set_vars = {idx[n]: [idx[s] for s, k, v in rev[n] if k == "var_set"] for n in order if flag[n][3]}
        self.readers = {idx[n]: [idx[s] for s, k, v in rev[n] if k == "var_read"] for n in order if flag[n][4]}

        # carriers[i]: the nodes the walk steps to from node i (src of a carrier edge into i)
        carriers = [[idx[s] for s, k, v in rev.get(n, ()) if k in CARRIER_KINDS] for n in order]
        self.comp, sccs = self._tarjan(carriers)
        self.closure = []           # per SCC: (bit set, tuple of extra node numbers), see the class docstring
        for ci, members in enumerate(sccs):
            extra = set(members)
            bases = {}
            for c in {self.comp[w] for v in members for w in carriers[v]} - {ci}:
                base, more = self.closure[c]
                if base:
                    bases[id(base)] = base
                extra.update(more)
            if len(bases) > 1 or len(extra) > self.SMALL:
                bits = _mask(extra)
                for base in bases.values():
                    bits |= base
                self.closure.append((bits, ()))
            else:
                self.closure.append((next(iter(bases.values()), 0), tuple(extra)))
        self.module_bit = 1 if has_module else 0
        self.cache = {}             # frozenset of seed SCCs -> what the walk from them finds (see _found)
        self.decoded = {}           # (kind, low bits) -> decoded list/set (areas, quests, soft bit set)
        self.dists = {}             # node -> {node: carrier steps from it}, see _distance

    @staticmethod
    def _tarjan(succ):
        """Strongly connected components of the graph i -> succ[i], without recursion (a big module's chains are
        deeper than Python's recursion limit). Returns (comp, sccs): comp[i] = SCC number of node i; sccs in the order
        Tarjan's algorithm finds them, every SCC after the SCCs it reaches."""
        n = len(succ)
        index, low, on_stack = [-1] * n, [0] * n, [False] * n
        stack, comp, sccs, counter = [], [-1] * n, [], 0
        for root in range(n):
            if index[root] != -1:
                continue
            work = [(root, 0)]
            while work:
                v, pos = work.pop()
                if pos == 0:
                    index[v] = low[v] = counter
                    counter += 1
                    stack.append(v)
                    on_stack[v] = True
                descended = False
                for i in range(pos, len(succ[v])):
                    w = succ[v][i]
                    if index[w] == -1:
                        work.append((v, i + 1))     # come back to v's next successor afterwards
                        work.append((w, 0))
                        descended = True
                        break
                    if on_stack[w]:
                        low[v] = min(low[v], index[w])
                if descended:
                    continue
                if low[v] == index[v]:
                    members = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp[w] = len(sccs)
                        members.append(w)
                        if w == v:
                            break
                    sccs.append(members)
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
        return comp, sccs

    def _union(self, comps):
        """The union of these SCCs' closures as one bit set, or None when it is small enough to walk directly."""
        bases, extra = {}, set()
        for c in comps:
            base, more = self.closure[c]
            if base:
                bases[id(base)] = base
            extra.update(more)
        if not bases:
            return None
        bits = _mask(extra)
        for base in bases.values():
            bits |= base
        return bits

    def _decode(self, kind, bits):
        """Cached decoding of the low bits of a walked set: "areas" -> sorted area ids, "quests" -> quest tags,
        "soft" -> the bit set the soft pass reaches from the variables the scripts in these bits set (walked nodes not
        yet removed)."""
        key = (kind, bits)
        if key not in self.decoded:
            if kind == "areas":
                val = sorted(self.ids[i] for i in _bit_numbers(bits))
            elif kind == "quests":
                val = {q for i in _bit_numbers(bits) for q in self.quest_names[i]}
            else:
                # the soft pass walks from a variable to the scripts that read it (var_read edges) and on along
                # carrier edges, and again from any read variable that this reaches
                val, done = 0, 0
                todo = [self.comp[v] for i in _bit_numbers(bits) for v in self.set_vars[i]]
                while todo:
                    val |= self._union(todo) or _mask(i for c in todo for i in self.closure[c][1])
                    new = val & self.read_mask & ~done
                    done |= new
                    todo = [self.comp[r] for i in _bit_numbers(new) for r in self.readers[i]]
            self.decoded[key] = val
        return self.decoded[key]

    def _found(self, comps):
        """What the walk from these seed SCCs finds, start-independent so targets can share it: dict(types (node type
        -> count, start included), areas, quests, soft (count), affects_live, module (bool)), or None when the walk
        must be run (a small walk, or a read variable among the walked nodes)."""
        if comps not in self.cache:
            hard = self._union(comps)
            found = None
            if hard is not None and not hard & self.read_mask:
                low = hard & self.low_mask
                soft = self._decode("soft", low & self.setter_mask) & ~hard
                types = {}
                for t, m in self.type_masks:
                    n = _popcount(hard & m)
                    if n:
                        types[t] = n
                found = dict(types=types, areas=self._decode("areas", low & self.area_mask),
                             quests=self._decode("quests", low & self.quest_mask),
                             soft=_popcount(soft & self.counted_mask),
                             affects_live=bool((hard | soft) & self.live_mask), module=bool(hard & self.module_bit))
            self.cache[comps] = found
        return self.cache[comps]

    def _distance(self, x, seeds):
        """Breadth-first distance of node x from start: the fewest carrier steps from a seed to x plus the seed's own
        distance (seeds: {node: 0 for start, 1 for the first step, 2 for references to start's tag}). None when the
        walk never reaches x."""
        if x not in self.dists:
            # carrier edges forward from x: d[y] = carrier steps from y back up to x
            d = {x: 0}
            q = deque([x])
            while q:
                cur = q.popleft()
                for dst, kind, via in self.g.fwd.get(cur, ()):
                    if kind in CARRIER_KINDS and dst not in d:
                        d[dst] = d[cur] + 1
                        q.append(dst)
            self.dists[x] = d
        d = self.dists[x]
        return min((d[s] + step for s, step in seeds.items() if s in d), default=None)

    def _module_edge(self, start, seeds, first):
        """(kind, via) of the module edge the walk from start meets first (the module is known to be reached and not
        in the first step). The candidates are the walked nodes the module has a followable edge into; the nearest
        one is dequeued first, and within it the first such edge in edge order wins."""
        g = self.g
        cands = {}
        for dst, kind, via in g.fwd.get("module", ()):
            if dst in cands:
                continue
            if kind in CARRIER_KINDS or (dst in first and dst.startswith("tag:") and g._one_step(kind, "module", start)):
                d = self._distance(dst, seeds)
                if d is not None:
                    cands[dst] = (d, kind, via)
        nearest = min(d for d, k, v in cands.values())
        answers = {(k, v) for d, k, v in cands.values() if d == nearest}
        if len(answers) == 1:
            return answers.pop()
        hard = g._walk(start, until="module")[0]       # equally near: the walk's own order decides
        return hard["module"][1:]

    def impact(self, start):
        """Graph.impact(start, detail=False, live) for one target, from the shared closures."""
        g = self.g
        first, placed = g._first_step(start)
        seeds = {start: 0}
        for n_ in first:
            seeds.setdefault(n_, 1)
        late_copies = []        # placed copies linked to start's own tag (counted unless the walk reaches them)
        for tag in first:
            if tag.startswith("tag:"):
                for src, kind, via in g.rev.get(tag, []):
                    if kind in CARRIER_KINDS:
                        continue
                    if kind == "template":
                        if src.startswith("inst:"):
                            late_copies.append(src)
                    elif g._one_step(kind, src, start):
                        seeds.setdefault(src, 2)
        comps = frozenset(self.comp[self.idx[s]] for s in seeds)
        found = self._found(comps)
        if found is None or (late_copies and any(self._union(comps) >> self.idx[s] & 1 for s in late_copies)):
            return g.impact(start, detail=False, live=self.live)
        types = dict(found["types"])
        if not start.startswith(("tag:", "var:")):
            t0 = g.type(start)              # the counts leave start itself out
            types[t0] -= 1
            if not types[t0]:
                del types[t0]
        module_via = None
        if start != "module" and found["module"]:
            kind, via = first["module"] if "module" in first else self._module_edge(start, seeds, first)
            module_via = via if kind == "module_event" else None
        quests = set(found["quests"]) | ({start[6:]} if start.startswith("quest:") else set())
        out = g._judge(start, sum(types.values()), types, list(found["areas"]), quests,
                       len(placed) + len(late_copies), found["soft"], module_via)
        g.unreachable_rule(start, out, self.live, found["affects_live"])
        return out


def sha(s):
    """SHA-1 hex digest of a text (used as a compact signature for comparisons, not for security)."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def server_config_values(out_dir):
    """(loaded, values) from <out_dir>/server_config.json - the server settings loaded on the Overview page (secrets
    already masked by nwn_modsettings). values: every setting's value, lower case, with a .ncs/.nss ending removed, so
    it can be compared with script names. (False, set()) when none were loaded or the file can't be read."""
    try:
        with open(os.path.join(out_dir, "server_config.json"), encoding="utf-8") as fh:
            configs = json.load(fh)
    except (OSError, ValueError):
        return False, set()
    vals = set()
    for c in configs if isinstance(configs, list) else []:
        for v in (c.get("values") or {}).values() if isinstance(c, dict) else ():
            v = str(v).strip().strip("\"'").lower()
            vals.add(v[:-4] if v.endswith((".ncs", ".nss")) else v)
    return bool(configs), vals


def run_analysis(out_dir, verbose=True):
    """Run the whole analysis pass on one analysis folder and write its report.

    out_dir: the analysis folder (holds index.sqlite from nwn_index.run_index). verbose: print progress notes and
    the failures of optional checks. Returns the report dict as written to report.json (the slimmed version, plus
    any hak catalogue issues). Writes only inside out_dir (see the module docstring); never touches the module.
    Raises FileNotFoundError when out_dir has no index.sqlite (sqlite3 errors when it is incomplete); failures of
    the optional checks never stop it."""
    nwn_progress.emit(phase="analysis", stage="Analysis: loading the index")
    index_path = os.path.join(out_dir, "index.sqlite")
    if not os.path.isfile(index_path):
        # sqlite3.connect would create an empty database here, turning a mistyped folder into a broken analysis
        raise FileNotFoundError(f"no index.sqlite in {out_dir} - run the index pass (nwn_analyse.py) first")
    db = sqlite3.connect(index_path)
    g = Graph(db)
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    nasher = meta.get("module_format") == "nasher json"      # scripts as source only (nwn_nasher)
    files = [dict(zip(("id", "source", "relpath", "resref", "ext", "kind", "size", "sha256", "content_hash",
                       "status", "error", "node"), r))
             for r in db.execute("SELECT f.id, s.kind, f.relpath, f.resref, f.ext, f.kind, f.size, f.sha256, "
                                 "f.content_hash, f.status, f.error, f.node FROM files f JOIN sources s ON s.id=f.source_id")]
    files_by_node = defaultdict(list)
    for f in files:
        files_by_node[f["node"]].append(f)
    module_files = [f for f in files if f["source"] == "module"]

    base_names = {r[0] for r in db.execute("SELECT name FROM base_resources")} \
        if db.execute("SELECT name FROM sqlite_master WHERE name='base_resources'").fetchone() else set()
    haks_missing = json.loads(meta.get("haks_missing") or "[]")

    def is_base(nid):
        """Does the base game ship a resource with this name? (exact when the NWN install is configured)
        Without the install's file list it falls back to a guess from common base-game name prefixes (nw_, x2_ ...,
        nwn_index.BASE_GAME_PREFIXES)."""
        nd_ = g.nodes.get(nid, {})
        name_ = nd_.get("name", nid)
        if base_names:
            files_ = [f"{f['resref']}.{f['ext']}" for f in files_by_node.get(nid, [])]
            if nid.startswith("script:"):
                files_ += [f"{name_}.nss", f"{name_}.ncs"]
            elif nid.startswith("dlg:"):
                files_.append(f"{name_}.dlg")
            elif "." in name_:
                files_.append(name_)
            return any(x in base_names for x in files_)
        return name_.split(".")[0].startswith(BASE_GAME_PREFIXES)

    # what was NOT read, so "missing" may just mean "in a hak / the base game"
    not_loaded_hint = ""
    if haks_missing or not base_names:
        parts = ([f"{len(haks_missing)} hak(s)"] if haks_missing else []) + ([] if base_names else ["the base game"])
        not_loaded_hint = f" ({' and '.join(parts)} not loaded - it may be there)"

    # pieces of names that scripts/variables build at runtime (prefixes AND suffixes)
    # (a bare number like "02" glued onto something says nothing about which names it builds - it would match
    # every name ending in 02; the prefix it is glued to is recorded on its own)
    fragments = sorted({r[0].lower() for r in db.execute("SELECT literal FROM concat_literals")
                        if len(r[0]) >= 2 and any(c.isalpha() for c in r[0])})

    def dynamic_match(base_):
        """The runtime name piece that `base_` starts or ends with (e.g. "q_" for q_guard2), or None. A match means
        a script may build this name at run time, so the resource can't be proved unused or merged away."""
        for fr in fragments:
            if base_ != fr and (base_.startswith(fr) or base_.endswith(fr)):
                return fr
        return None

    # Create* calls whose blueprint name is read at run time (a variable, a database read - nwn_index.index_nss): any
    # blueprint of that type ("*" = any type) may be the one created, so it can't be called unused or merged away.
    # blueprint ext -> ["script line N: call"]
    dyn_creates = defaultdict(list)
    for sc_, ln_, ext_, call_ in json.loads(meta.get("dynamic_creates") or "[]"):
        dyn_creates[ext_].append(f"{sc_} line {ln_}: {call_}")

    descriptions_override = {}
    p = os.path.join(out_dir, "descriptions.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            descriptions_override = json.load(fh)

    # ------------------------------------------------------------- issues
    # The index pass already recorded file-level problems; here the graph adds the "named but not there" ones (a
    # node that something refers to but for which no file was loaded: in_module = 0), then broken talk-table
    # references and resources that several sources supply.
    nwn_progress.stage("analysis", "Analysis: issues")
    issues = [dict(severity=s, category=c, node=n_, label=g.label(n_), detail=d)
              for s, c, n_, d in db.execute("SELECT * FROM issues") if c != "missing_script"]
    issues += missing_script_issues(db, g, not_loaded_hint)
    for nid, nd in g.nodes.items():
        if nd["in_module"]:
            continue
        refs = g.rev.get(nid, [])
        if not refs:
            continue
        t = nd["type"]
        who = ", ".join(sorted({g.label(s) for s, k, v in refs})[:4])
        if t == "conversation":
            issues.append(dict(severity="info" if is_base(nid) else "error", category="missing_conversation", node=nid,
                               label=nid, detail=f"Conversation '{nd['name']}' not found; used by {who}" + not_loaded_hint))
        elif t == "quest":
            issues.append(dict(severity="error", category="missing_quest", node=nid, label=nid,
                               detail=f"Journal quest '{nd['name']}' not in module.jrl; used by {who}"))
        elif t == "tag" and any(k == "tag_ref" for s, k, v in refs):
            issues.append(dict(severity="warning", category="unknown_tag", node=nid, label=nid,
                               detail=f"Looked up by tag '{nd['name']}' but no object in the module has that tag "
                                      f"(fine if created at runtime); used by {who}"))
        elif t == "blueprint":
            kinds = {k for s, k, v in refs}
            if kinds <= {"template"}:   # only placed copies use it - they carry their own data, so they still work
                issues.append(dict(severity="info", category="blueprint_not_in_module", node=nid, label=nid,
                                   detail=f"Placed objects come from blueprint '{nd['name']}' which is not in the "
                                          f"module (base game/hak, or deleted). Instances still work. Used by {len(refs)} object(s)"))
            elif kinds & {"spawns", "inventory"}:
                sev = "info" if is_base(nid) else "error"
                issues.append(dict(severity=sev, category="missing_blueprint", node=nid, label=nid,
                                   detail=f"Blueprint '{nd['name']}' is spawned/given but not found in module; used by {who}" +
                                          not_loaded_hint))
        elif t == "script" and is_base(nid):
            issues.append(dict(severity="info", category="base_game_script", node=nid, label=nid,
                               detail=f"Uses base-game script '{nd['name']}' (not in module, supplied by the game); used by {who}"))
    file_by_id = {x["id"]: x for x in files}
    for fid, path, strref, custom, has_text, status in db.execute(
            "SELECT * FROM strrefs WHERE status IN ('custom-missing','base-missing')"):
        f = file_by_id.get(fid)
        if f:
            issues.append(dict(severity="warning" if has_text else "error", category="broken_strref", node=f["node"],
                               label=f["relpath"], detail=f"{path}: string #{strref} is not in the "
                               f"{'custom' if custom else 'base'} talk table" +
                               (" (falls back to the embedded text)" if has_text else " - shows as blank in game")))
    for res, srcs in db.execute("SELECT * FROM conflicts"):
        # haks that carry the same model/texture/material are layered on purpose (load order picks one - the Hak
        # catalogue shows which): an info note. A warning only when the module's own copy or the override is involved
        # (one of them is then not what the game uses), or for tables and tilesets, where a mix-up breaks things
        # (2da clashes also have their own checks). A large world had 6,374 such warnings, all hak-vs-hak models/textures/materials.
        only_haks = all(x.strip().startswith("hak:") for x in srcs.split("|"))
        layered = only_haks and res.rpartition(".")[2].lower() not in ("2da", "set")
        issues.append(dict(severity="info" if layered else "warning", category="resource_conflict", node=res, label=res,
                           detail=f"Same resource in several places: {srcs}" +
                                  (" (haks layered on each other: the first in load order wins)" if layered else "")))
    sev_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (sev_order.get(i["severity"], 3), i["category"], i["label"]))

    # ------------------------------------------------------------- reachability (used by impact + deletions)
    nwn_progress.stage("analysis", "Analysis: what is reachable from the module")
    # Everything reachable from module.ifo, the loaded 2das and other GFF files, and the scripts the server's settings
    # name (when those settings were loaded: Overview > Server settings, then re-analyse) is "live" (Graph.live)
    server_cfg_loaded, server_values = server_config_values(out_dir)
    live = g.live(server_values)

    # ------------------------------------------------------------- impact
    nwn_progress.stage("analysis", "Analysis: impact levels")
    if verbose:
        print("  computing impact levels ...", flush=True)
    # report.json keeps each item's summary; the full chain (affected list, paths) is worked out when someone asks for
    # it (dashboard detail panel, nwn_facts), with the same level: Graph.impact(..., live=...)
    impact = g.impact_all([nid for nid, nd in g.nodes.items() if nd["in_module"] and nd["type"] in IMPACT_TYPES], live)
    db.execute("DROP TABLE IF EXISTS impact_detail")    # stored by older versions; out of date after this run
    db.commit()

    # ------------------------------------------------------------- scripts
    nwn_progress.stage("analysis", "Analysis: scripts")
    scripts = []
    lits_by_script = defaultdict(list)
    for s, line, func, lit, kind in db.execute("SELECT * FROM script_literals"):
        lits_by_script[s].append((func, lit))
    dyn_by_script = defaultdict(set)
    for s, line, func, lit in db.execute("SELECT * FROM concat_literals"):
        dyn_by_script[s].add(lit)
    defined_in = defaultdict(list)     # every script that defines a function (two token systems can share names)
    for name, defined in db.execute("SELECT name, functions_defined FROM scripts"):
        for fn in (defined or "").split(","):
            if fn and fn != "main" and fn != "StartingConditional":
                defined_in[fn].append(name)
    for (fid, name, has_main, has_sc, lines, nh, has_ncs, src, header, defined, calls) in \
            db.execute("SELECT * FROM scripts"):
        own = set((defined or "").split(","))
        nid = f"script:{name}"
        # reach_inc: every script this one #includes, directly or through other includes (names without "script:")
        reach_inc = set()
        stack = [d_[7:] for d_, k, v in g.fwd.get(nid, []) if k == "include"]
        while stack:
            x_ = stack.pop()
            if x_ in reach_inc:
                continue
            reach_inc.add(x_)
            stack += [d_[7:] for d_, k, v in g.fwd.get(f"script:{x_}", []) if k == "include"]
        # A call to a function another module script defines needs that script in the include chain; otherwise
        # the official compiler fails (or the function comes from a base-game/hak include we could not read).
        lib_calls, missing_inc = [], []
        # includes we could not read (base game / hak): any of them may define the function
        unread = sorted(x_ for x_ in reach_inc if not g.nodes.get(f"script:{x_}", {}).get("in_module"))
        for fn in json.loads(calls or "{}"):
            if fn in defined_in and fn not in own:
                libs = defined_in[fn]
                hit = next((l_ for l_ in libs if l_ in reach_inc), None)
                if hit:
                    lib_calls.append(f"{fn}() from {hit}")
                else:
                    missing_inc.append((fn, libs[0] if len(libs) == 1 else "' or '".join(libs[:3])))
        lib_calls.sort()
        for fn, lib in missing_inc:
            ncs_note = " A compiled .ncs of this script is in the module, so it compiled at some point." if has_ncs else ""
            if unread:
                issues.append(dict(severity="info", category="missing_include", node=nid, label=g.label(nid),
                                   detail=f"calls {fn}() (defined in the module in '{lib}', which it does not #include); it "
                                          f"also includes {', '.join(unread[:3])}, which is not in the module (base game or "
                                          f"a hak) and may define it - can't check without those files.{ncs_note}"))
            else:
                issues.append(dict(severity="error", category="missing_include", node=nid, label=g.label(nid),
                                   detail=f"calls {fn}() which is defined in '{lib}' but does not #include it "
                                          f"(will not compile unless the function exists elsewhere).{ncs_note}"))
        triggers = [(k, g.label(s), v) for s, k, v in g.rev.get(nid, [])
                    if k in ("event_script", "module_event", "dlg_script", "execute_script", "tag_based_script")]
        inc_users = sum(1 for s, k, v in g.rev.get(nid, []) if k == "include")
        d = nwn_describe.describe_script(name, has_main, has_sc, header, calls, lits_by_script[name],
                                         triggers, inc_users, sorted(dyn_by_script[name]), lib_calls,
                                         [f for f in (defined or "").split(",") if f],
                                         missing_inc=[f"{fn}() from '{lib}' (NOT included)" for fn, lib in missing_inc])
        scripts.append(dict(node=nid, name=name, role=d["role"], summary=d["summary"], details=d["details"],
                            description=descriptions_override.get(name), lines=lines,
                            compiled=bool(has_ncs) or (nasher and bool(has_main or has_sc)),
                            functions=defined.split(",") if defined else [],
                            includes=[d_[7:] for d_, k, v in g.fwd.get(nid, []) if k == "include"],
                            triggers=[dict(kind=k, by=lab, via=v) for k, lab, v in triggers],
                            included_by=inc_users, impact=impact.get(nid, {}).get("level", "None")))
    # compiled scripts with no source in the index: still listed, so they can be seen and their impact checked
    ncs_only = [f["resref"] for f in files if f["ext"] == "ncs" and not any(
        s["name"] == f["resref"] for s in scripts)]
    for name in ncs_only:
        nid = f"script:{name}"
        scripts.append(dict(node=nid, name=name, role="Compiled only", summary="Compiled script with no source - "
                            "cannot be read or rebuilt", details=[], description=descriptions_override.get(name),
                            lines=0, compiled=True, functions=[], includes=[], triggers=[
                                dict(kind=k, by=g.label(s), via=v) for s, k, v in g.rev.get(nid, [])],
                            included_by=0, impact=impact.get(nid, {}).get("level", "None")))

    # ------------------------------------------------------------- objects / items
    nwn_progress.stage("analysis", "Analysis: objects and items")
    objects = [dict(zip(("id", "file_id", "node", "cls", "is_bp", "resref", "template", "tag", "name", "area",
                         "container", "path", "x", "y", "z"), r)) for r in db.execute("SELECT * FROM objects")]
    inst_by_template = defaultdict(list)        # (class, blueprint resref) -> placed copies of it
    for o in objects:
        if not o["is_bp"] and o["template"]:
            inst_by_template[(o["cls"], o["template"])].append(o)

    def quest_links(nid):
        """Quests connected to an object via scripts/conversations that reference it."""
        qs = set()
        refs = [s for s, k, v in g.rev.get(nid, [])]
        tag_nodes = [s for s, k, v in g.rev.get(nid, []) if k == "tag_provider"]
        for t in tag_nodes:
            refs += [s for s, k, v in g.rev.get(t, []) if k != "tag_provider"]
        users = set(refs)
        for u in list(users):  # conversations that run those scripts
            if u.startswith("script:"):
                users |= {s for s, k, v in g.rev.get(u, []) if k in ("dlg_script", "execute_script", "include")}
        for u in users:
            for d, k, v in g.fwd.get(u, []):
                if k == "quest_ref":
                    qs.add(d[6:])
        return sorted(qs), sorted(u for u in users if u.startswith(("script:", "dlg:", "inst:")))

    field_rows = defaultdict(list)
    for fid, path, label, value in db.execute(
            "SELECT fi.file_id, fi.path, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
            "WHERE f.ext='uti'"):
        field_rows[fid].append((path, label, value))
    items = []
    for o in objects:
        if not (o["is_bp"] and o["cls"] == "item"):
            continue
        fr = field_rows.get(o["file_id"], [])
        # top-level fields only (paths without "/" or "[" - not inside a list or struct)
        vals = {lab: v for p_, lab, v in fr if "/" not in p_ and "[" not in p_}
        # what the item does: every field except the cosmetic ones, so look-alikes with equal stats compare equal
        func_sig = sha(json.dumps(sorted((p_, v) for p_, lab, v in fr if lab not in ITEM_COSMETIC)))
        qs, users = quest_links(o["node"])
        placed = inst_by_template.get(("item", o["resref"]), [])
        carried = [s for s, k, v in g.rev.get(o["node"], []) if k == "inventory"]
        created = [s for s, k, v in g.rev.get(o["node"], []) if k == "spawns"]
        tagscript = [d for d, k, v in g.fwd.get(o["node"], []) if k == "tag_based_script"]
        items.append(dict(node=o["node"], resref=o["resref"], tag=o["tag"], name=o["name"],
                          base_item=vals.get("BaseItem"), cost=vals.get("Cost"), plot=vals.get("Plot"),
                          properties=len([1 for p_, lab, v in fr if lab == "PropertyName"]),
                          func_sig=func_sig, placed=len(placed),
                          locations=[dict(node=i["node"], where=g.breadcrumb(i["node"]),
                                          pos=[i["x"], i["y"], i["z"]] if i["x"] is not None else None)
                                     for i in placed][:100],
                          carried_by=[g.label(c) for c in carried], created_by=[g.label(c) for c in created],
                          referenced_by=[g.label(u) for u in users][:30], quests=qs,
                          tag_based_script=[t[7:] for t in tagscript],
                          impact=impact.get(o["node"], {}).get("level", "None")))
    # object class -> blueprint file extension, to find a placed object's blueprint node (bp:<template>.<ext>)
    _bpx = {'creature': 'utc', 'door': 'utd', 'placeable': 'utp', 'item': 'uti', 'store': 'utm', 'sound': 'uts',
            'trigger': 'utt', 'waypoint': 'utw', 'encounter': 'ute'}
    orphan_instances = [dict(node=o["node"], label=g.label(o["node"]), template=o["template"], cls=o["cls"],
                             area=o.get("area"), where=g.breadcrumb(o["node"]),
                             base=(f"{o['template'].lower()}.{_bpx.get(o['cls'], 'x')}" in base_names) if base_names else None)
                        for o in objects if not o["is_bp"] and o["template"] and
                        not g.nodes.get(f"bp:{o['template']}.{_bpx.get(o['cls'], 'x')}", {}).get("in_module")]
    # the ones that matter first (blueprint gone entirely), then by area, so the capped list keeps them
    orphan_instances.sort(key=lambda o: (o["base"], o.get("area") or "", o["where"]))

    # ------------------------------------------------------------- duplicates
    nwn_progress.stage("analysis", "Analysis: duplicates")
    dups = []
    gid = 0

    def refcount(nid):
        """How many things use nid (its own tag providing it doesn't count)."""
        return len([1 for s, k, v in g.rev.get(nid, []) if k not in ("tag_provider",)])

    compiled = {f["resref"] for f in module_files if f["ext"] == "ncs"}
    if nasher:   # a nasher project keeps sources only; nasher (and a build) compile them - see nwn_index.validate
        compiled |= {s_["name"] for s_ in scripts if s_["compiled"]}
    stub_cache = {}

    def is_stub(script_name):
        """A script whose main() body has no statements (e.g. 'void main() { }')."""
        if script_name not in stub_cache:
            row = db.execute("SELECT source FROM scripts WHERE name=?", (script_name,)).fetchone()
            code = nwn_index.lex_nss(row[0])[0] if row else ""       # source with comments removed
            # "void main() { ... }" as the last thing in the file; group 1 is the body. A file with anything after
            # main() (or no main) does not match and counts as not a stub - the safe answer.
            m = re.search(r"\bvoid\s+main\s*\(\s*\)\s*\{(.*?)\}\s*$", code, re.S)
            stub_cache[script_name] = bool(m) and not m.group(1).strip()
        return stub_cache[script_name]

    def owner_of(n_):
        """The file-level node that holds n_ (placed objects live in their area's .git)."""
        return "area:" + n_.split(":")[1] if n_.startswith("inst:") else n_

    pal_types = {"creature": "utc", "door": "utd", "encounter": "ute", "item": "uti", "placeable": "utp",
                 "sound": "uts", "store": "utm", "trigger": "utt", "waypoint": "utw"}   # = nwn_build.PALETTE_EXT

    def palette_lists(src, nid):
        """True when src is a palette file (.itp) of nid's own blueprint type: its mention of nid is the palette entry,
        which the build drops when nid is merged away (nwn_build.Rewriter._prune_palette)."""
        if not (src.startswith("file:") and src.endswith(".itp") and nid.startswith("bp:")):
            return False
        stem = src[5:-4].rsplit("/", 1)[-1].lower()
        return pal_types.get(re.sub(r"pal(cus|std)?$", "", stem)) == nid.rpartition(".")[2]

    def unrewritable_refs(nid):
        """Why this member can't be merged away: references the builder cannot re-point safely."""
        bad = []
        base = g.nodes.get(nid, {}).get("name", nid).split(".")[0]
        fr = dynamic_match(base)
        if fr:
            bad.append(f"name may be built at runtime (matches '{fr}')")
        if is_base(nid):
            bad.append("overrides a base-game resource")
        if nid.startswith("bp:"):
            dc = dyn_creates.get(nid.rpartition(".")[2], []) + dyn_creates.get("*", [])
            if dc:
                bad.append(f"a script creates this type from a name read at run time ({dc[0]}) - a saved name may be this one")
        for s_, k, v in g.rev.get(nid, []):
            if k == "tag_provider":
                continue
            if k in REWRITABLE_KINDS:
                continue
            if k == "spawns" and g.type(s_) in ("blueprint", "instance"):
                continue  # encounter CreatureList ResRef - rewritable
            if k == "name_ref" and palette_lists(s_, nid):
                continue  # the custom palette's entry for it: the build drops that entry (logged), the audit allows it
            if k == "name_ref" and any(owner_of(s2) == s_ and (k2 in REWRITABLE_KINDS or
                                                           (k2 == "spawns" and g.type(s2) in ("blueprint", "instance")))
                                       for s2, k2, v2 in g.rev.get(nid, [])):
                continue  # the file's mention is the same link as a rewritable field inside it
            bad.append(f"referenced by {g.label(s_)} ({k.replace('_', ' ')}{': ' + v if v else ''})")
        return bad

    def add_group(category, nodes, note, mergeable_default):
        """Record one duplicate group (2+ nodes): pick the keeper and decide whether it can be merged.

        category/note: text for the dashboard. mergeable_default: whether this kind of group can ever be merged
        automatically (identical content) - look-alike groups are always for review. A mergeable group is still
        blocked (with the reasons listed) when the keeper has no compiled code, no member is in use, the scripts
        are empty stubs, or another member can't be safely re-pointed (see unrewritable_refs)."""
        nonlocal gid
        if len(nodes) < 2:
            return
        gid += 1
        def is_compiled(n_):
            # only scripts can lack compiled code; n_[7:] strips "script:"
            return not n_.startswith("script:") or n_[7:] in compiled
        # keeper: in use first, then compiled scripts, then most referenced (node id breaks ties, so it is stable)
        members = sorted(({"node": n_, "label": g.label(n_), "refs": refcount(n_),
                           "impact": impact.get(n_, {}).get("level", "None"), "in_use": n_ in live,
                           "base_game": is_base(n_), "compiled": is_compiled(n_)} for n_ in nodes),
                         key=lambda m: (not m["in_use"], not m["compiled"], -m["refs"], m["node"]))
        keeper = members[0]["node"]
        blocked = []
        if mergeable_default:
            if not is_compiled(keeper):
                blocked.append("no member has a compiled .ncs - compile first")
            if not members[0]["in_use"]:
                blocked.append("no member is in use - delete instead of merging")
            if category.startswith("Script") and is_stub(keeper[7:]):
                blocked.append("empty/trivial scripts - merging would tie unrelated events to one file; delete unused ones instead")
            for m in members[1:]:
                b = unrewritable_refs(m["node"])
                if b:
                    blocked.append(f"{m['label']}: {'; '.join(b[:3])}")
        if blocked and "Safe to merge" in note:   # don't say "safe" next to the reasons it can't be merged
            note = note.split(" Safe to merge")[0] + " Not merged automatically: see why below."
        dups.append(dict(id=gid, category=category, note=note, members=members, keeper=keeper,
                         mergeable=bool(mergeable_default and not blocked), blocked=blocked))

    # identical blueprints / conversations (content minus resref/comment/palette)
    # content_hash is computed by the index pass with those fields left out
    by_hash = defaultdict(list)
    for f in module_files:
        if f["content_hash"] and (f["ext"] in ("uti", "utc", "utp", "utd", "ute", "utm", "uts", "utt", "utw", "dlg")):
            by_hash[(f["ext"], f["content_hash"])].append(f["node"])
    for (ext, h), nodes in by_hash.items():
        cat = "Conversation - identical" if ext == "dlg" else f"{'Item' if ext == 'uti' else 'Blueprint'} ({ext}) - identical"
        add_group(cat, nodes, "Same content, different resref (file name): separate files that make the same thing. Safe to merge: references are re-pointed to the keeper.", True)
    # items - same function, different name/look
    by_func = defaultdict(list)
    for it in items:
        by_func[it["func_sig"]].append(it["node"])
    # the groups so far (all "identical"): a look-alike group with exactly the same members would add nothing
    identical_sets = [set(d["members"][i]["node"] for i in range(len(d["members"]))) for d in dups]
    for sig, nodes in by_func.items():
        if len(nodes) > 1 and set(nodes) not in identical_sets:
            add_group("Item - same stats, different name/look", nodes,
                      "Identical properties/base item/cost but differs in name, tag or appearance. Review before merging.", False)
    by_tag = defaultdict(list)
    for it in items:
        if it["tag"]:
            by_tag[it["tag"]].append(it["node"])
    for tag, nodes in by_tag.items():
        if any(set(nodes) <= set(m["node"] for m in d["members"]) for d in dups if "identical" in d["category"]):
            continue
        add_group("Item - shared tag", nodes,
                  f"Several item blueprints use tag '{tag}'. Scripts that look items up by tag cannot tell them apart.", False)
    by_name = defaultdict(list)
    for it in items:
        if it["name"]:
            by_name[it["name"].strip().lower()].append(it["node"])
    for nm, nodes in by_name.items():
        if len(nodes) > 1 and not any(set(nodes) <= s for s in
                                      [set(m["node"] for m in d["members"]) for d in dups]):
            add_group("Item - same display name", nodes, f"Several items are called '{nm}'.", False)
    # scripts
    by_code = defaultdict(list)
    for (name, nh) in db.execute("SELECT name, norm_hash FROM scripts"):
        by_code[nh].append(f"script:{name}")
    for h, nodes in by_code.items():
        add_group("Script - identical code", nodes,
                  "Same code once comments/whitespace are ignored. Merge re-points events/conversations to the keeper.", True)
    # compiled-only / assets with identical bytes (models and textures: asset_duplicates below, which also looks in
    # the haks)
    by_sha = defaultdict(list)
    for f in module_files:
        if f["ext"] not in ("nss",) and f["ext"] not in n_gff_exts() and f["ext"] not in ASSET_DUP_EXTS and f["sha256"]:
            by_sha[(f["ext"], f["sha256"])].append(f["node"])
    for (ext, h), nodes in by_sha.items():
        if ext == "ncs":
            continue  # covered by script groups
        add_group(f"Asset ({ext}) - identical file", nodes,
                  "Byte-for-byte identical files under different names. Models/2das refer to assets by name, "
                  "so these are listed for manual review.", False)

    # how look-alike members differ from their keeper (colours, model parts, names, stats ...)
    try:
        import nwn_dupdiff
        for d in dups:
            if "identical" in d["category"] or not d["keeper"].startswith("bp:"):
                continue
            for m in d["members"]:
                if m["node"] == d["keeper"] or not m["node"].startswith("bp:"):
                    continue
                diffs = nwn_dupdiff.compare(db, d["keeper"], m["node"])
                if diffs is not None:
                    m["differs"] = nwn_dupdiff.summary(diffs, 6)
            d["differs_in"] = sorted({c for m in d["members"] for c in (m.get("differs") or {}).get("counts", {})})
    except Exception as ex:  # noqa - informational only
        if verbose:
            print("  duplicate differences failed:", ex)

    # ------------------------------------------------------------- safe to delete
    # Statuses Safe / Safe as group / Review: see the module docstring. dead = candidates: node -> its module files.
    nwn_progress.stage("analysis", "Analysis: safe to delete")
    dead = {}
    for nid, fl in files_by_node.items():
        fl = [f for f in fl if f["source"] == "module"]
        if not fl or nid in live:
            continue
        exts = {f["ext"] for f in fl}
        if exts & NEVER_DELETE_EXTS or nid == "module":
            continue
        dead[nid] = fl
    # a module we could not read completely can't prove anything is unused
    incomplete = []
    mod_rel = {f["relpath"].lower() for f in module_files}
    if "module.ifo" not in mod_rel:
        incomplete.append("the module has no module.ifo, so its areas, events and entry point are unknown")
    # nwnlib names an entry whose resource type number it doesn't know "t<number>" (e.g. foo.t2099)
    odd = sorted(f["relpath"] for f in module_files if re.fullmatch(r"t\d+", f["ext"] or ""))
    if odd:
        incomplete.append(f"{len(odd)} entr{'y has' if len(odd) == 1 else 'ies have'} an unknown resource type "
                          f"({', '.join(odd[:3])}{' …' if len(odd) > 3 else ''}) - the file may be damaged")
    dup_rel = Counter(f["relpath"].lower() for f in module_files)
    for rel_, n_ in sorted(dup_rel.items()):
        if n_ > 1:
            issues.append(dict(severity="warning", category="duplicate_entry", node="module", label=rel_,
                               detail=f"the module contains {n_} files called {rel_} (names differ only in capitals, or "
                                      "the same entry twice) - the game loads only one of them; which one is not defined"))
    for why in incomplete:
        issues.append(dict(severity="error", category="module_incomplete", node="module", label="module",
                           detail=why + ". Nothing is marked safe to delete until this is fixed."))
    # Ways the game can run or create a candidate that no reference shows (rule: when unsure, keep). Each one gives a
    # Review reason in the loop below.
    #  - a tag-based item-event prefix set from a value the index could not read: a script named <prefix><item tag>
    tag_unknown = json.loads(meta.get("tagbased_prefix_unknown") or "[]")
    item_tags = {r[0].lower() for r in db.execute("SELECT DISTINCT tag FROM objects WHERE class='item' AND tag<>''")} \
        if tag_unknown else set()
    #  - Create* calls whose blueprint name is read at run time (dyn_creates, see "load" above)
    #  - NWNX can run a script named only in the server's environment; without those settings loaded, any unused
    #    script might be one
    nwnx_unchecked = not server_cfg_loaded and any(d_.startswith("script:nwnx_") for s_, d_, k_, v_ in
                                                   db.execute("SELECT * FROM edges WHERE kind='include'"))
    #  - the toolset palettes: a blueprint a palette lists is in the DMs' creator, so they can spawn it in game.
    #    palette_lists: blueprint ext -> resrefs the module's palettes of that type list
    from nwn_build import PALETTE_EXT
    pal_type = lambda resref_: PALETTE_EXT.get(re.sub(r"pal(cus|std)?$", "", resref_))   # noqa: E731 itempalcus -> uti
    palette_lists = {pal_type(f["resref"]): set() for f in module_files if f["ext"] == "itp" and pal_type(f["resref"])}
    for pal_, v_ in db.execute("SELECT x.resref, f.value FROM fields f JOIN files x ON x.id=f.file_id JOIN sources s "
                               "ON s.id=x.source_id WHERE x.ext='itp' AND s.kind='module' AND f.label='RESREF'"):
        if pal_type(pal_):
            palette_lists[pal_type(pal_)].add((v_ or "").lower())
    deletions = []
    for nid, fl in sorted(dead.items()):
        nd = g.nodes.get(nid, {})
        name = nd.get("name", nid)
        base = name.split(".")[0]
        referrers = sorted({s for s, k, v in g.rev.get(nid, []) if k != "tag_provider"} - {nid})
        dead_refs = [r for r in referrers if r in dead]       # users that are candidates too -> "Safe as group"
        # users that are not candidates (not deletable module files) - a person should judge that link -> Review
        live_refs = [r for r in referrers if r not in dead and not r.startswith(("tag:", "var:"))]
        review = []
        fr = dynamic_match(base)
        if fr:
            review.append(f"name matches '{fr}', a piece of a name that scripts/variables build at runtime")
        if is_base(nid):
            review.append("same name as a base-game resource - this file overrides the game's version")
        elif nd.get("type") in ("asset", "model") and not base_names:
            review.append("NWN install not configured, so it can't be confirmed this asset doesn't override a "
                          "base-game file (set 'NWN install folder' and re-analyse)")
        if incomplete:
            review.append("module not read completely: " + incomplete[0])
        if haks_missing and nd.get("type") in ("script", "model", "asset", "2da"):
            # hak 2das can name scripts/models/textures
            review.append("hak(s) not loaded: " + ", ".join(haks_missing[:3]) +
                          " - their 2da tables could refer to this by name")
        elif haks_missing and nd.get("type") == "conversation":
            # tag-based item scripts often live in haks and open a module conversation by name (an emote wand's
            # script opening emotewand.dlg); blueprints stay Safe - hak content refers to its own blueprints
            review.append("hak(s) not loaded: " + ", ".join(haks_missing[:3]) +
                          " - a script in them (e.g. an item's tag-based script) could start this conversation by name")
        if nd.get("type") == "area":
            review.append("whole area - not in the module area list and nothing links to it; confirm it is really retired")
        for rx, why in ASSET_REVIEW_PATTERNS:
            if nd.get("type") in ("asset", "model") and rx.search(base):
                review.append(why)
                break
        if live_refs:
            review.append("mentioned by: " + ", ".join(g.label(r) for r in live_refs[:3]))
        note = ""
        if nd.get("type") == "script":
            t_ = next((t for t in sorted(item_tags, key=len, reverse=True) if len(base) > len(t) and base.endswith(t)), None)
            if t_:
                review.append(f"may be a tag-based item script: {tag_unknown[0]} sets the item-event prefix from a value "
                              f"the analysis can't read, and this name ends in the item tag '{t_}' - the game runs "
                              "<prefix><tag> when that item is used")
            if nwnx_unchecked:
                review.append("the module uses NWNX (#include nwnx_...) and no server settings are loaded: NWNX can run "
                              "a script named only in the server's environment - load them on the Overview page and "
                              "re-analyse")
        if nd.get("type") == "blueprint":
            bext = name.rpartition(".")[2]
            dc = dyn_creates.get(bext, []) + dyn_creates.get("*", [])
            if dc:
                review.append(f"a script creates {nwn_index.EXT_CLASS.get(bext, bext)} objects from a name it reads at "
                              f"run time ({dc[0]}) - a saved or configured name (a storage chest, a bank) may be this one")
            if base in palette_lists.get(bext, ()):
                review.append("only in the toolset palette (DMs can spawn it)")
            elif bext not in palette_lists:
                review.append(f"the module has no {nwn_index.EXT_CLASS.get(bext, bext)} palette file, so it can't be "
                              "checked whether DMs can spawn it from the creator")
            else:
                note = "Not placed, not created by any script, and not in the toolset palette."
        # general reasons hold for every candidate of a kind at once (NWNX settings not loaded, haks missing, a script
        # creating objects from saved names, the DMs' palette, base-game overrides): they can't be settled one item at a
        # time, so an item with only those is "likely in use" - kept, and listed apart from the ones worth a look
        general_only = bool(review) and all(r_.startswith(GENERAL_REVIEW) or
                                            (r_.startswith("mentioned by: ") and all(x.endswith(".itp") for x in
                                                                                      (g.label(lr) for lr in live_refs)))
                                            for r_ in review)
        if review:
            status = "Review"
        elif dead_refs:
            status = "Safe as group"
            note = (note + " " if note else "") + "Only used by other unused files: " + ", ".join(g.label(r) for r in dead_refs[:5])
        else:
            status = "Safe"
        deletions.append(dict(node=nid, label=g.label(nid), type=nd.get("type", "?"), status=status,
                              reason="; ".join(review), note=note,
                              files=[f["relpath"] for f in fl], bytes=sum(f["size"] or 0 for f in fl),
                              group=dead_refs, general=general_only))
    # 'Safe as group' only holds while everything that uses it can go too: a user marked Review makes it Review
    # (repeated until nothing changes, so Review spreads along whole chains of users)
    by_node = {d["node"]: d for d in deletions}
    changed = True
    while changed:
        changed = False
        for d in deletions:
            if d["status"] != "Safe as group":
                continue
            rv = [u for u in d["group"] if by_node.get(u, {}).get("status") == "Review"]
            if rv:
                d["status"] = "Review"
                # only held back by users that are likely in use: likely in use too
                d["general"] = not d["reason"] and all(by_node[u].get("general") for u in rv)
                d["reason"] = "; ".join(filter(None, [d["reason"], "used by " + ", ".join(g.label(u) for u in rv[:3]) +
                                                      ", which is marked Review - decide on that first"]))
                changed = True

    # ------------------------------------------------------------- areas / dlg / quests
    nwn_progress.stage("analysis", "Analysis: areas, conversations, quests")
    obj_by_area = defaultdict(list)
    for o in objects:
        if not o["is_bp"] and o["area"]:
            obj_by_area[o["area"]].append(o)
    tag_area = {}                       # tag -> area of the first placed object with that tag (transition targets)
    for o in objects:
        if not o["is_bp"] and o["tag"]:
            tag_area.setdefault(o["tag"], o["area"])
    listed = {d for d, k, v in g.fwd.get("module", []) if k == "module_area"}
    areas = []
    for nid, nd in g.nodes.items():
        if nd["type"] != "area" or not nd["in_module"]:
            continue
        ar = nd["name"]
        objs = obj_by_area.get(ar, [])
        counts = defaultdict(int)
        for o in objs:
            counts[o["cls"]] += 1
        transitions = []
        for o in objs:
            for d, k, v in g.fwd.get(o["node"], []):
                if k == "transition_tag":
                    transitions.append(dict(frm=g.short_label(o["node"]), to_tag=d[4:], to_area=tag_area.get(d[4:]),
                                            kind="door/trigger"))
            # scripted jumps: a script run by this object that looks up a tag placed in another area
            for d, k, v in g.fwd.get(o["node"], []):
                if k != "event_script":
                    continue
                for d2, k2, v2 in g.fwd.get(d, []):
                    if k2 == "tag_ref" and tag_area.get(d2[4:]) not in (None, ar):
                        transitions.append(dict(frm=f"{g.short_label(o['node'])} via {d[7:]}", to_tag=d2[4:],
                                                to_area=tag_area[d2[4:]], kind="script"))
        areas.append(dict(node=nid, resref=ar, label=nd["label"], in_module_list=nid in listed,
                          counts=dict(counts), scripts=[dict(event=v, script=d[7:]) for d, k, v in g.fwd.get(nid, [])
                                                        if k == "event_script"],
                          transitions=transitions, impact=impact.get(nid, {}).get("level", "None")))
    convs = []

    def speaker_areas(nid):
        """Areas where the NPCs / placeables that use this conversation stand (placed copies of blueprints too)."""
        out = set()
        for s_, k, v in g.rev.get(nid, []):
            if k != "uses_conversation":
                continue
            owners = [s_] if s_.startswith("inst:") else [
                i_ for i_, k2, v2 in g.rev.get(s_, []) if k2 == "template" and
                # a placed copy keeps its own Conversation field: it only speaks this one if it didn't change it
                not any(k3 == "uses_conversation" and d3 != nid for d3, k3, v3 in g.fwd.get(i_, []))]
            for o in owners[:200]:
                if o.startswith("inst:"):
                    out.add(g.label("area:" + o.split(":", 2)[1]))
        return sorted(out)
    # scripts that start a conversation by name (ActionStartConversation / BeginConversation / StartConversation), or that
    # hold its name in a string (SetLocalString(o, "sConv", "cf_x") - often started later from that variable)
    def script_runs(sname):
        """How a script gets run, e.g. 'OnUsed of Lever [inst...]' - the first few triggers."""
        out = []
        for s_, k, v in g.rev.get(f"script:{sname}", []):
            if k in ("event_script", "module_event", "dlg_script", "execute_script", "tag_based_script"):
                out.append(f"{(v or k.replace('_', ' ')).split('/')[-1]} of {g.label(s_)}" if k in ("event_script", "module_event")
                           else f"{k.replace('_', ' ')} from {g.label(s_)}")
        return out
    starters = defaultdict(list)        # conversation name (lower case) -> scripts that start or name it
    for sc_, line_, func_, lit_, kind_ in db.execute("SELECT script, line, func, literal, kind FROM script_literals "
                                                   "WHERE kind IN ('conversation', 'conversation?')"):
        if kind_ == "conversation?" and (func_ or "") in ("if", "while", "switch", "case", "for", "return", ""):
            continue      # if (sCmd == "WAITRESS") compares text; it doesn't name a conversation
        runs = script_runs(sc_)
        starters[(lit_ or "").lower()].append(dict(
            script=sc_, line=line_, how="starts it" if kind_ == "conversation" else f"names it ({func_ or 'a string'})",
            runs=runs[:3], more_runs=max(0, len(runs) - 3), live=f"script:{sc_}" in live))
    # starts from a name held in a variable: can't be linked to one conversation, listed so they aren't missed
    dyn_starts = []
    # a call to one of the conversation-starting functions, e.g. "ActionStartConversation(oPC, sConv)"; _argpos says
    # which argument (0-based) holds the conversation name for each function
    _call = re.compile(r"\b(ActionStartConversation|BeginConversation|StartConversation)\s*\(")
    _argpos = {"ActionStartConversation": 1, "BeginConversation": 0, "StartConversation": 2}
    for sc_, src_ in db.execute("SELECT name, source FROM scripts WHERE source LIKE '%Conversation%'"):
        src_lines = (src_ or "").splitlines()
        for ln, text in enumerate(src_lines, 1):
            if text.lstrip().startswith("//"):
                continue
            for mm in _call.finditer(text):
                # split the arguments at top-level commas, counting brackets so f(a, b) inside stays one argument.
                # A call whose closing bracket is on a later line is joined with up to 8 following lines (limit:
                # a longer call, or one with a // comment inside the arguments, is cut short)
                call_text = text
                for extra in src_lines[ln:ln + 8]:
                    rest = call_text[mm.end():]
                    if rest.count(")") > rest.count("("):     # the call's own closing bracket is already there
                        break
                    call_text += " " + extra.strip()
                depth, args, cur = 1, [], ""
                for ch in call_text[mm.end():]:
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    if ch == "," and depth == 1:
                        args.append(cur.strip()); cur = ""
                    else:
                        cur += ch
                args.append(cur.strip())
                pos = _argpos[mm.group(1)]
                arg = args[pos] if len(args) > pos else ""
                if arg and not arg.startswith('"') and "=" not in arg:     # a name held in a variable/expression
                    # string s = "cf_x"; ... BeginConversation(s): the last literal assigned above the call
                    lits_ = []
                    if re.fullmatch(r"[A-Za-z_]\w*", arg):
                        above = "\n".join(src_.splitlines()[:ln])
                        lits_ = re.findall(r"\b" + re.escape(arg) + r"\s*=\s*\"([^\"\n]+)\"\s*;", above)
                    if lits_:
                        cname = lits_[-1].lower()
                        starters[cname].append(dict(script=sc_, line=ln, how="starts it", runs=script_runs(sc_)[:3],
                                                    more_runs=0, live=f"script:{sc_}" in live, via=f"{arg} = \"{lits_[-1]}\""))
                        if f"dlg:{cname}" not in g.nodes or not g.nodes[f"dlg:{cname}"].get("in_module"):
                            issues.append(dict(severity="error" if f"script:{sc_}" in live else "warning",
                                               category="missing_conversation", node=f"script:{sc_}", label=sc_,
                                               detail=f"Conversation '{cname}' not found; {sc_} line {ln} starts it "
                                                      f"(through {arg})" + not_loaded_hint))
                        continue
                    dyn_starts.append(dict(script=sc_, line=ln, call=f"{mm.group(1)}(… {arg} …)"[:120],
                                           runs=script_runs(sc_)[:3], live=f"script:{sc_}" in live))
    for fid, name, ne, nr, ns, words in db.execute("SELECT * FROM conversations"):
        nid = f"dlg:{name}"
        convs.append(dict(node=nid, name=name, entries=ne, replies=nr, starts=ns, words=words, areas=speaker_areas(nid),
                          started_by=starters.get(name.lower(), [])[:20],
                          placed_speakers=sum(1 for s_, k, v in g.rev.get(nid, []) if k == "uses_conversation" and s_.startswith("inst:")),
                          blueprint_speakers=sum(1 for s_, k, v in g.rev.get(nid, []) if k == "uses_conversation" and s_.startswith("bp:")),
                          used_by=[g.label(s) for s, k, v in g.rev.get(nid, []) if k in ("uses_conversation", "conversation_ref")],
                          scripts=sorted({d[7:] for d, k, v in g.fwd.get(nid, []) if k == "dlg_script"}),
                          quests=sorted({d[6:] for d, k, v in g.fwd.get(nid, []) if k == "quest_ref"}),
                          impact=impact.get(nid, {}).get("level", "None")))
    # ------------------------------------------------------------- quests: health + needed items
    item_by_tag = defaultdict(list)
    item_by_res = {}
    for it in items:
        item_by_res[it["resref"]] = it
        if it["tag"]:
            item_by_tag[it["tag"]].append(it)
    # every literal a script/dialogue uses, so we can tell what a quest needs
    lit_by_owner = defaultdict(list)
    for sc, line, func, lit, kind in db.execute("SELECT script, line, func, literal, kind FROM script_literals"):
        lit_by_owner[f"script:{sc}"].append((func or "", lit, kind or ""))

    def obtainable(it):
        """Can a player get this item at all: placed, carried/sold by something, or created by a script?"""
        return bool(it["placed"] or it["carried_by"] or it["created_by"])

    def quest_users(qnode):
        """scripts and conversations that update/read this quest, plus scripts those conversations run"""
        users = {s_ for s_, k, v in g.rev.get(qnode, []) if k == "quest_ref"}
        for u in list(users):
            if u.startswith("dlg:"):
                users |= {d_ for d_, k, v in g.fwd.get(u, []) if k == "dlg_script"}
        return users

    def quest_needs(users):
        """items the quest's scripts/dialogues look for or hand out, with their status"""
        needs = {}
        for u in users:
            for func, lit, kind in lit_by_owner.get(u, []):
                # GetItemPossessedBy(o, sTag) looks an item up by tag; GetObjectByTag-style calls count only when the
                # tag is an item's (they find anything). "tag?" is a literal the index could only match to a tag.
                if func == "GetItemPossessedBy" or kind == "tag?" or \
                        (func in ("GetObjectByTag", "GetNearestObjectByTag") and lit in item_by_tag):
                    cands = item_by_tag.get(lit, [])
                    key = ("tag", lit)
                    if not cands:
                        needs[key] = dict(kind="tag", name=lit, status="missing", detail="no item in the module has this tag",
                                          by=g.label(u))
                    elif not any(obtainable(c) for c in cands):
                        needs[key] = dict(kind="tag", name=lit, status="unobtainable", by=g.label(u),
                                          detail="blueprint exists (" + ", ".join(c["resref"] for c in cands[:3]) +
                                                 ") but it is never placed, carried, sold or created by a script")
                    else:
                        needs[key] = dict(kind="tag", name=lit, status="ok", by=g.label(u),
                                          detail=", ".join(c["resref"] for c in cands[:3]))
                elif func in ("CreateItemOnObject", "CreateItemOnObjectVoid") or kind == "blueprint?":
                    it = item_by_res.get(lit.lower())
                    key = ("resref", lit.lower())
                    if it is None and f"{lit.lower()}.uti" not in base_names:
                        needs[key] = dict(kind="resref", name=lit, status="missing", by=g.label(u),
                                          detail="no item blueprint with this resref in the module")
                    else:
                        needs[key] = dict(kind="resref", name=lit, status="ok", by=g.label(u),
                                          detail="created by script" if it else "base-game item")
        return list(needs.values())

    quests = []
    journal_tags = set()
    for tag, name, fid, nent in db.execute("SELECT * FROM quests"):
        journal_tags.add(tag)
        nid = f"quest:{tag}"
        ents = [dict(id=i, text=t, end=bool(e)) for _, i, t, e in
                db.execute("SELECT * FROM quest_entries WHERE tag=?", (tag,))]
        updaters = [s_ for s_, k, v in g.rev.get(nid, []) if k == "quest_ref"]
        users = quest_users(nid)
        needs = quest_needs(users)
        linked_items = [it["node"] for it in items if tag in it["quests"]]
        problems = []
        if not updaters:
            problems.append("nothing in the module updates this quest (journal entry can never appear)")
        elif not any(u in live for u in updaters):
            problems.append("only updated by scripts/conversations that are themselves unused (legacy quest?)")
        for nd_ in needs:
            if nd_["status"] != "ok":
                problems.append(f"needs item {nd_['name']}: {nd_['status']} - {nd_['detail']}")
        ends = [e for e in ents if e["end"]]
        if ents and not ends:
            problems.append("no entry is marked as finishing the quest")
        quests.append(dict(node=nid, tag=tag, name=name, entries=ents, in_journal=True,
                           updated_by=[g.label(u) for u in updaters],
                           updated_by_live=[g.label(u) for u in updaters if u in live],
                           items=[g.label(i) for i in linked_items], needs=needs, problems=problems,
                           health="ok" if not problems else ("broken" if any("missing" in p_ or "nothing" in p_ for p_ in problems) else "warning"),
                           impact=impact.get(nid, {}).get("level", "None")))
    # quests referenced but not in the journal
    for nid, nd in g.nodes.items():
        if nd["type"] == "quest" and nd["name"] not in journal_tags:
            users = quest_users(nid)
            quests.append(dict(node=nid, tag=nd["name"], name="(not in journal)", entries=[], in_journal=False,
                               updated_by=[g.label(u) for u in users], updated_by_live=[g.label(u) for u in users if u in live],
                               items=[], needs=quest_needs(users),
                               problems=["referenced by scripts/dialogue but there is no journal category with this tag"
                                         + ("" if any(u in live for u in users) else " (and none of them are in use - legacy leftover)")],
                               health="broken" if any(u in live for u in users) else "legacy", impact="None"))

    # ------------------------------------------------------------- inferred quests (no journal: tokens, variables, DB)
    nwn_progress.stage("analysis", "Analysis: quests that don't use the journal")
    try:
        import nwn_quests
        inferred = nwn_quests.infer_from_index(db, g, items, live, base_names)
    except Exception as ex:  # noqa - never let quest inference stop the analysis
        inferred = dict(summary=dict(error=f"quest inference failed: {ex}", quests=0), quests=[], hubs=[])
        if verbose:
            print("  quest inference failed:", ex)

    # ------------------------------------------------------------- orphan clusters (legacy quest leftovers)
    nwn_progress.stage("analysis", "Analysis: orphans")
    orphan_nodes = [d["node"] for d in deletions if d["type"] in ("script", "conversation")]
    orphan_set = set(orphan_nodes)
    feats = {}
    for o in orphan_nodes:
        f_ = set()
        for d_, k, v in g.fwd.get(o, []):
            if k in ("quest_ref",):
                f_.add("quest:" + d_[6:])
            elif k in ("tag_ref", "literal_match", "speaker_tag", "key_tag") and d_.startswith("tag:"):
                f_.add("tag:" + d_[4:])
            elif k in ("include", "dlg_script", "execute_script", "uses_conversation", "conversation_ref") and d_ in orphan_set:
                f_.add("link:" + d_)
        for s_, k, v in g.rev.get(o, []):
            if k in ("include", "dlg_script", "execute_script", "uses_conversation", "conversation_ref") and s_ in orphan_set:
                f_.add("link:" + s_)
        nm = g.nodes[o]["name"]
        # name stem: the word after a short prefix (at_oldmine, sc_oldmine, q_oldmine_chk -> 'oldmine')
        m_ = re.match(r"^[a-z0-9]{1,3}_([a-z]{4,})", nm)
        if m_:
            f_.add("stem:" + m_.group(1))
        for q_ in [x[6:] for x in f_ if x.startswith("quest:")]:
            mq = re.match(r"^(?:[a-z0-9]{1,3}_)?([a-z]{4,})", q_)
            if mq:
                f_.add("stem:" + mq.group(1))
        feats[o] = f_
    # union-find on shared features (quest/tag/link first, name prefix only as a weak tie)
    parent = {o: o for o in orphan_nodes}

    def find(x):
        """Union-find root of x, halving the path as it goes (keeps later lookups short)."""
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    by_feat = defaultdict(list)
    for o, fs in feats.items():
        for f_ in fs:
            by_feat[f_].append(o)
    for f_, members in by_feat.items():
        if f_.startswith("stem:") and len(members) < 2:
            continue
        for m_ in members[1:]:
            parent[find(m_)] = find(members[0])
    clusters = defaultdict(list)
    for o in orphan_nodes:
        clusters[find(o)].append(o)
    orphans = []
    for cid, members in clusters.items():
        qs = sorted({f_[6:] for m_ in members for f_ in feats[m_] if f_.startswith("quest:")})
        tags = sorted({f_[4:] for m_ in members for f_ in feats[m_] if f_.startswith("tag:")})
        hints = []
        for q_ in qs:
            if q_ in journal_tags:
                qq = next(x for x in quests if x["tag"] == q_)
                hints.append(f"journal quest '{q_}' still exists" + (" but nothing in use updates it" if not qq["updated_by_live"] else " and is still updated by live scripts - check before deleting"))
            else:
                hints.append(f"quest '{q_}' is not in the journal (removed quest)")
        for t_ in tags:
            its = item_by_tag.get(t_)
            if its and not any(obtainable(i) for i in its):
                hints.append(f"item tag '{t_}' exists only as an unplaced blueprint (" + ", ".join(i["resref"] for i in its[:2]) + ")")
            elif not its and f"tag:{t_}" in g.nodes and not g.nodes[f"tag:{t_}"]["in_module"]:   # no object carries it
                hints.append(f"tag '{t_}' matches nothing in the module")
        kind = "legacy quest" if qs else ("linked group" if len(members) > 1 else "single")
        orphans.append(dict(id=len(orphans) + 1, kind=kind, members=[dict(node=m_, label=g.label(m_), type=g.type(m_),
                                                                          status=by_node.get(m_, {}).get("status", "?"))
                                                                     for m_ in sorted(members)],
                            quests=qs, tags=tags, hints=hints, size=len(members)))
    orphans.sort(key=lambda o: (0 if o["kind"] == "legacy quest" else 1, -o["size"]))

    # ------------------------------------------------------------- creature AI health
    nwn_progress.stage("analysis", "Analysis: creature AI")
    # A creature that "stands there and ignores you" nearly always has an empty or non-AI script in one of these slots.
    AI_SLOTS = {"ScriptAttacked": "OnPhysicalAttacked", "ScriptDamaged": "OnDamaged", "ScriptOnNotice": "OnPerception",
                "ScriptEndRound": "OnCombatRoundEnd", "ScriptSpawn": "OnSpawn", "ScriptHeartbeat": "OnHeartbeat",
                "ScriptDeath": "OnDeath", "ScriptDialogue": "OnConversation", "ScriptDisturbed": "OnDisturbed",
                "ScriptRested": "OnRested", "ScriptSpellAt": "OnSpellCastAt", "ScriptUserDefine": "OnUserDefined",
                "ScriptOnBlocked": "OnBlocked"}
    COMBAT_SLOTS = ("ScriptAttacked", "ScriptDamaged", "ScriptOnNotice", "ScriptEndRound")
    AI_INCLUDES = ("nw_i0_generic", "x0_i0_", "x2_i0_", "nw_i0_", "x0_inc_", "x2_inc_", "j_inc_", "j_ai_", "hench_i0_",
                   "ms_i0_", "gen_ai", "tk_", "tonyk", "ak_inc")
    AI_CALLS = ("DetermineCombatRound", "DetermineSpecialBehavior", "ExecuteScript", "ActionAttack", "ClearAllActions",
                "GetAttackTarget", "SetCombatCondition", "RespondToShout", "ActionMoveToObject")
    src_cache = {r[0]: r[1] for r in db.execute("SELECT name, source FROM scripts")}
    inc_cache = defaultdict(set)
    for s_, d_, k_, v_ in db.execute("SELECT * FROM edges WHERE kind='include'"):
        inc_cache[s_[7:]].add(d_[7:])

    def includes_ai(name, seen=None):
        """Does this script, or anything it #includes, have a name that marks a known combat-AI library?"""
        seen = seen or set()
        if name in seen:
            return False
        seen.add(name)
        if name.startswith(AI_INCLUDES):
            return True
        return any(includes_ai(i, seen) for i in inc_cache.get(name, ()))

    def script_has_ai(name):
        """Does this event script hand control to the combat AI (directly or via includes)?
        True / False, or None when there is no source to read. Base-game scripts and the stock creature/henchman
        default-script families (nw_c2_, x2_def_ ...) are taken as AI scripts. A heuristic, so findings are warnings."""
        if is_base(f"script:{name}") or name.startswith(("nw_c2_", "x2_def_", "x0_ch_", "nw_ch_", "x2_ch_", "hen_", "nw_g0_")):
            return True
        src = src_cache.get(name)
        if src is None:
            return None  # compiled only / missing: unknown
        code = nwn_index.lex_nss(src)[0]
        if includes_ai(name) or any(re.search(r"\b" + fn + r"\s*\(", code) for fn in AI_CALLS):
            return True
        return False
    ai_findings = []
    creature_slots = defaultdict(dict)
    for relpath, node, label, value in db.execute(
            "SELECT f.relpath, o.node, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
            "JOIN objects o ON o.file_id=f.id AND o.is_blueprint=1 WHERE f.ext='utc' AND fi.path=fi.label AND fi.label LIKE 'Script%'"):
        creature_slots[node][label] = (value or "").lower()
    for o in objects:  # placed creatures: their own slots (full copies)
        if o["is_bp"] or o["cls"] != "creature":
            continue
        for d_, k_, v_ in g.fwd.get(o["node"], []):
            if k_ == "event_script":
                creature_slots[o["node"]][v_.split("/")[-1]] = d_[7:]
        creature_slots.setdefault(o["node"], {})
    for node, slots in creature_slots.items():
        if node.startswith("inst:") and not slots:
            continue
        problems = []
        empty = [AI_SLOTS[s_] for s_ in COMBAT_SLOTS if s_ in slots and not slots[s_]]
        if node.startswith("bp:") and all(s_ in slots for s_ in COMBAT_SLOTS) and empty:
            problems.append("empty combat event(s): " + ", ".join(empty) + " - will not react to being attacked")
        for s_ in COMBAT_SLOTS:
            sc = slots.get(s_)
            if not sc:
                continue
            has = script_has_ai(sc)
            if has is False:
                problems.append(f"{AI_SLOTS[s_]} runs '{sc}' which never calls the combat AI (no DetermineCombatRound / "
                                f"ExecuteScript / AI include) - creature may ignore attacks")
        if problems:
            ai_findings.append(dict(node=node, label=g.label(node), where=g.breadcrumb(node) if node.startswith("inst:") else "Palette (blueprint)",
                                    problems=problems, slots={AI_SLOTS.get(k_, k_): v_ for k_, v_ in slots.items()}))
            for p_ in problems:
                issues.append(dict(severity="warning", category="creature_ai", node=node, label=g.label(node), detail=p_))

    # ------------------------------------------------------------- skins & hides (creature items)
    nwn_progress.stage("analysis", "Analysis: skins and hides")
    # baseitems.2da rows of creature-only items (weapons and the skin/hide), extended below from any baseitems.2da
    # in the index whose label marks a creature item
    CREATURE_BASE = {38: "creature slashing weapon", 39: "creature piercing weapon", 40: "creature bludgeoning weapon",
                     41: "creature slashing/piercing weapon", 42: "creature item (skin/hide)"}
    for r_, c_, v_ in db.execute("SELECT row, col, value FROM twoda WHERE name='baseitems' AND col='label'"):
        try:
            if v_ and v_.lower().startswith("c") and ("creature" in v_.lower() or v_.lower() in ("cskin", "chide")):
                CREATURE_BASE.setdefault(int(r_), v_)
        except ValueError:
            pass
    # equipment slot bit flags, stored as the struct IDs of a creature's Equip_ItemList; the four highest are the
    # creature-only slots
    SLOT_NAMES = {131072: "creature hide", 16384: "creature weapon (left)", 32768: "creature weapon (right)",
                  65536: "creature weapon (bite)", 1: "head", 2: "chest", 4: "boots", 8: "arms", 16: "right hand",
                  32: "left hand", 64: "cloak", 128: "left ring", 256: "right ring", 512: "neck", 1024: "belt",
                  2048: "arrows", 4096: "bullets", 8192: "bolts"}
    file_id_by_node = {}            # first object row per node (a blueprint in two sources has two rows)
    for o in objects:
        file_id_by_node.setdefault(o["node"], o["file_id"])
    equips = defaultdict(list)
    for owner, item, slot, full in db.execute("SELECT * FROM equips"):
        equips[item].append((owner, slot, bool(full)))
    props_by_file = defaultdict(list)
    for fid, path, label, value in db.execute(
            "SELECT fi.file_id, fi.path, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
            "WHERE f.ext='uti' AND fi.label IN ('PropertyName','Subtype','CostValue','Param1Value') AND fi.path LIKE 'PropertiesList%'"):
        props_by_file[fid].append((path, label, value))
    skins = []
    skin_res = set()
    for it in items:
        try:
            bi = int(it["base_item"]) if it["base_item"] not in (None, "") else -1
        except ValueError:
            bi = -1
        eq = equips.get(it["resref"], [])
        hide_slots = [e for e in eq if e[1] in (131072, 16384, 32768, 65536)]
        if bi not in CREATURE_BASE and not hide_slots:
            continue
        skin_res.add(it["resref"])
        fid = file_id_by_node.get(it["node"])
        props = defaultdict(dict)
        for path, label, value in props_by_file.get(fid, []):
            props[path.split("/")[0]][label] = value
        skins.append(dict(node=it["node"], resref=it["resref"], name=it["name"], tag=it["tag"],
                          type=CREATURE_BASE.get(bi, f"equipped in a creature slot (base item {bi})"), base_item=bi,
                          properties=[f"prop {v.get('PropertyName')} sub {v.get('Subtype', '')} val {v.get('CostValue', '')}"
                                      for k_, v in sorted(props.items())],
                          property_count=len(props),
                          worn_by=[dict(node=o_, label=g.label(o_), slot=SLOT_NAMES.get(sl, str(sl)),
                                        where=g.breadcrumb(o_) if o_.startswith("inst:") else "Palette (blueprint)",
                                        full_copy=full) for o_, sl, full in eq],
                          created_by=it["created_by"], referenced_by=it["referenced_by"][:10],
                          in_use=it["node"] in live, impact=it["impact"]))
    # hides that creatures wear but which are not in the module
    for item, eq in equips.items():
        if item in item_by_res or f"{item}.uti" in base_names:
            continue
        if any(sl in (131072, 16384, 32768, 65536) for _, sl, _ in eq):
            unknown_base = not base_names and item.startswith(BASE_GAME_PREFIXES)
            skins.append(dict(node=f"bp:{item}.uti", resref=item, name="(base game?)" if unknown_base else "(missing)", tag="",
                              type="not in module - probably base game (set the NWN install folder to confirm)" if unknown_base
                              else "MISSING creature item",
                              base_item=-1, properties=[], property_count=0,
                              worn_by=[dict(node=o_, label=g.label(o_), slot=SLOT_NAMES.get(sl, str(sl)),
                                            where=g.breadcrumb(o_) if o_.startswith("inst:") else "Palette (blueprint)",
                                            full_copy=full) for o_, sl, full in eq],
                              created_by=[], referenced_by=[], in_use=True, impact="None"))
            if not unknown_base:
                issues.append(dict(severity="error", category="missing_creature_item", node=f"bp:{item}.uti", label=f"{item}.uti",
                                   detail=f"creature hide/weapon '{item}' is equipped by {len(eq)} creature(s) but the blueprint "
                                          "is not in the module (base game/hak, or deleted)"))
    skins.sort(key=lambda x: (not x["type"].startswith("MISSING"), -len(x["worn_by"]), x["resref"]))

    # ------------------------------------------------------------- load & performance
    nwn_progress.stage("analysis", "Analysis: load and performance")
    hb_objects = [o for o in objects if not o["is_bp"] and any(v.split("/")[-1] in ("ScriptHeartbeat", "OnHeartbeat")
                                                                and not d_[7:].startswith(("nw_c2_default1", "x2_def_heartbeat", "nw_o2_"))
                                                                for d_, k_, v in g.fwd.get(o["node"], []) if k_ == "event_script")]
    hb_nodes = {o["node"] for o in hb_objects}
    area_load = []
    for a in areas:
        objs = obj_by_area.get(a["resref"], [])
        area_load.append(dict(area=a["label"], node=a["node"], objects=len(objs),
                              creatures=sum(1 for o in objs if o["cls"] == "creature"),
                              items=sum(1 for o in objs if o["cls"] == "item"),
                              heartbeats=sum(1 for o in objs if o["node"] in hb_nodes)))
    area_load.sort(key=lambda x: -x["objects"])
    largest = sorted([f for f in files if f["size"]], key=lambda f: -f["size"])[:25]
    by_ext_bytes = defaultdict(int)
    for f in files:
        by_ext_bytes[f["ext"]] += f["size"] or 0
    # Note: deletable_bytes counts Safe and Safe as group; review_bytes counts Review.
    performance = dict(
        module_bytes=sum(f["size"] or 0 for f in module_files),
        hak_bytes=sum(f["size"] or 0 for f in files if f["source"] == "hak"),
        deletable_bytes=sum(d["bytes"] for d in deletions if d["status"] != "Review"),
        review_bytes=sum(d["bytes"] for d in deletions if d["status"] == "Review"),
        heartbeat_objects=len(hb_objects),
        heartbeat_list=[dict(node=o["node"], label=g.label(o["node"]), where=g.breadcrumb(o["node"])) for o in hb_objects[:200]],
        areas=area_load[:60],
        largest_files=[dict(relpath=f["relpath"], size=f["size"], source=f["source"], node=f["node"],
                            unused=f["node"] in {d["node"] for d in deletions}) for f in largest],
        bytes_by_type=sorted(by_ext_bytes.items(), key=lambda kv: -kv[1])[:15])

    # ------------------------------------------------------------- files table
    dead_status = {d["node"]: d["status"] for d in deletions}
    file_rows = [dict(relpath=f["relpath"], resref=f["resref"], ext=f["ext"], kind=f["kind"], size=f["size"],
                      source=f["source"], status=f["status"], error=f["error"], node=f["node"],
                      used="unused: " + dead_status[f["node"]] if f["node"] in dead_status else
                      ("in use" if f["node"] in live else "n/a"),
                      preview=f["ext"] in IMAGE_EXTS)
                 for f in files]
    models = [dict(zip(("file_id", "name", "format", "supermodel", "textures"), r))
              for r in db.execute("SELECT * FROM models")]

    # ------------------------------------------------------------- variables, tags, token slots (nwn_varaudit)
    nwn_progress.stage("analysis", "Analysis: variables, tags and token slots")
    try:
        import nwn_varaudit
        var_audit = nwn_varaudit.run(db, g)
        for li in nwn_varaudit.issues_from(var_audit, len(haks_missing or ())):
            issues.append(li)
    except Exception as ex:  # noqa - an audit must never stop the analysis
        var_audit = dict(summary=dict(error=f"variable/tag audit failed: {ex}"), variables=[], tags=[], tokens=[])
        if verbose:
            print("  variable/tag audit failed:", ex)

    # ------------------------------------------------------------- factions (repute.fac, members, scripts that change them)
    try:
        import nwn_factions
        factions = nwn_factions.analyse(db, g, live, g.label)
        issues += factions["issues"]
    except Exception as ex:  # noqa - never let this stop the analysis
        factions = dict(summary=dict(error=f"faction analysis failed: {ex}"), factions=[], scripts=[], issues=[])
        if verbose:
            print("  faction analysis failed:", ex)

    # ------------------------------------------------------------- campaign databases (script side; files load later)
    try:
        import nwn_database
        not_live = {n_[7:].lower() for n_ in g.nodes if n_.startswith("script:") and n_ not in live}
        db_uses = nwn_database.script_uses(db)
        databases = nwn_database.cross_check(None, db_uses, not_live, nwn_database.int_constants(db))
        for f in databases["findings"]:
            if f["severity"] in ("warning", "error"):
                who = (f.get("examples") or [""])[0]
                issues.append(dict(severity=f["severity"], category=f["category"],
                                   node=f"script:{who}" if who else "", label=f"{who}.nss" if who else f["db"],
                                   detail=f["text"], fix=f["fix"]))
        databases = dict(summary=databases["summary"], findings=databases["findings"], sql=databases["sql"],
                         databases=[dict(d, vars=len(d["vars"])) for d in databases["databases"]])
    except Exception as ex:  # noqa - never let this stop the analysis
        databases = dict(summary=dict(error=f"database check failed: {ex}"), findings=[], sql=[], databases=[])
        if verbose:
            print("  database check failed:", ex)

    # ------------------------------------------------------------- compiled scripts (.ncs): damaged, or out of date
    try:
        import nwn_ncs
        import nwn_edit
        import nwnlib
        # nwscript.nss (the engine's function list): its default parameter values (string s="...") can appear in
        # compiled code without being in the script's source, so the stale check must accept them
        nws, _w = nwn_edit.find_nwscript(meta.get("nwn_root") or None, meta.get("nwn_user") or None, db)
        ncs_issues, ncs_summary = nwn_ncs.check_module(db, nwscript_text=nwnlib.decode_text(nws) if nws else "",
                                                    nwn_root=meta.get("nwn_root") or None, live=live)
        issues += ncs_issues
    except Exception as ex:  # noqa - a check must never stop the analysis
        ncs_summary = dict(error=f"compiled-script check failed: {ex}")
        if verbose:
            print("  compiled-script check failed:", ex)

    # ------------------------------------------------------------- quest families: token includes, DMFI, Jasperre's AI...
    try:
        import nwn_questsets

        def _header(name):
            """A script's header comment as the index stored it (nwn_index.header_comment), or ""."""
            r_ = db.execute("SELECT header_comment FROM scripts WHERE name=? LIMIT 1", (name,)).fetchone()
            return (r_[0] or "") if r_ else ""
        nwn_questsets.classify(inferred, var_audit, _header)
    except Exception as ex:  # noqa - grouping is presentation; never stop the analysis
        inferred["families"] = []
        if verbose:
            print("  quest families failed:", ex)

    # ------------------------------------------------------------- persistent-world performance (nwn_perf)
    nwn_progress.stage("analysis", "Analysis: persistent-world performance")
    try:
        import nwn_perf
        pw_perf = nwn_perf.run(db, g, live)
        for f_ in pw_perf["findings"]:
            if f_["severity"] == "warning":
                issues.append(dict(severity="warning", category=f_["category"], node=f_["node"], label=f_["label"],
                                   detail=f_["detail"]))
    except Exception as ex:  # noqa - a check must never stop the analysis
        pw_perf = dict(summary=dict(error=f"performance checks failed: {ex}"), areas=[], findings=[])
        if verbose:
            print("  performance checks failed:", ex)

    # ------------------------------------------------------------- known NWN:EE pitfalls (nwn_lints)
    try:
        import nwn_lints
        for li in nwn_lints.run_all(db, g, base_names):
            li.setdefault("label", g.label(li["node"]) if li["node"] in g.nodes else li["node"])
            issues.append(li)
        issues.sort(key=lambda i: (sev_order.get(i["severity"], 3), i["category"], i["label"]))
    except Exception as ex:  # noqa
        if verbose:
            print("  pitfall checks failed:", ex)
    # problems that only sit in unused content become info notes (see UNUSED_DOWNGRADE)
    issues_in_unused = downgrade_unused(issues, g, live)
    issues.sort(key=lambda i: (sev_order.get(i["severity"], 3), i["category"], i["label"]))

    # ------------------------------------------------------------- summary
    # headline numbers for the dashboard's Overview page
    sev_counts = defaultdict(int)
    for i in issues:
        sev_counts[i["severity"]] += 1
    lvl_counts = defaultdict(int)
    for nid, im in impact.items():
        if not nid.startswith("inst:"):
            lvl_counts[im["level"]] += 1
    ext_counts = defaultdict(int)
    for f in module_files:
        ext_counts[f["ext"]] += 1
    del_counts = defaultdict(int)
    del_bytes = 0
    for d in deletions:
        del_counts[d["status"]] += 1
        if d["status"] != "Review":
            del_bytes += d["bytes"]
    summary = dict(module_name=meta.get("module_name") or os.path.basename(out_dir.rstrip("/\\")),
                   module_path=meta.get("module_path"), module_source=meta.get("module_source"),
                   indexed_at=meta.get("indexed_at"),
                   files=len(files), module_files=len(module_files), total_bytes=sum(f["size"] or 0 for f in files),
                   areas=len(areas), scripts=len(scripts), items=len(items),
                   blueprints=sum(1 for n_ in g.nodes.values() if n_["type"] == "blueprint" and n_["in_module"]),
                   instances=sum(1 for o in objects if not o["is_bp"]), conversations=len(convs), quests=len(quests),
                   issues=dict(sev_counts), impact_levels=dict(lvl_counts), file_types=dict(ext_counts),
                   duplicate_groups=len(dups), mergeable_groups=sum(1 for d in dups if d["mergeable"]),
                   deletions=dict(del_counts), deletable_bytes=del_bytes,
                   nodes=len(g.nodes), edges=sum(len(v) for v in g.fwd.values()))

    # Review items held back only by general reasons (likely in use) and issues lowered because only unused content
    # holds them - the Overview and Safe to delete pages show these apart, so the rest stands out
    summary["review_likely_used"] = sum(1 for d in deletions if d["status"] == "Review" and d.get("general"))
    summary["issues_in_unused"] = issues_in_unused
    summary["factions"] = (factions.get("summary") or {}).get("factions", 0)
    summary["databases_used"] = (databases.get("summary") or {}).get("databases_used", 0)
    summary["haks_missing"] = len(haks_missing)
    summary["compiled_scripts"] = ncs_summary
    summary["base_game_indexed"] = bool(base_names)
    summary["quest_problems"] = sum(1 for q in quests if q["health"] != "ok")
    summary["inferred_quests"] = inferred["summary"].get("quests", 0)
    # of those, real quests (token strings, journal, player variables a conversation drives) vs systems' own state
    if inferred.get("families"):
        summary["inferred_real_quests"] = sum(f["quests"] for f in inferred["families"] if f["kind"] == "quests")
        summary["inferred_journal_quests"] = sum(f["quests"] for f in inferred["families"] if f["name"] == "Journal quests")
        summary["inferred_system_state"] = summary["inferred_quests"] - summary["inferred_real_quests"]
    summary["inferred_quest_problems"] = sum(1 for q in inferred["quests"] if q.get("health") in ("broken", "warning"))
    summary["variable_warnings"] = (var_audit.get("summary") or {}).get("variable_warnings", 0)
    summary["tag_warnings"] = (var_audit.get("summary") or {}).get("tag_warnings", 0)
    summary["orphan_clusters"] = len(orphans)
    summary["skins"] = len(skins)
    import nwn_fixes
    nwn_fixes.add_fixes(issues)          # every issue carries a suggested path to resolution
    asset_dups = asset_duplicates(db, live)
    summary["asset_duplicate_groups"] = len(asset_dups)
    report = dict(summary=summary, issues=issues, scripts=scripts, items=items, orphan_instances=orphan_instances,
                  orphans=orphans, skins=skins, ai_findings=ai_findings, performance=performance,
                  duplicates=dups, merge_rules=2, noise_rules=2, asset_duplicates=asset_dups, deletions=deletions, areas=areas, conversations=convs, conversation_dynamic_starts=dyn_starts[:300], quests=quests,
                  inferred_quests=inferred, var_audit=var_audit, pw_performance=pw_perf, factions=factions, databases=databases,
                  files=file_rows, models=models, hierarchy=g.hierarchy(),
                  breadcrumbs={nid: g.breadcrumb(nid) for nid in g.nodes if nid.startswith("inst:")},
                  impact=impact,
                  labels={nid: nd["label"] for nid, nd in g.nodes.items()})
    nwn_progress.stage("analysis", "Analysis: writing reports")
    write_csvs(out_dir, report)          # CSVs carry everything (all hak files, every impact row)
    module_nodes = {f["node"] for f in module_files}
    report = slim_report(report, module_nodes)   # the browser gets the module-focused view; the rest is in index.sqlite
    import nwn_update
    report["toolkit"] = nwn_update.report_stamp()   # which version made this analysis (Overview; "analyse again" notice)
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False)
    try:
        import nwn_catalog
        nwn_progress.stage("analysis", "Analysis: hak catalogue and 2da conflicts")
        cat = nwn_catalog.build_catalog(out_dir, verbose=verbose)
        extra = []
        for c in cat.get("twoda_conflicts", []):
            if c["kind"] in ("hak-vs-hak", "override", "stale"):
                extra.append(dict(severity="warning" if c["kind"] != "stale" else "error", category="2da_" + c["kind"].replace("-", "_"),
                                  node=f"2da:{c['table']}", label=f"{c['table']}.2da", detail=c["summary"] + (" → " + c["advice"] if c.get("advice") else "")))
        for h in cat.get("heads", []):
            if h["status"] == "MISSING":
                extra.append(dict(severity="error", category="missing_head_model", node=h["node"], label=h["creature"],
                                  detail=f"head {h['head']} needs model {h['model']} which is not in the game, haks or module - the creature will show without a head"))
        if extra:
            nwn_fixes.add_fixes(extra)
            report["issues"] += extra
            report["summary"]["issues"] = dict(Counter(i["severity"] for i in report["issues"]))
            with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False)
            write_csvs(out_dir, report, only=("issues.csv",))
    except Exception as ex:  # noqa - catalogue is best-effort
        if verbose:
            print("  catalogue failed:", ex)
    try:
        # the Blueprints page: every custom blueprint and its palette category (palette.json, reports/palette.csv)
        import nwn_palette
        nwn_progress.stage("analysis", "Analysis: blueprint palette")
        nwn_palette.build_palette(out_dir, verbose=verbose)
    except Exception as ex:  # noqa - best-effort like the catalogue; the dashboard builds it again when asked
        if verbose:
            print("  palette failed:", ex)
    if verbose:
        # note: sev_counts was taken before the catalogue's issues were added
        print(f"  issues: {dict(sev_counts)}  duplicate groups: {len(dups)}  "
              f"deletable: {dict(del_counts)}  impact: {dict(lvl_counts)}", flush=True)
    db.close()
    return report


def n_gff_exts():
    """The file extensions that are GFF files (nwnlib.GFF_EXTENSIONS); imported late, only when needed."""
    import nwnlib
    return nwnlib.GFF_EXTENSIONS


# Size caps for report.json (see slim_report); the CSVs and index.sqlite keep everything.
HIERARCHY_GROUP_CAP = 40
ORPHAN_INSTANCE_CAP = 5000
TRIGGER_CAP = 25


def slim_report(report, module_nodes):
    """
    Cut report.json down to what a browser can hold for a big module (one large persistent world: 268k files, 415k nodes -> 288 MB).
    Everything removed here is still in index.sqlite and reachable through the dashboard API
    (/api/node, /api/find, /api/files); the CSVs are written from the full data before this runs.
      files        module files only (hak files: /api/files search, Hak catalogue page)
      impact       non-instance nodes only; hak nodes only when they carry an impact level
      labels       non-instance nodes whose label differs from their own name
      breadcrumbs  non-instance nodes (instances: /api/node, /api/find)
      models       models that appear in the kept impact map
      hierarchy    each area group shows at most HIERARCHY_GROUP_CAP objects (+ a '... N more' entry)
      scripts      triggers capped at TRIGGER_CAP per script (full list: /api/node -> used_by)
      orphan_instances  first ORPHAN_INSTANCE_CAP rows (all rows: reports/items.csv, /api/find)
    """
    r = dict(report)
    r["summary"] = dict(report["summary"], hak_files=sum(1 for f in report["files"] if f["source"] != "module"),
                        slim=True)
    r["files"] = [f for f in report["files"] if f["source"] == "module"]
    imp = {}
    for k, v in report["impact"].items():
        if k.startswith("inst:"):
            continue
        if k in module_nodes or k.split(":")[0] in ("bp", "script", "dlg", "area", "2da", "module", "quest") \
                or v["level"] in ("Critical", "High", "Medium"):
            imp[k] = dict(v, counts={ck: cv for ck, cv in v["counts"].items() if cv})
    r["impact"] = imp
    r["scripts"] = [dict(sc, triggers=sc["triggers"][:TRIGGER_CAP],
                         triggers_more=max(0, len(sc["triggers"]) - TRIGGER_CAP)) for sc in report["scripts"]]
    r["orphan_instances"] = report["orphan_instances"][:ORPHAN_INSTANCE_CAP]
    # the biggest groups go to the browser; reports/asset_duplicates.csv has them all (summary keeps the total)
    r["asset_duplicates"] = report.get("asset_duplicates", [])[:ASSET_DUP_CAP]
    r["summary"]["orphan_instances_total"] = len(report["orphan_instances"])
    r["labels"] = {k: v for k, v in report["labels"].items()
                   if not k.startswith("inst:") and (k in imp or k in module_nodes) and v != k.split(":", 1)[-1]}
    r["breadcrumbs"] = {k: v for k, v in report["breadcrumbs"].items() if not k.startswith("inst:")}
    r["models"] = [m for m in report["models"] if f"model:{m['name']}" in imp]

    def cap(node):
        """Copy of a hierarchy node whose groups show at most HIERARCHY_GROUP_CAP children (+ a "more" entry)."""
        ch = node.get("children")
        if not ch:
            return node
        out = dict(node)
        if node.get("type") == "group" and len(ch) > HIERARCHY_GROUP_CAP:
            kept = [cap(c) for c in ch[:HIERARCHY_GROUP_CAP]]
            kept.append(dict(id=node["id"] + "#more", label=f"… {len(ch) - HIERARCHY_GROUP_CAP} more (use Find object)",
                             type="more"))
            out["children"] = kept
        else:
            out["children"] = [cap(c) for c in ch]
        return out
    r["hierarchy"] = cap(report["hierarchy"])
    return r


# Models and textures that can be the same content under different names (asset_duplicates). Textures are compared
# by their bytes; models by files.content_hash, which masks the model's own name (nwn_index.index_mdl).
ASSET_DUP_CAP = 3000                  # groups kept in a slimmed report.json (largest extra copies first)
ASSET_DUP_EXTS = {"mdl": "model", "tga": "texture", "dds": "texture", "plt": "texture"}
ASSET_DUP_NOTES = {
    "model": "Same model apart from its own name. The game loads models by name, and heads and body parts by number "
             "(pmh0_head012 is head 12 of a male human), so each name may still be needed: remove a copy only when no "
             "creature, player choice or 2da uses that name or number - and keep the textures it names.",
    "texture": "Byte-for-byte the same picture under different names. Models and 2das name the textures they use, so "
               "each name may still be needed: remove a copy only when nothing names it."}


def asset_duplicates(db, live=()):
    """Groups of models / textures with the same content under different names, in the module and its haks.

    db: the analysis index. live: node ids in use (a member in use is marked in_use). The same name carried by two
    sources is one member (load order picks one copy - not a duplicate); its sources are listed winner first.
    Returns [{id, kind, ext, members: [{name, node, sources, in_use, head?}], bytes (one copy), extra_bytes (the other
    copies), note, mergeable: False}], largest extra_bytes first. Review only: names are what models, 2das and head
    numbers point at, so nothing here is merged or deleted automatically. Read-only."""
    import nwnlib as nl
    rows = db.execute(
        "SELECT f.resref, f.ext, f.sha256, f.content_hash, f.size, f.node, s.kind, s.path, s.priority, s.id "
        "FROM files f JOIN sources s ON s.id=f.source_id WHERE f.status='ok' AND s.kind IN ('module','hak') AND "
        f"f.ext IN ({','.join(repr(e) for e in ASSET_DUP_EXTS)})").fetchall()
    by = defaultdict(dict)                    # (ext, content key) -> name -> member
    for resref, ext, sha, chash, size, node, kind, path, prio, sid in rows:
        key = (ext, chash if ext == "mdl" and chash else sha)
        if not key[1]:
            continue
        name = f"{resref}.{ext}"
        m = by[key].setdefault(name, dict(name=name, node=node, _src=[], size=size or 0))
        m["_src"].append((nl.source_rank(kind, prio), sid, "module" if kind == "module" else
                          os.path.splitext(os.path.basename(path))[0]))
    groups = []
    for (ext, _h), mem in by.items():
        if len(mem) < 2:
            continue
        members = []
        for name in sorted(mem):
            m = mem[name]
            out = dict(name=name, node=m["node"], sources=[s_ for _r, _i, s_ in sorted(m["_src"])],
                       in_use=m["node"] in live)
            hd = re.search(r"_head(\d+)\.mdl$", name)
            if hd:
                out["head"] = int(hd.group(1))
            members.append(out)
        size = max(mem[x]["size"] for x in mem)
        kind = ASSET_DUP_EXTS[ext]
        groups.append(dict(kind=kind, ext=ext, members=members, bytes=size, extra_bytes=size * (len(members) - 1),
                           note=ASSET_DUP_NOTES[kind], mergeable=False))
    groups.sort(key=lambda g: (-g["extra_bytes"], g["members"][0]["name"]))
    for i, g in enumerate(groups, 1):
        g["id"] = i
    return groups


def write_csvs(out_dir, r, only=None):
    """Write the report's tables to <out_dir>/reports/*.csv (overwriting them).

    r: the report dict - the full one (before slim_report), except for an issues.csv-only rewrite.
    only: a set/tuple of CSV file names to write, or None for all.
    Lists in a cell are joined with "; ". UTF-8 with a byte-order mark ("utf-8-sig") so Excel shows accented
    names correctly. Writes nothing outside the reports folder."""
    d = os.path.join(out_dir, "reports")
    os.makedirs(d, exist_ok=True)

    def w(name, rows, cols):
        """Write one CSV: a header row of `cols`, then one row per dict in `rows` (missing keys -> empty cell)."""
        if only and name not in only:
            return
        with open(os.path.join(d, name), "w", newline="", encoding="utf-8-sig") as fh:
            cw = csv.writer(fh)
            cw.writerow(cols)
            for row in rows:
                cw.writerow([("; ".join(map(str, row.get(c))) if isinstance(row.get(c), list) else row.get(c, ""))
                             for c in cols])

    w("issues.csv", r["issues"], ["severity", "category", "label", "detail", "fix"])
    if only and set(only) <= {"issues.csv"}:
        # issues-only rewrite (after the catalogue adds its issues): `r` may be the slimmed report here, whose
        # impact rows no longer carry the full counts - building the other CSVs from it crashed (KeyError 'areas')
        return
    w("scripts.csv", r["scripts"], ["name", "role", "impact", "compiled", "lines", "summary", "description",
                                    "included_by", "includes"])
    items = [dict(it, locations=[l["where"] for l in it["locations"]]) for it in r["items"]]
    w("items.csv", items, ["resref", "tag", "name", "base_item", "cost", "placed", "locations", "carried_by",
                           "created_by", "referenced_by", "quests", "tag_based_script", "impact"])
    dup_rows = []
    for dg in r["duplicates"]:
        for m in dg["members"]:
            dup_rows.append(dict(group=dg["id"], category=dg["category"], member=m["label"], refs=m["refs"],
                                 keeper="yes" if m["node"] == dg["keeper"] else "", impact=m["impact"],
                                 mergeable=dg["mergeable"], blocked=dg["blocked"], note=dg["note"]))
    w("duplicates.csv", dup_rows, ["group", "category", "member", "keeper", "refs", "impact", "mergeable", "blocked", "note"])
    w("safe_to_delete.csv", r["deletions"], ["status", "type", "label", "files", "bytes", "reason", "note"])
    w("asset_duplicates.csv", [dict(group=g["id"], kind=g["kind"], name=m["name"], sources=m["sources"],
                                    head=m.get("head", ""), in_use="yes" if m["in_use"] else "", bytes=g["bytes"])
                               for g in r.get("asset_duplicates", []) for m in g["members"]],
      ["group", "kind", "name", "sources", "head", "in_use", "bytes"])
    imp = [dict(node=k, label=r["labels"].get(k, k), **{kk: v[kk] for kk in ("level",)}, reasons=v["reasons"],
                areas=v["counts"]["areas"], quests=v["quests"], affected=v["counts"]["total"])
           for k, v in r["impact"].items() if not k.startswith("inst:")]
    imp.sort(key=lambda x: (-LEVELS.index(x["level"]), -x["affected"]))
    w("impact.csv", imp, ["level", "label", "affected", "areas", "quests", "reasons", "node"])
    w("files.csv", r["files"], ["relpath", "ext", "kind", "size", "source", "status", "used", "error"])
    w("quest_health.csv", [dict(q, problems=q["problems"], needs=[f"{x['name']}={x['status']}" for x in q["needs"]])
                           for q in r["quests"]], ["tag", "name", "health", "in_journal", "updated_by", "needs", "problems"])
    fa = r.get("factions") or {}
    names_ = {f["id"]: f["name"] for f in fa.get("factions", [])}
    w("factions.csv", [dict(f, parent=f.get("parent_name") or "", feels_about_pc=f.get("to_pc"),
                            dislikes=[names_[j] for j, v in f["feels"].items() if v is not None and v <= 10 and j != f["id"]],
                            likes=[names_[j] for j, v in f["feels"].items() if v is not None and v >= 90 and j != f["id"]])
                       for f in fa.get("factions", [])],
      ["id", "name", "parent", "glob", "feels_about_pc", "attitude_to_pc", "creatures_placed", "creature_blueprints",
       "objects_placed", "areas", "likes", "dislikes", "holders"])
    w("faction_scripts.csv", fa.get("scripts", []), ["script", "line", "call", "what", "factions", "detail", "args", "runs", "live"])
    w("conversations.csv", [dict(c, started_by=[f"{x['script']}:{x['line']} {x['how']}" for x in c.get("started_by", [])])
                            for c in r.get("conversations", [])],
      ["name", "areas", "used_by", "started_by", "scripts", "quests", "entries", "replies", "words", "impact"])
    iq = (r.get("inferred_quests") or {}).get("quests", [])
    w("inferred_quests.csv", [dict(q, stages=[f"{s['value']}: set by {len(s['set_by'])}, checked by {len(s['checked_by'])}" for s in q["stages"]],
                                   problems=[p["text"] for p in q["problems"]], givers=q["givers"],
                                   items=[f"{i['role']} {i['name']} ({i['status']})" for i in q["items"]])
                              for q in iq],
      ["family", "set", "name", "health", "label", "kind", "key", "hub", "givers", "stages", "values_set", "items", "problems",
       "scripts"])
    va = r.get("var_audit") or {}
    w("variables.csv", [dict(v, findings=[f["text"] for f in v["findings"]],
                             set_in=[f"{x['script']}:{x['line']}" for x in v["sets"]] + [x["label"] for x in v["toolset"]],
                             read_in=[f"{x['script']}:{x['line']}" for x in v["reads"]]) for v in va.get("variables", [])],
      ["level", "status", "system", "name", "vtype", "scopes", "n_sets", "n_reads", "set_in", "read_in", "findings"])
    w("tag_lookups.csv", [dict(t, used_in=[f"{x['script']}:{x['line']} {x['func']}" for x in t["refs"]])
                          for t in va.get("tags", [])], ["level", "status", "tag", "lookups", "funcs", "used_in", "detail"])
    w("token_slots.csv", [dict(r_, system=t["system"], set_in=[f"{x['script']}:{x['line']}" for x in r_["sets"]],
                               read_in=[f"{x['script']}:{x['line']}" for x in r_["reads"]])
                          for t in va.get("tokens", []) for r_ in t["slots"]],
      ["system", "slot", "status", "names", "comment", "values", "n_sets", "n_reads", "set_in", "read_in"])
    pp = r.get("pw_performance") or {}
    w("performance_findings.csv", pp.get("findings", []), ["severity", "category", "label", "count", "detail", "node"])
    w("performance_areas.csv", pp.get("areas", []), ["area", "score", "creatures", "encounter_max", "custom_heartbeats",
                                                     "ai_heartbeats", "placeables", "static_candidates", "items", "big_containers"])
    w("performance_heartbeats.csv", [dict(h, kinds=", ".join(f"{n_} {k}" for k, n_ in h["kinds"].items()),
                                          areas=", ".join(f"{a['label']} ({a['n']})" for a in h["areas"]),
                                          factors="; ".join(h["factors"])) for h in pp.get("heartbeats", [])],
      ["script", "share", "copies", "runs_per_min", "weight", "cost", "early_exit", "standard", "kinds", "areas", "factors", "fix"])
    w("creature_ai.csv", [dict(a, problems=a["problems"]) for a in r.get("ai_findings", [])], ["label", "where", "problems"])
    w("orphans.csv", [dict(o, members=[m["label"] for m in o["members"]]) for o in r["orphans"]],
      ["id", "kind", "size", "members", "quests", "tags", "hints"])
    w("skins_hides.csv", [dict(sk, worn_by=[f"{w_['label']} ({w_['slot']})" for w_ in sk["worn_by"]]) for sk in r["skins"]],
      ["resref", "name", "type", "tag", "property_count", "worn_by", "created_by", "in_use", "impact"])


if __name__ == "__main__":
    run_analysis(sys.argv[1])
