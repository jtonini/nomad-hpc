# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.5: research assistants of a lab ([console.labs.assistants], `nomad lab
assistant`), and a group made a course this term is a class, not a lab."""
import sqlite3

import pytest
from click.testing import CliRunner

import nomad.cli as cli_mod
from nomad.cli import cli
from nomad.config.access import access_from, visible_people
from nomad.config.edit import TomlEdit

HUB = '''[general]
x = 1

[console.labs]
group_pattern = "{netid}$"
leads = {}                     # e.g. { NETID = ["chemlab$"] }

[console.labs.resources."pi1$"]
name = "Smith Lab"
workstations = ["labws1"]
'''


def members(table):
    return lambda g: set(table.get(g, ()))


def test_an_assistant_sees_the_lab_only_while_a_member():
    acc = access_from({"console": {"labs": {"group_pattern": "{netid}$",
                                            "assistants": {"pi1$": ["Eve", "zed"]}}}}, log=False)
    assert acc.assistants == {"pi1$": ("eve", "zed")}
    m = members({"pi1$": ["eve", "alice", "pi1"]})
    assert acc.assisted_groups("eve", m) == ["pi1$"]
    assert acc.assisted_groups("zed", m) == []                       # left the group
    assert acc.assisted_groups("eve", None) == []                    # can't tell: no
    assert acc.lapsed_assistants(m) == [("pi1$", "zed")]
    assert visible_people(acc, "eve", m) == {"eve", "alice"}         # not the PI's own work
    assert acc.leaders("pi1$") == {"pi1"}
    assert visible_people(acc, "zed", m) == {"zed"}
    assert acc.lab_groups("eve", lambda g: False) == []              # not a PI
    assert acc.group_resources(["pi1$"]) == {"workstations": set(), "storage": set()}


def test_a_group_made_a_course_this_term_is_a_class_not_a_lab():
    def acc(extra_labs=None, term=None):
        course = {"instructors": ["inst1"]} if term is None else \
            {"terms": {term: {"instructors": ["inst1"]}}}
        labs = {"group_pattern": "{netid}$", **(extra_labs or {})}
        return access_from({"console": {"labs": labs, "courses": {"term_start": "2026-08-24",
                                                                  "groups": {"inst1$": course}}}},
                           log=False)
    assert acc().lab_groups("inst1") == []
    assert acc().course_groups == frozenset({"inst1$"})
    assert acc().lab_groups("pi2") == ["pi2$"]                       # others' labs as before
    # machines listed for it, or a lead: still a lab
    assert acc({"resources": {"inst1$": {"workstations": ["ws1"]}}}).lab_groups("inst1") == ["inst1$"]
    assert acc({"leads": {"inst1": ["inst1$"]}}).lab_groups("inst1") == ["inst1$"]
    # a course only in another term: a lab this term
    assert acc(term="2026-01-12").lab_groups("inst1") == ["inst1$"]
    assert acc(term="2026-08-24").lab_groups("inst1") == []


def test_set_assistant_writes_its_own_table_and_nothing_else(tmp_path):
    from nomad.config import labs
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    e = TomlEdit(p)
    assert labs.set_assistant(e, "pi1$", "Eve") == ["eve is a research assistant of pi1$"]
    assert labs.set_assistant(e, "pi1$", "eve") == []
    assert labs.set_assistant(e, "pi1$", "zed") == ["zed is a research assistant of pi1$"]
    assert '[console.labs.assistants]\n"pi1$" = ["eve", "zed"]\n' in e.text
    assert "leads = {}" in e.text and "# e.g." in e.text
    assert labs.assistants_of(e.data) == {"pi1$": ["eve", "zed"]}
    assert labs.set_assistant(e, "pi1$", "eve", remove=True) == ["eve is no longer a research assistant of pi1$"]
    assert labs.set_assistant(e, "pi1$", "zed", remove=True)
    assert "assistants" not in e.text                                 # gone when empty
    with pytest.raises(labs.LabError):
        labs.set_assistant(e, "pi1$", "a@b")
    # an inline line in [console.labs] becomes the table, its names kept
    p.write_text(HUB.replace("leads = {}", 'leads = {}\nassistants = { "pi1$" = ["old"] }'))
    e = TomlEdit(p)
    labs.set_assistant(e, "pi1$", "eve")
    assert labs.assistants_of(e.data) == {"pi1$": ["old", "eve"]} and "assistants = {" not in e.text
    # written some other way: left for a person
    p.write_text(HUB.replace('[console.labs]\n', '[console.labs]\nassistants."pi1$" = ["old"]\n'))
    with pytest.raises(labs.LabError, match="by hand"):
        labs.set_assistant(TomlEdit(p), "pi1$", "eve")


def test_the_command_shows_writes_and_says_when_not_a_member(tmp_path, monkeypatch):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    monkeypatch.setattr(cli_mod, "_lab_member", lambda config, group, netid, path: False)
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "pi1", "eve"])
    assert r.exit_code == 0, r.output
    assert "+ eve is a research assistant of pi1$" in r.output and "isn't one of pi1$'s members" in r.output
    assert p.read_text() == HUB
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "Smith Lab", "eve", "--apply"])
    assert r.exit_code == 0 and "Written." in r.output, r.output
    assert access_from(TomlEdit(p).data, log=False).assistants == {"pi1$": ("eve",)}
    # a lab nomad doesn't know: refused unless --new-group
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "nobody", "eve"])
    assert r.exit_code != 0 and "not a lab nomad knows" in r.output
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "pi1", "eve", "--remove", "--apply"])
    assert r.exit_code == 0 and "assistants" not in p.read_text()


def test_console_roles_says_whether_it_applies(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"pi1$" = ["eve", "zed"]\n')
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'pi1$', 'c')", [("eve",), ("pi1",), ("al",)])
    c.commit()
    c.close()
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(p), "console", "roles", *a, "--db", str(db)]).output
    out = run()
    assert "assistants: 2 research assistants listed for 1 lab" in out
    assert "1 research assistant entry for someone no longer in the lab's group" in out and "zed in pi1$" in out
    eve = run("eve")
    assert "research assistant of Smith Lab (pi1$): sees it as its PI does" in eve
    assert "and 1 lab members" in eve                         # al; not pi1, its PI
    assert "labws1" in eve                                    # the lab's machines, as its PI
    assert "not one of its members now, so it doesn't apply" in run("zed")
    masked = run("--mask")
    assert "zed in pi1$" not in masked and "1 research assistant entry for someone no longer" in masked
    assert "research assistant of lab #1" in run("eve", "--mask")
    # a lab the groups data knows nothing of: not "lapsed"
    p.write_text(HUB + '\n[console.labs.assistants]\n"pi1$" = ["eve"]\n"ghost$" = ["amy"]\n')
    out = run()
    assert "no longer in the lab's group" not in out
    assert "1 lab with research assistants listed has no members in the groups data" in out
    assert "the groups data has no members for it" in run("amy")
    # an unreadable database: a note, no traceback
    db.write_bytes(b"not a database at all" * 100)
    r = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "--db", str(db)])
    assert r.exit_code == 0 and "can't be read" in r.output, r.output


def test_a_class_this_term_has_no_research_assistants():
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"inst1$": ["eve"]}},
                       "courses": {"term_start": "2026-08-24",
                                   "groups": {"inst1$": {"terms": {"2026-08-24": {"instructors": ["inst1"]}}}}}}}
    m = members({"inst1$": ["eve", "inst1"]})
    acc = access_from(cfg, log=False)
    assert not acc.lab_now("inst1$") and acc.assisted_groups("eve", m) == []
    assert visible_people(acc, "eve", m) == {"eve"}
    # a lead named for it keeps it a lab: for the lead, the PI and the assistants
    cfg["console"]["labs"]["leads"] = {"copi": ["inst1$"]}
    acc = access_from(cfg, log=False)
    assert acc.lab_now("inst1$") and acc.assisted_groups("eve", m) == ["inst1$"]
    assert acc.lab_groups("inst1", lambda g: True) == ["inst1$"]
    # another term: a lab again
    cfg["console"]["labs"].pop("leads")
    cfg["console"]["courses"]["term_start"] = "2027-01-11"
    assert access_from(cfg, log=False).assisted_groups("eve", m) == ["inst1$"]


def test_console_roles_and_lab_assistant_say_when_it_is_a_class(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_lab_member", lambda config, group, netid, path: True)
    monkeypatch.setattr(cli_mod, "_lab_size", lambda config, group, path: 2)
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"inst1$" = ["eve"]\n'
                 '\n[console.courses]\nterm_start = "2026-08-24"\n'
                 '\n[console.courses.groups."inst1$".terms."2026-08-24"]\ninstructors = ["inst1"]\n')
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'inst1$', 'c')", [("eve",), ("inst1",)])
    c.commit()
    c.close()
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "eve", "--db", str(db)]).output
    assert "a course this term, a class rather than a lab, so it doesn't apply" in out
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "inst1$", "zed", "--new-group"])
    assert "inst1$ is a course this term: a class rather than a lab" in r.output, r.output
    assert "(2 members in the groups data)" in r.output


def test_course_add_says_when_a_lab_becomes_a_class(tmp_path, monkeypatch):
    import nomad.config.course_cli as cc
    monkeypatch.setattr(cc, "_group_known", lambda ctx, g: True)
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"inst1$" = ["eve"]\n'
                 '\n[console.courses]\nterm_start = "2026-08-24"\n')
    r = CliRunner().invoke(cli, ["-c", str(p), "course", "add", "inst1$", "inst1"])
    assert r.exit_code == 0, r.output
    assert ("As a course this term, inst1$ is a class rather than a lab: it is not inst1's lab "
            "this term (no My lab for it); its research assistant (1) sees nothing of it") in r.output
    # pi1$ has machines listed: still a lab, nothing to say
    r = CliRunner().invoke(cli, ["-c", str(p), "course", "add", "pi1$", "pi1"])
    assert "class rather than a lab" not in r.output, r.output


def test_facts_give_an_assistant_the_lab_as_its_pi_sees_it(tmp_path):
    import copy
    from tests import test_facts
    from nomad import facts
    path = tmp_path / "combined.db"
    test_facts.build_hub(path)
    conn = facts.open_readonly(path)
    try:
        cfg = copy.deepcopy(test_facts.CONFIG)
        cfg["console"]["labs"]["assistants"] = {"alice$": ["bob", "zed"]}
        pi = facts.labs(conn, cfg, "alice", now=test_facts.NOW, names=True)["labs"]
        ra = facts.labs(conn, cfg, "bob", now=test_facts.NOW, names=True)["labs"]
        assert [(x["group"], x["as"]) for x in pi] == [("alice$", "pi")]
        assert [(x["group"], x["as"]) for x in ra] == [("alice$", "assistant")]
        assert ra[0]["storage"] == pi[0]["storage"]
        # the lab's members but for its PI: alice's own work stays hers
        assert {r["user"] for r in ra[0]["each"]} == {"bob"} and ra[0]["members"]["ran_jobs"] == 1
        assert facts.labs(conn, cfg, "zed", now=test_facts.NOW)["labs"] == []      # not a member
        staff = facts.labs(conn, cfg, groups=["alice$"], now=test_facts.NOW)["labs"]
        assert "as" not in staff[0]
    finally:
        conn.close()


def test_check_assistant_refuses_any_other_change():
    from nomad.config.labs import check_assistant
    before = {"console": {"labs": {"group_pattern": "{netid}$",
                                   "assistants": {"a$": ["x"], "b$": ["y"]}}}, "general": {"k": 1}}
    ok = {"console": {"labs": {"group_pattern": "{netid}$",
                               "assistants": {"a$": ["x", "eve"], "b$": ["y"]}}}, "general": {"k": 1}}
    check = check_assistant(before, "a$", "Eve")
    assert check(ok)
    bad = [
        {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"a$": ["x"], "b$": ["y"]}}},
         "general": {"k": 1}},                                                     # not added
        {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"a$": ["eve"], "b$": ["y"]}}},
         "general": {"k": 1}},                                                     # x lost
        {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"a$": ["x", "eve"]}}},
         "general": {"k": 1}},                                                     # b$ lost
        {"console": {"labs": {"group_pattern": "", "assistants": {"a$": ["x", "eve"], "b$": ["y"]}}},
         "general": {"k": 1}},                                                     # pattern changed
        {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"a$": ["x", "eve"], "b$": ["y"]}}},
         "general": {"k": 2}},                                                     # elsewhere
    ]
    assert not any(check(d) for d in bad)
    gone = check_assistant(ok, "a$", "eve", remove=True)
    assert gone(before) and not gone(ok)


def test_a_condo_owners_group_counts_whether_or_not_a_class():
    from nomad.condo.figures import resolve_members
    from nomad.condo.config import Condo
    acc = access_from({"console": {"labs": {"group_pattern": "{netid}$"},
                                   "courses": {"term_start": "2026-08-24",
                                               "groups": {"pi1$": {"instructors": ["pi1"]}}}}}, log=False)
    assert acc.lab_groups("pi1", lambda g: True) == []
    condo = Condo(name="c", cluster="hpc", owners=["pi1"], members=None)
    people, how = resolve_members(None, condo, acc, members({"pi1$": ["pi1", "stu"]}), lambda g: True)
    assert people == {"pi1", "stu"} and "pi1$" in how


def test_a_group_named_like_a_course_is_a_class_too():
    """Review: not only the groups nomad.toml lists -- one its pattern makes a
    course has no research assistants either (a TA would see the class one by
    one)."""
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"demo270": ["ta1"]}},
                       "courses": {"term_start": "2026-08-24"}}}
    m = members({"demo270": ["ta1", "s1", "s2"]})
    acc = access_from(cfg, log=False)
    assert acc.is_class("demo270") and not acc.lab_now("demo270")
    assert acc.assisted_groups("ta1", m) == [] and visible_people(acc, "ta1", m) == {"ta1"}
    # a pattern that takes $ groups: the PI's own group is a class too
    cfg["console"]["courses"]["pattern"] = r"^[a-z0-9]+\$$"
    acc = access_from(cfg, log=False)
    assert acc.lab_groups("inst1", lambda g: True) == []
    # no [console.courses]: nothing is a course
    assert access_from({"console": {"labs": {"group_pattern": "{netid}$"}}}, log=False).lab_now("demo270")


def test_a_tagged_workstation_does_not_keep_a_class_a_lab_but_a_lead_does(tmp_path):
    cfg = {"console": {"labs": {"group_pattern": "{netid}$"},
                       "courses": {"term_start": "2026-08-24",
                                   "groups": {"inst1$": {"instructors": ["inst1"]}}}}}
    acc = access_from(cfg, log=False)
    assert acc.lab_groups("inst1", lambda g: True, tagged=lambda g: {"ws1"}) == []
    # `nomad lab lead inst1 inst1` writes an entry for the pattern PI when the
    # group is a class this term (it would be a no-op otherwise)
    from nomad.config import labs
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.courses]\nterm_start = "2026-08-24"\n'
                 '\n[console.courses.groups."inst1$"]\ninstructors = ["inst1"]\n')
    e = TomlEdit(p)
    assert labs.set_lead(e, "inst1$", "inst1") == ["inst1 leads inst1$"]
    assert access_from(e.data, log=False).lab_groups("inst1", lambda g: True) == ["inst1$"]
    assert labs.set_lead(TomlEdit(tmp_path / "nomad.toml"), "pi1$", "pi1") == []    # a lab: no entry


def test_names_that_are_not_netids_are_reported_and_entries_nomad_cant_read_are_left_alone(tmp_path):
    acc = access_from({"console": {"labs": {"assistants": {"pi1$": ["a@b.edu", "*", "ok"]}}}}, log=False)
    assert acc.assistants == {"pi1$": ("ok",)}
    assert sum("is not a NetID" in x for x in acc.problems) == 2
    assert any("should be a table" in x for x in
               access_from({"console": {"labs": {"assistants": []}}}, log=False).problems)
    from nomad.config import labs
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"pi1$" = ["eve"]\n"pi3$" = 5\n')
    with pytest.raises(labs.LabError, match="can't read"):
        labs.set_assistant(TomlEdit(p), "pi1$", "zed")


def test_lead_notes_say_when_a_class_becomes_a_lab_or_back(tmp_path, monkeypatch):
    """Review 2: a lead entry on a class opens it to its PI and research
    assistants (say so); group_pattern messages only while it is a lab."""
    monkeypatch.setattr(cli_mod, "_lab_member", lambda config, group, netid, path: True)
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"inst1$" = ["eve"]\n'
                 '\n[console.courses]\nterm_start = "2026-08-24"\n'
                 '\n[console.courses.groups."inst1$"]\ninstructors = ["inst1"]\n')
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(p), "lab", *a])
    r = run("lead", "inst1$", "inst1", "--new-group", "--apply")
    assert r.exit_code == 0, r.output
    assert "with this entry it is a lab as well" in r.output and "its 1 research assistant" in r.output
    r = run("lead", "inst1$", "inst1", "--remove", "--apply")
    assert "still leads inst1$ through group_pattern" not in r.output, r.output
    assert "with no lead entry left it is a class rather than a lab" in r.output
    r = run("lead", "inst1$", "inst1", "--remove")
    assert "through group_pattern" not in r.output, r.output


def test_why_a_pi_has_no_lab_this_term(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.courses]\nterm_start = "2026-08-24"\n'
                 '\n[console.courses.groups."inst1$"]\ninstructors = ["inst1"]\n')
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'inst1$', 'c')", [("s1",), ("inst1",)])
    c.commit()
    c.close()
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "inst1", "--db", str(db)]).output
    assert "inst1$: a course this term, a class rather than a lab" in out, out
    masked = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "inst1", "--db", str(db),
                                      "--mask"]).output
    assert "inst1$" not in masked and "their own group: a course this term" in masked
    from nomad.facts.cli import _why_no_lab
    from nomad.config import read_toml
    assert "inst1$ is a course this term" in _why_no_lab(read_toml(p), "inst1")
    assert _why_no_lab(read_toml(p), "pi1") == ""
    db.write_bytes(b"junk" * 500)
    r = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "inst1", "--db", str(db)])
    assert r.exit_code == 0 and "can't be read" in r.output, r.output


def test_an_assistant_is_for_a_lab_not_any_group(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_lab_member", lambda config, group, netid, path: True)
    monkeypatch.setattr(cli_mod, "_lab_size", lambda config, group, path: 900)
    monkeypatch.setattr(cli_mod, "_lab_known", lambda *a, **k: True, raising=False)
    p = tmp_path / "nomad.toml"
    # With "{netid}$", a LAB without "$" is a PI's NetID; without a pattern, the group itself.
    p.write_text(HUB.replace('group_pattern = "{netid}$"', 'group_pattern = ""'))
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "faculty", "eve", "--new-group"])
    assert r.exit_code != 0 and "not a lab by nomad's rules" in r.output, r.output
    p.write_text(HUB)
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "jdoe$", "eve", "--new-group"])
    assert r.exit_code == 0 and "(900 members in the groups data)" in r.output, r.output
    # taking a name off is always possible
    p.write_text(HUB.replace('group_pattern = "{netid}$"', 'group_pattern = ""')
                 + '\n[console.labs.assistants]\n"faculty" = ["eve"]\n')
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "assistant", "faculty", "eve", "--remove",
                                 "--new-group", "--apply"])
    assert r.exit_code == 0 and "assistants" not in p.read_text(), r.output


def test_named_instructors_teach_a_reused_group_alone(tmp_path):
    """João, 26 Oct: a course group is reused term after term by whoever
    teaches the course; adding this term's instructor takes the earlier one
    out (they were its only faculty member), and nobody else is asked."""
    from nomad.config.courses import ADDED, courses_from, teaching
    cfg = {"console": {"courses": {"term_start": "2026-08-24", "groups": {
        "demo242": {"terms": {"2026-08-24": {"instructors": ["newprof"]}}}}}}}
    c = courses_from(cfg)
    m = {"demo242": {"oldprof", "s1"}, "faculty": {"oldprof", "newprof"}}
    t = teaching(c, "demo242", lambda g: m.get(g, set()), active=lambda g, cand: {},
                 answers={"oldprof": True})
    assert t["instructors"] == {"newprof"} and t["why"] == {"newprof": ADDED}
    assert t["left_out"] == {"oldprof"}
    # next term nothing is named: the guess again (its one faculty member)
    t = teaching(c, "demo242", lambda g: m.get(g, set()), term="2027-01-11")
    assert t["instructors"] == {"oldprof"}


def test_a_leader_spelled_in_capitals_is_still_left_out(tmp_path):
    """Review (Console fix 59, round 2): the groups data keeps the directory's
    spelling; leaders() is lower case."""
    import copy
    import sqlite3 as sq
    from tests import test_facts
    from nomad import facts
    path = tmp_path / "combined.db"
    test_facts.build_hub(path)
    c = sq.connect(path)
    for user in ("Hank", "bob", "ivy"):
        c.execute("INSERT INTO group_membership VALUES (?, 'hank$', 1, 'hpc1', ?, 1, 'hpc1')",
                  (user, test_facts.t(minutes=30)))
    c.commit()
    c.close()
    conn = facts.open_readonly(path)
    try:
        cfg = copy.deepcopy(test_facts.CONFIG)
        cfg["console"]["labs"]["assistants"] = {"hank$": ["bob"]}
        ra = [x for x in facts.labs(conn, cfg, "bob", now=test_facts.NOW, names=True)["labs"]
              if x["group"] == "hank$"]
        assert [x["as"] for x in ra] == ["assistant"]
        assert {r["user"] for r in ra[0]["each"]} == {"bob", "ivy"}
        assert ra[0]["members"]["current"] == 2
        acc = access_from(cfg, log=False)
        from nomad.config.access import members_lookup
        _, m = members_lookup(conn)
        assert visible_people(acc, "bob", m) >= {"bob", "ivy"} and "hank" not in visible_people(acc, "bob", m)
    finally:
        conn.close()


def test_an_entry_for_a_group_that_is_not_a_lab_applies_to_no_one(tmp_path):
    """Review 3: with a pattern that has no suffix every group is named for
    someone; a lab is the group named for one of its own members."""
    acc = access_from({"console": {"labs": {"group_pattern": "{netid}",
                                            "assistants": {"people": ["zed"], "ann": ["zed"]}}}},
                      log=False)
    m = members({"people": ["zed", "x1", "x2"], "ann": ["ann", "zed", "s1"]})
    assert not acc.is_lab("people", m) and acc.is_lab("ann", m)
    assert acc.assisted_groups("zed", m) == ["ann"]
    assert visible_people(acc, "zed", m) == {"zed", "s1"}
    # by hand, with the usual pattern: "faculty" is no lab either
    acc = access_from({"console": {"labs": {"group_pattern": "{netid}$",
                                            "assistants": {"faculty": ["zed"]}}}}, log=False)
    assert acc.assisted_groups("zed", members({"faculty": ["zed", "f1", "f2"]})) == []
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.assistants]\n"faculty" = ["zed"]\n')
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.executemany("INSERT INTO group_membership VALUES (?, 'faculty', 'c')", [("zed",), ("f1",)])
    c.commit()
    c.close()
    out = CliRunner().invoke(cli, ["-c", str(p), "console", "roles", "--db", str(db)]).output
    assert "1 group with research assistants listed is not a lab by nomad's rules (faculty)" in out
    assert "no longer in the lab's group" not in out
