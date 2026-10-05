"""
nwn_varaudit.py - variable, tag and token audit.

Variables (local, campaign database, and helper pairs such as SetPLocalInt / SetQuestToken), from every script, plus the
variables builders type into the toolset (the VarTable of blueprints, placed objects, areas and the module):
  read_never_set   a script reads it, but no script, toolset variable or run-time-built name ever sets it
  set_never_read   it is set but nothing reads it (bookkeeping, or read outside the module)
  type_mismatch    set as one type, read as another (NWN keeps int / string / float / object variables apart:
                   SetLocalInt(o, "X", 1) then GetLocalString(o, "X") always gives "")
  case_twin        the same name with different capital letters (variable names are case-sensitive)
  similar_name     read-never-set name one letter away from a name that is set (a typo?)
Tags looked up by scripts (GetObjectByTag, GetWaypointByTag, GetNearestObjectByTag, GetItemPossessedBy ...):
  placed / spawned / runtime (created with a new tag or SetTag) / blueprint_only / case_twin / missing
Token slots (a module's own helper pair, e.g. SetQuestToken / GetQuestToken): every slot of the token string, the names that use it, who sets and
who reads it, and the free slots.

Nothing is changed; this only reads the analysis index.

How: the script parser is nwn_quests.QuestInference (constants, helper pairs, wrappers, EE conversation script
parameters), subclassed here so that object and location locals are counted too. Toolset variables come from the
VarTable lists in the index's GFF fields; tag lookups come from the indexer's script_literals table.
Entry points: run(db, g) -> dict(summary, variables, tags, tokens); issues_from(audit) -> analysis issues.
Limits: a name built at run time is not a variable we can list; it only softens findings (it *might* set or read a
name). Variables set by the game itself, base-game scripts or server plug-ins are invisible, hence "info", not
"warning", for names that look like theirs.
"""
from __future__ import annotations

import re
from collections import defaultdict

import nwn_quests
from nwn_quests import QuestInference

# Names the base game, the expansions' scripts or NWNX use (X2_L_..., NW_GENERIC_..., NWNX_...): when the module
# never sets one, the game or a plug-in probably does, so "read but never set" is only info for these.
ENGINE_PREFIX = re.compile(r"^(X0_|X1_|X2_|X3_|NW_|NWNX)", re.I)
# The Type code of a toolset variable (a VarTable entry) -> its NWScript type; an unknown code is treated as int
VT_TYPES = {"1": "int", "2": "float", "3": "string", "4": "object", "5": "location"}
EXTRA_SET = {"SetLocalObject": "object", "SetLocalLocation": "location"}
EXTRA_GET = {"GetLocalObject": "object", "GetLocalLocation": "location"}
EXTRA_DEL = {"DeleteLocalObject": "object", "DeleteLocalLocation": "location"}
# Functions that give an object a tag at run time -> index of that tag argument:
# CreateObject(type, resref, location, appear animation, NEW TAG), CopyObject(object, location, owner, NEW TAG),
# SetTag(object, NEW TAG). CopyItemAndModify has no tag argument (None), so it is not scanned.
NEW_TAG_ARG = {"CreateObject": 4, "CopyObject": 3, "SetTag": 1, "CopyItemAndModify": None}
MAX_REFS = 8        # example references kept per variable / tag / slot (counts stay complete): keeps report.json small


class _AuditInference(QuestInference):
    """The quest engine's parser, plus object/location locals (not quest state, but still variables)."""

    def _state_funcs(self):
        """The quest parser's state calls plus Set/Get/DeleteLocalObject and ...Location."""
        return super()._state_funcs() | set(EXTRA_SET) | set(EXTRA_GET) | set(EXTRA_DEL)

    def _fact(self, fn, args, code, s, e, al):
        """Object/location locals become set/get facts with value "*" (their values are not tracked); everything
        else is handled by QuestInference._fact."""
        if fn in EXTRA_SET or fn in EXTRA_DEL:
            if len(args) >= 2:
                return dict(op="set", sys="local", key=self.resolve(args[1]), key_expr=args[1], value="*",
                            scope=self.scope(args[0], al), vtype=EXTRA_SET.get(fn) or EXTRA_DEL[fn])
            return None
        if fn in EXTRA_GET:
            if len(args) >= 2:
                return dict(op="get", sys="local", key=self.resolve(args[1]), key_expr=args[1], cmp=None, value=None,
                            scope=self.scope(args[0], al), vtype=EXTRA_GET[fn])
            return None
        return super()._fact(fn, args, code, s, e, al)


