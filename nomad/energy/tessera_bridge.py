# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Thin adapter between nomad's energy data and TESSERA's classifier.

Predicts which job submissions will waste energy, from request-time features
only (no runtime/outcome leakage), so the prediction is actionable BEFORE a
job runs: "this submission looks like it will reserve far more than it uses."

Two backends:
    tessera   the full TesseraClassifier (similarity network + GNN/LSTM/AE
              blend). The differentiator; topology carries signal a direct
              classifier misses. Requires the optional dependency:
                  pip install 'nomad-hpc[predict]'
    baseline  logistic regression on the same features. Always available,
              genuinely useful, and the comparison point that makes the
              topology gain visible.

TESSERA is an OPTIONAL dependency. Feature extraction and the baseline never
import it; only the tessera backend does, lazily, and a clean install hint is
raised if it is absent. We never silently substitute baseline for tessera --
if you ask for the full model and it is missing, we say so.

Features (request-time, what the scheduler sees before the job runs):
    req_cpus, req_gpus, req_time_h, req_mem_gb, hour_of_day, partition_index
Label (post-hoc, for training):
    high-waste = more than `waste_threshold` of reserved walltime went unused.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np


FEATURE_NAMES = [
    "req_cpus", "req_gpus", "req_time_h", "req_mem_gb", "hour_of_day", "partition_idx",
]


@dataclass
class JobPrediction:
    job_id: str
    user: str
    partition: str
    risk: float            # 0..1 risk score (ranking, not calibrated probability)
    was_high_waste: bool   # observed label (for context/eval, not used at predict)


@dataclass
class PredictionResult:
    method: str                 # "tessera" | "baseline"
    predictions: list           # list[JobPrediction], unsorted
    label_rate: float           # observed high-waste fraction in the data
    n_jobs: int
    auc: float | None = None    # in-sample/CV AUC where available (context only)
    baseline_auc: float | None = None   # logistic CV-AUC, computed alongside tessera for comparison
    component_weights: dict | None = None   # tessera blend weights, if applicable

    def ranked(self, top: int | None = None) -> list:
        out = sorted(self.predictions, key=lambda p: p.risk, reverse=True)
        return out[:top] if top else out


# ── feature extraction (always available, no TESSERA) ─────────────────────
def extract_job_features(
    db_path: str,
    cluster_name: str | None = None,
    waste_threshold: float = 0.6,
):
    """Build the (features, labels, metadata) arrays from completed jobs.

    Request-time features only; the label is computed post-hoc from the
    observed over-request ratio but never enters the feature matrix.
    Returns (X, y, meta, partitions).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        clause = "AND cluster = ?" if cluster_name else ""
        params = [cluster_name] if cluster_name else []
        rows = conn.execute(
            f"""SELECT job_id, user_name, partition, req_cpus, req_gpus,
                       req_time_seconds, runtime_seconds, req_mem_mb, start_time
                FROM jobs
                WHERE state = 'COMPLETED' AND req_time_seconds > 0
                  AND runtime_seconds > 0 {clause}""",
            params,
        ).fetchall()
    finally:
        conn.close()

    partitions = sorted({(r["partition"] or "") for r in rows})
    X, y, meta = [], [], []
    for r in rows:
        req_time_h = r["req_time_seconds"] / 3600.0
        run_h = r["runtime_seconds"] / 3600.0
        over_ratio = max(0.0, 1.0 - run_h / req_time_h)
        ts = str(r["start_time"]) if r["start_time"] else ""
        hour = int(ts[11:13]) if len(ts) >= 13 and ts[11:13].isdigit() else 12
        X.append([
            r["req_cpus"] or 0,
            r["req_gpus"] or 0,
            req_time_h,
            (r["req_mem_mb"] or 0) / 1024.0,
            hour,
            partitions.index(r["partition"] or ""),
        ])
        y.append(1 if over_ratio > waste_threshold else 0)
        meta.append((str(r["job_id"]), r["user_name"] or "", r["partition"] or ""))
    return np.array(X, dtype=float), np.array(y, dtype=int), meta, partitions


# ── baseline backend (always available) ───────────────────────────────────
def _baseline_cv_auc(X, y) -> float | None:
    """5-fold CV AUC of the logistic baseline on the same features."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.model_selection import cross_val_score
    if not (0 < y.sum() < len(y)):
        return None
    model = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=500, class_weight="balanced"))
    try:
        return float(cross_val_score(model, X, y, cv=5, scoring="roc_auc").mean())
    except Exception:
        return None


def _predict_baseline(X, y, meta) -> PredictionResult:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    from sklearn.model_selection import cross_val_score

    model = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=500, class_weight="balanced"))
    auc = None
    if 0 < y.sum() < len(y):
        try:
            auc = float(cross_val_score(model, X, y, cv=5, scoring="roc_auc").mean())
        except Exception:
            auc = None
    model.fit(X, y)
    risks = model.predict_proba(X)[:, 1]
    preds = [JobPrediction(m[0], m[1], m[2], float(risks[i]), bool(y[i]))
             for i, m in enumerate(meta)]
    return PredictionResult("baseline", preds, float(y.mean()), len(y), auc=auc)


# ── tessera backend (optional dependency) ─────────────────────────────────
def _predict_tessera(X, y, meta) -> PredictionResult:
    try:
        from tessera.models import TesseraClassifier
    except ImportError as exc:
        raise ImportError(
            "Energy waste prediction with the full model requires TESSERA.\n"
            "Install it with:  pip install 'nomad-hpc[predict]'\n"
            "Or run the lightweight baseline:  nomad energy predict --method baseline"
        ) from exc

    from sklearn.metrics import roc_auc_score

    clf = TesseraClassifier(gnn_epochs=100, lstm_epochs=60, ae_epochs=60)
    # per-job snapshot: no temporal sequence per node, so no time_windows ->
    # the facade runs GNN + AE and blends those two.
    clf.fit(X, y)
    risks = clf.predict_proba()
    auc = None
    if 0 < y.sum() < len(y):
        try:
            auc = float(roc_auc_score(y, risks))   # in-sample; context only
        except Exception:
            auc = None
    base_auc = _baseline_cv_auc(X, y)   # cheap; lets the CLI report the honest delta
    preds = [JobPrediction(m[0], m[1], m[2], float(risks[i]), bool(y[i]))
             for i, m in enumerate(meta)]
    return PredictionResult("tessera", preds, float(y.mean()), len(y),
                            auc=auc, baseline_auc=base_auc,
                            component_weights=clf.component_weights)


# ── public entry point (called by the CLI) ────────────────────────────────
def predict_energy_waste(
    db_path: str,
    config: dict | None = None,
    cluster_name: str | None = None,
    method: str = "tessera",
    waste_threshold: float = 0.6,
) -> PredictionResult:
    """Predict per-job energy-waste risk from request-time features.

    method='tessera' (default) uses the full TesseraClassifier and raises a
    clean install hint if the optional dependency is absent. method='baseline'
    uses logistic regression and is always available.
    """
    X, y, meta, _ = extract_job_features(db_path, cluster_name, waste_threshold)
    if len(y) == 0:
        return PredictionResult(method, [], 0.0, 0)
    if method == "baseline":
        return _predict_baseline(X, y, meta)
    return _predict_tessera(X, y, meta)
