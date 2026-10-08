"""1.7.44: `nomad import sacct` (an export, or sacct for a period) and the
monthly usage totals from sreport (collector slurm_usage)."""
import gzip
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad.collectors import sacct_import as si
from nomad.collectors import slurm as S
from nomad.collectors import slurm_usage as U
from nomad.collectors.plan import plan
from nomad.db.migrations import ensure_database


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


# An `sacct -a -X -P -o ...` export: some fields of -o ALL, in its own order.
HEADER = ("Account|AdminComment|AllocCPUS|AllocTRES|Elapsed|End|ExitCode|Group|JobID|JobName|"
          "NodeList|Partition|ReqMem|ReqTRES|Start|State|Submit|SubmitLine|Timelimit|User|WorkDir")


def rec(job_id, user="u1", name="job", state="COMPLETED", part="basic", nodes="cn01",
        submit="2025-10-05T09:00:00", start="2025-10-05T09:01:00", end="2025-10-05T09:11:00",
        elapsed="00:10:00", account="lab1", tres="billing=8,cpu=8,mem=41G,node=1",
        workdir="/home/u1/p/r", submitline="sbatch run.sh", exit_code="0:0"):
    values = dict(Account=account, AdminComment="", AllocCPUS="8", AllocTRES=tres,
                  Elapsed=elapsed, End=end, ExitCode=exit_code, Group="people", JobID=job_id,
                  JobName=name, NodeList=nodes, Partition=part, ReqMem="41G",
                  ReqTRES="billing=8,cpu=8,mem=41G,node=1", Start=start, State=state,
                  Submit=submit, SubmitLine=submitline, Timelimit="1-00:00:00", User=user,
                  WorkDir=workdir)
    return "|".join(values[h] for h in HEADER.split("|"))


def export(tmp_path, *lines, gz=False, name="sacct_all.psv"):
    p = tmp_path / (name + (".gz" if gz else ""))
    text = "\n".join((HEADER,) + lines) + "\n"
    if gz:
        with gzip.open(p, "wt") as f:
            f.write(text)
    else:
        p.write_text(text)
    return str(p)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "site.db"
    ensure_database(path)
    return str(path)


def rows(db, sql="SELECT * FROM jobs ORDER BY job_id", *args):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, args)]
    c.close()
    return out


def run_import(db, path, apply=True):
    c = sqlite3.connect(db) if apply else sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return si.import_export(c, path, apply)
    finally:
        c.close()


# -- reading an export --------------------------------------------------------------------

def test_an_export_is_read_by_its_header(tmp_path):
    p = export(tmp_path, rec("100"), rec("100.batch"), rec("101", name="a|b|c"),
               rec("102", submitline="sbatch --wrap 'x | y'"),
               rec("103", workdir="/home/u1/odd|dir"),
               rec("104", submitline="sbatch \\\n  --mem=4G run.sh"))
    out = list(si.read_export(si._open(p)))
    assert [r and r["JobID"] for r in out] == ["100", "100.batch", "101", "102", "103", "104"]
    assert out[2]["JobName"] == "a|b|c" and out[2]["State"] == "COMPLETED"
    assert out[3]["SubmitLine"] == "sbatch --wrap 'x | y'"
    assert out[4]["WorkDir"] == "/home/u1/odd|dir"
    assert out[5]["SubmitLine"] == "sbatch \\\n  --mem=4G run.sh" and out[5]["User"] == "u1"


def test_unreadable_records_are_counted_not_guessed(tmp_path):
    bad = rec("105").replace("2025-10-05T09:00:00", "garbage")
    p = export(tmp_path, bad, rec("106"))
    out = list(si.read_export(si._open(p)))
    assert out[0] is None and out[1]["JobID"] == "106"
    # A record cut short, at the end of the file and in the middle.
    cut = "|".join(rec("108").split("|")[:12])
    p = export(tmp_path, rec("107"), cut, name="cut.psv")
    assert [r and r["JobID"] for r in si.read_export(si._open(p))] == ["107", None]
    p = export(tmp_path, rec("107"), cut, rec("109"), name="cut2.psv")
    assert [r and r["JobID"] for r in si.read_export(si._open(p))] == ["107", None, "109"]


def test_a_gzipped_export_reads_the_same(tmp_path):
    p = export(tmp_path, rec("100"), rec("101"), gz=True)
    assert [r["JobID"] for r in si.read_export(si._open(p))] == ["100", "101"]


# -- importing -------------------------------------------------------------------------------

