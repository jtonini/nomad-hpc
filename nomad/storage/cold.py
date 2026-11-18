# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Data unchanged for a year: one list, and the conversations about it (1.44.0).

Everyone holding at least ``min_tb`` of data that hasn't changed in a year,
on every storage server nomad-storage.sh scans (copies apart), in one list
for Research Computing: who, where, how much of how much, when anything of
it last changed, their last job, their labs and PIs, and what the lab check
said of them (has left, their data can be removed...). Its purpose is the
conversation with them about a better place for it, so each one can become
a case with a number (ARC-0013, the account requests' sequence:
nomad.tickets) that goes through states, with notes:

    to contact -> contacted -> moving it -> moved (or removed)
                                         -> kept (and why)

No alert for each person: a weekly email to Research Computing (``notify``)
says what is new on the list, the cases and those not touched lately, and
first of all any server nearly full (``full_share``) with data on the list.

"Unchanged a year": the scan counts the bytes older than two ages
(nomad-storage.sh -a, 180,365 days by default); ``days`` takes the smallest
of them at least that old (else the largest, and says so). A file's age is
its last change (mtime): reading it doesn't count.

Settings, in the hub's nomad.toml::

    [console.cold_data]
    min_tb = 1.0                 # listed from this much unchanged data on one path (decimal TB)
    days = 365                   # unchanged this long
    full_share = 0.85            # a path this full is said first
    quiet_days = 30              # an open case not updated this long is called out
    notify = ["hpc@example.edu"] # the weekly email (default: [console.labs.accounts] notify)
    link = "https://nomad.example.edu/diagnostics?tab=storage"   # the list, in the email
    storage_link = "https://nomad.example.edu/storage"   # a person's own storage page, in a letter
                                 # (default: link's address, else [alerts] link, + /storage)

