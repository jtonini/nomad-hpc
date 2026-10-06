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

    [console.storage."10.0.0.28"]   # a storage server, as the mounts name it
    name = "sarahvaughan"
    note = "community $HOME, all users"

Anyone the file does not name who signs in is a viewer: they see their own
work. A viewer who leads a lab also sees its members: a PI. They lead the
group named by ``group_pattern`` (when it exists) and any group listed for
them in ``leads``. An empty ``group_pattern`` (the default) derives no labs
from names, so a site that has not said how its groups work gets no lab view
rather than a wrong one. A PI also sees their labs' workstations and storage;
on those machines, people outside the lab show as "another user"
(``shown_name``). A workstation belongs to a lab when the collector that
monitors it tags it with the lab's group (``department`` in its
``[[collectors.workstation.workstations]]`` entry, which reaches the hub with
the data: ``workstation_tags``), or when it is listed here. Storage is listed
here. Nothing is guessed from names. ``[console.storage]`` gives a storage
server a name and a note, shown wherever its exports are (``servers``): a
server shared by everyone should say so, or its use reads as the lab's.

The file names people. The Console's users.json keeps only what is private or
automatic: the break-glass password, and the record made at someone's first
login. For anyone named here, the file wins.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
    servers: dict = field(default_factory=dict)     # storage server -> (name, note)
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

    def lab_groups(self, netid, group_exists: Callable[[str], bool] | None = None,
                   tagged: Callable[[str], set] | None = None) -> list:
        """The groups this person leads: their ``leads`` entry, then the group
        ``group_pattern`` names for them if it exists (group_exists None: assume
        so). A group with machines listed here or tagged with it exists."""
        n = _netid(netid)
        if not n:
            return []
        groups = list(self.leads.get(n, ()))
        if self.group_pattern:
            g = self.group_pattern.replace("{netid}", n)
            # A group the file lists resources for is one the site says exists.
            if (group_exists is None or group_exists(g) or g in self.resources
                    or (tagged is not None and tagged(g))):
                groups.append(g)
        seen = set()
        return [g for g in groups if not (g in seen or seen.add(g))]

    def lab_resources(self, netid, group_exists: Callable[[str], bool] | None = None,
                      tagged: Callable[[str], set] | None = None) -> dict:
        """The workstations and storage of the labs this person leads:
        {"workstations": set, "storage": set}, from what is listed here and the
        workstations tagged with each lab (``tagged``: group -> hostnames).
        Admins and operators see every machine anyway; this is what a PI's
        view adds."""
        out = {k: set() for k in RESOURCE_KINDS}
        for g in self.lab_groups(netid, group_exists, tagged):
            for kind, names in self.resources.get(g, {}).items():
                out[kind].update(names)
            if tagged is not None:
                out["workstations"].update(tagged(g))
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

    servers = _servers(console.get("storage"), problems)

    for p in problems if log else ():
        logger.warning("nomad.toml: %s", p)
    return Access(admins=frozenset(found["admin"]), operators=frozenset(found["operator"]),
                  group_pattern=pattern, leads=leads, resources=resources,
                  servers=servers, problems=tuple(problems))


_SERVER_KEYS = ("name", "note")


