# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A lab's PI asks ARC for accounts for their students (1.42.0).

On My lab in the Console, a PI types NetIDs or names (First Last, or Last,
First), pastes a list, or gives a table (CSV, tab-separated or Excel, with
a header row naming its columns: NetID, Email, First name, Last name or
Name), and picks the clusters. Before anything goes to ARC, nomad says who
each one is, from the campus directory (nomad.directory): found, with their
name; several people of that name (the PI picks); not found; already has an
account on a cluster (nomad has their jobs, logins or groups there); already
asked for. The PI sends the ones they mean, and ARC is emailed (``notify``).

ARC makes the accounts as it does now (nomad makes none), and closes each
one here, made or not made, with a note; the PI is emailed. Every request
and every close is kept, with who and when, in the lab check's own small
database (lab_check.db, beside combined.db) and its log: the record ARC has
no ticket system for.

Each request has a number, as a ticket would (1.44.0; nomad.tickets):
ARC-0012, on the PI's card, in both emails' subjects, on the Admin page and
in the log; ``find`` looks one up by it, or by a NetID, a name, a lab or a
PI. ``report`` says, for a period, how many were asked for and made, by
cluster, lab and month, and how long making them took; ``records`` is the
same as one row per account, for a spreadsheet (``csv_text``).

Settings, in the hub's nomad.toml::

    [console.labs.accounts]
    enabled = true                       # default: true once [directory] is set
    sites = ["spydur", "arachne"]        # where a PI may ask (default: each site with Slurm nodes read this week)
    notify = ["hpc@example.edu"]         # new requests go here
    most = 100                           # people in one request
    link = "https://nomad.example.edu/diagnostics?tab=labs"   # ARC's page, in the email

    [console.tickets]
    prefix = "ARC"                       # the requests' numbers: ARC-0012 (nomad.tickets)
    digits = 4