def test_a_dry_run_counts_and_writes_nothing(db, tmp_path):
    p = export(tmp_path, rec("100"), rec("100.batch"), rec("101", submit="2025-11-02T08:00:00",
                                                           start="2025-11-02T08:00:05",
                                                           end="2025-11-02T09:00:05",
                                                           elapsed="01:00:00"))
    c = run_import(db, p, apply=False)
    assert (c.records, c.steps, c.new, c.same) == (3, 1, 2, 0)
    assert (c.first, c.last, c.months) == ("2025-10", "2025-11", {"2025-10": 1, "2025-11": 1})
    assert rows(db) == []


def test_new_jobs_are_written_with_every_field(db, tmp_path):
    p = export(tmp_path, rec("100", tres="cpu=6,gres/gpu:a40=1,gres/gpu=1,mem=32G,node=1",
                             state="FAILED", exit_code="1:0"))
    c = run_import(db, p)
    assert c.new == 1
    r = rows(db)[0]
    assert (r["user_name"], r["state"], r["failure_reason"], r["account"], r["alloc_gpus"],
            r["work_root"], r["work_tail"], r["runtime_seconds"], r["wait_time_seconds"]) == (
        "u1", "FAILED", S.FAILURE_FAILED, "lab1", 1, "/home", "p/r", 600, 60)


def test_a_stored_job_only_gets_what_it_lacks(db, tmp_path):
    c = sqlite3.connect(db)
    S.ensure_job_columns(c)
    # As job_metrics stored it: no account, no failure reason, its own node list.
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
              "start_time, end_time, runtime_seconds, node_list, exit_code) VALUES ('100', 'u1', "
              "'job', 'basic', 'FAILED', '2025-10-05T09:00:00', '2025-10-05T09:01:00', "
              "'2025-10-05T09:11:00', 600, 'cn02', 1)")
    c.commit()
    c.close()
    counts = run_import(db, export(tmp_path, rec("100", state="FAILED", exit_code="1:0")))
    assert (counts.new, counts.same, counts.filled) == (0, 1, 1)
    r = rows(db)[0]
    assert (r["account"], r["work_root"], r["node_list"], r["failure_reason"]) == (
        "lab1", "/home", "cn02", S.FAILURE_FAILED)


def test_a_job_stored_as_running_takes_its_outcome_and_a_newer_one_keeps_its_own(db, tmp_path):
    c = sqlite3.connect(db)
    S.ensure_job_columns(c)
    for jid, state, end in (("200", "RUNNING", None), ("201", "COMPLETED", "2025-10-05T09:11:00")):
        c.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
                  "start_time, end_time) VALUES (?, 'u1', 'job', 'basic', ?, '2025-10-05T09:00:00', "
                  "'2025-10-05T09:01:00', ?)", (jid, state, end))
    c.commit()
    c.close()
    p = export(tmp_path, rec("200", state="TIMEOUT", exit_code="0:15"),
               rec("201", state="RUNNING", end="Unknown", elapsed="00:05:00"))
    counts = run_import(db, p)
    assert counts.ended == 1
    r = {x["job_id"]: x for x in rows(db)}
    assert (r["200"]["state"], r["200"]["end_time"]) == ("TIMEOUT", "2025-10-05T09:11:00")
    assert (r["201"]["state"], r["201"]["end_time"]) == ("COMPLETED", "2025-10-05T09:11:00")


def test_the_partition_a_job_ran_in_replaces_a_list(db, tmp_path):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time) "
              "VALUES ('300', 'u1', 'job', 'a,b', 'CANCELLED', '2025-10-05T09:00:00')")
    c.commit()
    c.close()
    run_import(db, export(tmp_path, rec("300", part="b", state="CANCELLED")))
    assert rows(db)[0]["partition"] == "b"


def test_an_older_job_with_a_number_now_taken_is_kept_apart(db, tmp_path):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO jobs (job_id, user_name, job_name, partition, state, submit_time, "
              "end_time) VALUES ('400', 'new', 'n', 'basic', 'COMPLETED', '2026-08-01T00:00:00', "
              "'2026-08-01T01:00:00')")
    c.commit()
    c.close()
    counts = run_import(db, export(tmp_path, rec("400", user="old")))
    assert counts.older == 1
    by_id = {r["job_id"]: r["user_name"] for r in rows(db)}
    assert by_id == {"400": "new", "400@2025-10-05T09:00:00": "old"}
    # Importing again changes nothing and adds nothing.
    again = run_import(db, export(tmp_path, rec("400", user="old")))
    assert (again.new, again.older, again.same, len(rows(db))) == (0, 0, 1, 2)


