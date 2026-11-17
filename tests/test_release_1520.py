# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.52.0, `nomad metrics`: what nomad publishes for Prometheus. A hub's
combined database with two made-up sites: alpha, current, and beta, whose
readings stopped three hours ago."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import metrics as M

NOW = datetime.now().replace(microsecond=0)


def t(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def hub(tmp_path):
    db = tmp_path / "combined.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE sync_sites (name TEXT PRIMARY KEY, last_success TEXT, error TEXT);
        CREATE TABLE collection_log (id INTEGER PRIMARY KEY, source_site TEXT, collector TEXT, started_at TEXT,
            success INTEGER, error_message TEXT);
        CREATE TABLE node_state (id INTEGER PRIMARY KEY, source_site TEXT, node_name TEXT, timestamp TEXT,
            state TEXT, cpus_total INTEGER, cpus_alloc INTEGER, reason TEXT);
        CREATE TABLE cpu_clock_hosts (id INTEGER PRIMARY KEY, source_site TEXT, node TEXT, read_at TEXT,
            ratio REAL, throttled INTEGER, every_minutes REAL, problem TEXT);
        CREATE TABLE power_hosts (id INTEGER PRIMARY KEY, source_site TEXT, node TEXT, read_at TEXT, error TEXT,
            problems INTEGER, host_problems TEXT);
        CREATE TABLE power_sensors (id INTEGER PRIMARY KEY, source_site TEXT, node TEXT, sensor TEXT, subject TEXT,
            problem TEXT);
        CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY, source_site TEXT, timestamp TEXT, node_name TEXT,
            gpu_index INTEGER, data_source TEXT, real_util_pct REAL, memory_used_mb INTEGER,
            memory_total_mb INTEGER, temperature_c INTEGER, power_draw_w REAL);
        CREATE TABLE queue_state (id INTEGER PRIMARY KEY, source_site TEXT, timestamp TEXT, partition TEXT,
            pending_jobs INTEGER, running_jobs INTEGER, total_jobs INTEGER);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, source_site TEXT, job_id TEXT, user_name TEXT, partition TEXT,
            submit_time TEXT, start_time TEXT, wait_time_seconds INTEGER, state TEXT, eligible_time TEXT);
        CREATE TABLE filesystems (id INTEGER PRIMARY KEY, source_site TEXT, timestamp TEXT, path TEXT,
            total_bytes INTEGER, used_bytes INTEGER, available_bytes INTEGER, used_percent REAL);
        CREATE TABLE group_membership (username TEXT, group_name TEXT);
    """)
    c.executemany("INSERT INTO sync_sites (name, last_success) VALUES (?, ?)",
                  [("alpha", t(3)), ("beta", t(180))])
    c.executemany("INSERT INTO group_membership VALUES (?, ?)", [("kim", "kim$"), ("pat", "kim$")])
    for k in range(24):                                     # alpha: every 5 minutes for two hours
        at = t(2 + 5 * k)
        c.execute("INSERT INTO collection_log (source_site, collector, started_at, success) VALUES (?,?,?,?)",
                  ("alpha", "node_state", at, 1))
        c.execute("INSERT INTO collection_log (source_site, collector, started_at, success) VALUES (?,?,?,?)",
                  ("alpha", "gpu", at, 0 if k < 3 else 1))
        nodes = [("n01", "IDLE", 0), ("n02", "MIXED", 16), ("n03", "MIXED+DRAIN", 8), ("n04", "DOWN*", 0)]
        if k >= 6:                                          # n09: Slurm stopped listing it half an hour ago
            nodes.append(("n09", "IDLE", 0))
        for name, state, alloc in nodes:
            c.execute("INSERT INTO node_state (source_site, node_name, timestamp, state, cpus_total, cpus_alloc, "
                      "reason) VALUES (?,?,?,?,?,?,?)",
                      ("alpha", name, at, state, 32, alloc, "kim asked for it" if name == "n03" else None))
        for part, pend, run in (("standard", 12, 30), ("gpu", 3, 4)):
            c.execute("INSERT INTO queue_state (source_site, timestamp, partition, pending_jobs, running_jobs, "
                      "total_jobs) VALUES (?,?,?,?,?,?)", ("alpha", at, part, pend + k, run, pend + run))
        for source in ("dcgm", "nvidia-smi"):
            c.execute("INSERT INTO gpu_stats (source_site, timestamp, node_name, gpu_index, data_source, "
                      "real_util_pct, memory_used_mb, memory_total_mb, temperature_c, power_draw_w) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      ("alpha", at, "g01", 0, source, 75.0 if source == "dcgm" else None, 1024, 16384, 61, 120.5))
        c.execute("INSERT INTO gpu_stats (source_site, timestamp, node_name, gpu_index, data_source, "
                  "memory_used_mb, memory_total_mb, temperature_c) VALUES (?,?,?,?,?,?,?,?)",
                  ("alpha", at, "g01", 1, "nvidia-smi", 0, 16384, 40))
    for k in range(6):                                      # g02 went quiet five hours ago
        c.execute("INSERT INTO gpu_stats (source_site, timestamp, node_name, gpu_index, data_source, "
                  "real_util_pct, memory_used_mb, memory_total_mb) VALUES (?,?,?,?,?,?,?,?)",
                  ("alpha", t(300 + 5 * k), "g02", 0, "dcgm", 10.0, 1, 16384))
    for k in range(12):                                     # power every 10 minutes
        c.execute("INSERT INTO collection_log (source_site, collector, started_at, success) VALUES (?,?,?,?)",
                  ("alpha", "power", t(4 + 10 * k), 1))
    c.executemany("INSERT INTO cpu_clock_hosts (source_site, node, read_at, ratio, throttled, every_minutes) "
                  "VALUES (?,?,?,?,?,?)",
                  [("alpha", "n01", t(10), 0.98, 0, 15), ("alpha", "n02", t(180), 0.97, 0, 15),
                   ("alpha", "n03", t(5), 0.11, 1, 15)])
    c.executemany("INSERT INTO power_hosts (source_site, node, read_at, error, problems, host_problems) "
                  "VALUES (?,?,?,?,?,?)",
                  [("alpha", "n01", t(4), None, 0, None),
                   ("alpha", "n02", t(4), None, 3, json.dumps([{"subject": "power supply", "severity": "critical",
                                                               "problem": "one supply carries the node"}])),
                   ("alpha", "n04", t(4), "Could not open device at /dev/ipmi0", None, None),
                   ("alpha", "n05", t(120), None, 0, None),                      # its helper stopped
                   ("alpha", "n06", t(55), None, 0, None)])     # the helper's file read late: still in time
    c.executemany("INSERT INTO power_sensors (source_site, node, sensor, subject, problem) VALUES (?,?,?,?,?)",
                  [("alpha", "n02", "PSU1 Status", "power supply", "has no input power"),
                   ("alpha", "n02", "FAN3", "fans", "has stopped"),
                   ("alpha", "n01", "PSU1 Status", "power supply", None)])
    jobs = [("1", "kim", "standard", 60, 60), ("2", "pat", "standard", 120, 300), ("3", "kim", "standard", 600, 30),
            ("4", "pat", "gpu", None, 90), ("8", "pat", "standard", 86400, 45)]
    for jid, user, part, wait, started in jobs:
        start = NOW - timedelta(minutes=started)
        sub = start - timedelta(seconds=wait if wait is not None else 45)
        # job 8 waited a day for another to end (a dependency): eligible 90 seconds before it started
        elig = (start - timedelta(seconds=90)).isoformat() if jid == "8" else None
        c.execute("INSERT INTO jobs (source_site, job_id, user_name, partition, submit_time, start_time, "
                  "wait_time_seconds, state, eligible_time) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("alpha", jid, user, part, sub.isoformat(), start.isoformat(), wait, "RUNNING", elig))
    c.execute("INSERT INTO jobs (source_site, job_id, user_name, partition, submit_time, start_time, state) "
              "VALUES (?,?,?,?,?,?,?)", ("alpha", "5", "kim", "standard", t(20), "Unknown", "PENDING"))
    c.execute("INSERT INTO jobs (source_site, job_id, user_name, partition, submit_time, start_time, "
              "wait_time_seconds, state) VALUES (?,?,?,?,?,?,?,?)",
              ("alpha", "6", "kim", "standard", t(3000), t(2900), 6000, "COMPLETED"))      # two days ago
    for k in range(5):                                      # filesystems hourly
        for path, used in (("/home", 40), ("/scratch", 70)):
            c.execute("INSERT INTO filesystems (source_site, timestamp, path, total_bytes, used_bytes, "
                      "available_bytes, used_percent) VALUES (?,?,?,?,?,?,?)",
                      ("alpha", t(20 + 60 * k), path, 100 * 10 ** 9, used * 10 ** 9, (100 - used) * 10 ** 9, used))
    for k in range(6):                                      # beta: stopped three hours ago
        at = t(180 + 5 * k)
        for name in ("b01", "b02"):
            c.execute("INSERT INTO node_state (source_site, node_name, timestamp, state, cpus_total, cpus_alloc) "
                      "VALUES (?,?,?,?,?,?)", ("beta", name, at, "IDLE", 64, 0))
        c.execute("INSERT INTO queue_state (source_site, timestamp, partition, pending_jobs, running_jobs, "
                  "total_jobs) VALUES (?,?,?,?,?,?)", ("beta", at, "batch", 1, 1, 2))
    c.execute("INSERT INTO jobs (source_site, job_id, user_name, partition, submit_time, start_time, "
              "wait_time_seconds, state) VALUES (?,?,?,?,?,?,?,?)",
              ("beta", "7", "ann", "batch", t(200), t(190), 600, "RUNNING"))
    c.commit()
    c.close()
    return db


def page_of(db, **kw):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return M.build(conn, NOW, s=M.Settings(site="here", **kw))
    finally:
        conn.close()


def values(page, name, **match):
    return {tuple(sorted(lab.items())): v for lab, v in page.samples.get(name, [])
            if all(lab.get(k) == x for k, x in match.items())}


def one(page, name, **match):
    got = values(page, name, **match)
    assert len(got) == 1, (name, match, got)
    return next(iter(got.values()))


def test_the_page_is_prometheus_text(hub):
    parser = pytest.importorskip("prometheus_client.parser")
    text = M.render(page_of(hub))
    families = {f.name: f for f in parser.text_string_to_metric_families(text)}
    for name in ("nomad_build_info", "nomad_site_last_pull_timestamp_seconds", "nomad_node_condition",
                 "nomad_node_cpus", "nomad_node_cpu_clock_ratio", "nomad_node_power_problems",
                 "nomad_gpu_busy_ratio", "nomad_partition_jobs", "nomad_partition_wait_median_seconds",
                 "nomad_filesystem_used_bytes", "nomad_readings_too_old",
                 "nomad_collector_last_success_timestamp_seconds"):
        assert name in families, name
        assert families[name].type == "gauge"
    assert set(families) <= set(M.CATALOG)
    assert text.endswith("\n") and "\n\n" not in text


def test_nodes_now_and_the_ones_left_out(hub):
    p = page_of(hub)
    cond = {lab["node"]: lab["condition"] for lab, v in p.samples["nomad_node_condition"] if v == 1}
    assert cond == {"n01": "up", "n02": "up", "n03": "draining", "n04": "down"}
    assert len(values(p, "nomad_node_condition", node="n01")) == 4          # one-hot: every condition said
    assert one(p, "nomad_node_cpus_allocated", node="n02") == 16
    assert not values(p, "nomad_node_cpus", node="n09")                     # not in alpha's latest reading
    assert not values(p, "nomad_node_cpus", site="beta")                    # beta stopped: a gap, not old figures
    assert one(p, "nomad_readings_too_old", site="beta", source="node_state") == 2
    assert one(p, "nomad_readings_too_old", site="alpha", source="node_state") == 0
    # the pull and the collectors say why
    assert one(p, "nomad_site_last_pull_timestamp_seconds", site="beta") == pytest.approx(
        (NOW - timedelta(minutes=180)).timestamp())
    ran = one(p, "nomad_collector_last_run_timestamp_seconds", site="alpha", collector="gpu")
    good = one(p, "nomad_collector_last_success_timestamp_seconds", site="alpha", collector="gpu")
    assert ran == (NOW - timedelta(minutes=2)).timestamp() and good == (NOW - timedelta(minutes=17)).timestamp()


def test_clock_and_power(hub):
    p = page_of(hub)
    assert one(p, "nomad_node_cpu_clock_ratio", node="n01") == 0.98
    assert one(p, "nomad_node_cpu_throttled", node="n03") == 1
    assert not values(p, "nomad_node_cpu_clock_ratio", node="n02")          # read three hours ago
    assert one(p, "nomad_readings_too_old", site="alpha", source="cpu_clock") == 1
    parts = {lab["part"]: v for lab, v in p.samples["nomad_node_power_problems"] if lab["node"] == "n02"}
    assert parts == {"power_supply": 2, "voltage": 0, "fans": 1}             # a sensor's and the host's own
    assert set(values(p, "nomad_node_power_problems", node="n01").values()) == {0}
    assert not values(p, "nomad_node_power_problems", node="n04")           # its board can't be read...
    assert one(p, "nomad_node_power_watched", node="n04") == 0              # ...so it is said: not watched
    assert one(p, "nomad_node_power_watched", node="n02") == 1
    assert not values(p, "nomad_node_power_watched", node="n05")            # two hours old: a gap
    assert one(p, "nomad_node_power_watched", node="n06") == 1              # 1.5 x 10 + 30 + the file's 20
    assert one(p, "nomad_readings_too_old", site="alpha", source="power") == 1


def test_gpus_busy_only_from_dcgm(hub):
    p = page_of(hub)
    assert one(p, "nomad_gpu_busy_ratio", node="g01", gpu="0") == 0.75
    assert not values(p, "nomad_gpu_busy_ratio", node="g01", gpu="1")       # nvidia-smi only: not measured
    assert one(p, "nomad_gpu_memory_total_bytes", node="g01", gpu="1") == 16384 * 2 ** 20
    assert one(p, "nomad_gpu_power_watts", node="g01", gpu="0") == 120.5
    assert not values(p, "nomad_gpu_memory_total_bytes", node="g02")       # quiet for five hours
    assert one(p, "nomad_readings_too_old", site="alpha", source="gpu_stats") == 1


def test_partitions_and_waits(hub):
    p = page_of(hub)
    assert one(p, "nomad_partition_jobs", partition="standard", state="pending") == 12
    assert one(p, "nomad_partition_jobs", partition="gpu", state="running") == 4
    # 60, 120, 600 and job 8's 90 from when it could start (not its day waiting for another job)
    assert one(p, "nomad_partition_wait_median_seconds", partition="standard") == 105
    assert one(p, "nomad_partition_jobs_started", partition="standard") == 4   # not the one two days ago
    assert one(p, "nomad_partition_wait_median_seconds", partition="gpu") == 45  # from its times
    assert not values(p, "nomad_partition_jobs", site="beta")
    assert not values(p, "nomad_partition_wait_median_seconds", site="beta")  # beta isn't current
    assert one(p, "nomad_readings_too_old", site="beta", source="queue_state") == 1
    assert one(p, "nomad_filesystem_used_bytes", path="/scratch") == 70 * 10 ** 9
    assert one(p, "nomad_filesystem_avail_bytes", path="/home") == 60 * 10 ** 9


def test_no_one_is_named(hub):
    text = M.render(page_of(hub))
    for word in ("kim", "pat", "ann", "asked"):
        assert not re.search(rf"\b{word}\b", text), word
    labels = {k for lab, _ in (x for v in page_of(hub).samples.values() for x in v) for k in lab}
    assert labels <= {"version", "site", "collector", "source", "node", "condition", "part", "gpu",
                      "partition", "state", "path"}
    lines = M.summary(page_of(hub))
    joined = "\n".join(lines)
    for word in ("n01", "g01", "standard", "/home", "kim"):
        assert not re.search(rf"\b{re.escape(word)}\b", joined), word
    assert "beta, node_state: 2" in joined


def test_grace_counts_and_a_wider_one_keeps_beta(hub):
    p = page_of(hub, grace_minutes=240)
    assert values(p, "nomad_node_cpus", site="beta")
    assert one(p, "nomad_readings_too_old", site="beta", source="node_state") == 0


def test_text_format_escapes_and_numbers():
    p = M.Page(generated_at=NOW)
    p.add("nomad_filesystem_size_bytes", {"site": 'a"b', "path": "C:\\x\ny"}, 10 ** 12)
    p.add("nomad_node_cpu_clock_ratio", {"site": "s", "node": "n"}, 0.123456789)
    p.add("nomad_node_cpus", {"site": "s", "node": "n"}, float("nan"))
    text = M.render(p)
    assert 'nomad_filesystem_size_bytes{site="a\\"b",path="C:\\\\x\\ny"} 1000000000000' in text
    assert 'nomad_node_cpu_clock_ratio{site="s",node="n"} 0.123457' in text
    assert "nomad_node_cpus" not in text


def test_settings():
    s = M.settings({})
    assert (s.enabled, s.host, s.port, s.grace_minutes, s.token_file) == (False, "127.0.0.1", 10062, 30.0, None)
    assert M.settings({"hub": {"sync_interval_minutes": 5}}).grace_minutes == 15
    s = M.settings({"metrics": {"enabled": "yes", "listen": "nowhere", "wait_hours": 0, "colour": 1}})
    assert not s.enabled and s.listen == M.DEFAULT_LISTEN and s.wait_hours == 24
    assert len(s.problems) == 4
    assert M.split_listen("[::1]:9000") == ("::1", 9000) and M.split_listen(":9000") == ("0.0.0.0", 9000)
    assert M.loopback("127.0.0.1") and M.loopback("::1") and M.loopback("localhost")
    assert not M.loopback("0.0.0.0") and not M.loopback("10.0.0.5")


def test_the_token_file(tmp_path):
    p = tmp_path / "token"
    s = M.Settings(token_file=p)
    with pytest.raises(ValueError):
        M.read_token(s)                                     # missing
    p.write_text("a" * 40 + "\n")
    p.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        M.read_token(s)
    p.chmod(0o600)
    assert M.read_token(s) == "a" * 40
    p.write_text("short")
    with pytest.raises(ValueError):
        M.read_token(s)


def _get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read().decode()


def test_the_server_asks_for_its_token_and_reuses_the_page():
    built = []

    def page():
        built.append(1)
        return "# HELP x y\n"
    server = M.make_server(page, "127.0.0.1", 0, token="t" * 32, cache_seconds=60)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert _get(f"{base}/metrics")[0] == 401
        assert _get(f"{base}/metrics", "wrong" * 8)[0] == 401
        code, ctype, body = _get(f"{base}/metrics", "t" * 32)
        assert (code, ctype, body) == (200, M.CONTENT_TYPE, "# HELP x y\n")
        assert _get(f"{base}/metrics", "t" * 32)[0] == 200
        assert len(built) == 1                               # within cache_seconds: the same page
        assert _get(f"{base}/other")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_command_line(hub, tmp_path, monkeypatch):
    from nomad import metrics_cli as C
    from nomad.cli import cli
    monkeypatch.setattr(C, "_settings", lambda ctx: M.Settings(site="here"))
    r = CliRunner().invoke(cli, ["metrics", "--db", str(hub), "--summary"])
    assert r.exit_code == 0, r.output
    assert "Serving: off" in r.output and "n01" not in r.output and "standard" not in r.output
    out = tmp_path / "nomad.prom"
    r = CliRunner().invoke(cli, ["metrics", "--db", str(hub), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.read_text().startswith("# HELP nomad_build_info") and oct(out.stat().st_mode)[-3:] == "644"
    assert not list(tmp_path.glob(".nomad.prom.*"))
    r = CliRunner().invoke(cli, ["metrics", "serve", "--db", str(hub)])
    assert r.exit_code != 0 and "serving is off" in r.output
    monkeypatch.setattr(C, "_settings", lambda ctx: M.Settings(enabled=True, host="0.0.0.0"))
    r = CliRunner().invoke(cli, ["metrics", "serve", "--db", str(hub)])
    assert r.exit_code != 0 and "token_file" in r.output
    r = CliRunner().invoke(cli, ["metrics", "--catalog"])
    assert r.exit_code == 0 and r.output.count("nomad_") == len(M.CATALOG)


def test_times_written_with_a_space_across_midnight():
    """Python's own datetime adapter writes a space, not a T: just after
    midnight, a bound written with a T would leave the day's readings out."""
    now = datetime(2026, 11, 17, 0, 10)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE node_state (node_name TEXT, timestamp TEXT, state TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER)")
    c.execute("CREATE TABLE collection_log (collector TEXT, started_at TEXT, success INTEGER)")
    for k in range(12):
        at = (now - timedelta(minutes=5 * k + 2)).isoformat(sep=" ")
        c.execute("INSERT INTO node_state VALUES (?,?,?,?,?)", ("n1", at, "IDLE", 8, 0))
        c.execute("INSERT INTO collection_log VALUES (?,?,?)", ("node_state", at, 1))
    p = M.build(c, now, s=M.Settings(site="here"))
    assert one(p, "nomad_node_cpus", site="here", node="n1") == 8
    assert one(p, "nomad_collector_last_run_timestamp_seconds", collector="node_state") == (
        now - timedelta(minutes=2)).timestamp()
    assert one(p, "nomad_readings_too_old", source="node_state") == 0


