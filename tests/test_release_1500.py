# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.50.0: a fairer test of TESSERA.

thin: one person's same request on one day counts a few times, not
thousands. rolling: each of the last periods (weeks by default) held out in
turn, both models trained on what ended before it. people_folds: new people,
each group's held-out jobs tested on models that never saw its people. Over
the tests, the mean AUCs and how often the difference was clear of zero.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta

import numpy as np
import pytest

from nomad.learn import cli as C
from nomad.learn import dataset as D
from nomad.learn import records as R
from nomad.learn import train as T
from nomad.learn.settings import Settings, settings

NOW = datetime(2026, 11, 16, 9, 0, 0)
PEOPLE = ("kim", "pat", "ann", "olive", "lee", "sam", "quinn")


def test_the_settings_and_their_bounds():
    s = settings({"ml": {"train": {}}})
    assert (s.thin, s.rolling, s.rolling_days, s.people_folds) == (0, 0, 7, 0)
    assert {k: s.plan()[k] for k in ("thin", "rolling", "rolling_days", "people_folds")} == {
        "thin": 0, "rolling": 0, "rolling_days": 7, "people_folds": 0}
    s = settings({"ml": {"train": {"thin": 3, "rolling": 6, "rolling_days": 14, "people_folds": 5}}})
    assert (s.thin, s.rolling, s.rolling_days, s.people_folds) == (3, 6, 14, 5) and not s.problems
    bad = settings({"ml": {"train": {"thin": -1, "rolling": 27, "rolling_days": 0, "people_folds": 1}}})
    assert (bad.thin, bad.rolling, bad.rolling_days, bad.people_folds) == (0, 0, 7, 0) and len(bad.problems) == 4
    assert settings({"ml": {"train": {"people_folds": 11}}}).problems
    assert settings({"ml": {"train": {"people_folds": True}}}).problems


def _db(path, n=1400, seed=2, array=0):
    """Jobs ending over about ten weeks, by seven made-up people; failure set by partition and by
    cores, with noise. ``array``: kim also sends that many identical jobs on each of three days."""
    rng = np.random.default_rng(seed)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, user_name TEXT, partition TEXT, req_cpus INTEGER, "
              "req_gpus INTEGER, req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, "
              "submit_time TEXT, end_time TEXT, state TEXT)")
    rows = []
    for i in range(n):
        sub = NOW - timedelta(days=72) + timedelta(hours=i * 1.2)
        part = str(rng.choice(["a", "b", "c"]))
        cpus = int(rng.choice([1, 4, 16]))
        bad = (part == "c") != (cpus == 16)
        bad = bad if rng.random() > 0.1 else not bad
        rows.append((str(i), "s", PEOPLE[i % 7], part, cpus, sub, bad))
    for d in range(3):
        for j in range(array):
            rows.append((f"9{d}_{j}", "s", "kim", "a", 2, NOW - timedelta(days=30 + d, hours=5) + timedelta(seconds=j),
                         j % 2 == 0))
    for jid, site, who, part, cpus, sub, bad in rows:
        c.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, 0, 4096, 36000, 600, ?, ?, ?)",
                  (jid, site, who, part, cpus, sub.isoformat(), (sub + timedelta(minutes=10)).isoformat(),
                   "FAILED" if bad else "COMPLETED"))
    c.execute("INSERT INTO jobs VALUES ('w', 's', 'pat', 'a', 4, 0, 4096, 3600, 0, ?, NULL, 'PENDING')",
              (NOW.isoformat(),))
    c.commit()
    c.row_factory = sqlite3.Row
    return c


def _plan(**kw):
    return {**Settings(enabled=True, engine="baseline", predicts=("failure",), bootstrap=100).plan(), **kw}


def test_repeats_are_thinned_the_same_ones_each_time(tmp_path):
    c = _db(tmp_path / "c.db", array=500)
    full, _ = D.build(c, _plan(), NOW)
    data, mapping = D.build(c, _plan(thin=3), NOW)
    again, mapping2 = D.build(c, _plan(thin=3), NOW)
    assert data["counts"]["thinned"] == 3 * (500 - 3)
    assert full["counts"]["train"] + full["counts"]["holdout"] == 1400 + 1500
    assert data["counts"]["train"] + data["counts"]["holdout"] == 1400 + 9
    assert mapping == mapping2                                      # the same draw each time
    kept = [m[1] for m in mapping if "_" in m[1]]
    assert len(kept) == 9 and {k.split("_")[0] for k in kept} == {"90", "91", "92"}
    # before max_jobs: the newest 1,000 reach further back once the repeats are out
    capped, _ = D.build(c, _plan(max_jobs=1000), NOW)
    capped_thin, _ = D.build(c, _plan(max_jobs=1000, thin=3), NOW)
    assert capped_thin["counts"]["ended_from"] < capped["counts"]["ended_from"]


