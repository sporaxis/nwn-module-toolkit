"""
nwn_modsettings.py - the module's settings at a glance, and the SERVER's settings (settings.tml, the server's
environment) read against the module, so it is clear what is actually switched on.

    module_summary(db, report)          module.ifo settings, module event scripts, module variables (toolset), the
                                        switches / module variables scripts set, and a count of all variables
    parse_server_file(text, name)       settings.tml (TOML) or an environment file (KEY=VALUE, docker-compose
                                        'environment:' lists) -> {kind, values{flat key: value}} with secrets hidden
    interpret(configs, db, summary)     the settings that matter, in plain words, plus findings such as
                                        "scripts use NWNX Creature but NWNX_CREATURE_SKIP turns it off"

Values of secret-looking keys (passwords, keys, tokens) and passwords inside URLs are never stored: only whether
they are set.

How secrets are kept out
------------------------
parse_server_file() replaces the value of every key whose name looks secret (SECRET_RE: "pass", "pwd", "secret",
"token", "credential", "api key", "private", "auth", "cdkey", "webhook", "dsn", "connection string" anywhere in the
name, and "pw" or "key" as a whole word; any case) with "(set - hidden)" or "(empty)" before
it returns. The dashboard only ever keeps that masked result (server_config.json), so a password typed into a
settings file never reaches the disk or the page. Masking is by key NAME, plus one pattern in the value: a
password inside a URL (scheme://user:password@host, e.g. a database connection URL) is replaced by "(hidden)"
whatever the key is called, and the rest of the URL is kept. Any other secret inside the value of an
innocent-looking key is not detected.

What it reads and writes
------------------------
Reads the analysis index (sqlite3.Connection: SELECT queries on files, sources, fields, scripts, edges), the
analysis's report dict, and server-file text handed to it by the caller. It never opens files itself and never
writes anything; the caller decides what to save.

Limits
------
Script-set switches are found by a one-line regex: a call split over several lines, or one that sets a module
variable through a helper function, is not listed. Without Python 3.11's tomllib, settings.tml is read by a simple
line reader (sections and key = value only). NWNX plugin use is inferred from #include of nwnx_<plugin>... scripts.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# (module.ifo top-level field label, text shown on the page), in display order
IFO_LABELS = [
    ("Mod_Name", "Name"), ("Mod_Tag", "Tag"), ("Mod_MinGameVer", "Needs game version"),
    ("Expansion_Pack", "Expansions needed (1 = SoU, 2 = HotU, 3 = both)"), ("Mod_Entry_Area", "Start area"),
    ("Mod_StartMovie", "Start movie"), ("Mod_XPScale", "XP scale (% of normal kill XP; 0 = no XP from kills)"),
    ("Mod_DawnHour", "Dawn hour"), ("Mod_DuskHour", "Dusk hour"), ("Mod_MinPerHour", "Real minutes per game hour"),
    ("Mod_StartYear", "Start year"), ("Mod_StartMonth", "Start month"), ("Mod_StartDay", "Start day"),
    ("Mod_StartHour", "Start hour"), ("Mod_CustomTlk", "Custom talk table"), ("Mod_DefaultBic", "Default character"),
    ("Mod_PartyControl", "Party control"), ("Mod_IsSaveGame", "Is a saved game"), ("Mod_Version", "Module format version"),
]
# A key name that matches is treated as a secret. Deliberately broad (any case): "NWN_DMPASSWORD",
# "server.login.player-password", "REDIS_AUTH", "DB_PWD", "ADMIN_PW", "NWN_KEY", "NWN_CDKEY", "DISCORD_WEBHOOK_URL" (a
# Discord/Slack webhook URL is itself a secret), "SENTRY_DSN", "DB_CONNECTION_STRING" / "connstr" all match; hiding a
# harmless value is better than showing a password. "pw" and "key" count only as a whole word between _ - . or the
# ends, so names such as "NWN_PVP" or "MONKEYS" stay visible. Also used by nwn_database for variable/column names.
SECRET_RE = re.compile(r"pass|pwd|secret|token|credential|api[_-]?key|private|auth|cdkey|webhook|dsn|"
                       r"conn(ection)?[_.\-]?str|(^|[_.\-])(pw|key)($|[_.\-])", re.I)
# spellings of on/off in env files and settings.tml (NWNX uses y/n)
TRUE = {"1", "y", "yes", "true", "on", "t"}
FALSE = {"0", "n", "no", "false", "off", "f"}


def _ifo_fields(db):
    """(file id, [(path, label, type, value)]) of module.ifo, or (None, []) if the analysis has none.
    Sources are ordered by priority, and the module itself has priority 0, so its own module.ifo is taken."""
    row = db.execute("SELECT f.id FROM files f JOIN sources s ON s.id=f.source_id WHERE lower(f.relpath)='module.ifo' "
                     "ORDER BY s.priority LIMIT 1").fetchone()
    if not row:
        return None, []
    return row[0], db.execute("SELECT path, label, type, value FROM fields WHERE file_id=?", (row[0],)).fetchall()


def module_summary(db, report=None):
    """The module's own settings for the Overview page.

    db: the analysis index, sqlite3.Connection. report: the analysis's report.json as a dict (only var_audit is
    used), or None.
    Returns dict(settings, notes, events, haks, module_vars, script_sets, variables), or dict(error=...) when the
    analysis has no module.ifo. Read-only."""
    fid, rows = _ifo_fields(db)
    if fid is None:
        return dict(error="no module.ifo in this analysis")
    top = {p: v for p, lab, t, v in rows if "/" not in p}      # top-level fields only, not list entries
    settings = [dict(key=k, label=lab, value=top.get(k, "")) for k, lab in IFO_LABELS if k in top]
    xp = top.get("Mod_XPScale")
    notes = []
    if xp == "0":
        notes.append("XP scale is 0: players get no XP for kills - XP comes only from scripts (quests, rewards, RP tools).")
    events = [dict(event=k[6:] if k.startswith("Mod_On") else k, script=v) for k, v in top.items()
              if k.startswith("Mod_On")]
    haks = [v for p, lab, t, v in rows if lab == "Mod_Hak"]   # each Mod_HakList entry holds one Mod_Hak name
    toolset_vars = defaultdict(dict)
    for p, lab, t, v in rows:
        # variables set in the toolset are stored as a VarTable list: "VarTable[3]/Name", "VarTable[3]/Value"...
        m = re.match(r"VarTable\[(\d+)\]/(Name|Type|Value)$", p)
        if m:
            toolset_vars[int(m.group(1))][m.group(2)] = v
    vtypes = {"1": "int", "2": "float", "3": "string", "4": "object", "5": "location"}
    module_vars = [dict(name=v.get("Name"), type=vtypes.get(str(v.get("Type")), v.get("Type")), value=v.get("Value"))
                   for _, v in sorted(toolset_vars.items())]
    # switches and module variables that scripts set (SetModuleSwitch, SetLocalX(GetModule(), ...))
    on_load = (top.get("Mod_OnModLoad") or "").lower()
    # One call on one line, ending in ");". Matches, for example:
    #   SetModuleSwitch(MODULE_SWITCH_ENABLE_TAGBASED_SCRIPTS, TRUE);     -> key = the constant, value = TRUE
    #   SetLocalInt(GetModule(), "X_ON", GetLocalInt(oPC, "y"));          -> key = "X_ON" (value up to the last ")")
    #   SetLocalString(oModule, "srv", "a");                              -> a variable named oMod... = the module
    # Groups: function, target (None for SetModuleSwitch), key (string or CONSTANT), value (at most 80 characters).
    set_re = re.compile(r"\b(SetModuleSwitch|SetLocal(?:Int|String|Float))\s*\(\s*(GetModule\s*\(\s*\)\s*,|oMod\w*\s*,)?\s*"
                        r"(\"[^\"]*\"|[A-Z_][A-Z0-9_]*)\s*,\s*([^;]{1,80})\)\s*;")
    script_sets = []
    # the LIKE filter lets sqlite skip scripts that cannot match, so the regex runs on few sources
    for nm, src in db.execute("SELECT name, source FROM scripts WHERE source LIKE '%SetModuleSwitch%' "
                              "OR source LIKE '%GetModule()%' OR source LIKE '%oMod%'"):
        for ln, text in enumerate((src or "").replace("\r\n", "\n").split("\n"), 1):
            if text.lstrip().startswith("//"):
                continue                   # a commented-out line (block comments are not recognised)
            for m in set_re.finditer(text):
                fn, target, key, val = m.groups()
                if fn != "SetModuleSwitch" and not target:
                    continue           # SetLocalInt on some other object
                script_sets.append(dict(name=key.strip('"'), value=val.strip(), how=fn, script=nm, line=ln,
                                        on_module_load=nm.lower() == on_load))
                if len(script_sets) >= 400:
                    break
            if len(script_sets) >= 400:
                break
        if len(script_sets) >= 400:
            break           # the cap holds for the whole scan (not just one line / one script): the page stays readable
    script_sets.sort(key=lambda x: (not x["on_module_load"], x["name"]))
    # all variables, from the variable audit
    va = (report or {}).get("var_audit") or {}
    vs = va.get("variables", [])
    counts = dict(total=len(vs), by_kind=dict(Counter(v.get("system") for v in vs)),
                  on_module=sum(1 for v in vs if "module" in (v.get("scopes") or [])),
                  on_player=sum(1 for v in vs if "pc" in (v.get("scopes") or [])),
                  warnings=sum(1 for v in vs if v.get("level") == "warning"),
                  status=dict(Counter(v.get("status") for v in vs)))
    return dict(settings=settings, notes=notes, events=events, haks=haks, module_vars=module_vars,
                script_sets=script_sets, variables=counts)


# ------------------------------------------------------------------ server configuration
# user:password@ inside a connection URL, e.g. mysql://nwn:hunter2@db:3306/pw - group 2 is the password
_URL_CREDENTIALS = re.compile(r"(\b[A-Za-z][\w+.-]*://[^\s:/@]*:)([^\s@/]+)(@)")


def _mask(key, value):
    """The value to keep for `key`: unchanged, or "(set - hidden)" / "(empty)" when the key name looks secret.
    Empty quotes ('""', "''") count as empty, so an unset password is still reported as unset. A password written
    inside a URL (scheme://user:password@host) is hidden whatever the key is called; the rest of the URL is kept."""
    if SECRET_RE.search(key or ""):
        return "(set - hidden)" if str(value).strip() not in ("", '""', "''") else "(empty)"
    if isinstance(value, str) and _URL_CREDENTIALS.search(value):
        return _URL_CREDENTIALS.sub(r"\1(hidden)\3", value)
    return value


def _flatten(d, prefix=""):
    """Nested TOML tables as one level with dotted keys: {"server": {"pvp-mode": 2}} -> {"server.pvp-mode": 2}.
    Lists become one comma-separated string. A list of tables ([[db]] in TOML) is flattened per entry with its index
    ("db.0.password"), so every key keeps its own name and _mask can still recognise secret ones."""
    out = {}
    for k, v in d.items():
        kk = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, kk))
        elif isinstance(v, list) and any(isinstance(x, dict) for x in v):
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    out.update(_flatten(x, f"{kk}.{i}"))
                else:
                    out[f"{kk}.{i}"] = x
        else:
            out[kk] = v if not isinstance(v, list) else ", ".join(map(str, v))
    return out


