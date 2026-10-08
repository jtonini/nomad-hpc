# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad usage-report init`: a first report.toml from what nomad has seen."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime

from nomad.usage import calcs
from nomad.usage.sources import _columns, _site_filter

_TAIL = re.compile(r"^(.*?)(\d+)$")


def compress_hostlist(nodes) -> str:
    """['n01', 'n02', 'n03', 'n05', 'g1'] -> 'g1,n[01-03,05]'."""
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    plain = []
    for n in sorted(set(nodes)):
        m = _TAIL.match(n)
        if not m:
            plain.append(n)
            continue
        groups[(m.group(1), len(m.group(2)))].append(int(m.group(2)))
    parts = list(plain)
    for (prefix, width), nums in sorted(groups.items()):
        nums.sort()
        if len(nums) == 1:
            parts.append(f"{prefix}{str(nums[0]).zfill(width)}")
            continue
        runs, start, prev = [], nums[0], nums[0]
        for x in nums[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            runs.append(str(start).zfill(width) if start == prev
                        else f"{str(start).zfill(width)}-{str(prev).zfill(width)}")
            if x is not None:
                start = prev = x
        parts.append(f"{prefix}[{','.join(runs)}]")
    return ",".join(sorted(parts))


def _tier_name(cores, mem_gb, gpus) -> str:
    if gpus:
        return "gpu"
    if mem_gb and mem_gb >= 1000:
        return "large"
    if mem_gb and mem_gb >= 500:
        return "medium"
    return "basic"


def draft(conn, site: str | None, cluster: str) -> str:
    """report.toml text for ``cluster``, from the latest node samples."""
    cols = _columns(conn, "node_state")
    if not {"node_name", "cpus_total", "timestamp"} <= cols:
        raise ValueError("no node samples in this database (node_state): write report.toml by hand "
                         "from docs/report.example.toml")
    where, args = _site_filter(conn, "node_state", site)
    extra = ", ".join(c if c in cols else f"NULL AS {c}" for c in ("memory_total_mb", "gres", "partitions"))
    nodes = {}
    for r in conn.execute(f"SELECT node_name, cpus_total, {extra}, MAX(timestamp) FROM node_state "
                          f"WHERE 1=1{where} GROUP BY node_name", args):
        nodes[r[0]] = (r[1], (r[2] or 0) / 1024, calcs.gres_gpus(r[3]), str(r[4] or ""))
    if not nodes:
        raise ValueError("no node samples for this site")
    hw: dict[tuple, list[str]] = defaultdict(list)
    for n, (cores, mem, gpus, _) in nodes.items():
        hw[(cores, round(mem / 64) * 64, gpus)].append(n)
    names: dict[str, list[str]] = {}
    for (cores, mem, gpus), ns in sorted(hw.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][0])):
        base = _tier_name(cores, mem, gpus)
        name, k = base, 2
        while name in names:
            name, k = f"{base}{k}", k + 1
        names[name] = ns
    parts = defaultdict(set)
    for n, (_, _, _, ps) in nodes.items():
        for p in ps.split(","):
            if p.strip():
                parts[p.strip()].add(n)
    first_gpu = None
    if {"alloc_gpus", "start_time"} <= _columns(conn, "jobs"):
        w, a = _site_filter(conn, "jobs", site)
        row = conn.execute(f"SELECT MIN(start_time) FROM jobs WHERE alloc_gpus > 0{w}", a).fetchone()
        first_gpu = row[0] if row else None
    key = cluster if re.match(r"^[A-Za-z0-9_-]+$", cluster) else f'"{cluster}"'
    out = [
        f"# report.toml for `nomad usage-report`, drafted {datetime.now():%Y-%m-%d} from NØMAÐ's node samples"
        + (f" (site {site})" if site else "") + ".",
        "# Name the tiers, list the institution's (the others count as condo), check the",
        "# partition classes, and add application families. Site data: keep this file",
        "# off any public repository.",
        "",
        "[report]",
        'exclude_users = ["root"]          # accounts that are not people',
        "",
        f"[report.clusters.{key}]",
        "# Nodes grouped by hardware (cores, memory, GPUs); rename the groups to your tiers",
        "# and move condo (lab-owned) nodes into a tier of their own, e.g. condo = \"...\".",
    ]
    tiers = ", ".join(f'{n} = "{compress_hostlist(ns)}"' for n, ns in names.items())
    out.append(f"tiers = {{ {tiers} }}")
    for n, ns in names.items():
        c, m, g, _ = nodes[ns[0]]
        out.append(f"#   {n}: {len(ns)} nodes, {c} cores, about {m:.0f} GB" + (f", {g} GPUs" if g else "") + " each")
    out.append(f"institutional_tiers = [{', '.join(repr(n).replace(chr(39), chr(34)) for n in names)}]")
    out.append("# wait_tiers = [\"basic\", \"medium\", \"large\"]   # section 4 (default: institutional tiers without GPUs)")
    out.append(f"# gpu_accounting_start = \"{(first_gpu or 'YYYY-MM-DDTHH:MM:SS')[:19]}\""
               "   # when Slurm began accounting GPUs" + (" (first job with GPUs found)" if first_gpu else ""))
    out.append("# Partitions are classed by their nodes: one tier -> that tier, several -> overlay.")
    out.append("# Override where that is wrong, e.g. partition_classes = { short = \"basic\" }.")
    for p, ns in sorted(parts.items()):
        out.append(f"#   {p}: {compress_hostlist(ns)}")
    out.append('unlisted_partitions = "other"      # partitions no node lists any more')
    out += ["", f"[report.clusters.{key}.capacity]", "practical = 0.75", "core_weight = 1.45",
            "# planned = [ { label = \"new nodes\", cores = 768, weight = 1.45 } ]",
            "# growth = { low = 0.20, central = 0.26, high = 0.34 }   # default: from Slurm's totals", "",
            f"# [[report.clusters.{key}.gpu_families]]", "# name = \"molecular dynamics\"",
            "# regex = \"gmx|gromacs|amber|namd\"", ""]
    return "\n".join(out)
