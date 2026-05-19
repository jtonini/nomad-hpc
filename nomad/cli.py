# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NOMAD CLI

Command-line interface for NOMAD monitoring and analysis.

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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import click
import toml

from nomad.analysis.derivatives import (
    analyze_disk_trend,
)
from nomad.collectors.disk import DiskCollector
from nomad.collectors.gpu import GPUCollector
from nomad.collectors.groups import GroupCollector
from nomad.collectors.interactive import InteractiveCollector
from nomad.collectors.iostat import IOStatCollector
from nomad.collectors.job_metrics import JobMetricsCollector
from nomad.collectors.mpstat import MPStatCollector
from nomad.collectors.nfs import NFSCollector
from nomad.collectors.node_state import NodeStateCollector
from nomad.collectors.per_user import PerUserCollector
from nomad.collectors.slurm import SlurmCollector
from nomad.collectors.vmstat import VMStatCollector
from nomad.collectors.workstation import WorkstationCollector
from nomad.issue.cli_commands import issue as issue_group

# Cloud collectors (optional — only available when provider SDKs are installed)
try:
    from nomad.collectors.cloud.aws import AWSCollector
    HAS_AWS_COLLECTOR = True
except ImportError:
    HAS_AWS_COLLECTOR = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('nomad')


def load_config(config_path: Path) -> dict[str, Any]:
    """Load TOML configuration file."""
    if not config_path.exists():
        raise click.ClickException(f"Config file not found: {config_path}")

    with open(config_path) as f:
        return toml.load(f)


def resolve_config_path() -> str:
    """Find config file: user path first, then system path."""
    user_config = Path.home() / '.config' / 'nomad' / 'nomad.toml'
    system_config = Path('/etc/nomad/nomad.toml')
    if user_config.exists():
        return str(user_config)
    if system_config.exists():
        return str(system_config)
    return str(user_config)  # Default to user path even if missing


def get_db_path(config: dict[str, Any]) -> Path:
    """Get database path from config.

    Resolution:
      1. [database].path — if absolute, use as-is; if relative, join with data_dir
      2. Fall back to data_dir / nomad.db
    """
    default_data = str(Path.home() / '.local' / 'share' / 'nomad')
    data_dir = Path(config.get('general', {}).get('data_dir', default_data))

    db_path_str = config.get('database', {}).get('path', '')
    if db_path_str:
        db_path = Path(db_path_str)
        if db_path.is_absolute():
            return db_path
        return data_dir / db_path

    return data_dir / 'nomad.db'


def _get_version():
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version('nomad-hpc')
    except Exception:
        return 'dev'


@click.group()
@click.version_option(version=_get_version(), prog_name='nomad')
@click.option('-c', '--config', 'config_path',
              type=click.Path(),
              default=None,
              help='Path to config file')
@click.option('-v', '--verbose', is_flag=True, help='Enable debug logging')
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    """NØMAD - NØde Monitoring And Diagnostics
    
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

    # Ensure database schema exists
    from nomad.db import ensure_database
    ensure_database(db_path)

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
                from nomad.config import resolve_cluster_name
                node_state_config['cluster_name'] = resolve_cluster_name(config)
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

    # Interactive session collector -- check [interactive] and [collectors.interactive]
    interactive_config = config.get("interactive", {})
    if not interactive_config:
        interactive_config = config.get("collectors", {}).get("interactive", {})
    if not collector or "interactive" in collector:
        if interactive_config.get("enabled", False):
            collectors.append(InteractiveCollector(interactive_config, db_path))

    # Workstation collector (SSH-based remote collection)
    ws_config = config.get('collectors', {}).get('workstation', {})
    if not collector or 'workstation' in collector:
        if ws_config.get('enabled', False):
            collectors.append(WorkstationCollector(ws_config, db_path))

    # Per-user process tracking collector (head-node misuse detection — Idea 18)
    # Disabled by default. Enable per-host on head nodes only:
    #   [collectors.per_user]
    #   enabled = true
    #   role = "headnode"
    per_user_config = config.get('collectors', {}).get('per_user', {})
    if not collector or 'per_user' in collector:
        if per_user_config.get('enabled', False):
            collectors.append(PerUserCollector(per_user_config, db_path))


    # Cloud collectors
    cloud_config = config.get('collectors', {}).get('cloud', {})
    if not collector or 'aws' in collector or 'cloud' in collector:
        aws_config = cloud_config.get('aws', {})
        if HAS_AWS_COLLECTOR and aws_config.get('enabled', False):
            collectors.append(AWSCollector(aws_config, db_path=str(db_path)))

    if not collectors:
        raise click.ClickException("No collectors enabled")

    # Report which collectors are running and which were skipped
    all_collector_names = [
        "disk", "slurm", "job_metrics", "iostat", "mpstat",
        "vmstat", "node_state", "gpu", "nfs", "groups", "interactive",
        "workstation"
    ]
    running_names = [c.name for c in collectors]
    skipped_names = [
        n for n in all_collector_names
        if n not in running_names
        and config.get("collectors", {}).get(n, {}).get("enabled", True) is False
    ]
    click.echo(f"Running collectors: {running_names}")
    if skipped_names:
        click.echo(f"Disabled collectors: {skipped_names}")

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
        FROM storage_state
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
    click.echo(click.style("═══ NØMAD Status ═══", bold=True))
    click.echo()

    # Filesystem status
    click.echo(click.style("Filesystems:", bold=True))
    try:
        fs_rows = conn.execute(
            """
            SELECT hostname,
                   round(used_bytes/1e9, 2) as used_gb,
                   round(total_bytes/1e9, 2) as total_gb,
                   round(usage_percent, 1) as pct,
                   timestamp
            FROM storage_state f1
            WHERE timestamp = (
                SELECT MAX(timestamp) FROM storage_state f2 WHERE f2.hostname = f1.hostname
            )
            ORDER BY hostname
            """
        ).fetchall()

        for row in fs_rows:
            pct = row['pct']
            color = 'green' if pct < 70 else 'yellow' if pct < 85 else 'red'
            bar_len = int(pct / 5)
            bar = '█' * bar_len + '░' * (20 - bar_len)
            click.echo(f"  {row['hostname']:<20} [{click.style(bar, fg=color)}] {click.style(f'{pct}%', fg=color)} ({row['used_gb']}/{row['total_gb']} GB)")
    except Exception:
        click.echo("  No filesystem data available")

    # Queue status
    click.echo(click.style("Queue:", bold=True))
    try:
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
    except Exception:
        click.echo("  No queue data available")

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
            click.echo("  No iostat data (run: nomad collect -C iostat --once)")
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
            click.echo("  No mpstat data (run: nomad collect -C mpstat --once)")
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
    try:
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
    except Exception:
            click.echo(" No collection data available")
    click.echo()

    # Cloud Resources
    try:
        cloud_count = conn.execute(
            "SELECT COUNT(DISTINCT node_name) as n FROM cloud_metrics WHERE node_name NOT LIKE 'EC2/%'"
        ).fetchone()
        if cloud_count and cloud_count['n'] > 0:
            click.echo(click.style("Cloud Resources:", bold=True))
            cloud_instances = conn.execute(
                "SELECT DISTINCT node_name, instance_type, availability_zone "
                "FROM cloud_metrics WHERE node_name NOT LIKE 'EC2/%' AND instance_type IS NOT NULL"
            ).fetchall()
            for inst in cloud_instances:
                cpu_row = conn.execute(
                    "SELECT AVG(value) as avg_cpu FROM cloud_metrics "
                    "WHERE node_name = ? AND metric_name = 'cpu_util' "
                    "AND timestamp > datetime('now', '-7 days')",
                    (inst['node_name'],)
                ).fetchone()
                mem_row = conn.execute(
                    "SELECT AVG(value) as avg_mem FROM cloud_metrics "
                    "WHERE node_name = ? AND metric_name = 'mem_util' "
                    "AND timestamp > datetime('now', '-7 days')",
                    (inst['node_name'],)
                ).fetchone()
                cost_row = conn.execute(
                    "SELECT value FROM cloud_metrics "
                    "WHERE node_name LIKE ? AND metric_name = 'daily_cost_usd' "
                    "ORDER BY timestamp DESC LIMIT 1",
                    ('EC2/' + inst['node_name'],)
                ).fetchone()
                cpu_val = cpu_row['avg_cpu'] if cpu_row and cpu_row['avg_cpu'] else 0
                mem_val = mem_row['avg_mem'] if mem_row and mem_row['avg_mem'] else 0
                cost_val = cost_row['value'] if cost_row else 0
                cpu_color = 'green' if cpu_val < 50 else 'yellow' if cpu_val < 80 else 'red'
                mem_color = 'green' if mem_val < 50 else 'yellow' if mem_val < 80 else 'red'
                cpu_bar_len = int(cpu_val / 5)
                cpu_bar = chr(9608) * cpu_bar_len + chr(9617) * (20 - cpu_bar_len)
                click.echo(
                    "  {:<18} {:<14} [{}] CPU: {}  Mem: {}  ${:.2f}/day".format(
                        inst['node_name'], inst['instance_type'],
                        click.style(cpu_bar, fg=cpu_color),
                        click.style(f"{cpu_val:.0f}%", fg=cpu_color),
                        click.style(f"{mem_val:.0f}%", fg=mem_color),
                        cost_val))
            total_cost = conn.execute(
                "SELECT SUM(value) as total FROM cloud_metrics "
                "WHERE metric_name = 'daily_cost_usd' AND timestamp > datetime('now', '-7 days')"
            ).fetchone()
            if total_cost and total_cost['total']:
                click.echo("  {:<34} {}".format('Total 7-day cost:',
                    click.style("${:.2f}".format(total_cost['total']), fg='yellow', bold=True)))
            click.echo()
    except sqlite3.OperationalError:
        pass

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

    # Check if alerts table exists
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
    ).fetchone()
    if not table_check:
        click.echo()
        click.echo("  No alerts table found. Use NØMAD Console for real-time alerts.")
        click.echo()
        conn.close()
        return

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

    # Enrich alerts with insight narratives
    _stype_narr = {}
    _stype_rec = {}
    try:
        from nomad.insights import InsightEngine
        _eng = InsightEngine(db_path, hours=168)
        for _s, _n in _eng.narratives:
            _k = _s.signal_type.value
            if _k not in _stype_narr:
                _stype_narr[_k] = _n
        for _ins in _eng.insights:
            if _ins.recommendation:
                for _s in _ins.source_signals:
                    _k = _s.signal_type.value
                    if _k not in _stype_rec:
                        _stype_rec[_k] = _ins.recommendation
    except Exception:
        pass
    _alert_src = {"disk": "disk", "memory": "memory", "gpu": "gpu", "job": "jobs", "slurm": "jobs", "network": "network", "cloud": "cloud"}
    for row in rows:
        color = severity_colors.get(row['severity'], 'white')
        resolved = '✓' if row['resolved'] else '○'

        click.echo(f"  {resolved} [{click.style(row['severity'].upper(), fg=color)}] {row['timestamp']}")
        click.echo(f"    {row['message']}")
        if row['source']:
            click.echo(f"    Source: {row['source']}")
        _src = row["source"] if "source" in row.keys() else None
        _st = _alert_src.get(_src, _src) if _src else None
        _narr = _stype_narr.get(_st) if _st else None
        _rec = _stype_rec.get(_st) if _st else None
        if _narr:
            click.echo(click.style(f"    Insight: {_narr}", dim=True))
        if _rec:
            click.echo(click.style(f"    >> {_rec}", dim=True))
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
    from nomad.monitors.job_monitor import JobMonitor

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
    from nomad.analysis.similarity import SimilarityAnalyzer

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
    click.echo(click.style("NØMAD System Check", bold=True))
    click.echo("═" * 40)
    click.echo()

    errors = 0
    warnings = 0

    # Python check
    click.echo(click.style("Python:", bold=True))
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 10):
        click.echo(f"  {click.style('✓', fg='green')} Version {py_version} (requires >=3.10)")
    else:
        click.echo(f"  {click.style('✗', fg='red')} Version {py_version} (requires >=3.10)")
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
        click.echo(f"  {click.style('→', fg='yellow')} Install with: pip install nomad[ml]")

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
            click.echo("    → Enable AccountingStorageType in slurm.conf")
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
                click.echo("    → Add: JobAcctGatherType=jobacct_gather/linux")
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
        click.echo("    → apt install sysstat  OR  yum install sysstat")
        warnings += 1

    if shutil.which('mpstat'):
        click.echo(f"  {click.style('✓', fg='green')} mpstat available")
    else:
        click.echo(f"  {click.style('⚠', fg='yellow')} mpstat not found (install sysstat package)")
        click.echo("    → apt install sysstat  OR  yum install sysstat")
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
        click.echo("    → Run: nomad collect --once")
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
        click.echo("    → Run: nomad init")
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
    click.echo("NØMAD v0.2.0")
    click.echo("NØde Monitoring And Diagnostics")


@cli.command()
@click.option('--host', default='localhost', help='Host to bind to (use 0.0.0.0 for all interfaces)')
@click.option('--port', '-p', type=int, default=8050, help='Port to listen on')
@click.option('--db', '-d', type=click.Path(), help='Path to database file')
@click.pass_context
def dashboard(ctx, host, port, db):
    """Start the interactive web dashboard.
    
    The dashboard provides a 3D visualization of job networks with two view modes:
    
    - Raw Axes: Jobs positioned by nfs_write, local_write, io_wait
    - PCA View: Jobs positioned by principal components (patterns emerge from data)
    
    Remote access via SSH tunnel:
        ssh -L 8050:localhost:8050 badenpowell
        Then open http://localhost:8050 in your browser
    
    Examples:
        nomad dashboard                      # Start with demo data
        nomad dashboard --port 9000          # Custom port
        nomad dashboard --db /path/to.db   # Use database
    """
    from nomad.viz.server import serve_dashboard

    # Try to find data source
    data_source = db
    if not data_source:
        config = ctx.obj.get('config', {})
        # Try database first (skip empty/uninitialized files)
        db_path = get_db_path(config)
        if db_path.exists() and db_path.stat().st_size > 0:
            data_source = str(db_path)
        else:
            # Try demo database
            demo_db = Path.home() / "nomad_demo.db"
            if demo_db.exists() and demo_db.stat().st_size > 0:
                data_source = str(demo_db)
            else:
                # Try simulation metrics
                metrics_paths = [
                    Path('/tmp/nomad-metrics.log'),
                    Path.home() / 'nomad-metrics.log',
                ]
                for mp in metrics_paths:
                    if mp.exists():
                        data_source = str(mp)
                        break

    click.echo(click.style("===========================================", fg='cyan'))
    click.echo(click.style("           ", fg='cyan') +
               click.style("NOMAD Dashboard", fg='white', bold=True))
    click.echo(click.style("===========================================", fg='cyan'))
    click.echo()

    serve_dashboard(host, port, db_path=data_source)


@cli.command()
@click.option("--db", type=click.Path(), help="Database path")
@click.option("-v", "--verbose", is_flag=True, help="Show feature details")
@click.pass_context
def readiness(ctx, db, verbose):
    """Check data readiness for ML training.
    
    Analyzes your job data and reports:
    - Sample size adequacy (minimum, recommended, optimal thresholds)
    - Class balance (success/failure ratio)
    - Feature coverage and variance
    - Data recency
    - Estimated model accuracy at current sample size
    
    Examples:
        nomad readiness                # Check readiness
        nomad readiness -v             # Verbose with feature details
        nomad readiness --db data.db   # Specify database
    """
    from nomad.ml.estimator import check_readiness

    db_path = db
    if not db_path:
        config = ctx.obj.get("config", {})
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        click.echo("Run 'nomad collect' first to gather job data.")
        return

    report = check_readiness(db_path, verbose=verbose)
    click.echo(report)



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
        nomad train                    # Train with default settings
        nomad train --epochs 50        # Fewer epochs (faster)
        nomad train --db data.db       # Specify database
    """
    from nomad.ml import is_torch_available, train_and_save_ensemble

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
    click.echo(click.style("  NOMAD ML Training", fg="white", bold=True))
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
    Run 'nomad train' first to generate predictions.
    
    Examples:
        nomad predict                  # Show top 20 high-risk jobs
        nomad predict --top 50         # Show top 50
    """
    from nomad.ml import load_predictions_from_db

    db_path = db
    if not db_path:
        config = ctx.obj.get("config", {})
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(click.style(f"Database not found: {db_path}", fg="red"))
        return

    predictions = load_predictions_from_db(db_path)

    if not predictions:
        click.echo(click.style("No predictions found. Run 'nomad train' first.", fg="yellow"))
        return

    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo(click.style("  NOMAD ML Predictions", fg="white", bold=True))
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
        nomad report                   # Print to stdout
        nomad report -o report.txt     # Save to file
    """
    import sqlite3

    from nomad.ml import FAILURE_NAMES, load_predictions_from_db

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
    lines.append("  NOMAD Analysis Report")
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
        lines.append("  ML PREDICTIONS: Not available (run 'nomad train')")

    lines.append("")
    lines.append("=" * 60)

    report_text = "\n".join(lines)

    if output:
        Path(output).write_text(report_text)
        click.echo(f"Report saved to {output}")
    else:
        click.echo(report_text)



