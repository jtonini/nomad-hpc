"""1.7.42: a job number Slurm gives out again is a new job; start times are
real starts; sacct's Account, AllocTRES and WorkDir are kept; node lists in
Slurm's range form are read as the nodes they name."""
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import slurm as S
from nomad.collectors.job_metrics import JobMetrics, JobMetricsCollector
from nomad.collectors.slurm import SlurmCollector, gpu_count, work_dir_parts
from nomad.db import jobkeys
from nomad.db.migrations import ensure_database
from nomad.hostlist import MAX_NODES, expand_hostlist, like_clause, like_patterns, node_in


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# -- node lists -------------------------------------------------------------------

@pytest.mark.parametrize("text, nodes", [
    ("cn[01-02,05],gpu17", ["cn01", "cn02", "cn05", "gpu17"]),
    ("cn17", ["cn17"]),
    ("n[8-11]", ["n8", "n9", "n10", "n11"]),
    ("n[008-010]", ["n008", "n009", "n010"]),
    ("rack[1-2]n[01-02]", ["rack1n01", "rack1n02", "rack2n01", "rack2n02"]),
    ("a,a,b", ["a", "b"]),
    (" cn[17-18] ", ["cn17", "cn18"]),
    ("", []), (None, []), ("None assigned", []), ("(null)", []),
])
def test_node_lists_expand(text, nodes):
    assert expand_hostlist(text) == nodes


def test_a_node_list_that_cant_be_read_gives_what_can():
    assert expand_hostlist("cn[17-18,good") == []
    assert expand_hostlist("cn[17-18,ok],x01") == ["x01"]
    assert expand_hostlist("bad[1-99999999],x01") == ["x01"]
    assert expand_hostlist("bad[9-1],x01") == ["x01"]
    assert expand_hostlist("bad[a-b],x01") == ["x01"]
    assert len(expand_hostlist(f"n[0-{MAX_NODES - 1}],m[0-9]")) == MAX_NODES


def test_node_in_and_like_patterns():
    assert node_in("cn02", "cn[01-02]") and not node_in("cn1", "cn[10-18]")
    assert not node_in("cn10", "cn1") and not node_in("", "cn01")
    assert like_patterns("cn02") == ("%cn02%", "%cn[%")
    assert like_patterns("rack1n02") == ("%rack1n02%", "%rack[%", "%rack1n[%")
    assert like_patterns("my_node1") == ("%my\\_node1%", "%my\\_node[%")
    assert like_patterns("login") == ("%login%",)


@pytest.mark.parametrize("node, listed", [
    ("cn02", ["cn[01-02]", "cn02", "x01,cn02", "cn[01,02]"]),
    ("my_node2", ["my_node[1-2]", "my_node2"]),
    ("rack1n02", ["rack[1-2]n[01-02]", "rack1n[01-04]"]),
    ("a1b2c", ["a1b[2-3]c"]),
])
def test_like_clause_finds_every_list_naming_the_node(node, listed):
    others = ["myXnode1", "cn20", "cn[10-12]", "rack2n02"]
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (n TEXT)")
    c.executemany("INSERT INTO t VALUES (?)", [(x,) for x in listed + others])
    where, pats = like_clause("n", node)
    rows = [r[0] for r in c.execute(f"SELECT n FROM t WHERE {where}", pats)]
    assert set(listed) <= set(rows)
    assert [r for r in rows if node_in(node, r)] == listed
    # Lists that spell out other nodes are not even looked at.
    assert "cn20" not in rows and "myXnode1" not in rows


# -- sacct fields ------------------------------------------------------------------

def test_gpu_count_reads_only_gpus():
    assert gpu_count("cpu=6,gres/gpu:tesla_a40=1,gres/gpu=1,mem=32G,node=1") == 1
    assert gpu_count("cpu=4,gres/gpu=4,node=1") == 4
    assert gpu_count("cpu=4,gres/gpumem=40960,gres/gpuutil=100,gres/gpu=2") == 2
    assert gpu_count("cpu=8,mem=41G,node=1") == 0
    assert gpu_count("") == 0 and gpu_count(None) == 0


def test_req_gpus_no_longer_counts_gpu_memory():
    col = SlurmCollector({}, ":memory:")
    assert col._parse_gpus("cpu=4,gres/gpumem=40960,gres/gpu=2") == 2
    assert col._parse_gpus("gres/gpumem=40960") == 0
    assert col._parse_gpus("gpu:a100:2") == 2
    assert col._parse_gpus("N/A") == 0


def test_work_dir_keeps_only_its_ends():
    assert work_dir_parts("/home/u1/proj/run1") == ("/home", "proj/run1")
    assert work_dir_parts("/scratch/u1") == ("/scratch", "scratch/u1")
    assert work_dir_parts("/root") == ("/root", "root")
    assert work_dir_parts("") == (None, None)
    assert work_dir_parts("relative/dir") == (None, None)
    assert work_dir_parts(None) == (None, None)


LINE = ("4711|u1|people|basic|myjob|COMPLETED|cn[01-02]|8|41G|billing=8,cpu=8|1-00:00:00|"
        "00:10:00|2026-10-05T09:00:00|2026-10-05T09:01:00|2026-10-05T09:11:00|0:0|"
        "{account}|{tres}|{workdir}")


def test_sacct_line_gives_account_alloc_and_work_dir():
    col = SlurmCollector({}, ":memory:")
    j = col._parse_sacct_job(LINE.format(account="lab1", tres="cpu=8,gres/gpu=2,mem=41G",
                                         workdir="/home/u1/a|b/run"))
    assert (j.account, j.alloc_tres, j.alloc_gpus) == ("lab1", "cpu=8,gres/gpu=2,mem=41G", 2)
    assert (j.work_root, j.work_tail) == ("/home", "a|b/run")
    j = col._parse_sacct_job(LINE.format(account="", tres="", workdir=""))
    assert (j.account, j.alloc_tres, j.alloc_gpus, j.work_root, j.work_tail) == (None,) * 5
    # An old-format line (16 fields) still parses, without the new fields.
    old = LINE.rsplit("|", 3)[0]
    j = col._parse_sacct_job(old)
    assert j is not None and j.account is None and j.runtime_seconds == 600


