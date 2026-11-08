# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Each term, a lab's PI says who is still in it (1.33.0).

Lab groups go on listing people long after they leave, and people work in a
lab without being in its group. Once a term, on dates nomad.toml sets, nomad
asks each lab's PI two things, on My lab in the Console:

- each member (in the lab's group now, on any site, the PI apart): still in
  my lab, has left, or not in my lab. For one who has left: keep their data
  for the lab, it can be removed, or ask me first.
- each person who sent jobs to one of the lab's condos lately without being
  in its group: in my lab, or a guest (another lab's member using the condo).

The answers become ARC's to-do, a cluster at a time: take someone out of a
group, put someone in one, what to do with a leaver's data (where it is, and
how big), and the labs that didn't answer. A group change is done once
nomad's own groups reading shows it; the rest is marked done by someone at
ARC, with a note. nomad never changes a group, an account or a file itself.

PIs are told by email when the check opens, and reminded while they haven't
answered; while ``review_to`` is set, every email goes there instead, saying
whom it was for.

Settings, in the hub's nomad.toml::

    [console.labs.check]
    opens = ["09-15", "02-01", "06-15"]   # MM-DD: the day each term's check opens; [] (default): off
    remind_days = [14, 28]               # days after it opens: reminders to PIs who haven't answered
    closes_days = 42                     # days after it opens: unanswered, the lab is on ARC's to-do
    claim_days = 120                     # condo users of the last this many days are asked about
    link = "https://nomad.example.edu/lab"   # where PIs answer, in the email
    mail_domain = "example.edu"          # a PI's address: NETID@mail_domain
    send_from = "Research Computing <rc@example.edu>"
    review_to = ["rc@example.edu"]       # while set, every email goes here instead
    # db = "/home/nomad/.local/share/nomad/lab_check.db"   # the answers (default: beside combined.db)

``mail_domain``, ``send_from`` and ``review_to`` not given here are
report.toml's [report.labs] (the condo reports'): ``review_to = []`` here
sends to PIs whatever report.toml says.

