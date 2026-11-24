# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.60.0: My files open to some labs' people ([console.files] open = "labs"):
their members now and those their PI said are in the lab, less those said to
have left, and (active_days) only those nomad saw at work lately; `nomad
files status` and `nomad files open`."""
from __future__ import annotations

import sqlite3
from datetime import date

from click.testing import CliRunner

from nomad import lab_check as L
from nomad.submit import files as F

TODAY = date(2026, 11, 21)


def _combined(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT, cluster TEXT,
                                       collected_at TEXT, has_account INTEGER, first_seen TEXT);
        CREATE TABLE jobs (user_name TEXT, submit_time TEXT, source_site TEXT);
        CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, username TEXT, uid INTEGER);
        CREATE TABLE per_user_daily (day TEXT, hostname TEXT, username TEXT);
    """)
    for user in ("kim", "pat", "ann", "lee"):
        db.execute("INSERT INTO group_membership VALUES (?, 'kim$', 'hpc1', 'hpc1', '2026-11-20 10:00:00', 1, "
                   "'2024-01-01')", (user,))
    db.execute("INSERT INTO group_membership VALUES ('zed', 'pat$', 'hpc1', 'hpc1', '2026-11-20 10:00:00', 1, "
               "'2024-01-01')")
    db.executemany("INSERT INTO jobs VALUES (?, ?, 'hpc1')", [
        ("kim", "2026-10-30T09:00:00"), ("kim", "2025-01-02T09:00:00"),
        ("pat", "2024-06-01T09:00:00"), ("zed", "2026-11-01T09:00:00")])
    db.execute("INSERT INTO workstation_user_snapshot VALUES ('2026-11-14 12:00:00', 'ws1', 'lee', 2001)")
    db.execute("INSERT INTO workstation_user_snapshot VALUES ('not a time', 'ws1', 'ann', 2002)")
    db.execute("INSERT INTO per_user_daily VALUES ('2026-09-01', 'hpc1', 'joe')")
    db.commit()
    return db


def _answers(path, rows):
    rec = sqlite3.connect(path)
    rec.executescript(L.SCHEMA)
    rec.executemany("INSERT INTO lab_check_answers (round, lab, netid, kind, answer, by, at) "
                    "VALUES ('2026-fall', 'kim$', ?, ?, ?, 'kim', '2026-11-03T10:00:00')", rows)
    rec.commit()
    rec.row_factory = sqlite3.Row
    return rec


def test_settings_and_opens_to():
    s = F.settings({"console": {"files": {"open": "labs", "labs": "kim$"}}})
    assert s.open == "labs" and s.labs == ("kim$",) and s.active_days == 365 and not s.problems
    bad = F.settings({"console": {"files": {"open": "labs", "active_days": -1}}})
    assert len(bad.problems) == 2 and bad.active_days == 365 and bad.labs == ()
    assert F.settings({"console": {"files": {"labs": ["kim$", "no good!"]}}}).problems
    assert F.opens_to(s, "admin", "staff") and F.opens_to(s, "user", "staff", in_labs=True)
    assert not F.opens_to(s, "user", "everyone")                 # labs: not as Submit
    assert not F.opens_to(bad, "user", "staff", in_labs=True)    # no labs listed: staff only
    assert F.opens_to(F.settings({}), "user", "everyone") and not F.opens_to(F.settings({}), "user", "staff")


def test_last_active_takes_the_latest_anywhere(tmp_path):
    db = _combined(tmp_path / "combined.db")
    got = F.last_active(db, ["kim", "pat", "ann", "lee", "joe", "nobody"])
    assert got == {"kim": "2026-10-30", "pat": "2024-06-01", "lee": "2026-11-14", "joe": "2026-09-01"}
    assert F.last_active(sqlite3.connect(":memory:"), ["kim"]) == {}      # no tables: nothing seen
    assert F.last_active(db, ["kim", "pat", "lee"], since="2026-11-01") == {"lee": "2026-11-14"}