The PIs' emails go as the lab check's do ([console.labs.check] mail_domain,
send_from and review_to, else report.toml's): while ``review_to`` is set,
to it instead, saying whom each was for.
"""
from __future__ import annotations

import csv
import getpass
import io
import json
import math
import re
import sqlite3
import statistics
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from xml.etree import ElementTree

from nomad import tickets as T
from nomad.directory import NETID

STATES = ("asked", "made", "not_made", "withdrawn")
SAID = {"asked": "asked for", "made": "made", "not_made": "not made", "withdrawn": "withdrawn by the PI"}
MOST_LINES = 500
MOST_NAMES = 60           # name lookups in one check
NOTE_MAX = 600
SCHEMA = """
CREATE TABLE IF NOT EXISTS account_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lab TEXT NOT NULL, by TEXT NOT NULL, as_role TEXT,
    at TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS account_request_people (
    request INTEGER NOT NULL, netid TEXT NOT NULL, name TEXT, site TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'asked', closed_by TEXT, closed_at TEXT, closed_note TEXT,
    PRIMARY KEY (request, netid, site));
"""
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WORD = re.compile(r"^[^\W\d_][\w'.-]*$", re.UNICODE)


class RequestError(ValueError):
    """The request can't be made or closed as asked: what is wrong, in words."""


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    sites: tuple = ()
    notify: tuple = ()
    most: int = 100
    link: str = ""
    problems: tuple = field(default_factory=tuple)
    tickets: T.Settings = field(default_factory=T.Settings)


def settings(config: dict | None) -> Settings:
    from nomad import directory
    console = (config or {}).get("console") or {}
    labs = console.get("labs") if isinstance(console.get("labs"), dict) else {}
    raw = labs.get("accounts") if isinstance(labs.get("accounts"), dict) else {}
    problems = []
    d = directory.settings(config)
    enabled = raw.get("enabled", d.on)
    if not isinstance(enabled, bool):
        problems.append("[console.labs.accounts] enabled should be true or false")
        enabled = False
    if enabled and not d.on:
        problems.append("[console.labs.accounts] needs [directory] (where nomad looks people up): "
                        + ("; ".join(d.problems) or "not set"))
    sites = raw.get("sites", [])
    if not (isinstance(sites, list) and all(isinstance(s, str) and s for s in sites)):
        problems.append("[console.labs.accounts] sites should be a list of site names")
        sites = []
    notify = raw.get("notify", [])
    if isinstance(notify, str):
        notify = [notify]
    if not (isinstance(notify, list) and all(isinstance(a, str) and "@" in a for a in notify)):
        problems.append("[console.labs.accounts] notify should be a list of addresses")
        notify = []
    try:
        most = max(1, min(500, int(raw.get("most", 100))))
    except (TypeError, ValueError):
        most = 100
        problems.append("[console.labs.accounts] most should be a number")
    link = raw.get("link", "")
    link = link if isinstance(link, str) and link.startswith(("https://", "http://")) else ""
    numbers = T.settings(config)
    problems += list(numbers.problems)
    return Settings(enabled=bool(enabled) and d.on, sites=tuple(sites), notify=tuple(notify), most=most,
                    link=link, problems=tuple(problems), tickets=numbers)


# --- what the PI typed or gave ----------------------------------------------------------

def _netid_like(token: str) -> bool:
    return bool(NETID.match(token)) and token == token.lower()


def _name_like(token: str) -> bool:
    return bool(_WORD.match(token))


def _entry(text: str, netids=(), names=(), alone: bool = False, why: str | None = None) -> dict:
    return {"text": text, "netids": list(dict.fromkeys(netids)), "names": list(dict.fromkeys(names)),
            "alone": alone, **({"why": why} if why else {})}


def _of_email(found: str, domain: str | None) -> dict:
    local, _, host = found.partition("@")
    host = host.lower()
    if domain and not (host == domain or host.endswith("." + domain)):
        return _entry(found, why=f"an address outside {domain}: give their NetID")
    n = local.lower()
    return _entry(found, netids=[n] if NETID.match(n) else [])


def parse_text(text: str, domain: str | None = None) -> list[dict]:
    """Each thing asked about: {text, netids (to try as NetIDs), names
    ((first, last) to try, in turn), alone (one word)}. One per line, or
    separated by semicolons; NetIDs and addresses may share a line, with
    commas or spaces between; a name is "First Last" or "Last, First"; a
    row pasted from a table (tabs) is read as its NetID, else its name.
    ``domain``: the campus's mail domain: an address elsewhere isn't a NetID."""
    out = []
    for raw in str(text or "").replace("\r", "\n").split("\n")[:MOST_LINES]:
        for item in raw.split(";"):
            item = " ".join(item.replace("\t", ", ").split()).strip(" ,")
            if not item:
                continue
            emails = [m.group(0) for m in _EMAIL.finditer(item)]
            if emails:
                out += [_of_email(e, domain) for e in emails]
                continue
            if "," in item:
                parts = [p.strip() for p in item.split(",") if p.strip()]
                ids = [p for p in parts if _netid_like(p)]
                if len(ids) == len(parts) and len(parts) != 2:
                    out += [_entry(p, netids=[p]) for p in parts]
                    continue
                names = []
                if len(parts) == 2 and all(_name_like(w) for p in parts for w in p.split()):
                    last, first = parts[0], parts[1].split()[0]
                    names = [(first, last), (parts[0].split()[0], parts[1].split()[-1])]
                else:
                    many = [p for p in parts if len(p.split()) > 1 and all(_name_like(w) for w in p.split())]
                    caps = [p for p in parts if not _netid_like(p) and _name_like(p)]
                    if len(many) == 1:
                        w = many[0].split()
                        names = [(w[0], w[-1])]
                    elif len(caps) == 2:
                        names = [(caps[1], caps[0]), (caps[0], caps[1])]
                if not ids and not names:
                    out.append(_entry(item))
                else:
                    out.append(_entry(item, netids=ids, names=names))
                continue
            words = item.split()
            if len(words) == 1:
                w = words[0].lower()
                if NETID.match(w) or _name_like(words[0]):
                    out.append(_entry(words[0], netids=[w] if NETID.match(w) else [], alone=True))
                else:
                    out.append(_entry(words[0]))
                continue
            ids = [w for w in words if _netid_like(w)]
            caps = [w for w in words if not _netid_like(w)]
            pool = caps or words            # the words to read as a name
            names = []
            if 2 <= len(pool) <= 4 and all(_name_like(w) for w in pool) and (caps or len(words) <= 3):
                names = [(pool[0], pool[-1])] + ([(pool[-1], pool[0])] if len(pool) == 2 else [])
            out.append(_entry(item, netids=ids, names=names) if (ids or names) else _entry(item))
    return out


_HEADERS = {
    "netid": ("netid", "net id", "net_id", "username", "user name", "user", "uid", "login", "account"),
    "email": ("email", "e-mail", "email address", "mail", "e-mail address"),
    "first": ("first", "first name", "firstname", "given name", "given", "preferred name"),
    "last": ("last", "last name", "lastname", "surname", "family name", "family"),
    "name": ("name", "full name", "student", "student name", "person"),
}


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """The first worksheet of an Excel file, as text (no formulas worked out: their last values)."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        sheet = "xl/worksheets/sheet1.xml"
        try:
            wb = ElementTree.fromstring(z.read("xl/workbook.xml"))
            first = wb.find("m:sheets/m:sheet", ns)
            rid = first.get(f"{{{ns['r']}}}id") if first is not None else None
            rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            for rel in rels.findall("p:Relationship", ns):
                if rel.get("Id") == rid:
                    target = rel.get("Target").lstrip("/")
                    sheet = target if target.startswith("xl/") else "xl/" + target
        except (KeyError, ElementTree.ParseError, AttributeError):
            pass
        root = ElementTree.fromstring(z.read(sheet))
    rows = []
    for row in root.iter(f"{{{ns['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", ns):
            ref = c.get("r") or ""
            col = 0
            for ch in re.match(r"[A-Z]*", ref).group(0):
                col = col * 26 + ord(ch) - 64
            t = c.get("t")
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                text = shared[int(v.text)] if v.text and v.text.isdigit() and int(v.text) < len(shared) else ""
            elif t == "inlineStr":
                text = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
            else:
                text = v.text if v is not None and v.text else ""
            cells[max(col - 1, len(cells))] = text.strip()
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        if len(rows) > MOST_LINES:
            break
    return rows


def table_text(filename: str, data: bytes) -> str:
    """A table given as a file, as the lines parse_text reads: a header row
    naming the columns says which is which; without one, each row is a line."""
    name = (filename or "").lower()
    if len(data) > 2_000_000:
        raise RequestError("the file is larger than 2 MB")
    if name.endswith((".xlsx", ".xlsm")):
        try:
            rows = _xlsx_rows(data)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
            raise RequestError("the Excel file can't be read: save it as CSV and try that") from None
    elif name.endswith(".xls"):
        raise RequestError("an older Excel file (.xls): save it as .xlsx or CSV")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel_tab if "\t" in sample else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))[:MOST_LINES + 1]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    head = [c.lower().strip(" :*") for c in rows[0]]
    cols = {k: next((i for i, h in enumerate(head) if h in names), None) for k, names in _HEADERS.items()}
    if not any(v is not None for v in cols.values()):
        return "\n".join(", ".join(c for c in r if c) for r in rows)
    lines = []
    for r in rows[1:]:
        def cell(k, r=r):
            return (r[cols[k]] if cols[k] is not None and cols[k] < len(r) else "").strip()
        if cell("netid"):
            lines.append(cell("netid"))
        elif cell("email"):
            lines.append(cell("email"))
        elif cell("first") and cell("last"):
            lines.append(f"{cell('last')}, {cell('first')}")
        elif cell("name"):
            lines.append(cell("name"))
    return "\n".join(lines)


# --- who each one is --------------------------------------------------------------------

def has_account(conn, netids, sites=None) -> dict:
    """{netid: [sites]}: where nomad has a person's jobs, their account in a
    groups reading, or their use of a login node. Someone who never logged in
    nor ran a job isn't found here, though their account may exist."""
    from nomad.facts._common import columns, fetch, site_expr
    want = sorted({n for n in netids if n})
    out: dict = {n: set() for n in want}
    if conn is None or not want:
        return {n: [] for n in want}
    for table, user, extra in (("jobs", "user_name", ""), ("group_membership", "username", " AND has_account = 1"),
                               ("per_user_daily", "username", "")):
        cols = columns(conn, table)
        site = site_expr(cols)
        if user not in cols or site is None or (extra and "has_account" not in cols):
            continue
        for i in range(0, len(want), 400):
            part = want[i:i + 400]
            for r in fetch(conn, f"SELECT DISTINCT {user} AS u, {site} AS s FROM {table} "
                                 f"WHERE {user} IN ({','.join('?' * len(part))}){extra}", part):
                if r["u"] in out and r["s"] and (not sites or r["s"] in sites):
                    out[r["u"]].add(r["s"])
    return {n: sorted(s) for n, s in out.items()}


def sites_with_nodes(conn, days: int = 7, now: datetime | None = None) -> list:
    """The sites whose Slurm nodes nomad read in the last ``days``: where an account can run jobs."""
    from nomad.facts._common import columns, fetch, iso, site_expr
    cols = columns(conn, "node_state") if conn is not None else set()
    site = site_expr(cols)
    if site is None or "timestamp" not in cols:
        return []
    since = iso((now or datetime.now()) - timedelta(days=days))
    return sorted(r["s"] for r in fetch(conn, f"SELECT DISTINCT {site} AS s FROM node_state "
                                              f"WHERE timestamp >= ?", [since]) if r["s"])


def resolve(entries: list[dict], directory, conn=None, rec=None, sites=(), lab: str | None = None) -> list[dict]:
    """Each thing asked about, with who it is. ``status``:

    - found: one person (netid, name);
    - choose: several of that name (``choices``), the PI picks;
    - too_many: more than nomad.directory.MOST_MATCHES of that name: give the NetID;
    - not_found: no one in the directory by that NetID or name;
    - need_first: a surname alone (too many people share one): add the first name;
    - not_understood: neither a NetID, an address nor a name (``why``, when nomad can say).

    A NetID is the surer: tried first, and a name only when it isn't one.
    ``has``: the sites nomad sees each on already; ``asked``: the sites
    already asked for and not yet closed."""
    from nomad.directory import MOST_MATCHES
    tried = sorted({n for e in entries for n in e["netids"]})
    known = directory.by_netids(tried) if tried else {}
    rows, names = [], 0

    def by_id(e):
        return [{"text": n if len(e["netids"]) > 1 else e["text"], "status": "found", "netid": n, "name": known[n]}
                if known.get(n) else {"text": n if len(e["netids"]) > 1 else e["text"], "status": "not_found"}
                for n in e["netids"]]

    for e in entries:
        if e.get("why") and not e["netids"] and not e["names"]:
            rows.append({"text": e["text"], "status": "not_understood", "why": e["why"]})
            continue
        if e["netids"] and all(known.get(n) for n in e["netids"]):
            rows += by_id(e)
            continue
        if e["names"]:
            names += 1
            if names > MOST_NAMES:
                rows.append({"text": e["text"], "status": "not_understood",
                             "why": f"more than {MOST_NAMES} names at once: give these by NetID, or in parts"})
                continue
            got = None
            for first, last in e["names"]:
                found, more = directory.by_name(first, last)
                if more:
                    got = {"status": "too_many", "why": f"more than {MOST_MATCHES} people of that name: give their NetID"}
                elif len(found) == 1:
                    got = {"status": "found", **found[0]}
                elif found:
                    got = {"status": "choose", "choices": found}
                if got:
                    break
            if got:
                rows.append({"text": e["text"], **got})
            elif any(known.get(n) for n in e["netids"]):
                rows += by_id(e)
            else:
                rows.append({"text": e["text"], "status": "not_found"})
            continue
        if e["netids"]:
            if e.get("alone") and not known.get(e["netids"][0]):
                rows.append({"text": e["text"], "status": "not_found",
                             "why": "no NetID like that; if it is a surname, add the first name"})
            else:
                rows += by_id(e)
        elif e.get("alone"):
            rows.append({"text": e["text"], "status": "need_first", "why": "add the first name, or give the NetID"})
        else:
            rows.append({"text": e["text"], "status": "not_understood"})
    netids = sorted({r["netid"] for r in rows if r.get("netid")}
                    | {c["netid"] for r in rows for c in r.get("choices", [])})
    have = has_account(conn, netids, sites)
    open_ = asked(rec, netids)
    for r in rows:
        for x in [r] + r.get("choices", []):
            if x.get("netid"):
                x["has"] = have.get(x["netid"], [])
                x["asked"] = open_.get(x["netid"], [])
    return rows


# --- the requests -----------------------------------------------------------------------

def open_records(path, write: bool = False):
    """lab_check.db, with these tables too when written."""
    from nomad import lab_check
    rec = lab_check.open_records(path, write)
    if rec is not None and write:
        rec.executescript(SCHEMA)
        T.ensure(rec)
    return rec


def ref_of(number, s: Settings | None = None) -> str:
    """A request's number as shown: ARC-0012."""
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


def asked(rec, netids, lab: str | None = None) -> dict:
    """{netid: [sites]} asked for and not yet closed (by any lab: an account is one per person)."""
    want = sorted(set(netids))
    if not want:
        return {}
    marks = ",".join("?" * len(want))
    out: dict = {}
    for r in _rows(rec, f"SELECT netid, site FROM account_request_people WHERE state = 'asked' "
                        f"AND netid IN ({marks})", want):
        out.setdefault(r["netid"], []).append(r["site"])
    return {k: sorted(set(v)) for k, v in out.items()}


def clean_note(note) -> str | None:
    if not isinstance(note, str):
        return None
    note = " ".join(note.split())
    note = "".join(c for c in note if c.isprintable())
    return note[:NOTE_MAX] or None


def ask(rec, lab: str, people: list[dict], sites: list, s: Settings, by: str, as_role: str = "pi",
        note: str | None = None, allowed_sites=()) -> dict:
    """A PI's request: ``people`` [{netid, name}] (as the directory said),
    on ``sites``. Those already asked for on a site are left out of it."""
    sites = sorted({x for x in sites if isinstance(x, str)})
    allowed = set(allowed_sites or s.sites)
    if not sites:
        raise RequestError("pick the clusters to make the accounts on")
    bad = [x for x in sites if allowed and x not in allowed]
    if bad:
        raise RequestError(f"{', '.join(bad)}: not a cluster accounts are asked for here")
    seen, clean = set(), []
    for p in people:
        n = str(p.get("netid") or "").strip().lower()
        if not NETID.match(n):
            raise RequestError(f"{n!r} is not a NetID")
        if n not in seen:
            seen.add(n)
            clean.append({"netid": n, "name": " ".join(str(p.get("name") or "").split())[:120]})
    if not clean:
        raise RequestError("no one to ask for")
    if len(clean) > s.most:
        raise RequestError(f"{len(clean)} people: {s.most} at most in one request")
    already = asked(rec, [p["netid"] for p in clean])
    who = (by or getpass.getuser()).strip().lower()
    note = clean_note(note)
    with rec:
        rid = T.take(rec, "accounts")
        rec.execute("INSERT INTO account_requests (id, lab, by, as_role, at, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (rid, lab, who, as_role, _now(), note))
        rows, skipped = [], []
        for p in clean:
            for site in sites:
                if site in already.get(p["netid"], []):
                    skipped.append({"netid": p["netid"], "site": site})
                    continue
                rec.execute("INSERT INTO account_request_people (request, netid, name, site) VALUES (?, ?, ?, ?)",
                            (rid, p["netid"], p["name"], site))
                rows.append({**p, "site": site})
        if not rows:
            raise RequestError("each of them is already asked for on those clusters")
        _log(rec, who, {"account_request": {"id": rid, "ref": ref_of(rid, s), "lab": lab, "as": as_role,
                                            "sites": sites, "people": [p["netid"] for p in clean],
                                            "skipped": skipped}})
    return {"id": rid, "ref": ref_of(rid, s), "lab": lab, "by": who, "rows": rows, "skipped": skipped, "note": note}


def _with_people(rec, reqs: list[dict], s: Settings | None, open_only: bool = False) -> list[dict]:
    if not reqs:
        return []
    ids = [r["id"] for r in reqs]
    people: dict = {}
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        for p in _rows(rec, f"SELECT * FROM account_request_people WHERE request IN ({','.join('?' * len(part))}) "
                            "ORDER BY netid, site", part):
            people.setdefault(p["request"], []).append(p)
    out = []
    for r in reqs:
        ps = people.get(r["id"], [])
        if open_only and not any(p["state"] == "asked" for p in ps):
            continue
        out.append({**r, "ref": ref_of(r["id"], s), "people": ps, "open": sum(1 for p in ps if p["state"] == "asked")})
    return out


def requests(rec, lab: str | None = None, open_only: bool = False, days: int = 365,
             s: Settings | None = None) -> list[dict]:
    """The requests (a lab's, or all), newest first, each with its number
    (``ref``), its people and what became of each; closed ones of the last ``days``."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    reqs = _rows(rec, "SELECT * FROM account_requests WHERE at >= ?" + (" AND lab = ?" if lab else "")
                      + " ORDER BY id DESC", [since] + ([lab] if lab else []))
    return _with_people(rec, reqs, s, open_only)


FIND_MOST = 50


def find(rec, text: str, s: Settings | None = None, lab: str | None = None, most: int = FIND_MOST) -> list[dict]:
    """The requests, of any age, a search finds, newest first: by number
    (ARC-0012, #12 or 12: that one), else by a NetID, a name, a lab or the
    PI who asked (any part of one; case doesn't matter). ``lab``: within one lab's."""
    text = " ".join(str(text or "").split())[:80]
    if not text:
        return []
    n = T.number_of(text, (s or Settings()).tickets)
    one_lab = " AND r.lab = ?" if lab else ""
    if n is not None:
        reqs = _rows(rec, "SELECT * FROM account_requests r WHERE r.id = ?" + one_lab, [n] + ([lab] if lab else []))
        if reqs or not text.isdigit():          # ARC-0012 is that one or none; 12 may be part of a NetID
            return _with_people(rec, reqs, s)
    like = "%" + text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    ids = _rows(rec, "SELECT DISTINCT r.id AS id FROM account_requests r "
                     "LEFT JOIN account_request_people p ON p.request = r.id "
                     "WHERE (lower(r.lab) LIKE ? ESCAPE '\\' OR lower(r.by) LIKE ? ESCAPE '\\' "
                     "OR lower(p.netid) LIKE ? ESCAPE '\\' OR lower(coalesce(p.name, '')) LIKE ? ESCAPE '\\')"
                + one_lab + " ORDER BY r.id DESC LIMIT ?", [like] * 4 + ([lab] if lab else []) + [int(most)])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    reqs = _rows(rec, f"SELECT * FROM account_requests WHERE id IN ({marks}) ORDER BY id DESC", [r["id"] for r in ids])
    return _with_people(rec, reqs, s)


def close(rec, request: int, netid: str, site: str, state: str, by: str, note: str | None = None,
          s: Settings | None = None) -> dict:
    """ARC closes one account of a request: made, or not made (and why: a note
    is needed then); or the PI withdraws it while it is still asked for."""
    if state not in STATES[1:]:
        raise RequestError(f"{state!r}: one of {', '.join(STATES[1:])}")
    note = clean_note(note)
    if state == "not_made" and not note:
        raise RequestError("say why, in a note")
    row = _rows(rec, "SELECT * FROM account_request_people WHERE request = ? AND netid = ? AND site = ?",
                (int(request), str(netid).strip().lower(), site))
    if not row:
        raise RequestError("no such account in that request")
    if row[0]["state"] != "asked":
        raise RequestError(f"already closed: {SAID[row[0]['state']]}")
    who = (by or getpass.getuser()).strip().lower()
    with rec:
        rec.execute("UPDATE account_request_people SET state = ?, closed_by = ?, closed_at = ?, closed_note = ? "
                    "WHERE request = ? AND netid = ? AND site = ?",
                    (state, who, _now(), note, int(request), row[0]["netid"], site))
        _log(rec, who, {"account_closed": {"request": int(request), "ref": ref_of(request, s),
                                           "netid": row[0]["netid"], "site": site, "state": state, "note": note}})
    return {**row[0], "state": state, "closed_by": who, "closed_at": _now(), "closed_note": note}


def request_of(rec, request: int, s: Settings | None = None) -> dict | None:
    got = _rows(rec, "SELECT * FROM account_requests WHERE id = ?", (int(request),))
    return {**got[0], "ref": ref_of(got[0]["id"], s)} if got else None


# --- the report -------------------------------------------------------------------------

STATE_WORDS = {"asked": "still to make", "made": "made", "not_made": "not made", "withdrawn": "withdrawn"}


def _at(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _days(a, b) -> float | None:
    a, b = _at(a), _at(b)
    return round(max((b - a).total_seconds(), 0) / 86400, 1) if a and b else None


def _accounts(rec, since: datetime, until: datetime, site: str | None) -> list[dict]:
    """Each account asked for in [since, until), with its request's lab, PI and time."""
    return _rows(rec, "SELECT r.id AS request, r.lab, r.by, r.at, r.note AS request_note, p.netid, p.name, p.site, "
                      "p.state, p.closed_by, p.closed_at, p.closed_note FROM account_requests r "
                      "JOIN account_request_people p ON p.request = r.id WHERE r.at >= ? AND r.at < ?"
                 + (" AND p.site = ?" if site else "") + " ORDER BY r.id, p.netid, p.site",
                 [since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")] + ([site] if site else []))


def _tally(rows: list[dict]) -> dict:
    """Counts of accounts by what became of them, people, and the days from
    asked to made (median, 90 in 100 made within, longest)."""
    out = {"requests": len({r["request"] for r in rows}), "people": len({r["netid"] for r in rows}),
           "accounts": len(rows)}
    for state in STATES:
        out[state if state != "asked" else "open"] = sum(1 for r in rows if r["state"] == state)
    days = sorted(d for d in (_days(r["at"], r["closed_at"]) for r in rows if r["state"] == "made") if d is not None)
    out["days_to_make"] = ({"made": len(days), "median": round(statistics.median(days), 1),
                            "p90": days[max(math.ceil(0.9 * len(days)) - 1, 0)], "longest": days[-1]}
                           if days else None)
    return out


def report(rec, since: datetime, until: datetime, site: str | None = None, s: Settings | None = None,
           now: datetime | None = None) -> dict:
    """The account requests of a period (asked from ``since`` to before
    ``until``), and what became of each account as of now: in all, by
    cluster, by lab (with the PIs who asked) and by month; how long making
    them took (made accounts only: from asked to marked made); the slowest
    one; and, whenever asked, what is still to make now. ``site``: one cluster's."""
    now = now or datetime.now()
    rows = _accounts(rec, since, until, site)
    by_site, by_lab, by_month = {}, {}, {}
    for r in rows:
        by_site.setdefault(r["site"], []).append(r)
        by_lab.setdefault(r["lab"], []).append(r)
        by_month.setdefault(r["at"][:7], []).append(r)
    made = [(d, r) for r in rows if r["state"] == "made" for d in [_days(r["at"], r["closed_at"])] if d is not None]
    slowest = max(made, key=lambda x: (x[0], -x[1]["request"]), default=None)
    waiting = _rows(rec, "SELECT r.id AS request, r.at FROM account_requests r JOIN account_request_people p "
                         "ON p.request = r.id WHERE p.state = 'asked'" + (" AND p.site = ?" if site else ""),
                    [site] if site else [])
    oldest = min(waiting, key=lambda r: (r["at"], r["request"]), default=None)
    first = _rows(rec, "SELECT MIN(at) AS at FROM account_requests")
    return {
        "since": since.isoformat(timespec="seconds"), "until": until.isoformat(timespec="seconds"),
        "site": site, "as_of": now.isoformat(timespec="seconds"),
        "first_asked": first[0]["at"] if first else None,       # the records begin then (any cluster)
        "totals": {**_tally(rows), "labs": len(by_lab), "pis": len({r["by"] for r in rows})},
        "by_cluster": [{"site": k, **_tally(v)} for k, v in sorted(by_site.items())],
        "by_lab": [{"lab": k, "pis": sorted({r["by"] for r in v}), **_tally(v)} for k, v in sorted(by_lab.items())],
        "by_month": [{"month": k, **_tally(v)} for k, v in sorted(by_month.items())],
        "slowest": ({"ref": ref_of(slowest[1]["request"], s), "lab": slowest[1]["lab"], "site": slowest[1]["site"],
                     "days": slowest[0]} if slowest else None),
        "open_now": {"accounts": len(waiting), "requests": len({r["request"] for r in waiting}),
                     "oldest": ({"ref": ref_of(oldest["request"], s), "days": _days(oldest["at"], now)}
                                if oldest else None)},
    }


RECORD_COLUMNS = ("ref", "lab", "asked_by", "asked_at", "netid", "name", "cluster", "state", "closed_by",
                  "closed_at", "days", "closed_note", "request_note")


def records(rec, since: datetime, until: datetime, site: str | None = None, s: Settings | None = None) -> list[dict]:
    """The same period as one row per account (RECORD_COLUMNS), for a
    spreadsheet. It names people: for Research Computing's own use."""
    return [{"ref": ref_of(r["request"], s), "lab": r["lab"], "asked_by": r["by"], "asked_at": r["at"],
             "netid": r["netid"], "name": r["name"] or "", "cluster": r["site"], "state": STATE_WORDS[r["state"]],
             "closed_by": r["closed_by"] or "", "closed_at": r["closed_at"] or "",
             "days": "" if r["state"] == "asked" else _days(r["at"], r["closed_at"]),
             "closed_note": r["closed_note"] or "", "request_note": r["request_note"] or ""}
            for r in _accounts(rec, since, until, site)]


def _cell(value) -> str:
    """A spreadsheet cell: text a spreadsheet would run as a formula (=, +, -,
    @, a tab or a return first) is kept as text."""
    v = "" if value is None else str(value)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def csv_text(rows: list[dict], columns=RECORD_COLUMNS) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return out.getvalue()


# --- email ------------------------------------------------------------------------------

def _people(rows) -> list:
    return sorted({(r["netid"], r.get("name") or "") for r in rows})


def letter_to_arc(req: dict, s: Settings) -> tuple[str, str]:
    """(subject, text) of the email to ARC about a new request; its number
    first in the subject, as a ticket system's: "[ARC-0012] Account request: ..."."""
    sites = sorted({r["site"] for r in req["rows"]})
    people = _people(req["rows"])
    ref = ref_of(req["id"], s)
    subject = (f"[{ref}] Account request: {req['lab']}, {len(people)} "
               f"{'person' if len(people) == 1 else 'people'} on {', '.join(sites)}")
    lines = [f"{ref}: {req['by']} asked for accounts for the lab {req['lab']}, on {', '.join(sites)}:", ""]
    lines += [f"  {n:<12} {name}".rstrip() for n, name in people]
    if req.get("note"):
        lines += ["", f"Their note: {req['note']}"]
    if req.get("skipped"):
        lines += ["", "Already asked for, so not asked again: "
                  + ", ".join(f"{x['netid']} on {x['site']}" for x in req["skipped"])]
    lines += ["", "When each account is made (or not, and why), close it in the Console"
              + (f": {s.link}" if s.link else ", under Admin, Labs, Account requests.")]
    return subject, "\n".join(lines) + "\n"


def letter_to_pi(req: dict, closed: list, check_settings, s: Settings | None = None) -> tuple[str, str]:
    """(subject, text) of the email to the PI about the accounts of their
    request ARC just closed (one or several, at once); the request's number
    first in the subject, so a reply keeps it."""
    from nomad import lab_check
    made = [c for c in closed if c["state"] == "made"]
    not_made = [c for c in closed if c["state"] == "not_made"]
    ref = ref_of(req["id"], s)

    def who(c):
        return f"{c['name']} ({c['netid']})" if c.get("name") else c["netid"]
    if len(closed) == 1:
        c = closed[0]
        subject = (f"[{ref}] The account for {c['netid']} on {c['site']} is ready" if made else
                   f"[{ref}] The account for {c['netid']} on {c['site']} was not made")
    else:
        subject = f"[{ref}] Your account request: {len(made)} ready" + (f", {len(not_made)} not made"
                                                                         if not_made else "")
    body = []
    if made:
        body += [f"These accounts you asked for (request {ref}, {req['lab']}) are ready:"]
        body += [f"  {who(c)} on {c['site']}" for c in made]
    if not_made:
        body += ([""] if body else []) + [f"These accounts you asked for (request {ref}) were not made:"]
        body += [f"  {who(c)} on {c['site']}" + (f": {c['closed_note']}" if c.get("closed_note") else "")
                 for c in not_made]
    notes = sorted({c["closed_note"] for c in made if c.get("closed_note")})
    if notes:
        body += ["", "ARC's note: " + " ".join(notes)]
    text = [lab_check.greeting([req["by"]], check_settings), ""] + body
    contact = getattr(check_settings, "contact", None)
    text += ([""] + [contact] if contact else []) + ["", "Thank you,", ""]
    text += list(getattr(check_settings, "signature", ()) or ("Research Computing",))
    return subject, "\n".join(text) + "\n"


def pi_address(netid: str, check_settings) -> tuple[list, bool]:
    """(where the PI's email goes, reviewing): review_to while it is set,
    else NETID@mail_domain."""
    if check_settings.review_to:
        return list(check_settings.review_to), True
    if getattr(check_settings, "review_broken", False):
        return [], True
    return ([f"{netid}@{check_settings.mail_domain}"] if check_settings.mail_domain else []), False


def send(config, check_settings, subject: str, text: str, to: list, reply_to: list | None = None,
         review_for: str | None = None) -> str:
    """Send one email as the lab check does (nomad.mail, its send_from);
    what became of it, in words. ``review_for``: whom it was for, said at
    the top, while it goes to review_to instead."""
    from email.message import EmailMessage

    from nomad import mail
    try:
        cfg = mail.settings(config)
    except ValueError as exc:
        return f"not sent: {exc}"
    sender = check_settings.send_from or cfg.get("from")
    if not sender:
        return "not sent: no sender (send_from in [console.labs.check])"
    if not to:
        return "not sent: no address to send to (mail_domain in [console.labs.check])"
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
    except Exception as exc:          # noqa: BLE001 -- the record is kept; the email said not sent
        return "not sent: " + mail.failure_label(exc)
    return "sent"


def tell_arc(config, check_settings, s: Settings, req: dict) -> str:
    """Email ARC (``notify``) about a new request; the PI's address as Reply-To."""
    if not s.notify:
        return "not sent: no one to tell ([console.labs.accounts] notify)"
    subject, text = letter_to_arc(req, s)
    pi, _ = pi_address(req["by"], check_settings)
    reply = [f"{req['by']}@{check_settings.mail_domain}"] if check_settings.mail_domain else pi
    return send(config, check_settings, subject, text, list(s.notify), reply_to=reply)


def tell_pi(config, check_settings, s: Settings, req: dict, closed: list) -> str:
    """Email the PI about accounts of their request just closed; ARC's
    address (``notify``) as Reply-To. Withdrawn ones aren't said: the PI did that."""
    closed = [c for c in closed if c["state"] in ("made", "not_made")]
    if not closed:
        return "nothing to say"
    subject, text = letter_to_pi(req, closed, check_settings, s)
    to, reviewing = pi_address(req["by"], check_settings)
    return send(config, check_settings, subject, text, to, reply_to=list(s.notify) or None,
                review_for=(f"{req['by']}@{check_settings.mail_domain}" if check_settings.mail_domain
                            else req["by"]) if reviewing else None)


__all__ = ["RequestError", "Settings", "settings", "parse_text", "table_text", "resolve", "has_account",
           "sites_with_nodes", "open_records", "ask", "asked", "requests", "find", "close", "request_of", "ref_of",
           "report", "records", "csv_text", "letter_to_arc", "letter_to_pi", "send", "pi_address", "tell_arc",
           "tell_pi", "STATES", "SAID", "STATE_WORDS", "RECORD_COLUMNS"]
