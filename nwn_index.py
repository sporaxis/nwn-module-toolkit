"""
nwn_index.py - Phase 1: read every file in a NWN:EE module and build a searchable
SQLite index plus a dependency graph.

Why this exists
---------------
Every later step (nwn_analysis, impact, duplicates, safe-delete, build, audit, dashboard, nwn_facts) asks questions
such as "who uses script X?" or "which hak provides file Y?". Parsing the binary module files for every question
would be slow, so this module reads everything once and stores what it found in one SQLite file.

Usage (normally run through nwn_analyse.py):
    python nwn_index.py <module folder or .mod> [--hak X.hak ...] [--override DIR]
                        [--tlk dialog.tlk] [--out OUTPUT_DIR] [--no-json]

Read-only: nothing in the module folder is modified. The module, its haks, override folders, talk tables and the
game install are only opened for reading. Everything is written inside OUTPUT_DIR.

Outputs (in OUTPUT_DIR):
    index.sqlite        every file, every field, objects, scripts, quests, 2da rows,
                        and the dependency graph (nodes + edges). A fresh run deletes and recreates it.
    index.state         checkpoint of a time-boxed run (see "Checkpoint / resume"); removed when the index finishes
    json/               each GFF file as nwn_gff-compatible JSON (for reading/diffing); when several sources carry
                        the same file, the copy the game uses (first-listed hak > module > override)
    conversations/      each .dlg rendered as a readable Markdown tree (same rule for copies)

Main entry points
-----------------
    run_index(module_path, haks, overrides, ...)  -> OUTPUT_DIR when finished, None when checkpointed (call again)
    Indexer                                       the reader itself (one per run)
    find_haks / find_tlks / default_hak_dirs      locate the haks and talk tables the module asks for
    CARRIER_KINDS / REFERENCE_KINDS / SOFT_KINDS  edge kind groups (the first two are used by nwn_analysis)

Sources and priority
--------------------
Each folder or archive that is read becomes one row of `sources`:
    kind "module"    priority 0        the module folder or .mod
    kind "hak"       priority 1, 2 ... in module.ifo order; haks passed with --hak that module.ifo does not list
                                       come after the listed ones
    kind "override"  priority 100 ...  override folders (by default <NWN user folder>/override when it has files)
Every copy of a file is indexed, even when several sources have a file of the same name; such clashes are listed in
`conflicts`. The priority number is the loading position, NOT the precedence. The game uses
haks (first listed wins) > module > user override > base game (nwnlib.source_rank, which every reader that needs the
winning copy uses). The base game is not indexed file by file: only its resource names go into `base_resources`, so
a reference to a base-game script can be told apart from a missing one.

SQLite schema (index.sqlite)
----------------------------
    meta(key, value)          run settings. Keys written here: module_path, module_name, indexed_at, tlk_custom_name,
                              custom_tlk, haks (JSON list of loaded hak paths in load order), haks_missing (JSON list
                              of names), nwn_root, nwn_user, tlk / tlk_base (talk table paths), base_index (number of
                              base-game names), skipped_files (JSON), tagbased_prefix_unknown (JSON: "script line N"
                              where a tag-based script prefix is set from a non-literal), dynamic_creates (JSON:
                              [script, line, blueprint ext or "*", call] for Create* calls whose blueprint name is a
                              variable or a function result). Later phases may add keys of their own.
    sources(id, path, kind, priority)    one row per folder/archive read (see above)
    files(id, source_id, relpath, resref, ext, kind, size, sha256, content_hash, status, error, node)
                              one row per file in every source. relpath: path inside the folder, or the ERF entry
                              name. resref: name without extension, lower case. kind: readable type name.
                              sha256: of the raw bytes. content_hash: a hash that ignores identity, for duplicate
                              detection (GFF: the JSON without TemplateResRef/Comment/PaletteID/ResRef; .nss: the code
                              without comments and with whitespace collapsed; .mdl: the bytes with the model's own name
                              masked, see index_mdl; NULL for other types).
                              status: 'ok' or 'error' (error holds the reason). node: the file's graph node id.
    fields(file_id, path, label, type, value)
                              every leaf field of every GFF file. path: e.g. "Creature List[3]/ScriptHeartbeat"
                              (list items as [i]); at the top level path equals label. type: GFF field type name.
                              value: text form (localised strings resolved through the talk tables, VOID data as hex).
    objects(id, file_id, node, class, is_blueprint, resref, template, tag, name, area, container, path, x, y, z)
                              one row per blueprint (is_blueprint=1, resref = template = its own name) and per placed
                              instance in an area .git, including item copies nested in inventories and stores
                              (is_blueprint=0, template = TemplateResRef, area = area resref, container = node of the
                              area or of the object holding it, path = GFF path inside the .git, x/y/z = position).
    scripts(file_id, name, has_main, has_sc, lines, norm_hash, has_ncs, source, header_comment, functions_defined,
            calls)            one row per .nss. has_main / has_sc: defines main() / StartingConditional().
                              norm_hash: same as files.content_hash. has_ncs: a .ncs of the same name was loaded from
                              any source. source: full text. header_comment: see header_comment().
                              functions_defined: comma list. calls: JSON {Name: count} of capitalised names followed
                              by "(" (engine and module functions).
    script_literals(script, line, func, literal, kind)
                              every string literal in every script, the innermost call it is an argument of, and how
                              resolve_literals classified it: script / tag / blueprint / quest / conversation / 2da /
                              var_set / var_get, a guess ending in "?" ("tag?", "script?" ...) when it only matched a
                              known name, or NULL. script is the resref, without the "script:" prefix.
    concat_literals(script, line, func, literal)
                              string pieces used to build names at run time (ExecuteScript("x_" + n)). script holds a
                              script resref, or the owner's node id / "module" for VarTable values.
    quests(tag, name, file_id, entries) / quest_entries(tag, entry_id, text, is_end)   journal (.jrl) categories
    conversations(file_id, name, entries, replies, starts, words)   size of each .dlg
    twoda(file_id, name, row, col, value, label)
                              every cell of every 2da ("****" stored as NULL). row: the line's 0-based position
                              among the data lines - the number the game uses; label: the first column as written
                              (for people only - BioWare's 2DA doc: a misnumbered row still gets its position)
    models(file_id, name, format, supermodel, textures)   what nwnlib.scan_mdl found (textures as a comma list)
    equips(owner, item, slot, full_copy)
                              items worn by a creature. owner: node id. item: item resref. slot: the GFF struct id of
                              the equipped entry, which is the equipment slot bit (nwn_analysis.SLOT_NAMES).
                              full_copy: 1 when the creature carries a full item struct, 0 when it names a blueprint.
    strrefs(file_id, path, strref, custom, has_text, status)
                              every localised string that points into a talk table. custom: 1 for the custom .tlk
                              (strref >= 0x01000000). has_text: the file also carries its own text. status: from
                              nwnlib.TlkSet.status ('ok', 'custom-missing', 'no-custom-tlk', 'base-missing'...).
    nodes(node, type, name, label, file_id, in_module)
                              the graph's nodes. in_module = 1 when the thing exists in a loaded source (module, hak or
                              override); 0 for a reference to something that was not found (may be base game).
    edges(src, dst, kind, via)   "src uses dst", with two exceptions that point the other way: tag_provider
                              (tag -> the object that carries it) and var_set (variable -> the script or object that
                              sets it). via: where the link was found - a GFF field path, "line N" of a script, or a
                              short description. The kinds are listed at CARRIER_KINDS below.
    tokens(node, token)       scratch table for name scanning; emptied by link_names()
    issues(severity, category, node, detail)   problems found while reading ('error' / 'warning' / 'info')
    conflicts(resource, sources)   "name.ext" found in several sources, e.g. "module | hak:cep2_top"
    base_resources(name)      every "resref.ext" the base game provides (empty when the install was not found)

Graph node ids
--------------
    script:<resref>        a script (.nss and/or .ncs)          bp:<resref>.<ext>    a blueprint (.uti, .utc, ...)
    area:<resref>          an area (.are/.git/.gic together)    dlg:<resref>         a conversation
    module                 module.ifo                           2da:<resref>         a 2da table
    model:<resref>         a model (.mdl)                       hak:<name>           a hak named in module.ifo
    file:<resref>.<ext>    any other GFF file (.jrl, .itp ...)  asset:<resref>.<ext> anything else (textures ...)
    inst:<area>:<path>     a placed object, path = its GFF list path in the .git, e.g. inst:town:Door List[2]
    tag:<Tag>              an object tag (case kept: tags are case-sensitive in NWScript)
    quest:<tag>            a journal category      var:<name>   a local/campaign variable      set:<name>  a tileset
Resrefs are lower-cased; tags, quest tags and variable names keep their case.

How GFF fields become edges
---------------------------
    script fields (labels On*, Script*, Mod_On*; EndConversation...)  -> event_script / module_event / dlg_script
    Conversation                                                      -> uses_conversation
    KeyName / LinkedTo (doors, placeables, triggers)                   -> key_tag / transition_tag
    InventoryRes / EquippedRes, nested ItemList / StoreList items      -> inventory
    encounter CreatureList ResRef                                      -> spawns
    instance TemplateResRef, area Tileset, model supermodel            -> template
    area .git instance lists                                           -> contains (area -> instance)
    Tag                                                                -> tag_provider (tag -> the object that has it)
    dialogue Quest / Speaker                                           -> quest_ref / speaker_tag
    module.ifo Mod_Area_list, Mod_Entry_Area, Mod_HakList              -> module_area / hak
String values that might be names (VarTable variables, conversation script parameters, script literals) are collected
first and linked in resolve_literals() once every file is known. Last, link_names() adds a conservative name_ref
edge wherever a file's content mentions another file's resref, and a companion edge from a model, texture or
material to the same-named files it needs (walkmesh, texture, .txi, .mtr; see COMPANIONS).

Checkpoint / resume
-------------------
A remote sandbox may kill any command after 180 s. With time_budget, run_index stops cleanly between two files when
the budget is spent: buffered rows are written and committed to index.sqlite and the in-memory state (graph,
pending literals, lookup tables) is written to index.state as JSON. Calling run_index again with the same arguments
resumes from the next file. A state file in any other format (an older toolkit's pickle, or a damaged file) is refused
with a message, never loaded: index.state may come with an analysis folder copied from another PC. Post-processing
(linking, validation, writing the graph) runs only when at least 20 s
are left; otherwise it runs on the next call.

Limits
------
Binary (compiled) models are only scanned for names. References to 2da rows by number are not graph edges here.
Names found by scanning text are matches, not proof of use: they make files look used (safe), never unused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import nwnlib as n
import nwn_progress

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, priority INTEGER);
CREATE TABLE files(id INTEGER PRIMARY KEY, source_id INTEGER, relpath TEXT, resref TEXT,
    ext TEXT, kind TEXT, size INTEGER, sha256 TEXT, content_hash TEXT, status TEXT, error TEXT,
    node TEXT);
CREATE TABLE fields(file_id INTEGER, path TEXT, label TEXT, type TEXT, value TEXT);
CREATE TABLE objects(id INTEGER PRIMARY KEY, file_id INTEGER, node TEXT, class TEXT,
    is_blueprint INTEGER, resref TEXT, template TEXT, tag TEXT, name TEXT, area TEXT,
    container TEXT, path TEXT, x REAL, y REAL, z REAL);
CREATE TABLE scripts(file_id INTEGER, name TEXT, has_main INTEGER, has_sc INTEGER,
    lines INTEGER, norm_hash TEXT, has_ncs INTEGER, source TEXT, header_comment TEXT,
    functions_defined TEXT, calls TEXT);
CREATE TABLE script_literals(script TEXT, line INTEGER, func TEXT, literal TEXT, kind TEXT);
CREATE TABLE concat_literals(script TEXT, line INTEGER, func TEXT, literal TEXT);
CREATE TABLE quests(tag TEXT, name TEXT, file_id INTEGER, entries INTEGER);
CREATE TABLE quest_entries(tag TEXT, entry_id INTEGER, text TEXT, is_end INTEGER);
CREATE TABLE conversations(file_id INTEGER, name TEXT, entries INTEGER, replies INTEGER,
    starts INTEGER, words INTEGER);
CREATE TABLE twoda(file_id INTEGER, name TEXT, row TEXT, col TEXT, value TEXT, label TEXT);
CREATE TABLE models(file_id INTEGER, name TEXT, format TEXT, supermodel TEXT, textures TEXT);
CREATE TABLE equips(owner TEXT, item TEXT, slot INTEGER, full_copy INTEGER);
CREATE TABLE strrefs(file_id INTEGER, path TEXT, strref INTEGER, custom INTEGER, has_text INTEGER, status TEXT);
CREATE TABLE nodes(node TEXT PRIMARY KEY, type TEXT, name TEXT, label TEXT,
    file_id INTEGER, in_module INTEGER);
CREATE TABLE edges(src TEXT, dst TEXT, kind TEXT, via TEXT);
CREATE TABLE tokens(node TEXT, token TEXT);
CREATE TABLE issues(severity TEXT, category TEXT, node TEXT, detail TEXT);
CREATE TABLE conflicts(resource TEXT, sources TEXT);
"""

INDEXES = """
CREATE INDEX ix_fields_file ON fields(file_id);
CREATE INDEX ix_fields_label ON fields(label);
CREATE INDEX ix_objects_node ON objects(node);
CREATE INDEX ix_objects_tag ON objects(tag);
CREATE INDEX ix_objects_tpl ON objects(template);
CREATE INDEX ix_edges_src ON edges(src);
CREATE INDEX ix_edges_dst ON edges(dst);
CREATE INDEX ix_lit_script ON script_literals(script);
CREATE INDEX ix_files_res ON files(resref, ext);
"""