def _servers(raw, problems: list) -> dict:
    """[console.storage."SERVER"] name = ..., note = ...: SERVER -> (name, note).
    SERVER is the server as the mounts name it (``server`` in server:/export)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append('[console.storage] should hold one table per storage server, '
                        'as [console.storage."ADDRESS"]; ignored')
        return {}
    servers = {}
    for server, table in raw.items():
        where = f'[console.storage."{server}"]'
        s = server.strip()
        if not s or ":" in s:
            problems.append(f"{where}: a storage server, as the mounts name it (the part "
                            "before the colon in server:/export); ignored")
            continue
        if not isinstance(table, dict):
            problems.append(f'{where} should be a table (name = "...", note = "..."); ignored')
            continue
        values = {}
        for key, value in table.items():
            if key not in _SERVER_KEYS:
                problems.append(f"{where} {key}: unknown; the settings are "
                                f"{', '.join(_SERVER_KEYS)}")
            elif not isinstance(value, str):
                problems.append(f"{where} {key} should be text; ignored")
            else:
                values[key] = " ".join(value.split())
        if values.get("name") or values.get("note"):
            servers[s] = (values.get("name", ""), values.get("note", ""))
    return servers


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


def workstation_tags(conn) -> Callable[[str], set]:
    """group -> the workstations whose collector tags them with it: the
    ``department`` of each machine's latest record (a machine moved to another
    lab follows its latest tag)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workstation_state)")}
    latest: dict[str, tuple] = {}
    if {"hostname", "timestamp", "department"} <= cols:
        # One pass: a per-host subquery over months of records is far slower.
        for host, when, dept in conn.execute(
                "SELECT hostname, timestamp, department FROM workstation_state"):
            if host not in latest or str(when) > latest[host][0]:
                latest[host] = (str(when), dept)
    by_group: dict[str, set] = {}
    for host, (_, dept) in latest.items():
        if isinstance(dept, str) and dept.strip():
            by_group.setdefault(dept.strip(), set()).add(host)
    return lambda group: set(by_group.get(group, ()))


@dataclass(frozen=True)
class ExportSize:
    """An export's size as a lab machine mounting it last read it (the mount
    probe's statvfs(), df's numbers), in bytes. ``used`` is the whole
    export's, whoever wrote it. ``shares_free_with``: other exports of the
    same server read with the same free space in the same run -- most
    likely datasets of one pool, whose free space is one and must not be
    added up."""
    when: str
    host: str
    total: int
    used: int
    avail: int
    shares_free_with: tuple = ()

    @property
    def used_pct(self) -> int | None:
        """Percent used as df shows it: used / (used + available), rounded up."""
        return _pct(self.used, self.avail)


def _pct(used: int, avail: int) -> int | None:
    whole = used + avail
    return None if whole <= 0 else -(-used * 100 // whole)


@dataclass(frozen=True)
class SharedFree:
    """Exports that share their free space, as one collection run (``when``)
    read them all: together they use ``used`` and have ``avail`` left."""
    exports: tuple
    used: int
    avail: int
    when: str

    @property
    def used_pct(self) -> int | None:
        return _pct(self.used, self.avail)


_SIZE_COLUMNS = {"source", "timestamp", "hostname", "is_responsive",
                 "total_bytes", "used_bytes", "avail_bytes"}
_SIZED = ("total_bytes IS NOT NULL AND used_bytes IS NOT NULL AND avail_bytes IS NOT NULL "
          "AND is_responsive = 1")
# The readings of one run, made by the lab machines seconds apart, see a busy
# pool's free space move a little: 0.1%, at most 1 GiB. Separate pools rarely
# come this close; when they do, the free space is counted once -- less than
# there is, never more.
SHARED_FREE_TOLERANCE = 0.001
SHARED_FREE_MAX_GAP = 2 ** 30


def _same_free(a, b) -> bool:
    return (a > 0 and b > 0 and
            abs(a - b) <= min(max(a, b) * SHARED_FREE_TOLERANCE, SHARED_FREE_MAX_GAP))


def export_sizes(conn, names: Iterable[str]) -> dict[str, ExportSize]:
    """name -> its latest size, for each "server:/export" among ``names`` that
    has one. A server named alone has no single size; a database from before
    the sizes were collected (1.7.24) gives none.

    Exports of one server count as sharing their free space when the same
    free space (within SHARED_FREE_TOLERANCE) was read for them in one run,
    at the time of either one's latest size -- by one machine or by two: an
    export may be mounted on a single machine, and no other machine then
    reads it with the rest. Sharing holds both ways and chains (A with B and
    B with C puts all three together).
    """
    wanted = sorted({n for n in names if isinstance(n, str) and n.partition(":")[2]})
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workstation_mount_state)")}
    if not wanted or not _SIZE_COLUMNS <= cols:
        return {}
    latest: dict[str, tuple] = {}
    for source in wanted:
        # One record per export, the latest (ties: by machine name); the
        # hub indexes (source, timestamp) for this.
        row = conn.execute(
            "SELECT timestamp, hostname, total_bytes, used_bytes, avail_bytes "
            f"FROM workstation_mount_state WHERE source = ? AND {_SIZED} "
            "ORDER BY timestamp DESC, hostname LIMIT 1", (source,)).fetchone()
        if row:
            latest[source] = (str(row[0]),) + tuple(row[1:])

    parent = {s: s for s in latest}

    def top(s):
        while parent[s] != s:
            s = parent[s]
        return s

    by_server: dict[str, list] = {}
    for source in latest:
        by_server.setdefault(source.partition(":")[0], []).append(source)
    for group in by_server.values():
        if len(group) < 2:
            continue
        marks = ", ".join("?" * len(group))
        for when in sorted({latest[s][0] for s in group}):
            read: dict[str, set] = {}       # export -> the free space read, by any machine
            for source, avail in conn.execute(
                    "SELECT source, avail_bytes FROM workstation_mount_state "
                    f"WHERE timestamp = ? AND source IN ({marks}) AND {_SIZED}",
                    (when, *group)):
                read.setdefault(source, set()).add(avail)
            found = sorted(read)
            for i, a in enumerate(found):
                for b in found[i + 1:]:
                    if any(_same_free(x, y) for x in read[a] for y in read[b]):
                        parent[top(a)] = top(b)

    members: dict[str, list] = {}
    for source in latest:
        members.setdefault(top(source), []).append(source)
    return {source: ExportSize(when, host, total, used, avail,
                               tuple(sorted(set(members[top(source)]) - {source})))
            for source, (when, host, total, used, avail) in latest.items()}


