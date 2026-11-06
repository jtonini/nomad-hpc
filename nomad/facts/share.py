"""A lab's share of each cluster (1.30.0): how much of each cluster the lab's
jobs used over the last days, and where -- its own condo nodes, the shared
nodes, other labs' condos -- against everyone else's use and what the nodes
could have run; how full the lab kept its own condo; who waited for room
while the lab's jobs ran, and how the lab's own jobs waited.

- **Window:** from the midnight ``days`` before its end. With ``min_others``
  (the Console, for anyone but staff) it ends at a midnight (``whole_days``):
  the last one SETTLE_HOURS have passed since (a day is given once jobs still
  running at its end and the sites' last readings have reached the hub), and
  for a cluster whose readings are behind, the last one they settled; asked
  again the same day it answers the same. Staff and the command line end it
  now.
- **Nodes:** each cluster's nodes as its latest reading has them: their
  partitions say which are condo nodes (nomad.condo.discover, with
  report.toml's guest_partitions), and the condos report.toml lists (their
  nodes, else their partition's). A lab's own condos are the confirmed ones
  nomad.facts.condos.owns() gives it. A node read in the window but no
  longer is a shared node, unless report.toml lists it in a condo; its cores
  and GPUs are those of its last reading.
- **Capacity:** each node's cores (and GPUs) times the hours it was in
  service, as the condo reports count them (nomad.condo.figures.node_hours:
  down, drained with no job left and in maintenance are out; hours the
  collector didn't read count as in service).
- **Use:** each job's cores (Slurm's allocation, else the request) times the
  hours it ran inside the window, split evenly over its nodes, day by day.
  The lab's jobs are those of whoever was a member the day it ran (the groups
  collector: from the day it first saw them in the group, first_seen, to the
  day after it last did, or on while its cluster's latest groups run lists
  them; a membership seen before nomad 1.11, always), so a student joining or
  leaving moves no day already given (My Lab counts them alike); and,
  on its own condo, those in the condo's partition or under its Slurm account
  too, as the condo reports count them. Everyone else's are others'. On a
  condo two labs share (members naming both groups), the other lab's jobs
  there are the partners'.
- **Waiting for room:** a job that waited WAIT_MINUTES or more for the
  cluster -- Slurm's reason Resources or Priority, or none recorded; not its
  own limits, a dependency, a hold or nodes down -- for the nodes its
  partitions have now, and stopped waiting (started, or was cancelled) in the
  window: it counts on the day it stopped, and its wait is measured the same
  in every window (its last WAIT_LOOKBACK_DAYS at most). "While the lab's
  jobs ran there": for some of its wait the lab held cores on those nodes,
  off its own condo (its own nodes are its own to use); the time and the
  cores counted exactly. At the same time is not because of: Slurm's
  priorities, the jobs' sizes and everyone's jobs decide who starts when.
- **Its own condo, quiet:** ``condo_notes`` says where the lab kept its own
  condo under CONDO_QUIET_PCT busy while its own jobs that could have run
  there (no more cores than it has, no GPUs where it has none) waited a median
  of CONDO_NOTE_MINUTES or more elsewhere: the lab's own figures.
- **Others** (with ``min_others``): every figure about people outside the
  lab is a sum of days, and a day that rests on some but fewer than
  ``min_others`` of them is left out of it -- on that kind of node (their
  core-hours; their GPU-hours, among those who used GPUs), or in the waits
  that ended that day (the people who waited, those behind the lab and those
  not, and those who held the cores meanwhile) -- and said to be (how many
  days). The people counted are those who used cores, whom no lab in the
  answer claims, and who were never one of its labs' people (one asking
  about two labs sees both; and a lab's people are named in My Lab). They are
  counted on pieces of the nodes every lab in the answer has in common (each
  one's own condo, the other condos, the shared nodes), and the waits by the
  labs they waited behind, so that two labs' figures over different nodes
  can't be subtracted either. A sum of days each resting on none or at least
  ``min_others`` people rests on none or at least that many however answers
  are combined: two windows, two days, a row and the trend, two labs. Others'
  waits are given as an average (a sum), not a median (one job's). What a
  left-out day could be worked out from is not given (idle, where days are
  left out). Counts of people are not sums: two windows' can differ by one,
  which says someone ran in one and not the other, never what they ran.
  Guests and a partner lab on the lab's own condo are as its condo report
  gives them to its owners: totals, every day (not to a research assistant).
  What this can't stop: job records that reach the hub long after the day
  while its node readings do not (a later import of old jobs), and a
  membership the groups collector sees again after a gap, which starts anew.

As in nomad.facts.labs: a member's jobs count wholly toward the lab, someone
in two labs counts in both, and a research assistant sees the lab without its
PI's own jobs (those count with everyone else's).
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from nomad.facts._common import MAX_DAYS, envelope, iso, parse_time

WAIT_MINUTES = 15
WAIT_LOOKBACK_DAYS = 30
# Whole days: a day is given once this long has passed since its end (jobs still running then,
# and a site's last readings, reach the hub by the next syncs).
SETTLE_HOURS = 2
# A cluster whose groups runs stopped this long before the newest one's: its memberships end at their
# last sight.
MEMBERS_RUN_DAYS = 7
# A lab's own condo under this busy, while its jobs that could have run there waited at least
# CONDO_NOTE_MINUTES (median, CONDO_NOTE_JOBS of them or more) elsewhere: said (condo_notes).
CONDO_QUIET_PCT = 25
CONDO_NOTE_MINUTES = 60
CONDO_NOTE_JOBS = 5
STALE_AFTER_HOURS = 2
WHERE = ("own_condo", "shared", "other_condos")
_EPS = 1e-6
DAY = timedelta(days=1)


def bucket_days(days: int) -> int:
    """The trend's step: a day up to a month, a week up to four months, then four weeks."""
    return 1 if days <= 31 else 7 if days <= 120 else 28