# ---- Edge kinds -----------------------------------------------------------
# "src uses dst". CARRIER edges pass impact on transitively (if dst's behaviour
# changes, src's behaviour changes too). Other edges are references that only
# matter when the referenced thing itself is renamed/removed/re-tagged.
#   include            script #includes script               execute_script    ExecuteScript / VarTable naming a script
#   event_script       object field (OnX / ScriptX) -> script dlg_script        dialogue node action / condition script
#   uses_conversation  object's Conversation field           module_event      module.ifo Mod_OnX -> script
#   contains           area -> placed instance, holder -> item inside it
#   inventory          object carries an item blueprint      spawns            encounter / CreateObject -> blueprint
#   tag_based_script   item -> script named after its tag (EE tag-based scripting, see post_process)
#   template           instance -> its blueprint; area -> tileset; model -> supermodel; quest -> journal file
#   name_ref           the file mentions dst's resref (text scan)   companion   model/texture -> same-named helper file
#   *_tag / tag_ref    a tag named in a field or script call
#   literal_match      a string (script literal, VarTable value) equal to a known tag or blueprint name
#   quest_ref, conversation_ref, 2da_ref, module_area, hak   named by a field or a script call
# nwn_analysis imports these sets to trace impact. "tag_provider" (tag -> object) is in none of them: nwn_analysis
# handles it separately and follows it only for the object's own tag.
CARRIER_KINDS = {
    "include", "execute_script", "event_script", "dlg_script", "uses_conversation",
    "contains", "inventory", "tag_based_script", "module_event", "spawns",
}
REFERENCE_KINDS = {
    "name_ref", "companion", "template", "tag_ref", "quest_ref", "key_tag", "transition_tag", "speaker_tag",
    "conversation_ref", "module_area", "hak", "2da_ref", "literal_match",
}
SOFT_KINDS = {"var_read", "var_set"}  # coupling through local/campaign variables

# ---- GFF label knowledge ---------------------------------------------------
# The instance lists of an area's .git file: list label -> (object class, blueprint extension). The labels (with
# their spaces and the bare "List" for items) are as in BioWare's GIT format documentation.
GIT_LIST_CLASS = {
    "Creature List": ("creature", "utc"), "Door List": ("door", "utd"),
    "Placeable List": ("placeable", "utp"), "List": ("item", "uti"),
    "StoreList": ("store", "utm"), "SoundList": ("sound", "uts"),
    "TriggerList": ("trigger", "utt"), "WaypointList": ("waypoint", "utw"),
    "Encounter List": ("encounter", "ute"),
}
EXT_CLASS = {"uti": "item", "utc": "creature", "utp": "placeable", "utd": "door",
             "ute": "encounter", "utm": "store", "uts": "sound", "utt": "trigger",
             "utw": "waypoint"}
BLUEPRINT_EXTS = set(EXT_CLASS)
# A ResRef field whose label starts with On / Script / Mod_On holds an event script: "OnHeartbeat" (areas, doors,
# placeables), "ScriptHeartbeat" (creatures), "Mod_OnModLoad" (module.ifo).
SCRIPT_LABEL_RE = re.compile(r"^(On|Script|Mod_On)", re.I)
# Script fields inside a .dlg: node action (Script), link condition (Active), and the two end-of-conversation scripts.
DLG_SCRIPT_LABELS = {"Script", "Active", "EndConversation", "EndConverAbort"}

# ---- NWScript function knowledge ------------------------------------------
# Engine function -> what its string argument names. A string literal passed to one of these becomes a typed edge
# in resolve_literals ("script" -> execute_script, "tag" -> tag_ref, "blueprint" -> spawns, ...).
FUNC_KIND = {}
for f in ("ExecuteScript", "SetEventScript", "AddScriptToEvent"):
    FUNC_KIND[f] = "script"
for f in ("GetObjectByTag", "GetNearestObjectByTag", "GetItemPossessedBy", "GetWaypointByTag",
          "GetFirstObjectByTag", "GetNearestObjectByTagAndType"):
    FUNC_KIND[f] = "tag"
for f in ("CreateObject", "CreateItemOnObject", "CreateItemOnObjectVoid", "CreateObjectVoid"):
    FUNC_KIND[f] = "blueprint"
for f in ("AddJournalQuestEntry", "RemoveJournalQuestEntry", "GetJournalQuestExperience"):
    FUNC_KIND[f] = "quest"
for f in ("ActionStartConversation", "BeginConversation", "StartConversation"):
    FUNC_KIND[f] = "conversation"
for f in ("Get2DAString",):
    FUNC_KIND[f] = "2da"
# Variable writers / readers by name pattern: SetLocalInt, SetCampaignString, DeleteLocalObject ... / GetLocalInt ...
# The literal passed to them is the variable name (a var: node).
VAR_SET_RE = re.compile(r"^(SetLocal|SetCampaign|DeleteLocal|DeleteCampaign)\w*$")
VAR_GET_RE = re.compile(r"^(GetLocal|GetCampaign)\w*$")

# Files whose own content may mention other files by name (scanned for resref tokens).
SCAN_EXTS = n.GFF_EXTENSIONS | {"2da", "mdl", "set", "txi", "nss", "ncs", "ssf", "mtr", "shd",
                                "txt", "ini", "ltr", "wok", "pwk", "dwk"}
# Companion files: if the first is kept, the second must be kept too.
COMPANIONS = {"mdl": ("wok", "pwk", "dwk", "tga", "dds", "plt", "txi", "mtr"), "tga": ("txi", "mtr"), "dds": ("txi", "mtr"),
              "plt": ("txi",), "mtr": ("tga", "dds", "txi")}   # a model's own-name texture/material is used by it
TOKEN_RE = re.compile(rb"[A-Za-z0-9_-]{3,32}")   # "-" occurs in real resrefs (tcge0_msc-fence)

# Name prefixes of BioWare / common-package scripts. Used only when the base game's KEY index could not be read
# (is_base_script): a missing script with one of these prefixes is treated as base game, not reported as missing.
BASE_GAME_PREFIXES = ("nw_", "x0_", "x1_", "x2_", "x3_", "nw_c2_", "nw_ch_", "nw_g0_",
                      "nw_o2_", "nw_s0_", "nw_s1_", "nw_s2_", "nw_s3_", "gen_", "k_", "cnr_", "plc_")


def sha256(b: bytes) -> str:
    """Hex SHA-256 of b (used for files.sha256 and the content hashes)."""
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# NWScript lexer: strip comments, find literals with their enclosing call
# ---------------------------------------------------------------------------
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def lex_nss(src: str):
    """Returns (code_without_comments, includes, literals[(line, func, literal, concatenated)]).

    A small hand-written scanner, not a full parser. src: the script text. code_without_comments keeps every
    newline, so line numbers in it match the original file. includes: [(line, name lower case)].
    literals: each "..." string with its line, the innermost function call it appears in (or None), and whether it
    is glued to something with "+" (then it is only a piece of a name). Never raises."""
    out = []
    includes = []
    literals = []
    stack: List[Optional[str]] = []
    i, line, L = 0, 1, len(src)
    last_ident = None   # identifier just before a "(" names the call that is being opened
    last_sig = ""  # last non-blank character emitted (for detecting "..." + / + "...")
    # stack: one entry per open "(" - the function name before it, or None for a plain bracket
    while i < L:
        c = src[i]
        if c == "\n":
            line += 1; out.append(c); i += 1; continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = L if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)       # an unclosed block comment runs to the end of the file
            seg = src[i:(L if j < 0 else j + 2)]
            # the comment is dropped but its newlines are kept so later line numbers stay right
            line += seg.count("\n"); out.append("\n" * seg.count("\n"))
            i = L if j < 0 else j + 2
            continue
        if c == "#":
            j = src.find("\n", i)
            directive = src[i:(L if j < 0 else j)]
            m = re.match(r'#\s*include\s+"([^"]+)"', directive)    # #include "x0_i0_spells" -> x0_i0_spells
            if m:
                includes.append((line, m.group(1).lower()))
            out.append(directive)
            i = L if j < 0 else j
            continue
        if c == '"':
            # a string ends at the closing quote or, if unclosed, at the end of the line; escapes (\" \n) are kept
            # as written so they don't end the string early
            j = i + 1
            buf = []
            while j < L and src[j] != '"' and src[j] != "\n":
                if src[j] == "\\" and j + 1 < L:
                    buf.append(src[j:j + 2]); j += 2; continue
                buf.append(src[j]); j += 1
            lit = "".join(buf)
            # Only the innermost call counts, e.g. SetLocalInt(o, "X", ...)
            func = stack[-1] if stack else None
            prev = last_sig
            k = j + 1
            while k < L and src[k] in " \t":
                k += 1
            # "+" just before the literal, or just after it on the same line: it is glued into a longer name
            concat = prev == "+" or (k < L and src[k] == "+")
            literals.append((line, func, lit, concat))
            out.append('"' + lit + '"')
            i = j + 1
            last_ident = None
            last_sig = '"'
            continue
        m = _IDENT.match(src, i)
        if m:
            last_ident = m.group(0)
            out.append(last_ident)
            i = m.end()
            last_sig = last_ident[-1]
            continue
        if c == "(":
            stack.append(last_ident)
        elif c == ")":
            if stack:
                stack.pop()
        if not c.isspace():
            last_ident = None
            last_sig = c
        out.append(c)
        i += 1
    return "".join(out), includes, literals


# Header-comment lines that say nothing about what the script does: generator banners (Lilac Soul, Script Wizard),
# vault links, author/date/copyright lines, empty "Name:" lines and blank lines.
BOILERPLATE_RE = re.compile(r"lilac soul|script generator|nwvault|neverwintervault|script wizard|"
                            r"^created by\b|^created on\b|^copyright|^\(c\)|^file:\s|^name:\s*$|^\s*$", re.I)
# "Put this script OnEnter" / "Put this on the Action Taken event" -> the event name (group 1), kept as a hint
# of where the script is meant to be attached.
PLACEMENT_RE = re.compile(r"put this (?:script )?(?:in the |on the |under the |in |on )?"
                          r"(on\w+|[\w ]*?(?:action taken|text appears when|event|handle|slot)\b[\w ]*)", re.I)


def header_comment(src: str) -> str:
    """Leading comment of a script (authors describe it there). Skips Lilac Soul / BioWare
    generator banners and keeps a 'Put this script OnX' placement hint.

    src: script text. Returns one line of at most 600 characters, starting with "[Place: <event>] " when a
    placement hint was found; "" when the script has no leading comment."""
    s = src.lstrip("\ufeff \t\r\n")
    out = []
    lines = s.splitlines()
    i = 0
    while i < len(lines):
        t = lines[i].strip()
        if t.startswith("/*"):
            j = i
            block = []
            while j < len(lines):
                block.append(lines[j])
                if "*/" in lines[j]:
                    break
                j += 1
            body = "\n".join(block)
            body = body[2:body.find("*/")] if "*/" in body else body[2:]
            out += [l.strip(" *\t/") for l in body.splitlines()]
            i = j + 1
        elif t.startswith("//"):
            out.append(t.lstrip("/: ").strip(" *\t:"))
            i += 1
        elif t == "" and out:
            i += 1
        else:
            break
    hint = None
    kept = []
    for l in out:
        m = PLACEMENT_RE.search(l)
        if m and not hint:
            hint = m.group(1).strip()
        if not l or set(l) <= set("-=*:_#/") or BOILERPLATE_RE.search(l) or re.match(r"^(script generated by|for download|v\.?\s*\d)", l, re.I):
            continue
        if hint and l.lower().startswith("put this"):
            continue
        kept.append(l)
    txt = " ".join(kept)
    if hint:
        txt = f"[Place: {hint}] " + txt
    return txt[:600]


def normalise_code(code: str) -> str:
    """Collapse all whitespace to single spaces, so two scripts that differ only in layout hash the same."""
    return re.sub(r"\s+", " ", code).strip()


# OBJECT_TYPE_<X> constant of CreateObject -> the blueprint extension it creates from
OBJECT_TYPE_EXT = {"CREATURE": "utc", "ITEM": "uti", "PLACEABLE": "utp", "STORE": "utm", "WAYPOINT": "utw",
                   "DOOR": "utd", "TRIGGER": "utt", "ENCOUNTER": "ute"}


def call_args(code, pos):
    """The top-level arguments of the call whose "(" ends just before `pos` in comment-free code, stripped:
    'CreateObject(OBJECT_TYPE_ITEM, GetName(o), l)' -> ['OBJECT_TYPE_ITEM', 'GetName(o)', 'l']. Commas and brackets
    inside nested calls or "strings" don't split. Stops at the matching ")" (or the end of the code)."""
    args, depth, cur, i, in_str = [], 0, [], pos, False
    while i < len(code):
        c = code[i]
        if in_str:
            in_str = c != '"' or code[i - 1] == "\\"
        elif c == '"':
            in_str = True
        elif c in "([":
            depth += 1
        elif c in ")]":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            args.append("".join(cur).strip()); cur = []; i += 1
            continue
        cur.append(c)
        i += 1
    last = "".join(cur).strip()
    return args + [last] if last or args else args


# ---------------------------------------------------------------------------
class Checkpoint(Exception):
    """Raised inside add_source when the time budget is spent; carries (source_id, next_item_index)."""
    def __init__(self, sid, j):
        super().__init__(f"checkpoint at source {sid} item {j}")
        self.sid, self.j = sid, j


# module.ifo fields that name haks and the custom talk table. They become hak edges (and meta), never name tokens:
# a hak is often named like a file inside it (cep2_core0.hak holds cep2_core0.txt), and a rebuild renames the hak,
# so treating the name as a mention would make that file look used in the original and lost in the clean build.
IFO_NOT_NAMES = frozenset({"Mod_Hak", "Mod_CustomTlk"})


