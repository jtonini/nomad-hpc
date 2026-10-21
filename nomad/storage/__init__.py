# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Who uses a storage server's space (1.16).

Seeing everyone's files takes root on the storage server, and nomad never
runs as root there. So the part that reads runs there, from root's cron: a
short POSIX sh script (``nomad-storage.sh``, printed by ``nomad storage
script``) that only reads -- df, ``xfs_quota report``, ``zfs list /
userspace / groupspace``, and for the nightly scan ``find`` and ``stat`` --
and writes what it read to a folder its owner and one group can read. The
``storage_use`` collector, on a site or the hub, picks those files up with an
ordinary account (over ssh, or from this machine) and parses them here.

Two kinds of file:

* ``usage.txt`` (hourly or so): each owner's space on each path, from the
  filesystem's own accounting -- ZFS always keeps it, XFS when user or group
  accounting is on -- and quotas where set. Filesystems without such
  accounting say so; the scan covers them.
* ``scan.txt`` (nightly): for each folder at the top of a path
  (``/home/USER``), the folders inside it: size, files, the newest change,
  and how much is older than two ages (180 and 365 days by default). Each
  run scans for at most its time budget, the folders scanned longest ago
  first, so a large filesystem is covered over a few nights.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources

SCRIPT = "nomad-storage.sh"
VERSION = 1


# Sizes as people write them: a bare letter (500M, 20G, 1T) or KiB, MiB...
# counts in powers of 1024, as du -h and xfs_quota -h do; KB, MB, GB, TB, PB
# in powers of 1000, as df -H and nomad show them.
_UNITS = {"": 1, "B": 1}
for _i, _p in enumerate("KMGTP", 1):
    _UNITS[_p] = _UNITS[_p + "IB"] = 1024 ** _i
    _UNITS[_p + "B"] = 1000 ** _i


def parse_size(text) -> int:
    """'1T', '1.5TiB', '500GB', '20 G', '1048576' -> bytes. ValueError otherwise."""
    from decimal import Decimal
    m = re.fullmatch(r"\s*([0-9]{1,30}(?:\.[0-9]{0,30})?|\.[0-9]{1,30})\s*([A-Za-z]*)\s*", str(text))
    if not m or m.group(2).upper() not in _UNITS:
        raise ValueError(f"{text!r}: a size such as 500M, 20GB or 1T")
    return int(Decimal(m.group(1)) * _UNITS[m.group(2).upper()])


def format_size(b, binary: bool = False) -> str:
    """Bytes as nomad says them: decimal (TB, as df -H), or with binary
    powers of 1024 (TiB, as du -h and xfs_quota -h)."""
    if b is None:
        return "-"
    b = float(b)
    step = 1024 if binary else 1000
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB") if binary else ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if abs(b) < step or unit == units[-1]:
            return f"{b:.0f} {unit}" if unit in ("B", "KB", "KiB") else f"{b:.1f} {unit}"
        b /= step
    return f"{b:.1f} {units[-1]}"


def script_text() -> str:
    return resources.files("nomad.storage").joinpath(SCRIPT).read_text(encoding="utf-8")


class ReportError(ValueError):
    """A file that isn't one of the script's, or is cut short."""


@dataclass
class PathUse:
    path: str
    fs: str                      # zfs, xfs, ext2/ext3, nfs, other
    dataset: str | None = None   # zfs only
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    mount: str | None = None
    accounting: bool = False     # per-owner counts came with it
    note: str | None = None
    covers: str | None = None    # the counts are of all of this (a dataset's or filesystem's mount)
    errors: list[str] = field(default_factory=list)


@dataclass
class Owner:
    path: str
    dataset: str | None
    kind: str                    # user, group, project
    name: str                    # as the server resolves it; a number when it can't
    used_bytes: int
    soft_bytes: int | None = None
    hard_bytes: int | None = None


@dataclass
class Dataset:
    path: str
    name: str
    mountpoint: str | None
    used_bytes: int | None
    available_bytes: int | None
    quota_bytes: int | None
    refquota_bytes: int | None
    referenced_bytes: int | None


@dataclass
class Folder:
    name: str                    # "." for the files directly in the top folder
    owner_uid: int | None
    bytes: int
    files: int
    newest: int | None           # epoch
    older1_bytes: int
    older2_bytes: int


