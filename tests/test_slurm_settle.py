"""Jobs that leave the queue get their real outcome from sacct, never an assumed one."""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomad.collectors import slurm as S
from nomad.collectors.slurm import SlurmCollector
from nomad.db.migrations import ensure_database
from nomad.insights.signals import job_outcome

SUBMIT = "2026-10-05T09:00:00"
SQ = ("{id}|u{id}|people|{part}|job|{state}|n01|4|4G|N/A|1-00:00:00|1:00:00|"
      "2026-10-05T10:00:00|2026-10-05T10:05:00")
SA = ("{id}|u{id}|people|basic|job|{state}|n02|4|4G|billing=4|1-00:00:00|00:10:00|"
      "{submit}|2026-10-05T09:01:00|{end}|{exit}")


@pytest.fixture(autouse=True)
def eastern(monkeypatch):
    """Local time is not UTC here, as at the sites: UTC slips must show."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class _Keep(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture(autouse=True)
def no_swallowed_errors():
    """store() logs and goes on if settling raises; here that is a failure.
    (A handler of our own: nomad's loggers need not propagate to pytest's.)"""
    log = logging.getLogger("nomad.collectors.slurm")
    keep = _Keep()
    log.addHandler(keep)
    yield
    log.removeHandler(keep)
    bad = [m for m in keep.messages if "Failed to settle" in m]
    assert not bad, bad


class FakeSlurm:
    """squeue and sacct as subprocess.run sees them."""

    def __init__(self, queue=(), history=(), known=None, sacct_ok=True, squeue_ok=True,
                 during_lookup=None):
        self.queue = [q if len(q) == 3 else (*q, "basic") for q in queue]  # (id, state, part)
        self.history = list(history)        # (id, state, end, exit) from the 7-day pull
        self.known = dict(known or {})      # id -> (state, end, exit[, submit]) for sacct -j
        self.sacct_ok = sacct_ok
        self.squeue_ok = squeue_ok
        self.during_lookup = during_lookup
        self.looked_up = []
        self.batches = []
        self.pull_argv = None

    def __call__(self, argv, **kw):
        out = ""
        if argv[0] == "squeue":
            if not self.squeue_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="squeue: error")
            if argv[-1] == "%P|%t":
                out = "\n".join(f"{p}|{'R' if s == 'RUNNING' else 'PD'}" for _, s, p in self.queue)
            else:
                out = "\n".join(SQ.format(id=i, state=s, part=p) for i, s, p in self.queue)
        elif argv[0] == "sacct":
            if not self.sacct_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="slurmdbd down")
            if "-j" in argv:
                ids = argv[argv.index("-j") + 1].split(",")
                self.looked_up += ids
                self.batches.append(len(ids))
                if self.during_lookup:
                    self.during_lookup()
                lines = []
                for i, v in self.known.items():
                    if i in ids:
                        st, end, ex, *sub = v
                        lines.append(SA.format(id=i, state=st, end=end, exit=ex,
                                               submit=sub[0] if sub else SUBMIT))
                out = "\n".join(lines)
            else:
                self.pull_argv = argv
                out = "\n".join(SA.format(id=i, state=s, end=e, exit=x, submit=SUBMIT)
                                for i, s, e, x in self.history)
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    # The once-per-database repair of stored start times has its own tests
    # (test_job_identity.py); these jobs' made-up times would set it off.
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO config (key, value) VALUES (?, 'done in the fixture')",
                  (S._STARTS_REPAIRED,))

    def run(fake, config=None):
        col = SlurmCollector(config or {}, str(db))
        monkeypatch.setattr(S.subprocess, "run", fake)
        data = col.collect()
        col.store(data)
        fake.collector, fake.data = col, data
        return fake
    return SimpleNamespace(db=str(db), run=run)


def add_job(db, job_id, state, end=None, exit_code=None, submit=None, failure_reason=0):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs (job_id, user_name, partition, state, end_time, exit_code, "
                  "submit_time, failure_reason) VALUES (?, ?, 'basic', ?, ?, ?, ?, ?)",
                  (job_id, f"u{job_id}", state, end, exit_code, submit, failure_reason))


def job(db, job_id):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    c.close()
    return row


# -- jobs that just left the queue ---------------------------------------------

def test_a_job_that_left_the_queue_gets_its_outcome_from_sacct(site):
    add_job(site.db, "1001", "RUNNING", submit=SUBMIT)
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                              known={"1001": ("FAILED", "2026-10-05T11:00:00", "1:0")}))
    r = job(site.db, "1001")
    assert r["state"] == "FAILED" and r["exit_code"] == 1
    assert r["end_time"] == "2026-10-05T11:00:00"
    assert fake.looked_up == ["1001"]
    assert job(site.db, "1002")["state"] == "RUNNING"


def test_one_sacct_cannot_account_for_is_unknown_and_neither_outcome(site):
    add_job(site.db, "1001", "RUNNING", exit_code=0)    # sacct reports 0:0 while running
    site.run(FakeSlurm(queue=[("1002", "RUNNING")]))
    r = job(site.db, "1001")
    assert r["state"] == "UNKNOWN"
    assert r["exit_code"] is None and r["exit_signal"] is None and r["failure_reason"] is None
    assert "T" in r["end_time"]                      # local ISO time, like every other
    assert abs(datetime.fromisoformat(r["end_time"]) - datetime.now()).total_seconds() < 60
    assert job_outcome(r["state"]) == "other"


def test_the_seven_day_pull_settles_it_first_and_covers_all_users(site):
    add_job(site.db, "1001", "RUNNING")
    fake = site.run(FakeSlurm(history=[("1001", "TIMEOUT", "2026-10-05T12:00:00", "0:15")]))
    assert job(site.db, "1001")["state"] == "TIMEOUT"
    assert fake.looked_up == []
    assert "--allusers" in fake.pull_argv


def test_a_job_sacct_still_reports_running_is_not_looked_up(site):
    """squeue missed it, but this run's pull has it running: it has not ended."""
    add_job(site.db, "1001", "RUNNING")
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                              history=[("1001", "RUNNING", "Unknown", "0:0")]))
    assert job(site.db, "1001")["state"] == "RUNNING"
    assert "1001" not in fake.looked_up


