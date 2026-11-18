"""1.57.0: writing to someone about their data unchanged for a year.

The email a person gets about their data on the list: to them, a copy to
their faculty sponsor (the PI of each lab they are in, unless they lead one)
and to Research Computing; how much, where, their storage page, what they can
do; replies to whoever writes and to Research Computing. Sent as staff wrote
it, kept whole, their cases moved to "contacted". Thirty days on, if the data
is still there, the list and the weekly email say to write again. Made-up
names only.
"""
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from nomad.storage import cold as C

TB = int(1e12)
WHEN = datetime.now().replace(microsecond=0).isoformat()
CONFIG = {"console": {"labs": {"group_pattern": "{netid}$", "accounts": {"notify": ["hpc@example.edu"]},
                               "check": {"mail_domain": "example.edu", "send_from": "RC <rc@example.edu>",
                                         "signature": ["Research Computing", "Example University"]}},
                      "machines": {"fs1": {"note": "the cluster's storage"}},
                      "cold_data": {"min_tb": 1, "link": "https://nomad.example.edu/diagnostics?tab=storage"}}}


def _db(path):
    from nomad.collectors.storage_use import SCHEMA as STORAGE
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(STORAGE)
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, submit_time TEXT);
        CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            collected_at TEXT, has_account INTEGER, source_site TEXT);""")
    c.execute("INSERT INTO storage_reports (server, mode, started, finished) VALUES ('fs1', 'usage', ?, ?)", (WHEN, WHEN))
    for p, total, used in (("/home", 100, 50), ("/scratch", 500, 100)):
        c.execute("INSERT INTO storage_paths (timestamp, server, path, total_bytes, used_bytes) VALUES (?, 'fs1', ?, ?, ?)",
                  (WHEN, p, total * TB, used * TB))
    for p, owner, size, old in (("/home", "kim", 5, 3), ("/scratch", "kim", 15.3, 12.5), ("/scratch", "pat", 4, 2),
                                ("/scratch", "lee", 3, 2)):
        c.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_owner, bytes, files, newest, "
                  "older1_bytes, older2_bytes, age1_days, age2_days, complete) VALUES (?, 'fs1', ?, ?, ?, ?, 10, "
                  "'2026-07-19T00:00:00', ?, ?, 180, 365, 1)",
                  (WHEN, p, f"{p}/{owner}", owner, int(size * TB), int(old * TB), int(old * TB)))
    for user in ("kim", "pat"):
        c.execute("INSERT INTO group_membership VALUES (?, 'pat$', 1, 'c1', ?, 1, 'c1')", (user, WHEN))
    c.commit()
    return c


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    conn = _db(tmp_path / "combined.db")
    rec = C.open_records(tmp_path / "lab_check.db", write=True)
    return conn, rec, C.settings(CONFIG), L.settings(CONFIG, None)


NAMES = {"kim": "Kim", "arc1": "Sam"}


def test_the_draft_for_a_student_copies_their_sponsor(world):
    conn, rec, s, cs = world
    got = C.listing(conn, rec, CONFIG, s)
    d = C.draft(got, "kim", s, cs, CONFIG, sender="arc1", names=NAMES, rec=rec)
    assert d["to"] == ["kim@example.edu"] and d["cc"] == ["pat@example.edu", "hpc@example.edu"]
    assert d["reply_to"] == ["arc1@example.edu", "hpc@example.edu"] and d["sponsors"] == ["pat"]
    assert d["from"] == "RC <rc@example.edu>" and not d["faculty"] and d["problems"] == []
    assert d["subject"] == "Your data on fs1 (the cluster's storage): 15.5 TB unchanged for a year"
    text = d["text"]
    assert text.startswith("Dear Kim,\n\nOur storage scans show data of yours that hasn't changed in a year or more")
    assert "  /scratch on fs1 (the cluster's storage): 12.5 TB of the 15.3 TB you hold there" in text
    assert "  /home on fs1 (the cluster's storage): 3.0 TB of the 5.0 TB you hold there" in text
    assert "https://nomad.example.edu/storage" in text and "I'm copying your faculty sponsor." in text
    assert text.rstrip().endswith("Thank you,\nSam\nResearch Computing\nExample University")
    assert "used" not in text                      # what the scans know: a change, not a read


def test_a_pi_gets_no_copy_and_someone_in_no_lab_is_said(world):
    conn, rec, s, cs = world
    got = C.listing(conn, rec, CONFIG, s)
    d = C.draft(got, "pat", s, cs, CONFIG, sender="arc1", names={}, rec=rec)
    assert d["faculty"] and d["cc"] == ["hpc@example.edu"] and d["text"].startswith("Hello,")
    assert "copying" not in d["text"]
    d = C.draft(got, "lee", s, cs, CONFIG, sender="arc1", names={}, rec=rec)
    assert d["cc"] == ["hpc@example.edu"] and any("no faculty sponsor" in p for p in d["problems"])
    with pytest.raises(C.CaseError):
        C.draft(got, "zed", s, cs, CONFIG, names={})


def test_sent_as_written_kept_and_the_cases_move_on(world):
    conn, rec, s, cs = world
    got = C.listing(conn, rec, CONFIG, s)
    C.open_case(rec, "kim", "fs1", "/home", by="arc2", s=s)                 # one case there already
    got = C.listing(conn, rec, CONFIG, s)
    sent = []
    out = C.send_letter(CONFIG, cs, s, rec, got, "kim", ["kim"], "pat@example.edu, hpc@example.edu",
                        "About your data", "Dear Kim,\n\nPlease have a look.\n", by="arc1",
                        sender=lambda m, cfg: sent.append(m))
    assert out["status"] == "sent" and out["to"] == ["kim@example.edu"]
    assert out["cc"] == ["pat@example.edu", "hpc@example.edu"] and len(out["cases"]) == 2
    m = sent[0]
    assert (m["To"], m["Cc"], m["Reply-To"]) == ("kim@example.edu", "pat@example.edu, hpc@example.edu",
                                                 "arc1@example.edu, hpc@example.edu")
    assert m["From"] == "RC <rc@example.edu>" and m.get_content() == "Dear Kim,\n\nPlease have a look.\n"
    got = C.listing(conn, rec, CONFIG, s)
    mine = [r for r in got["rows"] if r["person"] == "kim"]
    assert {r["case"]["state"] for r in mine} == {"contacted"}
    assert mine[0]["last_letter"]["by"] == "arc1" and mine[0]["last_letter"]["count"] == 1
    case = C.case_of(rec, mine[0]["case"]["number"], s)
    assert case["notes"][-1]["note"] == "emailed kim@example.edu (copy pat@example.edu, hpc@example.edu): About your data"
    kept = C.letters_of(rec, "kim")
    assert kept[0]["text"] == "Dear Kim,\n\nPlease have a look." and kept[0]["unchanged_bytes"] == int(15.5 * TB)
    # again: a note, the cases stay contacted
    C.send_letter(CONFIG, cs, s, rec, got, "kim", ["kim@example.edu"], [], "Again", "Hello again", by="arc1",
                  sender=lambda m, cfg: sent.append(m))
    assert C.last_letters(rec)["kim"]["count"] == 2 and "Reply-To" in sent[-1]
    log = [json.loads(w) for (w,) in rec.execute("SELECT what FROM lab_check_log")]
    assert any("data_letter" in x for x in log)


def test_nothing_kept_unless_it_went(world):
    conn, rec, s, cs = world
    got = C.listing(conn, rec, CONFIG, s)

    def fails(m, cfg):
        raise OSError("connection refused")
    with pytest.raises(C.CaseError, match="not sent"):
        C.send_letter(CONFIG, cs, s, rec, got, "kim", ["kim"], [], "S", "T", by="arc1", sender=fails)
    with pytest.raises(C.CaseError, match="isn't an address"):
        C.send_letter(CONFIG, cs, s, rec, got, "kim", ["not an address!"], [], "S", "T", by="arc1",
                      sender=lambda m, c: None)
    with pytest.raises(C.CaseError, match="a subject and a message"):
        C.send_letter(CONFIG, cs, s, rec, got, "kim", ["kim"], [], "", "T", by="arc1", sender=lambda m, c: None)
    assert C.letters_of(rec, "kim") == [] and C.cases(rec, s) == []


def test_thirty_days_on_and_still_there_write_again(world):
    conn, rec, s, cs = world
    got = C.listing(conn, rec, CONFIG, s)
    C.send_letter(CONFIG, cs, s, rec, got, "kim", ["kim"], [], "S", "T", by="arc1", sender=lambda m, c: None)
    C.send_letter(CONFIG, cs, s, rec, got, "pat", ["pat"], [], "S", "T", by="arc1", sender=lambda m, c: None)
    got = C.listing(conn, rec, CONFIG, s)
    assert C.write_again(got["rows"], s) == {}
    later = datetime.now() + timedelta(days=31)
    assert set(C.write_again(got["rows"], s, later)) == {"kim", "pat"}
    # kim removed most of it: no letter again
    conn.execute("UPDATE storage_tops SET older2_bytes = ? WHERE top_owner = 'kim' AND path = '/scratch'", (TB,))
    conn.commit()
    got = C.listing(conn, rec, CONFIG, s)
    assert set(C.write_again(got["rows"], s, later)) == {"pat"}
    # pat's case closed: no letter again either
    num = next(r["case"]["number"] for r in got["rows"] if r["person"] == "pat")
    C.update_case(rec, num, "arc1", state="keep", note="teaching data", s=s)
    got = C.listing(conn, rec, CONFIG, s)
    assert C.write_again(got["rows"], s, later) == {}
    C.update_case(rec, num, "arc1", state="contacted", note="again", s=s)
    got = C.listing(conn, rec, CONFIG, s)
    subject, text = C.digest(got, [], C.cases(rec, s, open_only=True), s, now=later)
    assert "Written to 30 days ago or more, and their data still there (write again from the list): 1" in text
    assert "  pat          written" in text


def test_the_storage_page_link():
    assert C.settings(CONFIG).storage_link == "https://nomad.example.edu/storage"
    assert C.settings({"alerts": {"link": "https://n.example.edu/"}}).storage_link == "https://n.example.edu/storage"
    s = C.settings({"console": {"cold_data": {"storage_link": "https://x.example.edu/mine/"}}})
    assert s.storage_link == "https://x.example.edu/mine"
    bad = C.settings({"console": {"cold_data": {"storage_link": "x"}}})
    assert bad.storage_link == "" and any("storage_link" in p for p in bad.problems)
    assert C.settings({}).storage_link == ""


def test_the_cli_letter(world, tmp_path, monkeypatch):
    from nomad import lab_check_cli, mail
    from nomad.cli import cli as main
    conn, rec, s, cs = world
    monkeypatch.setattr(lab_check_cli, "_ctx", lambda ctx: (CONFIG, None, cs, conn, tmp_path / "lab_check.db"))
    monkeypatch.setattr(C, "first_names", lambda config, netids: {})
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg, timeout=15: sent.append(msg))
    run = lambda *a: CliRunner().invoke(main, ["storage", "cold", "letter", *a])  # noqa: E731
    r = run("kim", "--as", "arc1")
    assert r.exit_code == 0, r.output
    assert "To: kim@example.edu\nCc: pat@example.edu, hpc@example.edu\nReply-To: arc1@example.edu" in r.output
    assert "Subject: Your data on fs1" in r.output and not sent
    r = run("kim", "--as", "arc1", "--send")
    assert r.exit_code == 0, r.output
    assert "Sent to kim@example.edu, copy pat@example.edu, hpc@example.edu; ARC-0001, ARC-0002 contacted." in r.output
    assert "written to before" in run("kim", "--as", "arc1").output
    assert run("zed").exit_code != 0


def test_the_directory_gives_a_first_name():
    from nomad import directory as D
    out = ("dn: uid=kim,ou=people,dc=example,dc=edu\nuid: kim\ngivenName: Kim\ndisplayName: Doe, Kim\n\n"
           "dn: uid=lee,ou=people,dc=example,dc=edu\nuid: lee\ncn: Lee Roe\n\n")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, out, "")
    d = D.Directory(D.Settings(uri="ldaps://ldap.example.edu", base="dc=example,dc=edu"), run=run)
    assert d.first_names(["kim", "lee", "Bad Name"]) == {"kim": "Kim"}
    assert "(|(uid=kim)(uid=lee))" in seen[0]
