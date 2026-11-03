# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Where a job would start soonest (1.25.4): the partitions a person may
send it to, in the order the site prefers, each with Slurm's own estimate
of when it would start (``nomad-gate starts``: sbatch --test-only, nothing
submitted) and the usual wait there.

The site's partitions as the hub knows them (nomad.partitions, node_state)
and its condos as the condo reports' report.toml names them sort into:

- condo: every node is a condo's node -- the person's lab's own (its
  members first of all), or another lab's, where they are a guest
  (``guests``; only a job with a time limit of at most ``guest_max_days``,
  the condo policy's, else report.toml's [report.policy] guest_max_days);
- small: the shared nodes with the least memory (the least a shared node has),
  kind), where any job may go;
- big: shared nodes with more memory than those -- few, kept for the jobs
  that need them: a job that doesn't need them goes there only with a time
  limit of at most ``overflow_max_hours`` (it gives the node back at a known
  time), and only while more than ``reserve_nodes`` of its nodes are idle;
- gpu: nodes with GPUs, for jobs that ask for them;
- mixed: nodes of several kinds (a partition of every node), or a node
  nomad has no memory reading for: not suggested (a job that names one
  keeps it).

It fails closed: a node of a condo report.toml lists, confirmed or not, is
a condo's node (the person's own only when confirmed); a site report.toml
lists no condos table for (``[report.clusters.SITE.condos]``, even an
empty one for a site with none) gets only its default partition suggested,
as nomad can't tell its condos from its shared nodes. The default partition
is always among the ones offered: it is where the job goes if none is named.

Partitions with the very same nodes are one (an alias, not suggested again);
a shared partition inside another of its kind (one node of several) is
left to the bigger one. Nothing here changes Slurm: it says where, and why.

    [submit.where]                 # in the hub's nomad.toml; [submit.where.SITE] for one site
    guests = true                  # offer other labs' condos to guests
    # guest_max_days = 10          # default: report.toml's [report.policy] guest_max_days
    overflow_max_hours = 24        # a job that fits the small nodes, on big ones: at most this long
    reserve_nodes = 1              # big nodes left idle for the jobs that need them
    fastest_guests = false         # "Fastest start" may pick a guest condo by itself
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SOON_MINUTES = 2
WAIT_DAYS = 30
# Slurm plans a node free only when its jobs' time limits end: where they
# have none it puts the start a year or so away. Past this it is "later".
LATER_DAYS = 7


@dataclass
class Settings:
    guests: bool = True
    guest_max_days: float = 10.0
    overflow_max_hours: float = 24.0
    reserve_nodes: int = 1
    fastest_guests: bool = False


def settings(config: dict | None, site: str | None = None, policy_guest_days: float | None = None) -> Settings:
    """[submit.where] (and [submit.where.SITE] over it) from the hub's
    nomad.toml; a value of the wrong kind keeps the default."""
    s = Settings(guest_max_days=float(policy_guest_days) if policy_guest_days else 10.0)
    sub = (config or {}).get("submit") if isinstance(config, dict) else None
    base = sub.get("where") if isinstance(sub, dict) and isinstance(sub.get("where"), dict) else {}
    for layer in (base, base.get(site) if site and isinstance(base.get(site), dict) else {}):
        for key in ("guests", "fastest_guests"):
            if isinstance(layer.get(key), bool):
                setattr(s, key, layer[key])
        for key, low in (("guest_max_days", 0.0), ("overflow_max_hours", 0.0)):
            v = layer.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= low:
                setattr(s, key, float(v))
        v = layer.get("reserve_nodes")
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            s.reserve_nodes = v
    return s


@dataclass
class Part:
    name: str
    nodes: frozenset
    kind: str = "small"            # small, big, gpu, condo, mixed
    default: bool = False
    mem_mb: int | None = None      # its smallest node's memory
    max_mem_mb: int | None = None  # its biggest node's
    gpus: int = 0                  # the most GPUs one of its nodes has
    aliases: list = field(default_factory=list)
    condos: list = field(default_factory=list)     # the condos whose nodes it holds (names)
    mine: bool = False             # every condo it holds is the person's lab's

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "default": self.default, "nodes": len(self.nodes),
                "mem_mb": self.mem_mb, "max_mem_mb": self.max_mem_mb, "gpus": self.gpus,
                "aliases": list(self.aliases), "condos": list(self.condos), "mine": self.mine}


