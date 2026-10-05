# NWN Module Toolkit — User Manual

This manual explains how to use the toolkit from start to finish: scanning a module, analysing it, reading the
results, planning a cleanup, building and auditing a clean module, and keeping your workspace tidy. It is written
for people who build and run Neverwinter Nights: Enhanced Edition modules, not for programmers.

You can read it in the dashboard (**Help / manual** in the sidebar) or as `docs/MANUAL.md` in the toolkit folder.

---

## 1. What the toolkit does, and what it promises

The toolkit reads a module, meaning its areas, scripts, conversations, blueprints, 2das, models and textures, together with
its haks, talk tables and your `override` folder. It works out how everything connects, then shows you what is used,
what is broken, what is duplicated and what can safely go. It can build a **clean copy** of the module and
**prove** that nothing in use was lost.

**Safety promises**

- **Your originals are only read, except by five buttons you press yourself.** Analyses, edits and builds go into
  the toolkit's `nwn_workspace` folder. The five exceptions:
  - **Add to game folders** (Build & audit, after typing ADD) copies the finished build's `.mod` into your modules
    folder and each rebuilt hak into your hak folder, under names that don't exist yet. It never replaces a file, and
    **Undo** removes only the files it added that you have not changed since.
  - **Add to hak folder** (Hak editor) adds a rebuilt hak under a new name next to the original. It never replaces a
    file.
  - **Move** on the Modules folder page moves the module copies you ticked into an archive folder. It never deletes,
    and **Undo** moves them back.
  - **Restore** (Hak editor, only for backups made by older versions of the toolkit) replaces a hak with a backup. It
    backs up the current hak first.
  - **Extract** (Hak editor) writes only into an empty or new folder you choose.
  **Install / Update** (Modules page, section 2.0) never touches your modules, haks or game either: it writes only the
  install folder you choose, and remembers where it is in your user settings folder.
- **Evidence for every finding.** Each issue, impact level and delete suggestion names the file, field or script
  line behind it.
- **If in doubt, keep it.** Anything the toolkit cannot prove is unused is marked *Review*, never *Safe*.
- **Every clean build is audited** by a repeatable automatic audit (PASS, PASS WITH WARNINGS or FAIL).
- The dashboard only listens on your own computer (`127.0.0.1`) and needs the session link it prints when it starts,
  so other websites cannot use it.

### Undo and recovery

| To undo | Do this |
|---|---|
| An edit | **My edits** → **Discard**. The file is kept in `edits/.history/discarded/<time>/`; copy it back into `edits` to restore it |
| A Find & replace | **Find & replace** → **Earlier renames / replaces** → **Undo** |
| A staged hak change | **Hak editor** → **Undo** on the change, or **Undo all changes…** |
| Add to hak folder | **My edits** → discard the `module.ifo` edit, then delete `<hak>_r1.hak` from your hak folder yourself |
| Add to game folders | **Build & audit** → **Undo**: removes the `.mod` and haks that add put in, if they are unchanged; a file you changed since is left and listed. The record is `build/installed.json` |
| Restore | **Hak editor** → **Backups…** lists the copy Restore made of the replaced hak; **Restore…** puts it back |
| A Modules folder move | **Modules folder** → **Earlier moves** → **Undo** |
| Archive | **Unpack** on the Modules page |
| Clear or Delete of analysis data | Analyse the module again. Your own work (edits, builds, descriptions) is only deleted when you tick it and type the name - that cannot be undone |
| A build | Nothing to undo: the original is untouched. Build again, or Delete the build |

---

## 2. Installing and starting

The toolkit needs **Python 3.8 or newer** and nothing else. Unzip the release anywhere (Downloads is fine) and start it
as below. The Modules page then offers to **install** it in a folder of its own (section 2.0); you can also keep running
it from where you unzipped it.

**Windows**
1. Install Python from python.org and tick **Add python.exe to PATH**.
2. Double-click **`run_dashboard.bat`** in the toolkit folder. A console window opens, and your browser opens the
   dashboard.

**macOS**
1. Install Python with the installer from python.org. The `python3` that comes with macOS may be only a stub.
2. Double-click **`run_dashboard.command`**. The first time, macOS may refuse to open it: right-click it, choose
   **Open**, and confirm. A Terminal window opens, and your browser opens the dashboard.
3. When macOS asks whether Terminal may access your Documents folder, allow it - the game's user folder is there. If
   you refused, allow it in System Settings → Privacy & Security → Files and Folders → Terminal.
4. A Steam install of the game is under `~/Library`, which Finder hides. In Finder press Cmd+Shift+G and paste the
   path, or use the dashboard's **Browse…**.

**Linux**: run `python3 nwn_dashboard.py` in the toolkit folder.

Keep the console or Terminal window open while you work. Closing it stops the dashboard and anything it is doing: an
analysis, a build, a hak rebuild or an Add to hak folder. If the page says the session token is missing, open the exact
link printed in that window.

Optional: pick **System / Light / Dark** at the top of the sidebar. System follows your computer's setting. Your choice
is remembered in this browser.

The toolkit's version is shown at the bottom of the sidebar and at the top of this manual in the dashboard. A release
has it in `VERSION.txt`; a copy taken straight from the source says "development copy".

### 2.0 Installing and updating

An unzipped release shows an **Install the toolkit** card at the top of the Modules page:
- **Install folder** - suggested: `NWN Module Toolkit` in your user folder (`C:\Users\<you>\NWN Module Toolkit`,
  `~/NWN Module Toolkit` on macOS, `~/nwn-module-toolkit` on Linux). It is outside Documents and the Desktop on purpose:
  OneDrive and iCloud often sync those, and syncing can lock or damage the analysis databases. Type another folder or
  press **Browse…**; it must be new, empty, or the folder of an earlier install.
- **Bring my work from** - shown when the toolkit finds an older unzipped copy holding work (next to this one, or in
  Downloads, Desktop or Documents). Ticked, the install also copies that copy's settings, analyses (with your edits,
  builds, accepted issues and waiting changes), hak projects and compiler into the new install. That copy is only read,
  never changed.
- **Install** shows what will happen, then copies the program in, and restarts the toolkit from its new folder in a new
  window and browser tab (on Linux: start it with `start-nwn-toolkit.sh` in the install folder).

The install folder holds `toolkit` (the program), `nwn_workspace` (your work), `install.json` (which version, and the
history of installs and updates), **Start NWN Toolkit** (double-click it from now on) and, after an update, `previous`.

**Updating**: unzip the new version anywhere and start it. It finds your install (it remembers where you installed,
even in a folder you chose) and the card says **Update your installed toolkit**. **Update** swaps only the program:
your work stays as it is, the compiler in `tools` is kept, and the program it replaced is kept in `previous\<version>`
so you can go back (to go back, install that older version again: it asks you to confirm). Close the installed
toolkit's console window first - the update refuses while it is running, or while an analysis runs there.

**Not now** hides the card in this browser. In an installed toolkit the Settings section says where it is installed.
**Bring work from another copy…** (Settings) copies work from any other copy at any time, the same way.

After updating, open each module once: when the new version has checks that an analysis doesn't, the Overview says
which and offers **Analyse again** (the same module, haks and talk table, into the same folder - your edits, accepted
issues, descriptions and waiting changes stay). Changes queued in a newer version can't be built by an older one: the
older version's Build page says so instead of building without them, and refuses to change them.

### 2.1 Settings
**Settings** is at the bottom of the **Modules** page. Fill in:
- **NWN install folder**: the folder with `data` and `lang` in it. It gives exact base-game checks ("this script or
  head model ships with the game"). Press **Detect** to look in the usual places (the
  `NWN_ROOT` environment variable, the default Steam library, the Beamdog Client). When one is found, the empty box
  says so, and **Detect** fills it in. GOG installs and Steam libraries on other drives are not searched: type or
  paste those. Examples:
  - Windows (Steam): `C:\Program Files (x86)\Steam\steamapps\common\Neverwinter Nights`
  - macOS (Steam): `~/Library/Application Support/Steam/steamapps/common/Neverwinter Nights`
  - Linux (Steam): `~/.local/share/Steam/steamapps/common/Neverwinter Nights`
- **NWN user folder**: `Documents/Neverwinter Nights` on Windows and macOS, `~/.local/share/Neverwinter Nights` on Linux.
  It holds `modules`, `hak`, `tlk`, `override`, `database` and `logs`. Haks, the custom talk table and the override
  folder are found here automatically.
- **Compiler path** (optional): leave it empty when the compiler is in the toolkit's `tools` folder (see 2.2). The
  line above the boxes says whether it was found.
- **Log folders**: where the Log monitor looks. The label shows the folders it uses when you leave this empty.
- **External tools** (optional): programs you already use, such as Aurora Hak Explorer, Aurora TLK Explorer or the
  toolset, with the file types each one opens. The dashboard then shows **Open in …** buttons on the Overview (the
  module), the Hak catalogue and the Hak editor. On Windows a tool must be an `.exe`; on macOS it can be an
  application (`/Applications/Tool.app`). The dashboard only starts programs you listed, with the file as the
  argument, never through a command shell.

Press **Save settings**. "Saved." appears, with a warning under it for anything that looks wrong: an install folder
without `data` and `lang`, a user folder without `modules`, or a tool that is a script interpreter.

**Browse…** on the Modules page starts in your user folder's `modules` folder (then the last folder you used). In the
Hak editor it starts in the `hak` folder.

### 2.2 The tools folder (optional programs)
Three programs from **neverwinter.nim** (github.com/niv/neverwinter.nim/releases) make the toolkit more exact. They are
not part of the toolkit: they are third-party programs under the MIT licence. Download the build for your system and
copy them into the toolkit's `tools` folder:

| Program | What it adds | Without it |
|---|---|---|
| `nwn_script_comp` (`nwn_script_comp.exe` on Windows, with any `.dll` next to it) | Compiles scripts: **Validate all with official compiler**, compile checks on save, and builds that recompile edited scripts | Scripts are checked structurally but not compiled, and a build that edits an include is refused |
| `nwn_asm` (`nwn_asm.exe`) | A readable instruction listing under **Show compiled code** | The `.ncs` check still runs, without the listing |
| `nwn_erf` (`nwn_erf.exe`) | A fallback for a compressed hak entry the toolkit can't read itself | Such an entry is reported |

On macOS and Linux, let them run. In Terminal, in the `tools` folder:
`chmod +x nwn_script_comp nwn_asm nwn_erf`, and on macOS also
`xattr -d com.apple.quarantine nwn_script_comp nwn_asm nwn_erf`.
A compiler that is blocked or crashes without printing anything is reported as an error with these two commands -
it is never taken to mean "every script compiles". `tools/README.txt` has the same steps.

---

## 3. The workflow at a glance