@cli.command('test-alerts')
@click.option('--email', is_flag=True, help='Test only the email backend')
@click.option('--slack', is_flag=True, help='Test only the Slack backend')
@click.option('--webhook', is_flag=True, help='Test only the webhook backend')
@click.pass_context
def test_alerts(ctx, email, slack, webhook):
    """Test configured alert notification backends end-to-end.

    Runs a connection check on each configured backend, then dispatches
    a synthetic warning-severity alert through the full dispatch path
    (filtering, cooldown, send). Use this to confirm that real alerts
    will reach their destinations.

    Examples:
        nomad test-alerts                  # Test all configured backends
        nomad test-alerts --email          # Test only email
        nomad test-alerts --email --slack  # Test email and Slack
    """
    from nomad.alerts import AlertDispatcher

    config = ctx.obj.get('config', {})
    dispatcher = AlertDispatcher(config)

    if not dispatcher.backends:
        click.echo(click.style("No alert backends configured.", fg="yellow"))
        click.echo("Add configuration to nomad.toml. For example:")
        click.echo("")
        click.echo('[alerts.email]')
        click.echo('enabled = true')
        click.echo('smtp_server = "smtp.your-institution.edu"   # or "127.0.0.1" for local sendmail')
        click.echo('smtp_port = 587')
        click.echo('use_tls = true')
        click.echo('from_address = "nomad@your-institution.edu"')
        click.echo('recipients = ["admin@your-institution.edu"]')
        click.echo("")
        click.echo('[alerts.slack]')
        click.echo('enabled = true')
        click.echo('webhook_url = "https://hooks.slack.com/services/..."')
        click.echo("")
        click.echo('[alerts.webhook]')
        click.echo('enabled = true')
        click.echo('url = "https://your-monitoring.example.com/webhook"')
        return

    flag_to_class = {
        'EmailBackend':   email,
        'SlackBackend':   slack,
        'WebhookBackend': webhook,
    }
    selected_classes = {cls for cls, flag in flag_to_class.items() if flag}

    if selected_classes:
        filtered = [b for b in dispatcher.backends
                    if b.__class__.__name__ in selected_classes]
        if not filtered:
            missing = ", ".join(sorted(selected_classes))
            click.echo(click.style(
                f"Requested backend(s) not configured: {missing}",
                fg="yellow",
            ))
            configured = ", ".join(
                b.__class__.__name__ for b in dispatcher.backends
            )
            click.echo(f"Configured backends: {configured}")
            return
        dispatcher.backends = filtered

    click.echo(f"Testing {len(dispatcher.backends)} backend(s)...")

    results = dispatcher.test_backends()
    for backend, success in results.items():
        if success:
            click.echo(click.style(f"  {backend}: OK", fg="green"))
        else:
            click.echo(click.style(f"  {backend}: FAILED", fg="red"))

    click.echo("")
    click.echo("Sending test alert (severity=warning)...")
    send_results = dispatcher.dispatch({
        'severity': 'warning',
        'source': 'test',
        'message': ('NOMAD test alert (nomad test-alerts). '
                    'If you received this, real alerts will reach you.'),
        'host': 'cli-test',
    })

    if not send_results:
        click.echo(click.style(
            "  No backends dispatched. Possible causes:", fg="yellow",
        ))
        click.echo("    - Alert filtered by [alerts] min_severity")
        click.echo("    - Alert in cooldown window (cooldown_minutes)")
        click.echo("    - No backends matched the selected flags")
        return

    for backend, success in send_results.items():
        if success:
            click.echo(click.style(f"  {backend}: Sent", fg="green"))
        else:
            click.echo(click.style(
                f"  {backend}: Failed (see log for details)", fg="red",
            ))



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
      nomad learn --status           Show training status
      nomad learn --force            Train now
      nomad learn --strategy count   Train after 100 new jobs
      nomad learn --daemon           Run continuously
    """
    from nomad.ml import is_torch_available
    from nomad.ml.continuous import ContinuousLearner

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
        click.echo(click.style("  NOMAD Learning Status", fg="white", bold=True))
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
@click.option('--dry-run', is_flag=True, help='Show config without writing')
@click.option('--show', is_flag=True, help='Display current config and exit')
@click.pass_context
def init(ctx, system, force, quick, no_systemd, no_prolog, dry_run, show):
    """Initialize NOMAD with an interactive setup wizard.

    \b
    The wizard walks you through configuring NØMAD for your
    HPC cluster(s). It will ask about your clusters, partitions,
    storage, and monitoring preferences.

    \b
    If the wizard is interrupted (Ctrl+C), your progress is saved
    automatically. Run 'nomad init' again to pick up where you
    left off.

    \b
    User install (default):
      ~/.config/nomad/nomad.toml   Configuration
      ~/.local/share/nomad/         Data directory

    \b
    System install (--system, requires root):
      /etc/nomad/nomad.toml        Configuration
      /var/lib/nomad/               Data directory

    \b
    Examples:
      nomad init                    Interactive wizard
      nomad init --quick            Auto-detect everything
      nomad init --force            Overwrite existing config
      nomad init --show             Display current config
      nomad init --dry-run          Preview without writing
      sudo nomad init --system      System-wide installation
    """
    import os
    import subprocess as sp

    # ── Determine paths ──────────────────────────────────────────────
    if system:
        config_dir = Path('/etc/nomad')
        data_dir = Path('/var/lib/nomad')
        log_dir = Path('/var/log/nomad')
    else:
        config_dir = Path.home() / '.config' / 'nomad'
        data_dir = Path.home() / '.local' / 'share' / 'nomad'
        log_dir = data_dir / 'logs'

    config_file = config_dir / 'nomad.toml'

    # ── Show current config and exit ────────────────────────────────
    if show:
        if config_file.exists():
            click.echo()
            click.echo(click.style(
                f"  Config: {config_file}", fg="cyan", bold=True))
            click.echo(click.style(
                "  ══════════════════════════════════════", fg="cyan"))
            click.echo()
            click.echo(config_file.read_text())
        else:
            click.echo()
            click.echo(click.style(
                f"  No config found at {config_file}", fg="yellow"))
            click.echo("  Run 'nomad init' to create one.")
            click.echo()
        return

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
            "\n  Permission denied. Use: sudo nomad init --system",
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
            if not ssh_user:
                ssh_user = os.getenv("USER", "root")
            ssh_cmd = ["ssh", "-o", "ConnectTimeout=5",
                       "-o", "BatchMode=yes",
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
            return [line.strip().rstrip('*')
                    for line in out.split('\n') if line.strip()]
        return []

    def detect_nodes_per_partition(partition, host=None, ssh_user=None,
                                   ssh_key=None):
        out = run_cmd(f"sinfo -h -p {partition} -o %n",
                      host, ssh_user, ssh_key)
        if out:
            return sorted({line.strip() for line in out.split('\n') if line.strip()})
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
            found = [line.strip() for line in out.split('\n')[1:]
                     if line.strip() in hpc_paths]
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
                for i, p in enumerate(detected, 1):
                    click.echo(f"    {i}) {p}")
                click.echo()
                use_all = click.confirm(
                    "  Monitor all of these partitions?",
                    default=True)
                if use_all:
                    chosen = detected
                else:
                    click.echo()
                    click.echo(
                        "  Type the numbers of the partitions")
                    click.echo(
                        "  you want, separated by commas.")
                    click.echo(
                        f"  (e.g., 1,2 for"
                        f" {detected[0]} and"
                        f" {detected[1] if len(detected) > 1 else detected[0]})")
                    click.echo()
                    sel_str = click.prompt(
                        "  Partitions",
                        default=','.join(
                            str(i) for i in
                            range(1, len(detected) + 1)))
                    chosen = []
                    for part in sel_str.split(','):
                        part = part.strip()
                        # Accept numbers or names
                        try:
                            idx = int(part) - 1
                            if 0 <= idx < len(detected):
                                chosen.append(detected[idx])
                        except ValueError:
                            if part in detected:
                                chosen.append(part)
                    if not chosen:
                        click.echo(click.style(
                            "  No valid selection,"
                            " using all partitions.",
                            fg="yellow"))
                        chosen = detected
            else:
                click.echo(click.style(
                    "could not auto-detect", fg="yellow"))
                click.echo()
                click.echo(
                    "  NØMAD could not detect partitions automatically.")
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
                    click.echo("  separated by commas:")
                    click.echo("  (e.g., node01, node02, node03)")
                    nodes_str = click.prompt(
                        f"  Nodes for {p}", default="")
                    nodes = [n.strip() for n in nodes_str.split(',')
                             if n.strip()]
                    part_gpu = []

                cluster["partitions"][p] = {
                    "nodes": nodes,
                    "gpu_nodes": part_gpu,
                }
            # Ask for node_ssh_user (e.g., zeus on headnode but root on nodes)
            click.echo()
            click.echo(
                "  Some clusters require SSHing into compute nodes")
            click.echo(
                "  for GPU monitoring. If your compute nodes need a")
            click.echo(
                "  different SSH user (e.g., 'root'), enter it here.")
            click.echo("  Leave blank to use your current user.")
            click.echo()
            import getpass
            node_user = click.prompt(
                "  Compute node SSH user (optional)",
                default="",
                show_default=False)
            if node_user.strip():
                cluster["node_ssh_user"] = node_user.strip()
        else:
            # Workstation group
            click.echo(
                "  For workstation groups, you organize machines by")
            click.echo(
                "  department or lab. Each group becomes a section")
            click.echo("  in the dashboard.")
            click.echo()
            # Ask for workstation SSH user (zeus may SSH as root, etc.)
            import getpass
            current_user = getpass.getuser()
            click.echo(
                "  The collector will SSH to each workstation to gather")
            click.echo(
                "  metrics. What user should it connect as?")
            click.echo()
            ws_ssh_user = click.prompt(
                "  Workstation SSH user", default=current_user)
            cluster["ws_ssh_user"] = ws_ssh_user
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
                click.echo("  separated by commas:")
                click.echo("  (e.g., bio-ws01, bio-ws02, bio-ws03)")
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
        # For remote workstation groups, probe first node
        # For local HPC headnode, run df locally (host=None)
        probe_host = host
        if (not probe_host
                and cluster.get("mode") == "remote"
                and cluster.get("type") == "workstations"
                and cluster.get("partitions")):
            first_part = next(iter(cluster["partitions"].values()), {})
            first_nodes = first_part.get("nodes", [])
            if first_nodes:
                probe_host = first_nodes[0]

        click.echo()
        click.echo(click.style("  Storage", fg="green", bold=True))
        click.echo()
        click.echo(
            "  Which filesystems should NØMAD monitor for disk")
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
        # For remote workstation groups, probe first node
        # For local HPC headnode, run commands locally
        probe_host = host
        if (not probe_host
                and cluster.get("mode") == "remote"
                and cluster.get("type") == "workstations"
                and cluster.get("partitions")):
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
    click.echo("\033[2J\033[H", nl=False)  # Clear screen
    click.echo()
    click.echo(click.style(
        "  ◈ NØMAD Setup Wizard", fg="cyan", bold=True))
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo()

    # ── Check for previous incomplete setup ──────────────────────────
    saved = load_state()
    resume = False

    if saved and not quick and not force:
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
            "  This wizard will help you configure NØMAD for your")
        click.echo(
            "  HPC environment. Press Enter to accept the default")
        click.echo("  value shown in [brackets].")
        click.echo()
        click.echo(click.style(
            "  Tip:", fg="green", bold=True) +
            " After this wizard, run 'nomad syscheck' to verify")
        click.echo(
            "  your environment is ready for data collection.")
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
                "  ━━ Step 1: Connection Mode ━━",
                fg="cyan", bold=True))
            click.echo()
            click.echo("  Where is NØMAD running?")
            click.echo()
            click.echo("    1) On the cluster headnode")
            click.echo(
                "       NØMAD has direct access to SLURM commands")
            click.echo("       like sinfo, squeue, and sacct.")
            click.echo()
            click.echo(
                "    2) On a separate machine"
                " (laptop, desktop, etc.)")
            click.echo(
                "       NØMAD will connect to your cluster(s)"
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
                    "  NØMAD can connect to your cluster(s) without")
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
                            "  Would you like NØMAD to create one"
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
                        "  NØMAD can do this for you now.")
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

            # Step 2: Deployment strategy
            click.echo(click.style(
                "  ━━ Step 2: Deployment Strategy ━━",
                fg="cyan", bold=True))
            click.echo()
            click.echo(
                "  NØMAD supports two deployment strategies:")
            click.echo()
            click.echo(click.style(
                "  Strategy A — One NØMAD per machine + sync",
                fg="green", bold=True))
            click.echo(
                "    Install NØMAD on each machine independently.")
            click.echo(
                "    Each instance monitors its own environment.")
            click.echo(
                "    Use 'nomad sync' on a central host to merge")
            click.echo(
                "    all databases into a unified dashboard.")
            click.echo()
            click.echo(
                "    Best when: machines have different roles")
            click.echo(
                "    (HPC + interactive server + workstations),")
            click.echo(
                "    or are on different networks.")
            click.echo()
            click.echo(click.style(
                "  Strategy B — One NØMAD monitors everything",
                fg="green", bold=True))
            click.echo(
                "    A single instance monitors multiple systems")
            click.echo(
                "    via SSH from one machine.")
            click.echo()
            click.echo(
                "    Best when: everything is reachable from one")
            click.echo(
                "    machine and you want a simpler setup.")
            click.echo()
            click.echo(click.style(
                "  Tip: ", fg="yellow") +
                "Strategy A is recommended for most sites.")
            click.echo(
                "  You can always start with B and split later.")
            click.echo()
            click.echo(
                "  How many HPC clusters or workstation groups")
            click.echo(
                "  will THIS NØMAD instance monitor?")
            click.echo()
            click.echo(
                "  - Strategy A: enter 1 (this machine only)")
            click.echo(
                "  - Strategy B: enter the total number of systems")
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
                        "  (NØMAD will use SSH to reach"
                        " this cluster)")
                    click.echo()
                    host = click.prompt(
                        "  Headnode hostname"
                        " (e.g., cluster.university.edu)")
                else:
                    # Workstations: no headnode, NØMAD connects
                    # directly to each machine
                    click.echo("  SSH connection details:")
                    click.echo(
                        "  For workstation groups, NØMAD connects")
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
                        gpu_st = click.style(
                            "on" if cluster.get("has_gpu")
                            else "off",
                            fg="green" if cluster.get("has_gpu")
                            else "red")
                        nfs_st = click.style(
                            "on" if cluster.get("has_nfs")
                            else "off",
                            fg="green" if cluster.get("has_nfs")
                            else "red")
                        int_st = click.style(
                            "on" if cluster.get("has_interactive")
                            else "off",
                            fg="green"
                            if cluster.get("has_interactive")
                            else "red")
                        click.echo()
                        click.echo(
                            f"    1) GPU monitoring:"
                            f"         {gpu_st}")
                        click.echo(
                            f"    2) NFS monitoring:"
                            f"         {nfs_st}")
                        click.echo(
                            f"    3) Interactive sessions:"
                            f"  {int_st}")
                        click.echo(
                            "    4) Toggle all")
                        click.echo()
                        feat_choice = click.prompt(
                            "  Select",
                            type=click.IntRange(1, 4),
                            default=4)
                        if feat_choice == 1:
                            cluster["has_gpu"] = (
                                not cluster.get(
                                    "has_gpu", False))
                            st = ("enabled"
                                  if cluster["has_gpu"]
                                  else "disabled")
                            click.echo(
                                f"  GPU monitoring {st}.")
                        elif feat_choice == 2:
                            cluster["has_nfs"] = (
                                not cluster.get(
                                    "has_nfs", False))
                            st = ("enabled"
                                  if cluster["has_nfs"]
                                  else "disabled")
                            click.echo(
                                f"  NFS monitoring {st}.")
                        elif feat_choice == 3:
                            cluster["has_interactive"] = (
                                not cluster.get(
                                    "has_interactive",
                                    False))
                            st = ("enabled"
                                  if cluster[
                                      "has_interactive"]
                                  else "disabled")
                            click.echo(
                                "  Interactive session"
                                f" monitoring {st}.")
                        elif feat_choice == 4:
                            cluster["has_gpu"] = (
                                click.confirm(
                                    "  Enable GPU?",
                                    default=cluster.get(
                                        "has_gpu",
                                        False)))
                            cluster["has_nfs"] = (
                                click.confirm(
                                    "  Enable NFS?",
                                    default=cluster.get(
                                        "has_nfs",
                                        False)))
                            cluster["has_interactive"] = (
                                click.confirm(
                                    "  Enable interactive?",
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
            "  ━━ Step 3: Alerts ━━", fg="cyan", bold=True))
        click.echo()
        click.echo(
            "  NØMAD can send you email alerts when something"
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
            "  ━━ Step 4: Dashboard ━━", fg="cyan", bold=True))
        click.echo()
        click.echo(
            "  The NØMAD dashboard is a web page you open in"
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
    lines.append("# NØMAD Configuration File")
    lines.append("# Generated by: nomad init")
    lines.append(
        f"# Date:"
        f" {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    lines.append("[general]")
    lines.append('log_level = "info"')
    lines.append(f'data_dir = "{data_dir}"')
    lines.append("")

    # Name database after primary cluster
    db_name = clusters[0]["name"].lower().replace(" ", "_") if clusters else "nomad"
    lines.append("[database]")
    lines.append(f'path = "{db_name}.db"')
    lines.append("")

    # Collector feature flags
    any_gpu = any(c.get("has_gpu") for c in clusters)
    any_nfs = any(c.get("has_nfs") for c in clusters)
    any_interactive = any(
        c.get("has_interactive") for c in clusters)

    lines.append("[collectors]")
    lines.append("# Collection interval in seconds")
    lines.append("interval = 60")
    lines.append("")

    # Disk collector
    all_fs = set()
    for c in clusters:
        all_fs.update(c.get("filesystems", []))
    fs_items = ', '.join(f'"{f}"' for f in sorted(all_fs))
    lines.append("[collectors.disk]")
    lines.append("enabled = true")
    lines.append(f"filesystems = [{fs_items}]")
    lines.append("")

    # SLURM collector (only if HPC clusters exist)
    has_hpc = any(c.get("type", "hpc") == "hpc" for c in clusters)
    all_parts = set()
    for c in clusters:
        if c.get("type", "hpc") == "hpc":
            all_parts.update(
                c.get("partitions", {}).keys())
    lines.append("[collectors.slurm]")
    lines.append(f"enabled = {str(has_hpc).lower()}")
    if all_parts:
        parts_items = ', '.join(
            f'"{p}"' for p in sorted(all_parts))
        lines.append(f"partitions = [{parts_items}]")
    lines.append("")

    # Node state collector (requires SLURM)
    lines.append("[collectors.node_state]")
    lines.append(f"enabled = {str(has_hpc).lower()}")
    lines.append("")

    # System stat collectors
    lines.append("[collectors.iostat]")
    lines.append("enabled = true")
    lines.append("")
    lines.append("[collectors.mpstat]")
    lines.append("enabled = true")
    lines.append("")
    lines.append("[collectors.vmstat]")
    lines.append("enabled = true")
    lines.append("")
    lines.append("[collectors.job_metrics]")
    lines.append(f"enabled = {str(has_hpc).lower()}")
    lines.append("")
    lines.append("[collectors.groups]")
    lines.append(f"enabled = {str(has_hpc).lower()}")
    lines.append("")

    # GPU collector
    lines.append("[collectors.gpu]")
    lines.append(f"enabled = {str(any_gpu).lower()}")
    if any_gpu:
        # Use node_ssh_user from any HPC cluster that has it
        gpu_ssh_user = None
        all_gpu_nodes = []
        for c in clusters:
            if c.get("type") == "hpc":
                if c.get("node_ssh_user") and not gpu_ssh_user:
                    gpu_ssh_user = c["node_ssh_user"]
                for pdata in c.get("partitions", {}).values():
                    all_gpu_nodes.extend(pdata.get("gpu_nodes", []))
        if gpu_ssh_user:
            lines.append(f'ssh_user = "{gpu_ssh_user}"')
        if all_gpu_nodes:
            nodes_items = ', '.join(
                f'"{n}"' for n in sorted(set(all_gpu_nodes)))
            lines.append(f"gpu_nodes = [{nodes_items}]")
    lines.append("")

    # NFS collector
    lines.append("[collectors.nfs]")
    lines.append(f"enabled = {str(any_nfs).lower()}")
    if any_nfs:
        lines.append("mount_points = []")
    lines.append("")

    # Interactive collector
    lines.append("[collectors.interactive]")
    lines.append(f"enabled = {str(any_interactive).lower()}")
    lines.append("")

    # Workstation collector
    any_workstation = any(
        c.get("type") == "workstations" for c in clusters)
    if any_workstation:
        lines.append("[collectors.workstation]")
        lines.append("enabled = true")
        # Use the user-specified ws_ssh_user (or fallback to current user)
        ws_user = None
        for c in clusters:
            if c.get("type") == "workstations" and c.get("ws_ssh_user"):
                ws_user = c["ws_ssh_user"]
                break
        if not ws_user:
            import getpass
            ws_user = getpass.getuser()
        lines.append(f'ssh_user = "{ws_user}"')
        ws_list = []
        for c in clusters:
            if c.get("type") != "workstations":
                continue
            for dept, pdata in c.get("partitions", {}).items():
                for node in pdata.get("nodes", []):
                    ws_list.append(
                        f'  {{hostname = "{node}", '
                        f'department = "{dept}"}}')
        if ws_list:
            lines.append("workstations = [")
            for ws in ws_list:
                lines.append(ws + ",")
            lines.append("]")
        lines.append("")
    else:
        lines.append("[collectors.workstation]")
        lines.append("enabled = false")
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

        # SSH user for accessing compute nodes (e.g., root)
        if cluster.get("type") == "hpc":
            if cluster.get("node_ssh_user"):
                lines.append(
                    '# SSH user for compute nodes')
                lines.append(
                    f'node_ssh_user = "{cluster["node_ssh_user"]}"')
            else:
                lines.append(
                    '# SSH user for compute nodes'
                    ' (if different from current user)')
                lines.append('# node_ssh_user = "root"')

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

    if any_gpu:
        lines.append("[alerts.thresholds.gpu]")
        lines.append("memory_percent_warning = 90")
        lines.append("temperature_warning = 80")
        lines.append("temperature_critical = 90")
        lines.append("")

    if any_nfs:
        lines.append("[alerts.thresholds.nfs]")
        lines.append("retrans_percent_warning = 1.0")
        lines.append("retrans_percent_critical = 5.0")
        lines.append("avg_rtt_ms_warning = 50")
        lines.append("avg_rtt_ms_critical = 100")
        lines.append("")

    if any_interactive:
        lines.append("[alerts.thresholds.interactive]")
        lines.append("idle_sessions_warning = 50")
        lines.append("idle_sessions_critical = 100")
        lines.append("memory_gb_warning = 32")
        lines.append("memory_gb_critical = 64")
        lines.append("")

    # Interactive session monitoring config (top-level, read by collect)
    if any_interactive:
        lines.append("# ============================================")
        lines.append("# INTERACTIVE SESSION MONITORING")
        lines.append("# ============================================")
        lines.append("")
        lines.append("[interactive]")
        lines.append("enabled = true")
        lines.append("idle_session_hours = 24")
        lines.append("memory_hog_mb = 8192")
        lines.append("")

    if admin_email:
        lines.append("[alerts.email]")
        lines.append("enabled = true")
        lines.append(
            "# Update these with your SMTP server details:")
        lines.append('smtp_server = "smtp.example.com"')
        lines.append("smtp_port = 587")
        lines.append('from_address = "nomad@example.com"')
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

    # ============================================
    # HUB ROLE (optional — pulls databases from other sites)
    # ============================================
    click.echo()
    click.echo(click.style(
        "  ── Hub role ──", fg="cyan", bold=True))
    click.echo()
    click.echo(
        "  A hub machine pulls NOMAD databases from one or more other")
    click.echo(
        "  sites and merges them into a single combined database for")
    click.echo(
        "  cross-site dashboards and analytics.")
    click.echo()

    is_hub = click.confirm(
        "  Will this machine also pull from other sites?",
        default=False)

    if is_hub:
        click.echo()
        click.echo(
            "  For each remote site, you will need: a name, a hostname")
        click.echo(
            "  reachable via SSH, the SSH user, and the path to that")
        click.echo("  site's nomad.db.")
        click.echo()

        hub_sites = []
        site_idx = 1
        while True:
            click.echo(click.style(
                f"  Site {site_idx}", fg="cyan"))
            site_name = click.prompt(
                "    Name (e.g., arachne)", type=str).strip()
            site_host = click.prompt(
                "    Hostname or SSH alias",
                default=site_name, type=str).strip()
            site_user = click.prompt(
                "    SSH user", type=str).strip()
            site_db = click.prompt(
                "    Remote db_path",
                default="~/.local/share/nomad/nomad.db",
                type=str).strip()
            site_key = click.prompt(
                "    SSH key path (blank for default)",
                default="", show_default=False, type=str).strip()

            site_dict = {
                'name': site_name,
                'host': site_host,
                'ssh_user': site_user,
                'db_path': site_db,
            }
            if site_key:
                site_dict['ssh_key'] = site_key
            hub_sites.append(site_dict)

            click.echo()
            if not click.confirm(
                    "    Add another site?", default=True):
                break
            site_idx += 1
            click.echo()

        # Hub-level settings
        click.echo()
        hub_output_db = click.prompt(
            "  Output database path",
            default="~/.local/share/nomad/combined.db",
            type=str).strip()
        hub_interval = click.prompt(
            "  Sync interval (minutes)",
            default=10, type=int)

        # Optional SSH connectivity test
        click.echo()
        if click.confirm(
                "  Test SSH connectivity to each site now?",
                default=True):
            import subprocess as _sp
            click.echo()
            failures = []
            for s in hub_sites:
                user = s['ssh_user']
                host = s['host']
                key = s.get('ssh_key')
                cmd = ['ssh', '-o', 'BatchMode=yes',
                       '-o', 'ConnectTimeout=5',
                       '-o', 'StrictHostKeyChecking=accept-new']
                if key:
                    cmd += ['-i', key]
                cmd += [f'{user}@{host}', 'true']
                click.echo(f"    {s['name']}: ", nl=False)
                try:
                    r = _sp.run(cmd, capture_output=True,
                                text=True, timeout=10)
                    if r.returncode == 0:
                        click.echo(click.style("OK", fg="green"))
                    else:
                        err = (r.stderr.strip().splitlines()[0]
                               if r.stderr.strip()
                               else f"exit {r.returncode}")
                        click.echo(click.style(
                            f"FAILED — {err[:80]}", fg="red"))
                        failures.append(s['name'])
                except _sp.TimeoutExpired:
                    click.echo(click.style(
                        "TIMEOUT", fg="red"))
                    failures.append(s['name'])
                except Exception as _e:
                    click.echo(click.style(
                        f"ERROR — {_e}", fg="red"))
                    failures.append(s['name'])

            if failures:
                click.echo()
                click.echo(click.style(
                    f"  {len(failures)} site(s) failed: "
                    f"{', '.join(failures)}", fg="yellow"))
                click.echo(
                    "  Common fixes: copy your key with `ssh-copy-id`,")
                click.echo(
                    "  add the host to ~/.ssh/config, or update ssh_key.")
                click.echo(
                    "  You can re-run with `nomad hub test` after fixing.")
                if not click.confirm(
                        "  Continue saving config anyway?",
                        default=True):
                    click.echo(click.style(
                        "  Aborted. Config not written.", fg="red"))
                    return

        # Append [hub] sections to the config
        lines.append("# ============================================")
        lines.append("# HUB (cross-site database aggregation)")
        lines.append("# ============================================")
        lines.append("")
        lines.append("[hub]")
        lines.append("enabled = true")
        lines.append(f'output_db = "{hub_output_db}"')
        lines.append(f"sync_interval_minutes = {hub_interval}")
        lines.append("")
        for s in hub_sites:
            lines.append("[[hub.sites]]")
            lines.append(f'name = "{s["name"]}"')
            lines.append(f'host = "{s["host"]}"')
            lines.append(f'ssh_user = "{s["ssh_user"]}"')
            lines.append(f'db_path = "{s["db_path"]}"')
            if 'ssh_key' in s:
                lines.append(f'ssh_key = "{s["ssh_key"]}"')
            lines.append("")

        # Optional cron entry — install only if not in dry-run mode
        click.echo()
        install_cron = click.confirm(
            "  Add a cron entry to run `nomad sync` automatically?",
            default=True)
        if install_cron and not dry_run:
            import subprocess as _sp2
            try:
                _which = _sp2.run(['which', 'nomad'],
                                  capture_output=True,
                                  text=True, timeout=5)
                nomad_bin = (_which.stdout.strip()
                             if _which.returncode == 0
                             else 'nomad')
            except Exception:
                nomad_bin = 'nomad'

            cron_line = (
                f"*/{hub_interval} * * * * "
                f"{nomad_bin} sync >/dev/null 2>&1"
            )

            try:
                _ct = _sp2.run(['crontab', '-l'],
                               capture_output=True,
                               text=True, timeout=10)
                existing = _ct.stdout if _ct.returncode == 0 else ""
            except Exception:
                existing = ""

            if 'nomad sync' in existing:
                click.echo(click.style(
                    "  Crontab already has a `nomad sync` line — "
                    "skipping insert.", fg="yellow"))
                click.echo("  Suggested line:")
                click.echo(f"    {cron_line}")
            else:
                new_crontab = (
                    existing.rstrip() + chr(10) + cron_line + chr(10)
                    if existing.strip()
                    else cron_line + chr(10)
                )
                try:
                    _ins = _sp2.run(['crontab', '-'],
                                    input=new_crontab,
                                    capture_output=True,
                                    text=True, timeout=10)
                    if _ins.returncode == 0:
                        click.echo(click.style(
                            "  ✓ Cron entry installed.", fg="green"))
                        click.echo(f"    {cron_line}")
                    else:
                        click.echo(click.style(
                            "  Failed to install cron entry. "
                            "Add manually:", fg="yellow"))
                        click.echo(f"    {cron_line}")
                except Exception as _e:
                    click.echo(click.style(
                        f"  Could not run crontab: {_e}", fg="yellow"))
                    click.echo("  Add this line manually:")
                    click.echo(f"    {cron_line}")
        elif install_cron and dry_run:
            click.echo(click.style(
                "  (dry-run: cron entry not installed)", fg="yellow"))

    config_content = '\n'.join(lines)

    if dry_run:
        click.echo()
        click.echo(click.style(
            "  ── Preview (--dry-run) ──", fg="yellow", bold=True))
        click.echo()
        click.echo(config_content)
        click.echo()
        click.echo(click.style(
            f"  Would be written to: {config_file}", fg="yellow"))
        click.echo("  Run without --dry-run to save.")
        click.echo()
        return

    config_file.write_text(config_content)

    # Clean up wizard state file
    clear_state()

    # ── Summary ──────────────────────────────────────────────────────
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo(click.style(
        "  ✓ NØMAD configured!", fg="green", bold=True))
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
    click.echo("    1. Review your config (optional):")
    click.echo(f"         nano {config_file}")
    click.echo()
    click.echo("    2. Check that everything is ready:")
    click.echo("         nomad syscheck")
    click.echo()
    click.echo("    3. Start collecting data:")
    click.echo("         nomad collect")
    click.echo()
    click.echo(
        "    4. Open the dashboard in your browser:")
    click.echo("         nomad dashboard")
    click.echo()





def _pull_via_backup(user, host, remote_db, local_copy, ssh_key=None):
    """Pull a remote SQLite database using .backup for atomic snapshot.

    Returns True on success, False on failure.

    Tries SQLite .backup first (handles WAL correctly). Falls back to
    direct scp if sqlite3 is unavailable on the remote.

    Snapshot is created in /tmp on the remote, then scp'd locally,
    then deleted from the remote regardless of outcome.
    """
    import os
    import subprocess as sp
    import logging
    logger = logging.getLogger("nomad.sync")

    pid = os.getpid()
    remote_snap = f"/tmp/nomad_sync_{pid}.db"

    def _ssh(cmd, timeout=30):
        ssh_args = ["ssh", "-o", "ConnectTimeout=10",
                    "-o", "BatchMode=yes"]
        if ssh_key:
            ssh_args += ["-i", ssh_key]
        ssh_args += [f"{user}@{host}", cmd]
        return sp.run(ssh_args, capture_output=True,
                      text=True, timeout=timeout)

    def _scp(remote_path, local_path, timeout=120):
        scp_args = ["scp", "-o", "ConnectTimeout=10",
                    "-o", "BatchMode=yes"]
        if ssh_key:
            scp_args += ["-i", ssh_key]
        scp_args += [f"{user}@{host}:{remote_path}", str(local_path)]
        return sp.run(scp_args, capture_output=True,
                      text=True, timeout=timeout)

    # Try .backup first
    backup_used = False
    try:
        # Check sqlite3 binary exists on remote
        check = _ssh("command -v sqlite3 >/dev/null && echo OK || echo MISSING",
                     timeout=10)
        if check.returncode == 0 and "OK" in check.stdout:
            # Use .backup to make atomic snapshot
            backup_cmd = (f"sqlite3 {remote_db} \".backup {remote_snap}\" "
                          f"&& chmod 600 {remote_snap}")
            backup_result = _ssh(backup_cmd, timeout=120)
            if backup_result.returncode == 0:
                # scp the snapshot
                scp_result = _scp(remote_snap, local_copy)
                # Always cleanup remote snapshot
                _ssh(f"rm -f {remote_snap}", timeout=10)
                if scp_result.returncode == 0:
                    return True
                logger.warning(
                    f"scp of snapshot failed: {scp_result.stderr.strip()[:200]}")
            else:
                logger.warning(
                    f".backup failed on {host}: "
                    f"{backup_result.stderr.strip()[:200]}")
                # Cleanup any partial snapshot
                _ssh(f"rm -f {remote_snap}", timeout=10)
            backup_used = True  # We tried, but it failed
        else:
            logger.info(
                f"sqlite3 not available on {host}, falling back to scp")
    except Exception as e:
        logger.warning(f"backup attempt failed: {e}")

    # Fallback: direct scp of live DB (risk of corruption if mid-WAL-write)
    if not backup_used:
        logger.info(f"Falling back to direct scp for {host}")
    try:
        scp_result = _scp(remote_db, local_copy)
        if scp_result.returncode == 0:
            return True
        logger.warning(
            f"scp fallback failed: {scp_result.stderr.strip()[:200]}")
    except Exception as e:
        logger.warning(f"scp fallback exception: {e}")

    return False


def _load_hub_config(config_file_override=None):
    """Load hub/sync configuration.

    Priority order:
      1. ``config_file_override`` (from --config-file flag) — read as a
         standalone TOML file with top-level [[sites]], same shape as
         legacy sync.toml. No fallback if this is given and missing.
      2. ``~/.config/nomad/nomad.toml`` [hub] section — preferred location.
      3. ``~/.config/nomad/sync.toml`` — deprecated, warns every run.

    Returns a dict with keys:
        sites                   list of {name, host, ssh_user, db_path,
                                         ssh_key?} dicts (ssh_user is the
                                         canonical key — `user` accepted as
                                         legacy alias)
        output_db               Path or None (caller picks default)
        sync_interval_minutes   int or None (informational; cron is wizard's
                                job, not sync's)
        source                  human-readable string for diagnostics

    Returns None if no config can be found. Caller is responsible for
    printing the "create one with ..." help in that case.
    """
    import toml as toml_lib

    def _normalize_sites(raw_sites):
        """Accept ssh_user or legacy `user`; emit ssh_user."""
        out = []
        for s in raw_sites or []:
            site = dict(s)
            if 'ssh_user' not in site and 'user' in site:
                site['ssh_user'] = site.pop('user')
            elif 'user' in site and 'ssh_user' in site:
                site.pop('user')
            out.append(site)
        return out

    # 1. Explicit --config-file override
    if config_file_override:
        path = Path(config_file_override)
        if not path.exists():
            return None
        with open(path) as f:
            data = toml_lib.load(f)
        sites = _normalize_sites(data.get('sites', []))
        if not sites:
            return None
        return {
            'sites': sites,
            'output_db': data.get('output_db'),
            'sync_interval_minutes': data.get('sync_interval_minutes'),
            'source': str(path),
        }

    nomad_toml = Path.home() / '.config' / 'nomad' / 'nomad.toml'
    sync_toml = Path.home() / '.config' / 'nomad' / 'sync.toml'

    nomad_has_hub = False
    nomad_hub_data = None
    if nomad_toml.exists():
        try:
            with open(nomad_toml) as f:
                cfg = toml_lib.load(f)
            hub = cfg.get('hub')
            if hub and hub.get('sites'):
                nomad_has_hub = True
                nomad_hub_data = hub
        except Exception:
            pass

    sync_toml_exists = sync_toml.exists()

    # 2. nomad.toml [hub] is canonical
    if nomad_has_hub:
        if sync_toml_exists:
            click.echo(click.style(
                "  Warning: both nomad.toml [hub] and sync.toml exist. "
                "Using nomad.toml. Remove sync.toml to silence this.",
                fg="yellow"))
        sites = _normalize_sites(nomad_hub_data.get('sites', []))
        if not sites:
            return None
        return {
            'sites': sites,
            'output_db': nomad_hub_data.get('output_db'),
            'sync_interval_minutes': nomad_hub_data.get(
                'sync_interval_minutes'),
            'source': f"{nomad_toml} [hub]",
        }

    # 3. Legacy sync.toml fallback
    if sync_toml_exists:
        click.echo(click.style(
            "  Deprecation warning: sync.toml is deprecated. Move your "
            "site list under a [hub] section in nomad.toml. See "
            "`nomad sync --help` for the new format.",
            fg="yellow"))
        try:
            with open(sync_toml) as f:
                data = toml_lib.load(f)
        except Exception as e:
            click.echo(click.style(
                f"  Failed to parse sync.toml: {e}", fg="red"))
            return None
        sites = _normalize_sites(data.get('sites', []))
        if not sites:
            return None
        return {
            'sites': sites,
            'output_db': data.get('output_db'),
            'sync_interval_minutes': data.get('sync_interval_minutes'),
            'source': f"{sync_toml} (deprecated)",
        }

    return None


@cli.command()
@click.option('--config-file', '-c', type=click.Path(),
              help='Sync config file (default: read [hub] from nomad.toml, '
                   'falls back to ~/.config/nomad/sync.toml)')
@click.option('--output', '-o', type=click.Path(),
              default=None, help='Output combined database path')
@click.option('--dry-run', is_flag=True, help='Show what would be synced')
@click.pass_context
def sync(ctx, config_file, output, dry_run):
    """Sync remote NOMAD databases into a combined local database.

    \b
    Pulls nomad.db from each configured remote site via SCP,
    then merges all tables into a single combined database.
    Each site keeps its own database as a fallback.

    \b
    Preferred config location — ~/.config/nomad/nomad.toml:
      [hub]
      output_db = "~/.local/share/nomad/combined.db"
      sync_interval_minutes = 10

      [[hub.sites]]
      name = "cluster-a"
      host = "headnode-a"
      ssh_user = "jtonini"
      db_path = "~/.local/share/nomad/nomad.db"

      [[hub.sites]]
      name = "workstations"
      host = "jonimitchell"
      ssh_user = "zeus"
      db_path = "~/.local/share/nomad/nomad.db"

    \b
    Legacy fallback — ~/.config/nomad/sync.toml (deprecated, will be
    removed in a future release):
      [[sites]]
      name = "cluster-a"
      host = "headnode-a"
      user = "jtonini"
      db_path = "~/.local/share/nomad/nomad.db"

    \b
    Examples:
      nomad sync                     Sync all configured sites
      nomad sync --dry-run           Show what would happen
      nomad sync -o /tmp/combined.db Custom output path
    """
    import shutil
    import sqlite3
    import subprocess as sp
    import toml as toml_lib

    # Resolve config via shared loader (handles nomad.toml [hub] preference,
    # sync.toml fallback with deprecation warning, --config-file override)
    hub_cfg = _load_hub_config(config_file_override=config_file)

    if hub_cfg is None:
        nomad_toml_path = Path.home() / '.config' / 'nomad' / 'nomad.toml'
        click.echo(click.style(
            "  No hub configuration found.", fg="red"))
        click.echo()
        click.echo(f"  Add a [hub] section to {nomad_toml_path}:")
        click.echo()
        click.echo('    [hub]')
        click.echo('    output_db = "~/.local/share/nomad/combined.db"')
        click.echo('    sync_interval_minutes = 10')
        click.echo()
        click.echo('    [[hub.sites]]')
        click.echo('    name = "cluster-a"')
        click.echo('    host = "headnode-a"')
        click.echo('    ssh_user = "jtonini"')
        click.echo('    db_path = "~/.local/share/nomad/nomad.db"')
        click.echo()
        click.echo("  Or run `nomad init` to set this up interactively.")
        click.echo()
        return

    sites = hub_cfg['sites']

    # Output path
    default_data = Path.home() / '.local' / 'share' / 'nomad'
    if output:
        combined_path = Path(output)
    else:
        combined_path = default_data / 'combined.db'

    cache_dir = default_data / 'sync_cache'
    cache_dir.mkdir(parents=True, exist_ok=True)

    click.echo()
    click.echo(click.style(
        "  NOMAD Sync", fg="cyan", bold=True))
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo()
    click.echo(f"  Sites:    {len(sites)}")
    click.echo(f"  Output:   {combined_path}")
    click.echo()

    if dry_run:
        click.echo(click.style("  Dry run mode", fg="yellow"))
        click.echo()
        for site in sites:
            name = site.get('name', 'unknown')
            host = site.get('host', '?')
            user = site.get('ssh_user') or site.get('user', '?')
            db = site.get('db_path', '?')
            click.echo(f"  Would sync: {name}")
            click.echo(f"    scp {user}@{host}:{db} -> {cache_dir}/{name}.db")
        click.echo()
        click.echo(f"  Would merge into: {combined_path}")
        return

    # Phase 1: Pull remote databases
    click.echo(click.style(
        "  Phase 1: Pulling remote databases", bold=True))
    click.echo()

    pulled = []
    for site in sites:
        name = site.get('name', 'unknown')
        host = site.get('host')
        user = site.get('ssh_user') or site.get('user')
        remote_db = site.get('db_path', '~/.local/share/nomad/nomad.db')
        ssh_key = site.get('ssh_key')
        local_copy = cache_dir / f"{name}.db"

        click.echo(f"  {name}: ", nl=False)
        # Use SQLite .backup for atomic snapshot to fix corruption from
        # WAL mid-write during scp. Falls back to direct scp.
        try:
            ok = _pull_via_backup(
                user, host, remote_db, local_copy, ssh_key)
            if ok:
                for suffix in ["-wal", "-shm"]:
                    stale = Path(str(local_copy) + suffix)
                    if stale.exists():
                        stale.unlink()
                size_mb = local_copy.stat().st_size / (1024 * 1024)
                click.echo(click.style(
                    f"OK ({size_mb:.1f} MB)", fg="green"))
                pulled.append((name, local_copy))
            else:
                click.echo(click.style(
                    "FAILED (see log)", fg="red"))
        except Exception as e:
            click.echo(click.style(f"ERROR: {e}", fg="red"))

    if not pulled:
        click.echo()
        click.echo(click.style(
            "  No databases pulled. Check SSH connectivity.",
            fg="red"))
        return

    click.echo()

    # Phase 1b: Pull site configs (for partition metadata)
    site_configs = {}
    for site in sites:
        name = site.get('name', 'unknown')
        host = site.get('host', 'localhost')
        user = site.get('ssh_user') or site.get('user', '')
        ssh_key = site.get('ssh_key')
        # Derive config path from user's home
        if user == 'root':
            remote_toml = '/root/.config/nomad/nomad.toml'
        else:
            remote_toml = f'/home/{user}/.config/nomad/nomad.toml'
        local_toml = cache_dir / f'{name}.toml'
        toml_cmd = ['scp', '-o', 'ConnectTimeout=5',
                    '-o', 'BatchMode=yes']
        if ssh_key:
            toml_cmd += ['-i', ssh_key]
        toml_cmd += [f'{user}@{host}:{remote_toml}',
                     str(local_toml)]
        try:
            r = sp.run(toml_cmd, capture_output=True,
                       text=True, timeout=15)
            if r.returncode == 0 and local_toml.exists():
                with open(local_toml) as f:
                    cfg = toml_lib.load(f)
                partitions = (cfg.get('collectors', {})
                              .get('slurm', {})
                              .get('partitions', []))
                filesystems = (cfg.get('collectors', {})
                               .get('disk', {})
                               .get('filesystems', []))
                cluster_type = 'hpc'
                for cn, cv in cfg.get('clusters', {}).items():
                    ct = cv.get('type', 'hpc')
                    if ct in ('workstations', 'workstation'):
                        cluster_type = 'workstation'
                    break
                site_configs[name] = {
                    'partitions': ','.join(partitions)
                        if partitions else '',
                    'filesystems': ','.join(filesystems)
                        if filesystems else '',
                    'cluster_type': cluster_type,
                }
        except Exception:
            pass

    # Phase 2: Merge into combined database
    click.echo(click.style(
        "  Phase 2: Merging databases", bold=True))
    click.echo()

    # Tables that get a source_site column during merge.
    # Every data table gets tagged so the dashboard can filter by site.
    NEEDS_SITE_COL = {
        'filesystems', 'queue_state',
        'iostat_cpu', 'iostat_device',
        'mpstat_core', 'mpstat_summary',
        'vmstat', 'nfs_stats',
        'jobs', 'job_summary', 'job_metrics', 'node_state',
        'gpu_stats', 'workstation_state', 'group_membership',
        'job_accounting', 'alert_history',
        'interactive_sessions', 'interactive_summary',
        'interactive_servers', 'network_perf',
        'storage_state', 'proficiency_scores',
        'collector_runs',
    }

    # No longer needed — all tables get source_site
    SAFE_TABLES = set()

    # Schema-only tables (don't merge data)
    SKIP_TABLES = {
        'schema_version', 'schema_migrations',
        'sqlite_sequence', 'config',
    }

    # Build to a temp path so a failed or interrupted sync doesn't destroy
    # the existing combined.db. Swap into place only after successful merge.
    import os as _os
    combined_tmp_path = combined_path.with_suffix(combined_path.suffix + ".tmp")
    if combined_tmp_path.exists():
        combined_tmp_path.unlink()

    # Open a fresh connection per site (below) rather than holding one
    # across the whole loop. This prevents one site's failure from
    # polluting subsequent iterations with leaked ATTACH/lock state.
    _init = sqlite3.connect(combined_tmp_path)
    _init.execute("PRAGMA journal_mode=WAL")
    _init.close()

    total_records = 0

    for site_name, db_path in pulled:
        click.echo(f"  Merging {site_name}... ", nl=False)
        site_records = 0

        # Open a fresh connection per site. If a previous iteration's
        # DETACH failed (e.g., because the source was locked due to a
        # malformed DB error mid-read), that state cannot leak into this
        # iteration because we start with a new connection.
        combined = sqlite3.connect(combined_tmp_path)
        # Track whether ATTACH succeeded, so we know whether to DETACH
        attached = False
        try:
            # Checkpoint WAL on the cached copy before attaching
            try:
                _tmp = sqlite3.connect(str(db_path))
                _tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _tmp.close()
            except Exception:
                pass

            combined.execute(
                "ATTACH DATABASE ? AS source", (str(db_path),))
            attached = True

            # Get list of tables in source
            tables = [row[0] for row in combined.execute(
                "SELECT name FROM source.sqlite_master "
                "WHERE type='table'"
            ).fetchall()]

            for table in tables:
                if table in SKIP_TABLES or table.startswith('sqlite_'):
                    continue

                # Get source columns
                cols_info = combined.execute(
                    f"PRAGMA source.table_info({table})"
                ).fetchall()
                col_names = [c[1] for c in cols_info]
                col_defs = []
                for c in cols_info:
                    cname, ctype = c[1], c[2] or 'TEXT'
                    col_defs.append(f"{cname} {ctype}")

                # Check if table needs source_site
                needs_site = table in NEEDS_SITE_COL

                # Create table in combined if not exists
                if needs_site:
                    all_defs = col_defs + ["source_site TEXT"]
                    all_cols = col_names + ["source_site"]
                else:
                    all_defs = col_defs
                    all_cols = col_names

                # Skip autoincrement id column for inserts
                insert_cols = [c for c in all_cols if c != 'id']
                insert_defs = [d for d, c in zip(all_defs, all_cols)
                               if c != 'id']

                create_cols = ", ".join(
                    ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
                    + insert_defs
                )
                combined.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} "
                    f"({create_cols})"
                )
                # Migrate schema: add any columns present in source but missing in combined
                existing_cols = {r[1] for r in combined.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_def, col_name in zip(all_defs, all_cols):
                    if col_name != "id" and col_name not in existing_cols:
                        combined.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

                # Build insert query
                src_cols = [c for c in col_names if c != 'id']
                if needs_site:
                    select_part = ", ".join(
                        [f"source.{table}.{c}" for c in src_cols]
                        + [f"'{site_name}'"]
                    )
                    dest_cols = ", ".join(
                        src_cols + ["source_site"])
                else:
                    select_part = ", ".join(
                        [f"source.{table}.{c}" for c in src_cols])
                    dest_cols = ", ".join(src_cols)

                combined.execute(
                    f"INSERT INTO {table} ({dest_cols}) "
                    f"SELECT {select_part} FROM source.{table}"
                )

                count = combined.execute(
                    "SELECT changes()").fetchone()[0]
                site_records += count

            combined.commit()
            total_records += site_records
            click.echo(click.style(
                f"OK ({site_records:,} records)", fg="green"))

        except Exception as e:
            click.echo(click.style(f"ERROR: {e}", fg="red"))
            try:
                combined.rollback()
            except Exception:
                pass

        finally:
            # Release 'source' name if we attached it. If DETACH fails
            # (e.g., source locked after a mid-merge error), closing the
            # connection below releases it anyway, and the next iteration
            # opens a fresh one so no state leaks.
            if attached:
                try:
                    combined.execute("DETACH DATABASE source")
                except Exception:
                    pass
            try:
                combined.close()
            except Exception:
                pass

    # Write sync_sites metadata table (fresh connection, matching the
    # per-site pattern above).
    if site_configs:
        combined = sqlite3.connect(combined_tmp_path)
        try:
            combined.execute(
                'CREATE TABLE IF NOT EXISTS sync_sites ('
                '  name TEXT PRIMARY KEY,'
                '  partitions TEXT,'
                '  filesystems TEXT,'
                '  cluster_type TEXT,'
                '  synced_at TEXT'
                ')'
            )
            combined.execute('DELETE FROM sync_sites')
            from datetime import datetime
            now = datetime.now().isoformat()
            for sname, smeta in site_configs.items():
                combined.execute(
                    'INSERT OR REPLACE INTO sync_sites '
                    '(name, partitions, filesystems, '
                    ' cluster_type, synced_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (sname, smeta['partitions'],
                     smeta['filesystems'],
                     smeta['cluster_type'], now)
                )
            combined.commit()
        except Exception:
            pass
        finally:
            try:
                combined.close()
            except Exception:
                pass

    # Atomic swap: replace the real combined.db with the freshly-built
    # temp DB. If anything earlier failed badly enough to leave total_records
    # at 0, preserve the previous combined.db instead of clobbering it.
    if total_records == 0:
        click.echo()
        click.echo(click.style(
            "  Sync produced 0 records — not replacing existing combined.db",
            fg="yellow"))
        try:
            combined_tmp_path.unlink()
        except Exception:
            pass
    else:
        try:
            _os.replace(str(combined_tmp_path), str(combined_path))
        except Exception as swap_err:
            click.echo()
            click.echo(click.style(
                f"  Atomic swap failed: {swap_err}",
                fg="red"))
            click.echo(click.style(
                f"  Combined DB remains at: {combined_tmp_path}",
                fg="yellow"))

    # Summary
    click.echo()
    click.echo(click.style(
        "  ══════════════════════════════════════", fg="cyan"))
    click.echo(click.style(
        f"  Done: {len(pulled)} site(s) merged", fg="green",
        bold=True))
    click.echo()
    size_mb = combined_path.stat().st_size / (1024 * 1024)
    click.echo(f"  Combined DB: {combined_path} ({size_mb:.1f} MB)")
    click.echo(f"  Total records: {total_records:,}")
    click.echo()
    click.echo("  View with:")
    click.echo(f"    nomad status --db {combined_path}")
    click.echo(f"    nomad dashboard --db {combined_path}")
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
    Perfect for testing NØMAD without a real HPC cluster.

    Examples:
        nomad demo                  # Generate 1000 jobs, launch dashboard
        nomad demo --jobs 500       # Generate 500 jobs
        nomad demo --no-launch      # Generate only, don't launch dashboard
        nomad demo --seed 42        # Reproducible data
    """
    from nomad.demo import run_demo
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
    """NØMAD Edu — Educational analytics for HPC.

    Measures the development of computational proficiency over time
    by analyzing per-job behavioral fingerprints.
    """
    pass


@edu.command('explain')
@click.argument('job_id')
@click.option('--cluster', '-c', default=None, help='Cluster name (required if multiple clusters)')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--no-progress', is_flag=True, help='Skip progress comparison')
@click.pass_context
def edu_explain(ctx, job_id, cluster, db_path, output_json, no_progress):
    """Explain a job in plain language with proficiency scores.

    Analyzes a completed job across five dimensions of computational
    proficiency: CPU efficiency, memory sizing, time estimation,
    I/O awareness, and GPU utilization.

    Examples:
        nomad edu explain 12345
        nomad edu explain 12345 --cluster hpc-main
        nomad edu explain 12345 -c gpu-cluster --json
    """
    from nomad.edu.explain import explain_job

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    result = explain_job(
        job_id=job_id,
        cluster=cluster,
        db_path=db_path,
        show_progress=not no_progress,
        output_format='json' if output_json else 'terminal',
    )

    if result is None:
        click.echo(f"Job {job_id} not found in database.", err=True)
        click.echo("\nHint: Specify a database with --db or run 'nomad init' to configure.", err=True)
        click.echo("  Example: nomad edu explain {job_id} --db ~/nomad_demo.db", err=True)
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
        nomad edu trajectory student01
        nomad edu trajectory student01 --days 30
    """
    from nomad.edu.progress import format_trajectory, user_trajectory

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    traj = user_trajectory(db_path, username, days)

    if traj is None:
        click.echo(f"Not enough data for {username} (need at least 3 completed jobs).", err=True)
        click.echo("\nHint: Specify a database with --db or run 'nomad init' to configure.", err=True)
        click.echo(f"  Example: nomad edu trajectory {username} --db ~/nomad_demo.db", err=True)
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


@edu.command('me')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=90, help='Lookback period in days (default: 90)')
@click.option('--user', 'username', default=None,
              help='User to analyze (default: $USER). Admins can specify any user.')
@click.option('--detailed', is_flag=True,
              help='Show per-dimension trajectory and detail blurbs.')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_me(ctx, db_path, days, username, detailed, output_json):
    """Show your computational profile across recent jobs.

    Aggregates per-job recommendations into a structured summary, surfacing
    only the dimensions where most of your jobs have efficiency issues
    (>50% of jobs scoring below the recommendation threshold). Use this
    to find systemic patterns; use 'nomad edu explain <job>' for one job.

    Examples:
        nomad edu me
        nomad edu me --days 30
        nomad edu me --detailed
        nomad edu me --user other_user      (admin)
        nomad edu me --json                  (machine-readable)
    """
    import getpass
    import json as _json
    from dataclasses import asdict
    from nomad.edu.insights import user_insights, format_user_insights

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    if username is None:
        username = getpass.getuser()

    config = ctx.obj.get('config', {}) if ctx.obj else {}
    insights = user_insights(
        db_path=db_path,
        username=username,
        days=days,
        config=config,
    )

    if output_json:
        click.echo(_json.dumps(asdict(insights), indent=2, default=str))
        return

    text = format_user_insights(insights, detailed=detailed)
    click.echo(text)

    if any(i.severity == "critical" for i in insights.issues):
        raise SystemExit(2)


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
    LDAP). Configure group filters in nomad.toml.

    Examples:
        nomad edu report bio301
        nomad edu report bio301 --days 120
        nomad edu report physics-lab --json
    """
    from nomad.edu.progress import format_group_summary, group_summary

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    gs = group_summary(db_path, group_name, days)

    if gs is None:
        click.echo(f"No data found for group '{group_name}'.", err=True)
        click.echo("Ensure group membership data has been collected:")
        click.echo("  nomad collect -C groups --once")
        click.echo("\nOr specify a database with --db:", err=True)
        click.echo(f"  nomad edu report {group_name} --db ~/nomad_demo.db", err=True)
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

# =============================================================================
# DIAGNOSTICS COMMANDS
# =============================================================================


@edu.command('cloud')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--instance', 'instance_name', type=str, help='Specific instance')
@click.option('--days', default=7, help='Days to analyze (default: 7)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_cloud(ctx, db_path, instance_name, days, output_json):
    """Cloud resource proficiency analysis.

    Analyzes how efficiently cloud instances are being used.
    Same proficiency framework as HPC jobs, applied to cloud.

    Examples:
        nomad edu cloud --db ~/nomad.db
        nomad edu cloud --instance ml-train-01
    """
    config = ctx.obj.get('config', {})
    if not db_path:
        db_path = str(get_db_path(config))
    import sqlite3 as _sql
    conn = _sql.connect(db_path)
    conn.row_factory = _sql.Row
    try:
        cloud_check = conn.execute(
            "SELECT COUNT(DISTINCT node_name) as n FROM cloud_metrics WHERE node_name NOT LIKE 'EC2/%'"
        ).fetchone()
    except _sql.OperationalError:
        click.echo("No cloud data found in database.")
        conn.close()
        return
    if not cloud_check or cloud_check['n'] == 0:
        click.echo("No cloud instances found.")
        conn.close()
        return
    cutoff_dt = (datetime.now() - timedelta(days=days)).isoformat()
    if instance_name:
        instances = [{'node_name': instance_name}]
    else:
        instances = conn.execute(
            "SELECT DISTINCT node_name, instance_type FROM cloud_metrics "
            "WHERE node_name NOT LIKE 'EC2/%' AND instance_type IS NOT NULL"
        ).fetchall()
    results = []
    for inst in instances:
        name = inst['node_name']
        cpu_row = conn.execute("SELECT AVG(value) as avg, MAX(value) as peak FROM cloud_metrics WHERE node_name = ? AND metric_name = 'cpu_util' AND timestamp > ?", (name, cutoff_dt)).fetchone()
        mem_row = conn.execute("SELECT AVG(value) as avg, MAX(value) as peak FROM cloud_metrics WHERE node_name = ? AND metric_name = 'mem_util' AND timestamp > ?", (name, cutoff_dt)).fetchone()
        gpu_row = conn.execute("SELECT AVG(value) as avg, MAX(value) as peak FROM cloud_metrics WHERE node_name = ? AND metric_name = 'gpu_util' AND timestamp > ?", (name, cutoff_dt)).fetchone()
        cost_row = conn.execute("SELECT SUM(value) as total, AVG(value) as daily FROM cloud_metrics WHERE (node_name = ? OR node_name = 'EC2/' || ?) AND metric_name = 'daily_cost_usd' AND timestamp > ?", (name, name, cutoff_dt)).fetchone()
        itype_row = conn.execute("SELECT DISTINCT instance_type FROM cloud_metrics WHERE node_name = ? AND instance_type IS NOT NULL LIMIT 1", (name,)).fetchone()
        cpu_avg = cpu_row['avg'] if cpu_row and cpu_row['avg'] else 0
        mem_avg = mem_row['avg'] if mem_row and mem_row['avg'] else 0
        gpu_avg = gpu_row['avg'] if gpu_row and gpu_row['avg'] else None
        total_cost = cost_row['total'] if cost_row and cost_row['total'] else 0
        daily_cost = cost_row['daily'] if cost_row and cost_row['daily'] else 0
        itype = itype_row['instance_type'] if itype_row else 'unknown'
        def score_util(avg):
            if avg <= 0:
                return 0
            if 60 <= avg <= 85:
                return min(100, 90 + (avg - 60) * 0.4)
            if avg > 85:
                return 85 - (avg - 85) * 0.5
            if avg >= 40:
                return 50 + (avg - 40) * 2.0
            if avg >= 20:
                return 20 + (avg - 20) * 1.5
            return avg
        cpu_score = round(score_util(cpu_avg), 1)
        mem_score = round(score_util(mem_avg), 1)
        gpu_score = round(score_util(gpu_avg), 1) if gpu_avg is not None and gpu_avg > 0 else None
        if gpu_score is not None:
            overall = cpu_score * 0.3 + mem_score * 0.25 + gpu_score * 0.25 + 50 * 0.2
        else:
            overall = cpu_score * 0.35 + mem_score * 0.30 + 50 * 0.35
        overall = round(overall, 1)
        def level(s):
            if s >= 90:
                return 'Excellent'
            if s >= 70:
                return 'Good'
            if s >= 50:
                return 'Developing'
            return 'Needs Work'
        results.append({'instance': name, 'type': itype, 'cpu_avg': round(cpu_avg, 1), 'mem_avg': round(mem_avg, 1),
            'gpu_avg': round(gpu_avg, 1) if gpu_avg else None, 'cpu_score': cpu_score, 'mem_score': mem_score,
            'gpu_score': gpu_score, 'overall_score': overall, 'overall_level': level(overall),
            'total_cost': round(total_cost, 2), 'daily_cost': round(daily_cost, 2)})
    conn.close()
    if output_json:
        import json
        click.echo(json.dumps({'instances': results, 'days': days}, indent=2))
        return
    hline = chr(9472) * 56
    click.echo()
    click.echo(click.style("  NOMAD Cloud Proficiency Report", bold=True))
    click.echo(f"  Analysis period: {days} days")
    click.echo("  " + hline)
    click.echo()
    for r in sorted(results, key=lambda x: x['overall_score']):
        lc = 'green' if r['overall_score'] >= 70 else 'yellow' if r['overall_score'] >= 50 else 'red'
        click.echo("  {}  ({})".format(click.style(r['instance'], bold=True), r['type']))
        click.echo("    Overall: {}".format(click.style("{:.1f}% -- {}".format(r['overall_score'], r['overall_level']), fg=lc)))
        for dim, val, score in [('CPU', r['cpu_avg'], r['cpu_score']), ('Memory', r['mem_avg'], r['mem_score'])]:
            color = 'green' if score >= 70 else 'yellow' if score >= 50 else 'red'
            bar_len = int(val / 5)
            bar = chr(9608) * bar_len + chr(9617) * (20 - bar_len)
            click.echo(f"    {dim:<10} [{click.style(bar, fg=color)}] Util: {val:.0f}%  Score: {score:.0f}%")
        if r.get('gpu_avg') is not None and r.get('gpu_score') is not None:
            color = 'green' if r['gpu_score'] >= 70 else 'yellow' if r['gpu_score'] >= 50 else 'red'
            bar_len = int(r['gpu_avg'] / 5)
            bar = chr(9608) * bar_len + chr(9617) * (20 - bar_len)
            click.echo("    {:<10} [{}] Util: {:.0f}%  Score: {:.0f}%".format('GPU', click.style(bar, fg=color), r['gpu_avg'], r['gpu_score']))
        click.echo("    Cost: ${:.2f}/day  (${:.2f} total)".format(r['daily_cost'], r['total_cost']))
        click.echo()
    avg_overall = sum(r['overall_score'] for r in results) / len(results)
    total_cost_all = sum(r['total_cost'] for r in results)
    needs_work = [r for r in results if r['overall_score'] < 50]
    lc = 'green' if avg_overall >= 70 else 'yellow' if avg_overall >= 50 else 'red'
    click.echo("  " + hline)
    click.echo("  Average Score: {}".format(click.style(f"{avg_overall:.1f}%", fg=lc, bold=True)))
    click.echo(f"  Total Cost ({days}d): ${total_cost_all:.2f}")
    if needs_work:
        names = ', '.join(r['instance'] for r in needs_work)
        click.echo(click.style("  Needs attention: " + names, fg='yellow'))
    click.echo()


@cli.group()
def diag():
    """NØMAD Diagnostics — Infrastructure troubleshooting.

    Analyze nodes, workstations, and storage devices to identify
    issues and get actionable recommendations.

    Examples:
        nomad diag node hpc-cluster node01
        nomad diag workstation ws-physics-01
        nomad diag nas storage-01
    """
    pass


@diag.command('node')
@click.argument('cluster', required=False, default=None)
@click.argument('node_name', required=False, default=None)
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', default=24, help='Hours of history to analyze (default: 24)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def diag_node(ctx, cluster, node_name, db_path, hours, output_json):
    """Diagnose an HPC node.

    Analyzes SLURM state, job history, resource utilization, and failure
    patterns to identify issues and suggest fixes.

    Examples:
        nomad diag node hpc-cluster node01
        nomad diag node gpu-cluster gpu01 --hours 48
        nomad diag node main-cluster node05 --json
    """
    from nomad.diag.node import diagnose_node, format_diagnostic

    # Clear screen for clean output
    if not output_json:
        click.echo("\033[2J\033[H", nl=False)

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    if not db_path:
        click.echo("Error: No database found. Use --db or run 'nomad init'.", err=True)
        raise SystemExit(1)

    if cluster and "/" in cluster and not node_name:
        cluster, node_name = cluster.split("/", 1)
    if not cluster or not node_name:
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT DISTINCT cluster, node_name FROM node_state ORDER BY cluster, node_name").fetchall()
        conn.close()
        if not rows:
            click.echo("No nodes found in database.", err=True)
            raise SystemExit(1)
        click.echo("\nAvailable nodes:")
        for r in rows:
            click.echo(f"  {r[0]}/{r[1]}")
        click.echo("\nUsage: nomad diag node <cluster> <node_name>")
        return
    diag = diagnose_node(db_path, cluster, node_name, hours)

    if not diag:
        click.echo(f"Node {cluster}/{node_name} not found in database.", err=True)
        raise SystemExit(1)

    if output_json:
        import json
        from dataclasses import asdict
        result = asdict(diag)
        # Convert datetime to string
        if result.get('last_seen'):
            result['last_seen'] = str(result['last_seen'])
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(format_diagnostic(diag))


@diag.command('workstation')
@click.argument('hostname', required=False, default=None)
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', default=24, help='Hours of history to analyze (default: 24)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
@click.option('--prereqs', is_flag=True, default=False,
              help='Run collection prerequisite checks (cgroup config, probe, SSH).')
@click.option('--ssh-user', default=None,
              help='Override SSH user for prereq checks. By default, SSH config (~/.ssh/config) decides — match what the collector does.')
def diag_workstation(ctx, hostname, db_path, hours, output_json, prereqs, ssh_user):
    """Diagnose a workstation.

    Analyzes system state, user sessions, resource utilization, and
    issues to identify problems and suggest fixes.

    Features:
    - CPU/memory utilization and bottlenecks
    - Disk usage and I/O performance
    - Active user sessions
    - Process analysis (runaway jobs, zombies)
    - Department/group association

    Examples:
        nomad diag workstation ws-physics-01
        nomad diag workstation lab-desktop-05 --hours 48
        nomad diag workstation chem-workstation --json
    """
    from nomad.diag.workstation import diagnose_workstation, format_diagnostic

    # Clear screen for clean output
    if not output_json:
        click.echo("\033[2J\033[H", nl=False)

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    if not db_path:
        click.echo("Error: No database found. Use --db or run 'nomad init'.", err=True)
        raise SystemExit(1)

    if not hostname:
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT DISTINCT hostname FROM workstation_state ORDER BY hostname").fetchall()
        conn.close()
        if not rows:
            click.echo("No workstations found in database.", err=True)
            raise SystemExit(1)
        click.echo("\nAvailable workstations:")
        for r in rows:
            click.echo(f"  {r[0]}")
        click.echo("\nUsage: nomad diag workstation <hostname>")
        return
    diag = diagnose_workstation(
        db_path, hostname, hours,
        check_prereqs=prereqs,
        ssh_user=ssh_user,
    )

    if not diag:
        click.echo(f"Workstation {hostname} not found in database.", err=True)
        raise SystemExit(1)

    if output_json:
        import json
        from dataclasses import asdict
        result = asdict(diag)
        if result.get('last_seen'):
            result['last_seen'] = str(result['last_seen'])
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(format_diagnostic(diag))


@diag.command('nas')
@click.argument('hostname', required=False, default=None)
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', default=24, help='Hours of history to analyze (default: 24)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def diag_nas(ctx, hostname, db_path, hours, output_json):
    """Diagnose a NAS/storage device.

    Analyzes storage capacity, I/O throughput, NFS performance, and
    mount issues to identify problems and suggest fixes.

    Supports ZFS-specific diagnostics:
    - Pool health (zpool status)
    - Scrub status and errors
    - ARC hit rates
    - Snapshot usage
    - Quota enforcement

    Examples:
        nomad diag nas storage-01
        nomad diag nas zfs-home --hours 48
        nomad diag nas nfs-server --json
    """
    from nomad.diag.storage import diagnose_storage, format_diagnostic

    # Clear screen for clean output
    if not output_json:
        click.echo("\033[2J\033[H", nl=False)

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    if not db_path:
        click.echo("Error: No database found. Use --db or run 'nomad init'.", err=True)
        raise SystemExit(1)

    if not hostname:
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT DISTINCT hostname FROM storage_state ORDER BY hostname").fetchall()
        conn.close()
        if not rows:
            click.echo("No storage devices found in database.", err=True)
            raise SystemExit(1)
        click.echo("\nAvailable storage devices:")
        for r in rows:
            click.echo(f"  {r[0]}")
        click.echo("\nUsage: nomad diag nas <hostname>")
        return
    diag = diagnose_storage(db_path, hostname, hours)

    if not diag:
        click.echo(f"Storage device {hostname} not found in database.", err=True)
        raise SystemExit(1)

    if output_json:
        import json
        from dataclasses import asdict
        result = asdict(diag)
        if result.get('last_seen'):
            result['last_seen'] = str(result['last_seen'])
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(format_diagnostic(diag))



@diag.command('network')
@click.option('--source', '-s', help='Source hostname (optional)')
@click.option('--dest', '-d', help='Destination hostname (optional)')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', default=168, help='Hours of history to analyze (default: 168 = 1 week)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def diag_network(ctx, source, dest, db_path, hours, output_json):
    """Diagnose network performance."""
    from nomad.diag.network import diagnose_network, format_diagnostic
    if not output_json:
        click.echo("\033[2J\033[H", nl=False)
    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)
    if not db_path:
        click.echo("Error: No database found.", err=True)
        raise SystemExit(1)
    diag = diagnose_network(db_path, source, dest, hours)
    if not diag:
        click.echo("No network data found.", err=True)
        raise SystemExit(1)
    if output_json:
        import json
        from dataclasses import asdict
        result = asdict(diag)
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(format_diagnostic(diag))

@diag.command('gpu')
@click.option('--node', '-n', default=None, help='Limit to a specific node.')
@click.option('--health', is_flag=True, default=False, help='Show hardware health only (PCIe, ECC, remap).')
@click.option('--hours', default=24, type=int, show_default=True, help='Hours of history to analyze.')
@click.option('--db', 'db_path', default=None, type=click.Path(), help='Database path override.')
@click.pass_context
def diag_gpu(ctx, node, health, hours, db_path):
    """GPU diagnostic report: current state, workload patterns, hardware health.
 
    Without --health, shows utilization summary, workload classification
    distribution, and temperature status for all GPUs (or a specific node).
 
    With --health, focuses on PCIe replay counters, ECC errors, and row
    remap status — the early-warning signals for hardware degradation.
 
    Examples:
 
      nomad diag gpu
 
      nomad diag gpu --node node51
 
      nomad diag gpu --health
 
      nomad diag gpu --hours 48
    """
    import sqlite3
    from datetime import datetime, timedelta
 
    db_path = db_path or get_db_path(ctx.obj.get("config", {}) if ctx.obj else {})
    if not Path(db_path).exists():
        raise click.ClickException(f"Database not found: {db_path}")
 
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
 
    # ------------------------------------------------------------------
    # Health-focused report
    # ------------------------------------------------------------------
    if health:
        click.echo(click.style(f"\nGPU Hardware Health ({hours}h window)", bold=True))
        click.echo("=" * 56)
 
        try:
            q = "SELECT * FROM gpu_health WHERE timestamp >= ? ORDER BY timestamp DESC"
            params = [cutoff]
            if node:
                q = "SELECT * FROM gpu_health WHERE timestamp >= ? AND node = ? ORDER BY timestamp DESC"
                params = [cutoff, node]
            rows = conn.execute(q, params).fetchall()
        except sqlite3.OperationalError:
            click.echo("  No gpu_health table found — DCGM not yet active on this cluster.")
            conn.close()
            return
 
        if not rows:
            click.echo("  No health data in the requested window.")
            conn.close()
            return
 
        # Summarise per (node, gpu_id): latest status + worst seen
        summary: dict[tuple, dict] = {}
        for row in rows:
            key = (row['node'], row['gpu_id'])
            if key not in summary:
                summary[key] = dict(row)
                summary[key]['worst_status'] = row['health_status']
            else:
                # Escalate worst status
                rank = {'OK': 0, 'WARN': 1, 'HOT': 2, 'CRIT': 3}
                if rank.get(row['health_status'], 0) > rank.get(summary[key]['worst_status'], 0):
                    summary[key]['worst_status'] = row['health_status']
 
        status_colors = {'OK': 'green', 'WARN': 'yellow', 'HOT': 'yellow', 'CRIT': 'red'}
        for (node_name, gpu_id), s in sorted(summary.items()):
            color = status_colors.get(s['worst_status'], 'white')
            badge = click.style(f"[{s['worst_status']:4s}]", fg=color, bold=True)
            click.echo(f"\n  {badge}  {node_name} GPU {gpu_id}")
            if s['pcie_replay_count'] > 0:
                rate = s.get('pcie_replay_rate_per_sec', 0)
                click.echo(f"         PCIe replay: {s['pcie_replay_count']} total  ({rate:.3f}/s)")
            if s['ecc_correctable_total'] > 0:
                click.echo(f"         ECC correctable: {s['ecc_correctable_total']}")
            if s['ecc_uncorrectable_total'] > 0:
                click.echo(click.style(
                    f"         ECC uncorrectable: {s['ecc_uncorrectable_total']}  (investigate)", fg='red'
                ))
            if s['row_remap_failure'] > 0:
                click.echo(click.style(
                    "         Row remap failure — remove from production", fg='red', bold=True
                ))
 
        conn.close()
        return
 
    # ------------------------------------------------------------------
    # Standard diagnostic report
    # ------------------------------------------------------------------
    click.echo(click.style(f"\nGPU Diagnostic ({hours}h window)", bold=True))
    if node:
        click.echo(f"Node filter: {node}")
    click.echo("=" * 56)
 
    try:
        q = """
            SELECT node_name, gpu_index, gpu_name,
                   AVG(gpu_util_percent) AS avg_util,
                   MAX(gpu_util_percent) AS max_util,
                   AVG(real_util_pct)    AS avg_real_util,
                   AVG(temperature_c)    AS avg_temp,
                   MAX(temperature_c)    AS max_temp,
                   AVG(memory_used_mb)   AS avg_mem_used,
                   MAX(memory_total_mb)  AS mem_total,
                   data_source,
                   COUNT(*) AS samples
            FROM gpu_stats
            WHERE timestamp >= ?
        """
        params: list = [cutoff]
        if node:
            q += " AND node_name = ?"
            params.append(node)
        q += " GROUP BY node_name, gpu_index ORDER BY node_name, gpu_index"
        rows = conn.execute(q, params).fetchall()
    except sqlite3.OperationalError as e:
        click.echo(f"  Database error: {e}")
        conn.close()
        return
 
    if not rows:
        click.echo("  No GPU data in the requested window.")
        conn.close()
        return
 
    for row in rows:
        avg_util = row['avg_util'] or 0
        avg_real = row['avg_real_util']
        avg_temp = row['avg_temp'] or 0
        max_temp = row['max_temp'] or 0
        mem_pct = (row['avg_mem_used'] / row['mem_total'] * 100) if row['mem_total'] else 0
        source = row['data_source'] or 'nvidia-smi'
 
        click.echo(f"\n  {row['node_name']}  GPU {row['gpu_index']}  {row['gpu_name']}")
        click.echo(f"    Data source : {source}  ({row['samples']} samples)")
        click.echo(f"    Util (smi)  : avg {avg_util:5.1f}%  max {row['max_util'] or 0:5.1f}%")
        if avg_real is not None:
            click.echo(f"    Real Util   : avg {avg_real:5.1f}%")
        click.echo(f"    Memory      : {mem_pct:5.1f}% avg used")
        temp_color = 'red' if max_temp >= 93 else ('yellow' if max_temp >= 85 else 'green')
        click.echo(f"    Temperature : avg {avg_temp:4.0f}°C  max {click.style(f'{max_temp}°C', fg=temp_color)}")
 
    # Workload classification distribution
    try:
        wq = """
            SELECT node_name, workload_class, COUNT(*) AS n
            FROM gpu_stats
            WHERE timestamp >= ? AND workload_class IS NOT NULL
        """
        wparams: list = [cutoff]
        if node:
            wq += " AND node_name = ?"
            wparams.append(node)
        wq += " GROUP BY node_name, workload_class ORDER BY node_name, n DESC"
        wrows = conn.execute(wq, wparams).fetchall()
    except sqlite3.OperationalError:
        wrows = []
 
    if wrows:
        click.echo(click.style("\n  Workload distribution", bold=True))
        current_node = None
        for wr in wrows:
            if wr['node_name'] != current_node:
                current_node = wr['node_name']
                click.echo(f"\n    {current_node}")
            click.echo(f"      {wr['workload_class']:30s}  {wr['n']:4d} samples")
 
    conn.close()
 

# =============================================================================
# INSIGHT ENGINE COMMANDS
# =============================================================================

@cli.group()
def insights():
    """NØMAD Insight Engine — operational narratives and analysis."""
    pass


@insights.command('brief')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=24, help='Lookback window (hours)')
@click.option('--cluster', default=None, help='Cluster name for display')
@click.pass_context
def insights_brief(ctx, db_path, hours, cluster):
    """Print a concise operational briefing."""
    from nomad.insights import InsightEngine

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
        raise SystemExit(1)

    if not cluster:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(ctx.obj.get('config', {}))
    else:
        cluster_name = cluster
    engine = InsightEngine(db_path, hours=hours, cluster_name=cluster_name)
    click.echo(engine.brief())


