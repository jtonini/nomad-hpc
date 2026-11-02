# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.25.1: a job sent from the Console is the very text the person was
shown -- its sha256 checked by the gate and again by nomad, and sbatch is
given that text, never the path (on its standard input since 1.25.2) -- so
a folder others can change is said rather than refused; and a job's own
details (Slurm's view of it, its accounting and the end of its output),
for the Console."""
import hashlib
import os
import subprocess

import pytest
from click.testing import CliRunner

from nomad.submit import gate, remote, stage
from nomad.submit.cli import plan as REAL_PLAN
from tests.test_release_1182 import Slurm, scratch, show  # noqa: F401  (fixture)

PERSON = 5123


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ── nomad submit --via console ──────────────────────────────────────────────

def test_sbatch_is_given_nomads_copy_of_the_text_shown(scratch, monkeypatch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    sha = _sha(w / "job.sh")
    original = (w / "job.sh").read_bytes()
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "--no-localscratch",
                                        "job.sh"])
    assert r.exit_code == 0, r.output
    assert sent[-1].data == original and str(w / "job.sh") not in sent[-1]     # the text itself, no path
    copy = stage.record_of_job("4242")                                        # nomad's record of it
    assert os.path.dirname(copy) == os.path.expanduser(stage.STORE)
    assert open(copy, "rb").read() == original and oct(os.stat(copy).st_mode & 0o777) == "0o600"
    assert stage.carried_from(copy) == str(w / "job.sh")
    assert "is nomad's copy of" in r.output and "as you saw it" in r.output
    extra = next(a for a in sent[-1] if a.startswith("--extra="))
    assert ";via=console;" in extra and "stage=l" not in extra
    assert not any(a.startswith("--job-name") for a in sent[-1])        # the script names itself (-J water)
    # a script that doesn't name itself keeps its own name, as sbatch would have given it
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\ng16 < w.com > w.log\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", _sha(w / "job.sh"),
                                        "--no-localscratch", "job.sh"])
    assert r.exit_code == 0, r.output
    assert "--job-name=job.sh" in sent[-1] and str(w / "job.sh") not in sent[-1]
    # changed since the person looked: not sent
    n = len(sent)
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "job.sh"])
    assert r.exit_code != 0 and "changed since you looked at it" in r.output and len(sent) == n


def test_through_local_scratch_the_wrapper_carries_the_text_shown(scratch, monkeypatch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    shown = (w / "job.sh").read_bytes()
    sha = hashlib.sha256(shown).hexdigest()
    real = sc._pinned

    def pinned_then_changed(path, pin):
        data = real(path, pin)
        (w / "job.sh").write_text("#!/bin/bash\nrm -rf ~\n")          # swapped right after the check
        return data
    monkeypatch.setattr(sc, "_pinned", pinned_then_changed)
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "job.sh"])
    assert r.exit_code == 0, r.output
    script, text = stage.unwrap(sent[-1].data.decode())
    assert script == str(w / "job.sh") and text.encode() == shown            # what was shown, not the swap
    # ... and sized from it too: the Gaussian input it names (8 threads), not the swapped script
    assert "--cpus-per-task=8" in sent[-1]


def test_a_record_nomad_cant_keep_is_said_and_the_text_still_sent(scratch, monkeypatch):  # noqa: F811
    # 1.25.1 refused: sbatch read nomad's copy. Since 1.25.2 it reads the text itself.
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(stage, "carry", refuse)
    monkeypatch.setattr(stage, "keep", refuse)
    sha = _sha(w / "job.sh")
    for flag in ("--no-localscratch", "--localscratch"):
        r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, flag, "job.sh"])
        assert r.exit_code == 0 and "couldn't keep its record" in r.output and sent[-1].data
    assert stage.record_of_job("4242") is None


def test_sent_again_from_the_persons_own_script(scratch, monkeypatch):  # noqa: F811
    import nomad.submit.cli as sc
    from tests.test_release_1180 import SACCT, Fake
    tmp, w, sent, slurm = scratch
    sha = _sha(w / "job.sh")
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha,
                                           "--no-localscratch", "job.sh"])
    assert r.exit_code == 0, r.output
    copy = stage.record_of_job("4242")
    line = f"sbatch --parsable --extra=nomad=1.25.1;via=console {copy}"         # as 1.25.1 sent it
    f = Fake(sacct=SACCT.replace("/home/amy/w", str(w)).replace("sbatch job.sh", line, 1))
    monkeypatch.setattr(sc, "plan", lambda *a, **k: REAL_PLAN(*a, **{**k, "user": "amy", "run_sacct": f,
                                                                   "run_slurm": f}))
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "--again", "101",
                                           "--no-localscratch"])
    assert r.exit_code == 0, r.output
    assert "was sent from the Console; its own script is" in r.output
    assert sent[-1].data == (w / "job.sh").read_bytes() and stage.carried_from(stage.record_of_job("4242"))
    # the person's script changed since: sent again only once they have looked at it
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water\necho new\n")
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "--again", "101"])
    assert r.exit_code != 0 and "changed since you looked at it" in r.output


def test_nomads_copies_and_where_they_came_from(tmp_path, monkeypatch):
    monkeypatch.setattr(stage, "STORE", str(tmp_path / "store"))
    p = stage.carry_path("/home/amy/run/job.sh", b"#!/bin/bash\necho hi\n")
    stage.carry(p, b"#!/bin/bash\necho hi\n", "/home/amy/run/job.sh")
    assert stage.carried_from(p) == "/home/amy/run/job.sh"
    assert stage.sent(p, "#!/bin/bash\necho hi\n") == ("/home/amy/run/job.sh", "#!/bin/bash\necho hi\n", "carried")
    other = tmp_path / "job.sh"                                   # a script elsewhere, with a note beside it
    other.write_text("#!/bin/bash\n")
    (tmp_path / "job.sh.src").write_text("/etc/passwd\n")
    assert stage.carried_from(str(other)) is None and stage.sent(str(other), "#!/bin/bash\n") is None
    os.unlink(p + stage.SRC)
    assert stage.carried_from(p) is None
    # a note whose copy is gone goes with the next prune
    q = stage.carry_path("/x/a.sh", b"#!/bin/sh\n")
    stage.carry(q, b"#!/bin/sh\n", "/x/a.sh")
    os.unlink(q)
    stage.prune(os.path.dirname(q))
    assert not os.path.exists(q + stage.SRC)


# ── the gate ────────────────────────────────────────────────────────────────

@pytest.fixture
def person(monkeypatch, tmp_path):
    if os.getuid() != 0:
        pytest.skip("needs to own files as another uid")
    monkeypatch.setattr(gate, "_uid", lambda: PERSON)
    monkeypatch.setattr(gate, "_account", lambda: "jdoe")
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    return tmp_path


SHOW = ("JobId=86307 JobName=job.sh UserId=jdoe(5123) GroupId=people(100) JobState=RUNNING Reason=None "
        "RunTime=00:01:02 TimeLimit=00:40:00 NodeList=spdr01 WorkDir=/home/jdoe/my run StdOut={out} "
        "StdErr={out} TRES=cpu=1,mem=1G,node=1")


def test_a_jobs_details_its_record_and_the_end_of_its_output(person, capsys):
    out = person / "slurm-86307.out"
    out.write_text("".join(f"line {i}\n" for i in range(1, 501)))
    os.chown(out, PERSON, -1)
    asked = []

    def run_(argv, **kw):
        assert argv[6] == "exec"                                   # the gate's own exec, in the login (1.25.2)
        asked.append(argv[7:])
        if argv[7] == "scontrol":
            return subprocess.CompletedProcess(argv, 0, SHOW.format(out=out).encode() + b"\n")
        rows = ("86307|job.sh|jdoe|RUNNING|0:0|2026-11-02T09:46:00|2026-11-02T09:46:05|Unknown|00:01:02|"
                "00:40:00|1|spdr01|1|1G||00:00:01|cpu=1,mem=1G,node=1|/home/jdoe/my run\n"
                "86307.batch|batch||RUNNING|0:0|2026-11-02T09:46:05|2026-11-02T09:46:05|Unknown|00:01:02||1|"
                "spdr01|1||12M|00:00:01|cpu=1,mem=1G,node=1|\n"
                "86307|another's|amy|RUNNING|0:0|a|b|c|d|e|1|n|1|1G||0|cpu=1|/home/amy\n")   # a reused number
        return subprocess.CompletedProcess(argv, 0, rows.replace("|", gate.SEP).encode())
    assert gate.run(["job", "--job", "86307"], str(person / "bin" / "nomad-gate"), run_=run_) == 0
    doc = __import__("json").loads(capsys.readouterr().out)
    assert doc["ok"] and doc["now"]["JobState"] == "RUNNING" and doc["now"]["WorkDir"] == "/home/jdoe/my run"
    assert [r["JobID"] for r in doc["acct"]] == ["86307", "86307.batch"] and doc["acct"][1]["MaxRSS"] == "12M"
    (o,) = doc["output"]                                          # stdout and stderr the same file: once
    assert o["path"] == str(out) and o["tail"].splitlines()[-1] == "line 500"
    assert len(o["tail"].splitlines()) == gate.TAIL_LINES and o["cut"]
    assert asked[0] == ["scontrol", "show", "job", "-o", "86307"]
    assert asked[1][:5] == ["sacct", "-j", "86307", "-u", "jdoe"] and gate.SEP in asked[1]


def test_someone_elses_job_isnt_shown(person, capsys):
    def run_(argv, **kw):
        if argv[7] == "scontrol":
            return subprocess.CompletedProcess(argv, 0, SHOW.format(out="/x").replace("jdoe(5123)", "amy(5124)")
                                               .encode())
        return subprocess.CompletedProcess(argv, 0, b"")
    assert gate.run(["job", "--job", "86307"], str(person / "nomad-gate"), run_=run_) == 2
    assert "isn't yours" in __import__("json").loads(capsys.readouterr().out)["error"]

    def nothing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"slurm_load_jobs error: Invalid job id specified\n")
    assert gate.run(["job", "--job", "9"], str(person / "nomad-gate"), run_=nothing) == 2
    assert "knows no job 9 of yours" in __import__("json").loads(capsys.readouterr().out)["error"]


def test_an_ended_jobs_output_from_its_record(person, capsys):
    out = person / "jobs"
    out.mkdir()
    (out / "slurm-77.out").write_text("done\n")

    def run_(argv, **kw):
        if argv[5] == "scontrol":
            return subprocess.CompletedProcess(argv, 1, b"Invalid job id\n")
        if "-X" in argv:
            return subprocess.CompletedProcess(argv, 0, gate.SEP.join(["slurm-%j.out", "slurm-%j.out", "j|x", str(out),
                                                                       "77", "77"]).encode() + b"\n")
        return subprocess.CompletedProcess(argv, 0, gate.SEP.join(
            ["77", "j|x", "jdoe", "COMPLETED", "0:0", "a", "b", "c", "00:00:05", "00:10:00", "1", "n1", "1", "1G", "",
             "0", "cpu=1", str(out)]).encode() + b"\n")
    assert gate.run(["job", "--job", "77"], str(person / "nomad-gate"), run_=run_) == 0
    doc = __import__("json").loads(capsys.readouterr().out)
    assert doc["now"] == {} and doc["output"][0]["path"] == str(out / "slurm-77.out")
    assert doc["acct"][0]["JobName"] == "j|x"                         # a | in a name stays in it
    assert doc["output"][0]["tail"] == "done"


def test_the_hub_asks_for_details_and_pins_what_it_sends(tmp_path):
    words = remote.request("job", "jdoe", job="86307")
    assert words == ["job", "--user", "jdoe", "--job", "86307"]
    with pytest.raises(gate.Refused, match="needs --sha256"):
        remote.request("submit", "jdoe", script="/s/j.sh")
    with pytest.raises(gate.Refused, match="needs --sha256"):
        remote.request("again", "jdoe", job="4")
    with pytest.raises(gate.Refused, match="not a sha"):
        remote.request("submit", "jdoe", script="/s/j.sh", sha256="A" * 64)


def test_an_array_tasks_output_path():
    f = {"JobName": "arr", "JobID": "4242_7", "JobIDRaw": "4249"}
    assert gate._output_path("/o/slurm-%A_%a.out", f, "4242_7") == "/o/slurm-4242_7.out"
    assert gate._output_path("/o/%x-%j.out", f, "4249") == "/o/arr-4249.out"
    assert gate._output_path("/o/%5j.out", {"JobID": "77", "JobIDRaw": "77"}, "77") == "/o/00077.out"


def test_what_nomad_said_when_it_refused():
    doc = gate.submitted(1, "  some lines\nError: /x/job.sh changed since you looked at it: look at it again\n")
    assert not doc["ok"] and doc["error"].startswith("/x/job.sh changed since you looked at it")


def test_from_the_console_nomads_folder_is_made_private_and_one_above_doesnt_stop_it(scratch,  # noqa: F811
                                                                                       monkeypatch):
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    store = os.path.expanduser(stage.STORE)
    os.makedirs(store, exist_ok=True)
    os.chmod(store, 0o777)
    os.chmod(os.path.dirname(store), 0o775)
    sha = _sha(w / "job.sh")
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "--no-localscratch",
                                        "job.sh"])
    assert r.exit_code == 0, r.output                                  # its own folders: made private
    assert oct(os.stat(store).st_mode & 0o777) == "0o700"
    up = os.path.dirname(os.path.dirname(store))
    os.chmod(up, 0o777)              # a folder above, not nomad's: sent all the same (1.25.2: nothing to swap)
    try:
        n = len(sent)
        r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", sha, "job.sh"])
        assert r.exit_code == 0, r.output
        assert len(sent) == n + 1 and stage.unwrap(sent[-1].data.decode())[1].encode() == (w / "job.sh").read_bytes()
    finally:
        os.chmod(up, 0o755)


def test_a_script_that_isnt_utf8_is_sent_exactly(scratch):  # noqa: F811
    from nomad.submit.cli import submit_cmd
    tmp, w, sent, slurm = scratch
    data = b"#!/bin/bash\n#SBATCH -J water\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\necho caf\xe9\n"
    (w / "job.sh").write_bytes(data)
    r = CliRunner().invoke(submit_cmd, ["--yes", "--via", "console", "--script-sha256", hashlib.sha256(data).hexdigest(),
                                        "job.sh"])
    assert r.exit_code == 0, r.output
    assert sent[-1].data == data and "isn't plain UTF-8" in r.output
    assert open(stage.record_of_job("4242"), "rb").read() == data