def _window(days: int, now: datetime, whole_days: bool = False):
    """(days, lo, hi, spans): [lo, hi) from the midnight ``days`` before hi, hi
    now or (``whole_days``) the last midnight; the trend's spans, each starting
    at midnight, the last one ending at hi (or, when hi is not a midnight, at
    the midnight after it: node hours stop at hi)."""
    days = max(1, min(int(days), MAX_DAYS))
    midnight = datetime.combine((now - timedelta(hours=SETTLE_HOURS) if whole_days else now).date(),
                                datetime.min.time())
    hi = midnight if whole_days else now
    lo = midnight - timedelta(days=days) if whole_days else \
        datetime.combine((now - timedelta(days=days)).date(), datetime.min.time())
    step = timedelta(days=bucket_days(days))
    end = hi if hi == datetime.combine(hi.date(), datetime.min.time()) else \
        datetime.combine(hi.date(), datetime.min.time()) + DAY
    spans = []
    t = lo
    while t < hi:
        spans.append((iso(t)[:10], t, min(t + step, end)))
        t += step
    return days, lo, hi, spans


def _few(people, least: int) -> bool:
    """Too few people behind a figure to show it: some, and fewer than ``least``
    (none is no one's figure)."""
    return bool(least) and 0 < len(people) < least


def _pct(part, whole, digits=1):
    return None if not whole or part is None else round(float(100.0 * part / whole), digits)


def _r(x, d=1):
    return None if x is None else round(float(x), d)


def _labs_for(conn, acc, netid, groups, exists, members_of, tagged):
    """[(group, as, people)]: the labs ``netid`` leads or assists (or, for
    staff, the ``groups`` named), as nomad.facts.labs finds them."""
    led = list(groups) if groups is not None else (
        acc.lab_groups(netid, exists, tagged) if netid else [])
    led = [g for g in led if g not in acc.not_labs]
    assisted = [g for g in acc.assisted_groups(netid, members_of) if g not in led] \
        if groups is None and netid else []
    out = []
    for g in led + assisted:
        people = set(members_of(g)) if members_of is not None else set()
        if g in assisted:
            leaders = acc.leaders(g)
            people = {u for u in people if (u or "").strip().lower() not in leaders}
        out.append((g, None if groups is not None else ("assistant" if g in assisted else "pi"), people))
    return out


def _nodes_in(conn, site, lo, hi) -> dict:
    """{node: (cores, GPUs)} as each node read in [lo, hi) last said."""
    from nomad.condo.figures import _int
    from nomad.facts._common import columns
    from nomad.usage.calcs import gres_gpus
    cols = columns(conn, "node_state")
    if not {"node_name", "timestamp"} <= cols:
        return {}
    pick = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("cpus_total", "gres"))
    where = " AND source_site = ?" if site is not None and "source_site" in cols else ""
    # SQLite: the bare columns come from the row holding MAX(timestamp).
    return {name: (_int(cores), gres_gpus(gres)) for name, cores, gres, _t in conn.execute(
        f"SELECT node_name, {pick}, MAX(timestamp) FROM node_state WHERE timestamp >= ? AND timestamp < ?"
        f"{where} GROUP BY node_name", [lo.isoformat(), hi.isoformat()] + ([site] if where else [])) if name}


def _member_spans(conn, group: str, current: set, drop) -> dict:
    """{user: [(from, to)]}: when each person was one of ``group``'s, by day:
    from the day the groups collector first saw them in it (first_seen; not
    known, before nomad 1.11: always) to the day after it last did -- or on,
    for a membership listed in its cluster's latest groups run (a cluster still
    collecting). ``current``: members now with no record here (always).
    ``drop``: never (its PI, to a research assistant)."""
    from nomad.facts._common import columns
    cols = columns(conn, "group_membership")
    out: dict = defaultdict(list)
    if {"username", "group_name"} <= cols:
        first = "first_seen" if "first_seen" in cols else "NULL"
        last = "collected_at" if "collected_at" in cols else "NULL"
        site = "source_site" if "source_site" in cols else "NULL"
        cluster = "cluster" if "cluster" in cols else "NULL"
        latest = {(s_, c_): parse_time(t) for s_, c_, t in conn.execute(
            f"SELECT {site}, {cluster}, MAX({last}) FROM group_membership GROUP BY 1, 2")}
        newest = max((t for t in latest.values() if t is not None), default=None)
        for user, f, t, s_, c_ in conn.execute(
                f"SELECT username, {first}, {last}, {site}, {cluster} FROM group_membership "
                "WHERE group_name = ?", (group,)):
            if not user or user.strip().lower() in drop:
                continue
            f, t = parse_time(f), parse_time(t)
            top = latest.get((s_, c_))
            # listed in its cluster's latest run, and that cluster still collecting: on
            listed = t is None or (top is not None and t >= top - timedelta(minutes=1)
                                   and newest is not None and top >= newest - timedelta(days=MEMBERS_RUN_DAYS))
            a_ = datetime.combine(f.date(), datetime.min.time()) if f else None
            b_ = None if listed else datetime.combine(t.date(), datetime.min.time()) + DAY
            out[user].append((a_, b_))
    for user in current:
        if not out.get(user):
            out[user].append((None, None))
    return dict(out)


def _member_at(spans: dict, user: str, t) -> bool:
    for a, b in spans.get(user, ()):
        if (a is None or t is None or a <= t) and (b is None or t is None or t < b):
            return True
    return False


def _waits_for_room(j, lo, hi):
    """(a, b, minutes): a wait for room that ended in [lo, hi) (see the top):
    [a, b) its last WAIT_LOOKBACK_DAYS at most, ``minutes`` all of it; or None."""
    from nomad.db.jobkeys import waiting_for_cluster
    reason = j.pending_reason
    room = waiting_for_cluster(reason)
    if room is False or (room and not str(reason).startswith(("Resources", "Priority"))):
        return None
    began = j.ready_since if j.ready_since is not None and (j.ref is None or j.ready_since >= j.ref) else j.ref
    if began is None:
        return None
    if j.start is not None:
        b = j.start
    elif j.ended and j.end is not None:
        b = j.end                                # cancelled while it waited: it waited until then
    else:
        return None                              # waiting still: it counts on the day it stops
    if not (lo <= b < hi) or (b - began).total_seconds() < WAIT_MINUTES * 60:
        return None
    return max(began, b - timedelta(days=WAIT_LOOKBACK_DAYS)), b, (b - began).total_seconds() / 60.0


