# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.32.0: what each running job uses now (the job_live collector, from sstat)."""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta

import pytest

from nomad.collectors import job_live as J
from nomad.collectors.base import CollectionError

# spydur, 6 Nov 2026: bchen's 87790 on spdr10 (52 cores, 350 GB asked), and an array task of jgyasi's on vadur
SQUEUE = ("87790|87790|bchen|52|1|2026-11-06T13:17:30\n"
          "12281|12267_14|jgyasi|6|1|2026-11-06T16:15:00\n"
          "bad line\n"
          "99|99|-x|1|1|2026-11-06T16:15:00\n")
SSTAT = ("87790.extern|0|0|00:00:00|1\n"
         "87790.batch|32562868K|30527952K|8-03:02:12|1\n"
         "12267_14.batch|51200000K|40000000K|05:00:00|1\n"
         "12267_14.0|2G|1G|01:00:00|4\n")


def test_parsers():
    jobs = J.parse_squeue(SQUEUE)
    assert [j["job_id"] for j in jobs] == ["87790", "12267_14"]
    assert jobs[1] == {"slurm_id": "12281", "job_id": "12267_14", "user": "jgyasi", "cpus": 6, "nodes": 1,
                       "start": "2026-11-06T16:15:00"}
    steps = J.parse_sstat(SSTAT)
    assert set(steps) == {"87790", "12267_14"} and len(steps["87790"]) == 2
    s = J.summarise(steps["87790"])
    assert s["measured"] == 1 and s["steps"] == "batch" and s["tasks"] == 1
    assert s["cpu_s"] == 702132.0 and round(s["peak_rss_mb"] / 1024, 1) == 31.1
    s = J.summarise(steps["12267_14"])
    assert s["steps"] == "0,batch" and s["tasks"] == 5
    assert s["cpu_s"] == 5 * 3600 + 4 * 3600                       # each step's AveCPU times its tasks
    assert s["rss_now_mb"] == round(40000000 / 1024 + 4 * 1024, 1)
    assert s["peak_rss_mb"] == 50000.0                              # the most one task held
    assert J.summarise([{"step": "extern", "max_rss_mb": 0, "ave_rss_mb": 0, "ave_cpu_s": 0, "tasks": 1}])["measured"] == 0
    assert J.size_mb("1048576") == 1.0 and J.size_mb("0") == 0.0 and J.size_mb("n/a") is None
    assert J.seconds("8-03:02:12") == 702132 and J.seconds("02:12.5") == 132.5 and J.seconds("x") is None


def test_busy_now():
    t = datetime(2026, 11, 6, 17, 0)
    prev = {"cpu_s": 1000.0, "steps": "batch", "start": "s", "sampled_at": t.isoformat()}
    d = {"cpu_s": 1000.0 + 46 * 300, "steps": "batch", "start": "s", "cpus": 52,
         "sampled_at": (t + timedelta(seconds=300)).isoformat()}
    assert J.JobLiveCollector.busy_now(prev, d) == 46.0
    assert J.JobLiveCollector.busy_now(None, d) is None
    assert J.JobLiveCollector.busy_now({**prev, "steps": "0,batch"}, d) is None          # other steps then
    assert J.JobLiveCollector.busy_now({**prev, "start": "t"}, d) is None                # requeued: another run
    assert J.JobLiveCollector.busy_now({**prev, "cpu_s": 99999999.0}, d) is None         # less CPU time now
    # 1.32.1: not held to the cores asked: a cluster that doesn't keep jobs to them shows
    assert J.JobLiveCollector.busy_now(prev, {**d, "cpu_s": 1000.0 + 60 * 300}) == 60.0