| Step | Where | What you get |
|---|---|---|
| 1. Quick scan | Modules → **Quick scan (TL;DR)** | In seconds: haks found or missing, clashes, sizes, and how long a full analysis will take |
| 2. Analyse | Modules → **Analyse** | The full picture: every file, reference, issue and impact level |
| 3. Understand | Overview, Issues, Scripts, Quest health, Hak catalogue… | What is broken, risky, duplicated or unused |
| 4. Plan | **Safe to delete**, **Duplicates** | Tick what goes and which duplicates merge |
| 5. Build & audit | **Build & audit** | A clean module folder and `.mod` (optionally lean haks), a change log and an audit verdict; then, if you want, **Add to game folders** |
| 6. Play-test | **Log monitor** | Run-time errors grouped and linked to the scripts behind them |

Fix what the quick scan warns about (a missing hak, a missing talk table) **before** a long analysis. Otherwise
you pay for the full run and still get "Review" everywhere.

---

## 4. Modules page

### 4.1 Choosing a module
Type or paste the path, or use **Browse…**. You can point it at:
- an **unpacked module folder** (the one containing `module.ifo`, for example a toolset `temp0` folder or a nasher `src` folder), or
- a **`.mod`** file.

Under **Optional** you can add hak files by hand (one per line), a talk table and an analysis name. Usually you
need none of these: haks and the custom talk table are found from `module.ifo` and your NWN folders.

### 4.2 Quick scan (TL;DR)
**Quick scan** reads only `module.ifo`, the module's file list and each hak's table of contents. It never reads
file contents, so it takes seconds even with gigabytes of haks, and it writes nothing. You get:

- **Tiles**: areas, scripts, conversations, blueprints, number of haks (and how many are missing), the player
  download size (haks plus talk table), and the **estimated time for a full analysis**.
- **Things to know**: warnings in plain words, such as a missing hak, a missing custom talk table, names longer than
  16 characters (the game cannot load those), a hak that adds nothing because every file in it is also in a hak above
  it, or module files that a hak replaces (edits to those in the module have no effect in game).
- **Haks in load order**: the order in `module.ifo`, where **the first listed wins**. For each hak you see found,
  MISSING or unreadable, its size, what it carries (2das, models, textures, scripts, tilesets…) and how many of its
  files are hidden by haks listed above it.
- **2da tables in more than one hak**, and which hak's copy the game actually loads.
- **Also**: talk tables found or missing, the override folder (the module and its haks win over it: how many of its
  files they hide), module events, whether the module uses a journal, signs of NWNX, and whether this module has been
  analysed before.

The time estimate uses the speed of previous runs **on your computer**. The very first estimate is a guess. It gets
better with every finished analysis.

### 4.3 Analyse, progress and time left
Press **Analyse**. The button changes to **Loading… n%** and a progress panel shows:

- the current step, for example *Reading files - cep2_core2.hak*, *Linking: names mentioned inside files* or
  *Analysis: impact levels*
- a progress bar, the percentage, files read out of the total, the time elapsed and **about how long is left**

The estimate corrects itself as the run goes, using the speed it actually sees. The last two steps (linking and
analysis) have no file count, so their length comes from previous runs. If a step takes longer than expected, the panel
says *taking longer than estimated - still working* instead of sitting at 99%.

You can switch pages while it runs. The sidebar shows *Analysing … n% · time left* on every page, and clicking it
brings you back. Reloading the browser page also reconnects to the running analysis.

The analysis runs in its **own Python process**, so the dashboard stays responsive and the memory is released when it
ends. When it finishes, the analysis opens on the **Overview** page.

> **Re-analysing replaces the previous analysis of that module** from the moment it starts. If you want to keep the
> old one, give the new run a different name under *Optional → analysis name*.

### 4.4 Stop
**Stop** ends the running analysis immediately. The partial result is marked **incomplete**:
- it cannot be opened, because a half-built index does not match any report, and
- it shows in *Analysed modules* with an **incomplete** badge and a **Clear** button.

### 4.5 Clear
**Clear** resets the panel. If the last run was stopped or failed, it also removes that run's **partial files**
(after asking you). Only files an analysis run generates are removed: the index, report, JSON, conversations, CSV
reports and catalogue. **Your edits, builds and script descriptions are never touched by Clear.**

Clear refuses to remove:
- a **complete** analysis (use Delete for that, see 4.6),
- an analysis that is **still running** (press Stop first),
- a folder that **another process** is writing to, such as a second dashboard window or a command-line run. The toolkit checks
  the run lock, and for older runs whether the files changed in the last minute.

### 4.6 Analysed modules list and Delete (housekeeping)
Each row shows the module, its source path, when it was analysed, its status (**complete**, **running** or
**incomplete**) and whether it has a clean build. Click a complete row to open it.

**Delete…** frees disk space. The dialog shows how much each part uses:

| Part | What it is | Deleted? |
|---|---|---|
| Analysis data | index, report, JSON, CSV reports, catalogue, compile results | **Always.** Analysing again recreates it |
| My edits | edited and new scripts, 2das and GFF files | Only if you tick it |
| Clean builds | built module folders, `.mod`, lean haks, audits, git history | Only if you tick it |
| Descriptions, accepted issues, loaded server settings and database files | script descriptions (`descriptions.json` and its batch files), the issues you accepted as by design (`accepted_issues.json`), and the server settings and campaign database files you loaded (`server_config.json`, `database.json`) | Only if you tick it |
| Other files | anything else in the folder | Only if you tick it |

If you tick any of your own work, you must **type the analysis name** to confirm. When nothing is left, the analysis
folder itself is removed. Delete never touches your module, haks or game files.

If you keep your edits (or builds, or descriptions), the module disappears from the list, but those files stay in
`nwn_workspace/<analysis>`. When you analyse the same module again, they are picked up again.

### 4.7 Archive and Unpack (save disk space)
A complete analysis has an **Archive** button. It zips the analysis data (index, report, JSON, conversations, reports)
into `analysis_archive.zip`, reads the zip back to check every file, and only then removes the unpacked copies. A large
analysis shrinks to about a sixth of its size. Your edits, builds, descriptions and accepted issues stay as they are.
An archived analysis shows **archived** with its size, and **Unpack** brings it back (a few seconds to a minute). The zip
is checked against its recorded SHA-256 before unpacking, and nothing in it can land outside the analysis folder.
If either step is interrupted (a file open in another program, a crash), press the same button again: Archive finishes
removing only files the verified zip holds, and Unpack skips files already in place that match the zip byte for byte.
A file in the way that differs from the zip is never overwritten - the message names it.

### 4.8 Modules folder (housekeeping)
**Modules folder** (under Start) tidies a modules folder full of copies. Enter the folder and press **Scan**. It lists
every `.mod` and `.BackupMod`, groups them into *families* by name (`Dark_Shore_202202_8193_34_1f` and
`Dark_Shore-2024-03-08_build` are both *dark_shore*), and finds byte-identical copies (SHA-256, only for files of the
same size; results are cached, so a second scan is quick).

It suggests archiving:
- identical copies (one of each set is kept),
- older versions beyond the newest N of each family (you choose N; optionally also keep the newest of each month),
- toolset backups (`.BackupMod`) of older versions,
- empty (0-byte) module files left by failed saves.

It never suggests: the newest of a family, the toolset backup of your newest version, anything changed in the last
2 days, anything an analysis was made from, and anything on your **Always keep** list. Folders (like `temp0`) and `.rar`
files are left alone.

Untick anything you want to keep, then **Move ticked files…** and type **MOVE**. The files are *moved* (never deleted)
into `_toolkit_archive/<date>` inside the modules folder, or a folder you choose (another drive works: the copy is
checked before the original is removed). A `manifest.json` is written first, listing every file, where it went and its
size. A file the toolset or game has open is skipped. **Earlier moves** lists each batch with **Undo**, which moves the
files back (it never overwrites a file that has reappeared).

### 4.9 Compare modules
**Compare modules** (under Start) shows what changed between two copies: pick the older and newer copy (a `.mod`, a
module folder, or a snapshot) and optionally an analysis for impact levels. You get:
- counts of added, removed and changed files, and the size change,
- for each changed file: the **fields** that differ (path, old value, new value) for areas, blueprints, placed objects,
  conversations and `module.ifo`, or a **line-by-line diff** for scripts and 2das,
- the impact level of each change, and for a removed file how many things used it (*removed but still used* is the one
  to check first).

**Take a snapshot of the newer copy** keeps a small record of a module (a fingerprint of every file plus the text of its
scripts and 2das) in `nwn_workspace/_snapshots`. Compare against it later, even after the `.mod` itself has been
archived; field-level detail for GFF files needs the real `.mod`.

---

## 5. Reading an analysis

**Moving around.** A reload of the browser page comes back to the page you were on, with the same analysis open. You
can work with the keyboard: Tab moves between links, buttons and rows, Enter (or Space) opens the one in focus, and
Escape closes a dialog.

**Grouped lists.** Long lists are shown in groups so a big module doesn't bury you in rows. Each list has a
**Group by** menu above it; each group is a fold-out card with a count (and a Critical/High badge when a member is
high impact). Groups open when you click them, and stay open while you search. With only a few rows or groups they
open by themselves. Pick *no grouping (one list)* for a single table. More than 400 groups? Press **show all**.
The groupings are:

| Page | Group by |
|---|---|
| Overview (highest-impact resources), Impact explorer | why it matters (*Module-wide events* first, then *Touches a quest*, *Reaches 10+ areas*…), module-wide event (OnModLoad, OnClientEnter…), kind, level |
| Issues | type, severity, where (the area, or module-wide) |
| Scripts | set (shared name), role, impact |
| Items | set (shared resref), base item, where placed (area), impact |
| Conversations | set (shared name), area or region where the speaker stands, quest giver, used or not |
| Areas | region (the part of the area name before " - ", e.g. *Harbour* for *Harbour - Fish Market*), set (shared name), impact, in the module's area list |
| Variables | status, kind, set (shared name) |
| Tags looked up | status, set (shared name) |
| Performance findings | kind, area, severity |
| All files | type, in use, where from |
| Safe to delete | kind, status, set (the group shows its size and how many you selected) |

A **set** is a family of names that share a start, such as `harbour_guard1`, `harbour_guard2`, `harbour_merchant` →
*harbour…*. Names without separators group by a shared start of five or more letters (`northwatchinterior`,
`northwatchgate` → *north…*). Names that share nothing with anything else are under *(not part of a set)*. Groups are
listed largest first; *why it matters* and *level* groupings are in order of importance, and on Issues the groups with
errors come first. Under each group heading a line shows a few members and, for conversations, where they are spoken
and by whom.

Pages with a list and a detail (Factions, Database, Variables & tags, Compare modules) open the detail **on the right**,
next to the list; it stays in view while you scroll and ✕ closes it. In a narrow window it opens below the list.

