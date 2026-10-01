# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Group Membership and Job Accounting Collector

Collects Linux group membership for user-to-group mapping
and lightweight job accounting data for resource footprint
and activity heatmap features.

Tables created:
    group_membership  - username, group_name, gid, cluster
    job_accounting    - per-job resource usage with user info

Configuration (nomad.toml):
    [collectors.groups]
    enabled = true
    min_gid = 1000                # Skip system groups below this GID
    group_filters = []            # Optional prefix filters, e.g. ["bio", "chem"]
    accounting_days = 30          # How far back to pull sacct data
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .base import BaseCollector

logger = logging.getLogger(__name__)


class GroupCollector(BaseCollector):
    """Collect Linux group membership and job accounting data.

    This collector serves two purposes:
    1. Maps users to their Linux groups (courses, labs, departments)
       by running 'getent group' on each cluster.
    2. Pulls job accounting data from sacct to enable resource
       footprint and activity heatmap visualizations.

    For remote clusters, commands are run via SSH.
    For workstation groups (no headnode), group data is collected
    from the first reachable node.
    """

    name = "groups"
    description = "Group membership and job accounting"
    default_interval = 3600  # Every hour

    def __init__(
        self,
        config: dict[str, Any],
        db_path: Path | str,
    ):
        super().__init__(config, db_path)
        self._clusters = config.get('clusters', {})
        # With no [clusters] configured (a workstation hub, an interactive
        # server), membership is read on this host under the site's name.
        self._local_name = config.get('local_name') or 'local'
        # Filter: skip system groups with GID below this
        self._min_gid = config.get('min_gid', 1000)
        # Optional: only collect groups matching these prefixes
        self._group_filters = config.get('group_filters', [])
        # How far back to pull job accounting
        self._accounting_days = config.get('accounting_days', 30)

    # ── SSH helper ───────────────────────────────────────────────────

    def _run_cmd(
        self,
        cmd: str,
        host: str = None,
        ssh_user: str = None,
        ssh_key: str = None,
    ) -> str | None:
        """Run a command locally or via SSH. Returns stdout or None."""
        if host:
            ssh_cmd = [
                "ssh", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=accept-new",
            ]
            if ssh_key:
                ssh_cmd += ["-i", ssh_key]
            ssh_cmd += ["-o", "BatchMode=yes",
                        f"{ssh_user}@{host}" if ssh_user else host, cmd]
            full_cmd = ssh_cmd
        else:
            full_cmd = cmd.split()

        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception as e:
            logger.debug(f"Command failed: {cmd}: {e}")
            return None

    # ── Group parsing ────────────────────────────────────────────────

    def _parse_groups(self, getent_output: str) -> list[dict]:
        """Parse 'getent group' output into membership records.

        Each line has format: group_name:x:gid:user1,user2,...
        Returns a list of {username, group_name, gid} dicts.
        """
        records = []
        for line in getent_output.split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(':')
            if len(parts) < 4:
                continue

            group_name = parts[0]
            try:
                gid = int(parts[2])
            except ValueError:
                continue

            # Skip system groups
            if gid < self._min_gid:
                continue

            # Apply prefix filters if configured
            if self._group_filters:
                if not any(
                    group_name.startswith(f)
                    for f in self._group_filters
                ):
                    continue

            members_str = parts[3].strip()
            if not members_str:
                continue

            members = [
                m.strip() for m in members_str.split(',')
                if m.strip()
            ]
            for user in members:
                records.append({
                    'username': user,
                    'group_name': group_name,
                    'gid': gid,
                })

        return records

    # ── Data collection ──────────────────────────────────────────────

    def _collect_groups(
        self,
        host: str = None,
        ssh_user: str = None,
        ssh_key: str = None,
        cluster_name: str = 'local',
    ) -> list[dict]:
        """Collect group membership from a single host."""
        output = self._run_cmd(
            "getent group", host, ssh_user, ssh_key)
        if not output:
            logger.warning(
                f"Could not get group data from {cluster_name}")
            return []

        records = self._parse_groups(output)
        for r in records:
            r['cluster'] = cluster_name

        logger.info(
            f"Collected {len(records)} group memberships"
            f" from {cluster_name}")
        return records

    def _collect_accounting(
        self,
        host: str = None,
        ssh_user: str = None,
        ssh_key: str = None,
        cluster_name: str = 'local',
    ) -> list[dict]:
        """Collect job accounting data from sacct.

        Pulls completed and failed jobs from the configured time
        window with resource usage details.
        """
        start_date = (
            datetime.now()
            - timedelta(days=self._accounting_days)
        ).strftime('%Y-%m-%dT00:00:00')

        cmd = (
            f"sacct -n -X -P --starttime={start_date} "
            f"--format=JobID,User,Account,Partition,State,"
            f"ElapsedRaw,AllocCPUS,MaxRSS,ReqMem,Submit,"
            f"Start,End,ReqTRES"
        )
        output = self._run_cmd(cmd, host, ssh_user, ssh_key)
        if not output:
            logger.warning(
                f"Could not get accounting data"
                f" from {cluster_name}")
            return []

        records = []
        for line in output.split('\n'):
            line = line.strip()
            if not line:
                continue
            fields = line.split('|')
            if len(fields) < 10:
                continue

            job_id = fields[0]
            user = fields[1]
            account = fields[2]
            partition = fields[3]
            state = fields[4]

            # Parse elapsed seconds
            try:
                elapsed_sec = int(fields[5])
            except (ValueError, TypeError):
                elapsed_sec = 0

            # Parse allocated CPUs
            try:
                alloc_cpus = int(fields[6])
            except (ValueError, TypeError):
                alloc_cpus = 0

            # Parse memory (MaxRSS: "1234K", "5678M", "2G")
            mem_gb = self._parse_memory(fields[7])

            # Parse submit time
            submit_time = fields[9] if fields[9] else None

            # Parse GPU count from ReqTRES (e.g. "gres/gpu=2", "gres/gpu:a100=2")
            gpu_count = 0
            if len(fields) > 12 and fields[12]:
                gpu_count = self._parse_gpu_gres(fields[12])

            # Compute resource-hours
            cpu_hours = (alloc_cpus * elapsed_sec) / 3600.0
            gpu_hours = (gpu_count * elapsed_sec) / 3600.0

            records.append({
                'job_id': job_id,
                'username': user,
                'account': account,
                'partition': partition,
                'state': state,
                'elapsed_sec': elapsed_sec,
                'alloc_cpus': alloc_cpus,
                'mem_gb': round(mem_gb, 3),
                'gpu_count': gpu_count,
                'cpu_hours': round(cpu_hours, 3),
                'gpu_hours': round(gpu_hours, 3),
                'submit_time': submit_time,
                'cluster': cluster_name,
            })

        logger.info(
            f"Collected {len(records)} job accounting records"
            f" from {cluster_name}")
        return records

    @staticmethod
    def _parse_memory(rss_str: str) -> float:
        """Parse SLURM memory string to GB."""
        if not rss_str:
            return 0.0
        rss_str = rss_str.strip()
        try:
            if rss_str.endswith('K'):
                return float(rss_str[:-1]) / (1024 * 1024)
            elif rss_str.endswith('M'):
                return float(rss_str[:-1]) / 1024
            elif rss_str.endswith('G'):
                return float(rss_str[:-1])
            elif rss_str.endswith('T'):
                return float(rss_str[:-1]) * 1024
            else:
                # Assume bytes
                return float(rss_str) / (1024 ** 3)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_gpu_gres(gres_str: str) -> int:
        """Parse SLURM GRES/TRES string for GPU count.

        Handles:
          ReqGRES:  gpu:2, gpu:a100:1
          ReqTRES:  cpu=4,mem=8G,gres/gpu=2
        """
        gpu_count = 0
        for part in gres_str.split(','):
            if 'gpu' not in part.lower():
                continue
            # ReqTRES: gres/gpu=2 or gres/gpu:a100=2
            if '=' in part:
                try:
                    gpu_count += int(part.split('=')[-1])
                except ValueError:
                    gpu_count += 1
            else:
                # ReqGRES: gpu:2 or gpu:a100:1
                pieces = part.split(':')
                try:
                    gpu_count += int(pieces[-1])
                except ValueError:
                    gpu_count += 1
        return gpu_count

    # ── Main collect/store interface ─────────────────────────────────

    def collect(self) -> list[dict[str, Any]]:
        """Collect group membership and job accounting
        from all configured clusters.

        For HPC clusters: collects from headnode (local or SSH).
        For workstation groups: collects from first reachable node.
        """
        all_groups = []
        all_accounting = []

        if not self._clusters:
            # Group membership doesn't need Slurm; job accounting does.
            all_groups = self._collect_groups(cluster_name=self._local_name)
            if shutil.which('sacct'):
                all_accounting = self._collect_accounting(cluster_name=self._local_name)
            if not all_groups:
                self.note = "getent group returned no groups at or above min_gid"
            return [{'groups': all_groups, 'accounting': all_accounting}]

        for cluster_id, cluster_conf in self._clusters.items():
            name = cluster_conf.get('name', cluster_id)
            host = cluster_conf.get('host')
            ssh_user = cluster_conf.get('ssh_user')
            ssh_key = cluster_conf.get('ssh_key')
            cluster_type = cluster_conf.get('type', 'hpc')

            if host:
                # Remote HPC cluster with headnode
                groups = self._collect_groups(
                    host, ssh_user, ssh_key, name)
                all_groups.extend(groups)

                if cluster_type == 'hpc':
                    acct = self._collect_accounting(
                        host, ssh_user, ssh_key, name)
                    all_accounting.extend(acct)

            elif cluster_type == 'workstations' and not ssh_user:
                # No SSH account configured for the workstations: they share
                # this hub's directory, so read membership here.
                all_groups.extend(self._collect_groups(cluster_name=name))

            elif cluster_type == 'workstations':
                # Workstation group: no headnode, try first node
                partitions = cluster_conf.get(
                    'groups', cluster_conf.get('partitions', {}))
                collected = False
                for dept, dept_data in partitions.items():
                    if collected:
                        break
                    nodes = dept_data.get('nodes', [])
                    for node in nodes:
                        groups = self._collect_groups(
                            node, ssh_user, ssh_key, name)
                        if groups:
                            all_groups.extend(groups)
                            collected = True
                            break
            else:
                # Local cluster (running on headnode)
                groups = self._collect_groups(
                    cluster_name=name)
                all_groups.extend(groups)

                acct = self._collect_accounting(
                    cluster_name=name)
                all_accounting.extend(acct)

        if not all_groups:
            self.note = "no group membership read from the configured clusters"
        return [{
            'groups': all_groups,
            'accounting': all_accounting,
        }]

    def count_records(self, data: list[dict[str, Any]]) -> int:
        """Memberships and accounting rows, not the one envelope."""
        payload = data[0] if data else {}
        return len(payload.get('groups', [])) + len(payload.get('accounting', []))

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store group membership and accounting data in SQLite."""
        if not data:
            return

        payload = data[0]
        groups = payload.get('groups', [])
        accounting = payload.get('accounting', [])

        conn = sqlite3.connect(self.db_path, timeout=30)
        c = conn.cursor()

        # ── Create tables ────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS group_membership (
                username TEXT NOT NULL,
                group_name TEXT NOT NULL,
                gid INTEGER,
                cluster TEXT NOT NULL,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (username, group_name, cluster)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_grp_group
            ON group_membership(group_name)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_grp_user
            ON group_membership(username)
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS job_accounting (
                job_id TEXT NOT NULL,
                cluster TEXT NOT NULL,
                username TEXT,
                account TEXT,
                partition TEXT,
                state TEXT,
                elapsed_sec INTEGER,
                alloc_cpus INTEGER,
                mem_gb REAL,
                gpu_count INTEGER DEFAULT 0,
                cpu_hours REAL DEFAULT 0,
                gpu_hours REAL DEFAULT 0,
                submit_time TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (job_id, cluster)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_jacct_user
            ON job_accounting(username)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_jacct_submit
            ON job_accounting(submit_time)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_jacct_cluster
            ON job_accounting(cluster)
        """)

        # ── Upsert group memberships ─────────────────────────────────
        now = datetime.now().isoformat()
        for g in groups:
            c.execute("""
                INSERT OR REPLACE INTO group_membership
                (username, group_name, gid, cluster, collected_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                g['username'], g['group_name'],
                g['gid'], g['cluster'], now,
            ))

        # ── Upsert job accounting ────────────────────────────────────
        for j in accounting:
            c.execute("""
                INSERT OR REPLACE INTO job_accounting
                (job_id, cluster, username, account, partition,
                 state, elapsed_sec, alloc_cpus, mem_gb,
                 gpu_count, cpu_hours, gpu_hours,
                 submit_time, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                j['job_id'], j['cluster'], j['username'],
                j['account'], j['partition'], j['state'],
                j['elapsed_sec'], j['alloc_cpus'], j['mem_gb'],
                j['gpu_count'], j['cpu_hours'], j['gpu_hours'],
                j['submit_time'], now,
            ))

        conn.commit()
        conn.close()

        logger.info(
            f"Stored {len(groups)} group memberships and "
            f"{len(accounting)} job accounting records")
