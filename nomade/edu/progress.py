"""
NØMADE Edu — Proficiency Trajectory Tracking

Measures the development of computational proficiency over time.
This is the core educational insight that distinguishes NØMADE from
traditional HPC monitoring: not just "what did they use" but
"are they getting better at using it?"

Key outputs:
    - user_trajectory:  per-user proficiency over time
    - group_summary:    course/lab aggregate with improvement metrics
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from nomade.edu.scoring import (
    JobFingerprint,
    DimensionScore,
    proficiency_level,
    score_job,
    bar,
)

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class WindowStats:
    """Proficiency stats for a time window."""
    start: str
    end: str
    job_count: int
    scores: dict[str, float]  # dimension -> avg score
    overall: float


@dataclass
class UserTrajectory:
    """A user's proficiency development over time."""
    username: str
    total_jobs: int
    date_range: tuple[str, str]  # (earliest, latest)
    windows: list[WindowStats]   # chronological time windows
    current_scores: dict[str, float]
    improvement: dict[str, float]  # dimension -> delta (first window vs last)
    overall_improvement: float

    @property
    def is_improving(self) -> bool:
        return self.overall_improvement > 5

    @property
    def summary(self) -> str:
        """One-line summary of trajectory."""
        if self.total_jobs < 3:
            return "Too few jobs to assess trajectory."
        if self.overall_improvement > 15:
            return f"Strong improvement (+{self.overall_improvement:.0f}% overall)"
        elif self.overall_improvement > 5:
            return f"Improving (+{self.overall_improvement:.0f}% overall)"
        elif self.overall_improvement > -5:
            return "Stable proficiency"
        else:
            return f"Declining ({self.overall_improvement:.0f}% overall)"


@dataclass
class GroupSummary:
    """Aggregate proficiency data for a course or lab group."""
    group_name: str
    member_count: int
    total_jobs: int
    date_range: tuple[str, str]
    users: list[UserTrajectory]

    # Aggregate metrics
    avg_overall: float
    avg_improvement: float
    users_improving: int
    users_declining: int
    users_stable: int

    # Per-dimension aggregates
    dimension_avgs: dict[str, float]
    dimension_improvements: dict[str, float]

    # Common issues across the group
    weakest_dimension: str
    strongest_dimension: str

    @property
    def improvement_rate(self) -> str:
        """e.g., '15/20 students improved memory efficiency'"""
        return (f"{self.users_improving}/{self.member_count} "
                f"{'students' if self.member_count > 1 else 'student'} "
                f"improved overall proficiency")


# ── Core functions ───────────────────────────────────────────────────

def _load_user_jobs(
    db_path: str,
    username: str,
    days: int = 90,
) -> list[dict]:
    """Load a user's jobs with summaries for scoring."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT j.*, js.peak_cpu_percent, js.peak_memory_gb,
                   js.avg_cpu_percent, js.avg_memory_gb,
                   js.avg_io_wait_percent,
                   js.total_nfs_read_gb, js.total_nfs_write_gb,
                   js.total_local_read_gb, js.total_local_write_gb,
                   js.nfs_ratio, js.used_gpu, js.health_score
            FROM jobs j
            LEFT JOIN job_summary js ON j.job_id = js.job_id
            WHERE j.user_name = ?
              AND j.end_time >= ?
              AND j.state IN ('COMPLETED', 'FAILED', 'TIMEOUT')
            ORDER BY j.end_time ASC
        """, (username, cutoff)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error loading jobs for {username}: {e}")
        return []


def _split_job_fields(row: dict) -> tuple[dict, dict]:
    """Split a joined row into job fields and summary fields."""
    job_fields = {
        "job_id", "user_name", "partition", "node_list", "job_name",
        "state", "exit_code", "exit_signal", "failure_reason",
        "submit_time", "start_time", "end_time", "req_cpus",
        "req_mem_mb", "req_gpus", "req_time_seconds",
        "runtime_seconds", "wait_time_seconds",
    }
    job = {k: row.get(k) for k in job_fields}
    summary = {k: v for k, v in row.items() if k not in job_fields}
    return job, summary