### 5.1 Overview
Counts, errors and warnings, impact levels, what the module is made of, and the highest-risk resources. The **Quests**
tile counts real quests: those tracked in scripts (tokens, player variables) plus journal quests the scripts use
(some persistent worlds have no journal at all and track hundreds of quests in scripts). Settings kept by systems like
DMFI are counted separately, not as quests (see 5.8).

If haks or the base game were not read, a yellow **Not everything was read** note says so: some "missing" errors may
then be things those provide, and anything they could use is kept as Review. **Load & performance** shows total size
now against after cleanup, the heaviest areas, objects with custom heartbeat scripts, and the largest files.

**Module settings & variables** (a card on the Overview) shows what is switched on in the module itself:
- the module.ifo settings in plain words (start area, XP scale, day/night, haks, custom talk table, minimum game version);
  an XP scale of 0 is pointed out, because it usually means the module hands out XP by script;
- the module event scripts (OnModuleLoad, OnClientEnter, OnPlayerChat ...);
- the variables set on the module in the toolset, and the switches and module variables scripts set
  (`SetModuleSwitch`, `SetLocalInt(GetModule(), ...)`), marked when they are set on module load;
- how many variables the scripts use, by kind.

**Server settings** (same card) lets you load what the server actually runs with, so the analysis can tell you what is
enabled rather than guess:
- **Load file** - pick the server's `settings.tml`, an `.env` file, or the `docker-compose.yml` / `nwserver.env` the
  server starts from. You can also paste the text (for example the output of `env` on the server) and press **Read**.
- The toolkit shows the settings that matter in plain words: PvP, ELC and ILR, difficulty, player count, vault
  (local / server), reload when empty, and the module the server loads.
- **NWNX plugins**: for each plugin, whether the environment switches it on or off (`NWNX_<PLUGIN>_SKIP`,
  `NWNX_CORE_SKIP_ALL`) and which of the module's scripts use it. If plugins the module needs are switched off you get
  one warning listing them with the fix; plugins used only by scripts nothing uses are shown but not warned about.
- After you analyse again, scripts the settings name count as used, so they are never offered for deletion (see 7.1).
- Other findings: the server loads a different module than the one analysed; ELC on while haks weren't read; a public
  server without a player password.
- **What is hidden.** A value whose **name** contains pass, pwd, secret, token, api key, credential, private, auth,
  cdkey, webhook, dsn or connection string, or has pw or key as a separate word (`DB_PW`, `SSH_KEY`), is kept only as
  "(set - hidden)" or "(empty)". A password written inside a URL (`user:password@host`) is hidden too. **Every other
  value is stored as it is**, in `server_config.json` in the analysis folder - check that file before you share it.
  The chip's ✕ removes a loaded file again. No re-analysis is needed: this is worked out when you open the Overview.

### 5.2 Issues
Errors and warnings are listed; tick **show info notes** for the informational ones (base-game resources and things to
know). When there are 20 errors and warnings or fewer they are shown as one list; with more, they are grouped by type
(use **Group by** to change it). Every issue has a **How to fix** line: the concrete next step (which field, which
toolset menu, which toolkit page). When the issue may be a false alarm (a hak or the base game not loaded, a name built
at run time) the first step is to confirm it. The same advice is in `reports/issues.csv` (column *fix*) and in the
answers of the facts tool (section 10). Quest problems, variables, tags and performance findings have their own *How
to fix* lines.

Some issues are by design. Tick **By design** on an issue and add a note (for example "set by the DM tool at run
time"). It is hidden (tick *show accepted* to see it again), left out of the counts on the menu and the Overview, and
stays accepted when you re-analyse. If what the issue says changes, it comes back with *changed since you accepted it*.
Accepted issues are kept in `accepted_issues.json` in the analysis folder, as your work.

Many at once: narrow the list with the search and filters, then **Accept all shown (N)…** (one note for all of them);
with *show accepted* ticked, **Un-accept all shown (N)…** makes them count again. For a long review, **Export CSV**
saves the issues shown (with a `key` column, `by_design` and `note`); fill `by_design` with yes or no in a spreadsheet
(an empty cell leaves the issue as it is), save as CSV and press **Import…**. A preview lists what will be accepted or
un-accepted and every refused row with its row number and why (an issue that no longer exists, a value that isn't yes or
no, or rows for one issue that disagree); nothing is saved until you press **Apply**.

What is checked: missing or wrong-type scripts (for example a conversation condition with no `StartingConditional()`),
uncompiled scripts, missing conversations, quests and blueprints, names that are too long, include cycles, broken
talk-table references, 2da conflicts and stale 2das, 2da rows whose label doesn't match their line (`2da_row_label`),
missing head models, symbolic links that were skipped (`symlink_skipped`, see 15), and **creature AI** problems
(creatures whose combat events are empty or never reach the combat AI, so they ignore attacks). Each issue links to
the thing it is about.

**Compiled scripts** are checked too, because the game runs the compiled `.ncs`, not the `.nss` source:
- `ncs_damaged` (error): the `.ncs` is cut off, padded, has an unknown instruction, or jumps into the middle of an
  instruction. In the game this shows as *IP OUT OF CODE SEGMENT* in the log. Recompile it, or restore it from a backup
  if there is no source.
- `ncs_stale` (warning): the `.ncs` contains text (messages, tags, variable names) that its source and includes no
  longer have, so it was compiled from another version of the source. The game runs the old version. Compare it with
  the source (**Show compiled code**, see 5.3), check that the source is the version you want, then recompile.

Scripts nothing runs get notes instead. A script whose include can't be read (a base-game include without the NWN
install folder set, or a missing hak) is not judged. The check compares text only (see 15).

**Known NWN:EE pitfalls** are checked too (each one came up again and again on the community forums):

| Category | What it means | What to do |
|---|---|---|
| `resref_case` | Blueprint names with capital letters. In EE, `GetResRef()` returns the name exactly as typed (1.69 returned lower case), so a script comparing it with lower-case text stops matching | Rename to lower case, or compare with `GetStringLowerCase(GetResRef(o))` |
| `portrait_prefix` / `portrait_files` | A creature portrait that doesn't start with `po_`, or a `portraits.2da` row whose `po_<name><size>` pictures don't exist anywhere | Fix the name, or add the pictures to a hak |
| `area_tag` | An area with an empty tag (error: reported to crash saved-character reloads on a persistent world) or several areas sharing a tag (`GetObjectByTag` only finds one) | Give each area its own tag |
| `include_depth` | An `#include` chain close to or over the compiler's limit of 64 | Flatten the chain |
| `identifier_load` | A script whose includes bring in close to the compiler's limit of names (about 16,000 in EE; an approximate count) | Split the big include library |
| `hak_size` | A hak close to or over the 2 GB limit | Split the hak |

### 5.3 Scripts
Every script with a plain-English description, its role (event, conversation, include library…), what triggers it
and its impact level. Click one to see its source and the **cascade tree**: what calls it and what it calls.

**Validate all with official compiler** (at the top of the page) compiles every script with `nwn_script_comp` (see
2.2) without writing anything into the module. Compiler errors appear on the Issues page; the result is kept in the
analysis folder (`compile.json`).

