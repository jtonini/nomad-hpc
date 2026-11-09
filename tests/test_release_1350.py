# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.0: practice runs from the Console. The gate lists the kits ready at
a site, copies one into a new or empty folder of the person's, and makes a
new folder of theirs; practice runs size nothing but themselves."""
from __future__ import annotations

import json
import os

import pytest

from nomad.demos import kits as K
from nomad.submit import gate, remote
from nomad.submit.history import PastRun
from nomad.submit.sizing import similar

PERSON = 5123


def _out(capsys):
    lines = [x for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def test_the_requests_are_checked_as_any_other():
    assert gate.parse(["kits", "--user", "jdoe"])[0] == "kits"
    assert gate.parse(["practice", "--user", "jdoe", "--kit", "r-bootstrap", "--dir", "/home/jdoe/p/r"])[1] == {
        "--user": "jdoe", "--kit": "r-bootstrap", "--dir": "/home/jdoe/p/r"}
    for words, why in ((["practice", "--kit", "R Bootstrap", "--dir", "/x"], "a practice kit's name"),
                       (["practice", "--kit", "r-bootstrap"], "needs --dir"),
                       (["practice", "--kit", "r-bootstrap", "--dir", "/home/x/../y"], "without . or .."),
                       (["mkdir"], "needs --dir"),
                       (["kits", "--kits-dir", "relative"], "not a path")):
        with pytest.raises(gate.Refused, match=why):
            gate.parse(words)
    assert {"practice", "mkdir"} <= gate.EDITS and "kits" not in gate.EDITS      # they write: editing on
    assert remote.request("practice", "jdoe", kit="r-bootstrap", dir="/home/jdoe/p", kits_dir="/usr/local/sw/d") == [
        "practice", "--user", "jdoe", "--kit", "r-bootstrap", "--dir", "/home/jdoe/p", "--kits-dir",
        "/usr/local/sw/d"]


@pytest.fixture
def person(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_uid", lambda: PERSON)
    monkeypatch.setattr(gate, "_home", lambda: str(tmp_path))

    def call(*words):
        return gate.run(list(words), str(tmp_path / "bin" / "nomad-gate"))
    return call, tmp_path


def test_the_kits_ready_here(person, capsys, tmp_path):
    call, home = person
    assert call("kits") == 0
    doc = _out(capsys)
    names = {k["name"] for k in doc["kits"]}
    assert doc["ok"] and "r-bootstrap" in names and "gromacs-water" not in names     # not built here
    assert doc["practice"] == K.PRACTICE and all(k["practice"] == K.PRACTICE for k in doc["kits"])
    built = tmp_path / "site" / "gromacs-water"
    built.mkdir(parents=True)
    for f in K.find({}, "gromacs-water").path.iterdir():
        if f.is_file():
            (built / f.name).write_bytes(f.read_bytes())
    (built / "water.tpr").write_bytes(b"tpr")
    assert call("kits", "--kits-dir", str(tmp_path / "site")) == 0
    got = {k["name"]: k for k in _out(capsys)["kits"]}
    assert got["gromacs-water"]["from"] == str(tmp_path / "site") and "from" not in got["r-bootstrap"]
    assert got["gromacs-water"]["recorded_text"].startswith("A run of it on spydur")     # nomad's, for a built kit


def test_a_copy_and_a_new_folder_as_the_person(person, capsys):
    call, home = person
    assert call("mkdir", "--dir", str(home / "practice")) == 0
    assert _out(capsys)["dir"] == str(home / "practice")
    assert call("mkdir", "--dir", str(home / "practice")) == 2
    assert "there already" in _out(capsys)["error"]
    assert call("mkdir", "--dir", str(home / "no" / "such")) == 2
    assert "make it first" in _out(capsys)["error"]
    assert call("practice", "--kit", "r-bootstrap", "--dir", str(home / "practice" / "r")) == 0
    doc = _out(capsys)
    assert doc["ok"] and doc["input"] == str(home / "practice" / "r" / "bootstrap.R") and doc["program"] == "R"
    assert (home / "practice" / "r" / K.README).is_file()
    assert call("practice", "--kit", "r-bootstrap", "--dir", str(home / "practice" / "r")) == 2
    assert "isn't an empty folder" in _out(capsys)["error"]
    assert call("practice", "--kit", "no-such-kit", "--dir", str(home / "practice" / "x")) == 2
    assert "no kit named no-such-kit" in _out(capsys)["error"]
    assert call("practice", "--kit", "gromacs-water", "--dir", str(home / "practice" / "g")) == 2
    assert "isn't built yet" in _out(capsys)["error"]


def test_practice_runs_size_nothing_but_themselves(tmp_path):
    real, practice = tmp_path / "work", tmp_path / "nomad-demos" / "g"
    real.mkdir()
    practice.mkdir(parents=True)
    (practice / K.README).write_text("practice")
    runs = [PastRun(job_id="2", state="COMPLETED", job_name="benzene", workdir=str(practice),
                    script=str(practice / "benzene.sh"), cpus=4, elapsed_s=60),
            PastRun(job_id="1", state="COMPLETED", job_name="benzene", workdir=str(real),
                    script=str(real / "benzene.sh"), cpus=16, elapsed_s=7200)]
    what, got, _ = similar(runs, job_name="benzene", folder=str(tmp_path / "elsewhere"))
    assert [r.job_id for r in got] == ["1"]                                  # the practice run left out
    what, got, strong = similar(runs, script=str(practice / "benzene.sh"), folder=str(practice))
    assert [r.job_id for r in got] == ["2"] and strong                       # but it sizes itself
    assert K.is_practice(str(practice)) and not K.is_practice(str(real)) and not K.is_practice(None)
    import nomad.demos.kits as KK
    asked = []
    real_is = KK.is_practice
    KK.is_practice = lambda w: asked.append(w) or real_is(w)
    try:
        similar(runs * 50, job_name="benzene", folder=str(tmp_path / "elsewhere"))
    finally:
        KK.is_practice = real_is
    assert sorted(asked) == sorted({str(practice), str(real)})                 # each folder looked at once


def test_the_hub_names_each_sites_kits():
    s = K.settings({"submit": {"demos": {"site_dirs": {"spydur": "/usr/local/sw/nomad-demos"}}}})
    assert s.site_dirs == {"spydur": "/usr/local/sw/nomad-demos"} and not s.problems
    bad = K.settings({"submit": {"demos": {"site_dirs": {"spydur": "relative"}}}})
    assert bad.site_dirs == {} and bad.problems


def test_the_recorded_runs_come_with_their_kits():
    got = {k.name: k for k in K.kits({})[0]}
    kept = {"cp2k-water": ["recorded/water.out"], "gromacs-water": ["recorded/water.log"],
            "r-bootstrap": ["recorded/bootstrap-89548.out", "recorded/bootstrap_slopes.pdf"],
            "rfdiffusion-backbone": ["recorded/designs/practice_0.pdb", "recorded/practice-89549.out"],
            "amber-peptide": []}                                       # AMBER: an academic licence, its output not kept
    for name, files in kept.items():
        k = got[name]
        assert k.recorded["site"] == "spydur" and k.recorded["files"] == files, name
        assert k.minutes <= 2 and k.recorded["runtime_s"] < 60 * k.minutes, name
        text = K.recorded_text(k)
        assert text.startswith("A run of it on spydur (2026-11-09") and json.dumps(k.to_dict()), name
        for x in K.recorded_look(k):
            assert x["found"], (name, x["file"])                        # every thing to look at, kept
            for f in x["found"]:
                assert "lines" not in f or f["lines"], (name, x["file"])
        for f in files:                                                # no one's name in what is shipped
            data = (k.path / f).read_bytes()
            assert b"jtonini" not in data and b"/home/" not in data.replace(b"/home/you", b""), (name, f)
    assert "about 3.4 of its 4 core(s) kept busy" in K.recorded_text(got["cp2k-water"])
    assert "1 GPU(s) (NVIDIA A40)" in K.recorded_text(got["gromacs-water"])
    assert "at most 5.0 GB" in K.recorded_text(got["rfdiffusion-backbone"])
    assert "5 min 42 s of CPU time" in K.recorded_text(got["gromacs-water"])
    assert not got["gaussian-benzene"].recorded and K.recorded_text(got["gaussian-benzene"]) == ""


def _kit(tmp_path, recorded):
    d = tmp_path / "k"
    d.mkdir(exist_ok=True)
    (d / "a.R").write_text("1\n")
    (d / "kit.toml").write_text(
        'name = "k-one"\nprogram = "R"\ntitle = "t"\nabout = "a"\nlicence = "open"\nsource = "s"\n'
        'input = "a.R"\nfiles = ["a.R"]\ncpus = 1\nmem = "1G"\ntime = "00:05:00"\nminutes = 1\n'
        "[recorded]\n" + recorded)
    return d


def test_a_recorded_run_is_checked(tmp_path):
    good = 'site = "spydur"\ndate = "2026-11-09"\ncpus = 1\nruntime_s = 3\n'
    k = K.load(_kit(tmp_path, good + 'files = ["recorded/a.out", "recorded/sub/b.out"]\n'))
    assert k.recorded == {"site": "spydur", "date": "2026-11-09", "cpus": 1, "gpus": 0, "runtime_s": 3, "files": []}
    (tmp_path / "k" / "recorded" / "sub").mkdir(parents=True)
    (tmp_path / "k" / "recorded" / "a.out").write_text("x\n")
    os.symlink("/etc/passwd", tmp_path / "k" / "recorded" / "sub" / "b.out")
    assert K.load(tmp_path / "k").recorded["files"] == ["recorded/a.out"]      # a link: left out
    for extra, why in (('date = 2026-11-09\n', "date should be text"),
                       ('cores_busy = 3.0\n', "cores_busy should be"),
                       ('files = ["../etc/passwd"]\n', "recorded/ folder"),
                       ('files = ["recorded/../../x"]\n', "recorded/ folder"),
                       ('job = 12\n', "unknown setting"),
                       ('files = ["recorded/a.out", "recorded/a.out"]\n', "each once")):
        body = good.replace('date = "2026-11-09"\n', "") if extra.startswith("date") else good
        with pytest.raises(K.KitError, match=why):
            K.load(_kit(tmp_path, body + extra))


def test_a_built_kit_keeps_nomads_recorded_run(tmp_path):
    nomads = K.find({}, "gromacs-water")
    site = tmp_path / "site" / "gromacs-water"                        # built by 1.34.1: no [recorded] in it
    site.mkdir(parents=True)
    for f in nomads.path.iterdir():
        if f.is_file():
            text = f.read_bytes()
            if f.name == "kit.toml":
                text = text.split(b"\n[recorded]")[0] + b"\n[build]" + text.split(b"\n[build]")[1]
            (site / f.name).write_bytes(text)
    (site / "water.tpr").write_bytes(b"tpr")
    assert K.load(site).recorded is None
    k = K.find({"submit": {"demos": {"dirs": [str(tmp_path / "site")]}}}, "gromacs-water")
    assert not k.bundled and k.ready and k.recorded == nomads.recorded and k.recorded_from == nomads.path
    toml = (site / "kit.toml").read_text()
    (site / "kit.toml").write_text(toml.replace("cpus = 4", "cpus = 16", 1))     # another size: another run
    assert K.find({"submit": {"demos": {"dirs": [str(tmp_path / "site")]}}}, "gromacs-water").recorded is None
    (site / "kit.toml").write_text(toml)
    assert K.recorded_look(k)[1]["found"][0]["lines"]

    def made(cmd, cwd, **kw):                                          # a build now carries the recorded run along
        (tmp_path / "b" / "gromacs-water" / "water.tpr").write_bytes(b"tpr")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    was = os.umask(0o002)
    try:
        K.build(nomads, tmp_path / "b", run=made)
    finally:
        os.umask(was)
    from nomad.submit import gate as G
    assert G.tree_unsafe(str(tmp_path / "b" / "gromacs-water"), {os.getuid()}) is None       # the gate takes it
    b = K.load(tmp_path / "b" / "gromacs-water")
    assert b.recorded["files"] == ["recorded/water.log"] and (tmp_path / "b" / "gromacs-water" / "recorded"
                                                               / "water.log").is_file()


def test_a_kits_script_is_made_with_its_own_values_and_says_so():
    k = K.find({}, "gromacs-water")
    conf = {"submit": {"demos": {"partition": {"spydur": "condos"}, "gpu_partition": "gpunodes"}}}
    assert K.job_values(k, conf, "spydur") == {"cpus": 4, "mem": k.mem, "time": k.time, "gpus": 1,
                                               "partition": "gpunodes", "choose": dict(k.choose) or None}
    r = K.find({}, "r-bootstrap")
    assert K.job_values(r, conf, "spydur")["partition"] == "condos" and K.job_values(r, {}, "spydur")["partition"] is None
    text = K.mark("#!/bin/bash\n#SBATCH -c 4\nsrun x\n", k)
    assert text.startswith("#!/bin/bash\n# A practice run (nomad's gromacs-water kit)") and "#SBATCH -c 4" in text
    assert K.mark("echo\n", k).startswith("# A practice run")


TEMPLATES = {"submit": {
    "demos": {"partition": {"spydur": "condos"}, "gpu_partition": "gpunodes"},
    "templates": [
        {"program": "R", "modules": ["R/*"], "inputs": [".R"], "run": "Rscript {input}", "cpus": 1, "mem": "8G",
         "time": "1-00:00:00", "options": [{"name": "cores", "label": "Cores", "choices": [
             {"label": "One core"}, {"label": "8 cores", "cpus": 8}]}]},
        {"program": "AMBER", "modules": ["amber/26*"], "inputs": [".in"], "run": "pmemd.cuda -i {input}", "cpus": 1,
         "mem": "8G", "time": "1-00:00:00", "options": [{"name": "engine", "label": "Runs on", "choices": [
             {"label": "One GPU", "gpus": 1, "partition": "gpunodes"}, {"label": "One CPU core", "run": "pmemd -i {input}"}]}]},
        {"program": "GROMACS", "modules": ["gromacs/*"], "inputs": [".tpr"], "run": "gmx mdrun -s {input}", "cpus": 16,
         "mem": "32G", "time": "2-00:00:00", "gpus": 1, "options": [
             {"name": "ranks", "label": "Ranks", "choices": [{"label": "One rank"}, {"label": "Four ranks", "cpus": 32}]},
             {"name": "precision", "label": "Precision", "choices": [{"label": "Mixed"},
                                                                     {"label": "Double", "run": "gmx_d mdrun"}]}]},
    ]}}


def test_the_persons_own_choice_is_sized_as_the_template_sizes_it():
    r, a, g = (K.find({}, n) for n in ("r-bootstrap", "amber-peptide", "gromacs-water"))
    assert K.job_values(r, TEMPLATES, "spydur")["cpus"] == 1
    v = K.job_values(r, TEMPLATES, "spydur", choose={"cores": 2})                     # R's second run: 8 cores
    assert (v["cpus"], v["gpus"], v["choose"], v["mem"], v["time"], v["partition"]) == (
        8, 0, {"cores": 2}, r.mem, r.time, "condos")
    # the "(usual)" first choice, clicked: what the kit has anyway -- its own size
    assert K.job_values(r, TEMPLATES, "spydur", choose={"cores": 1}) == K.job_values(r, TEMPLATES, "spydur")
    assert K.job_values(g, TEMPLATES, "spydur", choose={"ranks": 1})["cpus"] == 4      # not the template's 16
    v = K.job_values(g, TEMPLATES, "spydur", choose={"precision": 2})                # changes neither size
    assert (v["cpus"], v["gpus"], v["choose"], v["partition"]) == (4, 1, {"precision": 2}, "gpunodes")
    assert K.job_values(g, TEMPLATES, "spydur", choose={"ranks": 2})["cpus"] == 32     # what the choice says
    # AMBER on one CPU core: no GPU, and not sent to the GPU nodes
    v = K.job_values(a, TEMPLATES, "spydur", choose={"engine": 2})
    assert (v["cpus"], v["gpus"], v["partition"]) == (1, 0, "condos")
    v = K.job_values(dict(K.kit_values(a), cpus=2), TEMPLATES, "spydur", choose={"engine": 2})
    assert (v["cpus"], v["gpus"]) == (2, 0)                                          # the kit's CPUs stay
    assert (K.job_values(a, TEMPLATES, "spydur")["gpus"], K.job_values(a, TEMPLATES, "spydur")["partition"]) == (
        1, "gpunodes")
    # a CPU kit never takes a GPU template's GPUs
    from nomad.submit import templates as TT
    text = TT.make(TEMPLATES, "spydur", "GROMACS", module="gromacs/2026.2-gpu", input_path="/home/a/w.tpr",
                   available=["gromacs/2026.2-gpu"], **K.job_values(dict(K.kit_values(g), gpus=0), TEMPLATES,
                                                                      "spydur"))["text"]
    assert "--gres=gpu" not in text and "--partition=condos" in text


def test_a_kit_described_by_a_gate_is_checked():
    d = K.find({}, "r-bootstrap").to_dict()
    assert K.kit_values(d) == {"name": "r-bootstrap", "program": "R", "cpus": 1, "mem": d["mem"], "time": d["time"],
                               "gpus": 0, "choose": {}}
    for bad, why in (({"name": "R Boot"}, "name"), ({"cpus": 0}, "cpus"), ({"mem": "lots"}, "mem"),
                     ({"time": "soon"}, "time"), ({"choose": {"x": 9}}, "choose"), ({"program": "a\nb"}, "program")):
        with pytest.raises(K.KitError, match=why):
            K.kit_values({**d, **bad})
    with pytest.raises(K.KitError, match="name"):
        K.mark("#!/bin/bash\n", "x\nrm -rf ~")
    assert K.mark("#!/bin/bash\nx\n", "r-bootstrap").startswith("#!/bin/bash\n# A practice run (nomad's r-bootstrap")


def test_a_copy_follows_no_link(tmp_path, monkeypatch):
    k = K.find({}, "r-bootstrap")
    fake = tmp_path / "kits" / "r-bootstrap"
    fake.mkdir(parents=True)
    for f in k.path.iterdir():
        if f.is_file():
            (fake / f.name).write_bytes(f.read_bytes())
    secret = tmp_path / "secret.txt"
    secret.write_text("mine")
    kit = K.load(fake)
    (fake / "bootstrap.R").unlink()
    os.symlink(secret, fake / "bootstrap.R")                   # swapped after it was checked
    monkeypatch.setattr(K.Kit, "ready", property(lambda self: True))     # (the check, as it was then)
    with pytest.raises(K.KitError, match="isn't a plain file"):
        K.copy(kit, dest=tmp_path / "out")
    assert not (tmp_path / "out" / "bootstrap.R").exists()
    os.symlink(fake, tmp_path / "kits" / "link")
    with pytest.raises(K.KitError, match="can't be read"):
        K.copy(K.Kit(**{**kit.__dict__, "path": tmp_path / "kits" / "link"}), dest=tmp_path / "out2")


def test_no_hidden_folder_and_no_kit_folder_others_can_change(person, capsys, tmp_path):
    call, home = person
    (home / ".bashrc.d").mkdir()
    os.symlink(home / ".bashrc.d", home / "plain")
    for op in (["practice", "--kit", "r-bootstrap"], ["mkdir"]):
        assert call(*op, "--dir", str(home / "plain" / "x")) == 2                     # a link to a hidden one
        assert "not a hidden folder" in _out(capsys)["error"]
        assert call(*op, "--dir", str(home / ".bashrc.d")) == 2
        assert "not a hidden folder" in _out(capsys)["error"]
        assert call(*op, "--dir", str(home / ".config" / "x")) == 2
        _out(capsys)
    site = tmp_path / "site"
    built = site / "gromacs-water"
    built.mkdir(parents=True)
    for f in K.find({}, "gromacs-water").path.iterdir():
        if f.is_file():
            (built / f.name).write_bytes(f.read_bytes())
    (built / "water.tpr").write_bytes(b"tpr")
    os.chmod(built / "water.tpr", 0o666)                        # anyone may change it
    assert call("kits", "--kits-dir", str(site)) == 0
    doc = _out(capsys)
    assert "gromacs-water" not in {k["name"] for k in doc["kits"]}
    assert any("aren't used" in p and "can be changed by others" in p for p in doc["problems"])
    assert call("practice", "--kit", "gromacs-water", "--dir", str(home / "g"), "--kits-dir", str(site)) == 2
    assert "isn't built yet" in _out(capsys)["error"]           # nomad's own, not the site's
