# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.33.0: each term, a lab's PI says who is still in it (#60)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from nomad.condo.config import from_dict

NOW = datetime(2026, 9, 20, 10, 0)
CONFIG = {"console": {"labs": {"group_pattern": "{netid}$", "not_labs": ["installer$"],
                               "check": {"opens": ["09-15", "02-01", "06-15"], "mail_domain": "example.edu",
                                         "send_from": "RC <rc@example.edu>", "link": "https://nomad.example.edu/lab"}}}}
REPORT = from_dict({"report": {"labs": {"mail_domain": "example.edu", "review_to": ["rc@example.edu"]},
                               "clusters": {"spydur": {"condos": {
                                   "bukach": {"owner": "cbukach", "members": "group:cbukach$"},
                                   "parish": {"owner": "cparish", "members": "group:cparish$"},
                                   "yang1": {"owner": "myang", "members": "group:myang$"},
                                   "yangnolin": {"owner": ["myang", "knolin"], "members": "group:myang$,knolin$"}}}}}})


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)


def hub(path, now=NOW):
    """A combined.db as the hub has it: two sites' group readings and jobs."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            collected_at TEXT, has_account INTEGER, account_missing_since TEXT, first_seen TEXT, source_site TEXT);
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, submit_time TEXT, source_site TEXT);""")
    t = (now - timedelta(hours=1)).isoformat()
    rows = [("cparish", "cparish$", "spydur"), ("ann", "cparish$", "spydur"), ("bo", "cparish$", "spydur"),
            ("bo", "cparish$", "spiderweb"), ("cparish", "cparish$", "spiderweb"),
            ("cbukach", "cbukach$", "spydur"),
            ("kdonald", "kdonald$", "spydur"), ("dee", "kdonald$", "spydur"), ("eli", "kdonald$", "spydur"),
            ("myang", "myang$", "spydur"), ("yu", "myang$", "spydur"),
            ("knolin", "knolin$", "spydur"), ("kim", "knolin$", "spydur"),
            ("ann", "faculty", "spydur"), ("cparish", "faculty", "spydur"),
            ("installer", "installer$", "spydur"), ("zed", "installer$", "spydur")]
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, ?, 1, ?, ?, 1, ?)", [(u, g, s, t, s) for u, g, s in rows])
    j = (now - timedelta(days=10)).isoformat()
    jobs = [("1", "dee", "bukach"), ("2", "eli", "condos,bukach"), ("3", "cbukach", "bukach"),
            ("4", "kim", "yangnolin"), ("5", "kim", "yang1"), ("6", "ann", "parish"), ("7", "old", "parish")]
    conn.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, 'spydur')", [(i, u, p, j) for i, u, p in jobs])
    conn.execute("INSERT INTO jobs VALUES ('8', 'gone', 'parish', ?, 'spydur')",
                 ((now - timedelta(days=200)).isoformat(),))
    conn.commit()
    return conn


@pytest.fixture
def world(tmp_path):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    yield conn, rec, tmp_path
    conn.close()
    rec.close()


# -- settings and terms --------------------------------------------------------------------

def test_settings():
    s = L.settings(CONFIG, REPORT)
    assert s.opens == ("02-01", "06-15", "09-15") and s.remind_days == (14, 28) and s.closes_days == 42
    assert s.review_to == ("rc@example.edu",) and s.reviewing          # report.toml's, not given here
    s = L.settings({"console": {"labs": {"check": {**CONFIG["console"]["labs"]["check"], "review_to": []}}}}, REPORT)
    assert not s.reviewing                                               # [] here: to PIs
    bad = L.settings({"console": {"labs": {"check": {"opens": ["13-01", "02-30", "9-15"], "remind_days": [0],
                                                     "closes_days": "x", "review_to": ["nobody"], "oops": 1}}}})
    assert bad.opens == () and not bad.on and bad.review_broken and bad.reviewing
    assert len(bad.problems) >= 6
    assert not L.settings({}).on


