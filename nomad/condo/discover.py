# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Finding each cluster's condos from what the hub already has.

Each node reading (node_state) carries the node's partitions. The guest
partition is the one that lends the condo nodes to everyone (report.toml's
``guest_partitions``, or a partition named like "condo"); a condo is a
partition whose nodes all sit inside it. Its owner is guessed from the
partition's name (``smith``, ``smithlab``, ``smith_lab`` -> a NetID
ending in "smith") and from who uses it, and only drafted: `nomad usage-report condos init`
writes the guess for someone to confirm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad.facts._common import columns, parse_time
from nomad.usage.calcs import gres_gpus
from nomad.usage.draft import compress_hostlist

# How far back the latest reading of a node may be and still say what the
# node is now (as a lab's membership: a day of collector trouble changes
# nothing).
RECENT_DAYS = 2
# What a site's own database (no source_site) is called in report.toml.
LOCAL = "this-cluster"
_GUEST_NAME = re.compile(r"condo", re.IGNORECASE)


@dataclass
class Node:
    name: str
    cores: int = 0
    memory_mb: float = 0.0
    gpus: int = 0
    partitions: tuple[str, ...] = ()


@dataclass
class Found:
    """One cluster's condo layout, as its nodes say now."""
    site: str | None
    nodes: dict[str, Node] = field(default_factory=dict)
    partitions: dict[str, set[str]] = field(default_factory=dict)   # partition -> nodes
    guest_partitions: list[str] = field(default_factory=list)
    condo_nodes: set[str] = field(default_factory=set)
    condos: dict[str, set[str]] = field(default_factory=dict)        # partition -> nodes
    when: str | None = None      # the latest reading


def local_name(settings, nomad_config: dict | None = None) -> str:
    """The name a site's own database goes by in report.toml (it has no
    source_site): "this-cluster" when report.toml says so, else the one
    cluster of nomad.toml ([clusters.ID], or its name) that report.toml
    lists condos for, as `nomad init` writes them."""
    clusters = getattr(settings, "clusters", {}) or {}
    if LOCAL in clusters:
        return LOCAL
    known = set()
    for cid, c in (((nomad_config or {}).get("clusters") or {}).items()):
        known.add(str(cid))
        if isinstance(c, dict) and c.get("name"):
            known.add(str(c["name"]))
    hits = sorted(n for n in known if n in clusters and clusters[n].configured)
    return hits[0] if len(hits) == 1 else LOCAL


def node_sites(conn, since: datetime | None = None) -> list[str | None]:
    """The sites with node readings since ``since`` (60 days ago) ([None]
    for a site's own database)."""
    cols = columns(conn, "node_state")
    if not {"node_name", "timestamp"} <= cols:
        return []
    if "source_site" not in cols:
        return [None]
    since = (since or datetime.now() - timedelta(days=60)).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT source_site FROM node_state WHERE timestamp >= ? AND source_site IS NOT NULL "
        "ORDER BY 1", (since,))]


def read_nodes(conn, site: str | None, until: datetime | None = None, latest: bool = True) -> Found:
    """Each node's latest reading in the RECENT_DAYS before ``until`` (now):
    cores, memory, GPUs and partitions. ``latest``: with none in that time,
    the RECENT_DAYS before the last reading there is."""
    found = Found(site=site)
    cols = columns(conn, "node_state")
    if not {"node_name", "timestamp"} <= cols:
        return found
    until = until or datetime.now()
    pick = ", ".join(c if c in cols else f"NULL AS {c}"
                     for c in ("cpus_total", "memory_total_mb", "gres", "partitions"))
    where = " AND source_site = ?" if site is not None and "source_site" in cols else ""
    args = [(until - timedelta(days=RECENT_DAYS)).isoformat(), until.isoformat()]
    args += [site] if where else []
    from nomad.condo.figures import _int, _num
    for name, cores, mem, gres, parts, when in conn.execute(
            f"SELECT node_name, {pick}, MAX(timestamp) FROM node_state "
            f"WHERE timestamp >= ? AND timestamp < ?{where} GROUP BY node_name", args):
        ps = tuple(p.strip() for p in str(parts or "").split(",") if p.strip())
        found.nodes[name] = Node(name, _int(cores), _num(mem) or 0.0, gres_gpus(gres), ps)
        for p in ps:
            found.partitions.setdefault(p, set()).add(name)
        if when and (found.when is None or when > found.when):
            found.when = when
    if not found.nodes and latest:
        # Nothing lately (a cluster that stopped reporting, a copy of the
        # database): as the nodes last said.
        row = conn.execute(f"SELECT MAX(timestamp) FROM node_state WHERE timestamp < ?{where}",
                           [until.isoformat()] + ([site] if where else [])).fetchone()
        last = parse_time(row[0]) if row and row[0] else None
        if last is not None:
            return read_nodes(conn, site, until=last + timedelta(seconds=1), latest=False)
    return found


