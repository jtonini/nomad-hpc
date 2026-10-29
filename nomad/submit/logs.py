# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Where `nomad submit` puts a job's Slurm output (its .out file, with
stderr in it unless the job splits them): a folder of its own per job,
by default /scratch/USER/jobs/JOBID-NAME.

Slurm never makes the folder an output file goes in (a job whose folder is
missing fails with no output at all), and a job's number is known only
once it is submitted. So nomad submits the job to start two minutes from
now (--begin=now+120), reads back where Slurm will write its output, and
-- only when that is Slurm's own default -- makes the job's folder, and in
one update points the job's output there and lets it start now. Should
nomad be stopped in between (Ctrl-C, a dropped session, a kill), or Slurm
not take the folder, nothing is left waiting: the job starts on its own
within the two minutes, its output where Slurm puts it. Anything the job or
the site set (#SBATCH --output or --error, SBATCH_OUTPUT, a job_submit
plugin) is left as it is; nomad never holds or releases a job.

The choice, first found: `nomad submit --logs`, then $NOMAD_LOGS (a site
can set it for everyone in /etc/profile.d, a person in their own shell
setup), then the default. Each is `here` (Slurm's own place: beside the
job), a folder (each job's folder goes in it), or a pattern with {user},
{jobid} and {name}. The default applies only when /scratch/USER exists, on
a filesystem the compute nodes share (NFS, Lustre, GPFS, ...): nomad never
makes a person's scratch folder.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

DEFAULT = "/scratch/{user}/jobs/{jobid}-{name}"
DELAY = 120                               # s: the start nomad gives a job, until its output is set
DEFAULT_ROOT = "/scratch/{user}"          # must exist for the default to apply
HERE = "here"
EACH = "{jobid}-{name}"                   # a job's folder in a folder given
FIELDS = ("user", "jobid", "name")
_FIELD = re.compile(r"\{([^{}]*)\}")
_NAME_KEEP = re.compile(r"[^A-Za-z0-9._+-]+")
# Filesystems a cluster's nodes share: the default goes only on one of them.
SHARED_FS = {"nfs", "nfs4", "lustre", "gpfs", "beegfs", "ceph", "fuse.ceph", "fuse.ceph-fuse", "cifs",
             "smb3", "glusterfs", "fuse.glusterfs", "panfs", "wekafs", "orangefs", "pvfs2", "fuse.sshfs",
             "fuse.juicefs", "cvfs", "acfs", "fhgfs", "fuse.lizardfs", "fuse.mfs", "fuse.moosefs",
             "gfs2", "ocfs2"}


class LogsError(ValueError):
    """A --logs or $NOMAD_LOGS nomad can't use."""


@dataclass
class Logs:
    """Where a job's output goes: ``pattern`` None is Slurm's own place."""
    pattern: str | None
    source: str                           # "flag", "env", "default"
    why: str                              # said when it is Slurm's own place
    delay: bool = False                   # nomad sends it to start in DELAY s (to set its output first)
    kind: str = "folder"                  # folder; here, own (the job's own), cannot, no-scratch


def _check_pattern(p: str, what: str) -> str:
    for f in _FIELD.findall(p):
        if f not in FIELDS:
            raise LogsError(f"{what}: {{{f}}} isn't one of {{user}}, {{jobid}}, {{name}}")
    if "{" in _FIELD.sub("", p) or "}" in _FIELD.sub("", p):
        raise LogsError(f"{what}: a {{ or }} that isn't {{user}}, {{jobid}} or {{name}}")
    if "\\" in p:
        raise LogsError(f"{what}: no backslashes (Slurm would then read the path as it is, without %)")
    if "{jobid}" not in p:
        raise LogsError(f"{what}: the pattern needs {{jobid}} (each job its own folder)")
    return p


def pattern_of(value: str, what: str, here_dir: str) -> str | None:
    """A setting as a pattern: None for `here`; a folder gets each job's
    folder in it; a relative one is from ``here_dir``."""
    v = (value or "").strip()
    if not v:
        raise LogsError(f"{what}: empty (give here, a folder, or a pattern with {{jobid}})")
    if v == HERE:
        return None
    v = os.path.expanduser(v)
    if "{" not in v:
        v = v.rstrip("/") + "/" + EACH
    if not v.startswith("/"):
        v = os.path.join(here_dir, v)
    return _check_pattern(os.path.normpath(v), what)


def fs_type(path: str, mounts: str = "/proc/self/mounts") -> str | None:
    """The type of the filesystem a path is on (the longest mount point
    above it), or None when it can't be told."""
    try:
        real = os.path.realpath(path)
        with open(mounts) as f:
            rows = [ln.split() for ln in f]
    except OSError:
        return None
    best, kind = "", None
    for row in rows:
        if len(row) < 3:
            continue
        point = row[1].replace("\\040", " ")
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind = point, row[2]
    return kind


def choose(*, flag: str | None, env: dict, user: str, here_dir: str, own_output: str | None,
           cannot: str | None, fs=None) -> Logs:
    """Where the output goes, and why. ``own_output``: what in the job sets
    its output (or None); ``cannot``: why nomad can't set it before the job
    starts (sbatch --wait or --test-only, its own --begin, a heterogeneous
    job), or None."""
    if flag is not None:
        source, value = "flag", flag
    elif env.get("NOMAD_LOGS"):
        source, value = "env", env["NOMAD_LOGS"]
    else:
        source, value = "default", DEFAULT
    what = {"flag": "--logs", "env": "$NOMAD_LOGS", "default": "the default"}[source]
    pattern = pattern_of(value, what, here_dir) if source != "default" else DEFAULT
    if pattern is None:
        return Logs(None, source, f"its output goes where Slurm puts it ({what} is here)", kind="here")
    if own_output:
        return Logs(None, source, f"its output goes where the job says ({own_output})", kind="own")
    if cannot:
        return Logs(None, source, f"its output goes where Slurm puts it ({cannot})", kind="cannot")
    if source == "default":
        root = DEFAULT_ROOT.format(user=user)
        try:
            os.listdir(root)                     # an automounted folder is mounted by a look inside
        except OSError:
            pass
        if not os.path.isdir(root) or not os.access(root, os.W_OK | os.X_OK):
            return Logs(None, source, f"its output goes where Slurm puts it (beside the job): you have no "
                                      f"{root}", kind="no-scratch")
        kind = (fs or fs_type)(root)
        if kind not in SHARED_FS:
            return Logs(None, source, f"its output goes where Slurm puts it (beside the job): {root} is on "
                                      f"{kind or 'a filesystem nomad can not tell'}, which compute nodes may "
                                      "not share (set NOMAD_LOGS to use it)", kind="no-scratch")
    return Logs(pattern, source, "", delay=True)


def safe_name(name: str | None) -> str:
    """A job's name as part of a folder's: letters, digits and . _ + -
    only (no %, which Slurm would expand), at most 64."""
    n = _NAME_KEEP.sub("_", (name or "").strip()).strip("._") or "job"
    return n[:64]


def folder_of(pattern: str, *, user: str, jobid: str, name: str | None) -> str:
    return _FIELD.sub(lambda m: {"user": user, "jobid": str(jobid), "name": safe_name(name)}[m.group(1)],
                      pattern)


def shown(pattern: str, *, user: str, name: str | None, jobid: str | None = None) -> str:
    """The folder as it will be, for showing before the job has a number."""
    return folder_of(pattern, user=user, jobid=jobid or "JOBID", name=name)


def make_folder(pattern: str, *, user: str, jobid: str, name: str | None, strict: bool = True) -> str:
    """Make a job's folder: what comes before its {jobid} part, then the
    job's own, new (a name already there, from a job number used before,
    gets .2, .3, ...). ``strict``, for the default nomad chose: what nomad
    makes before it is private (0700), and the folder it ends in must be the
    person's own (or their own link to one); else as the person's umask."""
    parts = pattern.split("/")
    at = next(i for i, p in enumerate(parts) if "{jobid}" in p)
    base = folder_of("/".join(parts[:at]), user=user, jobid=jobid, name=name) or "/"
    rest = [folder_of(p, user=user, jobid=jobid, name=name) for p in parts[at:]]
    path = "/"
    for part in [p for p in base.split("/") if p]:
        path = os.path.join(path, part)
        try:
            os.mkdir(path, 0o700 if strict else 0o777)      # yours: as your umask (a lab's folder, say)
        except FileExistsError:
            pass
    if not os.path.isdir(base):
        raise NotADirectoryError(20, "not a folder", base)
    if strict and (os.stat(base).st_uid != os.getuid() or os.lstat(base).st_uid != os.getuid()):
        raise PermissionError(1, "not your own folder", base)
    first = os.path.join(base, rest[0])
    job = first
    for n in range(2, 100):
        try:
            os.mkdir(job)
            break
        except FileExistsError:
            job = f"{first}.{n}"
    else:
        raise FileExistsError(17, "too many folders of that name", first)
    out = os.path.join(job, *rest[1:]) if len(rest) > 1 else job
    if out != job:
        os.makedirs(out, exist_ok=True)
    return out


# ── after sbatch: read the job back, set its output, start it ───────────────

@dataclass
class JobOut:
    """What Slurm says of a job just submitted: enough to tell its output
    is Slurm's default, and when it may start."""
    name: str | None
    workdir: str | None
    stdout: str | None
    stderr: str | None
    array: bool
    het: bool
    submitted: float | None = None        # epoch seconds
    eligible: float | None = None         # its begin time: when it may start


def parse_show_job(text: str) -> JobOut | None:
    """`scontrol show job JOBID` (not -o): its first record."""
    if not text or "JobId=" not in text:
        return None
    rec = text.split("\n\n")[0]
    first = rec.lstrip().split("\n", 1)[0]
    m = re.search(r"(?:^|\s)JobName=(.*)$", first)          # last on its line (after ArrayJobId=...)
    fields = {}
    for line in rec.split("\n"):
        s = line.strip()
        for key in ("WorkDir", "StdOut", "StdErr"):
            if s.startswith(key + "="):
                fields[key] = s[len(key) + 1:]
    array = re.search(r"(^|\s)ArrayJobId=\d+", rec) is not None
    het = re.search(r"(^|\s)HetJobId=\d+", rec) is not None
    return JobOut(m.group(1) if m else None, fields.get("WorkDir"), fields.get("StdOut"),
                  fields.get("StdErr"), array, het, _when(rec, "SubmitTime"), _when(rec, "EligibleTime"))


def _when(rec: str, key: str) -> float | None:
    """A time scontrol shows (in its standard form: nomad asks for it)."""
    m = re.search(rf"(?:^|\s){key}=(\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d)", rec)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def is_default(job: JobOut, jobid: str) -> bool:
    """Whether the job's output is where Slurm puts it by default
    (slurm-JOBID.out, or slurm-JOBID_TASK.out for an array, in its folder),
    with stderr in it."""
    if not job.workdir or not job.stdout:
        return False
    if job.stderr and job.stderr != job.stdout:
        return False
    base = job.workdir.rstrip("/") + "/"
    if not job.stdout.startswith(base):
        return False
    rest = job.stdout[len(base):]
    if job.array:
        return re.fullmatch(rf"slurm-{re.escape(jobid)}_[^/]+\.out", rest) is not None
    return rest == f"slurm-{jobid}.out"


_NUMBER = re.compile(r"(\d+)(?:;(\S+))?")


def job_number(stdout: str | None) -> tuple:
    """What `sbatch --parsable` printed: (job number, cluster or None), or
    (None, None). A site's wrapper may print lines before it: the last line
    that is a job number counts."""
    for line in reversed((stdout or "").strip().splitlines()):
        m = _NUMBER.fullmatch(line.strip())
        if m:
            return m.group(1), m.group(2)
    return None, None


def _run(argv: list) -> subprocess.CompletedProcess:
    # Times in Slurm's standard form, whatever the person's SLURM_TIME_FORMAT.
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, stdin=subprocess.DEVNULL,
                          env={**os.environ, "SLURM_TIME_FORMAT": "standard"})


