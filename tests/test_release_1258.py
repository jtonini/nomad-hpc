"""1.25.8: the boards' power supplies, voltages and fans (IPMI), read by a
root helper and judged by the power collector; vadur's gpu09 (a supply with
no input power since February) and node03 (a supply that reports no power,
logged as 0 W under a 0 W threshold since 2017), as their boards said."""
import importlib.util
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.collectors import power as P

ROOT = Path(__file__).resolve().parent.parent
MARK = "__NOMAD_POWER__"


def _helper():
    spec = importlib.util.spec_from_file_location("power_root_probe", ROOT / "scripts" / "power_root_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _helper()

# What vadur's boards answered (3 Nov 2026), trimmed.
SDR = {
    "gpu09": {
        "Power Supply": """PSU1 AC Lost     | 94h | ok  |  0.0 | Power Supply AC lost
PSU1 PWR Detect  | 97h | ok  |  0.0 | Presence detected
PSU2 AC Lost     | 9Ch | ok  |  0.0 | Presence detected
PSU2 PWR Detect  | 9Fh | ok  |  0.0 | Presence detected
PMBPower         | E1h | lnr |  3.0 | 0 Watts
PMBPower2        | E2h | ok  |  3.0 | 200 Watts""",
        "Voltage": """+12V             | 38h | ok  |  0.0 | 12.10 Volts
+5V              | 37h | ok  |  0.0 | 5.02 Volts""",
        "Fan": """FAN1             | 41h | ok  | 29.1 | 6100 RPM
PSU1 FAN1        | 99h | lcr | 10.1 | 0 RPM
PSU1 FAN2        | 9Ah | lcr | 10.1 | 0 RPM
PSU2 FAN1        | A1h | ok  | 10.2 | 9800 RPM""",
    },
    "node03": {
        "Power Supply": "PMBPower         | E1h | ns  |  3.0 | No Reading",
        "Voltage": """+VCORE1          | 34h | ok  |  0.0 | 1.81 Volts
+12V             | 38h | ok  |  0.0 | 11.90 Volts
+5V              | 37h | ok  |  0.0 | 5.06 Volts
+5VSB            | 3Bh | ok  |  0.0 | 4.93 Volts
+3.3V            | 36h | ok  |  0.0 | 3.31 Volts
VBAT             | 3Ch | ok  |  0.0 | 3.22 Volts""",
        "Fan": "FAN1             | 41h | ok  | 29.1 | 5200 RPM",
    },
}
GET = {
    "PMBPower": (0.0, 0.0, 0.0),
    "PSU1 FAN1": (None, 460.0, 600.0),
    "PSU1 FAN2": (None, 460.0, 600.0),
}
SELINFO = """SEL Information
Version          : 1.5 (v1.5, v2 compliant)
Entries          : {n}
Percent Used     : 0%
Last Add Time    : {last}
Overflow         : false"""
SEL = {
    "gpu09": [
        "   a | 02/17/2026 | 04:58:31 | Power Supply PSU1 AC Lost | Power Supply AC lost | Asserted",
        "   b | 02/17/2026 | 04:58:32 | Fan PSU1 FAN1 | Lower Non-critical going low  | Asserted | Reading 0 < Threshold 460 RPM",
        "   f | 02/17/2026 | 04:58:36 | Power Supply PMBPower | Lower Non-critical going low  | Asserted | Reading 0 < Threshold 0 Watts",
    ],
    "node03": [
        "   1 | 06/11/2017 | 13:20:07 | Power Supply PMBPower | Lower Non-critical going low  | Asserted | Reading 0 < Threshold 0 Watts",
        "   2 | 06/11/2017 | 13:20:07 | Power Supply PMBPower | Lower Critical going low  | Asserted | Reading 0 < Threshold 0 Watts",
        "  20 |  Pre-Init  |0000018153| Power Supply PMBPower | Lower Critical going low  | Asserted | Reading 0 < Threshold 0 Watts",
    ],
}


class Boards:
    """Stands in for root's sh and ssh: each host's ipmitool, as the script asks."""

    def __init__(self):
        self.sel = {h: list(v) for h, v in SEL.items()}
        self.calls = []
        self.fail = set()        # (host, sdr type or "sel") whose ipmitool times out

    def __call__(self, host, script, timeout, local):
        self.calls.append((host, local, script))
        if host == "nobmc":
            return f"{MARK} sdr Power Supply\nCould not open device at /dev/ipmi0 or /dev/ipmi/0: No such file\n{MARK} end"
        if host == "gone":
            raise RuntimeError("ssh: connect to host gone port 22: No route to host")
        out = []
        rc = f"{MARK} rc 0"
        if "sdr type" in script:
            for t in H.TYPES:
                fails = (host, t) in self.fail
                out += [f"{MARK} sdr {t}", "" if fails else SDR.get(host, {}).get(t, ""),
                        f"{MARK} rc 124" if fails else rc]
            out += [f"{MARK} selinfo", SELINFO.format(n=len(self.sel.get(host, [])),
                                                       last=self.sel.get(host, ["x"])[-1][:30]), rc]
        if "sensor get" in script:
            out.append(f"{MARK} get")
            for name, (lnr, lcr, lnc) in GET.items():
                if f"'{name}'" in script or f" {name} " in script + " ":
                    out += ["Locating sensor record...", f"Sensor ID              : {name} (0x99)",
                            " Status                : Lower Critical",
                            f" Lower Non-Recoverable : {'na' if lnr is None else f'{lnr:.3f}'}",
                            f" Lower Critical        : {lcr:.3f}", f" Lower Non-Critical    : {lnc:.3f}",
                            " Upper Non-Critical    : 1504.000"]
            out.append(rc)
        if "sel elist" in script:
            if (host, "sel") in self.fail:
                out += [f"{MARK} sel", "Error: timeout", f"{MARK} rc 124"]
            else:
                out += [f"{MARK} sel"] + self.sel.get(host, []) + [rc]
        out.append(f"{MARK} end")
        return "\n".join(out)


@pytest.fixture
def boards(monkeypatch):
    b = Boards()
    monkeypatch.setattr(H, "run_on", b)
    monkeypatch.setattr(H.socket, "gethostname", lambda: "head.cluster")
    return b


def _run(tmp_path, *args):
    out = tmp_path / "power.json"
    assert H.main(["--out", str(out), *args]) == 0
    return json.loads(out.read_text()), out


def test_the_helper_reads_each_board_and_only_reads(tmp_path, boards):
    doc, out = _run(tmp_path, "--nodes", "gpu09,node03,nobmc,gone,bad;name", "--self")
    hosts = doc["hosts"]
    assert set(hosts) == {"head", "gpu09", "node03", "nobmc", "gone"}            # "bad;name" left out
    assert hosts["nobmc"]["error"].startswith("Could not open device")
    assert "No route to host" in hosts["gone"]["error"]
    g = {s["name"]: s for s in hosts["gpu09"]["sensors"]}
    assert g["PSU1 AC Lost"]["value"] == "Power Supply AC lost" and g["PSU1 AC Lost"]["type"] == "Power Supply"
    assert g["PMBPower"]["thresholds"] == {"lnr": 0.0, "lcr": 0.0, "lnc": 0.0, "unc": 1504.0}   # its status isn't ok
    assert g["PSU1 FAN1"]["thresholds"] == {"lcr": 460.0, "lnc": 600.0, "unc": 1504.0}   # lnr: "na"
    assert "thresholds" not in g["PMBPower2"]
    n = {s["name"]: s for s in hosts["node03"]["sensors"]}
    assert n["PMBPower"]["status"] == "ns" and n["+12V"]["value"] == "11.90 Volts"
    # the first read of a log: history
    assert hosts["node03"]["sel"]["baseline"] is True and len(hosts["node03"]["sel"]["new"]) == 3
    assert hosts["node03"]["sel"]["new"][2]["date"] == "Pre-Init"
    # read-only: nothing but sdr type, sensor get, sel info, sel elist
    import re
    for _h, _local, script in boards.calls:
        runs = re.findall(r"\$T ipmitool (\S+) (\S+)", script)
        assert runs and set(runs) <= {("sdr", "type"), ("sensor", "get"), ("sel", "info"), ("sel", "elist")}
        assert script.count("ipmitool") == len(runs) + script.count("command -v ipmitool")
    assert [c for c in boards.calls if c[0] == "head"][0][1] is True          # itself: its own sh, no ssh
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(str(out) + ".state").st_mode) == 0o600


