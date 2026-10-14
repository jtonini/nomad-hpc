# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Courses (nomad 1.12): which groups are courses, who teaches them, this
term's students, what an instructor sees, and the weeks before nomad kept
the totals, rebuilt from the raw snapshots."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from nomad.collectors import interactive_daily as D
from nomad.config import courses as C
from nomad.config.edit import TomlEdit
from nomad.facts.courses import courses, resolve_name

NOW = datetime(2026, 10, 13, 12, 0)
TERM = {"console": {"courses": {"term_start": "2026-08-24"}}}


# --- settings ---------------------------------------------------------------------------

def test_courses_are_off_until_the_site_says_how_its_groups_work():
    c = C.courses_from({})
    assert not c.enabled and not c.is_course("demo101")
    c = C.courses_from(TERM)
    assert c.enabled and c.is_course("demo101") and not c.is_course("faculty")
    assert not c.is_course("jdoe$") and not c.is_course("chem")
    assert c.start() == date(2026, 8, 24)


def test_the_term_starts_by_the_calendar_unless_set():
    # 15 August, 1 January, 1 June, each moved to the Monday on or after.
    assert C.term_start_for(date(2026, 10, 13)) == date(2026, 8, 17)
    assert C.term_start_for(date(2026, 3, 2)) == date(2026, 1, 5)
    assert C.term_start_for(date(2026, 7, 1)) == date(2026, 6, 1)
    assert C.term_start_for(date(2026, 1, 2)) == date(2025, 8, 18)     # before the first Monday
    assert C.courses_from({"console": {"courses": {}}}).start(date(2026, 10, 1)) == date(2026, 8, 17)


def test_a_group_named_like_a_course_can_be_said_not_to_be_one(tmp_path):
    c = C.courses_from({"console": {"courses": {"groups": {"lab201": {"course": False}}}}})
    assert not c.is_course("lab201") and c.is_course("demo101")
    groups = {"faculty": {"prof1"}, "lab201": {"prof1", "s1"}}
    assert C.courses_of(c, "prof1", ["lab201"], lambda g: groups.get(g, set())) == []
    p = _toml(tmp_path, "[console.courses]\n")
    e = TomlEdit(p)
    assert C.set_course(e, "lab201", ignore=True) == ["lab201 is not a course"]
    e.save(lambda d: True)
    assert not C.courses_from(TomlEdit(p).data).is_course("lab201")
    e = TomlEdit(p)
    assert C.set_course(e, "lab201", ignore=False) == ["lab201 is a course again"]
    e.save(lambda d: True)
    assert C.courses_from(TomlEdit(p).data).is_course("lab201")


def test_bad_settings_are_said_and_left_out():
    c = C.courses_from({"console": {"courses": {
        "pattern": "([", "term_start": "fall", "min_students": -1, "names": "yes", "colour": 1,
        "groups": {"bio 1": {}, "demo101": {"instructors": ["ok", "not a netid"], "x": 1}}}}})
    text = " ".join(c.problems)
    for word in ("pattern", "term_start", "min_students", "names", "colour", "bio 1",
                 "not a netid", "x: unknown"):
        assert word in text, word
    assert c.listed["demo101"].instructors == ("ok",) and c.min_students == 0 and not c.names
    assert not c.is_course("demo202")            # a broken pattern names no course
    c = C.courses_from({"console": {"courses": {"groups": {"lab201": {"course": "false"}}}}})
    assert not c.is_course("lab201") and "taken as false" in c.problems[0]


def test_instructors_are_faculty_in_the_group_and_those_listed_less_the_excluded():
    groups = {"faculty": {"prof1", "prof2", "prof3"}, "demo101": {"prof1", "prof3", "s1"},
              "demo202": {"s2"}}
    c = C.courses_from({"console": {"courses": {"groups": {
        "demo101": {"exclude": ["prof3"]}, "demo202": {"instructors": ["prof2"]}}}}})
    members = lambda g: groups.get(g, set())          # noqa: E731
    assert C.instructors(c, "demo101", members) == {"prof1"}
    assert C.instructors(c, "demo202", members) == {"prof2"}
    known = ["chem", "demo101", "demo202", "faculty", "prof1$"]
    assert C.courses_of(c, "prof2", known, members) == ["demo202"]
    assert C.courses_of(c, "prof3", known, members) == []


def test_this_terms_students():
    groups = {"faculty": {"prof1"}, "demo101": {"prof1", "s1", "s2", "s3", "s4", "s5"}}
    c = C.courses_from(TERM)
    since = datetime(2026, 8, 24)
    joined = {"s3": "2026-09-01T10:00:00", "s4": "2026-01-10T10:00:00"}
    r = C.roster(c, "demo101", lambda g: groups.get(g, set()), lambda users: {"s1"}, joined, since)
    assert r["instructors"] == {"prof1"}
    assert r["this_term"] == {"s1", "s3"}            # active, or joined this term
    assert r["left_out_earlier"] == {"s4"}           # nomad saw them join last term
    assert r["left_out_unknown"] == {"s2", "s5"}     # no join date, not active


# --- editing nomad.toml ------------------------------------------------------------------

def _toml(tmp_path, text="[general]\ndata_dir = \"/x\"\n"):
    p = tmp_path / "nomad.toml"
    p.write_text(text)
    return p


def test_setup_and_course_changes_write_tables_that_read_back(tmp_path):
    p = _toml(tmp_path)
    e = TomlEdit(p)
    assert "courses are on" in C.set_settings(e, term_start="2026-08-24")[0]
    e.save(lambda d: True)
    e = TomlEdit(p)
    assert C.set_course(e, "demo202", add="Prof2", every_term=True) == \
        ["prof2 teaches demo202 (every term)"]
    e.save(lambda d: True)
    e = TomlEdit(p)
    C.set_course(e, "demo101", name="Intro  Data")
    C.set_course(e, "demo101", exclude="prof3", every_term=True)
    e.save(lambda d: True)
    c = C.courses_from(TomlEdit(p).data)
    assert c.enabled and c.term_start == date(2026, 8, 24)
    assert c.listed["demo202"].instructors == ("prof2",)
    assert c.listed["demo101"].name == "Intro Data" and c.listed["demo101"].exclude == ("prof3",)
    e = TomlEdit(p)
    assert C.set_course(e, "demo202", add="prof2", every_term=True) == []    # already so
    assert C.set_course(e, "demo202", remove="prof2") == ["prof2 no longer listed as teaching demo202"]
    e.save(lambda d: True)
    assert "demo202" not in C.courses_from(TomlEdit(p).data).listed
    e = TomlEdit(p)
    assert C.set_settings(e, term_start="2027-01-11") == ["term_start = '2027-01-11'"]
    e.save(lambda d: True)
    assert C.courses_from(TomlEdit(p).data).term_start == date(2027, 1, 11)


