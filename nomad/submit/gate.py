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
    submit --script PATH        nomad submit --yes, with the person's own values if any
                                (--cpus --mem --time --gpus --partition --keep
                                --localscratch/--no-localscratch)
    again  --job N              nomad submit --yes --again N (the same values may be given)
    jobs                        the person's jobs now (squeue -u PERSON)
    cancel --job N              scancel N (Slurm refuses another's job anyway)
    ls     --dir PATH           what a folder holds (names, kinds, sizes, times): to pick a script
    show   --script PATH        a job script's text, to look at before sending it

Every value is checked here, twice (as the cluster account, then as the
person); nothing is handed to a shell; the script sent must be the
person's own file. nomad runs in the person's login environment (`bash -l`,
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
_CHECK = {"job": _JOB, "int": _INT, "mem": _MEM, "time": _TIME, "partition": _PARTITION,
          "path": _PATH}

_CHOICES = {"--cpus": "int", "--mem": "mem", "--time": "time", "--gpus": "int",
            "--partition": "partition", "--keep": "flag", "--localscratch": "flag",
            "--no-localscratch": "flag"}
# op -> {option: kind}; kinds: path, job, int, mem, time, partition, flag
OPS = {
    "version": {},
    "size": {"--script": "path", "--job": "job"},
    "submit": {"--script": "path", **_CHOICES},
    "again": {"--job": "job", **_CHOICES},
    "jobs": {},
    "cancel": {"--job": "job"},
    "ls": {"--dir": "path"},
    "show": {"--script": "path"},
}
REQUIRED = {"submit": ["--script"], "again": ["--job"], "cancel": ["--job"], "ls": ["--dir"],
            "show": ["--script"]}


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

def own_script(path: str, strict: bool = True) -> str:
    """The text of the person's own job script at ``path``: a regular file
    they own (not a link), at most 256 KB, beginning with #!. ``strict``
    (to send it): neither it nor a folder above it can be changed by anyone
    but them or root, so what is sent is what they have."""
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
    if not data.startswith(b"#!"):
        raise Refused(f"{path} doesn't start with #!: not a job script")
    if strict:
        why = unsafe(path, {0, _uid()}, private_groups(_account()))
        if why:
            raise Refused(f"{path} could be changed by someone else before it is sent ({why}): send it from "
                          "a folder only you can change, or with nomad submit on the cluster")
    return data.decode("utf-8", errors="replace")


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
            return answer({"ok": True, "script": got["--script"],
                           "text": own_script(got["--script"], strict=False)})
        if op == "ls":
            return answer({"ok": True, **listing(got["--dir"])})
        if op == "size":
            if "--script" in got:
                own_script(got["--script"], strict=False)
                folder, argv = os.path.dirname(got["--script"]), [nomad, "size", "--json", got["--script"]]
            else:
                folder, argv = _home(), [nomad, "size", "--json", "--job", got["--job"]]
            code, said = in_login(folder, argv, run_=run_)
            plan = json_in(said, ("choices", "request"))
            if code != 0 or plan is None:
                raise Refused(f"nomad size didn't answer (exit {code}): {said.strip()[-600:]}")
            return answer({"ok": True, "plan": plan})
        if op in ("submit", "again"):
            if op == "submit":
                own_script(got["--script"])
                folder = os.path.dirname(got["--script"])
                argv = [nomad, "submit", "--yes", "--via", "console"] + _choices(got) + [got["--script"]]
            else:
                folder = _home()
                argv = [nomad, "submit", "--yes", "--via", "console", "--again", got["--job"]] + _choices(got)
            try:
                code, said = in_login(folder, argv, SEND_TIMEOUT, run_=run_)
            except TimedOut as e:
                return answer({"ok": False, "maybe": True, "error": f"{e}: it may have been submitted; check "
                               "your jobs before sending it again"}, 6)
            doc = submitted(code, said)
            return answer(doc, 0 if doc["ok"] else 6)
        if op == "jobs":
            code, said = in_login(_home(), ["squeue", "-u", _account(), "-h", "-o", _SQUEUE], 60, run_=run_)
            if code != 0:
                raise Refused(f"squeue: {said.strip()[-300:]}")
            rows = [dict(zip(JOB_FIELDS, f)) for f in (line.split("\x1f") for line in said.splitlines())
                    if len(f) == len(JOB_FIELDS)]
            return answer({"ok": True, "jobs": rows})
        if op == "cancel":
            code, said = in_login(_home(), ["scancel", got["--job"]], 60, run_=run_)
            return answer({"ok": code == 0, "job": got["--job"], "exit": code, "said": said.strip()[-2000:]},
                          0 if code == 0 else 6)
    except Refused as e:
        return refuse(str(e))
    return refuse(f"{op}: not done")


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
"""


def main(argv: list | None = None, me: str | None = None) -> int:
    """``me``: the gate's own path (bin/nomad-gate, as its launcher gives it)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    me = os.path.abspath(me or sys.argv[0])
    if argv[:1] == ["serve"] and len(argv) == 1:
        return serve(me)
    if argv[:1] == ["run"]:
        return run(argv[1:], me)
    if argv in (["version"], ["--version"]):
        return answer(_version())
    if argv == ["check"]:
        why = self_check(me)
        return answer({"ok": why is None, **({"error": why} if why else {})}, 0 if why is None else 3)
    sys.stderr.write((__doc__ or "").split("\n\n", 1)[1].split("\n\nInstalled beside")[0] + "\n\n" + USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
