"""nomad 1.14.7: when each machine is busiest (`nomad facts busy`), and the
lab commands on a machine without ssh."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.facts.busy import busy

NOW = datetime(2026, 10, 19, 23, 0)          # a Monday night
DAYS = 14


def busy_hours(t):
    """Weekdays 9:00-16:59 are busy; nights and weekends quiet."""
    return t.weekday() < 5 and 9 <= t.hour < 17


def build(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, memory_total_mb INTEGER,
        memory_alloc_mb INTEGER, memory_free_mb INTEGER, cluster TEXT, source_site TEXT);
    CREATE TABLE interactive_hourly (hour TEXT, server_id TEXT, runs INTEGER, measured_runs INTEGER,
        people_sum INTEGER, people_max INTEGER, measured_people_sum INTEGER, busy_people_sum INTEGER,
        busy_people_max INTEGER, sessions_max INTEGER, open_seconds REAL, busy_seconds REAL,
        cpu_seconds REAL, mem_mb_max REAL, updated_at TEXT, source_site TEXT);
    CREATE TABLE mpstat_summary (id INTEGER PRIMARY KEY, timestamp TEXT, num_cores INTEGER,
        avg_busy_percent REAL, source_site TEXT);
    CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
        status TEXT, cpu_count INTEGER, cpu_idle_pct REAL, memory_total_mb INTEGER,
        memory_used_mb INTEGER, users_logged_in INTEGER, source_site TEXT);
    CREATE TABLE workstation_user_snapshot (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
        username TEXT, uid INTEGER, session_epoch INTEGER, cpu_usage_usec INTEGER,
        memory_peak_bytes INTEGER, source_site TEXT);
    """)
    counters = {"ws-a": 0, "ws-b": 0, "ws-c": 0}
    t = NOW - timedelta(days=DAYS + 5)            # five days before the window too
    while t <= NOW:
        ts = t.isoformat(timespec="microseconds")
        day = busy_hours(t)
        for node in ("n1", "n2"):
            # hpc1 counts memory; hpc2 doesn't, and its jobs run more threads than cores.
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, "
                      "cpu_load, memory_total_mb, memory_alloc_mb, memory_free_mb, cluster, source_site) "
                      "VALUES (?, ?, 'MIXED', 64, ?, ?, 512000, ?, ?, 'default', 'hpc1')",
                      (ts, node, 50 if day else 5, 48.0 if day else 4.0, 256000 if day else 8000,
                       200000 if day else 480000))
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, "
                      "cpu_load, memory_total_mb, memory_alloc_mb, memory_free_mb, cluster, source_site) "
                      "VALUES (?, ?, 'MIXED', 128, ?, ?, 512000, 0, 300000, 'default', 'hpc2')",
                      (ts, node, 8 if day else 2, 200.0 if day else 2.0))
        for host, base in (("ws-a", 10.0), ("ws-b", 50.0), ("ws-c", 90.0)):
            # The collector's own CPU percentages were never measured: always 0.
            c.execute("INSERT INTO workstation_state (timestamp, hostname, status, cpu_count, "
                      "cpu_idle_pct, memory_total_mb, memory_used_mb, users_logged_in, source_site) "
                      "VALUES (?, ?, 'online', 16, 0, 65536, ?, 1, 'lab')",
                      (ts, host, 30000 if day else 4000))
            # What people's sessions used: their CPU counters, every 5 minutes.
            cores = 16 * base / 100 if day else 0.16
            if host == "ws-c" and t == datetime(2026, 10, 16, 10, 0):
                cores = 16.0                       # flat out for those 5 minutes
            counters[host] += int(cores * 300 * 1e6)
            c.execute("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, "
                      "session_epoch, cpu_usage_usec, memory_peak_bytes, source_site) "
                      "VALUES (?, ?, 'u1', 1001, 1, ?, ?, 'lab')",
                      (ts, host, counters[host], 2 ** 30))
        c.execute("INSERT INTO mpstat_summary (timestamp, num_cores, avg_busy_percent, source_site) "
                  "VALUES (?, 104, ?, 'teach')", (ts, 60.0 if day else 5.0))
        if t.minute == 0:
            c.execute("INSERT INTO interactive_hourly (hour, server_id, runs, measured_runs, "
                      "people_sum, people_max, busy_people_sum, busy_people_max, cpu_seconds, "
                      "mem_mb_max, source_site) VALUES (?, 'local', 12, 12, ?, ?, ?, ?, ?, ?, 'teach')",
                      (t.strftime("%Y-%m-%dT%H"), 12 * (40 if day else 3), 45 if day else 4,
                       12 * (10 if day else 0), 12 if day else 0, 3600 * (20 if day else 1),
                       200000 if day else 20000))
        t += timedelta(minutes=5)
    # One spike: hpc1 fully allocated on Wednesday 14 Oct at 15:00.
    spike = datetime(2026, 10, 14, 15, 0).isoformat(timespec="microseconds")
    c.execute("UPDATE node_state SET cpus_alloc = 64 WHERE source_site = 'hpc1' AND timestamp = ?",
              (spike,))
    # hpc1's n2 was down for the last day: out of service, not idle.
    c.execute("UPDATE node_state SET state = 'DOWN*', cpus_alloc = 0 WHERE source_site = 'hpc1' "
              "AND node_name = 'n2' AND timestamp >= ?", ((NOW - timedelta(days=1)).isoformat(),))
    c.commit()
    c.close()


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    path = tmp_path_factory.mktemp("busy") / "combined.db"
    build(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = busy(c, days=DAYS, now=NOW)
    c.close()
    return path, out


def site(entries, name):
    return next(e for e in entries if e["site"] == name)


def test_the_most_cores_allocated_at_once_and_when(data):
    _, out = data
    hpc1 = site(out["clusters"], "hpc1")
    assert hpc1["nodes"] == 2 and hpc1["every_minutes"] == 5.0
    peak = hpc1["cores"]["allocated"]["peak"]
    assert (peak["value"], peak["of"], peak["pct"]) == (128, 128, 100.0)
    assert peak["at"] == "2026-10-14T15:00:00"
    # Only the window: nothing from before it.
    assert hpc1["from"] >= (NOW - timedelta(days=DAYS)).isoformat()


def test_the_busiest_and_quietest_hours(data):
    _, out = data
    hpc1 = site(out["clusters"], "hpc1")
    assert all(b["weekday"] in ("Mon", "Tue", "Wed", "Thu", "Fri") and 9 <= b["hour"] < 17
               for b in hpc1["busiest"])
    # The spike lifts its hour of the week: one reading of 24 at 100%, the rest 78%.
    assert hpc1["busiest"][0] == {"weekday": "Wed", "hour": 15, "value": 79.0}
    assert all(not (q["weekday"] not in ("Sat", "Sun") and 9 <= q["hour"] < 17)
               for q in hpc1["quietest"])
    week = hpc1["week"]["cores_allocated_pct"]
    assert len(week) == 7 and all(len(row) == 24 for row in week)
    assert week[2][10] == pytest.approx(78.1, abs=0.1) and week[6][3] == pytest.approx(7.8, abs=0.1)


def test_memory_allocated_where_slurm_counts_it_and_said_not_tracked_where_not(data):
    _, out = data
    hpc1, hpc2 = site(out["clusters"], "hpc1"), site(out["clusters"], "hpc2")
    mem = hpc1["memory"]["allocated"]["peak"]
    assert mem["value"] == 500.0 and mem["of"] == 1000.0 and mem["pct"] == 50.0
    assert hpc2["memory"]["allocated"] == {"tracked": False}
    assert hpc2["week"]["memory_allocated_pct"] is None
    assert hpc1["memory"]["held"]["peak"]["value"] == pytest.approx(609.4, abs=0.1)


def test_load_is_load_and_above_the_cores_is_said(data):
    _, out = data
    hpc2 = site(out["clusters"], "hpc2")
    load = hpc2["cores"]["load"]
    # 200 on each of 2 nodes of 128 cores: each counts up to its cores, the
    # most at once as well as the average; how often is counted apart.
    assert (load["peak"]["value"], load["peak"]["of"], load["peak"]["pct"]) == (256, 256, 100.0)
    assert load["over_cores_pct"] > 20 and load["over_cores_readings"] > 0     # the busy hours
    assert max(v for row in hpc2["week"]["load_pct"] for v in row if v is not None) <= 100


def test_a_stalled_node_does_not_make_the_load_more_than_the_cores(tmp_path):
    c = sqlite3.connect(tmp_path / "stall.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER, cpu_load REAL)")
    for i in range(100):
        t = (NOW - timedelta(minutes=5 * i)).isoformat()
        # Storage stalls once: processes waiting on it push one node's load to 1,000.
        c.execute("INSERT INTO node_state VALUES (?, 'a', 'MIXED', 96, 8, ?)", (t, 1000.0 if i == 50 else 8.0))
        c.execute("INSERT INTO node_state VALUES (?, 'b', 'IDLE', 96, 0, 0.0)", (t,))
    (cl,) = busy(c, days=1, now=NOW)["clusters"]
    load = cl["cores"]["load"]
    assert load["peak"]["value"] == 96 and load["peak"]["of"] == 192
    # The highest load was the stall: said, so it doesn't read as cores in use.
    assert load["peak"]["nodes_above_cores"] == 1
    assert load["over_cores_readings"] == 1 and load["over_cores_pct"] == 1.0


def test_an_interactive_server(data):
    _, out = data
    teach = site(out["interactive"], "teach")
    assert teach["people"]["peak"]["value"] == 45 and teach["computing"]["peak"]["value"] == 12
    assert teach["session_memory_gb"]["peak"]["value"] == pytest.approx(195.3, abs=0.1)
    assert "session_cores" not in teach          # an hour's CPU over 3600 s misled; dropped
    assert teach["busiest"][0]["hour"] in range(9, 17)
    m = teach["machine"]
    assert m["busy_cores"]["peak"]["value"] == pytest.approx(62.4) and m["busy_cores"]["peak"]["of"] == 104


def test_workstations_together_and_each(data):
    _, out = data
    lab = site(out["workstations"], "lab")
    assert lab["machines"] == 3 and lab["cores"] == 48 and lab["memory_gb"] == 192.0
    peak = lab["busy_cores"]["peak"]
    # 1.6 + 8 + ws-c's ten minutes (16 then 14.4: 15.2 on average).
    assert peak["at"] == "2026-10-16T10:00:00" and peak["value"] == pytest.approx(24.8, abs=0.05)
    assert lab["machines_busy"]["peak"]["value"] == 3
    names = [m["name"] for m in lab["each"]]
    assert names == ["ws-c", "ws-b", "ws-a"]                 # busiest on average first
    c = lab["each"][0]
    # Its highest ten minutes: 16 then 14.4 cores.
    assert c["peak_cores"]["value"] == pytest.approx(15.2, abs=0.05)
    assert c["peak_cores"]["at"] == "2026-10-16T10:00:00"
    assert c["busiest"]["weekday"] in ("Mon", "Tue", "Wed", "Thu", "Fri")
    assert lab["cpu_measured"] and c["cpu_measured"]


def test_workstations_without_peoples_counters_say_cpu_not_measured(tmp_path):
    c = sqlite3.connect(tmp_path / "ws.db")
    c.execute("CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, cpu_count INTEGER, "
              "cpu_idle_pct REAL, memory_total_mb INTEGER, memory_used_mb INTEGER)")
    for i in range(50):
        c.execute("INSERT INTO workstation_state VALUES (?, 'w1', 8, 0, 32768, 2048)",
                  ((NOW - timedelta(minutes=5 * i)).isoformat(),))
    (w,) = busy(c, days=1, now=NOW)["workstations"]
    # cpu_idle_pct = 0 must not read as 8 of 8 cores busy.
    assert w["cpu_measured"] is False and w["busy_cores"] is None and w["machines_busy"] is None
    assert w["memory_in_use"]["peak"]["value"] == 2.0 and w["busiest"] == []


def test_peoples_cpu_counts_within_one_life_of_their_slice(tmp_path):
    """An interval across a gap of more than two hours, a counter that went
    down, or a memory peak that went down (the slice was made again) is not
    counted; the rest are, each in the ten minutes it ends in."""
    c = sqlite3.connect(tmp_path / "slices.db")
    c.execute("CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, cpu_count INTEGER, "
              "memory_total_mb INTEGER, memory_used_mb INTEGER)")
    c.execute("CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, username TEXT, "
              "uid INTEGER, cpu_usage_usec INTEGER, memory_peak_bytes INTEGER)")
    start = NOW - timedelta(hours=6)
    readings = [  # minutes from start, CPU seconds so far, memory peak
        (0, 0, 10), (5, 600, 10), (10, 1200, 10),        # 2 cores
        (200, 1800, 10),                                  # after 190 minutes: not counted
        (205, 2100, 10),                                  # 1 core
        (210, 60, 10),                                    # went down: made again
        (215, 360, 10),                                   # 1 core
        (220, 960, 5),                                    # peak went down: made again
        (225, 1260, 5),                                   # 1 core
    ]
    for minutes, cpu, peak in readings:
        t = (start + timedelta(minutes=minutes)).isoformat()
        c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'w1', 'u1', 1001, ?, ?)",
                  (t, int(cpu * 1e6), peak))
        c.execute("INSERT INTO workstation_state VALUES (?, 'w1', 8, 32768, 2048)", (t,))
    # Someone under 1000 (a system account) is not a person.
    c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'w1', 'svc', 999, 99999999999, 1)",
              ((start + timedelta(minutes=5)).isoformat(),))
    import sys
    cpu = sys.modules["nomad.facts.busy"]._people_cpu(c, (NOW - timedelta(days=1)).isoformat(), NOW.isoformat())
    # Times from SQLite's julianday: right to well under a millisecond.
    assert cpu == {(None, "w1"): pytest.approx({
        "2026-10-19T17:0": 2.0, "2026-10-19T17:1": 2.0,
        "2026-10-19T20:2": 1.0, "2026-10-19T20:3": 1.0, "2026-10-19T20:4": 1.0}, rel=1e-6)}
    (w,) = busy(c, days=1, now=NOW)["workstations"]
    assert w["busy_cores"]["peak"]["value"] == 2.0 and w["busy_cores"]["peak"]["at"] == "2026-10-19T17:00:00"
    assert w["week"]["busy_cores_pct"][0][17] == 25.0          # Monday 17:00, 2 of 8 cores


