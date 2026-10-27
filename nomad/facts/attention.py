# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What in a lab needs its PI's attention now: whom to help, and with what.

For one lab, from the jobs its members ran in the last ``days`` days, what
they have waiting now, the data they hold on the storage servers nomad reads,
and the lab's machines and storage. Each item says who, what and how much,
in words (``says``), with the jobs it is about, so the Console's My lab and
`nomad facts lab NETID --each` show the same thing. In the order a PI would
act on them:

  waiting       a member's jobs waiting longer than ``waiting_hours`` for
                something that won't change by itself: a job it depends on
                that can never run, a hold, a request that breaks a
                partition's or a per-job limit, an account or QOS that won't
                take it. Not limits that lift as the member's or others' jobs
                end, nor reasons that are the cluster's (its turn, free
                nodes, nodes down, drained or reserved, a partition down);
  out_of_memory a member's jobs that ran out of memory (Slurm killed them);
  timeout       a member's jobs that hit their time limit or their --deadline;
  failing       ``failing_jobs`` or more failed jobs, at least half of a
                member's jobs that ran to an end (node failures aside);
  quota         a member at ``quota_pct``% of a quota or more;
  storage       a lab storage server or export ``storage_pct``% full or more;
  data          the lab's ``holders`` biggest holders of data (``holder_gb``
                or more each), and anyone whose data grew by ``growth_gb`` or
                more in the last ``days`` days: how much, where, how much it
                changed and how much the scans found unchanged for a year;
  over_cpu      a member whose measured jobs held ``over_cpu_hours``
                core-hours or more and used under ``over_cpu_pct``% of them;
  over_memory   a member whose jobs used under ``over_memory_pct``% of the
                memory they asked for, over ``over_memory_jobs`` measured
                jobs or more, at sites where Slurm holds the memory a job
                asks for;
  workstation   a lab workstation that hasn't reported for
                ``workstation_hours`` hours.