def node_specs(conn: sqlite3.Connection, site: str | None) -> dict:
    """{node: (memory MB, GPUs)} from each node's latest reading."""
    from nomad.submit.cluster import gres_gpus
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(node_state)")}
    except sqlite3.Error:
        return {}
    if "node_name" not in cols:
        return {}
    mem = "memory_total_mb" if "memory_total_mb" in cols else "NULL"
    gres = "gres" if "gres" in cols else "NULL"
    where, args = "", []
    if site is not None and "source_site" in cols:
        where, args = "WHERE source_site = ?", [site]
    out = {}
    try:
        rows = conn.execute(f"SELECT node_name, {mem}, {gres}, MAX(timestamp) FROM node_state {where} "
                            "GROUP BY node_name", args).fetchall()
        # a node's latest reading may lack its memory (down): its latest one that has it
        known = {} if mem == "NULL" else {r[0]: r[1] for r in conn.execute(
            f"SELECT node_name, {mem}, MAX(timestamp) FROM node_state "
            f"{where + ' AND' if where else 'WHERE'} {mem} > 0 GROUP BY node_name", args)}
    except sqlite3.Error:
        return {}
    for node, m, g, _t in rows:
        m = m or known.get(node)
        out[str(node)] = (int(m) if m else None, gres_gpus(g))
    return out


def partitions(rows: list, specs: dict, condos: list, guest_parts=()) -> list[Part]:
    """The site's partitions sorted into kinds (see the top). ``rows``:
    nomad.partitions.current(); ``condos``: [(name, nodes, mine)];
    ``guest_parts``: the partitions report.toml says guests use (its
    guest_partitions), condos too whatever else they hold. A node in a
    guest partition (those, or one named like "condo" that isn't the
    default) is a condo's even if report.toml lists no condo for it: its
    lab is unknown, so it is no one's own and never shared."""
    from nomad.hostlist import expand_hostlist
    condo_nodes = {}
    for name, nodes, mine in condos:
        for n in nodes:
            condo_nodes.setdefault(n, []).append((name, mine))
    guests = set(guest_parts) | {r["partition"] for r in rows
                                 if "condo" in str(r.get("partition", "")).lower()}
    for r in rows:
        if r.get("partition") in guests and not r.get("is_default") and r.get("nodes"):
            for n in expand_hostlist(r["nodes"]):
                condo_nodes.setdefault(n, [("", False)])      # a condo report.toml doesn't list
    parts: list[Part] = []
    for r in rows:
        nodes = frozenset(expand_hostlist(r.get("nodes"))) if r.get("nodes") else frozenset()
        if not nodes:
            continue
        mems = [specs[n][0] for n in nodes if n in specs and specs[n][0]]
        every_mem = len(mems) == len(nodes)                # else small or big can't be told
        gpus = max((specs[n][1] for n in nodes if n in specs), default=0)
        p = Part(name=r["partition"], nodes=nodes, default=bool(r.get("is_default")),
                 mem_mb=min(mems) if mems else None, max_mem_mb=max(mems) if mems else None, gpus=gpus)
        held = [condo_nodes[n] for n in nodes if n in condo_nodes]
        if p.name in set(guest_parts) and not p.default:
            p.kind = "condo"
            p.condos = sorted({c for h in held for c, _m in h if c})
            p.mine = bool(held) and len(held) == len(nodes) and all(any(m for _c, m in h) for h in held)
        elif held and len(held) == len(nodes):
            p.kind = "condo"
            p.condos = sorted({c for h in held for c, _m in h if c})
            p.mine = all(any(m for _c, m in h) for h in held)
        elif held or not every_mem:
            p.kind = "mixed"
        elif gpus and all(n in specs and specs[n][1] for n in nodes):
            p.kind = "gpu"
        elif gpus:
            p.kind = "mixed"
        parts.append(p)
    # small or big: against the least memory of the shared CPU nodes, every one of them (not of the
    # partitions still small: one unread node there must not make the next ones up "small"). While
    # any shared node has no reading the least may be lower still: then nothing is small for sure.
    shared_nodes = {n for r in rows if r.get("nodes") for n in expand_hostlist(r["nodes"])} - set(condo_nodes)
    known = [specs[n][0] for n in shared_nodes if n in specs and specs[n][0] and not specs[n][1]]
    unread = any(n not in specs or not specs[n][0] for n in shared_nodes)
    base = min(known, default=None)
    for p in parts:
        if p.kind != "small":
            continue
        if base is None or not p.mem_mb:
            p.kind = "mixed"
        elif p.mem_mb > base:
            p.kind = "big"                                  # more than the least, whatever the unread have
        elif (p.max_mem_mb and p.max_mem_mb > base) or unread:
            p.kind = "mixed"
    # named a condo partition but holding nodes of no condo report.toml lists: never offered as shared
    for p in parts:
        if p.kind in ("small", "big", "gpu") and not p.default and "condo" in p.name.lower():
            p.kind = "mixed"
    # one name for the same nodes; a shared one inside another of its kind: left to that one
    def rank(p: Part):
        return (not p.default, p.name.lower().startswith("all"), len(p.name), p.name)
    kept: dict = {}
    for p in sorted(parts, key=rank):
        if p.nodes in kept:
            kept[p.nodes].aliases.append(p.name)
            continue
        kept[p.nodes] = p
    out = list(kept.values())
    inner = {p.name for p in out for q in out if p is not q and not p.default
             and p.kind == q.kind and p.kind in ("small", "big", "gpu") and p.nodes < q.nodes}
    order = {"small": 0, "big": 1, "gpu": 2, "condo": 3, "mixed": 4}
    return sorted([p for p in out if p.name not in inner], key=lambda p: (order.get(p.kind, 9), p.mem_mb or 0, p.name))


