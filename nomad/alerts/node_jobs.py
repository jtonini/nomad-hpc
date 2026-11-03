# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A node that fails every job at start (1.25.4).

A node that is up, idle and accepting jobs, but whose jobs all fail within
seconds -- a mount missing after a reboot, slurmd set up wrong after a
reinstall, a program gone -- looks healthy to Slurm and to the node_state
alerts. Each job it fails frees it at once for the next, so it can swallow a
whole array in minutes (vadur's node03, 29 Oct 2026: 46 tasks of one array
in six minutes, while the same array ran for hours on the other nodes).

From this site's ``jobs`` table, at each node_state collection: a node where,
among the jobs that started there in the last ``window_minutes`` (single-node
jobs), at least ``min_jobs`` failed within ``fail_fast_seconds`` of starting,
and they are at least ``share`` of all that started there -- while the very
same work ran on other nodes in that time: the same array (its job id before
the "_"), else the same person's jobs of the same name, at least one of them
running past ``fail_fast_seconds`` elsewhere and at most a quarter of them
failing fast there. A broken script fails everywhere, so it is not the node;
the same tasks running for hours elsewhere points at it (at least 2 of the
node's fast failures so shown). It can't be sure: an array whose last tasks
lack their input fails them just as fast, all on the node that frees first.
So the alert says what to look at before draining (one of the jobs), and
how many different works failed there (several: more likely the node). A
node already drained or down is left out: someone is on it.

    [alerts.node_jobs]
    enabled = true
    window_minutes = 60
    fail_fast_seconds = 60
    min_jobs = 5
    share = 0.8

Raised through the site's [alerts] (source ``node_jobs``, the node as host):
once, again when worse, then every reminder. The alert counts the people
affected; it names no one.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

DEFAULTS = {"enabled": True, "window_minutes": 60, "fail_fast_seconds": 60, "min_jobs": 5, "share": 0.8}
FAILED = {"FAILED", "NODE_FAIL", "BOOT_FAIL"}
BUSY_OK = {"RUNNING", "COMPLETING", "COMPLETED"}
ELSEWHERE_FAILING = 0.25     # the same work failing fast on other nodes: at most this share of it
SHOWN = 2                    # the node's fast failures whose very work ran well elsewhere: at least


def settings(config: dict | None) -> dict:
    """[alerts.node_jobs] over the defaults (a bad value: the default)."""
    raw = ((config or {}).get("alerts") or {}).get("node_jobs") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if raw.get("enabled") is False:
        out["enabled"] = False
    for key, kind, low in (("window_minutes", int, 5), ("fail_fast_seconds", int, 1), ("min_jobs", int, 2),
                           ("share", float, 0.1)):
        try:
            v = kind(raw[key]) if key in raw and not isinstance(raw[key], bool) else None
        except (TypeError, ValueError):
            v = None
        if v is not None and v >= low and (key != "share" or v <= 1):
            out[key] = v
    return out


def _word(state) -> str:
    return str(state or "").strip().upper().split(" ")[0].rstrip("+")


def _time(value):
    try:
        t = datetime.fromisoformat(str(value)[:19].replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    return t


def _work(job_id, user, name) -> tuple:
    """What a job is one of: its array (12272 of 12272_5), else its person's
    jobs of its name (else itself)."""
    jid = str(job_id or "")
    if "_" in jid:
        return ("array", jid.split("_")[0])
    return ("name", str(user or ""), str(name)) if name else ("job", jid)


def _one_node(node_list) -> str | None:
    text = str(node_list or "").strip()
    if not text or any(c in text for c in ",[ ") or text.lower() in ("none", "(null)"):
        return None
    return text


def failing_nodes(conn: sqlite3.Connection, now: datetime, cfg: dict | None = None,
                  skip: set | frozenset = frozenset()) -> list[dict]:
    """The nodes failing every job at start, worst first: [{node, failed,
    started, people, first, last, jobs}] (jobs: their ids). ``skip``: nodes
    to leave out (already drained or down)."""
    cfg = cfg or dict(DEFAULTS)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    except sqlite3.Error:
        return []
    if not {"job_id", "user_name", "node_list", "start_time", "state"} <= cols:
        return []
    rt = "runtime_seconds" if "runtime_seconds" in cols else "NULL"
    end = "end_time" if "end_time" in cols else "NULL"
    name = "job_name" if "job_name" in cols else "NULL"
    cutoff = now - timedelta(minutes=cfg["window_minutes"])
    try:
        rows = conn.execute(
            f"SELECT job_id, user_name, node_list, start_time, {end} AS end_time, state, {rt} AS runtime, "
            f"{name} AS job_name FROM jobs WHERE start_time >= ?",   # the day: then each time, whatever its separator
            (cutoff.strftime("%Y-%m-%d"),)).fetchall()
    except sqlite3.Error:
        return []
    by_node: dict = {}
    work: dict = {}                   # work -> node -> [started, failed fast, ran well]
    fast = cfg["fail_fast_seconds"]
    for job_id, user, node_list, start, end_t, state, runtime, job_name in rows:
        node = _one_node(node_list)
        t = _time(start)
        if node is None or t is None or t < cutoff or t > now:
            continue
        word = _word(state)
        took = runtime if isinstance(runtime, (int, float)) else None
        if took is None and _time(end_t):
            took = (_time(end_t) - t).total_seconds()
        if word == "RUNNING":
            took = (now - t).total_seconds()          # running so far
        failed_fast = word in FAILED and took is not None and 0 <= took < fast
        ran_well = word in BUSY_OK and took is not None and took >= fast
        w = _work(job_id, user, job_name)
        c = work.setdefault(w, {}).setdefault(node, [0, 0, 0])
        c[0] += 1
        c[1] += failed_fast
        c[2] += ran_well
        b = by_node.setdefault(node, {"started": 0, "failed": [], "people": set()})
        b["started"] += 1
        if failed_fast:
            b["failed"].append((t, str(job_id), w))
            b["people"].add(user)

    def shown_elsewhere(w, node) -> bool:
        """The same work ran well on other nodes, and hardly failed fast there."""
        others = [c for n, c in work.get(w, {}).items() if n != node]
        started, failed, well = (sum(c[i] for c in others) for i in range(3))
        return well >= 1 and failed <= ELSEWHERE_FAILING * started

    out = []
    for node, b in by_node.items():
        n = len(b["failed"])
        if node in skip or n < cfg["min_jobs"] or n < cfg["share"] * b["started"]:
            continue
        # It is this node, not the work: the very same work ran elsewhere.
        shown = sum(1 for _t, _j, w in b["failed"] if shown_elsewhere(w, node))
        if shown < SHOWN:
            continue
        times = sorted(x[0] for x in b["failed"])
        out.append({"node": node, "failed": n, "started": b["started"], "people": len(b["people"]),
                    "first": times[0], "last": times[-1], "shown": shown,
                    "works": len({w for _t, _j, w in b["failed"]}),
                    "jobs": [j for _t, j, _w in sorted(b["failed"], key=lambda x: (x[0], x[1]))][:200]})
    out.sort(key=lambda d: (-d["failed"], d["node"]))
    return out


def message(f: dict, fast: int) -> str:
    people = f"{f['people']} {'person' if f['people'] == 1 else 'people'}"
    when = (f"at {f['first']:%H:%M}" if f["first"] == f["last"]
            else f"between {f['first']:%H:%M} and {f['last']:%H:%M}")
    works = f.get("works") or 1
    likely = (f"jobs of {works} different works failed there: likely the node (a mount, a program, slurmd's "
              "setup after a reinstall)" if works > 1 else
              "it may be the node (a mount, a program, slurmd's setup after a reinstall), or those very tasks "
              "(an array's last ones without their input fail as fast, all on the node that frees first)")
    first = (f.get("jobs") or [None])[0]
    look = f"Look at job {first}'s output first (sacct -j {first}; its .err). " if first else ""
    return (f"Node {f['node']} failed {f['failed']} of the {f['started']} jobs that started there within "
            f"{fast} s of starting, {when} ({people}), while the very same work (an array's other tasks, "
            f"jobs of the same name) ran on other nodes: {likely}. {look}If it is the node, drain it while you "
            f"look: scontrol update nodename={f['node']} state=drain reason=\"jobs fail at start\"")


__all__ = ["DEFAULTS", "settings", "failing_nodes", "message"]
