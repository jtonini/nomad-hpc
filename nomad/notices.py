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
  (1.62.0) Asking for far more **time** than the jobs ran is told only
  when it cost the person waiting: their jobs waited at least
  ``waited_minutes`` longer to start, and ``waited_times`` as long, as
  other people's jobs of the same size on the same partition, sent in the
  same days, that asked for about the time they ran (at least
  ``compare_min_jobs`` of those). On a quiet cluster, where jobs start at
  once whatever they ask for, it isn't told; My activity still shows it.
- (1.51.0) **jobs that ran short**: jobs that reached their time limit or
  ran out of memory (at least ``short_min_jobs`` of them), with the value
  to ask for instead, from the person's own jobs: the work since a job's
  last checkpoint is lost when it is stopped;
- (1.51.0) **jobs that stopped within a minute** with an error (at least
  ``quick_min_jobs`` of them on a cluster in the last week): what usually
  causes it and where the job's own output says which;
- (1.51.0) **people new to a cluster** (their first job there in the last
  ``new_days``) most of whose jobs there have not completed: that it is
  common at the start, and that Research Computing will look at a script
  with them (on the first clusters it ran on, people's first ten jobs
  failed about half the time, against about three in ten after).

Each email says what, where and when, and the better way, and links to My
activity for the details. (1.62.0) It goes laid out (HTML) with a plain
copy: numbered notes, each with what NØMAÐ saw, the jobs (an array once,
with its tasks counted), what to ask for instead in a box of its own, what
to do and why; jobs that stopped within a minute or ran short come before
asking for too much. What is alike is one note: the times of one kind
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
    short_min_jobs = 2                   # (1.51.0) jobs that ran out of time or memory: at least this many
    quick_seconds = 60                   # (1.51.0) a job that stopped with an error this soon
    quick_min_jobs = 3                   # ... at least this many on a cluster in the last week (0: never)
    new_days = 30                        # (1.51.0) first job on a cluster this recently: new (0: never)
    time_if_waited = true                # (1.62.0) asking for too much time: told only when it made them wait
    waited_minutes = 30                  # ... at least this much longer than jobs like theirs
    waited_times = 2                     # ... and this many times as long
    compare_min_jobs = 10                # ... with at least this many jobs like theirs to go by

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
JOB_KEYS = {"days", "min_jobs", "severities", "repeat_days", "short_min_jobs", "quick_seconds", "quick_min_jobs",
            "new_days", "time_if_waited", "waited_minutes", "waited_times", "compare_min_jobs"}