def test_the_terms_check():
    s = L.settings(CONFIG)
    r = L.round_of(s, date(2026, 9, 20))
    assert (r.id, r.closes, r.next_opens) == ("2026-09-15", date(2026, 10, 27), date(2027, 2, 1))
    # closes a week before the next opens at the latest: unanswered labs are on the to-do a while
    long = L.settings({"console": {"labs": {"check": {"opens": ["02-01", "06-15", "09-15"], "closes_days": 100}}}})
    assert L.round_of(long, date(2026, 7, 1)).closes == date(2026, 9, 8)
    r = L.round_of(s, date(2026, 1, 10))                                 # before the year's first: last fall's
    assert r.id == "2025-09-15" and r.next_opens == date(2026, 2, 1)
    r = L.round_of(s, date(2026, 6, 20))
    assert r.closes == date(2026, 7, 27)
    short = L.settings({"console": {"labs": {"check": {"opens": ["09-15", "10-01"]}}}})
    assert L.round_of(short, date(2026, 9, 16)).closes == date(2026, 9, 24)     # never into the next term's


# -- who is asked ------------------------------------------------------------------------

def test_who_is_asked(world):
    conn, rec, _ = world
    got = {lab.group: lab for lab in L.labs(conn, CONFIG, REPORT, now=NOW)}
    assert set(got) == {"cparish$", "cbukach$", "kdonald$", "myang$", "knolin$"}   # not faculty, not installer$
    p = got["cparish$"]
    assert p.leaders == ("cparish",) and set(p.members) == {"ann", "bo"}       # the PI apart
    assert p.members["bo"] == {"spydur": True, "spiderweb": True}
    assert set(p.claims) == {"old"}                                          # used the condo lately, not in the group
    b = got["cbukach$"]
    assert b.members == {} and set(b.claims) == {"dee", "eli"}             # Donald's lab used Bukach's condo
    assert b.claims["eli"] == {"spydur": ["bukach"]}
    # a condo two labs share asks neither about the other's members; one of its own condos does
    assert got["myang$"].claims == {"kim": {"spydur": ["yang1"]}}
    assert "kim" not in got["knolin$"].claims and got["knolin$"].members.keys() == {"kim"}


# -- answers -----------------------------------------------------------------------------

def test_answers_fit_their_question(world):
    conn, rec, _ = world
    rnd = L.round_of(L.settings(CONFIG), NOW.date())
    lab = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    with pytest.raises(L.CheckError, match="one of here, left, not_mine"):
        L.record(rec, rnd, lab, "ann", "guest", by="cparish")
    with pytest.raises(L.CheckError, match="what to do with bo's data"):
        L.record(rec, rnd, lab, "bo", "left", by="cparish")
    with pytest.raises(L.CheckError, match="asked only of someone who has left"):
        L.record(rec, rnd, lab, "ann", "here", data="keep", by="cparish")
    with pytest.raises(L.CheckError, match="not someone"):
        L.record(rec, rnd, lab, "dee", "mine", by="cparish")
    with pytest.raises(L.CheckError, match="one of mine, guest"):
        L.record(rec, rnd, lab, "old", "here", by="cparish")
    row = L.record(rec, rnd, lab, "BO", "left", data="ask", note="  graduated\nin May ", by="cparish")
    assert (row["netid"], row["note"], row["kind"]) == ("bo", "graduated in May", "member")
    with pytest.raises(L.CheckError, match="2 still to answer"):
        L.submit(rec, rnd, lab, by="cparish")
    L.record(rec, rnd, lab, "ann", "here", by="cparish")
    L.record(rec, rnd, lab, "old", "guest", by="cparish")
    L.submit(rec, rnd, lab, by="cparish")
    log = [json.loads(r["what"]) for r in rec.execute("SELECT what FROM lab_check_log")]
    assert len(log) == 4 and "submitted" in log[-1]


