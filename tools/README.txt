This folder is for the official NWN:EE script compiler and disassembler. They are not part of this toolkit: they
come from neverwinter.nim (https://github.com/niv/neverwinter.nim), Copyright 2018 Bernhard Stoeckner and
contributors, released under the MIT licence (see LICENCE in that project). Without them the toolkit still works:
scripts are checked structurally but not compiled, the Compiled code panel has no listing, and a build that edits an
include is refused.

To install them:
  1. Go to https://github.com/niv/neverwinter.nim/releases (latest release).
  2. Under "Assets", download the zip for your system:
       Windows                          neverwinter-x86_64-windows.zip
       Mac with Apple silicon (M1+)     neverwinter-aarch64-macos.zip
       Intel Mac                        neverwinter-x86_64-macos.zip
       Linux                            neverwinter-x86_64-linux-gnu.zip
  3. Copy these programs into THIS folder:
       nwn_script_comp   the script compiler   (nwn_script_comp.exe on Windows, and any .dll next to it)
       nwn_asm           the disassembler      (nwn_asm.exe on Windows) - optional
       nwn_erf           archive tool          (nwn_erf.exe on Windows) - optional, only a fallback
  4. macOS and Linux: let them run. In Terminal, in this folder:
       chmod +x nwn_script_comp nwn_asm nwn_erf
     and on macOS also clear the "downloaded from the internet" block:
       xattr -d com.apple.quarantine nwn_script_comp nwn_asm nwn_erf

The dashboard finds the compiler here by itself (the Modules page says "found").
The compiler also needs the game: set "NWN install folder" in the dashboard's Settings to the folder that contains
data/ and lang/, for example
  Windows (Steam): C:/Program Files (x86)/Steam/steamapps/common/Neverwinter Nights
  macOS (Steam):   ~/Library/Application Support/Steam/steamapps/common/Neverwinter Nights
  Linux (Steam):   ~/.local/share/Steam/steamapps/common/Neverwinter Nights