def find(found: Found, guest_partitions: list[str] | None = None) -> Found:
    """Fill in the guest partitions, the condo nodes and the condos."""
    parts = found.partitions
    guests = [p for p in (guest_partitions or []) if p in parts]
    if not guest_partitions:
        guests = sorted(p for p in parts if _GUEST_NAME.search(p))
    found.guest_partitions = guests
    found.condo_nodes = set().union(*(parts[g] for g in guests)) if guests else set()
    if not found.condo_nodes:
        return found
    inside = {p: ns for p, ns in parts.items()
              if p not in guests and ns and ns <= found.condo_nodes}
    # A partition over every condo node is another way in for guests, unless
    # it is the only condo there is.
    whole = [p for p, ns in inside.items() if ns == found.condo_nodes]
    if len(inside) > len(whole):
        for p in whole:
            inside.pop(p)
            found.guest_partitions.append(p)
    found.condos = dict(sorted(inside.items()))
    return found


# --- owners ----------------------------------------------------------------------

def last_name(partition: str, patterns) -> str | None:
    """The owner's last name in a condo partition's name, by the first of
    ``patterns`` (most specific first) that fits: 'smith_lab' -> 'smith'.
    A number at the end is a second condo of the same lab ('lee2')."""
    text = re.sub(r"\d+$", "", partition.strip().lower())
    for pat in sorted(patterns, key=lambda p: -len(p.replace("{name}", ""))):
        pre, _, post = pat.lower().partition("{name}")
        m = re.fullmatch(re.escape(pre) + r"([a-z][a-z'-]*?)" + re.escape(post), text)
        if m and len(m.group(1)) >= 2:
            return m.group(1)
    return None


NETID_MAX = 8      # where an institution's NetIDs are cut short ([report.labs] netid_max_length)


def _initials_then(name: str, netid: str, netid_max: int = NETID_MAX) -> bool:
    """A NetID made of up to two initials then the last name ('jsmith'),
    perhaps a number after it ('jsmith3', two people of the same name), the
    name cut short where NetIDs stop ('tanderso' for Anderson, 'tanders2')."""
    netid = netid.lower()
    core = netid.rstrip("0123456789")
    for k in (1, 2, 0):
        head, tail = core[:k], core[k:]
        if k and not head.isalpha():
            continue
        if tail == name:
            return True
        if netid_max and len(netid) == netid_max and len(tail) >= 4 and name.startswith(tail):
            return True
    return False


def _lab_rx(acc):
    pattern = getattr(acc, "group_pattern", "") if acc is not None else ""
    if not pattern or "{netid}" not in pattern:
        return None
    pre, _, post = pattern.partition("{netid}")
    return re.compile("^" + re.escape(pre) + r"(.+)" + re.escape(post) + "$")


class Pool:
    """The NetIDs a condo's owner can be among, read once: lab groups' PIs
    (by [console.labs] group_pattern), those who lead labs in nomad.toml,
    who ran in the partition, and every account."""

    def __init__(self, conn, acc=None, site: str | None = None, partition: str | None = None):
        self.labs: dict[str, str] = {}
        self.leads = {str(n).lower() for n in (getattr(acc, "leads", None) or {})}
        self.ran: set[str] = set()
        self.accounts: set[str] = set()
        gcols = columns(conn, "group_membership")
        rx = _lab_rx(acc)
        if "group_name" in gcols and rx is not None:
            for (g,) in conn.execute("SELECT DISTINCT group_name FROM group_membership"):
                m = rx.match(str(g or ""))
                if m:
                    self.labs.setdefault(m.group(1).lower(), str(g))
        jcols = columns(conn, "jobs")
        if partition and {"user_name", "partition"} <= jcols:
            where = " AND source_site = ?" if site is not None and "source_site" in jcols else ""
            self.ran = {str(u).lower() for (u,) in conn.execute(
                f"SELECT DISTINCT user_name FROM jobs WHERE partition = ?{where}",
                [partition] + ([site] if where else [])) if u}
        if "username" in gcols:
            self.accounts = {str(u).lower() for (u,) in conn.execute(
                "SELECT DISTINCT username FROM group_membership") if u}

    def candidates(self, name: str, partition: str | None = None,
                   netid_max: int = NETID_MAX) -> list[tuple[str, str, int]]:
        """(NetID, why, strength) for ``name``, best first."""
        found: dict[str, tuple[int, str]] = {}

        def add(netid, points, why):
            if _initials_then(name, netid, netid_max) and points > found.get(netid, (0, ""))[0]:
                found[netid] = (points, why)
        for netid, g in self.labs.items():
            add(netid, 4, f"lab group {g}")
        for netid in self.leads:
            add(netid, 4, "leads a lab in nomad.toml")
        for netid in self.ran:
            add(netid, 3, f"ran jobs in {partition}")
        for netid in self.accounts:
            add(netid, 1, "has an account")
        return [(n, why, p) for n, (p, why) in sorted(found.items(), key=lambda kv: (-kv[1][0], kv[0]))]


