# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What a recommendation says about where its value comes from is true.

It printed "covers 95% of your jobs" for a single job, and "the most common
request across your flagged jobs" for a core count that came from what the
jobs used, not what they asked for.
"""
from nomad.edu.insights import _aggregate_mode, _aggregate_quantile
from nomad.edu.scoring import Suggestion, round_memory_up, round_time_up


def _mem(used_mb, requested_mb=1_433_600):
    return Suggestion(directive="mem", suggested_value=used_mb * 2, current_value=requested_mb,
                      actual_usage=used_mb, unit="MB", rationale="")


def _cores(needed, requested=48):
    return Suggestion(directive="ntasks", suggested_value=needed, current_value=requested,
                      actual_usage=needed, unit="cores", rationale="")


def test_one_job_is_not_a_percentage():
    value, _, why = _aggregate_quantile([_mem(12_000)], 2.0, round_memory_up)
    assert value >= 12_000
    assert why == "fits what the flagged job used, with a 2x safety buffer"
    assert "%" not in why


def test_time_for_one_job():
    one = Suggestion(directive="time", suggested_value=0, current_value=365 * 86400,
                     actual_usage=5 * 86400, unit="seconds", rationale="")
    _, _, why = _aggregate_quantile([one], 1.5, round_time_up)
    assert why == "fits what the flagged job used, with a 1.5x safety buffer"


def test_every_job_it_fits_is_counted():
    _, _, why = _aggregate_quantile([_mem(u) for u in (9_000, 10_000, 11_000, 12_000, 12_500)],
                                    2.0, round_memory_up)
    assert why == "fits what each of the 5 flagged jobs used, with a 2x safety buffer"


def test_an_outlier_it_does_not_fit_is_said():
    jobs = [_mem(1_000)] * 20 + [_mem(50_000)]
    value, _, why = _aggregate_quantile(jobs, 2.0, round_memory_up)
    assert value < 50_000
    assert why.startswith("fits what 20 of the 21 flagged jobs used, with a 2x safety buffer")


def test_cores_come_from_what_the_jobs_used_not_requested():
    _, _, one = _aggregate_mode([_cores(3)])
    _, _, same = _aggregate_mode([_cores(3)] * 4)
    value, _, most = _aggregate_mode([_cores(3)] * 3 + [_cores(5)] * 2)
    assert one == "what the flagged job needed, from what it used"
    assert same == "what each of the 4 flagged jobs needed, from what they used"
    assert value == 3
    assert most == "what 3 of the 5 flagged jobs needed, from what they used"
    assert all("request" not in w for w in (one, same, most))
