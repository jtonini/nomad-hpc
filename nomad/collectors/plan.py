# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Which collectors run on this host, and why -- one answer for every caller.

``nomad collect`` used to build its own fixed list. Two collectors that exist
(storage, network_perf) were never on it, so enabling them in nomad.toml did
nothing, silently. ``nomad collectors`` needs the same answer to explain it.
Both now ask :func:`plan`.

What decides whether a collector runs is its own table's ``enabled``:

    [collectors.nfs]
    enabled = false

The list ``[collectors] enabled = [...]`` that older configs carry is not
read; :func:`plan` reports it so nobody is misled by it.
"""
from __future__ import annotations

import importlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Spec:
    name: str
    module: str          # nomad.collectors.<module>
    cls: str
    default_on: bool
    where: str           # where it belongs, in a few words
    commands: tuple = () # commands it needs on this host (any one of a group "a|b")
    modules: tuple = ()  # Python modules it needs
    tables: tuple = ()
    log_name: str = ""   # name its runs are logged under, if not `name`

    @property
    def logged_as(self) -> str:
        return self.log_name or self.name


SPECS: tuple[Spec, ...] = (
    Spec("disk", "disk", "DiskCollector", True, "every host",
         ("df",), (), ("filesystems",)),
    Spec("slurm", "slurm", "SlurmCollector", True, "Slurm head node",
         ("squeue", "sacct"), (), ("queue_state", "jobs")),
    Spec("job_metrics", "job_metrics", "JobMetricsCollector", True, "Slurm head node",
         ("sacct",), (), ("jobs", "job_summary")),
    Spec("iostat", "iostat", "IOStatCollector", True, "every host (sysstat)",
         ("iostat",), (), ("iostat_device", "iostat_cpu")),
    Spec("mpstat", "mpstat", "MPStatCollector", True, "every host (sysstat)",
         ("mpstat",), (), ("mpstat_summary", "mpstat_core")),
    Spec("vmstat", "vmstat", "VMStatCollector", True, "every host",
         ("vmstat",), (), ("vmstat",)),
    Spec("node_state", "node_state", "NodeStateCollector", True, "Slurm head node",
         ("scontrol",), (), ("node_state",)),
    Spec("gpu", "gpu", "GPUCollector", True, "hosts with NVIDIA GPUs, or a head node "
         "reaching GPU nodes over SSH", ("nvidia-smi|ssh",), (), ("gpu_stats", "gpu_health")),
    Spec("nfs", "nfs", "NFSCollector", True, "hosts that mount NFS (client side)",
         ("nfsiostat",), (), ("nfs_stats",)),
    Spec("groups", "groups", "GroupCollector", True, "every site (membership); "
         "accounting needs Slurm", ("getent",), (), ("group_membership", "job_accounting")),
    Spec("interactive", "interactive", "InteractiveCollector", False,
         "hosts running RStudio or Jupyter", (), (),
         ("interactive_sessions", "interactive_summary")),
    Spec("workstation", "workstation", "WorkstationCollector", False,
         "a hub that reaches workstations over SSH", ("ssh",), (),
         ("workstation_state", "workstation_user_snapshot", "workstation_mount_state")),
    Spec("per_user", "per_user", "PerUserCollector", False,
         "login nodes and shared interactive hosts", (), ("psutil",),
         ("per_user_sample", "per_user_alert")),
    Spec("storage", "storage", "StorageCollector", False,
         "a host that reaches ZFS/NFS servers over SSH", (), (), ("storage_state",)),
    Spec("network_perf", "network_perf", "NetworkPerfCollector", False,
         "any host; tests paths that start here", ("ping",), (), ("network_perf",)),
    Spec("cloud", "cloud.aws", "AWSCollector", False, "sites with AWS resources",
         (), ("boto3",), ("cloud_metrics",), log_name="aws"),
)
NAMES = tuple(s.name for s in SPECS)


@dataclass
class Planned:
    spec: Spec
    enabled: bool
    why: str                       # how the decision was made
    config: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.spec.name

    def missing(self) -> list[str]:
        """Commands and modules this collector needs that this host lacks."""
        out = []
        from nomad.collectors.base import find_tool
        for c in self.spec.commands:
            if not any(find_tool(alt) for alt in c.split("|")):
                out.append(c.replace("|", " or "))
        for m in self.spec.modules:
            try:
                importlib.import_module(m)
            except ImportError:
                out.append(f"Python module {m}")
        return out


def _section(config: dict, name: str) -> tuple[dict, str]:
    """A collector's settings and where they came from."""
    collectors = config.get("collectors", {}) or {}
    if name == "interactive" and config.get("interactive"):
        return dict(config["interactive"]), "[interactive]"
    if name == "cloud":
        return dict((collectors.get("cloud", {}) or {}).get("aws", {}) or {}), \
            "[collectors.cloud.aws]"
    return dict(collectors.get(name, {}) or {}), f"[collectors.{name}]"


