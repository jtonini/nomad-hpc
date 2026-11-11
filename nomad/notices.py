# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A weekly email to each person about their own use (1.39.0; notes grouped, 1.39.1).

What nomad tells a person on the Console's My activity reaches them only
if they look. Once a week at most, nomad emails each person who has
something new there:

- **on the login nodes** (nomad.edu.login_node): what of theirs a login
  node's monitoring caught in the last week (a long computation, many
  programs busy at once, a lot of memory, a large transfer), as My activity
  shows it;
- **their jobs** (nomad.edu.insights): a pattern over their recent jobs of
  asking for far more than they used (most of at least ``min_jobs`` jobs on
  a partition, of severity ``high`` or ``critical``), with the value to ask
  for instead. Only for people who finished jobs in the last week.

Each email says what, where and when, and the better way, and links to My
activity for the details. What is alike is one note: the times of one kind
of login-node use (at most ``SHOWN`` of them), or one kind of asking for
too much over a person's partitions, with why it matters and the better way
said once (1.39.1). Never the same thing twice: a login-node entry is
told once; a jobs pattern again only after ``repeat_days`` if it still
holds. At most one email a person every ``every_days`` (7).

It is on for everyone by default: a person stops it on My activity in the
Console ("Email me about my own use"), or an admin for them (``nomad notices
set NETID off``). Accounts that exist only for a course (members of a
course group this term, of no lab and not of the faculty group) are left
out unless ``courses = true``.

**Review first.** While ``review_to`` holds addresses, every email goes
there instead, saying whom it was for. Emails go to people only once
``[notices] review_to = []`` is set: without ``review_to`` in [notices],
report.toml's review_to holds them, and with none there nothing is sent.

Settings, in the hub's nomad.toml::

    [notices]
    kinds = ["login_node", "jobs"]       # what the email tells
    every_days = 7                       # one email a person at most this often
    link = "https://nomad.example.edu"   # the Console: My activity is link + "/me"
    review_to = ["rc@example.edu"]       # while set, every email goes here; [] sends to people
    greeting = "Hello,"
    # mail_domain, send_from, signature, contact: as the lab check's (report.toml's)
    courses = false                      # true: accounts only in a course are emailed too
    skip_users = []                      # never emailed
    # db = "/home/nomad/.local/share/nomad/notices.db"   # default: beside combined.db

    [notices.jobs]
    days = 30                            # the jobs a pattern is read from
    min_jobs = 5                         # at least this many of them on the partition
    severities = ["critical", "high"]
    repeat_days = 30                     # the same pattern told again after this, if it still holds

    [notices.login_node]                 # what counts on a login node (nomad.edu.login_node)

``nomad notices mail`` shows what is due; ``--send`` sends it;
``nomad notices timer --apply`` sends it every week from systemd.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("login_node", "jobs")
SEVERITIES = ("critical", "high", "medium")
KEYS = {"kinds", "every_days", "link", "review_to", "greeting", "mail_domain", "send_from", "signature",
        "contact", "courses", "skip_users", "db", "jobs", "login_node"}
JOB_KEYS = {"days", "min_jobs", "severities", "repeat_days"}
GREETING = "Hello,"
_NETID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$|^[^<>\n]*<[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>$")
_LINK = re.compile(r"^https?://[^\s<>\"']+$")
SHOWN = 5               # occurrences of one note in an email at most; the rest: "and N more"
_ASKED = {"cpu": "cores", "memory": "memory", "time": "time", "gpu": "GPUs", "io": "I/O"}
# Why it matters, once a note: the email's own words (no contractions, as the rest of it).
_WHY = {
    "memory": "Memory a job asks for is held for it for as long as it runs: what it does not use, "
              "no other job on that node can use either.",
    "time": "Jobs that ask for less time start sooner: the scheduler can fit them into gaps that a "
            "longer request would not fit, and the queue moves faster for everyone.",
    "cpu": "Cores a job asks for are held for it for as long as it runs, used or not, so jobs that "
           "could use them wait.",
    "gpu": "GPUs are few: one a job asks for is held for it for as long as it runs, used or not, "
           "so jobs that need one wait.",
}
SYSTEM = frozenset({"root", "nobody", "daemon", "bin", "adm", "slurm", "munge", "nomad"})   # never emailed


class NoticesError(ValueError):
    """What was asked can't be done; said as it is."""


@dataclass
class Settings:
    kinds: tuple = KINDS
    every_days: int = 7
    link: str | None = None
    review_to: tuple = ()
    held: bool = False                   # nothing is sent (no review_to decided, or none that works)
    greeting: str = GREETING
    mail_domain: str | None = None
    send_from: str | None = None
    signature: tuple = ()
    contact: str | None = None
    courses: bool = False
    skip_users: tuple = ()
    db: Path | None = None
    jobs_days: int = 30
    jobs_min: int = 5
    jobs_severities: tuple = ("critical", "high")
    repeat_days: int = 30
    problems: tuple = ()

    @property
    def reviewing(self) -> bool:
        return bool(self.review_to)


def _section(config) -> dict | None:
    raw = (config or {}).get("notices") if isinstance(config, dict) else None
    return raw


def _int(raw, key, default, lo, hi, problems, where="[notices]"):
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        problems.append(f"{where} {key} = {v!r}: a whole number from {lo} to {hi}")
        return default
    return v


def _line(value, limit=200) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in " ".join(value.split()) if c.isprintable())[:limit]