def test_an_empty_queue_with_no_recent_jobs_still_settles(site):
    """The old cleanup ran only while something was running, and a run with
    nothing to store never reached store() at all."""
    add_job(site.db, "1001", "RUNNING")
    add_job(site.db, "1003", "PENDING")
    fake = site.run(FakeSlurm(known={"1001": ("COMPLETED", "2026-10-05T11:00:00", "0:0"),
                                     "1003": ("CANCELLED by 42", "2026-10-05T11:05:00", "0:0")}))
    assert job(site.db, "1001")["state"] == "COMPLETED"
    assert job(site.db, "1001")["exit_code"] == 0
    assert job(site.db, "1003")["state"].startswith("CANCELLED")
    assert fake.collector.count_records(fake.data) == 0
    assert "queue empty" in fake.collector.note


def test_nothing_is_concluded_when_squeue_did_not_answer(site):
    add_job(site.db, "1001", "RUNNING")
    fake = site.run(FakeSlurm(squeue_ok=False,
                              history=[("9", "COMPLETED", "2026-10-05T08:00:00", "0:0")]))
    assert job(site.db, "1001")["state"] == "RUNNING"
    assert fake.looked_up == []


def test_a_job_in_a_partition_not_followed_is_still_there(site):
    """squeue's list counts before the partition filter: a requeued job shows as 'a,b'."""
    add_job(site.db, "1001", "PENDING")
    fake = site.run(FakeSlurm(queue=[("1001", "PENDING", "a,b"), ("1002", "RUNNING", "a")],
                              sacct_ok=False), config={"partitions": ["a"]})
    assert job(site.db, "1001")["state"] == "PENDING"
    assert "1001" not in fake.looked_up


