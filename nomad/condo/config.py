# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The condo reports' settings, from report.toml: everything one institution
might do differently from another.

    [report.labs]                          # how the reports go out
    mail_domain = "example.edu"            # owner's address: NETID@mail_domain
    send_from = "Research Computing <rc@example.edu>"
    review_to = ["rc@example.edu"]         # while set, every report goes here
    summary_to = ["rc@example.edu"]        # the summary (default: review_to)
    name_members = true                    # an owner's report names their lab's members
    formats = ["html", "pdf", "json"]
    owner_patterns = ["{name}", "{name}lab", "{name}_lab"]
    contact = "Questions: rc@example.edu"
    out_dir = "/var/lib/nomad/reports/condos"
    institution = "Research Computing"     # the name at the top of each report

    [report.policy]                        # what the reports check against
    owner_start_minutes = 5
    guest_max_days = 10
    guest_grace_minutes = 5
    home_quota_gb = { soft = 50, hard = 60 }
    lab_scratch_quota_tb = { soft = 20, hard = 24 }
    scratch_path = "/scratch/{lab}"
    use_below = 0.25

    [report.clusters.c1]
    label = "Cluster 1"                    # how the reports name the cluster
    guest_partitions = ["condos"]          # default: partitions named like "condo"

    [report.clusters.c1.condos.smith]      # drafted by `nomad usage-report condos init`
    owner = "jsmith"
    partition = "smith"                    # default: the table's name
    nodes = "c[01-04]"                     # default: the partition's nodes
    members = "group:smith$"               # or "account:smith", or a list
    email = "jsmith@example.edu"           # default: owner@mail_domain
    cc = ["manager@example.edu"]
    confirmed = true                       # init writes false: reports wait for it
    lab_scratch_quota_tb = 30              # an agreed larger quota

Nothing is required. Without [report.labs] the reports are written but not
sent; without a condos table for a cluster, `nomad usage-report condos`
finds its condos from the nodes and lists them as not yet confirmed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from nomad.hostlist import expand_hostlist

FORMATS = ("html", "pdf", "json", "md")
DEFAULT_PATTERNS = ("{name}", "{name}lab", "{name}_lab", "{name}-lab")
_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class CondoConfigError(ValueError):
    """report.toml's condo settings can't be used: what is wrong, in words."""


@dataclass
class Quota:
    soft: float | None = None
    hard: float | None = None

    def as_dict(self) -> dict:
        return {"soft": self.soft, "hard": self.hard}


@dataclass
class Policy:
    owner_start_minutes: float = 5.0
    guest_max_days: float = 10.0
    guest_grace_minutes: float = 5.0
    home_quota_gb: Quota = field(default_factory=lambda: Quota(50, 60))
    lab_scratch_quota_tb: Quota = field(default_factory=lambda: Quota(20, 24))
    scratch_path: str = "/scratch/{lab}"
    use_below: float = 0.25

    def as_dict(self) -> dict:
        return {"owner_start_minutes": self.owner_start_minutes,
                "guest_max_days": self.guest_max_days,
                "guest_grace_minutes": self.guest_grace_minutes,
                "home_quota_gb": self.home_quota_gb.as_dict(),
                "lab_scratch_quota_tb": self.lab_scratch_quota_tb.as_dict(),
                "scratch_path": self.scratch_path, "use_below": self.use_below}


@dataclass
class Delivery:
    mail_domain: str | None = None
    send_from: str | None = None
    review_to: list[str] = field(default_factory=list)
    summary_to: list[str] = field(default_factory=list)
    name_members: bool = True
    formats: list[str] = field(default_factory=lambda: ["html", "pdf", "json"])
    owner_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    contact: str | None = None
    out_dir: Path | None = None
    institution: str = "Research Computing"

    @property
    def reviewing(self) -> bool:
        return bool(self.review_to)

    def summary_recipients(self) -> list[str]:
        return list(self.summary_to or self.review_to)


@dataclass
class Condo:
    name: str
    cluster: str
    owner: str | None = None
    partition: str | None = None
    nodes: list[str] = field(default_factory=list)       # empty: the partition's nodes
    members: str | list[str] | None = None
    email: str | None = None
    cc: list[str] = field(default_factory=list)
    confirmed: bool = True
    account: str | None = None
    label: str | None = None
    lab_scratch_quota_tb: Quota | None = None

    @property
    def part(self) -> str:
        return self.partition or self.name

    def address(self, delivery: Delivery) -> str | None:
        if self.email:
            return self.email
        if self.owner and delivery.mail_domain:
            return f"{self.owner}@{delivery.mail_domain}"
        return None


@dataclass
class ClusterCondos:
    name: str
    label: str | None = None
    guest_partitions: list[str] = field(default_factory=list)
    condos: dict[str, Condo] = field(default_factory=dict)
    configured: bool = False       # has a condos table (even an empty one)

    @property
    def shown(self) -> str:
        return self.label or self.name


