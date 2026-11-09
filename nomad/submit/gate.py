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
    save   --script PATH (--sha256 HEX | --new) --body-sha256 HEX
                                the script's new text (on standard input), written as the person
                                over the very text they opened, or as a new file (1.25.3); only
                                where the cluster says so too: `nomad-gate serve --edit` in the
                                hub key's authorized_keys line (nomad gate install-key --edit)
    versions --script PATH      the earlier texts kept when it was saved
    recall --script PATH --id ID  one of them
    read   --path PATH          a text file of theirs to edit: a job script (#!) or a file
                                with an extension on TEXT_EXTENSIONS (.py .R .inp ...); only
                                with serve --edit, as save (1.25.4)
    starts --script PATH --sha256 HEX --partitions P1,P2,...
                                when Slurm would start that very text as nomad would send it
                                (with --cpus --mem --time --gpus), in each partition: sbatch
                                --test-only, nothing submitted; and their nodes' states (1.25.4)
           --job N --sha256 HEX ...  for a job of theirs that ran, as again would send it

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
import errno
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
STARTS_HERE = 2                 # of them "starts" (up to 12 sbatch --test-only each, minutes): the rest stay free
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
_VID = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{12}")       # an earlier version kept (1.25.3)
_PARTS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}(,[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,11}")   # 1.25.4
_KIT = re.compile(r"[a-z0-9][a-z0-9-]{1,47}")                                                     # 1.35.0
_KIND_WORDS = {"parts": "a list of up to 12 partitions, comma between,", "vid": "a version id",
               "kit": "a practice kit's name"}
_CHECK = {"job": _JOB, "int": _INT, "mem": _MEM, "time": _TIME, "partition": _PARTITION,
          "path": _PATH, "sha": _SHA, "vid": _VID, "parts": _PARTS, "kit": _KIT}

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
    # A job script written as the person (1.25.3): its new text on standard
    # input (--body-sha256 its sha256), over the one they opened (--sha256)
    # or as a new file (--new); the one before kept (versions; recall: its text).
    "save": {"--script": "path", "--sha256": "sha", "--new": "flag", "--body-sha256": "sha"},
    "versions": {"--script": "path"},
    "recall": {"--script": "path", "--id": "vid"},
    # A text file of theirs to edit (1.25.4): the job script's rules, or an extension on TEXT_EXTENSIONS.
    "read": {"--path": "path"},
    # When it would start in each partition (1.25.4): a read; the values as for submit.
    # --job: a job of theirs that ran, as "again" would send it (--sha256: its script as shown).
    "starts": {"--script": "path", "--job": "job", "--sha256": "sha", "--partitions": "parts", "--cpus": "int",
               "--mem": "mem", "--time": "time", "--gpus": "int", "--keep": "flag"},
    # Practice runs (1.35.0): the kits ready here (nomad's, and the site's in --kits-dir), a copy of one
    # in a new or empty folder of the person's, and a new folder of theirs to put things in.
    "kits": {"--kits-dir": "path"},
    "practice": {"--kit": "kit", "--dir": "path", "--kits-dir": "path"},
    "mkdir": {"--dir": "path"},
}
REQUIRED = {"submit": ["--script", "--sha256"], "again": ["--job", "--sha256"], "job": ["--job"],
            "cancel": ["--job"], "ls": ["--dir"], "show": ["--script"],
            "save": ["--script", "--body-sha256"], "versions": ["--script"], "recall": ["--script", "--id"],
            "starts": ["--sha256", "--partitions"], "read": ["--path"], "practice": ["--kit", "--dir"],
            "mkdir": ["--dir"]}
# The requests that carry a body on standard input (at most MAX_SCRIPT bytes).
WITH_BODY = {"save"}


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
            raise Refused(f"{w} {v[:60]!r}: not {_KIND_WORDS.get(kind, 'a ' + kind)} nomad takes")
        elif kind == "path" and ("//" in v or any(x in (".", "..") for x in v.split("/"))):
            raise Refused(f"{w} {v[:60]!r}: give the path without . or .. in it")
        got[w] = v
        i += 2
    for r in REQUIRED.get(op, []):
        if r not in got:
            raise Refused(f"{op} needs {r}")
    if op in ("size", "starts") and ("--script" in got) == ("--job" in got):
        raise Refused(f"{op} needs --script or --job (one of them)")
    if op == "save" and ("--sha256" in got) == ("--new" in got):
        raise Refused("save needs --sha256 (the text it replaces) or --new (one of them)")
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


