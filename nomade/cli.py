"""
NOMADE CLI

Command-line interface for NOMADE monitoring and analysis.

Commands:
    collect     Run collectors once or continuously
    analyze     Analyze collected data
    status      Show system status
    alerts      Show and manage alerts
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import toml

from nomade.collectors.base import registry
from nomade.collectors.disk import DiskCollector
from nomade.collectors.slurm import SlurmCollector
from nomade.collectors.job_metrics import JobMetricsCollector
from nomade.collectors.iostat import IOStatCollector
from nomade.collectors.mpstat import MPStatCollector
from nomade.collectors.vmstat import VMStatCollector
from nomade.collectors.node_state import NodeStateCollector
from nomade.collectors.gpu import GPUCollector
from nomade.collectors.nfs import NFSCollector
from nomade.analysis.derivatives import (
    DerivativeAnalyzer,
    analyze_disk_trend,
    AlertLevel,
)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('nomade')


def load_config(config_path: Path) -> dict[str, Any]:
    """Load TOML configuration file."""
    if not config_path.exists():
        raise click.ClickException(f"Config file not found: {config_path}")
    
    with open(config_path) as f:
        return toml.load(f)


def get_db_path(config: dict[str, Any]) -> Path:
    """Get database path from config."""
    data_dir = Path(config.get('general', {}).get('data_dir', '/var/lib/nomade'))
    return data_dir / 'nomade.db'


@click.group()
@click.option('-c', '--config', 'config_path', 
              type=click.Path(),
              default='/etc/nomade/nomade.toml',
              help='Path to config file')
@click.option('-v', '--verbose', is_flag=True, help='Enable debug logging')
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """NØMADE - NØde MAnagement DEvice
    
    Lightweight HPC monitoring and prediction tool.
    """
    ctx.ensure_object(dict)
    
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Try to load config, but don't fail if not found
    config_file = Path(config_path)
    if config_file.exists():
        try:
            ctx.obj['config'] = load_config(config_file)
            ctx.obj['config_path'] = config_path
        except Exception:
            ctx.obj['config'] = {}
            ctx.obj['config_path'] = None
    else:
        ctx.obj['config'] = {}
        ctx.obj['config_path'] = None

@cli.command()
@click.option('--collector', '-C', multiple=True, help='Specific collectors to run')
@click.option('--once', is_flag=True, help='Run once and exit')
@click.option('--interval', '-i', type=int, default=60, help='Collection interval (seconds)')
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def collect(ctx: click.Context, collector: tuple, once: bool, interval: int, db: str) -> None:
    """Run data collectors.
    
    By default, runs all enabled collectors continuously.
    Use --once to run a single collection cycle.
    """
    config = ctx.obj['config']
    
    # Determine database path
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    click.echo(f"Database: {db_path}")
    
    # Initialize collectors
    collectors = []
    
    # Disk collector
    disk_config = config.get('collectors', {}).get('disk', {})
    if not collector or 'disk' in collector:
        if disk_config.get('enabled', True):
            collectors.append(DiskCollector(disk_config, db_path))
    
    # SLURM collector
    slurm_config = config.get('collectors', {}).get('slurm', {})
    if not collector or 'slurm' in collector:
        if slurm_config.get('enabled', True):
            collectors.append(SlurmCollector(slurm_config, db_path))
    
    # Job metrics collector
    job_metrics_config = config.get('collectors', {}).get('job_metrics', {})
    if not collector or 'job_metrics' in collector:
        if job_metrics_config.get('enabled', True):
            collectors.append(JobMetricsCollector(job_metrics_config, db_path))
    
    # IOStat collector
    iostat_config = config.get('collectors', {}).get('iostat', {})
    if not collector or 'iostat' in collector:
        if iostat_config.get('enabled', True):
            collectors.append(IOStatCollector(iostat_config, db_path))
    
    # MPStat collector
    mpstat_config = config.get('collectors', {}).get('mpstat', {})
    if not collector or 'mpstat' in collector:
        if mpstat_config.get('enabled', True):
            collectors.append(MPStatCollector(mpstat_config, db_path))
    
    # VMStat collector
    vmstat_config = config.get('collectors', {}).get('vmstat', {})
    if not collector or 'vmstat' in collector:
        if vmstat_config.get('enabled', True):
            collectors.append(VMStatCollector(vmstat_config, db_path))
    
    # Node state collector
    node_state_config = config.get('collectors', {}).get('node_state', {})
    if not collector or 'node_state' in collector:
        if node_state_config.get('enabled', True):
            collectors.append(NodeStateCollector(node_state_config, db_path))
    
    # GPU collector (graceful skip if no GPU)
    gpu_config = config.get('collectors', {}).get('gpu', {})
    if not collector or 'gpu' in collector:
        if gpu_config.get('enabled', True):
            collectors.append(GPUCollector(gpu_config, db_path))
    
    # NFS collector (graceful skip if no NFS)
    nfs_config = config.get('collectors', {}).get('nfs', {})
    if not collector or 'nfs' in collector:
        if nfs_config.get('enabled', True):
            collectors.append(NFSCollector(nfs_config, db_path))
    
    if not collectors:
        raise click.ClickException("No collectors enabled")
    
    click.echo(f"Running collectors: {[c.name for c in collectors]}")
    
    if once:
        # Single collection cycle
        for c in collectors:
            result = c.run()
            status = click.style('✓', fg='green') if result.success else click.style('✗', fg='red')
            click.echo(f"  {status} {c.name}: {result.records_collected} records")
    else:
        # Continuous collection
        click.echo(f"Starting continuous collection (interval: {interval}s)")
        click.echo("Press Ctrl+C to stop")
        
        try:
            while True:
                for c in collectors:
                    result = c.run()
                    status = '✓' if result.success else '✗'
                    click.echo(f"[{datetime.now():%H:%M:%S}] {status} {c.name}: {result.records_collected} records")
                
                time.sleep(interval)
        except KeyboardInterrupt:
            click.echo("\nStopping collectors")


@cli.command()
@click.option('--path', '-p', default='/localscratch', help='Filesystem path to analyze')
@click.option('--hours', '-h', type=int, default=24, help='Hours of history')
@click.option('--limit-gb', type=float, help='Disk limit in GB for projection')
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def analyze(ctx: click.Context, path: str, hours: int, limit_gb: float, db: str) -> None:
    """Analyze filesystem trends using derivatives.
    
    Shows current trend, rate of change, and projections.
    """
    config = ctx.obj['config']
    
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    if not db_path.exists():
        raise click.ClickException(f"Database not found: {db_path}")
    
    # Get historical data
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute(
        """
        SELECT timestamp, used_bytes, used_percent, total_bytes
        FROM filesystems
        WHERE path = ?
          AND timestamp > datetime('now', ?)
        ORDER BY timestamp ASC
        """,
        (path, f'-{hours} hours')
    ).fetchall()
    
    if not rows:
        raise click.ClickException(f"No data found for {path}")
    
    # Convert to history format
    history = [dict(row) for row in rows]
    
    # Determine limit
    limit_bytes = None
    if limit_gb:
        limit_bytes = int(limit_gb * 1e9)
    elif history:
        limit_bytes = history[-1]['total_bytes']
    
    # Analyze
    analysis = analyze_disk_trend(history, limit_bytes=limit_bytes)
    
    # Display results
    click.echo()
    click.echo(click.style(f"═══ Analysis: {path} ═══", bold=True))
    click.echo(f"  Records:     {analysis.n_points}")
    click.echo(f"  Time span:   {analysis.time_span_hours:.1f} hours")
    click.echo()
    
    # Current state
    current_gb = analysis.current_value / 1e9
    total_gb = limit_bytes / 1e9 if limit_bytes else 0
    pct = (current_gb / total_gb * 100) if total_gb else 0
    
    click.echo(f"  Current:     {current_gb:.2f} GB / {total_gb:.2f} GB ({pct:.1f}%)")
    
    # Trend
    trend_colors = {
        'stable': 'green',
        'increasing_linear': 'yellow',
        'decreasing_linear': 'cyan',
        'accelerating_growth': 'red',
        'decelerating_growth': 'yellow',
        'accelerating_decline': 'cyan',
        'decelerating_decline': 'green',
        'unknown': 'white',
    }
    trend_color = trend_colors.get(analysis.trend.value, 'white')
    click.echo(f"  Trend:       {click.style(analysis.trend.value, fg=trend_color)}")
    
    # Derivatives
    if analysis.first_derivative:
        rate_gb = analysis.first_derivative / 1e9
        direction = "↑" if rate_gb > 0 else "↓" if rate_gb < 0 else "→"
        click.echo(f"  Rate:        {direction} {abs(rate_gb):.4f} GB/day")
    
    if analysis.second_derivative:
        accel_gb = analysis.second_derivative / 1e9
        direction = "↑↑" if accel_gb > 0 else "↓↓" if accel_gb < 0 else "→→"
        click.echo(f"  Accel:       {direction} {abs(accel_gb):.6f} GB/day²")
    
    # Projections
    click.echo()
    if analysis.projected_value_1d:
        proj_1d_gb = analysis.projected_value_1d / 1e9
        click.echo(f"  In 1 day:    {proj_1d_gb:.2f} GB")
    
    if analysis.projected_value_7d:
        proj_7d_gb = analysis.projected_value_7d / 1e9
        click.echo(f"  In 7 days:   {proj_7d_gb:.2f} GB")
    
    if analysis.days_until_limit:
        click.echo(f"  Days until full: {click.style(f'{analysis.days_until_limit:.1f}', fg='red')}")
    
    # Alert level
    click.echo()
    alert_colors = {
        'none': 'green',
        'info': 'blue',
        'warning': 'yellow',
        'critical': 'red',
    }
    alert_color = alert_colors.get(analysis.alert_level.value, 'white')
    click.echo(f"  Alert:       {click.style(analysis.alert_level.value.upper(), fg=alert_color)}")
    click.echo()


@cli.command()
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def status(ctx: click.Context, db: str) -> None:
    """Show system status overview."""
    config = ctx.obj['config']
    
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    if not db_path.exists():
        raise click.ClickException(f"Database not found: {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    click.echo()
    click.echo(click.style("═══ NØMADE Status ═══", bold=True))
    click.echo()
    
    # Filesystem status
    click.echo(click.style("Filesystems:", bold=True))
    fs_rows = conn.execute(
        """
        SELECT path, 
               round(used_bytes/1e9, 2) as used_gb,
               round(total_bytes/1e9, 2) as total_gb,
               round(used_percent, 1) as pct,
               timestamp
        FROM filesystems f1
        WHERE timestamp = (
            SELECT MAX(timestamp) FROM filesystems f2 WHERE f2.path = f1.path
        )
        ORDER BY path
        """
    ).fetchall()
    
    for row in fs_rows:
        pct = row['pct']
        color = 'green' if pct < 70 else 'yellow' if pct < 85 else 'red'
        bar_len = int(pct / 5)
        bar = '█' * bar_len + '░' * (20 - bar_len)
        click.echo(f"  {row['path']:<20} [{bar}] {click.style(f'{pct}%', fg=color):>6} ({row['used_gb']}/{row['total_gb']} GB)")
    
    click.echo()
    
    # Queue status
    click.echo(click.style("Queue:", bold=True))
    queue_rows = conn.execute(
        """
        SELECT partition, pending_jobs, running_jobs, total_jobs, timestamp
        FROM queue_state q1
        WHERE timestamp = (
            SELECT MAX(timestamp) FROM queue_state q2 WHERE q2.partition = q1.partition
        )
        ORDER BY partition
        """
    ).fetchall()
    
    if queue_rows:
        for row in queue_rows:
            click.echo(f"  {row['partition']:<15} Running: {row['running_jobs']:>3}  Pending: {row['pending_jobs']:>3}")
    else:
        click.echo("  No queue data")
    
    click.echo()
    
    # I/O status (from iostat)
    click.echo(click.style("I/O:", bold=True))
    try:
        iostat_row = conn.execute(
            """
            SELECT iowait_percent, user_percent, system_percent, idle_percent, timestamp
            FROM iostat_cpu
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()
        
        if iostat_row:
            iowait = iostat_row['iowait_percent']
            iowait_color = 'green' if iowait < 10 else 'yellow' if iowait < 30 else 'red'
            click.echo(f"  CPU iowait:    {click.style(f'{iowait:.1f}%', fg=iowait_color)}")
            click.echo(f"  CPU user/sys:  {iostat_row['user_percent']:.1f}% / {iostat_row['system_percent']:.1f}%")
            
            # Device utilization
            device_rows = conn.execute(
                """
                SELECT device, util_percent, write_kb_per_sec, write_await_ms
                FROM iostat_device
                WHERE timestamp = (SELECT MAX(timestamp) FROM iostat_device)
                  AND device NOT LIKE 'loop%'
                  AND device NOT LIKE 'dm-%'
                ORDER BY util_percent DESC
                LIMIT 3
                """
            ).fetchall()
            
            for dev in device_rows:
                util = dev['util_percent']
                util_color = 'green' if util < 50 else 'yellow' if util < 80 else 'red'
                click.echo(f"  {dev['device']:<12} util: {click.style(f'{util:.1f}%', fg=util_color):<8} write: {dev['write_kb_per_sec']:.0f} KB/s  latency: {dev['write_await_ms']:.1f}ms")
        else:
            click.echo("  No iostat data (run: nomade collect -C iostat --once)")
    except sqlite3.OperationalError:
        click.echo("  No iostat data (table not created yet)")
    
    click.echo()
    
    # CPU Core status (from mpstat)
    click.echo(click.style("CPU Cores:", bold=True))
    try:
        mpstat_row = conn.execute(
            """
            SELECT num_cores, avg_busy_percent, max_busy_percent, min_busy_percent,
                   std_busy_percent, busy_spread, imbalance_ratio, 
                   cores_idle, cores_saturated, timestamp
            FROM mpstat_summary
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()
        
        if mpstat_row:
            avg_busy = mpstat_row['avg_busy_percent']
            busy_color = 'green' if avg_busy < 50 else 'yellow' if avg_busy < 80 else 'red'
            
            imbalance = mpstat_row['imbalance_ratio']
            imbalance_color = 'green' if imbalance < 0.3 else 'yellow' if imbalance < 0.6 else 'red'
            
            click.echo(f"  Cores:         {mpstat_row['num_cores']}")
            click.echo(f"  Avg busy:      {click.style(f'{avg_busy:.1f}%', fg=busy_color)}")
            click.echo(f"  Range:         {mpstat_row['min_busy_percent']:.1f}% - {mpstat_row['max_busy_percent']:.1f}% (spread: {mpstat_row['busy_spread']:.1f}%)")
            click.echo(f"  Imbalance:     {click.style(f'{imbalance:.2f}', fg=imbalance_color)} (std/avg)")
            
            if mpstat_row['cores_idle'] > 0:
                click.echo(f"  Idle cores:    {click.style(str(mpstat_row['cores_idle']), fg='cyan')} (<5% busy)")
            if mpstat_row['cores_saturated'] > 0:
                click.echo(f"  Saturated:     {click.style(str(mpstat_row['cores_saturated']), fg='red')} (>95% busy)")
        else:
            click.echo("  No mpstat data (run: nomade collect -C mpstat --once)")
    except sqlite3.OperationalError:
        click.echo("  No mpstat data (table not created yet)")
    
    click.echo()
    
    # Memory status (from vmstat)
    click.echo(click.style("Memory:", bold=True))
    try:
        vmstat_row = conn.execute(
            """
            SELECT swap_used_kb, free_kb, buffer_kb, cache_kb,
                   swap_in_kb, swap_out_kb, procs_blocked,
                   memory_pressure, timestamp
            FROM vmstat
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()
        
        if vmstat_row:
            free_gb = vmstat_row['free_kb'] / 1024 / 1024
            cache_gb = vmstat_row['cache_kb'] / 1024 / 1024
            swap_mb = vmstat_row['swap_used_kb'] / 1024
            pressure = vmstat_row['memory_pressure']
            
            pressure_color = 'green' if pressure < 0.3 else 'yellow' if pressure < 0.6 else 'red'
            swap_color = 'green' if swap_mb < 100 else 'yellow' if swap_mb < 1000 else 'red'
            
            click.echo(f"  Free:          {free_gb:.2f} GB")
            click.echo(f"  Cache:         {cache_gb:.2f} GB")
            click.echo(f"  Swap used:     {click.style(f'{swap_mb:.0f} MB', fg=swap_color)}")
            click.echo(f"  Pressure:      {click.style(f'{pressure:.2f}', fg=pressure_color)}")
            
            if vmstat_row['procs_blocked'] > 0:
                click.echo(f"  Blocked procs: {click.style(str(vmstat_row['procs_blocked']), fg='yellow')}")
            if vmstat_row['swap_in_kb'] > 0 or vmstat_row['swap_out_kb'] > 0:
                click.echo(f"  Swap activity: {click.style('ACTIVE', fg='red')} (in:{vmstat_row['swap_in_kb']} out:{vmstat_row['swap_out_kb']} KB/s)")
        else:
            click.echo("  No vmstat data")
    except sqlite3.OperationalError:
        click.echo("  No vmstat data (table not created yet)")
    
    click.echo()
    
    # Node status (from scontrol)
    click.echo(click.style("Nodes:", bold=True))
    try:
        node_rows = conn.execute(
            """
            SELECT node_name, state, cpus_alloc, cpus_total,
                   memory_alloc_mb, memory_total_mb, cpu_load, reason
            FROM node_state
            WHERE timestamp = (SELECT MAX(timestamp) FROM node_state)
            ORDER BY node_name
            """
        ).fetchall()
        
        if node_rows:
            for node in node_rows:
                state = node['state']
                state_color = 'green' if state in ('IDLE', 'MIXED', 'ALLOCATED') else 'yellow' if 'DRAIN' in state else 'red'
                
                cpu_pct = (node['cpus_alloc'] / node['cpus_total'] * 100) if node['cpus_total'] else 0
                mem_pct = (node['memory_alloc_mb'] / node['memory_total_mb'] * 100) if node['memory_total_mb'] else 0
                
                click.echo(f"  {node['node_name']:<15} {click.style(state, fg=state_color):<12} CPU: {node['cpus_alloc']}/{node['cpus_total']} ({cpu_pct:.0f}%)  Mem: {mem_pct:.0f}%  Load: {node['cpu_load']:.2f}")
                
                if node['reason']:
                    click.echo(f"    └─ Reason: {click.style(node['reason'], fg='yellow')}")
        else:
            click.echo("  No node data")
    except sqlite3.OperationalError:
        click.echo("  No node data (table not created yet)")
    
    click.echo()
    
    # GPU status (if available)
    try:
        gpu_rows = conn.execute(
            """
            SELECT gpu_index, gpu_name, gpu_util_percent, memory_util_percent,
                   memory_used_mb, memory_total_mb, temperature_c, power_draw_w
            FROM gpu_stats
            WHERE timestamp = (SELECT MAX(timestamp) FROM gpu_stats)
            ORDER BY gpu_index
            """
        ).fetchall()
        
        if gpu_rows:
            click.echo(click.style("GPUs:", bold=True))
            for gpu in gpu_rows:
                util = gpu['gpu_util_percent']
                util_color = 'green' if util < 50 else 'yellow' if util < 80 else 'red'
                temp = gpu['temperature_c']
                temp_color = 'green' if temp < 70 else 'yellow' if temp < 85 else 'red'
                
                mem_pct = (gpu['memory_used_mb'] / gpu['memory_total_mb'] * 100) if gpu['memory_total_mb'] else 0
                power = gpu['power_draw_w']
                
                click.echo(f"  GPU {gpu['gpu_index']}: {gpu['gpu_name']}")
                click.echo(f"    Util: {click.style(f'{util:.0f}%', fg=util_color)}  Mem: {mem_pct:.0f}%  Temp: {click.style(f'{temp}°C', fg=temp_color)}  Power: {power:.0f}W")
            click.echo()
    except sqlite3.OperationalError:
        pass  # No GPU table - skip silently
    
    click.echo()
    
    # Recent collection stats
    click.echo(click.style("Collection:", bold=True))
    collection_rows = conn.execute(
        """
        SELECT collector, 
               COUNT(*) as runs,
               SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
               MAX(completed_at) as last_run
        FROM collection_log
        WHERE started_at > datetime('now', '-24 hours')
        GROUP BY collector
        """
    ).fetchall()
    
    if collection_rows:
        for row in collection_rows:
            success_rate = (row['successes'] / row['runs'] * 100) if row['runs'] else 0
            color = 'green' if success_rate == 100 else 'yellow' if success_rate > 90 else 'red'
            click.echo(f"  {row['collector']:<15} {row['runs']:>3} runs  {click.style(f'{success_rate:.0f}% success', fg=color)}")
    else:
        click.echo("  No collection data")
    
    click.echo()


@cli.command()
@click.option('--db', type=click.Path(), help='Database path override')
@click.option('--unresolved', is_flag=True, help='Show only unresolved alerts')
@click.option('--severity', type=click.Choice(['info', 'warning', 'critical']), help='Filter by severity')
@click.pass_context
def alerts(ctx: click.Context, db: str, unresolved: bool, severity: str) -> None:
    """Show and manage alerts."""
    config = ctx.obj['config']
    
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    if not db_path.exists():
        raise click.ClickException(f"Database not found: {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Build query
    query = "SELECT * FROM alerts WHERE 1=1"
    params = []
    
    if unresolved:
        query += " AND resolved = 0"
    
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    
    query += " ORDER BY timestamp DESC LIMIT 20"
    
    rows = conn.execute(query, params).fetchall()
    
    click.echo()
    click.echo(click.style("═══ Alerts ═══", bold=True))
    click.echo()
    
    if not rows:
        click.echo("  No alerts found")
        click.echo()
        return
    
    severity_colors = {
        'info': 'blue',
        'warning': 'yellow',
        'critical': 'red',
    }
    
    for row in rows:
        color = severity_colors.get(row['severity'], 'white')
        resolved = '✓' if row['resolved'] else '○'
        
        click.echo(f"  {resolved} [{click.style(row['severity'].upper(), fg=color)}] {row['timestamp']}")
        click.echo(f"    {row['message']}")
        if row['source']:
            click.echo(f"    Source: {row['source']}")
        click.echo()


@cli.command()
@click.option('--interval', '-i', type=int, default=30, help='Sample interval (seconds)')
@click.option('--once', is_flag=True, help='Run once and exit')
@click.option('--nfs-paths', multiple=True, help='Paths to classify as NFS')
@click.option('--local-paths', multiple=True, help='Paths to classify as local')
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def monitor(ctx: click.Context, interval: int, once: bool, 
            nfs_paths: tuple, local_paths: tuple, db: str) -> None:
    """Monitor running jobs for I/O metrics.
    
    Tracks NFS vs local storage writes in real-time.
    Updates job_summary with actual I/O patterns when jobs complete.
    """
    from nomade.monitors.job_monitor import JobMonitor
    
    config = ctx.obj['config']
    
    # Determine database path
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    click.echo(f"Database: {db_path}")
    
    # Build monitor config
    monitor_config = config.get('monitor', {})
    monitor_config['sample_interval'] = interval
    
    if nfs_paths:
        monitor_config['nfs_paths'] = list(nfs_paths)
    if local_paths:
        monitor_config['local_paths'] = list(local_paths)
    
    # Create and run monitor
    job_monitor = JobMonitor(monitor_config, str(db_path))
    
    click.echo(f"Starting job monitor (interval: {interval}s)")
    if not once:
        click.echo("Press Ctrl+C to stop")
    
    job_monitor.run(once=once)


@cli.command()
@click.option('--min-samples', type=int, default=3, help='Min I/O samples per job')
@click.option('--export', type=click.Path(), help='Export JSON for visualization')
@click.option('--find-similar', type=str, help='Find jobs similar to this job ID')
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def similarity(ctx: click.Context, min_samples: int, export: str, 
               find_similar: str, db: str) -> None:
    """Analyze job similarity and clustering.
    
    Computes similarity matrix using enriched feature vectors
    from both sacct metrics and real-time I/O monitoring.
    """
    from nomade.analysis.similarity import SimilarityAnalyzer
    
    config = ctx.obj['config']
    
    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)
    
    analyzer = SimilarityAnalyzer(str(db_path))
    
    if find_similar:
        features = analyzer.get_enriched_features(min_samples)
        sim_matrix, job_ids = analyzer.compute_similarity_matrix(features)
        similar = analyzer.find_similar_jobs(find_similar, features, sim_matrix)
        
        click.echo(f"\nJobs similar to {find_similar}:")
        for job_id, score in similar:
            bar = "█" * int(score * 20)
            click.echo(f"  {job_id}: {bar} {score:.3f}")
    
    elif export:
        import json
        features = analyzer.get_enriched_features(min_samples)
        sim_matrix, job_ids = analyzer.compute_similarity_matrix(features)
        clusters = analyzer.cluster_jobs(sim_matrix, job_ids)
        data = analyzer.export_for_visualization(features, sim_matrix, clusters)
        
        with open(export, 'w') as f:
            json.dump(data, f, indent=2)
        click.echo(f"Exported {len(data['nodes'])} nodes, {len(data['edges'])} edges to {export}")
    
    else:
        click.echo(analyzer.summary_report())


@cli.command()
@click.pass_context
def syscheck(ctx: click.Context) -> None:
    """Check system requirements and configuration.
    
    Validates SLURM setup, database, config, and filesystems.
    """
    import shutil
    import subprocess
    
    click.echo()
    click.echo(click.style("NØMADE System Check", bold=True))
    click.echo("═" * 40)
    click.echo()
    
    errors = 0
    warnings = 0
    
    # Python check
    click.echo(click.style("Python:", bold=True))
    import sys
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 9):
        click.echo(f"  {click.style('✓', fg='green')} Version {py_version} (requires >=3.9)")
    else:
        click.echo(f"  {click.style('✗', fg='red')} Version {py_version} (requires >=3.9)")
        errors += 1
    
    # Check required packages
    required_packages = ['click', 'toml', 'rich', 'numpy', 'pandas', 'scipy']
    missing = []
    for pkg in required_packages:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    
    if not missing:
        click.echo(f"  {click.style('✓', fg='green')} Required packages installed")
    else:
        click.echo(f"  {click.style('✗', fg='red')} Missing packages: {', '.join(missing)}")
        errors += 1

    # ML packages (optional)
    click.echo()
    click.echo(click.style("ML Packages (optional):", bold=True))
    ml_packages = [("sklearn", "scikit-learn"), ("torch", "pytorch"), ("torch_geometric", "torch-geometric")]
    ml_available = True
    for pkg, name in ml_packages:
        try:
            __import__(pkg)
            click.echo(f"  {click.style('✓', fg='green')} {name}")
        except ImportError:
            click.echo(f"  {click.style('○', fg='cyan')} {name} (not installed)")
            ml_available = False
    if not ml_available:
        click.echo(f"  {click.style('→', fg='yellow')} Install with: pip install nomade[ml]")
    
    click.echo()
    
    # SLURM check
    click.echo(click.style("SLURM:", bold=True))
    
    slurm_commands = ['sinfo', 'squeue', 'sacct', 'sstat']
    for cmd in slurm_commands:
        if shutil.which(cmd):
            click.echo(f"  {click.style('✓', fg='green')} {cmd} available")
        else:
            click.echo(f"  {click.style('✗', fg='red')} {cmd} not found")
            errors += 1
    
    # Check slurmdbd
    try:
        result = subprocess.run(['sacct', '--version'], capture_output=True, text=True, timeout=5)
        result2 = subprocess.run(['sacct', '-n', '-X', '-j', '1'], capture_output=True, text=True, timeout=5)
        if 'Slurm accounting storage is disabled' in result2.stderr:
            click.echo(f"  {click.style('⚠', fg='yellow')} slurmdbd not enabled (job history limited)")
            click.echo(f"    → Enable AccountingStorageType in slurm.conf")
            warnings += 1
        else:
            click.echo(f"  {click.style('✓', fg='green')} slurmdbd enabled")
    except Exception:
        click.echo(f"  {click.style('⚠', fg='yellow')} Could not check slurmdbd status")
        warnings += 1
    
    # Check JobAcctGather
    try:
        result = subprocess.run(['scontrol', 'show', 'config'], capture_output=True, text=True, timeout=10)
        if 'JobAcctGatherType' in result.stdout:
            if 'jobacct_gather/linux' in result.stdout or 'jobacct_gather/cgroup' in result.stdout:
                click.echo(f"  {click.style('✓', fg='green')} JobAcctGather configured")
            elif 'jobacct_gather/none' in result.stdout:
                click.echo(f"  {click.style('✗', fg='red')} JobAcctGather disabled (no per-job metrics)")
                click.echo(f"    → Add: JobAcctGatherType=jobacct_gather/linux")
                errors += 1
    except Exception:
        pass
    
    click.echo()
    
    # System tools check
    click.echo(click.style("System Tools:", bold=True))
    
    if shutil.which('iostat'):
        click.echo(f"  {click.style('✓', fg='green')} iostat available")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} iostat not found (install sysstat package)")
        click.echo(f"    → apt install sysstat  OR  yum install sysstat")
        warnings += 1
    
    if shutil.which('mpstat'):
        click.echo(f"  {click.style('✓', fg='green')} mpstat available")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} mpstat not found (install sysstat package)")
        click.echo(f"    → apt install sysstat  OR  yum install sysstat")
        warnings += 1
    
    if shutil.which('vmstat'):
        click.echo(f"  {click.style('✓', fg='green')} vmstat available")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} vmstat not found")
        warnings += 1
    
    if shutil.which('nvidia-smi'):
        click.echo(f"  {click.style('✓', fg='green')} nvidia-smi available (GPU monitoring)")
    else:
        click.echo(f"  {click.style('○', fg='cyan')} nvidia-smi not found (no GPU monitoring)")
    
    if shutil.which('nfsiostat'):
        click.echo(f"  {click.style('✓', fg='green')} nfsiostat available (NFS monitoring)")
    else:
        click.echo(f"  {click.style('○', fg='cyan')} nfsiostat not found (no NFS monitoring)")
    
    if Path('/proc/1/io').exists():
        click.echo(f"  {click.style('✓', fg='green')} /proc/[pid]/io accessible")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} /proc/[pid]/io not accessible (job I/O monitoring limited)")
        warnings += 1
    
    click.echo()
    
    # Database check
    click.echo(click.style("Database:", bold=True))
    
    config = ctx.obj.get('config', {})
    db_path = get_db_path(config)
    
    if shutil.which('sqlite3'):
        click.echo(f"  {click.style('✓', fg='green')} SQLite available")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} sqlite3 CLI not found (optional)")
        warnings += 1
    
    if db_path.exists():
        click.echo(f"  {click.style('✓', fg='green')} Database: {db_path}")
        # Check schema
        try:
            conn = sqlite3.connect(db_path)
            version = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
            if version:
                click.echo(f"  {click.style('✓', fg='green')} Schema version: {version[0]}")
            conn.close()
        except Exception as e:
            click.echo(f"  {click.style('⚠', fg='yellow')} Could not read schema: {e}")
            warnings += 1
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} Database not found: {db_path}")
        click.echo(f"    → Run: nomade collect --once")
        warnings += 1
    
    click.echo()
    
    # Config check
    click.echo(click.style("Config:", bold=True))
    
    config_path = ctx.obj.get('config_path')
    if config_path and Path(config_path).exists():
        click.echo(f"  {click.style('✓', fg='green')} Config: {config_path}")
        
        # Check partitions match SLURM
        config_partitions = config.get('collectors', {}).get('slurm', {}).get('partitions', [])
        if config_partitions:
            try:
                result = subprocess.run(['sinfo', '-h', '-o', '%P'], capture_output=True, text=True, timeout=5)
                slurm_partitions = [p.strip().rstrip('*') for p in result.stdout.strip().split('\n') if p.strip()]
                
                for p in config_partitions:
                    if p not in slurm_partitions:
                        click.echo(f"  {click.style('⚠', fg='yellow')} Partition '{p}' in config but not in SLURM")
                        warnings += 1
            except Exception:
                pass
    else:
        click.echo(f"  {click.style('✗', fg='red')} Config not found: /etc/nomade/nomade.toml")
        click.echo(f"    → Create config or use: nomade -c /path/to/config.toml")
        errors += 1
    
    click.echo()
    
    # Filesystem check
    click.echo(click.style("Filesystems:", bold=True))
    
    filesystems = config.get('collectors', {}).get('disk', {}).get('filesystems', ['/'])
    for fs in filesystems:
        if Path(fs).exists():
            click.echo(f"  {click.style('✓', fg='green')} {fs} (accessible)")
        else:
            click.echo(f"  {click.style('✗', fg='red')} {fs} (not found)")
            errors += 1
    
    click.echo()
    
    # Summary
    click.echo("─" * 40)
    if errors == 0 and warnings == 0:
        click.echo(click.style("✓ All checks passed!", fg='green', bold=True))
    else:
        parts = []
        if errors > 0:
            parts.append(click.style(f"{errors} error(s)", fg='red'))
        if warnings > 0:
            parts.append(click.style(f"{warnings} warning(s)", fg='yellow'))
        click.echo(f"Summary: {', '.join(parts)}")
    
    click.echo()


