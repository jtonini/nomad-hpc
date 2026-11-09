# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.1: a practice run is known for what it is. Its job is named
practice-KIT; the hub tells one from its records (a folder named after a
kit, and that name or the kit's own job name); and the gate's answer for one
of the person's jobs says, for a practice run, what to look at in its folder
now (that job's own files first), beside the kit's recorded run."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from nomad.demos import kits as K
from nomad.submit import gate

PERSON = 5123


def test_a_practice_runs_job_is_named_for_its_kit():
    text = "#!/bin/bash\n#SBATCH --job-name=bootstrap\n#SBATCH --cpus-per-task=1\nRscript bootstrap.R\n"
    got = K.mark(text, "r-bootstrap")
    assert "#SBATCH --job-name=practice-r-bootstrap\n" in got and "job-name=bootstrap" not in got
    assert got.startswith("#!/bin/bash\n# A practice run (nomad's r-bootstrap kit)")
    assert "#SBATCH -J practice-cp2k-water" in K.mark("#!/bin/bash\n#SBATCH -J water\nx\n", "cp2k-water")
    plain = K.mark("#!/bin/bash\n#SBATCH -c 1\nx\n", "r-bootstrap")              # no name of its own: given one
    assert plain.splitlines()[3] == "#SBATCH --job-name=practice-r-bootstrap" and plain.count("job-name") == 1
    # only the first: a later command's words are its own
    twice = K.mark("#!/bin/bash\n#SBATCH --job-name=a\necho '#SBATCH --job-name=b'\n", "r-bootstrap")
    assert "practice-r-bootstrap" in twice and "--job-name=b'" in twice


def test_the_hub_tells_a_practice_run_from_its_records():
    names = K.practice_names()
    assert names["r-bootstrap"] == "bootstrap" and names["cp2k-water"] == names["gromacs-water"] == "water"
    label = K.practice_label
    assert label("practice-r-bootstrap", "jtonini/r-bootstrap-2", names) == "r-bootstrap"     # 1.35.1's name
    assert label("practice-lab-fit", "amy/lab-fit", names) == "lab-fit"           # a site's own kit, unknown here
    # before 1.35.1: the kit's own job name, in a folder named after the kit
    assert label("bootstrap", "jtonini/r-bootstrap", names) == "r-bootstrap"
    assert label("bootstrap", "nomad-demos/r-bootstrap-20261109-131500", names) == "r-bootstrap"
    assert label("water", "jtonini/gromacs-water-2", names) == "gromacs-water"            # the folder decides
    assert label("water", "nomad-practice/cp2k-water", names) == "cp2k-water"
    assert label("practice", "nomad-practice/rfdiffusion-backbone", names) == "rfdiffusion-backbone"
    # real work is never one: another name, or a folder of its own
    for job, tail in (("bootstrap", "thesis/chapter2"), ("water", "jtonini/water-runs"), ("fit", "x/r-bootstrap"),
                      ("practice", "class/hw1"), (None, None), ("water", None),
                      ("practice-r-bootstrap", "thesis/chapter2"),       # a practice script copied into real work
                      ("practice-no-such-kit", "x/y"), ("water", "x/cp2k-water-run")):
        assert label(job, tail, names) is None, (job, tail)


def test_a_copy_says_its_kit(tmp_path):
    k = K.find({}, "r-bootstrap")
    got = K.copy(k, dest=tmp_path / "r")
    readme = (tmp_path / "r" / K.README).read_text()
    assert "\nKit: r-bootstrap\n(nomad demos show r-bootstrap" in readme and "Kit folder" not in readme
    assert K.kit_of(got["folder"]).name == "r-bootstrap"
    old = tmp_path / "old"                                   # a 1.35.0 copy: its title says
    old.mkdir()
    (old / K.README).write_text(readme.replace("Kit: r-bootstrap", "Something else"))
    assert K.kit_of(old).name == "r-bootstrap"
    (old / K.README).write_text("Not a kit's title\n")
    assert K.kit_of(old) is None and K.kit_of(tmp_path / "nowhere") is None
    lnk = tmp_path / "l"
    lnk.mkdir()
    os.symlink(tmp_path / "r" / K.README, lnk / K.README)
    assert K.kit_of(lnk) is None                              # a link isn't read


def test_what_to_look_at_reads_the_end_of_big_files(tmp_path, monkeypatch):
    k = K.find({}, "r-bootstrap")
    K.copy(k, dest=tmp_path / "r")
    out = tmp_path / "r" / "bootstrap-1.out"
    out.write_text("Fitted slope: 0.1\n" + "x" * 1000 + "\n" + "Fitted slope: 0.803 " + "y" * 900 + "\n")
    monkeypatch.setattr(K, "LOOK_BYTES", 1200)
    (entry,) = [x for x in K.look(k, tmp_path / "r") if x["file"] == "bootstrap-*.out"]
    (f,) = entry["found"]
    assert f["lines"] == [("Fitted slope: 0.803 " + "y" * 900)[:K.LOOK_LINE]]     # the first one isn't read
    for i in range(10):
        (tmp_path / "r" / f"bootstrap-{i + 10}.out").write_text("Fitted slope: 1\n")
    (entry,) = [x for x in K.look(k, tmp_path / "r") if x["file"] == "bootstrap-*.out"]
    assert len(entry["found"]) == K.LOOK_FOUND


def test_a_practice_jobs_folder_described(tmp_path):
    assert K.practice_of(None) is None and K.practice_of(str(tmp_path)) is None     # not a copy
    k = K.find({}, "cp2k-water")
    K.copy(k, dest=tmp_path / "w")
    (tmp_path / "w" / "water.out").write_text(" ENERGY| Total FORCE_EVAL ( QS ) energy [hartree]   -17.2193\n")
    got = K.practice_of(str(tmp_path / "w"))
    assert got["kit"] == "cp2k-water" and got["recorded_text"].startswith("A run of it on spydur")
    first = got["look"][0]
    assert first["found"][0]["lines"] == [" ENERGY| Total FORCE_EVAL ( QS ) energy [hartree]   -17.2193"]
    rec = got["recorded"][0]["found"][0]
    assert rec["path"] == "recorded/water.out" and rec["lines"][-1].strip().endswith("-17.219308551645923")
    json.dumps(got)
    (tmp_path / "w" / K.README).write_text("hand-edited\n")                     # a copy whose kit can't be told
    assert K.practice_of(str(tmp_path / "w")) == {"folder": str(tmp_path / "w"), "practice": K.PRACTICE, "kit": None}


@pytest.fixture
def person(monkeypatch, tmp_path):
    if os.getuid() != 0:
        pytest.skip("needs to own files as another uid")
    monkeypatch.setattr(gate, "_uid", lambda: PERSON)
    monkeypatch.setattr(gate, "_account", lambda: "jdoe")
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))
    return tmp_path


