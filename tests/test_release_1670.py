# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.67.0: jobs on lab machines. nomad-gate starts a person's job script on
a lab machine with no scheduler (serve --runs), held to cores of its own, at
the lowest priority, stopped past its time or its memory; nomad's jobs there
together hold at most a share of the machine; the hub's settings and record."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nomad.submit import gate, machine_run as M, machine_runs as R, remote

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ("#!/bin/bash\necho \"nice $(nice) oom $(cat /proc/self/oom_score_adj)\"\n"
          "grep Cpus_allowed_list /proc/self/status\necho \"cpus $NOMAD_CPUS omp $OMP_NUM_THREADS gpus=[$CUDA_VISIBLE_DEVICES]\"\n"
          "sleep 30\necho done\n")


def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ── what the gate takes ─────────────────────────────────────────────────────

def test_the_gate_takes_the_new_requests_and_checks_them():
    assert gate.parse(["start", "--test", "--cpus", "1", "--mem", "64M", "--time", "5:00"])[0] == "start"
    sha = "a" * 64
    gate.parse(["start", "--script", "/h/j.sh", "--sha256", sha, "--cpus", "4", "--mem", "8G", "--time", "4:00:00",
                "--share", "50"])
    for words, said in ((["start", "--script", "/h/j.sh", "--sha256", sha, "--test", "--cpus", "1", "--mem", "1G",
                          "--time", "5"], "takes no script"),
                        (["start", "--cpus", "1", "--mem", "1G", "--time", "5"], "--script and --sha256, or --test"),
                        (["start", "--test", "--mem", "1G", "--time", "5"], "needs --cpus"),
                        (["machine", "--share", "95"], "10 to 90"),
                        (["run", "--run", "x1"], "a job's id on a machine"),
                        (["stop"], "needs --run")):
        with pytest.raises(gate.Refused, match=said):
            gate.parse(words)
    assert remote.request("start", "jdoe", test=True, cpus=1, mem="64M", time="5:00", share=50)[:3] == \
        ["start", "--user", "jdoe"]


def test_only_a_machine_that_says_so_starts_jobs(monkeypatch):
    monkeypatch.setattr(gate, "person", lambda name: type("P", (), {"pw_name": "jdoe"})())
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    monkeypatch.setattr(gate, "_log", lambda m: None)
    calls, out = [], []

    def run_(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok": true, "run": "r0123456789", "cores": [7]}\n', "")
    monkeypatch.setattr(gate, "answer", lambda doc, code=0: out.append(doc) or code)
    env = {"SSH_ORIGINAL_COMMAND": "nomad-gate start --user jdoe --test --cpus 1 --mem 64M --time 5:00"}
    slot = lambda: os.open(os.devnull, os.O_RDONLY)                          # noqa: E731
    gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=slot, check_=lambda me: None, edit=True)
    assert calls == [] and "--runs" in out[-1]["error"]
    gate.serve("/g/nomad-gate", env=env, run_=run_, slot_=slot, check_=lambda me: None, runs=True)
    assert calls and calls[-1][-8:] == ["start", "--test", "--cpus", "1", "--mem", "64M", "--time", "5:00"]
    for runs in (False, True):
        gate.serve("/g/nomad-gate", env={"SSH_ORIGINAL_COMMAND": "nomad-gate version"}, run_=run_, slot_=slot,
                   check_=lambda me: None, runs=runs)
        assert out[-1]["runs"] is runs
    seen = []
    monkeypatch.setattr(gate, "serve", lambda me, **kw: seen.append(kw) or 0)
    assert gate.main(["serve", "--edit", "--runs"], me="/g/nomad-gate") == 0 and seen[-1] == {"edit": True, "runs": True}
    assert gate.main(["serve", "--runs", "--edit"], me="/g/nomad-gate") == 2
    assert remote.forced_command("/opt/nomad-gate/bin/nomad-gate", edit=True, runs=True).endswith("serve --edit --runs")