class Fake(J.JobLiveCollector):
    """squeue and sstat pretended; sstat as Slurm's account through sudo."""

    def __init__(self, db, squeue=SQUEUE, sstat=SSTAT, sstat_rc=0, sstat_err="", **cfg):
        super().__init__({"sstat_as": "slurm", **cfg}, db)
        self.out = {"squeue": squeue, "sstat": sstat}
        self.rc, self.err, self.calls = sstat_rc, sstat_err, []

    def _argv(self):
        return ["/usr/bin/sudo", "-n", "-u", self.sstat_as, "/usr/bin/sstat"]

    def _run(self, argv):
        self.calls.append(argv)
        if argv[0].endswith("squeue"):
            return subprocess.CompletedProcess(argv, 0, self.out["squeue"], "")
        return subprocess.CompletedProcess(argv, self.rc, self.out["sstat"], self.err)


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(J, "find_tool", lambda n: f"/usr/bin/{n}")


def test_collect_and_store(tmp_path, tools):
    db = tmp_path / "s.db"
    c = Fake(db)
    rows = c.collect()
    sstat = [a for a in c.calls if a[-1].startswith("JobID")]
    assert sstat == [["/usr/bin/sudo", "-n", "-u", "slurm", "/usr/bin/sstat", "-a", "-n", "-P", "-j", "87790,12281",
                      "-o", "JobID,MaxRSS,AveRSS,AveCPU,NTasks"]]
    by = {r["job_id"]: r for r in rows}
    assert by["87790"]["measured"] == 1 and by["87790"]["busy_avg"] == round(702132 / by["87790"]["elapsed_s"], 2)
    c.store(rows)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    got = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_live")}
    assert set(got) == {"87790", "12267_14"} and got["87790"]["busy_now"] is None     # nothing to compare yet
    # five minutes on: 46 cores' worth more CPU time; 12267_14 has ended
    later = SSTAT.replace("8-03:02:12", "8-06:52:12")
    c2 = Fake(db, squeue=SQUEUE.split("\n")[0] + "\n", sstat=later)
    rows2 = c2.collect()
    rows2[0]["sampled_at"] = (datetime.fromisoformat(got["87790"]["sampled_at"]) + timedelta(seconds=300)).isoformat()
    c2.store(rows2)
    got = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_live")}
    assert set(got) == {"87790"} and got["87790"]["busy_now"] == 46.0
    # nothing running: nothing kept
    Fake(db, squeue="").store(Fake(db, squeue="").collect())
    assert conn.execute("SELECT COUNT(*) FROM job_live").fetchone()[0] == 0


def test_a_job_sstat_has_nothing_of(tmp_path, tools):
    c = Fake(tmp_path / "s.db", sstat="87790.batch|32562868K|30527952K|8-03:02:12|1\n",
             sstat_rc=1, sstat_err="sstat: error: couldn't get steps for job 12281\n")
    by = {r["job_id"]: r for r in c.collect()}
    assert by["87790"]["measured"] == 1 and by["12267_14"]["measured"] == 0 and by["12267_14"]["cpu_s"] is None
    with pytest.raises(CollectionError, match="couldn't get steps"):
        Fake(tmp_path / "s.db", sstat="", sstat_rc=1, sstat_err="sstat: error: couldn't get steps\n").collect()