def _slurm(workdir):
    def run_(argv, **kw):
        if argv[7] == "scontrol":
            return subprocess.CompletedProcess(argv, 1, b"Invalid job id\n")
        if "-X" in argv:
            return subprocess.CompletedProcess(argv, 0, gate.SEP.join(
                ["bootstrap-%j.out", "bootstrap-%j.out", "practice-r-bootstrap", workdir, "77", "77"]).encode() + b"\n")
        return subprocess.CompletedProcess(argv, 0, gate.SEP.join(
            ["77", "practice-r-bootstrap", "jdoe", "COMPLETED", "0:0", "a", "b", "c", "00:00:29", "00:20:00", "1",
             "n1", "1", "2G", "", "00:00:28", "cpu=1", workdir]).encode() + b"\n")
    return run_


def test_the_gates_answer_for_a_practice_job_says_what_to_look_at(person, capsys):
    K.copy(K.find({}, "r-bootstrap"), dest=person / "r")
    (person / "r" / "bootstrap-77.out").write_text("Fitted slope: 0.803 (the made-up data used 0.8)\n"
                                                   "20000 bootstrap fits on 1 core(s) in 28.2 s\n")
    assert gate.run(["job", "--job", "77"], str(person / "bin" / "nomad-gate"), run_=_slurm(str(person / "r"))) == 0
    doc = json.loads(capsys.readouterr().out)
    p = doc["practice"]
    assert doc["ok"] and p["kit"] == "r-bootstrap" and p["folder"] == str(person / "r")
    out = next(x for x in p["look"] if x["file"] == "bootstrap-*.out")
    assert out["found"][0]["lines"][-1] == "20000 bootstrap fits on 1 core(s) in 28.2 s"
    assert p["recorded"][0]["found"][0]["path"].startswith("recorded/") and "practice" in p
    # a job that isn't a practice run: as before
    (person / "real").mkdir()
    assert gate.run(["job", "--job", "77"], str(person / "bin" / "nomad-gate"), run_=_slurm(str(person / "real"))) == 0
    assert json.loads(capsys.readouterr().out)["practice"] is None             # this gate can tell: not one


