# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.24: nomad submit through a node's local scratch. A single-node job is
sent as nomad's own script that copies the job's folder to
/localscratch/USER/job_JOBID, runs the person's script there, copies back
what is new or changed to the job's own folder (where its Slurm output is),
checks each file and only then removes the local copy."""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from nomad.submit import logs, stage
from nomad.submit import stage_helper as h
from nomad.submit.cli import plan as REAL_PLAN
from tests.test_release_1182 import Slurm, scratch, show  # noqa: F401  (fixture)

BASH = shutil.which("bash")


# ── the helper: in, back, clean ─────────────────────────────────────────────

def _tree(root, files):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)


def _stage_in(tmp_path, files=None, **kw):
    src = tmp_path / "w"
    src.mkdir(parents=True, exist_ok=True)
    _tree(str(src), files or {"in.txt": "input\n", "sub/keep.dat": "x\n"})
    local = tmp_path / "ls" / "amy" / "job_4242"
    dest = src                                                # what changes goes back where it ran
    jobdir = tmp_path / "scratch" / "amy" / "jobs" / "4242-water"
    jobdir.mkdir(parents=True, exist_ok=True)
    argv = ["--src", str(src), "--local", str(local), "--dest", str(dest), "--jobdir", str(jobdir), "--job", "4242",
            "--max", str(kw.get("max", 10 ** 9)), "--reserve-min", "0", "--reserve-pct", "0"]
    for x in kw.get("exclude", []):
        argv += ["--exclude", x]
    return src, local, dest, h.cmd_in(argv)


@pytest.fixture(params=["rsync", "python"])
def copier(request, monkeypatch):
    """Both ways of copying: rsync, and Python's own where a node has none."""
    if request.param == "rsync":
        if not shutil.which("rsync"):
            pytest.skip("no rsync here")
        monkeypatch.setattr(h, "which", lambda name: shutil.which(name))
    else:
        monkeypatch.setattr(h, "which", lambda name: None)
    return request.param


def test_only_what_is_new_or_changed_goes_back(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    assert rc == 0 and (local / "in.txt").read_text() == "input\n"
    assert stat.S_IMODE(os.stat(local).st_mode) == 0o700
    (local / "out.txt").write_text("result\n")
    (local / "sub" / "keep.dat").write_text("changed\n")
    (local / "new" / "deep").mkdir(parents=True)
    (local / "new" / "deep" / "r.txt").write_text("r\n")
    (local / "empty").mkdir()
    os.symlink("out.txt", local / "link")
    before = os.stat(src / "in.txt")
    assert h.cmd_back(["--local", str(local)]) == 0
    got = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.name != h.FOLDER_LOCK)
    assert got == ["empty", "in.txt", "link", "new", "new/deep", "new/deep/r.txt", "out.txt", "sub", "sub/keep.dat"]
    after = os.stat(src / "in.txt")                              # untouched inputs aren't written again
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert (dest / "sub" / "keep.dat").read_text() == "changed\n" and os.readlink(dest / "link") == "out.txt"
    assert h.read_info(str(local))["state"] == "copied back"
    assert h.cmd_clean(["--local", str(local)]) == 0 and not local.exists()


def test_a_file_changed_in_place_goes_back(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    p = local / "in.txt"
    time.sleep(0.01)
    p.write_text("input\nmore\n")
    assert h.cmd_back(["--local", str(local)]) == 0
    assert (dest / "in.txt").read_text() == "input\nmore\n"


def test_a_copy_that_doesnt_check_out_is_kept(tmp_path, monkeypatch, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (local / "out.txt").write_text("result\n")
    monkeypatch.setattr(h, "same", lambda *a: False)
    assert h.cmd_back(["--local", str(local)]) == 1
    info = h.read_info(str(local))
    assert info["state"] == "copy back failed" and info["left"] == ["out.txt"]
    assert h.cmd_clean(["--local", str(local)]) == 1 and (local / "out.txt").exists()


def test_slurms_own_output_is_never_overwritten(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (dest / "slurm-4242.out").write_text("the job's log\n")
    (local / "slurm-4242.out").write_text("the job wrote one too\n")
    assert h.cmd_back(["--local", str(local), "--protect-path", str(dest / "slurm-4242.out")]) == 1
    assert (dest / "slurm-4242.out").read_text() == "the job's log\n"
    assert (local / "slurm-4242.out").exists()


def test_too_big_or_no_room_runs_in_place(tmp_path, monkeypatch):
    src, local, dest, rc = _stage_in(tmp_path, files={"big": "x" * 5000}, max=1000)
    assert rc == h.NO_ROOM and not local.exists()
    src, local, dest, rc = _stage_in(tmp_path / "b", files={"f": "x"})
    assert rc == 0
    shutil.rmtree(local)

    class Full:
        f_bavail, f_frsize, f_blocks = 10, 1, 100
    monkeypatch.setattr(h.os, "statvfs", lambda p: Full)
    src, local, dest, rc = _stage_in(tmp_path / "c", files={"f": "x" * 50})
    assert rc == h.NO_ROOM and not local.exists()


def test_excluded_folders_arent_copied_in(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"a": "1", "jobs/9-old/r": "2", "jobsx": "3"},
                                     exclude=["jobs"])
    assert rc == 0 and (local / "a").exists() and (local / "jobsx").exists() and not (local / "jobs").exists()


def test_an_earlier_run_of_the_same_job_is_moved_aside_never_over_this_one(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (local / "first-run.txt").write_text("1\n")             # requeued before it copied back
    src, local, dest, rc = _stage_in(tmp_path)
    assert rc == 0 and not (local / "first-run.txt").exists() and not (dest / "first-run.txt").exists()
    aside = [p for p in local.parent.iterdir() if p.name.startswith("job_4242.") and p.is_dir()]
    assert len(aside) == 1 and (aside[0] / "first-run.txt").read_text() == "1\n"


def test_a_partial_copy_in_is_only_removed(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    info = h.read_info(str(local))
    info["state"] = "copying in"                           # stopped while it was copied in
    h.write_info(str(local), info)
    src, local, dest, rc = _stage_in(tmp_path)
    assert rc == 0 and [p.name for p in local.parent.iterdir() if p.is_dir()] == ["job_4242"]


def test_a_file_replaced_with_its_old_size_and_time_goes_back(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    p = local / "in.txt"
    st = os.stat(p)
    tmp = local / "x.tmp"
    tmp.write_text("INPUT\n")                              # same size
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))     # and the old time (cp -p, tar x, rsync -a)
    os.replace(tmp, p)
    assert h.cmd_back(["--local", str(local)]) == 0
    assert (dest / "in.txt").read_text() == "INPUT\n"


def test_links_that_lead_out_of_the_folder_still_lead_there(tmp_path, copier):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "ref.txt").write_text("ref\n")
    src = tmp_path / "w"
    src.mkdir()
    os.symlink("../shared/ref.txt", src / "ref")
    os.symlink("../summary.csv", src / "summary.csv")
    os.symlink("in.txt", src / "inside")
    src, local, dest, rc = _stage_in(tmp_path)
    assert rc == 0
    assert os.readlink(local / "ref") == str(shared / "ref.txt") and (local / "ref").read_text() == "ref\n"
    assert os.readlink(local / "summary.csv") == str(tmp_path / "summary.csv")
    assert os.readlink(local / "inside") == "in.txt"
    with open(local / "summary.csv", "a") as f:                  # written where it always was
        f.write("row\n")
    assert (tmp_path / "summary.csv").read_text() == "row\n"
    names = sorted(p.name for p in src.iterdir())
    assert h.cmd_back(["--local", str(local)]) == 0
    assert sorted(p.name for p in src.iterdir() if p.name != h.FOLDER_LOCK) == names


def test_a_file_changed_where_it_ran_meanwhile_is_never_overwritten(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"notes.txt": "v1\n", "new.txt": "x"})
    os.unlink(src / "new.txt")
    (local / "notes.txt").write_text("the job's\n")
    (src / "notes.txt").write_text("the person's edit\n")             # while the job ran
    (local / "made.txt").write_text("job\n")
    (src / "made.txt").write_text("another job's\n")                  # appeared there meanwhile
    assert h.cmd_back(["--local", str(local)]) == h.CONFLICTS
    assert (src / "notes.txt").read_text() == "the person's edit\n" and (src / "made.txt").read_text() == "another job's\n"
    aside = tmp_path / "scratch" / "amy" / "jobs" / "4242-water" / "nomad-conflicts-4242"
    assert (aside / "notes.txt").read_text() == "the job's\n" and (aside / "made.txt").read_text() == "job\n"
    assert h.cmd_clean(["--local", str(local)]) == 0                    # all of it is somewhere, checked


def test_rows_two_jobs_added_are_both_kept(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"summary.csv": "header\n"})
    with open(local / "summary.csv", "a") as f:
        f.write("row-A\n")
    with open(src / "summary.csv", "a") as f:                       # another job, done first
        f.write("row-B\n")
    assert h.cmd_back(["--local", str(local)]) == 0
    assert (src / "summary.csv").read_text() == "header\nrow-B\nrow-A\n"


