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
BRAND = {"branding": {"institution": "Example University",
                      "unit": ["Academic Research Computing", "Office of the Provost"]}}


def check(**more):
    """CONFIG with the Console's branding, lab names and more check settings."""
    labs = {**CONFIG["console"]["labs"], "resources": {"rames$": {"name": "Ames Lab"}},
            "check": {**CONFIG["console"]["labs"]["check"], **more}}
    return {"console": {**BRAND, "labs": labs}}


@pytest.fixture(autouse=True)
def no_accounts(monkeypatch):
    """No account has a full name unless a test gives one; not run as root."""
    monkeypatch.setattr(L, "_system_name", lambda netid: None)
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)


def entry(**more):
    e = {"round": "2026-11-02", "lab": "rames$", "name": "Ames Lab", "kind": "open", "review": False,
         "for": ["rames@example.edu"], "leaders": ["rames"], "members": 132, "claims": 0, "left": 132,
         "condos": True, "closes": "2026-12-14"}
    return {**e, **more}


CONTRACTION = re.compile(r"n't\b|\b(we|you|it|they|that|there|who)'(d|re|ll|ve|s)\b|\bI'm\b", re.I)


# -- names ----------------------------------------------------------------------------------

def test_a_full_name_as_an_account_holds_it():
    assert L.split_name("Robin Ames") == ("Robin", "Ames")
    assert L.split_name("Robin A. Ames,Gottwald C-210,804-555-0100") == ("Robin", "Ames")
    assert L.split_name("AMES, ROBIN") == ("", "Ames")           # GECOS: the name, then office ...
    assert L.split_name("robin ames jr.") == ("Robin", "Ames")
    assert L.split_name("Mei Chen PhD") == ("Mei", "Chen")
    assert L.split_name("McDonald") == ("", "McDonald")
    assert L.split_name("") is None and L.split_name("user123") is None and L.split_name(None) is None


def test_the_account_s_name(monkeypatch):
    monkeypatch.setattr(L, "_system_name", REAL_SYSTEM_NAME)             # the real lookup
    import pwd
    acct = {"rames": "Robin Ames,,,", "svc": "svc", "empty": ""}
    monkeypatch.setattr(pwd, "getpwnam", lambda n: SimpleNamespace(pw_gecos=acct[n]) if n in acct
                        else (_ for _ in ()).throw(KeyError(n)))
    assert L._system_name("rames") == ("Robin", "Ames")
    assert L._system_name("svc") is None                                # its name is its NetID
    assert L._system_name("empty") is None and L._system_name("nobody") is None


def test_settings_for_the_letter():
    s = L.settings(check(names={"rames": "Robin Ames", "mdelacruz": {"first": "Maria", "last": "de la Cruz"},
                                "bad": 3}, contact="Questions: rc@example.edu"), REPORT)
    assert s.salutation == "Dr. {last}"
    assert s.names == (("mdelacruz", "Maria", "de la Cruz"), ("rames", "Robin", "Ames"))
    assert any("names: bad" in p for p in s.problems)
    assert s.signature == ("Academic Research Computing", "Office of the Provost", "Example University")
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
    s = L.settings(check(names={"rames": "Robin Ames", "tchen": "Mei Chen", "lnovak": "Kim Novak"}), REPORT)
    assert L.greeting(["rames"], s) == "Dear Dr. Ames,"
    assert L.greeting(["tchen", "lnovak"], s) == "Dear Dr. Chen and Dr. Novak,"
    assert L.greeting(["tchen", "lnovak", "rames"], s) == "Dear Dr. Chen, Dr. Novak and Dr. Ames,"
    assert L.greeting(["nobody"], s) == "Dear colleague,"
    assert L.greeting(["tchen", "nobody"], s) == "Dear colleagues,"       # not half a greeting
    assert L.greeting([], s) == "Dear colleague,"
    first = L.settings(check(salutation="{first}", names={"pp": "AMES, ROBIN", "cp": "Robin Ames"}), REPORT)
    assert L.greeting(["cp"], first) == "Dear Robin," and L.greeting(["pp"], first) == "Dear colleague,"


