# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What nomad keeps about an interactive server (RStudio, JupyterHub), for a
course's instructor and for the usage report: who joined a group when, each
person's sessions day by day, the processes the kernel killed for want of
memory, and per_user's rules for a server where heavy use is the point."""
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.collectors import interactive_daily as D
from nomad.collectors.groups import GroupCollector
from nomad.collectors.oom import OOMCollector, parse

T0 = datetime(2026, 10, 12, 9, 0)


# --- group membership: when someone joined ------------------------------------

GROUPS = {"getent group": "demo101:x:5001:ann,bob\nother:x:5002:ann"}
PASSWD = ("ann:x:1001:1001::/home/ann:/bin/bash\nbob:x:1002:1002::/home/bob:/bin/bash\n"
          "cy:x:1003:1003::/home/cy:/bin/bash")


def _collector(db, groups):
    c = GroupCollector({"clusters": {}, "local_name": "web1"}, db)

    def run_cmd(cmd, *a, **k):
        if cmd == "getent group":
            return groups
        if isinstance(cmd, list) and cmd[:3] == ["getent", "passwd", "--"]:
            return PASSWD
        return None
    c._run_cmd = run_cmd
    return c


def _first_seen(db):
    with sqlite3.connect(db) as conn:
        return {(u, g): f for u, g, f in conn.execute(
            "SELECT username, group_name, first_seen FROM group_membership")}


def test_a_membership_remembers_when_it_was_first_seen(tmp_path):
    db = tmp_path / "n.db"
    c = _collector(db, "demo101:x:5001:ann,bob")
    c._now = lambda: T0
    c.store(c.collect())
    c._now = lambda: T0 + timedelta(days=3)
    c.groups_text = "demo101:x:5001:ann,bob,cy"
    c = _collector(db, "demo101:x:5001:ann,bob,cy")
    c._now = lambda: T0 + timedelta(days=3)
    c.store(c.collect())
    got = _first_seen(db)
    assert got[("ann", "demo101")] == T0.isoformat()          # kept, not moved
    assert got[("cy", "demo101")] == (T0 + timedelta(days=3)).isoformat()


def test_rejoining_after_a_gap_starts_over_but_a_stopped_collector_does_not(tmp_path):
    db = tmp_path / "n.db"
    for day, members in ((0, "ann,bob"), (1, "ann"), (10, "ann"), (11, "ann,bob")):
        c = _collector(db, f"demo101:x:5001:{members}")
        c._now = lambda d=day: T0 + timedelta(days=d)
        c.store(c.collect())
    got = _first_seen(db)
    # bob was listed on day 0, then not while runs went on (days 1 to 10):
    # back on day 11 is a new membership (a later course).
    assert got[("bob", "demo101")] == (T0 + timedelta(days=11)).isoformat()
    # ann was listed throughout: the gap between day 1 and day 10 was the
    # collector not running, not ann leaving.
    assert got[("ann", "demo101")] == T0.isoformat()


def test_memberships_recorded_before_this_version_stay_unknown(tmp_path):
    db = tmp_path / "n.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE group_membership (username TEXT NOT NULL, group_name TEXT "
                     "NOT NULL, gid INTEGER, cluster TEXT NOT NULL, collected_at TIMESTAMP, "
                     "has_account INTEGER, account_missing_since TEXT, "
                     "PRIMARY KEY (username, group_name, cluster))")
        conn.execute("INSERT INTO group_membership VALUES ('ann', 'demo101', 5001, 'web1', "
                     "?, 1, NULL)", ((T0 - timedelta(hours=1)).isoformat(),))
    c = _collector(db, "demo101:x:5001:ann,bob")
    c._now = lambda: T0
    c.store(c.collect())
    got = _first_seen(db)
    assert got[("ann", "demo101")] is None                     # since before nomad looked
    assert got[("bob", "demo101")] == T0.isoformat()


# --- sessions, day by day -------------------------------------------------------

def _s(user, kind, pid, start, mem, ticks, age_hours=5.0):
    return {"user": user, "session_type": kind, "pid": pid, "start_time": start,
            "mem_mb": mem, "cpu_ticks": ticks, "age_hours": age_hours}


def U(conn, sessions, when):
    """One run at local time ``when``, the clock agreeing with it."""
    return D.update(conn, sessions, when, "web1", epoch=when.timestamp())


