# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.59.0: whether licensed programs run on the lab machines -- a tiny job with
each install of each program listed, on each workstation, twice a day; the
hub lists it and the daily license email says each change once."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from nomad import license_tries as T
from nomad import licenses as L

KEY = "44PlO1JyeIzyEA2zzNOTAREALKEYzz4B9N1HeCSkQXk"


def _install(root: Path, name: str, how: str, qc_setup: str | None = None) -> Path:
    """A stand-in Q-Chem install: its license file, its setup, its qchem."""
    q = root / name
    (q / "qcaux" / "license").mkdir(parents=True)
    (q / "qcaux" / "license" / "qchem.license.dat").write_text("an encrypted license\n")
    (q / "bin").mkdir()
    (q / "qcenv.sh").write_text(f'export QC={qc_setup or q}\nexport PATH="$QC/bin:$PATH"\n')
    body = {
        "ok": 'echo " Total energy in the final basis set = -1.1167" > "$2"\n'
              'echo "  Thank you very much for using Q-Chem.  Have a nice day." >> "$2"\n',
        "expired": f'echo " License file: {q}/qcaux/license/qchem.license.dat"\n'
                   f'echo " Expired license 09-12-26 {KEY}"\n'
                   'echo " Unable to validate license on $(hostname -s) SID 3A0000059F"\nexit 1\n',
        "notcovered": 'echo " failed standard license check on $(hostname -s) SID 4F43DE1058"\nexit 1\n',
        "hang": "sleep 30\n",
        "broken": 'echo "qcprog.exe: error while loading shared libraries: libgfortran.so.3"\nexit 127\n',
    }[how]
    (q / "bin" / "qchem").write_text("#!/bin/bash\n" + body)
    (q / "bin" / "qchem").chmod(0o755)
    for p in (q, *q.rglob("*")):
        os.chmod(p, 0o755 if p.is_dir() or p.name in ("qchem",) else 0o644)
    return q


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "sw"
    chem = root / "chem.sw"
    chem.mkdir(parents=True)
    os.chmod(tmp_path, 0o755)
    os.chmod(root, 0o755)
    os.chmod(chem, 0o755)
    _install(chem, "qchem64", "ok")
    _install(chem, "qchem70", "expired")
    _install(chem / "qchem", "qchem63", "notcovered")
    os.chmod(chem / "qchem", 0o755)
    _install(chem, "qchem611", "ok", qc_setup=str(tmp_path / "gone" / "qchem611"))
    _install(chem / "qchem", "qchem521", "ok")          # older than min_version: left out
    me = "root" if os.geteuid() == 0 else os.environ.get("USER", "nobody")
    s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem", "min_version": "6",
                                  "roots": [str(root)]}],
                    "workstation_hosts": ["localhost"], "run_as": me, "timeout": 20})
    return s, root


def test_settings():
    s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem", "min_version": "6.4"},
                                 {"name": "X", "recipe": "unknown"}, {"recipe": "qchem"},
                                 {"name": "Y", "recipe": "qchem", "min_version": "six"}],
                    "workstation_hosts": ["ws1", "ws1", "bad host"], "run_as": "Bad Name", "timeout": 5})
    assert [p.name for p in s.programs] == ["Q-Chem", "Y"] and s.programs[0].min_version == "6.4"
    assert s.machines == ["ws1"] and s.run_as == "nomad" and s.ssh_user == "root" and s.timeout == 10
    assert any("unknown" in p for p in s.problems) and any("needs a name" in p for p in s.problems)
    assert any("'bad host'" in p for p in s.problems) and any("six" in p for p in s.problems)
    # its own machines over the workstations'
    assert T.settings({"machines": ["a", "b"], "workstation_hosts": ["c"]}).machines == ["a", "b"]


def test_versions_and_reasons():
    assert [T.version_of(x) for x in ("/x/qchem64", "/x/qchem611", "/x/qchem70", "/opt/qchem70.cpu-old",
                                      "/x/qchem", "/x/qchem5")] == ["6.4", "6.1.1", "7.0", "7.0", "", "5"]
    said = [" License file: /usr/local/chem.sw/qchem70/qcaux/license/qchem.license.dat",
            f" Expired license 09-12-26 {KEY}", " Unable to validate license on erica SID 3A0000059F"]
    assert T.reason("license", said, None, 120) == "the license expired on 12 Sep 2026"
    assert T.reason("license", [" failed standard license check on kevin SID 4F43DE1058"], None, 120) == \
        "this machine isn't in the license"
    odd = T.reason("license", [" License file: /x", f" license key {KEY} refused for SID 3A0000059F"], None, 120)
    assert KEY not in odd and "3A0000059F" not in odd and odd.startswith("license refused:")
    assert T.reason("nostart", ["no qchem command in /usr/local/qchem70/bin"],
                    ("/usr/local/qchem70", "0"), 120) == \
        "its setup says it is in /usr/local/qchem70, which isn't on this machine"
    assert T.reason("timeout", [], None, 90) == "still running after 90 s"
    assert T.reason("failed", ["x", f"boom {KEY}"], None, 1) == "boom (key)"
    assert T.reason("runs", ["anything"], None, 1) == ""