The answers are kept in their own small database on the hub (the nomad
account's, beside combined.db, which sync rewrites): every answer, who gave
it and when, and every change in a log.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

MEMBER_ANSWERS = ("here", "left", "not_mine")
CLAIM_ANSWERS = ("mine", "guest")
DATA_ANSWERS = ("keep", "remove", "ask")
KINDS = ("remove", "add", "data", "no_answer")
SAID = {"here": "still in the lab", "left": "has left the lab", "not_mine": "not in the lab",
        "mine": "in the lab", "guest": "a guest, not in the lab",
        "keep": "keep their data for the lab", "remove": "their data can be removed",
        "ask": "ask the PI first about their data"}
_MMDD = re.compile(r"^(\d\d)-(\d\d)$")
_NETID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\$?$")
_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$|^[^<>\n]*<[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>$")
NOTE_MAX = 300
_KEYS = ("opens", "remind_days", "closes_days", "claim_days", "link", "mail_domain", "send_from", "review_to",
         "db")


class CheckError(ValueError):
    """The check can't do what was asked: what is wrong, in words."""


@dataclass(frozen=True)
class Settings:
    opens: tuple = ()                    # "MM-DD", in the year's order
    remind_days: tuple = (14, 28)
    closes_days: int = 42
    claim_days: int = 120
    link: str | None = None
    mail_domain: str | None = None
    send_from: str | None = None
    review_to: tuple = ()
    review_broken: bool = False          # review_to given, but no address in it works: nothing is sent
    db: Path | None = None
    problems: tuple = ()

    @property
    def on(self) -> bool:
        return bool(self.opens)

    @property
    def reviewing(self) -> bool:
        return bool(self.review_to) or self.review_broken


@dataclass(frozen=True)
class Round:
    """One term's check: it opens on ``opened`` (its name), and is the
    term's until the next one opens."""
    id: str                              # YYYY-MM-DD, the day it opened
    opened: date
    closes: date                         # unanswered after this: on ARC's to-do
    next_opens: date

    def remind_on(self, days: int) -> date:
        return self.opened + timedelta(days=days)


def _int(value, what: str, lo: int, hi: int, problems: list, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        problems.append(f"[console.labs.check] {what}: a whole number from {lo} to {hi}, not {value!r}")
        return default
    return value


def settings(config: dict | None, report=None, report_problem: str | None = None) -> Settings:
    """[console.labs.check] of nomad.toml; ``report``: report.toml's
    settings (nomad.condo.config.Settings) for the mail settings not given
    here. What can't be used is listed in ``problems`` and left out.
    ``report_problem``: report.toml is there but doesn't read; then, unless
    review_to is given here, nothing is sent (its review_to may be what
    holds the emails), and its condos are left out (said so)."""
    raw = (((config or {}).get("console") or {}).get("labs") or {}).get("check")
    if raw is None:
        raw = {}
    problems: list[str] = []
    if not isinstance(raw, dict):
        return Settings(problems=("[console.labs.check] is a table",))
    for k in raw:
        if k not in _KEYS:
            problems.append(f"[console.labs.check] {k}: not a setting here (left out)")
    opens = []
    given = raw.get("opens", [])
    if not isinstance(given, list):
        problems.append("[console.labs.check] opens: a list of \"MM-DD\"")
        given = []
    for v in given:
        m = _MMDD.match(str(v)) if isinstance(v, str) else None
        try:
            ok = m is not None and date(2024, int(m.group(1)), int(m.group(2))) is not None
        except ValueError:
            ok = False
        if not ok:
            problems.append(f"[console.labs.check] opens: {v!r} is not a day of the year as MM-DD")
        elif v not in opens:
            opens.append(v)
    opens.sort()
    remind = raw.get("remind_days", [14, 28])
    if not isinstance(remind, list) or not all(isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 120
                                                for d in remind):
        problems.append("[console.labs.check] remind_days: a list of days from 1 to 120")
        remind = [14, 28]
    closes = _int(raw.get("closes_days"), "closes_days", 1, 180, problems, 42)
    claim = _int(raw.get("claim_days"), "claim_days", 7, 730, problems, 120)
    link = raw.get("link")
    if link is not None and (not isinstance(link, str) or not re.match(r"^https?://[^\s<>\"']+$", link)):
        problems.append("[console.labs.check] link: an http(s) address")
        link = None
    delivery = getattr(report, "delivery", None)

    def text(key, fallback):
        v = raw.get(key, fallback)
        if v is not None and not isinstance(v, str):
            problems.append(f"[console.labs.check] {key}: text")
            return fallback
        return (v or "").strip() or None
    mail_domain = text("mail_domain", getattr(delivery, "mail_domain", None))
    if mail_domain and not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", mail_domain):
        problems.append(f"[console.labs.check] mail_domain: {mail_domain!r} is not a domain")
        mail_domain = None
    send_from = text("send_from", getattr(delivery, "send_from", None))
    if send_from and not _ADDRESS.match(send_from):
        problems.append(f"[console.labs.check] send_from: {send_from!r} is not an address")
        send_from = None
    if "review_to" in raw:
        review = raw["review_to"]
    else:
        review = list(getattr(delivery, "review_to", None) or [])
    if isinstance(review, str):
        review = [review]
    if not isinstance(review, list):
        problems.append("[console.labs.check] review_to: a list of addresses")
        review = []
    good = []
    for a in review:
        if isinstance(a, str) and _ADDRESS.match(a.strip()):
            good.append(a.strip())
        else:
            problems.append(f"[console.labs.check] review_to: {a!r} is not an address (left out)")
    if review and not good:
        # Asked to review, with no address that works: send nothing to PIs
        # rather than everything.
        problems.append("[console.labs.check] review_to has no address that works: nothing is sent")
    held = bool(review) and not good
    if report_problem:
        problems.append(f"report.toml doesn't read ({report_problem}): its condos are left out")
        if "review_to" not in raw:
            problems.append("report.toml doesn't read, and its review_to may be what holds the emails: "
                            "nothing is sent until it reads (or review_to is set in [console.labs.check])")
            held = True
    db = raw.get("db")
    if db is not None and not isinstance(db, str):
        problems.append("[console.labs.check] db: a path")
        db = None
    return Settings(opens=tuple(opens), remind_days=tuple(sorted(set(remind))), closes_days=closes,
                    claim_days=claim, link=link, mail_domain=mail_domain, send_from=send_from,
                    review_to=tuple(good), review_broken=held,
                    db=Path(db).expanduser() if db else None, problems=tuple(problems))


def round_of(s: Settings, today: date) -> Round | None:
    """The term's check on ``today``: the last opening on or before it; None
    when the check is off."""
    if not s.opens:
        return None
    days = []
    for year in (today.year - 1, today.year, today.year + 1):
        for v in s.opens:
            mm, dd = int(v[:2]), int(v[3:])
            try:
                days.append(date(year, mm, dd))
            except ValueError:            # 29 February in a year without one
                days.append(date(year, mm, 28))
    days.sort()
    past = [d for d in days if d <= today]
    opened = past[-1]
    nxt = next(d for d in days if d > opened)
    # Closed a week before the next opens at the latest: a lab that didn't
    # answer is on the to-do for a while before the next term asks again.
    closes = max(opened, min(opened + timedelta(days=s.closes_days), nxt - timedelta(days=7)))
    return Round(id=opened.isoformat(), opened=opened, closes=closes, next_opens=nxt)


# --- where the answers are kept --------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_check_answers (
    round TEXT NOT NULL, lab TEXT NOT NULL, netid TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- member | claim
    answer TEXT NOT NULL, data TEXT, note TEXT,
    by TEXT NOT NULL, as_role TEXT, at TEXT NOT NULL,
    PRIMARY KEY (round, lab, netid));
CREATE TABLE IF NOT EXISTS lab_check_submitted (
    round TEXT NOT NULL, lab TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (round, lab));
CREATE TABLE IF NOT EXISTS lab_check_done (
    round TEXT NOT NULL, lab TEXT NOT NULL, netid TEXT NOT NULL, kind TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT '', by TEXT NOT NULL, at TEXT NOT NULL, note TEXT,
    PRIMARY KEY (round, lab, netid, kind, site));
CREATE TABLE IF NOT EXISTS lab_check_mail (
    round TEXT NOT NULL, lab TEXT NOT NULL, kind TEXT NOT NULL,
    sent_to TEXT NOT NULL, review INTEGER NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (round, lab, kind));
CREATE TABLE IF NOT EXISTS lab_check_log (
    at TEXT NOT NULL, by TEXT NOT NULL, what TEXT NOT NULL);
"""


def db_path(s: Settings, config_target=None) -> Path:
    """Where the answers are: ``db``, else lab_check.db beside the hub's
    combined database."""
    from nomad.actions.remote import hub_settings
    here = hub_settings(config_target)["combined_db"].parent
    if s.db is not None:
        # A relative path is beside combined.db, wherever the command runs from.
        return s.db if s.db.is_absolute() else here / s.db
    return here / "lab_check.db"


def open_records(path: Path, write: bool = False) -> sqlite3.Connection | None:
    """The answers' database; None when there is none yet and only reading.
    Made when first written, readable by its owner only (it names people):
    as the account the Console runs as (root would make it root's)."""
    path = Path(path)
    if not path.exists():
        if not write:
            return None
        if os.geteuid() == 0:
            raise CheckError(f"{path} would be root's: write it as the account the Console runs as "
                             "(sudo -u nomad nomad lab check ...)")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    if write and os.geteuid() == 0 and path.stat().st_uid != 0:
        raise CheckError(f"{path} is another account's: write it as that account (sudo -u nomad ...)")
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if write:
        conn.executescript(SCHEMA)
    return conn


def _when(value) -> datetime | None:
    """An ISO time as the collectors and this module write it; None if not one."""
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")) if value else None
    except ValueError:
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(conn, by: str, what: dict) -> None:
    conn.execute("INSERT INTO lab_check_log (at, by, what) VALUES (?, ?, ?)",
                 (_now(), by, json.dumps(what, sort_keys=True)))


def _rows(conn, sql: str, args=()) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args))]
    except sqlite3.OperationalError:     # no such table yet: nothing recorded
        return []


