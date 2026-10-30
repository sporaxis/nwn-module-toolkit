@echo off
REM Double-click to start the NWN Module Toolkit dashboard (needs Python 3.8+ from python.org)
cd /d "%~dp0"
REM The py launcher picks the newest Python 3; "python" is the fallback only when py is not installed, so a
REM dashboard that stops with an error (port in use, Python too old) is not started a second time.
where py >nul 2>nul
if errorlevel 1 goto nopy
py -3 nwn_dashboard.py
goto done
:nopy
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found - install Python 3.8 or newer from python.org and tick "Add python.exe to PATH".
  goto done
)
python nwn_dashboard.py
:done
pause