def test_what_the_pi_sees(world):
    conn, rec, _ = world
    s = L.settings(CONFIG)
    spring = L.round_of(s, date(2026, 2, 10))
    lab = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    L.record(rec, spring, lab, "ann", "here", by="cparish")
    v = L.view(conn, rec, CONFIG, REPORT, "cparish$", now=NOW)
    assert v["round"] == "2026-09-15" and v["asked"] == 3 and v["answered"] == 0
    ann = next(q for q in v["questions"] if q["netid"] == "ann")
    assert ann["answer"] is None and ann["last_term"]["answer"] == "here" and ann["sites"] == ["spydur"]
    old = next(q for q in v["questions"] if q["netid"] == "old")
    assert old["kind"] == "claim" and old["used"] == [{"site": "spydur", "partitions": ["parish"]}]
    assert L.view(conn, rec, {"console": {"labs": {"group_pattern": "{netid}$"}}}, REPORT, "cparish$") is None


# -- ARC's to-do -------------------------------------------------------------------------

def test_the_to_do(world):
    conn, rec, _ = world
    rnd = L.round_of(L.settings(CONFIG), NOW.date())
    par = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    buk = L.lab_for(conn, CONFIG, REPORT, "cbukach$", now=NOW)
    L.record(rec, rnd, par, "bo", "left", data="keep", by="cparish")
    L.record(rec, rnd, par, "ann", "here", by="cparish")
    L.record(rec, rnd, par, "old", "mine", by="cparish")
    L.record(rec, rnd, buk, "dee", "guest", by="cbukach")
    L.record(rec, rnd, buk, "eli", "mine", by="cbukach")
    t = L.todo(conn, rec, CONFIG, REPORT, now=NOW)
    got = sorted((i["kind"], i["lab"], i["netid"], i["site"]) for i in t["items"])
    assert got == [("add", "cbukach$", "eli", "spydur"), ("add", "cparish$", "old", "spydur"),
                   ("data", "cparish$", "bo", ""),
                   ("remove", "cparish$", "bo", "spiderweb"), ("remove", "cparish$", "bo", "spydur")]
    data = next(i for i in t["items"] if i["kind"] == "data")
    assert data["data"] == "keep" and data["where"] == []             # no storage readings here
    # ARC takes bo out on spydur: the groups readings there no longer list him (as nomad counts a
    # member: gone from its readings for a couple of days, so a missed reading takes no one out)
    later = (NOW + timedelta(days=3)).isoformat()
    conn.execute("UPDATE group_membership SET collected_at = ? WHERE source_site = 'spydur' AND username != 'bo'",
                 (later,))
    conn.commit()
    t = L.todo(conn, rec, CONFIG, REPORT, now=NOW)
    assert ("remove", "spydur") not in {(i["kind"], i["site"]) for i in t["items"] if i["netid"] == "bo"}
    # marked: the data moved
    with pytest.raises(L.CheckError, match="note"):
        L.mark_done(rec, rnd.id, "cparish$", "bo", "data", by="jtonini", note="  ")
    L.mark_done(rec, rnd.id, "cparish$", "bo", "data", by="jtonini", note="moved to /scratch/parish/bo")
    t = L.todo(conn, rec, CONFIG, REPORT, now=NOW)
    assert "data" not in {i["kind"] for i in t["items"]}
    assert t["done"][0]["done"]["note"] == "moved to /scratch/parish/bo"