def owner_candidates(conn, name: str, acc=None, site: str | None = None,
                     partition: str | None = None, netid_max: int = NETID_MAX) -> list[tuple[str, str]]:
    """NetIDs that could own a condo named for ``name``, best first, each with
    why: a lab group named for them (by [console.labs] group_pattern), a
    lab they lead in nomad.toml, jobs in the partition, or an account."""
    return [(n, why) for n, why, _ in Pool(conn, acc, site, partition).candidates(name, partition, netid_max)]


@dataclass
class UsageGroup:
    netid: str          # the PI the lab group is named for
    group: str
    people: int         # of those who ran in the partition, how many are in it
    of: int             # how many ran in the partition
    share: float        # of their core-hours, the group's members'


def usage_groups(conn, acc, site: str | None, partition: str, days: int = 365,
                 now: datetime | None = None) -> list[UsageGroup]:
    """The lab groups of the people who ran jobs in ``partition`` in the last
    ``days``: most of those people first, then most core-hours. A condo's
    partition is its owner's lab's, so its users' lab group points at the
    owner, whatever the partition is called."""
    rx = _lab_rx(acc)
    jcols = columns(conn, "jobs")
    if rx is None or not {"user_name", "partition", "end_time"} <= jcols \
            or "group_name" not in columns(conn, "group_membership"):
        return []
    since = ((now or datetime.now()) - timedelta(days=days)).isoformat()[:10]
    cpu = "COALESCE(req_cpus, 0)" if "req_cpus" in jcols else "1"
    run = "COALESCE(runtime_seconds, 0)" if "runtime_seconds" in jcols else "0"
    where = " AND source_site = ?" if site is not None and "source_site" in jcols else ""
    use: dict = {}
    for user, ch in conn.execute(
            f"SELECT user_name, SUM({cpu} * {run}) / 3600.0 FROM jobs WHERE end_time >= ?{where} "
            f"AND partition = ? GROUP BY user_name", [since] + ([site] if where else []) + [partition]):
        if user:
            use[user] = max(0.0, float(ch or 0))
    if not use:
        return []
    total = sum(use.values()) or 1.0
    groups: dict = {}
    for user, ch in use.items():
        for (g,) in conn.execute("SELECT DISTINCT group_name FROM group_membership WHERE username = ?", (user,)):
            m = rx.match(str(g or ""))
            if m:
                k = groups.setdefault(g, [m.group(1).lower(), 0, 0.0])
                k[1] += 1
                k[2] += ch
    out = [UsageGroup(n, g, k, len(use), round(c / total, 3)) for g, (n, k, c) in groups.items()]
    out.sort(key=lambda u: (-u.people, -u.share, u.group))
    return out


@dataclass
class Guess:
    owners: list[str]
    notes: list[str]            # the evidence, in words, for the draft's comments
    disagree: bool = False      # the name and the condo's users point at different labs


def _usage_words(u: UsageGroup) -> str:
    return (f"lab group {u.group} holds {u.people} of the {u.of} people who ran here "
            f"({100 * u.share:.0f}% of their core-hours)")


