"""The labs a person leads: who is in them, what their members ran, and the
lab's own machines and storage.

Lab-wide figures only: how many members there are and how many ran jobs,
and the members' jobs taken together. No row is about one member: how much a
PI sees of each student is a decision for the institution, and the Console's
own pages carry whatever it decides. Nor is a total one member's in
disguise: with ``min_others``, a total that rests on fewer than that many
members besides the one asking is left out, with how many ran jobs (a lab of
a PI and one student: the lab's total less the PI's own would be the
student's).

That protects against reading one member off one answer, not against
someone combining several: the difference between two windows, two labs, or
a site's total and a lab's can still come down to one person. The Console
narrows it (fixed windows for anyone but staff) but cannot close it; these
figures are not anonymised, and whoever decides what a PI may see should
know that. Labs, members and machines are nomad's
rules, the same as `nomad console roles` and the Console's My Lab:
[console.labs] and [console.storage] in nomad.toml, and the groups collector.

A member's jobs count wholly toward the lab, whatever they ran them for, and
someone in two labs counts in both: group membership is all nomad has to go
on (Slurm accounts, where a site keeps them per lab, would say more).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from nomad.facts import jobs as jobfacts
from nomad.facts._common import MACHINE_STALE_MINUTES, age_minutes, envelope, fetch, columns, window


def _workstation(conn, host: str, now: datetime) -> dict:
    cols = columns(conn, "workstation_state")
    if not {"hostname", "timestamp"} <= cols:
        return {"name": host, "online": None, "last_seen": None}
    status = "status" if "status" in cols else "NULL AS status"
    row = fetch(conn, f"SELECT timestamp, {status} FROM workstation_state WHERE hostname = ? "
                      "ORDER BY timestamp DESC LIMIT 1", [host])
    if not row:
        return {"name": host, "online": None, "last_seen": None}
    when, state = row[0]["timestamp"], row[0]["status"]
    age = age_minutes(when, now)
    online = (age is not None and age <= MACHINE_STALE_MINUTES
              and (state is None or state in ("online", "degraded")))
    return {"name": host, "online": online, "status": state, "last_seen": str(when)}


def _storage(conn, acc, entry: str, now: datetime, sizes: dict) -> dict:
    """A lab's storage entry: a whole server (the storage collector's
    reading) or one export (as the lab's machines mount it; ``sizes`` from
    export_sizes() over all the lab's exports, so exports sharing a pool's
    free space say so)."""
    from nomad.config.access import server_state
    host, _, export = entry.partition(":")
    named = acc.servers.get(host)
    out = {"entry": entry, "server": host, "name": (named[0] if named and named[0] else host)}
    if not export:
        s = server_state(conn, host)
        if s is None:
            return {**out, "measured": False}
        age = age_minutes(s.when, now)
        return {**out, "measured": True, "status": s.status, "as_of": s.when,
                "stale": age is None or age > MACHINE_STALE_MINUTES,
                "total_bytes": s.total, "used_bytes": s.used, "free_bytes": s.free,
                "used_pct": s.used_pct}
    size = sizes.get(entry)
    if size is None:
        return {**out, "export": export, "measured": False}
    age = age_minutes(size.when, now)
    return {**out, "export": export, "measured": True, "as_of": size.when,
            "stale": age is None or age > MACHINE_STALE_MINUTES,
            "total_bytes": size.total, "used_bytes": size.used, "free_bytes": size.avail,
            "used_pct": size.used_pct,
            # free space counted once across these: datasets of one pool
            "shares_free_with": list(size.shares_free_with)}


def labs(conn: sqlite3.Connection | None, config: dict | None, netid: str | None = None,
         days: int = 30, now: datetime | None = None, groups: list | None = None,
         min_others: int = 0) -> dict:
    """The labs ``netid`` leads (or, for staff, the ``groups`` named), each
    with its members, their jobs over ``days`` taken together, its
    workstations and its storage. ``min_others``: leave out a total resting
    on fewer members than this besides ``netid`` (see the top of the file)."""
    from nomad.config.access import (access_from, export_sizes, members_lookup, membership_counts,
                                     workstation_tags)
    from nomad.facts.person import now_counts
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    since, until, win = window(days, now)
    acc = access_from(config or {}, log=False)
    # No database: no group can be shown to exist, so only labs nomad.toml
    # lists count (as the Console's My Lab).
    exists = (lambda group: False)
    tagged = members_of = None
    if conn is not None:
        exists, members_of = members_lookup(conn)
        tagged = workstation_tags(conn, now)
    led = list(groups) if groups is not None else (
        acc.lab_groups(netid, exists, tagged) if netid else [])
    ceilings = memory_ceilings(conn) if conn is not None else {}
    out = envelope("lab", now, win)
    out["labs"] = []
    for g in led:
        lab = {"group": g, "name": acc.lab_names.get(g) or g}
        people = set(members_of(g)) if members_of is not None else set()
        if conn is not None:
            counts = membership_counts(conn, g)
            rows = jobfacts.finished_rows(conn, since, until, users=sorted(people))
            stats = jobfacts.summarize(rows, ceilings)
            ran = {r.get("user_name") for r in rows or () if r.get("user_name")}
            me = {(netid or "").strip().lower()}
            few = 0 < len(ran - me) < min_others
            lab["members"] = {"current": counts["members"],
                              # how many ran jobs is itself one member's news in a small lab
                              "ran_jobs": None if few else len(ran),
                              "no_longer_listed": counts["former"],
                              "listed_without_account": counts["no_account"]}
            if few:
                stats = {"finished": None, "suppressed": True,
                         "why": f"fewer than {min_others} members besides you ran jobs in the window"}
            lab["jobs"] = stats
            now_all = now_counts(conn, jobfacts.active(conn, users=sorted(people)), now)
            # Who is busy, at the sites the totals count (not ones gone quiet).
            busy = jobfacts.active_people(conn, sorted(people), exclude_sites=now_all["stale_sites"])
            if 0 < len(busy - me) < min_others:
                lab["now"] = {"running": None, "pending": None, "suppressed": True,
                              "why": f"fewer than {min_others} members besides you have jobs now"}
            else:
                lab["now"] = {"running": now_all["running"], "pending": now_all["pending"],
                              "stale_sites": now_all["stale_sites"]}
        listed = acc.resources.get(g, {})
        storage = sorted(listed.get("storage", ()))
        hosts = set(listed.get("workstations", ())) | (tagged(g) if tagged else set())
        hosts -= {s.partition(":")[0] for s in storage}
        if conn is not None:
            lab["workstations"] = [_workstation(conn, h, now) for h in sorted(hosts)]
            sizes = export_sizes(conn, [s for s in storage if ":" in s])
            lab["storage"] = [_storage(conn, acc, s, now, sizes) for s in storage]
        else:
            lab["workstations"] = [{"name": h, "online": None, "last_seen": None} for h in sorted(hosts)]
            lab["storage"] = [{"entry": s, "measured": False} for s in storage]
        out["labs"].append(lab)
    out["notes"] = [
        "Lab-wide figures only; nothing here is about one member.",
        "Running and waiting leave out sites that stopped reporting.",
        "A member's jobs count wholly toward the lab, and someone in two labs counts in both.",
        "CPU and memory use are over the jobs nomad measured.",
    ]
    return out