# --- who is asked about ------------------------------------------------------------------

def _clean(netid) -> str | None:
    v = str(netid or "").strip().lower()
    return v if _NETID.match(v) else None


def by_site(conn) -> dict:
    """group -> site -> {netid: (current, has_account, first_seen)}: the
    groups collector's rows, a site at a time, a member counted as
    nomad.config.access counts one: listed in its cluster's latest reading,
    give or take MEMBER_WINDOW_DAYS, at a cluster read within
    CLUSTER_RETIRED_DAYS of its site's latest. A site with several clusters:
    current if any says so. first_seen: when this stretch of membership
    began (the collector starts it again after a gap); None when not known."""
    from nomad.config.access import CLUSTER_RETIRED_DAYS, MEMBER_WINDOW_DAYS
    cols = {r[1] for r in conn.execute("PRAGMA table_info(group_membership)")}
    if not cols:
        return {}
    site = "source_site" if "source_site" in cols else "NULL"
    cluster = "cluster" if "cluster" in cols else "NULL"
    stamp = "julianday(collected_at)" if "collected_at" in cols else "NULL"
    account = "has_account" if "has_account" in cols else "NULL"
    first = "first_seen" if "first_seen" in cols else "NULL"
    latest = {(s, c): t for s, c, t in conn.execute(
        f"SELECT {site}, {cluster}, MAX({stamp}) FROM group_membership GROUP BY 1, 2")}
    site_latest: dict = {}
    for (s, _), t in latest.items():
        if t is not None and (site_latest.get(s) is None or t > site_latest[s]):
            site_latest[s] = t
    out: dict = {}
    for user, group, s, c, t, acc, seen in conn.execute(
            f"SELECT username, group_name, {site}, {cluster}, {stamp}, {account}, {first} FROM group_membership"):
        n = _clean(user)
        if not n or not group:
            continue
        top, site_top = latest.get((s, c)), site_latest.get(s)
        current = (t is None or top is None
                   or (t >= top - MEMBER_WINDOW_DAYS
                       and (site_top is None or top >= site_top - CLUSTER_RETIRED_DAYS)))
        here = out.setdefault(group, {}).setdefault(s or c or "", {})
        was = here.get(n)
        acc = None if acc is None else bool(acc)
        if was is None:
            here[n] = (current, acc, seen if current else None)
            continue
        cur = was[0] or current
        account_now = True if (was[1] or acc) else (False if (was[1] is False and acc is False) else None)
        seens = [x for x in (was[2], seen if current else None) if x]
        here[n] = (cur, account_now, min(seens) if seens else None)
    return out


def _inside(entry) -> bool:
    """In the group now at a site: listed now, with an account (or not known)."""
    return bool(entry and entry[0] and entry[1] is not False)


def _members(sites: dict) -> set:
    """Who counts as a member anywhere."""
    return {n for people in sites.values() for n, e in people.items() if _inside(e)}


def condo_labs(report, acc, groups: dict, owners: dict | None = None) -> dict:
    """lab group -> [(site, partition, condo name)]: the condos each lab
    holds by report.toml (its ``members`` groups, else its owners' groups as
    group_pattern names them). A condo given people by name or account has
    no group to put anyone in: left out. ``owners``, when given, is filled
    with group -> the condos' owners (who answer for it as its PIs)."""
    from nomad.condo.config import member_groups
    out: dict = {}
    for cname, cl in (getattr(report, "clusters", None) or {}).items():
        for name, c in cl.condos.items():
            gs = member_groups(c.members)
            if not gs and c.members is None and acc.group_pattern and "{netid}" in acc.group_pattern:
                gs = [acc.group_pattern.replace("{netid}", o) for o in c.owners]
                gs = [g for g in gs if g in groups]
            for g in gs:
                out.setdefault(g, []).append((cname, c.part, name))
                if owners is not None:
                    owners.setdefault(g, set()).update(o for o in (_clean(x) for x in c.owners) if o)
    return out