def test_a_copy_cut_short_never_leaves_half_a_file(tmp_path, monkeypatch):
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    (src / "big").write_text("new" * 1000)
    (dst / "big").write_text("old, whole")

    def torn(a, b):
        with open(b, "w") as f:
            f.write("ne")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(h.shutil, "copy2", torn)
    with pytest.raises(OSError):
        h._copy_entry(str(src), str(dst), "big", "f", None)
    assert (dst / "big").read_text() == "old, whole" and sorted(p.name for p in dst.iterdir()) == ["big"]


def test_nomads_own_folders_arent_copied_in(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"a": "1", "nomad-recovered-n1-2026/x": "2",
                                                      "nomad-conflicts-11/y": "3", "sub/nomad-recovered-z": "4"})
    assert rc == 0 and (local / "a").exists() and (local / "sub" / "nomad-recovered-z").exists()
    assert not (local / "nomad-recovered-n1-2026").exists() and not (local / "nomad-conflicts-11").exists()


def test_where_results_go(tmp_path):
    base = tmp_path / "jobs"
    made = base / "4242-water"
    made.mkdir(parents=True)
    out = []
    run = lambda *a: (out.clear(), h.cmd_dest(list(a)))[1]  # noqa: E731
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert h.cmd_dest(["--stdout", f"{made}/slurm-4242.out", "--base", str(base), "--job", "4242",
                           "--name", "water"]) == 0
        assert h.cmd_dest(["--stdout", "/home/amy/w/slurm-4242.out", "--base", str(base), "--job", "4242",
                           "--name", "water"]) == 0       # not nomad's: a folder of its own, beside
        assert h.cmd_dest(["--stdout", "", "--base", str(base), "--job", "77", "--name", "w x",
                           "--array-job", "70", "--task", "3"]) == 0
        (made / "slurm-4242.out").write_text("")
        assert h.cmd_dest(["--stdout", "", "--base", str(base), "--job", "4242",
                           "--name", "water"]) == 0       # scontrol said nothing: nomad's folder, with the .out
    assert buf.getvalue().split() == [str(made), str(made) + ".2", str(base / "70-w_x" / "task-3"), str(made)]
    del run


def _leftover(base, tmp_path, job, state="running", made=None):
    d = base / f"job_{job}"
    (d / h.STATE).mkdir(parents=True)
    dest = tmp_path / "res" / job
    dest.mkdir(parents=True, exist_ok=True)
    h.write_info(str(d), {"v": 1, "job": job, "dest": str(dest), "state": state, "node": "spdr05"})
    with open(d / h.STATE / h.MANIFEST, "w"):
        pass
    (d / "made.txt").write_text(made or job)
    return d, dest


def test_leftovers_go_back_unless_their_job_holds_them(tmp_path, copier):
    base = tmp_path / "ls" / "amy"
    _leftover(base, tmp_path, "11")
    _leftover(base, tmp_path, "12")
    _leftover(base, tmp_path, "13", state="copying in")
    _leftover(base, tmp_path, "4242")
    held = h.try_lock(h.lock_path(str(base), "job_12"))      # job 12 still runs (squeue may not list it)
    try:
        assert h.cmd_recover(["--base", str(base), "--self", "4242"]) == 0
    finally:
        os.close(held)
    back = list((tmp_path / "res" / "11").glob("nomad-recovered-spdr05-*"))
    assert len(back) == 1 and (back[0] / "made.txt").read_text() == "11" and not (base / "job_11").exists()
    assert (base / "job_12" / "made.txt").exists() and list((tmp_path / "res" / "12").iterdir()) == []
    assert not (base / "job_13").exists() and list((tmp_path / "res" / "13").iterdir()) == []
    assert (base / "job_4242").exists()                                                 # this job's own


