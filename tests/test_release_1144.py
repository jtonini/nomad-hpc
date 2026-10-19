"""nomad 1.14.4: a job's GPUs as squeue showed them stay when sacct shows none.

Slurm records GPUs only where AccountingStorageTRES lists gres/gpu. Before
that, sacct gives a GPU job neither a GPU request nor an allocation, and what
nomad read from squeue while the job waited or ran is the only record of its
GPUs. The upserts kept the first GPU count stored (so a reading with GPUs
never corrected it), and the job's summary took sacct's none: no GPU flag,
so no DCGM utilization.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import slurm as S
from nomad.collectors.job_metrics import JobMetricsCollector
from nomad.collectors.slurm import SlurmCollector
from nomad.db.jobkeys import ensure_job_columns, fix_gpu_summaries, gpu_util_during, with_gpu_flag
from nomad.db.migrations import MIGRATIONS, MigrationManager, ensure_database

CPU_TRES = "billing=4,cpu=4,mem=16G,node=1"
GPU_TRES = "billing=4,cpu=4,gres/gpu=2,mem=16G,node=1"


def fields(job_id, *, state="COMPLETED", req_tres=CPU_TRES, alloc=CPU_TRES, end="2026-06-05T12:00:00"):
    return {"JobID": job_id, "User": "u1", "Group": "g", "Partition": "gpu", "JobName": "train",
            "State": state, "NodeList": "gpu01", "AllocCPUS": "4", "ReqMem": "16G",
            "ReqTRES": req_tres, "Timelimit": "04:00:00", "Elapsed": "02:00:00",
            "Submit": "2026-06-05T09:00:00", "Start": "2026-06-05T10:00:00",
            "End": end, "ExitCode": "0:0", "AllocTRES": alloc}


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO config (key, value) VALUES (?, 'done')", (S._STARTS_REPAIRED,))
        for col, kind in (("node_name", "TEXT"), ("data_source", "TEXT"), ("real_util_pct", "REAL")):
            try:
                c.execute(f"ALTER TABLE gpu_stats ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError:
                pass
    monkeypatch.setattr(S.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    col = SlurmCollector({}, str(db))

    def store(*jobs):
        col.store([{"type": "job", **j.to_dict()} for j in jobs])

    def one(sql, *args):
        return sqlite3.connect(db).execute(sql, args).fetchone()
    return SimpleNamespace(db=db, col=col, store=store, one=one)


def seen_by_squeue(col, job_id, gpus=2):
    """While it ran: squeue's gres shows the GPUs; there is no allocation yet."""
    job = col.job_from_fields({**fields(job_id, state="RUNNING", req_tres="", alloc=""), "End": "Unknown"})
    job.req_gpus = gpus
    return job


def test_a_reading_without_gpus_keeps_the_stored_count(site):
    site.store(seen_by_squeue(site.col, "10"))
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '10'")[0] == 2
    site.store(site.col.job_from_fields(fields("10")))            # sacct, before GPU accounting
    assert site.one("SELECT req_gpus, state FROM jobs WHERE job_id = '10'") == (2, "COMPLETED")


def test_a_reading_with_gpus_replaces_the_stored_count(site):
    site.store(seen_by_squeue(site.col, "11", gpus=1))             # squeue's per-node figure
    site.store(site.col.job_from_fields(fields("11", req_tres=GPU_TRES, alloc=GPU_TRES)))
    assert site.one("SELECT req_gpus, alloc_gpus FROM jobs WHERE job_id = '11'") == (2, 2)


def test_a_cpu_job_stays_a_cpu_job(site):
    site.store(site.col.job_from_fields(fields("12")))
    site.store(site.col.job_from_fields(fields("12")))
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '12'")[0] == 0


def sacct_line(job_id, req_tres=CPU_TRES, alloc=CPU_TRES):
    parts = [job_id, "train", "u1", "g", "gpu", "COMPLETED", "0:0", "2026-06-05T09:00:00",
             "2026-06-05T10:00:00", "2026-06-05T12:00:00", "02:00:00", "04:00:00", "4", "16G",
             req_tres, "01:00:00", "2G", "1G", "4G", "", "", "", "", "gpu01", "06:00:00", "1",
             alloc, "sbatch train.sh"]
    return "|".join(parts)


