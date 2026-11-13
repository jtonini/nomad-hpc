# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.47.0: which jobs a recommendation was found in; the time limit counting least.

Each card carries the jobs it was found in (a "Jobs that failed" card: the
jobs that failed), newest first, with what each asked and used. An overall
score weighs the dimensions by [edu.weights]: cores, memory and GPUs fully,
I/O half, the time limit a quarter. Within a place, the cards of the
dimensions that count fully come first.
"""
from __future__ import annotations

import sqlite3
import sys

import pytest
from click.testing import CliRunner

from nomad.edu import insights as I
from nomad.edu import scoring as S

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_release_1450 import SIZING_TIERS, _sized_db  # noqa: E402


@pytest.fixture(autouse=True)
def _weights_read_again():
    S.set_dimension_weights(None)
    yield
    S.set_dimension_weights(None)


def test_the_weights_and_their_problems():
    assert S.DIMENSION_WEIGHTS == {"cpu": 1.0, "memory": 1.0, "gpu": 1.0, "io": 0.5, "time": 0.25}
    assert S.read_weights(None) == ({}, [])
    got, problems = S.read_weights({"time": 0.1, "io": "half", "cpu": True, "walltime": 1, "gpu": 11})
    assert got == {"time": 0.1}
    assert len(problems) == 4 and any("walltime: not a dimension" in p for p in problems)
    assert S.read_weights([1, 2])[1] and S.read_weights({"memory": 0})[0] == {"memory": 0.0}


def test_the_weights_come_from_nomad_toml(monkeypatch):
    import nomad.config as C
    monkeypatch.setattr(C, "load_config", lambda *a, **k: {"edu": {"weights": {"time": 0.5, "io": "x"}}})
    S.set_dimension_weights(None)
    assert S.dimension_weights() == {**S.DIMENSION_WEIGHTS, "time": 0.5}
    monkeypatch.setattr(C, "load_config", lambda *a, **k: {})
    assert S.dimension_weights()["time"] == 0.5               # read once
    S.set_dimension_weights(None)
    assert S.dimension_weights()["time"] == 0.25


def test_a_weighted_mean():
    assert S.weighted_mean({"cpu": 80, "time": 0}) == pytest.approx(64.0)     # not 40
    assert S.weighted_mean({}) is None
    assert S.weighted_mean({"memory_pressure": 50, "duration_fit": 70}) == 60  # a session's: 1 each
    S.set_dimension_weights({"time": 0.0})
    assert S.weighted_mean({"time": 30}) == 30                                 # all 0: the plain mean
    assert S.weighted_mean({"cpu": 80, "time": 0}) == 80


def test_a_jobs_overall_counts_the_time_limit_a_quarter():
    fp = S.JobFingerprint(job_id="1", user="kim")
    for k, v in (("cpu", 80), ("memory", 60), ("time", 10)):
        fp.dimensions[k] = S.DimensionScore(name=k, score=v, level="", detail="")
    fp.dimensions["io"] = S.DimensionScore(name="io", score=0, level="", detail="", applicable=False)
    assert fp.overall == pytest.approx((80 + 60 + 0.25 * 10) / 2.25)
    S.set_dimension_weights({k: 1.0 for k in S.DIMENSION_WEIGHTS})
    assert fp.overall == pytest.approx(50.0)


def _db(tmp_path, jobs, name="w"):
    db = str(tmp_path / f"{name}.db")
    _sized_db(db, jobs)
    c = sqlite3.connect(db)
    c.execute("UPDATE jobs SET job_name = 'align_' || job_id")
    c.commit()
    c.close()
    return db


def test_a_persons_overall_is_weighed_and_kept_per_weighting(tmp_path):
    from nomad.edu.population import _mean_of_dims, population
    dims, overall = _mean_of_dims({"cpu": 160.0, "time": 20.0}, {"cpu": 2, "time": 2})
    assert dims == {"cpu": 80.0, "time": 10.0} and overall == pytest.approx(66.0)
    db = _db(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3)
    first = population(db).people["pat"]
    S.set_dimension_weights({k: 1.0 for k in S.DIMENSION_WEIGHTS})
    plain = population(db).people["pat"]
    assert first.dimensions == plain.dimensions and first.overall != plain.overall
    assert plain.overall == pytest.approx(sum(plain.dimensions.values()) / len(plain.dimensions), abs=0.06)


def _cards(db):
    ui = I.user_insights(db, "pat", cluster_capacities=SIZING_TIERS)
    return ui, [i for i in ui.issues if i.kind == "dimension"]


def test_a_card_lists_its_jobs_newest_first(tmp_path):
    ui, cards = _cards(_db(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3))
    cpu = next(c for c in cards if c.dimension_key == "cpu")
    assert [j["job_id"] for j in cpu.jobs] == ["9000", "9001", "9002"]     # 9000 ended last
    j = cpu.jobs[0]
    assert j["site"] == "spydur" and j["partition"] == "basic" and j["state"] == "COMPLETED"
    assert j["name"] == "align_9000" and j["ended"] and j["score"] < 40
    assert j["asked"] == 52 and j["unit"] == "cores" and j["used"] == pytest.approx(1.04, abs=0.05)
    assert j["detail"]
    mem = next(c for c in cards if c.dimension_key == "memory")
    assert mem.jobs[0]["unit"] == "MB" and mem.jobs[0]["asked"] == 358400
    assert mem.jobs[0]["used"] == pytest.approx(3 * 1024)


def test_the_failed_card_lists_the_jobs_that_failed(tmp_path):
    _, cards = _cards(_db(tmp_path, [("COMPLETED", 95.0, 900_000, 330.0)] + [("FAILED", 2.0, 300, 3.0)] * 3))
    failed = next(c for c in cards if c.dimension_key == "failed")
    assert [j["job_id"] for j in failed.jobs] == ["9001", "9002", "9003"]
    assert {j["state"] for j in failed.jobs} == {"FAILED"} and "score" not in failed.jobs[0]


def test_at_most_so_many_jobs_and_the_rest_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "FLAGGED_JOBS_MAX", 2)
    ui, cards = _cards(_db(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3))
    cpu = next(c for c in cards if c.dimension_key == "cpu")
    assert len(cpu.jobs) == 2 and cpu.affected_jobs == 3
    assert I._job_ids(cpu) == "Jobs: 9000, 9001 and 1 more (newest first; nomad edu explain JOB for one)"
    text = I.format_user_insights(ui, detailed=True)
    assert "Jobs: 9000, 9001 and 1 more" in text
    assert "The overall counts each by its weight: CPU 1, memory 1, GPU 1, I/O 0.5, time limit 0.25" in text


def test_cores_and_memory_come_before_the_time_limit(tmp_path):
    """12 days asked, an hour run: the time card is the worst, and still comes after cores and memory."""
    ui, cards = _cards(_db(tmp_path, [("COMPLETED", 2.0, 3600, 3.0)] * 3))
    keys = [c.dimension_key for c in cards]
    assert {"cpu", "memory", "time"} <= set(keys)
    assert keys.index("time") > keys.index("cpu") and keys.index("time") > keys.index("memory")
    weight = {c.dimension_key: c.weight for c in cards}
    assert weight["time"] == 0.25 and weight["cpu"] == 1.0
    assert ui.weights == S.DIMENSION_WEIGHTS
    assert ui.overall_score == pytest.approx(S.weighted_mean(ui.dimensions), abs=0.06)


def test_the_time_limit_in_plain_words():
    assert I.KEY_TO_DISPLAY["time"] == "Time Limit"
    job = {"state": "COMPLETED", "runtime_seconds": 3600, "req_time_seconds": 86400}
    d = S.score_time(job, {})
    assert d.name == "Time Limit"
    assert "Ran 1h of the 1d 0h it asked for (4%)" in d.detail and "start it sooner" in d.detail
    t = S.score_time({"state": "TIMEOUT", "runtime_seconds": 3600, "req_time_seconds": 3600}, {})
    assert t.detail.startswith("Stopped at its time limit (1h)")
    assert "walltime" not in I.DIMENSION_FRAMING["time"] and "start them sooner" in I.DIMENSION_FRAMING["time"]


def test_config_check_says_what_is_wrong_with_the_weights(tmp_path):
    from nomad import cli as cli_mod
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[edu.weights]\ntime = "low"\nwalltime = 1\n')
    out = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "config", "check"]).output
    assert "[edu.weights] time = 'low': a number from 0 to 10; 0.25 is used" in out
    assert "[edu.weights] walltime: not a dimension" in out
