"""nomad 1.14.1: a job's memory request is what Slurm allocated it, per node.

squeue's %m and sacct's ReqMem give the figure per CPU for a job submitted
with --mem-per-cpu, with nothing to say so: such a job was stored as asking
for a CPU's worth, and peaked "above" it.
"""
import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import slurm as S
from nomad.collectors.job_metrics import JobMetricsCollector
from nomad.collectors.slurm import SlurmCollector
from nomad.db.jobkeys import ensure_job_columns, fix_memory_requests, mem_per_node_mb
from nomad.db.migrations import MIGRATIONS, MigrationManager, ensure_database


def test_memory_per_node_from_the_allocation():
    assert mem_per_node_mb("billing=8,cpu=8,mem=32G,node=1") == 32 * 1024
    assert mem_per_node_mb("cpu=16,mem=64000M,node=2") == 32000          # per node
    assert mem_per_node_mb("cpu=4,mem=1.5T") == int(1.5 * 1024 * 1024)
    assert mem_per_node_mb("cpu=4,gres/gpumem=40G,node=1") is None     # not the job's memory
    assert mem_per_node_mb("billing=8,cpu=8,node=1") is None            # Slurm allocates no memory
    assert mem_per_node_mb(None) is None and mem_per_node_mb("") is None


def test_reqmem_marked_per_cpu_counts_every_cpu():
    col = SlurmCollector({}, ":memory:")
    assert col._parse_req_mem("4000Mc", 8) == 32000
    assert col._parse_req_mem("16Gn", 8) == 16 * 1024
    assert col._parse_req_mem("16G", 8) == 16 * 1024
    assert col._parse_memory("2T") == 2 * 1024 * 1024