def test_edits_refuse_what_they_cannot_do_safely(tmp_path):
    e = TomlEdit(_toml(tmp_path, '[console]\ncourses = { term_start = "2026-08-24" }\n'))
    with pytest.raises(C.CourseError):
        C.set_course(e, "demo101", add="prof1", every_term=True)
    with pytest.raises(C.CourseError):
        C.set_settings(e, names=True)
    e = TomlEdit(_toml(tmp_path))
    for bad in ({"add": "a@b.edu"}, {"exclude": "two words"}):
        with pytest.raises(C.CourseError):
            C.set_course(e, "demo101", **bad)
    with pytest.raises(C.CourseError):
        C.set_course(e, "bio 101", add="prof1")
    with pytest.raises(C.CourseError):
        C.set_settings(e, term_start="next fall")


def test_settings_keep_what_is_there(tmp_path):
    p = _toml(tmp_path, "[console.courses]\nlegacy = true  # keep me\nterm_start = 2026-08-24\n"
                        'pattern = """\n^[a-z]+[0-9]{3}$"""\n')
    e = TomlEdit(p)
    assert C.set_settings(e, term_start="2026-08-24") == []            # a TOML date, the same day
    assert C.set_settings(e, names=True) == ["names = True"]
    assert "legacy = true  # keep me" in e.text
    with pytest.raises(C.CourseError):                                 # spread over lines
        C.set_settings(e, pattern="^x$")


# --- what an instructor sees -------------------------------------------------------------

def _db(tmp_path, habits=None, rows_from=datetime(2026, 8, 24), measured_from=None, until=NOW):
    """A site: prof1 teaches demo101 (s1-s4 students, old1 from before),
    readings every 30 minutes, 08:00-22:00, from rows_from to NOW."""
    conn = sqlite3.connect(tmp_path / "n.db")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, "
                 "cluster TEXT, collected_at TEXT, has_account INTEGER, "
                 "account_missing_since TEXT, first_seen TEXT)")
    for g, users in {"faculty": ["prof1"], "demo101": ["prof1", "s1", "s2", "s3", "s4", "old1"],
                     "prof1$": ["prof1", "s1"]}.items():
        for u in users:
            conn.execute("INSERT INTO group_membership VALUES (?,?,1,'web1',?,1,NULL,NULL)",
                         (u, g, NOW.isoformat()))
    habits = habits or {"s1": (8, 22), "s2": (12, 14), "s3": (20, 22)}
    t, ticks = rows_from, {}
    while t < until:
        if 8 <= t.hour < 22:
            on = []
            for u, (a, b) in habits.items():
                if a <= t.hour < b:
                    ticks[u] = ticks.get(u, 0) + (1800 * 100 if u == "s1" else 0)
                    on.append({"user": u, "session_type": "RStudio", "pid": 100 + len(u),
                               "start_time": u, "mem_mb": 1024, "cpu_ticks": ticks[u],
                               "age_seconds": 10 ** 7})
            D.update(conn, on, t, "web1", epoch=t.timestamp())
        t += timedelta(minutes=30)
    conn.commit()
    if measured_from is not None:          # as if CPU was measured only from then on
        conn.execute("UPDATE interactive_daily SET busy_seconds = 0, cpu_measured_seconds = 0, "
                     "cpu_seconds = 0 WHERE day < ?", (measured_from,))
        conn.commit()
    return conn


def test_an_instructor_sees_their_course_with_each_student_unnamed(tmp_path):
    conn = _db(tmp_path)
    d = courses(conn, TERM, netid="prof1", now=NOW)
    c, = d["courses"]
    assert c["group"] == "demo101" and c["instructors"] == ["prof1"]
    s = c["students"]
    assert s["this_term"] == 3 and s["left_out_unknown"] == 2     # s4 never on, old1
    assert s["used_a_server"] == 3 and s["not_yet"] == 0
    assert s["open_never_computing"] == 2                         # s2, s3 never computed
    rows = c["each_student"]
    assert [r["user"] for r in rows] == ["student 1", "student 2", "student 3"]
    first = rows[0]["open_per_week"]
    # s1: readings every 30 minutes, 08:00 to 21:30; the first after the night's
    # gap adds no time: 13.5 hours a day, every day, in each complete week since 24 Aug.
    assert first["typical"] == pytest.approx(94.5) and first["weeks"] == 7
    assert rows[0]["computing_per_week"]["typical"] == pytest.approx(94.5)
    assert rows[1]["computing_per_week"]["typical"] == 0
    assert c["interactive"]["weeks"][0]["students_on"] == 3
    grid = c["when"]["students_on"]
    assert grid[0][13] == pytest.approx(2.0, abs=0.01)            # s1 and s2 at 13:00 on Mondays
    assert grid[0][3] == 0 and grid[0][21] == pytest.approx(2.0, abs=0.01)
    named = courses(conn, TERM, netid="prof1", now=NOW, names=True)
    assert named["courses"][0]["each_student"][0]["user"] == "s1"
    assert courses(conn, TERM, netid="s1", now=NOW)["courses"] == []


def test_weeks_before_computing_was_measured_are_left_out_not_zero(tmp_path):
    conn = _db(tmp_path, measured_from="2026-10-05")
    c, = courses(conn, TERM, groups=["demo101"], now=NOW)["courses"]
    assert c["interactive"]["computing_measured_since"] == "2026-10-05"
    assert c["interactive"]["weeks_counted"] == {"open": 7, "computing": 1}
    # One whole week is not a range yet: an average week from the 8 days measured.
    est = c["each_student"][0]["computing_per_week"]
    assert est["estimated"] and est["days"] == 8 and est["low"] is None
    weeks = {w["week"]: w for w in c["interactive"]["weeks"]}
    assert weeks["2026-09-28"]["hours_computing"] is None
    assert weeks["2026-10-05"]["hours_computing"] > 0


