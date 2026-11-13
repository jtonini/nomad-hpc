# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The training requests and their results, on the hub (1.45.0): ml.db.

ml.db is beside lab_check.db ([ml.train] db to say otherwise), the nomad account's
and readable by it only. It keeps:

  ml_requests  each request: who asked and when, what it was asked to do (its
               plan, from [ml.train] then), its state (queued, running, done,
               failed, cancelled), the worker that took it, its progress, and
               when done, the summary of how well it did
  ml_jobs      which dataset row was which job (only here: never sent)
  ml_scores    the scores of the holdout's jobs and of those running or
               waiting then, by target; of the newest ``keep_runs`` runs

One request at a time: another is refused while one is queued or running
(one running longer than ``stale_minutes`` without a word is given up).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from nomad.learn import dataset as D
from nomad.learn.settings import TARGETS, Settings
from nomad.learn.settings import settings as ml_settings

STATES = ("queued", "running", "done", "failed", "cancelled")
OPEN = ("queued", "running")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, asked_by TEXT NOT NULL, asked_at TEXT NOT NULL,
    state TEXT NOT NULL, plan TEXT NOT NULL, note TEXT, worker TEXT, claimed_at TEXT,
    progress TEXT, progress_at TEXT, finished_at TEXT, error TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS ml_jobs (
    request INTEGER NOT NULL, idx INTEGER NOT NULL, job_id TEXT NOT NULL, site TEXT,
    PRIMARY KEY (request, idx));
CREATE TABLE IF NOT EXISTS ml_scores (
    request INTEGER NOT NULL, target TEXT NOT NULL, idx INTEGER NOT NULL, part TEXT NOT NULL,
    risk REAL, baseline REAL, label INTEGER, PRIMARY KEY (request, target, idx));
"""


class RequestError(ValueError):
    """Refused, and why, in words."""


def db_path(config: dict | None, config_target=None) -> Path:
    """[ml.train] db (relative: beside combined.db), else ml.db beside lab_check.db."""
    from nomad import lab_check as L
    s = ml_settings(config)
    if s.db is not None:
        from nomad.actions.remote import hub_settings
        return s.db if s.db.is_absolute() else hub_settings(config_target)["combined_db"].parent / s.db
    return L.db_path(L.settings(config or {}), config_target).parent / "ml.db"


def open_records(path, write: bool = False) -> sqlite3.Connection | None:
    """ml.db; None when there is none yet and only reading. Made when first
    written, readable by its owner only, never root's."""
    path = Path(path)
    if not path.exists():
        if not write:
            return None
        if os.geteuid() == 0:
            raise RequestError(f"{path} would be root's: write it as the account the Console runs as (sudo -u nomad)")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
    if write and os.geteuid() == 0 and path.stat().st_uid != 0:
        raise RequestError(f"{path} is another account's: write it as that account (sudo -u nomad)")
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if write:
        conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(rec, sql, args=()) -> list[dict]:
    if rec is None:
        return []
    try:
        return [dict(r) for r in rec.execute(sql, tuple(args))]
    except sqlite3.OperationalError:
        return []


