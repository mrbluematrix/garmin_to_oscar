#!/bin/bash
# setup.sh - installs everything garmin2oscar needs, in this folder:
#   .venv/          Python environment (garminconnect, and bleak + cryptography for the AirSense)
#   airbreak-plus/  ResMed AirSense 11 Bluetooth library (only used with --airsense)
# Run it once, and again later to update:   ./setup.sh
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.12 or newer is needed. On a Mac: brew install python@3.13  (then run ./setup.sh again)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creating the Python environment (.venv) with $PY..."
  "$PY" -m venv .venv
fi
echo "Installing / updating the Python libraries..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q --upgrade garminconnect bleak cryptography

if [ -d airbreak-plus/.git ]; then
  echo "Updating airbreak-plus..."
  git -C airbreak-plus pull -q --ff-only || echo "(airbreak-plus could not be updated, the current copy is kept)"
elif command -v git >/dev/null 2>&1; then
  echo "Downloading airbreak-plus (AirSense 11 Bluetooth)..."
  git clone -q --depth 1 https://github.com/m-kozlowski/airbreak-plus.git airbreak-plus
else
  echo "git not found: airbreak-plus not installed (only needed for --airsense)."
fi
chmod +x g2o 2>/dev/null || true

echo
echo "Done. Use:"
echo "  ./g2o                  last night"
echo "  ./g2o --nights 7       the last 7 nights"
echo "  ./g2o --airsense       ... and line the times up with the AirSense 11 clock"
if [ ! -f "$HOME/.as11_ble.json" ]; then
  echo
  echo "AirSense not paired yet (only needed for --airsense). Once, near the machine:"
  echo "  .venv/bin/python airbreak-plus/python/as11_config.py devices scan"
  echo "  .venv/bin/python airbreak-plus/python/as11_config.py devices pair <address shown by scan>"
fi
