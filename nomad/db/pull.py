# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Copy a site's database to the hub: a consistent snapshot, never a live file.

``nomad sync`` asks each site for a snapshot (SQLite's ``.backup``, which is
consistent while the site's collectors keep writing), copies it, and deletes
it. What this module adds to that, each from something that went wrong:

* **The snapshot goes where there is room for it**: in /tmp when it has
  ample room (as before), otherwise beside the site's database, and in the
  other place when the first runs out of room. A cluster's login node often
  has a /tmp of a few GB shared by everyone, smaller than nomad's database:
  once the database outgrew the room left there, every pull failed with
  "database or disk is full" and the site dropped out of the hub.
* **It is always removed**, whatever happened (a copy cut off by its time
  limit left a snapshot the size of the database behind, and two of those
  filled /tmp); and snapshots left by runs that died before cleaning up are
  removed after two hours.
* **The copy lands under a temporary name** and replaces the hub's cached
  copy only once it is complete, so a failed pull never destroys the last
  good copy the hub falls back on.
* **Time limits follow the database's size** instead of a fixed two
  minutes, which a growing database outgrows.
* **A failed snapshot is never followed by copying the live database**,
  whatever the failure (a file copied while it is written looks complete and
  is malformed). A site without the sqlite3 command takes the snapshot with
  its python3 (SQLite's backup, Python 3.7 or later); only a site with
  neither is copied directly, as before, and the caller is told so (such a
  copy can be torn without looking it: it is not trusted to say where the
  site's change log stands).
* **The reason given is the error**, not the site's login banner: a banner
  comes first in what ssh prints, the error last.
* **A site on the hub itself** (``host = "localhost"``) is read directly,
  with SQLite's backup, as whichever account runs the sync: no ssh to itself
  (an account with no login shell can't).
* **Before a site without sqlite3 is copied directly**, its write-ahead log
  is checkpointed into the database (with python3, when the site has it), so
  the copy holds what was written since the last checkpoint.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
import subprocess as sp
import time
from pathlib import Path

logger = logging.getLogger("nomad.sync")

PREFIX = "nomad_sync_"            # snapshot names, in /tmp and (hidden) beside the database
TMP = "/tmp"                      # the site's shared temporary directory
STALE_MINUTES = 120               # a snapshot older than this is a leftover
BEGIN = "__NOMAD_OUTPUT_BEGINS__"  # what a site prints before it (banners, motd) is not read
SQLITE_HEADER = b"SQLite format 3\x00"
MIB = 1024 * 1024
# Room beyond the database's own size: SQLite's journal for the snapshot, and
# never filling a filesystem to the last byte.
MARGIN = 64 * MIB
# In a shared /tmp, leave others room: use it only with this much to spare.
TMP_MARGIN = 256 * MIB
# Time limits: a floor, plus the database's size at a slow-but-working rate.
SNAPSHOT_RATE = 20 * MIB          # bytes per second, writing the snapshot
COPY_RATE = 5 * MIB               # bytes per second, copying it to the hub
MAX_SECONDS = 3600
# A directory that is safe to name in a command and in scp's remote path.
_SAFE_DIR = re.compile(r"^/[A-Za-z0-9._/+@:-]*$")
# A site on the hub itself: read directly, no ssh.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_local(host) -> bool:
    return str(host or "").strip().lower() in LOCAL_HOSTS


# The site's own python3, as nomad's exporter runs it (not a module path
# meant for another Python).
PY = "env -u PYTHONHOME -u PYTHONPATH python3"
_PY_BACKUP = ("import sqlite3,sys; s=sqlite3.connect(sys.argv[1], timeout=20); "
              "d=sqlite3.connect(sys.argv[2]); s.backup(d, pages=100); d.close(); s.close()")


def checkpoint_command(remote_db: str) -> str:
    """Fold the write-ahead log into the database before it is copied as a
    file (a site without sqlite3); nothing if the site has no python3."""
    script = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1], timeout=20); "
              "c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()")
    return (f"if command -v python3 >/dev/null 2>&1; then "
            f"python3 -c {shlex.quote(script)} {remote_word(remote_db)}; fi")


def remote_word(path: str) -> str:
    """A remote path as one shell word: ``~/x`` stays the remote home's."""
    path = str(path)
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def error_text(stderr: str | None, banner: str = "", limit: int = 300) -> str:
    """What a command said went wrong. A site's login banner comes first on
    stderr (sshd's Banner), the error last: the banner -- as the site printed
    it for a command that went well -- is taken off; not knowing it, only the
    last line is kept."""
    text = stderr or ""
    if banner and text.startswith(banner):
        text = text[len(banner):]
        keep = 3
    else:
        keep = 1
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = " | ".join(lines[-keep:])
    return text if len(text) <= limit else "…" + text[-limit:]


def after_marker(stdout: str | None) -> dict:
    """KEY=VALUE lines printed after BEGIN (whatever the site prints before)."""
    text = stdout or ""
    if BEGIN in text:
        text = text.split(BEGIN, 1)[1]
    out = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.isupper():
            out[key] = value
    return out


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def plan_snapshot(size: int | None, beside_free: int | None, beside_writable: bool,
                  tmp_free: int | None) -> list:
    """Where to try writing the snapshot, in order: /tmp when it has ample
    room (as nomad always did: nothing there counts against the account's
    quota, which the site's own collector writes under), then beside the
    database, then /tmp if it has just enough. Empty when neither has room
    (``size`` None: unknown, so only the margin is asked for)."""
    s = size or 0
    tmp_ample = tmp_free is not None and tmp_free >= s + max(TMP_MARGIN, s // 4)
    tmp_enough = tmp_free is not None and tmp_free >= s + max(MARGIN, s // 20)
    beside = beside_writable and beside_free is not None and beside_free >= s + max(MARGIN, s // 20)
    order = ["tmp"] if tmp_ample else []
    if beside:
        order.append("beside")
    if tmp_enough and not tmp_ample:
        order.append("tmp")
    return order


def limit(size: int | None, floor: int, rate: int) -> int:
    """Seconds allowed for moving ``size`` bytes at ``rate``, at least ``floor``."""
    return int(min(MAX_SECONDS, floor + (size or 0) / rate))


def _gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e9:.1f} GB"


def _kib(value) -> int | None:
    n = _int(value)
    return None if n is None else n * 1024


def probe_command(remote_db: str) -> str:
    """One round trip: is there sqlite3, how big is the database (through a
    symlink), where is it really, how much room is beside it and in /tmp; and
    this account's snapshots left by earlier runs (older than STALE_MINUTES,
    regular files named as nomad names them, in those two directories only)
    are removed."""
    db = remote_word(remote_db)
    tmp = shlex.quote(TMP.rstrip("/") + "/")
    return (
        f"db={db}; d=$(dirname -- \"$db\"); r=$(cd -- \"$d\" 2>/dev/null && pwd -P); "
        f"u=$(id -un); "
        f"find {tmp} -mindepth 1 -maxdepth 1 -type f -user \"$u\" -name '{PREFIX}*.db*' "
        f"-mmin +{STALE_MINUTES} -delete 2>/dev/null; "
        f"if [ -n \"$r\" ]; then find \"$r\"/ -mindepth 1 -maxdepth 1 -type f -user \"$u\" "
        f"-name '.{PREFIX}*.db*' -mmin +{STALE_MINUTES} -delete 2>/dev/null; fi; "
        f"echo {BEGIN}; "
        f"if command -v sqlite3 >/dev/null 2>&1; then echo SQLITE=1; else echo SQLITE=0; fi; "
        f"if {PY} -c 'import sqlite3; getattr(sqlite3.Connection, \"backup\")' >/dev/null 2>&1; "
        f"then echo PYBACKUP=1; else echo PYBACKUP=0; fi; "
        f"if [ -f \"$db\" ]; then echo SIZE=$(stat -L -c %s -- \"$db\" 2>/dev/null "
        f"|| wc -c < \"$db\"); fi; "
        f"echo DIR=\"$r\"; "
        f"if [ -n \"$r\" ] && [ -w \"$r\" ]; then echo DIRW=1; else echo DIRW=0; fi; "
        f"if [ -n \"$r\" ]; then echo DIRFREE_KB=$(df -Pk -- \"$r\" 2>/dev/null "
        f"| awk 'NR==2{{print $4}}'); fi; "
        f"echo TMPFREE_KB=$(df -Pk {tmp} 2>/dev/null | awk 'NR==2{{print $4}}')"
    )


def snapshot_command(remote_db: str, snap: str, seconds: int, python: bool = False) -> str:
    """Write the snapshot (private from the start: other users share /tmp),
    stopped on the site too if it overruns, and say how big it is: with the
    sqlite3 command, or (``python``) with the site's python3."""
    q = shlex.quote(snap)
    if python:
        backup = f"{PY} -c {shlex.quote(_PY_BACKUP)} {remote_word(remote_db)} {q}"
    else:
        # .timeout: wait out a collector's write instead of failing at once.
        backup = (f"sqlite3 -cmd '.timeout 20000' {remote_word(remote_db)} "
                  f"{shlex.quote('.backup ' + snap)}")
    return (
        f"umask 077; if command -v timeout >/dev/null 2>&1; "
        f"then timeout {max(seconds - 10, 10)} {backup}; else {backup}; fi "
        f"&& echo {BEGIN} && echo SNAP=$(stat -c %s -- {q} 2>/dev/null || wc -c < {q})"
    )


# A snapshot that failed for want of room may fit in the other place.
_NO_ROOM = ("full", "quota", "no space", "i/o error")


def pull(user: str, host: str, remote_db: str, local_copy, ssh_key: str | None = None,
         why: list | None = None, tag: str | None = None, how: list | None = None,
         refuse_live: bool = False) -> bool:
    """Copy ``host``'s database to ``local_copy``. True when ``local_copy`` is
    a complete new copy; False otherwise, with ``local_copy`` untouched (the
    hub's last good copy) and the reason appended to ``why``. ``how`` gets
    "live" when the live file was copied (no snapshot could be taken); with
    ``refuse_live`` it is not, and ``how`` gets "no snapshot" instead (the
    caller has another way)."""
    local_copy = Path(local_copy)
    if is_local(host):
        return copy_local(remote_db, local_copy, why)
    # Unique, so two syncs (with different outputs) never share one.
    part = local_copy.with_name(f"{local_copy.name}.{os.getpid()}.part")
    _remove_old_parts(local_copy)

    def fail(reason: str) -> bool:
        logger.warning(f"{host}: {reason}")
        if why is not None:
            why.append(reason)
        try:
            part.unlink()
        except OSError:
            pass
        return False

    def ssh(cmd: str, timeout: int):
        args = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
        if ssh_key:
            args += ["-i", ssh_key]
        return sp.run(args + [f"{user}@{host}", cmd], capture_output=True, text=True,
                      timeout=timeout)

    def scp(remote_path: str, timeout: int):
        args = ["scp", "-q", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
        if ssh_key:
            args += ["-i", ssh_key]
        return sp.run(args + [f"{user}@{host}:{remote_path}", str(part)], capture_output=True,
                      text=True, timeout=timeout)

    try:
        probe = ssh(probe_command(remote_db), timeout=60)
    except sp.TimeoutExpired:
        return fail("no answer within 60 s")
    except OSError as e:
        return fail(f"ssh could not run: {e}")
    facts = after_marker(probe.stdout)
    if probe.returncode != 0 or "SQLITE" not in facts:
        return fail(f"ssh failed: {error_text(probe.stderr) or 'exit ' + str(probe.returncode)}")
    # All the probe printed on stderr is what the site prints for every
    # command (its banner): taken off what later commands say.
    banner = probe.stderr or ""
    size = _int(facts.get("SIZE"))
    if size is None:
        return fail(f"no database at {remote_db}")

    python = facts.get("SQLITE") != "1" and facts.get("PYBACKUP") == "1"
    if facts.get("SQLITE") != "1" and not python:
        # No sqlite3 there: the database itself is all there is to copy (as
        # before), checked whole before it replaces the cached copy.
        if refuse_live:
            if how is not None:
                how.append("no snapshot")
            return fail("no snapshot can be taken there (no sqlite3 command, and no python3 "
                        "3.7 or later)")
        if how is not None:
            how.append("live")
        logger.info(f"sqlite3 not available on {host}, copying the database directly")
        try:
            done = ssh(checkpoint_command(remote_db), timeout=60)
            if done.returncode != 0:
                logger.info(f"{host}: checkpoint before the copy failed "
                            f"({error_text(done.stderr, banner) or done.returncode}); copying anyway")
        except (sp.TimeoutExpired, OSError) as e:
            logger.info(f"{host}: checkpoint before the copy failed ({e}); copying anyway")
        return _fetch(scp, remote_db, size, part, local_copy, fail, banner=banner,
                      check_size=False, check_whole=True)

    directory = facts.get("DIR") or ""
    beside_ok = facts.get("DIRW") == "1" and bool(_SAFE_DIR.match(directory))
    tmp_free, dir_free = _kib(facts.get("TMPFREE_KB")), _kib(facts.get("DIRFREE_KB"))
    places = plan_snapshot(size, dir_free, beside_ok, tmp_free)
    if not places:
        return fail(f"no room for a snapshot: the database is {_gb(size)}; "
                    f"{_gb(dir_free)} free beside it, {_gb(tmp_free)} in {TMP}")
    token = tag or f"{os.getpid()}_{secrets.token_hex(3)}"
    tried = []
    seconds = limit(size, 180, SNAPSHOT_RATE)
    try:
        for n, where in enumerate(places):
            snap = (f"{directory}/.{PREFIX}{token}.db" if where == "beside"
                    else f"{TMP.rstrip('/')}/{PREFIX}{token}.db")
            tried.append(snap)
            result, err = None, ""
            for attempt in range(3):
                try:
                    result = ssh(snapshot_command(remote_db, snap, seconds, python=python),
                                 timeout=seconds)
                except sp.TimeoutExpired:
                    return fail(f"snapshot not finished within {seconds} s")
                if result.returncode == 0:
                    break
                err = error_text(result.stderr, banner)
                if result.returncode == 124 and not err:      # stopped by timeout there
                    err = f"not finished within {max(seconds - 10, 10)} s on the site"
                if "locked" not in err.lower() or attempt == 2:
                    break
                logger.info(f".backup on {host} hit a lock, retrying in 15s "
                            f"(attempt {attempt + 1}/3)")
                _remove(ssh, [snap])
                time.sleep(15)
            if result.returncode == 0:
                snap_size = _int(after_marker(result.stdout).get("SNAP"))
                return _fetch(scp, snap, snap_size, part, local_copy, fail, banner=banner,
                              check_size=True)
            if n + 1 < len(places) and any(w in err.lower() for w in _NO_ROOM):
                logger.info(f"{host}: no room for the snapshot in {snap.rsplit('/', 1)[0]} "
                            f"({err}); trying {places[n + 1]}")
                _remove(ssh, [snap])
                continue
            return fail(f"snapshot failed: {err or 'exit ' + str(result.returncode)}")
        return fail("snapshot failed")
    finally:
        # Always, whatever happened above; SQLite may have left a journal.
        if not _remove(ssh, tried):
            logger.warning(f"{host}: could not remove {', '.join(tried)}; removed after "
                           f"{STALE_MINUTES} min, at a later pull")


def copy_local(db_path: str, local_copy, why: list | None = None) -> bool:
    """A database on this host, copied with SQLite's backup (consistent while
    its collectors write) to ``local_copy``, which it replaces only when the
    copy is whole. False, with ``local_copy`` untouched and the reason in
    ``why``, otherwise."""
    import sqlite3
    local_copy = Path(local_copy)
    src_path = Path(os.path.expanduser(str(db_path)))
    if not src_path.is_absolute():
        # As a remote site's relative path is in its home: in the data
        # directory, not wherever nomad runs from (/ under systemd).
        from nomad.config import default_data_dir
        src_path = default_data_dir() / src_path
    part = local_copy.with_name(f"{local_copy.name}.{os.getpid()}.part")
    _remove_old_parts(local_copy)

    def fail(reason: str) -> bool:
        logger.warning(f"localhost: {reason}")
        if why is not None:
            why.append(reason)
        for p in (part, Path(str(part) + "-journal")):
            try:
                p.unlink()
            except OSError:
                pass
        return False
    if not src_path.is_file():
        return fail(f"no database at {src_path}")
    src = dst = None
    try:
        # Read-write when this account may write it (its own collectors'
        # database), so a journal a crashed writer left is rolled back
        # rather than read around; read-only otherwise.
        rw = os.access(src_path, os.W_OK) and os.access(src_path.parent, os.W_OK)
        src = sqlite3.connect(f"{src_path.resolve().as_uri()}?mode={'rw' if rw else 'ro'}",
                              uri=True, timeout=20)
        if part.exists():
            part.unlink()
        dst = sqlite3.connect(str(part))
        src.backup(dst)
        ok = dst.execute("PRAGMA quick_check").fetchone()
        dst.close()
        dst = None
        if not ok or ok[0] != "ok":
            return fail(f"the copy of {src_path} is not whole ({ok[0] if ok else 'no answer'})")
        os.replace(part, local_copy)
        return True
    except (sqlite3.Error, OSError) as e:
        return fail(f"could not copy {src_path}: {e}")
    finally:
        for c in (src, dst):
            if c is not None:
                try:
                    c.close()
                except sqlite3.Error:
                    pass


def _remove(ssh, snaps) -> bool:
    if not snaps:
        return True
    names = " ".join(shlex.quote(p) for s in snaps for p in (s, s + "-journal"))
    try:
        return ssh(f"rm -f {names}", timeout=30).returncode == 0
    except (sp.TimeoutExpired, OSError):
        return False


def _remove_old_parts(local_copy: Path) -> None:
    """Partial copies a hub run left when it was killed."""
    cutoff = time.time() - STALE_MINUTES * 60
    try:
        for p in local_copy.parent.glob(f"{local_copy.name}.*.part"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except OSError:
        pass


def _fetch(scp, remote_path, size, part, local_copy, fail, banner: str = "",
           check_size: bool = True, check_whole: bool = False) -> bool:
    """Copy to ``part``; only a complete copy replaces ``local_copy``."""
    seconds = limit(size, 120, COPY_RATE)
    try:
        copied = scp(remote_path, seconds)
    except sp.TimeoutExpired:
        return fail(f"copy not finished within {seconds} s")
    except OSError as e:
        return fail(f"scp could not run: {e}")
    if copied.returncode != 0:
        return fail(f"copy failed: {error_text(copied.stderr, banner)}")
    try:
        got = part.stat().st_size
        with open(part, "rb") as f:
            header = f.read(len(SQLITE_HEADER))
    except OSError as e:
        return fail(f"copy unreadable: {e}")
    if check_size and size is not None and got != size:
        return fail(f"copy incomplete: {got:,} of {size:,} bytes")
    if header != SQLITE_HEADER:
        return fail("copy is not an SQLite database")
    if check_whole:
        # A database copied while it was being written can look complete and
        # be malformed.
        import sqlite3
        try:
            # immutable: read as it is, leaving no -wal/-shm beside it
            conn = sqlite3.connect(f"file:{part}?mode=ro&immutable=1", uri=True)
            ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            conn.close()
        except sqlite3.Error:
            ok = False
        if not ok:
            return fail("copy of the live database is malformed (written while copied)")
    os.replace(part, local_copy)
    return True
