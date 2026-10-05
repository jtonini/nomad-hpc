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
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)

SACCT_FORMAT = ("JobID,User,Group,Partition,JobName,State,NodeList,AllocCPUS,ReqMem,"
                "ReqTRES,Timelimit,Elapsed,Submit,Start,End,ExitCode")

# Jobs looked up by number in sacct per run: those that left the queue, and
# older records whose outcome was assumed rather than read (see
# SlurmCollector._settle_ended_jobs).
LOOKUP_BATCH = 200
LOOKUP_MAX = 1000           # at most this many a run; the rest wait for the next

# States of a job that has not ended. squeue shows them; sacct shows them for
# jobs it still has as running.
ACTIVE_STATES = ('RUNNING', 'PENDING', 'COMPLETING', 'CONFIGURING', 'SUSPENDED',
                 'REQUEUED', 'REQUEUE_HOLD', 'REQUEUE_FED', 'RESIZING', 'SIGNALING',
                 'STAGE_OUT', 'STOPPED', 'RESV_DEL_HOLD', 'SPECIAL_EXIT', 'EXPEDITING')

# An end time written by SQLite's datetime('now') (UTC, a space, no 'T'): the
# mark of an outcome assumed by versions before 1.7.19. Every time read from
# Slurm is stored as local ISO time, with a 'T'.
_ASSUMED_END = "end_time GLOB '????-??-?? ??:??:??'"

# Sent through store() when squeue answered but there is nothing else to
# store, so that jobs which were running are still settled.
_LISTING = 'squeue_listing'

# A job number sacct -j accepts: 123, 123_4 (array task), 123+0 (het job part).
# A pending array range ("123_[5-10]") is not one job; its tasks get their own rows.
_LOOKUP_ID = re.compile(r'^\d+(_\d+)?(\+\d+)?$')

# A job that left the queue and that sacct cannot (yet) account for. Not a
# Slurm state: its outcome is unknown, and no success or failure count takes it.
UNKNOWN = 'UNKNOWN'

_JOB_UPSERT = """
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
"""
_JOB_FIELDS = ('job_id', 'user_name', 'group_name', 'partition', 'job_name', 'state',
               'node_list', 'submit_time', 'start_time', 'end_time', 'exit_code',
               'exit_signal', 'failure_reason', 'req_cpus', 'req_mem_mb', 'req_gpus',
               'req_time_seconds', 'runtime_seconds', 'wait_time_seconds')


def _state_word(state) -> str:
    """'CANCELLED by 123' -> 'CANCELLED'."""
    parts = str(state or '').split()
    return parts[0].rstrip('+').upper() if parts else ''


def _same_submit(stored, submit) -> bool:
    """Whether a stored submit time is sacct's: job numbers are reused after
    Slurm's counter restarts, and sacct -j answers with the newest job of a number."""
    if submit is None:
        return False
    try:
        return datetime.fromisoformat(str(stored).replace(' ', 'T')) == submit
    except ValueError:
        return False


