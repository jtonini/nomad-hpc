# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.4: the lab check's email reads as a letter to the PI, by name, and
says who asks (the Console's branding)."""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from tests.test_release_1330 import CONFIG, REPORT, hub

REAL_SYSTEM_NAME = L._system_name
BRAND = {"branding": {"institution": "University of Richmond",
                      "unit": ["Academic Research Computing", "Office of the Provost"]}}


def check(**more):
    """CONFIG with the Console's branding, lab names and more check settings."""
    labs = {**CONFIG["console"]["labs"], "resources": {"cparish$": {"name": "Parish Lab"}},
            "check": {**CONFIG["console"]["labs"]["check"], **more}}
    return {"console": {**BRAND, "labs": labs}}


@pytest.fixture(autouse=True)
def no_accounts(monkeypatch):
    """No account has a full name unless a test gives one; not run as root."""
    monkeypatch.setattr(L, "_system_name", lambda netid: None)
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)


def entry(**more):
    e = {"round": "2026-11-02", "lab": "cparish$", "name": "Parish Lab", "kind": "open", "review": False,
         "for": ["cparish@example.edu"], "leaders": ["cparish"], "members": 132, "claims": 0, "left": 132,
         "condos": True, "closes": "2026-12-14"}
    return {**e, **more}


CONTRACTION = re.compile(r"n't\b|\b(we|you|it|they|that|there|who)'(d|re|ll|ve|s)\b|\bI'm\b", re.I)


# -- names ----------------------------------------------------------------------------------

def test_a_full_name_as_an_account_holds_it():
    assert L.split_name("Carol Parish") == ("Carol", "Parish")
    assert L.split_name("Carol A. Parish,Gottwald C-210,804-555-0100") == ("Carol", "Parish")
    assert L.split_name("PARISH, CAROL") == ("", "Parish")           # GECOS: the name, then office ...
    assert L.split_name("carol parish jr.") == ("Carol", "Parish")
    assert L.split_name("Mei Yang PhD") == ("Mei", "Yang")
    assert L.split_name("McDonald") == ("", "McDonald")
    assert L.split_name("") is None and L.split_name("user123") is None and L.split_name(None) is None


def test_the_account_s_name(monkeypatch):
    monkeypatch.setattr(L, "_system_name", REAL_SYSTEM_NAME)             # the real lookup
    import pwd
    acct = {"cparish": "Carol Parish,,,", "svc": "svc", "empty": ""}
    monkeypatch.setattr(pwd, "getpwnam", lambda n: SimpleNamespace(pw_gecos=acct[n]) if n in acct
                        else (_ for _ in ()).throw(KeyError(n)))
    assert L._system_name("cparish") == ("Carol", "Parish")
    assert L._system_name("svc") is None                                # its name is its NetID
    assert L._system_name("empty") is None and L._system_name("nobody") is None


def test_settings_for_the_letter():
    s = L.settings(check(names={"cparish": "Carol Parish", "mdelacruz": {"first": "Maria", "last": "de la Cruz"},
                                "bad": 3}, contact="Questions: rc@example.edu"), REPORT)
    assert s.salutation == "Dr. {last}"
    assert s.names == (("cparish", "Carol", "Parish"), ("mdelacruz", "Maria", "de la Cruz"))
    assert any("names: bad" in p for p in s.problems)
    assert s.signature == ("Academic Research Computing", "Office of the Provost", "University of Richmond")
    assert s.contact == "Questions: rc@example.edu"
    # a salutation with an unknown or no field, a format spec, or not text: the default, said
    for bad in ("Dr. {surname}", "{0}", "Dr. {last!r}", "Professor", 3, "Dr. {last"):
        b = L.settings(check(salutation=bad), REPORT)
        assert b.salutation == "Dr. {last}" and any("salutation" in p for p in b.problems), bad
    assert L.settings(check(salutation="Professor {first} {last}"), REPORT).salutation == "Professor {first} {last}"
    # signature: given, else branding (units, then institution), else report.toml's institution
    assert L.settings(check(signature=["ARC", "", "UR"]), REPORT).signature == ("ARC", "UR")
    assert L.settings(check(signature="ARC"), REPORT).signature == ("ARC",)
    plain = {"console": {"labs": CONFIG["console"]["labs"]}}
    assert L.settings(plain, REPORT).signature == ("Research Computing",)
    assert L.settings({"support": {"institution": "Example U"}, **plain}, REPORT).signature == ("Example U",)
    off = SimpleNamespace(delivery=SimpleNamespace(branding=False, institution="RC Group", mail_domain=None,
                                                   send_from=None, review_to=[], contact=None))
    assert L.settings(check(), off).signature == ("RC Group",)
    assert any("date_format" in p for p in L.settings(check(date_format=7), REPORT).problems)