def settings(config: dict | None, report=None, report_problem: str | None = None) -> Settings:
    """[notices] of nomad.toml, with what it doesn't say from the lab check's
    mail settings (its own, else report.toml's). What can't be used is in
    ``problems`` and left out; with no usable decision on review_to, nothing
    is sent (``held``)."""
    from nomad import lab_check as LC
    raw = _section(config)
    problems: list[str] = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return Settings(held=True, problems=("[notices] is not a table",))
    for k in sorted(set(raw) - KEYS):
        problems.append(f"[notices] {k}: not a setting nomad knows (left out)")
    lc = LC.settings(config, report, report_problem)
    kinds = raw.get("kinds", list(KINDS))
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        problems.append(f"[notices] kinds: a list of {', '.join(KINDS)}")
        kinds = list(KINDS)
    for k in kinds:
        if k not in KINDS:
            problems.append(f"[notices] kinds: {k!r} is not one ({', '.join(KINDS)}; left out)")
    kinds = tuple(k for k in KINDS if k in kinds)
    every = _int(raw, "every_days", 7, 1, 90, problems)
    link = raw.get("link", lc.link)
    if isinstance(link, str) and link.rstrip("/").endswith("/lab"):
        link = link.rstrip("/")[:-4]               # the lab check's link is to My lab
    if link is not None and (not isinstance(link, str) or not _LINK.match(link)):
        problems.append("[notices] link: the Console's address, http(s)")
        link = None

    def text(key, fallback, limit=200):
        v = raw.get(key, fallback)
        if v is not None and not isinstance(v, str):
            problems.append(f"[notices] {key}: text")
            return fallback
        return _line(v or "", limit) or None
    mail_domain = text("mail_domain", lc.mail_domain)
    if mail_domain and not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", mail_domain):
        problems.append(f"[notices] mail_domain: {mail_domain!r} is not a domain")
        mail_domain = None
    send_from = text("send_from", lc.send_from)
    if send_from and not _ADDRESS.match(send_from):
        problems.append(f"[notices] send_from: {send_from!r} is not an address")
        send_from = None
    # review_to: [notices]'s own decision, else report.toml's; never the lab
    # check's own (switching that check on must not switch these on).
    held = False
    if "review_to" in raw:
        review = raw["review_to"]
    else:
        delivery = getattr(report, "delivery", None)
        review = list(getattr(delivery, "review_to", None) or [])
        if not review:
            problems.append("[notices] review_to is not set: nothing is sent until it is "
                            "(review_to = [\"you@example.edu\"] to read them first, [] to send to people)")
            held = True
        if report_problem:
            problems.append("report.toml doesn't read, and its review_to may be what holds these emails: "
                            "nothing is sent until it reads (or review_to is set in [notices])")
            held = True
    if isinstance(review, str):
        review = [review]
    if not isinstance(review, list):
        problems.append("[notices] review_to: a list of addresses ([] sends to people)")
        review, held = [], True
    good = [a.strip() for a in review if isinstance(a, str) and _ADDRESS.match(a.strip())]
    for a in review:
        if not (isinstance(a, str) and _ADDRESS.match(a.strip())):
            problems.append(f"[notices] review_to: {a!r} is not an address (left out)")
    if review and not good:
        problems.append("[notices] review_to has no address that works: nothing is sent")
        held = True
    greeting = text("greeting", GREETING, 80) or GREETING
    sig = raw.get("signature")
    if sig is not None:
        lines = [sig] if isinstance(sig, str) else sig
        if isinstance(lines, list) and all(isinstance(x, str) for x in lines) and any(_line(x) for x in lines):
            signature = tuple(x for x in (_line(t, 120) for t in lines) if x)[:4]
        else:
            problems.append("[notices] signature: a list of lines")
            signature = lc.signature
    else:
        signature = lc.signature
    contact = text("contact", lc.contact, 120)
    courses = raw.get("courses", False)
    if not isinstance(courses, bool):
        problems.append(f"[notices] courses = {courses!r}: true or false, without quotes")
        courses = False
    skip = raw.get("skip_users", [])
    if not isinstance(skip, list) or not all(isinstance(x, str) for x in skip):
        problems.append("[notices] skip_users: a list of NetIDs")
        skip = []
    db = raw.get("db")
    if db is not None and not isinstance(db, str):
        problems.append("[notices] db: a path")
        db = None
    jobs = raw.get("jobs", {})
    if not isinstance(jobs, dict):
        problems.append("[notices.jobs] is not a table")
        jobs = {}
    for k in sorted(set(jobs) - JOB_KEYS):
        problems.append(f"[notices.jobs] {k}: not a setting nomad knows (left out)")
    jdays = _int(jobs, "days", 30, 7, 365, problems, "[notices.jobs]")
    jmin = _int(jobs, "min_jobs", 5, 1, 1000, problems, "[notices.jobs]")
    repeat = _int(jobs, "repeat_days", 30, 7, 365, problems, "[notices.jobs]")
    sev = jobs.get("severities", ["critical", "high"])
    if not isinstance(sev, list) or not sev or not all(s in SEVERITIES for s in sev):
        problems.append(f"[notices.jobs] severities: a list of {', '.join(SEVERITIES)}")
        sev = ["critical", "high"]
    return Settings(kinds=kinds, every_days=every, link=link.rstrip("/") if link else None,
                    review_to=tuple(good), held=held, greeting=greeting, mail_domain=mail_domain,
                    send_from=send_from, signature=tuple(signature), contact=contact, courses=courses,
                    skip_users=tuple(sorted({x.strip().lower() for x in skip if x.strip()})),
                    db=Path(db).expanduser() if db else None, jobs_days=jdays, jobs_min=jmin,
                    jobs_severities=tuple(s for s in SEVERITIES if s in sev), repeat_days=repeat,
                    problems=tuple(problems))


