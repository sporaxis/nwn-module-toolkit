"""
nwn_logs.py - read NWN:EE client/server logs, group runtime errors, and link them to
the module analysis (which script, what triggers it, where the object lives, impact).

Default log folder: <user folder>/logs, or logs.0 on older versions (nwclientLog1.txt, nwserverLog1.txt); the user
folder is Documents/Neverwinter Nights on Windows and macOS, ~/.local/share/Neverwinter Nights on Linux.

    python nwn_logs.py <analysis folder> [--logs FOLDER_OR_FILE ...]

Exit codes: 0 done (also when there are no log files to read - one line says so); 2 the analysis folder or a --logs
path does not exist, or the analysis folder has no report.json.

Why this exists
---------------
A play-test produces thousands of log lines, most of them engine noise. The Log monitor turns them into a short list
of groups ("this script hit TOO MANY INSTRUCTIONS 40 times"), each with what it means, whether it needs fixing
(matters: yes / maybe / no) and how to fix it, and - when an analysis is given - the script's triggers, impact level
and where the tagged object lives in the module.

How a line is read
------------------
    1. Strip the time stamp: the EE engine prefix ("E [00:40:16] ScriptVM (file.cpp:264:Func): ") or an older
       bracketed date ("[Mon Sep 28 13:00:05] ").
    2. Try PATTERNS in order; the first match decides the kind (script_error, missing_resource...) and pulls out
       the script / resource / tag. Lines that match nothing are ignored.
    3. Group by kind + script (or resource) + the message with numbers blanked out, and count.
    4. Explain the group from EXPLAIN (first match wins) and link it to the analysis.

What it reads and writes
------------------------
Reads the log files (only the bytes added since the last poll), and the analysis's report.json and index.sqlite
(queries only). Writes nothing; the dashboard keeps the result in memory.

Public entry points: LogMonitor (poll / report), explain(kind, message), default_log_dirs(), expand(paths), main().

Limits: the patterns match known wordings of EE client and server messages; a message worded differently is grouped
as a plain "error" / "warning" or ignored. Lines are cut at MAX_LINE characters. A log file is assumed to be
rotated (and is read again from the start) only when it gets smaller.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict

# Older bracketed time stamp at the start of a line: "[Mon Sep 28 13:00:05] " or "[2024-01-02 10:00:00] ".
# The length limits stop a long bracketed message from being taken for a date.
TS_RE = re.compile(r"^\[(?P<ts>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[^\]]{0,40}|\d{4}-\d\d-\d\d[^\]]{0,30})\]\s*")
MAX_LINE = 2048          # longer lines are cut: a runaway script can print very long lines
# Player chat copied into the log ("[CHAT] ...", "SHOUT ...") is never an error, even when it says "error".
SKIP_RE = re.compile(r"^\[?(CHAT|Chat|TALK|SHOUT|WHISPER|PARTY|DM)\b")
# EE engine log prefix: "E [00:40:16] ScriptVM (nwsvmachinecommands.cpp:264:ReportError): message"
# lvl = E/I/D/W (error, info, debug, warning); origin = "nwsvmachinecommands.cpp:264:ReportError" (engine source
# file, line, function). "ScriptVM " and the origin are optional: "E [00:40:37] (twodimarray.cpp:940:Load2DArray): ".
EE_RE = re.compile(r"^(?P<lvl>[EIDW])\s+\[(?P<ts>[\d:]{5,8})\]\s+(?:ScriptVM\s+)?(?:\((?P<origin>[\w.]+:\d+:[\w~:]+)\):\s*)?")
# (kind, regex), tried in this order on the line without its time stamp; the first match wins, so the specific
# patterns come first and the catch-all "error" / "warning" last. Named groups: script, oid, tag, err, us, res, plugin.
# An example line each pattern matches, top to bottom:
#   EE engine:     "Script error: ms_onmodload OID: 00000000, Tag: , ERROR: IP OUT OF CODE SEGMENT"
#                  (err keeps its "ERROR: " / "SCRIPT_ABORT: " prefix)
#   other form:    "Script area_enter, OID: 80000010, Tag: AREA_TOWN, ERROR: TOO MANY INSTRUCTIONS"
#   follow-up:     "script ms_onmodload at level 0 on object 0x00000000 took 57830 us, error: -646"
#   compiler:      "nw_s0_fireball.nss(12): ERROR: UNDEFINED IDENTIFIER"
#   other script:  "Script foo ... ERROR: x"  ((?!error\b): "Script error: ..." is not a script called "error")
#   2da:           "Failed to demand gosslulncomm.2da"
#   resource:      "Could not find resource xyz.utc" (the name must have a 3-character extension)
#   resource:      "Resource missing: abc"
#   NWNX:          "NWNX_Redis: ERROR connecting"
#   anything else with error / exception / fatal / assert / exowarning, then warn / warning
PATTERNS = [
    ("script_error", re.compile(r"Script error: (?P<script>[\w\-]+) OID: (?P<oid>[0-9a-fA-Fx]+), Tag: (?P<tag>[^,]*), "
                                r"(?P<err>(?:ERROR|SCRIPT_ABORT|WARNING): .+)")),
    ("script_error", re.compile(r"Script (?P<script>[\w\-]+), OID: (?P<oid>[0-9a-fA-Fx]+), Tag: (?P<tag>[^,]*), ERROR: (?P<err>.+)")),
    ("script_stopped", re.compile(r"(?i)script (?P<script>[\w\-]+) at level \d+ on object \S+ took (?P<us>\d+) us, "
                                  r"error: (?P<err>-?\d+)")),
    ("compile_error", re.compile(r"^(?P<script>[\w\-]+)\.nss(?:\(\d+\))?: (?P<err>(?:ERROR|Error): .+)")),
    ("script_error", re.compile(r"Script (?!error\b)(?P<script>[\w\-]+).*?ERROR: (?P<err>.+)", re.I)),
    ("missing_resource", re.compile(r"(?i)failed to demand (?P<res>[\w\-]{1,32}\.2da)")),
    ("missing_resource", re.compile(r"(?i)(?:could not|couldn't|failed to|unable to) (?:find|load|open|locate)\b.*?(?P<res>[\w\-]{1,32}\.[a-z0-9]{3})")),
    ("missing_resource", re.compile(r"(?i)(?:resource|resref).*?(?:not found|missing).*?(?P<res>[\w\-]{1,32})")),
    ("nwnx", re.compile(r"(?P<plugin>NWNX_\w+).*?(?P<err>(?:ERROR|WARN|FATAL).*)")),
    ("error", re.compile(r"(?i)\b(?P<err>(?:error|exception|fatal|assert|exowarning)\b.*)")),
    ("warning", re.compile(r"(?i)\b(?P<err>warn(?:ing)?\b.*)")),
]
# Short tips by error text (upper case). Used only when no EXPLAIN entry matches.
HINTS = {
    "TOO MANY INSTRUCTIONS": "Script ran too long (infinite loop or heavy loop). Check loops and recursion.",
    "DIVIDE BY ZERO": "A division by a zero value. Guard the divisor.",
    "STACK UNDERFLOW": "Usually a mismatched include or stale compiled script - recompile.",
    "INVALID OBJECT": "Script used an object that no longer exists - check GetIsObjectValid().",
    "INSTRUCTION LIMIT": "Script ran too long.",
}

# What a log message means, whether it matters, and how to fix it. `matters`: yes | maybe | no (engine/client noise).
# First match wins; checked against the message text (and the kind).
EXPLAIN = [
    (r"IP OUT OF CODE SEGMENT", dict(
        title="The script's compiled code is broken or out of date",
        meaning="The game jumped outside the compiled script (.ncs). The .ncs doesn't match the game: usually it was "
                "compiled with an older compiler or a different nwscript.nss (for example NWNX or newer EE functions), "
                "or the file is damaged. The script stopped there, so the rest of it did not run.",
        matters="yes",
        fix="Recompile the script: toolset Build > Compile (or open it and Save & Compile), or the toolkit's compile "
            "check. If it uses NWNX or new EE functions, compile it with the same nwscript.nss the game/server uses.")),
    (r"TOO MANY INSTRUCTIONS|INSTRUCTION LIMIT", dict(
        title="The script ran too long and was stopped",
        meaning="NWN stops a script after a fixed number of instructions - an endless or very long loop (often a "
                "GetFirst/GetNext loop that never advances, or a loop over every object in the module).",
        matters="yes",
        fix="Look at the loops in the script: make sure each one advances (GetNext...) and ends; split heavy work with "
            "DelayCommand so it runs over several moments.")),
    (r"SCRIPT_ABORT: An internal NWNX function was called without NWNX", dict(
        title="The script uses NWNX, which isn't running here",
        meaning="NWNX is a server add-on. When a script calls an NWNX function and NWNX isn't loaded (playing or "
                "testing locally, or a server without it) the whole script stops at that line - everything after it "
                "is skipped.",
        matters="maybe",
        fix="Expected when you test locally without NWNX. On the server: make sure NWNX runs and the plugin is on "
            "(Overview > Server settings shows which plugins the scripts need). To keep local testing useful, put the "
            "NWNX parts in their own script or behind a check that NWNX is present.")),
    (r"^-?\d+$", dict(
        title="The script stopped with an error",
        meaning="The engine's follow-up line for the error just above it for the same script (the number is the engine's "
                "internal error code; the line also says how long the script ran).",
        matters="yes",
        fix="Fix the error reported just before it for this script (usually on the row above).",
        kinds=("script_stopped",))),
    (r"NO FUNCTION MAIN\(\)|NO FUNCTION STARTINGCONDITIONAL\(\)", dict(
        title="Compiled a file that isn't a runnable script",
        meaning="The compiler found no main() (and no StartingConditional()): the file is an include - a library other "
                "scripts #include - not something that runs on its own. The toolset tries to compile every script, so "
                "includes always log this.",
        matters="no",
        fix="Nothing to do for an include. Only if this file is set as an event or conversation script: give it a "
            "void main() (or int StartingConditional() for a conversation condition).")),
    (r"Failed to demand .*\.2da|\.2da", dict(
        title="A 2da table was asked for but doesn't exist",
        meaning="A script (Get2DAString) or the game asked for a 2da table that isn't in the module, its haks, the "
                "override folder or the base game. Every lookup in it returns an empty string, so whatever the script "
                "builds from it (loot, gossip lines...) comes out empty.",
        matters="yes",
        fix="Add the 2da to a hak or the module, or fix the name in the script. If a script creates the table at run "
            "time (NWNX), it only exists on a server with NWNX running.",
        kinds=("missing_resource",))),
    (r"creator palette|\.itp", dict(
        title="A toolset palette file is missing",
        meaning="A custom palette (.itp) the module lists couldn't be found. Palettes only matter in the toolset.",
        matters="no",
        fix="Nothing for play. In the toolset, the custom palette is empty until the .itp is back (it may be in a hak).")),
    (r"Error loading Orientation for tile", dict(
        title="A tileset has a faulty tile",
        meaning="A tileset (usually from a hak) has a tile whose definition is incomplete. Only areas that use that "
                "tile can look wrong.",
        matters="no",
        fix="Nothing unless an area shows a broken tile; then update the tileset hak (or tell its author).")),
    (r"Failed to read in entire header", dict(
        title="A hak or other container file couldn't be read",
        meaning="A .hak, .erf or similar file in the game's resource list is too short or damaged (often a 0-byte or "
                "half-downloaded file in hak/, override or nwsync). Its contents are missing for the whole session.",
        matters="maybe",
        fix="Look in the hak and nwsync folders for 0-byte or unusually small files and replace them; the Quick scan "
            "lists the module's haks and their sizes.")),
    (r"Sound/OpenAL Error|OpenAL", dict(
        title="Sound driver message", meaning="The game's audio library reported a device message. Nothing to do with "
        "the module.", matters="no", fix="Ignore (or update the sound driver if you hear no sound).")),
    (r"GOG|Galaxy|Steam|Authentication failed", dict(
        title="Store launcher message", meaning="The GOG/Steam launcher layer couldn't be reached (game started outside "
        "the launcher, or offline). Nothing to do with the module.", matters="no", fix="Ignore.")),
    (r"ASSERT \(nui\.cpp|ASSERT \(\w+\.cpp", dict(
        title="Game client self-check", meaning="An internal check inside the game client's own code (here its user "
        "interface) - not caused by the module.", matters="no", fix="Ignore, unless the game crashes at the same moment.")),
    (r"undefined alias", dict(
        title="Game path message", meaning="The game looked for a folder alias that isn't set up. Harmless.",
        matters="no", fix="Ignore.")),
]
EXPLAIN = [(re.compile(rx, re.I), e) for rx, e in EXPLAIN]   # compiled once, case-insensitive


def explain(kind, message):
    """The plain-English explanation for a log message: dict(title, meaning, matters, fix), or None if no entry
    matches. kind: the PATTERNS kind of the line; entries with `kinds` apply only to those kinds. Never raises."""
    for rx, e in EXPLAIN:
        if e.get("kinds") and kind not in e["kinds"]:
            continue
        if rx.search(message or ""):
            return {k: v for k, v in e.items() if k != "kinds"}
    return None


def default_log_dirs(nwn_user=None):
    """EE writes logs to <user folder>/logs (older versions: logs.0).

    nwn_user: the NWN user folder from the settings, tried first. Otherwise the usual locations: Documents/Neverwinter
    Nights in the home folder (Windows and macOS - NWN:EE uses the same place on both), the same under OneDrive
    (Windows with Documents redirected), and ~/.local/share/Neverwinter Nights (Linux). Returns the log folders of the
    first base that has any; [] if none exist. Read-only."""
    home = os.path.expanduser("~")
    bases = ([nwn_user] if nwn_user else []) + [os.path.join(home, "Documents", "Neverwinter Nights"),
                                                os.path.join(home, "OneDrive", "Documents", "Neverwinter Nights"),
                                                os.path.join(home, ".local", "share", "Neverwinter Nights")]
    out = []
    for b in bases:
        for sub in ("logs", "logs.0"):
            c = os.path.join(b, sub)
            if os.path.isdir(c) and c not in out:
                out.append(c)
        if out:
            break
    return out


def expand(paths, nwn_user=None):
    """Turn folders and files into a list of log files: every *.txt and *.log in each folder (not sub-folders), plus
    each file given. With no paths, the default log folders are used. Paths that don't exist are skipped."""
    out = []
    for p in paths or default_log_dirs(nwn_user):
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "*.txt")) + glob.glob(os.path.join(p, "*.log")))
        elif os.path.isfile(p):
            out.append(p)
    return out


