"""
nwn_quests.py - infer quests that do not use the journal.

Many modules - persistent worlds especially - keep quest progress in variables instead of the journal:
  - "token" systems: a helper pair such as SetQuestToken(oPC, SLOT, "1") / GetQuestToken(oPC, SLOT) == "1"
    (e.g. q_tokens_inc: one character per quest in a string on the player's hide), or SetPersistentInt/GetPersistentInt
  - local variables on the player or the module: SetLocalInt(GetPCSpeaker(), "nDoneKobolds", 1)
  - the campaign database: SetCampaignInt("pwdata", "QUEST_X", 2, oPC)
  - the journal itself: AddJournalQuestEntry("q_rats", 2, oPC)

This module reads every script's source (from the analysis index), finds those reads and writes, auto-detects
helper pairs (Set<X>(object, key, value) / Get<X>(object, key) defined in the module's own includes), resolves
named constants (const int QT_DONEOGRE = 34; // comment), and builds one "quest" per piece of state, with:
  - its stages (every value set, every value checked) and where each happens (conversation node, object event,
    another script), the NPCs whose conversations drive it, items it needs / gives / takes, rewards
  - completability problems in plain words: a check that can never be true, state read but never set, a token
    slot shared by two quests, a slot past the end of the token string, needed items nobody can obtain,
    state only touched by scripts the module never runs.
Nothing here guesses silently: dynamic names ("Q_" + sTag) are counted and reported as "not followed".

How a quest is inferred (in order)
----------------------------------
  1. scan_definitions  - every `const` in every script, and every Set<X>/Get<X> helper pair whose name looks like
                         quest state (token, quest, plot, stage...). These become "adapters": calls to them are read
                         like engine calls. The token string's length comes from a *_STR_LEN-style constant.
  2. facts             - every call that reads or writes state (helpers, Set/GetLocal*, Set/GetCampaign*,
                         AddJournalQuestEntry), checks/gives/takes items, or gives gold/XP. Variable names are
                         resolved through constants and single-assignment variables; a name built at run time is
                         turned into a regex (name_pattern) and kept only as "doubt" for later checks.
  3. EE script parameters - a generic conversation script that reads the variable name from GetScriptParam("...")
                         becomes a template; each conversation node that runs it with parameters yields one concrete
                         fact (expand_param_templates), honouring the script's if/else branches for those values.
  4. run               - facts are grouped into one quest per (system, scope, name, type); each quest gets stages,
                         places, items, rewards, problems and a health: broken / warning / ok / legacy.

Health: "broken" means the toolkit has proof inside the module that a player cannot progress (a live check that can
never be true, a token slot past the end of its string, two quests sharing a slot). "warning" means a likely problem
that something outside the toolkit's view could still explain (a run-time-built name, a base-game item, a DM).
"legacy" means every script touching it is unused by the module.

Reads:  script sources and the dependency graph from the analysis index (sqlite3) - read-only.
Writes: nothing; it returns a dict that nwn_analysis stores in report.json -> inferred_quests.

Limits: this is a text-level reader of NWScript, not a compiler. It follows constants, simple aliases (one
assignment), wrappers that pass their parameter on as the variable name (two levels), and if/else guards in the same
function. It does not follow loops, switch cases, values computed at run time, or state set outside the module
(a server tool, another module sharing the campaign database).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

import nwnlib as n
from nwn_index import lex_nss

# functions that return the player (or a party member) in the usual events.
# Matches the function name followed by "(": `GetPCSpeaker()`, `GetFirstFactionMember(oPC)`.
PC_FUNC = re.compile(r"\b(GetPCSpeaker|GetEnteringObject|GetExitingObject|GetLastUsedBy|GetFirstPC|GetNextPC|"
                     r"GetFirstFactionMember|GetNextFactionMember|GetLastKiller|GetItemActivator|GetLastOpenedBy|"
                     r"GetLastClosedBy|GetClickingObject|GetLastPerceived|GetItemPossessor|GetModuleItemAcquiredBy|"
                     r"GetModuleItemLostBy|GetPCChatSpeaker|GetLastPCRested|GetLastPlayerDied|GetLastPlayerDying|"
                     r"GetLastRespawnButtonPresser|GetPCLevellingUp|GetLastDisturbed|GetLastSpeaker|"
                     r"GetPlaceableLastClickedBy|GetLastAttacker|GetLastDamager|GetPCItemLastEquippedBy|"
                     r"GetPCItemLastUnequippedBy|GetLastSpellCaster|GetLastPlayerToSelectTarget|GetLastGuiEventPlayer|"
                     r"GetFactionLeader)\s*\(")
# variable names builders use for the player (a guess used only when the script doesn't say where the object came from)
# Searched anywhere in the name, any case: oPC, oPlayer, oTarget, oSkin. scope() rules out names containing "npc" first.
PC_NAME = re.compile(r"pc|player|speaker|enter|killer|activator|user|perceived|clicker|disturb|possessor|member|hero|"
                     r"target|creature|char|leader|party|opener|hide|skin", re.I)
# A simple assignment `name = expression;` (the type word is optional, so `string sVar = "Q_X";` and `oPC = x;` both
# match): `object oPC = GetPCSpeaker();` -> ("oPC", "GetPCSpeaker()"). The first character of the expression may not
# be "=", so a comparison `a == b` is not taken for an assignment.
OBJ_ASSIGN = re.compile(r"\b(?:object\s+)?([A-Za-z_]\w*)\s*=\s*([^;=][^;]*);")
# Prototype or definition of a setter helper at the start of a line: (object, int|string key, value [= default]).
# `void SetQuestToken(object oPC, int nSlot, string sValue = "1")` -> ("SetQuestToken", "int", "string", '"1"').
PROTO_SET = re.compile(r"^\s*void\s+(Set\w+)\s*\(\s*object\s+\w+\s*,\s*(int|string)\s+\w+(?:\s*=\s*[^,)]*)?\s*,"
                       r"\s*(int|string|float)\s+\w+(?:\s*=\s*([^,)]+))?\s*\)", re.M)
# The matching getter: `string GetQuestToken(object oPC, int nSlot)` -> ("string", "GetQuestToken", "int").
PROTO_GET = re.compile(r"^\s*(int|string|float)\s+(Get\w+)\s*\(\s*object\s+\w+\s*,\s*(int|string)\s+\w+"
                       r"(?:\s*=\s*[^,)]*)?\s*\)", re.M)
# A whole-line constant with an optional trailing // comment (the comment becomes the quest's label):
# `const int QT_DONEOGRE = 34; // 061019 jdoe - ogre cave quest`
#   -> ("int", "QT_DONEOGRE", "34", "061019 jdoe - ogre cave quest").
# The value may be hex (0x1F), decimal, a quoted string, or a bare word/float (another constant's name is kept as text).
CONST_RE = re.compile(r"^\s*const\s+(int|string|float)\s+(\w+)\s*=\s*(-?0[xX][0-9a-fA-F]+|-?\d+|\"[^\"]*\"|[\w.]+)\s*;"
                      r"[ \t]*(?://+\s*(.*))?$", re.M)
# A NWScript string literal on one line, with backslash escapes: "Q_RATS", "say \"hi\"". Pattern text, not compiled,
# because it is embedded in other patterns.
STR_LIT = r'"(?:\\.|[^"\\\n])*"'
# EE conversation script parameters: GetScriptParam("VAR_NAME"), optionally wrapped in StringToInt/StringToFloat.
# Must be the whole expression: `StringToInt(GetScriptParam("VALUE"))` -> "VALUE".
SCRIPT_PARAM = re.compile(r'^(?:StringTo(?:Int|Float)\s*\(\s*)?GetScriptParam\s*\(\s*"([^"]+)"\s*\)\s*\)?$')

# helper pairs that hold quest/plot state (others - arrays, counters, timers - are left alone).
# Both are searched in the helper's name after "Set": SetQuestToken -> "QuestToken" is state; SetArrayString is not.
STATE_NAME = re.compile(r"token|quest|plot|stage|state|flag|persist|journal|plocal|campaign|db|progress|step", re.I)
NOT_STATE = re.compile(r"array|bound|count|hour|every|timer|time|index|size|len", re.I)
# Engine functions -> the variable type they handle. NWN keeps int, string and float variables of the same name apart,
# so the type is part of a variable's identity throughout this module.
ENGINE_LOCAL = {"SetLocalInt": "int", "SetLocalString": "string", "SetLocalFloat": "float"}
ENGINE_LOCAL_GET = {"GetLocalInt": "int", "GetLocalString": "string", "GetLocalFloat": "float"}
CAMPAIGN_SET = {"SetCampaignInt": "int", "SetCampaignString": "string", "SetCampaignFloat": "float"}
CAMPAIGN_GET = {"GetCampaignInt": "int", "GetCampaignString": "string", "GetCampaignFloat": "float"}
ITEM_CHECK = {"HasItem": 1, "GetItemPossessedBy": 1}          # function -> index of the tag argument
ITEM_GIVE = {"CreateItemOnObject": 0, "CreateItemOnObjectVoid": 0}   # -> index of the blueprint resref argument
GOLD = {"GiveGoldToCreature": 1, "GiveGoldToAll": 0, "TakeGoldFromCreature": 0}   # -> index of the amount argument
XP = {"GiveXPToCreature": 1, "RewardPartyXP": 0, "GiveXPToAll": 0, "SetXP": 1}
# What a variable reads as before anything sets it (NWN returns 0 / "" for an unset local or campaign variable)
INITIAL = {"int": "0", "string": "", "float": "0", "token": "0", "journal": "0"}
DELETE_LOCAL = {"DeleteLocalInt": "int", "DeleteLocalString": "string", "DeleteLocalFloat": "float"}


# ------------------------------------------------------------------ tiny NWScript call parser
def iter_calls(code, names):
    """Yield (name, [arg strings], start, end) for calls to any of `names` (nested parentheses handled).

    code:  NWScript text, normally comment-free (nwn_index.lex_nss output) so a call inside a comment is not seen.
    names: set of function names to look for.
    start is the offset of the function name, end the offset just after the closing ")". Arguments are the raw text
    between top-level commas, stripped; commas and brackets inside string literals are ignored.
    `SetLocalInt(oPC, "Q_" + s, 1)` -> ("SetLocalInt", ["oPC", '"Q_" + s', "1"], start, end).
    A match whose brackets never close (or hit ; { }) is skipped. Never raises."""
    if not names:
        return
    pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\s*\(")
    n = len(code)
    for m in pat.finditer(code):
        i, depth, args, cur, instr = m.end(), 1, [], [], False
        while i < n and depth:
            c = code[i]
            if instr:
                cur.append(c)
                if c == "\\" and i + 1 < n:          # escaped character inside a string: \" does not end it
                    cur.append(code[i + 1]); i += 2
                    continue
                if c == '"' or c == "\n":
                    instr = False
            elif c in ";{}":
                break                                  # a call's arguments never contain these: not a real call
            elif c == '"':
                instr = True; cur.append(c)
            elif c == "(":
                depth += 1; cur.append(c)
            elif c == ")":
                depth -= 1
                if depth:
                    cur.append(c)
            elif c == "," and depth == 1:
                args.append("".join(cur).strip()); cur = []
            else:
                cur.append(c)
            i += 1
        if depth == 0:
            if "".join(cur).strip() or args:
                args.append("".join(cur).strip())
            yield m.group(1), args, m.start(), i


def comparison_after(code, end, before_start):
    """('==', 'X') for `Call(...) == X`; ('!', '') for `!Call(...)`; (None, None) when used bare.

    code: the script text; end: offset just after the call's ")"; before_start: offset of the call's name.
    X is the literal text (a number, a quoted string or a constant name), not yet resolved.
    `3 == Call(...)` is also understood and returned as ('==', '3'). Only looks 80 characters either side."""
    # `GetLocalInt(oPC, "Q") >= 2`: an operator, then a string literal or a word/number right after the call
    m = re.match(r"\s*(==|!=|>=|<=|>|<)\s*(" + STR_LIT + r"|[-\w.]+)", code[end:end + 80])
    if m:
        return m.group(1), m.group(2)
    # the reversed form `2 == GetLocalInt(...)`: a literal and == / != immediately before the call
    m = re.search(r"(" + STR_LIT + r"|[-\w.]+)\s*(==|!=)\s*$", code[max(0, before_start - 80):before_start])
    # `3 == Get(...)` only when nothing else follows the call (not `3 == Get(...) + 1`)
    if m and re.match(r"\s*(\)|&&|\|\||;|\?|,|$)", code[end:end + 4]):
        return m.group(2), m.group(1)
    if re.search(r"!\s*$", code[max(0, before_start - 3):before_start]):
        return "!", ""
    return None, None


def line_of(code, pos):
    """1-based line number of offset pos. lex_nss keeps every newline when it removes comments, so this is also the
    line number in the original .nss source."""
    return code.count("\n", 0, pos) + 1


# ------------------------------------------------------------------ inference
class QuestInference:
    """Infers quests from script source. Build it with the analysis' inputs, then call run() once.

    It only reads the dictionaries it is given (never the index, never the module), so it can be used in tests with
    hand-written scripts. nwn_varaudit subclasses it to reuse the parser for every variable, not just quest state.
    """

    def __init__(self, scripts, triggers, labels, live, items_by_tag, items_by_res, dlg_owners, base_names=(),
                 node_params=None, instance_tags=None):
        """
        scripts:  {name: source}                         (only scripts with source)
        triggers: {script name: [(kind, by_node, via)]}  (dlg_script / event_script / module_event / execute_script)
        labels:   node -> label
        live:     set of nodes the module reaches
        items_by_tag / items_by_res: from the analysis items list (placed / carried_by / created_by)
        dlg_owners: {dlg node: [creature labels]}
        base_names: names of the game's own resources ("nw_it_gem001.uti"...); empty when the install wasn't read
        node_params: {(dlg node, via): {param: value}} - EE conversation script parameters set on each node
                     (ActionParams / ConditionParams), for scripts that take the variable name as a parameter
        instance_tags: {tag: [where]} - items placed in the world (creature/placeable/store inventories) whose own
                     tag differs from their blueprint's (builders retag a copy: a 'Head of X' made from an ogre head)
        """
        # one newline style, so line numbers and ^/$ regexes work whatever editor saved the script
        self.src = {k: v.replace("\r\n", "\n").replace("\r", "\n") for k, v in scripts.items()}
        self.triggers = triggers
        self.labels = labels
        self.live = live
        self.items_by_tag = items_by_tag
        self.items_by_res = items_by_res
        self.dlg_owners = dlg_owners
        self.base_names = set(base_names)
        self.node_params = node_params or {}
        self.instance_tags = instance_tags or {}
        self.param_templates = defaultdict(list)  # script -> facts whose name/value come from GetScriptParam(...)
        self.param_facts = 0                      # facts expanded from conversation nodes' script parameters
        self.param_items = defaultdict(list)      # (dlg node, via) -> item facts expanded there
        self.param_rewards = defaultdict(list)    # (dlg node, via) -> rewards given there (GOLD / XP parameters)
        self.param_given_res = set()              # blueprints handed out by parameter (REWARD_ITEM)
        self._live_has_dlg = any(str(x).startswith("dlg:") for x in live)
        self.consts = {}           # name -> (type, value, comment, script)
        self.const_conflicts = {}  # name -> sorted distinct values, when scripts disagree
        self.adapters = {}         # setter -> dict(getter, key_type, value_type, default, defined_in)
        self.getters = {}          # getter -> setter
        self.code = {}             # script -> source without comments (lex_nss), same line numbers
        self.not_followed = 0      # reads/writes whose variable name is only known at run time
        self.unfollowed_sets = defaultdict(list)  # system -> [(regex of names it could write, script)] for writes
                                                  # whose name is built at run time
        self.unfollowed_gets = defaultdict(list)  # the same for reads
        self.other_sets = defaultdict(list)       # (sys, key, vtype) -> writes on an object we couldn't identify
        self.string_len = None        # length of the token string (slots past it can never be stored)
        self.string_len_note = ""
        self.string_len_by_def = {}   # defining include -> token string length
        self.includes = {}            # script -> scripts it #includes directly
        self._closure = {}            # cache for closure()
        self.all_defs = {}            # constant -> every definition
        self._cur_script = None       # the script facts() is reading, so resolve() can pick that script's constants

    # -- pass 1: constants and helper pairs
    def scan_definitions(self):
        """Pass 1: read every constant and find the helper pairs (adapters) that hold quest state.

        Fills self.consts, self.const_conflicts, self.all_defs, self.adapters, self.getters, self.code,
        self.includes and the token-string length (self.string_len / string_len_by_def). Returns nothing.
        A pair counts as an adapter when Set<X>(object, key, value) and Get<X>(object, key) both exist, X looks like
        state (STATE_NAME) and not like an array/counter/timer (NOT_STATE)."""
        # how many scripts #include each file: used below to pick the "main" definition when several files define
        # the same constant or helper
        inc_count = defaultdict(int)
        for src in self.src.values():
            for m in re.finditer(r'#\s*include\s+"([^"]+)"', src):
                inc_count[m.group(1).lower()] += 1
        all_defs = defaultdict(list)        # name -> [(type, value, comment, script)]
        for name, src in self.src.items():
            # constants inside /* ... */ are not real (line comments can't start a `const` line anyway)
            # a `//****` banner is a line comment, not the start of a /* block (it hid everything below it)
            # So: // comments are kept (CONST_RE reads them as the constant's label); /* blocks */ become the same
            # number of newlines. "//" is listed first in the alternation so it wins at the same position.
            nocomment = re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: m.group(0) if m.group(0).startswith("//")
                               else "\n" * m.group(0).count("\n"), src, flags=re.S)
            for m in CONST_RE.finditer(nocomment):
                typ, cname, val, comment = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()
                if re.fullmatch(r"-?0[xX][0-9a-fA-F]+", val):
                    val = str(int(val, 16))
                all_defs[cname].append((typ, val.strip('"'), comment, name))
            # the code every later pass reads: no comments (a call inside a comment is not a use), strings intact
            code, incs, _lits = lex_nss(src)
            self.code[name] = code
            self.includes[name.lower()] = {i for _ln, i in incs}
            sets = {m.group(1): m for m in PROTO_SET.finditer(code)}
            gets = {m.group(2): m for m in PROTO_GET.finditer(code)}
            for sname, sm in sets.items():
                gname = "Get" + sname[3:]          # SetQuestToken -> GetQuestToken
                # the engine's own Set/GetLocal* and Set/GetCampaign* are handled directly, never as adapters
                if self._key_switched(code, sname):
                    continue        # SetState(o, iRegion, n): switch(iRegion) picks a named variable - follow those
                if gname in gets and sname not in ENGINE_LOCAL and sname not in CAMPAIGN_SET and \
                        STATE_NAME.search(sname[3:]) and not NOT_STATE.search(sname[3:]):
                    default = (sm.group(4) or "").strip() or None      # resolved later (it may be a constant)
                    prev = self.adapters.get(sname)
                    defs = (prev["defined_in_all"] if prev else []) + [name]
                    self.adapters[sname] = dict(getter=gname, key_type=sm.group(2), value_type=sm.group(3),
                                                default=default, defined_in=name, defined_in_all=defs)
                    self.getters[gname] = sname
        # the same constant in several scripts: use the definition from the most-#included one; note disagreements
        self.all_defs = all_defs
        for cname, defs in all_defs.items():
            self.consts[cname] = max(defs, key=lambda d: inc_count.get(d[3].lower(), 0))
            vals = sorted({d[1] for d in defs})
            if len(vals) > 1:
                self.const_conflicts[cname] = vals
        # several includes may define the same helpers (e.g. q_tokens_inc and tut_tokens_inc): name the one
        # most scripts actually #include
        for a in self.adapters.values():
            a["defined_in"] = max(a["defined_in_all"], key=lambda x: inc_count.get(x.lower(), 0))
        # the token string's length (e.g. QT_STR_LEN in q_tokens_inc): only from the token helpers' own includes, and only
        # when they agree - an unrelated CHAT_STR_LEN elsewhere must not decide it
        # Token helpers are the adapters keyed by an int slot. The name test matches STR_LEN, STRING_LEN, TOKENLEN...
        token_incs = {x.lower() for a in self.adapters.values() if a["key_type"] == "int" for x in a["defined_in_all"]}
        cands = {int(d[1]) for cname, defs in all_defs.items() if re.search(r"(STR|STRING|TOKEN)_?LEN", cname)
                 for d in defs if d[0] == "int" and d[1].isdigit() and d[3].lower() in token_incs}
        if len(cands) == 1:
            self.string_len = cands.pop()
        elif len(cands) > 1:
            self.string_len_note = f"several token-length constants disagree ({', '.join(map(str, sorted(cands)))})"
        # ...and per defining include, for modules with two token systems (e.g. q_tokens_inc's sQuest_PC and
        # tut_tokens_inc's sTut_Quest use the same function names)
        for inc in token_incs:
            c = {int(d[1]) for cname, defs in all_defs.items() if re.search(r"(STR|STRING|TOKEN)_?LEN", cname)
                 for d in defs if d[0] == "int" and d[1].isdigit() and d[3].lower() == inc}
            if len(c) == 1:
                self.string_len_by_def[inc] = c.pop()
        # if the includes disagree, use the length from an include that is a helper pair's main (most-included)
        # definition
        primary = {a["defined_in"].lower() for a in self.adapters.values()}
        if self.string_len is None and len(self.string_len_by_def) > 1:
            self.string_len = next((v for k, v in self.string_len_by_def.items() if k in primary), None)

    @staticmethod
    def _key_switched(code, fname):
        """Does the helper's body `switch` on its key parameter (mapping it to other variable names)?

        Such a helper is not a key/value store: each case writes a differently named variable, and those inner
        calls are followed as ordinary facts instead. Looks only at the helper's definition (with a body)."""
        m = re.search(r"^[ \t]*void\s+" + re.escape(fname) + r"\s*\(([^)]*)\)\s*\{", code, re.M)
        if not m:
            return False
        params = [p_.strip().split()[-1].split("=")[0].strip() for p_ in m.group(1).split(",") if p_.strip()]
        if len(params) < 2:
            return False
        depth, i = 1, m.end()
        while i < len(code) and depth:
            depth += {"{": 1, "}": -1}.get(code[i], 0)
            i += 1
        return bool(re.search(r"switch\s*\(\s*" + re.escape(params[1]) + r"\s*\)", code[m.end():i]))

    def closure(self, script):
        """Every script `script` #includes, directly or through other includes (itself included).
        Names are lower-case; the result is cached. Include cycles are safe (each name is visited once)."""
        script = script.lower()
        if script in self._closure:
            return self._closure[script]
        seen, todo = set(), [script]
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            todo += list(self.includes.get(x, ()))
        self._closure[script] = seen
        return seen

    def system_for(self, fn, script):
        """Token-system name for a helper call. With one definition it is the helper's name (QuestToken); when several
        includes define the same helpers (separate strings), calls through a less-used include get '@include'."""
        base = fn[3:]
        name = fn if fn in self.adapters else self.getters.get(fn)
        a = self.adapters.get(name)
        if not a or len(set(x.lower() for x in a["defined_in_all"])) < 2 or not script:
            return base
        cl = self.closure(script)
        mine = [x for x in a["defined_in_all"] if x.lower() in cl]
        if not mine or a["defined_in"] in mine:
            return base
        return f"{base}@{mine[0]}"

    def resolve(self, expr):
        """Literal value of an argument: resolves named constants; None when it is computed at runtime.

        expr: argument text. Returns a string: "34" for 34, 0x22 or QT_DONEX (= 34); "Q_X" for "Q_X" (quotes
        removed); "1"/"0" for TRUE/FALSE. When several scripts define the constant, the definition the current
        script (self._cur_script) actually #includes wins. A constant defined as another constant's name is
        returned as that name, not followed further."""
        e = (expr or "").strip()
        if re.fullmatch(r"-?\d+", e):
            return str(int(e))
        if re.fullmatch(r"-?0[xX][0-9a-fA-F]+", e):
            return str(int(e, 16))
        if re.fullmatch(STR_LIT, e):
            return e[1:-1]
        if e in ("TRUE", "FALSE"):
            return "1" if e == "TRUE" else "0"
        if e in self.consts:
            defs = self.all_defs.get(e) or ()
            if len(defs) > 1 and self._cur_script:
                cl = self.closure(self._cur_script)
                mine = [d for d in defs if d[3].lower() in cl]
                if mine:
                    return mine[0][1]     # the definition this script actually #includes
            return self.consts[e][1]
        return None

    @staticmethod
    def aliases(code):
        """{variable: [expressions assigned to it]} - `object oMember = GetFirstFactionMember(oPC);` etc.
        Whole-script, not per function: a name reused in two functions collects both assignments."""
        out = defaultdict(list)
        for m in OBJ_ASSIGN.finditer(code):
            out[m.group(1)].append(m.group(2))
        return out

    @classmethod
    def scope(cls, obj_expr, aliases=None, depth=0):
        """pc / module / self / other for the object a variable is stored on.

        obj_expr: the object argument's text (`GetPCSpeaker()`, `oPC`, `GetModule()`); aliases: from aliases().
        Variables are followed through their assignments up to 3 levels. A bare name with no assignment falls back
        to a guess from the name (PC_NAME). run() only turns local variables on "pc" or "module" into quests;
        "self"/"other" locals are an NPC's or object's own working state."""
        o = obj_expr.replace(" ", "")
        if "GetModule()" in o:
            return "module"
        if o in ("OBJECT_SELF", "") or o.startswith("OBJECT_SELF"):
            return "self"
        if o.startswith("GetLocalObject("):
            return "other"      # an object remembered on the player (a widget, a horse...), not the player
        if PC_FUNC.search(o):
            return "pc"
        # an item the player wears or carries (some modules keep quest state on the PC hide/skin):
        #   GetItemInSlot(INVENTORY_SLOT_CARMOUR, oPC), GetItemPossessedBy(oPC, "tag"), GetPCHide(oPC)
        m = re.match(r"(\w*(?:Item|Hide|Skin|Widget)\w*)\(", o)
        if m and depth < 3:
            for _fn, args, _s, _e in iter_calls(o, {m.group(1)}):
                if any(cls.scope(x, aliases, depth + 1) == "pc" for x in args):
                    return "pc"
                break
        if re.fullmatch(r"[A-Za-z_]\w*", o):
            if aliases and o in aliases and depth < 3:
                found = {cls.scope(x, aliases, depth + 1) for x in aliases[o]}
                for sc in ("pc", "module"):
                    if sc in found:
                        return sc
                return "self" if found == {"self"} else "other"     # the script says where it came from
            if "npc" not in o.lower() and PC_NAME.search(o):
                return "pc"
        return "other"

    # -- pass 2: facts
    def _state_funcs(self):
        """Names of every call that reads, writes or deletes state (adapters, locals, campaign, journal).
        nwn_varaudit overrides this to add object/location locals."""
        return set(self.adapters) | set(self.getters) | set(ENGINE_LOCAL) | set(ENGINE_LOCAL_GET) | \
            set(CAMPAIGN_SET) | set(CAMPAIGN_GET) | set(DELETE_LOCAL) | {"DeleteCampaignVariable", "AddJournalQuestEntry"}

    def find_wrappers(self):
        """Helper functions that pass their own parameter on as the variable name:
             void SetStage(object o, string sQuest, int n) { SetLocalInt(o, sQuest, n); }
        Calls to SetStage(oPC, "Q_BEAR", 2) are then followed as SetLocalInt(oPC, "Q_BEAR", 2).
        {wrapper name: (underlying function, [("param", i) | ("expr", text)] per underlying argument, defining script)}
        Also finds wrappers of the item/gold/XP calls (GiveReward(oPC, sItem) { CreateItemOnObject(sItem, oPC); })."""
        wrappers = {}
        state_funcs = self._state_funcs()
        # a function definition at the start of a line: `int SetStage(object o, string s, int n) {` -> name, params
        head = re.compile(r"^[ \t]*(?:void|int|string|float|object)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*\{", re.M)
        give_funcs = set(ITEM_GIVE) | set(GOLD) | set(XP)
        for _round in range(2):                 # a wrapper of a wrapper
            funcs = state_funcs | give_funcs | set(wrappers)
            for script, code in self.code.items():
                for m in head.finditer(code):
                    fname = m.group(1)
                    if fname in state_funcs or fname in wrappers or fname in ("main", "StartingConditional"):
                        continue
                    # parameter names only: "string sQuest = \"\"" -> "sQuest"
                    params = [p_.strip().split()[-1].split("=")[0].strip() for p_ in m.group(2).split(",") if p_.strip()]
                    # the body runs to the matching "}" (plain counting: a brace inside a string is counted too)
                    body, depth, i = [], 1, m.end()
                    while i < len(code) and depth:
                        depth += {"{": 1, "}": -1}.get(code[i], 0)
                        i += 1
                    body = code[m.end():i]
                    # quest state first: SelectBELegacyHide() also creates an item, but what it is for is the token
                    for pool in (funcs - give_funcs, give_funcs):
                        for fn, args, _s, _e in iter_calls(body, pool):
                            key_i = self._key_index(fn)
                            if fn in wrappers:
                                continue
                            # a wrapper only when the variable-name (or item/amount) argument IS one of its parameters
                            if len(args) > key_i and args[key_i].strip() in params and fn not in ("AddJournalQuestEntry",):
                                mapping = [("param", params.index(a_.strip())) if a_.strip() in params else ("expr", a_)
                                           for a_ in args]
                                wrappers[fname] = (fn, mapping, script)
                                break
                        if fname in wrappers:
                            break
        return wrappers

    @staticmethod
    def _key_index(fn):
        """Which argument names the variable (or the item / the amount) for a call.
        Default 1: Set/GetLocal*(object, name...), Set/GetCampaign*(campaign, name...) and the adapters."""
        for d in (ITEM_GIVE, GOLD, XP, ITEM_CHECK):
            if fn in d:
                return d[fn]
        return 0 if fn in ("DeleteCampaignVariable", "AddJournalQuestEntry") else 1

    def facts(self):
        """[(script, fact dict)] - every read/write/item/reward, with line numbers.

        Call scan_definitions() first. A fact is a dict with "op": set / get / item_check / item_take / item_give /
        reward, plus (for set/get) "sys" (local, campaign, journal or the adapter's name), "key", "value", "scope",
        "vtype". Reads/writes whose name is only known at run time are not returned: they are counted in
        not_followed and kept as regexes in unfollowed_sets / unfollowed_gets. Facts from EE script-parameter
        templates are appended at the end, one per conversation node."""
        out = []
        self.wrappers = self.find_wrappers()
        names = self._state_funcs() | set(ITEM_CHECK) | set(ITEM_GIVE) | set(GOLD) | set(XP) | {"DestroyObject"} | \
            set(self.wrappers)
        helper_defs = {x for a in self.adapters.values() for x in a["defined_in_all"]} | \
            {w[2] for w in self.wrappers.values()}
        for script, code in self.code.items():
            self._cur_script = script
            al = self.aliases(code)
            for fn, args, s, e in iter_calls(code, names):
                if script in helper_defs and self._inside_definition(code, s, fn):
                    continue        # a helper's own prototype/definition line, not a use
                if fn in self.wrappers:
                    # rewrite the call as the underlying engine/helper call, filling in the caller's arguments
                    under, mapping, _where = self.wrappers[fn]
                    args = [args[i] if kind == "param" and i < len(args) else ("" if kind == "param" else i)
                            for kind, i in mapping]
                    fn = under
                elif script in helper_defs and self._key_is_parameter(code, s, fn, args):
                    continue        # the wrapper's own body: followed through its callers instead
                f = self._fact(fn, args, code, s, e, al)
                if f is None:
                    continue
                if (fn in self.adapters or fn in self.getters) and f.get("sys") == fn[3:]:
                    f["sys"] = self.system_for(fn, script)
                self._resolve_via_alias(f, al)
                tpl = self._param_template(f, al)
                if tpl is not None:
                    # the name comes from the conversation node (EE script parameters): expanded per node below
                    tpl["line"] = line_of(code, s)
                    tpl["guards"] = self.guards_at(code, s)
                    tpl["_al"] = al
                    self.param_templates[script].append(tpl)
                    continue
                if f.get("op") in ("set", "get") and f.get("key") is None:
                    self.not_followed += 1
                    # campaign key_expr is "campaign, name": only the name part becomes the pattern
                    kx = f["key_expr"].split(",", 1)[1] if f["sys"] == "campaign" and "," in f["key_expr"] else f["key_expr"]
                    (self.unfollowed_sets if f["op"] == "set" else self.unfollowed_gets)[f["sys"]].append(
                        (self.name_pattern(kx, al), script))
                    continue
                f["line"] = line_of(code, s)
                out.append((script, f))
        self._cur_script = None
        out += self.expand_param_templates()
        return out

    def _alias_literal(self, expr, al):
        """`sVar` when the script assigns it exactly one literal and never changes it (string sVar = "Q_X";) ->
        that literal. Counters (n++, n += 1, for loops), function parameters and reassigned variables don't count."""
        e = (expr or "").strip()
        if not (al and re.fullmatch(r"[A-Za-z_]\w*", e) and e in al and len(al[e]) == 1):
            return None
        code = self.code.get(self._cur_script or "", "")
        v = re.escape(e)
        # changed in place anywhere (n++, --n, n += 2...): not a fixed value
        if re.search(r"\b" + v + r"\s*(\+\+|--|[-+*/%|&^]=)", code) or re.search(r"(\+\+|--)\s*" + v + r"\b", code):
            return None
        # exactly one plain assignment "v =" (not "v ==") in the whole script
        if len(re.findall(r"\b" + v + r"\s*=(?!=)", code)) != 1:
            return None
        # `(..., string sVar,` or `(string sVar = ...)`: declared as a parameter in some function's header
        if re.search(r"\(([^()]*[\s,(])?(?:int|string|float|object)\s+" + v + r"\s*[,)=]", code):
            return None     # a function parameter somewhere in this file
        val = self.resolve(al[e][0])
        return val

    def _resolve_via_alias(self, f, al):
        """Fill in a fact's unresolved name (and set/compared value) from a single-assignment variable, in place.
        Campaign facts are left alone (their key combines two arguments)."""
        if f.get("op") not in ("set", "get") or f["sys"] == "campaign":
            return
        if f.get("key") is None:
            k = self._alias_literal(f.get("key_expr"), al)
            if k is not None:
                f["key"] = k
        if f.get("op") == "set" and f.get("value") == "*" and f.get("value_expr"):
            v = self._alias_literal(f["value_expr"], al)
            if v is not None:
                f["value"] = v
        elif f.get("op") == "get" and f.get("value") is None and f.get("value_expr"):
            f["value"] = self._alias_literal(f["value_expr"], al)

    # -- EE conversation script parameters (GetScriptParam): one generic script, configured per dialogue node
    @staticmethod
    def param_of(expr, al, depth=0):
        """Name of the script parameter an expression comes from: GetScriptParam("VAR_NAME"), or a variable assigned
        from it (string sVar = GetScriptParam("VAR_NAME"); int n = StringToInt(GetScriptParam("VALUE"))). None if not."""
        e = (expr or "").strip()
        m = SCRIPT_PARAM.match(e)
        if m:
            return m.group(1)
        if depth < 3 and al and re.fullmatch(r"[A-Za-z_]\w*", e) and e in al:
            found = {QuestInference.param_of(x, al, depth + 1) for x in al[e]} - {None}
            if len(found) == 1:
                return found.pop()
        return None

    def _param_template(self, f, al):
        """A fact whose variable name (or item tag) is a script parameter -> template, else None.

        The template is the fact plus the parameter names to fill in per node: key_param (variable name),
        camp_param (campaign name), value_param, self_param (see _self_switch), tag_param, resref_param or
        amount_param. Example: SetLocalInt(oPC, GetScriptParam("VAR_NAME"), n) -> key_param "VAR_NAME"."""
        if f.get("op") in ("set", "get") and f.get("key") is None:
            if f["sys"] == "campaign":
                parts = [x.strip() for x in f["key_expr"].split(",", 1)]
                if len(parts) != 2:
                    return None
                camp = self.resolve(parts[0])
                cp = None if camp is not None else self.param_of(parts[0], al)
                kp = self.param_of(parts[1], al)
                if kp is None or (camp is None and cp is None):
                    return None
                t = dict(f, key_param=kp, camp_param=cp, camp=camp)
            else:
                kp = self.param_of(f["key_expr"], al)
                if kp is None:
                    return None
                t = dict(f, key_param=kp)
            t["value_param"] = self.param_of(f.get("value_expr", ""), al)
            t["self_param"] = self._self_switch(f.get("obj_expr", ""), al)
            return t
        if f.get("op") in ("item_check", "item_take") and f.get("tag") is None:
            tp = self.param_of(f.get("expr", ""), al)
            if tp:
                return dict(f, tag_param=tp)
        if f.get("op") == "item_give" and f.get("resref") is None:
            tp = self.param_of(f.get("expr", ""), al)
            if tp:
                return dict(f, resref_param=tp)
        if f.get("op") == "reward" and f.get("amount") is None:
            tp = self.param_of(f.get("expr", ""), al)
            if tp:
                return dict(f, amount_param=tp)
        return None

    # -- which branch a statement sits in: [(condition text, True if inside the if-body / False if in an else)]
    # Used so that a generic parameter script's facts are kept only on the nodes whose parameter values lead to
    # them: `if (sMode == "SET") SetLocalInt(...)` applies only at nodes with MODE=SET.
    @staticmethod
    def _skip_ws(code, i):
        while i < len(code) and code[i] in " \t\r\n":
            i += 1
        return i

    @classmethod
    def _paren_end(cls, code, i):
        """i at '(' -> index after the matching ')'. Brackets inside string literals are skipped."""
        depth = 0
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            elif code[i] == '"':
                j = i + 1
                while j < len(code) and code[j] != '"':
                    j += 2 if code[j] == "\\" else 1
                i = j
            i += 1
        return len(code)

    @classmethod
    def _stmt_end(cls, code, i):
        """i at the start of a statement/body -> index after it ({ block }, if/else chain, or up to ';')."""
        i = cls._skip_ws(code, i)
        if i < len(code) and code[i] == "{":
            depth = 0
            while i < len(code):
                depth += {"{": 1, "}": -1}.get(code[i], 0)
                if code[i] == '"':
                    j = i + 1
                    while j < len(code) and code[j] != '"':
                        j += 2 if code[j] == "\\" else 1
                    i = j
                i += 1
                if depth == 0:
                    return i
            return len(code)
        m = re.match(r"(if|while|for|switch)\s*\(", code[i:])
        if m:
            j = cls._paren_end(code, i + m.end() - 1)
            j = cls._stmt_end(code, j)
            if m.group(1) == "if":
                k = cls._skip_ws(code, j)
                if code.startswith("else", k) and not re.match(r"\w", code[k + 4:k + 5]):
                    j = cls._stmt_end(code, k + 4)
            return j
        while i < len(code) and code[i] != ";":
            if code[i] == '"':
                j = i + 1
                while j < len(code) and code[j] != '"':
                    j += 2 if code[j] == "\\" else 1
                i = j
            i += 1
        return i + 1

    def guards_at(self, code, pos):
        """The if/else-if/else conditions that must hold for the statement at `pos` to run.

        Returns [(condition text, wanted truth)]: the branch containing pos gives (its condition, True); earlier
        branches of the same if/else-if chain give (condition, False). Only `if` statements inside the enclosing
        function are looked at (loops, switch cases and ?: are not). Example: in
        `if (a) {...} else if (b) { X; }` the guards of X are [("a", False), ("b", True)]."""
        # start from the function that contains pos (the last function header before it)
        heads = list(re.finditer(r"^[ \t]*(?:void|int|string|float|object)\s+[A-Za-z_]\w*\s*\([^)]*\)\s*\{", code[:pos], re.M))
        start = heads[-1].end() if heads else 0
        out, seen_chain = [], set()
        for m in re.finditer(r"\bif\s*\(", code[start:pos]):
            i = start + m.start()
            if i in seen_chain:
                continue
            back = code[max(0, i - 12):i].rstrip()
            if back.endswith("else"):
                continue                     # handled as part of its chain
            prev = []
            while True:
                seen_chain.add(i)
                c0 = code.index("(", i)
                c1 = self._paren_end(code, c0)
                cond = code[c0 + 1:c1 - 1]
                body_end = self._stmt_end(code, c1)
                if c1 <= pos < body_end:
                    out += [(p_, False) for p_ in prev] + [(cond, True)]
                    break
                prev.append(cond)
                k = self._skip_ws(code, body_end)
                if not (code.startswith("else", k) and not re.match(r"\w", code[k + 4:k + 5])):
                    break
                k2 = self._skip_ws(code, k + 4)
                if re.match(r"if\s*\(", code[k2:]):
                    i = k2
                    continue
                else_end = self._stmt_end(code, k2)
                if k2 <= pos < else_end:
                    out += [(p_, False) for p_ in prev]
                break
        return out

    def _param_info(self, expr, al):
        """(parameter name, as int?) for an expression that is (a variable holding) GetScriptParam(...)."""
        e = (expr or "").strip()
        p_ = self.param_of(e, al)
        if p_ is None:
            return None, False
        src = e
        if al and re.fullmatch(r"[A-Za-z_]\w*", e) and e in al:
            src = al[e][0]
        return p_, bool(re.match(r"\s*StringTo(Int|Float)", src))

    def eval_cond(self, cond, params, al):
        """True / False / None (can't tell) for a guard, given the node's script parameters.

        cond: condition text from guards_at(); params: {parameter: value} of one conversation node; al: aliases.
        Understands ||, &&, !, brackets, a comparison between a parameter (or a variable holding one) and a
        literal or constant, and a bare parameter used as a truth value. Anything else is None, and None never
        removes a fact (see _guards_pass)."""
        c = cond.strip()
        while c.startswith("(") and self._paren_end(c, 0) == len(c):
            c = c[1:-1].strip()
        parts = self._split_top(c, "||")
        if len(parts) > 1:
            vals = [self.eval_cond(x, params, al) for x in parts]
            return True if True in vals else (False if all(v is False for v in vals) else None)
        parts = self._split_top(c, "&&")
        if len(parts) > 1:
            vals = [self.eval_cond(x, params, al) for x in parts]
            return False if False in vals else (True if all(v is True for v in vals) else None)
        neg = False
        while c.startswith("!") and not c.startswith("!="):
            neg, c = not neg, c[1:].strip()
        m = re.fullmatch(r"(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)", c)
        if m:
            a, op, b = m.group(1).strip(), m.group(2), m.group(3).strip()
            pa, ia = self._param_info(a, al)
            pb, ib = self._param_info(b, al)
            # `5 < nParam` -> `nParam > 5`: keep the parameter on the left
            if pa is None and pb is not None:
                a, b, pa, ia = b, a, pb, ib
                op = {">": "<", "<": ">", ">=": "<=", "<=": ">="}.get(op, op)
            if pa is None or self._param_info(b, al)[0] is not None:
                return None
            lit = self.resolve(b)
            if lit is None:
                return None
            raw = params.get(pa, "")         # a parameter the node doesn't set reads as ""
            if ia:
                # compared as numbers when the script converts it with StringToInt/StringToFloat
                try:
                    x, y = int(self._param_value(raw, "int")), int(lit)
                except ValueError:
                    return None
            else:
                x, y = raw, lit
            r = {"==": x == y, "!=": x != y, ">=": x >= y, "<=": x <= y, ">": x > y, "<": x < y}[op]
            return (not r) if neg else r
        pa, ia = self._param_info(c, al)
        if pa is None:
            return None
        raw = params.get(pa, "")
        r = int(self._param_value(raw, "int")) != 0 if ia else raw != ""
        return (not r) if neg else r

    @staticmethod
    def _split_top(c, op):
        """Split c on op ("||" or "&&") outside brackets and strings: "a && (b || c)" -> ["a", "(b || c)"]."""
        out, depth, cur, i, instr = [], 0, [], 0, False
        while i < len(c):
            ch = c[i]
            if ch == '"':
                instr = not instr
            if not instr:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif depth == 0 and c.startswith(op, i):
                    out.append("".join(cur)); cur = []; i += len(op); continue
            cur.append(ch); i += 1
        out.append("".join(cur))
        return [x.strip() for x in out]

    def _guards_pass(self, t, params):
        """False only when a guard can be decided and goes the other way; "can't tell" keeps the fact."""
        for cond, want in t.get("guards") or ():
            v = self.eval_cond(cond, params, t.get("_al"))
            if v is not None and v != want:
                return False
        return True

    def _self_switch(self, obj, al):
        """A parameter that redirects the object to the NPC: `int bSelf = StringToInt(GetScriptParam("SELF"));
        if (bSelf == 1) oPC = OBJECT_SELF;` -> "SELF" (that node's variable then belongs to the NPC, not the player)."""
        obj = (obj or "").strip()
        if not obj or not al or "OBJECT_SELF" not in [x.strip() for x in al.get(obj, [])]:
            return None
        code = self.code.get(self._cur_script or "", "")
        m = re.search(r"if\s*\(\s*([A-Za-z_]\w*)\s*(?:==\s*(?:1|TRUE)\s*)?\)\s*\{?\s*" + re.escape(obj) +
                      r"\s*=\s*OBJECT_SELF", code)
        return self.param_of(m.group(1), al) if m else None

    @staticmethod
    def _param_value(raw, vtype):
        """What StringToInt/StringToFloat make of a parameter value ("" -> 0).
        For int (and token / unknown type) only the leading integer is kept ("12abc" -> "12"); with no leading
        number an int is "0", while a token or unknown type keeps the text. Other types are returned as given."""
        raw = (raw or "").strip()
        if vtype in ("int", "token") or vtype is None:
            m = re.match(r"-?\d+", raw)
            return str(int(m.group(0))) if m else ("0" if vtype == "int" else raw)
        return raw

    def expand_param_templates(self):
        """One concrete fact per dialogue node that runs a parameter script with the parameter set.

        Returns [(script, fact)] for variable reads/writes; item and reward facts are stored per node in
        param_items / param_rewards / param_given_res instead (they are attached to quests in _quest)."""
        out = []
        for script, tpls in self.param_templates.items():
            for kind, by, via in self.triggers.get(script, []):
                if kind != "dlg_script":
                    continue
                params = self.node_params.get((by, via))
                if not params:
                    continue            # used without parameters here: the script's own `if (sVar != "")` skips it
                # the graph's via is '<link>/Active' for a "text appears when" condition, '<node>/Script' for an action
                role = "condition" if via.endswith("/Active") else "action"
                place = dict(kind="conversation", node=by, label=self.labels.get(by, by),
                             detail=f"{role} at {via.split('/')[0]} (script parameters)")
                for t in tpls:
                    f = {k: v for k, v in t.items() if k not in ("key_param", "camp_param", "camp", "value_param",
                                                                    "tag_param", "self_param", "guards", "_al",
                                                                    "resref_param", "amount_param")}
                    f["place"], f["param_node"] = place, (by, via)
                    if not self._guards_pass(t, params):
                        continue        # this node's parameters send the script down another branch
                    if t.get("self_param") and self._param_value(params.get(t["self_param"], ""), "int") == "1":
                        continue        # this node points it at the NPC (SELF=1): NPC state, not the player's
                    if "tag_param" in t:
                        tag = params.get(t["tag_param"])
                        if not tag:
                            continue
                        f["tag"] = tag
                        self.param_items[(by, via)].append(f)
                        continue
                    if "resref_param" in t:
                        res = (params.get(t["resref_param"]) or "").lower()
                        if not res:
                            continue
                        f["resref"] = res
                        self.param_items[(by, via)].append(f)
                        self.param_given_res.add(res)
                        continue
                    if "amount_param" in t:
                        amt = self._param_value(params.get(t["amount_param"]), "int")
                        if amt and amt != "0":
                            self.param_rewards[(by, via)].append(dict(kind=f["kind"], amount=amt,
                                                                      by=f"{place['label']} ({place['detail']})"))
                        continue
                    key = params.get(t["key_param"])
                    if not key:
                        continue
                    if t["sys"] == "campaign":
                        camp = t.get("camp") if t.get("camp") is not None else params.get(t.get("camp_param") or "")
                        if not camp:
                            continue
                        key = f"{camp}/{key}"
                    f["key"], f["key_expr"] = key, f'"{key}" (parameter {t["key_param"]})'
                    vt = f.get("vtype")
                    if t.get("value_param"):
                        f["value"] = self._param_value(params.get(t["value_param"]), vt)
                    out.append((script, f))
                    self.param_facts += 1
        return out

    @staticmethod
    def name_pattern(expr, al, depth=0):
        """What names a run-time name could produce: "Q_" + GetTag(o)  ->  ^Q_.*$ ; an unknown variable -> anything.

        Returns a regex string without ^/$ (callers use re.fullmatch): "Q_" + GetTag(o) -> "Q_.*". A variable is
        replaced by the patterns of up to 6 expressions assigned to it (3 levels deep). Used only to decide whether
        a missing write *might* exist (_doubt_writes), never to invent a variable."""
        e = expr.strip()
        if re.fullmatch(r"[A-Za-z_]\w*", e):
            if depth < 3 and al and e in al:
                return "|".join(f"(?:{QuestInference.name_pattern(x, al, depth + 1)})" for x in al[e][:6])
            return ".*"
        parts = []
        # the lookahead accepts a "+" only when an even number of quotes follow it (so it is not inside a string)
        for piece in re.split(r"\+(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", e):     # split on + outside strings
            piece = piece.strip()
            if re.fullmatch(STR_LIT, piece):
                parts.append(re.escape(piece[1:-1]))
            elif re.fullmatch(r"[A-Za-z_]\w*", piece) and depth < 3 and al and piece in al:
                parts.append("(?:" + QuestInference.name_pattern(piece, al, depth + 1) + ")")
            else:
                parts.append(".*")
        return "".join(parts) or ".*"

    @staticmethod
    def _pure_wildcard(pat):
        """A name built entirely at run time ('.*'): it could be anything, so it says nothing about one name.
        True when nothing but regex syntax is left after removing (?: ( ) . * ? + |, i.e. no literal part."""
        return not re.sub(r"\(\?:|[().*?+|]", "", pat or "")

    def _doubt_writes(self, sysname, key, reads=False, skip_wildcards=False):
        """Run-time-named writes (or reads) of this system that could produce this name.

        sysname: "local", "campaign" or an adapter system; key: the resolved name ("campaign/name" for campaign).
        Returns the list of scripts (one entry per matching call). skip_wildcards ignores names with no literal part.
        A pattern that is not a valid regex counts as a match: when unsure, doubt (a warning, not an error)."""
        k = key.split("/", 1)[1] if sysname == "campaign" and "/" in key else key
        hits = []
        for pat, script in (self.unfollowed_gets if reads else self.unfollowed_sets).get(sysname, ()):
            if skip_wildcards and self._pure_wildcard(pat):
                continue
            try:
                if re.fullmatch(pat, k, re.S):
                    hits.append(script)
            except re.error:
                hits.append(script)
        return hits

    def _key_is_parameter(self, code, pos, fn, args):
        """Is this call (to a function some wrapper wraps) inside a function whose parameter supplies the key?
        Then it is the wrapper's own body; its callers are followed instead (see facts())."""
        w = next((w for w in self.wrappers.values() if w[0] == fn), None)
        if not w:
            return False
        key_i = self._key_index(fn)
        if len(args) <= key_i:
            return False
        # is the key a parameter of the function this call sits in?
        heads = list(re.finditer(r"^[ \t]*(?:void|int|string|float|object)\s+[A-Za-z_]\w*\s*\(([^)]*)\)\s*\{",
                                 code[:pos], re.M))
        if not heads:
            return False
        params = [p_.strip().split()[-1].split("=")[0].strip() for p_ in heads[-1].group(1).split(",") if p_.strip()]
        return args[key_i].strip() in params

    def _fact(self, fn, args, code, s, e, al):
        """One read/write/item/reward fact for a call (None when it isn't one).

        fn/args: the call (from iter_calls); code, s, e: the script text and the call's start/end offsets (to read a
        comparison around it); al: aliases(). Unresolvable names give key None; an unresolvable written value
        gives value "*" (any value)."""
        f = None
        if fn in self.adapters:
            a = self.adapters[fn]
            if len(args) >= 2:
                key = self.resolve(args[1])
                val = self.resolve(args[2] if len(args) >= 3 else a["default"])
                f = dict(op="set", sys=fn[3:], key=key, key_expr=args[1], value=val if val is not None else "*",
                         value_expr=args[2] if len(args) >= 3 else "", scope=self.scope(args[0], al), obj_expr=args[0],
                         vtype=self._adapter_vtype(a))
        elif fn in self.getters:
            if len(args) >= 2:
                op, v = comparison_after(code, e, s)
                f = dict(op="get", sys=fn[3:], key=self.resolve(args[1]), key_expr=args[1], cmp=op,
                         value=self.resolve(v) if v else None, value_expr=v or "", scope=self.scope(args[0], al), obj_expr=args[0],
                         vtype=self._adapter_vtype(self.adapters[self.getters[fn]]))
        elif fn in ENGINE_LOCAL or fn in DELETE_LOCAL:
            if len(args) >= 2:
                key = self.resolve(args[1])
                vtype = ENGINE_LOCAL.get(fn) or DELETE_LOCAL[fn]
                # DeleteLocal* is a write of the unset value (0 / ""): a later check for 0 is then reachable
                val = INITIAL[vtype] if fn in DELETE_LOCAL else (self.resolve(args[2]) if len(args) >= 3 else None)
                f = dict(op="set", sys="local", key=key, key_expr=args[1], value=val if val is not None else "*",
                         value_expr=args[2] if len(args) >= 3 and fn not in DELETE_LOCAL else "",
                         scope=self.scope(args[0], al), obj_expr=args[0], vtype=vtype)
        elif fn in ENGINE_LOCAL_GET:
            if len(args) >= 2:
                op, v = comparison_after(code, e, s)
                f = dict(op="get", sys="local", key=self.resolve(args[1]), key_expr=args[1], cmp=op,
                         value=self.resolve(v) if v else None, value_expr=v or "", scope=self.scope(args[0], al), obj_expr=args[0],
                         vtype=ENGINE_LOCAL_GET[fn])
        elif fn in CAMPAIGN_SET or fn == "DeleteCampaignVariable":
            if len(args) >= 2:
                camp, key = self.resolve(args[0]), self.resolve(args[1])
                val = "(deleted)" if fn.startswith("Delete") else (self.resolve(args[2]) if len(args) >= 3 else None)
                # Set/DeleteCampaign* take an optional player as their last argument (4th for Set, 3rd for Delete):
                # with it the value is stored per player, without it once for the whole module.
                # DeleteCampaignVariable gets vtype None: run() applies it to every type stored under that name.
                f = dict(op="set", sys="campaign", key=f"{camp}/{key}" if camp is not None and key is not None else None,
                         key_expr=f"{args[0]}, {args[1]}", value=val if val is not None else "*",
                         value_expr=args[2] if len(args) >= 3 and not fn.startswith("Delete") else "",
                         scope="pc" if len(args) >= (3 if fn.startswith("Delete") else 4) else "module",
                         vtype=CAMPAIGN_SET.get(fn))
        elif fn in CAMPAIGN_GET:
            if len(args) >= 2:
                camp, key = self.resolve(args[0]), self.resolve(args[1])
                op, v = comparison_after(code, e, s)
                # GetCampaign*(campaign, name, player): the optional 3rd argument makes it a per-player read
                f = dict(op="get", sys="campaign", key=f"{camp}/{key}" if camp is not None and key is not None else None,
                         key_expr=f"{args[0]}, {args[1]}", cmp=op, value=self.resolve(v) if v else None, value_expr=v or "",
                         scope="pc" if len(args) >= 3 else "module", vtype=CAMPAIGN_GET[fn])
        elif fn == "AddJournalQuestEntry":
            # AddJournalQuestEntry(quest tag, entry id, player...): the journal entry id acts as the stage
            if len(args) >= 2:
                f = dict(op="set", sys="journal", key=self.resolve(args[0]), key_expr=args[0],
                         value=self.resolve(args[1]) or "*", scope="pc")
        elif fn in ITEM_CHECK:
            k = ITEM_CHECK[fn]
            if len(args) > k:
                tag = self.resolve(args[k])
                # "takes" rather than "needs" when the item is destroyed: either the call sits inside a Take*/Destroy*
                # call (within 80 characters before it), or - a broad heuristic - the script has a main() that calls
                # DestroyObject anywhere
                destroyed = fn == "GetItemPossessedBy" and (("DestroyObject" in code and
                    bool(re.search(r"\bvoid\s+main\s*\(", code))) or bool(
                    re.search(r"\b(?:Take|Destroy)\w*\s*\([^;()]*$", code[max(0, s - 80):s])))
                f = dict(op="item_take" if destroyed else "item_check", tag=tag, expr=args[k])
        elif fn in ITEM_GIVE:
            k = ITEM_GIVE[fn]
            if len(args) > k:
                f = dict(op="item_give", resref=(self.resolve(args[k]) or "").lower() or None, expr=args[k])
        elif fn in GOLD:
            k = GOLD[fn]
            if len(args) > k:
                f = dict(op="reward", kind="gold" if fn != "TakeGoldFromCreature" else "gold taken",
                         amount=self.resolve(args[k]), expr=args[k])
        elif fn in XP:
            k = XP[fn]
            if len(args) > k:
                f = dict(op="reward", kind="xp", amount=self.resolve(args[k]), expr=args[k])
        return f

    @staticmethod
    def _adapter_vtype(a):
        """Token helpers (int slot, string value) behave as tokens; others by their value type."""
        return "token" if a["key_type"] == "int" and a["value_type"] == "string" else a["value_type"]

    @staticmethod
    def _inside_definition(code, pos, fn):
        """Is this call part of the helper's own definition/prototype (not a real use)?
        True when a return type word directly precedes the name: `void SetQuestToken(`."""
        before = code[max(0, pos - 40):pos]
        return bool(re.search(r"\b(void|int|string|float|object)\s+$", before))

    # -- where does a script run?
    def where(self, script, depth=0):
        """Places that run `script`: [dict(kind, node, label, detail)] - conversation nodes, object/module events,
        and (two levels deep) the places of scripts that ExecuteScript it; an include gives one "library" entry."""
        out = []
        for kind, by, via in self.triggers.get(script, []):
            lab = self.labels.get(by, by)
            if kind == "dlg_script":
                role = "condition" if via.endswith("/Active") else "action"
                out.append(dict(kind="conversation", node=by, label=lab, detail=f"{role} at {via.split('/')[0]}"))
            elif kind in ("event_script", "module_event", "tag_based_script"):
                out.append(dict(kind="event", node=by, label=lab, detail=via))
            elif kind == "execute_script" and depth < 2 and by.startswith("script:"):
                for w in self.where(by[7:], depth + 1):
                    out.append(dict(w, detail=f"via {by[7:]}: {w['detail']}"))
            elif kind == "include" and depth < 1 and by.startswith("script:"):
                out.append(dict(kind="library", node=by, label=lab, detail="library function"))
        return out

    # -- build quests
    def run(self):
        """Run every pass and return dict(summary, quests, hubs).

        quests: one dict per piece of state (see _quest), sorted broken, warning, ok, legacy, then by label.
        hubs:   quests grouped by the conversation that drives them most.
        summary: counts by kind and health, the adapters found, how many names could not be followed, the token
                 string length. Read-only: works only on the inputs given to __init__."""
        self.scan_definitions()
        facts = self.facts()
        state = defaultdict(lambda: dict(sets=[], gets=[]))
        per_script = defaultdict(list)
        keys_per_script = defaultdict(set)
        for script, f in facts:
            if f.get("param_node"):
                continue        # per-node facts of a generic script: attributed by node, not by script
            per_script[script].append(f)
            if f["op"] in ("set", "get"):
                keys_per_script[script].add((f["sys"], f["key"]))
        for script, tpls in self.param_templates.items():
            for t in tpls:
                if t.get("key_param"):
                    keys_per_script[script].add(("param", t["key_param"]))
        self.keys_per_script = keys_per_script
        # group the facts into pieces of state: (system, scope for locals, name, type) -> sets and gets
        deletes = []
        for script, f in facts:
            if f["op"] in ("set", "get"):
                if f["sys"] == "local" and f["scope"] not in ("pc", "module"):
                    if f["op"] == "set":        # NPC/object working state is not quest state - but it may be the
                        self.other_sets[(f["sys"], f["key"], f.get("vtype"))].append((script, f))   # player after all
                    continue
                if f["sys"] == "campaign" and f.get("vtype") is None:
                    deletes.append((script, f))  # DeleteCampaignVariable: applies to whichever type the name has
                    continue
                # NWN keeps int, string and float variables of the same name apart: so do we
                vt = f.get("vtype") if f["sys"] in ("local", "campaign") else ""
                k = (f["sys"], f["scope"] if f["sys"] == "local" else "", f["key"], vt or "")
                state[k]["sets" if f["op"] == "set" else "gets"].append((script, f))
        for script, f in deletes:
            # a delete is a write of the unset value to every type stored under that name (an int if none is known)
            typed = [k for k in state if k[0] == "campaign" and k[2] == f["key"]] or [("campaign", "", f["key"], "int")]
            for k in typed:
                state[k]["sets"].append((script, dict(f, value=INITIAL.get(k[3], "0"), vtype=k[3])))
        slot_names = defaultdict(set)    # (sys, slot) -> const names
        quests = []
        for (sysname, scope, key, vt), st in state.items():
            q = self._quest(sysname, scope, key, st, per_script, vt)
            if q:
                quests.append(q)
                if q["kind"] == "token":
                    for expr in q["key_exprs"]:
                        if expr in self.consts:
                            slot_names[(sysname, key)].add(expr)
        # token slots shared by two differently named quests
        # An error, not a warning: both constants resolve to the same character of the same string, so each quest's
        # writes overwrite the other's progress.
        for q in quests:
            if q["kind"] == "token":
                names = slot_names.get((q["system"], q["key"]), set())
                if len(names) > 1:
                    q["problems"].append(dict(level="error", text=f"slot {q['key']} is used under {len(names)} names "
                                              f"({', '.join(sorted(names))}) - these quests overwrite each other's progress"))
        for q in quests:
            q["health"] = self._health(q)
        quests.sort(key=lambda q: ({"broken": 0, "warning": 1, "ok": 2, "legacy": 3}[q["health"]], q["label"].lower()))
        hubs = self._hubs(quests)
        summary = dict(quests=len(quests), by_kind=dict(_count(q["kind"] for q in quests)),
                       by_health=dict(_count(q["health"] for q in quests)), adapters=[
                           dict(setter=s, getter=a["getter"], key=a["key_type"], value=a["value_type"], defined_in=a["defined_in"])
                           for s, a in sorted(self.adapters.items())],
                       not_followed=self.not_followed, from_script_parameters=self.param_facts,
                       parameter_scripts=sorted(self.param_templates), token_length=self.string_len,
                       token_length_note=self.string_len_note, hubs=len(hubs))
        return dict(summary=summary, quests=quests, hubs=hubs)

    def _label(self, sysname, key, exprs):
        """(display label, raw constant comment) for a quest: the constant's comment plus its name when there is a
        named constant, else the key itself. exprs should list the most-used name first."""
        names = [e for e in exprs if e in self.consts]
        comment = next((self.consts[e][2] for e in names if self.consts[e][2]), "")
        # "061019 jdoe - hobgoblin quest for the marshal" -> "hobgoblin quest for the marshal" (date/author kept in comment)
        comment_clean = re.sub(r"^\s*\d{4,8}\s+[\w.]+\s*-\s*", "", comment).strip()
        if names:
            base = names[0]
            return (f"{comment_clean} ({base})" if comment_clean else base), comment
        return key, ""

    def _quest(self, sysname, scope, key, st, per_script, vt=""):
        """Build one quest dict from all the sets and gets of one piece of state.

        sysname/scope/key/vt: the state's identity (see run()); st: dict(sets=[(script, fact)], gets=[...]);
        per_script: {script: [facts]} used to attach items and rewards.
        kind: token (int slot in a string), named (helper keyed by a string), local, campaign or journal.
        problems: [dict(level=error|warning|info, text, fix?)] - _health() turns them into the quest's health.
        Returns the quest dict (never None today)."""
        kind = ("token" if sysname not in ("local", "campaign", "journal") else sysname)
        a = self.adapters.get("Set" + sysname.split("@")[0]) if kind == "token" else None
        # "QuestToken@tut_tokens_inc" = a second token string defined by another include: use that include's length
        slen = self.string_len_by_def.get(sysname.split("@")[1].lower()) if "@" in sysname else self.string_len
        if kind == "token" and a and a["key_type"] == "string":
            kind = "named"      # e.g. SetPersistentInt(oPC, "QUEST", 2)
        exprs = sorted({f["key_expr"] for _, f in st["sets"] + st["gets"]})
        use = defaultdict(int)
        for _, f in st["sets"] + st["gets"]:
            use[f["key_expr"]] += 1
        label, comment = self._label(sysname, key, sorted(exprs, key=lambda e: (-use[e], e)))   # most-used name first
        if kind == "token":
            vtype = "token"
        elif kind == "journal":
            vtype = "journal"
        elif kind == "named":
            vtype = a["value_type"] if a else "int"
        else:
            vtype = vt or "int"
        initial = INITIAL.get(vtype, "0")
        set_values = sorted({f["value"] for _, f in st["sets"]}, key=_sortkey)
        stages = defaultdict(lambda: dict(set_by=[], checked_by=[]))
        for script, f in st["sets"]:
            stages[f["value"]]["set_by"].append(self._ref(script, f))
        for script, f in st["gets"]:
            stages[f.get("value") if f.get("cmp") in ("==", "!=", ">=", "<=", ">", "<") else "(any)"]["checked_by"].append(
                dict(self._ref(script, f), test=(f"{f['cmp']} {f['value']}" if f.get("cmp") and f.get("value") is not None
                                                  else ("is not set" if f.get("cmp") == "!" else "used as a value"))))
        scripts = sorted({s for s, _ in st["sets"] + st["gets"]})
        problems = []
        # completability of every check
        known = set(set_values) | {initial}
        if vtype == "token":
            known |= {"0", ""}      # an unset slot reads as "0" or "" depending on how the helper pads the string
        dynamic = "*" in set_values
        # reasons the toolkit can't be sure: then a "can never be true" is a warning to check, not an error
        doubt = []
        dyn = self._doubt_writes(sysname, key)
        if dyn:
            doubt.append(f"{len(dyn)} write(s) with a name built at run time could set it ("
                         + ", ".join(sorted(set(dyn))[:4]) + ")")
        other = self.other_sets.get((sysname, key, vt or None)) or self.other_sets.get((sysname, key, vtype))
        if other:
            doubt.append("it is also set on an object the toolkit couldn't identify as the player (" +
                         ", ".join(sorted({s_ for s_, _ in other})[:4]) + ") - that may be the player")
        # The "can never be true" rule. Only exact tests (== / !=) against a resolved value are judged, and only
        # when every write's value is known (one "*" write could set anything). `== v` needs v among the values
        # ever set (or the starting value); `!= v` needs at least one other possible value.
        # Severity: error when the check is in a live script and nothing could explain it; warning when there is
        # doubt (a run-time-named write, or a write on an object not identified as the player); info in dead code.
        for script, f in st["gets"]:
            cmpop, v = f.get("cmp"), f.get("value")
            if dynamic or cmpop not in ("==", "!=") or v is None:
                continue
            ok = (v in known) if cmpop == "==" else bool(known - {v})
            if not ok:
                what = "nothing ever sets it" if not set_values else f"it is only ever set to {', '.join(set_values)}"
                text = f"{script} (line {f['line']}) checks {cmpop} {v}, which can never be true: {what}"
                if not self._is_live(script, f):
                    # dead code can't stop a player finishing the quest - worth tidying, not a broken quest
                    problems.append(dict(level="info", text=text + f" - but {script} is not used by the module (dead code)",
                                         script=script))
                    continue
                if doubt:
                    text += " - but check it: " + "; ".join(doubt)
                fix = (f"Either set it to {v} at the step that should lead here, or change the check in {script} line "
                       f"{f['line']} to a value that is set ({', '.join(sorted(known - {''})) or 'none yet'}).")
                problems.append(dict(level="warning" if doubt else "error", text=text, script=script, fix=fix))
        # read but never written: a warning, not an error - the server, the database or another module may set it
        # (when a == / != test exists, the rule above already reported it more precisely)
        if st["gets"] and not st["sets"] and not any(f.get("cmp") in ("==", "!=") for _, f in st["gets"]):
            problems.append(dict(level="warning", text="read but never set anywhere in the module (only the starting value "
                                                       "is ever seen, unless something outside the module sets it)" +
                                                       (" - " + "; ".join(doubt) if doubt else ""),
                                 fix="Set it where the quest step happens (Script generator: set a variable/token), or "
                                     "remove the check if the quest was retired."))
        for e in exprs:
            if e in self.const_conflicts:
                problems.append(dict(level="warning", text=f"{e} has different values in different scripts "
                                     f"({', '.join(self.const_conflicts[e])}); the value from the most-included script "
                                     f"({self.consts[e][1]}, in {self.consts[e][3]}) was used"))
        if st["sets"] and not st["gets"]:
            problems.append(dict(level="info", text="set but never read in the module (bookkeeping, or read by the "
                                                    "server/database outside the module)"))
        if kind == "token":
            # past the end is an error (the helper cannot store that character); exactly at the length is a warning,
            # because whether slots count from 0 or 1 depends on how the module's helper indexes the string
            if key.isdigit() and slen and int(key) > slen:
                problems.append(dict(level="error", text=f"slot {key} is past the end of the token string "
                                                         f"({slen} characters) - it can never be stored",
                                     fix="Give the constant a free slot below the string length (Variables & tags > "
                                         "Token slots lists the free ones), or lengthen the string in the include."))
            elif key.isdigit() and slen and int(key) == slen:
                problems.append(dict(level="warning", text=f"slot {key} is the token string's length ({slen}): "
                                     "past the end if slots count from 0 - check how the helper reads it"))
            lits = [e for e in exprs if re.fullmatch(r"\d+", e.strip())]
            if lits and len(exprs) > len(lits):
                problems.append(dict(level="warning", text=f"slot {key} is sometimes written as a bare number "
                                                           f"({', '.join(lits)}) instead of its name - easy to mistype"))
        # items, rewards, where it runs
        items, rewards = {}, []
        plain_scripts = {s_ for s_, f_ in st["sets"] + st["gets"] if not f_.get("param_node")}
        for script in sorted(plain_scripts):
            # items/rewards are only attributed from focused scripts; an OnClientEnter that touches 40 variables and
            # checks a widget item says nothing about which of those 40 quests needs the widget
            if len(self.keys_per_script.get(script, ())) > 3:
                continue
            for f in per_script.get(script, []):
                if f["op"] in ("item_check", "item_take") and f.get("tag"):
                    items.setdefault(("tag", f["tag"]), self._item_status("tag", f["tag"], "needs" if f["op"] == "item_check" else "takes", script))
                elif f["op"] == "item_give" and f.get("resref"):
                    items.setdefault(("res", f["resref"]), self._item_status("res", f["resref"], "gives", script))
                elif f["op"] == "reward" and any(s_ == script for s_, _ in st["sets"]):
                    rewards.append(dict(kind=f["kind"], amount=f["amount"] or "(computed)", by=script))
        # items handed over at the conversation nodes that set this variable through script parameters (Q_ITEM)
        for node in sorted({f["param_node"] for _s, f in st["sets"] + st["gets"] if f.get("param_node")}):
            where_ = self.labels.get(node[0], node[0])
            for f in self.param_items.get(node, []):
                if f["op"] == "item_give":
                    items.setdefault(("res", f["resref"]), self._item_status("res", f["resref"], "gives", where_))
                else:
                    role = "needs" if f["op"] == "item_check" else "takes"
                    items.setdefault(("tag", f["tag"]), self._item_status("tag", f["tag"], role, where_))
            if any(f_["op"] == "set" and f_.get("param_node") == node for _s, f_ in st["sets"]):
                rewards += self.param_rewards.get(node, [])
        for it in items.values():
            if it["status"] == "missing" and it["role"] in ("needs", "takes"):
                # a tag we can't find may be a base-game item, or a tag a script sets at run time: check, don't condemn
                problems.append(dict(level="warning", text=f"needs item '{it['name']}': {it['detail']}"))
            elif it["status"] == "unobtainable" and it["role"] in ("needs", "takes"):
                # a check for an item is not always a requirement (else-branches, "must NOT have it", clean-up
                # removal, one of several alternatives), and DMs can hand out palette items: check, don't condemn
                problems.append(dict(level="warning", text=f"checks for item '{it['name']}': {it['detail']} - if the quest "
                                     "really needs it, players can only get it from a DM or a way the toolkit can't "
                                     "follow (check the script: the test may be optional or negative)"))
            elif it["status"] == "missing" and it["role"] == "gives":
                # giving a blueprint that exists nowhere (module, haks, or the game when it was read) creates nothing:
                # the reward silently fails, so this is an error once the game's own items were checked
                if self.base_names:
                    problems.append(dict(level="error", text=f"gives item '{it['name']}': {it['detail']}"))
                else:           # the game's own items weren't loaded, so nw_* items can't be checked
                    problems.append(dict(level="warning", text=f"gives item '{it['name']}': {it['detail']} (the game's own "
                                         "items weren't loaded - set the game folder in Settings and re-analyse)"))
        places = []
        own = [(s_, f) for s_, f in st["sets"] + st["gets"]]
        plain = {s_ for s_, f in own if not f.get("place")}
        for s_ in scripts:
            if s_ in plain:     # a script configured by parameters only counts at the nodes that name this variable
                places += [dict(w, script=s_) for w in self.where(s_)]
        seen_p = set()
        for s_, f in own:
            if f.get("place") and (s_, f["param_node"]) not in seen_p:
                seen_p.add((s_, f["param_node"]))
                places.append(dict(f["place"], script=s_))
        convs = sorted({p["node"] for p in places if p["kind"] == "conversation"})
        givers = sorted({o for c in convs for o in self.dlg_owners.get(c, [])})
        # legacy: nothing the module runs touches it, so its problems cannot affect players (health "legacy")
        legacy = bool(scripts) and not any(self._is_live(s_, f_) for s_, f_ in st["sets"] + st["gets"])
        if legacy:
            problems.append(dict(level="info", text="every script that touches this is unused by the module (legacy quest?)"))
        return dict(id=f"{sysname}:{scope}:{key}" + (f":{vt}" if vt and vt != "int" else ""), kind=kind, vtype=vtype, system=sysname, scope=scope or ("pc" if kind != "campaign" else ""),
                    key=key, key_exprs=exprs, label=label, comment=comment, initial=initial,
                    stages=[dict(value=v, **stages[v]) for v in sorted(stages, key=_sortkey)],
                    values_set=set_values, scripts=scripts, conversations=convs,
                    conversation_labels=[self.labels.get(c, c) for c in convs], givers=givers,
                    places=places[:60], items=sorted(items.values(), key=lambda x: (x["role"], x["name"])),
                    rewards=rewards[:30], problems=problems, legacy=legacy,
                    adapter=(a or {}).get("defined_in"))

    def _ref(self, script, f):
        """Reference shown on a stage: script, line, up to 4 places that run it, and whether it is unused."""
        w = [f["place"]] if f.get("place") else self.where(script)
        return dict(script=script, line=f.get("line"), where=[f"{x['label']} ({x['detail']})" for x in w[:4]],
                    unused=not self._is_live(script, f))

    def _is_live(self, script, f):
        """A generic script is live if any conversation uses it; a node's own fact is live if its conversation is."""
        if f"script:{script}" not in self.live:
            return False
        if f.get("place") and self._live_has_dlg:
            return f["place"]["node"] in self.live
        return True

    def _item_status(self, by, name, role, script):
        """Can players get this item? by: "tag" (checked/taken items) or "res" (given items, by blueprint resref).
        role: needs / takes / gives. Returns dict(name, role, status=ok|missing|unobtainable, by, detail).
        "unobtainable" = a blueprint exists but is never placed, carried, sold or created by a script."""
        if by == "tag":
            cands = self.items_by_tag.get(name, [])
            inst = self.instance_tags.get(name)
            given = [c["resref"] for c in cands if c["resref"] in self.param_given_res]
            if given and not any(c["placed"] or c["carried_by"] or c["created_by"] for c in cands):
                return dict(name=name, role=role, status="ok", by=script,
                            detail=f"{given[0]} is handed out by a conversation (REWARD_ITEM parameter)")
            if inst and not any(c["placed"] or c["carried_by"] or c["created_by"] for c in cands):
                return dict(name=name, role=role, status="ok", by=script,
                            detail="a placed copy carries this tag: " + ", ".join(inst[:3]))
            if not cands:
                return dict(name=name, role=role, status="missing", by=script,
                            detail="no item blueprint in the module or its loaded haks has this tag (a base-game item, "
                                   "or a tag a script sets at run time?)")
            if not any(c["placed"] or c["carried_by"] or c["created_by"] for c in cands):
                return dict(name=name, role=role, status="unobtainable", by=script,
                            detail="its blueprint (" + ", ".join(c["resref"] for c in cands[:3]) + ") is never placed, "
                                   "carried, sold or created by a script, so players can't get it")
            return dict(name=name, role=role, status="ok", by=script, detail=", ".join(c["resref"] for c in cands[:3]))
        it = self.items_by_res.get(name)
        if it is None and f"{name}.uti" not in self.base_names:
            return dict(name=name, role=role, status="missing", by=script, detail="no item blueprint with this resref")
        return dict(name=name, role=role, status="ok", by=script, detail="module blueprint" if it else "base-game item")

    @staticmethod
    def _health(q):
        """legacy (all scripts unused) beats everything; then broken (any error), warning (any warning), ok.
        Info-level problems never change the health."""
        lv = {p["level"] for p in q["problems"]}
        if q["legacy"]:
            return "legacy"
        if "error" in lv:
            return "broken"
        if "warning" in lv:
            return "warning"
        return "ok"

    def _hubs(self, quests):
        """Group quests by the conversation that drives them most (the quest giver).
        Also sets q["hub"] on each quest ("(no conversation)" when none). Returns hubs, biggest first."""
        hubs = defaultdict(lambda: dict(quests=[], label="", givers=set()))
        for q in quests:
            counts = defaultdict(int)
            for p in q["places"]:
                if p["kind"] == "conversation":
                    counts[p["node"]] += 1
            hub = max(counts, key=counts.get) if counts else "(no conversation)"
            q["hub"] = hub
            h = hubs[hub]
            h["label"] = self.labels.get(hub, hub)
            h["quests"].append(q["id"])
            h["givers"] |= set(self.dlg_owners.get(hub, []))
        return sorted([dict(node=k, label=v["label"], givers=sorted(v["givers"]), quests=v["quests"])
                       for k, v in hubs.items()], key=lambda h: -len(h["quests"]))


