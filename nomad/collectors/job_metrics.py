# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAD Job Metrics Collector

Collects detailed job metrics from SLURM sacct for similarity analysis.
Computes feature vectors and health scores for completed jobs.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


# Feature weights for health score calculation
HEALTH_WEIGHTS = {
    'nfs_ratio': -0.3,      # High NFS usage = bad
    'had_swap': -0.2,       # Swap usage = bad
    'io_wait_high': -0.2,   # High IO wait = bad
    'exit_success': 0.3,    # Clean exit = good
}


@dataclass
class JobMetrics:
    """Comprehensive job metrics from sacct."""

    job_id: str
    job_name: str
    user_name: str
    group_name: str | None
    partition: str
    state: str
    exit_code: int

    # Time metrics
    submit_time: datetime | None
    start_time: datetime | None
    end_time: datetime | None
    elapsed_seconds: int
    timelimit_seconds: int | None

    # Resource requests
    req_cpus: int
    req_mem_mb: int
    req_gpus: int

    # Actual usage
    avg_cpu_percent: float | None
    max_rss_mb: float | None
    avg_rss_mb: float | None
    max_vmsize_mb: float | None

    # I/O metrics
    max_disk_read_mb: float | None
    max_disk_write_mb: float | None
    avg_disk_read_mb: float | None
    avg_disk_write_mb: float | None

    # Derived metrics (computed)
    cpu_efficiency: float | None = None
    memory_efficiency: float | None = None
    nfs_ratio: float | None = None
    used_gpu: bool = False
    node_list: str | None = None
    had_swap: bool = False

    # Health and clustering
    health_score: float | None = None
    feature_vector: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'job_id': self.job_id,
            'job_name': self.job_name,
            'user_name': self.user_name,
            'group_name': self.group_name,
            'partition': self.partition,
            'state': self.state,
            'exit_code': self.exit_code,
            'submit_time': self.submit_time.isoformat() if self.submit_time else None,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'elapsed_seconds': self.elapsed_seconds,
            'timelimit_seconds': self.timelimit_seconds,
            'req_cpus': self.req_cpus,
            'req_mem_mb': self.req_mem_mb,
            'req_gpus': self.req_gpus,
            'avg_cpu_percent': self.avg_cpu_percent,
            'max_rss_mb': self.max_rss_mb,
            'avg_rss_mb': self.avg_rss_mb,
            'max_vmsize_mb': self.max_vmsize_mb,
            'max_disk_read_mb': self.max_disk_read_mb,
            'max_disk_write_mb': self.max_disk_write_mb,
            'avg_disk_read_mb': self.avg_disk_read_mb,
            'avg_disk_write_mb': self.avg_disk_write_mb,
            'cpu_efficiency': self.cpu_efficiency,
            'memory_efficiency': self.memory_efficiency,
            'nfs_ratio': self.nfs_ratio,
            'used_gpu': self.used_gpu,
            'node_list': self.node_list,
            'had_swap': self.had_swap,
            'health_score': self.health_score,
            'feature_vector': json.dumps(self.feature_vector),
        }


