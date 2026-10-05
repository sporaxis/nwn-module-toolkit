"""
nwn_edit.py - editing and generating files WITHOUT touching the original module.

Why this exists
---------------
Golden rule 1: the original module is never modified. Every change the builder makes (in the dashboard editor,
the script generator or Find & replace in nwn_refactor.py) is written instead to an overlay folder,
<analysis>/edits/. The dashboard shows the edited version, and the clean build (nwn_build.py) takes the edited copy
in place of the original.

The overlay (all names are lower case, one flat folder)
-------------------------------------------------------
    edits/<file>            the edited (or new) file, in the game's own format: text for .nss/.2da/...,
                            binary GFF for .uti/.utc/.dlg/...; it shadows the module's file of the same name
    edits/<file>.new        empty marker: <file> is NEW (not in the module) - the build adds it
    edits/<file>.deleted    marker: the build drops <file> from the clean module; the text inside is a note,
                            e.g. "renamed to foo.uti" (written by a Find & replace rename)
    edits/.history/         Undo records of Find & replace (owned by nwn_refactor.py)
    edits/.history/discarded/<time>/   what discard() took out of the overlay, kept so it can be restored by hand

Reads: the edits folder; the game's nwscript.nss (to check generated scripts against the installed game build); the
analysis index, read-only, for the `apply` reminder.
Writes: only inside <analysis>/edits/. Never the module folder, the .mod, a hak or the game install.

Main entry points
-----------------
    save_text / save_gff_json   validate, then write an edit (a bad edit raises and nothing is written)
    current_bytes               the file as the toolkit should see it: edited copy if any, else the original
    list_edits / discard        what is in the overlay; drop one edit (kept in edits/.history/discarded)
    generate_script             build a commented NWScript from an event + conditions + actions
    load_nwscript / verify_script   the functions and constants of the installed game, and a check against them

Supported edits
  * scripts (.nss)         plain text; optional compile check with the official compiler
  * 2da / txi / set / ascii mdl / any text resource   plain text
  * every GFF file (.uti .utc .utp .are .git .dlg .jrl .ifo ...)  edited as nwn_gff JSON,
    validated by converting back to binary before it is accepted
  * new scripts from the generator (see generate_script)

Limits
------
Edits take effect in the clean build; the analysis (index.sqlite, report.json) still describes the original until it
is re-analysed. `python nwn_edit.py apply <analysis>` only prints that reminder - it does not re-analyse.
The text checks are light: a .2da is parsed, other text is stored as given. Scripts are not compiled here (the
dashboard's editor and the build do that).
"""
from __future__ import annotations

import json
import os
import re

import nwnlib as n

# Resource types edited as plain text. Everything else is either GFF (edited as JSON) or binary (not editable here).
# ASCII .mdl is also accepted by save_text, but kept out of this set because a .mdl may be a binary model.
TEXT_EXTS = {"nss", "2da", "txi", "set", "ini", "txt", "ltr", "mtr", "shd", "lua", "sql", "jui", "tml"}


def edits_dir(analysis):
    """Path of the analysis's edits overlay folder (<analysis>/edits), created if missing.

    analysis: the analysis folder (nwn_workspace/<module>). Creates only that one folder."""
    d = os.path.join(analysis, "edits")
    os.makedirs(d, exist_ok=True)
    return d


# A resource name (resref) as the game and toolset accept it: lower-case letters, digits and _, 1-16 characters.
# Used with fullmatch, e.g. "q_rats_done" matches, "Q-Rats" and "a_name_longer_than_16" do not.
RESREF_RE = re.compile(r"[a-z0-9_]{1,16}")


def valid_new_name(fn):
    """A name the game and toolset accept for a NEW resource: letters, digits and _ only, 16 characters at most.

    fn: file name such as "my_script.nss" (the extension is ignored; capitals are allowed and checked lower-cased).
    Returns fn unchanged when it is acceptable; raises ValueError with a plain-English reason otherwise."""
    base = fn.rsplit(".", 1)[0].lower()
    if not RESREF_RE.fullmatch(base):
        raise ValueError(f"'{base}' can't be a resource name - use letters, digits and _ only, 16 characters at most")
    return fn


def edit_path(analysis, relpath):
    """Where the edit of `relpath` lives: <analysis>/edits/<file name, lower case>.

    relpath: the file's name in the module, e.g. "quest_giver.utc" (any folder part is dropped).
    Raises ValueError for a name that could escape the edits folder or is not a usable file name (path separators,
    "..", reserved Windows device names, no extension) - this is the path check every edit write goes through.
    Lower case because NWN resource names are case-insensitive: one file, one edit."""
    fn = os.path.basename(relpath).lower()
    if not n.safe_resref(fn.rsplit(".", 1)[0]) or "." not in fn:
        raise ValueError(f"bad file name: {relpath!r}")
    return os.path.join(edits_dir(analysis), fn)


def list_edits(analysis):
    """Everything in the overlay, as {file name: info}. Read-only.

    info is dict(kind="edited", size, mtime, new) for an edited or new file (new=True when a <file>.new marker exists),
    or dict(kind="deleted", note) for a <file>.deleted marker (note = the marker's text, e.g. "renamed to x.uti").
    The .new markers themselves are not listed, and nor is the .history folder."""
    d = edits_dir(analysis)
    out = {}
    for fn in sorted(os.listdir(d)):
        p = os.path.join(d, fn)
        if fn.endswith(".deleted"):
            out[fn[:-8]] = dict(kind="deleted", note=open(p, encoding="utf-8", errors="replace").read().strip())
        elif fn.endswith(".new"):
            continue                    # marker: shown as the 'new' flag of its file
        elif os.path.isfile(p):
            out[fn] = dict(kind="edited", size=os.path.getsize(p), mtime=os.path.getmtime(p),
                           new=os.path.isfile(p + ".new"))
    return out


def current_bytes(analysis, relpath, original_loader):
    """Edited bytes if an edit exists, else the original.

    original_loader: a function relpath -> bytes (or None) that reads the module's own copy; it is only called when
    there is no edit. Returns (bytes, True) for the edited copy, (original bytes, False) otherwise. Read-only.
    A .deleted marker is not consulted here: callers that care (the build, Find & replace) check it themselves."""
    p = edit_path(analysis, relpath)
    if os.path.isfile(p):
        with open(p, "rb") as fh:
            return fh.read(), True
    data = original_loader(relpath)
    return data, False


def _undelete(p):
    """Remove the <file>.deleted marker next to a just-written edit `p`, if there is one.

    list_edits() lets a .deleted marker override the file of the same name, so without this the build would still
    drop a file the builder has just saved (a rename's old name brought back, or a removal reversed by editing)."""
    if os.path.isfile(p + ".deleted"):
        os.remove(p + ".deleted")


def save_text(analysis, relpath, text, is_new=False):
    """Validate and write a text resource (.nss, .2da, ASCII .mdl ...) to the edits overlay.

    analysis: the analysis folder; relpath: the file name, e.g. "q_rats.nss"; text: the whole new content (str).
    is_new: True for a file that is not in the module - its name must then be a valid resref, and a <file>.new
    marker is written so the build adds it.
    Returns the path written. Raises ValueError (nothing written) for a non-text type, a .2da whose first line does
    not start with "2DA", a bad new name, or a character that has no Windows-1252 byte.
    Line endings are stored as \\n; the text is stored in NWN's Windows-1252 encoding (nwnlib.encode_text).
    A <file>.deleted marker (from a Find & replace rename or removal) is dropped: the saved file is wanted again.
    Writes only inside <analysis>/edits/."""
    ext = relpath.rsplit(".", 1)[-1].lower()
    if ext not in TEXT_EXTS and ext != "mdl":
        raise ValueError(f".{ext} is not a text resource - edit it as JSON")
    if ext == "2da":
        n.read_2da(text)  # validates the header/columns
    text = text.replace("\r\n", "\n")
    if is_new:
        valid_new_name(os.path.basename(relpath))
    p = edit_path(analysis, relpath)
    with open(p, "wb") as fh:
        fh.write(n.encode_text(text))
    if is_new:
        open(p + ".new", "w").close()
    _undelete(p)
    return p