def config_problems(config) -> list[str]:
    """What is wrong in [notices] (not [notices.login_node]: nomad.edu.login_node
    says that), for ``nomad config check``. Not what report.toml holds."""
    if not isinstance(config, dict) or "notices" not in config:
        return []
    return [p for p in settings(config).problems
            if p.startswith("[notices") and "review_to is not set" not in p]


# -- the record ----------------------------------------------------------------------------------

def db_path(s: Settings, combined=None) -> Path:
    """The notices' own database: [notices] db, else beside the hub's
    combined database (``combined``, else [hub] output_db's)."""
    if s.db is not None:
        return s.db
    if combined is None:
        from nomad.config import combined_db_path
        combined = combined_db_path(None)
    return Path(combined).parent / "notices.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS notices_choice (
    username TEXT PRIMARY KEY,
    email INTEGER NOT NULL,          -- 1: emailed (the default), 0: not
    at TEXT NOT NULL,
    by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notices_mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    at TEXT NOT NULL,
    sent_to TEXT NOT NULL,
    review INTEGER NOT NULL,
    entries INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notices_mail ON notices_mail(username, review, at);
CREATE TABLE IF NOT EXISTS notices_told (
    username TEXT NOT NULL,
    key TEXT NOT NULL,
    review INTEGER NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (username, key, review)
);
"""


def open_db(path: Path, write: bool = False) -> sqlite3.Connection | None:
    """The record; None when there is none yet and only reading."""
    path = Path(path)
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def clean_netid(value) -> str | None:
    n = str(value or "").strip().lower()
    return n if _NETID.match(n) else None


def wants_email(rec, username: str) -> bool:
    """Whether ``username`` is emailed: yes unless they (or an admin) said not."""
    if rec is None:
        return True
    try:
        row = rec.execute("SELECT email FROM notices_choice WHERE username = ?", (username,)).fetchone()
    except sqlite3.Error:
        return True
    return row is None or bool(row[0])


def set_email(rec, username: str, on: bool, by: str = "", now: datetime | None = None) -> None:
    """``username``'s choice: emailed or not."""
    n = clean_netid(username)
    if not n:
        raise NoticesError(f"{username!r} is not a NetID")
    with rec:
        rec.execute("INSERT INTO notices_choice (username, email, at, by) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(username) DO UPDATE SET email = excluded.email, at = excluded.at, by = excluded.by",
                    (n, int(bool(on)), (now or _now()).isoformat(), _line(by, 64)))


