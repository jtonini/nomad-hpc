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
  idle     (1.46.0) a completed job of 2 cores or more that ran 5 minutes or
           more and was measured (job_summary), that on average left more than
           idle_threshold of the cores it asked for unused (1), or not (0): the
           jobs Recommendations judge for cores, and as they judge them

Each of waste and idle carries its spread (counts only): how much of what
was asked the jobs used, at the tenth to nine tenths of them, and how many
would count at a few thresholds.

1.50.0, a fairer test:
  thin     of the jobs one person submitted on one day with the same request
           (partition, cluster, cores, GPUs, memory, time), at most this many
           are kept, picked at random but the same each time (by job): a job
           array of thousands counts a few times, not thousands, and the same
           job isn't learned from and tested on under two numbers. Before the
           max_jobs cap, so the newest max_jobs reach further back.
  period   each ended job's period, counting back from the newest one that
           ended: 0 the last rolling_days, 1 the ones before, ...; ``periods``
           says when each began (YYYY-MM-DD). For rolling: each of the last
           periods held out in turn.
  group    with people_folds, each ended job's group of people (numbered from
           0): people dealt into that many groups, the busiest first, each to
           the group with the fewest jobs so far, so no group is one person's
           arrays. Only the number leaves the hub, never who is in which.

1.51.0, a simple rule to beat: ``previous``, per target, what became of the
same person's last job that had ended when this one was sent (1 it did it,
0 it didn't, None no such job): of all their jobs, before thinning. "Their
last job failed" foretold failures better than the models on UR's clusters;
a model is worth its keep where it does better than that.
"""
from __future__ import annotations

import hashlib
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


def _measured(conn) -> tuple[str, str]:
    """(join, column): each job's average share of its cores used, from job_summary, matched
    to jobs as Recommendations match them; NULL where there is none."""
    try:
        scols = {r[1] for r in conn.execute("PRAGMA table_info(job_summary)")}
    except sqlite3.Error:
        scols = set()
    if not {"job_id", "avg_cpu_percent"} <= scols:
        return "", "NULL"
    from nomad.edu.progress import summary_join
    return f" LEFT JOIN job_summary js ON {summary_join(conn)}", "js.avg_cpu_percent"


def build(conn: sqlite3.Connection, plan: dict, now: datetime | None = None) -> tuple[dict, list]:
    from nomad.edu.scoring import SHORT_JOB_SECONDS
    now = now or datetime.now()
    cols = _columns(conn)
    if not cols:
        raise ValueError("no jobs table in this database")
    site = "j.source_site" if "source_site" in cols else "NULL"
    user = "j.user_name" if "user_name" in cols else "NULL"
    since = (now - timedelta(days=int(plan["jobs_days"]))).isoformat(sep=" ")
    join, avg = _measured(conn) if "idle" in plan.get("predicts", ()) else ("", "NULL")
    rows = conn.execute(
        f"SELECT j.job_id AS job_id, {site} AS site, j.partition AS partition, j.req_cpus AS req_cpus, "
        "j.req_gpus AS req_gpus, j.req_mem_mb AS req_mem_mb, j.req_time_seconds AS req_time_seconds, "
        "j.runtime_seconds AS runtime_seconds, j.submit_time AS submit_time, j.end_time AS end_time, "
        f"j.state AS state, {user} AS user_name, {avg} AS avg_cpu FROM jobs j{join} WHERE j.submit_time >= ?",
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
    threshold = float(plan["waste_threshold"])
    idle_at = float(plan.get("idle_threshold", 0.5))
    history = _history(ended, threshold, idle_at, SHORT_JOB_SECONDS)
    seen = len(ended)
    ended = thin(ended, int(plan.get("thin") or 0))
    thinned = seen - len(ended)
    if plan.get("max_jobs"):
        ended = ended[-int(plan["max_jobs"]):]
    n_hold = int(round(len(ended) * float(plan["holdout_share"])))
    parts = ["train"] * (len(ended) - n_hold) + ["holdout"] * n_hold + ["now"] * len(waiting)
    every = ended + waiting
    partitions = sorted({str(x[3]["partition"] or "") for x in every})
    sites = sorted({str(x[3]["site"] or "") for x in every})
    first = min((x[1] for x in every), default=now)
    previous: dict = {t: [] for t in LABELS}
    out_rows, windows, failure, waste, idle, mapping = [], [], [], [], [], []
    days = max(1, int(plan.get("rolling_days") or 7))
    last = max((end for end, *_ in ended), default=now).date()
    period = [None if part == "now" else (last - end.date()).days // days for (end, *_), part in zip(every, parts)]
    periods = [(last - timedelta(days=days * (k + 1) - 1)).isoformat()
               for k in range(max((p for p in period if p is not None), default=-1) + 1)]
    people = groups(ended, int(plan.get("people_folds") or 0))
    group = [None if part == "now" else people.get(str(r["user_name"] or "")) for (_, _, _, r), part in zip(every, parts)]
    for i, ((end, sub, st, r), part) in enumerate(zip(every, parts)):
        asked = (r["req_time_seconds"] or 0) / 3600.0
        out_rows.append([float(r["req_cpus"] or 0), float(r["req_gpus"] or 0), (r["req_mem_mb"] or 0) / 1024.0,
                         asked, float(sub.hour), float(sub.weekday()),
                         float(partitions.index(str(r["partition"] or ""))), float(sites.index(str(r["site"] or "")))])
        windows.append((sub - first).days // 30)
        if part == "now":
            failure.append(None)
            waste.append(None)
            idle.append(None)
        else:
            got = labels_of(st, r, threshold, idle_at, SHORT_JOB_SECONDS)
            failure.append(got["failure"])
            waste.append(got["waste"])
            idle.append(got["idle"])
        before = _before(history.get(str(r["user_name"] or "")), sub)
        for t in LABELS:
            previous[t].append(before.get(t))
        mapping.append((i, str(r["job_id"]), r["site"]))
    counts = {p: parts.count(p) for p in PARTS}
    counts["thinned"] = thinned
    busy = sorted(_tally(ended).values(), reverse=True)
    counts["people"] = len(busy)
    counts["busiest"] = {str(n): round(sum(busy[:n]) / len(ended), 4) for n in (1, 5)} if ended else None
    counts["ended_from"] = min(end for end, *_ in ended).date().isoformat() if ended else None
    counts["ended_to"] = last.isoformat() if ended else None
    data = {"features": list(FEATURES), "rows": out_rows, "window": windows, "part": parts,
            "period": period, "periods": periods, "group": group if people else None,
            "labels": {"failure": failure, "waste": waste, "idle": idle}, "previous": previous,
            "plan": plan, "counts": counts,
            "partitions": len(partitions), "clusters": len(sites),
            "spread": {"waste": used_spread(ended),
                       "idle": _spread([_cores_used(st, r, SHORT_JOB_SECONDS) for _, _, st, r in ended], "cores")}}
    return data, mapping


LABELS = ("failure", "waste", "idle")


def labels_of(st, r, threshold: float, idle_at: float, short: int) -> dict:
    """An ended job's outcome for each target: 1 it did it, 0 it didn't, None not asked of it."""
    ran = r["runtime_seconds"] or 0
    used = _cores_used(st, r, short)
    return {"failure": 0 if st == "COMPLETED" else 1,
            "waste": (1 if 1.0 - ran / r["req_time_seconds"] > threshold else 0)
            if st == "COMPLETED" and (r["req_time_seconds"] or 0) > 0 and ran > 0 else None,
            "idle": None if used is None else (1 if 1.0 - used > idle_at else 0)}


def _history(ended: list, threshold: float, idle_at: float, short: int) -> dict:
    """{person: ([when each ended], [{target: what their latest job ended by then did}])},
    in order of ending: each target keeps its latest known outcome (a job not asked a
    target doesn't wipe it). Stays on the hub."""
    out: dict = {}
    for end, _, st, r in ended:
        times, states = out.setdefault(str(r["user_name"] or ""), ([], []))
        now = dict(states[-1]) if states else {}
        for t, v in labels_of(st, r, threshold, idle_at, short).items():
            if v is not None:
                now[t] = v
        times.append(end)
        states.append(now)
    return out


def _before(seq, sub) -> dict:
    """What the person's jobs that had ended before ``sub`` last did, per target."""
    import bisect
    if not seq:
        return {}
    i = bisect.bisect_left(seq[0], sub)
    return seq[1][i - 1] if i else {}


def _key(r) -> int:
    """A job's place in its thinning draw: random-looking, but the same for the same job every time."""
    return int.from_bytes(hashlib.blake2b(f"{r['site']}|{r['job_id']}".encode(), digest_size=8).digest(), "big")


def thin(ended: list, most: int) -> list:
    """Of the ended jobs one person submitted on one day with the same request (partition,
    cluster, cores, GPUs, memory, time), at most ``most`` (1.50.0; 0: all), the same ones
    each time; still in order of when they ended."""
    if most <= 0:
        return ended
    keep: dict = {}
    for n, (_, sub, _, r) in enumerate(ended):
        k = (str(r["user_name"] or ""), sub.date(), str(r["partition"] or ""), str(r["site"] or ""), r["req_cpus"],
             r["req_gpus"], r["req_mem_mb"], r["req_time_seconds"])
        keep.setdefault(k, []).append((_key(r), n))
    chosen = {n for v in keep.values() for _, n in sorted(v)[:most]}
    return [x for n, x in enumerate(ended) if n in chosen]


def _tally(ended: list) -> dict:
    """How many of the ended jobs are each person's (stays on the hub)."""
    jobs: dict = {}
    for *_, r in ended:
        who = str(r["user_name"] or "")
        jobs[who] = jobs.get(who, 0) + 1
    return jobs


def groups(ended: list, k: int) -> dict:
    """People dealt into ``k`` groups (1.50.0; 0: none): the busiest first, each to the
    group with the fewest jobs so far (ties: the lower number). {person: group}."""
    if k < 2:
        return {}
    jobs = _tally(ended)
    load = [0] * k
    out = {}
    for who, n in sorted(jobs.items(), key=lambda x: (-x[1], x[0])):
        g = min(range(k), key=lambda i: (load[i], i))
        out[who] = g
        load[g] += n
    return out


def _cores_used(st, r, short) -> float | None:
    """The average share of its cores a job used, where Recommendations would judge it: completed,
    2 cores or more, 5 minutes or more, measured. None otherwise."""
    avg = r["avg_cpu"] if "avg_cpu" in r.keys() else None
    if st != "COMPLETED" or avg is None or avg <= 0 or (r["req_cpus"] or 0) < 2 or (r["runtime_seconds"] or 0) < short:
        return None
    return min(1.0, float(avg) / 100.0)


SHARES = (0.1, 0.25, 0.5, 0.75, 0.9)
THRESHOLDS = (0.5, 0.6, 0.75, 0.9)


def used_spread(ended) -> dict | None:
    """How much of the time they asked for the completed jobs used (1.45.1), as a whole:
    the share used at the tenth, quarter, half, three quarters and nine tenths of
    them, and how many would count as wasted at a few thresholds. No job's own figure."""
    return _spread([min(1.0, (r["runtime_seconds"] or 0) / r["req_time_seconds"])
                    for _, _, st, r in ended
                    if st == "COMPLETED" and (r["req_time_seconds"] or 0) > 0 and (r["runtime_seconds"] or 0) > 0],
                   "time")


def _spread(values, of: str) -> dict | None:
    used = sorted(v for v in values if v is not None)
    if len(used) < 20:
        return None
    n = len(used)
    at = {f"{q:g}": round(used[min(n - 1, int(q * n))], 4) for q in SHARES}
    wasted = {f"{t:g}": round(sum(1 for u in used if 1.0 - u > t) / n, 4) for t in THRESHOLDS}
    return {"of": of, "jobs": n, "used_at": at, "wasted_at": wasted}


__all__ = ["build", "thin", "groups", "labels_of", "used_spread", "FEATURES", "ENDED", "NOW", "PARTS", "LABELS"]
