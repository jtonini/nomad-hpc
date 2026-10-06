"""
Workstation Diagnostics for departmental machines.

Provides detailed analysis of workstation health and issues:
- System resource utilization (CPU, memory, disk)
- User session analysis
- Process issues (zombies, runaway processes)
- Department context
- Trend analysis for resource usage

Integrates with:
- analysis/derivatives.py for trend detection
- alerts/thresholds.py for threshold checking
- collectors/workstation.py data
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad.diag.workstation_prereqs import (
    WorkstationPrereqDiagnostic,
    check_workstation_prerequisites,
    format_prerequisite_checks,
)

# Import existing analysis tools
try:
    from nomad.analysis.derivatives import AlertLevel, DerivativeAnalyzer
    HAS_DERIVATIVES = True
except ImportError:
    HAS_DERIVATIVES = False

logger = logging.getLogger(__name__)


@dataclass
class WorkstationDiagnostic:
    """Container for workstation diagnostic information."""
    hostname: str
    department: str | None
    current_status: str
    last_seen: datetime | None

    # Current metrics
    cpu_load: float = 0.0
    cpu_count: int = 1
    memory_used_pct: float = 0.0
    memory_total_mb: int = 0
    disk_used_pct: float = 0.0
    swap_used_mb: int = 0

    # Users and processes
    users_logged_in: int = 0
    active_sessions: list = field(default_factory=list)
    process_count: int = 0
    zombie_count: int = 0

    # History and trends
    resource_history: dict = field(default_factory=dict)
    trends: dict = field(default_factory=dict)

    # Analysis results
    potential_causes: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)

    # Collection prerequisites diagnostic (filled when check_prereqs=True)
    prereqs: WorkstationPrereqDiagnostic | None = None


def get_workstation_state(db_path: str, hostname: str) -> dict | None:
    """Get current workstation state from database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT * FROM workstation_state 
            WHERE hostname = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (hostname,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error getting workstation state: {e}")
        return None


def get_state_history(db_path: str, hostname: str, hours: int = 24) -> list:
    """The workstation's readings over the last ``hours``, oldest first, each
    in the terms of reading()."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT * FROM workstation_state
            WHERE hostname = ? AND timestamp > ?
            ORDER BY timestamp
        """, (hostname, since)).fetchall()
        conn.close()
        return [reading(dict(r)) for r in rows]
    except Exception as e:
        logger.error(f"Error getting state history: {e}")
        return []


def _number(row: dict, *keys) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def reading(row: dict | None) -> dict:
    """One workstation_state row in the terms this module judges by.

    The collector's columns are load_avg_1m, memory_used_mb of
    memory_total_mb, and disk_usage_pct of disk_total_gb. This module used to
    read load_1m, memory_percent and disk_percent, which no table has, and so
    showed every workstation at 0% load, memory and disk with nothing to flag.

    A figure the reading doesn't have is None, not 0: when a command fails the
    collector stores zeros (a total of 0 MB or 0 GB, a load of 0 with no
    uptime), and those are not measurements.
    """
    row = row or {}

    load = _number(row, 'load_avg_1m', 'load_1m')
    if load == 0 and 'uptime_seconds' in row and not row.get('uptime_seconds'):
        load = None

    mem_total = _number(row, 'memory_total_mb')
    mem_used = _number(row, 'memory_used_mb')
    if mem_total and mem_total > 0 and mem_used is not None:
        mem_pct = max(0.0, min(100.0, mem_used / mem_total * 100))
    else:
        mem_total = None
        mem_pct = _number(row, 'memory_percent')

    disk_total = _number(row, 'disk_total_gb')
    disk_pct = _number(row, 'disk_usage_pct', 'disk_percent')
    if disk_total is not None and disk_total <= 0:
        disk_pct = None
    elif disk_pct is None and disk_total:
        disk_pct = (_number(row, 'disk_used_gb') or 0) / disk_total * 100

    cpus = _number(row, 'cpu_count')
    return {
        'timestamp': row.get('timestamp'),
        'status': row.get('status'),
        'load': load,
        'cpu_count': int(cpus) if cpus and cpus > 0 else 1,
        'memory_total_mb': int(mem_total) if mem_total else None,
        'mem_pct': mem_pct,
        'disk_pct': disk_pct,
        'swap_used_mb': int(_number(row, 'swap_used_mb') or 0),
        'users': int(_number(row, 'users_logged_in') or 0),
        'processes': int(_number(row, 'process_count') or 0),
        'zombies': int(_number(row, 'zombie_count') or 0),
    }


