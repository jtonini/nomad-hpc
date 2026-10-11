"""nomad.facts: sites, a person, a lab -- what the Console's /api/v1 answers."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad import facts
from nomad.facts import jobs as jobfacts

NOW = datetime(2026, 10, 11, 7, 0, 0)


def t(**delta):
    return (NOW - timedelta(**delta)).isoformat(timespec="seconds")


def build_hub(path):
    """A hub's combined database: a cluster (hpc1), a workstation site (ws1),
    and a site gone quiet whose sync fails (gone)."""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE sync_sites (name TEXT PRIMARY KEY, partitions TEXT, filesystems TEXT,
        cluster_type TEXT, synced_at TEXT, merged INTEGER, records INTEGER, error TEXT,
        source TEXT, last_success TEXT);
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
        cluster TEXT, partitions TEXT, reason TEXT, gres TEXT, source_site TEXT);
    CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, gpu_index INTEGER,
        gpu_util_percent REAL, real_util_pct REAL, data_source TEXT, source_site TEXT);
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, state TEXT, submit_time TEXT,
        start_time TEXT, end_time TEXT, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
        runtime_seconds INTEGER, wait_time_seconds INTEGER, source_site TEXT);
    CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, peak_memory_gb REAL, source_site TEXT);
    CREATE TABLE filesystems (id INTEGER PRIMARY KEY, timestamp TEXT, path TEXT, total_bytes INTEGER,
        used_bytes INTEGER, available_bytes INTEGER, used_percent REAL, source_site TEXT);
    CREATE TABLE alert_state (key TEXT PRIMARY KEY, source TEXT, host TEXT, subject TEXT,
        severity TEXT, first_seen TEXT, last_seen TEXT, message TEXT, last_severity TEXT,
        source_site TEXT);
    CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
        department TEXT, status TEXT, source_site TEXT);
    CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
        collected_at TEXT, has_account INTEGER, source_site TEXT);
    CREATE TABLE storage_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT, status TEXT,
        total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER, pools_json TEXT);
    """)
    c.executemany("INSERT INTO sync_sites (name, synced_at, last_success, error) VALUES (?, ?, ?, ?)", [
        ("hpc1", t(minutes=5), t(minutes=5), None),
        ("ws1", t(minutes=5), t(minutes=5), None),
        ("gone", t(minutes=5), t(days=3), "ssh: connect to host gone port 22: secret-detail"),
    ])
    gb = 1024
    for stamp, states in ((t(days=1), ("IDLE", "IDLE", "IDLE", "IDLE")),
                          (t(minutes=10), ("IDLE", "MIXED", "IDLE+DRAIN", "DOWN*"))):
        for i, st in enumerate(states, 1):
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, "
                      "memory_total_mb, memory_alloc_mb, cluster, reason, gres, source_site) "
                      "VALUES (?, ?, ?, 32, ?, ?, ?, 'hpc1.cluster', ?, ?, 'hpc1')",
                      (stamp, f"n{i}", st, 16 if st == "MIXED" else 0, 256 * gb,
                       64 * gb if st == "MIXED" else 0, "disk swap" if "DRAIN" in st else None,
                       "gpu:a100:4(S:0-1)" if i == 2 else None))
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, "
              "memory_total_mb, memory_alloc_mb, cluster, source_site) "
              "VALUES (?, 'g1', 'IDLE', 16, 0, 65536, 0, 'gone', 'gone')", (t(days=3),))
    for g, u in enumerate((10, 20, 30, 40)):
        c.execute("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, real_util_pct, data_source, "
                  "source_site) VALUES (?, 'n2', ?, ?, 'dcgm', 'hpc1')", (t(minutes=5), g, u))
        c.execute("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, real_util_pct, data_source, "
                  "source_site) VALUES (?, 'n2', ?, 99, 'dcgm', 'hpc1')", (t(hours=1), g))
        c.execute("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, gpu_util_percent, data_source, "
                  "source_site) VALUES (?, 'n2', ?, 100, 'nvidia-smi', 'hpc1')", (t(minutes=4), g))
    jobs = [
        # id, user, state, ended (days ago), cpus, mem MB, gpus, runtime s, wait s
        ("1", "alice", "COMPLETED", 1, 4, 8000, 0, 3600, 60),
        ("2", "alice", "FAILED", 2, 4, 8000, 0, 3600, 120),
        ("3", "alice", "CANCELLED by 1234", 3, 4, 8000, 0, 600, 180),
        ("4", "bob", "TIMEOUT", 4, 8, 16000, 1, 7200, 600),
        ("5", "bob", "OUT_OF_MEMORY", 5, 8, 16000, 0, 1800, 1200),
        ("6", "carol", "NODE_FAIL", 6, 2, 4000, 0, 1800, 30),
        ("7", "carol", "COMPLETED", 40, 2, 4000, 0, 1800, 30),           # outside 30 days
    ]
    for jid, user, state, ago, cpus, mem, gpus, rt, wait in jobs:
        end = t(days=ago)
        c.execute("INSERT INTO jobs VALUES (?, ?, 'p', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hpc1')",
                  (jid, user, state, t(days=ago, seconds=rt + wait), t(days=ago, seconds=rt), end,
                   cpus, mem, gpus, rt, wait))
    c.execute("INSERT INTO jobs VALUES ('8', 'alice', 'p', 'RUNNING', ?, ?, 'Unknown', 4, 8000, 0, "
              "NULL, NULL, 'hpc1')", (t(hours=2), t(hours=1)))
    c.execute("INSERT INTO jobs VALUES ('9', 'alice', 'p', 'PENDING', ?, NULL, 'Unknown', 4, 8000, 0, "
              "NULL, NULL, 'hpc1')", (t(hours=1),))
    c.execute("INSERT INTO jobs VALUES ('10', 'bob', 'p', 'RUNNING', ?, ?, 'Unknown', 4, 8000, 0, "
              "NULL, NULL, 'hpc1')", (t(hours=2), t(hours=1)))
    # The same job number at another site must not lend its measurements.
    c.execute("INSERT INTO job_summary VALUES ('1', 50, 2.0, 'hpc1')")
    c.execute("INSERT INTO job_summary VALUES ('1', 100, 7.0, 'other')")
    c.execute("INSERT INTO job_summary VALUES ('2', 25, 4.0, 'hpc1')")
    c.execute("INSERT INTO job_summary VALUES ('4', 100, 2000.0, 'hpc1')")    # larger than any node
    TB = 10 ** 12
    for day in range(30, -1, -1):
        stamp = t(days=day) if day else t(minutes=15)
        used = int(4 * TB + (30 - day) * 0.01 * TB)                         # +10 GB a day
        c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes, available_bytes, "
                  "used_percent, source_site) VALUES (?, '/home', ?, ?, ?, ?, 'hpc1')",
                  (stamp, 10 * TB, used, 10 * TB - used, round(100 * used / (10 * TB), 1)))
    for site, path, stamp in (("hpc1", "/scratch", t(minutes=15)), ("ws1", "/data", t(minutes=20))):
        c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes, available_bytes, "
                  "used_percent, source_site) VALUES (?, ?, ?, ?, ?, 50, ?)",
                  (stamp, path, 100 * TB, 50 * TB, 50 * TB, site))
    c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes, available_bytes, "
              "used_percent, source_site) VALUES (?, '/old', 1, 1, 0, 100, 'hpc1')", (t(days=20),))
    c.executemany("INSERT INTO alert_state (key, severity, first_seen, last_seen, message, "
                  "last_severity, source_site) VALUES (?, ?, ?, ?, ?, ?, ?)", [
                      ("a", "warning", t(days=2), t(minutes=10), "/home of alice at 99%", "critical", "hpc1"),
                      ("b", "warning", t(days=2), t(days=1), "old", None, "hpc1"),
                  ])
    for host, status, ago, dept in (("w1", "online", {"minutes": 5}, "alice$"),
                                    ("w2", "offline", {"minutes": 5}, "alice$"),
                                    ("w3", "online", {"hours": 3}, None),
                                    ("w4", "online", {"days": 10}, "alice$")):
        c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
                  "VALUES (?, ?, ?, ?, 'ws1')", (t(**ago), host, dept, status))
    for user, account in (("alice", 1), ("bob", 1), ("dave", 0)):
        c.execute("INSERT INTO group_membership VALUES (?, 'alice$', 1, 'hpc1', ?, ?, 'hpc1')",
                  (user, t(minutes=30), account))
    c.execute("INSERT INTO storage_state (timestamp, hostname, status, total_bytes, used_bytes, free_bytes) "
              "VALUES (?, 'nas1', 'online', ?, ?, ?)", (t(minutes=10), 40 * TB, 10 * TB, 30 * TB))
    c.commit()
    c.close()


