"""Jobs in a window, for one person, a set of people, or a site.

One calculation for every scope, so a person's figures, their lab's and their
site's are computed the same way:

  * outcomes of the jobs that ended in the window, and the share of those
    that ran to an end (not cancelled, preempted or of unknown outcome) that
    failed, timed out or ran out of memory; node failures apart, in neither
    count, since they are the machine's, not the job's. (The Console's
    Dashboard, until it reads these facts, counts node failures as failures
    and divides by every job that ended, cancelled ones too.)
  * core-hours asked for, and how much of them was used, over the jobs whose
    CPU use was measured: core-hours used over core-hours requested, so a
    big long job counts for what it held (the Dashboard's figure);
  * memory used at peak over memory requested, by memory-hours, over the
    jobs measured; a peak larger than any node at its site is not a
    measurement (Slurm's jobacct_gather/linux counts shared memory once per
    process) and is set aside and counted;
  * GPUs asked for (their use per job is not measured here);
  * how long jobs waited to start, from when they could (a job waiting on
    another job or a --begin time isn't waiting for the queue; 1.13).

A figure that rests on nothing measured is None, never zero.
"""
from __future__ import annotations

import sqlite3

from nomad.facts._common import chunks, columns, fetch, median, num, pct, quantile, site_label

# Slurm's end states, as nomad stores them ("CANCELLED by 1234" is CANCELLED).
OUTCOMES = {
    "COMPLETED": "completed", "FAILED": "failed", "TIMEOUT": "timeout", "DEADLINE": "timeout",
    "OUT_OF_MEMORY": "out_of_memory", "NODE_FAIL": "node_fail", "BOOT_FAIL": "node_fail",
    "CANCELLED": "cancelled", "PREEMPTED": "preempted",
}
OUTCOME_KEYS = ("completed", "failed", "timeout", "out_of_memory", "node_fail", "cancelled",
                "preempted", "other")
# Ended without running to an end of its own: not counted in the failure share.
NOT_RAN_TO_END = ("cancelled", "preempted", "other")
PROBLEMS = ("failed", "timeout", "out_of_memory")

ACTIVE = {"RUNNING": "running", "COMPLETING": "running", "CONFIGURING": "running",
          "PENDING": "pending"}


def _site_filter(cols: set, alias: str) -> str | None:
    """A WHERE clause selecting one site that can use the hub's indexes."""
    if "source_site" in cols:
        return f"{alias}.source_site = ?"
    if "cluster" in cols:
        return f"{alias}.cluster = ?"
    return None


def _select(jcols: set, scols: set) -> str:
    def j(c):
        return f"j.{c}" if c in jcols else "NULL"

    def s(c):
        return f"js.{c}" if c in scols else "NULL"
    site = ("j.source_site" if "source_site" in jcols else "j.cluster" if "cluster" in jcols
            else "NULL")
    # From when the job could start (1.13), not from submission.
    from nomad.db.jobkeys import queue_wait_sql
    wait = queue_wait_sql(jcols, "j") if "wait_time_seconds" in jcols else "NULL"
    return (
        "SELECT CASE WHEN instr(j.state, ' by ') > 0 "
        "THEN substr(j.state, 1, instr(j.state, ' by ') - 1) ELSE j.state END AS state, "
        f"{j('user_name')} AS user_name, {j('req_cpus')} AS req_cpus, "
        f"{j('req_mem_mb')} AS req_mem_mb, {j('req_gpus')} AS req_gpus, "
        f"{j('req_time_seconds')} AS req_time, {j('eligible_time')} AS eligible_time, "
        f"{j('runtime_seconds')} AS runtime, {j('submit_time')} AS submit_time, "
        f"{j('start_time')} AS start_time, {wait} AS wait_seconds, "
        f"{site} AS site, {s('avg_cpu_percent')} AS avg_cpu, {s('peak_memory_gb')} AS peak_gb, "
        f"{s('max_tasks')} AS max_tasks "
    )


def finished_rows(conn: sqlite3.Connection, since: str, until: str, *, users=None,
                  site=None) -> list[dict] | None:
    """The jobs that ended in [since, until], with their measured use where
    there is one; None when the database has no jobs to speak of."""
    jcols = columns(conn, "jobs")
    if not {"state", "end_time"} <= jcols:
        return None
    scols = columns(conn, "job_summary")
    join = ""
    if "job_id" in jcols and "job_id" in scols:
        # The same job number exists at more than one site.
        same = (" AND js.source_site = j.source_site"
                if "source_site" in jcols and "source_site" in scols else "")
        join = f" LEFT JOIN job_summary js ON js.job_id = j.job_id{same}"
    else:
        scols = set()
    base = _select(jcols, scols) + f"FROM jobs j{join} WHERE j.end_time >= ? AND j.end_time <= ?"
    args = [since, until]
    if site is not None:
        clause = _site_filter(jcols, "j")
        if clause:
            base += f" AND {clause}"
            args.append(site)
    if users is None:
        return fetch(conn, base, args)
    users = sorted({u for u in users if u})
    if not users or "user_name" not in jcols:
        return []
    out = []
    for part in chunks(users):
        out += fetch(conn, base + f" AND j.user_name IN ({','.join('?' * len(part))})",
                     args + part)
    return out


def _wait_seconds(r) -> float | None:
    w = num(r.get("wait_seconds"))
    if w is not None and w >= 0:
        return w
    from nomad.facts._common import parse_time
    s, t = parse_time(r.get("submit_time")), parse_time(r.get("start_time"))
    if s and t and t >= s:
        return (t - s).total_seconds()
    return None