@insights.command('detail')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=24, help='Lookback window (hours)')
@click.option('--cluster', default=None, help='Cluster name for display')
@click.pass_context
def insights_detail(ctx, db_path, hours, cluster):
    """Print a detailed operational analysis."""
    from nomad.insights import InsightEngine

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
        raise SystemExit(1)

    if not cluster:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(ctx.obj.get('config', {}))
    else:
        cluster_name = cluster
    engine = InsightEngine(db_path, hours=hours, cluster_name=cluster_name)
    click.echo(engine.detail())


@insights.command('json')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=24, help='Lookback window (hours)')
@click.option('--cluster', default=None, help='Cluster name for display')
@click.pass_context
def insights_json(ctx, db_path, hours, cluster):
    """Output insights as JSON (for API/Console integration)."""
    from nomad.insights import InsightEngine

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
        raise SystemExit(1)

    if not cluster:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(ctx.obj.get('config', {}))
    else:
        cluster_name = cluster
    engine = InsightEngine(db_path, hours=hours, cluster_name=cluster_name)
    click.echo(engine.to_json())


@insights.command('slack')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=24, help='Lookback window (hours)')
@click.option('--cluster', default=None, help='Cluster name for display')
@click.option('--webhook', help='Slack webhook URL (if provided, posts directly)')
@click.pass_context
def insights_slack(ctx, db_path, hours, cluster, webhook):
    """Generate a Slack-formatted insight message."""
    from nomad.insights import InsightEngine

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
        raise SystemExit(1)

    if not cluster:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(ctx.obj.get('config', {}))
    else:
        cluster_name = cluster
    engine = InsightEngine(db_path, hours=hours, cluster_name=cluster_name)
    message = engine.to_slack()

    if webhook:
        import urllib.request
        import json as json_mod
        req = urllib.request.Request(
            webhook,
            data=json_mod.dumps({"text": message}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            click.echo("Posted to Slack.")
        except Exception as e:
            click.echo(f"Failed to post: {e}", err=True)
            click.echo(message)
    else:
        click.echo(message)


@insights.command('digest')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=24, help='Lookback window (hours)')
@click.option('--cluster', default=None, help='Cluster name for display')
@click.option('--period', type=click.Choice(['daily', 'weekly']), default='daily', help='Digest period')
@click.option('--email', 'email_addr', help='Send via email (requires configured SMTP)')
@click.pass_context
def insights_digest(ctx, db_path, hours, cluster, period, email_addr):
    """Generate an email digest of insights."""
    from nomad.insights import InsightEngine

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
        raise SystemExit(1)

    if period == 'weekly':
        hours = max(hours, 168)

    if not cluster:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(ctx.obj.get('config', {}))
    else:
        cluster_name = cluster
    engine = InsightEngine(db_path, hours=hours, cluster_name=cluster_name)
    subject, body = engine.to_email(period=period)

    if email_addr:
        click.echo("Email delivery not yet configured. Subject and body below:")

    click.echo(f"Subject: {subject}")
    click.echo("")
    click.echo(body)

@insights.command('gpu')
@click.option('--node', '-n', default=None, help='Limit to a specific node.')
@click.option('--hours', default=6, type=int, show_default=True, help='Hours of history to summarize.')
@click.option('--db', 'db_path', default=None, type=click.Path(), help='Database path override.')
@click.pass_context
def insights_gpu(ctx, node, hours, db_path):
    """Narrative summary of GPU usage patterns and health concerns.
 
    Produces a human-readable summary of what the cluster's GPUs have
    been doing over the specified window. Highlights workload patterns,
    potential inefficiency (high nvidia-smi util but low Real Util),
    and any hardware health concerns from DCGM.
 
    Examples:
 
      nomad insights gpu
 
      nomad insights gpu --hours 12
 
      nomad insights gpu --node spdr16
    """
    import sqlite3
    from datetime import datetime, timedelta
 
    db_path = db_path or get_db_path(ctx.obj.get("config", {}) if ctx.obj else {})
    if not Path(db_path).exists():
        raise click.ClickException(f"Database not found: {db_path}")
 
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
 
    try:
        q = """
            SELECT node_name, gpu_index, gpu_name,
                   AVG(gpu_util_percent) AS avg_smi_util,
                   AVG(real_util_pct)    AS avg_real_util,
                   AVG(temperature_c)    AS avg_temp,
                   MAX(temperature_c)    AS max_temp,
                   data_source
            FROM gpu_stats
            WHERE timestamp >= ?
        """
        params: list = [cutoff]
        if node:
            q += " AND node_name = ?"
            params.append(node)
        q += " GROUP BY node_name, gpu_index"
        rows = conn.execute(q, params).fetchall()
    except sqlite3.OperationalError:
        click.echo("No GPU data found.")
        conn.close()
        return
 
    if not rows:
        click.echo(f"No GPU data in the last {hours}h.")
        conn.close()
        return
 
    # Dominant workload per node
    try:
        wq = """
            SELECT node_name, gpu_index, workload_class, COUNT(*) AS n
            FROM gpu_stats
            WHERE timestamp >= ? AND workload_class IS NOT NULL
        """
        wparams: list = [cutoff]
        if node:
            wq += " AND node_name = ?"
            wparams.append(node)
        wq += " GROUP BY node_name, gpu_index, workload_class ORDER BY n DESC"
        wrows = conn.execute(wq, wparams).fetchall()
        dominant: dict[tuple, str] = {}
        seen: set = set()
        for wr in wrows:
            key = (wr['node_name'], wr['gpu_index'])
            if key not in seen:
                dominant[key] = wr['workload_class']
                seen.add(key)
    except sqlite3.OperationalError:
        dominant = {}
 
    # Health summary
    try:
        hrows = conn.execute(
            "SELECT node, gpu_id, health_status FROM gpu_health "
            "WHERE timestamp >= ? ORDER BY timestamp DESC",
            [cutoff]
        ).fetchall()
        health_latest: dict[tuple, str] = {}
        for hr in hrows:
            key = (hr['node'], hr['gpu_id'])
            if key not in health_latest:
                health_latest[key] = hr['health_status']
    except sqlite3.OperationalError:
        health_latest = {}
 
    # ------------------------------------------------------------------
    # Generate narrative
    # ------------------------------------------------------------------
    click.echo(click.style(f"\nGPU Insights — last {hours}h", bold=True))
    if node:
        click.echo(f"Scope: {node}")
    click.echo()
 
    issues: list[str] = []
    narratives: list[str] = []
 
    for row in rows:
        node_name = row['node_name']
        gpu_id = row['gpu_index']
        gpu_name = row['gpu_name']
        avg_smi = row['avg_smi_util'] or 0
        avg_real = row['avg_real_util']
        avg_temp = row['avg_temp'] or 0
        max_temp = row['max_temp'] or 0
        source = row['data_source'] or 'nvidia-smi'
        key = (node_name, gpu_id)
        workload = dominant.get(key)
        health = health_latest.get(key, 'OK')
 
        # Build narrative sentence
        if workload and workload != 'idle':
            narrative = f"{node_name} GPU {gpu_id} has been running {workload}"
        elif avg_smi > 5:
            narrative = f"{node_name} GPU {gpu_id} is active ({avg_smi:.0f}% avg utilization)"
        else:
            narrative = f"{node_name} GPU {gpu_id} is idle"
 
        narratives.append(f"  {narrative}.")
 
        # Flag utilization gap (nvidia-smi high, Real Util low — suggests idle tensor cores)
        if avg_real is not None and avg_smi > 40 and avg_real < avg_smi * 0.6:
            gap = avg_smi - avg_real
            issues.append(
                f"  {node_name} GPU {gpu_id}: nvidia-smi reports {avg_smi:.0f}% but "
                f"Real Util is {avg_real:.0f}% — {gap:.0f}pt gap suggests kernel-launch overhead "
                f"or idle pipeline stages."
            )
 
        # Temperature warning
        if max_temp >= 90:
            issues.append(f"  {node_name} GPU {gpu_id}: peak temperature {max_temp}°C.")
 
        # Health issues
        if health == 'CRIT':
            issues.append(click.style(
                f"  {node_name} GPU {gpu_id}: CRITICAL health status — row remap failure or "
                f"uncorrectable ECC errors. Remove from production.", fg='red', bold=True
            ))
        elif health == 'HOT':
            issues.append(f"  {node_name} GPU {gpu_id}: HOT — temperature at or above warning threshold.")
        elif health == 'WARN':
            issues.append(f"  {node_name} GPU {gpu_id}: PCIe replay errors detected — monitor link health.")
 
    for line in narratives:
        click.echo(line)
 
    if issues:
        click.echo(click.style("\n  Attention:", bold=True))
        for issue in issues:
            click.echo(issue)
    else:
        click.echo(click.style("\n  No issues detected.", fg='green'))
 
    # DCGM coverage note
    sources = {row['data_source'] for row in rows if row['data_source']}
    if 'dcgm' not in sources:
        click.echo(
            "\n  Note: DCGM is not active on this cluster. Real Util, workload classification,\n"
            "  and hardware health metrics are unavailable. Install dcgm and set dcgmi in PATH\n"
            "  on GPU nodes to enable enhanced metrics."
        )
 
    conn.close()
 

# =============================================================================
# DYNAMICS COMMANDS
# =============================================================================
@cli.group()
def dyn():
    """System dynamics analysis — ecological and economic metrics.

    Quantitative frameworks from community ecology and economics
    applied to research computing usage patterns.

    \b
    Commands:
      nomad dyn               Full dynamics summary
      nomad dyn diversity     Workload diversity indices
      nomad dyn niche         Resource usage overlap between groups
      nomad dyn capacity      Multi-dimensional carrying capacity
      nomad dyn resilience    Recovery time after disturbance events
      nomad dyn externality   Inter-group impact quantification
    """
    pass



@dyn.command('summary')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_summary(ctx, db_path, hours, cluster_name, output_json):
    """Full dynamics summary combining all metrics.

    Produces a holistic assessment of workload diversity, niche overlap,
    carrying capacity, resilience, and inter-group externalities.
    """
    from nomad.dynamics.engine import DynamicsEngine

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))
    if cluster_name is None:
        cluster_name = config.get('cluster', {}).get('name', 'cluster')

    engine = DynamicsEngine(db_path, hours=hours, cluster_name=cluster_name)

    if output_json:
        click.echo(engine.to_json())
    else:
        click.echo(engine.full_summary())