def share(conn: sqlite3.Connection | None, config: dict | None, netid: str | None = None, *,
          groups: list | None = None, days: int = 30, now: datetime | None = None, condos=None,
          min_others: int = 0, whole_days: bool | None = None) -> dict:
    """The share of each cluster of the labs ``netid`` leads or is a research
    assistant of (or, for staff, the ``groups`` named): see the top of the
    file. ``condos``: report.toml's condo settings (nomad.condo.config.load);
    without them, condos are what the nodes' partitions say and none is a
    lab's own. ``min_others``: leave out days of figures about people outside
    the lab resting on fewer than this many of them. ``whole_days``: end the
    window at the last midnight (default: with ``min_others``)."""
    from nomad.condo import discover
    from nomad.condo.config import ClusterCondos
    from nomad.condo.figures import load_jobs, node_hours
    from nomad.config.access import access_from, members_lookup, workstation_tags
    from nomad.facts.condos import owns
    now = now or datetime.now()
    whole = bool(min_others) if whole_days is None else bool(whole_days)
    days, lo, hi, spans = _window(days, now, whole)
    out = envelope("lab_share", now, {"days": days, "since": iso(lo), "until": iso(hi),
                                      "whole_days": whole, "bucket_days": bucket_days(days)})
    out["min_others"] = min_others
    out["labs"] = []
    out["notes"] = [
        "Nodes as each cluster's latest reading has them: a condo's nodes, its partition's.",
        "Capacity: each node's cores times the hours it was in service (down, drained with no job "
        "left and in maintenance are out; hours not read count as in service).",
        "Use: each job's cores times the hours it ran in the window, split over its nodes.",
        f"Waited for room: {WAIT_MINUTES} minutes or more, for the cluster (Resources, Priority), "
        "not for the job's own limits, a dependency or a hold; counted on the day the wait ended.",
        "While the lab's jobs ran there is at the same time, not because of: Slurm's priorities and "
        "the jobs' sizes decide who starts when.",
    ]
    if whole:
        out["notes"].append("The window ends at midnight: today's jobs count from tomorrow.")
    if min_others:
        out["notes"].append(f"A day resting on fewer than {min_others} people outside the lab is left out of "
                            "their figures (the days left out are counted).")
    if conn is None:
        return out
    acc = access_from(config or {}, log=False)
    exists, members_of = members_lookup(conn)
    tagged = workstation_tags(conn, now)
    labs = _labs_for(conn, acc, netid, groups, exists, members_of, tagged)
    if not labs:
        return out
    import dataclasses
    every = dataclasses.replace(acc, not_labs=frozenset())
    sites = discover.node_sites(conn, since=lo)
    local = discover.local_name(condos, config) if condos is not None else discover.LOCAL
    entries = {g: {"group": g, "name": acc.lab_names.get(g) or g, **({"as": how} if how else {}),
                   "members": len(people), "clusters": [], "condo_notes": []} for g, how, people in labs}
    # Who was each lab's when: a job is the lab's if its owner was a member the day it ran, so a
    # student joining or leaving never moves days already given between the lab and the rest.
    member_spans = {g: _member_spans(conn, g, people, acc.leaders(g) if how == "assistant" else ())
                    for g, how, people in labs}
    # Anyone ever one of these labs' is no outsider to how many people a figure rests on: the one asking
    # sees them by name in the lab they are in (My Lab), so they can't make up the others' number.
    adjacent = set().union(*(set(x) for x in member_spans.values()))
    lab_waits: dict = {}                        # (lab, cluster) -> its jobs' waits there
    owner_views = {g for g, how, _people in labs if how != "assistant"}
    for site in sites:
        cname = site if site is not None else local
        cc = condos.cluster(cname) if condos is not None else ClusterCondos(name=cname)
        found = discover.find(discover.read_nodes(conn, site, until=now), cc.guest_partitions)
        read = _nodes_in(conn, site, lo, hi)
        universe = set(read) | set(found.nodes)
        if not universe:
            continue
        # Every condo there: report.toml's, then the ones the nodes show that it doesn't list.
        sets = []
        listed_parts = set()
        for _name, c in sorted(cc.condos.items()):
            ns = set(c.nodes) if c.nodes else set(found.condos.get(c.part) or found.partitions.get(c.part, ()))
            listed_parts.add(c.part)
            sets.append((c, ns))
        for part, ns in sorted(found.condos.items()):
            if part not in listed_parts:
                sets.append((None, set(ns)))
        condo_nodes = set(found.condo_nodes).union(*(ns for _c, ns in sets)) if sets else set(found.condo_nodes)
        # Cores and GPUs: as each node says now, else as it last said in the window.
        cores_of = {n: v[0] for n, v in read.items()}
        gpus_of = {n: v[1] for n, v in read.items()}
        for n, i in found.nodes.items():
            cores_of[n], gpus_of[n] = i.cores, i.gpus
        seen = parse_time(found.when) if found.when else None
        stale = seen is None or (now - seen).total_seconds() / 3600 > STALE_AFTER_HOURS
        # Whole days: a cluster whose readings are behind ends at the last midnight they settled.
        site_hi = hi
        if whole and seen is not None:
            settled = datetime.combine((seen - timedelta(hours=SETTLE_HOURS)).date(), datetime.min.time())
            site_hi = max(lo, min(hi, settled))
        if site_hi <= lo:
            continue
        # (cut short only at a midnight: site_hi is one whenever it is before hi)
        site_spans = spans if site_hi == hi else [(k, a, min(b, site_hi)) for k, a, b in spans if a < site_hi]
        hours = node_hours(conn, site, universe, site_spans, gpus_of, until=site_hi, from_first_day=True)
        # The jobs of the window, and of the WAIT_LOOKBACK_DAYS before it (who held the nodes
        # while a wait that ended in the window began).
        jobs = load_jobs(conn, site, lo - timedelta(days=WAIT_LOOKBACK_DAYS), site_hi, now)
        # Each lab, and which of them each job is (several labs in one answer: someone in one is
        # no outsider to the other, as far as how many people a figure rests on goes).
        ctxs = {}
        for g, how, people in labs:
            leaders = acc.leaders(g)
            own = [(c, ns) for c, ns in sets
                   if c is not None and owns(c, g, leaders, acc.group_pattern,
                                             lambda n: every.lab_groups(n, exists, tagged))]
            ctxs[g] = _Lab(g, how, people, own, member_spans[g],
                           frozenset(leaders) if how == "assistant" else frozenset(), members_of)
        claims = {id(j): frozenset(g for g, ctx in ctxs.items() if ctx.claims(j)) for j in jobs}
        outsider = {id(j): not claims[id(j)] and j.user not in adjacent for j in jobs}
        # How many outsiders each figure rests on is counted on pieces every lab in the answer has
        # in common -- each one's own condo, the other condos, the shared nodes -- so two labs'
        # figures over different nodes can't be subtracted to give a few people's.
        owned: dict = defaultdict(set)
        for g, ctx in ctxs.items():
            for n in ctx.own_nodes:
                owned[n].add(g)
        parts = {}

        def part_of(n, owned=owned, condo_nodes=condo_nodes, parts=parts):
            if n not in parts:
                parts[n] = ("own", frozenset(owned[n])) if n in owned else \
                    ("condo",) if n in condo_nodes else ("shared",)
            return parts[n]
        pieces = _pieces(jobs, outsider, part_of, lo, site_hi)
        # A piece of a lab's own condo is shown to its owners whatever it rests on (their condo report).
        exempt = lambda piece, owner_views=owner_views: piece[0] == "own" and bool(piece[1] & owner_views)  # noqa: E731
        done = []
        for g, how, people in labs:
            entry, finish = _cluster(
                _Where(cname, cc.shown, site, found, stale, universe, condo_nodes, ctxs[g].own, cores_of, gpus_of),
                ctxs[g], claims, outsider, (part_of, pieces, exempt), hours, site_spans, jobs, lo, site_hi,
                min_others)
            if site_hi < hi:
                entry["until"] = iso(site_hi)
            done.append((g, entry, finish))
        # Waits: with several labs, each day's outsiders by which of the labs they waited behind,
        # every such group none or at least min_others, too.
        bad_days: set = set()
        if min_others and len(done) > 1:
            behind_of: dict = defaultdict(lambda: [None, None, set()])
            for g, _entry, finish in done:
                for key, (day, user, behind) in finish.outsiders.items():
                    x = behind_of[key]
                    x[0], x[1] = day, user
                    if behind:
                        x[2].add(g)
            groups_: dict = defaultdict(set)
            for day, user, labs_behind in behind_of.values():
                groups_[(day, frozenset(labs_behind))].add(user)
            bad_days = {day for (day, _k), users in groups_.items() if _few(users, min_others)}
        for g, entry, finish in done:
            entry["waits"] = finish(bad_days)
            entries[g]["clusters"].append(entry)
            lab_waits[(g, entry["cluster"])] = finish.lab_waits
    for g, _how, _people in labs:
        entries[g]["condo_notes"] = _condo_notes(entries[g]["clusters"], g, lab_waits)
    out["labs"] = [entries[g] for g, _how, _people in labs]
    return out