def _daily(conn):
    return {(r[0], r[1], r[2]): r[3:] for r in conn.execute(
        "SELECT day, user, session_type, sessions, session_seconds, busy_seconds, "
        "cpu_measured_seconds, cpu_seconds, mem_mb_seconds, peak_mem_mb FROM interactive_daily")}


def test_daily_totals_add_up_time_cpu_and_memory():
    conn = sqlite3.connect(":memory:")
    a = _s("ann", "RStudio", 10, "t", 1000, 0)
    U(conn, [a], T0)                    # first sight: counted, no time yet
    U(conn, [dict(a, cpu_ticks=300 * 100 * 0.5)], T0 + timedelta(minutes=5))
    U(conn, [dict(a, cpu_ticks=300 * 100 * 0.5, mem_mb=3000)],
      T0 + timedelta(minutes=10))                     # no CPU in this interval: idle
    row = _daily(conn)[("2026-10-12", "ann", "RStudio")]
    sessions, seconds, busy, measured, cpu, mem, peak = row
    assert sessions == 1 and seconds == 600
    assert busy == 300 and measured == 600 and cpu == pytest.approx(150)
    assert mem == pytest.approx(1000 * 300 + 3000 * 300) and peak == 3000


def test_a_gap_adds_no_time_and_a_new_day_counts_the_session_again():
    conn = sqlite3.connect(":memory:")
    a = _s("ann", "Jupyter (Python)", 11, "t", 500, 100)
    U(conn, [a], T0)
    U(conn, [a], T0 + timedelta(hours=3))          # collector was stopped
    assert _daily(conn)[("2026-10-12", "ann", "Jupyter (Python)")][1] == 0
    U(conn, [a], T0 + timedelta(hours=15, minutes=1))     # 00:01 next day: a gap too
    U(conn, [a], T0 + timedelta(hours=15, minutes=6))
    d = _daily(conn)
    assert d[("2026-10-12", "ann", "Jupyter (Python)")][0] == 1
    assert d[("2026-10-13", "ann", "Jupyter (Python)")][0] == 1 and \
        d[("2026-10-13", "ann", "Jupyter (Python)")][1] == 300


def test_a_session_new_since_the_last_run_counts_its_cpu_since_it_started():
    conn = sqlite3.connect(":memory:")
    U(conn, [], T0)                                 # a run with nobody on
    new = _s("bob", "RStudio", 12, "t2", 200, 60 * 100, age_hours=2 / 60)
    old = _s("cy", "RStudio", 13, "t3", 200, 999999, age_hours=30)  # seen for the first time
    U(conn, [new, old], T0 + timedelta(minutes=5))
    d = _daily(conn)
    assert d[("2026-10-12", "bob", "RStudio")][4] == pytest.approx(60)   # cpu_seconds
    assert d[("2026-10-12", "bob", "RStudio")][2] == 300                 # busy
    # Older than the interval: its CPU here is not known, so no busy guess.
    assert d[("2026-10-12", "cy", "RStudio")][3] == 0 and d[("2026-10-12", "cy", "RStudio")][2] == 0


def test_a_person_with_several_sessions_is_counted_once_in_any():
    conn = sqlite3.connect(":memory:")
    day = "2026-10-12"
    server = _s("ann", "Jupyter Server", 20, "t", 100, 0)
    k1 = _s("ann", "Jupyter (Python)", 21, "t", 2000, 0)
    k2 = _s("ann", "Jupyter (Python)", 22, "t", 1000, 0)
    U(conn, [server, k1, k2], T0)
    U(conn, [server, dict(k1, cpu_ticks=300 * 100), k2], T0 + timedelta(minutes=5))
    d = _daily(conn)
    py, anyone = d[(day, "ann", "Jupyter (Python)")], d[(day, "ann", D.ANY)]
    # By type: two kernels open five minutes are ten session-minutes.
    assert py[0] == 2 and py[1] == 600 and py[2] == 300 and py[6] == 2000
    # The person: on for five minutes, busy for five (one kernel at 100%),
    # holding all three sessions' memory at once.
    assert anyone[0] == 3 and anyone[1] == 300 and anyone[2] == 300
    assert anyone[4] == pytest.approx(300) and anyone[6] == 3100
    assert anyone[5] == pytest.approx(3100 * 300)