@dyn.command('diversity')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--by', 'dimension', type=click.Choice(['group', 'partition', 'user']),
              default='group', help='Dimension to measure diversity over')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_diversity(ctx, db_path, hours, dimension, output_json):
    """Workload diversity indices (Shannon, Simpson).

    \b
    Measures how evenly workload is distributed across groups,
    partitions, or users. Tracks trends over time and warns
    when diversity drops below safe levels.

    \b
    Examples:
      nomad dyn diversity                # diversity by group
      nomad dyn diversity --by partition  # diversity by partition
      nomad dyn diversity --hours 720    # 30-day window
    """
    from nomad.dynamics.engine import DynamicsEngine
    from nomad.dynamics.formatters import format_diversity_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    engine = DynamicsEngine(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_diversity_json(engine.diversity), indent=2))
    else:
        click.echo(engine.diversity_report(dimension=dimension))


@dyn.command('niche')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--threshold', type=float, default=0.6, help='Overlap threshold for flagging')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_niche(ctx, db_path, hours, threshold, output_json):
    """Resource usage overlap between user communities.

    \b
    Computes pairwise niche overlap (Pianka's index) between groups.
    Flags high-overlap pairs that are likely to compete for the same
    resources, creating contention risk.

    \b
    Examples:
      nomad dyn niche                    # default threshold 0.6
      nomad dyn niche --threshold 0.5    # more sensitive
      nomad dyn niche --json             # JSON output for Console
    """
    from nomad.dynamics.niche import compute_niche_overlap
    from nomad.dynamics.formatters import format_niche_cli, format_niche_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_niche_overlap(db_path, hours=hours, overlap_threshold=threshold)

    if output_json:
        import json
        click.echo(json.dumps(format_niche_json(result), indent=2))
    else:
        click.echo(format_niche_cli(result))