def guess_owners(conn, partition: str, acc=None, site: str | None = None, patterns=(),
                 netid_max: int = NETID_MAX, now: datetime | None = None) -> Guess:
    """Who owns a condo, from two kinds of evidence: its name (a NetID made
    of initials and the last name, cut short where NetIDs stop; two last
    names joined are two owners) and its users (the lab group that holds
    most of the people who ran in the partition). Each alone misleads: a
    partition can be named for another of the PI's surnames, and a condo
    lent out a lot is used mostly by guests' labs."""
    notes: list[str] = []
    usage = usage_groups(conn, acc, site, partition, now=now)
    top = usage[0] if usage else None
    # The users' lab, when it holds more than half of them (two at least).
    by_use = top if top is not None and top.people >= 2 and 2 * top.people > top.of else None

    pool = Pool(conn, acc, site, partition)

    def best(name):
        cands = pool.candidates(name, partition, netid_max)
        return cands[0] if cands else None

    owners: list[str] = []
    name = last_name(partition, patterns or ("{name}",))
    first = best(name) if name else None
    weak = [n for n, _, p in pool.candidates(name, partition, netid_max) if p == 1] if name else []
    if first is not None and first[2] == 1 and len(weak) > 1:
        # Only accounts that happen to fit, several of them: no guess.
        notes.append(f"name: {len(weak)} accounts fit, none a lab's PI: {', '.join(weak[:6])}")
        first = None
    elif first is not None:
        owners = [first[0]]
        notes.append(f"name: {first[1]}")
    elif name and not weak:
        # Two last names joined ('garciakhan'): two owners, when each half
        # (three letters at least) names someone; the split with the best
        # evidence on its weaker side wins.
        splits = []
        for i in range(3, len(name) - 2):
            a, b = best(name[:i]), best(name[i:])
            # One side at least a lab's PI: two accounts that happen to
            # spell the halves are no evidence.
            if a and b and a[0] != b[0] and max(a[2], b[2]) >= 4:
                splits.append((min(a[2], b[2]), a[2] + b[2], i, a, b))
        if splits:
            _, _, i, a, b = max(splits)
            owners = [a[0], b[0]]
            notes.append(f"name: two names, {name[:i]} ({a[1]}) and {name[i:]} ({b[1]})")
    strong = bool(first and first[1].startswith(("lab group", "leads")))
    disagree = False
    if by_use is not None:
        if not owners or (not strong and by_use.netid not in owners):
            # A name that names nobody, or only an account: the users' lab wins.
            if owners:
                notes.append(f"the name gave {', '.join(owners)} on weaker evidence")
            owners = [by_use.netid]
            notes.append("users: " + _usage_words(by_use))
        elif by_use.netid in owners:
            notes.append("users agree: " + _usage_words(by_use))
        else:
            disagree = True
            notes.append("users DISAGREE: " + _usage_words(by_use) + "; check")
    elif top is not None:
        notes.append("users: " + _usage_words(top) + " (too few to go by)")
    elif not owners:
        notes.append("nobody ran in the partition this past year, and the name names nobody")
    else:
        notes.append("nobody ran in the partition this past year")
    return Guess(owners, notes, disagree)


# --- report.toml draft -------------------------------------------------------------

def _key(name: str) -> str:
    return name if re.match(r"^[A-Za-z0-9_-]+$", name) else _q(name)