def test_the_change_from_daylight_time_adds_no_hour():
    conn = sqlite3.connect(":memory:")
    a = _s("ann", "RStudio", 10, "t", 1000, 0)
    e = 1_793_500_000.0
    # 01:58 daylight time, then 01:03 standard time: five minutes later.
    D.update(conn, [a], datetime(2026, 11, 1, 1, 58), "web1", epoch=e)
    D.update(conn, [a], datetime(2026, 11, 1, 1, 3), "web1", epoch=e + 300)
    assert _daily(conn)[("2026-11-01", "ann", "RStudio")][1] == 300
    # A clock set back adds nothing, rather than a negative interval.
    D.update(conn, [a], datetime(2026, 11, 1, 1, 8), "web1", epoch=e + 100)
    assert _daily(conn)[("2026-11-01", "ann", "RStudio")][1] == 300


def test_a_session_started_minutes_ago_keeps_its_first_cpu():
    conn = sqlite3.connect(":memory:")
    U(conn, [], T0)
    # Four minutes old: ps's age, rounded to a tenth of an hour, says six.
    s = dict(_s("bob", "RStudio", 12, "t2", 200, 120 * 100, age_hours=0.1), age_seconds=240.0)
    U(conn, [s], T0 + timedelta(minutes=5))
    assert _daily(conn)[("2026-10-12", "bob", "RStudio")][4] == pytest.approx(120)


def _fake_processes(monkeypatch, ps):
    import time

    import nomad.collectors.interactive as interactive
    monkeypatch.setattr(interactive.subprocess, "check_output", lambda *a, **k: ps)
    monkeypatch.setattr(interactive, "_stat_fields", lambda pid: ["x"])
    monkeypatch.setattr(interactive, "_start",
                        lambda fields: ("2026-10-12T08:50:00", time.time() - 600))
    monkeypatch.setattr(interactive, "get_process_memory",
                        lambda pid: {"rss_mb": 100.0, "vms_mb": 200.0})
    # Own CPU: the pid; children it waited for: 1000 more.
    monkeypatch.setattr(interactive, "get_process_cpu_ticks",
                        lambda pid, children=True: float(pid) + (1000 if children else 0))
    monkeypatch.setattr(interactive, "user_name", lambda uid: {"1001": "ann", "1002": "bob"}[uid])
    return interactive


def test_a_session_is_a_process_tree(monkeypatch):
    ps = "\n".join([
        " 1001   100     1  5.0  1.0 /usr/lib/rstudio-server/bin/rsession -u ann",
        # A worker R forked (mclapply) keeps its parent's command line.
        " 1001   101   100 90.0  2.0 /usr/lib/rstudio-server/bin/rsession -u ann",
        " 1001   200     1  0.1  0.5 /opt/jh/bin/python /opt/jh/bin/jupyterhub-singleuser --ip=",
        " 1001   201   200 50.0  3.0 /opt/jh/bin/python -m ipykernel_launcher -f k1.json",
        " 1001   202   200  0.0  1.0 /opt/jh/bin/python -m ipykernel_launcher -f k2.json",
        " 1001   203   201 50.0  1.0 /opt/jh/bin/python -m ipykernel_launcher -f k1.json",
        # Someone else's, under ann's: their own session.
        " 1002   300   100  1.0  1.0 /usr/lib/rstudio-server/bin/rsession -u bob",
        " 1001   400     1  0.0  0.0 pip install ipykernel",
    ])
    interactive = _fake_processes(monkeypatch, ps)
    got = {s["pid"]: s for s in interactive.collect_sessions("web1")}
    assert not interactive.LAST_FAILED
    assert sorted(got) == [100, 200, 201, 202, 300]
    r = got[100]
    assert (r["user"], r["session_type"], r["processes"]) == ("ann", "RStudio", 2)
    assert r["mem_mb"] == 200 and r["cpu_ticks"] == 2201 and r["cpu_percent"] == 95
    k1 = got[201]
    assert (k1["session_type"], k1["processes"], k1["cpu_ticks"]) == ("Jupyter (Python)", 2, 2404)
    # A server's ended children are kernels, counted as theirs: not again here.
    assert got[200]["session_type"] == "Jupyter Server" and got[200]["processes"] == 1
    assert got[200]["cpu_ticks"] == 200
    assert got[300]["user"] == "bob"
    assert 590 <= r["age_seconds"] <= 660 and r["age_hours"] == 0.2


