# Contributing to the NWN Module Toolkit

Thank you for helping. The toolkit is used on modules people have spent years building, so every change follows a
few rules.

## The rules
1. **Never change an original.** The toolkit only reads the user's module, haks and game install. Everything it
   writes goes into `nwn_workspace/`, and everything a build makes gets a new name. The only writes outside the
   workspace are buttons the user presses: Add to game folders (adds the build's new files, never replaces one; Undo
   removes only those, if unchanged), Add to hak folder (adds a new file, never replaces one), Modules folder Move
   (moves, never deletes; Undo), Restore (backs up first) and Extract (only into an empty folder). Do not add another
   without the same care.
2. **When unsure whether something is used, keep it.** A check that can't prove something unused marks it Review,
   never Safe. A false "Safe" can break a module; a false "Review" only costs the user a look.
3. **Comment the code for a reviewer.** Someone who knows Python but not NWN must be able to follow each file:
   - a module docstring: why the file exists, what it reads and writes, and its limits;
   - a docstring on every public function: its arguments, what it returns, and what it never does ("never raises",
     "never writes the module");
   - comments on the *why* of lines that aren't obvious: file-format offsets (cite the source, for example
     neverwinter.nim), safety choices (temporary copies, no shell, read-only), and NWN behaviour the code relies on.

   Don't comment what the code already says. `nwn_ncs.py` is a good example.

## Code
- **Python 3.8 or newer, standard library only.** No packages to install. Don't use features newer than 3.8
  (for example `str.removeprefix`) unless you check the version first.
- Start external programs with an argument list, never through a shell.
- Open analysis indexes read-only when you only read them (`nwnlib.sqlite_ro`).
- User-facing messages are plain sentences that say what to do next. Write paths with forward slashes.

## Tests
Run `python tests/run_all.py` (`python3` on macOS and Linux) before and after a change: it runs every suite, and all
must pass. Each suite also runs on its own and ends with "N/M checks passed". When you fix a bug, add a check that
fails without the fix.

## Documentation
The user manual is `docs/MANUAL.md`, shown in the dashboard's Help page. Update it in the same change when a page,
button or workflow changes, and use the button names exactly as the page shows them. Plain English, short sentences,
British spelling.

## Licence
MIT - see `LICENSE`. Your contributions are released under the same licence. The neverwinter.nim tools are separate
programs under their own MIT licence and are not included.

## Release markers
Releases are generated from the source repository by a release script that is not part of the release. It removes
text that only some editions carry, using markers in the source. Keep any markers you find intact and balanced (every
opening marker needs its closing marker), or the release script refuses to run.
