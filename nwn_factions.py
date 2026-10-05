"""
nwn_factions.py - who is in which faction, how the factions feel about each other, and which scripts change that.

A module's factions live in repute.fac:
  FactionList   one entry per faction: FactionName, FactionParentID (the faction it was copied from in the toolset;
                0xFFFFFFFF = none), FactionGlobal (1 = a reputation change applies to every member at once)
  RepList       FactionID1, FactionID2, FactionRep: how much faction 2 LIKES faction 1 (0-10 hostile, 11-89 neutral,
                90-100 friendly). The PC faction (0) row = how each faction feels about players.
The first five are the standard factions every module has: PC, Hostile, Commoner, Merchant, Defender.
Members: creatures carry FactionID (blueprints and placed copies - a placed copy can differ from its blueprint);
placeables, doors, triggers and encounters carry Faction.
Scripts change factions with ChangeFaction(oTarget, oMemberOfFaction) - usually a hidden "faction holder" creature
found by tag - ChangeToStandardFaction, AdjustReputation, SetStandardFactionReputation, ClearPersonalReputation and
SetIsTemporaryEnemy/Friend/Neutral (personal, temporary).

Reads:  the analysis index (sqlite3) - repute.fac's GFF fields, the objects table and script sources - and,
        optionally, the dependency graph to say where each faction-changing script runs. Read-only.
Writes: nothing; analyse() returns a dict that nwn_analysis stores in report.json -> factions.
Issues raised: faction_invalid (an object uses a faction number repute.fac doesn't have), faction_holder_missing
(ChangeFaction to the faction of a tag no placed creature has), faction_unused (info).
Limits: scripts are read line by line (comments removed first), so a call split over several lines only shows its
first line's arguments; a faction holder found by a tag built at run time, or spawned at run time, cannot be resolved.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import nwnlib as n

# nwscript.nss constant -> faction index in repute.fac (the standard factions sit at fixed positions 1-4; PC is 0)
STANDARD = {"STANDARD_FACTION_HOSTILE": 1, "STANDARD_FACTION_COMMONER": 2, "STANDARD_FACTION_MERCHANT": 3,
            "STANDARD_FACTION_DEFENDER": 4}
NONE_PARENT = 4294967295        # 0xFFFFFFFF: the toolset's "no parent" / "no faction" value
# The engine functions that change membership or reputation -> plain-English wording for the Factions page
CALLS = {
    "ChangeFaction": "joins the faction of",
    "ChangeToStandardFaction": "joins standard faction",
    "AdjustReputation": "changes how a faction feels about",
    "SetStandardFactionReputation": "sets how a standard faction feels about",
    "ClearPersonalReputation": "clears personal likes/dislikes of",
    "SetIsTemporaryEnemy": "makes a temporary enemy of",
    "SetIsTemporaryFriend": "makes a temporary friend of",
    "SetIsTemporaryNeutral": "makes temporarily neutral",
}
# Any of the CALLS followed by "(": `AdjustReputation(oPC, oNPC, -50)` -> "AdjustReputation"
CALL_RE = re.compile(r"\b(" + "|".join(CALLS) + r")\s*\(")
# A tag lookup with a literal tag: `GetObjectByTag("FAC_GUARDS")` or `GetNearestObjectByTag("FAC_GUARDS")`
# -> "FAC_GUARDS"
BYTAG_RE = re.compile(r'Get(?:Nearest)?ObjectByTag\s*\(\s*"([^"]+)"')


def attitude(rep):
    """hostile / neutral / friendly for a reputation value 0-100 (bands as in the module docstring); None stays None."""
    if rep is None:
        return None
    return "hostile" if rep <= 10 else ("friendly" if rep >= 90 else "neutral")


def _args(text, start):
    """Arguments of the call whose "(" ends just before text[start], split on top-level commas.
    Simple bracket counting on one line: commas or brackets inside string literals are not special-cased."""
    depth, args, cur = 1, [], ""
    for ch in text[start:]:
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
    return args


def analyse(db, g=None, live=None, label=None):
    """Read the module's factions, their members and the scripts that change them.

    db: the analysis index (sqlite3.Connection), read-only; g: the dependency graph or None (then scripts are not
    traced to the objects that run them); live: set of node ids the module reaches; label: callable(node id) -> text.
    Returns dict(summary, factions, scripts, issues, names). A module without repute.fac gives an "error" summary and
    empty lists, not an exception. Never writes anything."""
    label = label or (lambda nid: nid)
    live = live or set()
    # the copy the game uses (nwnlib.source_rank): the first-listed hak, then the module's own, then the override
    row = db.execute("SELECT f.id FROM files f JOIN sources s ON s.id=f.source_id WHERE lower(f.relpath)='repute.fac' "
                     f"ORDER BY {n.source_rank_sql()} LIMIT 1").fetchone()
    if not row:
        return dict(summary=dict(error="this module has no repute.fac (no custom factions)"), factions=[], scripts=[], issues=[])
    fac = defaultdict(dict)
    rep = {}
    reps = defaultdict(dict)
    for path, lab, val in db.execute("SELECT path, label, value FROM fields WHERE file_id=?", (row[0],)):
        # GFF field paths in repute.fac: "FactionList[3]/FactionName", "RepList[17]/FactionRep". The list index of
        # FactionList is the faction's number (what FactionID / Faction fields on objects refer to)
        m = re.match(r"(FactionList|RepList)\[(\d+)\]/(\w+)$", path)
        if not m:
            continue
        (fac if m.group(1) == "FactionList" else reps)[int(m.group(2))][m.group(3)] = val
    for r_ in reps.values():
        try:
            a, b, v = int(r_["FactionID1"]), int(r_["FactionID2"]), int(r_["FactionRep"])
        except (KeyError, ValueError):
            continue
        rep[(b, a)] = v          # b feels v about a  (FactionID2 about FactionID1, as described in the docstring)
    n_fac = len(fac)
    factions = []
    for i in sorted(fac):
        f = fac[i]
        # a parent outside the list (0xFFFFFFFF or a stale number) is shown as no parent
        par = int(f.get("FactionParentID") or NONE_PARENT)
        # to_pc: how this faction feels about players; pc_to: how the PC faction feels about this one;
        # feels: {other faction id: how this faction feels about it}
        factions.append(dict(id=i, name=f.get("FactionName") or f"faction {i}", standard=i < 5,
                             parent=None if par == NONE_PARENT or par >= n_fac else par,
                             glob=str(f.get("FactionGlobal")) == "1",
                             to_pc=rep.get((i, 0)), pc_to=rep.get((0, i)),
                             feels={j: rep.get((i, j)) for j in sorted(fac)}))
    by_id = {f["id"]: f for f in factions}
    for f in factions:
        f["parent_name"] = by_id[f["parent"]]["name"] if f["parent"] is not None else None
        f["attitude_to_pc"] = attitude(f["to_pc"])

    # members: creatures (FactionID) and other objects (Faction), blueprints and placed copies
    # The objects table knows each object's file and GFF path; the faction number is a separate field row whose path
    # is the object's path + "/FactionID" (or "/Faction"). Join the two by (file, object path).
    objs = {}
    for fid_, path_, node, cls, is_bp, area, tag, name, tmpl in db.execute(
            "SELECT file_id, path, node, class, is_blueprint, area, tag, name, template FROM objects "
            "WHERE class IN ('creature','placeable','door','trigger','encounter')"):
        objs[(fid_, path_ or "")] = (node, cls, is_bp, area, tag, name, tmpl)
    rows_ = []
    for fid_, path_, val in db.execute("SELECT file_id, path, value FROM fields WHERE label IN ('FactionID','Faction')"):
        parent = path_.rsplit("/", 1)[0] if "/" in path_ else ""
        o = objs.get((fid_, parent))
        if o:
            rows_.append(o + (val,))
    members = defaultdict(list)
    bad_members = []
    bp_faction = {}
    for node, cls, is_bp, area, tag, name, tmpl, val in rows_:
        try:
            fid = int(val)
        except (TypeError, ValueError):
            continue
        m = dict(node=node, cls=cls, blueprint=bool(is_bp), area=area, tag=tag, name=name, template=tmpl)
        if fid == NONE_PARENT and cls != "creature":
            continue          # 'no faction' on a placeable/door/trigger (the toolset writes 0xFFFFFFFF) - fine
        if fid not in by_id:
            bad_members.append(dict(m, faction=fid))
            continue
        members[fid].append(m)
        # blueprint resref -> faction, to spot placed copies whose faction was changed in the area
        if is_bp and cls == "creature":
            bp_faction[(node[3:].rsplit(".", 1)[0] if node.startswith("bp:") else tmpl or "").lower()] = fid
    node_faction = {m["node"]: fid for fid, ms in members.items() for m in ms}
    # tag -> factions of placed creatures with that tag: resolves ChangeFaction(o, GetObjectByTag("HOLDER"))
    tag_faction = defaultdict(set)
    for fid, ms in members.items():
        for m in ms:
            if m["tag"] and not m["blueprint"] and m["cls"] == "creature":
                tag_faction[m["tag"]].add(fid)
    changed_placed = 0
    for f in factions:
        ms = members.get(f["id"], [])
        cre = [m for m in ms if m["cls"] == "creature"]
        f["creatures_placed"] = sum(1 for m in cre if not m["blueprint"])
        f["creature_blueprints"] = sum(1 for m in cre if m["blueprint"])
        f["objects_placed"] = sum(1 for m in ms if m["cls"] != "creature" and not m["blueprint"])
        f["areas"] = [a for a, _n in Counter(m["area"] for m in cre if m["area"] and not m["blueprint"]).most_common(12)]
        f["examples"] = [dict(node=m["node"], name=m["name"] or m["tag"], area=m["area"], blueprint=m["blueprint"])
                         for m in sorted(cre, key=lambda x: (x["blueprint"], x["area"] or ""))[:40]]
        f["holders"] = sorted({m["tag"] for m in cre if not m["blueprint"] and m["tag"]})[:60]
        for m in cre:
            if not m["blueprint"] and m["template"] and bp_faction.get(m["template"].lower()) not in (None, f["id"]):
                changed_placed += 1

    # scripts that change membership or reputation
    scripts = []
    from nwn_index import lex_nss
    # SQL LIKE narrows the scripts read; the pattern text comes only from the CALLS constants above (no user input)
    for nm, src in db.execute("SELECT name, source FROM scripts WHERE source IS NOT NULL AND ("
                              + " OR ".join(f"source LIKE '%{c}%'" for c in CALLS) + ")"):
        # lex_nss blanks // and /* */ comments but keeps every newline (and string literals), so a commented-out
        # call is not counted and the line numbers reported still match the source
        for ln, text in enumerate(lex_nss(src.replace("\r\n", "\n"))[0].split("\n"), 1):
            for mm in CALL_RE.finditer(text):
                fn = mm.group(1)
                args = _args(text, mm.end())
                target_faction, how_found = None, ""
                if fn == "ChangeFaction" and len(args) > 1:
                    t = BYTAG_RE.search(args[1])
                    if t:
                        fs = tag_faction.get(t.group(1))
                        target_faction = sorted(by_id[x]["name"] for x in fs) if fs else None
                        how_found = f"the faction of '{t.group(1)}'" + ("" if fs else " - NO placed creature has that tag")
                    else:
                        how_found = f"the faction of {args[1][:60]} (found at run time)"
                elif fn == "ChangeToStandardFaction" and len(args) > 1:
                    sid = STANDARD.get(args[1].strip())
                    target_faction = [by_id[sid]["name"]] if sid in by_id else None
                    how_found = args[1].strip()
                elif fn == "SetStandardFactionReputation" and args:
                    sid = STANDARD.get(args[0].strip())
                    target_faction = [by_id[sid]["name"]] if sid in by_id else None
                    how_found = f"{args[0].strip()} -> {args[1].strip() if len(args) > 1 else '?'}"
                elif fn == "AdjustReputation" and len(args) > 2:
                    t = BYTAG_RE.search(args[1])
                    fs = tag_faction.get(t.group(1)) if t else None
                    target_faction = sorted(by_id[x]["name"] for x in fs) if fs else None
                    how_found = f"by {args[2].strip()}" + (f" (faction of '{t.group(1)}')" if t else "")
                if fn in ("AdjustReputation", "SetIsTemporaryEnemy", "SetIsTemporaryFriend", "SetIsTemporaryNeutral") \
                        and not target_faction and g is not None:
                    # "OBJECT_SELF" = the NPC/object running the script: a conversation's speakers, or the object
                    # whose event slot holds it
                    who = args[1] if fn == "AdjustReputation" and len(args) > 1 else (args[1] if len(args) > 1 else "OBJECT_SELF")
                    if "OBJECT_SELF" in who or not who:
                        runners = set()
                        for s_, k, v in g.rev.get(f"script:{nm}", []):
                            if k == "event_script":
                                runners.add(s_)
                            elif k == "dlg_script":
                                runners |= {u for u, k2, v2 in g.rev.get(s_, []) if k2 == "uses_conversation"}
                        fs = {node_faction[r_] for r_ in runners if r_ in node_faction}
                        if fs:
                            target_faction = sorted(by_id[x]["name"] for x in fs)
                            how_found = (how_found + " - " if how_found else "") + "run by members of " + ", ".join(target_faction[:4])
                # where the script runs: event slots, conversations, other scripts (up to 3 shown)
                runs = []
                if g is not None:
                    for s_, k, v in g.rev.get(f"script:{nm}", []):
                        if k in ("event_script", "module_event", "dlg_script", "execute_script", "tag_based_script"):
                            runs.append(f"{(v or k).split('/')[-1]} of {label(s_)}" if k in ("event_script", "module_event")
                                        else f"{k.replace('_', ' ')} from {label(s_)}")
                scripts.append(dict(script=nm, line=ln, call=fn, what=CALLS[fn], args=[a[:80] for a in args[:3]],
                                    factions=target_faction or [], detail=how_found, runs=runs[:3],
                                    live=(f"script:{nm}" in live) if live else None))

    issues = []
    for b in bad_members[:200]:
        issues.append(dict(severity="error", category="faction_invalid", node=b["node"], label=b["name"] or b["tag"] or b["node"],
                           detail=f"{b['cls']} uses faction number {b['faction']}, but repute.fac has only {n_fac} factions "
                                  "(0-" + str(n_fac - 1) + ") - the game falls back to a default and it may act unexpectedly"))
    # a missing faction holder is an error only in a script the module runs; in unused code it is a warning
    for s in scripts:
        if s["call"] == "ChangeFaction" and "NO placed creature" in s["detail"]:
            issues.append(dict(severity="error" if s["live"] else "warning", category="faction_holder_missing",
                               node=f"script:{s['script']}", label=s["script"],
                               detail=f"line {s['line']} joins {s['detail']} - the lookup finds nothing, so the "
                                      "faction never changes (unless that creature is spawned at run time)"))
    for f in factions:
        if not f["standard"] and not f["creatures_placed"] and not f["creature_blueprints"] and not f["objects_placed"] and \
                not any(f["name"] in s["factions"] for s in scripts):
            issues.append(dict(severity="info", category="faction_unused", node="file:repute.fac", label=f["name"],
                               detail=f"faction '{f['name']}' has no creatures, objects or scripts using it"))
    summary = dict(factions=len(factions), custom=sum(1 for f in factions if not f["standard"]),
                   hostile_to_pc=sum(1 for f in factions if f["attitude_to_pc"] == "hostile"),
                   placed_creatures=sum(f["creatures_placed"] for f in factions),
                   script_calls=len(scripts), scripts=len({s["script"] for s in scripts}),
                   placed_differs_from_blueprint=changed_placed, invalid_members=len(bad_members))
    return dict(summary=summary, factions=factions, scripts=scripts, issues=issues,
                names={f["id"]: f["name"] for f in factions})
