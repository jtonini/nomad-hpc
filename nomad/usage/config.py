# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""report.toml: what nomad can't know about a site by itself.

Which nodes form which tier, which tiers belong to the institution (the rest
are condo nodes, owned by labs), how partitions map to tiers where their
nodes don't say, which accounts are not people, how job names map to
applications, and the capacity assumptions behind the projection.

The file is site data (tier maps, partition and account names) and stays on
the machine that runs the report; docs/report.example.toml shows the format
with invented names. Everything is optional: without a tier map every node
is one tier, "all nodes", and the report says so.

    [report]
    exclude_users = ["root"]                 # for every cluster

    [report.clusters.c1]
    tiers = { basic = "n[01-08]", large = "n[09-10]", gpu = "g[01-02]", condo = "c[01-04]" }
    institutional_tiers = ["basic", "large", "gpu"]
    exclude_users = ["installer"]            # added to [report]'s
    gpu_accounting_start = "2026-08-13T10:48:00"
    wait_tiers = ["basic", "large"]
    partition_classes = { short = "basic" }  # where the nodes don't decide
    unlisted_partitions = "condo"            # partitions found nowhere else

    [report.clusters.c1.capacity]            # or [report.capacity] for all
    practical = 0.75
    core_weight = 1.45
    planned = [ { label = "new nodes", cores = 768, weight = 1.45 } ]

    [[report.clusters.c1.gpu_families]]      # or [[report.gpu_families]]
    name = "molecular dynamics"
    regex = "gmx|gromacs|amber"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nomad.hostlist import expand_hostlist

DEFAULT_PATH = Path.home() / ".config" / "nomad" / "report.toml"

# Partition classes besides tier names.
OVERLAY = "overlay"
CONDO = "condo"
OTHER = "other"
ALL_NODES = "all nodes"


class ReportConfigError(ValueError):
    """report.toml can't be used: what is wrong, in words."""


@dataclass
class Family:
    name: str
    regex: re.Pattern


@dataclass
class Planned:
    label: str
    cores: float
    weight: float


@dataclass
class Capacity:
    practical: float = 0.75
    core_weight: float = 1.45
    core_weight_range: tuple[float, float] = (1.3, 1.6)
    hours_per_year: float = 8760.0
    base: str = "clipped"                     # "clipped" or "whole"
    growth: dict[str, float] | None = None    # low/central/high; else from sreport
    planned: list[Planned] = field(default_factory=list)
    horizon_years: int = 10


@dataclass
class ClusterConfig:
    """Everything the report reads from report.toml for one cluster."""
    name: str
    path: Path | None = None
    tiers: dict[str, list[str]] = field(default_factory=dict)      # tier -> nodes
    institutional_tiers: list[str] = field(default_factory=list)
    cores_per_node: dict[str, int] = field(default_factory=dict)   # node -> cores
    gpus_per_node: dict[str, int] = field(default_factory=dict)    # node -> GPUs
    exclude_users: list[str] = field(default_factory=list)
    gpu_accounting_start: datetime | None = None
    wait_tiers: list[str] = field(default_factory=list)
    partition_classes: dict[str, str] = field(default_factory=dict)
    unlisted_partitions: str = OTHER
    storage_paths: list[str] = field(default_factory=list)
    gpu_families: list[Family] = field(default_factory=list)
    families: list[Family] = field(default_factory=list)
    capacity: Capacity = field(default_factory=Capacity)
    min_cell: int = 0
    has_tier_map: bool = False
    # Whether report.toml has a [report.clusters.NAME] for this cluster, and
    # which clusters it has.
    section_found: bool = False
    known_clusters: list[str] = field(default_factory=list)

    def tier_of(self, node: str) -> str | None:
        """The node's tier; None for a node outside the tier map."""
        if not self.has_tier_map:
            return ALL_NODES
        return self._tier_by_node.get(node)

    def __post_init__(self):
        self._tier_by_node = {n: t for t, nodes in self.tiers.items() for n in nodes}

    def reindex(self) -> None:
        self._tier_by_node = {n: t for t, nodes in self.tiers.items() for n in nodes}

    @property
    def institutional(self) -> set[str]:
        return set(self.institutional_tiers) if self.has_tier_map else {ALL_NODES}


def _hostmap(value, what: str, cast) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReportConfigError(f"{what} must be a table of node list = number")
    out = {}
    for hl, n in value.items():
        nodes = expand_hostlist(str(hl))
        if not nodes:
            raise ReportConfigError(f"{what}: {hl!r} is not a node list")
        try:
            num = cast(n)
        except (TypeError, ValueError):
            raise ReportConfigError(f"{what}: {hl} = {n!r} is not a number") from None
        if num < 0:
            raise ReportConfigError(f"{what}: {hl} = {n!r} is negative")
        for node in nodes:
            out[node] = num
    return out