def test_put_back_after_leaving_is_asked_again_not_taken_out(world):
    conn, rec, _ = world
    rnd = L.round_of(L.settings(CONFIG), NOW.date())
    par = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    L.record(rec, rnd, par, "ann", "left", data="keep", by="cparish")
    assert ("remove", "ann") in {(i["kind"], i["netid"]) for i in L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"]}
    # ARC took her out; later she is back in the group (a new stretch of membership)
    later = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
    conn.execute("UPDATE group_membership SET first_seen = ? WHERE username = 'ann' AND group_name = 'cparish$'",
                 (later,))
    conn.commit()
    t = L.todo(conn, rec, CONFIG, REPORT, now=NOW)
    assert ("remove", "ann") not in {(i["kind"], i["netid"]) for i in t["items"]}


def test_a_changed_answer_is_to_do_again(world):
    conn, rec, _ = world
    rnd = L.round_of(L.settings(CONFIG), NOW.date())
    par = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    L.record(rec, rnd, par, "bo", "left", data="ask", by="cparish")
    L.mark_done(rec, rnd.id, "cparish$", "bo", "data", by="jtonini", note="waiting until December")
    assert "data" not in {i["kind"] for i in L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"]}
    import time
    time.sleep(1.1)                           # answers and marks are kept to the second
    L.record(rec, rnd, par, "bo", "left", data="remove", by="cparish")
    item = next(i for i in L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"] if i["kind"] == "data")
    assert item["data"] == "remove"


def test_a_condo_group_named_for_no_one(world):
    conn, rec, _ = world
    conn.execute("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                 "source_site) VALUES ('jsmith', 'chemlab$', 1, 'spydur', ?, 1, 'spydur'), "
                 "('sam', 'chemlab$', 1, 'spydur', ?, 1, 'spydur')",
                 ((NOW - timedelta(hours=1)).isoformat(),) * 2)
    conn.commit()
    report = from_dict({"report": {"clusters": {"spydur": {"condos": {
        "chem": {"owner": "jsmith", "members": "group:chemlab$"}}}}}})
    lab = L.lab_for(conn, CONFIG, report, "chemlab$", now=NOW)
    assert lab.leaders == ("jsmith",) and set(lab.members) == {"sam"}       # not "chemlab", and not jsmith
    assert L.led_by(conn, CONFIG, report, "jsmith", now=NOW) == ["chemlab$"]


def test_a_member_as_nomad_counts_one(world):
    conn, rec, _ = world
    t = (NOW - timedelta(hours=1)).isoformat()
    # two clusters read at one site: one's reading 40 days behind the other's is no longer read
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, 'cparish$', 1, ?, ?, 1, 'jonimitchell')",
                     [("cy", "ws-a", t), ("dot", "ws-b", (NOW - timedelta(days=40)).isoformat())])
    conn.commit()
    sites = L.by_site(conn)["cparish$"]
    assert sites["jonimitchell"]["cy"][0] and not sites["jonimitchell"]["dot"][0]
    # a whole site not read for a while still counts, as nomad.config.access counts it
    conn.execute("UPDATE group_membership SET collected_at = ? WHERE source_site = 'spiderweb'",
                 ((NOW - timedelta(days=40)).isoformat(),))
    conn.commit()
    from nomad.config.access import members_lookup
    _, members_of = members_lookup(conn)
    assert ("bo" in members_of("cparish$")) == L._inside(L.by_site(conn)["cparish$"]["spiderweb"]["bo"])


def test_added_only_where_they_used_the_condo(world):
    conn, rec, _ = world
    conn.execute("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                 "source_site) VALUES ('cparish', 'cparish$', 1, 'arachne', ?, 1, 'arachne')",
                 ((NOW - timedelta(hours=1)).isoformat(),))
    conn.commit()
    report = from_dict({"report": {"clusters": {
        "spydur": {"condos": {"parish": {"owner": "cparish", "members": "group:cparish$"}}},
        "arachne": {"condos": {"parish2": {"owner": "cparish", "members": "group:cparish$"}}}}}})
    rnd = L.round_of(L.settings(CONFIG), NOW.date())
    L.record(rec, rnd, L.lab_for(conn, CONFIG, report, "cparish$", now=NOW), "old", "mine", by="cparish")
    adds = {(i["netid"], i["site"]) for i in L.todo(conn, rec, CONFIG, report, now=NOW)["items"] if i["kind"] == "add"}
    assert adds == {("old", "spydur")}