def test_a_slice_made_again_inside_ten_minutes_is_one_person(tmp_path):
    """Readings every 2 minutes, 1 core throughout, the slice made again at
    10:04: the ten minutes read 1 core (the person's average), not 2."""
    c = sqlite3.connect(tmp_path / "again.db")
    c.execute("CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, uid INTEGER, "
              "cpu_usage_usec INTEGER)")
    cpu = 0
    for m in range(0, 10, 2):
        cpu = 0 if m == 4 else cpu + 120
        c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'w1', 1001, ?)",
                  (f"2026-10-19T10:{m:02d}:00", int(cpu * 1e6)))
    got = sys.modules["nomad.facts.busy"]._people_cpu(c, "2026-10-19T00:00:00", "2026-10-20T00:00:00")
    assert got == {(None, "w1"): pytest.approx({"2026-10-19T10:0": 1.0}, rel=1e-6)}


def test_readings_that_are_not_numbers_are_not_counted(tmp_path):
    c = sqlite3.connect(tmp_path / "text.db")
    c.execute("CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, uid INTEGER, "
              "cpu_usage_usec)")
    for m, v in ((0, 100000), (5, 100300), (10, "N/A"), (15, 100900), (20, 101200)):
        c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'w1', 1001, ?)",
                  (f"2026-10-19T10:{m:02d}:00", v if isinstance(v, str) else int(v * 1e6)))
    got = sys.modules["nomad.facts.busy"]._people_cpu(c, "2026-10-19T00:00:00", "2026-10-20T00:00:00")
    # 10:05 and 10:20 (1 core each); nothing across the text reading.
    assert got == {(None, "w1"): pytest.approx({"2026-10-19T10:0": 1.0, "2026-10-19T10:2": 1.0}, rel=1e-6)}
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total, "
              "cpus_alloc, cpu_load, memory_total_mb INTEGER, memory_free_mb INTEGER)")
    for i in range(20):
        t = (NOW - timedelta(minutes=5 * i)).isoformat()
        c.execute("INSERT INTO node_state VALUES (?, 'a', 'MIXED', 64, 32, 32.0, 1024000, 512000)", (t,))
        # An idle node whose load reads 'N/A', and one reading of FreeMem=N/A (stored as 0).
        c.execute("INSERT INTO node_state VALUES (?, 'b', 'IDLE', 64, 0, 'N/A', 1024000, ?)",
                  (t, 0 if i == 3 else 1000000))
    (cl,) = busy(c, days=1, now=NOW)["clusters"]
    assert cl["cores"]["load"]["peak"]["value"] == 32 and cl["cores"]["load"]["over_cores_readings"] == 0
    assert cl["memory"]["held"]["peak"]["value"] == 500.0 and cl["memory"]["held"]["mean_pct"] == 25.0