# A reading older than this is not the machine's present state: the collector
# runs every five minutes.
STALE_AFTER = timedelta(minutes=60)


def _age(timestamp) -> timedelta | None:
    try:
        when = (timestamp if isinstance(timestamp, datetime)
                else datetime.fromisoformat(str(timestamp)))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return datetime.now() - when


def _either(words: list) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _series(history: list, key: str) -> list:
    """(time, value) pairs, oldest first, leaving out readings without it."""
    out = []
    for record in history:
        value = record.get(key)
        stamp = record.get('timestamp')
        if value is None or stamp is None:
            continue
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp)
            except ValueError:
                continue
        out.append((stamp, value))
    return sorted(out, key=lambda p: p[0])


def analyze_memory_trend(history: list) -> dict:
    """Analyze memory usage trend."""
    points = _series(history, 'mem_pct')
    if not points or not HAS_DERIVATIVES:
        return {}

    analyzer = DerivativeAnalyzer(window_size=len(points))
    for timestamp, mem_pct in points:
        analyzer.add_point(timestamp, mem_pct)

    analysis = analyzer.analyze(limit=100)  # 100% is the limit

    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_disk_trend(history: list) -> dict:
    """Analyze disk usage trend."""
    points = _series(history, 'disk_pct')
    if not points or not HAS_DERIVATIVES:
        return {}

    analyzer = DerivativeAnalyzer(window_size=len(points))
    for timestamp, disk_pct in points:
        analyzer.add_point(timestamp, disk_pct)

    analysis = analyzer.analyze(limit=100)

    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'days_until_full': analysis.days_until_limit,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_load_trend(history: list, cpu_count: int = 1) -> dict:
    """Analyze CPU load trend."""
    points = _series(history, 'load')
    if not points or not HAS_DERIVATIVES:
        return {}

    analyzer = DerivativeAnalyzer(window_size=len(points))
    for timestamp, load in points:
        analyzer.add_point(timestamp, load)

    # Limit is CPU count * 2 (high load threshold)
    analysis = analyzer.analyze(limit=cpu_count * 2)

    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_potential_causes(state: dict, history: list, trends: dict) -> list:
    """Analyze data to suggest potential causes for workstation issues."""
    causes = []

    if not state:
        causes.append({
            'cause': 'Workstation not reporting',
            'confidence': 'high',
            'detail': 'No recent data - may be powered off or network issue'
        })
        return causes

    r = reading(state)

    age = _age(state.get('timestamp'))
    if age is not None and age > STALE_AFTER:
        hours_ago = age.total_seconds() / 3600
        causes.append({
            'cause': 'Workstation not reporting',
            'confidence': 'high',
            'detail': (f'Last reading {hours_ago:.1f} h ago ({state.get("timestamp")}); '
                       'the figures shown are from then')
        })

    missing = [what for what, value in (('load', r['load']), ('memory', r['mem_pct']),
                                        ('disk', r['disk_pct'])) if value is None]
    if missing:
        causes.append({
            'cause': 'Incomplete Reading',
            'confidence': 'low',
            'detail': (f'The latest reading has no {_either(missing)} figures, '
                       'so those checks were skipped')
        })

    # Check memory pressure
    mem_pct = r['mem_pct']
    if mem_pct is not None and mem_pct > 95:
        causes.append({
            'cause': 'Critical Memory Pressure',
            'confidence': 'high',
            'detail': f'Memory at {mem_pct:.1f}% - system may be swapping heavily'
        })
    elif mem_pct is not None and mem_pct > 85:
        causes.append({
            'cause': 'High Memory Usage',
            'confidence': 'medium',
            'detail': f'Memory at {mem_pct:.1f}% - approaching critical levels'
        })

    # Check swap usage
    swap_used = r['swap_used_mb']
    if swap_used > 1024:  # More than 1GB swap
        causes.append({
            'cause': 'Heavy Swap Usage',
            'confidence': 'high',
            'detail': f'{swap_used} MB swap in use - indicates memory pressure'
        })

    # Check disk usage
    disk_pct = r['disk_pct']
    if disk_pct is not None and disk_pct > 95:
        causes.append({
            'cause': 'Disk Almost Full',
            'confidence': 'high',
            'detail': f'Disk at {disk_pct:.1f}% - may cause application failures'
        })
    elif disk_pct is not None and disk_pct > 85:
        causes.append({
            'cause': 'High Disk Usage',
            'confidence': 'medium',
            'detail': f'Disk at {disk_pct:.1f}% - should be monitored'
        })

    # Check CPU load
    load = r['load']
    cpu_count = r['cpu_count']
    if load is not None and load > cpu_count * 2:
        causes.append({
            'cause': 'CPU Overload',
            'confidence': 'high',
            'detail': f'Load average {load:.1f} exceeds {cpu_count * 2} (2x CPU count)'
        })
    elif load is not None and load > cpu_count:
        causes.append({
            'cause': 'High CPU Load',
            'confidence': 'medium',
            'detail': f'Load average {load:.1f} exceeds CPU count ({cpu_count})'
        })

    # Check zombie processes
    zombies = r['zombies']
    if zombies > 10:
        causes.append({
            'cause': 'Many Zombie Processes',
            'confidence': 'medium',
            'detail': f'{zombies} zombie processes - parent processes not reaping children'
        })
    elif zombies > 0:
        causes.append({
            'cause': 'Zombie Processes Present',
            'confidence': 'low',
            'detail': f'{zombies} zombie process(es) detected'
        })

    # Check trends for accelerating issues
    if trends.get('memory', {}).get('alert_level') == 'critical':
        causes.append({
            'cause': 'Memory Usage Accelerating',
            'confidence': 'high',
            'detail': 'Memory usage increasing rapidly - possible memory leak'
        })

    if trends.get('disk', {}).get('alert_level') == 'critical':
        days = trends['disk'].get('days_until_full')
        detail = 'Disk filling rapidly'
        if days:
            detail += f' - estimated full in {days:.1f} days'
        causes.append({
            'cause': 'Disk Filling Rapidly',
            'confidence': 'high',
            'detail': detail
        })

    if not causes:
        causes.append({
            'cause': 'No obvious issues detected',
            'confidence': 'low',
            'detail': 'Workstation appears healthy'
        })

    return causes


