# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ cgroup v2 per-user probe

Reads live per-user resource data from systemd's user slices:
  /sys/fs/cgroup/user.slice/user-<UID>.slice/

Zero external dependencies. Pure stdlib. Designed to be invoked locally
(for testing) or over SSH (for workstation collection). Output is
JSON lines — one object per active user — for robust pipe-based parsing.

Why JSON lines?
  - One SSH round-trip collects all users on a host.
  - Parsing is exact; no fixed-width column alignment to break.
  - Future eBPF / prometheus sources can emit the same schema.

Usage (local test):
    python3 cgroup_probe.py
    python3 cgroup_probe.py --fake-root /tmp/fake-cgroup
    python3 cgroup_probe.py --pretty

Usage (remote, via SSH):
    ssh workstation "python3 -" < cgroup_probe.py
    # or after deployment:
    ssh workstation /usr/local/bin/nomad-cgroup-probe

All cgroup v2 files are stable kernel interfaces; see
Documentation/admin-guide/cgroup-v2.rst in the kernel tree.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


# Default cgroup v2 root. Override with --fake-root for testing.
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")

# How fresh is "active enough to report"? If a user slice has had no CPU
# activity and no alive processes, we still report it — the caller gets
# to decide how to filter. But we skip user-<UID>.slice directories where
# pids.current is absent or zero AND the slice has cpu.stat == 0. These
# are phantom slices systemd sometimes leaves behind briefly.
PROBE_VERSION = "1"


@dataclass
class UserCgroupSnapshot:
    """Point-in-time cgroup data for one logged-in user.

    Mirrors the workstation_user_snapshot DB schema. Fields use None for
    missing data rather than 0, so callers can distinguish "feature not
    present on this kernel" from "zero activity".
    """

    # Identity
    hostname: str
    username: str
    uid: int

    # Session marker: cgroup slice ctime (unix seconds).
    # Changes when user logs out and back in -> slice recreated.
    session_epoch: int

    # Collection timestamp (unix seconds). Set by probe, not the collector,
    # so the value reflects the moment of measurement on the remote host.
    collected_at: int

    # CPU (microseconds, cumulative since session_epoch)
    cpu_usage_usec: int | None
    cpu_user_usec: int | None
    cpu_system_usec: int | None

    # Memory (bytes)
    memory_current_bytes: int | None
    memory_peak_bytes: int | None  # None on kernels without memory.peak

    # I/O (bytes, cumulative, summed across block devices)
    io_read_bytes: int | None
    io_write_bytes: int | None

    # Process activity
    pids_current: int | None

    # Probe metadata
    probe_version: str = PROBE_VERSION
    source: str = "cgroup_v2"


# -----------------------------------------------------------------------------
# File readers — each returns None on "file missing or unreadable" rather
# than raising. A missing file is valid data: it means the kernel doesn't
# expose that metric, not that the probe is broken.
# -----------------------------------------------------------------------------

def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        return None


def _read_cpu_stat(path: Path) -> dict[str, int]:
    """Parse cpu.stat into {key: int}. Returns {} on failure."""
    out: dict[str, int] = {}
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError):
        return out
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass  # future kernels may add non-integer fields; ignore
    return out


def _read_io_stat(path: Path) -> tuple[int | None, int | None]:
    """Sum rbytes and wbytes across all block devices in io.stat.

    Format (one line per device):
        MAJOR:MINOR rbytes=N wbytes=N rios=N wios=N [dbytes=N dios=N]

    Returns (read_bytes, write_bytes). None if file missing.
    """
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError):
        return None, None

    rbytes_total = 0
    wbytes_total = 0
    for line in text.splitlines():
        # Skip the leading "MAJOR:MINOR" token; parse the rest as key=value.
        tokens = line.split()
        for tok in tokens[1:]:
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            try:
                value = int(v)
            except ValueError:
                continue
            if k == "rbytes":
                rbytes_total += value
            elif k == "wbytes":
                wbytes_total += value
    return rbytes_total, wbytes_total


