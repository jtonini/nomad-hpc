# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.59.1: min_version is the whole version ("6.4" leaves out 6.1, 6.1.1 and
6.3; 1.59.0 read only the 6), and the license email's lines name the install."""
from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from click.testing import CliRunner

from nomad import license_tries as T


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


def test_min_version_is_the_whole_version():
    got = {}
    for value in ("6.4", 6.4, 6, "06.4", " 7 ", "", None, "six", True, "6.4beta"):
        s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem", "min_version": value}]})
        got[repr(value)] = (s.programs[0].min_version, bool(s.problems))
    assert got == {"'6.4'": ("6.4", False), "6.4": ("6.4", False), "6": ("6", False), "'06.4'": ("6.4", False),
                   "' 7 '": ("7", False), "''": ("0", False), "None": ("0", False), "'six'": ("0", True),
                   "True": ("0", True), "'6.4beta'": ("0", True)}


def test_older_in_the_script():
    head = T._QCHEM.split("try_qchem() {")[0]
    cases = [("64", "6.4", 1), ("65", "6.4", 1), ("70", "6.4", 1), ("7", "6.4", 1), ("63", "6.4", 0),
             ("611", "6.4", 0), ("61", "6.4", 0), ("6", "6.4", 0), ("521", "6", 0), ("61", "6", 1),
             ("64", "0", 1), ("4", "0", 1), ("611", "6.1.1", 1), ("61", "6.1.1", 0)]
    body = head + "".join(f'older {d} {m} && echo "{d} {m} 0" || echo "{d} {m} 1"\n' for d, m, _ in cases)
    out = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=True).stdout.split("\n")
    assert out[:len(cases)] == [f"{d} {m} {kept}" for d, m, kept in cases]


def test_left_out_installs_and_their_rows(tmp_path):
    root = tmp_path / "sw"
    chem = root / "chem.sw"
    chem.mkdir(parents=True)
    for p in (tmp_path, root, chem):
        os.chmod(p, 0o755)
    _install(chem, "qchem64")
    _install(chem, "qchem70")
    _install(chem, "qchem611", refused=True)
    _install(chem, "qchem61", refused=True)
    (chem / "qchem").mkdir()
    os.chmod(chem / "qchem", 0o755)
    _install(chem / "qchem", "qchem63", refused=True)
    me = "root" if os.geteuid() == 0 else os.environ.get("USER", "nobody")

    def tried(min_version):
        s = T.settings({"programs": [{"name": "Q-Chem", "recipe": "qchem", "min_version": min_version,
                                      "roots": [str(root)]}], "workstation_hosts": ["localhost"],
                        "run_as": me, "timeout": 20})
        res = T.try_machine("localhost", s)
        assert res["reached"], res
        return s, res

    s6, res6 = tried("6")
    assert sorted(T.version_of(r["install"]) for r in res6["rows"]) == ["6.1", "6.1.1", "6.3", "6.4", "7.0"]
    s64, res64 = tried("6.4")
    assert sorted((T.version_of(r["install"]), r["result"]) for r in res64["rows"]) == [("6.4", "runs"),
                                                                                       ("7.0", "runs")]
    # tried with 6 first, then 6.4: the left-out installs' rows go at the next try
    conn = sqlite3.connect(":memory:")
    for res, s, when in ((res6, s6, datetime(2026, 11, 20, 13)), (res64, s64, datetime(2026, 11, 21, 1))):
        res = dict(res, machine="ws1")
        T.store(conn, [res], s.programs, when)
    assert sorted(r["version"] for r in T.listing(conn)["tries"]) == ["6.4", "7.0"]


def test_remind_names_the_install(tmp_path):
    from nomad.cli import cli
    conn = sqlite3.connect(tmp_path / "combined.db")
    T.ensure(conn)
    for table in (T.TABLE, T.HOSTS):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN source_site TEXT")
    for inst in ("/opt/qchem70", "/opt/qchem70.cpu-old", "/sw/chem.sw/qchem70"):
        conn.execute(f"INSERT INTO {T.TABLE} (machine, program, install, version, result, reason, seconds, tried_at, "
                     "since, source_site) VALUES ('ws1', 'Q-Chem', ?, '7.0', 'license', "
                     "'the license expired on 12 Sep 2026', 1, '2026-11-20T13:33:00', '2026-11-20T13:33:00', 'labsite')",
                     (inst,))
    conn.commit()
    conn.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "{tmp_path / "combined.db"}"\n[[hub.sites]]\nname = "labsite"\n'
                   'host = "labsite"\n[support]\nemail = "rc@example.edu"\n')
    out = CliRunner().invoke(cli, ["-c", str(cfg), "licenses", "remind", "--text"])
    assert out.exit_code == 0, out.output
    for inst in ("/opt/qchem70", "/opt/qchem70.cpu-old", "/sw/chem.sw/qchem70"):
        assert (f"labsite: Q-Chem 7.0 on ws1 ({inst}): doesn't run: the license expired on 12 Sep 2026"
                in out.output), out.output
