# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector — privileged operations.

This module contains every operation that REQUIRES elevated privilege
(typically root, or CAP_DAC_READ_SEARCH). Everything else in the per_user
collector uses ordinary psutil calls that work for any uid.

Why this exists
---------------
Validation showed two privilege regimes:
  - spydur: collector ran as `installer` (no sudo). Process metadata across
    all users was visible (psutil works), but /proc/<pid>/io was AccessDenied
    for processes belonging to other users (320/391 sample rows).
  - arachne: collector ran as root. /proc/<pid>/io and /proc/<pid>/fd were
    fully accessible — required for fd attribution on compute nodes.

For Component 1 head/monitoring rules (CPU + memory), no privilege is
strictly required — psutil's cpu_percent and memory_info read from
/proc/<pid>/stat and /proc/<pid>/status, which are world-readable.

For Component 2 compute-node fd walking, root is required to read
/proc/<pid>/fd/* symlinks belonging to other users.

The architecture
----------------
Every privileged call goes through a function in this module. The
unprivileged code path never imports os, doesn't touch /proc/<pid>/fd
directly, and gracefully degrades when an operation returns None or raises
PermissionDenied.

v1: these functions execute in the main collector process. If the process
runs as root (via systemd), they succeed. If it runs as a regular user,
they degrade gracefully — the rule engine still detects CPU/memory misuse
on the user's own processes (which is most of head-node misuse anyway).

v2 (deferred): swap the bodies for IPC to a tiny root helper over a
Unix socket. Call sites remain unchanged. The protocol is whatever's in
this file's signatures, serialised.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """Raised when a privileged operation cannot proceed.

    Callers should catch this and either skip the operation or record
    a sample row with the privileged fields left NULL. Never escalate
    to the user — privilege gaps are a deployment property, not an error.
    """


@dataclass(frozen=True)
class FdWalkResult:
    """Result of walking /proc/<pid>/fd for one process.

    bucket_counts: how many fds resolve into each filesystem bucket.
    representative_paths: longest common prefix per bucket (truncated to 80 chars).
    total_walked: total fds successfully resolved.
    total_failed: fds we couldn't readlink (rare; usually transient).
    """
    bucket_counts: dict[str, int]
    representative_paths: dict[str, str]
    total_walked: int
    total_failed: int


# ---------------------------------------------------------------------------
# Capability detection (called once at startup)
# ---------------------------------------------------------------------------

def can_read_other_users_io() -> bool:
    """Check whether we can read /proc/<pid>/io for processes we don't own.

    On Linux, /proc/<pid>/io is mode 0400, owned by the process owner. Root
    bypasses; CAP_DAC_READ_SEARCH bypasses. Anyone else gets EACCES.

    Result drives whether fd walking is worth attempting on multi-user nodes.
    """
    if os.geteuid() == 0:
        return True
    # Probe: try to read our own /proc/self/io (always works) and any other
    # process's io. We can't easily test "another user's process" without
    # picking one, so we use a heuristic: if we can list /proc/1/io and
    # actually read it, we have the capability. /proc/1 is init/systemd,
    # owned by root.
    try:
        with open("/proc/1/io", "r") as f:
            f.read(64)
        return True
    except (PermissionError, FileNotFoundError, OSError):
        return False


def can_walk_fds_of_other_users() -> bool:
    """Check whether we can readlink /proc/<pid>/fd/* for processes we don't own.

    Same privilege regime as can_read_other_users_io. We probe /proc/1/fd
    because init always exists and is owned by root.
    """
    if os.geteuid() == 0:
        return True
    try:
        # Listing /proc/<pid>/fd of another user requires we can stat() the dir.
        entries = os.listdir("/proc/1/fd")
        if not entries:
            return False
        # Listing succeeds even unprivileged on some kernels; the real test
        # is whether readlink succeeds.
        os.readlink(f"/proc/1/fd/{entries[0]}")
        return True
    except (PermissionError, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# fd walking (Component 2; called only on role='compute')
# ---------------------------------------------------------------------------

# Filesystem bucket configuration. The collector passes in a mount map at
# call time; this default mirrors the validation's classifier.
DEFAULT_BUCKETS: tuple[tuple[str, str], ...] = (
    # (bucket_name, prefix) — order matters: first match wins
    ("local", "/localscratch/"),
    ("local", "/scratch/local/"),
    ("tmp", "/tmp/"),
    ("tmp", "/dev/shm/"),
    ("nfs_home", "/home/"),
    ("nfs_shared", "/shared/"),
    ("nfs_shared", "/usr/local/sw/"),
    ("system", "/proc/"),
    ("system", "/sys/"),
    ("system", "/dev/"),
    # Anything not matched falls into 'other'
)


def walk_fds(
    pid: int,
    buckets: tuple[tuple[str, str], ...] = DEFAULT_BUCKETS,
    max_fds: int = 10_000,
) -> FdWalkResult:
    """Walk /proc/<pid>/fd, classify each link by filesystem bucket.

    Returns a FdWalkResult with per-bucket counts and a representative path
    per bucket (longest common prefix, truncated). Soft-fails: if the
    process exits mid-walk, returns whatever was collected.

    Raises:
        PermissionDenied: we don't have access to this pid's fd dir.
                          The caller should record this once per collection
                          cycle and proceed.
    """
    fd_dir = f"/proc/{pid}/fd"
    try:
        entries = os.listdir(fd_dir)
    except PermissionError as e:
        raise PermissionDenied(f"cannot list {fd_dir}") from e
    except FileNotFoundError:
        # Process exited — return empty result, not an error
        return FdWalkResult({}, {}, 0, 0)
    except OSError as e:
        logger.debug("listdir(%s) failed: %s", fd_dir, e)
        return FdWalkResult({}, {}, 0, 0)

    bucket_counts: Counter[str] = Counter()
    bucket_paths: dict[str, list[str]] = {}
    walked = 0
    failed = 0

    for name in entries[:max_fds]:
        try:
            target = os.readlink(f"{fd_dir}/{name}")
        except (FileNotFoundError, PermissionError, OSError):
            failed += 1
            continue
        walked += 1
        bucket = _classify_path(target, buckets)
        bucket_counts[bucket] += 1
        bucket_paths.setdefault(bucket, []).append(target)

    # Compute representative path per bucket: longest common prefix, truncated
    representative: dict[str, str] = {}
    for bucket, paths in bucket_paths.items():
        prefix = _longest_common_prefix(paths)
        if len(prefix) > 80:
            prefix = prefix[:77] + "..."
        representative[bucket] = prefix

    return FdWalkResult(
        bucket_counts=dict(bucket_counts),
        representative_paths=representative,
        total_walked=walked,
        total_failed=failed,
    )


def _classify_path(path: str, buckets: tuple[tuple[str, str], ...]) -> str:
    """First-match bucket classifier. Anything unmatched -> 'other'."""
    if not path.startswith("/"):
        # Sockets, pipes, anon_inode etc. all have non-path 'targets'.
        return "other"
    for bucket_name, prefix in buckets:
        if path.startswith(prefix):
            return bucket_name
    return "other"


def _longest_common_prefix(paths: list[str]) -> str:
    """Return the longest common path prefix of a list of paths."""
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    s1 = min(paths)
    s2 = max(paths)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    return s1[:i]


# ---------------------------------------------------------------------------
# /proc/<pid>/io (auxiliary; not gating any v1 rules but useful for samples)
# ---------------------------------------------------------------------------

def read_proc_io(pid: int) -> dict[str, int] | None:
    """Read /proc/<pid>/io. Returns counters dict or None on permission denial.

    Does NOT raise on permission errors — just returns None — because IO data
    is supplementary in v1. Callers should not branch on this.
    """
    path = f"/proc/{pid}/io"
    try:
        with open(path, "r") as f:
            data = f.read()
    except (PermissionError, FileNotFoundError, OSError):
        return None
    out: dict[str, int] = {}
    for line in data.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                continue
    return out