def _norm(msg):
    """Message with hex and decimal numbers replaced by '#', so repeats that differ only in object ids or timings
    group together: "OID 0x80000010 took 57830 us" -> "OID # took # us"."""
    return re.sub(r"0x[0-9a-fA-F]+|\d+", "#", msg.strip())[:160]


class LogMonitor:
    """Incremental log reader. Call poll() repeatedly; it only reads new bytes.

    The dashboard keeps one per analysis and calls poll() then report() each time the Log monitor page refreshes.
    It never writes to the logs or the analysis."""

    def __init__(self, analysis_dir=None, paths=None, nwn_user=None):
        """analysis_dir: analysis folder to link errors to (None = no linking). paths: log folders/files (None =
        default log folders). nwn_user: NWN user folder for the defaults. The file list is fixed here; a log file
        created later is not picked up until a new monitor is started."""
        self.paths = expand(paths, nwn_user)
        self.offsets = {}            # path -> bytes already read (the dashboard can preset these to start at the end)
        self.groups = OrderedDict()
        self.lines_read = 0
        self.ctx = self._load_context(analysis_dir) if analysis_dir else None

    def _load_context(self, d):
        """Load what linking needs from analysis folder d: scripts by name (from report.json), where each tag lives
        ("Area › Object" or "Palette › Blueprint"), the module's file names, the short strings each script contains
        (to say which script asks for a missing resource) and the scripts that call NWNX functions.
        Returns None if report.json can't be opened or is not valid JSON. The index is opened read-only (a mistyped
        folder must not get a new, empty index.sqlite); without a usable index, only report.json's facts are kept."""
        try:
            with open(os.path.join(d, "report.json"), encoding="utf-8") as fh:
                rep = json.load(fh)
        except (OSError, ValueError):            # json.JSONDecodeError is a ValueError
            return None
        scripts = {s["name"]: s for s in rep["scripts"]}
        tag_where = {}
        lits = {}
        nwnx = set()
        try:
            import nwnlib
            db = nwnlib.sqlite_ro(os.path.join(d, "index.sqlite"))
        except sqlite3.Error:
            db = None                            # no index (archived or still indexing): no tag places, no "asked for by"
        try:
            for node, tag in db.execute("SELECT node, tag FROM objects WHERE tag IS NOT NULL AND tag != ''") if db else ():
                if node.startswith("inst:"):     # a placed object: its area path, then its label without " in <area>"
                    where = rep["breadcrumbs"].get(node, "Module") + " › " + rep["labels"].get(node, node).split(" in ")[0]
                else:
                    where = "Palette › " + rep["labels"].get(node, node)
                tag_where.setdefault(tag, []).append(where)
            # areas are not in `objects`: their tag is the "Tag" field of the area's .are file
            for tag, resref in db.execute("SELECT fi.value, f.resref FROM fields fi JOIN files f ON f.id=fi.file_id "
                                          "WHERE f.ext='are' AND fi.path='Tag'") if db else ():
                tag_where.setdefault(tag, []).append("Module › " + rep["labels"].get(f"area:{resref}", resref) + " (area)")
            # string pieces in scripts, to say which script asks for a missing 2da / resource
            for script, lit, func in db.execute("SELECT script, literal, func FROM script_literals UNION ALL "
                                                "SELECT script, literal, func FROM concat_literals") if db else ():
                # only name-like strings ("goss_", "loot_table"); shorter pieces would match almost any name
                if lit and 3 <= len(lit) <= 32 and re.fullmatch(r"[\w\-]+", lit):
                    lits.setdefault(lit.lower(), set()).add(script)
                if func and func.startswith("NWNX_"):
                    nwnx.add(script)
        except sqlite3.Error:
            pass                                 # an older or partial index without these tables: fewer links
        finally:
            if db:
                db.close()
        files = {f["resref"] + "." + f["ext"] for f in rep["files"]}
        return dict(scripts=scripts, tag_where=tag_where, files=files, lits=lits, nwnx=nwnx)

    def _asked_by(self, res):
        """Scripts whose string pieces name this resource (exactly, or as the start of a name built at run time).

        res: e.g. "gosslulncomm.2da". Returns (scripts, built): an exact match gives (names, False); otherwise
        scripts holding a piece the name starts with ("goss" + "lulncomm") give (up to 8 names, True)."""
        if not (self.ctx and res):
            return [], False
        stem = res.rsplit(".", 1)[0].lower()
        exact = sorted(self.ctx["lits"].get(stem, ()))
        if exact:
            return exact, False
        pref = sorted({s for lit, ss in self.ctx["lits"].items() if len(lit) >= 3 and stem.startswith(lit) for s in ss})
        return pref[:8], bool(pref)

    def poll(self):
        """Read what was added to each log since the last poll and group it. Returns the number of lines that
        matched a pattern. A file that can't be read this time is skipped and tried again next poll."""
        new = 0
        for p in self.paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            off = self.offsets.get(p, 0)
            if size < off:
                off = 0  # log rotated
            if size == off:
                continue
            with open(p, "rb") as fh:
                fh.seek(off)
                chunk = fh.read()
            # the game may be half-way through writing a line: keep only complete lines and read the rest next time
            cut = chunk.rfind(b"\n")
            if cut < 0:
                continue  # wait until the line is complete
            chunk = chunk[:cut + 1]
            self.offsets[p] = off + len(chunk)
            # NWN writes Windows-1252 text; "replace" keeps an odd byte from stopping the read
            for raw in chunk.decode("cp1252", "replace").splitlines():
                if self._line(raw, os.path.basename(p)):
                    new += 1
                self.lines_read += 1
        return new

    def _line(self, raw, src):
        """Parse one log line from file `src` (base name) and add it to its group. True if a pattern matched."""
        line = raw.strip()[:MAX_LINE]
        if not line or SKIP_RE.match(line):
            return False
        ts, origin = None, None
        m = EE_RE.match(line)
        if m:
            ts, origin = m.group("ts"), m.group("origin")
            line = line[m.end():]
        else:
            m = TS_RE.match(line)
            if m:
                ts = m.group("ts"); line = line[m.end():]
        for kind, rx in PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            gd = m.groupdict()
            script = (gd.get("script") or "").lower() or None
            res = (gd.get("res") or "").lower() or None
            err = gd.get("err") or line
            asked = self._asked_by(res) if res else ([], False)
            if res and res.endswith(".2da"):
                # many missing tables asked for by the same script are one problem
                key = (kind, "2da:" + (",".join(asked[0][:3]) or res), "")
            else:
                # same kind, same script (or resource / plugin) and same message apart from numbers = one group
                key = (kind, script or res or gd.get("plugin") or "", _norm(err))
            g = self.groups.get(key)
            if not g:
                g = dict(kind=kind, script=script, resource=res, message=err.strip()[:300], count=0,
                         first_seen=ts, last_seen=ts, tags=[], sources=[], example=raw.strip()[:400], origin=origin)
                g.update(self._enrich(kind, script, res, err, line))
                if gd.get("us"):                 # script_stopped: the engine reports microseconds
                    g["message"] = f"stopped with error code {err} after {int(gd['us']) / 1000:.0f} ms"
                self.groups[key] = g
            g["count"] += 1
            g["last_seen"] = ts or g["last_seen"]
            if res and res.endswith(".2da"):
                g.setdefault("resources", [])
                if res not in g["resources"] and len(g["resources"]) < 200:
                    g["resources"].append(res)
                    n_ = len(g["resources"])
                    g["message"] = f"Failed to load {g['resources'][0]}" + (f" and {n_ - 1} other 2da table(s)" if n_ > 1 else "")
            tag = gd.get("tag")
            if tag and tag not in g["tags"] and len(g["tags"]) < 10:
                g["tags"].append(tag)
                if self.ctx and tag in self.ctx["tag_where"]:
                    g.setdefault("where", [])
                    g["where"] = list(dict.fromkeys(g["where"] + self.ctx["tag_where"][tag][:3]))[:10]
            if src not in g["sources"]:
                g["sources"].append(src)
            return True
        return False

    def _enrich(self, kind, script, res, err, line=""):
        """Extra fields for a new group: severity (error / warning / info), hint or explain, which scripts ask for a
        missing resource, and - with an analysis - the script's impact, summary and triggers, or in_module=False
        for a script the module doesn't contain."""
        out = dict(hint=next((h for k, h in HINTS.items() if k in err.upper()), None), severity="warning")
        if kind in ("script_error", "script_stopped", "missing_resource", "error", "nwnx", "compile_error"):
            out["severity"] = "error"
        ex = explain(kind, err) or explain(kind, line)
        if ex:
            out["explain"] = dict(ex)
            out["hint"] = None
            if ex["matters"] == "no":
                out["severity"] = "info"          # engine / client noise, or harmless
        if res:
            asked, built = self._asked_by(res)
            if asked:
                out["asked_by"] = asked
                x = out.setdefault("explain", dict(title="A file was asked for but doesn't exist", meaning="", matters="yes",
                                                   fix="Add the file or fix the name."))
                who = ", ".join(asked[:4])
                x["meaning"] = (x.get("meaning") or "") + (
                    f" Asked for by {who}" + (" (the name is built at run time from a piece in that script)" if built else "") + ".")
                if self.ctx and any(a in self.ctx["nwnx"] for a in asked):
                    x["meaning"] += (" That script uses NWNX: if it is the one that creates these tables, they only exist "
                                     "when NWNX runs (on the server), so this is expected when testing locally.")
                    x["matters"] = "maybe"
        # "no main()" is harmless for an include, but a real fault if the file is hooked to an event or conversation
        if self.ctx and script and kind == "compile_error" and ex and ex["matters"] == "no":
            s = self.ctx["scripts"].get(script) or {}
            if s.get("triggers"):
                out["severity"] = "error"
                out["explain"]["matters"] = "yes"
                out["explain"]["meaning"] = ("This file is set as an event/conversation script (" +
                                             "; ".join(f"{t['via'].split('/')[-1]} of {t['by']}" for t in s["triggers"][:3]) +
                                             ") but has no main(), so nothing runs there.")
            elif s.get("included_by") or "library" in (s.get("summary") or "").lower():
                out["explain"]["meaning"] += " This one is a library the module's scripts include."
        if self.ctx and script:
            s = self.ctx["scripts"].get(script)
            if s:
                out.update(in_module=True, node=s["node"], impact=s["impact"], summary=s["summary"],
                           triggers=[f"{t['via'].split('/')[-1]} of {t['by']}" for t in s["triggers"][:5]])
            else:
                out.update(in_module=False, note="script not in this module (base game, hak, or another module)")
        if self.ctx and res:
            out["in_module"] = res in self.ctx["files"]
        return out

    def report(self):
        """Everything read so far: dict(files, lines_read, groups, errors, warnings, harmless, at). Groups are sorted
        errors first, then by impact level (Critical first), then by count. Counts are lines, not groups."""
        order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "None": 4, None: 5}
        groups = sorted(self.groups.values(),
                        key=lambda g: (0 if g["severity"] == "error" else 1, order.get(g.get("impact")), -g["count"]))
        return dict(files=self.paths, lines_read=self.lines_read, groups=groups,
                    errors=sum(g["count"] for g in groups if g["severity"] == "error"),
                    warnings=sum(g["count"] for g in groups if g["severity"] == "warning"),
                    harmless=sum(g["count"] for g in groups if g["severity"] == "info"),
                    at=time.strftime("%H:%M:%S"))