GREETING = "Hello,"
_NETID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$|^[^<>\n]*<[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>$")
_LINK = re.compile(r"^https?://[^\s<>\"']+$")
SHOWN = 5               # occurrences of one note in an email at most; the rest: "and N more"
JOB_IDS_SHOWN = 8       # the jobs a jobs note is about, by id (1.49.0); the rest: "and N more"
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
    short_min: int = 2                   # 1.51.0: jobs that ran out of time or memory
    quick_seconds: int = 60              # 1.51.0: jobs that stopped with an error within this
    quick_min: int = 3
    new_days: int = 30                   # 1.51.0: people new to a cluster
    time_if_waited: bool = True          # 1.62.0: "ask for less time" only when it cost them waiting
    waited_minutes: int = 30             # ... their jobs waited at least this much longer
    waited_times: int = 2                # ... and this many times as long
    compare_min: int = 10                # ... than at least this many jobs like theirs
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
    short = _int(jobs, "short_min_jobs", 2, 1, 1000, problems, "[notices.jobs]")
    quick_s = _int(jobs, "quick_seconds", 60, 5, 3600, problems, "[notices.jobs]")
    quick_n = _int(jobs, "quick_min_jobs", 3, 0, 1000, problems, "[notices.jobs]")
    new_days = _int(jobs, "new_days", 30, 0, 365, problems, "[notices.jobs]")
    if_waited = jobs.get("time_if_waited", True)
    if not isinstance(if_waited, bool):
        problems.append(f"[notices.jobs] time_if_waited = {if_waited!r}: true or false, without quotes")
        if_waited = True
    waited_m = _int(jobs, "waited_minutes", 30, 0, 7 * 24 * 60, problems, "[notices.jobs]")
    waited_x = _int(jobs, "waited_times", 2, 1, 100, problems, "[notices.jobs]")
    compare_n = _int(jobs, "compare_min_jobs", 10, 1, 10000, problems, "[notices.jobs]")
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
                    short_min=short, quick_seconds=quick_s, quick_min=quick_n, new_days=new_days,
                    time_if_waited=if_waited, waited_minutes=waited_m, waited_times=waited_x,
                    compare_min=compare_n, problems=tuple(problems))


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
    each with what nomad saw (``lines``), the jobs it is about and what to
    ask for instead; then the note's ``todo`` and ``advice`` (why) once
    (1.62.0: the parts apart, so the email can lay them out)."""
    kind: str                         # login_node | jobs
    key: str                          # what it is, so it is told once
    title: str                        # the note's heading
    lines: list = field(default_factory=list)       # what nomad saw, this occurrence
    group: str = ""                   # entries with the same group are one note
    advice: list = field(default_factory=list)      # why, said once a note
    started: str = ""                 # newest first within a note
    jobs: str = ""                    # "array 13212 (15 tasks)": the jobs it is about
    change: list = field(default_factory=list)      # what to ask for instead (#SBATCH lines)
    change_note: str = ""             # that value in words
    todo: list = field(default_factory=list)        # what to do, said once a note (commands as lines)
    command: str = ""                 # the command that ran (login nodes)


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
        return _many(stats.median, unit[:-1])          # 1.62.0: "1 core", not "1"
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
        command = ""
        if f.command and f.command != f.program:
            cmd = " ".join(f.command.split())
            command = cmd if len(cmd) <= 160 else cmd[:157] + "..."
        out.append(Entry("login_node", key, f.title, lines, group=f"login_node:{f.kind}:{'|'.join(f.better)}",
                         advice=[f.why], todo=list(f.better), started=str(f.started or ""), command=command))
    return out


def _job_entries(db_path, username, config, s: Settings, now: datetime, told: dict, conn=None) -> list[Entry]:
    from nomad.edu.insights import sbatch_lines, user_insights
    try:
        ins = user_insights(db_path=str(db_path), username=username, days=s.jobs_days, config=config)
    except Exception:                       # noqa: BLE001 -- one person's jobs don't stop the others
        return []
    out = []
    for i in getattr(ins, "issues", None) or []:
        if (i.kind == "dimension" and i.direction == "raise" and i.dimension_key in _SHORT
                and i.affected_jobs >= s.short_min and i.suggested_display and i.directive):
            e = _short_entry(i, s, now, told)
            if e:
                out.append(e)
            continue
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
        waited = None
        if i.dimension_key == "time" and s.time_if_waited:
            # 1.62.0: asking for more time than a job runs costs its owner only
            # when the cluster is busy enough to make it wait: told only then.
            waited = _waited(conn, username, i, s)
            if waited is None:
                continue
        where = f"{i.cluster}" + (f" ({i.partition})" if i.partition else "") if i.cluster else "the clusters"
        word = _ASKED.get(i.dimension_key, i.dimension_key or "resources")
        # 1.45.0: size advice comes from the jobs that completed; say so when some didn't
        kind = "completed jobs" if getattr(i, "failed_jobs", 0) else "jobs"
        lines = [f"On {where}, {i.affected_jobs} of your last {i.total_applicable} {kind} asked for more "
                 f"{word} than they {'ran' if i.dimension_key == 'time' else 'used'}: {_typical(i)}."]
        if waited:
            lines.append(f"They waited about {_dur(waited['mine'])} to start"
                         + (" (the median)" if waited["n_mine"] > 1 else "") + ". Jobs of the same size "
                         f"on the same partition, sent in the same days, that asked for about the time they ran "
                         f"waited about {_dur(waited['others'])} (the median of {waited['n_others']} jobs).")
        out.append(Entry("jobs", key, f"Your jobs ask for more {word} than they use", lines,
                         group=f"jobs:{i.dimension_key}", advice=[_WHY[i.dimension_key]] if i.dimension_key in _WHY
                         else [], jobs=_ids(i), **_change(sbatch_lines(i.directive, i.suggested_display),
                                                         _suggested_words(i))))
    return out


def _change(lines, words: str) -> dict:
    """sbatch_lines apart: the #SBATCH lines to ask with, and its words beside
    them ("(or --ntasks=1, if the job runs several tasks...)") with ``words``."""
    lines = list(lines or [])
    said = " ".join([x for x in lines if not _is_command(x)] + ([words] if words else []))
    return {"change": [x for x in lines if _is_command(x)], "change_note": said}


def _typical(i) -> str:
    """'they typically asked for 365 days; most ran about 2 h 10 min, the
    longest 6 days 10 h': what they asked for and used, in words (1.62.0:
    the longest too, where it differs, so the value to ask for makes sense)."""
    asked = _asked_for(i)
    st = getattr(i, "usage_stats", None)
    used = _used(st)
    if not used:
        return f"they typically asked for {asked}"
    verb = "ran" if getattr(i, "dimension_key", "") == "time" else "used"
    text = f"they typically asked for {asked}; most {verb} about {used}"
    top = getattr(st, "max", 0) or 0
    if top and top > 1.5 * (st.median or 0):
        one = _dur(top) if str(st.unit) == "seconds" else _mem(top) if str(st.unit) == "MB" else _num(top)
        text += f", the longest {one}" if verb == "ran" else f", at most {one}"
    return text


def _suggested_words(i) -> str:
    """The value to ask for, in words, and what it covers (1.62.0)."""
    v = getattr(i, "suggested_value", 0) or 0
    st = getattr(i, "usage_stats", None)
    if not v or st is None:
        return ""
    unit = str(st.unit or "")
    said = _dur(v) if unit == "seconds" else _mem(v) if unit == "MB" else ""
    if not said:
        return ""
    covers = ("enough for the " + ("longest" if unit == "seconds" else "largest") + " of these jobs"
              if (st.max or 0) <= v else "enough for most of these jobs")
    return f"That is {said}: {covers}, with some margin."


def _waited(conn, username: str, i, s: Settings) -> dict | None:
    """Whether asking for far more time than they ran made a person's jobs
    wait (1.62.0): their jobs' waits for the queue (nomad.db.jobkeys.
    queue_wait_sql: an array by its first task) against those of other
    people's jobs on the same partition, sent in the same days (a day either
    side), of the same size (half to twice the cores, GPUs or not alike),
    that asked for about the time they ran (at most twice it, or an hour
    more). {"mine", "others", "n_mine", "n_others"} (seconds, medians) when
    theirs waited at least ``waited_minutes`` longer and ``waited_times`` as
    long, with at least ``compare_min`` jobs to go by; else None, also when
    nomad can't tell."""
    import statistics

    from nomad.db.jobkeys import queue_wait_sql
    if conn is None:
        return None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    except sqlite3.Error:
        return None
    need = {"job_id", "user_name", "partition", "submit_time", "start_time", "req_time_seconds",
            "runtime_seconds", "req_cpus"}
    ids = [str(j.get("job_id")) for j in (getattr(i, "jobs", None) or []) if isinstance(j, dict) and j.get("job_id")]
    if not need <= cols or not ids:
        return None
    wait = queue_wait_sql(cols)
    gpus = "COALESCE(req_gpus, 0)" if "req_gpus" in cols else "0"
    site, site_args = "", []
    if "source_site" in cols and i.cluster:
        site, site_args = " AND source_site IS ?", [i.cluster]
    part, part_args = "", []
    if i.partition:
        part, part_args = " AND partition IS ?", [i.partition]
    try:
        mine = conn.execute(f"SELECT {wait}, replace(submit_time, ' ', 'T'), req_cpus, {gpus} FROM jobs "
                            f"WHERE user_name = ? AND job_id IN ({','.join('?' * len(ids))}){site}{part}",
                            [username, *ids, *site_args, *part_args]).fetchall()
    except sqlite3.Error:
        return None
    waits = [float(w) for w, *_ in mine if w is not None and w >= 0]
    sent = sorted(x for _, x, _, _ in mine if x)
    cores = [c for _, _, c, _ in mine if c]
    if not waits or not sent or not cores:
        return None
    try:
        lo = (datetime.fromisoformat(sent[0][:19]) - timedelta(days=1)).isoformat()
        hi = (datetime.fromisoformat(sent[-1][:19]) + timedelta(days=1)).isoformat()
    except ValueError:
        return None
    c = statistics.median(cores)
    gpu = 1 if statistics.median([g or 0 for *_, g in mine]) > 0 else 0
    try:
        rows = conn.execute(
            f"SELECT {wait} FROM jobs WHERE user_name IS NOT ?{site}{part} "
            "AND replace(submit_time, ' ', 'T') BETWEEN ? AND ? AND req_cpus BETWEEN ? AND ? "
            f"AND (CASE WHEN {gpus} > 0 THEN 1 ELSE 0 END) = ? "
            "AND runtime_seconds > 0 AND req_time_seconds > 0 "
            "AND req_time_seconds <= MAX(2 * runtime_seconds, runtime_seconds + 3600)",
            [username, *site_args, *part_args, lo, hi, c / 2, c * 2, gpu]).fetchall()
    except sqlite3.Error:
        return None
    others = [float(w) for (w,) in rows if w is not None and w >= 0]
    if len(others) < s.compare_min:
        return None
    m, o = statistics.median(waits), statistics.median(others)
    if m - o < s.waited_minutes * 60 or m < s.waited_times * o:
        return None
    return {"mine": m, "others": o, "n_mine": len(waits), "n_others": len(others)}


