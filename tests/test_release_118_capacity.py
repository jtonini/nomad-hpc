"""nomad 1.18: partition settings (`nomad partitions`), why and for how many
nodes a job waited (squeue), and idle condo capacity while jobs that would
have fit waited elsewhere (`nomad usage-report condos idle`)."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import partitions as P
from nomad.condo import config as ccfg, idle

# --- partition settings ------------------------------------------------------------

SCONTROL = """\
PartitionName=basic AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL Default=YES QoS=N/A DefaultTime=NONE DisableRootJobs=NO ExclusiveUser=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime=UNLIMITED MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED Nodes=n[01-02] PriorityJobFactor=1 PriorityTier=1 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=OFF State=UP TotalCPUs=32 TotalNodes=2 SelectTypeParameters=NONE JobDefaults=(null) DefMemPerCPU=4096 MaxMemPerNode=UNLIMITED TRES=cpu=32,mem=128000M,node=2,billing=32
PartitionName=smith AllowGroups=ALL AllowAccounts=smithlab AllowQos=ALL AllocNodes=ALL Default=NO QoS=N/A DefaultTime=01:00:00 DisableRootJobs=NO ExclusiveUser=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime=14-00:00:00 MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED Nodes=c[01-02] PriorityJobFactor=1 PriorityTier=10 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=OFF State=UP TotalCPUs=32 TotalNodes=2 SelectTypeParameters=NONE JobDefaults=DefCpuPerGPU=4 DefMemPerNode=UNLIMITED MaxMemPerNode=UNLIMITED TRES=cpu=32,mem=128000M,node=2,billing=32
PartitionName=condos AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL Default=NO QoS=N/A DefaultTime=30 DisableRootJobs=NO ExclusiveUser=NO GraceTime=300 Hidden=NO MaxNodes=1 MaxTime=2-00:00:00 MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED Nodes=c[01-02] PriorityJobFactor=1 PriorityTier=1 RootOnly=NO ReqResv=NO OverSubscribe=NO OverTimeLimit=NONE PreemptMode=REQUEUE State=UP TotalCPUs=32 TotalNodes=2 SelectTypeParameters=NONE JobDefaults=(null) DefMemPerCPU=2000 MaxMemPerNode=UNLIMITED TRES=cpu=32,mem=128000M,node=2,billing=32
"""


@pytest.mark.parametrize("text,expected", [
    ("UNLIMITED", (None, True)), ("INFINITE", (None, True)), ("NONE", (None, False)),
    ("30", (30, False)), ("05:30", (6, False)), ("01:00:00", (60, False)),
    ("2-00:00:00", (2880, False)), ("1-12", (2160, False)), ("1-12:30", (2190, False)),
    ("garbage", (None, False)),
])
def test_slurm_time_limits(text, expected):
    assert P.slurm_minutes(text) == expected


def test_parse_keeps_every_setting_and_types_the_useful_ones():
    rows = {r["partition"]: r for r in P.parse(SCONTROL)}
    b, s, g = rows["basic"], rows["smith"], rows["condos"]
    assert b["max_time_unlimited"] == 1 and b["max_time_min"] is None and b["default_time_min"] is None
    assert b["def_mem_per_cpu_mb"] == 4096 and b["is_default"] == 1 and b["allow_accounts"] == "ALL"
    assert s["max_time_min"] == 14 * 1440 and s["default_time_min"] == 60 and s["priority_tier"] == 10
    assert s["allow_accounts"] == "smithlab" and s["job_defaults"] == "DefCpuPerGPU=4"
    assert g["preempt_mode"] == "REQUEUE" and g["grace_time_s"] == 300 and g["max_nodes"] == 1
    assert s["settings"]["TRES"] == "cpu=32,mem=128000M,node=2,billing=32"
    assert b["qos"] is None                      # N/A is not a value


def test_store_writes_only_changes_and_keeps_a_history():
    c = sqlite3.connect(":memory:")
    t = datetime(2026, 10, 26, 9, 0)
    rows = P.parse(SCONTROL)
    assert P.store(c, "hpc1", rows, t) == {"new": 3, "changed": 0, "gone": 0, "same": 0}
    assert P.store(c, "hpc1", rows, t + timedelta(minutes=5)) == {"new": 0, "changed": 0, "gone": 0, "same": 3}
    checked = c.execute("SELECT DISTINCT checked_at FROM partition_settings").fetchall()
    assert checked == [(t.isoformat(),)]         # unchanged: not rewritten every 5 minutes
    P.store(c, "hpc1", rows, t + timedelta(hours=2))
    assert c.execute("SELECT DISTINCT checked_at FROM partition_settings").fetchall() == [
        ((t + timedelta(hours=2)).isoformat(),)]
    longer = P.parse(SCONTROL.replace("MaxTime=UNLIMITED", "MaxTime=7-00:00:00", 1))
    assert P.store(c, "hpc1", longer[:2], t + timedelta(hours=3)) == {"new": 0, "changed": 1, "gone": 1, "same": 1}
    now = {p["partition"]: p for p in P.current(c)}
    assert set(now) == {"basic", "smith"} and now["basic"]["max_time_min"] == 7 * 1440
    hist = P.changes(c)
    assert hist[0]["kind"] in ("changed", "gone")
    basic = next(h for h in hist if h["partition"] == "basic" and h["kind"] == "changed")
    assert basic["changes"] == {"MaxTime": ["UNLIMITED", "7-00:00:00"]}
    P.store(c, "hpc1", P.parse(SCONTROL), t + timedelta(hours=4))
    assert [k for (k,) in c.execute("SELECT kind FROM partition_changes ORDER BY id")] == [
        "new", "new", "new", "changed", "gone", "changed", "back"]
    assert {p["partition"] for p in P.current(c)} == {"basic", "smith", "condos"}


def test_a_guest_partition_is_found_from_slurm_settings_not_its_name():
    c = sqlite3.connect(":memory:")
    P.store(c, "hpc1", P.parse(SCONTROL.replace("condos", "everyone")))
    cur = P.current(c)
    guest = {p["partition"]: P.is_guest(p, cur) for p in cur}
    assert guest == {"basic": False, "everyone": True, "smith": False}


def test_node_state_stores_partitions_and_survives_without_them(tmp_path, monkeypatch):
    from nomad.collectors import node_state
    db = tmp_path / "site.db"
    col = node_state.NodeStateCollector({"cluster_name": "hpc1"}, str(db))
    node_out = ("NodeName=n01 State=IDLE CPUTot=16 CPUAlloc=0 CPULoad=0.0 RealMemory=64000 AllocMem=0 "
                "FreeMem=60000 Partitions=basic\n")

    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode, self.stderr = out, rc, ""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[-2:] == ["show", "partition"]:
            assert "--all" in cmd                 # hidden and group-restricted partitions too
        if cmd[-2:] == ["show", "partition"]:
            return R(SCONTROL)
        return R(node_out)
    monkeypatch.setattr(node_state.subprocess, "run", fake_run)
    col._alert_config = {"enabled": False}
    data = col.collect()
    col.store(data)
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM node_state").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM partition_settings").fetchone()[0] == 3

    def failing(cmd, **kw):
        if cmd[-2:] == ["show", "partition"]:
            return R("", rc=1)
        return R(node_out)
    monkeypatch.setattr(node_state.subprocess, "run", failing)
    data = col.collect()
    assert [d["type"] for d in data] == ["node_state"]
    col.store(data)
    assert c.execute("SELECT COUNT(*) FROM node_state").fetchone()[0] == 2


def test_partitions_command(tmp_path):
    from nomad.cli import cli
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    P.store(c, "hpc1", P.parse(SCONTROL))
    c.commit()
    c.close()
    r = CliRunner().invoke(cli, ["partitions", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "basic [default]: 2 nodes, no time limit, memory default 4 GB/core" in r.output
    assert "2.0 GB/core" in r.output
    assert "condos [guest (preemptible)]" in r.output and "preempt REQUEUE after 300 s" in r.output
    assert "smith: 2 nodes, time limit 14 d, default 1 h" in r.output and "accounts smithlab" in r.output
    assert "hpc1 (3 partitions)" in r.output
    r = CliRunner().invoke(cli, ["partitions", "--db", str(db), "--changes"])
    assert r.exit_code == 0 and "first seen" in r.output
    # --mask (1.17.3): no partition, account or group names, for sharing.
    for args in (["partitions", "--db", str(db), "--mask"], ["partitions", "--db", str(db), "--mask", "--json"],
                 ["partitions", "--db", str(db), "--mask", "--changes"]):
        r = CliRunner().invoke(cli, args)
        assert r.exit_code == 0, r.output
        assert "smith" not in r.output and "condos" not in r.output and "basic" not in r.output, r.output
    r = CliRunner().invoke(cli, ["partitions", "--db", str(db), "--mask"])
    assert "partition 1" in r.output and "accounts 1 listed" in r.output and "preempt REQUEUE" in r.output


# --- squeue: why a job waits, and for how many nodes ----------------------------------

def test_squeue_reason_and_nodes(tmp_path):
    from nomad.collectors.slurm import SlurmCollector
    col = SlurmCollector({}, str(tmp_path / "x.db"))
    waiting = col._parse_job_line("101|u1|g|basic|my|job|PENDING||4|4000M|N/A|1-00:00:00|0:00|"
                                  "2026-10-01T10:00:00|N/A|Priority|1")
    assert waiting.job_name == "my|job" and waiting.pending_reason == "Priority" and waiting.req_nodes == 1
    running = col._parse_job_line("102|u1|g|basic|run|RUNNING|n01|4|4000M|N/A|1-00:00:00|1:00|"
                                  "2026-10-01T10:00:00|2026-10-01T10:05:00|None|2")
    assert running.pending_reason is None and running.req_nodes == 2
    old = col._parse_job_line("103|u1|g|basic|old|PENDING||4|4000M|N/A|1-00:00:00|0:00|"
                              "2026-10-01T10:00:00|N/A")
    assert old is not None and old.pending_reason is None and old.req_nodes is None


def test_the_last_waiting_reason_stays_once_the_job_runs(tmp_path):
    from nomad.collectors.slurm import SlurmCollector, _JOB_FIELDS, _JOB_UPSERT
    from nomad.db.jobkeys import ensure_job_columns
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, group_name TEXT, "
              "partition TEXT, node_list TEXT, job_name TEXT, submit_time DATETIME, start_time DATETIME, "
              "end_time DATETIME, state TEXT, exit_code INTEGER, exit_signal INTEGER, failure_reason INTEGER, "
              "req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, "
              "runtime_seconds INTEGER, wait_time_seconds INTEGER)")
    ensure_job_columns(c)
    col = SlurmCollector({}, str(tmp_path / "x.db"))
    for line in ("201|u1|g|basic|j|PENDING||4|4000M|N/A|60|0:00|2026-10-01T10:00:00|N/A|Resources|1",
                 "201|u1|g|basic|j|RUNNING|n01|4|4000M|N/A|60|1:00|2026-10-01T10:00:00|2026-10-01T11:00:00|None|1"):
        rec = col._parse_job_line(line).to_dict()
        c.execute(_JOB_UPSERT, tuple(rec[f] for f in _JOB_FIELDS))
    assert c.execute("SELECT state, pending_reason, req_nodes FROM jobs").fetchone() == ("RUNNING", "Resources", 1)


# --- idle condo capacity while jobs waited elsewhere ---------------------------------

REPORT = """
[report.policy]
guest_max_days = 10