# -- the collector, end to end ------------------------------------------------------

SQ = "{id}|{user}|people|basic|{name}|{state}|{nodes}|4|4G|N/A|1-00:00:00|0:00|{submit}|{start}"
SA = ("{id}|{user}|people|basic|{name}|{state}|{nodes}|8|41G|billing=8|1-00:00:00|{elapsed}|"
      "{submit}|{start}|{end}|0:0|{account}|{tres}|{workdir}")


class Fake:
    def __init__(self, queue=(), history=(), known=None, sacct_ok=True):
        self.queue, self.history = list(queue), list(history)
        self.known = dict(known or {})
        self.sacct_ok = sacct_ok
        self.looked_up = []

    def __call__(self, argv, **kw):
        out = ""
        if argv[0] == "squeue":
            if argv[-1] == "%P|%t":
                out = "\n".join("basic|PD" for _ in self.queue)
            else:
                out = "\n".join(SQ.format(**q) for q in self.queue)
        elif argv[0] == "sacct":
            if not self.sacct_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="down")
            if "-j" in argv:
                ids = argv[argv.index("-j") + 1].split(",")
                self.looked_up += ids
                out = "\n".join(SA.format(**self.known[i]) for i in ids if i in self.known)
            else:
                out = "\n".join(SA.format(**h) for h in self.history)
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")


def sa(id, user="u1", name="job", state="COMPLETED", nodes="cn01", submit="2026-10-05T09:00:00",
       start="2026-10-05T09:01:00", end="2026-10-05T09:11:00", elapsed="00:10:00",
       account="", tres="cpu=8,mem=41G,node=1", workdir="/home/u1/p/r"):
    return dict(id=id, user=user, name=name, state=state, nodes=nodes, submit=submit,
                start=start, end=end, elapsed=elapsed, account=account, tres=tres,
                workdir=workdir)


def sq(id, user="u1", name="job", state="PENDING", nodes="", submit="2026-10-05T09:00:00",
       start="2026-10-27T19:43:30"):
    return dict(id=id, user=user, name=name, state=state, nodes=nodes, submit=submit, start=start)


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))

    def run(fake):
        col = SlurmCollector({}, str(db))
        monkeypatch.setattr(S.subprocess, "run", fake)
        col.store(col.collect())
        return fake

    def rows(sql, *args):
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        out = [dict(r) for r in c.execute(sql, args)]
        c.close()
        return out

    def execute(sql, *args):
        with sqlite3.connect(db) as c:
            c.execute(sql, args)
    return SimpleNamespace(db=str(db), run=run, rows=rows, execute=execute)


def mark_repaired(site):
    site.execute("INSERT INTO config (key, value) VALUES (?, 'test')", S._STARTS_REPAIRED)


def old_job(site, job_id="4711", user="old", name="oldjob", submit="2026-03-01T10:00:00",
            end="2026-03-01T12:00:00", state="COMPLETED"):
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, end_time, runtime_seconds) VALUES (?, ?, ?, 'basic', ?, ?, ?, ?, ?)",
                 job_id, user, name, state, submit, submit, end, 7200)
    site.execute("INSERT INTO job_summary (job_id, peak_memory_gb) VALUES (?, 99.0)", job_id)
    site.execute("INSERT INTO job_metrics (job_id, timestamp) VALUES (?, ?)", job_id, submit)


def test_a_number_given_out_again_is_a_new_job(site):
    mark_repaired(site)
    old_job(site)
    site.run(Fake(queue=[sq("4711", user="new", name="newjob", state="RUNNING", nodes="cn02",
                            submit="2026-10-05T09:00:00", start="2026-10-05T09:05:00")]))
    by_id = {r["job_id"]: r for r in site.rows("SELECT * FROM jobs")}
    assert set(by_id) == {"4711", "4711@2026-03-01T10:00:00"}
    assert by_id["4711"]["user_name"] == "new" and by_id["4711"]["node_list"] == "cn02"
    old = by_id["4711@2026-03-01T10:00:00"]
    assert (old["user_name"], old["end_time"], old["state"]) == (
        "old", "2026-03-01T12:00:00", "COMPLETED")
    # The old job's metrics went with it; the new job inherits nothing.
    assert [r["job_id"] for r in site.rows("SELECT job_id FROM job_summary")] == [
        "4711@2026-03-01T10:00:00"]
    assert [r["job_id"] for r in site.rows("SELECT job_id FROM job_metrics")] == [
        "4711@2026-03-01T10:00:00"]


def test_the_new_job_then_updates_in_place(site):
    mark_repaired(site)
    old_job(site)
    new = dict(user="new", name="newjob", submit="2026-10-05T09:00:00")
    site.run(Fake(queue=[sq("4711", state="PENDING", **new)]))
    site.run(Fake(history=[sa("4711", start="2026-10-05T09:01:00", end="2026-10-05T09:11:00",
                              **new)]))
    ids = sorted(r["job_id"] for r in site.rows("SELECT job_id FROM jobs"))
    assert ids == ["4711", "4711@2026-03-01T10:00:00"]
    r = site.rows("SELECT * FROM jobs WHERE job_id = '4711'")[0]
    assert r["user_name"] == "new" and r["state"] == "COMPLETED" and r["wait_time_seconds"] == 60


