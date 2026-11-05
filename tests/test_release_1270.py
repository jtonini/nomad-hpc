"""1.27.0: how fast each CPU really runs when busy. arachne's node01-03 lost a
supply's input on 4 Oct 2026 and their boards braked the CPUs to ~400 MHz for
a month; the readings are João's from 5 Nov (node02 braked, node51 not)."""
import ast
import sqlite3
import subprocess
from datetime import datetime, timedelta

from click.testing import CliRunner

from nomad.collectors import clock as C

NODE02 = ("CLOCK|base_khz=None|cpu_s=0.506|cpus=64|driver=acpi-cpufreq|governor=performance|load1=0.29|"
          "max_khz=3764202|mhz=396|model=AMD EPYC 9554 64-Core Processor|os_khz=399418|sha_mbps=115.1|v=1")
NODE51 = ("CLOCK|base_khz=None|cpu_s=0.5|cpus=128|driver=acpi-cpufreq|governor=performance|load1=0.0|"
          "max_khz=3764202|mhz=3730|model=AMD EPYC 9554 64-Core Processor|os_khz=3100000|sha_mbps=1825.8|v=1")
RULE = C.rules({}, "x")


def test_the_probe_is_python_3_6_and_runs_here():
    ast.parse(C.PROBE, feature_version=(3, 6))
    r = subprocess.run(["sh", "-c", C.remote_command(0.2)], capture_output=True, text=True, timeout=60)
    got = C.parse("here", r.stdout, "2026-11-05T09:00:00")
    # a cycle counter where there is one; the sha256 rate always
    assert got["sha_mbps"] and got["sha_mbps"] > 0
    assert got["mhz"] or got["perf"]


def test_node02_is_braked_node51_is_not():
    n02 = C.parse("node02", NODE02, "2026-11-05T08:56:00")
    n51 = C.parse("node51", NODE51, "2026-11-05T08:56:00")
    assert (n02["mhz"], n02["max_mhz"], n02["os_mhz"], n02["model"]) == (396, 3764, 399, "AMD EPYC 9554 64-Core Processor")
    j = C.judge(n02, [], [n51], RULE)
    assert j["low"] and j["reference"] == 3764 and j["ratio"] < 0.11
    assert j["problem"] == ("its CPU runs at 396 MHz busy, 11% of 3764 MHz (its CPU's top clock): "
                            "jobs there run about 10x slower")
    assert not C.judge(n51, [], [n02], RULE)["low"]
    # with its own history only (no cpufreq maximum): its usual clock
    alone = dict(n02, max_mhz=None)
    j = C.judge(alone, [{"mhz": 3700}] * 10, [], RULE)
    assert j["low"] and j["basis"] == "its usual clock"
    # all its usual readings braked too (since the day nomad started): hosts with its CPU say
    j = C.judge(alone, [{"mhz": 396}] * 10, [dict(n51, node="node52"), dict(n51, node="node53"), n02], RULE)
    assert j["low"] and j["basis"] == "3 hosts with its CPU" and j["reference"] == 3730


def test_what_the_os_says_and_a_powersave_governor_are_said():
    n = C.parse("n", NODE02.replace("os_khz=399418", "os_khz=3100000"), "t")
    assert "(the OS says 3100 MHz: the clock it asks for, not the one the CPU gets)" in C.judge(n, [], [], RULE)["problem"]
    n = C.parse("n", NODE02.replace("governor=performance", "governor=powersave"), "t")
    assert "governor is powersave" in C.judge(n, [], [], RULE)["problem"]
    # an Intel model's rated clock counts as what it should give
    n = C.parse("n", "CLOCK|cpus=40|max_khz=None|mhz=800|model=Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz|"
                     "os_khz=None|sha_mbps=200|v=1", "t")
    j = C.judge(n, [], [], RULE)
    assert j["low"] and j["reference"] == 2500 and j["basis"] == "its CPU's rated clock"