def test_what_is_recovered_never_overwrites_results(tmp_path, copier):
    base = tmp_path / "ls" / "amy"
    d, dest = _leftover(base, tmp_path, "11", made="run1-partial")
    (dest / "made.txt").write_text("run2-FINAL")             # the job ran again elsewhere and finished
    (dest / "slurm-11.out").write_text("log")
    (d / "slurm-11.out").write_text("the job's own file of that name")
    assert h.cmd_recover(["--base", str(base), "--only", "job_11"]) == 0
    assert (dest / "made.txt").read_text() == "run2-FINAL" and (dest / "slurm-11.out").read_text() == "log"
    back = list(dest.glob("nomad-recovered-*"))[0]
    assert (back / "made.txt").read_text() == "run1-partial"


def test_a_folder_a_process_still_works_in_is_kept(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (local / "out.txt").write_text("r\n")
    assert h.cmd_back(["--local", str(local)]) == 0
    p = subprocess.Popen(["sleep", "30"], cwd=local)           # a job it started, working there
    try:
        assert h.cmd_clean(["--local", str(local)]) == 1 and local.exists()
    finally:
        p.kill()
        p.wait()
    assert h.read_info(str(local))["state"] == "running"     # what that job changes goes back later


def test_discard_takes_only_what_this_run_made(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    info = h.read_info(str(local))
    info["token"] = "earlier-run"
    h.write_info(str(local), info)
    assert h.cmd_discard(["--local", str(local), "--token", "this-run"]) == 0 and local.exists()
    info["token"] = "this-run"
    h.write_info(str(local), info)
    assert h.cmd_discard(["--local", str(local), "--token", "this-run"]) == 0 and not local.exists()


def test_reap_never_guesses_at_processes(tmp_path):
    # outside a Slurm job's cgroup, and with no scontrol listpids: nothing is touched
    p = subprocess.Popen(["sleep", "30"])
    try:
        assert h.cmd_reap(["--job", "4242"]) == 0
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


def test_time_left():
    import io
    from contextlib import redirect_stdout
    for text, want in (("9:30", 570), ("1:00:00", 3600), ("2-01:00:00", 176400), ("UNLIMITED", -1),
                       ("", -1), ("INVALID", -1)):
        buf = io.StringIO()
        with redirect_stdout(buf):
            h.cmd_seconds([text])
        assert int(buf.getvalue()) == want


def test_kill_stops_a_program_and_what_it_started(tmp_path):
    p = subprocess.Popen([BASH, "-c", "sleep 300 & sleep 300 & wait"])
    time.sleep(0.3)
    kids = h._children(p.pid)
    assert len(kids) == 2
    h.cmd_kill(["--pid", str(p.pid), "--signal", "15"])
    p.wait(timeout=10)
    time.sleep(0.2)
    assert all(not os.path.exists(f"/proc/{k}") or "Z" in open(f"/proc/{k}/status").read().split("State:")[1][:4]
               for k in kids)


def test_the_helper_runs_on_python_3_6():
    vermin = shutil.which("vermin")
    if not vermin:
        pytest.skip("no vermin here")
    r = subprocess.run([vermin, "-t=3.6-", "--no-tips", "--violations", h.__file__],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── the script nomad sends, run as a job would run it ───────────────────────

def _fake_slurm(tmp_path, stdout):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "scontrol").write_text("#!/bin/sh\n[ \"$1\" = show ] || exit 1\n"
                                f"echo 'JobId=4242 JobName=water'\necho '   StdOut={stdout}'\n"
                                f"echo '   StdErr={stdout}'\n"
                                "echo \"   RunTime=00:00:01 TimeLimit=${FAKE_LIMIT:-UNLIMITED} TimeMin=N/A\"\n")
    (b / "squeue").write_text("#!/bin/sh\nprintf '%s\\n' \"$FAKE_SQUEUE\"\n")
    (b / "sbatch").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/sbatch.args\necho 999\n")
    (b / "scancel").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/scancel.args\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _job(tmp_path, script, *, files=None, stage_kw=None, env=None, args=(), header=None, out_here=False):
    w = tmp_path / "w"
    w.mkdir(parents=True, exist_ok=True)
    _tree(str(w), files or {"in.txt": "input\n"})
    (w / "job.sh").write_text(script)
    dest = tmp_path / "scratch" / "amy" / "jobs" / "4242-water"
    dest.mkdir(parents=True, exist_ok=True)
    out = (w if out_here else dest) / "slurm-4242.out"
    kw = {"base": str(tmp_path / "ls" / "amy"), "root": str(tmp_path / "ls"),
          "results": str(tmp_path / "scratch" / "amy" / "jobs"), "name": "water", "folder": str(w),
          "reserve_min": 0, "reserve_pct": 0, "linger": 2, "grace": 3, "term_grace": 2}
    kw.update(stage_kw or {})
    st = stage.Stage(**kw)
    (tmp_path / "ls").mkdir(exist_ok=True)
    text = stage.wrapper_text(str(w / "job.sh"), script, st, "1.24.0")
    wrap = tmp_path / "nomad-wrap.sh"
    wrap.write_text(text)
    b = _fake_slurm(tmp_path, out)
    e = {"PATH": f"{b}:{os.environ['PATH']}", "SLURM_JOB_ID": "4242", "HOME": str(tmp_path),
         "TMPDIR": str(tmp_path)}
    e.update(env or {})
    e = {k: v for k, v in e.items() if v is not None}
    return w, dest, out, [BASH, str(wrap), *args], e


def _run(argv, env, cwd, timeout=60):
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)


SCRIPT = """#!/bin/bash
#SBATCH -J water
#SBATCH -c 2
echo "pwd=$PWD"
echo "submit=$SLURM_SUBMIT_DIR"
echo "orig=$NOMAD_SUBMIT_DIR"
printf '%s|' "$@"; echo
cat in.txt > out.txt
mkdir -p results && echo r > results/r.txt
exit 3
"""


def test_a_job_runs_in_local_scratch_and_its_results_come_back(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT, args=("a", "b c"))
    before = os.stat(w / "in.txt")
    r = _run(argv, env, w)
    local = tmp_path / "ls" / "amy" / "job_4242"
    assert r.returncode == 3, r.stderr                         # the script's own exit
    assert f"pwd={local}" in r.stdout and f"submit={local}" in r.stdout and f"orig={w}" in r.stdout
    assert "a|b c|" in r.stdout
    # back where it ran, as if it had run there; what it didn't change isn't written again
    assert (w / "out.txt").read_text() == "input\n" and (w / "results" / "r.txt").exists()
    assert os.stat(w / "in.txt").st_ino == before.st_ino and os.stat(w / "in.txt").st_mtime_ns == before.st_mtime_ns
    assert list(dest.iterdir()) == []                        # the job's folder: only Slurm's output
    assert not local.exists() and not (tmp_path / "ls" / "amy" / "job_4242.lock").exists()
    assert "copied back 3 new or changed item(s)" in r.stderr


def test_sbatch_reads_the_scripts_own_options_from_nomads(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT)
    text = open(argv[1]).read()
    head = text.split("\n_NS_BASE=")[0].splitlines()
    assert head[0] == "#!/bin/bash" and head[1] == stage.MARK
    assert "#SBATCH -J water" in head and "#SBATCH -c 2" in head and "#SBATCH --signal=B:USR1@600" in head
    assert all(line.startswith("#") or not line for line in head)   # nothing sbatch would stop at
    assert stage.unwrap(text) == (str(w / "job.sh"), SCRIPT)


@pytest.mark.parametrize("env, said", [
    ({"SLURM_JOB_NUM_NODES": "2"}, "the job has 2 nodes"),
    ({"SLURM_JOB_ID": None}, "not in a Slurm job"),
])
def test_runs_in_place_when_it_cant_stage(tmp_path, env, said):
    w, dest, out, argv, e = _job(tmp_path, SCRIPT, env=env)
    r = _run(argv, e, w)
    assert r.returncode == 3 and f"pwd={w}" in r.stdout and said in r.stderr
    assert (w / "out.txt").exists() and not (tmp_path / "ls" / "amy" / "job_4242").exists()


def test_no_local_scratch_on_the_node(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT, stage_kw={"root": "/nonexistent-ls",
                                                                "base": "/nonexistent-ls/amy"})
    r = _run(argv, env, w)
    assert r.returncode == 3 and "has no /nonexistent-ls" in r.stderr and f"pwd={w}" in r.stdout


def test_too_big_runs_in_place(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT, files={"in.txt": "x" * 4000}, stage_kw={"max_bytes": 100})
    r = _run(argv, env, w)
    assert r.returncode == 3 and "over the" in r.stderr and f"pwd={w}" in r.stdout


def test_without_a_path_or_with_the_jobs_own_shebang(tmp_path):
    script = "#!/usr/bin/env python3\nimport os, sys\nopen('out.txt', 'w').write(os.getcwd())\nsys.exit(0)\n"
    w, dest, out, argv, env = _job(tmp_path, script)
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "out.txt").read_text() == str(tmp_path / "ls" / "amy" / "job_4242")
    # --export=NONE: no PATH at all
    env2 = {k: v for k, v in env.items() if k != "PATH"}
    env2["PATH"] = str(tmp_path / "bin")                    # only the fake Slurm tools
    script = "#!/bin/bash\necho hi > out.txt\n"
    w, dest, out, argv, env = _job(tmp_path / "n", script)
    env["PATH"] = str(tmp_path / "n" / "bin")
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "out.txt").read_text() == "hi\n"