def test_a_requeued_job_stays_one_job(site):
    """Same user and name, the stored job still active: a requeue, not a new job."""
    mark_repaired(site)
    old_job(site, user="u1", name="job", submit="2026-10-05T08:00:00", end=None, state="RUNNING")
    site.run(Fake(queue=[sq("4711", state="PENDING", submit="2026-10-05T09:00:00")]))
    assert [r["job_id"] for r in site.rows("SELECT job_id FROM jobs")] == ["4711"]


def test_same_user_and_name_long_after_is_a_new_job(site):
    mark_repaired(site)
    old_job(site, user="u1", name="job")
    site.run(Fake(queue=[sq("4711", state="PENDING", submit="2026-10-05T09:00:00")]))
    assert len(site.rows("SELECT job_id FROM jobs")) == 2


def test_job_metrics_moves_the_old_job_aside_too(site):
    old_job(site)
    m = JobMetrics(job_id="4711", job_name="newjob", user_name="new", group_name=None,
                   partition="basic", state="COMPLETED", exit_code=0,
                   submit_time=datetime(2026, 10, 5, 9), start_time=datetime(2026, 10, 5, 9, 2),
                   end_time=datetime(2026, 10, 5, 9, 12), elapsed_seconds=600,
                   timelimit_seconds=3600, req_cpus=4, req_mem_mb=4096, req_gpus=0,
                   avg_cpu_percent=50.0, max_rss_mb=1024.0, avg_rss_mb=512.0,
                   max_vmsize_mb=None, max_disk_read_mb=None, max_disk_write_mb=None,
                   avg_disk_read_mb=None, avg_disk_write_mb=None, node_list="cn[01-02]")
    col = JobMetricsCollector({}, site.db)
    col.store([{"type": "job_metrics", **m.to_dict()}])
    by_id = {r["job_id"]: r for r in site.rows("SELECT * FROM jobs")}
    assert by_id["4711"]["user_name"] == "new" and by_id["4711"]["wait_time_seconds"] == 120
    assert by_id["4711@2026-03-01T10:00:00"]["user_name"] == "old"
    summary = {r["job_id"]: r["peak_memory_gb"] for r in site.rows("SELECT * FROM job_summary")}
    assert summary == {"4711@2026-03-01T10:00:00": 99.0, "4711": 1.0}


def test_place_for_an_older_record_and_a_taken_aside_id(tmp_path, caplog):
    db = tmp_path / "x.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, state, submit_time, end_time) "
              "VALUES ('9', 'b', 'n', 'COMPLETED', '2026-10-01T00:00:00', '2026-10-01T01:00:00')")
    # A record from the past (an imported history) for another job: its own aside id.
    assert jobkeys.place(c, "9", "2026-01-01T00:00:00", "a", "m") == "9@2026-01-01T00:00:00"
    # The same job seen earlier than what is stored: keep what is stored.
    assert jobkeys.place(c, "9", "2026-09-30T00:00:00", "b", "n") is None
    # No submit time, or the same one: the number.
    assert jobkeys.place(c, "9", None, "a", "m") == "9"
    assert jobkeys.place(c, "9", "2026-10-01 00:00:00", "b", "n") == "9"
    # Placing the same older record again: the same place (no ~2, ~3 copies).
    assert jobkeys.place(c, "9", "2026-01-01T00:00:00", "a", "m") == "9@2026-01-01T00:00:00"
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, state, submit_time) "
              "VALUES ('9@2026-01-01T00:00:00', 'a', 'm', 'COMPLETED', '2026-01-01T00:00:00')")
    assert jobkeys.place(c, "9", "2026-01-01T00:00:00", "a", "m") == "9@2026-01-01T00:00:00"
    # The aside id taken by another job: the next free one.
    c.execute("INSERT INTO jobs (job_id, user_name, state, submit_time) "
              "VALUES ('9@2026-10-01T00:00:00', 'x', 'COMPLETED', '2026-10-01T00:00:00')")
    assert jobkeys.place(c, "9", "2027-06-01T00:00:00", "z", "q") == "9"
    assert c.execute("SELECT user_name FROM jobs WHERE job_id = '9@2026-10-01T00:00:00~2'"
                     ).fetchone()[0] == "b"
    assert c.execute("SELECT 1 FROM jobs WHERE job_id = '9'").fetchone() is None
    # None free: the record is not stored, and that is said.
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, state, submit_time, end_time) "
              "VALUES ('8', 'b', 'n', 'COMPLETED', '2026-10-01T00:00:00', '2026-10-01T01:00:00')")
    for n in ["", "~2", "~3", "~4", "~5", "~6", "~7", "~8", "~9"]:
        c.execute("INSERT INTO jobs (job_id, user_name, state) VALUES (?, 'x', 'COMPLETED')",
                  (f"8@2026-10-01T00:00:00{n}",))
    with caplog.at_level(logging.WARNING, logger="nomad.db.jobkeys"):
        assert jobkeys.place(c, "8", "2027-06-01T00:00:00", "z", "q") is None
    assert "could not move" in caplog.text
    assert c.execute("SELECT user_name FROM jobs WHERE job_id = '8'").fetchone()[0] == "b"