def _families(value, what: str) -> list[Family]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReportConfigError(f"{what} must be a list of [[{what}]] tables with name and regex")
    out = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict) or not item.get("name") or not item.get("regex"):
            raise ReportConfigError(f"{what} entry {i} needs a name and a regex")
        try:
            rx = re.compile(str(item["regex"]))
        except re.error as e:
            raise ReportConfigError(f"{what} entry {i} ({item['name']}): bad regex: {e}") from None
        out.append(Family(str(item["name"]), rx))
    return out


def _when(value, what: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        t = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        raise ReportConfigError(f"{what} = {value!r} is not a date and time "
                                "(YYYY-MM-DDTHH:MM:SS)") from None
    # Times in nomad's data are local: an offset given here is converted.
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _number(value, what: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ReportConfigError(f"{what} = {value!r} is not a number") from None
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ReportConfigError(f"{what} = {value!r} is outside {lo}–{hi}")
    return x


def _capacity(value, base: Capacity | None = None) -> Capacity:
    cap = Capacity() if base is None else Capacity(**vars(base))
    if value is None:
        return cap
    if not isinstance(value, dict):
        raise ReportConfigError("capacity must be a table")
    if "practical" in value:
        cap.practical = _number(value["practical"], "capacity.practical", 0.05, 1.0)
    if "core_weight" in value:
        cap.core_weight = _number(value["core_weight"], "capacity.core_weight", 0.1, 10)
    if "core_weight_range" in value:
        r = value["core_weight_range"]
        if not isinstance(r, list) or len(r) != 2:
            raise ReportConfigError("capacity.core_weight_range must be [low, high]")
        cap.core_weight_range = (_number(r[0], "core_weight_range"), _number(r[1], "core_weight_range"))
    if "hours_per_year" in value:
        cap.hours_per_year = _number(value["hours_per_year"], "capacity.hours_per_year", 1, 8784)
    if "base" in value:
        if value["base"] not in ("clipped", "whole"):
            raise ReportConfigError('capacity.base must be "clipped" or "whole"')
        cap.base = value["base"]
    if "growth" in value:
        g = value["growth"]
        if not isinstance(g, dict) or not {"low", "central", "high"} <= set(g):
            raise ReportConfigError("capacity.growth needs low, central and high (e.g. 0.20)")
        cap.growth = {k: _number(g[k], f"capacity.growth.{k}", -0.9, 5) for k in ("low", "central", "high")}
    if "planned" in value:
        items = value["planned"]
        if not isinstance(items, list):
            raise ReportConfigError("capacity.planned must be a list of { label, cores, weight }")
        cap.planned = []
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict) or "cores" not in it:
                raise ReportConfigError(f"capacity.planned entry {i} needs cores")
            cap.planned.append(Planned(str(it.get("label") or f"planned {i}"),
                                       _number(it["cores"], f"capacity.planned[{i}].cores", 0),
                                       _number(it.get("weight", cap.core_weight),
                                               f"capacity.planned[{i}].weight", 0.1, 10)))
    if "horizon_years" in value:
        cap.horizon_years = int(_number(value["horizon_years"], "capacity.horizon_years", 1, 50))
    return cap


def _str_list(value, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if not isinstance(value, list):
        raise ReportConfigError(f"{what} must be a list")
    return [str(v) for v in value]


def load(path: Path | str | None, cluster: str) -> ClusterConfig:
    """The settings for ``cluster``. A missing file (the default path only)
    gives the defaults; a file that can't be read raises ReportConfigError."""
    from nomad.config import read_toml
    explicit = path is not None
    path = Path(path).expanduser() if path is not None else DEFAULT_PATH
    if not path.exists():
        if explicit:
            raise ReportConfigError(f"{path}: no such file")
        return ClusterConfig(name=cluster)
    try:
        data = read_toml(path)
    except (OSError, ValueError) as e:
        raise ReportConfigError(f"{path}: {e}") from None
    return from_dict(data, cluster, path)


def from_dict(data: dict, cluster: str, path: Path | None = None) -> ClusterConfig:
    report = data.get("report") or {}
    if not isinstance(report, dict):
        raise ReportConfigError("[report] must be a table")
    clusters = report.get("clusters") or {}
    c = clusters.get(cluster) or {}
    if not isinstance(c, dict):
        raise ReportConfigError(f"[report.clusters.{cluster}] must be a table")

    cfg = ClusterConfig(name=cluster, path=path)
    cfg.known_clusters = sorted(str(k) for k in clusters) if isinstance(clusters, dict) else []
    cfg.section_found = cluster in cfg.known_clusters
    tiers = c.get("tiers") or {}
    if not isinstance(tiers, dict):
        raise ReportConfigError("tiers must be a table of tier = node list")
    seen: dict[str, str] = {}
    for tier, hl in tiers.items():
        nodes = expand_hostlist(str(hl))
        if not nodes:
            raise ReportConfigError(f"tiers.{tier} = {hl!r} is not a node list")
        for n in nodes:
            if n in seen and seen[n] != tier:
                raise ReportConfigError(f"node {n} is in two tiers ({seen[n]} and {tier})")
            seen[n] = tier
        cfg.tiers[str(tier)] = nodes
    cfg.has_tier_map = bool(cfg.tiers)

    cfg.institutional_tiers = _str_list(c.get("institutional_tiers"), "institutional_tiers")
    unknown = [t for t in cfg.institutional_tiers if t not in cfg.tiers]
    if unknown:
        raise ReportConfigError(f"institutional_tiers names tiers not in tiers: {', '.join(unknown)}")
    if cfg.has_tier_map and not cfg.institutional_tiers:
        cfg.institutional_tiers = [t for t in cfg.tiers if t != CONDO]
    cfg.wait_tiers = _str_list(c.get("wait_tiers"), "wait_tiers")
    unknown = [t for t in cfg.wait_tiers if t not in cfg.tiers]
    if unknown:
        raise ReportConfigError(f"wait_tiers names tiers not in tiers: {', '.join(unknown)}")

    cfg.cores_per_node = _hostmap(c.get("cores_per_node"), "cores_per_node", int)
    cfg.gpus_per_node = _hostmap(c.get("gpus_per_node"), "gpus_per_node", int)
    cfg.exclude_users = sorted(set(_str_list(report.get("exclude_users"), "exclude_users")
                                   + _str_list(c.get("exclude_users"), "exclude_users")))
    cfg.gpu_accounting_start = _when(c.get("gpu_accounting_start"), "gpu_accounting_start")
    cfg.storage_paths = _str_list(c.get("storage_paths"), "storage_paths")

    # Partition classes: explicit table, plus the lists of the handoff's
    # format (tier_partitions / gpu_partitions / overlay_partitions).
    classes: dict[str, str] = {}
    tp = c.get("tier_partitions") or {}
    if not isinstance(tp, dict):
        raise ReportConfigError("tier_partitions must be a table of partition = tier")
    for p, t in tp.items():
        classes[str(p)] = str(t)
    gpu_tier = next((t for t in cfg.tiers if t == "gpu"), "gpu")
    for p in _str_list(c.get("gpu_partitions"), "gpu_partitions"):
        classes[p] = gpu_tier
    for p in _str_list(c.get("overlay_partitions"), "overlay_partitions"):
        classes[p] = OVERLAY
    pc = c.get("partition_classes") or {}
    if not isinstance(pc, dict):
        raise ReportConfigError("partition_classes must be a table of partition = class")
    for p, k in pc.items():
        classes[str(p)] = str(k)
    allowed = set(cfg.tiers) | {OVERLAY, CONDO, OTHER}
    bad = sorted({k for k in classes.values() if k not in allowed})
    if bad and cfg.has_tier_map:
        raise ReportConfigError(f"partition classes must be a tier, overlay, condo or other: {', '.join(bad)}")
    cfg.partition_classes = classes
    unl = str(c.get("unlisted_partitions") or OTHER)
    if unl not in allowed:
        raise ReportConfigError(f"unlisted_partitions = {unl!r}: a tier, overlay, condo or other")
    cfg.unlisted_partitions = unl

    cfg.gpu_families = _families(c.get("gpu_families", report.get("gpu_families")), "gpu_families")
    fams = c.get("families", report.get("families"))
    cfg.families = (_families(fams, "families") if fams is not None
                    else cfg.gpu_families)
    cfg.capacity = _capacity(c.get("capacity"), _capacity(report.get("capacity")))
    mc = c.get("min_cell", report.get("min_cell", 0))
    cfg.min_cell = int(_number(mc, "min_cell", 0, 1000))
    cfg.reindex()
    return cfg


__all__ = ["ClusterConfig", "Capacity", "Planned", "Family", "ReportConfigError",
           "load", "from_dict", "DEFAULT_PATH", "OVERLAY", "CONDO", "OTHER", "ALL_NODES"]