@dyn.command('capacity')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_capacity(ctx, db_path, hours, output_json):
    """Multi-dimensional carrying capacity utilization.

    \b
    Analyzes utilization across CPU, memory, GPU, I/O, and scheduler
    queue. Identifies the binding constraint (Liebig's law of the
    minimum) and projects time to saturation.

    \b
    Examples:
      nomad dyn capacity            # current capacity report
      nomad dyn capacity --json     # JSON output for Console
    """
    from nomad.dynamics.capacity import compute_capacity
    from nomad.dynamics.formatters import format_capacity_cli, format_capacity_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_capacity(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_capacity_json(result), indent=2))
    else:
        click.echo(format_capacity_cli(result))


@dyn.command('resilience')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=720, help='Analysis window (hours, default 30 days)')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_resilience(ctx, db_path, hours, output_json):
    """Recovery time after disturbance events.

    \b
    Detects disturbance events (node failures, job failure spikes)
    and computes mean/median recovery time. Tracks whether the
    cluster is becoming more or less resilient over time.

    \b
    Requires historical data — longer time windows produce better
    resilience estimates.

    \b
    Examples:
      nomad dyn resilience               # 30-day window
      nomad dyn resilience --hours 2160  # 90-day window
    """
    from nomad.dynamics.resilience import compute_resilience
    from nomad.dynamics.formatters import format_resilience_cli, format_resilience_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_resilience(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_resilience_json(result), indent=2))
    else:
        click.echo(format_resilience_cli(result))


