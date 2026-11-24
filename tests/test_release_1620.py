# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.62.0: nomad's own databases read without reading them whole (a lone
MIN and a lone MAX), sizes in TB; the weekly note about asking for too much
time told only when it made a person's jobs wait, the value to ask for in
words, an array named once, and the email laid out (HTML with a plain copy)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from email import message_from_bytes

import pytest

from nomad import db_health as H
from nomad import notices as N
from nomad.edu.insights import Issue, UsageStats, UserInsights

NOW = datetime.now().replace(microsecond=0)
CONFIG = {"notices": {"review_to": ["rc@example.edu"], "mail_domain": "example.edu", "kinds": ["jobs"],
                      "send_from": "Research Computing <rc@example.edu>", "link": "https://nomad.example.edu",
                      "signature": ["Research Computing", "Example University"],
                      "contact": "Questions: rc@example.edu"}}
CONTRACTION = re.compile(r"\b[A-Za-z]+'(t|re|ll|ve|d|m)\b|\b(it|that|there|what|here|he|she|who)'s\b", re.I)


# -- nomad's own databases ------------------------------------------------------------------------

def test_a_reading_jumps_to_each_end_of_a_table(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (a)")
        c.executemany("INSERT INTO t VALUES (?)", ((i,) for i in range(2000)))
        c.execute("CREATE TABLE k (a PRIMARY KEY, b) WITHOUT ROWID")
    seen = []
    real = H._ro

    def traced(path):
        c = real(path)
        c.set_trace_callback(seen.append)
        return c
    monkeypatch.setattr(H, "_ro", traced)
    r = H.measure(db)
    assert [(x["name"], x["max_rowid"], x["min_rowid"]) for x in r["tables"]] == [("t", 2000, 1)]   # k: no rowid
    q = [s for s in seen if "MAX(rowid)" in s and '"t"' in s]
    assert q, seen
    with sqlite3.connect(db) as c:
        plan = " ".join(str(row[-1]) for row in c.execute("EXPLAIN QUERY PLAN " + q[0]))
    assert "SCAN" not in plan.replace("SCAN CONSTANT ROW", "") and "SEARCH" in plan, plan


def test_sizes_in_tb():
    assert H.size(112548.26e9) == "112.55 TB" and H.size(21245.02e9) == "21.25 TB"
    assert H.size(14.68e9) == "14.68 GB" and H.size(-2e12) == "-2.00 TB"


def test_status_says_when_there_is_no_growth_yet(tmp_path):
    from click.testing import CliRunner

    from nomad.db_cli import db_group
    site = tmp_path / "site.db"
    with sqlite3.connect(site) as c:
        c.execute("CREATE TABLE jobs (a)")
        H.ensure(c)                                     # the readings' tables, but no reading yet
    out = CliRunner().invoke(db_group, ["status"], obj={"config": {}, "db_path": str(site)})
    assert out.exit_code == 0, out.output
    assert "No growth yet" in out.output and "Growth needs" not in out.output


# -- an array once --------------------------------------------------------------------------------

def test_an_array_is_named_once_with_its_tasks():
    W = N._job_words
    assert W(["13212_23", "13212_6", "13212_3"], 15) == "array 13212 (3 tasks) and 12 more"
    assert W([f"13212_{k}" for k in range(15)], 15) == "array 13212 (15 tasks)"
    assert W(["12269_17", "12269_14", "13213_99", "13213_96", "13213_1"], 5) == \
        "array 12269 (2 tasks) and array 13213 (3 tasks)"
    assert W(["4521", "77_1", "4519"], 3) == "4521, array 77 (1 task) and 4519"
    assert W([], 0) == "" and W(["9"], 1) == "9"


# -- asking for too much time: only when it made them wait --------------------------------------

def jobs_db(tmp_path, mine_wait, others_wait, others=12, gpus=0):
    """kim's five jobs on hpc3's cpu partition asked for a year and ran 2 h;
    other people's 4-core jobs there, sent the same days, asked for about
    what they ran."""
    db = tmp_path / "combined.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, user_name TEXT, partition TEXT, state TEXT, "
              "submit_time TEXT, eligible_time TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER, "
              "req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER, wait_time_seconds INTEGER)")

    def job(jid, user, wait, asked, ran, days_ago, cpus=4, part="cpu", site="hpc3", g=0):
        sub = NOW - timedelta(days=days_ago)
        start = sub + timedelta(seconds=wait)
        end = start + timedelta(seconds=ran)
        c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (jid, site, user, part, "COMPLETED", sub.isoformat(sep=" "), sub.isoformat(), start.isoformat(),
                   end.isoformat(), cpus, g, asked, ran, wait))
    for k in range(5):
        job(f"90{k}", "kim", mine_wait, 365 * 86400, 7200, 2 + k * 0.2, g=gpus)
    for k in range(others):
        job(f"80{k:02d}", "pat" if k % 2 else "lee", others_wait, 3 * 3600, 2 * 3600, 2.5 + k * 0.05, g=gpus)
    # not like kim's: another partition, much bigger, asked for far more than they ran, long ago
    job("700", "ann", 1, 7200, 3600, 2.5, part="gpu")
    job("701", "ann", 1, 7200, 3600, 2.5, cpus=64)
    job("702", "ann", 1, 365 * 86400, 3600, 2.5)
    job("703", "ann", 1, 7200, 3600, 30)
    c.commit()
    c.close()
    return db