def measured(col, line):
    job = col._parse_sacct_line(line)
    col._compute_derived_metrics(job)
    col._compute_feature_vector(job)
    col._compute_health_score(job)
    return {"type": "job_metrics", **job.to_dict()}


def dcgm(db, node, times_utils, site_name=None):
    with sqlite3.connect(db) as c:
        for ts, util in times_utils:
            if site_name is None:
                c.execute("INSERT INTO gpu_stats (timestamp, gpu_index, node_name, data_source, real_util_pct) "
                          "VALUES (?, 0, ?, 'dcgm', ?)", (ts, node, util))
            else:
                c.execute("INSERT INTO gpu_stats (timestamp, gpu_index, node_name, data_source, real_util_pct, "
                          "source_site) VALUES (?, 0, ?, 'dcgm', ?, ?)", (ts, node, util, site_name))


def test_the_summary_of_a_job_known_only_from_squeue_is_a_gpu_job(site):
    site.store(seen_by_squeue(site.col, "20"))
    dcgm(site.db, "gpu01", [("2026-06-05T10:30:00", 60.0), ("2026-06-05T11:30:00", 80.0),
                            ("2026-06-05T13:00:00", 0.0)])                # after it ended: not its
    col = JobMetricsCollector({}, str(site.db))
    rec = measured(col, sacct_line("20"))
    assert rec["req_gpus"] == 0 and rec["used_gpu"] is False         # sacct alone: no GPUs
    col.store([rec])
    used, util, vector = site.one("SELECT used_gpu, avg_gpu_util, feature_vector FROM job_summary "
                                  "WHERE job_id = '20'")
    assert used == 1 and util == pytest.approx(70.0)
    assert json.loads(vector)[2] == 1.0
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '20'")[0] == 2


def test_a_second_reading_never_clears_the_flag(site):
    site.store(seen_by_squeue(site.col, "21"))
    col = JobMetricsCollector({}, str(site.db))
    col.store([measured(col, sacct_line("21"))])
    with sqlite3.connect(site.db) as c:                  # as if stored by 1.14.3
        c.execute("UPDATE jobs SET req_gpus = 0 WHERE job_id = '21'")
    col.store([measured(col, sacct_line("21"))])
    assert site.one("SELECT used_gpu FROM job_summary WHERE job_id = '21'")[0] == 1


def test_the_metrics_reading_keeps_the_stored_count_too(site):
    site.store(seen_by_squeue(site.col, "22"))
    col = JobMetricsCollector({}, str(site.db))
    col.store([measured(col, sacct_line("22"))])
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '22'")[0] == 2
    col.store([measured(col, sacct_line("22", req_tres=GPU_TRES.replace("=2", "=4"),
                                        alloc=GPU_TRES.replace("=2", "=4")))])
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '22'")[0] == 4


def test_without_gpu_samples_the_figure_is_none():
    c = sqlite3.connect(":memory:")
    assert gpu_util_during(c, "gpu01", "2026-06-05T10:00:00", "2026-06-05T12:00:00") is None   # no table
    c.execute("CREATE TABLE gpu_stats (timestamp TEXT, gpu_util_percent REAL)")
    assert gpu_util_during(c, "gpu01", "2026-06-05T10:00:00", "2026-06-05T12:00:00") is None   # no DCGM
    assert gpu_util_during(c, None, "2026-06-05T10:00:00", "2026-06-05T12:00:00") is None
    assert with_gpu_flag("[0.5, 0.2, 0.0, 0.0]") == "[0.5, 0.2, 1.0, 0.0]"
    assert with_gpu_flag(None) is None and with_gpu_flag("not json") == "not json"


def at_version(db, last):
    with MigrationManager(db) as mgr:
        mgr.get_current_version()
        for version, description, sql in MIGRATIONS:
            if version <= last:
                mgr.apply_migration(version, description, sql)