def test_slurm_takes_the_jobs_where_it_is(monkeypatch):
    monkeypatch.setattr(M, "slurm_here", lambda: True)
    with pytest.raises(M.RunError, match="has Slurm"):
        M.start(None, None, 1, "64M", "5:00", test=True)


# ── the machine and its share ───────────────────────────────────────────────

def test_memory_and_time_as_slurm_says_them():
    assert M.memory_of("8G") == 8 * 1024 ** 3 and M.memory_of("500M") == 500 * 1024 ** 2
    assert M.memory_of("2048") == 2048 * 1024 ** 2 and M.memory_of("1.5g") == int(1.5 * 1024 ** 3)
    assert [M.seconds_of(x) for x in ("90", "5:30", "4:00:00", "2-3", "1-02:30", "1-00:00:00")] == \
        [5400, 330, 14400, 2 * 86400 + 3 * 3600, 86400 + 2 * 3600 + 30 * 60, 86400]
    with pytest.raises(M.RunError):
        M.memory_of("lots")


def _proc(tmp_path, held: list, gpus: list | None = None) -> str:
    proc = tmp_path / "proc"
    for i, (cores, mem) in enumerate(held, start=100):
        (proc / str(i)).mkdir(parents=True)
        args = ["/x/python", "-I", "-B", "-m", M.MODULE, "supervise", "--dir", f"/h/r{i:010x}",
                "--cores", ",".join(map(str, cores)), "--mem", str(mem)]
        if gpus and gpus[i - 100]:
            args += ["--gpus", ",".join(map(str, gpus[i - 100]))]
        (proc / str(i) / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    (proc / "7").mkdir(parents=True)
    (proc / "7" / "cmdline").write_bytes(b"bash\0-l\0")
    return str(proc)


@pytest.fixture
def no_gpus(monkeypatch):
    monkeypatch.setattr(M, "gpus_here", lambda: [])
    monkeypatch.setattr(M, "gpus_busy", lambda: set())


def test_nomads_jobs_hold_at_most_their_share(tmp_path, monkeypatch, no_gpus):
    monkeypatch.setattr(M, "cores_here", lambda: list(range(16)))
    monkeypatch.setattr(M, "memory_here", lambda: 64 * 1024 ** 3)
    monkeypatch.setattr(M, "slurm_here", lambda: False)
    proc = _proc(tmp_path, [([15, 14, 13, 12], 8 * 1024 ** 3), ([11, 10], 4 * 1024 ** 3)])
    f = M.facts(50, proc)
    assert (f["may_cpus"], f["held_cpus"], f["free_cpus"]) == (8, 6, 2)
    assert f["may_mem"] == 32 * 1024 ** 3 and f["free_mem"] == 20 * 1024 ** 3 and f["jobs_here"] == 2
    with pytest.raises(M.RunError, match="busy: nomad's jobs hold 6 of the 8 cores"):
        M.start(None, None, 4, "1G", "5:00", share=50, test=True, home=str(tmp_path), proc=proc)
    with pytest.raises(M.RunError, match="at most 8 cores"):
        M.start(None, None, 9, "1G", "5:00", share=50, test=True, home=str(tmp_path), proc=proc)
    with pytest.raises(M.RunError, match="at most 32.0 GB"):
        M.start(None, None, 1, "40G", "5:00", share=50, test=True, home=str(tmp_path), proc=proc)
    assert not (tmp_path / ".local").exists()                          # refused: nothing written
    got = []

    class P:                                                           # a supervisor that never runs
        def __init__(self, argv, **kw):
            got.append(argv)

        def poll(self):
            return None
    monkeypatch.setattr(M, "STARTED_WAIT", 0.2)
    a = M.start(None, None, 2, "1G", "5:00", share=50, test=True, home=str(tmp_path), proc=proc, spawn=P)
    assert a["cores"] == [8, 9] and a["state"] == "starting"           # the highest the others don't hold
    assert got[0][got[0].index("--cores") + 1] == "8,9" and got[0][3:6] == ["-m", M.MODULE, "supervise"]


def test_gpus_are_given_whole_and_only_free_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "cores_here", lambda: list(range(16)))
    monkeypatch.setattr(M, "memory_here", lambda: 64 * 1024 ** 3)
    monkeypatch.setattr(M, "slurm_here", lambda: False)
    monkeypatch.setattr(M, "STARTED_WAIT", 0.2)
    gpus = [{"index": i, "uuid": f"GPU-000{i}", "name": "NVIDIA RTX A6000", "mem": 48 * 1024 ** 3} for i in range(3)]
    monkeypatch.setattr(M, "gpus_here", lambda: gpus)
    monkeypatch.setattr(M, "gpus_busy", lambda: {"GPU-0002"})          # someone computes on the third now
    proc = _proc(tmp_path, [([15], 1024 ** 3)], gpus=[[0]])            # one of nomad's jobs holds the first
    f = M.facts(50, proc)
    assert (f["gpus"], f["held_gpus"], f["busy_gpus"], f["free_gpus"]) == (3, 1, 1, 1)
    assert f["gpu_names"] == ["NVIDIA RTX A6000"]
    with pytest.raises(M.RunError, match="not 2 GPUs free on .*nomad's jobs hold 1 and other programs compute on 1"):
        M.start(None, None, 1, "1G", "5:00", test=True, home=str(tmp_path), proc=proc, gpus=2)
    with pytest.raises(M.RunError, match="has 3 GPUs: fewer than 4"):
        M.start(None, None, 1, "1G", "5:00", test=True, home=str(tmp_path), proc=proc, gpus=4)
    got = []

    class P:
        def __init__(self, argv, **kw):
            got.append(argv)

        def poll(self):
            return None
    a = M.start(None, None, 1, "1G", "5:00", test=True, home=str(tmp_path), proc=proc, spawn=P, gpus=1)
    assert a["gpus"] == 1 and a["gpu_ids"] == [1] and got[0][got[0].index("--gpus") + 1] == "1"
    assert M._read_state(os.path.join(M.runs_dir(str(tmp_path)), a["run"]))["gpu_uuids"] == ["GPU-0001"]
    a = M.start(None, None, 1, "1G", "5:00", test=True, home=str(tmp_path), proc=proc, spawn=P)
    assert "--gpus" not in got[-1] and a["gpu_ids"] == []                  # none asked for: none given
    monkeypatch.setattr(M, "gpus_here", lambda: [])
    with pytest.raises(M.RunError, match="has no GPU nomad can lend"):
        M.start(None, None, 1, "1G", "5:00", test=True, home=str(tmp_path), proc=proc, gpus=1)