def _utc_to_local(text):
    """'YYYY-MM-DD HH:MM:SS' in UTC (SQLite's datetime('now')) as local ISO time,
    the clock every other job time is in; anything else comes back unchanged."""
    try:
        t = datetime.strptime(str(text), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return text
    return t.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()


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

        # None or [] = all ("empty = all", as the example config says).
        self.partitions = config.get('partitions') or None
        self.job_history_days = config.get('job_history_days', 7)
        self.collect_queue = config.get('collect_queue', True)
        self.collect_jobs = config.get('collect_jobs', True)
        self.collect_completed = config.get('collect_completed', True)
        # Every job id squeue listed in this run, before any partition filter;
        # None when squeue did not answer, and then nothing can be concluded
        # about jobs that seem gone.
        self._squeue_ids: set[str] | None = None
        self._listed: set[str] = set()

        logger.info(f"SlurmCollector monitoring partitions: {self.partitions or 'all'}")

    def collect(self) -> list[dict[str, Any]]:
        """Collect SLURM queue and job data."""
        data = []
        self._squeue_ids = None

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
                self._squeue_ids = set(self._listed)
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
            if self._squeue_ids is None:
                raise CollectionError("No SLURM data collected")
            # An empty queue and no recent jobs: nothing to store, but the jobs
            # that were running have ended and still need their outcomes.
            self.note = (f"queue empty; no jobs in sacct's last "
                         f"{self.job_history_days} days")
            data.append({'type': _LISTING})

        return data

    def count_records(self, data: list[dict[str, Any]]) -> int:
        return sum(1 for r in data if r.get('type') != _LISTING)

    def _collect_completed_jobs(self) -> list[JobInfo]:
        """Collect completed job information from sacct."""
        try:
            # --allusers: without it sacct shows a non-root user only their own
            # jobs, and every other job would go unaccounted for.
            jobs = [job for job in self._sacct('--allusers',
                                               f'--starttime=now-{self.job_history_days}days')
                    # Filter by partition if configured
                    if self.partitions is None or job.partition in self.partitions]
            logger.debug(f"Collected {len(jobs)} completed jobs from sacct")
            return jobs

        except subprocess.TimeoutExpired:
            raise CollectionError("sacct command timed out")
        except FileNotFoundError:
            logger.warning("sacct command not found - skipping completed job collection")
            return []

    def _sacct(self, *selection: str) -> list[JobInfo]:
        """sacct, one line per job (no steps), parsed. Raises on failure."""
        # ExitCode gives exit_status:signal.
        result = subprocess.run(
            ['sacct', '-n', '-P', '-X', *selection, f'--format={SACCT_FORMAT}'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise CollectionError(f"sacct failed: {result.stderr}")
        jobs = []
        for line in result.stdout.splitlines():
            if line.strip():
                job = self._parse_sacct_job(line)
                if job:
                    jobs.append(job)
        return jobs

    def _lookup_jobs(self, job_ids: list[str]) -> dict[str, JobInfo] | None:
        """What sacct knows about these jobs, by number; None if sacct can't be asked."""
        found: dict[str, JobInfo] = {}
        try:
            for i in range(0, len(job_ids), LOOKUP_BATCH):
                chunk = job_ids[i:i + LOOKUP_BATCH]
                for job in self._sacct('-j', ','.join(chunk)):
                    found[str(job.job_id)] = job
        except (CollectionError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"sacct lookup of {len(job_ids)} jobs failed: {e}")
            return None
        return found

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
            listed = set()
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                # Every listed job counts as still there, whether or not its
                # line parses or its partition is one we follow.
                listed.add(line.split('|', 1)[0].strip())

                job = self._parse_job_line(line)
                if job:
                    # Filter by partition if configured
                    if self.partitions is None or job.partition in self.partitions:
                        jobs.append(job)

            self._listed = listed
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
                    conn.execute(_JOB_UPSERT, tuple(record[f] for f in _JOB_FIELDS))

            # Committed before sacct is asked about anything, so the database
            # is not held locked while it answers.
            conn.commit()
            try:
                self._settle_ended_jobs(conn, data)
            except Exception as e:
                logger.warning(f"Failed to settle ended jobs: {e}")

            conn.commit()
            logger.debug(f"Stored {len(data)} SLURM records")

    def _settle_ended_jobs(self, conn, data: list[dict[str, Any]]) -> None:
        """Give jobs that left the queue their real outcome, never an assumed one.

        A job that ends between two runs leaves squeue; when this run's sacct
        pull already reported it, the upsert above has its outcome. The rest
        are looked up in sacct by number (with the submit time checked, since
        numbers are reused). Those sacct cannot account for yet are marked
        UNKNOWN: ended by now, outcome not known, no exit code or failure
        reason, so no success or failure count takes them; the next sacct
        pull corrects them. A pending array range ("123_[5-10]") that left the
        queue is dropped: it stood for tasks that now have rows of their own.

        Versions before 1.7.19 marked such jobs COMPLETED, with the end time
        in UTC (no 'T') where every other job time is local. Those rows are
        looked up too, LOOKUP_BATCH a run, newest first; if sacct no longer
        has them they become UNKNOWN, with the end time moved to local.
        """
        listed = self._squeue_ids
        if listed is None:
            return      # squeue did not answer this run: no job can be called ended
        still = set(listed) | {
            str(r['job_id']) for r in data
            if r.get('type') == 'job' and _state_word(r.get('state')) in ACTIVE_STATES}
        marks = ','.join('?' * len(ACTIVE_STATES))
        ended = [str(r[0]) for r in conn.execute(
            f"SELECT job_id FROM jobs WHERE state IN ({marks})", ACTIVE_STATES)
            if str(r[0]) not in still][:LOOKUP_MAX]
        ended_set = set(ended)
        recent = (datetime.now() - timedelta(days=self.job_history_days)).isoformat(
            timespec='seconds')
        repair = [str(r[0]) for r in conn.execute(
            f"SELECT job_id FROM jobs WHERE (state = 'COMPLETED' AND {_ASSUMED_END}) "
            "OR (state = ? AND end_time >= ?) "
            # assumed outcomes first: the 7-day pull settles recent UNKNOWNs anyway
            "ORDER BY state = ?, end_time DESC LIMIT ?",
            (UNKNOWN, recent, UNKNOWN, LOOKUP_BATCH))
            if str(r[0]) not in ended_set]
        todo = ended + repair
        if not todo:
            return

        found = self._lookup_jobs([j for j in todo if _LOOKUP_ID.match(j)])
        now = datetime.now().isoformat(timespec='seconds')
        settled = unknown = dropped = 0
        for job_id in todo:
            row = conn.execute("SELECT state, submit_time, end_time FROM jobs WHERE job_id = ?",
                               (job_id,)).fetchone()
            if row is None:
                continue
            if '[' in job_id:
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                dropped += 1
                continue
            fresh = job_id in ended_set
            job = (found or {}).get(job_id)
            if job is not None and not (_same_submit(row['submit_time'], job.submit_time)
                                        or (row['submit_time'] is None and fresh)):
                job = None      # sacct's job of that number is another, later one
            if job is not None:
                if fresh and _state_word(job.state) in ACTIVE_STATES:
                    continue    # sacct still has it running: leave it as it is
                # Its outcome; or, for a row marked UNKNOWN or assumed, sacct
                # saying it is still active, which puts it back as it is.
                record = job.to_dict()
                conn.execute(_JOB_UPSERT, tuple(record[f] for f in _JOB_FIELDS))
                settled += 1
            elif fresh:
                cur = conn.execute(
                    f"UPDATE jobs SET state = ?, end_time = ?, exit_code = NULL, "
                    f"exit_signal = NULL, failure_reason = NULL "
                    f"WHERE job_id = ? AND state IN ({marks})",
                    (UNKNOWN, now, job_id, *ACTIVE_STATES))
                unknown += cur.rowcount
            elif found is not None and row['state'] == 'COMPLETED':
                # An old assumed outcome sacct cannot confirm: say so.
                cur = conn.execute(
                    f"UPDATE jobs SET state = ?, end_time = ?, exit_code = NULL, "
                    f"exit_signal = NULL, failure_reason = NULL "
                    f"WHERE job_id = ? AND state = 'COMPLETED' AND {_ASSUMED_END}",
                    (UNKNOWN, _utc_to_local(row['end_time']), job_id))
                unknown += cur.rowcount
        if settled or unknown or dropped:
            logger.info(f"Jobs that left the queue or had an assumed outcome: "
                        f"{settled} settled from sacct, {unknown} marked {UNKNOWN}, "
                        f"{dropped} pending array ranges dropped")

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