def save_gff_json(analysis, relpath, json_text):
    """Validate and write a GFF resource (.uti .utc .dlg .are .git .ifo ...) given as nwn_gff JSON text.

    The JSON is converted to binary GFF and read back before anything is written, so an edit that the game could
    not load (a bad value, a resref over 16 characters, broken structure) is refused with an error instead of
    reaching the build. An empty file-type in the JSON is filled from the extension ("UTI " etc.).
    Returns the path written (<analysis>/edits/<file>). On bad input it raises (ValueError for bad JSON or a
    non-GFF type, nwnlib.GffError for a value the format can't hold) and nothing is written.
    As in save_text, a <file>.deleted marker is dropped so the build takes the saved file."""
    ext = relpath.rsplit(".", 1)[-1].lower()
    if ext not in n.GFF_EXTENSIONS:
        raise ValueError(f".{ext} is not a GFF resource")
    j = json.loads(json_text)
    root = n.gff_from_json(j)
    if not root.file_type.strip():
        root.file_type = ext.upper().ljust(4)
    data = n.write_gff(root)          # raises GffError on bad values (e.g. resref > 16)
    n.read_gff(data)                  # and it must read back
    p = edit_path(analysis, relpath)
    with open(p, "wb") as fh:
        fh.write(data)
    _undelete(p)
    return p


def discard(analysis, relpath):
    """Drop an edit. Discarding a file that a rename created also brings the old name back (its .deleted marker),
    so the clean build never loses both.

    Takes <file>, <file>.new and <file>.deleted out of the overlay (whichever exist), and - for a new file - any
    marker whose text is exactly "renamed to <file>". They are MOVED, not deleted, into
    <analysis>/edits/.history/discarded/<date_time_microseconds>/ (one new folder per discard), so a discard can be
    restored by hand by copying the files back into edits/. Nothing there is read by the build or the dashboard.
    Returns that folder (None when there was nothing to discard). Never touches the module."""
    p = edit_path(analysis, relpath)
    d = edits_dir(analysis)
    fn = os.path.basename(p)
    moving = [q for q in (p, p + ".new", p + ".deleted") if os.path.isfile(q)]
    if os.path.isfile(p + ".new"):
        for m in sorted(os.listdir(d)):
            mp = os.path.join(d, m)
            if m.endswith(".deleted") and mp not in moving:
                with open(mp, encoding="utf-8", errors="replace") as fh:
                    if fh.read().strip() == f"renamed to {fn}":
                        moving.append(mp)
    if not moving:
        return None
    keep = _discard_folder(d)
    for q in moving:
        # same folder tree as the overlay (same drive), so this is a rename: nothing is copied or lost on the way
        os.replace(q, os.path.join(keep, os.path.basename(q)))
    return keep


