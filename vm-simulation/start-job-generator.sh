#!/bin/bash
#
# Start the NØMAÐ synthetic job generator
#
# Usage:
#   ./start-job-generator.sh              # Default: 10 jobs/hour
#   ./start-job-generator.sh --rate 20    # 20 jobs/hour
#   ./start-job-generator.sh --burst      # Quick burst of jobs
#

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

python3 jobs/job-generator.py "$@"
