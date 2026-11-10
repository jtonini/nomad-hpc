# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.39.0: a weekly email to each person about their own use (nomad.notices):
who gets one, what it says, never the same thing twice, review first,
course-only accounts left out, a person's own choice to stop it."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from click.testing import CliRunner

from nomad import notices as N
from nomad.edu.insights import Issue, UsageStats, UserInsights

NOW = datetime.now().replace(microsecond=0)
CONFIG = {"console": {"courses": {}, "labs": {"group_pattern": "{netid}$"}},
          "notices": {"review_to": ["rc@example.edu"], "mail_domain": "example.edu",
                      "send_from": "Research Computing <rc@example.edu>", "link": "https://nomad.example.edu",
                      "signature": ["Research Computing", "Example University"], "contact": "Questions: rc@example.edu"}}


def utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def world(tmp_path):
    """A combined database: login-node flags, finished jobs, groups."""
    db = tmp_path / "combined.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE per_user_alert (hostname TEXT, role TEXT, username TEXT, rule_id TEXT, rule_type TEXT, "
              "threshold_value REAL, sustained_for_seconds INTEGER, command TEXT, cmdline TEXT, "
              "peak_cpu_percent REAL, peak_memory_bytes INTEGER, process_session_id TEXT, last_seen TEXT, "
              "source_site TEXT)")
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, end_time TEXT)")
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT, collected_at TEXT, "
              "has_account INTEGER)")
    for user, ago in (("amy", 2), ("ben", 3), ("stu1", 1), ("cara", 20)):
        end = datetime.now() - timedelta(days=ago)
        c.execute("INSERT INTO per_user_alert VALUES ('spydur.cluster', 'headnode', ?, 'cpu_50pct_2min', 'cpu', 50, "
                  "3600, 'python3', 'python3 fit.py', 150, NULL, ?, ?, 'spydur')", (user, f"s-{user}", utc(end)))
    c.execute("INSERT INTO jobs VALUES ('1', 'dan', ?)", ((NOW - timedelta(days=1)).isoformat(),))
    c.execute("INSERT INTO jobs VALUES ('2', 'eve', ?)", ((NOW - timedelta(days=30)).isoformat(),))
    c.execute("INSERT INTO jobs VALUES ('3', 'root', ?)", ((NOW - timedelta(days=1)).isoformat(sep=' '),))
    stamp = NOW.isoformat(sep=" ")
    for user, group in (("stu1", "bio101"), ("amy", "bio101"), ("amy", "cpi$"), ("cpi", "cpi$"),
                        ("ben", "chem201"), ("ben", "faculty")):
        c.execute("INSERT INTO group_membership VALUES (?, ?, 'spydur', ?, 1)", (user, group, stamp))
    c.commit()
    c.close()
    return db


def jobs_issue(**kw):
    base = {"dimension": "Memory Efficiency", "dimension_key": "memory", "affected_jobs": 7, "total_applicable": 9,
            "avg_score": 20.0, "severity": "critical", "trajectory": "stable", "directive": "mem",
            "suggested_value": 8192, "suggested_display": "8G", "current_display": "64 GB", "direction": "lower",
            "cluster": "spydur", "partition": "basic", "usage_stats": UsageStats(6.2, 5, 7, 4, 9, "GB"),
            "rationale": "Memory you ask for and leave unused keeps other jobs waiting."}
    base.update(kw)
    return Issue(**base)


@pytest.fixture
def insights(monkeypatch):
    issues = {"dan": [jobs_issue(),
                      jobs_issue(dimension_key="cpu", directive="cores", suggested_display="2", severity="medium"),
                      jobs_issue(dimension_key="time", directive="time", affected_jobs=3),
                      jobs_issue(dimension_key="gpu", direction="raise")]}

    def fake(db_path, username, days=90, config=None, **k):
        return UserInsights(username=username, job_count=9, window_days=days, issues=issues.get(username, []))
    import nomad.edu.insights as I
    monkeypatch.setattr(I, "user_insights", fake)
    return issues


def due(db, rec=None, config=CONFIG, now=None, **kw):
    s = N.settings(config)
    import sqlite3 as _s
    conn = _s.connect(db)
    try:
        return N.plan(conn, db, config, s, rec, now or NOW, **kw)
    finally:
        conn.close()


# -- who gets one, and what it says --------------------------------------------------------------

