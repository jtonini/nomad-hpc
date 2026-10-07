# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A lab's members are the people its group lists now and who still have an
account. A hand-kept /etc/group went on listing people whose accounts were
deleted years ago (38 of 798 at one site, 6 of one PI's 138), and a person
taken out of a group kept their old row: both counted as members, and a PI
could see them."""
import sqlite3
import subprocess
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.collectors.groups import GroupCollector
from nomad.config.access import access_from, members_lookup, membership_counts

GROUPS = "pi1$:x:5001:ann,bob,ghost\nother:x:5002:ann\nwheel:x:10:ann"
PASSWD = "ann:x:1001:1001::/home/ann:/bin/bash\nbob:x:1002:1002::/home/bob:/bin/bash"


def fake(passwd=PASSWD, calls=None):
    def run_cmd(cmd, host=None, ssh_user=None, ssh_key=None, ok_codes=(0,), **k):
        if calls is not None:
            calls.append((cmd, host, ok_codes))
        if cmd == "getent group":
            return GROUPS
        if isinstance(cmd, list) and cmd[:3] == ["getent", "passwd", "--"]:
            return passwd
        return None
    return run_cmd


# --- the collector ---------------------------------------------------------------

def test_each_member_is_looked_up_where_the_group_lives(tmp_path):
    c = GroupCollector({"clusters": {}, "local_name": "site1"}, tmp_path / "n.db")
    calls = []
    c._run_cmd = fake(calls=calls)
    rows = {(r["username"], r["group_name"]): r["has_account"] for r in c._collect_groups(
        "head1", None, None, "site1")}
    assert rows == {("ann", "pi1$"): True, ("bob", "pi1$"): True, ("ghost", "pi1$"): False,
                    ("ann", "other"): True}
    lookup = [c for c in calls if isinstance(c[0], list)]
    assert lookup == [(["getent", "passwd", "--", "ann", "bob", "ghost"], "head1", (0, 2))]


def test_a_lookup_that_fails_is_not_known_not_gone(tmp_path):
    c = GroupCollector({"clusters": {}}, tmp_path / "n.db")
    c._run_cmd = fake(passwd=None)
    assert {r["has_account"] for r in c._collect_groups()} == {None}


def test_one_failed_batch_makes_the_whole_answer_unknown(tmp_path):
    c = GroupCollector({"clusters": {}}, tmp_path / "n.db")
    c.ACCOUNT_BATCH = 2
    seen = []

    def run_cmd(cmd, *a, **k):
        if cmd == "getent group":
            return GROUPS
        seen.append(cmd[3:])
        return PASSWD if len(seen) == 1 else None
    c._run_cmd = run_cmd
    assert {r["has_account"] for r in c._collect_groups()} == {None}
    assert seen == [["ann", "bob"], ["ghost"]]


def test_names_go_to_ssh_quoted_and_here_as_they_are(tmp_path, monkeypatch):
    c = GroupCollector({"clusters": {}}, tmp_path / "n.db")
    argvs = []

    def run(argv, **k):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 2, stdout="ann:x:1:1::/:/bin/sh\n", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    assert c._run_cmd(["getent", "passwd", "--", "ann", "o'neil;rm"], ok_codes=(0, 2)) \
        == "ann:x:1:1::/:/bin/sh"
    assert argvs[-1] == ["getent", "passwd", "--", "ann", "o'neil;rm"]
    c._run_cmd(["getent", "passwd", "--", "o'neil;rm"], "head1", ok_codes=(0, 2))
    assert argvs[-1][-1] == "getent passwd -- 'o'\"'\"'neil;rm'"
    assert c._run_cmd(["getent", "passwd", "--", "x"]) is None        # 2 not accepted by default


def test_stored_with_the_column_added_to_an_older_table(tmp_path):
    db = tmp_path / "n.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE group_membership (username TEXT NOT NULL, group_name TEXT "
                 "NOT NULL, gid INTEGER, cluster TEXT NOT NULL, collected_at TIMESTAMP "
                 "DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (username, group_name, cluster))")
    conn.execute("INSERT INTO group_membership VALUES ('old', 'pi1$', 5001, 'site1', "
                 "'2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    c = GroupCollector({"clusters": {}, "local_name": "site1"}, db)
    c._run_cmd = fake()
    t0 = datetime(2026, 10, 7, 15, 0)

    def got():
        conn = sqlite3.connect(db)
        out = dict(conn.execute("SELECT username, has_account FROM group_membership "
                                "WHERE group_name = 'pi1$'").fetchall())
        conn.close()
        return out
    c._now = lambda: t0
    c.store(c.collect())
    # Not found, but only just: a directory hiccup takes no one out.
    assert got() == {"ann": 1, "bob": 1, "ghost": None, "old": None}
    c._now = lambda: t0 + timedelta(days=2, minutes=1)
    c.store(c.collect())
    assert got() == {"ann": 1, "bob": 1, "ghost": 0, "old": None}


def account_runs(tmp_path, runs):
    """Store one run per (hours after the first, passwd text or None)."""
    db = tmp_path / "n.db"
    c = GroupCollector({"clusters": {}, "local_name": "site1"}, db)
    t0 = datetime(2026, 10, 7, 15, 0)
    out = []
    for hours, passwd in runs:
        c._run_cmd = fake(passwd=passwd)
        c._now = lambda h=hours: t0 + timedelta(hours=h)
        c.store(c.collect())
        conn = sqlite3.connect(db)
        out.append(conn.execute("SELECT has_account FROM group_membership WHERE username = "
                                "'ghost'").fetchone()[0])
        conn.close()
    return out


def test_no_account_takes_two_days_of_runs(tmp_path):
    """Every hour for a day finds none (an outage looks like that): still counted."""
    runs = [(h, PASSWD) for h in range(0, 25)] + [(47, PASSWD), (48, PASSWD), (50, None)]
    got = account_runs(tmp_path, runs)
    assert set(got[:26]) == {None} and got[-3:] == [None, 0, 0]


def test_found_again_counts_again_and_starts_over(tmp_path):
    back = PASSWD + "\nghost:x:1003:1003::/home/ghost:/bin/bash"
    assert account_runs(tmp_path, [(0, PASSWD), (49, PASSWD), (50, back), (51, PASSWD),
                                   (60, PASSWD)]) == [None, 0, 1, 1, 1]


def test_many_unresolved_at_once_is_said(tmp_path, caplog):
    """getent says "not found" whether the user is gone or sssd is down: said
    in the log, and the two days' grace does the rest."""
    c = GroupCollector({"clusters": {}}, tmp_path / "n.db")
    many = [f"user{i}" for i in range(20)]
    c._run_cmd = lambda cmd, *a, **k: "user0:x:1:1::/:/bin/sh"
    with caplog.at_level("WARNING"):
        assert c._accounts(set(many), where="site1") == {"user0"}
    assert "check the directory" in caplog.text


def test_case_and_digits(tmp_path):
    c = GroupCollector({"clusters": {}}, tmp_path / "n.db")

    def run_cmd(cmd, *a, **k):
        if cmd == "getent group":
            return "pi1$:x:5001:JSmith,12345"
        return "jsmith:x:1001:1001::/home/jsmith:/bin/bash"
    c._run_cmd = run_cmd
    got = {r["username"]: r["has_account"] for r in c._collect_groups()}
    assert got == {"JSmith": True, "12345": None}


# --- who counts --------------------------------------------------------------------

LATEST = datetime(2026, 10, 7, 15, 20)


def build(path, rows, site_column=True):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, "
                 "cluster TEXT, collected_at TIMESTAMP, has_account INTEGER"
                 + (", source_site TEXT)" if site_column else ")"))
    for user, group, when, account, site in rows:
        conn.execute("INSERT INTO group_membership VALUES (?, ?, 5001, ?, ?, ?"
                     + (", ?)" if site_column else ")"),
                     (user, group, site, when.isoformat(), account)
                     + ((site,) if site_column else ()))
    conn.commit()
    return conn


