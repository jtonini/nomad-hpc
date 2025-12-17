#!/bin/bash
#
# NØMADE Dashboard Quick Start
# 
# Run this on badenpowell, then from your laptop:
#   ssh -L 8050:localhost:8050 badenpowell
#   Open http://localhost:8050 in your browser
#

set -e

cd "$(dirname "$0")/.."

echo "==========================================================="
echo "            NOMADE Dashboard Quick Start                   "
echo "==========================================================="
echo

# Check for data sources
DATA_FLAG=""
if [ -f "/tmp/nomade-metrics.log" ]; then
    echo "[*] Found: /tmp/nomade-metrics.log"
    DATA_FLAG="--data /tmp/nomade-metrics.log"
elif [ -f "$HOME/nomade-metrics.log" ]; then
    echo "[*] Found: $HOME/nomade-metrics.log"
    DATA_FLAG="--data $HOME/nomade-metrics.log"
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
python -m nomade.cli dashboard --port 8050 $DATA_FLAG