CONFIG = {"console": {"labs": {"group_pattern": "{netid}$",
                               "resources": {"alice$": {"storage": ["nas1"], "name": "Alice Lab"}}}},
          "alerts": {"episode_gap_minutes": 60}}


@pytest.fixture
def hub(tmp_path):
    path = tmp_path / "combined.db"
    build_hub(path)
    conn = facts.open_readonly(path)
    yield conn
    conn.close()


def by_name(data):
    return {s["name"]: s for s in data["sites"]}


def test_every_site_with_its_own_latest_reading(hub):
    data = facts.sites(hub, days=30, now=NOW, config=CONFIG)
    assert data["api"] == "nomad.facts/1" and data["kind"] == "sites"
    s = by_name(data)
    assert list(s) == ["gone", "hpc1", "ws1"]
    h = s["hpc1"]
    assert h["status"] == "ok" and h["age_minutes"] == 4         # its newest reading, any table
    n = h["nodes"]
    assert (n["total"], n["up"], n["drained"], n["down"], n["not_reporting"]) == (4, 2, 1, 1, 0)
    assert [u["node"] for u in n["unavailable"]] == ["n3", "n4"]
    assert h["cpu"] == {"cores": 128, "allocated_pct": 12.5}
    assert h["memory"]["allocated_pct"] == 6.2
    assert h["gpus"] == {"count": 4, "busy_pct": 25.0, "measured": 4, "source": "dcgm"}
    assert h["queue"]["running"] == 2 and h["queue"]["pending"] == 1
    assert h["alerts"] == {"critical": 1, "warning": 0, "info": 0}