def _lev1(a, b):
    """True when a and b differ by exactly one insert, delete or substitution."""
    # Levenshtein distance of exactly 1, without building the full table: same length -> one differing character;
    # lengths differ by one -> skip the common start, then the longer one must equal the shorter after one extra char
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _ref(script, f):
    """Short reference to one read/write: script, line, scope (and the conversation node for parameter scripts)."""
    r = dict(script=script, line=f.get("line"), scope=f.get("scope"))
    if f.get("place"):      # a generic script told the name by this conversation node's parameters
        r["where"] = f"{f['place']['label']} ({f['place']['detail']})"
    return r


def toolset_variables(db):
    """[(name, type, owner node, owner label)] from every VarTable (blueprints, placed objects, areas, module).

    db: the analysis index (sqlite3.Connection), read-only. A VarTable entry is a GFF struct with Name, Type and
    Value fields; the index stores each field as its own row with a path such as
    "Placeable List[4]/VarTable[0]/Name", so Name and Type are paired by the path before the last "/".
    Placed objects in an area (.git) get an "inst:<area>:<path>" owner; anything else is owned by its file's node."""
    files = {fid: (rel, node) for fid, rel, node in db.execute("SELECT id, relpath, node FROM files")}
    names, types = {}, {}
    for fid, path, label, value in db.execute(
            "SELECT file_id, path, label, value FROM fields WHERE label IN ('Name','Type') AND path LIKE '%VarTable[%]/%'"):
        pre = path.rsplit("/", 1)[0]
        (names if label == "Name" else types)[(fid, pre)] = value
    out = []
    for (fid, pre), name in names.items():
        if not name:
            continue
        rel, node = files.get(fid, ("?", None))
        owner_path = pre.rsplit("/VarTable[", 1)[0] if "/VarTable[" in pre else ""
        if owner_path and rel.endswith(".git"):
            owner = f"inst:{rel[:-4]}:{owner_path}"
        else:
            owner = node or f"file:{rel}"
        out.append((name, VT_TYPES.get(str(types.get((fid, pre), "")), "int"), owner,
                    rel + (f" › {owner_path}" if owner_path else "")))
    return out


