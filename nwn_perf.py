"""
nwn_perf.py - persistent-world performance checks (what costs the server CPU and players bandwidth).

  heartbeats       objects whose OnHeartbeat runs a custom script (every 6 seconds, for every object, forever),
                   per area; heartbeat scripts that walk every object in the area or loop over players
  spawn density    creatures placed in each area, plus what its encounters can spawn
  delay floods     DelayCommand inside a loop (one queued action per pass), and scripts or functions that keep
                   re-scheduling themselves with DelayCommand (a hidden heartbeat that never stops)
  inventory bloat  containers, creatures and stores holding very many items (each item is an object the server
                   keeps and sends to players who open it)
  static candidates placed placeables that could be ticked Static: not usable, no scripts, no inventory, no
                   conversation, not plot-relevant by tag - static placeables cost nothing at run time
  heartbeat cost   every heartbeat script ranked by an ESTIMATED load: copies running x a relative weight read from
                   what its code calls (see HB_WEIGHTS). It is a ranking aid, not a measurement.
Nothing is changed; this reads the analysis index.

Entry point: run(db, g, live) -> dict(summary, areas, findings, heartbeats). Findings at "warning" level become
analysis issues (nwn_analysis). Thresholds (AREA_CREATURES_WARN...) are rules of thumb, not engine limits: tune them
to the server. Limits: everything is read from the module's files and script text, so actual player counts, how
long a script really takes, and objects created at run time are not known. NWScript is read as text (brace and
bracket counting), not compiled.
"""
from __future__ import annotations

import re
from collections import defaultdict


# Script-name prefixes of standard creature/henchman AI heartbeats (BioWare, Jasperre's, Tony K's henchman AI).
# These are counted as "AI heartbeats": their cost is a matter of how many creatures are placed, not of the script.
STANDARD_HB = ("nw_c2_default1", "x2_def_heartbeat", "nw_o2_", "j_ai_onheartbeat", "x0_", "nw_ch_ac1", "x2_hen",
               "hench_", "nw_c2_")
# Rule-of-thumb thresholds (not engine limits) above which a finding is raised
AREA_CREATURES_WARN = 60
AREA_CUSTOM_HB_WARN = 10
CONTAINER_ITEMS_WARN = 50
STORE_ITEMS_WARN = 300
STATIC_AREA_INFO = 20
# The start of a loop over a group of objects: `GetFirstObjectInArea(`, `GetFirstPC(` ... (each has a GetNext* partner)
LOOP_OVER = re.compile(r"\b(GetFirstObjectInArea|GetFirstObjectInShape|GetFirstPC|GetFirstFactionMember|"
                       r"GetFirstInPersistentObject|GetFirstItemInInventory)\s*\(")
# Event-script fields of a placed placeable in an area's .git (any of them set = the placeable does something)
PLC_EVENTS = ("OnClick", "OnClosed", "OnDamaged", "OnDeath", "OnDisarm", "OnHeartbeat", "OnInvDisturbed", "OnLock",
              "OnMeleeAttacked", "OnOpen", "OnSpellCastAt", "OnTrapTriggered", "OnUnlock", "OnUsed", "OnUserDefined")


def _is_standard(script):
    """Is this a standard AI heartbeat (by name prefix, see STANDARD_HB)?"""
    return script.lower().startswith(STANDARD_HB)


def _block_end(code, i):
    """Index just after the statement/block starting at i (after a loop header).
    A { block } ends at its matching brace, a single statement at the next ";". Braces inside strings are counted."""
    while i < len(code) and code[i] in " \t\r\n":
        i += 1
    if i < len(code) and code[i] == "{":
        depth = 0
        for j in range(i, len(code)):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
        return len(code)
    j = code.find(";", i)
    return len(code) if j < 0 else j + 1


