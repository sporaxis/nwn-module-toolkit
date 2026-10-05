# Installing the NWN Module Toolkit

About ten minutes. You need: Neverwinter Nights: Enhanced Edition already installed, and this folder.
Nothing here changes your game, your modules or your haks.

## Step 1 - Install Python (once)

The toolkit is a set of Python programs. Python is free.

**Windows**
1. Go to <https://www.python.org/downloads/> and press the big yellow **Download Python** button.
2. Run the installer. On the first screen **tick "Add python.exe to PATH"**, then press **Install Now**.

**macOS**
1. Go to <https://www.python.org/downloads/> and download the macOS installer.
2. Open it and follow the steps. (Use this installer even if your Mac already has a `python3` command - that one is
   often only a placeholder.)

**Linux**: Python 3 is almost always installed already. Check with `python3 --version` (3.8 or newer).

## Step 2 - Unzip it

Unzip the download anywhere - your Downloads folder is fine. This copy is only used to install from.

## Step 3 - Start it and press Install

- **Windows**: double-click `run_dashboard.bat`.
- **macOS**: double-click `run_dashboard.command`. The first time, macOS may refuse to open it: right-click it, choose
  **Open**, then **Open** again. If macOS asks whether Terminal may access your Documents folder, choose **Allow**
  (your modules live there).
- **Linux**: in a terminal, in this folder: `python3 nwn_dashboard.py`.

A small console window opens and your web browser shows the dashboard. At the top of the **Modules** page:
**Install the toolkit**. The suggested folder (`NWN Module Toolkit` in your user folder) is a good choice - it is
outside OneDrive / iCloud, whose syncing can damage the toolkit's databases. Press **Install**, check the list, press
**Install** again. If it finds an older copy of the toolkit with your work in it, it offers **Bring my work from …**
(ticked): your settings, analyses and compiler come along, and the old copy is not changed.

The toolkit then restarts from its new folder in a new window and browser tab (Linux: start it with
`start-nwn-toolkit.sh` in the install folder). The unzipped download isn't needed any more.

From now on, start the toolkit with **Start NWN Toolkit** in the install folder (on macOS the first time: right-click,
**Open**). Keep its console window open while you work - closing it stops the toolkit.

## Step 4 - Tell it where the game is

If you brought your work across from an older copy, this is already done - skip to step 5.

In the dashboard, on the **Modules** page, open **Settings**:
1. **NWN install folder** - press **Detect**. It finds the usual Steam and Beamdog installs. If it finds nothing, type
   the folder that contains the `data` and `lang` folders, for example
   - Windows (Steam): `C:\Program Files (x86)\Steam\steamapps\common\Neverwinter Nights`
   - Windows (Beamdog): `C:\Users\<you>\Beamdog Library\00785`
   - macOS (Steam): `~/Library/Application Support/Steam/steamapps/common/Neverwinter Nights`
2. **NWN user folder** - your `Documents/Neverwinter Nights` folder (it holds `modules`, `hak`, `tlk`, `override`).
3. Press **Save settings**. If a folder is wrong, a warning appears right under "Saved."

That's it - pick your module on the **Modules** page and press **Quick scan**, then **Analyse**.

## Step 5 (optional, recommended) - the official script compiler

With the compiler the toolkit can compile and check every script, and show what a compiled script really does.
Without it everything else still works.

1. Go to <https://github.com/niv/neverwinter.nim/releases> and open the latest release.
2. Under **Assets**, download the zip for your computer:
   - Windows: `neverwinter-x86_64-windows.zip`
   - Mac with Apple silicon (M1 and later): `neverwinter-aarch64-macos.zip`; older Intel Mac: `neverwinter-x86_64-macos.zip`
   - Linux: `neverwinter-x86_64-linux-gnu.zip`
3. Open the zip and copy `nwn_script_comp` (on Windows `nwn_script_comp.exe` **and the .dll files next to it**) into
   the `tools` folder of the installed toolkit: `NWN Module Toolkit\toolkit\tools` in your user folder (macOS:
   `~/NWN Module Toolkit/toolkit/tools`; or the
   `tools` folder of an unzipped copy you run without installing). Updates keep it there. `nwn_asm` and `nwn_erf` are useful too - copy them as well.
4. macOS and Linux only: open Terminal in the `tools` folder and run
   `chmod +x nwn_script_comp nwn_asm nwn_erf`, and on macOS also
   `xattr -d com.apple.quarantine nwn_script_comp nwn_asm nwn_erf`.

The Modules page then says the compiler was **found**. More detail: `tools/README.txt`.

## Step 6 (optional) - add the tools you already use

The toolkit needs no other program, and it names none. If you use a hak or 2da editor, an image viewer or a text
editor, add it under **Settings → External tools → + Add a tool**: a name, the program (on Windows the `.exe`, on
macOS the `.app`) and the file types it opens. The dashboard then shows **Open in …** buttons for those files. It only
ever starts programs you added, with the file as the argument.

## Updating to a new version

1. Close the toolkit's console window.
2. Unzip the new version anywhere (Downloads is fine) and start it (step 3).
3. The Modules page says **Update your installed toolkit** and names your install folder. Press **Update**, check the
   list, press **Update** again.

Only the program is swapped: your settings, analyses, edits, builds and compiler stay as they are, and the program it
replaced is kept in the install folder's `previous` folder so you can go back. The toolkit restarts from the install
folder; the unzipped download isn't needed any more.

Coming from version 1.4.0 or older (an unzipped copy, no install yet)? Same steps: the new version finds the old
folder and offers **Bring my work from …** while it installs. The old folder is not changed - delete it yourself
once you're happy.

After updating, open each module once. If the new version has checks the analysis doesn't, the Overview says so: press
**Analyse again** (your edits, accepted issues and waiting changes stay with it).

## Something not working?

| What you see | What to do |
|---|---|
| "python is not recognized" / nothing happens on double-click | Python isn't installed or "Add python.exe to PATH" wasn't ticked: run the Python installer again, choose **Modify**, and tick it. |
| "Port 8765 is already in use" | The dashboard is already running - find its console window. Or start a second one with `python nwn_dashboard.py --port 8766`. |
| The page says "The dashboard is not running" | The console window was closed: start it again (step 3) and use the new link it prints. |
| macOS: "cannot be opened because it is from an unidentified developer" | Right-click the file, **Open**, **Open** (step 3). For the compiler: the `xattr` command in step 5. |
| Settings shows a warning under "Saved." | The folder named there doesn't look like the game or user folder - press **Detect** or check the path. |

To check the toolkit itself on your computer: `python tests/run_all.py` (macOS/Linux: `python3 tests/run_all.py`).
The last line must say all suites passed.

## Useful links

| What | Where |
|---|---|
| Python | <https://www.python.org/downloads/> |
| Script compiler, disassembler, archive tool (neverwinter.nim) | <https://github.com/niv/neverwinter.nim/releases> |
| Git (optional - gives each clean build a version history) | <https://git-scm.com/downloads> |
| How the game picks a file when several haks have it | <https://nwn.wiki/display/NWN1/Content+Load+Order> |
| NWNX:EE (server plugins some persistent worlds use) | <https://nwnxee.github.io/unified/> |
| Neverwinter Vault (community modules, haks and tools) | <https://neverwintervault.org/> |

The full manual is in the dashboard (**Help / manual**) and in `docs/MANUAL.md`.