def audit_variables(qi, facts, toolset):
    """One row per variable (system, name, type) with its findings.

    qi: the _AuditInference that produced the facts (used for run-time-name doubt); facts: [(script, fact)] from
    qi.facts(); toolset: toolset_variables() rows (each counts as a set of a local variable).
    Journal entries and token slots are skipped here (tokens have audit_tokens).
    Finding precedence for a read-never-set variable: type_mismatch, then case_twin, then a run-time-named write
    that could match (info), then engine-like name (info), campaign (info), else warning. similar_name is added on
    top when no type/case explanation was found. Returns the rows, warnings first."""
    vars_ = {}

    def entry(sysname, key, vtype):
        """The row for (system, name, type), created on first use. NWN keeps types apart, so each type is a row."""
        k = (sysname, key, vtype)
        if k not in vars_:
            vars_[k] = dict(system=sysname, name=key, vtype=vtype, scopes=set(), sets=[], reads=[], toolset=[],
                            n_sets=0, n_reads=0)
        return vars_[k]

    for script, f in facts:
        if f["op"] not in ("set", "get"):
            continue
        sysname = f["sys"]
        if sysname == "journal":
            continue
        vt = f.get("vtype") or "int"
        if vt == "token":
            continue        # token slots have their own section
        key = f["key"]
        e = entry(sysname if sysname in ("local", "campaign") else sysname, key, vt)
        e["scopes"].add(f.get("scope") or "")
        if f["op"] == "set":
            e["n_sets"] += 1
            if len(e["sets"]) < MAX_REFS:
                e["sets"].append(_ref(script, f))
        else:
            e["n_reads"] += 1
            if len(e["reads"]) < MAX_REFS:
                e["reads"].append(_ref(script, f))
    for name, vt, owner, label in toolset:
        e = entry("local", name, vt)
        e["n_sets"] += 1
        if len(e["toolset"]) < MAX_REFS:
            e["toolset"].append(dict(node=owner, label=label))
        e["has_toolset"] = True

    by_name = defaultdict(list)                      # (system, name) -> entries of every type
    by_lower = defaultdict(set)                      # (system, lower name) -> names
    set_names = defaultdict(set)                     # system -> names that are set somewhere
    for (sysname, key, vt), e in vars_.items():
        by_name[(sysname, key)].append(e)
        by_lower[(sysname, key.lower())].add(key)
        if e["n_sets"]:
            set_names[sysname].add(key)
    out = []
    for (sysname, key, vt), e in vars_.items():
        findings = []
        others = [x for x in by_name[(sysname, key)] if x is not e]
        if e["n_reads"] and not e["n_sets"]:
            typed = [x for x in others if x["n_sets"]]
            twins = sorted(n for n in by_lower[(sysname, key.lower())] if n != key and n in set_names[sysname])
            # names of 6+ characters only: short names one letter apart (Q1/Q2) are usually different on purpose
            near = [n for n in set_names[sysname] if len(key) >= 6 and n != key and n.lower() != key.lower()
                    and _lev1(n.lower(), key.lower())][:3]
            # a name built entirely at run time ("sVar" from a parameter) could be anything - it doesn't cast doubt
            # on every single variable; only writes with a matching literal part do
            dyn = qi._doubt_writes(sysname, key, skip_wildcards=True)
            if typed:
                findings.append(dict(kind="type_mismatch", level="warning", text=(
                    f"read as {vt}, but only ever set as {', '.join(sorted(x['vtype'] for x in typed))} - NWN keeps the "
                    f"types apart, so this read always gets the empty value")))
            elif twins:
                findings.append(dict(kind="case_twin", level="warning", text=(
                    f"never set under this spelling, but {', '.join(twins)} is - variable names are case-sensitive")))
            elif dyn:
                findings.append(dict(kind="read_never_set", level="info", text=(
                    f"no script sets this exact name, but {len(dyn)} write(s) with a name built at run time could "
                    f"({', '.join(sorted(set(dyn))[:3])})")))
            elif ENGINE_PREFIX.match(key):
                findings.append(dict(kind="read_never_set", level="info", text=(
                    "not set in the module - the name suggests the game, a base-game script or a server plug-in sets it")))
            elif sysname == "campaign":
                findings.append(dict(kind="read_never_set", level="info", text=(
                    "read from the campaign database but never written by the module (another module or a server "
                    "tool may write it; otherwise the read always gets 0 / \"\")")))
            else:
                findings.append(dict(kind="read_never_set", level="warning", text=(
                    "read but never set anywhere in the module - not by a script, not in the toolset - so the read "
                    "always gets 0 / \"\"")))
            if near and not typed and not twins:
                # read but never set, and a set variable is one letter away: very likely a typo bug
                findings.append(dict(kind="similar_name", level="warning", text=(
                    f"a set variable with a nearly identical name exists: {', '.join(near)} - probably a typo, so this "
                    "read never sees the value")))
        elif e["n_sets"] and not e["n_reads"]:
            dynr = qi._doubt_writes(sysname, key, reads=True, skip_wildcards=True)
            if not dynr and not any(x["n_reads"] for x in others):
                only_toolset = e.get("has_toolset") and not e["sets"]
                findings.append(dict(kind="set_never_read", level="info", text=(
                    "set in the toolset but no script reads it (the game, a base-game script or a plug-in may)"
                    if only_toolset else "set but never read by any script in the module (bookkeeping, or read "
                                         "by a server tool / the database)")))
        twins_any = sorted(n for n in by_lower[(sysname, key.lower())] if n != key)
        if twins_any and e["n_sets"] and not any(fd["kind"] == "case_twin" for fd in findings):
            findings.append(dict(kind="case_twin", level="info", text=(
                f"also spelled {', '.join(twins_any[:3])} elsewhere - variable names are case-sensitive, so these "
                "are different variables")))
        e = dict(e, scopes=sorted(s for s in e["scopes"] if s))
        e.pop("has_toolset", None)
        e["findings"] = findings
        # status: the first warning's kind, else the first finding's kind, else "ok"
        e["status"] = next((f["kind"] for f in findings if f["level"] == "warning"), None) or \
            (findings[0]["kind"] if findings else "ok")
        e["level"] = "warning" if any(f["level"] == "warning" for f in findings) else ("info" if findings else "ok")
        out.append(e)
    out.sort(key=lambda x: ({"warning": 0, "info": 1, "ok": 2}[x["level"]], x["system"], x["name"].lower()))
    return out