@dataclass
class Settings:
    path: Path | None = None
    delivery: Delivery = field(default_factory=Delivery)
    policy: Policy = field(default_factory=Policy)
    clusters: dict[str, ClusterCondos] = field(default_factory=dict)

    def cluster(self, name: str) -> ClusterCondos:
        return self.clusters.get(name) or ClusterCondos(name=name)


# --- reading ---------------------------------------------------------------------

_LABS_KEYS = ("mail_domain", "send_from", "review_to", "summary_to", "name_members", "formats",
              "owner_patterns", "contact", "out_dir", "institution")
_POLICY_KEYS = ("owner_start_minutes", "guest_max_days", "guest_grace_minutes", "home_quota_gb",
                "lab_scratch_quota_tb", "scratch_path", "use_below")
_CONDO_KEYS = ("owner", "partition", "nodes", "members", "email", "cc", "confirmed", "account", "label",
               "lab_scratch_quota_tb")


def _known(d: dict, keys, what: str) -> None:
    """A key nobody reads is a typo until shown otherwise: `reveiw_to`
    would quietly send reports to owners, `confirmd = false` leave a guess
    confirmed."""
    import difflib
    for k in d:
        if k not in keys:
            near = difflib.get_close_matches(str(k), keys, n=1)
            raise CondoConfigError(f"{what}: unknown setting {k!r}"
                                   + (f" (did you mean {near[0]!r}?)" if near else
                                      f" (settings: {', '.join(keys)})"))


def _str_list(value, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CondoConfigError(f"{what} must be a list of text")
    return [v.strip() for v in value if v.strip()]


def _addresses(value, what: str) -> list[str]:
    out = _str_list(value, what)
    for a in out:
        bare = a.rsplit("<", 1)[-1].rstrip(">").strip()
        if not _ADDRESS.match(bare):
            raise CondoConfigError(f"{what}: {a!r} is not an email address")
    return out


def _number(value, what: str, lo: float = 0, hi: float = 1e9) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CondoConfigError(f"{what} must be a number")
    if not lo <= value <= hi:
        raise CondoConfigError(f"{what} must be between {lo:g} and {hi:g}")
    return float(value)


def _quota(value, what: str, base: Quota | None = None) -> Quota:
    """``{ soft = 20, hard = 24 }`` or a single number (soft and hard alike)."""
    if value is None:
        return base or Quota()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = _number(value, what)
        return Quota(n, n)
    if not isinstance(value, dict):
        raise CondoConfigError(f"{what} must be a number or {{ soft = N, hard = N }}")
    unknown = set(value) - {"soft", "hard"}
    if unknown:
        raise CondoConfigError(f"{what}: unknown {', '.join(sorted(unknown))} (soft, hard)")
    q = Quota(*(None if value.get(k) is None else _number(value[k], f"{what}.{k}")
                for k in ("soft", "hard")))
    if q.soft is not None and q.hard is not None and q.soft > q.hard:
        raise CondoConfigError(f"{what}: soft is above hard")
    return q


def _patterns(value) -> list[str]:
    pats = _str_list(value, "owner_patterns") if value is not None else list(DEFAULT_PATTERNS)
    for p in pats:
        if p.count("{name}") != 1:
            raise CondoConfigError(f"owner_patterns: {p!r} must hold {{name}} once")
    return pats


def _delivery(d: dict) -> Delivery:
    if not isinstance(d, dict):
        raise CondoConfigError("[report.labs] must be a table")
    _known(d, _LABS_KEYS, "[report.labs]")
    out = Delivery()
    dom = d.get("mail_domain")
    if dom is not None:
        if not isinstance(dom, str) or not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom.strip()):
            raise CondoConfigError("mail_domain must be a domain, like example.edu")
        out.mail_domain = dom.strip()
    if d.get("send_from") is not None:
        sender = d["send_from"]
        if not isinstance(sender, str):
            raise CondoConfigError("send_from must be one address, like \"Research Computing <rc@example.edu>\"")
        if sender.strip():
            out.send_from = _addresses([sender], "send_from")[0]
    out.review_to = _addresses(d.get("review_to"), "review_to")
    out.summary_to = _addresses(d.get("summary_to"), "summary_to")
    if "name_members" in d:
        if not isinstance(d["name_members"], bool):
            raise CondoConfigError("name_members must be true or false")
        out.name_members = d["name_members"]
    if d.get("formats") is not None:
        fm = _str_list(d["formats"], "formats")
        bad = [f for f in fm if f not in FORMATS]
        if bad:
            raise CondoConfigError(f"formats: {', '.join(bad)} (choose from {', '.join(FORMATS)})")
        out.formats = fm or ["html"]
    out.owner_patterns = _patterns(d.get("owner_patterns"))
    if d.get("contact") is not None:
        out.contact = str(d["contact"]).strip() or None
    if d.get("out_dir"):
        out.out_dir = Path(str(d["out_dir"])).expanduser()
    if d.get("institution"):
        out.institution = str(d["institution"]).strip()
    return out