def test_few_students_show_counts_only_when_the_site_says_so(tmp_path):
    conn = _db(tmp_path)
    cfg = {"console": {"courses": {"term_start": "2026-08-24", "min_students": 5}}}
    c, = courses(conn, cfg, groups=["demo101"], now=NOW)["courses"]
    assert "suppressed" in c and "each_student" not in c and c["students"]["this_term"] == 3
    c, = courses(conn, TERM, groups=["demo101"], now=NOW)["courses"]   # default 0: shown
    assert "each_student" in c


def test_courses_off_answer_nothing(tmp_path):
    conn = _db(tmp_path, habits={"s1": (8, 9)}, rows_from=datetime(2026, 10, 12))
    d = courses(conn, {}, netid="prof1", now=NOW)
    assert d["courses"] == [] and "Courses are off" in d["notes"][0]


def test_names_cut_short_by_ps_are_matched_when_only_one_fits():
    known = {"abcdefghij", "abcdefgxyz", "zyxwvutsrq"}
    assert resolve_name("zyxwvut+", known) == "zyxwvutsrq"
    assert resolve_name("abcdefg+", known) is None                # two fit
    assert resolve_name("short", known) is None and resolve_name("abcdefghij", known) == "abcdefghij"


# --- the weeks before: rebuilt from the raw snapshots ------------------------------------

def _raw(conn, t, users, server="web1"):
    for u in users:
        conn.execute("INSERT INTO interactive_sessions (timestamp, server_id, user, session_type, "
                     "pid, mem_mb) VALUES (?, ?, ?, 'RStudio', 1, 512)",
                     ((t + timedelta(seconds=2)).isoformat(), server, u))


def test_the_backfill_rebuilds_who_was_on_and_marks_it(tmp_path):
    from nomad.collectors.interactive_backfill import rebuild
    conn = sqlite3.connect(tmp_path / "n.db")
    conn.execute("CREATE TABLE interactive_sessions (id INTEGER PRIMARY KEY, timestamp TEXT, "
                 "server_id TEXT, user TEXT, session_type TEXT, pid INTEGER, mem_mb REAL)")
    conn.execute("CREATE TABLE collection_log (collector TEXT, started_at TEXT, success BOOLEAN)")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    conn.execute("INSERT INTO group_membership VALUES ('longername1', 'demo101')")
    t0 = datetime(2026, 9, 1, 10, 0)
    for i in range(6):                            # runs every 5 min; nobody on at the 3rd
        t = t0 + timedelta(minutes=5 * i)
        conn.execute("INSERT INTO collection_log VALUES ('interactive', ?, 1)", (t.isoformat(),))
        if i != 2:
            _raw(conn, t, ["ann", "longern+"] if i < 4 else ["ann"])
    # From 2 September the collector kept its own totals.
    D.update(conn, [], datetime(2026, 9, 2, 10, 0), "web1", epoch=datetime(2026, 9, 2, 10).timestamp())
    conn.commit()
    plan = rebuild(conn)
    s, = plan["servers"]
    assert s["days"] == 1 and s["people"] == 2 and s["readings_from"] == "the collector's run log"
    assert conn.execute("SELECT COUNT(*) FROM interactive_daily WHERE rebuilt = 1").fetchone()[0] == 0
    rebuild(conn, apply=True)
    day = {u: r for u, *r in conn.execute(
        "SELECT user, session_seconds, sessions, cpu_measured_seconds, rebuilt "
        "FROM interactive_daily WHERE day = '2026-09-01'")}
    # ann: on at readings 1, 3, 4, 5 after a known interval (the empty reading counted as one).
    assert day["ann"] == [4 * 300.0, 0, 0.0, 1]
    assert day["longername1"] == [2 * 300.0, 0, 0.0, 1]          # matched, and stopped after 4
    h = conn.execute("SELECT runs, people_sum, people_max, measured_runs, rebuilt FROM "
                     "interactive_hourly WHERE hour = '2026-09-01T10'").fetchone()
    assert h == (6, 2 + 2 + 0 + 2 + 1 + 1, 2, 0, 1)
    assert rebuild(conn, apply=True)["servers"] == []              # once only
    again = rebuild(conn, apply=True, redo=True)
    assert again["servers"][0]["days"] == 1
    assert conn.execute("SELECT COUNT(*) FROM interactive_daily WHERE day = '2026-09-01'"
                        ).fetchone()[0] == 2


def test_only_courses_are_shown_as_classes(tmp_path):
    conn = _db(tmp_path, habits={"s1": (8, 9)}, rows_from=datetime(2026, 10, 12))
    d = courses(conn, TERM, groups=["prof1$", "demo101"], now=NOW)
    assert [c["group"] for c in d["courses"]] == ["demo101"] and d["not_courses"] == ["prof1$"]


def test_hours_of_the_week_count_only_hours_that_have_passed(tmp_path):
    # Readings from Monday 08:00; asked on Monday at 12:00 the same week:
    # Monday 14:00 hasn't come round yet, so it is not an hour with nobody on.
    asked = datetime(2026, 10, 19, 12, 0)
    conn = _db(tmp_path, habits={"s1": (8, 22)}, rows_from=datetime(2026, 10, 12, 8), until=asked)
    c, = courses(conn, TERM, groups=["demo101"], now=asked)["courses"]
    grid = c["when"]["students_on"]
    assert grid[0][14] == pytest.approx(1.0)      # last Monday, once
    assert grid[0][9] == pytest.approx(1.0)       # last Monday and today: both on