def _slot(place: str | None = None, kind: str = "slot", n: int | None = None, wait: float | None = None):
    """One of AT_ONCE places for a request on this account (waiting up to
    WAIT_FOR_SLOT s for one); the lock to release, or None when all are taken.
    ``kind``/``n``/``wait``: another set of places (starts: STARTS_HERE)."""
    place = place or state_dir()
    os.makedirs(place, mode=0o700, exist_ok=True)
    n = AT_ONCE if n is None else n
    deadline = time.time() + (WAIT_FOR_SLOT if wait is None else wait)
    while True:
        for i in range(n):
            fd = os.open(os.path.join(place, f"{kind}{i}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
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


def _read_body(stream) -> str:
    """A request's body (1.25.3): at most MAX_SCRIPT bytes of UTF-8 text."""
    data = stream.read(MAX_SCRIPT + 1) if stream is not None else b""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="surrogateescape")
    if len(data) > MAX_SCRIPT:
        raise Refused(f"the text is over {MAX_SCRIPT // 1024} KB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("the text isn't UTF-8") from None


# What writes a person's files (1.25.3), and what reads their files beyond
# job scripts for it, or the earlier texts saving kept (1.25.4): served only
# with `serve --edit`, the cluster's own say (the hub's [[hub.sites]]
# edit = true is the hub's).
EDITS = {"save", "read", "versions", "recall", "practice", "mkdir"}    # practice, mkdir: they write (1.35.0)


def serve(me: str, env=None, run_=None, slot_=None, check_=None, stdin=None, edit: bool = False) -> int:
    env = os.environ if env is None else env
    if stdin is None:
        stdin = sys.stdin.buffer
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
    if op in EDITS and not edit:
        _log(f"user={who} {asked}: refused: editing is off here")
        return refuse("editing files from the Console isn't turned on on this cluster (its nomad account's "
                      "authorized_keys line for the hub's key runs nomad-gate serve, not nomad-gate serve "
                      "--edit: nomad gate install-key --edit)")
    body = None
    if op in WITH_BODY:
        try:
            body = _read_body(stdin)
        except Refused as e:
            _log(f"user={who} {asked}: refused: {e}")
            return refuse(str(e))
    plain = {"PATH": TOOL_PATH, "LANG": "C", "LC_ALL": "C"}     # sudo's own words, the same everywhere
    sends = op in ("submit", "again")
    long = None
    try:
        if op == "starts":
            # a few at most, so sending and the rest are never kept waiting by them
            long = slot_() if slot_ else _slot(kind="starts", n=STARTS_HERE, wait=0)
            if long is None:
                _log(f"user={who} {asked}: busy (starts)")
                return refuse(f"busy: {STARTS_HERE} people are asking where a job would start here; ask "
                              "again in a minute", 4)
        slot = slot_() if slot_ else _slot()
    except OSError as e:
        if long is not None:
            os.close(long)
        return refuse(f"the gate has no place to keep its locks: {e.strerror or e}", 4)
    if slot is None:
        if long is not None:
            os.close(long)
        _log(f"user={who} {asked}: busy")
        return refuse(f"busy: {AT_ONCE} requests are being served here; ask again in a minute", 4)
    try:
        why = check_(me) if check_ else checked(me, state_dir())
        if why:
            _log(f"refused (unsafe install): {why}")
            return refuse(f"the gate won't serve as it is installed: {why}", 3)
        if pw is None:
            return answer({**_version(), "edit": bool(edit)})
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
        given = {"input": body} if body is not None else {"stdin": subprocess.DEVNULL}
        try:
            r = run_(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, errors="replace", timeout=SERVE_TIMEOUT, env=plain, **given)
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
        if long is not None:
            os.close(long)
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
    if op == "save" and doc.get("ok"):
        old = got.get("--sha256", "new")[:12]
        done = f" -> {old} => {str(doc.get('sha256'))[:12]}" + (f", kept {doc['kept']}" if doc.get("kept") else "")
    _log(f"user={pw.pw_name} {asked}: {'ok' if doc.get('ok') else 'not done'}{done}")
    if op == "version":
        doc["edit"] = bool(edit)                    # the cluster's say on saving (1.25.3)
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
        if op in ("size", "submit", "again", "starts") and not os.access(nomad, os.X_OK):
            raise Refused(f"no nomad beside the gate ({nomad})")
        if op == "version":
            return answer({**_version(), "home": _home()})
        if op == "show":
            data = script_bytes(got["--script"])
            return answer({"ok": True, "script": got["--script"], "text": data.decode("utf-8", errors="replace"),
                           "sha256": sha256_of(data), "utf8": is_utf8(data), **_warning(got["--script"])})
        if op == "ls":
            return answer({"ok": True, **listing(got["--dir"])})
        if op == "save":
            text = _read_body(sys.stdin.buffer)
            return answer(save(got["--script"], text, body_sha=got["--body-sha256"], old_sha=got.get("--sha256"),
                               new="--new" in got))
        if op == "versions":
            return answer({"ok": True, "script": got["--script"], "versions": versions(got["--script"])})
        if op == "read":
            return answer({"ok": True, **read_text_file(got["--path"])})
        if op == "starts":
            ask = [nomad, "submit", "--via", "console", "--script-sha256", got["--sha256"], "--starts",
                   got["--partitions"]] + _choices(got)
            if "--script" in got:
                pinned(got["--script"], got["--sha256"])          # the very text shown: nomad checks again
                folder, argv = os.path.dirname(got["--script"]), ask + [got["--script"]]
            else:
                folder, argv = _home(), ask[:4] + ["--again", got["--job"]] + ask[4:]   # nomad checks the pin
            code, said = in_login(folder, argv, run_=run_)
            doc = json_in(said, ("starts",))
            if code != 0 or doc is None:
                raise Refused(f"nomad didn't say when it would start (exit {code}): {said.strip()[-600:]}")
            return answer({**doc, "ok": True})
        if op == "recall":
            return answer({"ok": True, "script": got["--script"], "id": got["--id"],
                           "text": recall(got["--script"], got["--id"])})
        if op == "kits":
            return answer({"ok": True, **practice_kits(got.get("--kits-dir"))})
        if op == "practice":
            return answer({"ok": True, **practice_copy(got["--kit"], got["--dir"], got.get("--kits-dir"))})
        if op == "mkdir":
            return answer({"ok": True, **new_folder(got["--dir"])})
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
    # A practice run (1.35.1): its kit, what to look at in its folder now, and the kit's recorded run;
    # None for any other job (the key says this gate can tell).
    workdir = now.get("WorkDir") or next((r["WorkDir"] for r in acct if r.get("WorkDir")), "")
    # an array task's own number names its files (%j), not its array's
    own = (now.get("JobId") or "").strip() or job.partition("_")[0]
    out["practice"] = _practice(workdir, own) if workdir.startswith("/") else None
    return out


def _kit_folder(src: str, name: str | None) -> tuple:
    """(config, what is wrong) for a site's kit a copy names (its README's
    Kit folder): that kit's own folder only, checked as kits' are -- the
    README is the person's, so it may name any folder."""
    if not name or not os.path.isdir(os.path.join(src, name)):
        return {}, [f"the kit {name or '?'} isn't in {src} here"]
    owners = trusted_owners()
    folder = os.path.join(src, name)
    why = unsafe(folder, owners) or tree_unsafe(folder, owners)
    if why:
        return {}, [f"the site's kit in {folder} isn't used: {why}"]
    return {"submit": {"demos": {"dirs": [src]}}}, []


def _practice(workdir: str, job: str) -> dict | None:
    from nomad.demos import kits as K
    try:
        name, src, _title = K.kit_source(workdir)
        config, said = _kit_folder(src, name) if src else ({}, [])
        got = K.practice_of(workdir, config, job=job)
        if got and got.get("kit") is None:
            got["error"] = (said[0] if said else
                            "its kit can't be told here: a site's own kit copied before 1.35.1, or its "
                            "README-practice.txt was changed")
        return got
    except Exception as e:                              # noqa: BLE001 -- the job's details stand without it
        return {"folder": workdir, "kit": None, "error": f"what to look at couldn't be read ({type(e).__name__})"}


def listing(path: str) -> dict:
    """A folder's entries as the person sees them (name, kind, size, time,
    whether theirs, whether a job script, whether a text file of theirs the
    Console may edit), folders first; hidden ones left out; at most 500."""
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
        # a text file of theirs the Console may open to edit (1.25.4; only with serve --edit)
        text = kind == "file" and mine and not script and st.st_size <= MAX_SCRIPT and text_kind(e.name)
        rows.append({"name": e.name, "kind": kind, "size": st.st_size, "mtime": int(st.st_mtime),
                     "mine": mine, "script": script, "text": bool(text)})
    rows.sort(key=lambda r: (r["kind"] != "folder", r["name"].lower()))
    return {"dir": os.path.normpath(path), "entries": rows[:MAX_LS], "more": len(rows) > MAX_LS}


# ── practice runs (1.35.0) ─────────────────────────────────────────────────
# A kit's files copied, as the person, into a folder of theirs that is new
# or empty (never over a file: nomad.demos.kits.copy), from nomad's own kits
# or the site's (--kits-dir, the hub's [submit.demos] site_dirs); and a new
# folder of theirs. Both only in a folder whose parent is there already.

def _kits_config(kits_dir: str | None) -> tuple:
    """(nomad's kits and the site's in ``kits_dir``, what is wrong): the
    site's folder only when no one but root, the person, nomad's installer
    and Slurm could have put a kit there -- its files are copied as the
    person, and its scripts run as them."""
    if not kits_dir:
        return {}, []
    owners = trusted_owners()
    why = unsafe(kits_dir, owners) if os.path.isdir(kits_dir) else f"{kits_dir} isn't a folder here"
    why = why or tree_unsafe(kits_dir, owners)
    if why:
        return {}, [f"the site's kits in {kits_dir} aren't used: {why}"]
    return {"submit": {"demos": {"dirs": [kits_dir]}}}, []


def _not_hidden(path: str) -> None:
    """No part of the folder hidden, as given nor where it really leads (a
    link to ~/.bashrc.d is ~/.bashrc.d)."""
    real = os.path.join(os.path.realpath(os.path.dirname(os.path.normpath(path))), os.path.basename(path))
    for p in (path, real):
        if any(part.startswith(".") for part in p.split("/") if part):
            raise Refused(f"{path}: not a hidden folder, nor one in a hidden folder")


def practice_kits(kits_dir: str | None) -> dict:
    """The practice kits ready here (a kit to build: once built at the site)."""
    from nomad.demos import kits as K
    config, said = _kits_config(kits_dir)
    got, problems = K.kits(config, ready_only=True)
    problems = said + problems
    # a site's own kit: where it is, for `nomad demos copy NAME --from` where copying from the Console is off
    return {"kits": [{**k.to_dict(), **({} if k.bundled else {"from": str(k.path.parent)})} for k in got],
            "practice": K.PRACTICE,
            "problems": [str(p)[:300] for p in problems][:10]}


def _parent_there(path: str) -> None:
    parent = os.path.dirname(os.path.normpath(path))
    if not os.path.isdir(parent):
        raise Refused(f"{parent} isn't a folder here: make it first")


def practice_copy(name: str, dest: str, kits_dir: str | None) -> dict:
    """A copy of practice kit ``name`` in ``dest`` (new, or empty), as the person."""
    from nomad.demos import kits as K
    _not_hidden(dest)
    _parent_there(dest)
    config, _said = _kits_config(kits_dir)
    try:
        k = K.find(config, name)
        got = K.copy(k, dest=dest)
    except K.KitError as e:
        raise Refused(str(e)) from None
    except OSError as e:
        raise Refused(f"{dest}: {e.strerror or e}") from None
    return {"kit": k.name, "program": k.program, **got, "practice": K.PRACTICE}


def new_folder(path: str) -> dict:
    """A new folder of the person's, in one that is there already."""
    _not_hidden(path)
    _parent_there(path)
    try:
        os.mkdir(path)
    except FileExistsError:
        raise Refused(f"{path} is there already") from None
    except OSError as e:
        raise Refused(f"{path}: {e.strerror or e}") from None
    return {"dir": os.path.normpath(path)}


# ── save: a job script written as the person (1.25.3) ──────────────────────
# Only their own job script (a regular file they own, not a link, beginning
# with #!, as the new text must; no part of its path hidden, as given nor
# as it really is), only over the very text they opened (its sha256, checked
# again just before it is replaced) or as a new file (never over one that is
# there). The new text is written beside it and put in its place in one step
# (never half a script), with its permissions, group and access list; the
# one before is kept, private, in ~/.local/share/nomad/versions (the last
# KEEP_VERSIONS of each), to go back to. Those folders are walked one at a
# time, never through a link nor into one someone else owns: ~/.local may be
# open to others (account_open says so), and what is kept is theirs alone.
KEEP_VERSIONS = 20
VERSIONS = ".local/share/nomad/versions"


# Text files edited from the Console besides job scripts (1.25.4): programs,
# inputs and settings a job reads -- never a hidden file, nothing binary,
# nothing over MAX_SCRIPT. Editing a job script is already running code as
# the person; a program it runs is the same. What is kept before each save
# (versions) covers an input edited by mistake.
TEXT_EXTENSIONS = (".py", ".r", ".m", ".jl", ".sh", ".in", ".inp", ".com", ".gjf", ".yaml", ".yml", ".json",
                   ".toml", ".cfg", ".conf", ".txt", ".md")


def text_kind(name: str) -> bool:
    """Whether a file of this name is one of TEXT_EXTENSIONS (whatever case)."""
    base = os.path.basename(name)
    return not base.startswith(".") and os.path.splitext(base)[1].lower() in TEXT_EXTENSIONS


def _editable(name: str, data: bytes, what: str, doing: str = "edited from the Console") -> None:
    """A job script (#!), or a text file by its extension; never with a NUL."""
    if not (data.startswith(b"#!") or text_kind(name)):
        raise Refused(f"{what} isn't a job script (#!) nor a text file nomad edits ({', '.join(TEXT_EXTENSIONS)}): "
                      f"not {doing}")
    if b"\x00" in data:
        raise Refused(f"{what} holds a NUL byte (not text): not {doing}")


def _place(path: str) -> tuple:
    """(folder, name) of a script to write: an absolute path, no part of it
    hidden (a dot file or folder: login files, ~/.ssh, ...)."""
    if not path.startswith("/") or path.endswith("/"):
        raise Refused(f"{path}: give the script's full path")
    if any(part.startswith(".") for part in path.split("/") if part):
        raise Refused(f"{path}: not a hidden file or folder")
    folder, name = os.path.split(path)
    return folder, name


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _read_at(dfd: int, name: str, what: str) -> tuple:
    """(bytes, stat, access list or None) of the person's own job script
    ``name`` in the folder dfd."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError as e:
        if e.errno == 40 or os.path.islink(what):           # ELOOP: a link
            raise Refused(f"{what} is a link: give the script itself") from None
        raise Refused(f"{what}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{what} isn't a file")
        if st.st_uid != _uid():
            raise Refused(f"{what} isn't yours")
        if st.st_nlink > 1:
            # another name for the same file may be anywhere (a hidden folder): never edited through it
            raise Refused(f"{what} has other names (a hard link): edit it where it really is, or copy it")
        data = f.read(MAX_SCRIPT + 1)
        acl = {}
        for attr in ACL_ATTRS:
            try:
                acl[attr] = os.getxattr(f.fileno(), attr)
            except (OSError, AttributeError):
                pass
    if len(data) > MAX_SCRIPT:
        raise Refused(f"{what} is over {MAX_SCRIPT // 1024} KB")
    _editable(name, data, what)
    return data, st, acl


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# A file's access lists, copied to the text that replaces it.
ACL_ATTRS = ("system.posix_acl_access", "system.nfs4_acl")
# A new script is the person's alone (chmod shares it): the gate can't know
# the umask their login sets.
NEW_MODE = 0o600


def is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _walk(parts: tuple, make: bool) -> int | None:
    """A folder of the person's own under their home (each of ``parts`` a
    name), opened one step at a time: never through a link, never one
    someone else owns (made, private, when ``make`` and it isn't there).
    Its descriptor, or None (not there, or not theirs alone to use)."""
    try:
        fd = os.open(_home(), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        for i, part in enumerate(parts):
            try:
                nfd = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
            st = os.fstat(fd)
            if st.st_uid != _uid():
                return None
            if i >= 3 and stat.S_IMODE(st.st_mode) & 0o077:     # versions/ and below: theirs alone
                os.fchmod(fd, 0o700)
        out, fd = fd, -1
        return out
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _versions_parts(path: str) -> tuple:
    return tuple(VERSIONS.split("/")) + (_sha(path.encode("utf-8", errors="surrogateescape"))[:16],)


def _write_new(dfd: int, name: str, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dfd)
    with os.fdopen(fd, "wb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 or st.st_uid != _uid():   # never through a link
            raise OSError("not a file of its own")
        f.truncate(0)
        f.write(data)


def _kept_names(dfd: int) -> list:
    out = []
    for n in os.listdir(dfd):
        if n.endswith(".sh") and _VID.fullmatch(n[:-3]):
            try:
                if stat.S_ISREG(os.stat(n, dir_fd=dfd, follow_symlinks=False).st_mode):
                    out.append(n)
            except OSError:
                pass
    return sorted(out)


def _keep(path: str, data: bytes) -> str | None:
    """The text before, kept privately; its id (None if it couldn't be)."""
    import time as _t
    dfd = _walk(_versions_parts(path), make=True)
    if dfd is None:
        return None
    try:
        _write_new(dfd, "path", (path + "\n").encode("utf-8", errors="surrogateescape"))
        vid = f"{_t.strftime('%Y%m%dT%H%M%S', _t.gmtime())}-{_sha(data)[:12]}"     # UTC: in order
        _write_new(dfd, vid + ".sh", data)
        for n in _kept_names(dfd)[:-KEEP_VERSIONS]:
            os.unlink(n, dir_fd=dfd)
        return vid
    except OSError:
        return None
    finally:
        os.close(dfd)


def _unkeep(path: str, vid: str) -> None:
    """A version kept for a save that then didn't happen: taken back."""
    dfd = _walk(_versions_parts(path), make=False)
    if dfd is None:
        return
    try:
        os.unlink(vid + ".sh", dir_fd=dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def versions(path: str) -> list:
    """The earlier versions of ``path`` kept when it was saved, newest first."""
    dfd = _walk(_versions_parts(path), make=False)
    if dfd is None:
        return []
    out = []
    try:
        for n in reversed(_kept_names(dfd)):
            try:
                st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            out.append({"id": n[:-3], "size": st.st_size, "mtime": int(st.st_mtime)})
    except OSError:
        return []
    finally:
        os.close(dfd)
    return out


def recall(path: str, vid: str) -> str:
    """One earlier version's text."""
    if not _VID.fullmatch(vid):
        raise Refused("not a version")
    dfd = _walk(_versions_parts(path), make=False)
    try:
        if dfd is None:
            raise FileNotFoundError
        fd = os.open(vid + ".sh", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        raise Refused(f"no version {vid} of {path} is kept") from None
    finally:
        if dfd is not None:
            os.close(dfd)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise Refused(f"no version {vid} of {path} is kept")
        data = f.read(MAX_SCRIPT + 1)
    return data[:MAX_SCRIPT].decode("utf-8", errors="replace")


def _real_folder(dfd: int, folder: str) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{dfd}")
    except OSError:
        return os.path.realpath(folder)


def _same_as(dfd: int, name: str, fd: int) -> bool:
    try:
        a, b = os.stat(name, dir_fd=dfd, follow_symlinks=False), os.fstat(fd)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _as_before(fd: int, st, acl: dict, notes: list) -> None:
    """The new text's file given the one before's group, permissions and
    access lists (POSIX and NFSv4); what can't be is said in ``notes``."""
    now_gid = os.fstat(fd).st_gid         # a setgid folder gives its own group
    if now_gid != st.st_gid:
        try:
            os.fchown(fd, -1, st.st_gid)
        except OSError:
            notes.append(f"its group is now {_group_of(now_gid)}, not {_group_of(st.st_gid)} (you aren't in that "
                         "group): its group permissions are now for that one")
    os.fchmod(fd, stat.S_IMODE(st.st_mode) & 0o777)        # after chown, which clears setgid
    if acl.get("system.posix_acl_access"):
        try:
            os.setxattr(fd, "system.posix_acl_access", acl["system.posix_acl_access"])
        except OSError:
            notes.append("its access list couldn't be copied")
    else:
        # none before: none inherited from the folder's default list either
        try:
            os.removexattr(fd, "system.posix_acl_access")
        except OSError as e:
            if e.errno not in (errno.ENODATA, errno.EOPNOTSUPP, errno.ENOTSUP):
                notes.append("it may carry the folder's default access list")
    if acl.get("system.nfs4_acl"):
        try:
            os.setxattr(fd, "system.nfs4_acl", acl["system.nfs4_acl"])
        except OSError:
            notes.append("its access list (NFSv4) couldn't be copied")


def read_text_file(path: str) -> dict:
    """A text file of the person's to edit (1.25.4): their own regular file
    (not a link), no hidden part in its path nor in its folder as it really
    is, at most MAX_SCRIPT, a job script or one of TEXT_EXTENSIONS, no NUL.
    {"path", "text", "sha256", "utf8"} (the sha256 of its bytes, as save
    checks)."""
    folder, name = _place(path)
    try:
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise Refused(f"{folder}: {e.strerror}") from None
    try:
        real = _real_folder(dfd, folder)
        if any(part.startswith(".") for part in real.split("/") if part):
            raise Refused(f"{folder} is really {real}: not a hidden folder")
        data, _st, _acl = _read_at(dfd, name, path)
    finally:
        os.close(dfd)
    return {"path": path, "text": data.decode("utf-8", errors="replace"), "sha256": _sha(data),
            "utf8": is_utf8(data), "size": len(data)}


def save(path: str, text: str, *, body_sha: str, old_sha: str | None = None, new: bool = False) -> dict:
    """Write ``text`` as the person's job script ``path`` (see above)."""
    data = text.encode("utf-8")
    if _sha(data) != body_sha:
        raise Refused("the text didn't arrive whole (its sha256 differs): not saved")
    if len(data) > MAX_SCRIPT:
        raise Refused(f"the text is over {MAX_SCRIPT // 1024} KB")
    folder, name = _place(path)
    _editable(name, data, "the text", "saved")
    try:
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise Refused(f"{folder}: {e.strerror}") from None
    try:
        return _save_in(dfd, folder, name, path, data, old_sha, new)
    except OSError as e:                                    # said, as every refusal is
        raise Refused(f"{path}: not saved ({e.strerror or e})") from None
    finally:
        os.close(dfd)


def _save_in(dfd: int, folder: str, name: str, path: str, data: bytes, old_sha, new: bool) -> dict:
    import secrets as _secrets
    real = _real_folder(dfd, folder)
    if any(part.startswith(".") for part in real.split("/") if part):
        raise Refused(f"{folder} is really {real}: not a hidden folder")
    changed = f"{path} changed since you opened it: open it again (your text is not lost)"
    before, st, acl = None, None, {}
    if new:
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
            raise Refused(f"{path} is there already: save under another name")
        except FileNotFoundError:
            pass
    else:
        before, st, acl = _read_at(dfd, name, path)
        if _sha(before) != old_sha:
            raise Refused(changed)
        if not is_utf8(before):
            raise Refused(f"{path} isn't UTF-8 text: saved from here it would change where you didn't edit it "
                          "(edit it on the cluster)")
    tmp = f".{name}.nomad-{_secrets.token_hex(6)}.tmp"
    try:
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    except OSError as e:
        raise Refused(f"can't write in {folder}: {e.strerror}") from None
    notes, kept = [], None
    try:
        with os.fdopen(tfd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            if st is not None:
                _as_before(f.fileno(), st, acl, notes)
            else:
                os.fchmod(f.fileno(), NEW_MODE)
        if new:
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
            except FileExistsError:
                raise Refused(f"{path} is there already: save under another name") from None
            except OSError:
                _new_in_place(dfd, name, path, data)       # no hard links here (some shared file systems)
        else:
            # The text before kept first; then the last look, and the swap right after it.
            kept = _keep(path, before)
            again, _st2, _acl2 = _read_at(dfd, name, path)
            if _sha(again) != old_sha:
                if kept:
                    _unkeep(path, kept)
                raise Refused(changed)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
    try:
        os.fsync(dfd)
    except OSError:
        pass
    if new:
        notes.append("a new script is yours alone to read (chmod g+r shares it with its group)")
    elif kept is None:
        notes.append("the text before couldn't be kept (nomad's folder for it, ~/.local/share/nomad/versions, "
                     "couldn't be written or isn't yours alone)")
    out = {"ok": True, "script": path, "sha256": _sha(data), "size": len(data), "new": new, "kept": kept,
           "notes": notes}
    return {**out, **_warning(path)}


def _new_in_place(dfd: int, name: str, path: str, data: bytes) -> None:
    """A new script written where hard links can't be made: never over one
    there, and never left half written."""
    try:
        nfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, NEW_MODE, dir_fd=dfd)
    except FileExistsError:
        raise Refused(f"{path} is there already: save under another name") from None
    try:
        with os.fdopen(nfd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        if _same_as(dfd, name, nfd):
            try:
                os.unlink(name, dir_fd=dfd)
            except OSError:
                pass
        raise
    finally:
        os.close(nfd)


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
    if argv[:1] == ["serve"] and argv[1:] in ([], ["--edit"]):
        return serve(me, edit=argv[1:] == ["--edit"])
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
