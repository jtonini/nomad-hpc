# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.38.0: a person's own heavy use of a login node, told to them
(nomad.edu.login_node): what the per_user collector caught, filtered to
what is worth telling, one entry per process, with the better way."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from nomad.db.migrations import ensure_database
from nomad.edu import login_node as L

NOW = datetime(2026, 11, 10, 21, 0, 0, tzinfo=timezone.utc)
MIN = 60


def utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


RULES = {   # rule_id: (rule_type, threshold, unit) as the collector's defaults have them
    "cpu_10pct_5min": ("cpu", 10.0, "percent"), "cpu_50pct_2min": ("cpu", 50.0, "percent"),
    "memory_4gb_10min": ("memory", 4.0, "gb"), "memory_16gb_2min": ("memory", 16.0, "gb"),
    "user_cpu_200pct_10min": ("user_cpu", 200.0, "percent"),
    "user_memory_32gb_10min": ("user_memory", 32.0, "gb"), "io_50mbs_10min": ("io", 50.0, "mb_s"),
    "interactive_user_cpu_800pct_30min": ("user_cpu", 800.0, "percent"),
}


_SEQ = [0]


def row(rule="cpu_50pct_2min", user="stu01", held=20 * MIN, end=NOW - timedelta(hours=2), command="python3",
        cmdline=None, session="s1", host="spydur.cluster", role="headnode", site="spydur", cpu=150.0, mem=None):
    _SEQ[0] += 1
    n = _SEQ
    rtype, threshold, unit = RULES[rule]
    return {"fired_at": utc(end), "hostname": host, "role": role, "username": user, "uid": 5000, "pid": 100 + n[0],
            "process_session_id": session, "rule_id": rule, "rule_type": rtype, "severity": "actionable",
            "threshold_value": threshold, "threshold_unit": unit, "sustained_for_seconds": held,
            "command": command, "cmdline": cmdline if cmdline is not None else f"{command} run.py --big",
            "ancestry_chain": "[]", "peak_cpu_percent": cpu, "peak_memory_bytes": mem,
            "dedup_key": f"k{n[0]}", "occurrences": 1, "last_seen": utc(end), "edu_template_id": None,
            "source_site": site}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "combined.db"
    ensure_database(p)
    with sqlite3.connect(p) as c:
        c.execute("ALTER TABLE per_user_alert ADD COLUMN source_site TEXT")     # as the hub's sync adds it
    return p


