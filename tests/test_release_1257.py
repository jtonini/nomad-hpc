"""1.25.7: a chip's cores as one temperature; an alert with nowhere to go
isn't counted as sent; a test send always goes; the GPU collector's ssh
skips a login banner and logs in as ssh's own settings say."""
import sqlite3
import subprocess
from datetime import timedelta

from click.testing import CliRunner

from nomad.collectors import temps as T
from tests.test_alert_episodes import _Backend, _db, _run, clock  # noqa: F401  (fixture)


def test_a_sockets_cores_are_one_reading_their_hottest():
    # spydur's Xeons: 26 cores a socket, 1,674 rows a round in 1.25.6
    lines = ["T|coretemp|coretemp.0|temp1|Package id 0|61000|100000"]
    lines += [f"T|coretemp|coretemp.0|temp{i + 2}|Core {i}|{40000 + i * 500}|100000" for i in range(26)]
    lines += ["T|coretemp|coretemp.1|temp1|Package id 1|58000|100000",
              "T|coretemp|coretemp.1|temp2|Core 0|52000|100000", "T|nvme|nvme0|temp1|Composite|41000|"]
    rows = T.parse("spdr01", "\n".join(lines), "2026-11-03T11:30:00")
    got = sorted((r["chip"], r["sensor"], r["label"], r["temp_c"]) for r in rows)
    assert got == [("coretemp@coretemp.0", "cores", "Cores (hottest)", 52.5),
                   ("coretemp@coretemp.0", "temp1", "Package id 0", 61.0),
                   ("coretemp@coretemp.1", "cores", "Cores (hottest)", 52.0),
                   ("coretemp@coretemp.1", "temp1", "Package id 1", 58.0),
                   ("nvme", "temp1", "Composite", 41.0)]
    assert {r["crit_c"] for r in rows if r["sensor"] == "cores"} == {100.0}
    assert [r["label"] for r in T.cpu_sensor(rows)] == ["Package id 0", "Package id 1"]    # still the alert's


def test_the_per_core_rows_kept_before_are_taken_out(tmp_path):
    from nomad.db.migrations import _temps_cores_as_one
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    conn.executescript(T.TempsCollector.SCHEMA)
    for chip, label in (("coretemp@coretemp.0", "Core 3"), ("coretemp", "Core 0"), ("coretemp", "Package id 0"),
                        ("k10temp", "Tctl"), ("coretemp", "Cores (hottest)")):
        conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES "
                     "('2026-11-03T11:00:00', 'n', ?, 'x', ?, 40.0)", (chip, label))
        conn.execute("INSERT INTO hw_temps_hourly (hour, node, chip, sensor, label, min_c, avg_c, max_c, readings) "
                     "VALUES ('2026-11-03T11:00:00', 'n', ?, ?, ?, 40, 40, 40, 1)", (chip, label, label))
    _temps_cores_as_one(conn)
    for table in ("hw_temps", "hw_temps_hourly"):
        left = sorted(r[0] for r in conn.execute(f"SELECT label FROM {table}"))
        assert left == ["Cores (hottest)", "Package id 0", "Tctl"], table
    _temps_cores_as_one(sqlite3.connect(":memory:"))               # no tables: nothing, no error


class _Nothing(_Backend):
    """A site with no alert backends (mingus until 3 Nov): dispatch gets none."""


def test_an_alert_with_nowhere_to_go_is_not_counted_as_sent(tmp_path, clock):  # noqa: F811
    from nomad.alerts.dispatcher import AlertDispatcher
    path = _db(tmp_path)
    # no backends: raised into nothing, then email set up: the next run sends it (a retry), not a day later
    d = AlertDispatcher({"database": {"path": path.name}, "general": {"data_dir": str(path.parent)}, "alerts": {}})
    d.backends = []
    a = {"severity": "warning", "source": "temps", "host": "w", "subject": "CPU", "message": "hot"}
    assert d.dispatch(dict(a)) == {}
    row = sqlite3.connect(path).execute("SELECT send_failures, retry_at FROM alert_state").fetchone()
    assert row[0] == 1 and row[1]
    clock.advance(minutes=20)
    backend = _Backend()
    _run(path, backend, source="temps", host="w", subject="CPU", message="hot")
    assert len(backend.sent) == 1


def test_a_test_always_goes(tmp_path, clock):  # noqa: F811
    path, backend = _db(tmp_path), _Backend()
    for _ in range(3):
        _run(path, backend, source="temps", host="w", subject="CPU (a test)", test=True)
        clock.advance(minutes=1)
    assert len(backend.sent) == 3
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM alert_state").fetchone()[0] == 0


def test_nomad_temps_says_why_it_sent_nothing(tmp_path, monkeypatch):
    from datetime import datetime
    from nomad.cli import cli
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    conn.executescript(T.TempsCollector.SCHEMA)
    now = datetime.now()
    for m in range(0, 10):
        conn.execute("INSERT INTO hw_temps (timestamp, node, chip, sensor, label, temp_c) VALUES "
                     "(?, 'w', 'k10temp', 'temp1', 'Tctl', 73.0)", ((now - timedelta(minutes=m)).isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()
    toml = tmp_path / "nomad.toml"
    args = ["temps", "--db", str(db), "--host", "w", "--check", "--warning", "70", "--send"]
    toml.write_text("[alerts.email]\nenabled = false\n")
    r = CliRunner().invoke(cli, ["-c", str(toml)] + args)
    assert "not sent: no alert backends here" in r.output
    toml.write_text('[alerts]\nmin_severity = "critical"\n[alerts.email]\nenabled = true\nrecipients = ["a@b.edu"]\n')
    r = CliRunner().invoke(cli, ["-c", str(toml)] + args)
    assert "not sent: warning is below [alerts] min_severity (critical)" in r.output
    sent = []
    import nomad.alerts as A
    monkeypatch.setattr(A, "send_alert", lambda **k: sent.append(k) or {"EmailBackend": True})
    toml.write_text('[alerts.email]\nenabled = true\nrecipients = ["a@b.edu"]\n')
    for _ in range(2):
        r = CliRunner().invoke(cli, ["-c", str(toml)] + args)
        assert "sent: EmailBackend sent" in r.output
    assert len(sent) == 2 and all(k["test"] for k in sent)


def test_the_gpu_collector_skips_a_login_banner(tmp_path, monkeypatch):
    from nomad.collectors import gpu
    seen = []
    banner = (" ____\n/ This computer is peterslabworkstation \\\n GPU INFO\n24564 MiB, 2244 MiB, 345.49 W, 61\n")

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, banner + gpu.SSH_MARK + "\nreal output\n", "")
    monkeypatch.setattr(gpu.subprocess, "run", run)
    c = gpu.GPUCollector({"gpu_nodes": ["peterslabworkstation"]}, tmp_path / "db")
    assert c._run_command_ssh("nvidia-smi -L", "peterslabworkstation") == "real output"
    argv = seen[-1]
    assert "-l" not in argv and argv[-3:-1] == ["--", "peterslabworkstation"]     # ssh's own User
    assert argv[-1] == f"echo {gpu.SSH_MARK}; nvidia-smi -L"
    c = gpu.GPUCollector({"gpu_nodes": ["node51"], "ssh_user": "zeus"}, tmp_path / "db")
    c._run_command_ssh("true", "node51")
    assert seen[-1][seen[-1].index("-l") + 1] == "zeus"
    # no mark (the login failed before the command): nothing read from the banner
    monkeypatch.setattr(gpu.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, banner, ""))
    assert c._run_command_ssh("nvidia-smi -L", "node51") is None
