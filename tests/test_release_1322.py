# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.32.2: Slurm's MaxRSS is a job's largest task's peak, not the job's. For a
job of many tasks at once (MPI, or steps run side by side), what they held
together: a running job's at nomad's readings, and their own peaks added up;
an ended job's most tasks at once."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import job_live as J
from nomad.collectors import job_metrics as JM
from nomad.collectors.job_metrics import JobMetricsCollector
from nomad.db import ensure_database

GB = 1024.0


def step(name, max_gb, ave_gb, tasks):
    return {"step": name, "max_rss_mb": None if max_gb is None else max_gb * GB,
            "ave_rss_mb": None if ave_gb is None else ave_gb * GB, "ave_cpu_s": 60.0, "tasks": tasks}


# -- running jobs ------------------------------------------------------------------------

def test_their_peaks_added_up():
    # bchen's 87790: one program, many threads: its one task's peak is the job's
    s = J.summarise([step("extern", 0, 0, 1), step("batch", 31.1, 29.5, 1)])
    assert s["peaks_sum_mb"] == s["peak_rss_mb"] == round(31.1 * GB, 1)
    # 40 tasks of about 2 GB (MPI): MaxRSS is one task's; added up, 88 GB, never less than the job's
    s = J.summarise([step("batch", 0.1, 0.1, 1), step("0", 2.2, 1.9, 40)])
    assert s["peak_rss_mb"] == round(2.2 * GB, 1)
    assert s["peaks_sum_mb"] == round((0.1 + 40 * 2.2) * GB, 1)
    assert s["rss_now_mb"] == round((0.1 + 40 * 1.9) * GB, 1)
    # tasks spread unevenly over nodes don't matter: it is the whole job's
    s = J.summarise([step("0", 1.6, 1.5, 60)])
    assert s["peaks_sum_mb"] == round(60 * 1.6 * GB, 1)
    assert J.summarise([step("batch", None, None, 1)])["peaks_sum_mb"] is None


def test_what_was_started_by_ssh_into_the_node_counts():
    # pam_slurm_adopt: a notebook reached by ssh holds 30 GB in the extern step; the batch step sleeps
    s = J.summarise([step("extern", 31, 30, 1), step("batch", 0.01, 0.01, 1)])
    assert s["tasks"] == 1 and s["steps"] == "batch"                     # not one of its steps
    assert round(s["rss_now_mb"] / GB) == 30 and round(s["peaks_sum_mb"] / GB) == 31
    assert J.summarise([step("extern", 1, 1, 1)])["measured"] == 0


@pytest.mark.parametrize("before, now, unseen", [
    (None, "batch", 0),
    (None, "0,batch", 0),
    (None, "2,batch", 1),                    # steps 0 and 1 came and went before nomad read it
    ("batch", "0,batch", 0),
    ("0,batch", "0,1,batch", 0),
    ("0,batch", "1,batch", 1),               # step 0 ended since it was read
    ("0,batch", "0,2,batch", 1),             # step 1 came and went between readings
    ("0,1,batch", "0,1,2,3,batch", 0),
])
def test_a_step_gone_unseen(before, now, unseen):
    prev = None if before is None else {"start": "s", "measured": 1, "steps": before, "steps_unseen": 0}
    assert J.JobLiveCollector.unseen(prev, {"start": "s", "measured": 1, "steps": now}) == unseen


def test_once_unseen_for_the_rest_of_the_run():
    prev = {"start": "s", "measured": 1, "steps": "1,batch", "steps_unseen": 1}
    assert J.JobLiveCollector.unseen(prev, {"start": "s", "measured": 1, "steps": "1,batch"}) == 1
    assert J.JobLiveCollector.unseen(prev, {"start": "s", "measured": 0, "steps": None}) == 1
    # requeued: another run
    assert J.JobLiveCollector.unseen(prev, {"start": "t", "measured": 1, "steps": "0,batch"}) == 0
    # sstat said nothing of it this time: no step taken for gone
    ok = {"start": "s", "measured": 1, "steps": "0,batch", "steps_unseen": 0}
    assert J.JobLiveCollector.unseen(ok, {"start": "s", "measured": 0, "steps": None}) == 0


class Fake(J.JobLiveCollector):
    def __init__(self, db, squeue, sstat):
        super().__init__({"sstat_as": "slurm"}, db)
        self.out = {"squeue": squeue, "sstat": sstat}

    def _argv(self):
        return ["/usr/bin/sstat"]

    def _run(self, argv):
        return subprocess.CompletedProcess(argv, 0, self.out["squeue" if argv[0].endswith("squeue") else "sstat"], "")


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(J, "find_tool", lambda n: f"/usr/bin/{n}")


SQ = "500|500|ia3nk|40|1|2026-11-06T09:00:00\n"


