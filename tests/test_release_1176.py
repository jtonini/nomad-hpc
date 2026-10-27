# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.6: what in a lab needs its PI's attention (nomad.facts.attention)."""
import copy
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

NOW = datetime(2026, 10, 27, 9, 0, 0)
CONFIG = {"console": {
    "labs": {"group_pattern": "{netid}$",
             "resources": {"pi$": {"name": "Pi Lab", "storage": ["nas1"], "workstations": ["ws8", "ws9"]}},
             "assistants": {"pi$": ["a2"]}},
    "storage": {"10.0.0.5": {"name": "bigstore", "note": "community $HOME"}}}}
TB, GB = 10**12, 10**9


def t(**d):
    return (NOW - timedelta(**d)).isoformat(timespec="seconds")


def _storage(c):
    from nomad.collectors import storage_use as su
    c.executescript(su.SCHEMA)

    def owners(when, server, path, rows):
        for name, used, soft in rows:
            c.execute("INSERT INTO storage_owners (timestamp, server, path, dataset, kind, name, used_bytes, "
                      "soft_bytes, hard_bytes) VALUES (?, ?, ?, NULL, 'user', ?, ?, ?, NULL)",
                      (when, server, path, name, used, soft))

    for server, path, total, used in (("bigstore", "/home", 100 * TB, 60 * TB), ("nas1", "/data", 10 * TB, 9 * TB),
                                      ("backup1", "/home", 100 * TB, 60 * TB)):
        c.execute("INSERT INTO storage_paths (timestamp, server, path, fs, total_bytes, used_bytes, "
                  "available_bytes, accounting) VALUES (?, ?, ?, 'xfs', ?, ?, ?, 1)",
                  (t(minutes=10), server, path, total, used, total - used))
    # backup1 holds copies of bigstore: never anyone's data twice
    c.execute("INSERT INTO storage_reports (server, mode, started, finished, copy_of) "
              "VALUES ('backup1', 'usage', ?, ?, 'bigstore')", (t(minutes=10), t(minutes=9)))
    now_rows = {"bigstore": [("a1", 3 * TB, None), ("a4", 200 * GB, None), ("pi", 5 * TB, None),
                             ("a3", 50 * GB, None), ("1234", 2 * TB, None)],
                "nas1": [("a1", 1 * TB, None), ("a2", 400 * GB, 420 * GB)],
                "backup1": [("a1", 3 * TB, None)]}
    then_rows = {"bigstore": [("a1", 2500 * GB, None), ("a4", 200 * GB, None), ("pi", 5 * TB, None),
                              ("a3", 50 * GB, None)],
                 "nas1": [("a1", 1 * TB, None), ("a2", 200 * GB, 420 * GB)],
                 "backup1": [("a1", 3 * TB, None)]}
    for server, path in (("bigstore", "/home"), ("nas1", "/data"), ("backup1", "/home")):
        owners(t(minutes=10), server, path, now_rows[server])
        owners(t(days=8), server, path, then_rows[server])
    c.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_uid, top_owner, bytes, files, "
              "older1_bytes, older2_bytes, age1_days, age2_days, complete) "
              "VALUES (?, 'bigstore', '/home', 'a1', 1001, 'a1', ?, 1000, ?, ?, 180, 365, 1)",
              (t(hours=8), 3 * TB, 2500 * GB, 2 * TB))


