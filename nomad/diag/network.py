"""
Network Diagnostics for infrastructure paths.

Provides detailed analysis of network health and issues:
- Latency trends and jitter analysis
- Throughput degradation detection
- TCP retransmit correlation
- Business hours vs off-hours comparison
- Path comparison (direct vs switch)

Integrates with:
- analysis/derivatives.py for trend detection
- collectors/network_perf.py data
"""

import sqlite3
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
class NetworkDiagnostic:
    """Container for network diagnostic information."""
    source_host: str
    dest_host: str
    path_type: str
    current_status: str
    last_seen: Optional[datetime]
    
    # Current metrics
    latency_avg_ms: float = 0.0
    latency_jitter_ms: float = 0.0
    packet_loss_pct: float = 0.0
    throughput_mbps: float = 0.0
    tcp_retrans: int = 0
    
    # Historical comparison
    samples_count: int = 0
    avg_throughput_mbps: float = 0.0
    min_throughput_mbps: float = 0.0
    max_throughput_mbps: float = 0.0
    
    # Time-based analysis
    weekday_avg_mbps: float = 0.0
    weekend_avg_mbps: float = 0.0
    business_hours_avg_mbps: float = 0.0
    off_hours_avg_mbps: float = 0.0
    
    # Trends
    trends: dict = field(default_factory=dict)
    
    # Analysis results
    potential_causes: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)


def get_network_state(db_path: str, source: str = None, dest: str = None) -> Optional[dict]:
    """Get current network state from database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        if source and dest:
            row = conn.execute("""
                SELECT * FROM network_perf
                WHERE source_host = ? AND dest_host = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (source, dest)).fetchone()
        elif dest:
            row = conn.execute("""
                SELECT * FROM network_perf
                WHERE dest_host = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (dest,)).fetchone()
        elif source:
            row = conn.execute("""
                SELECT * FROM network_perf
                WHERE source_host = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (source,)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM network_perf
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()
        
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error getting network state: {e}")
        return None


def get_state_history(db_path: str, source: str = None, dest: str = None, hours: int = 168) -> list:
    """Get network state history (default: 1 week)."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        if source and dest:
            rows = conn.execute("""
                SELECT * FROM network_perf 
                WHERE source_host = ? AND dest_host = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """, (source, dest, since)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM network_perf 
                WHERE timestamp > ?
                ORDER BY timestamp DESC
            """, (since,)).fetchall()
        
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error getting state history: {e}")
        return []


def analyze_throughput_trend(history: list) -> dict:
    """Analyze throughput trend using derivatives."""
    if not history or not HAS_DERIVATIVES:
        return {}
    
    analyzer = DerivativeAnalyzer(window_size=len(history))
    
    for record in history:
        timestamp = record.get('timestamp')
        throughput = record.get('throughput_mbps', 0)
        
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
        
        if throughput:
            analyzer.add_point(timestamp, throughput)
    
    analysis = analyzer.analyze()
    
    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_latency_trend(history: list) -> dict:
    """Analyze latency trend."""
    if not history or not HAS_DERIVATIVES:
        return {}
    
    analyzer = DerivativeAnalyzer(window_size=len(history))
    
    for record in history:
        timestamp = record.get('timestamp')
        latency = record.get('ping_avg_ms', 0)
        
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
        
        if latency:
            analyzer.add_point(timestamp, latency)
    
    analysis = analyzer.analyze()
    
    return {
        'current': analysis.current_value,
        'trend': analysis.trend.value if analysis.trend else 'unknown',
        'first_derivative': analysis.first_derivative,
        'alert_level': analysis.alert_level.value if analysis.alert_level else 'normal',
    }


def analyze_time_patterns(history: list) -> dict:
    """Analyze performance by time of day and day of week."""
    if not history:
        return {}
    
    weekday_samples = []
    weekend_samples = []
    business_samples = []  # 9am-5pm
    off_hours_samples = []
    
    for record in history:
        timestamp = record.get('timestamp')
        throughput = record.get('throughput_mbps', 0)
        
        if not throughput:
            continue
        
        if isinstance(timestamp, str):
            try:
                timestamp = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
        
        # Day of week (0=Monday, 6=Sunday)
        if timestamp.weekday() < 5:
            weekday_samples.append(throughput)
        else:
            weekend_samples.append(throughput)
        
        # Hour of day
        hour = timestamp.hour
        if 9 <= hour < 17 and timestamp.weekday() < 5:
            business_samples.append(throughput)
        else:
            off_hours_samples.append(throughput)
    
    return {
        'weekday_avg': sum(weekday_samples) / len(weekday_samples) if weekday_samples else 0,
        'weekend_avg': sum(weekend_samples) / len(weekend_samples) if weekend_samples else 0,
        'business_hours_avg': sum(business_samples) / len(business_samples) if business_samples else 0,
        'off_hours_avg': sum(off_hours_samples) / len(off_hours_samples) if off_hours_samples else 0,
        'weekday_count': len(weekday_samples),
        'weekend_count': len(weekend_samples),
    }


