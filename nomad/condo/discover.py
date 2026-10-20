# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Finding each cluster's condos from what the hub already has.

Each node reading (node_state) carries the node's partitions. The guest
partition is the one that lends the condo nodes to everyone (report.toml's
``guest_partitions``, or a partition named like "condo"); a condo is a
partition whose nodes all sit inside it. Its owner is guessed from the
partition's name (``parish``, ``parishlab``, ``parish_lab`` -> a NetID
ending in "parish"), and only drafted: `nomad usage-report condos init`
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
    ``patterns`` (most specific first) that fits: 'parish_lab' -> 'parish'.
    A number at the end is a second condo of the same lab ('yang2')."""
    text = re.sub(r"\d+$", "", partition.strip().lower())
    for pat in sorted(patterns, key=lambda p: -len(p.replace("{name}", ""))):
        pre, _, post = pat.lower().partition("{name}")
        m = re.fullmatch(re.escape(pre) + r"([a-z][a-z'-]*?)" + re.escape(post), text)
        if m and len(m.group(1)) >= 2:
            return m.group(1)
    return None


def _initials_then(name: str, netid: str) -> bool:
    """A NetID made of up to two initials then the last name ('cparish')."""
    netid = netid.lower()
    if not netid.endswith(name):
        return False
    head = netid[:-len(name)]
    return len(head) <= 2 and head.isalpha() or head == ""


def owner_candidates(conn, name: str, acc=None, site: str | None = None,
                     partition: str | None = None) -> list[tuple[str, str]]:
    """NetIDs that could own a condo named for ``name``, best first, each with
    why: a lab group named for them (by [console.labs] group_pattern), a
    lab they lead in nomad.toml, jobs in the partition, or an account."""
    score: dict[str, tuple[int, str]] = {}

    def add(netid, points, why):
        netid = str(netid or "").strip().lower()
        if netid and _initials_then(name, netid) and points > score.get(netid, (0, ""))[0]:
            score[netid] = (points, why)

    gcols = columns(conn, "group_membership")
    if acc is not None and getattr(acc, "group_pattern", "") and "group_name" in gcols:
        pre, _, post = acc.group_pattern.partition("{netid}")
        rx = re.compile(re.escape(pre) + r"(.+)" + re.escape(post) + r"$")
        for (g,) in conn.execute("SELECT DISTINCT group_name FROM group_membership"):
            m = rx.match(str(g or ""))
            if m:
                add(m.group(1), 4, f"lab group {g}")
    for netid in (getattr(acc, "leads", None) or {}):
        add(netid, 4, "leads a lab in nomad.toml")
    jcols = columns(conn, "jobs")
    if partition and {"user_name", "partition"} <= jcols:
        where = " AND source_site = ?" if site is not None and "source_site" in jcols else ""
        for (u,) in conn.execute(f"SELECT DISTINCT user_name FROM jobs WHERE partition = ?{where}",
                                 [partition] + ([site] if where else [])):
            add(u, 3, f"ran jobs in {partition}")
    if "username" in gcols:
        for (u,) in conn.execute("SELECT DISTINCT username FROM group_membership WHERE username LIKE ?",
                                 (f"%{name}",)):
            add(u, 1, "has an account")
    return [(n, why) for n, (p, why) in sorted(score.items(), key=lambda kv: (-kv[1][0], kv[0]))]


# --- report.toml draft -------------------------------------------------------------

def _key(name: str) -> str:
    return name if re.match(r"^[A-Za-z0-9_-]+$", name) else '"' + name.replace('"', '\\"') + '"'


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
    ``layouts``: (cluster, Found, [(partition, [(netid, why), ...])], or
    None for a cluster whose condos aren't drafted this time). A table the
    file has, in any form, is never declared again."""
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
        new = [(p, cands) for p, cands in (condos or ()) if p not in have_parts]
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
        for part, cands in new:
            nodes = sorted(found.condos[part])
            cores = sum(found.nodes[n].cores for n in nodes if n in found.nodes)
            gpus = sum(found.nodes[n].gpus for n in nodes if n in found.nodes)
            out += ["", f"[report.clusters.{ckey}.condos.{_key(part)}]"]
            if cands:
                netid, why = cands[0]
                others = "; also " + ", ".join(n for n, _ in cands[1:4]) if len(cands) > 1 else ""
                out.append(f"owner = {_q(netid)}                  # guessed: {why}{others}")
            else:
                out.append("owner = \"\"                         # not found from the name: set it")
            out.append(f"nodes = {_q(compress_hostlist(nodes))}            "
                       f"# {len(nodes)} node{'s' if len(nodes) != 1 else ''}, {cores} cores"
                       + (f", {gpus} GPUs" if gpus else ""))
            out.append("# members = \"group:NAME\"          # default: the owner's lab by [console.labs]")
            out.append("# email = \"\"                       # default: owner@mail_domain")
            out.append("confirmed = false")
        said.append(f"{cluster}: {len(new)} condo{'s' if len(new) != 1 else ''}")
    return "\n".join(out) + "\n", said
