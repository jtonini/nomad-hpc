"""
Node Diagnostics for HPC clusters.

Provides detailed analysis of node health and issues:
- SLURM state and drain reasons
- Resource utilization history
- Job history and user activity
- Failure pattern analysis
- Root cause suggestions

Integrates with:
- analysis/derivatives.py for trend detection
- alerts/thresholds.py for threshold checking
- collectors/ data for historical metrics
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

# Import existing analysis tools
try:
    from nomad.analysis.derivatives import DerivativeAnalyzer, analyze_disk_trend, AlertLevel
    HAS_DERIVATIVES = True
except ImportError:
    HAS_DERIVATIVES = False

try:
    from nomad.alerts.thresholds import ThresholdChecker
    HAS_THRESHOLDS = True
except ImportError:
    HAS_THRESHOLDS = False

logger = logging.getLogger(__name__)



@dataclass
class NodeDiagnostic:
    """Container for node diagnostic information."""
    node_name: str
    cluster: str
    current_state: str
    drain_reason: Optional[str]
    last_seen: Optional[datetime]
    state_history: list
    resource_history: dict
    recent_jobs: list
    active_users: list
    failure_summary: dict
    potential_causes: list
    recommendations: list


def get_node_state(db_path: str, cluster: str, node_name: str) -> Optional[dict]:
    """Get current node state from database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT * FROM node_state 
            WHERE node_name = ? AND cluster = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (node_name, cluster)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error getting node state: {e}")
        return None


def get_state_history(db_path: str, cluster: str, node_name: str, hours: int = 24) -> list:
    """Get node state changes over time."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT timestamp, state, reason, cpu_load, memory_alloc_percent
            FROM node_state 
            WHERE node_name = ? AND cluster = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """, (node_name, cluster, since)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting state history: {e}")
        return []