def test_the_migration_marks_stored_gpu_jobs(tmp_path):
    db = tmp_path / "old.db"
    at_version(db, 23)
    with sqlite3.connect(db) as c:
        ensure_job_columns(c)
        have = {r[1] for r in c.execute("PRAGMA table_info(gpu_stats)")}
        for col, kind in (("node_name", "TEXT"), ("data_source", "TEXT"), ("real_util_pct", "REAL")):
            if col not in have:                     # the GPU collector adds them
                c.execute(f"ALTER TABLE gpu_stats ADD COLUMN {col} {kind}")
        jobs = [("1", 2, "gpu01"), ("2", 0, "cn01"), ("3", 1, "gpu02"), ("4", 1, "gpu01")]
        for jid, gpus, node in jobs:
            c.execute("INSERT INTO jobs (job_id, user_name, state, req_gpus, node_list, start_time, end_time) "
                      "VALUES (?, 'u', 'COMPLETED', ?, ?, '2026-06-05T10:00:00', '2026-06-05 12:00:00')",
                      (jid, gpus, node))
        vec = json.dumps([0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 1.0])
        c.executemany("INSERT INTO job_summary (job_id, used_gpu, feature_vector, avg_gpu_util) VALUES (?, ?, ?, ?)",
                      [("1", 0, vec, None), ("2", 0, vec, None), ("3", 0, vec, None), ("4", 1, vec, 55.0)])
    dcgm(db, "gpu01", [("2026-06-05T11:00:00", 40.0)])
    ensure_database(db)
    got = {r[0]: r[1:] for r in sqlite3.connect(db).execute(
        "SELECT job_id, used_gpu, avg_gpu_util, feature_vector FROM job_summary")}
    assert got["1"][:2] == (1, 40.0) and json.loads(got["1"][2])[2] == 1.0
    assert got["2"][:2] == (0, None) and json.loads(got["2"][2])[2] == 0.0      # a CPU job
    assert got["3"][:2] == (1, None)                                             # no samples on its node
    assert got["4"][:2] == (1, 55.0)                                             # already right
    assert fix_gpu_summaries(sqlite3.connect(db)) == (0, 0)                      # nothing left


def test_on_a_hub_the_sites_stay_apart(tmp_path):
    c = sqlite3.connect(tmp_path / "combined.db")
    c.execute("CREATE TABLE jobs (job_id TEXT, req_gpus INT, node_list TEXT, start_time TEXT, "
              "end_time TEXT, source_site TEXT)")
    c.execute("CREATE TABLE job_summary (job_id TEXT, used_gpu INT, avg_gpu_util REAL, source_site TEXT)")
    c.execute("CREATE TABLE gpu_stats (timestamp TEXT, gpu_index INT, node_name TEXT, data_source TEXT, "
              "real_util_pct REAL, source_site TEXT)")
    c.executemany("INSERT INTO jobs VALUES ('7', ?, 'gpu01', '2026-06-05T10:00:00', '2026-06-05T12:00:00', ?)",
                  [(1, "a"), (0, "b")])
    c.executemany("INSERT INTO job_summary VALUES ('7', 0, NULL, ?)", [("a",), ("b",)])
    c.executemany("INSERT INTO gpu_stats VALUES ('2026-06-05T11:00:00', 0, 'gpu01', 'dcgm', ?, ?)",
                  [(30.0, "a"), (90.0, "b")])
    assert fix_gpu_summaries(c) == (1, 1)
    got = {s: (u, v) for s, u, v in c.execute("SELECT source_site, used_gpu, avg_gpu_util FROM job_summary")}
    assert got == {"a": (1, 30.0), "b": (0, None)}


def test_the_migration_passes_a_database_without_jobs(tmp_path):
    db = tmp_path / "ws.db"
    ensure_database(db)
    assert sqlite3.connect(db).execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 24
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE jobs (job_id TEXT)")
    c.execute("CREATE TABLE job_summary (job_id TEXT)")
    assert fix_gpu_summaries(c) == (0, 0)


def test_the_import_keeps_the_stored_count(site):
    from nomad.collectors import sacct_import as SI
    site.store(seen_by_squeue(site.col, "30"))
    with sqlite3.connect(site.db) as c:
        rec = site.col.job_from_fields(fields("30")).to_dict()
        c.execute(SI._FILL, tuple(rec[f] for f in SI._JOB_FIELDS))
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '30'")[0] == 2
    with sqlite3.connect(site.db) as c:
        rec = site.col.job_from_fields(fields("30", req_tres=GPU_TRES.replace("=2", "=3"))).to_dict()
        c.execute(SI._FILL, tuple(rec[f] for f in SI._JOB_FIELDS))
    assert site.one("SELECT req_gpus FROM jobs WHERE job_id = '30'")[0] == 3
