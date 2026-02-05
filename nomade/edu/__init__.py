"""
NØMADE Edu — Educational Analytics for HPC

Bridges the gap between infrastructure monitoring and educational outcomes
by capturing per-job behavioral fingerprints that enable administrators and
faculty to measure the development of computational proficiency over time.
"""

from nomade.edu.scoring import score_job, JobFingerprint
from nomade.edu.explain import explain_job
from nomade.edu.progress import user_trajectory, group_summary

__all__ = [
    "score_job",
    "JobFingerprint",
    "explain_job",
    "user_trajectory",
    "group_summary",
]
