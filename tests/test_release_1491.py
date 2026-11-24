# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.49.1: the lab check, going to PIs in the middle of a term.

A reminder waits remind_gap_days after the lab's last email (the same way:
for review, or to its PIs), so a lab first emailed late isn't reminded the
next day; `nomad lab check mail --counts` says how many, with no lab or name.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from nomad.condo.config import from_dict

OPENS = datetime(2026, 11, 2, 8, 0)
REPORT = from_dict({"report": {"labs": {"mail_domain": "example.edu", "review_to": ["rc@example.edu"]}}})


def config(**check):
    return {"console": {"labs": {"group_pattern": "{netid}$",
                                 "check": {"opens": ["11-02", "02-01", "06-15"], "mail_domain": "example.edu",
                                           "send_from": "RC <rc@example.edu>", **check}}}}


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)


@pytest.fixture
def world(tmp_path):
    conn = sqlite3.connect(tmp_path / "combined.db")
    conn.executescript("""
        CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            collected_at TEXT, has_account INTEGER, account_missing_since TEXT, first_seen TEXT, source_site TEXT);
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, submit_time TEXT, source_site TEXT);""")
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, ?, 1, 'hpc1', ?, 1, 'hpc1')",
                     [(u, g, "2099-01-01T00:00:00") for u, g in
                      (("kim", "kim$"), ("ann", "kim$"), ("pat", "pat$"), ("olive", "pat$"), ("lee", "pat$"))])
    conn.commit()
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    yield conn, rec, tmp_path
    conn.close()
    rec.close()


@pytest.fixture
def clock(monkeypatch):
    """L's datetime at a day of our choosing: what is sent is recorded then."""
    at = {"now": OPENS}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return at["now"]
    monkeypatch.setattr(L, "datetime", Frozen)
    sent = []
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: sent.append(msg))
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    return at, sent


def day(m, d):
    return datetime(2026, m, d, 8, 0)


def due(conn, rec, cfg, when):
    return sorted((e["lab"], e["kind"]) for e in L.mail_plan(conn, rec, cfg, REPORT, now=when))


def run(conn, rec, cfg, clock, when):
    clock[0]["now"] = when
    return sorted((e["lab"], e["kind"], e["status"]) for e in L.send(conn, rec, cfg, REPORT, now=when))


def test_going_to_pis_mid_term_opening_then_a_week_between(world, clock):
    conn, rec, _ = world
    review = config()                                  # report.toml's review_to: for review
    live = config(review_to=[])
    # turned on late (the 8th): the review copies, the opening and the 14-day reminder on its day
    assert run(conn, rec, review, clock, day(11, 8)) == [("kim$", "open", "sent"), ("pat$", "open", "sent")]
    assert due(conn, rec, review, day(11, 15)) == []
    assert run(conn, rec, review, clock, day(11, 16)) == [("kim$", "remind14", "sent"),
                                                          ("pat$", "remind14", "sent")]
    # review_to cleared on the 16th: the PIs' own, the opening first
    assert run(conn, rec, live, clock, day(11, 17)) == [("kim$", "open", "sent"), ("pat$", "open", "sent")]
    assert {m["To"] for m in clock[1][-2:]} == {"kim@example.edu", "pat@example.edu"}
    for d in (18, 20, 23):                             # before 1.49.1: the 14-day reminder on the 18th
        assert due(conn, rec, live, day(11, d)) == [], d
    assert run(conn, rec, live, clock, day(11, 24)) == [("kim$", "remind14", "sent"), ("pat$", "remind14", "sent")]
    assert due(conn, rec, live, day(11, 30)) == []    # the 28-day one's day, but a week after the last
    assert due(conn, rec, live, day(12, 1)) == [("kim$", "remind28"), ("pat$", "remind28")]
    run(conn, rec, live, clock, day(12, 1))
    assert due(conn, rec, live, day(12, 9)) == [] and due(conn, rec, live, day(12, 15)) == []


