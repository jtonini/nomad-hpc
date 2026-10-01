# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Network Performance Collector

Collects network throughput and latency metrics between hosts:
- Ping latency (min, avg, max, jitter)
- TCP throughput via iperf3 or ssh+pv
- TCP retransmits and packet loss
- Path comparison (direct wire vs switch)

Inspired by fileiotest methodology for isolating network bottlenecks.
"""

import logging
import re
import socket
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class PingStats:
    """Ping latency statistics."""
    min_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    mdev_ms: float = 0.0  # jitter
    loss_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            'min_ms': self.min_ms,
            'avg_ms': self.avg_ms,
            'max_ms': self.max_ms,
            'mdev_ms': self.mdev_ms,
            'loss_pct': self.loss_pct,
        }


@dataclass
class ThroughputStats:
    """Throughput measurement statistics."""
    bytes_transferred: int = 0
    rate_mbps: float = 0.0
    duration_sec: float = 0.0
    tcp_retrans: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'bytes_transferred': self.bytes_transferred,
            'rate_mbps': self.rate_mbps,
            'duration_sec': self.duration_sec,
            'tcp_retrans': self.tcp_retrans,
        }


@dataclass
class NetworkPerfStats:
    """Complete network performance statistics."""
    source_host: str
    dest_host: str
    path_type: str  # 'direct', 'switch', 'nfs', 'unknown'
    timestamp: datetime | None = None

    # Latency
    ping: PingStats | None = None

    # Throughput (different test conditions)
    throughput_cold: ThroughputStats | None = None  # Cold cache
    throughput_hot: ThroughputStats | None = None   # Hot cache (avg of 3)
    throughput_write: ThroughputStats | None = None # True write

    # Overall status
    status: str = 'unknown'  # 'healthy', 'degraded', 'error'

    def to_dict(self) -> dict[str, Any]:
        return {
            'source_host': self.source_host,
            'dest_host': self.dest_host,
            'path_type': self.path_type,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'ping': self.ping.to_dict() if self.ping else None,
            'throughput_cold': self.throughput_cold.to_dict() if self.throughput_cold else None,
            'throughput_hot': self.throughput_hot.to_dict() if self.throughput_hot else None,
            'throughput_write': self.throughput_write.to_dict() if self.throughput_write else None,
            'status': self.status,
        }

    @property
    def is_healthy(self) -> bool:
        """Check if network performance is healthy."""
        if not self.ping:
            return False
        # Thresholds for healthy network
        if self.ping.loss_pct > 1.0:
            return False
        if self.ping.avg_ms > 50:
            return False
        if self.ping.mdev_ms > 20:
            return False
        if self.throughput_hot and self.throughput_hot.rate_mbps < 100:
            return False
        return True


_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]*$")


def _valid_name(name: str) -> bool:
    """A host name, IP address or user name -- nothing a shell would read."""
    return bool(name) and len(name) <= 253 and bool(_NAME.match(name))


def _host_names() -> set[str]:
    """Names this host answers to (short forms), for test sources."""
    names = {"localhost"}
    for n in (socket.gethostname(), socket.getfqdn()):
        if n:
            names.add(n.split(".")[0].lower())
    return names


def _is_here(source: str) -> bool:
    """A test's source is this host: one of its names, or one of its
    addresses (an IP literal this host can bind to, loopback included)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(source)
    except ValueError:
        return source.split(".")[0].lower() in _host_names()
    if ip.is_loopback:
        return True
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((str(ip), 0))
        return True
    except OSError:
        return False