# -- the email --------------------------------------------------------------------------------

def test_the_opening_email():
    s = L.settings(check(names={"rames": "Robin Ames"}, link="https://nomad.example.edu/lab"), REPORT)
    e = entry()
    body = L._body(e, s)
    assert not CONTRACTION.search(body) and not CONTRACTION.search(L.subject_of(e)), body
    lines = body.splitlines()
    assert lines[0] == "Dear Dr. Ames,"
    assert "Each term, Academic Research Computing asks the faculty who lead research groups to confirm who " \
           "belongs to their labs. Your answers keep access" in body
    assert "condo nodes" in body
    assert "For the Ames Lab (rames$), please tell us whether each of its 132 members is still in the lab" in body
    assert "until Monday, December 14, 2026." in body
    assert "everyone with no job in the past year in one step" in body           # 132: the bulk answer
    assert "Nothing happens automatically. Our team reviews your answers before making any change." in body
    assert lines[-4:] == ["", "Academic Research Computing", "Office of the Provost", "Example University"]
    assert lines[-5] == "Thank you,"
    assert L.subject_of(e) == "Please confirm the members of the Ames Lab (rames$)"
    # a small lab with no condo: no bulk sentence, no condos said
    small = L._body(entry(members=1, left=1, condos=False), s)
    assert "its one member is still in the lab" in small and "one step" not in small and "condo" not in small


def test_a_reminder_and_condo_users():
    s = L.settings(check(names={"rames": "Robin Ames"}, contact="Questions: rc@example.edu"), REPORT)
    e = entry(kind="remind14", claims=3, left=120)
    body = L._body(e, s)
    assert not CONTRACTION.search(body), body
    assert "This is a reminder that the membership review for the Ames Lab (rames$) is still open: " \
           "120 people do not have an answer yet." in body
    assert "\nPlease tell us:\n- whether each of its 132 members" in body
    assert "- whether each of the 3 people outside the group who ran jobs on your condo nodes in the last 120 " \
           "days works with you or is a guest." in body
    assert "Questions: rc@example.edu\n\nThank you," in body
    assert L.subject_of(e) == "Reminder: please confirm the members of the Ames Lab (rames$)"
    one = L._body(entry(kind="remind28", members=0, claims=1, left=1), s)
    assert "1 person does not have an answer yet" in one and "whether the one person outside the group" in one
    # review: the banner first, then the letter as the PI would get it
    rv = L._body(entry(review=True), s)
    assert rv.startswith("[For review: this would have gone to rames@example.edu.]\n\nDear Dr. Ames,")
    assert L.subject_of(entry(review=True)).startswith("[review] Please confirm")
    assert "until 2026-12-14." in L._body(e, L.settings(check(date_format="%Y-%m-%d"), REPORT))


def test_how_the_lab_is_named():
    t = lambda name: L._title({"lab": "x$", "name": name})            # noqa: E731
    assert t("Ames Lab") == "the Ames Lab (x$)"
    assert t("Chen Research Group") == "the Chen Research Group (x$)"
    assert t("The Erickson Lab") == "The Erickson Lab (x$)"
    assert t("Robin's lab") == "Robin's lab (x$)"
    assert t("Bioinformatics") == "Bioinformatics (x$)"
    assert t("x$") == "your lab (x$)" and t(None) == "your lab (x$)"


def test_the_emails_sent(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    sent = []
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: sent.append(msg))
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    cfg = check(names={"rames": "Robin Ames", "tchen": "Mei Chen"})
    opened = datetime(2026, 9, 15, 8, 0)
    plan = {e["lab"]: e for e in L.mail_plan(conn, rec, cfg, REPORT, now=opened)}
    assert plan["rames$"]["leaders"] == ["rames"] and plan["rames$"]["condos"]
    assert not plan["kdunn$"]["condos"]
    L.send(conn, rec, cfg, REPORT, now=opened)
    by = {m["Subject"]: m.get_content() for m in sent}
    ames = by["[review] Please confirm the members of the Ames Lab (rames$)"]
    assert "\n\nDear Dr. Ames,\n\n" in ames and ames.rstrip().endswith("Example University")
    assert "Dear Dr. Chen," in by["[review] Please confirm the members of your lab (tchen$)"]
    assert "Dear colleague," in by["[review] Please confirm the members of your lab (kdunn$)"]
    conn.close()
    rec.close()