@dataclass
class _Where:
    """One cluster's layout, as every lab's figures there read it."""
    cluster: str
    label: str
    site: str | None
    found: object
    stale: bool
    universe: set
    condo_nodes: set
    own: list                   # this lab's own condos: [(Condo, nodes)]
    cores_of: dict
    gpus_of: dict


class _Lab:
    """One lab on one cluster: its own condos, who its people were when, and
    which jobs are its."""

    def __init__(self, group, how, people, own, spans, left_out, members_of):
        from nomad.condo.config import member_groups
        self.group, self.how, self.people, self.own, self.spans, self.left_out = \
            group, how, people, own, spans, left_out
        self.own_nodes = set().union(*(ns for _c, ns in own)) if own else set()
        self.own_parts = {c.part for c, _ns in own}
        self.own_accounts = {c.account for c, _ns in own if c.account}
        # A condo two labs share: the other groups its members name, and their people, on its nodes.
        self.partners_at: dict = {}
        self.all_partners: set = set()
        self.shared_with: dict = {}
        for c, ns in own:
            listed = c.members if isinstance(c.members, list) else [c.members] if c.members else []
            names = [x for m in listed for x in member_groups(str(m).strip())]
            others = [x for x in names if x != group]
            if not others:
                continue
            self.shared_with[c.name] = others
            ppl = set()
            for x in others:
                ppl |= set(members_of(x)) if members_of is not None else set()
            ppl -= people
            self.all_partners |= ppl
            for n in ns:
                self.partners_at.setdefault(n, set()).update(ppl)

    def claims(self, j) -> bool:
        """Whether the job is the lab's: its owner was a member when it ran (or waited); or, on its
        own condo, it ran in the condo's partition or under its account, as the condo report counts
        it -- not the partner lab's people, nor those the lab is shown without (its PI, to a research
        assistant). A job not started yet: in the lab's condo partitions only."""
        if _member_at(self.spans, j.user, j.start or j.ref or j.submit):
            return True
        if j.user in self.all_partners or j.user.strip().lower() in self.left_out:
            return False
        if j.nodes:
            on_own = any(n in self.own_nodes for n in j.nodes)
        else:
            on_own = bool(j.partitions) and set(j.partitions) <= self.own_parts
        return on_own and (bool(self.own_parts & set(j.partitions))
                           or (j.account is not None and j.account in self.own_accounts))


class _Cell:
    """One kind of node, one day: core-hours and GPU-hours, the lab's and
    others', and who."""
    __slots__ = ("lab", "lab_gpu", "others", "others_gpu", "partners", "lab_jobs", "lab_people",
                 "others_people")

    def __init__(self):
        self.lab = self.lab_gpu = self.others = self.others_gpu = self.partners = 0.0
        self.lab_jobs, self.lab_people, self.others_people = set(), set(), set()