[report.clusters.hpc1]
label = "Cluster One"
guest_partitions = ["condos"]

[report.clusters.hpc1.condos.smith]
owner = "jsmith"
"""
DAY1 = datetime(2026, 9, 1)


def _db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
        partitions TEXT, gres TEXT, cluster TEXT, source_site TEXT);
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, group_name TEXT, partition TEXT, node_list TEXT,
        job_name TEXT, submit_time TEXT, eligible_time TEXT, start_time TEXT, end_time TEXT, state TEXT,
        req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER,
        wait_time_seconds INTEGER, account TEXT, alloc_tres TEXT, alloc_gpus INTEGER,
        pending_reason TEXT, req_nodes INTEGER, source_site TEXT);
    """)
    ts = DAY1
    while ts < DAY1 + timedelta(days=2):
        for node, parts in (("c01", "smith,condos"), ("c02", "smith,condos"), ("n01", "basic")):
            busy = (node == "c02" and ts < DAY1 + timedelta(days=1)) or node == "n01"
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load, "
                      "memory_total_mb, memory_alloc_mb, partitions, gres, cluster, source_site) "
                      "VALUES (?, ?, ?, 16, ?, 0, 64000, ?, ?, NULL, 'hpc1', 'hpc1')",
                      (ts.isoformat(), node, "ALLOCATED" if busy else "IDLE", 16 if busy else 0,
                       64000 if busy else 0, parts))
        ts += timedelta(minutes=5)

    def T(h):
        return (DAY1 + timedelta(hours=h)).isoformat()
    jobs = [
        # id, partition, submit, start, end, state, cpus, mem, gpus, time_s, reason, nodes, node_list
        ("A", "basic", T(0), T(6), T(10), "COMPLETED", 4, 8000, 0, 36000, "Priority", 1, "n01"),
        ("B", "basic", T(0), T(6), T(10), "COMPLETED", 32, 8000, 0, 36000, "Resources", 1, "n01"),
        ("C", "basic", T(0), T(6), T(10), "COMPLETED", 4, 8000, 0, 36000, "Resources", 2, "n01"),
        ("D", "basic", T(0), T(6), T(30), "COMPLETED", 4, 8000, 0, 20 * 86400, "Resources", 1, "n01"),
        ("E", "basic", T(0), T(6), T(10), "COMPLETED", 4, 8000, 0, 36000, "QOSMaxJobsPerUserLimit", 1, "n01"),
        ("F", "condos", T(0), T(6), T(10), "COMPLETED", 4, 8000, 0, 36000, "Resources", 1, "c01"),
        ("G", "basic", T(24), T(25), T(26), "COMPLETED", 2, 1000, 0, 3600, None, None, "n01"),
        ("H", "basic", T(0), T(6), T(10), "COMPLETED", 2, 1000, 1, 36000, "Resources", 1, "n01"),
    ]
    for jid, part, sub, st, en, state, cpus, mem, gpus, tl, reason, nodes, nl in jobs:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, pending_reason, "
                  "req_nodes, source_site) VALUES (?, 'u1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3600, ?, ?, 'hpc1')",
                  (jid, part, nl, sub, st, en, state, cpus, mem, gpus, tl, reason, nodes))
    c.commit()
    return c


