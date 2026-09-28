# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Workstation Collector

Collects system metrics from departmental workstations:
- CPU and memory utilization
- Disk usage
- Active user sessions
- Process information
- Department/group metadata

Supports both local and SSH-based remote collection.
"""

import logging
import socket
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry
import base64
import json
import os
import tempfile
from dataclasses import asdict

from nomad.collectors import pacct as _pacct

# Per-user tracking: deployed location for the cgroup probe on each workstation.
# Push via: scp nomad/collectors/cgroup_probe.py <host>:/usr/local/lib/nomad/
# (The bootstrap command will automate this later.)
WORKSTATION_PROBE_PATH = "/usr/local/lib/nomad/cgroup_probe.py"
WORKSTATION_PROBE_FALLBACK = "/tmp/nomad_cgroup_probe.py"
DEFAULT_PACCT_PATH = "/var/account/pacct"
# Mount monitoring probe. Same deployment pattern as the cgroup probe.
WORKSTATION_MOUNT_PROBE_PATH = "/usr/local/lib/nomad/mount_probe.py"
WORKSTATION_MOUNT_PROBE_FALLBACK = "/tmp/nomad_mount_probe.py"
COLLECTOR_VERSION = "1.0"


logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    """Active user session information."""
    username: str
    terminal: str
    login_time: str
    idle_time: str
    remote_host: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'username': self.username,
            'terminal': self.terminal,
            'login_time': self.login_time,
            'idle_time': self.idle_time,
            'remote_host': self.remote_host,
        }


@dataclass
class WorkstationStats:
    """Workstation system statistics."""
    hostname: str
    department: str | None = None

    # System info
    os_version: str = ''
    cpu_model: str = ''
    uptime_seconds: int = 0
    load_avg_1m: float = 0.0
    load_avg_5m: float = 0.0
    load_avg_15m: float = 0.0

    # CPU
    cpu_count: int = 0
    cpu_user_pct: float = 0.0
    cpu_system_pct: float = 0.0
    cpu_idle_pct: float = 0.0
    cpu_iowait_pct: float = 0.0

    # Memory (MB)
    memory_total_mb: int = 0
    memory_used_mb: int = 0
    memory_free_mb: int = 0
    memory_cached_mb: int = 0
    swap_total_mb: int = 0
    swap_used_mb: int = 0

    # Disk
    disk_total_gb: float = 0.0
    disk_used_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_usage_pct: float = 0.0

    # Users
    users_logged_in: int = 0
    sessions: list = field(default_factory=list)

    # Processes
    process_count: int = 0
    zombie_count: int = 0

    # Status
    status: str = 'online'  # online, offline, degraded
    last_seen: datetime | None = None
    # Per-user data (populated when cgroup v2 / pacct are available on the host)
    user_snapshots: list = field(default_factory=list)
    process_records: list = field(default_factory=list)
    mount_snapshots: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'hostname': self.hostname,
            'department': self.department,
            'os_version': self.os_version,
            'cpu_model': self.cpu_model,
            'uptime_seconds': self.uptime_seconds,
            'load_avg_1m': self.load_avg_1m,
            'load_avg_5m': self.load_avg_5m,
            'load_avg_15m': self.load_avg_15m,
            'cpu_count': self.cpu_count,
            'cpu_user_pct': self.cpu_user_pct,
            'cpu_system_pct': self.cpu_system_pct,
            'cpu_idle_pct': self.cpu_idle_pct,
            'cpu_iowait_pct': self.cpu_iowait_pct,
            'memory_total_mb': self.memory_total_mb,
            'memory_used_mb': self.memory_used_mb,
            'memory_free_mb': self.memory_free_mb,
            'memory_cached_mb': self.memory_cached_mb,
            'swap_total_mb': self.swap_total_mb,
            'swap_used_mb': self.swap_used_mb,
            'disk_total_gb': self.disk_total_gb,
            'disk_used_gb': self.disk_used_gb,
            'disk_free_gb': self.disk_free_gb,
            'disk_usage_pct': self.disk_usage_pct,
            'users_logged_in': self.users_logged_in,
            'process_count': self.process_count,
            'zombie_count': self.zombie_count,
            'status': self.status,
            'user_snapshots': [asdict(s) for s in self.user_snapshots],
            'process_records': [asdict(r) for r in self.process_records],
            'mount_snapshots': list(self.mount_snapshots),
        }

    @property
    def memory_usage_pct(self) -> float:
        """Memory usage percentage."""
        if self.memory_total_mb == 0:
            return 0.0
        return (self.memory_used_mb / self.memory_total_mb) * 100

    @property
    def is_healthy(self) -> bool:
        """Check if workstation is in healthy state."""
        return (
            self.status == 'online' and
            self.memory_usage_pct < 95 and
            self.disk_usage_pct < 95 and
            self.zombie_count < 10 and
            self.load_avg_1m < self.cpu_count * 2
        )


@dataclass
class UserCgroupSnapshot:
    """Point-in-time cgroup v2 data for one logged-in user on a workstation.

    Populated by the cgroup_probe (nomad/collectors/cgroup_probe.py)
    invoked over SSH on each collection cycle. Fields use None for missing
    data so callers can distinguish "feature not present on this kernel"
    from "zero activity".
    """

    hostname: str
    username: str
    uid: int
    # cgroup slice ctime (unix seconds). Changes when user logs out and
    # back in — slice is destroyed and recreated. Delta queries MUST group
    # by session_epoch to avoid computing nonsense across resets.
    session_epoch: int
    # Collection timestamp set by the probe on the remote host, so the value
    # reflects the moment of measurement (not when the collector ingested it).
    collected_at: int

    # CPU (microseconds, cumulative since session_epoch)
    cpu_usage_usec: int | None
    cpu_user_usec: int | None
    cpu_system_usec: int | None

    # Memory (bytes)
    memory_current_bytes: int | None
    memory_peak_bytes: int | None  # None on kernels < 5.19 without memory.peak

    # I/O (bytes, cumulative, summed across block devices)
    io_read_bytes: int | None
    io_write_bytes: int | None

    # Process activity
    pids_current: int | None

    probe_version: str = "1"
    source: str = "cgroup_v2"


@dataclass
class ProcessAcctRecord:
    """One pacct record (process exit) from a workstation.

    Populated by copying /var/account/pacct from the remote host and
    parsing it locally via nomad.collectors.pacct. Provides the historical
    per-process record that complements the live cgroup snapshots.
    """

    hostname: str
    username: str
    uid: int
    pid: int
    ppid: int
    command: str
    start_time: int        # unix seconds
    exit_time: int         # unix seconds
    elapsed_seconds: float
    cpu_user_seconds: float
    cpu_system_seconds: float
    memory_avg_kb: int     # ac_mem: AVERAGE memory, not peak (kernel limitation)
    io_chars: int
    io_read_blocks: int
    io_write_blocks: int
    exit_code: int
    flags: int             # ac_flag bitfield: AFORK|ASU|ACORE|AXSIG



def run_command(cmd: str, host: str | None = None, timeout: int = 30) -> str:
    """Run command locally or via SSH."""
    if host and host not in ('localhost', '127.0.0.1', socket.gethostname()):
        cmd = f"ssh -o ConnectTimeout=10 -o BatchMode=yes {host} '{cmd}'"

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise CollectionError(f"Command timed out: {cmd[:50]}...")
    except Exception as e:
        raise CollectionError(f"Command failed: {e}")


# Cache for probe sources — loaded once per process from the package.
_PROBE_SOURCE_CACHE: dict[str, str] = {}


def _load_probe_source(probe_name: str) -> str:
    """Read a probe script from the nomad.collectors package.

    Caches sources so repeated probe invocations do not re-read the file.
    """
    if probe_name in _PROBE_SOURCE_CACHE:
        return _PROBE_SOURCE_CACHE[probe_name]
    try:
        import importlib.resources
        ref = importlib.resources.files("nomad.collectors") / f"{probe_name}.py"
        source = ref.read_text(encoding="utf-8")
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, f"{probe_name}.py"), encoding="utf-8") as f:
            source = f.read()
    _PROBE_SOURCE_CACHE[probe_name] = source
    return source


def run_python_probe(
    probe_name: str,
    host: str | None = None,
    timeout: int = 30,
) -> str:
    """Pipe a probe script over SSH stdin and return its stdout.

    Reads probe source from the nomad.collectors package and sends it
    to `python3 -` on the target host. Eliminates per-host probe
    deployment — works on read-only systems, immutable OS images,
    and ephemeral container hosts.
    """
    script = _load_probe_source(probe_name)

    if host and host not in ('localhost', '127.0.0.1', socket.gethostname()):
        argv = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            host,
            "python3 -",
        ]
    else:
        argv = ["python3", "-"]

    try:
        result = subprocess.run(
            argv,
            input=script,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CollectionError(
            f"{probe_name} timed out on {host or 'local'} after {timeout}s"
        )
    except Exception as e:
        raise CollectionError(f"{probe_name} failed on {host or 'local'}: {e}")

    if result.returncode != 0:
        err = result.stderr.strip() or "(no stderr)"
        raise CollectionError(
            f"{probe_name} on {host or 'local'} exited "
            f"{result.returncode}: {err[:200]}"
        )
    return result.stdout.strip()


def parse_uptime(output: str) -> tuple[int, float, float, float]:
    """Parse uptime output for uptime and load averages."""
    # Example: " 10:30:01 up 5 days,  3:45,  2 users,  load average: 0.50, 0.40, 0.35"
    import re

    uptime_seconds = 0
    load_1, load_5, load_15 = 0.0, 0.0, 0.0

    # Parse load averages
    load_match = re.search(r'load average[s]?:\s*([\d.]+),?\s*([\d.]+),?\s*([\d.]+)', output)
    if load_match:
        load_1 = float(load_match.group(1))
        load_5 = float(load_match.group(2))
        load_15 = float(load_match.group(3))

    # Parse uptime (simplified)
    days_match = re.search(r'up\s+(\d+)\s+day', output)
    hours_match = re.search(r'(\d+):(\d+)', output)

    if days_match:
        uptime_seconds += int(days_match.group(1)) * 86400
    if hours_match:
        uptime_seconds += int(hours_match.group(1)) * 3600
        uptime_seconds += int(hours_match.group(2)) * 60

    return uptime_seconds, load_1, load_5, load_15


def parse_meminfo(output: str) -> dict[str, int]:
    """Parse /proc/meminfo output."""
    mem = {}
    for line in output.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            # Extract numeric value (in kB)
            import re
            num = re.search(r'(\d+)', value)
            if num:
                mem[key.strip()] = int(num.group(1))
    return mem


def parse_df(output: str, path: str = '/') -> tuple[float, float, float, float]:
    """Parse df output for disk usage."""
    for line in output.split('\n')[1:]:  # Skip header
        parts = line.split()
        if len(parts) >= 6 and parts[5] == path:
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            free_kb = int(parts[3])
            usage_pct = float(parts[4].rstrip('%'))
            return (
                total_kb / 1024 / 1024,  # GB
                used_kb / 1024 / 1024,
                free_kb / 1024 / 1024,
                usage_pct
            )
    return 0.0, 0.0, 0.0, 0.0


def parse_who(output: str) -> list[UserSession]:
    """Parse 'who' command output."""
    sessions = []
    for line in output.split('\n'):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            username = parts[0]
            terminal = parts[1]
            login_time = ' '.join(parts[2:4]) if len(parts) >= 4 else parts[2]
            remote = parts[-1].strip('()') if parts[-1].startswith('(') else None
            sessions.append(UserSession(
                username=username,
                terminal=terminal,
                login_time=login_time,
                idle_time='',
                remote_host=remote
            ))
    return sessions


@registry.register
class WorkstationCollector(BaseCollector):
    """
    Collector for departmental workstation metrics.
    
    Configuration:
        workstations:
          - hostname: ws-physics-01
            department: physics
          - hostname: ws-chem-lab
            department: chemistry
            
    Collected data:
        - System load and uptime
        - CPU utilization
        - Memory usage
        - Disk usage
        - Active user sessions
        - Process counts
    """

    name = "workstation"
    description = "Departmental workstation metrics"
    default_interval = 300  # 5 minutes

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.workstations = config.get('workstations', [])
        logger.info(f"WorkstationCollector initialized with {len(self.workstations)} workstations")

    def collect(self) -> list[dict[str, Any]]:
        """Collect metrics from all configured workstations."""
        results = []

        for ws_config in self.workstations:
            hostname = ws_config.get('hostname')
            department = ws_config.get('department')

            if not hostname:
                continue

            try:
                stats = self._collect_workstation(hostname, department)
                results.append(stats.to_dict())
                logger.debug(f"Collected from {hostname}: {stats.status}")
            except CollectionError as e:
                logger.warning(f"Failed to collect from {hostname}: {e}")
                # Record offline status
                results.append({
                    'hostname': hostname,
                    'department': department,
                    'status': 'offline',
                })
            except Exception as e:
                logger.error(f"Unexpected error collecting from {hostname}: {e}")
                results.append({
                    'hostname': hostname,
                    'department': department,
                    'status': 'error',
                })

        return results

    def _collect_workstation(self, hostname: str, department: str | None) -> WorkstationStats:
        """Collect metrics from a single workstation."""
        stats = WorkstationStats(hostname=hostname, department=department)
        stats.last_seen = datetime.now()

        # Get uptime and load
        try:
            uptime_out = run_command('uptime', hostname)
            stats.uptime_seconds, stats.load_avg_1m, stats.load_avg_5m, stats.load_avg_15m = parse_uptime(uptime_out)
        except CollectionError:
            pass

        # Get CPU count
        try:
            cpu_out = run_command('nproc', hostname)
            stats.cpu_count = int(cpu_out.strip())
        except (CollectionError, ValueError):
            stats.cpu_count = 1

        # Get OS version
        try:
            os_out = run_command('cat /etc/os-release', hostname)
            for line in os_out.strip().split(chr(10)):
                if line.startswith('PRETTY_NAME='):
                    stats.os_version = line.split('=', 1)[1].strip().strip('"')
                    break
        except CollectionError:
            pass

        # Get CPU model
        try:
            cpu_model_out = run_command('grep "model name" /proc/cpuinfo | head -1', hostname)
            if ':' in cpu_model_out:
                stats.cpu_model = cpu_model_out.split(':', 1)[1].strip()
        except CollectionError:
            pass

        # Get memory info
        try:
            mem_out = run_command('cat /proc/meminfo', hostname)
            mem = parse_meminfo(mem_out)
            stats.memory_total_mb = mem.get('MemTotal', 0) // 1024
            stats.memory_free_mb = mem.get('MemFree', 0) // 1024
            stats.memory_cached_mb = (mem.get('Cached', 0) + mem.get('Buffers', 0)) // 1024
            stats.memory_used_mb = stats.memory_total_mb - stats.memory_free_mb - stats.memory_cached_mb
            stats.swap_total_mb = mem.get('SwapTotal', 0) // 1024
            stats.swap_used_mb = (mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) // 1024
        except CollectionError:
            pass

        # Get disk usage
        try:
            df_out = run_command('df -k /', hostname)
            stats.disk_total_gb, stats.disk_used_gb, stats.disk_free_gb, stats.disk_usage_pct = parse_df(df_out)
        except CollectionError:
            pass

        # Get logged in users
        try:
            who_out = run_command('who', hostname)
            stats.sessions = parse_who(who_out)
            stats.users_logged_in = len(set(s.username for s in stats.sessions))
        except CollectionError:
            pass

        # Get process info
        try:
            ps_out = run_command('ps aux | wc -l', hostname)
            stats.process_count = max(0, int(ps_out.strip()) - 1)  # Subtract header
        except (CollectionError, ValueError):
            pass

        try:
            zombie_out = run_command("ps aux | grep -c ' Z'", hostname)
            stats.zombie_count = int(zombie_out.strip())
        except (CollectionError, ValueError):
            pass

        # Determine status
        if stats.is_healthy:
            stats.status = 'online'
        else:
            stats.status = 'degraded'

        # Per-user data. Failures here are logged but non-fatal — machine-
        # level metrics are unaffected by per-user probe problems.
        self._collect_per_user(hostname, stats)

        # Mount monitoring. Same non-fatal guarantee.
        self._collect_mounts(hostname, stats)

        return stats

    def _collect_mounts(
        self,
        hostname: str,
        stats: WorkstationStats,
    ) -> None:
        """Collect mount-point state for one host via mount_probe.py.

        Failures here are non-fatal — machine-level metrics are
        unaffected if the probe isn't deployed, times out, or returns
        bad JSON. Same design as _collect_per_user.
        """
        try:
            # Pipe the probe over SSH stdin (see run_python_probe docstring
            # for the rationale; same pattern as cgroup_probe).
            probe_out = run_python_probe(
                "mount_probe", hostname, timeout=30,
            )
            for line in probe_out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"{hostname}: bad mount probe JSON: {e}: {line[:80]}"
                    )
                    continue
                # Normalize hostname to the collector's view.
                d["hostname"] = hostname
                stats.mount_snapshots.append(d)
        except CollectionError as e:
            # Probe not deployed yet, or SSH failed. Not an error.
            logger.debug(f"{hostname}: mount probe unavailable: {e}")

    def _collect_per_user(
        self,
        hostname: str,
        stats: WorkstationStats,
    ) -> None:
        """Collect per-user cgroup snapshots and pacct records for one host.

        Failures here never propagate — machine-level metrics are unaffected
        by per-user probe problems. This preserves the invariant that a
        degraded probe (no pacct, stale cgroup_probe, missing psacct package)
        doesn't break basic workstation monitoring.
        """
        # --- cgroup probe (live per-user snapshot) ------------------------
        try:
            # Pipe the probe script over SSH stdin — no per-host deployment
            # required. Works on read-only systems, immutable OS images,
            # and any SSH-reachable host.
            probe_out = run_python_probe(
                "cgroup_probe", hostname, timeout=20,
            )
            for line in probe_out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"{hostname}: bad cgroup probe JSON: {e}: {line[:80]}"
                    )
                    continue
                # Normalize hostname to the collector's view. The remote
                # probe may report a short name or FQDN that differs from
                # the one the collector uses as its key.
                d["hostname"] = hostname
                try:
                    stats.user_snapshots.append(UserCgroupSnapshot(**d))
                except TypeError as e:
                    logger.warning(
                        f"{hostname}: cgroup probe returned unexpected "
                        f"schema (did the probe_version change?): {e}"
                    )
        except CollectionError as e:
            # Probe not deployed yet, or SSH failed — not an error.
            logger.debug(f"{hostname}: cgroup probe unavailable: {e}")

        # --- pacct records (historical per-process) -----------------------
        try:
            last_exit_time = self._get_pacct_cursor(hostname)
            stats.process_records = self._fetch_pacct(
                hostname, since=last_exit_time
            )
        except CollectionError as e:
            # psacct not installed, pacct not readable, or file missing.
            logger.debug(f"{hostname}: pacct unavailable: {e}")
        except Exception as e:  # noqa: BLE001 — isolate pacct failures
            logger.warning(f"{hostname}: pacct ingestion failed: {e}")

    def _get_pacct_cursor(self, hostname: str) -> int | None:
        """Return the last ingested exit_time for this host, or None.

        Returns None if:
          - The workstation_pacct_cursor table doesn't exist yet (migration
            v6 hasn't been applied).
          - No prior ingestion has occurred for this host.
        """
        conn = self.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT last_exit_time FROM workstation_pacct_cursor "
                "WHERE hostname = ?",
                (hostname,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
        except sqlite3.OperationalError:
            # Table doesn't exist — migration hasn't been applied on this DB.
            return None
        finally:
            conn.close()

    def _fetch_pacct(
        self,
        hostname: str,
        since: int | None,
        pacct_path: str = DEFAULT_PACCT_PATH,
    ) -> list[ProcessAcctRecord]:
        """Copy pacct from remote, parse locally, return new records.

        Running the parser locally (not on the workstation) means:
          - No Python stdlib version parity issues across hosts.
          - The parser lives in one place (the NØMAÐ repo), not scattered
            across every workstation.
          - Binary format is validated against the same test suite.
        """
        # Gate: is pacct readable by the SSH user?
        # After `nomad workstation bootstrap` runs, the SSH user is in the
        # nomad-readers group and /var/account/pacct is mode 0640. Until
        # then, we degrade gracefully.
        probe = run_command(
            f"test -r {pacct_path} && echo ok",
            hostname,
            timeout=10,
        )
        if probe.strip() != "ok":
            raise CollectionError(f"pacct not readable at {pacct_path}")

        # Stream pacct over SSH, base64-encoded to survive the single-quote
        # shell wrapping in run_command. Size is typically KB-to-MB and
        # SSH compresses well; this is cheap compared to parsing.
        raw_b64 = run_command(
            f"base64 -w0 {pacct_path}",
            hostname,
            timeout=60,
        )
        raw = base64.b64decode(raw_b64)

        records: list[ProcessAcctRecord] = []
        with tempfile.NamedTemporaryFile(
            prefix=f"pacct_{hostname}_",
            suffix=".bin",
            delete=False,
        ) as tf:
            tf.write(raw)
            tmp_path = tf.name

        try:
            for rec in _pacct.parse_pacct(tmp_path, since=since):
                records.append(
                    ProcessAcctRecord(
                        hostname=hostname,
                        username=rec.username,
                        uid=rec.uid,
                        pid=rec.pid,
                        ppid=rec.ppid,
                        command=rec.command,
                        start_time=rec.start_time,
                        exit_time=rec.exit_time,
                        elapsed_seconds=rec.elapsed_seconds,
                        cpu_user_seconds=rec.cpu_user_seconds,
                        cpu_system_seconds=rec.cpu_system_seconds,
                        memory_avg_kb=rec.memory_avg_kb,
                        io_chars=rec.io_chars,
                        io_read_blocks=rec.io_read_blocks,
                        io_write_blocks=rec.io_write_blocks,
                        exit_code=rec.exit_code,
                        flags=rec.flags,
                    )
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return records

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store workstation metrics in database."""
        if not data:
            return

        conn = self.get_db_connection()
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workstation_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                hostname TEXT NOT NULL,
                department TEXT,
                status TEXT,
                os_version TEXT,
                cpu_model TEXT,
                uptime_seconds INTEGER,
                load_avg_1m REAL,
                load_avg_5m REAL,
                load_avg_15m REAL,
                cpu_count INTEGER,
                cpu_user_pct REAL,
                cpu_system_pct REAL,
                cpu_idle_pct REAL,
                cpu_iowait_pct REAL,
                memory_total_mb INTEGER,
                memory_used_mb INTEGER,
                memory_free_mb INTEGER,
                memory_cached_mb INTEGER,
                swap_total_mb INTEGER,
                swap_used_mb INTEGER,
                disk_total_gb REAL,
                disk_used_gb REAL,
                disk_free_gb REAL,
                disk_usage_pct REAL,
                users_logged_in INTEGER,
                process_count INTEGER,
                zombie_count INTEGER
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ws_timestamp ON workstation_state(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ws_hostname ON workstation_state(hostname)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ws_dept ON workstation_state(department)")

        # Insert records
        timestamp = datetime.now().isoformat()
        for record in data:
            cursor.execute("""
                INSERT INTO workstation_state (
                    timestamp, hostname, department, status,
                    os_version, cpu_model,
                    uptime_seconds, load_avg_1m, load_avg_5m, load_avg_15m,
                    cpu_count, cpu_user_pct, cpu_system_pct, cpu_idle_pct, cpu_iowait_pct,
                    memory_total_mb, memory_used_mb, memory_free_mb, memory_cached_mb,
                    swap_total_mb, swap_used_mb,
                    disk_total_gb, disk_used_gb, disk_free_gb, disk_usage_pct,
                    users_logged_in, process_count, zombie_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                record.get('hostname'),
                record.get('department'),
                record.get('status', 'unknown'),
                record.get('os_version', ''),
                record.get('cpu_model', ''),
                record.get('uptime_seconds', 0),
                record.get('load_avg_1m', 0),
                record.get('load_avg_5m', 0),
                record.get('load_avg_15m', 0),
                record.get('cpu_count', 0),
                record.get('cpu_user_pct', 0),
                record.get('cpu_system_pct', 0),
                record.get('cpu_idle_pct', 0),
                record.get('cpu_iowait_pct', 0),
                record.get('memory_total_mb', 0),
                record.get('memory_used_mb', 0),
                record.get('memory_free_mb', 0),
                record.get('memory_cached_mb', 0),
                record.get('swap_total_mb', 0),
                record.get('swap_used_mb', 0),
                record.get('disk_total_gb', 0),
                record.get('disk_used_gb', 0),
                record.get('disk_free_gb', 0),
                record.get('disk_usage_pct', 0),
                record.get('users_logged_in', 0),
                record.get('process_count', 0),
                record.get('zombie_count', 0),
            ))

        # ---------------------------------------------------------------
        # Per-user snapshots (workstation_user_snapshot)
        # ---------------------------------------------------------------
        # Appended to whatever workstation_state rows were just written,
        # in the same transaction. Each record may carry 0..N snapshots
        # depending on how many users were logged in at probe time.
        for record in data:
            hostname = record.get('hostname')
            for snap in record.get('user_snapshots', []):
                cursor.execute("""
                    INSERT INTO workstation_user_snapshot (
                        timestamp, hostname, username, uid, session_epoch,
                        cpu_usage_usec, cpu_user_usec, cpu_system_usec,
                        memory_current_bytes, memory_peak_bytes,
                        io_read_bytes, io_write_bytes, pids_current,
                        collector_version, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp,
                    hostname,
                    snap.get('username'),
                    snap.get('uid'),
                    snap.get('session_epoch'),
                    snap.get('cpu_usage_usec'),
                    snap.get('cpu_user_usec'),
                    snap.get('cpu_system_usec'),
                    snap.get('memory_current_bytes'),
                    snap.get('memory_peak_bytes'),
                    snap.get('io_read_bytes'),
                    snap.get('io_write_bytes'),
                    snap.get('pids_current'),
                    COLLECTOR_VERSION,
                    snap.get('source', 'cgroup_v2'),
                ))

        # ---------------------------------------------------------------
        # Mount snapshots (workstation_mount_state)
        # ---------------------------------------------------------------
        # Append-only time series, same as user_snapshots. No dedup needed
        # because we expect mount_probe to run per collection cycle.
        for record in data:
            hostname = record.get('hostname')
            for m in record.get('mount_snapshots', []):
                try:
                    cursor.execute("""
                        INSERT INTO workstation_mount_state (
                            timestamp, hostname, mountpoint, fstype, source,
                            is_mounted, is_responsive, response_ms,
                            collected_at, probe_version, collector_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        timestamp,
                        hostname,
                        m.get('mountpoint'),
                        m.get('fstype'),
                        m.get('source'),
                        m.get('is_mounted'),
                        m.get('is_responsive'),
                        m.get('response_ms'),
                        m.get('collected_at'),
                        m.get('probe_version'),
                        COLLECTOR_VERSION,
                    ))
                except sqlite3.OperationalError:
                    # workstation_mount_state table doesn't exist yet
                    # (migration v7 not applied on this DB). Skip
                    # silently; user sees other data.
                    break

        # ---------------------------------------------------------------
        # Process records from pacct (workstation_process_record)
        # ---------------------------------------------------------------
        # Idempotent via UNIQUE(hostname, pid, start_time): IntegrityError
        # means "already ingested", so we silently skip. This is why we
        # don't need to check the cursor before inserting; the DB enforces
        # uniqueness for us.
        max_exit_by_host: dict[str, int] = {}
        new_count_by_host: dict[str, int] = {}
        for record in data:
            hostname = record.get('hostname')
            for rec in record.get('process_records', []):
                try:
                    cursor.execute("""
                        INSERT INTO workstation_process_record (
                            hostname, username, uid, pid, ppid, command,
                            start_time, exit_time, elapsed_seconds,
                            cpu_user_seconds, cpu_system_seconds,
                            memory_avg_kb, io_chars,
                            io_read_blocks, io_write_blocks,
                            exit_code, flags, collector_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        hostname,
                        rec.get('username'),
                        rec.get('uid'),
                        rec.get('pid'),
                        rec.get('ppid'),
                        rec.get('command'),
                        rec.get('start_time'),
                        rec.get('exit_time'),
                        rec.get('elapsed_seconds'),
                        rec.get('cpu_user_seconds'),
                        rec.get('cpu_system_seconds'),
                        rec.get('memory_avg_kb'),
                        rec.get('io_chars'),
                        rec.get('io_read_blocks'),
                        rec.get('io_write_blocks'),
                        rec.get('exit_code'),
                        rec.get('flags'),
                        COLLECTOR_VERSION,
                    ))
                    new_count_by_host[hostname] = (
                        new_count_by_host.get(hostname, 0) + 1
                    )
                except sqlite3.IntegrityError:
                    # Duplicate — already ingested this (hostname, pid, start_time).
                    pass
                exit_time = rec.get('exit_time') or 0
                if exit_time > max_exit_by_host.get(hostname, 0):
                    max_exit_by_host[hostname] = exit_time

        # ---------------------------------------------------------------
        # Advance the ingestion cursor (workstation_pacct_cursor)
        # ---------------------------------------------------------------
        # One UPSERT per host that had any pacct activity this cycle. We
        # keep MAX(last_exit_time) so that a cycle with fewer records
        # than expected doesn't move the cursor backwards.
        now_iso = datetime.now().isoformat()
        for hostname, max_exit_time in max_exit_by_host.items():
            new_count = new_count_by_host.get(hostname, 0)
            cursor.execute("""
                INSERT INTO workstation_pacct_cursor
                    (hostname, last_exit_time, last_run_at,
                     records_ingested_total)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(hostname) DO UPDATE SET
                    last_exit_time = MAX(excluded.last_exit_time, last_exit_time),
                    last_run_at = excluded.last_run_at,
                    records_ingested_total = records_ingested_total + ?
            """, (hostname, max_exit_time, now_iso, new_count, new_count))

        conn.commit()
        conn.close()
        logger.info(f"Stored {len(data)} workstation records")

    def get_history(self, hostname: str, hours: int = 24) -> list[dict]:
        """Get workstation history for analysis."""
        conn = self.get_db_connection()
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()

        since = datetime.now().timestamp() - (hours * 3600)
        cursor.execute("""
            SELECT * FROM workstation_state
            WHERE hostname = ? AND timestamp > datetime(?, 'unixepoch')
            ORDER BY timestamp DESC
        """, (hostname, since))

        rows = cursor.fetchall()
        conn.close()
        return rows
