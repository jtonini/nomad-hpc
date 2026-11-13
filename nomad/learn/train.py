# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The worker's training (1.45.0): TESSERA and a logistic regression on the
same jobs, measured on the same newest jobs neither learned from.

``run(data, device)`` takes the dataset the hub sent (nomad.learn.dataset)
and gives back the result: a summary (per target: how many jobs, how often
the outcome happened, and on the holdout each model's AUC and how often the
outcome happened among the tenth it ranked riskiest) and the scores of the
holdout's jobs and of those running or waiting, by row.

Per target, the jobs it is asked of (failure: every ended job; waste: the
completed ones), plus those running or waiting. TESSERA is fitted with the
training part's outcomes and every other job's as unknown (label -1), so the
holdout's scores are an honest test; the logistic regression is trained on
the training part only.
"""
from __future__ import annotations

import math
import time

LOG_FEATURES = (0, 1, 2, 3)          # cores, GPUs, memory, time asked: log1p before TESSERA's binning
CATEGORICAL = (6, 7)                 # partition, cluster: one-hot for the logistic regression
MIN_TRAIN = 50


class TrainError(RuntimeError):
    """Training can't go on, and why, in words."""


def _versions() -> dict:
    from importlib.metadata import PackageNotFoundError, version
    out = {}
    for name in ("nomad-hpc", "tessera-ml", "torch", "torch-geometric", "scikit-learn"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def _device(device: str) -> str:
    try:
        import torch
    except ImportError:
        raise TrainError("TESSERA needs PyTorch here: pip install 'nomad-hpc[predict]'") from None
    if device == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise TrainError("no CUDA GPU PyTorch can use here (torch.cuda.is_available() is False): fix the driver, "
                         "or set [ml.worker] device = \"cpu\" (much slower)")
    return device


def _auc(y, p):
    from sklearn.metrics import roc_auc_score
    try:
        return round(float(roc_auc_score(y, p)), 4) if 0 < y.sum() < len(y) else None
    except ValueError:
        return None


def _top_tenth(y, p) -> float | None:
    """How often the outcome happened among the tenth ranked riskiest."""
    import numpy as np
    if len(y) < 10:
        return None
    k = max(1, math.ceil(len(y) / 10))
    order = np.argsort(-p, kind="stable")[:k]
    return round(float(y[order].mean()), 4)


def _baseline_features(X):
    import numpy as np
    cols = [np.log1p(np.clip(X[:, c], 0, None)) if c in LOG_FEATURES else X[:, c]
            for c in range(X.shape[1]) if c not in CATEGORICAL]
    for c in CATEGORICAL:
        values = np.unique(X[:, c])
        if len(values) > 1:
            cols += [(X[:, c] == v).astype(float) for v in values[1:]]
    return np.column_stack(cols)


def _tessera_features(X):
    import numpy as np
    out = X.astype(float).copy()
    for c in LOG_FEATURES:
        out[:, c] = np.log1p(np.clip(out[:, c], 0, None))
    return out


def run(data: dict, device: str = "cuda", progress=lambda text: None) -> dict:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    plan = data["plan"]
    engine = plan["engine"]
    epochs = plan.get("epochs") or {}
    X = np.asarray(data["rows"], dtype=float)
    part = np.asarray(data["part"])
    window = np.asarray(data["window"], dtype=int)
    if X.ndim != 2 or X.shape[0] != len(part) or X.shape[0] != len(window):
        raise TrainError("the dataset's rows, parts and windows don't match")
    used = None
    if engine == "tessera":
        used = _device(device)
        try:
            import warnings
            with warnings.catch_warnings():          # torch.jit.script's notice, from a dependency: not ours to act on
                warnings.simplefilter("ignore", FutureWarning)
                from tessera.models import TesseraClassifier
        except ImportError:
            raise TrainError("TESSERA isn't installed here: pip install 'nomad-hpc[predict]' "
                             "(tessera-ml[ml] 0.3 or later)") from None
    started = time.time()
    summary = {"engine": engine, "device": used, "versions": _versions(), "jobs": data.get("counts"),
               "partitions": data.get("partitions"), "clusters": data.get("clusters"), "targets": {}}
    scores = {}
    for target in plan["predicts"]:
        t0 = time.time()
        lab = data["labels"][target]
        y = np.array([-1 if v is None else int(v) for v in lab], dtype=int)
        pop = np.where((y >= 0) | (part == "now"))[0]
        yp, pp = y[pop], part[pop]
        train = (pp == "train") & (yp >= 0)
        hold = (pp == "holdout") & (yp >= 0)
        out = {"jobs": int(len(pop)), "train": int(train.sum()), "holdout": int(hold.sum()),
               "now": int((pp == "now").sum()), "rate_train": round(float(yp[train].mean()), 4) if train.any() else None,
               "rate_holdout": round(float(yp[hold].mean()), 4) if hold.any() else None}
        if train.sum() < MIN_TRAIN or len(set(yp[train].tolist())) < 2:
            out["skipped"] = f"too few jobs to learn from (fewer than {MIN_TRAIN}, or one outcome only)"
            summary["targets"][target] = out
            continue
        progress(f"{target}: the logistic regression")
        Xb = _baseline_features(X[pop])
        base_model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced"))
        base_model.fit(Xb[train], yp[train])
        base = base_model.predict_proba(Xb)[:, 1]
        risk = base
        if engine == "tessera":
            progress(f"{target}: TESSERA on {len(pop):,} jobs ({used})")
            clf = TesseraClassifier(gnn_epochs=int(epochs.get("gnn", 100)), lstm_epochs=int(epochs.get("lstm", 60)),
                                    ae_epochs=int(epochs.get("ae", 60)), device=used)
            clf.fit(_tessera_features(X[pop]), np.where(train, yp, -1), time_windows=window[pop])
            risk = np.asarray(clf.predict_proba(), dtype=float)
            out["weights"] = {k: round(float(v), 4) for k, v in clf.component_weights.items()}
            out["similarity"] = getattr(clf, "similarity_backend_", None)
        yh = yp[hold]
        out["auc"] = {"tessera": _auc(yh, risk[hold]) if engine == "tessera" else None,
                      "baseline": _auc(yh, base[hold])}
        out["top_tenth"] = {"tessera": _top_tenth(yh, risk[hold]) if engine == "tessera" else None,
                            "baseline": _top_tenth(yh, base[hold])}
        out["seconds"] = round(time.time() - t0, 1)
        summary["targets"][target] = out
        keep = np.where(pp != "train")[0]
        scores[target] = [{"idx": int(pop[i]), "part": str(pp[i]),
                           "risk": None if not np.isfinite(risk[i]) else round(float(risk[i]), 5),
                           "baseline": round(float(base[i]), 5),
                           "label": int(yp[i]) if yp[i] >= 0 else None}
                          for i in keep if pp[i] in ("holdout", "now")]
    summary["seconds"] = round(time.time() - started, 1)
    return {"summary": summary, "scores": scores}


__all__ = ["run", "TrainError"]