def _condo_notes(clusters, group, lab_waits) -> list:
    """Where a lab kept its own condo quiet while its jobs that could have run there waited
    elsewhere: [{cluster, label, own_condo_pct, partitions, cores, gpus, elsewhere: [{cluster,
    label, jobs, median_minutes}]}], for each cluster where its condos were under
    CONDO_QUIET_PCT busy and at least CONDO_NOTE_JOBS such jobs waited a median of
    CONDO_NOTE_MINUTES or more on another cluster or another partition. Could have run there:
    no more cores than the condo has, and no GPUs where it has none (no more than it has). The
    lab's own figures: nothing about anyone else."""
    out = []
    for c in clusters:
        pct, condos = c.get("own_condo_pct"), c.get("own_condos") or []
        if not condos or pct is None or pct >= CONDO_QUIET_PCT:
            continue
        cores = sum(o["cores"] for o in condos)
        gpus = sum(o["gpus"] for o in condos)
        parts = {o["partition"] for o in condos}
        elsewhere = []
        for e in clusters:
            fit = [m for m, cpus, g_, ps in lab_waits.get((group, e["cluster"]), ())
                   if cpus <= cores and (g_ == 0 if not gpus else g_ <= gpus)
                   and not (e is c and parts & set(ps))]
            if len(fit) >= CONDO_NOTE_JOBS and statistics.median(fit) >= CONDO_NOTE_MINUTES:
                elsewhere.append({"cluster": e["cluster"], "label": e["label"], "jobs": len(fit),
                                  "median_minutes": round(statistics.median(fit))})
        if elsewhere:
            out.append({"cluster": c["cluster"], "label": c["label"], "own_condo_pct": pct,
                        "partitions": sorted(parts), "cores": cores, "gpus": gpus,
                        "elsewhere": sorted(elsewhere, key=lambda x: -x["jobs"])})
    return out


def _pieces(jobs, outsider, part_of, lo, hi) -> tuple:
    """({(piece, day): outsiders}, {(piece, day): outsiders who used GPUs}) over the jobs."""
    outs: dict = defaultdict(set)
    gouts: dict = defaultdict(set)
    for j in jobs:
        if not outsider[id(j)] or j.cpus <= 0 or not j.nodes or j.start is None or j.end is None:
            continue
        a, b = max(j.start, lo), min(j.end, hi)
        if b <= a:
            continue
        for d in range((a - lo).days, (b - lo - timedelta(microseconds=1)).days + 1):
            for n in j.nodes:
                outs[(part_of(n), d)].add(j.user)
                if j.gpus:
                    gouts[(part_of(n), d)].add(j.user)
    return outs, gouts