_SHORT = {"time": ("time", "reached their time limit or came close to it"),
          "memory": ("memory", "ran out of memory or came close to it")}
_SHORT_WHY = {
    "time": "A job stopped at its time limit loses the work it did since it last saved its progress, and its "
            "time in the queue.",
    "memory": "A job that runs out of memory is stopped: the work it did since it last saved its progress is "
              "lost.",
}


def _recent(last, now: datetime, days: int) -> bool:
    try:
        return bool(last) and now - datetime.fromisoformat(last) < timedelta(days=days)
    except ValueError:
        return False


def _short_entry(i, s: Settings, now: datetime, told: dict) -> Entry | None:
    """Jobs that ran out of time or memory, or came close (1.51.0): the value
    to ask for instead, from the person's own jobs."""
    from nomad.edu.insights import sbatch_lines
    key = f"jobs:short:{i.cluster or ''}:{i.partition or ''}:{i.dimension_key}"
    if _recent(told.get(key), now, s.repeat_days):
        return None
    word, what = _SHORT[i.dimension_key]
    where = f"{i.cluster}" + (f" ({i.partition})" if i.partition else "") if i.cluster else "the clusters"
    used = _used(i.usage_stats)
    typical = f"they typically asked for {_asked_for(i)}" + (
        f" and {'ran' if word == 'time' else 'used'} about {used}" if used else "")
    lines = [f"On {where}, {i.affected_jobs} of your last {i.total_applicable} jobs {what} ({typical})."]
    todo = []
    if s.link:
        todo.append(f"NØMAÐ's Submit page checks this against your earlier jobs before a job goes: {s.link}/submit")
    return Entry("jobs", key, f"Some of your jobs ran out of {word}", lines, group=f"jobs:short:{word}",
                 advice=[_SHORT_WHY[i.dimension_key]], jobs=_ids(i), todo=todo,
                 **_change(sbatch_lines(i.directive, i.suggested_display), ""))


