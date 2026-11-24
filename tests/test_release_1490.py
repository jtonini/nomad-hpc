"""nomad 1.49.0: alerts that say what is real and when it matters.

* The CPU clock of a busy host is judged against busy readings (hpc1's
  Xeon 6230Rs: 3,970 MHz idle, about 2,700 with every core at work, dips to
  1,420 on heavy vector code: 24 alerts in a week, none a brake).
* The dispatcher waits before it mails what may pass ([alerts.settle]) and
  mails what keeps coming back (the third time in 7 days), keeping a record
  of every episode (alert_episodes).
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.collectors import clock as C

XEON = "Intel(R) Xeon(R) Gold 6230R CPU @ 2.10GHz"
RULE = dict(C.DEFAULTS)


def reading(mhz, load1, model=XEON, cpus=52, max_mhz=4000, **kw):
    return {"node": "nd52", "model": model, "cpus": cpus, "load1": load1, "mhz": mhz, "max_mhz": max_mhz,
            "sha_mbps": None, **kw}


def test_a_busy_host_is_judged_against_busy_readings():
    busy_past = [reading(m, 50.0) for m in (2600, 2650, 2700, 2750, 2690)]
    idle_past = [reading(3970, 0.5)] * 10
    # every core at work, on heavy vector code: below half its top clock, not below its busy clock
    j = C.judge(reading(1422, 49.5), idle_past + busy_past, [], RULE)
    assert not j["low"] and j["basis"] == "its usual clock this busy" and j["reference"] == 2690
    # what 1.48 said: low, against the top clock
    assert C.judge(reading(1422, 1.0), idle_past, [], RULE)["low"]
    # a brake is one at any load
    j = C.judge(reading(400, 49.5), idle_past + busy_past, [], RULE)
    assert j["low"] and j["problem"].startswith("its CPU runs at 400 MHz under load (0.95 per CPU), 15% of 2690 MHz")
    # idle, the top clock as before
    j = C.judge(reading(3973, 0.4), idle_past + busy_past, [], RULE)
    assert not j["low"] and j["basis"] == "its CPU's top clock"


def test_busy_with_no_busy_past():
    peers = [reading(2700, 51.0, node="nd09"), reading(2800, 50.0, node="nd10"), reading(3970, 0.1)]
    j = C.judge(reading(1500, 50.0), [], peers, RULE)
    assert not j["low"] and j["basis"] == "2 busy hosts with its CPU" and j["reference"] == 2750
    j = C.judge(reading(1500, 50.0), [], [], RULE)
    assert not j["low"] and j["basis"] == "its CPU's rated clock" and j["reference"] == 2100
    assert C.judge(reading(600, 50.0), [], [], RULE)["low"]
    epyc = "AMD EPYC 9554 64-Core Processor"
    j = C.judge(reading(1000, 60.0, model=epyc, cpus=64, max_mhz=3764), [], [], RULE)
    assert not j["low"] and j["basis"] == "60% of its CPU's top clock"
    assert C.judge(reading(800, 60.0, model=epyc, cpus=64, max_mhz=3764), [], [], RULE)["low"]
    # [alerts.clock] busy_min_fraction, its own
    assert C.rules({"busy_min_fraction": 0.6}, "nd52")["busy_min_fraction"] == 0.6
    assert C.judge(reading(1500, 50.0), [], [], dict(RULE, busy_min_fraction=0.75))["low"]


def test_the_collector_keeps_load_with_its_readings(tmp_path, monkeypatch):
    sent = []
    import nomad.alerts as A
    monkeypatch.setattr(A, "send_alert", lambda **k: sent.append(k) or {"email": True})
    col = C.ClockCollector({"slurm_nodes": True, "min_seconds": 0, "alert_rules": {}}, tmp_path / "site.db")
    t0 = datetime(2026, 11, 14, 0, 0)

    def read(i, mhz, load1):
        return dict(reading(mhz, load1), timestamp=(t0 + timedelta(minutes=15 * i)).isoformat(timespec="seconds"),
                    os_mhz=None, driver="intel_pstate", governor="performance", perf="ok", vm=0, sha_ni=0,
                    pkgs=2, mhz_pkgs=None)
    i = 0
    for mhz, load in [(3970, 0.2)] * 4 + [(2700, 50.0), (2650, 51.0), (2600, 49.0), (2720, 50.0)]:
        col.store([read(i, mhz, load)])
        i += 1
    for mhz in (1430, 1450, 1420):                 # three dips in a row, every core busy
        col.store([read(i, mhz, 50.5)])
        i += 1
    conn = sqlite3.connect(tmp_path / "site.db")
    assert conn.execute("SELECT SUM(low) FROM cpu_clock").fetchone()[0] == 0
    assert conn.execute("SELECT throttled, basis FROM cpu_clock_hosts").fetchone() == (0, "its usual clock this busy")
    assert not sent
    for mhz in (400, 410):                          # braked, busy
        col.store([read(i, mhz, 50.5)])
        i += 1
    assert conn.execute("SELECT throttled FROM cpu_clock_hosts").fetchone() == (1,)
    assert sent and "under load (0.97 per CPU)" in sent[-1]["message"]
    conn.close()


# ── The dispatcher: wait for what may pass, mail what comes back ──

from nomad.alerts import dispatcher as D  # noqa: E402

T0 = datetime(2026, 11, 14, 5, 55)


@pytest.fixture
def at(monkeypatch):
    box = {"now": T0}

    class Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return box["now"]
    monkeypatch.setattr(D, "datetime", Fake)
    return box


class Mailbox:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(dict(alert))
        return True


def run(tmp_path, mail, cfg=None, **alert):
    d = D.AlertDispatcher({"database": {"path": "site.db"}, "general": {"data_dir": str(tmp_path)},
                           "alerts": cfg or {}})
    d.backends = [mail]
    a = {"severity": "warning", "source": "clock", "host": "nd52", "subject": "cpu clock",
         "message": "nd52: its CPU runs at 1432 MHz busy.", "details": {"episode_gap_minutes": 60}}
    a.update(alert)
    return d.dispatch(a)


def episodes(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    try:
        return c.execute("SELECT source, host, first_seen, last_seen, raised_count, wait_minutes "
                         "FROM alert_episodes ORDER BY id").fetchall()
    finally:
        c.close()


def test_a_warning_waits_its_sources_wait(tmp_path, at):
    mail = Mailbox()
    for minutes in (0, 15, 30, 45):
        at["now"] = T0 + timedelta(minutes=minutes)
        assert isinstance(run(tmp_path, mail), D.Held)
    assert not mail.sent
    at["now"] = T0 + timedelta(minutes=60)
    run(tmp_path, mail)
    assert len(mail.sent) == 1                       # it lasted the hour: mailed
    at["now"] = T0 + timedelta(minutes=75)
    assert isinstance(run(tmp_path, mail), D.Held)   # then reminders, as before
    (src, host, first, last, raised, wait), = episodes(tmp_path)
    assert (src, host, raised, wait) == ("clock", "nd52", 1, 60.0) and first == T0.isoformat()


def test_what_passes_within_its_wait_is_kept_not_mailed(tmp_path, at):
    mail = Mailbox()
    run(tmp_path, mail)
    at["now"] = T0 + timedelta(minutes=15)
    run(tmp_path, mail)
    assert not mail.sent
    (_, _, first, last, raised, _), = episodes(tmp_path)
    assert raised == 0 and last == (T0 + timedelta(minutes=15)).isoformat()


def test_critical_and_sources_without_a_wait_go_at_once(tmp_path, at):
    mail = Mailbox()
    run(tmp_path, mail, severity="critical")
    run(tmp_path, mail, source="disk", subject="/scratch", message="Disk /scratch at 87%")
    run(tmp_path, mail, source="policy_watch", subject="listening:f3", message="labhead1: a program")
    assert [a["source"] for a in mail.sent] == ["clock", "disk", "policy_watch"]
    held = run(tmp_path, mail, source="policy_watch", subject="heavy:f9", message="hpc1: heavy use")
    assert isinstance(held, D.Held) and len(mail.sent) == 3


def test_alerts_wait_in_nomad_toml(tmp_path, at):
    mail = Mailbox()
    run(tmp_path, mail, {"wait": {"clock": 0}})
    assert len(mail.sent) == 1
    run(tmp_path, mail, {"wait": {"policy_watch": {"heavy": 0}, "disk": 30}}, source="policy_watch",
        subject="heavy:f9", message="hpc1: heavy use")
    assert len(mail.sent) == 2
    assert isinstance(run(tmp_path, mail, {"wait": {"disk": 30}}, source="disk", subject="/home",
                          message="Disk /home at 81%"), D.Held)
    assert D.waits({"clock": -5, "x": "y", "default": 10}) == {"clock": 60.0, "policy_watch.heavy": 30.0,
                                                              "default": 10.0}


def test_the_third_time_in_seven_days_is_mailed_once(tmp_path, at):
    mail = Mailbox()
    starts = [T0, T0 + timedelta(hours=6), T0 + timedelta(days=1), T0 + timedelta(days=2)]
    for start in starts:                             # each 30 min, then gone for hours
        for m in (0, 15, 30):
            at["now"] = start + timedelta(minutes=m)
            run(tmp_path, mail)
    assert len(mail.sent) == 1                       # the third, at its start; the fourth: not again
    msg = mail.sent[0]["message"]
    assert msg.startswith("nd52: its CPU runs at 1432 MHz busy. The 3rd time in 7 days (before: Sat 14 05:55, "
                          "30 min; Sat 14 11:55, 30 min).")
    assert mail.sent[0]["details"]["recurring"] == 3
    assert [e[4] for e in episodes(tmp_path)] == [0, 0, 1, 0]
    # a week later, a third again: mailed again
    for d in (8, 8.5, 9):
        for m in (0, 15):
            at["now"] = T0 + timedelta(days=d, minutes=m)
            run(tmp_path, mail)
    assert len(mail.sent) == 2 and "The 3rd time in 7 days" in mail.sent[1]["message"]


def test_recurrence_off(tmp_path, at):
    mail = Mailbox()
    for h in (0, 3, 6, 9):
        at["now"] = T0 + timedelta(hours=h)
        run(tmp_path, mail, {"recur_times": 0})
    assert not mail.sent and len(episodes(tmp_path)) == 4
    assert D.recurrence_words([("2026-11-14T05:55:00", "2026-11-14T05:55:00")] * 7, "x", 7).startswith(
        "The 8th time in 7 days (before: Sat 14 05:55, one reading; ") and "; and 2 more)." in \
        D.recurrence_words([("2026-11-14T05:55:00", "2026-11-14T05:55:00")] * 7, "x", 7)


# ── What a message says: the jobs, the use, where to see it ──

def test_a_clock_alert_names_the_jobs_it_slows(tmp_path):
    conn = sqlite3.connect(tmp_path / "site.db")
    conn.execute("CREATE TABLE jobs (job_id TEXT, node_list TEXT, state TEXT, start_time TEXT, end_time TEXT, "
                 "source_site TEXT)")
    now = datetime(2026, 11, 14, 6, 10)
    for jid, nodes, state, start, end in (
            ("4521", "nd[51-52]", "RUNNING", now - timedelta(hours=2), None),
            ("4519", "nd52", "RUNNING", now - timedelta(hours=5), None),
            ("4400", "nd52", "COMPLETED", now - timedelta(days=1), now - timedelta(hours=20)),
            ("4522", "nd01", "RUNNING", now - timedelta(hours=1), None)):
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, 'hpc1')",
                     (jid, nodes, state, start.isoformat(), end.isoformat() if end else None))
    assert C.jobs_on(conn, "nd52", now=now) == ["4521", "4519"]
    assert C.jobs_on(conn, "nd52", "hpc3", now=now) == []
    host = {"problem": "its CPU runs at 400 MHz under load (0.95 per CPU), 15% of 2690 MHz", "low_since": "x"}
    it = C.alert_item("nd52", host, [], None, jobs=C.jobs_on(conn, "nd52", now=now))
    assert "since x. Running there: 2 jobs (4521, 4519). Drain it" in it["message"]
    assert it["details"]["jobs"] == ["4521", "4519"]
    assert C.jobs_words([str(i) for i in range(10)]).endswith("(0, 1, 2, 3, 4, 5, 6, 7 and 2 more).")
    conn.close()


def test_the_console_link_in_an_alert(tmp_path, at):
    mail = Mailbox()
    run(tmp_path, mail, {"wait": {"clock": 0}, "link": "https://nomad.example.edu/"}, site="hpc1")
    assert mail.sent[0]["message"].endswith(
        "In the Console: https://nomad.example.edu/nodes/timeline?site=hpc1&node=nd52")
    # [notices] link on a hub; Policy watch's page; nothing without either
    d = D.AlertDispatcher({"notices": {"link": "https://nomad.example.edu"}})
    assert d.link == "https://nomad.example.edu"
    assert D.console_link(d.link, {"source": "policy_watch"}) == "https://nomad.example.edu/watch"
    assert D.console_link(d.link, {"source": "disk"}) == "https://nomad.example.edu/alerts"
    assert D.console_link(d.link, {"source": "clock", "host": "node3", "site": "hpc3/node3",
                                   "details": {"cluster": "hpc3"}}).endswith("site=hpc3&node=node3")
    assert D.AlertDispatcher({"alerts": {"link": "javascript:x"}}).link is None


def test_heavy_use_says_how_much():
    from nomad.collectors import policy_watch as P
    assert P.heavy_note({"cores": 4, "rss_mb": 2355}) == "~4 cores, sustained; 2.3 GB"


def test_a_jobs_note_names_its_jobs():
    from nomad import notices as N

    class Issue:
        affected_jobs = 12
        jobs = [{"job_id": 4500 + k} for k in range(10)]
    # 1.62.0: the words alone (the email puts them on a "Jobs:" line); arrays: test_release_1620
    assert N._ids(Issue()) == "4500, 4501, 4502, 4503, 4504, 4505, 4506, 4507 and 4 more"
    Issue.jobs, Issue.affected_jobs = [{"job_id": "77"}], 1
    assert N._ids(Issue()) == "77"
    Issue.jobs = []
    assert N._ids(Issue()) == ""