def test_a_copy_back_that_fails_fails_a_job_that_succeeded(tmp_path):
    # its output where Slurm puts it (in the folder), and the job writes a file of that name:
    # never overwritten, so kept on the node
    script = "#!/bin/bash\necho mine > slurm-4242.out\n"
    w, dest, out, argv, env = _job(tmp_path, script, out_here=True)
    out.write_text("slurm's\n")
    (dest / "slurm-4242.out").write_text("")                  # nomad's folder for the job (its .out was)
    r = _run(argv, env, w)
    assert r.returncode == 75, r.stderr
    assert out.read_text() == "slurm's\n"
    assert (tmp_path / "ls" / "amy" / "job_4242" / "slurm-4242.out").exists()
    assert "submitted job 999 to bring back" in r.stderr                  # a job pinned to the node
    assert "--dependency=afterany:4242" in (tmp_path / "sbatch.args").read_text()
    # that job, once this one has ended: what was left goes in a folder of its own, then it is removed
    rescue = tmp_path / "ls" / "amy" / "job_4242" / ".nomad-stage" / "recover.sh"
    r = subprocess.run([BASH, str(rescue)], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 0, r.stderr
    back = list(dest.glob("nomad-recovered-*"))
    assert len(back) == 1 and (back[0] / "slurm-4242.out").read_text() == "mine\n"
    assert out.read_text() == "slurm's\n" and not (tmp_path / "ls" / "amy" / "job_4242").exists()


LONG = """#!/bin/bash
echo start > progress.txt
trap 'echo stopped >> progress.txt; exit 1' TERM
for i in $(seq 1 600); do echo $i >> progress.txt; sleep 0.1; done
"""


def _start(argv, env, cwd):
    p = subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        if (cwd.parent / "ls" / "amy" / "job_4242" / "progress.txt").exists() or (cwd / "progress.txt").exists():
            break
        time.sleep(0.05)
    time.sleep(0.3)
    return p


@pytest.mark.parametrize("left, lingers", [("9:00", True), ("3:00:00", False)])
def test_usr1_stops_copies_back_and_waits_for_the_limit(tmp_path, left, lingers):
    w, dest, out, argv, env = _job(tmp_path, LONG, env={"FAKE_SQUEUE": left})
    p = _start(argv, env, w)
    t0 = time.time()
    p.send_signal(signal.SIGUSR1)
    o, e = p.communicate(timeout=60)
    assert "asked to stop (USR1)" in e and "copied back 1 new or changed item(s)" in e, e
    assert (w / "progress.txt").read_text().rstrip().endswith("stopped")
    assert ("waiting for the time limit" in e) == lingers
    assert (time.time() - t0 >= 2) == lingers                    # linger=2 s in the test
    assert not (tmp_path / "ls" / "amy" / "job_4242").exists()


def test_term_copies_back_at_once_and_sends_a_job_for_what_is_left(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, LONG)
    p = _start(argv, env, w)
    p.send_signal(signal.SIGTERM)
    o, e = p.communicate(timeout=30)
    assert "the job is being stopped" in e and (w / "progress.txt").exists(), e
    assert "waiting for the time limit" not in e
    sent = (tmp_path / "sbatch.args").read_text().split("\n")
    assert "-w" in sent and "--dependency=afterany:4242" in sent and sent[-2].endswith("/.nomad-stage/recover.sh")
    # all of it came back and checked out: the job sent for what might be left is cancelled
    assert (tmp_path / "scancel.args").read_text().split() == ["999"] and "submitted job" not in e


def test_a_short_jobs_warning_doesnt_stop_it(tmp_path):
    script = "#!/bin/bash\necho start > progress.txt\nsleep 2\necho done >> progress.txt\n"
    w, dest, out, argv, env = _job(tmp_path, script, env={"FAKE_LIMIT": "00:05:00", "FAKE_SQUEUE": "4:59"})
    p = _start(argv, env, w)
    p.send_signal(signal.SIGUSR1)                          # Slurm sends it at once to a 5-minute job
    o, e = p.communicate(timeout=30)
    assert p.returncode == 0, e
    assert "too short for nomad's copy back" in e
    assert (w / "progress.txt").read_text().split() == ["start", "done"]


def test_the_scripts_input_and_signals_are_its_own(tmp_path):
    script = ("#!/bin/bash\ncat > got.txt\ntrap 'echo int >> got.txt; exit 0' INT\n"
              "echo ready > progress.txt\nwhile :; do sleep 0.1; done\n")
    w, dest, out, argv, env = _job(tmp_path, script, stage_kw={"signal": False, "forward": "INT"})
    with open(tmp_path / "input.txt", "w") as f:
        f.write("the job's input\n")
    with open(tmp_path / "input.txt") as f:
        p = subprocess.Popen(argv, env=env, cwd=w, stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        deadline = time.time() + 20
        while time.time() < deadline and not (tmp_path / "ls" / "amy" / "job_4242" / "progress.txt").exists():
            time.sleep(0.05)
        time.sleep(0.3)
        p.send_signal(signal.SIGINT)
        o, e = p.communicate(timeout=30)
    assert p.returncode == 0, e
    assert (w / "got.txt").read_text() == "the job's input\nint\n"


def test_the_jobs_own_signal_reaches_it(tmp_path):
    script = ("#!/bin/bash\n#SBATCH --signal=B:USR2@300\ntrap 'echo got >> progress.txt; exit 0' USR2\n"
              "echo start > progress.txt\nwhile :; do sleep 0.1; done\n")
    w, dest, out, argv, env = _job(tmp_path, script, stage_kw={"signal": False, "forward": "USR2"})
    assert "#SBATCH --signal=B:USR1" not in open(argv[1]).read()
    p = _start(argv, env, w)
    p.send_signal(signal.SIGUSR2)
    o, e = p.communicate(timeout=30)
    assert p.returncode == 0, e
    assert (w / "progress.txt").read_text().split() == ["start", "got"]


def test_an_array_task_comes_back_to_its_folder_too(tmp_path):
    script = "#!/bin/bash\necho $SLURM_ARRAY_TASK_ID > out-$SLURM_ARRAY_TASK_ID.txt\n"
    w, dest, out, argv, env = _job(tmp_path, script, env={"SLURM_ARRAY_JOB_ID": "4240", "SLURM_ARRAY_TASK_ID": "2"})
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "out-2.txt").read_text() == "2\n"


def test_the_next_job_on_the_node_brings_back_what_was_left(tmp_path):
    base = tmp_path / "ls" / "amy"
    left, res = _leftover(base, tmp_path, "11", made="from job 11\n")
    w, dest, out, argv, env = _job(tmp_path, "#!/bin/bash\ntrue\n")
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    back = list(res.glob("nomad-recovered-*"))
    assert len(back) == 1 and (back[0] / "made.txt").read_text() == "from job 11\n" and not left.exists()
    assert not (base / "job_4242.lock").exists() and not (base / "job_4242").exists()


# ── what nomad submit decides ───────────────────────────────────────────────

def _logs(tmp_path):
    return logs.Logs(str(tmp_path / "scratch" / "{user}" / "jobs" / "{jobid}-{name}"), "default", "", delay=True)


def _decide(tmp_path, text="#!/bin/bash\ng16 < w.com\n", **kw):
    w = tmp_path / "w"
    w.mkdir(exist_ok=True)
    args = {"flag": None, "env": {}, "user": "amy", "script": str(w / "job.sh"), "text": text,
            "folder": str(w), "logs": _logs(tmp_path), "name": "water", "nodes": None, "options": []}
    args.update(kw)
    return stage.decide(**args)


def test_staged_by_default(tmp_path):
    st, note = _decide(tmp_path)
    assert st is not None and note is None
    assert st.base == "/localscratch/amy" and st.root == "/localscratch"
    assert st.results == str(tmp_path / "scratch" / "amy" / "jobs") and st.signal and st.warn == 600


def test_the_warning_fits_the_time_limit(tmp_path):
    assert _decide(tmp_path, limit_s=40 * 60)[0].warn == 400          # a sixth of 40 minutes
    assert _decide(tmp_path, limit_s=30 * 60)[0].warn == 300          # never under 5 minutes
    assert _decide(tmp_path, limit_s=48 * 3600)[0].warn == 600


@pytest.mark.parametrize("kw, said", [
    ({"flag": False}, None),
    ({"env": {"NOMAD_LOCALSCRATCH": "off"}}, None),
    ({"nodes": 2}, "it asks for 2 nodes"),
    ({"text": "#!/bin/bash\ncd /data/amy/run\ng16 < w.com\n"}, "leaves its folder itself (cd /data/amy/run)"),
    ({"text": "#!/bin/bash\ncd $WORK\n"}, "(cd $WORK)"),
    ({"text": "#!/bin/bash\ncd ..\n"}, "(cd ..)"),
    ({"text": "g16 < w.com\n"}, "doesn't start with #!"),
    ({"options": [("test-only", None)]}, "--test-only"),
    ({"env": {"NOMAD_LOCALSCRATCH": "relative/{user}"}}, "$NOMAD_LOCALSCRATCH: off, on, or an absolute"),
    ({"env": {"NOMAD_LOCALSCRATCH": "/tmp/fixed"}}, "with {user} in it"),
    ({"measure_": lambda f, x: (60 * 1024 ** 3, 10, True)}, "over the 50.0 GB nomad stages"),
    ({"measure_": lambda f, x: (6 * 1024 ** 3, 10, True), "array": True, "flag": True},
     "over the 5.0 GB nomad stages for each task"),
    ({"array": True}, "an array: its tasks would each copy the folder"),
    ({"limit_s": 600}, "its time limit (10 min) is under 30 minutes"),
    ({"text": "#!/bin/bash\nsbatch next.sh\n"}, "it sends jobs itself (sbatch)"),
    ({"text": "#!/bin/bash\njid=$(sbatch --parsable next.sh)\n"}, "it sends jobs itself (sbatch)"),
    ({"text": "#!/bin/bash\nif sbatch next.sh; then echo ok; fi\n"}, "it sends jobs itself (sbatch)"),
    ({"text": "#!/bin/bash\nls *.sh | xargs sbatch\n"}, "it sends jobs itself (sbatch)"),
    ({"text": "#!/bin/bash\nsnakemake --executor slurm -j 10\n"}, "it sends jobs itself (snakemake)"),
    ({"text": "#!/bin/bash\nnomad submit --yes next.sh\n"}, "it sends jobs itself (nomad submit)"),
    ({"text": "#!/bin/bash\ncd $SLURM_SUBMIT_DIR/..\n"}, "(cd $SLURM_SUBMIT_DIR/..)"),
    ({"text": "#!/bin/bash\ncd \"${SLURM_SUBMIT_DIR}/../inputs\"\n"}, "../inputs)"),
    ({"text": "#!/bin/bash\ncd run/../..\n"}, "(cd run/../..)"),
    ({"text": "#!/bin/bash\r\ntrue\r\n"}, "DOS line breaks"),
    ({"home": "HERE"}, "your home folder itself"),
])
def test_when_it_isnt(tmp_path, kw, said):
    if kw.get("home") == "HERE":
        kw = {**kw, "home": str(tmp_path / "w")}
    st, note = _decide(tmp_path, **kw)
    assert st is None
    assert (note is None) if said is None else (said in note), note


def test_a_cd_into_its_own_folder_still_stages(tmp_path):
    for text in ("#!/bin/bash\ncd $SLURM_SUBMIT_DIR\n", "#!/bin/bash\ncd \"${SLURM_SUBMIT_DIR}/run1\"\n",
                 "#!/bin/bash\ncd run1 && g16 < w.com\n"):
        st, note = _decide(tmp_path, text=text)
        assert st is not None, (text, note)


def test_on_overrides_off_and_another_folder(tmp_path):
    st, note = _decide(tmp_path, flag=True, env={"NOMAD_LOCALSCRATCH": "off"})
    assert st is not None and st.base == "/localscratch/amy"
    st, note = _decide(tmp_path, env={"NOMAD_LOCALSCRATCH": "/local/{user}/scratch"})
    assert st.base == "/local/amy/scratch" and st.root == "/local"


def test_the_jobs_folder_isnt_copied_into_itself(tmp_path):
    # submitted from /scratch/amy: its jobs/ (where results go) stays out
    st, note = stage.decide(flag=None, env={}, user="amy", script=str(tmp_path / "scratch" / "amy" / "j.sh"),
                            text="#!/bin/bash\ntrue\n", folder=str(tmp_path / "scratch" / "amy"),
                            logs=_logs(tmp_path), name="j", nodes=None, options=[])
    assert st.excludes == ["jobs"]


def test_the_jobs_own_signal(tmp_path):
    st, _ = _decide(tmp_path, options=[("signal", "B:USR2@300")])
    assert not st.signal and st.forward == "USR2"
    st, _ = _decide(tmp_path, options=[("signal", "SIGINT@60")])
    assert not st.signal and st.forward is None                 # to its steps: not the shell's
    st, _ = _decide(tmp_path, env={"SBATCH_SIGNAL": "B:12@120"})
    assert not st.signal and st.forward == "12"


def test_no_scratch_folder_still_stages(tmp_path, monkeypatch):
    # results go back where the job ran: a job's folder on /scratch is only where what is
    # recovered goes (else, a folder of its own where it ran)
    monkeypatch.setattr(logs, "DEFAULT_ROOT", str(tmp_path / "none" / "{user}"))
    st, note = _decide(tmp_path, logs=logs.Logs(None, "flag", "x", kind="here"))
    assert st is not None and st.results == ""


# ── nomad submit ────────────────────────────────────────────────────────────

def test_submit_sends_nomads_script(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    script = sent[-1][-1]
    assert script.startswith(stage.STORE) and script.endswith(".sh")
    assert stat.S_IMODE(os.stat(script).st_mode) == 0o600
    assert stage.unwrap(open(script).read())[0] == str(w / "job.sh")
    assert "--job-name=job.sh" not in sent[-1]                 # the script names itself (-J water)
    assert any(a.startswith("--extra=") and a.endswith(";stage=l") for a in sent[-1])
    assert "Through local scratch" in r.output and "comes back to" in r.output


def test_a_script_without_a_name_keeps_its_own(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\ng16 < w.com > w.log\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    assert "--job-name=job.sh" in sent[-1]


def test_dry_run_keeps_nothing(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--dry-run", "job.sh"])
    assert r.exit_code == 0 and "Through local scratch" in r.output
    assert not os.path.exists(stage.STORE)


def test_no_localscratch_sends_it_as_it_is(scratch, monkeypatch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--yes", "--no-localscratch", "job.sh"])
    assert r.exit_code == 0 and sent[-1][-1] == str(w / "job.sh") and "local scratch" not in r.output
    monkeypatch.setenv("NOMAD_LOCALSCRATCH", "off")
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and sent[-1][-1] == str(w / "job.sh") and "local scratch" not in r.output
    assert not any(a.endswith(";stage=l") for a in sent[-1])


def test_a_job_on_two_nodes_is_sent_as_it_is(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water -N 2\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\n"
                              "srun g16 < w.com > w.log\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    assert sent[-1][-1] == str(w / "job.sh") and "Not through local scratch: it asks for 2 nodes" in r.output


def test_a_copy_nomad_cant_keep_sends_it_as_it_is(scratch, monkeypatch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch

    def refuse(path, text):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(stage, "keep", refuse)
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and sent[-1][-1] == str(w / "job.sh") and "couldn't keep its copy" in r.output


def test_a_staged_job_sent_again_runs_its_own_script(scratch, monkeypatch):  # noqa: F811
    import nomad.submit.cli as sc
    from tests.test_release_1180 import SACCT, Fake
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    wrapper = sent[-1][-1]
    line = f"sbatch --parsable --begin=now+120 --extra=nomad=1.24.0;via=cli;stage=l {wrapper}"
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)).replace("sbatch job.sh", line, 1))
    monkeypatch.setattr(sc, "plan", lambda *a, **k: REAL_PLAN(*a, **{**k, "user": "amy", "run_sacct": f,
                                                                   "run_slurm": f}))
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "--again", "101"])
    assert r.exit_code == 0, r.output
    assert "ran through local scratch; its own script is" in r.output
    assert stage.unwrap(open(sent[-1][-1]).read())[0] == str(w / "job.sh")      # staged again, its own


def test_past_runs_sent_through_local_scratch_are_their_scripts(tmp_path):
    from nomad.submit import cli
    from nomad.submit.history import PastRun
    w = tmp_path / "w"
    w.mkdir()
    (w / "job.sh").write_text("#!/bin/bash\ng16 < w.com > w.log\n")
    st = stage.Stage(base="/localscratch/amy", root="/localscratch", results="/scratch/amy/jobs", name="w",
                     folder=str(w))
    text = stage.wrapper_text(str(w / "job.sh"), (w / "job.sh").read_text(), st, "1.24.0")
    wp = tmp_path / "wrap.sh"
    wp.write_text(text)
    run = PastRun.__new__(PastRun)
    for k, v in {"submit_line": f"sbatch --parsable {wp}", "workdir": str(w), "script": None, "family": None,
                 "executable": None}.items():
        setattr(run, k, v)
    cli.name_programs([run])
    assert run.script == str(w / "job.sh") and run.family == "Gaussian"


def test_the_helper_never_ends_the_here_document():
    assert stage.PY_END not in stage.helper_source().splitlines()
    assert json.dumps(stage.B64_END)


def test_the_script_nomad_sends_is_valid_bash(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT, stage_kw={"excludes": ["jobs", "a b"], "forward": "USR2"})
    r = subprocess.run([BASH, "-n", argv[1]], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_job_sent_from_a_helper_script_starts_where_the_job_came_from(tmp_path):
    # the sbatch is in another script, so nomad couldn't see it: its stand-in sends it from the folder
    files = {"in.txt": "x", "sub/.keep": "", "submit_next.sh": "#!/bin/bash\nsbatch --parsable -o \"$PWD/log.out\" next.sh\n"}
    w, dest, out, argv, env = _job(tmp_path, "#!/bin/bash\ncd sub && bash ../submit_next.sh\n", files=files)
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    sent = (tmp_path / "sbatch.args").read_text().split("\n")
    assert sent[:5] == ["--parsable", "-o", f"{tmp_path}/ls/amy/job_4242/sub/log.out", f"--chdir={w}/sub",
                        f"--output={w}/sub/log.out"] and sent[5] == "next.sh"


def test_a_job_sent_to_work_in_a_folder_only_in_the_copy_is_refused(tmp_path):
    files = {"in.txt": "x", "submit_next.sh": "#!/bin/bash\nsbatch --parsable next.sh || echo refused > ../why\n"}
    w, dest, out, argv, env = _job(tmp_path, "#!/bin/bash\nmkdir -p new && cd new && bash ../submit_next.sh\n",
                                   files=files)
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert "which isn't there yet" in r.stderr and not (tmp_path / "sbatch.args").exists()
    assert (w / "why").read_text() == "refused\n"


def test_the_scripts_locale_is_its_own(tmp_path):
    script = "#!/bin/bash\necho \"[${LC_CTYPE-unset}]\" > lc.txt\n"
    w, dest, out, argv, env = _job(tmp_path, script, env={"LC_ALL": None, "LANG": "C"})
    env.pop("LC_CTYPE", None)
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "lc.txt").read_text() == "[unset]\n"


def test_a_short_jobs_warning_before_it_starts_doesnt_stop_it(tmp_path):
    # the warning can come while the folder is copied in
    files = {f"f{i}": "x" * 1000 for i in range(400)}
    w, dest, out, argv, env = _job(tmp_path, "#!/bin/bash\necho ran > ran.txt\n", files=files,
                                   env={"FAKE_LIMIT": "00:05:00"})
    p = subprocess.Popen(argv, env=env, cwd=w, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.05)
    p.send_signal(signal.SIGUSR1)
    o, e = p.communicate(timeout=60)
    assert p.returncode == 0, e
    assert (w / "ran.txt").read_text() == "ran\n"


def test_no_lock_file_left_when_it_ran_in_place(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, SCRIPT, files={"in.txt": "x" * 4000}, stage_kw={"max_bytes": 100})
    r = _run(argv, env, w)
    assert r.returncode == 3 and "over the" in r.stderr
    assert list((tmp_path / "ls" / "amy").glob("*.lock")) == []


def test_slurms_output_deeper_in_the_folder_is_protected_too(tmp_path):
    # --output=logs/%j.out: the job's own copy of it must never replace Slurm's
    script = "#!/bin/bash\necho mine >> logs/slurm-4242.out\n"
    w, dest, out, argv, env = _job(tmp_path, script, files={"in.txt": "x", "logs/slurm-4242.out": "start\n"})
    b = tmp_path / "bin" / "scontrol"
    b.write_text(b.read_text().replace(str(out), str(w / "logs" / "slurm-4242.out")))
    (w / "logs" / "slurm-4242.out").write_text("slurm's log\n")
    r = _run(argv, env, w)
    assert r.returncode == 75, r.stderr
    assert (w / "logs" / "slurm-4242.out").read_text() == "slurm's log\n"


def test_two_copy_backs_into_one_folder_take_turns(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"summary.csv": "header\n"})
    with open(local / "summary.csv", "a") as f:
        f.write("row-1\n")
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(src / h.FOLDER_LOCK)!r}, os.O_RDWR | os.O_CREAT, 0o666)\n"
        "fcntl.lockf(fd, fcntl.LOCK_EX)\n"
        f"open({str(tmp_path / 'held')!r}, 'w').close()\n"
        "time.sleep(1.5)\n"
        f"open({str(src / 'summary.csv')!r}, 'a').write('row-2\\n')\n")])
    while not (tmp_path / "held").exists():
        time.sleep(0.02)
    t0 = time.time()
    assert h.cmd_back(["--local", str(local)]) == 0                 # waits for the other's copy back
    holder.wait(timeout=10)
    assert time.time() - t0 >= 1.0
    assert (src / "summary.csv").read_text() == "header\nrow-2\nrow-1\n"


