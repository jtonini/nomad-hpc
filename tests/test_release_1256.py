"""1.25.6: hardware temperatures (the temps collector, [alerts.temps], nomad temps);
[alerts.node_state] and [alerts.node_jobs] reach the node_state collector."""
import socket
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.collectors import temps as T

H = socket.gethostname().split(".")[0]          # what a host read here is kept as


def _chip(root: Path, n: int, name: str, device: str, sensors: dict) -> None:
    d = root / f"hwmon{n}"
    (d / "dev").mkdir(parents=True)
    (d / "name").write_text(name + "\n")
    (d / "device").symlink_to(root / "devices" / device) if (root / "devices").exists() else None
    for i, (label, milli) in enumerate(sensors.items(), start=1):
        (d / f"temp{i}_input").write_text(f"{milli}\n")
        if not label.startswith("temp"):
            (d / f"temp{i}_label").write_text(label + "\n")


@pytest.fixture
def peters(tmp_path):
    """peterslabworkstation's sensors, as /sys/class/hwmon shows them."""
    root = tmp_path / "hwmon"
    for dev in ("0000:00:18.3", "nvme0", "nvme1", "0000:0b:00.0", "acpi"):
        (root / "devices" / dev).mkdir(parents=True)
    _chip(root, 0, "k10temp", "0000:00:18.3", {"Tctl": 72625, "Tccd1": 66000, "Tccd2": 45250})
    _chip(root, 1, "nvme", "nvme0", {"Composite": 41850})
    _chip(root, 2, "nvme", "nvme1", {"Composite": 38850})
    _chip(root, 3, "amdgpu", "0000:0b:00.0", {"edge": 44000})
    _chip(root, 4, "acpitz", "acpi", {"temp1": 0})                  # not a reading
    return root


def _read(root) -> str:
    import subprocess
    return subprocess.run(["sh", "-c", T.remote_command(str(root))], capture_output=True, text=True).stdout


def test_one_line_of_sh_reads_every_sensor(peters):
    cmd = T.remote_command(str(peters))
    assert "\n" not in cmd and "!" not in cmd                    # any login shell, tcsh too
    rows = T.parse("peterslabworkstation", _read(peters), "2026-11-03T10:30:00")
    got = {(r["chip"], r["label"] or r["sensor"]): r["temp_c"] for r in rows}
    assert got == {("k10temp", "Tctl"): 72.6, ("k10temp", "Tccd1"): 66.0, ("k10temp", "Tccd2"): 45.2,
                   ("nvme@nvme0", "Composite"): 41.9, ("nvme@nvme1", "Composite"): 38.9,
                   ("amdgpu", "edge"): 44.0}                    # two drives told apart; acpitz's 0 left out
    assert [(r["chip"], r["label"]) for r in T.cpu_sensor(rows)] == [("k10temp", "Tctl")]


def test_the_cpu_package_on_intel_and_older_amd():
    rows = [{"chip": "coretemp", "sensor": f"temp{i}", "label": lab, "temp_c": 50.0}
            for i, lab in enumerate(["Package id 0", "Core 0", "Core 1", "Package id 1"], start=1)]
    assert [r["label"] for r in T.cpu_sensor(rows)] == ["Package id 0", "Package id 1"]
    old = [{"chip": "k10temp", "sensor": "temp1", "label": None, "temp_c": 50.0}]
    assert T.cpu_sensor(old) == old
    assert T.cpu_sensor([{"chip": "nvme", "sensor": "temp1", "label": "Composite", "temp_c": 40.0}]) == []


def test_rules_from_alerts_temps():
    assert T.rules({}, "x") == T.DEFAULTS
    cfg = {"warning": 88, "sustain_minutes": 2, "critical": "hot",
           "peterslabworkstation": {"warning": 85, "critical": 89}}
    r = T.rules(cfg, "peterslabworkstation.richmond.edu")           # its short name's table
    assert (r["warning"], r["critical"], r["sustain_minutes"]) == (85.0, 89.0, 2.0)
    assert T.rules(cfg, "other")["warning"] == 88.0 and T.rules(cfg, "other")["critical"] == 95.0
    assert T.rules({"enabled": False}, "x")["enabled"] is False
    assert T.rules({"enabled": False, "x": {"enabled": True}}, "x")["enabled"] is True


def _pts(now, temps, every=1):
    return [(now - timedelta(minutes=i * every), t) for i, t in enumerate(temps)]


