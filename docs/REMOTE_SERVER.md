# Running the toolkit against a module on a server (Vultr / any Linux host)

The toolkit is pure Python 3.8+ with no packages to install, so the simplest and safest setup is to run it
**on the server**, next to the module `nwserver` loads, and reach the dashboard through an SSH tunnel.
Nothing is exposed to the internet: the dashboard only ever listens on `127.0.0.1`.

## One-time setup on the server
```bash
ssh user@your-server
sudo apt-get install -y python3 git unzip          # Debian/Ubuntu (git is optional: it gives builds a version history)
mkdir -p ~/nwn && cd ~/nwn && unzip ~/nwn-toolkit.zip   # copy the zip up with scp first
# optional: official compiler for script validation
#   download the Linux build from github.com/niv/neverwinter.nim/releases,
#   unzip nwn_script_comp (+ libnwnscriptcomp.so) into ~/nwn/nwn-toolkit/tools/ and chmod +x it
python3 ~/nwn/nwn-toolkit/tests/run_all.py              # runs every test suite: all must pass
```

## Where things are on a typical server
| What | Native nwserver | Docker (nwnxee/unified, beamdog nwserver) |
|---|---|---|
| module | `~/.local/share/Neverwinter Nights/modules/<name>.mod` | the folder you mount as `/nwn/home/modules` |
| haks | `~/.local/share/Neverwinter Nights/hak/` | mounted `/nwn/home/hak` |
| game files (for the compiler) | the install folder containing `data/` and `lang/` | inside the image; use `--nwn-root /nwn/data` or copy `nwscript.nss` |
| logs | `~/.local/share/Neverwinter Nights/logs/nwserverLog1.txt` (`logs.0` on older versions) | `docker logs -f <container> >> /var/log/nwserver.txt` |

## Analyse and browse
```bash
cd ~/nwn/nwn-toolkit
python3 nwn_analyse.py "/path/to/modules/mymodule.mod" --hak "/path/to/hak/mymodule_core.hak" \
        --nwn-root "/path/to/nwn" --nwn-user "$HOME/.local/share/Neverwinter Nights"
python3 nwn_dashboard.py --no-browser --port 8765
```
Then from your own computer:
```bash
ssh -N -L 8765:127.0.0.1:8765 user@your-server
```
and open the link the dashboard printed (it includes the session token) in your own browser.
To keep it running: `nohup python3 nwn_dashboard.py --no-browser > dashboard.log 2>&1 &` (or a systemd unit).

## Live log monitoring while players are on
Point the Log monitor at the server's log folder (or the file you pipe `docker logs` into). It reads only new
lines, groups repeated errors, and links each to the script, the object's location and its impact.

## Putting a clean build live
Builds never touch the module the server is running. The clean `.mod` is written under
`nwn_workspace/<name>/build/`; copy it into the modules folder under a new name and switch on the next restart.
Keep the original until the play-test is clean. If Git is installed, each build is a git commit, so
`git -C <clean folder> diff original HEAD --stat` shows exactly what changed.

