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

1.45.1: both models get the same columns: what was asked as numbers (cores,
GPUs, memory and time on a log scale; hour and weekday) and partition and
cluster as one column per value (in 1.45.0 TESSERA had them as plain
numbers). TESSERA's similarity, bins and neighbours come from
[ml.train.tessera] (default euclidean on scaled features: simpson cuts each
feature into a few equal-width bins). With [ml.train] compare, each model is
also measured without partition and cluster, and the logistic regression
with them alone: how much of what each model knows is where a job ran. The
waste target carries the spread of how much of their time completed jobs
used (the hub's counts, no job's own figure).

1.46.0: the idle target (most of the cores asked left idle), and with
[ml.train] bootstrap, 95% ranges of each AUC and of the difference between
the models, from resamples of the held-out jobs.
"""
from __future__ import annotations

import math
import time

LOG_FEATURES = (0, 1, 2, 3)          # cores, GPUs, memory, time asked: log1p
CATEGORICAL = (6, 7)                 # partition, cluster: one column per value, for both models (1.45.1)
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


def _intervals(y, a, b, resamples: int, seed: int = 0) -> dict | None:
    """95% ranges of each model's AUC and of their difference (a - b), from ``resamples``
    resamples of the held-out jobs (1.46.0). Paired: each resample's jobs are the same for
    both models, so the difference's range is the one that says whether they differ."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    if not resamples or b is None or len(y) < 20 or not 0 < int(y.sum()) < len(y):
        return None
    rng = np.random.default_rng(seed)
    got_a, got_b, diff = [], [], []
    for _ in range(int(resamples)):
        i = rng.integers(0, len(y), len(y))
        yi = y[i]
        if yi.min() == yi.max():
            continue
        bb = roc_auc_score(yi, b[i])
        got_b.append(bb)
        if a is not None:
            aa = roc_auc_score(yi, a[i])
            got_a.append(aa)
            diff.append(aa - bb)

    def q(v):
        return [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)] if v else None
    return {"resamples": len(got_b), "tessera": q(got_a), "baseline": q(got_b), "difference": q(diff)}


def _top_tenth(y, p) -> float | None:
    """How often the outcome happened among the tenth ranked riskiest."""
    import numpy as np
    if len(y) < 10:
        return None
    k = max(1, math.ceil(len(y) / 10))
    order = np.argsort(-p, kind="stable")[:k]
    return round(float(y[order].mean()), 4)


def _place(X):
    """Partition and cluster as one column per value seen (both models, 1.45.1): they are
    names, numbered; as plain numbers, partition 3 would be "near" partition 4."""
    import numpy as np
    cols = []
    for c in CATEGORICAL:
        values = np.unique(X[:, c])
        if len(values) > 1:
            cols += [(X[:, c] == v).astype(float) for v in values]
    return np.column_stack(cols) if cols else np.zeros((len(X), 0))


def _numbers(X):
    """What was asked, as numbers: cores, GPUs, memory and time on a log scale; hour and day."""
    import numpy as np
    return np.column_stack([np.log1p(np.clip(X[:, c], 0, None)) if c in LOG_FEATURES else X[:, c].astype(float)
                            for c in range(X.shape[1]) if c not in CATEGORICAL])


def _features(X, which: str):
    """The columns each comparison gives the models: all, without partition and cluster, or those alone."""
    import numpy as np
    if which == "without_place":
        return _numbers(X)
    if which == "place_alone":
        return _place(X)
    return np.column_stack([_numbers(X), _place(X)])


def _logistic(Xf, y, train):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, class_weight="balanced"))
    model.fit(Xf[train], y[train])
    return model.predict_proba(Xf)[:, 1]


def _tessera(Xf, y, train, window, plan, used):
    import numpy as np
    from tessera.models import TesseraClassifier
    epochs = plan.get("epochs") or {}
    t = plan.get("tessera") or {}
    clf = TesseraClassifier(gnn_epochs=int(epochs.get("gnn", 100)), lstm_epochs=int(epochs.get("lstm", 60)),
                            ae_epochs=int(epochs.get("ae", 60)), device=used,
                            similarity_method=t.get("similarity", "simpson"), n_bins=int(t.get("n_bins", 3)),
                            k_neighbors=int(t.get("k_neighbors", 10)))
    clf.fit(Xf, np.where(train, y, -1), time_windows=window)
    return np.asarray(clf.predict_proba(), dtype=float), clf


def run(data: dict, device: str = "cuda", progress=lambda text: None) -> dict:
    import numpy as np

    plan = data["plan"]
    engine = plan["engine"]
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
                from tessera.models import TesseraClassifier  # noqa: F401
        except ImportError:
            raise TrainError("TESSERA isn't installed here: pip install 'nomad-hpc[predict]' "
                             "(tessera-ml[ml] 0.3 or later)") from None
    started = time.time()
    summary = {"engine": engine, "device": used, "versions": _versions(), "jobs": data.get("counts"),
               "partitions": data.get("partitions"), "clusters": data.get("clusters"),
               "tessera": plan.get("tessera") if engine == "tessera" else None, "targets": {}}
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
        spread = (data.get("spread") or {}).get(target)
        if spread:
            out["spread"] = spread
        if train.sum() < MIN_TRAIN or len(set(yp[train].tolist())) < 2:
            out["skipped"] = f"too few jobs to learn from (fewer than {MIN_TRAIN}, or one outcome only)"
            summary["targets"][target] = out
            continue
        Xp, wp, yh = X[pop], window[pop], yp[hold]
        progress(f"{target}: the logistic regression")
        base = _logistic(_features(Xp, "all"), yp, train)
        risk = base
        if engine == "tessera":
            progress(f"{target}: TESSERA on {len(pop):,} jobs ({used})")
            risk, clf = _tessera(_features(Xp, "all"), yp, train, wp, plan, used)
            out["weights"] = {k: round(float(v), 4) for k, v in clf.component_weights.items()}
            out["similarity"] = getattr(clf, "similarity_backend_", None)
        out["auc"] = {"tessera": _auc(yh, risk[hold]) if engine == "tessera" else None,
                      "baseline": _auc(yh, base[hold])}
        out["top_tenth"] = {"tessera": _top_tenth(yh, risk[hold]) if engine == "tessera" else None,
                            "baseline": _top_tenth(yh, base[hold])}
        resamples = int(plan.get("bootstrap") or 0)
        if resamples:
            progress(f"{target}: ranges ({resamples:,} resamples)")
            out["interval"] = _intervals(yh, risk[hold] if engine == "tessera" else None, base[hold], resamples)
        if plan.get("compare"):
            # What partition and cluster carry: each model without them; the logistic regression with them alone.
            progress(f"{target}: without partition and cluster")
            b2 = _logistic(_features(Xp, "without_place"), yp, train)
            t2 = None
            if engine == "tessera":
                t2, _ = _tessera(_features(Xp, "without_place"), yp, train, wp, plan, used)
            out["without_place"] = {"auc": {"tessera": _auc(yh, t2[hold]) if t2 is not None else None,
                                            "baseline": _auc(yh, b2[hold])},
                                    "interval": _intervals(yh, t2[hold] if t2 is not None else None, b2[hold],
                                                           resamples)}
            alone = _features(Xp, "place_alone")
            out["place_alone"] = {"auc": {"baseline": _auc(yh, _logistic(alone, yp, train)[hold])
                                          if alone.shape[1] else None}}
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
