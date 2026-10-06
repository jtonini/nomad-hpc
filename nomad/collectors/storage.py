# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Storage/NAS Collector

Collects metrics from storage devices and NAS systems:
- ZFS pool health, scrub status, ARC stats
- NFS export status and performance
- Disk usage and quotas
- SMART drive health

Supports:
- ZFS-based storage (TrueNAS, FreeNAS, native ZFS)
- Generic NFS servers
- Local storage
"""

import logging
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry, tool_env

logger = logging.getLogger(__name__)


@dataclass
class ZFSPool:
    """ZFS pool information."""
    name: str
    health: str  # ONLINE, DEGRADED, FAULTED, OFFLINE
    size_bytes: int = 0
    allocated_bytes: int = 0
    free_bytes: int = 0
    fragmentation_pct: float = 0.0
    capacity_pct: float = 0.0
    dedup_ratio: float = 1.0

    # Scrub info
    last_scrub: str | None = None
    scrub_errors: int = 0
    scrub_in_progress: bool = False

    # Errors
    read_errors: int = 0
    write_errors: int = 0
    checksum_errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'health': self.health,
            'size_bytes': self.size_bytes,
            'allocated_bytes': self.allocated_bytes,
            'free_bytes': self.free_bytes,
            'fragmentation_pct': self.fragmentation_pct,
            'capacity_pct': self.capacity_pct,
            'dedup_ratio': self.dedup_ratio,
            'last_scrub': self.last_scrub,
            'scrub_errors': self.scrub_errors,
            'scrub_in_progress': self.scrub_in_progress,
            'read_errors': self.read_errors,
            'write_errors': self.write_errors,
            'checksum_errors': self.checksum_errors,
        }

    @property
    def is_healthy(self) -> bool:
        return (
            self.health == 'ONLINE' and
            self.read_errors == 0 and
            self.write_errors == 0 and
            self.checksum_errors == 0 and
            self.capacity_pct < 85
        )


@dataclass
class ZFSArcStats:
    """ZFS ARC (Adaptive Replacement Cache) statistics."""
    size_bytes: int = 0
    target_size_bytes: int = 0
    min_size_bytes: int = 0
    max_size_bytes: int = 0
    hits: int = 0
    misses: int = 0
    hit_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            'size_bytes': self.size_bytes,
            'target_size_bytes': self.target_size_bytes,
            'min_size_bytes': self.min_size_bytes,
            'max_size_bytes': self.max_size_bytes,
            'hits': self.hits,
            'misses': self.misses,
            'hit_ratio': self.hit_ratio,
        }


@dataclass
class NFSExport:
    """NFS export information."""
    path: str
    clients: list = field(default_factory=list)
    options: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': self.path,
            'clients': self.clients,
            'options': self.options,
        }


@dataclass
class StorageStats:
    """Complete storage system statistics."""
    hostname: str
    storage_type: str  # 'zfs', 'nfs', 'local'

    # Status
    status: str = 'online'  # online, degraded, offline, error
    last_seen: datetime | None = None

    # ZFS-specific
    pools: list = field(default_factory=list)
    arc_stats: ZFSArcStats | None = None

    # NFS-specific
    nfs_exports: list = field(default_factory=list)
    nfs_clients_connected: int = 0

    # Generic disk
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    usage_pct: float = 0.0

    # I/O stats
    read_bytes_sec: float = 0.0
    write_bytes_sec: float = 0.0
    iops_read: float = 0.0
    iops_write: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            'hostname': self.hostname,
            'storage_type': self.storage_type,
            'status': self.status,
            'pools': [p.to_dict() if hasattr(p, 'to_dict') else p for p in self.pools],
            'arc_stats': self.arc_stats.to_dict() if self.arc_stats else None,
            'nfs_exports': [e.to_dict() if hasattr(e, 'to_dict') else e for e in self.nfs_exports],
            'nfs_clients_connected': self.nfs_clients_connected,
            # No capacity measured (no ZFS pools, no paths configured) is
            # NULL, not an empty server at 0%.
            'total_bytes': self.total_bytes or None,
            'used_bytes': self.used_bytes if self.total_bytes else None,
            'free_bytes': self.free_bytes if self.total_bytes else None,
            'usage_pct': self.usage_pct if self.total_bytes else None,
            'read_bytes_sec': self.read_bytes_sec,
            'write_bytes_sec': self.write_bytes_sec,
            'iops_read': self.iops_read,
            'iops_write': self.iops_write,
        }

    @property
    def is_healthy(self) -> bool:
        """Check if storage is healthy."""
        if self.status != 'online':
            return False
        if self.usage_pct >= 90:
            return False
        for pool in self.pools:
            if hasattr(pool, 'is_healthy') and not pool.is_healthy:
                return False
        return True


_HOST = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]*$")


def _is_local(host: str | None) -> bool:
    return not host or host in ('localhost', '127.0.0.1', socket.gethostname(),
                                socket.gethostname().split('.')[0])


def run_command(cmd: str, host: str | None = None, timeout: int = 30) -> str:
    """Run one of this module's fixed commands here, or on ``host`` over SSH.

    A command that fails raises: an unreachable server used to come back as
    empty output, and was stored as a server with no storage.
    """
    from nomad.collectors.workstation import OUTPUT_MARK, _after_mark
    env = None
    remote = not _is_local(host)
    if not remote:
        argv, shell, env = cmd, True, tool_env()     # zpool, exportfs: /usr/sbin
    else:
        if not _HOST.match(host or ""):
            raise CollectionError(f"not a valid host name: {host!r}")
        # The mark first: a login banner is not the command's output.
        argv = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host,
                f"echo {OUTPUT_MARK}; {cmd}"]
        shell = False
    try:
        result = subprocess.run(argv, shell=shell, capture_output=True, text=True,
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise CollectionError(f"Command timed out: {cmd[:50]}...")
    except Exception as e:
        raise CollectionError(f"Command failed: {e}")
    if result.returncode != 0:
        raise CollectionError(f"{cmd.split()[0]} failed on {host or 'localhost'} "
                              f"(exit {result.returncode}): {result.stderr.strip()[:120]}")
    return (_after_mark(result.stdout) if remote else result.stdout).strip()


def parse_zpool_list(output: str) -> list[ZFSPool]:
    """Parse 'zpool list -Hp' output."""
    pools = []
    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) >= 10:
            # zpool list -Hp columns:
            # NAME SIZE ALLOC FREE CKPOINT EXPANDSZ FRAG CAP DEDUP HEALTH
            try:
                pool = ZFSPool(
                    name=parts[0],
                    size_bytes=int(parts[1]),
                    allocated_bytes=int(parts[2]),
                    free_bytes=int(parts[3]),
                    fragmentation_pct=float(parts[6].rstrip('%')) if parts[6] != '-' else 0,
                    capacity_pct=float(parts[7].rstrip('%')) if parts[7] != '-' else 0,
                    dedup_ratio=float(parts[8].rstrip('x')) if parts[8] != '-' else 1.0,
                    health=parts[9] if len(parts) > 9 else 'UNKNOWN',
                )
                pools.append(pool)
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse zpool line: {line}, error: {e}")
    return pools


# Pools that hold the operating system (TrueNAS SCALE, TrueNAS CORE, Ubuntu).
BOOT_POOLS = frozenset({"boot-pool", "freenas-boot", "bpool"})


def parse_zfs_roots(output: str) -> dict[str, tuple[int, int]]:
    """'zfs list -Hp -o name,used,avail -d 0': pool -> (used, avail) bytes."""
    roots = {}
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and '/' not in parts[0]:
            try:
                roots[parts[0]] = (int(parts[1]), int(parts[2]))
            except ValueError:
                continue
    return roots


def parse_zpool_status(output: str, pools: list[ZFSPool]) -> list[ZFSPool]:
    """Parse 'zpool status' for scrub info and errors."""
    current_pool = None

    for line in output.split('\n'):
        line = line.strip()

        # Find pool name
        if line.startswith('pool:'):
            pool_name = line.split(':', 1)[1].strip()
            current_pool = next((p for p in pools if p.name == pool_name), None)

        if not current_pool:
            continue

        # Parse scrub status
        if 'scrub' in line.lower():
            if 'in progress' in line.lower():
                current_pool.scrub_in_progress = True
            elif 'scrub repaired' in line.lower() or 'scrub completed' in line.lower():
                # Extract date if present
                date_match = re.search(r'(\w+ \w+ +\d+ .+)', line)
                if date_match:
                    current_pool.last_scrub = date_match.group(1)

        # Parse errors (from the pool status table)
        # Format: NAME STATE READ WRITE CKSUM
        if current_pool.name in line and 'ONLINE' in line or 'DEGRADED' in line:
            parts = line.split()
            if len(parts) >= 5:
                try:
                    current_pool.read_errors = int(parts[-3]) if parts[-3].isdigit() else 0
                    current_pool.write_errors = int(parts[-2]) if parts[-2].isdigit() else 0
                    current_pool.checksum_errors = int(parts[-1]) if parts[-1].isdigit() else 0
                except (ValueError, IndexError):
                    pass

    return pools


def parse_arc_stats(output: str) -> ZFSArcStats:
    """Parse /proc/spl/kstat/zfs/arcstats or arc_summary."""
    stats = ZFSArcStats()

    for line in output.split('\n'):
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0]
            try:
                value = int(parts[2])
                if name == 'size':
                    stats.size_bytes = value
                elif name == 'c':  # target size
                    stats.target_size_bytes = value
                elif name == 'c_min':
                    stats.min_size_bytes = value
                elif name == 'c_max':
                    stats.max_size_bytes = value
                elif name == 'hits':
                    stats.hits = value
                elif name == 'misses':
                    stats.misses = value
            except ValueError:
                pass

    # Calculate hit ratio
    total = stats.hits + stats.misses
    if total > 0:
        stats.hit_ratio = stats.hits / total

    return stats


def parse_exportfs(output: str) -> list[NFSExport]:
    """Parse 'exportfs -v' output."""
    exports = []
    for line in output.strip().split('\n'):
        if not line:
            continue
        # Format: /path client(options)
        parts = line.split()
        if len(parts) >= 2:
            path = parts[0]
            client_info = parts[1]
            client = client_info.split('(')[0]
            options = client_info.split('(')[1].rstrip(')') if '(' in client_info else ''

            # Find or create export
            export = next((e for e in exports if e.path == path), None)
            if not export:
                export = NFSExport(path=path)
                exports.append(export)
            export.clients.append(client)
            export.options = options

    return exports


@registry.register
class StorageCollector(BaseCollector):
    """
    Collector for NAS and storage system metrics.
    
    Configuration:
        storage_devices:
          - hostname: nas-01
            type: zfs
          - hostname: nfs-server
            type: nfs
            
    Collected data:
        - ZFS pool health, capacity, scrub status
        - ZFS ARC statistics
        - NFS export status
        - Disk usage
        - I/O throughput
    """

    name = "storage"
    description = "NAS and storage system metrics"
    default_interval = 300  # 5 minutes

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.storage_devices = config.get('storage_devices', [])
        logger.info(f"StorageCollector initialized with {len(self.storage_devices)} devices")

    def collect(self) -> list[dict[str, Any]]:
        """Collect metrics from all configured storage devices."""
        results = []

        if not self.storage_devices:
            self.note = "no storage_devices configured"
            return []

        for device_config in self.storage_devices:
            hostname = device_config.get('hostname')
            storage_type = device_config.get('type', 'zfs')

            if not hostname:
                continue

            try:
                stats = self._collect_storage(hostname, storage_type,
                                              device_config.get('paths') or [])
                results.append(stats.to_dict())
                logger.debug(f"Collected from {hostname}: {stats.status}")
            except CollectionError as e:
                logger.warning(f"Failed to collect from {hostname}: {e}")
                results.append({
                    'hostname': hostname,
                    'storage_type': storage_type,
                    'status': 'offline',
                })
            except Exception as e:
                logger.error(f"Unexpected error collecting from {hostname}: {e}")
                results.append({
                    'hostname': hostname,
                    'storage_type': storage_type,
                    'status': 'error',
                })

        return results

    def _collect_storage(self, hostname: str, storage_type: str,
                         paths: list | None = None) -> StorageStats:
        """Collect metrics from a single storage device."""
        stats = StorageStats(hostname=hostname, storage_type=storage_type)
        stats.last_seen = datetime.now()
        self._paths = [p for p in (paths or []) if isinstance(p, str) and p.startswith('/')
                       and "'" not in p and ' ' not in p]

        if not _is_local(hostname):
            run_command('true', hostname)        # unreachable: raises, stored as offline

        if storage_type == 'zfs':
            self._collect_zfs(stats, hostname)

        # Always try NFS exports
        self._collect_nfs(stats, hostname)

        # Get generic disk stats
        self._collect_disk(stats, hostname)

        # Determine overall status
        if stats.is_healthy:
            stats.status = 'online'
        else:
            stats.status = 'degraded'

        return stats

    def _collect_zfs(self, stats: StorageStats, hostname: str) -> None:
        """Collect ZFS-specific metrics."""
        # Get pool list
        try:
            pool_out = run_command('zpool list -Hp', hostname)
            stats.pools = parse_zpool_list(pool_out)
        except CollectionError as e:
            logger.debug(f"ZFS not available on {hostname}: {e}")
            return

        # Get detailed pool status
        try:
            status_out = run_command('zpool status', hostname)
            stats.pools = parse_zpool_status(status_out, stats.pools)
        except CollectionError:
            pass

        # Get ARC stats
        try:
            arc_out = run_command('cat /proc/spl/kstat/zfs/arcstats', hostname)
            stats.arc_stats = parse_arc_stats(arc_out)
        except CollectionError:
            pass

        # The space users have: each pool's root dataset (zfs list), which
        # for RAIDZ leaves out the parity that zpool list's SIZE counts. A
        # boot pool is the system's, not storage.
        usable = {}
        try:
            usable = parse_zfs_roots(run_command('zfs list -Hp -o name,used,avail -d 0',
                                                 hostname))
        except CollectionError:
            pass
        for pool in stats.pools:
            if pool.name in BOOT_POOLS:
                continue
            if pool.name in usable:
                used, avail = usable[pool.name]
                stats.total_bytes += used + avail
                stats.used_bytes += used
                stats.free_bytes += avail
            else:
                stats.total_bytes += pool.size_bytes
                stats.used_bytes += pool.allocated_bytes
                stats.free_bytes += pool.free_bytes

        if stats.total_bytes > 0:
            stats.usage_pct = (stats.used_bytes / stats.total_bytes) * 100

    def _collect_nfs(self, stats: StorageStats, hostname: str) -> None:
        """Collect NFS export information."""
        try:
            export_out = run_command('exportfs -v 2>/dev/null', hostname)
            if export_out:
                stats.nfs_exports = parse_exportfs(export_out)
        except CollectionError:
            pass

        # Count connected clients
        try:
            clients_out = run_command('ss -tn state established | grep :2049 | wc -l', hostname)
            stats.nfs_clients_connected = int(clients_out.strip())
        except (CollectionError, ValueError):
            pass

    def _collect_disk(self, stats: StorageStats, hostname: str) -> None:
        """Capacity from df, for the device's configured ``paths``.

        Only when ZFS gave none, and only for paths named in the config: the
        root filesystem of a file server says nothing about what it serves
        (it used to be reported as the server's storage).
        """
        if stats.total_bytes or not getattr(self, '_paths', None):
            return
        seen = set()
        for path in self._paths:
            try:
                parts = run_command(f"df -B1 -P '{path}' | tail -1", hostname).split()
                # Two paths on one filesystem count once (df's first column).
                if len(parts) >= 4 and parts[0] not in seen:
                    seen.add(parts[0])
                    stats.total_bytes += int(parts[1])
                    stats.used_bytes += int(parts[2])
                    stats.free_bytes += int(parts[3])
            except (CollectionError, ValueError):
                continue
        if stats.total_bytes:
            stats.usage_pct = stats.used_bytes / stats.total_bytes * 100

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store storage metrics in database."""
        if not data:
            return

        conn = self.get_db_connection()
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS storage_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                hostname TEXT NOT NULL,
                storage_type TEXT,
                status TEXT,
                total_bytes INTEGER,
                used_bytes INTEGER,
                free_bytes INTEGER,
                usage_pct REAL,
                read_bytes_sec REAL,
                write_bytes_sec REAL,
                iops_read REAL,
                iops_write REAL,
                nfs_clients_connected INTEGER,
                pools_json TEXT,
                arc_stats_json TEXT,
                nfs_exports_json TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_storage_timestamp ON storage_state(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_storage_hostname ON storage_state(hostname)")

        # Insert records
        import json
        timestamp = datetime.now().isoformat()
        for record in data:
            cursor.execute("""
                INSERT INTO storage_state (
                    timestamp, hostname, storage_type, status,
                    total_bytes, used_bytes, free_bytes, usage_pct,
                    read_bytes_sec, write_bytes_sec, iops_read, iops_write,
                    nfs_clients_connected, pools_json, arc_stats_json, nfs_exports_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                record.get('hostname'),
                record.get('storage_type'),
                record.get('status', 'unknown'),
                # An offline server's capacity is unknown, not zero.
                record.get('total_bytes'),
                record.get('used_bytes'),
                record.get('free_bytes'),
                record.get('usage_pct'),
                record.get('read_bytes_sec'),
                record.get('write_bytes_sec'),
                record.get('iops_read'),
                record.get('iops_write'),
                record.get('nfs_clients_connected'),
                json.dumps(record.get('pools', [])),
                json.dumps(record.get('arc_stats')),
                json.dumps(record.get('nfs_exports', [])),
            ))

        conn.commit()
        conn.close()
        logger.info(f"Stored {len(data)} storage records")

    def get_history(self, hostname: str, hours: int = 24) -> list[dict]:
        """Get storage history for analysis."""
        conn = self.get_db_connection()
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
        cursor = conn.cursor()

        since = datetime.now().timestamp() - (hours * 3600)
        cursor.execute("""
            SELECT * FROM storage_state
            WHERE hostname = ? AND timestamp > datetime(?, 'unixepoch')
            ORDER BY timestamp DESC
        """, (hostname, since))

        rows = cursor.fetchall()
        conn.close()
        return rows