def _discard_folder(edits):
    """A new, empty edits/.history/discarded/<date_time_microseconds>[_<n>] folder for one discard. The name sorts
    by time as text; a second discard in the same microsecond gets a _2, _3 ... suffix instead of sharing a folder."""
    import datetime
    base = os.path.join(edits, ".history", "discarded", datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    keep, i = base, 1
    while True:
        try:
            os.makedirs(keep)
            return keep
        except FileExistsError:
            i += 1
            keep = f"{base}_{i}"


# ---------------------------------------------------------------------------
# Script generator - in the spirit of Lilac Soul's NWN Script Generator:
# pick the event, pick what should happen, get a commented script that compiles.
# ---------------------------------------------------------------------------
# The event-specific lines come from the NWScript functions the engine provides for each event (nwscript.nss), e.g.
# GetEnteringObject() is only meaningful inside an OnEnter script.
EVENTS = {
    # key: (label, who OBJECT_SELF is, how to get the player, hint of where to put it, is condition?)
    "OnEnter": ("Area / trigger OnEnter", "the area or trigger", "object oPC = GetEnteringObject();", "Area properties > Events > OnEnter, or a trigger's OnEnter", False),
    "OnExit": ("Area / trigger OnExit", "the area or trigger", "object oPC = GetExitingObject();", "OnExit event of the area or trigger", False),
    "OnUsed": ("Placeable OnUsed", "the placeable", "object oPC = GetLastUsedBy();", "Placeable properties > Scripts > OnUsed", False),
    "OnOpen": ("Door / placeable OnOpen", "the door or container", "object oPC = GetLastOpenedBy();", "OnOpen event of the door or placeable", False),
    "OnDeath": ("Creature OnDeath", "the dying creature", "object oPC = GetLastKiller();", "Creature properties > Scripts > OnDeath", False),
    "OnSpawn": ("Creature OnSpawn", "the creature", "object oPC = OBJECT_INVALID;", "Creature properties > Scripts > OnSpawn", False),
    "OnConversation": ("Creature OnConversation", "the creature", "object oPC = GetLastSpeaker();", "Creature properties > Scripts > OnConversation", False),
    "OnPerception": ("Creature OnPerception", "the creature", "object oPC = GetLastPerceived();", "Creature properties > Scripts > OnPerception", False),
    "OnHeartbeat": ("Heartbeat (every 6 seconds)", "the object", "object oPC = GetFirstPC();", "OnHeartbeat of the creature/placeable/area", False),
    "OnUserDefined": ("OnUserDefined (custom event)", "the object", "object oPC = GetFirstPC();", "OnUserDefined of the object; fire with SignalEvent(oObj, EventUserDefined(n))", False),
    "OnActivateItem": ("Module OnActivateItem (tag-based)", "the module", "object oPC = GetItemActivator();\n    object oItem = GetItemActivated();", "Module properties > Events > OnActivateItem (or name the script after the item tag)", False),
    "OnClientEnter": ("Module OnClientEnter", "the module", "object oPC = GetEnteringObject();", "Module properties > Events > OnClientEnter", False),
    "OnPlayerDeath": ("Module OnPlayerDeath", "the module", "object oPC = GetLastPlayerDied();", "Module properties > Events > OnPlayerDeath", False),
    "DlgAction": ("Conversation action (Actions Taken)", "the NPC talking", "object oPC = GetPCSpeaker();", "the conversation node's 'Actions Taken' tab", False),
    # --- more events (EE) ---
    "OnModuleLoad": ("Module OnModuleLoad", "the module", "object oPC = OBJECT_INVALID;", "Module properties > Events > OnModuleLoad", False),
    "OnClientLeave": ("Module OnClientLeave", "the module", "object oPC = GetExitingObject();", "Module properties > Events > OnClientLeave", False),
    "OnPlayerChat": ("Module OnPlayerChat", "the module", "object oPC = GetPCChatSpeaker();\n    string sChat = GetPCChatMessage();", "Module properties > Events > OnPlayerChat", False),
    "OnAcquireItem": ("Module OnAcquireItem", "the module", "object oPC = GetModuleItemAcquiredBy();\n    object oItem = GetModuleItemAcquired();", "Module properties > Events > OnAcquireItem", False),
    "OnUnAcquireItem": ("Module OnUnAcquireItem", "the module", "object oPC = GetModuleItemLostBy();\n    object oItem = GetModuleItemLost();", "Module properties > Events > OnUnAcquireItem", False),
    "OnPlayerEquipItem": ("Module OnPlayerEquipItem", "the module", "object oPC = GetPCItemLastEquippedBy();\n    object oItem = GetPCItemLastEquipped();", "Module properties > Events > OnPlayerEquipItem", False),
    "OnPlayerUnEquipItem": ("Module OnPlayerUnEquipItem", "the module", "object oPC = GetPCItemLastUnequippedBy();\n    object oItem = GetPCItemLastUnequipped();", "Module properties > Events > OnPlayerUnEquipItem", False),
    "OnPlayerLevelUp": ("Module OnPlayerLevelUp", "the module", "object oPC = GetPCLevellingUp();", "Module properties > Events > OnPlayerLevelUp", False),
    "OnPlayerRest": ("Module OnPlayerRest", "the module", "object oPC = GetLastPCRested();", "Module properties > Events > OnPlayerRest", False),
    "OnPlayerDying": ("Module OnPlayerDying", "the module", "object oPC = GetLastPlayerDying();", "Module properties > Events > OnPlayerDying", False),
    "OnPlayerRespawn": ("Module OnPlayerRespawn", "the module", "object oPC = GetLastRespawnButtonPresser();", "Module properties > Events > OnPlayerRespawn", False),
    "OnPlayerTarget": ("Module OnPlayerTarget (EE targeting mode)", "the module", "object oPC = GetLastPlayerToSelectTarget();\n    object oTarget = GetTargetingModeSelectedObject();", "Module properties > Events > OnPlayerTarget", False),
    "OnPlayerGUI": ("Module OnPlayerGUIEvent (EE)", "the module", "object oPC = GetLastGuiEventPlayer();", "Module properties > Events > OnPlayerGUIEvent", False),
    "OnDamaged": ("Creature / placeable OnDamaged", "the object hit", "object oPC = GetLastDamager();", "OnDamaged of the creature or placeable", False),
    "OnPhysicalAttacked": ("Creature / placeable OnPhysicalAttacked", "the object attacked", "object oPC = GetLastAttacker();", "OnPhysicalAttacked of the creature or placeable", False),
    "OnDisturbed": ("Creature / placeable OnDisturbed (inventory)", "the container or creature", "object oPC = GetLastDisturbed();\n    object oItem = GetInventoryDisturbItem();", "OnDisturbed / OnInventoryDisturbed", False),
    "OnSpellCastAt": ("Creature / placeable OnSpellCastAt", "the target", "object oPC = GetLastSpellCaster();", "OnSpellCastAt of the creature or placeable", False),
    "OnPlaceableClick": ("Placeable OnClick (EE)", "the placeable", "object oPC = GetPlaceableLastClickedBy();", "Placeable properties > Scripts > OnClick", False),
    "OnDoorClick": ("Door / trigger OnClick (area transition)", "the door or trigger", "object oPC = GetClickingObject();", "OnClick / OnAreaTransitionClick of the door or trigger", False),
    "OnFailToOpen": ("Door OnFailToOpen (locked)", "the door", "object oPC = GetClickingObject();", "Door properties > Scripts > OnFailToOpen", False),
    "OnStoreOpen": ("Store OnOpenStore", "the store", "object oPC = GetLastOpenedBy();", "Store properties > Scripts > OnOpenStore", False),
    "DlgCondition": ("Conversation condition (Text Appears When)", "the NPC talking", "object oPC = GetPCSpeaker();", "the conversation node's 'Text Appears When' tab", True),
}

# Actions: key -> (label, fields[(name, label, kind)], code template, includes, condition-expr or None)
# Field kinds (checked by _fmt): int, float, resref, stage, token, text (free text - only ever inside "quotes"),
# const:<PREFIX> (a constant from nwscript.nss) and select:<a|b|c> (one of a fixed list).
# Templates are Python str.format strings: {name} is a field, and {{ / }} are literal NWScript braces.
ACTIONS = {
    "journal": ("Update a journal quest", [("quest", "Quest tag", "text"), ("entry", "Entry number", "int")],
                'AddJournalQuestEntry("{quest}", {entry}, oPC);', [], None),
    "give_item": ("Give the player an item", [("resref", "Item blueprint (resref)", "resref"), ("count", "How many", "int")],
                  'CreateItemOnObject("{resref}", oPC, {count});', [], None),
    "take_item": ("Take an item from the player", [("tag", "Item tag", "text")],
                  'object oTake = GetItemPossessedBy(oPC, "{tag}");\n    if (GetIsObjectValid(oTake)) DestroyObject(oTake);', [], None),
    "gold": ("Give gold", [("amount", "Gold pieces", "int")], 'GiveGoldToCreature(oPC, {amount});', [], None),
    "take_gold": ("Take gold", [("amount", "Gold pieces", "int")], 'TakeGoldFromCreature({amount}, oPC, TRUE);', [], None),
    "xp": ("Give experience", [("amount", "XP", "int")], 'GiveXPToCreature(oPC, {amount});', [], None),
    "spawn": ("Spawn a creature at a waypoint", [("resref", "Creature blueprint (resref)", "resref"), ("wp", "Waypoint tag", "text")],
              'location lSpawn = GetLocation(GetWaypointByTag("{wp}"));\n    CreateObject(OBJECT_TYPE_CREATURE, "{resref}", lSpawn);', [], None),
    "spawn_placeable": ("Spawn a placeable at a waypoint", [("resref", "Placeable blueprint (resref)", "resref"), ("wp", "Waypoint tag", "text")],
                        'location lSpawn = GetLocation(GetWaypointByTag("{wp}"));\n    CreateObject(OBJECT_TYPE_PLACEABLE, "{resref}", lSpawn);', [], None),
    "jump": ("Teleport the player to a waypoint", [("wp", "Waypoint tag", "text")],
             'AssignCommand(oPC, ClearAllActions());\n    AssignCommand(oPC, JumpToObject(GetWaypointByTag("{wp}")));', [], None),
    "setint": ("Set a variable (int) on the player", [("var", "Variable name", "text"), ("value", "Value", "int")],
               'SetLocalInt(oPC, "{var}", {value});', [], None),
    "setint_self": ("Set a variable (int) on this object", [("var", "Variable name", "text"), ("value", "Value", "int")],
                    'SetLocalInt(OBJECT_SELF, "{var}", {value});', [], None),
    "setint_module": ("Set a variable (int) on the module", [("var", "Variable name", "text"), ("value", "Value", "int")],
                      'SetLocalInt(GetModule(), "{var}", {value});', [], None),
    "unlock": ("Unlock a door or container by tag", [("tag", "Object tag", "text")],
               'object oLock = GetObjectByTag("{tag}");\n    SetLocked(oLock, FALSE);', [], None),
    "lock": ("Lock a door or container by tag", [("tag", "Object tag", "text")],
             'object oLock = GetObjectByTag("{tag}");\n    SetLocked(oLock, TRUE);', [], None),
    "open_door": ("Open a door by tag", [("tag", "Door tag", "text")],
                  'object oDoor = GetObjectByTag("{tag}");\n    AssignCommand(oDoor, ActionOpenDoor(oDoor));', [], None),
    "destroy": ("Destroy an object by tag", [("tag", "Object tag", "text")],
                'DestroyObject(GetObjectByTag("{tag}"));', [], None),
    "hostile": ("Make a creature hostile to the player", [("tag", "Creature tag", "text")],
                'object oNPC = GetObjectByTag("{tag}");\n    SetIsTemporaryEnemy(oPC, oNPC);\n    AssignCommand(oNPC, ActionAttack(oPC));', [], None),
    "faction": ("Change a creature to a standard faction", [("tag", "Creature tag", "text"), ("faction", "Faction", "select:STANDARD_FACTION_HOSTILE|STANDARD_FACTION_COMMONER|STANDARD_FACTION_MERCHANT|STANDARD_FACTION_DEFENDER")],
                'ChangeToStandardFaction(GetObjectByTag("{tag}"), {faction});', [], None),
    "speak": ("Make this object say something", [("text", "Text", "text")], 'ActionSpeakString("{text}");', [], None),
    "floaty": ("Floating text over the player", [("text", "Text", "text")], 'FloatingTextStringOnCreature("{text}", oPC, FALSE);', [], None),
    "message": ("Send the player a message", [("text", "Text", "text")], 'SendMessageToPC(oPC, "{text}");', [], None),
    "sound": ("Play a sound", [("sound", "Sound resref (e.g. as_cv_bell1)", "text")], 'PlaySound("{sound}");', [], None),
    "music": ("Change area music", [("track", "Track number (ambientmusic.2da row)", "int")],
              'MusicBackgroundChangeDay(GetArea(oPC), {track});\n    MusicBackgroundChangeNight(GetArea(oPC), {track});\n    MusicBackgroundPlay(GetArea(oPC));', [], None),
    "effect": ("Apply a visual effect to the player", [("vfx", "Effect", "const:VFX_IMP_")],
               'ApplyEffectToObject(DURATION_TYPE_INSTANT, EffectVisualEffect({vfx}), oPC);', [], None),
    "heal": ("Heal the player fully", [], 'ApplyEffectToObject(DURATION_TYPE_INSTANT, EffectHeal(GetMaxHitPoints(oPC)), oPC);', [], None),
    "damage": ("Damage the player", [("amount", "Hit points", "int")],
               'ApplyEffectToObject(DURATION_TYPE_INSTANT, EffectDamage({amount}), oPC);', [], None),
    "conversation": ("Start a conversation with the player", [("dlg", "Conversation resref (blank = this creature's own)", "text")],
                     'AssignCommand(OBJECT_SELF, ActionStartConversation(oPC, "{dlg}"));', [], None),
    "execute": ("Run another script", [("script", "Script name", "resref")], 'ExecuteScript("{script}", OBJECT_SELF);', [], None),
    "signal": ("Fire a user-defined event on an object", [("tag", "Object tag", "text"), ("num", "Event number", "int")],
               'SignalEvent(GetObjectByTag("{tag}"), EventUserDefined({num}));', [], None),
    "delay": ("Wait, then run another script", [("secs", "Seconds", "int"), ("script", "Script name", "resref")],
              'DelayCommand({secs}.0, ExecuteScript("{script}", OBJECT_SELF));', [], None),
    # --- effects (EE constants come from your installed game's nwscript.nss) ---
    "effect_timed": ("Apply a timed effect to the player", [("effect", "Effect", "select:" + "|".join(k for k in (
        "Haste", "Slow", "Invisibility", "Sanctuary", "Paralyze", "Blindness", "Deafness", "Regenerate", "Temporary HP",
        "See invisible", "Ultravision", "Knockdown", "Stun", "Daze", "Sleep", "Silence", "Faster movement",
        "Slower movement", "Petrify", "Cutscene paralyze"))), ("secs", "Seconds", "float")],
                     'ApplyEffectToObject(DURATION_TYPE_TEMPORARY, {effect}, oPC, {secs});', [], None),
    "ability_boost": ("Raise an ability for a while", [("ability", "Ability", "const:ABILITY_"), ("amount", "By", "int"), ("secs", "Seconds", "float")],
                      'ApplyEffectToObject(DURATION_TYPE_TEMPORARY, EffectAbilityIncrease({ability}, {amount}), oPC, {secs});', [], None),
    "skill_boost": ("Raise a skill for a while", [("skill", "Skill", "const:SKILL_"), ("amount", "By", "int"), ("secs", "Seconds", "float")],
                    'ApplyEffectToObject(DURATION_TYPE_TEMPORARY, EffectSkillIncrease({skill}, {amount}), oPC, {secs});', [], None),
    "damage_typed": ("Damage the player (with a damage type)", [("amount", "Hit points", "int"), ("dtype", "Damage type", "const:DAMAGE_TYPE_")],
                     'ApplyEffectToObject(DURATION_TYPE_INSTANT, EffectDamage({amount}, {dtype}), oPC);', [], None),
    "vfx_duration": ("Visual effect on the player for a while", [("vfx", "Effect", "const:VFX_DUR_"), ("secs", "Seconds", "float")],
                     'ApplyEffectToObject(DURATION_TYPE_TEMPORARY, EffectVisualEffect({vfx}), oPC, {secs});', [], None),
    "vfx_location": ("Visual effect where the player stands", [("vfx", "Effect", "const:VFX_")],
                     'ApplyEffectAtLocation(DURATION_TYPE_INSTANT, EffectVisualEffect({vfx}), GetLocation(oPC));', [], None),
    "remove_effects": ("Remove all effects from the player", [],
                       'effect eFx = GetFirstEffect(oPC);\n    while (GetIsEffectValid(eFx))\n    {{\n        RemoveEffect(oPC, eFx);\n        eFx = GetNextEffect(oPC);\n    }}', [], None),
    "animation": ("Make this object play an animation", [("anim", "Animation", "const:ANIMATION_"), ("secs", "Seconds (0 = once)", "float")],
                  'AssignCommand(OBJECT_SELF, ActionPlayAnimation({anim}, 1.0, {secs}));', [], None),
    # --- spawning ---
    "spawn_near_pc": ("Spawn creatures next to the player", [("resref", "Creature blueprint (resref)", "resref"), ("count", "How many", "int"),
                                                              ("hostile", "Attack the player?", "select:yes|no")],
                      'int nSpawn;\n    for (nSpawn = 0; nSpawn < {count}; nSpawn++)\n    {{\n        object oNew = CreateObject(OBJECT_TYPE_CREATURE, "{resref}", GetLocation(oPC));\n        if ("{hostile}" == "yes") AssignCommand(oNew, ActionAttack(oPC));\n    }}', [], None),
    "spawn_timed": ("Spawn a creature at a waypoint, remove it after a while", [("resref", "Creature blueprint (resref)", "resref"), ("wp", "Waypoint tag", "text"), ("secs", "Remove after seconds", "float")],
                    'object oTemp = CreateObject(OBJECT_TYPE_CREATURE, "{resref}", GetLocation(GetWaypointByTag("{wp}")));\n    DestroyObject(oTemp, {secs});', [], None),
    # --- inventory and party ---
    "give_item_party": ("Give every party member an item", [("resref", "Item blueprint (resref)", "resref")],
                        'object oMember = GetFirstFactionMember(oPC, TRUE);\n    while (GetIsObjectValid(oMember))\n    {{\n        CreateItemOnObject("{resref}", oMember);\n        oMember = GetNextFactionMember(oPC, TRUE);\n    }}', [], None),
    "take_items_all": ("Take every item with a tag from the player", [("tag", "Item tag", "text")],
                       'object oInv = GetFirstItemInInventory(oPC);\n    while (GetIsObjectValid(oInv))\n    {{\n        if (GetTag(oInv) == "{tag}") DestroyObject(oInv);\n        oInv = GetNextItemInInventory(oPC);\n    }}', [], None),
    "xp_party": ("Give experience to the whole party", [("amount", "XP each", "int")],
                 'object oMember = GetFirstFactionMember(oPC, TRUE);\n    while (GetIsObjectValid(oMember))\n    {{\n        GiveXPToCreature(oMember, {amount});\n        oMember = GetNextFactionMember(oPC, TRUE);\n    }}', [], None),
    "gold_party": ("Give gold to the whole party", [("amount", "Gold each", "int")],
                   'object oMember = GetFirstFactionMember(oPC, TRUE);\n    while (GetIsObjectValid(oMember))\n    {{\n        GiveGoldToCreature(oMember, {amount});\n        oMember = GetNextFactionMember(oPC, TRUE);\n    }}', [], None),
    "journal_party": ("Update a journal quest for the whole party", [("quest", "Quest tag", "text"), ("entry", "Entry number", "int")],
                      'AddJournalQuestEntry("{quest}", {entry}, oPC, TRUE);', [], None),
    "journal_remove": ("Remove a journal quest from the player", [("quest", "Quest tag", "text")],
                       'RemoveJournalQuestEntry("{quest}", oPC);', [], None),
    # --- variables and persistence ---
    "setstring_pc": ("Set a variable (text) on the player", [("var", "Variable name", "text"), ("value", "Text", "text")],
                     'SetLocalString(oPC, "{var}", "{value}");', [], None),
    "delete_local": ("Clear a variable on the player", [("var", "Variable name", "text")],
                     'DeleteLocalInt(oPC, "{var}");\n    DeleteLocalString(oPC, "{var}");', [], None),
    "campaign_int": ("Save a number in the campaign database (per player)", [("db", "Database name", "text"), ("var", "Variable", "text"), ("value", "Value", "int")],
                     'SetCampaignInt("{db}", "{var}", {value}, oPC);', [], None),
    "campaign_str": ("Save text in the campaign database (per player)", [("db", "Database name", "text"), ("var", "Variable", "text"), ("value", "Text", "text")],
                     'SetCampaignString("{db}", "{var}", "{value}", oPC);', [], None),
    "sql_player_set": ("Save a value in the player's own database (EE, travels with the character)", [("key", "Key", "text"), ("value", "Value", "text")],
                       'TkSqlSet(oPC, "{key}", "{value}");', [], None),
    "sql_module_set": ("Save a value in the module's database (EE, per server)", [("key", "Key", "text"), ("value", "Value", "text")],
                       'TkSqlSet(GetModule(), "{key}", "{value}");', [], None),
    "custom_token": ("Set a conversation custom token (<CUSTOMnnn>)", [("num", "Token number (e.g. 1001)", "int"), ("text", "Text", "text")],
                     'SetCustomToken({num}, "{text}");', [], None),
    "token_set": ("Set a quest token (this module's token system)", [("slot", "Quest (token name)", "token"), ("value", "Stage (one character: 1-9, or a letter such as D)", "stage")],
                  '{setter}(oPC, {slot}, "{value}");', [], None),
    # --- objects ---
    "plot_flag": ("Make an object plot (can't be destroyed) or not", [("tag", "Object tag", "text"), ("on", "Plot?", "select:TRUE|FALSE")],
                  'SetPlotFlag(GetObjectByTag("{tag}"), {on});', [], None),
    "appearance": ("Change a creature's appearance", [("tag", "Creature tag", "text"), ("app", "Appearance", "const:APPEARANCE_TYPE_")],
                   'SetCreatureAppearanceType(GetObjectByTag("{tag}"), {app});', [], None),
    "destroy_self": ("Remove this object after a delay", [("secs", "Seconds", "float")], 'DestroyObject(OBJECT_SELF, {secs});', [], None),
    "once": ("Only do this once (per object)", [], "", [], None),   # handled specially
    "pc_only": ("Only for player characters", [], "", [], None),    # handled specially
}

# Conditions: key -> (label, fields, NWScript boolean expression). The same field kinds and format rules as ACTIONS.
CONDITIONS = {
    "has_item": ("Player carries an item", [("tag", "Item tag", "text")], 'GetIsObjectValid(GetItemPossessedBy(oPC, "{tag}"))'),
    "int_pc": ("Variable on the player equals", [("var", "Variable name", "text"), ("value", "Value", "int")], 'GetLocalInt(oPC, "{var}") == {value}'),
    "int_module": ("Variable on the module equals", [("var", "Variable name", "text"), ("value", "Value", "int")], 'GetLocalInt(GetModule(), "{var}") == {value}'),
    "int_self": ("Variable on this object equals", [("var", "Variable name", "text"), ("value", "Value", "int")], 'GetLocalInt(OBJECT_SELF, "{var}") == {value}'),
    "quest_state": ("Journal quest at entry (player's variable convention)", [("quest", "Quest tag", "text"), ("entry", "Entry number", "int")],
                    'GetLocalInt(oPC, "{quest}") == {entry}'),
    "gold": ("Player has at least this much gold", [("amount", "Gold", "int")], 'GetGold(oPC) >= {amount}'),
    "level": ("Player level at least", [("lvl", "Level", "int")], 'GetHitDice(oPC) >= {lvl}'),
    "class": ("Player has class", [("cls", "Class", "const:CLASS_TYPE_")], 'GetLevelByClass({cls}, oPC) > 0'),
    "alignment": ("Player alignment (good/evil axis)", [("al", "Alignment", "select:ALIGNMENT_GOOD|ALIGNMENT_NEUTRAL|ALIGNMENT_EVIL")], 'GetAlignmentGoodEvil(oPC) == {al}'),
    "skill": ("Skill check (roll d20 + skill vs DC)", [("skill", "Skill", "const:SKILL_"), ("dc", "DC", "int")], 'GetSkillRank({skill}, oPC) + d20() >= {dc}'),
    "dead": ("Creature with tag is dead", [("tag", "Creature tag", "text")], 'GetIsDead(GetObjectByTag("{tag}"))'),
    "night": ("It is night", [], 'GetIsNight()'),
    "chance": ("Random chance (percent)", [("pct", "Percent", "int")], 'd100() <= {pct}'),
    "not_dm": ("Player is not a DM", [], '!GetIsDM(oPC)'),
    "race": ("Player race", [("race", "Race", "const:RACIAL_TYPE_")], 'GetRacialType(oPC) == {race}'),
    "gender": ("Player gender", [("g", "Gender", "select:GENDER_MALE|GENDER_FEMALE")], 'GetGender(oPC) == {g}'),
    "feat": ("Player has a feat", [("feat", "Feat", "const:FEAT_")], 'GetHasFeat({feat}, oPC)'),
    "spell_effect": ("Player is under a spell", [("spell", "Spell", "const:SPELL_")], 'GetHasSpellEffect({spell}, oPC)'),
    "ability": ("Ability score at least", [("ability", "Ability", "const:ABILITY_"), ("n", "Score", "int")], 'GetAbilityScore(oPC, {ability}) >= {n}'),
    "level_max": ("Player level at most", [("lvl", "Level", "int")], 'GetHitDice(oPC) <= {lvl}'),
    "string_pc": ("Text variable on the player equals", [("var", "Variable name", "text"), ("value", "Text", "text")], 'GetLocalString(oPC, "{var}") == "{value}"'),
    "area_tag": ("Player is in the area with tag", [("tag", "Area tag", "text")], 'GetTag(GetArea(oPC)) == "{tag}"'),
    "in_combat": ("Player is in combat", [], 'GetIsInCombat(oPC)'),
    "day": ("It is day", [], 'GetIsDay()'),
    "campaign_int": ("Campaign database number equals (per player)", [("db", "Database name", "text"), ("var", "Variable", "text"), ("value", "Value", "int")],
                     'GetCampaignInt("{db}", "{var}", oPC) == {value}'),
    "sql_player_eq": ("Player's own database value equals (EE)", [("key", "Key", "text"), ("value", "Value", "text")], 'TkSqlGet(oPC, "{key}") == "{value}"'),
    "item_count": ("Player carries at least N items with a tag", [("tag", "Item tag", "text"), ("n", "How many", "int")], 'TkCountItems(oPC, "{tag}") >= {n}'),
    "token_eq": ("Quest token at stage (this module's token system)", [("slot", "Quest (token name)", "token"), ("value", "Stage (0 = not started; one character)", "stage")],
                 '{getter}(oPC, {slot}) == "{value}"'),
}

NAME_RE = re.compile(r"^[a-z0-9_]{1,16}$")      # a script name (resref): same rule as RESREF_RE
# A line of generated code that declares a variable, e.g. "    object oLock = GetObjectByTag(...);" - such actions are
# wrapped in their own { } block (see generate_script).
DECLARES = re.compile(r"^\s*(?:object|location|int|float|string|effect|itemproperty)\s+\w+\s*[=;]", re.M)

# helper functions some templates call; added above main() only when used
HELPERS = {
    "TkSqlSet": """// Store a value in oOwner's SQLite database (EE). The player's database travels inside the character file.
void TkSqlSet(object oOwner, string sKey, string sValue)
{
    sqlquery q = SqlPrepareQueryObject(oOwner, "CREATE TABLE IF NOT EXISTS tk_kv (k TEXT PRIMARY KEY, v TEXT);");
    SqlStep(q);
    q = SqlPrepareQueryObject(oOwner, "INSERT OR REPLACE INTO tk_kv (k, v) VALUES (@k, @v);");
    SqlBindString(q, "@k", sKey);
    SqlBindString(q, "@v", sValue);
    SqlStep(q);
}""",
    "TkSqlGet": """// Read a value stored with TkSqlSet ("" when there is none).
string TkSqlGet(object oOwner, string sKey)
{
    sqlquery q = SqlPrepareQueryObject(oOwner, "CREATE TABLE IF NOT EXISTS tk_kv (k TEXT PRIMARY KEY, v TEXT);");
    SqlStep(q);
    q = SqlPrepareQueryObject(oOwner, "SELECT v FROM tk_kv WHERE k = @k;");
    SqlBindString(q, "@k", sKey);
    return SqlStep(q) ? SqlGetString(q, 0) : "";
}""",
    "TkCountItems": """// How many items (counting stacks) with this tag the creature carries.
int TkCountItems(object oCreature, string sTag)
{
    int nCount = 0;
    object oItem = GetFirstItemInInventory(oCreature);
    while (GetIsObjectValid(oItem))
    {
        if (GetTag(oItem) == sTag) nCount += GetItemStackSize(oItem);
        oItem = GetNextItemInInventory(oCreature);
    }
    return nCount;
}""",
}
# The "effect_timed" action's menu choice -> the NWScript effect constructor it stands for (fixed strengths chosen
# for the generator, e.g. 50% movement change, 20 temporary hit points).
EFFECT_EXPR = {"Haste": "EffectHaste()", "Slow": "EffectSlow()", "Invisibility": "EffectInvisibility(INVISIBILITY_TYPE_NORMAL)",
               "Sanctuary": "EffectSanctuary(20)", "Paralyze": "EffectParalyze()", "Blindness": "EffectBlindness()",
               "Deafness": "EffectDeaf()", "Regenerate": "EffectRegenerate(2, 6.0)", "Temporary HP": "EffectTemporaryHitpoints(20)",
               "See invisible": "EffectSeeInvisible()", "Ultravision": "EffectUltravision()", "Knockdown": "EffectKnockdown()",
               "Stun": "EffectStunned()", "Daze": "EffectDazed()", "Sleep": "EffectSleep()", "Silence": "EffectSilence()",
               "Faster movement": "EffectMovementSpeedIncrease(50)", "Slower movement": "EffectMovementSpeedDecrease(50)",
               "Petrify": "EffectPetrify()", "Cutscene paralyze": "EffectCutsceneParalyze()"}


# ---------------------------------------------------------------------------
# The installed game's nwscript.nss: every function and constant of YOUR build (EE adds new ones each patch)
# ---------------------------------------------------------------------------
_NWSCRIPT_CACHE = {}            # (nwn_root, nwn_user) -> load_nwscript() result; the file is large and rarely changes
# A constant declaration in nwscript.nss, e.g. "int VFX_IMP_HEALING_S = 266;" -> (type, name, value text).
NSS_CONST_RE = re.compile(r"^\s*(int|float|string|object|vector|location|json)\s+([A-Z][A-Z0-9_]*)\s*=\s*([^;]+);", re.M)
# A function prototype (declared, not defined - it ends in ";"), e.g. "object GetEnteringObject();"
# -> (return type, name, argument list).
NSS_FUNC_RE = re.compile(r"^\s*(void|int|float|string|object|vector|location|effect|itemproperty|talent|event|json|"
                         r"sqlquery|cassowary|action)\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*;", re.M)


def find_nwscript(nwn_root=None, nwn_user=None, db=None):
    """(bytes, where) of nwscript.nss, the copy the game would use: a hak's or the module's own (when db, an analysis
    index, is given - haks in module.ifo order, then the module), then the user override, then the install (its
    ovr/ folders before its key files, see nwnlib.BaseGame). A module or hak can ship its own nwscript.nss (with
    extra constants or string defaults); it wins over the game's as any resource does (nwnlib.source_rank).

    nwn_root: the game install folder; nwn_user: the user folder (Documents/Neverwinter Nights). Any may be None.
    Read-only. Returns (None, None) when no copy is found; an error reading the game's KEY/BIF counts as "not found"."""
    if db is not None:
        row = db.execute("SELECT sc.source, s.kind, s.path FROM scripts sc JOIN files f ON f.id=sc.file_id JOIN sources s "
                         "ON s.id=f.source_id WHERE lower(sc.name)='nwscript' AND s.kind IN ('hak', 'module') AND "
                         f"sc.source IS NOT NULL ORDER BY {n.source_rank_sql()} LIMIT 1").fetchone()
        if row:
            # the index keeps sources decoded with nwnlib.decode_text, so encode_text gives back the exact bytes
            return n.encode_text(row[0]), f"{row[1]} {row[2]}"
    p = os.path.join(nwn_user, "override", "nwscript.nss") if nwn_user else None
    if p and os.path.isfile(p):
        with open(p, "rb") as fh:
            return fh.read(), p
    if nwn_root:
        try:
            data = n.BaseGame(nwn_root).get("nwscript.nss")
            if data:
                return data, f"{nwn_root} (game data)"
        except Exception:  # noqa
            pass
    return None, None


def load_nwscript(nwn_root=None, nwn_user=None):
    """Every constant and engine function of the installed game, from its nwscript.nss (cached per folder pair).

    Returns dict(found, where, constants={NAME: (type, value text)},
                 functions={Name: dict(ret, args, doc)}) - doc is the // comment block above the prototype, max 400
    characters. When nwscript.nss is not found, found=False and both tables are empty. Read-only."""
    key = (nwn_root or "", nwn_user or "")
    if key in _NWSCRIPT_CACHE:
        return _NWSCRIPT_CACHE[key]
    data, where = find_nwscript(nwn_root, nwn_user)
    info = dict(found=bool(data), where=where, constants={}, functions={})
    if data:
        text = n.decode_text(data).replace("\r\n", "\n")
        for m in NSS_CONST_RE.finditer(text):
            info["constants"][m.group(2)] = (m.group(1), m.group(3).strip())
        lines = text.split("\n")
        for m in NSS_FUNC_RE.finditer(text):
            doc = []
            # walk upwards from the line above the prototype while the lines are // comments
            j = text.count("\n", 0, m.start()) - 1
            while j >= 0 and lines[j].strip().startswith("//"):
                doc.insert(0, lines[j].strip()[2:].strip())
                j -= 1
            info["functions"][m.group(2)] = dict(ret=m.group(1), args=m.group(3).strip(), doc=" ".join(doc)[:400])
    _NWSCRIPT_CACHE[key] = info
    return info


def constants_with_prefix(nws, prefix, limit=4000):
    """Sorted names of the constants in `nws` (a load_nwscript() result) that start with `prefix`, e.g. "VFX_IMP_".
    At most `limit` names (they fill a drop-down list)."""
    return sorted(k for k in nws.get("constants", {}) if k.startswith(prefix))[:limit]


COMPILER_BUILTINS = {"OBJECT_SELF", "OBJECT_INVALID", "LOCATION_INVALID", "JSON_NULL", "JSON_FALSE", "JSON_TRUE",
                     "JSON_ARRAY", "JSON_OBJECT", "JSON_STRING"}   # known to the compiler, not listed in nwscript.nss


def verify_script(src, nws, module_libs=None, module_consts=()):
    """Warnings for functions/constants that your game's nwscript.nss doesn't have (typos, or newer/older builds).

    src: NWScript source text; nws: a load_nwscript() result; module_libs: {function name: library script} of
    functions the module's own includes define; module_consts: constant names the module defines.
    Returns a list of plain-English warnings (empty = nothing unknown). A heuristic, not a compiler: only names
    starting with a capital are checked as functions, and only ALL_CAPS names with an _ as constants. /* */ comments
    are not stripped. Read-only."""
    if not nws or not nws.get("found"):
        return ["Your game's nwscript.nss was not found (set the NWN install folder in Settings) - functions and "
                "constants were not checked against your build."]
    code = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)          # strings first (they may contain //)
    code = re.sub(r"//[^\n]*", "", code)
    # functions this script defines itself, e.g. "int TkCountItems(object oC, string sTag) {"
    defined = set(re.findall(r"^\s*\w+\s+(\w+)\s*\([^;{]*\)\s*\{", code, re.M))
    warn = []
    for fn in sorted(set(re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*\(", code))):
        if fn not in nws["functions"] and fn not in defined and fn not in (module_libs or {}):
            warn.append(f"{fn}() is not in your game's nwscript.nss")
    for c in sorted(set(re.findall(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b", code))):
        if c not in nws["constants"] and c not in module_consts and c not in COMPILER_BUILTINS:
            warn.append(f"{c} is not a constant in your game's nwscript.nss")
    return warn


def _fmt(template, values, fields, extra=None):
    """Fill one ACTIONS/CONDITIONS template with the values the builder typed, after checking each value against
    its kind.

    This is the injection guard of the generator: numbers must be numbers, names must be resrefs or constants, and
    free text is escaped and only ever placed inside "..." (see _check_templates), so no value can become code.
    extra: values that are not user fields (the token setter/getter names). Raises ValueError naming the bad field."""
    vals = dict(extra or {})
    for name, label, kind in fields:
        v = str(values.get(name, "")).strip()
        if kind == "int":
            # NWScript int is a signed 32-bit number
            if not re.fullmatch(r"-?[0-9]{1,10}", v or "") or not -2 ** 31 <= int(v) < 2 ** 31:
                raise ValueError(f"'{label}' must be a whole number")
            v = str(int(v))
        elif kind == "stage":
            if not re.fullmatch(r"[0-9A-Za-z]", v or ""):
                raise ValueError(f"'{label}' must be one character - a digit 0-9 or a letter such as D (done) or F "
                                 "(failed): each quest has one character in the token string")
        elif kind == "float":
            if not re.fullmatch(r"-?[0-9]{1,9}(\.[0-9]{1,6})?", v or ""):
                raise ValueError(f"'{label}' must be a number of seconds, e.g. 30 or 7.5")
            v = v if "." in v else v + ".0"             # NWScript needs "30.0", not "30", where a float is expected
        elif kind.startswith("const:"):
            v = v.upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", v):
                raise ValueError(f"'{label}': pick a constant such as {kind[6:]}...")
        elif kind.startswith("select:"):
            opts = kind[7:].split("|")
            if v not in opts:
                raise ValueError(f"'{label}' must be one of: {', '.join(opts)}")
            if name == "effect" and v in EFFECT_EXPR:
                v = EFFECT_EXPR[v]
        elif kind == "token":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|\d+", v):
                raise ValueError(f"'{label}': pick one of this module's quest token names")
        elif kind == "resref":
            v = v.lower()
            if not NAME_RE.match(v):
                raise ValueError(f"'{label}' must be a resource name (letters, digits, _; max 16)")
        else:
            # free text: only ever placed inside "quotes" in a template (checked at start-up), escaped, one line
            if re.search(r"[\r\n\x00]", v):
                raise ValueError(f"'{label}' must be a single line of text")
            v = v.replace("\\", "\\\\").replace('"', '\\"')
        vals[name] = v
    return template.format(**vals)


def _check_templates():
    """Every free-text field must sit inside quotes in its template, so a value can never become code."""
    for table in (ACTIONS, CONDITIONS):
        for key, d in table.items():
            tmpl = d[2]
            for fname, _label, kind in d[1]:
                if kind == "text":
                    if tmpl.count("{" + fname + "}") != tmpl.count('"{' + fname + '}"'):
                        raise AssertionError(f"generator template {key}: text field {fname} used outside quotes")


_check_templates()      # at import: a template mistake stops the toolkit loading rather than producing unsafe scripts


def generate_script(spec, module_libs=None, nwscript=None, adapter=None, module_consts=()):
    """
    Build a complete, commented NWScript from a form: an event, optional conditions, and actions (deterministic).

    spec = {name, event, description, conditions: [{key, values}], actions: [{key, values}],
            condition_logic: 'all'|'any', once: bool, pc_only: bool}
    module_libs = {function_name: library_script} from the analysis (adds #include when used)
    nwscript = a load_nwscript() result; when given, the result is checked with verify_script
    adapter = the module's quest-token helper pair (dict with setter, getter, include, names, unused_names, length),
              needed only for the token_set / token_eq choices
    module_consts = constant names the module defines (not reported as unknown)
    Returns dict(name, source, placement, warnings). Raises ValueError for a bad name, event, key or field value.
    Writes nothing - saving the script is a separate save_text(..., is_new=True).
    A condition event (DlgCondition) produces int StartingConditional(); every other event produces void main().
    """
    name = (spec.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("Script name: letters, digits and _ only, max 16 characters")
    ev = EVENTS.get(spec.get("event"))
    if not ev:
        raise ValueError("Unknown event")
    label, who, get_pc, placement, is_cond = ev
    conds, warnings, includes = [], [], []
    extra = {}
    if adapter:
        extra = dict(setter=adapter["setter"], getter=adapter["getter"])

    def need_adapter(key):
        """Token choices need the module's token helpers: refuse without them, else #include their library."""
        if key in ("token_set", "token_eq"):
            if not adapter:
                raise ValueError("this module has no quest-token system (no Set.../Get... helper pair was found)")
            includes.append(adapter["include"])
    def check_slot(values):
        """The slot must be one of the module's slot names (or a number inside the string)."""
        v = str((values or {}).get("slot", "")).strip()
        if not adapter or not v:
            return
        names, unused, length = set(adapter.get("names") or ()), set(adapter.get("unused_names") or ()), adapter.get("length")
        if v.isdigit():
            if length and int(v) >= int(length):
                raise ValueError(f"slot {v} is past the end of the token string ({length} characters)")
            warnings.append(f"slot {v} is written as a bare number - use its name from {adapter['include']} so it can't be mistyped")
        elif names and v not in names and v not in unused:
            raise ValueError(f"'{v}' is not one of the slot names in {adapter['include']} - pick one from the list "
                             "(slots of another include, e.g. a tutorial token string, need that include's own helpers)")
        elif v in unused:
            warnings.append(f"{v} is named in {adapter['include']} but no script uses it yet - fine for a new quest, "
                            "but make sure nothing else was meant to use that slot")
    for c in spec.get("conditions", []) or []:
        cd = CONDITIONS.get(c.get("key"))
        if not cd:
            raise ValueError(f"unknown condition {c.get('key')}")
        need_adapter(c.get("key"))
        if c.get("key") in ("token_set", "token_eq"):
            check_slot(c.get("values"))
        conds.append("(" + _fmt(cd[2], c.get("values", {}), cd[1], extra) + ")")
    logic = " && " if (spec.get("condition_logic") or "all") == "all" else " || "
    body = []
    once = bool(spec.get("once"))
    pc_only = bool(spec.get("pc_only"))
    for a in spec.get("actions", []) or []:
        ad = ACTIONS.get(a.get("key"))
        if not ad:
            raise ValueError(f"unknown action {a.get('key')}")
        if a["key"] in ("once", "pc_only"):
            once = once or a["key"] == "once"; pc_only = pc_only or a["key"] == "pc_only"
            continue
        need_adapter(a["key"])
        if a["key"] in ("token_set", "token_eq"):
            check_slot(a.get("values"))
        code = _fmt(ad[2], a.get("values", {}), ad[1], extra)
        body.append("    // " + ad[0])
        if DECLARES.search(code):
            # its own { } block, so the same action twice (two "unlock"s, give_item_party + xp_party) doesn't
            # declare the same variable twice
            body.append("    {\n        " + code.replace("\n    ", "\n        ") + "\n    }")
        else:
            body.append("    " + code)
        includes += ad[3]
    desc = (spec.get("description") or "").strip()
    header = ["//::///////////////////////////////////////////////",
              f"//:: {name}",
              "//:: Generated by the NWN Module Toolkit script generator",
              f"//:: Where it goes: {placement}",
              f"//:: Event: {label}  (OBJECT_SELF is {who})"]
    if desc:
        header += ["//::"] + [f"//:: {l}" for l in desc.splitlines()]
    header.append("//::///////////////////////////////////////////////")
    body_text = "\n".join(conds + body)
    # libraries from this module: #include one when the generated code calls a function it defines
    if module_libs:
        includes += [lib for fn, lib in module_libs.items() if re.search(r"\b" + re.escape(fn) + r"\s*\(", body_text)]
    lines = header[:]
    for inc in sorted(set(includes)):
        lines.append(f'#include "{inc}"')
    for hname, htext in HELPERS.items():
        if re.search(r"\b" + hname + r"\s*\(", body_text):
            lines += ["", htext]
    if is_cond:
        expr = logic.join(conds) if conds else "TRUE"
        lines += ["", "int StartingConditional()", "{", f"    {get_pc}"]
        if pc_only:
            lines.append("    if (!GetIsPC(oPC)) return FALSE;")
        lines += [f"    return {expr};", "}"]
        if body:
            warnings.append("Actions are ignored in a condition script - use a separate 'Actions Taken' script.")
    else:
        lines += ["", "void main()", "{", f"    {get_pc}"]
        if pc_only:
            lines.append("    if (!GetIsPC(oPC)) return;   // players only")
        if conds:
            lines.append(f"    if (!({logic.join(conds)})) return;")
        if once:
            # a local variable on the object the script runs on, not a database value: a server restart (or the
            # object being destroyed and respawned) clears it
            lines.append(f'    if (GetLocalInt(OBJECT_SELF, "DONE_{name.upper()}")) return;   // only once')
            lines.append(f'    SetLocalInt(OBJECT_SELF, "DONE_{name.upper()}", TRUE);')
        if body:
            lines.append("")
            lines += body
        else:
            lines.append("    // (no actions chosen)")
        lines.append("}")
    src = "\n".join(lines) + "\n"
    if nwscript is not None:
        warnings += verify_script(src, nwscript, module_libs, module_consts)
    return dict(name=name, source=src, placement=placement, warnings=warnings)


def catalog(nwscript=None, adapter=None, tokens=()):
    """Everything the generator page needs. Constant pickers are filled from YOUR game's nwscript.nss.

    nwscript: a load_nwscript() result (or None); adapter: the module's quest-token helpers (or None);
    tokens: token names to offer. Returns a JSON-ready dict(nwscript, constants, adapter, tokens, events, actions,
    conditions). Read-only."""
    prefixes = sorted({f[2][6:] for d in list(ACTIONS.values()) + list(CONDITIONS.values()) for f in d[1]
                       if f[2].startswith("const:")})
    consts = {p: constants_with_prefix(nwscript or {}, p) for p in prefixes}
    return dict(nwscript=dict(found=bool(nwscript and nwscript.get("found")), where=(nwscript or {}).get("where"),
                              functions=len((nwscript or {}).get("functions", {})), constants=len((nwscript or {}).get("constants", {}))),
                constants=consts, adapter=adapter, tokens=list(tokens),
                events={k: dict(label=v[0], placement=v[3], condition=v[4]) for k, v in EVENTS.items()},
                actions={k: dict(label=v[0], fields=[dict(name=f[0], label=f[1], kind=f[2]) for f in v[1]]) for k, v in ACTIONS.items()},
                conditions={k: dict(label=v[0], fields=[dict(name=f[0], label=f[1], kind=f[2]) for f in v[1]]) for k, v in CONDITIONS.items()})


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "apply":
        # "apply" does not re-analyse: it checks the analysis exists (read-only, so a mistyped folder can't get a new
        # empty index.sqlite) and prints where edits take effect
        a = sys.argv[2]
        if not os.path.isfile(os.path.join(a, "index.sqlite")):
            print(f"error: {a} is not an analysis folder (no index.sqlite)")
            sys.exit(2)
        db = n.sqlite_ro(os.path.join(a, "index.sqlite"))
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        finally:
            db.close()
        print(f"Edits to {meta.get('module_name') or a} are applied in the clean build. Re-analyse after building to "
              "see their effect, or run the dashboard's Build & audit.")
    else:
        # no arguments: print a demonstration script from the generator (a quick self-check)
        print(json.dumps(generate_script(dict(name="demo_use", event="OnUsed", description="Demo",
                                              conditions=[dict(key="has_item", values=dict(tag="KEY_RUSTY"))],
                                              actions=[dict(key="unlock", values=dict(tag="DOOR_CELLAR")),
                                                       dict(key="journal", values=dict(quest="q_rats", entry="2")),
                                                       dict(key="once")]))["source"]))
