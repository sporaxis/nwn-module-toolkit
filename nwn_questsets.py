"""
nwn_questsets.py - sort the quests found in the scripts into families and sets, so 700 entries read as a handful of
systems instead of one long list.

Each inferred quest gets:
  family       - "Q_TOKENS quest tokens (q_tokens_inc)", "DMFI", "Jasperre's AI", "Module quests", ...
  family_kind  - "quests"   : real quest state (token strings, the journal, player variables tied to a conversation)
                 "package"  : a known community package keeping its own settings/state (DMFI, Jasperre's AI, BioWare...)
                 "system"   : the module's own systems and settings (module-wide or not tied to any conversation)
  set          - a family of names inside it: QT_* (QT_DONEANTS, QT_DONEKOBOLDS...), HUT_*, DUKES*...
  name         - the constant / variable name that is the quest's key (QT_DONEOGRE), without the comment

A package is recognised from the variable name or from the scripts that use it (more than half of them), never from
one stray script. The package table lists only systems whose names/prefixes are unambiguous.

Reads:  the inferred-quests dict from nwn_quests.QuestInference.run(), the variable audit from nwn_varaudit.run()
        (for token-slot details), and an optional callable that returns an include's header comment.
Writes: nothing on disk. classify() adds keys to the dict it is given, in place. This is presentation only: no
        quest's health or problems are changed.
Entry point: classify(inferred, var_audit, header_of).
Limits: grouping is by name patterns and script prefixes, so a module that names things inconsistently gets more
"(on its own)" entries; a package not in PACKAGES is treated as the module's own.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict

# (id, display name, what it is, variable-name pattern, script-name pattern)
# Patterns are prefixes: r"^(dmfi|dmw)_" matches the variable "dmfi_voice" and the script "dmw_wand"; r"^AI_" matches
# Jasperre's "AI_INTELLIGENCE". The variable test is searched in the quest's name, the script test in each script name.
PACKAGES = [
    ("dmfi", "DMFI (DM Friendly Initiative)", "DM tools: wands, emotes, voice, dice, languages - settings and state, not quests",
     r"^(dmfi|dmw)_|^dmfi/|^hls_", r"^(dmfi|dmw)_"),
    ("jasperre", "Jasperre's AI", "creature combat AI - its own settings and state on creatures and the module",
     r"^AI_|JASPERRE", r"^j_(ai|inc)_"),
    ("henchman", "Henchman Inventory & Battle AI (Tony K)", "henchman AI and radial-menu commands",
     r"^HENCH_", r"^hench_"),
    ("bioware", "BioWare standard scripts (NW_ / X0_-X3_)", "base-game and expansion switches and state (horses, XP2, spells...)",
     r"^(NW|X0|X1|X2|X3)_", r"^(nw|x0|x1|x2|x3)_"),
    ("cep", "CEP (Community Expansion Pack) scripts", "CEP creature and placeable scripts", r"^ZEP_", r"^zep_"),
    ("nwnx", "NWNX (server extender)", "server plug-in settings and state", r"^NWNX", r"^nwnx_"),
    ("sas", "Simple Addiction System (M. Janicki)", "addictive items and withdrawal", r"^SAS_", r"^sas_"),
    ("sunjammer", "Sunjammer's TileMagic / debug library", "tile effects and debug settings", r"^SJ_", r"^sj_"),
    ("wdm", "Dead and Wild Magic System (D. Anderson)", "dead/wild magic areas", r"^WDM_", r"^wdm_"),
]
# Variable patterns are case-insensitive only for DMFI and BioWare (their names appear in either case: nw_/NW_);
# for the others only the package's own spelling counts (AI_..., HENCH_...). Script patterns are always as written.
_PK = [(i, nm, desc, re.compile(v, re.I if i in ("dmfi", "bioware") else 0), re.compile(s)) for i, nm, desc, v, s in PACKAGES]


def _const_name(q):
    """The constant / variable name that keys the quest: QT_DONEHOBS (not 'hobgoblin quest for the marshal (QT_DONEHOBS)')."""
    for e in q.get("key_exprs") or []:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", e or ""):
            return e
    lab = q.get("label") or q.get("key") or ""
    m = re.search(r"\(([A-Za-z_][A-Za-z0-9_]*)\)$", lab)
    return m.group(1) if m else lab


def _tokens(name):
    """Lower-case words of a name, split on _ space / : . - ("QT_DONE_ANTS" -> ["qt", "done", "ants"]).
    Trailing digits of the last word become a word of their own ("Q_RATS2" -> ["q", "rats", "2"]), so Q_RATS1,
    Q_RATS2... share the prefix "q_rats" (name_sets never uses a name's last word as a prefix)."""
    t = [x for x in re.split(r"[_\s/:.-]+", name.lower()) if x]
    if t:
        m = re.search(r"\d+$", t[-1])
        if m and m.start() > 0:
            t[-1:] = [t[-1][:m.start()], m.group(0)]
    return t


def name_sets(names, lo=2, hi=60):
    """name -> set label. Shortest shared start of words (QT_*), else the longest shared start of 5+ letters
    among names without separators (DUKESARMY, DUKESWIZARD -> DUKES*). Unmatched -> None.

    names: list of quest names; lo/hi: a set must have between lo and hi members (2..60), so a prefix shared by
    almost everything (or by one name only) is not a set. Returns {name: "QT_*" | None}."""
    toks = {n: _tokens(n) for n in names}
    # how many names start with each word-prefix ("qt", "qt_done"...)
    cnt = Counter()
    for t in toks.values():
        for k in range(1, len(t)):          # a prefix of words, never the whole name
            cnt["_".join(t[:k])] += 1
    out = {}
    for n, t in toks.items():
        # the shortest word-prefix with a usable group size wins
        for k in range(1, len(t)):
            p = "_".join(t[:k])
            if lo <= cnt[p] <= hi:
                # keep the name's own spelling and separator ("QT_") when it was written with "_"; after a
                # split-off digit word there is no separator ("Q_RATS2" -> "Q_RATS")
                sep = n[len(p):len(p) + 1]
                out[n] = n[:len(p)] + (sep if sep and not sep.isalnum() else "") if n[:len(p)].lower() == p else p.upper() + "_"
                break
    # fallback for names without separators: group by the first 5 letters/digits, label = longest common start
    rest = [n for n in names if n not in out]
    low = {n: re.sub(r"[^a-z0-9]", "", n.lower()) for n in rest}
    by5 = defaultdict(list)
    for n in rest:
        if len(low[n]) >= 5:
            by5[low[n][:5]].append(n)
    for p5, members in by5.items():
        if lo <= len(members) <= hi:
            common = os.path.commonprefix([low[m] for m in members])
            for m in members:
                out[m] = m[:len(common)] if m[:len(common)].lower() == common else common.upper()
    return {n: (out[n] + "*" if n in out else None) for n in names}


def _script_prefix(scripts):
    """The most common text before the first "_" among script names ("ms_spawn", "ms_rest" -> "ms"), or None."""
    c = Counter(re.split(r"_", s)[0] for s in scripts if "_" in s)
    return c.most_common(1)[0][0] if c else None


def classify(inferred, var_audit=None, header_of=None):
    """Add family / family_kind / set / name to every inferred quest and a 'families' summary to inferred.

    inferred:  the dict from nwn_quests (summary, quests, hubs); changed in place and also returned.
    var_audit: nwn_varaudit.run() result or None; its token systems add slot counts to token families.
    header_of: callable(script name) -> that script's header comment ("" when unknown); used to describe a token
               include in one line.
    Family order in inferred["families"]: quests, then the module's systems, then packages; larger first."""
    quests = inferred.get("quests") or []
    adapters = {a.get("defined_in"): a for a in (inferred.get("summary") or {}).get("adapters", [])}
    header_of = header_of or (lambda _n: "")
    tok_sys = {t["system"]: t for t in (var_audit or {}).get("tokens", [])}
    fam_info = {}

    def token_family(q):
        """Family name for a token quest, from the include that defines its helpers:
        q_tokens_inc -> "Q_TOKENS quest tokens (q_tokens_inc)". Registers the family's description once."""
        sysname = q.get("system") or ""
        inc = sysname.split("@", 1)[1] if "@" in sysname else None
        if not inc:
            inc = (tok_sys.get(sysname) or {}).get("defined_in") or sysname
        short = re.sub(r"_?(include|inc)$", "", inc, flags=re.I).upper() or inc
        setter = (tok_sys.get(sysname) or {}).get("setter") or (adapters.get(inc) or {}).get("setter") or "a helper"
        name = f"{short} quest tokens ({inc})"
        # first line of the include's header comment, cut before "Updated/Created/Version/Author/By" or "//", 60 chars
        head = re.split(r"\s+(?:Updated|Created|Version|Author|By)\b|\s*//", (header_of(inc) or "").strip().split("\n")[0])[0][:60]
        fam_info.setdefault(name, dict(kind="quests", description=f"quest slots in one string on the player, set with "
                                       f"{setter} in {inc}" + (f" - '{head}'" if head else ""), system=sysname, include=inc))
        return name

    # DM-tool names: "dm" at the start or after _ / -, or "wand" anywhere ("dm_tools", "x_dmwand", "pt_dm_menu")
    DM_RE = re.compile(r"(^|[_\-])dm|wand|^pt_dm|dmfi|dmw", re.I)

    def package_of(name, scripts):
        """(id, display name, description) of the package this name or script set belongs to, else None."""
        # a package's own variable prefix wins over any script majority (X3_HORSE_* is BioWare even when set by henchman scripts)
        for pid, pname, desc, vre, sre in _PK:
            if vre.search(name):
                return pid, pname, desc
        for pid, pname, desc, vre, sre in _PK:
            if scripts and sum(1 for s in scripts if sre.search(s)) * 2 > len(scripts):
                return pid, pname, desc
        return None

    for q in quests:
        name = _const_name(q)
        q["name"] = name
        scripts = q.get("scripts") or []
        fam = kind = None
        if q.get("kind") == "token":
            fam, kind = token_family(q), "quests"
        elif q.get("kind") == "journal":
            pk = package_of("", scripts) if scripts else None
            if pk:      # e.g. BioWare's nw_ch_ac7 adding the 'Henchman' entry the module's journal doesn't have
                fam, kind = pk[1], "package"
                fam_info.setdefault(fam, dict(kind="package", description=pk[2], id=pk[0]))
            else:
                fam, kind = "Journal quests", "quests"
                fam_info.setdefault(fam, dict(kind="quests", description="quests in the module's journal"))
        else:
            pk = package_of(name, scripts)
            if pk:
                fam, kind = pk[1], "package"
                fam_info.setdefault(fam, dict(kind="package", description=pk[2], id=pk[0]))
        if not fam:
            # The module's own state. A DM tool when every conversation (or every script) looks like one; a quest
            # when it is on the player (or per-player in the database) and a conversation drives it; else a system.
            convs = [str(c) for c in (q.get("conversations") or [])]
            dm_tool = (convs and all(DM_RE.search(c.split(":", 1)[-1]) for c in convs)) or \
                      (scripts and all(DM_RE.search(s_) for s_ in scripts))
            quest_like = (q.get("scope") == "pc" or q.get("kind") == "campaign") and (convs or q.get("givers"))
            if dm_tool:
                fam, kind = "DM tools (the module's own)", "system"
                fam_info.setdefault(fam, dict(kind="system", description="state of the module's DM wands and DM "
                                              "conversations - settings, not quests"))
            elif quest_like:
                fam, kind = "Module quests (player variables)", "quests"
                fam_info.setdefault(fam, dict(kind="quests", description="variables on the player (or per-player "
                                              "database entries) that conversations set or check - quests and "
                                              "quest-like progress"))
            else:
                fam, kind = "Module systems & settings", "system"
                fam_info.setdefault(fam, dict(kind="system", description="module-wide or player state no conversation "
                                              "touches: the module's own systems, settings, counters and the database"))
        q["family"], q["family_kind"] = fam, kind

    # sets inside each family: by the quest names; module systems by the scripts' prefix when the name gives none
    by_fam = defaultdict(list)
    for q in quests:
        by_fam[q["family"]].append(q)
    for fam, qs in by_fam.items():
        sets = name_sets([q["name"] for q in qs])
        # quests with no shared name but the same quest-giver conversation belong together too
        hub_n = Counter(q.get("hub") for q in qs if not sets.get(q["name"]) and str(q.get("hub", "")).startswith("dlg:"))
        for q in qs:
            s = sets.get(q["name"])
            if not s and hub_n.get(q.get("hub"), 0) >= 2:
                s = "given in " + q["hub"][4:]
            if not s and fam_info[fam]["kind"] != "quests":
                p = _script_prefix(q.get("scripts") or [])
                s = f"scripts {p}_*" if p else None
            q["set"] = s or "(on its own)"

    # one summary row per family; token families also get the slot facts from the variable audit
    order = {"quests": 0, "system": 1, "package": 2}
    families = []
    for fam, qs in by_fam.items():
        info = fam_info[fam]
        f = dict(name=fam, kind=info["kind"], description=info["description"], quests=len(qs),
                 health=dict(Counter(q.get("health") for q in qs)), sets=len({q["set"] for q in qs}))
        t = tok_sys.get(info.get("system") or "")
        if t:
            f["named_unused"] = [dict(slot=s["slot"], names=s["names"]) for s in t.get("slots", [])
                                 if s.get("status") == "named, unused"]
            f["free_slots"] = len(t.get("free") or [])
            f["length"] = t.get("length")
        families.append(f)
    families.sort(key=lambda f: (order.get(f["kind"], 3), -f["quests"]))
    inferred["families"] = families
    return inferred