def test_parse():
    out = ("a banner\n" + T.MARK + "\n@host\tkevin\tnomad\n"
           "@try\tQ-Chem\tqchem\t/x/qchem64\truns\t5\n@setup\t/x/qchem64\t1\n"
           "@try\tQ-Chem\tqchem\t/x/qchem70\tlicense\t1\n@setup\t/x/qchem70\t1\n"
           f"@said\t Expired license 09-12-26 {KEY}\n"
           "@try\tQ-Chem\tqchem\t/x/qchem9\tweird\t1\n")
    name, rows = T.parse(out, 120)
    assert name == "kevin" and len(rows) == 2
    assert rows[0] == {"program": "Q-Chem", "recipe": "qchem", "install": "/x/qchem64", "version": "6.4",
                       "result": "runs", "seconds": 5, "reason": ""}
    assert rows[1]["reason"] == "the license expired on 12 Sep 2026"


def test_tried_on_a_machine(lab, tmp_path):
    s, root = lab
    before = set(os.listdir("/tmp"))
    res = T.try_machine("localhost", s)
    assert res["reached"] and set(os.listdir("/tmp")) - before == set()        # nothing left behind
    got = {Path(r["install"]).name: (r["version"], r["result"], r["reason"]) for r in res["rows"]}
    assert got == {
        "qchem64": ("6.4", "runs", ""),
        "qchem70": ("7.0", "license", "the license expired on 12 Sep 2026"),
        "qchem63": ("6.3", "license", "this machine isn't in the license"),
        "qchem611": ("6.1.1", "nostart",
                     f"its setup says it is in {tmp_path / 'gone' / 'qchem611'}, which isn't on this machine"),
    }
    assert KEY not in repr(res)
    # a hanging one: given up on at the limit
    _install(root / "chem.sw", "qchem65", "hang")
    s.timeout = 10
    hung = [r for r in T.try_machine("localhost", s)["rows"] if r["install"].endswith("qchem65")]
    assert hung[0]["result"] == "timeout" and hung[0]["reason"] == "still running after 10 s"


def test_reached_over_ssh():
    s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem"}], "ssh_user": "root"})
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 255, "", "ssh: connect to host ws9 port 22: Connection timed out\n")
    res = T.try_machine("ws9", s, run=run)
    argv, kw = seen[0]
    assert argv[0] == "ssh" and "root@ws9" in argv and "LogLevel=ERROR" in argv and "BatchMode=yes" in argv
    assert argv[-1].startswith(f"echo {T.MARK}; bash -s -- nomad 120") and "try_qchem" in kw["input"]
    assert not res["reached"] and "timed out" in res["note"]


def _results(rows_by_machine, unreached=()):
    out = [{"machine": m, "reached": True, "note": "", "rows": [
        {"program": "Q-Chem", "recipe": "qchem", "install": i, "version": T.version_of(i), "result": r,
         "reason": w, "seconds": 1} for i, r, w in rows]} for m, rows in rows_by_machine.items()]
    return out + [{"machine": m, "reached": False, "note": "no answer", "rows": []} for m in unreached]


