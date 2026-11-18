# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Ask ARC: help requests with a number, the person's own context, and the
conversation kept (1.54.0).

Every name here is the site's: the computing center is ``[support] team``
(its short name, as people say it: "Ask ARC", "ARC answered"; default
"Research Computing"), the numbers' prefix ``[console.tickets] prefix``, the
addresses below. Nothing about one institution is written into nomad.

Someone asks the computing center for help from the Console: what it is about
(one of their jobs, or anything else), their words, and the context they
chose to attach after seeing it in full -- the job as nomad read it, the end
of its output, its nodes now, their storage, the page they were on. The
request gets a number from the one sequence (ARC-0042, nomad.tickets),
Research Computing is emailed with the number in the subject, and the person
gets a copy saying the number. Each message after that is kept with the
request: the person's, ARC's answers (emailed to the person), ARC's own
notes (never shown or sent to the person). States:

    open (waiting for ARC) -> answered -> closed
    a message from the person opens it again

A reply to one of these emails keeps the number in its subject but goes to
ARC's mailbox, not here: replies on the Console are the ones kept with the
request (mail read back into it would come later).

Settings, in the hub's nomad.toml::

    [support]
    team = "ARC"                   # the computing center, as people call it (default "Research Computing")
    email = "hpc@example.edu"      # where help requests go, unless [console.help] notify says

    [console.help]
    notify = ["hpc@example.edu"]   # ARC's addresses (default: [support] email)
    mail_domain = "example.edu"    # a person's address: NETID@mail_domain
                                   #   (default: [support] user_email_domain)
    send_from = "nomad@example.edu"   # default: [mail] from
    review_to = []                 # while set, every email for a person goes here instead
    link = "https://nomad.example.edu/help"   # the page, in the emails
    per_hour = 5                   # requests (not replies) a person may send in an hour

The requests are kept in lab_check.db (the nomad account's, beside
combined.db), with the account requests and the data cases, every change in
its log.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad import tickets as T

STATES = ("open", "answered", "closed")
SAID = {"open": "waiting for {team}", "answered": "answered", "closed": "closed"}
SIDES = ("person", "arc", "note")
ABOUT = {"job": "a job", "storage": "storage", "software": "software or a module", "account": "an account",
         "other": "something else"}
SUBJECT_MAX = 200
TEXT_MAX = 20_000
CONTEXT_MAX = 40_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS help_requests (
    number INTEGER PRIMARY KEY, asked_by TEXT NOT NULL, asked_at TEXT NOT NULL, subject TEXT NOT NULL,
    about TEXT, site TEXT, job TEXT, page TEXT, context TEXT,
    state TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_help_requests_by ON help_requests(asked_by, asked_at);
CREATE TABLE IF NOT EXISTS help_messages (
    number INTEGER NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL, side TEXT NOT NULL,
    text TEXT NOT NULL, emailed TEXT);
CREATE INDEX IF NOT EXISTS idx_help_messages ON help_messages(number, at);
"""
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DOMAIN = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
_NETID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class HelpError(ValueError):
    """The request can't be kept or changed as asked: what is wrong, in words."""


DEFAULT_TEAM = "Research Computing"
TEAM_MAX = 60


@dataclass(frozen=True)
class Settings:
    team: str = DEFAULT_TEAM
    notify: tuple = ()
    mail_domain: str | None = None
    send_from: str | None = None
    review_to: tuple = ()
    link: str = ""
    per_hour: int = 5
    problems: tuple = field(default_factory=tuple)
    tickets: T.Settings = field(default_factory=T.Settings)


def _addresses(value, where: str, problems: list) -> list:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(a, str) and _ADDRESS.fullmatch(a.strip()) for a in value):
        problems.append(f"{where} should be a list of email addresses")
        return []
    return [a.strip() for a in value]


