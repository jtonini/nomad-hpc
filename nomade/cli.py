# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations
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
from nomade.collectors.groups import GroupCollector
from nomade.collectors.interactive import InteractiveCollector
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


def resolve_config_path() -> str:
    """Find config file: user path first, then system path."""
    user_config = Path.home() / '.config' / 'nomade' / 'nomade.toml'
    system_config = Path('/etc/nomade/nomade.toml')
    if user_config.exists():
        return str(user_config)
    if system_config.exists():
        return str(system_config)
    return str(user_config)  # Default to user path even if missing


def get_db_path(config: dict[str, Any]) -> Path:
    """Get database path from config."""
    default_data = str(Path.home() / '.local' / 'share' / 'nomade')
    data_dir = Path(config.get('general', {}).get('data_dir', default_data))
    return data_dir / 'nomade.db'


@click.group()
@click.option('-c', '--config', 'config_path', 
              type=click.Path(),
              default=None,
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
    if config_path is None:
        config_path = resolve_config_path()
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
            if 'cluster_name' not in node_state_config:
                node_state_config['cluster_name'] = config.get('cluster_name', 'default')
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


    # Group membership and job accounting collector
    groups_config = config.get('collectors', {}).get('groups', {})
    if not collector or 'groups' in collector:
        if groups_config.get('enabled', True):
            groups_config['clusters'] = config.get('clusters', {})
            collectors.append(GroupCollector(groups_config, db_path))

    # Interactive session collector
    interactive_config = config.get("interactive", {})
    if not collector or "interactive" in collector:
        if interactive_config.get("enabled", False):
            collectors.append(InteractiveCollector(interactive_config, db_path))
    
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
        expected = resolve_config_path()
        click.echo(f"  {click.style('✗', fg='red')} Config not found: {expected}")
        click.echo(f"    → Run: nomade init")
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
@click.option('--quick', is_flag=True, help='Skip wizard, use auto-detected defaults')
@click.option('--no-systemd', is_flag=True, help='Skip systemd service installation')
@click.option('--no-prolog', is_flag=True, help='Skip SLURM prolog hook')
@click.pass_context
def init(ctx, system, force, quick, no_systemd, no_prolog):
    """Initialize NOMADE with an interactive setup wizard.

    \b
    The wizard walks you through configuring NØMADE for your
    HPC cluster(s). It will ask about your clusters, partitions,
    storage, and monitoring preferences.

    \b
    If the wizard is interrupted (Ctrl+C), your progress is saved
    automatically. Run 'nomade init' again to pick up where you
    left off.

    \b
    User install (default):
      ~/.config/nomade/nomade.toml   Configuration
      ~/.local/share/nomade/         Data directory

    \b
    System install (--system, requires root):
      /etc/nomade/nomade.toml        Configuration
      /var/lib/nomade/               Data directory

    \b
    Examples:
      nomade init                    Interactive wizard
      nomade init --quick            Auto-detect everything
      nomade init --force            Overwrite existing config
      sudo nomade init --system      System-wide installation
    """
    import shutil
    import subprocess as sp
    import os
    import json

    # ── Determine paths ──────────────────────────────────────────────
    if system:
        config_dir = Path('/etc/nomade')
        data_dir = Path('/var/lib/nomade')
        log_dir = Path('/var/log/nomade')
    else:
        config_dir = Path.home() / '.config' / 'nomade'
        data_dir = Path.home() / '.local' / 'share' / 'nomade'
        log_dir = data_dir / 'logs'

    config_file = config_dir / 'nomade.toml'

    # Check existing config
    if config_file.exists() and not force:
        click.echo(click.style(
            f"\n  Config already exists: {config_file}", fg="yellow"))
        if not click.confirm("  Overwrite it?", default=False):
            click.echo(
                "  Run with --force to overwrite, or edit the file directly.")
            return

    # ── Create directories ───────────────────────────────────────────
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / 'models').mkdir(exist_ok=True)
    except PermissionError:
        click.echo(click.style(
            "\n  Permission denied. Use: sudo nomade init --system",
            fg="red"))
        return

    # ── State file for resume support ────────────────────────────────
    state_file = config_dir / '.wizard_state.json'

    def save_state(state):
        """Save wizard progress so it can be resumed if interrupted."""
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            state['_timestamp'] = datetime.now().isoformat()
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def load_state():
        """Load saved wizard progress."""
        try:
            if state_file.exists():
                return json.loads(state_file.read_text())
        except Exception:
            pass
        return None

    def clear_state():
        """Remove state file after successful completion."""
        try:
            if state_file.exists():
                state_file.unlink()
        except Exception:
            pass

    # ── Helper: run a command locally or via SSH ─────────────────────
    def run_cmd(cmd, host=None, ssh_user=None, ssh_key=None):
        """Run a command locally or via SSH. Returns stdout or None."""
        if host:
            ssh_cmd = ["ssh", "-o", "ConnectTimeout=5",
                       "-o", "StrictHostKeyChecking=accept-new"]
            if ssh_key:
                ssh_cmd += ["-i", ssh_key]
            ssh_cmd += [f"{ssh_user}@{host}", cmd]
            full_cmd = ssh_cmd
        else:
            full_cmd = cmd.split()
        try:
            result = sp.run(full_cmd, capture_output=True, text=True,
                            timeout=15)
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def detect_partitions(host=None, ssh_user=None, ssh_key=None):
        out = run_cmd("sinfo -h -o %P", host, ssh_user, ssh_key)
        if out:
            return [l.strip().rstrip('*')
                    for l in out.split('\n') if l.strip()]
        return []

    def detect_nodes_per_partition(partition, host=None, ssh_user=None,
                                   ssh_key=None):
        out = run_cmd(f"sinfo -h -p {partition} -o %n",
                      host, ssh_user, ssh_key)
        if out:
            return sorted(set(
                l.strip() for l in out.split('\n') if l.strip()))
        return []

    def detect_gpu_nodes(host=None, ssh_user=None, ssh_key=None):
        out = run_cmd("sinfo -h -o %n,%G", host, ssh_user, ssh_key)
        if out:
            gpu = set()
            for line in out.split('\n'):
                parts = line.strip().split(',', 1)
                if len(parts) == 2 and 'gpu' in parts[1].lower():
                    gpu.add(parts[0])
            return sorted(gpu)
        return []

    def detect_filesystems(host=None, ssh_user=None, ssh_key=None):
        out = run_cmd("df -h --output=target", host, ssh_user, ssh_key)
        hpc_paths = {'/', '/home', '/scratch', '/localscratch', '/project',
                     '/work', '/data', '/shared'}
        if out:
            found = [l.strip() for l in out.split('\n')[1:]
                     if l.strip() in hpc_paths]
            return sorted(found) if found else ['/', '/home']
        return ['/', '/home']

    def has_command(cmd, host=None, ssh_user=None, ssh_key=None):
        return run_cmd(f"which {cmd}", host, ssh_user, ssh_key) is not None

    # ── Reusable collection helpers ──────────────────────────────────
    def collect_partitions(cluster, host, ssh_user, ssh_key):
        """Ask user about partitions and nodes. Modifies cluster."""
        cluster["partitions"] = {}
        is_hpc = cluster.get("type") == "hpc"
        is_remote_ws = (cluster.get("mode") == "remote"
                        and not is_hpc)

        if is_hpc:
            click.echo("  Detecting SLURM partitions... ", nl=False)
            detected = detect_partitions(host, ssh_user, ssh_key)
            gpu_nodes = detect_gpu_nodes(host, ssh_user, ssh_key)

            if detected:
                click.echo(click.style(
                    f"found {len(detected)}", fg="green"))
                click.echo()
                for p in detected:
                    click.echo(f"    • {p}")
                click.echo()
                use_all = click.confirm(
                    "  Monitor all of these partitions?", default=True)
                if use_all:
                    chosen = detected
                else:
                    click.echo()
                    click.echo("  Type the partition names you want,")
                    click.echo("  separated by commas:")
                    chosen_str = click.prompt(
                        "  Partitions",
                        default=', '.join(detected))
                    chosen = [p.strip() for p in chosen_str.split(',')
                              if p.strip()]
            else:
                click.echo(click.style(
                    "could not auto-detect", fg="yellow"))
                click.echo()
                click.echo(
                    "  NØMADE could not detect partitions automatically.")
                click.echo(
                    "  This usually means SLURM is not installed here,")
                click.echo(
                    "  or the SSH connection is not working yet.")
                click.echo()
                click.echo(
                    "  You can find partition names by running this")
                click.echo("  command on the cluster headnode:")
                click.echo('    sinfo -h -o "%P"')
                click.echo()
                click.echo(
                    "  Type your partition names separated by commas:")
                chosen_str = click.prompt("  Partitions")
                chosen = [p.strip() for p in chosen_str.split(',')
                          if p.strip()]
            click.echo()

            click.echo("  Detecting nodes per partition...")
            for p in chosen:
                nodes = detect_nodes_per_partition(
                    p, host, ssh_user, ssh_key)
                part_gpu = [n for n in nodes if n in gpu_nodes]
                if nodes:
                    gpu_info = (f" ({len(part_gpu)} with GPU)"
                                if part_gpu else "")
                    click.echo(
                        f"    {p}: {len(nodes)} nodes{gpu_info}")
                else:
                    click.echo(
                        f"    {p}: could not detect nodes automatically")
                    click.echo()
                    click.echo(f"  Type the node names for '{p}',")
                    click.echo(f"  separated by commas:")
                    click.echo(f"  (e.g., node01, node02, node03)")
                    nodes_str = click.prompt(
                        f"  Nodes for {p}", default="")
                    nodes = [n.strip() for n in nodes_str.split(',')
                             if n.strip()]
                    part_gpu = []

                cluster["partitions"][p] = {
                    "nodes": nodes,
                    "gpu_nodes": part_gpu,
                }
        else:
            # Workstation group
            click.echo(
                "  For workstation groups, you organize machines by")
            click.echo(
                "  department or lab. Each group becomes a section")
            click.echo("  in the dashboard.")
            click.echo()
            click.echo(
                "  Type your department/lab names, separated by commas:")
            click.echo("  (e.g., biology, chemistry, physics)")
            click.echo()
            depts_str = click.prompt("  Departments")
            depts = [d.strip() for d in depts_str.split(',')
                     if d.strip()]
            click.echo()

            for dept in depts:
                click.echo(f"  Type the hostnames for '{dept}',")
                click.echo(f"  separated by commas:")
                click.echo(f"  (e.g., bio-ws01, bio-ws02, bio-ws03)")
                nodes_str = click.prompt(f"  Nodes for {dept}")
                nodes = [n.strip() for n in nodes_str.split(',')
                         if n.strip()]
                cluster["partitions"][dept] = {
                    "nodes": nodes,
                    "gpu_nodes": [],
                }
                # Test SSH to first workstation in each group
                if is_remote_ws and nodes and ssh_user:
                    click.echo(
                        f"  Testing SSH to {nodes[0]}... ",
                        nl=False)
                    test = run_cmd(
                        "echo ok", nodes[0], ssh_user, ssh_key)
                    if test:
                        click.echo(click.style(
                            "✓ Connected", fg="green"))
                    else:
                        click.echo(click.style(
                            "✗ Could not connect", fg="yellow"))
                        click.echo(
                            f"    Check that {nodes[0]} is"
                            f" reachable and your SSH key"
                            f" is authorized.")
                click.echo()

    def collect_filesystems(cluster, host, ssh_user, ssh_key):
        """Ask user about filesystems. Modifies cluster."""
        # For workstation groups, probe first node instead of headnode
        probe_host = host
        if not probe_host and cluster.get("partitions"):
            first_part = next(iter(cluster["partitions"].values()), {})
            first_nodes = first_part.get("nodes", [])
            if first_nodes:
                probe_host = first_nodes[0]

        click.echo()
        click.echo(click.style("  Storage", fg="green", bold=True))
        click.echo()
        click.echo(
            "  Which filesystems should NØMADE monitor for disk")
        click.echo(
            "  usage? Common HPC paths: /, /home, /scratch,")
        click.echo("  /localscratch, /project")
        click.echo()
        detected_fs = detect_filesystems(
            probe_host, ssh_user, ssh_key)
        default_fs = ', '.join(detected_fs)
        fs_str = click.prompt(
            "  Filesystems (comma-separated)", default=default_fs)
        cluster["filesystems"] = [
            f.strip() for f in fs_str.split(',') if f.strip()]

    def collect_features(cluster, host, ssh_user, ssh_key):
        """Ask user about optional features. Modifies cluster."""
        # For workstation groups, probe first node instead of headnode
        probe_host = host
        if not probe_host and cluster.get("partitions"):
            first_part = next(iter(cluster["partitions"].values()), {})
            first_nodes = first_part.get("nodes", [])
            if first_nodes:
                probe_host = first_nodes[0]

        click.echo()
        click.echo(click.style(
            "  Optional Features", fg="green", bold=True))
        click.echo()

        has_gpu_cmd = has_command(
            "nvidia-smi", probe_host, ssh_user, ssh_key)
        if has_gpu_cmd:
            click.echo("  ✓ GPU support detected (nvidia-smi found)")
            cluster["has_gpu"] = click.confirm(
                "  Enable GPU monitoring?", default=True)
        else:
            click.echo("  ○ nvidia-smi not found (no GPU detected)")
            cluster["has_gpu"] = click.confirm(
                "  Enable GPU monitoring anyway?", default=False)

        has_nfs_cmd = has_command(
            "nfsiostat", probe_host, ssh_user, ssh_key)
        if has_nfs_cmd:
            click.echo(
                "  ✓ NFS monitoring available (nfsiostat found)")
            cluster["has_nfs"] = click.confirm(
                "  Enable NFS monitoring?", default=True)
        else:
            click.echo("  ○ nfsiostat not found (no NFS detected)")
            cluster["has_nfs"] = click.confirm(
                "  Enable NFS monitoring anyway?", default=False)

        has_jup = run_cmd(
            "pgrep -f jupyterhub",
            probe_host, ssh_user, ssh_key) is not None
        has_rst = run_cmd(
            "pgrep -f rserver",
            probe_host, ssh_user, ssh_key) is not None
        if has_jup or has_rst:
            services = []
            if has_jup:
                services.append("JupyterHub")
            if has_rst:
                services.append("RStudio Server")
            click.echo(f"  ✓ Detected: {', '.join(services)}")
            cluster["has_interactive"] = click.confirm(
                "  Enable interactive session monitoring?",
                default=True)
        else:
            click.echo(
                "  ○ No JupyterHub or RStudio Server detected")
            cluster["has_interactive"] = click.confirm(
                "  Enable interactive session monitoring?",
                default=False)

    def show_cluster_summary(cluster, is_remote):
        """Display a summary of a configured cluster."""
        click.echo(click.style(
            f"  ─── Summary: {cluster['name']} ───", fg="cyan"))
        click.echo()
        ctype_label = ("HPC cluster"
                       if cluster.get("type") == "hpc"
                       else "Workstation group")
        click.echo(f"    Type:         {ctype_label}")
        if cluster.get("host"):
            click.echo(f"    Headnode:     {cluster['host']}")
        if cluster.get("ssh_user"):
            click.echo(f"    SSH user:     {cluster['ssh_user']}")
        parts = cluster.get("partitions", {})
        part_label = ("Partition" if cluster.get("type") == "hpc"
                      else "Group")
        for pid, pdata in parts.items():
            gpu_info = (f" ({len(pdata['gpu_nodes'])} GPU)"
                        if pdata.get("gpu_nodes") else "")
            click.echo(
                f"    {part_label}:    {pid}"
                f" — {len(pdata['nodes'])} nodes{gpu_info}")
        click.echo(
            f"    Filesystems:  "
            f"{', '.join(cluster.get('filesystems', []))}")
        feats = []
        if cluster.get("has_gpu"):
            feats.append("GPU")
        if cluster.get("has_nfs"):
            feats.append("NFS")
        if cluster.get("has_interactive"):
            feats.append("Interactive")
        if feats:
            click.echo(f"    Monitoring:   {', '.join(feats)}")
        click.echo()

    # ── Banner ───────────────────────────────────────────────────────
    click.echo()
    click.echo(click.style(
        "  ◈ NØMADE Setup Wizard", fg="cyan", bold=True))
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo()

    # ── Check for previous incomplete setup ──────────────────────────
    saved = load_state()
    resume = False

    if saved and not quick:
        from datetime import datetime as dt
        try:
            ts = dt.fromisoformat(saved['_timestamp'])
            age = ts.strftime('%b %d at %H:%M')
        except Exception:
            age = "unknown time"

        click.echo("  A previous setup was interrupted.")
        click.echo()

        if saved.get('clusters'):
            click.echo("  Progress saved:")
            mode_label = ("remote (SSH)" if saved.get('is_remote')
                          else "local (headnode)")
            click.echo(f"    Mode: {mode_label}")
            for ci, c in enumerate(saved['clusters']):
                pcount = len(c.get('partitions', {}))
                click.echo(
                    f"    Cluster {ci+1}: "
                    f"{c.get('name', '?')} ({pcount} partitions)")
            remaining = (saved.get('num_clusters', 1)
                         - len(saved['clusters']))
            if remaining > 0:
                click.echo(
                    f"    {remaining} cluster(s) still to configure")
        click.echo(f"    (from {age})")
        click.echo()

        resume = click.confirm(
            "  Continue where you left off?", default=True)
        click.echo()

        if not resume:
            clear_state()
            saved = None

    if not saved or not resume:
        click.echo(
            "  This wizard will help you configure NØMADE for your")
        click.echo(
            "  HPC environment. Press Enter to accept the default")
        click.echo("  value shown in [brackets].")
        click.echo()

    # ── Collect configuration ────────────────────────────────────────
    clusters = (saved.get('clusters', [])
                if (saved and resume) else [])
    admin_email = (saved.get('admin_email', '')
                   if (saved and resume) else "")
    dash_port = (saved.get('dash_port', 8050)
                 if (saved and resume) else 8050)

    if quick:
        # ── Quick mode: auto-detect everything ───────────────────────
        click.echo("  Quick mode: auto-detecting your environment...")
        click.echo()
        hostname = run_cmd("hostname -s") or "my-cluster"
        partitions = detect_partitions()
        gpu_nodes = detect_gpu_nodes()
        filesystems = detect_filesystems()
        has_gpu = has_command("nvidia-smi")
        has_nfs = has_command("nfsiostat")
        has_jupyter = (
            run_cmd("pgrep -f jupyterhub") is not None)
        has_rstudio = (
            run_cmd("pgrep -f rserver") is not None)

        cluster = {
            "name": hostname,
            "mode": "local",
            "type": "hpc",
            "partitions": {},
            "filesystems": filesystems,
            "has_gpu": has_gpu,
            "has_nfs": has_nfs,
            "has_interactive": has_jupyter or has_rstudio,
        }
        for p in partitions:
            nodes = detect_nodes_per_partition(p)
            cluster["partitions"][p] = {
                "nodes": nodes,
                "gpu_nodes": [n for n in nodes if n in gpu_nodes],
            }
        clusters.append(cluster)

        click.echo(f"  Cluster:      {hostname}")
        click.echo(
            f"  Partitions:   "
            f"{', '.join(partitions) or 'none detected'}")
        click.echo(f"  Filesystems:  {', '.join(filesystems)}")
        click.echo(f"  GPU:          {'yes' if has_gpu else 'no'}")
        click.echo(f"  NFS:          {'yes' if has_nfs else 'no'}")
        click.echo(
            f"  Interactive:  "
            f"{'yes' if cluster['has_interactive'] else 'no'}")
        click.echo()

    else:
        # ── Interactive wizard ───────────────────────────────────────

        # Restore or ask for connection mode
        if saved and resume and 'is_remote' in saved:
            is_remote = saved['is_remote']
            num_clusters = saved.get('num_clusters', 1)
        else:
            # Step 1: Connection mode
            click.echo(click.style(
                "  Step 1: Connection Mode",
                fg="green", bold=True))
            click.echo()
            click.echo("  Where is NØMADE running?")
            click.echo()
            click.echo("    1) On the cluster headnode")
            click.echo(
                "       NØMADE has direct access to SLURM commands")
            click.echo("       like sinfo, squeue, and sacct.")
            click.echo()
            click.echo(
                "    2) On a separate machine"
                " (laptop, desktop, etc.)")
            click.echo(
                "       NØMADE will connect to your cluster(s)"
                " via SSH")
            click.echo(
                "       to run commands and collect data remotely.")
            click.echo()
            mode_choice = click.prompt(
                "  Select", type=click.IntRange(1, 2), default=1)
            is_remote = (mode_choice == 2)
            click.echo()
            save_state({
                'is_remote': is_remote, 'clusters': clusters,
                'admin_email': admin_email,
                'dash_port': dash_port})

            # ── SSH key setup helper (remote only) ───────────────────
            if is_remote:
                click.echo(click.style(
                    "  SSH Key Setup", fg="green", bold=True))
                click.echo()
                click.echo(
                    "  Remote mode requires SSH key authentication"
                    " so")
                click.echo(
                    "  NØMADE can connect to your cluster(s) without")
                click.echo(
                    "  asking for a password every time.")
                click.echo()

                ssh_dir = Path.home() / ".ssh"
                key_types = [
                    ("id_ed25519", "Ed25519 (recommended)"),
                    ("id_rsa", "RSA"),
                    ("id_ecdsa", "ECDSA"),
                ]
                found_keys = []
                for keyfile, label in key_types:
                    if (ssh_dir / keyfile).exists():
                        found_keys.append((keyfile, label))

                if found_keys:
                    click.echo("  ✓ Found existing SSH key(s):")
                    for keyfile, label in found_keys:
                        click.echo(
                            f"    • ~/.ssh/{keyfile} ({label})")
                    click.echo()
                else:
                    click.echo("  ○ No SSH keys found in ~/.ssh/")
                    click.echo()
                    click.echo(
                        "  An SSH key is like a digital ID card"
                        " that")
                    click.echo(
                        "  lets your computer prove who you are"
                        " to a")
                    click.echo(
                        "  remote server, without needing to type"
                        " a")
                    click.echo("  password.")
                    click.echo()

                    if click.confirm(
                            "  Would you like NØMADE to create one"
                            " for you?", default=True):
                        click.echo()
                        ssh_dir.mkdir(mode=0o700, exist_ok=True)
                        key_path = ssh_dir / "id_ed25519"
                        email = click.prompt(
                            "  Your email"
                            " (used as a label on the key)",
                            default=(os.getenv("USER", "user")
                                     + "@localhost"))
                        click.echo()
                        click.echo(
                            "  Generating SSH key... ", nl=False)

                        result = sp.run(
                            ["ssh-keygen", "-t", "ed25519",
                             "-C", email,
                             "-f", str(key_path), "-N", ""],
                            capture_output=True, text=True)
                        if result.returncode == 0:
                            click.echo(click.style(
                                "✓ Created", fg="green"))
                            click.echo(
                                "    Private key:"
                                " ~/.ssh/id_ed25519")
                            click.echo(
                                "    Public key: "
                                " ~/.ssh/id_ed25519.pub")
                            found_keys.append(
                                ("id_ed25519", "Ed25519"))
                        else:
                            click.echo(click.style(
                                "✗ Failed", fg="red"))
                            click.echo(
                                f"    {result.stderr.strip()}")
                            click.echo()
                            click.echo(
                                "  You can create one"
                                " manually later:")
                            click.echo(
                                '    ssh-keygen -t ed25519'
                                ' -C "your@email.com"')
                        click.echo()
                    else:
                        click.echo()
                        click.echo(
                            "  You can create one later"
                            " by running:")
                        click.echo(
                            '    ssh-keygen -t ed25519'
                            ' -C "your@email.com"')
                        click.echo()

                if found_keys:
                    click.echo(
                        "  To connect without a password, your"
                        " public")
                    click.echo(
                        "  key needs to be copied to each cluster.")
                    click.echo(
                        "  NØMADE can do this for you now.")
                    click.echo()
                    click.echo(
                        "  (This will ask for your cluster password"
                        " ONE TIME.")
                    click.echo(
                        "   After that, SSH will use the key"
                        " automatically.)")
                    click.echo()

                    if click.confirm(
                            "  Copy SSH key to your cluster(s)"
                            " now?",
                            default=True):
                        click.echo()
                        copy_host = click.prompt(
                            "  Cluster headnode hostname"
                            " (e.g., cluster.university.edu)")
                        copy_user = click.prompt(
                            "  SSH username"
                            " (your login on the cluster)")
                        click.echo()

                        key_to_copy = str(
                            ssh_dir / found_keys[0][0])
                        click.echo(
                            f"  Copying {found_keys[0][0]}"
                            f" to {copy_host}...")
                        click.echo(
                            f"  You will be asked for your"
                            f" password on {copy_host}.")
                        click.echo()

                        copy_result = sp.run(
                            ["ssh-copy-id",
                             "-i", key_to_copy + ".pub",
                             f"{copy_user}@{copy_host}"])
                        click.echo()
                        if copy_result.returncode == 0:
                            click.echo(click.style(
                                "  ✓ Key copied! Password-free"
                                " SSH is ready.", fg="green"))
                        else:
                            click.echo(click.style(
                                "  ✗ Could not copy key"
                                " automatically.",
                                fg="yellow"))
                            click.echo()
                            click.echo(
                                "  You can do it manually"
                                " later:")
                            click.echo(
                                "    ssh-copy-id your_username"
                                "@cluster.university.edu")
                        click.echo()
                    else:
                        click.echo()
                        click.echo(
                            "  No problem. Copy your key"
                            " later with:")
                        click.echo(
                            "    ssh-copy-id your_username"
                            "@cluster.university.edu")
                        click.echo()

            # Step 2: Number of clusters
            click.echo(click.style(
                "  Step 2: Clusters", fg="green", bold=True))
            click.echo()
            click.echo(
                "  How many HPC clusters or workstation groups"
                " do you")
            click.echo(
                "  want to monitor? Most sites have 1-3 clusters.")
            click.echo()
            num_clusters = click.prompt(
                "  Number of clusters",
                type=click.IntRange(1, 20), default=1)
            click.echo()
            save_state({
                'is_remote': is_remote,
                'num_clusters': num_clusters,
                'clusters': clusters,
                'admin_email': admin_email,
                'dash_port': dash_port})

        # ── Step 3: Configure each cluster ───────────────────────────
        start_from = len(clusters)

        for i in range(start_from, num_clusters):
            click.echo(click.style(
                f"  ─── Cluster {i + 1} of {num_clusters}"
                f" {'─' * 25}", fg="green"))
            click.echo()

            # Cluster name — always generic default
            default_name = f"cluster-{i + 1}"
            name = click.prompt(
                "  Cluster name", default=default_name)

            # Cluster type
            click.echo()
            click.echo("  What type of system is this?")
            click.echo(
                "    1) HPC cluster (managed by SLURM)")
            click.echo(
                "    2) Workstation group"
                " (department machines, not SLURM)")
            click.echo()
            ctype = click.prompt(
                "  Select",
                type=click.IntRange(1, 2), default=1)
            is_hpc = (ctype == 1)
            click.echo()

            cluster = {
                "name": name,
                "mode": "remote" if is_remote else "local",
                "type": "hpc" if is_hpc else "workstations",
                "partitions": {},
                "filesystems": ['/', '/home'],
                "has_gpu": False,
                "has_nfs": False,
                "has_interactive": False,
            }

            # SSH details (remote only)
            ssh_user = None
            ssh_key = None
            host = None
            if is_remote:
                if is_hpc:
                    # HPC: need a headnode to SSH into
                    click.echo("  SSH connection details:")
                    click.echo(
                        "  (NØMADE will use SSH to reach"
                        " this cluster)")
                    click.echo()
                    host = click.prompt(
                        "  Headnode hostname"
                        " (e.g., cluster.university.edu)")
                else:
                    # Workstations: no headnode, NØMADE connects
                    # directly to each machine
                    click.echo("  SSH connection details:")
                    click.echo(
                        "  For workstation groups, NØMADE connects")
                    click.echo(
                        "  directly to each machine via SSH. Just")
                    click.echo(
                        "  provide a username and key below — the")
                    click.echo(
                        "  individual machine hostnames will be set")
                    click.echo(
                        "  when you list your departments.")
                    click.echo()
                    host = None

                ssh_user = click.prompt(
                    "  SSH username"
                    " (your login on the machines)")
                default_key = str(
                    Path.home() / ".ssh" / "id_ed25519")
                if not Path(default_key).exists():
                    default_key = str(
                        Path.home() / ".ssh" / "id_rsa")
                ssh_key = click.prompt(
                    "  SSH key path", default=default_key)

                if host:
                    cluster["host"] = host
                cluster["ssh_user"] = ssh_user
                cluster["ssh_key"] = ssh_key

                # Test connection (HPC headnode only;
                # workstation nodes tested per-department)
                if host:
                    click.echo()
                    click.echo(
                        "  Testing SSH connection... ", nl=False)
                    test = run_cmd(
                        "echo ok", host, ssh_user, ssh_key)
                    if test:
                        click.echo(click.style(
                            "✓ Connected", fg="green"))
                    else:
                        click.echo(click.style(
                            "✗ Could not connect", fg="red"))
                        click.echo()
                        click.echo("  Check that:")
                        click.echo(
                            f"    - {host} is reachable"
                            f" from this machine")
                        click.echo(
                            f"    - SSH key {ssh_key} exists"
                            f" and is authorized")
                        click.echo(
                            f"    - Username '{ssh_user}'"
                            f" is correct")
                        click.echo()
                        click.echo(
                            "  You can fix these settings in the"
                            " config file later.")
                click.echo()

            # Collect partitions, filesystems, features
            collect_partitions(cluster, host, ssh_user, ssh_key)
            collect_filesystems(cluster, host, ssh_user, ssh_key)
            collect_features(cluster, host, ssh_user, ssh_key)
            click.echo()

            # ── Confirm / edit / redo loop ───────────────────────────
            while True:
                show_cluster_summary(cluster, is_remote)

                choice = click.prompt(
                    "  Is this correct?"
                    " (y)es / (e)dit / (s)tart over",
                    type=click.Choice(
                        ['y', 'e', 's'],
                        case_sensitive=False),
                    default='y')

                if choice == 'y':
                    break

                elif choice == 's':
                    # Redo entire cluster
                    click.echo()
                    click.echo(click.style(
                        f"  ─── Cluster {i + 1}"
                        f" of {num_clusters}"
                        f" (redo) {'─' * 19}",
                        fg="green"))
                    click.echo()

                    name = click.prompt(
                        "  Cluster name",
                        default=cluster["name"])
                    cluster["name"] = name

                    click.echo()
                    click.echo(
                        "  What type of system is this?")
                    click.echo(
                        "    1) HPC cluster"
                        " (managed by SLURM)")
                    click.echo(
                        "    2) Workstation group"
                        " (department machines,"
                        " not SLURM)")
                    click.echo()
                    ctype = click.prompt(
                        "  Select",
                        type=click.IntRange(1, 2),
                        default=1)
                    is_hpc = (ctype == 1)
                    cluster["type"] = (
                        "hpc" if is_hpc
                        else "workstations")
                    click.echo()

                    if is_remote:
                        if is_hpc:
                            host = click.prompt(
                                "  Headnode hostname",
                                default=cluster.get(
                                    "host", ""))
                            cluster["host"] = host
                        else:
                            host = None
                            cluster.pop("host", None)
                        ssh_user = click.prompt(
                            "  SSH username"
                            " (your login on the machines)",
                            default=cluster.get(
                                "ssh_user", ""))
                        ssh_key = click.prompt(
                            "  SSH key path",
                            default=cluster.get(
                                "ssh_key", ""))
                        cluster["ssh_user"] = ssh_user
                        cluster["ssh_key"] = ssh_key
                        click.echo()

                    collect_partitions(
                        cluster, host, ssh_user, ssh_key)
                    collect_filesystems(
                        cluster, host, ssh_user, ssh_key)
                    collect_features(
                        cluster, host, ssh_user, ssh_key)
                    click.echo()
                    continue

                elif choice == 'e':
                    click.echo()
                    click.echo(
                        "  What would you like to edit?")
                    click.echo("    1) Cluster name")
                    click.echo(
                        "    2) Partitions and nodes")
                    click.echo("    3) Filesystems")
                    click.echo(
                        "    4) Optional features"
                        " (GPU / NFS / Interactive)")
                    if is_remote:
                        click.echo(
                            "    5) SSH connection")
                    click.echo()
                    max_opt = 5 if is_remote else 4
                    edit_choice = click.prompt(
                        "  Select",
                        type=click.IntRange(1, max_opt))

                    if edit_choice == 1:
                        cluster["name"] = click.prompt(
                            "  Cluster name",
                            default=cluster["name"])

                    elif edit_choice == 2:
                        click.echo()
                        p_label = ("partitions"
                                   if cluster.get("type") == "hpc"
                                   else "groups")
                        click.echo(
                            f"  Current {p_label}:")
                        for pid, pdata in (
                                cluster[
                                    "partitions"].items()):
                            ncount = len(pdata["nodes"])
                            click.echo(
                                f"    • {pid}"
                                f" ({ncount} nodes)")
                        click.echo()
                        click.echo(
                            f"  Enter ALL {p_label} names"
                            f" you want")
                        click.echo(
                            "  (this replaces the"
                            " current list):")
                        current = ', '.join(
                            cluster["partitions"].keys())
                        new_str = click.prompt(
                            f"  {'Partitions' if cluster.get('type') == 'hpc' else 'Groups'}",
                            default=current)
                        new_parts = [
                            p.strip()
                            for p in new_str.split(',')
                            if p.strip()]

                        _h = cluster.get("host")
                        _u = cluster.get("ssh_user")
                        _k = cluster.get("ssh_key")
                        gn = detect_gpu_nodes(_h, _u, _k)

                        new_partitions = {}
                        for p in new_parts:
                            if p in cluster["partitions"]:
                                new_partitions[p] = (
                                    cluster[
                                        "partitions"][p])
                                nc = len(
                                    new_partitions[p][
                                        'nodes'])
                                click.echo(
                                    f"    {p}: keeping"
                                    f" {nc} nodes")
                            else:
                                if cluster.get("type") == "hpc":
                                    nodes = (
                                        detect_nodes_per_partition(
                                            p, _h, _u, _k))
                                    pg = [n for n in nodes
                                          if n in gn]
                                    if nodes:
                                        click.echo(
                                            f"    {p}:"
                                            f" detected"
                                            f" {len(nodes)}"
                                            f" nodes")
                                    else:
                                        click.echo(
                                            f"  Nodes for"
                                            f" '{p}',"
                                            f" comma-separated:")
                                        ns = click.prompt(
                                            f"  Nodes for {p}")
                                        nodes = [
                                            n.strip()
                                            for n in
                                            ns.split(',')
                                            if n.strip()]
                                        pg = []
                                else:
                                    click.echo(
                                        f"  Hostnames for"
                                        f" '{p}',"
                                        f" comma-separated:")
                                    ns = click.prompt(
                                        f"  Nodes for {p}")
                                    nodes = [
                                        n.strip()
                                        for n in
                                        ns.split(',')
                                        if n.strip()]
                                    pg = []
                                new_partitions[p] = {
                                    "nodes": nodes,
                                    "gpu_nodes": pg}
                        cluster["partitions"] = (
                            new_partitions)

                    elif edit_choice == 3:
                        current_fs = ', '.join(
                            cluster.get(
                                "filesystems", []))
                        fs_str = click.prompt(
                            "  Filesystems"
                            " (comma-separated)",
                            default=current_fs)
                        cluster["filesystems"] = [
                            f.strip()
                            for f in fs_str.split(',')
                            if f.strip()]

                    elif edit_choice == 4:
                        cluster["has_gpu"] = (
                            click.confirm(
                                "  Enable GPU monitoring?",
                                default=cluster.get(
                                    "has_gpu", False)))
                        cluster["has_nfs"] = (
                            click.confirm(
                                "  Enable NFS monitoring?",
                                default=cluster.get(
                                    "has_nfs", False)))
                        cluster["has_interactive"] = (
                            click.confirm(
                                "  Enable interactive"
                                " session monitoring?",
                                default=cluster.get(
                                    "has_interactive",
                                    False)))

                    elif (edit_choice == 5
                          and is_remote):
                        if is_hpc:
                            cluster["host"] = click.prompt(
                                "  Headnode hostname",
                                default=cluster.get(
                                    "host", ""))
                            host = cluster["host"]
                        cluster["ssh_user"] = (
                            click.prompt(
                                "  SSH username"
                                " (your login on"
                                " the machines)",
                                default=cluster.get(
                                    "ssh_user", "")))
                        cluster["ssh_key"] = (
                            click.prompt(
                                "  SSH key path",
                                default=cluster.get(
                                    "ssh_key", "")))
                        ssh_user = cluster["ssh_user"]
                        ssh_key = cluster["ssh_key"]

                    click.echo()
                    continue

            # Save after each confirmed cluster
            clusters.append(cluster)
            save_state({
                'is_remote': is_remote,
                'num_clusters': num_clusters,
                'clusters': clusters,
                'admin_email': admin_email,
                'dash_port': dash_port})

        # ── Alerts ───────────────────────────────────────────────────
        click.echo(click.style(
            "  Step 3: Alerts", fg="green", bold=True))
        click.echo()
        click.echo(
            "  NØMADE can send you email alerts when something"
            " needs")
        click.echo(
            "  attention (disk filling up, nodes going down,"
            " etc.).")
        click.echo(
            "  You can also view all alerts in the dashboard.")
        click.echo()
        admin_email = click.prompt(
            "  Your email address (press Enter to skip)",
            default="", show_default=False)
        click.echo()
        save_state({
            'is_remote': is_remote,
            'num_clusters': num_clusters,
            'clusters': clusters,
            'admin_email': admin_email,
            'dash_port': dash_port})

        # ── Dashboard ────────────────────────────────────────────────
        click.echo(click.style(
            "  Step 4: Dashboard", fg="green", bold=True))
        click.echo()
        click.echo(
            "  The NØMADE dashboard is a web page you open in"
            " your")
        click.echo(
            "  browser to view cluster status, node health, and")
        click.echo(
            "  alerts. It runs on a port you choose.")
        click.echo()
        dash_port = click.prompt(
            "  Dashboard port", type=int, default=8050)
        click.echo()

    # ══════════════════════════════════════════════════════════════════
    # Generate TOML config file
    # ══════════════════════════════════════════════════════════════════
    lines = []
    lines.append("# NØMADE Configuration File")
    lines.append("# Generated by: nomade init")
    lines.append(
        f"# Date:"
        f" {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    lines.append("[general]")
    lines.append('log_level = "info"')
    lines.append(f'data_dir = "{data_dir}"')
    lines.append("")

    lines.append("[database]")
    lines.append('path = "nomade.db"')
    lines.append("")

    # Collectors
    coll_list = ["disk", "slurm", "node_state"]
    any_gpu = any(c.get("has_gpu") for c in clusters)
    any_nfs = any(c.get("has_nfs") for c in clusters)
    any_interactive = any(
        c.get("has_interactive") for c in clusters)
    if any_gpu:
        coll_list.append("gpu")
    if any_nfs:
        coll_list.append("nfs")
    if any_interactive:
        coll_list.append("interactive")

    lines.append("[collectors]")
    coll_str = ', '.join(f'"{c}"' for c in coll_list)
    lines.append(f"enabled = [{coll_str}]")
    lines.append("interval = 60")
    lines.append("")

    # Filesystems
    all_fs = set()
    for c in clusters:
        all_fs.update(c.get("filesystems", []))
    fs_items = ', '.join(f'"{f}"' for f in sorted(all_fs))
    lines.append("[collectors.disk]")
    lines.append(f"filesystems = [{fs_items}]")
    lines.append("")

    # SLURM partitions
    all_parts = set()
    for c in clusters:
        if c.get("type", "hpc") == "hpc":
            all_parts.update(
                c.get("partitions", {}).keys())
    if all_parts:
        parts_items = ', '.join(
            f'"{p}"' for p in sorted(all_parts))
        lines.append("[collectors.slurm]")
        lines.append(f"partitions = [{parts_items}]")
        lines.append("")

    if any_gpu:
        lines.append("[collectors.gpu]")
        lines.append("enabled = true")
        lines.append("")

    if any_nfs:
        lines.append("[collectors.nfs]")
        lines.append("mount_points = []")
        lines.append("")

    # Clusters
    lines.append("# ============================================")
    lines.append("# CLUSTERS")
    lines.append("# ============================================")
    lines.append("")

    for cluster in clusters:
        cid = cluster["name"].lower().replace(' ', '-')
        lines.append(f'[clusters.{cid}]')
        lines.append(f'name = "{cluster["name"]}"')
        lines.append(
            f'type = "{cluster.get("type", "hpc")}"')

        if cluster.get("mode") == "remote":
            if cluster.get("host"):
                lines.append(f'host = "{cluster["host"]}"')
            lines.append(
                f'ssh_user = "{cluster["ssh_user"]}"')
            lines.append(
                f'ssh_key = "{cluster["ssh_key"]}"')

        total_nodes = sum(
            len(p["nodes"])
            for p in cluster["partitions"].values())
        ctype_label = (
            "cluster" if cluster.get("type") == "hpc"
            else "workstation group")
        lines.append(
            f'description ='
            f' "{total_nodes}-node {ctype_label}"')
        lines.append("")

        sect_label = ("partitions"
                      if cluster.get("type") == "hpc"
                      else "groups")
        for pid, pdata in cluster["partitions"].items():
            lines.append(
                f'[clusters.{cid}.{sect_label}.{pid}]')
            desc_label = ("partition"
                          if cluster.get("type") == "hpc"
                          else "group")
            lines.append(
                f'description ='
                f' "{len(pdata["nodes"])}-node {desc_label}"')
            nodes_items = ', '.join(
                f'"{n}"' for n in pdata["nodes"])
            lines.append(f'nodes = [{nodes_items}]')
            if pdata.get("gpu_nodes"):
                gpu_items = ', '.join(
                    f'"{n}"' for n in pdata["gpu_nodes"])
                lines.append(f'gpu_nodes = [{gpu_items}]')
            lines.append("")

    # Alerts
    lines.append("# ============================================")
    lines.append("# ALERTS")
    lines.append("# ============================================")
    lines.append("")
    lines.append("[alerts]")
    lines.append("enabled = true")
    lines.append('min_severity = "warning"')
    lines.append("cooldown_minutes = 15")
    lines.append("")
    lines.append("[alerts.thresholds.disk]")
    lines.append("used_percent_warning = 80")
    lines.append("used_percent_critical = 95")
    lines.append("")

    if any_interactive:
        lines.append("[alerts.thresholds.interactive]")
        lines.append("idle_sessions_warning = 50")
        lines.append("idle_sessions_critical = 100")
        lines.append("memory_gb_warning = 32")
        lines.append("memory_gb_critical = 64")
        lines.append("")

    if admin_email:
        lines.append("[alerts.email]")
        lines.append("enabled = true")
        lines.append(
            "# Update these with your SMTP server details:")
        lines.append('smtp_server = "smtp.example.com"')
        lines.append("smtp_port = 587")
        lines.append('from_address = "nomade@example.com"')
        lines.append(f'recipients = ["{admin_email}"]')
        lines.append("")

    # Dashboard
    lines.append("# ============================================")
    lines.append("# DASHBOARD")
    lines.append("# ============================================")
    lines.append("")
    lines.append("[dashboard]")
    lines.append('host = "127.0.0.1"')
    lines.append(f"port = {dash_port}")
    lines.append("")

    # ML
    lines.append("# ============================================")
    lines.append("# ML PREDICTION")
    lines.append("# ============================================")
    lines.append("")
    lines.append("[ml]")
    lines.append("enabled = true")
    lines.append("")

    # Write the config
    config_content = '\n'.join(lines)
    config_file.write_text(config_content)

    # Clean up wizard state file
    clear_state()

    # ── Summary ──────────────────────────────────────────────────────
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo(click.style(
        "  ✓ NØMADE configured!", fg="green", bold=True))
    click.echo()
    click.echo(f"  Config:  {config_file}")
    click.echo(f"  Data:    {data_dir}")
    click.echo()
    click.echo("  Clusters:")
    for c in clusters:
        pcount = len(c["partitions"])
        ncount = sum(
            len(p["nodes"])
            for p in c["partitions"].values())
        if c.get("host"):
            loc = f" → {c['host']}"
        elif c.get("mode") == "remote":
            loc = " (SSH to each node)"
        else:
            loc = " (local)"
        plabel = ("partitions"
                  if c.get("type") == "hpc"
                  else "groups")
        click.echo(
            f"    • {c['name']}:"
            f" {pcount} {plabel},"
            f" {ncount} nodes{loc}")
    click.echo()

    features = []
    if any_gpu:
        features.append("GPU monitoring")
    if any_nfs:
        features.append("NFS monitoring")
    if any_interactive:
        features.append("interactive sessions")
    if features:
        click.echo(f"  Enabled: {', '.join(features)}")
        click.echo()

    click.echo(click.style("  What to do next:", bold=True))
    click.echo()
    click.echo(f"    1. Review your config (optional):")
    click.echo(f"         nano {config_file}")
    click.echo()
    click.echo(f"    2. Check that everything is ready:")
    click.echo(f"         nomade syscheck")
    click.echo()
    click.echo(f"    3. Start collecting data:")
    click.echo(f"         nomade collect")
    click.echo()
    click.echo(
        f"    4. Open the dashboard in your browser:")
    click.echo(f"         nomade dashboard")
    click.echo()


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




# =============================================================================
# EDU COMMANDS
# =============================================================================

@cli.group()
def edu():
    """NØMADE Edu — Educational analytics for HPC.

    Measures the development of computational proficiency over time
    by analyzing per-job behavioral fingerprints.
    """
    pass


@edu.command('explain')
@click.argument('job_id')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--no-progress', is_flag=True, help='Skip progress comparison')
@click.pass_context
def edu_explain(ctx, job_id, db_path, output_json, no_progress):
    """Explain a job in plain language with proficiency scores.

    Analyzes a completed job across five dimensions of computational
    proficiency: CPU efficiency, memory sizing, time estimation,
    I/O awareness, and GPU utilization.

    Examples:
        nomade edu explain 12345
        nomade edu explain 12345 --json
        nomade edu explain 12345 --no-progress
    """
    from nomade.edu.explain import explain_job

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    result = explain_job(
        job_id=job_id,
        db_path=db_path,
        show_progress=not no_progress,
        output_format='json' if output_json else 'terminal',
    )

    if result is None:
        click.echo(f"Job {job_id} not found in database.", err=True)
        click.echo("\nHint: Specify a database with --db or run 'nomade init' to configure.", err=True)
        click.echo("  Example: nomade edu explain {job_id} --db ~/nomade_demo.db", err=True)
        raise SystemExit(1)

    click.echo(result)


@edu.command('trajectory')
@click.argument('username')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=90, help='Lookback period in days (default: 90)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_trajectory(ctx, username, db_path, days, output_json):
    """Show a user's proficiency development over time.

    Tracks how a student or researcher's HPC skills evolve across
    their job submissions, highlighting areas of improvement and
    dimensions that need attention.

    Examples:
        nomade edu trajectory student01
        nomade edu trajectory student01 --days 30
    """
    from nomade.edu.progress import user_trajectory, format_trajectory
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    traj = user_trajectory(db_path, username, days)

    if traj is None:
        click.echo(f"Not enough data for {username} (need at least 3 completed jobs).", err=True)
        click.echo("\nHint: Specify a database with --db or run 'nomade init' to configure.", err=True)
        click.echo(f"  Example: nomade edu trajectory {username} --db ~/nomade_demo.db", err=True)
        raise SystemExit(1)

    if output_json:
        result = {
            "username": traj.username,
            "total_jobs": traj.total_jobs,
            "date_range": traj.date_range,
            "overall_improvement": traj.overall_improvement,
            "summary": traj.summary,
            "current_scores": traj.current_scores,
            "improvement": traj.improvement,
            "windows": [
                {"start": w.start, "end": w.end, "job_count": w.job_count,
                 "scores": w.scores, "overall": w.overall}
                for w in traj.windows
            ],
        }
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(format_trajectory(traj))


@edu.command('report')
@click.argument('group_name')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=90, help='Lookback period in days (default: 90)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_report(ctx, group_name, db_path, days, output_json):
    """Generate a proficiency report for a course or lab group.

    Aggregates per-student proficiency data to produce insights like
    "15/20 students improved memory efficiency over the semester."

    The group_name maps to a Linux group (from SLURM accounting or
    LDAP). Configure group filters in nomade.toml.

    Examples:
        nomade edu report bio301
        nomade edu report bio301 --days 120
        nomade edu report physics-lab --json
    """
    from nomade.edu.progress import group_summary, format_group_summary
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    gs = group_summary(db_path, group_name, days)

    if gs is None:
        click.echo(f"No data found for group '{group_name}'.", err=True)
        click.echo("Ensure group membership data has been collected:")
        click.echo("  nomade collect -C groups --once")
        click.echo("\nOr specify a database with --db:", err=True)
        click.echo(f"  nomade edu report {group_name} --db ~/nomade_demo.db", err=True)
        raise SystemExit(1)

    if output_json:
        result = {
            "group_name": gs.group_name,
            "member_count": gs.member_count,
            "total_jobs": gs.total_jobs,
            "date_range": gs.date_range,
            "improvement_rate": gs.improvement_rate,
            "avg_overall": gs.avg_overall,
            "avg_improvement": gs.avg_improvement,
            "users_improving": gs.users_improving,
            "users_stable": gs.users_stable,
            "users_declining": gs.users_declining,
            "dimension_avgs": gs.dimension_avgs,
            "dimension_improvements": gs.dimension_improvements,
            "weakest_dimension": gs.weakest_dimension,
            "strongest_dimension": gs.strongest_dimension,
            "users": [
                {"username": t.username, "total_jobs": t.total_jobs,
                 "overall_improvement": t.overall_improvement,
                 "current_scores": t.current_scores}
                for t in gs.users
            ],
        }
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(format_group_summary(gs))


def main() -> None:
    """Entry point for CLI."""
# =============================================================================
# COMMUNITY COMMANDS
# =============================================================================

@cli.group()
def community():
    """NØMADE Community Dataset commands."""
    pass


@community.command('export')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--output', '-o', required=True, type=click.Path(), help='Output file (.parquet or .json)')
@click.option('--salt-file', type=click.Path(exists=True), help='File containing institution salt')
@click.option('--salt', help='Institution salt (use --salt-file for security)')
@click.option('--institution-type', type=click.Choice(['academic', 'government', 'industry', 'nonprofit']), 
              default='academic', help='Institution type')
@click.option('--cluster-type', type=click.Choice([
    'cpu_small', 'cpu_medium', 'cpu_large',
    'gpu_small', 'gpu_medium', 'gpu_large', 
    'mixed_small', 'mixed_medium', 'mixed_large'
]), default='mixed_small', help='Cluster type')
@click.option('--start-date', help='Start date (YYYY-MM-DD)')
@click.option('--end-date', help='End date (YYYY-MM-DD)')
@click.pass_context
def community_export(ctx, db_path, output, salt_file, salt, institution_type, cluster_type, start_date, end_date):
    """Export anonymized data for community dataset."""
    from nomade.community import export_community_data
    from pathlib import Path
    
    if salt_file:
        with open(salt_file) as f:
            salt = f.read().strip()
    elif not salt:
        click.echo("Error: Either --salt or --salt-file is required", err=True)
        raise SystemExit(1)
    
    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = config.get('database', {}).get('path')
        if not db_path:
            default_db = Path.home() / '.config' / 'nomade' / 'nomade.db'
            if default_db.exists():
                db_path = str(default_db)
            else:
                click.echo("Error: No database found. Use --db to specify path.", err=True)
                raise SystemExit(1)
    
    try:
        export_community_data(
            db_path=Path(db_path),
            output_path=Path(output),
            salt=salt,
            institution_type=institution_type,
            cluster_type=cluster_type,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


@community.command('verify')
@click.argument('file_path', type=click.Path(exists=True))
def community_verify(file_path):
    """Verify an export file meets community standards."""
    from nomade.community import verify_export
    from pathlib import Path
    result = verify_export(Path(file_path))
    raise SystemExit(0 if result['valid'] else 1)


@community.command('preview')
@click.argument('file_path', type=click.Path(exists=True))
@click.option('-n', 'n_samples', default=5, help='Number of sample records')
def community_preview(file_path, n_samples):
    """Preview an export file."""
    from nomade.community import preview_export
    from pathlib import Path
    preview_export(Path(file_path), n_samples=n_samples)



def main() -> None:
    """Entry point for CLI."""
    cli(obj={})


if __name__ == '__main__':
    main()


@cli.command('report-interactive')
@click.option('--server-id', default='local', help='Server identifier')
@click.option('--idle-hours', type=int, default=24, help='Hours to consider session stale')
@click.option('--memory-threshold', type=int, default=4096, help='Memory hog threshold (MB)')
@click.option('--max-idle', type=int, default=5, help='Max idle sessions per user before alert')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.option('--quiet', '-q', is_flag=True, help='Only show alerts')
def report_interactive(server_id, idle_hours, memory_threshold, max_idle, as_json, quiet):
    """Report on interactive sessions (RStudio/Jupyter).
    
    Monitors running sessions and identifies:
    - Users with many idle sessions
    - Sessions idle for extended periods (stale)
    - Sessions consuming excessive memory (memory hogs)
    
    Examples:
        nomade report-interactive              # Full report
        nomade report-interactive --json       # JSON output
        nomade report-interactive --quiet      # Only show alerts
    """
    import json as json_module
    
    try:
        from nomade.collectors.interactive import get_report, print_report
    except (ImportError, SyntaxError):
        click.echo("Error: Interactive collector requires Python 3.7+", err=True)
        raise SystemExit(1)
    
    data = get_report(
        server_id=server_id,
        idle_hours=idle_hours,
        memory_hog_mb=memory_threshold,
        max_idle=max_idle
    )
    
    if as_json:
        click.echo(json_module.dumps(data, indent=2))
        return
    
    if quiet:
        alerts = data.get('alerts', {})
        has_alerts = False
        
        if alerts.get('idle_session_hogs'):
            has_alerts = True
            click.echo(f"[!] Users with >{max_idle} idle sessions:")
            for u in alerts['idle_session_hogs']:
                click.echo(f"    {u['user']}: {u['idle']} idle ({u['rstudio']} RStudio, {u['jupyter']} Jupyter), {u['memory_mb']:.0f} MB")
        
        if alerts.get('stale_sessions'):
            has_alerts = True
            click.echo(f"\n[!] Stale sessions (idle >{idle_hours}h): {len(alerts['stale_sessions'])}")
            for s in alerts['stale_sessions'][:10]:
                click.echo(f"    {s['user']}: {s['session_type']}, {s['age_hours']:.0f}h old, {s['mem_mb']:.0f} MB")
        
        if alerts.get('memory_hogs'):
            has_alerts = True
            click.echo(f"\n[!] Memory hogs (>{memory_threshold/1024:.0f}GB): {len(alerts['memory_hogs'])}")
            for s in alerts['memory_hogs'][:10]:
                click.echo(f"    {s['user']}: {s['session_type']}, {s['mem_mb']/1024:.1f} GB")
        
        if not has_alerts:
            click.echo("No alerts - all sessions within thresholds")
        return
    
    print_report(data)
