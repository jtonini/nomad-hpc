"""1.41.0: one node over a window (nomad.facts.node, `nomad facts node`)."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import facts
from nomad.facts.node import NodeFactsError, step_minutes
from nomad.maintenance import Window

NOW = datetime(2026, 11, 11, 12, 0, 0)
SINCE = NOW - timedelta(hours=24)


def ts(t):
    return t.isoformat()


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "combined.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
            cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, memory_total_mb INTEGER,
            memory_alloc_mb INTEGER, memory_free_mb INTEGER, partitions TEXT, reason TEXT, gres TEXT,
            source_site TEXT, cluster TEXT);
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, job_name TEXT, state TEXT, partition TEXT,
            node_list TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER, req_gpus INTEGER,
            source_site TEXT, cluster TEXT);
        CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, gpu_index INTEGER,
            gpu_util_percent REAL, real_util_pct REAL, data_source TEXT, memory_used_mb INTEGER,
            memory_total_mb INTEGER, temperature_c INTEGER, source_site TEXT);
        CREATE TABLE hw_temps (id INTEGER PRIMARY KEY, timestamp TEXT, node TEXT, chip TEXT, sensor TEXT,
            label TEXT, temp_c REAL, crit_c REAL, source_site TEXT);
        CREATE TABLE cpu_clock (id INTEGER PRIMARY KEY, timestamp TEXT, node TEXT, mhz INTEGER,
            max_mhz INTEGER, source_site TEXT);
        CREATE TABLE node_disks (id INTEGER PRIMARY KEY, timestamp TEXT, node TEXT, path TEXT,
            used_percent REAL, source_site TEXT);
        CREATE TABLE oom_kills (timestamp TEXT, host TEXT, pid INTEGER, process TEXT, uid INTEGER,
            username TEXT, source_site TEXT);
        CREATE TABLE power_events (id INTEGER PRIMARY KEY, node TEXT, key TEXT, logged TEXT, sensor TEXT,
            event TEXT, detail TEXT, first_seen TEXT, severity TEXT, problem TEXT, source_site TEXT);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY, timestamp TEXT, severity TEXT, source TEXT, host TEXT,
            message TEXT, source_site TEXT);
    """)
    i = 0
    t = SINCE
    while t <= NOW:
        i += 1
        hour = (t - SINCE).total_seconds() / 3600
        if 6 <= hour < 7:                       # an hour with no readings at all
            t += timedelta(minutes=5)
            continue
        state, alloc, reason = "MIXED", 24, None
        if 10 <= hour < 12:
            state, alloc, reason = "DOWN*", 0, "jsmith's job wedged it"
        elif 12 <= hour < 13:
            state, alloc, reason = "IDLE+DRAIN", 0, "checking it"
        c.execute("INSERT INTO node_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, ts(t), "n012", state, 48, alloc, 30.0, 256000, 0, 128000, "basic,cpunodes", reason,
                   "gpu:a100:2", "spydur", "spydur"))
        # another node, and the same name at another site: not this node
        c.execute("INSERT INTO node_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (100000 + i, ts(t), "n013", "ALLOCATED", 48, 48, 48.0, 256000, 0, 1000, "basic", None, "",
                   "spydur", "spydur"))
        c.execute("INSERT INTO node_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (200000 + i, ts(t), "n012", "ALLOCATED", 16, 16, 16.0, 64000, 0, 1000, "batch", None, "",
                   "vadur", "vadur"))
        for g in (0, 1):
            c.execute("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, gpu_util_percent, real_util_pct, "
                      "data_source, memory_used_mb, memory_total_mb, temperature_c, source_site) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (ts(t), "n012", g, 100.0, 40.0 + 20 * g, "dcgm", 20000, 40000, 60 + g, "spydur"))
        c.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, temp_c, crit_c, source_site) "
                  "VALUES (?,?,?,?,?,?,?)", (ts(t), "n012", "coretemp", "Package id 0",
                                             81.0 if 15 <= hour < 15.1 else 55.0, 100.0, "spydur"))
        c.execute("INSERT INTO cpu_clock (timestamp, node, mhz, max_mhz, source_site) VALUES (?,?,?,?,?)",
                  (ts(t), "n012", 2400, 3000, "spydur"))
        c.execute("INSERT INTO node_disks (timestamp, node, path, used_percent, source_site) VALUES (?,?,?,?,?)",
                  (ts(t), "n012", "/localscratch", 40.0, "spydur"))
        t += timedelta(minutes=5)
    jobs = [("101", "amy", "fit", "COMPLETED", "n[010-012]", SINCE + timedelta(hours=1), SINCE + timedelta(hours=3)),
            ("102", "bob", "sim", "FAILED", "n012", SINCE + timedelta(hours=2), SINCE + timedelta(hours=4)),
            ("103", "bob", "sim2", "NODE_FAIL", "n[011,012]", SINCE + timedelta(hours=8), SINCE + timedelta(hours=10)),
            ("104", "amy", "long", "RUNNING", "n012", SINCE + timedelta(hours=20), None),
            ("105", "cat", "elsewhere", "COMPLETED", "n013", SINCE + timedelta(hours=2), SINCE + timedelta(hours=3)),
            ("106", "dan", "old", "COMPLETED", "n012", SINCE - timedelta(days=2), SINCE - timedelta(days=1))]
    for jid, user, name, state, nodes, start, end in jobs:
        c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (jid, user, name, state, "basic", nodes, ts(start), ts(end) if end else "Unknown", 8, 0,
                   "spydur", "spydur"))
    c.execute("INSERT INTO oom_kills VALUES (?,?,?,?,?,?,?)",
              (ts(SINCE + timedelta(hours=3, minutes=50)), "n012", 4242, "python3", 1001, "bob", "spydur"))
    c.execute("INSERT INTO power_events (node, key, first_seen, sensor, event, severity, source_site) "
              "VALUES (?,?,?,?,?,?,?)", ("n012", "k1", ts(SINCE + timedelta(hours=9)), "PS1 Status",
                                         "Power Supply AC lost", "warning", "spydur"))
    c.execute("INSERT INTO alerts (timestamp, severity, source, host, message, source_site) VALUES (?,?,?,?,?,?)",
              (ts(SINCE + timedelta(hours=10, minutes=5)), "critical", "node_state", "n012",
               "n012 down: jsmith's job wedged it", "spydur"))
    c.commit()
    c.close()
    ro = facts.open_readonly(path)
    yield ro
    ro.close()


