# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Whose to see each machine's description is (1.17.7).

nomad.toml says what a machine is ([console.machines], and storage servers'
[console.storage] notes). A description is seen by whoever sees the machine
named: a site, a storage server and a cluster's node are everyone's to see
(infrastructure); a workstation, often named for the person at it, is its
lab's (its PI, research assistants) and staff's; a name nomad can't place,
staff's. In that order: a site that is also a lab's workstation (a head
node) is a site; a machine listed or collected as storage is storage, old
workstation readings of it aside.

    kinds(conn, acc)                 {name: "site" | "storage" | "workstation"
                                      | "node" | "unknown"} for every name
                                      described
    visible(conn, acc, mine=None)    {name: note} the caller may see: mine
                                      None for staff (all), else the names of
                                      the caller's labs' machines

The database is read once per state on disk (a hub's changes once a sync):
one DISTINCT per table (the clusters' nodes over the last NODE_DAYS days).
A read that fails is never kept, and then only what nomad.toml says places a
name: anything else is "unknown", staff's.
"""
from __future__ import annotations

import sqlite3

from nomad.facts._common import columns

EVERYONES = ("site", "storage", "node")
NODE_DAYS = 30
_KEPT: dict = {}


class _Unread(Exception):
    """A table that couldn't be read: nothing it would say can be assumed."""


def _distinct(conn, table: str, col: str, since: str | None = None) -> set:
    if conn is None or col not in columns(conn, table):
        return set()
    sql, args = f"SELECT DISTINCT {col} FROM {table}", []
    if since is not None and "timestamp" in columns(conn, table):
        sql += " WHERE timestamp >= ?"
        args.append(since)
    try:
        return {str(r[0]).strip().lower() for r in conn.execute(sql, args) if r[0] is not None}
    except sqlite3.Error as exc:
        raise _Unread(table) from exc


def _sites(conn, config) -> set:
    from nomad.facts.sites import _local_name, site_names
    if conn is None:
        return {str(_local_name(config)).lower()}
    try:
        names = site_names(conn)
    except sqlite3.Error as exc:
        raise _Unread("sync_sites") from exc
    if names == [None]:                 # one site's own database: the name nomad.toml gives it
        return {str(_local_name(config)).lower()}
    return {str(n).lower() for n in names if n}


def kinds(conn: sqlite3.Connection | None, acc, config: dict | None = None) -> dict:
    """What kind of machine each described name is (see the module's notes)."""
    names = set(acc.notes())
    if not names:
        return {}
    listed_storage, listed_ws = set(), set()
    for kinds_ in (acc.resources or {}).values():
        for entry in (kinds_ or {}).get("storage", ()):
            listed_storage |= acc.aliases(str(entry).partition(":")[0])
        listed_ws |= {str(w).lower() for w in (kinds_ or {}).get("workstations", ())}
    configured = set()
    for server in (acc.servers or {}):
        configured |= acc.aliases(server)
    try:
        sites = _sites(conn, config)
        key = None
        if conn is not None:
            from nomad.facts.attention import _on_disk
            state = _on_disk(conn)
            key = (state, tuple(sorted(names)), tuple(sorted(sites)), tuple(sorted(configured)),
                   tuple(sorted(listed_storage)), tuple(sorted(listed_ws))) if state else None
            kept = _KEPT.get(key) if key is not None else None
            if kept is not None:
                return kept
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=NODE_DAYS)).isoformat(timespec="seconds")
        collected_storage = _distinct(conn, "storage_state", "hostname")
        workstations = listed_ws | _distinct(conn, "workstation_state", "hostname")
        nodes = _distinct(conn, "node_state", "node_name", since)
        read_storage = _distinct(conn, "storage_paths", "server")
    except _Unread:
        # Fail closed: only nomad.toml's word, and nothing kept.
        out = {}
        for n in names:
            al = acc.aliases(n)
            out[n] = ("storage" if al & (configured | listed_storage) and not al & listed_ws
                      else "workstation" if al & listed_ws else "unknown")
        return out
    out = {}
    for n in names:
        al = acc.aliases(n)
        if al & sites:
            out[n] = "site"
        elif al & (configured | listed_storage | collected_storage):
            out[n] = "storage"
        elif al & workstations:
            out[n] = "workstation"
        elif al & nodes:
            out[n] = "node"
        elif al & read_storage:
            out[n] = "storage"
        else:
            out[n] = "unknown"
    if key is not None:
        _KEPT.clear()
        _KEPT[key] = out
    return out


def visible(conn: sqlite3.Connection | None, acc, mine=None, config: dict | None = None) -> dict:
    """{name: note} for the caller: every description for staff (mine None);
    else those of the sites, storage servers and nodes, and of the machines
    in ``mine`` (names of the caller's labs' workstations and storage)."""
    notes = acc.notes()
    if mine is None:
        return notes
    own = set()
    for m in mine or ():
        own |= acc.aliases(m)
    k = kinds(conn, acc, config)
    return {n: note for n, note in notes.items() if k.get(n) in EVERYONES or acc.aliases(n) & own}