def analyze_potential_causes(state: dict, history: list, trends: dict, time_patterns: dict) -> list:
    """Analyze data to suggest potential causes for network issues."""
    causes = []
    
    if not state:
        causes.append({
            'cause': 'No network data available',
            'confidence': 'high',
            'detail': 'No recent measurements found for this path'
        })
        return causes
    
    status = state.get('status', '')
    
    # Check packet loss
    loss = state.get('ping_loss_pct', 0)
    if loss > 5:
        causes.append({
            'cause': 'High Packet Loss',
            'confidence': 'high',
            'detail': f'{loss:.1f}% packet loss - indicates network instability'
        })
    elif loss > 1:
        causes.append({
            'cause': 'Elevated Packet Loss',
            'confidence': 'medium',
            'detail': f'{loss:.1f}% packet loss - minor network issues'
        })
    
    # Check latency
    latency = state.get('ping_avg_ms', 0)
    jitter = state.get('ping_mdev_ms', 0)
    
    if latency > 100:
        causes.append({
            'cause': 'High Latency',
            'confidence': 'high',
            'detail': f'{latency:.1f}ms average latency - significantly impacts performance'
        })
    elif latency > 50:
        causes.append({
            'cause': 'Elevated Latency',
            'confidence': 'medium',
            'detail': f'{latency:.1f}ms average latency'
        })
    
    if jitter > 20:
        causes.append({
            'cause': 'High Jitter',
            'confidence': 'high',
            'detail': f'{jitter:.1f}ms jitter - indicates network congestion or instability'
        })
    
    # Check TCP retransmits
    retrans = state.get('tcp_retrans', 0)
    if retrans > 100:
        causes.append({
            'cause': 'Excessive TCP Retransmits',
            'confidence': 'high',
            'detail': f'{retrans} retransmits - significant packet loss or corruption'
        })
    elif retrans > 10:
        causes.append({
            'cause': 'Elevated TCP Retransmits',
            'confidence': 'medium',
            'detail': f'{retrans} retransmits'
        })
    
    # Check throughput
    throughput = state.get('throughput_mbps', 0)
    if throughput and throughput < 100:
        causes.append({
            'cause': 'Low Throughput',
            'confidence': 'medium',
            'detail': f'{throughput:.1f} Mbps - below expected performance'
        })
    
    # Check business hours vs off-hours (congestion indicator)
    if time_patterns:
        biz = time_patterns.get('business_hours_avg', 0)
        off = time_patterns.get('off_hours_avg', 0)
        if biz and off and off > 0:
            ratio = biz / off
            if ratio < 0.7:
                pct_drop = (1 - ratio) * 100
                causes.append({
                    'cause': 'Business Hours Congestion',
                    'confidence': 'high',
                    'detail': f'{pct_drop:.0f}% throughput drop during 9am-5pm weekdays'
                })
            elif ratio < 0.85:
                pct_drop = (1 - ratio) * 100
                causes.append({
                    'cause': 'Mild Business Hours Impact',
                    'confidence': 'medium',
                    'detail': f'{pct_drop:.0f}% throughput drop during business hours'
                })
    
    # Check trends
    if trends.get('throughput', {}).get('trend') == 'decreasing':
        causes.append({
            'cause': 'Declining Throughput Trend',
            'confidence': 'medium',
            'detail': 'Throughput has been decreasing over time'
        })
    
    if trends.get('latency', {}).get('trend') == 'increasing':
        causes.append({
            'cause': 'Increasing Latency Trend',
            'confidence': 'medium',
            'detail': 'Latency has been increasing over time'
        })
    
    if not causes:
        causes.append({
            'cause': 'No obvious issues detected',
            'confidence': 'low',
            'detail': 'Network path appears healthy'
        })
    
    return causes