def test_a_site_gone_quiet_is_stale_not_averaged_in(hub):
    g = by_name(facts.sites(hub, now=NOW, config=CONFIG))["gone"]
    assert g["status"] == "stale" and g["age_minutes"] == 3 * 1440
    assert g["nodes"]["not_reporting"] == 1 and g["nodes"]["up"] == 0
    assert g["cpu"]["allocated_pct"] is None
    assert g["sync"]["failing"] is True
    # The reason stays in the hub's own log.
    assert "secret-detail" not in json.dumps(facts.sites(hub, now=NOW, config=CONFIG))


def test_jobs_in_the_window(hub):
    j = by_name(facts.sites(hub, days=30, now=NOW, config=CONFIG))["hpc1"]["jobs"]
    assert j["finished"] == 6                                   # job 7 ended 40 days ago
    assert j["outcomes"]["cancelled"] == 1 and j["outcomes"]["node_fail"] == 1
    assert j["ran_to_an_end"] == 5
    # failed, timeout, OOM of the 4 that ran to an end; the node failure in neither count
    assert j["failed_pct"] == 75.0
    assert j["people"] == 3
    # Job 4's peak is larger than any node: set aside, not a measurement.
    assert j["memory"]["set_aside"] == 1 and j["memory"]["jobs_measured"] == 2
    # Job 1's own site's reading (50%), not the other site's (100%).
    assert j["core_hours"]["jobs_measured"] == 3
    assert j["core_hours"]["used_pct"] == round(100 * (0.5 * 4 + 0.25 * 4 + 1.0 * 16) / (4 + 4 + 16), 1)
    assert j["gpus"] == {"jobs": 1, "gpu_hours_requested": 2.0}
    assert j["wait_minutes"]["jobs"] == 6


