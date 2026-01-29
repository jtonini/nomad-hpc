#!/usr/bin/env python3
"""
NOMADE Interactive Session Collector
Monitors RStudio and Jupyter sessions via process inspection.
No root or API tokens required.
"""

import subprocess
import os
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


def get_process_memory(pid: int) -> Dict[str, float]:
    """Get memory info from /proc/[pid]/status."""
    try:
        with open(f'/proc/{pid}/status', 'r') as f:
            content = f.read()
        
        rss = 0
        vms = 0
        
        for line in content.split('\n'):
            if line.startswith('VmRSS:'):
                rss = int(line.split()[1])  # in kB
            elif line.startswith('VmSize:'):
                vms = int(line.split()[1])  # in kB
        
        return {
            'rss_mb': round(rss / 1024, 1),
            'vms_mb': round(vms / 1024, 1)
        }
    except:
        return {'rss_mb': 0, 'vms_mb': 0}


def get_process_start_time(pid: int) -> Optional[str]:
    """Get process start time from /proc/[pid]/stat."""
    try:
        with open('/proc/stat', 'r') as f:
            for line in f:
                if line.startswith('btime'):
                    boot_time = int(line.split()[1])
                    break
        
        with open(f'/proc/{pid}/stat', 'r') as f:
            stat = f.read().split()
            starttime_ticks = int(stat[21])
        
        clk_tck = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        
        return datetime.fromtimestamp(start_seconds).isoformat()
    except:
        return None


def calc_age_hours(start_time: Optional[str]) -> Optional[float]:
    """Calculate age in hours from start time."""
    if not start_time:
        return None
    try:
        start_dt = datetime.fromisoformat(start_time)
        age = datetime.now() - start_dt
        return round(age.total_seconds() / 3600, 1)
    except:
        return None


def collect_sessions() -> Dict[str, Any]:
    """Collect RStudio and Jupyter session info from running processes."""
    
    sessions = []
    
    try:
        ps_output = subprocess.check_output(
            ['ps', 'aux'],
            text=True,
            stderr=subprocess.DEVNULL
        )
        
        for line in ps_output.strip().split('\n')[1:]:  # Skip header
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            
            cmdline = parts[10].lower()
            
            # Detect session type
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
            
            mem_info = get_process_memory(pid)
            start_time = get_process_start_time(pid)
            age_hours = calc_age_hours(start_time)
            
            # Consider idle if CPU < 1% 
            is_idle = cpu_pct < 1.0
            
            sessions.append({
                'type': session_type,
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
        logger.warning(f"Failed to collect sessions: {e}")
    
    return build_result(sessions)


def build_result(sessions: List[Dict]) -> Dict[str, Any]:
    """Build structured result from collected sessions."""
    
    # Group by user
    users = {}
    total_memory = 0
    idle_count = 0
    
    for s in sessions:
        user = s['user']
        if user not in users:
            users[user] = {'sessions': 0, 'memory_mb': 0, 'idle': 0}
        
        users[user]['sessions'] += 1
        users[user]['memory_mb'] += s['mem_mb']
        total_memory += s['mem_mb']
        
        if s['is_idle']:
            idle_count += 1
            users[user]['idle'] += 1
    
    # Sort users by memory
    user_list = [
        {'user': u, **stats}
        for u, stats in sorted(users.items(), key=lambda x: -x[1]['memory_mb'])
    ]
    
    # Find old idle sessions (>24h)
    stale_sessions = [
        s for s in sessions
        if s['is_idle'] and s.get('age_hours', 0) and s['age_hours'] >= 24
    ]
    
    # Find memory hogs (>4GB)
    memory_hogs = [
        s for s in sessions
        if s['mem_mb'] >= 4096
    ]
    
    return {
        'timestamp': datetime.now().isoformat(),
        'summary': {
            'total_sessions': len(sessions),
            'idle_sessions': idle_count,
            'total_memory_mb': round(total_memory, 1),
            'total_memory_gb': round(total_memory / 1024, 2),
            'unique_users': len(users)
        },
        'users': user_list,
        'sessions': sorted(sessions, key=lambda x: -x['mem_mb']),
        'alerts': {
            'stale_sessions': sorted(stale_sessions, key=lambda x: -x.get('age_hours', 0)),
            'memory_hogs': sorted(memory_hogs, key=lambda x: -x['mem_mb'])
        }
    }


def collect(config: dict = None) -> Dict[str, Any]:
    """Entry point for NOMADE collector framework."""
    return collect_sessions()


def print_report(data: Dict[str, Any]):
    """Print a human-readable report."""
    summary = data['summary']
    
    print("=" * 60)
    print("         Interactive Sessions Report")
    print("=" * 60)
    print(f"  Timestamp:      {data['timestamp']}")
    print(f"  Total Sessions: {summary['total_sessions']}")
    print(f"  Idle Sessions:  {summary['idle_sessions']}")
    print(f"  Total Memory:   {summary['total_memory_gb']} GB")
    print(f"  Unique Users:   {summary['unique_users']}")
    print("-" * 60)
    
    if data['users']:
        print("\n  TOP USERS BY MEMORY:")
        print(f"  {'User':<15} {'Sessions':>10} {'Memory (MB)':>12} {'Idle':>6}")
        print(f"  {'-'*15} {'-'*10} {'-'*12} {'-'*6}")
        for u in data['users'][:10]:
            print(f"  {u['user']:<15} {u['sessions']:>10} {u['memory_mb']:>12.0f} {u['idle']:>6}")
    
    if data['alerts']['stale_sessions']:
        print(f"\n  ⚠ STALE SESSIONS (idle >24h): {len(data['alerts']['stale_sessions'])}")
        for s in data['alerts']['stale_sessions'][:5]:
            print(f"    - {s['user']}: {s['type']}, {s['age_hours']:.0f}h old, {s['mem_mb']:.0f} MB")
    
    if data['alerts']['memory_hogs']:
        print(f"\n  ⚠ MEMORY HOGS (>4GB): {len(data['alerts']['memory_hogs'])}")
        for s in data['alerts']['memory_hogs'][:5]:
            print(f"    - {s['user']}: {s['type']}, {s['mem_mb']/1024:.1f} GB")
    
    print("=" * 60)


if __name__ == '__main__':
    data = collect_sessions()
    print_report(data)