def test_periods_count_back_from_the_newest_job(tmp_path):
    data, _ = D.build(_db(tmp_path / "c.db"), _plan(rolling=4), NOW)
    c = data["counts"]
    assert c["ended_to"] == "2026-11-14" and c["ended_from"] == "2026-09-05"
    assert data["periods"][0] == "2026-11-08" and data["periods"][1] == "2026-11-01"
    ended = [p for p, part in zip(data["period"], data["part"]) if part != "now"]
    assert ended == sorted(ended, reverse=True) and ended[-1] == 0
    assert [p for p, part in zip(data["period"], data["part"]) if part == "now"] == [None]
    assert data["group"] is None and c["people"] == 7
    assert c["busiest"]["1"] == pytest.approx(200 / 1400, abs=1e-3) and c["busiest"]["5"] == pytest.approx(1000 / 1400, abs=1e-3)
    days14, _ = D.build(_db(tmp_path / "d.db"), _plan(rolling=4, rolling_days=14), NOW)
    assert days14["periods"][1] == "2026-10-18"


def test_people_dealt_into_groups_and_never_named(tmp_path):
    c = _db(tmp_path / "c.db", array=200)
    data, _ = D.build(c, _plan(people_folds=3), NOW)
    g = [x for x, p in zip(data["group"], data["part"]) if p != "now"]
    assert set(g) == {0, 1, 2}
    who = {}
    for (i, jid, site), x in zip(D.build(c, _plan(people_folds=3), NOW)[1], data["group"]):
        if x is not None:
            who.setdefault(x, set()).add(jid)
    assert all(len(v) > 0 for v in who.values())
    # kim (the busiest, with the arrays) alone in group 0, the next busiest in 1 and 2
    assert D.groups([(None, None, None, {"user_name": u}) for u in ["kim"] * 10 + ["pat"] * 4 + ["ann"] * 3 + ["lee"]],
                    3) == {"kim": 0, "pat": 1, "ann": 2, "lee": 2}
    assert D.groups([], 3) == {} and D.groups([(None, None, None, {"user_name": "kim"})], 0) == {}
    text = json.dumps(data)
    assert not any(f'"{p}"' in text for p in PEOPLE)               # numbers leave the hub, never who


def test_each_period_held_out_in_turn_learns_only_from_before(tmp_path):
    data, _ = D.build(_db(tmp_path / "c.db"), _plan(rolling=3, compare=True), NOW)
    out = T.run(data, "cpu")["summary"]["targets"]["failure"]
    r = out["rolling"]
    assert r["days"] == 7 and [m["start"] for m in r["periods"]] == [data["periods"][2], data["periods"][1],
                                                                     data["periods"][0]]
    period = np.array([-1 if v is None else v for v in data["period"]])
    for m in r["periods"]:
        k = data["periods"].index(m["start"])
        assert m["train"] == int((period > k).sum()) and m["test"] == int((period == k).sum())
        assert m["auc"]["baseline"] > 0.6 and m["auc"]["tessera"] is None
        assert m["interval"]["resamples"] > 0 and m["interval"]["difference"] is None
        assert m["without_place"]["auc"]["baseline"] is not None and m["place_alone"]["auc"]["baseline"] is not None
    assert r["over"]["tests"] == 3 and r["over_without_place"]["tests"] == 3
    assert r["over"]["auc"]["baseline"] == pytest.approx(np.mean([m["auc"]["baseline"] for m in r["periods"]]), abs=1e-4)
    data["plan"]["rolling"] = 0
    assert "rolling" not in T.run(data, "cpu")["summary"]["targets"]["failure"]