def generate_recommendations(causes: list, state: dict, time_patterns: dict) -> list:
    """Generate actionable recommendations based on analysis."""
    recommendations = []
    
    for cause in causes:
        cause_name = cause['cause']
        
        if 'Packet Loss' in cause_name:
            recommendations.append('Check cable connections and switch ports')
            recommendations.append('Verify switch port error counters: show interface counters errors')
            recommendations.append('Test with different cables or ports')
        
        elif 'Latency' in cause_name:
            recommendations.append('Check for routing changes: traceroute <dest>')
            recommendations.append('Verify no bandwidth-heavy processes running')
            recommendations.append('Check switch/router CPU utilization')
        
        elif 'Jitter' in cause_name:
            recommendations.append('Network jitter often indicates congestion')
            recommendations.append('Check for broadcast storms or network loops')
            recommendations.append('Consider QoS policies for critical traffic')
        
        elif 'Retransmit' in cause_name:
            recommendations.append('TCP retransmits indicate packet loss')
            recommendations.append('Check for duplex mismatch: ethtool <interface>')
            recommendations.append('Verify MTU settings match across path')
        
        elif 'Congestion' in cause_name:
            recommendations.append('Consider dedicated network path for HPC traffic')
            recommendations.append('Evaluate traffic shaping or QoS policies')
            recommendations.append('Schedule large transfers for off-hours')
            recommendations.append('Document congestion pattern for infrastructure upgrade proposal')
        
        elif 'Low Throughput' in cause_name:
            recommendations.append('Run iperf3 test to isolate bottleneck: iperf3 -c <dest>')
            recommendations.append('Check NIC link speed: ethtool <interface>')
            recommendations.append('Verify no half-duplex links in path')
    
    if not recommendations:
        recommendations.append('Network appears healthy - no action required')
    
    return list(dict.fromkeys(recommendations))  # Remove duplicates


def diagnose_network(
    db_path: str,
    source: str = None,
    dest: str = None,
    hours: int = 168,  # 1 week default
) -> Optional[NetworkDiagnostic]:
    """
    Generate comprehensive diagnostics for a network path.
    
    Args:
        db_path: Path to NØMAD database
        source: Source hostname (optional)
        dest: Destination hostname (optional)
        hours: Hours of history to analyze
    
    Returns:
        NetworkDiagnostic object or None if no data found
    """
    # Get current state
    state = get_network_state(db_path, source, dest)
    
    # Get history
    history = get_state_history(db_path, source, dest, hours)
    
    if not state and not history:
        return None
    
    # Use state values or derive from history
    if state:
        src = state.get('source_host', source or 'unknown')
        dst = state.get('dest_host', dest or 'unknown')
        path_type = state.get('path_type', 'unknown')
    else:
        src = source or 'unknown'
        dst = dest or 'unknown'
        path_type = 'unknown'
    
    diag = NetworkDiagnostic(
        source_host=src,
        dest_host=dst,
        path_type=path_type,
        current_status=state.get('status', 'unknown') if state else 'no_data',
        last_seen=state.get('timestamp') if state else None,
    )
    
    if state:
        diag.latency_avg_ms = state.get('ping_avg_ms', 0) or 0
        diag.latency_jitter_ms = state.get('ping_mdev_ms', 0) or 0
        diag.packet_loss_pct = state.get('ping_loss_pct', 0) or 0
        diag.throughput_mbps = state.get('throughput_mbps', 0) or 0
        diag.tcp_retrans = state.get('tcp_retrans', 0) or 0
    
    # Calculate historical stats
    if history:
        throughputs = [h.get('throughput_mbps', 0) for h in history if h.get('throughput_mbps')]
        if throughputs:
            diag.samples_count = len(throughputs)
            diag.avg_throughput_mbps = sum(throughputs) / len(throughputs)
            diag.min_throughput_mbps = min(throughputs)
            diag.max_throughput_mbps = max(throughputs)
    
    # Analyze time patterns
    time_patterns = analyze_time_patterns(history)
    if time_patterns:
        diag.weekday_avg_mbps = time_patterns.get('weekday_avg', 0)
        diag.weekend_avg_mbps = time_patterns.get('weekend_avg', 0)
        diag.business_hours_avg_mbps = time_patterns.get('business_hours_avg', 0)
        diag.off_hours_avg_mbps = time_patterns.get('off_hours_avg', 0)
    
    # Analyze trends
    diag.trends = {
        'throughput': analyze_throughput_trend(history),
        'latency': analyze_latency_trend(history),
    }
    
    # Determine causes
    diag.potential_causes = analyze_potential_causes(state, history, diag.trends, time_patterns)
    
    # Generate recommendations
    diag.recommendations = generate_recommendations(diag.potential_causes, state, time_patterns)
    
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