def summarize(rows: list[dict] | None, ceilings: dict | None = None) -> dict | None:
    """The figures described at the top, from finished_rows()."""
    if rows is None:
        return None
    from nomad.edu.progress import memory_ceiling
    ceilings = ceilings or {}
    # Numbers as numbers: a column holding text counts as not recorded.
    rows = [{**r, **{k: num(r.get(k)) for k in ("req_cpus", "req_mem_mb", "req_gpus", "runtime",
                                                 "avg_cpu", "peak_gb")}} for r in rows]
    outcomes = dict.fromkeys(OUTCOME_KEYS, 0)
    for r in rows:
        outcomes[OUTCOMES.get(str(r.get("state") or "").upper(), "other")] += 1
    finished = len(rows)
    ended = finished - sum(outcomes[k] for k in NOT_RAN_TO_END)
    # Node failures are the machine's: in neither count of the failure share.
    judged = ended - outcomes["node_fail"]

    def hours(r):
        return (r.get("runtime") or 0) / 3600

    held = [r for r in rows if (r.get("req_cpus") or 0) > 0 and hours(r) > 0]
    cpu_measured = [r for r in held if (r.get("avg_cpu") or 0) > 0]
    core_held = sum(r["req_cpus"] * hours(r) for r in cpu_measured)
    core_used = sum(r["avg_cpu"] / 100 * r["req_cpus"] * hours(r) for r in cpu_measured)

    def possible(r):
        c = memory_ceiling(ceilings, r.get("site"))
        return not c or (r.get("peak_gb") or 0) * 1024 <= c

    with_peak = [r for r in rows if (r.get("peak_gb") or 0) > 0]
    set_aside = sum(1 for r in with_peak if not possible(r))
    mem_measured = [r for r in with_peak if possible(r) and (r.get("req_mem_mb") or 0) > 0
                    and hours(r) > 0]
    mem_held = sum(r["req_mem_mb"] * hours(r) for r in mem_measured)
    mem_used = sum(r["peak_gb"] * 1024 * hours(r) for r in mem_measured)

    gpu = [r for r in rows if (r.get("req_gpus") or 0) > 0]
    waits = [w / 60 for w in (_wait_seconds(r) for r in rows) if w is not None]
    return {
        "finished": finished,
        "outcomes": outcomes,
        "ran_to_an_end": ended,
        # failed, timed out or out of memory, of the jobs that ran to an end
        # (node failures left out of both)
        "failed_pct": pct(sum(outcomes[k] for k in PROBLEMS), judged),
        "people": len({r.get("user_name") for r in rows if r.get("user_name")}),
        "core_hours": {
            "requested": round(sum(r["req_cpus"] * hours(r) for r in held), 1),
            "used_pct": pct(core_used, core_held),
            "jobs_measured": len(cpu_measured),
        },
        "memory": {
            "used_pct": pct(mem_used, mem_held),
            "jobs_measured": len(mem_measured),
            "set_aside": set_aside,
        },
        "gpus": {
            "jobs": len(gpu),
            "gpu_hours_requested": round(sum(r["req_gpus"] * hours(r) for r in gpu), 1),
        },
        "wait_minutes": {
            "median": round(median(waits), 1) if waits else None,
            "p90": round(quantile(waits, 0.9), 1) if waits else None,
            "jobs": len(waits),
        },
    }


def active_people(conn: sqlite3.Connection, users, exclude_sites=()) -> set:
    """Which of ``users`` have a job running or waiting now, at sites other
    than ``exclude_sites``. For the caller's own reckoning (how many people
    a figure rests on); never shown."""
    jcols = columns(conn, "jobs")
    users = sorted({u for u in users if u})
    if not {"state", "user_name"} <= jcols or not users:
        return set()
    site_col = ("source_site" if "source_site" in jcols else "cluster" if "cluster" in jcols
                else None)
    states = sorted(ACTIVE)
    found = set()
    for part in chunks(users):
        for r in fetch(conn, f"SELECT DISTINCT user_name AS u, {site_col or 'NULL'} AS s FROM jobs "
                             f"WHERE state IN ({','.join('?' * len(states))}) "
                             f"AND user_name IN ({','.join('?' * len(part))})", states + part):
            if site_label(r["s"]) not in set(exclude_sites):
                found.add(r["u"])
    return found


def active(conn: sqlite3.Connection, *, users=None, site=None) -> dict:
    """{site: {"running": n, "pending": n}} for jobs running or waiting, as
    the last collection at each site saw them."""
    jcols = columns(conn, "jobs")
    if "state" not in jcols:
        return {}
    site_col = ("source_site" if "source_site" in jcols else "cluster" if "cluster" in jcols
                else None)
    states = sorted(ACTIVE)
    sql = (f"SELECT {site_col or 'NULL'} AS site, state, COUNT(*) AS n FROM jobs "
           f"WHERE state IN ({','.join('?' * len(states))})")
    args = list(states)
    if site is not None and site_col:
        sql += f" AND {site_col} = ?"
        args.append(site)
    groups = [None]
    if users is not None:
        users = sorted({u for u in users if u})
        if not users or "user_name" not in jcols:
            return {}
        groups = list(chunks(users))
    out: dict = {}
    for part in groups:
        q, a = sql, list(args)
        if part is not None:
            q += f" AND user_name IN ({','.join('?' * len(part))})"
            a += part
        for r in fetch(conn, q + " GROUP BY 1, 2", a):
            entry = out.setdefault(r["site"], {"running": 0, "pending": 0})
            entry[ACTIVE[r["state"]]] += r["n"]
    return out
