"""
nwn_describe.py - plain-English description of what an NWScript does.

Built from: the author's header comment, whether it is an event/condition/include
script, what triggers it (from the dependency graph), and which engine functions
it calls with which string arguments. Deterministic: the same script always gets the same text. Descriptions
written by you (or by any helper) can be placed in descriptions.json in the analysis
folder and the dashboard will show them alongside.

A pure function module: describe_script() takes facts the analysis already gathered from the index and returns
text. It reads no files and writes nothing. Called by nwn_analysis.py for every script with source.
Limits: the phrases come from WHICH engine functions are called (ACTIONS below), not from the script's logic, so
"gives gold" means GiveGoldToCreature appears somewhere in the script, not that every run gives gold.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

# (regex on function name, phrase, uses string argument?)
# When the third value is True, {args} in the phrase becomes the text literals the script passes to those functions,
# e.g. CreateItemOnObject("nw_it_gold001", ...) -> "creates item 'nw_it_gold001'". Order = order in the description.
ACTIONS = [
    (r"^CreateItemOnObject$", "creates item {args}", True),
    (r"^CreateObject$", "spawns {args}", True),
    (r"^CopyObject$|^CopyItem", "copies objects/items", False),
    (r"^DestroyObject$", "destroys objects", False),
    (r"^(AddJournalQuestEntry)$", "updates journal quest {args}", True),
    (r"^RemoveJournalQuestEntry$", "removes journal quest {args}", True),
    (r"^GiveXPToCreature$|^SetXP$", "awards experience", False),
    (r"^GiveGoldToCreature$", "gives gold", False),
    (r"^TakeGoldFromCreature$", "takes gold", False),
    (r"^GetItemPossessedBy$", "checks whether someone carries item tagged {args}", True),
    (r"^GetObjectByTag$|^GetNearestObjectByTag$|^GetWaypointByTag$", "looks up object(s) tagged {args}", True),
    (r"^ActionStartConversation$|^BeginConversation$", "starts a conversation {args}", True),
    (r"^ExecuteScript$", "runs script {args}", True),
    (r"^SetEventScript$", "changes event scripts at runtime {args}", True),
    (r"^(Action)?JumpTo(Location|Object)$", "teleports creatures", False),
    (r"^StartNewModule$", "moves players to another module {args}", True),
    (r"^ApplyEffectToObject$|^ApplyEffectAtLocation$", "applies effects", False),
    (r"^(ActionAttack|SetIsTemporaryEnemy|AdjustReputation|ChangeFaction|ChangeToStandardFaction)$",
     "changes hostility/factions", False),
    (r"^(SpeakString|ActionSpeakString|SpeakOneLinerConversation|FloatingTextStringOnCreature|SendMessageToPC|SendMessageToAllDMs)$",
     "shows text/messages", False),
    (r"^(PlaySound|MusicBackground\w*|SoundObject\w*|AmbientSound\w*)$", "controls sound/music", False),
    (r"^(SetPlotFlag|SetImmortal)$", "changes plot/immortal flags", False),
    (r"^(SetLocked|SetLockKeyTag|ActionOpenDoor|ActionCloseDoor|ActionUnlockObject)$", "opens/closes/locks doors or containers", False),
    (r"^(SetCampaign\w*|GetCampaign\w*|DeleteCampaign\w*)$", "reads/writes the persistent campaign database", False),
    (r"^Sql\w*$", "runs SQL queries (persistent data)", False),
    (r"^NWNX_", "calls NWNX server extensions", False),
    (r"^(SignalEvent|EventUserDefined)$", "fires user-defined events", False),
    (r"^DelayCommand$", "schedules delayed actions", False),
    (r"^(OpenStore|gplotAppraiseOpenStore)$", "opens a store", False),
    (r"^(ActionRest|ForceRest)$", "makes creatures rest", False),
    (r"^(SetCutsceneMode|FadeToBlack|FadeFromBlack|BlackScreen|SetCameraFacing|LockCameraDistance)$", "runs cutscene/camera effects", False),
    (r"^(ActionCastSpellAtObject|ActionCastSpellAtLocation|ActionCastFakeSpell\w*)$", "casts spells", False),
    (r"^(ActionMoveToObject|ActionMoveToLocation|ActionRandomWalk|ActionForceMoveTo\w*)$", "moves creatures", False),
    (r"^(ActionEquipItem|ActionUnequipItem|ActionGiveItem|ActionTakeItem)$", "moves items between inventories", False),
    (r"^(SetLocal\w+|DeleteLocal\w+)$", "sets variable(s) {args}", True),
    (r"^GetLocal\w+$", "reads variable(s) {args}", True),
    (r"^(d\d+|Random)$", "uses dice/randomness", False),
    (r"^Get2DAString$", "reads 2da table {args}", True),
    (r"^(ExportSingleCharacter|ExportAllCharacters)$", "saves player characters", False),
    (r"^(BootPC|SetPCLike|SetPCDislike)$", "affects player connection/relations", False),
]
_ACTION_RES = [(re.compile(p), phrase, uses) for p, phrase, uses in ACTIONS]     # compiled once at import

def _fmt_args(vals, limit=4):
    """The distinct non-empty values, sorted and quoted: "'a', 'b' (+3 more)"; "" when there are none."""
    vals = sorted({v for v in vals if v})
    if not vals:
        return ""
    shown = ", ".join(f"'{v}'" for v in vals[:limit])
    return shown + (f" (+{len(vals) - limit} more)" if len(vals) > limit else "")


def describe_script(name, has_main, has_sc, header, calls_json, literals, triggers, include_users, dynamic,
                    lib_calls=(), defined=(), missing_inc=()):
    """
    Plain-English description of one script from facts the index already holds. Pure: no I/O, never writes.

    name: script resref (not used in the text); has_main / has_sc: the script defines main() / StartingConditional()
    header: the author's leading comment, one line ("" when none)
    calls_json: JSON text {function name: number of calls} from the index's scripts.calls column (every capitalised
                name followed by "(" - engine functions and most module functions)
    literals: [(func, literal)] typed literals from this script
    triggers: [(kind, src_label, via)] - who runs this script
    include_users: number of scripts that #include it
    dynamic: list of name prefixes this script builds at runtime
    lib_calls: ["Fn() from lib", ...] functions it calls from module includes; defined: functions it defines
    missing_inc: text of calls to module functions whose include is missing (reported as a compile problem)
    Returns dict(summary, details[list], role) - role is "Condition", "Action/event" or "Include library".
    """
    calls = json.loads(calls_json or "{}")
    by_func = defaultdict(set)
    for func, lit in literals:
        if func:
            by_func[func].add(lit)

    # StartingConditional() is the entry point the engine calls for a conversation "Text Appears When" script
    if has_sc:
        role = "Condition"
    elif has_main:
        role = "Action/event"
    else:
        role = "Include library"

    # what triggers it
    trig = []
    for kind, src_label, via in triggers[:6]:
        label = via.split("/")[-1] if via else ""           # last part of the field path, e.g. "OnDeath"
        if kind == "dlg_script":
            # in a .dlg, the "Active" field holds the node's condition script; other script fields are actions
            trig.append(f"{'condition' if label == 'Active' else 'action'} in conversation {src_label}")
        elif kind == "module_event":
            trig.append(f"module {label}")
        elif kind == "event_script":
            trig.append(f"{label} of {src_label}")
        elif kind == "execute_script":
            trig.append(f"ExecuteScript from {src_label}")
        elif kind == "tag_based_script":
            trig.append(f"tag-based events of item {src_label}")
    if len(triggers) > 6:
        trig.append(f"+{len(triggers) - 6} more")

    # what it does
    doing = []
    used = set()
    for rx, phrase, uses_args in _ACTION_RES:
        hits = [f for f in calls if rx.search(f)]
        if not hits:
            continue
        args = set()
        for f in hits:
            args |= by_func.get(f, set())
            used.add(f)
        text = phrase.format(args=_fmt_args(args) if uses_args else "").strip()
        text = re.sub(r"\s+", " ", text)
        if text not in doing:
            doing.append(text)
    for p in dynamic:
        doing.append(f"builds script/object names at runtime starting with '{p}'")
    if lib_calls:
        doing.append("calls library function(s) " + ", ".join(lib_calls[:4]) +
                     (f" (+{len(lib_calls) - 4} more)" if len(lib_calls) > 4 else ""))
    if missing_inc:
        doing.append("COMPILE ERROR: calls " + ", ".join(missing_inc[:3]))

    engine_calls = sum(calls.values())
    details = []
    if header:
        details.append(f"Author's note: {header}")
    if missing_inc:
        details.append("Problem: uses " + ", ".join(missing_inc) + " - add the #include or the script will not compile")
    if trig:
        details.append("Triggered by: " + "; ".join(trig))
    elif role != "Include library":
        details.append("Triggered by: nothing found in the module (may be unused, dynamic, or called by the engine)")
    if defined:
        details.append("Defines: " + ", ".join(defined[:20]) + (" …" if len(defined) > 20 else ""))
    if include_users:
        details.append(f"Included by {include_users} script(s) - changes here recompile into all of them")
    if doing:
        details.append("Does: " + "; ".join(doing))
    details.append(f"Size: {engine_calls} function calls across {len(calls)} distinct functions")

    # the one-line summary: lead with the role, then the most telling phrases
    if role == "Condition":
        summary = "Conversation/event condition"
        checks = [d for d in doing if d.startswith(("checks", "reads", "looks up"))]
        if checks:
            summary += " that " + "; ".join(checks[:2])
    elif role == "Include library":
        fns = [f for f in defined if f not in ("main", "StartingConditional")]
        summary = "Shared function library"
        if fns:
            summary += " (" + ", ".join(fns[:4]) + (f" +{len(fns) - 4}" if len(fns) > 4 else "") + ")"
        summary += f" used by {include_users} script(s)" if include_users else " not included by anything"
        if doing:
            summary += " that " + "; ".join(doing[:2])
    else:
        summary = "Script that " + ("; ".join(doing[:3]) if doing else "performs simple logic")
    if trig:
        summary += f" (runs on: {trig[0]}{' …' if len(trig) > 1 else ''})"
    return {"role": role, "summary": summary[0].upper() + summary[1:], "details": details}