def _one_run(conn, exports: tuple, upto: str):
    """(timestamp, {export: (total, used, avail)}) of the latest run, at or
    before ``upto`` and within a day of it, that read all of ``exports``;
    None when there is none. Each export's figures come, where it can, from
    the machine that read most of them (its reads were a moment apart)."""
    marks = ", ".join("?" * len(exports))
    when = upto
    found = conn.execute(
        "SELECT COUNT(DISTINCT source) FROM workstation_mount_state "
        f"WHERE timestamp = ? AND source IN ({marks}) AND {_SIZED}",
        (upto, *exports)).fetchone()[0]
    if found < len(exports):
        try:
            since = (datetime.fromisoformat(upto[:19]) - timedelta(days=1)).isoformat()
        except ValueError:
            return None
        row = conn.execute(
            "SELECT timestamp FROM workstation_mount_state "
            f"WHERE source IN ({marks}) AND {_SIZED} AND timestamp <= ? AND timestamp >= ? "
            "GROUP BY timestamp HAVING COUNT(DISTINCT source) = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (*exports, upto, since, len(exports))).fetchone()
        if row is None:
            return None
        when = str(row[0])
    by_host: dict[str, dict] = {}
    for host, source, total, used, avail in conn.execute(
            "SELECT hostname, source, total_bytes, used_bytes, avail_bytes "
            "FROM workstation_mount_state "
            f"WHERE timestamp = ? AND source IN ({marks}) AND {_SIZED}",
            (when, *exports)):
        by_host.setdefault(host, {}).setdefault(source, (total, used, avail))
    hosts = sorted(by_host, key=lambda h: (-len(by_host[h]), h))
    figures = {e: next(by_host[h][e] for h in hosts if e in by_host[h]) for e in exports}
    return when, figures


def shared_free(conn, sizes: dict[str, ExportSize]) -> list[SharedFree]:
    """The groups of exports that share their free space, each once, with
    their space together as one run read them all (the latest such run, at
    or before the oldest of their latest readings): used space summed, and
    the free space counted once (the least read). Exports read with exactly
    the same figures are one filesystem (a directory of an export, listed
    too) and count once. A group no run read in full gets no line."""
    groups, done = [], set()
    for source in sorted(sizes):
        size = sizes[source]
        if not size.shares_free_with or source in done:
            continue
        exports = tuple(sorted((source,) + size.shares_free_with))
        done.update(exports)
        reading = _one_run(conn, exports, min(sizes[e].when for e in exports))
        if reading is None:
            continue
        when, figures = reading
        distinct = set(figures.values())
        groups.append(SharedFree(exports, sum(used for _, used, _ in distinct),
                                 min(avail for _, _, avail in distinct), when))
    return groups


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
