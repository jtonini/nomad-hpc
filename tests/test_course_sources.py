# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Courses from groups, instructors' own groups and environments (1.12.5):
what a class uses says who is in it and who teaches it; `nomad course
discover`, `nomad course env` and the questions `nomad course setup` asks."""
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from nomad.collectors import interactive_daily as D
from nomad.config import courses as C
from nomad.config.edit import TomlEdit
from nomad.facts.courses import CourseIndex, courses, discover, may_answer, questions

NOW = datetime(2026, 10, 13, 12, 0)
FALL, SPRING = "2026-08-24", "2027-01-11"
GROUPS_ONLY = {"console": {"courses": {"term_start": FALL}}}
WITH_ENVS = {"console": {"courses": {"term_start": FALL, "sources": ["groups", "environments"]}}}


def _cfg(**courses_table):
    return {"console": {"courses": {"term_start": FALL, **courses_table}}}


def _hub(tmp_path):
    """A hub's combined database, this term:

    * demo250, a course group reused term after term: profb (who taught it
      once) and old1-old3, earlier students (old1 and old2 active this term,
      in other classes). This term profa teaches it: her class is in her
      own group profa$ and uses the environment demo250, as she does. s7
      opened its kernel once, by mistake;
    * demo303, a course group (ProfD, s7-s9, s12) whose class uses dataenv
      (s12 works in RStudio: no environment); demo304 (profe, t1-t3);
    * profc$, profc's lab, using labenv;
    * demo999, an environment named like a course, no group: profd, s10, s11, s13;
    * py311, shared software named like a course: profc, profd and others;
    * shared, an environment demo303's and demo304's people both use;
    * club, a group with no faculty member; tiny, an environment one person used.

    Each used an environment on two days, enough to count (MIN_ENV_DAYS)."""
    conn = sqlite3.connect(tmp_path / "hub.db")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, "
                 "cluster TEXT, collected_at TEXT, has_account INTEGER, "
                 "account_missing_since TEXT, first_seen TEXT, source_site TEXT)")
    rows = {"faculty": ["profa", "profb", "profc", "profd", "profe"],
            "demo250": ["profb", "old1", "old2", "old3"],
            "profa$": ["profa", "s1", "s2", "s3", "s4"],
            "profc$": ["profc", "r1", "r2", "r3"],
            "demo303": ["ProfD", "s7", "s8", "s9", "s12"],
            "demo304": ["profe", "t1", "t2", "t3"],
            "club": ["s1", "s7", "r1", "x1"]}
    for g, users in rows.items():
        for u in users:
            conn.execute("INSERT INTO group_membership VALUES (?,?,1,'web1',?,1,NULL,NULL,'web1')",
                         (u, g, NOW.isoformat()))
    D.ensure(conn)
    for table in ("interactive_daily", "interactive_env_daily"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN source_site TEXT")
    used = {"demo250": ["profa", "s1", "s2", "s3", "s4"],
            "dataenv": ["profd", "s7", "s8", "s9"],
            "labenv": ["profc", "r1", "r2", "r3"],
            "demo999": ["profd", "s10", "s11", "s13"],
            "py311": ["profc", "profd", "s1", "s7", "r1"],
            "shared": ["profd", "profe", "s7", "t1"],
            "tiny": ["x1"]}

    def env_day(day, user, env, own=0, path=None):
        conn.execute("INSERT INTO interactive_env_daily (day, server_id, user, environment, path, "
                     "own, sessions, session_seconds, busy_seconds, cpu_seconds, updated_at, "
                     "source_site) VALUES (?, 'srv', ?, ?, ?, ?, 1, 3600, 600, 600, ?, 'web1')",
                     (day, user, env, path or f"/sw/envs/{env}", own, day + "T23:00:00"))
    active = {"old1", "old2", "s12", "t2", "t3"}
    for env, users in used.items():
        for u in users:
            active.add(u)
            for day in ("2026-10-01", "2026-10-02"):
                env_day(day, u, env)
    env_day("2026-10-05", "s7", "demo250")        # one wrong click: not one of its users
    for day in ("2026-10-01", "2026-10-02"):       # a student's own environment, named like one
        env_day(day, "s8", "demo304", own=1, path="/home/s8/envs/demo304")
    env_day("2026-03-01", "profb", "demo250")     # an earlier term's
    env_day("2026-03-02", "profb", "demo250")
    for u in sorted(active):
        conn.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
                     "session_seconds, source_site) VALUES ('2026-10-01', 'srv', ?, '(any)', 1, "
                     "3600, 'web1')", (u,))
    conn.commit()
    return conn


def _toml(tmp_path, text):
    p = tmp_path / "nomad.toml"
    p.write_text('[general]\ndata_dir = "/x"\n\n' + text)
    return p


def _cli(cfg, *args, **kw):
    from nomad.cli import cli
    return CliRunner().invoke(cli, ["-c", str(cfg), "course", *args], **kw)


# --- the settings -------------------------------------------------------------------------

def test_sources_say_which_names_make_a_course():
    c = C.courses_from(GROUPS_ONLY)
    assert c.sources == ("groups",) and c.is_course("demo101")
    assert not c.is_course("demo101", "environment")       # environments are not a source
    c = C.courses_from(WITH_ENVS)
    assert c.is_course("demo101") and c.is_course("demo101", "environment")
    c = C.courses_from(_cfg(sources=["environments"]))
    assert not c.is_course("demo101") and c.is_course("demo101", "environment")
    c = C.courses_from(_cfg(sources=["own_groups"]))
    assert not c.is_course("demo101") and not c.is_course("demo101", "environment")
    # Listed: a course whatever the sources.
    c = C.courses_from(_cfg(sources=["own_groups"], groups={"profa$": {"name": "Methods"}}))
    assert c.is_course("profa$")
    c = C.courses_from(_cfg(sources=["groups", "bogus"], groups={
        "demo303": {"environments": ["dataenv", "bad env"]}}))
    assert c.sources == ("groups",) and c.listed["demo303"].environments == ("dataenv",)
    assert any("bogus" in p for p in c.problems) and any("bad env" in p for p in c.problems)
    # An environment listed under a course is that course's, not one of its own.
    c = C.courses_from(_cfg(sources=["groups", "environments"],
                            groups={"demo303": {"environments": ["demo999"]}}))
    assert not c.is_course("demo999", "environment") and c.claimed() == {"demo999"}
    # Named like a course, an environment is the course of the group of its
    # name: shared software is named like courses too.
    c = C.courses_from(WITH_ENVS)
    assert C.course_keys(c, ["demo250"], ["demo250", "py311", "cuda121"]) == ["demo250"]
    assert C.course_keys(C.courses_from(_cfg(sources=["environments"])), ["demo250"],
                         ["demo250", "py311"]) == ["demo250"]


def test_a_group_listed_only_for_a_term_is_a_course_that_term():
    table = {"groups": {"profa$": {"terms": {FALL: {"instructors": ["profa"]}}}}}
    assert C.courses_from(_cfg(**table)).is_course("profa$")
    spring = C.courses_from({"console": {"courses": {"term_start": SPRING, **table}}})
    assert not spring.is_course("profa$")                      # her lab again
    assert spring.is_course("profa$", term=FALL)
    named = {"groups": {"profa$": {"name": "Methods", "terms": {FALL: {"instructors": ["profa"]}}}}}
    assert C.courses_from({"console": {"courses": {"term_start": SPRING, **named}}}) \
        .is_course("profa$")                                    # named: every term
    assert C.course_keys(spring, ["profa$", "demo101"]) == ["demo101"]
    # Someone excluded for a term makes no course.
    out = {"groups": {"profc$": {"terms": {FALL: {"exclude": ["profc"]}}}}}
    assert not C.courses_from(_cfg(**out)).is_course("profc$")


def test_settings_and_environments_are_written_and_read_back(tmp_path):
    p = _toml(tmp_path, "[console.courses]\nterm_start = \"2026-08-24\"\n")
    e = TomlEdit(p)
    assert C.set_settings(e, sources=["groups", "environments"]) == \
        ["sources = ['groups', 'environments']"]
    with pytest.raises(C.CourseError):
        C.set_settings(e, sources=["groups", "registrar"])
    with pytest.raises(C.CourseError):
        C.set_settings(e, sources=[])
    assert C.set_course(e, "demo303", env_add="dataenv") == \
        ["whoever used the environment dataenv this term is one of demo303's people"]
    assert C.set_course(e, "demo303", env_add="dataenv") == []                  # already
    with pytest.raises(C.CourseError):
        C.set_course(e, "demo303", env_add="two words")
    e.save(lambda d: True)
    c = C.courses_from(TomlEdit(p).data)
    assert c.sources == ("groups", "environments") and c.listed["demo303"].environments == \
        ("dataenv",)
    e = TomlEdit(p)
    assert C.set_course(e, "demo303", env_remove="dataenv") == \
        ["the environment dataenv no longer counts for demo303"]
    e.save(lambda d: True)
    assert "demo303" not in C.courses_from(TomlEdit(p).data).listed


# --- who is in a course and who teaches it -------------------------------------------------

def test_the_environment_a_class_uses_says_who_teaches_it_and_who_is_in_it(tmp_path):
    conn = _hub(tmp_path)
    # Groups only: demo250's one faculty member is last term's; its students
    # this term are earlier students active in other classes.
    idx = CourseIndex(conn, GROUPS_ONLY, NOW)
    assert idx.keys == ["demo250", "demo303", "demo304"]
    r = idx.roster("demo250")
    assert r["instructors"] == {"profb"} and r["this_term"] == {"old1", "old2"}
    # With environments: whoever used demo250 this term is in the course;
    # profa used it and teaches it; profb didn't and is left out. Not py311
    # or demo999: no group has their names.
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.keys == ["demo250", "demo303", "demo304"]
    assert idx.envs_of("demo250") == {"demo250"} and idx.envs_of("demo303") == set()
    r = idx.roster("demo250")
    t = r["teaching"]
    assert r["instructors"] == {"profa"} and t["why"] == {"profa": C.ENVIRONMENT}
    assert t["left_out"] == {"profb"}
    # Its students this term: those who used its environment (on two days:
    # not s7's one click), not earlier students active elsewhere.
    assert r["this_term"] == {"s1", "s2", "s3", "s4"} and "s7" not in r["listed"]
    assert r["left_out"] == {"old1", "old2", "old3"}
    assert idx.courses_of("profa") == ["demo250"] and idx.courses_of("profb") == []
    # NetIDs whatever their case in the groups data.
    assert idx.courses_of("profd") == ["demo303"] and idx.courses_of("PROFD") == ["demo303"]
    d = courses(conn, WITH_ENVS, now=NOW)
    shown = {x["group"]: x for x in d["courses"]}
    assert shown["demo250"]["instructors_why"] == {"profa": "environment"}
    assert shown["demo250"]["environments"] == ["demo250"]
    assert shown["demo250"]["students"]["this_term"] == 4
    assert shown["demo250"]["students"]["by_environment"]
    assert [x["group"] for x in courses(conn, WITH_ENVS, netid="profa", now=NOW)["courses"]] == \
        ["demo250"]
    # profa is asked about demo250, and may answer for it; a student may not.
    asked = {q["group"]: q for q in questions(conn, WITH_ENVS, "profa", now=NOW)}
    assert set(asked) == {"demo250"} and asked["demo250"]["why"] == C.ENVIRONMENT
    assert may_answer(conn, WITH_ENVS, "profa", "demo250", NOW) == (True, "")
    assert may_answer(conn, WITH_ENVS, "s1", "demo250", NOW)[0] is False
    assert may_answer(conn, GROUPS_ONLY, "profa", "demo250", NOW)[0] is False


def test_an_environment_with_no_group_is_a_course_when_listed(tmp_path):
    conn = _hub(tmp_path)
    for table in ({"name": "Data"}, {"environments": ["demo999"]}):
        cfg = _cfg(sources=["groups", "environments"], groups={"demo999": table})
        idx = CourseIndex(conn, cfg, NOW)
        assert "demo999" in idx.keys and idx.envs_of("demo999") == {"demo999"}
        r = idx.roster("demo999")
        assert r["instructors"] == {"profd"} and r["teaching"]["why"] == {"profd": C.ENVIRONMENT}
        assert r["this_term"] == {"s10", "s11", "s13"}
    # Listed under itself, it needs no source.
    idx = CourseIndex(conn, _cfg(groups={"demo999": {"environments": ["demo999"]}}), NOW)
    assert idx.roster("demo999")["instructors"] == {"profd"}


def test_a_listed_environment_adds_students_not_instructors(tmp_path):
    conn = _hub(tmp_path)
    # demo303's class uses dataenv: its people are in its group anyway, so the
    # group says who is in it -- s12, who works in RStudio, too.
    cfg = _cfg(sources=["groups", "environments"], groups={"demo303": {"environments": ["dataenv"]}})
    idx = CourseIndex(conn, cfg, NOW)
    assert idx.by_environment("demo303") == set()
    r = idx.roster("demo303")
    assert r["instructors"] == {"profd"} and r["this_term"] == {"s7", "s8", "s9", "s12"}
    # Two courses share an environment: its users are students of both, but
    # neither's faculty member teaches the other.
    cfg = _cfg(groups={"demo303": {"environments": ["shared"]},
                       "demo304": {"environments": ["shared"]}})
    idx = CourseIndex(conn, cfg, NOW)
    r3, r4 = idx.roster("demo303"), idx.roster("demo304")
    assert r3["instructors"] == {"profd"} and r4["instructors"] == {"profe"}
    assert "t1" in r3["this_term"] and "s7" in r4["this_term"]
    assert idx.courses_of("profe") == ["demo304"]
    assert may_answer(conn, cfg, "profe", "demo303", NOW)[0] is False
    # An own group with its environment listed: a course, its own students.
    idx = CourseIndex(conn, _cfg(groups={"profa$": {"environments": ["demo250"]}}), NOW)
    assert "profa$" in idx.keys
    r = idx.roster("profa$")
    assert r["instructors"] == {"profa"} and r["this_term"] == {"s1", "s2", "s3", "s4"}


# --- discover ----------------------------------------------------------------------------

def test_discover_finds_environments_and_own_groups(tmp_path):
    conn = _hub(tmp_path)
    d = discover(conn, GROUPS_ONLY, NOW)
    assert d["sources"] == ["groups"] and d["environments_recorded"]
    envs = {e["environment"]: e for e in d["environments"]}
    assert list(envs) == ["demo250", "py311", "dataenv", "demo999", "labenv", "shared", "tiny"]
    e = envs["demo250"]
    assert e["people"] == 5 and e["faculty"] == ["profa"] and e["courses"] == []
    assert e["named_like_a_course"] and e["group_of_its_name"]
    assert e["group"] == {"group": "profa$", "people": 4, "is_course": False, "own_of": "profa"}
    assert envs["py311"]["named_like_a_course"] and not envs["py311"]["group_of_its_name"]
    assert envs["py311"]["faculty"] == ["profc", "profd"]
    assert envs["dataenv"]["group"]["group"] == "demo303" and envs["dataenv"]["group"]["is_course"]
    assert not envs["dataenv"]["named_like_a_course"]
    groups = {g["group"]: g for g in d["groups"]}
    assert set(groups) == {"profa$", "profc$"}                 # club: no faculty member
    assert groups["profa$"]["own_of"] == "profa" and groups["profa$"]["active_this_term"] == 4
    assert groups["profa$"]["environment"]["environment"] == "demo250"
    assert d["courses"] == {"count": 3, "without_instructor": 0}
    d = discover(conn, _cfg(sources=["groups", "environments"]), NOW)
    envs = {e["environment"]: e for e in d["environments"]}
    assert envs["demo250"]["courses"] == ["demo250"] and envs["demo999"]["courses"] == []


def test_course_discover_says_what_to_do(tmp_path):
    _hub(tmp_path).close()
    db = str(tmp_path / "hub.db")
    cfg = _toml(tmp_path, f'[console.courses]\nterm_start = "{FALL}"\n')
    r = _cli(cfg, "discover", "--db", db)
    assert r.exit_code == 0, r.output
    out = r.output
    assert "demo250: 5 people (faculty: profa); named like a course, counts for none" in out
    assert "all 4 besides faculty are in profa$ (profa's own group)" in out
    assert "py311: 5 people (faculty: profc, profd); named like a course, no group of its name" \
        in out
    assert "tiny: 1 person; counts for no course; the one besides faculty is in club" in out
    assert "profa$: 5 members, 4 active this term besides faculty; profa's own group" in out
    todo = out[out.index("To do"):]
    # One change of sources (--sources sets the whole list), with each reason.
    assert "nomad course setup --sources groups,environments,own_groups" in todo
    assert "1 environment named like a course group (demo250)" in todo
    assert "1 faculty member's own group has people active this term" in todo
    assert "nomad course env demo303 dataenv" in todo
    assert "nomad course env demo999 demo999" in todo          # a course of its own, if listed
    assert "py311" not in todo                                 # two faculty members use it
    assert "labenv" not in todo and "shared" not in todo
    assert "nomad course add 'profa$'" not in todo             # her class: demo250's people
    assert "tiny" not in todo                                  # one person
    # --mask: no group, environment or NetID.
    r = _cli(cfg, "discover", "--db", db, "--mask")
    assert r.exit_code == 0, r.output
    for name in ("prof", "demo", "dataenv", "labenv", "tiny", "py311", "hub.db", "s1"):
        assert name not in r.output, (name, r.output)
    assert "nomad course env <group #" in r.output
    r = _cli(cfg, "discover", "--db", str(tmp_path / "none" / "x.db"), "--mask")
    assert r.exit_code != 0 and str(tmp_path) not in r.output
    # Own groups, once the site says classes are held in them: a course for
    # this term only (an own group is often a lab too).
    cfg = _toml(tmp_path, f'[console.courses]\nterm_start = "{FALL}"\n'
                          'sources = ["groups", "environments", "own_groups"]\n')
    todo = _cli(cfg, "discover", "--db", db).output
    todo = todo[todo.index("To do"):]
    assert "--sources" not in todo and "nomad course env demo303 dataenv" in todo
    assert "nomad course add 'profc$' profc" in todo and "this term only" in todo
    assert "labenv" not in todo and "nomad course add 'profa$'" not in todo
    off = _toml(tmp_path, "")
    assert "Courses are off" in _cli(off, "discover", "--db", db).output


def test_course_env_maps_an_environment_to_a_course(tmp_path, monkeypatch):
    import nomad.actions.remote as remote
    _hub(tmp_path).close()
    db = tmp_path / "hub.db"
    monkeypatch.setattr(remote, "hub_settings", lambda: {"combined_db": db})
    cfg = _toml(tmp_path, f'[console.courses]\nterm_start = "{FALL}"\n'
                          'sources = ["groups", "environments"]\n')
    r = _cli(cfg, "env", "demo303", "dataenv")
    assert r.exit_code == 0 and "whoever used the environment dataenv" in r.output
    assert "instructors still come from its group" in r.output
    assert "demo303" not in cfg.read_text()                                    # a dry run
    r = _cli(cfg, "env", "demo303", "dataenv", "--apply")
    assert r.exit_code == 0, r.output
    assert C.courses_from(TomlEdit(cfg).data).listed["demo303"].environments == ("dataenv",)
    # Named like a course: said so.
    r = _cli(cfg, "env", "demo303", "demo999")
    assert "demo999 is named like a course" in r.output
    # Already another course's: said so.
    r = _cli(cfg, "env", "demo304", "dataenv")
    assert "dataenv also counts for demo303" in r.output
    # Never seen: said so, and not refused (it may be used later).
    r = _cli(cfg, "env", "demo303", "newenv")
    assert r.exit_code == 0 and "No session has run from newenv yet" in r.output
    # An own group becomes a course with it, every term: said so.
    r = _cli(cfg, "env", "profc$", "labenv")
    assert "profc$ becomes a course every term" in r.output
    # A course that is only an environment can be listed by its name.
    r = _cli(cfg, "env", "demo999", "demo999")
    assert r.exit_code == 0, r.output
    r = _cli(cfg, "env", "nosuch", "dataenv")
    assert r.exit_code != 0 and "no such group" in r.output
    r = _cli(cfg, "env", "faculty", "dataenv")
    assert r.exit_code != 0 and "never a course" in r.output
    r = _cli(cfg, "env", "demo303", "dataenv", "--remove", "--apply")
    assert r.exit_code == 0, r.output
    assert "demo303" not in C.courses_from(TomlEdit(cfg).data).listed


# --- setup's questions -------------------------------------------------------------------

def test_setup_asks_how_classes_get_access_and_shows_what_it_finds(tmp_path):
    _hub(tmp_path).close()
    db = str(tmp_path / "hub.db")
    cfg = _toml(tmp_path, "")
    # Groups and environments; the pattern, faculty group, minimum and names as
    # offered, the term's first day given; then whether to write it.
    answers = "1,3\n\n\n2026-08-24\n\n\n"
    r = _cli(cfg, "setup", "--ask", "--db", db, input=answers + "n\n")
    assert r.exit_code == 0, r.output
    out = r.output
    assert "3 groups here named like a course: demo250, demo303, demo304" in out
    assert "2 faculty members' own groups have people active this term" in out
    assert "7 environments used this term, 1 named like a course group: demo250" in out
    assert ("matches 3 groups (demo250, demo303, demo304) and 1 environment with a group of "
            "its name (demo250)") in out
    assert "faculty: 5 members" in out
    assert "courses from groups, environments: 3 course(s) this term" in out
    assert "Nothing changed." in out and "console" not in cfg.read_text()
    assert "A new term" not in out                              # courses weren't on
    r = _cli(cfg, "setup", "--ask", "--db", db, input=answers + "y\n")
    assert r.exit_code == 0, r.output
    assert "Written." in r.output
    c = C.courses_from(TomlEdit(cfg).data)
    assert c.enabled and c.sources == ("groups", "environments") and c.term_start.isoformat() == FALL
    assert not c.names
    # Enter on the term's first day leaves it to the calendar, unset.
    cfg2 = _toml(tmp_path, "")
    r = _cli(cfg2, "setup", "--ask", "--db", db, "--apply",
             input="4\n2\nnobody\nn\nfaculty\nlast fall\n\n0\nn\n")
    assert r.exit_code == 0, r.output
    assert "1, 2 or 3" in r.output and "No names make a course" in r.output
    assert "nobody: no members in the groups data" in r.output and "as YYYY-MM-DD" in r.output
    assert "Enter: by the calendar" in r.output
    c = C.courses_from(TomlEdit(cfg2).data)
    assert c.sources == ("own_groups",) and c.faculty_group == "faculty" and c.term_start is None
    # With no database: the questions, without what it finds.
    cfg3 = _toml(tmp_path, "")
    r = _cli(cfg3, "setup", "--ask", "--db", str(tmp_path / "none.db"), "--apply",
             input="1\n\n\n\n\n\n")
    assert r.exit_code == 0, r.output
    assert "named like a course" not in r.output
    assert C.courses_from(TomlEdit(cfg3).data).sources == ("groups",)


def test_setup_takes_sources_on_the_command_line(tmp_path):
    cfg = _toml(tmp_path, f'[console.courses]\nterm_start = "{FALL}"\n')
    r = _cli(cfg, "setup", "--sources", "groups,environments", "--apply")
    assert r.exit_code == 0, r.output
    assert C.courses_from(TomlEdit(cfg).data).sources == ("groups", "environments")
    r = _cli(cfg, "setup", "--sources", "groups,registrar")
    assert r.exit_code != 0 and "registrar" in r.output
    # Not a terminal and nothing given: no questions (as before).
    r = _cli(cfg, "setup")
    assert r.exit_code == 0 and "Nothing to change" in r.output


def test_facts_course_shows_and_masks_environments(tmp_path):
    from nomad.cli import cli
    _hub(tmp_path).close()
    db = str(tmp_path / "hub.db")
    cfg = _toml(tmp_path, f'[console.courses]\nterm_start = "{FALL}"\n'
                          'sources = ["groups", "environments"]\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "course", "demo250", "--db", db])
    assert r.exit_code == 0, r.output
    assert "profa (used the course's environment this term)" in r.output
    assert "environment(s): demo250" in r.output
    assert "3 listed but not using its environment" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "list", "--db", db])
    line = next(x for x in r.output.splitlines() if x.startswith("  demo250:"))
    assert "profa (used its environment)" in line and "environment demo250" in line
    assert "3 listed but not using its environment" in line
    for args in (["facts", "course", "--mask"], ["facts", "course", "--json", "--mask"],
                 ["course", "list", "--mask"]):
        out = CliRunner().invoke(cli, ["-c", str(cfg), *args, "--db", db]).output
        for name in ("prof", "demo", "dataenv", "'faculty'", "[a-z]"):
            assert name not in out, (args, name, out)


def test_which_courses_a_person_teaches_without_figures(tmp_path):
    from nomad import facts
    conn = _hub(tmp_path)
    assert facts.teaches(conn, WITH_ENVS, "profa", now=NOW) == ["demo250"]
    assert facts.teaches(conn, GROUPS_ONLY, "profa", now=NOW) == []
    assert facts.teaches(conn, WITH_ENVS, "PROFD", now=NOW) == ["demo303"]
    assert facts.teaches(conn, {}, "profa", now=NOW) == []                   # courses off
    listed = _cfg(sources=["groups", "environments"], groups={"demo999": {"name": "Data"}})
    assert facts.every_course(conn, listed, now=NOW) == [
        {"group": "demo250", "name": "demo250"}, {"group": "demo303", "name": "demo303"},
        {"group": "demo304", "name": "demo304"}, {"group": "demo999", "name": "demo999 (Data)"}]
    assert facts.every_course(None, WITH_ENVS) == []


def test_an_older_hub_without_environments_still_answers(tmp_path):
    conn = _hub(tmp_path)
    conn.execute("DROP TABLE interactive_env_daily")
    conn.commit()
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.keys == ["demo250", "demo303", "demo304"]
    assert idx.roster("demo250")["instructors"] == {"profb"}
    d = discover(conn, WITH_ENVS, NOW)
    assert d["environments"] == [] and not d["environments_recorded"]
    # The group-only helpers (an older Console) agree for group courses.
    from nomad.config.access import members_lookup
    from nomad.facts.courses import Activity
    c = C.courses_from(GROUPS_ONLY)
    _, members_of = members_lookup(conn)
    act = Activity(conn, idx.since, members_of, c)
    assert C.courses_of(c, "profb", idx.groups, members_of, act.faculty, {}, FALL) == ["demo250"]


def _hub_use(conn, user, env, own=0, days=("2026-10-06", "2026-10-07"), path=None):
    for day in days:
        conn.execute("INSERT INTO interactive_env_daily (day, server_id, user, environment, path, "
                     "own, sessions, session_seconds, busy_seconds, cpu_seconds, updated_at, "
                     "source_site) VALUES (?, 'srv', ?, ?, ?, ?, 1, 3600, 0, 0, 'x', 'web1')",
                     (day, user, env, path or f"/sw/envs/{env}", own))
        conn.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
                     "session_seconds, source_site) VALUES (?, 'srv', ?, '(any)', 1, 3600, "
                     "'web1')", (day, user))
    conn.commit()


def test_a_persons_own_environment_counts_only_for_faculty(tmp_path):
    conn = _hub(tmp_path)
    # s8's own environment named demo304 makes them none of demo304's people.
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert "s8" not in idx.env_people.get("demo304", set())
    assert "s8" not in idx.people("demo304")
    # An instructor who built her course's environment herself still counts:
    # her students use it.
    conn.execute("UPDATE interactive_env_daily SET own = 1 WHERE user = 'profa'")
    conn.commit()
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    r = idx.roster("demo250")
    assert r["instructors"] == {"profa"} and r["teaching"]["why"] == {"profa": C.ENVIRONMENT}
    # A faculty member's own environment named like a course, nobody else's:
    # no claim on that course.
    conn.execute("INSERT INTO group_membership VALUES ('profx','faculty',1,'web1',?,1,NULL,NULL,"
                 "'web1')", (NOW.isoformat(),))
    _hub_use(conn, "profx", "demo250", own=1, path="/home/profx/envs/demo250")
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert "profx" not in idx.env_people["demo250"]
    assert idx.roster("demo250")["instructors"] == {"profa"}
    assert may_answer(conn, WITH_ENVS, "profx", "demo250", NOW)[0] is False


def test_one_outsider_in_a_courses_environment_changes_nothing(tmp_path):
    conn = _hub(tmp_path)
    _hub_use(conn, "u9", "demo303")               # not in demo303, used its kernel twice
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.by_environment("demo303") == set()
    r = idx.roster("demo303")
    assert r["this_term"] == {"s7", "s8", "s9", "s12", "u9"} and r["instructors"] == {"profd"}


def test_a_reused_groups_faculty_member_who_never_used_the_class_environment_is_left_out(tmp_path):
    conn = _hub(tmp_path)
    # profa teaches demo250 without opening its kernel: her students do.
    conn.execute("DELETE FROM interactive_env_daily WHERE user = 'profa'")
    conn.commit()
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.by_environment("demo250") == {"s1", "s2", "s3", "s4"}
    r = idx.roster("demo250")
    # Not profb, last term's, who would see her class: nobody until she is added.
    assert r["instructors"] == set() and "profb" not in idx.members("demo250")
    assert r["teaching"]["left_out"] == {"profb"}
    assert idx.courses_of("profb") == []
    cfg = _cfg(sources=["groups", "environments"], groups={"demo250": {"instructors": ["profa"]}})
    idx = CourseIndex(conn, cfg, NOW)
    assert idx.roster("demo250")["instructors"] == {"profa"}
    assert idx.roster("demo250")["this_term"] == {"s1", "s2", "s3", "s4"}


def test_early_in_a_term_an_outside_instructor_using_the_environment_is_enough(tmp_path):
    conn = _hub(tmp_path)
    # Only s1 of profa's class has used demo250 on two days so far.
    conn.execute("DELETE FROM interactive_env_daily WHERE environment = 'demo250' "
                 "AND user IN ('s2', 's3', 's4')")
    conn.commit()
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.by_environment("demo250") == {"s1"}
    r = idx.roster("demo250")
    assert r["instructors"] == {"profa"} and r["this_term"] == {"s1"}       # not old1, old2


def test_a_colleague_who_borrowed_a_courses_environment_teaches_nothing(tmp_path):
    conn = _hub(tmp_path)
    # demo303's class uses the environment demo303; profe, outside the group,
    # used it too; profd teaches it without opening the kernel.
    for user in ("s7", "s8", "s9", "profe"):
        _hub_use(conn, user, "demo303")
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert idx.by_environment("demo303") == set()
    assert "profe" not in idx.members("demo303")
    r = idx.roster("demo303")
    assert r["instructors"] == {"profd"} and r["teaching"]["why"] == {"profd": C.ONLY}
    assert r["this_term"] == {"s7", "s8", "s9", "s12"}
    assert idx.courses_of("profe") == ["demo304"]


def test_last_terms_instructor_using_the_environment_early_takes_nothing_from_her(tmp_path):
    conn = _hub(tmp_path)
    conn.execute("DELETE FROM interactive_env_daily WHERE environment = 'demo250' "
                 "AND user IN ('s2', 's3', 's4')")
    conn.commit()
    _hub_use(conn, "profb", "demo250")
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    # Both used it: both teach it until one says no or is excluded -- not
    # profb alone.
    r = idx.roster("demo250")
    assert r["instructors"] == {"profa", "profb"}
    assert {q["group"] for q in questions(conn, WITH_ENVS, "profa", now=NOW)} == {"demo250"}
    # A colleague alone in a group course's kernel (its students in RStudio):
    # still nothing.
    other = tmp_path / "other"
    other.mkdir()
    conn = _hub(other)
    _hub_use(conn, "profe", "demo303")
    idx = CourseIndex(conn, WITH_ENVS, NOW)
    assert "profe" not in idx.members("demo303")
    assert idx.roster("demo303")["instructors"] == {"profd"}


def test_another_courses_students_in_a_shared_environment_admit_no_colleague(tmp_path):
    conn = _hub(tmp_path)
    # demo303 lists "shared", which t1 (demo304) uses; profx opened only
    # demo303's own kernel.
    conn.execute("INSERT INTO group_membership VALUES ('profx','faculty',1,'web1',?,1,NULL,NULL,"
                 "'web1')", (NOW.isoformat(),))
    _hub_use(conn, "profx", "demo303")
    cfg = _cfg(sources=["groups", "environments"], groups={"demo303": {"environments": ["shared"]}})
    idx = CourseIndex(conn, cfg, NOW)
    assert idx.outside_students("demo303") == set() and "profx" not in idx.members("demo303")
    assert idx.roster("demo303")["instructors"] == {"profd"}
