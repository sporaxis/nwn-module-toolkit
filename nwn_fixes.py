"""
nwn_fixes.py - a suggested way to resolve each issue ("path to resolution"), in plain words.

    fix_for(issue) -> str      one or two sentences: what to do, where, and how to check it worked
    add_fixes(issues)          sets issue["fix"] on every issue (kept when a module-specific fix is already there)

The advice is deliberately concrete (which file / field / toolset menu) and conservative: when an issue may be a false
alarm (a hak or the base game not loaded, a name built at run time), the first step is always to confirm it.

Why this exists: every reported issue must come with its path to resolution, not only the problem. nwn_analysis calls
add_fixes on the finished issue list, so the Issues page ("How to fix"), issues.csv and nwn_facts all show the same
text.

Pure text: reads nothing and writes nothing (no files, no index). An issue dict is
    dict(severity, category, node, label, detail[, fix])
and the advice is chosen by its `category` (the FIXES table). Some helpers pull names out of `detail`, so they depend
on the wording the producing check uses - e.g. the "... not loaded" suffix that nwn_analysis adds when haks or the base
game were not read. A category with no entry gets a generic "open it and decide" sentence.
"""
from __future__ import annotations

import re

# Text between single quotes: the checks quote the names they report,
# e.g. "Conversation 'cf_guard' not found" -> cf_guard.
_Q = re.compile(r"'([^']+)'")


def _names(detail):
    """Every 'quoted' name in an issue's detail text, in order."""
    return _Q.findall(detail or "")


def _first(detail, default="it"):
    """The first 'quoted' name in the detail, or `default` when there is none."""
    n = _names(detail)
    return n[0] if n else default


def _missing_hint(detail):
    """A 'confirm first' sentence when the detail says haks or the base game were not loaded (else "")."""
    # "not loaded" is the wording of the not_loaded_hint suffix that nwn_analysis adds to "missing" issues.
    if "not loaded" in (detail or ""):
        return ("First load the missing haks / set the NWN install folder in Settings and re-analyse - it may simply "
                "be in a hak or the base game. ")
    return ""


def _missing_script(i):
    d = i.get("detail", "")
    nm = _first(d, "the script")
    return (_missing_hint(d) + f"If '{nm}' really doesn't exist: open the object named in the issue in the toolset and "
            f"clear or correct the event slot (or create {nm}.nss with the Script generator). The Log monitor shows a "
            "'script not found' error when the game hits it.")


def _missing_include(i):
    """Advice for missing_include. Parses the detail nwn_analysis writes, e.g.
    "calls GetToken() which is defined in 'inc_tokens' but does not #include it" -> fn GetToken, lib inc_tokens."""
    d = i.get("detail", "")
    fn = re.search(r"calls (\w+)\(\)", d)
    lib = re.search(r"defined in (?:the module in )?'([^']+)'", d)
    # severity "info" is the variant where an unread (base game / hak) include might define the function
    if i.get("severity") == "info":
        return ("Nothing to do unless the script fails to compile: the function may come from the base-game or hak "
                "include it already uses. Set the NWN install folder (and load the haks) to check it properly.")
    return (f"Add #include \"{lib.group(1) if lib else '<the include>'}\" at the top of the script (or stop calling "
            f"{fn.group(1) + '()' if fn else 'the function'}), then compile it. If two includes define the same function, "
            "include only the one this script's token/data system uses.")


def _missing_blueprint(i):
    d = i.get("detail", "")
    nm = _first(d, "the blueprint")
    return (_missing_hint(d) + f"Otherwise the script, store or encounter asks for '{nm}', which doesn't exist: create a "
            "blueprint with that ResRef, or change the script/store to an existing one (Find & replace finds every use).")


def _missing_conversation(i):
    d = i.get("detail", "")
    nm = _first(d, "the conversation")
    return (_missing_hint(d) + f"Otherwise create the conversation '{nm}' or point the object/script at an existing one "
            "(Find & replace, whole value). Until then the NPC says nothing when clicked.")


