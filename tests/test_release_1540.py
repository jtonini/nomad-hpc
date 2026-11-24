"""nomad 1.54.0: Ask ARC -- help requests with a number, the person's own
context, and the conversation kept (nomad.help_requests)."""
from datetime import datetime, timedelta

import pytest

from nomad import help_requests as H
from nomad import tickets as T

CONFIG = {"support": {"email": "help@example.edu", "user_email_domain": "example.edu", "team": "Helpdesk"},
          "mail": {"from": "nomad@example.edu"},
          "console": {"help": {"link": "https://nomad.example.edu/help/"}}}


@pytest.fixture
def rec(tmp_path, monkeypatch):
    from nomad import lab_check as L
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)        # the Console's account, not root
    r = H.open_records(tmp_path / "lab_check.db", write=True)
    yield r
    r.close()


def test_settings_from_support_and_mail():
    s = H.settings(CONFIG)
    assert s.notify == ("help@example.edu",) and s.mail_domain == "example.edu"
    assert s.send_from == "nomad@example.edu" and s.link == "https://nomad.example.edu/help"
    assert s.per_hour == 5 and not s.problems and s.team == "Helpdesk"
    assert H.settings({}).team == "Research Computing"                 # nothing of one institution built in
    assert H.settings({"support": {"team": "x" * 61}}).team == "Research Computing"
    bad = H.settings({"console": {"help": {"notify": "nobody", "per_hour": 0, "link": "ftp://x"}}})
    assert len(bad.problems) == 3 and bad.notify == () and bad.per_hour == 5 and bad.link == ""
    assert H.settings(None).notify == ()


def test_a_request_and_its_conversation(rec):
    s = H.settings(CONFIG)
    T.ensure(rec)
    with rec:
        T.take(rec, "accounts")                     # an account request took REQ-0001
    req = H.ask(rec, "Amy", "  My job   ended early ", "It stopped after an hour.", about="job", site="hpc1",
                job="123", page="/job?job_id=123", context="Job 123: TIMEOUT", s=s)
    assert req["ref"] == "REQ-0002" and req["asked_by"] == "amy" and req["subject"] == "My job ended early"
    assert req["state"] == "open" and req["said"] == "waiting for Helpdesk"
    assert [m["side"] for m in req["messages"]] == ["person"]
    assert T.kind_of(rec, req["number"]) == "help"
    n = req["number"]
    H.say(rec, n, "rcadmin", "note", "Probably the time limit.", s=s)
    assert H.request_of(rec, n, s)["state"] == "open"                      # a note changes nothing
    got = H.say(rec, n, "rcadmin", "arc", "It reached its time limit: ask for more.", s=s)
    assert got["state"] == "answered"
    seen = H.request_of(rec, n, s, notes=False)
    assert [m["side"] for m in seen["messages"]] == ["person", "arc"]           # the note is ARC's own
    assert H.say(rec, n, "amy", "person", "Thanks, and for the next one?", s=s)["state"] == "open"
    with pytest.raises(H.HelpError, match="only the person"):
        H.say(rec, n, "pat", "person", "me too", s=s)
    assert H.set_state(rec, n, "amy", "closed", s=s)["state"] == "closed"
    listed = H.requests(rec, by="amy", s=s)
    assert len(listed) == 1 and listed[0]["n"] == 3 and listed[0]["last_side"] == "person"
    assert "context" not in listed[0]
    assert H.requests(rec, by="amy", open_only=True, s=s) == []
    assert H.requests(rec, by="pat", s=s) == []
    log = [r[0] for r in rec.execute("SELECT what FROM lab_check_log")]
    assert len(log) == 5 and all("help" in w for w in log)