def test_importing_twice_is_the_same_as_once(db, tmp_path):
    p = export(tmp_path, rec("100"), rec("101", submit="2025-11-02T08:00:00",
                                         start="2025-11-02T08:01:00", end="2025-11-02T08:11:00"))
    run_import(db, p)
    first = rows(db)
    second = run_import(db, p)
    assert (second.new, second.same) == (0, 2) and rows(db) == first


def test_repeated_records_count_once(db, tmp_path):
    counts = run_import(db, export(tmp_path, rec("100"), rec("100")))
    assert (counts.new, counts.repeats) == (1, 1)


def test_an_export_whose_jobs_reuse_numbers_keeps_both(db, tmp_path):
    """One export across a counter restart: the same number for two jobs."""
    p = export(tmp_path, rec("7", user="new", submit="2026-06-01T00:00:00",
                             start="2026-06-01T00:00:01", end="2026-06-01T00:10:01"),
               rec("7", user="old", submit="2025-10-05T09:00:00"))
    run_import(db, p)
    assert {r["job_id"]: r["user_name"] for r in rows(db)} == {
        "7": "new", "7@2025-10-05T09:00:00": "old"}


# -- from sacct, a month at a time ----------------------------------------------------------------

def test_month_windows():
    w = list(si.month_windows(date(2025, 10, 15), date(2026, 1, 3)))
    assert w == [(date(2025, 10, 15), date(2025, 11, 1)), (date(2025, 11, 1), date(2025, 12, 1)),
                 (date(2025, 12, 1), date(2026, 1, 1)), (date(2026, 1, 1), date(2026, 1, 3))]


SA = ("{id}|u1|people|basic|job|COMPLETED|cn01|8|41G|billing=8|1-00:00:00|00:10:00|"
      "{submit}|{start}|{end}|0:0|lab1|cpu=8|/home/u1/p/r")


def test_import_from_sacct_asks_month_by_month(db, monkeypatch):
    asked = []

    def fake(argv, **kw):
        a = next(x for x in argv if x.startswith("--starttime=")).split("=")[1]
        asked.append(a)
        # A job running across the month boundary is listed in both months.
        out = SA.format(id="9", submit="2025-10-31T20:00:00", start="2025-10-31T20:00:00",
                        end="2025-11-01T02:00:00")
        if a == "2025-11-01":
            out += "\n" + SA.format(id="10", submit="2025-11-02T00:00:00",
                                    start="2025-11-02T00:00:00", end="2025-11-02T00:10:00")
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(S.subprocess, "run", fake)
    c = sqlite3.connect(db)
    counts = si.import_from_sacct(c, date(2025, 10, 1), date(2025, 12, 1), apply=True)
    c.close()
    assert asked == ["2025-10-01", "2025-11-01"]
    assert (counts.new, counts.repeats) == (2, 1)
    assert sorted(r["job_id"] for r in rows(db)) == ["10", "9"]


# -- the command ------------------------------------------------------------------------------------

def test_the_command(db, tmp_path, monkeypatch):
    from nomad.cli import cli
    p = export(tmp_path, rec("100"), rec("101", submit="2025-11-02T08:00:00",
                                         start="2025-11-02T08:01:00", end="2025-11-02T08:11:00"))
    runner = CliRunner()
    r = runner.invoke(cli, ["import", "sacct", p, "--db", db])
    assert r.exit_code == 0, r.output
    assert "dry run" in r.output and "jobs added" in r.output and "--apply" in r.output
    assert rows(db) == []
    r = runner.invoke(cli, ["import", "sacct", p, "--db", db, "--apply"])
    assert r.exit_code == 0, r.output
    assert len(rows(db)) == 2 and "2025-10" in r.output and "2025-11" in r.output
    assert runner.invoke(cli, ["import", "sacct", "--db", db]).exit_code != 0
    assert runner.invoke(cli, ["import", "sacct", p, "--from", "2025-10-01", "--db", db]
                         ).exit_code != 0
    assert runner.invoke(cli, ["import", "sacct", "--from", "2025-13-01", "--db", db]
                         ).exit_code != 0
    missing = str(tmp_path / "none.db")
    r = runner.invoke(cli, ["import", "sacct", p, "--db", missing])
    assert r.exit_code != 0 and not Path(missing).exists()


# -- sreport ----------------------------------------------------------------------------------------