def test_sustained_means_every_reading_for_that_long():
    now = datetime(2026, 11, 3, 10, 30)
    assert T.sustained(_pts(now, [86, 86, 87, 85.5]), 85, 3, now)[0]          # 3 minutes at or above
    assert not T.sustained(_pts(now, [86, 86, 84]), 85, 3, now)[0]            # 1 minute only
    assert not T.sustained(_pts(now, [95]), 85, 3, now)[0]                    # a spike
    yes, since, lo, hi = T.sustained(_pts(now, [86, 88, 87, 80]), 85, 2, now)
    assert yes and since == now - timedelta(minutes=2) and (lo, hi) == (86, 88)
    # every 5 minutes (the main timer): two readings in a row are 5 minutes
    assert T.sustained(_pts(now, [86, 86], every=5), 85, 3, now)[0]
    # an old reading is not now; a gap ends the stretch
    assert not T.sustained(_pts(now - timedelta(minutes=30), [90, 90, 90, 90]), 85, 3, now)[0]
    gap = [(now, 90), (now - timedelta(minutes=20), 90), (now - timedelta(minutes=21), 90)]
    assert not T.sustained(gap, 85, 3, now)[0]
    assert T.sustained([(now, 90)], 85, 0, now)[0]                            # sustain 0: at once


def test_collected_kept_hour_by_hour_and_alerted(peters, tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    sent = []
    import nomad.alerts as A
    monkeypatch.setattr(A, "send_alert", lambda **k: sent.append(k) or {"email": True})
    c = T.TempsCollector({"hosts": ["localhost"], "hwmon_root": str(peters), "min_seconds": 0,
                          "alert_rules": {H: {"warning": 70, "critical": 89}}}, db)
    data = c.collect()
    assert {(r["chip"], r["label"] or r["sensor"]) for r in data} >= {("k10temp", "Tctl")}
    c.store(data)
    assert sent == []                                       # one reading: not sustained yet
    conn = sqlite3.connect(db)
    # three minutes of readings before it, and one too old to keep
    now = datetime.now()
    for m in (1, 2, 3):
        conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES "
                     "(?, ?, 'k10temp', 'temp1', 'Tctl', 73.0)",
                     ((now - timedelta(minutes=m)).isoformat(timespec="seconds"), H))
    conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES "
                 "(?, ?, 'k10temp', 'temp1', 'Tctl', 50.0)",
                 ((now - timedelta(days=40)).isoformat(timespec="seconds"), H))
    conn.commit()
    c.store(c.collect())
    assert len(sent) == 1 and sent[0]["severity"] == "warning" and sent[0]["source"] == "temps"
    assert sent[0]["host"] == H and "k10temp Tctl" in sent[0]["subject"]
    msg = sent[0]["message"]
    assert "at 72.6-73 °C for 3 minutes" in msg and "warning at 70 °C, critical at 89 °C" in msg
    assert "Tccd1 66 °C" in msg
    assert conn.execute("SELECT COUNT(*) FROM hw_temps WHERE temp_c = 50.0").fetchone()[0] == 0   # past keep_days
    hour = conn.execute("SELECT min_c, max_c, readings FROM hw_temps_hourly WHERE chip = 'k10temp' AND "
                        "sensor = 'temp1' ORDER BY hour DESC").fetchone()
    assert hour[0] <= 72.6 <= hour[1] and hour[2] >= 2
    conn.close()


def test_a_run_right_after_another_reads_nothing(peters, tmp_path):
    db = tmp_path / "site.db"
    c = T.TempsCollector({"hosts": ["localhost"], "hwmon_root": str(peters), "alert_rules": {}}, db)
    c.store(c.collect())
    assert c.collect() == [] and "less than 45 s ago" in c.note
    assert T.TempsCollector({"alert_rules": {}}, db).collect() == []          # no hosts: nothing, said


def test_over_ssh_the_login_s_own_output_is_skipped(monkeypatch, tmp_path):
    import subprocess
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "Welcome!\n" + T.MARK + "\nT|k10temp|x|temp1|Tctl|72625|\n", "")
    monkeypatch.setattr(T.subprocess, "run", run)
    c = T.TempsCollector({"hosts": ["peterslabworkstation"], "ssh_user": "root", "alert_rules": {}},
                         tmp_path / "db")
    rows = c.collect()
    assert [(r["node"], r["temp_c"]) for r in rows] == [("peterslabworkstation", 72.6)]
    argv = seen[0]
    assert argv[0] == "ssh" and "BatchMode=yes" in argv and argv[argv.index("-l") + 1] == "root"
    assert argv[-2:][0] == "peterslabworkstation" and argv[-3] == "--"
    assert argv[-1].startswith(f"echo {T.MARK}; exec sh -c ")
    # a host that doesn't answer: said, not a crash
    monkeypatch.setattr(T.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 255, "", "ssh: connect to host peterslabworkstation port 22: No route to host"))
    with pytest.raises(T.CollectionError, match="No route to host"):
        T.TempsCollector({"hosts": ["peterslabworkstation"], "alert_rules": {}}, tmp_path / "db").collect()
    assert T.TempsCollector({"hosts": ["-oProxyCommand=x", "ok-host"], "alert_rules": {}},
                            tmp_path / "db").hosts == ["ok-host"]


