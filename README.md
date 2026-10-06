# NWN Module Toolkit 1.5.2

Analyse, understand and clean up Neverwinter Nights: Enhanced Edition modules - safely.

- **Understand a module**: every file, script, conversation, quest, faction, variable, campaign database and hak, with
  what uses what and how much would break if it changed (impact levels).
- **Find problems**: missing scripts and resources, broken quests, typos in variable names, faction mistakes,
  performance risks for persistent worlds - each with a plain *How to fix* line.
- **Clean up**: remove what nothing uses, merge duplicates, slim haks to what the module reaches, then build a NEW clean
  module and audit it against the original.
- **Play-test**: a log monitor that explains each game/server error and links it to the script behind it.

Runs on Windows, macOS and Linux.

## Why I made this
By day I work in cyber security risk. In my own time I tinker with Neverwinter Nights, and I'm part of the community behind Mystara: Alternate, a large persistent world where I look after the server, keeping it up and keeping it secure.

NWN time is hard to find around work, family and sport, so I don't build this in big sessions. I fit it in here and there. But I'm still very much invested in the game and the people who play it.

Running a server that has grown for years, I kept hitting the same problem. The module holds thousands of scripts, duplicate items, and haks that players have to download and that are bigger than they need to be. Nobody can say for sure what's still in use. The toolset can't answer "what uses this?" across a whole module and its haks, and deleting the wrong thing can break a live server.

So I built the toolkit the way I'd run a security assessment:

- Read everything, never touch the original.
- Back every finding with evidence: the file, field or script line it came from.
- Treat every clean-up as a controlled change: plan it, build a new copy, and audit that copy against the original before anyone uses it.

I leveraged AI to help me rapidly build and develop the toolkit - it's how a project like this fits into the time I have.

**Your originals are only read, except by five buttons you press yourself.** Everything else the toolkit writes goes
into its own `nwn_workspace` folder. The five exceptions: **Add to game folders** (Build & audit, after typing ADD)
copies the finished build's `.mod` and rebuilt haks into your modules and hak folders under names that don't exist
yet, never replacing a file, and **Undo** removes only those files if unchanged; **Add to hak folder** (Hak editor)
adds a rebuilt hak under a new name next to the original and never replaces a file; **Move** (Modules folder page)
moves the module copies you ticked into an archive folder, never deletes, and **Undo** moves them back; **Restore**
(Hak editor, only for backups made by older versions) backs up the current hak before replacing it; **Extract** (Hak
editor) writes only into an empty or new folder you choose. Rebuilt modules and haks always get new names, so copying
them into your game folders can never replace an original. **Install / Update** writes only the install folder you
choose.

## Install
Step-by-step instructions, with download links, are in **`INSTALL.md`** (about ten minutes). In short:
1. Install Python 3.8 or newer from python.org (Windows: tick *Add python.exe to PATH*).
2. Unzip this folder anywhere and start it: Windows `run_dashboard.bat`, macOS `run_dashboard.command`, Linux
   `python3 nwn_dashboard.py`.
3. On the Modules page press **Install** (a folder of its own is suggested; an older copy's work can come along).
   Later versions find the install and **Update** it: only the program is swapped, your work stays.
4. Optional but recommended: the official script compiler (INSTALL.md step 5, or `tools/README.txt`).

No other installs are needed - the toolkit uses only Python's standard library.

## First steps
1. **Modules** page: pick your module (folder or `.mod`), press **Quick scan** (seconds), then **Analyse**.
2. Read **Overview** and **Issues** (every issue has a *How to fix* line).
3. Tick what to remove on **Safe to delete** / **Duplicates**, then **Build & audit**.
4. Start the **Log monitor** while you play-test the clean module.

The full manual is in the dashboard (**Help**) and in `docs/MANUAL.md`.

## Check it works on your computer
`python tests/run_all.py` (macOS / Linux: `python3 tests/run_all.py`) runs every test suite and prints one table; its
last line must say that all suites passed. Each suite can also be run on its own (for example
`python tests/test_quests.py`) and ends with "N/M checks passed".

## Credits
Created by **Sporaxis** - contact: sporaxis@gmail.com.
The code was developed within Claude, Anthropic's AI assistant. The toolkit itself uses no AI: it runs entirely on
your computer and makes no internet connections.

## Licence
MIT - see `LICENSE`. The neverwinter.nim programs described in `tools/README.txt` are separate (MIT licence) and not
included. Neverwinter Nights is a trademark of its owners; this toolkit contains no game content.