def delay_findings(code):
    """[(kind, line, detail)] for DelayCommand inside loops and self-rescheduling functions.

    code: comment-free script text (lex_nss output) so lines match the source. kind is "delay_in_loop" or
    "self_reschedule". A "loop" is a for/while header; do-while loops are not looked at.
    Self-rescheduling: a function whose own body contains `DelayCommand(<secs>, <same function>(...))`, e.g.
    `void Tick() { ...; DelayCommand(6.0, Tick()); }`. Never raises."""
    out = []
    for m in re.finditer(r"\b(for|while)\s*\(", code):
        # skip the loop header's (...) to reach the loop body
        depth, i = 1, m.end()
        while i < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[i], 0)
            i += 1
        body = code[i:_block_end(code, i)]
        if "DelayCommand" in body:
            out.append(("delay_in_loop", code.count("\n", 0, m.start()) + 1,
                        "DelayCommand inside a loop: one queued action per pass"))
    # every function definition at the start of a line: `void Tick(object o) {` -> "Tick"
    for m in re.finditer(r"^[ \t]*(?:void|int|string|object|float)\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{", code, re.M):
        name = m.group(1)
        end = _block_end(code, m.end() - 1)
        body = code[m.end():end]
        # `DelayCommand(6.0, Tick(` inside Tick's own body; group 1 is the delay text ("6.0", "fDelay")
        for d in re.finditer(r"DelayCommand\s*\(\s*([^,]+),\s*" + re.escape(name) + r"\s*\(", body):
            secs = d.group(1).strip()
            out.append(("self_reschedule", code.count("\n", 0, m.end() + d.start()) + 1,
                        f"{name}() re-schedules itself every {secs} s - a hidden heartbeat that runs until something stops it"))
    return out


# ---------------------------------------------------------------------------------------------- heartbeat cost
# Relative cost of ONE run of a heartbeat script, read from its code (and the include functions it calls).
# Not a timing measurement: it ranks scripts by what they do, the way a builder would read them.
# How the weight is built (heartbeat_costs):
#   - every run starts at 1.0 (the engine starting the script at all);
#   - each call to an engine function adds the weight of the FIRST pattern below that matches its name, once per
#     place it is written in the code (a call inside a loop counts once - the number of passes is unknown);
#   - the numbers are judgement-based estimates of relative expense (walking every object in an area is the
#     most expensive; touching the campaign database means disk work), chosen to rank scripts, not to predict
#     milliseconds. Change them freely; only the order of the results depends on them.
# Each entry: (regex on the called function's name, weight, plain-English reason shown on the Performance page).
HB_WEIGHTS = [
    (re.compile(r"^GetFirstObjectInArea$"), 10, "walks every object in the area"),
    (re.compile(r"^GetFirstObjectInShape$"), 6, "searches a shape around itself"),
    (re.compile(r"^GetFirstFactionMember$"), 4, "walks a faction / party"),
    (re.compile(r"^GetFirstInPersistentObject$"), 3, "walks everything inside an AoE / trigger"),
    (re.compile(r"^GetFirstItemInInventory$"), 3, "walks an inventory"),
    (re.compile(r"^GetFirstPC$"), 2, "walks every player on the server"),
    (re.compile(r"^GetNearest"), 2, "searches for the nearest object"),
    (re.compile(r"^(Get|Set|Delete)Campaign|^(Store|Retrieve)CampaignObject$|^DestroyCampaignDatabase$"), 5,
     "reads or writes the campaign database (disk)"),
    (re.compile(r"^SqlPrepareQuery|^NWNX_SQL_|^SQLExecDirect$"), 5, "runs a database query"),
    (re.compile(r"^CreateObject$"), 3, "creates objects"),
    (re.compile(r"^CreateItemOnObject$"), 2, "creates items"),
    (re.compile(r"^ExecuteScript$"), 1, "runs another script"),
    (re.compile(r"^DelayCommand$"), 1, "queues delayed actions"),
    (re.compile(r"^SignalEvent$"), 1, "fires events"),
]
# A function definition with any NWScript return type, including `struct name`: `int Count(object o) {` -> "Count"
_FUNC_DEF = re.compile(r"\b(?:void|int|float|string|object|location|vector|effect|itemproperty|json|sqlquery|talent|"
                       r"event|struct\s+\w+)\s+(\w+)\s*\([^)]*\)\s*\{")