@dataclass
class Placed:
    path: str | None                  # where its output goes (None: Slurm's own place)
    note: str | None                  # what to say
    late: bool = False                # it starts in DELAY s after all (its start couldn't be moved)


def place(logs: Logs, jobid: str, cluster: str | None, *, user: str, run=None,
          makedirs=None) -> Placed:
    """For a job submitted just now (to start in DELAY s, when logs.delay):
    where its output goes, set in the same update that lets it start now;
    or, when its output can't or mustn't be set, only its start moved to
    now. Whatever happens, the job starts: now, or in DELAY s on its own.
    What is said is what Slurm holds after (an update can report an error
    after it took effect)."""
    run = run or _run
    makedirs = makedirs or make_folder
    m = ["-M", cluster] if cluster else []
    started, mine = False, logs.delay
    path, note = None, None
    try:
        path, note, started, mine = _set_output(logs, jobid, m, user=user, run=run, makedirs=makedirs)
    finally:
        if logs.delay and mine and not started:
            started = _start_now(jobid, m, run)
    return Placed(path, note, late=logs.delay and mine and not started)


def _show(jobid: str, m: list, run) -> JobOut | None:
    try:
        r = run(["scontrol", *m, "show", "job", jobid])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_show_job(r.stdout) if r.returncode == 0 else None