def audit_tags(db, g, qi):
    """One row per tag that a script looks up, with whether anything in the module can carry that tag.

    db: the analysis index (read-only); g: the dependency graph (g.rev says whether a blueprint is spawned, sold
    or carried) or None (then a blueprint-only tag is reported as such, since nothing shows it reachable); qi: the
    _AuditInference (its comment-free code, for run-time tags). Status: placed / runtime / spawned are ok; blueprint_only, case_twin and missing are warnings,
    because the lookup returns OBJECT_INVALID and the script's action on it usually does nothing, without an error."""
    placed, bp_tags = defaultdict(int), defaultdict(set)
    for tag, is_bp, node in db.execute("SELECT tag, is_blueprint, node FROM objects WHERE tag IS NOT NULL AND tag <> ''"):
        if is_bp:
            bp_tags[tag].add(node)
        else:
            placed[tag] += 1
    # area tags too (GetObjectByTag finds areas); only the module's own .are files when the module source is known
    sid = db.execute("SELECT id FROM sources WHERE kind='module'").fetchone()
    for (tag,) in db.execute("SELECT fi.value FROM fields fi JOIN files f ON f.id=fi.file_id WHERE f.ext='are' "
                             "AND fi.path='Tag'" + (" AND f.source_id=?" if sid else ""), (sid[0],) if sid else ()):
        if tag:
            placed[tag] += 1
    # tags given at run time: CreateObject(..., "NEWTAG"), CopyObject(..., "NEWTAG"), SetTag(o, "NEWTAG")
    runtime = defaultdict(list)
    for script, code in qi.code.items():
        for fn, args, s, _e in nwn_quests.iter_calls(code, {k for k, v in NEW_TAG_ARG.items() if v is not None}):
            i = NEW_TAG_ARG[fn]
            if len(args) > i:
                v = qi.resolve(args[i])
                if v:
                    runtime[v].append(script)

    def obtainable(bp_node):
        """Does anything spawn this blueprint, hold it in an inventory/store, or use it as a template?
        Without a graph nothing can show that, so the answer is no (run() allows g=None)."""
        return g is not None and any(k in ("spawns", "inventory", "template") for _s, k, _v in g.rev.get(bp_node, []))
    lookups = defaultdict(lambda: dict(funcs=set(), refs=[], n=0))
    # the indexer already classified literal arguments of tag-lookup functions as kind 'tag'
    for script, line, func, lit in db.execute("SELECT script, line, func, literal FROM script_literals WHERE kind='tag'"):
        d = lookups[lit]
        d["funcs"].add(func or "")
        d["n"] += 1
        if len(d["refs"]) < MAX_REFS:
            d["refs"].append(dict(script=script, line=line, func=func))
    lower_all = defaultdict(set)
    for t in list(placed) + list(bp_tags) + list(runtime):
        lower_all[t.lower()].add(t)
    out = []
    for tag, d in lookups.items():
        if tag in placed:
            status, level, text = "placed", "ok", f"{placed[tag]} placed object(s)/area(s) have this tag"
        elif tag in runtime:
            status, level, text = "runtime", "ok", "given to an object at run time by " + ", ".join(sorted(set(runtime[tag]))[:3])
        elif tag in bp_tags:
            nodes = sorted(bp_tags[tag])
            if any(obtainable(n) for n in nodes):
                status, level, text = "spawned", "ok", "only on blueprint(s) that are spawned, sold or carried: " + ", ".join(nodes[:3])
            else:
                status, level, text = "blueprint_only", "warning", ("only on blueprint(s) nothing places, spawns or "
                                                                    "sells: " + ", ".join(nodes[:3]) + " - the lookup finds nothing")
        else:
            twins = sorted(lower_all.get(tag.lower(), set()) - {tag})
            if twins:
                status, level, text = "case_twin", "warning", (f"no object has this exact tag, but {', '.join(twins[:3])} "
                                                               "exists - tags are case-sensitive")
            else:
                status, level, text = "missing", "warning", ("no object in the module, its haks or scripts' run-time "
                                                             "tags has this tag - the lookup finds nothing (unless a "
                                                             "name built at run time creates it)")
        out.append(dict(tag=tag, funcs=sorted(x for x in d["funcs"] if x), lookups=d["n"], refs=d["refs"],
                        status=status, level=level, detail=text, placed=placed.get(tag, 0)))
    out.sort(key=lambda x: ({"warning": 0, "ok": 1}[x["level"]], x["status"], x["tag"].lower()))
    return out