def settings(config: dict | None) -> Settings:
    config = config or {}
    console = config.get("console") if isinstance(config.get("console"), dict) else {}
    raw = console.get("help") if isinstance(console.get("help"), dict) else {}
    support = config.get("support") if isinstance(config.get("support"), dict) else {}
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    problems: list = []
    team = support.get("team", DEFAULT_TEAM)
    if not (isinstance(team, str) and team.strip() and len(team.strip()) <= TEAM_MAX and "\n" not in team):
        problems.append(f"[support] team should be the computing center's name, at most {TEAM_MAX} characters")
        team = DEFAULT_TEAM
    team = " ".join(team.split())
    notify = _addresses(raw["notify"], "[console.help] notify", problems) if "notify" in raw else (
        [support["email"]] if isinstance(support.get("email"), str) and _ADDRESS.fullmatch(support["email"]) else [])
    domain = raw.get("mail_domain", support.get("user_email_domain"))
    if domain is not None and not (isinstance(domain, str) and _DOMAIN.fullmatch(domain)):
        problems.append("[console.help] mail_domain should be a domain, such as example.edu")
        domain = None
    sender = raw.get("send_from", mail.get("from"))
    if sender is not None and not (isinstance(sender, str) and _ADDRESS.fullmatch(sender)):
        problems.append("[console.help] send_from should be an email address")
        sender = None
    review = _addresses(raw.get("review_to", []), "[console.help] review_to", problems)
    link = raw.get("link", "")
    if not (isinstance(link, str) and (not link or link.startswith(("https://", "http://")))):
        problems.append("[console.help] link should start with https://")
        link = ""
    per_hour = raw.get("per_hour", 5)
    if isinstance(per_hour, bool) or not isinstance(per_hour, int) or not 1 <= per_hour <= 100:
        problems.append("[console.help] per_hour should be a whole number from 1 to 100")
        per_hour = 5
    numbers = T.settings(config)
    problems += list(numbers.problems)
    return Settings(team=team, notify=tuple(notify), mail_domain=domain, send_from=sender, review_to=tuple(review),
                    link=link.rstrip("/"), per_hour=per_hour, problems=tuple(problems), tickets=numbers)


def open_records(path, write: bool = False):
    """lab_check.db, with these tables too when written."""
    from nomad import lab_check
    rec = lab_check.open_records(path, write)
    if rec is not None and write:
        rec.executescript(SCHEMA)
        T.ensure(rec)
    return rec


def ref_of(number, s: Settings | None = None) -> str:
    """A request's number as shown: ARC-0042."""
    return T.ref(number, (s or Settings()).tickets)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(rec, by: str, what: dict) -> None:
    rec.execute("INSERT INTO lab_check_log (at, by, what) VALUES (?, ?, ?)",
                (_now(), by, json.dumps(what, sort_keys=True)))


def _rows(rec, sql: str, args=()) -> list[dict]:
    if rec is None:
        return []
    try:
        return [dict(r) for r in rec.execute(sql, tuple(args))]
    except sqlite3.OperationalError:          # no such table yet: nothing asked
        return []


def _text(value, what: str, most: int, needed: bool = True) -> str:
    t = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if needed and not t:
        raise HelpError(f"{what} is empty")
    if len(t) > most:
        raise HelpError(f"{what} is longer than {most:,} characters")
    return t


def _who(by) -> str:
    b = str(by or "").strip().lower()
    if not _NETID.fullmatch(b):
        raise HelpError("who is asking isn't a username")
    return b