def test_one_reading_stamped_over_seconds_is_one_reading(tmp_path):
    from nomad.collectors.interactive_backfill import rebuild
    conn = sqlite3.connect(tmp_path / "n.db")
    conn.execute("CREATE TABLE interactive_sessions (id INTEGER PRIMARY KEY, timestamp TEXT, "
                 "server_id TEXT, user TEXT, session_type TEXT, pid INTEGER, mem_mb REAL)")
    t0 = datetime(2026, 9, 1, 10, 0)
    for i in range(3):                    # no run log; each reading's two rows a second apart
        t = t0 + timedelta(minutes=5 * i)
        _raw(conn, t, ["ann"])
        _raw(conn, t + timedelta(seconds=1), ["bob"])
    conn.commit()
    rebuild(conn, apply=True)
    h = conn.execute("SELECT runs, people_sum, open_seconds FROM interactive_hourly").fetchone()
    assert h == (3, 6, 2 * 2 * 300.0)


def test_a_renamed_server_gets_only_its_own_runs(tmp_path):
    from nomad.collectors.interactive_backfill import rebuild
    conn = sqlite3.connect(tmp_path / "n.db")
    conn.execute("CREATE TABLE interactive_sessions (id INTEGER PRIMARY KEY, timestamp TEXT, "
                 "server_id TEXT, user TEXT, session_type TEXT, pid INTEGER, mem_mb REAL)")
    conn.execute("CREATE TABLE collection_log (collector TEXT, started_at TEXT, success BOOLEAN)")
    t0 = datetime(2026, 9, 1, 10, 0)
    for i in range(24):                   # two hours of runs; "local" the first, "web1" the second
        t = t0 + timedelta(minutes=5 * i)
        conn.execute("INSERT INTO collection_log VALUES ('interactive', ?, 1)", (t.isoformat(),))
        _raw(conn, t, ["ann"], server="local" if i < 12 else "web1")
    conn.commit()
    rebuild(conn, apply=True)
    rows = dict(conn.execute("SELECT server_id || ' ' || hour, runs FROM interactive_hourly"))
    assert rows == {"local 2026-09-01T10": 12, "web1 2026-09-01T11": 12}


def test_the_backfill_runs_on_the_server_not_on_the_hub(tmp_path):
    from nomad.collectors.interactive_backfill import rebuild
    conn = sqlite3.connect(tmp_path / "n.db")
    conn.execute("CREATE TABLE interactive_sessions (timestamp TEXT, server_id TEXT, user TEXT, "
                 "source_site TEXT)")
    assert "hub" in rebuild(conn, apply=True)["note"]


def test_students_are_ranked_by_computing_once_it_is_measured(tmp_path):
    # s1 computes all the time it is on; s3 only has sessions open, for longer.
    conn = _db(tmp_path, habits={"s1": (8, 12), "s3": (8, 22)}, measured_from="2026-10-08")
    c, = courses(conn, TERM, groups=["demo101"], now=NOW, names=True)["courses"]
    assert c["interactive"]["ranked_by"] == "computing"
    assert c["interactive"]["days_computing_measured"] == 5
    first, second = c["each_student"][:2]
    assert first["user"] == "s1" and second["user"] == "s3"
    # 08:00-11:30: three and a half hours a day after the first reading, every day.
    assert first["computing_per_week"]["typical"] == pytest.approx(3.5 * 7)
    assert second["open_per_week"]["typical"] > first["open_per_week"]["typical"]
    # Too few days measured: by time open, as before.
    (tmp_path / "b").mkdir()
    conn2 = _db(tmp_path / "b", habits={"s1": (8, 12), "s3": (8, 22)}, measured_from="2026-10-12")
    c2, = courses(conn2, TERM, groups=["demo101"], now=NOW, names=True)["courses"]
    assert c2["interactive"]["ranked_by"] == "open" and c2["each_student"][0]["user"] == "s3"


def test_a_course_change_needs_a_group_the_data_knows(tmp_path, monkeypatch):
    import nomad.actions.remote as remote
    import nomad.cli as cli
    from nomad.config.course_cli import _group_known
    db = tmp_path / "n.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    conn.execute("INSERT INTO group_membership VALUES ('s1', 'demo101')")
    conn.commit()
    monkeypatch.setattr(remote, "hub_settings", lambda: {"combined_db": tmp_path / "none.db"})
    monkeypatch.setattr(cli, "get_db_path", lambda config: db)

    class Ctx:
        obj = {"config": {}}
    assert _group_known(Ctx(), "demo101") is True
    assert _group_known(Ctx(), "GROUP") is False
    monkeypatch.setattr(cli, "get_db_path", lambda config: tmp_path / "missing.db")
    assert _group_known(Ctx(), "demo101") is None            # nothing to ask: not refused


# --- who teaches it this term (1.12.3) ---------------------------------------------------

def _members(groups):
    return lambda g: groups.get(g, set())


def _active(*who):
    return lambda group, candidates: {n: C.ACTIVE for n in candidates if n in who}


def test_one_faculty_member_teaches_it_several_must_have_been_active():
    groups = {"faculty": {"prof1", "prof2", "prof3"},
              "demo101": {"prof1", "s1"},                       # one: teaches it
              "demo202": {"prof1", "prof2", "prof3", "s2"},     # reused term after term
              "demo303": {"prof2", "prof3", "s3"},              # several, none active
              "demo404": {"s4"}}                                # no faculty member
    c, m, act = C.courses_from(TERM), _members(groups), _active("prof2")
    t = C.teaching(c, "demo101", m, act)
    assert t["instructors"] == {"prof1"} and t["why"]["prof1"] == C.ONLY and not t["undecided"]
    t = C.teaching(c, "demo202", m, act)
    assert t["instructors"] == {"prof2"} and t["left_out"] == {"prof1", "prof3"}
    assert t["why"] == {"prof2": C.ACTIVE}
    t = C.teaching(c, "demo303", m, _active())
    assert t["instructors"] == set() and t["undecided"]
    t = C.teaching(c, "demo404", m, act)
    assert t["instructors"] == set() and not t["undecided"]
    # Nobody known to be active (no activity given): several can't be told apart.
    assert C.teaching(c, "demo202", m)["undecided"]
    known = sorted(groups)
    assert C.courses_of(c, "prof1", known, m, act) == ["demo101"]
    # Activity is a person's, not a course's: active this term, prof2 counts in
    # every group of several they are in (what `nomad course list` is for).
    assert C.courses_of(c, "prof2", known, m, act) == ["demo202", "demo303"]
    assert C.courses_of(c, "prof3", known, m, act) == []