def test_the_log_is_sent_once_then_only_what_is_new(tmp_path, boards):
    _run(tmp_path, "--nodes", "gpu09,node03")
    boards.calls.clear()
    doc, _ = _run(tmp_path, "--nodes", "gpu09,node03")
    assert doc["hosts"]["gpu09"]["sel"]["new"] == [] and "baseline" not in doc["hosts"]["gpu09"]["sel"]
    assert not any("sel elist" in s for _h, _l, s in boards.calls)          # the log unchanged: not read
    boards.sel["gpu09"].append("  11 | 11/03/2026 | 17:01:02 | Power Supply PSU2 AC Lost | Power Supply AC lost "
                               "| Asserted")
    doc, _ = _run(tmp_path, "--nodes", "gpu09,node03")
    sel = doc["hosts"]["gpu09"]["sel"]
    assert sel["baseline"] is False and [e["id"] for e in sel["new"]] == ["11"]


def test_one_run_at_a_time_and_a_folder_only_root_writes(tmp_path, boards, monkeypatch):
    import fcntl
    out = tmp_path / "power.json"
    with open(str(out) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert H.main(["--out", str(out), "--nodes", "gpu09"]) == 0
    assert not out.exists()                                     # the run still going keeps it
    monkeypatch.setattr(H.os, "geteuid", lambda: 0)
    os.chmod(tmp_path, 0o777)
    with pytest.raises(SystemExit, match="must be root's"):
        H.main(["--out", str(out), "--nodes", "gpu09"])


def test_gpu09_has_a_supply_with_no_input_and_node03_nothing_wrong():
    found = P.problems("gpu09", [dict(s, thresholds=GET.get(s["name"]) and dict(zip(("lnr", "lcr", "lnc"),
                                                                                    GET[s["name"]])))
                                 for s in _sensors("gpu09")])
    said = {(p["subject"], p["severity"], p["sensor"]) for p in found}
    assert ("power supply", "warning", "PSU1 AC Lost") in said
    assert ("power supply", "warning", "PSU1 FAN1") in said                   # its own fan: part of the supply
    assert not any(p["sensor"] == "PMBPower" for p in found)                 # 0 W under a 0 W threshold
    loose = [p["problem"] for p in found if p["sensor"] is None]
    assert loose == ["PMBPower gives 0 W while PMBPower2 gives 200 W: one supply carries the node"]
    assert P.problems("node03", _sensors("node03")) == []
    # a real one: +12V under its critical threshold; a fan stopped
    bad = [{"name": "+12V", "type": "Voltage", "status": "lcr", "value": "10.80 Volts",
            "thresholds": {"lcr": 11.04, "lnc": 11.4}},
           {"name": "FAN3", "type": "Fan", "status": "lcr", "value": "0 RPM", "thresholds": {"lcr": 460.0}},
           {"name": "PS2 Status", "type": "Power Supply", "status": "ok",
            "value": "Presence detected, Failure detected"},
           {"name": "PS Redundancy", "type": "Power Supply", "status": "ok", "value": "Fully Redundant"}]
    got = {p["sensor"]: (p["subject"], p["severity"]) for p in P.problems("n", bad)}
    assert got == {"+12V": ("voltage", "critical"), "FAN3": ("fans", "warning"),
                   "PS2 Status": ("power supply", "warning")}
    assert P.problems("n", bad, {"fans": False, "ignore": ["n:+12V"]})[0]["sensor"] == "PS2 Status"


def _sensors(host):
    return [s for t in H.TYPES for s in H.parse_sdr(SDR[host].get(t, "").splitlines(), t)[0]]


def test_log_entries_judged():
    def e(line):
        return H.parse_sel([line])[0]
    assert P.judge_event(e(SEL["gpu09"][0]))[0] == "warning"
    assert P.judge_event(e(SEL["gpu09"][1])) == ("warning", "PSU1 FAN1 0 RPM below 460")
    assert P.judge_event(e(SEL["gpu09"][2])) == (None, None)                   # 0 under 0
    assert all(P.judge_event(e(x)) == (None, None) for x in SEL["node03"])
    assert P.judge_event(e("  12 | 11/03/2026 | 17:05:00 | Power Supply PSU1 AC Lost | Power Supply AC lost "
                           "| Deasserted")) == (None, None)
    assert P.judge_event(e("   1 | 08/15/2017 | 18:04:47 | Processor CPU_CATERR | IERR | Asserted")) == (None, None)
    assert P.logged_at(e(SEL["node03"][2])) == "(clock not set)"


def _collect(tmp_path, monkeypatch, sent, rules=None):
    monkeypatch.setattr("nomad.alerts.send_alert", lambda **k: sent.append(k) or {"x": True})
    c = P.PowerCollector({"files": [str(tmp_path / "*.json")], "alert_rules": rules or {}}, tmp_path / "site.db")
    data = c.collect()
    if data:
        c.store(data)
    return c, data


def test_the_collector_keeps_and_alerts(tmp_path, boards, monkeypatch):
    _run(tmp_path, "--nodes", "gpu09,node03,nobmc")
    sent = []
    c, data = _collect(tmp_path, monkeypatch, sent)
    assert {d["node"] for d in data} == {"gpu09", "node03", "nobmc"}      # no --self: not the head itself
    db = sqlite3.connect(tmp_path / "site.db")
    hosts = {r[0]: r[1:] for r in db.execute("SELECT node, severity, problems, error FROM power_hosts")}
    # AC lost, and one supply carrying the node; its fans at 0 RPM since the first read aren't counted (1.26.0)
    assert hosts["gpu09"][:2] == ("warning", 2) and hosts["node03"][:2] == (None, 0)
    assert hosts["nobmc"][2].startswith("Could not open")
    assert db.execute("SELECT reading, unit FROM power_sensors WHERE node = 'node03' AND sensor = '+12V'"
                      ).fetchone() == (11.9, "Volts")
    assert db.execute("SELECT state FROM power_sensors WHERE node = 'gpu09' AND sensor = 'PSU1 AC Lost'"
                      ).fetchone() == ("Power Supply AC lost",)
    assert db.execute("SELECT COUNT(*), SUM(history) FROM power_events").fetchone() == (6, 6)
    # one alert: gpu09's power supply; the log's history isn't news
    assert [(k["host"], k["subject"], k["severity"]) for k in sent] == [("gpu09", "power supply", "warning")]
    assert "no input power" in sent[0]["message"] and "power cord" in sent[0]["message"]
    # read again with nothing new: nothing stored, nothing sent
    sent.clear()
    c, data = _collect(tmp_path, monkeypatch, sent)
    assert data == [] and "nothing newer" in c.note and sent == []
    # a new entry in gpu09's log: alerted
    boards.sel["gpu09"].append("  11 | 11/03/2026 | 17:01:02 | Power Supply PSU2 AC Lost | Power Supply AC lost "
                               "| Asserted")
    doc, out = _run(tmp_path, "--nodes", "gpu09,node03,nobmc")
    _later(doc, out)                                    # five minutes on (the test runs within a second)
    _collect(tmp_path, monkeypatch, sent)
    subjects = [(k["host"], k["subject"]) for k in sent]
    assert ("gpu09", "power events") in subjects and ("gpu09", "power supply") in subjects
    events = [k for k in sent if k["subject"] == "power events"][0]
    assert "11/03/2026 17:01:02" in events["message"] and "PSU2 AC Lost" in events["message"]


def _later(doc, out, minutes=5):
    for h in doc["hosts"].values():
        h["read_at"] = (datetime.fromisoformat(h["read_at"]) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    out.write_text(json.dumps(doc))


def test_an_old_file_is_said(tmp_path, boards, monkeypatch):
    doc, out = _run(tmp_path, "--nodes", "node03")
    doc["written"] = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    out.write_text(json.dumps(doc))
    c, _ = _collect(tmp_path, monkeypatch, [])
    assert "min old: is the helper's cron running?" in c.note
    c = P.PowerCollector({"files": [str(tmp_path / "none.json")]}, tmp_path / "site.db")
    with pytest.raises(P.CollectionError, match="isn't there"):
        c.collect()


def test_the_hub_alerts_for_a_site_that_leaves_them_to_it(tmp_path, boards, monkeypatch):
    from nomad.alerts import hub_power
    _run(tmp_path, "--nodes", "gpu09,node03")
    _collect(tmp_path, monkeypatch, [])
    # the hub's copy: the site's rows with source_site
    hub = tmp_path / "combined.db"
    src = sqlite3.connect(tmp_path / "site.db")
    dst = sqlite3.connect(hub)
    for t in ("power_hosts", "power_sensors", "power_events"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        dst.execute(f"CREATE TABLE {t} ({', '.join(cols)}, source_site TEXT)")
        for row in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):
            dst.execute(f"INSERT INTO {t} VALUES ({', '.join('?' * (len(cols) + 1))})", (*row, "vadur"))
    dst.commit()
    sent = []
    monkeypatch.setattr("nomad.alerts.dispatcher.send_alert",
                        lambda severity, source, message, **k: sent.append((severity, source, message, k)) or {"x": 1})
    cfg = {"hub": {"sites": [{"name": "vadur", "alerts": "hub"}, {"name": "spydur"}]}}
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg)
    assert [(s[1], s[3]["subject"]) for s in sent] == [("power", "vadur/gpu09 power supply")]
    assert "gpu09 at vadur:" in sent[0][2] and sent[0][2].endswith("Sent by the hub for vadur.")
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg)
    assert len(sent) == 1                                          # once, then every reminder_hours
    hub_power.hub_power_alerts(hub, tmp_path, config=dict(cfg, alerts={"power": {"ignore": ["gpu09:PSU1*"]}}),
                               now=datetime.now())
    assert len(sent) == 1


