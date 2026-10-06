# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Who may see what in the Console: ``[console.roles]`` and ``[console.labs]``.

    [console.roles]
    admin = ["jtonini"]            # everything, settings included
    operator = []                  # everything, no settings

    [console.labs]
    group_pattern = "{netid}$"     # the group a faculty member leads, by its name
    leads = { NETID = ["group$"] } # exceptions and extra groups

    [console.labs.resources."group$"]
    workstations = ["adam", "eve"] # the lab's own machines
    storage = ["sarahvaughan"]     # its storage: a server, or server:/export

Anyone the file does not name who signs in is a viewer: they see their own
work. A viewer who leads a lab also sees its members: a PI. They lead the
group named by ``group_pattern`` (when it exists) and any group listed for
them in ``leads``. An empty ``group_pattern`` (the default) derives no labs
from names, so a site that has not said how its groups work gets no lab view
rather than a wrong one. A PI also sees the workstations and storage listed
for their labs; on those machines, people outside the lab show as "another
user" (``shown_name``). Nothing in the data says who owns a machine, so they
are listed, not guessed.

The file names people. The Console's users.json keeps only what is private or
automatic: the break-glass password, and the record made at someone's first
login. For anyone named here, the file wins.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ROLES = ("admin", "operator")       # what the file can grant; anyone else is a viewer
_LABS_KEYS = ("group_pattern", "leads", "resources")
RESOURCE_KINDS = ("workstations", "storage")
OTHER_USER = "another user"