@pytest.fixture
def idle_data(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT)
    settings = ccfg.load(path)
    conn = _db(tmp_path / "combined.db")
    try:
        return idle.build(conn, settings, "2026-09", now=datetime(2026, 10, 1, 7, 0))
    finally:
        conn.close()


def test_idle_while_waiting(idle_data):
    cl = idle_data["clusters"][0]
    r = cl["condos"][0]
    assert r["condo"] == "smith" and r["nodes"] == 2 and r["cores"] == 32
    # A (4 cores, 6 h) and G (2 cores, 1 h) could have run on c01.
    assert r["usable_core_hours"] == pytest.approx(4 * 6 + 2 * 1, abs=0.5)
    assert r["hours_with_taker"] == pytest.approx(7, abs=0.2)
    assert r["earlier_jobs"] == 2 and cl["earlier_jobs_any"] == 2
    assert r["earlier_hours_median"] == pytest.approx(6, abs=0.1)
    assert r["reason_unknown"] == 1                  # G: waited before reasons were recorded
    assert r["left_out"] == {"several_nodes": 1, "too_long": 1, "no_time_limit": 0, "own_limits": 1,
                             "too_big": 1}
    assert r["waiting_jobs"] == 3                    # A, G and H (H wants a GPU these nodes lack)


def test_idle_hours_and_stretches(idle_data):
    r = idle_data["clusters"][0]["condos"][0]
    # c01 idle 48 h, c02 idle the second day: 16 cores each.
    assert r["idle_core_hours"] == pytest.approx(16 * 72, rel=0.01)
    assert r["idle_pct"] == pytest.approx(75, abs=1)
    assert r["idle_node_hours"] == pytest.approx(72, abs=0.5)
    assert r["stretch_share_pct"]["24"] == 100.0 and r["stretch_median_hours"] == pytest.approx(48, abs=0.5)