def test_what_the_site_says_wins_over_the_guess():
    groups = {"faculty": {"prof1", "prof2", "prof3", "staff1"},
              "demo202": {"prof1", "prof2", "prof3", "s2"},
              "demo303": {"prof2", "prof3", "s3"},
              "demo505": {"prof1", "staff1", "staff2", "s5"},
              "demo606": {"staff1", "prof2", "s6"}}
    cfg = {"console": {"courses": {"term_start": "2026-08-24", "not_teaching": ["Staff1", "staff2"],
                                   "groups": {"demo202": {"exclude": ["prof2"]},
                                              "demo303": {"instructors": ["prof3"]},
                                              "demo606": {"instructors": ["staff1"]}}}}}
    c, m = C.courses_from(cfg), _members(groups)
    assert c.not_teaching == ("staff1", "staff2") and not c.problems
    # The active one excluded: the other two, neither active, can't be told apart.
    t = C.teaching(c, "demo202", m, _active("prof2"))
    assert t["instructors"] == set() and t["undecided"] and t["faculty"] == {"prof1", "prof3"}
    # Added by name: teaches it; the other, not active, is said to be left out.
    t = C.teaching(c, "demo303", m, _active())
    assert t["instructors"] == {"prof3"} and t["why"]["prof3"] == C.ADDED
    assert t["left_out"] == {"prof2"} and not t["undecided"]
    # Staff in the faculty group teach nothing, active or not: one faculty member left.
    t = C.teaching(c, "demo505", m, _active("staff1"))
    assert t["instructors"] == {"prof1"} and t["why"]["prof1"] == C.ONLY
    # ...unless added to a course by name.
    t = C.teaching(c, "demo606", m, _active())
    assert t["instructors"] == {"staff1", "prof2"}
    assert t["why"] == {"staff1": C.ADDED, "prof2": C.ONLY}
    # Staff outside the faculty group are not students either.
    r = C.roster(c, "demo505", m, lambda users: set(users), {}, datetime(2026, 8, 24),
                 _active("staff1"))
    assert r["listed"] == {"s5"} and r["this_term"] == {"s5"}


def _hub(tmp_path):
    """A hub's combined database. The directory lists every group on both
    sites (web1, a teaching server; hpc1, a cluster).

    * demo202: students work on web1 (s1, s8, s9; old2, an earlier term's,
      ran one job on hpc1); prof3 used web1, prof10 too (a raw snapshot
      before web1's daily totals), prof2 ran jobs on hpc1 only, prof4 did
      nothing, prof11's only snapshot has no site;
    * demo303: prof5, prof6, neither active; demo404: no faculty member;
    * demo505: prof1 joined this term, prof7 used web1: who joined teaches it;
    * demo606: prof8 has a row from before 1.11 and one from a site added
      since (with a date), prof12: neither joined nor active;
    * demo707: students run jobs on hpc1 (s7): prof2 ran jobs there."""
    conn = sqlite3.connect(tmp_path / "hub.db")
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, "
                 "cluster TEXT, collected_at TEXT, has_account INTEGER, "
                 "account_missing_since TEXT, first_seen TEXT, source_site TEXT)")
    faculty = ["prof1", "prof2", "prof3", "prof4", "prof5", "prof6", "prof7", "prof8", "prof10",
               "prof11", "prof12"]
    rows = {"faculty": faculty,
            "demo202": ["prof2", "prof3", "prof4", "prof10", "prof11", "s1", "s2", "s8", "s9",
                        "old2"],
            "demo303": ["prof5", "prof6", "s3"], "demo404": ["s4"],
            "demo505": ["prof1", "prof7", "s5"], "demo606": ["prof8", "prof12", "s6"],
            "demo707": ["prof2", "prof4", "s7"]}
    for site in ("web1", "hpc1"):
        for g, users in rows.items():
            for u in users:
                joined = None
                if (u, g) == ("prof1", "demo505"):
                    joined = "2026-10-13 09:00:00"
                if (u, g, site) == ("prof8", "demo606", "hpc1"):
                    joined = "2026-10-13T09:00:00"
                conn.execute("INSERT INTO group_membership VALUES (?,?,1,?,?,1,NULL,?,?)",
                             (u, g, site, NOW.isoformat(), joined, site))
    D.ensure(conn)
    conn.execute("ALTER TABLE interactive_daily ADD COLUMN source_site TEXT")
    for u, site in (("prof3", "web1"), ("prof7", "web1"), ("s1", "web1"), ("s3", "web1"),
                    ("s4", "web1"), ("s5", "web1"), ("s8", "web1"), ("s9", "web1")):
        conn.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
                     "session_seconds, source_site) VALUES ('2026-10-01', 'srv', ?, '(any)', 1, "
                     "3600, ?)", (u, site))
    conn.execute("CREATE TABLE interactive_sessions (id INTEGER PRIMARY KEY, timestamp TEXT, "
                 "server_id TEXT, user TEXT, session_type TEXT, pid INTEGER, mem_mb REAL, "
                 "source_site TEXT)")
    conn.execute("INSERT INTO interactive_sessions (timestamp, server_id, user, source_site) "
                 "VALUES ('2026-09-01T10:00:00', 'srv', 'prof10', 'web1')")
    conn.execute("INSERT INTO interactive_sessions (timestamp, server_id, user, source_site) "
                 "VALUES ('2026-09-01T10:00:00', 'srv', 'prof11', NULL)")
    conn.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, submit_time TEXT, "
                 "source_site TEXT)")
    for i, u in enumerate(("prof2", "s7", "old2")):
        conn.execute("INSERT INTO jobs VALUES (?, ?, '2026-10-02T10:00:00', 'hpc1')", (str(i), u))
    conn.commit()
    return conn