def test_the_collector_keeps_daily_totals_but_not_from_a_failed_look(tmp_path, monkeypatch):
    from nomad.db.migrations import ensure_database
    db = tmp_path / "n.db"
    ensure_database(db)
    interactive = _fake_processes(monkeypatch, "")
    c = interactive.InteractiveCollector({"server_id": "web1", "alerts_enabled": False}, db)

    def last_run():
        with sqlite3.connect(db) as conn:
            return conn.execute("SELECT cpu_ticks, seen_at FROM interactive_state "
                                "WHERE key = 'last_run'").fetchone()

    assert c.run().success                            # nobody on: the run is still marked
    first = last_run()
    assert first is not None

    def fail(*a, **k):
        raise OSError("ps: not found")
    monkeypatch.setattr(interactive.subprocess, "check_output", fail)
    r = c.run()
    assert r.success and interactive.LAST_FAILED and "ps failed" in (r.note or "")
    assert last_run() == first                         # not "nobody on"

    _fake_processes(monkeypatch, " 1001 100 1 5.0 1.0 /usr/lib/rstudio-server/bin/rsession -u ann")
    r = c.run()
    assert r.success and r.records_collected == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 1
        rows = dict(conn.execute("SELECT session_type, sessions FROM interactive_daily "
                                 "WHERE user = 'ann'").fetchall())
    assert rows == {"RStudio": 1, D.ANY: 1}


def test_raw_snapshots_are_pruned_only_when_asked():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE interactive_sessions (timestamp TEXT, user TEXT)")
    conn.executemany("INSERT INTO interactive_sessions VALUES (?, 'ann')",
                     [((T0 - timedelta(days=d)).isoformat(),) for d in (1, 40, 100)])
    assert D.prune(conn, 0, T0) == 0
    assert D.prune(conn, 30, T0) == 2
    assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 1


def test_session_kinds_and_long_names():
    from nomad.collectors.interactive import session_type, user_name
    assert session_type("/usr/lib/rstudio-server/bin/rsession -u ann") == "RStudio"
    assert session_type("python -m ipykernel_launcher -f k.json") == "Jupyter (Python)"
    assert session_type("/opt/jh/bin/python /opt/jh/bin/jupyterhub-singleuser --ip=") == \
        "Jupyter Server"
    assert session_type("python3 -m jupyterhub.singleuser") == "Jupyter Server"
    assert session_type("bash -l") is None
    assert session_type("pip install ipykernel") is None
    assert session_type("tail -f /var/log/rstudio/rsession.log") is None
    assert session_type("bash -c python -m ipykernel_launcher") is None
    assert session_type("vim jupyter-lab") is None
    assert session_type('python3 -c "import ipykernel"') is None
    # Interpreter options before the module or script.
    for line in ("/opt/py/bin/python3.12 -Xfrozen_modules=off -m ipykernel_launcher -f k.json",
                 "python3 -X frozen_modules=off -m ipykernel_launcher -f k.json",
                 "python3 -W ignore -Im ipykernel_launcher -f k.json",
                 "python3 -E -s /opt/py/lib/ipykernel_launcher.py -f k.json"):
        assert session_type(line) == "Jupyter (Python)", line
    assert session_type("/usr/bin/python3 -s /usr/bin/jupyter-notebook --no-browser") == \
        "Jupyter Server"
    assert session_type("/usr/lib/R/bin/exec/R --no-echo -e IRkernel::main() --args f.json") == \
        "Jupyter (R)"
    import os
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    assert user_name(str(os.getuid())) == me                 # the whole name, never "abcdefgh+"
    assert user_name("4000000000") == "4000000000"


# --- out-of-memory kills ---------------------------------------------------------

RHEL8 = [
    ("2026-10-01T14:03:11", "python3 invoked oom-killer: gfp_mask=0x6200ca(GFP_HIGHUSER_MOVABLE), "
                            "nodemask=(null), order=0, oom_score_adj=0"),
    ("2026-10-01T14:03:11", "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
                            "mems_allowed=0-1,global_oom,task_memcg=/user.slice/user-1234.slice/"
                            "session-c5.scope,task=python3,pid=55555,uid=1234"),
    ("2026-10-01T14:03:11", "Out of memory: Killed process 55555 (python3) total-vm:200000000kB, "
                            "anon-rss:190000000kB, file-rss:0kB, shmem-rss:0kB, UID:1234 "
                            "pgtables:380000kB oom_score_adj:0"),
    ("2026-10-01T14:03:12", "oom_reaper: reaped process 55555 (python3), now anon-rss:0kB, "
                            "file-rss:0kB, shmem-rss:0kB"),
]