def test_nomad_power_says_it(tmp_path, boards, monkeypatch):
    from nomad.cli import cli
    _run(tmp_path, "--nodes", "gpu09,node03,nobmc")
    _collect(tmp_path, monkeypatch, [])
    toml = tmp_path / "nomad.toml"
    toml.write_text("")
    r = CliRunner().invoke(cli, ["-c", str(toml), "power", "--db", str(tmp_path / "site.db"), "--check"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "gpu09" in out and "WARNING" in out and "PSU1 AC Lost says" in out
    assert "supplies: PMBPower 0 W, PMBPower2 200 W" in out
    assert "node03" in out and "supplies: PMBPower: no reading (the board gets no figure from them" in out
    assert "nobmc" in out and "NOT WATCHED its board can't be read in-band" in out      # 1.27.0
    assert "check: warning: power supply: gpu09:" in out
    assert "h gpu09" in out                                         # its log's history, marked
    r = CliRunner().invoke(cli, ["-c", str(toml), "power", "--db", str(tmp_path / "site.db"), "--host", "node03",
                                 "--all", "--events", "0"])
    assert "+12V" in r.output and "11.9 Volts" in r.output and "gpu09" not in r.output


def test_the_plan_knows_it():
    from nomad.collectors.plan import SPECS, plan
    assert any(s.name == "power" and not s.default_on for s in SPECS)
    got = {p.spec.name: p for p in plan({"collectors": {"power": {"enabled": True, "files": ["/x"]}},
                                         "alerts": {"power": {"fans": False}}})}
    assert got["power"].enabled and got["power"].config["alert_rules"] == {"fans": False}


# ── what the review found (1.25.8) ────────────────────────────────────────────

def test_each_batch_of_log_entries_is_its_own_alert(tmp_path, boards, monkeypatch):
    """Two batches within an hour: both sent (each its own condition), through the real dispatcher."""
    from nomad.alerts.dispatcher import alert_key
    _run(tmp_path, "--nodes", "gpu09")
    sent = []
    _collect(tmp_path, monkeypatch, sent)
    keys = []
    for n, line in enumerate(("Power Supply PSU2 AC Lost | Power Supply AC lost | Asserted",
                              "Power Supply PSU2 Status | Failure detected | Asserted")):
        boards.sel["gpu09"].append(f"  1{n} | 11/03/2026 | 17:0{n}:00 | {line}")
        doc, out = _run(tmp_path, "--nodes", "gpu09")
        _later(doc, out, minutes=5 * (n + 1))
        sent.clear()
        _collect(tmp_path, monkeypatch, sent)
        ev = [k for k in sent if k["subject"] == "power events"]
        assert len(ev) == 1
        keys.append(alert_key({"source": "power", "host": "gpu09", "subject": "power events", "details": ev[0]["details"]}))
    assert keys[0] != keys[1]


def test_a_failed_command_keeps_what_was_known(tmp_path, boards, monkeypatch):
    _run(tmp_path, "--nodes", "gpu09")
    _collect(tmp_path, monkeypatch, [])
    db = sqlite3.connect(tmp_path / "site.db")
    volts = db.execute("SELECT COUNT(*) FROM power_sensors WHERE node = 'gpu09' AND type = 'Voltage'").fetchone()[0]
    assert volts == 2
    # the Voltage read times out, and so does the log's: rows and the log's place kept
    boards.fail |= {("gpu09", "Voltage"), ("gpu09", "sel")}
    boards.sel["gpu09"].append("  11 | 11/03/2026 | 17:01:02 | Power Supply PSU2 AC Lost | Power Supply AC lost "
                               "| Asserted")
    doc, out = _run(tmp_path, "--nodes", "gpu09")
    assert doc["hosts"]["gpu09"]["unread_types"] == ["Voltage"] and doc["hosts"]["gpu09"]["sel"]["new"] == []
    _later(doc, out)
    _collect(tmp_path, monkeypatch, [])
    assert db.execute("SELECT COUNT(*) FROM power_sensors WHERE node = 'gpu09' AND type = 'Voltage'").fetchone()[0] == 2
    boards.fail.clear()
    doc, _ = _run(tmp_path, "--nodes", "gpu09")
    assert [e["id"] for e in doc["hosts"]["gpu09"]["sel"]["new"]] == ["11"]        # read now, not months later


def test_a_log_said_in_other_words_is_still_known(tmp_path, boards):
    _run(tmp_path, "--nodes", "node03")
    boards.sel["node03"] = [x.replace("going low  |", "going low |").replace("Lower", "lower")
                            for x in boards.sel["node03"]]           # an ipmitool update's wording
    boards.sel["node03"].append("  21 | 11/03/2026 | 17:00:00 | Power Supply PS1 | Failure detected | Asserted")
    doc, _ = _run(tmp_path, "--nodes", "node03")
    sel = doc["hosts"]["node03"]["sel"]
    assert sel["baseline"] is False and not sel["cleared"] and [e["id"] for e in sel["new"]] == ["21"]
    boards.sel["node03"] = ["   1 | 11/03/2026 | 17:05:00 | Power Supply PS1 | Failure detected | Asserted"]
    doc, _ = _run(tmp_path, "--nodes", "node03")
    sel = doc["hosts"]["node03"]["sel"]
    assert sel["cleared"] is True and sel["baseline"] is False and len(sel["new"]) == 1


def test_output_without_end_is_cut_and_the_run_stops(monkeypatch):
    monkeypatch.setattr(H, "MAX_OUTPUT", 1024 * 1024)
    with pytest.raises(RuntimeError, match="more than 1 MB"):
        H.run_on("x", "yes __NOMAD_POWER__", 30, True)
    with pytest.raises(RuntimeError, match="no answer in 1 s"):
        H.run_on("x", "echo __NOMAD_POWER__; sleep 30 & sleep 30", 1, True)
    out = H.run_on("x", "echo banner; echo __NOMAD_POWER__ sdr Fan; echo 'FAN1 | 41h | ok | 29.1 | 5200 RPM'; "
                   "echo __NOMAD_POWER__ rc 0", 30, True)
    sec, rcs = H.sections(out)
    assert sec == {"sdr Fan": ["FAN1 | 41h | ok | 29.1 | 5200 RPM"]} and rcs == {"sdr Fan": 0}


def test_names_ipmitool_could_take_for_options_are_not_asked_about():
    assert not H.SENSOR_OK.match("-H evil") and not H.SENSOR_OK.match("-S/tmp/x")
    assert H.SENSOR_OK.match("+12V") and H.SENSOR_OK.match("PSU1 FAN1")


def test_sinfo_failing_reads_the_nodes_read_before(tmp_path, boards, monkeypatch):
    _run(tmp_path, "--nodes", "gpu09,node03")
    monkeypatch.setattr(H, "slurm_nodes", lambda timeout, sinfo: (None, "sinfo: Unable to contact slurm controller"))
    doc, _ = _run(tmp_path, "--nodes", "slurm")
    assert set(doc["hosts"]) == {"gpu09", "node03"}
    assert "couldn't list Slurm's nodes" in doc["notes"][0]


def test_states_beside_a_reading_two_sensors_of_one_name_and_negative_rails():
    hpe = {"name": "PS 1", "type": "Power Supply", "status": "ok", "value": "0 Watts, Presence detected, Power Supply AC lost"}
    assert P.judge(hpe)[0] == "warning" and "no input power" in P.judge(hpe)[1]
    assert P.split_value(hpe["value"]) == (0.0, "Watts", "Presence detected, Power Supply AC lost")
    assert P.judge({"name": "PS", "type": "Power Supply", "status": "ok",
                    "value": "Presence detected, Predictive failure"})[1].endswith("(it is predicted to fail)")
    minus = {"name": "-12V", "type": "Voltage", "status": "lcr", "value": "-14.20 Volts", "thresholds": {"lcr": -13.2}}
    assert P.judge(minus)[0] == "critical"
    dell = [{"name": "Status", "type": "Power Supply", "number": "63h", "status": "ok", "value": "Presence detected"},
            {"name": "Status", "type": "Power Supply", "number": "64h", "status": "ok",
             "value": "Presence detected, Failure detected"}]
    found = P.problems("r720", P.named(dell))
    assert [p["sensor"] for p in found] == ["Status #64h"]


def test_a_node_taken_out_goes_and_quiet_rules_apply_to_the_log(tmp_path, boards, monkeypatch):
    _run(tmp_path, "--nodes", "gpu09,node03")
    _collect(tmp_path, monkeypatch, [])
    doc, out = _run(tmp_path, "--nodes", "gpu09")
    _later(doc, out)
    _collect(tmp_path, monkeypatch, [])
    db = sqlite3.connect(tmp_path / "site.db")
    assert [r[0] for r in db.execute("SELECT node FROM power_hosts")] == ["gpu09"]
    assert db.execute("SELECT COUNT(*) FROM power_sensors WHERE node = 'node03'").fetchone()[0] == 0
    boards.sel["gpu09"].append("  11 | 11/03/2026 | 17:01:02 | Power Supply PSU2 AC Lost | Power Supply AC lost "
                               "| Asserted")
    doc, out = _run(tmp_path, "--nodes", "gpu09")
    _later(doc, out, minutes=10)
    sent = []
    _collect(tmp_path, monkeypatch, sent, rules={"ignore": ["gpu09:PSU*", "gpu09:PMBPower*"]})
    assert sent == []                 # PSU1, its fans, the supplies watts and PSU2 log entry: all ignored


def test_the_hub_holds_one_unread_host_not_its_site(tmp_path, boards, monkeypatch):
    from nomad.alerts import hub_power
    boards.sel["node03"].append("  21 | 11/03/2026 | 17:00:00 | Power Supply PMBPower | Failure detected | Asserted")
    _run(tmp_path, "--nodes", "gpu09,node03")
    _collect(tmp_path, monkeypatch, [])
    hub = tmp_path / "combined.db"
    src, dst = sqlite3.connect(tmp_path / "site.db"), sqlite3.connect(hub)
    for t in ("power_hosts", "power_sensors", "power_events"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        dst.execute(f"CREATE TABLE {t} ({', '.join(cols)}, source_site TEXT)")
        for row in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):
            dst.execute(f"INSERT INTO {t} VALUES ({', '.join('?' * (len(cols) + 1))})", (*row, "vadur"))
    # node03's last read: an error, and a new (not history) log entry made by hand
    dst.execute("UPDATE power_hosts SET error = 'no answer in 240 s' WHERE node = 'node03'")
    dst.execute("UPDATE power_events SET history = 0, severity = 'warning', problem = 'PS failed', "
                "first_seen = ? WHERE node = 'gpu09' AND record_id = 'a'", (datetime.now().isoformat(timespec="seconds"),))
    dst.commit()
    sent = []
    monkeypatch.setattr("nomad.alerts.dispatcher.send_alert",
                        lambda severity, source, message, **k: sent.append((k["subject"], k["details"])) or {"x": 1})
    cfg = {"hub": {"sites": [{"name": "vadur", "alerts": "hub"}]}}
    items, held = hub_power.due_power_alerts(sqlite3.connect(hub), ["vadur"], cfg, datetime.now())
    assert held == {"vadur/node03"}
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg)
    assert sorted(s for s, _d in sent) == ["vadur/gpu09 power events", "vadur/gpu09 power supply"]
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg)
    assert len(sent) == 2                                          # each once