@cli.command()
@click.pass_context
def version(ctx: click.Context) -> None:
    """Show version information."""
    click.echo("NØMADE v0.2.0")
    click.echo("NØde MAnagement DEvice")


@cli.command()
@click.option('--host', default='localhost', help='Host to bind to (use 0.0.0.0 for all interfaces)')
@click.option('--port', '-p', type=int, default=8050, help='Port to listen on')
@click.option('--data', '-d', type=click.Path(), help='Data source (db file or metrics log)')
@click.pass_context
def dashboard(ctx, host, port, data):
    """Start the interactive web dashboard.
    
    The dashboard provides a 3D visualization of job networks with two view modes:
    
    - Raw Axes: Jobs positioned by nfs_write, local_write, io_wait
    - PCA View: Jobs positioned by principal components (patterns emerge from data)
    
    Remote access via SSH tunnel:
        ssh -L 8050:localhost:8050 badenpowell
        Then open http://localhost:8050 in your browser
    
    Examples:
        nomade dashboard                      # Start with demo data
        nomade dashboard --port 9000          # Custom port
        nomade dashboard --data /path/to.db   # Use database
    """
    from nomade.viz.server import serve_dashboard
    
    # Try to find data source
    data_source = data
    if not data_source:
        config = ctx.obj.get('config', {})
        # Try database first
        db_path = get_db_path(config)
        if db_path.exists():
            data_source = str(db_path)
        else:
            # Try simulation metrics
            metrics_paths = [
                Path('/tmp/nomade-metrics.log'),
                Path.home() / 'nomade-metrics.log',
            ]
            for mp in metrics_paths:
                if mp.exists():
                    data_source = str(mp)
                    break
    
    click.echo(click.style("===========================================", fg='cyan'))
    click.echo(click.style("           ", fg='cyan') + 
               click.style("NOMADE Dashboard", fg='white', bold=True))
    click.echo(click.style("===========================================", fg='cyan'))
    click.echo()
    
    serve_dashboard(host, port, data_source)