def test_the_last_answer_given_counts(world):
    conn, rec, _ = world
    s = L.settings(CONFIG)
    par = L.lab_for(conn, CONFIG, REPORT, "cparish$", now=NOW)
    later = L.Round(id="2026-09-20", opened=date(2026, 9, 20), closes=date(2026, 10, 30), next_opens=date(2027, 2, 1))
    L.record(rec, later, par, "bo", "left", data="keep", by="cparish")       # kept under a later round's name
    import time
    time.sleep(1.1)
    L.record(rec, L.round_of(s, NOW.date()), par, "bo", "here", by="cparish")  # opens moved: the answer given last
    assert "bo" not in {i["netid"] for i in L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"]}


def test_a_relative_db_is_beside_combined(tmp_path, monkeypatch):
    monkeypatch.setattr("nomad.actions.remote.hub_settings", lambda target=None: {"combined_db": tmp_path / "c.db"})
    s = L.settings({"console": {"labs": {"check": {"db": "answers.db"}}}})
    assert L.db_path(s) == tmp_path / "answers.db"
    assert L.db_path(L.settings({})) == tmp_path / "lab_check.db"


def test_labs_that_didnt_answer(world):
    conn, rec, _ = world
    late = datetime(2026, 10, 28, 9, 0)
    t = L.todo(conn, rec, CONFIG, REPORT, now=late)
    assert {i["lab"] for i in t["items"] if i["kind"] == "no_answer"} == {
        "cparish$", "cbukach$", "kdonald$", "myang$", "knolin$"}
    assert not [i for i in L.todo(conn, rec, CONFIG, REPORT, now=NOW)["items"] if i["kind"] == "no_answer"]


# -- email --------------------------------------------------------------------------------

def test_the_emails(world, monkeypatch):
    conn, rec, _ = world
    sent = []
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: sent.append(msg))
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    opened = datetime(2026, 9, 15, 8, 0)
    plan = L.mail_plan(conn, rec, CONFIG, REPORT, now=opened)
    assert {e["lab"] for e in plan} == {"cparish$", "cbukach$", "kdonald$", "myang$", "knolin$"}
    assert all(e["review"] and e["to"] == ["rc@example.edu"] for e in plan)        # report.toml's review_to
    out = L.send(conn, rec, CONFIG, REPORT, now=opened)
    assert {e["status"] for e in out} == {"sent"} and len(sent) == 5
    body = next(m for m in sent if "cparish$" in m.get_content()).get_content()
    assert "would have gone to cparish@example.edu" in body and "https://nomad.example.edu/lab" in body
    assert sent[0]["Subject"].startswith("[review] Please confirm the members of")
    assert L.send(conn, rec, CONFIG, REPORT, now=opened) == []                     # nothing twice
    # a lab that answered everything gets no reminder
    rnd = L.round_of(L.settings(CONFIG), opened.date())
    buk = L.lab_for(conn, CONFIG, REPORT, "cbukach$", now=opened)
    L.record(rec, rnd, buk, "dee", "guest", by="cbukach")
    L.record(rec, rnd, buk, "eli", "guest", by="cbukach")
    reminders = L.mail_plan(conn, rec, CONFIG, REPORT, now=datetime(2026, 10, 20, 8, 0))
    assert "cbukach$" not in {e["lab"] for e in reminders}
    assert {e["kind"] for e in reminders} == {"remind28"}
    assert all(e["skipped"] == ["review-remind14"] for e in reminders)
    assert L.mail_plan(conn, rec, CONFIG, REPORT, now=datetime(2026, 11, 2)) == []   # after it closed
    # review_to cleared: what went for review didn't use up the PIs' own emails, the opening first
    live = {"console": {"labs": {**CONFIG["console"]["labs"],
                                 "check": {**CONFIG["console"]["labs"]["check"], "review_to": []}}}}
    plan = L.mail_plan(conn, rec, live, REPORT, now=datetime(2026, 10, 20, 8, 0))
    assert plan and {e["kind"] for e in plan} == {"open"} and not any(e["review"] for e in plan)
    assert next(e for e in plan if e["lab"] == "cparish$")["to"] == ["cparish@example.edu"]


