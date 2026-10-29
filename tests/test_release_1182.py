# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.18.2: a job's Slurm output in a folder of its own
(/scratch/USER/jobs/JOBID-NAME): sent to start in two minutes, its output
set and its start moved to now in one update; never held."""
import os
import subprocess

import pytest
from click.testing import CliRunner

from nomad.submit import logs
from nomad.submit.cli import plan as REAL_PLAN

SHOW = """JobId={jid} {array}JobName={name}
   UserId=amy(1001) GroupId=amy(1001) MCS_label=N/A
   Priority=0 Nice=0 Account=chem QOS=normal
   JobState=PENDING Reason=BeginTime Dependency=(null)
   WorkDir={wd}
   StdErr={err}
   StdIn=/dev/null
   StdOut={out}
"""


def show(jid="4242", name="water", wd="/home/amy/w", out=None, err=None, array=False):
    if out is None:
        out = f"{wd}/slurm-{jid}_4294967294.out" if array else f"{wd}/slurm-{jid}.out"
    return SHOW.format(jid=jid, name=name, wd=wd, out=out, err=out if err is None else err,
                       array=f"ArrayJobId={jid} ArrayTaskId=1-4 " if array else "")


class Slurm:
    """scontrol's answers, and what was asked of it. ``refuse``: the
    updates that fail (those setting StdOut, or all)."""
    def __init__(self, text, refuse=None):
        self.text, self.refuse, self.calls = text, refuse, []

    def __call__(self, argv):
        self.calls.append(argv)
        args = argv[3:] if argv[1] == "-M" else argv[1:]
        if args[0] == "show":
            return subprocess.CompletedProcess(argv, 0, self.text, "")
        if self.refuse == "all" or (self.refuse == "stdout" and any(a.startswith("StdOut=") for a in args)):
            return subprocess.CompletedProcess(argv, 1, "", "denied")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def updates(self):
        return [[a for a in c if "=" in a and not a.startswith("jobid=")] for c in self.calls
                if "update" in c]


# ── the setting ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, want", [
    ("here", None),
    ("/data/amy/logs", "/data/amy/logs/{jobid}-{name}"),
    ("/data/amy/logs/", "/data/amy/logs/{jobid}-{name}"),
    ("logs", "/home/amy/w/logs/{jobid}-{name}"),
    ("/data/{user}/{name}/{jobid}", "/data/{user}/{name}/{jobid}"),
])
def test_settings(value, want):
    assert logs.pattern_of(value, "--logs", "/home/amy/w") == want


@pytest.mark.parametrize("value, bad", [
    ("/data/{who}/{jobid}", "{who}"),
    ("/data/{name}", "needs {jobid}"),
    ("/data/a\\b", "backslash"),
    ("", "empty"),
    ("/data/{jobid", "{ or }"),
])
def test_settings_refused(value, bad):
    with pytest.raises(logs.LogsError) as e:
        logs.pattern_of(value, "--logs", "/home/amy/w")
    assert bad in str(e.value)


def _choose(tmp_path, monkeypatch, scratch=True, **kw):
    root = tmp_path / "scratch" / "amy"
    if scratch:
        root.mkdir(parents=True)
    monkeypatch.setattr(logs, "DEFAULT", str(tmp_path / "scratch" / "{user}" / "jobs" / "{jobid}-{name}"))
    monkeypatch.setattr(logs, "DEFAULT_ROOT", str(tmp_path / "scratch" / "{user}"))
    args = {"flag": None, "env": {}, "user": "amy", "here_dir": "/home/amy/w", "own_output": None,
            "cannot": None, "fs": lambda p: "nfs4"}
    args.update(kw)
    return logs.choose(**args)


def test_the_default_needs_your_shared_scratch(tmp_path, monkeypatch):
    c = _choose(tmp_path, monkeypatch)
    assert c.pattern.endswith("/scratch/{user}/jobs/{jobid}-{name}") and c.delay and c.source == "default"
    c = _choose(tmp_path / "x", monkeypatch, scratch=False)
    assert c.pattern is None and c.kind == "no-scratch" and "you have no" in c.why
    c = _choose(tmp_path / "y", monkeypatch, fs=lambda p: "xfs")          # a node's own disk, maybe
    assert c.pattern is None and c.kind == "no-scratch" and "is on xfs" in c.why and "NOMAD_LOGS" in c.why
    c = _choose(tmp_path / "z", monkeypatch, fs=lambda p: "xfs", env={"NOMAD_LOGS": str(tmp_path / "l")})
    assert c.pattern and c.source == "env"                                   # the site's say wins


def test_what_decides(tmp_path, monkeypatch):
    assert _choose(tmp_path / "a", monkeypatch, flag="here").kind == "here"
    assert _choose(tmp_path / "b", monkeypatch, env={"NOMAD_LOGS": "here"}).kind == "here"
    c = _choose(tmp_path / "c", monkeypatch, flag="/data/x", env={"NOMAD_LOGS": "here"})
    assert c.pattern == "/data/x/{jobid}-{name}" and c.source == "flag"
    c = _choose(tmp_path / "d", monkeypatch, own_output="#SBATCH --output")
    assert c.pattern is None and c.kind == "own"
    c = _choose(tmp_path / "e", monkeypatch, cannot="sbatch waits")
    assert c.pattern is None and c.kind == "cannot"


def test_names_in_folders():
    assert logs.safe_name("water opt") == "water_opt"
    assert logs.safe_name("100%/x") == "100_x"
    assert logs.safe_name("") == "job" and len(logs.safe_name("a" * 200)) == 64
    assert logs.folder_of("/s/{user}/jobs/{jobid}-{name}", user="amy", jobid="7", name="w 1") == "/s/amy/jobs/7-w_1"


def test_fs_type(tmp_path):
    m = tmp_path / "mounts"
    m.write_text("/dev/sda1 / xfs rw 0 0\nspdrstor01:/scratch /scratch nfs4 rw 0 0\n"
                 "/dev/sdb1 /scratch/local xfs rw 0 0\n")
    assert logs.fs_type("/scratch/amy", str(m)) == "nfs4"
    assert logs.fs_type("/scratch/local/x", str(m)) == "xfs" and logs.fs_type("/home", str(m)) == "xfs"


def test_a_job_folder_is_new_and_private(tmp_path):
    import stat
    pat = str(tmp_path / "s" / "amy" / "jobs" / "{jobid}-{name}")
    (tmp_path / "s" / "amy").mkdir(parents=True)
    a = logs.make_folder(pat, user="amy", jobid="7", name="w")
    b = logs.make_folder(pat, user="amy", jobid="7", name="w")      # a job number used before
    assert a.endswith("/jobs/7-w") and b.endswith("/jobs/7-w.2")
    assert stat.S_IMODE(os.stat(tmp_path / "s" / "amy" / "jobs").st_mode) == 0o700
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "jobs").symlink_to(tmp_path / "s" / "amy")    # a link of one's own: fine
    assert logs.make_folder(str(tmp_path / "t" / "jobs" / "{jobid}"), user="amy", jobid="8", name=None)


def test_a_folder_you_give_keeps_your_umask(tmp_path):
    import stat
    old = os.umask(0o022)
    try:
        out = logs.make_folder(str(tmp_path / "lab" / "logs" / "{jobid}"), user="amy", jobid="9", name=None,
                               strict=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "lab" / "logs").st_mode) == 0o755 and out.endswith("/9")


# ── reading the job back, setting its output ────────────────────────────────

def test_parse_and_default():
    j = logs.parse_show_job(show())
    assert (j.name, j.workdir, j.array) == ("water", "/home/amy/w", False) and logs.is_default(j, "4242")
    j = logs.parse_show_job(show(array=True))
    assert j.array and j.name == "water" and logs.is_default(j, "4242")
    assert not logs.is_default(logs.parse_show_job(show(out="/home/amy/w/my.log")), "4242")
    assert not logs.is_default(logs.parse_show_job(show(err="/home/amy/w/err.log")), "4242")
    assert logs.is_default(logs.parse_show_job(show(err="")), "4242")      # stderr in stdout
    assert logs.parse_show_job("") is None


def test_job_numbers():
    assert logs.job_number("4242;hpc1\n") == ("4242", "hpc1")
    assert logs.job_number("Remember: /scratch is purged\n4242\n") == ("4242", None)
    assert logs.job_number("") == (None, None) and logs.job_number("Submitted batch job 7") == (None, None)


def _placed(tmp_path, text, refuse=None, **kw):
    slurm = Slurm(text, refuse)
    pattern = str(tmp_path / "jobs" / "{jobid}-{name}")
    out = logs.Logs(pattern, "default", "", delay=kw.pop("delay", True))
    p = logs.place(out, "4242", kw.pop("cluster", None), user="amy", run=slurm, **kw)
    return p, slurm


def test_output_set_and_started_in_one_update(tmp_path):
    p, slurm = _placed(tmp_path, show())
    assert slurm.updates() == [[f"StdOut={tmp_path}/jobs/4242-water/slurm-4242.out", "StartTime=now"]]
    assert p.path == f"{tmp_path}/jobs/4242-water/slurm-4242.out" and p.note is None and not p.late
    assert (tmp_path / "jobs" / "4242-water").is_dir()


def test_an_array_keeps_slurms_task_numbers(tmp_path):
    p, slurm = _placed(tmp_path, show(array=True))
    assert slurm.updates()[0][0] == f"StdOut={tmp_path}/jobs/4242-water/slurm-%A_%a.out"
    assert p.path.endswith("/4242-water/slurm-4242_TASK.out")


def test_a_percent_in_the_folder_is_kept(tmp_path):
    slurm = Slurm(show())
    out = logs.Logs(str(tmp_path / "100%" / "{jobid}"), "flag", "", delay=True)
    logs.place(out, "4242", None, user="amy", run=slurm)
    assert slurm.updates()[0][0] == f"StdOut={tmp_path}/100%%/4242/slurm-4242.out"


def test_a_cluster_is_named_to_scontrol(tmp_path):
    p, slurm = _placed(tmp_path, show(), cluster="hpc1")
    assert all(c[1:3] == ["-M", "hpc1"] for c in slurm.calls)


@pytest.mark.parametrize("text, note", [
    (show(out="/home/amy/w/my.log"), "where Slurm was told: /home/amy/w/my.log"),
    ("", "couldn't read the job back"),
])
def test_left_as_slurm_has_it_but_started_now(tmp_path, text, note):
    p, slurm = _placed(tmp_path, text)
    assert slurm.updates() == [["StartTime=now"]] and p.path is None and note in p.note and not p.late


def test_started_whatever_happens(tmp_path):
    p, slurm = _placed(tmp_path, show(), refuse="stdout")
    assert slurm.updates()[-1] == ["StartTime=now"] and p.path is None and "didn't take the folder" in p.note

    def no_room(pattern, **kw):
        raise PermissionError(13, "Permission denied", "/s/amy/jobs")
    p, slurm = _placed(tmp_path, show(), makedirs=no_room)
    assert slurm.updates() == [["StartTime=now"]] and "couldn't be made (Permission denied: /s/amy/jobs)" in p.note

    def interrupted(pattern, **kw):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _placed(tmp_path, show(), makedirs=interrupted)
    p, slurm = _placed(tmp_path, show(), refuse="all")
    assert p.late and p.path is None              # it starts on its own, in DELAY s


def test_without_a_delay_nothing_moves_its_start(tmp_path):
    p, slurm = _placed(tmp_path, show(), delay=False)
    assert slurm.updates() == [[f"StdOut={tmp_path}/jobs/4242-water/slurm-4242.out"]]


# As Slurm 25.11.5 shows them (spydur, 29 Oct; the account renamed).
REAL_ONE = """JobId=86275 JobName=wrap
   WorkDir=/home/amy/jobtest2
   StdErr=
   StdOut=/home/amy/jobtest2/slurm-86275.out