def _unknown_tag(i):
    nm = _first(i.get("detail", ""), "the tag")
    return (f"Check the tag spelling (tags are case-sensitive) with Find & replace on '{nm}'. If the object is created "
            "while the game runs, this is fine - tick By design. Otherwise give the object that tag, or fix the "
            "GetObjectByTag call.")


def _tag_case(i):
    """Advice for tag_case; names the tag when the detail starts with "<tag>:" (nwn_varaudit writes it that way)."""
    d = i.get("detail", "")
    m = re.match(r"(\S+):", d)
    return (f"Make the capitals match: either change the script to use the object's tag exactly, or rename the tag "
            f"(Find & replace with 'match capitals' ticked){' for ' + m.group(1) if m else ''}.")


def _variable_case_twin(i):
    return ("Pick one spelling and use it everywhere (Find & replace, whole word, 'match capitals' ticked). Beware: "
            "players' saved values stay under the old spelling.")


def _variable_type(i):
    return ("Read the variable with the same type it is set with (GetLocalString for a string, GetLocalInt for an "
            "int), or change the setter - NWN keeps each type separate, so the read currently gets 0/\"\".")


def _wrong_script_type(i):
    return ("A conversation condition must be a script with int StartingConditional(). Put this script in the "
            "action slot instead, or rewrite it as a condition (Script generator: 'Conversation: Text appears when').")


def _creature_ai(i):
    return ("Set the creature's combat events to the default AI scripts (nw_c2_default5 OnPhysicalAttacked, "
            "nw_c2_default6 OnDamaged, nw_c2_default2 OnPerception - or your AI package's), unless it is a statue or "
            "prop that should not fight (then tick By design).")


def _hak_not_loaded(i):
    nm = _first(i.get("detail", ""), "the hak")
    return (f"Point the analysis at the folder holding {nm} (Analyse > haks, or the NWN user folder in Settings) and "
            "re-analyse. Until then, results about things the hak provides are only 'Review'.")


def _area_tag(i):
    return ("Give each area its own unique tag in the toolset (Area Properties > Basic > Tag), then fix transitions and "
            "scripts that used the old tag (Find & replace).")


def _perf_inventory(i):
    return ("Split the inventory across several containers/stores or spawn the items when a player opens it. Stores: "
            "trim the list or use unlimited-stock items. Each item is an object the server keeps in memory.")


def _perf_area_hb(i):
    return ("Replace per-object heartbeats with one area heartbeat or trigger/OnEnter-driven logic, or disable them "
            "while no player is in the area (check GetFirstPC / area player count).")


def _perf_heavy_hb(i):
    return ("Avoid walking all objects every 6 seconds: cache what you need on OnEnter/OnSpawn, use a trigger, or "
            "run the loop only when a player is present.")


def _perf_spawn(i):
    return ("Spawn creatures when players arrive (encounters or an OnEnter spawner) and despawn them when the area is "
            "empty, instead of placing them all in the toolset.")


def _perf_static(i):
    return ("In the toolset tick Static on these placeables (Properties > Basic) - but NOT on ones a script finds by "
            "walking the area (visual-effect or lighting scripts) or ones with variables; check the Performance page list.")


