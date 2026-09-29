#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Dashboard Server - Integrated Version
Connects to TOML config, NØMAÐ database, and falls back to demo data.
"""

import importlib.metadata
import http.server
import urllib.parse
import json
import logging
import math
import random
import socketserver
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

# Try to import toml (fall back to tomllib in Python 3.11+)
try:
    import tomllib
except ImportError:
    try:
        import toml as tomllib
    except ImportError:
        tomllib = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration Loading
# ============================================================================

DEFAULT_CONFIG = {
    "general": {
        "cluster_name": "demo-cluster",
        "data_dir": "/var/lib/nomad",
    },
    "clusters": {},  # Will be populated from TOML or auto-detected
    "dashboard": {
        "host": "localhost",
        "port": 8050,
    }
}

def find_config_file() -> Optional[Path]:
    """The config nomad itself would use (nomad.config.find_config), then the
    older places this dashboard also looked: ~/nomad/nomad.toml, ./nomad.toml."""
    from nomad.config import find_config
    found = find_config()
    if found:
        return found
    for path in (Path.home() / "nomad" / "nomad.toml", Path("nomad.toml")):
        if path.exists():
            return path
    return None


def load_config(config_path: Optional[Path] = None) -> dict:
    """Load configuration from TOML file, over this dashboard's defaults."""
    import copy
    from nomad.config import read_toml
    config = copy.deepcopy(DEFAULT_CONFIG)

    if config_path is None:
        config_path = find_config_file()
    if config_path is not None:
        config_path = Path(config_path)
    # Skip if path is a directory (not a config file)
    if config_path is not None and config_path.is_dir():
        logger.debug(f"Skipping directory: {config_path}")
        return config

    if config_path is not None and config_path.exists():
        logger.info(f"Loading config from {config_path}")
        try:
            user_config = read_toml(config_path)
            # Merge with defaults
            for key, value in user_config.items():
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
    else:
        logger.info("No config file found, using defaults")

    return config


# ============================================================================
# Database Connection
# ============================================================================

def find_database() -> Optional[Path]:
    """Search for NØMAÐ database."""
    search_paths = [
        Path("/var/lib/nomad/nomad.db"),
        Path.home() / "nomad" / "nomad.db",
        Path.home() / ".config" / "nomad" / "nomad.db",
        Path("nomad.db"),
        # Demo database created by 'nomad demo'
        Path.home() / "nomad_demo.db",
    ]
    for path in search_paths:
        if path.exists():
            return path
    return None


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    """Create database connection with row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def row_get(row, key, default=None):
    """Safely get a value from sqlite3.Row (which doesn't have .get())."""
    try:
        val = row[key]
        return val if val is not None else default
    except (IndexError, KeyError):
        return default


# ============================================================================
# Data Loaders - Real Data from Database
# ============================================================================

def load_clusters_from_db(db_path: Path) -> dict:
    """
    Auto-detect clusters from node_state or node_status tables.
    Groups nodes by partition or cluster field.
    """
    clusters = {}

    try:
        conn = get_db_connection(db_path)

        # Try NØMAÐ's node_state table first
        try:
            rows = conn.execute("""
                SELECT DISTINCT ns.node_name, ns.partitions, ns.gres,
                       COALESCE(ns.cluster, 'default') as cluster
                FROM node_state ns
                INNER JOIN (
                    SELECT cluster, MAX(timestamp) as max_ts
                    FROM node_state
                    GROUP BY cluster
                ) latest ON ns.cluster = latest.cluster
                    AND ns.timestamp = latest.max_ts
            """).fetchall()

            if rows:
                # Group by cluster, then by partition
                cluster_data = defaultdict(lambda: defaultdict(list))
                gpu_nodes = set()

                for row in rows:
                    node = row['node_name']
                    cluster = row['cluster'] or 'default'
                    partitions = row['partitions'] or 'default'
                    # Strip SLURM asterisks and assign node to ALL its partitions
                    for part in partitions.split(','):
                        part = part.strip().rstrip('*')
                        if part:
                            cluster_data[cluster][part].append(node)

                    if row['gres'] and 'gpu' in row['gres'].lower():
                        gpu_nodes.add(node)

                for cluster_name, part_map in cluster_data.items():
                    all_nodes_set = set()
                    for p_nodes in part_map.values():
                        all_nodes_set.update(p_nodes)
                    all_nodes = sorted(all_nodes_set)
                    cluster_id = cluster_name.lower().replace(' ', '-')
                    clusters[cluster_id] = {
                        "name": cluster_name,
                        "description": f"{len(all_nodes)}-node cluster",
                        "nodes": all_nodes,
                        "gpu_nodes": [n for n in all_nodes if n in gpu_nodes],
                        "type": "gpu" if all_nodes and all(n in gpu_nodes for n in all_nodes) else "cpu",
                        "partitions": {p: sorted(ns) for p, ns in part_map.items()},
                    }

                # Filter partitions using sync_sites metadata
                try:
                    sync_rows = conn.execute(
                        "SELECT name, partitions, cluster_type"
                        " FROM sync_sites"
                    ).fetchall()
                    sync_meta = {
                        r[0]: {
                            "partitions": [
                                p.strip() for p in
                                (r[1] or "").split(",")
                                if p.strip()
                            ],
                            "type": r[2] or "hpc",
                        }
                        for r in sync_rows
                    }
                    # Filter each cluster's partitions
                    for cid, cdata in list(
                            clusters.items()):
                        cname = cdata.get("name", cid)
                        meta = (sync_meta.get(cname)
                                or sync_meta.get(cid))
                        if meta and meta["partitions"]:
                            allowed = set(
                                meta["partitions"])
                            filtered = {
                                p: ns for p, ns
                                in cdata.get(
                                    "partitions",
                                    {}).items()
                                if p in allowed
                            }
                            cdata["partitions"] =\
                                filtered
                            # Update node list to
                            # only include filtered
                            all_ns = set()
                            for pns in filtered.values():
                                all_ns.update(pns)
                            cdata["nodes"] = sorted(
                                all_ns)
                        if meta:
                            cdata["type"] = meta[
                                "type"]
                except Exception:
                    pass  # No sync_sites table

                # Also detect non-SLURM clusters from source_site
                # (e.g., spiderweb has no node_state but has data
                # in other tables with source_site column)
                try:
                    site_tables = [
                        "filesystems", "iostat_cpu",
                        "interactive_sessions"]
                    known_clusters = set(clusters.keys())
                    for st in site_tables:
                        try:
                            sites = conn.execute(
                                f"SELECT DISTINCT source_site"
                                f" FROM {st}"
                                f" WHERE source_site IS NOT NULL"
                            ).fetchall()
                            for (site,) in sites:
                                site_id = site.lower().replace(
                                    " ", "-")
                                if site_id not in known_clusters:
                                    clusters[site_id] = {
                                        "name": site,
                                        "description":
                                            "Workstation/server",
                                        "nodes": [],
                                        "gpu_nodes": [],
                                        "type": "workstation",
                                        "partitions": {},
                                    }
                                    known_clusters.add(site_id)
                        except Exception:
                            pass
                except Exception:
                    pass

                conn.close()
                return clusters

        except sqlite3.OperationalError:
            pass  # Table doesn't exist

        # Try cluster_monitor's node_status table
        try:
            rows = conn.execute("""
                SELECT DISTINCT cluster, node_name
                FROM node_status
                WHERE timestamp = (SELECT MAX(timestamp) FROM node_status)
            """).fetchall()

            if rows:
                cluster_nodes = defaultdict(list)
                for row in rows:
                    cluster_nodes[row['cluster']].append(row['node_name'])

                for cluster_name, nodes in cluster_nodes.items():
                    cluster_id = cluster_name.lower().replace(' ', '-')
                    # Detect GPU nodes by name pattern
                    gpu_nodes = [n for n in nodes if 'gpu' in n.lower() or n.startswith('gn')]
                    clusters[cluster_id] = {
                        "name": cluster_name,
                        "description": f"{len(nodes)}-node cluster",
                        "nodes": sorted(nodes),
                        "gpu_nodes": gpu_nodes,
                        "type": "hybrid" if gpu_nodes else "cpu"
                    }

        except sqlite3.OperationalError:
            pass

        conn.close()

    except Exception as e:
        logger.warning(f"Failed to load clusters from database: {e}")

    # Fallback: Try simulator's simple nodes table
    if not clusters:
        try:
            conn = get_db_connection(db_path)
            rows = conn.execute("""
                SELECT hostname, cluster, partition, status, cpu_count, gpu_count, memory_mb
                FROM nodes
            """).fetchall()
            if rows:

                # Group by cluster
                cluster_nodes = defaultdict(list)
                gpu_nodes = set()
                cluster_partitions = defaultdict(set)

                for row in rows:
                    node = row["hostname"]
                    cluster_name = row["cluster"] or "default"
                    partitions = row["partition"] or ""

                    cluster_nodes[cluster_name].append(node)
                    cluster_partitions[cluster_name].update(partitions.split(","))

                    if row["gpu_count"] and row["gpu_count"] > 0:
                        gpu_nodes.add(node)

                # Build partition -> nodes mapping
                partition_node_map = defaultdict(lambda: defaultdict(list))
                for row in rows:
                    node = row["hostname"]
                    cluster_name = row["cluster"] or "default"
                    partitions = row["partition"] or "default"
                    # Assign node to ALL its partitions, strip asterisks
                    for part in partitions.split(","):
                        part = part.strip().rstrip("*")
                        if part:
                            partition_node_map[cluster_name][part].append(node)

                for cluster_name, nodes in cluster_nodes.items():
                    cluster_id = cluster_name.lower().replace(" ", "-")
                    part_list = sorted(p for p in cluster_partitions[cluster_name] if p)
                    part_map = {p: sorted(ns) for p, ns in partition_node_map[cluster_name].items()}
                    clusters[cluster_id] = {
                        "name": cluster_name,
                        "description": f"{len(nodes)}-node cluster",
                        "nodes": sorted(nodes),
                        "gpu_nodes": [n for n in nodes if n in gpu_nodes],
                        "type": "gpu" if any(n in gpu_nodes for n in nodes) else "cpu",
                        "partitions": part_map,
                    }

                logger.info("Loaded clusters from simulator nodes table")
            conn.close()
        except Exception as e:
            logger.debug(f"No simulator nodes table: {e}")

    return clusters


def load_node_data_from_db(db_path: Path, clusters: dict) -> dict:
    """Load real node statistics from database."""
    nodes = {}

    try:
        conn = get_db_connection(db_path)

        # Try NØMAÐ's node_state table
        try:
            rows = conn.execute("""
                SELECT 
                    ns.node_name, ns.state, ns.cpus_total, ns.cpus_alloc,
                    ns.cpu_load,
                    ns.memory_total_mb, ns.memory_alloc_mb,
                    ns.memory_free_mb,
                    ns.cpu_alloc_percent, ns.memory_alloc_percent,
                    ns.partitions, ns.reason, ns.gres, ns.is_healthy,
                    COALESCE(ns.cluster, 'default') as cluster
                FROM node_state ns
                INNER JOIN (
                    SELECT cluster, MAX(timestamp) as max_ts
                    FROM node_state
                    GROUP BY cluster
                ) latest ON ns.cluster = latest.cluster
                    AND ns.timestamp = latest.max_ts
            """).fetchall()

            if rows:
                # Get per-partition running/pending from queue_state
                # (more accurate than jobs table for live counts)
                queue_running = {}
                try:
                    qrows = conn.execute("""
                        SELECT qs.partition,
                               qs.running_jobs,
                               qs.pending_jobs
                        FROM queue_state qs
                        INNER JOIN (
                            SELECT partition,
                                   MAX(timestamp) as mt
                            FROM queue_state
                            GROUP BY partition
                        ) latest
                        ON qs.partition = latest.partition
                           AND qs.timestamp = latest.mt
                    """).fetchall()
                    for qr in qrows:
                        queue_running[qr["partition"]] = {
                            "running": qr["running_jobs"],
                            "pending": qr["pending_jobs"],
                        }
                except Exception:
                    pass

                # Get job statistics per node from jobs table
                job_stats = {}
                try:
                    job_rows = conn.execute("""
                        SELECT 
                            node_list,
                            state,
                            COUNT(*) as count
                        FROM jobs
                        GROUP BY node_list, state
                    """).fetchall()

                    for row in job_rows:
                        if row['node_list']:
                            for node in row['node_list'].split(','):
                                node = node.strip()
                                if node not in job_stats:
                                    job_stats[node] = {'success': 0, 'failed': 0, 'running': 0, 'pending': 0}
                                if row['state'] == 'COMPLETED':
                                    job_stats[node]['success'] += row['count']
                                elif row['state'] in ('FAILED', 'TIMEOUT', 'OUT_OF_MEMORY'):
                                    job_stats[node]['failed'] += row['count']
                                elif row['state'] == 'RUNNING':
                                    job_stats[node]['running'] += row['count']
                                elif row['state'] == 'PENDING':
                                    job_stats[node]['pending'] += row['count']
                except:
                    pass

                for row in rows:
                    node_name = row['node_name']

                    # Find which cluster this node belongs to
                    cluster_id = None
                    for cid, cluster in clusters.items():
                        if node_name in cluster['nodes']:
                            cluster_id = cid
                            break

                    if not cluster_id:
                        continue

                    is_down = not row['is_healthy'] or 'DOWN' in (row['state'] or '').upper()

                    # Calculate job stats
                    stats = job_stats.get(node_name, {'success': 0, 'failed': 0})
                    total_jobs = stats['success'] + stats['failed']
                    success_rate = stats['success'] / total_jobs if total_jobs > 0 else 1.0

                    has_gpu = row['gres'] and 'gpu' in row['gres'].lower()

                    # Get top users for this node from job_accounting or jobs table
                    top_users = []
                    try:
                        user_rows = conn.execute("""
                            SELECT username, COUNT(*) as job_count
                            FROM job_accounting
                            WHERE node_list LIKE ?
                            AND (end_time > datetime('now', '-1 day')
                                 OR end_time IS NULL)
                            GROUP BY username
                            ORDER BY job_count DESC
                            LIMIT 5
                        """, (f'%{node_name}%',)).fetchall()
                        if user_rows:
                            top_users = [{"user": r['username'], "jobs": r['job_count']} for r in user_rows]
                    except:
                        pass
                    if not top_users:
                        try:
                            user_rows = conn.execute("""
                                SELECT user_name as username, COUNT(*) as job_count
                                FROM jobs
                                WHERE node_list LIKE ?
                                AND (state = 'RUNNING' OR state = 'PENDING'
                                     OR end_time > datetime('now', '-1 day'))
                                GROUP BY user_name
                                ORDER BY job_count DESC
                                LIMIT 5
                            """, (f'%{node_name}%',)).fetchall()
                            if user_rows:
                                top_users = [{"user": r['username'], "jobs": r['job_count']} for r in user_rows]
                        except:
                            pass

                    nodes[node_name] = {
                        "name": node_name,
                        "cluster": cluster_id,
                        "status": "down" if is_down else "online",
                        "slurm_state": row['state'],
                        "success_rate": success_rate,
                        "jobs_today": total_jobs,
                        "jobs_running": stats.get('running', 0),
                        "jobs_pending": stats.get('pending', 0),
                        "jobs_success": stats['success'],
                        "jobs_failed": stats['failed'],
                        "failures": {},  # TODO: aggregate failure types
                        "top_users": top_users,
                        "has_gpu": has_gpu,
                        "gpu_util": 0,  # Will be updated from gpu_stats
                        "gpu_name": row['gres'] if has_gpu else None,
                        "cpu_util": int(row['cpu_alloc_percent'] or 0),
                        "mem_util": int(((row["memory_total_mb"] - row["memory_free_mb"]) / row["memory_total_mb"] * 100) if row["memory_total_mb"] and row["memory_total_mb"] > 0 else 0),
                        "load_avg": round(float(row['cpu_load'] or 0), 2),
                        "drain_reason": row['reason'],
                        "last_seen": datetime.now().isoformat()
                    }

                # Get GPU stats mapped to nodes
                try:
                    gpu_rows = conn.execute("""
                        SELECT node_name, gpu_index, gpu_name,
                               gpu_util_percent, memory_util_percent,
                               memory_used_mb, memory_total_mb,
                               temperature_c, power_draw_w,
                               power_limit_w, compute_processes
                        FROM gpu_stats
                        WHERE timestamp >= (
                            SELECT datetime(MAX(timestamp), '-10 minutes')
                            FROM gpu_stats
                        )
                        ORDER BY node_name, gpu_index
                    """).fetchall()
                    for gpu_row in gpu_rows:
                        node = gpu_row['node_name'] or 'unknown'
                        if node in nodes:
                            if 'gpus' not in nodes[node]:
                                nodes[node]['gpus'] = []
                            nodes[node]['gpus'].append({
                                'index': gpu_row['gpu_index'],
                                'name': gpu_row['gpu_name'],
                                'util_pct': gpu_row['gpu_util_percent'],
                                'mem_util_pct': gpu_row['memory_util_percent'],
                                'mem_used_mb': gpu_row['memory_used_mb'],
                                'mem_total_mb': gpu_row['memory_total_mb'],
                                'temp_c': gpu_row['temperature_c'],
                                'power_w': gpu_row['power_draw_w'],


                            })
                            nodes[node]['gpu_util'] = int(
                                sum(g['util_pct'] for g in nodes[node]['gpus']) / len(nodes[node]['gpus'])
                            )
                            # Use actual GPU model name from nvidia-smi/DCGM
                            nodes[node]['gpu_name'] = nodes[node]['gpus'][0]['name']
                            # Aggregate real_util and workload at node level
                            nodes[node]['gpu_real_util'] = None
                            # Dominant workload class across GPUs (most common non-idle)
                            nodes[node]['gpu_workload'] = None
                            nodes[node]['gpu_data_source'] = 'nvidia-smi'
                            # Worst health status across GPUs
                            rank = {'OK': 0, 'WARN': 1, 'HOT': 2, 'CRIT': 3}
                            nodes[node]['gpu_health'] = 'OK'
                except:
                    pass

                conn.close()
                return nodes

        except sqlite3.OperationalError:
            pass

        # Try cluster_monitor's node_status table
        try:
            rows = conn.execute("""
                SELECT cluster, node_name, status, slurm_state, is_available
                FROM node_status
                WHERE timestamp = (SELECT MAX(timestamp) FROM node_status)
            """).fetchall()

            if rows:
                for row in rows:
                    node_name = row['node_name']
                    cluster_id = row['cluster'].lower().replace(' ', '-')

                    # Status can be 'ok', 'online', or other values
                    is_down = not row['is_available'] or row['status'] not in ('ok', 'online', 'up')

                    # Detect GPU by node name pattern
                    has_gpu = any(x in node_name.lower() for x in ['gpu']) or \
                              node_name in ('node51', 'node52', 'node53') or \
                              (node_name.startswith('arachne') and node_name[-2:] in ['04', '05', '06'])

                    nodes[node_name] = {
                        "name": node_name,
                        "cluster": cluster_id,
                        "status": "down" if is_down else "online",
                        "slurm_state": row['slurm_state'],
                        "success_rate": 0.9 if not is_down else 0,  # Placeholder
                        "jobs_today": 0,
                        "jobs_success": 0,
                        "jobs_failed": 0,
                        "failures": {},
                        "top_users": [],
                        "has_gpu": has_gpu,
                        "gpu_util": 0,
                        "gpu_name": "NVIDIA RTX 6000 Ada" if has_gpu else None,
                        "cpu_util": random.randint(40, 90) if not is_down else 0,
                        "mem_util": random.randint(30, 80) if not is_down else 0,
                        "load_avg": round(random.uniform(1, 12), 2) if not is_down else 0,
                        "last_seen": datetime.now().isoformat()
                    }

        except sqlite3.OperationalError:
            pass

        # Fallback: Try simulator's simple nodes table
        if not nodes:
            try:
                rows = conn.execute("""
                    SELECT hostname, cluster, partition, status, cpu_count, gpu_count, memory_mb
                    FROM nodes
                """).fetchall()

                if rows:
                    # Get job statistics per node
                    job_stats = {}
                    try:
                        job_rows = conn.execute("""
                            SELECT 
                                node_list, state, failure_reason,
                                COUNT(*) as count
                            FROM jobs
                            GROUP BY node_list, state, failure_reason
                        """).fetchall()

                        for row in job_rows:
                            if row['node_list']:
                                node = row['node_list'].strip()
                                if node not in job_stats:
                                    job_stats[node] = {'success': 0, 'failed': 0, 'running': 0, 'pending': 0, 'failures': {}}
                                if row['state'] == 'COMPLETED':
                                    job_stats[node]['success'] += row['count']
                                elif row['state'] == 'RUNNING':
                                    job_stats[node]['running'] += row['count']
                                elif row['state'] == 'PENDING':
                                    job_stats[node]['pending'] += row['count']
                                else:
                                    job_stats[node]['failed'] += row['count']
                                    # Track failure types
                                    fr = row_get(row, 'failure_reason', 3)
                                    fr_names = {1:'timeout', 2:'cancelled', 3:'failed', 4:'oom', 5:'segfault', 6:'node_fail', 7:'dependency'}
                                    fr_name = fr_names.get(fr, 'other')
                                    job_stats[node]['failures'][fr_name] = job_stats[node]['failures'].get(fr_name, 0) + row['count']
                    except:
                        pass

                    for row in rows:
                        node_name = row['hostname']
                        partitions = row['partition'] or 'default'
                        primary_partition = partitions.split(',')[0]
                        cluster_id = (row["cluster"] or "default").lower().replace(' ', '-')

                        has_gpu = row['gpu_count'] and row['gpu_count'] > 0
                        is_down = row['status'] and row['status'].upper() in ('DOWN', 'DRAIN', 'FAIL')

                        # Get job stats
                        stats = job_stats.get(node_name, {'success': 0, 'failed': 0, 'failures': {}})
                        total_jobs = stats['success'] + stats['failed']
                        success_rate = stats['success'] / total_jobs if total_jobs > 0 else 1.0

                        nodes[node_name] = {
                            "name": node_name,
                            "cluster": cluster_id,
                            "status": "down" if is_down else "online",
                            "slurm_state": row['status'],
                            "success_rate": success_rate,
                            "jobs_today": total_jobs,
                            "jobs_success": stats['success'],
                            "jobs_failed": stats['failed'],
                            "failures": stats['failures'],
                            "top_users": [],
                            "has_gpu": has_gpu,
                            "gpu_util": random.randint(40, 95) if has_gpu and not is_down else 0,
                            "gpu_name": f"GPU x{row['gpu_count']}" if has_gpu else None,
                            "cpu_util": random.randint(30, 90) if not is_down else 0,
                            "mem_util": random.randint(20, 80) if not is_down else 0,
                            "load_avg": round(random.uniform(0.5, 16), 2) if not is_down else 0,
                            "last_seen": datetime.now().isoformat()
                        }

                    logger.info("Loaded nodes from simulator nodes table")
            except Exception as e:
                logger.debug(f"No simulator nodes table: {e}")

        conn.close()

    except Exception as e:
        logger.warning(f"Failed to load node data from database: {e}")

    return nodes


def load_jobs_from_db(db_path: Path, limit: int = 5000) -> list:
    """Load job data for network visualization with all available features."""
    jobs = []

    try:
        conn = get_db_connection(db_path)

        # Get comprehensive job data joining multiple tables
        try:
            rows = conn.execute("""
                SELECT 
                    j.job_id,
                    j.user_name,
                    j.state,
                    j.partition,
                    j.source_site,
                    j.runtime_seconds,
                    j.wait_time_seconds,
                    j.req_cpus,
                    j.req_mem_mb,
                    j.req_time_seconds,
                    j.failure_reason,
                    j.exit_code,
                    j.exit_signal,
                    js.total_nfs_write_gb,
                    js.total_local_write_gb,
                    js.avg_io_wait_percent,
                    js.peak_cpu_percent,
                    js.peak_memory_gb,
                    js.avg_cpu_percent,
                    js.avg_memory_gb,
                    js.health_score,
                    js.nfs_ratio
                FROM jobs j
                LEFT JOIN job_summary js ON j.job_id = js.job_id
                WHERE j.end_time IS NOT NULL
                ORDER BY j.end_time DESC
                LIMIT ?
            """, (limit,)).fetchall()

            if rows:
                # Also get aggregated io_samples per job
                io_data = {}
                try:
                    io_rows = conn.execute("""
                        SELECT 
                            job_id,
                            MAX(total_write_bytes) as max_write_bytes,
                            MAX(total_read_bytes) as max_read_bytes,
                            AVG(nfs_ratio) as avg_nfs_ratio
                        FROM job_io_samples
                        GROUP BY job_id
                    """).fetchall()
                    for io_row in io_rows:
                        io_data[io_row['job_id']] = {
                            'max_write_bytes': io_row['max_write_bytes'] or 0,
                            'max_read_bytes': io_row['max_read_bytes'] or 0,
                            'avg_nfs_ratio': io_row['avg_nfs_ratio'] or 0
                        }
                except:
                    pass

                for row in rows:
                    job_id = row['job_id']
                    io_info = io_data.get(job_id, {})

                    # Get failure_reason from job if available, otherwise compute from state
                    failure_reason = row_get(row, 'failure_reason', 0)
                    if failure_reason is None:
                        # Compute from state if not set
                        state = row['state'] or ''
                        if state == 'COMPLETED':
                            failure_reason = 0
                        elif state == 'TIMEOUT':
                            failure_reason = 1
                        elif state in ('CANCELLED', 'PREEMPTED'):
                            failure_reason = 2
                        elif state == 'OUT_OF_MEMORY':
                            failure_reason = 4
                        elif state == 'NODE_FAIL':
                            failure_reason = 6
                        else:
                            failure_reason = 3  # Generic failure

                    jobs.append({
                        "job_id": job_id,
                        "user_name": row['user_name'] or '—',
                        "source_site": row_get(row, 'source_site') or '—',
                        "state": row['state'],
                        "partition": row['partition'],
                        "success": row['state'] == 'COMPLETED',
                        "failure_reason": failure_reason,
                        "exit_code": row_get(row, 'exit_code'),
                        "exit_signal": row_get(row, 'exit_signal'),
                        # Time features
                        "runtime_sec": row['runtime_seconds'] or 0,
                        "wait_time_sec": row['wait_time_seconds'] or 0,
                        # Resource requests
                        "req_cpus": row['req_cpus'] or 1,
                        "req_mem_mb": row['req_mem_mb'] or 0,
                        "req_time_sec": row['req_time_seconds'] or 0,
                        # I/O features
                        "nfs_write_gb": row['total_nfs_write_gb'] or 0,
                        "local_write_gb": row['total_local_write_gb'] or 0,
                        "io_wait_pct": row['avg_io_wait_percent'] or 0,
                        "total_write_mb": io_info.get('max_write_bytes', 0) / (1024*1024),
                        "total_read_mb": io_info.get('max_read_bytes', 0) / (1024*1024),
                        # CPU/Memory features
                        "peak_cpu_pct": row['peak_cpu_percent'] or 0,
                        "peak_mem_gb": row['peak_memory_gb'] or 0,
                        "avg_cpu_pct": row['avg_cpu_percent'] or 0,
                        "avg_mem_gb": row['avg_memory_gb'] or 0,
                        # Derived
                        "health_score": row['health_score'] or 0,
                        "nfs_ratio": row['nfs_ratio'] or 0,
                        # Efficiency (runtime / requested)
                        "time_efficiency": (row['runtime_seconds'] or 0) / max(row['req_time_seconds'] or 1, 1),
                    })
                conn.close()
                return jobs

        except sqlite3.OperationalError as e:
            logger.debug(f"Job query failed: {e}")

        # Fallback: Try jobs table directly
        try:
            rows = conn.execute("""
                SELECT job_id, state, partition, runtime_seconds, wait_time_seconds,
                       exit_code, exit_signal, failure_reason
                FROM jobs
                WHERE end_time IS NOT NULL
                ORDER BY end_time DESC
                LIMIT ?
            """, (limit,)).fetchall()

            if rows:
                for row in rows:
                    # Get failure_reason from job if available, otherwise compute from state
                    failure_reason = row_get(row, 'failure_reason', 0)
                    if failure_reason is None:
                        state = row['state'] or ''
                        if state == 'COMPLETED':
                            failure_reason = 0
                        elif state == 'TIMEOUT':
                            failure_reason = 1
                        elif state in ('CANCELLED', 'PREEMPTED'):
                            failure_reason = 2
                        elif state == 'OUT_OF_MEMORY':
                            failure_reason = 4
                        elif state == 'NODE_FAIL':
                            failure_reason = 6
                        else:
                            failure_reason = 3

                    jobs.append({
                        "job_id": row['job_id'],
                        "state": row['state'],
                        "partition": row['partition'],
                        "success": row['state'] == 'COMPLETED',
                        "failure_reason": failure_reason,
                        "exit_code": row_get(row, 'exit_code'),
                        "exit_signal": row_get(row, 'exit_signal'),
                        "runtime_sec": row['runtime_seconds'] or 0,
                        "wait_time_sec": row['wait_time_seconds'] or 0,
                        "nfs_write_gb": 0,
                        "local_write_gb": 0,
                        "io_wait_pct": 0,
                        "total_write_mb": 0,
                        "req_cpus": 1,
                    })

        except sqlite3.OperationalError:
            pass

        conn.close()

    except Exception as e:
        logger.warning(f"Failed to load jobs from database: {e}")

    return jobs


def compute_feature_stats(jobs: list) -> dict:
    """Compute statistics for each numeric feature to help users choose axes."""
    if not jobs:
        return {}

    # Identify numeric features
    numeric_features = []
    sample_job = jobs[0]
    for key, value in sample_job.items():
        if isinstance(value, (int, float)) and key not in ('job_id', 'success'):
            numeric_features.append(key)

    stats = {}
    for feature in numeric_features:
        values = [j.get(feature, 0) or 0 for j in jobs]
        if not values:
            continue

        mean = sum(values) / len(values)
        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val

        # Variance and standard deviation
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        std = variance ** 0.5

        # Coefficient of variation (normalized measure of spread)
        cv = (std / mean * 100) if mean != 0 else 0

        # Count non-zero values
        non_zero = sum(1 for v in values if v != 0)
        non_zero_pct = non_zero / len(values) * 100

        stats[feature] = {
            "mean": round(mean, 3),
            "std": round(std, 3),
            "min": round(min_val, 3),
            "max": round(max_val, 3),
            "range": round(range_val, 3),
            "cv": round(cv, 1),  # Coefficient of variation
            "non_zero_pct": round(non_zero_pct, 1),
            "n": len(values)
        }

    return stats


def suggest_best_axes(feature_stats: dict, n: int = 3) -> list:
    """Suggest the best features for visualization based on variance and coverage."""
    if not feature_stats:
        return ["runtime_sec", "wait_time_sec", "total_write_mb"]

    # Score features by: high CV + high non-zero percentage
    scored = []
    for feature, stats in feature_stats.items():
        # Skip features with no variation or all zeros
        if stats['range'] == 0 or stats['non_zero_pct'] < 10:
            continue

        # Score = CV * (non_zero_pct / 100)
        score = stats['cv'] * (stats['non_zero_pct'] / 100)
        scored.append((feature, score, stats))

    # Sort by score descending
    scored.sort(key=lambda x: -x[1])

    # Return top N feature names
    return [f[0] for f in scored[:n]]


def compute_correlation_matrix(jobs: list, features: list = None) -> dict:
    """
    Compute Pearson correlation matrix between numeric features.
    Returns dict with 'features', 'matrix', and 'high_correlations' for warnings.
    """
    if not jobs:
        return {"features": [], "matrix": [], "high_correlations": []}

    # Get numeric features if not specified
    if features is None:
        sample_job = jobs[0]
        features = [k for k, v in sample_job.items()
                   if isinstance(v, (int, float)) and k not in ('job_id', 'success')]

    # Extract values for each feature
    data = {}
    for f in features:
        values = [j.get(f, 0) or 0 for j in jobs]
        # Skip constant features
        if max(values) == min(values):
            continue
        data[f] = values

    valid_features = list(data.keys())
    n = len(valid_features)

    if n == 0:
        return {"features": [], "matrix": [], "high_correlations": []}

    # Compute means and standard deviations
    means = {f: sum(data[f]) / len(data[f]) for f in valid_features}
    stds = {}
    for f in valid_features:
        variance = sum((x - means[f]) ** 2 for x in data[f]) / len(data[f])
        stds[f] = variance ** 0.5 if variance > 0 else 1

    # Compute correlation matrix
    matrix = []
    high_correlations = []

    for i, f1 in enumerate(valid_features):
        row = []
        for j, f2 in enumerate(valid_features):
            if i == j:
                row.append(1.0)
            elif j < i:
                # Already computed, mirror it
                row.append(matrix[j][i])
            else:
                # Compute Pearson correlation
                n_samples = len(data[f1])
                cov = sum((data[f1][k] - means[f1]) * (data[f2][k] - means[f2])
                         for k in range(n_samples)) / n_samples
                r = cov / (stds[f1] * stds[f2]) if (stds[f1] * stds[f2]) > 0 else 0
                r = max(-1, min(1, r))  # Clamp to [-1, 1]
                row.append(round(r, 3))

                # Track high correlations for warnings
                if abs(r) >= 0.7 and i != j:
                    high_correlations.append({
                        "feature1": f1,
                        "feature2": f2,
                        "correlation": round(r, 3),
                        "strength": "strong" if abs(r) >= 0.85 else "moderate"
                    })
        matrix.append(row)

    return {
        "features": valid_features,
        "matrix": matrix,
        "high_correlations": high_correlations
    }


def suggest_decorrelated_axes(feature_stats: dict, correlation_data: dict, n: int = 3) -> list:
    """
    Suggest N features that are both high-variance AND decorrelated from each other.
    Uses a greedy selection approach.
    """
    if not feature_stats or not correlation_data.get("features"):
        return ["runtime_sec", "nfs_write_gb", "io_wait_pct"]

    features = correlation_data["features"]
    matrix = correlation_data["matrix"]
    variance_rank = {f: feature_stats.get(f, {}).get("variance", 0) for f in features}

    selected = []
    remaining = sorted(features, key=lambda f: -variance_rank.get(f, 0))

    while len(selected) < n and remaining:
        candidate = remaining.pop(0)
        dominated = False
        for sel in selected:
            try:
                i1 = features.index(candidate)
                i2 = features.index(sel)
                if abs(matrix[i1][i2]) > 0.7:
                    dominated = True
                    break
            except (ValueError, IndexError):
                pass
        if not dominated:
            selected.append(candidate)

    return selected if selected else ["runtime_sec", "nfs_write_gb", "io_wait_pct"]
    if not feature_stats or not correlation_data.get('features'):
        return suggest_best_axes(feature_stats, n)


def compute_failure_hotspots(jobs: list, n_bins: int = 3) -> list:
    """
    Identify resource bins that are over-represented in failures.
    Returns list of hotspots with feature, bin, failure_rate, and baseline_rate.
    """
    if not jobs:
        return []

    # Features to analyze
    features = ["nfs_write_gb", "local_write_gb", "io_wait_pct", "runtime_sec", "req_mem_mb"]
    available_features = [f for f in features if any(j.get(f) is not None for j in jobs)]

    if not available_features:
        return []

    hotspots = []

    for feature in available_features:
        # Get values
        values = [j.get(feature, 0) or 0 for j in jobs]
        if max(values) == min(values):
            continue

        # Compute quantile boundaries
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        boundaries = [sorted_vals[int(n * i / n_bins)] for i in range(1, n_bins)]

        # Assign bins
        def get_bin(v):
            for i, b in enumerate(boundaries):
                if v <= b:
                    return ["low", "med", "high"][i]
            return "high"

        # Count failures per bin
        bin_counts = {"low": {"total": 0, "failed": 0}, "med": {"total": 0, "failed": 0}, "high": {"total": 0, "failed": 0}}

        for j, v in zip(jobs, values):
            b = get_bin(v)
            bin_counts[b]["total"] += 1
            if j.get("failure_reason", 0) != 0:
                bin_counts[b]["failed"] += 1

        # Calculate rates and find hotspots
        total_jobs = len(jobs)
        total_failures = sum(1 for j in jobs if j.get("failure_reason", 0) != 0)
        baseline_rate = total_failures / total_jobs if total_jobs > 0 else 0

        for bin_name, counts in bin_counts.items():
            if counts["total"] < 10:  # Skip small samples
                continue
            failure_rate = counts["failed"] / counts["total"]
            # Check if significantly higher than baseline
            if failure_rate > baseline_rate * 1.3:  # 30% higher than baseline
                hotspots.append({
                    "feature": feature,
                    "bin": bin_name,
                    "failure_rate": round(failure_rate * 100, 1),
                    "baseline_rate": round(baseline_rate * 100, 1),
                    "n_jobs": counts["total"],
                    "n_failures": counts["failed"],
                    "ratio": round(failure_rate / baseline_rate, 2) if baseline_rate > 0 else 0
                })

    # Sort by ratio (most over-represented first)
    hotspots.sort(key=lambda x: -x["ratio"])
    return hotspots[:5]  # Top 5 hotspots


def compute_clustering_quality(jobs: list, edges: list) -> dict:
    """
    Compute metrics measuring how well failure types cluster in the network.
    
    Inspired by phylogenetic community structure metrics:
    - MNTD (Mean Nearest Taxon Distance) → Mean nearest same-class distance
    - NTI/NRI (z-score vs null) → Compare to randomized labels
    - Assortativity → Do same-type nodes connect preferentially?
    
    Returns dict with:
        - assortativity: Coefficient measuring same-type connectivity (-1 to +1)
        - neighborhood_purity: Average fraction of same-class neighbors
        - mntd_ratio: Mean nearest same-class distance / mean nearest any distance
        - z_scores: Significance vs null model for each metric
        - interpretation: Human-readable summary
    """
    import random as rand

    if not jobs or not edges:
        return {"error": "Insufficient data", "assortativity": 0, "neighborhood_purity": 0}

    n_jobs = len(jobs)

    # Build adjacency list
    neighbors = {i: set() for i in range(n_jobs)}
    for edge in edges:
        src, tgt = edge['source'], edge['target']
        if src < n_jobs and tgt < n_jobs:
            neighbors[src].add(tgt)
            neighbors[tgt].add(src)

    # Get failure labels (binary: success vs any failure)
    labels_binary = [0 if j.get('failure_reason', 0) == 0 else 1 for j in jobs]

    # Get detailed failure labels (0-7)
    labels_detailed = [j.get('failure_reason', 0) for j in jobs]

    # Count label frequencies
    n_success = sum(1 for l in labels_binary if l == 0)
    n_failure = n_jobs - n_success

    # =========================================================================
    # 1. Assortativity Coefficient (binary: success vs failure)
    # =========================================================================
    # Measures tendency of nodes to connect to same-type nodes
    # Range: -1 (disassortative) to +1 (assortative)

    def compute_assortativity(labels):
        """Compute assortativity coefficient for categorical labels."""
        e_same = 0  # Edges between same type
        e_total = len(edges)

        if e_total == 0:
            return 0

        for edge in edges:
            src, tgt = edge['source'], edge['target']
            if src < len(labels) and tgt < len(labels):
                if labels[src] == labels[tgt]:
                    e_same += 1

        # Observed fraction of same-type edges
        observed = e_same / e_total

        # Expected fraction under random mixing
        # Sum of (fraction of each type)^2
        label_counts = {}
        for l in labels:
            label_counts[l] = label_counts.get(l, 0) + 1

        expected = sum((c / len(labels)) ** 2 for c in label_counts.values())

        # Assortativity coefficient
        if expected >= 1:
            return 0

        r = (observed - expected) / (1 - expected)
        return round(max(-1, min(1, r)), 4)

    assortativity_binary = compute_assortativity(labels_binary)
    assortativity_detailed = compute_assortativity(labels_detailed)

    # =========================================================================
    # 2. Neighborhood Purity (local clustering)
    # =========================================================================
    # For each node, what fraction of neighbors share its label?

    def compute_purity(labels):
        """Average fraction of same-label neighbors."""
        purities = []
        for i in range(len(labels)):
            if len(neighbors[i]) == 0:
                continue
            same_label = sum(1 for j in neighbors[i] if j < len(labels) and labels[j] == labels[i])
            purities.append(same_label / len(neighbors[i]))

        if not purities:
            return 0
        return round(sum(purities) / len(purities), 4)

    purity_binary = compute_purity(labels_binary)
    purity_detailed = compute_purity(labels_detailed)

    # =========================================================================
    # 3. Mean Nearest Same-Class Distance (MNTD analog)
    # =========================================================================
    # Ratio of distance to nearest same-class vs nearest any-class
    # < 1 means same-class nodes are closer (clustering)
    # > 1 means same-class nodes are farther (overdispersion)

    def compute_mntd_ratio(labels):
        """Compute ratio of same-class to any-class nearest distances."""
        # Use edge weights as inverse distance (higher similarity = closer)
        # Build distance matrix from edges
        distances = {}
        for edge in edges:
            src, tgt = edge['source'], edge['target']
            dist = 1 - edge.get('similarity', 0.5)  # Convert similarity to distance
            distances[(src, tgt)] = dist
            distances[(tgt, src)] = dist

        same_class_dists = []
        any_class_dists = []

        for i in range(len(labels)):
            # Find nearest same-class neighbor
            same_class_nearest = float('inf')
            any_class_nearest = float('inf')

            for j in neighbors[i]:
                if j >= len(labels):
                    continue
                dist = distances.get((i, j), 1.0)

                if dist < any_class_nearest:
                    any_class_nearest = dist

                if labels[j] == labels[i] and dist < same_class_nearest:
                    same_class_nearest = dist

            if same_class_nearest < float('inf'):
                same_class_dists.append(same_class_nearest)
            if any_class_nearest < float('inf'):
                any_class_dists.append(any_class_nearest)

        if not same_class_dists or not any_class_dists:
            return 1.0

        mean_same = sum(same_class_dists) / len(same_class_dists)
        mean_any = sum(any_class_dists) / len(any_class_dists)

        if mean_any == 0:
            return 1.0

        return round(mean_same / mean_any, 4)

    mntd_ratio = compute_mntd_ratio(labels_binary)

    # =========================================================================
    # 4. Z-scores against null model (NTI/NRI analog)
    # =========================================================================
    # Shuffle labels N times, compute metrics, get z-score
    n_permutations = 999
    null_assortativity = []
    null_purity = []
    null_mntd = []

    for _ in range(n_permutations):
        shuffled = labels_binary.copy()
        rand.shuffle(shuffled)
        null_assortativity.append(compute_assortativity(shuffled))
        null_purity.append(compute_purity(shuffled))
        null_mntd.append(compute_mntd_ratio(shuffled))

    def z_score(observed, null_values):
        if not null_values:
            return 0
        mean_null = sum(null_values) / len(null_values)
        var_null = sum((x - mean_null) ** 2 for x in null_values) / len(null_values)
        std_null = var_null ** 0.5 if var_null > 0 else 1
        return round((observed - mean_null) / std_null, 2)

    z_assortativity = z_score(assortativity_binary, null_assortativity)
    z_purity = z_score(purity_binary, null_purity)
    ses_mntd = z_score(mntd_ratio, null_mntd)

    # =========================================================================
    # 5. Interpretation
    # =========================================================================

    interpretations = []

    # Assortativity interpretation
    if assortativity_binary > 0.2:
        interpretations.append(f"Strong clustering: failures tend to connect to other failures (r={assortativity_binary})")
    elif assortativity_binary > 0.05:
        interpretations.append(f"Moderate clustering: some tendency for failures to group (r={assortativity_binary})")
    elif assortativity_binary < -0.1:
        interpretations.append(f"Dispersed: failures are spread among successes (r={assortativity_binary})")
    else:
        interpretations.append(f"Random: no clear clustering pattern (r={assortativity_binary})")

    # Significance interpretation
    if abs(z_assortativity) > 2:
        interpretations.append(f"Pattern is statistically significant (z={z_assortativity})")
    else:
        interpretations.append(f"Pattern not significantly different from random (z={z_assortativity})")

    # MNTD interpretation
    if mntd_ratio < 0.8:
        interpretations.append(f"Same-type jobs are closer than expected (MNTD ratio={mntd_ratio})")
    elif mntd_ratio > 1.2:
        interpretations.append(f"Same-type jobs are farther than expected (MNTD ratio={mntd_ratio})")

    return {
        "assortativity": {
            "binary": assortativity_binary,  # Success vs failure
            "detailed": assortativity_detailed,  # All 8 failure types
            "z_score": z_assortativity,
        },
        "neighborhood_purity": {
            "binary": purity_binary,
            "detailed": purity_detailed,
            "z_score": z_purity,
        },
        "mntd_ratio": mntd_ratio,
        "ses_mntd": ses_mntd,
        "sample_sizes": {
            "n_jobs": n_jobs,
            "n_edges": len(edges),
            "n_success": n_success,
            "n_failure": n_failure,
        },
        "interpretation": interpretations,
        "is_clustered": assortativity_binary > 0.1 and z_assortativity > 1.5,
        "hotspots": compute_failure_hotspots(jobs),
    }

    corr_features = correlation_data['features']
    corr_matrix = correlation_data['matrix']

    # Build correlation lookup
    corr_lookup = {}
    for i, f1 in enumerate(corr_features):
        for j, f2 in enumerate(corr_features):
            corr_lookup[(f1, f2)] = corr_matrix[i][j]

    # Score features by variance (CV * coverage)
    scored = []
    for feature, stats in feature_stats.items():
        if stats['range'] == 0 or stats['non_zero_pct'] < 10:
            continue
        if feature not in corr_features:
            continue
        score = stats['cv'] * (stats['non_zero_pct'] / 100)
        scored.append((feature, score))

    scored.sort(key=lambda x: -x[1])

    # Greedy selection: pick highest scored, then next highest that's not correlated
    selected = []
    for feature, score in scored:
        if len(selected) >= n:
            break

        # Check correlation with already selected features
        is_correlated = False
        for sel in selected:
            r = corr_lookup.get((feature, sel), corr_lookup.get((sel, feature), 0))
            if abs(r) > 0.7:  # Threshold for "too correlated"
                is_correlated = True
                break

        if not is_correlated:
            selected.append(feature)

    # If we couldn't find enough decorrelated features, fall back
    if len(selected) < n:
        for feature, score in scored:
            if feature not in selected:
                selected.append(feature)
            if len(selected) >= n:
                break

    return selected[:n]


def load_similarity_edges_from_db(db_path: Path, job_ids: list, threshold: float = 0.85) -> list:
    """Load pre-computed similarity edges from database."""
    edges = []

    try:
        conn = get_db_connection(db_path)

        # Create job_id to index mapping
        job_id_to_idx = {jid: idx for idx, jid in enumerate(job_ids)}

        rows = conn.execute("""
            SELECT job_id_a, job_id_b, similarity
            FROM job_similarity
            WHERE similarity >= ?
            AND job_id_a IN ({})
            AND job_id_b IN ({})
        """.format(
            ','.join('?' * len(job_ids)),
            ','.join('?' * len(job_ids))
        ), [threshold] + job_ids + job_ids).fetchall()

        for row in rows:
            if row['job_id_a'] in job_id_to_idx and row['job_id_b'] in job_id_to_idx:
                edges.append({
                    "source": job_id_to_idx[row['job_id_a']],
                    "target": job_id_to_idx[row['job_id_b']],
                    "similarity": row['similarity']
                })

        conn.close()

    except Exception as e:
        logger.debug(f"Failed to load similarity edges: {e}")

    return edges


# ============================================================================
# Demo Data Generation (Fallback)
# ============================================================================

def generate_demo_clusters():
    """Generate demo cluster data."""
    clusters = {
        "cluster-1": {
            "name": "Cluster-1",
            "description": "30-node CPU cluster",
            "nodes": [f"cn{i:02d}" for i in range(1, 31)],
            "type": "cpu"
        },
        "cluster-2": {
            "name": "Cluster-2",
            "description": "6-node GPU cluster (3 CPU + 3 GPU)",
            "nodes": ["gn01", "gn02", "gn03", "gn04", "gn05", "gn06"],
            "gpu_nodes": ["gn04", "gn05", "gn06"],
            "type": "hybrid"
        },
    }
    return clusters


def generate_demo_node_data(clusters):
    """Generate realistic node statistics."""
    nodes = {}
    failure_types = ["OOM", "Timeout", "Cancelled", "NodeFail", "DiskFull"]
    users = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "henry"]

    for cluster_id, cluster in clusters.items():
        for node_name in cluster["nodes"]:
            random.seed(hash(node_name) % 2**32)
            is_down = random.random() < 0.08

            if is_down:
                nodes[node_name] = {
                    "name": node_name,
                    "cluster": cluster_id,
                    "status": "down",
                    "success_rate": 0,
                    "jobs_today": 0,
                    "jobs_success": 0,
                    "jobs_failed": 0,
                    "failures": {},
                    "top_users": [],
                    "has_gpu": node_name in cluster.get("gpu_nodes", []),
                    "gpu_util": 0,
                    "cpu_util": 0,
                    "mem_util": 0,
                    "load_avg": 0,
                    "last_seen": (datetime.now() - timedelta(hours=random.randint(1, 48))).isoformat()
                }
            else:
                jobs_total = random.randint(5, 80)
                if random.random() < 0.15:
                    success_rate = random.uniform(0.35, 0.65)
                elif random.random() < 0.3:
                    success_rate = random.uniform(0.65, 0.85)
                else:
                    success_rate = random.uniform(0.85, 0.99)

                jobs_success = int(jobs_total * success_rate)
                jobs_failed = jobs_total - jobs_success

                failures = {}
                remaining = jobs_failed
                for ft in random.sample(failure_types, min(3, len(failure_types))):
                    if remaining <= 0:
                        break
                    count = random.randint(1, max(1, remaining))
                    failures[ft] = count
                    remaining -= count

                node_users = random.sample(users, random.randint(2, 5))
                top_users = []
                jobs_remaining = jobs_total
                for u in node_users[:-1]:
                    count = random.randint(1, max(1, jobs_remaining // 2))
                    top_users.append({"user": u, "jobs": count})
                    jobs_remaining -= count
                top_users.append({"user": node_users[-1], "jobs": jobs_remaining})
                top_users.sort(key=lambda x: -x["jobs"])

                has_gpu = node_name in cluster.get("gpu_nodes", [])

                # GPU workload profile for demo nodes
                gpu_workload_profiles = [
                    ("tensor-heavy compute", 0.90),
                    ("tensor compute",       0.75),
                    ("FP64 / HPC compute",   0.85),
                    ("compute-active",       0.65),
                    ("memory-bound",         0.50),
                    ("idle",                 0.20),
                ]
                gpu_util = random.randint(60, 98) if has_gpu else 0
                gpu_workload, real_ratio = random.choice(gpu_workload_profiles) if has_gpu else (None, 0)
                gpu_real_util = round(gpu_util * real_ratio + random.gauss(0, 2), 1) if has_gpu else None
                gpu_real_util = max(0, min(100, gpu_real_util)) if gpu_real_util is not None else None
                gpu_health = random.choices(["OK", "WARN", "HOT"], weights=[85, 10, 5])[0] if has_gpu else None

                nodes[node_name] = {
                    "name": node_name,
                    "cluster": cluster_id,
                    "status": "online",
                    "success_rate": success_rate,
                    "jobs_today": jobs_total,
                    "jobs_success": jobs_success,
                    "jobs_failed": jobs_failed,
                    "failures": failures,
                    "top_users": top_users[:5],
                    "has_gpu": has_gpu,
                    "gpu_util": gpu_util,
                    "gpu_name": "NVIDIA A100-SXM4-40GB" if has_gpu else None,
                    "gpu_real_util": gpu_real_util,
                    "gpu_workload": gpu_workload,
                    "gpu_data_source": "dcgm" if has_gpu else None,
                    "gpu_health": gpu_health,
                    "cpu_util": random.randint(40, 95),
                    "mem_util": random.randint(30, 85),
                    "load_avg": round(random.uniform(0.5, 16.0), 2),
                    "last_seen": datetime.now().isoformat()
                }

    random.seed()
    return nodes


def generate_demo_jobs(count=150):
    """Generate demo job data for network visualization."""
    jobs = []
    partitions = ["compute", "gpu", "short", "long"]

    # State to failure_reason mapping
    # 0=success, 1=timeout, 2=cancelled, 3=failed_generic, 4=oom, 5=segfault, 6=node_fail, 7=dependency
    state_to_failure = {
        "COMPLETED": 0,
        "TIMEOUT": 1,
        "CANCELLED": 2,
        "FAILED": 3,
        "OUT_OF_MEMORY": 4,
        "SEGFAULT": 5,  # We'll add this state for variety
        "NODE_FAIL": 6,
    }

    states = ["COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "CANCELLED", "SEGFAULT", "NODE_FAIL"]

    for i in range(count):
        nfs_write = random.uniform(0, 100)
        local_write = random.uniform(0, 100)
        io_wait = random.uniform(0, 50)
        runtime_sec = random.randint(60, 86400)  # 1 min to 24 hours
        wait_time_sec = random.randint(0, 3600)  # 0 to 1 hour
        req_time_sec = int(runtime_sec * random.uniform(1.0, 2.0))  # Requested time >= runtime
        req_cpus = random.choice([1, 2, 4, 8, 16, 32])
        req_mem_mb = random.choice([1024, 2048, 4096, 8192, 16384, 32768])

        # State probabilities based on I/O patterns
        if local_write > 50:
            state = random.choices(states, weights=[80, 3, 5, 4, 3, 3, 2])[0]
        elif nfs_write > 70:
            state = random.choices(states, weights=[35, 15, 20, 12, 5, 8, 5])[0]
        else:
            state = random.choices(states, weights=[68, 8, 10, 6, 3, 3, 2])[0]

        # Get failure_reason from state
        failure_reason = state_to_failure.get(state, 3)

        # Generate exit_code and exit_signal based on failure_reason
        exit_code = None
        exit_signal = None

        if failure_reason == 0:  # SUCCESS
            exit_code = 0
        elif failure_reason == 1:  # TIMEOUT
            exit_code = 0  # Clean exit but timed out
        elif failure_reason == 2:  # CANCELLED
            exit_signal = 15  # SIGTERM
        elif failure_reason == 3:  # FAILED
            exit_code = random.choice([1, 2, 127, 255])
        elif failure_reason == 4:  # OOM
            exit_code = 137  # 128 + 9 (SIGKILL)
            exit_signal = 9
        elif failure_reason == 5:  # SEGFAULT
            exit_code = 139  # 128 + 11 (SIGSEGV)
            exit_signal = 11
        elif failure_reason == 6:  # NODE_FAIL
            exit_code = None  # Unknown - node died

        # Map SEGFAULT to FAILED for display state
        display_state = "FAILED" if state == "SEGFAULT" else state

        jobs.append({
            "job_id": 10000 + i,
            "nfs_write_gb": round(nfs_write, 2),
            "local_write_gb": round(local_write, 2),
            "io_wait_pct": round(io_wait, 2),
            "partition": random.choice(partitions),
            "state": display_state,
            "success": failure_reason == 0,
            "failure_reason": failure_reason,
            "exit_code": exit_code,
            "exit_signal": exit_signal,
            "runtime_sec": runtime_sec,
            "wait_time_sec": wait_time_sec,
            "req_time_sec": req_time_sec,
            "req_cpus": req_cpus,
            "req_mem_mb": req_mem_mb,
            "total_write_mb": round((nfs_write + local_write) * 1024, 2),  # Convert GB to MB
            "total_read_mb": round(random.uniform(0, 50) * 1024, 2),
            "health_score": random.uniform(0.3, 1.0) if failure_reason == 0 else random.uniform(0, 0.5),
            "time_efficiency": runtime_sec / max(req_time_sec, 1),
        })

    return jobs


def build_job_network(jobs, threshold=0.95, features=None):
    """Build similarity network between jobs using cosine similarity.
    
    NOTE: This is the legacy method. Use build_bipartite_network() for the
    Vilhena & Antonelli approach with Simpson's β-diversity.
    """
    if features is None:
        features = ["nfs_write_gb", "local_write_gb", "io_wait_pct"]

    edges = []

    for i, job1 in enumerate(jobs):
        vec1 = [job1.get(f, 0) or 0 for f in features]
        mag1 = math.sqrt(sum(x*x for x in vec1)) or 1

        for j, job2 in enumerate(jobs[i+1:], i+1):
            vec2 = [job2.get(f, 0) or 0 for f in features]
            mag2 = math.sqrt(sum(x*x for x in vec2)) or 1

            dot = sum(a*b for a, b in zip(vec1, vec2))
            similarity = dot / (mag1 * mag2) if (mag1 * mag2) > 0 else 0

            if similarity >= threshold:
                edges.append({
                    "source": i,
                    "target": j,
                    "similarity": round(similarity, 4)
                })

    return edges


def discretize_features(jobs: list, features: list = None, n_bins: int = 3) -> dict:
    """
    Discretize continuous features into categorical bins.
    Uses quantile-based binning to create balanced categories.
    
    Returns:
        dict with:
        - 'bin_labels': List of bin names (e.g., 'runtime_sec_low', 'runtime_sec_med', 'runtime_sec_high')
        - 'job_bins': List of sets, where each set contains the bin labels for that job
        - 'bin_thresholds': Dict mapping feature -> list of threshold values
    """
    if not jobs:
        return {'bin_labels': [], 'job_bins': [], 'bin_thresholds': {}}

    if features is None:
        sample = jobs[0]
        features = [k for k, v in sample.items()
                   if isinstance(v, (int, float)) and k not in ('job_id', 'success')]

    # Compute quantile thresholds for each feature
    bin_thresholds = {}
    bin_suffixes = ['low', 'med', 'high'] if n_bins == 3 else [f'q{i+1}' for i in range(n_bins)]

    for feature in features:
        values = sorted([j.get(feature, 0) or 0 for j in jobs])
        n = len(values)

        # Skip constant features
        if values[0] == values[-1]:
            continue

        # Compute quantile boundaries
        thresholds = []
        for q in range(1, n_bins):
            idx = int(n * q / n_bins)
            thresholds.append(values[idx])

        bin_thresholds[feature] = thresholds

    # Create all possible bin labels
    all_bin_labels = []
    for feature in bin_thresholds.keys():
        for suffix in bin_suffixes:
            all_bin_labels.append(f"{feature}_{suffix}")

    # Assign bins to each job
    job_bins = []
    for job in jobs:
        bins = set()
        for feature, thresholds in bin_thresholds.items():
            value = job.get(feature, 0) or 0

            # Find which bin this value falls into
            bin_idx = 0
            for thresh in thresholds:
                if value > thresh:
                    bin_idx += 1

            bin_label = f"{feature}_{bin_suffixes[bin_idx]}"
            bins.add(bin_label)

        job_bins.append(bins)

    return {
        'bin_labels': all_bin_labels,
        'job_bins': job_bins,
        'bin_thresholds': bin_thresholds,
        'n_features': len(bin_thresholds),
        'n_bins_per_feature': n_bins
    }


def simpson_similarity(set1: set, set2: set) -> float:
    """
    Compute Simpson similarity between two sets.
    
    Based on Vilhena & Antonelli (2015):
    βsim = min(b, c) / (a + min(b, c))  [dissimilarity]
    
    Simpson similarity = a / (a + min(b, c)) = 1 - βsim
    
    Where:
    - a = |set1 ∩ set2| (shared elements)
    - b = |set1 - set2| (unique to set1)
    - c = |set2 - set1| (unique to set2)
    
    This measure focuses on the proportion of shared elements relative to
    the smaller set, avoiding bias toward larger sets.
    
    Returns value in [0, 1] where 1 = identical sets, 0 = no overlap
    """
    if not set1 or not set2:
        return 0.0

    a = len(set1 & set2)  # intersection
    b = len(set1 - set2)  # unique to set1
    c = len(set2 - set1)  # unique to set2

    denominator = a + min(b, c)

    if denominator == 0:
        return 1.0 if a > 0 else 0.0

    return a / denominator


def build_bipartite_network(jobs: list, features: list = None,
                            threshold: float = 0.5, n_bins: int = 3,
                            max_edges: int = 10000) -> dict:
    """
    Build job similarity network using Vilhena & Antonelli's bipartite approach.
    
    Methodology (from Nature Communications, 2015):
    1. Create bipartite network: Jobs × Resource-bins
    2. Each job is characterized by which resource bins it occupies
    3. Similarity computed using Simpson's index (handles set-size bias)
    4. Edges created between jobs with similarity above threshold
    
    This approach:
    - Treats each resource bin as a "site" (biogeography analogy)
    - Jobs are "species" that occur in multiple sites
    - Similar jobs share resource usage patterns
    - Clusters emerge as "bioregions" of job behavior
    
    Args:
        jobs: List of job dicts with numeric features
        features: List of feature names to use (None = auto-detect)
        threshold: Simpson similarity threshold for edge creation (0.5 recommended)
        n_bins: Number of bins per feature (3 = low/med/high)
        max_edges: Maximum edges to prevent memory issues
    
    Returns:
        dict with:
        - 'edges': List of edge dicts with source, target, similarity
        - 'discretization': Info about how features were binned
        - 'stats': Network statistics
    """
    if not jobs:
        return {'edges': [], 'discretization': {}, 'stats': {}}

    # Step 1: Discretize features into bins
    disc = discretize_features(jobs, features, n_bins)
    job_bins = disc['job_bins']

    if not job_bins:
        return {'edges': [], 'discretization': disc, 'stats': {'error': 'No valid features'}}

    # Step 2: Compute Simpson similarity for all pairs
    edges = []
    n_jobs = len(jobs)
    n_comparisons = 0
    similarity_sum = 0
    n_above_threshold = 0

    for i in range(n_jobs):
        for j in range(i + 1, n_jobs):
            sim = simpson_similarity(job_bins[i], job_bins[j])
            n_comparisons += 1
            similarity_sum += sim

            if sim >= threshold:
                n_above_threshold += 1
                if len(edges) < max_edges:
                    edges.append({
                        "source": i,
                        "target": j,
                        "similarity": round(sim, 4)
                    })

    # Compute network statistics
    avg_similarity = similarity_sum / n_comparisons if n_comparisons > 0 else 0
    edge_density = len(edges) / n_comparisons if n_comparisons > 0 else 0

    stats = {
        'n_jobs': n_jobs,
        'n_comparisons': n_comparisons,
        'n_edges': len(edges),
        'n_above_threshold': n_above_threshold,
        'avg_similarity': round(avg_similarity, 4),
        'edge_density': round(edge_density, 4),
        'threshold': threshold,
        'truncated': len(edges) >= max_edges
    }

    return {
        'edges': edges,
        'discretization': disc,
        'stats': stats
    }

def normalize_features(jobs: list, features: list = None) -> tuple:
    """
    Extract and normalize feature vectors from jobs using z-score.
    
    Returns:
        Tuple of (normalized_vectors, feature_names, normalization_params)
    """
    if not jobs:
        return [], [], {}

    # Auto-detect numeric features
    if features is None:
        sample = jobs[0]
        features = [k for k, v in sample.items()
                   if isinstance(v, (int, float)) and k not in ('job_id', 'success', 'exit_code')]

    # Extract raw vectors
    raw_vectors = []
    for job in jobs:
        vec = [job.get(f, 0) or 0 for f in features]
        raw_vectors.append(vec)

    # Compute mean and std for each feature
    n_features = len(features)
    n_jobs = len(jobs)

    if n_jobs == 0 or n_features == 0:
        return [], features, {}

    means = [0.0] * n_features
    for vec in raw_vectors:
        for i, v in enumerate(vec):
            means[i] += v
    means = [m / n_jobs for m in means]

    stds = [0.0] * n_features
    for vec in raw_vectors:
        for i, v in enumerate(vec):
            stds[i] += (v - means[i]) ** 2
    stds = [math.sqrt(s / n_jobs) if n_jobs > 0 else 1.0 for s in stds]
    stds = [s if s > 1e-10 else 1.0 for s in stds]  # Avoid division by zero

    # Z-score normalization
    normalized = []
    for vec in raw_vectors:
        norm_vec = [(v - means[i]) / stds[i] for i, v in enumerate(vec)]
        normalized.append(norm_vec)

    params = {
        'features': features,
        'means': means,
        'stds': stds,
        'normalized': True
    }

    return normalized, features, params


def cosine_similarity(vec1: list, vec2: list) -> float:
    """
    Compute cosine similarity between two vectors.
    
    cosine_sim = (A · B) / (||A|| * ||B||)
    
    Returns value in [-1, 1] where:
        1 = identical direction
        0 = orthogonal
       -1 = opposite direction
    """
    if len(vec1) != len(vec2) or len(vec1) == 0:
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = math.sqrt(sum(x * x for x in vec1))
    mag2 = math.sqrt(sum(x * x for x in vec2))

    if mag1 < 1e-10 or mag2 < 1e-10:
        return 0.0

    return dot_product / (mag1 * mag2)


def build_cosine_network(
    jobs: list,
    features: list = None,
    threshold: float = 0.7,
    normalize: bool = True,
    max_edges: int = 10000
) -> dict:
    """
    Build job similarity network using cosine similarity on continuous features.
    
    Unlike Simpson (which discretizes), this operates on raw continuous vectors.
    Jobs are connected if their resource usage vectors point in similar directions.
    
    Args:
        jobs: List of job dicts with numeric features
        features: List of feature names to use (None = auto-detect)
        threshold: Cosine similarity threshold for edge creation (0.7 default)
        normalize: Whether to z-score normalize features (recommended)
        max_edges: Maximum edges to prevent memory issues
    
    Returns:
        dict with:
        - 'edges': List of edge dicts with source, target, similarity
        - 'normalization': Info about how features were normalized
        - 'stats': Network statistics
    """
    if not jobs:
        return {'edges': [], 'normalization': {}, 'stats': {}}

    # Normalize features
    if normalize:
        vectors, feature_names, norm_params = normalize_features(jobs, features)
    else:
        if features is None:
            sample = jobs[0]
            features = [k for k, v in sample.items()
                       if isinstance(v, (int, float)) and k not in ('job_id', 'success', 'exit_code')]
        vectors = [[job.get(f, 0) or 0 for f in features] for job in jobs]
        feature_names = features
        norm_params = {'features': features, 'normalized': False}

    if not vectors or not vectors[0]:
        return {'edges': [], 'normalization': norm_params, 'stats': {'error': 'No valid features'}}

    # Compute pairwise cosine similarity
    edges = []
    n_jobs = len(jobs)
    n_comparisons = 0
    similarity_sum = 0
    n_above_threshold = 0

    for i in range(n_jobs):
        for j in range(i + 1, n_jobs):
            sim = cosine_similarity(vectors[i], vectors[j])
            n_comparisons += 1
            similarity_sum += sim

            if sim >= threshold:
                n_above_threshold += 1
                if len(edges) < max_edges:
                    edges.append({
                        "source": i,
                        "target": j,
                        "similarity": round(sim, 4)
                    })

    # Compute network statistics
    avg_similarity = similarity_sum / n_comparisons if n_comparisons > 0 else 0
    edge_density = len(edges) / n_comparisons if n_comparisons > 0 else 0

    stats = {
        'n_jobs': n_jobs,
        'n_features': len(feature_names),
        'n_comparisons': n_comparisons,
        'n_edges': len(edges),
        'n_above_threshold': n_above_threshold,
        'avg_similarity': round(avg_similarity, 4),
        'edge_density': round(edge_density, 4),
        'threshold': threshold,
        'method': 'cosine',
        'truncated': len(edges) >= max_edges
    }

    return {
        'edges': edges,
        'normalization': norm_params,
        'stats': stats
    }


def build_similarity_network(
    jobs: list,
    method: str = 'cosine',
    features: list = None,
    threshold: float = None,
    **kwargs
) -> dict:
    """
    Build job similarity network using specified method.
    
    This is the unified interface - use this instead of calling
    build_cosine_network or build_bipartite_network directly.
    
    Args:
        jobs: List of job dicts with numeric features
        method: 'cosine' (default) or 'simpson'
        features: List of feature names to use
        threshold: Similarity threshold (default: 0.7 for cosine, 0.5 for simpson)
        **kwargs: Additional arguments passed to specific method
    
    Returns:
        dict with edges, stats, and method-specific info
    """
    if method == 'cosine':
        if threshold is None:
            threshold = 0.7
        return build_cosine_network(
            jobs,
            features=features,
            threshold=threshold,
            **kwargs
        )

    elif method == 'simpson':
        if threshold is None:
            threshold = 0.5
        return build_bipartite_network(
            jobs,
            features=features,
            threshold=threshold,
            **kwargs
        )

    else:
        raise ValueError(f"Unknown method: {method}. Use 'cosine' or 'simpson'.")

def compute_bipartite_matrix(jobs: list, features: list = None, n_bins: int = 3) -> dict:
    """
    Create the bipartite incidence matrix (Jobs × Resource-bins).
    
    This is the core data structure for the Vilhena & Antonelli method:
    - Rows = Jobs
    - Columns = Resource bins (discretized features)
    - Cell = 1 if job occupies that bin, 0 otherwise
    
    Returns dict with matrix data for visualization and analysis.
    """
    disc = discretize_features(jobs, features, n_bins)

    if not disc['bin_labels']:
        return {'matrix': [], 'row_labels': [], 'col_labels': []}

    # Build incidence matrix
    bin_labels = sorted(disc['bin_labels'])
    bin_to_idx = {b: i for i, b in enumerate(bin_labels)}

    matrix = []
    for i, job in enumerate(jobs):
        row = [0] * len(bin_labels)
        for bin_label in disc['job_bins'][i]:
            if bin_label in bin_to_idx:
                row[bin_to_idx[bin_label]] = 1
        matrix.append(row)

    # Compute column sums (bin occupancy counts)
    col_sums = [sum(matrix[i][j] for i in range(len(jobs))) for j in range(len(bin_labels))]

    return {
        'matrix': matrix,
        'row_labels': [f"job_{j['job_id']}" for j in jobs],
        'col_labels': bin_labels,
        'col_sums': col_sums,
        'n_jobs': len(jobs),
        'n_bins': len(bin_labels)
    }


# ============================================================================
# Data Manager - Unified Interface
# ============================================================================

class DataManager:
    """Manages data loading from database or demo fallback."""

    def __init__(self, config: dict, db_path: str = None):
        self.config = config
        self.db_path = Path(db_path) if db_path else find_database()
        self.data_source = "demo"

        self._clusters = None
        self._nodes = None
        self._jobs = None
        self._edges = None
        self._feature_stats = None
        self._correlation_data = None
        self._suggested_axes = None
        self._network_stats = None
        self._ml_predictions = None
        self._discretization = None
        self._clustering_quality = None

        self._load_data()

    def _load_data(self):
        """Load all data from best available source."""

        # Try to load from database
        if self.db_path:
            logger.info(f"Found database: {self.db_path}")

            # Load clusters
            self._clusters = load_clusters_from_db(self.db_path)

            # Filter partitions by TOML config if available
            if self._clusters and self.config:
                slurm_config = self.config.get("collectors", {}).get("slurm", {})
                configured_parts = slurm_config.get("partitions")
                if configured_parts:
                    for cid, cluster in self._clusters.items():
                        if "partitions" in cluster:
                            filtered = {p: ns for p, ns in cluster["partitions"].items()
                                        if p in configured_parts}
                            if filtered:
                                cluster["partitions"] = filtered
                                # Update node list to only configured partition nodes
                                all_ns = set()
                                for ns in filtered.values():
                                    all_ns.update(ns)
                                cluster["nodes"] = sorted(all_ns)
                                cluster["description"] = f"{len(all_ns)}-node cluster"

            if self._clusters:
                self.data_source = f"database ({self.db_path.name})"
                logger.info(f"Loaded {len(self._clusters)} clusters from database")

                # Load nodes
                self._nodes = load_node_data_from_db(self.db_path, self._clusters)
                logger.info(f"Loaded {len(self._nodes)} nodes from database")

                # Load jobs
                self._jobs = load_jobs_from_db(self.db_path)
                if self._jobs:
                    logger.info(f"Loaded {len(self._jobs)} jobs from database")

                    # Compute feature statistics
                    self._feature_stats = compute_feature_stats(self._jobs)

                    # Compute correlation matrix
                    self._correlation_data = compute_correlation_matrix(self._jobs)
                    n_high_corr = len(self._correlation_data.get('high_correlations', []))
                    logger.info(f"Computed correlations for {len(self._correlation_data.get('features', []))} features ({n_high_corr} high correlations)")

                    # Suggest decorrelated axes
                    self._suggested_axes = suggest_decorrelated_axes(
                        self._feature_stats,
                        self._correlation_data
                    )
                    logger.info(f"Suggested axes: {self._suggested_axes}")

                    # Try to load pre-computed edges
                    job_ids = [j["job_id"] for j in self._jobs]
                    self._edges = load_similarity_edges_from_db(self.db_path, job_ids)

                    if not self._edges:
                        network_result = build_similarity_network(
                            self._jobs,
                            method='cosine',
                            features=self._suggested_axes,
                            threshold=0.7
                        )
                        self._edges = network_result['edges']
                        self._network_stats = network_result['stats']
                        self._discretization = network_result.get('discretization') or network_result.get('normalization')
                        logger.info(f"Built cosine network: {len(self._edges)} edges (threshold ≥ 0.7)")

                        # Compute clustering quality metrics
                        self._clustering_quality = compute_clustering_quality(self._jobs, self._edges)
                        if self._clustering_quality.get('is_clustered'):
                            logger.info(f"Clustering detected: assortativity={self._clustering_quality['assortativity']['binary']}")
                        else:
                            logger.info(f"No significant clustering (assortativity={self._clustering_quality['assortativity']['binary']})")
                        # Run ML predictions
                        self.run_ml_predictions()
                else:
                    # No completed jobs yet — show empty network
                    self._jobs = []
                    self._edges = []
                    self._network_stats = {"nodes": 0, "edges": 0}
                    logger.info("No completed jobs in database — network view empty")
                return

        # No database found — generate demo database and reload
        demo_db = Path.home() / "nomad_demo.db"
        logger.info("No database found — generating demo database...")
        try:
            import subprocess
            result = subprocess.run(
                ["nomad", "demo", "--no-launch"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0 and demo_db.exists():
                logger.info("Demo database generated — reloading from nomad_demo.db")
                self.db_path = demo_db
                self.data_source = "database"
                self._load_data()
                return
            else:
                logger.warning(f"Demo generation failed: {result.stderr.strip()}")
        except Exception as e:
            logger.warning(f"Could not generate demo database: {e}")
        # Final fallback to in-memory demo if generation failed
        logger.info("Using in-memory demo data")
        self.data_source = "demo"
        self._clusters = generate_demo_clusters()
        self._nodes = generate_demo_node_data(self._clusters)
        self._jobs = generate_demo_jobs(150)
        self._feature_stats = compute_feature_stats(self._jobs)
        self._correlation_data = compute_correlation_matrix(self._jobs)
        self._suggested_axes = suggest_decorrelated_axes(
            self._feature_stats,
            self._correlation_data
        )
        network_result = build_similarity_network(self._jobs, method='cosine', threshold=0.7)
        self._edges = network_result['edges']
        self._network_stats = network_result['stats']
        self._discretization = network_result.get('discretization') or network_result.get('normalization')
        self._clustering_quality = compute_clustering_quality(self._jobs, self._edges)
        self.run_ml_predictions()

    @property
    def clusters(self):
        return self._clusters

    @property
    def nodes(self):
        return self._nodes

    @property
    def jobs(self):
        return self._jobs

    @property
    def edges(self):
        return self._edges

    @property
    def feature_stats(self):
        return self._feature_stats

    @property
    def correlation_data(self):
        return self._correlation_data

    @property
    def suggested_axes(self):
        return self._suggested_axes

    @property
    def network_stats(self):
        return self._network_stats

    @property
    def discretization(self):
        return self._discretization

    @property
    def clustering_quality(self):
        return self._clustering_quality
    @property
    def ml_predictions(self):
        return self._ml_predictions

    def refresh(self):
        """Refresh data from source."""
        self._load_data()


    def run_ml_predictions(self):
        """Run ML ensemble predictions on current jobs."""
        try:
            from nomad.ml import is_torch_available
            if not is_torch_available():
                self._ml_predictions = {"status": "pytorch_not_available"}
                return

            import torch

            from nomad.ml.autoencoder import JobAutoencoder, prepare_autoencoder_data
            from nomad.ml.gnn_torch import FailureGNN, prepare_pyg_data

            jobs = self._jobs
            edges = self._edges

            if not jobs or len(jobs) < 10:
                self._ml_predictions = {"status": "insufficient_data"}
                return

            # Prepare GNN data
            gnn_edges = [{"source": e["source"], "target": e["target"]} for e in edges]
            gnn_data = prepare_pyg_data(jobs, gnn_edges)

            # Quick GNN prediction (untrained - just structure)
            gnn_model = FailureGNN(input_dim=gnn_data.x.size(1), hidden_dim=32, output_dim=8)

            # Autoencoder for anomaly detection
            ae_features, ae_labels, _ = prepare_autoencoder_data(jobs)
            ae_model = JobAutoencoder(input_dim=ae_features.size(1), latent_dim=4)

            # Simple anomaly scores (reconstruction error without training)
            ae_model.eval()
            with torch.no_grad():
                recon = ae_model(ae_features)
                errors = ((ae_features - recon) ** 2).mean(dim=1)
                threshold = errors.mean() + 2 * errors.std()
                anomalies = errors > threshold

            # Identify high-risk jobs
            high_risk = []
            for i, (job, is_anomaly, error) in enumerate(zip(jobs, anomalies.tolist(), errors.tolist())):
                if is_anomaly or job.get("failure_reason", 0) != 0:
                    high_risk.append({
                        "job_idx": i,
                        "job_id": job.get("job_id", i),
                        "anomaly_score": round(error, 4),
                        "is_anomaly": is_anomaly,
                        "failure_reason": job.get("failure_reason", 0)
                    })

            high_risk.sort(key=lambda x: -x["anomaly_score"])

            self._ml_predictions = {
                "status": "ready",
                "n_jobs": len(jobs),
                "n_anomalies": int(anomalies.sum()),
                "threshold": round(float(threshold), 4),
                "high_risk": high_risk[:50]  # Top 50
            }
            logger.info(f"ML predictions: {len(high_risk)} high-risk jobs identified")

        except Exception as e:
            logger.error(f"ML prediction error: {e}")
            self._ml_predictions = {"status": "error", "message": str(e)}
    def get_queue_running(self):
        """Get running/pending from queue_state."""
        try:
            conn = get_db_connection(self.db_path)
            rows = conn.execute("""
                SELECT qs.partition, qs.running_jobs, qs.pending_jobs, qs.source_site
                FROM queue_state qs
                INNER JOIN (
                    SELECT partition, COALESCE(source_site,'local') as ss, MAX(timestamp) as mt
                    FROM queue_state GROUP BY partition, COALESCE(source_site,'local')
                ) latest ON qs.partition = latest.partition AND qs.timestamp = latest.mt
                   AND COALESCE(qs.source_site,'local') = latest.ss
            """).fetchall()
            conn.close()
            by_site = {}
            for r in rows:
                site = r["source_site"] or "local"
                if site not in by_site:
                    by_site[site] = {"running": 0, "pending": 0}
                by_site[site]["running"] += (r["running_jobs"] or 0)
                by_site[site]["pending"] += (r["pending_jobs"] or 0)
            return by_site
        except Exception:
            return {}

    def get_stats(self) -> dict:
        """Get summary statistics."""
        online_nodes = sum(1 for n in self._nodes.values() if n['status'] == 'online')
        success_jobs = sum(1 for j in self._jobs if j['success'])

        return {
            "data_source": self.data_source,
            "clusters": len(self._clusters),
            "nodes_total": len(self._nodes),
            "nodes_online": online_nodes,
            "nodes_down": len(self._nodes) - online_nodes,
            "jobs": len(self._jobs),
            "jobs_success": success_jobs,
            "jobs_failed": len(self._jobs) - success_jobs,
            "edges": len(self._edges)
        }


# ============================================================================
# HTML Dashboard
# ============================================================================

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NØMAÐ - HPC Monitor</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.5/babel.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-deep: #0d1117;
            --bg-surface: #161b22;
            --bg-elevated: #21262d;
            --bg-hover: #30363d;
            --border: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --green: #3fb950;
            --green-muted: #238636;
            --yellow: #d29922;
            --yellow-muted: #9e6a03;
            --red: #f85149;
            --red-muted: #da3633;
            --cyan: #58a6ff;
            --purple: #a371f7;
            --bg-secondary: #1e293b;
            --bg-tertiary: #0f172a;
            --input-bg: #1e293b;
            --input-border: #334155;
            --btn-text: #e2e8f0;
        }
        /* Light theme - toggle with button in header */
        .light-theme {
            --bg-deep: #ffffff;
            --bg-surface: #f5f6f8;
            --bg-elevated: #ffffff;
            --bg-hover: #e0e3e8;
            --border: #b0b8c0;
            --text-primary: #111111;
            --text-secondary: #333333;
            --text-muted: #555555;
            --green: #0d6a28;
            --yellow: #7a5200;
            --red: #b91c1c;
            --cyan: #0077a0;
            --purple: #6b21a8;
            --bg-secondary: #f0f2f5;
            --bg-tertiary: #e8eaed;
            --input-bg: #ffffff;
            --input-border: #c0c6cc;
            --btn-text: #1a1a1a;
        }
        .light-theme .logo > span {
            color: #00BACF !important;
        }

        .light-theme .node-card {
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .light-theme .sidebar {
            box-shadow: -2px 0 8px rgba(0,0,0,0.1);
        }
        .light-theme .util-track {
            background: #e1e4e8;
        }
        .light-theme .logo-dark {
            display: none !important;
        }
        .light-theme .logo-light {
            display: inline-block !important;
        }
        .light-theme .partition-header,
        .light-theme .partition-type,
        .light-theme .partition-count,
        .light-theme .partition-name {
            color: #1a1a1a !important;
        }

        .theme-toggle {
            background: var(--bg-hover);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 6px 12px;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
        }
        .theme-toggle:hover {
            background: var(--bg-elevated);
            color: var(--text-primary);
        }
        .theme-toggle svg {
            width: 16px;
            height: 16px;
        }

        
        body {
            font-family: 'IBM Plex Sans', -apple-system, sans-serif;
            background: var(--bg-deep);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }
        
        .mono { font-family: 'IBM Plex Mono', monospace; }
        
        .header {
            background: var(--bg-surface);
            border-bottom: 1px solid var(--border);
            padding: 0 24px;
            display: flex;
            align-items: center;
            height: 64px;
            gap: 32px;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: 12px;
            font-weight: 700;
            font-size: 20px;
            letter-spacing: -0.5px;
        }
        
        .logo-icon {
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
        }
        
        .tabs {
            display: flex;
            gap: 4px;
            flex: 1;
        }
        
        .tab {
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.15s;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-secondary);
            border: 1px solid transparent;
        }
        
        .tab:hover {
            background: var(--bg-hover);
            color: var(--text-primary);
        }
        
        .tab.active {
            background: var(--bg-elevated);
            color: var(--text-primary);
            border-color: var(--border);
        }
        
        .tab-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 20px;
            height: 20px;
            padding: 0 6px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 600;
            margin-left: 8px;
            background: var(--bg-hover);
        }
        
        .tab.active .tab-badge { background: var(--border); }
        
        .header-right {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        
        .data-source {
            font-size: 11px;
            color: var(--text-muted);
            padding: 4px 8px;
            background: var(--bg-elevated);
            border-radius: 4px;
            font-family: 'IBM Plex Mono', monospace;
        }
        
        .timestamp {
            font-size: 12px;
            color: var(--text-muted);
            font-family: 'IBM Plex Mono', monospace;
        }
        
        .main {
            display: flex;
            height: calc(100vh - 64px);
        }
        
        .content {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
        }
        
        .cluster-header {
            margin-bottom: 24px;
        }
        
        .cluster-title {
            font-size: 24px;
            font-weight: 700;
            margin-bottom: 4px;
        }
        
        .cluster-desc {
            color: var(--text-secondary);
            font-size: 14px;
        }
        
        .stats-bar {
            display: flex;
            gap: 24px;
            margin-bottom: 24px;
            padding: 16px 20px;
            background: var(--bg-surface);
            border-radius: 12px;
            border: 1px solid var(--border);
        }
        
        .stat {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }
        
        .stat-value {
            font-size: 28px;
            font-weight: 700;
            font-family: 'IBM Plex Mono', monospace;
        }
        
        .stat-label {
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .stat-value.green { color: var(--green); }
        .stat-value.yellow { color: var(--yellow); }
        .stat-value.red { color: var(--red); }
        
        .node-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
            gap: 12px;
        }
                    .partition-section {
                        background: rgba(255,255,255,0.02);
                        border: 1px solid rgba(255,255,255,0.06);
                        border-radius: 12px;
                        padding: 20px;
                        margin-bottom: 24px;
                    }
                    .partition-header {
                        margin-bottom: 16px;
                    }
                    .partition-title {
                        display: flex;
                        align-items: baseline;
                        gap: 12px;
                        margin-bottom: 8px;
                    }
                    .partition-name {
                        font-size: 18px;
                        font-weight: 600;
                        color: #e0e0e0;
                    }
                    .partition-type {
                        font-size: 13px;
                        color: #808080;
                    }
                    .partition-count {
                        font-size: 13px;
                        color: #a0a0a0;
                        margin-left: auto;
                    }
                    .partition-down {
                        color: #ef4444;
                    }
                    .partition-stats {
                        font-size: 12px;
                        color: #808080;
                        margin-bottom: 12px;
                    }
                    .partition-jobs {
                        display: flex;
                        gap: 12px;
                    }
                    .partition-bars {
                        display: flex;
                        gap: 16px;
                        flex-wrap: wrap;
                    }
                    .util-bar {
                        display: flex;
                        align-items: center;
                        gap: 8px;
                        min-width: 180px;
                    }
                    .util-label {
                        font-size: 12px;
                        color: #808080;
                        width: 50px;
                    }
                    .util-track {
                        flex: 1;
                        height: 6px;
                        background: rgba(255,255,255,0.1);
                        border-radius: 3px;
                        overflow: hidden;
                        min-width: 80px;
                    }
                    .util-fill {
                        height: 100%;
                        border-radius: 3px;
                        transition: width 0.3s;
                    }
                    .util-fill.cpu { background: linear-gradient(90deg, #22c55e, #4ade80); }
                    .util-fill.mem { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
                    .util-fill.gpu { background: linear-gradient(90deg, #8b5cf6, #a78bfa); }
                    .util-fill.gpu-real { background: linear-gradient(90deg, #0072B2, #56B4E9); }
                    /* Okabe-Ito workload badge colors */
                    .workload-badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 500; margin-top: 2px; }
                    .workload-badge.tensor-heavy { background: rgba(0,114,178,0.15); color: #0072B2; border: 1px solid rgba(0,114,178,0.3); }
                    .workload-badge.tensor       { background: rgba(86,180,233,0.15); color: #0072B2; border: 1px solid rgba(86,180,233,0.3); }
                    .workload-badge.fp64         { background: rgba(0,158,115,0.15); color: #009E73; border: 1px solid rgba(0,158,115,0.3); }
                    .workload-badge.memory       { background: rgba(230,159,0,0.15); color: #b87a00; border: 1px solid rgba(230,159,0,0.3); }
                    .workload-badge.compute      { background: rgba(204,121,167,0.15); color: #CC79A7; border: 1px solid rgba(204,121,167,0.3); }
                    .workload-badge.io           { background: rgba(213,94,0,0.15); color: #D55E00; border: 1px solid rgba(213,94,0,0.3); }
                    .workload-badge.idle         { background: rgba(128,128,128,0.1); color: #888; border: 1px solid rgba(128,128,128,0.2); }
                    .workload-badge.other        { background: rgba(128,128,128,0.1); color: #888; border: 1px solid rgba(128,128,128,0.2); }
                    .health-badge { font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 4px; margin-left: 6px; vertical-align: middle; }
                    .health-badge.WARN { background: rgba(230,159,0,0.2); color: #b87a00; border: 1px solid rgba(230,159,0,0.4); }
                    .health-badge.HOT  { background: rgba(213,94,0,0.2);  color: #D55E00; border: 1px solid rgba(213,94,0,0.4); }
                    .health-badge.CRIT { background: rgba(204,0,0,0.2);   color: #cc0000; border: 1px solid rgba(204,0,0,0.4); }
                    .dcgm-badge { font-size: 9px; font-weight: 600; padding: 1px 4px; border-radius: 3px; margin-left: 4px; background: rgba(0,114,178,0.1); color: #0072B2; border: 1px solid rgba(0,114,178,0.25); vertical-align: middle; letter-spacing: 0.03em; }
                    .util-value {
                        font-size: 12px;
                        color: #a0a0a0;
                        width: 36px;
                        text-align: right;
                    }

        
        .node-card {
            background: var(--bg-surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            cursor: pointer;
            transition: all 0.15s;
            text-align: center;
        }
        
        .node-card:hover {
            background: var(--bg-elevated);
            border-color: var(--cyan);
            transform: translateY(-2px);
        }
        
        .node-card.selected {
            border-color: var(--cyan);
            box-shadow: 0 0 0 1px var(--cyan);
        }
        
        .node-card.down { opacity: 0.5; }
        
        .node-name {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 11px;
            color: var(--text-secondary);
            margin-bottom: 8px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        
        .node-indicator {
            width: 48px;
            height: 48px;
            border-radius: 50%;
            margin: 0 auto 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            font-weight: 600;
            font-family: 'IBM Plex Mono', monospace;
        }
        
        .node-indicator.green {
            background: rgba(63, 185, 80, 0.15);
            border: 2px solid var(--green);
            color: var(--green);
        }
        
        .node-indicator.yellow {
            background: rgba(210, 153, 34, 0.15);
            border: 2px solid var(--yellow);
            color: var(--yellow);
        }
        
        .node-indicator.red {
            background: rgba(248, 81, 73, 0.15);
            border: 2px solid var(--red);
            color: var(--red);
        }
        
        .node-indicator.offline {
            background: var(--bg-hover);
            border: 2px solid var(--text-muted);
            color: var(--text-muted);
        }
        
        .node-jobs {
            font-size: 11px;
            color: var(--text-muted);
        }
        
        .node-gpu-badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 9px;
            font-weight: 600;
            background: rgba(163, 113, 247, 0.2);
            color: var(--purple);
            margin-top: 4px;
        }
        
        .sidebar {
            width: 380px;
            background: var(--bg-surface);
            border-left: 1px solid var(--border);
            padding: 24px;
            overflow-y: auto;
        }
        
        .sidebar-empty {
            height: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: var(--text-muted);
            text-align: center;
            gap: 12px;
        }
        
        .sidebar-empty-icon { font-size: 48px; opacity: 0.3; }
        
        .node-detail-header {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border);
        }
        
        .node-detail-name {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 20px;
            font-weight: 600;
        }
        
        .node-status-badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .node-status-badge.online {
            background: rgba(63, 185, 80, 0.15);
            color: var(--green);
        }
        
        .node-status-badge.down {
            background: rgba(248, 81, 73, 0.15);
            color: var(--red);
        }
        
        .detail-section { margin-bottom: 20px; }
        
        .detail-section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            margin-bottom: 12px;
        }
        
        .detail-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 0;
            border-bottom: 1px solid var(--border);
        }
        
        .detail-row:last-child { border-bottom: none; }
        
        .detail-label {
            color: var(--text-secondary);
            font-size: 13px;
        }
        
        .detail-value {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 13px;
            font-weight: 500;
        }
        
        .detail-value.green { color: var(--green); }
        .detail-value.red { color: var(--red); }
        
        .progress-bar {
            height: 6px;
            background: var(--bg-hover);
            border-radius: 3px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s;
        }
        
        .progress-fill.green { background: var(--green); }
        .progress-fill.yellow { background: var(--yellow); }
        .progress-fill.red { background: var(--red); }
        .progress-fill.cyan { background: var(--cyan); }
        .progress-fill.purple { background: var(--purple); }
        
        .failure-list {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        
        .failure-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
        }
        
        .failure-count {
            font-family: 'IBM Plex Mono', monospace;
            font-weight: 600;
            color: var(--red);
            min-width: 24px;
        }
        
        .failure-type { color: var(--text-secondary); }
        
        .user-list {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        
        .user-item {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .user-avatar {
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: var(--bg-hover);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-secondary);
        }
        
        .user-name { flex: 1; font-size: 13px; }
        
        .user-jobs {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 12px;
            color: var(--text-muted);
        }
        
        .network-container {
            width: 100%;
            height: calc(100vh - 180px);
            background: var(--bg-surface);
            border-radius: 12px;
            border: 1px solid var(--border);
            position: relative;
            overflow: hidden;
        }
        
        .network-canvas { width: 100%; height: 100%; }
        
        .network-controls {
            position: absolute;
            top: 16px;
            left: 16px;
            display: flex;
            gap: 8px;
        }
        
        .network-btn {
            padding: 8px 14px;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-primary);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s;
        }
        
        .network-btn:hover {
            background: var(--bg-hover);
            border-color: var(--cyan);
        }
        
        .network-btn.active {
            background: var(--cyan);
            color: var(--bg-deep);
            border-color: var(--cyan);
        }
        
        .network-legend {
            position: absolute;
            bottom: 16px;
            right: 16px;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 16px;
            font-size: 12px;
        }
        
        .legend-item {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 6px;
        }
        
        .legend-item:last-child { margin-bottom: 0; }
        
        .legend-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }
        
        .legend-dot.success { background: var(--green); }
        .legend-dot.failed { background: var(--red); }
        
        .network-stats {
            position: absolute;
            top: 16px;
            right: 16px;
            background: var(--bg-elevated);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 16px;
            font-size: 12px;
        }
        
        .network-stat {
            display: flex;
            justify-content: space-between;
            gap: 24px;
            margin-bottom: 4px;
        }
        
        .network-stat:last-child { margin-bottom: 0; }
        
        .network-stat-value {
            font-family: 'IBM Plex Mono', monospace;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div id="root"></div>
    
    <script type="text/babel">
        const { useState, useEffect, useRef, useMemo } = React;
        

            const eduStyles = {
                panel: { padding: '24px', maxWidth: '1200px', margin: '0 auto' },
                filterBar: { display: 'flex', gap: '12px', marginBottom: '24px', flexWrap: 'wrap' },
                select: { padding: '8px 12px', borderRadius: '6px', border: '1px solid var(--border)', background: 'var(--bg-hover)', color: 'var(--text-primary)', fontSize: '14px', cursor: 'pointer' },
                cards: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '16px', marginBottom: '32px' },
                card: { background: 'var(--bg-surface)', borderRadius: '10px', padding: '20px', textAlign: 'center', border: '1px solid var(--border)' },
                cardValue: { fontSize: '28px', fontWeight: '700', color: 'var(--text-primary)', fontVariantNumeric: 'tabular-nums' },
                cardLabel: { fontSize: '12px', color: 'var(--text-muted)', marginTop: '4px', textTransform: 'uppercase', letterSpacing: '0.5px' },
                section: { fontSize: '16px', fontWeight: '600', color: 'var(--text-secondary)', marginBottom: '16px', marginTop: '8px' },
                barRow: { display: 'flex', alignItems: 'center', marginBottom: '8px', gap: '12px' },
                barLabel: { width: '140px', fontSize: '13px', color: 'var(--text-secondary)', textAlign: 'right', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
                barTrack: { flex: 1, height: '22px', background: 'var(--bg-surface)', borderRadius: '4px', overflow: 'hidden' },
                barFill: { height: '100%', background: 'linear-gradient(90deg, #22c55e, #4ade80)', borderRadius: '4px', transition: 'width 0.3s', minWidth: '2px' },
                barValue: { width: '160px', fontSize: '12px', color: 'var(--text-muted)', flexShrink: 0 },
                table: { width: '100%', borderCollapse: 'collapse', fontSize: '13px' },
                th: { textAlign: 'left', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.5px', cursor: 'pointer', userSelect: 'none' },
                thNum: { textAlign: 'right', padding: '10px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)', fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.5px', cursor: 'pointer', userSelect: 'none' },
                td: { padding: '8px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)' },
                tdNum: { padding: '8px 12px', borderBottom: '1px solid var(--border)', color: 'var(--text-secondary)', textAlign: 'right', fontVariantNumeric: 'tabular-nums' },
                loading: { padding: '60px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '15px' },
                hmRow: { display: 'flex', gap: '2px', marginBottom: '2px' },
                hmDayLabel: { width: '40px', fontSize: '11px', color: 'var(--text-muted)', textAlign: 'right', paddingRight: '8px', lineHeight: '20px', flexShrink: 0 },
                hmCell: { width: '100%', maxWidth: '40px', height: '20px', borderRadius: '3px', cursor: 'default', flex: 1 },
                hmHeader: { display: 'flex', gap: '2px', marginBottom: '4px' },
                hmHourLabel: { width: '100%', maxWidth: '40px', fontSize: '9px', color: 'var(--text-muted)', textAlign: 'center', flex: 1 },
                hmLabelCell: { width: '40px', paddingRight: '8px', flexShrink: 0 },
                legend: { display: 'flex', alignItems: 'center', gap: '8px', marginTop: '16px', justifyContent: 'center' },
                legendLabel: { fontSize: '11px', color: 'var(--text-muted)' },
                legendBar: { display: 'flex', gap: '1px', borderRadius: '3px', overflow: 'hidden' },
            };

            const ResourcesPanel = () => {
                const [data, setData] = useState(null);
                const [filters, setFilters] = useState({cluster: 'all', group: 'all', days: '30'});
                const [sort, setSort] = useState({by: 'cpu_hours', dir: 'desc'});
                useEffect(() => {
                    const {cluster, group, days} = filters;
                    fetch('/api/footprint?cluster=' + cluster + '&group=' + group + '&days=' + days)
                        .then(r => r.json()).then(setData).catch(() => setData(null));
                }, [filters]);
                if (!data) return React.createElement('div', {style: eduStyles.loading}, 'Loading resource data...');
                const maxCpu = Math.max(...data.groups.map(g => g.cpu_hours), 1);
                const sorted_users = [...(data.users || [])].sort((a, b) => {
                    if (sort.by === 'username') return sort.dir === 'asc' ? a.username.localeCompare(b.username) : b.username.localeCompare(a.username);
                    return sort.dir === 'desc' ? (b[sort.by] || 0) - (a[sort.by] || 0) : (a[sort.by] || 0) - (b[sort.by] || 0);
                });
                const doSort = (col) => setSort({by: col, dir: sort.by === col && sort.dir === 'desc' ? 'asc' : 'desc'});
                const arrow = (col) => sort.by === col ? (sort.dir === 'asc' ? ' ^' : ' v') : '';
                return React.createElement('div', {style: eduStyles.panel},
                    React.createElement('div', {style: eduStyles.filterBar},
                        React.createElement('select', {value: filters.cluster, onChange: e => setFilters({...filters, cluster: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: 'all'}, 'All Clusters'),
                            (data.filters.clusters || []).map(c => React.createElement('option', {key: c, value: c}, c))
                        ),
                        React.createElement('select', {value: filters.group, onChange: e => setFilters({...filters, group: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: 'all'}, 'All Groups'),
                            (data.filters.groups || []).map(g => React.createElement('option', {key: g, value: g}, g))
                        ),
                        React.createElement('select', {value: filters.days, onChange: e => setFilters({...filters, days: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: '7'}, 'Last 7 days'),
                            React.createElement('option', {value: '30'}, 'Last 30 days'),
                            React.createElement('option', {value: '90'}, 'Last 90 days'),
                            React.createElement('option', {value: '365'}, 'Last year')
                        )
                    ),
                    React.createElement('div', {style: eduStyles.cards},
                        React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, Math.round(data.totals.cpu_hours).toLocaleString()),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'CPU-hours')
                        ),
                        React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, Math.round(data.totals.gpu_hours).toLocaleString()),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'GPU-hours')
                        ),
                        React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, (data.totals.jobs || 0).toLocaleString()),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'Jobs')
                        ),
                        React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, data.totals.users || 0),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'Users')
                        )
                    ),
                    data.groups.length > 0 && React.createElement('div', null,
                        React.createElement('div', {style: eduStyles.section}, 'Resource Usage by Group'),
                        React.createElement('div', {style: {marginBottom: '32px'}},
                            data.groups.map(g => React.createElement('div', {key: g.name, style: eduStyles.barRow},
                                React.createElement('div', {style: eduStyles.barLabel, title: g.name}, g.name),
                                React.createElement('div', {style: eduStyles.barTrack},
                                    React.createElement('div', {style: {...eduStyles.barFill, width: (g.cpu_hours / maxCpu * 100) + '%'}})
                                ),
                                React.createElement('div', {style: eduStyles.barValue},
                                    Math.round(g.cpu_hours).toLocaleString() + ' CPU-hrs' +
                                    (g.gpu_hours > 0 ? ' / ' + Math.round(g.gpu_hours).toLocaleString() + ' GPU-hrs' : '') +
                                    ' (' + g.users + ' users)'
                                )
                            ))
                        )
                    ),
                    React.createElement('div', {style: eduStyles.section}, 'User Breakdown'),
                    React.createElement('table', {style: eduStyles.table},
                        React.createElement('thead', null,
                            React.createElement('tr', null,
                                React.createElement('th', {style: eduStyles.th, onClick: () => doSort('username')}, 'User' + arrow('username')),
                                React.createElement('th', {style: eduStyles.thNum, onClick: () => doSort('cpu_hours')}, 'CPU-hrs' + arrow('cpu_hours')),
                                React.createElement('th', {style: eduStyles.thNum, onClick: () => doSort('gpu_hours')}, 'GPU-hrs' + arrow('gpu_hours')),
                                React.createElement('th', {style: eduStyles.thNum, onClick: () => doSort('jobs')}, 'Jobs' + arrow('jobs')),
                                React.createElement('th', {style: eduStyles.th}, 'Groups')
                            )
                        ),
                        React.createElement('tbody', null,
                            sorted_users.slice(0, 50).map(u => React.createElement('tr', {key: u.username + u.cluster},
                                React.createElement('td', {style: eduStyles.td}, u.username),
                                React.createElement('td', {style: eduStyles.tdNum}, Math.round(u.cpu_hours).toLocaleString()),
                                React.createElement('td', {style: eduStyles.tdNum}, Math.round(u.gpu_hours).toLocaleString()),
                                React.createElement('td', {style: eduStyles.tdNum}, u.jobs),
                                React.createElement('td', {style: eduStyles.td}, (u.groups || []).join(', '))
                            ))
                        )
                    )
                );
            };

            const ActivityPanel = () => {
                const [data, setData] = useState(null);
                const [filters, setFilters] = useState({cluster: 'all', group: 'all', days: '30'});
                useEffect(() => {
                    const {cluster, group, days} = filters;
                    fetch('/api/heatmap?cluster=' + cluster + '&group=' + group + '&days=' + days)
                        .then(r => r.json()).then(setData).catch(() => setData(null));
                }, [filters]);
                if (!data) return React.createElement('div', {style: eduStyles.loading}, 'Loading activity data...');
                const dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
                const getColor = (v) => {
                    if (!v) return 'rgba(255,255,255,0.03)';
                    const i = Math.min(v / (data.max_value || 1), 1);
                    return 'rgb(' + Math.round(20 + i * 20) + ',' + Math.round(40 + i * 180) + ',' + Math.round(20 + i * 60) + ')';
                };
                return React.createElement('div', {style: eduStyles.panel},
                    React.createElement('div', {style: eduStyles.filterBar},
                        React.createElement('select', {value: filters.cluster, onChange: e => setFilters({...filters, cluster: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: 'all'}, 'All Clusters'),
                            (data.filters.clusters || []).map(c => React.createElement('option', {key: c, value: c}, c))
                        ),
                        React.createElement('select', {value: filters.group, onChange: e => setFilters({...filters, group: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: 'all'}, 'All Groups'),
                            (data.filters.groups || []).map(g => React.createElement('option', {key: g, value: g}, g))
                        ),
                        React.createElement('select', {value: filters.days, onChange: e => setFilters({...filters, days: e.target.value}), style: eduStyles.select},
                            React.createElement('option', {value: '7'}, 'Last 7 days'),
                            React.createElement('option', {value: '30'}, 'Last 30 days'),
                            React.createElement('option', {value: '90'}, 'Last 90 days'),
                            React.createElement('option', {value: '365'}, 'Last year')
                        )
                    ),
                    React.createElement('div', {style: eduStyles.cards},
                        React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, (data.total_jobs || 0).toLocaleString()),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'Total Jobs')
                        ),
                        data.busiest && React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, data.busiest.day + ' ' + data.busiest.hour + ':00'),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'Busiest Hour (' + data.busiest.count + ' jobs)')
                        ),
                        data.quietest && React.createElement('div', {style: eduStyles.card},
                            React.createElement('div', {style: eduStyles.cardValue}, data.quietest.day + ' ' + data.quietest.hour + ':00'),
                            React.createElement('div', {style: eduStyles.cardLabel}, 'Quietest Hour')
                        )
                    ),
                    React.createElement('div', {style: eduStyles.section}, 'Job Submissions by Day and Hour'),
                    React.createElement('div', {style: {marginBottom: '32px'}},
                        React.createElement('div', {style: eduStyles.hmHeader},
                            React.createElement('div', {style: eduStyles.hmLabelCell}),
                            Array.from({length: 24}, (_, i) => React.createElement('div', {key: i, style: eduStyles.hmHourLabel}, i % 3 === 0 ? i + 'h' : ''))
                        ),
                        data.grid.map((row, di) => React.createElement('div', {key: di, style: eduStyles.hmRow},
                            React.createElement('div', {style: eduStyles.hmDayLabel}, dayNames[di]),
                            row.map((v, hi) => React.createElement('div', {
                                key: hi,
                                style: {...eduStyles.hmCell, backgroundColor: getColor(v)},
                                title: dayNames[di] + ' ' + hi + ':00 -- ' + v + ' jobs'
                            }))
                        ))
                    ),
                    React.createElement('div', {style: eduStyles.legend},
                        React.createElement('span', {style: eduStyles.legendLabel}, 'Less'),
                        React.createElement('div', {style: eduStyles.legendBar},
                            [0, 0.2, 0.4, 0.6, 0.8, 1.0].map(i => React.createElement('div', {
                                key: i,
                                style: {width: '16px', height: '12px', backgroundColor: 'rgb(' + Math.round(20+i*20) + ',' + Math.round(40+i*180) + ',' + Math.round(20+i*60) + ')'}
                            }))
                        ),
                        React.createElement('span', {style: eduStyles.legendLabel}, 'More')
                    )
                );
            };


            // ═══════════════════════════════════════════════════════════
            // Workstations Panel
            // ═══════════════════════════════════════════════════════════
            const WorkstationsPanel = () => {
                const [workstations, setWorkstations] = useState(null);
                const [expandedWorkstations, setExpandedWorkstations] = useState({});
                const [userDataByHost, setUserDataByHost] = useState({});
                const [hideSystemByHost, setHideSystemByHost] = useState({});
                const [mountDataByHost, setMountDataByHost] = useState({});
                const [hideLocalMountsByHost, setHideLocalMountsByHost] = useState({});
                const isNetworkMount = (m) => {
                    const fs = (m.fstype || "").toLowerCase();
                    if (fs.startsWith("nfs")) return true;
                    if (fs.startsWith("cifs")) return true;
                    if (fs.startsWith("smb")) return true;
                    if (fs.startsWith("fuse.")) return true;
                    if ((m.source || "").indexOf(":") !== -1) return true;
                    return false;
                };
                useEffect(() => {
                    fetch("/api/workstations")
                        .then(r => r.json())
                        .then(setWorkstations)
                        .catch(() => setWorkstations({workstations: [], summary: {}}));
                }, []);
                const toggleExpandWs = (hostname) => {
                    const isOpening = !expandedWorkstations[hostname];
                    setExpandedWorkstations(prev => ({...prev, [hostname]: isOpening}));
                    if (isOpening && !userDataByHost[hostname]) {
                        fetch("/api/workstation_users?hostname=" + encodeURIComponent(hostname))
                            .then(r => r.json())
                            .then(d => setUserDataByHost(prev => ({...prev, [hostname]: d.users || []})))
                            .catch(() => setUserDataByHost(prev => ({...prev, [hostname]: []})));
                    }
                    if (isOpening && !mountDataByHost[hostname]) {
                        fetch("/api/workstation_mounts?hostname=" + encodeURIComponent(hostname))
                            .then(r => r.json())
                            .then(d => setMountDataByHost(prev => ({...prev, [hostname]: d.mounts || []})))
                            .catch(() => setMountDataByHost(prev => ({...prev, [hostname]: []})));
                    }
                };
                const mountStatusLabel = (m) => {
                    if (m.is_mounted === 0) return {text: "unmounted", color: "#f87171"};
                    if (m.is_responsive === 0) return {text: "stale", color: "#f5a623"};
                    if (m.response_ms !== null && m.response_ms !== undefined && m.response_ms > 500) {
                        return {text: "slow", color: "#f5a623"};
                    }
                    return {text: "ok", color: "#4ade80"};
                };
                const isSystemUserWs = (u) => {
                    if (u.uid !== null && u.uid !== undefined && u.uid < 1000) return true;
                    const systemNames = ['root','daemon','bin','sys','sync','games','man','lp','mail','news','uucp','proxy','www-data','backup','list','irc','gnats','nobody','systemd-network','systemd-resolve','systemd-timesync','messagebus','sshd','polkitd','chrony','avahi','colord','rtkit','pulse','gdm','lightdm','dnsmasq','tcpdump','uuidd','named','postfix','cockpit-ws','cockpit-wsinstance','nm-openvpn','nm-openconnect'];
                    return systemNames.indexOf(u.username) !== -1;
                };
                const humanBytesWs = (b) => {
                    if (!b && b !== 0) return '—';
                    if (b < 1024) return b + ' B';
                    if (b < 1024*1024) return (b/1024).toFixed(1) + ' KB';
                    if (b < 1024*1024*1024) return (b/(1024*1024)).toFixed(1) + ' MB';
                    return (b/(1024*1024*1024)).toFixed(2) + ' GB';
                };
                const humanAgeWs = (epochSec) => {
                    if (!epochSec) return '—';
                    const now = Math.floor(Date.now() / 1000);
                    const age = now - epochSec;
                    if (age < 0) return 'future?';
                    if (age < 60) return age + 's';
                    if (age < 3600) return Math.floor(age/60) + 'm';
                    if (age < 86400) return Math.floor(age/3600) + 'h ' + Math.floor((age%3600)/60) + 'm';
                    return Math.floor(age/86400) + 'd ' + Math.floor((age%86400)/3600) + 'h';
                };
                if (!workstations) return React.createElement("div", {style: eduStyles.loading}, "Loading workstations...");
                const {workstations: ws = [], summary = {}} = workstations;
                
                // Group by department
                const byDept = ws.reduce((acc, w) => {
                    const dept = w.department || "Uncategorized";
                    if (!acc[dept]) acc[dept] = [];
                    acc[dept].push(w);
                    return acc;
                }, {});
                
                const statusColor = (status) => {
                    if (status === "online") return "#4ade80";
                    if (status === "degraded") return "#f5a623";
                    return "#f87171";
                };
                
                return React.createElement("div", {style: eduStyles.panel},
                    React.createElement("div", {style: eduStyles.section}, "Workstation Overview"),
                    React.createElement("div", {style: eduStyles.cards},
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, summary.total || ws.length),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Total Workstations")
                        ),
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#4ade80"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#4ade80"}}, summary.online || ws.filter(w => w.status === "online").length),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Online")
                        ),
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#f5a623"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#f5a623"}}, summary.degraded || ws.filter(w => w.status === "degraded").length),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Degraded")
                        ),
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#f87171"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#f87171"}}, summary.offline || ws.filter(w => w.status === "offline").length),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Offline")
                        )
                    ),
                    Object.entries(byDept).map(([dept, machines]) =>
                        React.createElement("div", {key: dept, style: {marginTop: "1.5rem"}},
                            React.createElement("div", {style: {...eduStyles.section, fontSize: "1rem"}}, dept + " (" + machines.length + ")"),
                            React.createElement("table", {style: eduStyles.table},
                                React.createElement("thead", null,
                                    React.createElement("tr", null,
                                        React.createElement("th", {style: eduStyles.th}, "Hostname"),
                                        React.createElement("th", {style: eduStyles.th}, "Status"),
                                        React.createElement("th", {style: eduStyles.th}, "OS"),
                                        React.createElement("th", {style: eduStyles.th}, "CPU Model"),
                                        React.createElement("th", {style: eduStyles.th}, "CPU Load"),
                                        React.createElement("th", {style: eduStyles.th}, "Memory"),
                                        React.createElement("th", {style: eduStyles.th}, "Disk"),
                                        React.createElement("th", {style: eduStyles.th}, "Users")
                                    )
                                ),
                                React.createElement("tbody", null,
                                    machines.flatMap((w, i) => {
                                        const isExpanded = !!expandedWorkstations[w.hostname];
                                        const chevron = isExpanded ? "▼" : "▶";
                                        const mainRow = React.createElement("tr", {
                                            key: "row-" + i,
                                            style: {cursor: "pointer"},
                                            onClick: () => toggleExpandWs(w.hostname),
                                        },
                                            React.createElement("td", {style: eduStyles.td},
                                                React.createElement("span", {style: {marginRight: "0.4rem", opacity: 0.6, fontSize: "0.75rem"}}, chevron),
                                                w.hostname
                                            ),
                                            React.createElement("td", {style: eduStyles.td},
                                                React.createElement("span", {style: {color: statusColor(w.status)}}, w.status)
                                            ),
                                            React.createElement("td", {style: {...eduStyles.td, fontSize: "0.75rem", maxWidth: 120}}, w.os_version || "-"),
                                            React.createElement("td", {style: {...eduStyles.td, fontSize: "0.75rem", maxWidth: 160}}, w.cpu_model || "-"),
                                            React.createElement("td", {style: eduStyles.td}, (w.load_avg_1m || 0).toFixed(2) + " / " + (w.cpu_count || "?")),
                                            React.createElement("td", {style: eduStyles.td},
                                                w.memory_total_mb ? Math.round(w.memory_used_mb / w.memory_total_mb * 100) + "%" : "N/A"
                                            ),
                                            React.createElement("td", {style: eduStyles.td}, (w.disk_usage_pct || 0).toFixed(1) + "%"),
                                            React.createElement("td", {style: eduStyles.td}, w.users_logged_in || 0)
                                        );
                                        if (!isExpanded) return [mainRow];
                                        const users = userDataByHost[w.hostname];
                                        const hideSystem = hideSystemByHost[w.hostname] !== false;
                                        let content;
                                        if (!users) {
                                            content = React.createElement("div", {style: {padding: "0.5rem", opacity: 0.6, fontSize: "0.8rem"}}, "Loading per-user data...");
                                        } else if (users.length === 0) {
                                            // Truly no rows for this host. Probe not deployed or collector
                                            // hasn't captured anything yet.
                                            content = React.createElement("div", {style: {padding: "0.5rem", opacity: 0.6, fontSize: "0.8rem"}}, "No per-user data recorded yet. Probe may not be deployed on " + w.hostname + ", or the collector hasn't run since deploy.");
                                        } else {
                                            const visible = hideSystem ? users.filter(u => !isSystemUserWs(u)) : users;
                                            const sysCount = users.filter(isSystemUserWs).length;
                                            const thStyle = {...eduStyles.th, padding: "0.35rem 0.5rem", fontSize: "0.72rem"};
                                            const tdStyle = {...eduStyles.td, padding: "0.35rem 0.5rem", fontSize: "0.8rem"};
                                            const mounts = mountDataByHost[w.hostname];
                                            const hideLocal = hideLocalMountsByHost[w.hostname] !== false;
                                            const netMounts = mounts ? mounts.filter(isNetworkMount) : null;
                                            const localMountCount = mounts ? (mounts.length - (netMounts ? netMounts.length : 0)) : 0;
                                            const visibleMounts = !mounts
                                                ? null
                                                : (hideLocal ? netMounts : mounts);
                                            const staleCount = mounts ? mounts.filter(m => m.is_mounted && !m.is_responsive).length : 0;
                                            const mountsSection = React.createElement("div", {style: {marginBottom: "0.75rem"}},
                                                React.createElement("div", {style: {display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem", fontSize: "0.8rem", opacity: 0.75}},
                                                    React.createElement("span", null,
                                                        mounts === undefined
                                                            ? "Loading mounts..."
                                                            : mounts.length === 0
                                                                ? "Mounts: (no data; mount probe may not be deployed)"
                                                                : (
                                                                    "Mounts on " + w.hostname + ": "
                                                                    + (visibleMounts ? visibleMounts.length : 0)
                                                                    + (hideLocal && localMountCount > 0 ? " (+ " + localMountCount + " local hidden)" : "")
                                                                    + (staleCount > 0 ? " — " + staleCount + " unresponsive" : "")
                                                                )
                                                    ),
                                                    mounts && mounts.length > 0 && React.createElement("label", {style: {cursor: "pointer", fontSize: "0.75rem"}, onClick: (e) => e.stopPropagation()},
                                                        React.createElement("input", {
                                                            type: "checkbox",
                                                            checked: !hideLocal,
                                                            onChange: (e) => setHideLocalMountsByHost(prev => ({...prev, [w.hostname]: e.target.checked})),
                                                            style: {marginRight: "0.3rem"}
                                                        }),
                                                        "show local mounts"
                                                    )
                                                ),
                                                visibleMounts && visibleMounts.length > 0 && React.createElement("table", {style: {...eduStyles.table, marginTop: 0, fontSize: "0.8rem"}},
                                                    React.createElement("thead", null,
                                                        React.createElement("tr", null,
                                                            React.createElement("th", {style: thStyle}, "Mountpoint"),
                                                            React.createElement("th", {style: thStyle}, "Type"),
                                                            React.createElement("th", {style: thStyle}, "Source"),
                                                            React.createElement("th", {style: thStyle}, "Status"),
                                                            React.createElement("th", {style: thStyle}, "Latency")
                                                        )
                                                    ),
                                                    React.createElement("tbody", null,
                                                        visibleMounts.map((m, k) => {
                                                            const st = mountStatusLabel(m);
                                                            return React.createElement("tr", {key: "m-" + k},
                                                                React.createElement("td", {style: tdStyle}, m.mountpoint),
                                                                React.createElement("td", {style: tdStyle}, m.fstype || "-"),
                                                                React.createElement("td", {style: {...tdStyle, fontSize: "0.72rem", opacity: 0.75, maxWidth: 220}}, m.source || "-"),
                                                                React.createElement("td", {style: tdStyle},
                                                                    React.createElement("span", {style: {color: st.color}}, st.text)
                                                                ),
                                                                React.createElement("td", {style: tdStyle}, (m.response_ms !== null && m.response_ms !== undefined) ? m.response_ms.toFixed(1) + " ms" : "-")
                                                            );
                                                        })
                                                    )
                                                ),
                                                mounts && mounts.length > 0 && visibleMounts && visibleMounts.length === 0 && React.createElement("div", {style: {opacity: 0.5, fontSize: "0.8rem", padding: "0.25rem 0"}},
                                                    hideLocal && localMountCount > 0
                                                        ? ("No network mounts on this host (" + localMountCount + " local mount" + (localMountCount === 1 ? "" : "s") + " hidden — toggle above to view).")
                                                        : "No mounts to display."
                                                )
                                            );
                                            content = React.createElement("div", null,
                                                mountsSection,
                                                React.createElement("div", {style: {display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem", fontSize: "0.8rem", opacity: 0.75}},
                                                    React.createElement("span", null, "Users on " + w.hostname + ": " + visible.length + (hideSystem && sysCount > 0 ? " (+ " + sysCount + " system hidden)" : "")),
                                                    React.createElement("label", {style: {cursor: "pointer", fontSize: "0.75rem"}, onClick: (e) => e.stopPropagation()},
                                                        React.createElement("input", {
                                                            type: "checkbox",
                                                            checked: !hideSystem,
                                                            onChange: (e) => setHideSystemByHost(prev => ({...prev, [w.hostname]: e.target.checked})),
                                                            style: {marginRight: "0.3rem"}
                                                        }),
                                                        "show system users"
                                                    )
                                                ),
                                                visible.length === 0
                                                    ? React.createElement("div", {style: {opacity: 0.5, fontSize: "0.8rem", padding: "0.5rem 0"}},
                                                        hideSystem && sysCount > 0
                                                            ? "No human users logged in (" + sysCount + " system user" + (sysCount === 1 ? "" : "s") + " hidden — toggle above to view)."
                                                            : "No user sessions on this host right now."
                                                      )
                                                    : React.createElement("table", {style: {...eduStyles.table, marginTop: 0, fontSize: "0.8rem"}},
                                                        React.createElement("thead", null,
                                                            React.createElement("tr", null,
                                                                React.createElement("th", {style: thStyle}, "User"),
                                                                React.createElement("th", {style: thStyle}, "UID"),
                                                                React.createElement("th", {style: thStyle}, "Memory"),
                                                                React.createElement("th", {style: thStyle}, "Peak Memory"),
                                                                React.createElement("th", {style: thStyle}, "PIDs"),
                                                                React.createElement("th", {style: thStyle}, "Session Age")
                                                            )
                                                        ),
                                                        React.createElement("tbody", null,
                                                            visible.map((u, j) => React.createElement("tr", {key: "u-" + j},
                                                                React.createElement("td", {style: tdStyle},
                                                                    u.username,
                                                                    isSystemUserWs(u) ? React.createElement("span", {style: {marginLeft: "0.4rem", fontSize: "0.7rem", opacity: 0.5}}, "sys") : null
                                                                ),
                                                                React.createElement("td", {style: tdStyle}, u.uid),
                                                                React.createElement("td", {style: tdStyle}, humanBytesWs(u.memory_current_bytes)),
                                                                React.createElement("td", {style: tdStyle}, humanBytesWs(u.memory_peak_bytes)),
                                                                React.createElement("td", {style: tdStyle}, u.pids_current),
                                                                React.createElement("td", {style: tdStyle}, humanAgeWs(u.session_epoch))
                                                            ))
                                                        )
                                                    )
                                            );
                                        }
                                        const expandedRow = React.createElement("tr", {key: "exp-" + i},
                                            React.createElement("td", {
                                                colSpan: 8,
                                                style: {...eduStyles.td, padding: "0.75rem 1rem", background: "rgba(255,255,255,0.02)", borderTop: "none"}
                                            }, content)
                                        );
                                        return [mainRow, expandedRow];
                                    })
                                )
                            )
                        )
                    )
                );
            };

            // ═══════════════════════════════════════════════════════════
            // Storage Panel
            // ═══════════════════════════════════════════════════════════
            const StoragePanel = () => {
                const [storage, setStorage] = useState(null);
                useEffect(() => {
                    fetch("/api/storage")
                        .then(r => r.json())
                        .then(setStorage)
                        .catch(() => setStorage({devices: [], summary: {}}));
                }, []);
                if (!storage) return React.createElement("div", {style: eduStyles.loading}, "Loading storage devices...");
                const {devices = [], summary = {}} = storage;
                
                const formatBytes = (bytes) => {
                    if (!bytes) return "0 B";
                    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
                    let i = 0;
                    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
                    return bytes.toFixed(1) + " " + units[i];
                };
                
                const usageColor = (pct) => {
                    if (pct >= 95) return "#f87171";
                    if (pct >= 85) return "#f5a623";
                    return "#4ade80";
                };

                // Group devices by server
                const byServer = {};
                devices.forEach(d => {
                    const parts = (d.hostname || "local").split(":");
                    const srv = parts[0];
                    if (!byServer[srv]) byServer[srv] = [];
                    byServer[srv].push(d);
                });
                
                return React.createElement("div", {style: eduStyles.panel},
                    React.createElement("div", {style: eduStyles.section}, "Storage Overview"),
                    React.createElement("div", {style: eduStyles.cards},
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, devices.length),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Storage Devices")
                        ),
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, formatBytes(summary.total_bytes || 0)),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Total Capacity")
                        ),
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, formatBytes(summary.used_bytes || 0)),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Used")
                        )
                    ),
                    React.createElement("div", {style: eduStyles.section}, "Storage by Server"),
                    ...Object.entries(byServer).map(([srv, devs]) =>
                        React.createElement("div", {key: srv, style: {marginBottom: "1.5rem"}},
                            React.createElement("div", {style: {fontWeight: "bold", fontSize: "1.1rem", marginBottom: "0.5rem", opacity: 0.8}}, srv),
                            React.createElement("div", {style: {display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem"}},
                                ...devs.map((dev, i) =>
                                    React.createElement("div", {key: i, style: {
                                        background: "var(--bg-secondary, #1e293b)",
                                        border: "1px solid var(--border)",
                                        borderRadius: "8px",
                                        padding: "1rem"
                                    }},
                                        React.createElement("div", {style: {display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem"}},
                                            React.createElement("span", {style: {fontWeight: "bold"}}, dev.hostname),
                                            React.createElement("span", {style: {
                                                color: dev.status === "online" ? "#4ade80" : "#f87171",
                                                fontWeight: "bold", fontSize: "0.8rem"
                                            }}, (dev.status || "").toUpperCase())
                                        ),
                                        React.createElement("div", {style: {marginBottom: "0.25rem", fontSize: "0.85rem"}},
                                            React.createElement("span", null, "Capacity "),
                                            React.createElement("span", {style: {color: usageColor(dev.usage_pct || 0)}},
                                                formatBytes(dev.used_bytes) + " / " + formatBytes(dev.total_bytes) + " (" + (dev.usage_pct || 0).toFixed(1) + "%)"
                                            )
                                        ),
                                        React.createElement("div", {style: {
                                            background: "var(--border)",
                                            borderRadius: "4px",
                                            height: "8px",
                                            overflow: "hidden"
                                        }},
                                            React.createElement("div", {style: {
                                                background: usageColor(dev.usage_pct || 0),
                                                height: "100%",
                                                width: (dev.usage_pct || 0) + "%",
                                                transition: "width 0.3s"
                                            }})
                                        )
                                    )
                                )
                            )
                        )
                    )
                );
            };

                        // Dynamics Panel

            // Education Panel
            const EducationPanel = () => {
                const [eduView, setEduView] = useState('overview');
                const [eduUsers, setEduUsers] = useState([]);
                const [eduGroups, setEduGroups] = useState([]);
                const [selectedUser, setSelectedUser] = useState('');
                const [selectedGroup, setSelectedGroup] = useState('');
                const [trajData, setTrajData] = useState(null);
                const [groupData, setGroupData] = useState(null);
                const [eduLoading, setEduLoading] = useState(false);
                const [eduCluster, setEduCluster] = useState('all');
                const [eduClusters, setEduClusters] = useState([]);

                useEffect(() => {
                    fetch('/api/data')
                        .then(r => r.json())
                        .then(d => {
                            const cls = Object.keys(d.clusters || {}).filter(
                                k => d.clusters[k].type !== 'workstation');
                            setEduClusters(cls);
                        }).catch(() => {});
                }, []);

                useEffect(() => {
                    setSelectedUser(''); setSelectedGroup('');
                    setTrajData(null); setGroupData(null);
                    setEduView('overview');
                    fetch('/api/edu/users?cluster=' + eduCluster)
                        .then(r => r.json())
                        .then(d => { setEduUsers(d.users || []); setEduGroups(d.groups || []); })
                        .catch(() => {});
                }, [eduCluster]);

                const loadTrajectory = (user) => {
                    setSelectedUser(user);
                    setEduLoading(true);
                    setTrajData(null);
                    fetch('/api/edu/trajectory?user=' + encodeURIComponent(user))
                        .then(r => r.json())
                        .then(d => { setTrajData(d); setEduLoading(false); setEduView('trajectory'); })
                        .catch(() => { setTrajData({error: 'Failed'}); setEduLoading(false); });
                };

                const loadGroup = (group) => {
                    setSelectedGroup(group);
                    setEduLoading(true);
                    setGroupData(null);
                    fetch('/api/edu/group?group=' + encodeURIComponent(group))
                        .then(r => r.json())
                        .then(d => { setGroupData(d); setEduLoading(false); setEduView('group'); })
                        .catch(() => { setGroupData({error: 'Failed'}); setEduLoading(false); });
                };

                const scoreColor = (s) => s >= 80 ? '#22c55e' : s >= 60 ? '#f59e0b' : s >= 40 ? '#B64326' : '#ef4444';
                const impColor = (v) => v > 0 ? '#22c55e' : v < 0 ? '#ef4444' : '#64748b';
                const dims = ['cpu', 'memory', 'time', 'io', 'gpu'];
                const dimLabels = {cpu: 'CPU', memory: 'Memory', time: 'Time', io: 'I/O', gpu: 'GPU'};

                // Overview: select user or group
                const overviewView = React.createElement('div', null,
                    React.createElement('div', {style: {display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16}},
                        React.createElement('div', {className: 'card', style: {padding: 16}},
                            React.createElement('h3', {style: {fontSize: 14, fontWeight: 600, marginBottom: 12, color: 'var(--btn-text)'}}, 'User Trajectory'),
                            React.createElement('select', {
                                value: selectedUser,
                                onChange: e => { if (e.target.value) { loadTrajectory(e.target.value); } else { setSelectedUser(''); setTrajData(null); setEduView('overview'); } },
                                style: eduStyles.select
                            },
                                React.createElement('option', {value: ''}, 'Select a user...'),
                                eduUsers.map(u => React.createElement('option', {key: u, value: u}, u))
                            ),
                            selectedUser && React.createElement('button', {
                                onClick: () => loadTrajectory(selectedUser),
                                style: {marginTop: 8, padding: '6px 16px', background: '#00BACF', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 12}
                            }, 'View Report'),
                            React.createElement('p', {style: {fontSize: 11, color: '#64748b', marginTop: 8}},
                                eduUsers.length + ' users with job history')
                        ),
                        React.createElement('div', {className: 'card', style: {padding: 16}},
                            React.createElement('h3', {style: {fontSize: 14, fontWeight: 600, marginBottom: 12, color: 'var(--btn-text)'}}, 'Group Report'),
                            React.createElement('select', {
                                value: selectedGroup,
                                onChange: e => { if (e.target.value) { loadGroup(e.target.value); } else { setSelectedGroup(''); setGroupData(null); setEduView('overview'); } },
                                style: eduStyles.select
                            },
                                React.createElement('option', {value: ''}, 'Select a group...'),
                                eduGroups.map(g => React.createElement('option', {key: g, value: g}, g))
                            ),
                            selectedGroup && React.createElement('button', {
                                onClick: () => loadGroup(selectedGroup),
                                style: {marginTop: 8, padding: '6px 16px', background: '#00BACF', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', fontSize: 12}
                            }, 'View Report'),
                            React.createElement('p', {style: {fontSize: 11, color: '#64748b', marginTop: 8}},
                                eduGroups.length + ' groups available')
                        )
                    )
                );

                // Trajectory view
                const trajectoryView = trajData && !trajData.error ? React.createElement('div', null,
                    React.createElement('button', {
                        onClick: () => setEduView('overview'),
                        style: {background: 'none', border: 'none', color: '#00BACF', cursor: 'pointer', fontSize: 12, marginBottom: 12}
                    }, '< Back to overview'),
                    React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                        React.createElement('h3', {style: {fontSize: 16, fontWeight: 700, marginBottom: 4}}, trajData.username),
                        React.createElement('div', {style: {fontSize: 12, color: '#64748b', marginBottom: 16}},
                            trajData.total_jobs + ' jobs | ' + (trajData.date_range || []).join(' to ')),
                        React.createElement('div', {style: {display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap'}},
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Overall'),
                                React.createElement('div', {style: {fontSize: 18, fontWeight: 700, color: impColor(trajData.overall_improvement || 0)}},
                                    (trajData.overall_improvement > 0 ? '+' : '') + (trajData.overall_improvement || 0).toFixed(1) + '%')
                            ),
                            ...dims.map(d => {
                                const score = (trajData.current_scores || {})[d];
                                const imp = (trajData.improvement || {})[d];
                                return score !== undefined ? React.createElement('div', {key: d, style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                    React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, dimLabels[d] || d),
                                    React.createElement('div', {style: {fontSize: 18, fontWeight: 700, color: scoreColor(score)}}, score.toFixed(0)),
                                    imp !== undefined ? React.createElement('div', {style: {fontSize: 10, color: impColor(imp)}},
                                        (imp > 0 ? '+' : '') + imp.toFixed(1)) : null
                                ) : null;
                            })
                        ),
                        (trajData.windows || []).length > 0 ? React.createElement('div', null,
                            React.createElement('h4', {style: {fontSize: 12, fontWeight: 600, color: '#94a3b8', marginBottom: 8}}, 'Weekly Windows'),
                            React.createElement('table', {style: {fontSize: 11, borderCollapse: 'collapse', width: '100%'}},
                                React.createElement('thead', null,
                                    React.createElement('tr', null,
                                        React.createElement('th', {style: {padding: '4px 8px', color: '#64748b', textAlign: 'left'}}, 'Period'),
                                        React.createElement('th', {style: {padding: '4px 8px', color: '#64748b', textAlign: 'right'}}, 'Jobs'),
                                        ...dims.map(d => React.createElement('th', {key: d, style: {padding: '4px 8px', color: '#64748b', textAlign: 'right'}}, dimLabels[d] || d))
                                    )
                                ),
                                React.createElement('tbody', null,
                                    trajData.windows.map((w, i) => React.createElement('tr', {key: i},
                                        React.createElement('td', {style: {padding: '4px 8px', color: '#94a3b8'}}, (w.start || '').slice(5, 10) + ' - ' + (w.end || '').slice(5, 10)),
                                        React.createElement('td', {style: {padding: '4px 8px', textAlign: 'right'}}, w.jobs),
                                        ...dims.map(d => {
                                            const s = (w.scores || {})[d];
                                            return React.createElement('td', {key: d, style: {padding: '4px 8px', textAlign: 'right', color: s !== undefined ? scoreColor(s) : '#64748b'}},
                                                s !== undefined ? s.toFixed(0) : '-');
                                        })
                                    ))
                                )
                            )
                        ) : null
                    )
                ) : (trajData && trajData.error ? React.createElement('div', {style: {color: '#ef4444', padding: 16}},
                    React.createElement('button', {onClick: () => setEduView('overview'), style: {background: 'none', border: 'none', color: '#00BACF', cursor: 'pointer', fontSize: 12, marginBottom: 12}}, '< Back'),
                    'Error: ' + trajData.error) : null);

                // Group view
                const groupView = groupData && !groupData.error ? React.createElement('div', null,
                    React.createElement('button', {
                        onClick: () => setEduView('overview'),
                        style: {background: 'none', border: 'none', color: '#00BACF', cursor: 'pointer', fontSize: 12, marginBottom: 12}
                    }, '< Back to overview'),
                    React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                        React.createElement('h3', {style: {fontSize: 16, fontWeight: 700, marginBottom: 4}}, groupData.group_name),
                        React.createElement('div', {style: {fontSize: 12, color: '#64748b', marginBottom: 16}},
                            groupData.member_count + ' members | ' + groupData.total_jobs + ' jobs | ' + (groupData.date_range || []).join(' to ')),
                        React.createElement('div', {style: {display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap'}},
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Avg Score'),
                                React.createElement('div', {style: {fontSize: 20, fontWeight: 700, color: scoreColor(groupData.avg_overall || 0)}},
                                    (groupData.avg_overall || 0).toFixed(0))
                            ),
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Improving'),
                                React.createElement('div', {style: {fontSize: 20, fontWeight: 700, color: '#22c55e'}}, groupData.users_improving || 0)
                            ),
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Stable'),
                                React.createElement('div', {style: {fontSize: 20, fontWeight: 700, color: '#f59e0b'}}, groupData.users_stable || 0)
                            ),
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Declining'),
                                React.createElement('div', {style: {fontSize: 20, fontWeight: 700, color: '#ef4444'}}, groupData.users_declining || 0)
                            ),
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Strongest'),
                                React.createElement('div', {style: {fontSize: 14, fontWeight: 600, color: '#22c55e'}}, dimLabels[groupData.strongest_dimension] || groupData.strongest_dimension || '-')
                            ),
                            React.createElement('div', {style: {padding: '8px 16px', background: 'var(--bg-secondary)', borderRadius: 8, textAlign: 'center'}},
                                React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Weakest'),
                                React.createElement('div', {style: {fontSize: 14, fontWeight: 600, color: '#ef4444'}}, dimLabels[groupData.weakest_dimension] || groupData.weakest_dimension || '-')
                            )
                        ),
                        React.createElement('h4', {style: {fontSize: 12, fontWeight: 600, color: '#94a3b8', marginBottom: 8}}, 'Members'),
                        React.createElement('table', {style: {fontSize: 11, borderCollapse: 'collapse', width: '100%'}},
                            React.createElement('thead', null,
                                React.createElement('tr', null,
                                    React.createElement('th', {style: {padding: '4px 8px', color: '#64748b', textAlign: 'left'}}, 'User'),
                                    React.createElement('th', {style: {padding: '4px 8px', color: '#64748b', textAlign: 'right'}}, 'Jobs'),
                                    ...dims.map(d => React.createElement('th', {key: d, style: {padding: '4px 8px', color: '#64748b', textAlign: 'right'}}, dimLabels[d] || d)),
                                    React.createElement('th', {style: {padding: '4px 8px', color: '#64748b', textAlign: 'right'}}, 'Trend')
                                )
                            ),
                            React.createElement('tbody', null,
                                (groupData.users || []).map((u, i) => React.createElement('tr', {key: i, style: {cursor: 'pointer'}, onClick: () => loadTrajectory(u.username)},
                                    React.createElement('td', {style: {padding: '4px 8px', color: '#00BACF'}}, u.username),
                                    React.createElement('td', {style: {padding: '4px 8px', textAlign: 'right'}}, u.total_jobs),
                                    ...dims.map(d => {
                                        const s = (u.current_scores || {})[d];
                                        return React.createElement('td', {key: d, style: {padding: '4px 8px', textAlign: 'right', color: s !== undefined ? scoreColor(s) : '#64748b'}},
                                            s !== undefined ? s.toFixed(0) : '-');
                                    }),
                                    React.createElement('td', {style: {padding: '4px 8px', textAlign: 'right', color: impColor(u.overall_improvement || 0)}},
                                        (u.overall_improvement > 0 ? '+' : '') + (u.overall_improvement || 0).toFixed(1) + '%')
                                ))
                            )
                        )
                    )
                ) : (groupData && groupData.error ? React.createElement('div', {style: {color: '#ef4444', padding: 16}},
                    React.createElement('button', {onClick: () => setEduView('overview'), style: {background: 'none', border: 'none', color: '#00BACF', cursor: 'pointer', fontSize: 12, marginBottom: 12}}, '< Back'),
                    'Error: ' + groupData.error) : null);

                return React.createElement('div', {style: {padding: 16, maxWidth: 900}},
                    React.createElement('h2', {style: {fontSize: 18, fontWeight: 700, marginBottom: 4}}, 'Educational Analytics'),
                    React.createElement('p', {style: {fontSize: 12, color: '#64748b', marginBottom: 16}}, 'Computational proficiency tracking and development analysis'),
                    React.createElement('div', {style: {marginBottom: 16}},
                        React.createElement('select', {
                            value: eduCluster,
                            onChange: e => setEduCluster(e.target.value),
                            style: eduStyles.select
                        },
                            React.createElement('option', {value: 'all'}, 'All Clusters'),
                            eduClusters.map(c => React.createElement('option', {key: c, value: c}, c))
                        )
                    ),
                    eduLoading ? React.createElement('div', {style: {padding: 20, color: '#94a3b8'}}, 'Loading...') :
                    eduView === 'trajectory' ? trajectoryView :
                    eduView === 'group' ? groupView :
                    overviewView
                );
            };

                        const DynamicsPanel = () => {
                const [dynData, setDynData] = useState(null);
                const [dynLoading, setDynLoading] = useState(true);
                const [dynCluster, setDynCluster] = useState('all');
                const [dynClusters, setDynClusters] = useState([]);
                useEffect(() => {
                    // Get available clusters
                    fetch('/api/data')
                        .then(r => r.json())
                        .then(d => {
                            const cls = Object.keys(d.clusters || {}).filter(
                                k => d.clusters[k].type !== 'workstation');
                            setDynClusters(cls);
                        }).catch(() => {});
                }, []);
                useEffect(() => {
                    setDynLoading(true);
                    const url = dynCluster === 'all'
                        ? '/api/dynamics?hours=168'
                        : '/api/dynamics?hours=168&cluster=' + dynCluster;
                    fetch(url)
                        .then(r => r.json())
                        .then(d => { setDynData(d); setDynLoading(false); })
                        .catch(() => { setDynData({error: 'Failed to load'}); setDynLoading(false); });
                }, [dynCluster]);
                if (dynLoading) return React.createElement('div', {style: {padding: 20, color: '#94a3b8'}}, 'Loading dynamics...');
                if (!dynData || dynData.error) return React.createElement('div', {style: {padding: 20, color: '#ef4444'}}, 'Error: ' + (dynData?.error || 'unknown'));
                const dynDropdown = React.createElement('div', {style: {marginBottom: 16, display: 'flex', gap: 8}},
                    React.createElement('select', {
                        value: dynCluster,
                        onChange: e => setDynCluster(e.target.value),
                        style: eduStyles.select
                    },
                        React.createElement('option', {value: 'all'}, 'All Clusters'),
                        dynClusters.map(c => React.createElement('option', {key: c, value: c}, c))
                    )
                );
                const div = dynData.diversity || {};
                const cap = dynData.capacity || {};
                const res = dynData.resilience || {};
                const niche = dynData.niche || {};
                const ext = dynData.externality || {};
                const cur = div.current || {};
                const pressCol = {low: '#22c55e', moderate: '#f59e0b', high: '#B64326', critical: '#ef4444'};
                const scoreCol = (res.resilience_score || 0) >= 80 ? '#22c55e' : (res.resilience_score || 0) >= 50 ? '#f59e0b' : '#ef4444';
                // Executive strip
                const strip = React.createElement('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginBottom: 16}},
                    React.createElement('div', {className: 'card', style: {padding: 12}},
                        React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4}}, "Diversity (H')"),
                        React.createElement('div', {style: {fontSize: 24, fontWeight: 700}}, (cur.shannon_h || 0).toFixed(3)),
                        React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, div.trend_direction || 'stable')
                    ),
                    React.createElement('div', {className: 'card', style: {padding: 12}},
                        React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4}}, 'Capacity'),
                        React.createElement('div', {style: {fontSize: 24, fontWeight: 700, color: pressCol[cap.overall_pressure] || '#94a3b8'}}, (cap.overall_pressure || '—').toUpperCase()),
                        React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'Binding: ' + (cap.binding_constraint || 'none'))
                    ),
                    React.createElement('div', {className: 'card', style: {padding: 12}},
                        React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4}}, 'Resilience'),
                        React.createElement('div', {style: {fontSize: 24, fontWeight: 700, color: scoreCol}}, (res.resilience_score || 0).toFixed(0) + '/100'),
                        React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, res.resilience_trend || 'stable')
                    ),
                    React.createElement('div', {className: 'card', style: {padding: 12}},
                        React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4}}, 'Niche Risk'),
                        React.createElement('div', {style: {fontSize: 24, fontWeight: 700}}, (niche.high_overlap_pairs || []).length),
                        React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, 'high-overlap pairs')
                    ),
                    React.createElement('div', {className: 'card', style: {padding: 12}},
                        React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4}}, 'Externalities'),
                        React.createElement('div', {style: {fontSize: 24, fontWeight: 700}}, (ext.edges || []).length),
                        React.createElement('div', {style: {fontSize: 11, color: '#64748b'}}, (ext.top_imposers || []).length ? 'Top: ' + ext.top_imposers.slice(0,2).join(', ') : 'No impacts')
                    )
                );
                // Diversity distribution bars
                const cats = Object.entries(cur.category_counts || {}).sort((a,b) => b[1]-a[1]);
                const maxCount = cats.length ? cats[0][1] : 1;
                const colors = ['#00BACF','#3b82f6','#8b5cf6','#B64326','#22c55e','#f59e0b','#ef4444','#64748b'];
                const divBars = React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 'Workload Composition'),
                    ...cats.map(([name, count], i) => React.createElement('div', {key: name, style: {display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6}},
                        React.createElement('span', {style: {width: 80, fontSize: 12, color: '#94a3b8', textAlign: 'right', flexShrink: 0}}, name),
                        React.createElement('div', {style: {flex: 1, height: 18, background: 'var(--bg-secondary)', borderRadius: 4, overflow: 'hidden'}},
                            React.createElement('div', {style: {width: (count/maxCount*100)+'%', height: '100%', background: colors[i%colors.length], borderRadius: 4, opacity: 0.8}})
                        ),
                        React.createElement('span', {style: {width: 50, fontSize: 11, color: '#64748b', textAlign: 'right'}}, count + ' jobs')
                    ))
                );
                // User diversity distribution bars
                const divUser = dynData.diversity_by_user || {};
                const curUser = divUser.current || {};
                const userCats = Object.entries(curUser.category_counts || {}).sort((a,b) => b[1]-a[1]).slice(0, 20);
                const maxUserCount = userCats.length ? userCats[0][1] : 1;
                const userColors = ['#22c55e','#f59e0b','#3b82f6','#8b5cf6','#00BACF','#B64326','#ef4444','#64748b'];
                const userBars = React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 
                        'User Composition (' + (curUser.richness || 0) + ' users, H\u2032=' + (curUser.shannon_h || 0).toFixed(3) + ')'),
                    ...userCats.map(([name, count], i) => React.createElement('div', {key: name, style: {display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6}},
                        React.createElement('span', {style: {width: 80, fontSize: 12, color: '#94a3b8', textAlign: 'right', flexShrink: 0}}, name),
                        React.createElement('div', {style: {flex: 1, height: 18, background: 'var(--bg-secondary)', borderRadius: 4, overflow: 'hidden'}},
                            React.createElement('div', {style: {width: (count/maxUserCount*100)+'%', height: '100%', background: userColors[i%userColors.length], borderRadius: 4, opacity: 0.8}})
                        ),
                        React.createElement('span', {style: {width: 50, fontSize: 11, color: '#64748b', textAlign: 'right'}}, count + ' jobs')
                    ))
                );
                // Capacity bars
                const dims = (cap.dimensions || []).sort((a,b) => b.current_utilization - a.current_utilization);
                const capBars = React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 'Carrying Capacity — ' + (cap.overall_pressure || '').toUpperCase()),
                    ...dims.map(d => {
                        const pct = (d.current_utilization * 100).toFixed(1);
                        const col = d.is_binding ? '#ef4444' : d.current_utilization > 0.75 ? '#f59e0b' : '#00BACF';
                        return React.createElement('div', {key: d.dimension, style: {display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6}},
                            React.createElement('span', {style: {width: 100, fontSize: 12, color: '#94a3b8', textAlign: 'right', flexShrink: 0}}, d.label + (d.is_binding ? ' ←' : '')),
                            React.createElement('div', {style: {flex: 1, height: 18, background: 'var(--bg-secondary)', borderRadius: 4, overflow: 'hidden'}},
                                React.createElement('div', {style: {width: pct+'%', height: '100%', background: col, borderRadius: 4, opacity: 0.8}})
                            ),
                            React.createElement('span', {style: {width: 45, fontSize: 11, color: col, textAlign: 'right', fontFamily: 'monospace'}}, pct + '%')
                        );
                    })
                );
                // Niche overlap matrix
                const names = (niche.profiles || []).map(p => p.name);
                const matrix = niche.overlap_matrix || {};
                const nicheTable = names.length > 0 ? React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16, overflowX: 'auto'}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 'Niche Overlap Matrix'),
                    React.createElement('table', {style: {fontSize: 11, borderCollapse: 'collapse', width: '100%'}},
                        React.createElement('thead', null,
                            React.createElement('tr', null,
                                React.createElement('th', {style: {padding: '4px 8px', color: '#64748b'}}, ''),
                                ...names.map(n => React.createElement('th', {key: n, style: {padding: '4px 6px', color: '#94a3b8', fontWeight: 400, maxWidth: 70, overflow: 'hidden', textOverflow: 'ellipsis'}}, n.slice(0,10)))
                            )
                        ),
                        React.createElement('tbody', null,
                            ...names.map(row => React.createElement('tr', {key: row},
                                React.createElement('td', {style: {padding: '4px 8px', color: '#94a3b8'}}, row.slice(0,10)),
                                ...names.map(col => {
                                    if (row === col) return React.createElement('td', {key: col, style: {padding: '4px 6px', textAlign: 'center', color: 'var(--text-muted)'}}, '—');
                                    const val = matrix[row+'|'+col];
                                    const bg = val >= 0.8 ? 'rgba(239,68,68,0.25)' : val >= 0.6 ? 'rgba(245,158,11,0.15)' : 'transparent';
                                    return React.createElement('td', {key: col, style: {padding: '4px 6px', textAlign: 'center', fontFamily: 'monospace', background: bg}}, val !== undefined ? val.toFixed(2) : '—');
                                })
                            ))
                        )
                    )
                ) : null;
                // Resilience
                const resCard = React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 'System Resilience'),
                    React.createElement('div', {style: {display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12}},
                        React.createElement('div', null,
                            React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase'}}, 'Score'),
                            React.createElement('div', {style: {fontSize: 22, fontWeight: 700, color: scoreCol}}, (res.resilience_score||0).toFixed(0) + '/100')
                        ),
                        React.createElement('div', null,
                            React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase'}}, 'Mean Recovery'),
                            React.createElement('div', {style: {fontSize: 22, fontWeight: 700}}, res.mean_recovery_hours ? res.mean_recovery_hours.toFixed(1) + 'h' : '—')
                        ),
                        React.createElement('div', null,
                            React.createElement('div', {style: {fontSize: 11, color: '#94a3b8', textTransform: 'uppercase'}}, 'Disturbances'),
                            React.createElement('div', {style: {fontSize: 22, fontWeight: 700}}, (res.disturbances||[]).length)
                        )
                    )
                );
                // Externality edges
                const extCard = React.createElement('div', {className: 'card', style: {padding: 16, marginBottom: 16}},
                    React.createElement('h3', {style: {fontSize: 13, fontWeight: 600, color: 'var(--btn-text)', marginBottom: 12}}, 'Inter-Group Externalities'),
                    React.createElement('div', {style: {fontSize: 12, color: '#94a3b8', marginBottom: 8}}, ext.summary || 'No data'),
                    ...(ext.edges || []).slice(0, 8).map((e, i) => React.createElement('div', {key: i, style: {display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid var(--border)'}},
                        React.createElement('span', {style: {fontSize: 12}},
                            React.createElement('span', {style: {color: '#ef4444'}}, e.source_group),
                            ' → ',
                            React.createElement('span', {style: {color: '#3b82f6'}}, e.target_group)
                        ),
                        React.createElement('span', {style: {fontSize: 11, fontFamily: 'monospace', color: '#f59e0b'}}, 'r=' + e.impact_score.toFixed(2))
                    ))
                );
                return React.createElement('div', {style: {padding: 16, maxWidth: 900}},
                    React.createElement('h2', {style: {fontSize: 18, fontWeight: 700, marginBottom: 4}}, 'System Dynamics'),
                    React.createElement('p', {style: {fontSize: 12, color: '#64748b', marginBottom: 16}}, 'Ecological and economic frameworks applied to cluster usage patterns'),
                    dynDropdown,
                    strip,
                    divBars,
                    userBars,
                    capBars,
                    nicheTable,
                    resCard,
                    extCard
                );
            };


            // Report Issue Panel
            const ReportIssuePanel = () => {
                const [category, setCategory] = useState('bug');
                const [component, setComponent] = useState('other');
                const [title, setTitle] = useState('');
                const [description, setDescription] = useState('');
                const [steps, setSteps] = useState('');
                const [expected, setExpected] = useState('');
                const [actual, setActual] = useState('');
                const [sysInfo, setSysInfo] = useState(null);
                const [duplicates, setDuplicates] = useState([]);
                const [submitting, setSubmitting] = useState(false);
                const [result, setResult] = useState(null);
                const components = ['collectors','alerts','dashboard','tessera','cli','console','dynamics','insights','reference','other'];
                const categories = [{v:'bug',l:'Bug Report',d:'Something is broken'},{v:'feature',l:'Feature Request',d:'Suggest improvement'},{v:'question',l:'Question',d:'Ask about usage'}];

                React.useEffect(() => {
                    fetch('/api/issue/info').then(r=>r.json()).then(setSysInfo).catch(()=>{});
                }, []);

                const searchDuplicates = () => {
                    if (!title || title.length < 3) return;
                    fetch('/api/issue/search?q=' + encodeURIComponent(component + ' ' + title))
                        .then(r=>r.json()).then(setDuplicates).catch(()=>setDuplicates([]));
                };

                const handleSubmit = () => {
                    setSubmitting(true);
                    const data = {category, component, title, description, steps, expected, actual,
                        problem: description, question: description};
                    fetch('/api/issue/submit', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(data)
                    }).then(r => r.json()).then(r => {
                        setResult(r);
                        setSubmitting(false);
                        if (r.method === 'browser' && r.url) window.open(r.url, '_blank');
                    }).catch(e => {
                        setResult({success: false, error: String(e)});
                        setSubmitting(false);
                    });
                };

                const openBrowser = () => {
                    const p = new URLSearchParams({category, component, title, description, steps, expected, actual});
                    fetch('/api/issue/url?' + p.toString())
                        .then(r=>r.json()).then(r => { if(r.url) window.open(r.url, '_blank'); })
                        .catch(()=>{});
                };

                const cs = {
                    card: {background:'var(--bg-tertiary)', border:'1px solid var(--border)', borderRadius:8, padding:16, marginBottom:12},
                    label: {fontSize:12, color:'#94a3b8', marginBottom:4, display:'block'},
                    input: {width:'100%', padding:'8px 12px', background:'var(--bg-secondary)', border:'1px solid var(--input-border)',
                            borderRadius:6, color:'var(--btn-text)', fontSize:13, fontFamily:'inherit', boxSizing:'border-box'},
                    textarea: {width:'100%', padding:'8px 12px', background:'var(--bg-secondary)', border:'1px solid var(--input-border)',
                               borderRadius:6, color:'var(--btn-text)', fontSize:13, fontFamily:'inherit',
                               minHeight:80, resize:'vertical', boxSizing:'border-box'},
                    select: {padding:'8px 12px', background:'var(--bg-secondary)', border:'1px solid var(--input-border)',
                             borderRadius:6, color:'var(--btn-text)', fontSize:13},
                    btn: {padding:'8px 16px', borderRadius:6, border:'none', cursor:'pointer', fontSize:13, fontWeight:600},
                    catBtn: (active) => ({padding:'10px 16px', borderRadius:6, border: active ? '2px solid var(--cyan)' : '1px solid var(--input-border)',
                        background: active ? 'var(--bg-hover)' : 'var(--bg-tertiary)', color: active ? '#93c5fd' : '#94a3b8',
                        cursor:'pointer', fontSize:13, flex:1, textAlign:'center'}),
                };

                if (result && result.success) {
                    return React.createElement('div', {style:{padding:16, maxWidth:700}},
                        React.createElement('div', {style:{...cs.card, borderColor:'#22c55e', textAlign:'center', padding:32}},
                            React.createElement('div', {style:{fontSize:32, marginBottom:8}}, '\u2713'),
                            React.createElement('h3', {style:{color:'#22c55e', marginBottom:8}}, 'Issue #' + result.number + ' Created'),
                            React.createElement('a', {href:result.url, target:'_blank', style:{color:'#93c5fd', fontSize:13}}, result.url),
                            React.createElement('div', {style:{marginTop:16}},
                                React.createElement('button', {onClick:()=>{setResult(null);setTitle('');setDescription('');setSteps('');setExpected('');setActual('');setDuplicates([]);},
                                    style:{...cs.btn, background:'var(--bg-secondary)', color:'var(--btn-text)'}}, 'Report Another Issue')
                            )
                        )
                    );
                }

                return React.createElement('div', {style:{padding:16, maxWidth:700}},
                    React.createElement('h2', {style:{fontSize:18, fontWeight:700, marginBottom:4}}, 'Report Issue'),
                    React.createElement('p', {style:{fontSize:12, color:'#64748b', marginBottom:16}}, 'Submit bug reports, feature requests, or questions directly to the NØMAÐ GitHub repository'),

                    // Category selection
                    React.createElement('div', {style:{display:'flex', gap:8, marginBottom:16}},
                        ...categories.map(c => React.createElement('button', {key:c.v, onClick:()=>setCategory(c.v), style:cs.catBtn(category===c.v)},
                            React.createElement('div', {style:{fontWeight:600}}, c.l),
                            React.createElement('div', {style:{fontSize:11, marginTop:2, opacity:0.7}}, c.d)
                        ))
                    ),

                    // Component + Title
                    React.createElement('div', {style:cs.card},
                        React.createElement('label', {style:cs.label}, 'Affected Component'),
                        React.createElement('select', {value:component, onChange:e=>setComponent(e.target.value), style:{...cs.select, width:'100%', marginBottom:12}},
                            ...components.map(c => React.createElement('option', {key:c, value:c}, c))
                        ),
                        React.createElement('label', {style:cs.label}, 'Title'),
                        React.createElement('input', {value:title, onChange:e=>setTitle(e.target.value),
                            onBlur:searchDuplicates, placeholder:'Brief description of the issue', style:cs.input})
                    ),

                    // Duplicates
                    duplicates.length > 0 ? React.createElement('div', {style:{...cs.card, borderColor:'#f59e0b'}},
                        React.createElement('div', {style:{fontSize:13, fontWeight:600, color:'#f59e0b', marginBottom:8}}, 'Similar open issues found:'),
                        ...duplicates.map(d => React.createElement('div', {key:d.number, style:{padding:'6px 0', borderBottom:'1px solid var(--border)'}},
                            React.createElement('a', {href:d.url, target:'_blank', style:{color:'#93c5fd', fontSize:13}},
                                '#' + d.number + ': ' + d.title),
                            React.createElement('span', {style:{fontSize:11, color:'#64748b', marginLeft:8}},
                                d.created_at + ' \u00b7 ' + d.comments + ' comments')
                        ))
                    ) : null,

                    // Category-specific fields
                    React.createElement('div', {style:cs.card},
                        category === 'bug' ? React.createElement(React.Fragment, null,
                            React.createElement('label', {style:cs.label}, 'Description'),
                            React.createElement('textarea', {value:description, onChange:e=>setDescription(e.target.value),
                                placeholder:'What happened?', style:{...cs.textarea, marginBottom:12}}),
                            React.createElement('label', {style:cs.label}, 'Steps to Reproduce'),
                            React.createElement('textarea', {value:steps, onChange:e=>setSteps(e.target.value),
                                placeholder:'1. Run nomad ... 2. Navigate to ... 3. See error', style:{...cs.textarea, marginBottom:12}}),
                            React.createElement('label', {style:cs.label}, 'Expected Behavior'),
                            React.createElement('textarea', {value:expected, onChange:e=>setExpected(e.target.value),
                                placeholder:'What should have happened?', style:{...cs.textarea, marginBottom:12}}),
                            React.createElement('label', {style:cs.label}, 'Actual Behavior'),
                            React.createElement('textarea', {value:actual, onChange:e=>setActual(e.target.value),
                                placeholder:'What actually happened?', style:cs.textarea})
                        ) : category === 'feature' ? React.createElement(React.Fragment, null,
                            React.createElement('label', {style:cs.label}, 'What problem does this solve?'),
                            React.createElement('textarea', {value:description, onChange:e=>setDescription(e.target.value),
                                placeholder:'Describe the problem or need', style:{...cs.textarea, marginBottom:12}}),
                            React.createElement('label', {style:cs.label}, 'Proposed Solution (optional)'),
                            React.createElement('textarea', {value:steps, onChange:e=>setSteps(e.target.value),
                                placeholder:'How would you like this to work?', style:cs.textarea})
                        ) : React.createElement(React.Fragment, null,
                            React.createElement('label', {style:cs.label}, 'Your Question'),
                            React.createElement('textarea', {value:description, onChange:e=>setDescription(e.target.value),
                                placeholder:'What would you like to know?', style:{...cs.textarea, marginBottom:12}}),
                            React.createElement('label', {style:cs.label}, "What I've Already Tried (optional)"),
                            React.createElement('textarea', {value:steps, onChange:e=>setSteps(e.target.value),
                                placeholder:'Documentation, commands, or approaches tried', style:cs.textarea})
                        )
                    ),

                    // System info preview
                    sysInfo ? React.createElement('div', {style:{...cs.card, fontSize:11, fontFamily:'monospace', color:'#64748b'}},
                        React.createElement('div', {style:{fontWeight:600, marginBottom:4, color:'#94a3b8'}}, 'Auto-included system info:'),
                        React.createElement('div', null, 'NØMAÐ ' + (sysInfo.nomad_version||'?') + ' | Python ' + (sysInfo.python_version||'?') + ' | ' + (sysInfo.os_info||'?')),
                        sysInfo.active_collectors && sysInfo.active_collectors.length > 0 ?
                            React.createElement('div', null, 'Collectors: ' + sysInfo.active_collectors.join(', ')) : null
                    ) : null,

                    // Submit buttons
                    React.createElement('div', {style:{display:'flex', gap:8, marginTop:8}},
                        React.createElement('button', {onClick:handleSubmit, disabled:submitting || !title,
                            style:{...cs.btn, background: title ? '#3b82f6' : 'var(--input-border)', color:'#fff', flex:1}},
                            submitting ? 'Submitting...' : 'Submit Issue'),
                        React.createElement('button', {onClick:openBrowser, disabled:!title,
                            style:{...cs.btn, background:'var(--bg-secondary)', color:'var(--btn-text)'}}, 'Open in GitHub')
                    ),

                    // Error display
                    result && !result.success && result.error ? React.createElement('div', {style:{...cs.card, borderColor:'#ef4444', marginTop:8, color:'#fca5a5', fontSize:12}},
                        result.error
                    ) : null
                );
            };

            // Insights Panel


            // ═══════════════════════════════════════════════════════════
            // Reference Panel
            // ═══════════════════════════════════════════════════════════
            const ReferencePanel = () => {
                const [refData, setRefData] = useState(null);
                const [refView, setRefView] = useState('index');
                const [refTopic, setRefTopic] = useState(null);
                const [refSearch, setRefSearch] = useState('');
                const [refResults, setRefResults] = useState(null);
                const [refLoading, setRefLoading] = useState(false);

                useEffect(() => {
                    fetch('/api/ref')
                        .then(r => r.json())
                        .then(d => setRefData(d))
                        .catch(() => setRefData({categories: {}}));
                }, []);

                const loadTopic = (key) => {
                    setRefLoading(true);
                    fetch('/api/ref?action=topic&key=' + encodeURIComponent(key))
                        .then(r => r.json())
                        .then(d => { setRefTopic(d); setRefView('topic'); setRefLoading(false); })
                        .catch(() => setRefLoading(false));
                };

                const doSearch = () => {
                    if (!refSearch.trim()) return;
                    setRefLoading(true);
                    fetch('/api/ref?action=search&q=' + encodeURIComponent(refSearch))
                        .then(r => r.json())
                        .then(d => { setRefResults(d); setRefView('search'); setRefLoading(false); })
                        .catch(() => setRefLoading(false));
                };

                if (!refData) return React.createElement("div", {style: {padding: "40px", textAlign: "center"}}, "Loading reference...");

                const catLabels = {commands: "Commands", concepts: "Concepts", config: "Configuration", collectors: "Collectors", alerts: "Alerts", other: "Other"};
                const catColors = {commands: "#00BACF", concepts: "#a78bfa", config: "#f59e0b", collectors: "#22c55e", alerts: "#f87171", other: "#64748b"};

                const cardStyle = {background: "var(--bg-secondary, #1e293b)", borderRadius: "10px", padding: "16px", marginBottom: "12px", cursor: "pointer", transition: "border-color 0.2s", border: "1px solid transparent"};
                const tagStyle = {display: "inline-block", padding: "2px 8px", borderRadius: "4px", fontSize: "10px", fontWeight: 600, marginRight: "6px"};

                // Search bar
                const searchBar = React.createElement("div", {style: {display: "flex", gap: "8px", marginBottom: "20px"}},
                    React.createElement("input", {
                        type: "text", value: refSearch, placeholder: "Search reference...",
                        onChange: e => setRefSearch(e.target.value),
                        onKeyDown: e => { if (e.key === 'Enter') doSearch(); },
                        style: {flex: 1, padding: "10px 14px", borderRadius: "8px", border: "1px solid var(--border, #333)", background: "var(--bg-secondary, #1e293b)", color: "inherit", fontSize: "14px", outline: "none"}
                    }),
                    React.createElement("button", {
                        onClick: doSearch,
                        style: {padding: "10px 20px", borderRadius: "8px", border: "none", background: "#00BACF", color: "#fff", cursor: "pointer", fontSize: "13px", fontWeight: 600}
                    }, "Search")
                );

                // Index view
                if (refView === 'index') {
                    const cats = refData.categories || {};
                    return React.createElement("div", {style: {padding: "20px", maxWidth: "900px"}},
                        React.createElement("h2", {style: {fontSize: "20px", fontWeight: 600, marginBottom: "6px"}}, "Reference"),
                        React.createElement("p", {style: {fontSize: "13px", color: "#64748b", marginBottom: "20px"}}, "Browse NØMAÐ documentation, commands, and concepts"),
                        searchBar,
                        ...Object.entries(cats).sort().map(([cat, entries]) =>
                            React.createElement("div", {key: cat, style: {marginBottom: "24px"}},
                                React.createElement("div", {style: {fontSize: "13px", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: catColors[cat] || "#64748b", marginBottom: "10px"}},
                                    (catLabels[cat] || cat) + " (" + entries.length + ")"),
                                ...entries.map(e =>
                                    React.createElement("div", {
                                        key: e.key,
                                        style: {...cardStyle},
                                        onClick: () => loadTopic(e.key),
                                        onMouseEnter: ev => ev.currentTarget.style.borderColor = catColors[cat] || "#64748b",
                                        onMouseLeave: ev => ev.currentTarget.style.borderColor = "transparent"
                                    },
                                        React.createElement("div", {style: {fontSize: "14px", fontWeight: 600, marginBottom: "4px"}}, e.title),
                                        e.summary && React.createElement("div", {style: {fontSize: "12px", color: "#94a3b8"}}, e.summary)
                                    )
                                )
                            )
                        )
                    );
                }

                // Search results view
                if (refView === 'search') {
                    const results = (refResults && refResults.results) || [];
                    return React.createElement("div", {style: {padding: "20px", maxWidth: "900px"}},
                        React.createElement("button", {onClick: () => setRefView('index'), style: {background: "none", border: "none", color: "#00BACF", cursor: "pointer", fontSize: "12px", marginBottom: "12px"}}, "< Back to index"),
                        searchBar,
                        React.createElement("div", {style: {fontSize: "13px", color: "#64748b", marginBottom: "16px"}},
                            results.length + ' result(s) for "' + (refResults.query || '') + '"'),
                        ...results.map(r =>
                            React.createElement("div", {
                                key: r.key, style: {...cardStyle}, onClick: () => loadTopic(r.key),
                                onMouseEnter: ev => ev.currentTarget.style.borderColor = catColors[r.category] || "#64748b",
                                onMouseLeave: ev => ev.currentTarget.style.borderColor = "transparent"
                            },
                                React.createElement("div", {style: {display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px"}},
                                    React.createElement("span", {style: {...tagStyle, background: (catColors[r.category] || "#64748b") + "22", color: catColors[r.category] || "#64748b"}}, r.category),
                                    React.createElement("span", {style: {fontSize: "14px", fontWeight: 600}}, r.title)
                                ),
                                r.summary && React.createElement("div", {style: {fontSize: "12px", color: "#94a3b8"}}, r.summary)
                            )
                        ),
                        results.length === 0 && React.createElement("div", {style: {textAlign: "center", padding: "40px", color: "#64748b"}}, "No results found. Try different keywords.")
                    );
                }

                // Topic detail view
                if (refView === 'topic' && refTopic && !refTopic.error) {
                    const t = refTopic;
                    const sectionStyle = {marginBottom: "16px"};
                    const sectionTitle = {fontSize: "12px", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.05em", color: "#64748b", marginBottom: "6px"};
                    const codeBlock = {background: "var(--bg-primary, #0f172a)", borderRadius: "6px", padding: "12px", fontSize: "12px", fontFamily: "monospace", whiteSpace: "pre-wrap", overflowX: "auto", lineHeight: 1.5};

                    return React.createElement("div", {style: {padding: "20px", maxWidth: "900px"}},
                        React.createElement("button", {onClick: () => setRefView('index'), style: {background: "none", border: "none", color: "#00BACF", cursor: "pointer", fontSize: "12px", marginBottom: "12px"}}, "< Back to index"),
                        React.createElement("div", {style: {background: "var(--bg-secondary, #1e293b)", borderRadius: "12px", padding: "24px"}},
                            React.createElement("div", {style: {display: "flex", alignItems: "center", gap: "10px", marginBottom: "8px"}},
                                React.createElement("span", {style: {...tagStyle, background: (catColors[t.category] || "#64748b") + "22", color: catColors[t.category] || "#64748b"}}, t.category),
                                React.createElement("code", {style: {fontSize: "11px", color: "#64748b"}}, t.key)
                            ),
                            React.createElement("h2", {style: {fontSize: "22px", fontWeight: 700, marginBottom: "8px"}}, t.title),
                            t.summary && React.createElement("p", {style: {fontSize: "14px", color: "#94a3b8", marginBottom: "16px", lineHeight: 1.5}}, t.summary),
                            t.description && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Description"),
                                React.createElement("div", {style: {fontSize: "13px", lineHeight: 1.6, whiteSpace: "pre-wrap"}}, t.description)
                            ),
                            t.math && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Formula"),
                                React.createElement("div", {style: codeBlock}, t.math)
                            ),
                            t.examples && t.examples.length > 0 && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Examples"),
                                ...t.examples.map((ex, i) => React.createElement("div", {key: i, style: codeBlock, marginBottom: "8px"}, ex))
                            ),
                            t.config_section && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Configuration"),
                                React.createElement("code", {style: {fontSize: "12px"}}, t.config_section),
                                t.config_keys && t.config_keys.length > 0 && React.createElement("div", {style: {marginTop: "8px", fontSize: "12px", color: "#94a3b8"}},
                                    "Keys: " + t.config_keys.join(", "))
                            ),
                            t.source_files && t.source_files.length > 0 && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Source Files"),
                                React.createElement("div", {style: {fontSize: "12px", color: "#94a3b8"}}, t.source_files.join(", "))
                            ),
                            (t.related && t.related.length > 0 || t.see_also && t.see_also.length > 0) && React.createElement("div", {style: sectionStyle},
                                React.createElement("div", {style: sectionTitle}, "Related Topics"),
                                React.createElement("div", {style: {display: "flex", flexWrap: "wrap", gap: "6px"}},
                                    ...(t.see_also || []).concat(t.related || []).map(r =>
                                        React.createElement("span", {
                                            key: r, onClick: (e) => { e.stopPropagation(); loadTopic(r); },
                                            style: {padding: "4px 10px", borderRadius: "6px", background: "var(--bg-primary, #0f172a)", fontSize: "12px", cursor: "pointer", color: "#00BACF"}
                                        }, r)
                                    )
                                )
                            ),
                            t.children && t.children.length > 0 && React.createElement("div", {style: {marginTop: "16px"}},
                                React.createElement("div", {style: sectionTitle}, "Subtopics"),
                                ...t.children.map(c =>
                                    React.createElement("div", {
                                        key: c.key, style: {...cardStyle, marginBottom: "8px"}, onClick: () => loadTopic(c.key),
                                        onMouseEnter: ev => ev.currentTarget.style.borderColor = "#00BACF",
                                        onMouseLeave: ev => ev.currentTarget.style.borderColor = "transparent"
                                    },
                                        React.createElement("div", {style: {fontSize: "13px", fontWeight: 600}}, c.title),
                                        c.summary && React.createElement("div", {style: {fontSize: "12px", color: "#94a3b8"}}, c.summary)
                                    )
                                )
                            )
                        )
                    );
                }

                // Error or loading
                if (refLoading) return React.createElement("div", {style: {padding: "40px", textAlign: "center"}}, "Loading...");
                return React.createElement("div", {style: {padding: "40px"}},
                    React.createElement("button", {onClick: () => setRefView('index'), style: {background: "none", border: "none", color: "#00BACF", cursor: "pointer", fontSize: "12px", marginBottom: "12px"}}, "< Back to index"),
                    React.createElement("div", {style: {color: "#f87171"}}, refTopic && refTopic.error ? refTopic.error : "Topic not found")
                );
            };

            const ReadinessPanel = () => {
                const [data, setData] = useState(null);
                const [loading, setLoading] = useState(true);
                useEffect(() => {
                    const load = () => fetch('/api/readiness')
                        .then(r => r.json())
                        .then(d => { setData(d); setLoading(false); })
                        .catch(() => setLoading(false));
                    load();
                    const iv = setInterval(load, 10000);
                    return () => clearInterval(iv);
                }, []);

                if (loading) return React.createElement("div", {style: {padding: "40px", textAlign: "center"}}, "Loading readiness data...");
                if (!data || data.status === "error") return React.createElement("div", {style: {padding: "40px"}}, "Could not load readiness data.");

                const cs = data.collectors || {};
                const ts = data.tables || {};
                const col = data.collection || {};
                const db = data.database || {};

                const formatBytes = (b) => {
                    if (b > 1024*1024*1024) return (b/(1024*1024*1024)).toFixed(1) + " GB";
                    if (b > 1024*1024) return (b/(1024*1024)).toFixed(1) + " MB";
                    if (b > 1024) return (b/1024).toFixed(1) + " KB";
                    return b + " B";
                };

                const formatAge = (ts) => {
                    if (!ts) return "—";
                    const diff = (Date.now() - new Date(ts).getTime()) / 1000;
                    if (diff < 0) return "just now";
                    if (diff < 60) return Math.round(diff) + "s ago";
                    if (diff < 3600) return Math.round(diff/60) + "m ago";
                    if (diff < 86400) return Math.round(diff/3600) + "h ago";
                    return Math.round(diff/86400) + "d ago";
                };

                const formatUptime = (first, last) => {
                    if (!first || !last) return "—";
                    const diff = (new Date(last).getTime() - new Date(first).getTime()) / 1000;
                    const h = Math.floor(diff / 3600);
                    const m = Math.floor((diff % 3600) / 60);
                    if (h > 24) return Math.floor(h/24) + "d " + (h%24) + "h";
                    return h + "h " + m + "m";
                };

                const collectorOrder = ["node_state", "slurm", "jobs", "disk", "iostat", "mpstat", "vmstat", "gpu", "nfs", "groups"];
                const statusColors = {active: "#22c55e", empty: "#f59e0b", no_table: "#6b7280", error: "#ef4444"};
                const statusLabels = {active: "Active", empty: "Empty", no_table: "No Table", error: "Error"};

                const cardStyle = {background: "var(--bg-secondary, #1e293b)", borderRadius: "12px", padding: "20px"};
                const labelStyle = {fontSize: "11px", textTransform: "uppercase", letterSpacing: "0.05em", opacity: 0.5, marginBottom: "4px"};
                const valueStyle = {fontSize: "24px", fontWeight: 700, fontFamily: "monospace"};

                return React.createElement("div", {style: {padding: "20px", maxWidth: "1000px"}},
                    // Title
                    React.createElement("h2", {style: {fontSize: "20px", fontWeight: 600, marginBottom: "20px"}}, "System Readiness"),

                    // Summary cards row
                    React.createElement("div", {style: {display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "16px", marginBottom: "24px"}},
                        React.createElement("div", {style: cardStyle},
                            React.createElement("div", {style: labelStyle}, "Status"),
                            React.createElement("div", {style: {...valueStyle, fontSize: "18px", color: data.status === "ok" ? "#22c55e" : "#ef4444"}},
                                data.status === "ok" ? "Collecting" : "Error")
                        ),
                        React.createElement("div", {style: cardStyle},
                            React.createElement("div", {style: labelStyle}, "Uptime"),
                            React.createElement("div", {style: {...valueStyle, fontSize: "18px"}},
                                formatUptime(col.first_timestamp, col.last_timestamp))
                        ),
                        React.createElement("div", {style: cardStyle},
                            React.createElement("div", {style: labelStyle}, "Collection Cycles"),
                            React.createElement("div", {style: valueStyle}, col.cycles || 0)
                        ),
                        React.createElement("div", {style: cardStyle},
                            React.createElement("div", {style: labelStyle}, "Database Size"),
                            React.createElement("div", {style: {...valueStyle, fontSize: "18px"}},
                                formatBytes(db.size_bytes || 0))
                        )
                    ),

                    // Last update
                    React.createElement("div", {style: {...cardStyle, marginBottom: "24px", padding: "12px 20px", display: "flex", justifyContent: "space-between", alignItems: "center"}},
                        React.createElement("span", {style: {opacity: 0.7}}, "Last collection"),
                        React.createElement("span", {style: {fontFamily: "monospace", fontWeight: 600}},
                            col.last_timestamp ? formatAge(col.last_timestamp) + " (" + col.last_timestamp + ")" : "—")
                    ),

                    // Collectors table
                    React.createElement("h3", {style: {fontSize: "16px", fontWeight: 600, marginBottom: "12px", marginTop: "8px"}}, "Collectors"),
                    React.createElement("div", {style: {...cardStyle, padding: 0, overflow: "hidden", marginBottom: "24px"}},
                        React.createElement("table", {style: {width: "100%", borderCollapse: "collapse", fontSize: "13px"}},
                            React.createElement("thead", null,
                                React.createElement("tr", {style: {borderBottom: "1px solid var(--border, #333)"}},
                                    React.createElement("th", {style: {textAlign: "left", padding: "12px 16px", opacity: 0.6, fontWeight: 500}}, "Collector"),
                                    React.createElement("th", {style: {textAlign: "center", padding: "12px 16px", opacity: 0.6, fontWeight: 500}}, "Status"),
                                    React.createElement("th", {style: {textAlign: "right", padding: "12px 16px", opacity: 0.6, fontWeight: 500}}, "Records"),
                                    React.createElement("th", {style: {textAlign: "right", padding: "12px 16px", opacity: 0.6, fontWeight: 500}}, "Last Update"),
                                    React.createElement("th", {style: {textAlign: "right", padding: "12px 16px", opacity: 0.6, fontWeight: 500}}, "First Seen")
                                )
                            ),
                            React.createElement("tbody", null,
                                ...collectorOrder.filter(k => cs[k] && !(cs[k].status === "empty" && cs[k].rows === 0)).map((k, i) =>
                                    React.createElement("tr", {key: k, style: {borderBottom: i < collectorOrder.length-1 ? "1px solid var(--border, #222)" : "none"}},
                                        React.createElement("td", {style: {padding: "10px 16px", fontWeight: 500}}, k),
                                        React.createElement("td", {style: {padding: "10px 16px", textAlign: "center"}},
                                            React.createElement("span", {style: {
                                                background: (statusColors[cs[k].status] || "#6b7280") + "22",
                                                color: statusColors[cs[k].status] || "#6b7280",
                                                padding: "2px 10px", borderRadius: "4px", fontSize: "11px", fontWeight: 600
                                            }}, statusLabels[cs[k].status] || cs[k].status)
                                        ),
                                        React.createElement("td", {style: {padding: "10px 16px", textAlign: "right", fontFamily: "monospace"}},
                                            (cs[k].rows || 0).toLocaleString()),
                                        React.createElement("td", {style: {padding: "10px 16px", textAlign: "right", fontSize: "12px", opacity: 0.7}},
                                            formatAge(cs[k].last_update)),
                                        React.createElement("td", {style: {padding: "10px 16px", textAlign: "right", fontSize: "12px", opacity: 0.7}},
                                            formatAge(cs[k].first_update))
                                    )
                                )
                            )
                        )
                    ),

                    // Database path
                    React.createElement("div", {style: {...cardStyle, padding: "12px 20px", display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "12px", opacity: 0.6}},
                        React.createElement("span", null, "Database"),
                        React.createElement("span", {style: {fontFamily: "monospace"}}, db.path || "—")
                    )
                );
            };

            const InsightsPanel = () => {
                const [insights, setInsights] = useState(null);
                const [loading, setLoading] = useState(true);
                useEffect(() => {
                    fetch('/api/insights?hours=168')
                        .then(r => r.json())
                        .then(d => { setInsights(d); setLoading(false); })
                        .catch(() => { setInsights({signals: [], insights: [], overall_health: 'unknown'}); setLoading(false); });
                }, []);
                if (loading) return React.createElement("div", {style: {padding: "40px", textAlign: "center"}}, "Loading insights...");
                if (!insights) return React.createElement("div", {style: {padding: "40px"}}, "No insight data available.");

                const healthColors = {good: "#22c55e", nominal: "#06b6d4", degraded: "#f59e0b", impaired: "#ef4444", unknown: "#6b7280"};
                const sevColors = {critical: "#ef4444", warning: "#f59e0b", notice: "#06b6d4", info: "#22c55e"};
                const sevLabels = {critical: "CRIT", warning: "WARN", notice: "NOTE", info: "OK"};

                const healthColor = healthColors[insights.overall_health] || "#6b7280";
                const healthLabel = {good: "Good", nominal: "Nominal", degraded: "Degraded", impaired: "Impaired"}[insights.overall_health] || "Unknown";

                return React.createElement("div", {style: {padding: "20px", maxWidth: "900px"}},
                    // Health banner
                    React.createElement("div", {style: {
                        background: "var(--bg-secondary, #1e293b)", borderRadius: "12px", padding: "20px",
                        marginBottom: "24px", borderLeft: `4px solid ${healthColor}`
                    }},
                        React.createElement("div", {style: {display: "flex", alignItems: "center", gap: "12px", marginBottom: "8px"}},
                            React.createElement("div", {style: {
                                width: "12px", height: "12px", borderRadius: "50%", background: healthColor,
                                boxShadow: `0 0 8px ${healthColor}40`
                            }}),
                            React.createElement("span", {style: {fontSize: "20px", fontWeight: 600}}, "Cluster Health: " + healthLabel)
                        ),
                        React.createElement("div", {style: {fontSize: "13px", opacity: 0.7}},
                            insights.signal_count + " signals | " + insights.insight_count + " correlated findings"
                        )
                    ),
                    // Correlated insights (Level 2)
                    insights.insights && insights.insights.length > 0 && React.createElement("div", {style: {marginBottom: "24px"}},
                        React.createElement("h3", {style: {fontSize: "16px", fontWeight: 600, marginBottom: "12px", opacity: 0.8}}, "Linked Findings"),
                        insights.insights.map((ins, i) =>
                            React.createElement("div", {key: i, style: {
                                background: "var(--bg-secondary, #1e293b)", borderRadius: "8px", padding: "16px",
                                marginBottom: "12px", borderLeft: `3px solid ${sevColors[ins.severity] || "#6b7280"}`
                            }},
                                React.createElement("div", {style: {display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px"}},
                                    React.createElement("span", {style: {
                                        fontSize: "11px", fontWeight: 700, padding: "2px 6px", borderRadius: "4px",
                                        background: (sevColors[ins.severity] || "#6b7280") + "20",
                                        color: sevColors[ins.severity] || "#6b7280"
                                    }}, sevLabels[ins.severity] || "?"),
                                    React.createElement("span", {style: {fontSize: "13px", fontWeight: 600}},
                                        ins.title.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())
                                    )
                                ),
                                React.createElement("p", {style: {fontSize: "13px", lineHeight: 1.5, margin: "0 0 8px 0"}}, ins.narrative),
                                ins.recommendation && React.createElement("div", {style: {
                                    fontSize: "12px", opacity: 0.7, borderTop: "1px solid var(--border)",
                                    paddingTop: "8px", marginTop: "4px"
                                }}, "Recommendation: " + ins.recommendation)
                            )
                        )
                    ),
                    // Individual signals
                    React.createElement("h3", {style: {fontSize: "16px", fontWeight: 600, marginBottom: "12px", opacity: 0.8}},
                        "Signals (" + insights.signal_count + ")"
                    ),
                    (insights.signals || []).map((sig, i) =>
                        React.createElement("div", {key: i, style: {
                            background: "var(--bg-secondary, #1e293b)", borderRadius: "8px", padding: "14px",
                            marginBottom: "8px", borderLeft: `3px solid ${sevColors[sig.severity] || "#6b7280"}`
                        }},
                            React.createElement("div", {style: {display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px"}},
                                React.createElement("span", {style: {
                                    fontSize: "11px", fontWeight: 700, padding: "2px 6px", borderRadius: "4px",
                                    background: (sevColors[sig.severity] || "#6b7280") + "20",
                                    color: sevColors[sig.severity] || "#6b7280"
                                }}, sevLabels[sig.severity] || "?"),
                                React.createElement("span", {style: {fontSize: "12px", fontWeight: 600, textTransform: "capitalize"}}, sig.type)
                            ),
                            React.createElement("p", {style: {fontSize: "13px", lineHeight: 1.5, margin: 0}}, sig.narrative)
                        )
                    )
                );
            };

            const CloudPanel = () => {
                const [data, setData] = React.useState(null);
                const [loading, setLoading] = React.useState(true);
                const [selectedInstance, setSelectedInstance] = React.useState(null);

                React.useEffect(() => {
                    fetch('/api/cloud').then(r => r.json()).then(d => { setData(d); setLoading(false); });
                    const iv = setInterval(() => {
                        fetch('/api/cloud').then(r => r.json()).then(setData);
                    }, 30000);
                    return () => clearInterval(iv);
                }, []);

                if (loading) return React.createElement("div", {style: {padding: "40px", textAlign: "center", color: "var(--text-secondary)"}}, "Loading cloud metrics...");
                if (!data || !data.instances || data.instances.length === 0) {
                    return React.createElement("div", {style: {padding: "40px", textAlign: "center", color: "var(--text-secondary)"}},
                        React.createElement("h2", {style: {fontSize: "20px", marginBottom: "12px"}}, "Cloud Monitoring"),
                        React.createElement("p", null, "No cloud metrics found. Enable cloud collectors in nomad.toml or run nomad demo.")
                    );
                }

                const { instances, latest, timeseries, cost, summary } = data;

                const metricsByInstance = {};
                (latest || []).forEach(m => {
                    if (!metricsByInstance[m.node_name]) metricsByInstance[m.node_name] = {};
                    metricsByInstance[m.node_name][m.metric_name] = m;
                });

                const costByInstance = {};
                (cost || []).forEach(c => { costByInstance[c.node_name] = c; });

                const metricColor = (name, val) => {
                    if (name.includes('cpu') || name.includes('gpu') || name.includes('mem')) {
                        if (val > 90) return 'var(--red)';
                        if (val > 70) return 'var(--yellow)';
                        return 'var(--green)';
                    }
                    return 'var(--text-primary)';
                };

                const formatBytes = (b) => {
                    if (b > 1e9) return (b / 1e9).toFixed(1) + ' GB';
                    if (b > 1e6) return (b / 1e6).toFixed(1) + ' MB';
                    return (b / 1e3).toFixed(1) + ' KB';
                };

                const metricLabel = {cpu_util: 'CPU', mem_util: 'Memory', gpu_util: 'GPU', gpu_mem_util: 'GPU Mem', net_recv_bytes: 'Net In', net_send_bytes: 'Net Out'};

                // Sparkline data
                const sparklines = {};
                (timeseries || []).forEach(t => {
                    const key = t.node_name + '|' + t.metric_name;
                    if (!sparklines[key]) sparklines[key] = [];
                    sparklines[key].push(t.avg_value);
                });

                const Sparkline = ({values, color, width, height}) => {
                    width = width || 80; height = height || 24;
                    if (!values || values.length < 2) return null;
                    const max = Math.max(...values), min = Math.min(...values);
                    const range = max - min || 1;
                    const step = width / (values.length - 1);
                    const points = values.map((v, i) => (i * step) + ',' + (height - ((v - min) / range) * (height - 2) - 1)).join(' ');
                    return React.createElement("svg", {width, height, style: {display: "block"}},
                        React.createElement("polyline", {points, fill: "none", stroke: color || "var(--accent)", strokeWidth: "1.5", strokeLinejoin: "round"})
                    );
                };

                const styles = {
                    container: {padding: "24px", width: "100%", overflow: "auto"},
                    header: {marginBottom: "24px"},
                    title: {fontSize: "20px", marginBottom: "8px"},
                    summaryRow: {display: "flex", gap: "24px", color: "var(--text-secondary)", fontSize: "13px"},
                    grid: {display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: "16px", marginBottom: "24px"},
                    card: (sel) => ({background: "var(--bg-secondary)", borderRadius: "8px", padding: "16px", cursor: "pointer", border: sel ? "1px solid var(--accent)" : "1px solid var(--border)", transition: "border-color 0.15s"}),
                    cardHeader: {display: "flex", justifyContent: "space-between", marginBottom: "12px"},
                    cardName: {fontWeight: 600, fontSize: "14px"},
                    cardSub: {fontSize: "12px", color: "var(--text-secondary)"},
                    badge: {fontSize: "11px", padding: "2px 8px", borderRadius: "4px", background: "var(--green-muted)", color: "var(--green)"},
                    metricsGrid: {display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "8px"},
                    metricCell: {textAlign: "center"},
                    metricName: {fontSize: "11px", color: "var(--text-secondary)", marginBottom: "4px"},
                    expanded: {marginTop: "12px", paddingTop: "12px", borderTop: "1px solid var(--border)", fontSize: "12px"},
                    detailGrid: {display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px"},
                    detailRow: {display: "flex", justifyContent: "space-between"},
                    costSection: {background: "var(--bg-secondary)", borderRadius: "8px", padding: "16px", border: "1px solid var(--border)"},
                    costGrid: {display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "12px"},
                    costBar: {height: "4px", background: "var(--bg-primary)", borderRadius: "2px", overflow: "hidden"},
                };

                return React.createElement("div", {style: styles.container},
                    // Header
                    React.createElement("div", {style: styles.header},
                        React.createElement("h2", {style: styles.title}, "Cloud Monitoring"),
                        React.createElement("div", {style: styles.summaryRow},
                            React.createElement("span", null, (summary.instance_count || 0) + " instances"),
                            React.createElement("span", null, (summary.total_metrics || 0).toLocaleString() + " metrics"),
                            React.createElement("span", null, (summary.providers || []).join(', ').toUpperCase()),
                            summary.total_cost_7d > 0 ? React.createElement("span", {style: {color: "var(--yellow)"}}, "$" + summary.total_cost_7d.toFixed(2) + " / 7d") : null
                        )
                    ),
                    // Instance cards
                    React.createElement("div", {style: styles.grid},
                        instances.map(inst => {
                            const m = metricsByInstance[inst.node_name] || {};
                            const c = costByInstance['EC2/' + inst.node_name];
                            const isSel = selectedInstance === inst.node_name;
                            return React.createElement("div", {key: inst.node_name, onClick: () => setSelectedInstance(isSel ? null : inst.node_name), style: styles.card(isSel)},
                                React.createElement("div", {style: styles.cardHeader},
                                    React.createElement("div", null,
                                        React.createElement("div", {style: styles.cardName}, inst.node_name),
                                        React.createElement("div", {style: styles.cardSub}, inst.instance_type + " \u00b7 " + inst.availability_zone)
                                    ),
                                    React.createElement("div", {style: styles.badge}, inst.source.toUpperCase())
                                ),
                                React.createElement("div", {style: styles.metricsGrid},
                                    ['cpu_util', 'mem_util', 'gpu_util'].map(metric => {
                                        const val = m[metric];
                                        if (!val) return null;
                                        const spark = sparklines[inst.node_name + '|' + metric];
                                        return React.createElement("div", {key: metric, style: styles.metricCell},
                                            React.createElement("div", {style: styles.metricName}, metricLabel[metric] || metric),
                                            React.createElement("div", {style: {fontSize: "18px", fontWeight: 600, color: metricColor(metric, val.avg_value)}}, val.avg_value.toFixed(0) + "%"),
                                            spark ? React.createElement(Sparkline, {values: spark, color: metricColor(metric, val.avg_value)}) : null
                                        );
                                    })
                                ),
                                isSel ? React.createElement("div", {style: styles.expanded},
                                    React.createElement("div", {style: styles.detailGrid},
                                        Object.entries(m).map(([name, val]) =>
                                            React.createElement("div", {key: name, style: styles.detailRow},
                                                React.createElement("span", {style: {color: "var(--text-secondary)"}}, metricLabel[name] || name),
                                                React.createElement("span", null, val.unit === 'bytes' ? formatBytes(val.avg_value) : val.avg_value.toFixed(1) + (val.unit === 'percent' ? '%' : ''))
                                            )
                                        )
                                    ),
                                    c ? React.createElement("div", {style: {marginTop: "8px", color: "var(--yellow)"}}, "Cost: $" + c.total_cost.toFixed(2) + " / 7d (avg $" + c.avg_daily_cost.toFixed(2) + "/day)") : null
                                ) : null
                            );
                        })
                    ),
                    // Cost breakdown
                    cost && cost.length > 0 ? React.createElement("div", {style: styles.costSection},
                        React.createElement("h3", {style: {fontSize: "14px", marginBottom: "12px"}}, "Cost Breakdown (7 days)"),
                        React.createElement("div", {style: styles.costGrid},
                            cost.map(c => {
                                const name = c.node_name.replace('EC2/', '');
                                const pct = summary.total_cost_7d > 0 ? (c.total_cost / summary.total_cost_7d * 100) : 0;
                                return React.createElement("div", {key: c.node_name},
                                    React.createElement("div", {style: {display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "4px"}},
                                        React.createElement("span", null, name),
                                        React.createElement("span", {style: {color: "var(--yellow)"}}, "$" + c.total_cost.toFixed(2))
                                    ),
                                    React.createElement("div", {style: styles.costBar},
                                        React.createElement("div", {style: {height: "100%", width: pct + "%", background: "var(--yellow)", borderRadius: "2px"}})
                                    )
                                );
                            })
                        )
                    ) : null
                );
            };

            const InteractivePanel = () => {
                const [sessions, setSessions] = useState(null);
                useEffect(() => {
                    fetch("/api/interactive")
                        .then(r => r.json())
                        .then(setSessions)
                        .catch(() => setSessions({servers: [], sessions: [], summary: {}}));
                }, []);
                if (!sessions) return React.createElement("div", {style: eduStyles.loading}, "Loading interactive sessions...");
                const {servers = [], sessions: sess = [], summary = {}} = sessions;
                return React.createElement("div", {style: eduStyles.panel},
                    React.createElement("div", {style: eduStyles.section}, "Interactive Computing Sessions"),
                    React.createElement("div", {style: eduStyles.cards},
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, summary.total_sessions || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Active Sessions")
                        ),
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, summary.idle_sessions || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Idle Sessions")
                        ),
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, ((summary.total_memory_mb || 0) / 1024).toFixed(1) + " GB"),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Total Memory")
                        ),
                        React.createElement("div", {style: eduStyles.card},
                            React.createElement("div", {style: eduStyles.cardValue}, summary.unique_users || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Active Users")
                        )
                    ),
                    React.createElement("div", {style: eduStyles.section}, "Sessions by Type"),
                    React.createElement("div", {style: eduStyles.cards},
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#4a9eff"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#4a9eff"}}, summary.rstudio_sessions || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "RStudio")
                        ),
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#f5a623"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#f5a623"}}, summary.jupyter_python_sessions || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Jupyter (Python)")
                        ),
                        React.createElement("div", {style: {...eduStyles.card, borderColor: "#7b68ee"}},
                            React.createElement("div", {style: {...eduStyles.cardValue, color: "#7b68ee"}}, summary.jupyter_r_sessions || 0),
                            React.createElement("div", {style: eduStyles.cardLabel}, "Jupyter (R)")
                        )
                    ),
                    React.createElement("div", {style: eduStyles.section}, "Active Sessions"),
                    React.createElement("table", {style: eduStyles.table},
                        React.createElement("thead", null,
                            React.createElement("tr", null,
                                React.createElement("th", {style: eduStyles.th}, "User"),
                                React.createElement("th", {style: eduStyles.th}, "Type"),
                                React.createElement("th", {style: eduStyles.th}, "Server"),
                                React.createElement("th", {style: eduStyles.th}, "Memory (MB)"),
                                React.createElement("th", {style: eduStyles.th}, "Age (hrs)"),
                                React.createElement("th", {style: eduStyles.th}, "Status")
                            )
                        ),
                        React.createElement("tbody", null,
                            sess.map((s, i) => React.createElement("tr", {key: i},
                                React.createElement("td", {style: eduStyles.td}, s.user),
                                React.createElement("td", {style: eduStyles.td}, s.session_type),
                                React.createElement("td", {style: eduStyles.td}, s.server_id),
                                React.createElement("td", {style: eduStyles.td}, Math.round(s.mem_mb)),
                                React.createElement("td", {style: eduStyles.td}, s.age_hours.toFixed(1)),
                                React.createElement("td", {style: eduStyles.td},
                                    React.createElement("span", {style: {color: s.is_idle ? "#f5a623" : "#4ade80"}}, s.is_idle ? "Idle" : "Active")
                                )
                            ))
                        )
                    )
                );
            };
        function App() {
            const [clusters, setClusters] = useState(null);
            const [nodes, setNodes] = useState(null);
            const [jobs, setJobs] = useState(null);
            const [edges, setEdges] = useState(null);
            const [featureStats, setFeatureStats] = useState(null);
            const [correlationData, setCorrelationData] = useState(null);
            const [suggestedAxes, setSuggestedAxes] = useState(null);
            const [networkStats, setNetworkStats] = useState(null);
            const [networkMethod, setNetworkMethod] = useState(null);
            const [clusteringQuality, setClusteringQuality] = useState(null);
            const [mlPredictions, setMlPredictions] = useState(null);
            const [features, setFeatures] = useState({});
                const [dataSource, setDataSource] = useState('loading...');
                const [nomadVersion, setNomadVersion] = useState('');
            



            const [activeTab, setActiveTab] = useState(null);
            const [selectedNode, setSelectedNode] = useState(null);
            const [queueRunning, setQueueRunning] = useState({});
            const [currentTime, setCurrentTime] = useState(new Date());
            
            useEffect(() => {
                fetch('/api/data')
                    .then(r => r.json())
                    .then(data => {
                        setClusters(data.clusters);
                        setNodes(data.nodes);
                        setJobs(data.jobs);
                        setEdges(data.edges);
                        setFeatureStats(data.feature_stats);
                        setCorrelationData(data.correlation_data);
                        setSuggestedAxes(data.suggested_axes);
                        setNetworkStats(data.network_stats);
                        setNetworkMethod(data.network_method);
                        setClusteringQuality(data.clustering_quality);
                        setMlPredictions(data.ml_predictions);
                        setDataSource(data.data_source || 'unknown');
                        setNomadVersion(data.nomad_version || '');
                        setFeatures(data.features || {});
                        setQueueRunning(data.queue_running || {});
                        setActiveTab('insights');
                    });
                    
                const timer = setInterval(() => setCurrentTime(new Date()), 1000);
                return () => clearInterval(timer);
            }, []);
            
            if (!clusters || !nodes || !jobs || !edges || !activeTab) {
                return (
                    <div style={{
                        height: '100vh',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        flexDirection: 'column',
                        gap: '16px'
                    }}>
                        <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'%3E%3Cdefs%3E%3Cstyle%3E.bg%7Bfill:%230a0a12%7D.ring%7Bfill:none;stroke:%2300BACF;stroke-width:6%7D.ring-inner%7Bfill:none;stroke:%2300BACF;stroke-width:3%7D.grid%7Bstroke:%2300BACF;stroke-width:1;opacity:.3%7D.oslash%7Bfont-family:Helvetica,Arial,sans-serif;font-size:28px;fill:%23B64326;font-weight:500%7D.needle-n%7Bfill:%23B64326%7D.needle-s%7Bfill:%2300BACF%7D.node%7Bfill:%2300BACF%7D.cardinal%7Bfont-family:Helvetica,Arial,sans-serif;font-size:10px;fill:%2300BACF;font-weight:500%7D%3C/style%3E%3C/defs%3E%3Ccircle class='bg' cx='100' cy='100' r='98'/%3E%3Ccircle class='ring' cx='100' cy='100' r='95'/%3E%3Ccircle class='ring-inner' cx='100' cy='100' r='60'/%3E%3Cg class='grid'%3E%3Cline x1='100' y1='5' x2='100' y2='195'/%3E%3Cline x1='5' y1='100' x2='195' y2='100'/%3E%3Cline x1='32' y1='32' x2='168' y2='168'/%3E%3Cline x1='168' y1='32' x2='32' y2='168'/%3E%3C/g%3E%3Ccircle class='grid' cx='100' cy='100' r='78' fill='none'/%3E%3Ccircle class='node' cx='100' cy='42' r='3'/%3E%3Ccircle class='node' cx='158' cy='100' r='3'/%3E%3Ccircle class='node' cx='100' cy='158' r='3'/%3E%3Ccircle class='node' cx='42' cy='100' r='3'/%3E%3Ctext class='cardinal' x='100' y='56' text-anchor='middle'%3EN%3C/text%3E%3Ctext class='cardinal' x='146' y='104' text-anchor='middle'%3EE%3C/text%3E%3Ctext class='cardinal' x='100' y='150' text-anchor='middle'%3ES%3C/text%3E%3Ctext class='cardinal' x='54' y='104' text-anchor='middle'%3EW%3C/text%3E%3Cg transform='rotate(45,100,100)'%3E%3Cpolygon class='needle-n' points='100,65 96,100 104,100'/%3E%3Cpolygon class='needle-s' points='100,135 104,100 96,100'/%3E%3C/g%3E%3Ccircle cx='100' cy='100' r='18' fill='%230a0a12' stroke='%2300BACF' stroke-width='2'/%3E%3Ctext class='oslash' x='100' y='108' text-anchor='middle'%3EØ%3C/text%3E%3C/svg%3E" style={{ width: 48, height: 48, borderRadius: 8 }} alt="NØMAÐ-HPC" />
                        <div style={{ color: 'var(--text-muted)' }}>Loading NØMAÐ...</div>
                    </div>
                );
            }
            
            return (
                <div>
                    <header className="header">
                        <div className="logo">
                            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'%3E%3Cdefs%3E%3Cstyle%3E.bg%7Bfill:%230a0a12%7D.ring%7Bfill:none;stroke:%2300BACF;stroke-width:6%7D.ring-inner%7Bfill:none;stroke:%2300BACF;stroke-width:3%7D.grid%7Bstroke:%2300BACF;stroke-width:1;opacity:.3%7D.oslash%7Bfont-family:Helvetica,Arial,sans-serif;font-size:28px;fill:%23B64326;font-weight:500%7D.needle-n%7Bfill:%23B64326%7D.needle-s%7Bfill:%2300BACF%7D.node%7Bfill:%2300BACF%7D.cardinal%7Bfont-family:Helvetica,Arial,sans-serif;font-size:10px;fill:%2300BACF;font-weight:500%7D%3C/style%3E%3C/defs%3E%3Ccircle class='bg' cx='100' cy='100' r='98'/%3E%3Ccircle class='ring' cx='100' cy='100' r='95'/%3E%3Ccircle class='ring-inner' cx='100' cy='100' r='60'/%3E%3Cg class='grid'%3E%3Cline x1='100' y1='5' x2='100' y2='195'/%3E%3Cline x1='5' y1='100' x2='195' y2='100'/%3E%3Cline x1='32' y1='32' x2='168' y2='168'/%3E%3Cline x1='168' y1='32' x2='32' y2='168'/%3E%3C/g%3E%3Ccircle class='grid' cx='100' cy='100' r='78' fill='none'/%3E%3Ccircle class='node' cx='100' cy='42' r='3'/%3E%3Ccircle class='node' cx='158' cy='100' r='3'/%3E%3Ccircle class='node' cx='100' cy='158' r='3'/%3E%3Ccircle class='node' cx='42' cy='100' r='3'/%3E%3Ctext class='cardinal' x='100' y='56' text-anchor='middle'%3EN%3C/text%3E%3Ctext class='cardinal' x='146' y='104' text-anchor='middle'%3EE%3C/text%3E%3Ctext class='cardinal' x='100' y='150' text-anchor='middle'%3ES%3C/text%3E%3Ctext class='cardinal' x='54' y='104' text-anchor='middle'%3EW%3C/text%3E%3Cg transform='rotate(45,100,100)'%3E%3Cpolygon class='needle-n' points='100,65 96,100 104,100'/%3E%3Cpolygon class='needle-s' points='100,135 104,100 96,100'/%3E%3C/g%3E%3Ccircle cx='100' cy='100' r='18' fill='%230a0a12' stroke='%2300BACF' stroke-width='2'/%3E%3Ctext class='oslash' x='100' y='108' text-anchor='middle'%3E%C3%98%3C/text%3E%3C/svg%3E" className="logo-icon logo-dark" style={{ width: 32, height: 32, borderRadius: 6 }} alt="NØMAÐ-HPC" />
                            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'%3E%3Cdefs%3E%3Cstyle%3E.bg%7Bfill:%23f5f6f8%7D.ring%7Bfill:none;stroke:%2300BACF;stroke-width:6%7D.ring-inner%7Bfill:none;stroke:%2300BACF;stroke-width:3%7D.grid%7Bstroke:%2300BACF;stroke-width:1;opacity:.4%7D.oslash%7Bfont-family:Helvetica,Arial,sans-serif;font-size:28px;fill:%23B64326;font-weight:500%7D.needle-n%7Bfill:%23B64326%7D.needle-s%7Bfill:%2300BACF%7D.node%7Bfill:%2300BACF%7D.cardinal%7Bfont-family:Helvetica,Arial,sans-serif;font-size:10px;fill:%2300BACF;font-weight:500%7D%3C/style%3E%3C/defs%3E%3Ccircle class='bg' cx='100' cy='100' r='98'/%3E%3Ccircle class='ring' cx='100' cy='100' r='95'/%3E%3Ccircle class='ring-inner' cx='100' cy='100' r='60'/%3E%3Cg class='grid'%3E%3Cline x1='100' y1='5' x2='100' y2='195'/%3E%3Cline x1='5' y1='100' x2='195' y2='100'/%3E%3Cline x1='32' y1='32' x2='168' y2='168'/%3E%3Cline x1='168' y1='32' x2='32' y2='168'/%3E%3C/g%3E%3Ccircle class='grid' cx='100' cy='100' r='78' fill='none'/%3E%3Ccircle class='node' cx='100' cy='42' r='3'/%3E%3Ccircle class='node' cx='158' cy='100' r='3'/%3E%3Ccircle class='node' cx='100' cy='158' r='3'/%3E%3Ccircle class='node' cx='42' cy='100' r='3'/%3E%3Ctext class='cardinal' x='100' y='56' text-anchor='middle'%3EN%3C/text%3E%3Ctext class='cardinal' x='146' y='104' text-anchor='middle'%3EE%3C/text%3E%3Ctext class='cardinal' x='100' y='150' text-anchor='middle'%3ES%3C/text%3E%3Ctext class='cardinal' x='54' y='104' text-anchor='middle'%3EW%3C/text%3E%3Cg transform='rotate(45,100,100)'%3E%3Cpolygon class='needle-n' points='100,65 96,100 104,100'/%3E%3Cpolygon class='needle-s' points='100,135 104,100 96,100'/%3E%3C/g%3E%3Ccircle cx='100' cy='100' r='18' fill='%23f5f6f8' stroke='%2300BACF' stroke-width='2'/%3E%3Ctext class='oslash' x='100' y='108' text-anchor='middle'%3E%C3%98%3C/text%3E%3C/svg%3E" className="logo-icon logo-light" style={{ width: 32, height: 32, borderRadius: 6, display: 'none' }} alt="NØMAÐ-HPC" />
                            <span style={{color:'#00BACF'}}>N<span style={{color:'#B64326'}}>Ø</span>MAÐ</span>
                        </div>
                        
                        <nav className="tabs">
                            <div
                                className={`tab ${activeTab === 'insights' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('insights'); setSelectedNode(null); }}
                            >
                                Insights
                            </div>
                            {Object.entries(clusters).filter(([id, cluster]) => cluster.type !== "workstation").map(([id, cluster]) => {
                                const clusterNodes = Object.values(nodes).filter(n => n.cluster === id);
                                const downCount = clusterNodes.filter(n => n.status === 'down').length;
                                return (
                                    <div
                                        key={id}
                                        className={`tab ${activeTab === id ? 'active' : ''}`}
                                        onClick={() => { setActiveTab(id); setSelectedNode(null); }}
                                    >
                                        {cluster.name}
                                        {downCount > 0 && (
                                            <span className="tab-badge" style={{ background: 'var(--red-muted)', color: 'var(--red)' }}>
                                                {downCount}
                                            </span>
                                        )}
                                    </div>
                                );
                            })}
                            <div
                                className={`tab ${activeTab === 'network' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('network'); setSelectedNode(null); }}
                            >
                                Network View
                            </div>
                            <div
                                className={`tab ${activeTab === 'resources' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('resources'); setSelectedNode(null); }}
                            >
                                Resources
                            </div>
                            <div
                                className={`tab ${activeTab === 'activity' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('activity'); setSelectedNode(null); }}
                            >
                                Activity
                            </div>
                            <div
                                className={`tab ${activeTab === 'interactive' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('interactive'); setSelectedNode(null); }}
                            >
                                Interactive
                            </div>
                            <div
                                className={`tab ${activeTab === 'workstations' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('workstations'); setSelectedNode(null); }}
                            >
                                Workstations
                            </div>
                            <div
                                className={`tab ${activeTab === 'storage' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('storage'); setSelectedNode(null); }}
                            >
                                Storage
                            </div>
                            {features.cloud !== false && <div
                                className={`tab ${activeTab === 'cloud' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('cloud'); setSelectedNode(null); }}
                            >
                                Cloud
                            </div>}

                            <div
                                className={`tab ${activeTab === 'dynamics' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('dynamics'); setSelectedNode(null); }}
                            >
                                Dynamics
                            </div>
                            <div
                                className={`tab ${activeTab === 'education' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('education'); setSelectedNode(null); }}
                            >
                                Education
                            </div>
                            <div
                                className={`tab ${activeTab === 'reference' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('reference'); setSelectedNode(null); }}
                            >
                                Reference
                            </div>
                            <div
                                className={`tab ${activeTab === 'issue' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('issue'); setSelectedNode(null); }}
                            >
                                Report Issue
                            </div>
                            <div
                                className={`tab ${activeTab === 'readiness' ? 'active' : ''}`}
                                onClick={() => { setActiveTab('readiness'); setSelectedNode(null); }}
                            >
                                Readiness
                            </div>
                        </nav>
                        
                        <div className="header-right">
                            <button className="theme-toggle" onClick={() => { document.body.classList.toggle("light-theme"); localStorage.setItem("nomad-theme", document.body.classList.contains("light-theme") ? "light" : "dark"); }}>🌓 Theme</button>
                            <div className="data-source">{dataSource}{nomadVersion ? " | v" + nomadVersion : ""}</div>
                            <div className="timestamp">
                                {currentTime.toLocaleTimeString()}
                            </div>
                        </div>
                    </header>
                    
                    <main className="main">
                        {activeTab === 'network' ? (
                            <NetworkView 
                                jobs={jobs} 
                                edges={edges} 
                                featureStats={featureStats}
                                correlationData={correlationData}
                                suggestedAxes={suggestedAxes}
                                networkStats={networkStats}
                                networkMethod={networkMethod}
                                clusteringQuality={clusteringQuality}
                                mlPredictions={mlPredictions}
                            />
                        ) : activeTab === 'resources' ? (
                            <ResourcesPanel />
                        ) : activeTab === 'activity' ? (
                            <ActivityPanel />
                        ) : activeTab === 'interactive' ? (
                            <InteractivePanel />
                        ) : activeTab === 'workstations' ? (
                            <WorkstationsPanel />
                        ) : activeTab === 'storage' ? (
                            <StoragePanel />
                        ) : activeTab === 'cloud' ? (
                            <CloudPanel />
                        ) : activeTab === 'insights' ? (
                            <InsightsPanel />
                        ) : activeTab === 'education' ? (
                            <EducationPanel />
                        ) : activeTab === 'dynamics' ? (
                            <DynamicsPanel />
                        ) : activeTab === 'reference' ? (
                            <ReferencePanel />
                        ) : activeTab === 'readiness' ? (
                            <ReadinessPanel />
                        ) : activeTab === 'issue' ? (
                            <ReportIssuePanel />
                        ) : (
                            <>
                                <ClusterView
                                    cluster={clusters[activeTab]}
                                    clusterName={activeTab}
                                    nodes={Object.values(nodes).filter(n => n.cluster === activeTab)}
                                    selectedNode={selectedNode}
                                    onSelectNode={setSelectedNode}
                                    queueRunning={queueRunning}
                                />
                                <NodeSidebar node={selectedNode ? nodes[selectedNode] : null} />
                            </>
                        )}
                    </main>
                </div>
            );
        }
        
        function ClusterView({ cluster, clusterName, nodes, selectedNode, onSelectNode, queueRunning }) {
            const stats = useMemo(() => {
                const online = nodes.filter(n => n.status === 'online');
                // Deduplicate nodes (same node appears in multiple partitions)
                const seen = new Set();
                let runningJobs = 0, pendingJobs = 0, successJobs = 0, failedJobs = 0;
                online.forEach(n => {
                    if (!seen.has(n.name)) {
                        seen.add(n.name);
                        runningJobs += (n.jobs_running || 0);
                        pendingJobs += (n.jobs_pending || 0);
                        successJobs += (n.jobs_success || 0);
                        failedJobs += (n.jobs_failed || 0);
                    }
                });
                // Override with queue_state data if available (more accurate)
                const qr = queueRunning[clusterName] || queueRunning[cluster?.name];
                if (qr) {
                    runningJobs = qr.running || runningJobs;
                    pendingJobs = qr.pending || pendingJobs;
                }
                const totalCompleted = successJobs + failedJobs;
                const avgSuccess = totalCompleted > 0
                    ? successJobs / totalCompleted
                    : (runningJobs > 0 ? 1.0 : 0);
                return {
                    online: online.length,
                    down: nodes.length - online.length,
                    runningJobs,
                    pendingJobs,
                    successJobs,
                    failedJobs,
                    avgSuccess
                };
            }, [nodes]);
            
            const getHealthColor = (rate) => {
                if (rate >= 0.85) return 'green';
                if (rate >= 0.60) return 'yellow';
                return 'red';
            };
            
            // Workstation cluster: show workstation cards instead of SLURM view
            if (cluster.type === 'workstation' || nodes.length === 0) {
                const [wsData, setWsData] = useState(null);
                const [expandedHosts, setExpandedHosts] = useState({});
                const [userDataByHost, setUserDataByHost] = useState({});
                const [hideSystemByHost, setHideSystemByHost] = useState({});
                useEffect(() => {
                    fetch("/api/workstations")
                        .then(r => r.json())
                        .then(setWsData);
                }, []);
                const toggleExpand = (hostname) => {
                    const isOpening = !expandedHosts[hostname];
                    setExpandedHosts(prev => ({...prev, [hostname]: isOpening}));
                    if (isOpening && !userDataByHost[hostname]) {
                        fetch("/api/workstation_users?hostname=" + encodeURIComponent(hostname))
                            .then(r => r.json())
                            .then(d => setUserDataByHost(prev => ({...prev, [hostname]: d.users || []})));
                    }
                };
                const isSystemUser = (u) => {
                    if (u.uid !== null && u.uid !== undefined && u.uid < 1000) return true;
                    const systemNames = ['root','daemon','bin','sys','sync','games','man','lp','mail','news','uucp','proxy','www-data','backup','list','irc','gnats','nobody','systemd-network','systemd-resolve','systemd-timesync','messagebus','sshd','polkitd','chrony','avahi','colord','rtkit','pulse','gdm','lightdm','dnsmasq','tcpdump','uuidd','named','postfix','cockpit-ws','cockpit-wsinstance','nm-openvpn','nm-openconnect'];
                    return systemNames.indexOf(u.username) !== -1;
                };
                const humanBytes = (b) => {
                    if (!b && b !== 0) return '—';
                    if (b < 1024) return b + ' B';
                    if (b < 1024*1024) return (b/1024).toFixed(1) + ' KB';
                    if (b < 1024*1024*1024) return (b/(1024*1024)).toFixed(1) + ' MB';
                    return (b/(1024*1024*1024)).toFixed(2) + ' GB';
                };
                const humanAge = (epochSec) => {
                    if (!epochSec) return '—';
                    const now = Math.floor(Date.now() / 1000);
                    const age = now - epochSec;
                    if (age < 0) return 'future?';
                    if (age < 60) return age + 's';
                    if (age < 3600) return Math.floor(age/60) + 'm';
                    if (age < 86400) return Math.floor(age/3600) + 'h ' + Math.floor((age%3600)/60) + 'm';
                    return Math.floor(age/86400) + 'd ' + Math.floor((age%86400)/3600) + 'h';
                };
                const siteWs = wsData ? (wsData.workstations || []).filter(
                    w => w.source_site === clusterName || w.source_site === cluster.name
                ) : [];
                const online = siteWs.filter(w => w.status === 'online').length;
                const total = siteWs.length;
                const byDept = {};
                siteWs.forEach(w => {
                    const d = w.department || 'ungrouped';
                    if (!byDept[d]) byDept[d] = [];
                    byDept[d].push(w);
                });
                const memColor = (pct) => pct > 90 ? '#f87171' : pct > 70 ? '#f5a623' : '#4ade80';
                const diskColor = (pct) => pct > 90 ? '#f87171' : pct > 80 ? '#f5a623' : '#4ade80';
                const loadColor = (load, cpus) => {
                    const r = load / (cpus || 1);
                    return r > 1.5 ? '#f87171' : r > 0.8 ? '#f5a623' : '#4ade80';
                };
                return (
                    <div className="content">
                        <div className="cluster-header">
                            <h1 className="cluster-title">{cluster.name}</h1>
                            <p className="cluster-desc">Workstation Group</p>
                        </div>
                        <div className="stats-bar">
                            <div className="stat">
                                <div className="stat-value green">{online}</div>
                                <div className="stat-label">Online</div>
                            </div>
                            <div className="stat">
                                <div className="stat-value red">{total - online}</div>
                                <div className="stat-label">Offline</div>
                            </div>
                            <div className="stat">
                                <div className="stat-value">{total}</div>
                                <div className="stat-label">Total</div>
                            </div>
                        </div>
                        {!wsData ? (
                            <div style={{padding: '2rem', opacity: 0.5}}>Loading workstations...</div>
                        ) : siteWs.length === 0 ? (
                            <div style={{padding: '2rem', opacity: 0.5}}>No workstation data yet.</div>
                        ) : (
                            Object.entries(byDept).map(([dept, machines]) => (
                                <div key={dept} style={{marginBottom: '1.5rem'}}>
                                    <div style={{fontWeight: 'bold', fontSize: '1.1rem', marginBottom: '0.75rem', opacity: 0.8}}>
                                        {dept} ({machines.length})
                                    </div>
                                    <div style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: '0.75rem'}}>
                                        {machines.map(w => {
                                            const memPct = w.memory_total_mb ? ((w.memory_used_mb / w.memory_total_mb) * 100) : 0;
                                            return (
                                                <div key={w.hostname} style={{
                                                    background: 'var(--bg-secondary, #1e293b)',
                                                    border: '1px solid var(--border)',
                                                    borderRadius: '8px',
                                                    padding: '1rem',
                                                }}>
                                                    <div style={{display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem'}}>
                                                        <span style={{fontWeight: 'bold'}}>{w.hostname}</span>
                                                        <span style={{color: w.status === 'online' ? '#4ade80' : '#f87171', fontSize: '0.8rem', fontWeight: 'bold'}}>
                                                            {(w.status || '').toUpperCase()}
                                                        </span>
                                                    </div>
                                                    <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.25rem', fontSize: '0.85rem'}}>
                                                        <span style={{opacity: 0.6}}>CPU</span>
                                                        <span style={{textAlign: 'right', color: loadColor(w.load_avg_1m || 0, w.cpu_count || 1)}}>
                                                            {(w.load_avg_1m || 0).toFixed(1)} / {w.cpu_count || '?'} cores
                                                        </span>
                                                        <span style={{opacity: 0.6}}>Memory</span>
                                                        <span style={{textAlign: 'right', color: memColor(memPct)}}>
                                                            {memPct.toFixed(0)}% ({((w.memory_used_mb || 0) / 1024).toFixed(1)} GB)
                                                        </span>
                                                        <span style={{opacity: 0.6}}>Disk</span>
                                                        <span style={{textAlign: 'right', color: diskColor(w.disk_usage_pct || 0)}}>
                                                            {(w.disk_usage_pct || 0).toFixed(0)}% ({(w.disk_free_gb || 0).toFixed(0)} GB free)
                                                        </span>
                                                        <span style={{opacity: 0.6}}>Users</span>
                                                        <span style={{textAlign: 'right'}}>{w.users_logged_in || 0} logged in</span>
                                                    </div>
                                                    {/* Expand / per-user section */}
                                                    <div
                                                        onClick={(e) => { e.stopPropagation(); toggleExpand(w.hostname); }}
                                                        style={{
                                                            marginTop: '0.5rem',
                                                            paddingTop: '0.5rem',
                                                            borderTop: '1px solid var(--border)',
                                                            cursor: 'pointer',
                                                            opacity: 0.7,
                                                            fontSize: '0.8rem',
                                                            textAlign: 'center',
                                                        }}
                                                    >
                                                        {expandedHosts[w.hostname] ? '∧ hide users' : '∨ show users'}
                                                    </div>
                                                    {expandedHosts[w.hostname] && (() => {
                                                        const users = userDataByHost[w.hostname];
                                                        if (!users) return (<div style={{padding:'0.5rem',opacity:0.5,fontSize:'0.8rem'}}>Loading...</div>);
                                                        if (users.length === 0) return (<div style={{padding:'0.5rem',opacity:0.5,fontSize:'0.8rem'}}>No per-user data. Probe may not be deployed on this host.</div>);
                                                        const hideSystem = hideSystemByHost[w.hostname] !== false;
                                                        const visible = hideSystem ? users.filter(u => !isSystemUser(u)) : users;
                                                        const sysCount = users.filter(isSystemUser).length;
                                                        return (
                                                            <div style={{marginTop:'0.5rem',fontSize:'0.8rem'}}>
                                                                <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:'0.35rem',opacity:0.7}}>
                                                                    <span>Users ({visible.length}{hideSystem && sysCount > 0 ? ' + ' + sysCount + ' system' : ''})</span>
                                                                    <label style={{cursor:'pointer',fontSize:'0.75rem'}}>
                                                                        <input
                                                                            type="checkbox"
                                                                            checked={!hideSystem}
                                                                            onChange={(e) => { e.stopPropagation(); setHideSystemByHost(prev => ({...prev, [w.hostname]: e.target.checked})); }}
                                                                            style={{marginRight:'0.25rem'}}
                                                                        />
                                                                        show system
                                                                    </label>
                                                                </div>
                                                                {visible.length === 0 ? (
                                                                    <div style={{opacity:0.5,padding:'0.25rem 0'}}>No human users active.</div>
                                                                ) : (
                                                                    <div style={{display:'grid',gridTemplateColumns:'1fr auto auto',gap:'0.15rem 0.5rem'}}>
                                                                        <span style={{fontWeight:'bold',opacity:0.6,fontSize:'0.72rem'}}>USER</span>
                                                                        <span style={{fontWeight:'bold',opacity:0.6,fontSize:'0.72rem',textAlign:'right'}}>MEM</span>
                                                                        <span style={{fontWeight:'bold',opacity:0.6,fontSize:'0.72rem',textAlign:'right'}}>PIDS</span>
                                                                        {visible.map(u => (
                                                                            <React.Fragment key={u.username}>
                                                                                <span title={'UID ' + u.uid + ' · session ' + humanAge(u.session_epoch) + ' ago'}>
                                                                                    {u.username}{isSystemUser(u) ? ' · sys' : ''}
                                                                                </span>
                                                                                <span style={{textAlign:'right'}}>{humanBytes(u.memory_current_bytes)}</span>
                                                                                <span style={{textAlign:'right'}}>{u.pids_current}</span>
                                                                            </React.Fragment>
                                                                        ))}
                                                                    </div>
                                                                )}
                                                            </div>
                                                        );
                                                    })()}
                                                </div>
                                            );
                                        })}
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                );
            }
            
            return (
                <div className="content">
                    <div className="cluster-header">
                        <h1 className="cluster-title">{cluster.name}</h1>
                        <p className="cluster-desc">{cluster.description}</p>
                    </div>
                    
                    <div className="stats-bar">
                        <div className="stat">
                            <div className="stat-value green">{stats.online}</div>
                            <div className="stat-label">Online</div>
                        </div>
                        <div className="stat">
                            <div className="stat-value red">{stats.down}</div>
                            <div className="stat-label">Down</div>
                        </div>
                        <div className="stat">
                            <div className="stat-value" style={{color: stats.runningJobs > 0 ? "#3b82f6" : "inherit"}}>{stats.runningJobs.toLocaleString()}</div>
                            <div className="stat-label">Running</div>
                        </div>
                        <div className="stat">
                            <div className="stat-value" style={{color: stats.pendingJobs > 0 ? "#f59e0b" : "inherit"}}>{stats.pendingJobs.toLocaleString()}</div>
                            <div className="stat-label">Pending</div>
                        </div>
                        <div className="stat">
                            <div className="stat-value green">{stats.successJobs.toLocaleString()}</div>
                            <div className="stat-label">Succeeded</div>
                        </div>
                        <div className="stat">
                            <div className="stat-value red">{stats.failedJobs.toLocaleString()}</div>
                            <div className="stat-label">Failed</div>
                        </div>
                        <div className="stat">
                            <div className={`stat-value ${getHealthColor(stats.avgSuccess)}`}>
                                {(stats.avgSuccess * 100).toFixed(1)}%
                            </div>
                            <div className="stat-label">Avg Success</div>
                        </div>
                    </div>
                    
                    {(() => {
                        // Group nodes by partition
                        const partitions = cluster.partitions || {};
                        const partitionNames = Object.keys(partitions);
                        
                        // If no partition info, render flat grid
                        if (partitionNames.length === 0) {
                            return (
                                <div className="node-grid">
                                    {nodes.map(node => (
                                        <div
                                            key={node.name}
                                            className={`node-card ${node.status === 'down' ? 'down' : ''} ${selectedNode === node.name ? 'selected' : ''}`}
                                            onClick={() => onSelectNode(node.name)}
                                        >
                                            <div className="node-name">{node.name}</div>
                                            <div className={`node-indicator ${node.status === 'down' ? 'offline' : getHealthColor(node.success_rate || 0)}`}>
                                                {node.status === 'down' ? '—' : `${Math.round((node.success_rate || 0) * 100)}%`}
                                            </div>
                                            <div className="node-jobs">
                                                {node.status === 'down' ? (node.slurm_state || 'OFFLINE') : (node.jobs_running > 0 ? `${node.jobs_running} running` : `${node.jobs_today || 0} jobs`)}
                                            </div>
                                            <div className="node-gpu-badge" style={{ background: node.has_gpu ? "#1a1a1a" : "rgba(255,255,255,0.9)", color: node.has_gpu ? "#ffffff" : "#1a1a1a" }}>{node.has_gpu ? "GPU" : "CPU"}</div>
                                        </div>
                                    ))}
                                </div>
                            );
                        }
                        
                        // Render partition sections
                        return partitionNames.map(partName => {
                            const partNodeNames = partitions[partName] || [];
                            const partNodes = nodes.filter(n => partNodeNames.includes(n.name));
                            if (partNodes.length === 0) return null;
                            
                            const online = partNodes.filter(n => n.status === 'online');
                            const down = partNodes.length - online.length;
                            const hasGpu = partNodes.some(n => n.has_gpu);
                            
                            // Calculate utilization
                            const avgCpu = online.length > 0 
                                ? Math.round(online.reduce((s, n) => s + (n.cpu_util || 0), 0) / online.length)
                                : 0;
                            const avgMem = online.length > 0
                                ? Math.round(online.reduce((s, n) => s + (n.mem_util || 0), 0) / online.length)
                                : 0;
                            const avgGpu = hasGpu && online.length > 0
                                ? Math.round(online.filter(n => n.has_gpu).reduce((s, n) => s + (n.gpu_util || 0), 0) / online.filter(n => n.has_gpu).length)
                                : 0;
                            
                            // Job stats for this partition
                            const totalRunning = partNodes.reduce((s, n) => s + (n.jobs_running || 0), 0);
                            const totalJobs = partNodes.reduce((s, n) => s + (n.jobs_today || 0), 0);
                            const okJobs = partNodes.reduce((s, n) => s + (n.jobs_success || 0), 0);
                            const failJobs = totalJobs - okJobs;
                            
                            // Partition type description
                            const partType = hasGpu ? 'GPU-accelerated partition' 
                                : partName.toLowerCase().includes('highmem') ? 'High-memory partition'
                                : partName.toLowerCase().includes('debug') ? 'Debug partition'
                                : partName.toLowerCase().includes('short') ? 'Short jobs partition'
                                : 'General CPU partition';
                            
                            return (
                                <div key={partName} className="partition-section">
                                    <div className="partition-header">
                                        <div className="partition-title">
                                            <span className="partition-name">{partName}</span>
                                            <span className="partition-type">{partType}</span>
                                            <span className="partition-count">
                                                {online.length}/{partNodes.length} nodes
                                                {down > 0 && <span className="partition-down"> ({down} down)</span>}
                                            </span>
                                        </div>
                                        <div className="partition-stats">
                                            <span className="partition-jobs">
                                                {totalRunning > 0 ? <><span style={{color: '#3b82f6'}}>{totalRunning} running</span>{'  '}</> : ''}{okJobs > 0 ? <><span style={{color: '#22c55e'}}>{okJobs} succeeded</span>{'  '}</> : ''}{failJobs > 0 ? <span style={{color: '#ef4444'}}>{failJobs} fail</span> : ''}{totalRunning === 0 && okJobs === 0 && failJobs === 0 ? '0 jobs' : ''}
                                            </span>
                                        </div>
                                        <div className="partition-bars">
                                            <div className="util-bar">
                                                <span className="util-label">CPU</span>
                                                <div className="util-track">
                                                    <div className="util-fill cpu" style={{width: avgCpu + '%'}}></div>
                                                </div>
                                                <span className="util-value">{avgCpu}%</span>
                                            </div>
                                            <div className="util-bar">
                                                <span className="util-label">Memory</span>
                                                <div className="util-track">
                                                    <div className="util-fill mem" style={{width: avgMem + '%'}}></div>
                                                </div>
                                                <span className="util-value">{avgMem}%</span>
                                            </div>
                                            {hasGpu && (
                                                <div className="util-bar">
                                                    <span className="util-label">GPU</span>
                                                    <div className="util-track">
                                                        <div className="util-fill gpu" style={{width: avgGpu + '%'}}></div>
                                                    </div>
                                                    <span className="util-value">{avgGpu}%</span>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                    <div className="node-grid">
                                        {partNodes.map(node => (
                                            <div
                                                key={node.name}
                                                className={`node-card ${node.status === 'down' ? 'down' : ''} ${selectedNode === node.name ? 'selected' : ''}`}
                                                onClick={() => onSelectNode(node.name)}
                                            >
                                                <div className="node-name">{node.name}</div>
                                                <div className={`node-indicator ${node.status === 'down' ? 'offline' : getHealthColor(node.success_rate || 0)}`}>
                                                    {node.status === 'down' ? '—' : `${Math.round((node.success_rate || 0) * 100)}%`}
                                                </div>
                                                <div className="node-jobs">
                                                    {node.status === 'down' ? (node.slurm_state || 'OFFLINE') : (node.jobs_running > 0 ? `${node.jobs_running} running` : `${node.jobs_today || 0} jobs`)}
                                                </div>
                                                <div className="node-gpu-badge" style={{ background: node.has_gpu ? "#1a1a1a" : "rgba(255,255,255,0.9)", color: node.has_gpu ? "#ffffff" : "#1a1a1a" }}>{node.has_gpu ? "GPU" : "CPU"}</div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            );
                        });
                    })()}
                </div>
            );
        }
        
        function NodeSidebar({ node }) {
            if (!node) {
                return (
                    <aside className="sidebar">
                        <div className="sidebar-empty">
                            <div className="sidebar-empty-icon">◇</div>
                            <div>Select a node to view details</div>
                        </div>
                    </aside>
                );
            }
            
            const getHealthColor = (rate) => {
                if (rate >= 0.85) return 'green';
                if (rate >= 0.60) return 'yellow';
                return 'red';
            };
            
            return (
                <aside className="sidebar">
                    <div className="node-detail-header">
                        <span className="node-detail-name">{node.name}</span>
                        <span className={`node-status-badge ${node.status}`}>
                            {node.slurm_state || node.status}
                        </span>
                    </div>
                    
                    {node.status === 'down' ? (
                        <div className="detail-section">
                            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: '32px 0' }}>
                                <div style={{ fontSize: '32px', marginBottom: '8px' }}>⚠</div>
                                <div>Node is offline</div>
                                {node.drain_reason && (
                                    <div style={{ fontSize: '12px', marginTop: '8px', color: 'var(--red)' }}>
                                        {node.drain_reason}
                                    </div>
                                )}
                                <div style={{ fontSize: '12px', marginTop: '8px' }}>
                                    Last seen: {new Date(node.last_seen).toLocaleString()}
                                </div>
                            </div>
                        </div>
                    ) : (
                        <>
                            <div className="detail-section">
                                <div className="detail-section-title">Job Statistics</div>
                                <div className="detail-row">
                                    <span className="detail-label">Running</span>
                                    <span className="detail-value" style={{color: "#3b82f6"}}>{node.jobs_running || 0}</span>
                                </div>
                                <div className="detail-row">
                                    <span className="detail-label">Pending</span>
                                    <span className="detail-value" style={{color: "#f59e0b"}}>{node.jobs_pending || 0}</span>
                                </div>
                                <div className="detail-row">
                                    <span className="detail-label">Succeeded</span>
                                    <span className="detail-value green">{node.jobs_success || 0}</span>
                                </div>
                                <div className="detail-row">
                                    <span className="detail-label">Failed</span>
                                    <span className="detail-value red">{node.jobs_failed || 0}</span>
                                </div>
                                <div className="detail-row">
                                    <span className="detail-label">Success Rate</span>
                                    <span className={`detail-value ${getHealthColor(node.success_rate || 0)}`}>
                                        {((node.success_rate || 0) * 100).toFixed(1)}%
                                    </span>
                                </div>
                            </div>
                            
                            <div className="detail-section">
                                <div className="detail-section-title">Resource Utilization</div>
                                <div className="detail-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '4px' }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                                        <span className="detail-label">CPU</span>
                                        <span className="detail-value">{node.cpu_util || 0}%</span>
                                    </div>
                                    <div className="progress-bar">
                                        <div 
                                            className={`progress-fill ${(node.cpu_util || 0) > 90 ? 'red' : (node.cpu_util || 0) > 70 ? 'yellow' : 'cyan'}`}
                                            style={{ width: `${node.cpu_util || 0}%` }}
                                        />
                                    </div>
                                </div>
                                <div className="detail-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: '4px' }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                                        <span className="detail-label">Memory</span>
                                        <span className="detail-value">{node.mem_util || 0}%</span>
                                    </div>
                                    <div className="progress-bar">
                                        <div 
                                            className={`progress-fill ${(node.mem_util || 0) > 90 ? 'red' : (node.mem_util || 0) > 70 ? 'yellow' : 'green'}`}
                                            style={{ width: `${node.mem_util || 0}%` }}
                                        />
                                    </div>
                                </div>
                                {node.has_gpu && (() => {
                                    const healthBadge = node.gpu_health && node.gpu_health !== "OK"
                                        ? React.createElement("span", {className: "health-badge " + node.gpu_health}, node.gpu_health)
                                        : null;
                                    const dcgmBadge = node.gpu_data_source === "dcgm"
                                        ? React.createElement("span", {className: "dcgm-badge", title: "Enhanced metrics via DCGM"}, "DCGM")
                                        : null;
                                    const wc = node.gpu_workload || "";
                                    const workloadClass = wc.includes("tensor-heavy") ? "tensor-heavy" : wc.includes("tensor") ? "tensor" : (wc.includes("FP64") || wc.includes("HPC")) ? "fp64" : wc.includes("memory") ? "memory" : wc.includes("compute") ? "compute" : (wc.includes("I/O") || wc.includes("data")) ? "io" : wc === "idle" ? "idle" : "other";
                                    return React.createElement(React.Fragment, null,
                                        React.createElement("div", {className: "detail-row", style: {flexDirection: "column", alignItems: "stretch", gap: "4px"}},
                                            React.createElement("div", {style: {display: "flex", justifyContent: "space-between", alignItems: "center"}},
                                                React.createElement("span", {className: "detail-label"}, "GPU (" + (node.gpu_name || "GPU") + ")", healthBadge, dcgmBadge),
                                                React.createElement("span", {className: "detail-value"}, (node.gpu_util || 0) + "%")
                                            ),
                                            React.createElement("div", {className: "progress-bar"},
                                                React.createElement("div", {className: "progress-fill purple", style: {width: (node.gpu_util || 0) + "%"}})
                                            )
                                        ),
                                        node.gpu_real_util != null && React.createElement("div", {className: "detail-row", style: {flexDirection: "column", alignItems: "stretch", gap: "4px"}},
                                            React.createElement("div", {style: {display: "flex", justifyContent: "space-between"}},
                                                React.createElement("span", {className: "detail-label", style: {color: "var(--text-muted)", fontSize: "11px"}}, "Real Util"),
                                                React.createElement("span", {className: "detail-value", style: {fontSize: "11px"}}, node.gpu_real_util + "%")
                                            ),
                                            React.createElement("div", {className: "progress-bar", style: {height: "4px"}},
                                                React.createElement("div", {className: "progress-fill", style: {width: node.gpu_real_util + "%", background: "linear-gradient(90deg, #0072B2, #56B4E9)"}})
                                            )
                                        ),
                                        node.gpu_workload && React.createElement("div", {className: "detail-row", style: {paddingTop: "2px"}},
                                            React.createElement("span", {className: "workload-badge " + workloadClass}, node.gpu_workload)
                                        )
                                    );
                                })()}
                                <div className="detail-row">
                                    <span className="detail-label">Load Average</span>
                                    <span className="detail-value">{node.load_avg || 0}</span>
                                </div>
                            </div>
                            
                            {node.failures && Object.keys(node.failures).length > 0 && (
                                <div className="detail-section">
                                    <div className="detail-section-title">Failure Breakdown</div>
                                    <div className="failure-list">
                                        {Object.entries(node.failures)
                                            .sort((a, b) => b[1] - a[1])
                                            .map(([type, count]) => (
                                                <div key={type} className="failure-item">
                                                    <span className="failure-count">{count}</span>
                                                    <span className="failure-type">{type}</span>
                                                </div>
                                            ))}
                                    </div>
                                </div>
                            )}
                            
                            {node.top_users && node.top_users.length > 0 && (
                                <div className="detail-section">
                                    <div className="detail-section-title">Top Users</div>
                                    <div className="user-list">
                                        {node.top_users.map(({ user, jobs }) => (
                                            <div key={user} className="user-item">
                                                <div className="user-avatar">
                                                    {user[0].toUpperCase()}
                                                </div>
                                                <span className="user-name">{user}</span>
                                                <span className="user-jobs">{jobs} jobs</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </>
                    )}
                </aside>
            );
        }
        
        function NetworkView({ jobs, edges, featureStats, correlationData, suggestedAxes, networkStats, networkMethod, clusteringQuality, mlPredictions }) {
            const containerRef = useRef(null);
            const sceneRef = useRef(null);
            const nodeGroupRef = useRef(null);
            const edgeGroupRef = useRef(null);
            const [viewMode, setViewMode] = useState('force'); // Default to force-directed
            const [showStats, setShowStats] = useState(false);
            const [showCorrelation, setShowCorrelation] = useState(false);
            const [showMethod, setShowMethod] = useState(false);
            const [showClustering, setShowClustering] = useState(false);
            const [showML, setShowML] = useState(false);
            const [mlTraining, setMlTraining] = useState(false);
            const [forceIterations, setForceIterations] = useState(0);
            const forcePositionsRef = useRef(null);
            const animationRef = useRef(null);
            
            // Empty state when no completed jobs
            if (!jobs || jobs.length === 0) {
                return (
                    <div className="content">
                        <div className="cluster-header">
                            <h1 className="cluster-title">Job Network</h1>
                            <p className="cluster-desc">3D force-directed layout — connected jobs cluster together</p>
                        </div>
                        <div style={{display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "60vh", color: "var(--text-muted)"}}>
                            <div style={{fontSize: "48px", marginBottom: "16px", opacity: 0.3}}>◇</div>
                            <div style={{fontSize: "18px", marginBottom: "8px"}}>Waiting for completed jobs</div>
                            <div style={{fontSize: "13px", maxWidth: "400px", textAlign: "center", lineHeight: "1.6"}}>
                                The network visualization builds from completed job data. Jobs currently running will appear here once they finish and their metrics are recorded.
                            </div>
                        </div>
                    </div>
                );
            }

            // Force-directed layout computation
            const computeForceLayout = useMemo(() => {
                if (!jobs || !edges) return null;
                
                const n = jobs.length;
                
                // Initialize positions randomly in a sphere
                const positions = jobs.map(() => ({
                    x: (Math.random() - 0.5) * 50 + 25,
                    y: (Math.random() - 0.5) * 50 + 25,
                    z: (Math.random() - 0.5) * 50 + 25,
                    vx: 0, vy: 0, vz: 0
                }));
                
                // Build adjacency list for faster edge lookup
                const neighbors = new Map();
                for (let i = 0; i < n; i++) neighbors.set(i, new Set());
                edges.forEach(e => {
                    neighbors.get(e.source).add(e.target);
                    neighbors.get(e.target).add(e.source);
                });
                
                // Fruchterman-Reingold parameters
                const area = 50 * 50 * 50;
                const k = Math.pow(area / n, 1/3) * 0.5; // Optimal distance
                const iterations = 150;
                let temperature = 50;
                const cooling = 0.95;
                
                for (let iter = 0; iter < iterations; iter++) {
                    // Calculate repulsive forces (all pairs)
                    const disp = positions.map(() => ({ x: 0, y: 0, z: 0 }));
                    
                    for (let i = 0; i < n; i++) {
                        for (let j = i + 1; j < n; j++) {
                            const dx = positions[i].x - positions[j].x;
                            const dy = positions[i].y - positions[j].y;
                            const dz = positions[i].z - positions[j].z;
                            const dist = Math.sqrt(dx*dx + dy*dy + dz*dz) || 0.01;
                            
                            // Repulsive force: k^2 / dist
                            const force = (k * k) / dist;
                            const fx = (dx / dist) * force;
                            const fy = (dy / dist) * force;
                            const fz = (dz / dist) * force;
                            
                            disp[i].x += fx; disp[i].y += fy; disp[i].z += fz;
                            disp[j].x -= fx; disp[j].y -= fy; disp[j].z -= fz;
                        }
                    }
                    
                    // Calculate attractive forces (edges only)
                    edges.forEach(e => {
                        const i = e.source, j = e.target;
                        const dx = positions[i].x - positions[j].x;
                        const dy = positions[i].y - positions[j].y;
                        const dz = positions[i].z - positions[j].z;
                        const dist = Math.sqrt(dx*dx + dy*dy + dz*dz) || 0.01;
                        
                        // Attractive force: dist^2 / k, scaled by similarity
                        const strength = e.similarity || 1;
                        const force = (dist * dist / k) * strength;
                        const fx = (dx / dist) * force;
                        const fy = (dy / dist) * force;
                        const fz = (dz / dist) * force;
                        
                        disp[i].x -= fx; disp[i].y -= fy; disp[i].z -= fz;
                        disp[j].x += fx; disp[j].y += fy; disp[j].z += fz;
                    });
                    
                    // Apply displacements with temperature limiting
                    for (let i = 0; i < n; i++) {
                        const dispMag = Math.sqrt(disp[i].x**2 + disp[i].y**2 + disp[i].z**2) || 0.01;
                        const scale = Math.min(dispMag, temperature) / dispMag;
                        
                        positions[i].x += disp[i].x * scale;
                        positions[i].y += disp[i].y * scale;
                        positions[i].z += disp[i].z * scale;
                        
                        // Keep in bounds
                        positions[i].x = Math.max(0, Math.min(50, positions[i].x));
                        positions[i].y = Math.max(0, Math.min(50, positions[i].y));
                        positions[i].z = Math.max(0, Math.min(50, positions[i].z));
                    }
                    
                    temperature *= cooling;
                }
                
                return positions;
            }, [jobs, edges]);
            
            // Available numeric features for axis selection
            const availableFeatures = useMemo(() => {
                if (!featureStats) return [];
                return Object.entries(featureStats)
                    .filter(([k, v]) => v.range > 0)  // Only features with variation
                    .sort((a, b) => b[1].cv - a[1].cv)  // Sort by coefficient of variation
                    .map(([k, v]) => ({ name: k, ...v }));
            }, [featureStats]);
            
            // Default axes from suggestions or fallback
            const defaultAxes = useMemo(() => {
                if (suggestedAxes && suggestedAxes.length >= 3) {
                    return { x: suggestedAxes[0], y: suggestedAxes[1], z: suggestedAxes[2] };
                }
                return { x: 'runtime_sec', y: 'wait_time_sec', z: 'total_write_mb' };
            }, [suggestedAxes]);
            
            const [axisX, setAxisX] = useState(defaultAxes.x);
            const [axisY, setAxisY] = useState(defaultAxes.y);
            const [axisZ, setAxisZ] = useState(defaultAxes.z);
            
            // Update defaults when suggestions change
            useEffect(() => {
                if (suggestedAxes && suggestedAxes.length >= 3) {
                    setAxisX(suggestedAxes[0]);
                    setAxisY(suggestedAxes[1]);
                    setAxisZ(suggestedAxes[2]);
                }
            }, [suggestedAxes]);
            
            // Get correlation between two features
            const getCorrelation = (f1, f2) => {
                if (!correlationData || !correlationData.features) return 0;
                const idx1 = correlationData.features.indexOf(f1);
                const idx2 = correlationData.features.indexOf(f2);
                if (idx1 === -1 || idx2 === -1) return 0;
                return correlationData.matrix[idx1][idx2];
            };
            
            // Check for correlations between selected axes
            const axisCorrelations = useMemo(() => {
                const warnings = [];
                const pairs = [
                    { a: 'X', b: 'Y', f1: axisX, f2: axisY },
                    { a: 'X', b: 'Z', f1: axisX, f2: axisZ },
                    { a: 'Y', b: 'Z', f1: axisY, f2: axisZ },
                ];
                for (const { a, b, f1, f2 } of pairs) {
                    const r = getCorrelation(f1, f2);
                    if (Math.abs(r) >= 0.7) {
                        warnings.push({
                            axes: `${a}-${b}`,
                            features: [f1, f2],
                            correlation: r,
                            strength: Math.abs(r) >= 0.85 ? 'strong' : 'moderate'
                        });
                    }
                }
                return warnings;
            }, [axisX, axisY, axisZ, correlationData]);
            
            const stats = useMemo(() => {
                // Count by failure_reason
                // 0=success, 1=timeout, 2=cancelled, 3=failed, 4=oom, 5=segfault, 6=node_fail, 7=dependency
                const counts = { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0 };
                
                jobs.forEach(j => {
                    const fr = j.failure_reason;
                    if (fr !== undefined && fr !== null) {
                        counts[fr] = (counts[fr] || 0) + 1;
                    } else {
                        // Fallback to state-based
                        const state = (j.state || '').toUpperCase();
                        if (state === 'COMPLETED') counts[0]++;
                        else if (state === 'TIMEOUT') counts[1]++;
                        else if (state === 'CANCELLED') counts[2]++;
                        else if (state === 'OUT_OF_MEMORY') counts[4]++;
                        else if (state === 'NODE_FAIL') counts[6]++;
                        else counts[3]++;
                    }
                });
                
                return {
                    total: jobs.length,
                    completed: counts[0],
                    timeout: counts[1],
                    cancelled: counts[2],
                    failed: counts[3],
                    oom: counts[4],
                    segfault: counts[5],
                    nodeFail: counts[6],
                    dependency: counts[7],
                    successRate: (counts[0] / jobs.length * 100).toFixed(1),
                    edges: edges.length,
                    counts: counts
                };
            }, [jobs, edges]);
            
            // Normalize values to 0-50 range for visualization
            const normalizeValue = (value, feature) => {
                if (!featureStats || !featureStats[feature]) return value * 0.5;
                const { min, max } = featureStats[feature];
                if (max === min) return 25;
                return ((value - min) / (max - min)) * 50;
            };
            
            const getPosition = (job, index) => {
                if (viewMode === 'force' && computeForceLayout) {
                    return computeForceLayout[index];
                }
                if (viewMode === 'pca') {
                    return pcaPositions[index];
                }
                return {
                    x: normalizeValue(job[axisX] || 0, axisX),
                    y: normalizeValue(job[axisY] || 0, axisY),
                    z: normalizeValue(job[axisZ] || 0, axisZ)
                };
            };
            
            const pcaPositions = useMemo(() => {
                const data = jobs.map(j => [
                    j[axisX] || 0,
                    j[axisY] || 0,
                    j[axisZ] || 0
                ]);
                const means = [0, 1, 2].map(i => data.reduce((s, d) => s + d[i], 0) / data.length);
                const centered = data.map(d => d.map((v, i) => v - means[i]));
                
                const cov = [[0,0,0],[0,0,0],[0,0,0]];
                for (let i = 0; i < 3; i++) {
                    for (let j = 0; j < 3; j++) {
                        cov[i][j] = centered.reduce((s, d) => s + d[i] * d[j], 0) / (data.length - 1);
                    }
                }
                
                const powerIteration = (mat, numIter = 50) => {
                    let v = [1, 1, 1];
                    for (let iter = 0; iter < numIter; iter++) {
                        const newV = [0, 0, 0];
                        for (let i = 0; i < 3; i++) {
                            for (let j = 0; j < 3; j++) {
                                newV[i] += mat[i][j] * v[j];
                            }
                        }
                        const norm = Math.sqrt(newV.reduce((s, x) => s + x*x, 0));
                        v = newV.map(x => x / norm);
                    }
                    return v;
                };
                
                const pc1 = powerIteration(cov);
                const deflated = cov.map((row, i) => row.map((val, j) => val - pc1[i] * pc1[j] * cov[i][j] * 10));
                const pc2 = powerIteration(deflated);
                const pc3 = [
                    pc1[1]*pc2[2] - pc1[2]*pc2[1],
                    pc1[2]*pc2[0] - pc1[0]*pc2[2],
                    pc1[0]*pc2[1] - pc1[1]*pc2[0]
                ];
                
                return centered.map(d => ({
                    x: (d[0]*pc1[0] + d[1]*pc1[1] + d[2]*pc1[2]) * 0.8 + 25,
                    y: (d[0]*pc2[0] + d[1]*pc2[1] + d[2]*pc2[2]) * 0.8 + 25,
                    z: (d[0]*pc3[0] + d[1]*pc3[1] + d[2]*pc3[2]) * 0.8 + 25
                }));
            }, [jobs, axisX, axisY, axisZ]);
            
            // Update positions when axes or view mode change
            useEffect(() => {
                if (!nodeGroupRef.current || !edgeGroupRef.current) return;
                
                nodeGroupRef.current.children.forEach((mesh, i) => {
                    const pos = getPosition(jobs[i], i);
                    mesh.position.set(pos.x, pos.y, pos.z);
                });
                
                edgeGroupRef.current.children.forEach((line, i) => {
                    const edge = edges[i];
                    if (!edge) return;
                    const pos1 = getPosition(jobs[edge.source], edge.source);
                    const pos2 = getPosition(jobs[edge.target], edge.target);
                    
                    const positions = line.geometry.attributes.position.array;
                    positions[0] = pos1.x; positions[1] = pos1.y; positions[2] = pos1.z;
                    positions[3] = pos2.x; positions[4] = pos2.y; positions[5] = pos2.z;
                    line.geometry.attributes.position.needsUpdate = true;
                });
            }, [viewMode, axisX, axisY, axisZ, jobs, edges, pcaPositions, computeForceLayout]);
            
            useEffect(() => {
                if (!containerRef.current || !jobs || !edges) return;
                
                const container = containerRef.current;
                const width = container.clientWidth;
                const height = container.clientHeight;
                
                const scene = new THREE.Scene();
                const updateBackground = () => {
                    const isLight = document.body.classList.contains('light-theme');
                    scene.background = new THREE.Color(isLight ? 0xffffff : 0x161b22);
                };
                updateBackground();
                // Listen for theme changes
                const observer = new MutationObserver(updateBackground);
                observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
                sceneRef.current = scene;
                
                const camera = new THREE.PerspectiveCamera(60, width / height, 0.1, 1000);
                camera.position.set(80, 80, 80);
                camera.lookAt(0, 0, 0);
                
                const renderer = new THREE.WebGLRenderer({ antialias: true });
                renderer.setSize(width, height);
                renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
                container.appendChild(renderer.domElement);
                
                // Raycaster for mouse interaction
                const raycaster = new THREE.Raycaster();
                const mouse = new THREE.Vector2();
                
                // Tooltip element
                const tooltip = document.createElement('div');
                tooltip.style.cssText = `
                    position: absolute;
                    background: rgba(22, 27, 34, 0.95);
                    border: 1px solid #30363d;
                    border-radius: 8px;
                    padding: 12px 16px;
                    font-size: 11px;
                    color: #e6edf3;
                    pointer-events: none;
                    display: none;
                    z-index: 1000;
                    max-width: 280px;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
                `;
                container.appendChild(tooltip);
                
                const gridHelper = new THREE.GridHelper(100, 20, 0x30363d, 0x21262d);
                scene.add(gridHelper);
                
                const axesGroup = new THREE.Group();
                const axisMaterial = new THREE.LineBasicMaterial({ color: 0x6e7681 });
                
                [[100, 0, 0], [0, 100, 0], [0, 0, 100]].forEach(([x, y, z]) => {
                    const points = [new THREE.Vector3(0, 0, 0), new THREE.Vector3(x, y, z)];
                    const geometry = new THREE.BufferGeometry().setFromPoints(points);
                    const line = new THREE.Line(geometry, axisMaterial);
                    axesGroup.add(line);
                });
                scene.add(axesGroup);
                
                const nodeGeometry = new THREE.SphereGeometry(0.8, 16, 16);
                
                // Failure reason colors (factor 0-7)
                // 0=success, 1=timeout, 2=cancelled, 3=failed_generic, 4=oom, 5=segfault, 6=node_fail, 7=dependency
                const failureColors = {
                    0: 0x3fb950,  // Success - Green
                    1: 0xd29922,  // Timeout - Yellow/Amber
                    2: 0x6e7681,  // Cancelled - Gray
                    3: 0xf85149,  // Failed (generic) - Red
                    4: 0xa371f7,  // OOM - Purple
                    5: 0xda3633,  // Segfault - Dark Red
                    6: 0xdb6d28,  // Node Fail - Orange
                    7: 0x79c0ff,  // Dependency - Cyan
                };
                
                const getJobColorHex = (job) => {
                    const fr = job.failure_reason;
                    if (fr !== undefined && fr !== null && failureColors[fr]) {
                        return failureColors[fr];
                    }
                    // Fallback to state-based coloring
                    const state = (job.state || '').toUpperCase();
                    if (state === 'COMPLETED') return 0x3fb950;
                    if (state === 'TIMEOUT') return 0xd29922;
                    if (state === 'CANCELLED') return 0x6e7681;
                    if (state === 'OUT_OF_MEMORY') return 0xa371f7;
                    if (state === 'NODE_FAIL') return 0xdb6d28;
                    return 0xf85149;
                };
                
                const getJobMaterial = (job) => {
                    const color = getJobColorHex(job);
                    return new THREE.MeshBasicMaterial({ color: color });
                };
                
                const nodeGroup = new THREE.Group();
                jobs.forEach((job, i) => {
                    const material = getJobMaterial(job);
                    const mesh = new THREE.Mesh(nodeGeometry, material);
                    const pos = getPosition(job, i);
                    mesh.position.set(pos.x, pos.y, pos.z);
                    mesh.userData = { index: i, job, originalColor: getJobColorHex(job) };
                    nodeGroup.add(mesh);
                });
                scene.add(nodeGroup);
                nodeGroupRef.current = nodeGroup;
                
                const edgeMaterial = new THREE.LineBasicMaterial({ 
                    color: 0x58a6ff, 
                    transparent: true, 
                    opacity: 0.15 
                });
                
                const edgeGroup = new THREE.Group();
                edges.forEach(edge => {
                    const pos1 = getPosition(jobs[edge.source], edge.source);
                    const pos2 = getPosition(jobs[edge.target], edge.target);
                    
                    const points = [
                        new THREE.Vector3(pos1.x, pos1.y, pos1.z),
                        new THREE.Vector3(pos2.x, pos2.y, pos2.z)
                    ];
                    
                    const geometry = new THREE.BufferGeometry().setFromPoints(points);
                    const line = new THREE.Line(geometry, edgeMaterial.clone());
                    line.userData = { edge };
                    edgeGroup.add(line);
                });
                scene.add(edgeGroup);
                edgeGroupRef.current = edgeGroup;
                
                let isDragging = false;
                let previousMousePosition = { x: 0, y: 0 };
                let theta = Math.PI / 4;
                let phi = Math.PI / 4;
                let radius = 120;
                let hoveredNode = null;
                
                const updateCamera = () => {
                    camera.position.x = radius * Math.sin(phi) * Math.cos(theta);
                    camera.position.y = radius * Math.cos(phi);
                    camera.position.z = radius * Math.sin(phi) * Math.sin(theta);
                    camera.lookAt(25, 25, 25);
                };
                
                updateCamera();
                
                const formatValue = (val) => {
                    if (val === undefined || val === null) return '—';
                    if (typeof val === 'number') {
                        if (val > 1000) return val.toLocaleString();
                        if (val % 1 !== 0) return val.toFixed(2);
                    }
                    return String(val);
                };
                
                const showTooltip = (mesh, event) => {
                    const job = mesh.userData.job;
                    
                    // Failure reason labels and colors
                    const failureLabels = {
                        0: { label: 'SUCCESS', color: '#3fb950' },
                        1: { label: 'TIMEOUT', color: '#d29922' },
                        2: { label: 'CANCELLED', color: '#6e7681' },
                        3: { label: 'FAILED', color: '#f85149' },
                        4: { label: 'OOM', color: '#a371f7' },
                        5: { label: 'SEGFAULT', color: '#da3633' },
                        6: { label: 'NODE_FAIL', color: '#db6d28' },
                        7: { label: 'DEPENDENCY', color: '#79c0ff' },
                    };
                    
                    const fr = job.failure_reason;
                    const frInfo = failureLabels[fr] || failureLabels[3];
                    const stateColor = frInfo.color;
                    const stateLabel = frInfo.label;
                    
                    // Exit code info
                    let exitInfo = '';
                    if (job.exit_code !== null && job.exit_code !== undefined) {
                        exitInfo = `<div><span style="color: #8b949e;">Exit Code:</span> ${job.exit_code}</div>`;
                    }
                    if (job.exit_signal !== null && job.exit_signal !== undefined) {
                        const sigNames = { 6: 'SIGABRT', 9: 'SIGKILL', 11: 'SIGSEGV', 15: 'SIGTERM' };
                        const sigName = sigNames[job.exit_signal] || `SIG${job.exit_signal}`;
                        exitInfo += `<div><span style="color: #8b949e;">Signal:</span> ${sigName}</div>`;
                    }
                    
                    tooltip.innerHTML = `
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: 600; font-size: 13px;">Job ${job.job_id}</span>
                            <span style="background: ${stateColor}22; color: ${stateColor}; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600;">${stateLabel}</span>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; font-size: 10px;">
                            <div><span style="color: #8b949e;">User:</span> ${job.user_name || '—'}</div>
                            <div><span style="color: #8b949e;">Cluster:</span> ${job.source_site || '—'}</div>
                            <div><span style="color: #8b949e;">Partition:</span> ${job.partition || '—'}</div>
                            <div><span style="color: #8b949e;">Runtime:</span> ${formatValue(job.runtime_sec)}s</div>
                            <div><span style="color: #8b949e;">Wait:</span> ${formatValue(job.wait_time_sec)}s</div>
                            <div><span style="color: #8b949e;">CPUs:</span> ${formatValue(job.req_cpus)}</div>
                            <div><span style="color: #8b949e;">Memory:</span> ${formatValue(job.req_mem_mb)} MB</div>
                            <div><span style="color: #8b949e;">Write:</span> ${formatValue(job.total_write_mb)} MB</div>
                            ${exitInfo}
                            ${job.health_score ? `<div><span style="color: #8b949e;">Health:</span> ${(job.health_score * 100).toFixed(0)}%</div>` : ''}
                            ${job.time_efficiency ? `<div><span style="color: #8b949e;">Time Eff:</span> ${(job.time_efficiency * 100).toFixed(0)}%</div>` : ''}
                        </div>
                    `;
                    
                    tooltip.style.display = 'block';
                    tooltip.style.left = (event.clientX - container.getBoundingClientRect().left + 15) + 'px';
                    tooltip.style.top = (event.clientY - container.getBoundingClientRect().top - 10) + 'px';
                };
                
                const hideTooltip = () => {
                    tooltip.style.display = 'none';
                };
                
                container.addEventListener('mousedown', (e) => {
                    isDragging = true;
                    previousMousePosition = { x: e.clientX, y: e.clientY };
                });
                
                container.addEventListener('mousemove', (e) => {
                    if (isDragging) {
                        const deltaX = e.clientX - previousMousePosition.x;
                        const deltaY = e.clientY - previousMousePosition.y;
                        
                        theta += deltaX * 0.005;
                        phi = Math.max(0.1, Math.min(Math.PI - 0.1, phi + deltaY * 0.005));
                        
                        updateCamera();
                        previousMousePosition = { x: e.clientX, y: e.clientY };
                        hideTooltip();
                        return;
                    }
                    
                    // Raycasting for hover
                    const rect = container.getBoundingClientRect();
                    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
                    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
                    
                    raycaster.setFromCamera(mouse, camera);
                    const intersects = raycaster.intersectObjects(nodeGroup.children);
                    
                    // Reset previous hover
                    if (hoveredNode && (!intersects.length || intersects[0].object !== hoveredNode)) {
                        hoveredNode.material.color.setHex(hoveredNode.userData.originalColor);
                        hoveredNode.scale.set(1, 1, 1);
                        hoveredNode = null;
                        hideTooltip();
                    }
                    
                    // Set new hover
                    if (intersects.length > 0) {
                        const mesh = intersects[0].object;
                        if (mesh !== hoveredNode) {
                            hoveredNode = mesh;
                            mesh.material.color.setHex(0x58a6ff);
                            mesh.scale.set(1.5, 1.5, 1.5);
                            showTooltip(mesh, e);
                        } else {
                            // Update tooltip position
                            tooltip.style.left = (e.clientX - rect.left + 15) + 'px';
                            tooltip.style.top = (e.clientY - rect.top - 10) + 'px';
                        }
                    }
                });
                
                container.addEventListener('mouseup', () => { isDragging = false; });
                container.addEventListener('mouseleave', () => { 
                    isDragging = false; 
                    hideTooltip();
                    if (hoveredNode) {
                        hoveredNode.material.color.setHex(hoveredNode.userData.originalColor);
                        hoveredNode.scale.set(1, 1, 1);
                        hoveredNode = null;
                    }
                });
                
                container.addEventListener('wheel', (e) => {
                    e.preventDefault();
                    radius = Math.max(50, Math.min(250, radius + e.deltaY * 0.1));
                    updateCamera();
                });
                
                const animate = () => {
                    requestAnimationFrame(animate);
                    renderer.render(scene, camera);
                };
                animate();
                
                return () => {
                    renderer.dispose();
                    if (tooltip.parentNode) tooltip.parentNode.removeChild(tooltip);
                    container.removeChild(renderer.domElement);
                };
            }, [jobs, edges]);
            
            const formatFeatureName = (name) => {
                return name.replace(/_/g, ' ').replace(/\b\\w/g, c => c.toUpperCase());
            };
            
            // Color scale for correlation heatmap
            const getCorrelationColor = (r) => {
                if (r >= 0.85) return '#3fb950';      // Strong positive - green
                if (r >= 0.7) return '#7ee787';       // Moderate positive - light green
                if (r >= 0.3) return '#a5d6a7';       // Weak positive - very light green
                if (r > -0.3) return '#6e7681';       // Near zero - gray
                if (r > -0.7) return '#ffab91';       // Weak negative - light red
                if (r > -0.85) return '#f85149';      // Moderate negative - red
                return '#da3633';                      // Strong negative - dark red
            };
            
            return (
                <div className="content" style={{ padding: '24px' }}>
                    <div className="cluster-header">
                        <h1 className="cluster-title">Job Network</h1>
                        <p className="cluster-desc">
                            {viewMode === 'force' 
                                ? '3D force-directed layout - connected jobs cluster together'
                                : '3D visualization of job similarity based on selected features'}
                        </p>
                    </div>
                    
                    <div className="network-container" ref={containerRef}>
                        <div className="network-controls">
                            <button 
                                className={`network-btn ${viewMode === 'force' ? 'active' : ''}`}
                                onClick={() => setViewMode('force')}
                            >
                                Force Layout
                            </button>
                            <button 
                                className={`network-btn ${viewMode === 'raw' ? 'active' : ''}`}
                                onClick={() => setViewMode('raw')}
                            >
                                Raw Axes
                            </button>
                            <button 
                                className={`network-btn ${viewMode === 'pca' ? 'active' : ''}`}
                                onClick={() => setViewMode('pca')}
                            >
                                PCA View
                            </button>
                            <button 
                                className={`network-btn ${showStats ? 'active' : ''}`}
                                onClick={() => { setShowStats(!showStats); setShowCorrelation(false); setShowMethod(false); setShowClustering(false); }}
                                style={{ marginLeft: '16px' }}
                            >
                                Variance
                            </button>
                            <button 
                                className={`network-btn ${showCorrelation ? 'active' : ''}`}
                                onClick={() => { setShowCorrelation(!showCorrelation); setShowStats(false); setShowMethod(false); setShowClustering(false); }}
                            >
                                Correlation
                            </button>
                            <button 
                                className={`network-btn ${showMethod ? 'active' : ''}`}
                                onClick={() => { setShowMethod(!showMethod); setShowStats(false); setShowCorrelation(false); setShowClustering(false); }}
                            >
                                Method
                            </button>
                            <button 
                                className={`network-btn ${showClustering ? 'active' : ''}`}
                                onClick={() => { setShowClustering(!showClustering); setShowStats(false); setShowCorrelation(false); setShowMethod(false); }}
                            >
                                Clustering
                            </button>
                            <button
                                className={`network-btn ${showML ? "active" : ""}`}
                                onClick={() => { setShowML(!showML); setShowStats(false); setShowCorrelation(false); setShowMethod(false); setShowClustering(false); }}
                                style={{ background: showML ? "#e74c3c" : "#3498db" }}
                            >
                                ML Risk
                            </button>
                        </div>
                        
                        {/* Correlation Warning */}
                        {axisCorrelations.length > 0 && (
                            <div style={{
                                position: 'absolute',
                                top: '60px',
                                left: '50%',
                                transform: 'translateX(-50%)',
                                background: 'rgba(210, 153, 34, 0.15)',
                                border: '1px solid var(--yellow)',
                                borderRadius: '8px',
                                padding: '8px 16px',
                                fontSize: '12px',
                                color: 'var(--yellow)',
                                display: 'flex',
                                alignItems: 'center',
                                gap: '8px',
                                zIndex: 100
                            }}>
                                <span style={{ fontSize: '16px' }}>⚠</span>
                                <span>
                                    {axisCorrelations.map(w => 
                                        `${w.axes}: r=${w.correlation.toFixed(2)} (${w.strength})`
                                    ).join(' | ')}
                                    {' '}- Consider selecting less correlated features
                                </span>
                            </div>
                        )}
                        
                        {/* Axis Selectors - only show for raw/pca modes */}
                        {viewMode !== 'force' && (
                        <div style={{
                            position: 'absolute',
                            bottom: '16px',
                            left: '16px',
                            background: 'var(--bg-elevated)',
                            border: '1px solid var(--border)',
                            borderRadius: '8px',
                            padding: '12px 16px',
                            fontSize: '12px',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '8px'
                        }}>
                            <div style={{ fontWeight: '600', marginBottom: '4px', color: 'var(--text-muted)' }}>
                                AXIS SELECTION
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span style={{ color: '#f85149', fontWeight: '600', width: '16px' }}>X</span>
                                <select 
                                    value={axisX} 
                                    onChange={(e) => setAxisX(e.target.value)}
                                    style={{
                                        background: 'var(--bg-hover)',
                                        border: '1px solid var(--border)',
                                        borderRadius: '4px',
                                        color: 'var(--text-primary)',
                                        padding: '4px 8px',
                                        fontSize: '11px',
                                        minWidth: '140px'
                                    }}
                                >
                                    {availableFeatures.map(f => (
                                        <option key={f.name} value={f.name}>
                                            {formatFeatureName(f.name)} (CV: {f.cv}%)
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span style={{ color: '#3fb950', fontWeight: '600', width: '16px' }}>Y</span>
                                <select 
                                    value={axisY} 
                                    onChange={(e) => setAxisY(e.target.value)}
                                    style={{
                                        background: 'var(--bg-hover)',
                                        border: '1px solid var(--border)',
                                        borderRadius: '4px',
                                        color: 'var(--text-primary)',
                                        padding: '4px 8px',
                                        fontSize: '11px',
                                        minWidth: '140px'
                                    }}
                                >
                                    {availableFeatures.map(f => (
                                        <option key={f.name} value={f.name}>
                                            {formatFeatureName(f.name)} (CV: {f.cv}%)
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span style={{ color: '#58a6ff', fontWeight: '600', width: '16px' }}>Z</span>
                                <select 
                                    value={axisZ} 
                                    onChange={(e) => setAxisZ(e.target.value)}
                                    style={{
                                        background: 'var(--bg-hover)',
                                        border: '1px solid var(--border)',
                                        borderRadius: '4px',
                                        color: 'var(--text-primary)',
                                        padding: '4px 8px',
                                        fontSize: '11px',
                                        minWidth: '140px'
                                    }}
                                >
                                    {availableFeatures.map(f => (
                                        <option key={f.name} value={f.name}>
                                            {formatFeatureName(f.name)} (CV: {f.cv}%)
                                        </option>
                                    ))}
                                </select>
                            </div>
                        </div>
                        )}
                        
                        {/* Feature Stats Panel */}
                        {showStats && (
                            <div style={{
                                position: 'absolute',
                                top: '60px',
                                left: '16px',
                                background: 'var(--bg-elevated)',
                                border: '1px solid var(--border)',
                                borderRadius: '8px',
                                padding: '12px 16px',
                                fontSize: '11px',
                                maxHeight: '300px',
                                overflowY: 'auto',
                                minWidth: '280px'
                            }}>
                                <div style={{ fontWeight: '600', marginBottom: '8px', color: 'var(--text-muted)' }}>
                                    FEATURE VARIANCE (sorted by CV)
                                </div>
                                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                                    <thead>
                                        <tr style={{ color: 'var(--text-muted)', borderBottom: '1px solid var(--border)' }}>
                                            <th style={{ textAlign: 'left', padding: '4px 0' }}>Feature</th>
                                            <th style={{ textAlign: 'right', padding: '4px 4px' }}>CV%</th>
                                            <th style={{ textAlign: 'right', padding: '4px 4px' }}>Range</th>
                                            <th style={{ textAlign: 'right', padding: '4px 0' }}>Non-0%</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {availableFeatures.slice(0, 12).map(f => (
                                            <tr key={f.name} style={{ 
                                                borderBottom: '1px solid var(--border)',
                                                background: [axisX, axisY, axisZ].includes(f.name) ? 'rgba(88, 166, 255, 0.1)' : 'transparent'
                                            }}>
                                                <td style={{ padding: '4px 0', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    {formatFeatureName(f.name)}
                                                </td>
                                                <td style={{ 
                                                    textAlign: 'right', 
                                                    padding: '4px 4px',
                                                    color: f.cv > 50 ? 'var(--green)' : f.cv > 20 ? 'var(--yellow)' : 'var(--text-muted)'
                                                }}>
                                                    {f.cv}%
                                                </td>
                                                <td style={{ textAlign: 'right', padding: '4px 4px', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    {f.range > 1000 ? `${(f.range/1000).toFixed(1)}k` : f.range.toFixed(1)}
                                                </td>
                                                <td style={{ 
                                                    textAlign: 'right', 
                                                    padding: '4px 0',
                                                    color: f.non_zero_pct > 80 ? 'var(--green)' : f.non_zero_pct > 50 ? 'var(--yellow)' : 'var(--red)'
                                                }}>
                                                    {f.non_zero_pct}%
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                                <div style={{ marginTop: '8px', color: 'var(--text-muted)', fontSize: '10px' }}>
                                    CV = Coefficient of Variation (higher = more spread)
                                </div>
                            </div>
                        )}
                        
                        {/* Correlation Matrix Panel */}
                        {showCorrelation && correlationData && correlationData.features && (
                            <div style={{
                                position: 'absolute',
                                top: '60px',
                                left: '16px',
                                background: 'var(--bg-elevated)',
                                border: '1px solid var(--border)',
                                borderRadius: '8px',
                                padding: '12px 16px',
                                fontSize: '10px',
                                maxHeight: '400px',
                                overflowY: 'auto',
                                overflowX: 'auto'
                            }}>
                                <div style={{ fontWeight: '600', marginBottom: '8px', color: 'var(--text-muted)', fontSize: '11px' }}>
                                    CORRELATION MATRIX
                                </div>
                                
                                {/* Heatmap */}
                                <div style={{ display: 'flex', gap: '1px' }}>
                                    {/* Row labels */}
                                    <div style={{ display: 'flex', flexDirection: 'column', marginRight: '4px' }}>
                                        <div style={{ height: '20px' }}></div>
                                        {correlationData.features.slice(0, 10).map(f => (
                                            <div key={f} style={{ 
                                                height: '20px', 
                                                display: 'flex', 
                                                alignItems: 'center',
                                                color: [axisX, axisY, axisZ].includes(f) ? 'var(--cyan)' : 'var(--text-secondary)',
                                                fontFamily: 'IBM Plex Mono, monospace',
                                                fontSize: '9px',
                                                whiteSpace: 'nowrap',
                                                paddingRight: '4px'
                                            }}>
                                                {f.length > 12 ? f.slice(0, 10) + '..' : f}
                                            </div>
                                        ))}
                                    </div>
                                    
                                    {/* Matrix cells */}
                                    <div>
                                        {/* Column labels */}
                                        <div style={{ display: 'flex', gap: '1px', marginBottom: '2px' }}>
                                            {correlationData.features.slice(0, 10).map(f => (
                                                <div key={f} style={{ 
                                                    width: '20px', 
                                                    height: '20px',
                                                    display: 'flex',
                                                    alignItems: 'flex-end',
                                                    justifyContent: 'center',
                                                    color: [axisX, axisY, axisZ].includes(f) ? 'var(--cyan)' : 'var(--text-muted)',
                                                    fontSize: '8px',
                                                    transform: 'rotate(-45deg)',
                                                    transformOrigin: 'center'
                                                }}>
                                                    {f.slice(0, 3)}
                                                </div>
                                            ))}
                                        </div>
                                        
                                        {/* Matrix rows */}
                                        {correlationData.matrix.slice(0, 10).map((row, i) => (
                                            <div key={i} style={{ display: 'flex', gap: '1px' }}>
                                                {row.slice(0, 10).map((r, j) => {
                                                    const f1 = correlationData.features[i];
                                                    const f2 = correlationData.features[j];
                                                    const isSelected = [axisX, axisY, axisZ].includes(f1) && 
                                                                      [axisX, axisY, axisZ].includes(f2);
                                                    return (
                                                        <div 
                                                            key={j}
                                                            title={`${f1} × ${f2}: r=${r.toFixed(2)}`}
                                                            style={{
                                                                width: '20px',
                                                                height: '20px',
                                                                background: getCorrelationColor(r),
                                                                display: 'flex',
                                                                alignItems: 'center',
                                                                justifyContent: 'center',
                                                                fontSize: '8px',
                                                                color: Math.abs(r) > 0.5 ? '#fff' : 'var(--text-muted)',
                                                                borderRadius: '2px',
                                                                border: isSelected ? '2px solid var(--cyan)' : 'none',
                                                                cursor: 'default'
                                                            }}
                                                        >
                                                            {i === j ? '—' : (Math.abs(r) >= 0.5 ? r.toFixed(1) : '')}
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                                
                                {/* Legend */}
                                <div style={{ marginTop: '12px', display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                                    <span style={{ color: 'var(--text-muted)' }}>Legend:</span>
                                    {[
                                        { r: 0.9, label: '+Strong' },
                                        { r: 0.5, label: '+Mod' },
                                        { r: 0, label: 'None' },
                                        { r: -0.5, label: '-Mod' },
                                        { r: -0.9, label: '-Strong' },
                                    ].map(({ r, label }) => (
                                        <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '2px' }}>
                                            <div style={{ 
                                                width: '12px', 
                                                height: '12px', 
                                                background: getCorrelationColor(r),
                                                borderRadius: '2px'
                                            }}></div>
                                            <span style={{ fontSize: '9px' }}>{label}</span>
                                        </div>
                                    ))}
                                </div>
                                
                                {/* High correlations list */}
                                {correlationData.high_correlations && correlationData.high_correlations.length > 0 && (
                                    <div style={{ marginTop: '12px', borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
                                        <div style={{ fontWeight: '600', marginBottom: '4px', color: 'var(--yellow)' }}>
                                            ⚠ High Correlations ({correlationData.high_correlations.length})
                                        </div>
                                        {correlationData.high_correlations.slice(0, 5).map((hc, i) => (
                                            <div key={i} style={{ 
                                                fontSize: '9px', 
                                                color: 'var(--text-secondary)',
                                                padding: '2px 0'
                                            }}>
                                                {hc.feature1} ↔ {hc.feature2}: <span style={{ color: 'var(--yellow)' }}>r={hc.correlation}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                        
                        {/* Network Method Panel */}
                        {showMethod && (
                            <div style={{
                                position: 'absolute',
                                top: '60px',
                                left: '16px',
                                background: 'var(--bg-elevated)',
                                border: '1px solid var(--border)',
                                borderRadius: '8px',
                                padding: '16px',
                                fontSize: '11px',
                                maxWidth: '320px'
                            }}>
                                <div style={{ fontWeight: '600', marginBottom: '12px', color: 'var(--cyan)', fontSize: '12px' }}>
                                    NETWORK METHOD
                                </div>
                                    <div style={{ marginBottom: '12px' }}>
                                    <div style={{ color: 'var(--text-primary)', fontWeight: '600', marginBottom: '4px' }}>
                                        Cosine Similarity (Continuous)
                                    </div>
                                    <div style={{ color: 'var(--text-muted)', fontSize: '10px', lineHeight: '1.5' }}>
                                        Jobs are connected based on similar resource usage vectors.
                                        Features are z-score normalized before comparison.
                                    </div>
                                </div>

                                <div style={{
                                    background: 'var(--bg-hover)',
                                    borderRadius: '6px',
                                    padding: '10px',
                                    marginBottom: '12px',
                                    fontFamily: 'IBM Plex Mono, monospace',
                                    fontSize: '10px'
                                }}>
                                    <div style={{ color: 'var(--text-muted)', marginBottom: '4px' }}>Formula:</div>
                                    <div style={{ color: 'var(--green)' }}>
                                        cos(θ) = (A · B) / (||A|| × ||B||)
                                    </div>
                                    <div style={{ color: 'var(--text-muted)', marginTop: '8px', fontSize: '9px' }}>
                                        A, B = job feature vectors<br/>
                                        Result: 1 = identical, 0 = orthogonal
                                    </div>
                                </div>

                                <div style={{ color: 'var(--text-muted)', fontSize: '10px', marginBottom: '12px' }}>
                                    <strong style={{ color: 'var(--text-secondary)' }}>Why Cosine?</strong><br/>
                                    Works on continuous features without discretization.
                                    Standard in ML, measures direction similarity regardless of magnitude.
                                </div>                               

                                {networkStats && (
                                    <div style={{ borderTop: '1px solid var(--border)', paddingTop: '12px' }}>
                                        <div style={{ color: 'var(--text-muted)', marginBottom: '8px', fontWeight: '600' }}>
                                            NETWORK STATISTICS
                                        </div>
                                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                                            <div>
                                                <div style={{ color: 'var(--text-muted)', fontSize: '9px' }}>Threshold</div>
                                                <div style={{ color: 'var(--cyan)', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    ≥ {networkStats.threshold || 0.5}
                                                </div>
                                            </div>
                                            <div>
                                                <div style={{ color: 'var(--text-muted)', fontSize: '9px' }}>Avg Similarity</div>
                                                <div style={{ color: 'var(--text-primary)', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    {networkStats.avg_similarity || '—'}
                                                </div>
                                            </div>
                                            <div>
                                                <div style={{ color: 'var(--text-muted)', fontSize: '9px' }}>Edge Density</div>
                                                <div style={{ color: 'var(--text-primary)', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    {((networkStats.edge_density || 0) * 100).toFixed(1)}%
                                                </div>
                                            </div>
                                            <div>
                                                <div style={{ color: 'var(--text-muted)', fontSize: '9px' }}>Comparisons</div>
                                                <div style={{ color: 'var(--text-primary)', fontFamily: 'IBM Plex Mono, monospace' }}>
                                                    {(networkStats.n_comparisons || 0).toLocaleString()}
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                )}
                                
                                <div style={{ 
                                    marginTop: '12px', 
                                    paddingTop: '12px', 
                                    borderTop: '1px solid var(--border)',
                                    color: 'var(--text-muted)',
                                    fontSize: '9px'
                                }}>
                                    <strong>Discretization:</strong> Features binned into low/med/high (quantile-based)
                                </div>
                            </div>
                        )}
                        
                        {/* Clustering Quality Panel */}
                        {showClustering && clusteringQuality && (
                            <div style={{
                                position: 'absolute',
                                top: '60px',
                                left: '16px',
                                background: 'var(--bg-elevated)',
                                border: '1px solid var(--border)',
                                borderRadius: '8px',
                                padding: '16px',
                                fontSize: '11px',
                                maxWidth: '360px'
                            }}>
                                <div style={{ fontWeight: '600', marginBottom: '12px', color: 'var(--purple)', fontSize: '12px' }}>
                                    FAILURE CLUSTERING ANALYSIS
                                </div>
                                
                                <div style={{ marginBottom: '12px', color: 'var(--text-muted)', fontSize: '10px', lineHeight: '1.5' }}>
                                    Metrics inspired by phylogenetic community structure (MNTD, NTI).
                                    Tests whether failures cluster together in the network.
                                </div>
                                
                                {/* Assortativity */}
                                <div style={{ 
                                    background: 'var(--bg-hover)', 
                                    borderRadius: '6px', 
                                    padding: '10px',
                                    marginBottom: '10px'
                                }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
                                        <span style={{ color: 'var(--text-muted)', fontWeight: '600' }}>Assortativity</span>
                                        <span style={{ 
                                            color: clusteringQuality.assortativity?.binary > 0.1 ? 'var(--green)' : 
                                                   clusteringQuality.assortativity?.binary < -0.1 ? 'var(--red)' : 'var(--text-secondary)',
                                            fontFamily: 'IBM Plex Mono, monospace'
                                        }}>
                                            r = {clusteringQuality.assortativity?.binary?.toFixed(3) || '—'}
                                        </span>
                                    </div>
                                    <div style={{ fontSize: '9px', color: 'var(--text-muted)' }}>
                                        {clusteringQuality.assortativity?.binary > 0.1 ? 
                                            '✓ Failures tend to connect to other failures' :
                                         clusteringQuality.assortativity?.binary < -0.1 ?
                                            '✗ Failures dispersed among successes' :
                                            '○ No strong clustering pattern'}
                                    </div>
                                    <div style={{ fontSize: '9px', color: 'var(--text-muted)', marginTop: '4px' }}>
                                        z-score: {clusteringQuality.assortativity?.z_score || '—'}
                                        {Math.abs(clusteringQuality.assortativity?.z_score || 0) > 2 ? ' (significant)' : ' (not significant)'}
                                    </div>
                                </div>
                                
                                {/* Neighborhood Purity */}
                                <div style={{ 
                                    background: 'var(--bg-hover)', 
                                    borderRadius: '6px', 
                                    padding: '10px',
                                    marginBottom: '10px'
                                }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
                                        <span style={{ color: 'var(--text-muted)', fontWeight: '600' }}>Neighborhood Purity</span>
                                        <span style={{ 
                                            color: 'var(--text-primary)',
                                            fontFamily: 'IBM Plex Mono, monospace'
                                        }}>
                                            {((clusteringQuality.neighborhood_purity?.binary || 0) * 100).toFixed(1)}%
                                        </span>
                                    </div>
                                    <div style={{ fontSize: '9px', color: 'var(--text-muted)' }}>
                                        Average fraction of same-class neighbors
                                    </div>
                                </div>
                                
                                {/* MNTD Ratio */}
                                <div style={{ 
                                    background: 'var(--bg-hover)', 
                                    borderRadius: '6px', 
                                    padding: '10px',
                                    marginBottom: '10px'
                                }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '6px' }}>
                                        <span style={{ color: 'var(--text-muted)', fontWeight: '600' }}>MNTD Ratio</span>
                                        <span style={{ 
                                            color: clusteringQuality.mntd_ratio < 0.9 ? 'var(--green)' : 
                                                   clusteringQuality.mntd_ratio > 1.1 ? 'var(--red)' : 'var(--text-secondary)',
                                            fontFamily: 'IBM Plex Mono, monospace'
                                        }}>
                                            {clusteringQuality.mntd_ratio?.toFixed(3) || '—'}
                                        </span>
                                    </div>
                                    <div style={{ fontSize: '9px', color: 'var(--text-muted)' }}>
                                        {clusteringQuality.mntd_ratio < 0.9 ? 
                                            '✓ Same-class jobs are closer than expected' :
                                         clusteringQuality.mntd_ratio > 1.1 ?
                                            '✗ Same-class jobs are farther than expected' :
                                            '○ Distance to same-class ≈ random'}
                                    </div>
                                </div>
                                
                                {/* SES.MNTD */}
                                <div style={{
                                    background: "var(--bg-hover)",
                                    borderRadius: "6px",
                                    padding: "10px",
                                    marginBottom: "10px"
                                }}>
                                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "6px" }}>
                                        <span style={{ color: "var(--text-muted)", fontWeight: "600" }}>SES.MNTD</span>
                                        <span style={{
                                            color: clusteringQuality.ses_mntd < -2 ? "var(--green)" :
                                                   clusteringQuality.ses_mntd > 2 ? "var(--red)" : "var(--text-secondary)",
                                            fontFamily: "IBM Plex Mono, monospace"
                                        }}>
                                            {clusteringQuality.ses_mntd?.toFixed(2) || "—"}
                                        </span>
                                    </div>
                                    <div style={{ fontSize: "9px", color: "var(--text-muted)" }}>
                                        {clusteringQuality.ses_mntd < -2 ?
                                            "✓ Significant clustering (p < 0.05)" :
                                         clusteringQuality.ses_mntd > 2 ?
                                            "✗ Significant overdispersion (p < 0.05)" :
                                            "○ Not significant"}
                                    </div>
                                </div>
                                
                                {/* Interpretation */}
                                {clusteringQuality.interpretation && (
                                    <div style={{ 
                                        borderTop: '1px solid var(--border)', 
                                        paddingTop: '12px',
                                        marginTop: '4px'
                                    }}>
                                        <div style={{ color: 'var(--text-muted)', fontWeight: '600', marginBottom: '8px', fontSize: '10px' }}>
                                            INTERPRETATION
                                        </div>
                                        {clusteringQuality.interpretation.map((line, i) => (
                                            <div key={i} style={{ 
                                                color: 'var(--text-secondary)', 
                                                fontSize: '10px',
                                                marginBottom: '4px',
                                                paddingLeft: '8px',
                                                borderLeft: '2px solid var(--border)'
                                            }}>
                                                {line}
                                            </div>
                                        ))}
                                    </div>
                                )}
                                
                                {/* Sample sizes */}
                                <div style={{ 
                                    marginTop: '12px', 
                                    fontSize: '9px', 
                                    color: 'var(--text-muted)',
                                    display: 'flex',
                                    gap: '16px'
                                }}>
                                    <span>n={clusteringQuality.sample_sizes?.n_jobs || '—'}</span>
                                    <span>edges={clusteringQuality.sample_sizes?.n_edges?.toLocaleString() || '—'}</span>
                                    <span style={{ color: 'var(--green)' }}>
                                        ✓{clusteringQuality.sample_sizes?.n_success || 0}
                                    </span>
                                    <span style={{ color: 'var(--red)' }}>
                                        ✗{clusteringQuality.sample_sizes?.n_failure || 0}
                                    </span>
                                </div>
                            </div>
                        )}
                        {showML && mlPredictions && (
                            <div style={{
                                position: "absolute",
                                top: "60px",
                                left: "16px",
                                background: "var(--bg-elevated)",
                                border: "1px solid var(--border)",
                                borderRadius: "8px",
                                padding: "16px",
                                fontSize: "11px",
                                maxWidth: "400px",
                                maxHeight: "500px",
                                overflowY: "auto"
                            }}>
                                <div style={{ fontWeight: "600", marginBottom: "12px", color: "#e74c3c", fontSize: "12px" }}>
                                    ML RISK ANALYSIS
                                </div>
                                <div style={{ marginBottom: "12px", color: "var(--text-muted)", fontSize: "10px" }}>
                                    Status: {mlPredictions.status} | Anomalies: {mlPredictions.n_anomalies || 0} / {mlPredictions.n_jobs || 0}
                                    <button
                                        onClick={() => {
                                            setMlTraining(true);
                                            fetch("/api/train_ml").then(r => r.json()).then(data => {
                                                setMlTraining(false);
                                                window.location.reload();
                                            });
                                        }}
                                        disabled={mlTraining}
                                        style={{
                                            marginLeft: "10px",
                                            padding: "4px 8px",
                                            fontSize: "9px",
                                            background: mlTraining ? "#666" : "#27ae60",
                                            color: "white",
                                            border: "none",
                                            borderRadius: "4px",
                                            cursor: mlTraining ? "wait" : "pointer"
                                        }}
                                    >
                                        {mlTraining ? "Training..." : "🔄 Update Models"}
                                    </button>
                                </div>
                                {mlPredictions.summary && (
                                    <div style={{ background: "var(--bg-hover)", borderRadius: "6px", padding: "10px", marginBottom: "10px" }}>
                                        <div style={{ fontWeight: "600", marginBottom: "6px", color: "var(--text-muted)" }}>Model Performance</div>
                                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px", fontSize: "10px" }}>
                                            <span>GNN Accuracy:</span><span style={{ fontFamily: "monospace" }}>{((mlPredictions.summary.gnn_accuracy || 0) * 100).toFixed(1)}%</span>
                                            <span>LSTM Accuracy:</span><span style={{ fontFamily: "monospace" }}>{((mlPredictions.summary.lstm_accuracy || 0) * 100).toFixed(1)}%</span>
                                            <span>AE Precision:</span><span style={{ fontFamily: "monospace" }}>{((mlPredictions.summary.ae_precision || 0) * 100).toFixed(1)}%</span>
                                            <span>AE Recall:</span><span style={{ fontFamily: "monospace" }}>{((mlPredictions.summary.ae_recall || 0) * 100).toFixed(1)}%</span>
                                        </div>
                                    </div>
                                )}
                                <div style={{ fontWeight: "600", marginBottom: "8px", color: "var(--text-muted)" }}>High-Risk Jobs (Top 10)</div>
                                {(mlPredictions.high_risk || []).slice(0, 10).map((job, i) => (
                                    <div key={i} style={{
                                        background: job.is_anomaly ? "rgba(231, 76, 60, 0.1)" : "var(--bg-hover)",
                                        borderRadius: "4px",
                                        padding: "8px",
                                        marginBottom: "6px",
                                        borderLeft: job.is_anomaly ? "3px solid #e74c3c" : "3px solid var(--border)"
                                    }}>
                                        <div style={{ display: "flex", justifyContent: "space-between" }}>
                                            <span style={{ fontFamily: "monospace" }}>Job {job.job_id}</span>
                                            <span style={{ color: "#e74c3c", fontFamily: "monospace" }}>
                                                {job.anomaly_score?.toFixed(2) || "—"}
                                            </span>
                                        </div>
                                        <div style={{ fontSize: "9px", color: "var(--text-muted)", marginTop: "2px" }}>
                                            {job.is_anomaly ? "🔴 Anomaly" : ""}
                                            {job.failure_reason > 0 ? ` | Failure: ${job.predicted_name || job.failure_reason}` : ""}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                        
                        <div className="network-stats">
                            <div className="network-stat">
                                <span>Jobs</span>
                                <span className="network-stat-value">{stats.total}</span>
                            </div>
                            <div className="network-stat">
                                <span>Edges</span>
                                <span className="network-stat-value">{stats.edges.toLocaleString()}</span>
                            </div>
                            <div className="network-stat">
                                <span>Completion</span>
                                <span className="network-stat-value" style={{ color: 'var(--green)' }}>{stats.successRate}%</span>
                            </div>
                        </div>
                        
                        <div className="network-legend">
                            <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#3fb950' }}></div>
                                <span>Completed ({stats.completed})</span>
                            </div>
                            {stats.timeout > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#d29922' }}></div>
                                <span>Timeout ({stats.timeout})</span>
                            </div>}
                            {stats.failed > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#f85149' }}></div>
                                <span>Failed ({stats.failed})</span>
                            </div>}
                            {stats.oom > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#a371f7' }}></div>
                                <span>OOM ({stats.oom})</span>
                            </div>}
                            {stats.segfault > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#da3633' }}></div>
                                <span>Segfault ({stats.segfault})</span>
                            </div>}
                            {stats.nodeFail > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#db6d28' }}></div>
                                <span>Node Fail ({stats.nodeFail})</span>
                            </div>}
                            {stats.cancelled > 0 && <div className="legend-item">
                                <div className="legend-dot" style={{ background: '#6e7681' }}></div>
                                <span>Cancelled ({stats.cancelled})</span>
                            </div>}
                        </div>
                    </div>
                </div>
            );
        }
        
        ReactDOM.render(<App />, document.getElementById('root'));
    </script>
</body>
</html>'''

EXIT_CODE_NAMES = {
    0: 'SUCCESS', 1: 'FAILED', 2: 'TIMEOUT', 3: 'OOM',
    4: 'SEGFAULT', 5: 'NODE_FAIL', 6: 'CANCELLED', 7: 'UNKNOWN'
}

EXIT_CODE_ICONS = {
    'success': '✓', 'failed': '❌', 'timeout': '⏱', 'oom': '💾',
    'segfault': '💥', 'node_fail': '🖥', 'cancelled': '🚫', 'unknown': '❓'
}


def get_failure_name(job):
    """Get failure reason name from job, handling both string and int."""
    reason = job.get('failure_reason') or job.get('exit_code', 0)
    if isinstance(reason, int):
        return EXIT_CODE_NAMES.get(reason, f'CODE_{reason}')
    return str(reason).upper() if reason else 'UNKNOWN'


def generate_failure_list_v2(failures):
    """Generate HTML for mobile failure list with proper exit code names."""
    if not failures:
        return ''
    html = ''
    for job in failures[:5]:
        reason = get_failure_name(job)
        icon = EXIT_CODE_ICONS.get(reason.lower(), '❓')
        job_id = job.get('job_id', 'N/A')
        runtime = job.get('runtime_sec', 0)
        runtime_str = f"{int(runtime//60)}m {int(runtime%60)}s" if runtime else 'N/A'
        html += f'<div class="alert-item"><div class="alert-icon red">{icon}</div><div class="alert-content"><div class="alert-title">Job {job_id}</div><div class="alert-subtitle">{reason} • {runtime_str}</div></div></div>'
    return html


def generate_risk_list_v2(predictions):
    """Generate HTML for high-risk job list."""
    if not predictions:
        return ''
    html = ''
    for pred in predictions[:5]:
        job_id = pred.get('job_id', 'N/A')
        risk_pct = int(pred.get('risk_score', 0) * 100)
        reason = pred.get('top_reason', 'unknown pattern')
        html += f'<div class="alert-item"><div class="alert-icon yellow">⚡</div><div class="alert-content"><div class="alert-title">Job {job_id}</div><div class="alert-subtitle">{reason}</div></div><div class="risk-score">{risk_pct}%</div></div>'
    return html


def generate_cluster_data(dm):
    """Generate per-cluster stats for mobile view."""
    cluster_data = {}
    for cluster_name in dm.clusters:
        # Get nodes for this cluster
        cluster_nodes = [n for n in dm.nodes.values() if n.get('cluster') == cluster_name]
        online = sum(1 for n in cluster_nodes if n.get('status') == 'online')
        total = len(cluster_nodes)

        # Get jobs for this cluster (by node)
        cluster_node_names = {n.get('name') for n in cluster_nodes}
        cluster_jobs = [j for j in dm.jobs if j.get('node') in cluster_node_names]
        job_success = sum(1 for j in cluster_jobs if j.get('success', True))
        job_total = len(cluster_jobs)
        job_rate = int(100 * job_success / job_total) if job_total > 0 else 100

        # Recent failures in cluster
        cluster_failures = [j for j in cluster_jobs if not j.get('success', True)][-3:]
        cluster_failures.reverse()

        # Status
        node_health = int(100 * online / total) if total > 0 else 0
        if node_health >= 95 and job_rate >= 80:
            status = 'healthy'
        elif node_health >= 80 and job_rate >= 60:
            status = 'warning'
        else:
            status = 'critical'

        cluster_data[cluster_name] = {
            'nodes_online': online,
            'nodes_total': total,
            'node_health': node_health,
            'jobs_success': job_success,
            'jobs_total': job_total,
            'job_rate': job_rate,
            'failures': cluster_failures,
            'status': status
        }
    return cluster_data

def generate_mobile_html(dm, stats):
    """Generate enhanced mobile dashboard HTML."""
    high_risk_jobs = []
    if hasattr(dm, '_predictions') and dm._predictions:
        high_risk_jobs = [p for p in dm._predictions if p.get('risk_score', 0) > 0.7][:5]
    recent_failures = [j for j in dm.jobs if not j.get('success', True)][-5:]
    recent_failures.reverse()
    total_nodes = stats.get('nodes_total', 0)
    online_nodes = stats.get('nodes_online', 0)
    node_health = int(100 * online_nodes / total_nodes) if total_nodes > 0 else 0
    total_jobs = stats.get('jobs', 0)
    success_jobs = stats.get('jobs_success', 0)
    job_success_rate = int(100 * success_jobs / total_jobs) if total_jobs > 0 else 0
    if node_health >= 95 and job_success_rate >= 80:
        overall_status, status_color = "healthy", "#22c55e"
    elif node_health >= 80 and job_success_rate >= 60:
        overall_status, status_color = "warning", "#f59e0b"
    else:
        overall_status, status_color = "critical", "#ef4444"
    node_color = 'green' if node_health >= 95 else 'yellow' if node_health >= 80 else 'red'
    job_color = 'green' if job_success_rate >= 80 else 'yellow' if job_success_rate >= 60 else 'red'
    cluster_data = generate_cluster_data(dm)
    cluster_chips_html = ''
    for name, data in cluster_data.items():
        status_icon = '✓' if data['status'] == 'healthy' else '⚠' if data['status'] == 'warning' else '✗'
        cluster_chips_html += f'<button class="chip chip-{data["status"]}" onclick="toggleCluster(\'{name}\')">{name} {status_icon}</button>'
    cluster_details_html = ''
    for name, data in cluster_data.items():
        failures_html = ''
        for job in data['failures'][:3]:
            reason = get_failure_name(job)
            job_id = job.get('job_id', 'N/A')
            failures_html += f'<div class="cluster-failure">Job {job_id} - {reason}</div>'
        if not failures_html:
            failures_html = '<div class="cluster-failure dim">No recent failures</div>'
        cluster_details_html += f'<div id="cluster-{name}" class="cluster-detail" style="display:none;"><div class="cluster-header">{name.upper()}</div><div class="cluster-stats"><div class="cluster-stat"><span class="stat-label">Nodes</span><span class="stat-value">{data["nodes_online"]}/{data["nodes_total"]}</span></div><div class="cluster-stat"><span class="stat-label">Success</span><span class="stat-value">{data["job_rate"]}%</span></div><div class="cluster-stat"><span class="stat-label">Jobs</span><span class="stat-value">{data["jobs_total"]}</span></div></div><div class="cluster-failures-title">Recent failures:</div>{failures_html}</div>'
    pattern_html = ''
    cq = dm.clustering_quality
    if cq and not cq.get('error'):
        r = cq.get('assortativity', {}).get('binary', 0)
        z = cq.get('assortativity', {}).get('z_score', 0)
        is_sig = abs(z) > 2
        if r > 0.1 and is_sig:
            pattern_html = f'<div class="card card-full" style="margin-bottom:20px;"><div class="card-label">📊 Pattern Analysis</div><div class="pattern-item"><span class="pattern-icon">🔗</span><span>Failures cluster together (r={r:.2f})</span></div>'
            hotspots = compute_failure_hotspots(dm.jobs) if dm.jobs else []
            if hotspots:
                top = hotspots[0]
                pattern_html += f'<div class="pattern-item"><span class="pattern-icon">🔥</span><span>Hotspot: {top["feature"]}={top["bin"]} ({int(top["failure_rate"])}% fail)</span></div>'
            pattern_html += '</div>'
    failure_html = generate_failure_list_v2(recent_failures) if recent_failures else '<div class="empty-state">No recent failures ✓</div>'
    risk_html = generate_risk_list_v2(high_risk_jobs) if high_risk_jobs else '<div class="empty-state">No high-risk jobs ✓</div>'
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>NØMAÐ</title>
    <style>
        :root {{ --bg:#0f172a; --bg-card:#1e293b; --bg-hover:#334155; --text:#f1f5f9; --text-muted:#94a3b8; --green:#22c55e; --yellow:#f59e0b; --red:#ef4444; --cyan:#06b6d4; --purple:#a855f7; }}
        * {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; padding:16px; padding-bottom:80px; }}
        .header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; padding-bottom:16px; border-bottom:1px solid var(--bg-hover); }}
        .logo {{ font-size:24px; font-weight:700; letter-spacing:-0.5px; }}
        .logo span {{ color:var(--cyan); }}
        .status-badge {{ display:flex; align-items:center; gap:6px; padding:6px 12px; border-radius:20px; font-size:12px; font-weight:600; background:{status_color}22; color:{status_color}; }}
        .status-dot {{ width:8px; height:8px; border-radius:50%; background:{status_color}; animation:pulse 2s infinite; }}
        @keyframes pulse {{ 0%,100%{{opacity:1;}} 50%{{opacity:0.5;}} }}
        .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:20px; }}
        .card {{ background:var(--bg-card); border-radius:12px; padding:16px; }}
        .card-full {{ grid-column:1/-1; }}
        .card-label {{ font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px; }}
        .card-value {{ font-size:32px; font-weight:700; line-height:1; }}
        .card-value.green {{ color:var(--green); }}
        .card-value.yellow {{ color:var(--yellow); }}
        .card-value.red {{ color:var(--red); }}
        .card-value.cyan {{ color:var(--cyan); }}
        .card-subtitle {{ font-size:12px; color:var(--text-muted); margin-top:4px; }}
        .progress-bar {{ height:6px; background:var(--bg-hover); border-radius:3px; margin-top:12px; overflow:hidden; }}
        .progress-fill {{ height:100%; border-radius:3px; }}
        .section-title {{ font-size:14px; font-weight:600; color:var(--text-muted); margin-bottom:12px; }}
        .alert-list {{ display:flex; flex-direction:column; gap:8px; }}
        .alert-item {{ display:flex; align-items:center; gap:12px; padding:12px; background:var(--bg-hover); border-radius:8px; font-size:13px; }}
        .alert-icon {{ width:32px; height:32px; border-radius:8px; display:flex; align-items:center; justify-content:center; font-size:16px; flex-shrink:0; }}
        .alert-icon.red {{ background:#ef444422; }}
        .alert-icon.yellow {{ background:#f59e0b22; }}
        .alert-content {{ flex:1; min-width:0; }}
        .alert-title {{ font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
        .alert-subtitle {{ font-size:11px; color:var(--text-muted); }}
        .risk-score {{ font-size:12px; font-weight:600; padding:4px 8px; border-radius:4px; background:#ef444422; color:var(--red); }}
        .refresh-btn {{ position:fixed; bottom:20px; right:20px; width:56px; height:56px; border-radius:50%; background:var(--cyan); color:var(--bg); border:none; font-size:24px; cursor:pointer; box-shadow:0 4px 12px rgba(6,182,212,0.4); }}
        .refresh-btn:active {{ transform:scale(0.95); }}
        .empty-state {{ text-align:center; padding:20px; color:var(--text-muted); font-size:13px; }}
        .cluster-chips {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:8px; }}
        .chip {{ padding:6px 12px; border-radius:12px; font-size:12px; background:var(--bg-hover); border:none; color:var(--text); cursor:pointer; transition:all 0.2s; }}
        .chip:active {{ transform:scale(0.95); }}
        .chip-healthy {{ border-left:3px solid var(--green); }}
        .chip-warning {{ border-left:3px solid var(--yellow); }}
        .chip-critical {{ border-left:3px solid var(--red); }}
        .chip.active {{ background:#06b6d422; color:var(--cyan); }}
        .cluster-detail {{ background:var(--bg-hover); border-radius:8px; padding:12px; margin-top:12px; animation:slideDown 0.2s ease; }}
        @keyframes slideDown {{ from {{ opacity:0; transform:translateY(-10px); }} to {{ opacity:1; transform:translateY(0); }} }}
        .cluster-header {{ font-size:12px; font-weight:600; color:var(--cyan); margin-bottom:8px; }}
        .cluster-stats {{ display:flex; gap:16px; margin-bottom:8px; }}
        .cluster-stat {{ display:flex; flex-direction:column; }}
        .stat-label {{ font-size:10px; color:var(--text-muted); }}
        .stat-value {{ font-size:14px; font-weight:600; }}
        .cluster-failures-title {{ font-size:10px; color:var(--text-muted); margin-top:8px; margin-bottom:4px; }}
        .cluster-failure {{ font-size:11px; color:var(--text-muted); padding:2px 0; }}
        .cluster-failure.dim {{ opacity:0.6; }}
        .data-source {{ text-align:center; font-size:11px; color:var(--text-muted); margin-top:20px; }}
        .desktop-link {{ display:block; text-align:center; margin-top:16px; color:var(--cyan); font-size:12px; text-decoration:none; }}
        .pattern-item {{ display:flex; align-items:center; gap:8px; font-size:12px; margin-top:8px; }}
        .pattern-icon {{ font-size:14px; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="logo">N<span>Ø</span>MADE</div>
        <div class="status-badge"><div class="status-dot"></div>{overall_status.upper()}</div>
    </div>
    <div class="grid">
        <div class="card">
            <div class="card-label">Nodes Online</div>
            <div class="card-value {node_color}">{online_nodes}</div>
            <div class="card-subtitle">of {total_nodes} total</div>
            <div class="progress-bar"><div class="progress-fill" style="width:{node_health}%;background:var(--{node_color});"></div></div>
        </div>
        <div class="card">
            <div class="card-label">Job Success</div>
            <div class="card-value {job_color}">{job_success_rate}%</div>
            <div class="card-subtitle">{success_jobs:,} of {total_jobs:,}</div>
            <div class="progress-bar"><div class="progress-fill" style="width:{job_success_rate}%;background:var(--{job_color});"></div></div>
        </div>
        <div class="card">
            <div class="card-label">Failed Jobs</div>
            <div class="card-value red">{stats.get('jobs_failed', 0)}</div>
            <div class="card-subtitle">requires attention</div>
        </div>
        <div class="card">
            <div class="card-label">Network Edges</div>
            <div class="card-value cyan">{stats.get('edges', 0):,}</div>
            <div class="card-subtitle">job connections</div>
        </div>
    </div>
    <div class="card card-full" style="margin-bottom:20px;">
        <div class="card-label">Clusters (tap to expand)</div>
        <div class="cluster-chips">{cluster_chips_html}</div>
        {cluster_details_html}
    </div>
    {pattern_html}
    <div class="section-title">🎯 High Risk Jobs</div>
    <div class="card card-full" style="margin-bottom:20px;">
        <div class="alert-list">{risk_html}</div>
    </div>
    <div class="section-title">⚠️ Recent Failures</div>
    <div class="card card-full">
        <div class="alert-list">{failure_html}</div>
    </div>
    <div class="data-source">Data: {stats.get('data_source', 'unknown')}</div>
    <a href="/" class="desktop-link">Open Full Dashboard →</a>
    <button class="refresh-btn" onclick="location.reload()">↻</button>
    <script>
    // Theme toggle
    function initTheme() {{
        const saved = localStorage.getItem('nomad-theme');
        if (saved === 'light') {{
            document.body.classList.add('light-theme');
        }}
    }}
    function toggleTheme() {{
        document.body.classList.toggle('light-theme');
        const isLight = document.body.classList.contains('light-theme');
        localStorage.setItem('nomad-theme', isLight ? 'light' : 'dark');
        // Update button icon
        const btn = document.querySelector('.theme-toggle');
        if (btn) {{
            btn.innerHTML = isLight 
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg> Light'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg> Dark';
        }}
    }}
    initTheme();

        let activeCluster = null;
        function toggleCluster(name) {{
            const detail = document.getElementById('cluster-' + name);
            const chips = document.querySelectorAll('.chip');
            document.querySelectorAll('.cluster-detail').forEach(d => d.style.display = 'none');
            chips.forEach(c => c.classList.remove('active'));
            if (activeCluster === name) {{
                activeCluster = null;
            }} else {{
                detail.style.display = 'block';
                event.target.classList.add('active');
                activeCluster = name;
            }}
        }}
        setTimeout(()=>location.reload(),60000);
    </script>
</body>
</html>'''

# ============================================================================
# HTTP Server
# ============================================================================



def _filter_umbrella_groups(conn, groups):
    """Exclude groups that contain >80% of all users."""
    try:
        total_users = conn.execute(
            "SELECT COUNT(DISTINCT username) FROM group_membership"
        ).fetchone()[0]
        if total_users > 0:
            umbrella = set()
            for r in conn.execute(
                "SELECT group_name, COUNT(DISTINCT username) as cnt"
                " FROM group_membership GROUP BY group_name"
            ).fetchall():
                if r["cnt"] / total_users > 0.8:
                    umbrella.add(r["group_name"])
            if umbrella:
                return [g for g in groups if g not in umbrella]
    except Exception:
        pass
    return groups


def query_resource_footprint(db_path, cluster='all', group='all', days=30):
    """Query resource footprint from job_accounting + group_membership."""
    import sqlite3 as _sql
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    start = (_dt.now() - _td(days=int(days))).strftime('%Y-%m-%dT00:00:00')
    conn = _sql.connect(str(db_path))
    conn.row_factory = _sql.Row
    c = conn.cursor()
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    empty = {
        'groups': [], 'users': [],
        'totals': {'cpu_hours': 0, 'gpu_hours': 0, 'jobs': 0, 'users': 0},
        'filters': {'clusters': [], 'groups': []},
    }
    if 'job_accounting' not in tables or (
            'job_accounting' in tables and
            c.execute('SELECT COUNT(*) FROM job_accounting'
                      ).fetchone()[0] < 5):
        # Fallback: compute from jobs table
        if 'jobs' in tables:
            try:
                where_j = ['end_time >= ?']
                params_j = [start]
                if cluster != 'all':
                    where_j.append(
                        "source_site = ?")
                    params_j.append(cluster)
                c.execute("""
                    SELECT user_name as username,
                        COALESCE(source_site, 'unknown')
                            as cluster,
                        SUM(CASE WHEN req_gpus > 0
                            THEN runtime_seconds/3600.0
                            ELSE 0 END) as gpu_hours,
                        SUM(CASE WHEN req_gpus = 0
                                      OR req_gpus IS NULL
                            THEN req_cpus *
                                 runtime_seconds/3600.0
                            ELSE 0 END) as cpu_hours,
                        COUNT(*) as jobs
                    FROM jobs
                    WHERE """ + ' AND '.join(where_j)
                    + ' GROUP BY username, cluster',
                    params_j)
                user_rows = c.fetchall()
                grp_map = {}
                if 'group_membership' in tables:
                    c.execute(
                        'SELECT username, group_name'
                        ' FROM group_membership')
                    for row in c.fetchall():
                        grp_map.setdefault(
                            row['username'], []
                        ).append(row['group_name'])
                users = []
                for row in user_rows:
                    u = row['username']
                    ugroups = grp_map.get(u, [])
                    if group != 'all' and \
                            group not in ugroups:
                        continue
                    users.append({
                        'username': u,
                        'cluster': row['cluster'],
                        'cpu_hours': round(
                            row['cpu_hours'] or 0, 1),
                        'gpu_hours': round(
                            row['gpu_hours'] or 0, 1),
                        'jobs': row['jobs'],
                        'groups': ugroups,
                    })
                gtotals = {}
                for u in users:
                    for g in u['groups']:
                        if g not in gtotals:
                            gtotals[g] = {
                                'name': g,
                                'cpu_hours': 0,
                                'gpu_hours': 0,
                                'jobs': 0,
                                'users': set()}
                        gtotals[g]['cpu_hours'] += \
                            u['cpu_hours']
                        gtotals[g]['gpu_hours'] += \
                            u['gpu_hours']
                        gtotals[g]['jobs'] += u['jobs']
                        gtotals[g]['users'].add(
                            u['username'])
                # Get umbrella groups to exclude
                _umb = set()
                try:
                    _total = conn.execute(
                        'SELECT COUNT(DISTINCT username)'
                        ' FROM group_membership'
                    ).fetchone()[0]
                    if _total > 0:
                        for _r in conn.execute(
                            'SELECT group_name,'
                            ' COUNT(DISTINCT username) as cnt'
                            ' FROM group_membership'
                            ' GROUP BY group_name'
                        ).fetchall():
                            if _r['cnt'] / _total > 0.8:
                                _umb.add(_r['group_name'])
                except Exception:
                    pass
                groups_list = []
                for g in sorted(
                        gtotals.values(),
                        key=lambda x: x['jobs'],
                        reverse=True):
                    g['users'] = len(g['users'])
                    if g['name'] not in _umb:
                        groups_list.append(g)
                all_clusters = []
                try:
                    all_clusters = sorted(set(
                        r[0] for r in c.execute(
                            'SELECT DISTINCT source_site FROM jobs WHERE source_site IS NOT NULL'
                        ).fetchall()))
                except Exception:
                    all_clusters = sorted(set(
                        u['cluster'] for u in users))
                all_groups = sorted(set(
                    g for u in users
                    for g in u['groups']))
                all_groups = _filter_umbrella_groups(
                    conn, all_groups)
                conn.close()
                return {
                    'groups': groups_list,
                    'users': sorted(
                        users,
                        key=lambda x: x['jobs'],
                        reverse=True),
                    'totals': {
                        'cpu_hours': round(sum(
                            u['cpu_hours']
                            for u in users), 1),
                        'gpu_hours': round(sum(
                            u['gpu_hours']
                            for u in users), 1),
                        'jobs': sum(
                            u['jobs'] for u in users),
                        'users': len(set(
                            u['username']
                            for u in users)),
                    },
                    'filters': {
                        'clusters': all_clusters,
                        'groups': all_groups,
                    },
                }
            except Exception:
                pass
        conn.close()
        return empty
    where = ["submit_time >= ?"]
    params = [start]
    if cluster != 'all':
        where.append("cluster = ?")
        params.append(cluster)
    c.execute("""
        SELECT username, cluster,
               SUM(cpu_hours) as cpu_hours,
               SUM(gpu_hours) as gpu_hours,
               COUNT(*) as jobs
        FROM job_accounting
        WHERE """ + " AND ".join(where) + """
        GROUP BY username, cluster
    """, params)
    user_rows = c.fetchall()
    grp_map = {}
    if 'group_membership' in tables:
        c.execute("SELECT username, group_name FROM group_membership")
        for row in c.fetchall():
            grp_map.setdefault(row['username'], []).append(row['group_name'])
    users = []
    user_set = set()
    for row in user_rows:
        u = row['username']
        user_set.add(u)
        ugroups = grp_map.get(u, [])
        if group != 'all' and group not in ugroups:
            continue
        users.append({
            'username': u, 'cluster': row['cluster'],
            'cpu_hours': round(row['cpu_hours'] or 0, 1),
            'gpu_hours': round(row['gpu_hours'] or 0, 1),
            'jobs': row['jobs'], 'groups': ugroups,
        })
    gtotals = {}
    for u in users:
        for g in u['groups']:
            if g not in gtotals:
                gtotals[g] = {'name': g, 'cpu_hours': 0,
                              'gpu_hours': 0, 'jobs': 0, 'users': set()}
            gtotals[g]['cpu_hours'] += u['cpu_hours']
            gtotals[g]['gpu_hours'] += u['gpu_hours']
            gtotals[g]['jobs'] += u['jobs']
            gtotals[g]['users'].add(u['username'])
    glist = sorted(gtotals.values(), key=lambda x: x['cpu_hours'], reverse=True)
    for g in glist:
        g['users'] = len(g['users'])
    try:
        c.execute("SELECT DISTINCT source_site FROM jobs WHERE source_site IS NOT NULL")
        avail_clusters = [r[0] for r in c.fetchall()]
    except Exception:
        c.execute("SELECT DISTINCT cluster FROM job_accounting")
        avail_clusters = [r[0] for r in c.fetchall()]
    avail_groups = _filter_umbrella_groups(conn, sorted(gtotals.keys()))
    umbrella_set = set(sorted(gtotals.keys())) - set(avail_groups)
    glist = [g for g in glist if g["name"] not in umbrella_set]
    conn.close()
    return {
        'groups': glist[:50],
        'users': sorted(users, key=lambda x: x['cpu_hours'], reverse=True)[:100],
        'totals': {
            'cpu_hours': round(sum(u['cpu_hours'] for u in users), 1),
            'gpu_hours': round(sum(u['gpu_hours'] for u in users), 1),
            'jobs': sum(u['jobs'] for u in users),
            'users': len(user_set),
        },
        'filters': {'clusters': avail_clusters, 'groups': avail_groups},
    }


def query_activity_heatmap(db_path, cluster='all', group='all', days=30):
    """Query activity heatmap from job_accounting submit times."""
    import sqlite3 as _sql
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    start = (_dt.now() - _td(days=int(days))).strftime('%Y-%m-%dT00:00:00')
    conn = _sql.connect(str(db_path))
    conn.row_factory = _sql.Row
    c = conn.cursor()
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    empty = {
        'grid': [[0]*24 for _ in range(7)], 'max_value': 0,
        'total_jobs': 0, 'busiest': None, 'quietest': None,
        'filters': {'clusters': [], 'groups': []},
    }
    has_accounting = (
        'job_accounting' in tables and
        c.execute('SELECT COUNT(*) FROM job_accounting'
                  ).fetchone()[0] >= 5)
    if not has_accounting:
        # Fallback: use jobs table for activity
        if 'jobs' in tables:
            try:
                where_j = [
                    'start_time >= ?',
                    'start_time IS NOT NULL']
                params_j = [start]
                if cluster != 'all':
                    where_j.append('source_site = ?')
                    params_j.append(cluster)
                c.execute(
                    'SELECT start_time, user_name'
                    ' FROM jobs WHERE '
                    + ' AND '.join(where_j),
                    params_j)
                grid = [[0]*24 for _ in range(7)]
                total = 0
                gu2 = None
                if group != 'all' and \
                        'group_membership' in tables:
                    c2 = conn.cursor()
                    c2.execute(
                        'SELECT username FROM'
                        ' group_membership WHERE'
                        ' group_name = ?', (group,))
                    gu2 = set(
                        r[0] for r in c2.fetchall())
                for row in c.fetchall():
                    if gu2 and row['user_name'] \
                            not in gu2:
                        continue
                    try:
                        dt = _dt.strptime(
                            row['start_time'][:19],
                            '%Y-%m-%dT%H:%M:%S')
                        grid[dt.weekday()][
                            dt.hour] += 1
                        total += 1
                    except (ValueError, TypeError):
                        continue
                max_val = max(
                    max(row) for row in grid) \
                    if total else 0
                busiest = quietest = None
                if total:
                    days = ['Monday','Tuesday',
                        'Wednesday','Thursday',
                        'Friday','Saturday',
                        'Sunday']
                    best = (0, 0, 0)
                    worst = (0, 0, 999999)
                    for d in range(7):
                        for h in range(24):
                            v = grid[d][h]
                            if v > best[2]:
                                best = (d, h, v)
                            if v < worst[2]:
                                worst = (d, h, v)
                    busiest = {
                        'day': days[best[0]],
                        'hour': f'{best[1]}:00',
                        'count': best[2]}
                    quietest = {
                        'day': days[worst[0]],
                        'hour': f'{worst[1]}:00',
                        'count': worst[2]}
                all_clusters = []
                try:
                    all_clusters = sorted(set(
                        r[0] for r in c.execute(
                            'SELECT DISTINCT'
                            ' source_site FROM jobs'
                            ' WHERE source_site'
                            ' IS NOT NULL'
                        ).fetchall()))
                except Exception:
                    pass
                all_groups = []
                try:
                    all_groups = _filter_umbrella_groups(
                        conn, sorted(set(
                            r[0] for r in c.execute(
                                'SELECT DISTINCT'
                                ' group_name FROM'
                                ' group_membership'
                            ).fetchall())))
                except Exception:
                    pass
                conn.close()
                return {
                    'grid': grid,
                    'max_value': max_val,
                    'total_jobs': total,
                    'busiest': busiest,
                    'quietest': quietest,
                    'filters': {
                        'clusters': all_clusters,
                        'groups': all_groups,
                    },
                }
            except Exception:
                pass
        conn.close()
        return empty
    group_users = None
    if group != 'all' and 'group_membership' in tables:
        c.execute(
            "SELECT username FROM group_membership WHERE group_name = ?",
            (group,))
        group_users = set(r[0] for r in c.fetchall())
    where = ["submit_time >= ?", "submit_time IS NOT NULL"]
    params = [start]
    if cluster != 'all':
        where.append("cluster = ?")
        params.append(cluster)
    c.execute("""
        SELECT submit_time, username
        FROM job_accounting WHERE """ + " AND ".join(where) + """
    """, params)
    grid = [[0]*24 for _ in range(7)]
    total = 0
    for row in c.fetchall():
        if group_users is not None and row['username'] not in group_users:
            continue
        try:
            dt = _dt.strptime(row['submit_time'][:19], '%Y-%m-%dT%H:%M:%S')
            grid[dt.weekday()][dt.hour] += 1
            total += 1
        except (ValueError, TypeError):
            continue
    max_val = 0
    busiest = {'day': 'Monday', 'hour': 0, 'count': 0}
    quietest = {'day': 'Monday', 'hour': 0, 'count': 999999}
    dnames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
              'Friday', 'Saturday', 'Sunday']
    for di in range(7):
        for hi in range(24):
            v = grid[di][hi]
            if v > max_val:
                max_val = v
                busiest = {'day': dnames[di], 'hour': hi, 'count': v}
            if v < quietest['count']:
                quietest = {'day': dnames[di], 'hour': hi, 'count': v}
    if quietest['count'] == 999999:
        quietest['count'] = 0
    c.execute("SELECT DISTINCT cluster FROM job_accounting")
    avail_clusters = [r[0] for r in c.fetchall()]
    avail_groups = []
    if 'group_membership' in tables:
        c.execute(
            "SELECT DISTINCT group_name FROM group_membership ORDER BY group_name")
        avail_groups = _filter_umbrella_groups(
            conn, [r[0] for r in c.fetchall()])
    conn.close()
    return {
        'grid': grid, 'max_value': max_val, 'total_jobs': total,
        'busiest': busiest, 'quietest': quietest,
        'filters': {'clusters': avail_clusters, 'groups': avail_groups},
    }


def _get_cloud_data(db_path) -> dict:
    """Query cloud_metrics table for dashboard display."""
    empty = {"instances": [], "latest": [], "timeseries": [], "cost": [], "summary": {}}
    if db_path is None:
        return empty

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cloud_metrics'")
    if not c.fetchone():
        conn.close()
        return empty

    c.execute("""
        SELECT DISTINCT node_name, instance_type, availability_zone, source
        FROM cloud_metrics WHERE metric_name != 'daily_cost_usd'
        ORDER BY node_name
    """)
    instances = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT node_name, metric_name, AVG(value) as avg_value,
               MAX(value) as max_value, MIN(value) as min_value, unit, COUNT(*) as samples
        FROM cloud_metrics
        WHERE metric_name != 'daily_cost_usd'
          AND timestamp >= datetime('now', '-1 hour')
        GROUP BY node_name, metric_name ORDER BY node_name, metric_name
    """)
    latest = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT node_name, metric_name,
            strftime('%Y-%m-%dT%H:', timestamp) ||
                CASE WHEN CAST(strftime('%M', timestamp) AS INTEGER) < 30
                     THEN '00' ELSE '30' END AS bucket,
            AVG(value) as avg_value, unit
        FROM cloud_metrics
        WHERE metric_name IN ('cpu_util', 'mem_util', 'gpu_util', 'gpu_mem_util')
          AND timestamp >= datetime('now', '-24 hours')
        GROUP BY node_name, metric_name, bucket ORDER BY bucket
    """)
    timeseries = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT node_name, SUM(value) as total_cost, AVG(value) as avg_daily_cost, COUNT(*) as days
        FROM cloud_metrics WHERE metric_name = 'daily_cost_usd'
        GROUP BY node_name ORDER BY total_cost DESC
    """)
    cost = [dict(r) for r in c.fetchall()]

    total_cost = sum(r["total_cost"] for r in cost)
    c.execute("""
        SELECT COUNT(DISTINCT node_name) as instance_count, COUNT(*) as total_metrics
        FROM cloud_metrics WHERE metric_name != 'daily_cost_usd'
    """)
    summary = dict(c.fetchone())
    summary["total_cost_7d"] = round(total_cost, 2)
    summary["providers"] = list(set(i["source"] for i in instances))

    conn.close()
    return {"instances": instances, "latest": latest, "timeseries": timeseries, "cost": cost, "summary": summary}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler for the dashboard."""

    data_manager: DataManager = None

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/' or parsed.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode())

        elif parsed.path == '/api/data':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            dm = DashboardHandler.data_manager
            data = {
                "clusters": dm.clusters,
                "nodes": dm.nodes,
                "jobs": dm.jobs,
                "edges": dm.edges,
                "data_source": dm.data_source,
                "nomad_version": importlib.metadata.version("nomad-hpc"),
                "feature_stats": dm.feature_stats,
                "correlation_data": dm.correlation_data,
                "suggested_axes": dm.suggested_axes,
                "network_stats": dm.network_stats,
                "clustering_quality": dm.clustering_quality,
                "network_method": dm.network_stats.get("method", "cosine") if dm.network_stats else "cosine",
                "ml_predictions": dm.ml_predictions or {"status": "not_ready"},
                "queue_running": dm.get_queue_running(),
            }
            # Detect which features have data
            features = {}
            if dm.db_path:
                try:
                    fconn = sqlite3.connect(str(dm.db_path))
                    for feat, query in [
                        ('cloud', 'SELECT 1 FROM cloud_instances LIMIT 1'),
                        ('interactive', 'SELECT 1 FROM interactive_sessions LIMIT 1'),
                        ('nfs', 'SELECT 1 FROM nfs_stats WHERE 1 LIMIT 1'),
                    ]:
                        try:
                            r = fconn.execute(query).fetchone()
                            features[feat] = r is not None
                        except Exception:
                            features[feat] = False
                    fconn.close()
                except Exception:
                    pass
            data['features'] = features
            self.wfile.write(json.dumps(data).encode())

        elif parsed.path == '/api/clustering':
            # Dedicated endpoint for clustering quality metrics
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(DashboardHandler.data_manager.clustering_quality).encode())

        elif parsed.path == "/api/predictions":
            # ML predictions endpoint
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            dm = DashboardHandler.data_manager
            predictions = dm.ml_predictions or {"status": "not_trained", "high_risk": []}
            self.wfile.write(json.dumps(predictions).encode())
        elif parsed.path == "/api/train_ml":
            # Train ML models endpoint
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                dm = DashboardHandler.data_manager
                if dm.db_path:
                    from nomad.ml import load_predictions_from_db, train_and_save_ensemble
                    result = train_and_save_ensemble(str(dm.db_path), epochs=50, verbose=False)
                    dm._ml_predictions = load_predictions_from_db(str(dm.db_path))
                    self.wfile.write(json.dumps({"status": "trained", "prediction_id": result.get("prediction_id")}).encode())
                else:
                    self.wfile.write(json.dumps({"status": "error", "message": "No database"}).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
        elif parsed.path == '/api/refresh':
            DashboardHandler.data_manager.refresh()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "refreshed"}).encode())

        elif parsed.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            stats = DashboardHandler.data_manager.get_stats()
            stats["queue_running"] = (
                DashboardHandler.data_manager
                .get_queue_running())
            self.wfile.write(json.dumps(stats).encode())
        elif parsed.path == '/mobile':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            dm = DashboardHandler.data_manager
            stats = dm.get_stats()
            mobile_html = generate_mobile_html(dm, stats)
            self.wfile.write(mobile_html.encode())
        elif parsed.path.startswith('/api/footprint'):
            query = parse_qs(parsed.query)
            fp_cluster = query.get('cluster', ['all'])[0]
            fp_group = query.get('group', ['all'])[0]
            fp_days = int(query.get('days', [30])[0])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            result = query_resource_footprint(
                dm.db_path, fp_cluster, fp_group, fp_days)
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path.startswith('/api/heatmap'):
            query = parse_qs(parsed.query)
            hm_cluster = query.get('cluster', ['all'])[0]
            hm_group = query.get('group', ['all'])[0]
            hm_days = int(query.get('days', [30])[0])
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            result = query_activity_heatmap(
                dm.db_path, hm_cluster, hm_group, hm_days)
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path == '/api/groups':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                import sqlite3 as _sql
                conn = _sql.connect(str(dm.db_path))
                conn.row_factory = _sql.Row
                c = conn.cursor()
                c.execute("""
                    SELECT group_name, cluster, COUNT(*) as members
                    FROM group_membership
                    GROUP BY group_name, cluster
                    ORDER BY group_name
                """)
                groups = [dict(r) for r in c.fetchall()]
                conn.close()
            except Exception:
                groups = []
            self.wfile.write(json.dumps({'groups': groups}).encode())
        elif parsed.path == '/api/interactive':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                import sqlite3 as _sql
                conn = _sql.connect(str(dm.db_path))
                conn.row_factory = _sql.Row
                c = conn.cursor()
                # Get servers
                c.execute("SELECT * FROM interactive_servers WHERE enabled = 1")
                servers = [dict(r) for r in c.fetchall()]
                # Get recent sessions
                c.execute("""SELECT * FROM interactive_sessions 
                    WHERE timestamp >= datetime(
                        (SELECT MAX(timestamp)
                         FROM interactive_sessions),
                        '-5 seconds')""")
                sessions = [dict(r) for r in c.fetchall()]
                # Get summary
                c.execute("""SELECT * FROM interactive_summary 
                    WHERE timestamp = (SELECT MAX(timestamp) FROM interactive_summary) LIMIT 1""")
                row = c.fetchone()
                summary = dict(row) if row else {}
                conn.close()
            except Exception:
                servers, sessions, summary = [], [], {}
            self.wfile.write(json.dumps({'servers': servers, 'sessions': sessions, 'summary': summary}).encode())

        elif parsed.path == '/api/workstations':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                import sqlite3 as _sql
                conn = _sql.connect(str(dm.db_path))
                conn.row_factory = _sql.Row
                c = conn.cursor()
                # Get latest workstation state for each hostname
                c.execute("""
                    SELECT w.* FROM workstation_state w
                    INNER JOIN (
                        SELECT hostname, MAX(timestamp) as max_ts
                        FROM workstation_state GROUP BY hostname
                    ) latest ON w.hostname = latest.hostname AND w.timestamp = latest.max_ts
                """)
                workstations = [dict(r) for r in c.fetchall()]
                conn.close()
                # Calculate summary
                summary = {
                    'total': len(workstations),
                    'online': sum(1 for w in workstations if w.get('status') == 'online'),
                    'degraded': sum(1 for w in workstations if w.get('status') == 'degraded'),
                    'offline': sum(1 for w in workstations if w.get('status') in ('offline', 'error')),
                }
            except Exception:
                workstations, summary = [], {}
            self.wfile.write(json.dumps({'workstations': workstations, 'summary': summary}).encode())

        elif parsed.path == '/api/workstation_users':
            # Per-user snapshot data for a single workstation.
            # Query string: ?hostname=<name>
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            from urllib.parse import parse_qs as _parse_qs
            qs = _parse_qs(parsed.query or '')
            hostname = (qs.get('hostname') or [''])[0]
            users = []
            if hostname:
                dm = DashboardHandler.data_manager
                try:
                    import sqlite3 as _sql
                    conn = _sql.connect(str(dm.db_path))
                    conn.row_factory = _sql.Row
                    c = conn.cursor()
                    # Most recent snapshot per (hostname, username)
                    c.execute("""
                        SELECT u.username, u.uid, u.session_epoch, u.timestamp,
                               u.cpu_usage_usec, u.cpu_user_usec, u.cpu_system_usec,
                               u.memory_current_bytes, u.memory_peak_bytes,
                               u.pids_current, u.source
                        FROM workstation_user_snapshot u
                        INNER JOIN (
                            SELECT hostname, username, MAX(timestamp) AS max_ts
                            FROM workstation_user_snapshot
                            WHERE hostname = ?
                            GROUP BY hostname, username
                        ) latest
                          ON u.hostname = latest.hostname
                         AND u.username = latest.username
                         AND u.timestamp = latest.max_ts
                        WHERE u.hostname = ?
                        ORDER BY u.memory_current_bytes DESC
                    """, (hostname, hostname))
                    users = [dict(r) for r in c.fetchall()]
                    conn.close()
                except Exception as e:
                    users = []
            self.wfile.write(json.dumps({
                'hostname': hostname,
                'users': users,
            }).encode())

        elif parsed.path == '/api/workstation_mounts':
            # Mount-state snapshot per (hostname, mountpoint).
            # Query string: ?hostname=<n>
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            from urllib.parse import parse_qs as _parse_qs_m
            qs = _parse_qs_m(parsed.query or '')
            hostname = (qs.get('hostname') or [''])[0]
            mounts = []
            if hostname:
                dm = DashboardHandler.data_manager
                try:
                    import sqlite3 as _sql
                    conn = _sql.connect(str(dm.db_path))
                    conn.row_factory = _sql.Row
                    c = conn.cursor()
                    c.execute("""
                        SELECT m.mountpoint, m.fstype, m.source,
                               m.is_mounted, m.is_responsive,
                               m.response_ms, m.timestamp, m.collected_at
                        FROM workstation_mount_state m
                        INNER JOIN (
                            SELECT hostname, mountpoint, MAX(timestamp) AS max_ts
                            FROM workstation_mount_state
                            WHERE hostname = ?
                            GROUP BY hostname, mountpoint
                        ) latest
                          ON m.hostname = latest.hostname
                         AND m.mountpoint = latest.mountpoint
                         AND m.timestamp = latest.max_ts
                        WHERE m.hostname = ?
                        ORDER BY m.mountpoint
                    """, (hostname, hostname))
                    mounts = [dict(r) for r in c.fetchall()]
                    conn.close()
                except Exception:
                    mounts = []
            self.wfile.write(json.dumps({
                'hostname': hostname,
                'mounts': mounts,
            }).encode())

        elif parsed.path == '/api/cloud':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                dm = DashboardHandler.data_manager
                db_path = getattr(dm, 'db_path', None)
                if db_path is None:
                    db_path = find_database()
                cloud_data = _get_cloud_data(db_path)
                self.wfile.write(json.dumps(cloud_data).encode())
            except Exception as e:
                logger.error(f"Cloud API error: {e}")
                self.wfile.write(json.dumps({"error": str(e), "instances": [], "latest": [], "timeseries": [], "cost": [], "summary": {}}).encode())

        elif parsed.path == '/api/storage':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                import sqlite3 as _sql
                conn = _sql.connect(str(dm.db_path))
                conn.row_factory = _sql.Row
                c = conn.cursor()
                # Get latest storage state for each hostname
                c.execute("""
                    SELECT s.* FROM storage_state s
                    INNER JOIN (
                        SELECT hostname, MAX(timestamp) as max_ts
                        FROM storage_state GROUP BY hostname
                    ) latest ON s.hostname = latest.hostname AND s.timestamp = latest.max_ts
                """)
                rows = [dict(r) for r in c.fetchall()]
                conn.close()
                # Parse JSON fields
                devices = []
                for row in rows:
                    dev = dict(row)
                    for field in ['pools_json', 'arc_stats_json', 'nfs_exports_json']:
                        if dev.get(field):
                            try:
                                import json as _json
                                dev[field.replace('_json', '')] = _json.loads(dev[field])
                            except:
                                dev[field.replace('_json', '')] = None
                            del dev[field]
                    devices.append(dev)
                # Calculate summary
                summary = {
                    'total': len(devices),
                    'total_bytes': sum(d.get('total_bytes', 0) or 0 for d in devices),
                    'used_bytes': sum(d.get('used_bytes', 0) or 0 for d in devices),
                    'nfs_clients': sum(d.get('nfs_clients_connected', 0) or 0 for d in devices),
                }
            except Exception:
                devices, summary = [], {}

            # Fallback: if no storage_state data, use filesystems table
            if not devices:
                try:
                    conn2 = _sql.connect(str(dm.db_path))
                    conn2.row_factory = _sql.Row
                    c2 = conn2.cursor()
                    # Get latest filesystem entry per path (per source_site)
                    try:
                        c2.execute("""
                            SELECT f.path, f.total_bytes, f.used_bytes,
                                   f.available_bytes, f.used_percent,
                                   f.timestamp, f.source_site,
                                   f.days_until_full
                            FROM filesystems f
                            INNER JOIN (
                                SELECT path,
                                       COALESCE(source_site, 'local')
                                           as ss,
                                       MAX(timestamp) as max_ts
                                FROM filesystems
                                GROUP BY path,
                                    COALESCE(source_site, 'local')
                            ) latest
                            ON f.path = latest.path
                               AND f.timestamp = latest.max_ts
                               AND COALESCE(f.source_site, 'local')
                                   = latest.ss
                        """)
                    except Exception:
                        # source_site column may not exist
                        c2.execute("""
                            SELECT f.path, f.total_bytes, f.used_bytes,
                                   f.available_bytes, f.used_percent,
                                   f.timestamp, NULL as source_site,
                                   f.days_until_full
                            FROM filesystems f
                            INNER JOIN (
                                SELECT path, MAX(timestamp) as max_ts
                                FROM filesystems GROUP BY path
                            ) latest
                            ON f.path = latest.path
                               AND f.timestamp = latest.max_ts
                        """)
                    fs_rows = c2.fetchall()
                    for row in fs_rows:
                        site = row['source_site'] or 'local'
                        devices.append({
                            'hostname':
                                f"{site}:{row['path']}",
                            'storage_type': 'disk',
                            'mount_point': row['path'],
                            'status': 'online',
                            'total_bytes': row['total_bytes'],
                            'used_bytes': row['used_bytes'],
                            'available_bytes':
                                row['available_bytes'],
                            'used_percent': row['used_percent'],
                            'usage_pct': row['used_percent'],
                            'timestamp': row['timestamp'],
                            'days_until_full':
                                row['days_until_full'],
                        })
                    summary = {
                        'total': len(devices),
                        'total_bytes': sum(
                            d.get('total_bytes', 0) or 0
                            for d in devices),
                        'used_bytes': sum(
                            d.get('used_bytes', 0) or 0
                            for d in devices),
                        'nfs_clients': 0,
                    }
                    conn2.close()
                except Exception:
                    pass

            self.wfile.write(json.dumps({'devices': devices, 'summary': summary}).encode())


        elif parsed.path == '/api/ref':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                from nomad.reference import KnowledgeBase
                kb = KnowledgeBase()
                params = dict(urllib.parse.parse_qsl(parsed.query))
                action = params.get('action', 'index')
                if action == 'topic':
                    key = params.get('key', '')
                    entry = kb.get(key)
                    if entry:
                        children = kb.get_children(key)
                        result = {
                            "key": entry.key, "title": entry.title,
                            "summary": entry.summary, "description": entry.description,
                            "examples": entry.examples, "math": entry.math,
                            "related": entry.related, "see_also": entry.see_also,
                            "tags": entry.tags, "category": entry.category,
                            "config_section": entry.config_section,
                            "config_keys": entry.config_keys,
                            "source_files": entry.source_files,
                            "children": [{"key": c.key, "title": c.title, "summary": c.summary} for c in children],
                        }
                    else:
                        result = {"error": f"Topic '{key}' not found"}
                elif action == 'search':
                    query = params.get('q', '')
                    results = kb.search(query, max_results=15)
                    result = {"query": query, "results": [
                        {"key": r.key, "title": r.title, "summary": r.summary, "category": r.category}
                        for r in results
                    ]}
                else:
                    topics = kb.list_topics()
                    cats = {}
                    for t in topics:
                        c = t.category or "other"
                        if c not in cats:
                            cats[c] = []
                        cats[c].append({"key": t.key, "title": t.title, "summary": t.summary})
                    result = {"categories": cats}
            except Exception as e:
                result = {"error": str(e)}
            self.wfile.write(json.dumps(result).encode())

        elif parsed.path == '/api/readiness':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            result = {"status": "no_database"}
            try:
                import sqlite3 as _sql
                import os as _os
                db_path = dm.db_path
                conn = _sql.connect(str(db_path), timeout=5)
                conn.row_factory = _sql.Row
                c = conn.cursor()

                # DB file size
                db_size_bytes = _os.path.getsize(str(db_path))
                db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

                # Table row counts
                tables = {}
                table_names = [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' "
                    "AND name NOT LIKE 'schema_%' ORDER BY name"
                ).fetchall()]
                for t in table_names:
                    try:
                        cnt = c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                        tables[t] = cnt
                    except Exception:
                        tables[t] = -1

                # Collector freshness — last timestamp per data table
                collectors = {}
                collector_tables = {
                    "disk": ("filesystems", "timestamp"),
                    "slurm": ("queue_state", "timestamp"),
                    "node_state": ("node_state", "timestamp"),
                    "iostat": ("iostat_cpu", "timestamp"),
                    "mpstat": ("mpstat_core", "timestamp"),
                    "vmstat": ("vmstat", "timestamp"),
                    "gpu": ("gpu_stats", "timestamp"),
                    "nfs": ("nfs_stats", "timestamp"),
                    "jobs": ("jobs", "submit_time"),
                    "groups": ("group_membership", "timestamp" if "timestamp" in
                        [r[1] for r in c.execute("PRAGMA table_info(group_membership)").fetchall()]
                        else None),
                }
                for name, (table, ts_col) in collector_tables.items():
                    if table not in table_names:
                        collectors[name] = {"status": "no_table", "rows": 0}
                        continue
                    rows = tables.get(table, 0)
                    if ts_col:
                        try:
                            last = c.execute(
                                f"SELECT MAX({ts_col}) FROM [{table}]"
                            ).fetchone()[0]
                            first = c.execute(
                                f"SELECT MIN({ts_col}) FROM [{table}]"
                            ).fetchone()[0]
                            collectors[name] = {
                                "status": "active" if rows > 0 else "empty",
                                "rows": rows,
                                "last_update": last,
                                "first_update": first,
                            }
                        except Exception:
                            collectors[name] = {"status": "error", "rows": rows}
                    else:
                        collectors[name] = {
                            "status": "active" if rows > 0 else "empty",
                            "rows": rows,
                        }

                # Collection cycles (from node_state distinct timestamps)
                try:
                    cycles = c.execute(
                        "SELECT COUNT(DISTINCT timestamp) FROM node_state"
                    ).fetchone()[0]
                except Exception:
                    cycles = 0

                # Uptime (first to last node_state)
                try:
                    r = c.execute(
                        "SELECT MIN(timestamp), MAX(timestamp) FROM node_state"
                    ).fetchone()
                    first_ts, last_ts = r[0], r[1]
                except Exception:
                    first_ts, last_ts = None, None

                # Config info
                config = dm.config or {}
                cluster_name = "unknown"
                try:
                    clusters_cfg = config.get("clusters", {})
                    if clusters_cfg:
                        cluster_name = list(clusters_cfg.values())[0].get("name", "unknown")
                except Exception:
                    pass

                result = {
                    "status": "ok",
                    "cluster_name": cluster_name,
                    "database": {
                        "path": str(db_path),
                        "size_mb": db_size_mb,
                        "size_bytes": db_size_bytes,
                    },
                    "collection": {
                        "cycles": cycles,
                        "first_timestamp": first_ts,
                        "last_timestamp": last_ts,
                    },
                    "collectors": collectors,
                    "tables": tables,
                    "data_source": dm.data_source,
                }

                conn.close()
            except Exception as e:
                result = {"status": "error", "error": str(e)}

            self.wfile.write(json.dumps(result).encode())

        elif parsed.path == '/api/insights':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                from nomad.insights import InsightEngine
                hours = int(dict(urllib.parse.parse_qsl(parsed.query)).get('hours', '168'))
                cluster_name = dict(urllib.parse.parse_qsl(parsed.query)).get('cluster', 'cluster')
                engine = InsightEngine(dm.db_path, hours=hours, cluster_name=cluster_name)
                result = engine.to_dict()
            except Exception as e:
                result = {"error": str(e), "signals": [], "insights": [], "overall_health": "unknown", "signal_count": 0, "insight_count": 0}
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path == '/api/dynamics':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                from nomad.dynamics.engine import DynamicsEngine
                from nomad.insights.signals import create_site_db
                hours = int(dict(urllib.parse.parse_qsl(parsed.query)).get('hours', '168'))
                cluster_name = dict(urllib.parse.parse_qsl(parsed.query)).get('cluster', 'all')
                use_db = dm.db_path
                tmp_path = None
                if cluster_name not in ('all', 'cluster'):
                    try:
                        tmp_path = create_site_db(
                            Path(str(dm.db_path)), cluster_name)
                        use_db = tmp_path
                    except Exception:
                        pass
                engine = DynamicsEngine(use_db, hours=hours, cluster_name=cluster_name)
                result = engine.to_dict()
                if tmp_path:
                    import os
                    os.unlink(tmp_path)
            except Exception as e:
                result = {"error": str(e)}
            self.wfile.write(json.dumps(result, default=str).encode())
        elif parsed.path == '/api/issue/info':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                from nomad.issue.collector import IssueCollector
                collector = IssueCollector(
                    db_path=str(dm.db_path) if dm.db_path else None,
                    source="dashboard",
                )
                info = collector.collect()
                result = info.to_dict()
            except Exception as e:
                result = {"error": str(e)}
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path == '/api/issue/url':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                from nomad.issue.collector import IssueCollector
                from nomad.issue.formatter import IssueFormatter
                from nomad.issue.github_api import GitHubClient
                params = dict(urllib.parse.parse_qsl(parsed.query))
                category = params.get('category', 'bug')
                title = params.get('title', '')
                component = params.get('component', 'other')
                description = params.get('description', '')
                collector = IssueCollector(
                    db_path=str(dm.db_path) if dm.db_path else None,
                    source="dashboard",
                )
                sys_info = collector.collect()
                formatter = IssueFormatter(system_info=sys_info)
                data = {
                    "category": category, "title": title,
                    "component": component, "description": description,
                    "steps": params.get("steps", ""),
                    "expected": params.get("expected", ""),
                    "actual": params.get("actual", ""),
                    "problem": params.get("problem", description),
                    "question": params.get("question", description),
                }
                fmt_title, body = formatter.format_from_dict(data)
                client = GitHubClient()
                url = client.generate_browser_url(fmt_title, body, category)
                result = {"url": url, "title": fmt_title}
            except Exception as e:
                result = {"error": str(e), "url": "https://github.com/jtonini/nomad-hpc/issues/new"}
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path == '/api/issue/search':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                from nomad.issue.github_api import GitHubClient
                params = dict(urllib.parse.parse_qsl(parsed.query))
                keywords = params.get('q', '')
                client = GitHubClient()
                duplicates = client.search_duplicates(keywords, max_results=5)
                result = [
                    {"number": d.number, "title": d.title, "url": d.url,
                     "state": d.state, "labels": d.labels,
                     "created_at": d.created_at, "comments": d.comments}
                    for d in duplicates
                ]
            except Exception as e:
                result = []
            self.wfile.write(json.dumps(result).encode())

        elif parsed.path == '/api/edu/users':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            try:
                conn = sqlite3.connect(str(dm.db_path))
                conn.row_factory = sqlite3.Row
                edu_cluster = dict(urllib.parse.parse_qsl(parsed.query)).get('cluster', 'all')
                if edu_cluster and edu_cluster != 'all':
                    users = [r[0] for r in conn.execute(
                        "SELECT DISTINCT user_name FROM jobs WHERE source_site=? ORDER BY user_name",
                        (edu_cluster,)).fetchall()]
                else:
                    users = [r[0] for r in conn.execute(
                        "SELECT DISTINCT user_name FROM jobs ORDER BY user_name"
                    ).fetchall()]
                groups = []
                try:
                    if edu_cluster and edu_cluster != "all":
                        groups = [r[0] for r in conn.execute(
                            "SELECT DISTINCT group_name FROM group_membership WHERE cluster=? ORDER BY group_name",
                            (edu_cluster,)).fetchall()]
                    else:
                        groups = [r[0] for r in conn.execute(
                            "SELECT DISTINCT group_name FROM group_membership ORDER BY group_name"
                        ).fetchall()]
                except Exception:
                    pass
                conn.close()
                result = {"users": users, "groups": groups}
            except Exception as e:
                result = {"users": [], "groups": [], "error": str(e)}
            self.wfile.write(json.dumps(result).encode())
        elif parsed.path.startswith('/api/edu/trajectory'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            params = dict(urllib.parse.parse_qsl(parsed.query))
            username = params.get('user', '')
            try:
                from nomad.edu import user_trajectory
                traj = user_trajectory(str(dm.db_path), username, days=90)
                if traj:
                    result = {
                        "username": traj.username,
                        "total_jobs": traj.total_jobs,
                        "date_range": list(traj.date_range),
                        "current_scores": traj.current_scores,
                        "improvement": traj.improvement,
                        "overall_improvement": traj.overall_improvement,
                        "windows": [
                            {"start": str(w.start), "end": str(w.end),
                             "jobs": w.job_count, "scores": w.scores}
                            for w in traj.windows
                        ] if hasattr(traj, 'windows') else [],
                    }
                else:
                    # Check why - not enough jobs?
                    try:
                        conn2 = sqlite3.connect(str(dm.db_path))
                        cnt = conn2.execute(
                            'SELECT COUNT(*) FROM jobs WHERE user_name=?',
                            (username,)).fetchone()[0]
                        conn2.close()
                        if cnt < 3:
                            result = {'error': f'{username} has {cnt} job(s). Minimum 3 required for trajectory analysis.'}
                        else:
                            result = {'error': f'No trajectory data for {username} ({cnt} jobs found but outside 90-day window)'}
                    except Exception:
                        result = {"error": f"No data for user {username}"}
            except Exception as e:
                result = {"error": str(e)}
            self.wfile.write(json.dumps(result, default=str).encode())
        elif parsed.path.startswith('/api/edu/group'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            dm = DashboardHandler.data_manager
            params = dict(urllib.parse.parse_qsl(parsed.query))
            group_name = params.get('group', '')
            try:
                from nomad.edu import group_summary
                gs = group_summary(str(dm.db_path), group_name, days=90)
                if gs:
                    result = {
                        "group_name": gs.group_name,
                        "member_count": gs.member_count,
                        "total_jobs": gs.total_jobs,
                        "date_range": list(gs.date_range),
                        "avg_overall": gs.avg_overall,
                        "avg_improvement": gs.avg_improvement,
                        "users_improving": gs.users_improving,
                        "users_declining": gs.users_declining,
                        "users_stable": gs.users_stable,
                        "dimension_avgs": gs.dimension_avgs,
                        "dimension_improvements": gs.dimension_improvements,
                        "weakest_dimension": gs.weakest_dimension,
                        "strongest_dimension": gs.strongest_dimension,
                        "users": [
                            {"username": u.username, "total_jobs": u.total_jobs,
                             "current_scores": u.current_scores,
                             "overall_improvement": u.overall_improvement}
                            for u in gs.users
                        ],
                    }
                else:
                    result = {"error": f"No data for group '{group_name}'"}
            except Exception as e:
                result = {"error": str(e)}
            self.wfile.write(json.dumps(result, default=str).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # Quiet logging
    def do_POST(self):
        """Handle POST requests for issue submission."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/issue/submit':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                import json as _json
                from nomad.issue.collector import IssueCollector
                from nomad.issue.formatter import IssueFormatter
                from nomad.issue.github_api import GitHubClient
                data = _json.loads(post_data.decode())
                dm = DashboardHandler.data_manager
                collector = IssueCollector(
                    db_path=str(dm.db_path) if dm.db_path else None,
                    config=getattr(dm, 'config', {}),
                    source="dashboard",
                )
                sys_info = collector.collect()
                formatter = IssueFormatter(system_info=sys_info)
                fmt_title, body = formatter.format_from_dict(data)
                # Check for token in config
                cfg = getattr(dm, 'config', {})
                from nomad.config import read_secret
                token = read_secret(cfg.get('issue_reporting', {}), 'github_token')
                client = GitHubClient(token=token)
                if client.has_token:
                    result = client.create_issue(
                        title=fmt_title, body=body,
                        category=data.get('category', ''),
                        component=data.get('component', ''),
                        version=sys_info.nomad_version,
                        institution=sys_info.institution,
                        source="dashboard",
                    )
                    resp = {"success": result.success, "url": result.url,
                            "number": result.number, "method": result.method,
                            "error": result.error}
                else:
                    url = client.generate_browser_url(fmt_title, body, data.get('category', ''))
                    resp = {"success": False, "url": url, "method": "browser",
                            "error": "No token — use browser link"}
                self.wfile.write(_json.dumps(resp).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
        else:
            self.send_error(404)



def serve_dashboard(host='localhost', port=8050, config_path=None, db_path=None):
    """Start the dashboard server."""

    # Load configuration
    config = load_config(config_path)

    # Initialize data manager
    data_manager = DataManager(config, db_path=db_path)
    DashboardHandler.data_manager = data_manager

    # Get stats
    stats = data_manager.get_stats()

    print("=" * 60)
    print("              NØMAÐ Dashboard")
    print("=" * 60)
    print(f"  Server:      http://{host}:{port}")
    print(f"  Version:     {importlib.metadata.version('nomad-hpc')}")
    print(f"  Data Source: {stats['data_source']}")
    print("-" * 60)
    print(f"  Clusters:    {stats['clusters']}")
    print(f"  Nodes:       {stats['nodes_online']}/{stats['nodes_total']} online")
    print(f"  Jobs:        {stats['jobs']} ({stats['jobs_success']} success, {stats['jobs_failed']} failed)")
    print(f"  Edges:       {stats['edges']}")
    print("-" * 60)
    # Clustering metrics
    cq = data_manager.clustering_quality
    if cq:
        r = cq.get("assortativity", {}).get("binary", 0)
        z = cq.get("assortativity", {}).get("z_score", 0)
        ses_mntd = cq.get("ses_mntd", 0)
        assort_sig = "sig" if abs(z) > 2 else "ns"
        mntd_sig = "sig" if abs(ses_mntd) > 2 else "ns"
        if r > 0.1:
            assort_msg = "failures cluster (resource pattern)"
        elif r < -0.1:
            assort_msg = "failures dispersed (code/user issue)"
        else:
            assort_msg = "random"
        print(f"  Assortativity:  r={r:>6.3f}  z={z:>5.1f} ({assort_sig:>3})  {assort_msg}")
        print(f"  SES.MNTD:       {ses_mntd:>7.2f}        ({mntd_sig:>3})  spatial clustering")
        hotspots = cq.get("hotspots", [])
        if hotspots:
            print("  Hotspots:")
            for h in hotspots[:3]:
                feat = f"{h['feature']}={h['bin']}"
                print(f"    {feat:<20} {h['failure_rate']:>5.0f}% fail  (base {h['baseline_rate']:.0f}%, {h['ratio']:.1f}x)")
    print("=" * 60)
    print("  Local access:")
    print(f"    Open: http://localhost:{port}")
    print()
    if host in ('localhost', '127.0.0.1', '0.0.0.0'):
        import socket
        hostname = socket.gethostname()
        print("  Remote access:")
        print(f"    ssh -L {port}:localhost:{port} {hostname}")
        print(f"    Then open: http://localhost:{port}")
        print("-" * 60)
    print("  Press Ctrl+C to stop")
    print()

    # Allow port reuse
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True
    with ReusableTCPServer((host, port), DashboardHandler) as httpd:
        httpd.serve_forever()


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8050
    serve_dashboard(port=port)
