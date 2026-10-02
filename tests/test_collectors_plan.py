# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Every collector can run, and one that collects nothing says why.

On 1 October 2026 the hub's collection_log showed what the sites ran:
- storage and network_perf never ran anywhere -- `nomad collect` built a
  fixed list that left them out, so enabling them did nothing;
- nfs ran at all four sites every 5 minutes and never collected anything,
  logged as success with 0 records;
- per_user on arachne logged "1 records" every run while collecting nothing
  (psutil missing), and groups did the same through its one envelope;
- the `[collectors] enabled = [...]` list in the example config was never read.
"""
from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.collectors import plan as plan_mod
from nomad.collectors import status
from nomad.collectors.base import MissingToolError


def _config(**collectors):
    return {"collectors": collectors}


# ── What runs, and why ──────────────────────────────────────────────────

def test_every_collector_is_planned_with_its_reason():
    planned = {p.name: p for p in plan_mod.plan(_config(nfs={"enabled": False}))}
    assert set(planned) == set(plan_mod.NAMES)
    assert {"storage", "network_perf"} <= set(planned)
    assert planned["disk"].enabled and planned["disk"].why == "on by default"
    assert not planned["nfs"].enabled and planned["nfs"].why == "disabled in [collectors.nfs]"
    assert not planned["storage"].enabled
    assert planned["storage"].why == "off unless enabled in [collectors.storage]"


def test_storage_and_network_are_built_when_enabled(tmp_path):
    cfg = _config(storage={"enabled": True, "storage_devices": [{"hostname": "nas1"}]},
                  network_perf={"enabled": True, "network_tests": [{"dest": "nas1"}]})
    collectors, _ = plan_mod.build(cfg, tmp_path / "t.db", only=("storage,network_perf",))
    by_name = {c.name: c for c in collectors}
    assert set(by_name) == {"storage", "network_perf"}
    assert by_name["storage"].storage_devices == [{"hostname": "nas1"}]
    assert by_name["network_perf"].network_tests == [{"dest": "nas1"}]


def test_lists_at_the_old_top_level_place_are_read_and_flagged(tmp_path):
    cfg = {"collectors": {"network_perf": {"enabled": True}},
           "network_tests": [{"dest": "nas1"}]}
    p = {x.name: x for x in plan_mod.plan(cfg)}["network_perf"]
    assert p.config["network_tests"] == [{"dest": "nas1"}]
    assert "move it to [[collectors.network_perf.network_tests]]" in p.warnings[0]


def test_only_accepts_commas_and_refuses_unknown_names():
    assert plan_mod.parse_only(("disk,nfs", "aws")) == {"disk", "nfs", "cloud"}
    with pytest.raises(ValueError, match="unknown collector"):
        plan_mod.parse_only(("disks",))


def test_the_enabled_list_is_reported_as_unused():
    assert plan_mod.unused_enabled_list({"collectors": {"enabled": ["disk"]}}) == ["disk"]
    assert plan_mod.unused_enabled_list({"collectors": {}}) is None


# ── A run that collects nothing says why ────────────────────────────────

def _log(db: Path):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT collector, success, records_collected, error_message "
                         "FROM collection_log ORDER BY id").fetchall()
    finally:
        c.close()


@pytest.fixture
def db(tmp_path):
    from nomad.db import ensure_database
    path = tmp_path / "site.db"
    ensure_database(path)
    return path


def test_nfs_without_nfsiostat_logs_the_reason(db, monkeypatch):
    from nomad.collectors import nfs
    monkeypatch.setattr(nfs, "find_tool", lambda name: None)
    r = nfs.NFSCollector({}, db).run()
    assert r.success and r.records_collected == 0
    assert r.note == "nfsiostat not installed (nfs-utils)"
    assert _log(db)[-1] == ("nfs", 1, 0, "nfsiostat not installed (nfs-utils)")


def test_a_missing_tool_fails_at_once_without_retries(db, monkeypatch):
    from nomad.collectors.iostat import IOStatCollector
    monkeypatch.setenv("PATH", "/nonexistent")
    c = IOStatCollector({"retry_delay": 30}, db)
    t0 = datetime.now()
    r = c.run()
    assert (datetime.now() - t0).total_seconds() < 5
    assert not r.success and "install sysstat" in r.error_message
    assert isinstance(MissingToolError("x"), Exception)


def test_per_user_without_psutil_says_so_instead_of_one_record(db, monkeypatch):
    from nomad.collectors.per_user import collector as pu
    monkeypatch.setattr(pu, "psutil", None)
    r = pu.PerUserCollector({"enabled": True, "role": "headnode"}, db).run()
    assert r.success and r.records_collected == 0
    assert r.note == "psutil not installed: nothing collected"


def test_per_user_counts_what_its_envelope_holds(db):
    from nomad.collectors.per_user import collector as pu
    c = pu.PerUserCollector({"enabled": True}, db)
    env = pu._envelope([{"x": 1}] * 3, [{"a": 1}], [], evicted=0)
    assert c.count_records(env) == 4


def test_groups_without_clusters_reads_membership_here(db, monkeypatch):
    from nomad.collectors.groups import GroupCollector
    c = GroupCollector({"clusters": {}, "local_name": "spiderweb"}, db)
    monkeypatch.setattr(c, "_run_cmd", lambda cmd, *a, **k:
                        "lab$:x:5001:ann,bob\nusers:x:100:ann" if cmd == "getent group" else None)
    monkeypatch.setattr("shutil.which", lambda name: None)      # no sacct here
    r = c.run()
    assert r.success and r.records_collected == 2               # two memberships, not 1 envelope
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT username, group_name, cluster FROM group_membership "
                        "ORDER BY username").fetchall()
    conn.close()
    assert rows == [("ann", "lab$", "spiderweb"), ("bob", "lab$", "spiderweb")]


def test_groups_with_nothing_read_says_so(db, monkeypatch):
    from nomad.collectors.groups import GroupCollector
    c = GroupCollector({"clusters": {}, "local_name": "x"}, db)
    monkeypatch.setattr(c, "_run_cmd", lambda *a, **k: None)
    r = c.run()
    assert r.records_collected == 0 and "getent group returned no groups" in r.note


# ── Network ─────────────────────────────────────────────────────────────

@pytest.fixture
def net(db, monkeypatch):
    from nomad.collectors import network_perf as npf
    calls = {"ping": [], "iperf": []}

    def ping(host, count=10):
        calls["ping"].append(host)
        return npf.PingStats(1.0, 2.0, 3.0, 0.5, 0.0)

    def iperf(host, duration=10):
        calls["iperf"].append(host)
        return npf.ThroughputStats(10**9, 940.0, duration, 0)
    monkeypatch.setattr(npf, "measure_ping", ping)
    monkeypatch.setattr(npf, "measure_throughput_iperf", iperf)
    monkeypatch.setattr(npf, "measure_throughput_ssh", lambda *a, **k: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    return npf, calls


def test_network_pings_by_default_and_never_from_another_host(db, net):
    npf, calls = net
    c = npf.NetworkPerfCollector({"network_tests": [
        {"dest": "nas1", "path_type": "switch"},
        {"source": "some-other-host", "dest": "nas2"},
        {"dest": "x; rm -rf /"},
    ]}, db)
    r = c.run()
    assert calls == {"ping": ["nas1"], "iperf": []}             # ping-only unless asked
    assert r.records_collected == 1
    assert "some-other-host->nas2: starts on another host" in r.note
    assert "not a valid host name" in r.note
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT dest_host, status, ping_avg_ms, throughput_mbps "
                       "FROM network_perf").fetchone()
    conn.close()
    assert row == ("nas1", "healthy", 2.0, None)


def test_throughput_at_most_once_per_interval_across_runs(db, net):
    npf, calls = net
    cfg = {"throughput": True, "throughput_interval": 3600, "network_tests": [{"dest": "nas1"}]}
    npf.NetworkPerfCollector(cfg, db).run()
    npf.NetworkPerfCollector(cfg, db).run()                     # a new process, as under cron
    assert calls["iperf"] == ["nas1"] and calls["ping"] == ["nas1", "nas1"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE network_perf SET timestamp = ? WHERE throughput_mbps IS NOT NULL",
                 ((datetime.now() - timedelta(hours=2)).isoformat(),))
    conn.commit(); conn.close()
    npf.NetworkPerfCollector(cfg, db).run()
    assert calls["iperf"] == ["nas1", "nas1"]


def test_network_with_nothing_configured_or_no_ping(db, net, monkeypatch):
    npf, _ = net
    assert npf.NetworkPerfCollector({}, db).run().note == "no network_tests configured"
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = npf.NetworkPerfCollector({"network_tests": [{"dest": "nas1"}]}, db).run()
    assert r.note == "ping not installed" and r.records_collected == 0


def test_an_unanswered_ping_is_unreachable(db, net, monkeypatch):
    npf, _ = net
    monkeypatch.setattr(npf, "measure_ping", lambda h, c=10: npf.PingStats(loss_pct=100.0))
    npf.NetworkPerfCollector({"network_tests": [{"dest": "nas1"}]}, db).run()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM network_perf").fetchone() == ("unreachable",)
    conn.close()


# ── Storage ─────────────────────────────────────────────────────────────

def test_storage_without_devices_says_so(db):
    from nomad.collectors.storage import StorageCollector
    r = StorageCollector({}, db).run()
    assert r.note == "no storage_devices configured" and r.records_collected == 0


def test_an_unreachable_server_is_offline_with_unknown_capacity(db, monkeypatch):
    from nomad.collectors import storage as st

    def fake_run(argv, shell=False, **k):
        return subprocess.CompletedProcess(argv, 255, "", "ssh: connect: No route to host")
    monkeypatch.setattr(st.subprocess, "run", fake_run)
    st.StorageCollector({"storage_devices": [{"hostname": "nas9", "type": "zfs"}]}, db).run()
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT hostname, status, total_bytes, usage_pct FROM storage_state").fetchone()
    conn.close()
    assert row == ("nas9", "offline", None, None)


def test_storage_never_reports_a_servers_root_disk(db, monkeypatch):
    from nomad.collectors import storage as st
    seen = []

    def fake_run(argv, shell=False, **k):
        cmd = argv if isinstance(argv, str) else argv[-1]
        seen.append(cmd)
        if cmd.startswith("df -B1 -P '/export/home'"):
            return subprocess.CompletedProcess(argv, 0, "/dev/x 1000 600 400 60% /export/home", "")
        if cmd == "true":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "not here")   # no zpool, no exportfs
    monkeypatch.setattr(st.subprocess, "run", fake_run)
    st.StorageCollector({"storage_devices": [
        {"hostname": "nfs1", "type": "nfs"},
        {"hostname": "nfs2", "type": "nfs", "paths": ["/export/home"]},
    ]}, db).run()
    assert not [c for c in seen if c.startswith("df -B1 /")]
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT hostname, usage_pct FROM storage_state").fetchall())
    conn.close()
    assert rows == {"nfs1": None, "nfs2": 60.0}


def test_storage_refuses_a_host_name_a_shell_would_read(db):
    from nomad.collectors import storage as st
    with pytest.raises(st.CollectionError, match="not a valid host name"):
        st.run_command("true", "nas1;reboot")


# ── nomad collectors ───────────────────────────────────────────────────

def _write_log(db, rows, site_col=False):
    c = sqlite3.connect(db)
    if site_col:
        c.execute("ALTER TABLE collection_log ADD COLUMN source_site TEXT")
    for r in rows:
        c.execute("INSERT INTO collection_log (collector, started_at, completed_at, success, "
                  "records_collected, error_message" + (", source_site" if site_col else "")
                  + ") VALUES (?, ?, ?, ?, ?, ?" + (", ?" if site_col else "") + ")", r)
    c.commit(); c.close()


def test_status_tells_working_from_running_with_nothing(db):
    now = datetime.now()
    rows = []
    for i in range(3):
        t = (now - timedelta(minutes=5 * i)).isoformat()
        rows += [("disk", t, t, 1, 4, None), ("nfs", t, t, 1, 0, "nfsiostat not installed"),
                 ("iostat", t, t, 0, 0, "iostat not found - install sysstat package")]
    _write_log(db, rows)
    conn = sqlite3.connect(db)
    st = status.read(conn)
    conn.close()
    assert st[(None, "disk")].summary().startswith("3 runs, all with data")
    assert st[(None, "nfs")].summary().startswith("3 runs, never any data")
    assert st[(None, "nfs")].last_message == "nfsiostat not installed"
    assert st[(None, "iostat")].summary().startswith("3 runs, all failed")
    assert not st[(None, "nfs")].working and st[(None, "disk")].working


def test_nomad_collectors_on_a_site(tmp_path, db):
    from nomad.cli import cli
    t = datetime.now().isoformat()
    _write_log(db, [("nfs", t, t, 1, 0, "nfsiostat not installed (nfs-utils)")])
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[database]\npath = "{db}"\n\n[collectors]\nenabled = ["disk"]\n\n'
                   '[collectors.storage]\nenabled = true\n')
    out = CliRunner().invoke(cli, ["-c", str(cfg), "collectors", "--db", str(db)]).output
    assert "nfs           on   on by default" in out
    assert "never any data" in out and "nfsiostat not installed" in out
    assert "storage       on   enabled in [collectors.storage]" in out
    assert "needs: a storage_devices list in [collectors.storage]" in out
    assert "[collectors] enabled = ['disk'] is not read" in out


def test_nomad_collectors_on_the_hub_shows_every_site(tmp_path, db):
    from nomad.cli import cli
    t = datetime.now().isoformat()
    _write_log(db, [("disk", t, t, 1, 4, None, "arachne"),
                    ("nfs", t, t, 1, 0, "no NFS mounts on this host", "arachne"),
                    ("disk", t, t, 1, 2, None, "spiderweb")], site_col=True)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[collectors]\n")
    out = CliRunner().invoke(cli, ["-c", str(cfg), "collectors", "--db", str(db)]).output
    assert "arachne" in out and "spiderweb" in out
    assert "no NFS mounts on this host" in out
    assert "not run:" in out


def test_wizard_keeps_groups_on_for_workstation_sites():
    import inspect
    from nomad import cli as cli_mod
    src = inspect.getsource(cli_mod)
    assert 'lines.append("[collectors.groups]")\n    lines.append("enabled = true")' in src


# ── From the review ─────────────────────────────────────────────────────

def test_diagnostics_read_rows_the_collectors_now_store(db, net, monkeypatch):
    """Ping-only rows have no throughput or retransmits; an offline server
    has no capacity. `nomad diag network` / `nas` must read them."""
    npf, _ = net
    npf.NetworkPerfCollector({"network_tests": [{"dest": "nas1"}]}, db).run()
    from nomad.diag.network import diagnose_network, format_diagnostic
    import socket
    d = diagnose_network(str(db), source=socket.gethostname(), dest="nas1")
    assert d is not None and "nas1" in format_diagnostic(d)

    from nomad.collectors import storage as st
    monkeypatch.setattr(st.subprocess, "run", lambda argv, shell=False, **k:
                        subprocess.CompletedProcess(argv, 255, "", "unreachable"))
    st.StorageCollector({"storage_devices": [{"hostname": "nas9"}]}, db).run()
    from nomad.diag.storage import diagnose_storage, format_diagnostic as fmt
    s = diagnose_storage(str(db), "nas9")
    assert s is not None and "nas9" in fmt(s)


def test_iperf3_without_a_server_is_no_reading(monkeypatch):
    from nomad.collectors import network_perf as npf
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    out = '{"start": {}, "intervals": [], "end": {}, "error": "unable to connect to server"}'
    monkeypatch.setattr(npf.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 1, out, ""))
    assert npf.measure_throughput_iperf("nas1", 1) is None


def test_one_stale_name_in_minus_c_does_not_stop_collection():
    assert plan_mod.parse_only(("disk", "nodes")) == {"disk"}
    with pytest.raises(ValueError):
        plan_mod.parse_only(("nodes", "licenses"))


def test_the_aws_collector_is_found_under_the_name_it_logs(tmp_path, db):
    from nomad.cli import cli
    t = datetime.now().isoformat()
    _write_log(db, [("aws", t, t, 1, 5, None)])
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[collectors.cloud.aws]\nenabled = true\n")
    out = CliRunner().invoke(cli, ["-c", str(cfg), "collectors", "--db", str(db)]).output
    assert "1 run, all with data" in out


def test_two_paths_on_one_filesystem_count_once(db, monkeypatch):
    from nomad.collectors import storage as st

    def fake_run(argv, shell=False, **k):
        cmd = argv if isinstance(argv, str) else argv[-1]
        if cmd.startswith("df -B1 -P"):
            return subprocess.CompletedProcess(argv, 0, "/dev/md0 1000 600 400 60% /export", "")
        if cmd == "true":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr(st.subprocess, "run", fake_run)
    st.StorageCollector({"storage_devices": [
        {"hostname": "nfs2", "type": "nfs", "paths": ["/export", "/export/home"]}]}, db).run()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT total_bytes FROM storage_state").fetchone() == (1000,)
    conn.close()


def test_a_test_whose_source_is_this_hosts_address_runs(db, net):
    npf, calls = net
    npf.NetworkPerfCollector({"network_tests": [{"source": "127.0.0.1", "dest": "nas1"},
                                                {"source": "192.0.2.77", "dest": "nas2"}]},
                             db).run()
    assert calls["ping"] == ["nas1"]


def test_workstation_clusters_without_an_ssh_account_read_membership_here(db, monkeypatch):
    from nomad.collectors.groups import GroupCollector
    c = GroupCollector({"clusters": {"ws": {"name": "jonimitchell", "type": "workstations",
                                            "partitions": {"bio": {"nodes": ["ws1"]}}}}}, db)
    calls = []

    def run_cmd(cmd, host=None, *a, **k):
        calls.append(host)
        return "lab$:x:5001:ann,bob"
    monkeypatch.setattr(c, "_run_cmd", run_cmd)
    r = c.run()
    assert calls == [None] and r.records_collected == 2



# ── 1.7.14: tools outside cron's PATH; the newest run's reason ─────────

def test_nfsiostat_in_usr_sbin_is_found_under_crons_path(db, tmp_path, monkeypatch):
    """nfs-utils puts nfsiostat in /usr/sbin; cron's PATH is /usr/bin:/bin.
    All four UR sites logged "nfsiostat not installed" with it installed."""
    from nomad.collectors import base, nfs
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    tool = sbin / "nfsiostat"
    tool.write_text("#!/bin/sh\necho\n")
    tool.chmod(0o755)
    monkeypatch.setattr(base, "EXTRA_TOOL_DIRS", (str(sbin),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert base.find_tool("nfsiostat") == str(tool)
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: real_open(
        tmp_path / "mounts" if f == "/proc/mounts" else f, *a, **k))
    (tmp_path / "mounts").write_text("srv:/home /home nfs4 rw 0 0\n")
    r = nfs.NFSCollector({}, db).run()
    assert r.note == "nfsiostat printed nothing nomad could read"   # ran it, from sbin


def test_the_hub_shows_why_a_collector_stopped_collecting(tmp_path, db):
    """per_user on arachne: data all week from the old version's envelope,
    nothing since the upgrade -- the newest run's reason is shown."""
    from nomad.cli import cli
    now = datetime.now()
    rows = [("per_user", (now - timedelta(hours=h)).isoformat(), None, 1, 1, None, "arachne")
            for h in range(30, 40)]
    rows += [("per_user", (now - timedelta(hours=h)).isoformat(), None, 1, 0,
              "psutil not installed: nothing collected", "arachne") for h in range(0, 5)]
    _write_log(db, rows, site_col=True)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[collectors]\n")
    out = CliRunner().invoke(cli, ["-c", str(cfg), "collectors", "--db", str(db)]).output
    assert "15 runs, 10 with data" in out
    assert "now: psutil not installed: nothing collected" in out