@registry.register
class JobMetricsCollector(BaseCollector):
    """
    Collector for detailed job metrics using sacct.
    
    Configuration options:
        lookback_hours: Hours of completed jobs to collect (default: 168 = 7 days).
            Used as a fallback when no prior watermark exists. The collector
            tracks the most recent processed job's end_time and prefers that
            over a fixed window, so this only matters on first run or after
            extended collector outages.
        min_runtime_seconds: Minimum job runtime to include (default: 10)
        partitions: List of partitions to monitor (default: all)
    
    Collected data:
        - Job metadata and resources
        - CPU, memory, disk I/O from sacct
        - Computed efficiency metrics
        - Feature vectors for similarity analysis
        - Health scores
    """

    name = "job_metrics"
    description = "Detailed job metrics from sacct"
    default_interval = 300  # 5 minutes

    # sacct format string
    SACCT_FORMAT = (
        "JobID,JobName,User,Group,Partition,State,ExitCode,"
        "Submit,Start,End,Elapsed,Timelimit,"
        "ReqCPUS,ReqMem,ReqTRES,"
        "AveCPU,MaxRSS,AveRSS,MaxVMSize,"
        "MaxDiskRead,MaxDiskWrite,AveDiskRead,AveDiskWrite,"
        "NodeList"
    )

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self.lookback_hours = config.get('lookback_hours', 168)  # 7 days fallback
        self.min_runtime = config.get('min_runtime_seconds', 10)
        self.partitions = config.get('partitions', None)

        # Track processed jobs to avoid duplicates
        self._processed_jobs: set[str] = set()
        self._load_processed_jobs()

        logger.info(f"JobMetricsCollector: lookback={self.lookback_hours}h "
                    f"(fallback when no watermark), min_runtime={self.min_runtime}s")

    def _load_processed_jobs(self) -> None:
        """Load already processed job IDs from database."""
        try:
            with self.get_db_connection() as conn:
                rows = conn.execute(
                    "SELECT job_id FROM job_summary"
                ).fetchall()
                self._processed_jobs = {row[0] for row in rows}
                logger.debug(f"Loaded {len(self._processed_jobs)} processed jobs")
        except Exception as e:
            logger.debug(f"Could not load processed jobs: {e}")

    def _get_watermark(self) -> datetime | None:
        """
        Return the end_time of the most recently processed job, or None
        if no jobs have been processed yet.

        Used as the start of the next sacct query so we don't miss
        long-running jobs that complete after the fixed lookback window
        ends. Long-job clusters (where typical job duration > lookback_hours)
        would otherwise produce zero collected jobs every cycle indefinitely.
        """
        try:
            with self.get_db_connection() as conn:
                row = conn.execute(
                    """
                    SELECT MAX(j.end_time)
                    FROM jobs j
                    JOIN job_summary js ON j.job_id = js.job_id
                    """
                ).fetchone()
                if row and row[0]:
                    return datetime.fromisoformat(row[0])
        except Exception as e:
            logger.debug(f"Could not load watermark: {e}")
        return None

    def collect(self) -> list[dict[str, Any]]:
        """Collect job metrics from sacct."""

        # Calculate time range. Prefer the watermark (the end_time of the
        # most recent successfully processed job) over the fixed lookback,
        # but use whichever produces an earlier start_time so we don't miss
        # jobs near boundaries. The 5-minute overlap is to handle clock skew
        # and jobs that complete during a collection cycle.
        end_time = datetime.now()
        watermark = self._get_watermark()
        fallback_start = end_time - timedelta(hours=self.lookback_hours)
        if watermark is not None:
            start_time = min(watermark - timedelta(minutes=5), fallback_start)
        else:
            start_time = fallback_start

        try:
            # Build sacct command
            cmd = [
                'sacct',
                '-a',              # All users (collector may run as non-submitting monitoring user)
                '-n', '-P',        # No header, parseable. -X dropped so step rows return too.
                '--format', self.SACCT_FORMAT,
                '--starttime', start_time.strftime('%Y-%m-%dT%H:%M:%S'),
                '--endtime', end_time.strftime('%Y-%m-%dT%H:%M:%S'),
                '--state', 'COMPLETED,FAILED,TIMEOUT,CANCELLED',
            ]

            if self.partitions:
                cmd.extend(['--partition', ','.join(self.partitions)])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                raise CollectionError(f"sacct failed: {result.stderr}")

            # Parse jobs. SLURM emits one row per job plus one row per step
            # (e.g. job '506137' has rows '506137', '506137.batch', '506137.0').
            # Job rows carry request fields (ReqCPUS, ReqMem); step rows carry
            # actual usage metrics (AveCPU, MaxRSS, etc.). We group by parent
            # JobID and merge the two so each emitted record has both.
            grouped: dict[str, dict] = {}  # parent_jobid -> {'job': line, 'steps': [lines]}
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                first_field = line.split('|', 1)[0]
                parent_id = first_field.split('.')[0]
                bucket = grouped.setdefault(parent_id, {'job': None, 'steps': []})
                if '.' in first_field:
                    bucket['steps'].append(line)
                else:
                    bucket['job'] = line

            jobs = []
            for parent_id, bucket in grouped.items():
                if bucket['job'] is None:
                    # Step rows without a parent (rare; can happen at sacct
                    # window boundaries). Skip; we have no request fields.
                    continue
                job = self._parse_sacct_line(bucket['job'])
                if job is None:
                    continue
                # Merge in step-level metrics (max across all steps).
                self._merge_step_metrics(job, bucket['steps'])
                if not self._should_include(job):
                    continue
                self._compute_derived_metrics(job)
                self._compute_feature_vector(job)
                self._compute_health_score(job)
                jobs.append({
                    'type': 'job_metrics',
                    **job.to_dict()
                })
                self._processed_jobs.add(job.job_id)

            # Distinguish between "sacct returned nothing" and "we filtered
            # everything out". Silent zero-result loops are how this bug hid
            # for weeks; explicit logging surfaces it.
            if not result.stdout.strip():
                logger.info(
                    f"sacct returned no rows for window "
                    f"{start_time.isoformat()} to {end_time.isoformat()} — "
                    f"either no jobs in window, or all jobs still RUNNING"
                )
            else:
                raw_lines = len(result.stdout.strip().split('\n'))
                logger.info(
                    f"Collected metrics for {len(jobs)} jobs "
                    f"(sacct returned {raw_lines} rows)"
                )
            return jobs

        except subprocess.TimeoutExpired:
            raise CollectionError("sacct command timed out")

    def _parse_sacct_line(self, line: str) -> JobMetrics | None:
        """Parse a sacct output line."""
        try:
            parts = line.split('|')
            if len(parts) < 24:
                return None

            job_id = parts[0].split('.')[0]  # Remove step suffix

            return JobMetrics(
                job_id=job_id,
                job_name=parts[1] or 'unknown',
                user_name=parts[2] or 'unknown',
                group_name=parts[3] or None,
                partition=parts[4] or 'unknown',
                state=parts[5] or 'UNKNOWN',
                exit_code=self._parse_exit_code(parts[6]),
                submit_time=self._parse_datetime(parts[7]),
                start_time=self._parse_datetime(parts[8]),
                end_time=self._parse_datetime(parts[9]),
                elapsed_seconds=self._parse_elapsed(parts[10]),
                timelimit_seconds=self._parse_elapsed(parts[11]),
                req_cpus=self._parse_int(parts[12]),
                req_mem_mb=self._parse_memory(parts[13]),
                req_gpus=self._parse_gpus(parts[14]),
                avg_cpu_percent=self._parse_cpu_time(parts[15]),
                max_rss_mb=self._parse_memory(parts[16]),
                avg_rss_mb=self._parse_memory(parts[17]),
                max_vmsize_mb=self._parse_memory(parts[18]),
                max_disk_read_mb=self._parse_memory(parts[19]),
                max_disk_write_mb=self._parse_memory(parts[20]),
                avg_disk_read_mb=self._parse_memory(parts[21]),
                avg_disk_write_mb=self._parse_memory(parts[22]),
                node_list=(parts[23] or None) if len(parts) > 23 else None,
            )
        except Exception as e:
            logger.debug(f"Failed to parse sacct line: {e}")
            return None

    def _merge_step_metrics(self, job: JobMetrics, step_lines: list[str]) -> None:
        """
        Merge step-level metrics into the parent job record.

        SLURM stores AveCPU, MaxRSS, AveRSS, MaxVMSize, and disk metrics on
        steps (.batch, .0, .extern, etc.), not on the parent job row. We take
        the maximum across all steps for each metric — this gives "peak
        observed across the job's execution" which is the meaningful number
        for efficiency analysis.

        AveCPU is reported as cumulative CPU time (HH:MM:SS), not percentage.
        We sum across steps and convert to a percentage in
        _compute_derived_metrics using elapsed_seconds and req_cpus.
        """
        max_rss_mb = 0.0
        avg_rss_mb_sum = 0.0
        avg_rss_mb_count = 0
        max_vmsize_mb = 0.0
        max_disk_read_mb = 0.0
        max_disk_write_mb = 0.0
        avg_disk_read_sum = 0.0
        avg_disk_read_count = 0
        avg_disk_write_sum = 0.0
        avg_disk_write_count = 0
        cpu_seconds_total = 0.0

        for line in step_lines:
            parts = line.split('|')
            if len(parts) < 23:
                continue
            # AveCPU is parts[15]: cumulative CPU time, summed across steps
            ave_cpu = self._parse_elapsed(parts[15])
            if ave_cpu:
                cpu_seconds_total += ave_cpu
            for value, _max in (
                (self._parse_memory(parts[16]), 'max_rss'),
                (self._parse_memory(parts[18]), 'max_vmsize'),
                (self._parse_memory(parts[19]), 'max_disk_read'),
                (self._parse_memory(parts[20]), 'max_disk_write'),
            ):
                pass  # handled explicitly below for clarity
            v = self._parse_memory(parts[16])
            if v is not None:
                max_rss_mb = max(max_rss_mb, v)
            v = self._parse_memory(parts[17])
            if v is not None:
                avg_rss_mb_sum += v
                avg_rss_mb_count += 1
            v = self._parse_memory(parts[18])
            if v is not None:
                max_vmsize_mb = max(max_vmsize_mb, v)
            v = self._parse_memory(parts[19])
            if v is not None:
                max_disk_read_mb = max(max_disk_read_mb, v)
            v = self._parse_memory(parts[20])
            if v is not None:
                max_disk_write_mb = max(max_disk_write_mb, v)
            v = self._parse_memory(parts[21])
            if v is not None:
                avg_disk_read_sum += v
                avg_disk_read_count += 1
            v = self._parse_memory(parts[22])
            if v is not None:
                avg_disk_write_sum += v
                avg_disk_write_count += 1

        # Convert cumulative CPU seconds to a percent using job's elapsed time
        # and allocated CPUs. Done lazily here; _compute_derived_metrics expects
        # avg_cpu_percent to be a percentage already.
        if cpu_seconds_total > 0 and job.elapsed_seconds > 0 and job.req_cpus > 0:
            theoretical_max = job.elapsed_seconds * job.req_cpus
            job.avg_cpu_percent = (cpu_seconds_total / theoretical_max) * 100.0
        # Memory and disk: take what we found, leave None if nothing reported
        if max_rss_mb > 0:
            job.max_rss_mb = max_rss_mb
        if avg_rss_mb_count > 0:
            job.avg_rss_mb = avg_rss_mb_sum / avg_rss_mb_count
        if max_vmsize_mb > 0:
            job.max_vmsize_mb = max_vmsize_mb
        if max_disk_read_mb > 0:
            job.max_disk_read_mb = max_disk_read_mb
        if max_disk_write_mb > 0:
            job.max_disk_write_mb = max_disk_write_mb
        if avg_disk_read_count > 0:
            job.avg_disk_read_mb = avg_disk_read_sum / avg_disk_read_count
        if avg_disk_write_count > 0:
            job.avg_disk_write_mb = avg_disk_write_sum / avg_disk_write_count

    def _should_include(self, job: JobMetrics) -> bool:
        """Check if job should be included in collection."""
        # Skip already processed
        if job.job_id in self._processed_jobs:
            return False

        # Skip very short jobs
        if job.elapsed_seconds < self.min_runtime:
            return False

        return True

    def _compute_derived_metrics(self, job: JobMetrics) -> None:
        """Compute derived metrics from raw data."""

        # CPU efficiency: actual CPU time / (elapsed * cores)
        if job.avg_cpu_percent and job.elapsed_seconds > 0 and job.req_cpus > 0:
            # avg_cpu_percent is already percentage
            job.cpu_efficiency = job.avg_cpu_percent / 100.0

        # Memory efficiency: max_rss / requested
        if job.max_rss_mb and job.req_mem_mb > 0:
            job.memory_efficiency = job.max_rss_mb / job.req_mem_mb

        # GPU usage detection
        job.used_gpu = job.req_gpus > 0

        # Swap detection (VMSize >> RSS suggests swap)
        if job.max_vmsize_mb and job.max_rss_mb:
            job.had_swap = job.max_vmsize_mb > job.max_rss_mb * 2

        # NFS ratio (placeholder - would need actual NFS vs local breakdown)
        # For now, estimate from total I/O patterns
        # In production, this would come from /proc or cgroups
        job.nfs_ratio = 0.0  # Default to local

    def _compute_feature_vector(self, job: JobMetrics) -> None:
        """Compute normalized feature vector for similarity analysis."""

        # Features (all normalized 0-1):
        features = [
            min(job.cpu_efficiency or 0, 1.0),
            min(job.memory_efficiency or 0, 1.0),
            1.0 if job.used_gpu else 0.0,
            1.0 if job.had_swap else 0.0,
            job.nfs_ratio or 0.0,
            min((job.max_disk_write_mb or 0) / 10000, 1.0),  # Normalize to 10GB
            1.0 if job.state == 'COMPLETED' and job.exit_code == 0 else 0.0,
        ]

        job.feature_vector = features

    def _compute_health_score(self, job: JobMetrics) -> None:
        """Compute health score (0-1, higher is better)."""

        score = 0.5  # Start neutral

        # Success bonus
        if job.state == 'COMPLETED' and job.exit_code == 0:
            score += 0.3
        elif job.state in ('FAILED', 'TIMEOUT'):
            score -= 0.3

        # Efficiency bonuses
        if job.cpu_efficiency:
            if job.cpu_efficiency > 0.8:
                score += 0.1
            elif job.cpu_efficiency < 0.2:
                score -= 0.1

        if job.memory_efficiency:
            if 0.5 <= job.memory_efficiency <= 0.9:
                score += 0.1  # Good memory usage
            elif job.memory_efficiency > 1.0:
                score -= 0.1  # Over-used memory

        # Penalties
        if job.had_swap:
            score -= 0.2

        if job.nfs_ratio and job.nfs_ratio > 0.5:
            score -= 0.1

        # Clamp to [0, 1]
        job.health_score = max(0.0, min(1.0, score))

    def _parse_int(self, value: str) -> int:
        try:
            return int(value.strip()) if value.strip() else 0
        except ValueError:
            return 0

    def _parse_exit_code(self, value: str) -> int:
        """Parse exit code from format like '0:0'."""
        try:
            if ':' in value:
                return int(value.split(':')[0])
            return int(value) if value.strip() else 0
        except ValueError:
            return 0

    def _parse_memory(self, value: str) -> float | None:
        """Parse memory string to MB."""
        try:
            value = value.strip().upper()
            if not value or value in ('', 'N/A', '0'):
                return None

            # Handle units
            multipliers = {'K': 1/1024, 'M': 1, 'G': 1024, 'T': 1024*1024}

            for suffix, mult in multipliers.items():
                if value.endswith(suffix):
                    return float(value[:-1]) * mult

            # Try as bytes, convert to MB
            return float(value) / (1024 * 1024)
        except ValueError:
            return None

    def _parse_elapsed(self, value: str) -> int:
        """Parse elapsed time to seconds."""
        try:
            value = value.strip()
            if not value or value in ('', 'UNLIMITED', 'INVALID'):
                return 0

            # Format: [D-]HH:MM:SS or MM:SS
            days = 0
            if '-' in value:
                day_part, time_part = value.split('-', 1)
                days = int(day_part)
            else:
                time_part = value

            parts = time_part.split(':')
            if len(parts) == 3:
                h, m, s = map(int, parts)
            elif len(parts) == 2:
                h = 0
                m, s = map(int, parts)
            else:
                return int(parts[0])

            return days * 86400 + h * 3600 + m * 60 + s
        except ValueError:
            return 0

    def _parse_datetime(self, value: str) -> datetime | None:
        """Parse datetime string."""
        try:
            value = value.strip()
            if not value or value in ('Unknown', 'N/A', 'None'):
                return None

            for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            return None
        except Exception:
            return None

    def _parse_cpu_time(self, value: str) -> float | None:
        """Parse CPU time/percentage."""
        try:
            value = value.strip()
            if not value or value in ('', 'N/A'):
                return None

            # If it's a time format (HH:MM:SS), convert to percentage
            if ':' in value:
                seconds = self._parse_elapsed(value)
                return float(seconds) if seconds > 0 else None

            return float(value)
        except ValueError:
            return None

    def _parse_gpus(self, value: str) -> int:
        """Parse GPU request from ReqTRES format (e.g., 'gres/gpu=2' or 'gres/gpu:a100=2')."""
        try:
            value = value.strip()
            if not value or value in ('', 'N/A'):
                return 0

            # ReqTRES format: cpu=4,mem=8G,gres/gpu=2 or gres/gpu:type=N
            if 'gpu' in value.lower():
                # Find gpu part
                for part in value.split(','):
                    if 'gpu' in part.lower():
                        # Extract number after =
                        if '=' in part:
                            num = part.split('=')[-1]
                            return int(num)
            return 0
        except ValueError:
            return 0

    def _compute_avg_gpu_util(self, conn, node_list, start_time, end_time):
        """Mean DCGM real utilization for a GPU job, over its node(s) + window.

        Uses gpu_stats.real_util_pct (DCGM-derived honest utilization), NOT
        gpu_util_percent (nvidia-smi's misleading "a kernel ran" number).
        Gated on data_source='dcgm' — where DCGM isn't deployed there is no
        real measurement, so we return None (scored N/A downstream) rather
        than fabricate a number.

        Coarse join: averages across all GPUs on the job's node during the
        window. Jobs record GPU count (req_gpus), not specific indices, so
        per-GPU attribution isn't possible; this blends when jobs share a
        node's GPUs but is directionally correct for an education tool.
        """
        if not node_list or not start_time or not end_time:
            return None
        nodes = [n.strip() for n in str(node_list).split(",") if n.strip()]
        if not nodes:
            return None
        placeholders = ",".join("?" * len(nodes))
        row = conn.execute(
            f"""
            SELECT AVG(real_util_pct)
            FROM gpu_stats
            WHERE node_name IN ({placeholders})
              AND data_source = 'dcgm'
              AND real_util_pct IS NOT NULL
              AND timestamp BETWEEN ? AND ?
            """,
            (*nodes, start_time, end_time),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store job metrics in database."""

        with self.get_db_connection() as conn:
            for record in data:
                if record.get('type') != 'job_metrics':
                    continue

                # Update jobs table
                conn.execute(
                    """
                    INSERT INTO jobs
                    (job_id, user_name, group_name, partition, job_name, state,
                     submit_time, start_time, end_time, exit_code,
                     req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds,
                     node_list)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        state = excluded.state,
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
                        record['submit_time'],
                        record['start_time'],
                        record['end_time'],
                        record['exit_code'],
                        record['req_cpus'],
                        record['req_mem_mb'],
                        record['req_gpus'],
                        record['timelimit_seconds'],
                        record['elapsed_seconds'],
                        record.get('node_list'),
                    )
                )

                # Update job_summary table
                conn.execute(
                    """
                    INSERT INTO job_summary
                    (job_id, peak_cpu_percent, peak_memory_gb, avg_cpu_percent, avg_memory_gb,
                     total_local_write_gb, nfs_ratio, used_gpu, avg_gpu_util, had_swap,
                     health_score, feature_vector)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        peak_cpu_percent = excluded.peak_cpu_percent,
                        peak_memory_gb = excluded.peak_memory_gb,
                        avg_gpu_util = excluded.avg_gpu_util,
                        health_score = excluded.health_score,
                        feature_vector = excluded.feature_vector
                    """,
                    (
                        record['job_id'],
                        record['avg_cpu_percent'],
                        (record['max_rss_mb'] or 0) / 1024,
                        record['avg_cpu_percent'],
                        (record['avg_rss_mb'] or 0) / 1024,
                        (record['max_disk_write_mb'] or 0) / 1024,
                        record['nfs_ratio'],
                        record['used_gpu'],
                        (self._compute_avg_gpu_util(
                            conn, record.get('node_list'),
                            record.get('start_time'), record.get('end_time'),
                        ) if record.get('used_gpu') else None),
                        record['had_swap'],
                        record['health_score'],
                        record['feature_vector'],
                    )
                )

            conn.commit()
            logger.debug(f"Stored {len(data)} job metrics records")

    def get_job_features(self, job_id: str) -> list[float] | None:
        """Get feature vector for a specific job."""
        with self.get_db_connection() as conn:
            row = conn.execute(
                "SELECT feature_vector FROM job_summary WHERE job_id = ?",
                (job_id,)
            ).fetchone()

            if row and row[0]:
                return json.loads(row[0])
        return None

    def get_recent_features(self, limit: int = 100) -> list[tuple[str, list[float]]]:
        """Get recent job IDs and their feature vectors."""
        with self.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT js.job_id, js.feature_vector
                FROM job_summary js
                JOIN jobs j ON js.job_id = j.job_id
                WHERE js.feature_vector IS NOT NULL
                ORDER BY j.end_time DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()

            return [(row[0], json.loads(row[1])) for row in rows if row[1]]