SREPORT = """--------------------------------------------------------------------------------
Cluster Utilization 2026-09-01T00:00:00 - 2026-09-30T23:59:59
Usage reported in TRES Hours
--------------------------------------------------------------------------------
Cluster|TRES Name|Allocated|Down|PLND Down|Idle|{planned}|Reported
c1|cpu|547983|38741|0|123082|413393|1123200
c1|gres/gpu|8866|800|0|3294|0|12960
"""


@pytest.mark.parametrize("planned", ["Planned", "Reserved"])
def test_sreport_is_read_by_its_header(planned):
    rows_ = U.parse_sreport(SREPORT.format(planned=planned))
    assert rows_[0] == {"cluster": "c1", "tres": "cpu", "allocated_h": 547983.0,
                        "down_h": 38741.0, "planned_down_h": 0.0, "idle_h": 123082.0,
                        "planned_h": 413393.0, "reported_h": 1123200.0}
    assert rows_[1]["tres"] == "gres/gpu"


def test_an_old_sreport_with_over_comm_reads_too():
    text = ("Cluster|TRES Name|Allocated|Down|PLND Down|Idle|Reserved|Over Comm|Reported\n"
            "c1|cpu|10|1|0|5|2|0|20\n")
    assert U.parse_sreport(text)[0]["reported_h"] == 20.0


class FakeSreport:
    """sreport over [start, end): months from `first` on have usage, except
    `gaps`; GPU accounting optional; `fail` months error; `timeout` months
    time out."""

    def __init__(self, first="2024-01", gpu=True, fail_months=(), gaps=(), timeout=(),
                 error="sreport: error: Problem talking to the database"):
        self.first, self.gpu, self.fail = first, gpu, set(fail_months)
        self.gaps, self.timeout, self.error = set(gaps), set(timeout), error
        self.asked = []

    def has(self, month):
        return month >= self.first and month not in self.gaps

    def __call__(self, argv, **kw):
        start = next(a for a in argv if a.startswith("start="))[6:]
        end = next(a for a in argv if a.startswith("end="))[4:]
        tres = argv[argv.index("-T") + 1]
        self.asked.append((start[:7], tres))
        if start[:7] in self.timeout:
            raise U.subprocess.TimeoutExpired(argv, 60)
        if start[:7] in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.error)
        if "gres/gpu" in tres and not self.gpu:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="sreport: error: Invalid TRES gres/gpu")
        months, m = [], datetime.strptime(start[:10], "%Y-%m-%d").date()
        stop = datetime.strptime(end[:10], "%Y-%m-%d").date()
        while m < stop:
            months.append(m.strftime("%Y-%m"))
            m = U.next_month(m)
        n = sum(1 for x in months if self.has(x))
        out = "Cluster|TRES Name|Allocated|Down|PLND Down|Idle|Planned|Reported\n"
        if n:
            out += f"c1|cpu|{100 * n}|0|0|{50 * n}|{10 * n}|{160 * n}\n"
            if "gres/gpu" in tres:
                out += f"c1|gres/gpu|{5 * n}|0|0|{5 * n}|0|{10 * n}\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture
def usage(db, monkeypatch):
    monkeypatch.setattr(U, "find_tool", lambda name: "/usr/bin/" + name)

    def run(fake, config=None):
        monkeypatch.setattr(U.subprocess, "run", fake)
        col = U.SlurmUsageCollector(config or {}, db)
        data = col.collect()
        if data:
            col.store(data)
        return col, fake
    return run


def months_back(n):
    m = U.month_start(date.today())
    out = []
    for _ in range(n):
        out.append(m.strftime("%Y-%m"))
        m = U.previous_month(m)
    return out


def test_the_first_runs_go_back_to_the_clusters_start_newest_first(usage, db):
    first = months_back(30)[-1]                       # the cluster began 30 months ago
    col, fake = usage(FakeSreport(first=first), {"months_per_run": 12})
    assert [m for m, _ in fake.asked] == months_back(12)
    for _ in range(3):
        usage(FakeSreport(first=first), {"months_per_run": 12})
    stored = rows(db, "SELECT DISTINCT month FROM cluster_usage WHERE reported_h > 0")
    assert len(stored) == 30
    floor = rows(db, "SELECT value FROM config WHERE key = 'slurm_usage.nothing_before'")
    assert floor and floor[0]["value"] < first
    # Done: later runs ask nothing until this month is due again.
    col, fake = usage(FakeSreport(first=first), {"months_per_run": 12})
    assert fake.asked == [] and col.note == "up to date"


