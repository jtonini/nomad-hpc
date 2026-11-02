# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad-gate: the one way the hub acts on a cluster as a person (1.25).

The Console (on the hub) lets a signed-in person size, submit, send again,
list and cancel their own jobs on a cluster. It holds no key that can do
anything as anyone: it reaches the cluster's nomad account (installer on
spydur) with a key of its own whose authorized_keys line forces
`nomad-gate serve`, so that key can do nothing but ask the gate. The gate
does what was asked as the person -- `sudo -n -u PERSON nomad-gate run
...`, with the cluster account's own sudo rights -- and only what is on its
list:

    version                     the gate's and nomad's versions; with --user, as that person
    size   --script PATH        nomad size --json: what the job should ask for
           --job N              ... for a job of theirs that ran, to send it again
    submit --script PATH --sha256 HEX
                                nomad submit --yes, with the person's own values if any
                                (--cpus --mem --time --gpus --partition --keep
                                --localscratch/--no-localscratch)
    again  --job N --sha256 HEX nomad submit --yes --again N (the same values may be given)
    jobs                        the person's jobs now (squeue -u PERSON)
    job    --job N              one job of theirs: Slurm's record of it, and the end of its output
    cancel --job N              scancel N (Slurm refuses another's job anyway)
    ls     --dir PATH           what a folder holds (names, kinds, sizes, times): to pick a script
    show   --script PATH        a job script's text and its sha256, to look at before sending it

Every value is checked here, twice (as the cluster account, then as the
person); nothing is handed to a shell; the script sent must be the
person's own file, with the very text they were shown (--sha256, 1.25.1):
nomad checks it once more and gives sbatch that text itself, on its
standard input (1.25.2), so nothing changes it on the way. A script, folder
or account others could change is said (``warning``), not refused; a
Slurm program (sbatch, squeue, ...) others could have put on the person's
PATH is refused, before any of them runs: from the Console each is found
once and run by its full path (nomad.submit.programs). nomad runs in the person's login environment (`bash -l`,
as when they log in and type it) and in the script's folder, so the job
gets the environment it would get by hand. Each request is logged to the
cluster's syslog (who, what, the job it made), and each answer is one JSON
document on one line.

Installed beside nomad for users (/usr/local/sw/nomad/bin/nomad-gate, by
scripts/install_nomad_for_users.sh). It refuses to serve when it, or the
nomad and Python it runs, could be changed by anyone but its owner or root.
"""
import fcntl
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import time

VERSION = 1
MAX_SCRIPT = 256 * 1024
MAX_SAID = 16 * 1024            # what nomad said, kept in an answer
MAX_READ = 4 * 1024 * 1024      # what nomad may print (size's JSON is the largest)
MAX_LS = 500
MIN_UID = 1000
NEVER = {"root", "installer", "slurm", "munge", "nomad", "zeus", "nobody", "nfsnobody", "daemon", "bin", "sys"}
NOBODY_UIDS = {65534, 4294967294, 4294967295}
TOOL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
STEP_TIMEOUT = 300              # a step as the person: nomad size, squeue, ...
SEND_TIMEOUT = 600              # nomad submit (its own sbatch may take 300 s, after sizing)
SERVE_TIMEOUT = SEND_TIMEOUT + 60
AT_ONCE = 4                     # requests served at once on this account; more are refused
WAIT_FOR_SLOT = 20              # seconds a request waits for one
PREFLIGHT_TIMEOUT = 60          # sudo -n -l
CHECKED_FOR = 60                # seconds a passed install check is taken as still true
# The most a request can take here; the hub waits a minute more (remote.SSH_TIMEOUT).
LONGEST = WAIT_FOR_SLOT + PREFLIGHT_TIMEOUT + SERVE_TIMEOUT
GATE = "nomad-gate"

_NAME = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
_JOB = re.compile(r"[0-9]{1,12}(?:_[0-9]{1,9})?")
_INT = re.compile(r"[0-9]{1,5}")
_MEM = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,3})?[KMGTkmgt]?")
_TIME = re.compile(r"(?:[0-9]{1,3}-)?[0-9]{1,4}(?::[0-9]{1,2}){0,2}")
_PARTITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# A full path, without control characters (C0, C1), lone surrogates (bytes that
# aren't UTF-8), line or paragraph separators, or invisible and direction marks.
_PATH = re.compile("/[^\x00-\x1f\x7f-\x9f\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                   "\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb"
                   "\ud800-\udfff\U000e0000-\U000e0fff]{0,4095}")
_SHA = re.compile(r"[0-9a-f]{64}")
_CHECK = {"job": _JOB, "int": _INT, "mem": _MEM, "time": _TIME, "partition": _PARTITION,
          "path": _PATH, "sha": _SHA}

_CHOICES = {"--cpus": "int", "--mem": "mem", "--time": "time", "--gpus": "int",
            "--partition": "partition", "--keep": "flag", "--localscratch": "flag",
            "--no-localscratch": "flag"}
# op -> {option: kind}; kinds: path, job, int, mem, time, partition, sha, flag
OPS = {
    "version": {},
    "size": {"--script": "path", "--job": "job"},
    "submit": {"--script": "path", "--sha256": "sha", **_CHOICES},
    "again": {"--job": "job", "--sha256": "sha", **_CHOICES},
    "jobs": {},
    "job": {"--job": "job"},
    "cancel": {"--job": "job"},
    "ls": {"--dir": "path"},
    "show": {"--script": "path"},
}
REQUIRED = {"submit": ["--script", "--sha256"], "again": ["--job", "--sha256"], "job": ["--job"],
            "cancel": ["--job"], "ls": ["--dir"], "show": ["--script"]}


class Refused(Exception):
    """A request the gate won't do: said in the answer, never done."""


