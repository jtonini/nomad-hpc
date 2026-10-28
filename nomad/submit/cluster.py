# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The partitions a job could go to: their limits, their biggest node, and
whether the person may use them.

Read live on a login node (``live``: ``scontrol show partition -o``,
``sinfo``, the person's groups and Slurm accounts), or from nomad's database
on the hub (``from_db``: the partitions collector's rows and the nodes'
latest readings). Both give ``Partition``s.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

from nomad import partitions as _parts


@dataclass
class Partition:
    name: str
    state: str | None = None
    default: bool = False
    max_time_s: int | None = None        # None: no limit (or not said)
    unlimited: bool = False
    default_time_s: int | None = None
    max_mem_per_node_mb: int | None = None
    max_mem_per_cpu_mb: int | None = None
    def_mem_per_cpu_mb: int | None = None
    def_mem_per_node_mb: int | None = None
    max_cpus_per_node: int | None = None
    max_nodes: int | None = None
    node_mem_mb: int | None = None       # the biggest node's memory
    node_cpus: int | None = None         # the biggest node's CPUs
    node_gpus: int | None = None         # the most GPUs one node has
    shapes: list = field(default_factory=list)   # (memory MB, CPUs, GPUs) of each kind of node
    counts: dict = field(default_factory=dict)   # shape -> how many nodes of it
    nodes: int | None = None
    total_cpus: int | None = None
    total_gpus: int | None = None        # GPUs on all its nodes (None: not read)
    guest: bool = False                  # its jobs can be preempted by another partition's
    allow_accounts: list = field(default_factory=list)   # empty: anyone
    deny_accounts: list = field(default_factory=list)
    allow_groups: list = field(default_factory=list)
    allow_qos: list = field(default_factory=list)

    @property
    def up(self) -> bool:
        return (self.state or "UP").upper() == "UP"

    def time_default_s(self) -> int | None:
        """The time limit a job that asks none gets: DefaultTime, else
        MaxTime (None: no limit, or not known)."""
        return self.default_time_s or self.max_time_s

    def mem_default_mb(self, cpus_per_node: int | None) -> int | None:
        """The memory a node of a job that asks none gets: DefMemPerNode,
        or DefMemPerCPU × its CPUs (None: not known; Slurm's own default is
        then all of a node's)."""
        if self.def_mem_per_node_mb:
            return self.def_mem_per_node_mb
        if self.def_mem_per_cpu_mb:
            return self.def_mem_per_cpu_mb * (cpus_per_node or 1)
        return None

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["up"] = self.up
        return out


def _names(value) -> list:
    v = str(value or "").strip()
    if not v or v.upper() in ("ALL", "(NULL)", "NONE"):
        return []
    return [x for x in v.split(",") if x]


def _from_row(r: dict, rows: list) -> Partition:
    return Partition(
        name=r["partition"], state=r.get("state"), default=bool(r.get("is_default")),
        max_time_s=(r["max_time_min"] * 60) if r.get("max_time_min") else None,
        unlimited=bool(r.get("max_time_unlimited")),
        default_time_s=(r["default_time_min"] * 60) if r.get("default_time_min") else None,
        max_mem_per_node_mb=r.get("max_mem_per_node_mb"), max_mem_per_cpu_mb=r.get("max_mem_per_cpu_mb"),
        def_mem_per_cpu_mb=r.get("def_mem_per_cpu_mb"), def_mem_per_node_mb=r.get("def_mem_per_node_mb"),
        max_cpus_per_node=r.get("max_cpus_per_node"), max_nodes=r.get("max_nodes"),
        nodes=r.get("total_nodes"), total_cpus=r.get("total_cpus"), guest=_parts.is_guest(r, rows),
        allow_accounts=_names(r.get("allow_accounts")), deny_accounts=_names(r.get("deny_accounts")),
        allow_groups=_names(r.get("allow_groups")), allow_qos=_names(r.get("allow_qos")))


def gres_gpus(gres) -> int:
    """GPUs in a node's gres ('gpu:a100:4(S:0-1)', 'gpu:2,mps:100')."""
    total = 0
    for part in str(gres or "").split(","):
        bits = re.sub(r"\(.*?\)", "", part.strip()).split(":")
        if bits and bits[0] == "gpu":
            n = bits[-1] if len(bits) > 1 else "1"
            total += int(n) if n.isdigit() else 1
    return total


def _add_shape(p: Partition, mem, cpus, gpus, count: int = 1) -> None:
    mem = int(mem) if mem else None
    cpus = int(cpus) if cpus else None
    shape = (mem, cpus, gpus or 0)
    if shape not in p.shapes:
        p.shapes.append(shape)
    p.counts[shape] = p.counts.get(shape, 0) + max(1, count)
    if mem:
        p.node_mem_mb = max(p.node_mem_mb or 0, mem)
    if cpus:
        p.node_cpus = max(p.node_cpus or 0, cpus)
    p.node_gpus = max(p.node_gpus or 0, gpus or 0)
    p.total_gpus = (p.total_gpus or 0) + (gpus or 0) * max(1, count)


def _run(argv: list, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def live(run=_run) -> list[Partition]:
    """This cluster's partitions as Slurm has them now (empty when scontrol
    can't be run here)."""
    try:
        # --all: hidden partitions too (a condo's, say), which a person may still use
        r = run(["scontrol", "--all", "show", "partition", "-o"])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    rows = _parts.parse(r.stdout)
    out = {p.name: p for p in (_from_row(x, rows) for x in rows)}
    try:
        # -a: hidden partitions too (sinfo leaves them out otherwise); -e: each
        # kind of node on its own line (else sinfo puts a partition's nodes on
        # one, with the smallest's memory and CPUs and a '+'); %D: how many
        # nodes of each kind, for the GPUs a partition has in all
        s = run(["sinfo", "-a", "-e", "-h", "-o", "%R|%m|%c|%G|%D"])
    except (OSError, subprocess.TimeoutExpired):
        s = None
    if s is not None and s.returncode == 0:
        for line in (s.stdout or "").splitlines():
            bits = line.strip().split("|")
            if len(bits) < 4 or bits[0] not in out:
                continue
            mem = bits[1].rstrip("+")
            cpus = bits[2].rstrip("+")
            count = bits[4].strip() if len(bits) > 4 else "1"
            _add_shape(out[bits[0]], mem if mem.isdigit() else None, cpus if cpus.isdigit() else None,
                       gres_gpus(bits[3]), int(count) if count.isdigit() else 1)
    return list(out.values())


def from_db(conn, site: str | None = None) -> list[Partition]:
    """The partitions as nomad last read them (on a hub, one site's), with
    each one's biggest node from the nodes' latest readings."""
    rows = _parts.current(conn, site)
    out = {r["partition"]: _from_row(r, rows) for r in rows}
    if not out:
        return []
    cols = {c[1] for c in conn.execute("PRAGMA table_info(node_state)")}
    if not {"node_name", "timestamp", "partitions"} <= cols:
        return list(out.values())
    site_col = "source_site" if "source_site" in cols else None
    where, args = "", []
    if site is not None and site_col:
        where, args = f"WHERE {site_col} = ?", [site]
    mem = "memory_total_mb" if "memory_total_mb" in cols else "NULL"
    cpus = "cpus_total" if "cpus_total" in cols else "NULL"
    gres = "gres" if "gres" in cols else "NULL"
    sql = (f"SELECT node_name, partitions, {mem}, {cpus}, {gres}, MAX(timestamp) FROM node_state "
           f"{where} GROUP BY node_name" + (f", {site_col}" if site_col else ""))
    for _node, parts, m, c, g, _t in conn.execute(sql, args):
        for name in str(parts or "").split(","):
            p = out.get(name.strip())
            if p is None:
                continue
            _add_shape(p, m, c, gres_gpus(g))
    return list(out.values())


# ── who may use one ─────────────────────────────────────────────────────────

@dataclass
class Access:
    """A person's groups and Slurm accounts (None: not known), and the
    account a job of theirs is charged to when it names none."""
    groups: set | None = None
    accounts: set | None = None
    default_account: str | None = None


def my_access(user: str | None = None, run=_run) -> Access:
    """The person running nomad: their groups, and their Slurm accounts
    (sacctmgr; None when it can't say)."""
    groups: set = set()
    try:
        import grp                     # POSIX only: nomad itself must load elsewhere
        import pwd
        name = user or pwd.getpwuid(os.getuid()).pw_name
        gids = os.getgrouplist(name, pwd.getpwnam(name).pw_gid)
        for g in gids:
            try:
                groups.add(grp.getgrgid(g).gr_name)
            except KeyError:
                continue
    except (ImportError, KeyError, OSError, AttributeError):
        name = user
        groups = set()
    accounts = None
    default = None
    try:
        r = run(["sacctmgr", "-n", "-P", "show", "assoc", f"user={name}", "format=account"])
        if r.returncode == 0:
            accounts = {line.strip() for line in (r.stdout or "").splitlines() if line.strip()}
        r = run(["sacctmgr", "-n", "-P", "show", "user", str(name), "format=defaultaccount"])
        if r.returncode == 0:
            default = next((line.strip() for line in (r.stdout or "").splitlines() if line.strip()), None)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return Access(groups=groups or None, accounts=accounts, default_account=default)


def may_use(p: Partition, access: Access | None, account: str | None = None) -> tuple[bool | None, str]:
    """(True, "") when the person may use the partition with the account
    the job is charged to (``account``: its -A, else their default);
    (False, why) when Slurm would refuse it; (None, why) when nomad can't
    tell."""
    if access is None:
        return (None, "who may use it isn't known here") if (p.allow_accounts or p.allow_groups
                                                             or p.deny_accounts) else (True, "")
    if p.allow_groups:
        if access.groups is None:
            return None, f"only for the groups {', '.join(p.allow_groups)}"
        if not set(p.allow_groups) & access.groups:
            return False, f"only for the groups {', '.join(p.allow_groups)}"
    charged = account or access.default_account
    if p.allow_accounts:
        if charged is None:
            if access.accounts is None:
                return None, f"only for the accounts {', '.join(p.allow_accounts)}"
            if not set(p.allow_accounts) & access.accounts:
                return False, f"only for the accounts {', '.join(p.allow_accounts)}"
            return None, f"only for the accounts {', '.join(p.allow_accounts)}: give one with --account"
        if charged not in p.allow_accounts:
            yours = sorted(set(p.allow_accounts) & (access.accounts or set()))
            return False, (f"only for the accounts {', '.join(p.allow_accounts)}"
                           + (f" (yours: add --account={yours[0]})" if yours else ""))
    if p.deny_accounts and charged and charged in p.deny_accounts:
        return False, f"the account {charged} may not use it"
    return True, ""


def extra_is_constraints(run=_run) -> bool | None:
    """Whether Slurm reads a job's --extra as node constraints here
    (SchedulerParameters has extra_constraints): then nomad doesn't put its
    tag there. None when it can't be told."""
    try:
        r = run(["scontrol", "show", "config"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "SchedulerParameters":
            return "extra_constraints" in value
    return False


def slurm_version(run=_run) -> tuple | None:
    """Slurm's version here, as (major, minor): (25, 11) from 'slurm
    25.11.5'; None when sbatch can't say."""
    try:
        r = run(["sbatch", "--version"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"([0-9]+)\.([0-9]+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if (r.returncode == 0 and m) else None