def typical_waits(conn: sqlite3.Connection, site: str | None, names: list, now: datetime | None = None,
                  days: int = WAIT_DAYS) -> dict:
    """{partition: median wait in seconds} over the jobs that started in the
    last ``days`` (nomad's own queue history)."""
    now = now or datetime.now()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    except sqlite3.Error:
        return {}
    if not {"partition", "wait_time_seconds", "start_time"} <= cols or not names:
        return {}
    where, args = "", []
    if site is not None and "source_site" in cols:
        where, args = " AND source_site = ?", [site]
    marks = ",".join("?" * len(names))
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict = {}
    try:
        for part, wait in conn.execute(
                f"SELECT partition, wait_time_seconds FROM jobs WHERE partition IN ({marks}) "
                f"AND start_time >= ? AND wait_time_seconds IS NOT NULL AND wait_time_seconds >= 0{where}",
                [*names, since, *args]):
            out.setdefault(part, []).append(wait)
    except sqlite3.Error:
        return {}
    return {p: int(statistics.median(v)) for p, v in out.items() if len(v) >= 5}


@dataclass
class Job:
    mem_mb: int | None = None      # per node
    time_s: int | None = None      # None: no limit asked
    gpus: int = 0


def job_from(mem=None, time=None, gpus=None) -> Job:
    """A Job from the values as the Console sends them ("12G", "4:00:00", 1)."""
    from nomad.submit.spec import parse_memory_mb, parse_time_seconds
    return Job(mem_mb=parse_memory_mb(mem) if mem else None, time_s=parse_time_seconds(time) if time else None,
               gpus=int(gpus) if isinstance(gpus, int) and gpus > 0 else 0)


