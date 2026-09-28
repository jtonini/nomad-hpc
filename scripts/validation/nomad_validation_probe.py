#!/usr/bin/env python3
"""
NØMAÐ validation probe — passive baseline collector.

Purpose: characterize per-user process activity on a machine before
deploying alerting thresholds. Collect, don't alert. Write to a
private SQLite file. Track collector overhead.

Usage:
    python3 nomad_validation_probe.py --machine spydur --duration 168h
    python3 nomad_validation_probe.py --machine arachne-node05 --duration 48h --walk-fds

Flags:
    --machine       Identifier for this machine (free-form string, used in DB and output filename)
    --duration      How long to run, e.g. "168h", "30m", "2d". Default: 168h (1 week)
    --interval      Seconds between samples. Default: 60
    --output-dir    Where to write the SQLite file. Default: ~/nomad_validation
    --walk-fds      Enable /proc/<pid>/fd walking for filesystem attribution. Off by default.
    --min-uid       Ignore users with uid below this. Default: 1000
    --extra-system-users  Comma-separated list of usernames to also ignore (e.g. "slurm,munge")

Output:
    <output-dir>/<machine>_baseline_<YYYYMMDD_HHMMSS>.db

Tables:
    samples         — one row per (timestamp, pid) for non-system user processes
    fd_paths        — one row per (timestamp, pid, fd) when --walk-fds enabled
    overhead        — one row per sample with collector's own CPU/memory/wall time
    meta            — single row with run config (machine, interval, start time, etc.)
"""

import argparse
import os
import pwd
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
from pathlib import Path

try:
    import psutil
