# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector — process ancestry and whitelist matching.

Pure logic over a tree abstraction. The collector provides a
`get_process_info(pid) -> ProcessInfo | None` callable; this module
walks ancestors and applies whitelist rules without touching /proc directly.

Whitelist semantics
-------------------
A process is whitelisted if ANY of:
  - its uid < min_uid (system account)
  - its username is in `users` (e.g. slurm, munge)
  - its exe_path or any ancestor's exe_path starts with one of `parent_paths`
    (recursive — children of whitelisted ancestors are also whitelisted)
  - (username, command_basename) is in `user_commands`

The validation showed 5/7 false positives on spydur came from arobbins's
clusterbackup tool — a system-installed cron job. Parent-path whitelisting
on /usr/local/sw/ eliminates these without name lists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Callable


@dataclass(frozen=True)
class ProcessInfo:
    """Minimal process snapshot needed for ancestry/whitelist decisions.

    The collector populates this from psutil; tests construct it directly.
    """
    pid: int
    ppid: int | None
    uid: int
    username: str
    command: str                          # comm (16-byte kernel name)
    exe_path: str | None                  # /proc/<pid>/exe target


@dataclass(frozen=True)
class WhitelistConfig:
    parent_paths: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    user_commands: tuple[tuple[str, str], ...] = ()
    min_uid: int = 1000


@dataclass
class WhitelistMatch:
    """Records why a process was whitelisted (for transparency in samples)."""
    reason: str                           # 'min_uid' | 'user' | 'parent_path' | 'user_command'
    detail: str                           # the specific entry that matched


@dataclass
class AncestryResult:
    """Output of walking a process tree.

    `chain` is root-first (the deepest ancestor we found is index 0).
    For an ssh -> bash -> R chain, chain == ['sshd', 'bash', 'R'].
    """
    chain: list[str] = field(default_factory=list)
    exe_chain: list[str | None] = field(default_factory=list)
    depth: int = 0
    truncated: bool = False               # True if depth limit was hit


# ---------------------------------------------------------------------------
# Ancestry walking
# ---------------------------------------------------------------------------

# Type alias for the process info lookup function the collector provides.
ProcessLookup = Callable[[int], ProcessInfo | None]


def walk_ancestry(
    pid: int,
    lookup: ProcessLookup,
    max_depth: int = 8,
) -> AncestryResult:
    """Walk parent links from `pid` up to root or `max_depth`.

    Returns the chain of process basenames root-first (i.e. with sshd or
    init at index 0 and the leaf at the end). The leaf process itself
    is NOT included — this is the *ancestor* chain.

    Cycle-safe: stops if a ppid revisits an already-seen pid.
    """
    seen: set[int] = set()
    leaf = lookup(pid)
    if leaf is None:
        return AncestryResult()

    ancestors: list[ProcessInfo] = []
    cur_pid = leaf.ppid
    while cur_pid is not None and cur_pid > 1:
        if cur_pid in seen:
            break
        seen.add(cur_pid)
        if len(ancestors) >= max_depth:
            return AncestryResult(
                chain=[a.command for a in reversed(ancestors)],
                exe_chain=[a.exe_path for a in reversed(ancestors)],
                depth=len(ancestors),
                truncated=True,
            )
        info = lookup(cur_pid)
        if info is None:
            break
        ancestors.append(info)
        cur_pid = info.ppid

    return AncestryResult(
        chain=[a.command for a in reversed(ancestors)],
        exe_chain=[a.exe_path for a in reversed(ancestors)],
        depth=len(ancestors),
    )


# ---------------------------------------------------------------------------
# Whitelist matching
# ---------------------------------------------------------------------------

def match_whitelist(
    proc: ProcessInfo,
    ancestry: AncestryResult,
    config: WhitelistConfig,
) -> WhitelistMatch | None:
    """Apply whitelist rules. Returns a match (with reason) or None."""

    # Rule 1: uid < min_uid
    if proc.uid < config.min_uid:
        return WhitelistMatch(reason="min_uid", detail=f"uid={proc.uid}")

    # Rule 2: explicit user list
    if proc.username in config.users:
        return WhitelistMatch(reason="user", detail=proc.username)

    # Rule 3: (user, command basename) pair
    cmd_basename = _command_basename(proc.command)
    for u, c in config.user_commands:
        if u == proc.username and c == cmd_basename:
            return WhitelistMatch(reason="user_command", detail=f"{u}:{c}")

    # Rule 4: parent path — recursive over the leaf's exe_path AND all ancestors
    paths_to_check = [proc.exe_path, *ancestry.exe_chain]
    for path in paths_to_check:
        if path is None:
            continue
        for prefix in config.parent_paths:
            if _path_starts_with(path, prefix):
                return WhitelistMatch(reason="parent_path", detail=prefix)

    return None


def _command_basename(command: str) -> str:
    """Return the basename of a command. comm is already 16 bytes, but
    cmdline-derived strings may have a path prefix."""
    if "/" in command:
        return os.path.basename(command)
    return command


def _path_starts_with(path: str, prefix: str) -> bool:
    """Path-aware prefix match: '/usr/local/sw/' matches '/usr/local/sw/foo'
    but not '/usr/local/swag/foo'. Trailing slash on prefix is recommended;
    if missing, we add it so 'sw' doesn't accidentally match 'swag'."""
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    # An exact match (path == prefix without trailing slash) also counts
    if path + "/" == prefix:
        return True
    return path.startswith(prefix)