def _score_jobs(rows: list[dict]) -> list[JobFingerprint]:
    """Score a list of joined job+summary rows."""
    fingerprints = []
    for row in rows:
        job, summary = _split_job_fields(row)
        try:
            fp = score_job(job, summary)
            fp._end_time = row.get("end_time", "")
            fingerprints.append(fp)
        except Exception as e:
            logger.debug(f"Could not score job {row.get('job_id')}: {e}")
    return fingerprints


def user_trajectory(
    db_path: str,
    username: str,
    days: int = 90,
    window_size: int = 7,
) -> Optional[UserTrajectory]:
    """
    Compute a user's proficiency trajectory.

    Divides the time range into windows and computes average scores
    per window, showing how proficiency develops over time.

    Args:
        db_path:      Path to NOMADE database
        username:      User to analyze
        days:          Lookback period in days
        window_size:   Days per window for averaging

    Returns:
        UserTrajectory or None if insufficient data.
    """
    rows = _load_user_jobs(db_path, username, days)
    if len(rows) < 3:
        return None

    fingerprints = _score_jobs(rows)
    if len(fingerprints) < 3:
        return None

    # Split into time windows
    start_date = datetime.now() - timedelta(days=days)
    windows = []
    current_window_start = start_date

    while current_window_start < datetime.now():
        window_end = current_window_start + timedelta(days=window_size)
        window_start_str = current_window_start.isoformat()
        window_end_str = window_end.isoformat()

        # Find fingerprints in this window
        window_fps = [
            fp for fp in fingerprints
            if hasattr(fp, "_end_time") and fp._end_time
            and window_start_str <= fp._end_time < window_end_str
        ]

        if window_fps:
            # Average scores per dimension
            dim_scores = defaultdict(list)
            for fp in window_fps:
                for name, dim in fp.dimensions.items():
                    if dim.applicable:
                        dim_scores[name].append(dim.score)

            avg_scores = {
                name: round(sum(s) / len(s), 1)
                for name, s in dim_scores.items()
                if s
            }
            overall = (sum(avg_scores.values()) / len(avg_scores)
                       if avg_scores else 0)

            windows.append(WindowStats(
                start=current_window_start.strftime("%Y-%m-%d"),
                end=window_end.strftime("%Y-%m-%d"),
                job_count=len(window_fps),
                scores=avg_scores,
                overall=round(overall, 1),
            ))

        current_window_start = window_end

    if len(windows) < 2:
        # Not enough windows for trajectory
        if windows:
            current = windows[-1].scores
            return UserTrajectory(
                username=username,
                total_jobs=len(fingerprints),
                date_range=(rows[0].get("end_time", ""), rows[-1].get("end_time", "")),
                windows=windows,
                current_scores=current,
                improvement={},
                overall_improvement=0,
            )
        return None

    # Compute improvement (first window vs last window)
    first = windows[0]
    last = windows[-1]
    improvement = {}
    all_dims = set(first.scores.keys()) | set(last.scores.keys())
    for dim in all_dims:
        if dim in first.scores and dim in last.scores:
            improvement[dim] = round(last.scores[dim] - first.scores[dim], 1)

    overall_imp = last.overall - first.overall

    return UserTrajectory(
        username=username,
        total_jobs=len(fingerprints),
        date_range=(rows[0].get("end_time", ""), rows[-1].get("end_time", "")),
        windows=windows,
        current_scores=last.scores,
        improvement=improvement,
        overall_improvement=round(overall_imp, 1),
    )