_QUICK_WHY = [
    "A job that stops this soon usually could not find or start something: a file or folder path with a typo "
    "or that is not on the cluster, a module the script does not load, a program name the cluster does not "
    "know, or a script that is not executable.",
]
_QUICK_TODO = [
    "Look at the first lines of the output file of one of these jobs (slurm-JOBID.out, in the folder the job "
    "was sent from, unless the script names another): they usually say what was missing.",
]


def _ended_iso(v) -> str:
    return str(v or "").replace(" ", "T")


def _quick_entries(conn, username: str, s: Settings, now: datetime, told: dict) -> list:
    """Jobs that stopped with an error within ``quick_seconds`` (1.51.0), at least
    ``quick_min`` of them on a cluster in the last ``every_days``; and, for a person
    new to a cluster (first job there within ``new_days``), most of whose jobs there
    have not completed, a word that it is common at the start and where to get help.
    A job killed for memory or stopped at its time limit isn't one: those are told
    as jobs that ran short."""
    from nomad.collectors.slurm import compute_failure_reason
    if conn is None:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    except sqlite3.Error:
        return []
    if not {"user_name", "state", "end_time", "runtime_seconds", "job_id"} <= cols:
        return []
    site = "source_site" if "source_site" in cols else "NULL"
    code = "exit_code" if "exit_code" in cols else "NULL"
    sig = "exit_signal" if "exit_signal" in cols else "NULL"
    since = _local_iso(now - timedelta(days=s.every_days))
    out = []
    try:
        rows = conn.execute(f"SELECT job_id, {site} AS site, state, runtime_seconds, end_time, {code} AS code, "
                            f"{sig} AS sig FROM jobs WHERE user_name = ? AND replace(end_time, ' ', 'T') >= ?",
                            (username, since)).fetchall()
    except sqlite3.Error:
        return []
    quick: dict = {}
    for r in rows:
        st = str(r[2] or "").split()[0].upper() if r[2] else ""
        if st != "FAILED" or (r[3] if r[3] is not None else 10 ** 9) >= s.quick_seconds:
            continue
        if compute_failure_reason(st, r[5], r[6]) == 4:          # killed for memory: ran short, not quick
            continue
        quick.setdefault(str(r[1] or ""), []).append((_ended_iso(r[4]), str(r[0])))
    for where, jobs in sorted(quick.items()):
        if not s.quick_min or len(jobs) < s.quick_min:
            continue
        if _recent(told.get(f"jobs:quick:{where}"), now, s.every_days):
            continue
        jobs.sort(reverse=True)
        days = "this week" if s.every_days == 7 else f"in the last {s.every_days} days"
        cluster = f"On {where}, " if where else ""
        out.append(Entry("jobs", f"jobs:quick:{where}", "Some of your jobs stopped within a minute",
                         [f"{cluster}{len(jobs)} of your jobs {days} stopped with an error within "
                          f"{_dur(s.quick_seconds) if s.quick_seconds != 60 else 'a minute'} of starting."],
                         group="jobs:quick", advice=list(_QUICK_WHY), todo=list(_QUICK_TODO),
                         started=max(j[0] for j in jobs), jobs=_job_words([j[1] for j in jobs], len(jobs))))
    if s.new_days:
        out += _new_entries(conn, username, s, now, told, site, cols)
    return out