def test_an_empty_partition_list_means_all(site):
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING", "gpu")]), config={"partitions": []})
    assert job(site.db, "1002")["state"] == "RUNNING"
    assert fake.collector.partitions is None


def test_sacct_down_ended_jobs_are_unknown_and_old_guesses_wait(site):
    add_job(site.db, "1001", "RUNNING")
    add_job(site.db, "2001", "COMPLETED", end="2026-09-01 12:00:00", submit=SUBMIT)
    site.run(FakeSlurm(queue=[("1002", "RUNNING")], sacct_ok=False))
    assert job(site.db, "1001")["state"] == "UNKNOWN"
    old = job(site.db, "2001")
    assert old["state"] == "COMPLETED" and old["end_time"] == "2026-09-01 12:00:00"


def test_an_unknown_job_sacct_later_has_pending_is_put_back(site):
    """Marked UNKNOWN while sacct was down; sacct then says it is pending."""
    add_job(site.db, "1001", "UNKNOWN", end=datetime.now().isoformat(timespec="seconds"),
            submit=SUBMIT)
    site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                       known={"1001": ("PENDING", "Unknown", "0:0")}))
    assert job(site.db, "1001")["state"] == "PENDING"


def test_a_recent_unknown_is_looked_up_again(site):
    add_job(site.db, "1001", "UNKNOWN", end=datetime.now().isoformat(timespec="seconds"),
            submit=SUBMIT)
    site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                       known={"1001": ("FAILED", "2026-10-05T11:00:00", "2:0")}))
    assert job(site.db, "1001")["state"] == "FAILED"


def test_a_pending_array_range_is_dropped_not_kept_as_a_job(site):
    add_job(site.db, "500_[5-10%2]", "PENDING")
    add_job(site.db, "501_3", "RUNNING")
    fake = site.run(FakeSlurm(queue=[("500_[7-10%2]", "PENDING")],
                              known={"501_3": ("COMPLETED", "2026-10-05T11:00:00", "0:0")}))
    assert not any("[" in i for i in fake.looked_up)
    assert job(site.db, "500_[5-10%2]") is None
    assert job(site.db, "501_3")["state"] == "COMPLETED"


def test_sacct_still_running_is_left_alone(site):
    add_job(site.db, "1001", "RUNNING")
    site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                       known={"1001": ("RUNNING", "Unknown", "0:0")}))
    assert job(site.db, "1001")["state"] == "RUNNING"


def test_transitional_states_are_settled_too(site):
    add_job(site.db, "1001", "COMPLETING")
    site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                       known={"1001": ("NODE_FAIL", "2026-10-05T11:00:00", "0:0")}))
    assert job(site.db, "1001")["state"] == "NODE_FAIL"


def test_the_database_is_not_locked_while_sacct_answers(site):
    add_job(site.db, "1001", "RUNNING")
    seen = []

    def try_write():
        c = sqlite3.connect(site.db, timeout=0)
        try:
            c.execute("CREATE TABLE IF NOT EXISTS probe (x)")
            c.commit()
            seen.append("free")
        except sqlite3.OperationalError as e:
            seen.append(str(e))
        finally:
            c.close()
    site.run(FakeSlurm(queue=[("1002", "RUNNING")], during_lookup=try_write))
    assert seen == ["free"]


def test_lookups_are_batched_and_capped(site, monkeypatch):
    monkeypatch.setattr(S, "LOOKUP_BATCH", 3)
    monkeypatch.setattr(S, "LOOKUP_MAX", 5)
    for i in range(8):
        add_job(site.db, str(3000 + i), "RUNNING")
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")]))
    assert fake.batches == [3, 2]
    states = [job(site.db, str(3000 + i))["state"] for i in range(8)]
    assert states.count("UNKNOWN") == 5 and states.count("RUNNING") == 3