except ImportError:
    sys.stderr.write("ERROR: psutil not installed. Run: pip install --user psutil\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_duration(s: str) -> int:
    """Parse '168h', '30m', '2d', '3600s' into seconds."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"Bad duration: {s!r}. Use e.g. '60s', '30m', '24h', '7d'."
        )
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_args():
    p = argparse.ArgumentParser(description="NØMAÐ validation probe")
    p.add_argument("--machine", required=True,
                   help="Machine identifier (e.g. 'spydur', 'arachne-head', 'arachne-node05')")
    p.add_argument("--duration", type=parse_duration, default=parse_duration("168h"),
                   help="Run duration, e.g. '168h', '30m', '2d'. Default: 168h")
    p.add_argument("--interval", type=int, default=60,
                   help="Seconds between samples. Default: 60")
    p.add_argument("--output-dir", type=Path,
                   default=Path.home() / "nomad_validation",
                   help="Output directory. Default: ~/nomad_validation")
    p.add_argument("--walk-fds", action="store_true",
                   help="Walk /proc/<pid>/fd for filesystem attribution (compute nodes only)")
    p.add_argument("--min-uid", type=int, default=1000,
                   help="Ignore users with uid below this. Default: 1000")
    p.add_argument("--extra-system-users", default="",
                   help="Comma-separated usernames to also treat as system (e.g. 'slurm,munge')")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    timestamp INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    ppid INTEGER,
    username TEXT,
    uid INTEGER,
    command TEXT,            -- argv[0] or process name
    cmdline TEXT,            -- truncated full cmdline
    cpu_percent REAL,        -- averaged over the sample interval
    memory_rss_mb REAL,
    runtime_seconds INTEGER, -- process age at sample time
    io_read_bytes INTEGER,   -- cumulative from /proc/<pid>/io
    io_write_bytes INTEGER,
    num_threads INTEGER,
    status TEXT,
    PRIMARY KEY (timestamp, pid)
);
CREATE INDEX IF NOT EXISTS idx_samples_user ON samples(username);
CREATE INDEX IF NOT EXISTS idx_samples_cpu ON samples(cpu_percent);

CREATE TABLE IF NOT EXISTS fd_paths (
    timestamp INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    fd INTEGER NOT NULL,
    path TEXT,
    PRIMARY KEY (timestamp, pid, fd)
);
CREATE INDEX IF NOT EXISTS idx_fd_pid ON fd_paths(pid);

CREATE TABLE IF NOT EXISTS overhead (
    timestamp INTEGER PRIMARY KEY,
    probe_cpu_percent REAL,
    probe_memory_rss_mb REAL,
    sample_wall_seconds REAL,
    n_processes_seen INTEGER,
    n_processes_recorded INTEGER,
    n_fds_walked INTEGER
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_system_users(min_uid: int, extras: set[str]) -> set[str]:
    """Return set of usernames considered 'system' (to be excluded)."""
    sysusers = set(extras)
    try:
        for entry in pwd.getpwall():
            if entry.pw_uid < min_uid:
                sysusers.add(entry.pw_name)
    except Exception:
        pass
    # Always-ignored regardless of uid
    sysusers.update({"root", "nobody", "nfsnobody"})
    return sysusers


CMDLINE_MAX = 512


def get_cmdline(proc: psutil.Process) -> str:
    try:
        parts = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
    s = " ".join(parts) if parts else ""
    return s[:CMDLINE_MAX]


def walk_fds(pid: int) -> list[tuple[int, str]]:
    """Return [(fd, resolved_path), ...] for /proc/<pid>/fd, best effort."""
    out = []
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except (PermissionError, FileNotFoundError, OSError):
        return out
    for name in entries:
        try:
            fd = int(name)
        except ValueError:
            continue
        try:
            target = os.readlink(f"{fd_dir}/{name}")
        except OSError:
            continue
        # Skip non-file fds (sockets, pipes, anon_inodes)
        if target.startswith(("socket:", "pipe:", "anon_inode:", "/dev/")):
            continue
        out.append((fd, target))
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now()
    db_name = f"{args.machine}_baseline_{started.strftime('%Y%m%d_%H%M%S')}.db"
    db_path = args.output_dir / db_name

    extras = {x.strip() for x in args.extra_system_users.split(",") if x.strip()}
    sysusers = load_system_users(args.min_uid, extras)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Reasonable durability/perf tradeoff for a week-long run:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    meta = {
        "machine": args.machine,
        "started_utc": _utc_now_iso(),
        "duration_seconds": str(args.duration),
        "interval_seconds": str(args.interval),
        "walk_fds": "1" if args.walk_fds else "0",
        "min_uid": str(args.min_uid),
        "system_users_excluded": ",".join(sorted(sysusers)),
        "hostname": os.uname().nodename,
        "probe_pid": str(os.getpid()),
        "python_version": sys.version.split()[0],
        "psutil_version": psutil.__version__,
    }
    conn.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta.items())
    conn.commit()

    # Self-monitoring handle
    self_proc = psutil.Process(os.getpid())
    self_proc.cpu_percent(None)  # prime

    # Prime per-process CPU readings on first pass — psutil needs two samples
    # to compute CPU percent. We do a non-recording warmup pass first.
    for proc in psutil.process_iter(["pid"]):
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    deadline = time.time() + args.duration
    sys.stderr.write(
        f"[probe] machine={args.machine} db={db_path} "
        f"duration={args.duration}s interval={args.interval}s "
        f"walk_fds={args.walk_fds}\n"
    )
    sys.stderr.flush()

    sample_idx = 0
    next_tick = time.time() + args.interval

    try:
        while time.time() < deadline:
            # Sleep until the next aligned tick (drift-resistant).
            now = time.time()
            if now < next_tick:
                time.sleep(next_tick - now)
            tick_start = time.time()
            ts = int(tick_start)

            seen = 0
            recorded = 0
            fds_walked = 0
            sample_rows = []
            fd_rows = []

            for proc in psutil.process_iter():
                seen += 1
                try:
                    with proc.oneshot():
                        uids = proc.uids()
                        uid = uids.real
                        try:
                            uname = pwd.getpwuid(uid).pw_name
                        except KeyError:
                            uname = str(uid)

                        if uname in sysusers or uid < args.min_uid:
                            # Still need to drain cpu_percent so next reading is correct
                            proc.cpu_percent(None)
                            continue

                        cpu = proc.cpu_percent(None)  # since last call (per-process)
                        mem = proc.memory_info().rss / (1024 * 1024)
                        create = proc.create_time()
                        runtime = int(tick_start - create)
                        cmd = proc.name()
                        cmdline = get_cmdline(proc)
                        ppid = proc.ppid()
                        nthreads = proc.num_threads()
                        status = proc.status()

                        try:
                            io = proc.io_counters()
                            ior, iow = io.read_bytes, io.write_bytes
                        except (psutil.AccessDenied, AttributeError):
                            ior = iow = None

                        sample_rows.append((
                            ts, proc.pid, ppid, uname, uid,
                            cmd, cmdline, cpu, mem, runtime,
                            ior, iow, nthreads, status,
                        ))
                        recorded += 1

                        if args.walk_fds:
                            for fd, target in walk_fds(proc.pid):
                                fd_rows.append((ts, proc.pid, fd, target))
                                fds_walked += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except Exception as e:
                    # Don't let one bad process kill the run
                    sys.stderr.write(f"[probe] warn: pid={getattr(proc,'pid','?')} {e!r}\n")
                    continue

            # Batch insert
            if sample_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    sample_rows,
                )
            if fd_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO fd_paths VALUES (?,?,?,?)",
                    fd_rows,
                )

            # Overhead row
            wall = time.time() - tick_start
            self_cpu = self_proc.cpu_percent(None)
            self_mem = self_proc.memory_info().rss / (1024 * 1024)
            conn.execute(
                "INSERT OR REPLACE INTO overhead VALUES (?,?,?,?,?,?,?)",
                (ts, self_cpu, self_mem, wall, seen, recorded, fds_walked),
            )
            conn.commit()

            sample_idx += 1
            if sample_idx % 60 == 0:  # heartbeat every ~hour at 60s interval
                sys.stderr.write(
                    f"[probe] {datetime.now().isoformat(timespec='seconds')} "
                    f"samples={sample_idx} last_wall={wall:.2f}s "
                    f"recorded={recorded}/{seen}\n"
                )
                sys.stderr.flush()

            next_tick += args.interval
            # If we've fallen badly behind (e.g. machine was suspended), resync
            if next_tick < time.time():
                next_tick = time.time() + args.interval
    except KeyboardInterrupt:
        sys.stderr.write("[probe] interrupted, flushing\n")
    finally:
        conn.commit()
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            ("ended_utc", _utc_now_iso()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            ("samples_taken", str(sample_idx)),
        )
        conn.commit()
        conn.close()
        sys.stderr.write(f"[probe] done. db={db_path}\n")


if __name__ == "__main__":
    main()