@pytest.fixture
def hub_path(tmp_path):
    path = tmp_path / "combined.db"
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, state TEXT, submit_time TEXT,
        start_time TEXT, end_time TEXT, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
        runtime_seconds INTEGER, wait_time_seconds INTEGER, pending_reason TEXT, eligible_time TEXT,
        alloc_tres TEXT, source_site TEXT);
    CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL, source_site TEXT);
    CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
        collected_at TEXT, has_account INTEGER, source_site TEXT);
    CREATE TABLE storage_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT, status TEXT,
        total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER, pools_json TEXT);
    CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
        department TEXT, status TEXT, source_site TEXT);
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
        cluster TEXT, partitions TEXT, reason TEXT, gres TEXT, source_site TEXT);
    """)
    # hpc1's queue read a few minutes ago: its waiting jobs are now
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
              "memory_alloc_mb, cluster, source_site) VALUES (?, 'n1', 'IDLE', 32, 0, 262144, 0, 'hpc1', 'hpc1')",
              (t(minutes=5),))
    for u in ("pi", "a1", "a2", "a3", "a4", "a5", "a6"):
        c.execute("INSERT INTO group_membership VALUES (?, 'pi$', 1, 'hpc1', ?, 1, 'hpc1')", (u, t(minutes=30)))
    jobs, summary = [], []

    def job(jid, user, state, ended, cpus=1, mem=1024, hours=1, cpu=None, peak=None, reason=None, sub=None,
            tres=None):
        waiting = state in ("PENDING", "REQUEUE_HOLD")
        jobs.append((str(jid), user, "compute", state, sub or t(days=ended, hours=hours + 1),
                     None if waiting else t(days=ended, hours=hours), None if waiting else t(days=ended),
                     cpus, mem, 0, None if waiting else hours * 3600, 60, reason, None, tres, "hpc1"))
        if cpu is not None or peak is not None:
            summary.append((str(jid), cpu, peak, "hpc1"))
    job(101, "a1", "OUT_OF_MEMORY", 1)
    job(102, "a1", "OUT_OF_MEMORY", 2)
    job(103, "a1", "OUT_OF_MEMORY", 10)                 # before the week: not counted
    job(201, "a2", "TIMEOUT", 3)
    job(202, "a2", "DEADLINE", 2)
    for i in range(4):
        job(301 + i, "a3", "FAILED", 1 + i)
    job(305, "a3", "COMPLETED", 1)
    for i in range(10):
        job(401 + i, "a4", "COMPLETED", 1 + i % 5, cpus=16, mem=64 * 1024, hours=1, cpu=10.0, peak=4.0,
            tres="cpu=16,mem=64G,node=1")
    # a6: one measured job, ten that weren't: what they used isn't known
    job(601, "a6", "COMPLETED", 1, cpus=1, hours=1, cpu=5.0)
    for i in range(10):
        job(611 + i, "a6", "COMPLETED", 2, cpus=10, hours=10)
    # Waiting: only what won't start by itself, a member at a time, the longest first
    for jid, reason, days in ((501, "DependencyNeverSatisfied", 3), (502, "Priority", 3),
                              (504, "QOSMaxJobsPerUserLimit", 4), (505, "AssocGrpGRES", 4),
                              (506, "QOSMaxWallDurationPerJobLimit", 2),
                              (507, "ReqNodeNotAvail, Reserved for maintenance", 5), (508, "PartitionDown", 5),
                              (509, "JobArrayTaskLimit", 5), (511, "AssocGrpCpuLimit", 5)):
        job(jid, "a5", "PENDING", 0, reason=reason, sub=t(days=days))
    job(503, "a5", "PENDING", 0, reason="JobHeldUser", sub=t(hours=2))          # not a day yet
    job(510, "a3", "REQUEUE_HOLD", 0, reason="launch failed requeued held", sub=t(days=30))
    job(901, "pi", "OUT_OF_MEMORY", 1)                  # the PI's own
    c.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", jobs)
    c.executemany("INSERT INTO job_summary VALUES (?, ?, ?, ?)", summary)
    c.execute("INSERT INTO storage_state (timestamp, hostname, status, total_bytes, used_bytes, free_bytes) "
              "VALUES (?, 'nas1', 'online', 100, 95, 5)", (t(minutes=10),))
    # ws9: last answered 3 h ago, the collector's "offline" records since;
    # ws8: answered 30 minutes ago (not long enough to say)
    c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
              "VALUES (?, 'ws9', 'pi$', 'online', 'ws')", (t(hours=3),))
    for m in (120, 60, 5):
        c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
                  "VALUES (?, 'ws9', 'pi$', 'offline', 'ws')", (t(minutes=m),))
    c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
              "VALUES (?, 'ws8', 'pi$', 'online', 'ws')", (t(minutes=30),))
    c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
              "VALUES (?, 'ws8', 'pi$', 'offline', 'ws')", (t(minutes=5),))
    _storage(c)
    c.commit()
    c.close()
    return path


@pytest.fixture
def hub(hub_path):
    from nomad import facts
    conn = facts.open_readonly(hub_path)
    yield conn
    conn.close()


def _lab(conn, who="pi", config=CONFIG, **kw):
    from nomad import facts
    lab, = facts.labs(conn, config, who, now=NOW, names=True, **kw)["labs"]
    return lab


def test_the_pi_is_told_whom_to_help_most_pressing_first(hub):
    lab = _lab(hub)
    got = [(i["kind"], i["user"]) for i in lab["attention"]]
    # not the PI's own job or data: whom to help, not themselves
    assert got == [("waiting", "a3"), ("waiting", "a5"), ("out_of_memory", "a1"), ("timeout", "a2"),
                   ("failing", "a3"), ("quota", "a2"), ("storage", None),
                   ("data", "a1"), ("data", "a2"), ("data", "a4"),
                   ("over_cpu", "a4"), ("over_memory", "a4"), ("workstation", None)]
    says = {(i["kind"], i["user"]): i["says"] for i in lab["attention"]}
    assert says[("waiting", "a3")] == ("a3's job 510 won't start by itself: it has waited 30 d because it was "
                                       "requeued and is held (scontrol release lets it start).")
    assert says[("waiting", "a5")] == ("a5: 2 jobs won't start by themselves; the longest, 501, has waited 3 d "
                                       "because the job it depends on ended in a way that never lets it start.")
    a5 = next(i for i in lab["attention"] if i["user"] == "a5")
    assert [j["job_id"] for j in a5["jobs"]] == ["501", "506"]
    assert says[("out_of_memory", "a1")] == "a1: 2 jobs ran out of memory in the last 7 days (the latest, 101)."
    assert says[("timeout", "a2")] == ("a2: 1 job hit its time limit and 1 job reached its --deadline in the "
                                       "last 7 days (the latest, 202).")
    assert says[("failing", "a3")] == "a3: 4 of the 5 jobs that ran to an end failed in the last 7 days (1 completed)."
    assert says[("quota", "a2")] == "a2 is at 95% of a soft quota on nas1:/data (the lab's): 400.0 GB of 420.0 GB."
    assert says[("storage", None)] == "nas1 is 95% full."
    assert says[("data", "a1")] == ("a1 holds 4.0 TB: 3.0 TB on bigstore:/home (community $HOME), 1.0 TB on "
                                    "nas1:/data (the lab's); up 500.0 GB in the last 7 days; 2.0 TB of it "
                                    "unchanged for a year.")
    assert says[("data", "a2")] == "a2 holds 400.0 GB on nas1:/data (the lab's); up 200.0 GB in the last 7 days."
    assert says[("data", "a4")] == "a4 holds 200.0 GB on bigstore:/home (community $HOME)."
    assert says[("over_cpu", "a4")] == ("a4 held 160 core-hours in the last 7 days, in jobs nomad measured, "
                                        "and used 10% of them.")
    assert says[("over_memory", "a4")] == ("a4's jobs used 6% of the memory they asked for in the last 7 days "
                                           "(10 jobs measured).")
    assert says[("workstation", None)] == f"Workstation ws9 hasn't reported since {t(hours=3)[:16].replace('T', ' ')}."
    oom = next(i for i in lab["attention"] if i["kind"] == "out_of_memory")
    assert [j["job_id"] for j in oom["jobs"]] == ["101", "102"] and oom["jobs"][0]["site"] == "hpc1"
    assert lab["attention_days"] == 7


def test_an_assistant_is_not_told_about_the_pi_or_themselves(hub):
    lab = _lab(hub, "a2")
    assert lab["as"] == "assistant"
    users = {i["user"] for i in lab["attention"]}
    assert not users & {"pi", "a2"} and {"a1", "a3"} <= users
    assert "5.0 TB" not in str(lab["attention"])          # the PI's data, not even as a total


def test_staff_see_every_member_the_pi_too(hub):
    from nomad import facts
    lab, = facts.labs(hub, CONFIG, groups=["pi$"], now=NOW, names=True)["labs"]
    got = [(i["kind"], i["user"]) for i in lab["attention"]]
    assert ("out_of_memory", "pi") in got
    assert [u for k, u in got if k == "data"][:3] == ["pi", "a1", "a2"]


def test_a_number_unknown_to_the_machine_and_copies_are_nobodys(hub):
    lab = _lab(hub, groups=None)
    text = str(lab["attention"])
    assert "1234" not in text and "backup1" not in text


def test_nothing_without_names_and_quiet_weeks_say_nothing(hub):
    from nomad import facts
    lab, = facts.labs(hub, CONFIG, "pi", now=NOW)["labs"]
    assert "attention" not in lab
    later = NOW + timedelta(days=90)                    # the week moved on; waiting jobs too old
    lab, = facts.labs(hub, CONFIG, "pi", now=later, names=True)["labs"]
    assert not {i["kind"] for i in lab["attention"]} & {"waiting", "out_of_memory", "timeout", "failing",
                                                        "over_cpu", "over_memory"}


def test_core_hours_held_are_the_measured_jobs(hub):
    # a6 held 1,001 core-hours, 1 of them in a job nomad measured: nothing to say
    assert "a6" not in {i["user"] for i in _lab(hub)["attention"]}


def test_memory_not_held_by_slurm_says_nothing(hub_path):
    c = sqlite3.connect(hub_path)
    c.execute("UPDATE jobs SET alloc_tres = NULL")
    c.commit()
    c.close()
    from nomad import facts
    conn = facts.open_readonly(hub_path)
    try:
        assert "over_memory" not in {i["kind"] for i in _lab(conn)["attention"]}
    finally:
        conn.close()


def test_job_numbers_say_their_site_when_the_lab_runs_at_several(hub_path):
    c = sqlite3.connect(hub_path)
    c.execute("INSERT INTO jobs (job_id, user_name, state, submit_time, start_time, end_time, req_cpus, "
              "runtime_seconds, source_site) VALUES ('101', 'a4', 'COMPLETED', ?, ?, ?, 1, 60, 'hpc2')",
              (t(days=1, hours=2), t(days=1, hours=1), t(days=1)))
    c.commit()
    c.close()
    from nomad import facts
    conn = facts.open_readonly(hub_path)
    try:
        says = {(i["kind"], i["user"]): i["says"] for i in _lab(conn)["attention"]}
    finally:
        conn.close()
    assert says[("out_of_memory", "a1")].endswith("(the latest, 101 at hpc1).")


def test_shares_read_under_their_limit():
    from nomad.facts.attention import _under
    assert _under(24.96, 25) == "24.9%" and _under(19.5, 20) == "19.5%"
    assert _under(10.2, 25) == "10%" and _under(0.3, 25) == "under 1%"


def test_the_reasons_a_job_waits_for_ever():
    from nomad.facts.attention import why_stuck
    for reason in ("QOSMaxWallDurationPerJobLimit", "AssocMaxCpuPerJobLimit", "QOSMaxGRESPerJob",
                   "JobHoldMaxRequeue", "QOSMinCpuNotSatisfied", "MaxMemPerLimit"):
        assert why_stuck(reason), reason
    # the cluster's, or lifting as jobs end
    for reason in ("Priority", "Resources", "QOSMaxJobsPerUserLimit", "AssocGrpCpuLimit", "AssocGrpGRES",
                   "QOSGrpCPURunMinutesLimit", "JobArrayTaskLimit", "PartitionDown", "BeginTime",
                   "ReqNodeNotAvail, Reserved for maintenance", "ReqNodeNotAvail, UnavailableNodes:n[1-2]",
                   "ReqNodeNotAvail, May be reserved for other job", "", None):
        assert why_stuck(reason) is None, reason
    assert why_stuck("launch failed requeued held", "REQUEUE_HOLD")


def test_the_limits_come_from_nomad_toml(hub):
    config = copy.deepcopy(CONFIG)
    config["console"]["labs"]["attention"] = {"days": 30, "holder_gb": 1000, "growth_gb": 1000,
                                              "storage_pct": 96, "workstation_hours": 4}
    lab = _lab(hub, config=config)
    got = [(i["kind"], i["user"]) for i in lab["attention"]]
    says = {k: i["says"] for k, i in zip(got, lab["attention"])}
    assert says[("out_of_memory", "a1")].startswith("a1: 3 jobs ran out of memory in the last 30 days")
    assert [u for k, u in got if k == "data"] == ["a1"]
    assert "storage" not in {k for k, _ in got} and "workstation" not in {k for k, _ in got}
    assert lab["attention_days"] == 30


def test_unusable_limits_are_reported_and_the_defaults_apply():
    from nomad.config.access import access_from
    from nomad.config.attention import DEFAULTS, settings
    got, problems = settings({"days": 0, "storage_pct": "high", "holders": 2.5, "colour": 1, "quota_pct": 80,
                              "holder_gb": float("inf"), "growth_gb": float("nan"), "failing_jobs": 10**7})
    assert got == {**DEFAULTS, "quota_pct": 80}
    assert len(problems) == 7 and any("colour: unknown" in p for p in problems)
    assert any("from 1 to 1,000,000" in p for p in problems)
    config = copy.deepcopy(CONFIG)
    config["console"]["labs"]["attention"] = {"days": 400, "holders": float("inf")}
    assert sum("[console.labs.attention]" in p for p in access_from(config, log=False).problems) == 2
    assert not any("attention" in p for p in access_from(CONFIG, log=False).problems)


def test_the_command_shows_it_and_masking_drops_it(hub_path, tmp_path, monkeypatch):
    from nomad.cli import cli
    import nomad.facts.cli as fcli
    import nomad.facts as facts
    monkeypatch.setattr(fcli, "_open", lambda ctx, db: ("x.db", facts.open_readonly(hub_path)))
    orig = facts.labs
    monkeypatch.setattr(facts, "labs", lambda *a, **k: orig(*a, **{**k, "now": NOW}))
    p = tmp_path / "nomad.toml"
    p.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n\n[console.labs.resources."pi$"]\n'
                 'name = "Pi Lab"\nstorage = ["nas1"]\nworkstations = ["ws9"]\n')
    out = CliRunner().invoke(cli, ["-c", str(p), "facts", "lab", "pi", "--each"]).output
    assert "Needs attention:" in out and "a1: 2 jobs ran out of memory" in out, out
    assert "a1 holds 4.0 TB" in out
    for args in (["--mask"], ["--each", "--mask"]):
        masked = CliRunner().invoke(cli, ["-c", str(p), "facts", "lab", "pi", *args]).output
        assert "Needs attention" not in masked
        for name in ("a1", "nas1", "bigstore", "ws9"):
            assert name not in masked, (args, name)


def _write(hub_path, sql, *args):
    c = sqlite3.connect(hub_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _fresh(hub_path, **kw):
    from nomad import facts
    conn = facts.open_readonly(hub_path)
    try:
        return _lab(conn, **kw)
    finally:
        conn.close()


def test_an_old_reading_is_said_with_its_date_and_no_change(hub_path):
    # bigstore read 20 days ago, and a reading 8 days before that
    _write(hub_path, "UPDATE storage_owners SET timestamp = ? WHERE server = 'bigstore' AND timestamp = ?",
           t(days=20), t(minutes=10))
    _write(hub_path, "UPDATE storage_owners SET timestamp = ? WHERE server = 'bigstore' AND timestamp = ?",
           t(days=28), t(days=8))
    says = {(i["kind"], i["user"]): i["says"] for i in _fresh(hub_path)["attention"]}
    a1 = says[("data", "a1")]
    assert f"3.0 TB on bigstore:/home (community $HOME), as of {t(days=20)[:10]}" in a1
    assert "up" not in a1                               # its change was weeks ago


def test_a_change_from_a_reading_far_older_than_the_window_is_not_said(hub_path):
    _write(hub_path, "UPDATE storage_owners SET timestamp = ? WHERE timestamp = ?", t(days=80), t(days=8))
    lab = _fresh(hub_path)
    assert not any(" up " in i["says"] for i in lab["attention"] if i["kind"] == "data")
    # a2 was named for her growth only with it: now she is named as a holder (3 holders)
    assert [i["user"] for i in lab["attention"] if i["kind"] == "data"] == ["a1", "a2", "a4"]


def test_unchanged_data_comes_from_a_recent_scan_and_never_exceeds_what_is_there(hub_path):
    _write(hub_path, "UPDATE storage_tops SET older2_bytes = ?", 9 * TB)        # more than a1 holds there
    a1 = next(i for i in _fresh(hub_path)["attention"] if i["user"] == "a1" and i["kind"] == "data")
    assert "; 3.0 TB of it unchanged for a year." in a1["says"]
    _write(hub_path, "UPDATE storage_tops SET scanned_at = ?", t(days=12))      # an old scan says nothing now
    a1 = next(i for i in _fresh(hub_path)["attention"] if i["user"] == "a1" and i["kind"] == "data")
    assert "unchanged" not in a1["says"]


def test_nested_zfs_paths_count_once_and_keep_the_growth(tmp_path):
    from nomad.collectors import storage_use as su
    from nomad.facts.attention import holdings, lab_attention
    path = tmp_path / "z.db"
    c = sqlite3.connect(path)
    c.executescript(su.SCHEMA)
    for p_, total in (("/tank", 10 * TB), ("/tank/a", 10 * TB)):
        c.execute("INSERT INTO storage_paths (timestamp, server, path, fs, total_bytes, used_bytes, "
                  "available_bytes, accounting) VALUES (?, 'zfs1', ?, 'zfs', ?, 0, 0, 1)", (t(minutes=5), p_, total))
    rows = [("/tank", "tank", 50 * GB, 50 * GB), ("/tank", "tank/a", 500 * GB, 200 * GB),
            ("/tank/a", "tank/a", 500 * GB, 200 * GB)]
    for p_, ds, now_b, then_b in rows:
        for when, b in ((t(minutes=5), now_b), (t(days=8), then_b)):
            c.execute("INSERT INTO storage_owners (timestamp, server, path, dataset, kind, name, used_bytes, "
                      "soft_bytes) VALUES (?, 'zfs1', ?, ?, 'user', 'nia', ?, ?)",
                      (when, p_, ds, b, 520 * GB if ds == "tank/a" else None))
    c.commit()
    c.close()
    from nomad import facts
    conn = facts.open_readonly(path)
    try:
        items = lab_attention(conn, {"storage": []}, {"nia"}, {}, NOW, settings={"holders": 0, "growth_gb": 100})
    finally:
        conn.close()
    says = [i["says"] for i in items]
    assert says == ["nia is at 96% of a soft quota on zfs1:/tank/a (dataset tank/a): 500.0 GB of 520.0 GB.",
                    "nia holds 550.0 GB: 500.0 GB on zfs1:/tank/a, 50.0 GB on zfs1:/tank; up 300.0 GB in the "
                    "last 7 days."]
    assert holdings is not None


def test_readings_that_cant_be_read_are_said_unchecked(hub, monkeypatch):
    import nomad.facts.attention as A
    monkeypatch.setattr(A, "holdings", lambda conn, days: None)
    lab = _lab(hub)
    assert lab["attention_unchecked"] == ["storage"]
    assert "data" not in {i["kind"] for i in lab["attention"]}


def test_whos_holding_what_is_read_once_until_the_database_changes(hub_path, monkeypatch):
    import nomad.facts.attention as A
    from nomad.storage import views
    calls = []
    orig = views.paths
    monkeypatch.setattr(views, "paths", lambda conn: calls.append(1) or orig(conn))
    A._HOLDINGS.clear()
    _fresh(hub_path)
    _fresh(hub_path)
    assert len(calls) == 1
    _write(hub_path, "UPDATE storage_owners SET used_bytes = used_bytes + 1 WHERE name = 'a1'")
    _fresh(hub_path)
    assert len(calls) == 2


def test_a_member_whose_every_job_failed(hub_path):
    _write(hub_path, "DELETE FROM jobs WHERE job_id = '305'")
    says = {(i["kind"], i["user"]): i["says"] for i in _fresh(hub_path)["attention"]}
    assert says[("failing", "a3")] == "a3: all 4 jobs that ran to an end in the last 7 days failed."
