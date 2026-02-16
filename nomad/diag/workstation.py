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

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

# Import existing analysis tools
try:
    from nomad.analysis.derivatives import DerivativeAnalyzer, AlertLevel
    HAS_DERIVATIVES = True
except ImportError:
    HAS_DERIVATIVES = False

logger = logging.getLogger(__name__)


@dataclass
class WorkstationDiagnostic:
    """Container for workstation diagnostic information."""
    hostname: str
    department: Optional[str]
    current_status: str
    last_seen: Optional[datetime]
    
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


def get_workstation_state(db_path: str, hostname: str) -> Optional[dict]:
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
    """Get workstation state changes over time."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT timestamp, status, load_avg_1m, memory_used_mb, memory_total_mb,
                   disk_usage_pct, swap_used_mb, users_logged_in, zombie_count
            FROM workstation_state 
            WHERE hostname = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """, (hostname, since)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting state history: {e}")
        return []


def analyze_memory_trend(history: list) -> dict:
    """Analyze memory usage trend."""
    if not history or not HAS_DERIVATIVES:
        return {}
    
    analyzer = DerivativeAnalyzer(window_size=len(history))
    
    for record in history:
        timestamp = record.get('timestamp')
        mem_total = record.get('memory_total_mb', 1)
        mem_used = record.get('memory_used_mb', 0)
        
        if mem_total > 0:
            mem_pct = (mem_used / mem_total) * 100
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
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
    if not history or not HAS_DERIVATIVES:
        return {}
    
    analyzer = DerivativeAnalyzer(window_size=len(history))
    
    for record in history:
        timestamp = record.get('timestamp')
        disk_pct = record.get('disk_usage_pct', 0)
        
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
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
    if not history or not HAS_DERIVATIVES:
        return {}
    
    analyzer = DerivativeAnalyzer(window_size=len(history))
    
    for record in history:
        timestamp = record.get('timestamp')
        load = record.get('load_avg_1m', 0)
        
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
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
    
    status = state.get('status', '')
    
    # Check memory pressure
    mem_total = state.get('memory_total_mb', 1)
    mem_used = state.get('memory_used_mb', 0)
    mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0
    
    if mem_pct > 95:
        causes.append({
            'cause': 'Critical Memory Pressure',
            'confidence': 'high',
            'detail': f'Memory at {mem_pct:.1f}% - system may be swapping heavily'
        })
    elif mem_pct > 85:
        causes.append({
            'cause': 'High Memory Usage',
            'confidence': 'medium',
            'detail': f'Memory at {mem_pct:.1f}% - approaching critical levels'
        })
    
    # Check swap usage
    swap_used = state.get('swap_used_mb', 0)
    if swap_used > 1024:  # More than 1GB swap
        causes.append({
            'cause': 'Heavy Swap Usage',
            'confidence': 'high',
            'detail': f'{swap_used} MB swap in use - indicates memory pressure'
        })
    
    # Check disk usage
    disk_pct = state.get('disk_usage_pct', 0)
    if disk_pct > 95:
        causes.append({
            'cause': 'Disk Almost Full',
            'confidence': 'high',
            'detail': f'Disk at {disk_pct:.1f}% - may cause application failures'
        })
    elif disk_pct > 85:
        causes.append({
            'cause': 'High Disk Usage',
            'confidence': 'medium',
            'detail': f'Disk at {disk_pct:.1f}% - should be monitored'
        })
    
    # Check CPU load
    load = state.get('load_avg_1m', 0)
    cpu_count = state.get('cpu_count', 1)
    if load > cpu_count * 2:
        causes.append({
            'cause': 'CPU Overload',
            'confidence': 'high',
            'detail': f'Load average {load:.1f} exceeds {cpu_count * 2} (2x CPU count)'
        })
    elif load > cpu_count:
        causes.append({
            'cause': 'High CPU Load',
            'confidence': 'medium',
            'detail': f'Load average {load:.1f} exceeds CPU count ({cpu_count})'
        })
    
    # Check zombie processes
    zombies = state.get('zombie_count', 0)
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
        detail = f'Disk filling rapidly'
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
) -> Optional[WorkstationDiagnostic]:
    """
    Generate comprehensive diagnostics for a workstation.
    
    Args:
        db_path: Path to NØMAD database
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
        diag.cpu_load = state.get('load_avg_1m', 0)
        diag.cpu_count = state.get('cpu_count', 1)
        diag.memory_total_mb = state.get('memory_total_mb', 0)
        mem_used = state.get('memory_used_mb', 0)
        diag.memory_used_pct = (mem_used / diag.memory_total_mb * 100) if diag.memory_total_mb > 0 else 0
        diag.disk_used_pct = state.get('disk_usage_pct', 0)
        diag.swap_used_mb = state.get('swap_used_mb', 0)
        diag.users_logged_in = state.get('users_logged_in', 0)
        diag.process_count = state.get('process_count', 0)
        diag.zombie_count = state.get('zombie_count', 0)
    
    # Analyze trends
    diag.trends = {
        'memory': analyze_memory_trend(history),
        'disk': analyze_disk_trend(history),
        'load': analyze_load_trend(history, diag.cpu_count),
    }
    
    # Build resource history summary
    if history:
        diag.resource_history = {
            'samples': len(history),
            'avg_load': sum(h.get('load_avg_1m', 0) or 0 for h in history) / max(len(history), 1),
            'avg_mem_pct': sum(
                (h.get('memory_used_mb', 0) / max(h.get('memory_total_mb', 1), 1) * 100) 
                for h in history
            ) / max(len(history), 1),
            'max_users': max(h.get('users_logged_in', 0) or 0 for h in history),
        }
    
    # Determine causes
    diag.potential_causes = analyze_potential_causes(state, history, diag.trends)
    
    # Generate recommendations
    diag.recommendations = generate_recommendations(diag.potential_causes, state, diag.trends)
    
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
    lines.append(f"\n  {c.BOLD}NØMAD Workstation Diagnostic{c.RESET} — {c.CYAN}{diag.hostname}{dept_str}{c.RESET}")
    lines.append(f"  {'─' * 56}")
    
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
