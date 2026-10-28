# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.10: groups that are never labs ([console.labs] not_labs, `nomad lab
hide`): a service account's group, a mistyped one, someone's who left."""
import sqlite3

import pytest
from click.testing import CliRunner

import nomad.cli as cli_mod
from nomad.cli import cli
from nomad.config import labs
from nomad.config.access import access_from
from nomad.config.edit import TomlEdit

HUB = '''[general]
x = 1

[console.labs]
group_pattern = "{netid}$"     # the group a faculty member leads
leads = {}                     # e.g. { NETID = ["chemlab$"] }

[console.labs.resources."pi1$"]
name = "Smith Lab"
'''


LEADS_LINE = 'leads = {}                     # e.g. { NETID = ["chemlab$"] }'


def members(table):
    return lambda g: set(table.get(g, ()))


def _acc(**extra):
    return access_from({"console": {"labs": {"group_pattern": "{netid}$", **extra}}}, log=False)


def test_a_group_listed_is_no_lab_whatever_else_says():
    m = members({"svc$": ["svc"], "pi1$": ["pi1", "eve"], "typo$": ["eve"]})
    acc = _acc(not_labs=["svc$", "typo$"])
    assert acc.not_labs == frozenset({"svc$", "typo$"})
    assert not acc.is_lab("svc$", m) and acc.is_lab("pi1$", m)
    assert not acc.lab_now("svc$") and acc.lab_now("pi1$")
    assert acc.lab_groups("svc", lambda g: True) == []               # nobody leads it
    assert acc.lab_groups("pi1", lambda g: True) == ["pi1$"]
    assert acc.problems == ()
    # whatever else nomad.toml says of it: not a lab, and said so
    acc = _acc(not_labs="svc$", leads={"amy": ["svc$"]},
               resources={"svc$": {"name": "Service Lab"}}, assistants={"svc$": ["eve"]})
    assert acc.not_labs == frozenset({"svc$"})                       # one string: a list of one
    assert not acc.is_lab("svc$", m) and acc.lab_groups("amy", lambda g: g != "amy$") == []
    assert acc.assisted_groups("eve", members({"svc$": ["eve", "svc"]})) == []
    p, = acc.problems
    assert "not_labs lists svc$" in p and "a name or machines and a lead and research assistants" in p
    # not a list of names: reported, nothing hidden
    acc = _acc(not_labs={"svc$": True})
    assert acc.not_labs == frozenset() and "not_labs should be a list of names" in acc.problems[0]


def test_facts_labs_leaves_out_a_group_staff_asked_for():
    from nomad.facts.labs import labs as lab_facts
    config = {"console": {"labs": {"group_pattern": "{netid}$", "not_labs": ["svc$"],
                                   "resources": {"pi1$": {"name": "Smith Lab"}}}}}
    got = lab_facts(None, config, groups=["pi1$", "svc$"])
    assert [lab["group"] for lab in got["labs"]] == ["pi1$"]


def labs_after_one(p, group):
    e = TomlEdit(p)
    labs.set_not_lab(e, group)
    return e.data


def test_set_not_lab_writes_one_line_and_keeps_the_rest(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    e = TomlEdit(p)
    assert labs.set_not_lab(e, "svc$") == ["svc$: not a lab (not shown in the Console; nobody leads it)"]
    assert labs.set_not_lab(e, "svc$") == []
    assert labs.set_not_lab(e, "typo$")
    assert '[console.labs]\nnot_labs = ["svc$", "typo$"]\ngroup_pattern' in e.text
    assert "# the group a faculty member leads" in e.text and "# e.g." in e.text
    assert labs.not_labs_of(e.data) == ["svc$", "typo$"]
    before = TomlEdit(p).data
    assert labs.check_not_lab(before, "svc$")(labs_after_one(p, "svc$"))
    assert not labs.check_not_lab(before, "svc$", remove=True)(labs_after_one(p, "svc$"))
    assert labs.set_not_lab(e, "svc$", remove=True) == ["svc$: a lab again by nomad's rules"]
    assert labs.set_not_lab(e, "svc$", remove=True) == []
    assert labs.set_not_lab(e, "typo$", remove=True)
    assert e.text == HUB                                              # gone when empty
    with pytest.raises(labs.LabError, match='"shared" is not a lab'):
        labs.set_not_lab(e, "shared")


def test_set_not_lab_leaves_what_it_cant_rewrite(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB.replace('leads = {}', 'not_labs = [\n  "svc$",\n]\nleads = {}'))
    with pytest.raises(labs.LabError, match="by hand"):
        labs.set_not_lab(TomlEdit(p), "typo$")
    p.write_text('[console]\nlabs = { group_pattern = "{netid}$" }\n')
    with pytest.raises(labs.LabError, match="by hand"):
        labs.set_not_lab(TomlEdit(p), "typo$")
    # no [console.labs] yet: one is made
    p.write_text('[general]\nx = 1\n')
    e = TomlEdit(p)
    assert labs.set_not_lab(e, "svc$")
    assert access_from(e.data, log=False).not_labs == frozenset({"svc$"})


def test_lab_hide_and_what_nomad_says_of_it(tmp_path, monkeypatch):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    monkeypatch.setattr(cli_mod, "_lab_known", lambda config, group, path=None, db=None: group != "nobody$")
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "svc"])
    assert r.exit_code == 0 and "+ svc$: not a lab" in r.output and "Nothing changed" in r.output, r.output
    assert p.read_text() == HUB
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "svc", "--apply"])
    assert r.exit_code == 0 and "Written." in r.output, r.output
    assert access_from(TomlEdit(p).data, log=False).not_labs == frozenset({"svc$"})
    # one nomad.toml names: kept, said not to apply
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "pi1$", "--apply"])
    assert "nomad.toml also gives pi1$ a name: kept, but not shown" in r.output, r.output
    out = CliRunner().invoke(cli, ["-c", str(p), "lab", "show"]).output
    assert "not labs ([console.labs] not_labs, not shown): svc$, pi1$" in out
    assert "Smith Lab (pi1$):\n    not a lab" in out
    # a group nomad doesn't know: written, with a warning
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "nobody$"])
    assert "nomad knows no nobody$ here" in r.output
    # why someone leads no lab
    from nomad.facts.cli import _why_no_lab
    assert "not_labs lists svc$" in _why_no_lab(TomlEdit(p).data, "svc")
    db = tmp_path / "groups.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'svc$', 'c')", [("svc",)])
    c.commit()
    c.close()
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "svc", "--db", str(db)]).output
    assert "svc$: listed in [console.labs] not_labs, so not a lab" in out and "a PI" not in out
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "svc", "--remove", "--apply"])
    assert r.exit_code == 0 and "a lab again" in r.output
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "pi1$", "--remove", "--apply"])
    assert p.read_text() == HUB


def test_lab_hide_takes_a_group_nomad_knows_by_that_very_name(tmp_path, monkeypatch):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    seen = []

    def known(config, group, path=None, db=None):
        seen.append(db)
        return group == "research"                     # a group not named like a lab
    monkeypatch.setattr(cli_mod, "_lab_known", known)
    (tmp_path / "x.db").write_bytes(b"")
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "research", "--db", str(tmp_path / "x.db"),
                                 "--apply"])
    assert r.exit_code == 0 and "+ research: not a lab" in r.output, r.output
    assert labs.not_labs_of(TomlEdit(p).data) == ["research"] and str(tmp_path / "x.db") in seen
    # the pattern's group when that is the one nomad knows, or neither is
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "svc", "--apply"])
    assert labs.not_labs_of(TomlEdit(p).data) == ["research", "svc$"]
    # off the list as written
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "research", "--remove", "--apply"])
    assert "+ research: a lab again" in r.output and labs.not_labs_of(TomlEdit(p).data) == ["svc$"]


def test_the_line_stays_where_it_is_with_its_comment(tmp_path):
    p = tmp_path / "nomad.toml"
    text = HUB.replace(LEADS_LINE, LEADS_LINE + '\nnot_labs = ["a$"]   # service accounts')
    p.write_text(text)
    e = TomlEdit(p)
    assert labs.set_not_lab(e, "b$")
    assert LEADS_LINE + '\nnot_labs = ["a$", "b$"]   # service accounts\n' in e.text
    assert labs.set_not_lab(e, "a$", remove=True) and labs.set_not_lab(e, "b$", remove=True)
    assert 'not_labs = []   # service accounts\n' in e.text           # the comment stays
    # entries that aren't group names, a name with spaces: left to a person
    p.write_text(HUB.replace(LEADS_LINE, LEADS_LINE + '\nnot_labs = ["a$", 3]'))
    from nomad.config.edit import loads
    try:
        loads(p.read_text())
    except Exception:
        pass                    # an older TOML reader (Python 3.10) refuses the whole file
    else:
        with pytest.raises(labs.LabError, match="isn't a list of group names"):
            labs.set_not_lab(TomlEdit(p), "b$")
    with pytest.raises(labs.LabError, match="no spaces"):
        labs.set_not_lab(TomlEdit(tmp_path / "nomad.toml"), "smith lab$")
    # a single string, as access_from reads it
    p.write_text(HUB.replace('leads = {}', 'leads = {}\nnot_labs = "a$"'))
    e = TomlEdit(p)
    assert labs.set_not_lab(e, "b$") and labs.not_labs_of(e.data) == ["a$", "b$"]


def test_the_save_check_refuses_any_other_change(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    before = TomlEdit(p).data
    e = TomlEdit(p)
    labs.set_not_lab(e, "svc$")
    assert labs.check_not_lab(before, "svc$")(e.data)
    e.text = e.text.replace('name = "Smith Lab"', 'name = "Other"')
    assert not labs.check_not_lab(before, "svc$")(e.data)


def _groups_conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    conn.executemany("INSERT INTO group_membership VALUES (?, ?)", rows)
    return conn


def test_hiding_changes_no_condo_report_and_moves_no_condo():
    from nomad.condo.config import Condo
    from nomad.condo.figures import resolve_members
    from nomad.config.access import members_lookup
    conn = _groups_conn([("jdoe", "jdoe$"), ("s1", "jdoe$"), ("s2", "jdoe$"),
                         ("jdoe", "chemlab$"), ("s3", "chemlab$")])
    exists, members_of = members_lookup(conn)
    labs_cfg = {"group_pattern": "{netid}$", "leads": {"jdoe": ["chemlab$"]}}
    condo = Condo(name="jdoe", cluster="hpc1", owners=["jdoe"])
    shown = access_from({"console": {"labs": labs_cfg}}, log=False)
    hidden = access_from({"console": {"labs": {**labs_cfg, "not_labs": ["jdoe$"]}}}, log=False)
    # the monthly condo reports: the same members either way
    assert resolve_members(conn, condo, hidden, members_of, exists) == \
        resolve_members(conn, condo, shown, members_of, exists)


def test_hiding_one_of_two_labs_moves_no_condo(tmp_path):
    from datetime import datetime
    from tests.test_release_1179 import _db
    now = datetime.now()
    conn = _db(tmp_path, when=now)
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT, "
                 "collected_at TEXT, has_account INTEGER)")
    conn.executemany("INSERT INTO group_membership VALUES (?, ?, 'hpc1', ?, 1)",
                     [("jsmith", "jsmith$", now.isoformat()), ("st1", "jsmith$", now.isoformat()),
                      ("jsmith", "chemlab$", now.isoformat()), ("st2", "chemlab$", now.isoformat())])
    conn.commit()
    conn.close()
    toml = tmp_path / "nomad.toml"
    (tmp_path / "report.toml").write_text(
        '[report.clusters.hpc1.condos.smith]\nowner = "jsmith"\nlabel = "Smith condo"\n')
    run = lambda: CliRunner().invoke(cli, ["-c", str(toml), "facts", "lab", "jsmith", "--db",
                                           str(tmp_path / "n.db")]).output
    toml.write_text('[console.labs]\ngroup_pattern = "{netid}$"\nleads = { jsmith = ["chemlab$"] }\n')
    out = run()
    assert out.count("condo Smith condo") == 1                   # the lab named for its owner's
    assert out.index("condo Smith condo") < out.index("chemlab$") or "chemlab$" not in out.split(
        "condo Smith condo")[0].splitlines()[-1]
    toml.write_text('[console.labs]\ngroup_pattern = "{netid}$"\nnot_labs = ["jsmith$"]\n'
                    'leads = { jsmith = ["chemlab$"] }\n')
    out = run()
    assert "chemlab$" in out and "condo Smith condo" not in out, out


def test_busy_shows_a_hidden_groups_machines_by_site():
    from datetime import datetime
    from nomad.facts.busy import _labs
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE workstation_state (hostname TEXT, timestamp TEXT, department TEXT)")
    now = datetime.now()
    conn.executemany("INSERT INTO workstation_state VALUES (?, ?, ?)",
                     [("ws1", now.isoformat(), "svc$"), ("ws2", now.isoformat(), "pi1$")])
    config = {"console": {"labs": {"group_pattern": "{netid}$", "not_labs": ["svc$"],
                                   "resources": {"svc$": {"workstations": ["ws3"]}}}}}
    got, _, _ = _labs(conn, config, now)
    assert got == {"ws2": {"pi1$"}}


def test_lead_and_assistant_on_a_hidden_group(tmp_path, monkeypatch):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB.replace('leads = {}', 'leads = {}\nnot_labs = ["pi2$", "pi1$"]'))
    # a PI's own hidden group: no entry written (it's theirs by group_pattern once shown)
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "lead", "pi2$", "pi2", "--new-group"])
    assert "Nothing to change: pi2 leads pi2$ through group_pattern" in r.output, r.output
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "lead", "pi1$", "amy"])
    assert "not_labs lists pi1$: while it does, it is no lab" in r.output and "will see" not in r.output
    monkeypatch.setattr(cli_mod, "_lab_member", lambda config, group, netid, path: True)
    monkeypatch.setattr(cli_mod, "_lab_size", lambda config, group, path: 3)
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "pi1", "eve"])
    assert "not_labs lists pi1$" in r.output and "a course this term" not in r.output, r.output
    assert "will see" not in r.output


def test_roles_says_a_hidden_lead_and_assistant(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB.replace('leads = {}', 'not_labs = ["chem$"]\n[console.labs.leads]\namy = ["chem$"]')
                 + '\n[console.labs.assistants]\n"chem$" = ["eve"]\n')
    db = tmp_path / "groups.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'chem$', 'c')", [("amy",), ("eve",)])
    c.commit()
    c.close()
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "amy", "--db", str(db)]).output
    assert "chem$: listed in [console.labs] not_labs, so not a lab" in out and "a PI" not in out, out
    assert "members" not in out.split("chem$: listed")[1].splitlines()[0]
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "eve", "--db", str(db)]).output
    assert "research assistant of chem$: listed in [console.labs] not_labs" in out, out


def test_lab_hide_by_the_labs_name_and_a_missing_db(tmp_path, monkeypatch):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    monkeypatch.setattr(cli_mod, "_lab_known", lambda config, group, path=None, db=None: False)
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "Smith Lab", "--apply"])
    assert "+ pi1$: not a lab" in r.output, r.output
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "No Such Lab"])
    assert r.exit_code != 0 and "no spaces" in r.output
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "svc", "--db", str(tmp_path / "none.db")])
    assert r.exit_code != 0 and "no database at" in r.output
    # both "research" and "research$" listed: --remove takes the one typed
    p.write_text(HUB.replace('leads = {}', 'leads = {}\nnot_labs = ["research", "research$"]'))
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "hide", "research", "--remove", "--apply"])
    assert labs.not_labs_of(TomlEdit(p).data) == ["research$"]