def test_times_carry_their_offset(tmp_path, boards, monkeypatch):
    """root's cron and nomad's may not share a time zone: the helper says its offset."""
    import re as _re
    from datetime import timezone
    doc, _ = _run(tmp_path, "--nodes", "gpu09")
    assert _re.search(r"[+-]\d\d:\d\d$", doc["written"]) and _re.search(r"[+-]\d\d:\d\d$", doc["hosts"]["gpu09"]["read_at"])
    utc = datetime(2026, 11, 3, 17, 30, tzinfo=timezone.utc)
    assert P.local_time(utc.isoformat()) == utc.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    assert P.local_time("2026-11-03T12:30:00") == "2026-11-03T12:30:00"
    c, _ = _collect(tmp_path, monkeypatch, [])
    assert c.note is None or "min old" not in c.note


# ── the review's second round ─────────────────────────────────────────────────

def test_what_a_command_left_running_goes_and_errors_past_the_cap_are_let_go():
    import time as _t
    t0 = _t.monotonic()
    out = H.run_on("x", "(sleep 30 &) ; echo __NOMAD_POWER__ end", 30, True)
    assert out.startswith("__NOMAD_POWER__") and _t.monotonic() - t0 < 15
    out = H.run_on("x", "head -c 300000 /dev/zero | tr '\\\\0' e >&2; echo __NOMAD_POWER__ end", 30, True)
    assert out.strip() == "__NOMAD_POWER__ end"