@cli.command()
@click.option("--db", type=click.Path(), help="Database path")
@click.option("--epochs", "-e", type=int, default=100, help="Training epochs")
@click.option("--verbose", "-v", is_flag=True, help="Show training progress")
@click.pass_context
def train(ctx, db, epochs, verbose):
    """Train ML ensemble models on job data.
    
    Trains GNN, LSTM, and Autoencoder models on historical job data
    and saves predictions to the database.
    
    Examples:
        nomade train                    # Train with default settings
        nomade train --epochs 50        # Fewer epochs (faster)
        nomade train --db data.db       # Specify database
    """
    from nomade.ml import train_and_save_ensemble, is_torch_available
    
    if not is_torch_available():
        click.echo(click.style("Error: PyTorch not available", fg="red"))
        click.echo("Install with: pip install torch torch-geometric")
        return
    
    db_path = db
    if not db_path:
        config = ctx.obj.get("config", {})
        db_path = str(get_db_path(config))
    
    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        return
    
    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo(click.style("  NOMADE ML Training", fg="white", bold=True))
    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo(f"  Database: {db_path}")
    click.echo(f"  Epochs: {epochs}")
    click.echo()
    
    result = train_and_save_ensemble(db_path, epochs=epochs, verbose=verbose)
    
    click.echo()
    click.echo(click.style("=" * 60, fg="green"))
    click.echo(click.style("  Training Complete", fg="white", bold=True))
    click.echo(click.style("=" * 60, fg="green"))
    click.echo(f"  Prediction ID: {result.get('prediction_id', '-')}")
    click.echo(f"  High-risk jobs: {len(result.get('high_risk', []))}")
    click.echo(f"  Anomalies: {result.get('n_anomalies', 0)}")
    if result.get("summary"):
        s = result["summary"]
        click.echo(f"  GNN Accuracy: {s.get('gnn_accuracy', 0)*100:.1f}%")
        click.echo(f"  LSTM Accuracy: {s.get('lstm_accuracy', 0)*100:.1f}%")