def test_no_cycle_counter_the_sha256_rate_against_its_own_and_its_peers():
    vm = C.parse("vm1", "CLOCK|cpus=2|mhz=None|model=Common KVM processor|perf=[Errno 2] No such file|"
                        "sha_mbps=120|v=1", "t")
    assert vm["mhz"] is None and vm["perf"]
    j = C.judge(vm, [], [], RULE)
    assert not j["low"] and j["reference"] is None                 # nothing to compare it with: said, not judged
    j = C.judge(vm, [{"sha_mbps": 1800}] * 6, [], RULE)
    assert j["low"] and j["measure"] == "sha_mbps" and "its cycle counter can't be read here" in j["problem"]
    j = C.judge(vm, [], [{"sha_mbps": 1700}, {"sha_mbps": 1800}], RULE)
    assert j["low"] and j["basis"] == "2 hosts with its CPU"
    # the sha path's own, lower fraction: a busy hyperthread sibling halves a rate, not a brake
    assert not C.judge(dict(vm, sha_mbps=700), [{"sha_mbps": 1800}] * 6, [], RULE)["low"]


def test_rules():
    raw = {"min_fraction": 0.3, "sustain": 3, "ignore": ["gpu0*"], "node07": {"min_fraction": 0.8, "enabled": False}}
    assert C.rules(raw, "node02") == {"enabled": True, "min_fraction": 0.3, "sha_min_fraction": 0.25, "sustain": 3}
    assert C.rules(raw, "gpu09")["enabled"] is False
    assert C.rules(raw, "node07.cluster") == {"enabled": False, "min_fraction": 0.8, "sha_min_fraction": 0.25,
                                             "sustain": 3}
    assert C.rules({"min_fraction": 2, "sustain": "x"}, "n") == C.DEFAULTS


def _collector(tmp_path, monkeypatch, answers, sent, rules=None):
    def run(argv, **kw):
        if argv[0].endswith("sinfo"):
            return subprocess.CompletedProcess(argv, 0, "".join(f"{n} {n} drain\n" for n in answers), "")
        host = argv[argv.index("--") + 1]
        return subprocess.CompletedProcess(argv, 0, f"{C.MARK}\n{answers[host]}\n", "")
    monkeypatch.setattr(C.subprocess, "run", run)
    import nomad.collectors.base as B
    monkeypatch.setattr(B, "find_tool", lambda name: f"/usr/bin/{name}")
    import nomad.alerts as A
    monkeypatch.setattr(A, "send_alert", lambda **k: sent.append(k) or {"email": True})
    return C.ClockCollector({"slurm_nodes": True, "min_seconds": 0, "alert_rules": rules or {}}, tmp_path / "site.db")


def test_throttled_after_two_low_readings_critical_with_a_lost_supply(tmp_path, monkeypatch):
    sent = []
    answers = {"node01": NODE02, "node02": NODE02, "node51": NODE51}
    c = _collector(tmp_path, monkeypatch, answers, sent)
    # node01's board can't be read in-band; node02's says its PSU1 has no input (the power collector)
    db = sqlite3.connect(tmp_path / "site.db")
    db.executescript("CREATE TABLE power_hosts (node TEXT PRIMARY KEY, read_at TEXT, error TEXT);"
                     "CREATE TABLE power_sensors (node TEXT, sensor TEXT, subject TEXT, severity TEXT, problem TEXT);")
    db.execute("INSERT INTO power_hosts VALUES ('node01', 't', 'Could not open device at /dev/ipmi0 or /dev/ipmi/0')")
    db.execute("INSERT INTO power_hosts VALUES ('node02', 't', NULL)")
    db.execute("INSERT INTO power_sensors VALUES ('node02', 'PSU1 AC Lost', 'power supply', 'warning', "
               "'PSU1 AC Lost says \"Power Supply AC lost\"')")
    db.commit()
    c.store(c.collect())
    assert sent == []                                               # one low reading: not yet
    hosts = {h["node"]: h for h in C.hosts_now(db)}
    assert hosts["node02"]["low_since"] and not hosts["node02"]["throttled"] and not hosts["node51"]["low_since"]
    first = hosts["node02"]["low_since"]
    db.execute("UPDATE cpu_clock SET timestamp = ?", ((datetime.now() - timedelta(minutes=15)).isoformat(timespec="seconds"),))
    db.commit()
    c.store(c.collect())
    by = {s["host"]: s for s in sent}
    assert sorted(by) == ["node01", "node02"]
    assert by["node02"]["severity"] == "critical" and by["node02"]["source"] == "clock"
    assert by["node02"]["subject"] == "cpu clock"
    m = by["node02"]["message"]
    assert m.startswith("node02: its CPU runs at 396 MHz busy, 11% of 3764 MHz") and "Its power: PSU1 AC Lost" in m
    assert "scontrol update nodename=node02 state=drain" in m and "ipmitool mc reset cold" in m
    assert by["node01"]["severity"] == "warning"
    assert "its board can't be read in-band (no /dev/ipmi0); check them out of band" in by["node01"]["message"]
    hosts = {h["node"]: h for h in C.hosts_now(db)}
    assert hosts["node02"]["throttled"] == 1 and hosts["node02"]["low_since"] == first      # since the first
    assert set(C.throttled_nodes(db)) == {"node01", "node02"}
    # back to its clock: no longer low, nothing sent
    answers["node02"] = NODE51
    sent.clear()
    c.store(c.collect())
    hosts = {h["node"]: h for h in C.hosts_now(db)}
    assert not hosts["node02"]["throttled"] and not hosts["node02"]["low_since"]
    assert [s["host"] for s in sent] == ["node01"]


