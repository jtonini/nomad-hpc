# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The part of a job `nomad submit` staged that runs on its compute node.

nomad submit sends such a job as a small bash script (nomad/submit/stage.py
writes it) that carries the person's own script and this file's text; the
job runs this with the node's own python3 (``python3 -I -c TEXT sub ...``),
so it needs nothing of nomad's on the node: standard library only, Python
3.6 or later. It runs as the person, like the rest of their job.

    dest      the job's own folder on /scratch (where its Slurm output is):
              what a stopped job left on the node is brought back there
    lock      lock a job's local folder for as long as the job lives (the
              lock is on a file descriptor the job's script holds)
    in        copy the job's folder to the node's local scratch, and note
              what was copied (the record "back" compares with)
    exec      run the person's script, with the signals a program starts with
    reap      stop what the job's script left running when it ended
    back      copy back what is new or changed since, then check each file
              copied (sha256 on both sides, read back from the server; a
              link's target; a folder)
    clean     remove the local folder, only once "back" checked everything
              and no process works in it any more
    discard   remove a local folder this run made and never ran in
    recover   for folders jobs left behind on this node (a copy back cut
              short: a cancel, a node that went down), unlocked: copy back
              into a new folder beside the results, check, then remove
    kill      signal a program and every process it started
    seconds   a Slurm time (squeue %L, TimeLimit) as seconds

Nothing here deletes anything but the local folder of a job whose copy back
was checked (or that never ran). A folder left behind keeps a note
(``.nomad-stage/info.json``) of where it goes back to, and what is
recovered never overwrites anything: it goes in a folder of its own.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

STATE = ".nomad-stage"          # nomad's notes, inside the local folder (never copied back)
INFO = "info.json"
MANIFEST = "manifest"
TOOL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
MEASURE_ENTRIES = 2000000        # entries counted before giving up on measuring a folder
CHUNK = 1 << 20
RECOVER_LIMIT = 3                # leftovers one job takes back

NO_ROOM, COPY_FAILED, BAD = 10, 11, 2


def say(msg):
    sys.stderr.write("nomad-stage: " + msg + "\n")
    sys.stderr.flush()


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%d B" % n


def node():
    return socket.gethostname().split(".")[0]


def which(name):
    for d in TOOL_PATH.split(":"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ── what a folder holds ─────────────────────────────────────────────────────

# What nomad itself puts at the top of a folder: never copied in.
NOMADS = ("nomad-recovered-", "nomad-conflicts-", "nomad-recover-", ".nomad-tmp-", ".nomad-lock")
FOLDER_LOCK = ".nomad-lock"      # in the folder a job came from: one copy back at a time
FOLDER_LOCK_WAIT = 900           # s a copy back waits for another's


def _skip(rel, excludes):
    if "/" not in rel and rel.startswith(NOMADS):
        return True
    for x in excludes:
        if rel == x or rel.startswith(x + "/"):
            return True
    return False


def walk(root, excludes=()):
    """(relative path, kind, size, mtime_ns, link target, inode, ctime_ns)
    of everything in ``root``, folders before what is in them; links not
    followed; nomad's own notes and ``excludes`` (relative paths) left out.
    Kinds: f (a file), l (a link), d (a folder), o (anything else: a pipe,
    a socket...)."""
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        path = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            raise OSError(e.errno, "can't read the folder %s: %s" % (path, e.strerror))
        subdirs = []
        for e in entries:
            rel = os.path.join(rel_dir, e.name) if rel_dir else e.name
            if rel == STATE or _skip(rel, excludes):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                target = os.readlink(e.path) if stat.S_ISLNK(st.st_mode) else None
            except FileNotFoundError:
                continue                            # gone since the folder was read
            mode = st.st_mode
            if stat.S_ISLNK(mode):
                kind = "l"
            elif stat.S_ISDIR(mode):
                kind = "d"
                subdirs.append(rel)
            elif stat.S_ISREG(mode):
                kind = "f"
            else:
                kind = "o"
            yield (rel, kind, st.st_size if kind in "fl" else 0, st.st_mtime_ns, target, st.st_ino,
                   st.st_ctime_ns)
        stack.extend(reversed(subdirs))


def measure(root, excludes=(), limit=MEASURE_ENTRIES):
    """(bytes in files, entries, complete)."""
    total = n = 0
    for entry in walk(root, excludes):
        n += 1
        if entry[1] == "f":
            total += entry[2]
        if n >= limit:
            return total, n, False
    return total, n, True


# ── copying ─────────────────────────────────────────────────────────────────

_TMP_N = [0]


def _tmp_beside(d, tmpdir=None):
    _TMP_N[0] += 1
    if tmpdir:
        return os.path.join(tmpdir, "%d-%d" % (os.getpid(), _TMP_N[0]))
    return os.path.join(os.path.dirname(d) or ".", ".%s.nomad-%d.tmp" % (os.path.basename(d)[:100], os.getpid()))


class Changed(OSError):
    """The destination changed just before the job's version would have
    replaced it."""


def _copy_entry(src_root, dst_root, rel, kind, target, tmpdir=None, expect=None):
    """One entry, from ``src_root`` to ``dst_root``. A file or a link is
    written beside under another name, flushed, then renamed in place: what
    was there stays whole until the new one is (a copy cut short leaves the
    old one, never half a file)."""
    s, d = os.path.join(src_root, rel), os.path.join(dst_root, rel)
    parent = os.path.dirname(d)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if kind == "d":
        if os.path.lexists(d) and not os.path.isdir(d):
            os.unlink(d)
        os.makedirs(d, exist_ok=True)
        shutil.copymode(s, d)
        return
    if os.path.isdir(d) and not os.path.islink(d):
        shutil.rmtree(d)
    tmp = _tmp_beside(d, tmpdir)
    try:
        if kind == "l":
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(target, tmp)
        elif kind == "f":
            shutil.copy2(s, tmp)
            with open(tmp, "rb") as f:
                os.fsync(f.fileno())
        else:
            return
        if expect is not None and not expect():
            raise Changed(0, "changed just now", d)
        os.replace(tmp, d)
    finally:
        if os.path.lexists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _dir_times(src_root, dst_root, rels):
    for rel in sorted(rels, key=lambda r: -r.count("/")):    # deepest first: a parent's time last
        try:
            st = os.stat(os.path.join(src_root, rel))
            os.utime(os.path.join(dst_root, rel), ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def copy_tree(src, dst, excludes=()):
    """Everything in ``src`` (less ``excludes``) into ``dst``: modes, times
    and links kept. rsync when the node has it, else Python's own copy."""
    rsync = which("rsync")
    if rsync and all(not re.search(r"[*?\[\\]", x) for x in excludes):
        argv = [rsync, "-a", "--exclude=/" + STATE] + ["--exclude=/%s*" % p for p in NOMADS]
        argv += ["--exclude=/" + x for x in excludes]
        r = subprocess.run(argv + [src.rstrip("/") + "/", dst.rstrip("/") + "/"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode != 0:
            raise OSError(0, "rsync: " + (r.stderr or "").strip()[-300:])
        return "rsync"
    dirs = []
    for rel, kind, _s, _m, target, _i, _c in walk(src, excludes):
        if kind == "o":
            continue
        _copy_entry(src, dst, rel, kind, target)
        if kind == "d":
            dirs.append(rel)
    _dir_times(src, dst, dirs)
    return "copy"


def relink(src, local):
    """A relative link that leads out of the job's folder would lead
    elsewhere from the copy: it becomes an absolute link to where it led
    (so reads and writes through it go where they always went). How many."""
    n = 0
    top = os.path.normpath(src)
    for rel, kind, _s, _m, target, _i, _c in walk(local):
        if kind != "l" or target.startswith("/"):
            continue
        led = os.path.normpath(os.path.join(top, os.path.dirname(rel), target))
        if led == top or led.startswith(top.rstrip("/") + "/"):
            continue
        p = os.path.join(local, rel)
        os.unlink(p)
        os.symlink(led, p)
        n += 1
    return n


def copy_list(src, dst, entries, tmpdir=None):
    """The given entries [(rel, kind, target)] from ``src`` to ``dst``; each
    file written in ``tmpdir`` (on dst's filesystem) first, then renamed
    into place."""
    rsync = which("rsync")
    rels = [e[0] for e in entries]
    if rsync and entries and all("\n" not in r for r in rels):
        listing = os.path.join(src, STATE, "back.list")
        with open(listing, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write("\0".join(rels) + "\0")
        argv = [rsync, "-a", "--from0", "--files-from=" + listing]
        if tmpdir:
            argv.append("--temp-dir=" + tmpdir)
        r = subprocess.run(argv + [src.rstrip("/") + "/", dst.rstrip("/") + "/"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if r.returncode == 0:
            return "rsync"
        say("rsync didn't copy everything (%s); copying again file by file"
            % (r.stderr or "").strip()[-200:])
    for rel, kind, target in sorted(entries, key=lambda e: (e[1] != "d", e[0])):
        try:
            _copy_entry(src, dst, rel, kind, target, tmpdir)
        except OSError as e:
            say("couldn't copy %s: %s" % (rel, e.strerror or e))
    _dir_times(src, dst, [e[0] for e in entries if e[1] == "d"])
    return "copy"


def sha256(path, fresh=False):
    """``fresh``: written to disk (to the server, on NFS) and read back from
    it, not from this node's cache."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if fresh:
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
            try:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError):
                pass
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def same(src_root, dst_root, rel, kind, target):
    """Whether ``rel`` arrived in ``dst_root`` as it is in ``src_root``."""
    d = os.path.join(dst_root, rel)
    try:
        if kind == "d":
            return os.path.isdir(d) and not os.path.islink(d)
        if kind == "l":
            return os.path.islink(d) and os.readlink(d) == target
        if kind == "f":
            s = os.path.join(src_root, rel)
            if os.path.islink(d) or not os.path.isfile(d) or os.path.getsize(d) != os.path.getsize(s):
                return False
            return sha256(s) == sha256(d, fresh=True)
    except OSError:
        return False
    return True


# ── notes kept in the local folder ──────────────────────────────────────────

def _state(local, name):
    return os.path.join(local, STATE, name)


def read_info(local):
    try:
        with open(_state(local, INFO), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_info(local, info):
    tmp = _state(local, INFO + ".new")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, sort_keys=True)
    os.replace(tmp, _state(local, INFO))


SRC_MANIFEST = "source"
HASHES = "hashes"
HASH_FILE_MAX = 64 << 20         # inputs up to this size have their content noted (for a merge)
HASH_TOTAL_MAX = 2 << 30         # ... and no more than this in all


def write_hashes(local):
    """sha256 of each small file as it was copied in: what a file that was
    only added to must still begin with."""
    out, total = {}, 0
    for rel, kind, size, _m, _t, _i, _c in walk(local):
        if kind != "f" or size > HASH_FILE_MAX or total + size > HASH_TOTAL_MAX:
            continue
        try:
            out[rel] = sha256(os.path.join(local, rel))
        except OSError:
            continue
        total += size
    with open(_state(local, HASHES), "w", encoding="utf-8", errors="surrogateescape") as f:
        json.dump(out, f)


def read_hashes(local):
    try:
        with open(_state(local, HASHES), encoding="utf-8", errors="surrogateescape") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(local, entries=None, name=MANIFEST):
    with open(_state(local, name), "w", encoding="utf-8", errors="surrogateescape") as f:
        for entry in (walk(local) if entries is None else entries):
            f.write(json.dumps(list(entry)) + "\n")


def read_manifest(local, name=MANIFEST):
    out = {}
    with open(_state(local, name), encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            row = json.loads(line)
            out[row[0]] = tuple(row[1:])
    return out


def state_of(path):
    """(kind, size, mtime_ns, target, inode, ctime_ns) of a path now, as in a
    manifest; None when there is nothing there. A file is opened first, so
    NFS looks again rather than answering from its cache."""
    try:
        st = os.lstat(path)
        if stat.S_ISREG(st.st_mode):
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
    except OSError:
        return None
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        kind = "l"
    elif stat.S_ISDIR(mode):
        kind = "d"
    elif stat.S_ISREG(mode):
        kind = "f"
    else:
        kind = "o"
    try:
        target = os.readlink(path) if kind == "l" else None
    except OSError:
        target = None
    return (kind, st.st_size if kind in "fl" else 0, st.st_mtime_ns, target, st.st_ino, st.st_ctime_ns)


def _prefix_sha(path, n):
    h = hashlib.sha256()
    left = n
    with open(path, "rb") as f:
        while left > 0:
            b = f.read(min(CHUNK, left))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest(), n - left


def _tail_sha(path, start):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def append_merge(local_path, src_path, was_size, was_sha):
    """A file of lines both the job and someone else (another job, the
    person) only added to since the job began (a log, a table of rows):
    both still begin with what it held then (``was_sha``: it held something,
    ending in a newline), so the job's new lines are added to the file as it
    is now. "merged": done and checked; "no": not only that, nothing
    written; (offset, bytes): written, but the file changed meanwhile
    (another writer), so it couldn't be checked."""
    if not was_sha or was_size <= 0:
        return "no"
    try:
        now = os.stat(src_path)
        mine = os.lstat(local_path)
        if not (stat.S_ISREG(now.st_mode) and stat.S_ISREG(mine.st_mode)):
            return "no"
        if now.st_size < was_size or mine.st_size <= was_size:
            return "no"
        with open(local_path, "rb") as f:
            f.seek(was_size - 1)
            if f.read(1) != b"\n":
                return "no"                          # not lines added after lines
        if _prefix_sha(local_path, was_size) != (was_sha, was_size) or \
                _prefix_sha(src_path, was_size) != (was_sha, was_size):
            return "no"
        with open(src_path, "rb") as f:
            if now.st_size > was_size:
                f.seek(now.st_size - 1)
                if f.read(1) != b"\n":
                    return "no"                      # theirs doesn't end a line: not rows
        with open(local_path, "rb") as f:
            f.seek(was_size)
            tail = f.read()
        with open(src_path, "ab") as f:
            before = os.fstat(f.fileno()).st_size
            f.write(tail)
            f.flush()
            os.fsync(f.fileno())
        if before == now.st_size and os.stat(src_path).st_size == before + len(tail) and \
                _tail_sha(src_path, before) == hashlib.sha256(tail).hexdigest():
            return "merged"
        return (before, len(tail))
    except OSError:
        return "no"


def changed(local, manifest):
    """What is new or changed in ``local`` since it was staged in:
    [(rel, kind, target)], folders first. A file is unchanged only when its
    kind, size, times, inode and link target are all as they were (a file
    replaced, or rewritten with its old time put back, shows in its inode or
    ctime)."""
    out = []
    for rel, kind, size, mtime, target, ino, ctime in walk(local):
        if kind == "o":
            continue
        was = manifest.get(rel)
        if was is None:
            out.append((rel, kind, target))
        elif kind == "d":
            if was[0] != "d":
                out.append((rel, kind, target))
        elif (kind, size, mtime, target, ino, ctime) != tuple(was):
            out.append((rel, kind, target))
    return out


# ── locks: a folder in use is never taken back ──────────────────────────────

def lock_path(base, name):
    return os.path.join(base, name + ".lock")


def try_lock(path):
    """An open, locked file descriptor on ``path``, or None when another
    process holds its lock (a job still alive)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _lock_is_local(folder):
    """The mount option that keeps a folder's locks on each node ("nolock",
    "local_lock=posix"...), when it is on NFS mounted so; else None."""
    try:
        real = os.path.realpath(folder)
        with open("/proc/self/mounts") as f:
            rows = [line.split() for line in f]
    except OSError:
        return None
    best, opts, kind = "", "", ""
    for row in rows:
        if len(row) < 4:
            continue
        point = row[1].replace("\\040", " ")
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind, opts = point, row[2], row[3]
    if not kind.startswith("nfs"):
        return None
    for o in opts.split(","):
        if o == "nolock" or o in ("local_lock=posix", "local_lock=all"):
            return o
    return None


class FolderLock(object):
    """One copy back at a time into a folder (POSIX lock on its .nomad-lock,
    which NFS's lock manager serves to every node). Where it can't be had
    (no lock service, no right to make the file) the copy back goes on
    without it, and says so."""

    def __init__(self, folder, wait=FOLDER_LOCK_WAIT):
        self.folder, self.wait, self.fd, self.held = folder, wait, None, False

    def __enter__(self):
        local = _lock_is_local(self.folder)
        if local:
            say("%s is on NFS mounted with %s: its locks aren't shared between nodes, so the copy back "
                "checks each file just before it goes in" % (self.folder, local))
            return self
        path = os.path.join(self.folder, FOLDER_LOCK)
        try:
            try:
                self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)
                try:
                    os.fchmod(self.fd, 0o666)       # a lab's folder: its members' jobs take turns too
                except OSError:
                    pass
            except FileExistsError:
                self.fd = os.open(path, os.O_RDWR)
        except OSError as e:
            say("no lock on %s (%s): the copy back checks each file just before it goes in"
                % (self.folder, e.strerror or e))
            return self
        t0, said = time.time(), False
        while True:
            try:
                fcntl.lockf(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    say("no lock on %s (%s): the copy back checks each file just before it goes in"
                        % (self.folder, e.strerror or e))
                    return self
            if not said:
                say("another job is copying back to %s: waiting for it" % self.folder)
                said = True
            if time.time() - t0 > self.wait:
                say("still waiting after %d s: the copy back goes on, checking each file just before it "
                    "goes in" % self.wait)
                return self
            time.sleep(1)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.lockf(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
        return False


def cmd_lock(argv):
    """Lock the file open on the job script's own descriptor --fd: the lock
    lasts as long as that descriptor (the job's processes) does."""
    a = _args(argv, "fd")
    try:
        fcntl.flock(int(a["fd"]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        say("couldn't lock (%s)" % (e.strerror or e))
        return 1
    return 0


# ── the subcommands ─────────────────────────────────────────────────────────

def _args(argv, *names, **multi):
    """--name value pairs; names in ``multi`` may repeat (a list)."""
    out = dict.fromkeys(names)
    out.update({n: [] for n in multi})
    i = 0
    while i < len(argv):
        k = argv[i]
        if not k.startswith("--") or i + 1 >= len(argv):
            raise SystemExit("nomad-stage: bad arguments: %r" % argv[i:])
        key = k[2:].replace("-", "_")
        if key in multi:
            out[key].append(argv[i + 1])
        elif key in out:
            out[key] = argv[i + 1]
        else:
            raise SystemExit("nomad-stage: unknown argument %s" % k)
        i += 2
    return out


_NAME_KEEP = re.compile(r"[^A-Za-z0-9._+-]+")


def safe_name(name):
    n = _NAME_KEEP.sub("_", (name or "").strip()).strip("._") or "job"
    return n[:64]


def cmd_dest(argv):
    """Where the job's results go: the folder its Slurm output is in, when
    nomad made it (under --base); else the job's folder nomad made there
    (it holds the job's .out); else a folder of its own made there now,
    as nomad names them (JOBID-NAME, then .2, .3...). An array task's go in
    task-N inside its array's folder."""
    a = _args(argv, "stdout", "base", "job", "name", "array_job", "task")
    base = (a["base"] or "").rstrip("/")
    if not base.startswith("/"):
        say("no folder for the results (%r)" % base)
        return BAD
    folder = None
    out = a["stdout"] or ""
    if out:
        d = os.path.dirname(out)
        if d.startswith(base + "/") and "/" not in d[len(base) + 1:] and os.path.isdir(d) \
                and not os.path.islink(d):
            folder = d
    owner = a["array_job"] or a["job"]
    first = os.path.join(base, "%s-%s" % (owner, safe_name(a["name"])))
    log = ("slurm-%s_%s.out" % (a["array_job"], a["task"])) if a["array_job"] else ("slurm-%s.out" % a["job"])
    try:
        if folder is None:
            for n in range(1, 100):                 # the one nomad made, with the job's .out in it
                cand = first if n == 1 else "%s.%d" % (first, n)
                if not os.path.isdir(cand):
                    break
                if os.path.exists(os.path.join(cand, log)) and not os.path.islink(cand):
                    folder = cand
                    break
        if folder is None:
            os.makedirs(base, mode=0o700, exist_ok=True)
            if a["array_job"]:
                os.makedirs(first, exist_ok=True)     # the array's tasks share it
                folder = first
            else:
                folder = first
                for n in range(2, 100):
                    try:
                        os.mkdir(folder)
                        break
                    except FileExistsError:
                        folder = "%s.%d" % (first, n)
                else:
                    say("too many folders named %s" % first)
                    return BAD
        if a["task"]:
            folder = os.path.join(folder, "task-" + a["task"])
            os.makedirs(folder, exist_ok=True)
    except OSError as e:
        say("couldn't make the folder for the results (%s: %s)" % (e.filename or base, e.strerror or e))
        return BAD
    print(folder)
    return 0


RECOVER_SH = """#!/bin/bash
# nomad: brings back what job %(job)s left on %(node)s, once it has ended.
if [ ! -e %(helper)s ]; then echo "nomad-stage: nothing was left on $(hostname -s)"; exit 0; fi
exec %(py)s -I %(helper)s recover --base %(base)s --only %(name)s
"""


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_in(argv):
    a = _args(argv, "src", "local", "dest", "jobdir", "job", "max", "reserve_min", "reserve_pct", "token",
              "python", "helper", exclude=True)
    src, local = a["src"].rstrip("/") or "/", a["local"].rstrip("/")
    excludes = [x.strip("/") for x in a["exclude"] if x.strip("/")]
    limit = int(a["max"] or 0)
    t0 = time.time()
    try:
        entries = []                                 # what the folder holds as the job begins
        total, complete = 0, True
        for entry in walk(src, excludes):
            entries.append(entry)
            if entry[1] == "f":
                total += entry[2]
            if len(entries) >= MEASURE_ENTRIES or (limit and total > limit):
                complete = False
                break
    except OSError as e:
        say(str(e.strerror or e))
        return COPY_FAILED
    n = len(entries)
    if not complete or (limit and total > limit):
        say("the job's folder %s is %s%s, over the %s nomad stages: it runs there instead"
            % (src, "more than " if not complete else "", human(total), human(limit)))
        return NO_ROOM
    base = os.path.dirname(local)
    try:
        os.makedirs(base, exist_ok=True)
        vfs = os.statvfs(base)
    except OSError as e:
        say("no local scratch folder %s (%s): it runs in %s" % (base, e.strerror or e, src))
        return NO_ROOM
    free = vfs.f_bavail * vfs.f_frsize
    size = vfs.f_blocks * vfs.f_frsize
    reserve = max(int(a["reserve_min"] or 0), size * int(a["reserve_pct"] or 0) // 100)
    if total + reserve > free:
        say("%s has %s free and keeps %s for the node: %s won't fit; it runs in %s"
            % (base, human(free), human(reserve), human(total), src))
        return NO_ROOM
    # A folder already there under this job's number (the caller holds its
    # lock): an earlier run of the same job (requeued) left it. Moved aside,
    # for a later job to bring back (never over this run's results).
    if os.path.lexists(local):
        old = read_info(local)
        if old is not None and old.get("state") == "copying in":
            _rmtree(local)                          # only a copy of the job's folder: nothing ran there
        else:
            aside = "%s.%d" % (local, int(time.time()))
            try:
                os.rename(local, aside)
                say("an earlier run of this job left %s: moved to %s, to be brought back later" % (local, aside))
            except OSError as e:
                say("%s is in the way (%s): it runs in %s" % (local, e.strerror or e, src))
                return NO_ROOM
    try:
        os.mkdir(local, 0o700)
        os.mkdir(os.path.join(local, STATE), 0o700)
        info = {"v": 1, "job": a["job"], "dest": a["dest"] or src, "jobdir": a["jobdir"] or "", "src": src,
                "node": node(), "staged": int(time.time()), "state": "copying in", "token": a["token"] or ""}
        write_info(local, info)
        write_manifest(local, entries, SRC_MANIFEST)
        how = copy_tree(src, local, excludes)
        os.chmod(local, 0o700)                     # rsync gives it the folder's own mode: kept private
        moved = relink(src, local)
        write_manifest(local)
        write_hashes(local)
        if a["helper"] and a["python"]:
            helper = _state(local, "helper.py")
            with open(helper, "w", encoding="utf-8") as f:
                f.write(a["helper"])
            with open(_state(local, "recover.sh"), "w", encoding="utf-8") as f:
                f.write(RECOVER_SH % {"job": a["job"], "node": node(), "helper": _q(helper),
                                      "py": _q(a["python"]), "base": _q(base),
                                      "name": _q(os.path.basename(local))})
        info["state"] = "running"
        write_info(local, info)
    except OSError as e:
        say("couldn't copy %s to %s (%s): it runs in %s instead" % (src, local, e.strerror or e, src))
        _rmtree(local)
        return COPY_FAILED
    say("copied %s (%s, %d entries) to %s on %s in %ds%s%s"
        % (src, human(total), n, local, node(), time.time() - t0,
           "" if how == "rsync" else " (no rsync here: Python's copy)",
           ("; %d link(s) that led out of the folder now lead there by their full path" % moved) if moved else ""))
    return 0


CONFLICTS = 3                    # back: copied, but some went aside (changed meanwhile where it ran)


def _sorted_out(local, dest, todo, srcman, hashes):
    """Of what the job made or changed: what goes back as it is, what is
    added at the end of a file others added to too, and what changed where
    it ran since the job began (someone else's change there is never undone:
    the job's version goes aside, in the job's folder). Also the modes to
    put back on files only someone's chmod or touch changed there, and the
    merges written but not checked (another writer)."""
    plain, merged, aside, modes, unsure = [], [], [], {}, []
    for rel, kind, target in todo:
        was = srcman.get(rel)
        here = os.path.join(dest, rel)
        now = state_of(here)
        if kind == "d":
            plain.append((rel, kind, target))      # folders: made if not there, never replaced
            continue
        if was is None and now is None:
            plain.append((rel, kind, target))      # new, and new there
            continue
        if was is not None and now is not None and tuple(was) == tuple(now):
            plain.append((rel, kind, target))      # there as the job found it: the job's change goes in
            continue
        if was is not None and now is not None and was[0] == now[0] == "f" and was[1] == now[1] \
                and hashes.get(rel):
            try:
                if sha256(here) == hashes[rel]:    # the same content: only its times or mode changed
                    plain.append((rel, kind, target))
                    modes[rel] = stat.S_IMODE(os.stat(here).st_mode)
                    continue
            except OSError:
                pass
        if kind == "f" and was is not None and was[0] == "f" and now is not None and now[0] == "f":
            how = append_merge(os.path.join(local, rel), here, was[1], hashes.get(rel))
            if how == "merged":
                merged.append(rel)
                continue
            if how != "no":
                unsure.append((rel, how))
        aside.append((rel, kind, target))
    return plain, merged, aside, modes, unsure


def _back(local, protect, into=None):
    """Copy back what is new or changed (to the folder it ran from, or
    ``into`` another); check it; note the result. 0: all back and checked;
    CONFLICTS: back and checked, some aside; 1: some not back (kept)."""
    info = read_info(local)
    if info is None or not info.get("dest"):
        say("%s has no note of where it goes back to: left as it is" % local)
        return 1
    dest = into or info["dest"]
    try:
        manifest = read_manifest(local)
    except (OSError, ValueError):
        say("%s has no record of what was copied in: everything in it goes back" % local)
        manifest = {}
    srcman = {}
    if into is None:
        try:
            srcman = read_manifest(local, SRC_MANIFEST)
        except (OSError, ValueError):
            srcman = None
    t0 = time.time()
    try:
        todo = changed(local, manifest)
        if todo or into is None:
            os.makedirs(dest, exist_ok=True)
    except OSError as e:
        say("couldn't look at %s or %s (%s): left as it is" % (local, dest, e.strerror or e))
        info["state"] = "copy back failed"
        write_info(local, info)
        return 1
    top = os.path.normpath(dest)
    rels = set()
    for p in protect or ():
        p = os.path.normpath(p)
        if p.startswith(top + "/"):
            rels.add(p[len(top) + 1:])
    blocked = [t for t in todo if t[0] in rels]
    todo = [t for t in todo if t[0] not in rels]
    merged, aside, aside_to, modes, unsure = [], [], None, {}, []
    tmpdir = os.path.join(dest, ".nomad-tmp-%s" % (info.get("job") or "job"))
    info["tmpdir"] = tmpdir
    write_info(local, info)
    with (FolderLock(dest) if into is None else _NoLock()) as lock:
        if into is None and srcman is not None:
            todo, merged, aside, modes, unsure = _sorted_out(local, dest, todo, srcman, read_hashes(local))
        elif into is None:
            say("no record of what %s held when the job began: what changed there meanwhile can't be told" % dest)
        try:
            os.makedirs(tmpdir, exist_ok=True)
        except OSError:
            tmpdir = None
        if todo and (lock.held or into is not None):
            copy_list(local, dest, todo, tmpdir)
        elif todo:
            # No lock held: file by file, each one checked just before it goes in.
            expected = {}
            srcm = srcman or {}
            for rel, kind, target in todo:
                expected[rel] = srcm.get(rel)
            moved = []
            for rel, kind, target in sorted(todo, key=lambda e: (e[1] != "d", e[0])):
                path = os.path.join(dest, rel)
                want = expected.get(rel)
                if kind == "d" or rel in modes:
                    check = None
                elif want is None:
                    check = lambda p=path: state_of(p) is None          # noqa: E731
                else:
                    check = lambda p=path, w=want: state_of(p) == tuple(w)  # noqa: E731
                try:
                    _copy_entry(local, dest, rel, kind, target, tmpdir, expect=check)
                except Changed:
                    moved.append((rel, kind, target))
                except OSError as e:
                    say("couldn't copy %s: %s" % (rel, e.strerror or e))
            if moved:
                todo = [t for t in todo if t not in moved]
                aside += moved
        for rel, mode in modes.items():
            try:
                os.chmod(os.path.join(dest, rel), mode)  # someone's chmod there meanwhile: kept
            except OSError:
                pass
        bad = [t for t in todo if not same(local, dest, *t)]
        if bad:
            say("%d of %d didn't check out after copying back: copying them again" % (len(bad), len(todo)))
            for rel, kind, target in bad:
                try:
                    _copy_entry(local, dest, rel, kind, target, tmpdir)
                except OSError as e:
                    say("couldn't copy %s: %s" % (rel, e.strerror or e))
            bad = [t for t in bad if not same(local, dest, *t)]
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if aside:
        aside_to = os.path.join(info.get("jobdir") or dest, "nomad-conflicts-%s" % (info.get("job") or "job"))
        for rel, kind, target in aside:
            try:
                _copy_entry(local, aside_to, rel, kind, target)
            except OSError as e:
                say("couldn't copy %s aside: %s" % (rel, e.strerror or e))
        bad += [t for t in aside if not same(local, aside_to, *t)]
    size = 0
    for rel, kind, _t in todo:
        if kind == "f":
            try:
                size += os.path.getsize(os.path.join(local, rel))
            except OSError:
                pass
    problems = [t[0] for t in bad] + [t[0] for t in blocked]
    if problems:
        info.update(state="copy back failed", left=problems[:50], left_count=len(problems))
        write_info(local, info)
        names = [t[0] for t in blocked]
        for rel in problems[:20]:
            say("not copied back: %s%s" % (rel, " (Slurm's output has that name there)" if rel in names else ""))
        say("%d new or changed item(s) are only in %s on %s: kept there"
            % (len(problems), local, info.get("node") or node()))
        return 1
    info.update(state="copied back", back=int(time.time()), back_count=len(todo) + len(merged),
                back_bytes=size, back_to=dest, merged=merged[:50], aside=[t[0] for t in aside][:50],
                aside_to=aside_to)
    write_info(local, info)
    say("copied back %d new or changed item(s) (%s) to %s and checked each (sha256) in %ds"
        % (len(todo), human(size), dest, time.time() - t0))
    for rel in merged[:20]:
        say("%s changed in %s while the job ran, only at its end, as the job's did: the job's new lines "
            "were added after them" % (rel, dest))
    unsure = dict(unsure)
    if aside:
        for rel, _k, _t in aside[:20]:
            if rel in unsure:
                at, n = unsure[rel]
                say("%s changed in %s while the job ran: the job's %d new byte(s) were added at byte %d, but "
                    "the file changed again meanwhile, so that couldn't be checked: look at its end; the "
                    "job's whole version is in %s" % (rel, dest, n, at, os.path.join(aside_to, rel)))
            else:
                say("%s changed in %s while the job ran: left as it is there; the job's version is in %s"
                    % (rel, dest, os.path.join(aside_to, rel)))
        return CONFLICTS
    return 0


class _NoLock(object):
    held = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def cmd_back(argv):
    """--protect-path: Slurm's own output files (never replaced by the job's
    copy of one)."""
    a = _args(argv, "local", protect_path=True)
    return _back(a["local"].rstrip("/"), a["protect_path"])


def _rmtree(path):
    def fix(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=fix)
    return not os.path.lexists(path)


def _in_use(local):
    """A process of the person's whose current folder is in ``local`` (a
    job it started, still working there)."""
    top = os.path.realpath(local)
    me = os.getpid()
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % d)
        except OSError:
            continue
        if cwd == top or cwd.startswith(top + "/"):
            return int(d)
    return None


def _sent_from(local, info):
    """Jobs of the person's sent from inside the local copy (their working
    folder would be gone with it): a pending one is moved to the same
    folder where the job came from; what can't be moved is returned."""
    squeue = which("squeue") or shutil.which("squeue")
    scontrol = which("scontrol") or shutil.which("scontrol")
    if not squeue:
        return []
    top = os.path.realpath(local)
    out = None
    for who in (["--me"], ["-u", str(os.getuid())]):
        try:
            r = subprocess.run([squeue, "--all"] + who + ["-h", "-o", "%i|%T|%Z"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode == 0:
            out = r.stdout
            break
    left = []
    for line in (out or "").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        job, state, wd = parts
        wd = os.path.normpath(wd)
        if not (wd == top or wd.startswith(top + "/")):
            continue
        there = os.path.normpath((info.get("src") or "/") + wd[len(top):])
        if state == "PENDING" and scontrol:
            try:
                r = subprocess.run([scontrol, "update", "jobid=" + job, "WorkDir=" + there],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                   timeout=30)
                if r.returncode == 0:
                    say("job %s was sent from %s: it now starts in %s" % (job, wd, there))
                    continue
            except (OSError, subprocess.TimeoutExpired):
                pass
        left.append(job)
    return left


def _clean(local):
    info = read_info(local)
    if info is None or info.get("state") != "copied back":
        say("%s wasn't checked back: kept" % local)
        return 1
    sent = _sent_from(local, info)
    if sent:
        say("job(s) %s were sent from %s and work in it: kept (this job's results are back and checked; "
            "what those jobs write there is brought back once they have ended)" % (", ".join(sent[:10]), local))
        info["state"] = "running"                  # what they change there goes back later
        write_info(local, info)
        return 1
    busy = _in_use(local)

    if busy:
        say("process %d still works in %s (a job it started?): kept; what it changed is brought back later"
            % (busy, local))
        info["state"] = "running"                  # its changes since go back later
        write_info(local, info)
        return 1
    if _rmtree(local):
        try:
            os.unlink(lock_path(os.path.dirname(local), os.path.basename(local)))
        except OSError:
            pass
        return 0
    say("couldn't remove all of %s" % local)
    return 1


def cmd_clean(argv):
    a = _args(argv, "local")
    return _clean(a["local"].rstrip("/"))


def cmd_discard(argv):
    """Remove the local folder this run (--token) made, when its script never
    ran there (the job was stopped while it was being copied in)."""
    a = _args(argv, "local", "token")
    local = a["local"].rstrip("/")
    info = read_info(local) if os.path.lexists(local) else None
    if info is None or not a["token"] or info.get("token") != a["token"]:
        return 0
    if _rmtree(local):
        try:
            os.unlink(lock_path(os.path.dirname(local), os.path.basename(local)))
        except OSError:
            pass
        return 0
    return 1


_LEFT = re.compile(r"job_(\d+)(\.\d+)?")


def cmd_recover(argv):
    """Folders the person's jobs left on this node, whose job is gone (its
    lock is free): what they changed goes back into a folder of its own
    (nomad-recovered-NODE-TIME, in the job's folder beside its Slurm
    output), is checked, then the folder is removed. --only: that one;
    else at most a few, this job's own aside."""
    a = _args(argv, "base", "self", "only", "limit")
    base = a["base"].rstrip("/")
    limit = int(a["limit"] or RECOVER_LIMIT)
    try:
        names = [a["only"]] if a["only"] else sorted(os.listdir(base))
    except OSError:
        return 0
    done = 0
    for name in names:
        m = _LEFT.fullmatch(name)
        if not m or (a["self"] and name == "job_" + a["self"]):
            continue
        local = os.path.join(base, name)
        try:
            st = os.lstat(local)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            continue
        info = read_info(local)
        if info is None:
            continue
        if done >= limit:
            say("more folders left on this node by earlier jobs: the next job takes them")
            break
        fd = try_lock(lock_path(base, name))
        if fd is None:
            continue                                # its job is still alive: never touched
        try:
            old_tmp = info.get("tmpdir")
            if old_tmp and os.path.basename(old_tmp).startswith(".nomad-tmp-") and os.path.isdir(old_tmp):
                shutil.rmtree(old_tmp, ignore_errors=True)   # a copy back cut short: its unfinished files
            if info.get("state") == "copying in":
                _rmtree(local)                      # only a copy of the job's folder: nothing ran there
                continue
            if info.get("state") != "copied back":
                # never over anything: a folder of its own, in the job's folder (else where it ran)
                into = os.path.join(info.get("jobdir") or info.get("dest") or "/nonexistent",
                                    "nomad-recovered-%s-%s" % (info.get("node") or node(),
                                                               time.strftime("%Y%m%d-%H%M%S")))
                say("job %s left %s: what it changed goes back to %s" % (m.group(1), local, into))
                if _back(local, None, into=into) not in (0, CONFLICTS):
                    done += 1
                    continue
            _clean(local)
            done += 1
        finally:
            os.close(fd)
    return 0


def _children(pid):
    kids = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % d) as f:
                s = f.read()
            ppid = int(s[s.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        for c in kids.get(p, []):
            if c not in out:
                out.append(c)
                todo.append(c)
    return out


def cmd_kill(argv):
    """Signal a program and everything it started (deepest first)."""
    a = _args(argv, "pid", "signal")
    pid, sig = int(a["pid"]), int(a["signal"])
    me = os.getpid()
    for p in [x for x in reversed(_children(pid)) if x != me] + [pid]:
        try:
            os.kill(p, sig)
        except OSError:
            pass
    return 0


def _job_pids(job):
    """This job's processes on this node: Slurm's own list (scontrol
    listpids), else the job's cgroup (only one that names the job)."""
    scontrol = which("scontrol") or shutil.which("scontrol")
    if scontrol:
        try:
            r = subprocess.run([scontrol, "listpids", job], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=30)
            if r.returncode == 0:
                pids = set()
                for line in r.stdout.splitlines():
                    w = line.split()
                    # not the extern step: a person's ssh session adopted into the job
                    if len(w) >= 3 and w[0].isdigit() and w[1] == job and w[2] not in ("extern", "4294967295"):
                        pids.add(int(w[0]))
                if pids:
                    return pids
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        with open("/proc/self/cgroup") as f:
            rows = [line.strip().split(":", 2) for line in f]
    except OSError:
        return set()
    for _h, ctrl, path in rows:
        if not re.search(r"(^|/)job_%s(/|$)" % re.escape(job), path):
            continue                                # not the job's own cgroup: nothing is guessed
        m = re.search(r"^(.*?/job_%s)(/|$)" % re.escape(job), path)
        top = m.group(1)
        roots = ["/sys/fs/cgroup" + top] if not ctrl else \
            ["/sys/fs/cgroup/%s%s" % (c, top) for c in ctrl.split(",")]
        pids = set()
        for root in roots:
            for d, subs, files in os.walk(root):
                subs[:] = [x for x in subs if x != "step_extern"]
                if "cgroup.procs" in files:
                    try:
                        with open(os.path.join(d, "cgroup.procs")) as f:
                            pids.update(int(x) for x in f.read().split() if x.isdigit())
                    except OSError:
                        pass
        if pids:
            return pids
    return set()


def cmd_reap(argv):
    """Stop what the job's script left running (processes it started in the
    background, out of its own tree), before the copy back reads its files:
    Slurm would stop them anyway when the job ends. Only this job's, only
    the person's own, never the job's script itself (--keep)."""
    a = _args(argv, "job", keep=True)
    keep = {os.getpid(), os.getppid()} | {int(k) for k in a["keep"] if k.isdigit()}
    uid = os.getuid()
    left = []
    for p in sorted(_job_pids(a["job"] or "")):
        if p in keep:
            continue
        try:
            if os.stat("/proc/%d" % p).st_uid != uid:
                continue
        except OSError:
            continue
        left.append(p)
    if not left:
        return 0
    say("the script left %d process(es) running: stopping them before copying back" % len(left))
    for p in left:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(os.path.exists("/proc/%d" % p) for p in left):
        time.sleep(0.2)
    for p in left:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return 0


def _restores(argv, env):
    """Leading --restore NAME "1:VALUE"|"0:" pairs (and --lc-ctype VALUE):
    variables put back in ``env`` as the job had them (nomad's own steps run
    without the job's LD_LIBRARY_PATH and LD_PRELOAD; Python sets LC_CTYPE
    when the locale is C). The arguments after them."""
    while argv:
        if argv[0] == "--restore" and len(argv) >= 3:
            name, was, argv = argv[1], argv[2], argv[3:]
        elif argv[0] == "--lc-ctype" and len(argv) >= 2:
            name, was, argv = "LC_CTYPE", argv[1], argv[2:]
        else:
            break
        if was.startswith("1:"):
            env[name] = was[2:]
        else:
            env.pop(name, None)
    return argv


def cmd_exec(argv):
    """Run the person's script as a program starts: every signal back to
    its default (a background job's shell ignores INT and QUIT; Python
    ignores PIPE and XFSZ), its environment as the job had it (--restore),
    then exec it in place."""
    env = dict(os.environ)
    argv = _restores(argv, env)
    if not argv:
        return BAD
    for n in range(1, signal.NSIG):
        if n in (signal.SIGKILL, signal.SIGSTOP):
            continue
        try:
            signal.signal(n, signal.SIG_DFL)
        except (OSError, RuntimeError, ValueError):
            pass
    try:
        os.execve(argv[0], argv, env)
    except OSError as e:
        say("couldn't run %s: %s" % (argv[0], e.strerror or e))
        return 127 if e.errno == errno.ENOENT else 126


# sbatch's options that take a value (nomad/submit/spec.py's, kept the same: a test checks)
SBATCH_LONG_VALUE = frozenset("""
account acctg-freq array batch bb bbf begin chdir cluster cluster-constraint
clusters comment constraint container container-id core-spec cores-per-socket cpu-freq cpus-per-gpu cpus-per-task
deadline delay-boot dependency distribution error exclude export export-file extra extra-node-info
gid gpu-bind gpu-freq gpus gpus-per-node gpus-per-socket gpus-per-task gres gres-flags hint
input job-name kill-on-invalid-dep licenses mail-type mail-user mcs-label mem mem-bind mem-per-cpu
mem-per-gpu mincpus network nodefile nodelist nodes ntasks ntasks-per-core ntasks-per-gpu ntasks-per-node
ntasks-per-socket ntasks-per-tres open-mode output partition power prefer priority profile qos
reservation segment signal sockets-per-node switches tasks-per-node thread-spec threads-per-core time time-min
tmp tres-bind tres-per-task uid wait-all-nodes wckey wrap
""".split())
SBATCH_SHORT_VALUE = frozenset("AabBcCDdeFGiJLMmNnopqStwx")


_PATH_OPTS = {"chdir": "chdir", "output": "output", "error": "error", "input": "input"}
_PATH_SHORT = {"D": "chdir", "o": "output", "e": "error", "i": "input"}


def _sbatch_paths(words):
    """In sbatch's options (getopt's way): ({chdir/output/error/input: the
    value given}, where the script is among them or None)."""
    got, i = {}, 0
    while i < len(words):
        w = words[i]
        if w == "--":
            return got, (i + 1 if i + 1 < len(words) else None)
        if w.startswith("--") and len(w) > 2:
            name, eq, value = w[2:].partition("=")
            full = [n for n in SBATCH_LONG_VALUE if n.startswith(name)]
            which_ = _PATH_OPTS.get(name) or (_PATH_OPTS.get(full[0]) if len(full) == 1 else None)
            takes = name in SBATCH_LONG_VALUE or len(full) == 1
            if which_ and takes:
                if eq:
                    got[which_] = value
                elif i + 1 < len(words):
                    got[which_] = words[i + 1]
                    i += 1
            elif not eq and takes:
                i += 1
            i += 1
            continue
        if w.startswith("-") and len(w) > 1:
            j = 1
            while j < len(w):
                c = w[j]
                if c in SBATCH_SHORT_VALUE:
                    value = w[j + 1:].lstrip("=")
                    if not value and i + 1 < len(words):
                        value = words[i + 1]
                        i += 1
                    if c in _PATH_SHORT:
                        got[_PATH_SHORT[c]] = value
                    break
                j += 1
            i += 1
            continue
        return got, i
    return got, None


def _sbatch_chdir(words):
    """In sbatch's options (getopt's way): (the -D/--chdir given or None,
    where the script is among them or None)."""
    got, at = _sbatch_paths(words)
    return got.get("chdir"), at


def _script_paths(path):
    """The -D/--chdir, -o, -e, -i a job script's own #SBATCH lines give."""
    import shlex
    got = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                s = line.strip()
                if n == 0 and s.startswith("#!"):
                    continue
                if not s:
                    continue
                if not s.startswith("#"):
                    break
                if line.startswith("#SBATCH"):
                    try:
                        g, _at = _sbatch_paths(shlex.split(line[len("#SBATCH"):], comments=True))
                    except ValueError:
                        continue
                    got.update(g)
    except OSError:
        return {}
    return got


def _script_chdir(path):
    """The -D/--chdir a job script's own #SBATCH lines give."""
    import shlex
    chdir = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                s = line.strip()
                if n == 0 and s.startswith("#!"):
                    continue
                if not s:
                    continue
                if not s.startswith("#"):
                    break
                if line.startswith("#SBATCH"):
                    try:
                        c, _at = _sbatch_chdir(shlex.split(line[len("#SBATCH"):], comments=True))
                    except ValueError:
                        continue
                    if c is not None:
                        chdir = c
    except OSError:
        return None
    return chdir


def cmd_sbatch_shim(argv):
    """sbatch, run from inside a job's local copy: when the job it sends
    would work in the copy (gone once this job ends), it is sent to work in
    the same folder where this job came from; else exactly as it is."""
    argv = _restores(argv, os.environ)              # the real sbatch, and the job it sends, get them back
    if len(argv) < 3:
        return BAD
    shimdir, local, submit, words = argv[0], os.path.realpath(argv[1]), argv[2], argv[3:]
    real = None
    for d in os.environ.get("PATH", "").split(":"):
        if not d or os.path.realpath(d) == os.path.realpath(shimdir):
            continue
        cand = os.path.join(d, "sbatch")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            real = cand
            break
    if not real:
        sys.stderr.write("sbatch: command not found\n")
        return 127
    given, at = _sbatch_paths(words)
    own = _script_paths(words[at]) if at is not None else {}
    paths = dict(own)
    paths.update(given)                              # the command line's win
    cwd = os.path.realpath(os.getcwd())

    def inside(p):
        return p == local or p.startswith(local + "/")
    chdir = paths.get("chdir")
    eff = os.path.normpath(os.path.join(cwd, os.path.expanduser(chdir)) if chdir else cwd)
    flags = []
    if inside(eff):
        there = os.path.normpath(submit + eff[len(local):])
        if not os.path.isdir(there):
            sys.stderr.write(
                "sbatch (nomad): this job runs in its node's local scratch (%s), and the job sent here would "
                "work in %s, which is only in that copy: it would be %s, which isn't there yet (this job's "
                "results come back when it ends). Send the job that sends jobs with nomad submit "
                "--no-localscratch.\n" % (local, eff, there))
            return 1
        flags.append("--chdir=" + there)
    for name in ("output", "error", "input"):
        v = paths.get(name)
        if v and v.startswith("/") and inside(os.path.normpath(v)):
            flags.append("--%s=%s" % (name, os.path.normpath(submit + os.path.normpath(v)[len(local):])))
    if flags:
        words = words[:at] + flags + words[at:] if at is not None else words + flags
        say("a job sent from this job's local copy (%s) is sent with %s (the copy goes when this job ends)"
            % (local, " ".join(flags)))
    os.execv(real, [real] + words)


_TIME = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)")



def cmd_seconds(argv):
    """A Slurm time (D-HH:MM:SS, HH:MM:SS, MM:SS, D-HH) as seconds; -1
    when there is no limit or it can't be read."""
    text = (argv[0] if argv else "").strip()
    m = _TIME.fullmatch(text)
    if not m:
        m2 = re.fullmatch(r"(\d+)-(\d+)", text)
        print(((int(m2.group(1)) * 24 + int(m2.group(2))) * 3600) if m2 else -1)
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    print(((d * 24 + h) * 60 + mi) * 60 + s)
    return 0


COMMANDS = {"dest": cmd_dest, "lock": cmd_lock, "in": cmd_in, "exec": cmd_exec, "reap": cmd_reap,
            "back": cmd_back, "clean": cmd_clean, "discard": cmd_discard, "recover": cmd_recover,
            "kill": cmd_kill, "seconds": cmd_seconds, "sbatch-shim": cmd_sbatch_shim}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("usage: stage_helper {%s} ...\n" % ",".join(sorted(COMMANDS)))
        return BAD
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