def get_recent_jobs(db_path: str, cluster: str, node_name: str, limit: int = 20) -> list:
    """Get recent jobs that ran on this node."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT job_id, user_name, job_name, state, exit_code, 
                   start_time, end_time, runtime_seconds, failure_reason
            FROM jobs 
            WHERE cluster = ? AND node_list LIKE ?
            ORDER BY end_time DESC LIMIT ?
        """, (cluster, f"%{node_name}%", limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting recent jobs: {e}")
        return []


def get_failure_summary(jobs: list) -> dict:
    """Analyze failure patterns from job history."""
    summary = {
        'total_jobs': len(jobs),
        'failed_jobs': 0,
        'failure_types': {},
        'exit_codes': {},
        'affected_users': set()
    }
    
    for job in jobs:
        if job.get('state') not in ('COMPLETED',):
            summary['failed_jobs'] += 1
            summary['affected_users'].add(job.get('user_name'))
            
            # Count failure types
            reason = job.get('failure_reason') or job.get('state') or 'UNKNOWN'
            summary['failure_types'][reason] = summary['failure_types'].get(reason, 0) + 1
            
            # Count exit codes
            exit_code = job.get('exit_code', 0)
            if exit_code != 0:
                summary['exit_codes'][exit_code] = summary['exit_codes'].get(exit_code, 0) + 1
    
    summary['affected_users'] = list(summary['affected_users'])
    summary['failure_rate'] = summary['failed_jobs'] / max(summary['total_jobs'], 1)
    return summary


def analyze_potential_causes(state: dict, history: list, jobs: list, failures: dict) -> list:
    """Analyze data to suggest potential causes for node issues."""
    causes = []
    
    if not state:
        causes.append({
            'cause': 'Node not reporting',
            'confidence': 'high',
            'detail': 'No recent data from node - may be powered off or network issue'
        })
        return causes
    
    # Check for drain reason
    reason = state.get('reason', '')
    if reason:
        if 'oom' in reason.lower() or 'memory' in reason.lower():
            causes.append({
                'cause': 'Out of Memory (OOM)',
                'confidence': 'high',
                'detail': f'SLURM drain reason indicates memory issue: {reason}'
            })
        elif 'gpu' in reason.lower():
            causes.append({
                'cause': 'GPU Issue',
                'confidence': 'high',
                'detail': f'SLURM drain reason indicates GPU problem: {reason}'
            })
        elif 'health' in reason.lower():
            causes.append({
                'cause': 'Health Check Failed',
                'confidence': 'high',
                'detail': f'Node failed health check: {reason}'
            })
        else:
            causes.append({
                'cause': 'Admin Drain',
                'confidence': 'medium',
                'detail': f'Node was drained: {reason}'
            })
    
    # Check memory pressure
    mem_pct = state.get('memory_alloc_percent', 0)
    if mem_pct and mem_pct > 95:
        causes.append({
            'cause': 'Memory Pressure',
            'confidence': 'medium',
            'detail': f'Memory allocation at {mem_pct:.1f}% before issue'
        })
    
    # Check for OOM patterns in jobs
    oom_count = failures.get('exit_codes', {}).get(137, 0)  # SIGKILL often from OOM
    if oom_count > 0:
        causes.append({
            'cause': 'Job OOM Kills',
            'confidence': 'medium',
            'detail': f'{oom_count} jobs killed with exit code 137 (likely OOM)'
        })
    
    # Check for high failure rate
    if failures.get('failure_rate', 0) > 0.3:
        causes.append({
            'cause': 'High Job Failure Rate',
            'confidence': 'medium',
            'detail': f"{failures['failure_rate']*100:.0f}% of recent jobs failed"
        })
    
    # Check load average
    cpu_load = state.get('cpu_load', 0)
    cpus_total = state.get('cpus_total', 1)
    if cpu_load and cpus_total and cpu_load > cpus_total * 2:
        causes.append({
            'cause': 'CPU Overload',
            'confidence': 'medium',
            'detail': f'Load average ({cpu_load:.1f}) far exceeds CPU count ({cpus_total})'
        })
    
    if not causes:
        causes.append({
            'cause': 'No obvious issues detected',
            'confidence': 'low',
            'detail': 'Manual investigation recommended'
        })
    
    return causes


def generate_recommendations(causes: list, state: dict, failures: dict) -> list:
    """Generate actionable recommendations based on analysis."""
    recommendations = []
    
    for cause in causes:
        if cause['cause'] == 'Out of Memory (OOM)':
            recommendations.append('Check dmesg for OOM killer messages: dmesg | grep -i oom')
            recommendations.append('Review memory limits for jobs on this node')
            recommendations.append('Consider adding memory constraints to partition')
        
        elif cause['cause'] == 'GPU Issue':
            recommendations.append('Check GPU status: nvidia-smi')
            recommendations.append('Check GPU driver: nvidia-smi -q')
            recommendations.append('Review GPU error logs: dmesg | grep -i nvidia')
        
        elif cause['cause'] == 'Health Check Failed':
            recommendations.append('Run manual health check: scontrol show node <node>')
            recommendations.append('Check SLURM health check script output')
        
        elif cause['cause'] == 'Memory Pressure':
            recommendations.append('Review running jobs: squeue -w <node>')
            recommendations.append('Check for memory leaks in long-running jobs')
        
        elif cause['cause'] == 'CPU Overload':
            recommendations.append('Check for runaway processes: top -bn1')
            recommendations.append('Review CPU-bound jobs for inefficiencies')
        
        elif cause['cause'] == 'Node not reporting':
            recommendations.append('Ping node: ping <node>')
            recommendations.append('Check SSH access: ssh <node> hostname')
            recommendations.append('Check SLURM daemon: systemctl status slurmd')
            recommendations.append('Check power/IPMI if available')
    
    # Only add resume if there are actual issues
    if recommendations:
        recommendations.append('Resume node after fixing: scontrol update nodename=<node> state=resume')
    else:
        recommendations.append('Node appears healthy - no action required')
    
    return list(dict.fromkeys(recommendations))  # Remove duplicates


def diagnose_node(
    db_path: str,
    cluster: str,
    node_name: str,
    hours: int = 24,
) -> Optional[NodeDiagnostic]:
    """
    Generate comprehensive diagnostics for an HPC node.
    
    Args:
        db_path: Path to NØMAD database
        cluster: Cluster name
        node_name: Node hostname
        hours: Hours of history to analyze
    
    Returns:
        NodeDiagnostic object or None if node not found
    """
    # Get current state
    state = get_node_state(db_path, cluster, node_name)
    
    # Get history
    history = get_state_history(db_path, cluster, node_name, hours)
    
    # Get recent jobs
    jobs = get_recent_jobs(db_path, cluster, node_name)
    
    # Analyze failures
    failures = get_failure_summary(jobs)
    
    # Determine causes
    causes = analyze_potential_causes(state, history, jobs, failures)
    
    # Generate recommendations
    recommendations = generate_recommendations(causes, state, failures)
    
    # Extract active users from recent jobs
    active_users = list(set(j.get('user_name') for j in jobs[:10] if j.get('user_name')))
    
    # Build resource history summary
    resource_history = {}
    if history:
        resource_history = {
            'samples': len(history),
            'avg_cpu_load': sum(h.get('cpu_load', 0) or 0 for h in history) / max(len(history), 1),
            'avg_mem_pct': sum(h.get('memory_alloc_percent', 0) or 0 for h in history) / max(len(history), 1),
            'state_changes': len(set(h.get('state') for h in history))
        }
    
    return NodeDiagnostic(
        node_name=node_name,
        cluster=cluster,
        current_state=state.get('state', 'UNKNOWN') if state else 'NOT_FOUND',
        drain_reason=state.get('reason') if state else None,
        last_seen=state.get('timestamp') if state else None,
        state_history=history[:10],  # Last 10 state records
        resource_history=resource_history,
        recent_jobs=jobs[:10],
        active_users=active_users,
        failure_summary=failures,
        potential_causes=causes,
        recommendations=recommendations
    )


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


def format_diagnostic(diag: NodeDiagnostic) -> str:
    """Format diagnostic for terminal output."""
    c = Colors
    lines = []
    
    # Header
    lines.append(f"\n  {c.BOLD}NØMAD Node Diagnostic{c.RESET} — {c.CYAN}{diag.cluster}/{diag.node_name}{c.RESET}")
    lines.append(f"  {'─' * 56}")
    
    # Current State
    state_color = c.GREEN if diag.current_state in ('idle', 'allocated', 'mixed') else c.RED
    lines.append(f"\n  {c.BOLD}Current State:{c.RESET} {state_color}{diag.current_state}{c.RESET}")
    
    if diag.drain_reason:
        lines.append(f"  {c.BOLD}Drain Reason:{c.RESET} {c.YELLOW}{diag.drain_reason}{c.RESET}")
    
    if diag.last_seen:
        lines.append(f"  {c.BOLD}Last Seen:{c.RESET} {diag.last_seen}")
    
    # Resource Summary
    if diag.resource_history:
        rh = diag.resource_history
        lines.append(f"\n  {c.BOLD}Resource History{c.RESET} ({rh.get('samples', 0)} samples)")
        lines.append(f"  {'─' * 56}")
        lines.append(f"    Avg CPU Load:    {rh.get('avg_cpu_load', 0):.1f}")
        lines.append(f"    Avg Memory:      {rh.get('avg_mem_pct', 0):.1f}%")
        lines.append(f"    State Changes:   {rh.get('state_changes', 0)}")
    
    # Failure Summary
    fs = diag.failure_summary
    if fs.get('total_jobs', 0) > 0:
        lines.append(f"\n  {c.BOLD}Job Summary{c.RESET} (last {fs['total_jobs']} jobs)")
        lines.append(f"  {'─' * 56}")
        fail_color = c.RED if fs['failure_rate'] > 0.2 else c.YELLOW if fs['failure_rate'] > 0.1 else c.GREEN
        lines.append(f"    Failed:          {fs['failed_jobs']} ({fail_color}{fs['failure_rate']*100:.0f}%{c.RESET})")
        
        if fs.get('failure_types'):
            lines.append(f"    Failure Types:")
            for ftype, count in sorted(fs['failure_types'].items(), key=lambda x: -x[1])[:5]:
                lines.append(f"      {c.GRAY}•{c.RESET} {ftype}: {count}")
        
        if fs.get('affected_users'):
            lines.append(f"    Affected Users:  {', '.join(fs['affected_users'][:5])}")
    
    # Active Users
    if diag.active_users:
        lines.append(f"\n  {c.BOLD}Recent Users{c.RESET}")
        lines.append(f"  {'─' * 56}")
        lines.append(f"    {', '.join(diag.active_users[:8])}")
    
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