def test_waiting_for_the_cluster_or_for_its_own_limits():
    from nomad.db.jobkeys import waiting_for_cluster as w
    assert w("Resources") and w("Priority") and w("PartitionDown")
    assert w("Nodes required for job are DOWN, DRAINED or reserved for jobs in higher priority partitions")
    # ReqNodeNotAvail often means the job named its nodes (-w): a condo node won't do.
    assert w("ReqNodeNotAvail, UnavailableNodes:n01") is False
    assert w("QOSMaxJobsPerUserLimit") is False and w("Dependency") is False
    assert w(None) is None and w("None") is None


def test_condos_idle_command(tmp_path):
    from nomad.cli import cli
    rt = tmp_path / "report.toml"
    rt.write_text(REPORT)
    _db(tmp_path / "combined.db").close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("")
    args = ["-c", str(cfg), "usage-report", "condos", "idle", "--db", str(tmp_path / "combined.db"),
            "--config", str(rt), "--month", "2026-09"]
    r = CliRunner().invoke(cli, args + ["--mask"])
    assert r.exit_code == 0, r.output
    assert "Cluster One: 2 jobs waiting elsewhere could have started earlier on a condo" in r.output
    assert "condo 1: 2 nodes" in r.output and "smith" not in r.output
    assert "left out: 1 several nodes, 1 too long, 1 own limits, 1 too big" in r.output
    r = CliRunner().invoke(cli, args + ["--json"])
    assert r.exit_code == 0 and json.loads(r.output)["clusters"][0]["condos"][0]["earlier_jobs"] == 2


