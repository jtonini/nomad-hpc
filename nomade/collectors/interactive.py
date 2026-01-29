"""
NOMADE Interactive Session Collector
Monitors RStudio and Jupyter sessions via process inspection.
No root or API tokens required.
"""

import subprocess
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from nomade.collectors.base import BaseCollector, registry

logger = logging.getLogger(__name__)

# Default thresholds (can be overridden via config)
DEFAULT_IDLE_SESSION_HOURS = 24
DEFAULT_MEMORY_HOG_MB = 4096
DEFAULT_MAX_IDLE_SESSIONS = 5


@registry.register
class InteractiveCollector(BaseCollector):
    """Collector for interactive computing sessions (RStudio, Jupyter)."""

    name = "interactive"
    description = "RStudio and Jupyter session monitor"
    default_interval = 300  # 5 minutes

    def __init__(self, config: Dict[str, Any], db_path: Path):
        super().__init__(config, db_path)
        
        self.idle_session_hours = config.get('idle_session_hours', DEFAULT_IDLE_SESSION_HOURS)
        self.memory_hog_mb = config.get('memory_hog_mb', DEFAULT_MEMORY_HOG_MB)
        self.max_idle_sessions = config.get('max_idle_sessions', DEFAULT_MAX_IDLE_SESSIONS)
        self.server_id = config.get('server_id', 'local')

    def collect(self) -> List[Dict[str, Any]]:
        """Collect RStudio and Jupyter session info from running processes."""
        sessions = []

        try:
            ps_output = subprocess.check_output(
                ['ps', 'aux'],
                universal_newlines=True,
                stderr=subprocess.DEVNULL
            )

            for line in ps_output.strip().split('\n')[1:]:
                parts = line.split(None, 10)
                if len(parts) < 11:
                    continue

                cmdline = parts[10].lower()

                if 'rsession' in cmdline:
                    session_type = 'RStudio'
                elif 'ipykernel' in cmdline:
                    session_type = 'Jupyter (Python)'
                elif 'irkernel' in cmdline:
                    session_type = 'Jupyter (R)'
                elif 'jupyter-lab' in cmdline or 'jupyter-notebook' in cmdline:
                    session_type = 'Jupyter Server'
                else:
                    continue

                user = parts[0]
                pid = int(parts[1])
                cpu_pct = float(parts[2])
                mem_pct = float(parts[3])

                mem_info = self._get_process_memory(pid)
                start_time = self._get_process_start_time(pid)
                age_hours = self._calc_age_hours(start_time)
                is_idle = cpu_pct < 1.0

                sessions.append({
                    'timestamp': datetime.now().isoformat(),
                    'server_id': self.server_id,
                    'session_type': session_type,
                    'user': user,
                    'pid': pid,
                    'cpu_percent': cpu_pct,
                    'mem_percent': mem_pct,
                    'mem_mb': mem_info['rss_mb'],
                    'mem_virtual_mb': mem_info['vms_mb'],
                    'start_time': start_time,
                    'age_hours': age_hours,
                    'is_idle': is_idle
                })

        except Exception as e:
            logger.warning("Failed to collect sessions: {}".format(e))
            raise

        return sessions

    def store(self, data: List[Dict[str, Any]]) -> None:
        """Store collected session data in the database."""
        if not data:
            return

        with self.get_db_connection() as conn:
            for session in data:
                conn.execute(
                    """
                    INSERT INTO interactive_sessions
                    (timestamp, server_id, user, session_type, pid, cpu_percent,
                     mem_percent, mem_mb, mem_virtual_mb, start_time, age_hours, is_idle)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session['timestamp'],
                        session['server_id'],
                        session['user'],
                        session['session_type'],
                        session['pid'],
                        session['cpu_percent'],
                        session['mem_percent'],
                        session['mem_mb'],
                        session['mem_virtual_mb'],
                        session['start_time'],
                        session['age_hours'],
                        session['is_idle']
                    )
                )

            summary = self._build_summary(data)
            conn.execute(
                """
                INSERT INTO interactive_summary
                (timestamp, server_id, total_sessions, idle_sessions, total_memory_mb,
                 unique_users, rstudio_sessions, jupyter_python_sessions, jupyter_r_sessions,
                 stale_sessions, memory_hog_sessions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    self.server_id,
                    summary['total_sessions'],
                    summary['idle_sessions'],
                    summary['total_memory_mb'],
                    summary['unique_users'],
                    summary['rstudio_sessions'],
                    summary['jupyter_python_sessions'],
                    summary['jupyter_r_sessions'],
                    summary['stale_sessions'],
                    summary['memory_hog_sessions']
                )
            )

            conn.commit()

    def _build_summary(self, sessions: List[Dict]) -> Dict[str, Any]:
        """Build summary statistics from collected sessions."""
        users = set()
        total_memory = 0
        idle_count = 0
        rstudio_count = 0
        jupyter_python_count = 0
        jupyter_r_count = 0
        stale_count = 0
        memory_hog_count = 0

        for s in sessions:
            users.add(s['user'])
            total_memory += s['mem_mb']

            if s['is_idle']:
                idle_count += 1

            if s['session_type'] == 'RStudio':
                rstudio_count += 1
            elif s['session_type'] == 'Jupyter (Python)':
                jupyter_python_count += 1
            elif s['session_type'] == 'Jupyter (R)':
                jupyter_r_count += 1

            age = s.get('age_hours', 0) or 0
            if s['is_idle'] and age >= self.idle_session_hours:
                stale_count += 1

            if s['mem_mb'] >= self.memory_hog_mb:
                memory_hog_count += 1

        return {
            'total_sessions': len(sessions),
            'idle_sessions': idle_count,
            'total_memory_mb': round(total_memory, 1),
            'unique_users': len(users),
            'rstudio_sessions': rstudio_count,
            'jupyter_python_sessions': jupyter_python_count,
            'jupyter_r_sessions': jupyter_r_count,
            'stale_sessions': stale_count,
            'memory_hog_sessions': memory_hog_count
        }

    def _get_process_memory(self, pid: int) -> Dict[str, float]:
        """Get memory info from /proc/[pid]/status."""
        try:
            with open('/proc/{}/status'.format(pid), 'r') as f:
                content = f.read()

            rss = 0
            vms = 0

            for line in content.split('\n'):
                if line.startswith('VmRSS:'):
                    rss = int(line.split()[1])
                elif line.startswith('VmSize:'):
                    vms = int(line.split()[1])

            return {
                'rss_mb': round(rss / 1024, 1),
                'vms_mb': round(vms / 1024, 1)
            }
        except:
            return {'rss_mb': 0, 'vms_mb': 0}

    def _get_process_start_time(self, pid: int) -> Optional[str]:
        """Get process start time from /proc/[pid]/stat."""
        try:
            with open('/proc/stat', 'r') as f:
                for line in f:
                    if line.startswith('btime'):
                        boot_time = int(line.split()[1])
                        break

            with open('/proc/{}/stat'.format(pid), 'r') as f:
                stat = f.read().split()
                starttime_ticks = int(stat[21])

            clk_tck = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
            start_seconds = boot_time + (starttime_ticks / clk_tck)

            return datetime.fromtimestamp(start_seconds).isoformat()
        except:
            return None

    def _calc_age_hours(self, start_time: Optional[str]) -> Optional[float]:
        """Calculate age in hours from start time."""
        if not start_time:
            return None
        try:
            start_dt = datetime.fromisoformat(start_time)
            age = datetime.now() - start_dt
            return round(age.total_seconds() / 3600, 1)
        except:
            return None

    def get_report(self) -> Dict[str, Any]:
        """Get a full report with alerts (for CLI/dashboard use)."""
        sessions = self.collect()
        summary = self._build_summary(sessions)

        users = {}
        for s in sessions:
            user = s['user']
            if user not in users:
                users[user] = {
                    'sessions': 0,
                    'memory_mb': 0,
                    'idle': 0,
                    'rstudio': 0,
                    'jupyter': 0
                }

            users[user]['sessions'] += 1
            users[user]['memory_mb'] += s['mem_mb']

            if s['session_type'] == 'RStudio':
                users[user]['rstudio'] += 1
            else:
                users[user]['jupyter'] += 1

            if s['is_idle']:
                users[user]['idle'] += 1

        user_list = [
            {'user': u, **stats}
            for u, stats in sorted(users.items(), key=lambda x: -x[1]['memory_mb'])
        ]

        stale_sessions = [
            s for s in sessions
            if s['is_idle'] and s.get('age_hours', 0) and s['age_hours'] >= self.idle_session_hours
        ]

        memory_hogs = [
            s for s in sessions
            if s['mem_mb'] >= self.memory_hog_mb
        ]

        idle_session_hogs = [
            u for u in user_list
            if u['idle'] > self.max_idle_sessions
        ]

        by_type = {
            'RStudio': {'total': 0, 'idle': 0, 'memory_mb': 0},
            'Jupyter (Python)': {'total': 0, 'idle': 0, 'memory_mb': 0},
            'Jupyter (R)': {'total': 0, 'idle': 0, 'memory_mb': 0},
            'Jupyter Server': {'total': 0, 'idle': 0, 'memory_mb': 0}
        }

        for s in sessions:
            stype = s['session_type']
            if stype in by_type:
                by_type[stype]['total'] += 1
                by_type[stype]['memory_mb'] += s['mem_mb']
                if s['is_idle']:
                    by_type[stype]['idle'] += 1

        return {
            'timestamp': datetime.now().isoformat(),
            'server_id': self.server_id,
            'summary': {
                'total_sessions': summary['total_sessions'],
                'idle_sessions': summary['idle_sessions'],
                'total_memory_mb': summary['total_memory_mb'],
                'total_memory_gb': round(summary['total_memory_mb'] / 1024, 2),
                'unique_users': summary['unique_users']
            },
            'by_type': by_type,
            'users': user_list,
            'sessions': sorted(sessions, key=lambda x: -x['mem_mb']),
            'alerts': {
                'stale_sessions': sorted(stale_sessions, key=lambda x: -x.get('age_hours', 0)),
                'memory_hogs': sorted(memory_hogs, key=lambda x: -x['mem_mb']),
                'idle_session_hogs': idle_session_hogs
            },
            'thresholds': {
                'idle_session_hours': self.idle_session_hours,
                'memory_hog_mb': self.memory_hog_mb,
                'max_idle_sessions': self.max_idle_sessions
            }
        }