def _slice_ctime(path: Path) -> int:
    """Return slice directory ctime in unix seconds.

    ctime changes when the directory is created (new slice after login)
    or when its metadata changes. For systemd user slices, this is a
    reliable "session_epoch" marker.
    """
    try:
        return int(path.stat().st_ctime)
    except (FileNotFoundError, PermissionError):
        return 0


def _resolve_uid(uid: int, cache: dict[int, str]) -> str:
    if uid in cache:
        return cache[uid]
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    cache[uid] = name
    return name


def _parse_user_uid(dirname: str) -> int | None:
    """Extract UID from a directory name like 'user-1001.slice'."""
    if not dirname.startswith("user-") or not dirname.endswith(".slice"):
        return None
    try:
        return int(dirname[len("user-"):-len(".slice")])
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Main probe
# -----------------------------------------------------------------------------

def probe_users(
    cgroup_root: Path = DEFAULT_CGROUP_ROOT,
    hostname: str | None = None,
    now: int | None = None,
) -> list[UserCgroupSnapshot]:
    """Collect cgroup data for every active user on this host.

    Args:
        cgroup_root: cgroup v2 mount point. /sys/fs/cgroup in production.
        hostname:    Override hostname (default: socket.gethostname()).
        now:         Override collection timestamp (for deterministic tests).

    Returns:
        One UserCgroupSnapshot per user-<UID>.slice directory present.
    """
    if hostname is None:
        import socket
        hostname = socket.gethostname()
    if now is None:
        now = int(time.time())

    user_slice_root = cgroup_root / "user.slice"
    if not user_slice_root.is_dir():
        # Host doesn't use systemd cgroup v2 user slices. Return empty —
        # this is not an error; it's "no data to report".
        return []

    uid_cache: dict[int, str] = {}
    snapshots: list[UserCgroupSnapshot] = []

    try:
        entries = sorted(user_slice_root.iterdir())
    except PermissionError:
        return []

    for entry in entries:
        uid = _parse_user_uid(entry.name)
        if uid is None:
            continue
        if not entry.is_dir():
            continue

        cpu = _read_cpu_stat(entry / "cpu.stat")
        mem_current = _read_int(entry / "memory.current")
        mem_peak = _read_int(entry / "memory.peak")  # may not exist
        io_read, io_write = _read_io_stat(entry / "io.stat")
        pids = _read_int(entry / "pids.current")

        snap = UserCgroupSnapshot(
            hostname=hostname,
            username=_resolve_uid(uid, uid_cache),
            uid=uid,
            session_epoch=_slice_ctime(entry),
            collected_at=now,
            cpu_usage_usec=cpu.get("usage_usec"),
            cpu_user_usec=cpu.get("user_usec"),
            cpu_system_usec=cpu.get("system_usec"),
            memory_current_bytes=mem_current,
            memory_peak_bytes=mem_peak,
            io_read_bytes=io_read,
            io_write_bytes=io_write,
            pids_current=pids,
        )
        snapshots.append(snap)

    return snapshots


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Probe cgroup v2 user slices. Emits JSON lines.",
    )
    p.add_argument(
        "--fake-root",
        type=Path,
        default=None,
        help="Use this path instead of /sys/fs/cgroup (for testing).",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON for human reading (breaks JSON-lines format).",
    )
    p.add_argument(
        "--hostname",
        default=None,
        help="Override hostname in output (default: socket.gethostname()).",
    )
    args = p.parse_args(argv)

    cgroup_root = args.fake_root if args.fake_root else DEFAULT_CGROUP_ROOT

    snapshots = probe_users(cgroup_root=cgroup_root, hostname=args.hostname)

    for snap in snapshots:
        d = asdict(snap)
        try:
            if args.pretty:
                print(json.dumps(d, indent=2))
            else:
                print(json.dumps(d))
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except Exception:
                pass
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
            return 0

    # Summary to stderr so it doesn't pollute JSON-lines output
    if not snapshots:
        print("no active user slices found", file=sys.stderr)
    else:
        print(f"{len(snapshots)} user slice(s) reported", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