def generate_recommendations(causes: list, state: dict, trends: dict) -> list:
    """Generate actionable recommendations based on analysis."""
    recommendations = []

    for cause in causes:
        if cause['cause'] == 'Critical Memory Pressure':
            recommendations.append('Identify memory-hungry processes: ps aux --sort=-%mem | head -10')
            recommendations.append('Check for memory leaks in long-running applications')
            recommendations.append('Consider adding more RAM or closing unused applications')

        elif cause['cause'] == 'High Memory Usage':
            recommendations.append('Monitor memory usage: watch -n 5 free -h')
            recommendations.append('Review running applications for unnecessary processes')

        elif cause['cause'] == 'Heavy Swap Usage':
            recommendations.append('Check what is swapped: cat /proc/swaps')
            recommendations.append('Identify swapping processes: for f in /proc/*/status; do awk \'/VmSwap/{print $2}\' $f 2>/dev/null; done | sort -n | tail')
            recommendations.append('Consider increasing RAM if swap usage is chronic')

        elif cause['cause'] == 'Disk Almost Full':
            recommendations.append('Find large files: du -sh /* 2>/dev/null | sort -h | tail -10')
            recommendations.append('Check for old logs: find /var/log -type f -size +100M')
            recommendations.append('Clear package caches: apt clean / yum clean all')

        elif cause['cause'] == 'High Disk Usage':
            recommendations.append('Monitor disk usage trends')
            recommendations.append('Set up automated cleanup for temporary files')

        elif cause['cause'] == 'CPU Overload':
            recommendations.append('Find CPU-intensive processes: top -bn1 | head -15')
            recommendations.append('Check for runaway processes: ps aux | awk \'$3 > 80\'')

        elif cause['cause'] == 'Many Zombie Processes':
            recommendations.append('Find zombie parent: ps aux | grep -w Z')
            recommendations.append('Kill parent process to clear zombies')

        elif cause['cause'] == 'Memory Usage Accelerating':
            recommendations.append('Enable memory profiling for suspect applications')
            recommendations.append('Schedule regular process restarts if memory leak is confirmed')

        elif cause['cause'] == 'Disk Filling Rapidly':
            recommendations.append('Identify what is writing: iotop -o')
            recommendations.append('Check for runaway log files: lsof +D /var/log')

        elif cause['cause'] == 'Incomplete Reading':
            host = (state or {}).get('hostname') or '<hostname>'
            recommendations.append('Check what the collector can read there: '
                                   f'nomad diag workstation {host} --prereqs')

        elif cause['cause'] == 'Workstation not reporting':
            recommendations.append('Ping workstation: ping <hostname>')
            recommendations.append('Check SSH access: ssh <hostname> hostname')
            recommendations.append('Verify network connectivity and power status')

    # Add healthy message if no issues
    if not recommendations:
        recommendations.append('Workstation appears healthy - no action required')

    return list(dict.fromkeys(recommendations))  # Remove duplicates