def test_store_keeps_since_while_the_result_stays():
    conn = sqlite3.connect(":memory:")
    progs = [T.Program("Q-Chem", "qchem", 6)]
    T.store(conn, _results({"kevin": [("/q64", "runs", ""), ("/q70", "license", "x")],
                            "erica": [("/q64", "license", "this machine isn't in the license")]}), progs,
            datetime(2026, 11, 20, 8))
    T.store(conn, _results({"kevin": [("/q64", "runs", ""), ("/q70", "runs", "")]}, unreached=["erica"]), progs,
            datetime(2026, 11, 20, 20))
    rows = {(r["machine"], r["install"]): r for r in T.listing(conn)["tries"]}
    assert rows[("kevin", "/q64")]["since"] == "2026-11-20T08:00:00"          # kept: still runs
    assert rows[("kevin", "/q70")]["since"] == "2026-11-20T20:00:00"          # changed: anew
    assert rows[("erica", "/q64")]["result"] == "license"                    # not reached: as last tried
    hosts = {h["machine"]: h for h in T.listing(conn)["hosts"]}
    assert hosts["erica"]["reached"] == 0 and hosts["erica"]["reached_at"] == "2026-11-20T08:00:00"
    # an install no longer there goes; a machine no longer listed goes; some tried by hand: the rest stay
    T.store(conn, _results({"kevin": [("/q64", "runs", "")]}), progs, datetime(2026, 11, 21, 8), keep_others=True)
    assert {(r["machine"], r["install"]) for r in T.listing(conn)["tries"]} == {("kevin", "/q64"), ("erica", "/q64")}
    T.store(conn, _results({"kevin": [("/q64", "runs", "")]}), progs, datetime(2026, 11, 21, 9))
    assert {r["machine"] for r in T.listing(conn)["tries"]} == {"kevin"}


def _hub(rows):
    conn = sqlite3.connect(":memory:")
    T.ensure(conn)
    conn.execute(f"ALTER TABLE {T.TABLE} ADD COLUMN source_site TEXT")
    conn.execute(f"ALTER TABLE {T.HOSTS} ADD COLUMN source_site TEXT")
    for m, inst, res, why, since in rows:
        conn.execute(f"INSERT INTO {T.TABLE} (machine, program, install, version, result, reason, seconds, tried_at, "
                     "since, source_site) VALUES (?, 'Q-Chem', ?, ?, ?, ?, 1, ?, ?, 'labhead1')",
                     (m, inst, T.version_of(inst), res, why, since, since))
    return conn


def test_listing_summary_and_a_labs_machines():
    conn = _hub([("kevin.example.edu", "/q64", "runs", "", "2026-11-20T08:00:00"),
                 ("erica", "/q64", "license", "this machine isn't in the license", "2026-11-20T08:00:00"),
                 ("kevin.example.edu", "/q70", "license", "the license expired on 12 Sep 2026", "2026-11-20T08:00:00")])
    groups = T.summary(T.listing(conn))
    assert [g["label"] for g in groups] == ["Q-Chem 7.0", "Q-Chem 6.4"]
    assert groups[1]["runs"] == ["kevin.example.edu"] and groups[1]["not"][0][:2] == ("erica", "license")
    assert {r["machine"] for r in T.listing(conn, machines={"kevin"})["tries"]} == {"kevin.example.edu"}
    assert T.listing(conn, site="elsewhere")["tries"] == []


def test_told_once_each_change(tmp_path):
    conn = _hub([("erica", "/q64", "license", "this machine isn't in the license", "2026-11-20T08:00:00"),
                 ("kevin", "/q64", "runs", "", "2026-11-20T08:00:00")])
    s = L.settings({"licenses": {"to": ["hpc@example.edu"]}, "mail": {"from": "nomad@example.edu"}})
    rec = L.open_record(tmp_path / "licenses.db", write=True)
    sent = []
    out = L.remind(conn, s, rec, datetime(2026, 11, 21, 8, 20), dry=False, sender=lambda m, c: sent.append(m))
    assert out["status"] == "sent" and out["subject"] == "NOMAD: Q-Chem doesn't run on some machines"
    assert "Q-Chem 6.4 (labhead1) doesn't run on 1 machine: erica. Why: this machine isn't in the license." \
        in out["text"] and "kevin" not in out["text"]
    assert L.remind(conn, s, rec, datetime(2026, 11, 21, 20), dry=False, sender=lambda m, c: sent.append(m))["status"] \
        == "nothing due"
    conn.execute(f"UPDATE {T.TABLE} SET result = 'runs', reason = '', since = '2026-11-22T08:00:00' WHERE machine = 'erica'")
    out = L.remind(conn, s, rec, datetime(2026, 11, 22, 8, 20), dry=False, sender=lambda m, c: sent.append(m))
    assert out["subject"] == "NOMAD: licensed programs run again on some machines" and "runs again on erica" in out["text"]
    assert L.remind(conn, s, rec, datetime(2026, 11, 23), dry=True)["status"] == "nothing due"
    conn.execute(f"UPDATE {T.TABLE} SET result = 'license', since = '2026-11-24T08:00:00' WHERE machine = 'erica'")
    assert L.remind(conn, s, rec, datetime(2026, 11, 24), dry=True)["status"] == "would send"
    assert len(sent) == 2


