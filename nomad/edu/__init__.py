# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Edu — Educational Analytics for HPC

Bridges the gap between infrastructure monitoring and educational outcomes
by capturing per-job behavioral fingerprints that enable administrators and
faculty to measure the development of computational proficiency over time.
"""

from nomad.edu.explain import explain_job
from nomad.edu.progress import group_summary, user_trajectory
from nomad.edu.scoring import JobFingerprint, score_job

__all__ = [
    "score_job",
    "JobFingerprint",
    "explain_job",
    "user_trajectory",
    "group_summary",
]

# Storage functions for proficiency tracking
from nomad.edu.storage import (
    get_group_proficiency_stats,
    get_user_proficiency_history,
    init_proficiency_table,
    save_proficiency_score,
)