def fields(job_id, *, req_mem="32G", cpus=8, alloc="billing=8,cpu=8,mem=32G,node=1",
           req_tres="billing=8,cpu=8,mem=32G,node=1"):
    # sacct as Slurm 21.08 and later writes it for --mem-per-cpu=4G on 8 CPUs:
    # ReqMem and ReqTRES give the job's total.
    return {"JobID": job_id, "User": "u1", "Group": "g", "Partition": "basic", "JobName": "run",
            "State": "COMPLETED", "NodeList": "cn01", "AllocCPUS": str(cpus), "ReqMem": req_mem,
            "ReqTRES": req_tres, "Timelimit": "02:00:00", "Elapsed": "01:00:00",
            "Submit": "2026-10-05T09:00:00", "Start": "2026-10-05T09:00:00",
            "End": "2026-10-05T10:00:00", "ExitCode": "0:0", "AllocTRES": alloc}


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO config (key, value) VALUES (?, 'done')", (S._STARTS_REPAIRED,))
    monkeypatch.setattr(S.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    col = SlurmCollector({}, str(db))

    def store(*jobs):
        col.store([{"type": "job", **j.to_dict()} for j in jobs])

    def req(job_id):
        return sqlite3.connect(db).execute(
            "SELECT req_mem_mb FROM jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
    return SimpleNamespace(db=db, col=col, store=store, req=req)


def squeued(job_id):
    """A job as squeue stored it while it waited: %m, 4G, is per CPU."""
    return SlurmCollector({}, ":memory:").job_from_fields(
        {**fields(job_id), "State": "PENDING", "End": "Unknown", "AllocTRES": "",
         "ReqTRES": "", "ReqMem": "4G"})


def test_sacct_corrects_the_per_cpu_figure_squeue_stored(site):
    site.store(squeued("10"))
    assert site.req("10") == 4096
    # sacct, once it ended: allocated 32G on one node.
    site.store(site.col.job_from_fields(fields("10")))
    assert site.req("10") == 32 * 1024
    # A later record without an allocation (squeue's) leaves it.
    site.store(squeued("10"))
    assert site.req("10") == 32 * 1024


def test_at_a_site_that_allocates_no_memory_the_request_comes_from_reqtres(site):
    # CR_Core: AllocTRES has no mem=; what was asked is in ReqTRES.
    site.store(squeued("11"))
    site.store(site.col.job_from_fields(fields("11", alloc="billing=8,cpu=8,node=1")))
    assert site.req("11") == 32 * 1024
    # On several nodes, per node.
    site.store(site.col.job_from_fields(
        fields("12", alloc="billing=16,cpu=16,node=2", req_tres="cpu=16,mem=64G,node=2")))
    assert site.req("12") == 32 * 1024
    # gres/gpumem is not the job's memory: ReqMem then.
    site.store(site.col.job_from_fields(
        fields("13", req_mem="6G", alloc="billing=8,cpu=8,gres/gpu=1,gres/gpumem=40G,node=1",
               req_tres="cpu=8,gres/gpumem=40G,node=1")))
    assert site.req("13") == 6 * 1024


def test_the_migration_corrects_stored_jobs(tmp_path):
    db = tmp_path / "old.db"
    # A database at 1.14.0 (migration 21), with jobs stored the old way.
    with MigrationManager(db) as mgr:
        mgr.get_current_version()                       # makes schema_migrations
        for version, description, sql in MIGRATIONS:
            if version <= 21:
                mgr.apply_migration(version, description, sql)
    with sqlite3.connect(db) as c:
        ensure_job_columns(c)
        c.executemany("INSERT INTO jobs (job_id, user_name, state, req_mem_mb, req_cpus, "
                      "alloc_tres) VALUES (?, 'u', 'COMPLETED', ?, ?, ?)",
                      [("1", 4096, 8, "cpu=8,mem=32G,node=1"),       # per-CPU figure stored
                       ("2", 32000, 16, "cpu=16,mem=64000M,node=2"),  # per node already
                       ("3", 8192, 8, "billing=8,cpu=8,node=1"),     # no memory allocated
                       ("4", 2048, 2, None),
                       ("5", "4G", 8, "cpu=8,mem=16G,node=1")])        # text where a number goes
    ensure_database(db)
    got = dict(sqlite3.connect(db).execute("SELECT job_id, req_mem_mb FROM jobs"))
    assert got == {"1": 32 * 1024, "2": 32000, "3": 8192, "4": 2048, "5": 16 * 1024}
    assert fix_memory_requests(sqlite3.connect(db)) == 0                 # nothing left to do


def test_the_migration_passes_a_database_without_allocations(tmp_path):
    db = tmp_path / "ws.db"
    ensure_database(db)                                 # no alloc_tres column: nothing to do
    assert sqlite3.connect(db).execute("SELECT MAX(version) FROM schema_migrations"
                                       ).fetchone()[0] >= 22


def test_job_metrics_takes_the_allocation(tmp_path):
    col = JobMetricsCollector({}, str(tmp_path / "x.db"))
    parts = ["5", "run", "u1", "g", "basic", "COMPLETED", "0:0", "2026-10-05T09:00:00",
             "2026-10-05T09:00:00", "2026-10-05T10:00:00", "01:00:00", "02:00:00", "8", "4G",
             "cpu=8", "", "", "", "", "", "", "", "", "cn01", "", "", "cpu=8,mem=32G,node=1",
             "sbatch --mem-per-cpu=4G run.sh"]
    job = col._parse_sacct_line("|".join(parts))
    assert job.req_mem_mb == 32 * 1024 and job.alloc_tres == "cpu=8,mem=32G,node=1"
    assert job.cpu_request is None or isinstance(job.cpu_request, str)
    parts[26] = "cpu=8,node=1"                                        # CR_Core
    parts[14] = "cpu=8,mem=24G,node=1"
    assert col._parse_sacct_line("|".join(parts)).req_mem_mb == 24 * 1024
    parts[26], parts[14], parts[13] = "", "cpu=8", "4000Mc"           # an older Slurm
    assert col._parse_sacct_line("|".join(parts)).req_mem_mb == 32000


def test_the_import_corrects_a_stored_request(site, tmp_path):
    from nomad.collectors import sacct_import as SI
    with sqlite3.connect(site.db) as c:
        ensure_job_columns(c)
        c.execute("INSERT INTO jobs (job_id, user_name, state, submit_time, req_mem_mb, req_cpus) "
                  "VALUES ('20', 'u1', 'COMPLETED', '2026-10-05T09:00:00', 4096, 8)")
        rec = site.col.job_from_fields(fields("20")).to_dict()
        c.execute(SI._FILL, tuple(rec[f] for f in SI._JOB_FIELDS))
    assert site.req("20") == 32 * 1024


def test_the_report_says_when_a_site_allocates_no_memory():
    from nomad.facts.jobs import summarize
    from nomad.impact import render
    rows = [{"state": "COMPLETED", "req_cpus": 8, "req_mem_mb": 8192, "runtime": 3600,
             "avg_cpu": 50, "peak_gb": 12, "site": "c1", "alloc_tres": "billing=8,cpu=8,node=1"}]
    s = summarize(rows)
    assert s["memory"]["used_pct"] == 150 and s["memory"]["allocated"] is False
    rows[0]["alloc_tres"] = "cpu=8,mem=8G,node=1"
    assert summarize(rows)["memory"]["allocated"] is True
    rows[0]["alloc_tres"] = None
    assert summarize(rows)["memory"]["allocated"] is None
    rows[0]["alloc_tres"] = "billing=8,cpu=8,gres/gpumem=40G,node=1"   # GPU memory, not the job's
    assert summarize(rows)["memory"]["allocated"] is False
    # Jobs from two sites together: decided site by site, and named.
    other = {**rows[0], "site": "c2", "alloc_tres": "cpu=8,mem=8G,node=1"}
    m = summarize([rows[0], other, dict(other), dict(other)])["memory"]
    assert m["allocated"] is False and m["not_allocated_at"] == ["c1"]
    from nomad.facts.cli import _not_allocated
    assert "allocate memory at c1, so jobs there" in _not_allocated(m)
    month = {"month": "2026-10", "phase": "after", "finished": 1, "people": 1, "new_people": 0,
             "measured_pct": 100, "memory": {"used_pct": 150, "allocated": False},
             "core_hours": {}, "events": []}
    rep = {"site": "c1", "set": True, "launch": date(2026, 10, 19).isoformat(),
           "baseline_from": "2025-10-01", "data_from": "2025-10-01", "months": [month],
           "periods": {}}
    text = "\n".join(render(rep))
    assert "Slurm there doesn't allocate memory to jobs" in text
    month["memory"]["allocated"] = True
    assert "doesn't allocate memory" not in "\n".join(render(rep))