def asked_lately(rec, by: str, hours: float = 1.0) -> int:
    """How many requests ``by`` opened in the last ``hours``."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    got = _rows(rec, "SELECT COUNT(*) AS n FROM help_requests WHERE asked_by = ? AND asked_at >= ?",
                (str(by).lower(), since))
    return int(got[0]["n"]) if got else 0


def ask(rec, by: str, subject: str, message: str, about: str | None = None, site: str | None = None,
        job: str | None = None, page: str | None = None, context: str | None = None,
        s: Settings | None = None) -> dict:
    """Open a request: its number, kept with the person's first message and
    the context they chose (text, as they saw it). The request, as
    request_of gives it."""
    s = s or Settings()
    by = _who(by)
    subject = " ".join(_text(subject, "The subject", SUBJECT_MAX).split())
    message = _text(message, "The message", TEXT_MAX)
    context = _text(context, "The context", CONTEXT_MAX, needed=False) or None
    if about is not None and about not in ABOUT:
        raise HelpError(f"{about!r} isn't something a request is about")
    if asked_lately(rec, by) >= s.per_hour:
        raise HelpError(f"{s.per_hour} requests in the last hour: please wait a little before the next, "
                        "or add to one you have open")
    at = _now()
    with rec:
        number = T.take(rec, "help")
        rec.execute("INSERT INTO help_requests (number, asked_by, asked_at, subject, about, site, job, page, context, "
                    "state, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (number, by, at, subject, about, (site or None), (job or None), page[:200] if page else None, context, by, at))
        rec.execute("INSERT INTO help_messages (number, at, by, side, text) VALUES (?, ?, ?, 'person', ?)",
                    (number, at, by, message))
        _log(rec, by, {"help": number, "opened": subject[:120], "about": about, "site": site, "job": job})
    return request_of(rec, number, s)


def _request(row: dict, s: Settings | None, messages: list | None = None) -> dict:
    out = dict(row)
    out["ref"] = ref_of(row["number"], s)
    out["said"] = SAID.get(row["state"], row["state"]).format(team=(s or Settings()).team)
    if messages is not None:
        out["messages"] = messages
    return out


def request_of(rec, number: int, s: Settings | None = None, notes: bool = True) -> dict | None:
    """One request with its messages (ARC's notes too unless ``notes`` is
    False: what the person sees)."""
    got = _rows(rec, "SELECT * FROM help_requests WHERE number = ?", (int(number),))
    if not got:
        return None
    msgs = _rows(rec, "SELECT at, by, side, text, emailed FROM help_messages WHERE number = ? ORDER BY at, rowid",
                 (int(number),))
    if not notes:
        msgs = [m for m in msgs if m["side"] != "note"]
    return _request(got[0], s, msgs)


def requests(rec, by: str | None = None, open_only: bool = False, days: int = 365,
             s: Settings | None = None) -> list[dict]:
    """Requests, newest first: ``by``'s own, or everyone's; each with how many
    messages, and who said the last of them and when (notes not counted)."""
    where, args = ["asked_at >= ?"], [(datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")]
    if by is not None:
        where.append("asked_by = ?")
        args.append(str(by).lower())
    if open_only:
        where.append("state != 'closed'")
    rows = _rows(rec, f"SELECT * FROM help_requests WHERE {' AND '.join(where)} ORDER BY number DESC", args)
    last = {}
    for m in _rows(rec, "SELECT number, at, by, side FROM help_messages WHERE side != 'note' ORDER BY at, rowid"):
        k = m["number"]
        last[k] = {"n": last.get(k, {}).get("n", 0) + 1, "last_at": m["at"], "last_by": m["by"],
                   "last_side": m["side"]}
    out = []
    for r in rows:
        q = _request(r, s)
        q.update(last.get(r["number"], {"n": 0, "last_at": r["asked_at"], "last_by": r["asked_by"],
                                        "last_side": "person"}))
        q.pop("context", None)
        out.append(q)
    return out


def say(rec, number: int, by: str, side: str, text: str, state: str | None = None,
        s: Settings | None = None) -> dict:
    """A message on a request: the person's (it opens the request again),
    ARC's answer (the request is then answered, unless ``state`` says), or
    ARC's note (kept for ARC only; the state as it was, unless ``state``)."""
    by = _who(by)
    if side not in SIDES:
        raise HelpError(f"{side!r}: a message is the person's, ARC's or a note")
    if state is not None and state not in STATES:
        raise HelpError(f"{state!r} isn't a state")
    text = _text(text, "The message", TEXT_MAX)
    req = request_of(rec, number, s)
    if req is None:
        raise HelpError(f"there is no request {ref_of(number, s)}")
    if side == "person" and by != req["asked_by"]:
        raise HelpError("only the person who asked adds their own messages")
    new = state or {"person": "open", "arc": "answered", "note": req["state"]}[side]
    at = _now()
    with rec:
        rec.execute("INSERT INTO help_messages (number, at, by, side, text) VALUES (?, ?, ?, ?, ?)",
                    (int(number), at, by, side, text))
        rec.execute("UPDATE help_requests SET state = ?, updated_by = ?, updated_at = ? WHERE number = ?",
                    (new, by, at, int(number)))
        _log(rec, by, {"help": int(number), "said": side, "state": new})
    return request_of(rec, number, s)


def set_state(rec, number: int, by: str, state: str, s: Settings | None = None) -> dict:
    """Answered, closed, or open again; the person may close their own."""
    by = _who(by)
    if state not in STATES:
        raise HelpError(f"{state!r} isn't a state")
    req = request_of(rec, number, s)
    if req is None:
        raise HelpError(f"there is no request {ref_of(number, s)}")
    with rec:
        rec.execute("UPDATE help_requests SET state = ?, updated_by = ?, updated_at = ? WHERE number = ?",
                    (state, by, _now(), int(number)))
        _log(rec, by, {"help": int(number), "state": state})
    return request_of(rec, number, s)


def mark_emailed(rec, number: int, at: str, how: str) -> None:
    """What became of a message's email ("sent", "not sent: ...")."""
    with rec:
        rec.execute("UPDATE help_messages SET emailed = ? WHERE number = ? AND at = ? AND emailed IS NULL",
                    (how[:200], int(number), at))


# --- the emails ---------------------------------------------------------------------------

def person_address(netid: str, s: Settings) -> tuple[list, bool]:
    """(where an email for the person goes, reviewing): review_to while it is
    set, else NETID@mail_domain (none without a domain)."""
    if s.review_to:
        return list(s.review_to), True
    return ([f"{netid}@{s.mail_domain}"] if s.mail_domain and _NETID.fullmatch(netid or "") else []), False


def _link(req: dict, s: Settings) -> str:
    return f"{s.link}?request={req['number']}" if s.link else ""