def test_an_empty_file_both_wrote_is_a_conflict_not_a_merge(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"energy.txt": ""})
    (local / "energy.txt").write_text("-76.4\n")
    (src / "energy.txt").write_text("-76.3\n")
    assert h.cmd_back(["--local", str(local)]) == h.CONFLICTS
    assert (src / "energy.txt").read_text() == "-76.3\n"


def test_a_merge_that_cant_be_checked_says_what_it_wrote(tmp_path, monkeypatch, capsys):
    src, local, dest, rc = _stage_in(tmp_path, files={"run.log": "start\n"})
    with open(local / "run.log", "a") as f:
        f.write("job line\n")
    with open(src / "run.log", "a") as f:
        f.write("other line\n")
    monkeypatch.setattr(h, "_tail_sha", lambda path, start: "changed meanwhile")
    assert h.cmd_back(["--local", str(local)]) == h.CONFLICTS
    err = capsys.readouterr().err
    assert "were added at byte 17" in err and "couldn't be checked" in err and "left as it is" not in err


def test_only_a_touch_or_chmod_there_isnt_a_conflict(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"out.dat": "old\n"})
    (local / "out.dat").write_text("new result\n")
    os.utime(src / "out.dat", (1, 1))                                 # touch
    os.chmod(src / "out.dat", 0o640)                                  # chmod
    assert h.cmd_back(["--local", str(local)]) == 0
    assert (src / "out.dat").read_text() == "new result\n"
    assert stat.S_IMODE(os.stat(src / "out.dat").st_mode) == 0o640