@dyn.command('externality')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--threshold', type=float, default=0.3, help='Minimum correlation to report')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_externality(ctx, db_path, hours, threshold, output_json):
    """Inter-user/group impact quantification.

    \b
    Correlates each group's resource-intensive behavior with other
    groups' job failure rates. Answers: "whose jobs are hurting
    other people's jobs?"

    \b
    Examples:
      nomad dyn externality                  # default threshold
      nomad dyn externality --threshold 0.2  # more sensitive
    """
    from nomad.dynamics.externality import compute_externalities
    from nomad.dynamics.formatters import format_externality_cli, format_externality_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_externalities(db_path, hours=hours, correlation_threshold=threshold)

    if output_json:
        import json
        click.echo(json.dumps(format_externality_json(result), indent=2))
    else:
        click.echo(format_externality_cli(result))

# =============================================================================
# REFERENCE COMMANDS
# =============================================================================

@cli.command('ref')
@click.argument('topic_parts', nargs=-1)
def ref(topic_parts):
    """Built-in reference and documentation.

    Look up any NOMAD command, module, configuration option, or concept.

    \b
    Examples:
      nomad ref                          Browse all topics
      nomad ref alerts                   Alert system overview
      nomad ref dyn diversity            Dynamics diversity command
      nomad ref collectors disk          Disk collector details
      nomad ref config                   Configuration reference
      nomad ref search regime divergence Search all documentation
      nomad ref tessera                  TESSERA methodology
      nomad ref concepts governance      Ostrom governance framework
    """
    from nomad.reference import KnowledgeBase, ReferenceFormatter

    kb = KnowledgeBase()
    fmt = ReferenceFormatter()

    if not topic_parts:
        # No arguments — show index
        categories = {}
        for entry in kb.list_topics():
            cat = entry.category or "other"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(entry)
        click.echo(fmt.format_index(categories))
        return

    # Handle 'search' as special first word
    if topic_parts[0] == "search":
        query = " ".join(topic_parts[1:]) if len(topic_parts) > 1 else ""
        if not query:
            click.echo("\nUsage: nomad ref search <query>")
            click.echo("Example: nomad ref search regime divergence\n")
            return
        results = kb.search(query, max_results=10)
        click.echo(fmt.format_search_results(query, results))
        return

    # Build the topic key from parts
    # Try increasingly specific keys: "dyn.diversity", then "dyn", etc.
    # For "dyn diversity" -> try "dyn.diversity" first, then "dyn"
    # For "collectors disk" -> try "collectors.disk" first
    # For "concepts regime_divergence" -> try "concepts.regime_divergence"

    # Strategy: join all parts with dots and try exact match first
    full_key = ".".join(topic_parts)
    entry = kb.get(full_key)
    if entry:
        click.echo(fmt.format_entry(entry))
        children = kb.get_children(full_key)
        if children:
            click.echo(fmt.format_topic_list(children, heading="Subtopics"))
        return

    # Try with underscores instead of dots for multi-word concepts
    # e.g., "concepts regime divergence" -> "concepts.regime_divergence"
    if len(topic_parts) >= 2:
        prefix = topic_parts[0]
        rest = "_".join(topic_parts[1:])
        underscore_key = f"{prefix}.{rest}"
        entry = kb.get(underscore_key)
        if entry:
            click.echo(fmt.format_entry(entry))
            return

    # Try just the first word as the key
    first = topic_parts[0]
    entry = kb.get(first)
    if entry:
        click.echo(fmt.format_entry(entry))
        children = kb.get_children(first)
        if children:
            click.echo(fmt.format_topic_list(children, heading="Subtopics"))

        # If there were additional words, they might be a subtopic we missed
        if len(topic_parts) > 1:
            sub_key = first + "." + ".".join(topic_parts[1:])
            sub_entry = kb.get(sub_key)
            if sub_entry and sub_entry.key != entry.key:
                click.echo("\n" + "=" * 40 + "\n")
                click.echo(fmt.format_entry(sub_entry))
        return

    # Nothing found by key — fall back to search
    query = " ".join(topic_parts)
    results = kb.search(query, max_results=5)
    if results:
        click.echo(fmt.format_search_results(query, results))
    else:
        click.echo(f"\nNo reference entry found for '{query}'.")
        click.echo("Use 'nomad ref' to browse topics or 'nomad ref search <query>' to search.\n")