def _new_entries(conn, username, s: Settings, now: datetime, told: dict, site: str, cols: set) -> list:
    """A person whose first job on a cluster was within ``new_days`` and most of
    whose jobs there have not completed (1.51.0): told once a cluster."""
    if "submit_time" not in cols:
        return []
    out = []
    try:
        rows = conn.execute(f"SELECT {site} AS site, MIN(replace(submit_time, ' ', 'T')), COUNT(*), "
                            "SUM(CASE WHEN upper(substr(state, 1, 9)) = 'COMPLETED' THEN 1 ELSE 0 END), "
                            "SUM(CASE WHEN upper(substr(state, 1, 9)) = 'CANCELLED' OR end_time IS NULL "
                            "OR end_time = '' THEN 1 ELSE 0 END) FROM jobs WHERE user_name = ? GROUP BY 1",
                            (username,)).fetchall()
    except sqlite3.Error:
        return []
    since = _local_iso(now - timedelta(days=s.new_days))
    for where, first, n, done, other in rows:
        ended = (n or 0) - (other or 0)
        if not first or first < since or ended < 2 or (done or 0) * 2 > ended:
            continue
        key = f"jobs:new:{where or ''}"
        if told.get(key):
            continue
        place = f" on {where}" if where else ""
        out.append(Entry("jobs", key, f"Getting started{place}",
                         [f"You started running jobs{place} recently, and {ended - (done or 0)} of your "
                          f"{ended} jobs there so far have not completed."],
                         group="jobs:new",
                         advice=["That is common when starting out on a cluster, and it gets better quickly."],
                         todo=["Research Computing will gladly look at a job script with you: reply to this "
                               + ("email, or write to the address below." if s.contact else "email.")]))
    return out