def test_storage_growth_and_one_filesystem_at_two_sites(hub):
    s = by_name(facts.sites(hub, now=NOW, config=CONFIG))
    home = next(f for f in s["hpc1"]["storage"] if f["path"] == "/home")
    assert home["trend_days"] >= 30 and home["growth_bytes_per_day"] == pytest.approx(1e10, rel=0.01)
    assert home["days_until_full"] == pytest.approx(5.7e12 / 1e10, rel=0.02)
    assert "/old" not in {f["path"] for f in s["hpc1"]["storage"]}       # not read for 20 days
    scratch = next(f for f in s["hpc1"]["storage"] if f["path"] == "/scratch")
    assert scratch["same_as"] == [{"site": "ws1", "path": "/data"}]


def test_workstations_online_now(hub):
    w = by_name(facts.sites(hub, now=NOW, config=CONFIG))["ws1"]["workstations"]
    assert w["total"] == 3 and w["online"] == 1                 # w3 silent 3 h, w4 gone 10 days


def test_sites_names_nobody(hub):
    text = json.dumps(facts.sites(hub, now=NOW, config=CONFIG))
    for name in ("alice", "bob", "carol", "dave"):
        assert name not in text


def test_one_sites_own_database(tmp_path):
    path = tmp_path / "site.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER)")
    c.execute("INSERT INTO node_state VALUES (?, 'n1', 'ALLOCATED', 8, 8)", (t(minutes=1),))
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, state TEXT, end_time TEXT)")
    c.execute("INSERT INTO jobs VALUES ('1', 'x', 'RUNNING', 'Unknown')")
    c.commit()
    c.close()
    conn = facts.open_readonly(path)
    data = facts.sites(conn, now=NOW, config={"general": {"cluster_name": "solo"}})
    assert [s["name"] for s in data["sites"]] == ["solo"]
    s = data["sites"][0]
    assert s["status"] == "ok" and s["cpu"]["allocated_pct"] == 100.0 and s["queue"]["running"] == 1