def test_the_greeting():
    s = L.settings(check(names={"cparish": "Carol Parish", "myang": "Mei Yang", "knolin": "Kim Nolin"}), REPORT)
    assert L.greeting(["cparish"], s) == "Dear Dr. Parish,"
    assert L.greeting(["myang", "knolin"], s) == "Dear Dr. Yang and Dr. Nolin,"
    assert L.greeting(["myang", "knolin", "cparish"], s) == "Dear Dr. Yang, Dr. Nolin and Dr. Parish,"
    assert L.greeting(["nobody"], s) == "Dear colleague,"
    assert L.greeting(["myang", "nobody"], s) == "Dear colleagues,"       # not half a greeting
    assert L.greeting([], s) == "Dear colleague,"
    first = L.settings(check(salutation="{first}", names={"pp": "PARISH, CAROL", "cp": "Carol Parish"}), REPORT)
    assert L.greeting(["cp"], first) == "Dear Carol," and L.greeting(["pp"], first) == "Dear colleague,"


# -- the email --------------------------------------------------------------------------------

def test_the_opening_email():
    s = L.settings(check(names={"cparish": "Carol Parish"}, link="https://nomad.richmond.edu/lab"), REPORT)
    e = entry()
    body = L._body(e, s)
    assert not CONTRACTION.search(body) and not CONTRACTION.search(L.subject_of(e)), body
    lines = body.splitlines()
    assert lines[0] == "Dear Dr. Parish,"
    assert "Each term, Academic Research Computing asks the faculty who lead research groups to confirm who " \
           "belongs to their labs. Your answers keep access" in body
    assert "condo nodes" in body
    assert "For the Parish Lab (cparish$), please tell us whether each of its 132 members is still in the lab" in body
    assert "until Monday, December 14, 2026." in body
    assert "everyone with no job in the past year in one step" in body           # 132: the bulk answer
    assert "Nothing happens automatically. Our team reviews your answers before making any change." in body
    assert lines[-4:] == ["", "Academic Research Computing", "Office of the Provost", "University of Richmond"]
    assert lines[-5] == "Thank you,"
    assert L.subject_of(e) == "Please confirm the members of the Parish Lab (cparish$)"
    # a small lab with no condo: no bulk sentence, no condos said
    small = L._body(entry(members=1, left=1, condos=False), s)
    assert "its one member is still in the lab" in small and "one step" not in small and "condo" not in small


def test_a_reminder_and_condo_users():
    s = L.settings(check(names={"cparish": "Carol Parish"}, contact="Questions: rc@example.edu"), REPORT)
    e = entry(kind="remind14", claims=3, left=120)
    body = L._body(e, s)
    assert not CONTRACTION.search(body), body
    assert "This is a reminder that the membership review for the Parish Lab (cparish$) is still open: " \
           "120 people do not have an answer yet." in body
    assert "\nPlease tell us:\n- whether each of its 132 members" in body
    assert "- whether each of the 3 people outside the group who ran jobs on your condo nodes in the last 120 " \
           "days works with you or is a guest." in body
    assert "Questions: rc@example.edu\n\nThank you," in body
    assert L.subject_of(e) == "Reminder: please confirm the members of the Parish Lab (cparish$)"
    one = L._body(entry(kind="remind28", members=0, claims=1, left=1), s)
    assert "1 person does not have an answer yet" in one and "whether the one person outside the group" in one
    # review: the banner first, then the letter as the PI would get it
    rv = L._body(entry(review=True), s)
    assert rv.startswith("[For review: this would have gone to cparish@example.edu.]\n\nDear Dr. Parish,")
    assert L.subject_of(entry(review=True)).startswith("[review] Please confirm")
    assert "until 2026-12-14." in L._body(e, L.settings(check(date_format="%Y-%m-%d"), REPORT))


def test_how_the_lab_is_named():
    t = lambda name: L._title({"lab": "x$", "name": name})            # noqa: E731
    assert t("Parish Lab") == "the Parish Lab (x$)"
    assert t("Yang Research Group") == "the Yang Research Group (x$)"
    assert t("The Erickson Lab") == "The Erickson Lab (x$)"
    assert t("Carol's lab") == "Carol's lab (x$)"
    assert t("Bioinformatics") == "Bioinformatics (x$)"
    assert t("x$") == "your lab (x$)" and t(None) == "your lab (x$)"


