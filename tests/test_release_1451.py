# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.45.1: a fair comparison of TESSERA and the logistic regression.

Both models get partition and cluster as one column per value (1.45.0 gave
TESSERA their numbers); TESSERA's similarity comes from [ml.train.tessera];
with compare, each model is also measured without partition and cluster, and
the logistic regression with them alone; waste carries the spread of how
much of their asked time completed jobs used.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta

import numpy as np
import pytest

from nomad.learn import dataset as D
from nomad.learn import train as T
from nomad.learn.settings import Settings, settings

NOW = datetime(2026, 11, 12, 12, 0, 0)


def _data(n=600, seed=1, place_decides=True, engine="baseline", compare=True):
    """Made-up rows: failure set by partition (place_decides) or by cores asked."""
    rng = np.random.default_rng(seed)
    rows, labels, parts, windows = [], [], [], []
    n_hold = n // 5
    for i in range(n):
        partition = int(rng.integers(0, 4))
        cluster = int(rng.integers(0, 2))
        cpus = float(rng.choice([1, 4, 16, 64]))
        bad = (partition == 2) if place_decides else (cpus >= 16)
        bad = bad if rng.random() > 0.05 else not bad
        rows.append([cpus, 0.0, 4.0, float(rng.choice([1, 4, 24])), float(i % 24), float(i % 7),
                     float(partition), float(cluster)])
        labels.append(int(bad))
        parts.append("train" if i < n - n_hold else "holdout")
        windows.append(i // 60)
    plan = {**Settings(enabled=True, engine=engine, compare=compare).plan(), "predicts": ["failure"]}
    return {"features": list(D.FEATURES), "rows": rows, "window": windows, "part": parts,
            "labels": {"failure": labels, "waste": [None] * n}, "plan": plan,
            "counts": {"train": n - n_hold, "holdout": n_hold, "now": 0}, "partitions": 4, "clusters": 2}


def test_settings_carry_the_comparison_and_tesseras_similarity():
    s = settings({"ml": {"train": {}}})
    assert s.compare is True and s.tessera == {"similarity": "euclidean", "n_bins": 3, "k_neighbors": 10}
    plan = s.plan()
    assert plan["compare"] is True and plan["tessera"]["similarity"] == "euclidean"
    s = settings({"ml": {"train": {"compare": "yes", "tessera": {"similarity": "x", "k_neighbors": 1}}}})
    assert s.compare is True and s.tessera["similarity"] == "euclidean" and s.tessera["k_neighbors"] == 10
    assert len(s.problems) == 3


def test_partition_and_cluster_are_one_column_per_value_for_both_models():
    X = np.array([[1, 0, 4, 1, 0, 0, 0, 0], [4, 0, 4, 1, 0, 0, 3, 0], [16, 0, 4, 1, 0, 0, 3, 0]], dtype=float)
    place = T._place(X)
    assert place.shape == (3, 2)                     # partition 0 and 3; one cluster: no column
    assert place.tolist() == [[1, 0], [0, 1], [0, 1]]
    assert T._features(X, "all").shape == (3, 6 + 2)
    assert T._features(X, "without_place").shape == (3, 6)
    assert T._features(X, "place_alone").shape == (3, 2)
    assert T._numbers(X)[1, 0] == pytest.approx(np.log1p(4))


def test_the_comparison_says_what_partition_and_cluster_carry():
    out = T.run(_data(place_decides=True), "cpu")["summary"]["targets"]["failure"]
    assert out["auc"]["baseline"] > 0.85
    assert out["place_alone"]["auc"]["baseline"] > 0.85          # where a job ran says it all
    assert out["without_place"]["auc"]["baseline"] < 0.65        # what was asked says little
    out = T.run(_data(place_decides=False), "cpu")["summary"]["targets"]["failure"]
    assert out["without_place"]["auc"]["baseline"] > 0.85
    assert out["place_alone"]["auc"]["baseline"] < 0.65


def test_no_comparison_when_off():
    out = T.run(_data(compare=False), "cpu")["summary"]["targets"]["failure"]
    assert "without_place" not in out and "place_alone" not in out


def test_tessera_gets_the_same_columns_and_its_similarity(monkeypatch):
    fits = []

    class Fake:
        def __init__(self, **kw):
            self.kw = kw

        def fit(self, X, y, time_windows=None):
            fits.append((self.kw, X.shape, int((y == -1).sum())))
            self._n = len(X)
            self.similarity_backend_ = "sparse"
            return self

        def predict_proba(self):
            return np.linspace(0, 1, self._n)

        component_weights = {"gnn": 0.5, "lstm": 0.3, "ae": 0.2}
    fake = types.ModuleType("tessera.models")
    fake.TesseraClassifier = Fake
    monkeypatch.setitem(sys.modules, "tessera", types.ModuleType("tessera"))
    monkeypatch.setitem(sys.modules, "tessera.models", fake)
    monkeypatch.setattr(T, "_device", lambda device: "cpu")
    data = _data(engine="tessera")
    data["plan"]["tessera"] = {"similarity": "cosine", "n_bins": 5, "k_neighbors": 15}
    out = T.run(data, "cpu")
    (kw, shape, unknown), (kw2, shape2, _) = fits
    assert kw["similarity_method"] == "cosine" and kw["n_bins"] == 5 and kw["k_neighbors"] == 15
    assert shape == (600, 6 + 4 + 2)                  # what was asked, 4 partitions, 2 clusters
    assert shape2 == (600, 6)                         # and without them
    assert unknown == 120                             # the holdout, given as unknown
    f = out["summary"]["targets"]["failure"]
    assert f["without_place"]["auc"]["tessera"] is not None and f["similarity"] == "sparse"
    assert out["summary"]["tessera"]["similarity"] == "cosine"


def test_the_spread_of_used_time_is_counts_only():
    class Row(dict):
        pass
    ended = []
    for i in range(100):
        r = Row(runtime_seconds=(i + 1) * 36, req_time_seconds=3600)      # 1% .. 100% used
        ended.append((NOW, NOW - timedelta(hours=2), "COMPLETED", r))
    ended.append((NOW, NOW, "FAILED", Row(runtime_seconds=10, req_time_seconds=3600)))
    got = D.used_spread(ended)
    assert got["jobs"] == 100
    assert got["used_at"]["0.5"] == pytest.approx(0.51) and got["used_at"]["0.9"] == pytest.approx(0.91)
    assert got["wasted_at"]["0.6"] == pytest.approx(0.39) and got["wasted_at"]["0.9"] == pytest.approx(0.09)
    assert D.used_spread(ended[:10]) is None                             # too few to say


def test_the_waste_card_carries_the_spread(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / "combined.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE jobs (job_id TEXT, partition TEXT, req_cpus INTEGER, req_gpus INTEGER, req_mem_mb INTEGER, "
              "req_time_seconds INTEGER, runtime_seconds INTEGER, submit_time TEXT, end_time TEXT, state TEXT, "
              "source_site TEXT)")
    for i in range(300):
        sub = NOW - timedelta(days=200) + timedelta(hours=i * 10)
        ran = 3600 * (1 + i % 10)                                         # 10% .. 100% of 10 h used
        c.execute("INSERT INTO jobs VALUES (?, 'p', 4, 0, 4096, 36000, ?, ?, ?, ?, 's')",
                  (str(i), ran, sub.isoformat(), (sub + timedelta(seconds=ran)).isoformat(),
                   "COMPLETED" if i % 7 else "FAILED"))
    data, _ = D.build(c, {**Settings(enabled=True, engine="baseline").plan()}, NOW)
    spread = data["spread"]["waste"]
    assert spread["jobs"] == sum(1 for i in range(300) if i % 7) and set(spread["used_at"]) == {"0.1", "0.25", "0.5",
                                                                                               "0.75", "0.9"}
    out = T.run(data, "cpu")["summary"]["targets"]["waste"]
    assert out["spread"] == spread
    assert "spread" not in T.run(data, "cpu")["summary"]["targets"]["failure"]


def test_readiness_points_to_train():
    from nomad.ml import estimator
    src = open(estimator.__file__, encoding="utf-8").read()
    assert "run: nomad train\"" not in src and "Train… under The model" in src


def test_the_hub_keeps_the_whole_result(tmp_path, monkeypatch):
    """A 1.45.1 summary is five levels deep (targets > failure > without_place > auc > tessera)."""
    from nomad.learn import records as R
    monkeypatch.setattr(R.os, "geteuid", lambda: 1000)
    data = _data(engine="baseline")
    out = T.run(data, "cpu")
    s = settings({"ml": {"train": {"enabled": True, "engine": "baseline", "db": str(tmp_path / "ml.db")}}})
    rec = R.open_records(tmp_path / "ml.db", write=True)
    rid = R.ask(rec, s, "adam", None)["id"]
    R.claim(rec, s, "w")
    out["summary"]["targets"]["failure"]["spread"] = {"jobs": 30, "used_at": {"0.5": 0.1}, "wasted_at": {"0.6": 0.9}}
    R.finish(rec, s, rid, "w", {"summary": out["summary"], "scores": {}})
    kept = R.status(rec, s)["last"]["summary"]["targets"]["failure"]
    assert kept["without_place"]["auc"]["baseline"] == out["summary"]["targets"]["failure"]["without_place"]["auc"]["baseline"]
    assert kept["place_alone"]["auc"]["baseline"] is not None
    assert kept["spread"]["used_at"] == {"0.5": 0.1}