def _count(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return d


def _sortkey(v):
    """Sort stage values numerically when they are integers ("2" before "10"), text after."""
    v = str(v)
    return (0, int(v)) if re.fullmatch(r"-?\d+", v) else (1, v)


def winning_sources(db):
    """{script: source} using the copy the game runs: the first-listed hak, then the module, then the user override
    (nwnlib.source_rank).

    db: the analysis index (sqlite3.Connection), read-only. Scripts without source (.ncs only) are left out."""
    best = {}
    for nm, src, kind, prio in db.execute("SELECT s.name, s.source, x.kind, x.priority FROM scripts s "
                                          "JOIN files f ON f.id=s.file_id JOIN sources x ON x.id=f.source_id"):
        if not src:
            continue
        rank = n.source_rank(kind, prio)            # lowest wins
        if nm not in best or rank < best[nm][0]:
            best[nm] = (rank, src)
    return {k: v[1] for k, v in best.items()}


def triggers_from_graph(g):
    """{script: [(kind, by node, via)]}: what runs each script (conversation nodes, object events, other scripts).

    g: the analysis dependency graph; g.rev is {destination node: [(source node, edge kind, via)]}.
    Includes are kept too (kind "include"), so where() can say "library function"."""
    triggers = defaultdict(list)
    for dst, lst in g.rev.items():
        if not dst.startswith("script:"):
            continue
        for src, kind, via in lst:
            if kind in ("dlg_script", "event_script", "module_event", "execute_script", "tag_based_script", "include"):
                triggers[dst[7:]].append((kind, src, via))
    return triggers


def infer_from_index(db, g, items, live, base_names=()):
    """Glue for nwn_analysis: build the inputs from the index and the analysis' item list.

    db: the analysis index (sqlite3.Connection), read-only; g: the dependency graph (nodes, rev, label());
    items: the analysis' item list (dicts with resref, tag, placed, carried_by, created_by); live: set of node ids
    the module reaches; base_names: the game's own resource names. Returns QuestInference.run()'s dict.
    Writes nothing. May raise on a damaged index - the caller (nwn_analysis) catches it."""
    scripts = winning_sources(db)
    triggers = triggers_from_graph(g)
    labels = {nid: g.label(nid) for nid in g.nodes}
    items_by_tag, items_by_res = defaultdict(list), {}
    for it in items:
        items_by_res[it["resref"]] = it
        if it.get("tag"):
            items_by_tag[it["tag"]].append(it)
    dlg_owners = defaultdict(list)
    for nid in g.nodes:
        if nid.startswith("dlg:"):
            # objects linked to this dialogue by a uses_conversation edge (at most 8 labels kept)
            dlg_owners[nid] = sorted({g.label(s) for s, k, v in g.rev.get(nid, []) if k == "uses_conversation"})[:8]
    return QuestInference(scripts, triggers, labels, live, items_by_tag, items_by_res, dlg_owners, base_names,
                          dialogue_params(db), placed_item_tags(db, labels)).run()


def placed_item_tags(db, labels=None):
    """{tag: [where]} for item copies sitting in creature, placeable and store inventories (areas and blueprints).
    The analysis' item list knows blueprints; a copy edited in the area (retagged) only shows up here.
    db: the analysis index, read-only. At most 6 places per tag. Never raises (an older index gives {})."""
    out = defaultdict(list)
    try:
        rows = db.execute("SELECT x.relpath, x.node, f.path, f.value FROM fields f JOIN files x ON x.id=f.file_id "
                          "WHERE f.label='Tag' AND x.ext IN ('git','utc','utp','utm') AND f.path LIKE '%ItemList[%]/Tag'"
                          ).fetchall()
    except Exception:  # noqa
        return {}
    for relpath, node, path, tag in rows:
        if tag and len(out[tag]) < 6:
            # GFF field path such as "Creature List[2]/ItemList[0]/Tag": the part before the list names the holder
            owner = path.split("/ItemList[")[0].split("/Equip_ItemList[")[0]
            out[tag].append(f"{relpath} {owner}".strip())
    return dict(out)


# A field path inside a .dlg (as the index stores GFF paths) for one EE script parameter:
# "EntryList[3]/ActionParams[0]/Key" -> ("EntryList[3]", "Action", "0", "Key");
# "EntryList[3]/RepliesList[1]/ConditionParams[0]/Value" -> ("EntryList[3]/RepliesList[1]", "Condition", "0", "Value").
PARAM_PATH = re.compile(r"^(.*)/(Action|Condition)Params\[(\d+)\]/(Key|Value)$")


def dialogue_params(db):
    """{(dlg node, edge via): {param: value}} from every conversation's ActionParams / ConditionParams (EE).
    The via matches the dependency graph's dlg_script edges: '<node>/Script' for actions, '<link>/Active' for
    conditions. Only the copy of each conversation the game uses counts (nwnlib.source_rank, as winning_sources does
    for scripts), even when that copy has no parameters.
    db: the analysis index (sqlite3.Connection), read-only."""
    try:
        files = db.execute("SELECT x.id, x.node, s.kind, s.priority FROM files x JOIN sources s ON s.id=x.source_id "
                           "WHERE x.ext='dlg'").fetchall()
    except Exception:  # noqa - an older index without these columns: no parameters
        return {}
    best = {}
    for fid, node, kind, prio in files:
        r = n.source_rank(kind, prio)
        if node and (node not in best or r < best[node][0]):
            best[node] = (r, fid)
    winners = {fid: node for node, (_r, fid) in best.items()}
    # Key and Value are separate GFF fields (separate rows): pair them up by their parameter struct first
    raw = defaultdict(dict)       # (node, prefix, kind, idx) -> {Key, Value}
    rows = db.execute("SELECT f.file_id, f.path, f.value FROM fields f JOIN files x ON x.id=f.file_id "
                      "WHERE f.label IN ('Key','Value') AND x.ext='dlg' AND f.path LIKE '%Params[%'")
    for fid, path, value in rows:
        node = winners.get(fid)
        m = PARAM_PATH.match(path or "")
        if not m or not node:
            continue
        raw[(node, m.group(1), m.group(2), int(m.group(3)))][m.group(4)] = value or ""
    out = defaultdict(dict)
    for (node, prefix, kind, _i), kv in raw.items():
        if kv.get("Key"):
            via = prefix + ("/Script" if kind == "Action" else "/Active")
            out[(node, via)][kv["Key"]] = kv.get("Value", "")
    return dict(out)