def test_who_gets_an_email_and_what_it_says(tmp_path, insights):
    db = world(tmp_path)
    p = due(db)
    got = {m["user"]: m for m in p["mail"]}
    # amy: a login-node finding (and a lab, though in a course too); dan: his jobs; ben: faculty
    assert set(got) == {"amy", "ben", "dan"}
    # stu1 is only in a course; cara's was three weeks ago; root is not a person's account; eve's jobs are old
    assert p["left"]["course only"] == 1 and p["left"]["not a person's account"] == 1
    amy = got["amy"]
    assert amy["review"] and amy["to"] == ["rc@example.edu"] and amy["address"] == "amy@example.edu"
    (e,) = amy["entries"]
    assert e.kind == "login_node" and e.title == "A long computation on the login node"
    assert e.lines[0].startswith("python3 on spydur, ") and "for 1 h" in e.lines[0] and "1.5 cores" in e.lines[0]
    assert "srun --pty" in " ".join(e.lines)
    # jobs: only a pattern, of the severities and size asked, of asking for more than used
    (j,) = got["dan"]["entries"]
    assert j.kind == "jobs" and j.title == "Your jobs ask for more memory than they use"
    assert j.lines[0] == "On spydur (basic): 7 of your last 9 jobs asked for 64 GB, and used about 6.2 GB."
    assert "#SBATCH --mem=8G" in j.lines
    s = N.settings(CONFIG)
    text = N.text_of(got["dan"], s)
    assert text.startswith("[Review: this would have gone to dan@example.edu.]\n\nHello,\n")
    assert "Your jobs\n---------\n* Your jobs ask for more memory than they use" in text
    assert "https://nomad.example.edu/me" in text and "Email me about my own use" in text
    assert text.rstrip().endswith("Questions: rc@example.edu\n\nResearch Computing\nExample University")
    assert not any(c in text for c in ("n't", "'re", "'ll", "'ve", "'d "))      # no contractions
    assert "the monitoring that Research Computing runs on the clusters" in text
    assert N.subject_of(got["dan"]) == "[review: for dan] Research computing: a note on your recent work"
    # only one person, on asking
    assert [m["user"] for m in due(db, only="dan")["mail"]] == ["dan"]
    # what it tells
    assert set(m["user"] for m in due(db, config={**CONFIG, "notices": {**CONFIG["notices"],
                                                                         "kinds": ["jobs"]}})["mail"]) == {"dan"}
    # course-only accounts too, when asked
    with_courses = {**CONFIG, "notices": {**CONFIG["notices"], "courses": True}}
    assert "stu1" in {m["user"] for m in due(db, config=with_courses)["mail"]}


def test_never_the_same_thing_twice_and_at_most_weekly(tmp_path, insights, monkeypatch):
    db = world(tmp_path)
    sent = []
    import nomad.mail as mail
    monkeypatch.setattr(mail, "send", lambda msg, cfg, timeout=15: sent.append(msg))
    s = N.settings(CONFIG)
    rec = N.open_db(tmp_path / "notices.db", write=True)
    conn = sqlite3.connect(db)
    p = N.send(conn, db, CONFIG, s, rec, NOW)
    assert sorted(m["status"] for m in p["mail"]) == ["sent"] * 3 and len(sent) == 3
    msg: EmailMessage = sent[0]
    assert msg["To"] == "rc@example.edu" and msg["From"] == "Research Computing <rc@example.edu>"
    # a day later: emailed lately
    assert N.send(conn, db, CONFIG, s, rec, NOW + timedelta(days=1), dry=True)["left"]["emailed lately"] == 3
    # a week later: nothing lately at all; dan ran jobs again, but his pattern was told within repeat_days
    assert N.send(conn, db, CONFIG, s, rec, NOW + timedelta(days=7), dry=True)["mail"] == []
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs VALUES ('8', 'dan', ?)", ((NOW + timedelta(days=6)).isoformat(),))
    p = N.send(conn, db, CONFIG, s, rec, NOW + timedelta(days=7), dry=True)
    assert p["mail"] == [] and p["left"]["nothing new"] == 1
    # the same login-node finding is never told again, even when it is still recent
    p = N.send(conn, db, CONFIG, s, rec, NOW + timedelta(days=1), dry=True, only="amy")
    assert p["mail"] == [] and p["left"]["emailed lately"] == 1
    rec.execute("UPDATE notices_mail SET at = ?", ((NOW - timedelta(days=8)).isoformat(),))
    rec.commit()
    assert N.send(conn, db, CONFIG, s, rec, NOW + timedelta(days=1), dry=True, only="amy")["left"]["nothing new"] == 1
    # after repeat_days, a pattern that still holds is told again
    later = NOW + timedelta(days=31)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs VALUES ('9', 'dan', ?)", ((later - timedelta(days=1)).isoformat(),))
    assert [m["user"] for m in N.send(conn, db, CONFIG, s, rec, later, dry=True)["mail"]] == ["dan"]
    # going live: what review saw is still news to the person
    live = {**CONFIG, "notices": {**CONFIG["notices"], "review_to": []}}
    sl = N.settings(live)
    p = N.send(conn, db, live, sl, rec, NOW + timedelta(hours=1), dry=True)
    assert sorted(m["user"] for m in p["mail"]) == ["amy", "ben", "dan"]
    assert {m["to"][0] for m in p["mail"]} == {"amy@example.edu", "ben@example.edu", "dan@example.edu"}
    assert not any(m["review"] for m in p["mail"]) and "[Review" not in N.text_of(p["mail"][0], sl)


