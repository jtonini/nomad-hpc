# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NOMADE SLURM Collector

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
    exit_signal: int | None  # Signal number (e.g., 9=SIGKILL, 11=SIGSEGV)
    failure_reason: int  # Categorical: 0=success, 1=timeout, etc.
    req_cpus: int
    req_mem_mb: int
    req_gpus: int
    req_time_seconds: int | None
    runtime_seconds: int | None
    wait_time_seconds: int | None = None

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
            'exit_signal': self.exit_signal,
            'failure_reason': self.failure_reason,
            'req_cpus': self.req_cpus,
            'req_mem_mb': self.req_mem_mb,
            'req_gpus': self.req_gpus,
            'req_time_seconds': self.req_time_seconds,
            'runtime_seconds': self.runtime_seconds,
            'wait_time_seconds': self.wait_time_seconds,
        }


# Failure reason categories (factor variable)
FAILURE_SUCCESS = 0        # Job completed successfully
FAILURE_TIMEOUT = 1        # Time limit exceeded
FAILURE_CANCELLED = 2      # User/admin cancelled
FAILURE_FAILED = 3         # Generic failure (exit_code != 0)
FAILURE_OOM = 4            # Out of memory (SIGKILL from cgroup, exit 137)
FAILURE_SEGFAULT = 5       # Segmentation fault (SIGSEGV, exit 139)
FAILURE_NODE_FAIL = 6      # Node failure
FAILURE_DEPENDENCY = 7     # Dependency not satisfied

# Signal numbers for reference
SIGKILL = 9    # Kill signal (often OOM)
SIGSEGV = 11   # Segmentation fault
SIGTERM = 15   # Termination request
SIGABRT = 6    # Abort