The limits are nomad.toml's, [console.labs.attention] (nomad.config.attention).
Only with each member by name (the caller has decided a PI sees them: the
Console's PI and staff); the people given are whom the caller may see, less
the caller. Nothing measured, nothing said: an empty list is a lab where
none of these checks found anything, not one where all is known to be well.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from datetime import datetime, timedelta

from nomad.facts import jobs as jobfacts
from nomad.facts._common import fetch, columns, iso, num, parse_time, site_label

ORDER = ("waiting", "out_of_memory", "timeout", "failing", "quota", "storage", "data",
         "over_cpu", "over_memory", "workstation")
MAX_JOBS = 3                # the jobs listed with an item
MAX_PLACES = 3              # the places a member's data is named in, the rest summed
OLD_SHARE = 0.10            # unchanged data said when it is this share of theirs or more
FRESH_HOURS = 48            # a storage reading older than this: said with its date, no change from it
SCAN_DAYS = 8               # a scan older than this doesn't say what is unchanged now

# Slurm's waiting reasons that don't change by themselves (a prefix).
_STUCK = (
    ("DependencyNeverSatisfied", "the job it depends on ended in a way that never lets it start"),
    ("JobHeldAdmin", "an administrator is holding it"),
    ("JobHeldUser", "it is held (scontrol release lets it start)"),
    ("JobHoldMaxRequeue", "it was requeued too many times and is held"),
    ("BadConstraints", "no node meets what it asks for"),
    ("PartitionTimeLimit", "it asks for more time than its partition allows"),
    ("PartitionNodeLimit", "it asks for more nodes than its partition allows"),
    ("InvalidAccount", "its account isn't valid"),
    ("InvalidQOS", "its QOS isn't valid"),
    ("AccountNotAllowed", "its account may not use that partition"),
    ("QOSNotAllowed", "its QOS may not use that partition"),
    ("MaxMemPerLimit", "it asks for more memory than its partition allows a node or CPU"),
)
# Not listed: ReqNodeNotAvail (Slurm says it of a partition's drained or
# reserved nodes too, whatever the job asked for: the cluster's, and it ends).
# A per-job limit (QOSMaxWallDurationPerJobLimit, AssocMaxCpuPerJobLimit,
# QOSMaxGRESPerJob...): the job asks for more than one job may have, and
# waits for ever. A QOS minimum it doesn't meet, likewise. Per-user and group
# limits lift as jobs end, and *Minutes limits as use decays: not listed.
_PER_JOB = re.compile(r"^(QOS|Assoc)\w*PerJob")
_QOS_MIN = re.compile(r"^QOSMin")
# Held states, whatever the reason says.
_HELD = {"REQUEUE_HOLD": "it was requeued and is held (scontrol release lets it start)",
         "RESV_DEL_HOLD": "its reservation was deleted and it is held",
         "SPECIAL_EXIT": "it ended with a special exit code and is held"}
WAITING_STATES = ("PENDING",) + tuple(_HELD)


def why_stuck(reason, state=None) -> str | None:
    """Why a waiting job won't start by itself, in words; None when it will
    (or nothing says)."""
    if str(state or "").upper() in _HELD:
        return _HELD[str(state).upper()]
    r = str(reason or "").strip()
    if not r:
        return None
    for key, words in _STUCK:
        if r.startswith(key):
            return words
    head = r.split(",")[0].strip()
    if _PER_JOB.match(head):
        return f"it asks for more than one job may have ({head})"
    if _QOS_MIN.match(head):
        return f"it asks for less than its QOS requires ({head})"
    return None


def _span(hours: float) -> str:
    return f"{int(hours)} h" if hours < 48 else f"{int(hours // 24)} d"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _under(p: float, limit: float) -> str:
    """A share under ``limit``, said so that it reads under it (24.9%, not 25%)."""
    if p < 1:
        return "under 1%"
    if round(p) < limit:
        return f"{round(p)}%"
    return f"{math.floor(p * 10) / 10:.1f}%"


def _size(b) -> str:
    from nomad.storage import format_size
    return format_size(b)


def _ref(r: dict) -> dict:
    return {"job_id": r.get("job_id"), "site": r.get("site"), "submit_time": r.get("submit_time")}


def _newest(rows: list) -> list:
    """Newest first, by when they ended (times compared as times)."""
    return sorted(rows, key=lambda r: parse_time(r.get("end_time")) or datetime.min, reverse=True)


# -- What is shared across the labs of one answer --------------------------------------

def waiting_rows(conn: sqlite3.Connection) -> dict:
    """{user_name: [jobs waiting or held now]}, every site's, in one query
    (by state: few rows, whatever the size of the jobs table)."""
    jcols = columns(conn, "jobs")
    if not {"job_id", "state", "user_name"} <= jcols:
        return {}
    site = "source_site" if "source_site" in jcols else "cluster" if "cluster" in jcols else "NULL"
    sub = "submit_time" if "submit_time" in jcols else "NULL"
    reason = "pending_reason" if "pending_reason" in jcols else "NULL"
    out: dict = {}
    for r in fetch(conn, f"SELECT job_id, user_name, {site} AS site, {sub} AS submit_time, state, "
                         f"{reason} AS pending_reason FROM jobs "
                         f"WHERE state IN ({','.join('?' * len(WAITING_STATES))})", list(WAITING_STATES)):
        out.setdefault(r["user_name"], []).append(r)
    return out


_HOLDINGS: dict = {}         # (database, its state on disk, days) -> holdings, the latest only


def _on_disk(conn) -> tuple | None:
    """The database's file and its state (and its -wal's): what a kept
    answer is good for. None for one in memory."""
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    except (sqlite3.Error, TypeError, IndexError):
        return None
    if not path:
        return None
    state = []
    for p in (path, path + "-wal"):
        try:
            st = os.stat(p)
            state.append((st.st_ino, st.st_mtime_ns, st.st_size))
        except OSError:
            state.append(None)
    return path, tuple(state)


def holdings(conn: sqlite3.Connection, days: int) -> dict | None:
    """{owner (lower case): [their space on each path]} on every server the
    storage_use collector reads, copies (backups, replicas) aside: what the
    filesystem counts for them (else what the scans found), the change over
    ``days`` and the readings it is between, quotas, and the scans' figures
    for their top folders. None when the readings couldn't be read (a busy
    database). Kept until the database changes: reading every scan kept is
    the slow part, and a hub changes it once a sync."""
    key = _on_disk(conn)
    if key is not None and (key, days) in _HOLDINGS:
        return _HOLDINGS[(key, days)]
    from nomad.storage import views
    try:
        out: dict = {}
        if views.available(conn):
            for p in views.paths(conn):
                if p.get("copy_of"):
                    continue
                rows = views.owners(conn, p["server"], p["path"], days=days, jobs={})
                if not rows:
                    rows = [{"name": r["name"], "used_bytes": None, "change_bytes": None, "quotas": [],
                             "by_dataset": None, "scan": r, "read_at": None, "compared_with": None}
                            for r in views.scan_owners(conn, p["server"], p["path"], jobs={})]
                for r in rows:
                    name = str(r.get("name") or "").strip().lower()
                    if not name or name.startswith("uid ") or name.isdigit():
                        continue                    # no one this machine can name
                    out.setdefault(name, []).append({
                        "server": p["server"], "path": p["path"], "covers": p.get("covers"),
                        "used_bytes": r.get("used_bytes"), "by_dataset": r.get("by_dataset"),
                        "change_bytes": r.get("change_bytes"), "quotas": r.get("quotas") or [],
                        "scan": r.get("scan"), "read_at": r.get("read_at") or p.get("read_at"),
                        "compared_with": r.get("compared_with")})
    except sqlite3.Error:
        return None
    if key is not None:
        _HOLDINGS.clear()
        _HOLDINGS[(key, days)] = out
    return out


def _counted(rows: list, change_of) -> list:
    """Each place's share of one person's data, counted once (as
    nomad.storage.views.totals: a filesystem's counts once over the paths on
    it, a ZFS dataset once over the paths above it, the deeper path named),
    with its change (``change_of``) less that of the deeper places it holds,
    or None when that can't be told. Places that add nothing new are kept
    (their change counts) with bytes 0."""
    counted: dict = {}                  # what is counted -> the place that counted it
    out: list = []
    for r in sorted(rows, key=lambda r: (-len(r["path"]), r["server"], r["path"])):
        byds = r.get("by_dataset") or {}
        change = change_of(r)
        if r.get("used_bytes") is not None and byds and "" not in byds:
            keys = {(r["server"], "dataset", ds): b for ds, b in byds.items()}
            inner = {counted[k] for k in keys if k in counted}
            if inner and change is not None:
                # the deeper places inside this one: their change is theirs
                if all(set(out[i]["row"].get("by_dataset") or {}) <= set(byds) and out[i]["change"] is not None
                       for i in inner):
                    change -= sum(out[i]["change"] for i in inner)
                else:
                    change = None
            new = sum(b or 0 for k, b in keys.items() if k not in counted)
            for k in keys:
                counted.setdefault(k, len(out))
            out.append({"row": r, "bytes": new, "full": not inner, "change": change})
            continue
        used = r.get("used_bytes")
        if used is None:
            used = (r.get("scan") or {}).get("bytes")
        key = (r["server"], r.get("covers") or r["path"]) if r.get("used_bytes") is not None \
            else (r["server"], r["path"])
        if used is None or key in counted:
            continue
        counted[key] = len(out)
        out.append({"row": r, "bytes": used, "full": True, "change": change})
    return out


# -- One lab ---------------------------------------------------------------------------

def lab_attention(conn: sqlite3.Connection | None, lab: dict, people: set, ceilings: dict,
                  now: datetime, stale: set = frozenset(), *, settings: dict | None = None,
                  rows: list | None = None, shared: dict | None = None,
                  places: dict | None = None) -> list:
    """The items for one lab (see the module's notes), most pressing first.

    ``lab``: facts.labs' entry (its storage and workstations); ``people``:
    whom the caller sees in it, less the caller; ``stale``: sites that
    stopped reporting (their waiting jobs may be long gone); ``settings``:
    nomad.config.attention's; ``rows``: the members' finished jobs over at
    least the last ``days`` days (facts.jobs.finished_rows), else read here;
    ``shared``: a dict kept across the labs of one answer (what is waiting
    now, who holds what); ``places``: {server (lower case): (name, note)}
    for saying where data is, as nomad.toml's [console.storage] names them."""
    if conn is None:
        return []
    from nomad.config.attention import DEFAULTS
    cfg = {**DEFAULTS, **(settings or {})}
    shared = {} if shared is None else shared
    days = int(cfg["days"])
    since_t = now - timedelta(days=days)
    week = "in the last day" if days == 1 else f"in the last {days} days"
    people = {u for u in people if u}

    # The finished jobs of the window: the ones given, cut to it, or read.
    if rows is None:
        rows = jobfacts.finished_rows(conn, iso(since_t), iso(now + timedelta(days=1)),
                                      users=sorted(people)) or []
    rows = [r for r in rows if r.get("user_name") in people
            and (parse_time(r.get("end_time")) or datetime.min) >= since_t]
    if "waiting" not in shared:
        shared["waiting"] = waiting_rows(conn)
    waiting = [r for u in sorted(people) for r in shared["waiting"].get(u, ())
               if site_label(r.get("site")) not in stale]
    # A job number is a site's: said with its site where the lab's jobs are at several.
    several = len({site_label(r.get("site")) for r in rows + waiting}) > 1

    def jid(r) -> str:
        return f"{r.get('job_id')} at {site_label(r.get('site'))}" if several else str(r.get("job_id"))

    groups: dict = {k: [] for k in ORDER}

    # Waiting for something that won't change by itself: one item a member.
    stuck: dict = {}
    for r in waiting:
        why = why_stuck(r.get("pending_reason"), r.get("state"))
        since = parse_time(r.get("submit_time"))
        if why is None or since is None:
            continue
        hours = (now - since).total_seconds() / 3600
        if hours <= cfg["waiting_hours"] or hours > cfg["waiting_max_days"] * 24:
            continue
        stuck.setdefault(r["user_name"], []).append({**r, "hours": hours, "why": why})
    for user, jobs in stuck.items():
        jobs.sort(key=lambda r: -r["hours"])
        top, n = jobs[0], len(jobs)
        says = (f"{user}'s job {jid(top)} won't start by itself: it has waited {_span(top['hours'])} "
                f"because {top['why']}." if n == 1 else
                f"{user}: {n} jobs won't start by themselves; the longest, {jid(top)}, has waited "
                f"{_span(top['hours'])} because {top['why']}.")
        groups["waiting"].append((-top["hours"], {"kind": "waiting", "user": user, "count": n,
                                                  "jobs": [_ref(r) for r in jobs[:MAX_JOBS]], "says": says}))

    # Jobs that ended badly, per member: the same rows as the counts below.
    by_user: dict = {}
    for r in rows:
        by_user.setdefault(r["user_name"], []).append(r)
    def state(r) -> str:
        return str(r.get("state") or "").upper()
    for user, mine in sorted(by_user.items()):
        oom = _newest([r for r in mine if state(r) == "OUT_OF_MEMORY"])
        if oom:
            n = len(oom)
            groups["out_of_memory"].append((-n, {
                "kind": "out_of_memory", "user": user, "count": n, "jobs": [_ref(r) for r in oom[:MAX_JOBS]],
                "says": f"{user}: {n} job{_s(n)} ran out of memory {week} "
                        + (f"(the latest, {jid(oom[0])})." if n > 1 else f"({jid(oom[0])}).")}))
        late = _newest([r for r in mine if state(r) in ("TIMEOUT", "DEADLINE")])
        if late:
            t = sum(1 for r in late if state(r) == "TIMEOUT")
            d = len(late) - t
            what = " and ".join(x for x in (
                f"{t} job{_s(t)} hit {'its' if t == 1 else 'their'} time limit" if t else "",
                f"{d} job{_s(d)} reached {'its' if d == 1 else 'their'} --deadline" if d else "") if x)
            n = len(late)
            groups["timeout"].append((-n, {
                "kind": "timeout", "user": user, "count": n, "jobs": [_ref(r) for r in late[:MAX_JOBS]],
                "says": f"{user}: {what} {week} "
                        + (f"(the latest, {jid(late[0])})." if n > 1 else f"({jid(late[0])}).")}))
        st = jobfacts.summarize(mine, ceilings) or {}
        outcomes = st.get("outcomes") or {}
        judged = (st.get("ran_to_an_end") or 0) - outcomes.get("node_fail", 0)
        failed = outcomes.get("failed", 0)
        if failed >= cfg["failing_jobs"] and judged and failed * 2 >= judged:
            listed = _newest([r for r in mine if state(r) == "FAILED"])
            groups["failing"].append((-failed, {
                "kind": "failing", "user": user, "count": failed, "jobs": [_ref(r) for r in listed[:MAX_JOBS]],
                "says": (f"{user}: {failed} of the {judged} jobs that ran to an end failed {week} "
                         f"({outcomes.get('completed', 0)} completed)." if failed < judged else
                         f"{user}: the job that ran to an end {week} failed." if judged == 1 else
                         f"{user}: all {judged} jobs that ran to an end {week} failed.")}))
        cores = st.get("core_hours") or {}
        held, used = num(cores.get("measured_requested")), num(cores.get("used_pct"))
        if held is not None and used is not None and held >= cfg["over_cpu_hours"] \
                and used < cfg["over_cpu_pct"]:
            groups["over_cpu"].append((-held, {
                "kind": "over_cpu", "user": user, "count": cores.get("jobs_measured"), "jobs": [],
                "says": f"{user} held {round(held):,} core-hours {week}, in jobs nomad measured, "
                        f"and used {_under(used, cfg['over_cpu_pct'])} of them."}))
        mem = st.get("memory") or {}
        mused = num(mem.get("used_pct"))
        if mused is not None and mused < cfg["over_memory_pct"] \
                and (mem.get("jobs_measured") or 0) >= cfg["over_memory_jobs"] and mem.get("allocated") is True:
            groups["over_memory"].append((mused, {
                "kind": "over_memory", "user": user, "count": mem.get("jobs_measured"), "jobs": [],
                "says": f"{user}'s jobs used {_under(mused, cfg['over_memory_pct'])} of the memory they "
                        f"asked for {week} ({mem.get('jobs_measured')} jobs measured)."}))

    # The lab's storage servers and exports.
    for s in lab.get("storage") or ():
        p = num(s.get("used_pct"))
        if s.get("measured") and not s.get("stale") and p is not None and p >= cfg["storage_pct"]:
            name = s.get("name") or s.get("entry")
            where = f"{name}:{s['export']}" if s.get("export") else name
            groups["storage"].append((-p, {"kind": "storage", "user": None, "count": 1, "jobs": [],
                                           "says": f"{where} is {math.floor(p)}% full."}))

    # Who holds the lab's data, and where.
    _data(conn, lab, people, cfg, shared, places or {}, groups, week, now)

    # The lab's workstations: silent for a while, from when they last answered.
    for w in lab.get("workstations") or ():
        if w.get("online") is not False:
            continue
        last = parse_time(w.get("last_online"))
        if last is not None and (now - last).total_seconds() < cfg["workstation_hours"] * 3600:
            continue
        says = (f"Workstation {w['name']} hasn't reported since {str(w['last_online'])[:16].replace('T', ' ')}."
                if last is not None else
                f"Workstation {w['name']} hasn't reported (no reading of it online).")
        groups["workstation"].append((w["name"], {"kind": "workstation", "user": None, "count": 1,
                                                  "jobs": [], "says": says}))

    out = []
    for kind in ORDER:
        out += [item for _, item in sorted(groups[kind], key=lambda x: (x[0], str(x[1].get("user") or "")))]
    return out


def _data(conn, lab: dict, people: set, cfg: dict, shared: dict, places: dict, groups: dict,
          week: str, now: datetime) -> None:
    """The data and quota items: who holds the most of the lab's data, whose
    grew, who is near a quota; each place named, the lab's own said so, and
    a reading older than FRESH_HOURS said with its date (no change from it)."""
    days = int(cfg["days"])
    key = ("holdings", days)
    if key not in shared:
        shared[key] = holdings(conn, days)
    held = shared[key]
    if held is None:
        shared.setdefault("unchecked", set()).add("storage")
        return
    if not held:
        return
    own = []                        # (server or its name, export or None), lower case
    for s in lab.get("storage") or ():
        for n in {s.get("server"), s.get("name")} - {None, ""}:
            own.append((str(n).lower(), s.get("export")))
    fresh = timedelta(hours=FRESH_HOURS)

    def old_reading(r) -> str | None:
        t = parse_time(r.get("read_at"))
        return None if t is None or now - t <= fresh else str(r["read_at"])[:10]

    def owned(server: str, path: str | None) -> bool:
        """The lab's own: the whole server (path None: listed whole), or one
        of its exports holding the path."""
        return any(n == server.lower() and (e is None if path is None else
                                            (e is None or path == e or path.startswith(e.rstrip("/") + "/")))
                   for n, e in own)

    def server_label(server: str) -> str:
        """The server once, with what it is: "store1 (the cluster's storage)"."""
        name, note = places.get(server.lower(), (server, ""))
        extra = "; ".join(x for x in (note, "the lab's" if owned(server, None) else "") if x)
        return f"{name or server}" + (f" ({extra})" if extra else "")

    def where(r) -> str:
        """One place: server:path, what the server is, the lab's, the reading's date if old."""
        server, path = r["server"], r["path"]
        name, note = places.get(server.lower(), (server, ""))
        extra = "; ".join(x for x in (note, "the lab's" if owned(server, path) else "") if x)
        when = old_reading(r)
        return (f"{name or server}:{path}" + (f" ({extra})" if extra else "")
                + (f", as of {when}" if when else ""))

    def path_here(r, size) -> str:
        """A path under its server: "/scratch 11.4 TB" (and the lab's, an old reading's date)."""
        own_path = owned(r["server"], r["path"]) and not owned(r["server"], None)
        when = old_reading(r)
        return (f"{r['path']} {size}" + (" (the lab's)" if own_path else "")
                + (f" as of {when}" if when else ""))

    def places_said(parts: list) -> str:
        """Each server once, its paths under it, largest first; the rest summed."""
        top, rest = parts[:MAX_PLACES], parts[MAX_PLACES:]
        by_server: dict = {}
        for p in top:
            by_server.setdefault(p["row"]["server"], []).append(p)
        groups_ = sorted(by_server.items(), key=lambda kv: -sum(p["bytes"] for p in kv[1]))
        out = [f"on {server_label(server)} " + ", ".join(path_here(p["row"], _size(p["bytes"])) for p in ps)
               for server, ps in groups_]
        if rest:
            out.append(f"{_size(sum(p['bytes'] for p in rest))} on {len(rest)} other place{_s(len(rest))}")
        return out

    def change_of(r):
        """The change over ``days``, when it is between a fresh reading and
        one about ``days`` before it."""
        c, t, w = r.get("change_bytes"), parse_time(r.get("read_at")), parse_time(r.get("compared_with"))
        if c is None or t is None or w is None or now - t > fresh or t - w > timedelta(days=days) + fresh:
            return None
        return c

    ranked = []
    for user in sorted(people):
        rows = held.get(user.strip().lower())
        if not rows:
            continue
        parts = _counted(rows, change_of)
        total = sum(p["bytes"] for p in parts)
        if not total:
            continue
        known = [p["change"] for p in parts if p["change"] is not None]
        # What the scans found unchanged for the longer age, on places counted
        # whole, from a scan of the last SCAN_DAYS days, never more than
        # the place holds now.
        old, ages = 0, set()
        for p in parts:
            scan = p["row"].get("scan") or {}
            done = parse_time(scan.get("scanned_to") or scan.get("scanned_at"))
            age = (scan.get("ages") or (None, None))[1]
            if p["full"] and scan.get("older2_bytes") and age and done and now - done <= timedelta(days=SCAN_DAYS):
                old += min(scan["older2_bytes"], p["bytes"])
                ages.add(age)
        ranked.append({"user": user, "total": total, "parts": parts,
                       "change": sum(known) if known else None, "old": old, "ages": ages})
        # Near a quota: each dataset's (or path's) once, the deepest place naming it.
        seen = set()
        for r in sorted(rows, key=lambda r: (-len(r["path"]), r["server"], r["path"])):
            for q in r.get("quotas") or ():
                k = (r["server"], q.get("dataset") or r["path"])
                if k in seen:
                    continue
                seen.add(k)
                limits = [(x, w) for w, x in (("soft", q.get("soft_bytes")), ("hard", q.get("hard_bytes"))) if x]
                if not limits or q.get("used_bytes") is None:
                    continue
                limit, which = min(limits)
                pct = 100 * q["used_bytes"] / limit
                if pct >= cfg["quota_pct"]:
                    ds = f" (dataset {q['dataset']})" if q.get("dataset") else ""
                    groups["quota"].append((-pct, {
                        "kind": "quota", "user": user, "count": 1, "jobs": [],
                        "says": f"{user} is at {math.floor(pct)}% of a {which} quota on {where(r)}{ds}: "
                                f"{_size(q['used_bytes'])} of {_size(limit)}."}))
    ranked.sort(key=lambda x: (-x["total"], x["user"]))
    floor, growth = cfg["holder_gb"] * 1e9, cfg["growth_gb"] * 1e9
    named = [x for x in ranked[:int(cfg["holders"])] if x["total"] >= floor]
    named += [x for x in ranked if x not in named and growth and x["change"] is not None
              and x["change"] >= growth]
    for rank, x in enumerate(ranked):
        if x not in named:
            continue
        parts = sorted((p for p in x["parts"] if p["bytes"]),
                       key=lambda p: (-p["bytes"], p["row"]["server"], p["row"]["path"]))
        shown = places_said(parts)
        if len(parts) == 1:
            says = f"{x['user']} holds {_size(x['total'])} on {where(parts[0]['row'])}"
        elif len(shown) == 1:                 # one server: its paths after it
            says = (f"{x['user']} holds {_size(x['total'])} on {server_label(parts[0]['row']['server'])}: "
                    + ", ".join(path_here(p["row"], _size(p["bytes"])) for p in parts))
        else:
            says = f"{x['user']} holds {_size(x['total'])}: " + "; ".join(shown)
        if x["change"] is not None and growth and abs(x["change"]) >= growth:
            says += f"; {'up' if x['change'] > 0 else 'down'} {_size(abs(x['change']))} {week}"
        if x["old"] and x["old"] >= OLD_SHARE * x["total"]:
            age = min(x["ages"])
            says += (f"; {_size(x['old'])} of it unchanged for "
                     + ("a year" if age == 365 and len(x["ages"]) == 1 else
                        f"{age} days" if len(x["ages"]) == 1 else f"{age} days or more"))
        groups["data"].append((rank, {"kind": "data", "user": x["user"], "count": len(parts),
                                      "bytes": x["total"], "jobs": [], "says": says + "."}))