def _fake_tessera(monkeypatch):
    fake = types.ModuleType("tessera.models")
    fake.TesseraClassifier = object
    monkeypatch.setitem(sys.modules, "tessera", types.ModuleType("tessera"))
    monkeypatch.setitem(sys.modules, "tessera.models", fake)
    monkeypatch.setattr(T, "_device", lambda d: "cpu")
    calls = []

    class Clf:
        component_weights = {"gnn": 1.0}

    def tessera(Xf, y, train, window, plan, used):
        calls.append((len(Xf), int(train.sum()), int((~train).sum())))
        rng = np.random.default_rng(len(Xf))
        return np.where(train, 0.5, y + rng.normal(0, 0.8, len(y))).astype(float), Clf()
    monkeypatch.setattr(T, "_tessera", tessera)
    return calls


def test_tessera_sees_no_later_job_and_none_running(tmp_path, monkeypatch):
    data, _ = D.build(_db(tmp_path / "c.db"), _plan(rolling=2, engine="tessera", compare=False), NOW)
    calls = _fake_tessera(monkeypatch)
    out = T.run(data, "cpu")["summary"]["targets"]["failure"]
    periods = out["rolling"]["periods"]
    assert calls[0][0] == len(data["rows"])                         # the main holdout: every job, the waiting one too
    assert [(m["train"] + m["test"], m["train"], m["test"]) for m in periods] == calls[1:]
    assert all(m["auc"]["tessera"] is not None and m["interval"]["difference"] is not None for m in periods)
    sides = [T._side(m["interval"]) for m in periods]
    assert out["rolling"]["over"]["clear"] == {k: sides.count(k) for k in ("above", "below", "spans")}


def test_new_people_are_tested_on_models_that_never_saw_them(tmp_path, monkeypatch):
    c = _db(tmp_path / "c.db")
    data, mapping = D.build(c, _plan(people_folds=3, engine="tessera", compare=False), NOW)
    calls = _fake_tessera(monkeypatch)
    out = T.run(data, "cpu")["summary"]["targets"]["failure"]
    groups = out["people"]["groups"]
    assert [m["group"] for m in groups] == [1, 2, 3]
    grp, part = np.array([-1 if v is None else v for v in data["group"]]), np.array(data["part"])
    for m, call in zip(groups, calls[1:]):
        g = m["group"] - 1
        assert m["test"] == int(((grp == g) & (part == "holdout")).sum())
        assert m["train"] == int(((grp != g) & (grp >= 0) & (part == "train")).sum())
        assert call == (m["train"] + m["test"], m["train"], m["test"])  # the network: these, nothing else
    # whose jobs were in each test: none of them among those learned from
    users = {r["job_id"]: r["user_name"] for r in c.execute("SELECT job_id, user_name FROM jobs")}
    for g in range(3):
        tested = {users[mapping[i][1]] for i in range(len(mapping)) if grp[i] == g and part[i] == "holdout"}
        learned = {users[mapping[i][1]] for i in range(len(mapping)) if grp[i] not in (g, -1) and part[i] == "train"}
        assert tested and not tested & learned
    assert out["people"]["over"]["tests"] == 3