def _now_eligible(job: JobOut | None) -> bool:
    return job is not None and job.eligible is not None and job.eligible <= time.time() + 5


def _start_now(jobid: str, m: list, run) -> bool:
    try:
        if run(["scontrol", *m, "update", f"jobid={jobid}", "StartTime=now"]).returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _now_eligible(_show(jobid, m, run))           # an error after it took effect


def _set_output(logs: Logs, jobid: str, m: list, *, user: str, run, makedirs) -> tuple:
    """(where its output goes, what to say, whether it may start now,
    whether its start is still nomad's to move)."""
    try:
        job = _show(jobid, m, run)
        if job is None:
            return None, "nomad couldn't read the job back from Slurm: its output goes where Slurm puts it.", \
                False, True
        # Its start is nomad's DELAY unless the site (a job_submit plugin) set another.
        mine = not logs.delay or job.eligible is None or job.submitted is None or \
            abs((job.eligible - job.submitted) - DELAY) <= 30
        site_start = "" if mine else " Its start time is the site's: left as it is."
        if job.het:
            return None, "A heterogeneous job: its output goes where Slurm puts it." + site_start, False, mine
        if not is_default(job, jobid):
            return None, f"Its output goes where Slurm was told: {job.stdout or '(not said)'}." + site_start, \
                False, mine
        folder = makedirs(logs.pattern, user=user, jobid=jobid, name=job.name, strict=logs.source == "default")
        out = folder.replace("%", "%%") + ("/slurm-%A_%a.out" if job.array else f"/slurm-{jobid}.out")
        shown_path = folder + (f"/slurm-{jobid}_TASK.out" if job.array else f"/slurm-{jobid}.out")
        # One update: should Slurm refuse the output, it stops there, and the
        # start isn't moved either (the job then starts in DELAY s, as sent).
        try:
            u = run(["scontrol", *m, "update", f"jobid={jobid}", f"StdOut={out}"]
                    + (["StartTime=now"] if logs.delay and mine else []))
            ok = u.returncode == 0
            why = "" if ok else ((u.stderr or u.stdout or "").strip()[-200:] or f"exit {u.returncode}")
        except subprocess.TimeoutExpired:
            ok, why = False, "no answer in 60 s"
        if ok:
            return shown_path, (site_start.strip() or None), bool(logs.delay and mine), mine
        # What Slurm holds now: an error can come after the update took effect.
        after = _show(jobid, m, run)
        if after is not None and (after.stdout or "").startswith(folder + "/"):
            return shown_path, f"Slurm reported an error ({why}), but took the folder." + site_start, \
                _now_eligible(after), mine
        return None, f"Slurm didn't take the folder for its output ({why}): it goes where Slurm puts it." \
            + site_start, False, mine
    except OSError as e:
        where = f" {e.filename}" if getattr(e, "filename", None) else ""
        return None, (f"Its folder couldn't be made ({e.strerror or e}{':' + where if where else ''}): its "
                      "output goes where Slurm puts it."), False, True
    except subprocess.TimeoutExpired:
        return None, "Slurm didn't answer: its output goes where Slurm puts it.", False, True