def answer(doc: dict, code: int = 0) -> int:
    sys.stdout.write(json.dumps(doc, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


def refuse(why: str, code: int = 2) -> int:
    return answer({"ok": False, "error": why}, code)


def parse(words: list) -> tuple:
    """(op, {option: value}) from a request's words (after the gate's own
    name), each value checked; else Refused."""
    if not words or words[0] not in OPS:
        raise Refused(f"not something the gate does: {(words[0] if words else '(nothing)')[:40]}")
    op, allowed = words[0], OPS[words[0]]
    got, i = {}, 1
    while i < len(words):
        w = words[i]
        kind = "user" if w == "--user" else allowed.get(w)
        if kind is None:
            raise Refused(f"{op} doesn't take {w[:40]}")
        if w in got:
            raise Refused(f"{w} given twice")
        if kind == "flag":
            got[w] = True
            i += 1
            continue
        if i + 1 >= len(words):
            raise Refused(f"{w} without its value")
        v = words[i + 1]
        if kind == "user":
            if not _NAME.fullmatch(v):
                raise Refused("--user: not a user name")
        elif not _CHECK[kind].fullmatch(v):
            raise Refused(f"{w} {v[:60]!r}: not a {kind} nomad takes")
        elif kind == "path" and ("//" in v or any(x in (".", "..") for x in v.split("/"))):
            raise Refused(f"{w} {v[:60]!r}: give the path without . or .. in it")
        got[w] = v
        i += 2
    for r in REQUIRED.get(op, []):
        if r not in got:
            raise Refused(f"{op} needs {r}")
    if op == "size" and ("--script" in got) == ("--job" in got):
        raise Refused("size needs --script or --job (one of them)")
    if "--localscratch" in got and "--no-localscratch" in got:
        raise Refused("--localscratch or --no-localscratch, not both")
    if "--cpus" in got and not 1 <= int(got["--cpus"]) <= 4096:
        raise Refused("--cpus: 1 to 4096")
    if "--gpus" in got and int(got["--gpus"]) > 64:
        raise Refused("--gpus: at most 64")
    return op, got


def words_of(op: str, got: dict) -> list:
    """The request as words again, without --user (what serve passes on)."""
    out = [op]
    for k, v in got.items():
        if k != "--user":
            out += [k] if v is True else [k, v]
    return out


# ── what the gate runs from: changeable by no one but its owner ─────────────

def unsafe(path: str, owners: set, private: set = frozenset()) -> str | None:
    """Why ``path`` isn't safe to run from (or to take as someone's own),
    else None: it and each folder above it -- as named, and as they really
    are once links are followed -- must belong to one of ``owners`` and be
    writable by no one else (a folder above it may be sticky, as /tmp is).
    ``private``: groups that hold no one else (a person's own group), whose
    writing is theirs."""
    p = os.path.abspath(path)
    for q in dict.fromkeys((p, os.path.realpath(p))):
        why = _chain(q, owners, private)
        if why:
            return why
    return None


def private_groups(name: str) -> set:
    """The person's own group, when there is one: their primary group, named
    as they are, with no other member listed (a user private group); an
    empty set otherwise. (Another account with it as its primary group can't
    be seen from here: the convention is that none has.)"""
    import grp
    try:
        g = grp.getgrnam(name)
        pw = pwd.getpwnam(name)
    except KeyError:
        return set()
    return {g.gr_gid} if set(g.gr_mem) <= {name} and pw.pw_gid == g.gr_gid else set()


def _acl(p: str) -> bool:
    """Whether ``p`` has a POSIX access list (its group bits are then the
    list's mask, and others may be named in it)."""
    try:
        os.getxattr(p, "system.posix_acl_access", follow_symlinks=False)
        return True
    except OSError:
        return False


def _chain(p: str, owners: set, private: set = frozenset()) -> str | None:
    first = True
    while True:
        try:
            st = os.lstat(p)
        except OSError as e:
            return f"{p}: {e.strerror}"
        if st.st_uid not in owners:
            return f"{p} belongs to uid {st.st_uid}"
        sticky_dir = not first and stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX
        mine = st.st_gid in private and not (st.st_mode & 0o020 and _acl(p))
        others = st.st_mode & (0o002 if mine else 0o022)
        if not stat.S_ISLNK(st.st_mode) and others and not sticky_dir:
            return f"{p} can be changed by others (mode {stat.S_IMODE(st.st_mode):o})"
        if p == "/":
            return None
        p, first = os.path.dirname(p), False


def link_steps(path: str, limit: int = 40) -> list:
    """``path``, each path it becomes as its links are followed one at a
    time (any part of it a link), and where it really is: each holds a
    folder someone could change to change where it leads."""
    p = os.path.abspath(path)
    out = [p]
    for _ in range(limit):
        parts = p.split("/")[1:]
        cur, step = "/", None
        for i, part in enumerate(parts):
            here = os.path.join(cur, part)
            try:
                if not stat.S_ISLNK(os.lstat(here).st_mode):
                    cur = here
                    continue
                target = os.readlink(here)
            except OSError:
                break
            step = os.path.normpath(os.path.join(cur, target, *parts[i + 1:]))
            break
        if step is None or step in out:
            break
        out.append(step)
    real = os.path.realpath(p)
    return out if real in out else out + [real]


def _first_open(path: str, owners: set | None, private: set = frozenset()):
    """The first of ``path`` and the folders above it -- as named, as each
    link in it leads (link_steps), and as it really is -- that someone else
    could change: (where, how) -- how is ("owner", uid) when an account not
    in ``owners`` owns it (None: any owner will do), or ("mode", who, mode)
    when others may write it (a folder above it may be sticky, as /tmp is,
    when its owner is one of ``owners``). None when no one else can."""
    for q in link_steps(path):
        p, first = q, True
        while True:
            try:
                st = os.lstat(p)
            except OSError:
                break
            if owners is not None and st.st_uid not in owners:
                return p, ("owner", st.st_uid)
            if not stat.S_ISLNK(st.st_mode):
                sticky_dir = not first and stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX
                mine = st.st_gid in private and not (st.st_mode & 0o020 and _acl(p))
                others = st.st_mode & (0o002 if mine else 0o022)
                if others and not sticky_dir:
                    who = "anyone" if st.st_mode & 0o002 else f"anyone in the group {_group_of(st.st_gid)}"
                    return p, ("mode", who, stat.S_IMODE(st.st_mode))
            if p == "/":
                break
            p, first = os.path.dirname(p), False
    return None


def exposure(path: str, uid: int, private: set = frozenset()) -> str | None:
    """What could let someone else change ``path`` (a person's own script):
    the first of it and the folders above it, as named and as they really
    are, that another account owns or others may write; with the command
    that closes it. None when only they (and root) can."""
    hit = _first_open(path, {0, uid}, private)
    if hit is None:
        return None
    p, how = hit
    if how[0] == "owner":
        return f"{p} belongs to {_name_of(how[1])}, who can change what is in it"
    return (f"{p} can be changed by {how[1]} (mode {how[2]:o}): they could change "
            f"your files; to close it: chmod go-w {shlex.quote(p)}")


# What in a person's own account others could change so as to run programs
# as them (1.25.2): their home, what bash reads when they log in (the gate
# runs nomad in their login, as when they log in), and ~/.local (programs
# on PATH, Python's own packages, nomad's records). Said, not refused: it is
# open with or without the Console, and closes when they (or the site)
# close it.
LOGIN_FILES = (".bash_profile", ".bash_login", ".profile", ".bashrc")
LOCAL_SPOTS = (".local", ".local/bin", ".local/lib", ".local/share", ".local/share/nomad",
               ".local/share/nomad/staged", ".local/share/nomad/staged/jobs")


def account_open(home: str, uid: int, private: set = frozenset()) -> str | None:
    """The first place in the person's account (``home``) that someone else
    could change, so as to run programs as them; with the command that
    closes it. None when there is none."""
    import glob
    local = os.path.join(home, ".local")
    spots = [home] + [os.path.join(home, f) for f in LOGIN_FILES] + [os.path.join(home, d) for d in LOCAL_SPOTS]
    spots += sorted(glob.glob(os.path.join(glob.escape(local), "lib", "python*", "site-packages")))[:8]
    for spot in spots:
        if not os.path.lexists(spot):
            continue
        hit = _first_open(spot, {0, uid}, private)
        if hit is None:
            continue
        p, how = hit
        inside = p == home or p.startswith(home.rstrip("/") + "/")
        if how[0] == "owner":
            return (f"{p} belongs to {_name_of(how[1])}, who could run programs as you, with or without the "
                    "Console")
        if p == local or p.startswith(local + "/"):
            fix = f"chmod -R go-w {shlex.quote(local)}"
        elif inside:
            fix = f"chmod go-w {shlex.quote(p)}"
        else:
            fix = f"the cluster's admins: chmod go-w {shlex.quote(p)}"
        return (f"{p} can be changed by {how[1]} (mode {how[2]:o}): they could run programs as you, with or "
                f"without the Console; to close it: {fix}")
    return None


def trusted_owners(uid: int | None = None) -> set:
    """Who may own a program nomad runs as the person, and the folders it
    is in: root, the person, the account nomad itself is installed by
    (installer, on spydur), and Slurm's own account."""
    out = {0, os.getuid() if uid is None else uid}
    try:
        out.add(os.stat(os.path.dirname(os.path.abspath(__file__))).st_uid)
    except OSError:
        pass
    try:
        out.add(pwd.getpwnam("slurm").pw_uid)
    except KeyError:
        pass
    return out


def program_open(path: str, private: set = frozenset(), owners: set | None = None) -> str | None:
    """Why a program nomad is to run (sbatch, found on the person's PATH)
    could have been put there by someone else: it, a folder above it, or a
    link on the way to it, that another account owns (trusted_owners) or
    others may write. None when no one else could."""
    hit = _first_open(path, trusted_owners() if owners is None else owners, private)
    if hit is None:
        return None
    p, how = hit
    if how[0] == "owner":
        return f"{p} belongs to {_name_of(how[1])}"
    return f"{p} can be changed by {how[1]} (mode {how[2]:o})"


def _name_of(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _group_of(gid: int) -> str:
    import grp
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return f"gid {gid}"


def tree_unsafe(top: str, owners: set) -> str | None:
    """Why what is in a folder (a release of nomad: its environment and its
    Python) isn't safe, else None; where a link in it leads is checked too."""
    seen = set()
    for d, dirs, files in os.walk(top):
        for n in dirs + files:
            x = os.path.join(d, n)
            try:
                st = os.lstat(x)
            except OSError:
                continue
            if st.st_uid not in owners:
                return f"{x} belongs to uid {st.st_uid}"
            if stat.S_ISLNK(st.st_mode):
                real = os.path.realpath(x)
                if real not in seen:
                    seen.add(real)
                    why = unsafe(real, owners)
                    if why:
                        return f"{x} leads to {real}: {why}"
            elif st.st_mode & 0o022:
                return f"{x} can be changed by others (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def self_check(me: str) -> str | None:
    """Whether the gate is installed as it should be (a check for a mistake
    in how it was installed): the gate, the nomad beside it, the Python both
    run and all of nomad's environment and that Python's own files, owned by
    root or the gate's owner and writable by no one else."""
    try:
        owners = {0, os.stat(me).st_uid}
    except OSError as e:
        return f"{me}: {e.strerror}"
    for p in (me, os.path.join(os.path.dirname(me), "nomad"), sys.executable):
        why = unsafe(p, owners)
        if why:
            return why
    for top in dict.fromkeys((os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix))):
        why = unsafe(top, owners) or tree_unsafe(top, owners)
        if why:
            return why
    return None


def checked(me: str, place: str) -> str | None:
    """self_check, its pass taken as still true for CHECKED_FOR seconds (kept
    in the gate's own folder; every ls and jobs of the Console's would
    otherwise walk the whole release)."""
    mark = os.path.join(place, "check.ok")
    key = f"{me}\n{os.path.realpath(sys.prefix)}\n"
    try:
        st = os.stat(mark)
        if time.time() - st.st_mtime < CHECKED_FOR and st.st_uid == os.getuid():
            with open(mark) as f:
                if f.read() == key:
                    return None
    except OSError:
        pass
    why = self_check(me)
    if why is None:
        try:
            tmp = f"{mark}.{os.getpid()}"
            with open(tmp, "w") as f:
                f.write(key)
            os.replace(tmp, mark)
        except OSError:
            pass
    return why


def clean(text: str) -> str:
    """``text`` as one line of printable ASCII (anything else escaped), as it goes to the log."""
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape", "backslashreplace").decode("ascii")
                   for c in str(text))


def _log(msg: str) -> None:
    try:
        import syslog
        syslog.openlog(GATE, 0, getattr(syslog, "LOG_AUTHPRIV", syslog.LOG_USER))
        syslog.syslog(syslog.LOG_INFO, clean(msg)[:900])
    except Exception:                              # noqa: BLE001 -- logging never stops the gate
        pass


def _uid() -> int:
    return os.getuid()


def _account() -> str:
    try:
        return pwd.getpwuid(_uid()).pw_name
    except KeyError:
        return str(_uid())


def _version() -> dict:
    from nomad import __version__
    return {"ok": True, "gate": VERSION, "nomad": __version__, "account": _account()}


# ── serve: as the cluster's account, for the hub's key (a forced command) ───

def person(name: str | None):
    """The account a request is for: a person's, not a system one."""
    if not name or not _NAME.fullmatch(name):
        raise Refused("--user: not a user name")
    if name in NEVER:
        raise Refused(f"not for {name}")
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        raise Refused(f"no account {name} here")
    if pw.pw_uid < MIN_UID or pw.pw_uid in NOBODY_UIDS:
        raise Refused(f"not for a system account ({name})")
    if pw.pw_uid == os.getuid():
        raise Refused(f"not for {name}: the gate's own account")
    return pw


def state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".local", "state", GATE)