@dataclass
class Top:
    path: str
    name: str
    owner_uid: int | None
    scanned_at: int
    seconds: int | None = None
    rc: int | None = None
    denied: int = 0              # folders or files it couldn't read
    folders: list[Folder] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        # find's 1: files that vanished or couldn't be read, the rest counted.
        return self.rc in (0, 1)


@dataclass
class Report:
    mode: str                    # usage or scan
    version: int
    host: str | None = None
    os: str | None = None
    run_by: str | None = None    # the account that ran it (uid name, "via sudo from X")
    run_uid: int | None = None
    started: int | None = None
    finished: int | None = None
    paths: list[PathUse] = field(default_factory=list)
    owners: list[Owner] = field(default_factory=list)
    datasets: list[Dataset] = field(default_factory=list)
    ages: tuple[int, int] = (180, 365)
    tops: list[Top] = field(default_factory=list)
    not_scanned: int = 0
    present: dict[str, list[str]] = field(default_factory=dict)  # path -> its top folders now
    unreadable: list[str] = field(default_factory=list)          # paths the scan couldn't list
    odd_names: int = 0           # top folders left out for a tab, line break or backslash


def _int(v) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n


# Figures past these are not a real filesystem's: a made-up line.
MAX_BYTES = 1 << 60              # an exbibyte
MAX_EPOCH = 32503680000          # the year 3000


def _size(v) -> int | None:
    n = _int(v)
    return n if n is not None and 0 <= n < MAX_BYTES else None


def _quota(v) -> int | None:
    """A ZFS quota: none, '-' or 0 is no quota."""
    n = _size(v)
    return n if n and n > 0 else None


_XFS = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)")