_TASK = re.compile(r"^(\d+)_(\d+)$")


def _job_words(ids, total: int = 0) -> str:
    """'array 13212 (15 tasks)', '4521, 4519 and array 77 (3 tasks)': the
    jobs a note is about, newest first, so a person can look at each (1.49.0);
    a job array once, with its tasks counted (1.62.0); then "and N more"."""
    parts: dict = {}
    for j in ids:
        m = _TASK.match(str(j))
        k = ("array", m.group(1)) if m else ("job", str(j))
        parts[k] = parts.get(k, 0) + 1
    if not parts:
        return ""
    shown = list(parts.items())[:JOB_IDS_SHOWN]
    more = max(total or 0, len(ids)) - sum(n for _, n in shown)
    words = [f"array {k[1]} ({_many(n, 'task')})" if k[0] == "array" else k[1] for k, n in shown]
    if len(words) == 1:
        text = words[0]
    else:
        text = ", ".join(words[:-1]) + (", " if more > 0 else " and ") + words[-1]
    return text + (f" and {more} more" if more > 0 else "")


def _ids(issue) -> str:
    """The jobs an insight is about, in words (_job_words); '' when nomad has none."""
    ids = [str(j.get("job_id")) for j in (getattr(issue, "jobs", None) or [])
           if isinstance(j, dict) and j.get("job_id")]
    return _job_words(ids, getattr(issue, "affected_jobs", 0) or 0) if ids else ""


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
            entries += _job_entries(db, n, config, s, now, told, conn)
            entries += _quick_entries(conn, n, s, now, told)
        if not entries:
            left["nothing new"] += 1
            continue
        address = f"{n}@{s.mail_domain}" if s.mail_domain else None
        problem = None if address else "no address: set mail_domain"
        out.append({"user": n, "address": address, "to": list(s.review_to) if review else
                    ([address] if address else []), "review": review, "entries": entries, "problem": problem})
    return {"mail": out, "left": left}


# -- the email ----------------------------------------------------------------------------------

_NOTE_ORDER = ("jobs:quick", "jobs:short:", "jobs:new")


def _rank(kind: str, group: str) -> int:
    """Login nodes first; then jobs that stopped within a minute, that ran
    short, getting started; asking for too much last (1.62.0)."""
    if kind == "login_node":
        return 0
    return next((n for n, p in enumerate(_NOTE_ORDER, 1) if group.startswith(p)), len(_NOTE_ORDER) + 1)


def notes_of(entries) -> list[tuple[str, list]]:
    """The email's notes: (kind, [entries]) a group each, in _rank's order,
    then as first met; within a note, the newest first."""
    out: dict = {}
    for e in entries:
        out.setdefault((_rank(e.kind, e.group or e.key), e.group or e.key), []).append(e)
    notes = []
    for (_, _), es in sorted(out.items(), key=lambda kv: kv[0][0]):
        es.sort(key=lambda e: e.started, reverse=True)
        notes.append((es[0].kind, es))
    return notes


def subject_of(m: dict) -> str:
    n = len(notes_of(m["entries"]))
    words = "a note" if n == 1 else f"{n} notes"
    return (f"[review: for {m['user']}] " if m["review"] else "") + f"Research computing: {words} on your recent work"


_COMMAND = ("srun ", "sbatch ", "salloc ", "#SBATCH", "module ")


def _is_command(x: str) -> bool:
    return str(x).lstrip().startswith(_COMMAND)