"""
REAL_ARRAY = """JobId=86276 ArrayJobId=86276 ArrayTaskId=1-2 JobName=arr test
   WorkDir=/home/amy/jobtest2
   StdErr=
   StdOut=/home/amy/jobtest2/slurm-86276_4294967294.out
"""


def test_slurms_own_words():
    j = logs.parse_show_job(REAL_ONE)
    assert (j.name, j.array) == ("wrap", False) and logs.is_default(j, "86275")
    j = logs.parse_show_job(REAL_ARRAY)
    assert (j.name, j.array) == ("arr test", True) and logs.is_default(j, "86276")
    assert logs.folder_of(logs.DEFAULT, user="amy", jobid="86276", name=j.name) == \
        "/scratch/amy/jobs/86276-arr_test"


# ── nomad submit ────────────────────────────────────────────────────────────

@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A cluster where the person has a scratch folder (on NFS); scontrol recorded."""
    import nomad.submit.cli as sc
    from tests.test_release_1180 import SACCT, Fake
    w = tmp_path / "w"
    w.mkdir()
    (w / "w.com").write_text("%nprocshared=8\n%mem=16GB\n# b3lyp\n\nt\n\n0 1\nO\n")
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\n"
                              "g16 < w.com > w.log\n")
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)))
    monkeypatch.setattr(sc, "plan", lambda *a, **k: REAL_PLAN(*a, **{**k, "user": "amy", "run_sacct": f,
                                                                   "run_slurm": f}))
    monkeypatch.setattr(sc, "me", lambda: "amy")
    monkeypatch.setattr(sc.cluster, "slurm_version", lambda run=None: (25, 11))
    monkeypatch.setattr(sc.cluster, "extra_is_constraints", lambda run=None: False)
    for var in ("NOMAD_LOGS", "SBATCH_OUTPUT", "SBATCH_ERROR", "SBATCH_WAIT", "SBATCH_JOB_NAME",
                "SBATCH_IGNORE_PBS"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "scratch" / "amy").mkdir(parents=True)
    monkeypatch.setattr(logs, "DEFAULT", str(tmp_path / "scratch" / "{user}" / "jobs" / "{jobid}-{name}"))
    monkeypatch.setattr(logs, "DEFAULT_ROOT", str(tmp_path / "scratch" / "{user}"))
    monkeypatch.setattr(logs, "fs_type", lambda p: "nfs4")
    sent = []

    def sbatch(argv, cwd):
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, "4242;hpc1\n", "")
    monkeypatch.setattr(sc, "_run_sbatch", sbatch)
    slurm = Slurm(show(wd=str(w)))
    monkeypatch.setattr(logs, "_run", slurm)
    monkeypatch.chdir(w)
    return tmp_path, w, sent, slurm