def test_the_gpus_as_nvidia_smi_says_them(monkeypatch):
    monkeypatch.setattr(M.shutil, "which", lambda name, path=None: "/usr/bin/nvidia-smi")

    def run(argv, **kw):
        if "--query-gpu=index,uuid,name,memory.total" in argv:
            out = "0, GPU-aaaa, NVIDIA T1000 8GB, 8192\n1, GPU-bbbb, NVIDIA T1000 8GB, 8192\nNo devices were found\n"
        else:
            out = "GPU-bbbb, 4242\n"
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(M.subprocess, "run", run)
    assert [(g["index"], g["uuid"], g["mem"]) for g in M.gpus_here()] == [(0, "GPU-aaaa", 8 * 1024 ** 3),
                                                                        (1, "GPU-bbbb", 8 * 1024 ** 3)]
    assert M.gpus_busy() == {"GPU-bbbb"}
    monkeypatch.setattr(M.shutil, "which", lambda name, path=None: None)
    assert M.gpus_here() == [] and M.gpus_busy() == set()


# ── a real job, as this account ─────────────────────────────────────────────

def _spawn(argv, **kw):
    """The supervisor as nomad-gate starts it, but from this source tree (the
    tests' Python isn't the gate's isolated one)."""
    env = {**kw.pop("env"), "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-B"] + argv[3:], env=env, **kw)


