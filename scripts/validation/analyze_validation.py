#!/usr/bin/env python3
"""
Analyze a NØMAÐ validation probe DB.

Produces:
  - Per-user CPU percentile distribution
  - Catalog of processes that ever exceeded the proposed threshold
  - Count of distinct "elevated events" the proposed alerting rule would have fired
  - Collector overhead summary
  - Optional: filesystem attribution summary (if --walk-fds was enabled)

Usage:
    python3 analyze_validation.py path/to/spydur_baseline_*.db
    python3 analyze_validation.py path/to/db --cpu-threshold 10 --duration-minutes 5
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median


def parse_args():
    p = argparse.ArgumentParser(description="Analyze NØMAÐ validation probe DB")
    p.add_argument("db", type=Path)
    p.add_argument("--cpu-threshold", type=float, default=10.0,
                   help="CPU%% threshold to flag. Default: 10")
    p.add_argument("--duration-minutes", type=float, default=5.0,
                   help="Sustained duration in minutes to flag. Default: 5")
    p.add_argument("--top-n", type=int, default=20,
                   help="How many top processes/users to show. Default: 20")
    return p.parse_args()


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main():
    args = parse_args()
    if not args.db.exists():
        sys.exit(f"DB not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # --- meta ---
    meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
    interval = int(meta.get("interval_seconds", 60))
    samples_required = max(1, int((args.duration_minutes * 60) / interval))

    section("RUN INFO")
    for k in ("machine", "hostname", "started_utc", "ended_utc",
              "samples_taken", "interval_seconds", "walk_fds"):
        if k in meta:
            print(f"  {k:>22}: {meta[k]}")
    print(f"  {'cpu_threshold':>22}: {args.cpu_threshold}%")
    print(f"  {'duration_minutes':>22}: {args.duration_minutes} "
          f"(={samples_required} consecutive samples at {interval}s interval)")

    # --- sample counts ---
    n_rows, n_ticks, n_users, n_pids = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT timestamp),
               COUNT(DISTINCT username), COUNT(DISTINCT pid)
        FROM samples
    """).fetchone()
    section("DATA VOLUME")
    print(f"  total sample rows:    {n_rows}")
    print(f"  distinct ticks:       {n_ticks}")
    print(f"  distinct users:       {n_users}")
    print(f"  distinct pids:        {n_pids}")
    if n_rows == 0:
        print("\nNo user-process samples recorded. "
              "Check --min-uid and system-user exclusion.")
        return

    # --- per-user CPU distribution ---
    # Aggregate per (user, tick): sum of CPU across that user's processes.
    user_tick_cpu = defaultdict(list)
    for r in conn.execute("""
        SELECT timestamp, username, SUM(cpu_percent) AS cpu
        FROM samples GROUP BY timestamp, username
    """):
        user_tick_cpu[r["username"]].append(r["cpu"])

    section("PER-USER CPU DISTRIBUTION  (sum across user's processes per tick)")
    print(f"  {'user':<16} {'p50':>8} {'p90':>8} {'p99':>8} {'max':>8} {'ticks':>8}")
    user_summary = []
    for u, vals in user_tick_cpu.items():
        vals.sort()
        user_summary.append((u, percentile(vals, 50), percentile(vals, 90),
                             percentile(vals, 99), vals[-1], len(vals)))
    user_summary.sort(key=lambda x: x[3], reverse=True)  # by p99
    for row in user_summary[:args.top_n]:
        print(f"  {row[0]:<16} {row[1]:>8.1f} {row[2]:>8.1f} "
              f"{row[3]:>8.1f} {row[4]:>8.1f} {row[5]:>8}")
    if len(user_summary) > args.top_n:
        print(f"  ... and {len(user_summary) - args.top_n} more users")

    # --- elevated events (would the rule have fired?) ---
    # A "process-event" = N consecutive samples for the same pid where
    # cpu_percent >= threshold. We count distinct events.
    section(f"ELEVATED EVENTS  "
            f"(>= {args.cpu_threshold}% CPU for >= {samples_required} consecutive samples)")
    pid_series = defaultdict(list)  # pid -> [(ts, cpu, user, cmd)]
    for r in conn.execute("""
        SELECT timestamp, pid, username, command, cmdline, cpu_percent
        FROM samples ORDER BY pid, timestamp
    """):
        pid_series[r["pid"]].append(
            (r["timestamp"], r["cpu_percent"], r["username"],
             r["command"], r["cmdline"])
        )

    events = []  # (user, pid, cmd, cmdline, start_ts, end_ts, n_samples, peak_cpu)
    for pid, series in pid_series.items():
        i, n = 0, len(series)
        while i < n:
            if series[i][1] >= args.cpu_threshold:
                j = i
                while j < n and series[j][1] >= args.cpu_threshold:
                    # Require samples to be roughly consecutive in time
                    if j > i and series[j][0] - series[j-1][0] > interval * 2:
                        break
                    j += 1
                run = series[i:j]
                if len(run) >= samples_required:
                    peak = max(s[1] for s in run)
                    events.append((
                        run[0][2], pid, run[0][3], run[0][4],
                        run[0][0], run[-1][0], len(run), peak,
                    ))
                i = j
            else:
                i += 1

    print(f"  total elevated events: {len(events)}")
    if events:
        events.sort(key=lambda x: x[6], reverse=True)
        print(f"\n  {'user':<14} {'pid':>7} {'samples':>8} {'peak_cpu':>9}  command")
        for ev in events[:args.top_n]:
            user, pid, cmd, cmdline, _, _, ns, peak = ev
            line = (cmdline or cmd)[:60]
            print(f"  {user:<14} {pid:>7} {ns:>8} {peak:>9.1f}  {line}")
        if len(events) > args.top_n:
            print(f"  ... and {len(events) - args.top_n} more events")

        # Per-user event count
        per_user = defaultdict(int)
        for ev in events:
            per_user[ev[0]] += 1
        print(f"\n  events per user (top {args.top_n}):")
        for u, c in sorted(per_user.items(), key=lambda x: -x[1])[:args.top_n]:
            print(f"    {u:<16} {c}")

        # Process catalog — distinct commands that ever fired
        print(f"\n  distinct commands appearing in elevated events:")
        cmd_count = defaultdict(int)
        for ev in events:
            cmd_count[ev[2]] += 1
        for cmd, c in sorted(cmd_count.items(), key=lambda x: -x[1]):
            print(f"    {cmd:<24} {c} events")

    # --- overhead ---
    section("PROBE OVERHEAD")
    row = conn.execute("""
        SELECT
            ROUND(AVG(probe_cpu_percent),2),
            ROUND(MAX(probe_cpu_percent),2),
            ROUND(AVG(probe_memory_rss_mb),1),
            ROUND(MAX(probe_memory_rss_mb),1),
            ROUND(AVG(sample_wall_seconds),3),
            ROUND(MAX(sample_wall_seconds),3),
            ROUND(AVG(n_processes_seen),0),
            ROUND(AVG(n_processes_recorded),0)
        FROM overhead
    """).fetchone()
    print(f"  CPU%   avg={row[0]:>5}   max={row[1]:>5}")
    print(f"  RSS MB avg={row[2]:>5}   max={row[3]:>5}")
    print(f"  wall s avg={row[4]:>5}   max={row[5]:>5}")
    print(f"  proc avg seen={row[6]}   avg recorded={row[7]}")

    # --- fd attribution (optional) ---
    if meta.get("walk_fds") == "1":
        section("FILESYSTEM ATTRIBUTION  (from /proc/<pid>/fd)")
        # Classify paths into rough buckets.
        # NOTE: bucket names reflect intent, not mount point. On arachne,
        # /scratch doesn't exist as a separate filesystem -- "scratch"
        # directories live under /home (e.g. /home/scratch/<user>/...).
        # On clusters where /scratch IS a separate NFS mount, those
        # paths land in nfs_scratch.
        rows = conn.execute("""
            SELECT s.username, f.path
            FROM fd_paths f
            JOIN samples s ON s.timestamp = f.timestamp AND s.pid = f.pid
        """).fetchall()
        bucket = defaultdict(lambda: defaultdict(int))
        for r in rows:
            p = r["path"]
            if p.startswith("/localscratch") or p.startswith("/local/"):
                b = "local_scratch"
            elif p.startswith("/tmp"):
                b = "tmp"
            elif p.startswith("/scratch"):
                b = "nfs_scratch"
            elif p.startswith("/home"):
                b = "nfs_home"
            elif p.startswith("/data"):
                b = "nfs_data"
            elif p.startswith("/opt") or p.startswith("/usr") or p.startswith("/var/spool/slurmd"):
                b = "system"  # software, libs, slurm scripts -- not user I/O
            else:
                b = "other"
            bucket[r["username"]][b] += 1
        print(f"  {'user':<16} {'local':>8} {'tmp':>8} {'nfs_h':>8} "
              f"{'nfs_s':>8} {'nfs_d':>8} {'system':>8} {'other':>8}")
        for u in sorted(bucket):
            d = bucket[u]
            print(f"  {u:<16} {d['local_scratch']:>8} {d['tmp']:>8} "
                  f"{d['nfs_home']:>8} {d['nfs_scratch']:>8} "
                  f"{d['nfs_data']:>8} {d['system']:>8} {d['other']:>8}")

        # Drill-down: per-user, what's actually under nfs_home?
        # On arachne, /home/scratch/<user>/ is "scratch-like" use of NFS,
        # /home/<user>/ is "home-like" use. Worth distinguishing.
        section("NFS_HOME BREAKDOWN  (top path prefixes per user)")
        for u in sorted(bucket):
            if bucket[u]["nfs_home"] == 0:
                continue
            print(f"\n  {u}:")
            for r in conn.execute("""
                SELECT substr(f.path, 1, 50) AS prefix, COUNT(*) AS n
                FROM fd_paths f
                JOIN samples s ON s.timestamp = f.timestamp AND s.pid = f.pid
                WHERE s.username = ? AND f.path LIKE '/home%'
                GROUP BY prefix ORDER BY n DESC LIMIT 5
            """, (u,)):
                print(f"    {r[1]:>5}  {r[0]}")

    print()


if __name__ == "__main__":
    main()