def test_faculty_count_as_active_where_the_courses_students_work(tmp_path):
    from nomad.config.access import members_lookup
    from nomad.facts.courses import Activity
    conn = _hub(tmp_path)
    c = C.courses_from(TERM)
    _, members_of = members_lookup(conn)
    a = Activity(conn, datetime(2026, 8, 24), members_of, c)
    assert a.faculty("demo202", {"prof2", "prof3", "prof4", "prof10", "prof11"}) == \
        {"prof3": C.ACTIVE, "prof10": C.ACTIVE}
    assert a.faculty_sites["demo202"] == ["web1"]
    assert a.faculty("demo505", {"prof1", "prof7"}) == {"prof1": C.JOINED}     # joined wins
    assert a.faculty("demo606", {"prof8", "prof12"}) == {}       # a date beside an old row: unknown
    assert a.faculty("demo707", {"prof2", "prof4"}) == {"prof2": C.ACTIVE}
    assert a.faculty_sites["demo707"] == ["hpc1"]
    d = courses(conn, TERM, now=NOW)
    shown = {x["group"]: x for x in d["courses"]}
    assert set(shown) == {"demo202", "demo505", "demo707"}
    assert shown["demo202"]["instructors"] == ["prof10", "prof3"]
    assert shown["demo202"]["faculty_left_out"] == 3 and shown["demo202"]["active_on"] == ["web1"]
    # s1, s8, s9 and old2 (active, if elsewhere); s2 not active.
    assert shown["demo202"]["students"]["this_term"] == 4
    assert shown["demo505"]["instructors_why"] == {"prof1": "joined"}
    gone = {x["group"]: x for x in d["not_shown"]}
    assert set(gone) == {"demo303", "demo404", "demo606"}
    assert "none of them active" in gone["demo303"]["why"]
    assert "no faculty member" in gone["demo404"]["why"] and gone["demo404"]["students_this_term"] == 1
    assert [x["group"] for x in courses(conn, TERM, netid="prof3", now=NOW)["courses"]] == ["demo202"]
    # Research jobs on hpc1 don't make prof2 teach demo202: its students work on
    # web1, and one earlier student's job on hpc1 doesn't make hpc1 the course's.
    assert [x["group"] for x in courses(conn, TERM, netid="prof2", now=NOW)["courses"]] == ["demo707"]
    # Staff asking for a group by name see it, with why nobody teaches it.
    x, = courses(conn, TERM, groups=["demo303"], now=NOW)["courses"]
    assert x["instructors"] == [] and "none of them active" in x["no_instructor"]


def test_netids_match_whatever_their_case_and_join_stamps_whatever_their_form():
    groups = {"faculty": {"prof1", "prof2"}, "demo101": {"Prof1", "prof2", "s1"}}
    t = C.teaching(C.courses_from(TERM), "demo101", _members(groups), _active())
    assert t["faculty"] == {"prof1", "prof2"} and t["undecided"]      # not prof2 alone
    since = datetime(2026, 8, 24)
    for stamp, yes in (("2026-08-24", True), ("2026-08-24 09:00:00", True),
                       ("2026-08-24T00:00:00", True), ("2026-08-23T23:59:59", False), (None, False)):
        assert C.joined_this_term(stamp, since) is yes, stamp


def test_course_list_says_why_and_not_teaching_is_written(tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    _hub(tmp_path).close()
    cfg = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                          'term_start = "2026-08-24"\n')
    db = str(tmp_path / "hub.db")
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "list", "--db", db])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    line = next(x for x in lines if x.startswith("  demo202:"))
    assert "prof10 (active on web1), prof3 (active on web1)" in line
    assert "3 other faculty member(s) left out this term (prof11, prof2, prof4)" in line
    assert "prof1 (joined)" in next(x for x in lines if x.startswith("  demo505:"))
    tail = r.output[r.output.index("Shown to nobody"):]
    assert "demo303: 2 faculty member(s), none active this term (prof5, prof6)" in tail
    assert "demo404: no faculty member in the group" in tail
    # --mask: no group, NetID or site, in either command, text or JSON.
    masked = [CliRunner().invoke(cli, ["-c", str(cfg)] + args + ["--db", db, "--mask"]).output
              for args in (["course", "list"], ["facts", "course"], ["facts", "course", "--json"])]
    for out in masked:
        for name in ("prof", "demo", "web1", "hpc1", "hub.db"):
            assert name not in out, (name, out)
    assert "active this term" in masked[1]
    # Staff: not teaching anywhere.
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "not-teaching", "Prof3"])
    assert r.exit_code == 0 and "prof3 teaches no course" in r.output
    assert "not_teaching" not in cfg.read_text()                  # a dry run
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "not-teaching", "prof3", "--apply"])
    assert r.exit_code == 0, r.output
    assert C.courses_from(TomlEdit(cfg).data).not_teaching == ("prof3",)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "list", "--db", db])
    line = next(x for x in r.output.splitlines() if x.startswith("  demo202:"))
    assert "prof3" not in line and "prof10 (active on web1)" in line
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "not-teaching", "prof3", "--undo",
                                 "--apply"])
    assert r.exit_code == 0 and C.courses_from(TomlEdit(cfg).data).not_teaching == ()
    off = _toml(tmp_path, '[general]\ndata_dir = "/x"\n')
    r = CliRunner().invoke(cli, ["-c", str(off), "course", "not-teaching", "prof3"])
    assert r.exit_code != 0 and "courses are off" in r.output
    # Every course nobody teaches: said so.
    only = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                           'term_start = "2026-08-24"\npattern = "^demo303$"\n')
    r = CliRunner().invoke(cli, ["-c", str(only), "facts", "course", "--db", db])
    assert "no course has an instructor this term" in r.output and "demo303" in r.output



# --- decisions for a term, and what instructors say (1.12.4) ------------------------------

FALL, SPRING = "2026-08-24", "2027-01-11"


