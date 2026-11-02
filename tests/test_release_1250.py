# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.25.0: nomad-gate, the one way the hub acts on a cluster as a person
(the Console's Submit page): a key that can run nothing but `nomad-gate
serve`, a short list of requests checked twice, sudo to the person, and
nomad run in their login environment."""
import hashlib
import json
import os
import pwd
import shlex
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from nomad.submit import gate, remote
from tests.test_release_1180 import cluster_here  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSON = 5123
H = "a" * 64                                   # a sha256 (1.25.1: what sends must carry)
PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOKcK0YnNS1eRrXKk0wYdYq7lOq1Bv6c5mH8tqcR1Hx1 "
       "nomad-gate@hub")


def _out(capsys):
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 1, lines                     # one JSON document, one line
    return json.loads(lines[0])


# ── what the gate takes ─────────────────────────────────────────────────────

@pytest.mark.parametrize("words", [
    ["version"],
    ["version", "--user", "jdoe"],
    ["size", "--script", "/scratch/jdoe/a b/job.sh"],
    ["size", "--job", "4815162"],
    ["submit", "--script", "/s/j.sh", "--sha256", H, "--cpus", "8", "--mem", "1.5T", "--time", "1-12:00:00",
     "--gpus", "0",
     "--partition", "gpu_a100", "--keep", "--no-localscratch"],
    ["again", "--job", "4242_7", "--sha256", H, "--mem", "12G"],
    ["jobs", "--user", "jdoe"],
    ["cancel", "--job", "4242"],
    ["ls", "--dir", "/scratch/jdoe"],
    ["show", "--script", "/home/jdoe/x.sh"],
])
def test_requests_the_gate_takes(words):
    op, got = gate.parse(words)
    assert op == words[0] and gate.words_of(op, got) == [w for i, w in enumerate(words)
                                                          if not (w == "--user" or (i and words[i - 1] == "--user"))]


@pytest.mark.parametrize("words, why", [
    ([], "not something the gate does"),
    (["bash", "-c", "id"], "not something the gate does"),
    (["submit"], "submit needs --script"),
    (["submit", "--script", "job.sh"], "not a path"),
    (["submit", "--script", "/x/j.sh", "--mem", "8G;id"], "not a mem"),
    (["submit", "--script", "/x/j.sh", "--time", "soon"], "not a time"),
    (["submit", "--script", "/x/j.sh", "--partition", "-x"], "not a partition"),
    (["submit", "--script", "/x/j.sh", "--sha256", H, "--cpus", "0"], "--cpus: 1 to 4096"),
    (["submit", "--script", "/x/j.sh", "--sha256", H, "--gpus", "99"], "at most 64"),
    (["submit", "--script", "/x/j.sh", "--dependency", "afterok:1"], "doesn't take --dependency"),
    (["submit", "--script", "/x/j.sh", "--script", "/y.sh"], "given twice"),
    (["submit", "--script", "/x/j.sh", "--sha256", H, "--localscratch", "--no-localscratch"], "not both"),
    (["submit", "--script", "/x/\nj.sh"], "not a path"),
    (["cancel", "--job", "4242;rm"], "not a job"),
    (["cancel", "--job"], "without its value"),
    (["size"], "--script or --job"),
    (["size", "--script", "/a", "--job", "1"], "--script or --job"),
    (["jobs", "--user", "Robert'); DROP"], "not a user name"),
    (["ls", "--dir", "/x", "--keep"], "doesn't take --keep"),
    (["ls", "--dir", "/home/x/../y"], "without . or .."),
    (["show", "--script", "/home/x/./j.sh"], "without . or .."),
    (["show", "--script", "/home//j.sh"], "without . or .."),
    (["show", "--script", "/home/\u202ej.sh"], "not a path"),
    (["show", "--script", "/home/a\xadb.sh"], "not a path"),
    (["show", "--script", "/home/a\U000e0041b.sh"], "not a path"),
])
def test_requests_it_refuses(words, why):
    with pytest.raises(gate.Refused, match=__import__("re").escape(why)):
        gate.parse(words)


# ── what it runs from ───────────────────────────────────────────────────────

def test_an_install_only_its_owner_can_change(tmp_path):
    me = {0, os.getuid()}
    d = tmp_path / "sw" / "nomad" / "bin"
    d.mkdir(parents=True)
    for p in (tmp_path, tmp_path / "sw", tmp_path / "sw" / "nomad", d):
        os.chmod(p, 0o755)
    g = d / "nomad-gate"
    g.write_text("#!/bin/sh\n")
    os.chmod(g, 0o755)
    up = os.path.dirname(str(tmp_path))
    if gate.unsafe(up, me):
        pytest.skip(f"this machine's {up} isn't safe itself")
    assert gate.unsafe(str(g), me) is None
    os.chmod(tmp_path / "sw", 0o775)                  # group-writable folder above it
    assert "can be changed by others (mode 775)" in gate.unsafe(str(g), me)
    os.chmod(tmp_path / "sw", 0o1777)                 # sticky, as /tmp: others can't replace what's ours
    assert gate.unsafe(str(g), me) is None
    os.chmod(tmp_path / "sw", 0o755)
    os.chmod(g, 0o757)
    assert "mode 757" in gate.unsafe(str(g), me)
    os.chmod(g, 0o755)
    if os.getuid() == 0:
        os.chown(g, PERSON, -1)
        assert f"belongs to uid {PERSON}" in gate.unsafe(str(g), me)
        os.chown(g, 0, -1)
    # a link is followed: where it leads must be safe too
    bad = tmp_path / "open"
    bad.mkdir()
    os.chmod(bad, 0o777)
    (bad / "python").write_text("")
    (d / "python").symlink_to(bad / "python")
    assert "open can be changed by others" in gate.unsafe(str(d / "python"), me)
    assert "python leads to" in gate.tree_unsafe(str(tmp_path / "sw"), me)       # followed in the tree too
    (d / "python").unlink()
    assert gate.tree_unsafe(str(tmp_path / "sw"), me) is None
    # through a link to a folder: checked where it really is too
    real = tmp_path / "real" / "c"
    real.mkdir(parents=True)
    os.chmod(tmp_path / "real", 0o755)
    os.chmod(real, 0o777)
    (real / "y").write_text("")
    (d / "via").symlink_to(real)
    assert "c can be changed by others" in gate.unsafe(str(d / "via" / "y"), me)
    (d / "via").unlink()
    os.chmod(d / "nomad-gate", 0o775)
    assert "nomad-gate can be changed by others" in gate.tree_unsafe(str(tmp_path / "sw"), me)


# ── serve: as the cluster's account ─────────────────────────────────────────

@pytest.fixture
def served(monkeypatch, tmp_path):
    """serve() with a safe install, a person jdoe (uid 5123), and sudo recorded."""
    monkeypatch.setattr(gate, "self_check", lambda me: None)
    monkeypatch.setattr(gate, "_sudo", lambda: "/usr/bin/sudo")
    monkeypatch.setattr(gate, "_log", lambda msg: logged.append(msg))
    real = pwd.getpwnam
    people = {"jdoe": pwd.struct_passwd(("jdoe", "x", PERSON, PERSON, "", str(tmp_path), "/bin/bash")),
              "svc": pwd.struct_passwd(("svc", "x", 990, 990, "", "/", "/sbin/nologin"))}

    def getpwnam(name):
        return people[name] if name in people else real(name)
    monkeypatch.setattr(gate.pwd, "getpwnam", getpwnam)
    logged, sent = [], []
    answers = {"out": '{"ok": true, "jobs": []}\n', "err": "", "code": 0, "pre": 0, "slot": True}

    def run_(argv, **kw):
        if argv[2] == "-l":                          # sudo -n -l -u PERSON ...: may it?
            return subprocess.CompletedProcess(argv, answers["pre"], "", "")
        sent.append((argv, kw))
        return subprocess.CompletedProcess(argv, answers["code"], answers["out"], answers["err"])

    def slot():
        return os.open(os.devnull, os.O_RDONLY) if answers["slot"] else None

    def call(command):
        return gate.serve("/sw/nomad/bin/nomad-gate", env={"SSH_ORIGINAL_COMMAND": command, "LANG": "pt_BR"},
                          run_=run_, slot_=slot, check_=lambda me: gate.self_check(me))
    return call, sent, answers, logged


def test_serve_asks_sudo_for_the_person_and_passes_the_answer_on(served, capsys):
    call, sent, answers, logged = served
    assert call("nomad-gate jobs --user jdoe") == 0
    assert _out(capsys) == {"ok": True, "jobs": []}
    (argv, kw), = sent
    assert argv == ["/usr/bin/sudo", "-n", "-u", "jdoe", "/sw/nomad/bin/nomad-gate", "run", "jobs"]
    assert kw["env"] == {"PATH": gate.TOOL_PATH, "LANG": "C", "LC_ALL": "C"} and kw["stdin"] == subprocess.DEVNULL
    assert logged == ["user=jdoe jobs: ok"]
    # a path with spaces and quotes stays one word, never a shell's
    answers["out"] = 'motd\n{"ok": true, "job": "4242", "output": null}\n'
    path = "/scratch/jdoe/my run/it's $(id).sh"
    assert call(shlex.join([gate.GATE, "submit", "--user", "jdoe", "--script", path, "--sha256", H,
                            "--mem", "8G"])) == 0
    assert sent[-1][0][-7:] == ["submit", "--script", path, "--sha256", H, "--mem", "8G"]
    assert _out(capsys)["job"] == "4242" and logged[-1].endswith(": ok -> job 4242")
    # the gate's full path, as the hub may send it
    assert call("/usr/local/sw/nomad/bin/nomad-gate cancel --job 7 --user jdoe") == 0
    assert sent[-1][0][-3:] == ["cancel", "--job", "7"]
    capsys.readouterr()
    # what the person's side says is logged only as a job number, and as plain text
    answers["out"] = json.dumps({"ok": True, "job": "1\nuser=root submit: ok -> job 7"}) + "\n"
    assert call(f"nomad-gate again --job 7 --sha256 {H} --user jdoe") == 0
    assert logged[-1] == f"user=jdoe again --job 7 --sha256 {H}: ok"
    capsys.readouterr()
    assert gate.clean("a\nb \udcff \u202e") == "a\\nb \\udcff \\u202e"


@pytest.mark.parametrize("command, why", [
    ("nomad-gate jobs", "--user: not a user name"),
    ("nomad-gate jobs --user root", "not for root"),
    ("nomad-gate jobs --user installer", "not for installer"),
    ("nomad-gate jobs --user svc", "not for a system account"),
    ("nomad-gate jobs --user nosuchperson9", "no account nosuchperson9"),
    ("nomad-gate jobs --user jdoe; id", "not a user name"),
    ("nomad-gate jobs --user jdoe && id", "doesn't take &&"),
    ("nomad-gate 'jobs", "quotes don't close"),
    ("bash -c id", "not something the gate does"),
    ("nomad-gate submit --user jdoe --script /x/j.sh --wrap id", "doesn't take --wrap"),
])
def test_serve_refuses(served, capsys, command, why):
    call, sent, answers, logged = served
    assert call(command) != 0
    doc = _out(capsys)
    assert doc["ok"] is False and why in doc["error"]
    assert sent == []


def test_serve_says_when_sudo_may_not_or_nothing_came_back(served, capsys, monkeypatch):
    call, sent, answers, logged = served
    answers["pre"] = 1                               # sudo -l: no (by its exit status, in any language)
    assert call("nomad-gate jobs --user jdoe") == 5
    assert "may not act as jdoe here (sudo)" in _out(capsys)["error"] and sent == []
    answers.update(pre=0, out="Traceback...\n", err="boom", code=1)
    assert call("nomad-gate jobs --user jdoe") == 4
    assert "ended with exit 1: boom" in _out(capsys)["error"]
    answers["slot"] = False                          # every place taken: refused at once
    assert call("nomad-gate jobs --user jdoe") == 4
    assert "busy" in _out(capsys)["error"] and len(sent) == 1
    answers["slot"] = True
    # its version, as itself: no person, no sudo
    assert call("nomad-gate version") == 0
    v = _out(capsys)
    assert v["ok"] and v["gate"] == gate.VERSION and len(sent) == 1
    # without sshd, serve says what it is for
    assert gate.serve("/x/nomad-gate", env={}) == 2 and "for sshd" in _out(capsys)["error"]
    # an install others could change: nothing is done
    monkeypatch.setattr(gate, "self_check", lambda me: "/sw is owned by uid 5123")
    assert call("nomad-gate jobs --user jdoe") == 3
    assert "won't serve as it is installed: /sw is owned by uid 5123" in _out(capsys)["error"]
    assert len(sent) == 1


def test_what_may_have_been_sent_says_so(served, capsys, monkeypatch):
    call, sent, answers, logged = served
    answers.update(out="", err="Killed", code=137)            # the person's side died: no answer
    assert call(f"nomad-gate submit --user jdoe --script /s/j.sh --sha256 {H}") == 4
    doc = _out(capsys)
    assert doc["maybe"] and "it may have been submitted" in doc["error"]
    assert call("nomad-gate jobs --user jdoe") == 4 and "maybe" not in _out(capsys)

    def slow(argv, **kw):
        if argv[2] == "-l":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert gate.serve("/x/nomad-gate", env={"SSH_ORIGINAL_COMMAND": f"nomad-gate again --job 4 --sha256 {H} --user jdoe"},
                      run_=slow, slot_=lambda: os.open(os.devnull, os.O_RDONLY), check_=lambda me: None) == 4
    assert _out(capsys)["maybe"]
    doc = gate.submitted(1, "Error: sbatch didn't answer in 5 minutes: check squeue before sending it again "
                            "(it may have been submitted)\n")
    assert doc["maybe"] and not doc["ok"]


def test_the_install_check_once_a_minute(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(gate, "self_check", lambda me: runs.append(me) or None)
    assert gate.checked("/sw/bin/nomad-gate", str(tmp_path)) is None
    assert gate.checked("/sw/bin/nomad-gate", str(tmp_path)) is None and len(runs) == 1
    assert gate.checked("/other/nomad-gate", str(tmp_path)) is None and len(runs) == 2      # another gate
    os.utime(tmp_path / "check.ok", (time.time() - 120,) * 2)
    monkeypatch.setattr(gate, "self_check", lambda me: "/sw is open")
    assert gate.checked("/other/nomad-gate", str(tmp_path)) == "/sw is open"               # stale: asked again
    assert gate.checked("/other/nomad-gate", str(tmp_path)) == "/sw is open"               # a failure isn't kept


def test_not_the_gates_own_account_nor_nobody(served, monkeypatch, capsys):
    call, sent, answers, logged = served
    monkeypatch.setattr(gate.os, "getuid", lambda: PERSON)        # the gate runs as jdoe's uid
    assert call("nomad-gate jobs --user jdoe") == 2
    assert "the gate's own account" in _out(capsys)["error"]
    monkeypatch.undo()
    with pytest.raises(gate.Refused, match="not for nfsnobody"):
        gate.person("nfsnobody")


def test_the_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "WAIT_FOR_SLOT", 0)
    held = [gate._slot(str(tmp_path)) for _ in range(gate.AT_ONCE)]
    assert all(h is not None for h in held) and gate._slot(str(tmp_path)) is None
    os.close(held.pop())
    again = gate._slot(str(tmp_path))
    assert again is not None
    for h in held + [again]:
        os.close(h)


# ── run: as the person ──────────────────────────────────────────────────────

@pytest.fixture
def person(monkeypatch, tmp_path):
    """run() as uid 5123, from a gate in tmp/bin beside a nomad, with
    bash recorded: (call, said, answers, folder)."""
    if os.getuid() != 0:
        pytest.skip("needs to own files as another uid")
    monkeypatch.setattr(gate, "_uid", lambda: PERSON)
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    b = tmp_path / "bin"
    b.mkdir()
    (b / "nomad").write_text("#!/bin/sh\n")
    os.chmod(b / "nomad", 0o755)
    w = tmp_path / "run 1"
    w.mkdir()
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -c 2\necho hi\n")
    os.chown(w / "job.sh", PERSON, -1)
    said, answers = [], {"out": b"", "code": 0}

    def run_(argv, **kw):
        said.append(argv)
        return subprocess.CompletedProcess(argv, answers["code"], answers["out"])

    def call(*words):
        return gate.run(list(words), str(b / "nomad-gate"), run_=run_)
    return call, said, answers, w


def test_submit_runs_nomad_in_the_login_environment_from_the_scripts_folder(person, capsys):
    call, said, answers, w = person
    answers["out"] = (b"Welcome to the cluster\n  Its output: /scratch/jdoe/jobs/JOBID-job.sh/ (nomad sends it ...)\n"
                      b"\n  Submitted job 4242. nomad set: memory.\n  Its output: /scratch/jdoe/jobs/4242-job.sh\n")
    sha = hashlib.sha256((w / "job.sh").read_bytes()).hexdigest()
    assert call("submit", "--script", str(w / "job.sh"), "--sha256", sha, "--mem", "8G", "--no-localscratch") == 0
    doc = _out(capsys)
    assert doc["ok"] and doc["job"] == "4242" and doc["output"] == "/scratch/jdoe/jobs/4242-job.sh"
    argv, = said
    nomad = str(w.parent / "bin" / "nomad")
    assert argv == ["/bin/bash", "-lc", gate.LOGIN, "nomad-gate", str(w),
                    nomad, "submit", "--yes", "--via", "console", "--script-sha256", sha, "--mem", "8G",
                    "--no-localscratch", str(w / "job.sh")]
    # not the text the person was shown: not sent
    assert call("submit", "--script", str(w / "job.sh"), "--sha256", H) == 2
    assert "changed since you looked at it" in _out(capsys)["error"] and len(said) == 1
    # sent again: from the home folder, the job's own values unless given
    answers["out"] = b"  Submitted job 4243.\n"
    assert call("again", "--job", "4242", "--sha256", sha) == 0
    assert said[-1][4:] == [str(w.parent), nomad, "submit", "--yes", "--via", "console", "--again", "4242",
                            "--script-sha256", sha]
    assert _out(capsys)["job"] == "4243"
    # sbatch refused: not ok, and nomad's words said
    answers.update(out=b"Error: sbatch refused it: invalid partition\n", code=1)
    assert call("again", "--job", "4242", "--sha256", sha) == 6
    doc = _out(capsys)
    assert doc["ok"] is False and doc["job"] is None and "invalid partition" in doc["said"]

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert gate.run(["again", "--job", "4242", "--sha256", H], str(w.parent / "bin" / "nomad-gate"), run_=slow) == 6
    doc = _out(capsys)
    assert doc["maybe"] and "it may have been submitted" in doc["error"] and "600 s" in doc["error"]


@pytest.mark.parametrize("said, code, ok, job, warning", [
    ("  Submitted job 4242. nomad set: memory.\n", 0, True, "4242", None),
    ("  Note: last time 'Submitted job 999.' was printed\n  Submitted job 4242.\n", 0, True, "4242", None),
    ("Error: sbatch ended with an error (oops), but it had submitted job 77: check it with squeue\n", 1,
     True, "77", "had submitted the job"),
    ("\n  Submitted (sbatch didn't print the job's number).\n", 0, True, None, "didn't print"),
    ("Error: sbatch refused it: nope\n", 1, False, None, None),
    ("  Submitted job 4242.\n", 1, False, None, None),
])
def test_what_nomad_submit_said(said, code, ok, job, warning):
    doc = gate.submitted(code, said)
    assert (doc["ok"], doc["job"]) == (ok, job)
    assert (warning in doc["warning"]) if warning else "warning" not in doc


def test_only_the_persons_own_job_script(person, capsys, tmp_path):
    call, said, answers, w = person
    (w / "theirs.sh").write_text("#!/bin/bash\n")
    (w / "notes.txt").write_text("hello\n")
    os.chown(w / "notes.txt", PERSON, -1)
    (w / "link.sh").symlink_to(w / "job.sh")
    (w / "big.sh").write_text("#!/bin/bash\n" + "#" * (300 * 1024))
    os.chown(w / "big.sh", PERSON, -1)
    for name, why in (("theirs.sh", "isn't yours"), ("notes.txt", "doesn't start with #!"),
                      ("link.sh", "is a link"), ("big.sh", "over 256 KB"), ("none.sh", "No such file")):
        assert call("submit", "--script", str(w / name), "--sha256", H) == 2
        assert why in _out(capsys)["error"]
        assert call("show", "--script", str(w / name)) == 2
        assert why in _out(capsys)["error"]
    assert said == []
    assert call("show", "--script", str(w / "job.sh")) == 0
    doc = _out(capsys)
    assert doc["text"].startswith("#!/bin/bash\n#SBATCH -c 2") and "warning" not in doc
    assert doc["sha256"] == hashlib.sha256((w / "job.sh").read_bytes()).hexdigest()
    # a folder others can change: said, with what closes it; not refused (1.25.1: the text is pinned)
    shared = w / "lab"
    shared.mkdir()
    os.chmod(shared, 0o775)
    (shared / "j.sh").write_text("#!/bin/bash\n")
    os.chown(shared / "j.sh", PERSON, -1)
    assert call("show", "--script", str(shared / "j.sh")) == 0
    doc = _out(capsys)
    assert f"{shared} can be changed by anyone in the group" in doc["warning"] and "chmod go-w" in doc["warning"]
    answers["out"] = b"  Submitted job 9.\n"
    assert call("submit", "--script", str(shared / "j.sh"), "--sha256", doc["sha256"]) == 0
    assert _out(capsys)["job"] == "9"
    said.clear()
    os.chmod(shared, 0o755)
    os.chmod(shared / "j.sh", 0o666)
    assert call("show", "--script", str(shared / "j.sh")) == 0
    assert "can be changed by anyone (mode 666)" in _out(capsys)["warning"]
    os.chmod(shared / "j.sh", 0o664)
    # ... nothing said when the group is the person's own (a user private group: no one else in it)
    gid = os.stat(shared / "j.sh").st_gid
    real_private = gate.private_groups
    gate.private_groups = lambda name: {gid}
    try:
        assert call("show", "--script", str(shared / "j.sh")) == 0 and "warning" not in _out(capsys)
    finally:
        gate.private_groups = real_private
    import grp
    me = grp.getgrgid(os.getgid()).gr_name
    assert gate.private_groups("no-such-group-9") == set()
    assert gate.private_groups(me) in (set(), {os.getgid()})
    os.mkfifo(w / "pipe.sh")
    os.chown(w / "pipe.sh", PERSON, -1)
    assert call("show", "--script", str(w / "pipe.sh")) == 2 and "isn't a file" in _out(capsys)["error"]
    assert said == []
    assert call("version") == 0 and _out(capsys)["home"] == str(w.parent)
    # run is for people: not root, not as someone else
    assert call("jobs", "--user", "someoneelse") == 2
    assert "--user is for serve" in _out(capsys)["error"]


def test_size_jobs_cancel_and_ls(person, capsys):
    call, said, answers, w = person
    plan = {"choices": {"cpus": {"value": 2}}, "request": {}, "notes": ["{not json"]}
    answers["out"] = b"module: loaded\n{ stray\n" + json.dumps(plan, indent=2).encode() + b"\n"
    assert call("size", "--script", str(w / "job.sh")) == 0
    assert _out(capsys) == {"ok": True, "plan": plan}
    assert said[-1][5:] == [str(w.parent / "bin" / "nomad"), "size", "--json", str(w / "job.sh")]
    assert call("size", "--job", "4242") == 0 and said[-1][4] == str(w.parent)
    capsys.readouterr()
    answers["out"] = b"Error: no such job\n"
    assert call("size", "--job", "1") == 2 and "nomad size didn't answer" in _out(capsys)["error"]
    answers["out"] = "\x1f".join(("4242", "my job", "RUNNING", "1:02", "4:00:00", "1", "spdr02",
                                  "/scratch/jdoe/run 1")).encode() + b"\nnoise\n"
    assert call("jobs") == 0
    assert _out(capsys)["jobs"] == [{"job": "4242", "name": "my job", "state": "RUNNING", "time": "1:02",
                                     "limit": "4:00:00", "nodes": "1", "where": "spdr02",
                                     "workdir": "/scratch/jdoe/run 1"}]
    assert said[-1][5:8] == ["squeue", "-u", str(PERSON)]          # uid 5123 has no name here
    answers.update(out=b"scancel: error: Kill job error on job id 9: Access/permission denied\n", code=1)
    assert call("cancel", "--job", "9") == 6
    doc = _out(capsys)
    assert doc["ok"] is False and "permission denied" in doc["said"] and said[-1][5:] == ["scancel", "9"]
    (w / ".hidden").write_text("")
    (w / "sub").mkdir()
    assert call("ls", "--dir", str(w)) == 0
    doc = _out(capsys)
    assert [e["name"] for e in doc["entries"]] == ["sub", "job.sh"]
    assert doc["entries"][1]["script"] and doc["entries"][1]["mine"] and not doc["more"]


def test_run_refuses_root(monkeypatch, capsys):
    monkeypatch.setattr(gate, "_uid", lambda: 0)
    assert gate.run(["jobs"], "/x/bin/nomad-gate") == 2
    assert "a person's own account" in _out(capsys)["error"]


def test_the_login_shell_gets_the_folder_and_words_as_arguments(tmp_path):
    odd = tmp_path / "a b $(touch pwned) ;"
    odd.mkdir()
    code, said = gate.in_login(str(odd), ["/bin/sh", "-c", 'pwd; printf "%s|" "$@"', "x", "$(id)", "a b"])
    assert code == 0, said
    assert said.splitlines()[-2:] == [str(odd), "$(id)|a b|"]
    assert not (tmp_path / "pwned").exists() and not os.path.exists("pwned")
    with pytest.raises(gate.Refused, match="couldn't go into"):
        gate.in_login(str(tmp_path / "missing"), ["/bin/true"])
    # the locale sudo was given isn't the person's: their login sets theirs
    os.environ["LC_ALL"] = "C"
    try:
        code, said = gate.in_login(str(odd), ["/bin/sh", "-c", 'echo "[${LC_ALL-unset}]"'])
    finally:
        del os.environ["LC_ALL"]
    assert said.splitlines()[-1] == "[unset]"
    # nomad's own exit 97 isn't taken for a folder it couldn't go into
    fake = lambda argv, **kw: subprocess.CompletedProcess(argv, 97, b"nomad: something\n")  # noqa: E731
    assert gate.in_login(str(odd), ["/x"], run_=fake) == (97, "nomad: something\n")


def test_the_tag_says_it_came_from_the_console(cluster_here):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    sha = hashlib.sha256((w / "job.sh").read_bytes()).hexdigest()
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "job.sh"])
    assert r.exit_code == 0, r.output
    assert ";via=console;" in next(a for a in sent[-1][0] if a.startswith("--extra="))
    doc = gate.submitted(0, r.output)
    assert doc["ok"] and doc["job"] == "4242"
    assert "--via" not in CliRunner().invoke(submit_cmd, ["--help"]).output     # for the gate, not people
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "web", "job.sh"])
    assert r.exit_code != 0
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "job.sh"])  # no checksum: not from the Console
    assert r.exit_code != 0 and "needs --script-sha256" in r.output
    # from the Console, nomad checks the script once more itself: not what the person saw, not sent
    n = len(sent)
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", H, "job.sh"])
    assert r.exit_code != 0 and "changed since you looked at it" in r.output and len(sent) == n
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])                    # by hand: as before
    assert r.exit_code == 0 and len(sent) == n + 1


# ── the hub's side ──────────────────────────────────────────────────────────

def _site(tmp_path, **kw):
    key = tmp_path / "id_gate"
    key.write_text("k")
    return {"name": "spydur", "host": "spydur.example.edu", "ssh_user": "installer", "submit": "gate",
            "gate_key": str(key), **kw}


def test_a_site_asked_through_its_gate(tmp_path):
    s = _site(tmp_path)
    assert remote.mode(s) == "gate" and remote.mode({"name": "x"}) == "copy"
    cfg = remote.settings(s)
    assert cfg == {"name": "spydur", "host": "spydur.example.edu", "user": "installer", "port": None,
                   "key": str(tmp_path / "id_gate")}
    for bad, why in (({"submit": "copy"}, "no gate"), ({"host": "-oProxyCommand=x"}, "isn't a host name"),
                     ({"ssh_user": "a b"}, "isn't an account name"), ({"port": "22;x"}, "isn't a number"),
                     ({"gate_key": str(tmp_path / "none")}, "isn't there")):
        with pytest.raises(remote.GateError, match=why):
            remote.settings({**s, **bad})
    words = remote.request("submit", "jdoe", script="/s/my run/job.sh", sha256=H, mem="8G", localscratch=False,
                           keep=True)
    assert words == ["submit", "--user", "jdoe", "--script", "/s/my run/job.sh", "--sha256", H, "--mem", "8G",
                     "--no-localscratch", "--keep"]
    with pytest.raises(gate.Refused):
        remote.request("submit", "jdoe", script="/s/j.sh", sha256=H, mem="8G --wrap=id")
    argv = remote.ssh_argv(dict(cfg, port=2222), words)
    assert argv[1:3] == ["-F", "/dev/null"]                      # none of this account's ssh settings
    for o in ("IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=yes",
              "ControlPath=none", "ClearAllForwardings=yes", "RequestTTY=no"):
        assert o in argv
    assert argv[argv.index("-i") + 1] == cfg["key"] and argv[argv.index("-p") + 1] == "2222"
    assert argv[-4:-1] == ["installer", "--", "spydur.example.edu"]
    assert shlex.split(argv[-1]) == [gate.GATE] + words          # what serve reads back


def test_the_hubs_answers(tmp_path):
    s = _site(tmp_path)
    got = {}

    def ssh(out="", err="", code=0):
        def run_(argv, **kw):
            got["argv"] = argv
            return subprocess.CompletedProcess(argv, code, out, err)
        return run_
    doc = remote.call(s, "jobs", "jdoe", run_=ssh('Last login...\n{"ok": true, "jobs": []}\n'))
    assert set(got["argv"]) and doc["ok"]
    assert doc == {"ok": True, "jobs": []} and shlex.split(got["argv"][-1]) == ["nomad-gate", "jobs", "--user",
                                                                                "jdoe"]
    doc = remote.call(s, "jobs", "jdoe", run_=ssh(err="installer@spydur: Permission denied (publickey).", code=255))
    assert doc["where"] == "ssh" and "nomad gate install-key there" in doc["error"]
    doc = remote.call(s, "jobs", "jdoe", run_=ssh(err="Host key verification failed.", code=255))
    assert "host key" in doc["error"] and "known_hosts" in doc["error"]
    doc = remote.call(s, "jobs", "jdoe", run_=ssh(code=1))
    assert doc["where"] == "site" and "command= nomad-gate serve" in doc["error"]
    doc = remote.call(s, "jobs", "jdoe", run_=ssh(err="bash: /usr/local/sw/nomad/bin/nomad-gate: No such file",
                                                  code=127))
    assert "nomad-gate isn't installed there" in doc["error"]
    # no answer: never got in (nothing sent), or lost on the way (a job may have been sent)
    doc = remote.call(s, "submit", "jdoe", script="/s/j.sh", sha256=H,
                      run_=ssh(err="Connection to spydur.example.edu closed by remote host.", code=255))
    assert doc["maybe"] and "may have been submitted" in doc["error"]
    doc = remote.call(s, "submit", "jdoe", script="/s/j.sh", sha256=H,
                      run_=ssh(err="ssh: connect to host spydur port 22: Connection refused", code=255))
    assert "maybe" not in doc
    doc = remote.call(s, "jobs", "jdoe", run_=ssh(err="Connection to x closed by remote host.", code=255))
    assert "maybe" not in doc

    def late(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert remote.call(s, "again", "jdoe", job="4", sha256=H, run_=late)["maybe"]
    assert remote.SSH_TIMEOUT > gate.WAIT_FOR_SLOT + gate.PREFLIGHT_TIMEOUT + gate.SERVE_TIMEOUT
    doc = remote.call(s, "submit", "jdoe", script="relative.sh", run_=ssh())
    assert doc["where"] == "hub" and "not a path" in doc["error"]
    doc = remote.call({**s, "submit": "copy"}, "jobs", "jdoe", run_=ssh())
    assert doc["where"] == "hub" and "no gate" in doc["error"]


def _a_gate(tmp_path):
    b = tmp_path / "sw" / "bin"
    b.mkdir(parents=True)
    for x in (tmp_path / "sw", b):
        os.chmod(x, 0o755)
    g = b / "nomad-gate"
    g.write_text("#!/bin/sh\n")
    os.chmod(g, 0o755)
    if gate.unsafe(str(g), {0, os.getuid()}):
        pytest.skip("this machine's temporary folder isn't safe itself")
    return str(g)


def test_the_key_line_on_the_cluster(tmp_path):
    ak = tmp_path / ".ssh" / "authorized_keys"
    gp = _a_gate(tmp_path)
    status, lines = remote.install_key(PUB, origins="141.166.1.2", gate_path=gp, path=ak)
    assert status == "would-change" and not ak.exists()
    want = (f'restrict,command="{gp} serve",from="141.166.1.2" ' + PUB)
    assert "  " + want in lines
    ak.parent.mkdir()
    ak.write_text("ssh-ed25519 AAAAOTHER other@x\n")
    status, lines = remote.install_key(PUB, origins="141.166.1.2", gate_path=gp, path=ak, apply=True)
    assert status == "changed" and ak.read_text() == "ssh-ed25519 AAAAOTHER other@x\n" + want + "\n"
    assert any(p.name.startswith("authorized_keys.bak-nomad-gate-") for p in ak.parent.iterdir())
    status, lines = remote.install_key(PUB, origins="141.166.1.2", gate_path=gp, path=ak, apply=True)
    assert status == "present" and "restricted to `nomad-gate serve`" in lines[0]
    for bad in ("nomad-gate", "/x/nomad-gate; id", "/x;id;/nomad-gate", "/x|id>/tmp/p&/nomad-gate", "/x/other",
                '/x/"nomad-gate', "/x/../nomad-gate", "/not/there/nomad-gate"):
        with pytest.raises(ValueError, match="--gate"):
            remote.install_key(PUB, origins="h", gate_path=bad, path=ak)
    os.chmod(os.path.dirname(gp), 0o775)
    with pytest.raises(ValueError, match="not safe to run from"):
        remote.install_key(PUB, origins="h", gate_path=gp, path=ak)


def test_nomad_gate_commands(tmp_path, monkeypatch):
    from nomad.cli import cli
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    r = CliRunner().invoke(cli, ["gate", "install-key", PUB])
    assert r.exit_code != 0 and "give --from" in r.output
    r = CliRunner().invoke(cli, ["gate", "install-key", PUB, "--from", "10.0.0.5", "--apply"])
    assert r.exit_code != 0 and "not safe to run from" in r.output          # no gate there yet
    gp = _a_gate(tmp_path)
    r = CliRunner().invoke(cli, ["gate", "install-key", PUB, "--from", "10.0.0.5", "--gate", gp, "--apply"])
    assert r.exit_code == 0, r.output
    assert f'command="{gp} serve",from="10.0.0.5"' in (home / ".ssh" / "authorized_keys").read_text()
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[hub]\ngate_key = "{tmp_path}/id_gate"\n[[hub.sites]]\nname = "spydur"\n'
                    'host = "spydur.example.edu"\nssh_user = "installer"\nsubmit = "gate"\n')
    (tmp_path / "id_gate").write_text("k")
    asked = []

    def ask(site, words, hub=None, **kw):
        asked.append((site["name"], words, hub.get("gate_key")))
        if words[0] == "version":
            user = words[2] if len(words) > 2 else None
            return {"ok": True, "gate": 1, "nomad": "1.25.0", "account": user or "installer"}
        return {"ok": True, "jobs": [{"job": "1"}]}
    monkeypatch.setattr(remote, "ask", ask)
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--site", "spydur", "--user", "jdoe"])
    assert r.exit_code == 0, r.output
    assert "ok   it acts as jdoe" in r.output and "1 job(s)" in r.output
    assert [w for _s, w, _k in asked] == [["version"], ["version", "--user", "jdoe"], ["jobs", "--user", "jdoe"]]
    assert asked[0][2] == f"{tmp_path}/id_gate"
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "ask", "--site", "spydur", "--user", "jdoe",
                                 "size", "--script", "/s/j.sh"])
    assert r.exit_code == 0, r.output and asked[-1][1] == ["size", "--user", "jdoe", "--script", "/s/j.sh"]
    r = CliRunner().invoke(cli, ["-c", str(toml), "gate", "test", "--site", "nope"])
    assert r.exit_code != 0 and "isn't one of the hub's" in r.output


def test_the_install_for_users_puts_the_gate_beside_nomad():
    text = open(os.path.join(ROOT, "scripts", "install_nomad_for_users.sh")).read()
    assert subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", "install_nomad_for_users.sh")]).returncode == 0
    assert "from nomad.submit.gate import main; sys.exit(main(sys.argv[2:], me=sys.argv[1]))" in text
    assert "nomad-gate nomad-gate.before nomad-gate.new nomad-gate.before.new" in text
    assert 'grep -q "nomad.submit.gate import main"' in text
    assert '"$D/bin/nomad-gate" check' in text


def test_the_gate_starts_as_its_launcher_starts_it(tmp_path):
    """The launcher's own line: python -I -B -c ... ME ARGS (python -c has no name of its own)."""
    code = "import sys; from nomad.submit.gate import main; sys.exit(main(sys.argv[2:], me=sys.argv[1]))"
    r = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path / "bin" / "nomad-gate"), "version"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r = subprocess.run([sys.executable, "-B", "-c", code, "/x/nomad-gate", "serve"], capture_output=True,
                       text=True, cwd=ROOT, env={**os.environ, "SSH_ORIGINAL_COMMAND": "nomad-gate version"})
    doc = json.loads(r.stdout)
    assert r.returncode == 3 and "won't serve as it is installed: /x/nomad-gate" in doc["error"]