def test_the_names_command(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    from nomad import lab_check_cli as C
    from nomad.cli import cli
    cfg = check(names={"rames": "Robin Ames"})
    monkeypatch.setattr(C, "_ctx", lambda ctx: (cfg, REPORT, L.settings(cfg, REPORT), conn, tmp_path / "x.db"))
    r = CliRunner().invoke(cli, ["lab", "check", "names"])
    assert r.exit_code == 0, r.output
    assert re.search(r"rames\$\s+Dear Dr. Ames,\s+rames = Robin Ames \[names\]", r.output)
    assert re.search(r"kdunn\$\s+Dear colleague,\s+kdunn = \(no name\) \[none\]", r.output)
    assert "with no name" in r.output
    r = CliRunner().invoke(cli, ["lab", "check", "names", "--missing"])
    assert "rames$" not in r.output and "kdunn$" in r.output
    conn.close()


def test_a_lab_s_email_whenever_asked(tmp_path, monkeypatch):
    conn = hub(tmp_path / "combined.db")
    rec = L.open_records(tmp_path / "lab_check.db", write=True)
    cfg = check(names={"rames": "Robin Ames"}, review_to=["rc@example.edu"])
    opened = datetime(2026, 9, 15, 8, 0)
    monkeypatch.setattr("nomad.mail.send", lambda msg, cfg, timeout=15: None)
    monkeypatch.setattr("nomad.mail.settings", lambda config: {"from": "nomad@example.edu"})
    L.send(conn, rec, cfg, REPORT, now=opened)                          # the review copies went
    assert not [e for e in L.mail_plan(conn, rec, cfg, REPORT, now=opened) if e["lab"] == "rames$"]
    got = L.letter(conn, rec, cfg, REPORT, "rames$", now=opened)       # still shown, as the PI gets it
    assert got["to"] == ["rames@example.edu"] and got["subject"].startswith("Please confirm")
    assert got["text"].startswith("Dear Dr. Ames,") and "[For review" not in got["text"]
    rnd = L.round_of(L.settings(cfg, REPORT), opened.date())
    L.record(rec, rnd, L.lab_for(conn, cfg, REPORT, "rames$", now=opened), "bo", "here", by="rames")
    later = L.letter(conn, rec, cfg, REPORT, "rames$", reminder=True, now=opened)
    assert later["subject"].startswith("Reminder:") and "2 people do not have an answer yet" in later["text"]
    assert L.letter(conn, rec, cfg, REPORT, "nolab$", now=opened) is None
    off = {"console": {"labs": {**cfg["console"]["labs"], "check": {"opens": []}}}}
    assert L.letter(conn, rec, off, REPORT, "rames$", now=opened) is None

    from nomad import lab_check_cli as C
    from nomad.cli import cli
    monkeypatch.setattr(C, "_ctx", lambda ctx: (cfg, REPORT, L.settings(cfg, REPORT), conn, tmp_path / "lab_check.db"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return opened
    monkeypatch.setattr(C, "datetime", Frozen)
    monkeypatch.setattr(L, "datetime", Frozen)
    r = CliRunner().invoke(cli, ["lab", "check", "email", "rames$", "--reminder"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("To: rames@example.edu\nSubject: Reminder: please confirm the members of the "
                               "Ames Lab (rames$)\n\nDear Dr. Ames,")
    r = CliRunner().invoke(cli, ["lab", "check", "email", "nolab$"])
    assert r.exit_code != 0 and "no lab with anyone to ask about" in r.output
    conn.close()
    rec.close()