def _parse_toml(text):
    """settings.tml text -> {dotted key: value}. Uses tomllib (Python 3.11+); if that is missing or the file is not
    strict TOML, a simple line reader takes over. Never raises."""
    try:
        import tomllib
        return _flatten(tomllib.loads(text))
    except Exception:  # noqa - fall back to a simple reader (older Python, or odd files)
        out, sect = {}, ""
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()       # also cuts a "#" inside a quoted value; good enough here
            if not line:
                continue
            # a section heading: "[server]", "[server.login]", or "[[x]]" (taken as section "x")
            m = re.fullmatch(r"\[+\s*([^\]]+?)\s*\]+", line)
            if m:
                sect = m.group(1)
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[(sect + "." if sect else "") + k.strip()] = v.strip().strip('"').strip("'")
        return out


def _parse_env(text):
    """Environment-style text -> {KEY: value}. Accepts .env / `env` output ("KEY=value", "export KEY=value") and
    docker-compose 'environment:' entries, both the list form ("- KEY=value") and the map form ("KEY: value").
    Surrounding quotes on the key or the value are removed. Lines that don't look like a setting are skipped."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^(export\s+|-\s+)", "", s)        # "export " (shell) or "- " (YAML list item)
        # KEY, optionally quoted, then "=" or ":"; e.g. 'NWN_PVP=2', '"NWN_PVP": "2"', 'NWNX_REDIS_SKIP: n'
        m = re.match(r"""^["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*[=:]\s*(.*)$""", s)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if not v and re.search(r":\s*$", s):
            continue           # a YAML heading such as "environment:" - not a value
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def parse_server_file(text, name=""):
    """Read one server settings file pasted or loaded in the dashboard.

    text: the file's content. name: its file name, used to recognise .tml/.toml (may be "").
    Returns dict(kind="settings.tml" | "environment", name, values={key: value}) with every secret-looking value
    already masked (see _mask); the raw text is not kept by this module. Raises ValueError for empty, oversized (over
    2,000,000 characters) or unrecognisable text."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the file is empty")
    if len(text) > 2_000_000:
        raise ValueError("that file is too big to be a settings file")
    # TOML if the name says so, or if a line starts with a "[section]" heading and no line starts with NWN... (an
    # env file with NWN_/NWNX_ variables can also contain bracketed text, e.g. in a docker-compose file)
    is_toml = name.lower().endswith((".tml", ".toml")) or (re.search(r"(?m)^\s*\[[A-Za-z]", text) and
                                                           not re.search(r"(?m)^\s*(export\s+)?NWN", text))
    values = _parse_toml(text) if is_toml else _parse_env(text)
    if not values:
        raise ValueError("no settings found - expected settings.tml, or KEY=VALUE lines (an .env file, docker-compose "
                         "environment, or `env` output from the server)")
    return dict(kind="settings.tml" if is_toml else "environment", name=name or ("settings.tml" if is_toml else "env"),
                values={k: _mask(k, v) for k, v in values.items()})


def _truthy(v):
    """True / False for a recognised on/off spelling (TRUE / FALSE sets), None for anything else."""
    s = str(v).strip().lower()
    return True if s in TRUE else (False if s in FALSE else None)


# The server settings shown as rows, in display order. When both sources set one, the environment wins in
# interpret() (the order it checks them in).
KNOWN = [
    # (label, settings.tml key, env key, meaning of values)
    ("Player vs player", "server.pvp-mode", "NWN_PVP", {"0": "none", "1": "party", "2": "full PvP"}),
    ("Enforce legal characters (ELC)", "ruleset.enforce-legal-characters", "NWN_ELC", None),
    ("Item level restrictions (ILR)", "ruleset.item-level-restrictions", "NWN_ILR", None),
    ("Difficulty", "ruleset.difficulty", "NWN_DIFFICULTY", None),
    ("Maximum players", "server.login.max-players", "NWN_MAXCLIENTS", None),
    ("Character vault", "server.vault.mode", "NWN_SERVERVAULT", None),
    ("Reload module when empty", "server.reload-when-empty", "NWN_RELOADWHENEMPTY", None),
    ("Minimum level", None, "NWN_MINLEVEL", None), ("Maximum level", None, "NWN_MAXLEVEL", None),
    ("One party only", None, "NWN_ONEPARTY", None), ("Pause and play", None, "NWN_PAUSEANDPLAY", None),
    ("Public server", None, "NWN_PUBLICSERVER", None), ("Server name", None, "NWN_SERVERNAME", None),
    ("Module the server loads", None, "NWN_MODULE", None), ("Autosave interval", None, "NWN_AUTOSAVEINTERVAL", None),
    ("NWSync (hak download) URL", None, "NWN_NWSYNCURL", None),
    ("Player password", "server.login.player-password", "NWN_PLAYERPASSWORD", None),
    ("DM password", "server.login.dm-password", "NWN_DMPASSWORD", None),
    ("Admin password", None, "NWN_ADMINPASSWORD", None),
]


def unused_scripts(report):
    """Lower-case script names the analysis found nothing uses (Not used by anything / Safe to delete).

    report: report.json as a dict (orphans and deletions lists are read), or None (returns an empty set)."""
    out = set()
    for g in (report or {}).get("orphans") or []:
        for m in g.get("members") or []:
            if (m.get("node") or "").startswith("script:"):
                out.add(m["node"][7:].lower())
    for d in (report or {}).get("deletions") or []:
        if (d.get("node") or "").startswith("script:"):
            out.add(d["node"][7:].lower())
    return out


def interpret(configs, db=None, summary=None, unused=None):
    """configs = [{kind, name, values}] (all loaded files); unused = lower-case script names nothing uses (see
    unused_scripts). Returns {rows, nwnx, findings}.

    db: the analysis index, sqlite3.Connection (or None: no NWNX use is found). summary: report.json "summary"
    (module_name, module_path, haks_missing), or None.
    Also returns `other`: every loaded key that is not in KNOWN, sorted, at most 400 (values as stored, so secrets
    stay masked). Read-only; writes nothing."""
    tml, env = {}, {}
    for c in configs or []:
        (tml if c.get("kind") == "settings.tml" else env).update(c.get("values") or {})
    rows = []
    for label, tk, ek, meaning in KNOWN:
        src, val = None, None
        if ek and ek in env:
            src, val = "environment", env[ek]
        elif tk and tk in tml:
            src, val = "settings.tml", tml[tk]
        if src is None:
            continue
        shown = meaning.get(str(val), val) if meaning else val
        if label == "Difficulty" and src == "settings.tml":
            shown = {"0": "very easy", "1": "easy", "2": "normal", "3": "difficult", "4": "very difficult"}.get(str(val), val)
        if label == "Character vault":
            # read with opposite meanings for 0/1: environment 1 = server vault, settings.tml 0 = server vault
            sv = str(val).strip().lower()
            shown = ({"1": "server vault", "0": "local characters allowed"} if src == "environment" else
                     {"0": "server vault", "1": "local characters allowed"}).get(sv, val)
        if isinstance(val, bool):
            shown = "on" if val else "off"
        elif meaning is None and _truthy(val) is not None and label.startswith(("Enforce", "Item level", "Reload",
                                                                              "One party", "Pause", "Public")):
            shown = "on" if _truthy(val) else "off"
        rows.append(dict(setting=label, value=str(shown), raw=str(val), source=src))
    findings = []
    # NWNX plugins: which are switched on, and which the module's scripts need
    nwnx_env = {k: v for k, v in env.items() if k.startswith("NWNX_")}
    skip_all = _truthy(nwnx_env.get("NWNX_CORE_SKIP_ALL") or nwnx_env.get("NWNX_CORE_SKIP_ALL_PLUGINS") or "")
    plugins = {}                    # plugin name -> True (on) / False (off), from NWNX_<PLUGIN>_SKIP
    for k, v in nwnx_env.items():
        m = re.fullmatch(r"NWNX_([A-Z0-9]+)_SKIP", k)   # e.g. NWNX_REDIS_SKIP=n -> REDIS is on
        if m and m.group(1) != "CORE":
            t = _truthy(v)
            plugins[m.group(1)] = (t is False) if t is not None else not skip_all
    used = defaultdict(set)         # plugin name -> scripts that #include one of its nwnx_<plugin>... scripts
    if db is not None:
        # LIKE with ESCAPE: "\_" is a literal underscore (a bare "_" in LIKE matches any one character)
        for s_, d_ in db.execute("SELECT src, dst FROM edges WHERE kind='include' AND lower(dst) LIKE 'script:nwnx\\_%' "
                                 "ESCAPE '\\'"):
            plug = d_[len("script:nwnx_"):].split("_")[0].upper()     # nwnx_redis_lib -> REDIS
            if plug and plug not in ("INC", "CONSTS", "NWNX", "CORE"):   # shared NWNX includes, not plugins
                used[plug].add(s_[7:] if s_.startswith("script:") else s_)
    unused = unused or set()
    nwnx, off_needed = [], []
    for plug in sorted(set(plugins) | set(used)):
        on = plugins.get(plug)
        # no _SKIP line for this plugin: on unless SKIP_ALL is set; None (unknown) when no NWNX settings were loaded
        if on is None and nwnx_env:
            on = not skip_all
        live = sorted(x for x in used.get(plug, ()) if x.lower() not in unused)
        idle = len(used.get(plug, ())) - len(live)
        nwnx.append(dict(plugin=plug, enabled=on, scripts=len(live), unused_scripts=idle,
                         examples=(live or sorted(used.get(plug, ())))[:5]))
        if live and on is False:
            off_needed.append((plug, live))
    if off_needed:
        # one finding for all of them (a long list of near-identical warnings hides the point)
        parts = [f"{p_.title()} ({len(l_)}: {', '.join(l_[:2])}{'...' if len(l_) > 2 else ''})" for p_, l_ in off_needed]
        findings.append(dict(severity="warning",
                             text=f"{len(off_needed)} NWNX plugin(s) the module's scripts use are switched off in the "
                                  f"server's environment: {'; '.join(parts)} - those calls do nothing at run time "
                                  "(scripts not used by anything are not counted)",
                             fix="For each plugin you need, set NWNX_<PLUGIN>_SKIP=n in the server environment (e.g. "
                                 f"NWNX_{off_needed[0][0]}_SKIP=n); otherwise stop using it in those scripts.",
                             plugins=[p_ for p_, _ in off_needed]))
    if used and not nwnx_env and configs:
        findings.append(dict(severity="info", text=f"the module's scripts use NWNX ({', '.join(sorted(used)[:6])}) but the "
                             "loaded server settings don't mention NWNX - load the server's environment (env / docker "
                             "compose) to check the plugins are switched on", fix="Load the environment the server runs with."))
    mod_name = (summary or {}).get("module_name") or ""
    mod_file = ((summary or {}).get("module_path") or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if env.get("NWN_MODULE") and mod_file and env["NWN_MODULE"].strip().lower() not in (mod_file.lower(), mod_name.lower()):
        findings.append(dict(severity="info", text=f"the server loads module '{env['NWN_MODULE']}', this analysis is of "
                             f"'{mod_file}' - make sure you analysed the copy the server runs",
                             fix="Analyse the exact .mod the server loads (or a copy of it)."))
    elc = next((r for r in rows if r["setting"].startswith("Enforce legal")), None)
    if elc and _truthy(elc["raw"]) and (summary or {}).get("haks_missing"):
        findings.append(dict(severity="info", text="ELC is on: characters are checked against the server's rules and "
                             "2das - the module's haks (not all loaded in this analysis) decide what is legal",
                             fix="Load the haks to see the class/feat 2das the server uses."))
    pw = next((r for r in rows if r["setting"] == "Player password"), None)
    pub = next((r for r in rows if r["setting"] == "Public server"), None)
    if pw and pw["raw"] in ("(empty)", "") and pub and _truthy(pub["raw"]):
        findings.append(dict(severity="info", text="a public server without a player password - anyone can join",
                             fix="Fine for an open PW; set a player password for a private one."))
    return dict(rows=rows, nwnx=nwnx, findings=findings,
                other=sorted(((k, v) for k, v in {**tml, **env}.items()
                              if not any(k in (r_[1], r_[2]) for r_ in KNOWN)), key=lambda kv: kv[0])[:400])