# -- outcomes assumed by earlier versions ------------------------------------------

def test_old_guesses_are_repaired_or_marked_unknown(site):
    add_job(site.db, "2001", "COMPLETED", end="2026-09-01 12:00:00", submit=SUBMIT)
    add_job(site.db, "2002", "COMPLETED", end="2026-09-02 16:30:00", submit=SUBMIT)
    add_job(site.db, "2004", "COMPLETED", end="2026-09-04 10:00:00", exit_code=0,
            submit=SUBMIT)                              # seen running: exit code 0
    add_job(site.db, "2003", "COMPLETED", end="2026-09-03T10:00:00", exit_code=0,
            submit=SUBMIT)                              # a real one
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                              known={"2001": ("OUT_OF_MEMORY", "2026-09-01T07:59:00", "0:125"),
                                     "2004": ("FAILED", "2026-09-04T05:00:00", "1:0")}))
    assert job(site.db, "2001")["state"] == "OUT_OF_MEMORY"
    assert job(site.db, "2001")["end_time"] == "2026-09-01T07:59:00"
    assert job(site.db, "2004")["state"] == "FAILED"
    lost = job(site.db, "2002")
    assert lost["state"] == "UNKNOWN" and lost["failure_reason"] is None
    assert lost["end_time"] == "2026-09-02T12:30:00"   # 16:30 UTC is 12:30 in Richmond
    assert job(site.db, "2003")["state"] == "COMPLETED"
    assert "2003" not in fake.looked_up

    # Repaired rows are not asked about again.
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")]))
    assert fake.looked_up == []


def test_a_reused_job_number_does_not_lend_its_outcome(site):
    """sacct -j answers with the newest job of a number; an old row is another job."""
    add_job(site.db, "2001", "COMPLETED", end="2026-03-01 12:00:00",
            submit="2026-03-01T08:00:00")
    site.run(FakeSlurm(queue=[("1002", "RUNNING")],
                       known={"2001": ("FAILED", "2026-10-05T11:00:00", "1:0",
                                       "2026-10-05T09:00:00")}))
    r = job(site.db, "2001")
    assert r["state"] == "UNKNOWN"
    assert r["end_time"] == "2026-03-01T07:00:00"       # EST in March


def test_guesses_go_before_recent_unknowns(site, monkeypatch):
    monkeypatch.setattr(S, "LOOKUP_BATCH", 1)
    add_job(site.db, "1001", "UNKNOWN", end=datetime.now().isoformat(timespec="seconds"),
            submit=SUBMIT)
    add_job(site.db, "2001", "COMPLETED", end="2026-09-01 12:00:00", submit=SUBMIT)
    fake = site.run(FakeSlurm(queue=[("1002", "RUNNING")]))
    assert fake.looked_up == ["2001"]


def test_utc_to_local():
    assert S._utc_to_local("2026-10-05T10:00:00") == "2026-10-05T10:00:00"
    assert S._utc_to_local(None) is None
    assert S._utc_to_local("2026-10-05 14:00:00") == "2026-10-05T10:00:00"


# -- readers keep UNKNOWN out of success and failure -----------------------------

def test_community_export_leaves_unknown_out(site):
    from nomad.community import load_jobs_from_db
    add_job(site.db, "1", "COMPLETED", exit_code=0)
    add_job(site.db, "2", "UNKNOWN", failure_reason=None)
    assert [j["job_id"] for j in load_jobs_from_db(Path(site.db))] == ["1"]



# -- partitions (1.7.43) -------------------------------------------------------