MAINT = [Window(site="spydur", start=ts(SINCE + timedelta(hours=13)), until=ts(SINCE + timedelta(hours=14)),
                reason="new disk", by="jtonini"),
         Window(site="arachne", start=ts(SINCE), until=ts(NOW), reason="elsewhere", by="jtonini")]


def ask(conn, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("maintenance", MAINT)
    return facts.node(conn, "spydur", "n012", SINCE, NOW, **kw)


def test_steps_keep_points_few():
    assert step_minutes(SINCE, NOW) == 5
    assert step_minutes(NOW - timedelta(days=7), NOW) == 30
    assert step_minutes(NOW - timedelta(days=90), NOW) == 360


def test_readings_in_service_only(conn):
    r = ask(conn, staff=True)
    assert r["node"]["cpus"] == 48 and r["node"]["gpus"] == 2 and r["node"]["partitions"] == "basic,cpunodes"
    cpu = dict(r["series"]["cpu_alloc_pct"])
    assert set(cpu.values()) == {50.0}                     # 24 of 48; down and drained left out
    assert ts(SINCE + timedelta(hours=11))[:16] not in cpu
    assert r["series"]["load_pct"][0][1] == 62.5
    assert "mem_alloc_pct" not in r["series"]             # never allocated here: not tracked
    assert "doesn't allocate memory" in r["series_info"]["mem_held_pct"]["note"]   # and said why (1.45.1)
    assert r["series"]["mem_held_pct"][0][1] == 50.0
    assert r["series"]["gpu_busy_pct"][0][1] == 50.0      # DCGM, averaged over the GPUs
    assert max(v for _, v in r["series"]["temp_c"]) == 81.0
    assert r["series_info"]["temp_c"]["critical"] == 100.0
    assert r["series"]["clock_pct"][0][1] == 80.0
    assert r["series_info"]["disk_used_pct"]["paths"] == ["/localscratch"]


def test_what_happened(conn):
    r = ask(conn, staff=True)
    kinds = [e["kind"] for e in r["events"]]
    states = [e for e in r["events"] if e["kind"] == "state"]
    assert [(e["condition"], e["at"][11:16], e.get("until", "")[11:16]) for e in states] == [
        ("down", "22:00", "00:00"), ("drained", "00:00", "01:00"), ("up", "01:00", "")]
    assert states[0]["reason"] == "jsmith's job wedged it"
    gap = next(e for e in r["events"] if e["kind"] == "gap")
    assert gap["at"][11:16] == "17:55" and gap["until"][11:16] == "19:00"
    jobs = [e for e in r["events"] if e["kind"] == "job"]
    assert [(e["job_id"], e["what"]) for e in jobs] == [("101", "started"), ("102", "started"), ("101", "ended"),
                                                       ("102", "ended"), ("103", "started"), ("103", "ended"),
                                                       ("104", "started")]
    assert r["jobs"]["failed"] == 1 and r["jobs"]["node_fail"] == 1 and r["jobs"]["running_at_end"] == 1
    assert {"oom", "power", "alert", "maintenance"} <= set(kinds)
    m = next(e for e in r["events"] if e["kind"] == "maintenance")
    assert m["text"] == "maintenance: new disk" and m["by"] == "jtonini"
    assert next(e for e in r["events"] if e["kind"] == "oom")["user"] == "bob"
    said = " ".join(r["summary"])
    for words in ("spydur n012 (48 cores", "Down or not responding from", "(jsmith's job wedged it)",
                  "Cores allocated: 50%.", "4 jobs started and 3 ended",
                  "1 ended by a node failure", "Hottest sensor: 81 °C", "maintenance window"):
        assert words in said, words


def test_a_person_sees_their_own_and_counts_of_the_rest(conn):
    r = ask(conn, user="amy")
    text = json.dumps(r)
    for secret in ("bob", "jsmith", "checking it", "python3", "sim", "\"by\""):
        assert secret not in text, secret
    jobs = [e for e in r["events"] if e["kind"] == "job"]
    assert {e["job_id"] for e in jobs} == {"101", "104"} and all(e["mine"] for e in jobs)
    assert r["jobs"]["others"] == {"started": 2, "ended": 2}
    assert r["jobs"]["failed"] == 0                         # bob's failed job is bob's
    assert "of yours failed" not in " ".join(r["summary"]) and "(jsmith" not in " ".join(r["summary"])
    assert not any(e["kind"] == "alert" for e in r["events"])
    oom = next(e for e in r["events"] if e["kind"] == "oom")
    assert oom["text"] == "out of memory: the kernel ended a process" and "process" not in oom
    assert r["scope"] == "person"
    bob = ask(conn, user="bob")
    assert next(e for e in bob["events"] if e["kind"] == "oom")["process"] == "python3"


def test_what_cannot_be_answered(conn):
    with pytest.raises(NodeFactsError, match="92 days"):
        facts.node(conn, "spydur", "n012", NOW - timedelta(days=100), NOW, now=NOW)
    with pytest.raises(NodeFactsError, match="no readings of n999"):
        facts.node(conn, "spydur", "n999", SINCE, NOW, now=NOW)
    with pytest.raises(NodeFactsError, match="ends before"):
        facts.node(conn, "spydur", "n012", NOW, SINCE, now=NOW)


def test_the_command(conn, tmp_path):
    from nomad.cli import cli
    db = tmp_path / "combined.db"
    out = CliRunner().invoke(cli, ["facts", "node", "spydur", "n012", "--db", str(db),
                                   "--from", ts(SINCE), "--to", ts(NOW), "--json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["kind"] == "node" and data["scope"] == "staff"
    out = CliRunner().invoke(cli, ["facts", "node", "spydur", "n012", "--db", str(db),
                                   "--from", ts(SINCE), "--to", ts(NOW)])
    assert out.exit_code == 0, out.output
    assert "spydur n012 (48 cores" in out.output and "job 102 ended (failed), bob" in out.output
    out = CliRunner().invoke(cli, ["facts", "node", "spydur", "n999", "--db", str(db), "--hours", "6"])
    assert out.exit_code != 0 and "no readings of n999" in out.output
    out = CliRunner().invoke(cli, ["facts", "node", "spydur", "--db", str(db)])
    assert out.exit_code == 0 and "spydur's nodes in its latest reading (2):\n  n012 n013" in out.output


def test_memory_allocated_where_slurm_allocates_it(conn, tmp_path):
    """Where Slurm does hand out memory, its line is there and there is no note (1.45.1)."""
    w = sqlite3.connect(tmp_path / "combined.db")
    w.execute("UPDATE node_state SET memory_alloc_mb = memory_total_mb / 4")
    w.commit()
    w.close()
    r = ask(conn, staff=True)
    assert "mem_alloc_pct" in r["series"] and "note" not in r["series_info"]["mem_held_pct"]