def test_the_emails_sent(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    sent = []
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: sent.append(msg))
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    cfg = check(names={"cparish": "Carol Parish", "myang": "Mei Yang"})
    opened = datetime(2026, 9, 15, 8, 0)
    plan = {e["lab"]: e for e in L.mail_plan(conn, rec, cfg, REPORT, now=opened)}
    assert plan["cparish$"]["leaders"] == ["cparish"] and plan["cparish$"]["condos"]
    assert not plan["kdonald$"]["condos"]
    L.send(conn, rec, cfg, REPORT, now=opened)
    by = {m["Subject"]: m.get_content() for m in sent}
    parish = by["[review] Please confirm the members of the Parish Lab (cparish$)"]
    assert "\n\nDear Dr. Parish,\n\n" in parish and parish.rstrip().endswith("University of Richmond")
    assert "Dear Dr. Yang," in by["[review] Please confirm the members of your lab (myang$)"]
    assert "Dear colleague," in by["[review] Please confirm the members of your lab (kdonald$)"]
    conn.close()
    rec.close()


def test_the_names_command(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    from nomad import lab_check_cli as C
    from nomad.cli import cli
    cfg = check(names={"cparish": "Carol Parish"})
    monkeypatch.setattr(C, "_ctx", lambda ctx: (cfg, REPORT, L.settings(cfg, REPORT), conn, tmp_path / "x.db"))
    r = CliRunner().invoke(cli, ["lab", "check", "names"])
    assert r.exit_code == 0, r.output
    assert re.search(r"cparish\$\s+Dear Dr. Parish,\s+cparish = Carol Parish \[names\]", r.output)
    assert re.search(r"kdonald\$\s+Dear colleague,\s+kdonald = \(no name\) \[none\]", r.output)
    assert "with no name" in r.output
    r = CliRunner().invoke(cli, ["lab", "check", "names", "--missing"])
    assert "cparish$" not in r.output and "kdonald$" in r.output
    conn.close()


def test_a_lab_s_email_whenever_asked(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    cfg = check(names={"cparish": "Carol Parish"}, review_to=["rc@example.edu"])
    opened = datetime(2026, 9, 15, 8, 0)
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: None)
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    L.send(conn, rec, cfg, REPORT, now=opened)                          # the review copies went
    assert not [e for e in L.mail_plan(conn, rec, cfg, REPORT, now=opened) if e["lab"] == "cparish$"]
    got = L.letter(conn, rec, cfg, REPORT, "cparish$", now=opened)       # still shown, as the PI gets it
    assert got["to"] == ["cparish@example.edu"] and got["subject"].startswith("Please confirm")
    assert got["text"].startswith("Dear Dr. Parish,") and "[For review" not in got["text"]
    rnd = L.round_of(L.settings(cfg, REPORT), opened.date())
    L.record(rec, rnd, L.lab_for(conn, cfg, REPORT, "cparish$", now=opened), "bo", "here", by="cparish")
    later = L.letter(conn, rec, cfg, REPORT, "cparish$", reminder=True, now=opened)
    assert later["subject"].startswith("Reminder:") and "2 people do not have an answer yet" in later["text"]
    assert L.letter(conn, rec, cfg, REPORT, "nolab$", now=opened) is None
    off = {"console": {"labs": {**cfg["console"]["labs"], "check": {"opens": []}}}}
    assert L.letter(conn, rec, off, REPORT, "cparish$", now=opened) is None

    from nomad import lab_check_cli as C
    from nomad.cli import cli
    monkeypatch.setattr(C, "_ctx", lambda ctx: (cfg, REPORT, L.settings(cfg, REPORT), conn, tmp_path / "lab_check.db"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return opened
    monkeypatch.setattr(C, "datetime", Frozen)
    monkeypatch.setattr(L, "datetime", Frozen)
    r = CliRunner().invoke(cli, ["lab", "check", "email", "cparish$", "--reminder"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("To: cparish@example.edu\nSubject: Reminder: please confirm the members of the "
                               "Parish Lab (cparish$)\n\nDear Dr. Parish,")
    r = CliRunner().invoke(cli, ["lab", "check", "email", "nolab$"])
    assert r.exit_code != 0 and "no lab with anyone to ask about" in r.output
    conn.close()
    rec.close()
