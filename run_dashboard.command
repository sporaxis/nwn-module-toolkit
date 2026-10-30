#!/bin/bash
# Double-click to start the NWN Module Toolkit dashboard on macOS (needs Python 3.8+ from python.org).
# If macOS refuses to open it, right-click it and choose Open once.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found - install Python 3.8 or newer from python.org"
  read -r -p "Press Return to close"
  exit 1
fi
python3 nwn_dashboard.py
read -r -p "Press Return to close"
