# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The settings of training from the Console (1.45.0), in nomad.toml.

Its own table under [ml] (whose older keys, enabled, epochs, models_dir and
[ml.learning], are another thing's and change nothing here).

On the hub::

    [ml.train]
    enabled = true                   # the Console offers Train… (administrators)
    engine = "tessera"               # or "baseline": logistic regression only
    predicts = ["failure", "waste"]  # a job failing; most of its reserved time unused
    trainer = "badenpowell"          # the worker's name, as the Console says it
    jobs_days = 365                  # jobs submitted in the last this many days
    max_jobs = 50000                 # the newest this many ended jobs (0: all; the similarity
                                     # network's time grows as the square: ~12,000 jobs take
                                     # half a minute on a CPU, 50,000 several minutes)
    waste_threshold = 0.6            # "wasted": more than this share of the time asked went unused
    holdout_share = 0.2              # the newest share, trained on as unknown: how well it did
    keep_runs = 20                   # runs kept with their scores (older ones keep their summary)
    stale_minutes = 360              # a run not heard from this long is given up
    key_file = "~/.config/nomad/ml_worker_key"   # the worker's key (the nomad account's, 600)
    # db = "ml.db"                   # default: beside lab_check.db
    [ml.train.epochs]
    gnn = 100
    lstm = 60
    ae = 60

On the worker::

    [ml.worker]
    console = "https://nomad.example.edu"   # the Console it asks
    key_file = "~/.config/nomad/ml_worker_key"
    device = "cuda"                  # or "cpu", or "auto"
    # ca_file = "/etc/pki/tls/certs/campus-ca.pem"   # if the Console's certificate needs it
    # name = "badenpowell"           # default: this machine's short name
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path

ENGINES = ("tessera", "baseline")
TARGETS = ("failure", "waste")
DEVICES = ("cuda", "cpu", "auto")
KEY_MIN = 32


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    engine: str = "tessera"
    predicts: tuple = TARGETS
    trainer: str = ""
    jobs_days: int = 365
    max_jobs: int = 50000
    waste_threshold: float = 0.6
    holdout_share: float = 0.2
    keep_runs: int = 20
    stale_minutes: int = 360
    key_file: str = "~/.config/nomad/ml_worker_key"
    db: Path | None = None
    epochs: dict = field(default_factory=lambda: {"gnn": 100, "lstm": 60, "ae": 60})
    problems: tuple = field(default_factory=tuple)

    def plan(self) -> dict:
        """What a request is asked to do, kept with it (a later change of
        nomad.toml doesn't change a request already asked)."""
        return {"engine": self.engine, "predicts": list(self.predicts), "jobs_days": self.jobs_days,
                "max_jobs": self.max_jobs, "waste_threshold": self.waste_threshold,
                "holdout_share": self.holdout_share, "epochs": dict(self.epochs)}


def _num(raw, key, default, lo, hi, problems, kind=float, where="[ml.train]"):
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or (kind is int and int(v) != v):
        problems.append(f"{where} {key} should be a number from {lo} to {hi}")
        return default
    return kind(v)


def settings(config: dict | None) -> Settings:
    ml = (config or {}).get("ml")
    raw = ml.get("train") if isinstance(ml, dict) and isinstance(ml.get("train"), dict) else {}
    p: list = []
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        p.append("[ml.train] enabled should be true or false")
        enabled = False
    engine = raw.get("engine", "tessera")
    if engine not in ENGINES:
        p.append(f"[ml.train] engine should be one of {', '.join(ENGINES)}")
        engine = "tessera"
    predicts = raw.get("predicts", list(TARGETS))
    if isinstance(predicts, str):
        predicts = [predicts]
    if not (isinstance(predicts, list) and predicts and all(t in TARGETS for t in predicts)):
        p.append(f"[ml.train] predicts should be a list of {', '.join(TARGETS)}")
        predicts = list(TARGETS)
    trainer = raw.get("trainer", "")
    if not isinstance(trainer, str) or len(trainer) > 64:
        p.append("[ml.train] trainer should be a short name")
        trainer = ""
    key_file = raw.get("key_file", Settings.key_file)
    if not isinstance(key_file, str) or not key_file:
        p.append("[ml.train] key_file should be a path")
        key_file = Settings.key_file
    db = raw.get("db")
    if db is not None and not (isinstance(db, str) and db):
        p.append("[ml.train] db should be a path")
        db = None
    epochs_raw = raw.get("epochs") if isinstance(raw.get("epochs"), dict) else {}
    epochs = {k: _num(epochs_raw, k, d, 1, 2000, p, int, "[ml.train.epochs]")
              for k, d in (("gnn", 100), ("lstm", 60), ("ae", 60))}
    return Settings(
        enabled=enabled, engine=engine, predicts=tuple(dict.fromkeys(predicts)), trainer=trainer.strip(),
        jobs_days=_num(raw, "jobs_days", 365, 7, 3650, p, int),
        max_jobs=_num(raw, "max_jobs", 50000, 0, 5_000_000, p, int),
        waste_threshold=_num(raw, "waste_threshold", 0.6, 0.05, 0.95, p),
        holdout_share=_num(raw, "holdout_share", 0.2, 0.05, 0.5, p),
        keep_runs=_num(raw, "keep_runs", 20, 1, 500, p, int),
        stale_minutes=_num(raw, "stale_minutes", 360, 10, 10080, p, int),
        key_file=key_file, db=Path(db).expanduser() if db else None, epochs=epochs, problems=tuple(p))


def read_key(path: str) -> str | None:
    """The worker's key from its file, or None (missing, unreadable, too short)."""
    try:
        key = Path(path).expanduser().read_text().strip()
    except OSError:
        return None
    return key if len(key) >= KEY_MIN and "\n" not in key else None


@dataclass(frozen=True)
class WorkerSettings:
    console: str = ""
    key_file: str = "~/.config/nomad/ml_worker_key"
    device: str = "cuda"
    ca_file: str | None = None
    name: str = ""
    problems: tuple = field(default_factory=tuple)


def worker_settings(config: dict | None) -> WorkerSettings:
    ml = (config or {}).get("ml")
    raw = ml.get("worker") if isinstance(ml, dict) and isinstance(ml.get("worker"), dict) else {}
    p: list = []
    console = raw.get("console", "")
    if not (isinstance(console, str) and console.startswith(("https://", "http://"))):
        p.append("[ml.worker] console should be the Console's address (https://...)")
        console = ""
    device = raw.get("device", "cuda")
    if device not in DEVICES:
        p.append(f"[ml.worker] device should be one of {', '.join(DEVICES)}")
        device = "cuda"
    ca = raw.get("ca_file")
    if ca is not None and not (isinstance(ca, str) and ca):
        p.append("[ml.worker] ca_file should be a path")
        ca = None
    name = raw.get("name") or socket.gethostname().split(".")[0]
    if not isinstance(name, str) or len(name) > 64:
        p.append("[ml.worker] name should be a short name")
        name = socket.gethostname().split(".")[0]
    key_file = raw.get("key_file", WorkerSettings.key_file)
    if not isinstance(key_file, str) or not key_file:
        p.append("[ml.worker] key_file should be a path")
        key_file = WorkerSettings.key_file
    return WorkerSettings(console=console.rstrip("/"), key_file=key_file, device=device,
                          ca_file=str(Path(ca).expanduser()) if ca else None, name=name, problems=tuple(p))


__all__ = ["Settings", "settings", "WorkerSettings", "worker_settings", "read_key", "ENGINES", "TARGETS"]