def candidates(parts: list[Part], job: Job, cfg: Settings) -> list[dict]:
    """The partitions to ask Slurm about, in the site's order, each with its
    role and whether nomad may suggest it for this job (and why not)."""
    small_max = max((p.max_mem_mb or 0 for p in parts if p.kind == "small"), default=0)
    needs_big = bool(job.mem_mb and small_max and job.mem_mb > small_max)
    out = []

    def add(p: Part, role: str, ok: bool = True, why: str = "") -> None:
        if job.mem_mb and p.max_mem_mb and job.mem_mb > p.max_mem_mb:
            return                                         # no node of it has the memory
        if any(c["partition"] == p.name for c in out):
            return                                         # offered already (the default, say)
        out.append({"partition": p.name, "role": role, "allowed": ok, "why": why, **p.to_dict()})

    want_gpu = job.gpus > 0
    mine = [p for p in parts if p.kind == "condo" and p.mine and (p.gpus > 0) == want_gpu]
    for p in mine:
        add(p, "yours")
    if want_gpu:
        for p in parts:
            if p.kind == "gpu":
                add(p, "shared")
    else:
        for p in parts:
            if p.kind == "small":
                add(p, "shared")
    # where the job goes if it names none: always offered (a GPU job: if it has GPUs)
    default = next((p for p in parts if p.default), None)
    if default is not None and default.kind != "condo" and (default.gpus > 0 or not want_gpu):
        add(default, "default")
    if cfg.guests:
        guest = [p for p in parts if p.kind == "condo" and not p.mine and (p.gpus > 0) == want_gpu]
        # the one holding every condo first (Slurm picks the free one), then each
        guest.sort(key=lambda p: (-len(p.nodes), p.name))
        limit = cfg.guest_max_days * 86400
        # one that holds several condos (Slurm picks the free one) stands for them all
        for p in guest[:1] if guest and (len(guest[0].condos) > 1 or len(guest) == 1
                                         or guest[0].nodes > guest[1].nodes) else guest:
            if job.time_s is None:
                add(p, "guest", False, f"a guest's job needs a time limit (at most {cfg.guest_max_days:g} days)")
            elif job.time_s > limit:
                add(p, "guest", False, f"a guest's job may run at most {cfg.guest_max_days:g} days")
            else:
                add(p, "guest")
    if not want_gpu:
        for p in sorted((p for p in parts if p.kind == "big"), key=lambda p: (p.mem_mb or 0, p.name)):
            if needs_big:
                add(p, "shared")
            elif job.time_s is None:
                add(p, "overflow", False, f"big-memory nodes take a job that doesn't need them only with a time "
                                          f"limit of at most {cfg.overflow_max_hours:g} hours")
            elif job.time_s > cfg.overflow_max_hours * 3600:
                add(p, "overflow", False, f"big-memory nodes take a job that doesn't need them for at most "
                                          f"{cfg.overflow_max_hours:g} hours")
            else:
                add(p, "overflow")
    return out


