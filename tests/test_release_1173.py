# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.3: a shell ssh can use from a service account; runaway jobs (gone
from the queue, still active in sacct) kept, reported, and left out of the
condo reports; node alerts the hub sends for sites set to alerts = "hub"."""
import json
import os
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.shell_env import fix_shell, shell_ok

NOW = datetime(2026, 10, 26, 12, 0, 0)


# --- a shell for ssh's ProxyJump -------------------------------------------------------

@pytest.mark.parametrize("shell", [None, "", "/sbin/nologin", "/usr/sbin/nologin", "/bin/false",
                                   "/no/such/zsh", "bash"])
def test_a_shell_ssh_cant_use_becomes_bin_sh(shell):
    env = {} if shell is None else {"SHELL": shell}
    assert fix_shell(env) == (shell or "")
    assert env["SHELL"] == "/bin/sh"


def test_a_working_shell_is_left_as_it_is():
    env = {"SHELL": "/bin/sh"}
    assert shell_ok("/bin/sh") and fix_shell(env) is None and env == {"SHELL": "/bin/sh"}


def test_every_command_runs_with_a_usable_shell(monkeypatch, tmp_path):
    from nomad.cli import cli
    monkeypatch.setenv("SHELL", "/sbin/nologin")
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cli, ["config", "--help"])
    assert result.exit_code == 0, result.output
    assert os.environ["SHELL"] == "/bin/sh"           # and so for its ssh and scp


# --- node states, as the collector and the hub judge them ----------------------------------

def test_node_states_are_judged_one_way():
    from nomad.collectors.node_state import NodeStateCollector, classify_state
    assert classify_state("DOWN+NOT_RESPONDING") == "critical"
    assert classify_state("IDLE*") == "critical"                    # Slurm's not-responding mark
    assert classify_state("MIXED+DRAIN") == "warning"
    assert classify_state("weird") == "warning"                      # unhealthy, not named: warned
    assert classify_state("POWERED_DOWN", {"ignore_states": ["POWERED_DOWN"]}) is None
    assert classify_state("DRAIN", {"critical_states": ["DRAIN"]}) == "critical"
    assert classify_state("DOWN", None) == "critical"
    assert NodeStateCollector.DEFAULT_CRITICAL_STATES[0] == "DOWN"   # still there for callers


# --- runaway jobs -----------------------------------------------------------------------------

def _site(conn, active=("1", "2", "3")):
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT, submit_time TEXT)")
    for j in active:
        conn.execute("INSERT INTO jobs VALUES (?, 'PENDING', '2026-05-19T10:00:00')", (j,))


def _kept(conn):
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT job_id, first_seen, last_seen FROM runaway_jobs")}


def test_a_job_gone_from_the_queue_and_active_in_sacct_is_kept_with_when_first_seen():
    from nomad.collectors.slurm import record_runaways
    conn = sqlite3.connect(":memory:")
    _site(conn)
    seen = [("1", "PENDING", "2026-05-19T10:00:00"), ("2", "PENDING", "2026-05-19T10:00:00")]
    record_runaways(conn, seen, ["1", "2"], [], "2026-10-25T12:00:00")
    record_runaways(conn, seen, ["1", "2"], [], "2026-10-26T12:00:00")
    assert _kept(conn) == {"1": ("2026-10-25T12:00:00", "2026-10-26T12:00:00"),
                           "2": ("2026-10-25T12:00:00", "2026-10-26T12:00:00")}
    # 1: sacct settles it (looked up, not seen so); 2: the queue lists it again.
    record_runaways(conn, [], ["1"], ["2"], "2026-10-26T13:00:00")
    assert _kept(conn) == {}


def test_a_job_no_longer_stored_as_active_is_no_runaway():
    from nomad.collectors.slurm import record_runaways
    conn = sqlite3.connect(":memory:")
    _site(conn)
    record_runaways(conn, [("3", "PENDING", None)], ["3"], [], "2026-10-25T12:00:00")
    conn.execute("UPDATE jobs SET state = 'CANCELLED' WHERE job_id = '3'")
    record_runaways(conn, [], [], [], "2026-10-26T12:00:00")              # not looked up this time
    assert _kept(conn) == {}


def _hub_runaways(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runaway_jobs (job_id TEXT, state TEXT, submit_time TEXT, first_seen TEXT, "
                 "last_seen TEXT, source_site TEXT)")
    conn.executemany("INSERT INTO runaway_jobs VALUES (?, 'PENDING', ?, ?, ?, ?)", rows)
    return conn


def test_runaways_a_day_old_are_reported_by_count_and_date():
    from nomad.collection_health import _runaways
    day = timedelta(days=1, hours=1)
    conn = _hub_runaways([
        ("7", "2026-05-19T09:00:00", (NOW - day).isoformat(), NOW.isoformat(), "c1"),
        ("8", "2026-05-20T09:00:00", (NOW - day).isoformat(), NOW.isoformat(), "c1"),
        ("9", "2026-10-26T09:00:00", (NOW - timedelta(hours=2)).isoformat(), NOW.isoformat(), "c2")])
    found = _runaways(conn, NOW, {})
    assert [(p.key, p.details["count"]) for p in found] == [("runaway:c1", 2)]       # c2's only just went
    assert "sacctmgr show runawayjobs" in found[0].message and "2026-05-19" in found[0].message
    assert "7" not in found[0].message.replace("2026", "")                             # counts, not numbers


def test_the_condo_report_leaves_runaways_out_of_the_waiting():
    from nomad.condo.idle import runaway_ids
    conn = _hub_runaways([("7", None, NOW.isoformat(), NOW.isoformat(), "c1"),
                          ("8", None, NOW.isoformat(), NOW.isoformat(), "c2")])
    assert runaway_ids(conn, "c1") == {"7"}
    assert runaway_ids(sqlite3.connect(":memory:"), "c1") == set()                 # before 1.17.3


def test_the_runaway_table_reaches_the_hub_with_its_site(tmp_path):
    from nomad.collectors.slurm import record_runaways
    from nomad.db import delta_hub as H
    site = tmp_path / "site.db"
    conn = sqlite3.connect(site)
    _site(conn)
    record_runaways(conn, [("1", "PENDING", "2026-05-19T10:00:00")], ["1"], [], "2026-10-25T12:00:00")
    conn.commit()
    conn.close()
    hub = H.connect(tmp_path / "combined.db")
    H.apply_snapshot(hub, "c1", site)
    assert hub.execute("SELECT source_site, job_id, first_seen FROM runaway_jobs").fetchall() == \
        [("c1", "1", "2026-10-25T12:00:00")]
    hub.close()


# --- node alerts sent by the hub -------------------------------------------------------------

HUB = {"hub": {"sites": [{"name": "c1"}, {"name": "c2", "alerts": "hub"}, {"name": "c3", "alerts": "HUB"}]},
       "alerts": {"reminder_hours": 24}}


def _readings(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE node_state (source_site TEXT, node_name TEXT, state TEXT, reason TEXT, "
                 "timestamp TEXT, is_healthy INTEGER)")
    conn.executemany("INSERT INTO node_state VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _fresh(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


ROWS = [
    ("c2", "n1", "DOWN+NOT_RESPONDING", "Not responding", _fresh(10), 0),
    ("c2", "n2", "IDLE", None, _fresh(10), 1),
    ("c2", "n3", "MIXED+DRAIN", "disk", _fresh(10), 0),
    ("c2", "n1", "IDLE", None, _fresh(70), 1),                         # an older reading: not this one
    ("c1", "x1", "DOWN", "gone", _fresh(10), 0),                      # c1 sends its own
    ("c3", "m1", "DOWN", "gone", _fresh(60 * 5), 0),                  # c3 not read for 5 hours
]


def test_only_sites_set_to_hub_and_each_unhealthy_node_of_its_latest_reading(tmp_path):
    from nomad.alerts.hub_nodes import due_node_alerts, hub_alert_sites
    assert hub_alert_sites(HUB) == ["c2", "c3"] and hub_alert_sites({}) == []
    _readings(tmp_path / "combined.db", ROWS)
    conn = sqlite3.connect(tmp_path / "combined.db")
    items, skipped = due_node_alerts(conn, hub_alert_sites(HUB), HUB, NOW)
    assert [(i["key"], i["severity"]) for i in items] == [("node:c2:n1", "critical"), ("node:c2:n3", "warning")]
    assert skipped == {"c3"}
    assert items[0]["details"]["sent_by"] == "hub" and "c2" in items[0]["message"]
    off = dict(HUB, alerts={"node_state": {"enabled": False}})
    assert due_node_alerts(conn, ["c2"], off, NOW) == ([], set())


def test_sent_once_again_when_worse_kept_while_a_site_is_not_read(tmp_path, monkeypatch):
    from nomad.alerts import dispatcher
    from nomad.alerts.hub_nodes import STAMP, hub_node_alerts
    sent = []
    monkeypatch.setattr(dispatcher, "send_alert",
                        lambda severity, source, message, **kw: sent.append((severity, kw["host"])) or {"email": True})
    db = tmp_path / "combined.db"
    _readings(db, ROWS)
    hub_node_alerts(db, tmp_path, HUB, NOW)
    assert sent == [("critical", "n1"), ("warning", "n3")]
    hub_node_alerts(db, tmp_path, HUB, NOW + timedelta(minutes=10))
    assert len(sent) == 2                                             # not news again
    # A new reading: n3 goes down, worse, so sent again; n1 still down, not news.
    later = NOW + timedelta(minutes=20)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO node_state VALUES ('c2', ?, ?, 'x', ?, ?)",
                     [("n1", "DOWN", later.isoformat(), 0), ("n2", "IDLE", later.isoformat(), 1),
                      ("n3", "DOWN", later.isoformat(), 0)])
    conn.commit()
    conn.close()
    hub_node_alerts(db, tmp_path, HUB, later)
    assert sent[2:] == [("critical", "n3")]
    # c2 not read for hours (its pull fails): nothing sent, what was sent kept ...
    hub_node_alerts(db, tmp_path, HUB, later + timedelta(hours=5), failing={"c2"})
    kept = json.loads((tmp_path / STAMP).read_text())
    assert len(sent) == 3 and {"node:c2:n1", "node:c2:n3"} <= set(kept)
    assert kept["node:c2:n3"]["severity"] == "critical"
    # ... so when it reports again with the same nodes down, that isn't news.
    back = later + timedelta(hours=5, minutes=5)
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO node_state VALUES ('c2', ?, 'DOWN', 'x', ?, 0)",
                     [("n1", back.isoformat()), ("n3", back.isoformat())])
    conn.commit()
    conn.close()
    hub_node_alerts(db, tmp_path, HUB, back)
    assert len(sent) == 3


def test_no_sites_set_to_hub_reads_nothing(tmp_path):
    from nomad.alerts.hub_nodes import hub_node_alerts
    assert hub_node_alerts(tmp_path / "missing.db", tmp_path, {"hub": {"sites": [{"name": "c1"}]}}, NOW) == []
    assert not (tmp_path / "missing.db").exists()


# --- which job of a reused number nomad's analysis takes -----------------------------------

def _hub_jobs(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_id TEXT, source_site TEXT, user_name TEXT, "
                 "state TEXT, partition TEXT, submit_time TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER, "
                 "req_mem_mb INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER)")
    rows = [("4711", "c1", "ann", "COMPLETED", "2026-10-01T10:00:00"),
            ("4711@2026-03-01T10:00:00", "c1", "ben", "FAILED", "2026-03-01T10:00:00"),
            ("4711", "c2", "cy", "COMPLETED", "2026-10-02T10:00:00")]
    for jid, site, user, state, sub in rows:
        conn.execute("INSERT INTO jobs (job_id, source_site, user_name, state, partition, submit_time, start_time, "
                     "end_time, req_cpus, req_mem_mb, req_time_seconds, runtime_seconds) "
                     "VALUES (?, ?, ?, ?, 'basic', ?, ?, ?, 4, 8192, 3600, 1800)",
                     (jid, site, user, state, sub, sub, sub))
    conn.commit()
    conn.close()


def test_the_analysis_takes_the_job_its_site_and_submit_time_name(tmp_path):
    from nomad.edu.explain import explain_job, load_job
    db = str(tmp_path / "combined.db")
    _hub_jobs(db)
    assert load_job(db, "4711", "c1", "2026-10-01T10:00:00")["user_name"] == "ann"
    assert load_job(db, "4711", "c1", "2000-01-01T00:00:00")["user_name"] == "ann"     # requeued since
    assert load_job(db, "4711@2026-03-01T10:00:00", "c1")["user_name"] == "ben"
    assert load_job(db, "4711", "c2")["user_name"] == "cy"
    with pytest.raises(ValueError, match="c1, c2"):
        load_job(db, "4711")
    assert load_job(db, "4711", None, "2026-10-02T10:00:00")["user_name"] == "cy"
    out = json.loads(explain_job("4711", db, "c2", show_progress=False, output_format="json",
                                 record=False, submit_time="2026-10-02T10:00:00"))
    assert (out["user"], out["site"], out["submit_time"]) == ("cy", "c2", "2026-10-02T10:00:00")


def test_the_hub_indexes_jobs_by_number():
    from nomad.cli import SYNC_INDEXES
    assert ("jobs", "job_id") in SYNC_INDEXES             # a job opened by its number, at any site


# --- runaway jobs, as the collector finds them -------------------------------------------------

@pytest.fixture
def slurm_site(tmp_path, monkeypatch):
    """A site's database and a fake Slurm (tests/test_slurm_settle.py's),
    squeue's submit time the same as sacct's."""
    from pathlib import Path
    from types import SimpleNamespace

    import tests.test_slurm_settle as T
    from nomad.collectors import slurm as S
    from nomad.db.migrations import ensure_database
    monkeypatch.setattr(T, "SQ", T.SQ.replace("2026-10-05T10:00:00|", "2026-10-05T09:00:00|", 1))
    monkeypatch.setenv("TZ", "America/New_York")
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO config (key, value) VALUES (?, 'x')", (S._STARTS_REPAIRED,))

    def run(**fake):
        f = T.FakeSlurm(**fake)
        monkeypatch.setattr(S.subprocess, "run", f)
        col = S.SlurmCollector({}, str(db))
        col.store(col.collect())
        return f
    return SimpleNamespace(db=str(db), run=run)


def _runaway_rows(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT job_id, state FROM runaway_jobs ORDER BY job_id").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


RUN = ("RUNNING", "Unknown", "0:0")


def test_a_job_slurm_lost_but_its_accounting_keeps_returning_is_a_runaway(slurm_site):
    slurm_site.run(queue=[("900", "RUNNING")], history=[("900", *RUN)], known={"900": RUN})
    # slurmctld lost it; slurmdbd never closed it, so every pull returns it.
    for _ in range(2):
        slurm_site.run(queue=[("901", "RUNNING")], history=[("900", *RUN), ("901", *RUN)], known={"900": RUN})
        assert _runaway_rows(slurm_site.db) == [("900", "RUNNING")]


def test_one_the_pull_leaves_out_is_found_by_looking_it_up(slurm_site):
    slurm_site.run(queue=[("900", "RUNNING")], known={"900": RUN})
    slurm_site.run(queue=[("901", "RUNNING")], history=[("901", *RUN)], known={"900": RUN})
    assert _runaway_rows(slurm_site.db) == [("900", "RUNNING")]


def test_a_runaway_an_admin_fixed_is_cleared(slurm_site):
    slurm_site.run(queue=[("900", "RUNNING")], history=[("900", *RUN)], known={"900": RUN})
    slurm_site.run(queue=[("901", "RUNNING")], history=[("900", *RUN), ("901", *RUN)], known={"900": RUN})
    assert _runaway_rows(slurm_site.db) == [("900", "RUNNING")]
    # sacctmgr show runawayjobs, answered y: the pull now has it ended.
    slurm_site.run(queue=[("901", "RUNNING")],
                   history=[("901", *RUN), ("900", "COMPLETED", "2026-10-06T00:00:00", "0:0")], known={})
    assert _runaway_rows(slurm_site.db) == []


def test_a_job_back_in_the_queue_is_no_runaway(slurm_site):
    slurm_site.run(queue=[("901", "RUNNING")], history=[("900", *RUN), ("901", *RUN)], known={"900": RUN})
    assert _runaway_rows(slurm_site.db) == [("900", "RUNNING")]
    slurm_site.run(queue=[("900", "RUNNING"), ("901", "RUNNING")], history=[("900", *RUN), ("901", *RUN)])
    assert _runaway_rows(slurm_site.db) == []


def test_no_runaway_is_called_when_squeue_did_not_answer(slurm_site):
    slurm_site.run(squeue_ok=False, history=[("900", *RUN)], known={"900": RUN})
    assert not _runaway_rows(slurm_site.db)


# --- the hub's node alerts through the real dispatcher -----------------------------------------

class _Hook:
    """A webhook backend that records what it is given (ok: what it answers)."""
    sent = []
    ok = True

    def __init__(self, cfg):
        pass

    def send(self, alert):
        _Hook.sent.append(alert)
        return _Hook.ok


@pytest.fixture
def hub_alerts(tmp_path, monkeypatch):
    from nomad.alerts import dispatcher as D
    _Hook.sent, _Hook.ok = [], True
    monkeypatch.setattr(D, "WebhookBackend", _Hook)
    c = sqlite3.connect(tmp_path / "hub_own.db")
    c.execute("""CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, severity TEXT NOT NULL,
        category TEXT NOT NULL, source TEXT, message TEXT NOT NULL, details TEXT,
        resolved BOOLEAN DEFAULT FALSE, dedup_key TEXT)""")
    c.execute(D.ALERT_STATE_SQL)
    c.commit()
    c.close()
    config = {"cluster": {"name": "thehub"},
              "database": {"path": "hub_own.db"}, "general": {"data_dir": str(tmp_path)},
              "alerts": {"webhook": {"enabled": True, "url": "https://example.invalid/x"}},
              "hub": {"sites": [{"name": "c2", "alerts": "hub"}, {"name": "c3", "alerts": "hub"}],
                      "quiet_node_alerts": ["c3:old*"]}}
    db = tmp_path / "combined.db"
    now = datetime.now().replace(microsecond=0)
    t = (now - timedelta(minutes=5)).isoformat()
    _readings(db, [("c2", "node01", "DOWN", "dead [jdoe@2026-10-01T09:00:00]", t, 0),
                   ("c3", "node01", "DOWN", "rogue process of user ann", t, 0),
                   ("c3", "old07", "DOWN", "retired", t, 0),
                   ("c3", "node02", "DRAIN", None, t, None)])     # no health mark: by its state
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    conn.execute("INSERT INTO group_membership VALUES ('ann', 'lab$')")
    conn.commit()
    conn.close()
    return db, tmp_path, config, now


def test_same_named_nodes_at_two_sites_are_two_alerts_each_under_its_site(hub_alerts):
    from nomad.alerts.hub_nodes import hub_node_alerts
    db, cache, config, now = hub_alerts
    hub_node_alerts(db, cache, config, now)
    got = sorted((a["site"], a["host"], a["severity"]) for a in _Hook.sent)
    assert got == [("c2", "node01", "critical"), ("c3", "node01", "critical"), ("c3", "node02", "warning")]
    texts = " ".join(a["message"] for a in _Hook.sent)
    assert "jdoe" not in texts and "ann" not in texts and "old07" not in texts    # setter, user, quiet node
    stored = sqlite3.connect(cache / "hub_own.db").execute("SELECT details FROM alerts").fetchall()
    assert all(json.loads(d)["site"] in ("c2", "c3") for (d,) in stored)        # not the hub's name


def test_a_failed_send_is_sent_when_the_dispatcher_retries_not_a_day_later(hub_alerts, monkeypatch):
    from nomad.alerts import dispatcher as D
    from nomad.alerts.hub_nodes import STAMP, hub_node_alerts
    db, cache, config, now = hub_alerts

    def stamp():
        p = cache / STAMP
        return json.loads(p.read_text()) if p.exists() else {}
    _Hook.ok = False
    hub_node_alerts(db, cache, config, now)                       # mail down: nothing reached anyone
    assert len(_Hook.sent) == 3 and stamp() == {}
    _Hook.ok = True
    hub_node_alerts(db, cache, config, now + timedelta(minutes=10))  # its retry not due: held, not "sent"
    assert len(_Hook.sent) == 3 and stamp() == {}

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.today() + timedelta(minutes=20)       # past the 15-minute retry
    monkeypatch.setattr(D, "datetime", Later)
    hub_node_alerts(db, cache, config, now + timedelta(minutes=20))
    assert len(_Hook.sent) == 6 and len(stamp()) == 3


def test_a_link_to_a_job_since_moved_aside_still_opens_it(tmp_path):
    from nomad.edu.explain import load_job
    db = str(tmp_path / "combined.db")
    _hub_jobs(db)
    # The link named 4711 at c1 submitted in March; nomad has since moved that
    # job aside for a new 4711.
    assert load_job(db, "4711", "c1", "2026-03-01T10:00:00")["user_name"] == "ben"


PEND = ("PENDING", "Unknown", "0:0")


def test_jobs_squeue_hides_from_this_account_are_not_runaways(slurm_site, monkeypatch):
    # PrivateData=jobs in slurm.conf, not in slurmdbd.conf: squeue shows the
    # collecting account (u900) only its own jobs, sacct everyone's.
    from nomad.collectors import slurm as S
    monkeypatch.setattr(S, "_running_account", lambda: "u900")      # scontrol can't say: by the queue
    for _ in range(2):
        slurm_site.run(queue=[("900", "RUNNING")], history=[("900", *RUN), ("901", *RUN), ("902", *PEND)])
    # 902 waits as far as anyone can tell; 901 started weeks ago with a day's
    # limit (the fake's times), so it can't still run: that one is.
    assert _runaway_rows(slurm_site.db) == [("901", "RUNNING")]


def test_a_pending_array_range_squeue_cuts_short_is_no_runaway(slurm_site):
    full = "4567890_[12-50,52,54,56-100,200%10]"
    for _ in range(2):
        slurm_site.run(queue=[(full[:31], "PENDING"), ("901", "RUNNING")],
                       history=[(full, *PEND), ("901", *RUN)])
    assert _runaway_rows(slurm_site.db) in (None, [])


def test_hub_node_alerts_have_their_own_quiet_list(hub_alerts):
    from nomad.alerts.hub_nodes import hub_node_alerts
    db, cache, config, now = hub_alerts
    config = dict(config, hub=dict(config["hub"], quiet_nodes=["c2:*"], quiet_node_alerts=["c3:node02"]))
    hub_node_alerts(db, cache, config, now)
    got = sorted((a["site"], a["host"]) for a in _Hook.sent)
    assert got == [("c2", "node01"), ("c3", "node01"), ("c3", "old07")]   # quiet_nodes: GPU checks only


def _with_config(text):
    """FakeSlurm, with `scontrol show config` answering text."""
    import tests.test_slurm_settle as T
    from types import SimpleNamespace

    class Fake(T.FakeSlurm):
        def __call__(self, argv, **kw):
            if argv[0] == "scontrol":
                return SimpleNamespace(returncode=0, stdout=text, stderr="")
            return super().__call__(argv, **kw)
    return Fake


def test_with_private_jobs_slurms_config_says_squeue_hides_them(slurm_site, monkeypatch):
    # The collecting account (u901's job shows in squeue: a coordinator, say)
    # isn't root or SlurmUser, and PrivateData has jobs: not trusted.
    import tests.test_slurm_settle as T
    from nomad.collectors import slurm as S
    monkeypatch.setattr(T, "FakeSlurm", _with_config("PrivateData = jobs,usage\nSlurmUser = slurm(64030)\n"))
    monkeypatch.setattr(S, "_running_account", lambda: "monitor")
    for _ in range(2):
        slurm_site.run(queue=[("901", "RUNNING")], history=[("901", *RUN), ("902", *PEND)])
    assert _runaway_rows(slurm_site.db) in (None, [])
    # read once a day, kept in config
    assert sqlite3.connect(slurm_site.db).execute(
        "SELECT value FROM config WHERE key = 'slurm.private_jobs'").fetchone()[0].startswith("yes ")


def test_without_private_jobs_an_empty_queue_still_finds_waiting_runaways(slurm_site, monkeypatch):
    import tests.test_slurm_settle as T
    from nomad.collectors import slurm as S
    monkeypatch.setattr(T, "FakeSlurm", _with_config("PrivateData = none\nSlurmUser = slurm(64030)\n"))
    monkeypatch.setattr(S, "_running_account", lambda: "monitor")
    slurm_site.run(queue=[], history=[("902", *PEND)], known={"902": PEND})
    assert _runaway_rows(slurm_site.db) == [("902", "PENDING")]


def test_the_lookups_are_guarded_too(slurm_site, monkeypatch):
    # The 7-day pull fails one run while sacct -j answers: hidden jobs looked
    # up are no runaways either.
    import tests.test_slurm_settle as T
    from nomad.collectors import slurm as S
    monkeypatch.setattr(T, "FakeSlurm", _with_config("PrivateData = jobs\nSlurmUser = slurm(1)\n"))
    monkeypatch.setattr(S, "_running_account", lambda: "monitor")
    slurm_site.run(queue=[("901", "RUNNING")], history=[("901", *RUN), ("902", *PEND)])
    f = T.FakeSlurm(queue=[("901", "RUNNING")], known={"902": PEND})
    orig = f.__call__

    def no_pull(argv, **kw):
        from types import SimpleNamespace
        if argv[0] == "sacct" and "-j" not in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="timeout")
        return orig(argv, **kw)
    monkeypatch.setattr(S.subprocess, "run", no_pull)
    col = S.SlurmCollector({}, slurm_site.db)
    col.store(col.collect())
    assert _runaway_rows(slurm_site.db) in (None, [])


def test_reading_slurms_config_leaves_the_database_unlocked_for_the_lookups(slurm_site, monkeypatch):
    import tests.test_slurm_settle as T
    from nomad.collectors import slurm as S
    monkeypatch.setattr(T, "FakeSlurm", _with_config("PrivateData = none\nSlurmUser = slurm(1)\n"))
    monkeypatch.setattr(S, "_running_account", lambda: "monitor")
    slurm_site.run(queue=[("900", "RUNNING")], history=[("900", *RUN)], known={"900": RUN})
    locked = []

    def try_write():
        other = sqlite3.connect(slurm_site.db, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        except sqlite3.OperationalError as e:
            locked.append(str(e))
        finally:
            other.close()
    f = T.FakeSlurm(queue=[("901", "RUNNING")], known={"900": RUN}, during_lookup=try_write)
    monkeypatch.setattr(S.subprocess, "run", f)
    c = sqlite3.connect(slurm_site.db)
    c.execute("DELETE FROM config WHERE key = 'slurm.private_jobs'")     # read again this run
    c.commit()
    c.close()
    col = S.SlurmCollector({}, slurm_site.db)
    col.store(col.collect())
    assert f.looked_up and locked == []