def _cluster(w: _Where, lab: _Lab, claims: dict, outsider: dict, pieces, hours, spans, jobs, lo, hi, min_others):
    """One lab on one cluster (see share)."""
    group, how = lab.group, lab.how
    own = w.own
    own_nodes = lab.own_nodes
    where_of = dict.fromkeys(own_nodes, "own_condo")
    for n in w.condo_nodes - own_nodes:
        where_of[n] = "other_condos"
    # The nodes counted: those read in the window (or now), by where they are.
    classes = {"own_condo": own_nodes & w.universe, "other_condos": (w.condo_nodes - own_nodes) & w.universe,
               "shared": w.universe - w.condo_nodes - own_nodes}
    # Guests and a partner lab on its own condo: as its condo report gives the owners, totals; to a
    # research assistant they are figures like any other (its PI's jobs are among them).
    owner_view = how != "assistant"
    partners_at, shared_with = lab.partners_at, lab.shared_with

    part_of, (part_outs, part_gouts), exempt = pieces
    cells: dict = defaultdict(_Cell)            # (where, day) -> _Cell
    cell_parts: dict = defaultdict(set)         # (where, day) -> the pieces its jobs ran on
    is_mine = {}
    waits = []                                  # (job, a, b, minutes, mine, outsider)
    for j in jobs:
        m = is_mine[id(j)] = group in claims[id(j)]
        wt = _waits_for_room(j, lo, hi)
        if wt is not None:
            waits.append((j, *wt, m, outsider[id(j)]))
        if not j.nodes or j.start is None or j.end is None:
            continue
        a, b = max(j.start, lo), min(j.end, hi)
        if b <= a:
            continue
        frac = 1.0 / len(j.nodes)
        for d in range((a - lo).days, (b - lo - timedelta(microseconds=1)).days + 1):
            d0 = lo + d * DAY
            h = (min(b, d0 + DAY) - max(a, d0)).total_seconds() / 3600.0
            if h <= 0:
                continue
            for n in j.nodes:
                k = where_of.get(n, "shared")
                c = cells[(k, d)]
                cell_parts[(k, d)].add(part_of(n))
                if m:
                    c.lab += j.cpus * h * frac
                    c.lab_gpu += j.gpus * h * frac
                    c.lab_jobs.add(j.job_id)
                    c.lab_people.add(j.user)
                else:
                    c.others += j.cpus * h * frac
                    c.others_gpu += j.gpus * h * frac
                    if j.cpus > 0:
                        c.others_people.add(j.user)
                    if k == "own_condo" and j.user in partners_at.get(n, ()):
                        c.partners += j.cpus * h * frac

    # Days resting on too few people outside the lab, on any piece of the nodes: left out of their
    # figures there.
    def few_on(key, outs):
        return any(not exempt(pc) and _few(outs.get((pc, key[1]), ()), min_others) for pc in cell_parts[key])
    few = {key for key in cells if few_on(key, part_outs)}
    few_gpu = few | {key for key in cells if few_on(key, part_gouts)}

    # Capacity, by where and by span (the cluster's, and the lab's own condo's).
    cap = {k: {"core_hours": 0.0, "gpu_hours": 0.0} for k in WHERE}
    cap_span: dict = defaultdict(float)
    cap_own_span: dict = defaultdict(float)
    for (n, key), nm in hours.items():
        k = where_of.get(n, "shared")
        cap[k]["core_hours"] += nm.cores * nm.in_service
        cap[k]["gpu_hours"] += nm.gpus * nm.in_service
        cap_span[key] += nm.cores * nm.in_service
        if k == "own_condo":
            cap_own_span[key] += nm.cores * nm.in_service

    def add_up(keys):
        """The figures of these cells: the lab's whole, others' without the days left out."""
        r = {"lab": 0.0, "lab_gpu": 0.0, "others": 0.0, "others_gpu": 0.0, "partners": 0.0,
             "lab_jobs": set(), "lab_people": set(), "others_people": set(), "left_out": set(),
             "gpu_left_out": set(), "others_any": False}
        for key in keys:
            c = cells[key]
            r["lab"] += c.lab
            r["lab_gpu"] += c.lab_gpu
            r["lab_jobs"] |= c.lab_jobs
            r["lab_people"] |= c.lab_people
            r["partners"] += c.partners
            r["others_any"] = r["others_any"] or bool(c.others_people)
            if key in few:
                r["left_out"].add(key[1])
            else:
                r["others"] += c.others
                r["others_people"] |= c.others_people
            if key in few_gpu:
                if c.others_gpu > 0:
                    r["gpu_left_out"].add(key[1])
            else:
                r["others_gpu"] += c.others_gpu
        return r

    by_where = {k: [key for key in cells if key[0] == k] for k in WHERE}
    present = [k for k in WHERE if (classes[k] or by_where[k]) and (k != "own_condo" or own)]
    out_rows = []
    for k in present:
        ns = classes[k]
        r, c = add_up(by_where[k]), cap[k]
        row = {"where": k, "nodes": len(ns), "cores": sum(w.cores_of.get(n, 0) for n in ns),
               "gpus": sum(w.gpus_of.get(n, 0) for n in ns),
               "capacity_core_hours": _r(c["core_hours"]), "capacity_gpu_hours": _r(c["gpu_hours"]),
               "lab_core_hours": _r(r["lab"]), "lab_gpu_hours": _r(r["lab_gpu"]),
               "lab_jobs": len(r["lab_jobs"]), "lab_people": len(r["lab_people"]),
               "lab_pct": _pct(r["lab"], c["core_hours"]), "lab_gpu_pct": _pct(r["lab_gpu"], c["gpu_hours"]),
               "others_core_hours": _r(r["others"]), "others_people": len(r["others_people"]),
               "others_pct": _pct(r["others"], c["core_hours"]),
               "others_gpu_hours": _r(r["others_gpu"]), "others_gpu_pct": _pct(r["others_gpu"], c["gpu_hours"]),
               # days left out of others' figures here (they rested on too few people)
               "others_left_out_days": len(r["left_out"]), "others_gpu_left_out_days": len(r["gpu_left_out"])}
        # Idle is what others' left-out days would be worked out from: given only without them.
        idle = max(0.0, c["core_hours"] - r["lab"] - r["others"])
        idle_gpu = max(0.0, c["gpu_hours"] - r["lab_gpu"] - r["others_gpu"])
        row["idle_pct"] = None if r["left_out"] else _pct(idle, c["core_hours"])
        row["idle_gpu_pct"] = None if r["left_out"] or r["gpu_left_out"] else _pct(idle_gpu, c["gpu_hours"])
        if k == "own_condo" and shared_with and owner_view:
            row["partners_core_hours"] = _r(r["partners"])
            row["guests_core_hours"] = _r(r["others"] - r["partners"])
        if not r["left_out"] and c["core_hours"] and r["lab"] + r["others"] > c["core_hours"] * 1.02:
            row["over_capacity"] = True
        out_rows.append(row)

    t = add_up([key for k in present for key in by_where[k]])
    cap_all = sum(cap[k]["core_hours"] for k in WHERE)
    cap_gpu_all = sum(cap[k]["gpu_hours"] for k in WHERE)
    total = {"nodes": len(w.universe), "cores": sum(w.cores_of.get(n, 0) for n in w.universe),
             "gpus": sum(w.gpus_of.get(n, 0) for n in w.universe),
             "capacity_core_hours": _r(cap_all), "capacity_gpu_hours": _r(cap_gpu_all),
             "lab_core_hours": _r(t["lab"]), "lab_gpu_hours": _r(t["lab_gpu"]),
             "lab_jobs": len(t["lab_jobs"]), "lab_people": len(t["lab_people"]),
             "lab_pct": _pct(t["lab"], cap_all), "lab_gpu_pct": _pct(t["lab_gpu"], cap_gpu_all),
             "others_core_hours": _r(t["others"]), "others_people": len(t["others_people"]),
             "others_pct": _pct(t["others"], cap_all), "others_gpu_hours": _r(t["others_gpu"]),
             "others_left_out_days": len(t["left_out"]), "others_gpu_left_out_days": len(t["gpu_left_out"]),
             "idle_pct": None if t["left_out"] else _pct(max(0.0, cap_all - t["lab"] - t["others"]), cap_all),
             # of everyone's use: everyone else's without the days left out (none, when every
             # day others used it is left out: then the lab's share of it is not known at all)
             "lab_of_used_pct": None if t["left_out"] and not t["others"] else
             _pct(t["lab"], t["lab"] + t["others"]),
             "lab_of_used_gpu_pct": None if t["gpu_left_out"] and not t["others_gpu"] else
             _pct(t["lab_gpu"], t["lab_gpu"] + t["others_gpu"])}

    step = (spans[1][1] - spans[0][1]).days if len(spans) > 1 else max(1, (spans[0][2] - spans[0][1]).days)
    trend_out = []
    for key, a, b in spans:
        first = (a - lo).days
        r = add_up([(k, d) for k in present for d in range(first, first + step) if (k, d) in cells])
        trend_out.append({
            "from": iso(a), "to": iso(min(b, hi)), "days": round((min(b, hi) - a).total_seconds() / 86400, 3),
            "capacity_core_hours": _r(cap_span.get(key, 0.0)),
            "own_capacity_core_hours": _r(cap_own_span.get(key, 0.0)) if own else None,
            "lab_core_hours": _r(r["lab"]),
            "lab_own_core_hours": _r(sum(cells[("own_condo", d)].lab for d in range(first, first + step)
                                         if ("own_condo", d) in cells)) if own else None,
            "others_core_hours": _r(r["others"]), "others_left_out_days": len(r["left_out"])})

    finish = _waits(w, jobs, waits, is_mine, outsider, own_nodes, lo, hi, min_others)

    coverage = []
    if w.stale:
        coverage.append("The cluster hasn't reported lately: its nodes are as it last said.")
    unread = sum(nm.not_read for nm in hours.values())
    whole = sum(nm.hours for nm in hours.values())
    if whole and unread > 0.02 * whole:
        coverage.append(f"The nodes went unread for {unread:,.0f} of {whole:,.0f} node-hours; those hours "
                        "count as in service.")
    if any(r.get("over_capacity") for r in out_rows):
        coverage.append("Jobs are recorded for more core-hours than some nodes were in service (jobs on "
                        "drained or unread nodes).")
    own_row = next((r for r in out_rows if r["where"] == "own_condo"), None)
    return {"cluster": w.cluster, "label": w.label, "site": w.site, "as_of": w.found.when, "stale": w.stale,
            "own_condos": [{"condo": c.name, "label": c.label or c.name, "partition": c.part,
                            "nodes": len(ns), "cores": sum(w.cores_of.get(n, 0) for n in ns),
                            "gpus": sum(w.gpus_of.get(n, 0) for n in ns),
                            **({"shared_with": shared_with[c.name]} if c.name in shared_with else {})}
                           for c, ns in own],
            # how busy the lab kept its own condo: its use there, of what the condo could run
            "own_condo_pct": own_row["lab_pct"] if own_row else None,
            "rows": out_rows, "total": total, "waits": None, "trend": trend_out, "coverage": coverage}, finish