def read(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_live")}
    finally:
        conn.close()


def run(db, squeue, sstat):
    c = Fake(db, squeue, sstat)
    c.store(c.collect())
    return read(db)


def test_the_most_kept_across_readings_and_steps(tmp_path, tools):
    db = tmp_path / "s.db"
    for ave in ("1.5G", "1.9G", "1.2G"):                           # 60, 76, then 48 GB held
        got = run(db, SQ, f"500.batch|10M|10M|00:00:01|1\n500.0|2G|{ave}|01:00:00|40\n")["500"]
    assert round(got["rss_now_mb"] / GB) == 48 and round(got["held_most_mb"] / GB) == 76
    assert round(got["peaks_sum_mb"] / GB) == 80
    assert got["steps_unseen"] == 0
    # the MPI step has ended; a small one runs: what the job held before is kept, and a step went unseen
    got = run(db, SQ, "500.batch|10M|10M|00:00:01|1\n500.1|0.3G|0.3G|00:01:00|40\n")["500"]
    assert round(got["held_most_mb"] / GB) == 76 and round(got["peaks_sum_mb"] / GB) == 80
    assert round(got["peak_rss_mb"] / GB, 1) == 0.3 and got["steps_unseen"] == 1
    # requeued (another start): another run, counted anew
    got = run(db, SQ.replace("09:00:00", "10:00:00"), "500.0|1G|1G|00:01:00|40\n")["500"]
    assert round(got["held_most_mb"] / GB) == 40 and round(got["peaks_sum_mb"] / GB) == 40
    assert got["steps_unseen"] == 0


def test_a_table_made_by_1321_gains_the_columns(tmp_path, tools):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE job_live (
            job_id TEXT PRIMARY KEY, slurm_id TEXT, user TEXT, cpus INTEGER, nodes INTEGER, start TEXT,
            sampled_at TEXT NOT NULL, elapsed_s INTEGER, measured INTEGER, tasks INTEGER, steps TEXT,
            cpu_s REAL, busy_now REAL, busy_avg REAL, rss_now_mb REAL, peak_rss_mb REAL);
        INSERT INTO job_live (job_id, sampled_at, start, cpu_s, steps) VALUES ('500', '2000-01-01T00:00:00',
            '2026-11-06T09:00:00', 1.0, '0,batch');""")
    conn.commit()
    conn.close()
    got = run(db, SQ, "500.batch|10M|10M|00:00:01|1\n500.0|2G|1G|01:00:00|40\n")["500"]
    assert got["held_most_mb"] == got["rss_now_mb"] and got["peaks_sum_mb"] > 80 * GB
    assert got["busy_now"] is not None and got["steps_unseen"] == 0      # the row before still read


# -- ended jobs: the most tasks at once --------------------------------------------------

def t(hhmm):
    return datetime(2026, 11, 6, int(hhmm[:2]), int(hhmm[3:]))


def sp(name, a, b, n, gb=0.05):
    return (name, a, b, n, gb * GB)


@pytest.mark.parametrize("spans, most", [
    ([sp("batch", t("09:00"), t("10:00"), 1, 30)], 1),                                 # a program, threads
    ([sp("batch", t("09:00"), t("10:00"), 1), sp("0", t("09:00"), t("10:00"), 1)], 1),  # srun ./prog: its shell beside
    ([sp("batch", t("09:00"), t("10:00"), 1), sp("0", t("09:00"), t("10:00"), 40)], 40),  # MPI
    ([sp("extern", t("09:00"), t("10:00"), 1), sp("batch", t("09:00"), t("10:00"), 1)] +
     [sp(str(i), t("09:00"), t("09:50"), 1) for i in range(20)], 20),                    # srun -n1 ... & twenty times
    ([sp("0", t("09:00"), t("09:30"), 1), sp("1", t("09:30"), t("10:00"), 1)], 1),       # one after another
    ([sp("0", t("09:00"), t("09:30") + timedelta(seconds=2), 1),
      sp("1", t("09:30"), t("10:00"), 1)], 1),                                           # its end recorded just after
    ([sp("0", t("09:00"), t("09:40"), 4), sp("1", t("09:30"), t("10:00"), 4)], 8),       # overlapping
    ([sp("0", None, None, 2), sp("1", t("09:30"), t("10:00"), 3)], 5),                   # times not known: overlaps
    # a 30 GB driver in the batch step beside the solver it started: two programs at once
    ([sp("batch", t("09:00"), t("10:00"), 1, 30), sp("0", t("09:10"), t("09:50"), 1, 30)], 2),
    ([sp("interactive", t("09:00"), t("10:00"), 1), sp("0", t("09:10"), t("09:50"), 1)], 1),   # salloc's shell
    ([], 0),
])
def test_the_most_tasks_at_once(spans, most):
    assert JobMetricsCollector._most_at_once(spans) == most


def row(job_id, *, start="2026-10-05T09:00:00", end="2026-10-05T10:00:00", maxrss="", ntasks=""):
    parts = [job_id, "run", "u1", "people", "basic", "COMPLETED", "0:0", "2026-10-05T09:00:00", start, end,
             "01:00:00", "02:00:00", "20", "100G", "cpu=20", "00:10:00" if ntasks else "", maxrss, "", "",
             "", "", "", "", "cn01", "20:00:00" if not ntasks else "", ntasks, "", "sbatch pack.sh"]
    return "|".join(parts)


def test_an_ended_job_of_steps_side_by_side(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    lines = [row("700"), row("700.batch", maxrss="50M", ntasks="1")]
    lines += [row(f"700.{i}", end="2026-10-05T09:50:00", maxrss="4G", ntasks="1") for i in range(20)]
    lines += [row("701"), row("701.batch", maxrss="50M", ntasks="1"),
              row("701.0", end="2026-10-05T09:30:00", maxrss="4G", ntasks="1"),
              row("701.1", start="2026-10-05T09:30:00", maxrss="4G", ntasks="1")]
    # a step whose tasks Slurm didn't say: taken for more than one
    lines += [row("702"), row("702.batch", maxrss="50M", ntasks="1"), row("702.0", maxrss="4G")]
    monkeypatch.setattr(JM.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr=""))
    col = JobMetricsCollector({}, str(db))
    col.store(col.collect())
    c = sqlite3.connect(db)
    got = dict(c.execute("SELECT job_id, max_tasks FROM job_summary"))
    c.close()
    assert got == {"700": 20, "701": 1, "702": 2}   # twenty at once: 4 GB is one task's; one after another: the job's