def audit_tokens(qi, facts):
    """One row per slot of each token helper's string.

    qi/facts: as for audit_variables. A token system is a helper pair keyed by an int slot (SetQuestToken/GetQuestToken);
    "QuestToken@tut_tokens_inc" is a second string defined by another include. Each system gets: its slots from 0 to the
    string length (or the highest slot used) with status used / shared / set, never read / read, never set / free /
    named, unused / past the end, and the list of free slots. Returns [dict(system, setter, getter, defined_in,
    length, slots, free)]."""
    result = []
    systems = sorted({f["sys"] for _s, f in facts if f.get("vtype") == "token"})
    for sysname in systems:
        base, _, inc = sysname.partition("@")
        setter = "Set" + base
        a = qi.adapters.get(setter)
        if not a:
            continue
        slots = defaultdict(lambda: dict(names=set(), literals=set(), sets=[], reads=[], values=set(), n_sets=0, n_reads=0))
        for script, f in facts:
            # only reads/writes whose slot resolved to a number
            if f.get("sys") != sysname or f["op"] not in ("set", "get") or not str(f.get("key", "")).lstrip("-").isdigit():
                continue
            sl = slots[int(f["key"])]
            expr = f["key_expr"].strip()
            # a bare number (SetQuestToken(oPC, 34, "1")) is recorded apart from a named constant
            (sl["literals"] if re.fullmatch(r"\d+", expr) else sl["names"]).add(expr)
            if f["op"] == "set":
                sl["n_sets"] += 1
                sl["values"].add(f.get("value"))
                if len(sl["sets"]) < MAX_REFS:
                    sl["sets"].append(_ref(script, f))
            else:
                sl["n_reads"] += 1
                if len(sl["reads"]) < MAX_REFS:
                    sl["reads"].append(_ref(script, f))
        home = (inc or a["defined_in"]).lower()
        # slot names the include defines but no script uses: the int constants listed among the used slot names
        # in the include's own order (a token include often lists every quest slot in one block)
        used_names = {n for sl in slots.values() for n in sl["names"]}
        unused_named = defaultdict(list)
        home_src = next((v for k, v in qi.src.items() if k.lower() == home), "")
        ordered = [(m.group(2), m.group(3)) for m in nwn_quests.CONST_RE.finditer(home_src)
                   if m.group(1) == "int" and re.fullmatch(r"\d+", m.group(3))]
        idx = [i for i, (n, _v) in enumerate(ordered) if n in used_names]
        slen = qi.string_len_by_def.get(home) or qi.string_len
        if idx:
            # the block of slot names runs from the first used one to the last int constant that still fits in the
            # string - so a name added after the last used slot ("take the next number") is not reported free
            end = max(idx) + 1
            while slen and end < len(ordered) and 0 <= int(ordered[end][1]) < slen:
                end += 1
            for n, v in ordered[min(idx):end]:
                if n not in used_names and (not slen or 0 <= int(v) < slen):
                    unused_named[int(v)].append(n)
        # without a known string length, show up to the highest slot used or named
        length = slen or (max(list(slots) + list(unused_named) + [0]) + 1)
        rows = []
        for i in range(0, max(length, max(list(slots) + [0]) + 1)):
            sl = slots.get(i)
            names = sorted(sl["names"]) if sl else []
            # the slot's description: the constant's // comment, preferably from the defining include
            label = next((d[2] for n in names for d in qi.all_defs.get(n, ()) if d[2] and d[3].lower() == home), "") or \
                next((qi.consts[n][2] for n in names if n in qi.consts and qi.consts[n][2]), "")
            if not sl:
                status = "named, unused" if unused_named.get(i) else "free"
            elif len(sl["names"]) > 1:
                status = "shared"
            elif sl["n_sets"] and not sl["n_reads"]:
                status = "set, never read"
            elif sl["n_reads"] and not sl["n_sets"]:
                status = "read, never set"
            else:
                status = "used"
            if i >= length:
                status = "past the end"
            rows.append(dict(slot=i, status=status, names=names or sorted(unused_named.get(i, [])),
                             literals=sorted(sl["literals"]) if sl else [], comment=label,
                             values=sorted(v for v in (sl["values"] if sl else ()) if v is not None),
                             n_sets=sl["n_sets"] if sl else 0, n_reads=sl["n_reads"] if sl else 0,
                             sets=sl["sets"] if sl else [], reads=sl["reads"] if sl else []))
        result.append(dict(system=sysname, setter=setter, getter=a["getter"], defined_in=inc or a["defined_in"], length=slen,
                           slots=rows,
                           free=[r["slot"] for r in rows if r["status"] == "free"]))
    return result