NFSIOSTAT_TWO_REPORTS = """
srv1:/export/home mounted on /home:

           ops/s       rpc bklog
         512.000           0.000

read:              ops/s            kB/s           kB/op         retrans    avg RTT (ms)    avg exe (ms)  avg queue (ms)          errors
                 200.000        9000.000          45.000        0 (0.0%)           1.000           1.100           0.020        0 (0.0%)
write:             ops/s            kB/s           kB/op         retrans    avg RTT (ms)    avg exe (ms)  avg queue (ms)          errors
                 100.000        5000.000          50.000        0 (0.0%)           3.000           3.200           0.022        0 (0.0%)

srv1:/export/scratch mounted on /scratch:

           ops/s       rpc bklog
           1.000           0.000

read:             ops/s            kB/s           kB/op         retrans         avg RTT (ms)    avg exe (ms)
                  0.000           0.000           0.000        0 (0.0%)           0.000           0.000
write:            ops/s            kB/s           kB/op         retrans         avg RTT (ms)    avg exe (ms)
                  0.000           0.000           0.000        0 (0.0%)           0.000           0.000

srv1:/export/home mounted on /home:

           ops/s       rpc bklog
          19.857           0.000

read:              ops/s            kB/s           kB/op         retrans    avg RTT (ms)    avg exe (ms)  avg queue (ms)          errors
                   2.000         128.000          64.000        3 (1.5%)           1.000           1.200           0.020        0 (0.0%)
write:             ops/s            kB/s           kB/op         retrans    avg RTT (ms)    avg exe (ms)  avg queue (ms)          errors
                   2.000          58.000          29.000        0 (0.0%)           3.000           3.400           0.022        0 (0.0%)
"""


