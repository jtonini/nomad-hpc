# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ pacct binary parser (module form)

Parses Linux BSD process accounting files (/var/account/pacct) without
external dependencies. Supports acct_v3 only (CONFIG_BSD_PROCESS_ACCT_V3),
which is the default on Rocky 9, RHEL 9, and modern Ubuntu.

Struct reference: man 5 acct, linux/include/uapi/linux/acct.h

See scripts/pacct_inspect.py for a CLI wrapper suitable for ad-hoc debugging.
"""

import pwd
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator


# -----------------------------------------------------------------------------
# Constants (from linux/include/uapi/linux/acct.h)
# -----------------------------------------------------------------------------

# ac_flag bits
AFORK = 0x01   # Process forked but did not exec
ASU = 0x02     # Process used superuser privileges
ACORE = 0x08   # Process dumped core
AXSIG = 0x10   # Process killed by a signal

# ACCT_COMM is 16 in the kernel. In acct_v3 the on-disk field is exactly
# ACCT_COMM bytes (no trailing NUL guaranteed). Confirmed against
# include/uapi/linux/acct.h — struct acct_v3 { ... char ac_comm[ACCT_COMM]; }.
# The older v0/v1/v2 `struct acct` uses [ACCT_COMM + 1] which is where some
# stale documentation gets the "17" number. This parser is v3-only.
ACCT_COMM = 16

# AHZ for v3 is 100 (fixed, regardless of kernel USER_HZ).
AHZ_V3 = 100


@dataclass
class PacctRecord:
    """One parsed pacct record (process exit)."""

    pid: int
    ppid: int
    uid: int
    gid: int
    username: str
    command: str

    start_time: int          # unix epoch when process started
    exit_time: int           # start_time + elapsed_seconds
    elapsed_seconds: float

    cpu_user_seconds: float
    cpu_system_seconds: float

    memory_avg_kb: int       # ac_mem: AVERAGE memory, not peak (kernel limitation)
    io_chars: int
    io_read_blocks: int
    io_write_blocks: int

    exit_code: int
    flags: int
    tty: int

    version: int


class PacctFormatError(ValueError):
    """Raised when the pacct file version is not supported (v3 only)."""


# -----------------------------------------------------------------------------
# comp_t decoding
# -----------------------------------------------------------------------------

def decode_comp_t(value: int) -> int:
    """Decode the kernel's 16-bit pseudo-float: 3-bit base-8 exponent, 13-bit mantissa."""
    exponent = (value >> 13) & 0x07
    mantissa = value & 0x1FFF
    return mantissa << (3 * exponent)


# -----------------------------------------------------------------------------
# Struct format
# -----------------------------------------------------------------------------
# acct_v3 layout (64 bytes, native byte order, no alignment padding):
#   B   ac_flag
#   B   ac_version
#   H   ac_tty
#   I   ac_exitcode
#   I   ac_uid
#   I   ac_gid
#   I   ac_pid
#   I   ac_ppid
#   I   ac_btime
#   f   ac_etime               (float in userspace; u32 in kernel)
#   H   ac_utime  (comp_t)
#   H   ac_stime  (comp_t)
#   H   ac_mem    (comp_t)
#   H   ac_io     (comp_t)
#   H   ac_rw     (comp_t)
#   H   ac_minflt (comp_t)
#   H   ac_majflt (comp_t)
#   H   ac_swaps  (comp_t)
#   16s ac_comm   (16 bytes, not NUL-terminated)

ACCT_V3_FORMAT = "=BBHIIIIIIfHHHHHHHH16s"
ACCT_V3_SIZE = struct.calcsize(ACCT_V3_FORMAT)
assert ACCT_V3_SIZE == 64, f"acct_v3 should be 64 bytes, got {ACCT_V3_SIZE}"

SUPPORTED_VERSIONS = {3}


# -----------------------------------------------------------------------------
# Parser
# -----------------------------------------------------------------------------

def _resolve_uid(uid: int, cache: dict[int, str]) -> str:
    if uid in cache:
        return cache[uid]
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = str(uid)
    cache[uid] = name
    return name


def _parse_v3_record(buf: bytes, uid_cache: dict[int, str]) -> PacctRecord:
    (
        ac_flag, ac_version, ac_tty, ac_exitcode,
        ac_uid, ac_gid, ac_pid, ac_ppid, ac_btime,
        ac_etime,
        ac_utime, ac_stime, ac_mem, ac_io, ac_rw,
        _ac_minflt, _ac_majflt, _ac_swaps,
        ac_comm_raw,
    ) = struct.unpack(ACCT_V3_FORMAT, buf)

    # Command: strip at first NUL, decode as latin-1 to never raise on weird bytes
    command = ac_comm_raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")

    elapsed = float(ac_etime)
    start = int(ac_btime)
    exit_time = int(start + elapsed)

    return PacctRecord(
        pid=ac_pid,
        ppid=ac_ppid,
        uid=ac_uid,
        gid=ac_gid,
        username=_resolve_uid(ac_uid, uid_cache),
        command=command,
        start_time=start,
        exit_time=exit_time,
        elapsed_seconds=round(elapsed, 3),
        cpu_user_seconds=round(decode_comp_t(ac_utime) / AHZ_V3, 3),
        cpu_system_seconds=round(decode_comp_t(ac_stime) / AHZ_V3, 3),
        memory_avg_kb=decode_comp_t(ac_mem),
        io_chars=decode_comp_t(ac_io),
        io_read_blocks=0,  # ac_rw semantics are inconsistent; we don't split reads/writes
        io_write_blocks=decode_comp_t(ac_rw),
        exit_code=ac_exitcode,
        flags=ac_flag,
        tty=ac_tty,
        version=ac_version,
    )


def parse_pacct(
    path: str | Path,
    since: int | None = None,
    max_records: int | None = None,
) -> Iterator[PacctRecord]:
    """Yield PacctRecord objects from the pacct file at *path*.

    Args:
        path:        Path to pacct file (typically /var/account/pacct).
        since:       Only yield records with exit_time > since (unix epoch).
                     Used by the collector to incrementally ingest.
        max_records: Stop after this many records.

    Raises:
        PacctFormatError: if the file's version byte is not in SUPPORTED_VERSIONS.
        FileNotFoundError, PermissionError: standard I/O exceptions.
    """
    path = Path(path)
    uid_cache: dict[int, str] = {}
    emitted = 0

    with path.open("rb") as f:
        header = f.read(2)
        if len(header) < 2:
            return
        _, version = struct.unpack("=BB", header)
        if version not in SUPPORTED_VERSIONS:
            raise PacctFormatError(
                f"pacct file {path} uses version {version}; "
                f"this parser supports {sorted(SUPPORTED_VERSIONS)}. "
                f"Check /boot/config-$(uname -r) for CONFIG_BSD_PROCESS_ACCT_V3."
            )

        f.seek(0)
        while True:
            buf = f.read(ACCT_V3_SIZE)
            if len(buf) < ACCT_V3_SIZE:
                break
            try:
                rec = _parse_v3_record(buf, uid_cache)
            except struct.error as e:
                # Corrupt record: don't silently skip — misalignment means
                # everything after is garbage. Stop cleanly.
                print(
                    f"pacct_parser: struct error at byte {f.tell()-ACCT_V3_SIZE}: {e}",
                    file=sys.stderr,
                )
                break

            if since is not None and rec.exit_time <= since:
                continue

            yield rec
            emitted += 1
            if max_records is not None and emitted >= max_records:
                break