def test_every_table_with_a_job_id_moves(tmp_path):
    c = sqlite3.connect(tmp_path / "y.db")
    c.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT, job_name TEXT, "
              "state TEXT, submit_time TEXT, end_time TEXT)")
    c.execute("CREATE TABLE job_similarity (job_id_a TEXT, job_id_b TEXT, UNIQUE(job_id_a, job_id_b))")
    c.execute("CREATE TABLE proficiency_scores (id INTEGER PRIMARY KEY, job_id TEXT)")
    c.execute("CREATE TABLE unrelated (x TEXT)")
    c.execute("INSERT INTO jobs VALUES ('5', 'a', 'n', 'FAILED', '2026-01-01T00:00:00', "
              "'2026-01-01T00:10:00')")
    c.execute("INSERT INTO job_similarity VALUES ('5', '6'), ('7', '5')")
    c.execute("INSERT INTO proficiency_scores (job_id) VALUES ('5')")
    assert jobkeys.place(c, "5", "2026-09-01T00:00:00", "b", "n") == "5"
    new = "5@2026-01-01T00:00:00"
    assert sorted(c.execute("SELECT job_id_a, job_id_b FROM job_similarity").fetchall()) == [
        (new, "6"), ("7", new)]
    assert c.execute("SELECT job_id FROM proficiency_scores").fetchall() == [(new,)]


# -- start times ---------------------------------------------------------------------

def test_a_pending_job_has_no_start(site):
    mark_repaired(site)
    site.run(Fake(queue=[sq("10", state="PENDING", start="2026-10-01T00:00:00"),
                         sq("11", state="RUNNING", nodes="cn01", start="2026-10-05T09:05:00")]))
    starts = {r["job_id"]: (r["start_time"], r["wait_time_seconds"])
              for r in site.rows("SELECT * FROM jobs")}
    assert starts == {"10": (None, None), "11": ("2026-10-05T09:05:00", 300)}


def test_job_metrics_puts_the_real_start_over_an_estimate(site):
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, wait_time_seconds) VALUES ('20', 'u1', 'job', 'basic', 'PENDING', "
                 "'2026-05-18T11:58:24', '2026-05-27T19:43:30', 805506)")

    def record(start):
        m = JobMetrics(job_id="20", job_name="job", user_name="u1", group_name=None,
                       partition="basic", state="FAILED", exit_code=1,
                       submit_time=datetime(2026, 5, 18, 11, 58, 24), start_time=start,
                       end_time=datetime(2026, 5, 18, 11, 59, 47), elapsed_seconds=43,
                       timelimit_seconds=3600, req_cpus=1, req_mem_mb=1000, req_gpus=0,
                       avg_cpu_percent=None, max_rss_mb=None, avg_rss_mb=None,
                       max_vmsize_mb=None, max_disk_read_mb=None, max_disk_write_mb=None,
                       avg_disk_read_mb=None, avg_disk_write_mb=None)
        return {"type": "job_metrics", **m.to_dict()}
    col = JobMetricsCollector({}, site.db)
    col.store([record(None)])           # no start from sacct: what is stored stays
    r = site.rows("SELECT * FROM jobs")[0]
    assert r["start_time"] == "2026-05-27T19:43:30" and r["state"] == "FAILED"
    col.store([record(datetime(2026, 5, 18, 11, 59, 4))])
    r = site.rows("SELECT * FROM jobs")[0]
    assert (r["start_time"], r["wait_time_seconds"]) == ("2026-05-18T11:59:04", 40)
    col.store([record(datetime(2026, 5, 17))])      # before submission: not a start
    assert site.rows("SELECT start_time FROM jobs")[0]["start_time"] == "2026-05-18T11:59:04"


def bad_starts(site):
    rows = [
        # id, state, submit, stored start, end, runtime
        ("30", "FAILED", "2026-05-18T11:58:24", "2026-05-27T19:43:30", "2026-05-18T11:59:47", 43),
        ("31", "FAILED", "2026-05-18T12:09:20", "2026-05-27T19:43:30", "2026-05-18T12:19:10", 38),
        ("32", "COMPLETED", "2026-06-01T10:00:00", "2026-06-01T10:30:00", "2026-06-01T15:00:00", 3600),
        ("33", "UNKNOWN", "2026-06-01T10:00:00", "2026-06-01T10:05:00", "2026-07-01T00:00:00", 60),
        ("34", "COMPLETED", "2026-06-01T10:00:00", "2026-06-01T10:01:00", "2026-06-01T10:11:00", 600),
    ]
    for i, st, sub, start, end, rt in rows:
        site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, "
                     "submit_time, start_time, end_time, runtime_seconds, wait_time_seconds) "
                     "VALUES (?, 'u1', 'job', 'basic', ?, ?, ?, ?, ?, 1)",
                     i, st, sub, start, end, rt)


def test_impossible_starts_are_repaired_once(site):
    bad_starts(site)
    fake = Fake(known={"30": sa("30", state="FAILED", submit="2026-05-18T11:58:24",
                                start="2026-05-18T11:59:04", end="2026-05-18T11:59:47",
                                elapsed="00:00:43")})
    site.run(fake)
    assert sorted(fake.looked_up) == ["30", "31", "32"]
    got = {r["job_id"]: (r["start_time"], r["wait_time_seconds"])
           for r in site.rows("SELECT * FROM jobs")}
    assert got["30"] == ("2026-05-18T11:59:04", 40)                  # from sacct
    assert got["31"] == ("2026-05-18T12:18:32", 552)                 # end - runtime
    assert got["32"] == ("2026-06-01T10:30:00", 1)                   # possible: left
    assert got["33"] == ("2026-06-01T10:05:00", 1)                   # UNKNOWN: not asked
    assert got["34"] == ("2026-06-01T10:01:00", 1)                   # consistent
    note = site.rows("SELECT value FROM config WHERE key = ?", S._STARTS_REPAIRED)[0]["value"]
    assert note == "1 from sacct, 1 from end - runtime, 1 left as they were, of 3"
    again = site.run(Fake())
    assert again.looked_up == []


def test_the_repair_waits_for_sacct(site):
    bad_starts(site)
    site.run(Fake(sacct_ok=False))
    assert site.rows("SELECT * FROM config WHERE key = ?", S._STARTS_REPAIRED) == []
    assert site.rows("SELECT start_time FROM jobs WHERE job_id = '31'")[0]["start_time"] == \
        "2026-05-27T19:43:30"
    site.run(Fake())
    assert site.rows("SELECT start_time FROM jobs WHERE job_id = '31'")[0]["start_time"] == \
        "2026-05-18T12:18:32"


