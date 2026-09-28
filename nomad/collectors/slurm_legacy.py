# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ SLURM Collector

Collects job and queue data from SLURM.
Uses squeue, sinfo, and sacct commands to gather:
- Current queue state (pending/running jobs per partition)
- Job details (resources, runtime, state)
- Node status
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class JobInfo:
    """Information about a SLURM job."""

    job_id: str
    user_name: str
    group_name: str | None
    partition: str
    job_name: str
    state: str
    node_list: str | None
    submit_time: datetime | None
    start_time: datetime | None
    end_time: datetime | None
    exit_code: int | None
    req_cpus: int
    req_mem_mb: int
    req_gpus: int
    req_time_seconds: int | None
    runtime_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'user_name': self.user_name,
            'group_name': self.group_name,
            'partition': self.partition,
            'job_name': self.job_name,
            'state': self.state,
            'node_list': self.node_list,
            'submit_time': self.submit_time.isoformat() if self.submit_time else None,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'exit_code': self.exit_code,
            'req_cpus': self.req_cpus,
            'req_mem_mb': self.req_mem_mb,
            'req_gpus': self.req_gpus,
            'req_time_seconds': self.req_time_seconds,
            'runtime_seconds': self.runtime_seconds,
        }


@dataclass
class QueueState:
    """State of a SLURM partition queue."""

    partition: str
    pending_jobs: int
    running_jobs: int
    total_jobs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'partition': self.partition,
            'pending_jobs': self.pending_jobs,
            'running_jobs': self.running_jobs,
            'total_jobs': self.total_jobs,
        }


