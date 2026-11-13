# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.46.0: cores left idle, and ranges on the AUCs.

The idle target: completed jobs of 2 cores or more that ran 5 minutes or
more and were measured, which on average left more than idle_threshold of
the cores they asked for unused (the jobs Recommendations judge for cores).
With [ml.train] bootstrap, 95% ranges of each model's AUC and of their
difference, from paired resamples of the held-out jobs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from nomad.learn import dataset as D
from nomad.learn import train as T
from nomad.learn.settings import Settings, settings

NOW = datetime(2026, 11, 13, 6, 0, 0)


def test_the_default_question_is_cores_not_time():
    s = settings({"ml": {"train": {}}})
    assert s.predicts == ("failure", "idle") and s.idle_threshold == 0.5 and s.bootstrap == 1000
    assert s.plan()["idle_threshold"] == 0.5 and s.plan()["bootstrap"] == 1000
    s = settings({"ml": {"train": {"predicts": ["failure", "waste", "idle"], "idle_threshold": 2, "bootstrap": -1}}})
    assert s.predicts == ("failure", "waste", "idle") and s.idle_threshold == 0.5 and s.bootstrap == 1000
    assert len(s.problems) == 2


def _db(path, rows):
    """rows: (job_id, site, state, req_cpus, runtime_s, avg_cpu or None)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, partition TEXT, req_cpus INTEGER, req_gpus INTEGER, "
              "req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, submit_time TEXT, end_time TEXT, "
              "state TEXT)")
    c.execute("CREATE TABLE job_summary (job_id TEXT, source_site TEXT, avg_cpu_percent REAL)")
    for i, (jid, site, state, cpus, ran, avg) in enumerate(rows):
        sub = NOW - timedelta(days=30) + timedelta(hours=i)
        c.execute("INSERT INTO jobs VALUES (?, ?, 'p', ?, 0, 4096, 36000, ?, ?, ?, ?)",
                  (jid, site, cpus, ran, sub.isoformat(), (sub + timedelta(seconds=ran)).isoformat(), state))
        if avg is not None:
            c.execute("INSERT INTO job_summary VALUES (?, ?, ?)", (jid, site, avg))
    c.commit()
    c.row_factory = sqlite3.Row
    return c


def test_which_jobs_are_asked_whether_they_left_cores_idle(tmp_path):
    rows = [("1", "spydur", "COMPLETED", 8, 3600, 20.0),      # 80% idle: yes
            ("2", "spydur", "COMPLETED", 8, 3600, 80.0),      # 20% idle: no
            ("3", "spydur", "COMPLETED", 1, 3600, 10.0),      # one core: nothing to ask less of
            ("4", "spydur", "COMPLETED", 8, 120, 10.0),       # 2 minutes: starting up
            ("5", "spydur", "FAILED", 8, 3600, 10.0),         # failed: not asked
            ("6", "spydur", "COMPLETED", 8, 3600, None),      # not measured
            ("7", "arachne", "COMPLETED", 8, 3600, None)]     # job 7's figure is spydur's only
    c = _db(tmp_path / "c.db", rows + [("7x", "spydur", "COMPLETED", 8, 3600, 50.0)])
    c.execute("UPDATE job_summary SET job_id = '7' WHERE job_id = '7x'")
    plan = {**Settings(enabled=True, holdout_share=0.0).plan()}
    data, mapping = D.build(c, plan, NOW)
    idle = dict(zip([m[1] for m in mapping], data["labels"]["idle"]))
    assert idle["1"] == 1 and idle["2"] == 0
    assert idle["3"] is None and idle["4"] is None and idle["5"] is None and idle["6"] is None
    assert idle["7"] is None                                  # another site's job 7: never its figure
    assert data["spread"]["idle"] is None                     # fewer than 20: not said


def test_the_idle_spread_says_cores(tmp_path):
    rows = [(str(i), "s", "COMPLETED", 4, 3600, float(i)) for i in range(1, 101)]
    c = _db(tmp_path / "c.db", rows)
    data, _ = D.build(c, Settings(enabled=True).plan(), NOW)
    sp = data["spread"]["idle"]
    assert sp["of"] == "cores" and sp["jobs"] == 100
    assert sp["used_at"]["0.5"] == pytest.approx(0.51) and sp["wasted_at"]["0.5"] == pytest.approx(0.49)
    assert data["spread"]["waste"]["of"] == "time"


def test_no_summary_table_no_idle_labels(tmp_path):
    c = sqlite3.connect(tmp_path / "c.db")
    c.execute("CREATE TABLE jobs (job_id TEXT, partition TEXT, req_cpus INTEGER, req_gpus INTEGER, req_mem_mb INTEGER, "
              "req_time_seconds INTEGER, runtime_seconds INTEGER, submit_time TEXT, end_time TEXT, state TEXT)")
    c.execute("INSERT INTO jobs VALUES ('1', 'p', 8, 0, 1, 3600, 3600, ?, ?, 'COMPLETED')",
              ((NOW - timedelta(days=1)).isoformat(), NOW.isoformat()))
    c.row_factory = sqlite3.Row
    data, _ = D.build(c, Settings(enabled=True).plan(), NOW)
    assert data["labels"]["idle"] == [None]


def test_ranges_say_whether_two_models_differ():
    rng = np.random.default_rng(3)
    y = (rng.random(2000) < 0.2).astype(int)
    good = y + rng.normal(0, 0.6, len(y))
    same = good + rng.normal(0, 0.01, len(y))
    worse = y + rng.normal(0, 2.0, len(y))
    r = T._intervals(y, good, worse, 300)
    assert r["resamples"] == 300 and r["difference"][0] > 0          # clearly better: the range excludes 0
    lo, hi = r["tessera"]
    assert lo < T._auc(y, good) < hi
    r = T._intervals(y, good, same, 300)
    assert r["difference"][0] < 0 < r["difference"][1]               # the same: the range spans 0
    assert T._intervals(y, None, worse, 100)["difference"] is None   # one model: its range only
    assert T._intervals(y[:10], good[:10], worse[:10], 100) is None  # too few to say
    assert T._intervals(y, good, worse, 0) is None


def test_a_run_carries_its_ranges():
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from test_release_1451 import _data
    d = _data()
    d["plan"]["bootstrap"] = 200
    out = T.run(d, "cpu")["summary"]["targets"]["failure"]
    assert out["interval"]["resamples"] == 200 and out["interval"]["tessera"] is None
    assert out["without_place"]["interval"]["baseline"] is not None
    d["plan"].pop("bootstrap")
    assert "interval" not in T.run(d, "cpu")["summary"]["targets"]["failure"]