def _opening(m: dict, s: Settings) -> list[str]:
    who = s.signature[0] if s.signature else "Research Computing"
    n = len(notes_of(m["entries"]))
    things = "something" if n == 1 else f"{n} things"
    return [s.greeting,
            f"NØMAÐ, the monitoring that {who} runs on the clusters, noticed {things} in your recent work that "
            "could go better. Nothing is wrong and nothing has been changed: this is only to help."]


def _closing(s: Settings) -> tuple[str | None, str]:
    details = f"Your recent work in detail, and other ways to do it: {s.link}/me" if s.link else None
    stop = (f"These emails come at most once every {s.every_days} days, and only with something new. "
            + (f"To stop them, turn off \"Email me about my own use\" at {s.link}/me" if s.link
               else "To stop them, reply and say so") + ".")
    return details, stop


def text_of(m: dict, s: Settings) -> str:
    """The email, plain text (1.62.0: numbered notes, each with what NØMAÐ
    saw, the jobs, what to ask for instead, what to do and why; commands on
    lines of their own)."""
    lines = []
    if m["review"]:
        lines += [f"[Review: this would have gone to {m['address'] or m['user']}.]", ""]
    greeting, opening = _opening(m, s)
    lines += [greeting, "", opening, ""]
    for n, (kind, es) in enumerate(notes_of(m["entries"]), 1):
        many = kind == "login_node" and len(es) > 1
        lines += [f"{n}. {es[0].title}" + (f" ({len(es)} times)" if many else ""), ""]
        bullet = len(es) > 1
        for e in es[:SHOWN]:
            first, *rest = e.lines or [""]
            lines.append(("- " if bullet else "") + first)
            pad = "  " if bullet else ""
            lines += [pad + x for x in rest]
            if e.command:
                lines.append("    " + e.command)
            if e.jobs:
                lines.append(f"{pad}Jobs: {e.jobs}")
            if e.change:
                lines.append(f"{pad}Ask for this instead:")
                lines += ["    " + x for x in e.change]
                if e.change_note:
                    lines.append(pad + e.change_note)
            if bullet:
                lines.append("")
        if len(es) > SHOWN:
            lines += [f"... and {len(es) - SHOWN} more like these" + (" on My activity." if s.link else "."), ""]
        elif not bullet:
            lines.append("")
        todo = es[0].todo
        if todo:
            if len(todo) == 1 and not _is_command(todo[0]):
                lines.append(f"What to do: {todo[0]}")
            else:
                lines.append("What to do:")
                lines += [("    " + x) if _is_command(x) else x for x in todo]
            if es[0].advice:
                lines.append("")
        if es[0].advice:
            lines.append("Why: " + " ".join(es[0].advice))
        if todo or es[0].advice:
            lines.append("")
    details, stop = _closing(s)
    if details:
        lines += [details, ""]
    lines += [stop, ""]
    if s.contact:
        lines += [s.contact, ""]
    lines += list(s.signature)
    return "\n".join(lines).rstrip() + "\n"


_KEEP_UNIT = re.compile(r"(\d) (?=(?:h|min|days?|kB|MB|GB|TB|cores?|GPUs?|tasks?|jobs?)\b)")
_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif")
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
_LABEL = "margin:14px 0 4px;font-weight:600;font-size:14px"
_CODE = (f"margin:6px 0;padding:10px 12px;border:1px solid #c9d1d9;border-radius:6px;font-family:{_MONO};"
         "font-size:14px;line-height:1.45;white-space:pre-wrap;word-break:break-word")


