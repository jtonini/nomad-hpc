# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Niche overlap analysis for research computing environments.

Computes pairwise resource-usage overlap between user communities
using Pianka's niche overlap index. Identifies high-overlap pairs
with contention risk.

Mathematical reference:
    Pianka (1973): O_jk = Σ(p_ij * p_ik) / sqrt(Σ p_ij² * Σ p_ik²)
    where p_ij is the proportion of resource dimension i used by group j.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# Resource dimensions used for niche profiles
RESOURCE_DIMENSIONS = [
    "avg_cpus",
    "avg_mem_mb",
    "avg_gpus",
    "avg_runtime_sec",
    "job_count",
]


@dataclass
class NicheProfile:
    """Resource usage profile for a single group/community."""
    name: str
    raw_values: dict[str, float] = field(default_factory=dict)
    proportions: dict[str, float] = field(default_factory=dict)
    job_count: int = 0


@dataclass
class OverlapPair:
    """Pairwise overlap between two groups."""
    group_a: str
    group_b: str
    overlap: float  # Pianka's O_jk, 0-1
    contention_risk: str = "low"  # "low", "moderate", "high"
    shared_dimensions: list[str] = field(default_factory=list)


@dataclass
class NicheResult:
    """Complete niche overlap analysis."""
    profiles: list[NicheProfile]
    overlap_matrix: dict[tuple[str, str], float]
    high_overlap_pairs: list[OverlapPair]
    contention_risk_count: dict[str, int] = field(default_factory=dict)


def _pianka_overlap(p: dict[str, float], q: dict[str, float]) -> float:
    """Compute Pianka's niche overlap index between two proportion vectors.

    O_jk = Σ(p_i * q_i) / sqrt(Σ p_i² * Σ q_i²)
    """
    dims = set(p.keys()) | set(q.keys())
    if not dims:
        return 0.0

    numerator = sum(p.get(d, 0) * q.get(d, 0) for d in dims)
    denom_p = sum(p.get(d, 0) ** 2 for d in dims)
    denom_q = sum(q.get(d, 0) ** 2 for d in dims)

    denominator = math.sqrt(denom_p * denom_q)
    if denominator == 0:
        return 0.0

    return numerator / denominator


def _identify_shared_dimensions(
    pa: dict[str, float], pb: dict[str, float], threshold: float = 0.15
) -> list[str]:
    """Identify dimensions where both groups have substantial usage."""
    shared = []
    dims = set(pa.keys()) | set(pb.keys())
    for d in dims:
        if pa.get(d, 0) >= threshold and pb.get(d, 0) >= threshold:
            shared.append(d)
    return shared


def compute_niche_overlap(
    db_path: Path | str,
    hours: int = 168,
    overlap_threshold: float = 0.6,
    min_jobs: int = 5,
) -> NicheResult:
    """Compute pairwise niche overlap between user groups.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to look
    overlap_threshold : pairs above this value are flagged as high-overlap
    min_jobs : minimum jobs to include a group in analysis
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    # ── Build group resource profiles ─────────────────────────────────
    query = """
        SELECT
            COALESCE(gm.group_name, 'ungrouped') AS grp,
            COUNT(*) AS job_count,
            AVG(j.req_cpus) AS avg_cpus,
            AVG(j.req_mem_mb) AS avg_mem_mb,
            AVG(j.req_gpus) AS avg_gpus,
            AVG(j.runtime_seconds) AS avg_runtime_sec
        FROM jobs j
        LEFT JOIN group_membership gm ON j.user_name = gm.username
        WHERE j.submit_time >= ?
        GROUP BY grp
        HAVING job_count >= ?
        ORDER BY job_count DESC
    """
    rows = conn.execute(query, (cutoff, min_jobs)).fetchall()

    # Filter umbrella groups (>80% of all users)
    try:
        total_users = conn.execute(
            "SELECT COUNT(DISTINCT username) FROM group_membership"
        ).fetchone()[0]
        if total_users > 0:
            umbrella = set()
            grp_sizes = conn.execute(
                "SELECT group_name, COUNT(DISTINCT username) as cnt"
                " FROM group_membership GROUP BY group_name"
            ).fetchall()
            for g in grp_sizes:
                if g["cnt"] / total_users > 0.8:
                    umbrella.add(g["group_name"])
            if umbrella:
                rows = [r for r in rows if r["grp"] not in umbrella]
    except Exception:
        pass

    conn.close()

    if not rows:
        return NicheResult(
            profiles=[], overlap_matrix={},
            high_overlap_pairs=[], contention_risk_count={"low": 0, "moderate": 0, "high": 0},
        )

    # Build raw profiles
    profiles: list[NicheProfile] = []
    for r in rows:
        raw = {
            "avg_cpus": float(r["avg_cpus"] or 0),
            "avg_mem_mb": float(r["avg_mem_mb"] or 0),
            "avg_gpus": float(r["avg_gpus"] or 0),
            "avg_runtime_sec": float(r["avg_runtime_sec"] or 0),
            "job_count": float(r["job_count"] or 0),
        }
        profiles.append(NicheProfile(
            name=r["grp"],
            raw_values=raw,
            job_count=int(r["job_count"]),
        ))

    # Normalize each dimension across groups to get proportions
    # For each dimension, compute each group's share of total usage
    for dim in RESOURCE_DIMENSIONS:
        total = sum(p.raw_values.get(dim, 0) * p.job_count for p in profiles)
        if total > 0:
            for p in profiles:
                p.proportions[dim] = (p.raw_values[dim] * p.job_count) / total
        else:
            for p in profiles:
                p.proportions[dim] = 0.0

    # ── Compute pairwise overlap ──────────────────────────────────────
    overlap_matrix: dict[tuple[str, str], float] = {}
    high_overlap_pairs: list[OverlapPair] = []

    names = [p.name for p in profiles]
    profile_map = {p.name: p for p in profiles}

    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i >= j:
                # Self-overlap is always 1.0; store for completeness
                if i == j:
                    overlap_matrix[(a, b)] = 1.0
                continue

            o = _pianka_overlap(profile_map[a].proportions, profile_map[b].proportions)
            overlap_matrix[(a, b)] = o
            overlap_matrix[(b, a)] = o

            if o >= overlap_threshold:
                # Determine contention risk level
                if o >= 0.8:
                    risk = "high"
                elif o >= 0.6:
                    risk = "moderate"
                else:
                    risk = "low"

                shared = _identify_shared_dimensions(
                    profile_map[a].proportions,
                    profile_map[b].proportions,
                )

                high_overlap_pairs.append(OverlapPair(
                    group_a=a, group_b=b, overlap=o,
                    contention_risk=risk, shared_dimensions=shared,
                ))

    # Sort by overlap descending
    high_overlap_pairs.sort(key=lambda x: x.overlap, reverse=True)

    risk_counts = {"low": 0, "moderate": 0, "high": 0}
    for pair in high_overlap_pairs:
        risk_counts[pair.contention_risk] += 1

    return NicheResult(
        profiles=profiles,
        overlap_matrix=overlap_matrix,
        high_overlap_pairs=high_overlap_pairs,
        contention_risk_count=risk_counts,
    )