# =============================================================================
# DEVELOPER TOOLCHAIN COMMANDS
# =============================================================================

from nomad.dev.cli_commands import dev
cli.add_command(dev)

# =============================================================================
# COMMUNITY COMMANDS
# =============================================================================

@cli.group()
def community():
    """NØMAD Community Dataset commands."""
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
    from pathlib import Path

    from nomad.community import export_community_data

    if salt_file:
        with open(salt_file) as f:
            salt = f.read().strip()
    elif not salt:
        click.echo("Error: Either --salt or --salt-file is required", err=True)
        raise SystemExit(1)

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = str(get_db_path(config))

    if not Path(db_path).exists():
        click.echo(f"Error: Database not found at {db_path}. Run 'nomad collect --once' first.", err=True)
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
        raise SystemExit(1) from None


@community.command('verify')
@click.argument('file_path', type=click.Path(exists=True))
def community_verify(file_path):
    """Verify an export file meets community standards."""
    from pathlib import Path

    from nomad.community import verify_export
    result = verify_export(Path(file_path))
    raise SystemExit(0 if result['valid'] else 1)


@community.command('preview')
@click.argument('file_path', type=click.Path(exists=True))
@click.option('-n', 'n_samples', default=5, help='Number of sample records')
def community_preview(file_path, n_samples):
    """Preview an export file."""
    from pathlib import Path

    from nomad.community import preview_export
    preview_export(Path(file_path), n_samples=n_samples)



