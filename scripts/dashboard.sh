#!/bin/bash
#
# NØMAÐ Dashboard Quick Start
# 
# Run this on badenpowell, then from your laptop:
#   ssh -L 8050:localhost:8050 badenpowell
#   Open http://localhost:8050 in your browser
#

set -e

cd "$(dirname "$0")/.."

echo "==========================================================="
echo "            NØMAÐ Dashboard Quick Start                   "
echo "==========================================================="
echo

# Check for data sources
DATA_FLAG=""
if [ -f "/tmp/nomad-metrics.log" ]; then
    echo "[*] Found: /tmp/nomad-metrics.log"
    DATA_FLAG="--data /tmp/nomad-metrics.log"
elif [ -f "$HOME/nomad-metrics.log" ]; then
    echo "[*] Found: $HOME/nomad-metrics.log"
    DATA_FLAG="--data $HOME/nomad-metrics.log"
else
    echo "[*] No metrics file found, using demo data"
fi

echo
echo "Starting dashboard on http://localhost:8050"
echo
echo "==========================================================="
echo "To view from your laptop:"
echo "  1. Open a new terminal"
echo "  2. Run: ssh -L 8050:localhost:8050 badenpowell"
echo "  3. Open: http://localhost:8050"
echo "==========================================================="
echo
echo "Press Ctrl+C to stop"
echo

# Run the dashboard
python -m nomad.cli dashboard --port 8050 $DATA_FLAG