def test_free_memory_of_zero_is_not_held(tmp_path):
    """Slurm's FreeMem=N/A (a node that hasn't reported) is stored as 0: it
    would read as the whole node held."""
    c = sqlite3.connect(tmp_path / "na.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER, memory_total_mb INTEGER, memory_free_mb INTEGER)")
    for i in range(20):
        t = (NOW - timedelta(minutes=5 * i)).isoformat()
        for node in ("a", "b"):
            c.execute("INSERT INTO node_state VALUES (?, ?, 'MIXED', 64, 8, 1024000, ?)",
                      (t, node, 0 if (i == 7 and node == "a") else 921600))
    (cl,) = busy(c, days=1, now=NOW)["clusters"]
    assert cl["memory"]["held"]["peak"]["value"] == 200.0          # 100 GB on each, never 1,000


def test_a_cluster_with_every_node_out_all_window_is_said(tmp_path):
    from nomad.cli import cli
    path = tmp_path / "out.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER, source_site TEXT)")
    t = datetime.now() - timedelta(hours=1)
    for i in range(3):
        c.execute("INSERT INTO node_state VALUES (?, 'n1', 'DOWN*', 32, 0, 'gone')",
                  ((t - timedelta(minutes=5 * i)).isoformat(),))
    # And a site with one reading: no interval to say how often it is read.
    c.execute("INSERT INTO node_state VALUES (?, 'n1', 'IDLE', 32, 4, 'new')", (t.isoformat(),))
    c.commit()
    out = busy(c, days=1)
    assert [x["site"] for x in out["clusters"]] == ["new"] and out["clusters"][0]["every_minutes"] is None
    assert out["clusters_all_out"] == [{"site": "gone", "readings": 3, "from": out["clusters_all_out"][0]["from"],
                                        "until": out["clusters_all_out"][0]["until"]}]
    r = CliRunner().invoke(cli, ["facts", "busy", "--db", str(path), "--days", "1"])
    assert r.exit_code == 0, r.output
    assert "gone (cluster): every node out of service in all 3 readings" in r.output
    assert "new (cluster): 1 node in service, 1 reading," in r.output