# Lists the example config used to put at the top level, where the
# collectors never looked. Read from either place; the old one warns.
_MOVED_LISTS = {"storage": "storage_devices", "network_perf": "network_tests"}


def plan(config: dict) -> list[Planned]:
    """Every collector nomad has, whether it runs here, and why."""
    from nomad.config import resolve_cluster_name
    out = []
    for spec in SPECS:
        cfg, where = _section(config, spec.name)
        warnings = []
        if "enabled" in cfg:
            enabled = bool(cfg["enabled"])
            why = f"{'enabled' if enabled else 'disabled'} in {where}"
        else:
            enabled = spec.default_on
            why = "on by default" if enabled else f"off unless enabled in {where}"

        key = _MOVED_LISTS.get(spec.name)
        if key and not cfg.get(key) and config.get(key):
            cfg[key] = config[key]
            warnings.append(f"[[{key}]] is at the top level of nomad.toml; move it to "
                            f"[[collectors.{spec.name}.{key}]]")
        if spec.name == "node_state" and "cluster_name" not in cfg:
            cfg["cluster_name"] = resolve_cluster_name(config)
        if spec.name == "groups":
            cfg["clusters"] = config.get("clusters", {})
            cfg.setdefault("local_name", resolve_cluster_name(config))
        out.append(Planned(spec, enabled, why, cfg, warnings))
    return out


def unused_enabled_list(config: dict) -> list | None:
    """The ``[collectors] enabled = [...]`` list, which nothing reads."""
    value = (config.get("collectors", {}) or {}).get("enabled")
    return value if isinstance(value, list) else None


def parse_only(only) -> set[str]:
    """``-C disk -C nfs`` or ``-C disk,nfs``.

    An unknown name is warned about and dropped, as before (a cron line
    with one stale name must not stop collection); only when no name is
    known is it an error.
    """
    names = {n.strip() for item in (only or ()) for n in str(item).split(",") if n.strip()}
    if "aws" in names:
        names = (names - {"aws"}) | {"cloud"}
    unknown = sorted(names - set(NAMES))
    if unknown:
        msg = f"unknown collector(s): {', '.join(unknown)}; known: {', '.join(NAMES)}"
        if unknown == sorted(names):
            raise ValueError(msg)
        logger.warning(msg)
    return names & set(NAMES)


def build(config: dict, db_path: Path, only=()) -> tuple[list, list[Planned]]:
    """Instantiate the collectors that run here (``only`` narrows them)."""
    wanted = parse_only(only)
    collectors, planned = [], plan(config)
    for p in planned:
        if wanted and p.name not in wanted:
            continue
        if not p.enabled:
            continue
        for w in p.warnings:
            logger.warning(f"{p.name}: {w}")
        try:
            mod = importlib.import_module(f"nomad.collectors.{p.spec.module}")
            cls = getattr(mod, p.spec.cls)
        except (ImportError, AttributeError) as e:
            logger.warning(f"{p.name}: cannot load ({e})")
            continue
        if p.name == "cloud":
            collectors.append(cls(p.config, db_path=str(db_path)))
        else:
            collectors.append(cls(p.config, db_path))
    return collectors, planned
