"""nomad 1.53.3: a job array's later tasks don't count its age as their wait.

Every task of a job array carries the array's submission and eligible time.
An array sent five weeks ago whose tasks run a few at a time has tasks that
start today "eligible" for five weeks: on hpc1 and hpc3, nearly all the
hours the lab figures counted as waiting for room were such tasks. Now:

- a wait counts an array once, by its first task (the condo figures, the
  job facts, the Submit page's typical waits, the long-wait insight);
- a later task waited for room only from when nomad saw its array wait for
  room, and is otherwise no job an idle condo could have taken;
- the Slurm collector keeps the reason and the time it began waiting that
  the array's pending tasks had when a task first appears.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import slurm as S
from nomad.collectors.slurm import JobInfo, SlurmCollector
from nomad.condo import figures as F
from nomad.condo import idle
from nomad.db.jobkeys import array_of, later_task_sql, queue_wait_sql
from nomad.db.migrations import ensure_database
from nomad.facts import jobs as jobfacts
from nomad.facts.share import _waits_for_room
from nomad.submit.where import typical_waits

NOW = datetime(2026, 11, 17, 12, 0, 0)
SENT = NOW - timedelta(days=35)
COLS = ("job_id", "source_site", "user_name", "partition", "node_list", "state", "submit_time", "eligible_time",
        "start_time", "end_time", "req_cpus", "req_mem_mb", "req_gpus", "alloc_tres", "alloc_gpus",
        "runtime_seconds", "account", "req_time_seconds", "pending_reason", "req_nodes", "ready_since",
        "wait_time_seconds")


def iso(t):
    return t.isoformat(timespec="seconds") if t else None


def job(job_id, site, sub, start, *, end=None, ready=None, part="gpu"):
    end = end or (start + timedelta(minutes=30) if start else None)
    done = end is not None and end <= NOW
    return {"job_id": job_id, "source_site": site, "user_name": "ann", "partition": part, "node_list": "g01",
            "state": "COMPLETED" if done else "RUNNING", "submit_time": iso(sub), "eligible_time": iso(sub),
            "start_time": iso(start), "end_time": iso(end) if done else None, "req_cpus": 4, "req_mem_mb": 4000,
            "req_gpus": 0, "alloc_tres": "cpu=4,mem=4000M,node=1", "alloc_gpus": 0, "runtime_seconds": 1800,
            "account": None, "req_time_seconds": 3600, "pending_reason": None, "req_nodes": 1,
            "ready_since": iso(ready), "wait_time_seconds": int((start - sub).total_seconds()) if start else None}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(f"CREATE TABLE jobs ({', '.join(COLS)})")
    rows = [
        # array 700 at alpha, sent five weeks ago: its first task then, three today, one seen waiting for room
        job("700_1", "alpha", SENT, SENT + timedelta(hours=1)),
        job("700_2", "alpha", SENT, NOW - timedelta(hours=3)),
        job("700_3", "alpha", SENT, NOW - timedelta(hours=2)),
        job("700_4", "alpha", SENT, NOW - timedelta(hours=1, minutes=30)),
        job("700_5", "alpha", SENT, NOW - timedelta(hours=1), ready=NOW - timedelta(hours=5)),
        # another array with the same number at beta: its first task, two hours after it was sent
        job("700_1", "beta", NOW - timedelta(hours=4), NOW - timedelta(hours=2)),
        # jobs that aren't arrays, and array 7000 (not 700's)
        job("701", "alpha", NOW - timedelta(hours=6), NOW - timedelta(hours=5)),
        *[job(f"70{k}", "alpha", NOW - timedelta(hours=k, minutes=10), NOW - timedelta(hours=k)) for k in range(2, 6)],
        job("7000_2", "alpha", SENT, NOW - timedelta(hours=1), end=NOW + timedelta(hours=1)),     # running
    ]
    for r in rows:
        c.execute(f"INSERT INTO jobs ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                  [r[k] for k in COLS])
    return c


def cols(c):
    return {r[1] for r in c.execute("PRAGMA table_info(jobs)")}


def test_the_wait_counts_an_array_once(conn):
    for alias, frm in (("j", "jobs j"), ("", "jobs")):
        a = f"{alias}." if alias else ""
        got = {(r[0], r[1]): r[2] for r in conn.execute(
            f"SELECT {a}job_id, {a}source_site, {queue_wait_sql(cols(conn), alias)} FROM {frm}")}
        assert got[("700_1", "alpha")] == 3600                       # the array's wait: by its first task
        for k in (2, 3, 4, 5):
            assert got[(f"700_{k}", "alpha")] is None                # not its age, five weeks each
        assert got[("700_1", "beta")] == 7200                        # another site's array of that number
        assert got[("7000_2", "alpha")] == 35 * 86400 - 3600         # array 7000's first: counted
        assert got[("701", "alpha")] == 3600
    assert array_of("700_4") == "700" and array_of("700_[5-9%2]") is None and array_of("700_4@2026-01-01") is None
    assert later_task_sql({"job_id"}) is None


def test_the_job_facts_and_the_submit_page(conn):
    rows = jobfacts.finished_rows(conn, iso(NOW - timedelta(days=40)), iso(NOW + timedelta(days=1)))
    w = jobfacts.summarize(rows)["wait_minutes"]
    # 700_1 at alpha (60) and beta (120), 701 (60), 702-705 (10 each); the ended later tasks left out
    assert w["jobs"] == 7 and w["median"] == 10
    # The Submit page's typical wait at alpha: 702-705's 10 minutes, not five weeks
    assert typical_waits(conn, "alpha", ["gpu"], now=NOW, days=60) == {"gpu": 600}


def test_the_condo_figures(conn):
    jobs = {j.job_id: j for j in F.load_jobs(conn, "alpha", NOW - timedelta(days=2), NOW, NOW)}
    assert {k for k, j in jobs.items() if j.follows} == {"700_2", "700_3", "700_4", "700_5"}
    assert jobs["700_2"].wait_s is None and jobs["701"].wait_s == 3600
    lo, hi = NOW - timedelta(days=1), NOW
    assert _waits_for_room(jobs["700_2"], lo, hi) is None           # five weeks? not known: none
    a, b, minutes = _waits_for_room(jobs["700_5"], lo, hi)          # seen waiting for room: from then
    assert minutes == 240 and a == NOW - timedelta(hours=5)
    assert _waits_for_room(jobs["701"], lo, hi)[2] == 60
    beta = F.load_jobs(conn, "beta", NOW - timedelta(days=2), NOW, NOW)
    assert not beta[0].follows and beta[0].wait_s == 7200
    out = SimpleNamespace(left_out=defaultdict(int))
    waiting = idle._candidates(out, list(jobs.values()), {"condo"}, 64, NOW - timedelta(days=2), NOW, NOW,
                               timedelta(days=3))
    assert out.left_out["array_pace"] == 3
    names = {w.job.job_id for w in waiting}
    assert "700_5" in names and not names & {"700_2", "700_3", "700_4"}
    assert [w.since for w in waiting if w.job.job_id == "700_5"] == [NOW - timedelta(hours=5)]


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO config (key, value) VALUES (?, 'done')", (S._STARTS_REPAIRED,))
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    col = SlurmCollector({}, str(db))

    def store(job_id, *, state="RUNNING", reason=None, ready=None, start=None, sub=SENT):
        col.store([{"type": "job", **JobInfo(
            job_id=job_id, user_name="ann", group_name="ann", partition="gpu", job_name="sweep", state=state,
            node_list=None if state == "PENDING" else "g01", submit_time=sub, start_time=start, end_time=None,
            exit_code=None, exit_signal=None, failure_reason=0, req_cpus=4, req_mem_mb=4000, req_gpus=0,
            req_time_seconds=3600, runtime_seconds=0, pending_reason=reason, ready_since=ready).to_dict()}])

    def row(job_id):
        return sqlite3.connect(db).execute("SELECT pending_reason, ready_since FROM jobs WHERE job_id = ?",
                                           (job_id,)).fetchone()
    return SimpleNamespace(store=store, row=row)


def test_the_collector_keeps_the_arrays_reason(site):
    # the array's pending tasks, held by its own limit (two at a time)
    site.store("800_[3-9%2]", state="PENDING", reason="JobArrayTaskLimit")
    site.store("800_3", start=NOW - timedelta(hours=1))
    assert site.row("800_3") == ("JobArrayTaskLimit", None)
    # then waiting for room, since half an hour ago, when 800_4 starts
    waited = NOW - timedelta(minutes=30)
    site.store("800_[4-9%2]", state="PENDING", reason="Priority", ready=waited)
    site.store("800_4", start=NOW)
    assert site.row("800_4") == ("Priority", iso(waited))
    # kept: a later reading of the task without a reason (sacct), and 800_3 as it was
    site.store("800_4", start=NOW)
    site.store("800_3", start=NOW - timedelta(hours=1))
    assert site.row("800_4") == ("Priority", iso(waited)) and site.row("800_3") == ("JobArrayTaskLimit", None)
    # another array's range is not this one's; nor another submission's of the same number
    site.store("8010_[2-5]", state="PENDING", reason="Resources")
    site.store("801_1", start=NOW)
    assert site.row("801_1") == (None, None)
    site.store("800_[5-9%2]", state="PENDING", reason="Resources", sub=SENT)
    site.store("800_1", start=NOW, sub=NOW - timedelta(hours=1))       # a new array 800 (numbers come round)
    assert site.row("800_1")[0] is None