def _request(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ("plan", "summary"):
        try:
            out[k] = json.loads(out[k]) if out.get(k) else None
        except ValueError:
            out[k] = None
    return out


def request_of(rec, rid: int) -> dict | None:
    got = _rows(rec, "SELECT * FROM ml_requests WHERE id = ?", (int(rid),))
    return _request(got[0]) if got else None


def give_up_stale(rec, s: Settings, now: datetime | None = None) -> int:
    """Running requests not heard from in ``stale_minutes``: failed. How many."""
    now = now or datetime.now()
    cut = (now - timedelta(minutes=s.stale_minutes)).isoformat(timespec="seconds")
    with rec:
        cur = rec.execute("UPDATE ml_requests SET state = 'failed', finished_at = ?, error = ? WHERE state = 'running' "
                          "AND coalesce(progress_at, claimed_at) < ?",
                          (now.isoformat(timespec="seconds"), f"not heard from in {s.stale_minutes} minutes", cut))
    return cur.rowcount


def ask(rec, s: Settings, by: str, note: str | None = None) -> dict:
    """Queue a request with [ml.train]'s plan now; refused while one is queued or running."""
    if not s.enabled:
        raise RequestError("training from the Console is off ([ml.train] enabled)")
    give_up_stale(rec, s)
    busy = _rows(rec, "SELECT id, state FROM ml_requests WHERE state IN ('queued', 'running') ORDER BY id")
    if busy:
        raise RequestError(f"request {busy[0]['id']} is {busy[0]['state']} already")
    note = " ".join(str(note or "").split())[:300] or None
    with rec:
        cur = rec.execute("INSERT INTO ml_requests (asked_by, asked_at, state, plan, note) VALUES (?, ?, 'queued', ?, ?)",
                          ((by or "").strip().lower() or "unknown", _now(), json.dumps(s.plan(), sort_keys=True), note))
    return request_of(rec, cur.lastrowid)


def cancel(rec, rid: int, by: str) -> dict:
    got = request_of(rec, rid)
    if got is None:
        raise RequestError("no such request")
    if got["state"] != "queued":
        raise RequestError(f"request {rid} is {got['state']}: only a queued one can be cancelled")
    with rec:
        rec.execute("UPDATE ml_requests SET state = 'cancelled', finished_at = ?, error = ? WHERE id = ?",
                    (_now(), f"cancelled by {(by or '').strip().lower()}", int(rid)))
    return request_of(rec, rid)


def claim(rec, s: Settings, worker: str) -> dict | None:
    """The oldest queued request, now the worker's (running); None if none."""
    give_up_stale(rec, s)
    with rec:
        got = rec.execute("SELECT id FROM ml_requests WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()
        if got is None:
            return None
        now = _now()
        rec.execute("UPDATE ml_requests SET state = 'running', worker = ?, claimed_at = ?, progress = ?, "
                    "progress_at = ? WHERE id = ? AND state = 'queued'",
                    (worker[:64], now, "taken", now, got[0]))
    return request_of(rec, got[0])


def _running(rec, rid: int, worker: str) -> dict:
    got = request_of(rec, rid)
    if got is None or got["state"] != "running" or got["worker"] != worker[:64]:
        raise RequestError(f"request {rid} isn't running on {worker}")
    return got


def dataset(rec, conn, rid: int, worker: str, now: datetime | None = None) -> dict:
    """The request's dataset, built now from combined.db; which row is which
    job kept here (replacing any earlier build of the same request)."""
    got = _running(rec, rid, worker)
    data, mapping = D.build(conn, got["plan"], now)
    with rec:
        rec.execute("DELETE FROM ml_jobs WHERE request = ?", (int(rid),))
        rec.executemany("INSERT INTO ml_jobs (request, idx, job_id, site) VALUES (?, ?, ?, ?)",
                        [(int(rid), i, j, site) for i, j, site in mapping])
        rec.execute("UPDATE ml_requests SET progress = ?, progress_at = ? WHERE id = ?",
                    (f"sent {len(mapping)} jobs", _now(), int(rid)))
    return {"request": int(rid), **data}


def progress(rec, rid: int, worker: str, text: str) -> None:
    _running(rec, rid, worker)
    text = " ".join(str(text or "").split())[:200]
    with rec:
        rec.execute("UPDATE ml_requests SET progress = ?, progress_at = ? WHERE id = ?", (text, _now(), int(rid)))


def fail(rec, rid: int, worker: str, why: str) -> dict:
    _running(rec, rid, worker)
    with rec:
        rec.execute("UPDATE ml_requests SET state = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (_now(), " ".join(str(why or "failed").split())[:500], int(rid)))
    return request_of(rec, rid)


def _clean_summary(summary: dict) -> dict:
    """The summary as stored: only numbers and short words (it is shown)."""
    def clean(v, depth=0):
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            return v if v == v else None
        if isinstance(v, str):
            return v[:200]
        if isinstance(v, dict) and depth < 4:
            return {str(k)[:40]: clean(x, depth + 1) for k, x in list(v.items())[:40]}
        if isinstance(v, list) and depth < 4:
            return [clean(x, depth + 1) for x in v[:40]]
        return None
    return clean(summary)


def finish(rec, s: Settings, rid: int, worker: str, result: dict) -> dict:
    """The worker's result: the summary, and the scores of the holdout and of
    the jobs then running or waiting, by target. Done; older runs' scores pruned."""
    got = _running(rec, rid, worker)
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        raise RequestError("a result has a summary")
    n = rec.execute("SELECT COUNT(*) FROM ml_jobs WHERE request = ?", (int(rid),)).fetchone()[0]
    rows = []
    for target, scores in (result.get("scores") or {}).items():
        if target not in TARGETS or target not in got["plan"]["predicts"] or not isinstance(scores, list):
            raise RequestError(f"scores for {target!r}: not asked")
        for x in scores:
            try:
                idx, part = int(x["idx"]), str(x["part"])
                risk = None if x.get("risk") is None else float(x["risk"])
                base = None if x.get("baseline") is None else float(x["baseline"])
                label = None if x.get("label") is None else int(x["label"])
            except (KeyError, TypeError, ValueError):
                raise RequestError("a score is {idx, part, risk, baseline, label}") from None
            if not 0 <= idx < n or part not in ("holdout", "now"):
                raise RequestError(f"a score for row {idx} ({part}): not a row sent")
            rows.append((int(rid), target, idx, part, risk, base, label))
    summary = _clean_summary(result["summary"])
    with rec:
        rec.execute("DELETE FROM ml_scores WHERE request = ?", (int(rid),))
        rec.executemany("INSERT INTO ml_scores (request, target, idx, part, risk, baseline, label) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        rec.execute("UPDATE ml_requests SET state = 'done', finished_at = ?, progress = 'done', progress_at = ?, "
                    "summary = ? WHERE id = ?", (_now(), _now(), json.dumps(summary, sort_keys=True), int(rid)))
        old = [r[0] for r in rec.execute("SELECT id FROM ml_requests WHERE state = 'done' ORDER BY id DESC")][s.keep_runs:]
        for o in old:
            rec.execute("DELETE FROM ml_scores WHERE request = ?", (o,))
            rec.execute("DELETE FROM ml_jobs WHERE request = ?", (o,))
        # a request that failed keeps no rows either
        rec.execute("DELETE FROM ml_jobs WHERE request IN (SELECT id FROM ml_requests WHERE state IN "
                    "('failed', 'cancelled'))")
    return request_of(rec, rid)


def status(rec, s: Settings, history: int = 10) -> dict:
    """What the Console shows: the settings, the request queued or running,
    the newest done one's summary, and the last few requests."""
    if rec is not None:
        try:
            give_up_stale(rec, s)
        except sqlite3.OperationalError:          # read only: shown as it is
            pass
    reqs = [_request(r) for r in _rows(rec, "SELECT * FROM ml_requests ORDER BY id DESC LIMIT ?", (int(history),))]
    current = next((r for r in reqs if r["state"] in OPEN), None)
    last = next((r for r in reqs if r["state"] == "done"), None)
    if last is None:
        got = _rows(rec, "SELECT * FROM ml_requests WHERE state = 'done' ORDER BY id DESC LIMIT 1")
        last = _request(got[0]) if got else None
    return {"enabled": s.enabled, "engine": s.engine, "predicts": list(s.predicts), "trainer": s.trainer,
            "plan": s.plan(), "problems": list(s.problems), "current": current, "last": last, "history": reqs}


def scores(rec, rid: int, target: str, part: str = "now", top: int = 50) -> list[dict]:
    """A run's scores joined to the jobs they are (on the hub only), highest first."""
    return _rows(rec, "SELECT j.job_id, j.site, s.risk, s.baseline, s.label FROM ml_scores s JOIN ml_jobs j "
                      "ON j.request = s.request AND j.idx = s.idx WHERE s.request = ? AND s.target = ? AND s.part = ? "
                      "ORDER BY s.risk DESC LIMIT ?", (int(rid), target, part, int(top)))


__all__ = ["RequestError", "STATES", "SCHEMA", "db_path", "open_records", "request_of", "ask", "cancel", "claim",
           "dataset", "progress", "fail", "finish", "status", "scores", "give_up_stale"]