def test_a_copy_back_leaves_no_temporary_files(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (local / "out.txt").write_text("r\n")
    assert h.cmd_back(["--local", str(local)]) == 0
    assert not [p for p in src.rglob("*") if ".nomad-tmp" in p.name or p.name.endswith(".tmp")]


def test_recovery_removes_a_cut_short_copy_backs_unfinished_files(tmp_path, copier):
    base = tmp_path / "ls" / "amy"
    d, dest = _leftover(base, tmp_path, "11")
    tmp = dest / ".nomad-tmp-11"
    tmp.mkdir()
    (tmp / "123-1").write_text("half a file")
    info = h.read_info(str(d))
    info["tmpdir"] = str(tmp)
    h.write_info(str(d), info)
    assert h.cmd_recover(["--base", str(base), "--only", "job_11"]) == 0
    assert not tmp.exists()


def test_slurms_output_is_protected_however_its_path_is_written(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"logs/slurm-1.out": "x"})
    (local / "logs" / "slurm-1.out").write_text("the job's own\n")
    assert h.cmd_back(["--local", str(local), "--protect-path", f"{src}/./logs/slurm-1.out"]) == 1


def test_an_array_task_runs_in_place_unless_asked(tmp_path):
    script = "#!/bin/bash\necho $PWD > where.txt\n"
    env = {"SLURM_ARRAY_JOB_ID": "4240", "SLURM_ARRAY_TASK_ID": "2"}
    w, dest, out, argv, e = _job(tmp_path, script, env=env, stage_kw={"array_ok": False})
    r = _run(argv, e, w)
    assert r.returncode == 0 and "an array task" in r.stderr and (w / "where.txt").read_text() == f"{w}\n"


