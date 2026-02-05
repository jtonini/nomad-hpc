"""
NØMADE Edu — Job Explanation Engine

Translates raw HPC job data into plain-language educational feedback.
This is the atomic unit of the edu module — every other feature
(reports, dashboard, templates) builds on the ability to analyze
and explain a single job.

Usage:
    nomade edu explain <job_id>
    nomade edu explain <job_id> --json
    nomade edu explain <job_id> --no-progress
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

from nomade.edu.scoring import (
    JobFingerprint,
    bar,
    proficiency_level,
    score_job,
)

logger = logging.getLogger(__name__)


# ── Colors (ANSI) ───────────────────────────────────────────────────

class C:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    WHITE = "\033[37m"

    @staticmethod
    def score_color(score: float) -> str:
        if score >= 85:
            return C.GREEN
        if score >= 65:
            return C.CYAN
        if score >= 40:
            return C.YELLOW
        return C.RED

    @staticmethod
    def level_color(level: str) -> str:
        colors = {
            "Excellent": C.GREEN,
            "Good": C.CYAN,
            "Developing": C.YELLOW,
            "Needs Work": C.RED,
        }
        return colors.get(level, C.WHITE)


# ── Database queries ─────────────────────────────────────────────────

def load_job(db_path: str, job_id: str) -> Optional[dict]:
    """Load a job from the database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error loading job {job_id}: {e}")
        return None