def test_this_month_is_asked_again_when_due(usage, db):
    usage(FakeSreport(first="2000-01"), {"months_per_run": 3})
    c = sqlite3.connect(db)
    c.execute("UPDATE cluster_usage_asked SET asked_at = '2000-01-01T00:00:00' WHERE settled = 0")
    c.commit()
    c.close()
    col, fake = usage(FakeSreport(first="2000-01"), {"months_per_run": 3})
    this = months_back(1)[0]
    assert (this, "cpu,gres/gpu") in fake.asked
    assert rows(db, "SELECT settled FROM cluster_usage WHERE month = ? AND tres = 'cpu'",
                this)[0]["settled"] == 0
    old = months_back(3)[-1]
    assert rows(db, "SELECT settled FROM cluster_usage WHERE month = ? AND tres = 'cpu'",
                old)[0]["settled"] == 1


def test_since_limits_how_far_back(usage, db):
    since = months_back(5)[-1]
    col, fake = usage(FakeSreport(first="2000-01"), {"since": since, "months_per_run": 24})
    assert [m for m, _ in fake.asked] == months_back(5)


def test_without_gpu_accounting_cpu_only(usage, db):
    col, fake = usage(FakeSreport(first="2000-01", gpu=False), {"months_per_run": 2})
    assert fake.asked[0][1] == "cpu,gres/gpu" and all(t == "cpu" for _, t in fake.asked[1:])
    assert {r["tres"] for r in rows(db, "SELECT tres FROM cluster_usage")} == {"cpu"}


def test_a_failure_after_some_months_keeps_them(usage, db):
    fail = months_back(3)[-1]
    col, fake = usage(FakeSreport(first="2000-01", fail_months=[fail]), {"months_per_run": 6})
    assert "stopped after 2 months" in col.note
    assert len(rows(db, "SELECT DISTINCT month FROM cluster_usage")) == 2


def test_no_sreport_says_so(db, monkeypatch):
    monkeypatch.setattr(U, "find_tool", lambda name: None)
    col = U.SlurmUsageCollector({}, db)
    with pytest.raises(U.MissingToolError):
        col.collect()


def test_it_runs_where_slurm_does():
    on = {p.name: p for p in plan({})}
    assert on["slurm_usage"].enabled and "as the slurm collector" in on["slurm_usage"].why
    off = {p.name: p for p in plan({"collectors": {"slurm": {"enabled": False}}})}
    assert not off["slurm_usage"].enabled
    own = {p.name: p for p in plan({"collectors": {"slurm": {"enabled": False},
                                                   "slurm_usage": {"enabled": True}}})}
    assert own["slurm_usage"].enabled


# -- review findings (1.7.44) -------------------------------------------------------------------

def test_a_newline_in_the_last_field_and_a_pipe_before_a_newline(tmp_path):
    p = export(tmp_path, rec("100", workdir="/home/u1/a\nb"),
               rec("101", submitline="sbatch --wrap 'a | b\n c'"), rec("102"))
    out = list(si.read_export(si._open(p)))
    assert [r and r["JobID"] for r in out] == ["100", "101", "102"]
    assert out[0]["WorkDir"] == "/home/u1/a\nb"
    assert out[1]["SubmitLine"] == "sbatch --wrap 'a | b\n c'" and out[1]["User"] == "u1"


def test_pipes_in_two_fields_and_a_name_that_looks_like_a_path(tmp_path):
    p = export(tmp_path, rec("100", name="x|/scratch/y", workdir="/home/u/z"),
               rec("101", name="a|b", submitline="sbatch --wrap 'c | d'"))
    out = list(si.read_export(si._open(p)))
    assert (out[0]["JobName"], out[0]["WorkDir"]) == ("x|/scratch/y", "/home/u/z")
    assert (out[1]["JobName"], out[1]["SubmitLine"]) == ("a|b", "sbatch --wrap 'c | d'")


def test_an_export_without_a_header_is_refused(tmp_path):
    p = tmp_path / "noheader.psv"
    p.write_text(rec("100") + "\n" + rec("101") + "\n")
    with pytest.raises(si.ExportError):
        list(si.read_export(si._open(str(p))))
    empty = tmp_path / "empty.psv"
    empty.write_text("")
    assert list(si.read_export(si._open(str(empty)))) == []