@cli.command()
@click.option("--db", type=click.Path(), help="Database path")
@click.option("--top", "-n", type=int, default=20, help="Number of high-risk jobs to show")
@click.pass_context
def predict(ctx, db, top):
    """Show ML predictions for jobs.
    
    Displays high-risk jobs identified by the ensemble model.
    Run 'nomade train' first to generate predictions.
    
    Examples:
        nomade predict                  # Show top 20 high-risk jobs
        nomade predict --top 50         # Show top 50
    """
    from nomade.ml import load_predictions_from_db
    
    db_path = db
    if not db_path:
        config = ctx.obj.get("config", {})
        db_path = str(get_db_path(config))
    
    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        return
    
    predictions = load_predictions_from_db(db_path)
    
    if not predictions:
        click.echo(click.style("No predictions found. Run 'nomade train' first.", fg="yellow"))
        return
    
    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo(click.style("  NOMADE ML Predictions", fg="white", bold=True))
    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo(f"  Status: {predictions.get('status', 'unknown')}")
    click.echo(f"  Jobs analyzed: {predictions.get('n_jobs', 0)}")
    click.echo(f"  Anomalies: {predictions.get('n_anomalies', 0)}")
    click.echo(f"  Threshold: {predictions.get('threshold', 0):.4f}")
    click.echo()
    
    high_risk = predictions.get("high_risk", [])[:top]
    if high_risk:
        click.echo(click.style(f"  Top {len(high_risk)} High-Risk Jobs:", fg="red", bold=True))
        click.echo(f"  {'Job ID':<12} {'Score':<10} {'Anomaly':<8} {'Failure'}")
        click.echo(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*10}")
        for job in high_risk:
            anomaly = "Yes" if job.get("is_anomaly") else "No"
            failure = job.get("predicted_name", job.get("failure_reason", "-"))
            click.echo(f"  {str(job.get('job_id', '-')):<12} {job.get('anomaly_score', 0):<10.2f} {anomaly:<8} {failure}")