def _again_line(scratch, monkeypatch, line):
    import nomad.submit.cli as sc
    from tests.test_release_1180 import SACCT, Fake
    tmp, w, sent, slurm = scratch
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)).replace("sbatch job.sh", line, 1))
    monkeypatch.setattr(sc, "plan", lambda *a, **k: REAL_PLAN(*a, **{**k, "user": "amy", "run_sacct": f,
                                                                   "run_slurm": f}))
    return scratch


def test_submit_puts_the_output_in_scratch(scratch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    assert sent[-1][:3] == ["sbatch", "--parsable", "--begin=now+120"] and "--hold" not in sent[-1]
    assert slurm.calls[0][1:3] == ["-M", "hpc1"]
    assert slurm.updates() == [[f"StdOut={tmp}/scratch/amy/jobs/4242-water/slurm-4242.out", "StartTime=now"]]
    assert f"Its output: {tmp}/scratch/amy/jobs/JOBID-water/ (nomad sends it to start in 2 minutes" in r.output
    assert f"Its output: {tmp}/scratch/amy/jobs/4242-water/slurm-4242.out" in r.output


def test_submit_dry_run_sends_nothing(scratch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--dry-run", "job.sh"])
    assert r.exit_code == 0 and "JOBID-water/ (nomad sends it" in r.output
    assert "--begin" not in r.output                  # the command shown is one to run by hand
    assert sent == [] and slurm.calls == []


@pytest.mark.parametrize("script, env, args, said", [
    ("#SBATCH -o run.log\n", {}, [], None),
    ("#SBATCH --error=e.log\n", {}, [], None),
    ("#SBATCH --out=slurm-%j.out\n", {}, [], None),          # any prefix sbatch takes
    ("#PBS -o out.log\n", {}, [], None),
    ("", {"SBATCH_OUTPUT": "x.log"}, [], None),
    ("", {}, ["--logs", "here"], "(--logs is here)"),
    ("", {"NOMAD_LOGS": "here"}, [], "($NOMAD_LOGS is here)"),
    ("#SBATCH --wait\n", {}, [], "sbatch waits"),
    ("", {"SBATCH_WAIT": ""}, [], "sbatch waits"),            # set, even empty, sbatch waits
    ("#SBATCH --begin=now+1hour\n", {}, [], "its own start time"),
    ("#PBS -a 1200\n", {}, [], "its own start time"),
    ("", {"NOMAD_LOGS": "/x/{who}"}, [], "$NOMAD_LOGS can't be used"),
])
def test_submit_leaves_slurms_place(scratch, monkeypatch, script, env, args, said):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water\n" + script + "g16 < w.com > w.log\n")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = CliRunner().invoke(submit_cmd, ["--yes", *args, "job.sh"])
    assert r.exit_code == 0, r.output
    assert not any(a.startswith("--begin=now+") for a in sent[-1][:3]) and slurm.calls == []
    if said:
        assert said in r.output


def test_submit_logs_in_a_folder_you_give(scratch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--yes", "--logs", "runs", "job.sh"])
    assert r.exit_code == 0, r.output
    assert slurm.updates()[0][0] == f"StdOut={w}/runs/4242-water/slurm-4242.out"
    r = CliRunner().invoke(submit_cmd, ["--yes", "--logs", "/x/{name}", "job.sh"])
    assert r.exit_code != 0 and "needs {jobid}" in r.output


def test_submit_a_held_job_stays_held(scratch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water --hold\ng16 < w.com > w.log\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    assert not any("release" in c for c in slurm.calls) and slurm.updates()[0][-1] == "StartTime=now"
    assert "It is held, as the job asks: scontrol release 4242" in r.output


def test_submit_says_when_its_start_couldnt_move(scratch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    slurm.refuse = "all"
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and "It starts in 2 minutes at the latest" in r.output


def test_submit_again_gets_a_new_folder_and_not_nomads_begin(scratch, monkeypatch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = _again_line(scratch, monkeypatch,
                                      "sbatch --parsable --begin=now+120 --mem=12G --extra=nomad=1.18.2;via=cli job.sh")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "101"])
    assert r.exit_code == 0, r.output
    assert [a for a in sent[-1] if a.startswith("--begin")] == ["--begin=now+120"]
    assert slurm.updates()[0][0].endswith("/jobs/4242-water/slurm-4242.out")


def test_again_of_a_quiet_job_isnt_quiet(scratch, monkeypatch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = _again_line(scratch, monkeypatch, "sbatch -Q job.sh")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "101"])
    assert r.exit_code == 0, r.output
    assert "--quiet" not in sent[-1] and "Submitted job 4242." in r.output


def test_a_wrapper_line_before_the_number(scratch, monkeypatch):
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    monkeypatch.setattr(sc, "_run_sbatch", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 0, "Remember: /scratch is purged after 30 days\n4242\n", ""))
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and slurm.calls[0][-1] == "4242" and "Submitted job 4242." in r.output


def test_sbatch_failing_after_it_submitted(scratch, monkeypatch):
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    monkeypatch.setattr(sc, "_run_sbatch", lambda argv, cwd: subprocess.CompletedProcess(
        argv, -9, "4242\n", ""))
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code != 0 and "but it had submitted job 4242" in r.output
    assert slurm.updates()[-1][-1] == "StartTime=now"          # its output still set, started


def test_sbatch_not_answering(scratch, monkeypatch):
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch

    def slow(argv, cwd):
        raise subprocess.TimeoutExpired(argv, 300)
    monkeypatch.setattr(sc, "_run_sbatch", slow)
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code != 0 and "didn't answer in 5 minutes" in r.output and slurm.calls == []


def test_odd_bytes_from_sbatch_dont_stop_nomad():
    """sbatch and scontrol are read with errors='replace': a job_submit
    message in Latin-1 is no traceback."""
    import inspect
    import nomad.submit.cli as sc
    assert 'errors="replace"' in inspect.getsource(sc._run_sbatch)
    assert 'errors="replace"' in inspect.getsource(logs._run)


RUNNER = r'''
import os, sys
sys.path.insert(0, {repo!r})
import nomad.submit.cli as sc
from nomad.submit import logs
from tests.test_release_1180 import SACCT, Fake
w = {w!r}
f = Fake(sacct=SACCT.replace("/home/amy/w", w))
real = sc.plan
sc.plan = lambda *a, **k: real(*a, **{{**k, "user": "amy", "run_sacct": f, "run_slurm": f}})
sc.me = lambda: "amy"
sc.cluster.slurm_version = lambda run=None: (25, 11)
sc.cluster.extra_is_constraints = lambda run=None: False
logs.DEFAULT = {default!r}
logs.DEFAULT_ROOT = {root!r}
logs.fs_type = lambda p: "nfs4"
os.chdir(w)
sc.submit_cmd.main(["--yes", "job.sh"], standalone_mode=True)
'''


@pytest.mark.parametrize("sig", ["SIGHUP", "SIGTERM", "SIGINT", "SIGQUIT", "SIGKILL"])
def test_stopped_at_any_moment_the_job_isnt_left_waiting(tmp_path, sig):
    """nomad as a process, stopped while sbatch runs: the job it made was
    sent with --begin=now+120 and no hold, so it starts on its own."""
    import signal
    import sys
    import time as _t
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    w = tmp_path / "w"
    w.mkdir()
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water -c 4 --mem=8G -t 4:00:00 -p basic\nhostname\n")
    (tmp_path / "scratch" / "amy").mkdir(parents=True)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    st = tmp_path / "state"
    (bin_ / "sbatch").write_text(f"#!/bin/bash\necho \"$*\" >> {st}\nsleep 2\necho 4242\n")
    (bin_ / "scontrol").write_text(f"#!/bin/bash\necho \"scontrol $*\" >> {st}\n")
    for f in ("sbatch", "scontrol"):
        (bin_ / f).chmod(0o755)
    (tmp_path / "run.py").write_text(RUNNER.format(
        repo=repo, w=str(w), default=str(tmp_path / "scratch" / "{user}" / "jobs" / "{jobid}-{name}"),
        root=str(tmp_path / "scratch" / "{user}")))
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env.pop("NOMAD_LOGS", None)
    p = subprocess.Popen([sys.executable, str(tmp_path / "run.py")], env=env, cwd=repo,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(300):                              # until sbatch is running
        if st.exists():
            break
        _t.sleep(0.02)
    p.send_signal(getattr(signal, sig))
    p.communicate(timeout=30)
    sent = st.read_text().splitlines()[0].split()
    assert "--begin=now+120" in sent and "--hold" not in sent and "-H" not in sent


@pytest.mark.parametrize("line", ["#SLURM --begin=now+1hour", "#SBATCH--begin=now+1hour"])
def test_directives_as_sbatch_reads_them(scratch, line):
    """sbatch takes #SLURM lines and #SBATCH with no space: a job's own
    start time written so is its own, never moved by nomad."""
    from nomad.submit.cli import submit_cmd
    from nomad.submit import spec
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text(f"#!/bin/bash\n#SBATCH -J water\n{line}\ng16 < w.com\n")
    assert spec.script_options((w / "job.sh").read_text()).get("begin") == "now+1hour"
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and not any(a.startswith("--begin=now+120") for a in sent[-1]) and slurm.calls == []


# ── review 3 ────────────────────────────────────────────────────────────────

class SlurmAfter(Slurm):
    """An update that reports an error after it took effect: show then
    gives ``after``."""
    def __init__(self, text, after, rc=1):
        super().__init__(text)
        self.after, self.rc, self.updated = after, rc, False

    def __call__(self, argv):
        self.calls.append(argv)
        args = argv[3:] if argv[1] == "-M" else argv[1:]
        if args[0] == "show":
            return subprocess.CompletedProcess(argv, 0, self.after if self.updated else self.text, "")
        self.updated = True
        return subprocess.CompletedProcess(argv, self.rc, "", "Job violates accounting/QOS policy")


def _times(text, submit, eligible):
    import time as _t
    f = "%Y-%m-%dT%H:%M:%S"
    return text.replace("   WorkDir=", f"   SubmitTime={_t.strftime(f, _t.localtime(submit))} "
                                      f"EligibleTime={_t.strftime(f, _t.localtime(eligible))}\n   WorkDir=")


def test_an_error_after_the_update_took_effect(tmp_path):
    import time as _t
    now = _t.time()
    folder = f"{tmp_path}/jobs/4242-water"
    after = _times(show(out=f"{folder}/slurm-4242.out"), now - 1, now - 1)
    slurm = SlurmAfter(_times(show(), now, now + 120), after)
    out = logs.Logs(str(tmp_path / "jobs" / "{jobid}-{name}"), "default", "", delay=True)
    p = logs.place(out, "4242", None, user="amy", run=slurm)
    assert p.path == f"{folder}/slurm-4242.out" and "but took the folder" in p.note and not p.late
    assert slurm.updates() == [[f"StdOut={folder}/slurm-4242.out", "StartTime=now"]]   # no second try


def test_a_site_start_time_is_left(tmp_path):
    import time as _t
    now = _t.time()
    slurm = Slurm(_times(show(), now, now + 3600))          # a job_submit plugin: an hour
    out = logs.Logs(str(tmp_path / "jobs" / "{jobid}-{name}"), "default", "", delay=True)
    p = logs.place(out, "4242", None, user="amy", run=slurm)
    assert slurm.updates() == [[f"StdOut={tmp_path}/jobs/4242-water/slurm-4242.out"]]
    assert "start time is the site's" in p.note and not p.late
    slurm = Slurm(_times(show(), now, now + 121))           # nomad's own
    logs.place(out, "4243", None, user="amy", run=slurm)
    assert slurm.updates()[0][-1] == "StartTime=now"


def test_times_are_asked_in_slurms_standard_form():
    import inspect
    assert '"SLURM_TIME_FORMAT": "standard"' in inspect.getsource(logs._run)


@pytest.mark.parametrize("line, said", [
    ("#SBATCH -t 1:00:00 --deadline=now+61minutes", "--deadline"),
    ("#SBATCH -Hb 23:00", "its own start time"),          # -H, then -b with its value
])
def test_a_deadline_or_a_start_in_a_cluster_of_options(scratch, line, said):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text(f"#!/bin/bash\n#SBATCH -J water\n{line}\ng16 < w.com\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--keep", "job.sh"])
    assert r.exit_code == 0, r.output
    assert not any(a.startswith("--begin=now+") for a in sent[-1]) and said in r.output


def test_short_option_clusters():
    from nomad.submit import spec
    sub = spec.SubmitLine()
    spec._options(["-Hb", "23:00", "-c4", "-vvn", "2"], sub)
    assert sub.options == [("hold", None), ("begin", "23:00"), ("cpus-per-task", "4"),
                           ("verbose", None), ("verbose", None), ("ntasks", "2")]


def test_nomads_own_begin_isnt_called_the_jobs(scratch, monkeypatch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = _again_line(scratch, monkeypatch,
                                      "sbatch --parsable --begin=now+120 --mem=12G job.sh")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "101"])
    assert r.exit_code == 0 and "Its --begin" not in r.output
    tmp, w, sent, slurm = _again_line(scratch, monkeypatch, "sbatch --begin=now+1hour job.sh")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "101"])
    assert "Its --begin (a time for its first run) isn't sent again." in r.output
