# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.65.0: advice never asks for fewer cores (GPUs) than the most any of the
jobs it is about used; a GPU that doesn't report its power is stored as not
read, never 0 W."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from nomad.edu.insights import _aggregate_dimension
from nomad.edu.scoring import DimensionScore, JobFingerprint, Suggestion


def fp(job_id, asked, used, suggested=None):
    f = JobFingerprint(job_id=job_id, user="kim")
    sug = Suggestion(directive="cpus-per-task", suggested_value=suggested or max(1, used),
                     current_value=asked, actual_usage=used, unit="cores", rationale="test")
    f.dimensions["cpu"] = DimensionScore(name="CPU Efficiency", score=20.0, level="Needs Work", detail="",
                                         suggestion=sug, applicable=True)
    return f


def test_never_fewer_cores_than_the_most_used():
    jobs = [fp(f"j{i}", 8, 1) for i in range(5)] + [fp("j9", 8, 3)]
    issue = _aggregate_dimension("cpu", jobs, 40.0)
    assert issue.suggested_display == "3" and issue.usage_stats.max == 3
    assert "the most any of the 6 flagged jobs used" in issue.suggestion_rationale


def test_nothing_to_ask_less_of_when_one_used_more_than_is_asked():
    """As seen on the demo: jobs asking for different numbers of cores (the
    typical request 6), most using one, one using 10 of the 32 it asked for:
    told to ask for 1."""
    jobs = [fp("j1", 6, 1), fp("j2", 6, 1), fp("j3", 4, 1), fp("j4", 4, 1), fp("j5", 8, 1),
            fp("j6", 32, 10, suggested=11)]
    issue = _aggregate_dimension("cpu", jobs, 40.0)
    assert issue.current_value_typical == 6 and issue.usage_stats.max == 10
    assert not issue.suggested_display                    # no #SBATCH line: no note in the email


def test_the_email_says_it_in_cores():
    from nomad.notices import _suggested_words, _typical
    jobs = [fp(f"j{i}", 8, 1) for i in range(5)] + [fp("j9", 8, 3)]
    issue = _aggregate_dimension("cpu", jobs, 40.0)
    assert _typical(issue) == "they typically asked for 8 cores; most used about 1 core, at most 3 cores"
    assert _suggested_words(issue) == "That is 3 cores: as many as the most any of these jobs used."


def test_a_gpu_without_a_power_sensor_is_not_read():
    from nomad.collectors.gpu import GPUCollector
    c = GPUCollector({}, ":memory:")
    out = c._parse_nvidia_output("0, NVIDIA T1000 8GB, 12.0, 5, 512, 8192, 7680, 41, [N/A], [N/A]\n"
                                 "1, NVIDIA A100-SXM4-40GB, 38.5, 75, 32456, 40960, 8504, 62, 285.0, 400.0\n")
    assert (out[0]["power_draw_w"], out[0]["power_limit_w"]) == (None, None)
    assert out[0]["gpu_util_percent"] == 12.0
    assert (out[1]["power_draw_w"], out[1]["power_limit_w"]) == (285.0, 400.0)


def test_energy_leaves_out_0_watts():
    from nomad.energy.power import gpu_energy_from_dcgm
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE gpu_stats (node_name TEXT, gpu_index INTEGER, timestamp TEXT, power_draw_w REAL, "
                 "real_util_pct REAL, workload_class TEXT)")
    t0 = datetime(2026, 11, 1, 10)
    rows = [("g1", 0, (t0 + timedelta(minutes=m)).isoformat(sep=" "), 0.0, 50.0, "compute") for m in range(60)]
    conn.executemany("INSERT INTO gpu_stats VALUES (?,?,?,?,?,?)", rows)
    total, idle, present = gpu_energy_from_dcgm(conn, t0, t0 + timedelta(hours=1))
    assert present is False                               # estimated instead, not 0 Wh
    conn.executemany("INSERT INTO gpu_stats VALUES (?,?,?,?,?,?)",
                     [("g2", 0, (t0 + timedelta(minutes=m)).isoformat(sep=" "), 200.0, 50.0, "compute")
                      for m in range(60)])
    total, idle, present = gpu_energy_from_dcgm(conn, t0, t0 + timedelta(hours=1))
    assert present and abs(total - 200.0) < 5