def test_jobs_come_from_every_partition_the_list_limits_the_queue_snapshot(site):
    """A partition list (the setup wizard writes the partitions sinfo shows)
    limits queue_state only; jobs of other partitions are recorded too."""
    fake = site.run(FakeSlurm(queue=[("1001", "RUNNING", "a"), ("1002", "PENDING", "b"),
                                     ("1003", "PENDING", "a,b")],
                              history=[("9", "COMPLETED", "2026-10-05T08:00:00", "0:0")]),
                    config={"partitions": ["a"]})
    assert {r for r in ("1001", "1002", "1003", "9") if job(site.db, r) is not None} == {
        "1001", "1002", "1003", "9"}
    assert job(site.db, "1002")["partition"] == "b"
    assert job(site.db, "9")["partition"] == "basic"        # the sacct pull's partition
    c = sqlite3.connect(site.db)
    snapshot = {r[0] for r in c.execute("SELECT partition FROM queue_state")}
    c.close()
    assert snapshot == {"a"}


def test_a_job_submitted_to_several_partitions_shows_where_it_runs(site):
    site.run(FakeSlurm(queue=[("1003", "PENDING", "a,b")]))
    assert job(site.db, "1003")["partition"] == "a,b"
    site.run(FakeSlurm(queue=[("1003", "RUNNING", "b")]))
    assert job(site.db, "1003")["partition"] == "b"
    # Requeued: waiting again, for any of its partitions.
    site.run(FakeSlurm(queue=[("1003", "PENDING", "a,b")]))
    assert job(site.db, "1003")["partition"] == "a,b"


def upsert(db, job_id, partition, state):
    """One record through the slurm collector's upsert."""
    rec = {f: None for f in S._JOB_FIELDS}
    rec.update(job_id=job_id, user_name="u", partition=partition, state=state,
               submit_time="2026-10-05T09:00:00", failure_reason=0, req_cpus=1,
               req_mem_mb=1, req_gpus=0)
    with sqlite3.connect(db) as c:
        S.ensure_job_columns(c)
        c.execute(S._JOB_UPSERT, tuple(rec[f] for f in S._JOB_FIELDS))


@pytest.mark.parametrize("steps, final", [
    ([("b", "RUNNING"), ("a,b", "CANCELLED by 1")], "b"),      # ended with its list: ran in b
    ([("b", "RUNNING"), ("a,b", "REQUEUED")], "a,b"),
    ([("b", "RUNNING"), ("c", "RUNNING")], "c"),                # moved (scontrol update)
    ([("b", "RUNNING"), (None, "RUNNING")], "b"),
    ([("b", "RUNNING"), ("", "COMPLETED")], "b"),
    ([("", "PENDING"), ("a,b", "PENDING")], "a,b"),             # nothing stored yet
    ([("", "PENDING"), ("a,b", "CANCELLED")], "a,b"),
    ([("a,b", "PENDING"), ("a,c", "PENDING")], "a,c"),
])
def test_the_stored_partition(site, steps, final):
    for partition, state in steps:
        upsert(site.db, "77", partition, state)
    assert job(site.db, "77")["partition"] == final


def test_squeue_sees_hidden_partitions(site):
    calls = []

    class Seen(FakeSlurm):
        def __call__(self, argv, **kw):
            calls.append(argv)
            return super().__call__(argv, **kw)
    site.run(Seen(queue=[("1001", "RUNNING", "hidden")]))
    squeues = [a for a in calls if a[0] == "squeue"]
    assert squeues and all("-a" in a for a in squeues)


def test_a_job_in_a_partition_not_listed_gets_its_outcome(site):
    add_job(site.db, "1005", "RUNNING", submit=SUBMIT)
    site.run(FakeSlurm(queue=[("1006", "RUNNING", "a")],
                       known={"1005": ("TIMEOUT", "2026-10-05T11:00:00", "0:15")}),
             config={"partitions": ["a"]})
    assert job(site.db, "1005")["state"] == "TIMEOUT"


def test_a_partition_list_that_isnt_text_does_not_stop_the_collector(site):
    site.run(FakeSlurm(queue=[("1007", "RUNNING", "2024")]), config={"partitions": [2024]})
    assert job(site.db, "1007")["state"] == "RUNNING"