def compute_failure_reason(state: str, exit_code: int | None, exit_signal: int | None) -> int:
    """
    Compute failure reason category from job state and exit codes.
    
    Args:
        state: SLURM job state (COMPLETED, FAILED, TIMEOUT, etc.)
        exit_code: Exit status (0-255)
        exit_signal: Signal number if killed
    
    Returns:
        Integer category (0-7) for failure_reason
    """
    state = state.upper() if state else ""

    # Success case
    if state == 'COMPLETED' and (exit_code is None or exit_code == 0):
        return FAILURE_SUCCESS

    # Timeout
    if state == 'TIMEOUT':
        return FAILURE_TIMEOUT

    # Cancelled
    if state in ('CANCELLED', 'PREEMPTED'):
        return FAILURE_CANCELLED

    # Node failure
    if state == 'NODE_FAIL':
        return FAILURE_NODE_FAIL

    # Dependency failure
    if state == 'DEADLINE' or 'DEPEND' in state:
        return FAILURE_DEPENDENCY

    # OOM - either explicit state or SIGKILL (9)
    if state == 'OUT_OF_MEMORY':
        return FAILURE_OOM
    if exit_signal == SIGKILL:
        return FAILURE_OOM
    if exit_code == 137:  # 128 + 9 (SIGKILL)
        return FAILURE_OOM

    # Segfault - SIGSEGV (11)
    if exit_signal == SIGSEGV:
        return FAILURE_SEGFAULT
    if exit_code == 139:  # 128 + 11 (SIGSEGV)
        return FAILURE_SEGFAULT

    # Abort - SIGABRT (6)
    if exit_signal == SIGABRT or exit_code == 134:  # 128 + 6
        return FAILURE_SEGFAULT  # Group with segfault as "code bug"

    # Generic failure
    if state == 'FAILED' or (exit_code is not None and exit_code != 0):
        return FAILURE_FAILED

    # If completed but with non-zero exit, still a failure
    if state == 'COMPLETED' and exit_code is not None and exit_code != 0:
        return FAILURE_FAILED

    # Default to success if we can't determine
    return FAILURE_SUCCESS


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
        collect_completed: Whether to collect completed job history (default: True)
    
    Collected data:
        - Queue state per partition (pending/running counts)
        - Job metadata (user, resources, state, times)
        - Completed jobs with exit codes and failure classification
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
        self.collect_completed = config.get('collect_completed', True)

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

        # Collect running/pending jobs from squeue
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

        # Collect completed jobs from sacct (with exit codes)
        if self.collect_completed:
            try:
                completed_jobs = self._collect_completed_jobs()
                for job in completed_jobs:
                    data.append({
                        'type': 'job',
                        **job.to_dict()
                    })
            except Exception as e:
                logger.warning(f"Failed to collect completed jobs: {e}")

        if not data:
            raise CollectionError("No SLURM data collected")

        return data

    def _collect_completed_jobs(self) -> list[JobInfo]:
        """Collect completed job information from sacct."""
        try:
            # sacct format includes ExitCode which gives us exit_status:signal
            # Format: JobID|User|Group|Partition|JobName|State|NodeList|AllocCPUS|ReqMem|ReqTRES|Timelimit|Elapsed|Submit|Start|End|ExitCode
            format_str = "JobID,User,Group,Partition,JobName,State,NodeList,AllocCPUS,ReqMem,ReqTRES,Timelimit,Elapsed,Submit,Start,End,ExitCode"

            result = subprocess.run(
                [
                    'sacct',
                    '-n',  # No header
                    '-P',  # Parseable (pipe-delimited)
                    '-X',  # No job steps, only main job
                    f'--starttime=now-{self.job_history_days}days',
                    f'--format={format_str}',
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                raise CollectionError(f"sacct failed: {result.stderr}")

            jobs = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue

                job = self._parse_sacct_job(line)
                if job:
                    # Filter by partition if configured
                    if self.partitions is None or job.partition in self.partitions:
                        jobs.append(job)

            logger.debug(f"Collected {len(jobs)} completed jobs from sacct")
            return jobs

        except subprocess.TimeoutExpired:
            raise CollectionError("sacct command timed out")
        except FileNotFoundError:
            logger.warning("sacct command not found - skipping completed job collection")
            return []

    def _parse_sacct_job(self, line: str) -> JobInfo | None:
        """Parse a single sacct output line into JobInfo."""
        try:
            parts = line.split('|')
            if len(parts) < 16:
                return None

            job_id = parts[0].strip()
            # Skip job steps (contain '.')
            if '.' in job_id:
                return None

            user_name = parts[1].strip()
            group_name = parts[2].strip() or None
            partition = parts[3].strip()
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
            end_time = self._parse_datetime(parts[14])

            # Parse ExitCode (format: "exit_status:signal")
            exit_code, exit_signal = self._parse_exit_code(parts[15])

            # Compute failure reason
            failure_reason = compute_failure_reason(state, exit_code, exit_signal)

            # Compute wait time
            start_time = self._sane_start(start_time, submit_time)
            wait_time = None
            if submit_time and start_time:
                wait_time = int((start_time - submit_time).total_seconds())

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
                end_time=end_time,
                exit_code=exit_code,
                exit_signal=exit_signal,
                failure_reason=failure_reason,
                req_cpus=req_cpus,
                req_mem_mb=req_mem_mb,
                req_gpus=req_gpus,
                req_time_seconds=time_limit,
                runtime_seconds=runtime,
                wait_time_seconds=wait_time,
            )

        except Exception as e:
            logger.debug(f"Failed to parse sacct line: {line} - {e}")
            return None

    def _parse_exit_code(self, value: str) -> tuple[int | None, int | None]:
        """
        Parse SLURM ExitCode format: "exit_status:signal"
        
        Examples:
            "0:0" -> (0, 0) - clean exit
            "1:0" -> (1, 0) - exit code 1
            "0:9" -> (0, 9) - killed by SIGKILL
            "0:15" -> (0, 15) - killed by SIGTERM
        
        Returns:
            Tuple of (exit_code, signal)
        """
        try:
            value = value.strip()
            if not value or value == 'N/A':
                return None, None

            parts = value.split(':')
            if len(parts) == 2:
                exit_code = int(parts[0]) if parts[0] else None
                signal = int(parts[1]) if parts[1] else None
                # If signal is non-zero, that's how it was killed
                if signal and signal > 0:
                    return exit_code, signal
                return exit_code, None
            elif len(parts) == 1:
                return int(parts[0]), None
            else:
                return None, None
        except (ValueError, AttributeError):
            return None, None

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

            # Compute wait time for running jobs
            start_time = self._sane_start(start_time, submit_time)
            wait_time = None
            if submit_time and start_time:
                wait_time = int((start_time - submit_time).total_seconds())

            # Running jobs don't have exit codes yet
            # failure_reason = 0 (success) for now, will be updated when job completes
            failure_reason = FAILURE_SUCCESS

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
                exit_code=None,  # Not available until job completes
                exit_signal=None,  # Not available until job completes
                failure_reason=failure_reason,
                req_cpus=req_cpus,
                req_mem_mb=req_mem_mb,
                req_gpus=req_gpus,
                req_time_seconds=time_limit,
                runtime_seconds=runtime,
                wait_time_seconds=wait_time,
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
        """Parse GPU request from ReqTRES or ReqGRES format.

        Handles:
          ReqGRES:  gpu:2, gpu:a100:1
          ReqTRES:  cpu=4,mem=8G,gres/gpu=2, gres/gpu:a100=2
        """
        try:
            value = value.strip()
            if not value or value == 'N/A':
                return 0

            # ReqTRES format: comma-separated key=value pairs
            if '=' in value:
                for part in value.split(','):
                    if 'gpu' in part.lower():
                        # gres/gpu=2 or gres/gpu:a100=2
                        try:
                            return int(part.split('=')[-1])
                        except ValueError:
                            continue
                return 0

            # Legacy ReqGRES format: gpu:N or gpu:type:N
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

    @staticmethod
    def _sane_start(start, submit):
        """Return start only if it can be a real start time.

        squeue reports Slurm's ESTIMATED start for jobs that have not run yet.
        For a throttled job array that estimate can be a year into the future,
        and storing it as history fabricates a year-long queue wait. A start
        that is in the future, or precedes submission, is not a start.
        """
        if start is None:
            return None
        if start > datetime.now():
            return None
        if submit is not None and start < submit:
            return None
        return start

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
                    # Upsert job data with exit_signal, failure_reason, wait_time
                    conn.execute(
                        """
                        INSERT INTO jobs
                        (job_id, user_name, group_name, partition, job_name, state,
                         node_list, submit_time, start_time, end_time, exit_code,
                         exit_signal, failure_reason,
                         req_cpus, req_mem_mb, req_gpus, req_time_seconds, 
                         runtime_seconds, wait_time_seconds)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            state = excluded.state,
                            node_list = excluded.node_list,
                            start_time = excluded.start_time,
                            end_time = excluded.end_time,
                            exit_code = excluded.exit_code,
                            exit_signal = excluded.exit_signal,
                            failure_reason = excluded.failure_reason,
                            runtime_seconds = excluded.runtime_seconds,
                            wait_time_seconds = excluded.wait_time_seconds
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
                            record['exit_signal'],
                            record['failure_reason'],
                            record['req_cpus'],
                            record['req_mem_mb'],
                            record['req_gpus'],
                            record['req_time_seconds'],
                            record['runtime_seconds'],
                            record['wait_time_seconds'],
                        )
                    )

            # Clean up stale RUNNING jobs that are no longer in squeue.
            # When a job finishes between collection cycles, it disappears
            # from squeue but stays as RUNNING in the DB. Mark these as
            # COMPLETED so counts stay accurate. The sacct collector will
            # later update them with the real exit code/state.
            current_running = set()
            for record in data:
                if (record.get('type') == 'job'
                        and record.get('state') in ('RUNNING', 'PENDING')):
                    current_running.add(str(record['job_id']))

            if current_running:
                # Find DB jobs marked RUNNING that are no longer in squeue
                try:
                    stale_rows = conn.execute(
                        "SELECT job_id FROM jobs WHERE state IN ('RUNNING', 'PENDING')"
                    ).fetchall()
                    stale_ids = [
                        r['job_id'] for r in stale_rows
                        if str(r['job_id']) not in current_running
                    ]
                    if stale_ids:
                        placeholders = ','.join('?' for _ in stale_ids)
                        conn.execute(
                            f"UPDATE jobs SET state='COMPLETED',"
                            f" end_time=datetime('now')"
                            f" WHERE job_id IN ({placeholders})",
                            stale_ids
                        )
                        conn.commit()
                        logger.info(
                            f"Marked {len(stale_ids)} stale jobs"
                            f" as COMPLETED (no longer in squeue)")
                except Exception as e:
                    logger.warning(f"Failed to clean stale jobs: {e}")

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