def main(argv=None):
    """Command line: read the logs once (all of each file) and print one line per group. Returns the exit code:
    0 done (also when the folders hold no log files - one line says so), 2 the analysis folder or a --logs path
    given does not exist, or the analysis folder has no report.json (one line says which)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis", nargs="?", help="analysis folder (nwn_workspace/<name>): links each error to the "
                    "module's scripts; without it the logs are only grouped")
    ap.add_argument("--logs", action="append", help="a log folder or file (repeat for more); default: the logs "
                    "folder of the NWN user folder")
    a = ap.parse_args(argv)
    # A path typed by hand that does not exist is a mistake to report (exit 2), not "no logs": expand() quietly skips
    # missing paths because the dashboard's default folders may legitimately be absent.
    gone = [p for p in a.logs or [] if not os.path.exists(p)] + \
        ([a.analysis] if a.analysis and not os.path.isdir(a.analysis) else [])
    if gone:
        print(f"Not found: {gone[0]} - check the path (put it in quotes if it contains spaces)")
        return 2
    if a.analysis and not os.path.isfile(os.path.join(a.analysis, "report.json")):
        print(f"No analysis found in {a.analysis} (it has no report.json) - give the analysis folder, e.g. "
              "nwn_workspace/<module>, after analysing the module")
        return 2
    mon = LogMonitor(a.analysis, a.logs)
    if not mon.paths:
        if not a.logs and not default_log_dirs():
            print("No NWN log folder found in the usual places - pass --logs <folder> (the logs folder inside your "
                  "NWN user folder).")
        else:
            where = ", ".join(a.logs or default_log_dirs())
            print(f"No log files found in {where} - nothing to read (log files end in .txt or .log). "
                  "Pass --logs <folder> to read another folder.")
        return 0
    mon.poll()
    r = mon.report()
    for g in r["groups"]:
        print(f"[{g['severity']}] x{g['count']} {g['kind']} {g.get('script') or g.get('resource') or ''}: "
              f"{g['message'][:100]}  impact={g.get('impact')}  where={g.get('where', [])[:1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