def test_a_person_stops_it(tmp_path, insights):
    db = world(tmp_path)
    rec = N.open_db(tmp_path / "notices.db", write=True)
    assert N.wants_email(rec, "amy") and N.wants_email(None, "amy")
    N.set_email(rec, "amy", False, by="amy")
    assert not N.wants_email(rec, "amy")
    p = due(db, rec)
    assert "amy" not in {m["user"] for m in p["mail"]} and p["left"]["stopped"] == 1
    N.set_email(rec, "amy", True, by="jtonini")
    assert N.wants_email(rec, "amy") and N.choices(rec)["amy"][2] == "jtonini"
    with pytest.raises(N.NoticesError):
        N.set_email(rec, "Amy Smith", False)


def test_review_first_and_held(tmp_path):
    # without review_to in [notices] and none in report.toml: nothing is sent
    s = N.settings({"notices": {"mail_domain": "example.edu"}})
    assert s.held and any("review_to is not set" in p for p in s.problems)
    # report.toml's review_to holds them
    class Delivery:
        review_to = ["boss@example.edu"]
        mail_domain = "example.edu"
        send_from = "rc@example.edu"
        contact = None
        institution = "Example"
        branding = True

    class Report:
        delivery = Delivery()
    s = N.settings({"notices": {}}, Report())
    assert not s.held and s.review_to == ("boss@example.edu",)
    # an explicit [] sends to people; a broken address holds everything
    assert not N.settings({"notices": {"review_to": []}}).reviewing
    s = N.settings({"notices": {"review_to": ["not an address"]}})
    assert s.held and s.review_to == ()
    # the lab check's own review_to never decides these
    s = N.settings({"console": {"labs": {"check": {"review_to": []}}}, "notices": {}})
    assert s.held
    # a held send sends nothing
    st = N.status(N.settings({"notices": {}}), None)
    assert st["sending"] == "held"


def test_settings_and_what_config_check_says():
    s = N.settings({"notices": {"review_to": [], "every_days": 0, "kinds": ["jobs", "x"], "courses": "yes",
                                "jobs": {"min_jobs": 0, "severities": ["low"], "colour": 1},
                                "link": "nomad.example.edu", "greeting": "Hi there,", "skip_users": ["SVC1"]}})
    assert s.every_days == 7 and s.kinds == ("jobs",) and s.courses is False and s.jobs_min == 5
    assert s.jobs_severities == ("critical", "high") and s.link is None and s.greeting == "Hi there,"
    assert s.skip_users == ("svc1",)
    got = N.config_problems({"notices": {"review_to": [], "every_days": 0, "colour": 1}})
    assert any("every_days = 0" in p for p in got) and any("colour: not a setting" in p for p in got)
    assert N.config_problems({}) == [] and N.config_problems({"notices": {"review_to": []}}) == []
    # the lab check's link (to My lab) gives the Console's address
    assert N.settings({"console": {"labs": {"check": {"link": "https://nomad.example.edu/lab"}}},
                       "notices": {"review_to": []}}).link == "https://nomad.example.edu"
    assert N.db_path(N.Settings(), "/srv/nomad/combined.db").as_posix() == "/srv/nomad/notices.db"