def test_members_are_listed_now_and_have_an_account(tmp_path):
    conn = build(tmp_path / "c.db", [
        ("ann", "pi1$", LATEST, 1, "s1"),
        ("nolookup", "pi1$", LATEST, None, "s1"),              # not known: counted
        ("ghost", "pi1$", LATEST, 0, "s1"),                    # no account
        ("left", "pi1$", LATEST - timedelta(days=5), 1, "s1"),   # no longer listed
        ("grace", "pi1$", LATEST - timedelta(days=1), 1, "s1"),  # a missed run or two
        ("x", "pi1$", LATEST - timedelta(days=40), 1, "s2"),    # s2 stopped: frozen, kept
        ("y", "other$", LATEST - timedelta(days=40), 1, "s2"),   # same, last run there
    ])
    exists, members_of = members_lookup(conn)
    assert members_of("pi1$") == {"ann", "nolookup", "grace", "x"}
    assert membership_counts(conn, "pi1$") == {"members": 4, "former": 1, "no_account": 1}


def test_a_cluster_that_does_not_answer_freezes_and_a_retired_one_stops(tmp_path):
    """One site collecting two clusters: one head node down for 10 days keeps
    its lab as it was; a cluster unseen for 40 days while the site goes on
    is retired (renamed, or the old default name "local")."""
    def rows(days):
        return [("ann", "pi1$", LATEST, 1, "s1"),
                ("bob", "pi2$", LATEST - timedelta(days=days), 1, "s1")]
    conn = build(tmp_path / "a.db", rows(10))
    conn.execute("UPDATE group_membership SET cluster = 'other' WHERE username = 'bob'")
    assert members_lookup(conn)[1]("pi2$") == {"bob"}
    conn = build(tmp_path / "b.db", rows(40))
    conn.execute("UPDATE group_membership SET cluster = 'other' WHERE username = 'bob'")
    assert members_lookup(conn)[1]("pi2$") == set()