@registry.register
class SlurmCollector(BaseCollector):
    """
    Collector for SLURM job and queue data.
    
    Configuration options:
        partitions: List of partitions to monitor (default: all)
        job_history_days: Days of job history to collect (default: 7)
        collect_queue: Whether to collect queue state (default: True)
        collect_jobs: Whether to collect job details (default: True)
    
    Collected data:
        - Queue state per partition (pending/running counts)
        - Job metadata (user, resources, state, times)
    """

    name = "slurm"
    description = "SLURM job and queue monitoring"
    default_interval = 30

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self.partitions = config.get('partitions', None)  # None = all
        self.job_history_days = config.get('job_history_days', 7)
        self.collect_queue = config.get('collect_queue', True)
        self.collect_jobs = config.get('collect_jobs', True)

        logger.info(f"SlurmCollector monitoring partitions: {self.partitions or 'all'}")

    def collect(self) -> list[dict[str, Any]]:
        """Collect SLURM queue and job data."""
        data = []

        # Collect queue state
        if self.collect_queue:
            try:
                queue_states = self._collect_queue_state()
                for qs in queue_states:
                    data.append({
                        'type': 'queue_state',
                        **qs.to_dict()
                    })
            except Exception as e:
                logger.warning(f"Failed to collect queue state: {e}")

        # Collect running/recent jobs
        if self.collect_jobs:
            try:
                jobs = self._collect_jobs()
                for job in jobs:
                    data.append({
                        'type': 'job',
                        **job.to_dict()
                    })
            except Exception as e:
                logger.warning(f"Failed to collect jobs: {e}")

        if not data:
            raise CollectionError("No SLURM data collected")

        return data

    def _collect_queue_state(self) -> list[QueueState]:
        """Collect current queue state from squeue."""
        try:
            # Get all jobs grouped by partition and state
            result = subprocess.run(
                ['squeue', '-h', '-o', '%P|%t'],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise CollectionError(f"squeue failed: {result.stderr}")

            # Count jobs per partition
            partition_counts: dict[str, dict[str, int]] = {}

            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue

                parts = line.split('|')
                if len(parts) >= 2:
                    partition = parts[0].strip().rstrip('*')
                    state = parts[1].strip()

                    if partition not in partition_counts:
                        partition_counts[partition] = {'pending': 0, 'running': 0}

                    if state == 'PD':
                        partition_counts[partition]['pending'] += 1
                    elif state == 'R':
                        partition_counts[partition]['running'] += 1

            # Filter by configured partitions
            queue_states = []
            for partition, counts in partition_counts.items():
                if self.partitions is None or partition in self.partitions:
                    queue_states.append(QueueState(
                        partition=partition,
                        pending_jobs=counts['pending'],
                        running_jobs=counts['running'],
                        total_jobs=counts['pending'] + counts['running'],
                    ))

            # Add empty partitions if monitoring specific ones
            if self.partitions:
                for p in self.partitions:
                    if p not in partition_counts:
                        queue_states.append(QueueState(
                            partition=p,
                            pending_jobs=0,
                            running_jobs=0,
                            total_jobs=0,
                        ))

            return queue_states

        except subprocess.TimeoutExpired:
            raise CollectionError("squeue command timed out")

    def _collect_jobs(self) -> list[JobInfo]:
        """Collect job information from squeue."""
        try:
            # Format: JobID|User|Group|Partition|Name|State|NodeList|NumCPUs|MinMemory|Gres|TimeLimit|RunTime|SubmitTime|StartTime
            format_str = "%i|%u|%g|%P|%j|%T|%N|%C|%m|%b|%l|%M|%V|%S"

            result = subprocess.run(
                ['squeue', '-h', '-o', format_str],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise CollectionError(f"squeue failed: {result.stderr}")

            jobs = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue

                job = self._parse_job_line(line)
                if job:
                    # Filter by partition if configured
                    if self.partitions is None or job.partition in self.partitions:
                        jobs.append(job)

            return jobs

        except subprocess.TimeoutExpired:
            raise CollectionError("squeue command timed out")

    def _parse_job_line(self, line: str) -> JobInfo | None:
        """Parse a squeue output line into JobInfo."""
        try:
            parts = line.split('|')
            if len(parts) < 14:
                return None

            job_id = parts[0].strip()
            user_name = parts[1].strip()
            group_name = parts[2].strip() or None
            partition = parts[3].strip().rstrip('*')
            job_name = parts[4].strip()
            state = parts[5].strip()
            node_list = parts[6].strip() or None
            req_cpus = self._parse_int(parts[7])
            req_mem_mb = self._parse_memory(parts[8])
            req_gpus = self._parse_gpus(parts[9])
            time_limit = self._parse_time(parts[10])
            runtime = self._parse_time(parts[11])
            submit_time = self._parse_datetime(parts[12])
            start_time = self._parse_datetime(parts[13])

            return JobInfo(
                job_id=job_id,
                user_name=user_name,
                group_name=group_name,
                partition=partition,
                job_name=job_name,
                state=state,
                node_list=node_list,
                submit_time=submit_time,
                start_time=start_time,
                end_time=None,  # Not available from squeue
                exit_code=None,
                req_cpus=req_cpus,
                req_mem_mb=req_mem_mb,
                req_gpus=req_gpus,
                req_time_seconds=time_limit,
                runtime_seconds=runtime,
            )

        except Exception as e:
            logger.debug(f"Failed to parse job line: {line} - {e}")
            return None

    def _parse_int(self, value: str) -> int:
        """Parse integer, defaulting to 0."""
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return 0

    def _parse_memory(self, value: str) -> int:
        """Parse memory string (e.g., '4G', '4096M') to MB."""
        try:
            value = value.strip().upper()
            if not value or value == 'N/A':
                return 0

            if value.endswith('G'):
                return int(float(value[:-1]) * 1024)
            elif value.endswith('M'):
                return int(float(value[:-1]))
            elif value.endswith('K'):
                return int(float(value[:-1]) / 1024)
            else:
                return int(value)
        except (ValueError, AttributeError):
            return 0

    def _parse_gpus(self, value: str) -> int:
        """Parse GPU request string (e.g., 'gpu:2')."""
        try:
            value = value.strip()
            if not value or value == 'N/A':
                return 0

            # Format: gpu:N or gpu:type:N
            if 'gpu' in value.lower():
                parts = value.split(':')
                for p in reversed(parts):
                    try:
                        return int(p)
                    except ValueError:
                        continue
            return 0
        except (ValueError, AttributeError):
            return 0

    def _parse_time(self, value: str) -> int | None:
        """Parse SLURM time format (D-HH:MM:SS or HH:MM:SS) to seconds."""
        try:
            value = value.strip()
            if not value or value in ('N/A', 'UNLIMITED', 'INVALID'):
                return None

            days = 0
            if '-' in value:
                day_part, time_part = value.split('-', 1)
                days = int(day_part)
            else:
                time_part = value

            parts = time_part.split(':')
            if len(parts) == 3:
                hours, minutes, seconds = map(int, parts)
            elif len(parts) == 2:
                hours = 0
                minutes, seconds = map(int, parts)
            elif len(parts) == 1:
                hours = 0
                minutes = 0
                seconds = int(parts[0])
            else:
                return None

            return days * 86400 + hours * 3600 + minutes * 60 + seconds

        except (ValueError, AttributeError):
            return None

    def _parse_datetime(self, value: str) -> datetime | None:
        """Parse SLURM datetime format."""
        try:
            value = value.strip()
            if not value or value in ('N/A', 'Unknown'):
                return None

            # Try common SLURM formats
            for fmt in [
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M',
            ]:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue

            return None

        except (ValueError, AttributeError):
            return None

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store collected data in the database."""
        timestamp = datetime.now().isoformat()

        with self.get_db_connection() as conn:
            for record in data:
                record_type = record.get('type')

                if record_type == 'queue_state':
                    conn.execute(
                        """
                        INSERT INTO queue_state
                        (partition, pending_jobs, running_jobs, total_jobs, timestamp)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            record['partition'],
                            record['pending_jobs'],
                            record['running_jobs'],
                            record['total_jobs'],
                            timestamp,
                        )
                    )

                elif record_type == 'job':
                    # Upsert job data
                    conn.execute(
                        """
                        INSERT INTO jobs
                        (job_id, user_name, group_name, partition, job_name, state,
                         node_list, submit_time, start_time, end_time, exit_code,
                         req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            state = excluded.state,
                            node_list = excluded.node_list,
                            start_time = excluded.start_time,
                            end_time = excluded.end_time,
                            exit_code = excluded.exit_code,
                            runtime_seconds = excluded.runtime_seconds
                        """,
                        (
                            record['job_id'],
                            record['user_name'],
                            record['group_name'],
                            record['partition'],
                            record['job_name'],
                            record['state'],
                            record['node_list'],
                            record['submit_time'],
                            record['start_time'],
                            record['end_time'],
                            record['exit_code'],
                            record['req_cpus'],
                            record['req_mem_mb'],
                            record['req_gpus'],
                            record['req_time_seconds'],
                            record['runtime_seconds'],
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} SLURM records")

    def get_queue_history(
        self,
        partition: str,
        hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Get queue state history for derivative analysis."""
        with self.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM queue_state
                WHERE partition = ?
                  AND timestamp > datetime('now', ?)
                ORDER BY timestamp ASC
                """,
                (partition, f'-{hours} hours')
            ).fetchall()

            return [dict(row) for row in rows]

    def get_recent_jobs(
        self,
        partition: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get recent jobs with optional filtering."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params = []

        if partition:
            query += " AND partition = ?"
            params.append(partition)

        if state:
            query += " AND state = ?"
            params.append(state)

        query += " ORDER BY submit_time DESC LIMIT ?"
        params.append(limit)

        with self.get_db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