def main() -> None:
    """Entry point for CLI."""
    cli(obj={})



cli.add_command(issue_group, 'issue')

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
        nomad report-interactive              # Full report
        nomad report-interactive --json       # JSON output
        nomad report-interactive --quiet      # Only show alerts
    """
    import json as json_module

    try:
        from nomad.collectors.interactive import get_report, print_report
    except (ImportError, SyntaxError):
        click.echo("Error: Interactive collector requires Python 3.7+", err=True)
        raise SystemExit(1) from None

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



@diag.command('cloud')
@click.argument('instance_name')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=7, help='Days of history (default: 7)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def diag_cloud(ctx, instance_name, db_path, days, output_json):
    """Diagnose a cloud instance."""
    config = ctx.obj.get('config', {})
    if not db_path:
        db_path = str(get_db_path(config))
    import sqlite3 as _sql
    conn = _sql.connect(db_path)
    conn.row_factory = _sql.Row
    cutoff_dt = (datetime.now() - timedelta(days=days)).isoformat()
    check = conn.execute('SELECT COUNT(*) as n FROM cloud_metrics WHERE node_name = ? AND timestamp > ?', (instance_name, cutoff_dt)).fetchone()
    if not check or check['n'] == 0:
        click.echo(f'No data for {instance_name}'); conn.close(); return
    info = conn.execute("SELECT DISTINCT instance_type, availability_zone FROM cloud_metrics WHERE (node_name = ? OR node_name = 'EC2/' || ?) AND instance_type IS NOT NULL LIMIT 1", (instance_name, instance_name)).fetchone()
    itype = info['instance_type'] if info else 'unknown'
    az = info['availability_zone'] if info else 'unknown'
    metrics = {}
    for m in ['cpu_util','mem_util','gpu_util','gpu_mem_util']:
        row = conn.execute('SELECT AVG(value) as avg, MIN(value) as mn, MAX(value) as mx, COUNT(*) as n FROM cloud_metrics WHERE node_name = ? AND metric_name = ? AND timestamp > ?', (instance_name, m, cutoff_dt)).fetchone()
        if row and row['n'] > 0:
            metrics[m] = {'avg':row['avg'],'min':row['mn'],'max':row['mx'],'n':row['n']}
    cost_row = conn.execute("SELECT SUM(value) as total, AVG(value) as daily FROM cloud_metrics WHERE (node_name = ? OR node_name = 'EC2/' || ?) AND metric_name = 'daily_cost_usd' AND timestamp > ?", (instance_name, instance_name, cutoff_dt)).fetchone()
    total_cost = cost_row['total'] if cost_row and cost_row['total'] else 0
    daily_avg = cost_row['daily'] if cost_row and cost_row['daily'] else 0
    conn.close()
    if output_json:
        import json; click.echo(json.dumps({'instance':instance_name,'type':itype,'zone':az,'days':days,'metrics':{k:{kk:round(vv,2) for kk,vv in v.items()} for k,v in metrics.items()},'total_cost':round(total_cost,2),'daily_avg':round(daily_avg,2)},indent=2)); return
    hl = chr(9472)*56
    click.echo()
    click.echo(click.style(f'  NOMAD Cloud Diagnostic -- {instance_name}', bold=True))
    click.echo(f'  Instance type: {itype}')
    click.echo(f'  Availability zone: {az}')
    click.echo('  '+hl); click.echo()
    click.echo(click.style('  Utilization Summary', bold=True)); click.echo('  '+hl)
    for mn,lb in [('cpu_util','CPU'),('mem_util','Memory'),('gpu_util','GPU Compute'),('gpu_mem_util','GPU Memory')]:
        if mn not in metrics:
            continue
        v = metrics[mn]; a = v['avg']
        c = 'green' if a < 50 else 'yellow' if a < 80 else 'red'
        bl = int(a/5); b = chr(9608)*bl + chr(9617)*(20-bl)
        click.echo('    {:<16} [{}] {}'.format(lb, click.style(b,fg=c), click.style(f'{a:.1f}%',fg=c)))
        click.echo('                   Min: {:.1f}%  Max: {:.1f}%  ({} samples)'.format(v['min'],v['max'],v['n']))
    click.echo(); click.echo(click.style('  Cost Analysis', bold=True)); click.echo('  '+hl)
    click.echo(f'    Daily average:     ${daily_avg:.2f}')
    click.echo(f'    {days}-day total:      ${total_cost:.2f}')
    click.echo(f'    30-day projection: ${daily_avg*30:.2f}')
    click.echo(); click.echo(click.style('  Recommendations', bold=True)); click.echo('  '+hl)
    ca = metrics.get('cpu_util',{}).get('avg',0); ma = metrics.get('mem_util',{}).get('avg',0); ga = metrics.get('gpu_util',{}).get('avg',0)
    hr = False
    if ca < 20:
        click.echo(click.style('    [HIGH] ',fg='red')+f'CPU averages {ca:.0f}% -- over-provisioned')
        hr=True
    elif ca < 40:
        click.echo(click.style('    [MEDIUM] ',fg='yellow')+f'CPU averages {ca:.0f}% -- consider downsizing')
        hr=True
    if ma < 20:
        click.echo(click.style('    [HIGH] ',fg='red')+f'Memory averages {ma:.0f}% -- over-provisioned')
        hr=True
    if 'gpu_util' in metrics and ga < 20:
        click.echo(click.style('    [HIGH] ',fg='red')+f'GPU averages {ga:.0f}% -- batch or use CPU')
        hr=True
    if not hr:
        click.echo(click.style('    [OK] ',fg='green')+'Instance is well-utilized')
    click.echo()

# =============================================================================
# CLOUD COMMANDS
# =============================================================================
@cli.group()
@click.pass_context
def cloud(ctx):
    """Cloud provider monitoring commands.

    Manage cloud metric collection from AWS, Azure, and GCP.
    """
    ctx.ensure_object(dict)


@cloud.command('status')
@click.pass_context
def cloud_status(ctx):
    """Check cloud collector connectivity and authentication."""
    config = ctx.obj.get('config', {})
    cloud_cfg = config.get('collectors', {}).get('cloud', {})

    if not cloud_cfg:
        click.echo("No cloud collectors configured in nomad.toml.")
        click.echo("See: nomad/config/cloud_collectors.toml.example")
        return

    click.echo(f"{'Provider':<12} {'Enabled':<10} {'Auth':<12} {'Instances':<12}")
    click.echo("-" * 46)

    for name, pcfg in cloud_cfg.items():
        if not isinstance(pcfg, dict):
            continue

        enabled = pcfg.get('enabled', False)
        enabled_str = "yes" if enabled else "no"

        if not enabled:
            click.echo(f"{name:<12} {enabled_str:<10} {'--':<12} {'--':<12}")
            continue

        if name == 'aws':
            if not HAS_AWS_COLLECTOR:
                click.echo(
                    f"{name:<12} {enabled_str:<10} "
                    f"{'no SDK':<12} {'--':<12}"
                )
                click.echo("  Install with: pip install boto3")
                continue
            try:
                db_path = get_db_path(config)
                collector = AWSCollector(pcfg, db_path=str(db_path))
                collector._ensure_authenticated()
                instances = collector._list_instances()
                click.echo(
                    f"{name:<12} {enabled_str:<10} "
                    f"{click.style('OK', fg='green'):<12} {len(instances):<12}"
                )
            except Exception as exc:
                err = str(exc)[:30]
                click.echo(
                    f"{name:<12} {enabled_str:<10} "
                    f"{click.style('FAIL', fg='red'):<12} {err}"
                )
        else:
            click.echo(
                f"{name:<12} {enabled_str:<10} "
                f"{'planned':<12} {'--':<12}"
            )


@cloud.command('instances')
@click.option('--provider', '-p', type=click.Choice(['aws']), help='Specific provider')
@click.pass_context
def cloud_instances(ctx, provider):
    """List discovered cloud instances."""
    config = ctx.obj.get('config', {})
    cloud_cfg = config.get('collectors', {}).get('cloud', {})

    if not cloud_cfg:
        click.echo("No cloud collectors configured.")
        return

    providers = (
        [(provider, cloud_cfg[provider])]
        if provider and provider in cloud_cfg
        else [
            (n, c) for n, c in cloud_cfg.items()
            if isinstance(c, dict) and c.get('enabled', False)
        ]
    )

    for name, pcfg in providers:
        if name == 'aws':
            if not HAS_AWS_COLLECTOR:
                click.echo(f"{name.upper()} -- boto3 not installed")
                continue
            try:
                db_path = get_db_path(config)
                collector = AWSCollector(pcfg, db_path=str(db_path))
                collector._ensure_authenticated()
                instances = collector._list_instances()

                click.echo(f"\n{name.upper()} -- {len(instances)} instance(s)")
                click.echo(f"{'Name':<30} {'Type':<16} {'AZ':<16} {'State':<10}")
                click.echo("-" * 72)

                for inst in instances:
                    click.echo(
                        f"{inst.get('name', inst['instance_id']):<30} "
                        f"{inst.get('instance_type', '--'):<16} "
                        f"{inst.get('availability_zone', '--'):<16} "
                        f"{inst.get('state', '--'):<10}"
                    )
            except Exception as exc:
                click.echo(f"\n{name.upper()} -- ERROR: {exc}")
        else:
            click.echo(f"\n{name.upper()} -- not yet implemented")


@cloud.command('collect')
@click.option('--provider', '-p', type=click.Choice(['aws']), help='Specific provider')
@click.option('--db', type=click.Path(), help='Database path override')
@click.pass_context
def cloud_collect(ctx, provider, db):
    """Run cloud collectors once."""
    config = ctx.obj.get('config', {})
    cloud_cfg = config.get('collectors', {}).get('cloud', {})

    if db:
        db_path = Path(db)
    else:
        db_path = get_db_path(config)

    if not cloud_cfg:
        click.echo("No cloud collectors configured.")
        return

    providers = (
        [(provider, cloud_cfg[provider])]
        if provider and provider in cloud_cfg
        else [
            (n, c) for n, c in cloud_cfg.items()
            if isinstance(c, dict) and c.get('enabled', False)
        ]
    )

    for name, pcfg in providers:
        click.echo(f"Collecting from {name}... ", nl=False)
        if name == 'aws':
            if not HAS_AWS_COLLECTOR:
                click.echo(click.style("MISSING boto3", fg='yellow'))
                continue
            try:
                collector = AWSCollector(pcfg, db_path=str(db_path))
                result = collector.run()
                if result.success:
                    click.echo(click.style(
                        f"OK ({result.records_collected} metrics "
                        f"in {result.duration_seconds:.1f}s)",
                        fg='green',
                    ))
                else:
                    click.echo(click.style(
                        f"FAILED: {result.error_message}", fg='red'
                    ))
            except Exception as exc:
                click.echo(click.style(f"ERROR: {exc}", fg='red'))
        else:
            click.echo(click.style("not yet implemented", fg='yellow'))