def test_a_hub_reads_each_site_by_its_index_and_gets_the_same(tmp_path):
    """With the hub's (source_site, timestamp) index, each site is read on
    its own; rows without a site tag go by their cluster. Same answer."""
    import shutil
    path = tmp_path / "hub.db"
    build(path)
    c = sqlite3.connect(path)
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cluster) "
              "VALUES (?, 'x1', 'IDLE', 16, 4, 'untagged')", ((NOW - timedelta(hours=1)).isoformat(),))
    c.commit()
    plain = busy(c, days=DAYS, now=NOW)["clusters"]
    shutil.copy(path, tmp_path / "indexed.db")
    d = sqlite3.connect(tmp_path / "indexed.db")
    d.execute("CREATE INDEX idx_ns_site ON node_state(source_site, timestamp)")
    mod = sys.modules["nomad.facts.busy"]
    assert mod._indexed_first(d, "node_state", "source_site") and mod._distinct(d, "node_state", "source_site") == ["hpc1", "hpc2"]
    indexed = busy(d, days=DAYS, now=NOW)["clusters"]
    assert indexed == plain and [x["site"] for x in indexed] == ["hpc1", "hpc2", "untagged"]


def test_a_node_out_of_service_is_left_out_of_the_totals(data):
    _, out = data
    hpc1 = site(out["clusters"], "hpc1")
    assert hpc1["nodes_out"]["most"] == 1
    # n1 alone in the last day; the window's peaks are from when both ran.
    assert hpc1["cores"]["allocated"]["peak"]["of"] == 128


