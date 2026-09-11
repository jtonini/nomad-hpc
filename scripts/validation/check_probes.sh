#!/bin/bash
# Quick liveness check for all three validation probes.
# Run from badenpowell.

set -u

echo "=== spydur ==="
ssh installer@spydur 'PID=$(cat ~/nomad_validation/spydur_probe.pid 2>/dev/null)
if [ -n "$PID" ] && ps -p $PID > /dev/null 2>&1; then
    ps -p $PID -o pid,etime,pcpu,rss,comm
    DB=$(ls -t ~/nomad_validation/spydur_baseline_*.db 2>/dev/null | head -1)
    [ -n "$DB" ] && python3 -c "
import sqlite3
c = sqlite3.connect(\"$DB\")
n = c.execute(\"SELECT COUNT(*) FROM samples\").fetchone()[0]
t = c.execute(\"SELECT COUNT(DISTINCT timestamp) FROM samples\").fetchone()[0]
u = c.execute(\"SELECT COUNT(DISTINCT username) FROM samples\").fetchone()[0]
print(f\"  rows={n} ticks={t} users={u}\")
"
else
    echo "NOT RUNNING (pid=$PID)"
fi'

echo
echo "=== arachne head ==="
ssh zeus@arachne 'sudo bash -c "
PID=\$(cat /var/lib/nomad_validation/arachne_head_probe.pid 2>/dev/null)
if [ -n \"\$PID\" ] && ps -p \$PID > /dev/null 2>&1; then
    ps -p \$PID -o pid,etime,pcpu,rss,comm
    DB=\$(ls -t /var/lib/nomad_validation/arachne-head_baseline_*.db 2>/dev/null | head -1)
    [ -n \"\$DB\" ] && python3 -c \"
import sqlite3
c = sqlite3.connect(\\\"\$DB\\\")
n = c.execute(\\\"SELECT COUNT(*) FROM samples\\\").fetchone()[0]
t = c.execute(\\\"SELECT COUNT(DISTINCT timestamp) FROM samples\\\").fetchone()[0]
u = c.execute(\\\"SELECT COUNT(DISTINCT username) FROM samples\\\").fetchone()[0]
print(f\\\"  rows={n} ticks={t} users={u}\\\")
\"
else
    echo NOT RUNNING
fi"'

echo
echo "=== arachne node02 ==="
ssh zeus@arachne 'ssh node02 "
PID=\$(cat /var/lib/nomad_validation/node02_probe.pid 2>/dev/null)
if [ -n \"\$PID\" ] && ps -p \$PID > /dev/null 2>&1; then
    ps -p \$PID -o pid,etime,pcpu,rss,comm
    DB=\$(ls -t /var/lib/nomad_validation/arachne-node02_baseline_*.db 2>/dev/null | head -1)
    [ -n \"\$DB\" ] && python3 -c \"
import sqlite3
c = sqlite3.connect(\\\"\$DB\\\")
n = c.execute(\\\"SELECT COUNT(*) FROM samples\\\").fetchone()[0]
t = c.execute(\\\"SELECT COUNT(DISTINCT timestamp) FROM samples\\\").fetchone()[0]
u = c.execute(\\\"SELECT COUNT(DISTINCT username) FROM samples\\\").fetchone()[0]
fd = c.execute(\\\"SELECT COUNT(*) FROM fd_paths\\\").fetchone()[0]
print(f\\\"  rows={n} ticks={t} users={u} fd_rows={fd}\\\")
\"
else
    echo NOT RUNNING
fi
"'
