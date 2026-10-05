"""[console.roles] and [console.labs]: who may see what in the Console."""
import sqlite3

import pytest
from click.testing import CliRunner

from nomad.cli import cli
from nomad.config.access import access_from, members_lookup, visible_people

UR = {"console": {
    "roles": {"admin": ["jtonini", " Jacob "], "operator": ["ops1"]},
    "labs": {"group_pattern": "{netid}$", "leads": {"carol": ["chemlab$", "carol$"]}},
}}


def test_roles_from_the_file():
    a = access_from(UR)
    assert a.role("jtonini") == "admin"
    assert a.role("JACOB") == "admin"            # NetIDs compare without case or spaces
    assert a.role("ops1") == "operator"
    assert a.role("carol") is None               # the file grants her no role: a viewer
    assert a.role(None) is None
    assert a.named() == {"jtonini", "jacob", "ops1", "carol"}
    assert a.problems == ()


def test_labs_by_name_and_by_leads():
    a = access_from(UR)
    exists = {"carol$", "chemlab$", "dana$"}.__contains__
    assert a.lab_groups("carol", exists) == ["chemlab$", "carol$"]   # listed once
    assert a.lab_groups("dana", exists) == ["dana$"]                  # by the pattern
    assert a.lab_groups("erin", exists) == []                         # no such group
    assert a.lab_groups("erin") == ["erin$"]                          # existence unknown


def test_no_pattern_no_labs_from_names():
    """A site that has not said how its groups work gets no lab view."""
    a = access_from({"console": {"labs": {"leads": {"carol": "chemlab$"}}}})
    assert a.group_pattern == ""
    assert a.lab_groups("dana", lambda g: True) == []
    assert a.lab_groups("carol") == ["chemlab$"]                      # a string is a list of one
    assert access_from({}).lab_groups("carol", lambda g: True) == []


@pytest.mark.parametrize("config,expect", [
    ({"console": {"roles": {"pi": ["carol"]}}}, "a PI comes from [console.labs]"),
    ({"console": {"roles": {"superuser": ["x"]}}}, "not a role"),
    ({"console": {"roles": {"admin": ["x"], "operator": ["x"]}}}, "both admin and operator"),
    ({"console": {"roles": {"admin": [5]}}}, "is not a name"),
    ({"console": {"roles": {"admin": {"x": 1}}}}, "should be a list"),
    ({"console": {"roles": "admin"}}, "[console.roles] is not a table"),
    ({"console": {"labs": {"group_pattern": "faculty"}}}, "has no {netid}"),
    ({"console": {"labs": {"group_pattern": 3}}}, "should be text"),
    ({"console": {"labs": {"leads": ["carol"]}}}, "should be a table"),
    ({"console": {"labs": {"pattern": "{netid}$"}}}, "unknown setting"),
])
def test_what_is_wrong_is_said_and_left_out(config, expect):
    a = access_from(config, log=False)
    assert any(expect in p for p in a.problems), a.problems
    assert a.role("x") in (None, "admin")


def test_both_admin_and_operator_is_admin():
    a = access_from({"console": {"roles": {"admin": ["x"], "operator": ["x", "y"]}}}, log=False)
    assert a.role("x") == "admin" and a.role("y") == "operator"


def test_a_pattern_without_netid_would_make_one_group_everyones_lab():
    a = access_from({"console": {"labs": {"group_pattern": "faculty"}}}, log=False)
    assert a.group_pattern == ""
    assert a.lab_groups("anyone", lambda g: True) == []


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "combined.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    rows = [(u, "carol$", "spydur") for u in ("carol", "s1", "s2", "s3")]
    rows += [(u, "chemlab$", "arachne") for u in ("s3", "S4")]
    rows += [("s1", "carol$", "arachne")]                        # same person, two sites
    c.executemany("INSERT INTO group_membership VALUES (?, ?, ?)", rows)
    c.commit()
    c.close()
    return path


def test_whom_a_pi_sees(db):
    a = access_from(UR)
    c = sqlite3.connect(db)
    exists, members = members_lookup(c)
    assert exists("carol$") and not exists("nobody$")
    assert visible_people(a, "carol", members, exists) == {"carol", "s1", "s2", "s3", "s4"}
    assert visible_people(a, "s2", members, exists) == {"s2"}       # a student: themselves
    assert visible_people(a, "jtonini", members, exists) is None    # admin: everyone
    c.close()


def test_no_membership_table_means_no_labs(tmp_path):
    c = sqlite3.connect(tmp_path / "empty.db")
    exists, members = members_lookup(c)
    assert not exists("carol$") and members("carol$") == set()
    assert visible_people(access_from(UR), "dana", members, exists) == {"dana"}


def test_cli_roles(tmp_path, db):
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.roles]\nadmin = ["jtonini"]\npi = ["carol"]\n\n'
                   '[console.labs]\ngroup_pattern = "{netid}$"\n'
                   'leads = { carol = ["chemlab$"] }\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles"])
    assert r.exit_code == 0, r.output
    assert "admin:     1 (jtonini)" in r.output
    assert "a PI comes from [console.labs]" in r.output

    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "carol", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "viewer, and a PI: leads 2 labs" in r.output
    assert "chemlab$: 2 members" in r.output and "carol$: 4 members" in r.output
    assert "their own work and 4 lab members" in r.output

    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "carol", "--db", str(db),
                                 "--mask"])
    assert r.exit_code == 0, r.output
    assert "carol" not in r.output.split("Console access")[1].replace("console.labs", "")
    assert "jtonini" not in r.output
    assert "lab #1: 2 members" in r.output