def test_a_sacct_record_of_another_job_is_not_used_for_the_repair(site):
    """sacct -j answers with the newest job of a number: check its submit time."""
    bad_starts(site)
    fake = Fake(known={"30": sa("30", user="x", submit="2026-10-01T00:00:00",
                                start="2026-10-01T00:00:10", end="2026-10-01T00:10:10")})
    site.run(fake)
    r = site.rows("SELECT * FROM jobs WHERE job_id = '30'")[0]
    assert (r["start_time"], r["user_name"]) == ("2026-05-18T11:59:04", "u1")   # end - runtime


# -- new columns -------------------------------------------------------------------------

def test_sacct_fields_are_stored_and_squeue_keeps_them(site):
    mark_repaired(site)
    site.run(Fake(history=[sa("40", account="lab1", tres="cpu=6,gres/gpu:tesla_a40=1,gres/gpu=1",
                              workdir="/scratch/u1/proj/run2", nodes="cn[17-18]")]))
    site.run(Fake(queue=[sq("40", state="RUNNING", nodes="cn[17-18]",
                            start="2026-10-05T09:01:00")]))
    r = site.rows("SELECT * FROM jobs WHERE job_id = '40'")[0]
    assert (r["account"], r["alloc_gpus"], r["work_root"], r["work_tail"]) == (
        "lab1", 1, "/scratch", "proj/run2")


def test_an_old_database_gets_the_new_columns(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT NOT NULL,
        group_name TEXT, partition TEXT, node_list TEXT, job_name TEXT, submit_time DATETIME,
        start_time DATETIME, end_time DATETIME, state TEXT, exit_code INTEGER,
        exit_signal INTEGER, failure_reason INTEGER, req_cpus INTEGER, req_mem_mb INTEGER,
        req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER,
        wait_time_seconds INTEGER)""")
    c.execute("CREATE TABLE queue_state (partition TEXT, pending_jobs INTEGER, "
              "running_jobs INTEGER, total_jobs INTEGER, timestamp TEXT)")
    c.commit()
    c.close()
    col = SlurmCollector({}, str(db))
    monkeypatch.setattr(S.subprocess, "run", Fake(history=[sa("50", account="lab2")]))
    col.store(col.collect())
    c = sqlite3.connect(db)
    assert c.execute("SELECT account, work_root FROM jobs").fetchall() == [("lab2", "/home")]


# -- readers of node lists --------------------------------------------------------------------

def test_jobs_on_a_node_include_range_lists_and_nothing_else(tmp_path):
    from nomad.diag.node import get_recent_jobs
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, job_name TEXT, state TEXT, "
              "exit_code INTEGER, start_time TEXT, end_time TEXT, runtime_seconds INTEGER, "
              "failure_reason INTEGER, cluster TEXT, node_list TEXT)")
    for i, (nodes, end) in enumerate([("cn[01-02]", "2026-10-05T03"), ("cn02", "2026-10-05T02"),
                                      ("cn20", "2026-10-05T04"), ("cn[10-12]", "2026-10-05T05"),
                                      ("cn[01-04]", "2026-10-05T01")]):
        c.execute("INSERT INTO jobs (job_id, cluster, node_list, end_time, state) "
                  "VALUES (?, 'c1', ?, ?, 'COMPLETED')", (str(i), nodes, end))
    c.commit()
    c.close()
    assert [j["job_id"] for j in get_recent_jobs(str(db), "c1", "cn02")] == ["0", "1", "4"]
    assert [j["job_id"] for j in get_recent_jobs(str(db), "c1", "cn02", limit=2)] == ["0", "1"]
    assert [j["job_id"] for j in get_recent_jobs(str(db), "c1", "cn1")] == []
    assert "node_list" not in get_recent_jobs(str(db), "c1", "cn02")[0]


def test_gpu_utilization_of_a_job_on_a_range_of_nodes(tmp_path):
    db = tmp_path / "g.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE gpu_stats (node_name TEXT, data_source TEXT, real_util_pct REAL, "
              "timestamp TEXT)")
    c.executemany("INSERT INTO gpu_stats VALUES (?, 'dcgm', ?, '2026-10-05T10:00:00')",
                  [("cn17", 40.0), ("cn18", 60.0), ("cn16", 0.0)])
    col = JobMetricsCollector({}, str(db))
    util = col._compute_avg_gpu_util(c, "cn[17-18]", "2026-10-05T09:00:00",
                                     "2026-10-05T11:00:00")
    assert util == 50.0


def test_energy_gpu_tdp_reads_the_first_node_of_a_range(tmp_path):
    from nomad.energy.power import _job_gpu_tdp
    c = sqlite3.connect(tmp_path / "e.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE gpu_stats (node_name TEXT, gpu_name TEXT)")
    c.execute("INSERT INTO gpu_stats VALUES ('cn17', 'NVIDIA A40')")
    assert _job_gpu_tdp(c, "cn[17-18]") == _job_gpu_tdp(c, "cn17")
    assert _job_gpu_tdp(c, "cn[17-18]") != _job_gpu_tdp(c, None)


def test_old_dashboard_top_users_count_range_lists():
    from nomad.viz.server import _top_users_on
    rows = [{"username": "a", "node_list": "cn[01-02]"}, {"username": "a", "node_list": "cn02"},
            {"username": "b", "node_list": "cn20"}, {"username": "c", "node_list": "cn[02-03]"}]
    assert _top_users_on("cn02", rows) == [{"user": "a", "jobs": 2}, {"user": "c", "jobs": 1}]


# -- review findings ----------------------------------------------------------------------

def test_a_row_left_running_by_an_outage_does_not_take_a_new_job(site):
    """Same user and name, the stored row still RUNNING (Slurm lost its state):
    a year on, the number is another job's, and the stale row becomes UNKNOWN."""
    mark_repaired(site)
    old_job(site, user="u1", name="job", submit="2025-05-01T10:00:00", end=None, state="RUNNING")
    site.run(Fake(queue=[sq("4711", state="PENDING", submit="2026-10-05T09:00:00")]))
    by_id = {r["job_id"]: r for r in site.rows("SELECT * FROM jobs")}
    assert set(by_id) == {"4711", "4711@2025-05-01T10:00:00"}
    assert by_id["4711@2025-05-01T10:00:00"]["state"] == "UNKNOWN"
    assert by_id["4711"]["submit_time"] == "2026-10-05T09:00:00"