def choices(rec) -> dict:
    """{username: (email, at, by)} for everyone who chose."""
    if rec is None:
        return {}
    try:
        return {r["username"]: (bool(r["email"]), r["at"], r["by"])
                for r in rec.execute("SELECT username, email, at, by FROM notices_choice")}
    except sqlite3.Error:
        return {}


def last_mail(rec, username: str, review: bool) -> str | None:
    """When ``username`` was last emailed (or, reviewing, last had one sent for review)."""
    if rec is None:
        return None
    try:
        row = rec.execute("SELECT MAX(at) FROM notices_mail WHERE username = ? AND review = ?",
                          (username, int(review))).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _told(rec, username: str, review: bool) -> dict:
    if rec is None:
        return {}
    try:
        return {r["key"]: r["at"] for r in rec.execute(
            "SELECT key, at FROM notices_told WHERE username = ? AND review = ?", (username, int(review)))}
    except sqlite3.Error:
        return {}


# -- who, and what ------------------------------------------------------------------------------

def course_only(conn, acc, username: str, lookup=None) -> bool:
    """Whether ``username`` is an account only for a course: in a course
    group this term, in no lab (a member, a leader or an assistant) and not
    in the faculty group. Without groups data: no (they are emailed)."""
    from nomad.config.access import members_lookup
    try:
        group_exists, members_of = lookup or members_lookup(conn)
        groups = [g for (g,) in conn.execute(
            "SELECT DISTINCT group_name FROM group_membership WHERE username = ?", (username,))]
    except sqlite3.Error:
        return False
    current = [g for g in groups if username in members_of(g)]
    if not current:
        return False
    faculty = getattr(getattr(acc, "courses", None), "faculty_group", None)
    if faculty and faculty in current:
        return False
    if acc.lab_groups(username, group_exists) or acc.assisted_groups(username, members_of):
        return False
    if any(acc.is_lab(g, members_of) and not acc.is_class(g) for g in current):
        return False
    return any(acc.is_class(g) for g in current)


def _local_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat(timespec="seconds")


def _people(conn, s: Settings, now: datetime) -> dict:
    """{username: set of kinds worth looking at}: who had something lately."""
    since = now - timedelta(days=s.every_days)
    out: dict = {}
    if "login_node" in s.kinds:
        from datetime import timezone
        from nomad.edu.login_node import LOGIN_ROLES
        since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            marks = ",".join("?" * len(LOGIN_ROLES))
            for (u,) in conn.execute(f"SELECT DISTINCT username FROM per_user_alert WHERE last_seen >= ? "
                                     f"AND role IN ({marks})", (since_utc, *LOGIN_ROLES)):
                out.setdefault(u, set()).add("login_node")
        except sqlite3.Error:
            pass
    if "jobs" in s.kinds:
        try:
            # end_time as sacct writes it ("2026-11-10T14:00:00"), or with a space.
            for (u,) in conn.execute("SELECT DISTINCT user_name FROM jobs WHERE replace(end_time, ' ', 'T') >= ?",
                                     (_local_iso(since),)):
                out.setdefault(u, set()).add("jobs")
        except sqlite3.Error:
            pass
    return out


@dataclass
class Entry:
    """One thing to tell (what is recorded as told), as part of a note: the
    entries of one ``group`` are one note in the email, under ``title``,
    each in its ``lines``, then the note's ``advice`` once."""
    kind: str                         # login_node | jobs
    key: str                          # what it is, so it is told once
    title: str                        # the note's heading
    lines: list = field(default_factory=list)       # this occurrence
    group: str = ""                   # entries with the same group are one note
    advice: list = field(default_factory=list)      # the note's why and better way, said once
    started: str = ""                 # newest first within a note