def test_ignored_hosts_are_kept_but_not_alerted(tmp_path, monkeypatch):
    sent = []
    c = _collector(tmp_path, monkeypatch, {"node02": NODE02, "node51": NODE51}, sent,
                   rules={"ignore": ["node02"], "sustain": 1})
    c.store(c.collect())
    assert sent == [] and C.hosts_now(sqlite3.connect(tmp_path / "site.db"))[0]["throttled"] == 1


def test_where_a_job_starts_leaves_out_braked_nodes():
    from nomad.submit import where as W
    cfg = W.Settings()
    cands = [{"partition": "cpunodes", "role": "small", "allowed": True, "why": "", "nodes": 3},
             {"partition": "node02only", "role": "small", "allowed": True, "why": "", "nodes": 1},
             {"partition": "fast", "role": "small", "allowed": True, "why": "", "nodes": 2}]
    braked = {"node02": {"node": "node02", "mhz": 396, "ratio": 0.105}}
    slow = {"cpunodes": [("node02", braked["node02"])], "node02only": [("node02", braked["node02"])], "fast": []}
    starts = {"starts": [{"partition": p, "soon": True} for p in ("cpunodes", "node02only", "fast")]}
    got = W.rank(cands, starts, cfg, slow=slow)
    rows = {r["partition"]: r for r in got["rows"]}
    assert not rows["node02only"]["allowed"]
    assert rows["node02only"]["why"] == ("its node runs far below its clock, throttled (node02 at 396 MHz): "
                                         "a job there runs about 10x slower")
    assert rows["cpunodes"]["allowed"] and rows["cpunodes"]["why"].startswith("1 of its 3 nodes runs throttled")
    assert got["recommended"] == "fast" and got["fastest"] == "fast"            # not the first in order
    # nothing braked: as before
    assert W.rank(cands, starts, cfg)["recommended"] == "cpunodes"