def test_decisions_hold_for_their_term(tmp_path):
    p = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                        f'term_start = "{FALL}"\n')
    e = TomlEdit(p)
    with pytest.raises(C.CourseError):                      # a term, or every term
        C.set_course(e, "demo202", exclude="prof2")
    assert C.set_course(e, "demo202", exclude="prof2", term=FALL) == \
        ["prof2 is not one of demo202's instructors (the term from 2026-08-24)"]
    assert C.set_course(e, "demo202", add="prof9", term=FALL) == \
        ["prof9 teaches demo202 (the term from 2026-08-24)"]
    e.save(lambda d: True)
    assert '[console.courses.groups.demo202.terms.2026-08-24]' in p.read_text()
    c = C.courses_from(TomlEdit(p).data)
    assert not c.problems
    assert c.decisions("demo202", FALL) == ({"prof9"}, {"prof2"})
    assert c.decisions("demo202", SPRING) == (set(), set())          # a new term: nothing holds
    groups = {"faculty": {"prof1", "prof2"}, "demo202": {"prof1", "prof2", "s1"}}
    t = C.teaching(c, "demo202", _members(groups), _active("prof2"), term=FALL)
    assert t["instructors"] == {"prof9", "prof1"}                   # prof1: the one left
    t = C.teaching(c, "demo202", _members(groups), _active("prof2"), term=SPRING)
    assert t["instructors"] == {"prof2"}                            # the guess again
    # The same decision for every term is one more; remove takes both.
    e = TomlEdit(p)
    assert C.set_course(e, "demo202", exclude="prof2", term=FALL, every_term=True) == [
        "prof2 is not one of demo202's instructors (every term)"]
    e.save(lambda d: True)
    c = C.courses_from(TomlEdit(p).data)
    assert c.listed["demo202"].exclude == ("prof2",) and c.decisions("demo202", SPRING)[1] == {"prof2"}
    e = TomlEdit(p)
    assert C.set_course(e, "demo202", remove="prof9", term=FALL) == \
        ["prof9 no longer listed as teaching demo202"]
    e.save(lambda d: True)
    assert C.courses_from(TomlEdit(p).data).decisions("demo202", FALL) == (set(), {"prof2"})
    bad = C.courses_from({"console": {"courses": {"groups": {"demo202": {"terms": {
        "fall": {"exclude": ["x"]}, "20260824": {"exclude": ["z"]},
        FALL: {"exclude": ["y"], "colour": 1}}}}}}})
    text = " ".join(bad.problems)
    assert "fall" in text and "20260824" in text and "colour" in text
    assert bad.decisions("demo202", FALL)[1] == {"y"}


def test_a_terms_decisions_override_every_terms_and_leave_them_alone(tmp_path):
    p = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                        f'term_start = "{FALL}"\n\n[console.courses.groups."demo202"]\n'
                        'instructors = ["alice"]\nexclude = ["bob"]\n')
    e = TomlEdit(p)
    C.set_course(e, "demo202", exclude="alice", term=FALL)
    C.set_course(e, "demo202", add="bob", term=FALL)
    e.save(lambda d: True)
    c = C.courses_from(TomlEdit(p).data)
    assert c.listed["demo202"].instructors == ("alice",) and c.listed["demo202"].exclude == ("bob",)
    assert c.decisions("demo202", FALL) == ({"bob"}, {"alice"})
    assert c.decisions("demo202", SPRING) == ({"alice"}, {"bob"})       # next term, as before
    # A comment in the term's table stays when another change leaves it alone.
    text = p.read_text().replace('exclude = ["alice"]', 'exclude = ["alice"]  # per the chair')
    p.write_text(text)
    e = TomlEdit(p)
    C.set_course(e, "demo202", name="Econometrics")
    e.save(lambda d: True)
    assert "# per the chair" in p.read_text()
    # Keys set inside another table from the group's: refused, not lost.
    p.write_text(p.read_text() + '\n[console.courses.groups."demo303"]\n'
                                 f'terms."{SPRING}".instructors = ["future"]\n')
    with pytest.raises(C.CourseError):
        C.set_course(TomlEdit(p), "demo303", name="Something")


def test_the_calendar_says_when_term_start_is_out_of_date():
    from nomad.config.course_cli import _term_note
    c = C.courses_from({"console": {"courses": {"term_start": FALL}}})
    assert _term_note(c, date(2026, 10, 14)) is None
    note = _term_note(c, date(2027, 1, 12))
    assert "2027-01-04" in note and "--term-start" in note


def test_what_instructors_say_comes_before_the_guess():
    groups = {"faculty": {"prof1", "prof2", "prof3", "staff1"},
              "demo202": {"prof1", "prof2", "prof3", "s2"},
              "demo101": {"prof1", "s1"}}
    m = _members(groups)
    cfg = {"console": {"courses": {"term_start": FALL, "not_teaching": ["staff1"],
                                   "groups": {"demo202": {"terms": {FALL: {"exclude": ["prof3"]}}}}}}}
    c = C.courses_from(cfg)
    # prof1 said yes: only those who did, though prof2 was active.
    t = C.teaching(c, "demo202", m, _active("prof2"), {"prof1": True})
    assert t["instructors"] == {"prof1"} and t["why"]["prof1"] == C.CONFIRMED
    assert t["left_out"] == {"prof2"}
    # prof2 said no: that says nothing about prof1, who wasn't active either.
    t = C.teaching(c, "demo202", m, _active("prof2"), {"prof2": False})
    assert t["instructors"] == set() and t["undecided"] and t["said_no"] == {"prof2"}
    t = C.teaching(c, "demo202", m, _active("prof1", "prof2"), {"prof2": False})
    assert t["instructors"] == {"prof1"} and t["why"]["prof1"] == C.ACTIVE
    # nomad.toml wins: prof3 excluded this term, whatever they said; staff never count.
    t = C.teaching(c, "demo202", m, _active(), {"prof3": True, "staff1": True})
    assert "prof3" not in t["instructors"] and "staff1" not in t["instructors"]
    # The one faculty member said no: nobody, and said so.
    t = C.teaching(c, "demo101", m, None, {"prof1": False})
    assert t["instructors"] == set() and not t["undecided"] and t["said_no"] == {"prof1"}
    # Someone who isn't a faculty member of the group can't say yes.
    t = C.teaching(c, "demo101", m, None, {"s1": True})
    assert t["instructors"] == {"prof1"}