def test_an_empty_database(tmp_path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    conn = facts.open_readonly(path)
    data = facts.sites(conn, now=NOW, config={"general": {"cluster_name": "x"}})
    assert data["sites"][0]["status"] == "no_data"
    me = facts.person(conn, "alice", now=NOW)
    assert me["jobs"] is None and me["now"]["running"] == 0
    # A lab nomad.toml lists machines for exists, as on My Lab; it is just empty.
    lab = facts.labs(conn, CONFIG, "alice", now=NOW)["labs"][0]
    assert lab["members"]["current"] == 0 and lab["jobs"] is None
    assert facts.labs(conn, {}, "alice", now=NOW)["labs"] == []


def test_a_person_sees_only_their_own(hub):
    me = facts.person(hub, "alice", days=30, now=NOW, config=CONFIG)
    assert me["kind"] == "me" and me["person"] == {"username": "alice"}
    assert me["jobs"]["finished"] == 3 and me["jobs"]["people"] == 1
    assert list(me["jobs_by_site"]) == ["hpc1"]
    assert me["now"]["running"] == 1 and me["now"]["pending"] == 1
    assert me["now"]["by_site"]["hpc1"]["stale"] is False
    assert me["score"] is None and me["advice"] == []
    text = json.dumps(me)
    assert "bob" not in text and "carol" not in text


def test_a_person_with_no_jobs(hub):
    me = facts.person(hub, "nobody", now=NOW)
    assert me["jobs"]["finished"] == 0 and me["jobs"]["failed_pct"] is None
    assert me["jobs"]["core_hours"]["used_pct"] is None and "jobs_by_site" not in me


def test_score_and_advice_come_from_nomad_edu(hub, monkeypatch, tmp_path):
    from nomad.edu import insights as ins
    from nomad.edu.insights import Issue, UserInsights
    seen = {}

    def fake(db_path, username, days, config=None, **k):
        seen.update(db=db_path, user=username, days=days)
        return UserInsights(username=username, job_count=2, window_days=days, overall_score=71.25,
                            overall_trajectory="improving", overall_change=4.0,
                            dimensions={"cpu": 80.0},
                            issues=[Issue("Memory", "memory", 2, 3, 40.0, "high", "stable",
                                          directive_label="--mem from 8G to 4G", rationale="why")])
    monkeypatch.setattr(ins, "user_insights", fake)
    me = facts.person(hub, "alice", days=14, now=NOW, db_path=tmp_path / "x.db")
    assert seen == {"db": str(tmp_path / "x.db"), "user": "alice", "days": 14}
    assert me["score"] == {"overall": 71.2, "jobs_scored": 2, "trend": "improving", "change": 4.0,
                           "dimensions": {"cpu": 80.0}}
    assert me["advice"][0]["advice"] == "--mem from 8G to 4G" and me["advice"][0]["jobs_affected"] == 2


def test_a_lab_taken_together(hub):
    data = facts.labs(hub, CONFIG, "alice", days=30, now=NOW)
    assert data["kind"] == "lab" and len(data["labs"]) == 1
    lab = data["labs"][0]
    assert lab["group"] == "alice$"
    # dave is listed without an account: not a member.
    assert lab["members"] == {"current": 2, "ran_jobs": 2, "no_longer_listed": 0,
                              "listed_without_account": 1}
    assert lab["jobs"]["finished"] == 5
    assert lab["now"] == {"running": 2, "pending": 1, "stale_sites": []}
    assert [(w["name"], w["online"]) for w in lab["workstations"]] == [("w1", True), ("w2", False)]
    nas = lab["storage"][0]
    assert nas["measured"] and nas["used_pct"] == 25 and nas["free_bytes"] == 30 * 10 ** 12


def test_a_lab_names_no_member(hub):
    text = json.dumps(facts.labs(hub, CONFIG, "alice", now=NOW))
    for name in ("bob", "carol", "dave"):
        assert f'"{name}"' not in text
    assert "alice" in text                                  # the group's own name


def test_leading_nothing_and_staff_asking_by_group(hub):
    assert facts.labs(hub, CONFIG, "carol", now=NOW)["labs"] == []
    staff = facts.labs(hub, CONFIG, groups=["alice$"], now=NOW)
    assert staff["labs"][0]["members"]["current"] == 2
    no_db = facts.labs(None, CONFIG, "alice", now=NOW)
    assert no_db["labs"][0]["storage"] == [{"entry": "nas1", "measured": False}]


def test_many_people_are_read_in_parts(hub, monkeypatch):
    import nomad.facts._common as common
    monkeypatch.setattr(jobfacts, "chunks", lambda items, size=500: common.chunks(items, 1))
    rows = jobfacts.finished_rows(hub, t(days=30), t(days=-1), users=["alice", "bob", "zed"])
    assert sorted({r["user_name"] for r in rows}) == ["alice", "bob"] and len(rows) == 5
    assert jobfacts.active(hub, users=["alice", "bob"])["hpc1"] == {"running": 2, "pending": 1}


def test_the_window_is_bounded(hub):
    assert facts.sites(hub, days=10_000, now=NOW, config=CONFIG)["window"]["days"] == 365
    assert facts.sites(hub, days=0, now=NOW, config=CONFIG)["window"]["days"] == 1


@pytest.fixture
def real_clock(monkeypatch):
    """The command reads the real clock: build its data from it."""
    import sys
    monkeypatch.setattr(sys.modules[__name__], "NOW", datetime.now().replace(microsecond=0))


def test_the_command(tmp_path, real_clock):
    from click.testing import CliRunner
    from nomad.cli import cli
    path = tmp_path / "combined.db"
    build_hub(path)
    runner = CliRunner()
    r = runner.invoke(cli, ["facts", "sites", "--db", str(path), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["api"] == "nomad.facts/1" and {s["name"] for s in data["sites"]} == {"hpc1", "ws1", "gone"}
    r = runner.invoke(cli, ["facts", "sites", "--db", str(path)])
    assert "disk swap" in r.output                               # an admin's own view
    r = runner.invoke(cli, ["facts", "sites", "--db", str(path), "--mask"])
    assert r.exit_code == 0 and "disk swap" not in r.output and "hpc1" in r.output
    assert str(tmp_path) not in r.output and "/home" not in r.output and "/data" not in r.output
    r = runner.invoke(cli, ["facts", "me", "Alice", "--db", str(path), "--mask"])
    assert r.exit_code == 0 and "alice" not in r.output.lower(), r.output
    r = runner.invoke(cli, ["facts", "me", "alice", "--db", str(path), "--json", "--mask"])
    assert json.loads(r.output)["person"]["username"] == "(this person)"
    r = runner.invoke(cli, ["facts", "lab", "nobody", "--db", str(path)])
    assert r.exit_code == 0 and "leads no lab" in r.output
    r = runner.invoke(cli, ["facts", "sites", "--db", str(tmp_path / "missing.db")])
    assert r.exit_code != 0 and "no database" in r.output


def test_node_reasons_only_when_asked(hub):
    plain = by_name(facts.sites(hub, now=NOW, config=CONFIG))["hpc1"]["nodes"]["unavailable"]
    assert all("reason" not in u for u in plain)
    staff = by_name(facts.sites(hub, now=NOW, config=CONFIG, reasons=True))["hpc1"]["nodes"]
    assert {u["node"]: u.get("reason") for u in staff["unavailable"]}["n3"] == "disk swap"


def test_a_sites_jobs_resting_on_few_people_are_left_out(hub):
    one_day = by_name(facts.sites(hub, days=1, now=NOW, config=CONFIG, min_people=3))["hpc1"]["jobs"]
    assert one_day["suppressed"] and one_day["finished"] is None
    # Three people is two besides any one of them: not enough either.
    assert by_name(facts.sites(hub, days=30, now=NOW, config=CONFIG,
                               min_people=3))["hpc1"]["jobs"]["suppressed"]
    assert by_name(facts.sites(hub, days=30, now=NOW, config=CONFIG,
                               min_people=2))["hpc1"]["jobs"]["finished"] == 6
    assert by_name(facts.sites(hub, days=1, now=NOW, config=CONFIG))["hpc1"]["jobs"]["finished"] == 1


def test_nodes_are_as_current_as_the_node_collector(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    c.execute("DELETE FROM node_state WHERE source_site = 'hpc1' AND timestamp = ?", (t(days=1),))
    c.execute("UPDATE node_state SET timestamp = ? WHERE source_site = 'hpc1'", (t(days=3),))
    c.commit()
    c.close()
    h = by_name(facts.sites(facts.open_readonly(path), now=NOW, config=CONFIG))["hpc1"]
    assert h["status"] == "ok"                                   # its disks still report
    assert h["nodes"]["not_reporting"] == 4 and h["cpu"]["allocated_pct"] is None
    assert h["queue"]["stale"] is True and h["gpus"]["busy_pct"] is None


def test_running_at_a_site_gone_quiet_is_not_counted_now(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    c.executemany("INSERT INTO jobs (job_id, user_name, state, end_time, source_site) "
                  "VALUES (?, 'alice', 'RUNNING', 'Unknown', 'gone')", [("g1",), ("g2",)])
    c.commit()
    c.close()
    conn = facts.open_readonly(path)
    now = facts.person(conn, "alice", now=NOW)["now"]
    assert now["running"] == 1 and now["stale_sites"] == ["gone"]
    assert now["by_site"]["gone"] == {"running": 2, "pending": 0, "as_of": t(days=3), "stale": True}
    assert facts.labs(conn, CONFIG, "alice", now=NOW)["labs"][0]["now"]["running"] == 2


def test_a_lab_total_that_would_be_one_members_is_left_out(hub):
    lab = facts.labs(hub, CONFIG, "alice", now=NOW, min_others=3)["labs"][0]
    # Besides alice, only bob ran jobs: the total less alice's own would be bob's.
    assert lab["jobs"]["suppressed"] and lab["jobs"]["finished"] is None
    assert lab["now"]["suppressed"] and lab["now"]["running"] is None
    # How many ran jobs would say it too: one student, that day.
    assert lab["members"]["ran_jobs"] is None and lab["members"]["current"] == 2
    staff = facts.labs(hub, CONFIG, groups=["alice$"], now=NOW, min_others=0)["labs"][0]
    assert staff["jobs"]["finished"] == 5


def test_text_where_numbers_belong(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    c.execute("UPDATE jobs SET req_cpus = 'N/A', wait_time_seconds = 'n/a' WHERE job_id = '1'")
    c.execute("UPDATE node_state SET cpus_total = 'x' WHERE node_name = 'n1'")
    c.execute("UPDATE filesystems SET used_bytes = '?' WHERE path = '/scratch'")
    c.execute("UPDATE gpu_stats SET real_util_pct = 'busy' WHERE gpu_index = 0")
    c.commit()
    c.close()
    conn = facts.open_readonly(path)
    data = facts.sites(conn, now=NOW, config={"alerts": "not a table"})
    h = by_name(data)["hpc1"]
    assert h["jobs"]["finished"] == 6 and h["cpu"]["cores"] == 96
    assert facts.person(conn, "alice", now=NOW)["jobs"]["finished"] == 3


def test_a_shared_pool_filling_while_this_use_stays_flat(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    TB = 10 ** 12
    for day in range(30, -1, -1):
        free = 5 * TB - (30 - day) * 10 ** 11                    # 100 GB a day, by others
        c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes, available_bytes, "
                  "used_percent, source_site) VALUES (?, '/pool/a', ?, ?, ?, 20, 'hpc1')",
                  (t(days=day) if day else t(minutes=15), 10 * TB, 1 * TB, free))
    c.commit()
    c.close()
    fs = {f["path"]: f for f in by_name(facts.sites(facts.open_readonly(path), now=NOW,
                                                    config=CONFIG))["hpc1"]["storage"]}
    assert fs["/pool/a"]["growth_bytes_per_day"] == pytest.approx(1e11, rel=0.01)
    assert fs["/pool/a"]["days_until_full"] == pytest.approx(20, abs=1)


def test_exports_of_one_pool_say_they_share_its_free_space(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE workstation_mount_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT, "
              "mountpoint TEXT, source TEXT, is_mounted INTEGER, is_responsive INTEGER, total_bytes INTEGER, "
              "used_bytes INTEGER, avail_bytes INTEGER)")
    for src, used in (("nas2:/pool/a", 10 ** 12), ("nas2:/pool/b", 2 * 10 ** 12)):
        c.execute("INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, source, is_mounted, "
                  "is_responsive, total_bytes, used_bytes, avail_bytes) VALUES (?, 'w1', '/m', ?, 1, 1, ?, ?, ?)",
                  (t(minutes=5), src, 10 ** 13, used, 5 * 10 ** 12))
    c.commit()
    c.close()
    config = {"console": {"labs": {"group_pattern": "{netid}$", "resources": {
        "alice$": {"storage": ["nas2:/pool/a", "nas2:/pool/b"]}}}}}
    lab = facts.labs(facts.open_readonly(path), config, "alice", now=NOW)["labs"][0]
    by = {s["entry"]: s for s in lab["storage"]}
    assert by["nas2:/pool/a"]["shares_free_with"] == ["nas2:/pool/b"]


def test_masked_output_names_nothing(tmp_path, monkeypatch, real_clock):
    from click.testing import CliRunner
    from nomad.cli import cli
    import sys
    pmod = sys.modules["nomad.facts.person"]          # nomad.facts.person is also the function
    path = tmp_path / "jsmith" / "combined.db"
    path.parent.mkdir()
    build_hub(path)
    c = sqlite3.connect(path)
    c.execute("UPDATE filesystems SET path = '/data/smithlab' WHERE path = '/scratch'")
    c.execute("CREATE TABLE workstation_mount_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT, "
              "mountpoint TEXT, source TEXT, is_mounted INTEGER, is_responsive INTEGER, total_bytes INTEGER, "
              "used_bytes INTEGER, avail_bytes INTEGER)")
    c.execute("INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, source, is_mounted, "
              "is_responsive, total_bytes, used_bytes, avail_bytes) VALUES (?, 'w1', '/m', "
              "'smith-nas:/export/smithlab', 1, 1, 10, 5, 5)", (t(minutes=5),))
    c.execute("UPDATE workstation_state SET hostname = 'smithlab-ws1' WHERE hostname = 'w1'")
    c.commit()
    c.close()
    monkeypatch.setattr(pmod, "score", lambda *a, **k: (None, [{
        "kind": "dimension", "dimension": "Memory", "severity": "high", "advice": "x", "why": "",
        "jobs_affected": 1, "of_jobs": 1, "cluster": "hpc1", "partition": "smith-condo"}]))
    config = {"console": {"labs": {"group_pattern": "{netid}$", "resources": {
        "alice$": {"storage": ["smith-nas:/export/smithlab"], "name": "Smith Lab"}}}}}
    import nomad.cli as nomad_cli
    monkeypatch.setattr(nomad_cli, "load_config", lambda *a, **k: config, raising=False)
    runner = CliRunner()
    outs = []
    for args in (["facts", "sites"], ["facts", "me", "alice"], ["facts", "lab", "alice"]):
        for extra in ([], ["--json"]):
            r = runner.invoke(cli, args + ["--db", str(path), "--mask"] + extra, obj={"config": config})
            assert r.exit_code == 0, r.output
            outs.append(r.output)
    text = "\n".join(outs).lower()
    for secret in ("jsmith", "smithlab", "smith lab", "smith-nas", "smith-condo", "alice"):
        assert secret not in text, secret
    # ...and still says what there is.
    assert "lab #1" in outs[4] and "storage #1" in outs[4] and "1 of 2 online" in outs[4]
    lab = json.loads(outs[5])["labs"][0]
    assert {w["name"] for w in lab["workstations"]} == {"workstation #1", "workstation #2"}
    assert "filesystem #" in outs[0] and json.loads(outs[3])["advice"][0]["partition"] == "(partition)"


def test_who_is_busy_counts_only_sites_that_report(tmp_path):
    path = tmp_path / "hub.db"
    build_hub(path)
    c = sqlite3.connect(path)
    for user in ("erin", "frank"):
        c.execute("INSERT INTO group_membership VALUES (?, 'alice$', 1, 'hpc1', ?, 1, 'hpc1')",
                  (user, t(minutes=30)))
        c.execute("INSERT INTO jobs (job_id, user_name, state, end_time, source_site) "
                  "VALUES (?, ?, 'RUNNING', 'Unknown', 'gone')", (f"x{user}", user))
    c.commit()
    c.close()
    conn = facts.open_readonly(path)
    lab = facts.labs(conn, CONFIG, "alice", now=NOW, min_others=3)["labs"][0]
    # Busy where it counts: bob only (erin's and frank's jobs are at a silent site).
    assert lab["now"]["suppressed"]