def stored(db, job_id, **cols):
    c = sqlite3.connect(db)
    S.ensure_job_columns(c)
    base = dict(job_id=job_id, user_name="u1", job_name="job", partition="basic",
                submit_time="2025-10-05T09:00:00")
    base.update(cols)
    c.execute(f"INSERT INTO jobs ({', '.join(base)}) VALUES ({', '.join('?' * len(base))})",
              tuple(base.values()))
    c.commit()
    c.close()


def test_a_job_without_an_outcome_takes_the_records_one(db, tmp_path):
    stored(db, "200", state="UNKNOWN", end_time="2026-10-08T08:00:00")
    stored(db, "201", state="COMPLETED", end_time="2025-10-05 13:11:00")   # assumed, UTC
    stored(db, "202", state=None)
    p = export(tmp_path, rec("200", state="FAILED", exit_code="1:0"),
               rec("201", state="TIMEOUT", exit_code="0:15"),
               rec("202", state="FAILED", exit_code="1:0"))
    counts = run_import(db, p)
    r = {x["job_id"]: x for x in rows(db)}
    assert (r["200"]["state"], r["200"]["end_time"], r["200"]["exit_code"],
            r["200"]["failure_reason"]) == ("FAILED", "2025-10-05T09:11:00", 1, S.FAILURE_FAILED)
    assert (r["201"]["state"], r["201"]["end_time"]) == ("TIMEOUT", "2025-10-05T09:11:00")
    assert (r["202"]["state"], r["202"]["failure_reason"]) == ("FAILED", S.FAILURE_FAILED)
    assert counts.ended == 3


def test_a_stored_outcome_is_not_mixed_with_another(db, tmp_path):
    """Stored COMPLETED, records FAILED: nothing of the outcome changes."""
    stored(db, "210", state="COMPLETED", end_time="2025-10-05T09:11:00", exit_code=0,
           failure_reason=0)
    run_import(db, export(tmp_path, rec("210", state="FAILED", exit_code="1:0")))
    r = rows(db)[0]
    assert (r["state"], r["exit_code"], r["failure_reason"], r["account"]) == (
        "COMPLETED", 0, 0, "lab1")


def test_a_running_job_from_an_old_export_goes_in_as_unknown(db, tmp_path):
    p = export(tmp_path, rec("220", state="RUNNING", end="Unknown", elapsed="00:05:00"),
               rec("221_[5-10]", state="PENDING", start="Unknown", end="Unknown",
                   elapsed="00:00:00", nodes="None assigned"))
    counts = run_import(db, p)
    assert (counts.new, counts.unknown, counts.ranges) == (1, 1, 1)
    r = rows(db)[0]
    assert (r["job_id"], r["state"], r["end_time"], r["exit_code"]) == ("220", "UNKNOWN", None, None)


def test_an_earlier_look_at_a_stored_job_fills_but_keeps_the_outcome(db, tmp_path):
    """Stored: the requeued job (newer submit, running). The record: its first
    run (older submit, ended). Account goes in; the outcome stays."""
    stored(db, "230", state="RUNNING", submit_time="2025-10-06T00:00:00", req_time_seconds=86400)
    counts = run_import(db, export(tmp_path, rec("230", state="NODE_FAIL")))
    r = rows(db)[0]
    assert counts.stale == 1 and counts.filled == 1
    assert (r["state"], r["end_time"], r["account"]) == ("RUNNING", None, "lab1")


def test_from_sacct_keeps_the_months_done_when_sacct_fails(db, monkeypatch):
    def fake(argv, **kw):
        a = next(x for x in argv if x.startswith("--starttime=")).split("=")[1]
        if a == "2025-11-01":
            return SimpleNamespace(returncode=1, stdout="", stderr="slurmdbd down")
        return SimpleNamespace(returncode=0, stderr="", stdout=SA.format(
            id="9", submit="2025-10-05T00:00:00", start="2025-10-05T00:00:00",
            end="2025-10-05T00:10:00") + "\n")
    monkeypatch.setattr(S.subprocess, "run", fake)
    c = sqlite3.connect(db)
    with pytest.raises(si.SacctStopped) as e:
        si.import_from_sacct(c, date(2025, 10, 1), date(2025, 12, 1), apply=True)
    c.close()
    assert e.value.counts.new == 1 and e.value.month == date(2025, 11, 1)
    assert [r["job_id"] for r in rows(db)] == ["9"]
    from nomad.cli import cli
    r = CliRunner().invoke(cli, ["import", "sacct", "--from", "2025-10-01", "--to", "2025-12-01",
                                 "--db", db])
    assert r.exit_code != 0 and "2025-11" in r.output and "records read" in r.output