def load_summary(db_path: str, job_id: str) -> Optional[dict]:
    """Load a job summary from the database."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM job_summary WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"Error loading job summary {job_id}: {e}")
        return None


def load_user_history(db_path: str, user: str, limit: int = 50) -> list[dict]:
    """Load recent jobs for a user (for progress tracking)."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT j.*, js.peak_cpu_percent, js.peak_memory_gb,
                   js.avg_cpu_percent, js.avg_memory_gb, js.avg_io_wait_percent,
                   js.total_nfs_read_gb, js.total_nfs_write_gb,
                   js.total_local_read_gb, js.total_local_write_gb,
                   js.nfs_ratio, js.used_gpu, js.health_score
            FROM jobs j
            LEFT JOIN job_summary js ON j.job_id = js.job_id
            WHERE j.user_name = ?
              AND j.state IN ('COMPLETED', 'FAILED', 'TIMEOUT')
            ORDER BY j.end_time DESC
            LIMIT ?
        """, (user, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error loading user history: {e}")
        return []


# ── Progress calculation ─────────────────────────────────────────────

def compute_progress(db_path: str, user: str, current_fp: JobFingerprint) -> dict:
    """
    Compare current job's scores against the user's recent history.

    Returns dict of dimension_name -> {previous_avg, current, trend}
    """
    history = load_user_history(db_path, user, limit=30)

    if len(history) < 3:
        return {}  # not enough data for trends

    # Score each historical job
    from nomade.edu.scoring import score_job as _score

    # Split the job and summary fields from the joined row
    job_fields = [
        "job_id", "user_name", "partition", "node_list", "job_name",
        "state", "exit_code", "exit_signal", "failure_reason",
        "submit_time", "start_time", "end_time", "req_cpus",
        "req_mem_mb", "req_gpus", "req_time_seconds",
        "runtime_seconds", "wait_time_seconds",
    ]
    summary_fields = [
        "peak_cpu_percent", "peak_memory_gb", "avg_cpu_percent",
        "avg_memory_gb", "avg_io_wait_percent", "total_nfs_read_gb",
        "total_nfs_write_gb", "total_local_read_gb",
        "total_local_write_gb", "nfs_ratio", "used_gpu", "health_score",
    ]

    historical_scores = {dim: [] for dim in current_fp.dimensions}

    for row in history[1:]:  # skip most recent (that's the current job)
        job_data = {k: row.get(k) for k in job_fields}
        summary_data = {k: row.get(k) for k in summary_fields}
        fp = _score(job_data, summary_data)

        for dim_name, dim_score in fp.dimensions.items():
            if dim_score.applicable:
                historical_scores[dim_name].append(dim_score.score)

    # Compute trends
    progress = {}
    for dim_name, scores in historical_scores.items():
        if len(scores) < 3:
            continue

        current_dim = current_fp.dimensions.get(dim_name)
        if not current_dim or not current_dim.applicable:
            continue

        prev_avg = sum(scores) / len(scores)
        current_score = current_dim.score
        delta = current_score - prev_avg

        if delta > 5:
            trend = "improving"
            symbol = "↑"
        elif delta < -5:
            trend = "declining"
            symbol = "↓"
        else:
            trend = "stable"
            symbol = "→"

        progress[dim_name] = {
            "previous_avg": round(prev_avg, 1),
            "current": round(current_score, 1),
            "delta": round(delta, 1),
            "trend": trend,
            "symbol": symbol,
        }

    return progress


# ── Formatters ───────────────────────────────────────────────────────

def fmt_time(seconds: int) -> str:
    """Format seconds as human-readable time."""
    if not seconds:
        return "—"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_terminal(
    job: dict,
    summary: dict,
    fingerprint: JobFingerprint,
    progress: dict,
) -> str:
    """
    Format a job explanation for terminal output.
    """
    lines = []
    c = C

    # ── Header ───────────────────────────────────────────────────
    state = job.get("state", "UNKNOWN")
    state_color = c.GREEN if state == "COMPLETED" else c.RED
    partition = job.get("partition", "—")
    node = job.get("node_list", "—")
    runtime = fmt_time(job.get("runtime_seconds", 0))
    req_time = fmt_time(job.get("req_time_seconds", 0))

    lines.append("")
    lines.append(f"  {c.BOLD}NØMADE Job Analysis{c.RESET} — {c.CYAN}{fingerprint.job_id}{c.RESET}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"  User: {c.BOLD}{fingerprint.user}{c.RESET}"
                 f"    Partition: {partition}"
                 f"    Node: {node}")
    lines.append(f"  State: {state_color}{state}{c.RESET}"
                 f"    Runtime: {runtime} / {req_time} requested")
    lines.append("")

    # ── Proficiency scores ───────────────────────────────────────
    lines.append(f"  {c.BOLD}Proficiency Scores{c.RESET}")
    lines.append(f"  {'─' * 56}")

    for dim in fingerprint.dimensions.values():
        if not dim.applicable:
            continue
        color = c.score_color(dim.score)
        lines.append(
            f"    {dim.name:<20s} {color}{dim.bar}{c.RESET}"
            f"  {color}{dim.score:>5.1f}%{c.RESET}"
            f"   {c.level_color(dim.level)}{dim.level}{c.RESET}"
        )

    lines.append(f"    {'─' * 52}")
    overall = fingerprint.overall
    oc = c.score_color(overall)
    lines.append(
        f"    {'Overall Score':<20s} {oc}{bar(overall)}{c.RESET}"
        f"  {oc}{overall:>5.1f}%{c.RESET}"
        f"   {oc}{fingerprint.overall_level}{c.RESET}"
    )
    lines.append("")

    # ── Recommendations ──────────────────────────────────────────
    needs_work = fingerprint.needs_work
    if needs_work:
        lines.append(f"  {c.BOLD}Recommendations{c.RESET}")
        lines.append(f"  {'─' * 56}")

        for dim in needs_work:
            color = c.score_color(dim.score)
            lines.append(f"    {color}{dim.name}{c.RESET}: {dim.detail}")
            if dim.suggestion:
                for sline in dim.suggestion.split("\n"):
                    lines.append(f"      {c.CYAN}{sline}{c.RESET}")
            lines.append("")
    else:
        lines.append(f"  {c.GREEN}All dimensions look good — nice work!{c.RESET}")
        lines.append("")

    # ── Progress ─────────────────────────────────────────────────
    if progress:
        lines.append(f"  {c.BOLD}Your Progress{c.RESET} (last 30 jobs)")
        lines.append(f"  {'─' * 56}")

        for dim_name, p in progress.items():
            dim = fingerprint.dimensions.get(dim_name)
            if not dim or not dim.applicable:
                continue

            trend_color = (c.GREEN if p["trend"] == "improving"
                           else c.RED if p["trend"] == "declining"
                           else c.DIM)
            lines.append(
                f"    {dim.name:<20s} "
                f"{p['previous_avg']:>5.1f}% → {p['current']:>5.1f}%  "
                f"{trend_color}{p['symbol']} {p['trend']}{c.RESET}"
            )
        lines.append("")

    return "\n".join(lines)


def format_json(
    job: dict,
    summary: dict,
    fingerprint: JobFingerprint,
    progress: dict,
) -> str:
    """Format as JSON for programmatic consumption."""
    result = {
        "job_id": fingerprint.job_id,
        "user": fingerprint.user,
        "state": job.get("state"),
        "partition": job.get("partition"),
        "overall_score": round(fingerprint.overall, 1),
        "overall_level": fingerprint.overall_level,
        "dimensions": {},
        "progress": progress,
    }

    for name, dim in fingerprint.dimensions.items():
        result["dimensions"][name] = {
            "score": dim.score,
            "level": dim.level,
            "applicable": dim.applicable,
            "detail": dim.detail,
            "suggestion": dim.suggestion,
        }

    return json.dumps(result, indent=2)


# ── Main entry point ─────────────────────────────────────────────────

def explain_job(
    job_id: str,
    db_path: str,
    show_progress: bool = True,
    output_format: str = "terminal",
) -> Optional[str]:
    """
    Generate a plain-language explanation of a job.

    Args:
        job_id:         SLURM job ID
        db_path:        Path to NOMADE database
        show_progress:  Include progress comparison with recent jobs
        output_format:  "terminal" (colored text) or "json"

    Returns:
        Formatted string, or None if job not found.
    """
    job = load_job(db_path, job_id)
    if not job:
        return None

    summary = load_summary(db_path, job_id)
    if not summary:
        summary = {}

    # Score the job
    fingerprint = score_job(job, summary)

    # Compute progress if requested
    progress = {}
    if show_progress:
        progress = compute_progress(db_path, fingerprint.user, fingerprint)

    # Format output
    if output_format == "json":
        return format_json(job, summary, fingerprint, progress)
    else:
        return format_terminal(job, summary, fingerprint, progress)
