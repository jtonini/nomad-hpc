# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.59.2: license tries on a cluster's nodes (machines = "slurm": every node
Slurm knows, asked at each round; skip; ssh as the account nomad runs as), a
folder holding Q-Chem's versions isn't tried as an install, the email says
"machines", and licenses add/renew --apply keep the reading at once."""
from __future__ import annotations

import getpass
import os
import sqlite3
import subprocess
from pathlib import Path

from click.testing import CliRunner

from nomad import license_tries as T
from nomad import licenses as L


def _install(root: Path, name: str, refused: bool = False) -> Path:
    q = root / name
    (q / "qcaux" / "license").mkdir(parents=True)
    (q / "qcaux" / "license" / "qchem.license.dat").write_text("an encrypted license\n")
    (q / "bin").mkdir()
    (q / "qcenv.sh").write_text(f'export QC={q}\nexport PATH="$QC/bin:$PATH"\n')
    body = ('echo " failed standard license check on $(hostname -s) SID 4F43DE1058"\nexit 1\n' if refused else
            'echo "  Thank you very much for using Q-Chem.  Have a nice day." > "$2"\n')
    (q / "bin" / "qchem").write_text("#!/bin/bash\n" + body)
    for p in (q, *q.rglob("*")):
        os.chmod(p, 0o755 if p.is_dir() or p.name == "qchem" else 0o644)
    return q


def _sinfo(out: str, rc: int = 0):
    def run(argv, **kw):
        assert argv[:2] == ["sinfo", "-h"]
        return subprocess.CompletedProcess(argv, rc, out, "")
    return run


def test_machines_slurm_settings():
    s = T.settings({"machines": "slurm", "skip": ["n3"], "programs": [{"name": "Q-Chem", "recipe": "qchem"}]})
    assert s.slurm and s.machines == [] and s.skip == ["n3"] and not s.problems
    assert s.ssh_user == getpass.getuser()            # the account nomad runs as, not root
    s = T.settings({"machines": "SLURM", "ssh_user": "installer"})
    assert s.slurm and s.ssh_user == "installer"
    s = T.settings({"workstation_hosts": ["ws1", "ws2", "ws3"], "skip": ["ws2"]})
    assert not s.slurm and s.machines == ["ws1", "ws3"] and s.ssh_user == "root"
    bad = T.settings({"machines": "all", "skip": "n1"})
    assert len(bad.problems) == 2


def test_resolve_asks_slurm_each_time():
    s = T.settings({"machines": "slurm", "skip": ["n3"]})
    assert T.resolve(s, _sinfo("n1\nn2\nn2\nn3\nbad name!\n")) is None
    assert s.machines == ["n1", "n2"]
    assert T.resolve(s, _sinfo("n1\nn2\nn4\n")) is None and s.machines == ["n1", "n2", "n4"]
    note = T.resolve(s, _sinfo("", rc=1))
    assert "gave no nodes" in note and s.machines == []
    plain = T.settings({"workstation_hosts": ["ws1"]})
    assert T.resolve(plain, _sinfo("n1\n")) is None and plain.machines == ["ws1"]


def test_a_folder_of_versions_is_not_an_install(tmp_path):
    sw = tmp_path / "sw"
    top = sw / "qchem"
    (top / "qcaux" / "license").mkdir(parents=True)
    (top / "qcaux" / "license" / "qchem.license.dat").write_text("an encrypted license\n")
    _install(top, "qchem611")
    _install(top, "qchem541", refused=True)
    for p in (tmp_path, sw, *sw.rglob("*")):
        if p.is_dir():
            os.chmod(p, 0o755)
    me = "root" if os.geteuid() == 0 else os.environ.get("USER", "nobody")
    s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem", "roots": [str(sw)]}],
                    "workstation_hosts": ["localhost"], "run_as": me, "timeout": 20})
    res = T.try_machine("localhost", s)
    got = sorted((Path(r["install"]).name, r["result"]) for r in res["rows"])
    assert got == [("qchem541", "license"), ("qchem611", "runs")]      # not the qchem folder itself


def test_the_collector_tries_the_nodes(tmp_path, monkeypatch):
    from nomad.collectors.license_tries import LicenseTriesCollector
    tried = []

    def fake_try_all(s, run=None):
        tried.append(list(s.machines))
        return [{"machine": m, "reached": True, "note": "", "rows": [
            {"program": "Q-Chem", "recipe": "qchem", "install": "/sw/qchem611", "version": "6.1.1",
             "result": "runs" if m != "n2" else "license", "reason": "" if m != "n2" else
             "this machine isn't in the license", "seconds": 2}]} for m in s.machines]
    monkeypatch.setattr(T, "try_all", fake_try_all)
    monkeypatch.setattr(T, "slurm_nodes", lambda run=None: ["n1", "n2", "n3"])
    db = tmp_path / "nomad.db"
    c = LicenseTriesCollector({"machines": "slurm", "skip": ["n3"], "min_hours": 0,
                               "programs": [{"name": "Q-Chem", "recipe": "qchem"}]}, db)
    c.collect()
    assert tried == [["n1", "n2"]]
    data = T.listing(sqlite3.connect(db))
    assert sorted((r["machine"], r["result"]) for r in data["tries"]) == [("n1", "runs"), ("n2", "license")]
    # Slurm silent: nothing tried, nothing kept is dropped
    monkeypatch.setattr(T, "slurm_nodes", lambda run=None: [])
    c.collect()
    assert "gave no nodes" in c.note and len(tried) == 1
    assert len(T.listing(sqlite3.connect(db))["tries"]) == 2


def test_the_email_says_machines():
    notes = [{"row": {"site": "spydur", "program": "Q-Chem", "version": "6.1.1", "install": "/sw/qchem611",
                      "machine": "n2", "result": "license", "reason": "this machine isn't in the license"},
              "why": "notrun", "what": "x"}]
    subj, lines = T.section(notes)
    assert subj == "Q-Chem doesn't run on some machines"
    assert lines[0].startswith("Licensed programs on the machines nomad tries them on")
    assert "Q-Chem 6.1.1 (spydur) doesn't run on 1 machine: n2" in lines[2]


def test_tries_setup_nodes(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n\n[collectors.license_tries]\nmachines = ["ws1"]\n')
    run = CliRunner()
    args = ["-c", str(cfg), "licenses", "tries", "setup", "Q-Chem", "qchem", "--min-version", "6.4", "--nodes"]
    out = run.invoke(cli, args)
    assert out.exit_code == 0, out.output
    assert 'machines = "slurm"' in out.output and "slurm" not in cfg.read_text()
    out = run.invoke(cli, args + ["--apply"])
    assert out.exit_code == 0, out.output
    from nomad.config.edit import loads
    sec = loads(cfg.read_text())["collectors"]["license_tries"]
    assert sec["machines"] == "slurm" and sec["enabled"] is True
    assert sec["programs"] == [{"name": "Q-Chem", "recipe": "qchem", "min_version": "6.4"}]
    assert "node(s) (Slurm's" in out.output
    again = run.invoke(cli, args + ["--apply"])
    assert again.exit_code == 0 and "Nothing to change" in again.output
    # --nodes on a program listed already, the same: no --replace needed
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n\n[collectors.license_tries]\nenabled = true\n\n'
                   '[[collectors.license_tries.programs]]\nname = "Q-Chem"\nrecipe = "qchem"\nmin_version = "6.4"\n')
    out = run.invoke(cli, args + ["--apply"])
    assert out.exit_code == 0, out.output
    assert loads(cfg.read_text())["collectors"]["license_tries"]["machines"] == "slurm"


def test_add_and_renew_keep_the_reading_at_once(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n')
    run = CliRunner()
    out = run.invoke(cli, ["-c", str(cfg), "licenses", "add", "Chem suite", "date", "--ends", "2099-06-30", "--apply"])
    assert out.exit_code == 0, out.output
    assert "Read now and kept: ends" in out.output and "after its next sync" in out.output
    db = next(tmp_path.rglob("*.db"))
    rows = L.listing(sqlite3.connect(db))
    assert [(r["name"], r["ends"]) for r in rows] == [("Chem suite", "2099-06-30")]
    out = run.invoke(cli, ["-c", str(cfg), "licenses", "renew", "chem suite", "no", "--apply"])
    assert out.exit_code == 0, out.output
    assert "not being renewed" in out.output
    rows = L.listing(sqlite3.connect(db))
    assert len(rows) == 1 and not rows[0]["renew"]


def test_nothing_to_try_is_said(capsys):
    from nomad.licenses_cli import _print_tries
    _print_tries({"tries": [], "hosts": [{"machine": "n1", "reached": 1, "tried_at": "2026-11-21T05:00:00"},
                                         {"machine": "n2", "reached": 0, "tried_at": "2026-11-21T05:00:00"}]},
                 hub=False)
    out = capsys.readouterr().out
    assert "No install of the programs listed" in out and "on the 1 machine(s) reached" in out
    assert "Not reached at the last try: n2" in out