def time_issue(**kw):
    base = {"dimension": "time", "dimension_key": "time", "affected_jobs": 5, "total_applicable": 5,
            "avg_score": 10.0, "severity": "critical", "trajectory": "stable", "directive": "time",
            "direction": "lower", "current_value_typical": 365 * 86400.0, "current_display": "365-00:00:00",
            "suggested_display": "9-15:00:00", "suggested_value": 231 * 3600.0, "cluster": "hpc3",
            "partition": "cpu", "usage_stats": UsageStats(7800, 7000, 9000, 6000, 154 * 3600, "seconds"),
            "jobs": [{"job_id": f"90{k}"} for k in range(5)]}
    base.update(kw)
    return Issue(**base)


@pytest.fixture
def kim_asks_a_year(monkeypatch):
    def fake(db_path, username, days=90, config=None, **k):
        issues = [time_issue()] if username == "kim" else []
        return UserInsights(username=username, job_count=5, window_days=days, issues=issues)
    import nomad.edu.insights as I
    monkeypatch.setattr(I, "user_insights", fake)


def due(db, config=CONFIG):
    s = N.settings(config)
    conn = sqlite3.connect(db)
    try:
        return N.send(conn, db, config, s, None, NOW, dry=True, only="kim")
    finally:
        conn.close()


def test_told_when_their_jobs_waited_longer(tmp_path, kim_asks_a_year):
    db = jobs_db(tmp_path, mine_wait=3 * 3600 + 1200, others_wait=240)
    (m,) = due(db)["mail"]
    (e,) = m["entries"]
    assert e.title == "Your jobs ask for more time than they use"
    assert e.lines[0] == ("On hpc3 (cpu), 5 of your last 5 jobs asked for more time than they ran: they typically "
                          "asked for 365 days; most ran about 2 h 10 min, the longest 6 days 10 h.")
    assert e.lines[1] == ("They waited about 3 h 20 min to start (the median). Jobs of the same size on the same "
                          "partition, sent in the same days, that asked for about the time they ran waited about "
                          "4 min (the median of 12 jobs).")
    assert e.change == ["#SBATCH --time=9-15:00:00"]
    assert e.change_note == "That is 9 days 15 h: enough for the longest of these jobs, with some margin."
    assert e.jobs == "900, 901, 902, 903 and 904"
    assert not CONTRACTION.search(m["text"]) and not CONTRACTION.search(m["html"])


def test_not_told_when_it_cost_them_nothing(tmp_path, kim_asks_a_year):
    # a quiet cluster: theirs started as soon as the others
    assert not due(jobs_db(tmp_path, mine_wait=60, others_wait=60))["mail"]


@pytest.mark.parametrize("mine, others, n, why", [
    (20 * 60, 60, 12, "under waited_minutes longer"),
    (3 * 3600, 2 * 3600, 12, "under waited_times as long"),
    (3 * 3600, 60, 9, "under compare_min_jobs to go by"),
])
def test_not_told_on_too_little(tmp_path, kim_asks_a_year, mine, others, n, why):
    assert not due(jobs_db(tmp_path, mine_wait=mine, others_wait=others, others=n))["mail"], why


def test_gpu_jobs_are_compared_with_gpu_jobs(tmp_path, kim_asks_a_year):
    db = jobs_db(tmp_path, mine_wait=3 * 3600, others_wait=60, gpus=1)
    assert due(db)["mail"]
    with sqlite3.connect(db) as c:
        c.execute("UPDATE jobs SET req_gpus = 0 WHERE user_name != 'kim'")
    assert not due(db)["mail"]                       # no GPU job like theirs: nomad can't tell


def test_the_old_way_is_a_setting(tmp_path, kim_asks_a_year):
    off = {"notices": {**CONFIG["notices"], "jobs": {"time_if_waited": False}}}
    (m,) = due(jobs_db(tmp_path, mine_wait=60, others_wait=60), off)["mail"]
    (e,) = m["entries"]
    assert len(e.lines) == 1 and e.change == ["#SBATCH --time=9-15:00:00"]


def test_the_settings():
    s = N.settings(CONFIG)
    assert (s.time_if_waited, s.waited_minutes, s.waited_times, s.compare_min) == (True, 30, 2, 10)
    assert not s.problems
    bad = N.settings({"notices": {**CONFIG["notices"], "jobs": {"time_if_waited": "yes", "waited_times": 0,
                                                               "compare_min_jobs": 25, "waited_minutes": 90}}})
    assert (bad.time_if_waited, bad.waited_times, bad.compare_min, bad.waited_minutes) == (True, 2, 25, 90)
    assert len(bad.problems) == 2