def _q(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def has_header(text: str, dotted: str) -> bool:
    """Whether the file declares [dotted] itself (a sub-table doesn't)."""
    parts = dotted.split(".")
    rx = r"\.".join(r"\s*(?:" + re.escape(p) + r'|"' + re.escape(p) + r'")\s*' for p in parts)
    return re.search(r"(?m)^\s*\[" + rx + r"\]\s*(?:#.*)?$", text) is not None


def draft(existing_text: str, existing: dict, layouts: list[tuple[str, Found, list]],
          settings, mail_domain_hint: str | None = None) -> tuple[str, list[str]]:
    """The text to add to report.toml, and what was added in words.
    ``layouts``: (cluster, Found, [(partition, Guess)], or None for a
    cluster whose condos aren't drafted this time). A table the file has,
    in any form, is never declared again."""
    report = (existing or {}).get("report") or {}
    out = [f"\n# --- Condos, drafted by `nomad usage-report condos init` on {datetime.now():%Y-%m-%d} "
           "from the nodes' partitions.",
           "# Check each owner, then set confirmed = true: a report goes to its owner only",
           "# once confirmed (and while review_to is set, only to review_to)."]
    said = []
    if "labs" not in report:
        out += ["", "[report.labs]",
                f"# mail_domain = {_q(mail_domain_hint or 'example.edu')}   # owner's address: NETID@mail_domain",
                "# send_from = \"Research Computing <rc@example.edu>\"",
                "review_to = []                     # while set, every report goes here instead",
                "# summary_to = []                  # the summary (default: review_to)",
                "name_members = true                # an owner's report names their own lab's members",
                "formats = [\"html\", \"pdf\", \"json\"]",
                "owner_patterns = [\"{name}\", \"{name}lab\", \"{name}_lab\", \"{name}-lab\"]",
                "netid_max_length = 8               # NetIDs cut short here (tanderso for Anderson); 0: never",
                "# contact = \"Questions: rc@example.edu\"",
                "# out_dir = \"/var/lib/nomad/reports/condos\"",
                "# institution = \"Research Computing\""]
        said.append("[report.labs]")
    if "policy" not in report:
        p = settings.policy

        def g(x):
            return "0" if x is None else f"{x:g}"
        out += ["", "[report.policy]                    # what the reports check against",
                f"owner_start_minutes = {p.owner_start_minutes:g}           # an owner's job starts on its own nodes within",
                f"guest_max_days = {p.guest_max_days:g}",
                f"guest_grace_minutes = {p.guest_grace_minutes:g}",
                f"home_quota_gb = {{ soft = {g(p.home_quota_gb.soft)}, hard = {g(p.home_quota_gb.hard)} }}",
                f"lab_scratch_quota_tb = {{ soft = {g(p.lab_scratch_quota_tb.soft)}, "
                f"hard = {g(p.lab_scratch_quota_tb.hard)} }}",
                f"scratch_path = {_q(p.scratch_path)}",
                f"use_below = {p.use_below:g}                    # \"uses less than a quarter of what it requests\""]
        said.append("[report.policy]")
    clusters = report.get("clusters") or {}
    for cluster, found, condos in layouts:
        ckey = _key(cluster)
        have = (clusters.get(cluster) or {}).get("condos") or {}
        have_parts = {(v or {}).get("partition") or k for k, v in have.items()}
        new = [(p, guess) for p, guess in (condos or ()) if p not in have_parts]
        guests = ", ".join(_q(g) for g in found.guest_partitions)
        if cluster not in clusters and not has_header(existing_text, f"report.clusters.{cluster}"):
            out += ["", f"[report.clusters.{ckey}]",
                    f"# label = {_q(cluster.capitalize())}             # how the reports name the cluster"]
            if found.guest_partitions:
                out.append(f"guest_partitions = [{guests}]")
            else:
                out.append("# guest_partitions = []            # no condos found: no partition named like condo")
        elif found.guest_partitions and condos is not None:
            out += ["", f"# [report.clusters.{ckey}]: guest partitions found: {guests}"
                    " (add guest_partitions there if that's wrong)"]
        if condos is None:
            continue
        if not found.condos:
            out.append(f"# {cluster}: no condos (no partition whose nodes all sit in a guest partition)")
            said.append(f"{cluster}: no condos")
            continue
        if not new:
            said.append(f"{cluster}: every condo already listed")
            continue
        for part, guess in new:
            nodes = sorted(found.condos[part])
            cores = sum(found.nodes[n].cores for n in nodes if n in found.nodes)
            gpus = sum(found.nodes[n].gpus for n in nodes if n in found.nodes)
            out += ["", f"[report.clusters.{ckey}.condos.{_key(part)}]"]
            for note in guess.notes:
                out.append(f"# {note}")
            if len(guess.owners) > 1:
                out.append(f"owner = [{', '.join(_q(o) for o in guess.owners)}]"
                           + ("   # CHECK: the name and the users disagree" if guess.disagree else "   # guessed"))
            elif guess.owners:
                out.append(f"owner = {_q(guess.owners[0])}"
                           + ("   # CHECK: the name and the users disagree" if guess.disagree else "   # guessed"))
            else:
                out.append("owner = \"\"   # not found: set it")
            out.append(f"nodes = {_q(compress_hostlist(nodes))}            "
                       f"# {len(nodes)} node{'s' if len(nodes) != 1 else ''}, {cores} cores"
                       + (f", {gpus} GPUs" if gpus else ""))
            out.append("# members = \"group:NAME\"          # default: the owner's lab by [console.labs]")
            out.append("# email = \"\"                       # default: owner@mail_domain")
            out.append("confirmed = false")
        said.append(f"{cluster}: {len(new)} condo{'s' if len(new) != 1 else ''}")
    return "\n".join(out) + "\n", said
