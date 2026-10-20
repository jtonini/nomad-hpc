"""nomad 1.14.5: the hub says when a site that still syncs has stopped
collecting -- the whole site, one collector, a GPU node, or DCGM on a node.

A site's collection stopped for a month after maintenance, a GPU node's DCGM
answered nothing for three months (only nvidia-smi), a node went silent for
four days: the site kept syncing, so no alert, and the pages showed partial
data as if complete.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nomad.collection_health import (collection_alerts, collection_problems,
                                     disabled_collectors, masked)

NOW = datetime(2026, 10, 19, 12, 0)


def make_db(path, site_col=True):
    c = sqlite3.connect(path)
    s = ", source_site TEXT" if site_col else ""
    c.execute(f"CREATE TABLE collection_log (id INTEGER PRIMARY KEY, collector TEXT, started_at TEXT, "
              f"completed_at TEXT, success BOOLEAN, records_collected INTEGER, error_message TEXT{s})")
    c.execute(f"CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY, timestamp TEXT, gpu_index INTEGER, "
              f"node_name TEXT, data_source TEXT, real_util_pct REAL{s})")
    return c


def runs(c, site, collector, start, end, every_min, ok=True, msg=None, records=5):
    t = start
    while t <= end:
        c.execute("INSERT INTO collection_log (collector, started_at, success, error_message, "
                  "records_collected, source_site) VALUES (?, ?, ?, ?, ?, ?)",
                  (collector, t.isoformat(), ok, msg, records, site))
        t += timedelta(minutes=every_min)


def samples(c, site, node, start, end, every_min=30, dcgm=True):
    t = start
    while t <= end:
        c.execute("INSERT INTO gpu_stats (timestamp, gpu_index, node_name, data_source, real_util_pct, "
                  "source_site) VALUES (?, 0, ?, ?, ?, ?)",
                  (t.isoformat(), node, "dcgm" if dcgm else "nvidia-smi", 40.0 if dcgm else None, site))
        t += timedelta(minutes=every_min)


def pulled(c, **sites):
    c.execute("CREATE TABLE IF NOT EXISTS sync_sites (name TEXT, last_success TEXT)")
    for name, when in sites.items():
        c.execute("INSERT INTO sync_sites VALUES (?, ?)", (name, when.isoformat()))


def problems(c, now=NOW, **kw):
    return {p.key: p for p in collection_problems(c, now, **kw)}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "combined.db"
    c = make_db(path)
    yield path, c
    c.close()


def test_a_site_that_syncs_but_collects_nothing(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=3), NOW - timedelta(hours=3), 5)
    runs(c, "b", "disk", NOW - timedelta(days=3), NOW - timedelta(hours=30), 5)
    runs(c, "ok", "disk", NOW - timedelta(days=3), NOW - timedelta(minutes=4), 5)
    got = problems(c)
    assert set(got) == {"collection:a", "collection:b"}
    assert got["collection:a"].severity == "warning" and got["collection:b"].severity == "critical"
    assert "nothing collected since 2026-10-19 09:0" in got["collection:a"].message
    assert "3 h ago" in got["collection:a"].message and "still syncs" in got["collection:a"].message
    assert set(problems(c, config={"hub": {"alert_after_minutes": 300}})) == {"collection:b"}


def test_each_site_is_judged_as_of_its_last_pull(db):
    # The pull from a failed (cached copy), or a combined database the hub held
    # back: its data is as of then, which is not a site that stopped.
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=3), NOW - timedelta(hours=5), 5)
    runs(c, "b", "disk", NOW - timedelta(days=3), NOW - timedelta(hours=5), 5)
    pulled(c, a=NOW - timedelta(hours=5) + timedelta(minutes=2), b=NOW - timedelta(minutes=10))
    got = problems(c)
    assert list(got) == ["collection:b"]
    # Stopped well before its last pull: said, as of that pull.
    c.execute("DELETE FROM sync_sites WHERE name = 'a'")
    pulled(c, a=NOW - timedelta(hours=1))
    p = problems(c)["collection:a"]
    assert "before the hub's last pull from it, at 2026-10-19 11:00" in p.message


def test_one_collector_stopped_while_the_others_run(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=6), 5)
    got = problems(c)
    assert list(got) == ["collector:a:gpu"]
    p = got["collector:a:gpu"]
    assert p.severity == "warning" and p.subject == "collector gpu"
    assert "has not run since 2026-10-19 06:0" in p.message and "usually runs every 5 min" in p.message
    assert "quiet_collectors" in p.message


def test_a_collector_failing_every_run(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(hours=3) + timedelta(minutes=5), NOW - timedelta(minutes=2), 5,
         ok=False, msg="ssh gpu02: No route to host; see /home/alice/nomad.log")
    p = problems(c)["collector:a:gpu"]
    assert "has failed on every run since 2026-10-19 09:0" in p.message
    assert "No route to host" in p.message and "alice" not in p.message
    c2 = make_db(":memory:")
    runs(c2, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(minutes=2), 5)
    runs(c2, "a", "gpu", NOW - timedelta(minutes=7), NOW - timedelta(minutes=7), 5, ok=False,
         msg="database is locked")
    assert problems(c2) == {}                       # one failure among successes


def test_a_collector_that_never_worked_is_not_a_stop(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "iostat", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5, ok=False,
         msg="iostat not installed")
    assert problems(c) == {}


def test_a_collector_that_finds_nothing_any_more(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "workstation", NOW - timedelta(days=2), NOW - timedelta(hours=4), 5, records=12)
    runs(c, "a", "workstation", NOW - timedelta(hours=4) + timedelta(minutes=5),
         NOW - timedelta(minutes=2), 5, records=0, msg="no workstation answered")
    p = problems(c)["collector:a:workstation"]
    assert "runs but has collected nothing since 2026-10-19 08:0" in p.message
    assert p.details["metric"] == "collector_empty"
    # One that usually finds nothing (no GPU on this host) is not this.
    c.execute("DELETE FROM collection_log WHERE collector = 'workstation'")
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(minutes=2), 5, records=0,
         msg="no NVIDIA GPU here")
    assert problems(c) == {}


def test_a_daily_collector_is_judged_on_its_own_interval(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=20), NOW - timedelta(minutes=3), 60)
    runs(c, "a", "slurm_usage", NOW - timedelta(days=20), NOW - timedelta(hours=30), 24 * 60)
    assert problems(c) == {}                                    # 30 h: not three days yet
    c.execute("DELETE FROM collection_log WHERE collector = 'slurm_usage' AND started_at > ?",
              ((NOW - timedelta(days=4)).isoformat(),))
    assert "collector:a:slurm_usage" in problems(c)


def test_a_schedule_change_and_runs_by_hand_are_not_stops(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=20), NOW - timedelta(minutes=3), 5)
    # Every 5 minutes for weeks, then daily since yesterday.
    runs(c, "a", "slurm_usage", NOW - timedelta(days=20), NOW - timedelta(days=2), 5)
    runs(c, "a", "slurm_usage", NOW - timedelta(hours=45), NOW - timedelta(hours=21), 24 * 60)
    assert problems(c) == {}
    # A daily collector tried by hand eleven times this morning, every 4 min.
    runs(c, "a", "groups", NOW - timedelta(days=20), NOW - timedelta(days=1), 24 * 60)
    runs(c, "a", "groups", NOW - timedelta(hours=26), NOW - timedelta(hours=25, minutes=20), 4)
    assert problems(c) == {}
    # A collector not in cron, tried nine times three days ago.
    runs(c, "a", "node_disks", NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(minutes=40), 5)
    assert problems(c) == {}
    # Runs a few seconds apart count as one.
    runs(c, "a", "vmstat", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "vmstat", NOW - timedelta(days=2) + timedelta(seconds=10), NOW - timedelta(minutes=3), 5)
    assert problems(c) == {}


def test_quiet_and_disabled_collectors(db, tmp_path):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=6), 5)
    runs(c, "b", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "b", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=6), 5)
    assert set(problems(c)) == {"collector:a:gpu", "collector:b:gpu"}
    assert set(problems(c, config={"hub": {"quiet_collectors": ["a:gpu"]}})) == {"collector:b:gpu"}
    assert problems(c, config={"hub": {"quiet_collectors": ["gpu"]}}) == {}
    assert set(problems(c, config={"hub": {"quiet_collectors": ["b:*"]}})) == {"collector:a:gpu"}
    # Turned off in the site's own settings (the copy the hub's sync keeps).
    cache = tmp_path / "sync_cache"
    cache.mkdir()
    (cache / "b.toml").write_text("[collectors.gpu]\nenabled = false\n")
    off = disabled_collectors(cache)
    assert "gpu" in off["b"] and "disk" not in off["b"]
    assert set(problems(c, disabled=off)) == {"collector:a:gpu"}


def gpu_site(c, site="a", every=5):
    runs(c, site, "gpu", NOW - timedelta(days=10), NOW - timedelta(minutes=3), every)
    samples(c, site, "n1", NOW - timedelta(days=10), NOW - timedelta(minutes=10))


def test_a_gpu_node_that_went_quiet(db):
    _, c = db
    gpu_site(c)
    samples(c, "a", "n3", NOW - timedelta(days=10), NOW - timedelta(hours=5))
    got = problems(c)
    assert list(got) == ["gpu-node:a:n3"]
    assert "GPU node n3 has sent no samples since 2026-10-19 07:0" in got["gpu-node:a:n3"].message
    assert "quiet_nodes" in got["gpu-node:a:n3"].message
    assert problems(c, config={"hub": {"quiet_nodes": ["n3"]}}) == {}
    assert problems(c, config={"hub": {"quiet_nodes": ["a:*"]}}) == {}


def test_gpu_nodes_with_little_history_are_not_watched(db):
    _, c = db
    gpu_site(c)
    samples(c, "a", "old", NOW - timedelta(days=60), NOW - timedelta(days=31))   # retired
    samples(c, "a", "tried", NOW - timedelta(days=2), NOW - timedelta(days=2) + timedelta(hours=1))
    assert problems(c) == {}


def test_unnamed_rows_join_the_one_named_node(db):
    _, c = db
    gpu_site(c)
    samples(c, "a", "", NOW - timedelta(days=20), NOW - timedelta(days=9))     # before node names
    assert problems(c) == {}


def test_the_gpu_threshold_follows_how_often_the_gpu_collector_runs(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=10), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=10), NOW - timedelta(minutes=30), 180)
    samples(c, "a", "n1", NOW - timedelta(days=10), NOW - timedelta(hours=2, minutes=30), 180)
    samples(c, "a", "n2", NOW - timedelta(days=10), NOW - timedelta(hours=2, minutes=30), 180)
    assert problems(c) == {}                # every 3 h: 2.5 h without samples is normal


def test_dcgm_stopped_while_nvidia_smi_answers(db):
    _, c = db
    gpu_site(c)
    samples(c, "a", "n2", NOW - timedelta(days=10), NOW - timedelta(days=2))
    samples(c, "a", "n2", NOW - timedelta(days=2) + timedelta(minutes=30), NOW - timedelta(minutes=10),
            dcgm=False)
    p = problems(c)["dcgm:a:n2"]
    assert p.severity == "warning" and "no DCGM samples from GPU node n2 since 2026-10-17" in p.message
    assert "only nvidia-smi" in p.message and "not measured" in p.message
    c.execute("DELETE FROM gpu_stats WHERE node_name = 'n2' AND data_source = 'dcgm' AND timestamp > ?",
              ((NOW - timedelta(days=8)).isoformat(),))
    assert problems(c)["dcgm:a:n2"].severity == "critical"
    assert "dcgm:a:n2" not in problems(c, config={"hub": {"gpu_fallback_hours": 24 * 9}})
    c.execute("DELETE FROM gpu_stats WHERE node_name = 'n2' AND data_source = 'dcgm'")
    assert problems(c) == {}                # never had DCGM: not this


def test_all_gpu_nodes_quiet_is_one_alert_or_the_collectors(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    for n in ("n1", "n2", "n3"):
        samples(c, "a", n, NOW - timedelta(days=3), NOW - timedelta(hours=4))
    got = problems(c)
    assert list(got) == ["gpu:a"] and "no GPU samples from any node" in got["gpu:a"].message
    c.execute("DELETE FROM collection_log WHERE collector = 'gpu' AND started_at > ?",
              ((NOW - timedelta(hours=4)).isoformat(),))
    assert list(problems(c)) == ["collector:a:gpu"]


def test_a_site_that_stopped_collecting_raises_only_that(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(hours=40), 5)
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=40), 5)
    samples(c, "a", "n1", NOW - timedelta(days=2), NOW - timedelta(hours=40))
    assert list(problems(c)) == ["collection:a"]


def test_a_site_database_without_source_site(tmp_path):
    c = make_db(tmp_path / "site.db", site_col=False)
    for collector, last in (("disk", NOW - timedelta(minutes=3)), ("gpu", NOW - timedelta(hours=6))):
        t = NOW - timedelta(days=2)
        while t <= last:
            c.execute("INSERT INTO collection_log (collector, started_at, success) VALUES (?, ?, 1)",
                      (collector, t.isoformat()))
            t += timedelta(minutes=5)
    t = NOW - timedelta(days=2)
    while t <= NOW - timedelta(hours=6):
        c.execute("INSERT INTO gpu_stats (timestamp, gpu_index, node_name) VALUES (?, 0, 'n1')",
                  (t.isoformat(),))
        t += timedelta(minutes=30)
    got = problems(c)
    assert list(got) == ["collector:None:gpu"]          # not also "no GPU samples"
    assert "Site this host" in got["collector:None:gpu"].message


def test_odd_timestamps_and_rows_without_a_site(db):
    _, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW - timedelta(minutes=3), 5)
    c.execute("INSERT INTO collection_log (collector, started_at, success, source_site) "
              "VALUES ('disk', '2026-10-19T11:59:00+00:00', 1, 'a')")
    runs(c, None, "disk", NOW - timedelta(days=2), NOW - timedelta(hours=5), 5)
    got = problems(c)
    assert list(got) == ["collection:None"] and "(no site)" in got["collection:None"].message


def test_names_are_kept_out_of_messages():
    text = ("open /scratch/jdoe42/run.log failed; user 'msmith' unknown; ~msmith/.ssh; "
            "jdoe@node7 refused; KeyError('pkumar'); /home/alice/x; owner=bob")
    out = masked(text, names={"pkumar"})
    for name in ("jdoe42", "msmith", "jdoe@", "pkumar", "alice", "bob"):
        assert name not in out, name
    assert "node7" in out and "/scratch/…" in out and "refused" in out


def test_alerts_are_sent_once_then_daily_and_forgotten_on_recovery(db, tmp_path, monkeypatch):
    import nomad.alerts.dispatcher as disp
    path, c = db
    runs(c, "a", "disk", NOW - timedelta(days=2), NOW + timedelta(hours=60), 5)   # keeps running
    runs(c, "a", "gpu", NOW - timedelta(days=2), NOW - timedelta(hours=6), 5)
    c.commit()
    sent = []
    monkeypatch.setattr(disp, "send_alert", lambda *a, **k: sent.append((a, k)) or {})
    cache = tmp_path / "cache"
    cache.mkdir()

    def check(hours=0, config=None, failing=()):
        return collection_alerts(path, cache, now=NOW + timedelta(hours=hours), config=config or {},
                                 failing=failing)
    got = check()
    assert [i["key"] for i in got] == ["collector:a:gpu"]
    (severity, source, message), kw = sent[0]
    assert source == "collection" and kw["host"] == "a" and kw["subject"] == "collector gpu"
    assert check(1) == []
    assert [i["severity"] for i in check(19)] == ["critical"]
    assert check(30) == []
    assert [i["severity"] for i in check(43.5)] == ["critical"]
    assert check(44, config={"hub": {"collection_alerts": False}}) == []
    # The site's pull failed and it isn't in the database: what was sent is kept.
    c.execute("CREATE TABLE aside AS SELECT * FROM collection_log")
    c.execute("DELETE FROM collection_log")
    c.commit()
    assert check(44.2, failing={"a"}) == []
    assert "collector:a:gpu" in json.loads((cache / "collection_alerts.json").read_text())
    c.execute("INSERT INTO collection_log SELECT * FROM aside")
    c.commit()
    assert check(44.4) == []                         # not sent again as news
    # It runs again: forgotten.
    runs(c, "a", "gpu", NOW, NOW + timedelta(hours=50), 5)
    c.commit()
    assert check(50) == []
    assert json.loads((cache / "collection_alerts.json").read_text()) == {}


def test_a_database_that_cannot_be_read_raises_nothing(tmp_path):
    assert collection_alerts(tmp_path / "missing.db", config={}) == []


def test_the_command_lists_what_it_finds(db, tmp_path):
    from nomad.cli import cli
    path, c = db
    runs(c, "a", "disk", datetime.now() - timedelta(days=2), datetime.now() - timedelta(minutes=3), 5)
    runs(c, "a", "gpu", datetime.now() - timedelta(days=2), datetime.now() - timedelta(hours=6), 5)
    c.commit()
    r = CliRunner().invoke(cli, ["collection-check", "--db", str(path)], obj={})
    assert r.exit_code == 0, r.output
    assert "the gpu collector has not run since" in r.output and "warning" in r.output
    c.execute("DELETE FROM collection_log WHERE collector = 'gpu'")
    c.commit()
    r = CliRunner().invoke(cli, ["collection-check", "--db", str(path)], obj={})
    assert "every site collecting" in r.output
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    r = CliRunner().invoke(cli, ["collection-check", "--db", str(bad)], obj={})
    assert r.exit_code != 0 and "cannot read" in r.output and "Traceback" not in r.output


DCGM_ACTIVE = """\
#Entity   GRACT        SMACT        TENSO        DRAMA
ID
GPU 2     0.984        0.930        0.000        0.252
GPU 1     1.000        0.926        0.038        0.869
GPU 0     1.000        0.916        0.023        0.881
"""
NVIDIA_SMI = """\
0, NVIDIA A100-SXM4-40GB, 85, 60, 24000, 40000, 16000, 72, 280.5, 400.0
1, NVIDIA A100-SXM4-40GB, 5,  2,  1000, 40000, 39000, 45,  50.0, 400.0
2, NVIDIA A100-SXM4-40GB, 0, 0, 0, 40000, 40000, 35, 15.0, 400.0
"""


def test_dcgm_is_asked_of_each_gpu_node(tmp_path):
    from nomad.collectors.gpu import GPUCollector
    col = GPUCollector({"enabled": True, "gpu_nodes": ["n1", "n2"], "ssh_user": "x"},
                       str(tmp_path / "g.db"))
    col._gpu_available = True

    def run(cmd, host=None):
        cmd = cmd if isinstance(cmd, str) else " ".join(cmd)
        if "query-gpu" in cmd:
            return NVIDIA_SMI
        if host == "n2" and "dcgmi dmon" in cmd:
            return DCGM_ACTIVE
        return None                          # n1: no DCGM there
    with patch.object(col, "_run", side_effect=run):
        records = [r for r in col.collect() if r.get("type") == "gpu"]
    by = {r["node_name"]: r["data_source"] for r in records}
    assert by == {"n1": "nvidia-smi", "n2": "dcgm"}