class Indexer:
    """Reads sources one by one into index.sqlite and builds the dependency graph in memory.

    Normal use (as run_index does): add_source() for the module, then each hak and override folder, then
    post_process() once, which links names, validates and writes the graph tables. Rows that are numerous (fields,
    strrefs, tokens) are buffered and written in batches; the graph (nodes, edges) and issues stay in memory until
    post_process. Reads sources only; writes only inside out_dir."""

    def __init__(self, out_dir, tlk_path=None, write_json=True, verbose=True, resume=False):
        """out_dir: analysis folder (created if needed). tlk_path: a nwnlib.TlkSet, or a path to a custom .tlk, or
        None. write_json: also write json/ (conversations/ is always written). resume: open the existing
        index.sqlite of an interrupted run instead of deleting it and creating a new schema."""
        self.out_dir = out_dir
        self.write_json = write_json
        self.verbose = verbose
        os.makedirs(out_dir, exist_ok=True)
        db_path = os.path.join(out_dir, "index.sqlite")
        if os.path.exists(db_path) and not resume:
            os.remove(db_path)
        self.db = sqlite3.connect(db_path)
        if not resume:
            self.db.executescript(SCHEMA)
        self.budget = None            # (deadline, state_path) when running in time-boxed chunks
        self.tlk = tlk_path if isinstance(tlk_path, n.TlkSet) else n.TlkSet(custom=n.Tlk(tlk_path) if tlk_path else None)
        self.strref_rows = []
        self.nodes: Dict[str, dict] = {}
        self.edges: List[Tuple[str, str, str, str]] = []
        self.field_rows = []
        self.issue_rows = []
        self.base_names = set()    # "resref.ext" names the base game provides (empty: install not found/not set)
        self.skipped = []          # non-game files in the module folder (not indexed, not packed)
        self.resources = defaultdict(list)   # "resref.ext" -> [(source_id, file_id)]
        self.tags = defaultdict(set)          # tag -> {node}
        # (owner node, line, func, literal): strings that may name something, resolved after all files are read.
        # Owner is a script, or an object/module for VarTable values and dialogue parameters (line 0).
        self.pending_literals = []
        self.file_nodes = {}                  # "resref.ext" -> node
        self.token_rows = []                  # (node, token) buffered for the tokens table (name scanning)
        self.concat_literals = []            # string pieces used to build names at runtime
        self.concat_keys = set()
        self.str_consts = []                 # (script, line, name, value) string constants/variables with a literal value
        self.glued_idents = set()            # identifiers used to build a name at runtime (x + y, x += y, y = x ...)
        self.obj_types = {}                  # (script node, line, name) -> blueprint ext from CreateObject(OBJECT_TYPE_..)
        self.wrappers = {}                   # module function -> kind, when it passes a string parameter on to
                                             # CreateObject / CreateItemOnObject / ExecuteScript / ActionStartConversation
        self.tagbased_prefixes = set()
        self.tagbased_unknown = []           # "script line N" where a tag-based prefix is set from a non-literal
        self.dynamic_creates = []            # (script, line, blueprint ext or "*", call) - see index_nss
        self._cur_tokens = None
        self.stats = defaultdict(int)
        # json/ and conversations/ hold one copy per file name: the one the game uses. written_copies remembers
        # the rank (see add_source) of the copy written so far, so a later, losing copy doesn't overwrite it.
        self.written_copies = {}
        self._cur_rank = 1000

    def log(self, *a):
        """Print a progress line when verbose."""
        if self.verbose:
            print(*a, flush=True)

    # ---- graph helpers ----
    def node(self, nid, ntype, name, label=None, file_id=None, in_module=0):
        """Create graph node nid, or update it, and return nid.

        A node is often created first as a bare reference (in_module=0, no file) and later by the file itself, so
        an update only adds information: in_module is raised to 1, a missing file_id is filled, and a label is
        replaced only while it is still the plain name. The type of an existing node never changes."""
        cur = self.nodes.get(nid)
        if cur is None:
            self.nodes[nid] = dict(type=ntype, name=name, label=label or name,
                                   file_id=file_id, in_module=in_module)
        else:
            if in_module:
                cur["in_module"] = 1
            if file_id and not cur["file_id"]:
                cur["file_id"] = file_id
            if label and cur["label"] == cur["name"]:
                cur["label"] = label
        return nid

    def edge(self, src, dst, kind, via=""):
        """Record the edge src -> dst of the given kind (see CARRIER_KINDS); via says where it was found."""
        # edges repeat the same node ids and kinds many times: interning keeps one copy of each string in memory
        self.edges.append((sys.intern(src), sys.intern(dst), sys.intern(kind), sys.intern(via)))

    def issue(self, severity, category, node, detail):
        """Record a finding ('error' / 'warning' / 'info') for the issues table."""
        self.issue_rows.append((severity, category, node, detail))

    def text(self, v) -> str:
        """Readable text of a GFF value: a localised string through the talk tables, None as ""."""
        if isinstance(v, n.LocString):
            return v.text(self.tlk)
        return "" if v is None else str(v)

    def file_node(self, resref, ext, fid):
        """Canonical graph node for a file (see "Graph node ids" in the module docstring); creates it as in_module.

        Several files can share one node: .nss and .ncs -> script:x, and .are/.git/.gic -> area:x."""
        if ext in ("nss", "ncs"):
            nid = f"script:{resref}"; t = "script"; name = resref
        elif ext in BLUEPRINT_EXTS:
            nid = f"bp:{resref}.{ext}"; t = "blueprint"; name = f"{resref}.{ext}"
        elif ext in ("are", "git", "gic"):
            nid = f"area:{resref}"; t = "area"; name = resref
        elif ext == "dlg":
            nid = f"dlg:{resref}"; t = "conversation"; name = resref
        elif ext == "ifo":
            nid = "module"; t = "module"; name = "module"
        elif ext == "2da":
            nid = f"2da:{resref}"; t = "2da"; name = resref
        elif ext == "mdl":
            nid = f"model:{resref}"; t = "model"; name = resref
        elif ext == "hak":
            nid = f"hak:{resref}"; t = "hak"; name = resref
        elif ext in n.GFF_EXTENSIONS:
            nid = f"file:{resref}.{ext}"; t = "file"; name = f"{resref}.{ext}"
        else:
            nid = f"asset:{resref}.{ext}"; t = "asset"; name = f"{resref}.{ext}"
        self.node(nid, t, name, file_id=fid, in_module=1)
        return nid

    # ---- source loading ----
    def add_source(self, path, kind, priority, start=0, sid=None):
        """Index every file of one source (a folder or an ERF archive such as .mod / .hak). Returns its source id.

        kind: "module", "hak" or "override"; priority: see "Sources and priority" in the module docstring.
        start / sid: resume an interrupted source at item number start, reusing its existing sources row.
        Raises Checkpoint when the time budget runs out (rows read so far are committed first). Files that fail
        to read or parse are recorded (files.status='error' and an issue) rather than stopping the run.
        Only reads the source."""
        if sid is None:
            cur = self.db.execute("INSERT INTO sources(path, kind, priority) VALUES (?,?,?)",
                                  (os.path.abspath(path), kind, priority))
            sid = cur.lastrowid
        # precedence of this source's copies (lower wins), the game's order (nwnlib.source_rank)
        self._cur_rank = n.source_rank(kind, priority)
        # (relpath, resref, ext, loader). The loader reads the bytes only when the file's turn comes, so a big hak
        # is never held in memory whole. "Resume at item j" relies on the next run listing the items in the same
        # order (ERF entry order; os.walk order of an unchanged folder - it is not sorted here).
        items = []
        if os.path.isdir(path):
            links = []
            for full, rel in n.walk_folder(path, links):
                fn = os.path.basename(full)
                base, dot, ext = fn.rpartition(".")
                if fn.startswith(".") or not dot or ext.lower() not in n.EXT_TO_RESTYPE:
                    if kind == "module":
                        self.skipped.append(rel)
                    continue
                items.append((rel, base.lower(), ext.lower(), (lambda r=rel: n.read_file_inside(path, r))))
            if links and start == 0:
                # recorded once: a resumed source lists the same links again
                hak_node = f"hak:{os.path.splitext(os.path.basename(os.path.normpath(path)))[0].lower()}"
                self.issue("warning", "symlink_skipped", hak_node if kind == "hak" else "module",
                           f"{len(links)} symbolic link(s) in {path} were not read: a link can point outside the folder, "
                           "and its target would be indexed and packed into the build as part of the module ("
                           + ", ".join(links[:6]) + (" …" if len(links) > 6 else "") + "). Copy the real files in "
                           "if they belong to the module")
        else:
            erf = n.Erf(path)
            # nwnlib.Erf has already renamed entries with unsafe names (path characters or reserved device names, see
            # nwnlib.safe_resref) to invalid_name_<n>, so they can't steer the json/ file names; report each one
            for bad in erf.bad_names:
                self.issue("error", "unsafe_name", f"file:{bad}", f"{os.path.basename(path)} contains an entry with an "
                           f"unsafe name {bad!r} (path characters or reserved device name); it was renamed and ignored")
            for e in erf.entries:
                items.append((e.filename, e.resref, e.ext, (lambda e=e, erf=erf: erf.read(e))))
        self.log(f"  {kind}: {path}  ({len(items)} files{f', resuming at {start}' if start else ''})")
        erf = None if os.path.isdir(path) else erf
        try:
            for j, (relpath, resref, ext, loader) in enumerate(items):
                if j < start:
                    continue           # already indexed by an earlier, checkpointed run
                if self.budget and time.time() > self.budget[0]:
                    raise Checkpoint(sid, j)     # stop between two files, never half-way through one
                size = self.load_file(sid, relpath, resref, ext, loader)
                if nwn_progress.ENABLED:
                    self.tick(ext, size, os.path.basename(os.path.normpath(path)))
        finally:
            # also on Checkpoint: close the archive and commit, so the rows match what save_state records
            if erf is not None:
                erf.close()
            self.db.commit()
        if nwn_progress.ENABLED:
            self.tick(None, 0, os.path.basename(os.path.normpath(path)), force=True)
        return sid

    def tick(self, ext, size, source, force=False):
        """Progress for the dashboard: cost units done so far (throttled to ~3 lines a second)."""
        if ext is not None:
            self._done_units = getattr(self, "_done_units", 0.0) + nwn_progress.cost(ext, size)
            self._done_files = getattr(self, "_done_files", 0) + 1
        now = time.time()
        if force or now - getattr(self, "_last_tick", 0) >= 0.33:
            self._last_tick = now
            nwn_progress.emit(phase="index", done_units=round(getattr(self, "_done_units", 0.0), 1),
                              done_files=getattr(self, "_done_files", 0), source=source)

    # ---- checkpoint / resume (time-boxed runs: the device sandbox kills long jobs) ----
    # Everything held in memory between files. save_state writes these to index.state as JSON; load_state puts them
    # back. Anything already in index.sqlite (files, fields flushed so far, objects, scripts ...) is not repeated here.
    STATE_ATTRS = ("strref_rows", "nodes", "edges", "field_rows", "issue_rows", "base_names", "skipped",
                   "resources", "tags", "pending_literals", "file_nodes", "concat_literals",
                   "concat_keys", "tagbased_prefixes", "stats", "tlk_info", "str_consts", "glued_idents", "wrappers", "obj_types",
                   "written_copies", "tagbased_unknown", "dynamic_creates")
    # JSON has no sets and no tuple dictionary keys: these are written as sorted lists (obj_types as [key, value]
    # pairs) and rebuilt by load_state. Tuples come back as lists, which every reader of these attributes accepts.
    STATE_SETS = ("base_names", "concat_keys", "tagbased_prefixes", "glued_idents")
    STATE_FORMAT = "nwn_index checkpoint 2"      # 1 was a pickle (it could run code from the file; never read now)

    def save_state(self, state_path, progress):
        """Write a checkpoint: flush buffered rows, commit, and write the in-memory state to state_path as JSON.

        progress: dict(sources=plan, pos=(source index, item index), phase="sources" | "post") - where to resume.
        The file is written to a .tmp name and then renamed, so a kill during the write leaves the previous
        checkpoint intact (os.replace is atomic on the same disk)."""
        self.flush_fields()
        self.db.commit()
        st = {"format": self.STATE_FORMAT}
        for k in self.STATE_ATTRS:
            v = getattr(self, k, None)
            if k in self.STATE_SETS:
                v = sorted(v)
            elif k == "tags":
                v = {t: sorted(nodes) for t, nodes in v.items()}
            elif k == "obj_types":
                v = [[list(key), ext] for key, ext in v.items()]
            st[k] = v
        st["progress"] = progress
        # the highest files / sources ids this state knows about: rows above them (committed by a later step that
        # was killed before its state file was written) are removed by load_state, so they are never indexed twice
        st["max_file_id"] = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM files").fetchone()[0]
        st["max_source_id"] = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM sources").fetchone()[0]
        with open(state_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(state_path + ".tmp", state_path)

    def load_state(self, state_path):
        """Restore the in-memory state written by save_state and return its progress dict.

        Raises ValueError when the file is not a checkpoint in this version's JSON format (damaged, or the pickle an
        older toolkit wrote): such a file is never resumed from. Only data is read; nothing in it is run."""
        try:
            with open(state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (ValueError, UnicodeDecodeError):
            st = None
        if not isinstance(st, dict) or st.get("format") != self.STATE_FORMAT:
            raise ValueError(f"{state_path} is not a checkpoint this toolkit can resume (damaged, or written by an "
                             "older version) - delete index.state and run the analysis again to start the index afresh")
        for k in self.STATE_ATTRS:
            if k in st:
                setattr(self, k, st[k])
        for k in self.STATE_SETS:
            setattr(self, k, {tuple(x) if isinstance(x, list) else x for x in getattr(self, k)})
        self.obj_types = {tuple(key): ext for key, ext in self.obj_types}
        self.resources = defaultdict(list, self.resources)
        self.tags = defaultdict(set, {t: set(nodes) for t, nodes in self.tags.items()})
        self.stats = defaultdict(int, self.stats)
        self._drop_rows_after(st["max_file_id"], st["max_source_id"])
        return st["progress"]

    def _drop_rows_after(self, max_file_id, max_source_id):
        """Remove rows the state file doesn't know about: a run killed between committing a step's rows and writing
        its state leaves rows for files this state will index again. Those files (and everything keyed by them)
        go; tokens stay (link_names reads them DISTINCT, so a repeat is harmless)."""
        fids = [r[0] for r in self.db.execute("SELECT id FROM files WHERE id > ?", (max_file_id,))]
        if fids:
            nodes = [r[0] for r in self.db.execute("SELECT DISTINCT node FROM files WHERE id > ? AND node IS NOT NULL "
                                                   "AND node NOT IN (SELECT node FROM files WHERE id <= ? AND node IS NOT NULL)",
                                                   (max_file_id, max_file_id))]
            self.log(f"  resume: dropping {len(fids)} file row(s) left by an interrupted step")
            for tbl in ("fields", "strrefs", "objects", "scripts", "conversations", "twoda", "models"):
                self.db.execute(f"DELETE FROM {tbl} WHERE file_id > ?", (max_file_id,))
            # quest entries are keyed by tag and equips by the owner's node: only keys no surviving file shares are
            # cleared (a shared key would lose the surviving file's rows)
            self.db.execute("DELETE FROM quest_entries WHERE tag IN (SELECT tag FROM quests WHERE file_id > ?) "
                            "AND tag NOT IN (SELECT tag FROM quests WHERE file_id <= ?)", (max_file_id, max_file_id))
            self.db.execute("DELETE FROM quests WHERE file_id > ?", (max_file_id,))
            self.db.executemany("DELETE FROM equips WHERE owner=?", ((nd,) for nd in nodes))
            self.db.execute("DELETE FROM files WHERE id > ?", (max_file_id,))
        self.db.execute("DELETE FROM sources WHERE id > ?", (max_source_id,))
        self.db.commit()

    def wins(self, key):
        """Should the copy of `key` ("resref.ext") from the source being read be written to json/ or conversations/?
        True unless a copy from a source the game prefers (lower rank) was written already; records this copy."""
        prev = self.written_copies.get(key)
        if prev is not None and prev < self._cur_rank:
            return False
        self.written_copies[key] = self._cur_rank
        return True

    def load_file(self, sid, relpath, resref, ext, loader):
        """Index one file: add its files row and graph node, then parse it by type. Returns its size in bytes.

        loader: function returning the file's bytes. A file that can't be read gets a files row with
        status='error' (no node); a file that can't be parsed keeps its row and node, gets status='error' and a
        parse_error issue, and is still scanned for names. Never raises for bad content."""
        try:
            data = loader()
        except Exception as ex:  # noqa
            self.db.execute("INSERT INTO files(source_id, relpath, resref, ext, kind, status, error) "
                            "VALUES (?,?,?,?,?,?,?)", (sid, relpath, resref, ext,
                                                       n.TYPE_NAMES.get(ext, ext), "error", str(ex)))
            return 0
        cur = self.db.execute(
            "INSERT INTO files(source_id, relpath, resref, ext, kind, size, sha256, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, relpath, resref, ext, n.TYPE_NAMES.get(ext, ext), len(data), sha256(data), "ok"))
        fid = cur.lastrowid
        if len(resref) > 16:
            self.issue("error", "name_too_long", f"{resref}.{ext}",
                       f"'{resref}.{ext}' has a {len(resref)}-character name; NWN resource names are limited "
                       "to 16 characters, so the game/toolset cannot load it")
        self.resources[f"{resref}.{ext}"].append((sid, fid))
        fnode = self.file_node(resref, ext, fid)
        self.db.execute("UPDATE files SET node=? WHERE id=?", (fnode, fid))
        self.file_nodes[f"{resref}.{ext}"] = fnode
        # _cur_tokens: words in this file that might be other files' names (linked later by link_names). GFF and
        # .nss files set it in their own indexer from fields/literals only; other scannable types use every word.
        self._cur_tokens = None
        if ext in SCAN_EXTS and ext not in n.GFF_EXTENSIONS and ext != "nss":
            self._cur_tokens = {t.decode("ascii").lower() for t in TOKEN_RE.findall(data)}
        self.stats[ext] += 1
        try:
            if ext in n.GFF_EXTENSIONS:
                self.index_gff(fid, resref, ext, data)
            elif ext == "nss":
                self.index_nss(fid, resref, data)
            elif ext == "ncs":
                self.node(f"script:{resref}", "script", resref, in_module=1)
            elif ext == "2da":
                self.index_2da(fid, resref, data)
            elif ext == "mdl":
                self.index_mdl(fid, resref, data)
            elif ext == "hak":
                self.node(f"hak:{resref}", "hak", resref, file_id=fid, in_module=1)
        except Exception as ex:  # keep going; record the problem
            self.db.execute("UPDATE files SET status='error', error=? WHERE id=?", (str(ex), fid))
            self.issue("error", "parse_error", fnode, f"{resref}.{ext}: {ex}")
            # fall back to a raw word scan so names inside a damaged file still count as used (rule: keep if unsure)
            self._cur_tokens = {t.decode("latin-1").lower() for t in TOKEN_RE.findall(data)}
        if self._cur_tokens:
            self._cur_tokens.discard(resref)
            # resource names are <=16 chars: anything longer can never be a name reference
            self.token_rows.extend((fnode, t) for t in self._cur_tokens if len(t) <= 16)
            self._cur_tokens = None
            if len(self.token_rows) > 200000:     # write in batches to keep memory bounded on big haks
                self.flush_fields()
        return len(data)

    # ---- GFF ----
    def index_gff(self, fid, resref, ext, data):
        """Index a GFF file: JSON copy (json/), content hash, one fields row per leaf, talk-table references, name
        tokens, then the type-specific reader (blueprint, area, dialogue, journal, module.ifo). Raises on a
        damaged file (load_file records it)."""
        root = n.read_gff(data)
        j = n.gff_to_json(root)
        if self.write_json and self.wins(f"{resref}.{ext}"):
            d = os.path.join(self.out_dir, "json")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{resref}.{ext}.json"), "w", encoding="utf-8") as fh:
                json.dump(j, fh, indent=1, ensure_ascii=False)
        # identity-agnostic content hash (for duplicate detection): two blueprints that differ only in their own
        # name, toolset comment or palette category hash the same. Only top-level keys are dropped.
        ident = {k: v for k, v in j.items() if k not in ("TemplateResRef", "Comment", "PaletteID", "ResRef")}
        chash = sha256(json.dumps(ident, sort_keys=True).encode())
        self.db.execute("UPDATE files SET content_hash=? WHERE id=?", (chash, fid))
        toks = set()
        for path, label, ftype, value, _parent in n.iter_gff_leaves(root):
            self.field_rows.append((fid, path, label, n.TYPE_LABELS[ftype], n.leaf_to_text(ftype, value, self.tlk)))
            if ftype == n.CEXOLOCSTRING and value.strref != n.BAD_STRREF:
                # 0xFFFFFFFF = "no talk-table entry"; a number at or above 0x01000000 points into the custom .tlk
                self.strref_rows.append((fid, path, value.strref, int(value.strref >= n.CUSTOM_TLK_BIT),
                                         int(any(t for t in value.entries.values())), self.tlk.status(value.strref)))
            if ext == "ifo" and label in IFO_NOT_NAMES:
                continue    # hak and talk-table names: not mentions of files (a hak's own readme can share its name)
            if (ftype == n.RESREF or (ftype == n.CEXOSTRING and not re.search(r"\s", value))) and value:
                # prose (comments, messages) is not a name: "Improper use of item!" doesn't use item.uti
                toks.update(t.decode("latin-1").lower() for t in TOKEN_RE.findall(value.encode("latin-1", "replace")))
        self._cur_tokens = toks
        if len(self.field_rows) > 50000:     # write in batches to keep memory bounded
            self.flush_fields()

        if ext in BLUEPRINT_EXTS:
            self.index_blueprint(fid, resref, ext, root)
        elif ext in ("git", "are", "gic"):
            self.index_area_part(fid, resref, ext, root)
        elif ext == "dlg":
            self.index_dlg(fid, resref, root)
        elif ext == "jrl":
            self.index_jrl(fid, resref, root)
        elif ext == "ifo":
            self.index_ifo(fid, root)

    def flush_fields(self):
        """Write the buffered fields, strrefs and tokens rows to the database (not committed here)."""
        self.db.executemany("INSERT INTO fields VALUES (?,?,?,?,?)", self.field_rows)
        self.field_rows = []
        self.db.executemany("INSERT INTO strrefs VALUES (?,?,?,?,?,?)", self.strref_rows)
        self.strref_rows = []
        self.db.executemany("INSERT INTO tokens VALUES (?,?)", self.token_rows)
        self.token_rows = []

    def obj_name(self, s: n.GffStruct) -> str:
        """Display name of an object struct. The label differs by type (e.g. items LocalizedName, placeables and
        doors LocName, areas Name, creatures FirstName + LastName); "" when there is none."""
        for lab in ("LocalizedName", "LocName", "Name"):
            if lab in s:
                t = self.text(s.get(lab))
                if t:
                    return t
        if "FirstName" in s:
            return (self.text(s.get("FirstName")) + " " + self.text(s.get("LastName"))).strip()
        return ""

    def scan_struct_refs(self, owner, s: n.GffStruct, path: str, dlg=False):
        """Record script/conversation/tag references held directly by this struct.

        owner: node id the edges start from. path: GFF path of s (used as the edges' via). dlg: s is part of a
        .dlg, where Script/Active are node scripts rather than object events. Nested structs are not walked here;
        callers iterate them. VarTable string values are queued for resolve_literals."""
        for label, f in s.fields.items():
            via = f"{path}/{label}" if path else label
            if f.type == n.RESREF:
                v = f.value.lower()
                if not v:
                    continue
                if dlg and label in DLG_SCRIPT_LABELS:
                    self.edge(owner, self.node(f"script:{v}", "script", v), "dlg_script", via)
                elif SCRIPT_LABEL_RE.match(label) or label in ("EndConversation", "EndConverAbort"):
                    self.edge(owner, self.node(f"script:{v}", "script", v), "event_script", via)
                elif label == "Conversation":
                    self.edge(owner, self.node(f"dlg:{v}", "conversation", v), "uses_conversation", via)
            elif f.type == n.CEXOSTRING and f.value:
                v = f.value
                # KeyName: tag of the key that opens a door/placeable; LinkedTo: tag of a transition's destination
                if label == "KeyName":
                    self.edge(owner, self.node(f"tag:{v}", "tag", v), "key_tag", via)
                elif label == "LinkedTo":
                    self.edge(owner, self.node(f"tag:{v}", "tag", v), "transition_tag", via)
        # variables stored on the object (VarTable)
        for var in s.get("VarTable", []) or []:
            vname = var.get("Name", "")
            val = var.get("Value")
            if isinstance(val, str) and val:
                self.pending_literals.append((owner, 0, "VarTable:" + vname, val))
                # BioWare's tag-based item scripting (x2_inc_switches) runs the script named <this prefix> + item tag
                if vname == "MODULE_VAR_TAGBASED_SCRIPT_PREFIX":
                    self.tagbased_prefixes.add(val.lower())
                # short values like "spawn_" or variables named *PREFIX*/*SUFFIX* are probably glued onto names
                if 2 <= len(val) <= 16 and (val.endswith("_") or val.startswith("_") or
                                            re.search(r"PREFIX|SUFFIX", vname, re.I)):
                    self.concat_literals.append((owner, 0, "VarTable:" + vname, val))

    def register_tag(self, node, tag):
        """Note that node carries tag: a tag_provider edge tag -> node (tags are not unique in NWN)."""
        if tag:
            self.tags[tag].add(node)
            self.edge(self.node(f"tag:{tag}", "tag", tag), node, "tag_provider", "Tag")

    def index_blueprint(self, fid, resref, ext, root):
        """Blueprint (.uti/.utc/...): objects row, tag, script fields in every struct, inventory items (inventory
        edges, equipped ones also in equips) and, for encounters, the creatures they spawn."""
        cls = EXT_CLASS[ext]
        tag = root.get("Tag", "") or ""
        name = self.obj_name(root)
        nid = self.node(f"bp:{resref}.{ext}", "blueprint", f"{resref}.{ext}",
                        f"{name} [{resref}.{ext}]" if name else f"{resref}.{ext}", fid, 1)
        self.db.execute("INSERT INTO objects(file_id,node,class,is_blueprint,resref,template,tag,name,path) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (fid, nid, cls, 1, resref, resref, tag, name, ""))
        self.register_tag(nid, tag)
        for path, s in n.iter_gff_structs(root):
            self.scan_struct_refs(nid, s, path)
            for lab, tgt_ext, kind in (("InventoryRes", "uti", "inventory"),
                                       ("EquippedRes", "uti", "inventory")):
                v = (s.get(lab) or "").lower()
                if v:
                    self.edge(nid, self.node(f"bp:{v}.{tgt_ext}", "blueprint", f"{v}.{tgt_ext}"),
                              kind, f"{path}/{lab}")
                    if lab == "EquippedRes":
                        self.db.execute("INSERT INTO equips VALUES (?,?,?,?)", (nid, v, s.sid, 0))
            # an encounter's CreatureList[i]/ResRef names the creature blueprint it spawns
            if ext == "ute" and "/CreatureList" in "/" + path and s.get("ResRef"):
                v = s.get("ResRef").lower()
                self.edge(nid, self.node(f"bp:{v}.utc", "blueprint", f"{v}.utc"), "spawns", f"{path}/ResRef")

    def index_area_part(self, fid, resref, ext, root):
        """One of an area's three files, all mapped to the node area:<resref>.

        .are (static: name, tag, tileset, area event scripts), .git (the placed objects, walked through
        GIT_LIST_CLASS) and .gic (toolset comments only: just marks the area as present)."""
        anode = f"area:{resref}"
        if ext == "are":
            name = self.text(root.get("Name"))
            self.node(anode, "area", resref, f"{name} [{resref}]" if name else resref, fid, 1)
            self.scan_struct_refs(anode, root, "")
            tag = root.get("Tag", "")
            self.register_tag(anode, tag)
            ts = (root.get("Tileset") or "").lower()
            if ts:
                self.edge(anode, self.node(f"set:{ts}", "tileset", ts), "template", "Tileset")
            return
        self.node(anode, "area", resref, in_module=1)
        if ext != "git":
            return
        self.scan_struct_refs(anode, root, "")  # AreaProperties etc.
        # walk instance lists
        for list_label, (cls, bp_ext) in GIT_LIST_CLASS.items():
            for i, inst in enumerate(root.get(list_label, []) or []):
                self.index_instance(fid, resref, inst, f"{list_label}[{i}]", cls, bp_ext, anode)

    def index_instance(self, fid, area, s, path, cls, bp_ext, parent_node):
        """One placed object of a .git (or an item copy inside one), recursively for nested inventories.

        area: area resref. s: the instance struct. path: its GFF path in the .git (part of the node id, so it is
        unique within the area). cls / bp_ext: object class and blueprint extension. parent_node: the area or the
        object holding this one (contains edge)."""
        tpl = (s.get("TemplateResRef") or "").lower()
        tag = s.get("Tag", "") or ""
        name = self.obj_name(s)
        nid = f"inst:{area}:{path}"
        label = f"{cls} '{name or tag or tpl}' in {area}"
        self.node(nid, "instance", f"{area}:{path}", label, fid, 1)
        # position: XPosition/YPosition/ZPosition, else plain X/Y/Z, else None (e.g. an item inside an inventory)
        x = s.get("XPosition", s.get("X"))
        y = s.get("YPosition", s.get("Y"))
        z = s.get("ZPosition", s.get("Z"))
        self.db.execute("INSERT INTO objects(file_id,node,class,is_blueprint,resref,template,tag,name,area,"
                        "container,path,x,y,z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fid, nid, cls, 0, None, tpl, tag, name, area, parent_node, path, x, y, z))
        self.edge(parent_node, nid, "contains", path)
        if tpl:
            self.edge(nid, self.node(f"bp:{tpl}.{bp_ext}", "blueprint", f"{tpl}.{bp_ext}"), "template",
                      "TemplateResRef")
        self.register_tag(nid, tag)
        self.scan_struct_refs(nid, s, path)
        # nested inventories (full item copies inside creatures/placeables/stores)
        for lab in ("ItemList", "Equip_ItemList"):
            for k, it in enumerate(s.get(lab, []) or []):
                # a full item struct (has TemplateResRef) is its own instance; otherwise the entry only names a
                # blueprint (InventoryRes / EquippedRes) and becomes an inventory edge
                if "TemplateResRef" in it:
                    self.index_instance(fid, area, it, f"{path}/{lab}[{k}]", "item", "uti", nid)
                    if lab == "Equip_ItemList":
                        self.db.execute("INSERT INTO equips VALUES (?,?,?,?)",
                                        (nid, (it.get("TemplateResRef") or "").lower(), it.sid, 1))
                elif it.get("InventoryRes") or it.get("EquippedRes"):
                    v = (it.get("InventoryRes") or it.get("EquippedRes")).lower()
                    self.edge(nid, self.node(f"bp:{v}.uti", "blueprint", f"{v}.uti"), "inventory", f"{path}/{lab}[{k}]")
                    if lab == "Equip_ItemList":
                        self.db.execute("INSERT INTO equips VALUES (?,?,?,?)", (nid, v, it.sid, 0))
        for k, page in enumerate(s.get("StoreList", []) or []):    # a store's pages (armour, weapons ...) of items
            for m, it in enumerate(page.get("ItemList", []) or []):
                sub = f"{path}/StoreList[{k}]/ItemList[{m}]"
                if "TemplateResRef" in it:
                    self.index_instance(fid, area, it, sub, "item", "uti", nid)
                elif it.get("InventoryRes"):
                    v = it.get("InventoryRes").lower()
                    self.edge(nid, self.node(f"bp:{v}.uti", "blueprint", f"{v}.uti"), "inventory", sub)
        for k, cr in enumerate(s.get("CreatureList", []) or []):    # a placed encounter: the creatures it spawns
            v = (cr.get("ResRef") or "").lower()
            if v:
                self.edge(nid, self.node(f"bp:{v}.utc", "blueprint", f"{v}.utc"), "spawns",
                          f"{path}/CreatureList[{k}]")

    def index_dlg(self, fid, resref, root):
        """Conversation (.dlg): node/link scripts, journal updates, speaker tags, script parameters (queued for
        resolve_literals), a conversations row with its size, and the readable tree in conversations/."""
        nid = self.node(f"dlg:{resref}", "conversation", resref, file_id=fid, in_module=1)
        entries = root.get("EntryList", []) or []
        replies = root.get("ReplyList", []) or []
        starts = root.get("StartingList", []) or []
        words = 0
        for path, s in n.iter_gff_structs(root):
            self.scan_struct_refs(nid, s, path, dlg=True)
            q = s.get("Quest")
            if q:
                self.edge(nid, self.node(f"quest:{q}", "quest", q), "quest_ref", f"{path}/Quest")
            sp = s.get("Speaker")
            if sp:
                self.edge(nid, self.node(f"tag:{sp}", "tag", sp), "speaker_tag", f"{path}/Speaker")
            # EE dialogue script parameters (Key/Value pairs the script reads with GetScriptParam): a value may be
            # a name (a variable, a tag, a script ...), so it is resolved like a script literal
            for plist in ("ActionParams", "ConditionParams"):
                for prm in s.get(plist, []) or []:
                    val = prm.get("Value")
                    if isinstance(val, str) and val:
                        self.pending_literals.append((nid, 0, f"{plist}:{prm.get('Key', '')}", val))
            if "Text" in s:
                words += len(self.text(s.get("Text")).split())
        self.db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?)",
                        (fid, resref, len(entries), len(replies), len(starts), words))
        if self.wins(f"{resref}.dlg"):
            self.render_dlg(resref, root, entries, replies, starts)

    def render_dlg(self, resref, root, entries, replies, starts):
        """Write conversations/<resref>.md: the dialogue as an indented tree from each starting line, then the
        nodes no start reaches. Entries (E) are NPC lines, replies (R) PC lines; they alternate down the tree.
        index_dlg calls it only for the copy of a conversation that the game uses (see wins)."""
        d = os.path.join(self.out_dir, "conversations")
        os.makedirs(d, exist_ok=True)
        lines = [f"# Conversation `{resref}`", ""]
        for lab in ("EndConversation", "EndConverAbort"):
            if root.get(lab):
                lines.append(f"- {lab}: `{root.get(lab)}`")
        lines.append("")
        lines.append("Legend: **NPC** lines and **PC** replies; `if:` = condition script, "
                     "`do:` = action script, `quest:` = journal update, `→ link` = jumps to an existing node.")
        lines.append("")
        seen = set()

        def fmt(node, kind, idx, link):
            """One Markdown line for a dialogue node: speaker, [E/R index], text, then its condition (from the
            link that leads to it), action script and journal update."""
            t = self.text(node.get("Text")).replace("\n", " ")
            spk = node.get("Speaker") or ""
            who = "PC" if kind == "R" else (f"NPC({spk})" if spk else "NPC")
            extra = []
            if link.get("Active"):
                extra.append(f"if:`{link.get('Active')}`")
            if node.get("Script"):
                extra.append(f"do:`{node.get('Script')}`")
            if node.get("Quest"):
                extra.append(f"quest:`{node.get('Quest')}`#{node.get('QuestEntry')}")
            return f"**{who}** [{kind}{idx}] {t or '(continue)'}" + (("  — " + " ".join(extra)) if extra else "")

        def walk(kind, idx, link, depth):
            """Print node idx of list kind ("E" or "R") reached through link, then its children."""
            if depth > 60:          # guard against runaway depth (Python recursion limit) in a malformed file
                return
            lst = entries if kind == "E" else replies
            if idx >= len(lst):
                lines.append("  " * depth + f"- ⚠ broken link to {kind}{idx}")
                return
            node = lst[idx]
            # IsChild=1 marks a link that points back to a node owned elsewhere in the tree (BioWare dialogue
            # format); a node already printed is also shown as a link, which stops loops
            if link.get("IsChild") or (kind, idx) in seen:
                t = self.text(node.get("Text"))[:60].replace("\n", " ")
                cond = f" if:`{link.get('Active')}`" if link.get("Active") else ""
                lines.append("  " * depth + f"- → link to [{kind}{idx}] {t}{cond}")
                return
            seen.add((kind, idx))
            lines.append("  " * depth + "- " + fmt(node, kind, idx, link))
            child_list = node.get("RepliesList" if kind == "E" else "EntriesList", []) or []
            for ch in child_list:
                walk("R" if kind == "E" else "E", ch.get("Index", 0), ch, depth + 1)

        for st in starts:
            walk("E", st.get("Index", 0), st, 0)
        unreached = [("E", i) for i in range(len(entries)) if ("E", i) not in seen] + \
                    [("R", i) for i in range(len(replies)) if ("R", i) not in seen]
        if unreached:
            lines += ["", f"## Not reached from the start ({len(unreached)} node(s))", "",
                      "These nodes exist in the file but no path from a starting line leads to them (or they are "
                      "only reached through links shown above as → link). Orphans here are usually leftovers.", ""]
            for kind, idx in unreached:
                node = (entries if kind == "E" else replies)[idx]
                lines.append("- " + fmt(node, kind, idx, {}))
        with open(os.path.join(d, f"{resref}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def index_jrl(self, fid, resref, root):
        """Journal (.jrl): one quests row and quest:<tag> node per category, one quest_entries row per entry."""
        for cat in root.get("Categories", []) or []:
            tag = cat.get("Tag", "")
            name = self.text(cat.get("Name"))
            ents = cat.get("EntryList", []) or []
            self.db.execute("INSERT INTO quests VALUES (?,?,?,?)", (tag, name, fid, len(ents)))
            qn = self.node(f"quest:{tag}", "quest", tag, f"{name} [{tag}]", fid, 1)
            # the .jrl's own node (type "file", created by file_node when the file was loaded)
            self.edge(qn, self.node(f"file:{resref}.jrl", "file", f"{resref}.jrl", file_id=fid, in_module=1),
                      "template", "Categories")
            for e in ents:
                self.db.execute("INSERT INTO quest_entries VALUES (?,?,?,?)",
                                (tag, e.get("ID"), self.text(e.get("Text")), e.get("End", 0)))

    def index_ifo(self, fid, root):
        """module.ifo: module name, module event scripts, area list and entry area, hak list, custom talk table
        name (into meta), and the module's VarTable strings (queued like any other VarTable)."""
        mod = self.node("module", "module", "module", "MODULE (module.ifo)", fid, 1)
        name = self.text(root.get("Mod_Name"))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('module_name', ?)", (name,))
        for label, f in root.fields.items():
            if f.type == n.RESREF and f.value and label.startswith("Mod_On"):
                self.edge(mod, self.node(f"script:{f.value.lower()}", "script", f.value.lower()), "module_event", label)
        for a in root.get("Mod_Area_list", []) or []:
            v = (a.get("Area_Name") or "").lower()
            if v:
                self.edge(mod, self.node(f"area:{v}", "area", v), "module_area", "Mod_Area_list")
        ea = (root.get("Mod_Entry_Area") or "").lower()
        if ea:
            self.edge(mod, self.node(f"area:{ea}", "area", ea), "module_area", "Mod_Entry_Area")
        # Mod_HakList is the list form (first listed wins in game); a single top-level Mod_Hak is the older form
        haks = [h.get("Mod_Hak") for h in (root.get("Mod_HakList", []) or []) if h.get("Mod_Hak")]
        if root.get("Mod_Hak"):
            haks.append(root.get("Mod_Hak"))
        for h in haks:
            self.edge(mod, self.node(f"hak:{h.lower()}", "hak", h.lower()), "hak", "Mod_HakList")
        tlk = root.get("Mod_CustomTlk")
        if tlk:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('custom_tlk', ?)", (tlk,))
        for var in root.get("VarTable", []) or []:
            val = var.get("Value")
            if isinstance(val, str) and val:
                self.pending_literals.append((mod, 0, "VarTable:" + var.get("Name", ""), val))
                if var.get("Name") == "MODULE_VAR_TAGBASED_SCRIPT_PREFIX":
                    self.tagbased_prefixes.add(val.lower())
                if 2 <= len(val) <= 16 and (val.endswith("_") or val.startswith("_") or
                                            re.search(r"PREFIX|SUFFIX", var.get("Name", ""), re.I)):
                    self.concat_literals.append(("module", 0, "VarTable:" + var.get("Name", ""), val))

    # ---- scripts ----
    def index_nss(self, fid, resref, data):
        """Script source (.nss): scripts row, include edges, and the string literals (queued for resolve_literals).

        Also collects what is needed to spot names built at run time: string constants, identifiers glued with
        "+", the blueprint type of CreateObject calls, and module helper functions that pass a string parameter on
        to CreateObject / ExecuteScript / a conversation call ("wrappers"). Text-pattern based, not a compiler."""
        # lossless (nwnlib.decode_text): the build writes these sources back for the compiler (encode_text gives the
        # exact bytes again), and nwn_ncs compares their text with the compiled strings decoded the same way
        src = n.decode_text(data)
        code, includes, literals = lex_nss(src)
        # the engine runs main() for event/action scripts and StartingConditional() for dialogue conditions;
        # a script with neither is an include library
        has_main = bool(re.search(r"\bvoid\s+main\s*\(", code))
        has_sc = bool(re.search(r"\bint\s+StartingConditional\s*\(", code))
        norm = normalise_code(code)
        nh = sha256(norm.encode())
        self.db.execute("UPDATE files SET content_hash=? WHERE id=?", (nh, fid))
        # function definitions: a line starting with a NWScript return type, a name, a parameter list and "{"
        # (a prototype ends with ";" and is not counted), e.g. "int GetIsBoss(object o) {" -> GetIsBoss
        defined = sorted(set(re.findall(
            r"^\s*(?:void|int|float|string|object|location|effect|itemproperty|vector|talent|event|json|sqlquery|cassowary|struct\s+\w+)\s+(\w+)\s*\([^;{]*\)\s*\{",
            code, re.M)))
        calls = defaultdict(int)
        # calls: a capitalised name followed by "(" - engine functions and most module functions use this style
        for m in re.finditer(r"\b([A-Z][A-Za-z0-9_]+)\s*\(", code):
            calls[m.group(1)] += 1
        self.db.execute("INSERT INTO scripts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (fid, resref, int(has_main), int(has_sc), src.count("\n") + 1, nh, 0, src,
                         header_comment(src), ",".join(defined), json.dumps(calls)))
        role = "event/action" if has_main else ("condition" if has_sc else "include/library")
        sn = self.node(f"script:{resref}", "script", resref, f"{resref}.nss ({role})", fid, 1)
        for line, inc in includes:
            self.edge(sn, self.node(f"script:{inc}", "script", inc), "include", f"line {line}")
        toks = {inc for _, inc in includes}
        # string constants / variables are often glued onto names later (const string PFX = "evt_")
        # helper functions that pass a string parameter on to a create / run / talk call, e.g.
        #   void SelectLegacyHide(object oPC, string sHideRef) { ... CreateItemOnObject(sHideRef, oPC, 1); }
        # then SelectLegacyHide(oPC, "pchide_x") creates pchide_x (followed one level deep)
        # a function definition: return type, name, "(parameters)" without nested brackets, then "{"
        for m in re.finditer(r"\b(?:void|object|int|string|float)\s+([A-Za-z_]\w*)\s*\(([^()]*)\)\s*\{", code):
            fname = m.group(1)
            params = re.findall(r"\bstring\s+([A-Za-z_]\w*)", m.group(2))
            if not params or fname in FUNC_KIND:
                continue
            # the body runs to the matching "}" (comments are already removed; braces inside strings would
            # miscount, which only shortens or lengthens the text searched below)
            depth, j = 1, m.end()
            while j < len(code) and depth:
                depth += {"{": 1, "}": -1}.get(code[j], 0)
                j += 1
            body = code[m.end():j]
            for call, kind_ in (("CreateItemOnObject", "blueprint"), ("CreateObject", "blueprint"),
                                ("ExecuteScript", "script"), ("ActionStartConversation", "conversation"),
                                ("BeginConversation", "conversation")):
                # the call's arguments (up to the next ";") mention one of the string parameters
                for cm in re.finditer(r"\b" + call + r"\s*\(([^;]*)", body):
                    if any(re.search(r"\b" + re.escape(p_) + r"\b", cm.group(1)) for p_ in params):
                        self.wrappers.setdefault(fname, kind_)
        # identifiers glued into names: X + ..., ... + X, X += ..., and anything assigned into those (Y = X; Y += ...)
        #   first pattern: "sPfx + ..." and "sRes += ..." (but not "i++"); second: "... + sPfx" and "... += sPfx"
        glued = set(re.findall(r"\b([A-Za-z_]\w*)\s*\+(?![+])", code)) | set(re.findall(r"\+=?\s*([A-Za-z_]\w*)\b", code))
        assigns = re.findall(r"\b([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*;", code)    # "sRes = sPfx;" -> (sRes, sPfx)
        for _ in range(3):      # follow chains of plain assignments up to three steps back
            glued |= {rhs for lhs, rhs in assigns if lhs in glued}
        self.glued_idents |= glued
        # 'string sPfx = "evt_";' or 'sPfx = "evt_";' - a short text given to a variable or constant
        for m in re.finditer(r'\b(?:string\s+)?([A-Za-z_]\w*)\s*=\s*"([^"\n]{2,24})"\s*;', code):
            var, val = m.group(1), m.group(2)
            line_ = code[:m.start()].count("\n") + 1
            if var in glued:      # glued in this script (the common case)
                self.concat_literals.append((resref, line_, "string constant", val))
            elif m.group(0).startswith("string") and code[max(0, m.start() - 8):m.start()].rstrip().endswith("const"):
                self.str_consts.append((resref, line_, var, val))   # maybe glued in another script (an include constant)
        # the tag-based script prefix set by a script, either standard way (x2_inc_switches):
        #   SetLocalString(oMod, "MODULE_VAR_TAGBASED_SCRIPT_PREFIX", "i_")  ->  "i_"
        #   SetUserDefinedItemEventPrefix("i_")                              ->  "i_"  (sets that same variable)
        # A prefix that is not a literal can't be known, so it is recorded: the analysis then keeps every script
        # named <something><item tag> for review. The include that defines SetUserDefinedItemEventPrefix itself
        # (x2_inc_switches, if a module or hak ships a copy) only passes its parameter on, so it is not a use.
        if not re.search(r"\bvoid\s+SetUserDefinedItemEventPrefix\s*\([^)]*\)\s*\{", code):
            for m in re.finditer(r'(?:\bSetLocalString\s*\([^;]*?"MODULE_VAR_TAGBASED_SCRIPT_PREFIX"\s*,|'
                                 r'(?<!void )\bSetUserDefinedItemEventPrefix\s*\()\s*([^;]*?)\s*\)\s*;', code):
                lit = re.fullmatch(r'"([^"]*)"', m.group(1))
                if lit:
                    self.tagbased_prefixes.add(lit.group(1).lower())
                else:
                    self.tagbased_unknown.append(f"{resref} line {code[:m.start()].count(chr(10)) + 1}")
        # CreateObject / CreateItemOnObject whose blueprint name is neither a "literal" nor glued from pieces (those
        # are handled as runtime name pieces): a variable or a call such as NWNX_SQL_ReadDataInActiveRow(0) can name
        # ANY blueprint of that type - a PW's storage chest or bank recreating items from saved resrefs. Recorded
        # as (script, line, blueprint ext or "*" when the object type is not a constant, call text).
        for m in re.finditer(r"\b(CreateObject|CreateItemOnObject)\s*\(", code):
            if re.search(r"\b(object|void)\s*$", code[max(0, m.start() - 12):m.start()]):
                continue                     # the function's own declaration (nwscript.nss), not a call
            args = call_args(code, m.end())
            name_arg = args[1] if m.group(1) == "CreateObject" and len(args) > 1 else (args[0] if args else "")
            if not name_arg or re.fullmatch(r'"[^"]*"', name_arg) or "+" in re.sub(r'"[^"]*"', "", name_arg):
                continue
            if m.group(1) == "CreateItemOnObject":
                ext_ = "uti"
            else:
                mt = re.fullmatch(r"OBJECT_TYPE_(\w+)", args[0])
                ext_ = OBJECT_TYPE_EXT.get(mt.group(1), "*") if mt else "*"
            self.dynamic_creates.append((resref, code[:m.start()].count("\n") + 1, ext_,
                                         f"{m.group(1)}({', '.join(args)[:60]})"))
        code_lines = None
        for line, func, lit, concat in literals:
            # CreateObject(OBJECT_TYPE_PLACEABLE, "x", ...): the object type on the same line says which blueprint
            # extension "x" refers to (x.utp rather than x.utc)
            if func == "CreateObject" and not concat:
                code_lines = code_lines or code.split("\n")
                mt = re.search(r"OBJECT_TYPE_(\w+)", code_lines[line - 1] if 0 < line <= len(code_lines) else "")
                ext_ = OBJECT_TYPE_EXT.get(mt.group(1) if mt else "")
                if ext_:
                    self.obj_types[(sn, line, lit.lower())] = ext_
            if not re.search(r"\s", lit):          # a message or sentence is not a resource name
                toks.update(t.decode("latin-1").lower() for t in TOKEN_RE.findall(lit.encode("latin-1", "replace")))
            if concat:
                self.concat_keys.add((sn, line, lit))
                if len(lit) >= 2:
                    self.concat_literals.append((resref, line, func or "", lit))
                continue  # a piece of a name built at runtime - not a reference by itself
            if func and func.startswith("SetLocalString") and 2 <= len(lit) <= 16 and \
                    (lit.endswith("_") or lit.startswith("_")):
                self.concat_literals.append((resref, line, func, lit))  # e.g. "spawn_" kept in a variable
            self.pending_literals.append((sn, line, func or "", lit))
        self._cur_tokens = toks

    def resolve_literals(self):
        """Classify script string literals once all resources/tags are known.

        Turns every queued string (pending_literals) into an edge: typed by the call it is passed to (FUNC_KIND,
        a module wrapper, a Set/Get variable function), or, when the call says nothing, only if it exactly matches
        a known tag, script, quest, conversation or blueprint name (kind ends in "?"). Writes script_literals for
        strings owned by scripts. Run once, from post_process, after every source is loaded."""
        # include constants glued onto names in some other script: const string PFX = "oldmine"; (elsewhere) s = PFX; s += x
        for script_, line_, var, val in self.str_consts:
            if var in self.glued_idents:
                self.concat_literals.append((script_, line_, "string constant", val))
        known_scripts = {k[:-4] for k in self.resources if k.endswith(".nss") or k.endswith(".ncs")}
        known_bp = defaultdict(list)
        for k in self.resources:
            r, _, e = k.rpartition(".")
            if e in BLUEPRINT_EXTS:
                known_bp[r].append(e)
        known_dlg = {k[:-4] for k in self.resources if k.endswith(".dlg")}
        known_quests = {r[0] for r in self.db.execute("SELECT tag FROM quests")}
        rows = []
        for owner, line, func, lit in self.pending_literals:
            if not lit or len(lit) > 64:      # long text is a message, never a name
                continue
            via = f"line {line}" if line else func     # line 0: a VarTable / dialogue parameter, named by func
            kind = FUNC_KIND.get(func)
            low = lit.lower()
            if not kind and func in self.wrappers:
                # a module helper that passes the name on: only link names that really exist (other string
                # arguments of the helper - messages, tags - must not become 'missing' resources)
                wk = self.wrappers[func]
                if (wk == "blueprint" and low in known_bp) or (wk == "script" and low in known_scripts) or \
                        (wk == "conversation" and low in known_dlg):
                    kind = wk
            if func and VAR_SET_RE.match(func):
                kind = "var_set"
            elif func and VAR_GET_RE.match(func):
                kind = "var_get"
            if kind == "script":
                self.edge(owner, self.node(f"script:{low}", "script", low), "execute_script", via)
            elif kind == "tag":
                self.edge(owner, self.node(f"tag:{lit}", "tag", lit), "tag_ref", via)
            elif kind == "blueprint":
                # the name has no extension: link every blueprint type that exists with it; if none exists, guess
                # from the OBJECT_TYPE_ on the line, else from the call (CreateItemOnObject -> .uti, else .utc)
                exts = known_bp.get(low) or [self.obj_types.get((owner, line, low)) or
                                             ("uti" if "Item" in func else "utc")]
                for e in exts:
                    self.edge(owner, self.node(f"bp:{low}.{e}", "blueprint", f"{low}.{e}"), "spawns", via)
            elif kind == "quest":
                self.edge(owner, self.node(f"quest:{lit}", "quest", lit), "quest_ref", via)
            elif kind == "conversation":
                self.edge(owner, self.node(f"dlg:{low}", "conversation", low), "conversation_ref", via)
            elif kind == "2da":
                self.edge(owner, self.node(f"2da:{low}", "2da", low), "2da_ref", via)
            elif kind == "var_set":
                vn = self.node(f"var:{lit}", "variable", lit)
                self.edge(vn, owner, "var_set", via)     # reversed on purpose: variable -> its writer
            elif kind == "var_get":
                vn = self.node(f"var:{lit}", "variable", lit)
                self.edge(owner, vn, "var_read", via)
            else:
                # Untyped literal: link only when it exactly matches something we know.
                kind = None
                if lit in self.tags:
                    self.edge(owner, f"tag:{lit}", "literal_match", via); kind = "tag?"
                elif low in known_scripts and owner.startswith(("bp:", "inst:", "module", "area:")):
                    # a VarTable string naming a script (common pattern)
                    self.edge(owner, self.node(f"script:{low}", "script", low), "execute_script", via); kind = "script?"
                elif lit in known_quests:
                    self.edge(owner, f"quest:{lit}", "quest_ref", via); kind = "quest?"
                elif low in known_dlg:
                    self.edge(owner, f"dlg:{low}", "conversation_ref", via); kind = "conversation?"
                elif low in known_bp:
                    for e in known_bp[low]:
                        self.edge(owner, f"bp:{low}.{e}", "literal_match", via)
                    kind = "blueprint?"
            if owner.startswith("script:"):
                rows.append((owner[7:], line, func, lit, kind))
        self.db.executemany("INSERT INTO script_literals VALUES (?,?,?,?,?)", rows)

    # ---- 2da / models ----
    def index_2da(self, fid, resref, data):
        """2da table: one twoda row per cell and the node 2da:<resref>."""
        t = n.read_2da(data.decode(n.ENCODING, "replace"))
        # the game numbers rows by position and ignores the first column's label (BioWare 2DA doc; neverwinter.nim
        # twoda.nim drops it too), so row = position; hand-merged tables often have labels that drift from it
        rows = [(fid, resref, str(i), c, v, label) for i, (label, vals) in enumerate(t.rows)
                for c, v in zip(t.columns, vals)]
        self.db.executemany("INSERT INTO twoda VALUES (?,?,?,?,?,?)", rows)
        drift = [(i, label) for i, (label, _v) in enumerate(t.rows) if label != str(i)]
        if drift:
            self.issue("warning", "2da_row_label", f"2da:{resref}",
                       f"{resref}.2da: {len(drift)} row label(s) differ from the row's position, e.g. "
                       + ", ".join(f"line {i} labelled {label!r}" for i, label in drift[:4]) +
                       " - the game uses the position, so anything that counts by the label points at another row")
        self.node(f"2da:{resref}", "2da", resref, f"{resref}.2da", fid, 1)

    def index_mdl(self, fid, resref, data):
        """Model: a models row (format, supermodel, textures as found by nwnlib.scan_mdl) and a template edge to
        its supermodel (a model inherits animations from its supermodel)."""
        info = n.scan_mdl(data)
        # duplicate detection: a model names itself (newmodel / beginmodelgeom, its root node, usually its own
        # texture), so two copies of one head under different names never share bytes. content_hash masks every
        # occurrence of the model's own name (any case) - in a compiled model together with the zero padding after
        # it, since names sit in fixed-size fields - so such copies get the same hash.
        masked = re.sub(re.escape(resref.encode("latin-1", "replace")) + rb"\x00*", b"\x00<self>\x00", data, flags=re.I)
        self.db.execute("UPDATE files SET content_hash=? WHERE id=?", (sha256(masked), fid))
        self.db.execute("INSERT INTO models VALUES (?,?,?,?,?)",
                        (fid, resref, info["format"], info.get("supermodel"),
                         ",".join(info.get("textures", []))))
        mn = self.node(f"model:{resref}", "model", resref, f"{resref}.mdl", fid, 1)
        if info.get("supermodel"):
            self.edge(mn, self.node(f"model:{info['supermodel']}", "model", info["supermodel"]),
                      "template", "setsupermodel")

    # ---- finishing ----
    def link_names(self):
        """Any file that mentions another file's resref by name 'uses' it (conservative).

        Joins the tokens table (words found in each file) against all resource names inside SQLite, adds one
        name_ref edge per (file, mentioned file), then empties tokens. Also adds companion edges (COMPANIONS).
        A false match only makes a file look used, which is the safe direction."""
        by_name = defaultdict(set)
        for res, nd in self.file_nodes.items():
            r, _, e = res.rpartition(".")
            by_name[r].add(nd)
        self.flush_fields()
        # tokens were written to SQLite in batches while indexing (to bound memory), so the match is a join there
        self.db.execute("CREATE TEMP TABLE resnames(name TEXT PRIMARY KEY)")
        self.db.executemany("INSERT OR IGNORE INTO resnames VALUES (?)", ((r,) for r in by_name))
        seen = set()
        for src, t in self.db.execute("SELECT DISTINCT t.node, t.token FROM tokens t JOIN resnames r ON r.name = t.token"):
            for dst in by_name[t]:
                if dst != src and (src, dst) not in seen:
                    seen.add((src, dst))
                    self.edge(src, dst, "name_ref", f"mentions '{t}'")
        self.db.execute("DROP TABLE resnames")
        self.db.execute("DELETE FROM tokens")
        for res, nd in self.file_nodes.items():
            r, _, e = res.rpartition(".")
            for ce in COMPANIONS.get(e, ()):
                other = self.file_nodes.get(f"{r}.{ce}")
                if other:
                    self.edge(nd, other, "companion", f"{e} needs {ce}")

    def post_process(self):
        """Finish the index after every source is loaded (run once).

        Resolves literals and names, adds tag-based script edges, marks scripts that have a .ncs, records
        conflicts, decides which nodes exist (in_module), runs validate(), then writes nodes, edges, issues and
        concat_literals, creates the indexes and commits. Writes only index.sqlite."""
        nwn_progress.stage("post", "Linking: saving fields")
        self.flush_fields()
        nwn_progress.stage("post", "Linking: script strings")
        self.resolve_literals()
        nwn_progress.stage("post", "Linking: names mentioned inside files")
        self.link_names()
        # a custom talk-table entry with a sound plays that .wav when the line is spoken: a use no GFF field shows
        if self.tlk.custom:
            for i, snd in self.tlk.custom.sounds():
                nd = self.file_nodes.get(f"{snd}.wav")
                if nd:
                    self.edge("module", nd, "name_ref", f"custom talk table entry {i} plays this sound")
        nwn_progress.stage("post", "Linking: tags, conflicts, validation")
        # tag-based scripting: item tag == script name (EE default module events use this)
        scripts = {k[:-4] for k in self.resources if k.endswith(".nss") or k.endswith(".ncs")}
        prefixes = {""} | self.tagbased_prefixes
        # unknown prefixes: a script whose name ends with the item tag (e.g. i_<tag>, x_<tag>): a prefix of 1-4
        # characters ending in "_" and a tag of 4+ characters, so short tags don't match by chance. Every script is
        # filed once under each tag it could serve, so each tag is one lookup instead of a scan of all scripts.
        tag_suffix = defaultdict(set)
        for sc in scripts:
            for cut in range(1, 5):
                if sc[cut - 1:cut] == "_" and len(sc) - cut >= 4:
                    tag_suffix[sc[cut:]].add(sc)
        for tag, nodes in self.tags.items():
            items = [nd for nd in nodes if (nd.startswith("bp:") and nd.endswith(".uti")) or
                     (nd.startswith("inst:") and self.nodes[nd]["label"].startswith("item"))]
            if not items:
                continue
            t = tag.lower()
            hits = {p + t for p in prefixes if p + t in scripts}
            hits |= tag_suffix.get(t, set())
            for sc in hits:
                for nd in items:
                    self.edge(nd, f"script:{sc}", "tag_based_script",
                              "Tag = script name" if sc == t else f"Tag-based script ('{sc[:len(sc) - len(t)]}' prefix)")
        # mark which scripts have compiled .ncs
        for (name,) in self.db.execute("SELECT name FROM scripts").fetchall():
            if f"{name}.ncs" in self.resources:
                self.db.execute("UPDATE scripts SET has_ncs=1 WHERE name=?", (name,))
        # same resource in several sources (module vs hak vs override); listed in source-id (= load) order,
        # e.g. "module | hak:cep2_top" - which copy wins is decided by readers (see the module docstring)
        for res, lst in self.resources.items():
            sids = sorted({s for s, _ in lst})
            if len(sids) > 1:
                srcs = [self.db.execute("SELECT kind||':'||path FROM sources WHERE id=?", (s,)).fetchone()[0]
                        for s in sids]
                srcs = [("module" if x.startswith("module:") else x.split(":", 1)[0] + ":" +
                         re.sub(r"\.hak$", "", os.path.basename(x.split(":", 1)[1].rstrip("/\\")), flags=re.I)) for x in srcs]
                self.db.execute("INSERT INTO conflicts VALUES (?,?)", (res, " | ".join(srcs)))
        # existence of every node: a node first created as a bare reference exists if some loaded source has it
        # (variables always count as existing: they have no file)
        for nid, nd in self.nodes.items():
            if nd["in_module"]:
                continue
            t = nd["type"]
            nm = nd["name"]
            if t == "script" and (f"{nm}.nss" in self.resources or f"{nm}.ncs" in self.resources):
                nd["in_module"] = 1
            elif t == "blueprint" and nm in self.resources:
                nd["in_module"] = 1
            elif t == "conversation" and f"{nm}.dlg" in self.resources:
                nd["in_module"] = 1
            elif t == "tag" and nm in self.tags:
                nd["in_module"] = 1
            elif t == "variable":
                nd["in_module"] = 1
            elif t == "2da" and f"{nm}.2da" in self.resources:
                nd["in_module"] = 1
        self.validate()
        nwn_progress.stage("post", "Linking: writing the dependency graph")
        self.db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)",
                            [(k, v["type"], v["name"], v["label"], v["file_id"], v["in_module"])
                             for k, v in self.nodes.items()])
        self.db.executemany("INSERT INTO edges VALUES (?,?,?,?)", self.edges)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('skipped_files', ?)", (json.dumps(self.skipped[:500]),))
        # read by nwn_analysis's safe-to-delete stage (see index_nss for what they mean)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('tagbased_prefix_unknown', ?)",
                        (json.dumps(self.tagbased_unknown[:50]),))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('dynamic_creates', ?)", (json.dumps(self.dynamic_creates[:500]),))
        if self.skipped:
            self.issue("info", "non_game_files", "module",
                       f"{len(self.skipped)} file(s) in the module folder are not NWN resources and are ignored "
                       f"(not packed into the .mod): " + ", ".join(self.skipped[:6]) +
                       (" …" if len(self.skipped) > 6 else ""))
        self.db.executemany("INSERT INTO issues VALUES (?,?,?,?)", self.issue_rows)
        self.db.executemany("INSERT INTO concat_literals VALUES (?,?,?,?)", self.concat_literals)
        self.db.executescript(INDEXES)
        self.db.commit()

    def is_base_script(self, name):
        """True if script name comes with the base game: from the KEY index when it was read, else by name prefix."""
        if self.base_names:
            return f"{name}.ncs" in self.base_names or f"{name}.nss" in self.base_names
        return name.startswith(BASE_GAME_PREFIXES)

    def validate(self):
        """Structural checks that need no compiler.

        Records issues for: #include cycles, .ncs without .nss, scripts with main()/StartingConditional() but no
        .ncs, libraries nothing includes, missing includes and missing scripts (unless base game), and a script
        used in the wrong role (a condition without StartingConditional, an event/action without main)."""
        inc = defaultdict(set)
        for src, dst, k, v in self.edges:
            if k == "include":
                inc[src].add(dst)
        seen_cycles = set()
        # depth-first search with an explicit stack (no recursion limit on deep include chains); a step to a
        # script already on the current path closes a cycle. Each cycle is reported once (keyed by its members).
        for start in inc:
            stack, path = [(start, iter(inc[start]))], [start]
            onpath = {start}
            while stack:
                node, it = stack[-1]
                nxt = next(it, None)
                if nxt is None:
                    stack.pop(); onpath.discard(path.pop()); continue
                if nxt in onpath:
                    cyc = path[path.index(nxt):] + [nxt]
                    key = frozenset(cyc)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        self.issue("error", "include_cycle", start,
                                   "#include cycle: " + " -> ".join(c[7:] for c in cyc) +
                                   " (the compiler will fail with duplicate definitions)")
                elif nxt in inc and nxt not in onpath:
                    stack.append((nxt, iter(inc[nxt]))); path.append(nxt); onpath.add(nxt)
        scripts = {r[0]: (r[1], r[2], r[3]) for r in
                   self.db.execute("SELECT name, has_main, has_sc, has_ncs FROM scripts")}
        ncs_only = {k[:-4] for k in self.resources if k.endswith(".ncs")} - set(scripts)
        for s in sorted(ncs_only):
            self.issue("warning", "compiled_without_source", f"script:{s}",
                       "Compiled .ncs has no .nss source in the module - cannot be reviewed or rebuilt")
        includes_used = {dst for src, dst, k, v in self.edges if k == "include"}
        for name, (has_main, has_sc, has_ncs) in scripts.items():
            if (has_main or has_sc) and not has_ncs:
                self.issue("warning", "not_compiled", f"script:{name}",
                           "Source has main()/StartingConditional() but no .ncs - will not run until compiled")
            if not has_main and not has_sc and f"script:{name}" not in includes_used:
                self.issue("info", "orphan_library", f"script:{name}",
                           "No main()/StartingConditional() and not #included by anything")
        for src, dst, kind, via in self.edges:
            if not dst.startswith("script:"):
                continue
            name = dst[7:]
            info = scripts.get(name)
            in_mod = info is not None or name in ncs_only
            if kind == "include" and info is None:
                sev = "info" if self.is_base_script(name) else "error"
                self.issue(sev, "missing_include", src,
                           f"#include \"{name}\" not found in module ({via})" +
                           (" - probably base game" if sev == "info" else ""))
            elif kind in ("event_script", "module_event", "dlg_script", "execute_script", "tag_based_script"):
                if not in_mod:
                    if self.is_base_script(name):
                        continue  # base-game script; reported in missing_refs as 'base game'
                    self.issue("error", "missing_script", src, f"{via} -> '{name}' not found in module")
                    continue
                if info is None:
                    continue
                has_main, has_sc, _ = info
                # in a .dlg, a link's Active field is its condition (needs StartingConditional); Script is the action
                if kind == "dlg_script" and via.endswith("/Active") and not has_sc:
                    self.issue("error", "wrong_script_type", src,
                               f"{via} -> '{name}' is used as a condition but has no StartingConditional()")
                elif kind in ("event_script", "module_event", "execute_script") and not has_main:
                    self.issue("error", "wrong_script_type", src,
                               f"{via} -> '{name}' is used as an event/action script but has no main()")
                elif kind == "dlg_script" and via.endswith("/Script") and not has_main:
                    self.issue("error", "wrong_script_type", src,
                               f"{via} -> '{name}' is used as a conversation action but has no main()")


def find_haks(hak_names, explicit=(), search_dirs=()):
    """Locate the module's haks by name. Returns (found paths, missing names).

    hak_names: names from module.ifo (lower case, no extension), in module.ifo order; found keeps that order.
    explicit: hak paths given by the user - an exact name match first, then a loose one (one name is the other
    plus a suffix or prefix at a word boundary, e.g. hak_a and hak_a_v2) when exactly one explicit hak fits and
    that hak is not itself named in module.ifo. search_dirs: folders searched for <name>.hak, case-insensitively.
    Read-only (lists folders)."""
    found, missing = [], []
    exp = {os.path.splitext(os.path.basename(os.path.normpath(h)))[0].lower(): h for h in explicit}
    names = set(hak_names)

    def word_match(a, b):
        """Is `a` found whole inside `b` (not as part of a longer word, so 'core' is not in 'mycore_extras')?"""
        return re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", b) is not None
    for h in hak_names:
        if h in exp:
            found.append(exp[h]); continue
        loose = [k for k in exp if k not in names and (word_match(h, k) or word_match(k, h))]
        if len(loose) == 1:
            found.append(exp[loose[0]]); continue
        hit = None
        for d in search_dirs:
            if d and os.path.isdir(d):
                for fn in os.listdir(d):
                    if fn.lower() == f"{h}.hak":
                        hit = os.path.join(d, fn); break
            if hit:
                break
        (found.append(hit) if hit else missing.append(h))
    return found, missing


def default_hak_dirs(nwn_root=None, nwn_user=None):
    """The hak folders the game may use, in search order: the user folder, the install folder, then the usual
    "Neverwinter Nights" user folders on Windows and macOS (Documents/Neverwinter Nights, also under OneDrive), Linux
    (~/.local/share). Folders may not exist."""
    home = os.path.expanduser("~")
    return [os.path.join(p, "hak") for p in (nwn_user, nwn_root,
            os.path.join(home, "Documents", "Neverwinter Nights"),
            os.path.join(home, "OneDrive", "Documents", "Neverwinter Nights"),
            os.path.join(home, ".local", "share", "Neverwinter Nights")) if p]


def base_tlk_candidates(nwn_root):
    """Where dialog.tlk may be in an install, tried in order: lang/en/data/ (EE, English), then data/ and the
    install root (other layouts). Empty list without nwn_root."""
    return [os.path.join(nwn_root, "lang", "en", "data", "dialog.tlk"), os.path.join(nwn_root, "data", "dialog.tlk"),
            os.path.join(nwn_root, "dialog.tlk")] if nwn_root else []


def locate_custom_tlk(custom_name, nwn_root=None, nwn_user=None):
    """Path of the module's custom tlk in the NWN tlk folders, or None (does not read the file)."""
    if not custom_name:
        return None
    for d in default_hak_dirs(nwn_root, nwn_user):
        td = os.path.join(os.path.dirname(d), "tlk")     # the tlk/ folder sits next to each hak/ folder
        if os.path.isdir(td):
            for fn in os.listdir(td):
                if fn.lower() == f"{custom_name.lower()}.tlk":
                    return os.path.join(td, fn)
    return None


def find_tlks(custom_name, explicit=None, nwn_root=None, nwn_user=None):
    """Locate dialog.tlk (base) and the module's custom tlk. Returns (TlkSet, info dict).

    custom_name: Mod_CustomTlk from module.ifo (no extension). explicit: a custom .tlk path given by the user,
    used instead of searching. info: base / custom (paths or None), custom_name, custom_missing (True when the
    module names a custom tlk that was not found). A base candidate that is not a TLK file is skipped; a custom
    file that is not one raises ValueError. Read-only."""
    info = dict(base=None, custom=None, custom_name=custom_name or "", custom_missing=False)
    base = None
    for cand in base_tlk_candidates(nwn_root):
        if os.path.isfile(cand):
            try:
                base = n.Tlk(cand); info["base"] = cand; break
            except ValueError:
                pass
    custom = None
    if explicit and os.path.isfile(explicit):
        custom = n.Tlk(explicit); info["custom"] = explicit
    elif custom_name:
        p = locate_custom_tlk(custom_name, nwn_root, nwn_user)
        if p:
            custom = n.Tlk(p); info["custom"] = p
        else:
            info["custom_missing"] = True
    return n.TlkSet(base, custom), info


def default_overrides(nwn_user):
    """[<user>/override] when that folder has files, else []. The game loads it after the haks and the module (they
    win over it, see nwnlib.source_rank), so the index reads it to know what a builder's own game shows."""
    if not nwn_user:
        return []
    d = os.path.join(nwn_user, "override")
    return [d] if os.path.isdir(d) and any(True for _ in os.scandir(d)) else []


def run_index(module_path, haks=(), overrides=None, tlk=None, out=None, write_json=True, verbose=True,
              nwn_root=None, nwn_user=None, time_budget=None):
    """
    Index a module (+haks/overrides) into <out>/index.sqlite.
    time_budget (seconds): stop cleanly when it is spent and write <out>/index.state; calling again with the
    same arguments resumes. Returns out when finished, or None when checkpointed (call again).

    module_path: module folder or .mod. haks: extra hak paths (those named in module.ifo are found by name in
    the NWN hak folders). overrides: override folders; None (the default) = <nwn_user>/override if it has files, [] =
    none (the clean-build audit indexes that way: players don't have the builder's override folder). tlk: custom
    .tlk path. out: analysis folder (default ./nwn_analysis/<module>). nwn_root / nwn_user: game install and user
    folders (base-game names, dialog.tlk, hak and tlk folders). Order of work: module.ifo is read first for the
    custom tlk name, then the module, then the haks it lists, then the overrides, then post_process.
    Never writes outside out; a fresh run (no time_budget, or no index.state) replaces out/index.sqlite.
    """
    t0 = time.time()
    overrides = default_overrides(nwn_user) if overrides is None else list(overrides)
    mod_name = os.path.splitext(os.path.basename(os.path.normpath(module_path)))[0]
    out = out or os.path.join(os.getcwd(), "nwn_analysis", mod_name)
    state_path = os.path.join(out, "index.state")
    if time_budget and os.path.exists(state_path):
        return _resume_index(module_path, haks, overrides, tlk, out, write_json, verbose, nwn_root, nwn_user,
                             time_budget, state_path, t0)
    if os.path.exists(state_path):
        os.remove(state_path)      # a run without a budget starts fresh: an old checkpoint no longer matches
    # a nasher project (GFF files as JSON text): converted into an ordinary module folder inside `out` first, and that
    # copy is indexed (builds start from it). The project folder is only read. A resumed run keeps using the copy.
    module_source, conv = None, None
    import nwn_nasher
    if nwn_nasher.is_project(module_path):
        module_source = os.path.abspath(module_path)
        os.makedirs(out, exist_ok=True)
        conv = nwn_nasher.convert(module_path, os.path.join(out, nwn_nasher.CONVERTED))
        module_path = conv["dest"]
    # peek at module.ifo for the custom tlk name so names resolve during indexing
    # (best effort: a damaged module.ifo is reported later, when the module itself is indexed)
    custom_name = ""
    try:
        if os.path.isdir(module_path):
            ifo = os.path.join(module_path, "module.ifo")
            root_ = n.read_gff_file(ifo) if os.path.isfile(ifo) else None
        else:
            erf_ = n.Erf(module_path)
            try:
                e_ = next((e for e in erf_.entries if e.filename == "module.ifo"), None)
                root_ = n.read_gff(erf_.read(e_)) if e_ else None
            finally:
                erf_.close()      # add_source opens the .mod again; an open handle would lock it on Windows
        custom_name = (root_.get("Mod_CustomTlk") or "") if root_ else ""
    except Exception:  # noqa
        pass
    if nwn_progress.ENABLED or nwn_progress.hooked():
        try:
            import nwn_quickscan
            pl = nwn_quickscan.plan_sources(module_path, haks, overrides, nwn_root, nwn_user)
            nwn_progress.emit(phase="plan", units=round(pl["units"]), files=pl["files"], bytes=pl["bytes"],
                              sources=[dict(name=s_["name"], kind=s_["kind"], files=s_["files"]) for s_ in pl["sources"]])
        except Exception as ex:  # noqa - progress is best-effort, never blocks the run
            ix_note = f"(progress plan unavailable: {ex})"
            print(ix_note, flush=True)
    nwn_progress.emit(phase="index", done_units=0, done_files=0, source="module.ifo")
    tlkset, tlk_info = find_tlks(custom_name, tlk, nwn_root, nwn_user)
    ix = Indexer(out, tlkset, write_json, verbose)
    ix.log(f"Indexing '{mod_name}' -> {out}")
    if tlk_info["custom"] or tlk_info["base"]:
        ix.log(f"  tlk: base={tlk_info['base'] or 'none'} custom={tlk_info['custom'] or 'none'}")
    for o in overrides:
        ix.log(f"  override folder: {o}  (the haks and the module win over it, as in the game)")
    if tlk_info["custom_missing"]:
        ix.issue("error", "tlk_not_found", "module", f"module.ifo names custom talk table '{custom_name}.tlk' but it "
                 "was not found in the NWN tlk folder - custom names/descriptions cannot be checked")
    ix.tlk_info = tlk_info
    if time_budget:
        ix.budget = (t0 + time_budget, state_path)
    ix.db.execute("INSERT INTO meta VALUES ('module_path', ?)", (os.path.abspath(module_path),))
    ix.db.execute("INSERT INTO meta VALUES ('indexed_at', ?)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
    ix.db.execute("INSERT INTO meta VALUES ('tlk_custom_name', ?)", (custom_name,))
    if conv:
        ix.db.execute("INSERT INTO meta VALUES ('module_source', ?)", (module_source,))
        ix.db.execute("INSERT INTO meta VALUES ('module_format', 'nasher json')")
        ix.log(f"  nasher project: {module_source} - {conv['converted']} JSON file(s) converted and {conv['copied']} "
               f"copied into {module_path} (the project folder is only read)")
        for e in conv["errors"]:
            ix.issue("error", "json_unreadable", "module", f"{e} - left out of the analysis; fix the file (or run "
                     "nasher pack and analyse the .mod) and analyse again")
        for name, used, ignored in conv["clashes"]:
            ix.issue("warning", "json_duplicate_name", f"file:{name}", f"the project holds {name} twice: {used} was "
                     f"used, {ignored} ignored - a module holds one file per name; remove or rename one of them")
    try:
        ix.add_source(module_path, "module", 0)
    except Checkpoint as cp:
        ix.save_state(state_path, dict(sources=[[module_path, "module", 0, cp.sid]], pos=(0, cp.j), phase="sources"))
        ix.log(f"  checkpoint: module file {cp.j} (run again to continue)")
        ix.db.close()
        return None
    plan = _plan_after_module(ix, haks, overrides, nwn_root, nwn_user, tlk_info)
    return _index_sources(ix, plan, 0, 0, state_path, t0, time_budget, out)


def _plan_after_module(ix, haks, overrides, nwn_root, nwn_user, tlk_info):
    """Once the module itself is indexed: find its haks, record the settings in meta, read the base game's resource
    names, and return the plan of the sources still to load. Used by the fresh run and by a resumed run whose
    module was interrupted, so both do exactly the same.

    ix: the Indexer after add_source(module). haks / overrides / nwn_root / nwn_user: as run_index. tlk_info: from
    find_tlks. Returns [[path, kind, priority, source id or None], ...] - haks in module.ifo order, then overrides;
    the source id is filled in once a source is started, and the list is saved with every checkpoint so a resumed
    run continues the same plan."""
    # OR REPLACE / IF NOT EXISTS: a resumed run whose previous step was killed after this had run (but before its
    # state was written) does it again on the same database
    hak_names = [ix.nodes[d]["name"] for s_, d, k, v in ix.edges if k == "hak"]
    found, missing = find_haks(hak_names, haks, default_hak_dirs(nwn_root, nwn_user))
    for h in haks:  # explicitly given haks not listed in module.ifo are still loaded
        if h not in found:
            found.append(h)
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('haks_missing', ?)", (json.dumps(missing),))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('haks', ?)", (json.dumps([os.path.abspath(h) for h in found]),))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('nwn_root', ?)", (nwn_root or "",))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('nwn_user', ?)", (nwn_user or "",))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('tlk', ?)", (tlk_info.get("custom") or "",))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('tlk_base', ?)", (tlk_info.get("base") or "",))
    for h in missing:
        ix.issue("error", "hak_not_loaded", f"hak:{h}",
                 f"module.ifo uses hak '{h}.hak' but it was not found/loaded - anything it provides (2das, "
                 "scripts, models) is invisible, so unused-file results are downgraded to Review")
    base = n.base_game_names(nwn_root)
    if nwn_root and not base:
        ix.issue("warning", "base_game_not_found", "module",
                 f"the NWN install folder '{nwn_root}' has no data/*.key files, so the base game was not read - "
                 "base-game scripts, blueprints and textures can't be told apart from missing ones. Set the install "
                 "folder (the one containing 'data') in Settings and re-analyse")
    ix.db.execute("CREATE TABLE IF NOT EXISTS base_resources(name TEXT PRIMARY KEY)")
    ix.db.executemany("INSERT OR IGNORE INTO base_resources VALUES (?)", ((b,) for b in base))
    ix.db.execute("INSERT OR REPLACE INTO meta VALUES ('base_index', ?)", (str(len(base)),))
    ix.base_names = base
    return [[h, "hak", i + 1, None] for i, h in enumerate(found)] + \
           [[o, "override", 100 + i, None] for i, o in enumerate(overrides)]