def _condo_users(conn, places: list, since: str) -> dict:
    """netid -> {site: [partitions]}: who sent jobs to these condos'
    partitions since ``since``."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if not {"user_name", "partition", "submit_time"} <= cols:
        return {}
    site_col = "source_site" if "source_site" in cols else None
    out: dict = {}
    for site in sorted({p[0] for p in places}):
        parts = {p[1] for p in places if p[0] == site}
        where = " AND source_site = ?" if site_col else ""
        for user, part in conn.execute(
                f"SELECT DISTINCT user_name, partition FROM jobs WHERE submit_time >= ?{where}",
                [since] + ([site] if site_col else [])):
            n = _clean(user)
            if not n:
                continue
            hit = sorted(parts & {x.strip() for x in str(part or "").split(",")})
            if hit:
                got = out.setdefault(n, {}).setdefault(site, [])
                got.extend(x for x in hit if x not in got)
    return out


@dataclass
class Lab:
    group: str
    name: str
    leaders: tuple
    members: dict = field(default_factory=dict)      # netid -> {site: in the group now there}
    claims: dict = field(default_factory=dict)       # netid -> {site: [condo partitions]}
    condos: list = field(default_factory=list)       # [(site, partition, condo)]

    def questions(self) -> list:
        return [("member", n) for n in sorted(self.members)] + [("claim", n) for n in sorted(self.claims)]


def labs(conn, config: dict | None, report=None, s: Settings | None = None,
         now: datetime | None = None, only: set | None = None) -> list[Lab]:
    """Every lab with someone to ask about, by nomad's rules for labs
    ([console.labs]: group_pattern, leads, not_labs; not a course this
    term), and a PI to ask: its members now but whoever leads it, and who
    used its condos in the last ``claim_days`` without being in any of its
    groups. ``only``: these groups."""
    from nomad.config.access import access_from
    now = now or datetime.now()
    s = s or settings(config, report)
    acc = access_from(config or {}, log=False)
    groups = by_site(conn) if conn is not None else {}
    members_of = (lambda g: _members(groups.get(g, {})))
    owners: dict = {}
    held = condo_labs(report, acc, groups, owners) if report is not None else {}
    holders: dict = {}                   # (site, partition) -> the lab groups holding that condo
    for g, places in held.items():
        for site, part, _name in places:
            holders.setdefault((site, part), set()).add(g)
    since = (now - timedelta(days=s.claim_days)).isoformat(timespec="seconds")

    def leaders_of(g) -> set:
        # A lab by nomad's rules: who leads it (group_pattern, leads); a
        # condo's group otherwise only its leads entries (the pattern would
        # make a PI of a group named for no one: "chemlab$" -> chemlab).
        # A condo's owners answer for its group too.
        if acc.is_lab(g, members_of):
            got = set(acc.leaders(g))
        else:
            got = {n for n, gs in acc.leads.items() if g in gs}
        return got | owners.get(g, set())
    out = []
    for g in sorted(set(groups) | set(held)):
        if only is not None and g not in only:
            continue
        if not (acc.is_lab(g, members_of) or g in held) or g in acc.not_labs or not acc.lab_now(g):
            continue
        leaders = tuple(sorted(leaders_of(g)))
        if not leaders:
            continue
        lab = Lab(group=g, name=acc.lab_names.get(g) or g, leaders=leaders, condos=sorted(held.get(g, [])))
        sites = groups.get(g, {})
        everyone = _members(sites)
        for n in sorted(everyone - set(leaders)):
            lab.members[n] = {site: _inside(people.get(n)) for site, people in sites.items() if n in people}
        if lab.condos and conn is not None:
            for n, where in _condo_users(conn, lab.condos, since).items():
                if n in everyone:
                    continue
                # In any group holding that very condo is in: a condo two labs
                # share asks neither about the other's people.
                kept = {}
                for site, parts in where.items():
                    out_of = [x for x in parts if not any(
                        n in members_of(h) or n in leaders_of(h) for h in holders.get((site, x), ()))]
                    if out_of:
                        kept[site] = out_of
                if kept:
                    lab.claims[n] = kept
        if lab.members or lab.claims:
            out.append(lab)
    return out


# --- answers -----------------------------------------------------------------------------

def answers(rec, round_id: str | None = None, lab: str | None = None) -> list[dict]:
    """The answers kept: of a round, of a lab, or all."""
    where, args = [], []
    if round_id:
        where.append("round = ?")
        args.append(round_id)
    if lab:
        where.append("lab = ?")
        args.append(lab)
    sql = "SELECT * FROM lab_check_answers" + (" WHERE " + " AND ".join(where) if where else "")
    return _rows(rec, sql + " ORDER BY round, lab, netid", args)


def submitted(rec, round_id: str) -> dict:
    return {r["lab"]: r for r in _rows(rec, "SELECT * FROM lab_check_submitted WHERE round = ?", (round_id,))}


def _latest(rec) -> dict:
    """(lab, netid) -> the answer given last, whatever round it was kept
    under (a change to ``opens`` mid-term names rounds otherwise)."""
    out: dict = {}
    for r in _rows(rec, "SELECT * FROM lab_check_answers ORDER BY at, round"):
        out[(r["lab"], r["netid"])] = r
    return out


def validate(lab: Lab, netid: str, answer: str, data: str | None = None) -> tuple[str, str]:
    """(netid, kind) for an answer that fits a question the lab has now (a
    member: here, left or not_mine, and with left what to do with their
    data; a condo user: mine or guest); CheckError otherwise."""
    n = _clean(netid)
    if not n:
        raise CheckError(f"{netid!r} is not a NetID")
    if n in lab.members:
        kind = "member"
        if answer not in MEMBER_ANSWERS:
            raise CheckError(f"{n} is one of {lab.name}'s members: the answer is one of {', '.join(MEMBER_ANSWERS)}")
    elif n in lab.claims:
        kind = "claim"
        if answer not in CLAIM_ANSWERS:
            raise CheckError(f"{n} used {lab.name}'s condo: the answer is one of {', '.join(CLAIM_ANSWERS)}")
    else:
        raise CheckError(f"{n} is not someone {lab.name}'s check asks about now")
    if answer == "left":
        if data not in DATA_ANSWERS:
            raise CheckError(f"what to do with {n}'s data: one of {', '.join(DATA_ANSWERS)}")
    elif data is not None:
        raise CheckError("what to do with someone's data is asked only of someone who has left")
    return n, kind


def record(rec, rnd: Round, lab: Lab, netid: str, answer: str, data: str | None = None,
           note: str | None = None, by: str = "", as_role: str | None = None) -> dict:
    """Keep one answer of this round (``validate`` says which fit)."""
    n, kind = validate(lab, netid, answer, data)
    note = clean_note(note)
    who = _clean(by) or (by or "").strip() or getpass.getuser()
    row = {"round": rnd.id, "lab": lab.group, "netid": n, "kind": kind, "answer": answer, "data": data,
           "note": note, "by": who, "as_role": as_role, "at": _now()}
    with rec:
        rec.execute("INSERT INTO lab_check_answers (round, lab, netid, kind, answer, data, note, by, as_role, at) "
                    "VALUES (:round, :lab, :netid, :kind, :answer, :data, :note, :by, :as_role, :at) "
                    "ON CONFLICT(round, lab, netid) DO UPDATE SET kind = excluded.kind, answer = excluded.answer, "
                    "data = excluded.data, note = excluded.note, by = excluded.by, as_role = excluded.as_role, "
                    "at = excluded.at", row)
        _log(rec, who, {"answer": row})
    return row


def submit(rec, rnd: Round, lab: Lab, by: str = "", as_role: str | None = None) -> dict:
    """The PI is done with this round: every question answered."""
    have = {r["netid"] for r in answers(rec, rnd.id, lab.group)}
    missing = [n for _, n in lab.questions() if n not in have]
    if missing:
        raise CheckError(f"{len(missing)} still to answer: {', '.join(missing[:5])}"
                         + (" ..." if len(missing) > 5 else ""))
    who = _clean(by) or (by or "").strip() or getpass.getuser()
    row = {"round": rnd.id, "lab": lab.group, "by": who, "at": _now()}
    with rec:
        rec.execute("INSERT INTO lab_check_submitted (round, lab, by, at) VALUES (:round, :lab, :by, :at) "
                    "ON CONFLICT(round, lab) DO UPDATE SET by = excluded.by, at = excluded.at", row)
        _log(rec, who, {"submitted": row, "as_role": as_role})
    return row


def clean_note(note) -> str | None:
    if note is None:
        return None
    text = " ".join(str(note).split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:NOTE_MAX] or None


def view(conn, rec, config: dict | None, report, lab_group: str, now: datetime | None = None) -> dict | None:
    """What a lab's PI sees this term: the round, each question with this
    round's answer and the one before, and whether they are done. None:
    the check is off, or the lab has nothing to ask about."""
    now = now or datetime.now()
    s = settings(config, report)
    rnd = round_of(s, now.date())
    if rnd is None:
        return None
    found = labs(conn, config, report, s, now, only={lab_group})
    if not found:
        return None
    lab = found[0]
    mine = {r["netid"]: r for r in answers(rec, rnd.id, lab.group)}
    before = {}
    for r in _rows(rec, "SELECT * FROM lab_check_answers WHERE lab = ? AND round < ? ORDER BY round, at",
                   (lab.group, rnd.id)):
        before[r["netid"]] = r
    qs = []
    for kind, n in lab.questions():
        a = mine.get(n)
        b = before.get(n)
        q = {"netid": n, "kind": kind,
             "answer": a["answer"] if a else None, "data": a["data"] if a else None,
             "note": a["note"] if a else None, "by": a["by"] if a else None, "at": a["at"] if a else None,
             "last_term": {"round": b["round"], "answer": b["answer"], "data": b["data"]} if b else None}
        if kind == "member":
            q["sites"] = sorted(site for site, inside in lab.members[n].items() if inside)
        else:
            q["used"] = [{"site": site, "partitions": parts} for site, parts in sorted(lab.claims[n].items())]
        qs.append(q)
    done = submitted(rec, rnd.id).get(lab.group)
    return {"round": rnd.id, "opened": rnd.opened.isoformat(), "closes": rnd.closes.isoformat(),
            "next_opens": rnd.next_opens.isoformat(), "lab": lab.group, "name": lab.name,
            "leaders": list(lab.leaders), "condos": [{"site": c[0], "partition": c[1], "condo": c[2]}
                                                     for c in lab.condos],
            "questions": qs, "answered": sum(1 for q in qs if q["answer"]), "asked": len(qs),
            "submitted": {"by": done["by"], "at": done["at"]} if done else None,
            "claim_days": s.claim_days}


# --- ARC's to-do -------------------------------------------------------------------------

def _done(rec) -> dict:
    return {(r["round"], r["lab"], r["netid"], r["kind"], r["site"]): r
            for r in _rows(rec, "SELECT * FROM lab_check_done")}


def todo(conn, rec, config: dict | None, report, now: datetime | None = None, done_days: int = 30,
         report_problem: str | None = None) -> dict:
    """What ARC is left to do by the PIs' answers, a cluster at a time,
    from the answer given last for each person:

    - remove: has left, or not in the lab, and in the lab's group at a site
      since before that answer: done once nomad's groups reading there no
      longer lists them (or marked done: someone kept on purpose). Put
      back in the group after the answer, they are someone to ask about
      again, not to take out;
    - add: in the lab, not in its group at a site where they used its
      condo (every site of its condos when they no longer show among its
      condo's users): done once the reading lists them;
    - data: has left, with what to do with their data, and where it is:
      done when marked;
    - no_answer: this term's check closed with questions unanswered: done
      once answered, or marked.

    A mark made before the answer it is for changed doesn't cover the new
    one. ``done``: what was marked done in the last ``done_days``."""
    from nomad.config.access import access_from
    now = now or datetime.now()
    s = settings(config, report, report_problem)
    groups = by_site(conn) if conn is not None else {}
    held = condo_labs(report, access_from(config or {}, log=False), groups) if report is not None else {}
    found = {lab.group: lab for lab in labs(conn, config, report, s, now)} if conn is not None else {}
    marks = _done(rec)
    items, closed = [], []

    def add(item: dict):
        key = (item["round"], item["lab"], item["netid"], item["kind"], item.get("site") or "")
        mark = marks.get(key)
        # A mark for the answer as it was: the PI changed it since, so it is to do again.
        if mark and (item.get("at") is None or mark["at"] >= item["at"]):
            item["done"] = {"by": mark["by"], "at": mark["at"], "note": mark["note"]}
            if (now - datetime.fromisoformat(mark["at"])).days <= done_days:
                closed.append(item)
            return
        items.append(item)

    data_of = None
    for (lab_g, n), a in sorted(_latest(rec).items()):
        sites = groups.get(lab_g, {})
        base = {"round": a["round"], "lab": lab_g, "netid": n, "answer": a["answer"], "by": a["by"],
                "as_role": a["as_role"], "at": a["at"], "note": a["note"]}
        if a["answer"] in ("left", "not_mine"):
            for site, people in sorted(sites.items()):
                e = people.get(n)
                if not _inside(e):
                    continue
                if _when(e[2]) and _when(a["at"]) and _when(e[2]) > _when(a["at"]):
                    continue                 # put back since: asked about again, not taken out
                add({**base, "kind": "remove", "site": site, "what": f"take {n} out of {lab_g} on {site}"})
        if a["answer"] == "mine":
            used = found.get(lab_g).claims.get(n) if lab_g in found else None
            for site in sorted(used or {p[0] for p in held.get(lab_g, [])}):
                if not _inside(sites.get(site, {}).get(n)):
                    add({**base, "kind": "add", "site": site, "what": f"put {n} in {lab_g} on {site}"})
        if a["answer"] == "left" and a["data"]:
            if data_of is None:
                data_of = _data_places(conn)
            add({**base, "kind": "data", "site": "", "data": a["data"],
                 "what": f"{n}'s data: {SAID[a['data']]}", "where": data_of(n)})
    rnd = round_of(s, now.date())
    if rnd is not None and now.date() > rnd.closes:
        have = answers(rec, rnd.id)
        done_labs = submitted(rec, rnd.id)
        for lab in found.values():
            got = {r["netid"] for r in have if r["lab"] == lab.group}
            left = [n for _, n in lab.questions() if n not in got]
            if left and lab.group not in done_labs:
                add({"round": rnd.id, "lab": lab.group, "netid": "", "kind": "no_answer", "site": "",
                     "what": f"{lab.name}: {len(left)} of {len(lab.questions())} not answered",
                     "leaders": list(lab.leaders), "at": None})
    return {"items": items, "done": closed, "round": rnd.id if rnd else None}


def _data_places(conn):
    """netid -> [{server, path, used_bytes}]: where a person's data is, by
    the storage_use collector; [] when not read."""
    def none(_n):
        return []
    if conn is None:
        return none
    try:
        from nomad.storage import views
        if not views.available(conn):
            return none
    except Exception:          # noqa: BLE001 -- an older or odd database: say nothing of storage
        return none

    def of(n: str) -> list:
        try:
            return [{"server": r["server"], "path": r["path"], "used_bytes": r["used_bytes"]}
                    for r in views.person(conn, n) if not r.get("copy_of")]
        except Exception:      # noqa: BLE001
            return []
    return of


def mark_done(rec, item_round: str, lab: str, netid: str, kind: str, site: str = "",
              by: str = "", note: str | None = None) -> dict:
    """ARC says a to-do item is done (or won't be, and why): kept with
    who and when."""
    if kind not in KINDS:
        raise CheckError(f"{kind!r}: one of {', '.join(KINDS)}")
    note = clean_note(note)
    if not note:
        raise CheckError("say what was done, in a note")
    who = _clean(by) or (by or "").strip() or getpass.getuser()
    row = {"round": item_round, "lab": lab, "netid": _clean(netid) or "", "kind": kind, "site": site or "",
           "by": who, "at": _now(), "note": note}
    with rec:
        rec.execute("INSERT INTO lab_check_done (round, lab, netid, kind, site, by, at, note) VALUES "
                    "(:round, :lab, :netid, :kind, :site, :by, :at, :note) ON CONFLICT(round, lab, netid, kind, site) "
                    "DO UPDATE SET by = excluded.by, at = excluded.at, note = excluded.note", row)
        _log(rec, who, {"done": row})
    return row


# --- email ---------------------------------------------------------------------------------

def mail_plan(conn, rec, config: dict | None, report, now: datetime | None = None,
              report_problem: str | None = None) -> list[dict]:
    """The emails due today: each lab with questions, when its round opens
    and at each reminder while it isn't done (every question answered, or
    sent in), until the round closes. One email a lab, to each of its PIs;
    while reviewing, to review_to. What went for review is kept apart
    (kind "review-open", ...): once review_to is cleared, PIs get their own,
    the opening first."""
    now = now or datetime.now()
    s = settings(config, report, report_problem)
    rnd = round_of(s, now.date())
    if rnd is None:
        return []
    pre = "review-" if s.reviewing else ""
    sent = {(r["lab"], r["kind"]) for r in _rows(rec, "SELECT * FROM lab_check_mail WHERE round = ?", (rnd.id,))}
    have = answers(rec, rnd.id)
    done_labs = submitted(rec, rnd.id)
    today = now.date()
    if today > rnd.closes:
        return []                          # closed: on ARC's to-do now, not asked by email
    plan = []
    for lab in labs(conn, config, report, s, now):
        got = {r["netid"] for r in have if r["lab"] == lab.group}
        left = [n for _, n in lab.questions() if n not in got]
        if not left or lab.group in done_labs:
            continue
        due = []
        if (lab.group, pre + "open") not in sent:
            due.append("open")
        else:
            for d in s.remind_days:
                if today >= rnd.remind_on(d) and (lab.group, f"{pre}remind{d}") not in sent:
                    due.append(f"remind{d}")
        if not due:
            continue
        kind = due[-1]                     # one email a day: the latest that is due
        pis = [f"{p}@{s.mail_domain}" for p in lab.leaders] if s.mail_domain else []
        to = list(s.review_to) if s.reviewing else pis
        problem = None
        if s.review_broken:
            problem = "the emails are held: " + (
                "review_to has no address that works" if not report_problem else "report.toml doesn't read")
        elif not to:
            problem = "no address: set mail_domain in [console.labs.check] or report.toml's [report.labs]"
        plan.append({"round": rnd.id, "lab": lab.group, "name": lab.name, "kind": kind,
                     "record": pre + kind, "skipped": [pre + k for k in due if k != kind],
                     "to": to, "for": pis, "review": s.reviewing,
                     "members": len(lab.members), "claims": len(lab.claims), "left": len(left),
                     "closes": rnd.closes.isoformat(), "problem": problem})
    return plan


def _body(e: dict, s: Settings) -> str:
    asked = []
    if e["members"]:
        asked.append(f"{e['members']} member{'s' if e['members'] != 1 else ''} of its group")
    if e["claims"]:
        asked.append(f"{e['claims']} {'people' if e['claims'] != 1 else 'person'} who used its condo "
                     f"without being in its group")
    where = s.link or "My lab, in the Console"
    first = e["kind"] == "open"
    lines = []
    if e["review"]:
        lines += [f"[For review: this would have gone to {', '.join(e['for']) or '(no address)'}.]", ""]
    lines += [
        ("Each term we ask each lab's PI who is still in the lab, so that access to the "
         "clusters, condos and storage stays right." if first else
         f"A reminder: this term's check of {_title(e)} isn't done yet ({e['left']} to answer)."),
        "",
        f"For {_title(e)}, we'd like to know about {' and '.join(asked)}: "
        "for each, whether they are in your lab, and for anyone who has left, what to do with their data.",
        "",
        f"Please answer on {where} by {e['closes']}. It takes a few minutes.",
        "",
        "Nothing changes by itself: Research Computing makes the changes your answers call for.",
    ]
    return "\n".join(lines) + "\n"


def _title(e: dict) -> str:
    """How an email names the lab: its name, else "your lab" with its group."""
    return f"{e['name']} ({e['lab']})" if e["name"] and e["name"] != e["lab"] else f"your lab ({e['lab']})"


def subject_of(e: dict) -> str:
    subject = f"Who is in {_title(e)} this term?" if e["kind"] == "open" else \
        f"Reminder: who is in {_title(e)} this term?"
    return ("[review] " if e["review"] else "") + subject


def send(conn, rec, config: dict | None, report, now: datetime | None = None, dry: bool = False,
         report_problem: str | None = None, only: str | None = None) -> list[dict]:
    """Send what mail_plan says is due, and keep a record so nothing goes
    twice; ``dry``: only say what would go (each with its subject and
    text); ``only``: one lab's (the others stay due)."""
    from email.message import EmailMessage

    from nomad import mail
    s = settings(config, report, report_problem)
    plan = [e for e in mail_plan(conn, rec, config, report, now, report_problem) if only is None or e["lab"] == only]
    if dry or not plan:
        return [{**e, "status": "would send" if not e["problem"] else "can't send",
                 "subject": subject_of(e), "text": _body(e, s)} for e in plan]
    cfg = mail.settings(config)
    sender = s.send_from or cfg.get("from")
    out = []
    for e in plan:
        if e["problem"] or not sender:
            out.append({**e, "status": "can't send", "problem": e["problem"] or "no sender: set send_from"})
            continue
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(e["to"])
        msg["Subject"] = subject_of(e)
        msg.set_content(_body(e, s))
        try:
            mail.send(msg, cfg)
        except Exception as exc:        # noqa: BLE001 -- one failure doesn't stop the others
            out.append({**e, "status": "failed", "error": mail.failure_label(exc)})
            continue
        with rec:
            rec.execute("INSERT OR REPLACE INTO lab_check_mail (round, lab, kind, sent_to, review, at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (e["round"], e["lab"], e["record"], ", ".join(e["to"]), int(e["review"]), _now()))
            for k in e["skipped"]:        # a reminder overtaken by a later one isn't sent after it
                rec.execute("INSERT OR IGNORE INTO lab_check_mail (round, lab, kind, sent_to, review, at) "
                            "VALUES (?, ?, ?, '', ?, ?)", (e["round"], e["lab"], k, int(e["review"]), _now()))
            _log(rec, getpass.getuser(), {"mail": {k: e[k] for k in ("round", "lab", "kind", "to", "review")}})
        out.append({**e, "status": "sent"})
    return out


def status(conn, rec, config: dict | None, report, now: datetime | None = None,
           report_problem: str | None = None) -> dict:
    """This term's check, a lab at a time: asked, answered, sent in, emails."""
    now = now or datetime.now()
    s = settings(config, report, report_problem)
    rnd = round_of(s, now.date())
    out = {"on": s.on, "problems": list(s.problems), "review_to": list(s.review_to),
           "round": None, "labs": []}
    if rnd is None:
        return out
    out["round"] = {"id": rnd.id, "opened": rnd.opened.isoformat(), "closes": rnd.closes.isoformat(),
                    "next_opens": rnd.next_opens.isoformat()}
    have = answers(rec, rnd.id)
    done_labs = submitted(rec, rnd.id)
    mails: dict = {}
    for r in _rows(rec, "SELECT * FROM lab_check_mail WHERE round = ? AND sent_to != ''", (rnd.id,)):
        mails.setdefault(r["lab"], []).append({"kind": r["kind"].replace("review-", ""), "at": r["at"],
                                               "review": bool(r["review"])})
    for lab in labs(conn, config, report, s, now):
        got = {r["netid"] for r in have if r["lab"] == lab.group}
        qs = lab.questions()
        out["labs"].append({"lab": lab.group, "name": lab.name, "leaders": list(lab.leaders),
                            "members": len(lab.members), "claims": len(lab.claims), "asked": len(qs),
                            "answered": sum(1 for _, n in qs if n in got),
                            "submitted": done_labs.get(lab.group, {}).get("at"),
                            "mail": mails.get(lab.group, [])})
    return out


def led_by(conn, config, report, netid: str, now: datetime | None = None) -> list:
    """The labs ``netid`` answers for as the check sees them: those they
    lead by [console.labs], and the groups of condos they own."""
    n = _clean(netid)
    if not n:
        return []
    return [lab.group for lab in labs(conn, config, report, now=now) if n in lab.leaders]


def lab_for(conn, config, report, group: str, now: datetime | None = None) -> Lab:
    """The lab ``group`` as the check sees it now; CheckError when it has
    nothing to ask about (or isn't a lab)."""
    if not isinstance(group, str) or not _GROUP.match(group):
        raise CheckError(f"{group!r} is not a group")
    found = labs(conn, config, report, now=now, only={group})
    if not found:
        raise CheckError(f"{group}: no lab with anyone to ask about")
    return found[0]


__all__ = ["CheckError", "Settings", "Round", "Lab", "settings", "round_of", "db_path", "open_records",
           "by_site", "condo_labs", "labs", "answers", "submitted", "validate", "record", "submit", "view",
           "todo", "led_by",
           "mark_done", "mail_plan", "send", "status", "lab_for", "clean_note", "MEMBER_ANSWERS",
           "CLAIM_ANSWERS", "DATA_ANSWERS", "KINDS", "SAID"]