def test_on_time_the_days_are_as_before(world, clock):
    conn, rec, _ = world
    live = config(review_to=[])
    run(conn, rec, live, clock, OPENS)
    assert due(conn, rec, live, day(11, 15)) == []
    assert due(conn, rec, live, day(11, 16)) == [("kim$", "remind14"), ("pat$", "remind14")]
    run(conn, rec, live, clock, day(11, 16))
    assert due(conn, rec, live, day(11, 30)) == [("kim$", "remind28"), ("pat$", "remind28")]
    # missed days (the hub down): the latest due goes, a week after the last, the earlier one skipped
    rec2 = L.open_records(world[2] / "other.db", write=True)
    run(conn, rec2, live, clock, OPENS)
    plan = L.mail_plan(conn, rec2, live, REPORT, now=day(12, 3))
    assert {e["kind"] for e in plan} == {"remind28"} and all(e["skipped"] == ["remind14"] for e in plan)
    rec2.close()


def test_a_lab_new_to_the_check_mid_term(world, clock):
    conn, rec, _ = world
    live = config(review_to=[])
    run(conn, rec, live, clock, OPENS)
    run(conn, rec, live, clock, day(11, 16))
    conn.executemany("INSERT INTO group_membership (username, group_name, gid, cluster, collected_at, has_account, "
                     "source_site) VALUES (?, 'sam$', 1, 'hpc1', '2099-01-01T00:00:00', 1, 'hpc1')",
                     [("sam",), ("quinn",)])
    conn.commit()
    assert run(conn, rec, live, clock, day(11, 20)) == [("sam$", "open", "sent")]
    assert due(conn, rec, live, day(11, 21)) == []
    assert run(conn, rec, live, clock, day(11, 27)) == [("sam$", "remind14", "sent")]
    assert due(conn, rec, live, day(11, 30)) == [("kim$", "remind28"), ("pat$", "remind28")]
    run(conn, rec, live, clock, day(11, 30))
    assert due(conn, rec, live, day(12, 3)) == [] and due(conn, rec, live, day(12, 4)) == [("sam$", "remind28")]


def test_the_gap_is_a_setting(world, clock):
    conn, rec, _ = world
    assert L.settings(config()).remind_gap_days == 7
    assert L.settings(config(remind_gap_days=3)).remind_gap_days == 3
    bad = L.settings(config(remind_gap_days=0))
    assert bad.remind_gap_days == 7 and any("remind_gap_days" in p for p in bad.problems)
    assert not any("not a setting" in p for p in L.settings(config(remind_gap_days=3)).problems)
    short = config(review_to=[], remind_gap_days=2)
    run(conn, rec, short, clock, day(11, 17))
    assert due(conn, rec, short, day(11, 18)) == []
    assert due(conn, rec, short, day(11, 19)) == [("kim$", "remind14"), ("pat$", "remind14")]


def test_mail_counts_name_no_one(world, clock, monkeypatch):
    conn, rec, tmp_path = world
    from nomad import lab_check_cli as C
    from nomad.cli import cli
    for cfg, words in ((config(), "Emails today: 2 would send (2 openings; for review)."),
                       (config(review_to=[]), "Emails today: 2 would send (2 openings; to the labs' PIs).")):
        monkeypatch.setattr(C, "_ctx", lambda ctx, cfg=cfg: (cfg, REPORT, L.settings(cfg, REPORT), conn,
                                                             tmp_path / "lab_check.db"))
        clock[0]["now"] = day(11, 17)
        r = CliRunner().invoke(cli, ["lab", "check", "mail", "--counts"])
        assert r.exit_code == 0, r.output
        assert r.output.strip() == words
        assert "kim" not in r.output and "pat" not in r.output and "example.edu" not in r.output
    run(conn, rec, config(review_to=[]), clock, day(11, 17))
    clock[0]["now"] = day(11, 24)
    r = CliRunner().invoke(cli, ["lab", "check", "mail", "--counts"])
    assert r.output.strip() == "Emails today: 2 would send (2 reminders; to the labs' PIs)."
    clock[0]["now"] = day(11, 25)
    run(conn, rec, config(review_to=[]), clock, day(11, 24))
    r = CliRunner().invoke(cli, ["lab", "check", "mail", "--counts"])
    assert r.output.strip() == "Emails today: none due."