def letter_to_arc(req: dict, text: str, s: Settings, first: bool) -> tuple[str, str]:
    """The email for ARC: a new request (``first``), or the person's next
    message on it."""
    ref = req["ref"]
    subject = f"[{ref}] {req['subject']}" if first else f"Re: [{ref}] {req['subject']}"
    lines = [f"{'Help request' if first else 'A new message on'} {ref} from {req['asked_by']}"
             + (f", about {ABOUT.get(req.get('about'), req.get('about'))}" if first and req.get("about") else "")
             + (f" (job {req['job']} on {req['site']})" if first and req.get("job") and req.get("site") else "")
             + ".", "", text.strip(), ""]
    if first and req.get("context"):
        lines += ["--- What they attached (as nomad read it when they sent this) ---", req["context"].strip(), ""]
    if first and req.get("page"):
        lines += [f"Sent from the Console's page {req['page']}.", ""]
    link = _link(req, s)
    lines += ([f"Answer it on the Console, where it is kept with the request: {link}"] if link
              else [f"Answer it on the Console's Ask {s.team} page, where it is kept with the request."])
    lines += [f"Keep {ref} in the subject of anything you send about it."]
    return subject, "\n".join(lines) + "\n"


def letter_to_person(req: dict, text: str | None, s: Settings, first: bool) -> tuple[str, str]:
    """The email for the person: their copy of a new request (``first``,
    ``text`` their message), or ARC's answer (``text``)."""
    ref = req["ref"]
    subject = f"[{ref}] {req['subject']}" if first else f"Re: [{ref}] {req['subject']}"
    link = _link(req, s)
    if first:
        lines = [f"{s.team} has your request, {ref}: \"{req['subject']}\".", "",
                 "What you sent:", "", (text or "").strip(), "",
                 f"You'll hear back here by email, and on the Console's Ask {s.team} page"
                 + (f" ({link})." if link else "."),
                 f"To add something, write there, or reply to this email keeping {ref} in the subject."]
    else:
        lines = [f"{s.team} answered your request {ref}: \"{req['subject']}\".", "",
                 (text or "").strip(), "",
                 f"To answer, write on the Console's Ask {s.team} page" + (f" ({link})" if link else "")
                 + f", or reply to this email keeping {ref} in the subject."]
    return subject, "\n".join(lines) + "\n"


def send(config, s: Settings, subject: str, text: str, to: list, reply_to: list | None = None,
         review_for: str | None = None) -> str:
    """Send one email (nomad.mail); what became of it, in words. ``review_for``:
    whom it was for, said at the top, while it goes to review_to instead."""
    from email.message import EmailMessage

    from nomad import mail
    try:
        cfg = mail.settings(config)
    except ValueError as exc:
        return f"not sent: {exc}"
    sender = s.send_from or cfg.get("from")
    if not sender:
        return "not sent: no sender ([mail] from, or send_from in [console.help])"
    if not to:
        return "not sent: no address to send to"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg["Subject"] = ("[review] " if review_for else "") + subject
    if review_for:
        text = f"[For review: this would have gone to {review_for}.]\n\n" + text
    msg.set_content(text)
    try:
        mail.send(msg, cfg)
    except Exception as exc:          # noqa: BLE001 -- the request is kept; the email said not sent
        return "not sent: " + mail.failure_label(exc)
    return "sent"


def tell_arc(config, s: Settings, req: dict, text: str, first: bool) -> str:
    """Email ARC (``notify``): a new request, or the person's next message;
    the person's address as Reply-To."""
    if not s.notify:
        return "not sent: no one to tell ([console.help] notify, or [support] email)"
    subject, body = letter_to_arc(req, text, s, first)
    reply = [f"{req['asked_by']}@{s.mail_domain}"] if s.mail_domain else None
    return send(config, s, subject, body, list(s.notify), reply_to=reply)


def tell_person(config, s: Settings, req: dict, text: str, first: bool) -> str:
    """Email the person: their copy of the request, or ARC's answer; ARC's
    address as Reply-To."""
    to, reviewing = person_address(req["asked_by"], s)
    subject, body = letter_to_person(req, text, s, first)
    whom = f"{req['asked_by']}@{s.mail_domain}" if s.mail_domain else req["asked_by"]
    return send(config, s, subject, body, to, reply_to=list(s.notify) or None,
                review_for=whom if reviewing else None)


__all__ = ["DEFAULT_TEAM", "HelpError", "Settings", "settings", "open_records", "ref_of", "asked_lately", "ask", "request_of",
           "requests", "say", "set_state", "mark_emailed", "person_address", "letter_to_arc", "letter_to_person",
           "send", "tell_arc", "tell_person", "STATES", "SAID", "SIDES", "ABOUT"]