def _slot(place: str | None = None):
    """One of AT_ONCE places for a request on this account (waiting up to
    WAIT_FOR_SLOT s for one); the lock to release, or None when all are taken."""
    place = place or state_dir()
    os.makedirs(place, mode=0o700, exist_ok=True)
    deadline = time.time() + WAIT_FOR_SLOT
    while True:
        for i in range(AT_ONCE):
            fd = os.open(os.path.join(place, f"slot{i}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def _sudo() -> str:
    for d in TOOL_PATH.split(":"):
        if os.access(os.path.join(d, "sudo"), os.X_OK):
            return os.path.join(d, "sudo")
    raise Refused("no sudo here")


def serve(me: str, env=None, run_=None, slot_=None, check_=None) -> int:
    env = os.environ if env is None else env
    run_ = run_ or subprocess.run
    original = env.get("SSH_ORIGINAL_COMMAND")
    if original is None:
        return refuse("serve is for sshd (the hub key's authorized_keys command=); to try the gate "
                      "as yourself: nomad-gate run OP ...")
    try:
        words = shlex.split(original)
    except ValueError:
        return refuse("the request's quotes don't close")
    if words and os.path.basename(words[0]) == GATE:
        words = words[1:]
    try:
        op, got = parse(words)
        pw = None if op == "version" and "--user" not in got else person(got.get("--user"))
        sudo = _sudo() if pw else None
    except Refused as e:
        _log(f"refused: {e} ({original[:200]})")
        return refuse(str(e))
    who = pw.pw_name if pw else "-"
    asked = " ".join(shlex.quote(w) for w in words_of(op, got))
    plain = {"PATH": TOOL_PATH, "LANG": "C", "LC_ALL": "C"}     # sudo's own words, the same everywhere
    sends = op in ("submit", "again")
    try:
        slot = slot_() if slot_ else _slot()
    except OSError as e:
        return refuse(f"the gate has no place to keep its locks: {e.strerror or e}", 4)
    if slot is None:
        _log(f"user={who} {asked}: busy")
        return refuse(f"busy: {AT_ONCE} requests are being served here; ask again in a minute", 4)
    try:
        why = check_(me) if check_ else checked(me, state_dir())
        if why:
            _log(f"refused (unsafe install): {why}")
            return refuse(f"the gate won't serve as it is installed: {why}", 3)
        if pw is None:
            return answer(_version())
        # May this account act as them? sudo says so by its exit status alone.
        try:
            pre = run_([sudo, "-n", "-l", "-u", pw.pw_name, me, "run"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                       timeout=PREFLIGHT_TIMEOUT, env=plain)
        except (OSError, subprocess.TimeoutExpired) as e:
            return refuse(f"sudo could not be asked: {e}", 4)
        if pre.returncode != 0:
            _log(f"user={pw.pw_name} {asked}: sudo refused")
            return refuse(f"this account may not act as {pw.pw_name} here (sudo), so the Console can't "
                          "do it for them: they can paste the command on the cluster instead", 5)
        argv = [sudo, "-n", "-u", pw.pw_name, me, "run"] + words_of(op, got)
        try:
            r = run_(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, errors="replace", timeout=SERVE_TIMEOUT, env=plain)
        except subprocess.TimeoutExpired:
            _log(f"user={pw.pw_name} {asked}: no answer in {SERVE_TIMEOUT} s")
            if sends:
                return answer({"ok": False, "maybe": True, "error": f"no answer from the gate as {pw.pw_name} "
                               f"within {SERVE_TIMEOUT} s: it may have been submitted; check the jobs before "
                               "sending it again"}, 4)
            return refuse(f"no answer from the gate as {pw.pw_name} within {SERVE_TIMEOUT} s", 4)
        except OSError as e:
            return refuse(f"sudo could not run: {e}", 4)
    finally:
        os.close(slot)
    doc = None
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            break
        doc = None
    if doc is None:
        err = (r.stderr or "").strip()[-300:]
        _log(f"user={pw.pw_name} {asked}: exit {r.returncode}")
        why = f"the gate as {pw.pw_name} ended with exit {r.returncode}: {err or 'nothing said'}"
        if sends:                                  # it may have got as far as sbatch
            return answer({"ok": False, "maybe": True, "error": why + " (it may have been submitted; check the "
                           "jobs before sending it again)"}, 4)
        return refuse(why, 4)
    job = doc.get("job")
    done = f" -> job {job}" if op in ("submit", "again") and isinstance(job, str) and _JOB.fullmatch(job) else ""
    _log(f"user={pw.pw_name} {asked}: {'ok' if doc.get('ok') else 'not done'}{done}")
    return answer(doc, r.returncode)


# ── run: as the person ──────────────────────────────────────────────────────

def script_bytes(path: str) -> bytes:
    """The person's own job script at ``path``, as it is on disk: a regular
    file they own (not a link), at most 256 KB, beginning with #!."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if os.path.islink(path):
            raise Refused(f"{path} is a link: give the script itself")
        raise Refused(f"{path}: {e.strerror}")
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{path} isn't a file")
        if st.st_uid != _uid():
            raise Refused(f"{path} isn't yours")
        if st.st_size > MAX_SCRIPT:
            raise Refused(f"{path} is over {MAX_SCRIPT // 1024} KB")
        try:
            data = f.read(MAX_SCRIPT + 1)
        except OSError as e:
            raise Refused(f"{path}: {e.strerror}")
    if len(data) > MAX_SCRIPT:
        raise Refused(f"{path} is over {MAX_SCRIPT // 1024} KB")
    if not data.startswith(b"#!"):
        raise Refused(f"{path} doesn't start with #!: not a job script")
    return data


def own_script(path: str) -> str:
    """The text of the person's own job script (script_bytes)."""
    return script_bytes(path).decode("utf-8", errors="replace")


def pinned(path: str, sha: str) -> bytes:
    """The script at ``path``, only if it is still the text whose sha256
    the person was shown (else Refused: it changed since)."""
    import hashlib
    data = script_bytes(path)
    if hashlib.sha256(data).hexdigest() != sha:
        raise Refused(f"{path} changed since you looked at it: look at it again before sending it")
    return data


def sha256_of(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _warning(path: str) -> dict:
    """What the page says above the script: who else could change it, and
    (1.25.2) who else could run programs as the person."""
    private = private_groups(_account())
    said = [w for w in (exposure(path, _uid(), private), account_open(_home(), _uid(), private)) if w]
    return {"warning": ". Also, ".join(said)} if said else {}


# The Slurm programs the gate itself runs as the person (jobs, job, cancel):
# through `nomad-gate exec NAME ...` in their login, which finds NAME on
# that login's PATH and runs it by its full path, or refuses (1.25.2:
# nomad.submit.programs).
EXEC = ("squeue", "scancel", "scontrol", "sacct")
EXEC_REFUSED = 96
EXEC_SAYS = "nomad-gate: not run: "


def exec_slurm(words: list) -> int:
    """`nomad-gate exec NAME ARGS...`: one of EXEC, as this account's PATH
    finds it, unless someone else could have put it there."""
    from nomad.submit import programs
    if not words or words[0] not in EXEC:
        sys.stderr.write(EXEC_SAYS + "only " + ", ".join(EXEC) + "\n")
        return EXEC_REFUSED
    try:
        found = programs.check(words[0], private=private_groups(_account()))
    except programs.Unsafe as e:
        sys.stderr.write(f"{EXEC_SAYS}{e}\n")
        return EXEC_REFUSED
    if found is None:
        sys.stderr.write(f"{EXEC_SAYS}there's no {words[0]} on your PATH here\n")
        return EXEC_REFUSED
    sys.stdout.flush()
    try:
        os.execv(found, [words[0]] + list(words[1:]))
    except OSError as e:
        sys.stderr.write(f"{EXEC_SAYS}{found}: {e.strerror or e}\n")
    return EXEC_REFUSED


def _slurm(me: str, argv: list) -> list:
    """A Slurm step as the person: through the gate's own exec (above)."""
    return [me, "exec"] + list(argv)


# The person's own shell setup may define functions, cd among them: not used here.
LOGIN = ('unset -f cd exec builtin 2>/dev/null; builtin cd -- "$1" || '
         '{ echo "nomad-gate: could not go into the folder" >&2; exit 97; }; shift; builtin exec "$@"')
CD_FAILED = "nomad-gate: could not go into the folder"


class TimedOut(Refused):
    """No answer in time (a job may still have been sent)."""


def _env() -> dict:
    """The environment the person's login starts from: sudo's, without the
    locale the gate gave sudo (the login sets theirs, as when they log in)."""
    return {k: v for k, v in os.environ.items() if k not in ("LANG", "LANGUAGE") and not k.startswith("LC_")}


def in_login(folder: str, argv: list, timeout: int = STEP_TIMEOUT, run_=None) -> tuple:
    """``argv`` run in the person's login environment (bash -l, as when
    they log in), from ``folder``: (exit, what it said). The folder and the
    words are bash's arguments, never part of its script."""
    run_ = run_ or subprocess.run
    try:
        r = run_(["/bin/bash", "-lc", LOGIN, GATE, folder] + list(argv), stdin=subprocess.DEVNULL,
                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=folder, env=_env())
    except subprocess.TimeoutExpired:
        raise TimedOut(f"{os.path.basename(argv[0])} {argv[1] if len(argv) > 1 else ''}: "
                       f"no answer within {timeout} s")
    except OSError as e:
        raise Refused(f"couldn't go into {folder}: {e.strerror or e}")
    out = r.stdout or b""
    if len(out) > MAX_READ:
        raise Refused(f"{os.path.basename(argv[0])} said more than {MAX_READ // 1024 // 1024} MB")
    said = out.decode("utf-8", errors="replace")
    if r.returncode == 97 and CD_FAILED in said:
        raise Refused(f"couldn't go into {folder}")
    if r.returncode == EXEC_REFUSED and EXEC_SAYS in said:
        raise Refused(said[said.index(EXEC_SAYS) + len(EXEC_SAYS):].strip().splitlines()[0])
    return r.returncode, said


def json_in(text: str, keys: tuple) -> dict | None:
    """The JSON object in ``text`` that has ``keys`` (a login's own lines
    may come before it)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"(?m)^\{", text):
        try:
            obj, _end = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and all(k in obj for k in keys):
            return obj
    return None


_SUBMITTED = re.compile(r"^\s*Submitted job (\d+(?:_\d+)?)\.", re.M)
_SUBMITTED_ANYWAY = re.compile(r"but it had submitted job (\d+(?:_\d+)?)")
_NO_NUMBER = re.compile(r"^\s*Submitted \(sbatch didn't print the job's number\)\.", re.M)
_OUTPUT = re.compile(r"^\s*Its output: (/\S+)", re.M)


def submitted(code: int, said: str) -> dict:
    """What nomad submit's words say: whether a job was made, its number and
    where its output goes (as nomad said them once it was sent); "warning"
    when it was made but something else went wrong."""
    doc = {"ok": False, "job": None, "output": None, "exit": code, "said": said.strip()[-MAX_SAID:]}
    found = list(_SUBMITTED.finditer(said))
    anyway = list(_SUBMITTED_ANYWAY.finditer(said))
    if found and code == 0:
        m = found[-1]
        out = _OUTPUT.findall(said[m.end():])
        doc.update(ok=True, job=m.group(1), output=out[0] if out else None)
    elif anyway:
        doc.update(ok=True, job=anyway[-1].group(1),
                   warning="sbatch ended with an error, but it had submitted the job: check it before sending it again")
    elif _NO_NUMBER.search(said) and code == 0:
        doc.update(ok=True, warning="sbatch didn't print the job's number: look for it among your jobs")
    elif "may have been submitted" in said or "sbatch didn't answer" in said:
        doc.update(maybe=True, error="sbatch didn't answer in time: it may have been submitted; check your "
                                     "jobs before sending it again")
    else:
        errors = [x.strip()[len("Error:"):].strip() for x in said.splitlines() if x.strip().startswith("Error:")]
        if errors:
            doc["error"] = errors[-1]                    # nomad's own words for why it wasn't sent
    return doc


def _choices(got: dict) -> list:
    out = []
    for k in ("--cpus", "--mem", "--time", "--gpus", "--partition"):
        if k in got:
            out += [k, got[k]]
    for k in ("--keep", "--localscratch", "--no-localscratch"):
        if got.get(k):
            out.append(k)
    return out


def _home() -> str:
    try:
        return pwd.getpwuid(_uid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


JOB_FIELDS = ("job", "name", "state", "time", "limit", "nodes", "where", "workdir")
_SQUEUE = "\x1f".join(("%i", "%j", "%T", "%M", "%l", "%D", "%R", "%Z"))


def run(words: list, me: str, run_=None) -> int:
    try:
        op, got = parse(words)
        if _uid() < MIN_UID:
            raise Refused("run is for a person's own account")
        if got.get("--user") not in (None, _account()):
            raise Refused("--user is for serve: run acts as whoever runs it")
        nomad = os.path.join(os.path.dirname(me), "nomad")
        if op in ("size", "submit", "again") and not os.access(nomad, os.X_OK):
            raise Refused(f"no nomad beside the gate ({nomad})")
        if op == "version":
            return answer({**_version(), "home": _home()})
        if op == "show":
            data = script_bytes(got["--script"])
            return answer({"ok": True, "script": got["--script"], "text": data.decode("utf-8", errors="replace"),
                           "sha256": sha256_of(data), **_warning(got["--script"])})
        if op == "ls":
            return answer({"ok": True, **listing(got["--dir"])})
        if op == "size":
            if "--script" in got:
                script_bytes(got["--script"])
                folder, argv = os.path.dirname(got["--script"]), [nomad, "size", "--json", "--via", "console",
                                                                  got["--script"]]
            else:
                folder, argv = _home(), [nomad, "size", "--json", "--via", "console", "--job", got["--job"]]
            code, said = in_login(folder, argv, run_=run_)
            plan = json_in(said, ("choices", "request"))
            if code != 0 or plan is None:
                raise Refused(f"nomad size didn't answer (exit {code}): {said.strip()[-600:]}")
            return answer({"ok": True, "plan": plan})
        if op in ("submit", "again"):
            pin = ["--script-sha256", got["--sha256"]]
            if op == "submit":
                pinned(got["--script"], got["--sha256"])          # nomad checks again, and gives sbatch that text
                folder = os.path.dirname(got["--script"])
                argv = [nomad, "submit", "--yes", "--via", "console"] + pin + _choices(got) + [got["--script"]]
            else:
                folder = _home()
                argv = [nomad, "submit", "--yes", "--via", "console", "--again", got["--job"]] + pin + \
                    _choices(got)
            try:
                code, said = in_login(folder, argv, SEND_TIMEOUT, run_=run_)
            except TimedOut as e:
                return answer({"ok": False, "maybe": True, "error": f"{e}: it may have been submitted; check "
                               "your jobs before sending it again"}, 6)
            doc = submitted(code, said)
            return answer(doc, 0 if doc["ok"] else 6)
        if op == "jobs":
            code, said = in_login(_home(), _slurm(me, ["squeue", "-u", _account(), "-h", "-o", _SQUEUE]), 60,
                                  run_=run_)
            if code != 0:
                raise Refused(f"squeue: {said.strip()[-300:]}")
            rows = [dict(zip(JOB_FIELDS, f)) for f in (line.split("\x1f") for line in said.splitlines())
                    if len(f) == len(JOB_FIELDS)]
            return answer({"ok": True, "jobs": rows})
        if op == "job":
            return answer(dict(ok=True, **job_details(got["--job"], run_=run_, me=me)))
        if op == "cancel":
            code, said = in_login(_home(), _slurm(me, ["scancel", got["--job"]]), 60, run_=run_)
            return answer({"ok": code == 0, "job": got["--job"], "exit": code, "said": said.strip()[-2000:]},
                          0 if code == 0 else 6)
    except Refused as e:
        return refuse(str(e))
    return refuse(f"{op}: not done")


# What Slurm says of one job (scontrol, while it knows it), and its accounting record
_SHOWN = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_:/]*)=")
ACCT_FIELDS = ("JobID", "JobName", "User", "State", "ExitCode", "Submit", "Start", "End", "Elapsed", "Timelimit",
               "NNodes", "NodeList", "AllocCPUS", "ReqMem", "MaxRSS", "TotalCPU", "AllocTRES", "WorkDir")
TAIL = 64 * 1024
TAIL_LINES = 200
SEP = "\x1f"                    # sacct's separator: nothing in a job's name or folder holds it
MAX_STEPS = 200


def shown_fields(line: str) -> dict:
    """scontrol show job -o's one line as {Key: value}; values may hold spaces."""
    keys = list(_SHOWN.finditer(line))
    out = {}
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(line)
        out[m.group(1)] = line[m.end():end].strip()
    return out


def _tail(path: str) -> dict:
    """The end of an output file, read as the person (who can read it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return {"path": path, "error": e.strerror or str(e)}
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return {"path": path, "error": "not a file"}
        f.seek(max(0, st.st_size - TAIL))
        data = f.read(TAIL)
    lines = data.decode("utf-8", errors="replace").splitlines()
    if st.st_size > TAIL and lines:
        lines = lines[1:]                          # a part line
    return {"path": path, "size": st.st_size, "mtime": int(st.st_mtime),
            "tail": "\n".join(lines[-TAIL_LINES:]), "cut": st.st_size > TAIL or len(lines) > TAIL_LINES}


def _output_path(pattern: str, fields: dict, job: str) -> str | None:
    """sacct's StdOut, its %-patterns as Slurm fills them (the common ones;
    %j is the job's own number, an array task's too; %5j pads with zeros)."""
    if not pattern or pattern in ("(null)", "None"):
        return None
    jid = fields.get("JobID") or job
    arr, _, task = jid.partition("_")
    raw = fields.get("JobIDRaw") or (arr if not task else "")
    subst = {"j": raw or arr, "x": fields.get("JobName", ""), "u": _account(), "A": arr, "a": task, "%": "%"}

    def fill(m):
        v = subst.get(m.group(2))
        if v is None:
            return m.group(0)
        return v.zfill(int(m.group(1))) if m.group(1) and v.isdigit() else v
    out = re.sub(r"%(\d*)([jxuAa%])", fill, pattern)
    if not out.startswith("/"):
        out = os.path.join(fields.get("WorkDir") or _home(), out)
    return out


def job_details(job: str, run_=None, *, me: str) -> dict:
    """One job of the person's: Slurm's view of it now (scontrol), its
    accounting (sacct: steps, memory, CPU time) and the end of its output.
    Someone else's job is refused, whatever Slurm would show. ``me``: the
    gate's own path (its Slurm steps go through its exec)."""
    gate_, me = me, _account()
    now, acct = {}, []
    code, said = in_login(_home(), _slurm(gate_, ["scontrol", "show", "job", "-o", job]), 60, run_=run_)
    if code == 0:
        for line in said.splitlines():
            if line.startswith("JobId="):
                now = shown_fields(line)
                break
    try:
        code, said = in_login(_home(), _slurm(gate_, ["sacct", "-j", job, "-u", me, "-P", "-n", "--delimiter",
                                                      SEP, "-o", ",".join(ACCT_FIELDS)]), 60, run_=run_)
    except Refused:
        code, said = 1, ""                         # (slow or too much: what scontrol says still stands)
    if code == 0:
        for line in said.splitlines()[:MAX_STEPS]:
            parts = line.split(SEP)
            if len(parts) == len(ACCT_FIELDS) and parts[2] in ("", me):
                acct.append(dict(zip(ACCT_FIELDS, parts)))
    owner = (now.get("UserId") or "").split("(")[0] or next((r["User"] for r in acct if r.get("User")), "")
    if not now and not acct:
        raise Refused(f"Slurm knows no job {job} of yours")
    if owner != me:
        raise Refused(f"job {job} isn't yours")
    out = {"job": job, "now": now, "acct": acct}
    paths = []
    if now.get("StdOut"):
        paths.append(now["StdOut"])
        if now.get("StdErr") and now["StdErr"] != now["StdOut"]:
            paths.append(now["StdErr"])
    else:
        try:
            code, said = in_login(_home(), _slurm(gate_, ["sacct", "-j", job, "-u", me, "-X", "-P", "-n",
                                                          "--delimiter", SEP, "-o",
                                                          "StdOut,StdErr,JobName,WorkDir,JobID,JobIDRaw"]), 60,
                                  run_=run_)
        except Refused:
            code, said = 1, ""
        rows = [x.split(SEP) for x in said.strip().splitlines()] if code == 0 else []
        rows = [r for r in rows if len(r) == 6]
        row = next((r for r in rows if r[4] == job or r[5] == job), rows[0] if len(rows) == 1 else None)
        if row:
            fields = {"JobName": row[2], "WorkDir": row[3], "JobID": row[4], "JobIDRaw": row[5]}
            for pat in dict.fromkeys(row[:2]):
                p = _output_path(pat, fields, job)
                if p:
                    paths.append(p)
    out["output"] = [_tail(p) for p in paths[:2]]
    return out


def listing(path: str) -> dict:
    """A folder's entries as the person sees them (name, kind, size, time,
    whether theirs, whether a job script), folders first; hidden ones left
    out; at most 500."""
    try:
        with os.scandir(path) as it:
            entries = [e for e in it if not e.name.startswith(".")]
    except OSError as e:
        raise Refused(f"{path}: {e.strerror}")
    rows = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        kind = ("link" if stat.S_ISLNK(st.st_mode) else "folder" if stat.S_ISDIR(st.st_mode)
                else "file" if stat.S_ISREG(st.st_mode) else "other")
        mine = st.st_uid == _uid()
        script = False
        if kind == "file" and mine and st.st_size <= MAX_SCRIPT:
            try:
                fd = os.open(e.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    script = stat.S_ISREG(os.fstat(f.fileno()).st_mode) and f.read(2) == b"#!"
            except OSError:
                pass
        rows.append({"name": e.name, "kind": kind, "size": st.st_size, "mtime": int(st.st_mtime),
                     "mine": mine, "script": script})
    rows.sort(key=lambda r: (r["kind"] != "folder", r["name"].lower()))
    return {"dir": os.path.normpath(path), "entries": rows[:MAX_LS], "more": len(rows) > MAX_LS}


USAGE = """usage: nomad-gate serve            (run by sshd for the hub's key)
       nomad-gate run OP [OPTIONS]  (as yourself: what the hub would get)
       nomad-gate version | check   (check: whether it would serve as installed)
       nomad-gate exec NAME ARGS    (squeue, scancel, scontrol, sacct: as your PATH finds it,
                                     unless someone else could have put it there)
"""


def main(argv: list | None = None, me: str | None = None) -> int:
    """``me``: the gate's own path (bin/nomad-gate, as its launcher gives it)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    me = os.path.abspath(me or sys.argv[0])
    if argv[:1] == ["serve"] and len(argv) == 1:
        return serve(me)
    if argv[:1] == ["run"]:
        return run(argv[1:], me)
    if argv[:1] == ["exec"]:
        return exec_slurm(argv[1:])
    if argv in (["version"], ["--version"]):
        return answer(_version())
    if argv == ["check"]:
        why = self_check(me)
        return answer({"ok": why is None, **({"error": why} if why else {})}, 0 if why is None else 3)
    sys.stderr.write((__doc__ or "").split("\n\n", 1)[1].split("\n\nInstalled beside")[0] + "\n\n" + USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
