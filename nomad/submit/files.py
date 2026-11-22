# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Moving files from the browser (1.55.0): the hub's side.

A person signed in to the Console puts files of theirs on a cluster (dropped
on the page, or chosen) and takes files of theirs back, as themselves,
through the cluster's nomad-gate: a file goes in pieces (PIECE_MB each, 8 MB
by default), each with its own sha256, the whole file's sha256 checked on
the cluster before it is put in place, never over a file there unless they
say Replace; it comes back in pieces too, each checked, and stops if the
file changes meanwhile. Nothing is kept on the hub: a piece passes through.
A break (the network, the laptop's lid) costs nothing: the cluster says how
much of it is there, and the rest is sent from there.

Up to ``max_mb`` (2 GB by default) a file; past that, and for many files or
whole folders, the page gives the rsync command to type on their computer
instead (and the scp one, for Windows), with the cluster's login host.

The cluster's say is the gate's: only where its hub key's line runs
``nomad-gate serve --edit`` (nomad gate install-key --edit) -- the same as
saving scripts from the Console -- and the hub's ``edit = true`` in that
cluster's [[hub.sites]]. Every name here is the site's; nothing about one
institution is written into nomad.

Settings, in the hub's nomad.toml::

    [console.files]
    open = "submit"          # who may: "submit" (whoever may Submit a job: [console] submit),
                             #   "staff", "everyone" signed in with the campus sign-in, "labs"
                             #   (staff and the people of the labs below), or "off"
    labs = ["kim$"]          # with open = "labs": these labs' people (1.60.0)
    active_days = 365        # with open = "labs": only those nomad saw at work in the last this
                             #   many days; 0: all of them
    max_mb = 2048            # the largest file moved from the browser (up to 1 TB)
    piece_mb = 8             # a piece (1 to 16)
    folders = ["~"]          # where My files opens, as shortcuts: ~ is their home, {user} their name
    only_in_folders = false  # true: only in those folders and below

    [console.files.sites.spydur]   # one cluster's own (optional)
    folders = ["~", "/scratch/{user}"]
    login = "spydur.example.edu"   # the host people use for rsync (default: its [[hub.sites]] host)

**Open to some labs (1.60.0).** With ``open = "labs"``, besides staff, My
files is open to the people of the labs in ``labs``: their members now (in
the lab's group on any site, as My lab counts them), and anyone their PI
said is in the lab on My lab's check (a condo's user, "in my lab"); not
those their PI said have left or aren't in the lab. With ``active_days``
(365 by default), only those nomad saw at work in that many days: a job on
a cluster, a session on a lab machine, use of a login node or of a notebook
server (``last_active``). Once the PIs have said who is in their labs and
the groups follow it, ``active_days = 0`` opens it to all of each lab's
people. ``nomad files status`` says how many it is open to, and
``nomad files open labs 'kim$' --apply`` sets it.

**Copying between machines (1.58.0).** A file or a whole folder of theirs
copied from one machine to another -- cluster to cluster, a lab workstation
to a cluster and back -- by the hub, as the person on both (nomad.submit.copies):
read in pieces from the one's gate, each checked, written through the
other's, the whole file's sha256 checked there before it is put in place.
It goes on on the hub when the browser is closed, and after a restart.

    copy = true              # false: no copying between machines
    copy_max_gb = 200        # the most one copy moves (more: the rsync command, from one machine to the other)
    copy_files_max = 20000   # the most files and folders in one copy
    copies_at_once = 2       # copies running at once on the hub (one a person)

A lab workstation (or any machine people's files are on that isn't one of
the hub's sites) is one of the machines with its gate alone
(install_nomad_for_users.sh with NOMAD_GATE_ONLY=1, gate_site.sh) and an
entry here::

    [[console.files.machines]]
    name = "ws-kim-01"             # as people know it
    host = "ws-kim-01.example.edu"
    gate_user = "nomad"            # the account its gate runs as (default: nomad)
    # gate_jump = "zeus@jumphost"  # reached only through that host
    folders = ["~", "/scratch/{user}", "/shared"]
    # login = "..."                # the host for rsync (default: host)

Who may use a machine from the Console (staff always may): the members of
its lab, as nomad's workstation collector knows it (the lab of
``workstation``, by default the host's first name); or, said here,
``groups = ["kim$"]`` (members of these groups), or ``groups = ["*"]``
(everyone My files is open to). A machine nomad knows no lab of, and no
groups said: staff only. The gate runs as the person, so they never see
more than they could logged in there; this keeps people who can't log in
there (a lab's machines are for its members) from seeing even that.
"""
from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass, field

OPENS = ("submit", "staff", "everyone", "labs", "off")
ACTIVE_DAYS = 365                                # open = "labs": seen at work in the last this many days
ACTIVE_DAYS_CAP = 3650
# Where nomad sees someone at work (1.60.0): (table, whose, when) -- a job on
# a cluster, a session on a lab machine, use of a login node, a notebook server.
ACTIVITY = (("jobs", "user_name", "submit_time"),
            ("workstation_user_snapshot", "username", "timestamp"),
            ("per_user_daily", "username", "day"),
            ("interactive_sessions", "user", "timestamp"))
MAX_MB = 2048
MAX_MB_CAP = 1024 * 1024                         # the gate's MAX_FILE
PIECE_MB = 8
PIECE_MB_CAP = 16                                # the gate's MAX_PIECE
DEFAULT_FOLDERS = ("~",)
COPY_MAX_GB = 200
COPY_FILES_MAX = 20000                           # the gate's MAX_TREE
COPIES_AT_ONCE = 2
GATE_USER = "nomad"
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
_PLAIN = re.compile(r"[A-Za-z0-9._/+@%=:,-]+")


class FilesError(Exception):
    """What can't be done, said for the person."""


@dataclass(frozen=True)
class Settings:
    open: str = "submit"
    max_bytes: int = MAX_MB * 1024 * 1024
    piece: int = PIECE_MB * 1024 * 1024
    folders: tuple = DEFAULT_FOLDERS
    only_in_folders: bool = False
    sites: dict = field(default_factory=dict)        # site -> {"folders": (...), "login": "host"}
    problems: tuple = ()
    copy: bool = True
    copy_max_bytes: int = COPY_MAX_GB * 1000 ** 3
    copy_files_max: int = COPY_FILES_MAX
    copies_at_once: int = COPIES_AT_ONCE
    machines: tuple = ()                             # ({name, host, gate_user, submit, edit, groups, ...}, ...)
    labs: tuple = ()                                 # open = "labs": whose people (1.60.0)
    active_days: int = ACTIVE_DAYS                   # open = "labs": seen at work this lately; 0: any time

    @property
    def max_mb(self) -> int:
        return self.max_bytes // (1024 * 1024)


def _folders(raw, where: str, problems: list) -> tuple | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} folders: a list of folders, e.g. [\"~\", \"/scratch/{{user}}\"]")
        return None
    out = []
    for f in raw:
        if not isinstance(f, str) or not (f == "~" or f.startswith("~/") or f.startswith("/")):
            problems.append(f"{where} folders: {str(f)[:60]!r} isn't ~, ~/... or a full path")
            continue
        if any(p in (".", "..") or p.startswith(".") for p in f.split("/")[1:] if p):
            problems.append(f"{where} folders: {f[:60]!r} has a hidden part, . or ..")
            continue
        out.append(f.rstrip("/") or "/")
    return tuple(dict.fromkeys(out)) or None


def settings(config: dict | None) -> Settings:
    """[console.files] from the hub's nomad.toml (see above); what is wrong
    in it is said in ``problems`` and the default used."""
    console = (config or {}).get("console") if isinstance(config, dict) else None
    raw = console.get("files") if isinstance(console, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    problems: list = []
    open_ = str(raw.get("open", "submit")).strip().lower()
    if open_ not in OPENS:
        problems.append(f"open = {open_[:20]!r}: one of {', '.join(OPENS)} (submit used)")
        open_ = "submit"
    max_mb = raw.get("max_mb", MAX_MB)
    if isinstance(max_mb, bool) or not isinstance(max_mb, int) or not 1 <= max_mb <= MAX_MB_CAP:
        problems.append(f"max_mb: a whole number of MB, 1 to {MAX_MB_CAP} ({MAX_MB} used)")
        max_mb = MAX_MB
    piece_mb = raw.get("piece_mb", PIECE_MB)
    if isinstance(piece_mb, bool) or not isinstance(piece_mb, int) or not 1 <= piece_mb <= PIECE_MB_CAP:
        problems.append(f"piece_mb: 1 to {PIECE_MB_CAP} ({PIECE_MB} used)")
        piece_mb = PIECE_MB
    folders = _folders(raw.get("folders"), "[console.files]", problems) or DEFAULT_FOLDERS
    only = raw.get("only_in_folders", False)
    if not isinstance(only, bool):
        problems.append("only_in_folders: true or false (false used)")
        only = False
    sites = {}
    per = raw.get("sites") if isinstance(raw.get("sites"), dict) else {}
    for name, v in per.items():
        if not _SITE.fullmatch(str(name)) or not isinstance(v, dict):
            problems.append(f"[console.files.sites.{str(name)[:30]}]: a cluster's name, with its folders and login")
            continue
        entry = {}
        f = _folders(v.get("folders"), f"[console.files.sites.{name}]", problems)
        if f:
            entry["folders"] = f
        login = v.get("login")
        if login is not None:
            if isinstance(login, str) and _HOST.fullmatch(login.strip()):
                entry["login"] = login.strip()
            else:
                problems.append(f"[console.files.sites.{name}] login: a host name")
        sites[name] = entry
    copy = raw.get("copy", True)
    if not isinstance(copy, bool):
        problems.append("copy: true or false (true used)")
        copy = True
    gb = _whole(raw, "copy_max_gb", COPY_MAX_GB, 1, 100000, problems)
    most = _whole(raw, "copy_files_max", COPY_FILES_MAX, 1, COPY_FILES_MAX, problems)
    at_once = _whole(raw, "copies_at_once", COPIES_AT_ONCE, 1, 8, problems)
    machines = _machines(raw.get("machines"), problems, sites)
    labs = raw.get("labs", [])
    labs = [labs] if isinstance(labs, str) else labs
    if not isinstance(labs, list) or not all(isinstance(g, str) and _LAB.fullmatch(g.strip()) for g in labs):
        problems.append('labs: a list of labs\' groups, e.g. ["kim$"]')
        labs = []
    labs = tuple(dict.fromkeys(g.strip() for g in labs))
    active = _whole(raw, "active_days", ACTIVE_DAYS, 0, ACTIVE_DAYS_CAP, problems)
    if open_ == "labs" and not labs:
        problems.append('open = "labs" needs labs = ["..."], the labs whose people may: staff only until then')
    return Settings(open=open_, max_bytes=max_mb * 1024 * 1024, piece=piece_mb * 1024 * 1024, folders=folders,
                    only_in_folders=only, sites=sites, problems=tuple(problems), copy=copy,
                    copy_max_bytes=gb * 1000 ** 3, copy_files_max=most, copies_at_once=at_once,
                    machines=tuple(machines), labs=labs, active_days=active)


def _whole(raw: dict, key: str, default: int, low: int, high: int, problems: list) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        problems.append(f"{key}: a whole number, {low} to {high} ({default} used)")
        return default
    return v


_GROUP = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.$@ -]{0,63}")
_LAB = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.$@-]{0,63}")
_DAY = re.compile(r"\d{4}-\d\d-\d\d")


def _machines(raw, problems: list, sites: dict) -> list:
    """[[console.files.machines]] as gate sites ({name, host, gate_user,
    submit = "gate", edit = true, ...}) the hub copies to and from (1.58.0);
    their folders and login go in ``sites``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append("[[console.files.machines]]: entries ([[...]]), each a machine")
        return []
    out, seen = [], set()
    for i, m in enumerate(raw, 1):
        where = f"[[console.files.machines]] #{i}"
        if not isinstance(m, dict):
            problems.append(f"{where}: a table (name = ..., host = ...)")
            continue
        name, host = m.get("name"), m.get("host", m.get("name"))
        if not (isinstance(name, str) and _SITE.fullmatch(name)):
            problems.append(f"{where}: name, as people know the machine (letters, digits, . _ -)")
            continue
        if name in seen:
            problems.append(f"{where}: {name} is there twice (the first is used)")
            continue
        if not (isinstance(host, str) and _HOST.fullmatch(host)):
            problems.append(f"{where} ({name}): host should be a host name")
            continue
        user = m.get("gate_user", GATE_USER)
        if not (isinstance(user, str) and _USER.fullmatch(user)):
            problems.append(f"{where} ({name}): gate_user should be an account name")
            continue
        groups = m.get("groups")
        groups = [groups] if isinstance(groups, str) else groups
        if groups is not None and not (isinstance(groups, list) and groups and all(
                isinstance(g, str) and (g == "*" or _GROUP.fullmatch(g)) for g in groups)):
            problems.append(f"{where} ({name}): groups should be a list of group names, or [\"*\"] for everyone")
            continue
        ws = m.get("workstation", host.split(".")[0])
        if not (isinstance(ws, str) and _SITE.fullmatch(ws)):
            problems.append(f"{where} ({name}): workstation should be a machine's name, as nomad collects it")
            continue
        entry = {"name": name, "host": host, "gate_user": user, "submit": "gate", "edit": True,
                 "machine": True, "groups": None if groups is None else tuple(groups), "workstation": ws}
        if m.get("gate_jump") not in (None, ""):
            entry["gate_jump"] = str(m["gate_jump"])
        if m.get("port") is not None:
            entry["port"] = m["port"]
        unknown = sorted(set(m) - {"name", "host", "gate_user", "gate_jump", "port", "groups", "workstation",
                                   "folders", "login"})
        if unknown:
            problems.append(f"{where} ({name}): nomad doesn't read {', '.join(unknown)}")
        per = {}
        f = _folders(m.get("folders"), f"{where} ({name})", problems)
        if f:
            per["folders"] = f
        login = m.get("login")
        if login is not None:
            if isinstance(login, str) and _HOST.fullmatch(login.strip()):
                per["login"] = login.strip()
            else:
                problems.append(f"{where} ({name}) login: a host name")
        if per:
            sites.setdefault(name, {}).update(per)
        seen.add(name)
        out.append(entry)
    return out


def machine_labs(db, machines) -> dict:
    """{workstation: lab} for these machines, from the hub's combined
    database (``db``: an open sqlite3 connection): the lab nomad's
    workstation collector last saw each in. None when it can't say."""
    names = sorted({m["workstation"] for m in machines if m.get("groups") is None})
    if not names:
        return {}
    low = {n.lower(): n for n in names}                # a host name in any case is the same machine
    try:
        rows = db.execute(
            "SELECT hostname, department FROM workstation_state w WHERE lower(hostname) IN "
            f"({','.join('?' * len(low))}) AND timestamp = (SELECT MAX(timestamp) FROM workstation_state "
            "WHERE hostname = w.hostname) ORDER BY timestamp", list(low)).fetchall()
    except Exception:                                  # noqa: BLE001 -- no workstations collected here
        return {}
    return {low[h.lower()]: d for h, d in rows if d and h.lower() in low}


def machines_open_to(s: Settings, groups, staff: bool, labs: dict | None = None) -> list:
    """The machines (with their gate alone) this person may use (see the
    module's notes): every one for staff; else those of a lab of theirs
    (``labs``: machine_labs), those whose groups take one of theirs, and
    those open to all (groups = ["*"])."""
    mine, labs = set(groups or ()), labs or {}
    out = []
    for m in s.machines:
        if staff:
            out.append(m)
        elif m["groups"] is None:
            lab = labs.get(m["workstation"])
            if lab and lab in mine:
                out.append(m)
        elif "*" in m["groups"] or mine & set(m["groups"]):
            out.append(m)
    return out


def opens_to(s: Settings, role: str | None, submit_open: str, in_labs: bool = False) -> bool:
    """Whether My files is open to someone of this role (the Console checks
    the rest: a campus sign-in, not viewing as someone). ``in_labs``: with
    open = "labs", whether they are one of lab_people's (1.60.0)."""
    staff = role in ("admin", "operator")
    if s.open == "off":
        return False
    if s.open == "staff":
        return staff
    if s.open == "everyone":
        return True
    if s.open == "labs":
        return staff or (bool(in_labs) and bool(s.labs))
    return staff or submit_open == "everyone"             # "submit": as Submit a job


def last_active(db, names, since: str | None = None) -> dict:
    """netid -> the day (YYYY-MM-DD) nomad last saw them at work on a
    machine it watches (ACTIVITY), from the hub's combined database (``db``,
    an open sqlite3 connection); not listed: never on record (with
    ``since``, a day: not since then -- only the records since are read)."""
    import sqlite3
    names = sorted({n for n in names if n})
    out: dict = {}
    if db is None or not names:
        return out
    for table, who, when in ACTIVITY:
        try:
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not {who, when} <= cols:
            continue
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            recent = f" AND {when} >= ?" if since else ""
            try:
                rows = db.execute(f"SELECT {who}, MAX({when}) FROM {table} WHERE {who} IN "
                                  f"({','.join('?' * len(chunk))}){recent} GROUP BY {who}",
                                  chunk + ([since] if since else [])).fetchall()
            except sqlite3.Error:
                continue
            for user, t in rows:
                day = str(t or "")[:10]
                if user and _DAY.fullmatch(day) and day > out.get(user, ""):
                    out[user] = day
    return out


def lab_people(db, s: Settings, rec=None, today=None) -> dict:
    """Who My files is open to besides staff with open = "labs" (1.60.0;
    the module's notes): ``db`` the hub's combined database, ``rec`` the lab
    check's answers (nomad.lab_check.open_records; None: no answers).

    {"open": {netid, ...}, "since": "YYYY-MM-DD" or None (active_days 0),
     "last": {netid: day last seen at work},
     "labs": {lab: {"people", "members", "claimed", "left", "quiet", "open"}}}
    -- each lab's people (members now and those their PI said are in it,
    less those their PI said have left or aren't in it), and of them those
    not seen at work since ``since`` (quiet) and the rest (open)."""
    from datetime import date, timedelta

    from nomad import lab_check as L
    today = today or date.today()
    since = (today - timedelta(days=s.active_days)).isoformat() if s.active_days else None
    groups = L.by_site(db) if db is not None else {}
    said: dict = {}
    if rec is not None:
        try:
            for (lab, netid), row in L._latest(rec).items():
                said.setdefault(lab, {})[netid] = row.get("answer")
        except Exception:                                      # noqa: BLE001 -- no answers kept yet
            said = {}
    labs: dict = {}
    for lab in s.labs:
        members = L._members(groups.get(lab, {}))
        answers = said.get(lab, {})
        claimed = {n for n, a in answers.items() if a == "mine"} - members
        left = {n for n in members | claimed if answers.get(n) in ("left", "not_mine")}
        labs[lab] = {"people": (members | claimed) - left, "members": members, "claimed": claimed, "left": left}
    everyone = set().union(*(x["people"] for x in labs.values())) if labs else set()
    last = last_active(db, everyone, since) if since else {}
    opened: set = set()
    for x in labs.values():
        x["open"] = {n for n in x["people"] if since is None or last.get(n, "") >= since}
        x["quiet"] = x["people"] - x["open"]
        opened |= x["open"]
    return {"open": opened, "since": since, "last": last, "labs": labs}


def lab_people_here(s: Settings, config: dict | None, config_target=None, db_path=None, today=None) -> dict:
    """lab_people from the hub's own databases: combined.db (``db_path``,
    else the hub's) and the lab check's answers beside it (or its ``db``)."""
    from nomad import lab_check as L
    from nomad.facts import open_readonly
    db = rec = None
    try:
        if db_path is None:
            from nomad.actions.remote import hub_settings
            db_path = hub_settings(config_target)["combined_db"]
        try:
            db = open_readonly(db_path)
        except FileNotFoundError:
            db = None
        try:
            rec = L.open_records(L.db_path(L.settings(config or {}), config_target))
        except Exception:                                      # noqa: BLE001 -- no answers: none said
            rec = None
        return lab_people(db, s, rec, today)
    finally:
        for c in (db, rec):
            if c is not None:
                c.close()


def folders_for(s: Settings, site: str, user: str, home: str) -> list:
    """The folders My files offers for this person on ``site`` (full paths)."""
    if not _USER.fullmatch(user or "") or not home.startswith("/"):
        return []
    out = []
    for f in (s.sites.get(site) or {}).get("folders") or s.folders:
        f = f.replace("{user}", user)
        f = home if f == "~" else posixpath.join(home, f[2:]) if f.startswith("~/") else f
        out.append(posixpath.normpath(f))
    return list(dict.fromkeys(out))


def check_path(s: Settings, site: str, user: str, home: str, path: str, *, folder: bool = False) -> str:
    """``path`` as the gate takes it (a full path, no ., .. or hidden part;
    within the folders when only_in_folders), else FilesError."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise FilesError("a full path, please")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts) or "//" in path:
        raise FilesError("give the path without . or .. in it")
    if any(p.startswith(".") for p in parts):
        raise FilesError("not a hidden file or folder")
    norm = "/" + "/".join(parts)
    if not folder and norm == "/":
        raise FilesError("a file's full path, please")
    if s.only_in_folders:
        mine = folders_for(s, site, user, home)
        if not any(norm == f or norm.startswith(f.rstrip("/") + "/") for f in mine):
            raise FilesError(f"only in {', '.join(mine) or 'your folders'} here")
    return norm


def login_of(s: Settings, site: dict) -> str:
    return (s.sites.get(site.get("name")) or {}).get("login") or str(site.get("host") or site.get("name") or "")


def _remote_spec(user: str, host: str, path: str) -> str:
    spec = f"{user}@{host}:{path}"
    return spec if _PLAIN.fullmatch(spec) else shlex.quote(spec)


def _local(name: str) -> str:
    name = "./" + name if name.startswith("-") else name          # never taken for an option
    return name if _PLAIN.fullmatch(name) else shlex.quote(name)


def commands(s: Settings, site: dict, user: str, folder: str, name: str | None = None) -> dict:
    """What to type on their own computer for files too big for the browser,
    or many, or whole folders: rsync (goes on where it stopped) and scp
    (Windows has it), to put a file (or a folder) in ``folder``, and to take
    ``name`` from it back."""
    host = login_of(s, site)
    folder = folder.rstrip("/") or "/"
    here = _local(name or "FILE")
    put_to = _remote_spec(user, host, folder + "/")
    out = {"host": host,
           "put_rsync": f"rsync -av --partial --progress {here} {put_to}",
           "put_folder_rsync": f"rsync -av --partial --progress {_local('FOLDER')} {put_to}",
           "put_scp": f"scp {here} {put_to}"}
    if name:
        took = _remote_spec(user, host, posixpath.join(folder, name))
        out.update(get_rsync=f"rsync -av --partial --progress {took} .", get_scp=f"scp {took} .")
    return out


# ── taking a file back, in pieces ───────────────────────────────────────────

class Changed(FilesError):
    """The file changed while it was being taken."""


def facts(call, path: str) -> dict:
    """What the file is (its size, version), from the gate: ``call(op,
    **options)`` asks it as the person. FilesError when it can't be taken."""
    doc = call("get", path=path, length=0)
    if not isinstance(doc, dict) or not doc.get("ok"):
        raise FilesError(str((doc or {}).get("error") or "the cluster didn't say"))
    return doc


def pieces(call, path: str, size: int, version: str, piece: int):
    """The file's bytes, piece by piece, each checked (its sha256), the file
    the same one throughout (its version); FilesError (Changed) when not."""
    at = 0
    while at < size:
        doc = call("get", path=path, offset=at, length=min(piece, size - at))
        if not isinstance(doc, dict) or not doc.get("ok"):
            raise FilesError(str((doc or {}).get("error") or "the cluster didn't say"))
        if doc.get("version") != version:
            raise Changed(f"{path} changed while it was being taken: take it again")
        data = base64.b64decode(doc.get("data") or "", validate=True)
        if hashlib.sha256(data).hexdigest() != doc.get("sha256") or len(data) != doc.get("length"):
            raise FilesError("a piece didn't arrive whole: take it again")
        if not data:
            raise Changed(f"{path} is shorter than it was: take it again")
        yield data
        at += len(data)


def upload_id(user: str, site: str, path: str, size: int, modified: int) -> str:
    """The id a file being sent goes by (as the page makes it): the same
    file sent again to the same place goes on where it stopped."""
    return hashlib.sha256(f"{user}|{site}|{path}|{size}|{modified}".encode()).hexdigest()[:24]