def test_answers_are_kept_beside_the_hub_database(tmp_path):
    import os
    import stat

    from nomad.config import course_answers as A
    path = A.path_beside(tmp_path / "combined.db")
    assert A.load(path, FALL) == {} and A.listing(path) == []
    A.record(path, FALL, "demo202", "Prof1", True, now=NOW)
    A.record(path, FALL, "demo202", "prof2", False, now=NOW)
    A.record(path, SPRING, "demo202", "prof2", True, now=NOW)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert A.load(path, FALL) == {"demo202": {"prof1": True, "prof2": False}}
    assert A.load(path, SPRING) == {"demo202": {"prof2": True}}
    A.record(path, FALL, "demo202", "prof2", None)
    assert A.load(path, FALL) == {"demo202": {"prof1": True}}
    assert len(A.listing(path)) == 2 and len(A.listing(path, FALL)) == 1
    os.chmod(path, 0o644)                                    # widened by hand: narrowed again
    A.record(path, FALL, " demo202 ", "PROF3 ", True)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert A.load(path, FALL)["demo202"]["prof3"] is True
    with pytest.raises(A.AnswersError):
        A.record(path, "20260824", "demo202", "prof3", True)
    junk = tmp_path / "junk" / A.FILE
    junk.parent.mkdir()
    junk.write_text("not a database")
    with pytest.raises(A.AnswersError):
        A.load(junk, FALL)
    odd = tmp_path / "a#b?c"                                  # a path with URI characters
    A.record(A.path_beside(odd / "combined.db"), FALL, "demo202", "prof1", True)
    assert A.load(A.path_beside(odd / "combined.db"), FALL) == {"demo202": {"prof1": True}}


def test_faculty_are_asked_about_their_course_groups_and_their_answers_count(tmp_path):
    from nomad.config import course_answers as A
    from nomad.facts.courses import may_answer, questions
    conn = _hub(tmp_path)
    cfg = {"console": {"courses": {"term_start": FALL, "not_teaching": ["prof7"],
                                   "groups": {"demo707": {"instructors": ["prof4"]}}}}}
    # prof2: in demo202 (students this term) and demo707 (students on hpc1).
    asked = {q["group"]: q for q in questions(conn, cfg, "prof2", now=NOW)}
    assert set(asked) == {"demo202", "demo707"}
    assert asked["demo202"]["answer"] is None and not asked["demo202"]["teaches"]
    assert asked["demo707"]["teaches"] and asked["demo707"]["why"] == C.ACTIVE
    # prof4 is added to demo707 by name: not asked there.
    assert {q["group"] for q in questions(conn, cfg, "prof4", now=NOW)} == {"demo202"}
    assert may_answer(conn, cfg, "prof2", "demo202", NOW) == (True, "")
    assert may_answer(conn, cfg, "s1", "demo202", NOW)[0] is False          # a student
    assert may_answer(conn, cfg, "prof7", "demo505", NOW)[0] is False       # staff
    assert may_answer(conn, cfg, "prof4", "demo707", NOW)[0] is False       # already added
    assert may_answer(conn, cfg, "prof2", "faculty", NOW)[0] is False       # not a course
    # prof2 says yes to demo202: they teach it, and only they.
    path = A.path_beside(tmp_path / "hub.db")
    A.record(path, FALL, "demo202", "prof2", True)
    said = A.load(path, FALL)
    c, = [x for x in courses(conn, cfg, now=NOW, answers=said)["courses"] if x["group"] == "demo202"]
    assert c["instructors"] == ["prof2"] and c["instructors_why"] == {"prof2": "confirmed"}
    assert [x["group"] for x in courses(conn, cfg, netid="prof2", now=NOW, answers=said)["courses"]] \
        == ["demo202", "demo707"]
    assert courses(conn, cfg, netid="prof3", now=NOW, answers=said)["courses"] == []
    asked = {q["group"]: q for q in questions(conn, cfg, "prof2", said, now=NOW)}
    assert asked["demo202"]["answer"] is True and asked["demo202"]["why"] == C.CONFIRMED


def test_course_answer_and_answers_on_the_command_line(tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    _hub(tmp_path).close()
    cfg = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                          f'term_start = "{FALL}"\n')
    db = str(tmp_path / "hub.db")
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(cfg), "course", *a])   # noqa: E731
    r = run("answer", "demo202", "s1", "--yes", "--db", db)
    assert r.exit_code != 0 and "faculty members" in r.output
    r = run("answer", "demo202", "prof2", "--yes", "--db", db)
    assert r.exit_code == 0, r.output
    r = run("answers", "--db", db)
    assert "demo202: prof2 teaches it" in r.output
    assert "prof2" not in run("answers", "--db", db, "--mask").output
    r = run("list", "--db", db)
    line = next(x for x in r.output.splitlines() if x.startswith("  demo202:"))
    assert line.startswith("  demo202: prof2 (said so);") and "left out this term" in line
    r = run("answer", "demo202", "prof2", "--no", "--db", db)
    line = next(x for x in run("list", "--db", db).output.splitlines()
                if x.startswith("  demo202:"))
    assert "1 said they don't teach it (prof2)" in line
    # A decision for this term, shown as such; a new term_start says what stops applying.
    r = run("add", "demo202", "prof4", "--apply")
    assert r.exit_code == 0 and "(the term from 2026-08-24)" in r.output
    line = next(x for x in run("list", "--db", db).output.splitlines()
                if x.startswith("  demo202:"))
    assert "prof4 (added this term)" in line
    r = run("setup", "--term-start", SPRING)
    assert "A new term: decisions for the term from 2026-08-24 (1 course(s)" in r.output
    # Unset, the calendar's term is the one that ends.
    cal = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n')
    r = CliRunner().invoke(cli, ["-c", str(cal), "course", "setup", "--term-start", "2099-01-05"])
    assert "A new term: decisions for the term from" in r.output



def test_an_unreadable_answers_file_is_said_and_masked(tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    from nomad.config import course_answers as A
    _hub(tmp_path).close()
    A.path_beside(tmp_path / "hub.db").write_text("not a database")
    cfg = _toml(tmp_path, '[general]\ndata_dir = "/x"\n\n[console.courses]\n'
                          f'term_start = "{FALL}"\n')
    db = str(tmp_path / "hub.db")
    for args in (["course", "list"], ["facts", "course"]):
        r = CliRunner().invoke(cli, ["-c", str(cfg)] + args + ["--db", db, "--mask"])
        assert r.exit_code == 0 and "course_answers.db: can't read it" in r.output
        assert str(tmp_path) not in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "course", "answers", "--db", db])
    assert r.exit_code != 0 and "can't read it" in r.output