def parse(text: str) -> Report:
    """A file the script wrote. Raises ReportError when it isn't one, or was
    cut short (no #finished line: a run that died, or a file being written)."""
    # Only line feeds end lines: splitlines() would also break at a carriage
    # return or other control character in a name.
    lines = text.split("\n")
    if not lines or not lines[0].startswith("#nomad-storage\t"):
        raise ReportError("not a nomad-storage file")
    head = lines[0].split("\t")
    if len(head) < 3 or head[2] not in ("usage", "scan"):
        raise ReportError("not a nomad-storage file")
    version = _int(head[1])
    if version is None or version > VERSION:
        raise ReportError(f"written by a newer nomad-storage.sh (version {head[1]}); "
                          "upgrade nomad here")
    rep = Report(mode=head[2], version=version)
    section = None
    cur_path: PathUse | None = None
    cur_dataset: str | None = None
    top: Top | None = None
    present: list[str] | None = None
    for raw in lines[1:]:
        if not raw:
            continue
        f = raw.split("\t")
        key = f[0]
        # xfs_quota writes an account it can't name as #UID: a data line.
        xfs_row = section in ("#xfs-users", "#xfs-groups", "#xfs-projects") and re.match(r"^#\d+\s", raw)
        if key.startswith("#") and not xfs_row:
            section = key
            if key == "#host":
                rep.host = f[1] if len(f) > 1 else None
            elif key == "#os":
                rep.os = f[1] if len(f) > 1 else None
            elif key == "#run-by" and len(f) >= 3:
                rep.run_uid = _int(f[1])
                rep.run_by = f[2] or f[1]
                if len(f) > 3 and f[3] not in ("", "-"):
                    rep.run_by += f" (sudo from {f[3]})"
            elif key == "#readers" and len(f) > 1 and f[1] == "sudo":
                rep.run_by = (rep.run_by or "?") + ", reading through sudo"
            elif key == "#started":
                rep.started = _int(f[1]) if len(f) > 1 else None
            elif key == "#finished":
                rep.finished = _int(f[1]) if len(f) > 1 else None
            elif key == "#path" and len(f) >= 3:
                cur_path = PathUse(path=f[1], fs=f[2], dataset=(f[3] if len(f) > 3 and f[3] != "-" else None))
                rep.paths.append(cur_path)
                cur_dataset = cur_path.dataset
            elif key == "#df" and cur_path is not None and len(f) >= 7:
                total, used, avail = _int(f[2]), _int(f[3]), _int(f[4])
                cur_path.total_bytes = total * 1024 if total is not None else None
                cur_path.used_bytes = used * 1024 if used is not None else None
                cur_path.available_bytes = avail * 1024 if avail is not None else None
                cur_path.mount = f[6]
            elif key in ("#zfs-users", "#zfs-groups") and len(f) >= 2:
                cur_dataset = f[1]
                if cur_path is not None:
                    cur_path.accounting = True
            elif key in ("#xfs-users", "#xfs-groups", "#xfs-projects"):
                pass
            elif key == "#no-accounting" and cur_path is not None:
                cur_path.note = f[1] if len(f) > 1 else None
            elif key == "#covers" and cur_path is not None:
                cur_path.covers = f[1] if len(f) > 1 and f[1] else None
            elif key == "#error" and cur_path is not None and len(f) > 1:
                cur_path.errors.append(f[1])
            elif key == "#present" and len(f) > 1:
                present = rep.present.setdefault(f[1], [])
            elif key == "#unreadable" and len(f) > 1:
                rep.unreadable.append(f[1])
            elif key == "#odd-names":
                rep.odd_names = (_int(f[1]) or 0) if len(f) > 1 else 0
            elif key == "#denied" and top is not None:
                top.denied = (_int(f[1]) or 0) if len(f) > 1 else 0
            elif key == "#ages" and len(f) >= 3:
                a, b = _int(f[1]), _int(f[2])
                if a is not None and b is not None:
                    rep.ages = (a, b)
            elif key == "#top" and len(f) >= 5:
                top = Top(path=f[1], name=f[2], owner_uid=_int(f[3]), scanned_at=_int(f[4]) or 0)
                rep.tops.append(top)
            elif key == "#rc" and top is not None:
                top.rc = _int(f[1]) if len(f) > 1 else None
            elif key == "#endtop" and top is not None:
                top.seconds = _int(f[1]) if len(f) > 1 else None
                top = None
            elif key == "#not-scanned":
                rep.not_scanned = _int(f[1]) or 0
            if key != "#present":
                present = None
            continue
        # Data lines, by the section they are in.
        if section == "#present" and present is not None and key == "P" and len(f) > 1:
            present.append(f[1])
        elif section == "#datasets" and len(f) >= 7 and cur_path is not None:
            rep.datasets.append(Dataset(path=cur_path.path, name=f[0],
                                        mountpoint=None if f[1] in ("-", "none", "legacy") else f[1],
                                        used_bytes=_size(f[2]), available_bytes=_size(f[3]),
                                        quota_bytes=_quota(f[4]), refquota_bytes=_quota(f[5]),
                                        referenced_bytes=_size(f[6])))
        elif section in ("#zfs-users", "#zfs-groups") and len(f) >= 3 and cur_path is not None:
            used = _size(f[2])
            if used is None:
                continue
            rep.owners.append(Owner(path=cur_path.path, dataset=cur_dataset,
                                    kind="user" if section == "#zfs-users" else "group",
                                    name=f[1], used_bytes=used,
                                    hard_bytes=_quota(f[3]) if len(f) > 3 else None))
        elif section in ("#xfs-users", "#xfs-groups", "#xfs-projects") and cur_path is not None:
            m = _XFS.match(raw.strip())
            if not m:
                continue
            cur_path.accounting = True
            name, used, soft, hard = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            if max(used, soft, hard) * 1024 >= MAX_BYTES:
                continue
            rep.owners.append(Owner(path=cur_path.path, dataset=None,
                                    kind={"#xfs-users": "user", "#xfs-groups": "group"}.get(section, "project"),
                                    name=name.lstrip("#"), used_bytes=used * 1024,
                                    soft_bytes=soft * 1024 or None, hard_bytes=hard * 1024 or None))
        elif key == "F" and top is not None and len(f) >= 8:
            nums = [_size(x) for x in f[3:8]]
            if any(n is None for n in nums):
                continue                         # not a real folder's figures
            size, files, newest, old1, old2 = nums
            if newest > MAX_EPOCH:
                newest = 0                       # a file dated centuries ahead: no date
            top.folders.append(Folder(name=f[1], owner_uid=_int(f[2]), bytes=size, files=files,
                                      newest=newest or None, older1_bytes=old1, older2_bytes=old2))
    if rep.finished is None:
        raise ReportError("cut short: no #finished line (the run died, or is still writing)")
    return rep