def test_sbatchs_options_are_read_as_sbatch_reads_them():
    from nomad.submit import spec
    assert h.SBATCH_LONG_VALUE == spec._LONG_VALUE
    c = h._sbatch_chdir
    assert c(["--parsable", "next.sh"]) == (None, 1)
    assert c(["-D", "/x", "-J", "n", "next.sh", "-D", "y"]) == ("/x", 4)
    assert c(["--chd=run", "-pbasic", "--mem", "4G", "a.sh"]) == ("run", 4)
    assert c(["-Hcrun2", "--wrap=hostname"]) == (None, None)


def _shim(tmp_path, local, submit, child, cwd):
    b = tmp_path / "realbin"
    b.mkdir(exist_ok=True)
    (b / "sbatch").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/real.args\n")
    (b / "sbatch").chmod(0o755)
    shimdir = local / ".nomad-stage" / "bin"
    shimdir.mkdir(parents=True, exist_ok=True)
    (cwd / "child.sh").write_text(child)
    env = {"PATH": f"{shimdir}:{b}:/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-I", h.__file__, "sbatch-shim", str(shimdir), str(local), str(submit),
                        "--parsable", "child.sh"], cwd=cwd, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return (tmp_path / "real.args").read_text().split()


def test_the_sbatch_stand_in_moves_only_what_would_work_in_the_copy(tmp_path):
    local = tmp_path / "ls" / "amy" / "job_1"
    submit = tmp_path / "w"
    (local / "sweep").mkdir(parents=True)
    (submit / "sweep" / "run_3").mkdir(parents=True)
    got = _shim(tmp_path, local, submit, "#!/bin/bash\n#SBATCH --chdir=run_3\ntrue\n", local / "sweep")
    assert got == ["--parsable", f"--chdir={submit}/sweep/run_3", "child.sh"]
    got = _shim(tmp_path, local, submit, "#!/bin/bash\n#SBATCH -D /data/amy/elsewhere\ntrue\n", local)
    assert got == ["--parsable", "child.sh"]                         # its own folder, not the copy: as it is