def test_the_hub_alerts_for_a_site_that_leaves_them_to_it(tmp_path, monkeypatch):
    from nomad.alerts import hub_clock
    hub = sqlite3.connect(tmp_path / "combined.db")
    hub.executescript(C.ClockCollector.SCHEMA.replace("node TEXT PRIMARY KEY", "node TEXT")
                      .replace("every_minutes REAL);", "every_minutes REAL, source_site TEXT);"))
    now = datetime.now().isoformat(timespec="seconds")
    # node06: throttled, but the site's [alerts.clock] leaves it out (no severity stored)
    for site, node, throttled, sev in (("vadur", "node04", 1, "warning"), ("vadur", "node05", 0, None),
                                       ("vadur", "node06", 1, None), ("spydur", "spdr01", 1, "warning")):
        hub.execute("INSERT INTO cpu_clock_hosts (node, read_at, mhz, reference, basis, measure, ratio, low_since, "
                    "throttled, severity, problem, source_site) VALUES (?, ?, 400, 3000, 'its usual clock', 'mhz', "
                    "0.13, ?, ?, ?, 'its CPU runs at 400 MHz busy, 13% of 3000 MHz (its usual clock)', ?)",
                    (node, now, now if throttled else None, throttled, sev, site))
    hub.commit()
    sent = []
    monkeypatch.setattr("nomad.alerts.dispatcher.send_alert",
                        lambda severity, source, message, **k: sent.append((severity, source, message, k)) or {"x": 1})
    cfg = {"hub": {"sites": [{"name": "vadur", "alerts": "hub"}, {"name": "spydur"}]}}
    hub_clock.hub_clock_alerts(tmp_path / "combined.db", tmp_path, config=cfg)
    assert [(s[1], s[3]["subject"]) for s in sent] == [("clock", "vadur/node04 cpu clock")]
    assert sent[0][2].startswith("node04 at vadur: its CPU runs at 400 MHz") and sent[0][2].endswith(
        "Sent by the hub for vadur.")
    hub_clock.hub_clock_alerts(tmp_path / "combined.db", tmp_path, config=cfg)
    assert len(sent) == 1