# -- the email laid out -------------------------------------------------------------------------

def an_email():
    quick = N.Entry("jobs", "jobs:quick:hpc3", "Some of your jobs stopped within a minute",
                    ["On hpc3, 186 of your jobs this week stopped with an error within a minute of starting."],
                    group="jobs:quick", advice=list(N._QUICK_WHY), todo=list(N._QUICK_TODO),
                    jobs=N._job_words([f"13213_{k}" for k in range(180)] + [f"12269_{k}" for k in range(6)], 186))
    asked = N.Entry("jobs", "jobs:hpc3:cpu:time", "Your jobs ask for more time than they use",
                    ["On hpc3 (cpu), 15 of your last 15 jobs asked for more time than they ran: <script>x</script>."],
                    group="jobs:time", advice=[N._WHY["time"]], jobs="array 13212 (15 tasks)",
                    change=["#SBATCH --time=9-15:00:00"],
                    change_note="That is 9 days 15 h: enough for the longest of these jobs, with some margin.")
    return {"user": "kim", "address": "kim@example.edu", "review": True, "to": ["rc@example.edu"],
            "entries": [asked, quick], "problem": None}


def test_the_plain_copy():
    s = N.settings(CONFIG)
    text = N.text_of(an_email(), s)
    assert text.startswith("[Review: this would have gone to kim@example.edu.]\n\nHello,\n\n")
    assert "noticed 2 things in your recent work" in text
    # the jobs that stopped within a minute first; asking for too much last
    assert text.index("1. Some of your jobs stopped within a minute") < \
        text.index("2. Your jobs ask for more time than they use")
    assert "Jobs: array 13213 (180 tasks) and array 12269 (6 tasks)\n" in text
    assert "Ask for this instead:\n    #SBATCH --time=9-15:00:00\nThat is 9 days 15 h" in text
    assert "What to do: Look at the first lines" in text and "\nWhy: A job that stops this soon" in text
    assert "---" not in text and "\n* " not in text
    assert not CONTRACTION.search(text)
    assert N.subject_of(an_email()) == "[review: for kim] Research computing: 2 notes on your recent work"


def test_the_laid_out_copy():
    s = N.settings(CONFIG)
    page = N.html_of(an_email(), s)
    assert page.startswith("<!doctype html>") and "<script>" not in page and "&lt;script&gt;" in page
    assert page.index("1. Some of your jobs stopped within a minute") < page.index("2. Your jobs ask for more time")
    assert page.count("<h2") == 2 and page.count("What NØMAÐ saw") == 2
    assert re.search(r"<pre [^>]*>#SBATCH --time=9-15:00:00</pre>", page)
    assert '<a href="https://nomad.example.edu/me">https://nomad.example.edu/me</a>' in page
    # no colours of its own for text or background: a mail program's dark mode reads it too
    assert not re.search(r"(?<![-\w])(color|background)(-color)?\s*:", page)
    assert "Review: this would have gone to kim@example.edu." in page


def test_it_goes_as_both(tmp_path, kim_asks_a_year, monkeypatch):
    from nomad import mail
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg, timeout=15: sent.append(msg))
    db = jobs_db(tmp_path, mine_wait=3 * 3600, others_wait=60)
    s = N.settings(CONFIG)
    conn = sqlite3.connect(db)
    rec = N.open_db(tmp_path / "notices.db", write=True)
    try:
        p = N.send(conn, db, CONFIG, s, rec, NOW, only="kim")
    finally:
        conn.close()
    assert [m["status"] for m in p["mail"]] == ["sent"]
    (msg,) = sent
    back = message_from_bytes(msg.as_bytes())
    assert back.get_content_type() == "multipart/alternative"
    kinds = [part.get_content_type() for part in back.walk() if not part.is_multipart()]
    assert kinds == ["text/plain", "text/html"]
    html = [part for part in back.walk() if part.get_content_type() == "text/html"][0]
    assert "9-15:00:00" in html.get_payload(decode=True).decode()


def test_the_preview_files(tmp_path, kim_asks_a_year, monkeypatch):
    from click.testing import CliRunner

    from nomad import notices_cli as C
    db = jobs_db(tmp_path, mine_wait=3 * 3600, others_wait=60)
    conn = sqlite3.connect(db)
    monkeypatch.setattr(C, "_ctx", lambda ctx: (CONFIG, N.settings(CONFIG), conn, db, tmp_path / "notices.db"))
    out = CliRunner().invoke(C.notices_group, ["mail", "--user", "kim", "--html", str(tmp_path / "look")])
    assert out.exit_code == 0, out.output
    page = tmp_path / "look" / "kim.html"
    assert page.exists() and (page.stat().st_mode & 0o777) == 0o600
    assert "Ask for this instead" in page.read_text()
