"""nomad 1.13: CPU from TotalCPU, advice naming the flag a job used, jobs that
end out of memory, node diagnosis by site, queue waits from the eligible time,
lab-group attribution, the tier's own partition, names for admins."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import job_metrics as JM
from nomad.collectors import slurm as S
from nomad.collectors.job_metrics import JobMetricsCollector, _cpu_request_flag
from nomad.collectors.slurm import SlurmCollector
from nomad.db.migrations import ensure_database


# -- how a job asked for its cores ----------------------------------------------------

@pytest.mark.parametrize("line, flag", [
    ("sbatch -c 4 job.sh", "cpus-per-task"),
    ("sbatch --cpus-per-task=8 job.sh -n 4", "cpus-per-task"),   # -n is the script's
    ("sbatch -n 8 job.sh", "ntasks"),
    ("sbatch --ntasks 8 job.sh", "ntasks"),
    ("sbatch -n8 job.sh", "ntasks"),
    ("sbatch -N 2 --ntasks-per-node=4 job.sh", "ntasks"),
    ("sbatch -n 1 job.sh", None),                                # one task: says nothing
    ("sbatch -n 1 -c 4 job.sh", "cpus-per-task"),
    ("sbatch --mem=4G job.sh -n 8", None),
    ("sbatch --exclusive job.sh -n 8", None),
    ("sbatch -N1 job.sh -n 8", None),
    ("sbatch --wrap='python x.py -n 8'", None),
    ("sbatch -c 2 --wrap 'mpirun -n 8 x'", "cpus-per-task"),
    ("srun -n 4 ./a.out", "ntasks"),
    ("sbatch job.sh", None),
    ("", None),
    ("sbatch 'unbalanced", None),
])
def test_the_flag_comes_from_the_command_line_only(line, flag):
    assert _cpu_request_flag(line) == flag


def test_total_cpu_times():
    c = JobMetricsCollector({}, ":memory:")
    assert c._parse_cpu_seconds("00:02.500") == pytest.approx(2.5)
    assert c._parse_cpu_seconds("01:00:00") == 3600
    assert c._parse_cpu_seconds("2-01:00:00") == 2 * 86400 + 3600
    assert c._parse_cpu_seconds("") == 0 and c._parse_cpu_seconds("junk") == 0


# -- job_metrics, end to end ------------------------------------------------------------

def jm_row(job_id, *, state="COMPLETED", elapsed="01:00:00", cpus=16, mem="16G", avecpu="",
           maxrss="", total="", ntasks="", submit_line="", alloc=""):
    parts = [job_id, "run", "u1", "people", "basic", state, "0:0",
             "2026-10-05T09:00:00", "2026-10-05T09:00:00", "2026-10-05T10:00:00",
             elapsed, "02:00:00", str(cpus), mem, f"cpu={cpus}", avecpu, maxrss, "", "",
             "", "", "", "", "cn01", total, ntasks, alloc, submit_line]
    return "|".join(parts)


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))

    def run(lines, fail_submitline=False):
        asked = []

        def fake(argv, **kw):
            fmt = argv[argv.index("--format") + 1]
            asked.append(fmt)
            if fail_submitline and "SubmitLine" in fmt:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="sacct: error: Invalid field requested: \"SubmitLine\"")
            out = lines if "SubmitLine" in fmt else [
                "|".join(x.split("|")[:27]) for x in lines]
            return SimpleNamespace(returncode=0, stdout="\n".join(out) + "\n", stderr="")
        monkeypatch.setattr(JM.subprocess, "run", fake)
        col = JobMetricsCollector({}, str(db))
        col.store(col.collect())
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        rows = {r["job_id"]: dict(r) for r in c.execute("SELECT * FROM job_summary")}
        c.close()
        return rows, asked
    return run


def test_cpu_use_is_every_task_not_one(metrics):
    # 16 tasks, each busy all hour: TotalCPU 16 h. AveCPU (one task's average)
    # is 1 h, which read as 6% before 1.13.
    rows, _ = metrics([
        jm_row("100", total="16:00:00", submit_line="sbatch -n 16 mpi.sh"),
        jm_row("100.batch", avecpu="00:00:01", maxrss="100M", ntasks="1"),
        jm_row("100.0", avecpu="01:00:00", maxrss="1G", ntasks="16"),
    ])
    r = rows["100"]
    assert r["avg_cpu_percent"] == pytest.approx(100.0)
    assert r["max_tasks"] == 16 and r["cpu_request"] == "ntasks"


def test_without_total_cpu_the_step_averages_still_count(metrics):
    rows, _ = metrics([
        jm_row("101", cpus=4),
        jm_row("101.batch", cpus=4, avecpu="02:00:00", maxrss="1G", ntasks="1"),
    ])
    assert rows["101"]["avg_cpu_percent"] == pytest.approx(50.0)
    assert rows["101"]["max_tasks"] == 1 and rows["101"]["cpu_request"] is None


def test_the_flag_from_the_command_line_and_from_the_steps(metrics):
    rows, _ = metrics([
        jm_row("102", total="04:00:00", submit_line="sbatch -c 8 threads.sh"),
        jm_row("102.batch", avecpu="04:00:00", maxrss="1G", ntasks="1"),
        jm_row("103", total="04:00:00", submit_line="sbatch job.sh"),
        jm_row("103.0", avecpu="00:30:00", maxrss="1G", ntasks="8"),
    ])
    assert rows["102"]["cpu_request"] == "cpus-per-task"
    assert rows["103"]["cpu_request"] == "ntasks"          # its step ran 8 tasks


def test_a_slurm_without_submitline_is_asked_again(metrics):
    rows, asked = metrics([jm_row("104", total="01:00:00"),
                           jm_row("104.batch", avecpu="01:00:00", ntasks="1")],
                          fail_submitline=True)
    assert "SubmitLine" in asked[0] and "SubmitLine" not in asked[-1]
    assert rows["104"]["avg_cpu_percent"] == pytest.approx(100 / 16)


def test_out_of_memory_jobs_are_collected(metrics):
    rows, _ = metrics([jm_row("105", state="OUT_OF_MEMORY", total="00:30:00"),
                       jm_row("105.batch", avecpu="00:30:00", maxrss="16G", ntasks="1")])
    assert "105" in rows
    assert "OUT_OF_MEMORY" in JobMetricsCollector.ENDED_STATES


def test_new_columns_exist_after_migration(tmp_path):
    db = tmp_path / "n.db"
    ensure_database(Path(db))
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(job_summary)")}
    assert {"max_tasks", "cpu_request"} <= cols


# -- advice names the flag ----------------------------------------------------------

def _cpu(summary_extra):
    from nomad.edu.scoring import score_cpu
    job = {"req_cpus": 8, "runtime_seconds": 3600, "state": "COMPLETED"}
    return score_cpu(job, {"avg_cpu_percent": 12.5, **summary_extra})


def test_cpu_advice_names_the_flag_the_job_used():
    from nomad.edu.scoring import format_suggestion_directive
    s = _cpu({"cpu_request": "cpus-per-task"}).suggestion
    assert s.directive == "cpus-per-task"
    assert format_suggestion_directive(s) == "#SBATCH --cpus-per-task=1"
    assert _cpu({"cpu_request": "ntasks"}).suggestion.directive == "ntasks"
    s = _cpu({}).suggestion
    assert s.directive == "cores"
    assert format_suggestion_directive(s).startswith("#SBATCH --cpus-per-task=1   (or --ntasks")


def test_jobs_asking_different_ways_share_one_cpu_issue():
    from nomad.edu import insights as I
    from nomad.edu.scoring import Suggestion

    def sugg(d):
        return Suggestion(directive=d, suggested_value=1, current_value=8, actual_usage=1,
                          unit="cores", rationale="")
    fam = I._directive_family
    mixed = [sugg("cpus-per-task"), sugg("cores"), sugg("cpus-per-task")]
    assert {fam(s.directive) for s in mixed} == {"cores"}
    assert I.sbatch_lines("cores", "2") == [
        "#SBATCH --cpus-per-task=2", "(or --ntasks=2, if the job runs several tasks: srun, MPI)"]
    assert I.sbatch_lines("cpus-per-task", "2") == ["#SBATCH --cpus-per-task=2"]
    assert I.sbatch_lines("gres", "1") == ["#SBATCH --gres=gpu:1"]
    assert I.sbatch_lines("mem", "4G") == ["#SBATCH --mem=4G"]
    assert I.DIRECTIVE_LABEL["cpus-per-task"][0] == "--cpus-per-task"
    assert I._format_value_for_directive("cores", 2.0) == "2"


def test_memory_of_a_multi_task_job_is_not_scored():
    from nomad.edu.scoring import score_memory
    job = {"req_mem_mb": 16384, "state": "COMPLETED"}
    d = score_memory(job, {"peak_memory_gb": 1.5, "max_tasks": 8})
    assert not d.applicable and "8 tasks" in d.detail
    assert score_memory(job, {"peak_memory_gb": 1.5, "max_tasks": 1}).applicable
    # Out of memory is still a finding, whatever the tasks.
    oom = score_memory({**job, "state": "OUT_OF_MEMORY"}, {"peak_memory_gb": 16, "max_tasks": 8})
    assert oom.applicable and oom.score == 15


# -- explain: a job against the jobs before it ---------------------------------------

def _explain_db(tmp_path, jobs):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, state TEXT, end_time TEXT, "
              "req_cpus INTEGER, req_mem_mb INTEGER, runtime_seconds INTEGER, "
              "req_time_seconds INTEGER, partition TEXT, source_site TEXT)")
    c.execute("CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, "
              "peak_memory_gb REAL, source_site TEXT)")
    for jid, site, end, cpu in jobs:
        c.execute("INSERT INTO jobs VALUES (?, 'u1', 'COMPLETED', ?, 4, 4096, 3600, 7200, "
                  "'basic', ?)", (jid, end, site))
        c.execute("INSERT INTO job_summary VALUES (?, ?, 2.0, ?)", (jid, cpu, site))
    c.commit()
    c.close()
    return str(db)


def test_progress_compares_a_job_with_the_jobs_before_it(tmp_path):
    from nomad.edu.explain import compute_progress, load_job, load_summary
    from nomad.edu.scoring import score_job
    db = _explain_db(tmp_path, [
        ("1", "a", "2026-10-01T10:00:00", 25.0), ("2", "a", "2026-10-02T10:00:00", 25.0),
        ("3", "a", "2026-10-03T10:00:00", 25.0), ("4", "a", "2026-10-04T10:00:00", 95.0),
        ("5", "a", "2026-10-05T10:00:00", 95.0), ("6", "a", "2026-10-06T10:00:00", 95.0),
        ("4", "b", "2026-10-04T11:00:00", 95.0),
    ])
    job = load_job(db, "4", "a")
    fp = score_job(job, load_summary(db, "4", "a"))
    p = compute_progress(db, "u1", fp, job)
    # Jobs 1-3 only: not job 4 itself, not the later 5 and 6, not site b's job 4.
    assert p["cpu"]["previous_avg"] < p["cpu"]["current"]
    assert p["cpu"]["trend"] == "improving"


# -- node diagnosis by site, ended jobs only -------------------------------------------

def test_node_jobs_are_found_by_site_and_running_ones_are_not_failures(tmp_path):
    from nomad.diag.node import get_failure_summary, get_recent_jobs
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, job_name TEXT, state TEXT, "
              "exit_code INTEGER, start_time TEXT, end_time TEXT, runtime_seconds INTEGER, "
              "failure_reason INTEGER, node_list TEXT, source_site TEXT)")
    rows = [("1", "COMPLETED", "2026-10-05T01", "a"), ("2", "FAILED", "2026-10-05T02", "a"),
            ("3", "RUNNING", None, "a"), ("4", "CANCELLED by 12", "2026-10-05T03", "a"),
            ("5", "FAILED", "2026-10-05T04", "b")]
    for jid, state, end, site in rows:
        c.execute("INSERT INTO jobs (job_id, user_name, state, end_time, node_list, source_site, "
                  "exit_code) VALUES (?, 'u', ?, ?, 'cn01', ?, 1)", (jid, state, end, site))
    c.commit()
    c.close()
    jobs = get_recent_jobs(str(db), "default", "cn01", site="a")
    assert [j["job_id"] for j in jobs] == ["4", "2", "1"]
    summary = get_failure_summary(jobs + [{"state": "RUNNING", "user_name": "u"}])
    assert summary["total_jobs"] == 3 and summary["failed_jobs"] == 2


# -- queue waits from the eligible time ---------------------------------------------

LINE = ("4711|u1|people|basic|myjob|COMPLETED|cn01|8|41G|billing=8|1-00:00:00|00:10:00|"
        "2026-10-05T09:00:00|2026-10-05T12:00:00|2026-10-05T12:10:00|0:0|lab1|cpu=8|"
        "{eligible}|/home/u1/a/b")


def test_eligible_time_is_read_and_kept_only_when_it_can_be_one():
    col = SlurmCollector({}, ":memory:")
    j = col._parse_sacct_job(LINE.format(eligible="2026-10-05T11:30:00"))
    assert j.eligible_time == datetime(2026, 10, 5, 11, 30)
    assert j.wait_time_seconds == 3 * 3600                     # still from submission
    assert col._parse_sacct_job(LINE.format(eligible="Unknown")).eligible_time is None
    assert col._parse_sacct_job(LINE.format(eligible="2026-10-05T08:00:00")).eligible_time is None
    assert col._parse_sacct_job(LINE.format(eligible="2026-10-05T13:00:00")).eligible_time is None
    assert "Eligible" in S.SACCT_FORMAT and S.SACCT_FORMAT.endswith(",WorkDir")


def _jobs_db(tmp_path, rows, site_col=None):
    from nomad.db.jobkeys import ensure_job_columns
    db = tmp_path / "w.db"
    ensure_database(Path(db))
    c = sqlite3.connect(db)
    ensure_job_columns(c)
    if site_col:
        c.execute(f"ALTER TABLE jobs ADD COLUMN {site_col} TEXT")
    for i, (part, submit, eligible, start, site) in enumerate(rows):
        wait = int((datetime.fromisoformat(start) - datetime.fromisoformat(submit)).total_seconds())
        c.execute("INSERT INTO jobs (job_id, user_name, partition, state, submit_time, "
                  "eligible_time, start_time, end_time, wait_time_seconds) "
                  "VALUES (?, 'u', ?, 'COMPLETED', ?, ?, ?, ?, ?)",
                  (str(i), part, submit, eligible, start, start, wait))
        if site_col:
            c.execute(f"UPDATE jobs SET {site_col} = ? WHERE job_id = ?", (site, str(i)))
    c.commit()
    c.close()
    return str(db)


def test_queue_wait_counts_from_when_the_job_could_start(tmp_path):
    from nomad.db.jobkeys import queue_wait_sql
    db = _jobs_db(tmp_path, [
        # waited a day on another job, then 10 minutes for the queue
        ("basic", "2026-10-01T09:00:00", "2026-10-02T09:00:00", "2026-10-02T09:10:00", None),
        # no eligible time recorded: from submission
        ("basic", "2026-10-01T09:00:00", None, "2026-10-01T09:20:00", None),
    ])
    c = sqlite3.connect(db)
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    waits = [r[0] for r in c.execute(f"SELECT {queue_wait_sql(cols)} FROM jobs ORDER BY job_id")]
    assert waits == [600, 1200]
    assert queue_wait_sql({"wait_time_seconds"}) == "wait_time_seconds"


def test_partition_waits_by_site_from_the_eligible_time(tmp_path):
    from nomad.edu.insights import _load_partition_wait, _partition_wait
    db = _jobs_db(tmp_path, [
        ("basic", "2026-10-01T09:00:00", "2026-10-02T09:00:00", "2026-10-02T09:10:00", "a"),
        ("basic", "2026-10-01T09:00:00", "2026-10-01T09:00:00", "2026-10-01T09:30:00", "a"),
        ("basic", "2026-10-01T09:00:00", None, "2026-10-01T11:00:00", "b"),
    ], site_col="source_site")
    waits = _load_partition_wait(db)
    assert waits[("a", "basic")] == pytest.approx(1200)      # median of 600 and 1800
    assert _partition_wait(waits, "b", "basic") == 7200
    # A single site's database: jobs carry no site.
    (tmp_path / "one").mkdir()
    db1 = _jobs_db(tmp_path / "one", [
        ("basic", "2026-10-01T09:00:00", None, "2026-10-01T09:05:00", None)])
    assert _partition_wait(_load_partition_wait(db1), "clusterA", "basic") == 300


def test_facts_waits_count_from_the_eligible_time(tmp_path):
    from nomad.facts.jobs import finished_rows
    db = _jobs_db(tmp_path, [
        ("basic", "2026-10-01T09:00:00", "2026-10-02T09:00:00", "2026-10-02T09:10:00", None)])
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = finished_rows(c, "2026-09-01", "2026-11-01")
    assert rows[0]["wait_seconds"] == 600


def test_the_collector_stores_the_eligible_time(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    ensure_database(Path(db))
    line = LINE.format(eligible="2026-10-05T11:30:00").replace("4711", "50")

    def fake(argv, **kw):
        out = line if argv[0] == "sacct" else ""
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(S.subprocess, "run", fake)
    col = SlurmCollector({}, str(db))
    col.store(col.collect())
    r = sqlite3.connect(db).execute("SELECT eligible_time, wait_time_seconds FROM jobs "
                                    "WHERE job_id = '50'").fetchone()
    assert r == ("2026-10-05T11:30:00", 3 * 3600)


def test_an_import_fills_in_eligible_times_for_stored_jobs(tmp_path):
    from nomad.collectors import sacct_import as si
    row = (None, "/home", "cpu=8", "lab1", "basic", 0, "2026-10-05T12:10:00", None)
    assert si.Importer._lacks(row, {"work_root": "/home", "alloc_tres": "cpu=8",
                                    "account": "lab1", "partition": "basic",
                                    "state": "COMPLETED", "failure_reason": 0,
                                    "eligible_time": "2026-10-05T11:30:00"}, outcome=False)


# -- the tier's own partition, named by site -----------------------------------------

def test_the_suggested_partition_is_the_tiers_own(tmp_path):
    from nomad.edu.insights import _load_cluster_capacities, _tier_partition
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, cluster TEXT, "
              "memory_total_mb INTEGER, partitions TEXT, source_site TEXT)")
    now = datetime.now().isoformat()
    nodes = [("cn01", 196608, "basic,all,sci"), ("cn02", 196608, "basic,all,sci"),
             ("cn03", 196608, "basic,all"),
             ("bm01", 786432, "condo1,large,all,sci"), ("bm02", 786432, "large,all"),
             ("bm03", 786432, "large,all")]
    for n, mem, parts in nodes:
        c.execute("INSERT INTO node_state VALUES (?, ?, 'default', ?, ?, 'clusterA')",
                  (now, n, mem, parts))
    c.commit()
    c.close()
    tiers = _load_cluster_capacities(str(db))
    assert [(t["cluster"], t["memory_gb"], t["partition"]) for t in tiers] == [
        ("clusterA", 192.0, "basic"), ("clusterA", 768.0, "large")]
    # condo1 (one node of the tier) and sci (spans tiers) are not suggested.
    assert _tier_partition({"partitions": "all,basic"}) == "basic"


# -- jobs placed by lab group ----------------------------------------------------------

def _groups_db(tmp_path, members, jobs):
    db = tmp_path / "g.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, submit_time TEXT, req_cpus INTEGER, "
              "req_mem_mb INTEGER, req_gpus INTEGER, runtime_seconds INTEGER)")
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, ?)", members)
    t = (datetime.now() - timedelta(hours=1)).isoformat()
    for i, u in enumerate(jobs):
        c.execute("INSERT INTO jobs VALUES (?, ?, ?, 4, 4096, 0, 600)", (str(i), u, t))
    c.commit()
    c.close()
    return db


def _att(db, **kw):
    from nomad.dynamics.attribution import job_attribution
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(days=1)).isoformat()
    return job_attribution(c, since, **kw), c


def test_jobs_are_placed_by_lab_group_when_other_groups_overlap(tmp_path):
    members = [("pa", "pa$"), ("s1", "pa$"), ("s2", "pa$"),
               ("pb", "pb$"), ("s3", "pb$"), ("s4", "pb$"),
               # course and department groups everyone mixes in
               ("s1", "chem101"), ("s3", "chem101"), ("s2", "bio"), ("s4", "bio"),
               ("pa", "faculty"), ("pb", "faculty")]
    for u in ("x1", "x2", "x3", "x4", "x5", "x6", "x7"):
        members.append((u, "people"))
    db = _groups_db(tmp_path, members, ["s1", "s2", "s3", "s4", "s1", "s3"])
    att, c = _att(db)
    assert att.available and att.method == "lab_group"
    got = dict(c.execute(f"SELECT j.user_name, {att.group_expr} FROM jobs j {att.join} "
                         "GROUP BY j.user_name").fetchall())
    assert got == {"s1": "pa$", "s2": "pa$", "s3": "pb$", "s4": "pb$"}
    # Without lab groups the same data can't be placed.
    att, _ = _att(db, lab_pattern=None)
    assert not att.available and "more than one group" in att.reason


def test_someone_in_two_labs_still_stops_the_group_views(tmp_path):
    members = [("pa", "pa$"), ("s1", "pa$"), ("s2", "pa$"), ("pb", "pb$"), ("s1", "pb$"),
               ("s3", "pb$")]
    db = _groups_db(tmp_path, members, ["s1", "s2", "s3"])
    att, _ = _att(db)
    assert not att.available and "lab group" in att.reason


# -- names for admins --------------------------------------------------------------------

def test_heavy_use_of_shared_hosts_names_people_for_admins():
    from nomad.actions.spec import ACTIONS
    a = {x.name: x for x in ACTIONS}["per_user"]
    assert "--mask" not in a.argv and a.names