def _mem(mb) -> str:
    """Megabytes as people say it: "105 MB", "4.8 GB", "125 GB"."""
    mb = float(mb or 0)
    if mb < 1024:
        return f"{max(round(mb), 1)} MB"
    return f"{mb / 1024:.1f}".rstrip("0").rstrip(".") + " GB"


def _dur(seconds) -> str:
    """Seconds as people say it: "50 min", "1 h 38 min", "16 h", "1 day", "1 day 22 h"."""
    m = round(float(seconds or 0) / 60)
    if m < 1:
        return "under a minute"
    if m < 60:
        return f"{m} min"
    if m < 24 * 60:
        h, m = divmod(m, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    d, h = divmod(round(m / 60), 24)
    return (f"{d} day" if d == 1 else f"{d} days") + (f" {h} h" if h else "")


def _num(x) -> str:
    x = float(x or 0)
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _many(x, word: str) -> str:
    n = _num(x)
    return f"{n} {word}" if n == "1" else f"{n} {word}s"


def _asked_for(i) -> str:
    """What the jobs typically asked for, in words."""
    v = getattr(i, "current_value_typical", 0) or 0
    d = getattr(i, "directive", "")
    if v:
        if d == "mem":
            return _mem(v)
        if d == "time":
            return _dur(v)
        if d in ("cores", "ntasks", "cpus-per-task"):
            return _many(v, "core")
        if d == "gres":
            return _many(v, "GPU")
    return str(getattr(i, "current_display", "") or "more")


def _used(stats) -> str:
    """What they used (the median), in words; "" when not known."""
    if stats is None:
        return ""
    unit = str(stats.unit or "")
    if unit == "MB":
        return _mem(stats.median)
    if unit == "seconds":
        return _dur(stats.median)
    if unit in ("cores", "GPUs"):
        return _num(stats.median)
    return f"{_num(stats.median)} {unit}".strip()


def _login_entries(db_path, username, config, s: Settings, now: datetime, told: dict) -> list[Entry]:
    from nomad.edu import login_node as LN
    since = now - timedelta(days=s.every_days)
    out = []
    for f in LN.findings(db_path, username, config, now=now.astimezone()):
        try:
            if datetime.fromisoformat(f.last_seen) < since:
                continue
        except ValueError:
            continue
        key = f"login_node:{f.site}:{f.host}:{f.program}:{f.started}"
        if key in told:
            continue
        what = f.program or "your programs"
        size = ", ".join(x for x in (_many(f.cores, "core") if f.cores else "",
                                     f"{_num(f.memory_gb)} GB" if f.memory_gb else "") if x)
        lines = [f"{what} on {f.site}, {_when(f.started)}, for {LN._span(f.minutes)}"
                 + (f" ({size})" if size else "") + (", still going at the last reading" if f.ongoing else "")]
        if f.command and f.command != f.program:
            cmd = " ".join(f.command.split())
            lines.append(cmd if len(cmd) <= 160 else cmd[:157] + "...")
        out.append(Entry("login_node", key, f.title, lines, group=f"login_node:{f.kind}:{'|'.join(f.better)}",
                         advice=[f.why, *f.better], started=str(f.started or "")))
    return out


def _job_entries(db_path, username, config, s: Settings, now: datetime, told: dict) -> list[Entry]:
    from nomad.edu.insights import sbatch_lines, user_insights
    try:
        ins = user_insights(db_path=str(db_path), username=username, days=s.jobs_days, config=config)
    except Exception:                       # noqa: BLE001 -- one person's jobs don't stop the others
        return []
    out = []
    for i in getattr(ins, "issues", None) or []:
        if (i.kind != "dimension" or i.direction != "lower" or i.severity not in s.jobs_severities
                or i.affected_jobs < s.jobs_min or not i.suggested_display or not i.directive):
            continue
        key = f"jobs:{i.cluster or ''}:{i.partition or ''}:{i.dimension_key}"
        last = told.get(key)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(days=s.repeat_days):
                    continue
            except ValueError:
                pass
        where = f"{i.cluster}" + (f" ({i.partition})" if i.partition else "") if i.cluster else "the clusters"
        word = _ASKED.get(i.dimension_key, i.dimension_key or "resources")
        used = _used(i.usage_stats)
        typical = f"typically {_asked_for(i)}" + (f"; they used about {used}" if used else "")
        lines = [f"On {where}, {i.affected_jobs} of your last {i.total_applicable} jobs asked for more {word} "
                 f"than they used ({typical}). Ask for this instead:"]
        lines += [f"  {x}" for x in sbatch_lines(i.directive, i.suggested_display)]
        why = _WHY.get(i.dimension_key)
        out.append(Entry("jobs", key, f"Your jobs ask for more {word} than they use", lines,
                         group=f"jobs:{i.dimension_key}", advice=[why] if why else []))
    return out


def _when(iso: str) -> str:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{t:%a} {t.day} {t:%b %H:%M}"


def plan(conn, db, config: dict | None, s: Settings, rec, now: datetime | None = None,
         only: str | None = None) -> dict:
    """What is due now: {"mail": [one per person with something new], "left": {why: n}}.
    ``conn``: the combined database (read-only); ``db``: its path; ``rec``:
    the notices' record (None: nothing sent yet). ``only``: one person."""
    from nomad.config.access import access_from, members_lookup
    now = now or _now()
    review = s.reviewing
    people = _people(conn, s, now)
    if only is not None:
        people = {only: people.get(only, set(s.kinds))}
    acc = access_from(config, log=False)
    lookup = None
    try:
        lookup = members_lookup(conn)
    except sqlite3.Error:
        lookup = None
    left = {"stopped": 0, "course only": 0, "emailed lately": 0, "not a person's account": 0, "nothing new": 0}
    out = []
    for user in sorted(people):
        n = clean_netid(user)
        if not n or n in s.skip_users or n in SYSTEM:
            left["not a person's account"] += 1
            continue
        if not wants_email(rec, n):
            left["stopped"] += 1
            continue
        if not s.courses and lookup is not None and course_only(conn, acc, n, lookup):
            left["course only"] += 1
            continue
        last = last_mail(rec, n, review)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(days=s.every_days) - timedelta(hours=6):
                    left["emailed lately"] += 1
                    continue
            except ValueError:
                pass
        told = _told(rec, n, review)
        entries = []
        if "login_node" in people[user] and "login_node" in s.kinds:
            entries += _login_entries(db, n, config, s, now, told)
        if "jobs" in people[user] and "jobs" in s.kinds:
            entries += _job_entries(db, n, config, s, now, told)
        if not entries:
            left["nothing new"] += 1
            continue
        address = f"{n}@{s.mail_domain}" if s.mail_domain else None
        problem = None if address else "no address: set mail_domain"
        out.append({"user": n, "address": address, "to": list(s.review_to) if review else
                    ([address] if address else []), "review": review, "entries": entries, "problem": problem})
    return {"mail": out, "left": left}


# -- the email ----------------------------------------------------------------------------------

def notes_of(entries) -> list[tuple[str, list]]:
    """The email's notes: (kind, [entries]) a group each, login nodes first,
    in the order first met; within a note, the newest first."""
    out: dict = {}
    for e in entries:
        out.setdefault((e.kind != "login_node", e.group or e.key), []).append(e)
    notes = []
    for (_, _), es in sorted(out.items(), key=lambda kv: kv[0][0]):
        es.sort(key=lambda e: e.started, reverse=True)
        notes.append((es[0].kind, es))
    return notes


def subject_of(m: dict) -> str:
    n = len(notes_of(m["entries"]))
    words = "a note" if n == 1 else f"{n} notes"
    return (f"[review: for {m['user']}] " if m["review"] else "") + f"Research computing: {words} on your recent work"


def text_of(m: dict, s: Settings) -> str:
    """The email, plain text: what, where, when, and the better way."""
    lines = []
    if m["review"]:
        lines += [f"[Review: this would have gone to {m['address'] or m['user']}.]", ""]
    who = s.signature[0] if s.signature else "Research Computing"
    lines += [s.greeting, "",
              f"NØMAÐ, the monitoring that {who} runs on the clusters, noticed something in your recent "
              "work that could run better. Nothing is wrong and nothing has been done about it; this is "
              "only to help.", ""]
    notes = notes_of(m["entries"])
    for kind, title in (("login_node", "On the login nodes"), ("jobs", "Your jobs")):
        mine = [es for k, es in notes if k == kind]
        if not mine:
            continue
        lines += [title, "-" * len(title)]
        for es in mine:
            many = kind == "login_node" and len(es) > 1
            lines.append(f"* {es[0].title}" + (f" ({len(es)} times)" if many else ""))
            for e in es[:SHOWN]:
                first, *rest = e.lines or [""]
                lines.append(f"  - {first}")
                lines += [f"    {x}" for x in rest]
            if len(es) > SHOWN:
                lines.append(f"  ... and {len(es) - SHOWN} more like these"
                             + (" on My activity." if s.link else "."))
            # a command to type stands out from the words around it
            lines += [f"    {x}" if x.startswith(("srun ", "sbatch ")) else f"  {x}" for x in es[0].advice]
            lines.append("")
    if s.link:
        lines += [f"The details, and how to do it another way: {s.link}/me", ""]
    stop = (f"These emails come at most once every {s.every_days} days, and only with something new. "
            + (f"To stop them, turn off \"Email me about my own use\" at {s.link}/me" if s.link
               else "To stop them, reply and say so") + ".")
    lines += [stop, ""]
    if s.contact:
        lines += [s.contact, ""]
    lines += list(s.signature)
    return "\n".join(lines).rstrip() + "\n"


def send(conn, db, config: dict | None, s: Settings, rec, now: datetime | None = None, dry: bool = False,
         only: str | None = None) -> dict:
    """Send what ``plan`` says is due and keep a record, so nothing goes twice.
    ``dry``: only say what would go. Nothing at all while ``s.held``."""
    from email.message import EmailMessage

    from nomad import mail
    now = now or _now()
    p = plan(conn, db, config, s, rec, now, only)
    for m in p["mail"]:
        m["subject"] = subject_of(m)
        m["text"] = text_of(m, s)
        m["status"] = "would send" if not m["problem"] else "can't send"
    if dry or not p["mail"]:
        return p
    if s.held:
        for m in p["mail"]:
            m["status"], m["problem"] = "held", "nothing is sent: see `nomad notices`"
        return p
    cfg = mail.settings(config)
    sender = s.send_from or cfg.get("from")
    for m in p["mail"]:
        if m["problem"] or not sender or not m["to"]:
            m["status"], m["problem"] = "can't send", m["problem"] or ("no sender: set send_from" if not sender
                                                                      else "no address")
            continue
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(m["to"])
        msg["Subject"] = m["subject"]
        msg.set_content(m["text"])
        try:
            mail.send(msg, cfg)
        except Exception as exc:            # noqa: BLE001 -- one failure doesn't stop the others
            m["status"], m["error"] = "failed", mail.failure_label(exc)
            continue
        at = now.isoformat()
        with rec:
            rec.execute("INSERT INTO notices_mail (username, at, sent_to, review, entries) VALUES (?, ?, ?, ?, ?)",
                        (m["user"], at, ", ".join(m["to"]), int(m["review"]), len(m["entries"])))
            for e in m["entries"]:
                rec.execute("INSERT INTO notices_told (username, key, review, at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(username, key, review) DO UPDATE SET at = excluded.at",
                            (m["user"], e.key, int(m["review"]), at))
        m["status"] = "sent"
    return p


def status(s: Settings, rec, now: datetime | None = None) -> dict:
    """For `nomad notices` and the Console: how it is set, and what went lately."""
    now = now or _now()
    since = (now - timedelta(days=30)).isoformat()
    sent = review = 0
    last = None
    if rec is not None:
        try:
            sent, review, last = rec.execute(
                "SELECT SUM(review = 0), SUM(review = 1), MAX(at) FROM notices_mail WHERE at >= ?",
                (since,)).fetchone()
        except sqlite3.Error:
            pass
    ch = choices(rec)
    return {"kinds": list(s.kinds), "every_days": s.every_days,
            "sending": "held" if s.held else ("review" if s.reviewing else "people"),
            "review_to": list(s.review_to), "stopped": sum(1 for v in ch.values() if not v[0]),
            "sent_30_days": int(sent or 0), "review_30_days": int(review or 0), "last": last,
            "problems": list(s.problems)}