def diagnose_workstation(
    db_path: str,
    hostname: str,
    hours: int = 24,
    check_prereqs: bool = False,
    ssh_user: str | None = None,
) -> WorkstationDiagnostic | None:
    """
    Generate comprehensive diagnostics for a workstation.
    
    Args:
        db_path: Path to NØMAÐ database
        hostname: Workstation hostname
        hours: Hours of history to analyze
    
    Returns:
        WorkstationDiagnostic object or None if not found
    """
    # Get current state
    state = get_workstation_state(db_path, hostname)

    # Get history
    history = get_state_history(db_path, hostname, hours)

    if not state and not history:
        return None

    # Build diagnostic object
    diag = WorkstationDiagnostic(
        hostname=hostname,
        department=state.get('department') if state else None,
        current_status=state.get('status', 'unknown') if state else 'not_found',
        last_seen=state.get('timestamp') if state else None,
    )

    if state:
        r = reading(state)
        diag.cpu_load = r['load'] or 0.0
        diag.cpu_count = r['cpu_count']
        diag.memory_total_mb = r['memory_total_mb'] or 0
        diag.memory_used_pct = r['mem_pct'] or 0.0
        diag.disk_used_pct = r['disk_pct'] or 0.0
        diag.swap_used_mb = r['swap_used_mb']
        diag.users_logged_in = r['users']
        diag.process_count = r['processes']
        diag.zombie_count = r['zombies']

    # Not judged from trends: the analysis takes its slope and acceleration
    # from the last three readings, five minutes apart, where ordinary noise
    # reads as "accelerating" (a critical memory leak or a disk filling).
    # analyze_*_trend stay for when there is a sound method.
    diag.trends = {}

    # Build resource history summary
    if history:
        loads = [h['load'] for h in history if h['load'] is not None]
        mems = [h['mem_pct'] for h in history if h['mem_pct'] is not None]
        diag.resource_history = {
            'samples': len(history),
            'avg_load': sum(loads) / len(loads) if loads else 0.0,
            'avg_mem_pct': sum(mems) / len(mems) if mems else 0.0,
            'max_users': max(h['users'] for h in history),
        }

    # Determine causes
    diag.potential_causes = analyze_potential_causes(state, history, diag.trends)

    # Generate recommendations
    diag.recommendations = generate_recommendations(diag.potential_causes, state, diag.trends)

    if check_prereqs:
        try:
            diag.prereqs = check_workstation_prerequisites(
                hostname=hostname,
                ssh_user=ssh_user,
                db_path=db_path,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                f"prereq check failed for {hostname}: {exc}"
            )

    return diag


# ── Formatting ───────────────────────────────────────────────────────