def test_the_plan_gives_it_alerts_temps_and_node_state_its_alerts():
    from nomad.collectors.plan import plan
    cfg = {"collectors": {"temps": {"enabled": True, "hosts": ["peterslabworkstation"]}},
           "alerts": {"temps": {"peterslabworkstation": {"warning": 85}},
                      "node_jobs": {"enabled": False}, "node_state": {"enabled": False}}}
    p = {x.name: x for x in plan(cfg)}
    assert p["temps"].enabled and p["temps"].config["alert_rules"] == {"peterslabworkstation": {"warning": 85}}
    assert not {x.name: x for x in plan({})}["temps"].enabled                 # off unless enabled
    # [alerts.node_state] and [alerts.node_jobs] never reached the node_state collector before 1.25.6
    ns = p["node_state"].config["alerts"]
    assert ns == {"node_state": {"enabled": False}, "node_jobs": {"enabled": False}}
    from nomad.alerts import node_jobs
    assert node_jobs.settings(p["node_state"].config)["enabled"] is False


def test_nomad_temps_shows_now_the_day_and_tests_the_alert(peters, tmp_path, monkeypatch):
    from nomad.cli import cli
    db = tmp_path / "site.db"
    c = T.TempsCollector({"hosts": ["localhost"], "hwmon_root": str(peters), "min_seconds": 0,
                          "alert_rules": {}}, db)
    conn = sqlite3.connect(db)
    conn.executescript(T.TempsCollector.SCHEMA)
    now = datetime.now()
    for m in range(1, 200, 10):
        conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES "
                     "(?, ?, 'k10temp', 'temp1', 'Tctl', ?)",
                     ((now - timedelta(minutes=m)).isoformat(timespec="seconds"), H, 70 + m % 3))
    conn.commit()
    conn.close()
    c.store(c.collect())
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[alerts.temps."{H}"]\nwarning = 85\ncritical = 89\n')
    r = CliRunner().invoke(cli, ["-c", str(toml), "temps", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "CPU  k10temp Tctl" in r.output and "72.6 °C" in r.output and "warning 85, critical 89" in r.output
    assert "each hour: lowest, mean, highest" in r.output and ("day (08-20)" in r.output or "night (20-08)" in r.output)
    sent = []
    import nomad.alerts as A
    monkeypatch.setattr(A, "send_alert", lambda **k: sent.append(k) or {"EmailBackend": True})
    monkeypatch.setattr(A, "init_dispatcher", lambda config: type("D", (), {})())
    r = CliRunner().invoke(cli, ["-c", str(toml), "temps", "--db", str(db), "--check", "--warning", "60",
                                 "--sustain", "0"])
    assert "check: warning:" in r.output and sent == []                       # said, not sent
    r = CliRunner().invoke(cli, ["-c", str(toml), "temps", "--db", str(db), "--check", "--warning", "60",
                                 "--sustain", "0", "--send"])
    assert r.exit_code == 0, r.output
    assert len(sent) == 1 and "(A test" in sent[0]["message"] and "sent: EmailBackend sent" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "temps", "--db", str(db), "--check"])
    assert "check: nothing to raise (warning 85, critical 89 °C, for 3 min)" in r.output


