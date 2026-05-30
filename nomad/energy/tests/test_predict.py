# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the energy waste-prediction bridge (baseline path, torch-free).

The tessera backend needs the optional dependency and is exercised on a real
box; here we lock the parts that must hold without torch: request-time feature
extraction (no outcome leakage), label balance, the baseline ranking, the
honest comparison line, and the optional-import guard.
"""
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest

from nomad.energy.tessera_bridge import (
    extract_job_features, predict_energy_waste, FEATURE_NAMES,
    PredictionResult, JobPrediction,
)
from nomad.energy.formatters import format_prediction_cli


@pytest.fixture
def jobs_db(tmp_path):
    """Jobs with a clear request->waste signal: big walltime over-request
    is high-waste; tight requests are not."""
    path = tmp_path / "predict.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (job_id TEXT, cluster TEXT, user_name TEXT, partition TEXT,
            node_list TEXT, state TEXT, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
            req_time_seconds INTEGER, runtime_seconds INTEGER, start_time DATETIME,
            end_time DATETIME, PRIMARY KEY (job_id, cluster));
    """)
    base = datetime(2026, 4, 1)
    jid = 0
    for i in range(200):
        jid += 1
        waste = i % 3 == 0
        runtime = 3600
        req_time = runtime * (5 if waste else 1)     # wasteful jobs over-request 5x
        cpus = 16 if waste else 4
        start = base + timedelta(hours=jid)
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(jid), "test", f"u{i%5}", "gpu" if i % 2 else "compute", "n01",
             "COMPLETED", cpus, cpus*4000, 1 if i % 2 else 0,
             req_time, runtime, start.isoformat(), (start+timedelta(seconds=runtime)).isoformat()))
    conn.commit(); conn.close()
    return str(path)


def test_feature_extraction_shape_and_no_leakage(jobs_db):
    X, y, meta, parts = extract_job_features(jobs_db, cluster_name="test")
    assert X.shape[1] == len(FEATURE_NAMES) == 6
    assert len(y) == len(meta) == X.shape[0] == 200
    # label is non-degenerate
    assert 0 < y.sum() < len(y)
    # features are request-time only: each column matches a request field read
    # straight from the db, never a runtime/outcome-derived quantity.
    conn = sqlite3.connect(jobs_db); conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT req_cpus, req_gpus, req_time_seconds FROM jobs "
                        "WHERE cluster='test' ORDER BY rowid").fetchall()
    conn.close()
    # col 0 = req_cpus, col 1 = req_gpus, col 2 = req_time_h (request fields only)
    assert np.allclose(X[:, 0], [r["req_cpus"] for r in rows])
    assert np.allclose(X[:, 1], [r["req_gpus"] for r in rows])
    assert np.allclose(X[:, 2], [r["req_time_seconds"] / 3600 for r in rows])


def test_baseline_ranks_wasteful_high(jobs_db):
    res = predict_energy_waste(jobs_db, cluster_name="test", method="baseline")
    assert res.method == "baseline"
    assert res.n_jobs == 200
    top = res.ranked(top=20)
    # most of the top-ranked should be genuinely high-waste
    assert sum(p.was_high_waste for p in top) >= 14
    # risks are sorted descending
    risks = [p.risk for p in top]
    assert risks == sorted(risks, reverse=True)


def test_tessera_missing_raises_clean_hint(jobs_db, monkeypatch):
    # simulate tessera absent regardless of environment
    import builtins
    real_import = builtins.__import__
    def fake(name, *a, **k):
        if name.startswith("tessera"):
            raise ImportError("no tessera")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ImportError) as exc:
        predict_energy_waste(jobs_db, cluster_name="test", method="tessera")
    msg = str(exc.value)
    assert "nomad-hpc[predict]" in msg and "--method baseline" in msg


def test_comparison_line_three_regimes():
    preds = [JobPrediction(str(i), "u", "compute", 0.5, False) for i in range(3)]
    def line(auc, base):
        r = PredictionResult("tessera", preds, 0.18, 100, auc=auc, baseline_auc=base,
                             component_weights={"gnn": 0.5, "ae": 0.5})
        return format_prediction_cli(r, top=3)
    assert "improves on baseline" in line(0.80, 0.70)
    assert "no improvement" in line(0.70, 0.70)
    assert "below baseline" in line(0.66, 0.70)


def test_empty_db_returns_empty(tmp_path):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE jobs (job_id TEXT, cluster TEXT, user_name TEXT,
        partition TEXT, node_list TEXT, state TEXT, req_cpus INTEGER, req_mem_mb INTEGER,
        req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER,
        start_time DATETIME, end_time DATETIME)""")
    conn.commit(); conn.close()
    res = predict_energy_waste(str(path), method="baseline")
    assert res.n_jobs == 0 and res.predictions == []