def test_what_is_refused(rec):
    s = H.settings({"console": {"help": {"per_hour": 2}}})
    for bad in ({"subject": "", "message": "x"}, {"subject": "x", "message": ""},
                {"subject": "x" * 201, "message": "x"}, {"subject": "x", "message": "x", "about": "weather"},
                {"subject": "x", "message": "x", "context": "c" * (H.CONTEXT_MAX + 1)}):
        with pytest.raises(H.HelpError):
            H.ask(rec, "amy", s=s, **bad)
    with pytest.raises(H.HelpError, match="username"):
        H.ask(rec, "amy smith", "x", "x", s=s)
    H.ask(rec, "amy", "one", "x", s=s)
    H.ask(rec, "amy", "two", "x", s=s)
    with pytest.raises(H.HelpError, match="2 requests in the last hour"):
        H.ask(rec, "amy", "three", "x", s=s)
    H.ask(rec, "pat", "pat's own", "x", s=s)                                   # per person
    with pytest.raises(H.HelpError, match="no request"):
        H.say(rec, 999, "rcadmin", "arc", "x", s=s)
    with pytest.raises(H.HelpError):
        H.set_state(rec, 1, "rcadmin", "lost", s=s)


def test_the_letters(rec):
    s = H.settings(CONFIG)
    req = H.ask(rec, "amy", "Out of memory", "My job ran out of memory.", about="job", site="hpc1", job="77",
                page="/job?job_id=77", context="Job 77 on hpc1: OUT_OF_MEMORY", s=s)
    subject, text = H.letter_to_arc(req, "My job ran out of memory.", s, first=True)
    assert subject == f"[{req['ref']}] Out of memory"
    assert "from amy, about a job (job 77 on hpc1)" in text and "OUT_OF_MEMORY" in text
    assert "https://nomad.example.edu/help?request=" in text and f"Keep {req['ref']}" in text
    subject, text = H.letter_to_person(req, "My job ran out of memory.", s, first=True)
    assert subject == f"[{req['ref']}] Out of memory" and text.startswith("Helpdesk has your request")
    assert "Ask Helpdesk page" in text and "REQ" not in text.replace(req["ref"], "")
    subject, text = H.letter_to_person(req, "Ask for 8 GB.", s, first=False)
    assert subject.startswith("Re: [") and "Ask for 8 GB." in text
    assert H.person_address("amy", s) == (["amy@example.edu"], False)
    review = H.settings({**CONFIG, "console": {"help": {"review_to": ["rc@example.edu"]}}})
    assert H.person_address("amy", review) == (["rc@example.edu"], True)
    assert H.person_address("amy", H.settings({})) == ([], False)


def test_sending(rec, monkeypatch):
    from nomad import mail
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg: sent.append(msg))
    s = H.settings(CONFIG)
    req = H.ask(rec, "amy", "Help", "Please.", s=s)
    assert H.tell_arc(CONFIG, s, req, "Please.", first=True) == "sent"
    assert H.tell_person(CONFIG, s, req, "Please.", first=True) == "sent"
    arc, person = sent
    assert arc["To"] == "help@example.edu" and arc["Reply-To"] == "amy@example.edu"
    assert person["To"] == "amy@example.edu" and person["Reply-To"] == "help@example.edu"
    assert H.tell_arc({}, H.settings({}), req, "x", first=True).startswith("not sent")
    review = H.settings({**CONFIG, "console": {"help": {"review_to": ["rc@example.edu"]}}})
    H.tell_person(CONFIG, review, req, "x", first=False)
    assert sent[-1]["To"] == "rc@example.edu" and sent[-1]["Subject"].startswith("[review] ")
    when = req["messages"][0]["at"]
    H.mark_emailed(rec, req["number"], when, "sent")
    assert H.request_of(rec, req["number"], s)["messages"][0]["emailed"] == "sent"


def test_rate_counts_only_the_last_hour(rec):
    s = H.settings({})
    H.ask(rec, "amy", "old", "x", s=s)
    old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    with rec:
        rec.execute("UPDATE help_requests SET asked_at = ?", (old,))
    assert H.asked_lately(rec, "amy") == 0