def test_lab_people(tmp_path):
    db = _combined(tmp_path / "combined.db")
    rec = _answers(tmp_path / "lab_check.db", [("pat", "member", "left"), ("joe", "claim", "mine"),
                                               ("ann", "member", "here")])
    s = F.settings({"console": {"files": {"open": "labs", "labs": ["kim$"]}}})
    got = F.lab_people(db, s, rec, TODAY)
    lab = got["labs"]["kim$"]
    assert got["since"] == "2025-11-21"
    assert lab["members"] == {"kim", "pat", "ann", "lee"} and lab["claimed"] == {"joe"} and lab["left"] == {"pat"}
    assert lab["open"] == {"kim", "lee", "joe"} and lab["quiet"] == {"ann"}      # ann: never seen at work
    assert got["open"] == {"kim", "lee", "joe"}                                   # zed: another lab's
    every = F.lab_people(db, F.settings({"console": {"files": {"open": "labs", "labs": ["kim$"],
                                                                "active_days": 0}}}), rec, TODAY)
    assert every["since"] is None and every["open"] == {"kim", "ann", "lee", "joe"}   # pat said to have left
    none_said = F.lab_people(db, s, None, TODAY)
    assert none_said["open"] == {"kim", "lee"}                                    # pat: last job 2024


def _config(tmp_path):
    db = _combined(tmp_path / "combined.db")
    db.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n\n[hub]\noutput_db = "{tmp_path / "combined.db"}"\n\n'
                   '[[hub.sites]]\nname = "hpc1"\nhost = "hpc1.example.edu"\n\n'
                   '[console.files]\nmax_mb = 1024\n\n[[console.files.machines]]\nname = "ws1"\n'
                   'host = "ws1.example.edu"\n')
    return cfg


def test_files_open_and_status(tmp_path):
    from nomad.cli import cli
    from nomad.config.edit import loads
    cfg = _config(tmp_path)
    run = CliRunner()
    args = ["-c", str(cfg), "files", "open", "labs", "kim$", "--active-days", "365"]
    out = run.invoke(cli, args)
    assert out.exit_code == 0, out.output
    assert "Would set" in out.output and "open = \"labs\"" not in cfg.read_text()
    assert "kim$: 4 in its group now" in out.output and "-> open to 2" in out.output      # kim, lee
    out = run.invoke(cli, args + ["--apply"])
    assert out.exit_code == 0, out.output
    files = loads(cfg.read_text())["console"]["files"]
    assert files["open"] == "labs" and files["labs"] == ["kim$"] and files["active_days"] == 365
    assert files["max_mb"] == 1024 and files["machines"][0]["name"] == "ws1"
    assert list(tmp_path.glob("nomad.toml.bak-files-open-*"))
    again = run.invoke(cli, args + ["--apply"])
    assert again.exit_code == 0 and "Nothing to change" in again.output
    st = run.invoke(cli, ["-c", str(cfg), "files", "status"])
    assert st.exit_code == 0, st.output
    assert "staff, and the people of kim$" in st.output and "In all: 2 people besides staff." in st.output
    assert "kim" not in st.output.split("In all")[0].replace("kim$", "")              # no names without --names
    named = run.invoke(cli, ["-c", str(cfg), "files", "status", "--names"])
    assert "    kim lee" in named.output
    # all of them, once the PIs have said: only active_days changes
    out = run.invoke(cli, ["-c", str(cfg), "files", "open", "labs", "kim$", "--active-days", "0", "--apply"])
    assert out.exit_code == 0, out.output
    assert loads(cfg.read_text())["console"]["files"]["active_days"] == 0 and "-> open to 4" in out.output
    # back to staff: the labs are kept for another time
    out = run.invoke(cli, ["-c", str(cfg), "files", "open", "staff", "--apply"])
    assert out.exit_code == 0, out.output
    files = loads(cfg.read_text())["console"]["files"]
    assert files["open"] == "staff" and files["labs"] == ["kim$"]
    bad = run.invoke(cli, ["-c", str(cfg), "files", "open", "staff", "kim$"])
    assert bad.exit_code != 0 and "go with" in bad.output
    bad = run.invoke(cli, ["-c", str(cfg), "files", "open", "labs"])
    assert bad.exit_code != 0 and "which labs" in bad.output


def test_files_open_makes_the_table(tmp_path):
    from nomad.cli import cli
    from nomad.config.edit import loads
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n\n[[console.files.machines]]\nname = "ws1"\n'
                   'host = "ws1.example.edu"\n')
    out = CliRunner().invoke(cli, ["-c", str(cfg), "files", "open", "labs", "kim$", "--apply"])
    assert out.exit_code == 0, out.output
    files = loads(cfg.read_text())["console"]["files"]
    assert files["open"] == "labs" and files["labs"] == ["kim$"] and "active_days" not in files
    assert files["machines"][0]["name"] == "ws1"
    assert "nomad knows no one in kim$ now" in out.output      # no combined database here
