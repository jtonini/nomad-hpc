"""
Storage/NAS Diagnostics for storage devices.

Provides detailed analysis of storage health and issues:
- ZFS pool health, scrub status, errors
- ARC cache efficiency
- NFS export and client status
- Disk usage trends and fill rate prediction
- I/O throughput analysis

Integrates with:
- analysis/derivatives.py for trend detection
- alerts/thresholds.py for threshold checking
- collectors/storage.py data
"""

import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

# Import existing analysis tools
try:
    from nomad.analysis.derivatives import DerivativeAnalyzer, analyze_disk_trend as analyze_disk_derivative, AlertLevel
    HAS_DERIVATIVES = True
except ImportError:
    HAS_DERIVATIVES = False

logger = logging.getLogger(__name__)


@dataclass
class ZFSPoolDiagnostic:
    """ZFS pool diagnostic info."""
    name: str
    health: str
    capacity_pct: float
    fragmentation_pct: float
    read_errors: int
    write_errors: int
    checksum_errors: int
    scrub_in_progress: bool
    last_scrub: Optional[str]
    issues: list = field(default_factory=list)


@dataclass
class StorageDiagnostic:
    """Container for storage diagnostic information."""
    hostname: str
    storage_type: str
    current_status: str
    last_seen: Optional[datetime]
    
    # Capacity
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    usage_pct: float = 0.0
    
    # ZFS-specific
    pools: list = field(default_factory=list)
    arc_hit_ratio: float = 0.0
    arc_size_gb: float = 0.0
    
    # NFS-specific
    nfs_exports: list = field(default_factory=list)
    nfs_clients: int = 0
    
    # I/O
    read_bytes_sec: float = 0.0
    write_bytes_sec: float = 0.0
    
    # History and trends
    resource_history: dict = field(default_factory=dict)
    trends: dict = field(default_factory=dict)
    
    # Analysis results
    potential_causes: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)