def _policy(d: dict) -> Policy:
    if not isinstance(d, dict):
        raise CondoConfigError("[report.policy] must be a table")
    _known(d, _POLICY_KEYS, "[report.policy]")
    p = Policy()
    for key, hi in (("owner_start_minutes", 1440), ("guest_max_days", 365),
                    ("guest_grace_minutes", 1440)):
        if d.get(key) is not None:
            setattr(p, key, _number(d[key], key, 0, hi))
    p.home_quota_gb = _quota(d.get("home_quota_gb"), "home_quota_gb", p.home_quota_gb)
    p.lab_scratch_quota_tb = _quota(d.get("lab_scratch_quota_tb"), "lab_scratch_quota_tb",
                                    p.lab_scratch_quota_tb)
    if d.get("scratch_path") is not None:
        p.scratch_path = str(d["scratch_path"])
    if d.get("use_below") is not None:
        p.use_below = _number(d["use_below"], "use_below", 0, 1)
    return p


def _members(value, what: str):
    if value is None:
        return None
    if isinstance(value, str):
        kind, _, name = value.partition(":")
        if kind not in ("group", "account") or not name.strip():
            raise CondoConfigError(f"{what}: \"group:NAME\", \"account:NAME\" or a list of NetIDs")
        return f"{kind}:{name.strip()}"
    return _str_list(value, what)


def _condo(cluster: str, name: str, d: dict) -> Condo:
    what = f"[report.clusters.{cluster}.condos.{name}]"
    if not isinstance(d, dict):
        raise CondoConfigError(f"{what} must be a table")
    _known(d, _CONDO_KEYS, what)
    c = Condo(name=str(name), cluster=cluster)
    for key in ("owner", "partition", "account", "label"):
        v = d.get(key)
        if v is not None:
            if not isinstance(v, str):
                raise CondoConfigError(f"{what} {key} must be text")
            setattr(c, key, v.strip() or None)
    if d.get("nodes"):
        c.nodes = expand_hostlist(str(d["nodes"]))
        if not c.nodes:
            raise CondoConfigError(f"{what} nodes = {d['nodes']!r} is not a node list")
    c.members = _members(d.get("members"), f"{what} members")
    email = d.get("email")
    if email is not None:
        if not isinstance(email, str):
            raise CondoConfigError(f"{what} email must be one address")
        if email.strip():
            c.email = _addresses([email], f"{what} email")[0]
    c.cc = _addresses(d.get("cc"), f"{what} cc")
    if "confirmed" in d:
        if not isinstance(d["confirmed"], bool):
            raise CondoConfigError(f"{what} confirmed must be true or false")
        c.confirmed = d["confirmed"]
    if d.get("lab_scratch_quota_tb") is not None:
        c.lab_scratch_quota_tb = _quota(d["lab_scratch_quota_tb"], f"{what} lab_scratch_quota_tb")
    return c


def from_dict(data: dict, path: Path | None = None) -> Settings:
    report = (data or {}).get("report") or {}
    if not isinstance(report, dict):
        raise CondoConfigError("[report] must be a table")
    s = Settings(path=path)
    s.delivery = _delivery(report.get("labs") or {})
    s.policy = _policy(report.get("policy") or {})
    clusters = report.get("clusters") or {}
    if not isinstance(clusters, dict):
        raise CondoConfigError("[report.clusters] must be a table")
    for cname, c in clusters.items():
        if not isinstance(c, dict):
            raise CondoConfigError(f"[report.clusters.{cname}] must be a table")
        cc = ClusterCondos(name=str(cname))
        if c.get("label") is not None:
            cc.label = str(c["label"]).strip() or None
        cc.guest_partitions = _str_list(c.get("guest_partitions"), f"[report.clusters.{cname}] guest_partitions")
        condos = c.get("condos")
        if condos is not None:
            if not isinstance(condos, dict):
                raise CondoConfigError(f"[report.clusters.{cname}.condos] must be a table")
            cc.configured = True
            for name, d in condos.items():
                cc.condos[str(name)] = _condo(str(cname), str(name), d)
            parts = {}
            for k in cc.condos.values():
                if k.part in parts:
                    raise CondoConfigError(f"[report.clusters.{cname}.condos]: {parts[k.part]} and {k.name} "
                                           f"are both partition {k.part}")
                parts[k.part] = k.name
        s.clusters[cc.name] = cc
    return s


def load(path: Path | str | None, config_target=None) -> Settings:
    """report.toml's condo settings (``path`` None: where `usage-report`
    looks). A missing default file gives the defaults."""
    from nomad.config import read_toml
    from nomad.usage.config import default_path
    explicit = path is not None
    path = Path(path).expanduser() if explicit else default_path(config_target)
    if not path.exists():
        if explicit:
            raise CondoConfigError(f"{path}: no such file")
        return Settings(path=path)
    try:
        data = read_toml(path)
    except (OSError, ValueError) as e:
        raise CondoConfigError(f"{path}: {e}") from None
    return from_dict(data, path)
