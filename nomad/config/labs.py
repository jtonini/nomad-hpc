# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad lab`: a lab's machines and storage, in nomad.toml.

    add-machine LAB HOST   a workstation the workstation collector reaches,
                           tagged with the lab's group (department = "GROUP")
    add-nas LAB HOST       a NAS the storage collector reaches over ssh (its
                           pools' health and space), listed as the lab's
                           (LAB "shared": everyone's, listed for no lab)
    name LAB "NAME"        how the lab is shown ("Smith Lab")
    add-storage LAB SERVER[:/export]
                           storage the lab's machines mount, listed as the lab's
    lead LAB NETID         NETID leads LAB too (a co-PI, a lab manager); with
                           remove=True, no longer
    remove LAB HOST        out of all of these for that lab

A workstation's lab travels with its data (the tag), so add-machine is done
where it is collected. The listing of storage, and the storage servers'
names and notes, are read by the Console's machine (the hub). On a host
that is both, as when the hub collects a lab with no head node of its own,
one command does all of it.

Each function edits a TomlEdit and returns what it changed, in words; a
LabError says why nothing should be written.
"""
from __future__ import annotations

import re

from nomad.config.edit import TomlEdit

WS_SECTION = ("collectors", "workstation")
ST_SECTION = ("collectors", "storage")
RESOURCES = ("console", "labs", "resources")
NAMES = ("console", "storage")
LABS = ("console", "labs")
LEADS = LABS + ("leads",)


class LabError(Exception):
    pass


# The LAB of storage that is everyone's: collected, listed for no lab.
SHARED = "shared"


# A host as nomad reaches it over ssh: a name or an address, no user@ (the
# user comes from ~/.ssh/config), nothing a shell would read.
_HOST = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _host(host: str) -> str:
    host = host.strip()
    if "@" in host:
        raise LabError(f"{host}: give the host alone; to log in as another user (root, "
                       f"say), set it in ~/.ssh/config: Host {host.split('@', 1)[1]} / User "
                       f"{host.split('@', 1)[0]}")
    if not _HOST.match(host):
        raise LabError(f"{host}: not a host name or address nomad can use")
    return host


def _no_inline(edit: TomlEdit, path: tuple, what: str) -> None:
    if edit.defined_inline(path):
        raise LabError(f"{'.'.join(path)} is written inline in {edit.path.name} ({what}); "
                       "write it as tables, or change it by hand")


def lab_group(config: dict, lab: str) -> str:
    """The lab's group: LAB itself when it has a $ or there is no
    group_pattern, else the pattern with LAB as the NetID ("shared" stays
    "shared")."""
    lab = lab.strip()
    if lab.lower() == SHARED:
        return SHARED
    labs = (config.get("console") or {}).get("labs") if isinstance(config.get("console"), dict) else None
    pattern = (labs.get("group_pattern") or "") if isinstance(labs, dict) else ""
    pattern = pattern if isinstance(pattern, str) else ""
    if "$" in lab or "{netid}" not in pattern:
        return lab
    return pattern.strip().replace("{netid}", lab.lower())


def _get(data: dict, path: tuple):
    for k in path:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


def _list_path(data: dict, section: tuple, key: str) -> tuple:
    """Where the list of hosts lives: [[section.key]]. For storage devices,
    the old top-level [[storage_devices]] when only that is there (the
    storage collector still reads it then, and an entry added elsewhere would
    hide it). The workstation collector never read a top-level list."""
    if (key == "storage_devices" and _get(data, section + (key,)) is None
            and data.get(key)):
        return (key,)
    return section + (key,)


def _entries(data: dict, path: tuple) -> list:
    return [e for e in (_get(data, path) or []) if isinstance(e, dict)]


def _enable(edit: TomlEdit, section: tuple, what: str, changes: list) -> None:
    table = _get(edit.data, section)
    if isinstance(table, dict) and table.get("enabled") is False:
        raise LabError(f"[{'.'.join(section)}] has enabled = false here; set it to true "
                       f"by hand if {what} should run on this host")
    if not isinstance(table, dict) or "enabled" not in table:
        edit.insert_key(section, "enabled", True)
        changes.append(f"[{'.'.join(section)}] enabled = true")


def _resources(data: dict, group: str) -> dict:
    table = _get(data, RESOURCES + (group,)) or {}
    out = {}
    for k in ("workstations", "storage"):
        v = table.get(k) or []
        out[k] = [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)]
    return out


def _set_resources(edit: TomlEdit, group: str, res: dict, name: str | None = None) -> None:
    """The lab's table: its name (kept unless given) and lists."""
    _no_inline(edit, RESOURCES + (group,), f'the "{group}" lab')
    if name is None:
        name = (_get(edit.data, RESOURCES + (group,)) or {}).get("name")
    body = {"name": name} if isinstance(name, str) and name else {}
    body.update({k: v for k, v in res.items() if v})
    edit.set_table(RESOURCES + (group,), body)


def set_name(edit: TomlEdit, group: str, name: str) -> list:
    if group == SHARED:
        raise LabError('"shared" is not a lab; give the PI\'s NetID or the lab\'s group')
    name = " ".join(name.split())
    old = (_get(edit.data, RESOURCES + (group,)) or {}).get("name")
    if old == (name or None):
        return []
    _set_resources(edit, group, _resources(edit.data, group), name)
    return [f'{group}: shown as "{name}"' if name else f"{group}: shown by its group"]


def _listing_groups(data: dict, server: str) -> list:
    """The labs whose storage lists this server, alone or by an export."""
    return sorted(g for g, t in (_get(data, RESOURCES) or {}).items()
                  if isinstance(t, dict) and any(
                      x == server or x.startswith(server + ":")
                      for x in ([t.get("storage")] if isinstance(t.get("storage"), str)
                                else t.get("storage") or [])))


def _set_name(edit: TomlEdit, server: str, name: str | None, note: str | None,
              changes: list) -> None:
    if name is None and note is None:
        return
    old = _get(edit.data, NAMES + (server,)) or {}
    new = {"name": old.get("name", ""), "note": old.get("note", "")}
    if name is not None:
        new["name"] = name.strip()
    if note is not None:
        new["note"] = " ".join(note.split())
    body = {k: v for k, v in new.items() if v}
    if body != {k: v for k, v in old.items() if k in ("name", "note") and v}:
        edit.set_table(NAMES + (server,), body)
        changes.append(f'{server}: ' + ", ".join(f'{k} "{v}"' for k, v in body.items()))


def add_machine(edit: TomlEdit, group: str, host: str) -> list:
    host = _host(host)
    data = edit.data
    st = _list_path(data, ST_SECTION, "storage_devices")
    if any(e.get("hostname") == host for e in _entries(data, st)):
        raise LabError(f"{host} is collected here as a NAS; `nomad lab remove` it first "
                       "if it is a workstation")
    path = _list_path(data, WS_SECTION, "workstations")
    listed = {e.get("hostname"): e.get("department") for e in _entries(data, path)}
    changes = []
    if listed.get(host) == group:
        return changes
    _no_inline(edit, path, "the workstations")
    _enable(edit, WS_SECTION, "the workstation collector", changes)
    if host in listed:
        edit.remove_entries(path, hostname=host)
        changes.append(f"{host}: tagged {group} (was {listed[host] or 'untagged'})")
    else:
        changes.append(f"{host}: a workstation collected here, tagged {group}")
    edit.add_entry(path, {"hostname": host, "department": group})
    return changes


def add_nas(edit: TomlEdit, group: str, host: str, kind: str = "zfs",
            paths: list | None = None, name: str | None = None,
            note: str | None = None) -> list:
    host = _host(host)
    data = edit.data
    changes = []
    ws = _list_path(data, WS_SECTION, "workstations")
    if any(e.get("hostname") == host for e in _entries(data, ws)):
        edit.remove_entries(ws, hostname=host)
        changes.append(f"{host}: no longer collected as a workstation (a NAS is storage)")
    path = _list_path(edit.data, ST_SECTION, "storage_devices")
    current = [e for e in _entries(edit.data, path) if e.get("hostname") == host]
    entry = {"hostname": host, "type": kind, **({"paths": list(paths)} if paths else {})}
    # Everyone's storage says so, so that taking it off a lab never stops it
    # being collected (the collector ignores the key).
    if group == SHARED or any(e.get("shared") is True for e in current):
        entry["shared"] = True
    if current != [entry]:
        _no_inline(edit, path, "the storage devices")
        _enable(edit, ST_SECTION, "the storage collector", changes)
        edit.remove_entries(path, hostname=host)
        edit.add_entry(path, entry)
        changes.append(f"{host}: a NAS collected here ({kind}"
                       + (f", paths {', '.join(paths)}" if paths else "") + ")")
    if group == SHARED:
        _set_name(edit, host, name, note, changes)
    else:
        changes += add_storage(edit, group, host, name, note)
    return changes


def add_storage(edit: TomlEdit, group: str, target: str, name: str | None = None,
                note: str | None = None) -> list:
    if group == SHARED:
        raise LabError('"shared" storage is listed for no lab: add-storage needs a lab; '
                       "a shared NAS is added with add-nas shared HOST")
    server, _, export = target.strip().partition(":")
    if not server or (":" in target and not export.startswith("/")):
        raise LabError(f"{target}: a storage server, or server:/export as the lab's "
                       "machines mount it")
    _host(server)
    target = target.strip()
    changes = []
    res = _resources(edit.data, group)
    if target not in res["storage"]:
        res["storage"].append(target)
        _set_resources(edit, group, res)
        changes.append(f"{group}: storage {target}")
    _set_name(edit, server, name, note, changes)
    return changes


def remove(edit: TomlEdit, group: str, host: str) -> list:
    data = edit.data
    changes = []
    if group == SHARED:
        st = _list_path(data, ST_SECTION, "storage_devices")
        if any(e.get("hostname") == host for e in _entries(data, st)):
            listing = _listing_groups(data, host)
            if listing:
                raise LabError(f"{host} is listed for {', '.join(listing)}; remove it "
                               "from those labs first")
            edit.remove_entries(st, hostname=host)
            changes.append(f"{host}: no longer collected as a NAS")
        return changes
    ws = _list_path(data, WS_SECTION, "workstations")
    for e in _entries(data, ws):
        if e.get("hostname") == host:
            if e.get("department") not in (group, None, ""):
                raise LabError(f"{host} is tagged {e.get('department')}, not {group}; "
                               "nothing changed")
            edit.remove_entries(ws, hostname=host)
            changes.append(f"{host}: no longer collected as a workstation")
    st = _list_path(edit.data, ST_SECTION, "storage_devices")
    device = [e for e in _entries(edit.data, st) if e.get("hostname") == host]
    if device:
        others = [g for g in _listing_groups(edit.data, host) if g != group]
        if others:
            changes.append(f"{host}: still collected as a NAS (listed for {', '.join(others)} "
                           "too)")
        elif any(e.get("shared") is True for e in device):
            changes.append(f"{host}: still collected as a NAS (shared)")
        else:
            edit.remove_entries(st, hostname=host)
            changes.append(f"{host}: no longer collected as a NAS")
    res = _resources(edit.data, group)
    kept = {k: [x for x in v if x != host and not x.startswith(host + ":")]
            for k, v in res.items()}
    gone = [x for k in res for x in res[k] if x not in kept[k]]
    if gone:
        _set_resources(edit, group, kept)
        changes.append(f"{group}: no longer lists {', '.join(gone)}")
    return changes


# A NetID as the Console signs people in: no domain, nothing a shell would read.
_NETID = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")


def leads_of(data: dict) -> dict:
    """{netid: [groups]} from [console.labs] leads, as access_from() reads it
    (NetIDs lower-cased, a single group string taken as a list of one)."""
    raw = _get(data, LEADS)
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for who, groups in raw.items():
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(who, str) or not isinstance(groups, list):
            continue
        n = who.strip().lower()
        for g in groups:
            if isinstance(g, str) and g.strip() and g.strip() not in out.setdefault(n, []):
                out[n].append(g.strip())
    return out


def _write_leads(edit: TomlEdit, new: dict) -> None:
    """[console.labs.leads] holding ``new`` (gone when empty), right after
    [console.labs]. An inline ``leads = {...}`` line there (nomad.toml.example
    has ``leads = {}``) is replaced by the table; any other way of writing it
    is left for a person."""
    for i, table, k in reversed(edit.key_lines()):
        if table == LABS and k == ("leads",):
            try:
                edit.remove_line(i)
            except ValueError as e:
                raise LabError(f"{e}; change leads by hand") from e
    clash = edit.assigned(LEADS)
    if clash:
        raise LabError(f"leads is written as values in {edit.path.name} (line {clash[0] + 1}): "
                       "write it as [console.labs.leads] or change it by hand")
    edit.set_table(LEADS, new, after=LABS)


def set_lead(edit: TomlEdit, group: str, netid: str, remove: bool = False) -> list:
    """NETID leads GROUP too (or, remove=True, no longer) through
    [console.labs] leads. Whom group_pattern already makes the PI is not
    listed again."""
    if group == SHARED:
        raise LabError('"shared" is not a lab; give the PI\'s NetID or the lab\'s group')
    n = netid.strip().lower()
    if not _NETID.match(n):
        raise LabError(f"{netid}: not a NetID (no domain, no @)")
    current = leads_of(edit.data)
    groups = list(current.get(n, []))
    if remove:
        if group not in groups:
            return []
        groups.remove(group)
    else:
        from nomad.config.access import access_from
        acc = access_from(edit.data, log=False)
        if group in groups or (acc.group_pattern
                               and acc.group_pattern.replace("{netid}", n) == group):
            return []
        groups.append(group)
    new: dict = {}
    for who, gs in current.items():
        if who != n:
            new[who] = gs
        elif groups:
            new[who] = groups
    if n not in current and groups:
        new[n] = groups
    _write_leads(edit, new)
    return [f"{n} no longer leads {group}" if remove else f"{n} leads {group}"]


def summary(config: dict, group: str | None = None) -> list:
    """What nomad.toml here says about labs, as lines."""
    ws = _entries(config, _list_path(config, WS_SECTION, "workstations"))
    nas = _entries(config, _list_path(config, ST_SECTION, "storage_devices"))
    res = _get(config, RESOURCES) or {}
    names = _get(config, NAMES) or {}
    led = leads_of(config)
    groups = sorted({e.get("department") for e in ws if e.get("department")} | set(res)
                    | {g for gs in led.values() for g in gs})
    if group == SHARED:
        groups = []
    elif group:
        groups = [g for g in groups if g == group] or [group]
    lines = []
    for g in groups:
        shown = (_get(config, RESOURCES + (g,)) or {}).get("name")
        lines.append(f"{shown} ({g}):" if shown else f"{g}:")
        machines = sorted(e.get("hostname") for e in ws if e.get("department") == g)
        listed = _resources(config, g)
        if machines:
            lines.append(f"  workstations collected here: {', '.join(machines)}")
        if listed["workstations"]:
            lines.append(f"  workstations listed for the Console: {', '.join(listed['workstations'])}")
        for target in listed["storage"]:
            server = target.partition(":")[0]
            n = names.get(server) or {}
            label = (f"{n.get('name')} ({target})" if n.get("name") not in (None, "", target)
                     else target)
            here = next((e for e in nas if e.get("hostname") == server), None)
            lines.append(f"  storage: {label}" + (f", {n['note']}" if n.get("note") else "")
                         + (f"; a NAS collected here ({here.get('type', 'zfs')})" if here else ""))
        also = sorted(who for who, gs in led.items() if g in gs)
        if also:
            lines.append(f"  also led by: {', '.join(also)}")
        if lines[-1] in (f"{g}:", f"{shown} ({g}):"):
            lines.append("  nothing here")
    untagged = sorted(e.get("hostname") for e in ws if not e.get("department"))
    if untagged and not group:
        lines.append(f"workstations collected here with no lab: {', '.join(untagged)}")
    other_nas = sorted(e.get("hostname") for e in nas
                       if e.get("shared") is True
                       or not any(e.get("hostname") == t.partition(":")[0]
                                  for r in res.values() if isinstance(r, dict)
                                  for t in (r.get("storage") or [])))
    if other_nas and group in (None, SHARED):
        lines.append("shared NAS collected here (no lab): " + ", ".join(
            f"{h}, {names[h]['note']}" if (names.get(h) or {}).get("note") else h
            for h in other_nas))
    return lines