class _Timeline:
    """Cores held on a set of nodes over [lo, hi), as step functions of the
    time (seconds from lo), exactly: each changes only where a job starts or
    ends. ``integral(name, a, b)`` (arrays of times) is the cores-seconds (or,
    for a 0/1 function, the seconds) between."""

    def __init__(self, np, times, deltas: dict):
        self.np = np
        t = np.asarray(times, dtype=float)
        order = np.argsort(t, kind="stable")
        self.t = t[order]
        n = len(self.t)
        vals = {}
        for name, d in deltas.items():
            v = np.cumsum(np.asarray(d, dtype=float)[order])
            vals[name] = np.where(np.abs(v) < _EPS, 0.0, v)
        on = (vals["lab_off"] > _EPS).astype(float)
        rates = {"all": vals["all"], "lab": vals["lab"], "all_off": vals["all_off"], "lab_off": vals["lab_off"],
                 "on": on, "all_on": vals["all_off"] * on,
                 "others_on": ((vals["all"] - vals["lab"]) > _EPS).astype(float)}
        dt = np.diff(self.t) if n > 1 else np.zeros(0)
        self.rates = rates
        self.cum = {k: np.concatenate(([0.0], np.cumsum(r[:-1] * dt))) if n else np.zeros(0)
                    for k, r in rates.items()}
        # When the lab held cores off its own condo: [start, end) runs, merged.
        runs = []
        for j in range(n - 1):
            if on[j] and dt[j] > 0:
                if runs and abs(runs[-1][1] - self.t[j]) < _EPS:
                    runs[-1][1] = self.t[j + 1]
                else:
                    runs.append([self.t[j], self.t[j + 1]])
        self.runs = runs
        self.run_starts = [r[0] for r in runs]

    def at(self, name, x):
        np = self.np
        x = np.asarray(x, dtype=float)
        if not len(self.t):
            return np.zeros_like(x)
        j = np.searchsorted(self.t, x, side="right") - 1
        jc = np.clip(j, 0, len(self.t) - 1)
        val = self.cum[name][jc] + self.rates[name][jc] * (x - self.t[jc])
        return np.where(j < 0, 0.0, val)

    def integral(self, name, a, b):
        return self.at(name, b) - self.at(name, a)

    def lab_runs_within(self, a: float, b: float) -> list:
        """The parts of [a, b) when the lab held cores off its own condo there."""
        out = []
        i = max(0, bisect.bisect_right(self.run_starts, a) - 1)
        while i < len(self.runs) and self.runs[i][0] < b:
            s, e = max(self.runs[i][0], a), min(self.runs[i][1], b)
            if e > s:
                out.append((s, e))
            i += 1
        return out


class _Users:
    """When each person outside the lab held cores on a set of nodes: to tell
    who of them held some during given times (and to stop at enough)."""

    def __init__(self):
        self.raw = defaultdict(list)

    def add(self, user, a, b):
        self.raw[user].append((a, b))

    def ready(self):
        self.iv = {}
        for u, xs in self.raw.items():
            xs.sort()
            merged: list = []
            for a, b in xs:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            self.iv[u] = ([m[0] for m in merged], [m[1] for m in merged])

    def during(self, times, have: set, least: int) -> None:
        """Add to ``have`` who held cores in any of ``times`` [(a, b)], until it holds ``least``."""
        for u, (starts, ends) in self.iv.items():
            if len(have) >= least:
                return
            if u in have:
                continue
            for a, b in times:
                k = bisect.bisect_right(ends, a)        # their first stretch ending after a
                if k < len(starts) and starts[k] < b:
                    have.add(u)
                    break


