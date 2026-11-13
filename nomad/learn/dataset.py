# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What leaves the hub for training (1.45.0): per job, only what Slurm is
asked at submission, as numbers, and what became of the job.

``build(conn, plan, now)`` gives (the dataset sent to the worker, which row
is which job: kept on the hub). The dataset has no person, no job's name or
ID, no command line, and no partition's or cluster's name (they are numbered:
the numbering stays on the hub).

Which jobs: those submitted in the last ``jobs_days`` that ended (COMPLETED,
FAILED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL; a cancelled or preempted job is
someone's decision, not an outcome), at most the newest ``max_jobs`` of
them; and those running or waiting now. The newest ``holdout_share`` of the
ended ones (by when they ended) are the holdout: the worker trains on them
as unknown, then compares its scores with what happened.

The outcomes:
  failure  an ended job that didn't complete (1), or completed (0)
  waste    a completed job that used less than (1 - waste_threshold) of the
           time it asked for (1), or more (0); not asked of a job that failed
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

FEATURES = ("req_cpus", "req_gpus", "req_mem_gb", "req_time_h", "submit_hour", "submit_weekday",
            "partition", "cluster")
ENDED = ("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL")
NOW = ("PENDING", "RUNNING")
PARTS = ("train", "holdout", "now")


def _at(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace(" ", "T")[:19]) if v else None
    except ValueError:
        return None


def _state(v) -> str:
    return str(v or "").split()[0].upper() if v else ""


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}


def build(conn: sqlite3.Connection, plan: dict, now: datetime | None = None) -> tuple[dict, list]:
    now = now or datetime.now()
    cols = _columns(conn)
    if not cols:
        raise ValueError("no jobs table in this database")
    site = "source_site" if "source_site" in cols else "NULL"
    since = (now - timedelta(days=int(plan["jobs_days"]))).isoformat(sep=" ")
    rows = conn.execute(
        f"SELECT job_id, {site} AS site, partition, req_cpus, req_gpus, req_mem_mb, req_time_seconds, "
        "runtime_seconds, submit_time, end_time, state FROM jobs WHERE submit_time >= ?",
        (since,)).fetchall()
    ended, waiting = [], []
    for r in rows:
        st = _state(r["state"])
        sub = _at(r["submit_time"])
        if sub is None:
            continue
        if st in ENDED:
            end = _at(r["end_time"]) or sub
            ended.append((end, sub, st, r))
        elif st in NOW:
            waiting.append((None, sub, st, r))
    ended.sort(key=lambda x: (x[0], str(x[3]["job_id"])))
    if plan.get("max_jobs"):
        ended = ended[-int(plan["max_jobs"]):]
    n_hold = int(round(len(ended) * float(plan["holdout_share"])))
    parts = ["train"] * (len(ended) - n_hold) + ["holdout"] * n_hold + ["now"] * len(waiting)
    every = ended + waiting
    partitions = sorted({str(x[3]["partition"] or "") for x in every})
    sites = sorted({str(x[3]["site"] or "") for x in every})
    first = min((x[1] for x in every), default=now)
    threshold = float(plan["waste_threshold"])
    out_rows, windows, failure, waste, mapping = [], [], [], [], []
    for i, ((end, sub, st, r), part) in enumerate(zip(every, parts)):
        asked = (r["req_time_seconds"] or 0) / 3600.0
        out_rows.append([float(r["req_cpus"] or 0), float(r["req_gpus"] or 0), (r["req_mem_mb"] or 0) / 1024.0,
                         asked, float(sub.hour), float(sub.weekday()),
                         float(partitions.index(str(r["partition"] or ""))), float(sites.index(str(r["site"] or "")))])
        windows.append((sub - first).days // 30)
        if part == "now":
            failure.append(None)
            waste.append(None)
        else:
            failure.append(0 if st == "COMPLETED" else 1)
            ran = r["runtime_seconds"] or 0
            waste.append((1 if 1.0 - ran / (r["req_time_seconds"]) > threshold else 0)
                         if st == "COMPLETED" and (r["req_time_seconds"] or 0) > 0 and ran > 0 else None)
        mapping.append((i, str(r["job_id"]), r["site"]))
    counts = {p: parts.count(p) for p in PARTS}
    data = {"features": list(FEATURES), "rows": out_rows, "window": windows, "part": parts,
            "labels": {"failure": failure, "waste": waste}, "plan": plan, "counts": counts,
            "partitions": len(partitions), "clusters": len(sites),
            "spread": {"waste": used_spread(ended)}}
    return data, mapping


SHARES = (0.1, 0.25, 0.5, 0.75, 0.9)
THRESHOLDS = (0.5, 0.6, 0.75, 0.9)


def used_spread(ended) -> dict | None:
    """How much of the time they asked for the completed jobs used (1.45.1), as a whole:
    the share used at the tenth, quarter, half, three quarters and nine tenths of
    them, and how many would count as wasted at a few thresholds. No job's own figure."""
    used = sorted(min(1.0, (r["runtime_seconds"] or 0) / r["req_time_seconds"])
                  for _, _, st, r in ended
                  if st == "COMPLETED" and (r["req_time_seconds"] or 0) > 0 and (r["runtime_seconds"] or 0) > 0)
    if len(used) < 20:
        return None
    n = len(used)
    at = {f"{q:g}": round(used[min(n - 1, int(q * n))], 4) for q in SHARES}
    wasted = {f"{t:g}": round(sum(1 for u in used if 1.0 - u > t) / n, 4) for t in THRESHOLDS}
    return {"jobs": n, "used_at": at, "wasted_at": wasted}


__all__ = ["build", "used_spread", "FEATURES", "ENDED", "NOW", "PARTS"]