**Writing to them (1.57.0).** ``draft`` makes the email a person gets about
it, for staff to read and change before it goes (the Console's Write to
them; ``nomad storage cold letter NETID``): to them (NETID@mail_domain, the
lab check's), a copy to their faculty sponsor -- the PI of each lab they are
in, unless they lead one themselves -- and to Research Computing
(``notify``); each place with how much of theirs hasn't changed in a year,
their storage page, what they can do, and a reply goes to whoever sent it
and to Research Computing. ``send_letter`` sends it, keeps it whole
(``data_letters``), and their cases move to "contacted" (opened where there
was none), the email noted in each. "Unchanged" is what the scans know: a
file's last change, not whether anyone read it.

The cases are kept in lab_check.db (the nomad account's, beside combined.db),
every change in its log.
"""
from __future__ import annotations

import csv
import getpass
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad import tickets as T

TB = 1e12
STATES = ("to_contact", "contacted", "moving", "keep", "moved")
OPEN = ("to_contact", "contacted", "moving")
SAID = {"to_contact": "to contact", "contacted": "contacted", "moving": "moving it", "keep": "kept",
        "moved": "moved or removed"}
NOTE_MAX = 600
SCHEMA = """
CREATE TABLE IF NOT EXISTS data_cases (
    number INTEGER PRIMARY KEY, owner TEXT NOT NULL, server TEXT NOT NULL, path TEXT NOT NULL,
    state TEXT NOT NULL, reason TEXT, opened_by TEXT NOT NULL, opened_at TEXT NOT NULL,
    updated_by TEXT, updated_at TEXT, bytes_at_open INTEGER, unchanged_at_open INTEGER);
CREATE INDEX IF NOT EXISTS idx_data_cases ON data_cases(owner, server, path);
CREATE TABLE IF NOT EXISTS data_case_notes (
    number INTEGER NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL, state TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS data_cold_seen (
    owner TEXT NOT NULL, server TEXT NOT NULL, path TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (owner, server, path));
CREATE TABLE IF NOT EXISTS data_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL,
    sent_to TEXT NOT NULL, cc TEXT, reply_to TEXT, subject TEXT NOT NULL, text TEXT NOT NULL, cases TEXT,
    unchanged_bytes INTEGER);
CREATE INDEX IF NOT EXISTS idx_data_letters ON data_letters(person, at);
"""
CSV_COLUMNS = ("case", "state", "person", "server", "path", "unchanged_tb", "total_tb", "unchanged_share",
               "unchanged_days", "last_change", "last_job", "labs", "pis", "lab_check", "on_list_since",
               "folders", "path_full_share")


class CaseError(ValueError):
    """The case can't be opened or changed as asked: what is wrong, in words."""


@dataclass(frozen=True)
class Settings:
    min_tb: float = 1.0
    days: int = 365
    full_share: float = 0.85
    quiet_days: int = 30
    notify: tuple = ()
    link: str = ""
    storage_link: str = ""
    problems: tuple = field(default_factory=tuple)
    tickets: T.Settings = field(default_factory=T.Settings)

    @property
    def min_bytes(self) -> int:
        return int(self.min_tb * TB)


def settings(config: dict | None) -> Settings:
    console = (config or {}).get("console") or {}
    raw = console.get("cold_data") if isinstance(console.get("cold_data"), dict) else {}
    problems = []

    def number(key, default, lo, hi, kind=float):
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi \
                or (kind is int and int(v) != v):
            problems.append(f"[console.cold_data] {key} should be a number from {lo} to {hi}")
            return default
        return kind(v)
    min_tb = number("min_tb", 1.0, 0.01, 10_000)
    days = number("days", 365, 30, 3650, int)
    full = number("full_share", 0.85, 0.5, 1.0)
    quiet = number("quiet_days", 30, 1, 365, int)
    notify = raw.get("notify")
    if notify is None:
        labs = console.get("labs") if isinstance(console.get("labs"), dict) else {}
        acc = labs.get("accounts") if isinstance(labs.get("accounts"), dict) else {}
        notify = acc.get("notify", [])
    if isinstance(notify, str):
        notify = [notify]
    if not (isinstance(notify, list) and all(isinstance(a, str) and "@" in a for a in notify)):
        problems.append("[console.cold_data] notify should be a list of addresses")
        notify = []
    link = raw.get("link", "")
    link = link if isinstance(link, str) and link.startswith(("https://", "http://")) else ""
    mine = raw.get("storage_link")
    if mine is not None and not (isinstance(mine, str) and mine.startswith(("https://", "http://"))):
        problems.append("[console.cold_data] storage_link should start with https://")
        mine = None
    if mine is None:
        alerts = (config or {}).get("alerts") if isinstance((config or {}).get("alerts"), dict) else {}
        base = _origin(link) or _origin(alerts.get("link"))
        mine = f"{base}/storage" if base else ""
    numbers = T.settings(config)
    return Settings(min_tb=min_tb, days=days, full_share=full, quiet_days=quiet, notify=tuple(notify), link=link,
                    storage_link=mine.rstrip("/"), problems=tuple(problems) + numbers.problems, tickets=numbers)


def _origin(link) -> str:
    """https://host of a link ("" when it isn't one)."""
    import re
    m = re.match(r"(https?://[^/\s?#]+)", link) if isinstance(link, str) else None
    return m.group(1) if m else ""


# --- the records -------------------------------------------------------------------------

def open_records(path, write: bool = False):
    """lab_check.db, with the cases' tables (and the numbers') when written."""
    from nomad import account_requests, lab_check
    rec = lab_check.open_records(path, write)
    if rec is not None and write:
        rec.executescript(account_requests.SCHEMA)
        rec.executescript(SCHEMA)
        T.ensure(rec)
    return rec


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rows(rec, sql: str, args=()) -> list[dict]:
    if rec is None:
        return []
    try:
        return [dict(r) for r in rec.execute(sql, tuple(args))]
    except sqlite3.OperationalError:          # no such table yet: no case
        return []


def _log(rec, by: str, what: dict) -> None:
    rec.execute("INSERT INTO lab_check_log (at, by, what) VALUES (?, ?, ?)", (_now(), by, json.dumps(what, sort_keys=True)))


def clean_note(note) -> str | None:
    if not isinstance(note, str):
        return None
    note = "".join(c for c in " ".join(note.split()) if c.isprintable())
    return note[:NOTE_MAX] or None


def ref_of(number, s: Settings | None = None) -> str:
    return T.ref(number, (s or Settings()).tickets)


def _case(row: dict, s: Settings | None, notes: list | None = None) -> dict:
    out = {**row, "ref": ref_of(row["number"], s), "said": SAID.get(row["state"], row["state"]),
           "open": row["state"] in OPEN}
    if notes is not None:
        out["notes"] = notes
    return out


def cases(rec, s: Settings | None = None, open_only: bool = False, notes: bool = False) -> list[dict]:
    """The cases, newest first; with their notes, oldest first, when asked."""
    got = _rows(rec, "SELECT * FROM data_cases" + (" WHERE state IN ('to_contact', 'contacted', 'moving')"
                                                   if open_only else "") + " ORDER BY number DESC")
    by: dict = {}
    if notes and got:
        for n in _rows(rec, "SELECT * FROM data_case_notes ORDER BY at, rowid"):
            by.setdefault(n["number"], []).append(n)
    return [_case(r, s, by.get(r["number"], []) if notes else None) for r in got]


def case_of(rec, number: int, s: Settings | None = None) -> dict | None:
    got = _rows(rec, "SELECT * FROM data_cases WHERE number = ?", (int(number),))
    if not got:
        return None
    return _case(got[0], s, _rows(rec, "SELECT * FROM data_case_notes WHERE number = ? ORDER BY at, rowid",
                                  (int(number),)))


def open_case(rec, owner: str, server: str, path: str, by: str, note: str | None = None,
              row: dict | None = None, s: Settings | None = None) -> dict:
    """A case about ``owner``'s data on ``server``:``path``, to contact them;
    its number from the requests' sequence. One open case at a time for the
    same data. ``row``: what the list says of it now (kept as it was then)."""
    owner, server, path = (str(x or "").strip() for x in (owner, server, path))
    if not owner or not server or not path.startswith("/"):
        raise CaseError("say whose data, on which server and path")
    already = _rows(rec, "SELECT number FROM data_cases WHERE owner = ? AND server = ? AND path = ? "
                         "AND state IN ('to_contact', 'contacted', 'moving')", (owner, server, path))
    if already:
        raise CaseError(f"{ref_of(already[0]['number'], s)} is already open about it")
    who = (by or getpass.getuser()).strip().lower()
    note = clean_note(note)
    now = _now()
    with rec:
        number = T.take(rec, "data")
        rec.execute("INSERT INTO data_cases (number, owner, server, path, state, opened_by, opened_at, updated_by, "
                    "updated_at, bytes_at_open, unchanged_at_open) VALUES (?, ?, ?, ?, 'to_contact', ?, ?, ?, ?, ?, ?)",
                    (number, owner, server, path, who, now, who, now, (row or {}).get("bytes"),
                     (row or {}).get("unchanged_bytes")))
        rec.execute("INSERT INTO data_case_notes (number, at, by, state, note) VALUES (?, ?, ?, 'to_contact', ?)",
                    (number, now, who, note))
        _log(rec, who, {"data_case": {"number": number, "ref": ref_of(number, s), "owner": owner, "server": server,
                                      "path": path, "state": "to_contact", "note": note}})
    return case_of(rec, number, s)


def update_case(rec, number: int, by: str, state: str | None = None, note: str | None = None,
                s: Settings | None = None) -> dict:
    """A case moves on (``state``), or gets a note, or both. Kept needs why
    (the note); a note alone is kept with no change of state."""
    if state is not None and state not in STATES:
        raise CaseError(f"{state!r}: one of {', '.join(STATES)}")
    note = clean_note(note)
    got = case_of(rec, number, s)
    if got is None:
        raise CaseError(f"no case {ref_of(number, s)}")
    if state == got["state"]:
        state = None
    if state is None and not note:
        raise CaseError("nothing to change: a new state, or a note")
    if state == "keep" and not note:
        raise CaseError("say why it is kept, in a note")
    who = (by or getpass.getuser()).strip().lower()
    now = _now()
    with rec:
        if state is not None:
            rec.execute("UPDATE data_cases SET state = ?, reason = ?, updated_by = ?, updated_at = ? WHERE number = ?",
                        (state, note if state == "keep" else None, who, now, int(number)))
        else:
            rec.execute("UPDATE data_cases SET updated_by = ?, updated_at = ? WHERE number = ?", (who, now, int(number)))
        rec.execute("INSERT INTO data_case_notes (number, at, by, state, note) VALUES (?, ?, ?, ?, ?)",
                    (int(number), now, who, state, note))
        _log(rec, who, {"data_case": {"number": int(number), "ref": ref_of(number, s), "state": state, "note": note}})
    return case_of(rec, number, s)


# --- the list ----------------------------------------------------------------------------

def _unchanged(r: dict, days: int) -> tuple[int | None, int | None]:
    """(bytes unchanged at least ``days``, the age it is counted at): the
    smallest of the scan's two ages that is at least ``days``, else the largest."""
    pairs = [(a, b) for a, b in ((r["ages"][0], r["older1_bytes"]), (r["ages"][1], r["older2_bytes"]))
             if a is not None]
    if not pairs:
        return None, None
    enough = [p for p in pairs if p[0] >= days]
    age, size = min(enough) if enough else max(pairs)
    return int(size or 0), int(age)


def _labs_of(conn, config) -> dict:
    """netid -> [{lab, name, pis, pi}]: the labs a person is in now, or leads."""
    if conn is None:
        return {}
    try:
        from nomad import lab_check
        got = lab_check.labs(conn, config, None, lab_check.settings(config, None))
    except Exception:            # noqa: BLE001 -- no groups reading, an odd config: no labs said
        return {}
    out: dict = {}
    for lab in got:
        for n in lab.members:
            out.setdefault(n, []).append({"lab": lab.group, "name": lab.name, "pis": list(lab.leaders), "pi": False})
        for n in lab.leaders:
            out.setdefault(n, []).append({"lab": lab.group, "name": lab.name, "pis": list(lab.leaders), "pi": True})
    return out


def _answers(rec) -> dict:
    """netid -> what the lab check said of them last (any lab), in words."""
    from nomad import lab_check
    out: dict = {}
    for r in _rows(rec, "SELECT * FROM lab_check_answers ORDER BY at, round"):
        words = lab_check.SAID.get(r["answer"], r["answer"])
        if r.get("data"):
            words += "; " + lab_check.SAID.get(r["data"], r["data"])
        out[r["netid"]] = {"lab": r["lab"], "answer": r["answer"], "data": r.get("data"), "at": r["at"],
                           "said": words}
    return out


def listing(conn, rec=None, config: dict | None = None, s: Settings | None = None,
            now: datetime | None = None) -> dict:
    """The list: each person's data on each path (copies apart) with at least
    ``min_tb`` unchanged ``days``, largest first; the paths, how full, and
    how much of them is on the list; what couldn't be measured."""
    from nomad.storage import views
    s = s or settings(config)
    now = now or datetime.now()
    out = {"as_of": now.isoformat(timespec="seconds"), "min_tb": s.min_tb, "days": s.days,
           "full_share": s.full_share, "rows": [], "paths": [], "not_measured": []}
    if conn is None or not views.available(conn):
        out["not_measured"].append("no storage scans in this database (nomad storage, nomad-storage.sh)")
        return out
    jobs = views.last_jobs(conn)
    labs = _labs_of(conn, config)
    said = _answers(rec)
    seen = {(r["owner"], r["server"], r["path"]): r["first_seen"]
            for r in _rows(rec, "SELECT * FROM data_cold_seen")}
    by_key: dict = {}
    for c in reversed(cases(rec, s)):          # the newest case about the same data last: it stands
        by_key[(c["owner"], c["server"], c["path"])] = c
    wrote = last_letters(rec)
    for p in views.paths(conn):
        if p.get("copy_of"):
            continue
        scanned = views.folders_by_owner(conn, p["server"], p["path"])
        share = (p["used_bytes"] / p["total_bytes"]) if p.get("used_bytes") and p.get("total_bytes") else None
        if not scanned:
            out["not_measured"].append(f"{p['server']}:{p['path']}: no scan of its folders' ages yet")
            continue
        listed, ages = 0, set()
        for owner, r in scanned.items():
            size, age = _unchanged(r, s.days)
            if size is None:
                continue
            ages.add(age)
            if size < s.min_bytes:
                continue
            listed += size
            key = (owner, p["server"], p["path"])
            out["rows"].append({
                "person": owner, "server": p["server"], "path": p["path"], "folders": list(r["tops"]),
                "bytes": r["bytes"], "unchanged_bytes": size, "unchanged_days": age,
                "share": round(size / r["bytes"], 3) if r["bytes"] else None, "last_change": r["newest"],
                "scanned_at": r["scanned_at"], "complete": r["complete"], "last_job": jobs.get(owner),
                "labs": labs.get(owner, []), "lab_check": said.get(owner), "on_list_since": seen.get(key),
                "case": by_key.get(key), "path_full_share": round(share, 3) if share is not None else None,
                "last_letter": wrote.get(owner)})
        out["paths"].append({"server": p["server"], "path": p["path"], "used_bytes": p.get("used_bytes"),
                             "total_bytes": p.get("total_bytes"), "full_share": round(share, 3) if share else None,
                             "nearly_full": bool(share is not None and share >= s.full_share),
                             "listed_bytes": listed, "ages": sorted(ages)})
        if ages and min(ages) < s.days:
            out["not_measured"].append(f"{p['server']}:{p['path']}: the scan counts ages up to {max(ages)} days, "
                                       f"not {s.days}: listed by {max(ages)} (nomad-storage.sh -a)")
    out["rows"].sort(key=lambda r: (-r["unchanged_bytes"], r["person"], r["server"], r["path"]))
    out["total_bytes"] = sum(r["unchanged_bytes"] for r in out["rows"])
    out["people"] = len({r["person"] for r in out["rows"]})
    return out


def note_seen(rec, got: dict) -> list:
    """Mark when each listed one was first on the list; the new ones."""
    now = _now()
    new = []
    with rec:
        for r in got["rows"]:
            cur = rec.execute("INSERT OR IGNORE INTO data_cold_seen (owner, server, path, first_seen) VALUES (?, ?, ?, ?)",
                              (r["person"], r["server"], r["path"], now))
            if cur.rowcount:
                r["on_list_since"] = now
                new.append(r)
    return new


# --- writing to them (1.57.0) -------------------------------------------------------------

AGAIN_SHARE = 0.9            # still this much of what was unchanged when written to: "nothing changed"
_NETID = __import__("re").compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ADDRESS = __import__("re").compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def last_letters(rec) -> dict:
    """{person: {at, by, unchanged_bytes, count}}: the last letter each was sent."""
    out: dict = {}
    for r in _rows(rec, "SELECT person, at, by, unchanged_bytes FROM data_letters ORDER BY at, id"):
        n = out.get(r["person"], {}).get("count", 0) + 1
        out[r["person"]] = {"at": r["at"], "by": r["by"], "unchanged_bytes": r["unchanged_bytes"], "count": n}
    return out


def letters_of(rec, person: str) -> list:
    """The letters a person was sent, newest first, whole."""
    return _rows(rec, "SELECT * FROM data_letters WHERE person = ? ORDER BY at DESC, id DESC", (person,))


def write_again(rows: list, s: Settings, now: datetime | None = None) -> dict:
    """{person: the letter} for those written to ``quiet_days`` ago or more
    whose data is still there (AGAIN_SHARE of what was unchanged then, on
    the paths of their open cases): time to write again."""
    now = now or datetime.now()
    cut = (now - timedelta(days=s.quiet_days)).isoformat(timespec="seconds")
    by: dict = {}
    for r in rows:
        if r.get("case") and not r["case"]["open"]:
            continue
        by.setdefault(r["person"], []).append(r)
    out = {}
    for person, mine in by.items():
        last = mine[0].get("last_letter")
        if not last or last["at"] > cut:
            continue
        now_bytes = sum(r["unchanged_bytes"] for r in mine)
        if not last.get("unchanged_bytes") or now_bytes >= AGAIN_SHARE * last["unchanged_bytes"]:
            out[person] = last
    return out


def sponsors(labs: list, person: str) -> tuple[bool, list]:
    """(they lead a lab themselves, the faculty sponsors to copy): the PI of
    each lab they are in, when they lead none."""
    if any(x.get("pi") for x in labs or ()):
        return True, []
    return False, sorted({p for x in labs or () for p in x.get("pis") or () if p and p != person})


def address_of(who: str, domain: str | None) -> str | None:
    """An address as typed, or NETID@domain."""
    w = (who or "").strip()
    if _ADDRESS.match(w):
        return w
    if domain and _NETID.match(w.lower()):
        return f"{w.lower()}@{domain}"
    return None


def first_names(config: dict | None, netids) -> dict:
    """{netid: given name} from the campus directory ([directory]), where it
    can be asked; {} otherwise (the letter then says Hello)."""
    try:
        from nomad import directory as D
        s = D.settings(config)
        if not s.on:
            return {}
        return D.Directory(s).first_names(netids)
    except Exception:            # noqa: BLE001 -- no directory, no names: the letter still goes
        return {}


def _places(config: dict | None) -> dict:
    """{server (lower case): what it is}, as nomad.toml describes machines."""
    try:
        from nomad.config.access import access_from
        return {str(k).lower(): v for k, v in access_from(config, log=False).notes().items() if v}
    except Exception:            # noqa: BLE001 -- the server's name alone
        return {}


def _days_words(days: int) -> str:
    return "a year" if days == 365 else f"{days} days"


def draft(got: dict, person: str, s: Settings, check_settings, config: dict | None = None, sender: str | None = None,
          names: dict | None = None, rec=None) -> dict:
    """The email ``person`` would get about their data on the list, for staff
    to read and change: {to, cc, reply_to, from, subject, text, places,
    sponsors, faculty, problems, letters}. Raises CaseError when they aren't
    on the list now."""
    person = (person or "").strip()
    rows = [r for r in got.get("rows", []) if r["person"] == person]
    if not rows:
        raise CaseError(f"{person} isn't on the list of data unchanged for {got.get('days', s.days)} days now")
    domain = getattr(check_settings, "mail_domain", None)
    problems = []
    faculty, sponsor_ids = sponsors(rows[0].get("labs"), person)
    to = [a for a in [address_of(person, domain)] if a]
    if not to:
        problems.append("no address for them: mail_domain in [console.labs.check] (or type it)")
    cc = [a for a in (address_of(p, domain) for p in sponsor_ids) if a]
    center = list(s.notify)
    me = address_of(sender or "", domain) if sender else None
    reply = ([me] if me else []) + [a for a in center if a != me]
    if not faculty and not sponsor_ids:
        problems.append("nomad knows no faculty sponsor for them (no lab group): add one by hand if there is one")
    names = names if names is not None else first_names(config, [person] + ([sender] if sender else []))
    notes = _places(config)
    total = sum(r["unchanged_bytes"] for r in rows)
    held = sum(r["bytes"] or 0 for r in rows)
    days = min(r["unchanged_days"] for r in rows)
    servers = sorted({r["server"] for r in rows})
    where = ", ".join(f"{x} ({notes[x.lower()]})" if notes.get(x.lower()) else x for x in servers)
    subject = f"Your data on {where}: {tb(total)} unchanged for {_days_words(days)}"
    lines = [f"Dear {names[person]}," if names.get(person) else "Hello,", ""]
    lines.append(f"Our storage scans show data of yours that hasn't changed in {_days_words(days)} or more:")
    lines.append("")
    for r in rows:
        place = notes.get(r["server"].lower())
        lines.append(f"  {r['path']} on {r['server']}" + (f" ({place})" if place else "") + ": "
                     f"{tb(r['unchanged_bytes'])} of the {tb(r['bytes'])} you hold there")
    lines.append("")
    if s.storage_link:
        lines += [f"Your storage page shows which folders, once you sign in: {s.storage_link}", ""]
    lines += ["Could you remove what you no longer need, or move it somewhere better suited to keeping it, "
              "such as your lab's storage? If you need any of it where it is, or would like help moving it, "
              "just reply to this email.", ""]
    if cc:
        lines += ["I'm copying your faculty sponsor.", ""]
    lines.append("Thank you,")
    if sender and names.get(sender):
        lines.append(names[sender])
    sig = list(getattr(check_settings, "signature", ()) or ())
    lines += sig
    return {"person": person, "to": to, "cc": cc + [a for a in center if a not in cc], "reply_to": reply,
            "from": getattr(check_settings, "send_from", None), "subject": subject, "text": "\n".join(lines) + "\n",
            "places": [{"server": r["server"], "path": r["path"], "unchanged_bytes": r["unchanged_bytes"],
                        "bytes": r["bytes"], "case": r.get("case")} for r in rows],
            "unchanged_bytes": total, "bytes": held, "sponsors": sponsor_ids, "faculty": faculty,
            "problems": problems, "letters": [{k: x[k] for k in ("at", "by", "sent_to", "cc", "subject")}
                                              for x in letters_of(rec, person)] if rec is not None else []}


def addresses(values, domain: str | None, what: str) -> list:
    """The addresses typed (or NetIDs, with the domain); CaseError naming one that isn't."""
    out = []
    if isinstance(values, str):
        values = [values]
    for v in values or ():
        for part in str(v).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            a = address_of(part, domain)
            if a is None:
                raise CaseError(f"{what}: {part!r} isn't an address")
            if a not in out:
                out.append(a)
    return out


def send_letter(config: dict | None, check_settings, s: Settings, rec, got: dict, person: str, to, cc,
                subject: str, text: str, by: str, sender=None) -> dict:
    """Send the letter as written, keep it whole, and move their cases on:
    each place of theirs on the list gets a case "contacted" (opened if it
    had none), the email noted in it. Nothing is kept unless it went."""
    from email.message import EmailMessage

    from nomad import mail
    person = (person or "").strip()
    rows = [r for r in got.get("rows", []) if r["person"] == person]
    if not rows:
        raise CaseError(f"{person} isn't on the list now")
    domain = getattr(check_settings, "mail_domain", None)
    to = addresses(to, domain, "to")
    cc = [a for a in addresses(cc, domain, "copy") if a not in to]
    if not to:
        raise CaseError("to whom: no address")
    subject = " ".join(str(subject or "").split())[:200]
    text = str(text or "").replace("\r\n", "\n").strip()
    if not subject or not text:
        raise CaseError("a subject and a message")
    if len(text) > 20000:
        raise CaseError("the message is too long (20,000 characters at most)")
    who = (by or getpass.getuser()).strip().lower()
    me = address_of(who, domain)
    reply = ([me] if me else []) + [a for a in s.notify if a != me]
    cfg = mail.settings(config or {})
    frm = getattr(check_settings, "send_from", None) or cfg.get("from")
    if not frm:
        raise CaseError("no sender: send_from in [console.labs.check], or [mail] from")
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply:
        msg["Reply-To"] = ", ".join(reply)
    msg["Subject"] = subject
    msg.set_content(text + "\n")
    try:
        (sender or mail.send)(msg, cfg)
    except Exception as exc:            # noqa: BLE001 -- said; nothing kept
        raise CaseError("not sent: " + mail.failure_label(exc)) from None
    note = clean_note(f"emailed {', '.join(to)}" + (f" (copy {', '.join(cc)})" if cc else "") + f": {subject}")
    refs = []
    for r in rows:
        c = r.get("case")
        if c and c["open"]:
            got_case = update_case(rec, c["number"], who, state="contacted" if c["state"] == "to_contact" else None,
                                   note=note, s=s)
        else:
            opened = open_case(rec, person, r["server"], r["path"], who, row=r, s=s)
            got_case = update_case(rec, opened["number"], who, state="contacted", note=note, s=s)
        refs.append(got_case["ref"])
    total = sum(r["unchanged_bytes"] for r in rows)
    with rec:
        rec.execute("INSERT INTO data_letters (person, at, by, sent_to, cc, reply_to, subject, text, cases, "
                    "unchanged_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (person, _now(), who, ", ".join(to), ", ".join(cc) or None, ", ".join(reply) or None, subject,
                     text, ", ".join(refs), total))
        _log(rec, who, {"data_letter": {"person": person, "to": to, "cc": cc, "subject": subject, "cases": refs}})
    return {"status": "sent", "to": to, "cc": cc, "reply_to": reply, "cases": refs}


def tb(n) -> str:
    return "–" if n is None else f"{n / TB:,.1f} TB"


def _day(iso) -> str:
    return str(iso)[:10] if iso else ""


def csv_text(got: dict) -> str:
    """The list as a spreadsheet, a row each (it names people: Research
    Computing's own); text a spreadsheet would run as a formula kept as text."""
    from nomad.account_requests import _cell
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in got["rows"]:
        c = r.get("case") or {}
        w.writerow([_cell(x) for x in (
            c.get("ref", ""), c.get("said", ""), r["person"], r["server"], r["path"],
            round(r["unchanged_bytes"] / TB, 2), round((r["bytes"] or 0) / TB, 2),
            "" if r["share"] is None else r["share"], r["unchanged_days"], _day(r["last_change"]),
            _day(r["last_job"]), "; ".join(x["lab"] for x in r["labs"]),
            "; ".join(sorted({p for x in r["labs"] for p in x["pis"]})),
            (r["lab_check"] or {}).get("said", ""), _day(r["on_list_since"]), "; ".join(r["folders"]),
            "" if r["path_full_share"] is None else r["path_full_share"])])
    return out.getvalue()


# --- the weekly email --------------------------------------------------------------------

def digest(got: dict, new: list, open_cases: list, s: Settings, now: datetime | None = None) -> tuple[str, str]:
    """(subject, text) of the weekly email to Research Computing."""
    now = now or datetime.now()
    full = [p for p in got["paths"] if p["nearly_full"]]
    subject = (f"Data unchanged for a year: {got['people']} {'person' if got['people'] == 1 else 'people'}, "
               f"{tb(got['total_bytes'])}" + (f"; {len(full)} nearly full" if full else ""))
    lines = []
    for p in full:
        lines.append(f"NEARLY FULL: {p['server']}:{p['path']} is {round(100 * p['full_share'])}% full; "
                     f"{tb(p['listed_bytes'])} of it is on the list.")
    if full:
        lines.append("")
    lines.append(f"{got['people']} people hold {tb(got['total_bytes'])} unchanged for {s.days} days or more "
                 f"(at least {s.min_tb:g} TB each on a path).")
    if new:
        lines += ["", f"New on the list ({len(new)}):"]
        lines += [f"  {r['person']:<12} {r['server']}:{r['path']}  {tb(r['unchanged_bytes'])} of {tb(r['bytes'])}"
                  for r in new[:20]]
        if len(new) > 20:
            lines.append(f"  and {len(new) - 20} more")
    counts = {st: sum(1 for c in open_cases if c["state"] == st) for st in OPEN}
    if open_cases:
        lines += ["", "Open cases: " + ", ".join(f"{n} {SAID[st]}" for st, n in counts.items() if n) + "."]
        cut = (now - timedelta(days=s.quiet_days)).isoformat(timespec="seconds")
        quiet = [c for c in open_cases if (c.get("updated_at") or c["opened_at"]) < cut]
        if quiet:
            lines.append(f"Not updated in {s.quiet_days} days:")
            lines += [f"  {c['ref']} {c['owner']} {c['server']}:{c['path']} ({c['said']} since "
                      f"{_day(c.get('updated_at') or c['opened_at'])})" for c in quiet]
    again = write_again(got["rows"], s, now)
    if again:
        lines += ["", f"Written to {s.quiet_days} days ago or more, and their data still there "
                      f"(write again from the list): {len(again)}"]
        lines += [f"  {p:<12} written {_day(x['at'])} by {x['by']}" for p, x in sorted(again.items())]
    without = [r for r in got["rows"] if not r.get("case")]
    if without:
        lines += ["", f"Not a case yet: {len(without)}, {tb(sum(r['unchanged_bytes'] for r in without))}; "
                      "the largest:"]
        lines += [f"  {r['person']:<12} {r['server']}:{r['path']}  {tb(r['unchanged_bytes'])}"
                  + (f"  ({r['lab_check']['said']})" if r.get("lab_check") else "") for r in without[:10]]
    if got["not_measured"]:
        lines += ["", "Not measured: " + "; ".join(got["not_measured"])]
    lines += ["", "The list, and the cases: " + (s.link or "the Console, Diagnostics, Storage") + "."]
    return subject, "\n".join(lines) + "\n"


def send_digest(config, check_settings, s: Settings, subject: str, text: str) -> str:
    if not s.notify:
        return "not sent: no one to tell ([console.cold_data] notify)"
    from nomad.account_requests import send
    return send(config, check_settings, subject, text, list(s.notify))


__all__ = ["Settings", "settings", "CaseError", "STATES", "OPEN", "SAID", "open_records", "cases", "case_of",
           "open_case", "update_case", "listing", "note_seen", "csv_text", "digest", "send_digest", "ref_of", "tb",
           "CSV_COLUMNS", "TB", "draft", "send_letter", "letters_of", "last_letters", "write_again", "sponsors",
           "addresses"]