def _at(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def rank(cands: list[dict], starts: dict | None, cfg: Settings, waits: dict | None = None) -> dict:
    """``cands`` with Slurm's estimates (``starts``: nomad-gate starts) and
    the usual waits: each row's start, whether it starts within
    SOON_MINUTES, whether Slurm only puts it LATER_DAYS or more away
    ("later": it can't say when), why not if it can't be suggested.

    The recommended one: the first in the site's order that starts soon,
    else the earliest start Slurm could tell, else the shortest usual wait,
    else the first in the site's order. "Fastest start" takes the same but
    only on something known (else none), and never a guest condo unless
    ``fastest_guests``."""
    by = {s.get("partition"): s for s in (starts or {}).get("starts") or []}
    states = (starts or {}).get("nodes") if isinstance((starts or {}).get("nodes"), dict) else {}
    now = _at((starts or {}).get("now"))
    rows = []
    for c in cands:
        s = by.get(c["partition"]) or {}
        at = _at(s.get("start"))
        row = {**c, "start": s.get("start"), "soon": bool(s.get("soon")), "error": s.get("error"),
               "later": bool(at and now and at >= now + timedelta(days=LATER_DAYS) and not s.get("soon")),
               "usual_wait_s": (waits or {}).get(c["partition"])}
        if row["allowed"] and c["role"] == "overflow":
            st = states.get(c["partition"])
            if not isinstance(st, dict):
                row["allowed"] = False
                row["why"] = ("nomad can't tell how many of its nodes are idle, and some stay free for jobs "
                              "that need big memory")
            else:
                idle = st.get("idle") if isinstance(st.get("idle"), int) else 0
                if idle <= cfg.reserve_nodes:
                    row["allowed"] = False
                    row["why"] = (f"only {idle} of its nodes {'is' if idle == 1 else 'are'} idle, and "
                                  f"{cfg.reserve_nodes} stay{'s' if cfg.reserve_nodes == 1 else ''} free for jobs "
                                  "that need big memory")
        if row["error"] and row["allowed"]:
            row["allowed"] = False
            row["why"] = f"Slurm: {row['error']}"
        rows.append(row)

    def best(pool: list, guess: bool) -> str | None:
        soon = [r for r in pool if r["soon"]]
        if soon:
            return soon[0]["partition"]
        known = [r for r in pool if _at(r["start"]) and not r["later"]]
        if known:
            return min(known, key=lambda r: _at(r["start"]))["partition"]
        waited = [r for r in pool if r["usual_wait_s"] is not None]
        if waited:
            return min(waited, key=lambda r: r["usual_wait_s"])["partition"]
        return pool[0]["partition"] if pool and guess else None

    ok = [r for r in rows if r["allowed"]]
    rec = best(ok, True)
    fastest = best([r for r in ok if r["role"] != "guest" or cfg.fastest_guests], False)
    for r in rows:
        r["recommended"] = r["partition"] == rec
    return {"rows": rows, "recommended": rec, "fastest": fastest,
            "settings": {"guests": cfg.guests, "guest_max_days": cfg.guest_max_days,
                         "overflow_max_hours": cfg.overflow_max_hours, "reserve_nodes": cfg.reserve_nodes,
                         "fastest_guests": cfg.fastest_guests, "later_days": LATER_DAYS}}


def _nodes_of(c, by_part: dict) -> list:
    from nomad.hostlist import expand_hostlist
    return list(c.nodes) or (expand_hostlist(by_part[c.part]["nodes"])
                             if c.part in by_part and by_part[c.part].get("nodes") else [])


def my_condos(report, site: str, user: str, rows: list, people_of) -> list:
    """[(condo, nodes, mine)] for ``site``'s condos in report.toml
    (``report``: nomad.condo.config Settings): its nodes, else its
    partition's; mine: the condo is confirmed and the person is among its
    people (``people_of(condo)``: the condo reports' own reading of
    ``members`` -- a list, "group:NAME", "account:NAME", else the owners'
    lab groups -- and its owners). One not yet confirmed is still a condo's
    nodes, never anyone's own: nomad doesn't offer them as shared."""
    by_part = {r["partition"]: r for r in rows}
    out = []
    for c in report.cluster(site).condos.values():
        nodes = _nodes_of(c, by_part)
        if not nodes:
            continue
        mine = False
        if c.confirmed:
            people = {str(p).lower() for p in (people_of(c) or ())} | {o.lower() for o in c.owners}
            mine = user.lower() in people
        out.append((c.name, nodes, mine))
    return out


def unplaced_condos(report, site: str, rows: list) -> list:
    """The condos report.toml lists for ``site`` whose nodes nomad can't
    find (no ``nodes``, and its ``partition`` isn't one of Slurm's): their
    nodes could look shared, so nothing but the default is suggested."""
    by_part = {r["partition"]: r for r in rows}
    return sorted(c.name for c in report.cluster(site).condos.values() if not _nodes_of(c, by_part))


def people_lookup(conn, config: dict | None, site: str, now: datetime | None = None):
    """people_of(condo) as the condo reports work out a condo's members."""
    from nomad.condo.figures import resolve_members
    from nomad.config.access import access_from, members_lookup
    acc = access_from(config or {}, log=False)
    exists, members_of = members_lookup(conn)

    def people_of(c) -> set:
        try:
            people, _how = resolve_members(conn, c, acc, members_of, exists, site, now)
        except Exception:                        # noqa: BLE001 -- unknown: its owners only
            people = set(c.owners)
        return people
    return people_of


MAX_ASKED = 12      # nomad-gate starts takes at most this many


def _at_most(names: list, parts: list, n: int) -> list:
    """The first ``n`` of ``names``, keeping a GPU partition among them when
    there is one (asked for every job: one with GPUs or without)."""
    if len(names) <= n:
        return names
    gpu = {p.name for p in parts if p.gpus > 0}
    keep = names[:n]
    first = next((x for x in names if x in gpu), None)
    if first is not None and first not in keep:
        keep = keep[:n - 1] + [first]
    return keep


def advise(conn: sqlite3.Connection, site: str, user: str, job: Job, *, config: dict | None, report,
           ask_starts, now: datetime | None = None) -> dict:
    """Everything for the Submit page's "Where it starts": the site's
    partitions sorted, the ones for this job in order, Slurm's estimates
    for them (``ask_starts(names)``: nomad-gate starts, as the person) and
    the usual waits; ranked. Never raises for what the gate said."""
    from nomad import partitions as _parts
    cfg = settings(config, site, getattr(getattr(report, "policy", None), "guest_max_days", None))
    rows = _parts.current(conn, site)
    listed = report is not None and report.cluster(site).configured
    lost = unplaced_condos(report, site, rows) if listed else []
    condos = my_condos(report, site, user, rows, people_lookup(conn, config, site, now)) if listed else []
    guest_parts = report.cluster(site).guest_partitions if listed else []
    parts = partitions(rows, node_specs(conn, site), condos, guest_parts)
    note = None
    if not listed:
        # condos nomad doesn't know of would look like shared nodes: only the default partition
        note = (f"report.toml lists no condos for {site}, so nomad can't tell condo nodes from shared ones: it "
                "suggests only the default partition (nomad usage-report condos init lists them; a site with "
                f"none: an empty [report.clusters.{site}.condos])")
    elif lost:
        note = (f"nomad can't find the nodes of {', '.join(lost)} (report.toml gives no nodes, and its partition "
                "isn't one of Slurm's now), so it can't tell them from shared ones: it suggests only the default "
                "partition (give their nodes or partition in report.toml)")
    if note:
        parts = [p for p in parts if p.default]
    # Slurm is asked about every partition nomad might suggest (for a job with
    # GPUs or without); which suit this job is settled with the values nomad
    # itself would send, which its answer carries.
    names = []
    for probe in (Job(time_s=1), Job(time_s=1, gpus=1)):
        for c in candidates(parts, probe, cfg):
            if c["partition"] not in names:
                names.append(c["partition"])
    names = _at_most(names, parts, MAX_ASKED)
    if not names:
        return {"ok": True, "rows": [], "recommended": None, "fastest": None, "note": note,
                "why": "nomad knows no partition here to suggest"}
    try:
        starts = ask_starts(names)
    except Exception as e:                       # noqa: BLE001 -- said, never raised
        starts = {"ok": False, "error": str(e)}
    if not isinstance(starts, dict):
        starts = {"ok": False, "error": "the cluster's answer wasn't one nomad reads"}
    if not starts.get("ok") and "not something the gate does: starts" in str(starts.get("error")):
        starts["error"] = ("the cluster's nomad is older than 1.25.4: it can't say yet when a job would start "
                           "(nomad-gate there needs 1.25.4)")
    sent = starts.get("job") if starts.get("ok") and isinstance(starts.get("job"), dict) else None
    if sent:
        job = Job(mem_mb=sent.get("mem_mb") or job.mem_mb, time_s=sent.get("time_s"),
                  gpus=int(sent.get("gpus") or 0))
    cands = [c for c in candidates(parts, job, cfg) if c["partition"] in names]
    out = rank(cands, starts if starts.get("ok") else None, cfg, typical_waits(conn, site, names, now))
    out["ok"] = True
    if not starts.get("ok"):
        out["error"] = starts.get("error") or "the cluster didn't say when it would start"
    out["now"] = starts.get("now")
    out["note"] = note
    out["job"] = {"mem_mb": job.mem_mb, "time_s": job.time_s, "gpus": job.gpus}
    out["partitions"] = [p.to_dict() for p in parts]
    return out


__all__ = ["Settings", "settings", "Part", "partitions", "node_specs", "typical_waits", "Job", "candidates",
           "rank", "SOON_MINUTES", "LATER_DAYS", "MAX_ASKED", "my_condos", "unplaced_condos", "people_lookup",
           "advise", "job_from"]