# Anything that looks like a call: a name followed by "(" (keywords filtered out with _KEYWORDS)
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "do", "case", "else"}


def _bodies(code):
    """{function name: body text} for every function defined in the code.
    Prototypes (ending in ";") are not matched. The first definition of a name wins."""
    out = {}
    for m in _FUNC_DEF.finditer(code):
        depth, j = 1, m.end()
        while j < len(code) and depth:
            depth += {"{": 1, "}": -1}.get(code[j], 0)
            j += 1
        out.setdefault(m.group(1), code[m.end():j])
    return out


def heartbeat_costs(db, code, hb_scripts, live, area_label):
    """Rank heartbeat scripts by estimated server load: copies running x relative cost per run.

    db: the analysis index (read-only); code: callable(script name) -> comment-free source ("" when none);
    hb_scripts: {script: [owner nodes]} (inst:..., area:..., module, bp:...); live: set of reachable node ids;
    area_label: {area resref: display name}.
    Per script: copies = placed objects + areas + module that run it (blueprints are listed but not counted: only
    placed copies run); runs_per_min = copies x 10 (a heartbeat fires every 6 seconds); weight = see HB_WEIGHTS;
    cost = runs_per_min x weight; share = its % of the total cost. All of it is an estimate for ranking.
    Returns rows sorted by cost, highest first, each with a suggested fix."""
    # script -> scripts it #includes (from the dependency graph's include edges)
    inc = defaultdict(set)
    for s_, d_ in db.execute("SELECT src, dst FROM edges WHERE kind='include'"):
        if s_.startswith("script:") and d_.startswith("script:"):
            inc[s_[7:].lower()].add(d_[7:].lower())
    cls_of = dict(db.execute("SELECT node, class FROM objects"))
    body_cache = {}

    def bodies(nm):
        if nm not in body_cache:
            body_cache[nm] = _bodies(code(nm))
        return body_cache[nm]

    rows = []
    for script, owners in hb_scripts.items():
        # include closure, then every function reachable from main()
        closure, todo = [], [script]
        while todo:
            x = todo.pop()
            if x in closure:
                continue
            closure.append(x)
            todo.extend(sorted(inc.get(x, ())))
        funcs = {}
        # reversed: the script's own definitions (first in closure) are applied last, so they win over an include's
        for x in reversed(closure):
            funcs.update(bodies(x))
        main = bodies(script).get("main") or code(script)
        # Walk from main() into every user function it reaches (each body once). Calls to names with no body here
        # are engine functions (or functions we can't see) and are counted in `calls`. 400 functions is a safety cap.
        seen, stack, calls = set(), [main], defaultdict(int)
        while stack and len(seen) < 400:
            body = stack.pop()
            for m in _CALL.finditer(body):
                fn = m.group(1)
                if fn in _KEYWORDS:
                    continue
                if fn in funcs and fn not in seen and fn != "main":
                    seen.add(fn)
                    stack.append(funcs[fn])
                elif fn not in funcs:
                    calls[fn] += 1
        weight, why = 1.0, defaultdict(int)
        for fn, n_ in calls.items():
            for rx, w, text in HB_WEIGHTS:
                if rx.search(fn):
                    weight += w * n_
                    why[text] += n_
                    break
        # Early-exit heuristic: a "return" in the first 500 characters of main(), before any loop, usually means
        # "stop when no player is near". Such scripts get half of their extra weight (the base 1.0 stays).
        head = main[:500]
        loop_at = min([i for i in (head.find("while"), head.find("for ("), head.find("for(")) if i >= 0] or [len(head)])
        early = "return" in head[:loop_at]
        if early:
            weight = 1 + (weight - 1) * 0.5        # usually stops when nobody is around
        insts = [o for o in owners if o.startswith("inst:")]
        areas_n = sum(1 for o in owners if o.startswith("area:"))
        module = any(o == "module" for o in owners)
        bps = [o for o in owners if o.startswith("bp:")]
        copies = len(insts) + areas_n + (1 if module else 0)
        kinds = defaultdict(int)
        for o in insts:
            kinds[cls_of.get(o) or "object"] += 1
        per_area = defaultdict(int)
        for o in insts:
            per_area[o.split(":", 2)[1]] += 1       # instance node ids are "inst:<area resref>:<path in the .git>"
        top = sorted(per_area.items(), key=lambda kv: -kv[1])[:5]
        standard = _is_standard(script)
        walks = any(t.startswith(("walks", "searches a shape")) for t in why)
        if not copies and not bps:
            continue
        # one suggested fix, the most specific that applies (standard AI, walks without early exit, many copies,
        # does nothing, otherwise fine)
        if standard:
            fix = ("Standard creature AI: its cost comes from how many creatures are placed - spawn creatures when "
                   "players arrive (encounters / spawners) instead of placing them all, and clear empty areas.")
        elif walks and not early:
            fix = ("It walks objects every 6 s for every copy. Add an early exit (return at once when no player is in "
                   "the area), or move the work to OnEnter / a trigger / one area-level heartbeat.")
        elif copies > 20:
            fix = (f"{copies} copies each run every 6 s. Consider one controller (the area's OnHeartbeat or a single "
                   "object) that does the work for all of them.")
        elif not calls and weight <= 1.0:
            fix = "It does almost nothing: if it is empty, clear the event so it stops firing."
        else:
            fix = "Fine as it is unless the server is struggling; an early exit when no player is near still helps."
        rows.append(dict(script=script, node=f"script:{script}", standard=standard, copies=copies,
                         runs_per_min=copies * 10, blueprints=len(bps), weight=round(weight, 1),
                         cost=round(copies * 10 * weight), early_exit=early, kinds=dict(kinds),
                         areas=[dict(area=a, label=area_label.get(a, a), n=n_) for a, n_ in top],
                         areas_total=len(per_area), module=module, area_events=areas_n,
                         factors=[f"{t} ({n_}x)" for t, n_ in sorted(why.items(), key=lambda kv: -kv[1])],
                         live=f"script:{script}" in live, fix=fix))
    total = sum(r["cost"] for r in rows) or 1           # "or 1": no division by zero when nothing has a heartbeat
    custom_total = sum(r["cost"] for r in rows if not r["standard"]) or 1
    for r in rows:
        r["share"] = round(100.0 * r["cost"] / total, 1)
        if not r["standard"]:
            r["share_custom"] = round(100.0 * r["cost"] / custom_total, 1)   # among custom scripts only
    rows.sort(key=lambda r: -r["cost"])
    return rows