def test_rows_under_a_cluster_name_no_longer_collected_stop_counting(tmp_path):
    """A cluster renamed (or the old default name "local") left rows whose own
    latest run never moved."""
    conn = build(tmp_path / "c.db", [("ann", "pi1$", LATEST, 1, "s1"),
                                     ("left", "pi1$", LATEST - timedelta(days=150), None, "s1")])
    conn.execute("UPDATE group_membership SET cluster = 'local' WHERE username = 'left'")
    assert members_lookup(conn)[1]("pi1$") == {"ann"}


def test_listed_with_an_account_anywhere_counts(tmp_path):
    conn = build(tmp_path / "c.db", [
        ("ann", "pi1$", LATEST, 0, "s1"),
        ("ann", "pi1$", LATEST, 1, "s2"),
        ("bob", "pi1$", LATEST, 0, "s1"),
    ])
    assert members_lookup(conn)[1]("pi1$") == {"ann"}
    assert membership_counts(conn, "pi1$") == {"members": 1, "former": 0, "no_account": 1}


def test_a_group_of_only_departed_people_is_no_lab(tmp_path):
    conn = build(tmp_path / "c.db", [("ghost", "pi1$", LATEST, 0, "s1"),
                                     ("ann", "pi2$", LATEST, 1, "s1")])
    exists, _ = members_lookup(conn)
    assert not exists("pi1$") and exists("pi2$")
    acc = access_from({"console": {"labs": {"group_pattern": "{netid}$"}}}, log=False)
    assert acc.lab_groups("pi1", exists) == [] and acc.lab_groups("pi2", exists) == ["pi2$"]


def test_older_databases_count_everyone_as_before(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    conn.executemany("INSERT INTO group_membership VALUES (?, ?)",
                     [("ann", "pi1$"), ("bob", "pi1$")])
    assert members_lookup(conn)[1]("pi1$") == {"ann", "bob"}
    site = build(tmp_path / "site.db", [("ann", "pi1$", LATEST, None, "s1")], site_column=False)
    assert members_lookup(site)[1]("pi1$") == {"ann"}
    empty = sqlite3.connect(tmp_path / "none.db")
    assert members_lookup(empty)[1]("pi1$") == set()


def test_console_roles_says_who_is_not_counted(tmp_path, monkeypatch):
    from nomad.cli import cli
    db = tmp_path / "c.db"
    build(db, [("pi1", "pi1$", LATEST, 1, "s1"), ("ann", "pi1$", LATEST, 1, "s1"),
               ("ghost", "pi1$", LATEST, 0, "s1"), ("gone", "pi1$", LATEST - timedelta(days=9), 1,
                                                    "s1")]).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    out = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "pi1", "--db", str(db)]).output
    assert "pi1$: 2 members (1 without an account, 1 no longer listed: not counted)" in out
    assert "their own work and 1 lab members" in out