**Edit description** (in a script's panel) lets you write or correct its description. **Save description** keeps it in
`descriptions.json`; an empty description removes yours.

**Show compiled code** (in the script's panel) shows what the game actually runs. The toolkit first checks the `.ncs`
itself (intact or damaged, how many instructions, the text it contains). With `nwn_asm` in the `tools` folder, a
readable instruction listing follows. With the NWN install folder set, the listing shows engine function names;
without it, function numbers. The listing is made from a temporary copy, so the module is not touched. Compare it with
the source when a script behaves differently from what it says.

### 5.4 Items
Every item with its **inventory icon**, where copies are placed (grouped by area, then by the creature or container that
holds them), who carries, sells or creates it, quest links and duplicates. The icon is built the way the game does it:
simple items use `i<class>_<nnn>.tga`, three-part weapons stack their bottom/middle/top pictures, and armour shows its
default icon (the game assembles armour from body parts). Pictures are found in the haks, the module, the override
folder and the base game, in the game's order, so set the NWN install folder in Settings. Hover an icon for the picture
names; DDS-only pictures show "dds". For a 3D view, open the hak in a tool such as Aurora Hak Explorer (see *External
tools* in 2.1).

*Placed objects whose blueprint is not in the module* is grouped by area, then by holder, with a search, a kind filter
and **Missing only**, which hides normal copies of base-game items and creatures.

The **Palette** column says where each item sits in the toolset's palette (the same data as the Blueprints page); group
by **palette category** to see the items category by category.

### 5.4.1 Blueprints
Every blueprint from the module and its haks - items, creatures, placeables, doors, stores, triggers, encounters, sounds
and waypoints - laid out like the toolset's **Custom** palette (the base game's Standard palette is not listed). The
tabs pick the palette; the tiles count the blueprints, those in no palette category, those where the palette file is
out of date, and copies hidden by load order.

- **Palette tree**: the categories as the toolset shows them, with how many blueprints each holds (open one to see its
  sub-categories and blueprints; **Show empty categories** shows the rest). The number after a category name is its
  palette number: a blueprint sits in the category whose number matches its own (the blueprint's PaletteID; a store's
  is called ID). Type in the search box to list every match by name, tag or resref, each with its full palette path.
- **Same name**: blueprints that share a name (ignoring capitals and extra spaces; one without a name is grouped by its
  resref), with every copy and where it lives - its palette category, and whether it comes from the module or which
  hak. A copy the game never uses, because a hak listed higher (or a hak over the module) has the same resref, is listed
  too, marked **hidden by** the source that wins. **All types** puts, say, an item and a placeable with the same name
  in one group. A **#n** chip means the copy is also in duplicate group n (same content - see Duplicates); without it,
  same name means different content, so check before treating them as one. **Only names used more than once** is on
  at first.
- **Not in any palette category**: blueprints whose palette number no category has (for example 255). With no category
  to sit in they don't appear in the palette, so they are easy to lose - move them into one (below).
- **Note** column: the module's palette file (`itempalcus.itp` and so on) also keeps its own list of which blueprint
  sits where. When that list disagrees with a blueprint's own palette number, the note says so. Which of the two the
  toolset reads when it opens the module isn't something the toolkit has confirmed, so a move (below) sets both. A list
  of palette entries for blueprints that no longer exist is above the tree.
- Category names come from the talk tables (set the NWN install folder in Settings for the game's `dialog.tlk`), or
  from the English labels in a hak's palette skeleton (`itempal.itp`...). A name that can't be read shows as
  `#<number>`.
- **Filters** next to the search box: **Any category** (a category counts with all its sub-categories), **Any source**
  (the module or one hak), **Any base item** (items only: longsword, ring...; the names come from `baseitems.2da`), and
  the tick boxes **In no category**, **Palette file out of date** and **Waiting move**. In the Palette tree any search or
  filter shows one list of the matches, each with its palette path.
- **Export CSV** saves the rows in view (the same-name view adds the group), with a `waiting_move` column and an empty
  `move_to` column for importing (below). `reports/palette.csv` holds every copy of every type. A value that starts with
  `=`, `+`, `-` or `@` is written with a leading `'` so a spreadsheet shows it instead of running it.

**Moving blueprints to another category.** Tick module blueprints (in the tree, the search results or the Same name
view) - or many at once: **Select all shown (N)** ticks everything the search and filters show (on every page, not just
the one on screen), **select all** on a category heading ticks that category and its sub-categories, and **select
group** ticks a Same name group; blueprints whose copy in a hak wins are skipped and counted. Then press **Move selected
(N) to…**, pick a category (only categories that can hold blueprints are offered, each
with its number) and **Save move**. Nothing changes yet: the move waits in `palette_moves.json` in the analysis folder,
the row shows **→ new category (next build)** with **undo**, and the panel at the top lists every waiting move with
**Undo all moves**. Moving a blueprint back to where it sits cancels its move. The next **Build & audit** (its first line
counts the waiting moves) applies them to the clean module only: each blueprint gets its new palette number, and its
entry in the module's palette file moves to the new category - both logged in `changes.md` (*Palette moves*). The audit
then proves nothing else changed in those files and checks every move landed (*Palette moves landed where the change
log says*). A move is skipped, and the change log says why, when the blueprint is deleted or merged away in the same
build. Blueprints whose copy in a hak wins can't be ticked: the game loads the hak's copy, so change it in the hak. Only
existing categories can be used. `palette_moves.json` is your own work: Clear never removes it (Delete only with *edits*
ticked).

**Moving many blueprints with a spreadsheet.** Press **Export CSV**, open the file in Excel or LibreOffice, and type the
new category in the `move_to` column of each row to move - its path as the page shows it (`Weapons › Bladed ›
Longswords`; `>` or `/` between the names work too, capitals and spaces don't matter) or its number (`#24` or `24`).
Fill-down and filters in the spreadsheet make hundreds of rows quick. Save as CSV (either "CSV UTF-8" or plain "CSV"
works) and press **Import moves…**. Only the `type`, `resref` and `move_to` columns are read. A preview shows what will
happen to every row before anything is saved: the moves (from → to), waiting moves that are cancelled (`move_to` is
where the blueprint already sits), rows left unchanged (`move_to` empty), and refused rows with their row number and the
reason - an unknown type, blueprint or category, a category that can't hold blueprints, a path two categories share
(use the number), a blueprint whose copy in a hak wins, or rows for one blueprint that ask for different categories
(nothing is guessed: all its rows are refused). **Save** stores exactly what the preview showed; the moves then wait for
the next Build & audit like any other.

**Changing fields in bulk.** Tick blueprints of one type and press **Change fields…**. Fill only what should change;
empty boxes stay as they are. What can be changed: items - name, description, unidentified description, tag;
creatures - first name, last name, description, tag, faction (picked by name from the module's factions); placeables
and doors - name, description, tag; stores and sounds - name, tag; triggers and encounters - name, tag; waypoints -
name, description, tag. **Preview…** lists every change (from → to) and every refusal with its reason, then **Save N
changes** stores them in `blueprint_changes.json` in the analysis folder. As with moves, nothing changes until the next
**Build & audit**, which sets the new values on the blueprint and on every copy placed in an area that still holds the
blueprint's old value (a placed copy you changed by hand keeps yours); every change is logged in `changes.md`
(*Blueprint changes*) and the audit checks each one landed. A new name or description replaces the text in every
language. Refused, with the reason: a blueprint whose copy in a hak wins; a tag longer than 32 characters or blank; a
tag that scripts or conversations look up (renaming it would break them - use **Find & replace**, which renames it
everywhere); a new tag that scripts already look up for something else; a faction the module doesn't have. Waiting
changes show on each row and in the panel at the top, each with **undo**. In a spreadsheet, the export's `new_name`,
`new_tag`, `new_description`, `new_faction`... columns work like `move_to`: fill them, **Import…**, check the preview,
**Save**. `blueprint_changes.json` is your own work, like the moves.

The page is built from the analysis; an analysis made before this page existed gets its data the first time the page
is opened (nothing is re-analysed, and only `palette.json` and `reports/palette.csv` are written in the analysis
folder). Like a clean build, it counts the module and its haks only, not your own override folder.

### 5.5 Duplicates
Identical blueprints, scripts, conversations and assets; items with the same stats; shared tags; same names. A
**keeper** is suggested: the in-use, compiled, most-referenced copy. Groups marked *mergeable* can be merged by the
build. The others need a human decision.

**Models and textures with the same content (module and haks)**, below the groups: model and texture files that are the
same under different names, found in the module and every hak - for example a head copied to a new head number. Textures
count as the same when their bytes are identical; models when only their own name differs (a model writes its name
inside itself, so two copies of a head are never byte-identical). The same name carried by two haks is not listed here
(load order simply picks one copy). Each group shows its members with the haks they come from, the head number for
head models, whether the module uses the member, and how much the extra copies take. These are for review only: models
and 2das point at textures by name and heads are picked by number, so nothing here is merged or removed automatically -
remove a copy (with the Hak editor) only when nothing uses that name or number. Filter by kind, source or **Heads
only**; **Export CSV** saves the rows shown, and `reports/asset_duplicates.csv` has every group. Analyse again to find
models that differ only in their name (older analyses find byte-identical copies only).

**Export CSV** saves the groups shown, one row each (members, keeper, whether it can be merged and why not, and a `merge`
column). Fill `merge` with yes or no, save as CSV and press **Import…**: a preview shows which groups will be ticked or
un-ticked and refuses, with the reason, a group that needs a manual decision or no longer exists. **Apply** changes the
ticks, which the next Build & audit uses.

Only truly identical copies are ever merged automatically: everything must match apart from the resource name, comment
and palette slot, including colours, model parts, appearance, names, tags, properties and scripts. Look-alike groups
(*same stats, different name/look*, *shared tag*, *same name*) show chips for each copy saying how it differs from the
keeper: **look** (model parts, cloth/leather/metal/tattoo/hair/skin colours, appearance, portrait, phenotype, tail,
wings, visual transform), **name**, **tag**, **stats**, **scripts**, **inventory**, **variables**. Hover a chip for the
fields. Item copies show their icons side by side, so look differences are visible at a glance.

**Use keeper instead…** (on a copy of a blueprint) first shows every field that differs, with look differences
highlighted, because players will see the keeper's look wherever it replaces the copy. Continue and it opens
**Find & replace** with every use of the copy found and the keeper's name ready to put in (see 6.3).

### 5.6 Hierarchy
The Module › Area › Object › Inventory tree. **Find object** searches every placed object by name, tag or template.

### 5.7 Areas and Conversations
Area contents, event scripts and transitions (including scripted `JumpTo…` transitions). Conversations are shown as
readable dialogue trees with their conditions and actions, plus nodes that cannot be reached from the start.
The Conversations list shows **where** each one is spoken (the areas where the NPC or placeable using it stands, placed
copies of its blueprint included) and a chip on conversations that drive quests. Group it by set to see a family of
conversations together, by area to see everything spoken in one place, or by *quest giver*. A placed NPC that was given
a different conversation in the area counts only for its own conversation.

**Started by** lists scripts that start a conversation themselves (`ActionStartConversation`, `BeginConversation`,
`StartConversation`) with the line number and what runs that script (for example *OnUsed of Lever*, or *dlg script from
dlg_guard*). A script that only holds the name in a string (`SetLocalString(o, "sConv", "dlg_x")`) is marked *names it*:
it may start it later from that variable. `string s = "dlg_x"; BeginConversation(s);` is followed to dlg_x (and
reported as a missing conversation when there is none). A text comparison such as `if (sCmd == "WAITRESS")` is not a
start. Group by **how it starts** to separate conversations spoken by placed NPCs, by spawned creatures only
(blueprint), started by a script, or not used at all. The note at the top lists every place that starts a conversation
from a name held in a variable - those can't be linked to one conversation, so check them before removing a
conversation that looks unused. Also in `reports/conversations.csv`.

### 5.8 Quest health
Two tabs.

**Quests from scripts** finds quests that don't use the journal. Many modules keep quest progress in variables, and
the toolkit reads every script to follow them:
- **token systems**: a pair of helper functions in the module's own include, such as
  `SetQuestToken(oPC, QT_RATS_DONE, "1")` / `GetQuestToken(oPC, QT_RATS_DONE) == "1"` (one character per quest in a
  string kept on the player). The pair is found automatically, and the slot names (`QT_RATS_DONE = 51`) and their
  comments label the quest;
- **variables** on the player (including items the player wears, like the hide) or on the module:
  `SetLocalInt(GetPCSpeaker(), "nBeenPaid", 1)`;
- the **campaign database**: `SetCampaignInt("world", "RUNE_Q", 2, oPC)`;
- **journal** calls: `AddJournalQuestEntry("q_rats", 2, oPC)`;
- helper functions that pass a name on (`SetStage(oPC, "Q_BEAR", 2)` calling `SetLocalInt`) are followed through;
- **generic scripts configured on the conversation node** (EE script parameters): one script such as
  `gen_c_dobounty` is used by many conversations, and each node tells it which variable to set (`VAR_NAME`,
  `VALUE`, `Q_ITEM`...). Each node becomes its own fact, so the quest shows only the conversations that name its
  variable; its stages say *(script parameters)*. `COMPARE` picks the comparison, `SELF=1` means the NPC's variable.

**Grouped by system or package, then by set** (the default; *Group by* also offers quest giver, health or one list).
Not everything the scripts keep on the player is a quest, so each entry is put in a family:

| Family | What it is | Example |
|---|---|---|
| *<INCLUDE> quest tokens* | quest slots in one string, from a token helper pair; named after the include | **Q_TOKENS quest tokens (q_tokens_inc)** |
| *Journal quests* | journal quests the scripts use (a BioWare script's own entry, like the henchman's, goes to the BioWare package) | - |
| *Module quests (player variables)* | variables on the player that a conversation sets or checks | `q_harbour_done`, `SMITH_*` |
| *Module systems & settings* | module-wide state, or player state no conversation touches (weather, rest, games of chance, the database) | `MOD_WEATHER_*` |
| *DM tools (the module's own)* | state of the module's DM wands and DM conversations | `dm_rp_*`, `NoDespawn` |
| packages | a known community system's own settings, **not quests**: DMFI, Jasperre's AI, Tony K's henchman AI, BioWare `NW_`/`X0_`-`X3_`, CEP, NWNX, Simple Addiction System, Sunjammer's library, Dead and Wild Magic | DMFI settings |

A package is recognised from the variable name first (`X3_HORSE_*` is BioWare wherever it is set), or when more than half
of the scripts using it belong to the package (one package script among several is not enough). Per-player database
entries that conversations use count as quests. Inside a family, entries are grouped into **sets** by the quest's
name - the constant or variable name, not its comment: `QT_*` (QT_RATS_DONE, QT_WOLVES_DONE…), `GUILD_*`. Names that
share nothing are grouped when the same conversation gives them (*given in q_innkeeper*). A token family also lists how
many slots are free and the slot names defined in the include but never used.

Use **quests only** to hide packages and settings, and the system filter to see one family. The Overview **Quests** tile
counts real quests only and says how many settings of systems like DMFI were set aside. Grouped **by quest giver**,
quests sit under the conversation that drives them (e.g. *Captain Hale*, `q_captain_hale`). Click one for its
**stages**: each value, who sets it and who checks it (conversation node, object event or script, with line numbers),
the items it needs, takes or gives, and its rewards.

Each quest gets a health mark:
- **broken**: a check that can never be true (for example `== 3` when nothing ever sets 3), a slot past the end of the
  token string, two quest names sharing one slot, or a needed item nobody can obtain;
- **warning**: something to check by hand - for example a check that looks impossible while a name built at run time
  could match it, or a needed item tag that isn't in the module (it may be a base-game item);
- **ok**, or **legacy**: only scripts the module never runs touch it.

The tile **How these were found** lists the helper pairs detected, the token string length, and how many reads and
writes used names built at run time. Everything is also in `reports/inferred_quests.csv` (columns *family*, *set*,
*name*). A "broken" quest is strong evidence, not proof (see 15).

**Journal quests** is the journal view: for each journal quest, who updates it (and whether those scripts are in use),
the items it needs and their status (*ok*, *missing* = no blueprint, *unobtainable* = the blueprint is never placed,
carried, sold or created), and quests that are referenced but missing from the journal. Item status needs the module's
haks (see 15).

### 5.9 Variables & tags
Three tabs, also in `reports/variables.csv`, `tag_lookups.csv` and `token_slots.csv`.
- **Variables**: every local and campaign variable and helper-pair value the scripts use, plus the variables typed into
  the toolset (the Variables list of blueprints, placed objects, areas and the module). Problems:
  - *read, never set*: a script reads it, but nothing sets it, so the read always gets 0 or "". This is only *info*
    when something outside could set it: the game or base-game scripts (`X2_…`, `NW_…`), a server plug-in, another
    module sharing the campaign database, or a name built at run time;
  - *wrong type*: set as an int but read as a string (NWN keeps int, string, float, object and location variables apart);
  - *capital letters differ*: `nDoneKobolds` set, `nDonekobolds` read (names are case-sensitive);
  - *similar name*: one letter away from a name that is set (a typo?);
  - *set, never read* (info).
- **Tags looked up**: every tag the scripts look up by name (`GetObjectByTag`, `GetWaypointByTag`, `GetNearestObjectByTag`,
  `GetItemPossessedBy`…). Statuses: *placed*, *spawned/sold at run time* (only on a blueprint that is spawned, sold or
  carried), *tag given at run time* (`CreateObject(…, "NEWTAG")`, `SetTag`), *only on an unused blueprint* (the lookup
  finds nothing), *capital letters differ* (tags are case-sensitive), *no such tag*.
- **Token slots**: every slot of the module's quest-token string(s) with a coloured map. For each slot: the names that
  use it, the values set, who sets it and who reads it, and statuses *used*, *shared* (two names on one slot), *read,
  never set*, *set, never read*, *named, unused* and **free**, so you can pick a free slot for a new quest. A module can
  have two separate token strings (two includes with the same function names); the tab shows one map at a time with a
  button for each string.

Type and case problems also appear on the Issues page.

### 5.10 Factions
Factions decide who fights whom. **Factions** (under Analysis) reads the module's faction table (`repute.fac`, the
toolset's Tools > Faction Editor; the copy the game loads, see *Load order* in 16) and shows:
- **Factions**: each faction with how it feels about players (*hostile* 0-10, *neutral*, *friendly* 90-100), the standard
  faction it was copied from, whether it is **global** (annoy one member and the whole faction turns on you), how many
  creatures are placed and where, and which factions it is hostile towards. Click one for its likes and dislikes (both
  ways), members, the tags of placed members (the "faction holders" scripts use), and the scripts that change it. Group
  by the standard faction it was copied from, or by how it treats players.
- **Who feels what**: the whole table as a grid - each row is how that faction feels about each column (red hostile,
  grey neutral, green friendly; hover for the number).
- **Scripts**: every line that changes factions or reputation - `ChangeFaction` (joining the faction of a creature found
  by tag: the tag is resolved to its faction), `ChangeToStandardFaction`, `AdjustReputation`,
  `SetStandardFactionReputation`, `ClearPersonalReputation`, `SetIsTemporaryEnemy/Friend/Neutral` - with what runs the
  script. When the script is run by an NPC (its conversation or event), the NPC's faction is shown.

Issues: a creature with a faction number the table doesn't have (*faction invalid*), a `ChangeFaction` whose holder tag
no placed creature has (*faction holder missing*), and factions nothing uses (*faction unused*, info). Also in
`reports/factions.csv` and `reports/faction_scripts.csv`, and the facts tool `faction`.

### 5.11 Database
What the module keeps between server restarts: **campaign databases** (`SetCampaignInt/String...`,
`StoreCampaignObject` - quest progress, bank chests, player data) and SQL tables.
- The scripts are always checked: every database and variable they store or read, the type, and whether it is per
  character or shared. Helper functions (such as DMFI's `GetDMFIPersistentInt("dmfi", "dmfi_emotemute", oPC)`) are
  followed to the real names.
- **Load what is stored**, three ways: **Read database folder** (type the game's `Neverwinter Nights/database` folder -
  filled in for you when the user folder is set - or a copy of the server's), **Browse…** to pick the folder, or
  **Load files…** to pick database files from anywhere (several at once, e.g. the server's copies; they are copied into
  the analysis, read, and the copies deleted). EE `.sqlite3` files (including their compressed values), old BioWare
  `.DBF/.FPT` files, other SQLite files (NWNX) and `.sql` dumps are read. **Read-only**: each file is copied first, so a
  running server's database is never locked or changed. Values are hidden by the same name rule as server settings
  (see 5.1). **Forget loaded files** drops what was loaded.
- **Needs a look** (warnings, also on the Issues page): a value read but never written (the read always gets 0 or ""),
  and a value written as one type but read as another (campaign values are found only with the same type). When the
  name matches a quest token, the fix says to read the token instead (for example a script still reads `RATS_Q` from
  the database, but that quest moved to the `QT_RATS_DONE` token).
- **Good to know**: databases the scripts use with no file in the loaded folder (normal for a fresh server), files no
  script in this module uses (the folder is shared by every module), stored values nothing uses, old `.DBF` copies EE
  has already imported, `DestroyCampaignDatabase` calls, and SQL tables the queries use.
- Tabs: Databases, Variables (who writes/reads each, stored values per character), SQL, Files, Script lines.

### 5.12 Performance
What costs a persistent-world server CPU, and players bandwidth. Also in `reports/performance_*.csv`.
- **Heartbeats by load** (the first tab): every heartbeat script ranked by how much server work it causes. Load =
  copies running (placed objects, areas, the module - each runs every 6 seconds) × a cost per run read from the code and
  the include functions it calls: walking every object in an area or every player, nearest-object searches, database
  reads and writes, creating objects, delayed actions. A script that returns early (e.g. when nobody is around) counts
  at half. It ranks scripts - it is not a timing measurement. Standard creature AI usually dominates (its cost comes from
  how many creatures are placed); custom scripts show their share *among custom scripts* so they can be compared. Click
  a row for where it runs (areas, object kinds), what one run does, and how to fix it. The tile shows heartbeat runs a
  minute and the biggest one.
- **Areas by load**: per area, the creatures placed, what its encounters can spawn, objects running custom heartbeat
  scripts (every 6 seconds, forever) and the standard AI heartbeat, placeables, placeables that could be Static, items and
  huge inventories, with a load score to sort by.
- **Findings**:
  - crowded areas (more than 60 creatures) and areas with more than 10 custom heartbeats;
  - heartbeat scripts that walk every object or player (`GetFirstObjectInArea`…);
  - empty heartbeat scripts (they still fire; clear the event instead);
  - `DelayCommand` inside a loop (one queued action per pass), and functions that reschedule themselves with
    `DelayCommand` (a hidden heartbeat that never stops);
  - containers over 50 items and stores over 300;
  - placeables that could be ticked **Static** (not usable, no scripts, no inventory, no conversation, tag never looked
    up). Static placeables cost nothing at run time.

Warnings also appear on the Issues page, where you can accept them as by design.

### 5.13 Orphans
Unreferenced scripts and conversations, clustered by shared quest tag, item tag, links and name stem, so that a removed
quest's leftovers appear as one group.

### 5.14 Skins & hides
Creature items: their properties, which creatures wear them (blueprints and placed copies, by slot), which scripts
give them, and missing hides.

### 5.15 Hak catalogue
For each hak: the files it carries against the files the module references, 2da rows provided against rows used
(appearance, placeables, baseitems, portraits, soundsets, classes, races, feats, spells…), tilesets and the areas that
use them, and the load order with the files each source hides from the ones below it (see *Load order* in 16).
- **2da rows** are counted by their line position, as the game does (see 6.2). A row whose label doesn't match its
  line gets a `2da_row_label` warning. When several sources carry the same 2da, only the rows of the copy the game
  loads are listed.
- **Talk table**: used and unused custom entries, and broken string references that would show as blank in game.
- **2da conflicts**: the same 2da in several haks with the rows that never load, and hak 2das older than your installed
  game (hidden base rows or columns, plus the customised rows you would need to re-apply).

### 5.16 Impact explorer
Pick anything to see its impact level and the exact chain of references behind it. The chain is worked out when you
open an item.
- **Critical**: runs from a module-wide event, or reaches 10+ areas or 25+ scripts.
- **High**: touches a quest, or reaches 3+ areas or 8+ scripts.
- **Medium**: reaches an area, or has 3+ dependents.
- **Low**: something depends on it. **None**: nothing does.

### 5.17 All files
Every file in the module, the haks and the override folder, in use or not. Search across everything, with texture
previews and the raw data of every field.

---

## 6. Editing and generating scripts
- **Detail panel → Edit**: scripts, 2das and text files open as text, and GFF files (items, creatures, areas…) as JSON.
  Saving writes to the analysis's `edits` folder. **The original module is never changed.** Scripts can be
  compile-checked on save.
- **My edits** lists every edited or new file. **Discard** takes one out; the file is kept in
  `edits/.history/discarded/<time>/` (copy it back into `edits` to restore it).
- **Script generator** works in the style of Lilac Soul's generator: pick the event, conditions and actions, and get a
  commented script with the `#include`s for your own libraries added, compile-checked.
  - **36 events**, including the EE ones: OnPlayerChat, OnPlayerTarget, OnPlayerGUIEvent, placeable OnClick, equip and
    unequip, level up, rest, dying and respawn, and more. The header of each script says where it goes.
  - **Actions and conditions** cover timed effects, ability and skill boosts, typed damage, visual effects, party-wide
    rewards, spawning, campaign database values, and EE SQLite values stored on the player (they travel with the character).
    Pickers for constants (abilities, skills, feats, spells, races, visual effects, animations…) are filled from **your
    installed game's `nwscript.nss`**, so they match your game version. Every function and constant is checked against it,
    and anything your game doesn't have is flagged.
  - **Your module's token system**: when the analysis found a token helper pair, *Set a quest token* and *Quest token
    at stage* appear, with the module's own slot names to pick from and the include added.
  - Free text is always placed inside quotes and escaped, so a value can never turn into code; numbers, names and
    stages are checked. Saving under a name the module or your edits already use asks first.
- Edits go into the next clean build and the audit lists them (and whether they compiled).
- A module 2da opened from the detail panel has an **Open in 2da editor** button (see 6.2). The saved result goes
  into your edits in the same way.

### 6.1 Hak editor
**Edit → Hak editor.** Open any `.hak`: type its path, use **Browse…**, or click one of the current module's haks.
Opening a hak creates a **project** in the workspace (`nwn_workspace/_haks`). The project holds your staged changes,
rebuilt haks and extracted files. **Nothing on disk changes until you press Add to hak folder - and even then nothing is
replaced.** **Discard project…** (in the list of projects) deletes a project's staged changes, rebuilt haks and
extracted files; the hak itself and any backups are kept.

- **Usage checks.** If an analysed module lists this hak, it is picked automatically ("Usage checks from analysis").
  You then see which files **the module uses**, which files are **hidden by a hak above** (the game will never load
  them from this hak), and which 2da rows are in use. Without an analysis the editor still works, but it can't warn
  you about breaking the module.
- **Add files…** (pick one or many) and **Add folder…** (everything in it, including subfolders; hidden files are
  skipped, and symbolic links and system files such as Thumbs.db are skipped with a note). In a file's panel: **Replace…**, **Rename…**, **Remove** and, for a
  2da, **Edit 2da**. Each change is staged and listed under *Staged changes*, with **Undo** on every one. **Undo all
  changes…** puts the list back to exactly what is in the hak.
- **Extract all…** and **Extract selected** write files (with your staged changes) into the project's folder, or into
  an empty or new folder you type. A file path, or a folder with anything in it, is refused. Extracting is all or
  nothing: if one file can't be read, nothing is left behind.
- **Checks** explain every problem in plain words. **Errors block the rebuild**:
  - names over 16 characters, spaces or odd characters, and file types NWN can't load
  - removing or renaming a file the module uses. Use **Remove anyway** only if you know it is dead.
  - a 2da that won't parse, or a GFF file that won't read
  - the hak having changed on disk since you opened it

  **Warnings:** a model whose internal name doesn't match its file name; a model whose textures or supermodel can't
  be found; a file that a hak listed above it hides anyway; a hak approaching 2 GB.
- **Rebuild** writes the hak under a **new name** - `<hak>_r1`, `_r2`... (the next name not already in your hak
  folder; 16 characters at most) - into the project's `builds` folder, then re-reads it and proves every file is
  byte-for-byte what you intended. It reads one file at a time, so even a 1 GB hak needs little memory. The hak's
  description is carried over. A rebuild is written uncompressed, even when the original used EE's compressed format.
- **Add to hak folder…** (on a verified rebuild) is the only step that touches your hak folder, and it only ever
  *adds* a file:
  1. It checks the build is verified, that nothing was staged after it, and that the hak on disk hasn't changed.
  2. It copies the rebuild into the hak's folder under its new name and checks the copy. If a file with that name is
     already there, nothing is written. The original hak is never touched.
  3. **The module is pointed at the new name**: the hak list in `module.ifo` of the linked analysis is changed as an
     edit (see *My edits*). Your next **Build & audit** makes a clean module that uses the new hak, and the audit
     checks it against the new hak. No analysis linked? Pick one, or change the hak list in the toolset.
  4. The project continues from the new hak, so the next rebuild becomes `_r2`.

  The *Rebuilt haks* table shows when each one was **Added**. Players and your server need the new hak file.
  Re-analyse the module with it so usage information (and lean haks) are up to date. To undo, see 1. Undo and
  recovery. If the dashboard was closed during an Add, the unfinished copy is removed the next time the hak is opened.
- **2das inside a hak keep their names** (`appearance.2da`, `classes.2da`...): the game and scripts look them up by
  name, so renaming one would break it. Your originals are protected because the *hak* holding them gets a new name.
- **Backups…** lists backups made by older versions of the toolkit (which replaced haks in place) and by Restore.
  **Restore…** puts one back over the hak: close the game, toolset and server first. The current hak is backed up
  before it is replaced.
- **If the hak changed on disk** (another tool, or a Restore), the editor says so and offers **Use the file on disk as
  the new starting point**. Files you added or replaced stay staged, removals and renames are reset, and backups are
  kept.

### 6.2 2da editor
Opened from the Hak editor (**Edit 2da**) or from a module 2da in the detail panel. It is a spreadsheet-style grid:
click a cell and type. Leaving a cell empty (or typing four stars, `****`) means "no value" (\*\*\*\* in the file).
Values with spaces are quoted for you when saving.

> **The one rule that matters: NWN finds 2da rows by position.** Row 214 of appearance.2da is "the 215th line", not
> "the line labelled 214". Deleting or inserting a row in the middle moves every row after it. Every creature, item,
> placeable, other 2da or player character that pointed at those rows silently points at the wrong one. So:
> **add new rows at the end** (Add row at end, or a row's ⋯ → *Copy this row to the end*), and **retire a row by
> blanking it** (⋯ → *Blank this row*), not by deleting it.

- Each row shows its number, **used ×N** when the module uses it (hover to see what uses it), *new* or *was N*
  when it has moved, and a ⋯ menu (blank, copy to end, delete, insert above). The column ⋯ menu renames or deletes a column.
- **Checks update as you type:**
  - wrong or duplicate column names, and a `"` inside a value
  - row numbers that don't match their position (**Renumber rows** fixes them)
  - talk-table numbers that don't exist (shown blank in game)
  - resource names over 16 characters, and scripts named in the table that don't exist
  - rows the module uses that would move or disappear
  - scripts that read this table by row number
  - a comparison with **your installed game's copy**, which shows rows this copy hides (it's older than your game) and rows it customises
- **Save** refuses anything that would break the file. If the change moves or deletes rows the module uses, it
  lists them and asks you to confirm ("I understand these references will point at different rows"). It also refuses to
  save over a copy that changed in another tab since you opened it.
- Filters: find text, rows with issues, rows the module uses, and changed rows. Tables are shown 100 rows per page.

### 6.3 Find & replace (rename everywhere)
**Find & replace** (under Edit) searches the whole module:
- script lines,
- every field of every conversation (lines, speakers, condition and action script slots),
- every field of every blueprint, placed object, area, store and `module.ifo` (tags, resrefs, names, variables,
  inventories, event scripts),
- 2da cells,
- file names.

Choose how to match: *part of a value*, *whole word*, or *whole value*. In scripts, *whole value* means the whole string
in quotes, e.g. `"GoblinChiefHead"`. *match capitals* is on to start: names (tags, variables, scripts) are
case-sensitive in NWN, so `nGuardRank` and `nGuardrank` are two variables. If you untick it, a name with different
capitals is still refused (search for it separately), and in text (dialogue, item names) the capitals are kept:
*Grisk* → *Grosk*, *GRISK* → *GROSK*. Pick what to search, and *module only* or *module + haks*.

Your own **edited and new scripts** are searched as they are now. Object and conversation files you edited are searched
as they were analysed (the page says which; see 15).

Results are grouped by file, and each hit says exactly where it is: *line 13*, `EntryList[10]/Script`,
`Creature List[4]/Tag`, *row 12, Label*. Click the owner to see the object in context. If a name built at run time
(`"spawn_" + n`) could produce what you searched for, a warning lists those scripts: such uses can't be found or changed.
Scripts that build names from an object's tag (`"HATE:" + GetTag(oNPC)`) are listed when you search a tag: renaming the
tag changes those names too.

To rename or replace, tick the hits (all module hits are ticked to start), type the new value and press
**Preview changes…**. The preview shows every change as *before → after*, and every hit that can't be changed and why:
- it is in a hak (use the Hak editor),
- the new name is not a valid resource name (16 characters at most),
- the new tag is over 32 characters.

The preview runs every check Apply does (a file with the new name already exists, text that lives in the talk table,
a file changed since the search, a blueprint's own name - rename the file instead). A rename (*whole word* / *whole
value*) is all or nothing: Apply is blocked until every ticked use can change - untick a hit only if leaving it is what
you want. If any hit is a resource name or a tag, the new value must suit it everywhere. Notes in the preview remind you
that values players already saved under an old variable name are not renamed, and that changed scripts must be
compiled again. Line endings of your files are kept.

Tick *I've checked the changes* and **Apply to my edits**. The changed files are written to your edits (never the
original module). Renaming a file adds the renamed copy as a new file and drops the old name from the clean build; the
build's audit then flags anything still using the old name. **Earlier renames / replaces** lists each change with
**Undo**, newest first. Undo puts the edited files back as they were just before and removes files the change created.

---

## 7. Cleaning up

### 7.1 Safe to delete
Found by following every reference from `module.ifo`.
- **Safe**: nothing reaches it, and none of the Review reasons below applies.
- **Safe as group**: only used by other unused things, so they go together (the build enforces it). If one of those
  users is Review, this is Review too - decide on the user first.
- **Review**: probably unused, but something could still reach it at run time. Review items are never deleted unless
  you tick **allow Review items**.

**Export CSV** saves the candidates shown (node, files, size, status, reason and a `delete` column, "yes" for the ticked
ones). Go through them in a spreadsheet, put yes or no in `delete`, save as CSV and press **Import…**: the preview shows
what will be ticked and un-ticked, and refuses a Review item unless *allow Review items* is ticked, and anything no
longer on the list. **Apply** changes the ticks; nothing is deleted until you build.

An item is Review, never Safe, when:
- its name matches a piece of a name scripts build at run time (`"evt_" + n`, or `s = PREFIX; s += x`; see 15);
- it has the same name as a base-game file (it overrides it), or it is an asset and the NWN install folder is not set,
  so that can't be checked;
- a hak was not loaded: for scripts, models, assets and 2das (hak 2das can name them), and for conversations (a hak
  item's script can open a module conversation by name);
- it is a whole area that nothing links to;
- it is a model or texture whose name the engine builds from a 2da row: portraits (`po_…`), helmets, cloaks and other
  row-numbered part models (`helm_015`), phenotype body parts (two-digit `pmh0_…`-style names), item model parts and
  icons, creature models, tiles, loading screens and music. Minimal lean haks keep these too;
- it is a script whose name ends in an item tag, and a script sets the tag-based prefix
  (`SetUserDefinedItemEventPrefix`) from a value the analysis can't read. When the prefix can be read, the item scripts
  it leads to are followed and kept as used;
- it is a script, the module includes `nwnx_*` scripts, and no server settings are loaded: NWNX can run a script named
  only in the server's settings. Load them (Overview → Server settings) and analyse again. Scripts the loaded settings
  name are kept as used;
- it is a blueprint, and a script creates that kind of object from a name it reads at run time (a variable, a
  database value). Merging duplicates of that kind is blocked too;
- it is a blueprint the toolset palette lists ("only in the toolset palette": DMs can spawn it), or the module has no
  palette file of its kind. A blueprint can only be Safe when its palette exists and does not list it;
- something outside the list mentions it, or the module could not be read completely.

If the module could not be read completely (no `module.ifo`, or entries of unknown type - a damaged file), every item
is Review and the Issues page says why. Each group heading shows its size and how many you ticked. **Select all Safe**
ticks every Safe and Safe-as-group item; **Clear selection** unticks everything.

Your ticks are remembered per analysis in this browser.

### 7.2 Build & audit
Writes `<name>_clean` (a folder plus a `.mod`), `changes.md` (every change and the size line: module, haks and total
download before and after) and the audit. If Git is installed, the clean folder is also a git repository: the first
commit is the untouched original, tagged `original` (see 9). Without Git, the result says "Git: not installed - no
version history"; everything else works the same.

When the original is a `.mod`, the clean `.mod` keeps its description (shown on the game's Load Module screen). An
original `.mod` that lists one name twice (two entries whose names differ only in capitals) gives the clean module the
first one, as neverwinter.nim's tools do, with a warning.

**Lean haks** (optional, recommended for a persistent world, because players download the haks and size means load time):
- *Builder mode* (the default): the haks stay a builder's palette. Only duplicate copies and dead-end content (such as orphan
  walkmeshes) are dropped. Everything else is listed for your review.
- *Minimal mode*: keeps only what the module reaches, plus 2das, talk tables, tilesets and the engine-named models and
  textures listed in 7.1. This suits a client download for a finished module, not a builder's palette.
- Your own override folder never counts: players don't have it, so a file found only there doesn't keep a hak's copy,
  and the audit re-reads the clean module without it.
- Each lean hak keeps the original's description. A lean copy of a compressed (EE E1.0) hak is written uncompressed,
  so it can be larger than the original; a warning says so.
- Only haks that change are rebuilt. A hak with nothing to drop is left alone: no copy is written, it keeps its own
  name in the clean module's hak list, and the audit checks the clean module against the original in your hak
  folder. `changes.md` and the result card say how many haks were rebuilt, how many were left as they are, and how
  much the rebuilt ones saved.
- A new build replaces only the lean haks an earlier build wrote in `build/haks` (listed in its `manifest.json`).
  Other files there are left alone.

**New names - nothing can overwrite an original.** Everything the build rebuilds gets a new name, so copying the build
into your game folders never replaces an original:
- the clean module uses the name you type (default `<module>_clean`); the original's own name is refused;
- each rebuilt lean hak is renamed `<hak>_r1` (then `_r2`, ... - 16 characters at most, the game's limit), skipping
  any name already in your hak folder, e.g. an earlier rebuild you added. The clean module's hak list points at the
  new names, the change is logged field by field, and the audit treats the renamed hak as the same hak. Haks left as
  they are keep their names;
- `changes.md` and the result card list the old → new names. Copy the files in `build/haks` into your hak folder next
  to the originals, and the clean `.mod` into your modules folder - or let **Add to game folders** do it.

**Add to game folders.** After a build, the result card has an **Add to game folders…** button. It needs your NWN
user folder in Settings (the one with the `modules` and `hak` folders). It first shows what would be copied where -
the clean `.mod` into `modules`, each **rebuilt** hak from `build/haks` into `hak`, with sizes. Haks the build left as
they are are not copied (you already have them), and neither is the custom talk table (the build does not change it).
Type **ADD** to confirm; the copy runs as a job with progress.
- **Nothing is replaced.** If any of the names already exists in its folder (in any capitals), nothing is copied:
  build again with another name, or press **Undo** first if it is your earlier add of this build.
- It is refused if the build's audit verdict is **FAIL** - unless you confirm a second time that you want it anyway -
  or if a file in the build changed since the build (each file's checksum is compared).
- Each file is copied under a temporary name, checked byte for byte against the build, and only then given its name.
  The record `build/installed.json` is written before the first copy and updated after each file, so even a crash
  leaves an exact list. If a copy fails, the files already added stay and are listed; **Undo** removes them.
- **Undo** removes exactly the files that add put in, and only while they are unchanged. A file you changed or
  replaced since is left alone and listed. Nothing else in your folders is touched.

**Edited includes.** An include (a script with no `main()`, such as `q_tokens_inc`) is copied into every script that
`#include`s it when that script is compiled. So when you edit one, the build recompiles **every script that includes
it**, directly or through another include. It uses the include copy the toolset would use: a hak's copy over the
module's, by load order, and never one from your override folder. This needs the official compiler (see 2.2): without
it the build stops before writing anything, because those scripts would silently keep running the old code. The audit
fails if any of them kept its old `.ncs`.

**Progress.** While it runs the job box shows the step it is on, the whole step list (done steps with how long they
took), a progress bar and the time left. Most of a build is the audit, which re-reads and re-analyses the clean
module, so the first estimate comes from how long this module's analysis took; later builds use the previous
build's step times.

The build refuses an output that could overwrite the original module, and only ever replaces a folder or `.mod` an
earlier toolkit build made. A plan naming things that don't exist is reported in the change log. **Stop** (on the job
log) stops a build at the next safe point; the build is then incomplete - build again before using it. An empty,
unnamed entry some modules carry (the game ignores it) is dropped with a note.

**The automatic audit** re-reads the clean module (with the game folder in Settings, or the one the original was
analysed with when Settings has none) and checks that:
- every file can be read, and the `.mod` matches the folder byte for byte,
- only approved files were removed, and every changed field is a logged change,
- there are no new broken references, and nothing in use still points at something removed,
- every area keeps its placed objects, and every placed object keeps its position, tag and area,
- journal quests and conversations are preserved,
- everything in use is still reachable,
- lean haks keep every 2da identical,
- all scripts compile (with the compiler installed), and every edited script was compiled: an edited script whose
  `.ncs` is still the original is a warning - the game would run the old code.

The verdict is **PASS**, **PASS WITH WARNINGS** or **FAIL**. A FAIL stops everything.


---

## 8. Play-testing: the Log monitor
Start it on the **Log monitor** page while you play or while the server runs. Leave the folder blank to use your
user folder's `logs` folder (`logs.0` on older game versions). Errors from `nwserverLog1.txt`, `nwclientLog1.txt`,
`nwengineLog.txt` and the script compiler log are grouped live and linked to the script, where the object lives, and
its impact level.

Each row explains itself: **what it means** in plain words, whether it **needs fixing / depends / is harmless**, and
**how to fix it**. Examples:
- *IP OUT OF CODE SEGMENT* - the script's compiled `.ncs` doesn't match the game; recompile it. The line after it
  ("took N us, error: -646") is the same failure.
- *SCRIPT_ABORT ... without NWNX running* - expected when testing locally; on the server check NWNX (Overview > Server
  settings).
- *Failed to demand x.2da* - a 2da table that doesn't exist; the row names the script that asks for it, and says when
  the tables are made at run time by NWNX (for example loot tables a server plug-in creates).
- Sound driver, launcher (GOG/Steam), game self-checks and "no main()" for include files are marked harmless and hidden
  unless you tick **show harmless messages**.

Every new error group after a cleanup is either a bug that was already there (log it) or a regression (stop, fix,
rebuild).

---

## 9. Git and GitHub
If Git is installed (it is optional), each clean build folder is a git repository.
`git -C <clean folder> diff original HEAD --stat` shows every change since the original. To back it up, create a
**private** GitHub repository and run `git remote add origin <url>` then `git push -u origin HEAD --tags` inside the
clean folder. Module content may be copyrighted, so keep such repositories private.

---

## 10. Checking facts (read-only)
Before changing something, check it rather than guess ("that script is only used by…", "that item exists", "that
compiles"). The toolkit answers such questions from the analysis index, opened **read-only**.
- `python nwn_facts.py <analysis> summary|exists|who-uses|uses|script|grep|variable|tag|quest|faction|settings|database|impact|fields|issues|compiled …`
  (run it with no arguments to list analyses; add `--json` for machine-readable output).

---

## 11. Command line (optional)
Everything the dashboard does can also be run from a command prompt in the toolkit folder. The commands use `python`;
on macOS and Linux type `python3`.
```
python nwn_quickscan.py "<module folder or .mod>"          TL;DR in seconds (add --json for machine-readable output)
python nwn_analyse.py "<module>" [--hak x.hak] [--no-json]  full analysis (same results as the dashboard)
python nwn_analyse.py --version                             the toolkit's version
python nwn_compile.py "<module>" --nwn-root "<install>"     compile every script with the official compiler
python nwn_build.py nwn_workspace/<name> plan.json [--out DIR] [--name NAME] [--no-audit]
python nwn_audit.py nwn_workspace/<name> <build folder> <clean module name>
python nwn_logs.py nwn_workspace/<name> --logs "<logs folder>"
python nwn_dashboard.py --export <name>                     static read-only HTML report to share
python nwn_dashboard.py --port 8766                         start the dashboard on another port
python nwn_housekeep.py "<modules folder>"                  list what could be archived (scan only)
python nwn_archive.py archive|unpack nwn_workspace/<name>   zip / unzip an analysis
python nwn_diff.py <older> <newer> [--analysis <name>]      compare two copies; --snapshot <module> saves a snapshot
python nwn_facts.py <name> <question> ...                   read-only facts (see 10)
```
- `--export` writes `nwn_workspace/<name>_report.html`, a single file you can open in any browser.
- `--no-json` skips writing a JSON copy of every GFF file. It is faster and much smaller, and the dashboard does not
  need it.
- Only one analysis can run on a given analysis folder at a time. A second one is refused.
- When a module, hak, analysis folder, build folder, log folder or plan file you name does not exist,
  `nwn_quickscan`, `nwn_analyse`, `nwn_build`, `nwn_audit`, `nwn_logs`, `nwn_housekeep`, `nwn_archive`, `nwn_diff`,
  `nwn_facts` and `nwn_dashboard --export` print one line saying so and stop with exit code 2. `nwn_logs` on a
  folder that exists but holds no log files says so and stops with exit code 0.
- Each script's `--help` lists all its options.

**Self-test.** Run `python tests/run_all.py`. It runs every test suite, prints one table and ends by saying whether
all suites passed. Each suite can also be run on its own (for example `python tests/test_quests.py`) and ends with
"N/M checks passed". If one fails, report it with that output.

---

## 12. Where files live
```
toolkit folder/
  nwn_workspace/
    settings.json          your Settings
    timings.json           speed of past analyses (feeds the time estimates)
    build_timings.json     speed of past builds
    <name>_report.html     a static report written by --export
    <analysis>/
      index.sqlite         everything: files, every GFF field, objects, scripts, dependency graph
      report.json          what the dashboard shows
      catalog.json         hak catalogue
      palette.json         Blueprints page (where each blueprint sits in the palette)
      compile.json         the last Validate all with official compiler
      reports/*.csv        issues, scripts, items, duplicates, safe_to_delete, impact, quest_health, orphans, palette, ...
      conversations/       readable dialogue trees
      json/                every GFF file as JSON (skipped with --no-json)
      palette_moves.json   YOUR waiting palette moves (Blueprints page; applied by the next build)
      edits/               YOUR edits (kept unless you delete them on purpose)
        .history/          what each Find & replace changed (for Undo), and discarded edits
      build/               YOUR clean builds, audits, git history; build/haks: lean haks;
                           build/installed.json: what Add to game folders copied (for Undo)
      descriptions.json    YOUR script descriptions
      accepted_issues.json YOUR "by design" issues
      server_config.json   server settings you loaded (see 5.1)
      database.json        database files you loaded
      analysis_archive.zip, archive.json   when archived
      .incomplete          present while a run has not finished (or was stopped)
    _haks/                 Hak editor projects (staged changes, rebuilt haks, extracted files)
      _backups/            hak backups (older versions of the toolkit, and Restore)
    _housekeeping/         modules-folder scans, hash cache, move batches
    _snapshots/            module snapshots for Compare
    _diffs/                the last comparison
```

---

## 13. Big modules — tips
- **Quick scan first.** Fix missing haks and talk tables, and note the estimated time.
- Expect the first full run to take a long time: tens of thousands of GFF files and gigabytes of haks. Leave the
  dashboard window open, and don't let the computer sleep.
- Use **Stop** freely. It is instant, and **Clear** tidies up afterwards.
- Run one big analysis at a time: each one uses a lot of memory and disk.
- Use **Archive** on analyses you are not using, and **Delete** on ones you no longer need. A big module's index can
  be 1–3 GB.

---

## 14. Troubleshooting
| Symptom | Likely cause and fix |
|---|---|
| "missing or wrong session token" | Open the exact link printed in the console or Terminal window |
| "Port 8765 is already in use. The dashboard is probably already running…" | Find the window of the dashboard that is already running and use its link, or start a second one with `--port 8766` |
| "The dashboard is not running - start it again and open the new link it prints." | The console or Terminal window was closed. Start the dashboard again and use the new link (the old one no longer works) |
| "The toolkit cannot write to its workspace folder…" | The toolkit folder is somewhere you can't write (Program Files, Applications, a protected folder). Move it to a plain folder of your own |
| A note that the workspace is inside a OneDrive folder | Move the toolkit folder out of OneDrive; syncing can lock or damage the analysis databases |
| A hak shows **MISSING** | It is not in your user folder's `hak` folder or the install's `hak` folder. Add it under Optional → haks, or fix the NWN user folder in Settings |
| Analysis "incomplete" in the list | It was stopped or crashed. Press **Clear**, then Analyse again |
| "…is not a checkpoint this toolkit can resume…" (`index.state`) | The analysis folder holds a checkpoint from an older toolkit or another computer. Delete `index.state` in that folder and analyse again |
| Clear says another analysis is still running | A second dashboard window or a command-line run is using that folder. Stop it, wait a minute, then try again |
| Analysis failed with an error | Open the Log in the progress panel. The last lines say why. Please report it so a test can be added |
| Everything is marked *Review* | Usually a missing hak. See the quick scan warnings |
| Time estimate looks wrong | The first estimate is a guess. It learns from each finished run on your computer |
| The compiler "was killed" or "exited with code N without reporting a script error" | It can't run. On macOS and Linux run the `chmod +x` and `xattr` commands in 2.2 |
| Add to hak folder: "The system would not let the toolkit write … into the hak folder" | The hak folder is read-only, or the game, toolset, server, antivirus or a sync folder holds it. Close them, or wait for syncing or scanning to finish, and try again. Nothing was changed |
| Restore: "The system would not let the toolkit replace …" | Close the game, the toolset and nwserver, and check the hak is not read-only or held by antivirus or a sync folder. The hak was not replaced; the backup made first is kept |
| Hak editor says "this hak is busy" | A rebuild, Add to hak folder or Restore of that hak is running. Wait for it to finish |
| Hak editor says the hak changed on disk | Click *Use the file on disk as the new starting point* |
| `git -C … diff` says git is not recognised | Git is not installed. It is optional (see 9) |

---

## 15. Known limits
- **Names built at run time.** Scripts can build a name while the game runs (`ExecuteScript("evt_" + n)`,
  `"Q_" + GetTag(o)`, a blueprint name read from a variable). The toolkit can't follow such a name. It finds the
  string pieces, and then:
  - anything whose name matches a piece is Review, never Safe, and can't be merged away;
  - quest, variable and tag uses through such names are counted, not followed: a quest check that looks impossible
    becomes a warning, not broken, and "read, never set" becomes info;
  - Find & replace lists those scripts as uses it can't find or change;
  - faction changes through such names are listed as "found at run time", not followed.
- **Engine-named assets.** Asset use through 2da row numbers (appearance, portraits, item parts, phenotypes, tiles) is
  not resolved: such assets are kept or marked Review, never deleted automatically (see 7.1).
- **NWNX.** Scripts that only NWNX runs show as "triggered by nothing". Load the server settings so they count as used;
  until then, unused scripts in a module that includes `nwnx_*` are Review.
- Compiled binary models are inventoried and scanned for names, not fully parsed. The Hak editor checks models only in
  text (ASCII) form; compiled models are carried over unchanged, without checks.
- NWN `.dds` and `.plt` textures have no preview.
- Compressed haks (EE's E1.0 format) are read directly (none / zlib / zstd). If an entry ever uses something else, the
  toolkit falls back to `nwn_erf` from the `tools` folder. Rebuilt and lean haks are written uncompressed.
- **Symbolic links** in a module or hak folder are skipped, never followed, and reported (`symlink_skipped`): a link
  could point outside the folder. Copy the real files in if they belong to the module.
- **Checkpoints.** `index.state` (left by a time-boxed command-line run) is only resumed when this toolkit wrote it.
  One from an older toolkit or another computer is refused: delete it and run again.
- Compiled-script checks compare text constants only: a stale `.ncs` that differs in numbers or logic alone is not
  detected. Scripts with an unreadable include are not judged.
- 2da row usage is tracked for the common tables (appearance, placeables, baseitems, portraits, soundsets, classes,
  races, feats, spells, skills, doors, loadscreens, music, phenotype…). For other tables the editor says usage is
  unknown, so treat every row as possibly used.
- Quests from scripts: names given as conversation script parameters are followed per node; a parameter script used
  from something other than a conversation (an event, ExecuteScript) has no parameters to read and is skipped. A value
  set on an object the toolkit can't trace back to the player is not counted as the player's. A "broken" quest is
  strong evidence, not proof: read the stages before changing anything.
- Item status in quests needs the module's haks (and the game folder) loaded in the analysis; without them, items from
  haks and the base game show as missing.
- The identifier-load pitfall check is an approximate count.
- Performance thresholds (60 creatures, 10 custom heartbeats, 50 container items, 300 store items) are rules of thumb,
  not engine limits.
- Factions: reputation between two single creatures (personal reputation) is not shown.
- Find & replace searches object and conversation files you edited as they were analysed; re-analyse after big edits.
- Housekeeping moves only `.mod` and `.BackupMod` files at the top of the folder; unpacked module folders and archives
  (`.rar`) are left alone.

---

## 16. Glossary
| Term | Meaning |
|---|---|
| **Module** | The adventure: `module.ifo` plus areas, blueprints, scripts, conversations… (a folder or a `.mod`) |
| **Hak** | An add-on content pack (models, textures, 2das, scripts) listed in `module.ifo`. Players must download it |
| **Load order** | Which copy of a file the game uses: haks in `module.ifo` order (first listed wins) › module › override folder › base game |
| **Override folder** | `Documents/Neverwinter Nights/override`. Used for files the module and its haks don't have, on that computer only |
| **2da** | A table file (appearance, baseitems, placeables…) that other content refers to by row number |
| **Talk table (tlk)** | Numbered strings. `dialog.tlk` is the game's; a module can add a custom one |
| **GFF** | BioWare's structured file format (areas, blueprints, conversations, `module.ifo`…) |
| **Resref** | A resource's file name without extension, at most 16 characters |
| **Blueprint** | A template in the palette (item, creature, placeable…). **Instance**: a placed copy in an area |
| **Impact level** | How much breaks if something changes or goes: Critical / High / Medium / Low / None |
| **Keeper** | The copy of a duplicate group that stays when the group is merged |
| **Safe / Safe as group / Review** | Safe to delete: nothing uses it / only other unused things use it (they go together) / probably unused but something could reach it at run time - check first |
| **Clean build** | A new copy of the module (folder and `.mod`) with your approved deletions, merges and edits, audited against the original |
| **Lean haks** | Copies of the haks with unused or duplicate files left out, under new names, written by a clean build |
| **Run time** | While the game or server is running, as opposed to what is written in the module's files |
| **Cascade** | A change that flows on: an include used by 40 scripts, an event script, a conversation's scripts |
| **Legacy** | Content only unused scripts touch - left over from something retired |
| **Package** | A community system the module includes (DMFI, Jasperre's AI, CEP…): its variables are its own settings, not quests |
| **By design** | An issue you ticked as intended; hidden and left out of the counts until what it says changes |
| **Faction / reputation** | The group an NPC or object belongs to, and how much (0-100) one group likes another: 0-10 hostile, 90-100 friendly |
| **Faction holder** | A hidden placed creature whose only job is to be a member of a faction, so scripts can make others join it by tag |