# -- 1.53.0: the Grafana dashboard ---------------------------------------------------------------

def test_the_dashboard_asks_only_for_what_nomad_publishes():
    import re as _re

    from nomad.metrics_dashboard import dashboard, exprs
    d = dashboard()
    names = set()
    for e in exprs(d):
        names |= set(_re.findall(r"\bnomad_[a-z_]+\b", e))
    assert names and names <= set(M.CATALOG), names - set(M.CATALOG)
    variable = next(v for v in d["templating"]["list"] if v["name"] == "site")
    assert _re.search(r"label_values\((nomad_[a-z_]+), site\)", variable["definition"]).group(1) in M.CATALOG
    for e in exprs(d):
        assert 'site=~"$site"' in e, e                # every panel follows the site chosen


def test_the_dashboards_queries_parse():
    promql = pytest.importorskip("promql_parser")
    from nomad.metrics_dashboard import exprs
    for e in exprs():
        promql.parse(e.replace("$site", ".*"))


def test_the_dashboards_panels_fit_and_dont_overlap():
    from nomad.metrics_dashboard import dashboard
    d = dashboard()
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids))
    cells = {}
    for p in d["panels"]:
        g = p["gridPos"]
        assert 0 <= g["x"] and g["x"] + g["w"] <= 24 and g["h"] >= 1
        for x in range(g["x"], g["x"] + g["w"]):
            for y in range(g["y"], g["y"] + g["h"]):
                assert (x, y) not in cells, (p["title"], cells.get((x, y)))
                cells[(x, y)] = p["title"]
        if p["type"] != "row":
            assert p["description"] and p["datasource"]["uid"] == "${datasource}"
            for t in p["targets"]:
                assert t["datasource"]["uid"] == "${datasource}"
            assert p["fieldConfig"]["defaults"].get("custom", {}).get("spanNulls", False) is False


def test_the_dashboard_command(tmp_path):
    from nomad.cli import cli
    r = CliRunner().invoke(cli, ["metrics", "dashboard"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["uid"] == "nomad-hpc" and d["title"] == "NØMAÐ"
    out = tmp_path / "nomad.json"
    r = CliRunner().invoke(cli, ["metrics", "dashboard", "--out", str(out)])
    assert r.exit_code == 0 and json.loads(out.read_text(encoding="utf-8")) == d