def test_no_lock_held_for_long_while_importing(db, tmp_path, monkeypatch):
    """A collector writing with a short busy timeout gets in during an import."""
    import threading
    monkeypatch.setattr(si, "COMMIT_EVERY", 0.02)
    monkeypatch.setattr(si, "YIELD_FOR", 0.05)
    p = export(tmp_path, *[rec(str(1000 + i), submit=f"2025-10-05T{9 + i // 3600:02d}:"
                               f"{i // 60 % 60:02d}:{i % 60:02d}") for i in range(3000)])
    failures, writes, done = [], [], threading.Event()

    def collector():
        while not done.is_set():
            try:
                w = sqlite3.connect(db, timeout=0.3)
                w.execute("INSERT INTO queue_state (partition, pending_jobs, running_jobs, "
                          "total_jobs, timestamp) VALUES ('p', 0, 0, 0, 'now')")
                w.commit()
                w.close()
                writes.append(1)
            except sqlite3.OperationalError as e:
                failures.append(str(e))
            time.sleep(0.02)
    t = threading.Thread(target=collector)
    t.start()
    try:
        run_import(db, p)
    finally:
        done.set()
        t.join()
    assert len(rows(db)) == 3000 and writes and not failures


def test_the_command_needs_an_existing_database_and_reads_odd_paths(tmp_path):
    from nomad.cli import cli
    p = export(tmp_path, rec("100"))
    missing = tmp_path / "typo.db"
    r = CliRunner().invoke(cli, ["import", "sacct", p, "--db", str(missing), "--apply"])
    assert r.exit_code != 0 and not missing.exists()
    odd = tmp_path / "a#b?c.db"
    ensure_database(odd)
    r = CliRunner().invoke(cli, ["import", "sacct", p, "--db", str(odd)])
    assert r.exit_code == 0, r.output
    assert "jobs added                       1" in r.output


def test_the_command_fails_when_nothing_reads(tmp_path, db):
    from nomad.cli import cli
    p = tmp_path / "bad.psv"
    p.write_text(HEADER + "\nnot|a|record\n")
    r = CliRunner().invoke(cli, ["import", "sacct", str(p), "--db", db])
    assert r.exit_code != 0 and "no record could be read" in r.output
    p.write_text(rec("100") + "\n")
    r = CliRunner().invoke(cli, ["import", "sacct", str(p), "--db", db])
    assert r.exit_code != 0 and "header" in r.output


def test_a_base_format_line_with_extra_fields_gets_no_new_fields():
    col = S.SlurmCollector({}, ":memory:")
    line = ("4711|u1|people|basic|a|b|COMPLETED|cn01|8|41G|billing=8|1-00:00:00|00:10:00|"
            "2026-10-05T09:00:00|2026-10-05T09:01:00|2026-10-05T09:11:00|0:0")
    j = col._parse_sacct_job(line)
    assert (j.job_name, j.account, j.alloc_tres) == ("a|b", None, None)


def test_an_empty_month_is_not_asked_every_run(usage, db):
    this = months_back(1)[0]
    # This month has nothing yet: asked once, then again only when due.
    col, fake = usage(FakeSreport(first=this, gaps=[this]), {"months_per_run": 2})
    assert (this, "cpu,gres/gpu") in fake.asked
    col, fake = usage(FakeSreport(first=this), {"months_per_run": 2})
    assert (this, "cpu,gres/gpu") not in fake.asked
    assert rows(db, "SELECT * FROM cluster_usage WHERE cluster = ''") == []


def test_a_long_outage_is_not_taken_for_the_start(usage, db):
    """Three empty months in the middle of a cluster's life: the question
    about everything before them finds the earlier years, and backfill goes on."""
    gap = months_back(6)[-3:]                      # months 4-6 back
    first = months_back(20)[-1]
    for _ in range(4):
        usage(FakeSreport(first=first, gaps=gap), {"months_per_run": 6})
    assert rows(db, "SELECT value FROM config WHERE key = 'slurm_usage.nothing_before'") == [] \
        or rows(db, "SELECT value FROM config WHERE key = 'slurm_usage.nothing_before'"
                )[0]["value"] < first
    with_data = rows(db, "SELECT DISTINCT month FROM cluster_usage")
    assert len(with_data) == 17                    # 20 months minus the 3-month outage