def print_report(data: Dict[str, Any]):
    """Print a human-readable report."""
    summary = data['summary']
    by_type = data['by_type']
    thresholds = data['thresholds']

    print("=" * 70)
    print("              Interactive Sessions Report")
    print("=" * 70)
    print("  Timestamp:      {}".format(data['timestamp']))
    print("  Server:         {}".format(data['server_id']))
    print("  Total Sessions: {}".format(summary['total_sessions']))
    print("  Idle Sessions:  {}".format(summary['idle_sessions']))
    print("  Total Memory:   {} GB".format(summary['total_memory_gb']))
    print("  Unique Users:   {}".format(summary['unique_users']))
    print("-" * 70)

    print("\n  SESSIONS BY TYPE:")
    print("  {:<20} {:>8} {:>8} {:>12}".format('Type', 'Total', 'Idle', 'Memory (MB)'))
    print("  {:<20} {:>8} {:>8} {:>12}".format('-'*20, '-'*8, '-'*8, '-'*12))
    for stype, stats in by_type.items():
        if stats['total'] > 0:
            print("  {:<20} {:>8} {:>8} {:>12.0f}".format(
                stype, stats['total'], stats['idle'], stats['memory_mb']
            ))

    if data['users']:
        print("\n  TOP USERS BY MEMORY:")
        print("  {:<12} {:>8} {:>8} {:>8} {:>10} {:>6}".format(
            'User', 'Sessions', 'RStudio', 'Jupyter', 'Mem (MB)', 'Idle'))
        print("  {:<12} {:>8} {:>8} {:>8} {:>10} {:>6}".format(
            '-'*12, '-'*8, '-'*8, '-'*8, '-'*10, '-'*6))
        for u in data['users'][:10]:
            print("  {:<12} {:>8} {:>8} {:>8} {:>10.0f} {:>6}".format(
                u['user'][:12], u['sessions'], u['rstudio'], u['jupyter'],
                u['memory_mb'], u['idle']
            ))

    alerts = data['alerts']

    if alerts['idle_session_hogs']:
        print("\n  [!] USERS WITH >{} IDLE SESSIONS:".format(thresholds['max_idle_sessions']))
        for u in alerts['idle_session_hogs']:
            print("    - {}: {} idle sessions ({} RStudio, {} Jupyter), {:.0f} MB".format(
                u['user'], u['idle'], u['rstudio'], u['jupyter'], u['memory_mb']
            ))

    if alerts['stale_sessions']:
        print("\n  [!] STALE SESSIONS (idle >{}h): {}".format(
            thresholds['idle_session_hours'], len(alerts['stale_sessions'])))
        for s in alerts['stale_sessions'][:5]:
            print("    - {}: {}, {:.0f}h old, {:.0f} MB".format(
                s['user'], s['session_type'], s['age_hours'], s['mem_mb']))

    if alerts['memory_hogs']:
        print("\n  [!] MEMORY HOGS (>{}GB): {}".format(
            thresholds['memory_hog_mb']/1024, len(alerts['memory_hogs'])))
        for s in alerts['memory_hogs'][:5]:
            print("    - {}: {}, {:.1f} GB".format(
                s['user'], s['session_type'], s['mem_mb']/1024))

    print("=" * 70)


if __name__ == '__main__':
    class StandaloneCollector(InteractiveCollector):
        """Standalone version that does not require database."""
        
        def __init__(self, config=None):
            self.config = config or {}
            self.idle_session_hours = self.config.get('idle_session_hours', DEFAULT_IDLE_SESSION_HOURS)
            self.memory_hog_mb = self.config.get('memory_hog_mb', DEFAULT_MEMORY_HOG_MB)
            self.max_idle_sessions = self.config.get('max_idle_sessions', DEFAULT_MAX_IDLE_SESSIONS)
            self.server_id = self.config.get('server_id', 'local')
    
    collector = StandaloneCollector()
    report = collector.get_report()
    print_report(report)