def get_storage_state(db_path: str, hostname: str) -> Optional[dict]:
    """Get current storage state from database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT * FROM storage_state 
            WHERE hostname = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (hostname,)).fetchone()
        conn.close()
        if row:
            result = dict(row)
            # Parse JSON fields
            for field in ['pools_json', 'arc_stats_json', 'nfs_exports_json']:
                if result.get(field):
                    try:
                        result[field.replace('_json', '')] = json.loads(result[field])
                    except json.JSONDecodeError:
                        result[field.replace('_json', '')] = None
            return result
        return None
    except Exception as e:
        logger.error(f"Error getting storage state: {e}")
        return None


def get_state_history(db_path: str, hostname: str, hours: int = 24) -> list:
    """Get storage state changes over time."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        rows = conn.execute("""
            SELECT timestamp, status, total_bytes, used_bytes, free_bytes,
                   usage_pct, read_bytes_sec, write_bytes_sec, pools_json
            FROM storage_state 
            WHERE hostname = ? AND timestamp > ?
            ORDER BY timestamp DESC
        """, (hostname, since)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting state history: {e}")
        return []


def analyze_usage_trend(history: list) -> dict:
    """Analyze storage usage trend using derivatives."""
    if not history or not HAS_DERIVATIVES:
        return {}
    
    # Prepare data for derivative analysis
    data_points = []
    for record in history:
        timestamp = record.get('timestamp')
        used = record.get('used_bytes', 0)
        
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
        
        data_points.append({'timestamp': timestamp, 'used_bytes': used})
    
    if len(data_points) < 3:
        return {}
    
    # Get total capacity from most recent record
    total_bytes = history[0].get('total_bytes', 0) if history else 0
    
    analysis = analyze_disk_derivative(data_points, limit_bytes=total_bytes)
    
    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'second_derivative': analysis.second_derivative,
        'days_until_full': analysis.days_until_limit,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_zfs_pools(pools: list) -> list[ZFSPoolDiagnostic]:
    """Analyze ZFS pools for issues."""
    pool_diagnostics = []
    
    for pool in pools:
        if not isinstance(pool, dict):
            continue
            
        diag = ZFSPoolDiagnostic(
            name=pool.get('name', 'unknown'),
            health=pool.get('health', 'UNKNOWN'),
            capacity_pct=pool.get('capacity_pct', 0),
            fragmentation_pct=pool.get('fragmentation_pct', 0),
            read_errors=pool.get('read_errors', 0),
            write_errors=pool.get('write_errors', 0),
            checksum_errors=pool.get('checksum_errors', 0),
            scrub_in_progress=pool.get('scrub_in_progress', False),
            last_scrub=pool.get('last_scrub'),
        )
        
        # Check for issues
        if diag.health != 'ONLINE':
            diag.issues.append(f"Pool health is {diag.health}")
        
        if diag.capacity_pct > 90:
            diag.issues.append(f"Pool at {diag.capacity_pct:.1f}% capacity")
        elif diag.capacity_pct > 80:
            diag.issues.append(f"Pool approaching full ({diag.capacity_pct:.1f}%)")
        
        if diag.fragmentation_pct > 50:
            diag.issues.append(f"High fragmentation ({diag.fragmentation_pct:.1f}%)")
        
        total_errors = diag.read_errors + diag.write_errors + diag.checksum_errors
        if total_errors > 0:
            diag.issues.append(f"Errors detected: {diag.read_errors}R/{diag.write_errors}W/{diag.checksum_errors}C")
        
        pool_diagnostics.append(diag)
    
    return pool_diagnostics


def analyze_potential_causes(state: dict, history: list, trends: dict, pools: list) -> list:
    """Analyze data to suggest potential causes for storage issues."""
    causes = []
    
    if not state:
        causes.append({
            'cause': 'Storage not reporting',
            'confidence': 'high',
            'detail': 'No recent data - may be powered off or network issue'
        })
        return causes
    
    status = state.get('status', '')
    
    # Check overall capacity
    usage_pct = state.get('usage_pct', 0)
    if usage_pct > 95:
        causes.append({
            'cause': 'Storage Almost Full',
            'confidence': 'high',
            'detail': f'Capacity at {usage_pct:.1f}% - critical level'
        })
    elif usage_pct > 85:
        causes.append({
            'cause': 'High Storage Usage',
            'confidence': 'medium',
            'detail': f'Capacity at {usage_pct:.1f}% - should be monitored'
        })
    
    # Check ZFS pools
    for pool in pools:
        if pool.issues:
            for issue in pool.issues:
                severity = 'high' if 'health' in issue.lower() or 'error' in issue.lower() else 'medium'
                causes.append({
                    'cause': f'ZFS Pool {pool.name}: {issue}',
                    'confidence': severity,
                    'detail': f'Pool {pool.name} requires attention'
                })
    
    # Check ARC efficiency
    arc_stats = state.get('arc_stats')
    if arc_stats and isinstance(arc_stats, dict):
        hit_ratio = arc_stats.get('hit_ratio', 0)
        if hit_ratio < 0.7:
            causes.append({
                'cause': 'Low ZFS ARC Hit Ratio',
                'confidence': 'medium',
                'detail': f'ARC hit ratio is {hit_ratio*100:.1f}% - may need more RAM for caching'
            })
    
    # Check NFS clients
    nfs_clients = state.get('nfs_clients_connected', 0)
    if nfs_clients > 100:
        causes.append({
            'cause': 'High NFS Client Load',
            'confidence': 'low',
            'detail': f'{nfs_clients} NFS clients connected'
        })
    
    # Check trend analysis
    if trends.get('usage', {}).get('alert_level') == 'critical':
        days = trends['usage'].get('days_until_full')
        detail = 'Storage filling rapidly'
        if days:
            detail += f' - estimated full in {days:.1f} days'
        causes.append({
            'cause': 'Rapid Storage Fill Rate',
            'confidence': 'high',
            'detail': detail
        })
    elif trends.get('usage', {}).get('alert_level') == 'warning':
        causes.append({
            'cause': 'Storage Fill Rate Increasing',
            'confidence': 'medium',
            'detail': 'Storage usage accelerating - monitor closely'
        })
    
    if not causes:
        causes.append({
            'cause': 'No obvious issues detected',
            'confidence': 'low',
            'detail': 'Storage appears healthy'
        })
    
    return causes


def generate_recommendations(causes: list, state: dict, pools: list) -> list:
    """Generate actionable recommendations based on analysis."""
    recommendations = []
    
    for cause in causes:
        cause_name = cause['cause']
        
        if 'Storage' in cause_name and ('Full' in cause_name or 'filling' in cause_name.lower() or 'Usage' in cause_name):
            recommendations.append('Identify large files/datasets: zfs list -o name,used,refer -S used | head')
            recommendations.append('Check for old snapshots: zfs list -t snapshot -o name,used -S used | head')
            recommendations.append('Review quota usage by user/group')
        
        elif 'health' in cause_name.lower() and 'ZFS' in cause_name:
            recommendations.append('Check pool status: zpool status -v')
            recommendations.append('Review drive health: smartctl -a /dev/sdX')
            recommendations.append('Consider replacing failing drives immediately')
        
        elif 'error' in cause_name.lower() and 'ZFS' in cause_name:
            recommendations.append('Run scrub to verify data: zpool scrub <pool>')
            recommendations.append('Check zpool status for affected devices: zpool status -v')
            recommendations.append('Review system logs: dmesg | grep -i error')
        
        elif 'capacity' in cause_name.lower() and 'ZFS' in cause_name:
            recommendations.append('Delete old snapshots: zfs destroy <pool>@<snapshot>')
            recommendations.append('Enable compression if not already: zfs set compression=lz4 <dataset>')
            recommendations.append('Move cold data to archive storage')
        
        elif 'fragmentation' in cause_name.lower():
            recommendations.append('Fragmentation requires free space to defragment - clear space first')
            recommendations.append('Consider export/reimport of severely fragmented datasets')
        
        elif 'ARC' in cause_name:
            recommendations.append('Check ARC stats: arc_summary')
            recommendations.append('Consider adding RAM for larger ARC')
            recommendations.append('Review L2ARC usage if SSD cache available')
        
        elif 'NFS' in cause_name:
            recommendations.append('Review NFS client list: showmount -a')
            recommendations.append('Check NFS performance: nfsstat -s')
        
        elif 'not reporting' in cause_name.lower():
            recommendations.append('Ping storage device: ping <hostname>')
            recommendations.append('Check SSH access: ssh <hostname> hostname')
            recommendations.append('Verify network connectivity and power status')
    
    # ZFS-specific best practices
    if any('ZFS' in c['cause'] for c in causes):
        recommendations.append('Regular scrubs recommended: zpool scrub <pool> (monthly)')
    
    if not recommendations:
        recommendations.append('Storage appears healthy - no action required')
    
    return list(dict.fromkeys(recommendations))  # Remove duplicates


def diagnose_storage(
    db_path: str,
    hostname: str,
    hours: int = 24,
) -> Optional[StorageDiagnostic]:
    """
    Generate comprehensive diagnostics for a storage device.
    
    Args:
        db_path: Path to NØMAD database
        hostname: Storage device hostname
        hours: Hours of history to analyze
    
    Returns:
        StorageDiagnostic object or None if not found
    """
    # Get current state
    state = get_storage_state(db_path, hostname)
    
    # Get history
    history = get_state_history(db_path, hostname, hours)
    
    if not state and not history:
        return None
    
    # Build diagnostic object
    diag = StorageDiagnostic(
        hostname=hostname,
        storage_type=state.get('storage_type', 'unknown') if state else 'unknown',
        current_status=state.get('status', 'unknown') if state else 'not_found',
        last_seen=state.get('timestamp') if state else None,
    )
    
    if state:
        diag.total_bytes = state.get('total_bytes', 0)
        diag.used_bytes = state.get('used_bytes', 0)
        diag.free_bytes = state.get('free_bytes', 0)
        diag.usage_pct = state.get('usage_pct', 0)
        diag.nfs_clients = state.get('nfs_clients_connected', 0)
        diag.read_bytes_sec = state.get('read_bytes_sec', 0)
        diag.write_bytes_sec = state.get('write_bytes_sec', 0)
        
        # Parse ZFS pools
        pools_data = state.get('pools', [])
        if pools_data:
            diag.pools = analyze_zfs_pools(pools_data)
        
        # Parse ARC stats
        arc_stats = state.get('arc_stats')
        if arc_stats and isinstance(arc_stats, dict):
            diag.arc_hit_ratio = arc_stats.get('hit_ratio', 0)
            diag.arc_size_gb = arc_stats.get('size_bytes', 0) / (1024**3)
        
        # Parse NFS exports
        diag.nfs_exports = state.get('nfs_exports', [])
    
    # Analyze trends
    diag.trends = {
        'usage': analyze_usage_trend(history),
    }
    
    # Build resource history summary
    if history:
        diag.resource_history = {
            'samples': len(history),
            'avg_usage_pct': sum(h.get('usage_pct', 0) or 0 for h in history) / max(len(history), 1),
        }
    
    # Determine causes
    diag.potential_causes = analyze_potential_causes(state, history, diag.trends, diag.pools)
    
    # Generate recommendations
    diag.recommendations = generate_recommendations(diag.potential_causes, state, diag.pools)
    
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


def format_bytes(bytes_val: float) -> str:
    """Format bytes as human-readable size."""
    if not bytes_val:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} EB"


def format_diagnostic(diag: StorageDiagnostic) -> str:
    """Format diagnostic for terminal output."""
    c = Colors
    lines = []
    
    # Header
    lines.append(f"\n  {c.BOLD}NØMAD Storage Diagnostic{c.RESET} — {c.CYAN}{diag.hostname}{c.RESET} ({diag.storage_type})")
    lines.append(f"  {'─' * 56}")
    
    # Current State
    status_color = c.GREEN if diag.current_status == 'online' else c.YELLOW if diag.current_status == 'degraded' else c.RED
    lines.append(f"\n  {c.BOLD}Status:{c.RESET} {status_color}{diag.current_status}{c.RESET}")
    
    if diag.last_seen:
        lines.append(f"  {c.BOLD}Last Seen:{c.RESET} {diag.last_seen}")
    
    # Capacity Summary
    lines.append(f"\n  {c.BOLD}Capacity{c.RESET}")
    lines.append(f"  {'─' * 56}")
    
    usage_color = c.RED if diag.usage_pct > 90 else c.YELLOW if diag.usage_pct > 80 else c.GREEN
    lines.append(f"    Used:   {format_bytes(diag.used_bytes)} / {format_bytes(diag.total_bytes)}")
    lines.append(f"    Usage:  {usage_color}{diag.usage_pct:.1f}%{c.RESET}")
    lines.append(f"    Free:   {format_bytes(diag.free_bytes)}")
    
    # ZFS Pools
    if diag.pools:
        lines.append(f"\n  {c.BOLD}ZFS Pools{c.RESET}")
        lines.append(f"  {'─' * 56}")
        for pool in diag.pools:
            health_color = c.GREEN if pool.health == 'ONLINE' else c.RED
            cap_color = c.RED if pool.capacity_pct > 90 else c.YELLOW if pool.capacity_pct > 80 else c.GREEN
            lines.append(f"    {pool.name}: {health_color}{pool.health}{c.RESET} | {cap_color}{pool.capacity_pct:.1f}%{c.RESET}")
            
            if pool.scrub_in_progress:
                lines.append(f"      {c.YELLOW}⟳ Scrub in progress{c.RESET}")
            elif pool.last_scrub:
                lines.append(f"      Last scrub: {pool.last_scrub}")
            
            total_errors = pool.read_errors + pool.write_errors + pool.checksum_errors
            if total_errors > 0:
                lines.append(f"      {c.RED}Errors: {pool.read_errors}R/{pool.write_errors}W/{pool.checksum_errors}C{c.RESET}")
            
            if pool.issues:
                for issue in pool.issues:
                    lines.append(f"      {c.YELLOW}⚠ {issue}{c.RESET}")
    
    # ARC Stats (if ZFS)
    if diag.arc_size_gb > 0:
        lines.append(f"\n  {c.BOLD}ZFS ARC Cache{c.RESET}")
        lines.append(f"  {'─' * 56}")
        arc_color = c.GREEN if diag.arc_hit_ratio > 0.9 else c.YELLOW if diag.arc_hit_ratio > 0.7 else c.RED
        lines.append(f"    Size:      {diag.arc_size_gb:.1f} GB")
        lines.append(f"    Hit Ratio: {arc_color}{diag.arc_hit_ratio*100:.1f}%{c.RESET}")
    
    # NFS
    if diag.nfs_exports or diag.nfs_clients > 0:
        lines.append(f"\n  {c.BOLD}NFS{c.RESET}")
        lines.append(f"  {'─' * 56}")
        lines.append(f"    Exports: {len(diag.nfs_exports)}")
        lines.append(f"    Clients: {diag.nfs_clients}")
    
    # Trends
    if diag.trends.get('usage'):
        trend = diag.trends['usage']
        lines.append(f"\n  {c.BOLD}Usage Trend{c.RESET}")
        lines.append(f"  {'─' * 56}")
        
        trend_str = trend.get('trend', 'unknown')
        trend_color = c.RED if trend_str == 'accelerating' else c.YELLOW if trend_str == 'increasing' else c.GREEN
        lines.append(f"    Trend:        {trend_color}{trend_str}{c.RESET}")
        
        d1 = trend.get('first_derivative')
        if d1:
            lines.append(f"    Fill Rate:    {d1/1024**3:+.2f} GB/day")
        
        days = trend.get('days_until_full')
        if days and days < 365:
            days_color = c.RED if days < 30 else c.YELLOW if days < 90 else c.GREEN
            lines.append(f"    Days to Full: {days_color}{days:.0f} days{c.RESET}")
    
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
    for rec in diag.recommendations[:8]:
        lines.append(f"    {c.CYAN}→{c.RESET} {rec}")
    
    lines.append("")
    return '\n'.join(lines)