def run_command(cmd: str, timeout: int = 60) -> str:
    """Run command and return output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise CollectionError(f"Command timed out: {cmd[:50]}...")
    except Exception as e:
        raise CollectionError(f"Command failed: {e}")


def measure_ping(host: str, count: int = 10) -> PingStats:
    """Latency and loss to ``host``. No reply summary at all (unknown host,
    no route, ping missing) counts as 100% loss."""
    stats = PingStats(loss_pct=100.0)
    try:
        # -i 0.2: the shortest interval an unprivileged user may use, so ten
        # pings take two seconds, not ten. No shell: the host comes from config.
        output = subprocess.run(
            ["ping", "-c", str(int(count)), "-i", "0.2", "-W", "2", "-q", host],
            capture_output=True, text=True, timeout=int(count) + 15).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"Ping to {host} failed: {e}")
        return stats

    # "3 packets transmitted, 3 received, 0% packet loss"
    loss_match = re.search(r'(\d+(?:\.\d+)?)% packet loss', output)
    if loss_match:
        stats.loss_pct = float(loss_match.group(1))
    # "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.111 ms"
    rtt_match = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', output)
    if rtt_match:
        stats.min_ms = float(rtt_match.group(1))
        stats.avg_ms = float(rtt_match.group(2))
        stats.max_ms = float(rtt_match.group(3))
        stats.mdev_ms = float(rtt_match.group(4))
    return stats


def get_tcp_retrans() -> int:
    """Get current TCP retransmit count from nstat."""
    try:
        output = run_command("nstat -az TcpRetransSegs 2>/dev/null | grep TcpRetransSegs")
        parts = output.split()
        if len(parts) >= 2:
            return int(parts[1])
    except:
        pass
    return 0


def measure_throughput_iperf(host: str, duration: int = 10) -> ThroughputStats | None:
    """Measure throughput using iperf3 (if available)."""
    stats = ThroughputStats()

    try:
        import shutil as _shutil
        if not _shutil.which("iperf3"):
            return None

        retrans_before = get_tcp_retrans()

        # Run iperf3 client (needs `iperf3 -s` running on the destination)
        proc = subprocess.run(
            ["iperf3", "-c", host, "-t", str(int(duration)), "-J"],
            capture_output=True, text=True, timeout=int(duration) + 30)
        output = proc.stdout

        retrans_after = get_tcp_retrans()
        stats.tcp_retrans = max(0, retrans_after - retrans_before)

        # Parse JSON output. With no `iperf3 -s` at the far end iperf3
        # still prints JSON -- an "error" and an empty "end" -- which used
        # to be stored as 0 Mbps. That is no measurement: fall back.
        import json
        data = json.loads(output)
        sent = (data.get('end') or {}).get('sum_sent')
        if proc.returncode != 0 or data.get('error') or not sent:
            logger.debug(f"iperf3 to {host} gave no result: {data.get('error')}")
            return None
        stats.bytes_transferred = sent.get('bytes', 0)
        stats.rate_mbps = sent.get('bits_per_second', 0) / 1_000_000
        stats.duration_sec = sent.get('seconds', duration)
        return stats

    except Exception as e:
        logger.debug(f"iperf3 not available or failed: {e}")
        return None


def measure_throughput_ssh(host: str, user: str = None, size_mb: int = 50) -> ThroughputStats | None:
    """Throughput of an SSH copy of ``size_mb`` of zeros, timed end to end.

    A fallback when iperf3 isn't there. It measures what an SSH copy gets,
    which encryption can limit below what the network carries. (It used to
    report bytes / 10 s -- an assumed duration, not a measurement.)
    """
    import time as _time
    stats = ThroughputStats()
    dest = f"{user}@{host}" if user else host
    if not _valid_name(dest.replace("@", "", 1)):
        return None
    try:
        retrans_before = get_tcp_retrans()
        t0 = _time.monotonic()
        result = subprocess.run(
            f"dd if=/dev/zero bs=1M count={int(size_mb)} 2>/dev/null | "
            f"ssh -T -o BatchMode=yes -o ConnectTimeout=10 {dest} 'cat > /dev/null'",
            shell=True, capture_output=True, text=True, timeout=300)
        elapsed = _time.monotonic() - t0
        if result.returncode != 0 or elapsed <= 0:
            return None
        stats.tcp_retrans = max(0, get_tcp_retrans() - retrans_before)
        stats.bytes_transferred = int(size_mb) * 1024 * 1024
        stats.duration_sec = elapsed
        stats.rate_mbps = stats.bytes_transferred * 8 / elapsed / 1_000_000
        return stats
    except Exception as e:
        logger.debug(f"SSH throughput test failed: {e}")
        return None


def generate_random_files(directory: str, count: int = 3, size_mb: int = 10) -> list[str]:
    """Generate random test files (like fileiotest randomfiles.py)."""
    import os
    import random as rand
    import string

    files = []
    chars = string.ascii_letters + string.digits

    for i in range(count):
        filepath = os.path.join(directory, f"nomad_nettest_{i}.iotest")
        with open(filepath, 'w') as f:
            # Write random printable characters (not compressible)
            for _ in range(size_mb):
                chunk = ''.join(rand.choices(chars, k=1024*1024))
                f.write(chunk)
        files.append(filepath)

    return files


def flush_caches(host: str = None, user: str = None) -> bool:
    """Flush page caches (requires root/sudo)."""
    try:
        if host and host not in ('localhost', '127.0.0.1'):
            dest = f"{user}@{host}" if user else host
            run_command(f"ssh -o BatchMode=yes {dest} 'sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null 2>&1'", timeout=30)
        else:
            run_command("sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null 2>&1", timeout=30)
        return True
    except:
        return False


def lock_in_cache(files: list[str]) -> bool:
    """Lock files in page cache using vmtouch."""
    try:
        for f in files:
            run_command(f"vmtouch -t {f}", timeout=60)
        return True
    except:
        # vmtouch not available, try cat to /dev/null
        try:
            for f in files:
                run_command(f"cat {f} > /dev/null", timeout=60)
            return True
        except:
            return False


def measure_throughput_full(
    host: str,
    user: str = None,
    num_files: int = 3,
    file_size_mb: int = 10,
    work_dir: str = "/tmp"
) -> dict:
    """
    Full fileiotest-style throughput measurement.
    
    Measures 3 scenarios:
    - Cold cache: sender reads from disk, receiver discards
    - Hot cache: sender reads from RAM (3 runs), receiver discards
    - True write: sender reads from RAM, receiver writes to disk
    
    Returns dict with all phases and TCP stats.
    """
    import tempfile
    import time

    dest = f"{user}@{host}" if user else host
    results = {
        'cold_cache': None,
        'hot_cache_runs': [],
        'hot_cache_avg': None,
        'true_write': None,
        'tcp_retrans_total': 0,
        'error': None,
    }

    # Check pv is available
    try:
        run_command("which pv")
    except:
        results['error'] = "pv not installed"
        return results

    # Generate random test files
    test_dir = tempfile.mkdtemp(prefix="nomad_nettest_")
    try:
        files = generate_random_files(test_dir, num_files, file_size_mb)
        total_bytes = num_files * file_size_mb * 1024 * 1024

        # Record initial TCP stats
        tcp_start = get_tcp_retrans()

        # === COLD CACHE RUN ===
        flush_caches()  # Local
        flush_caches(host, user)  # Remote

        start_time = time.time()
        try:
            cmd = f"cat {' '.join(files)} | pv -f -n 2>&1 | ssh -T -o BatchMode=yes -o Compression=no {dest} 'cat > /dev/null'"
            output = run_command(cmd, timeout=300)
            duration = time.time() - start_time
            rate_mbps = (total_bytes * 8) / duration / 1_000_000
            results['cold_cache'] = ThroughputStats(
                bytes_transferred=total_bytes,
                rate_mbps=rate_mbps,
                duration_sec=duration,
            )
        except Exception as e:
            logger.warning(f"Cold cache test failed: {e}")

        # === HOT CACHE RUNS (3x) ===
        lock_in_cache(files)

        for run in range(3):
            start_time = time.time()
            try:
                cmd = f"cat {' '.join(files)} | pv -f -n 2>&1 | ssh -T -o BatchMode=yes -o Compression=no {dest} 'cat > /dev/null'"
                output = run_command(cmd, timeout=300)
                duration = time.time() - start_time
                rate_mbps = (total_bytes * 8) / duration / 1_000_000
                results['hot_cache_runs'].append(ThroughputStats(
                    bytes_transferred=total_bytes,
                    rate_mbps=rate_mbps,
                    duration_sec=duration,
                ))
            except Exception as e:
                logger.warning(f"Hot cache run {run+1} failed: {e}")

            if run < 2:
                time.sleep(5)  # Brief pause between runs

        # Calculate hot cache average
        if results['hot_cache_runs']:
            avg_rate = sum(r.rate_mbps for r in results['hot_cache_runs']) / len(results['hot_cache_runs'])
            avg_bytes = sum(r.bytes_transferred for r in results['hot_cache_runs']) / len(results['hot_cache_runs'])
            results['hot_cache_avg'] = ThroughputStats(
                bytes_transferred=int(avg_bytes),
                rate_mbps=avg_rate,
                duration_sec=sum(r.duration_sec for r in results['hot_cache_runs']) / len(results['hot_cache_runs']),
            )

        # === TRUE WRITE RUN ===
        flush_caches()
        flush_caches(host, user)
        lock_in_cache(files)

        start_time = time.time()
        try:
            # Write to actual file on remote
            cmd = f"cat {' '.join(files)} | pv -f -n 2>&1 | ssh -T -o BatchMode=yes -o Compression=no {dest} 'cat > /tmp/nomad_nettest_recv.tmp && rm -f /tmp/nomad_nettest_recv.tmp'"
            output = run_command(cmd, timeout=300)
            duration = time.time() - start_time
            rate_mbps = (total_bytes * 8) / duration / 1_000_000
            results['true_write'] = ThroughputStats(
                bytes_transferred=total_bytes,
                rate_mbps=rate_mbps,
                duration_sec=duration,
            )
        except Exception as e:
            logger.warning(f"True write test failed: {e}")

        # Record final TCP stats
        tcp_end = get_tcp_retrans()
        results['tcp_retrans_total'] = max(0, tcp_end - tcp_start)

    finally:
        # Cleanup test files
        import shutil
        shutil.rmtree(test_dir, ignore_errors=True)

    return results




@registry.register
class NetworkPerfCollector(BaseCollector):
    """
    Collector for network performance metrics.
    
    Configuration:
        network_tests:
          - source: localhost
            dest: storage-server
            path_type: switch
          - source: localhost  
            dest: 10.0.0.1
            path_type: direct
            
    Collected data:
        - Ping latency (min, avg, max, jitter)
        - Throughput via iperf3 or ssh+pv
        - TCP retransmits
        - Packet loss
    """

    name = "network_perf"
    description = "Network throughput and latency metrics"
    default_interval = 3600  # 1 hour (throughput tests can be heavy)

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.network_tests = config.get('network_tests', []) or []
        self.ping_count = config.get('ping_count', 10)
        # Ping (latency, loss) every run; throughput only when asked for,
        # at most once per throughput_interval per path: it puts real
        # traffic on the wire and needs iperf3 -s (or SSH) at the far end.
        self.throughput = bool(config.get('throughput', False))
        self.throughput_interval = int(config.get('throughput_interval', 3600))
        self.iperf_duration = config.get('iperf_duration', 10)
        # Full fileiotest-style options
        self.full_test = config.get('full_test', False)
        self.num_files = config.get('num_files', 3)
        self.file_size_mb = config.get('file_size_mb', 10)
        logger.info(f"NetworkPerfCollector initialized with {len(self.network_tests)} test paths "
                    f"(throughput={self.throughput}, full_test={self.full_test})")

    def collect(self) -> list[dict[str, Any]]:
        """Measure every configured path that starts on this host."""
        if not self.network_tests:
            self.note = "no network_tests configured"
            return []
        import shutil
        if not shutil.which("ping"):
            # Without ping every path would read as unreachable.
            self.note = "ping not installed"
            return []
        results, skipped = [], []

        for test_config in self.network_tests:
            dest = str(test_config.get('dest') or '').strip()
            source = str(test_config.get('source') or socket.gethostname()).strip()
            path_type = test_config.get('path_type', 'unknown')
            user = test_config.get('user')

            if not dest or not _valid_name(dest) or (user and not _valid_name(user)):
                skipped.append(f"{dest or '?'}: not a valid host name")
                continue
            # Tests run from this host. A path that starts elsewhere is
            # measured by the nomad on that host, not mislabelled here.
            if not _is_here(source):
                skipped.append(f"{source}->{dest}: starts on another host")
                continue

            try:
                stats = self._collect_path(source, dest, path_type, user)
                results.append(stats.to_dict())
                logger.debug(f"Collected network stats {source}->{dest}: {stats.status}")
            except Exception as e:
                logger.error(f"Failed to collect network stats for {source}->{dest}: {e}")
                results.append({
                    'source_host': source,
                    'dest_host': dest,
                    'path_type': path_type,
                    'status': 'error',
                    'timestamp': datetime.now().isoformat(),
                })

        if skipped:
            self.note = "skipped " + "; ".join(skipped)
        return results

    def _throughput_due(self, source: str, dest: str) -> bool:
        """No throughput reading for this path within throughput_interval.

        Read from the table, so it holds when cron starts a new process
        for every run.
        """
        try:
            with self.get_db_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(timestamp) FROM network_perf WHERE source_host = ? "
                    "AND dest_host = ? AND throughput_mbps IS NOT NULL", (source, dest)).fetchone()
        except sqlite3.Error:
            return True
        if not row or not row[0]:
            return True
        try:
            last = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return True
        return (datetime.now() - last).total_seconds() >= self.throughput_interval * 0.95

    def _collect_path(self, source: str, dest: str, path_type: str, user: str = None) -> NetworkPerfStats:
        """Collect metrics for a single network path."""
        stats = NetworkPerfStats(
            source_host=source,
            dest_host=dest,
            path_type=path_type,
            timestamp=datetime.now(),
        )

        stats.ping = measure_ping(dest, self.ping_count)

        if self.throughput and stats.ping.loss_pct < 100 and self._throughput_due(source, dest):
            if self.full_test:
                full_results = measure_throughput_full(
                    dest, user,
                    num_files=self.num_files,
                    file_size_mb=self.file_size_mb,
                )
                if full_results.get('cold_cache'):
                    stats.throughput_cold = full_results['cold_cache']
                if full_results.get('hot_cache_avg'):
                    stats.throughput_hot = full_results['hot_cache_avg']
                if full_results.get('true_write'):
                    stats.throughput_write = full_results['true_write']
                if full_results.get('tcp_retrans_total'):
                    if stats.throughput_hot:
                        stats.throughput_hot.tcp_retrans = full_results['tcp_retrans_total']
            else:
                stats.throughput_hot = measure_throughput_iperf(dest, self.iperf_duration)
                if not stats.throughput_hot:
                    stats.throughput_hot = measure_throughput_ssh(dest, user)

        if stats.ping.loss_pct >= 100:
            stats.status = 'unreachable'
        elif stats.is_healthy:
            stats.status = 'healthy'
        else:
            stats.status = 'degraded'
        return stats

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store network performance metrics in database."""
        if not data:
            return

        conn = self.get_db_connection()
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS network_perf (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                source_host TEXT NOT NULL,
                dest_host TEXT NOT NULL,
                path_type TEXT,
                status TEXT,
                ping_min_ms REAL,
                ping_avg_ms REAL,
                ping_max_ms REAL,
                ping_mdev_ms REAL,
                ping_loss_pct REAL,
                throughput_mbps REAL,
                bytes_transferred INTEGER,
                tcp_retrans INTEGER
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_netperf_timestamp ON network_perf(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_netperf_path ON network_perf(source_host, dest_host)")

        # Insert records
        timestamp = datetime.now().isoformat()
        for record in data:
            ping = record.get('ping') or {}
            throughput = record.get('throughput_hot') or record.get('throughput_cold') or {}

            cursor.execute("""
                INSERT INTO network_perf (
                    timestamp, source_host, dest_host, path_type, status,
                    ping_min_ms, ping_avg_ms, ping_max_ms, ping_mdev_ms, ping_loss_pct,
                    throughput_mbps, bytes_transferred, tcp_retrans
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.get('timestamp', timestamp),
                record.get('source_host'),
                record.get('dest_host'),
                record.get('path_type'),
                record.get('status'),
                ping.get('min_ms'),
                ping.get('avg_ms'),
                ping.get('max_ms'),
                ping.get('mdev_ms'),
                ping.get('loss_pct'),
                throughput.get('rate_mbps'),
                throughput.get('bytes_transferred'),
                throughput.get('tcp_retrans'),
            ))

        conn.commit()
        conn.close()
        logger.info(f"Stored {len(data)} network performance records")

    def get_history(self, source: str = None, dest: str = None, hours: int = 24) -> list[dict]:
        """Get network performance history for analysis."""
        conn = self.get_db_connection()
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()

        since = datetime.now().timestamp() - (hours * 3600)

        if source and dest:
            cursor.execute("""
                SELECT * FROM network_perf
                WHERE source_host = ? AND dest_host = ? AND timestamp > datetime(?, 'unixepoch')
                ORDER BY timestamp DESC
            """, (source, dest, since))
        elif source:
            cursor.execute("""
                SELECT * FROM network_perf
                WHERE source_host = ? AND timestamp > datetime(?, 'unixepoch')
                ORDER BY timestamp DESC
            """, (source, since))
        else:
            cursor.execute("""
                SELECT * FROM network_perf
                WHERE timestamp > datetime(?, 'unixepoch')
                ORDER BY timestamp DESC
            """, (since,))

        rows = cursor.fetchall()
        conn.close()
        return rows