def test_a_job_sent_from_the_copy_moves_or_keeps_it(tmp_path, monkeypatch, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    assert h.cmd_back(["--local", str(local)]) == 0
    b = tmp_path / "fakebin"
    b.mkdir()
    (b / "squeue").write_text(f"#!/bin/sh\necho \"77|$FAKE_STATE|{local}/sub\"\n")
    (b / "scontrol").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path}/scontrol.args\n")
    for f in b.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(h, "which", lambda name: None)
    monkeypatch.setenv("FAKE_STATE", "RUNNING")
    assert h.cmd_clean(["--local", str(local)]) == 1 and local.exists()       # it works there: kept
    assert h.read_info(str(local))["state"] == "running"                      # what it writes goes back later
    assert h.cmd_back(["--local", str(local)]) == 0
    monkeypatch.setenv("FAKE_STATE", "PENDING")
    assert h.cmd_clean(["--local", str(local)]) == 0 and not local.exists()   # moved where the job came from
    assert (tmp_path / "scontrol.args").read_text().split() == ["update", "jobid=77", f"WorkDir={src}/sub"]


def test_without_a_lock_shared_between_nodes_each_file_is_checked_before_it_goes_in(tmp_path, monkeypatch,
                                                                                    copier):
    src, local, dest, rc = _stage_in(tmp_path, files={"a.txt": "a\n", "b.txt": "b\n"})
    (local / "a.txt").write_text("job a\n")
    (local / "b.txt").write_text("job b\n")
    monkeypatch.setattr(h, "_lock_is_local", lambda folder: "local_lock=posix")
    real = h._copy_entry

    def meanwhile(src_root, dst_root, rel, kind, target, tmpdir=None, expect=None):
        if rel == "a.txt":                             # another node's job writes b.txt just then
            (src / "b.txt").write_text("other node's b\n")
        return real(src_root, dst_root, rel, kind, target, tmpdir, expect)
    monkeypatch.setattr(h, "_copy_entry", meanwhile)
    assert h.cmd_back(["--local", str(local)]) == h.CONFLICTS
    assert (src / "a.txt").read_text() == "job a\n" and (src / "b.txt").read_text() == "other node's b\n"
    aside = tmp_path / "scratch" / "amy" / "jobs" / "4242-water" / "nomad-conflicts-4242"
    assert (aside / "b.txt").read_text() == "job b\n"


def test_a_labs_folder_lock_is_for_all_its_members(tmp_path, copier):
    src, local, dest, rc = _stage_in(tmp_path)
    (local / "out.txt").write_text("r\n")
    old = os.umask(0o022)
    try:
        assert h.cmd_back(["--local", str(local)]) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(src / h.FOLDER_LOCK).st_mode) == 0o666