def test_an_error_alone_is_not_an_empty_type(tmp_path, boards, monkeypatch):
    SDR["gpu09"]["Current"] = "Unable to open SDR for reading"
    try:
        doc, _ = _run(tmp_path, "--nodes", "gpu09")
    finally:
        del SDR["gpu09"]["Current"]
    assert doc["hosts"]["gpu09"]["unread_types"] == ["Current"]
    assert "Unable to open SDR for reading" in doc["hosts"]["gpu09"]["notes"]


def test_log_keys_after_a_clear_and_the_clear_itself():
    a = H.parse_sel(["   1 |  Pre-Init  |0000017233| Power Supply PMBPower | Lower Critical going low | Asserted"])[0]
    b = H.parse_sel(["   1 |  Pre-Init  |0000017233| Power Supply PSU1 AC Lost | Power Supply AC lost | Asserted"])[0]
    c = H.parse_sel(["   1 |  Pre-Init  |0000017233| Power Supply PMBPOWER | lower critical  going low | Asserted"])[0]
    assert a["key"] != b["key"] and a["key"] == c["key"]
    info = H.parse_selinfo(["Entries : 3", "Last Add Time : 01/01/1970 05:02:33", "Last Del Time : 11/03/2026 18:00:00"])
    assert info["last_del"] == "11/03/2026 18:00:00"