def run(db, g):
    """Run the variable, tag and token-slot audits.

    db: the analysis index (sqlite3.Connection), read-only; g: the analysis dependency graph (used for script
    triggers and for blueprint reachability in audit_tags), or None for a run without either.
    Returns dict(summary, variables, tags, tokens).
    Writes nothing; nwn_analysis stores the result in report.json -> var_audit and catches any exception."""
    scripts = nwn_quests.winning_sources(db)
    # conversation nodes and their script parameters too: generic scripts (e.g. gen_c_dobounty) name the variable
    # in a node parameter, so those variables only exist per node
    qi = _AuditInference(scripts, nwn_quests.triggers_from_graph(g) if g is not None else {}, {}, set(), {}, {}, {},
                         (), nwn_quests.dialogue_params(db))
    qi.scan_definitions()
    facts = qi.facts()
    variables = audit_variables(qi, facts, toolset_variables(db))
    tags = audit_tags(db, g, qi)
    tokens = audit_tokens(qi, facts)

    def cnt(rows, key):
        """{value of rows[i][key]: how many rows}."""
        d = defaultdict(int)
        for r in rows:
            d[r[key]] += 1
        return dict(d)
    summary = dict(variables=len(variables), variable_status=cnt(variables, "status"),
                   variable_warnings=sum(1 for v in variables if v["level"] == "warning"),
                   tags=len(tags), tag_status=cnt(tags, "status"),
                   tag_warnings=sum(1 for t in tags if t["level"] == "warning"),
                   token_systems=[dict(system=t["system"], setter=t["setter"], defined_in=t["defined_in"],
                                       length=t["length"], free=len(t["free"]),
                                       used=sum(1 for r in t["slots"] if r["status"] not in ("free", "named, unused")))
                                  for t in tokens],
                   not_followed=qi.not_followed)
    return dict(summary=summary, variables=variables, tags=tags, tokens=tokens)


def issues_from(audit):
    """The high-signal findings, as analysis issues (the page has the rest).

    audit: run()'s result. Only variable type mismatches and case twins at warning level, and tag case twins, are
    raised: these are almost always real bugs, while read-never-set and missing tags have many innocent causes.
    Returns [dict(severity, category, node, label, detail)]."""
    out = []
    for v in audit["variables"]:
        for f in v["findings"]:
            if f["kind"] in ("type_mismatch", "case_twin") and f["level"] == "warning":
                where = ", ".join(f"{r['script']}:{r['line']}" for r in v["reads"][:3])
                out.append(dict(severity="warning", category=f"variable_{f['kind']}", node=f"var:{v['name']}",
                                label=v["name"], detail=f"{v['name']} ({v['vtype']}): {f['text']}. Read in {where}"))
    for t in audit["tags"]:
        if t["status"] == "case_twin":
            where = ", ".join(f"{r['script']}:{r['line']}" for r in t["refs"][:3])
            out.append(dict(severity="warning", category="tag_case", node=f"tag:{t['tag']}", label=t["tag"],
                            detail=f"{t['tag']}: {t['detail']}. Looked up in {where}"))
    return out