def put(db, *rows):
    with sqlite3.connect(db) as c:
        for r in rows:
            cols = ",".join(r)
            c.execute(f"INSERT INTO per_user_alert ({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))


def tell(*rows, **kw):
    return L.from_rows(rows, L.Settings(**kw), NOW)


# -- what is told ------------------------------------------------------------------------------

def test_what_is_worth_telling():
    # the low rule (a tenth of a core): a pip install reaches it; never told
    assert tell(row("cpu_10pct_5min", held=3 * 3600, cpu=40.0)) == []
    # half a core: told when it held 15 minutes or more
    assert tell(row(held=14 * MIN)) == []
    (f,) = tell(row(held=15 * MIN))
    assert (f.kind, f.title, f.program, f.site, f.host) == (
        "cpu", "A long computation on the login node", "python3", "spydur", "spydur")
    assert f.minutes == 15 and f.cores == 1.5 and f.memory_gb is None and not f.ongoing
    assert "login node" in f.why and "srun --pty" in " ".join(f.better) and "Submit a job" in f.better[0]
    # memory: 16 GB in one program is told, 4 GB is not; 32 GB together is
    assert tell(row("memory_4gb_10min", held=3 * 3600, mem=6 * L.GB)) == []
    assert tell(row("memory_16gb_2min", held=3 * MIN, mem=20 * L.GB))[0].memory_gb == 20.0
    assert tell(row("user_memory_32gb_10min", session="user-5000", mem=40 * L.GB))[0].kind == "together_memory"
    # many programs together: told as the person's, named after the busiest
    (t,) = tell(row("user_cpu_200pct_10min", session="user-5000", command="make", cpu=420.0))
    assert (t.kind, t.program, t.command, t.cores) == ("together_cpu", "make", "", 4.2)
    assert t.better[0].startswith("Build inside a job")
    # a large transfer: told, unless the program is one that moves data in or out
    assert tell(row("io_50mbs_10min", command="python3"))[0].kind == "transfer"
    for prog in ("scp", "sftp-server", "rsync", "sshd"):
        assert tell(row(held=3 * 3600, command=prog), row("io_50mbs_10min", command=prog)) == []
    assert tell(row(held=3 * 3600, command="tar"))[0].better[0].startswith("Pack or compress")
    # other settings
    assert tell(row(held=20 * MIN), min_minutes=30) == []
    assert tell(row(held=20 * MIN), min_cores=1) == []                 # the 50% rule is under one core
    assert tell(row("memory_16gb_2min", mem=20 * L.GB), min_memory_gb=32) == []
    assert tell(row(held=3 * 3600, command="scp"), skip_programs=()) != []
    # the interactive server's own rules are not a login node's (role, read in findings)


def test_one_entry_per_process_and_per_together_time():
    a = row("cpu_10pct_5min", held=3 * 3600, cpu=180.0, session="p1")
    b = row("cpu_50pct_2min", held=2 * 3600, cpu=180.0, session="p1")
    c = row("memory_16gb_2min", held=40 * MIN, mem=18 * L.GB, session="p1")
    (f,) = tell(a, b, c)
    assert f.kind == "memory" and sorted(f.rules) == ["cpu_50pct_2min", "memory_16gb_2min"]
    assert f.cores == 1.8 and f.memory_gb == 18.0 and f.minutes == 120
    # two processes: two entries, newest first
    old = row(session="p2", end=NOW - timedelta(days=3), command="Rscript")
    assert [x.program for x in tell(b, old)] == ["python3", "Rscript"]
    # the same process busy again after a long pause: two times, two entries
    later = row(session="p1", end=NOW - timedelta(minutes=10))
    assert len(tell(b, later)) == 2 and tell(b, later)[0].ongoing
    # a process inside a time the person's processes together were caught is part of it
    together = row("user_cpu_200pct_10min", session="user-5000", held=3 * 3600, end=NOW - timedelta(hours=1),
                   command="python3", cpu=600.0)
    inside = row(session="p9", held=40 * MIN, end=NOW - timedelta(hours=2), command="R", cpu=95.0)
    outside = row(session="p8", held=40 * MIN, end=NOW - timedelta(days=2), command="julia")
    got = tell(together, inside, outside)
    assert [(x.kind, x.program) for x in got] == [("together_cpu", "python3"), ("cpu", "julia")]
    assert got[0].also == ["R"] and got[0].cores == 6.0


@pytest.fixture
def richmond(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_times_are_the_readers_local_time(richmond):
    # the collector writes UTC: 20:00 UTC on 10 Nov is 15:00 in Richmond
    (f,) = tell(row(held=30 * MIN, end=NOW - timedelta(hours=1)))
    assert (f.started, f.last_seen) == ("2026-11-10T14:30", "2026-11-10T15:00")
    # still going at the last reading
    assert tell(row(end=NOW - timedelta(minutes=5)))[0].ongoing
    # a time it can't read is left out; no site: the host's short name
    bad = row()
    bad["last_seen"] = "soon"
    assert tell(bad) == []
    nosite = row(site=None, host="login02.hpc.example.edu")
    assert tell(nosite)[0].site == "login02"


# -- from the database --------------------------------------------------------------------------

def test_findings_from_the_database(db, tmp_path):
    put(db, row(user="stu01"), row(user="stu02", session="other"),
        row(user="stu01", session="old", end=NOW - timedelta(days=40)),
        row("interactive_user_cpu_800pct_30min", user="stu01", session="user-5000", role="interactive",
            host="spdrweb.example.edu", site="spiderweb", cpu=900.0))
    got = L.findings(db, "stu01", {}, now=NOW)
    assert [(f.site, f.program) for f in got] == [("spydur", "python3")]       # theirs, a login node, 30 days
    assert L.findings(db, "stu01", {"notices": {"login_node": {"days": 60}}}, now=NOW)[-1].minutes == 20
    assert len(L.findings(db, "stu01", {"notices": {"login_node": {"days": 60}}}, now=NOW)) == 2
    assert L.findings(db, "stu01", {"notices": {"login_node": {"show": False}}}, now=NOW) == []
    assert L.findings(db, "", {}, now=NOW) == []
    assert L.findings(tmp_path / "none.db", "stu01", {}, now=NOW) == []
    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).close()
    assert L.findings(plain, "stu01", {}, now=NOW) == []                        # no such table
    # a site's own database (no source_site column): the host names the site
    site_db = tmp_path / "site.db"
    ensure_database(site_db)
    r = row(user="stu01")
    del r["source_site"]
    put(site_db, r)
    assert L.findings(site_db, "stu01", {}, now=NOW)[0].site == "spydur"


def test_what_the_console_and_the_command_line_get():
    rows = [row(session=f"p{i}", end=NOW - timedelta(hours=i + 1)) for i in range(L.SHOWN + 3)]
    found = tell(*rows)
    s = L.summary(found, 30)
    assert s["days"] == 30 and len(s["findings"]) == L.SHOWN and s["more"] == 3
    assert set(s["findings"][0]) >= {"kind", "title", "why", "better", "site", "program", "command", "started",
                                     "minutes", "cores", "memory_gb", "ongoing", "also"}
    json.dumps(s)
    text = L.text(found[:1], 30)
    assert text.startswith("\nOn the login nodes (last 30 days)")
    assert "python3 on spydur" in text and "for 20 min (1.5 cores)" in text and "python3 run.py --big" in text
    assert "srun --pty" in text
    assert L.text([], 30) == ""
    assert L._span(75) == "1 h 15 min" and L._span(120) == "2 h" and L._span(5) == "5 min"


def test_settings_and_what_config_check_says():
    s = L.settings({"notices": {"login_node": {"days": 0, "min_minutes": "x", "min_cores": True,
                                               "skip_programs": "scp", "show": "no"}}})
    assert (s.days, s.min_minutes, s.min_cores, s.skip_programs, s.show) == (30, 15.0, 0.5, L.SKIP_PROGRAMS, True)
    assert L.settings({"notices": {"login_node": {"skip_programs": [" SCP ", ""]}}}).skip_programs == ("scp",)
    assert L.settings({"notices": "x"}) == L.Settings() and L.settings(None) == L.Settings()
    assert L.config_problems({}) == [] and L.config_problems({"notices": {}}) == []
    assert L.config_problems({"notices": "x"}) == ["[notices] is not a table"]
    got = L.config_problems({"notices": {"login_node": {"days": 0, "show": "no", "colour": 1,
                                                        "skip_programs": "scp"}}})
    assert any("colour: not a setting" in p for p in got) and any("show = 'no'" in p for p in got)
    assert any("days = 0" in p for p in got) and any("skip_programs" in p for p in got)


# -- the command line ----------------------------------------------------------------------------

def test_nomad_edu_me_says_it(db, tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    monkeypatch.setenv("HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    put(db, row(user="stu01", end=now - timedelta(hours=2)))
    r = CliRunner().invoke(cli_mod.cli, ["edu", "me", "--db", str(db), "--user", "stu01"])
    assert r.exit_code == 0, r.output
    assert "On the login nodes (last 30 days)" in r.output and "A long computation on the login node" in r.output
    r = CliRunner().invoke(cli_mod.cli, ["edu", "me", "--db", str(db), "--user", "stu01", "--json"])
    got = json.loads(r.output)["login_node"]
    assert got["days"] == 30 and got["findings"][0]["program"] == "python3"
    r = CliRunner().invoke(cli_mod.cli, ["edu", "me", "--db", str(db), "--user", "stu02"])
    assert r.exit_code == 0 and "login nodes" not in r.output


def test_nomad_config_check_says_what_is_wrong(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[notices.login_node]\nmin_minutes = \"15\"\n")
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "config", "check"])
    assert "[notices.login_node] min_minutes = '15': a number" in r.output, r.output
