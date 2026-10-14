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
    assert C.set_course(e, "demo202", add="Prof2") == ["prof2 teaches demo202"]
    e.save(lambda d: True)
    e = TomlEdit(p)
    C.set_course(e, "demo101", name="Intro  Data")
    C.set_course(e, "demo101", exclude="prof3")
    e.save(lambda d: True)
    c = C.courses_from(TomlEdit(p).data)
    assert c.enabled and c.term_start == date(2026, 8, 24)
    assert c.listed["demo202"].instructors == ("prof2",)
    assert c.listed["demo101"].name == "Intro Data" and c.listed["demo101"].exclude == ("prof3",)
    e = TomlEdit(p)
    assert C.set_course(e, "demo202", add="prof2") == []           # already so
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
        C.set_course(e, "demo101", add="prof1")
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
