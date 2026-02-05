"""
NØMADE Edu — Proficiency Scoring Engine

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

The scoring functions are intentionally separated so they can be reused by:
    - nomade edu explain  (single job analysis)
    - nomade edu report   (course/group aggregation)
    - dashboard edu tab   (visual proficiency tracking)
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


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class DimensionScore:
    """Score for a single proficiency dimension."""
    name: str
    score: float            # 0-100
    level: str              # Excellent/Good/Developing/Needs Work
    detail: str             # Human-readable explanation
    suggestion: str = ""    # Actionable fix (SLURM directive)
    applicable: bool = True # False if dimension doesn't apply to this job

    @property
    def bar(self) -> str:
        return bar(self.score)


@dataclass
class JobFingerprint:
    """Complete proficiency fingerprint for a single job."""
    job_id: str
    user: str
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
    peak_cpu = summary.get("peak_cpu_percent") or summary.get("peak_cpu_pct") or 0
    req_cpus = job.get("req_cpus", 1)

    if avg_cpu is None or avg_cpu == 0:
        # No CPU data — can't score
        return DimensionScore(
            name="CPU Efficiency",
            score=50,
            level="Unknown",
            detail="No CPU utilization data available for this job.",
            applicable=False,
        )

    # Score: map avg_cpu directly, with a small bonus for consistency
    # (peak close to average = steady, not bursty)
    score = min(100, avg_cpu * 1.1)  # slight generosity

    # Effective cores used
    cores_used = max(1, round(avg_cpu / 100 * req_cpus))
    waste_pct = max(0, 100 - avg_cpu)

    if score >= 85:
        detail = (f"Strong CPU utilization at {avg_cpu:.0f}%. "
                  f"Effectively using {cores_used}/{req_cpus} cores.")
        suggestion = ""
    elif score >= 65:
        detail = (f"Reasonable CPU utilization at {avg_cpu:.0f}%. "
                  f"Using ~{cores_used}/{req_cpus} requested cores.")
        suggestion = (f"Consider: #SBATCH --ntasks={max(1, cores_used + 1)}"
                      if req_cpus > cores_used + 2 else "")
    elif score >= 40:
        detail = (f"Low CPU utilization at {avg_cpu:.0f}% — "
                  f"only ~{cores_used}/{req_cpus} cores active. "
                  f"{waste_pct:.0f}% of allocated CPU was idle.")
        suggestion = f"Try: #SBATCH --ntasks={max(1, cores_used + 1)}"
    else:
        detail = (f"Very low CPU utilization at {avg_cpu:.0f}% — "
                  f"requested {req_cpus} cores but used ~{cores_used}. "
                  f"This wastes resources and may delay other users' jobs.")
        suggestion = (f"Try: #SBATCH --ntasks={max(1, cores_used)}\n"
                      f"    If your code is single-threaded, request 1 core.")

    return DimensionScore(
        name="CPU Efficiency",
        score=round(score, 1),
        level=proficiency_level(score),
        detail=detail,
        suggestion=suggestion,
    )


def score_memory(job: dict, summary: dict) -> DimensionScore:
    """
    Score memory efficiency.

    Compares peak memory usage against requested memory.
    Over-requesting memory is the most common resource waste on teaching
    clusters — students copy example scripts with --mem=64G for jobs
    that use 2GB.

    Scoring:
        Utilization 60-95%  → Excellent (good sizing with safety margin)
        Utilization 30-60%  → Good
        Utilization 10-30%  → Developing
        Utilization < 10%   → Needs Work (massive over-request)
        Utilization > 95%   → slight penalty (risk of OOM)
        OUT_OF_MEMORY state → Needs Work (under-requested)
    """
    # Check for OOM failure first
    job_state = job.get("state", "").upper()
    if job_state in ("OUT_OF_MEMORY", "OOM"):
        req_mem_mb = job.get("req_mem_mb", 0)
        req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0
        peak_mem_gb = summary.get("peak_memory_gb") or summary.get("peak_mem_gb") or req_mem_gb
        # Suggest 50% more memory
        suggested_gb = max(1, round(peak_mem_gb * 1.5))
        return DimensionScore(
            name="Memory Efficiency",
            score=15,
            level="Needs Work",
            detail=(f"Job failed with OUT_OF_MEMORY. Requested {req_mem_gb:.0f}GB "
                    f"was insufficient. Your job needed more memory than allocated."),
            suggestion=f"Try: #SBATCH --mem={suggested_gb}G  (increase memory request)",
        )
    
    peak_mem_gb = summary.get("peak_memory_gb") or summary.get("peak_mem_gb") or 0
    req_mem_mb = job.get("req_mem_mb", 0)
    req_mem_gb = req_mem_mb / 1024 if req_mem_mb else 0

    if peak_mem_gb == 0 or req_mem_gb == 0:
        return DimensionScore(
            name="Memory Efficiency",
            score=50,
            level="Unknown",
            detail="No memory utilization data available.",
            applicable=False,
        )

    utilization = (peak_mem_gb / req_mem_gb) * 100

    # Ideal zone: 50-90% utilization (safety margin without waste)
    if 50 <= utilization <= 90:
        score = 85 + (1 - abs(utilization - 70) / 20) * 15  # peak at 70%
    elif 90 < utilization <= 100:
        score = 75  # tight but ok
    elif utilization > 100:
        score = 60  # went over — risky
    elif 30 <= utilization < 50:
        score = 55 + (utilization - 30) * 1.5
    elif 10 <= utilization < 30:
        score = 25 + (utilization - 10) * 1.5
    else:
        score = max(5, utilization * 2.5)

    score = min(100, max(0, score))
    waste_gb = max(0, req_mem_gb - peak_mem_gb)

    # Suggested memory: peak + 20% buffer, rounded up to nearest GB
    suggested_gb = max(1, round(peak_mem_gb * 1.2 + 0.5))
    suggested_mb = suggested_gb * 1024

    if utilization >= 50 and utilization <= 90:
        detail = (f"Well-sized memory request. Used {peak_mem_gb:.1f}GB of "
                  f"{req_mem_gb:.0f}GB ({utilization:.0f}% utilization).")
        suggestion = ""
    elif utilization > 90:
        detail = (f"Memory usage very close to limit — {peak_mem_gb:.1f}GB of "
                  f"{req_mem_gb:.0f}GB ({utilization:.0f}%). Risk of out-of-memory.")
        suggestion = f"Try: #SBATCH --mem={suggested_gb + 2}G  (add safety margin)"
    else:
        detail = (f"Requested {req_mem_gb:.0f}GB but peaked at {peak_mem_gb:.1f}GB "
                  f"({utilization:.0f}% utilization). "
                  f"{waste_gb:.0f}GB was unused.")
        suggestion = f"Try: #SBATCH --mem={suggested_gb}G"

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

    Students often copy --time=48:00:00 from examples regardless of
    actual runtime. This hurts scheduler efficiency (backfill can't
    use the slot) and increases wait times for everyone.

    Scoring:
        Ratio 0.50-0.85  → Excellent (good estimate with buffer)
        Ratio 0.25-0.50  → Good
        Ratio 0.05-0.25  → Developing
        Ratio < 0.05     → Needs Work (massive over-estimate)
        TIMEOUT state    → Needs Work (under-estimated)
    """
    # Check for TIMEOUT failure first
    job_state = job.get("state", "").upper()
    if job_state == "TIMEOUT":
        runtime = job.get("runtime_seconds", 0)
        req_time = job.get("req_time_seconds", 0)
        # Suggest 50% more time
        suggested_sec = int(req_time * 1.5) if req_time else 7200
        suggested_h = suggested_sec // 3600
        suggested_m = (suggested_sec % 3600) // 60
        suggested_str = f"{suggested_h}:{suggested_m:02d}:00"
        return DimensionScore(
            name="Time Estimation",
            score=20,
            level="Needs Work",
            detail=(f"Job was killed for exceeding walltime. "
                    f"Your job needed more time than the requested limit."),
            suggestion=f"Try: #SBATCH --time={suggested_str}  (increase time request)",
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

    # Ideal: 50-85% of requested time
    if 0.50 <= ratio <= 0.85:
        score = 85 + (1 - abs(ratio - 0.67) / 0.18) * 15
    elif 0.85 < ratio <= 1.0:
        score = 75  # cutting it close
    elif ratio > 1.0:
        score = 50  # hit the wall — job may have been killed
    elif 0.25 <= ratio < 0.50:
        score = 50 + (ratio - 0.25) * 140
    elif 0.05 <= ratio < 0.25:
        score = 25 + (ratio - 0.05) * 125
    else:
        score = max(5, ratio * 500)

    score = min(100, max(0, score))

    # Format times for display
    def fmt_time(seconds):
        h, m = divmod(int(seconds), 3600)
        m, s = divmod(m, 60)
        if h > 0:
            return f"{h}h {m:02d}m"
        return f"{m}m {s:02d}s"

    runtime_str = fmt_time(runtime)
    req_str = fmt_time(req_time)

    # Suggested time: runtime + 50% buffer, rounded up
    suggested_sec = int(runtime * 1.5)
    suggested_h = suggested_sec // 3600
    suggested_m = (suggested_sec % 3600) // 60
    if suggested_h > 0:
        suggested_str = f"{suggested_h}:{suggested_m:02d}:00"
    else:
        suggested_str = f"0:{suggested_m:02d}:00"

    if score >= 85:
        detail = (f"Good time estimate. Job ran {runtime_str} of "
                  f"{req_str} requested ({ratio:.0%} utilization).")
        suggestion = ""
    elif ratio > 0.95:
        detail = (f"Job ran {runtime_str} of {req_str} requested — "
                  f"very close to the limit. Risk of walltime kill.")
        suggestion = f"Try: #SBATCH --time={suggested_str}  (add buffer)"
    else:
        detail = (f"Job ran {runtime_str} but {req_str} was requested "
                  f"({ratio:.0%} used). Over-estimating walltime "
                  f"reduces scheduler efficiency for everyone.")
        suggestion = f"Try: #SBATCH --time={suggested_str}"

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

    Measures whether the job used local scratch vs NFS appropriately.
    Heavy NFS writes are a common performance bottleneck and can
    affect other users sharing the network filesystem.

    Scoring based on nfs_ratio (0=all local, 1=all NFS):
        nfs_ratio < 0.3   → Excellent (using local scratch)
        nfs_ratio < 0.6   → Good
        nfs_ratio < 0.8   → Developing
        nfs_ratio >= 0.8  → Needs Work (all NFS, heavy I/O)
    """
    nfs_ratio = summary.get("nfs_ratio")
    nfs_write = summary.get("total_nfs_write_gb", 0) or 0
    local_write = summary.get("total_local_write_gb", 0) or 0
    total_write = nfs_write + local_write
    io_wait = summary.get("avg_io_wait_percent") or summary.get("avg_io_wait_pct") or 0

    if nfs_ratio is None and total_write == 0:
        return DimensionScore(
            name="I/O Awareness",
            score=50,
            level="Unknown",
            detail="No I/O data available for this job.",
            applicable=False,
        )

    # If very little I/O, don't penalize heavily
    if total_write < 0.1:  # less than 100MB total writes
        return DimensionScore(
            name="I/O Awareness",
            score=80,
            level="Good",
            detail=f"Minimal I/O ({total_write:.2f}GB total writes). Not a concern.",
            applicable=True,
        )

    if nfs_ratio is None:
        nfs_ratio = nfs_write / total_write if total_write > 0 else 0

    # Score: lower NFS ratio = better
    if nfs_ratio < 0.3:
        score = 90 + (0.3 - nfs_ratio) * 33
    elif nfs_ratio < 0.6:
        score = 65 + (0.6 - nfs_ratio) * 83
    elif nfs_ratio < 0.8:
        score = 40 + (0.8 - nfs_ratio) * 125
    else:
        score = max(10, 40 - (nfs_ratio - 0.8) * 150)

    score = min(100, max(0, score))

    # Add I/O wait penalty
    if io_wait > 20:
        score = max(10, score - (io_wait - 20))

    if score >= 85:
        detail = (f"Good I/O strategy. {local_write:.1f}GB local, "
                  f"{nfs_write:.1f}GB NFS ({nfs_ratio:.0%} network).")
        suggestion = ""
    elif nfs_write > 1.0:
        detail = (f"{nfs_write:.1f}GB written to NFS ({nfs_ratio:.0%} of I/O). "
                  f"Heavy network writes slow your job and affect others.")
        suggestion = ("Use local scratch: cp data $TMPDIR/ before processing,\n"
                      "    then cp results back to $HOME when done.")
    else:
        detail = (f"NFS ratio: {nfs_ratio:.0%}. "
                  f"Consider local scratch for better performance.")
        suggestion = "Use $TMPDIR for temporary files during computation."

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

    Checks whether requested GPU resources were actually used.
    GPU nodes are expensive and scarce — requesting a GPU and not
    using it blocks other users who need it.

    Currently binary (used/not used). Future: actual GPU utilization %.
    """
    req_gpus = job.get("req_gpus", 0)

    if not req_gpus:
        return DimensionScore(
            name="GPU Utilization",
            score=0,
            level="N/A",
            detail="No GPU requested — not applicable.",
            applicable=False,
        )

    used_gpu = summary.get("used_gpu", 0)

    if used_gpu:
        return DimensionScore(
            name="GPU Utilization",
            score=85,
            level="Good",
            detail=(f"GPU was requested and used. "
                    f"({req_gpus} GPU{'s' if req_gpus > 1 else ''} allocated.)"),
            suggestion="",
        )
    else:
        return DimensionScore(
            name="GPU Utilization",
            score=10,
            level="Needs Work",
            detail=(f"Requested {req_gpus} GPU{'s' if req_gpus > 1 else ''} "
                    f"but GPU was never utilized. GPU nodes are a scarce, "
                    f"expensive resource."),
            suggestion=("If your code doesn't use GPU, submit to a CPU partition:\n"
                        "    #SBATCH --partition=compute\n"
                        "    (remove --gres=gpu:N)"),
        )


# ── Main scoring function ───────────────────────────────────────────

def score_job(job: dict, summary: dict) -> JobFingerprint:
    """
    Score a job across all proficiency dimensions.

    Args:
        job:     Row from the jobs table (dict with req_cpus, req_mem_mb, etc.)
        summary: Row from the job_summary table (dict with peak_cpu_percent, etc.)

    Returns:
        JobFingerprint with scores for each dimension.
    """
    job_id = job.get("job_id", "unknown")
    user = job.get("user_name", "unknown")

    fp = JobFingerprint(job_id=job_id, user=user)
    fp.dimensions["cpu"] = score_cpu(job, summary)
    fp.dimensions["memory"] = score_memory(job, summary)
    fp.dimensions["time"] = score_time(job, summary)
    fp.dimensions["io"] = score_io(job, summary)
    fp.dimensions["gpu"] = score_gpu(job, summary)

    return fp
