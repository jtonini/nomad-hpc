"""1.17.8: a group report counts the group's members now, as Labs does."""
import sqlite3
from datetime import datetime, timedelta

from nomad.edu.population import group_members


def test_a_group_report_counts_its_members_now(tmp_path):
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT, "
              "collected_at TEXT, has_account INTEGER)")
    now = datetime.now()
    old = (now - timedelta(days=200)).isoformat()
    c.executemany("INSERT INTO group_membership VALUES (?, ?, 'hpc1', ?, ?)", [
        ("alice", "lab$", now.isoformat(), 1),
        ("bob", "lab$", now.isoformat(), 1),
        ("gone", "lab$", old, 1),               # the group stopped listing them long ago
        ("noacct", "lab$", now.isoformat(), 0),  # listed, with no account
        ("gone", "old$", old, 1),                # a group with no one in it now
    ])
    c.commit()
    c.close()
    assert group_members(str(db)) == {"lab$": {"alice", "bob"}}


def test_no_membership_table_is_no_groups(tmp_path):
    db = tmp_path / "n.db"
    sqlite3.connect(db).close()
    assert group_members(str(db)) == {}


def test_a_report_can_use_the_scoring_already_done(tmp_path, monkeypatch):
    from nomad.edu import population as P
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    c.execute("INSERT INTO group_membership VALUES ('alice', 'lab$')")
    c.commit()
    c.close()
    pop = P.Population(days=30, since="a", until="b", people={}, sites=[])
    monkeypatch.setattr(P, "population", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scored again")))
    gr = P.group_report(str(db), "lab$", 30, members={"alice"}, pop=pop)
    assert gr.members == 1 and gr.without_jobs == ["alice"]
    assert P.group_known(str(db), "lab$") and not P.group_known(str(db), "nosuch$")


def test_edu_report_says_a_group_has_no_members_now(tmp_path):
    from click.testing import CliRunner
    from nomad.cli import cli
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT, "
              "collected_at TEXT, has_account INTEGER)")
    now = datetime.now()
    c.execute("INSERT INTO group_membership VALUES ('gone', 'old$', 'hpc1', ?, 1)",
              ((now - timedelta(days=200)).isoformat(),))
    c.execute("INSERT INTO group_membership VALUES ('alice', 'lab$', 'hpc1', ?, 1)", (now.isoformat(),))
    c.commit()
    c.close()
    r = CliRunner().invoke(cli, ["edu", "report", "old$", "--db", str(db)])
    assert r.exit_code == 1 and "has no members now" in r.output
    r = CliRunner().invoke(cli, ["edu", "report", "nosuch$", "--db", str(db)])
    assert "No data found" in r.output
