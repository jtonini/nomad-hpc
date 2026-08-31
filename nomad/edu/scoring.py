# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Edu — Proficiency Scoring Engine

Scores each job across five dimensions of computational proficiency:
    1. CPU Efficiency      — how well CPU resources were utilized
    2. Memory Efficiency   — how well memory was sized
    3. Time Estimation     — how accurately walltime was estimated
    4. I/O Awareness       — appropriate use of local vs network storage
    5. GPU Utilization     — effective use of GPU resources (when applicable)

Each dimension produces a score from 0-100 and a proficiency level:
    Excellent  (85-100)  — demonstrates strong HPC understanding
    Good       (65-84)   — reasonable usage with minor waste
    Developing (40-64)   — learning, with clear room for improvement
    Needs Work (0-39)    — significant resource waste or misconfiguration

Each dimension also produces a structured Suggestion (not a formatted string)
carrying directive name, suggested value, current request, actual usage, and
unit. This lets aggregation logic in insights.py work with numeric data
across many jobs (median, p95, etc.) instead of string-matching, and lets
formatters compose the final user-facing message with full distribution
context.

The scoring functions are intentionally separated so they can be reused by:
    - nomad edu explain  (single job analysis)
    - nomad edu report   (course/group aggregation)
    - nomad edu me       (cross-job systemic insights)
    - dashboard edu tab  (visual proficiency tracking)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Proficiency levels ───────────────────────────────────────────────

LEVELS = [
    (85, "Excellent"),
    (65, "Good"),
    (40, "Developing"),
    (0,  "Needs Work"),
]


def proficiency_level(score: float) -> str:
    """Map a 0-100 score to a proficiency level."""
    for threshold, label in LEVELS:
        if score >= threshold:
            return label
    return "Needs Work"


def bar(score: float, width: int = 10) -> str:
    """Render a score as a text progress bar."""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── Rounding utilities ───────────────────────────────────────────────

# Memory ladder: SLURM-friendly values users actually type
MEMORY_LADDER_MB = [
    512,         # 512M (rare)
    1024,        # 1G
    2048, 4096, 8192, 16384,
    32768, 65536, 98304, 131072,        # 32G, 64G, 96G, 128G
    196608, 262144, 393216, 524288,     # 192G, 256G, 384G, 512G
    786432, 1048576,                    # 768G, 1024G
]