def run(db, g, live):
    """Run every performance check over the analysed module.

    db: the analysis index (sqlite3.Connection), read-only; g: the dependency graph (only its label() is used, for
    inventory findings; may be None); live: set of node ids the module reaches (delay checks only look at those).
    Returns dict(summary, areas, findings, heartbeats): areas sorted by a rough load score, findings by severity.
    Writes nothing; nwn_analysis catches any exception."""
    findings = []
    area_label = {}
    for nd, label in db.execute("SELECT node, label FROM nodes WHERE type='area'"):
        area_label[nd[5:]] = label or nd[5:]
    module_areas = {a for (a,) in db.execute("SELECT DISTINCT area FROM objects WHERE area IS NOT NULL AND is_blueprint=0")}
    rows = {a: dict(area=area_label.get(a, a), node=f"area:{a}", creatures=0, encounter_max=0, placeables=0,
                    static_candidates=0, custom_heartbeats=0, ai_heartbeats=0, big_containers=0, items=0)
            for a in module_areas}
    # placed creatures / placeables / items per area
    for area, cls, n_ in db.execute("SELECT area, class, count(*) FROM objects WHERE is_blueprint=0 AND area IS NOT NULL "
                                    "GROUP BY area, class"):
        r = rows.setdefault(area, dict(area=area_label.get(area, area), node=f"area:{area}", creatures=0, encounter_max=0,
                                       placeables=0, static_candidates=0, custom_heartbeats=0, ai_heartbeats=0,
                                       big_containers=0, items=0))
        if cls == "creature":
            r["creatures"] = n_
        elif cls == "placeable":
            r["placeables"] = n_
        elif cls == "item":
            r["items"] += n_
    # encounters: what they can spawn at once (MaxCreatures of each encounter in the area's .git; the .git's resref
    # is the area's resref)
    for rel, path, val in db.execute("SELECT f.resref, fi.path, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id "
                                     "WHERE f.ext='git' AND fi.label='MaxCreatures' AND fi.path LIKE 'Encounter List[%]/MaxCreatures'"):
        if rel in rows and str(val).isdigit():
            rows[rel]["encounter_max"] += int(val)
    # heartbeats (placed objects, areas, module)
    hb_scripts = defaultdict(list)     # script -> [owner nodes]
    creature_hb = set()                # (script, owner) pairs that are a creature's heartbeat (ScriptHeartbeat)
    # the graph's event edges carry the event field name in `via` (OnHeartbeat for objects, ScriptHeartbeat
    # for creatures, Mod_OnHeartbeat for the module)
    for src, dst, via in db.execute("SELECT src, dst, via FROM edges WHERE kind IN ('event_script','module_event') AND "
                                    "(via LIKE '%OnHeartbeat' OR via LIKE '%ScriptHeartbeat' OR via LIKE '%Mod_OnHeartbeat')"):
        script = dst[7:]
        hb_scripts[script].append(src)
        if via.endswith("ScriptHeartbeat"):
            creature_hb.add((script, src))
        if src.startswith("inst:"):
            area = src.split(":", 2)[1]
            if area in rows:
                rows[area]["ai_heartbeats" if _is_standard(script) else "custom_heartbeats"] += 1
    sources = {nm.lower(): src for nm, src in db.execute("SELECT name, source FROM scripts WHERE source IS NOT NULL")}
    code_of = {}

    def code(nm):
        """Comment-free source of a script (lex_nss), cached; "" when the module has no source for it."""
        if nm not in code_of:
            from nwn_index import lex_nss
            s = sources.get(nm)
            code_of[nm] = lex_nss(s)[0] if s else ""
        return code_of[nm]
    for script, owners in sorted(hb_scripts.items(), key=lambda kv: -len(kv[1])):
        if _is_standard(script):
            continue
        c = code(script)
        loops = sorted({m.group(1) for m in LOOP_OVER.finditer(c)})
        insts = [o for o in owners if o.startswith("inst:")]
        where = "the module" if any(o == "module" for o in owners) else \
            (f"{len(insts)} placed object(s)" if insts else f"{len(owners)} area(s)/blueprint(s)")
        if loops:
            # a warning when it runs more than once (several objects, or an area/module heartbeat); one object: info.
            # A script run only as creatures' heartbeats is info too: it runs only while such a creature exists
            # (most NPCs are spawned, not always there), and more slowly in areas with no players (AI_LEVEL_VERY_LOW,
            # about every 10 s - nwnlexicon AI_LEVEL); NPC heartbeats calling AI or looking around is normal. A
            # tester's report on 1.5.1 found these flagged on every custom NPC.
            only_creatures = all((script, o) in creature_hb for o in owners)
            sev = "warning" if not only_creatures and (len(insts) > 1 or any(o == "module" or o.startswith("area:")
                                                                              for o in owners)) else "info"
            findings.append(dict(severity=sev, category="perf_heavy_heartbeat", node=f"script:{script}", label=script,
                                 detail=f"heartbeat script run by {where} every 6 s walks objects ({', '.join(loops)}) - "
                                        "the cost grows with players/objects; consider a trigger or a timed event instead",
                                 count=len(owners)))
        elif not c.strip() or re.fullmatch(r"\s*void\s+main\s*\(\s*\)\s*\{\s*\}\s*", c):
            if sources.get(script) is not None:
                findings.append(dict(severity="info", category="perf_empty_heartbeat", node=f"script:{script}", label=script,
                                     detail=f"empty heartbeat script on {where}: it still fires every 6 s - clear the event instead",
                                     count=len(owners)))
    # DelayCommand floods / self-rescheduling (only scripts the module can run)
    for nm, src in sources.items():
        if f"script:{nm}" not in live:
            continue
        c = code(nm)
        if "DelayCommand" not in c:
            continue
        for kind, line, text in delay_findings(c):
            findings.append(dict(severity="warning" if kind == "delay_in_loop" and nm in hb_scripts else "info",
                                 category=f"perf_{kind}", node=f"script:{nm}", label=f"{nm}:{line}",
                                 detail=text + (" - and it runs on a heartbeat" if nm in hb_scripts else "")))
    # inventory bloat
    cls_of = dict(db.execute("SELECT node, class FROM objects"))
    # count placed item objects per holder; a store gets a higher threshold than a chest or creature (stores are
    # meant to hold many)
    for container, n_ in db.execute("SELECT container, count(*) FROM objects WHERE class='item' AND container IS NOT NULL "
                                    "AND is_blueprint=0 GROUP BY container"):
        kind = cls_of.get(container, "")
        if kind == "item":
            continue                                   # items inside bags
        limit = STORE_ITEMS_WARN if kind == "store" else CONTAINER_ITEMS_WARN
        if n_ > limit:
            area = container.split(":", 2)[1] if container.startswith("inst:") else None
            if area in rows:
                rows[area]["big_containers"] += 1
            findings.append(dict(severity="warning" if kind in ("store", "placeable") else "info",
                                 category="perf_inventory", node=container, label=g.label(container) if g else container,
                                 detail=f"{kind or 'object'} holds {n_} items (each one is an object the server keeps and sends "
                                        f"to whoever opens it) - over {limit}", count=n_))
    # placeables that could be Static
    # tags some script, transition, key or conversation refers to: such a placeable may be needed at run time
    tag_looked_up = {d[4:] for (d,) in db.execute("SELECT DISTINCT dst FROM edges WHERE kind IN ('tag_ref','literal_match',"
                                                  "'transition_tag','key_tag','speaker_tag')")}
    props = defaultdict(dict)
    labels = ("Static", "Useable", "HasInventory", "Conversation", "Tag") + PLC_EVENTS
    q = ("SELECT f.resref, fi.path, fi.label, fi.value FROM fields fi JOIN files f ON f.id=fi.file_id WHERE f.ext='git' "
         f"AND fi.label IN ({','.join('?' * len(labels))}) AND fi.path LIKE 'Placeable List[%'")
    for area, path, label, val in db.execute(q, labels):
        if path.count("/") != 1:
            continue            # only the placeable's own top-level fields, not those of its inventory or VarTable
        props[(area, path.split("/", 1)[0])][label] = val
    # placeables with variables are often found by scripts that walk the area (visual effects, lighting, spawners):
    # a Static placeable is invisible to GetFirstObjectInArea, so those are never suggested
    with_vars = {(a_, p_.split("/", 1)[0]) for a_, p_ in db.execute(
        "SELECT DISTINCT f.resref, fi.path FROM fields fi JOIN files f ON f.id=fi.file_id WHERE f.ext='git' "
        "AND fi.path LIKE 'Placeable List[%]/VarTable[%' ")}
    static_total = 0
    for (area, key), p in props.items():
        if str(p.get("Static", "0")) == "1" or (area, key) in with_vars:
            continue
        if str(p.get("Useable", "0")) == "1" or str(p.get("HasInventory", "0")) == "1" or p.get("Conversation"):
            continue
        if any(p.get(e) for e in PLC_EVENTS):
            continue
        if p.get("Tag") and p["Tag"] in tag_looked_up:
            continue
        static_total += 1
        if area in rows:
            rows[area]["static_candidates"] += 1
    # per-area findings
    for area, r in rows.items():
        if r["creatures"] > AREA_CREATURES_WARN:
            findings.append(dict(severity="warning", category="perf_spawn_density", node=r["node"], label=r["area"],
                                 detail=f"{r['creatures']} creatures placed in this area (plus up to {r['encounter_max']} from "
                                        "encounters) - every one runs AI and heartbeats; spawn them when players arrive",
                                 count=r["creatures"]))
        if r["custom_heartbeats"] > AREA_CUSTOM_HB_WARN:
            findings.append(dict(severity="warning", category="perf_area_heartbeats", node=r["node"], label=r["area"],
                                 detail=f"{r['custom_heartbeats']} placed objects run custom heartbeat scripts here (each every 6 s)",
                                 count=r["custom_heartbeats"]))
        if r["static_candidates"] >= STATIC_AREA_INFO:
            findings.append(dict(severity="info", category="perf_static_candidates", node=r["node"], label=r["area"],
                                 detail=f"{r['static_candidates']} placeables here could be ticked Static (not usable, no "
                                        "scripts, no inventory, no variables, tag never looked up) - static placeables cost nothing at run time",
                                 count=r["static_candidates"]))
        # rough area load score, only for ordering the areas table: creatures count fully, encounter capacity a third
        # (not all spawn at once), custom heartbeats x3, AI heartbeats half (already in the creature count), items
        # per 20, big containers x5. Like HB_WEIGHTS, these are judgement-based weights, not measurements.
        r["score"] = r["creatures"] + r["encounter_max"] // 3 + 3 * r["custom_heartbeats"] + r["ai_heartbeats"] // 2 + \
            r["items"] // 20 + 5 * r["big_containers"]
    areas = sorted(rows.values(), key=lambda r: -r["score"])
    findings.sort(key=lambda f: ({"error": 0, "warning": 1, "info": 2}[f["severity"]], f["category"], -f.get("count", 0)))
    summary = dict(areas=len(areas), placed_creatures=sum(r["creatures"] for r in areas),
                   custom_heartbeat_objects=sum(r["custom_heartbeats"] for r in areas),
                   ai_heartbeat_objects=sum(r["ai_heartbeats"] for r in areas),
                   heartbeat_scripts=len([s for s in hb_scripts if not _is_standard(s)]),
                   static_candidates=static_total,
                   big_inventories=sum(1 for f in findings if f["category"] == "perf_inventory"),
                   warnings=sum(1 for f in findings if f["severity"] == "warning"),
                   thresholds=dict(area_creatures=AREA_CREATURES_WARN, area_custom_heartbeats=AREA_CUSTOM_HB_WARN,
                                   container_items=CONTAINER_ITEMS_WARN, store_items=STORE_ITEMS_WARN))
    heartbeats = heartbeat_costs(db, code, hb_scripts, live, area_label)
    summary.update(heartbeat_runs_per_min=sum(r["runs_per_min"] for r in heartbeats),
                   heartbeat_top=[dict(script=r["script"], share=r["share"]) for r in heartbeats[:3]])
    return dict(summary=summary, areas=areas, findings=findings, heartbeats=heartbeats)