def test_sinfo_failing_forgets_no_host(tmp_path, boards, monkeypatch):
    boards.sel.pop("node03")                    # node03: no log, so no log state; still read before
    _run(tmp_path, "--nodes", "gpu09,node03")
    _collect(tmp_path, monkeypatch, [])
    monkeypatch.setattr(H, "slurm_nodes", lambda timeout, sinfo: (None, "sinfo: down"))
    doc, out = _run(tmp_path, "--nodes", "slurm")
    assert set(doc["hosts"]) == {"gpu09", "node03"} and doc["partial"] is True
    del doc["hosts"]["node03"]                  # had it been left out, it isn't forgotten either
    _later(doc, out)
    _collect(tmp_path, monkeypatch, [])
    db = sqlite3.connect(tmp_path / "site.db")
    assert sorted(r[0] for r in db.execute("SELECT node FROM power_hosts")) == ["gpu09", "node03"]


def test_the_hub_sends_a_batch_that_arrives_late(tmp_path, boards, monkeypatch):
    from nomad.alerts import hub_power
    _run(tmp_path, "--nodes", "gpu09")
    _collect(tmp_path, monkeypatch, [])
    hub = tmp_path / "combined.db"
    src, dst = sqlite3.connect(tmp_path / "site.db"), sqlite3.connect(hub)
    for t in ("power_hosts", "power_sensors", "power_events"):
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        dst.execute(f"CREATE TABLE {t} ({', '.join(cols)}, source_site TEXT)")
        for row in src.execute(f"SELECT {', '.join(cols)} FROM {t}"):
            dst.execute(f"INSERT INTO {t} VALUES ({', '.join('?' * (len(cols) + 1))})", (*row, "vadur"))
    # an entry the site read 13 hours ago reaches the hub now (vadur not pulled meanwhile)
    long_ago = (datetime.now() - timedelta(hours=13)).isoformat(timespec="seconds")
    dst.execute("UPDATE power_events SET history = 0, first_seen = ? WHERE record_id = 'a'", (long_ago,))
    dst.commit()
    sent = []
    monkeypatch.setattr("nomad.alerts.dispatcher.send_alert",
                        lambda severity, source, message, **k: sent.append(k["subject"]) or {"x": 1})
    cfg = {"hub": {"sites": [{"name": "vadur", "alerts": "hub"}]}}
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg)
    assert sent.count("vadur/gpu09 power events") == 1
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg, now=datetime.now() + timedelta(hours=1))
    hub_power.hub_power_alerts(hub, tmp_path, config=cfg, now=datetime.now() + timedelta(hours=13))
    assert sent.count("vadur/gpu09 power events") == 1           # once