# --- the review's cases ------------------------------------------------------------

def _db2(path):
    """c01 idle throughout, c02 draining with 8 of 16 cores free; jobs one at a time."""
    c = _db(path)
    c.execute("DELETE FROM jobs")
    c.execute("UPDATE node_state SET state = 'MIXED+DRAIN', cpus_alloc = 8, memory_alloc_mb = 0 "
              "WHERE node_name = 'c02'")
    c.execute("ALTER TABLE jobs ADD COLUMN ready_since TEXT")

    def T(h, m=0):
        return (DAY1 + timedelta(hours=h, minutes=m)).isoformat()
    jobs = [
        # id, submit, start, end, state, cpus, mem, time_s, reason, ready_since
        ("K", T(0), None, T(1), "CANCELLED", 4, 1000, 3600, "Priority", T(0)),     # given up after 1 h
        ("L", T(1), T(2), T(3), "COMPLETED", 2, 1000, None, None, None),           # no limit: ran 1 h
        ("M", T(40), None, None, "PENDING", 2, 1000, None, "Priority", T(40)),     # no limit, never ran
        ("N", T(4), T(9), T(10), "COMPLETED", 2, 1000, 3600, "Priority", T(8)),    # held until 8 h
        ("O", T(11), T(13), T(14), "COMPLETED", 2, 1000, 3600, "Dependency", None),
        ("P", T(15), None, None, "PENDING", 8, 16000, 3600, "Resources", T(15)),  # 8 x 16 GB > a node
        ("Q", T(20), T(20, 5), T(21), "COMPLETED", 2, 1000, 3600, "Priority", T(20)),  # 5 min: scheduling
    ]
    for jid, sub, st, en, state, cpus, mem, tl, reason, ready in jobs:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, pending_reason, "
                  "req_nodes, ready_since, source_site) VALUES (?, 'u1', 'basic', ?, ?, ?, ?, ?, ?, ?, 0, ?, "
                  "3600, ?, 1, ?, 'hpc1')",
                  (jid, "n01" if st else None, sub, st, en, state, cpus, mem, tl, reason, ready))
    c.commit()
    return c


@pytest.fixture
def review(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT)
    conn = _db2(tmp_path / "combined.db")
    try:
        return idle.build(conn, ccfg.load(path), "2026-09", now=datetime(2026, 10, 1, 7, 0))
    finally:
        conn.close()