# category -> function(issue dict) -> advice text. Categories come from the index pass, nwn_analysis and the check
# modules it runs (nwn_lints, nwn_varaudit, nwn_perf, nwn_factions, nwn_database, nwn_ncs, the hak catalogue).
FIXES = {
    "missing_script": _missing_script,
    "missing_include": _missing_include,
    "missing_blueprint": _missing_blueprint,
    "missing_conversation": _missing_conversation,
    "missing_quest": lambda i: ("Add the quest to the journal (Tools > Journal Editor) with this tag, or remove the "
                                "AddJournalQuestEntry call. If the script is a BioWare default (nw_*), it is harmless."),
    "missing_creature_item": lambda i: (_missing_hint(i.get("detail")) + "Otherwise give the creature an existing "
                                        "hide/weapon blueprint (Inventory > equip) or recreate the missing blueprint."),
    "missing_head_model": lambda i: ("Load the hak that has the head model, or pick another head for the creature "
                                     "(Appearance > Head)."),
    "unknown_tag": _unknown_tag,
    "tag_case": _tag_case,
    "variable_case_twin": _variable_case_twin,
    "variable_type_mismatch": _variable_type,
    "wrong_script_type": _wrong_script_type,
    "creature_ai": _creature_ai,
    "hak_not_loaded": _hak_not_loaded,
    "tlk_not_found": lambda i: ("Copy the custom .tlk into Documents/Neverwinter Nights/tlk (or set the NWN user "
                                "folder in Settings) and re-analyse, so names stored as talk-table numbers can be checked."),
    "base_game_not_found": lambda i: ("Settings > NWN install folder: choose the folder that contains 'data' (e.g. "
                                      ".../Beamdog Library/00785), then re-analyse."),
    "module_incomplete": lambda i: ("Re-save the module from the toolset (or re-export the .mod) and analyse again. "
                                    "Until then nothing is offered for deletion."),
    "area_tag": _area_tag,
    "name_too_long": lambda i: ("Rename the resource to 16 characters or fewer (toolset, or Find & replace on its file "
                                "name) - an empty/unnamed entry is dropped automatically by the build."),
    "unsafe_name": lambda i: ("Nothing to do in most cases: the entry is ignored by the game. Re-saving the module "
                              "from the toolset removes it."),
    "compiled_without_source": lambda i: ("Find the script's source (.nss) and add it to the module, or accept it as "
                                          "by design - without source it can't be reviewed or changed."),
    "not_compiled": lambda i: ("Compile the script (toolset Build > Compile, or set the official compiler in Settings "
                               "and use Validate) - until then the game runs no/old code for it."),
    "parse_error": lambda i: "Open the file in the toolset and re-save it; if it still fails, it is damaged - restore it from a backup.",
    "include_cycle": lambda i: "Remove one #include from the loop (includes must not include each other).",
    "orphan_library": lambda i: ("Nothing uses it: keep it if a hak or future script needs it, otherwise it can go "
                                 "(see Safe to delete)."),
    "resource_conflict": lambda i: ("Several sources provide this resource - the game uses the first in load order "
                                    "(first-listed hak > module > your override folder > base game). Keep one, or "
                                    "make sure the winner is the one you want."),
    "broken_strref": lambda i: ("Type the text into the field in the toolset (so it no longer depends on the talk "
                                "table), or add the string to the custom .tlk."),
    "resref_case": lambda i: ("Rename the blueprint to lower case (Find & replace, file name), or compare with "
                              "GetStringLowerCase(GetResRef(o)) in scripts."),
    "portrait_prefix": lambda i: "Use a portrait ResRef that starts with po_ (the game adds the size letter).",
    "portrait_files": lambda i: "Add the po_<name>h/l/m/s/t pictures to a hak, or remove the portraits.2da row.",
    "include_depth": lambda i: "Flatten the #include chain (move shared functions into one include).",
    "identifier_load": lambda i: "Split the big include library so each script includes only what it uses.",
    "hak_size": lambda i: "Split the hak into two (the Hak editor can move files into a new hak).",
    "2da_hak_vs_hak": lambda i: ("Decide which hak's version should win and remove or merge the other copy (Hak editor "
                                 "> 2da editor shows both)."),
    "2da_override": lambda i: "Remove the override copy or merge its rows into the hak's version.",
    "2da_stale": lambda i: ("Update the stale 2da copy to the newer base-game columns/rows (Hak editor > 2da editor), "
                            "or remove it if you don't change anything in it."),
    "perf_inventory": _perf_inventory,
    "perf_area_heartbeats": _perf_area_hb,
    "perf_heavy_heartbeat": _perf_heavy_hb,
    "perf_spawn_density": _perf_spawn,
    "perf_static_candidates": _perf_static,
    "perf_empty_heartbeat": lambda i: "Clear the heartbeat event slot - an empty script still runs every 6 seconds.",
    "perf_delay_in_loop": lambda i: "Queue one DelayCommand that handles all objects, or spread the work over a few heartbeats.",
    "perf_self_reschedule": lambda i: "Make sure the rescheduling loop stops (a condition or a counter), or use a heartbeat.",
    "base_game_script": lambda i: "Nothing to do - the game supplies it.",
    "blueprint_not_in_module": lambda i: "Nothing to do - placed copies keep their own data.",
    "non_game_files": lambda i: "Nothing to do - files the game doesn't use are left out of the clean build.",
    "duplicate_entry": lambda i: ("Keep one copy: open the module in the toolset and re-save it, or remove the extra "
                                  "entry with an ERF tool - which copy the game loads is not defined."),
    "db_read_never_written": lambda i: ("Check the variable name against the names written to that database (Database "
                                        "page), or add the SetCampaign... call that should store it."),
    "db_type_mismatch": lambda i: "Use the Get/SetCampaign function of the same type for both the write and the read.",
    "faction_invalid": lambda i: ("Open the object in the toolset and pick a faction from the list (Properties > Basic "
                                  "> Faction) - the number it has doesn't exist in this module's faction table."),
    "faction_holder_missing": lambda i: ("Place a creature with that tag in a hidden area (the usual 'faction holder'), "
                                         "or fix the tag in the script - otherwise ChangeFaction finds nothing."),
    "faction_unused": lambda i: ("Nothing uses this faction: keep it if a DM or future content will, otherwise delete it "
                                 "in the toolset (Tools > Faction Editor) to keep the table readable."),
    "lint_failed": lambda i: "A check could not run - re-analyse; if it keeps happening, report it.",
    "ncs_damaged": lambda i: ("Recompile the script (toolset Build > Compile, or the toolkit's compile check). If there "
                              "is no .nss source, get a good copy of the .ncs from a backup."),
    "ncs_stale": lambda i: ("Compare first: Script panel > Show compiled code shows what the game runs now. If that "
                            "text is old, recompile the script; if it is right, the .ncs may have been compiled against "
                            "a hak or a nwscript.nss this analysis didn't load - then leave it."),
    "2da_row_label": lambda i: ("Renumber the first column to match the line positions (2da editor), then check anything "
                                "that counted by the old labels."),
    "symlink_skipped": lambda i: ("Copy the real files into the module folder if they belong to it; links are never "
                                  "followed."),
    "area_without_are": lambda i: ("Delete the leftover .git/.gic files (Safe to delete lists the area as Review: "
                                   "tick it there, or remove them from the module folder / nasher project), unless you "
                                   "meant to keep the area - then restore its .are from a backup."),
    "nasher_not_compiled": lambda i: ("Nothing to fix: nasher compiles the scripts when it packs. For a clean .mod "
                                      "from Build & audit, set the official compiler (nwn_script_comp) in Settings - "
                                      "the build then compiles them - or use Save as nasher project and nasher pack."),
    "json_unreadable": lambda i: ("Open the file named above in your nasher project and fix it (or unpack that "
                                  "resource again with nasher), then press Analyse again. Until then the analysis "
                                  "and any build leave it out."),
    "json_duplicate_name": lambda i: ("Keep one copy: delete or rename the other in your nasher project (nasher pack "
                                      "would also put only one into the .mod), then press Analyse again."),
}


def fix_for(issue):
    """The suggested fix for one issue.

    issue: an issue dict (only `category`, `severity` and `detail` are read). Returns one or two plain-English
    sentences. Never raises: if a category's helper fails, the generic sentence is returned instead."""
    fn = FIXES.get(issue.get("category", ""))
    if fn:
        try:
            return fn(issue)
        except Exception:  # noqa - advice must never break the report
            pass
    cat = (issue.get("category") or "").replace("_", " ")
    return f"Open the item (click the row) to see what uses it, then decide: fix it, or tick By design if '{cat}' is intended."


def add_fixes(issues):
    """Set issue["fix"] on every issue that has no fix yet, in place.

    issues: list of issue dicts. A fix that is already there (a check that wrote a module-specific one, such as
    nwn_database) is kept. Returns the same list, for convenience. Never raises."""
    for i in issues:
        if not i.get("fix"):
            i["fix"] = fix_for(i)
    return issues