# ── 1.26.0: a fan never seen turning is a header with no fan ─────────────────

def test_a_fan_is_alerted_when_it_stops_not_when_it_never_turned(tmp_path, boards, monkeypatch):
    """vadur's CPU nodes: SYS_FAN1-4 at 0 RPM on every board (headers with no fan), 4 Nov 2026."""
    fans = "SYS_FAN1         | 41h | lnr | 29.1 | 0 RPM\nFRNT_FAN1        | 44h | ok  | 29.4 | 4200 RPM"
    SDR["node03"]["Fan"], before = fans, SDR["node03"]["Fan"]
    GET["SYS_FAN1"] = (600.0, 600.0, 700.0)
    GET["FRNT_FAN1"] = (600.0, 600.0, 700.0)
    try:
        doc, out = _run(tmp_path, "--nodes", "node03")
        sent = []
        _collect(tmp_path, monkeypatch, sent)
        db = sqlite3.connect(tmp_path / "site.db")
        assert sent == []
        assert "SYS_FAN1 at 0 RPM since nomad first read it (no fan on the header?)" in \
            db.execute("SELECT notes FROM power_hosts WHERE node = 'node03'").fetchone()[0]
        assert db.execute("SELECT sensor, spun FROM power_sensors WHERE node = 'node03' AND unit = 'RPM' "
                          "ORDER BY sensor").fetchall() == [("FRNT_FAN1", 1), ("SYS_FAN1", 0)]
        # the fan that turned stops: alerted
        SDR["node03"]["Fan"] = fans.replace("| ok  | 29.4 | 4200 RPM", "| lnr | 29.4 | 0 RPM")
        doc, out = _run(tmp_path, "--nodes", "node03")
        _later(doc, out)
        _collect(tmp_path, monkeypatch, sent)
        assert [(k["host"], k["subject"]) for k in sent] == [("node03", "fans")]
        assert "FRNT_FAN1 0 RPM, below its non-recoverable 600" in sent[0]["message"]
        assert "SYS_FAN1" not in sent[0]["message"]
    finally:
        SDR["node03"]["Fan"] = before
        del GET["SYS_FAN1"], GET["FRNT_FAN1"]


