# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad submit` through a node's local scratch (1.24).

A job runs faster, and spares the shared /scratch, when its files are on
the node's own disk. So by default `nomad submit` sends a single-node job as
a small bash script that, on the node, copies the job's folder (where it
runs: the folder nomad is run in, or its --chdir) to
/localscratch/USER/job_JOBID, runs the person's script there, then copies
what is new or changed back to the folder it ran from (as if it had run
there), checks every file it copied (sha256 on both sides), and only then
removes the local folder.

Before its time limit (Slurm's --signal=B:USR1, 10 minutes before) the job
stops the program and copies back what it has, then waits for the limit,
so Slurm still records it as TIMEOUT; `scancel -b -s USR1 JOB` does the
same at any time. A job cancelled (scancel) or preempted gets Slurm's
KillWait to copy back; what doesn't make it stays on the node, locked
while the job lives, and a small job pinned to that node (sent as it is
stopped), or the person's next staged job there, brings it back into a
folder of its own (nomad-recovered-NODE-TIME, in the job's folder beside
its Slurm output): nothing recovered ever overwrites anything.

The script nomad sends carries the person's own (as sbatch would have sent
it: what is on disk now) and nomad/submit/stage_helper.py, run with the
node's own python3; nothing of nomad's is needed on the node. It is kept in
~/.local/share/nomad/staged/ (Slurm's record names it), and starts with
lines that say which script it carries, so nomad still finds the person's
own script for a job it sent this way (`nomad submit --again`, and past
runs compared by script).

Off for one job with --no-localscratch; for everyone (a site, in
/etc/profile.d) or a person with NOMAD_LOCALSCRATCH=off (or another folder
pattern, with {user}: /localscratch/{user} is the default).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import time
from dataclasses import dataclass, field

from nomad.submit import apps, logs as logs_, spec

ENV = "NOMAD_LOCALSCRATCH"
ENV_MAX = "NOMAD_STAGE_MAX"
DEFAULT_BASE = "/localscratch/{user}"
MAX_BYTES = 50 * 1024 ** 3             # a job's folder larger than this runs where it is
ARRAY_SHARE = 10                       # an array's tasks each copy it: a tenth of that each
RESERVE_MIN = 50 * 1024 ** 3           # local scratch is often the node's own /: kept free
RESERVE_PCT = 10
WARN = 600                             # s before the time limit: stop, copy back
MIN_LIMIT = 1800                       # s: a job with a shorter limit isn't staged
LINGER = 1500                          # s: then wait for the limit (TIMEOUT as Slurm records it)
GRACE_STOP = 60                        # s the program gets to stop (USR1)
GRACE_TERM = 15                        # s it keeps of Slurm's KillWait (30) when stopped by Slurm
RESCUE_TIME = "02:00:00"               # the job that brings back what a stopped job left
MEASURE_ENTRIES = 200000               # at submission: entries counted before leaving it to the job
MEASURE_SECONDS = 5.0
STORE = "~/.local/share/nomad/staged"
KEEP_DAYS = 400                        # nomad's own copies older than this are removed
MARK = "# nomad-stage: 1"
SCRIPT_LINE = "# nomad-stage-script: "
B64_END = "NOMAD_SCRIPT_B64"
PY_END = "NOMAD_HELPER_PY"
_OFF = {"off", "no", "0", "false", "none"}
_ON = {"on", "yes", "1", "true", ""}
_PATH_OK = re.compile(r"/[A-Za-z0-9/._+{}-]*")
_SIGNAL = re.compile(r"(?:([RB]+):)?([A-Za-z0-9]+)(?:@\d+)?")


class StageError(ValueError):
    """A NOMAD_LOCALSCRATCH nomad can't use."""


@dataclass
class Stage:
    """What the job does on its node."""
    base: str                          # the person's local scratch folder, e.g. /localscratch/amy
    root: str                          # what must exist on the node: /localscratch
    results: str                       # the folder job folders go in: /scratch/amy/jobs
    name: str                          # the job's name, as in its folder's name
    folder: str                        # where it runs (what is copied)
    excludes: list = field(default_factory=list)   # relative to folder: never copied in
    size: int | None = None            # measured at submission (None: left to the job)
    files: int | None = None
    signal: bool = True                # nomad asks Slurm for USR1 before the limit
    forward: str | None = None         # the job's own --signal=B:SIG: forwarded to the program
    max_bytes: int = MAX_BYTES
    reserve_min: int = RESERVE_MIN
    reserve_pct: int = RESERVE_PCT
    warn: int = WARN
    linger: int = LINGER
    grace: int = GRACE_STOP
    term_grace: int = GRACE_TERM
    rescue_time: str = RESCUE_TIME
    array_ok: bool = False             # --localscratch given: an array task stages too

    def describe(self, user: str) -> str:
        size = f" ({_human(self.size)})" if self.size is not None else ""
        return (f"Through local scratch: on its node the job copies {self.folder}{size} to "
                f"{self.base}/job_JOBID, runs there, copies what is new or changed back to {self.folder}, "
                "checks each file and cleans up (--no-localscratch: as it is).")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{int(n)} B"


def base_of(env: dict, user: str) -> str | None:
    """The person's local scratch folder from NOMAD_LOCALSCRATCH (None: off)."""
    v = (env.get(ENV) or "").strip()
    if v.lower() in _OFF:
        return None
    if v.lower() in _ON:
        v = DEFAULT_BASE
    if not _PATH_OK.fullmatch(v) or "{user}" not in v or set(re.findall(r"\{([^{}]*)\}", v)) != {"user"} \
            or "//" in v or "/../" in v + "/" or "/./" in v + "/":
        raise StageError(f"${ENV}: off, on, or an absolute folder with {{user}} in it "
                         f"(like {DEFAULT_BASE}), not {v[:80]!r}")
    return os.path.normpath(v.replace("{user}", user))


def root_of(pattern: str) -> str:
    """What must be on the node for the job to stage: the folder the
    person's is made in (/localscratch for /localscratch/{user})."""
    prefix = pattern.split("{user}")[0]
    root = prefix.rstrip("/") if prefix.endswith("/") else os.path.dirname(prefix)
    return root or "/"


def max_bytes(env: dict) -> int:
    v = (env.get(ENV_MAX) or "").strip()
    if not v:
        return MAX_BYTES
    mb = spec.parse_memory_mb(v)
    if mb is None or mb <= 0:
        raise StageError(f"${ENV_MAX}: a size like 200G or 2T, not {v[:40]!r}")
    return int(mb * 1024 * 1024)


def results_base(logs: logs_.Logs, user: str, name: str | None = None, fs=None) -> str | None:
    """The folder each job's own folder goes in (its results go back
    there): nomad's folder for its output when it sets one, else the
    default's, when the person has a shared /scratch; None: nowhere."""
    pattern = logs.pattern
    if pattern is None:
        probe = logs_.choose(flag=None, env={}, user=user, here_dir="/", own_output=None, cannot=None, fs=fs)
        pattern = probe.pattern
    if pattern is None:
        return None
    parts = pattern.split("/")
    at = next((i for i, p in enumerate(parts) if "{jobid}" in p), None)
    if at is None:
        return None
    base = "/".join(parts[:at])
    if "{jobid}" in base:
        return None
    return logs_.folder_of(base, user=user, jobid="0", name=name) or "/"


# Programs that send jobs of their own: a job they send from the node's copy
# would start in it (gone when the job ends), so a script that runs one isn't
# staged. Any mention counts (a function, $(sbatch ...), xargs sbatch...).
_SENDS = re.compile(r"(?<![\w.-])(?:sbatch|snakemake|nextflow|cromwell|toil|rlaunch|qlaunch|parsl)(?![\w.-])"
                    r"|(?<![\w.-])nomad\s+submit\b")
_HOME_VAR = re.compile(r"\$\{?(?:SLURM_SUBMIT_DIR|PWD|NOMAD_LOCALSCRATCH)\}?(/[^$`]*)?")


def sends_jobs(text: str) -> str | None:
    """A program the script's commands name that sends jobs of its own
    (sbatch, nomad submit, a workflow tool), anywhere in them."""
    m = _SENDS.search("\n".join(spec.sbatch_lines(text or "")[1]))
    return " ".join(m.group(0).split()) if m else None


def cd_away(text: str) -> str | None:
    """A `cd` the script makes out of the folder it runs in (absolute, ~,
    up with .., a variable other than $SLURM_SUBMIT_DIR or $PWD, cd -,
    popd): then it doesn't run in the copy, and nothing is staged; or a job
    it sends itself (sbatch, nomad submit), which would start in the
    node's own copy. None when it stays."""
    for line in apps.commands_of(spec.sbatch_lines(text or "")[1]):
        words = apps._words(line)
        if not words:
            continue
        run, _launcher = apps._strip_launchers(words)
        first = os.path.basename(run[0]) if run else ""
        if first == "sbatch" or (first == "nomad" and "submit" in run[1:3]):
            return f"it sends a job itself ({first}{' submit' if first == 'nomad' else ''}), which would " \
                   "start in the node's own copy"
        if words[0] == "popd":
            return "it leaves its folder itself (popd)"
        if words[0] not in ("cd", "pushd"):
            continue
        args = [w for w in words[1:] if not w.startswith("-") or w == "-"]
        target = args[0] if args else "~"
        what = f"it leaves its folder itself ({words[0]} {target})"[:120]
        if target == "-":
            return what
        m = _HOME_VAR.fullmatch(target)
        if m:
            if ".." in (m.group(1) or "").split("/"):
                return what
            continue
        if "$" in target or "`" in target or target.startswith(("/", "~")):
            return what
        if ".." in target.split("/"):
            return what
    return None


def measure(folder: str, excludes: list, limit: int = MEASURE_ENTRIES,
            seconds: float = MEASURE_SECONDS) -> tuple[int, int, bool]:
    """(bytes in files, entries, complete) of the job's folder, counted
    for a few seconds at most (a large one is the job's to measure)."""
    total = n = 0
    t0 = time.monotonic()
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(os.path.join(folder, rel_dir) if rel_dir else folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if any(rel == x or rel.startswith(x + "/") for x in excludes):
                continue
            n += 1
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(rel)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            if n >= limit or (n % 1000 == 0 and time.monotonic() - t0 > seconds):
                return total, n, False
    return total, n, True


def _signal_of(value: str | None) -> tuple[bool, str | None]:
    """A --signal value: (it goes to the batch shell, its signal's name)."""
    m = _SIGNAL.fullmatch((value or "").strip())
    if not m:
        return False, None
    where, sig = m.group(1) or "", m.group(2)
    if sig.isdigit():
        return "B" in where, sig
    sig = sig.upper()
    return "B" in where, sig[3:] if sig.startswith("SIG") else sig


def decide(*, flag: bool | None, env: dict, user: str, script: str | None, text: str | None,
           folder: str, logs: logs_.Logs, name: str | None, nodes: int | None, options: list,
           limit_s: int | None = None, array: bool = False, home: str | None = None,
           measure_=None, fs=None) -> tuple[Stage | None, str | None]:
    """Whether the job goes through local scratch, and what to say when
    it doesn't (None: nothing to say, as when it is off). ``limit_s``: its
    time limit as it will be sent (None: none, or not known)."""
    if flag is False:
        return None, None
    v = (env.get(ENV) or "").strip()
    off = v.lower() in _OFF
    if off and not flag:
        return None, None                     # the site's or the person's choice: nothing to say
    pattern = DEFAULT_BASE if (off or v.lower() in _ON) else v
    try:
        base = base_of({ENV: pattern}, user)
        biggest = max_bytes(env)
    except StageError as e:
        return None, f"Not through local scratch: {e}."

    def no(why):
        return None, f"Not through local scratch: {why}."
    if not script or text is None:
        return no("no job script to carry")
    if any(n in ("test-only", "wrap") for n, _v in options):
        return no("no job script runs (--test-only or --wrap)")
    if not text.startswith("#!"):
        return no("the script doesn't start with #! (sbatch would refuse it)")
    if "\r\n" in text:
        return no("the script has DOS line breaks (sbatch would refuse it)")
    if any(c in script for c in "\n\r\t"):
        return no("the script's name has a line break or tab in it")
    if nodes and nodes > 1:
        return no(f"it asks for {nodes} nodes (each would have its own local scratch)")
    for o in spec.sbatch_lines(text)[0]:
        if o.split()[:1] in (["hetjob"], ["packjob"]):
            return no("a heterogeneous job")
    if limit_s is not None and 0 < limit_s < MIN_LIMIT:
        return no(f"its time limit ({limit_s // 60} min) is under {MIN_LIMIT // 60} minutes: too short to "
                  "gain from it, and to copy back before the limit")
    sends = sends_jobs(text)
    if sends:
        return no(f"it sends jobs itself ({sends}): one sent from the node's copy would start there")
    away = cd_away(text)
    if away:
        return no(away)
    if os.path.normpath(folder) == "/":
        return no("it runs in /")
    home = os.path.normpath(home if home is not None else os.path.expanduser("~"))
    if os.path.normpath(folder) == home:
        return no("it runs in your home folder itself (submit from a project's folder to stage it)")
    results = results_base(logs, user, name, fs=fs) or ""   # the job's own folder: for what is recovered
    folder = os.path.normpath(folder)
    excludes = []
    for inside in [x for x in (results, os.path.expanduser(STORE)) if x]:
        if inside.startswith(folder.rstrip("/") + "/"):
            excludes.append(inside[len(folder.rstrip("/")) + 1:])
    if base.startswith(folder.rstrip("/") + "/") or folder.startswith(base.rstrip("/") + "/") or folder == base:
        return no(f"its folder and {base} overlap")
    if array and not flag:
        return no("an array: its tasks would each copy the folder, with what the others brought back "
                  "(--localscratch stages it anyway)")
    if array:
        biggest //= ARRAY_SHARE
    size, files, complete = (measure_ or measure)(folder, excludes)
    if complete and size > biggest:
        return no(f"its folder ({folder}) holds {_human(size)}, over the {_human(biggest)} nomad stages "
                  + (f"for each task of an array (a tenth of ${ENV_MAX})" if array else f"(${ENV_MAX})")
                  + "; it runs there")
    warn = WARN if limit_s is None or limit_s <= 0 else min(WARN, max(300, limit_s // 6))
    sig_b, own = False, None
    own_value = None
    for n, v in options:
        if n == "signal":
            own_value = v
    if env.get("SBATCH_SIGNAL"):
        own_value = own_value or env["SBATCH_SIGNAL"]
    if own_value is not None:
        sig_b, own = _signal_of(own_value)
    return Stage(base=base, root=root_of(pattern), results=results.rstrip("/"),
                 name=logs_.safe_name(name), folder=folder, excludes=excludes,
                 size=size if complete else None, files=files if complete else None,
                 signal=own_value is None, forward=own if sig_b else None, max_bytes=biggest,
                 warn=warn, array_ok=bool(flag)), None


# ── the script sent ─────────────────────────────────────────────────────────

def header_lines(text: str) -> list:
    """The script's lines sbatch reads its options from: every comment
    line before its first command (the #! line aside), as they are."""
    out = []
    for i, raw in enumerate(text.splitlines()):
        s = raw.strip()
        if i == 0 and s.startswith("#!"):
            continue
        if not s:
            out.append("")
            continue
        if not s.startswith("#"):
            break
        out.append(raw)
    while out and not out[-1]:
        out.pop()
    return out


def helper_source() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "stage_helper.py"), encoding="utf-8") as f:
        return f.read()


def _q(v) -> str:
    return shlex.quote(str(v))


WRAPPER = r'''
exec 3<&0
_ns_lc="${LC_CTYPE+1}:${LC_CTYPE-}"
_ns_say() { printf 'nomad-stage: %s\n' "$*" >&2; }
_ns_stop=
trap '_ns_stop=${_ns_stop:-usr1}' USR1
trap '_ns_stop=term' TERM
_ns_path0=${PATH-}
_ns_hadpath=${PATH+1}
export PATH="/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"
_ns_py=
for _ns_p in /usr/bin/python3 /usr/libexec/platform-python /bin/python3; do
    if [ -x "$_ns_p" ]; then _ns_py=$_ns_p; break; fi
done
_ns_h() { "$_ns_py" -I -c "$_NS_HELPER" "$@"; }
_ns_alive() { [ -e "/proc/$1" ] && ! grep -q '^State:[[:space:]]*[ZX]' "/proc/$1/status" 2>/dev/null; }
_ns_node=$(hostname -s 2>/dev/null || hostname)
_ns_here=$PWD
_ns_job=${SLURM_JOB_ID:-}
_ns_token="${_ns_job}-${SLURM_RESTART_COUNT:-0}-$$-$(date +%s)"
_ns_snap=$(mktemp "${TMPDIR:-/tmp}/nomad-job.XXXXXX" 2>/dev/null) || _ns_snap=
if [ -z "$_ns_snap" ] || ! _ns_script > "$_ns_snap"; then
    _ns_say "couldn't write the job's script out on $_ns_node (in ${TMPDIR:-/tmp})"
    exit 1
fi
_ns_mode=here
_ns_why=
_ns_out=
_ns_err=
_ns_dest=
_ns_local=
_ns_short=
_ns_rescued=
if [ -z "$_ns_job" ]; then _ns_why="not in a Slurm job"
elif [ -z "$_ns_py" ]; then _ns_why="$_ns_node has no python3"
elif [ "${SLURM_JOB_NUM_NODES:-1}" != 1 ]; then _ns_why="the job has ${SLURM_JOB_NUM_NODES} nodes"
elif [ -n "${SLURM_ARRAY_TASK_ID:-}" ] && [ "$_NS_ARRAY_OK" != 1 ]; then
    _ns_why="an array task (staged only with nomad submit --localscratch)"
elif [ ! -d "$_NS_ROOT" ]; then _ns_why="$_ns_node has no $_NS_ROOT"
fi
if [ -z "$_ns_why" ]; then
    _ns_show=$(scontrol show job "$_ns_job" 2>/dev/null)
    _ns_out=$(printf '%s\n' "$_ns_show" | sed -n 's/^[[:space:]]*StdOut=//p' | head -n 1)
    _ns_err=$(printf '%s\n' "$_ns_show" | sed -n 's/^[[:space:]]*StdErr=//p' | head -n 1)
    _ns_tl=$(printf '%s\n' "$_ns_show" | sed -n 's/.*[[:space:]]TimeLimit=\([^[:space:]]*\).*/\1/p' | head -n 1)
    _ns_limit=$(_ns_h seconds "$_ns_tl")
    if [ "$_NS_SIGNAL" = 1 ] && [ -n "$_ns_limit" ] && [ "$_ns_limit" -ge 0 ] 2>/dev/null \
            && [ "$_ns_limit" -lt $((3 * _NS_WARN)) ]; then
        _ns_short=1
    fi
    if [ -n "$_NS_RESULTS" ]; then
        _ns_dest=$(_ns_h dest --stdout "$_ns_out" --base "$_NS_RESULTS" --job "$_ns_job" --name "$_NS_NAME" \
            --array-job "${SLURM_ARRAY_JOB_ID:-}" --task "${SLURM_ARRAY_TASK_ID:-}") || _ns_dest=
    fi
fi
if [ -z "$_ns_why" ]; then
    _ns_local=$_NS_BASE/job_$_ns_job
    if ! mkdir -p "$_NS_BASE" 2>/dev/null || ! { exec 9>>"$_NS_BASE/job_$_ns_job.lock"; } 2>/dev/null; then
        _ns_why="couldn't use $_NS_BASE"; _ns_local=
    elif ! _ns_h lock --fd 9; then
        _ns_why="another run of this job holds $_NS_BASE/job_$_ns_job.lock"; _ns_local=
    fi
fi
if [ -z "$_ns_why" ]; then
    _ns_x=()
    for _ns_e in "${_NS_EXCLUDE[@]}"; do _ns_x+=(--exclude "$_ns_e"); done
    if _ns_h in --src "$_ns_here" --local "$_ns_local" --dest "$_ns_here" --jobdir "$_ns_dest" --job "$_ns_job" \
            --max "$_NS_MAX" --reserve-min "$_NS_RESERVE_MIN" --reserve-pct "$_NS_RESERVE_PCT" \
            --token "$_ns_token" --python "$_ns_py" --helper "$_NS_HELPER" "${_ns_x[@]}"; then
        if cd "$_ns_local"; then
            _ns_mode=local
        else
            _ns_why="couldn't go into $_ns_local"
        fi
    else
        _ns_why=said
    fi
fi
if [ "$_ns_mode" = local ]; then
    export NOMAD_SUBMIT_DIR="$_ns_here" NOMAD_LOCALSCRATCH="$_ns_local"
    export SLURM_SUBMIT_DIR="$_ns_local"
elif [ "$_ns_why" != said ]; then
    _ns_say "runs in $_ns_here, not through local scratch: $_ns_why"
fi
_ns_rescue_pid=
_ns_rescue() {
    # A small job pinned to this node that brings back, once this one has
    # ended, whatever is still here: sent in the background (at most 5 s),
    # so the copy back doesn't wait for it; cancelled if nothing is left.
    [ -n "$_ns_rescued" ] && return 0
    _ns_rescued=1
    _ns_logs=${_ns_dest:-$_ns_here}
    if [ "$_ns_mode" != local ] || [ ! -e "$_ns_local/.nomad-stage/recover.sh" ]; then return 0; fi
    _ns_rid=${TMPDIR:-/tmp}/nomad-rescue.$_ns_job.$$
    timeout 5 sbatch --parsable -w "$_ns_node" --dependency="afterany:$_ns_job" -t "$_NS_RESCUE_TIME" \
        -N 1 -n 1 -c 1 --mem=1G -J "nomad-recover-$_ns_job" -o "${_ns_logs//\%/%%}/nomad-recover-%j.out" \
        --export=NONE ${SLURM_JOB_PARTITION:+-p "$SLURM_JOB_PARTITION"} \
        ${SLURM_JOB_ACCOUNT:+-A "$SLURM_JOB_ACCOUNT"} "$_ns_local/.nomad-stage/recover.sh" > "$_ns_rid" 2>&1 &
    _ns_rescue_pid=$!
}
_ns_rescue_said() {
    # what became of it: kept when something is left (say so), else cancelled
    [ -n "$_ns_rescue_pid" ] || return 0
    wait "$_ns_rescue_pid" 2>/dev/null
    _ns_rc2=$?
    _ns_r=$(head -c 300 "$_ns_rid" 2>/dev/null)
    rm -f "$_ns_rid"
    _ns_rescue_pid=
    _ns_r1=${_ns_r%%;*}
    if [ "$1" = keep ]; then
        if [ "$_ns_rc2" = 0 ]; then
            _ns_say "submitted job $_ns_r1 to bring back, once this job has ended, what is still on $_ns_node"
        else
            _ns_say "couldn't submit a job to bring back what is left (${_ns_r:-no answer from sbatch})"
        fi
    elif [ "$_ns_rc2" = 0 ] && [ -n "$_ns_r1" ]; then
        timeout 5 scancel "$_ns_r1" 2>/dev/null || :
    fi
}
if [ "$_ns_stop" = usr1 ] && [ -n "$_ns_short" ]; then
    _ns_say "its time limit is too short for nomad's copy back before it: it copies back when the job ends"
    _ns_stop=
    _ns_short=said
fi
if [ -n "$_ns_stop" ]; then
    _ns_say "stopped before the script started"
    if [ -n "$_ns_local" ]; then
        cd "$_ns_here" 2>/dev/null || cd /
        _ns_h discard --local "$_ns_local" --token "$_ns_token" || :
    fi
    rm -f "$_ns_snap"
    if [ "$_ns_stop" = term ]; then exit 143; else exit 138; fi
fi
if [ -n "$_NS_FORWARD" ]; then
    trap '[ -n "${_ns_pid:-}" ] && kill -s "$_NS_FORWARD" "$_ns_pid" 2>/dev/null' "$_NS_FORWARD"
fi
IFS= read -r _ns_line < "$_ns_snap"
_ns_line=${_ns_line#\#!}
_ns_line=${_ns_line#"${_ns_line%%[![:space:]]*}"}
_ns_interp=${_ns_line%%[[:space:]]*}
_ns_arg=${_ns_line#"$_ns_interp"}
_ns_arg=${_ns_arg#"${_ns_arg%%[![:space:]]*}"}
_ns_arg=${_ns_arg%"${_ns_arg##*[![:space:]]}"}
_ns_shim=
if [ "$_ns_mode" = local ] && [ -n "$_ns_hadpath" ] && [ -e "$_ns_local/.nomad-stage/helper.py" ] \
        && mkdir -p "$_ns_local/.nomad-stage/bin" 2>/dev/null; then
    # A job it sends (sbatch in a helper script, a workflow tool) would start in
    # this node's copy, gone when this job ends: it starts where this one came from.
    { printf '#!/bin/bash\n'; printf 'exec %q -I %q sbatch-shim %q %q %q "$@"\n' "$_ns_py" \
        "$_ns_local/.nomad-stage/helper.py" "$_ns_local/.nomad-stage/bin" "$_ns_local" "$_ns_here"; } \
        > "$_ns_local/.nomad-stage/bin/sbatch" \
        && chmod 700 "$_ns_local/.nomad-stage/bin/sbatch" && _ns_shim=$_ns_local/.nomad-stage/bin
fi
if [ -n "$_ns_hadpath" ]; then export PATH="${_ns_shim:+$_ns_shim:}$_ns_path0"; else unset PATH; fi
if [ -n "$_ns_py" ]; then
    "$_ns_py" -I -c "$_NS_HELPER" exec --lc-ctype "$_ns_lc" "$_ns_interp" ${_ns_arg:+"$_ns_arg"} "$_ns_snap" "$@" <&3 3<&- &
else
    "$_ns_interp" ${_ns_arg:+"$_ns_arg"} "$_ns_snap" "$@" <&3 3<&- &
fi
_ns_pid=$!
export PATH="/usr/bin:/bin:/usr/sbin:/sbin${_ns_path0:+:$_ns_path0}"
_ns_halt() {
    if [ "$_ns_stop" = term ]; then
        # Slurm has sent TERM to every process of the job, and KILLs them all
        # after KillWait: the program keeps part of it, the copy back the rest.
        _ns_say "the job is being stopped: the program has ${_NS_TERM_GRACE}s to end, then nomad copies back"
        _ns_rescue
        _ns_grace=$_NS_TERM_GRACE
    else
        _ns_say "asked to stop (USR1): stopping the program, then copying back"
        if [ -n "$_ns_py" ]; then _ns_h kill --pid "$_ns_pid" --signal 15; else kill -TERM "$_ns_pid" 2>/dev/null; fi
        _ns_grace=$_NS_GRACE
    fi
    _ns_i=0
    while _ns_alive "$_ns_pid" && [ "$_ns_i" -lt "$((_ns_grace * 5))" ]; do sleep 0.2; _ns_i=$((_ns_i + 1)); done
    if _ns_alive "$_ns_pid"; then
        if [ -n "$_ns_py" ]; then _ns_h kill --pid "$_ns_pid" --signal 9; else kill -KILL "$_ns_pid" 2>/dev/null; fi
    fi
    wait "$_ns_pid" 2>/dev/null
    _ns_rc=$?
}
_ns_rc=0
while :; do
    if [ -n "$_ns_stop" ]; then
        if [ "$_ns_stop" = usr1 ] && [ -n "$_ns_short" ]; then
            _ns_say "its time limit is too short for nomad's copy back before it: it copies back when the job ends"
            _ns_stop=
            _ns_short=said
        elif _ns_alive "$_ns_pid"; then
            _ns_halt
            break
        fi
    fi
    wait "$_ns_pid"
    _ns_rc=$?
    if _ns_alive "$_ns_pid"; then continue; fi
    break
done
trap '' USR1
_ns_final=$_ns_rc
if [ "$_ns_mode" = local ]; then
    _ns_h reap --job "$_ns_job" --keep $$ || :
    cd "$_ns_here" 2>/dev/null || cd /
    _ns_p=()
    for _ns_f in "$_ns_out" "$_ns_err"; do
        [ -n "$_ns_f" ] && _ns_p+=(--protect-path "$_ns_f")
    done
    _ns_h back --local "$_ns_local" "${_ns_p[@]}"
    _ns_b=$?
    if [ "$_ns_b" = 0 ] || [ "$_ns_b" = 3 ]; then
        _ns_h clean --local "$_ns_local" || :
        _ns_rescue_said cancel
        if [ "$_ns_b" = 3 ] && [ "$_ns_rc" = 0 ]; then _ns_final=75; fi
    else
        _ns_say "what didn't go back is in $_ns_local on $_ns_node"
        _ns_rescue
        _ns_rescue_said keep
        if [ "$_ns_rc" = 0 ]; then _ns_final=75; fi
    fi
elif [ -n "$_ns_job" ] && [ ! -e "$_NS_BASE/job_$_ns_job" ]; then
    rm -f "$_NS_BASE/job_$_ns_job.lock" 2>/dev/null       # took the lock, but ran where it was
fi
if [ -z "$_ns_stop" ] && [ -n "$_ns_py" ] && [ -n "$_ns_job" ] && [ -d "$_NS_BASE" ]; then
    _ns_s=$(_ns_h seconds "$(squeue -h -j "$_ns_job" -o %L 2>/dev/null)")
    if [ -z "$_ns_s" ] || [ "$_ns_s" -lt 0 ] 2>/dev/null || [ "$_ns_s" -ge 900 ] 2>/dev/null; then
        _ns_h recover --base "$_NS_BASE" --self "$_ns_job" || :
    fi
fi
rm -f "$_ns_snap"
if [ "$_ns_stop" = usr1 ] && [ "$_NS_SIGNAL" = 1 ] && [ -n "$_ns_py" ]; then
    _ns_s=$(_ns_h seconds "$(squeue -h -j "$_ns_job" -o %L 2>/dev/null)")
    if [ -n "$_ns_s" ] && [ "$_ns_s" -ge 0 ] 2>/dev/null && [ "$_ns_s" -le $((_NS_WARN + 300)) ]; then
        _ns_say "copied back; waiting for the time limit, so Slurm records the job as TIMEOUT"
        trap - TERM
        sleep "$_NS_LINGER"
    fi
fi
exit "$_ns_final"
'''


def wrapper_text(script: str, text: str, st: Stage, version: str) -> str:
    """The bash script nomad sends in place of the person's."""
    head = ["#!/bin/bash", MARK, SCRIPT_LINE + shlex.quote(script)]
    head += header_lines(text)
    if st.signal:
        head.append(f"#SBATCH --signal=B:USR1@{st.warn}")
    head += [
        f"# Sent by nomad {version} (nomad submit): {posixpath_base(script)} runs through the node's local",
        f"# scratch. The job copies its folder to {st.base}/job_JOBID, runs the script there,",
        "# copies what is new or changed back to the folder it ran from, checks each file",
        "# (sha256) and only then removes the local copy. The person's script follows, as it was",
        "# when sent (base64). --no-localscratch sends a script as it is.",
    ]
    consts = [
        f"_NS_BASE={_q(st.base)}", f"_NS_ROOT={_q(st.root)}", f"_NS_RESULTS={_q(st.results)}",
        f"_NS_NAME={_q(st.name)}", f"_NS_MAX={int(st.max_bytes)}", f"_NS_RESERVE_MIN={int(st.reserve_min)}",
        f"_NS_RESERVE_PCT={int(st.reserve_pct)}", f"_NS_WARN={int(st.warn)}", f"_NS_LINGER={int(st.linger)}",
        f"_NS_GRACE={int(st.grace)}", f"_NS_TERM_GRACE={int(st.term_grace)}",
        f"_NS_RESCUE_TIME={_q(st.rescue_time)}", f"_NS_SIGNAL={1 if st.signal else 0}",
        f"_NS_ARRAY_OK={1 if st.array_ok else 0}",
        f"_NS_FORWARD={_q(st.forward or '')}",
        "_NS_EXCLUDE=(" + " ".join(_q(x) for x in st.excludes) + ")",
    ]
    b64 = base64.encodebytes(text.encode("utf-8")).decode("ascii").rstrip("\n")
    helper = helper_source()
    if any(line == PY_END for line in helper.splitlines()):
        raise RuntimeError("stage_helper.py holds the here-document's end word")
    body = ["_ns_script() {", f"base64 -d <<'{B64_END}'", b64, B64_END, "}",
            f"IFS= read -r -d '' _NS_HELPER <<'{PY_END}' || :", helper.rstrip("\n"), PY_END]
    return "\n".join(head + consts + body) + WRAPPER


def posixpath_base(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def unwrap(text: str | None) -> tuple[str, str] | None:
    """For a script nomad sent through local scratch: (the person's own
    script, its text as it was sent). None for any other script."""
    if not text or not text.startswith("#!/bin/bash\n" + MARK + "\n"):
        return None
    lines = text.split("\n")
    if len(lines) < 3 or not lines[2].startswith(SCRIPT_LINE):
        return None
    try:
        words = shlex.split(lines[2][len(SCRIPT_LINE):])
    except ValueError:
        return None
    if len(words) != 1:
        return None
    try:
        start = lines.index(f"base64 -d <<'{B64_END}'") + 1
        end = lines.index(B64_END, start)
        body = base64.b64decode("".join(lines[start:end]), validate=False).decode("utf-8", errors="replace")
    except (ValueError, base64.binascii.Error):
        return None
    return words[0], body


def store_path(script: str, wrapper: str, home: str | None = None) -> str:
    """Where nomad keeps the script it sends (named for the person's, and
    its content: the same job sent again is the same file)."""
    store = os.path.expanduser(STORE) if home is None else os.path.join(home, STORE[2:])
    digest = hashlib.sha256(wrapper.encode("utf-8")).hexdigest()[:16]
    return os.path.join(store, f"{logs_.safe_name(posixpath_base(script))}-{digest}.sh")


def keep(path: str, wrapper: str) -> str:
    """Write it (private), unless it is there already; old ones removed."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if not os.path.isfile(path):
        tmp = f"{path}.{os.getpid()}.new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(wrapper)
        os.replace(tmp, path)
    else:
        os.utime(path)                     # used again: kept as long as the newest use
    prune(folder)
    return path


def prune(folder: str, days: int = KEEP_DAYS) -> None:
    cut = time.time() - days * 86400
    try:
        with os.scandir(folder) as it:
            for e in it:
                try:
                    if e.name.endswith(".sh") and e.is_file(follow_symlinks=False) and \
                            e.stat(follow_symlinks=False).st_mtime < cut:
                        os.unlink(e.path)
                except OSError:
                    pass
    except OSError:
        pass