@cli.command()
@click.option("--db", type=click.Path(), help="Database path")
@click.option("--output", "-o", type=click.Path(), help="Output file (default: stdout)")
@click.pass_context
def report(ctx, db, output):
    """Generate ML analysis report.
    
    Creates a summary report of job failures and ML predictions.
    
    Examples:
        nomade report                   # Print to stdout
        nomade report -o report.txt     # Save to file
    """
    from nomade.ml import load_predictions_from_db, FAILURE_NAMES
    import sqlite3
    
    db_path = db
    if not db_path:
        config = ctx.obj.get("config", {})
        db_path = str(get_db_path(config))
    
    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        return
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs").fetchall()]
    conn.close()
    
    predictions = load_predictions_from_db(db_path)
    
    lines = []
    lines.append("=" * 60)
    lines.append("  NOMADE Analysis Report")
    lines.append("=" * 60)
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Database: {db_path}")
    lines.append("")
    
    total = len(jobs)
    success = sum(1 for j in jobs if j.get("failure_reason", 0) == 0)
    failed = total - success
    lines.append("  JOB SUMMARY")
    lines.append(f"  Total jobs: {total}")
    lines.append(f"  Success: {success} ({100*success/total:.1f}%)")
    lines.append(f"  Failed: {failed} ({100*failed/total:.1f}%)")
    lines.append("")
    
    if failed > 0:
        lines.append("  FAILURE BREAKDOWN")
        failure_counts = {}
        for j in jobs:
            fr = j.get("failure_reason", 0)
            if fr > 0:
                name = FAILURE_NAMES.get(fr, f"Type {fr}")
                failure_counts[name] = failure_counts.get(name, 0) + 1
        for name, count in sorted(failure_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {name}: {count} ({100*count/failed:.1f}%)")
        lines.append("")
    
    if predictions:
        lines.append("  ML PREDICTIONS")
        lines.append(f"  Status: {predictions.get('status', 'unknown')}")
        lines.append(f"  Anomalies detected: {predictions.get('n_anomalies', 0)}")
        if predictions.get("summary"):
            s = predictions["summary"]
            lines.append(f"  GNN Accuracy: {s.get('gnn_accuracy', 0)*100:.1f}%")
            lines.append(f"  LSTM Accuracy: {s.get('lstm_accuracy', 0)*100:.1f}%")
            lines.append(f"  AE Precision: {s.get('ae_precision', 0)*100:.1f}%")
        lines.append("")
        
        high_risk = predictions.get("high_risk", [])[:10]
        if high_risk:
            lines.append("  TOP 10 HIGH-RISK JOBS")
            for job in high_risk:
                lines.append(f"    Job {job.get('job_id', '-')}: score={job.get('anomaly_score', 0):.2f}")
    else:
        lines.append("  ML PREDICTIONS: Not available (run 'nomade train')")
    
    lines.append("")
    lines.append("=" * 60)
    
    report_text = "\n".join(lines)
    
    if output:
        Path(output).write_text(report_text)
        click.echo(f"Report saved to {output}")
    else:
        click.echo(report_text)



@cli.command('test-alerts')
@click.option('--email', is_flag=True, help='Test email backend')
@click.option('--slack', is_flag=True, help='Test Slack backend')
@click.option('--webhook', is_flag=True, help='Test webhook backend')
@click.pass_context
def test_alerts(ctx, email, slack, webhook):
    """Test alert notification backends.
    
    Examples:
        nomade test-alerts --email     # Test email
        nomade test-alerts --slack     # Test Slack
        nomade test-alerts             # Test all configured backends
    """
    from nomade.alerts import AlertDispatcher, send_alert
    
    config = ctx.obj.get('config', {})
    
    # Build test config if flags provided
    if email or slack or webhook:
        if email:
            click.echo("Testing email backend...")
            # Would need config from file
        if slack:
            click.echo("Testing Slack backend...")
        if webhook:
            click.echo("Testing webhook backend...")
    
    # Test with actual config
    dispatcher = AlertDispatcher(config)
    
    if not dispatcher.backends:
        click.echo(click.style("No alert backends configured.", fg="yellow"))
        click.echo("Add configuration to nomade.toml:")
        click.echo("""
[alerts.email]
enabled = true
smtp_server = "smtp.example.com"
recipients = ["admin@example.com"]

[alerts.slack]
enabled = true
webhook_url = "https://hooks.slack.com/..."
""")
        return
    
    click.echo(f"Testing {len(dispatcher.backends)} backend(s)...")
    results = dispatcher.test_backends()
    
    for backend, success in results.items():
        if success:
            click.echo(click.style(f"  {backend}: OK", fg="green"))
        else:
            click.echo(click.style(f"  {backend}: FAILED", fg="red"))
    
    # Send test alert
    click.echo("\nSending test alert...")
    send_results = dispatcher.dispatch({
        'severity': 'info',
        'source': 'test',
        'message': 'This is a test alert from NOMADE',
        'host': 'cli-test'
    })
    
    for backend, success in send_results.items():
        if success:
            click.echo(click.style(f"  {backend}: Sent", fg="green"))
        else:
            click.echo(click.style(f"  {backend}: Failed", fg="red"))



@cli.command()
@click.option('--db', type=click.Path(), help='Database path')
@click.option('--strategy', type=click.Choice(['time', 'count', 'drift']), 
              default='count', help='Retraining strategy')
@click.option('--threshold', type=int, default=100, 
              help='Job count threshold (for count strategy)')
@click.option('--interval', type=int, default=6, 
              help='Hours between training (for time strategy)')
@click.option('--epochs', type=int, default=100, help='Training epochs')
@click.option('--force', is_flag=True, help='Force training regardless of strategy')
@click.option('--daemon', is_flag=True, help='Run as daemon')
@click.option('--check-interval', type=int, default=300, 
              help='Daemon check interval in seconds')
@click.option('--status', 'show_status', is_flag=True, help='Show training status')
@click.option('--history', 'show_history', is_flag=True, help='Show training history')
@click.option('-v', '--verbose', is_flag=True, help='Verbose output')
@click.pass_context
def learn(ctx, db, strategy, threshold, interval, epochs, force, daemon, 
          check_interval, show_status, show_history, verbose):
    """Continuous learning - retrain models as new data arrives.
    
    \b
    Strategies:
      count  Retrain after N new jobs (default: 100)
      time   Retrain every N hours (default: 6)
      drift  Retrain when prediction accuracy drops
    
    \b
    Examples:
      nomade learn --status           Show training status
      nomade learn --force            Train now
      nomade learn --strategy count   Train after 100 new jobs
      nomade learn --daemon           Run continuously
    """
    from nomade.ml import is_torch_available
    from nomade.ml.continuous import ContinuousLearner
    
    if not is_torch_available():
        click.echo(click.style("Error: PyTorch not available", fg="red"))
        return
    
    db_path = db
    if not db_path:
        config = ctx.obj.get('config', {})
        db_path = str(get_db_path(config))
    
    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        return
    
    # Build config
    learn_config = {
        'learning': {
            'strategy': strategy,
            'job_threshold': threshold,
            'interval_hours': interval,
            'epochs': epochs
        }
    }
    
    learner = ContinuousLearner(db_path, learn_config)
    
    # Show status
    if show_status:
        status = learner.get_training_status()
        click.echo(click.style("=" * 50, fg="cyan"))
        click.echo(click.style("  NOMADE Learning Status", fg="white", bold=True))
        click.echo(click.style("=" * 50, fg="cyan"))
        click.echo(f"  Strategy: {status['strategy']}")
        click.echo(f"  Total jobs: {status['total_jobs']}")
        click.echo(f"  Jobs since last training: {status['jobs_since_last_training']}")
        click.echo(f"  Last trained: {status['last_trained_at'] or 'Never'}")
        
        should_train, reason = learner.should_retrain()
        if should_train:
            click.echo(click.style(f"  Status: Training needed - {reason}", fg="yellow"))
        else:
            click.echo(click.style(f"  Status: Up to date - {reason}", fg="green"))
        return
    
    # Show history
    if show_history:
        history = learner.get_training_history()
        click.echo(click.style("=" * 70, fg="cyan"))
        click.echo(click.style("  Training History", fg="white", bold=True))
        click.echo(click.style("=" * 70, fg="cyan"))
        
        if not history:
            click.echo("  No training runs yet")
            return
        
        click.echo(f"  {'Completed':<20} {'Status':<10} {'Jobs':<8} {'GNN':<8} {'LSTM':<8}")
        click.echo(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*8}")
        
        for run in history:
            completed = run.get('completed_at', 'N/A')[:19] if run.get('completed_at') else 'N/A'
            status_color = 'green' if run['status'] == 'completed' else 'red'
            gnn = f"{run.get('gnn_accuracy', 0)*100:.1f}%" if run.get('gnn_accuracy') else 'N/A'
            lstm = f"{run.get('lstm_accuracy', 0)*100:.1f}%" if run.get('lstm_accuracy') else 'N/A'
            
            click.echo(f"  {completed:<20} " + 
                      click.style(f"{run['status']:<10}", fg=status_color) +
                      f" {run.get('jobs_trained', 'N/A'):<8} {gnn:<8} {lstm:<8}")
        return
    
    # Run daemon
    if daemon:
        click.echo(click.style("Starting continuous learning daemon...", fg="cyan"))
        click.echo(f"  Strategy: {strategy}")
        click.echo(f"  Check interval: {check_interval}s")
        click.echo("  Press Ctrl+C to stop")
        try:
            learner.run_daemon(check_interval=check_interval, verbose=verbose)
        except KeyboardInterrupt:
            click.echo("\nDaemon stopped")
        return
    
    # Single training run
    result = learner.train(force=force, verbose=verbose)
    
    if result['status'] == 'skipped':
        click.echo(click.style(f"Training skipped: {result['reason']}", fg="yellow"))
    elif result['status'] == 'completed':
        click.echo(click.style("=" * 50, fg="green"))
        click.echo(click.style("  Training Completed", fg="white", bold=True))
        click.echo(click.style("=" * 50, fg="green"))
        click.echo(f"  Prediction ID: {result.get('prediction_id')}")
        click.echo(f"  High-risk jobs: {len(result.get('high_risk', []))}")
    else:
        click.echo(click.style(f"Training failed: {result.get('error')}", fg="red"))



@cli.command()
@click.option('--system', is_flag=True, help='Install system-wide for HPC')
@click.option('--force', is_flag=True, help='Overwrite existing files')
@click.option('--no-systemd', is_flag=True, help='Skip systemd service installation')
@click.option('--no-prolog', is_flag=True, help='Skip SLURM prolog hook')
@click.pass_context
def init(ctx, system, force, no_systemd, no_prolog):
    """Initialize NOMADE configuration and data directories.
    
    \b
    User install (default):
      ~/.config/nomade/nomade.toml   Configuration
      ~/.local/share/nomade/         Data directory
    
    \b
    System install (--system, requires root):
      /etc/nomade/nomade.toml        Configuration
      /var/lib/nomade/               Data directory
      /var/log/nomade/               Log directory
      /etc/systemd/system/           Service files
      /etc/slurm/prolog.d/           SLURM hook
      /etc/logrotate.d/nomade        Log rotation
    
    \b
    Examples:
      nomade init                    User installation
      sudo nomade init --system      Full HPC installation
    """
    import shutil
    from importlib.resources import files
    
    if system:
        config_dir = Path('/etc/nomade')
        data_dir = Path('/var/lib/nomade')
        log_dir = Path('/var/log/nomade')
    else:
        config_dir = Path.home() / '.config' / 'nomade'
        data_dir = Path.home() / '.local' / 'share' / 'nomade'
        log_dir = data_dir / 'logs'
    
    config_file = config_dir / 'nomade.toml'
    
    # Check existing
    if config_file.exists() and not force:
        click.echo(click.style(f"Config already exists: {config_file}", fg="yellow"))
        click.echo("Use --force to overwrite")
        return
    
    # Create directories
    click.echo(f"Creating directories...")
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / 'models').mkdir(exist_ok=True)
        click.echo(f"  {config_dir}")
        click.echo(f"  {data_dir}")
        click.echo(f"  {log_dir}")
    except PermissionError:
        click.echo(click.style("Permission denied. Use sudo for system install.", fg="red"))
        return
    
    # Copy default config
    click.echo(f"Creating config file...")
    try:
        # Try to find packaged config
        try:
            import nomade.config
            pkg_config = Path(nomade.config.__file__).parent / 'default.toml'
            if pkg_config.exists():
                shutil.copy(pkg_config, config_file)
            else:
                raise FileNotFoundError
        except (ImportError, FileNotFoundError):
            # Fallback: look relative to cli.py
            cli_path = Path(__file__).parent
            pkg_config = cli_path / 'config' / 'default.toml'
            if pkg_config.exists():
                shutil.copy(pkg_config, config_file)
            else:
                # Generate minimal config
                minimal = f"""# NOMADE Configuration
[general]
data_dir = "{data_dir}"

[database]
path = "nomade.db"

[collectors]
enabled = ["disk", "slurm"]
interval = 60

[alerts]
enabled = true
min_severity = "warning"

[ml]
enabled = true
"""
                config_file.write_text(minimal)
        
        click.echo(f"  {config_file}")
    except Exception as e:
        click.echo(click.style(f"Failed to create config: {e}", fg="red"))
        return
    
    # Update config with actual paths
    if config_file.exists():
        config_text = config_file.read_text()
        config_text = config_text.replace('data_dir = "~/.local/share/nomade"', 
                                          f'data_dir = "{data_dir}"')
        if system:
            config_text = config_text.replace('/var/log/nomade', str(log_dir))
        config_file.write_text(config_text)
    
    click.echo()
    click.echo(click.style("NOMADE initialized!", fg="green", bold=True))
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Edit config: {config_file}")
    click.echo(f"  2. Check system: nomade syscheck")
    click.echo(f"  3. Start collecting: nomade collect")
    click.echo(f"  4. View dashboard: nomade dashboard")



@cli.command()
@click.option('--jobs', '-n', type=int, default=1000, help='Number of jobs to generate')
@click.option('--days', '-d', type=int, default=7, help='Days of history to simulate')
@click.option('--seed', '-s', type=int, default=None, help='Random seed for reproducibility')
@click.option('--port', '-p', type=int, default=5000, help='Dashboard port')
@click.option('--no-launch', is_flag=True, help='Generate data only, do not launch dashboard')
def demo(jobs, days, seed, port, no_launch):
    """Run demo mode with synthetic data.

    Generates realistic HPC job data and launches the dashboard.
    Perfect for testing NØMADE without a real HPC cluster.

    Examples:
        nomade demo                  # Generate 1000 jobs, launch dashboard
        nomade demo --jobs 500       # Generate 500 jobs
        nomade demo --no-launch      # Generate only, don't launch dashboard
        nomade demo --seed 42        # Reproducible data
    """
    from nomade.demo import run_demo
    run_demo(
        n_jobs=jobs,
        days=days,
        seed=seed,
        launch_dashboard=not no_launch,
        port=port,
    )


def main() -> None:
    """Entry point for CLI."""
    cli(obj={})


if __name__ == '__main__':
    main()