def test_an_active_row_within_its_time_limit_is_a_requeue(site):
    mark_repaired(site)
    old_job(site, user="u1", name="job", submit="2026-10-01T10:00:00", end=None, state="RUNNING")
    site.execute("UPDATE jobs SET req_time_seconds = 7 * 86400 WHERE job_id = '4711'")
    site.run(Fake(queue=[sq("4711", state="PENDING", submit="2026-10-05T09:00:00")]))
    rows = site.rows("SELECT * FROM jobs")
    assert [r["job_id"] for r in rows] == ["4711"]
    # A requeue gets a new submit time from Slurm: the row follows it.
    assert rows[0]["submit_time"] == "2026-10-05T09:00:00"


def test_rows_written_for_the_new_job_first_stay_with_it(site):
    """The job monitor's samples and the groups collector's accounting can
    reach the database before the job collectors see the new job."""
    mark_repaired(site)
    old_job(site)
    site.execute("CREATE TABLE IF NOT EXISTS job_io_samples (id INTEGER PRIMARY KEY, "
                 "job_id TEXT, timestamp DATETIME)")
    site.execute("INSERT INTO job_io_samples (job_id, timestamp) VALUES "
                 "('4711', '2026-03-01T11:00:00'), ('4711', '2026-10-05T09:02:00')")
    site.execute("CREATE TABLE job_accounting (job_id TEXT NOT NULL, cluster TEXT NOT NULL, "
                 "username TEXT, submit_time TEXT, PRIMARY KEY (job_id, cluster))")
    site.execute("INSERT INTO job_accounting VALUES ('4711', 'c1', 'new', '2026-10-05T09:00:00')")
    site.run(Fake(queue=[sq("4711", user="new", name="newjob", state="RUNNING", nodes="cn02",
                            submit="2026-10-05T09:00:00", start="2026-10-05T09:01:00")]))
    samples = sorted((r["job_id"], r["timestamp"])
                     for r in site.rows("SELECT * FROM job_io_samples"))
    assert samples == [("4711", "2026-10-05T09:02:00"),
                       ("4711@2026-03-01T10:00:00", "2026-03-01T11:00:00")]
    assert [r["job_id"] for r in site.rows("SELECT * FROM job_accounting")] == ["4711"]


def test_job_metrics_sees_a_reused_number_as_a_new_job(site):
    old_job(site)
    col = JobMetricsCollector({}, site.db)
    old = SimpleNamespace(job_id="4711", submit_time=datetime(2026, 3, 1, 10), elapsed_seconds=600)
    new = SimpleNamespace(job_id="4711", submit_time=datetime(2026, 10, 5, 9), elapsed_seconds=600)
    assert not col._should_include(old) and col._should_include(new)


def test_job_metrics_follows_a_requeue(site):
    """sacct gives a requeued job a new submit time: start - that, as the
    slurm collector computes it, not start - the first submit."""
    mark_repaired(site)
    site.run(Fake(history=[sa("60", submit="2026-10-05T08:00:00", start="2026-10-05T08:10:00",
                              end="2026-10-05T08:20:00", state="RUNNING")]))
    m = JobMetrics(job_id="60", job_name="job", user_name="u1", group_name=None,
                   partition="basic", state="COMPLETED", exit_code=0,
                   submit_time=datetime(2026, 10, 5, 9), start_time=datetime(2026, 10, 5, 9, 10),
                   end_time=datetime(2026, 10, 5, 9, 20), elapsed_seconds=600,
                   timelimit_seconds=3600, req_cpus=4, req_mem_mb=4096, req_gpus=0,
                   avg_cpu_percent=None, max_rss_mb=None, avg_rss_mb=None,
                   max_vmsize_mb=None, max_disk_read_mb=None, max_disk_write_mb=None,
                   avg_disk_read_mb=None, avg_disk_write_mb=None)
    JobMetricsCollector({}, site.db).store([{"type": "job_metrics", **m.to_dict()}])
    r = site.rows("SELECT * FROM jobs WHERE job_id = '60'")[0]
    assert (r["submit_time"], r["start_time"], r["wait_time_seconds"]) == (
        "2026-10-05T09:00:00", "2026-10-05T09:10:00", 600)


def test_a_job_cancelled_before_it_ran_gets_no_start(site):
    for i, (st, rt) in enumerate((("CANCELLED", 0), ("CANCELLED", 0))):
        site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, "
                     "submit_time, start_time, end_time, runtime_seconds, wait_time_seconds) "
                     "VALUES (?, 'u1', 'job', 'basic', ?, '2026-05-18T10:00:00', "
                     "'2026-05-27T19:43:30', '2026-05-19T10:00:00', ?, 1)", f"7{i}", st, rt)
    # sacct has the first, cancelled while pending: no start.
    fake = Fake(known={"70": sa("70", state="CANCELLED", submit="2026-05-18T10:00:00",
                                start="Unknown", end="2026-05-19T10:00:00", elapsed="00:00:00")})
    site.run(fake)
    got = {r["job_id"]: (r["start_time"], r["wait_time_seconds"])
           for r in site.rows("SELECT * FROM jobs")}
    assert got == {"70": (None, None), "71": (None, None)}