def test_memory_held_counts_only_the_nodes_running_jobs(tmp_path):
    """Slurm's free memory counts the file cache as used: a node running
    nothing can look full. Only the nodes running jobs count, and the
    figure is the most the jobs could have used."""
    c = sqlite3.connect(tmp_path / "cache.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER, memory_total_mb INTEGER, memory_alloc_mb INTEGER, "
              "memory_free_mb INTEGER)")
    for i in range(60):
        t = (NOW - timedelta(minutes=5 * i)).isoformat()
        for node in ("a", "b", "c"):              # running nothing, full of cache
            c.execute("INSERT INTO node_state VALUES (?, ?, 'IDLE', 52, 0, 384000, 0, 9000)", (t, node))
        c.execute("INSERT INTO node_state VALUES (?, 'd', 'MIXED', 52, 26, 384000, 192000, 192000)", (t,))
    (cl,) = busy(c, days=1, now=NOW)["clusters"]
    held = cl["memory"]["held"]
    assert held["peak"]["value"] == 187.5 and held["peak"]["of"] == 1500.0     # d's 192,000 MB of 4 nodes
    assert held["mean_pct"] == 12.5
    assert "in_use" not in cl["memory"] and cl["week"]["memory_held_pct"][0][22] == 12.5


def test_nothing_is_ranked_when_every_hour_is_the_same(tmp_path):
    c = sqlite3.connect(tmp_path / "idle.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER)")
    for i in range(14 * 24 * 12):
        c.execute("INSERT INTO node_state VALUES (?, 'n1', 32, 0)",
                  ((NOW - timedelta(minutes=5 * i)).isoformat(),))
    (cl,) = busy(c, days=14, now=NOW)["clusters"]
    assert cl["busiest"] == [] and cl["quietest"] == []


