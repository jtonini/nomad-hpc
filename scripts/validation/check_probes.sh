#!/bin/bash
# Quick liveness check for all three validation probes.
# Run from gpuws1.

set -u

echo "=== hpc1 ==="
ssh hpcadmin@hpc1 'PID=$(cat ~/nomad_validation/hpc1_probe.pid 2>/dev/null)
if [ -n "$PID" ] && ps -p $PID > /dev/null 2>&1; then
    ps -p $PID -o pid,etime,pcpu,rss,comm
    DB=$(ls -t ~/nomad_validation/hpc1_baseline_*.db 2>/dev/null | head -1)
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
echo "=== hpc2 head ==="
ssh monitor@hpc2 'sudo bash -c "
PID=\$(cat /var/lib/nomad_validation/hpc2_head_probe.pid 2>/dev/null)
if [ -n \"\$PID\" ] && ps -p \$PID > /dev/null 2>&1; then
    ps -p \$PID -o pid,etime,pcpu,rss,comm
    DB=\$(ls -t /var/lib/nomad_validation/hpc2-head_baseline_*.db 2>/dev/null | head -1)
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
echo "=== hpc2 node02 ==="
ssh monitor@hpc2 'ssh node02 "
PID=\$(cat /var/lib/nomad_validation/node02_probe.pid 2>/dev/null)
if [ -n \"\$PID\" ] && ps -p \$PID > /dev/null 2>&1; then
    ps -p \$PID -o pid,etime,pcpu,rss,comm
    DB=\$(ls -t /var/lib/nomad_validation/hpc2-node02_baseline_*.db 2>/dev/null | head -1)
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