def _index_sources(ix, plan, si, j, state_path, t0, time_budget, out):
    """Load every source in plan from (si, j); checkpoint when the budget runs out.

    si: index in plan of the source to start with; j: item to start at within that source. After the last
    source, post_process runs (unless less than 20 s of the budget is left: then a "post" checkpoint is saved).
    Returns out when the index is complete (index.state removed), None after a checkpoint."""
    for k in range(si, len(plan)):
        path, kind, prio, sid = plan[k]
        try:
            plan[k][3] = ix.add_source(path, kind, prio, start=j if k == si else 0, sid=sid)
        except Checkpoint as cp:
            plan[k][3] = cp.sid
            ix.save_state(state_path, dict(sources=plan, pos=(k, cp.j), phase="sources"))
            ix.log(f"  checkpoint: source {k + 1}/{len(plan)} file {cp.j} (run again to continue)")
            ix.db.close()
            return None
    if time_budget and time.time() > ix.budget[0] - 20:
        # not enough time left for post-processing in this chunk: save and let the next call do it
        ix.save_state(state_path, dict(sources=plan, pos=(len(plan), 0), phase="post"))
        ix.log("  checkpoint: all sources loaded; post-processing on the next run")
        ix.db.close()
        return None
    ix.budget = None
    nwn_progress.emit(phase="post", stage="Linking everything together")
    ix.post_process()
    ix.log(f"  files: {sum(ix.stats.values())}  nodes: {len(ix.nodes)}  edges: {len(ix.edges)}  "
           f"issues: {len(ix.issue_rows)}  ({time.time() - t0:.1f}s)")
    ix.db.close()
    if os.path.exists(state_path):
        os.remove(state_path)
    return out