def test_review_cases(review):
    r = review["clusters"][0]["condos"][0]
    # Earlier: K by 1 h (until it was given up, not the month's end), L by 1 h,
    # N by 1 h (from when it began waiting for the cluster), M and P still
    # waiting at the month's end... P never fits (its memory read per core).
    assert r["left_out"]["no_time_limit"] == 1            # M
    assert r["left_out"]["own_limits"] == 1               # O
    assert r["waiting_jobs"] == 5                         # K, L, N, P and Q (5 min: counted, not earlier)
    assert r["earlier_jobs"] == 3 and r["reason_unknown"] == 1     # L: no reason recorded
    assert r["earlier_hours_total"] == pytest.approx(3, abs=0.2)
    # c02 drains: its free cores are idle (as the monthly report counts them)
    # but not open, and take no job.
    assert r["idle_core_hours"] == pytest.approx(16 * 48 + 8 * 48, rel=0.01)
    assert r["open_core_hours"] == pytest.approx(16 * 48, rel=0.01)


def test_an_unknown_condo_is_an_error(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT)
    conn = _db(tmp_path / "combined.db")
    with pytest.raises(ValueError, match="no condo nosuch"):
        idle.build(conn, ccfg.load(path), "2026-09", only=["nosuch"], now=datetime(2026, 10, 1))


def test_partition_store_failure_keeps_the_node_rows(tmp_path, monkeypatch):
    from nomad.collectors import node_state
    db = tmp_path / "site.db"
    col = node_state.NodeStateCollector({"cluster_name": "hpc1"}, str(db))
    record = {"type": "node_state", "cluster": "hpc1", "timestamp": "2026-10-26T09:00:00", "node_name": "n01",
              "state": "IDLE", "cpus_total": 16, "cpus_alloc": 0, "cpu_load": 0.0, "memory_total_mb": 64000,
              "memory_alloc_mb": 0, "memory_free_mb": 60000, "cpu_alloc_percent": 0.0,
              "memory_alloc_percent": 0.0, "partitions": "basic", "reason": None, "features": None,
              "gres": None, "is_healthy": True}
    parts = {"type": "partition_settings", "cluster": "hpc1", "timestamp": "2026-10-26T09:00:00",
             "rows": P.parse(SCONTROL)}

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(P, "store", broken)
    col.store([record, parts])
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM node_state").fetchone()[0] == 1


def test_an_older_table_gains_every_column():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE partition_settings (id INTEGER PRIMARY KEY, cluster TEXT NOT NULL, "
              "partition TEXT NOT NULL, UNIQUE(cluster, partition))")
    assert P.store(c, "hpc1", P.parse(SCONTROL))["new"] == 3
    assert {"settings", "fingerprint", "checked_at", "gone_at", "max_time_min"} <= P._has(c, "partition_settings")


def test_a_renamed_cluster_leaves_no_partition_listed_under_the_old_name():
    c = sqlite3.connect(":memory:")
    P.store(c, "old", P.parse(SCONTROL))
    P.store(c, "new", P.parse(SCONTROL))
    assert {(p["cluster"], p["partition"]) for p in P.current(c)} == {
        ("new", "basic"), ("new", "smith"), ("new", "condos")}


def test_partitions_command_history_and_unknown_site(tmp_path):
    from nomad.cli import cli
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    P.store(c, "hpc1", P.parse(SCONTROL))
    P.store(c, "hpc1", [], datetime.now())             # every partition gone
    c.commit()
    c.close()
    r = CliRunner().invoke(cli, ["partitions", "--db", str(db), "--changes"])
    assert r.exit_code == 0 and "no longer listed" in r.output and "first seen" in r.output
    r = CliRunner().invoke(cli, ["partitions", "--db", str(db), "--site", "elsewhere"])
    assert r.exit_code != 0 and "--site is for a hub's combined database" in r.output
    hub = tmp_path / "combined.db"
    c = sqlite3.connect(hub)
    P.store(c, "hpc1", P.parse(SCONTROL))
    c.execute("ALTER TABLE partition_settings ADD COLUMN source_site TEXT")
    c.execute("UPDATE partition_settings SET source_site = 'hpc1'")
    c.commit()
    c.close()
    r = CliRunner().invoke(cli, ["partitions", "--db", str(hub), "--site", "elsewhere"])
    assert r.exit_code != 0 and "no partitions for site elsewhere (sites: hpc1)" in r.output
    r = CliRunner().invoke(cli, ["partitions", "--db", str(hub), "--site", "hpc1"])
    assert r.exit_code == 0 and "condos [guest (preemptible)]" in r.output and "2.0 GB/core" in r.output