def test_the_repair_takes_a_few_jobs_a_run_and_carries_on(site, monkeypatch):
    monkeypatch.setattr(S, "LOOKUP_BATCH", 2)
    monkeypatch.setattr(S, "LOOKUP_MAX", 4)
    for i in range(10):
        site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, "
                     "submit_time, start_time, end_time, runtime_seconds) "
                     "VALUES (?, 'u1', 'job', 'basic', 'FAILED', '2026-05-18T10:00:00', "
                     "'2026-05-27T19:43:30', '2026-05-18T10:10:00', 60)", f"8{i}")
    first = site.run(Fake())
    assert first.looked_up == ["80", "81", "82", "83"]
    assert site.rows("SELECT * FROM config WHERE key = ?", S._STARTS_REPAIRED) == []

    calls = []

    class Failing(Fake):
        def __call__(self, argv, **kw):
            if argv[0] == "sacct" and "-j" in argv:
                calls.append(argv[argv.index("-j") + 1])
                if len(calls) == 2:
                    return SimpleNamespace(returncode=1, stdout="", stderr="slurmdbd busy")
            return super().__call__(argv, **kw)
    site.run(Failing())
    assert calls == ["84,85", "86,87"]          # the second batch failed: kept for next run
    third = site.run(Fake())
    assert third.looked_up == ["86", "87", "88", "89"]
    note = site.rows("SELECT value FROM config WHERE key = ?", S._STARTS_REPAIRED)[0]["value"]
    assert note == "0 from sacct, 10 from end - runtime, 0 left as they were, of 10"
    assert {r["start_time"] for r in site.rows("SELECT start_time FROM jobs")} == {
        "2026-05-18T10:09:00"}


def test_the_repair_leaves_assumed_utc_ends_to_settle(site):
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, end_time, runtime_seconds) VALUES ('90', 'u1', 'job', 'basic', "
                 "'COMPLETED', '2026-05-18T10:00:00', '2026-05-18T10:01:00', "
                 "'2026-05-18 08:00:00', 60)")
    site.run(Fake())
    # (settle looks it up, as an assumed outcome; the start-time repair does not)
    note = site.rows("SELECT value FROM config WHERE key = ?", S._STARTS_REPAIRED)[0]["value"]
    assert note.endswith("of 0")
    assert site.rows("SELECT start_time FROM jobs WHERE job_id = '90'")[0]["start_time"] == \
        "2026-05-18T10:01:00"


def test_the_repair_takes_sacct_for_a_requeued_job(site):
    """Stored with its first submit time; sacct has the requeue's."""
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, end_time, runtime_seconds) VALUES ('91', 'u1', 'job', 'basic', "
                 "'COMPLETED', '2026-05-18T10:00:00', '2026-05-27T19:43:30', "
                 "'2026-05-18T12:10:00', 600)")
    site.run(Fake(known={"91": sa("91", submit="2026-05-18T11:00:00", start="2026-05-18T12:00:00",
                                  end="2026-05-18T12:10:00")}))
    r = site.rows("SELECT * FROM jobs WHERE job_id = '91'")[0]
    assert (r["submit_time"], r["start_time"], r["wait_time_seconds"]) == (
        "2026-05-18T11:00:00", "2026-05-18T12:00:00", 3600)


def test_a_pipe_in_a_job_name_does_not_shift_the_fields():
    col = SlurmCollector({}, ":memory:")
    line = ("4711|u1|people|basic|a|b|c|COMPLETED|cn01|8|41G|billing=8|1-00:00:00|00:10:00|"
            "2026-10-05T09:00:00|2026-10-05T09:01:00|2026-10-05T09:11:00|0:0|lab1|cpu=8|/home/u1/x|y")
    j = col._parse_sacct_job(line)
    assert (j.job_name, j.state, j.account, j.work_tail) == ("a|b|c", "COMPLETED", "lab1", "u1/x|y")
    assert j.submit_time == datetime(2026, 10, 5, 9) and j.runtime_seconds == 600
    assert col._parse_sacct_job("4711|u1|people|basic|job|COMPLETED|n|8|1G|x|1:00|00:10:00|"
                                "garbage|garbage|garbage|0:0|a|b|c") is None


def test_gpu_counts_from_typed_gres_alone():
    assert gpu_count("cpu=2,gres/gpu:a100=1,gres/gpu:v100=1") == 2
    assert gpu_count("gres/gpu:a100=1,gres/gpu=1") == 1
    assert JobMetricsCollector({}, ":memory:")._parse_gpus("cpu=4,gres/gpumem=40960") == 0
    assert JobMetricsCollector({}, ":memory:")._parse_gpus("cpu=4,gres/gpu:a40=2") == 2


def test_a_slurm_without_the_new_fields_still_collects(site):
    mark_repaired(site)
    seen = []

    class Old(Fake):
        def __call__(self, argv, **kw):
            fmt = next((a for a in argv if a.startswith("--format=")), "")
            if argv[0] == "sacct":
                seen.append(fmt)
                if "WorkDir" in fmt:
                    return SimpleNamespace(returncode=1, stdout="",
                                           stderr='sacct: error: Invalid field requested: "WorkDir"')
                out = SA.format(**sa("95")).rsplit("|", 3)[0]
                return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
            return super().__call__(argv, **kw)
    site.run(Old())
    r = site.rows("SELECT * FROM jobs WHERE job_id = '95'")[0]
    assert r["state"] == "COMPLETED" and r["account"] is None
    assert "WorkDir" in seen[0] and "WorkDir" not in seen[-1]