def test_nfsiostat_is_read_as_it_prints_the_latest_report_per_mount(db, tmp_path, monkeypatch):
    """The parser expected one 8-number line per mount and never matched
    real output; and `nfsiostat 1 1` gave since-mount averages."""
    from nomad.collectors import base, nfs
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    out = tmp_path / "report.txt"
    out.write_text(NFSIOSTAT_TWO_REPORTS)
    args = tmp_path / "args.txt"
    tool = sbin / "nfsiostat"
    tool.write_text(f'#!/bin/sh\necho "$@" > {args}\ncat {out}\n')
    tool.chmod(0o755)
    monkeypatch.setattr(base, "EXTRA_TOOL_DIRS", (str(sbin),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: real_open(
        tmp_path / "mounts" if f == "/proc/mounts" else f, *a, **k))
    (tmp_path / "mounts").write_text("srv1:/export/home /home nfs4 rw 0 0\n")

    r = nfs.NFSCollector({}, db).run()
    assert args.read_text().split() == ["5", "2"]          # a 5 s interval report
    assert r.success and r.records_collected == 2 and r.note is None
    conn = sqlite3.connect(db)
    rows = {m: rest for m, *rest in conn.execute(
        "SELECT mount_point, server, ops_per_sec, read_kb_per_sec, write_kb_per_sec, "
        "avg_rtt_ms, retrans_percent FROM nfs_stats")}
    conn.close()
    # /home from the second report (19.857 ops/s), not the since-mount one (512)
    assert rows["/home"] == ["srv1:/export/home", 19.857, 128.0, 58.0, 2.0, 0.75]
    # an idle mount: rates 0, latency unknown rather than 0 ms
    assert rows["/scratch"][1:] == [1.0, 0.0, 0.0, None, None]