def html_of(m: dict, s: Settings) -> str:
    """The email as a page (1.62.0), beside text_of's plain copy: the same
    words, each note in a box with its parts under small headings, commands
    in a box of their own. No colours of its own for text or background, so
    a mail program's dark mode reads it as well as its light one."""
    from html import escape

    def h(x, quote: bool = True) -> str:
        # a number keeps its unit on the same line ("6 days 10 h", not "10" / "h")
        return _KEEP_UNIT.sub("\\1&nbsp;", escape(str(x), quote=quote))

    def link(url: str) -> str:
        return f'<a href="{escape(url, quote=True)}">{escape(url)}</a>'

    def para(x: str, style: str = "") -> str:
        if _is_command(x):
            return f'<pre style="{_CODE}">{escape(x)}</pre>'
        return f'<p style="margin:6px 0;{style}">{h(x)}</p>'

    out = []
    if m["review"]:
        out.append(f'<p style="margin:0 0 16px;padding:8px 12px;border:1px dashed #8c959f;border-radius:6px;'
                   f'font-size:13px">Review: this would have gone to {h(m["address"] or m["user"])}.</p>')
    greeting, opening = _opening(m, s)
    out += [f'<p style="margin:0 0 12px">{h(greeting)}</p>', f'<p style="margin:0 0 8px">{h(opening)}</p>']
    for n, (kind, es) in enumerate(notes_of(m["entries"]), 1):
        many = kind == "login_node" and len(es) > 1
        title = f"{n}. {es[0].title}" + (f" ({len(es)} times)" if many else "")
        out.append('<div style="margin:20px 0 0;padding:14px 18px 10px;border:1px solid #c9d1d9;border-radius:10px">')
        out.append(f'<h2 style="margin:0 0 6px;font-size:17px;line-height:1.35">{h(title)}</h2>')
        out.append(f'<p style="{_LABEL}">What NØMAÐ saw</p>')
        items = []
        for e in es[:SHOWN]:
            bits = [f'<p style="margin:6px 0">{h(x)}</p>' for x in (e.lines or [])]
            if e.command:
                bits.append(f'<pre style="{_CODE}">{escape(e.command)}</pre>')
            if e.jobs:
                bits.append(f'<p style="margin:6px 0;font-size:14px">Jobs: {h(e.jobs)}</p>')
            if e.change:
                bits.append(f'<p style="{_LABEL}">Ask for this instead</p>')
                bits.append(f'<pre style="{_CODE}">{escape(chr(10).join(e.change))}</pre>')
                if e.change_note:
                    bits.append(f'<p style="margin:6px 0;font-size:14px">{h(e.change_note)}</p>')
            items.append("".join(bits))
        if len(es) > 1:
            out.append('<ul style="margin:0;padding-left:20px">'
                       + "".join(f'<li style="margin:0 0 8px">{x}</li>' for x in items) + "</ul>")
        else:
            out += items
        if len(es) > SHOWN:
            more = f"... and {len(es) - SHOWN} more like these"
            out.append(f'<p style="margin:6px 0">{h(more)}'
                       + (f' on {link(s.link + "/me")}.' if s.link else ".") + "</p>")
        if es[0].todo:
            out.append(f'<p style="{_LABEL}">What to do</p>')
            out += [para(x) for x in es[0].todo]
        if es[0].advice:
            out.append(f'<p style="{_LABEL}">Why</p>')
            out.append(para(" ".join(es[0].advice)))
        out.append("</div>")
    details, stop = _closing(s)
    if details:
        out.append('<p style="margin:22px 0 8px">Your recent work in detail, and other ways to do it: '
                   f'{link(s.link + "/me")}</p>')
    stop_html = h(stop)
    if s.link:
        url = f"{s.link}/me"
        stop_html = stop_html.replace(escape(url), link(url))
    out.append(f'<p style="margin:8px 0;font-size:13px">{stop_html}</p>')
    if s.contact:
        out.append(f'<p style="margin:8px 0">{h(s.contact)}</p>')
    if s.signature:
        out.append('<p style="margin:12px 0 0">' + "<br>".join(h(x) for x in s.signature) + "</p>")
    body = "\n".join(out)
    return ('<!doctype html>\n<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{h(subject_of(m))}</title></head>\n"
            f'<body style="margin:0;padding:0"><div style="max-width:640px;margin:0 auto;padding:20px 16px;'
            f'font-family:{_FONT};font-size:15px;line-height:1.55">\n{body}\n</div></body></html>\n')


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
        m["html"] = html_of(m, s)
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
        msg.add_alternative(m["html"], subtype="html")      # 1.62.0: laid out; the plain copy first
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