def test_a_1258_table_gets_the_new_column(tmp_path, boards, monkeypatch):
    db = sqlite3.connect(tmp_path / "site.db")
    db.executescript(P.PowerCollector.SCHEMA.replace(", spun INTEGER", ""))
    db.commit()
    _run(tmp_path, "--nodes", "gpu09")
    _collect(tmp_path, monkeypatch, [])
    assert "spun" in {r[1] for r in db.execute("PRAGMA table_info(power_sensors)")}


def test_a_fan_keeps_its_history_through_no_reading_and_the_upgrade(tmp_path, boards, monkeypatch):
    """Review of 1.26.0: a read with no RPM ("No Reading") keeps what was known; rows 1.25.8 kept
    (spun unknown) are judged by their own history, not taken for empty headers."""
    fans = "SYS_FAN1         | 41h | lnr | 29.1 | 0 RPM\nFRNT_FAN1        | 44h | ok  | 29.4 | 4200 RPM"
    SDR["node03"]["Fan"], before = fans, SDR["node03"]["Fan"]
    GET["SYS_FAN1"] = (600.0, 600.0, 700.0)
    GET["FRNT_FAN1"] = (600.0, 600.0, 700.0)
    db = sqlite3.connect(tmp_path / "site.db")
    spun = "SELECT sensor, spun, severity FROM power_sensors WHERE node = 'node03' AND sensor LIKE '%FAN1' ORDER BY sensor"
    try:
        doc, out = _run(tmp_path, "--nodes", "node03")
        _collect(tmp_path, monkeypatch, [])
        # the board can't read it for a while
        SDR["node03"]["Fan"] = fans.replace("| ok  | 29.4 | 4200 RPM", "| ns  | 29.4 | No Reading")
        doc, out = _run(tmp_path, "--nodes", "node03")
        _later(doc, out)
        _collect(tmp_path, monkeypatch, [])
        assert db.execute(spun).fetchall() == [("FRNT_FAN1", 1, None), ("SYS_FAN1", 0, None)]
        # then it stops: alerted
        SDR["node03"]["Fan"] = fans.replace("| ok  | 29.4 | 4200 RPM", "| lnr | 29.4 | 0 RPM")
        doc, out = _run(tmp_path, "--nodes", "node03")
        _later(doc, out, 10)
        sent = []
        _collect(tmp_path, monkeypatch, sent)
        assert [(k["host"], k["subject"]) for k in sent] == [("node03", "fans")]
        # as 1.25.8 kept them: nothing known of turning
        db.execute("UPDATE power_sensors SET spun = NULL")
        db.commit()
        doc, out = _run(tmp_path, "--nodes", "node03")
        _later(doc, out, 15)
        _collect(tmp_path, monkeypatch, [])
        assert db.execute(spun).fetchall() == [("FRNT_FAN1", 1, "warning"), ("SYS_FAN1", 0, None)]
    finally:
        SDR["node03"]["Fan"] = before
        del GET["SYS_FAN1"], GET["FRNT_FAN1"]
