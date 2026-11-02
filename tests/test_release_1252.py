# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.25.2: nomad gives sbatch the script it sends itself, on its standard
input, never a file of its own (nothing on disk for someone to change
before sbatch reads it): so a folder above nomad's records that others can
change is said, not refused. Which record a job came from is noted by its
number (Slurm keeps no file name for it). From the Console, the sbatch
nomad runs is the one the person's PATH finds, by its full path, unless
someone else could have put it there; and what in the person's own account
others could change (their home, their login files, ~/.local) is said when
a script is shown: open with or without the Console, it closes when they
or the site close it."""
import hashlib
import os
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from nomad.submit import gate, history, programs, stage
from nomad.submit.cli import _run_sbatch as REAL_RUN_SBATCH
from nomad.submit.cli import plan as REAL_PLAN
from tests.test_release_1182 import Slurm, scratch, show  # noqa: F401  (fixture)


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _console(*words):
    return ["--yes", "--via", "console", *words]


# ── sbatch reads the text itself ────────────────────────────────────────────

def test_from_the_console_sbatch_is_given_the_text_and_run_by_its_full_path(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    shown = (w / "job.sh").read_bytes()
    r = CliRunner().invoke(submit_cmd, _console("--script-sha256", _sha(w / "job.sh"), "--no-localscratch",
                                                "job.sh"))
    assert r.exit_code == 0, r.output
    argv = sent[-1]
    found = programs.where("sbatch")                                          # as PATH found it: run by its path
    assert os.path.isabs(found) and found == shutil.which("sbatch") and programs.argv(argv)[0] == found
    assert argv.data == shown and all(a.startswith("--") for a in argv[1:])     # no file: the text itself
    record = stage.record_of_job("4242")
    assert open(record, "rb").read() == shown and f"< {record}" in r.output     # the command to run by hand
    # by hand, sbatch is still run by name, given the person's own script
    r = CliRunner().invoke(submit_cmd, ["--yes", "--no-localscratch", "job.sh"])
    assert r.exit_code == 0 and sent[-1][0] == "sbatch" and sent[-1][-1] == str(w / "job.sh")
    assert sent[-1].data is None and not programs.pinned() and programs.argv(["sbatch"]) == ["sbatch"]


def test_a_jobs_arguments_follow_dev_stdin(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh", "a b", "c"])         # through local scratch
    assert r.exit_code == 0, r.output
    assert sent[-1][-3:] == [stage.STDIN, "a b", "c"] and stage.unwrap(sent[-1].data.decode())
    assert "/dev/stdin 'a b' c <" in r.output


def test_sbatch_really_reads_it_on_its_standard_input(scratch, monkeypatch):  # noqa: F811
    # nomad's own sbatch runner, with an sbatch that reads its script as sbatch does: the
    # file named after its options, else its standard input
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    monkeypatch.setattr(sc, "_run_sbatch", REAL_RUN_SBATCH)
    b = tmp / "reads"
    b.mkdir()
    (b / "sbatch").write_text(
        "#!/bin/sh\nf=\nwhile [ $# -gt 0 ]; do case \"$1\" in --*) shift;; *) f=$1; shift; break;; esac; done\n"
        f"if [ -n \"$f\" ]; then cat \"$f\" > {tmp}/got; else cat > {tmp}/got; fi\n"
        f"printf '%s\\n' \"$f\" \"$@\" > {tmp}/words\necho 4242\n")
    (b / "sbatch").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    data = (w / "job.sh").read_bytes()
    r = CliRunner().invoke(sc.submit_cmd, _console("--script-sha256", _sha(w / "job.sh"), "--no-localscratch",
                                                   "job.sh"))
    assert r.exit_code == 0 and "Submitted job 4242." in r.output, r.output
    assert (tmp / "got").read_bytes() == data and (tmp / "words").read_text() == "\n"
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh", "x y"])            # staged, with an argument
    assert r.exit_code == 0, r.output
    assert stage.unwrap((tmp / "got").read_text())[0] == str(w / "job.sh")
    assert (tmp / "words").read_text() == "/dev/stdin\nx y\n"


def test_an_open_folder_above_nomads_records_doesnt_stop_it(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    store = os.path.expanduser(stage.STORE)
    os.makedirs(store, exist_ok=True)
    os.chmod(store, 0o777)
    r = CliRunner().invoke(submit_cmd, _console("--script-sha256", _sha(w / "job.sh"), "job.sh"))
    assert r.exit_code == 0, r.output
    assert oct(os.stat(store).st_mode & 0o777) == "0o700"                      # nomad's own: made private
    assert stage.unwrap(sent[-1].data.decode())[1].encode() == (w / "job.sh").read_bytes()


# ── which record a job came from ────────────────────────────────────────────

def test_a_note_says_which_record_a_job_came_from(tmp_path, monkeypatch):
    monkeypatch.setattr(stage, "STORE", str(tmp_path / "store"))
    rec = stage.carry_path("/home/amy/run/job.sh", b"#!/bin/bash\necho hi\n")
    stage.carry(rec, b"#!/bin/bash\necho hi\n", "/home/amy/run/job.sh")
    note = stage.note_job("86307", rec)
    assert oct(os.stat(note).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(note)).st_mode & 0o777) == "0o700"
    assert stage.record_of_job("86307") == rec and stage.record_of_job("86308") is None
    # only nomad's own records, by a job's number
    other = tmp_path / "job-c0123456789abcdef.sh"
    other.write_text("#!/bin/bash\n")
    assert stage.note_job("1", str(other)) is None                             # not in nomad's folder
    assert stage.note_job("1; rm", rec) is None and stage.note_job("2_3", rec) is None
    assert stage.record_of_job("../x") is None and stage.record_of_job(None) is None
    (tmp_path / "store" / stage.JOBS / "5").write_text("../../etc/passwd\n")
    assert stage.record_of_job("5") is None
    # a note whose record is gone leads nowhere; notes as old as nomad's records go, once a day at most
    os.unlink(rec)
    assert stage.record_of_job("86307") is None
    jobs = os.path.join(str(tmp_path / "store"), stage.JOBS)
    os.utime(note, (1, 1))
    os.utime(os.path.join(jobs, ".pruned"), (1, 1))                           # looked over long ago
    stage.prune_notes(jobs)
    assert not os.path.exists(note) and os.stat(os.path.join(jobs, ".pruned")).st_mtime > 1
    old = stage.note_job("86308", stage.carry(rec, b"#!/bin/bash\necho hi\n", "/home/amy/run/job.sh"))
    os.utime(old, (1, 1))
    stage.prune_notes(jobs)                                                  # looked over less than a day ago
    assert os.path.exists(old)


def test_sent_again_from_its_note(scratch, monkeypatch):  # noqa: F811
    import nomad.submit.cli as sc
    from tests.test_release_1180 import SACCT, Fake
    tmp, w, sent, slurm = scratch
    sha = _sha(w / "job.sh")
    r = CliRunner().invoke(sc.submit_cmd, _console("--script-sha256", sha, "--no-localscratch", "job.sh"))
    assert r.exit_code == 0, r.output
    stage.note_job("101", stage.record_of_job("4242"), when=stage.epoch("2026-10-01T10:00:00"))
    line = "sbatch --parsable --extra=nomad=1.25.2;via=console;cpus=s"          # what Slurm keeps: no file
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)).replace("sbatch job.sh", line, 1))
    monkeypatch.setattr(sc, "plan", lambda *a, **k: REAL_PLAN(*a, **{**k, "user": "amy", "run_sacct": f,
                                                                   "run_slurm": f}))
    r = CliRunner().invoke(sc.submit_cmd, _console("--script-sha256", sha, "--again", "101", "--no-localscratch"))
    assert r.exit_code == 0, r.output
    assert "was sent from the Console; its own script is" in r.output
    assert sent[-1].data == (w / "job.sh").read_bytes()
    # no note: nothing guessed (its words are its arguments, not scripts)
    os.unlink(os.path.join(os.path.expanduser(stage.STORE), stage.JOBS, "101"))
    (w / "other.sh").write_text("#!/bin/bash\necho other\n")
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)).replace("sbatch job.sh", line + " /dev/stdin other.sh", 1))
    pl = sc.plan(None, job="101")
    assert pl.script is None and any("came on standard input" in n for n in pl.notes)
    r = CliRunner().invoke(sc.submit_cmd, ["--dry-run", "--again", "101"])
    assert r.exit_code != 0 and "doesn't know this job's script" in r.output


def test_past_runs_sent_on_standard_input_are_their_scripts(tmp_path, monkeypatch):
    from nomad.submit import cli
    from nomad.submit.history import PastRun
    monkeypatch.setattr(stage, "STORE", str(tmp_path / "store"))
    w = tmp_path / "w"
    w.mkdir()
    (w / "job.sh").write_text("#!/bin/bash\ng16 < w.com > w.log\n")
    data = (w / "job.sh").read_bytes()
    rec = stage.carry(stage.carry_path(str(w / "job.sh"), data), data, str(w / "job.sh"))
    stage.note_job("700", rec)
    runs = []
    for jid, arr, line in (("700", None, "sbatch --parsable --extra=nomad=1.25.2;via=console"),
                           ("701_3", "701", "sbatch --parsable /dev/stdin a"),
                           ("702", None, "sbatch --parsable")):
        run = PastRun.__new__(PastRun)
        for k, v in {"job_id": jid, "array_job": arr, "submit_line": line, "workdir": str(w), "script": None,
                     "family": None, "executable": None}.items():
            setattr(run, k, v)
        runs.append(run)
    stage.note_job("701", rec)
    cli.name_programs(runs)
    assert [r.script for r in runs] == [str(w / "job.sh"), str(w / "job.sh"), None]
    assert runs[0].family == "Gaussian"
    assert history.script_path("sbatch --parsable /dev/stdin a", str(w)) is None


# ── the sbatch the Console runs ─────────────────────────────────────────────

def test_an_sbatch_someone_else_could_have_put_there_is_refused(scratch, monkeypatch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    sha = _sha(w / "job.sh")
    open_bin = tmp / "open-bin"
    open_bin.mkdir()
    os.chmod(open_bin, 0o777)
    (open_bin / "sbatch").write_text("#!/bin/sh\n")
    (open_bin / "sbatch").chmod(0o755)
    monkeypatch.setenv("PATH", f"{open_bin}:{os.environ['PATH']}")
    r = CliRunner().invoke(submit_cmd, _console("--script-sha256", sha, "job.sh"))
    assert r.exit_code != 0 and sent == []
    assert f"the sbatch nomad would run is {open_bin}/sbatch" in r.output and "could have put it there" in r.output
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])                    # by hand: the person's own choice
    assert r.exit_code == 0 and sent[-1][0] == "sbatch"
    monkeypatch.setenv("PATH", str(tmp / "nothing-here"))
    r = CliRunner().invoke(submit_cmd, _console("--script-sha256", sha, "job.sh"))
    assert r.exit_code != 0 and "no sbatch on your PATH" in r.output


def test_programs_others_could_change():
    assert gate.program_open("/bin/sh") is None                                 # root's
    assert "/tmp can be changed by anyone (mode 1777)" in gate.program_open("/tmp")   # sticky: only above it


def test_a_program_in_an_open_folder(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "sbatch").write_text("#!/bin/sh\n")
    assert gate.program_open(str(d / "sbatch")) is None
    os.chmod(d, 0o775)
    assert f"{d} can be changed by anyone in the group" in gate.program_open(str(d / "sbatch"))
    os.chmod(d, 0o755)
    (d / "sbatch").chmod(0o757)
    assert "can be changed by anyone (mode 757)" in gate.program_open(str(d / "sbatch"))
    (d / "sbatch").chmod(0o775)
    assert gate.program_open(str(d / "sbatch"), private={os.stat(d / "sbatch").st_gid}) is None   # their own group
    os.symlink(str(d / "sbatch"), str(tmp_path / "link"))
    os.chmod(tmp_path, 0o777)
    try:
        assert f"{tmp_path} can be changed by anyone" in gate.program_open(str(tmp_path / "link"))
    finally:
        os.chmod(tmp_path, 0o700)


# ── who else could run programs as the person ───────────────────────────────

def test_an_account_others_could_change(tmp_path):
    home = tmp_path / "jdoe"
    home.mkdir()
    os.chmod(home, 0o711)
    uid = os.getuid()
    assert gate.account_open(str(home), uid) is None
    (home / ".bashrc").write_text("# mine\n")
    os.chmod(home / ".bashrc", 0o664)
    why = gate.account_open(str(home), uid)
    assert why.startswith(f"{home}/.bashrc can be changed by anyone in the group")
    assert f"to close it: chmod go-w {home}/.bashrc" in why and "with or without the Console" in why
    os.chmod(home / ".bashrc", 0o644)
    for d in (".local/share", ".local/bin", ".local/lib/python3.9/site-packages"):
        (home / d).mkdir(parents=True, exist_ok=True)
    os.chmod(home / ".local/lib/python3.9/site-packages", 0o2775)
    why = gate.account_open(str(home), uid)
    assert why.startswith(f"{home}/.local/lib/python3.9/site-packages can be changed by anyone in the group")
    assert why.endswith(f"chmod -R go-w {home}/.local")                         # all of it, at once
    os.chmod(home / ".local/lib/python3.9/site-packages", 0o755)
    for d in (".local", ".local/share"):
        os.chmod(home / d, 0o2775)                                              # as on spydur: people's
    assert gate.account_open(str(home), uid).startswith(f"{home}/.local can be changed by")
    assert gate.account_open(str(home), uid, private={os.stat(home / ".local").st_gid}) is None   # their own
    os.chmod(home / ".local", 0o755)
    os.chmod(home / ".local/share", 0o755)
    os.chmod(tmp_path, 0o777)                                                   # above their home: the site's
    try:
        assert gate.account_open(str(home), uid).endswith(f"the cluster's admins: chmod go-w {tmp_path}")
    finally:
        os.chmod(tmp_path, 0o700)
    assert gate.account_open(str(home), uid) is None                            # closed: nothing said


@pytest.fixture
def person(monkeypatch, tmp_path):
    if os.getuid() != 0:
        pytest.skip("needs to own files as another uid")
    monkeypatch.setattr(gate, "_uid", lambda: 5123)
    monkeypatch.setattr(gate, "_account", lambda: "jdoe")
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    return tmp_path


def test_shown_with_who_else_could_change_it(person, capsys):
    import json
    home = person
    (home / "job.sh").write_text("#!/bin/bash\necho hi\n")
    os.chown(home / "job.sh", 5123, -1)
    run = ["show", "--script", str(home / "job.sh")]
    assert gate.run(run, "/x/bin/nomad-gate") == 0
    assert "warning" not in json.loads(capsys.readouterr().out)
    (home / ".local").mkdir()
    os.chmod(home / ".local", 0o2775)
    assert gate.run(run, "/x/bin/nomad-gate") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["warning"].startswith(f"{home}/.local can be changed by") and "chmod -R go-w" in doc["warning"]
    os.chmod(home / "job.sh", 0o666)                                            # the script too: both, one sentence
    assert gate.run(run, "/x/bin/nomad-gate") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["warning"].startswith(f"{home}/job.sh can be changed by anyone (mode 666)")
    assert f". Also, {home}/.local can be changed by" in doc["warning"] and not doc["warning"].endswith(".")
    assert doc["sha256"] == _sha(home / "job.sh")


def test_the_real_sbatch_reading_stdin_is_what_subprocess_gives(tmp_path):
    # nomad's runner hands the bytes over exactly, and reads back what sbatch says
    cat = tmp_path / "sbatch"
    cat.write_text(f"#!/bin/sh\ncat > {tmp_path}/got\necho 'Submitted 1'\necho oops >&2\nexit 3\n")
    cat.chmod(0o755)
    data = b"#!/bin/bash\necho caf\xe9\n"
    r = REAL_RUN_SBATCH([str(cat)], str(tmp_path), data)
    assert (tmp_path / "got").read_bytes() == data
    assert r.returncode == 3 and r.stdout == "Submitted 1\n" and r.stderr == "oops\n"
    assert isinstance(subprocess.CompletedProcess, type)


# ── Slurm's programs, found once and checked before any runs ────────────────

def test_nothing_runs_when_a_slurm_program_could_have_been_put_there(scratch, monkeypatch):  # noqa: F811
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    open_bin = tmp / "open-bin"
    open_bin.mkdir()
    os.chmod(open_bin, 0o777)
    (open_bin / "scontrol").write_text("#!/bin/sh\ntouch planted\n")
    (open_bin / "scontrol").chmod(0o755)
    monkeypatch.setenv("PATH", f"{open_bin}:{os.environ['PATH']}")
    planned = []
    monkeypatch.setattr(sc, "plan", lambda *a, **k: planned.append(a) or REAL_PLAN(*a, **k))
    r = CliRunner().invoke(sc.submit_cmd, _console("--script-sha256", _sha(w / "job.sh"), "job.sh"))
    assert r.exit_code != 0 and f"the scontrol nomad would run is {open_bin}/scontrol" in r.output
    assert "Not sent." in r.output and planned == [] and sent == []           # refused before anything ran
    r = CliRunner().invoke(sc.size_cmd, ["--json", "--via", "console", "job.sh"])
    assert r.exit_code != 0 and "Not run." in r.output and planned == []
    assert "--via" not in CliRunner().invoke(sc.size_cmd, ["--help"]).output
    r = CliRunner().invoke(sc.size_cmd, ["--json", "job.sh"])                  # by hand: as before
    assert r.exit_code == 0 and len(planned) == 1 and not programs.pinned()


def test_a_pinned_program_runs_by_its_full_path(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    for n in ("sbatch", "scontrol"):
        (b / n).write_text("#!/bin/sh\necho \"$0 $*\"\n")
        (b / n).chmod(0o755)
    programs.pin(env={"PATH": str(b)})
    assert programs.argv(["scontrol", "show", "config"]) == [str(b / "scontrol"), "show", "config"]
    assert programs.argv(["/usr/bin/env", "x"]) == ["/usr/bin/env", "x"]          # not one of Slurm's
    with pytest.raises(FileNotFoundError):
        programs.argv(["sinfo", "-h"])                                         # not on that PATH: not run
    from nomad.submit import cluster
    assert cluster._run(["scontrol", "show", "config"]).stdout.startswith(str(b / "scontrol"))
    programs.unpin()
    assert programs.argv(["sinfo", "-h"]) == ["sinfo", "-h"]


def test_the_gates_own_slurm_steps_are_checked_in_the_login(tmp_path):
    import sys
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "squeue").write_text("#!/bin/sh\necho \"squeue says $*\"\n")
    (safe / "squeue").chmod(0o755)
    code = "import sys; from nomad.submit.gate import main; sys.exit(main(sys.argv[1:], me='/x/nomad-gate'))"

    def gate_exec(path, *words):
        return subprocess.run([sys.executable, "-c", code, "exec", *words], capture_output=True, text=True,
                              env={**os.environ, "PATH": path})
    r = gate_exec(str(safe), "squeue", "-u", "jdoe")
    assert r.returncode == 0 and r.stdout == "squeue says -u jdoe\n"
    r = gate_exec(str(safe), "sbatch", "x")                                   # not one the gate runs itself
    assert r.returncode == gate.EXEC_REFUSED and gate.EXEC_SAYS in r.stderr
    os.chmod(safe, 0o777)
    r = gate_exec(str(safe), "squeue", "-u", "jdoe")
    assert r.returncode == gate.EXEC_REFUSED and "the squeue nomad would run is" in r.stderr
    assert "squeue says" not in r.stdout
    r = gate_exec(str(tmp_path / "none"), "scancel", "9")
    assert r.returncode == gate.EXEC_REFUSED and "no scancel on your PATH" in r.stderr
    # in_login: such a refusal is the answer, said as it is
    said = (gate.EXEC_SAYS + "the squeue nomad would run is /x/squeue, and /x can be changed by anyone (mode 777)"
            ": someone else could have put it there\n").encode()
    with pytest.raises(gate.Refused, match="the squeue nomad would run is /x/squeue"):
        gate.in_login(str(tmp_path), ["/x/nomad-gate", "exec", "squeue"],
                      run_=lambda argv, **kw: subprocess.CompletedProcess(argv, gate.EXEC_REFUSED, said))


def test_a_note_is_for_its_cluster_its_folder_and_its_time(tmp_path, monkeypatch):
    monkeypatch.setattr(stage, "STORE", str(tmp_path / "store"))
    a = stage.carry(stage.carry_path("/home/amy/a/job.sh", b"#!/bin/bash\necho a\n"), b"#!/bin/bash\necho a\n",
                    "/home/amy/a/job.sh")
    b = stage.carry(stage.carry_path("/home/amy/b/job.sh", b"#!/bin/bash\necho b\n"), b"#!/bin/bash\necho b\n",
                    "/home/amy/b/job.sh")
    t = stage.epoch("2026-11-02T13:08:00")
    stage.note_job("4242", a, cluster="spydur", folder="/home/amy/a", when=t)
    stage.note_job("4242", b, cluster="spiderweb", folder="/home/amy/b", when=t)    # one home, two clusters
    assert stage.record_of_job("4242", cluster="spydur") == a
    assert stage.record_of_job("4242", cluster="spiderweb") == b
    assert stage.record_of_job("4242", cluster="arachne") is None
    assert stage.record_of_job("4242", cluster="spydur", folder="/home/amy/b") is None      # not that job
    assert stage.record_of_job("4242", cluster="spydur", submitted="2026-11-02T13:09:30") == a
    assert stage.record_of_job("4242", cluster="spydur", submitted="2027-03-01T08:00:00") is None   # number reused
    assert stage.record_of_job("4242", folder="/home/amy/b") == b                # its cluster's name unknown here


def test_the_clusters_own_name():
    from nomad.submit import cluster

    def config(out, code=0):
        return lambda argv: subprocess.CompletedProcess(argv, code, out, "")
    assert cluster.cluster_name(config("AccountingStorageType = x\nClusterName             = spydur\n")) == "spydur"
    assert cluster.cluster_name(config("ClusterName = a/b\n")) is None
    assert cluster.cluster_name(config("", 1)) is None


def test_an_exec_that_fails_is_said(tmp_path):
    import sys
    b = tmp_path / "bin"
    b.mkdir()
    (b / "squeue").write_text("no #! line here\n")
    (b / "squeue").chmod(0o755)
    code = "import sys; from nomad.submit.gate import main; sys.exit(main(sys.argv[1:], me='/x/nomad-gate'))"
    r = subprocess.run([sys.executable, "-c", code, "exec", "squeue"], capture_output=True, text=True,
                       env={**os.environ, "PATH": str(b)})
    assert r.returncode == gate.EXEC_REFUSED and gate.EXEC_SAYS in r.stderr and "Traceback" not in r.stderr


def test_a_program_someone_else_owns_or_links_through(tmp_path):
    other = 4321
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    theirs = sticky / "bin"                                   # made in a sticky folder by someone else
    theirs.mkdir()
    (theirs / "sbatch").write_text("#!/bin/sh\n")
    (theirs / "sbatch").chmod(0o755)
    if os.getuid() == 0:
        os.chown(theirs, other, -1)
        why = gate.program_open(str(theirs / "sbatch"))
        assert why == f"{theirs} belongs to uid {other}" or why.startswith(f"{theirs} belongs to")
        assert gate.program_open(str(theirs / "sbatch"), owners={0, other}) is None   # one it trusts
    # a link that leads through a folder anyone can change, two steps deep
    real = tmp_path / "real"
    real.mkdir()
    (real / "sbatch").write_text("#!/bin/sh\n")
    (real / "sbatch").chmod(0o755)
    alts = tmp_path / "alts"
    alts.mkdir()
    os.symlink(str(real / "sbatch"), str(alts / "sbatch"))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    os.symlink(str(alts / "sbatch"), str(bin_ / "sbatch"))
    assert gate.link_steps(str(bin_ / "sbatch")) == [str(bin_ / "sbatch"), str(alts / "sbatch"), str(real / "sbatch")]
    assert gate.program_open(str(bin_ / "sbatch")) is None
    os.chmod(alts, 0o777)
    assert f"{alts} can be changed by anyone" in gate.program_open(str(bin_ / "sbatch"))
    os.chmod(alts, 0o755)
    os.symlink("real", str(tmp_path / "rel"))                                   # a relative link to a folder
    assert gate.link_steps(str(tmp_path / "rel" / "sbatch"))[-1] == str(real / "sbatch")