def test_an_hour_needs_two_days_to_be_ranked(data):
    path, _ = data
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = busy(c, days=7, now=NOW)
    teach = site(out["interactive"], "teach")
    # One reading an hour: a week gives each hour of the week once.
    assert teach["busiest"] == [] and teach["quietest"] == []


def test_a_site_that_stopped_says_so(tmp_path):
    c = sqlite3.connect(tmp_path / "stopped.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER)")
    for i in range(100):
        c.execute("INSERT INTO node_state VALUES (?, 'n1', 32, 4)",
                  ((NOW - timedelta(days=20, minutes=5 * i)).isoformat(),))
    (cl,) = busy(c, days=30, now=NOW)["clusters"]
    assert cl["stopped"] is True and cl["until"] < (NOW - timedelta(days=19)).isoformat()


def test_names_left_out_on_request(data):
    path, _ = data
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = busy(c, days=DAYS, now=NOW, names=False)
    names = [m["name"] for m in site(out["workstations"], "lab")["each"]]
    assert names == ["machine 1", "machine 2", "machine 3"]
    assert "ws-" not in json.dumps(out)


def test_an_empty_database_says_nothing(tmp_path):
    c = sqlite3.connect(tmp_path / "empty.db")
    c.execute("CREATE TABLE other (x)")
    out = busy(c, days=30, now=NOW)
    assert out["kind"] == "busy" and out["clusters"] == out["interactive"] == out["workstations"] == []


def test_a_site_database_without_source_site(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    c.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER)")
    for i in range(10):
        c.execute("INSERT INTO node_state VALUES (?, 'n1', 32, ?)",
                  ((NOW - timedelta(minutes=5 * i)).isoformat(), i))
    out = busy(c, days=1, now=NOW)
    (cl,) = out["clusters"]
    assert cl["site"] == "this site" and cl["cores"]["allocated"]["peak"]["value"] == 9
    assert cl["cores"]["load"]["peak"] is None and cl["memory"]["held"] is None


def test_the_command(data):
    path, _ = data
    from nomad.cli import cli
    r = CliRunner().invoke(cli, ["facts", "busy", "--db", str(path), "--days", "30", "--each"])
    assert r.exit_code == 0, r.output
    assert "hpc1 (cluster)" in r.output and "most cores allocated at once" in r.output
    assert "not tracked" in r.output and "teach (interactive)" in r.output
    assert "lab (workstations)" in r.output and "ws-c" in r.output
    r = CliRunner().invoke(cli, ["facts", "busy", "--db", str(path), "--mask", "--each"])
    assert r.exit_code == 0 and "ws-" not in r.output and str(path) not in r.output
    r = CliRunner().invoke(cli, ["facts", "busy", "--db", str(path), "--json"])
    assert json.loads(r.output)["kind"] == "busy"


# ── lab commands on a machine without ssh ───────────────────────────────

def test_adding_a_machine_where_there_is_no_ssh(tmp_path, monkeypatch):
    import subprocess
    from nomad.cli import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[general]\nx = 1\n")
    real = subprocess.run

    def no_ssh(cmd, *a, **k):
        if cmd and cmd[0] == "ssh":
            raise FileNotFoundError(2, "No such file or directory", "ssh")
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", no_ssh)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "lab", "add-machine", "pi1", "ws1"])
    assert r.exit_code == 0, r.output
    assert "ws1: not checked: there is no ssh here" in r.output


def test_a_broken_settings_file_is_refused_before_ssh_is_tried(tmp_path, monkeypatch):
    import subprocess
    from nomad.cli import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("a = 1\na = 2\n")
    tried = []
    real = subprocess.run

    def watch(cmd, *a, **k):
        if cmd and cmd[0] == "ssh":
            tried.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", watch)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "lab", "add-machine", "pi1", "ws1", "--apply"])
    assert r.exit_code != 0 and "nothing changed" in r.stderr and tried == []