def test_requested_nodes_stay_as_asked(tmp_path):
    from nomad.collectors.slurm import SlurmCollector, _JOB_FIELDS, _JOB_UPSERT
    from nomad.db.jobkeys import ensure_job_columns
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, group_name TEXT, "
              "partition TEXT, node_list TEXT, job_name TEXT, submit_time DATETIME, start_time DATETIME, "
              "end_time DATETIME, state TEXT, exit_code INTEGER, exit_signal INTEGER, failure_reason INTEGER, "
              "req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, "
              "runtime_seconds INTEGER, wait_time_seconds INTEGER)")
    ensure_job_columns(c)
    col = SlurmCollector({}, str(tmp_path / "x.db"))
    for line in ("301|u1|g|basic|j|PENDING||4|4000M|N/A|60|0:00|2026-10-01T10:00:00|N/A|QOSMaxJobsPerUserLimit|1",
                 "301|u1|g|basic|j|PENDING||4|4000M|N/A|60|0:00|2026-10-01T10:00:00|N/A|Priority|1",
                 "301|u1|g|basic|j|PENDING||4|4000M|N/A|60|0:00|2026-10-01T10:00:00|N/A|Resources|1",
                 "301|u1|g|basic|j|RUNNING|n[01-02]|4|4000M|N/A|60|1:00|2026-10-01T10:00:00|"
                 "2026-10-01T11:00:00|None|2"):
        rec = col._parse_job_line(line).to_dict()
        c.execute(_JOB_UPSERT, tuple(rec[f] for f in _JOB_FIELDS))
        if "Priority" in line:
            first = c.execute("SELECT ready_since FROM jobs").fetchone()[0]
    reason, nodes, ready = c.execute("SELECT pending_reason, req_nodes, ready_since FROM jobs").fetchone()
    assert reason == "Resources" and nodes == 1 and ready == first and ready is not None


# --- the second review's cases -----------------------------------------------------

def _one_node(path, jobs):
    """c01 with 12 free cores of 16 throughout September's first day."""
    c = _db(path)
    c.execute("DELETE FROM jobs")
    c.execute("DELETE FROM node_state WHERE node_name <> 'c01' AND node_name <> 'n01'")
    c.execute("UPDATE node_state SET cpus_alloc = 4, state = 'MIXED' WHERE node_name = 'c01'")
    c.execute("ALTER TABLE jobs ADD COLUMN ready_since TEXT")
    for row in jobs:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, pending_reason, "
                  "req_nodes, ready_since, source_site) VALUES (?, 'u1', 'basic', 'n01', ?, ?, ?, 'COMPLETED', ?, "
                  "100, 0, 3600, 3600, 'Priority', 1, ?, 'hpc1')", row)
    c.commit()
    return c


def _run(tmp_path, jobs, now=datetime(2026, 10, 1, 7, 0)):
    path = tmp_path / "report.toml"
    path.write_text(REPORT.replace('owner = "jsmith"', 'owner = "jsmith"\nnodes = "c01"'))
    conn = _one_node(tmp_path / "combined.db", jobs)
    try:
        return idle.build(conn, ccfg.load(path), "2026-09", now=now)["clusters"][0]
    finally:
        conn.close()


def T(h):
    return (DAY1 + timedelta(hours=h)).isoformat()


def test_the_longest_waiting_go_first_whatever_their_size(tmp_path):
    # 12 free cores: A1 (4) waits from 0 h, B1 (8) from 1 h, A2 (4) from 2 h, all
    # until 10 h. Longest waiting first: A1 and B1, 4*10 + 8*9 = 112 core-hours.
    cl = _run(tmp_path, [("A1", T(0), T(10), T(11), 4, T(0)), ("B1", T(1), T(10), T(11), 8, T(1)),
                         ("A2", T(2), T(10), T(11), 4, T(2))])
    r = cl["condos"][0]
    assert r["usable_core_hours"] == pytest.approx(112, abs=1)
    assert r["earlier_jobs"] == 2 and cl["earlier_jobs_any"] == 2