def test_course_only(tmp_path):
    from nomad.config.access import access_from, members_lookup
    db = world(tmp_path)
    conn = sqlite3.connect(db)
    acc = access_from(CONFIG, log=False)
    look = members_lookup(conn)
    assert N.course_only(conn, acc, "stu1", look)
    assert not N.course_only(conn, acc, "amy", look)        # in a lab as well
    assert not N.course_only(conn, acc, "ben", look)        # faculty
    assert not N.course_only(conn, acc, "cpi", look)        # leads a lab
    assert not N.course_only(conn, acc, "dan", look)        # no groups known: emailed


# -- the commands ----------------------------------------------------------------------------------

def test_nomad_notices_commands(tmp_path, insights, monkeypatch):
    import nomad.cli as cli_mod
    import nomad.mail as mail
    db = world(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "{db}"\n[[hub.sites]]\nname = "spydur"\nhost = "spydur"\n'
                   '[console.courses]\n[console.labs]\ngroup_pattern = "{netid}$"\n'
                   '[notices]\nreview_to = ["rc@example.edu"]\nmail_domain = "example.edu"\n'
                   'send_from = "rc@example.edu"\nlink = "https://nomad.example.edu"\n')
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg, timeout=15: sent.append(msg))

    def nomad(*args):
        return CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "notices", *args])
    r = nomad()
    assert r.exit_code == 0, r.output
    assert "Emails go for review, to rc@example.edu." in r.output and "People who turned it off: 0." in r.output
    r = nomad("mail")
    assert r.exit_code == 0, r.output
    assert "would send: amy (1 on the login nodes) to rc@example.edu" in r.output
    assert "would send: dan (1 about jobs)" in r.output and "1 course only" in r.output
    assert "Nothing was sent: --send sends them (to review_to)." in r.output and not sent
    r = nomad("mail", "--counts")
    assert r.output == ("Emails: 3 would send (to rc@example.edu, for review).\n"
                        "Notes in them: 2 on the login nodes, 1 about jobs.\n"
                        "Not emailed: 1 course only, 1 not a person's account.\n"), r.output
    r = nomad("mail", "--user", "dan", "--text")
    assert "Subject: [review: for dan] Research computing: a note on your recent work" in r.output
    r = nomad("mail", "--send")
    assert r.exit_code == 0 and r.output.count("sent: ") == 3 and len(sent) == 3, r.output
    assert "Nothing due now." in nomad("mail").output
    assert "3 for review" in nomad().output
    # someone who asked by email
    r = nomad("set", "dan", "off")
    assert "would be no longer emailed" in r.output
    assert "no longer emailed" in nomad("set", "dan", "off", "--apply").output
    assert "not emailed already" in nomad("set", "dan", "off", "--apply").output
    assert "People who turned it off: 1." in nomad().output
    assert nomad("set", "Dan Smith", "off").exit_code != 0
    # the timer, printed
    r = nomad("timer", "--user", "nobody")
    assert "OnCalendar=Mon *-*-* 09:10" in r.output and "notices mail --send" in r.output, r.output
    assert "User=nobody" in r.output and "Install with --apply (as root)" in r.output
    assert "not as root" in nomad("timer", "--user", "root").output
    # config check
    cfg.write_text(cfg.read_text().replace('[notices]\n', '[notices]\nevery_days = "7"\n'))
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "config", "check"])
    assert "[notices] every_days = '7'" in r.output, r.output


def test_with_real_jobs(tmp_path):
    """user_insights over real-shaped jobs: the pattern of asking for 32 cores and using one."""
    from tests.test_user_insights_integration import _build_scoped_db
    db = tmp_path / "combined.db"
    _build_scoped_db(str(db))
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE per_user_alert (username TEXT, role TEXT, last_seen TEXT)")
    p = due(db, config={"notices": {"review_to": [], "mail_domain": "example.edu"}})
    (m,) = p["mail"]
    assert m["user"] == "bob" and m["to"] == ["bob@example.edu"]
    (e,) = m["entries"]                     # clusterA (5 jobs, critical); clusterB has 4, under min_jobs
    assert e.title == "Your jobs ask for more cores than they use" and e.lines[0].startswith("On clusterA (compute): 5 of")
    assert "#SBATCH --cpus-per-task=" in " ".join(e.lines)
    json.dumps([x.__dict__ for x in m["entries"]])