def test_nomad_clock_says_it(tmp_path, monkeypatch):
    from nomad.cli import cli
    sent = []
    c = _collector(tmp_path, monkeypatch, {"node02": NODE02, "node51": NODE51}, sent, rules={"sustain": 1})
    c.store(c.collect())
    toml = tmp_path / "nomad.toml"
    toml.write_text("[alerts.clock]\nsustain = 1\n")
    r = CliRunner().invoke(cli, ["-c", str(toml), "clock", "--db", str(tmp_path / "site.db"), "--check",
                                 "--history", "3"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "node02" in out and "THROTTLED" in out and "396 MHz busy, 11% of 3764 (its CPU's top clock)" in out
    assert "node51" in out and "3730 MHz busy, 99% of 3764" in out
    assert "AMD EPYC 9554 64-Core Processor, governor performance" in out
    assert "check: warning: cpu clock: node02: its CPU runs at 396 MHz" in out
    r = CliRunner().invoke(cli, ["-c", str(toml), "clock", "--db", str(tmp_path / "site.db"), "--host", "node51"])
    assert "node02" not in r.output and "ok" in r.output


# ── review of 1.27.0 ───────────────────────────────────────────────────────

def test_the_probe_is_one_line_for_any_login_shell():
    import shlex
    import shutil
    import pytest as _pytest
    remote = f"echo {C.MARK}; exec sh -c {shlex.quote(C.remote_command(0.1))}"
    assert "\n" not in remote and "!" not in remote
    shells = [x for x in ("tcsh", "csh", "bash", "sh") if shutil.which(x)]
    if not shells:
        _pytest.skip("no shell to try")
    for shell in shells:
        r = subprocess.run([shell, "-c", remote], capture_output=True, text=True, timeout=60)
        assert r.stdout.splitlines()[0] == C.MARK and C.parse("h", r.stdout, "t")["sha_mbps"], (shell, r.stderr)


def test_the_counter_is_read_with_its_time_running():
    # no hardware counter here: the same code with the software CPU clock (nanoseconds) gives "1000 MHz"
    sw = C.PROBE.replace('struct.pack("=IIQQQQQIIQQQQIiQIHH", 0, 112, 0,', 'struct.pack("=IIQQQQQIIQQQQIiQIHH", 1, 112, 0,')
    assert sw != C.PROBE
    r = subprocess.run(["python3", "-I", "-c", sw, "0.2"], capture_output=True, text=True, timeout=60)
    got = C.parse("h", r.stdout, "t")
    if got["mhz"] is None:
        import pytest as _pytest
        _pytest.skip(f"no perf events at all here: {got['perf']}")
    assert 900 <= got["mhz"] <= 1100 and got["mhz_pkgs"].endswith(str(got["mhz"])) and got["pkgs"] >= 1


def test_a_low_count_with_a_usual_sha256_rate_is_not_a_brake():
    n51 = C.parse("node51", NODE51, "t")
    # a count another tool disturbed: the clock reads low, the sha256 rate is as usual
    disturbed = dict(n51, mhz=900)
    assert not C.judge(disturbed, [{"mhz": 3700, "sha_mbps": 1800}] * 6, [], RULE)["low"]
    # a real brake lowers both
    braked = dict(n51, mhz=400, sha_mbps=110)
    assert C.judge(braked, [{"mhz": 3700, "sha_mbps": 1800}] * 6, [], RULE)["low"]


def test_hosts_are_compared_only_with_hosts_alike():
    real = {"model": "AMD EPYC 9554 64-Core Processor", "vm": 0, "sha_ni": 1}
    assert C.alike(real, dict(real))
    assert not C.alike(real, dict(real, vm=1)) and not C.alike(dict(real, vm=1), dict(real, vm=1))
    assert not C.alike(real, dict(real, sha_ni=0)) and not C.alike(real, dict(real, model="AMD EPYC 7742"))
    assert C.alike(real, {"model": real["model"]})                      # a v1 reading: flags unknown


def test_a_supply_carrying_the_node_alone_makes_it_critical(tmp_path):
    import json
    db = sqlite3.connect(tmp_path / "db")
    db.executescript("CREATE TABLE power_hosts (node TEXT, read_at TEXT, error TEXT, host_problems TEXT);"
                     "CREATE TABLE power_sensors (node TEXT, sensor TEXT, subject TEXT, severity TEXT, problem TEXT);")
    db.execute("INSERT INTO power_hosts VALUES ('n1', 't', NULL, ?)", (json.dumps(
        [{"subject": "power supply", "severity": "warning", "problem": "PSU2 gives 0 W while PSU1 gives 200 W"}]),))
    db.execute("INSERT INTO power_hosts VALUES ('n2', 't', 'ipmitool sdr type Fan: no answer in 30 s', NULL)")
    assert C.power_of(db, "n1") == (["PSU2 gives 0 W while PSU1 gives 200 W"], None)
    assert C.power_of(db, "n2") == ([], "its board wasn't read (ipmitool sdr type Fan: no answer in 30 s)")
    it = C.alert_item("n1", {"problem": "x", "low_since": "t"}, ["PSU2 gives 0 W"], None, every_minutes=60)
    assert it["severity"] == "critical" and it["details"]["episode_gap_minutes"] == 180


def test_one_slow_node_doesnt_send_the_job_to_a_guest_condo():
    from nomad.submit import where as W
    cfg = W.Settings()
    now = datetime(2026, 11, 5, 10, 0)
    cands = [{"partition": "cpunodes", "role": "small", "allowed": True, "why": "", "nodes": 50},
             {"partition": "parishcondo", "role": "guest", "allowed": True, "why": "", "nodes": 4}]
    starts = {"now": now.isoformat(), "starts": [{"partition": "cpunodes", "soon": True},
                                                 {"partition": "parishcondo",
                                                  "start": (now + timedelta(days=4)).isoformat()}]}
    got = W.rank(cands, starts, cfg, slow={"cpunodes": [("node02", {"mhz": 396, "ratio": 0.105})]})
    assert got["recommended"] == "cpunodes"                         # it starts now: one slow node of 50 is said
    assert "1 of its 50 nodes runs throttled" in got["rows"][0]["why"]


def test_a_drained_braked_node_slows_nothing(tmp_path, monkeypatch):
    from nomad.submit import where as W
    db = sqlite3.connect(tmp_path / "db")
    db.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT)")
    db.execute("INSERT INTO node_state VALUES ('2026-11-05T09:00:00', 'node02', 'drained')")
    db.execute("INSERT INTO node_state VALUES ('2026-11-05T09:00:00', 'node04', 'mixed')")
    assert W._out_of_service(db, None, "node02") and not W._out_of_service(db, None, "node04")
    assert not W._out_of_service(db, None, "nowhere")



# ── second review of 1.27.0 ────────────────────────────────────────────────

def test_a_month_braked_is_not_its_usual_clock(tmp_path, monkeypatch):
    sent = []
    braked = NODE02.replace("max_khz=3764202", "max_khz=None")            # no cpufreq maximum to go by
    c = _collector(tmp_path, monkeypatch, {"node02": braked}, sent, rules={"sustain": 1})
    c.store([])                                                            # the tables
    db = sqlite3.connect(tmp_path / "site.db")
    old = datetime.now() - timedelta(days=20)
    for i in range(5):                                                     # before: 3,700
        db.execute("INSERT INTO cpu_clock (timestamp, node, model, mhz, sha_mbps, low) VALUES (?, 'node02', "
                   "'AMD EPYC 9554 64-Core Processor', 3700, 1800, 0)", ((old + timedelta(hours=i)).isoformat(),))
    for i in range(60):                                                    # then braked for weeks
        db.execute("INSERT INTO cpu_clock (timestamp, node, model, mhz, sha_mbps, low) VALUES (?, 'node02', "
                   "'AMD EPYC 9554 64-Core Processor', 396, 115, 1)", ((old + timedelta(days=1, hours=i)).isoformat(),))
    db.commit()
    c.store(c.collect())
    h = C.hosts_now(db)[0]
    assert h["throttled"] == 1 and h["basis"] == "its usual clock" and h["reference"] == 3700
    assert [s["host"] for s in sent] == ["node02"]


def test_the_pick_stays_with_the_kind_the_site_puts_first():
    from nomad.submit import where as W
    cfg = W.Settings(reserve_nodes=0)
    cands = [{"partition": "cpunodes", "role": "small", "allowed": True, "why": "", "nodes": 50},
             {"partition": "parishcondo", "role": "guest", "allowed": True, "why": "", "nodes": 4},
             {"partition": "bigmem", "role": "overflow", "allowed": True, "why": "", "nodes": 5}]
    starts = {"starts": [{"partition": p, "soon": True} for p in ("cpunodes", "parishcondo", "bigmem")],
              "nodes": {"bigmem": {"idle": 3}}}
    got = W.rank(cands, starts, cfg, slow={"cpunodes": [("node02", {"mhz": 396, "ratio": 0.1})]})
    assert got["recommended"] == "cpunodes" and got["fastest"] == "cpunodes"


def test_a_partitions_share_is_of_the_nodes_slurm_still_uses():
    from nomad.submit import where as W
    cands = [{"partition": "trio", "role": "small", "allowed": True, "why": "", "nodes": 3}]
    braked = {"mhz": 396, "ratio": 0.1}
    starts = {"starts": [{"partition": "trio", "soon": True}]}
    # node01 drained (as the alert advises): the two left are both braked
    got = W.rank(cands, starts, W.Settings(), slow={"trio": [("node02", braked), ("node03", braked)]},
                 serving={"trio": 2})
    assert not got["rows"][0]["allowed"] and got["rows"][0]["why"].startswith("all 2 of its nodes run far below")


def test_slurm_states_are_read_as_slurm_writes_them(tmp_path):
    from nomad.submit import where as W
    db = sqlite3.connect(tmp_path / "db")
    db.execute("CREATE TABLE node_state (timestamp TEXT, node_name TEXT, state TEXT)")
    for node, state in (("a", "IDLE+CLOUD+POWERED_DOWN"), ("b", "MIXED+POWERING_UP"), ("c", "IDLE*"),
                        ("d", "IDLE+DRAIN"), ("e", "allocated")):
        db.execute("INSERT INTO node_state VALUES ('2026-11-05T09:00:00', ?, ?)", (node, state))
    assert [n for n in "abcde" if W._out_of_service(db, None, n)] == ["c", "d"]


def test_the_sha_rate_is_taken_where_the_clock_is_lowest():
    assert "pin(first[min(by)[1]] if by else cpu0)" in C.PROBE
    assert C.fresh({"read_at": (datetime.now() - timedelta(hours=5)).isoformat(), "every_minutes": 180})
    assert not C.fresh({"read_at": (datetime.now() - timedelta(hours=5)).isoformat(), "every_minutes": 15})