def test_who_runs_sstat(tmp_path, monkeypatch):
    from nomad.collectors.plan import plan
    cfg = {"actions": {"slurm_as": "slurm"}, "collectors": {"job_live": {"enabled": True}}}
    p = next(x for x in plan(cfg) if x.spec.name == "job_live")
    assert p.enabled and p.config["sstat_as"] == "slurm"
    cfg["collectors"]["job_live"]["sstat_as"] = "self"
    assert next(x for x in plan(cfg) if x.spec.name == "job_live").config["sstat_as"] == "self"
    assert not next(x for x in plan({}) if x.spec.name == "job_live").enabled            # off unless enabled
    c = J.JobLiveCollector({}, tmp_path / "s.db")
    with pytest.raises(CollectionError, match="sstat_as"):
        c._argv()
    c = J.JobLiveCollector({"sstat_as": "-u root"}, tmp_path / "s.db")
    assert c.sstat_as is None
    # through sudo: only when the nomad here is safe from others (nomad node drain's check)
    from nomad import drain
    monkeypatch.setattr(drain, "find", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(drain, "_install_checked", lambda: "/opt/x can be changed by others (mode 777)")
    c = J.JobLiveCollector({"sstat_as": "slurm"}, tmp_path / "s.db")
    with pytest.raises(CollectionError, match="someone else could change the nomad"):
        c._argv()
    monkeypatch.setattr(drain, "_install_checked", lambda: None)
    monkeypatch.setattr(J.shutil, "which", lambda n, path=None: "/usr/bin/sudo")
    assert c._argv() == ["/usr/bin/sudo", "-n", "-u", "slurm", "/usr/bin/sstat"]
    assert J.JobLiveCollector({"sstat_as": "self"}, tmp_path / "s.db")._argv() == ["/usr/bin/sstat"]


# --- 1.32.1 ------------------------------------------------------------------------------

def test_an_arrays_tasks_counted_once(tmp_path, tools):
    """vadur, 6 Nov: asked for 12266 (array 12266's last task, its own id), sstat also
    answered for the array's other running tasks, 12987 (12266_48) and 13020 (12266_81),
    which were asked for by their own ids too: they came back twice and read 10.7 and
    10.5 cores busy of the 6 asked, twice their 5.3."""
    squeue = ("12987|12266_48|jgyasi|6|1|2026-11-04T10:00:00\n"
              "13020|12266_81|jgyasi|6|1|2026-11-05T10:00:00\n"
              "12266|12266_99|jgyasi|6|1|2026-11-06T10:00:00\n")
    sstat = ("12987.batch|2905848K|2781828K|28-22:38:45|1\n"
             "13020.batch|1462448K|1398628K|11-21:05:04|1\n"
             "12266.batch|1373780K|1332612K|6-21:58:43|1\n"
             "12987.batch|2905848K|2781828K|28-22:38:45|1\n"
             "13020.batch|1462448K|1398628K|11-21:05:04|1\n")
    assert [len(v) for v in J.parse_sstat(sstat).values()] == [1, 1, 1]
    by = {r["job_id"]: r for r in Fake(tmp_path / "s.db", squeue=squeue, sstat=sstat).collect()}
    assert by["12266_48"]["cpu_s"] == J.seconds("28-22:38:45")
    assert by["12266_48"]["rss_now_mb"] == round(2781828 / 1024, 1)

    class TwoCalls(Fake):                       # the same step in two calls: still once
        def _run(self, argv):
            if not argv[0].endswith("squeue"):
                self.out["sstat"] = "12987.batch|2905848K|2781828K|28-22:38:45|1\n"
            return super()._run(argv)
    monkey = TwoCalls(tmp_path / "s.db", squeue=squeue)
    J_BATCH, J.BATCH = J.BATCH, 1
    try:
        by = {r["job_id"]: r for r in monkey.collect()}
    finally:
        J.BATCH = J_BATCH
    assert by["12266_48"]["cpu_s"] == J.seconds("28-22:38:45")


def test_nothing_running_clears_the_table(tmp_path, tools):
    db = tmp_path / "s.db"
    c = Fake(db)
    c.store(c.collect())
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM job_live").fetchone()[0] == 2
    assert Fake(db, squeue="").collect() == []             # collect itself clears: run() stores nothing
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM job_live").fetchone()[0] == 0
    fresh = tmp_path / "fresh.db"                         # and the table is there from the first run
    Fake(fresh, squeue="").collect()
    assert sqlite3.connect(fresh).execute("SELECT COUNT(*) FROM job_live").fetchone()[0] == 0


def test_the_example_toml_has_todays_settings():
    import tomllib
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "nomad.toml.example").read_text()
    d = tomllib.loads(text)
    assert d["collectors"]["job_live"] == {"enabled": False} and "actions" in d
    for key in ("# interval = 300", '# sstat_as = "slurm"', '# slurm_as = "slurm"', "# agent_key =",
                "# allow_unrestricted = false", "Defaults:ACCOUNT !requiretty", "NOPASSWD: /usr/bin/sstat"):
        assert key in text, key