def _resume_index(module_path, haks, overrides, tlk, out, write_json, verbose, nwn_root, nwn_user,
                  time_budget, state_path, t0):
    """Continue a checkpointed run from index.state (arguments as run_index). Returns as _index_sources.

    Three cases: the module itself was interrupted (finish it, then _plan_after_module exactly as the fresh run
    does), a hak/override was interrupted, or only post-processing is left."""
    custom_name = ""
    try:
        db = sqlite3.connect(os.path.join(out, "index.sqlite"))
        custom_name = db.execute("SELECT value FROM meta WHERE key='tlk_custom_name'").fetchone()
        custom_name = custom_name[0] if custom_name else ""
        db.close()
    except Exception:  # noqa
        pass
    tlkset, tlk_info = find_tlks(custom_name, tlk, nwn_root, nwn_user)
    ix = Indexer(out, tlkset, write_json, verbose, resume=True)
    progress = ix.load_state(state_path)
    ix.tlk_info = ix.tlk_info or tlk_info
    ix.budget = (t0 + time_budget, state_path)
    ix.log(f"Resuming index of '{os.path.basename(module_path)}' ({progress['phase']}, "
           f"{sum(ix.stats.values())} files so far)")
    plan = progress["sources"]
    si, j = progress["pos"]
    if progress["phase"] == "sources" and si == 0 and plan and plan[0][1] == "module":
        # module itself was interrupted: finish it, then discover haks exactly as the fresh run does
        path, kind, prio, sid = plan[0]
        try:
            ix.add_source(path, kind, prio, start=j, sid=sid)
        except Checkpoint as cp:
            ix.save_state(state_path, dict(sources=plan, pos=(0, cp.j), phase="sources"))
            ix.log(f"  checkpoint: module file {cp.j} (run again to continue)")
            ix.db.close()
            return None
        plan = _plan_after_module(ix, haks, overrides, nwn_root, nwn_user, tlk_info)
        return _index_sources(ix, plan, 0, 0, state_path, t0, time_budget, out)
    if progress["phase"] == "post":
        # time_budget=None: post-processing is not split, so it always runs to the end in this call
        return _index_sources(ix, plan, len(plan), 0, state_path, t0, None, out)
    return _index_sources(ix, plan, si, j, state_path, t0, time_budget, out)


def main(argv=None):
    """Command line: index one module (no time budget). See the module docstring for the arguments."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module", help="expanded module folder, or a .mod file")
    ap.add_argument("--hak", action="append", default=[], help="hak file or folder (repeatable)")
    ap.add_argument("--override", action="append", default=[], help="override folder (repeatable)")
    ap.add_argument("--tlk", help="dialog.tlk (resolves names stored as talk-table numbers)")
    ap.add_argument("--out", help="output folder (default ./nwn_analysis/<module>)")
    ap.add_argument("--no-json", action="store_true", help="skip writing per-file JSON")
    a = ap.parse_args(argv)
    run_index(a.module, a.hak, a.override or None, a.tlk, a.out, not a.no_json)


if __name__ == "__main__":
    main()