def _waits(w: _Where, jobs, waits, is_mine, outsider, own_nodes, lo, hi, min_others):
    """Who waited for room, and who held the nodes they could run on then (see the top): a
    function of the days left out besides (the answer's other labs' rule), giving the figures,
    with ``outsiders``, {job: (day, user, behind the lab)} for that rule."""
    import numpy as np
    found = w.found
    t0 = lo - timedelta(days=WAIT_LOOKBACK_DAYS)

    def nodes_for(j):
        return frozenset(n for p in j.partitions for n in found.partitions.get(p, ()))
    sets: dict = {}
    for j, *_rest in waits:
        ns = nodes_for(j)
        if ns:
            sets.setdefault(ns, len(sets))
    of_node: dict = defaultdict(list)
    for ns, i in sets.items():
        for n in ns:
            of_node[n].append(i)
    k = len(sets)
    ev = [{"t": [], "all": [], "lab": [], "all_off": [], "lab_off": []} for _ in range(k)]
    held = [_Users() for _ in range(k)]         # outsiders on the set's nodes
    held_off = [_Users() for _ in range(k)]     # ... off the lab's condo
    for j in jobs:
        if not j.nodes or j.start is None or j.end is None:
            continue
        a, b = max(j.start, t0), min(j.end, hi)
        if b <= a:
            continue
        hits: dict = defaultdict(lambda: [0, 0])  # set -> [its nodes the job ran on, of them off the lab's condo]
        for n in j.nodes:
            for i in of_node.get(n, ()):
                hits[i][0] += 1
                hits[i][1] += n not in own_nodes
        if not hits:
            continue
        sa, sb = (a - t0).total_seconds(), (b - t0).total_seconds()
        m = is_mine[id(j)]
        for i, (on, off) in hits.items():
            c_all, c_off = j.cpus * on / len(j.nodes), j.cpus * off / len(j.nodes)
            e = ev[i]
            e["t"] += (sa, sb)
            e["all"] += (c_all, -c_all)
            e["all_off"] += (c_off, -c_off)
            e["lab"] += (c_all, -c_all) if m else (0.0, 0.0)
            e["lab_off"] += (c_off, -c_off) if m else (0.0, 0.0)
            # who held cores while others waited: people no lab in the answer claims
            if not m and outsider[id(j)] and c_all > 0:
                held[i].add(j.user, sa, sb)
                if c_off > 0:
                    held_off[i].add(j.user, sa, sb)
    lines = []
    for i in range(k):
        e = ev[i]
        lines.append(_Timeline(np, e["t"], {x: e[x] for x in ("all", "lab", "all_off", "lab_off")}))
        held[i].ready()
        held_off[i].ready()
    cores_off = {i: sum(w.cores_of.get(n, 0) for n in ns if n not in own_nodes) for ns, i in sets.items()}

    # Each wait, worked out once: its day (when it ended), and what was held then.
    rows = []                                   # dicts, one a wait
    by_set: dict = defaultdict(list)
    for j, a, b, minutes, m, out in waits:
        r = {"job": id(j), "user": j.user, "day": (b - lo).days, "minutes": minutes, "mine": m,
             "outsider": out, "set": None, "behind": False, "cpus": j.cpus, "gpus": j.gpus,
             "partitions": tuple(j.partitions),
             "a": (a - t0).total_seconds(), "b": (b - t0).total_seconds()}
        rows.append(r)
        ns = nodes_for(j)
        if ns:
            r["set"] = sets[ns]
            by_set[r["set"]].append(r)
    for i, rs in by_set.items():
        tl = lines[i]
        sa = np.array([r["a"] for r in rs])
        sb = np.array([r["b"] for r in rs])
        vals = {name: tl.integral(name, sa, sb) for name in ("all", "lab", "others_on", "on", "lab_off", "all_on")}
        for q, r in enumerate(rs):
            for name, v in vals.items():
                r[name] = float(v[q])

    # Days, and who is behind each figure that day.
    day = defaultdict(lambda: {"others": set(), "behind": set(), "not_behind": set(), "held": set(),
                               "held_behind": set()})
    for r in rows:
        dd = day[r["day"]]
        if r["mine"]:
            if min_others and r["set"] is not None:
                held[r["set"]].during([(r["a"], r["b"])], dd["held"], min_others)
            continue
        if r["outsider"]:
            dd["others"].add(r["user"])
        behind = r["set"] is not None and r.get("on", 0.0) > _EPS
        r["behind"] = behind
        if r["outsider"]:
            dd["behind" if behind else "not_behind"].add(r["user"])
        if behind and min_others:
            held_off[r["set"]].during(lines[r["set"]].lab_runs_within(r["a"], r["b"]), dd["held_behind"],
                                      min_others)

    def median(xs):
        return round(statistics.median(xs)) if xs else None

    def finish(bad_days=frozenset()):
        def ok(d, *names):
            return not any(_few(day[d][n], min_others) for n in names)
        return _waits_out(rows, ok, bad_days, cores_off, median)
    finish.outsiders = {r["job"]: (r["day"], r["user"], r["behind"]) for r in rows
                        if not r["mine"] and r["outsider"]}
    # the lab's own waits, for what its own condos could have run (condo_notes)
    finish.lab_waits = [(r["minutes"], r["cpus"], r["gpus"], r["partitions"]) for r in rows if r["mine"]]
    return finish


def _waits_out(rows, ok, bad_days, cores_off, median) -> dict:
    others = [r for r in rows if not r["mine"] and r["day"] not in bad_days
              and ok(r["day"], "others", "behind", "not_behind")]
    behind = [r for r in others if r["behind"]]
    behind_held = [r for r in behind if ok(r["day"], "held_behind")]
    mine = [r for r in rows if r["mine"]]
    mine_held = [r for r in mine if r["set"] is not None and ok(r["day"], "held")]
    left_days = {r["day"] for r in rows if not r["mine"]} - {r["day"] for r in others}
    secs = sum(r["on"] for r in behind)
    held_lab = sum(r["lab_off"] for r in behind_held)
    held_all = sum(r["all_on"] for r in behind_held)
    m_all = sum(r["all"] for r in mine_held)
    m_wait = sum(r["b"] - r["a"] for r in mine_held)
    return {
        "wait_minutes": WAIT_MINUTES, "lookback_days": WAIT_LOOKBACK_DAYS,
        "others": {"jobs": len(others), "people": len({r["user"] for r in others}),
                   # the average (a sum the days' rule covers; a median is one job's wait)
                   "mean_minutes": round(sum(r["minutes"] for r in others) / len(others)) if others else None,
                   # days whose waits rested on too few people: left out
                   "left_out_days": len(left_days)},
        "behind_lab": {
            "jobs": len(behind), "people": len({r["user"] for r in behind}), "hours": _r(secs / 3600.0),
            "lab_cores_avg": _r(sum(r["lab_off"] for r in behind) / secs) if secs else None,
            "cores_there_avg": _r(sum(r["on"] * cores_off[r["set"]] for r in behind) / secs) if secs else None,
            # of the cores in use there, the lab's: the rest were others', so only over days when
            # enough of them held some
            "lab_of_busy_pct": _pct(held_lab, held_all),
            "busy_left_out_days": len({r["day"] for r in behind} - {r["day"] for r in behind_held})},
        "lab": {"jobs": len(mine), "people": len({r["user"] for r in mine}),
                "median_minutes": median([r["minutes"] for r in mine]),
                "others_of_busy_pct": _pct(m_all - sum(r["lab"] for r in mine_held), m_all),
                "while_others_ran_pct": _pct(sum(r["others_on"] for r in mine_held), m_wait),
                "busy_left_out_days": len({r["day"] for r in mine if r["set"] is not None}
                                          - {r["day"] for r in mine_held})},
    }