def _netid(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v or None


def _names(value, where: str, problems: list) -> list[str]:
    """A list of NetIDs or group names; a single string is taken as a list of one."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        problems.append(f"{where} should be a list of names; ignored")
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        else:
            problems.append(f"{where}: {item!r} is not a name; ignored")
    return out


@dataclass(frozen=True)
class Access:
    admins: frozenset = frozenset()
    operators: frozenset = frozenset()
    group_pattern: str = ""
    leads: dict = field(default_factory=dict)       # netid -> tuple of group names
    resources: dict = field(default_factory=dict)   # group -> {kind: tuple of names}
    problems: tuple = ()                            # what in the file was ignored, and why

    def role(self, netid) -> str | None:
        """'admin' or 'operator' when the file says so; None when it names
        no role for this person (the Console then uses its own record)."""
        n = _netid(netid)
        if n in self.admins:
            return "admin"
        if n in self.operators:
            return "operator"
        return None

    def named(self) -> set:
        """Everyone the file names: their access is the file's, not users.json's."""
        return set(self.admins) | set(self.operators) | set(self.leads)

    def lab_groups(self, netid, group_exists: Callable[[str], bool] | None = None) -> list:
        """The groups this person leads: their ``leads`` entry, then the group
        ``group_pattern`` names for them if it exists (group_exists None: assume so)."""
        n = _netid(netid)
        if not n:
            return []
        groups = list(self.leads.get(n, ()))
        if self.group_pattern:
            g = self.group_pattern.replace("{netid}", n)
            # A group the file lists resources for is one the site says exists.
            if group_exists is None or group_exists(g) or g in self.resources:
                groups.append(g)
        seen = set()
        return [g for g in groups if not (g in seen or seen.add(g))]

    def lab_resources(self, netid, group_exists: Callable[[str], bool] | None = None) -> dict:
        """The workstations and storage of the labs this person leads:
        {"workstations": set, "storage": set}. Admins and operators see every
        machine anyway; this is what a PI's view adds."""
        out = {k: set() for k in RESOURCE_KINDS}
        for g in self.lab_groups(netid, group_exists):
            for kind, names in self.resources.get(g, {}).items():
                out[kind].update(names)
        return out


def access_from(config: dict | None, log: bool = True) -> Access:
    """Read [console.roles] and [console.labs]; what is wrong is listed in
    ``problems`` (and logged, unless log=False) and left out, never guessed at."""
    console = (config or {}).get("console") or {}
    problems: list[str] = []
    if not isinstance(console, dict):
        problems.append("[console] is not a table; ignored")
        console = {}

    roles = console.get("roles") or {}
    if not isinstance(roles, dict):
        problems.append("[console.roles] is not a table; ignored")
        roles = {}
    found = {r: set() for r in ROLES}
    for key, value in roles.items():
        if key not in ROLES:
            hint = (" (a PI comes from [console.labs]: group_pattern or leads)"
                    if key.lower() in ("pi", "pis", "lead", "leads") else "")
            problems.append(f"[console.roles] {key}: not a role; roles are "
                            f"{', '.join(ROLES)}{hint}")
            continue
        for name in _names(value, f"[console.roles] {key}", problems):
            found[key].add(_netid(name))
    both = found["admin"] & found["operator"]
    for n in sorted(both):
        problems.append(f"{n} is both admin and operator: admin")
    found["operator"] -= found["admin"]

    labs = console.get("labs") or {}
    if not isinstance(labs, dict):
        problems.append("[console.labs] is not a table; ignored")
        labs = {}
    for key in labs:
        if key not in _LABS_KEYS:
            problems.append(f"[console.labs] {key}: unknown setting; ignored")
    pattern = labs.get("group_pattern", "")
    if not isinstance(pattern, str):
        problems.append("[console.labs] group_pattern should be text; ignored")
        pattern = ""
    pattern = pattern.strip()
    if pattern and "{netid}" not in pattern:
        problems.append(f"[console.labs] group_pattern {pattern!r} has no {{netid}}: it "
                        "would make one group everyone's lab; ignored")
        pattern = ""
    leads: dict[str, tuple] = {}
    raw = labs.get("leads") or {}
    if not isinstance(raw, dict):
        problems.append("[console.labs] leads should be a table (NETID = [groups]); ignored")
        raw = {}
    for who, groups in raw.items():
        n = _netid(who)
        names = _names(groups, f"[console.labs] leads.{who}", problems)
        if n and names:
            leads[n] = tuple(dict.fromkeys(leads.get(n, ()) + tuple(names)))

    resources: dict[str, dict] = {}
    raw = labs.get("resources") or {}
    if not isinstance(raw, dict):
        problems.append('[console.labs.resources] should hold one table per lab, '
                        'as [console.labs.resources."group$"]; ignored')
        raw = {}
    for group, table in raw.items():
        where = f'[console.labs.resources."{group}"]'
        if not isinstance(table, dict):
            problems.append(f"{where} should be a table (workstations = [...], "
                            "storage = [...]); ignored")
            continue
        kinds = {}
        for kind, names in table.items():
            if kind not in RESOURCE_KINDS:
                problems.append(f"{where} {kind}: unknown; the kinds are "
                                f"{', '.join(RESOURCE_KINDS)}")
                continue
            kinds[kind] = tuple(dict.fromkeys(_names(names, f"{where} {kind}", problems)))
        if any(kinds.values()):
            resources[group.strip()] = kinds

    for p in problems if log else ():
        logger.warning("nomad.toml: %s", p)
    return Access(admins=frozenset(found["admin"]), operators=frozenset(found["operator"]),
                  group_pattern=pattern, leads=leads, resources=resources,
                  problems=tuple(problems))


def members_lookup(conn) -> tuple[Callable[[str], bool], Callable[[str], set]]:
    """group_exists and members_of over a nomad database's group_membership
    (the groups collector's), every site together."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='group_membership'")}
    cache: dict[str, set] = {}

    def members_of(group: str) -> set:
        if group not in cache:
            cache[group] = ({r[0] for r in conn.execute(
                "SELECT DISTINCT username FROM group_membership WHERE group_name = ?",
                (group,))} if have else set())
        return cache[group]

    return (lambda g: bool(members_of(g))), members_of


def visible_people(access: Access, netid, members_of: Callable[[str], Iterable[str]],
                   group_exists: Callable[[str], bool] | None = None) -> set | None:
    """Whom this person may see individually: None means everyone (admin,
    operator); otherwise themselves and the members of the labs they lead."""
    if access.role(netid) in ROLES:
        return None
    n = _netid(netid)
    people = {n} if n else set()
    for g in access.lab_groups(n, group_exists):
        people |= {_netid(u) for u in members_of(g) if _netid(u)}
    return people


def shown_name(username, visible: set | None) -> str:
    """How a person appears to a viewer on a page about a machine: by name if
    the viewer may see them (visible None: everyone), else "another user"."""
    if visible is None:
        return username
    return username if _netid(username) in visible else OTHER_USER