class Colors:
    """ANSI color codes."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'


def format_diagnostic(diag: WorkstationDiagnostic) -> str:
    """Format diagnostic for terminal output."""
    c = Colors
    lines = []

    # Header
    dept_str = f" ({diag.department})" if diag.department else ""
    lines.append(f"\n  {c.BOLD}NØMAÐ Workstation Diagnostic{c.RESET} — {c.CYAN}{diag.hostname}{dept_str}{c.RESET}")
    lines.append(f"  {'─' * 56}")

    # Collection prerequisites (if checked)
    if diag.prereqs is not None:
        lines.append(format_prerequisite_checks(
            diag.prereqs, use_color=True, show_fix_hints=True,
        ))
        if diag.prereqs.fail_count > 0:
            lines.append("")
            lines.append(
                f"  {c.YELLOW}Note: data collection has FAILed prerequisites; "
                f"the health analysis below may be based on incomplete data.{c.RESET}"
            )

    # Current State
    status_color = c.GREEN if diag.current_status == 'online' else c.YELLOW if diag.current_status == 'degraded' else c.RED
    lines.append(f"\n  {c.BOLD}Status:{c.RESET} {status_color}{diag.current_status}{c.RESET}")

    if diag.last_seen:
        lines.append(f"  {c.BOLD}Last Seen:{c.RESET} {diag.last_seen}")

    # Resource Summary
    lines.append(f"\n  {c.BOLD}Current Resources{c.RESET}")
    lines.append(f"  {'─' * 56}")

    # CPU
    load_color = c.RED if diag.cpu_load > diag.cpu_count * 2 else c.YELLOW if diag.cpu_load > diag.cpu_count else c.GREEN
    lines.append(f"    CPU Load:     {load_color}{diag.cpu_load:.2f}{c.RESET} / {diag.cpu_count} cores")

    # Memory
    mem_color = c.RED if diag.memory_used_pct > 95 else c.YELLOW if diag.memory_used_pct > 85 else c.GREEN
    lines.append(f"    Memory:       {mem_color}{diag.memory_used_pct:.1f}%{c.RESET} of {diag.memory_total_mb} MB")

    # Disk
    disk_color = c.RED if diag.disk_used_pct > 95 else c.YELLOW if diag.disk_used_pct > 85 else c.GREEN
    lines.append(f"    Disk:         {disk_color}{diag.disk_used_pct:.1f}%{c.RESET}")

    # Swap
    if diag.swap_used_mb > 0:
        swap_color = c.RED if diag.swap_used_mb > 1024 else c.YELLOW
        lines.append(f"    Swap:         {swap_color}{diag.swap_used_mb} MB{c.RESET}")

    # Users & Processes
    lines.append(f"\n  {c.BOLD}Activity{c.RESET}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"    Users logged in:  {diag.users_logged_in}")
    lines.append(f"    Processes:        {diag.process_count}")
    if diag.zombie_count > 0:
        zombie_color = c.RED if diag.zombie_count > 10 else c.YELLOW
        lines.append(f"    Zombies:          {zombie_color}{diag.zombie_count}{c.RESET}")

    # Trends
    if diag.trends:
        lines.append(f"\n  {c.BOLD}Trends (last {diag.resource_history.get('samples', 0)} samples){c.RESET}")
        lines.append(f"  {'─' * 56}")

        for name, trend in diag.trends.items():
            if trend:
                trend_str = trend.get('trend', 'unknown')
                trend_color = c.RED if trend_str == 'accelerating' else c.YELLOW if trend_str == 'increasing' else c.GREEN
                d1 = trend.get('first_derivative')
                d1_str = f"{d1:+.2f}/day" if d1 else "N/A"
                lines.append(f"    {name.capitalize():12} {trend_color}{trend_str:12}{c.RESET} ({d1_str})")

    # Potential Causes
    lines.append(f"\n  {c.BOLD}Potential Causes{c.RESET}")
    lines.append(f"  {'─' * 56}")
    for cause in diag.potential_causes:
        conf_color = c.RED if cause['confidence'] == 'high' else c.YELLOW if cause['confidence'] == 'medium' else c.GRAY
        lines.append(f"    {conf_color}[{cause['confidence'].upper()}]{c.RESET} {cause['cause']}")
        lines.append(f"           {c.GRAY}{cause['detail']}{c.RESET}")

    # Recommendations
    lines.append(f"\n  {c.BOLD}Recommendations{c.RESET}")
    lines.append(f"  {'─' * 56}")
    for rec in diag.recommendations[:6]:
        lines.append(f"    {c.CYAN}→{c.RESET} {rec}")

    lines.append("")
    return '\n'.join(lines)
