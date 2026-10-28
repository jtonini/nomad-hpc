"""A lab's condo nodes (1.17.9): the nodes its PI owns in a cluster's condo,
as report.toml's condo reports name them, and how they are now.

Whose a condo is: only a confirmed one ([report.clusters.C.condos.NAME]
confirmed, the default); a draft is nobody's yet. When its ``members`` name
groups ("group:NAME"), it is those groups' labs' and no one else's.
Otherwise it is its owners': the lab group_pattern names for an owner
(``{netid}$``), or, for an owner who leads one lab only, that lab. An owner
who leads several labs, none of them named for them, gives none of them the
condo: which one it is for is report.toml's to say (``members``).

Its nodes are the ones report.toml lists, else its partition's, as the
cluster's latest reading has them (nomad.condo.discover). Now: the cluster's
latest reading (the node collector reads every node at once): each node up,
drained or down as Slurm says; a node missing from that reading (Slurm no
longer lists it) is said so, not counted up; and the cores allocated to jobs
on the nodes that are up, guests' jobs included (lending the nodes is what a
condo does). A cluster whose latest reading is over STALE_AFTER_HOURS old is
``stale``: its nodes are as it last reported them, and no share is given.

On a hub, report.toml's clusters are its sites; "this-cluster" (a site's own
database) has no readings there.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from nomad.facts._common import age_minutes, columns, fetch, num, parse_time, pct

STALE_AFTER_HOURS = 2
SAME_RUN_SECONDS = 120      # readings this close are one collector run


def owns(condo, group: str, leaders: set, pattern: str | None = None, labs_of=None) -> bool:
    """Whether a confirmed condo is this lab's (see the top of the file).
    ``leaders``: who leads ``group``; ``pattern``: nomad.toml's group_pattern;
    ``labs_of(netid)``: the labs a NetID leads."""
    if not getattr(condo, "confirmed", True):
        return False
    members = condo.members
    listed = members if isinstance(members, list) else [members] if members else []
    groups = [str(m).strip()[len("group:"):] for m in listed if str(m).strip().startswith("group:")]
    if groups:
        return group in groups
    owners = {str(o).strip().lower() for o in (condo.owners or ())}
    lead = owners & {str(x).strip().lower() for x in leaders}
    for owner in sorted(lead):
        if pattern and "{netid}" in pattern and pattern.replace("{netid}", owner) == group:
            return True
        if labs_of is not None and list(labs_of(owner)) == [group]:
            return True
    return False


def _nodes_now(conn, site, names: list, latest: str | None, cols: set) -> list:
    """Each node's latest reading up to the cluster's latest (``latest``),
    in the RECENT_DAYS before it: its state, cores, cores allocated, memory
    and GPUs, and whether it is in that latest reading."""
    from nomad.condo.discover import RECENT_DAYS
    from nomad.facts.sites import _condition, _gpu_count
    if not {"node_name", "timestamp"} <= cols or not names or not latest:
        return [{"node": n, "condition": None, "state": None, "as_of": None, "current": False,
                 "cores": 0, "cores_allocated": 0, "memory_gb": 0.0, "gpus": 0} for n in sorted(names)]
    top = parse_time(latest)
    pick = ", ".join(f"n.{c}" if c in cols else f"NULL AS {c}"
                     for c in ("state", "cpus_total", "cpus_alloc", "memory_total_mb", "gres"))
    same = " AND n.source_site = ?" if site is not None and "source_site" in cols else ""
    inner = " AND source_site = ?" if same else ""
    since = ((top or datetime.now()) - timedelta(days=RECENT_DAYS)).isoformat()
    rows = []
    names = sorted(set(names))
    for i in range(0, len(names), 500):
        part = names[i:i + 500]
        marks = ",".join("?" * len(part))
        rows += fetch(conn,
                      f"SELECT n.node_name AS node_name, {pick}, n.timestamp AS timestamp FROM node_state n "
                      f"JOIN (SELECT node_name, MAX(timestamp) AS t FROM node_state WHERE node_name IN ({marks}) "
                      f"  AND timestamp >= ? AND timestamp <= ?{inner} GROUP BY node_name"
                      f") l ON n.node_name = l.node_name AND n.timestamp = l.t{same}",
                      part + [since, latest] + ([site] if inner else []) + ([site] if same else []))
    out, seen = [], set()
    for r in rows:
        if r["node_name"] in seen:
            continue
        seen.add(r["node_name"])
        when = parse_time(r["timestamp"])
        current = bool(when and top and (top - when).total_seconds() <= SAME_RUN_SECONDS)
        out.append({"node": r["node_name"],
                    # missing from the cluster's latest reading: Slurm no longer lists it
                    "condition": _condition(r.get("state")) if current else "gone",
                    "state": r.get("state"), "as_of": str(r["timestamp"]), "current": current,
                    "cores": int(num(r.get("cpus_total")) or 0),
                    "cores_allocated": int(num(r.get("cpus_alloc")) or 0),
                    "memory_gb": round((num(r.get("memory_total_mb")) or 0) / 1024, 1),
                    "gpus": _gpu_count(r.get("gres"))})
    for n in sorted(set(names) - seen):
        out.append({"node": n, "condition": None, "state": None, "as_of": None, "current": False,
                    "cores": 0, "cores_allocated": 0, "memory_gb": 0.0, "gpus": 0})
    return sorted(out, key=lambda x: x["node"])


def lab_condos(conn, group: str, leaders: set, settings, now: datetime | None = None,
               found: dict | None = None, pattern: str | None = None, labs_of=None) -> list:
    """The lab's condos (see the top of the file), each with its nodes now.
    ``found``: each cluster's nodes, read once for several labs."""
    from nomad.condo.discover import LOCAL, Found, read_nodes
    now = now or datetime.now()
    found = {} if found is None else found
    cols = columns(conn, "node_state")
    hub = "source_site" in cols
    out = []
    for cname, cluster in sorted((getattr(settings, "clusters", None) or {}).items()):
        for name, condo in sorted(cluster.condos.items()):
            if not owns(condo, group, leaders, pattern, labs_of):
                continue
            # A hub's clusters are its sites; a site's own database is one cluster.
            site = None if (cname == LOCAL or not hub) else cname
            if cname not in found:
                found[cname] = Found(site=site) if (cname == LOCAL and hub) else read_nodes(conn, site, until=now)
            f = found[cname]
            listed = list(condo.nodes)
            names = listed or sorted(f.partitions.get(condo.part, ()))
            key = ("nodes", cname, tuple(names))
            if key not in found:
                found[key] = _nodes_now(conn, site, names, f.when, cols)
            nodes = found[key]
            age = age_minutes(f.when, now) if f.when else None
            stale = age is None or age > STALE_AFTER_HOURS * 60
            up = [n for n in nodes if n["condition"] == "up"]
            up_cores = sum(n["cores"] for n in up)
            allocated = sum(n["cores_allocated"] for n in up)
            out.append({
                "cluster": cname, "cluster_label": cluster.shown,
                "condo": name, "label": condo.label or name, "partition": condo.part,
                "owners": list(condo.owners),
                # nodes from report.toml, or the partition's in the latest reading
                "nodes_from": "listed" if listed else "partition",
                "nodes": nodes,
                "total": {"nodes": len(nodes), "cores": sum(n["cores"] for n in nodes),
                          "gpus": sum(n["gpus"] for n in nodes),
                          "memory_gb": round(sum(n["memory_gb"] for n in nodes), 1),
                          "up": len(up),
                          "drained": sum(1 for n in nodes if n["condition"] == "drained"),
                          "down": sum(1 for n in nodes if n["condition"] == "down"),
                          "gone": sum(1 for n in nodes if n["condition"] == "gone"),
                          "not_read": sum(1 for n in nodes if n["condition"] is None),
                          # on the nodes that are up: their cores, how many are allocated to
                          # jobs now (guests' included), and that share
                          "cores_up": None if stale else up_cores,
                          "cores_allocated": None if stale else allocated,
                          "cores_allocated_pct": None if stale else pct(allocated, up_cores)},
                "as_of": f.when, "stale": stale,
            })
    return out