def _wait(fn, secs=20):
    end = time.time() + secs
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.2)
    return fn()


@pytest.fixture
def machine(tmp_path, monkeypatch, no_gpus):
    monkeypatch.setattr(M, "slurm_here", lambda: False)
    monkeypatch.setattr(M, "_home", lambda: str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    job = work / "job.sh"
    job.write_text(SCRIPT)
    job.chmod(0o700)
    return tmp_path, job


def test_a_job_runs_held_to_its_cores_and_is_stopped(machine, monkeypatch):
    home, job = machine
    data = job.read_bytes()
    monkeypatch.setattr(gate, "_uid", lambda: os.getuid())
    a = M.start(str(job), _h(data), 1, "256M", "10:00", share=90, home=str(home), spawn=_spawn)
    run = a["run"]
    assert a["state"] == "running" and a["output"] == str(job.parent / f"nomad-run-{run}.out")
    out = Path(a["output"])
    said = _wait(lambda: "cpus" in out.read_text() and out.read_text())
    core = a["cores"][0]
    assert "nice 19" in said and "oom 1000" in said and f"Cpus_allowed_list:\t{core}\n" in said
    assert "cpus 1 omp 1 gpus=[]" in said
    listed = M.runs(str(home))
    assert [r["run"] for r in listed] == [run] and listed[0]["state"] == "running"
    assert any(h["dir"] and h["dir"].endswith(run) for h in M.held())       # counted by the next start
    with pytest.raises(M.RunError, match="not a job's id"):
        M.one("../x", str(home))
    s = M.stop(run, str(home), wait=10)
    assert s["state"] == "ended" and s["why"] == "stopped" and s["why_words"] == "it was stopped"
    one = M.one(run, str(home))
    assert "done" not in one["tail"] and one["exit"] != 0 and one["peak_mem"] > 0
    assert _wait(lambda: not any(h["dir"] and h["dir"].endswith(run) for h in M.held()), 10)
    with pytest.raises(M.RunError, match="changed since you looked"):
        M.start(str(job), "b" * 64, 1, "256M", "10:00", home=str(home), spawn=_spawn)


def _supervised(home: Path, job: Path, limit: int, mem: int, monkeypatch, **more) -> dict:
    """The supervisor in this process (its time and memory checked every second)."""
    monkeypatch.setattr(M, "POLL", 1)
    monkeypatch.setattr(M, "GRACE", 2)
    d = home / ".local/share/nomad/runs" / M.machine() / "r00000000aa"
    d.mkdir(parents=True)
    (d / "script").write_bytes(job.read_bytes())
    (d / "script").chmod(0o700)
    out = job.parent / "nomad-run-r00000000aa.out"
    out.touch()
    M._write_state(str(d), {"run": "r00000000aa", "folder": str(job.parent), "output": str(out), "limit": limit,
                            "state": "starting", "started": M._now(), **more})
    import signal
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    began = time.time()
    try:
        assert M.supervise(str(d), [0], mem) == 0
    finally:
        for s, h in before.items():
            signal.signal(s, h)
    return {**M._read_state(str(d)), "took": time.time() - began}


def test_past_its_time_it_is_stopped(machine, monkeypatch):
    home, job = machine
    doc = _supervised(home, job, 2, 1024 ** 3, monkeypatch, gpu_uuids=["GPU-0001"])
    assert doc["why"] == "time" and doc["took"] < 15 and doc["state"] == "ended"
    said = (job.parent / "nomad-run-r00000000aa.out").read_text()
    assert "gpus=[GPU-0001]" in said                                      # the GPU it was given, and that alone


def test_past_its_memory_it_is_stopped(machine, monkeypatch):
    home, job = machine
    doc = _supervised(home, job, 600, 1024, monkeypatch)                     # 1 KB: bash alone is more
    assert doc["why"] == "memory" and doc["took"] < 15 and doc["peak_mem"] > 1024


def test_a_record_whose_supervisor_went_is_lost(machine):
    home, _ = machine
    d = Path(M.runs_dir(str(home))) / "r0000000bad"
    d.mkdir(parents=True)
    M._write_state(str(d), {"run": "r0000000bad", "state": "running", "pid": 999999999, "started": M._now()})
    r = M.runs(str(home))[0]
    assert r["state"] == "ended" and r["why"] == "lost" and "restarted" in r["why_words"]


def test_the_supervisor_runs_only_for_a_jobs_own_folder(tmp_path):
    assert M.main(["supervise", "--dir", str(tmp_path), "--cores", "0", "--mem", "1"]) == 2
    assert M.main(["other"]) == 2


# ── the hub ─────────────────────────────────────────────────────────────────

def test_the_hubs_settings():
    s = R.settings({})
    assert (s.open, s.share, s.most_hours) == ("off", 50, 72) and not R.opens_to(s, "admin")
    s = R.settings({"console": {"machine_runs": {"open": "staff", "share": 40, "most_hours": 24}}})
    assert R.opens_to(s, "admin") and R.opens_to(s, "operator") and not R.opens_to(s, "viewer", member=True)
    assert R.within(s, "23:00:00") is None and "at most 24 hours" in R.within(s, "1-01:00:00")
    s = R.settings({"console": {"machine_runs": {"open": "labs"}}})
    assert R.opens_to(s, "viewer", member=True) and not R.opens_to(s, "viewer")
    bad = R.settings({"console": {"machine_runs": {"open": "all", "share": 95, "most": 1}}})
    assert bad.open == "off" and bad.share == 50 and len(bad.problems) == 3


def test_the_consoles_record(tmp_path):
    st = R.Store(str(tmp_path / "machine_runs.db"))
    st.add("jdoe", "chem-ws03", "r0123456789", "/h/jdoe/job.sh", 4)
    st.add("jdoe", "chem-ws03", "r0123456789", "/h/jdoe/job.sh", 4)           # once
    st.add("ann", "chem-ws04", "r9876543210", None, 1)
    assert [r["run"] for r in st.mine("jdoe")] == ["r0123456789"]
    st.ended("jdoe", "chem-ws03", "r0123456789")
    assert st.mine("jdoe")[0]["ended"] and (os.stat(st.path).st_mode & 0o777) == 0o600


def test_the_gate_test_starts_and_stops_a_test_job():
    from nomad.submit import gate_cli
    asked = []

    class Remote:
        @staticmethod
        def call(s, op, user=None, hub=None, **o):
            asked.append(op)
            return {"machine": {"ok": True, "cpus": 16, "mem": 64 * 1024 ** 3, "may_cpus": 8, "held_cpus": 0,
                                "share": 50},
                    "start": {"ok": True, "run": "r0123456789", "cores": [15]},
                    "runs": {"ok": True, "runs": [{"run": "r0123456789", "state": "running"}]},
                    "stop": {"ok": True, "state": "ended"}}[op]
    assert gate_cli.run_check(Remote, {}, {}, "jdoe", 50, pause=0) == {"ok": True}
    assert asked == ["machine", "start", "runs", "stop"]


def test_install_key_runs_on_a_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "forced_command", lambda p, owners=None, edit=False, runs=False:
                        f"{p} serve" + (" --edit" if edit else "") + (" --runs" if runs else ""))
    import base64
    blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
    key = f"ssh-ed25519 {blob} nomad-gate@hub"
    ak = tmp_path / "authorized_keys"
    _, lines = remote.install_key(key, origins="172.40.0.5", gate_path="/opt/nomad-gate/bin/nomad-gate",
                                  path=ak, edit=True, runs=True, apply=True)
    assert 'command="/opt/nomad-gate/bin/nomad-gate serve --edit --runs"' in ak.read_text()
    assert any("serve --edit --runs" in ln for ln in lines)