def test_how_long_is_said_as_it_is(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.executescript(T.TempsCollector.SCHEMA)
    now = datetime(2026, 11, 3, 10, 30)
    cpu = {"node": "w", "chip": "k10temp", "sensor": "temp1", "label": "Tctl", "temp_c": 90.0}
    rule = {**T.DEFAULTS, "warning": 85.0, "critical": 89.0, "sustain_minutes": 0.0}
    conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES (?, 'w', 'k10temp', "
                 "'temp1', 'Tctl', 90.0)", (now.isoformat(timespec="seconds"),))
    conn.commit()
    f = T.check_cpu(db, "w", cpu, rule, [cpu], now)
    assert f["severity"] == "critical" and f["message"].startswith("w's CPU is at 90 °C (k10temp Tctl;")
    assert "slows itself down" in f["message"]
    for m in range(1, 400, 2):                                 # hot for more than the 6 hours looked at
        conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES (?, 'w', "
                     "'k10temp', 'temp1', 'Tctl', 86.0)", ((now - timedelta(minutes=m)).isoformat(timespec="seconds"),))
    conn.commit()
    f = T.check_cpu(db, "w", cpu, {**rule, "sustain_minutes": 3.0}, [cpu], now)
    assert f["severity"] == "warning" and "for more than 6 hours" in f["message"] and "86-90 °C" in f["message"]


def test_workstations_and_slurm_s_nodes_too(tmp_path, monkeypatch):
    import subprocess
    from nomad.collectors.plan import plan
    cfg = {"collectors": {"temps": {"enabled": True, "workstations": True, "hosts": ["peterslabworkstation"]},
                          "workstation": {"workstations": [{"hostname": "peterslabworkstation"},
                                                           {"hostname": "ws-two"}]}}}
    c = T.TempsCollector({**{x.name: x for x in plan(cfg)}["temps"].config, "min_seconds": 0}, tmp_path / "db")
    assert c.hosts == ["peterslabworkstation", "ws-two"]                     # once each
    # the top level's [[workstations]] too, as older configs put them
    top = {"collectors": {"temps": {"enabled": True, "workstations": True}}, "workstations": [{"hostname": "w3"}]}
    assert {x.name: x for x in plan(top)}["temps"].config["workstation_hosts"] == ["w3"]
    # a head node: Slurm's nodes that answer, by NodeHostname, kept by NodeName; this host by its own name
    asked = []

    def run(argv, **kw):
        if argv[0].endswith("sinfo"):
            return subprocess.CompletedProcess(argv, 0, "spdr01 spdr01 idle\nspdr01 spdr01 idle\n"
                                                      "spdr02 spdr02-ib mix\nspdr03 spdr03 down*\n"
                                                      "spdr04 spdr04 idle~\n", "")
        asked.append(argv[argv.index("--") + 1] if "--" in argv else "here")
        return subprocess.CompletedProcess(argv, 0, f"{T.MARK}\nT|coretemp|x|temp1|Package id 0|61000|100000\n", "")
    monkeypatch.setattr(T.subprocess, "run", run)
    import nomad.collectors.base as B
    monkeypatch.setattr(B, "find_tool", lambda name: f"/usr/bin/{name}")
    c = T.TempsCollector({"hosts": ["localhost"], "slurm_nodes": True, "min_seconds": 0, "alert_rules": {}},
                         tmp_path / "db")
    rows = c.collect()
    assert sorted(asked) == ["here", "spdr01", "spdr02-ib"]                   # not down, not powered off
    assert sorted(r["node"] for r in rows) == sorted([H, "spdr01", "spdr02"])
    assert {r["crit_c"] for r in rows} == {100.0}


def test_a_host_with_no_sensors_is_said_not_failed(tmp_path):
    empty = tmp_path / "hwmon"
    empty.mkdir()
    c = T.TempsCollector({"hosts": ["localhost"], "hwmon_root": str(empty), "min_seconds": 0, "alert_rules": {}},
                         tmp_path / "db")
    assert c.collect() == [] and c.note == f"no temperature sensors on {H}"      # a VM: nothing, not an error


def test_spiderweb_two_sockets_and_an_older_driver(tmp_path):
    # spiderweb (3 Nov): hwmon0 and hwmon1 coretemp (a socket each), hwmon2 with no name
    root = tmp_path / "hwmon"
    for dev in ("coretemp.0", "coretemp.1", "acpi-power", "it87.656"):
        (root / "devices" / dev).mkdir(parents=True)
    _chip(root, 0, "coretemp", "coretemp.0", {"Package id 0": 48000, "Core 0": 45000, "Core 1": 47000})
    _chip(root, 1, "coretemp", "coretemp.1", {"Package id 1": 51000, "Core 0": 49000})
    (root / "hwmon2").mkdir()
    (root / "hwmon2" / "device").symlink_to(root / "devices" / "acpi-power")
    (root / "hwmon2" / "power1_input").write_text("120000000\n")       # no name, no temperature
    old = root / "hwmon3"                                              # an older driver: all of it in device/
    old.mkdir()
    (old / "device").symlink_to(root / "devices" / "it87.656")
    (root / "devices" / "it87.656" / "name").write_text("it87\n")
    (root / "devices" / "it87.656" / "temp1_input").write_text("38000\n")
    rows = T.parse("spiderweb", _read(root), "2026-11-03T10:30:00")
    got = sorted((r["chip"], r["label"] or r["sensor"], r["temp_c"]) for r in rows)
    assert got == [("coretemp@coretemp.0", "Core 0", 45.0), ("coretemp@coretemp.0", "Core 1", 47.0),
                   ("coretemp@coretemp.0", "Package id 0", 48.0), ("coretemp@coretemp.1", "Core 0", 49.0),
                   ("coretemp@coretemp.1", "Package id 1", 51.0), ("it87", "temp1", 38.0)]
    assert [(r["chip"], r["label"]) for r in T.cpu_sensor(rows)] == [("coretemp@coretemp.0", "Package id 0"),
                                                                    ("coretemp@coretemp.1", "Package id 1")]
    dup = "T|k10temp|x|temp1|Tctl|72000|\nT|k10temp|x|temp1|Tctl|72000|\n"
    assert len(T.parse("w", dup, "t")) == 1