def test_a_passing_sreport_error_does_not_drop_gpus(usage, db):
    this = months_back(1)[0]
    with pytest.raises(Exception):
        usage(FakeSreport(first="2000-01", fail_months=[this]), {"months_per_run": 2})
    assert rows(db, "SELECT * FROM config WHERE key = 'slurm_usage.cpu_only'") == []
    col, fake = usage(FakeSreport(first="2000-01"), {"months_per_run": 2})
    assert all(t == "cpu,gres/gpu" for _, t in fake.asked)


def test_cpu_only_is_remembered(usage, db):
    usage(FakeSreport(first="2000-01", gpu=False), {"months_per_run": 2})
    col, fake = usage(FakeSreport(first="2000-01", gpu=False), {"months_per_run": 2})
    assert fake.asked and all(t == "cpu" for _, t in fake.asked)


def test_a_timeout_ends_the_run_without_retries(usage, db):
    this = months_back(1)[0]
    col, fake = usage(FakeSreport(first="2000-01", timeout=[this]), {"months_per_run": 3})
    assert len(fake.asked) == 1 and "timed out" in col.note


def test_settings_that_dont_read_fall_back(db, caplog):
    col = U.SlurmUsageCollector({"since": "2024/01", "months_per_run": "lots",
                                 "refresh_hours": "x"}, db)
    assert (col.since, col.months_per_run, col.refresh_hours) == (None, 24, 6.0)


def test_one_collector_that_cant_start_does_not_stop_the_others(tmp_path, monkeypatch):
    from nomad.collectors import plan as plan_mod

    def boom(*a, **k):
        raise ValueError("bad setting")
    monkeypatch.setattr(U.SlurmUsageCollector, "__init__", boom)
    collectors, _ = plan_mod.build({}, tmp_path / "x.db")
    names = {c.name for c in collectors}
    assert "slurm_usage" not in names and "slurm" in names


def test_every_cluster_in_sreport_is_kept(usage, db):
    class Two(FakeSreport):
        def __call__(self, argv, **kw):
            r = super().__call__(argv, **kw)
            if r.returncode == 0 and "c1|cpu" in r.stdout:
                r.stdout += "c2|cpu|1|0|0|1|0|2\n"
            return r
    usage(Two(first="2000-01"), {"months_per_run": 1})
    assert {r["cluster"] for r in rows(db, "SELECT cluster FROM cluster_usage")} == {"c1", "c2"}


def test_a_newline_before_the_job_id_does_not_merge_two_jobs(tmp_path):
    """AdminComment comes before JobID in -o ALL: the record's first line
    shows no job id. It must not end up on the job before."""
    second = rec("101", user="u9", workdir="/scratch/u9/run")
    second = second.replace("lab1||", "lab1|note line 1\nline 2|", 1)
    p = export(tmp_path, rec("100"), second, rec("102"))
    out = list(si.read_export(si._open(p)))
    assert [r and r["JobID"] for r in out] == ["100", "101", "102"]
    assert (out[0]["User"], out[0]["WorkDir"]) == ("u1", "/home/u1/p/r")
    assert (out[1]["User"], out[1]["AdminComment"]) == ("u9", "note line 1\nline 2")


def test_a_line_that_doesnt_read_is_counted_not_merged(tmp_path):
    bad = rec("101").replace("2025-10-05T09:00:00", "garbage")
    p = export(tmp_path, rec("100"), bad, rec("102"))
    out = list(si.read_export(si._open(p)))
    assert [r and r["JobID"] for r in out] == ["100", None, "102"]
    assert out[0]["WorkDir"] == "/home/u1/p/r"


def test_an_empty_export_is_an_error(tmp_path, db):
    from nomad.cli import cli
    p = tmp_path / "empty.psv"
    p.write_text(HEADER + "\n")
    r = CliRunner().invoke(cli, ["import", "sacct", str(p), "--db", db])
    assert r.exit_code != 0 and "no records" in r.output


def test_pipes_before_a_newline_that_fill_the_columns_after_it(tmp_path):
    """A submit line whose first line has as many '|' as there are columns
    after it reads as a record on its own, wrongly; the next line fixes it."""
    p = export(tmp_path, rec("100"),
               rec("101", user="u9", submitline="sbatch --wrap='cat x|sort|uniq|head\n|wc -l'"),
               rec("102"))
    out = list(si.read_export(si._open(p)))
    assert [r and r["JobID"] for r in out] == ["100", "101", "102"]
    assert (out[1]["User"], out[1]["WorkDir"], out[1]["Timelimit"]) == (
        "u9", "/home/u1/p/r", "1-00:00:00")
    assert out[1]["SubmitLine"] == "sbatch --wrap='cat x|sort|uniq|head\n|wc -l'"