def test_report_toml_that_doesnt_read_holds_the_emails(world):
    conn, rec, _ = world
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "check": {"opens": ["09-15"],
                                                                      "mail_domain": "example.edu"}}}}
    s = L.settings(cfg, None, report_problem="line 3: oops")
    assert s.reviewing and s.review_broken and any("nothing is sent" in p for p in s.problems)
    plan = L.mail_plan(conn, rec, cfg, None, now=NOW, report_problem="line 3: oops")
    assert plan and all(e["problem"].startswith("the emails are held") for e in plan)
    assert {e["status"] for e in L.send(conn, rec, cfg, None, now=NOW, report_problem="x")} == {"can't send"}
    # review_to given here: report.toml's isn't needed to hold them
    cfg["console"]["labs"]["check"]["review_to"] = ["rc@example.edu"]
    assert not L.settings(cfg, None, report_problem="x").review_broken


def test_no_address_no_email(world):
    conn, rec, _ = world
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "check": {"opens": ["09-15"], "review_to": []}}}}
    plan = L.mail_plan(conn, rec, cfg, None, now=NOW)
    assert plan and all(e["problem"].startswith("no address") for e in plan)
    assert {e["status"] for e in L.send(conn, rec, cfg, None, now=NOW, dry=True)} == {"can't send"}


# -- the command line ----------------------------------------------------------------------

def test_the_commands(world, monkeypatch):
    conn, rec, tmp = world
    from nomad import lab_check_cli as C
    from nomad.cli import cli
    monkeypatch.setattr(C, "_ctx", lambda ctx: (CONFIG, REPORT, L.settings(CONFIG, REPORT), conn,
                                                 tmp / "lab_check.db"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(C, "datetime", Frozen)
    monkeypatch.setattr(L, "datetime", Frozen)
    run = CliRunner()
    r = run.invoke(cli, ["lab", "check", "answer", "cparish$", "bo", "left", "--data", "remove", "--by", "cparish"])
    assert r.exit_code == 0, r.output
    assert "bo in cparish$: has left the lab; their data can be removed" in r.output
    r = run.invoke(cli, ["lab", "check", "answer", "cparish$", "bo", "left"])
    assert r.exit_code != 0 and "what to do with bo's data" in r.output
    r = run.invoke(cli, ["lab", "check", "show", "cparish$"])
    assert "bo" in r.output and "has left the lab" in r.output and "1 of 3 answered" in r.output
    r = run.invoke(cli, ["lab", "check", "todo"])
    assert "[remove] take bo out of cparish$ on spydur" in r.output and "[data] bo's data" in r.output
    r = run.invoke(cli, ["lab", "check", "done", "cparish$", "bo", "data", "--note", "removed"])
    assert r.exit_code == 0, r.output
    r = run.invoke(cli, ["lab", "check", "status"])
    assert "cparish$" in r.output and "1 of 3 answered" in r.output
    # one lab's email, printed, then sent on its own: the others stay due
    r = run.invoke(cli, ["lab", "check", "mail", "--lab", "cparish$", "--text"])
    assert r.exit_code == 0, r.output
    assert "Subject: [review] Please confirm the members of" in r.output
    assert "would have gone to cparish@example.edu" in r.output and "For your lab (cparish$), please tell us" in r.output
    assert "cbukach$" not in r.output and "A dry run" in r.output
    sent = []
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: sent.append(msg))
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    r = run.invoke(cli, ["lab", "check", "mail", "--lab", "cparish$", "--send"])
    assert r.exit_code == 0 and len(sent) == 1, r.output
    r = run.invoke(cli, ["lab", "check", "mail"])
    assert "cbukach$" in r.output and "cparish$" not in r.output
    r = run.invoke(cli, ["lab", "check", "mail", "--lab", "cparish$"])
    assert "Nothing due today for cparish$." in r.output
