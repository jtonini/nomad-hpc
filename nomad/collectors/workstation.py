# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations
"""
NØMAD Workstation Collector

Collects system metrics from departmental workstations:
- CPU and memory utilization
- Disk usage
- Active user sessions
- Process information
- Department/group metadata

Supports both local and SSH-based remote collection.
"""

import logging
import subprocess
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    """Active user session information."""
    username: str
    terminal: str
    login_time: str
    idle_time: str
    remote_host: Optional[str] = None
    
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
    department: Optional[str] = None
    
    # System info
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
    last_seen: Optional[datetime] = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            'hostname': self.hostname,
            'department': self.department,
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


def run_command(cmd: str, host: Optional[str] = None, timeout: int = 30) -> str:
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
    
    def _collect_workstation(self, hostname: str, department: Optional[str]) -> WorkstationStats:
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
        
        return stats
    
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
                    uptime_seconds, load_avg_1m, load_avg_5m, load_avg_15m,
                    cpu_count, cpu_user_pct, cpu_system_pct, cpu_idle_pct, cpu_iowait_pct,
                    memory_total_mb, memory_used_mb, memory_free_mb, memory_cached_mb,
                    swap_total_mb, swap_used_mb,
                    disk_total_gb, disk_used_gb, disk_free_gb, disk_usage_pct,
                    users_logged_in, process_count, zombie_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                record.get('hostname'),
                record.get('department'),
                record.get('status', 'unknown'),
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