def test_kills_are_read_in_every_wording():
    k, = parse(RHEL8, "web1")
    assert (k["pid"], k["process"], k["uid"], k["anon_rss_kb"], k["constraint_type"]) == \
        (55555, "python3", 1234, 190000000, "CONSTRAINT_NONE")
    assert k["memcg"].startswith("/user.slice/user-1234.slice")
    # Without UID: on the kill line: from the oom-kill line, else from the slice.
    no_uid = [RHEL8[1], ("2026-10-01T14:03:11", RHEL8[2][1].replace("UID:1234 ", ""))]
    assert parse(no_uid, "web1")[0]["uid"] == 1234
    slice_only = [("t", RHEL8[1][1].replace(",uid=1234", "")),
                  ("t", RHEL8[2][1].replace("UID:1234 ", ""))]
    assert parse(slice_only, "web1")[0]["uid"] == 1234
    old = [("2020-01-01T00:00:00", "Out of memory: Kill process 4444 (R) score 900 or sacrifice child"),
           ("2020-01-01T00:00:00", "Killed process 4444 (R) total-vm:9000kB, anon-rss:8000kB, "
                                   "file-rss:0kB, shmem-rss:0kB")]
    k, = parse(old, "web1")
    assert (k["pid"], k["process"], k["uid"], k["anon_rss_kb"]) == (4444, "R", None, 8000)
    cg = [("t", "Memory cgroup out of memory: Killed process 777 (rsession) total-vm:10kB, "
                "anon-rss:5kB, file-rss:0kB, shmem-rss:0kB, UID:2000 pgtables:1kB")]
    k, = parse(cg, "web1")
    assert (k["process"], k["uid"], k["constraint_type"]) == ("rsession", 2000, "CONSTRAINT_MEMCG")
    assert parse([("t", "eth0: link up")], "web1") == []


class _Journal:
    """journalctl -o json _TRANSPORT=kernel, from a list of (unix seconds,
    message)."""

    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    def __call__(self, args):
        import json
        import subprocess
        self.calls.append(args)
        start = 0
        for a in args:
            if a.startswith("--after-cursor="):
                start = int(a.split("=", 1)[1].split("-")[1]) + 1
        lines = [json.dumps({"__REALTIME_TIMESTAMP": str(int(t * 1e6)), "MESSAGE": m,
                             "__CURSOR": f"c-{i}"})
                 for i, (t, m) in enumerate(self.entries) if i >= start]
        return subprocess.CompletedProcess(args, 0, "\n".join(lines), "")


def test_the_collector_reads_on_from_where_it_stopped(tmp_path, monkeypatch):
    import nomad.collectors.oom as oom
    monkeypatch.setattr(oom, "find_tool", lambda name: "/usr/bin/journalctl")
    c = OOMCollector({}, tmp_path / "n.db")
    t = T0.timestamp()
    journal = _Journal([(t, m) for _, m in RHEL8])
    monkeypatch.setattr(c, "_journal", journal)
    first = c.run()
    assert first.success and first.records_collected == 1
    assert journal.calls[0] == ["--since=-30d"]
    with sqlite3.connect(tmp_path / "n.db") as conn:
        row = conn.execute("SELECT host, pid, process, uid FROM oom_kills").fetchone()
        assert row[1:] == (55555, "python3", 1234)
    journal.entries.append((t + 60, "eth0: link up"))
    second = c.run()
    assert second.success and second.records_collected == 0
    assert journal.calls[1] == ["--after-cursor=c-3"]
    third = c.run()
    assert journal.calls[2] == ["--after-cursor=c-4"]
    with sqlite3.connect(tmp_path / "n.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM oom_kills").fetchone()[0] == 1
    assert third.success


def test_a_kernel_log_it_cannot_read_is_said_once(tmp_path, monkeypatch):
    import subprocess

    import nomad.collectors.oom as oom
    monkeypatch.setattr(oom, "find_tool", lambda name: "/usr/bin/journalctl")
    c = OOMCollector({}, tmp_path / "n.db")
    journal = _Journal([])
    monkeypatch.setattr(c, "_journal", journal)
    r = c.run()
    assert r.success and r.records_collected == 0 and "systemd-journal or adm" in (r.note or "")
    # Said once; from then on the look starts where that one stopped.
    journal.entries.extend((T0.timestamp(), m) for _, m in RHEL8)
    r = c.run()
    assert r.note is None and r.records_collected == 1
    assert journal.calls[1][0].startswith("--since=@")

    # Told to start from now (lookback_days = 0): finding nothing is no sign.
    now = OOMCollector({"lookback_days": 0}, tmp_path / "now.db")
    monkeypatch.setattr(now, "_journal", _Journal([]))
    r = now.run()
    assert r.note is None and now._journal.calls == [["--since=now"]]
    now.run()
    assert now._journal.calls[1][0].startswith("--since=@")

    denied = OOMCollector({}, tmp_path / "other.db")
    monkeypatch.setattr(denied, "_journal", lambda args: subprocess.CompletedProcess(
        args, 1, "", "Failed to get journal access: Permission denied"))
    r = denied.run()
    assert r.success and "systemd-journal or adm" in (r.note or "")
    monkeypatch.setattr(oom, "find_tool", lambda name: None)
    r = c.run()
    assert "journalctl not found" in (r.note or "")