def test_an_earlier_job_already_kept_aside_is_not_kept_twice(tmp_path):
    db = tmp_path / "m.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    for jid in ("9", "9@2026-10-01T00:00:00"):
        c.execute("INSERT INTO jobs (job_id, user_name, job_name, state, submit_time, end_time) "
                  "VALUES (?, 'b', 'n', 'COMPLETED', '2026-10-01T00:00:00', '2026-10-01T01:00:00')",
                  (jid,))
    c.execute("INSERT INTO job_summary (job_id, peak_memory_gb) VALUES ('9', 5.0)")
    c.execute("INSERT INTO job_metrics (job_id, timestamp) VALUES ('9', '2026-10-01T00:30:00'), "
              "('9', '2027-06-01T00:05:00')")
    assert jobkeys.place(c, "9", "2027-06-01T00:00:00", "z", "q") == "9"
    assert sorted(r[0] for r in c.execute("SELECT job_id FROM jobs")) == ["9@2026-10-01T00:00:00"]
    assert c.execute("SELECT job_id, peak_memory_gb FROM job_summary").fetchall() == [
        ("9@2026-10-01T00:00:00", 5.0)]
    assert sorted(c.execute("SELECT job_id, timestamp FROM job_metrics").fetchall()) == [
        ("9", "2027-06-01T00:05:00"), ("9@2026-10-01T00:00:00", "2026-10-01T00:30:00")]


def test_an_active_job_held_long_then_requeued_stays_one_job(site):
    """The window runs from the stored start, not only the submit time."""
    mark_repaired(site)
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, req_time_seconds) VALUES ('95', 'u1', 'job', 'basic', 'RUNNING', "
                 "'2026-08-01T00:00:00', '2026-10-05T07:00:00', 7200)")
    site.run(Fake(queue=[sq("95", state="PENDING", submit="2026-10-05T09:00:00")]))
    assert [r["job_id"] for r in site.rows("SELECT job_id FROM jobs")] == ["95"]


def test_the_repair_commits_before_each_sacct_call(site):
    """No write lock held while sacct answers."""
    for i in range(3):
        site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, "
                     "submit_time, start_time, end_time, runtime_seconds) "
                     "VALUES (?, 'u1', 'job', 'basic', 'FAILED', '2026-05-18T10:00:00', "
                     "'2026-05-27T19:43:30', '2026-05-18T10:10:00', 60)", f"8{i}")
    seen = []

    class Probe(Fake):
        def __call__(self, argv, **kw):
            if argv[0] == "sacct" and "-j" in argv:
                other = sqlite3.connect(site.db, timeout=0.1)
                other.execute("INSERT INTO config (key, value) VALUES (?, 'x')", (str(len(seen)),))
                other.commit()
                other.close()
                seen.append(argv[argv.index("-j") + 1])
            return super().__call__(argv, **kw)
    import nomad.collectors.slurm as mod
    old = mod.LOOKUP_BATCH
    mod.LOOKUP_BATCH = 1
    try:
        site.run(Probe())
    finally:
        mod.LOOKUP_BATCH = old
    assert seen == ["80", "81", "82"]


def test_a_batch_sacct_keeps_failing_on_is_repaired_without_it(site):
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, end_time, runtime_seconds) VALUES ('80', 'u1', 'job', 'basic', "
                 "'FAILED', '2026-05-18T10:00:00', '2026-05-27T19:43:30', '2026-05-18T10:10:00', 60)")

    class Failing(Fake):
        def __call__(self, argv, **kw):
            if argv[0] == "sacct" and "-j" in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="no")
            return super().__call__(argv, **kw)
    for _ in range(S._STARTS_TRIES - 1):
        site.run(Failing())
        assert site.rows("SELECT * FROM config WHERE key = ?", S._STARTS_REPAIRED) == []
    site.run(Failing())
    assert site.rows("SELECT start_time FROM jobs")[0]["start_time"] == "2026-05-18T10:09:00"
    assert site.rows("SELECT * FROM config WHERE key = ?", S._STARTS_REPAIRED) != []


def test_another_clusters_accounting_row_with_the_number_stays(site):
    mark_repaired(site)
    old_job(site)
    site.execute("CREATE TABLE job_accounting (job_id TEXT NOT NULL, cluster TEXT NOT NULL, "
                 "username TEXT, submit_time TEXT, PRIMARY KEY (job_id, cluster))")
    site.execute("INSERT INTO job_accounting VALUES ('4711', 'here', 'old', '2026-03-01T10:00:00'), "
                 "('4711', 'there', 'x', '2026-09-30T00:00:00')")
    site.run(Fake(queue=[sq("4711", user="new", name="newjob", state="RUNNING", nodes="cn02",
                            submit="2026-10-05T09:00:00", start="2026-10-05T09:01:00")]))
    rows = sorted((r["job_id"], r["cluster"]) for r in site.rows("SELECT * FROM job_accounting"))
    assert rows == [("4711", "there"), ("4711@2026-03-01T10:00:00", "here")]


def test_the_repair_waits_while_sacct_is_down(site):
    site.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                 "start_time, end_time, runtime_seconds) VALUES ('80', 'u1', 'job', 'basic', "
                 "'FAILED', '2026-05-18T10:00:00', '2026-05-27T19:43:30', '2026-05-18T10:10:00', 60)")
    for _ in range(S._STARTS_TRIES + 2):
        site.run(Fake(sacct_ok=False))
    assert site.rows("SELECT start_time FROM jobs")[0]["start_time"] == "2026-05-27T19:43:30"
    assert site.rows("SELECT * FROM config WHERE key LIKE 'repair.job_start_times%'") == []