def group_summary(
    db_path: str,
    group_name: str,
    days: int = 90,
) -> Optional[GroupSummary]:
    """
    Compute aggregate proficiency for a course or lab group.

    This produces the key insight: "15/20 students improved memory
    efficiency over the semester."

    Args:
        db_path:     Path to NOMADE database
        group_name:  Linux group name (maps to course/lab)
        days:        Lookback period in days

    Returns:
        GroupSummary or None if group not found.
    """
    # Load group members
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        members = conn.execute("""
            SELECT DISTINCT username FROM group_membership
            WHERE group_name = ?
        """, (group_name,)).fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error loading group {group_name}: {e}")
        return None

    if not members:
        return None

    usernames = [m["username"] for m in members]

    # Compute trajectory for each user
    trajectories = []
    total_jobs = 0
    earliest = None
    latest = None

    for username in usernames:
        traj = user_trajectory(db_path, username, days)
        if traj:
            trajectories.append(traj)
            total_jobs += traj.total_jobs
            if traj.date_range[0]:
                if earliest is None or traj.date_range[0] < earliest:
                    earliest = traj.date_range[0]
            if traj.date_range[1]:
                if latest is None or traj.date_range[1] > latest:
                    latest = traj.date_range[1]

    if not trajectories:
        return None

    # Aggregate metrics
    improving = sum(1 for t in trajectories if t.overall_improvement > 5)
    declining = sum(1 for t in trajectories if t.overall_improvement < -5)
    stable = len(trajectories) - improving - declining

    # Average current scores and improvements per dimension
    dim_scores = defaultdict(list)
    dim_improvements = defaultdict(list)
    for traj in trajectories:
        for dim, score in traj.current_scores.items():
            dim_scores[dim].append(score)
        for dim, imp in traj.improvement.items():
            dim_improvements[dim].append(imp)

    dim_avgs = {
        d: round(sum(s) / len(s), 1)
        for d, s in dim_scores.items() if s
    }
    dim_imps = {
        d: round(sum(s) / len(s), 1)
        for d, s in dim_improvements.items() if s
    }

    # Find weakest and strongest
    weakest = min(dim_avgs, key=dim_avgs.get) if dim_avgs else "unknown"
    strongest = max(dim_avgs, key=dim_avgs.get) if dim_avgs else "unknown"

    avg_overall = (sum(t.current_scores.get("cpu", 0)
                       + t.current_scores.get("memory", 0)
                       + t.current_scores.get("time", 0)
                       for t in trajectories)
                   / (len(trajectories) * 3)) if trajectories else 0

    avg_improvement = (sum(t.overall_improvement for t in trajectories)
                       / len(trajectories)) if trajectories else 0

    return GroupSummary(
        group_name=group_name,
        member_count=len(usernames),
        total_jobs=total_jobs,
        date_range=(earliest or "", latest or ""),
        users=trajectories,
        avg_overall=round(avg_overall, 1),
        avg_improvement=round(avg_improvement, 1),
        users_improving=improving,
        users_declining=declining,
        users_stable=stable,
        dimension_avgs=dim_avgs,
        dimension_improvements=dim_imps,
        weakest_dimension=weakest,
        strongest_dimension=strongest,
    )


# ── Terminal formatters ──────────────────────────────────────────────