def test_a_test_that_cant_be_measured_says_why():
    rng = np.random.default_rng(5)
    n = 400
    y = (rng.random(n) < 0.3).astype(int)
    period = np.repeat(np.arange(3, -1, -1), n // 4)                 # oldest first
    y[period == 0] = 0                                               # the newest: none did it
    X = np.column_stack([rng.random(n) + y, np.zeros(n), np.ones(n), np.ones(n), np.zeros(n), np.zeros(n),
                         rng.integers(0, 2, n), np.zeros(n)])
    part = np.array(["train"] * n)
    got = T._rolling(X, y, part, period, ["d0", "d1", "d2", "d3"], period, {"rolling": 4}, "baseline", None,
                     "failure", lambda s: None)
    m = got["periods"]
    assert [x["start"] for x in m] == ["d3", "d2", "d1", "d0"]
    assert "too few" in m[0]["skipped"] and m[0]["train"] == 0
    assert "auc" in m[1] and "auc" in m[2]
    assert "one outcome" in m[3]["skipped"] and m[3]["rate_test"] == 0.0
    assert got["over"]["tests"] == 2
    empty = T._fold(X, y, period > 0, period > 9, period, {}, "baseline", None)
    assert empty["skipped"] == "no jobs to test" and empty["test"] == 0


def test_the_hub_keeps_every_test_as_the_worker_sent_it(tmp_path):
    data, _ = D.build(_db(tmp_path / "c.db"), _plan(rolling=2, people_folds=2, compare=True), NOW)
    summary = T.run(data, "cpu")["summary"]
    kept = R._clean_summary(summary)
    assert kept["targets"]["failure"]["rolling"] == summary["targets"]["failure"]["rolling"]
    assert kept["targets"]["failure"]["people"] == summary["targets"]["failure"]["people"]
    lo, hi = kept["targets"]["failure"]["people"]["groups"][-1]["without_place"]["interval"]["baseline"]
    assert 0 < lo <= hi <= 1                                         # 8 levels down: still there
    assert kept["jobs"]["busiest"] == summary["jobs"]["busiest"]


def test_the_cli_says_each_test_and_the_preview_counts(tmp_path):
    one = {"start": "2026-11-03", "train": 700, "test": 150, "rate_test": 0.05, "auc": {"tessera": 0.91, "baseline": 0.86},
           "interval": {"difference": [0.01, 0.08]},
           "without_place": {"auc": {"tessera": 0.9, "baseline": 0.8}, "interval": {"difference": [0.05, 0.15]}}}
    over = {"tests": 1, "auc": {"tessera": 0.91, "baseline": 0.86}, "difference": {"mean": 0.05, "low": 0.05, "high": 0.05},
            "clear": {"above": 1, "below": 0, "spans": 0}}
    x = {"train": 900, "holdout": 200, "rate_holdout": 0.06, "auc": {"tessera": 0.9, "baseline": 0.87},
         "top_tenth": {"tessera": 0.38, "baseline": 0.32},
         "rolling": {"days": 7, "periods": [one, {"start": "2026-11-10", "train": 850, "test": 160, "rate_test": 0.0,
                                                  "skipped": "its jobs had one outcome only"}], "over": over},
         "people": {"groups": [{**one, "group": 1}], "over": over}}
    text = "\n".join(C._target_lines({"targets": {"failure": x}}))
    assert "period by period (7 days each" in text
    assert "from 2026-11-03: 150 tested (5% failed); TESSERA 0.91, logistic 0.86, difference +0.01 to +0.08; without " \
           "partition and cluster TESSERA 0.90, logistic 0.80, difference +0.05 to +0.15" in text
    assert "from 2026-11-10: 160 tested (0% failed): its jobs had one outcome only" in text
    assert "with everything, over 1 periods: TESSERA 0.91, logistic 0.86 on average; the difference +0.05 " \
           "(+0.05 to +0.05), clear above zero in 1, below in 0, spans zero in 0" in text
    assert "new people" in text and "group 1: 150 tested" in text and "over 1 groups" in text
    idle = C._target_lines({"targets": {"idle": {**x, "rolling": None, "people": None, "rate_holdout": 0.59}}})
    assert "59% of them left most of their cores idle" in idle[0]

    plan = _plan(thin=3, rolling=2, people_folds=3, predicts=["failure"])
    data, _ = D.build(_db(tmp_path / "c.db", array=100), plan, NOW)
    lines = C.preview_lines(data, plan)
    said = "\n".join(lines)
    assert lines[0] == f"Jobs: {1409:,} that ended, and 1 running or waiting (scored, not tested)."
    assert "thin = 3: 291 more left out as repeats" in said
    assert "they ended from 2026-09-05 to 2026-11-14; 7 people" in said
    assert "period by period (7 days): from 2026-11-01" in said and "new people: group 1" in said
    assert not any(re.search(rf"\b{p}\b", said) for p in PEOPLE)


def test_nomad_ml_preview_on_the_hub(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nomad.cli import cli
    _db(tmp_path / "combined.db").close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "{tmp_path / "combined.db"}"\nsites = [{{ name = "s", host = "localhost" }}]\n'
                   '[ml.train]\nenabled = true\npredicts = ["failure"]\nthin = 3\nrolling = 2\npeople_folds = 2\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "ml", "preview"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("Jobs: ") and "period by period" in r.output and "new people" in r.output
    cfg.write_text('[hub]\noutput_db = "/nowhere/combined.db"\n[ml.train]\nenabled = true\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "ml", "preview"])
    assert r.exit_code != 0 and "no combined database" in r.output