def test_a_job_that_started_after_the_month_counts(tmp_path):
    # Waited from 30 Sep 22:00, started 2 Oct: its September hours count.
    late = (datetime(2026, 9, 30, 22, 0)).isoformat()
    c = _one_node(tmp_path / "x.db", [])
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load, "
              "memory_total_mb, memory_alloc_mb, partitions, gres, cluster, source_site) "
              "VALUES (?, 'c01', 'IDLE', 16, 0, 0, 64000, 0, 'smith,condos', NULL, 'hpc1', 'hpc1')",
              ((datetime(2026, 9, 30, 23, 0)).isoformat(),))
    c.commit()
    c.close()
    path = tmp_path / "report.toml"
    path.write_text(REPORT)
    conn = sqlite3.connect(tmp_path / "x.db")
    conn.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                 "state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, pending_reason, "
                 "req_nodes, ready_since, source_site) VALUES ('Z', 'u1', 'basic', 'n01', ?, ?, ?, 'COMPLETED', "
                 "2, 100, 0, 3600, 3600, 'Priority', 1, ?, 'hpc1')",
                 (late, datetime(2026, 10, 2).isoformat(), datetime(2026, 10, 2, 1).isoformat(), late))
    conn.commit()
    r = idle.build(conn, ccfg.load(path), "2026-09", now=datetime(2026, 10, 5))["clusters"][0]["condos"][0]
    assert r["earlier_jobs"] == 1 and r["usable_core_hours"] > 0


def test_a_running_job_with_no_limit_is_left_out(tmp_path):
    cl = _run(tmp_path, [])
    path = tmp_path / "report.toml"
    conn = sqlite3.connect(tmp_path / "combined.db")
    conn.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                 "state, req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, pending_reason, "
                 "req_nodes, ready_since, source_site) VALUES ('R', 'u1', 'basic', 'n01', ?, ?, NULL, 'RUNNING', "
                 "2, 100, 0, NULL, 86400, 'Priority', 1, ?, 'hpc1')", (T(0), T(2), T(0)))
    conn.commit()
    r = idle.build(conn, ccfg.load(path), "2026-09", now=datetime(2026, 9, 2))["clusters"][0]["condos"][0]
    assert cl["condos"][0]["left_out"]["no_time_limit"] == 0
    assert r["left_out"]["no_time_limit"] == 1 and r["earlier_jobs"] == 0


def test_a_hold_breaks_the_wait_for_the_cluster(tmp_path):
    from nomad.collectors.slurm import SlurmCollector, _JOB_FIELDS, _JOB_UPSERT
    from nomad.db.jobkeys import ensure_job_columns
    c = sqlite3.connect(tmp_path / "x.db")
    c.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT NOT NULL, group_name TEXT, "
              "partition TEXT, node_list TEXT, job_name TEXT, submit_time DATETIME, start_time DATETIME, "
              "end_time DATETIME, state TEXT, exit_code INTEGER, exit_signal INTEGER, failure_reason INTEGER, "
              "req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER, "
              "runtime_seconds INTEGER, wait_time_seconds INTEGER)")
    ensure_job_columns(c)
    col = SlurmCollector({}, str(tmp_path / "x.db"))
    head = "401|u1|g|basic|j|PENDING||4|4000M|N/A|60|0:00|2026-10-01T10:00:00|N/A|"

    def read(line):
        rec = col._parse_job_line(line).to_dict()
        c.execute(_JOB_UPSERT, tuple(rec[f] for f in _JOB_FIELDS))
        return c.execute("SELECT ready_since FROM jobs").fetchone()[0]
    assert read(head + "Priority|1") is not None
    assert read(head + "JobHeldUser|1") is None          # held: the wait for the cluster broke
    again = read(head + "Resources|1")
    assert again is not None
    assert read("401|u1|g|basic|j|RUNNING|n01|4|4000M|N/A|60|1:00|2026-10-01T10:00:00|"
                "2026-10-01T11:00:00|None|1") == again
    # A requeue (a later submit time) starts it over.
    assert read("401|u1|g|basic|j|RUNNING|n01|4|4000M|N/A|60|1:00|2099-01-01T10:00:00|"
                "2099-01-01T11:00:00|None|1") is None