def format_diagnostic(diag: NetworkDiagnostic) -> str:
    """Format diagnostic for terminal output."""
    c = Colors
    lines = []
    
    # Header
    path_str = f"{diag.source_host} → {diag.dest_host}"
    lines.append(f"\n  {c.BOLD}NØMAD Network Diagnostic{c.RESET} — {c.CYAN}{path_str}{c.RESET}")
    lines.append(f"  Path type: {diag.path_type}")
    lines.append(f"  {'─' * 56}")
    
    # Current State
    status_color = c.GREEN if diag.current_status == 'healthy' else c.YELLOW if diag.current_status == 'degraded' else c.RED
    lines.append(f"\n  {c.BOLD}Status:{c.RESET} {status_color}{diag.current_status}{c.RESET}")
    
    if diag.last_seen:
        lines.append(f"  {c.BOLD}Last Test:{c.RESET} {diag.last_seen}")
    
    # Current Metrics
    lines.append(f"\n  {c.BOLD}Current Metrics{c.RESET}")
    lines.append(f"  {'─' * 56}")
    
    # Latency
    lat_color = c.RED if diag.latency_avg_ms > 50 else c.YELLOW if diag.latency_avg_ms > 20 else c.GREEN
    lines.append(f"    Latency:      {lat_color}{diag.latency_avg_ms:.1f} ms{c.RESET} (jitter: {diag.latency_jitter_ms:.1f} ms)")
    
    # Packet loss
    loss_color = c.RED if diag.packet_loss_pct > 1 else c.GREEN
    lines.append(f"    Packet Loss:  {loss_color}{diag.packet_loss_pct:.1f}%{c.RESET}")
    
    # Throughput
    if diag.throughput_mbps:
        tp_color = c.GREEN if diag.throughput_mbps > 500 else c.YELLOW if diag.throughput_mbps > 100 else c.RED
        lines.append(f"    Throughput:   {tp_color}{diag.throughput_mbps:.1f} Mbps{c.RESET}")
    
    # TCP retransmits
    if diag.tcp_retrans:
        ret_color = c.RED if diag.tcp_retrans > 50 else c.YELLOW if diag.tcp_retrans > 10 else c.GREEN
        lines.append(f"    Retransmits:  {ret_color}{diag.tcp_retrans}{c.RESET}")
    
    # Historical Summary
    if diag.samples_count > 0:
        lines.append(f"\n  {c.BOLD}Historical Summary{c.RESET} ({diag.samples_count} samples)")
        lines.append(f"  {'─' * 56}")
        lines.append(f"    Avg Throughput:  {diag.avg_throughput_mbps:.1f} Mbps")
        lines.append(f"    Min/Max:         {diag.min_throughput_mbps:.1f} / {diag.max_throughput_mbps:.1f} Mbps")
    
    # Time-based Analysis
    if diag.business_hours_avg_mbps or diag.off_hours_avg_mbps:
        lines.append(f"\n  {c.BOLD}Time-based Analysis{c.RESET}")
        lines.append(f"  {'─' * 56}")
        
        if diag.weekday_avg_mbps and diag.weekend_avg_mbps:
            lines.append(f"    Weekday Avg:      {diag.weekday_avg_mbps:.1f} Mbps")
            lines.append(f"    Weekend Avg:      {diag.weekend_avg_mbps:.1f} Mbps")
        
        if diag.business_hours_avg_mbps and diag.off_hours_avg_mbps:
            biz = diag.business_hours_avg_mbps
            off = diag.off_hours_avg_mbps
            diff_pct = ((off - biz) / off * 100) if off > 0 else 0
            
            biz_color = c.RED if diff_pct > 20 else c.YELLOW if diff_pct > 10 else c.GREEN
            lines.append(f"    Business Hours:   {biz_color}{biz:.1f} Mbps{c.RESET}")
            lines.append(f"    Off Hours:        {off:.1f} Mbps")
            if diff_pct > 5:
                lines.append(f"    {c.YELLOW}↓ {diff_pct:.0f}% drop during business hours{c.RESET}")
    
    # Trends
    if diag.trends:
        lines.append(f"\n  {c.BOLD}Trends{c.RESET}")
        lines.append(f"  {'─' * 56}")
        for name, trend in diag.trends.items():
            if trend:
                trend_str = trend.get('trend', 'unknown')
                if name == 'throughput':
                    trend_color = c.RED if trend_str == 'decreasing' else c.GREEN if trend_str == 'increasing' else c.GRAY
                else:  # latency - inverse
                    trend_color = c.RED if trend_str == 'increasing' else c.GREEN if trend_str == 'decreasing' else c.GRAY
                lines.append(f"    {name.capitalize():12} {trend_color}{trend_str}{c.RESET}")
    
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
