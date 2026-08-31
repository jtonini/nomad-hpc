# SPDX-License-Identifier: AGPL-3.0-or-later
"""Graded GPU scoring from real DCGM avg_gpu_util (fix #4)."""
from nomad.edu.scoring import score_job

BASE = {
    "job_id": "gpu-graded", "user_name": "u", "state": "COMPLETED",
    "req_cpus": 4, "req_mem_mb": 8192, "req_gpus": 2,
    "req_time_seconds": 3600, "runtime_seconds": 1800,
}


def _level(summary):
    return score_job(BASE, summary).dimensions["gpu"].level


def test_gpu_not_applicable_without_request():
    noreq = dict(BASE, req_gpus=0)
    assert not score_job(noreq, {}).dimensions["gpu"].applicable


def test_gpu_requested_never_used():
    assert _level({"used_gpu": 0}) == "Needs Work"


def test_gpu_used_no_dcgm_data_falls_back():
    assert _level({"used_gpu": 1}) == "Good"


def test_gpu_graded_bands():
    assert _level({"used_gpu": 1, "avg_gpu_util": 5}) == "Needs Work"
    assert _level({"used_gpu": 1, "avg_gpu_util": 25}) == "Developing"
    assert _level({"used_gpu": 1, "avg_gpu_util": 55}) == "Good"
    assert _level({"used_gpu": 1, "avg_gpu_util": 85}) == "Excellent"


def test_gpu_threshold_boundaries():
    assert _level({"used_gpu": 1, "avg_gpu_util": 70}) == "Excellent"
    assert _level({"used_gpu": 1, "avg_gpu_util": 40}) == "Good"
    assert _level({"used_gpu": 1, "avg_gpu_util": 15}) == "Developing"
    assert _level({"used_gpu": 1, "avg_gpu_util": 14.9}) == "Needs Work"


def test_gpu_low_util_has_suggestion():
    low = score_job(BASE, {"used_gpu": 1, "avg_gpu_util": 3})
    assert low.dimensions["gpu"].suggestion is not None