def test_the_journal_is_read_across_boots_and_on_old_systemd(tmp_path, monkeypatch):
    import subprocess

    import nomad.collectors.oom as oom
    monkeypatch.setattr(oom, "find_tool", lambda name: "/usr/bin/journalctl")
    calls = []

    def run(argv, **k):
        calls.append(argv)
        if "--output-fields=MESSAGE" in argv:       # systemd 219 (RHEL 7) has no such option
            return subprocess.CompletedProcess(
                argv, 1, "", "journalctl: unrecognized option '--output-fields=MESSAGE'")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(oom.subprocess, "run", run)
    OOMCollector({}, tmp_path / "n.db")._journal(["--since=-30d"])
    assert len(calls) == 2
    for argv in calls:
        # Every boot's kernel messages: -k (--dmesg) would be this boot's only.
        assert "-k" not in argv and "--dmesg" not in argv and argv[-1] == "_TRANSPORT=kernel"
        assert "--since=-30d" in argv and "json" in argv
    assert "--output-fields=MESSAGE" in calls[0] and "--output-fields=MESSAGE" not in calls[1]


# --- per_user on an interactive server ---------------------------------------------

def test_an_interactive_server_gets_its_own_rules():
    from nomad.collectors.per_user import DEFAULT_RULES, INTERACTIVE_RULES
    from nomad.collectors.per_user.collector import PerUserConfig
    head = PerUserConfig.from_dict({})
    web = PerUserConfig.from_dict({"role": "interactive"})
    assert head.rules == DEFAULT_RULES and web.rules == INTERACTIVE_RULES
    assert {r.rule_id for r in web.rules} == {"interactive_user_cpu_800pct_30min",
                                              "interactive_user_memory_64gb_30min"}
    # A busy notebook (one process at 100%, 16 GB) fires none of them.
    assert all(r.per_user for r in web.rules)


def test_the_hub_keeps_working_state_out_of_the_combined_database():
    import inspect

    import nomad.cli as cli
    src = inspect.getsource(cli.sync.callback)
    for table in ("per_user_state", "interactive_state", "oom_state"):
        assert f"'{table}'" in src


def test_oom_is_a_collector_nomad_plans():
    from nomad.collectors.plan import plan
    p = {x.name: x for x in plan({"collectors": {"oom": {"enabled": True}}})}
    assert p["oom"].enabled and "oom_kills" in p["oom"].spec.tables
    assert "interactive_daily" in p["interactive"].spec.tables


def test_the_wording_of_this_sites_kernel():
    """RHEL 8 (4.18.0-553), as an interactive server printed it (ids made up)."""
    lines = [
        ("2026-10-04T23:02:11", "python invoked oom-killer: gfp_mask=0x6280ca(GFP_HIGHUSER_MOVABLE|"
                                "__GFP_ZERO), order=0, oom_score_adj=0"),
        ("2026-10-04T23:02:11", "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
                                "mems_allowed=0-1,global_oom,task_memcg=/user.slice/user-5001.slice/"
                                "session-c99.scope,task=python,pid=2920394,uid=5001"),
        ("2026-10-04T23:02:11", "Out of memory: Killed process 2920394 (python) total-vm:359233860kB, "
                                "anon-rss:354114376kB, file-rss:356kB, shmem-rss:0kB, UID:5001 "
                                "pgtables:693528kB oom_score_adj:0"),
        ("2026-10-05T18:55:06", "tuned invoked oom-killer: gfp_mask=0x6200ca(GFP_HIGHUSER_MOVABLE), "
                                "order=0, oom_score_adj=0"),
    ]
    k, = parse(lines, "web1")
    assert (k["pid"], k["process"], k["uid"], k["anon_rss_kb"], k["total_vm_kb"]) == \
        (2920394, "python", 5001, 354114376, 359233860)
    assert k["memcg"] == "/user.slice/user-5001.slice/session-c99.scope"
