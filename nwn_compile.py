"""
nwn_compile.py - validate scripts with the OFFICIAL NWN:EE script compiler
(nwn_script_comp from neverwinter.nim). Nothing is written into the module:
the compiler runs in simulate mode (-s).

Why this exists
---------------
The toolkit's own checks (nwn_index.validate) find structural problems, but only the real compiler can say whether
a script compiles against the game's nwscript.nss and includes. This module runs that compiler and turns its log
into a per-script list of errors.

Finding the compiler: --compiler (or the Settings path) when given - then only that file; otherwise env
NWN_SCRIPT_COMP, ./tools/, PATH. The file must be executable (nwn_script_comp.exe on Windows).
Finding the game (for nwscript.nss and base-game includes): --nwn-root, env NWN_ROOT,
or the compiler's own auto-detection of your Steam/Beamdog install.

    python nwn_compile.py <module folder or .mod> [--nwn-root PATH] [--compiler PATH]

Entry points
------------
    find_compiler(explicit)            -> path of nwn_script_comp, or None
    compile_module(module_path, ...)   -> check every script of a module (simulate: no .ncs written anywhere)
    compile_files(folder, names, out_dir, ...)  -> really compile some scripts, writing .ncs into out_dir only
                                          (used by the build for edited / new scripts)

Reads and writes
----------------
compile_module reads the module's .nss files: they are copied into a new temporary folder (a .mod is first unpacked
into another temporary folder), the compiler is pointed at those copies, and both folders are deleted afterwards.
It writes nothing else. compile_files compiles the scripts in the folder it is given (the build's own folder) and
writes .ncs files only into its out_dir. The compiler is started directly with an argument list (no shell), so
file names can't be read as shell commands.

Limits
------
Results come from parsing the compiler's text log: an error line whose format the patterns below don't recognise
is not attributed to a script (the log is returned so it can be read).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

import nwnlib as n

HERE = os.path.dirname(os.path.abspath(__file__))
# One error line of the compiler log: "<anything>] <path>.nss: <message>", with an optional timing suffix such
# as " [12.3ms]" or " [<1ms]" that is not part of the message. Only lines that start with "E " (error level) are
# used. Shape: "E [...] foo.nss: <message> [0.4ms]" -> path "foo.nss", msg "<message>". The path may start with a
# Windows drive letter ("C:\...") but otherwise contains no ":" - the first ":" after it ends the path.
LINE_RE = re.compile(r"\]\s+(?P<path>(?:[A-Za-z]:)?[^:]+?\.nss):\s*(?P<msg>.*?)(?:\s+\[[<\d.]+ms\])?$")


def _candidates(explicit):
    """Where the compiler may be, in search order (see find_compiler). An explicit path is the only candidate:
    the caller asked for that program, so another one found elsewhere must not be used in its place."""
    if explicit:
        return [explicit]
    exe = "nwn_script_comp.exe" if os.name == "nt" else "nwn_script_comp"
    return [os.environ.get("NWN_SCRIPT_COMP"), os.path.join(HERE, "tools", exe), shutil.which("nwn_script_comp")]


def find_compiler(explicit=None):
    """Path of nwn_script_comp, or None. explicit: a path from the settings or the command line - when given, only
    that file is accepted (None if it is missing). Otherwise tried in order: env NWN_SCRIPT_COMP, the toolkit's
    tools/ folder, PATH. The file must exist and be executable; it is never run here."""
    for c in _candidates(explicit):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _run_hint(comp):
    """How to let a compiler file run: the execute bit can be lost when a zip is unpacked, and macOS blocks a
    downloaded program (the com.apple.quarantine attribute) until it is cleared."""
    return f'chmod +x "{comp}" (on macOS also: xattr -d com.apple.quarantine "{comp}")'


def not_found_reason(explicit=None):
    """One plain sentence on why find_compiler(explicit) found nothing: the file named is missing, a file was found
    but cannot be run (no execute permission), or none was found at all."""
    for c in _candidates(explicit):
        if c and os.path.isfile(c):
            return f"{c} is not executable - run: {_run_hint(c)}"
    if explicit:
        return f"the compiler set in Settings does not exist: {explicit}"
    return ("Official compiler (nwn_script_comp) not found. Download neverwinter.nim from GitHub releases and put "
            "nwn_script_comp (nwn_script_comp.exe on Windows) in the toolkit's tools/ folder.")


def _died_reason(comp, returncode, log):
    """The reason text for a compiler run that ended with a non-zero exit code and no error line the parser
    recognised: a crash, a missing library, or (macOS) a program blocked because it was downloaded."""
    last = (log.strip().splitlines() or [""])[-1][:300]
    how = f"was killed by signal {-returncode}" if returncode < 0 else f"exited with code {returncode}"
    return (f"the compiler {how} without reporting a script error" + (f" ({last})" if last else ", printing nothing")
            + f". If it never runs, allow it to: {_run_hint(comp)}")


def compile_module(module_path, compiler=None, nwn_root=None, extra_dirs=(), timeout=1800, nwn_user=None):
    """Returns dict(status, compiler, results{script: [errors]}, ok, failed, log).

    module_path: module folder or .mod. compiler / nwn_root / nwn_user: as for find_compiler and the module
    docstring (env NWN_ROOT, NWN_HOME / NWN_USER_DIRECTORY when not given). extra_dirs: more include folders
    (e.g. extracted haks). timeout: seconds for the whole compiler run.
    status: "ran", "skipped" (no compiler; see reason) or "error" (the module could not be opened, the compiler
    could not run, or it exited with a failure code without naming a script error; see reason). results maps every
    script name (lower case) to its error messages, [] when it compiled. ok / failed: counts. log: the last
    20,000 characters of the compiler output. Never writes into the module and never writes .ncs files (-s).
    In a folder module, links and hidden folders are skipped (nwnlib.walk_folder), as the index skips them."""
    comp = find_compiler(compiler)
    if not comp:
        return dict(status="skipped", reason=not_found_reason(compiler), results={}, ok=0, failed=0, log="")
    tmp = work = None
    src = module_path
    # one try/finally around everything that uses the temporary folders: they are removed whatever fails
    # (a module that can't be opened, a copy error, a timeout or a crash of the compiler)
    try:
        if not os.path.isdir(module_path):  # .mod/.erf: extract scripts to a temp folder
            tmp = tempfile.mkdtemp(prefix="nwn_comp_")
            try:
                erf = n.Erf(module_path)
            except (OSError, ValueError) as ex:
                return dict(status="error", reason=f"module could not be opened: {ex}", results={}, ok=0, failed=0, log="")
            try:
                for e in erf.entries:
                    if e.ext == "nss":
                        # e.filename is safe as a file name: nwnlib.Erf renames entries with path characters or
                        # reserved device names, so nothing can be written outside tmp
                        with open(os.path.join(tmp, e.filename), "wb") as fh:
                            fh.write(erf.read(e))
            finally:
                erf.close()
            src = tmp
        # the compiler gets its own copy of the scripts, so even a misbehaving compiler can't touch the module
        # folder. File names are lower-cased in the copy (the toolkit uses lower-case resrefs throughout).
        # walk_folder skips links and hidden folders, as the index does: a linked file is not part of the module.
        work = tempfile.mkdtemp(prefix="nwn_comp_src_")
        names = []
        for full, _rel in n.walk_folder(src):
            fn = os.path.basename(full)
            # nwscript.nss is the engine's list of functions and constants, not a script: it is not compiled
            # as one, and the compiler takes it from the game install
            if fn.lower().endswith(".nss") and fn.lower() != "nwscript.nss":
                shutil.copy2(full, os.path.join(work, fn.lower()))
                names.append(fn.lower()[:-4])
        # -c compile, -y keep going after a script fails (so every error is reported), -s simulate: write no .ncs.
        # Argument list, no shell: paths with spaces or odd characters are passed as they are.
        cmd = [comp, "-c", "-y", "-s", "--quiet"]
        root = nwn_root or os.environ.get("NWN_ROOT")
        if root:
            cmd += ["--root", root]
        user = nwn_user or os.environ.get("NWN_HOME") or os.environ.get("NWN_USER_DIRECTORY")
        if user:
            cmd += ["--userdirectory", user]
        # --dirs: the folders searched for #include files (the copies and extra_dirs); the last argument is the
        # folder of scripts to compile
        cmd += ["--dirs", ",".join([work] + list(extra_dirs)), work]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            log = (p.stdout or "") + (p.stderr or "")
        except PermissionError as ex:
            return dict(status="error", reason=f"{ex} - allow the compiler to run: {_run_hint(comp)}", results={},
                        ok=0, failed=0, log="")
        except Exception as ex:  # noqa
            return dict(status="error", reason=str(ex), results={}, ok=0, failed=0, log="")
    finally:
        for d in (work, tmp):
            if d:
                shutil.rmtree(d, ignore_errors=True)
    results = {nm: [] for nm in names}     # every script starts as "compiled OK"; error lines add messages
    fatal = None
    for line in log.splitlines():
        m = LINE_RE.search(line)
        if m and line.startswith("E "):
            script = os.path.basename(m.group("path"))[:-4].lower()
            msg = m.group("msg").strip() or "could not be compiled (invalid name?)"
            results.setdefault(script, []).append(msg)
        # these mean the compiler itself stopped (typically: game install not found), not that a script failed
        if "unhandled exception" in line or "not found [AssertionDefect]" in line:
            fatal = line.strip()
    if fatal:
        return dict(status="error", reason="Compiler could not start - usually the NWN install was not found. "
                    "Pass --nwn-root (the folder containing data/ and lang/). Detail: " + fatal,
                    results={}, ok=0, failed=0, log=log)
    failed = sum(1 for v in results.values() if v)
    # The compiler exits 1 when a script failed, and also when it could not run at all. Without an error line to
    # explain a non-zero exit, "every script compiled" would be a false all-clear.
    if p.returncode != 0 and not failed:
        return dict(status="error", reason=_died_reason(comp, p.returncode, log), results={}, ok=0, failed=0,
                    log=log[-20000:])
    return dict(status="ran", compiler=comp, results=results, ok=len(results) - failed, failed=failed, log=log[-20000:])


def compile_files(folder, names, out_dir, compiler=None, nwn_root=None, nwn_user=None, extra_dirs=(), timeout=600):
    """Really compile the named scripts (found in folder, includes resolved from folder + extra_dirs),
    writing .ncs files into out_dir. Returns dict(status, results{name: [errors]}, written[list], missing[list]).

    names: script names without .nss, matched to the files in folder without regard to case (a folder module may
    hold "MyScript.nss" on a case-sensitive file system; resrefs are lower case). missing: names with no file in
    folder - not compiled. written: names whose out_dir/<name>.ncs this run wrote (new, or changed since before the
    run) and that reported no error - a .ncs left there by an earlier run never counts. status: "ran", "skipped"
    (no compiler) or "error" (see reason). Writes only into out_dir (the compiler's -d; a .ncs the compiler named
    in another case is renamed to <name>.ncs there); folder is read, never changed. Scripts are passed in batches
    so the command line stays under the Windows length limit."""
    comp = find_compiler(compiler)
    if not comp:
        return dict(status="skipped", reason=not_found_reason(compiler), results={}, written=[], missing=list(names))
    on_disk = {f.lower(): f for f in os.listdir(folder) if f.lower().endswith(".nss")}
    files = [os.path.join(folder, on_disk[f"{nm.lower()}.nss"]) for nm in names if f"{nm.lower()}.nss" in on_disk]
    missing = [nm for nm in names if f"{nm.lower()}.nss" not in on_disk]
    if not files:
        return dict(status="ran", results={}, written=[], missing=missing)
    cmd = [comp, "-c", "-y", "--quiet", "-d", out_dir]      # no -s: real compile, .ncs go to out_dir (-d)
    root = nwn_root or os.environ.get("NWN_ROOT")
    if root:
        cmd += ["--root", root]
    user = nwn_user or os.environ.get("NWN_HOME") or os.environ.get("NWN_USER_DIRECTORY")
    if user:
        cmd += ["--userdirectory", user]
    cmd += ["--dirs", ",".join([folder] + list(extra_dirs))]
    # batches: Windows refuses command lines over ~32,000 characters (WinError 206)
    batches, cur, size = [], [], sum(len(c) + 3 for c in cmd)
    for f in files:
        if cur and size + len(f) + 3 > 24000:
            batches.append(cur); cur, size = [], sum(len(c) + 3 for c in cmd)
        cur.append(f); size += len(f) + 3
    batches.append(cur)

    def ncs_files():
        """out_dir's .ncs files by lower-case name -> (actual file name, (mtime_ns, size))."""
        out = {}
        for f in os.listdir(out_dir):
            if f.lower().endswith(".ncs"):
                st_ = os.stat(os.path.join(out_dir, f))
                out[f.lower()[:-4]] = (f, (st_.st_mtime_ns, st_.st_size))
        return out
    # what out_dir held before the run: a .ncs that is still the same file afterwards was not written by this run
    before = {k: v[1] for k, v in ncs_files().items()}
    log, codes = "", []
    try:
        for b in batches:
            p = subprocess.run(cmd + b, capture_output=True, text=True, timeout=timeout)
            log += (p.stdout or "") + (p.stderr or "")
            codes.append(p.returncode)
    except PermissionError as ex:
        return dict(status="error", reason=f"{ex} - allow the compiler to run: {_run_hint(comp)}", results={},
                    written=[], missing=missing)
    except Exception as ex:  # noqa
        return dict(status="error", reason=str(ex), results={}, written=[], missing=missing)
    results = {nm.lower(): [] for nm in names if nm not in missing}
    for line in log.splitlines():
        m = LINE_RE.search(line)
        if m and line.startswith("E "):
            results.setdefault(os.path.basename(m.group("path"))[:-4].lower(), []).append(m.group("msg").strip())
    if "unhandled exception" in log:
        return dict(status="error", reason=log.strip().splitlines()[-1][:300], results={}, written=[], missing=missing)
    bad_exit = next((c for c in codes if c != 0), 0)
    if bad_exit and not any(results.values()):
        # same rule as compile_module: a failed run with no error line is not "everything compiled"
        return dict(status="error", reason=_died_reason(comp, bad_exit, log), results={}, written=[],
                    missing=missing, log=log[-5000:])
    written = []
    after = ncs_files()
    for nm in results:
        if results[nm] or nm not in after:
            continue
        actual, stamp = after[nm]
        if stamp != before.get(nm):
            if actual != f"{nm}.ncs":
                os.replace(os.path.join(out_dir, actual), os.path.join(out_dir, f"{nm}.ncs"))
            written.append(nm)
    return dict(status="ran", results=results, written=written, missing=missing, log=log[-5000:])


def main(argv=None):
    """Command line: check every script of a module. Exit code 0 = all compiled, 1 = some failed, 2 = could not
    run (no compiler, or the compiler could not start)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module", help="module folder or .mod (only read)")
    ap.add_argument("--compiler", help="path of nwn_script_comp (default: env NWN_SCRIPT_COMP, the tools/ folder, PATH)")
    ap.add_argument("--nwn-root", help="NWN install folder (contains data/ and lang/)")
    ap.add_argument("--nwn-user", help="NWN user folder (Documents/Neverwinter Nights)")
    ap.add_argument("--dirs", action="append", default=[], help="extra include folders (e.g. extracted haks)")
    a = ap.parse_args(argv)
    r = compile_module(a.module, a.compiler, a.nwn_root, a.dirs, nwn_user=a.nwn_user)
    if r["status"] != "ran":
        print(r["status"].upper() + ": " + r.get("reason", ""))
        return 2
    for s, errs in sorted(r["results"].items()):
        for e in errs:
            print(f"FAIL {s}: {e}")
    print(f"{r['ok']} compiled OK, {r['failed']} failed")
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