def test_with_a_license_ending_one_email(tmp_path):
    notes = [L.Note({"site": "hpc1", "name": "Gurobi", "ends": "2026-12-01", "never": False, "detail": "",
                     "kind": "gurobi", "problem": None, "problem_since": None, "about": "", "read_at": None},
                    "ends", "ends:2026-12-01", ("14",), 11)]
    tries = [{"row": {"site": "labhead1", "machine": "erica", "program": "Q-Chem", "install": "/q64",
                      "version": "6.4", "result": "license", "reason": "this machine isn't in the license"},
              "why": "notrun", "what": "license:x"}]
    subject, text = L.letter(notes, L.settings({}), datetime(2026, 11, 20).date(), tries)
    assert subject == "NOMAD: Gurobi (hpc1) ends in 11 days; Q-Chem doesn't run on some machines"
    assert text.index("Gurobi (hpc1)") < text.index("Licensed programs on the machines")


def test_the_commands(lab, tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    s, root = lab
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[database]\npath = "{tmp_path / "site.db"}"\n\n[collectors.workstation]\nenabled = false\n'
                    '[[collectors.workstation.workstations]]\nhostname = "localhost"\n')
    run = CliRunner().invoke
    r = run(cli, ["-c", str(toml), "licenses", "tries", "setup", "Q-Chem", "qchem", "--min-version", "6"])
    assert r.exit_code == 0 and "Would add" in r.output and "license_tries" not in toml.read_text()
    r = run(cli, ["-c", str(toml), "licenses", "tries", "setup", "Q-Chem", "qchem", "--min-version", "6", "--apply"])
    assert r.exit_code == 0 and "Added Q-Chem" in r.output
    text = toml.read_text()
    assert "[collectors.license_tries]\nenabled = true" in text and 'min_version = "6"' in text
    r = run(cli, ["-c", str(toml), "licenses", "tries", "setup", "Q-Chem", "qchem", "--min-version", "6", "--apply"])
    assert "Nothing to change" in r.output and toml.read_text() == text
    r = run(cli, ["-c", str(toml), "licenses", "tries", "setup", "Q-Chem", "qchem", "--min-version", "7", "--apply"])
    assert r.exit_code != 0 and "--replace" in r.output
    # the roots of the stand-in installs, and the account to run as
    toml.write_text(toml.read_text().replace('min_version = "6"', f'min_version = "6"\nroots = ["{root}"]')
                    .replace("enabled = true", f'enabled = true\nrun_as = "{s.run_as}"\ntimeout = 20', 1))
    r = run(cli, ["-c", str(toml), "licenses", "tries", "now"])
    assert r.exit_code == 0, r.output
    assert "Q-Chem 6.4" in r.output and "runs on 1 of 1" in r.output
    assert "doesn't run on 1: localhost: the license expired on 12 Sep 2026" in r.output
    r = run(cli, ["-c", str(toml), "licenses", "tries"])
    assert r.exit_code == 0 and "Tried last:" in r.output and KEY not in r.output
    r = run(cli, ["-c", str(toml), "licenses", "tries", "now", "--machine", "elsewhere"])
    assert r.exit_code != 0 and "not among this site's machines" in r.output


def test_the_collector_twice_a_day(lab, tmp_path):
    from nomad.collectors.license_tries import LicenseTriesCollector
    from nomad.collectors.plan import plan
    s, root = lab
    cfg = {"collectors": {"workstation": {"workstations": [{"hostname": "localhost"}]},
                          "license_tries": {"enabled": True, "run_as": s.run_as, "timeout": 20,
                                            "programs": [{"name": "Q-Chem", "recipe": "qchem", "min_version": "6",
                                                          "roots": [str(root)]}]}}}
    p = [x for x in plan(cfg) if x.name == "license_tries"][0]
    assert p.enabled and p.config["workstation_hosts"] == ["localhost"]
    db = tmp_path / "site.db"
    c = LicenseTriesCollector(p.config, db)
    data = c.collect()
    c.store(data)
    assert c.note == "1 of 1 machine(s) answered"
    assert len(T.listing(sqlite3.connect(db))["tries"]) == 4
    c2 = LicenseTriesCollector(p.config, db)
    assert c2.collect() == [] and "less than 11 h ago" in c2.note
    # one round at a time: a run while another goes says so and tries nothing
    fresh = tmp_path / "fresh.db"
    with T.one_round(fresh):
        c3 = LicenseTriesCollector(p.config, fresh)
        assert c3.collect() == [] and c3.note == "another round of tries is going on"
        with pytest.raises(T.Busy):
            with T.one_round(fresh):
                pass
    with T.one_round(fresh):        # free again
        pass