def test_a_jobs_own_files_first_and_one_named_file_however_full_the_folder(tmp_path, monkeypatch):
    k = K.find({}, "r-bootstrap")
    K.copy(k, dest=tmp_path / "r")
    for n in range(89001, 89008):                                     # seven runs in one folder
        (tmp_path / "r" / f"bootstrap-{n}.out").write_text(f"Fitted slope: {n}\n")
    entry = lambda job: next(x for x in K.look(k, tmp_path / "r", job=job) if x["file"] == "bootstrap-*.out")
    (own,) = entry("89007")["found"]
    assert own["path"].endswith("bootstrap-89007.out") and own["lines"] == ["Fitted slope: 89007"]
    os.utime(tmp_path / "r" / "bootstrap-89003.out", (2e9, 2e9))      # without a job: the newest first
    assert entry(None)["found"][0]["path"].endswith("bootstrap-89003.out") and len(entry(None)["found"]) == K.LOOK_FOUND
    assert entry("123")["found"][0]["path"].endswith("bootstrap-89003.out")   # none of its own: the newest
    c = K.find({}, "cp2k-water")
    K.copy(c, dest=tmp_path / "w")
    monkeypatch.setattr(K, "LOOK_ENTRIES", 5)
    for i in range(20):
        (tmp_path / "w" / f"a{i:02d}.txt").write_text("x")
    (tmp_path / "w" / "water.out").write_text(" ENERGY| Total FORCE_EVAL ( QS ) energy [hartree]   -1\n")
    assert K.look(c, tmp_path / "w")[0]["found"][0]["lines"]                  # not lost among the entries
    os.mkfifo(tmp_path / "r" / "bootstrap-89999.out")                         # never opened: it would hang
    assert not any("89999" in f["path"] for f in entry("89999")["found"])


def test_a_site_kits_copy_is_known_by_the_folder_it_names(tmp_path, person, capsys):
    site = tmp_path / "site"
    kit = site / "lab-fit"
    kit.mkdir(parents=True)
    r = K.find({}, "r-bootstrap")
    for f in r.path.iterdir():
        if f.is_file():
            text = f.read_bytes()
            if f.name == "kit.toml":
                text = text.replace(b'name = "r-bootstrap"', b'name = "lab-fit"').split(b"\n[recorded]")[0] + b"\n"
            (kit / f.name).write_bytes(text)
    k = K.find({"submit": {"demos": {"dirs": [str(site)]}}}, "lab-fit")
    got = K.copy(k, dest=person / "lab-fit")
    readme = (person / "lab-fit" / K.README).read_text()
    assert f"Kit folder: {site}\n" in readme and K.kit_source(got["folder"])[:2] == ("lab-fit", str(site))
    (person / "lab-fit" / "bootstrap-77.out").write_text("Fitted slope: 0.5\n")
    assert gate.run(["job", "--job", "77"], str(person / "bin" / "nomad-gate"),
                    run_=_slurm(str(person / "lab-fit"))) == 0
    p = json.loads(capsys.readouterr().out)["practice"]
    assert p["kit"] == "lab-fit" and p["look"][0]["found"][0]["lines"] == ["Fitted slope: 0.5"]
    os.chmod(kit / "bootstrap.R", 0o666)                                       # a folder others could change: not used
    assert gate.run(["job", "--job", "77"], str(person / "bin" / "nomad-gate"),
                    run_=_slurm(str(person / "lab-fit"))) == 0
    p = json.loads(capsys.readouterr().out)["practice"]
    assert p["kit"] is None and "isn't used" in p["error"] and "can be changed by others" in p["error"]
    # a README naming another folder (the person may write anything there): only that kit's own folder is looked at
    readme = person / "lab-fit" / K.README
    readme.write_text(readme.read_text().replace(f"Kit folder: {site}", "Kit folder: /usr"))
    assert gate.run(["job", "--job", "77"], str(person / "bin" / "nomad-gate"),
                    run_=_slurm(str(person / "lab-fit"))) == 0
    assert "isn't in /usr here" in json.loads(capsys.readouterr().out)["practice"]["error"]


def test_scores_and_advice_leave_practice_runs_out(tmp_path):
    import sqlite3
    from datetime import datetime, timedelta

    from nomad.edu import population, progress
    db = tmp_path / "c.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, source_site TEXT, user_name TEXT, job_name TEXT, state TEXT, "
              "partition TEXT, submit_time TEXT, start_time TEXT, end_time TEXT, runtime_seconds INTEGER, "
              "req_cpus INTEGER, req_mem_mb INTEGER, req_time_seconds INTEGER, work_root TEXT, work_tail TEXT)")
    c.execute("CREATE TABLE job_summary (job_id TEXT, source_site TEXT, avg_cpu_percent REAL, peak_memory_gb REAL)")
    t = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    for jid, name, tail in (("1", "practice-r-bootstrap", "amy/r-bootstrap"), ("2", "bootstrap", "nomad-practice/r-bootstrap"),
                            ("3", "fit", "thesis/chapter2")):
        c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (jid, "spydur", "amy", name, "COMPLETED", "condos", t, t, t, 60, 1, 2048, 600, "/home", tail))
    c.commit()
    c.close()
    assert [r["job_id"] for r in progress._load_user_jobs(str(db), "amy", days=30)] == ["3"]
    pop = population.population(str(db), days=30)
    assert pop.people["amy"].jobs == 1