def round_memory_up(target_mb: float) -> int:
    """Round a memory value (MB) up to the nearest SLURM-friendly value."""
    target_mb = max(target_mb, 1024)  # never below 1G
    for v in MEMORY_LADDER_MB:
        if v >= target_mb:
            return v
    # Above the ladder — round up to nearest 512G
    return int((target_mb + 524287) // 524288 * 524288)


def format_memory(mb: float) -> str:
    """Format MB as a SLURM-friendly string like '4G' or '128G'."""
    if mb >= 1024 and mb % 1024 == 0:
        return f"{int(mb // 1024)}G"
    if mb < 1024:
        return f"{int(mb)}M"
    return f"{mb / 1024:.1f}G"


def round_time_up(target_seconds: float) -> int:
    """
    Round a time value (seconds) up to a SLURM-friendly value.

    Strategy:
        < 1 hour:   round up to nearest 5 minutes (minimum 5 minutes)
        1-4 hours:  round up to nearest 30 minutes
        > 4 hours:  round up to nearest 1 hour
    """
    target_seconds = max(target_seconds, 300)  # minimum 5 min
    if target_seconds <= 3600:
        # nearest 5 min
        return int((target_seconds + 299) // 300 * 300)
    if target_seconds <= 14400:
        # nearest 30 min
        return int((target_seconds + 1799) // 1800 * 1800)
    # nearest hour
    return int((target_seconds + 3599) // 3600 * 3600)


def format_time_slurm(seconds: float) -> str:
    """Format seconds as a SLURM walltime string 'HH:MM:SS' or 'D-HH:MM:SS'."""
    s = max(0, int(seconds))
    days, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if days > 0:
        return f"{days}-{h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_duration_human(seconds: float) -> str:
    """Format seconds as a human-readable duration: '4h 31m', '12 min', etc."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} sec"
    if s < 3600:
        return f"{s // 60} min"
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h < 24:
        if m > 0:
            return f"{h}h {m:02d}m"
        return f"{h}h"
    days = h // 24
    h_left = h % 24
    return f"{days}d {h_left}h"


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Suggestion:
    """
    Structured recommendation, dimension-agnostic.

    The fields carry numeric data so aggregation across many jobs can use
    sensible statistics (median, p95) rather than string-matching the
    pre-formatted output. Formatters render this to text/JSON at the
    presentation layer.
    """
    directive: str             # SLURM directive name: "ntasks", "mem", "time", "gres"
    suggested_value: float     # value in canonical units
    current_value: float       # what the user currently requests
    actual_usage: float        # what the job actually used (peak or avg)
    unit: str                  # "cores", "MB", "seconds", "MB/s"
    rationale: str = ""        # one-line context: "1 of 6 cores active"
    context: Optional[dict[str, Any]] = None  # structured payload for verdict-kind suggestions (cluster targets, headroom, sbatch snippets)

    @property
    def utilization_pct(self) -> float:
        """Actual usage as a percentage of current request."""
        if self.current_value <= 0:
            return 0.0
        return (self.actual_usage / self.current_value) * 100

    def __str__(self) -> str:
        """
        Render in the legacy single-line format for backward compatibility
        with consumers that expect a string. New consumers should use the
        structured fields directly.
        """
        return format_suggestion_directive(self)


def format_suggestion_directive(s: Suggestion) -> str:
    """
    Render a Suggestion as a SLURM directive line for legacy display.
    Format depends on the directive type.
    """
    if s.directive == "ntasks":
        return f"#SBATCH --ntasks={int(s.suggested_value)}"
    if s.directive == "mem":
        return f"#SBATCH --mem={format_memory(s.suggested_value)}"
    if s.directive == "time":
        return f"#SBATCH --time={format_time_slurm(s.suggested_value)}"
    if s.directive == "gres":
        return f"#SBATCH --gres=gpu:{int(s.suggested_value)}"
    return f"#SBATCH --{s.directive}={s.suggested_value}"


@dataclass
class DimensionScore:
    """Score for a single proficiency dimension."""
    name: str
    score: float                          # 0-100
    level: str                            # Excellent/Good/Developing/Needs Work
    detail: str                           # Human-readable explanation
    suggestion: Suggestion | None = None
    applicable: bool = True               # False if dimension doesn't apply
    raw: Optional[dict[str, Any]] = None  # structured payload of input values used to compute this score

    @property
    def bar(self) -> str:
        return bar(self.score)


@dataclass
class JobFingerprint:
    """Complete proficiency fingerprint for a single job."""
    job_id: str
    user: str
    cluster: str | None = None
    partition: str | None = None
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        """Weighted average of applicable dimensions."""
        applicable = [d for d in self.dimensions.values() if d.applicable]
        if not applicable:
            return 0.0
        return sum(d.score for d in applicable) / len(applicable)

    @property
    def overall_level(self) -> str:
        return proficiency_level(self.overall)

    @property
    def needs_work(self) -> list[DimensionScore]:
        """Dimensions that need improvement, worst first."""
        return sorted(
            [d for d in self.dimensions.values()
             if d.applicable and d.score < 65],
            key=lambda d: d.score,
        )

    @property
    def strengths(self) -> list[DimensionScore]:
        """Dimensions showing good proficiency."""
        return [d for d in self.dimensions.values()
                if d.applicable and d.score >= 65]


# ── Scoring functions ────────────────────────────────────────────────

def score_cpu(job: dict, summary: dict) -> DimensionScore:
    """
    Score CPU efficiency.

    Measures how well the requested CPU cores were actually utilized.
    A job requesting 32 cores but averaging 5% CPU is wasting 95% of
    allocated compute — a common beginner mistake.

    Scoring:
        avg_cpu_percent ≥ 80  → Excellent (90-100)
        avg_cpu_percent ≥ 50  → Good (65-89)
        avg_cpu_percent ≥ 20  → Developing (40-64)
        avg_cpu_percent < 20  → Needs Work (0-39)
    """
    avg_cpu = summary.get("avg_cpu_percent") or summary.get("avg_cpu_pct") or 0
    req_cpus = job.get("req_cpus", 1)

    if avg_cpu is None or avg_cpu == 0:
        return DimensionScore(
            name="CPU Efficiency",
            score=50,
            level="Unknown",
            detail="No CPU utilization data available for this job.",
            applicable=False,
        )

    score = min(100, avg_cpu * 1.1)
    cores_used = max(1, round(avg_cpu / 100 * req_cpus))
    waste_pct = max(0, 100 - avg_cpu)

    if score >= 85:
        detail = (f"Strong CPU utilization at {avg_cpu:.0f}%. "
                  f"Effectively using {cores_used}/{req_cpus} cores.")
        suggestion = None
    elif score >= 65:
        detail = (f"Reasonable CPU utilization at {avg_cpu:.0f}%. "
                  f"Using ~{cores_used}/{req_cpus} requested cores.")
        if req_cpus > cores_used + 2:
            suggestion = Suggestion(
                directive="ntasks",
                suggested_value=max(1, cores_used + 1),
                current_value=req_cpus,
                actual_usage=cores_used,
                unit="cores",
                rationale=f"~{cores_used} of {req_cpus} cores active",
            )
        else:
            suggestion = None
    elif score >= 40:
        detail = (f"Low CPU utilization at {avg_cpu:.0f}% — "
                  f"only ~{cores_used}/{req_cpus} cores active. "
                  f"{waste_pct:.0f}% of allocated CPU was idle.")
        suggestion = Suggestion(
            directive="ntasks",
            suggested_value=max(1, cores_used + 1),
            current_value=req_cpus,
            actual_usage=cores_used,
            unit="cores",
            rationale=f"~{cores_used} of {req_cpus} cores active",
        )
    else:
        detail = (f"Very low CPU utilization at {avg_cpu:.0f}% — "
                  f"requested {req_cpus} cores but used ~{cores_used}. "
                  f"This wastes resources and may delay other users' jobs.")
        suggestion = Suggestion(
            directive="ntasks",
            suggested_value=max(1, cores_used),
            current_value=req_cpus,
            actual_usage=cores_used,
            unit="cores",
            rationale=(f"only {cores_used} of {req_cpus} cores used; "
                       f"if single-threaded, request 1 core"),
        )

    return DimensionScore(
        name="CPU Efficiency",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=suggestion,
    )


def score_memory(job: dict, summary: dict) -> DimensionScore:
    """
    Score memory utilization.

    Measures how well memory request matched actual peak usage.
    Memory is genuinely allocated by SLURM — over-request directly
    blocks other jobs from running on the node.

    Scoring:
        utilization 50-90%  → Excellent
        utilization 30-50%  → Good
        utilization 10-30%  → Developing
        utilization <10%    → Needs Work
        OUT_OF_MEMORY       → Needs Work (under-requested)
    """
    # Check for OOM failure first — under-request, not over-request
    job_state = job.get("state", "").upper()
    if job_state in ("OUT_OF_MEMORY", "OOM"):
        req_mem_mb_oom = job.get("req_mem_mb", 0) or 0
        req_mem_gb_oom = req_mem_mb_oom / 1024 if req_mem_mb_oom else 0
        peak_mem_gb_oom = (summary.get("peak_memory_gb")
                           or summary.get("peak_mem_gb")
                           or req_mem_gb_oom)
        suggested_mb_oom = round_memory_up(peak_mem_gb_oom * 1024 * 1.5)
        return DimensionScore(
            name="Memory Efficiency",
            score=15,
            level="Needs Work",
            detail=(f"Job failed with OUT_OF_MEMORY. Requested "
                    f"{req_mem_gb_oom:.0f}GB was insufficient. "
                    f"Your job needed more memory than allocated."),
            suggestion=Suggestion(
                directive="mem",
                suggested_value=suggested_mb_oom,
                current_value=req_mem_mb_oom,
                actual_usage=peak_mem_gb_oom * 1024,
                unit="MB",
                rationale="job hit OOM; increase request by 50%",
            ),
        )

    peak_mem_gb = summary.get("peak_memory_gb") or 0
    req_mem_mb = job.get("req_mem_mb", 0) or 0
    req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0

    if peak_mem_gb == 0 or req_mem_gb == 0:
        return DimensionScore(
            name="Memory Efficiency",
            score=50,
            level="Unknown",
            detail="No memory usage data available for this job.",
            applicable=False,
        )

    utilization = (peak_mem_gb / req_mem_gb) * 100
    waste_gb = max(0, req_mem_gb - peak_mem_gb)

    # Score formula
    if 50 <= utilization <= 90:
        score = 85 + (1 - abs(utilization - 70) / 20) * 15
    elif 90 < utilization <= 100:
        score = 80
    elif utilization > 100:
        score = 30
    elif 30 <= utilization < 50:
        score = 65 + (utilization - 30) * 1
    elif 10 <= utilization < 30:
        score = 40 + (utilization - 10) * 1.25
    else:
        score = max(5, utilization * 2.5)

    score = min(100, max(0, score))

    # Suggestion: peak × 2 buffer, rounded up to a SLURM-friendly value
    # (single-job scorer uses 2x; aggregator may override with p95 strategy)
    if score >= 85:
        detail = (f"Good memory sizing. Used {peak_mem_gb:.1f}GB of "
                  f"{req_mem_gb:.0f}GB requested ({utilization:.0f}% utilization).")
        suggestion = None
    elif score >= 65:
        detail = (f"Reasonable memory sizing. Used {peak_mem_gb:.1f}GB of "
                  f"{req_mem_gb:.0f}GB requested ({utilization:.0f}% utilization).")
        suggestion = None
    elif score >= 40:
        suggested_mb = round_memory_up(peak_mem_gb * 1024 * 2)  # 2x buffer
        detail = (f"Moderate memory over-request. Used {peak_mem_gb:.1f}GB "
                  f"of {req_mem_gb:.0f}GB requested. {waste_gb:.0f}GB unused.")
        suggestion = Suggestion(
            directive="mem",
            suggested_value=suggested_mb,
            current_value=req_mem_mb,
            actual_usage=peak_mem_gb * 1024,
            unit="MB",
            rationale=f"peak {peak_mem_gb:.1f}GB × 2x buffer",
        )
    else:
        suggested_mb = round_memory_up(peak_mem_gb * 1024 * 2)
        detail = (f"Requested {req_mem_gb:.0f}GB but peaked at "
                  f"{peak_mem_gb:.1f}GB ({utilization:.0f}% utilization). "
                  f"{waste_gb:.0f}GB was unused.")
        suggestion = Suggestion(
            directive="mem",
            suggested_value=suggested_mb,
            current_value=req_mem_mb,
            actual_usage=peak_mem_gb * 1024,
            unit="MB",
            rationale=("memory you don't use is unavailable to other jobs "
                       "on the node"),
        )

    return DimensionScore(
        name="Memory Efficiency",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=suggestion,
    )


def score_time(job: dict, summary: dict) -> DimensionScore:
    """
    Score walltime estimation accuracy.

    Over-requesting walltime delays your jobs in the queue (the backfill
    scheduler can't fit them into gaps) and reduces throughput for other
    users. Walltime isn't "compute time wasted" — it's queue-position waste.

    Scoring:
        Ratio 0.50-0.85  → Excellent (good estimate with buffer)
        Ratio 0.25-0.50  → Good
        Ratio 0.05-0.25  → Developing
        Ratio < 0.05     → Needs Work (massive over-estimate)
        TIMEOUT state    → Needs Work (under-estimated)
    """
    job_state = job.get("state", "").upper()

    if job_state == "TIMEOUT":
        runtime = job.get("runtime_seconds", 0)
        req_time = job.get("req_time_seconds", 0)
        suggested_sec = round_time_up(req_time * 1.5 if req_time else 7200)
        return DimensionScore(
            name="Time Estimation",
            score=20,
            level="Needs Work",
            detail=("Job was killed for exceeding walltime. "
                    "Your job needed more time than the requested limit."),
            suggestion=Suggestion(
                directive="time",
                suggested_value=suggested_sec,
                current_value=req_time,
                actual_usage=runtime,
                unit="seconds",
                rationale="hit walltime limit; increase by 50%",
            ),
        )

    runtime = job.get("runtime_seconds", 0)
    req_time = job.get("req_time_seconds", 0)
    if not runtime or not req_time:
        return DimensionScore(
            name="Time Estimation",
            score=50,
            level="Unknown",
            detail="No runtime data available.",
            applicable=False,
        )

    ratio = runtime / req_time

    if 0.50 <= ratio <= 0.85:
        score = 85 + (1 - abs(ratio - 0.67) / 0.18) * 15
    elif 0.85 < ratio <= 1.0:
        score = 75
    elif ratio > 1.0:
        score = 50
    elif 0.25 <= ratio < 0.50:
        score = 50 + (ratio - 0.25) * 140
    elif 0.05 <= ratio < 0.25:
        score = 25 + (ratio - 0.05) * 125
    else:
        score = max(5, ratio * 500)

    score = min(100, max(0, score))

    runtime_str = format_duration_human(runtime)
    req_str = format_duration_human(req_time)

    # Suggestion: runtime × 1.5 buffer, rounded up to SLURM-friendly value
    suggested_sec = round_time_up(runtime * 1.5)

    if score >= 85:
        detail = (f"Good time estimate. Job ran {runtime_str} of "
                  f"{req_str} requested ({ratio:.0%} utilization).")
        suggestion = None
    elif ratio > 0.95:
        detail = (f"Job ran {runtime_str} of {req_str} requested — "
                  f"very close to the limit. Risk of walltime kill.")
        suggestion = Suggestion(
            directive="time",
            suggested_value=suggested_sec,
            current_value=req_time,
            actual_usage=runtime,
            unit="seconds",
            rationale="add buffer to avoid walltime kill",
        )
    else:
        detail = (f"Job ran {runtime_str} but {req_str} was requested "
                  f"({ratio:.0%} used). Over-requesting walltime delays "
                  f"your jobs in the queue (backfill scheduler can't fit "
                  f"them into gaps).")
        suggestion = Suggestion(
            directive="time",
            suggested_value=suggested_sec,
            current_value=req_time,
            actual_usage=runtime,
            unit="seconds",
            rationale="runtime × 1.5 buffer",
        )

    return DimensionScore(
        name="Time Estimation",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=suggestion,
    )


def score_io(job: dict, summary: dict) -> DimensionScore:
    """
    Score I/O awareness.

    Heavy NFS use during a job is a known antipattern: NFS round-trip
    latency dominates many small reads/writes, slowing the job and
    saturating shared filesystem bandwidth for other users.

    Scoring:
        nfs_ratio < 0.10   → Excellent (used local scratch)
        nfs_ratio < 0.30   → Good
        nfs_ratio < 0.60   → Developing
        nfs_ratio >= 0.60  → Needs Work (heavy NFS use)
    """
    nfs_ratio = summary.get("nfs_ratio")
    total_io_gb = ((summary.get("total_nfs_read_gb") or 0) +
                   (summary.get("total_nfs_write_gb") or 0) +
                   (summary.get("total_local_read_gb") or 0) +
                   (summary.get("total_local_write_gb") or 0))

    if nfs_ratio is None or total_io_gb < 0.1:
        return DimensionScore(
            name="I/O Awareness",
            score=80,
            level="Good",
            detail="Job had minimal I/O activity.",
            applicable=True,
        )

    if nfs_ratio < 0.10:
        score = 85 + (0.10 - nfs_ratio) * 150
    elif nfs_ratio < 0.30:
        score = 65 + (0.30 - nfs_ratio) * 100
    elif nfs_ratio < 0.60:
        score = 40 + (0.60 - nfs_ratio) * 83
    else:
        score = max(5, 40 - (nfs_ratio - 0.60) * 80)

    score = min(100, max(0, score))

    if score >= 85:
        detail = (f"Good I/O pattern: {(1 - nfs_ratio) * 100:.0f}% on "
                  f"local storage. Total I/O: {total_io_gb:.1f}GB.")
        suggestion = None
    elif score >= 40:
        detail = (f"Moderate NFS use: {nfs_ratio * 100:.0f}% of I/O "
                  f"hit shared storage. Consider using $LOCAL_SCRATCH "
                  f"for intermediate files.")
        suggestion = None  # I/O recommendations are pattern-based, not numeric
    else:
        detail = (f"Heavy NFS use: {nfs_ratio * 100:.0f}% of I/O hit "
                  f"shared storage. This slows your job and impacts "
                  f"others. Use $LOCAL_SCRATCH for intermediate data.")
        suggestion = None

    return DimensionScore(
        name="I/O Awareness",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=suggestion,
    )


def score_gpu(job: dict, summary: dict) -> DimensionScore:
    """
    Score GPU utilization.

    GPUs are scarce and expensive. Requesting GPUs without using them
    is the single most wasteful HPC pattern.

    Scoring (only applicable to GPU jobs):
        avg_gpu_util ≥ 70  → Excellent
        avg_gpu_util ≥ 40  → Good
        avg_gpu_util ≥ 15  → Developing
        avg_gpu_util < 15  → Needs Work
    """
    used_gpu = summary.get("used_gpu") or 0
    req_gpus = job.get("req_gpus", 0) or 0

    if req_gpus == 0:
        return DimensionScore(
            name="GPU Utilization",
            score=0,
            level="N/A",
            detail="Job did not request GPUs.",
            applicable=False,
        )

    if not used_gpu:
        return DimensionScore(
            name="GPU Utilization",
            score=10,
            level="Needs Work",
            detail=(f"Requested {req_gpus} GPU(s) but GPU was never "
                    f"utilized. GPU nodes are a scarce, expensive resource."),
            suggestion=Suggestion(
                directive="gres",
                suggested_value=0,
                current_value=req_gpus,
                actual_usage=0,
                unit="GPUs",
                rationale="job did not use GPUs; remove --gres or run on CPU partition",
            ),
        )

    # Graded scoring from real DCGM utilization (avg_gpu_util = mean
    # real_util_pct over the job's node+window). None means no DCGM data was
    # available for this job's node/window — we can't grade honestly, so fall
    # back to the binary "activity detected" signal rather than inventing a
    # number.
    avg_util = summary.get("avg_gpu_util")
    if avg_util is None:
        return DimensionScore(
            name="GPU Utilization",
            score=70,
            level="Good",
            detail=(f"GPU activity detected on {req_gpus} requested GPU(s). "
                    f"(No DCGM utilization data for this job's node/window, "
                    f"so efficiency can't be graded.)"),
            suggestion=None,
        )

    # Grade against the DCGM real utilization. Thresholds per the dimension's
    # design: >=70 Excellent, >=40 Good, >=15 Developing, <15 Needs Work.
    if avg_util >= 70:
        return DimensionScore(
            name="GPU Utilization",
            score=95,
            level="Excellent",
            detail=(f"Strong GPU utilization: {avg_util:.0f}% average real "
                    f"activity across {req_gpus} GPU(s)."),
            suggestion=None,
        )
    if avg_util >= 40:
        return DimensionScore(
            name="GPU Utilization",
            score=70,
            level="Good",
            detail=(f"Reasonable GPU utilization: {avg_util:.0f}% average real "
                    f"activity across {req_gpus} GPU(s)."),
            suggestion=None,
        )
    if avg_util >= 15:
        return DimensionScore(
            name="GPU Utilization",
            score=45,
            level="Developing",
            detail=(f"Low GPU utilization: {avg_util:.0f}% average real "
                    f"activity across {req_gpus} GPU(s). The GPUs are only "
                    f"partly used — consider whether fewer GPUs or a larger "
                    f"batch/model would use them more fully."),
            suggestion=Suggestion(
                directive="gres",
                suggested_value=max(1, req_gpus - 1),
                current_value=req_gpus,
                actual_usage=round(avg_util, 1),
                unit="GPUs",
                rationale=(f"GPUs averaged {avg_util:.0f}% real activity; "
                           f"fewer GPUs or more work per GPU improves efficiency"),
            ),
        )
    return DimensionScore(
        name="GPU Utilization",
        score=15,
        level="Needs Work",
        detail=(f"Very low GPU utilization: {avg_util:.0f}% average real "
                f"activity across {req_gpus} requested GPU(s). GPU nodes are a "
                f"scarce, expensive resource; near-idle GPUs block other users."),
        suggestion=Suggestion(
            directive="gres",
            suggested_value=0,
            current_value=req_gpus,
            actual_usage=round(avg_util, 1),
            unit="GPUs",
            rationale=(f"GPUs averaged only {avg_util:.0f}% real activity; "
                       f"consider running on a CPU partition or debugging why "
                       f"the GPUs sit idle"),
        ),
    )


# ── Top-level scorer ─────────────────────────────────────────────────


# ── Workstation scoring ──────────────────────────────────────────────
#
# Parallel to job scoring above. A "session" is to a workstation what a
# "job" is to a cluster: a bounded unit of use we can score for health.
# Identified by (hostname, username, session_epoch) in
# workstation_user_snapshot.
#
# Dimensions describe session health (memory pressure, duration fit).
# Cross-cutting verdicts like "you should be on the cluster" are NOT
# dimensions — they live in insights.py and are derived from the
# co-occurrence of these dimensional findings. See B2 decision in
# v1.7.0 design notes.


# Smallest-fit cluster memory tiers (MB). Sourced from node_state on
# spydur as of 2026-05. Ideally pulled live at runtime; constant here
# keeps the scorer DB-free for testability.
SPYDUR_MEMORY_TIERS_MB: tuple[int, ...] = (384_000, 768_000, 1_536_000)


@dataclass
class SessionFingerprint:
    """Complete proficiency fingerprint for a single workstation session."""
    username: str
    hostname: str
    session_epoch: int
    dimensions: dict[str, DimensionScore] = field(default_factory=dict)

    @property
    def overall(self) -> float:
        """Weighted average of applicable dimensions."""
        applicable = [d for d in self.dimensions.values() if d.applicable]
        if not applicable:
            return 0.0
        return sum(d.score for d in applicable) / len(applicable)

    @property
    def overall_level(self) -> str:
        return proficiency_level(self.overall)

    @property
    def needs_work(self) -> list[DimensionScore]:
        """Dimensions that need improvement, worst first."""
        return sorted(
            [d for d in self.dimensions.values()
             if d.applicable and d.score < 65],
            key=lambda d: d.score,
        )

    @property
    def strengths(self) -> list[DimensionScore]:
        """Dimensions showing good proficiency."""
        return [d for d in self.dimensions.values()
                if d.applicable and d.score >= 65]


def score_memory_pressure(session: dict, host_state: dict) -> DimensionScore:
    """
    Score memory pressure on a workstation session.

    Unlike cluster score_memory (where low utilization is wasteful), here
    high utilization is the bad signal: a workstation is shared with
    other users and the OS, and a session consuming most of host RAM
    starves everything else.

    Scoring (peak / host_total):
        <40%   → Excellent (plenty of headroom)
        40-65% → Good
        65-85% → Developing (noticeable pressure)
        >85%   → Needs Work (sustained near-saturation)
    """
    peak_bytes = session.get("peak_memory_bytes") or 0
    host_mb = host_state.get("memory_total_mb") or 0

    if peak_bytes <= 0 or host_mb <= 0:
        return DimensionScore(
            name="Memory Pressure",
            score=50,
            level="Unknown",
            detail="No memory snapshot data available for this session.",
            applicable=False,
            raw={
                "peak_bytes": peak_bytes,
                "host_total_bytes": host_mb * 1024 * 1024,
                "pressure_ratio": 0.0,
            },
        )

    peak_mb = peak_bytes / 1024 / 1024
    pressure = peak_mb / host_mb  # 0..1+ (can exceed 1.0 if cgroup peak
                                  # includes cache/buffers beyond RSS)
    pressure_pct = pressure * 100

    # Score: inverse of pressure, clamped 0..100.
    # 0% pressure → 100; 100% pressure → 0; linear between.
    score = max(0, min(100, 100 * (1 - pressure)))

    peak_gb = peak_mb / 1024
    host_gb = host_mb / 1024

    if score >= 85:
        detail = (f"Comfortable headroom. Peaked at {peak_gb:.1f}GB of "
                  f"{host_gb:.0f}GB host RAM ({pressure_pct:.0f}%).")
    elif score >= 65:
        detail = (f"Moderate memory use. Peaked at {peak_gb:.1f}GB of "
                  f"{host_gb:.0f}GB host RAM ({pressure_pct:.0f}%).")
    elif score >= 40:
        detail = (f"Noticeable memory pressure. Peaked at {peak_gb:.1f}GB "
                  f"of {host_gb:.0f}GB host RAM ({pressure_pct:.0f}%); "
                  f"other users on this host have less to work with.")
    else:
        detail = (f"Sustained memory pressure. Peaked at {peak_gb:.1f}GB "
                  f"of {host_gb:.0f}GB host RAM ({pressure_pct:.0f}%). "
                  f"At this level the workstation is effectively yours; "
                  f"other users will see slowdowns.")

    # No Suggestion attached: the actionable recommendation
    # ("move to spydur") is a cross-cutting verdict produced in
    # insights.py, not a per-dimension tuning suggestion.
    return DimensionScore(
        name="Memory Pressure",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=None,
        raw={
            "peak_bytes": peak_bytes,
            "host_total_bytes": host_mb * 1024 * 1024,
            "pressure_ratio": pressure,
        },
    )


def score_duration_fit(session: dict, host_state: dict) -> DimensionScore:
    """
    Score whether a session's duration fits its substrate.

    Short sessions are always fine; long sessions on a workstation are
    a smell even when memory is comfortable, because they tie up an
    interactive resource that should be available to other users.

    Piecewise score on span_hours:
        ≤2h   → 100
        2-6h  → linear taper 100 → 60
        6-18h → linear taper 60 → 30
        ≥18h  → 10 (floor)
    """
    span_hours = session.get("span_hours")
    if span_hours is None or span_hours < 0:
        return DimensionScore(
            name="Session Duration",
            score=50,
            level="Unknown",
            detail="No session duration data available.",
            applicable=False,
            raw={
                "span_hours": span_hours if span_hours is not None else 0.0,
                "span_seconds": (span_hours * 3600) if (span_hours is not None and span_hours >= 0) else 0.0,
            },
        )

    if span_hours <= 2:
        score = 100.0
    elif span_hours <= 6:
        # 2h → 100, 6h → 60
        score = 100 - (span_hours - 2) * (40 / 4)
    elif span_hours <= 18:
        # 6h → 60, 18h → 30
        score = 60 - (span_hours - 6) * (30 / 12)
    else:
        score = 10.0

    if score >= 85:
        detail = (f"Session ran for {span_hours:.1f}h — typical "
                  f"interactive use.")
    elif score >= 65:
        detail = (f"Session ran for {span_hours:.1f}h — extended but "
                  f"reasonable for a workstation.")
    elif score >= 40:
        detail = (f"Session ran for {span_hours:.1f}h. Long enough that "
                  f"a batch job on the cluster would have been a "
                  f"reasonable alternative.")
    else:
        detail = (f"Session ran for {span_hours:.1f}h. Workloads this "
                  f"long are what the cluster is for: jobs survive "
                  f"disconnects, reboots, and competing users.")

    return DimensionScore(
        name="Session Duration",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=None,
        raw={
            "span_hours": span_hours,
            "span_seconds": span_hours * 3600,
        },
    )


def score_session(session: dict, host_state: dict) -> SessionFingerprint:
    """
    Build a complete fingerprint for one workstation session.

    Parallel to score_job. Takes the session record (one row from
    workstation_user_snapshot, augmented with derived span_hours from
    grouping by session_epoch) and the workstation_state snapshot for
    its host.
    """
    return SessionFingerprint(
        username=session.get("username", ""),
        hostname=session.get("hostname", ""),
        session_epoch=session.get("session_epoch", 0),
        dimensions={
            "memory_pressure": score_memory_pressure(session, host_state),
            "duration_fit":    score_duration_fit(session, host_state),
        },
    )


def select_cluster_target(
    peak_mb: float,
    host_mb: int,
    tiers: tuple[int, ...] = SPYDUR_MEMORY_TIERS_MB,
) -> tuple[int, float] | None:
    """
    Pick the smallest cluster memory tier that gives meaningful headroom.

    Rule: smallest tier that is BOTH (a) ≥ peak_mb × 1.5 and (b) ≥ 2× host
    workstation RAM. The 2× host rule prevents recommending a near-equal
    box ("move from 256GB to 384GB" is not a real upgrade).

    Returns (target_mb, ratio_vs_host) or None if no tier qualifies.
    """
    if peak_mb <= 0 or host_mb <= 0:
        return None
    needed = peak_mb * 1.5
    min_meaningful = host_mb * 2
    threshold = max(needed, min_meaningful)
    for tier in sorted(tiers):
        if tier >= threshold:
            return tier, tier / host_mb
    return None

def score_job(job: dict, summary: dict) -> JobFingerprint:
    """Score a job across all five dimensions."""
    fp = JobFingerprint(
        job_id=job.get("job_id", "unknown"),
        user=job.get("user_name", "unknown"),
        cluster=job.get("cluster"),
        partition=job.get("partition"),
    )
    fp.dimensions["cpu"] = score_cpu(job, summary)
    fp.dimensions["memory"] = score_memory(job, summary)
    fp.dimensions["time"] = score_time(job, summary)
    fp.dimensions["io"] = score_io(job, summary)
    fp.dimensions["gpu"] = score_gpu(job, summary)
    return fp