def format_trajectory(traj: UserTrajectory) -> str:
    """Format a user trajectory for terminal output."""
    from nomade.edu.explain import C

    lines = []
    lines.append("")
    lines.append(f"  {C.BOLD}NØMADE Proficiency Trajectory{C.RESET} — {C.CYAN}{traj.username}{C.RESET}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"  Jobs analyzed: {traj.total_jobs}    Period: {traj.date_range[0][:10]} → {traj.date_range[1][:10]}")
    lines.append(f"  {traj.summary}")
    lines.append("")

    if traj.windows:
        lines.append(f"  {C.BOLD}Score Progression{C.RESET}")
        lines.append(f"  {'─' * 56}")

        # Show each window
        for w in traj.windows:
            overall_color = C.score_color(w.overall)
            lines.append(
                f"    {w.start}  "
                f"{overall_color}{bar(w.overall, 8)}{C.RESET} "
                f"{overall_color}{w.overall:>5.1f}%{C.RESET}  "
                f"({w.job_count} jobs)"
            )
        lines.append("")

    if traj.improvement:
        lines.append(f"  {C.BOLD}Dimension Changes{C.RESET}")
        lines.append(f"  {'─' * 56}")
        for dim, delta in sorted(traj.improvement.items(), key=lambda x: -x[1]):
            current = traj.current_scores.get(dim, 0)
            color = C.GREEN if delta > 5 else C.RED if delta < -5 else C.DIM
            symbol = "↑" if delta > 5 else "↓" if delta < -5 else "→"
            dim_label = {
                "cpu": "CPU Efficiency",
                "memory": "Memory Efficiency",
                "time": "Time Estimation",
                "io": "I/O Awareness",
                "gpu": "GPU Utilization",
            }.get(dim, dim)
            lines.append(
                f"    {dim_label:<20s} "
                f"{current:>5.1f}%  "
                f"{color}{symbol} {delta:+.1f}%{C.RESET}"
            )
        lines.append("")

    return "\n".join(lines)


def format_group_summary(gs: GroupSummary) -> str:
    """Format a group summary for terminal output."""
    from nomade.edu.explain import C

    lines = []
    lines.append("")
    lines.append(f"  {C.BOLD}NØMADE Group Report{C.RESET} — {C.CYAN}{gs.group_name}{C.RESET}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"  Members: {gs.member_count}    Jobs: {gs.total_jobs}")
    lines.append(f"  Period: {gs.date_range[0][:10]} → {gs.date_range[1][:10]}")
    lines.append("")

    # Key insight
    lines.append(f"  {C.BOLD}Key Insight{C.RESET}")
    lines.append(f"  {'─' * 56}")
    imp_color = C.GREEN if gs.users_improving > gs.member_count / 2 else C.YELLOW
    lines.append(f"    {imp_color}{gs.improvement_rate}{C.RESET}")
    lines.append("")

    # Dimension averages
    lines.append(f"  {C.BOLD}Group Proficiency{C.RESET}")
    lines.append(f"  {'─' * 56}")
    for dim, avg in sorted(gs.dimension_avgs.items(), key=lambda x: -x[1]):
        color = C.score_color(avg)
        imp = gs.dimension_improvements.get(dim, 0)
        imp_sym = "↑" if imp > 5 else "↓" if imp < -5 else "→"
        imp_color = C.GREEN if imp > 5 else C.RED if imp < -5 else C.DIM
        dim_label = {
            "cpu": "CPU Efficiency",
            "memory": "Memory Efficiency",
            "time": "Time Estimation",
            "io": "I/O Awareness",
            "gpu": "GPU Utilization",
        }.get(dim, dim)
        lines.append(
            f"    {dim_label:<20s} "
            f"{color}{bar(avg)}{C.RESET}  "
            f"{color}{avg:>5.1f}%{C.RESET}  "
            f"{imp_color}{imp_sym} {imp:+.1f}%{C.RESET}"
        )
    lines.append("")

    lines.append(
        f"    Weakest area:   {C.YELLOW}{gs.weakest_dimension}{C.RESET}  |  "
        f"Strongest: {C.GREEN}{gs.strongest_dimension}{C.RESET}"
    )
    lines.append("")

    # Breakdown
    lines.append(f"  {C.BOLD}Student Breakdown{C.RESET}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"    {C.GREEN}Improving:{C.RESET}  {gs.users_improving}")
    lines.append(f"    {C.DIM}Stable:{C.RESET}     {gs.users_stable}")
    lines.append(f"    {C.RED}Declining:{C.RESET}  {gs.users_declining}")
    lines.append("")

    # Per-student table
    if gs.users:
        lines.append(f"  {C.BOLD}Per-Student Summary{C.RESET}")
        lines.append(f"  {'─' * 56}")
        lines.append(
            f"    {'User':<15s} {'Jobs':>5s} {'Overall':>8s} {'Change':>8s} {'Trend':>10s}"
        )
        for traj in sorted(gs.users, key=lambda t: -t.overall_improvement):
            overall = sum(traj.current_scores.values()) / max(1, len(traj.current_scores))
            color = C.score_color(overall)
            imp_color = (C.GREEN if traj.overall_improvement > 5
                         else C.RED if traj.overall_improvement < -5
                         else C.DIM)
            symbol = ("↑" if traj.overall_improvement > 5
                      else "↓" if traj.overall_improvement < -5
                      else "→")
            lines.append(
                f"    {traj.username:<15s} "
                f"{traj.total_jobs:>5d} "
                f"{color}{overall:>7.1f}%{C.RESET} "
                f"{imp_color}{traj.overall_improvement:>+7.1f}%{C.RESET} "
                f"{imp_color}{symbol:>10s}{C.RESET}"
            )
        lines.append("")

    return "\n".join(lines)
