# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.34.0: practice runs. Kits of small inputs, known to work, that show a
newcomer how a job runs: checked, copied as the person (never over a file),
built once at a site, and fitted to the site's templates."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.demos import kits as K

NOW = datetime(2026, 11, 10, 14, 5, 0)

# UR's templates, as far as the kits need them (the hub's nomad.toml, 9 Nov 2026)
TEMPLATES = [
    {"program": "Gaussian", "modules": ["gaussian/*"], "inputs": [".gjf", ".com"], "run": "g16 < {input} > {stem}.log"},
    {"program": "Q-Chem", "modules": ["qchem/*"], "inputs": [".in", ".inp"], "run": "qchem -nt {cpus} {input} {stem}.out"},
    {"program": "ORCA", "modules": ["orca/*"], "inputs": [".inp"], "run": "$ORCA/orca {input} > {stem}.out",
     "parallel": "mpi"},
    {"program": "Columbus", "modules": ["columbus/*"], "folder": "control.run",
     "run": "$COLUMBUS/runc -m {mem_mb_each} -nproc {cpus} > runls 2>&1", "parallel": "mpi"},
    {"program": "GROMACS", "modules": ["gromacs/*"], "inputs": [".tpr"], "gpus": 1, "partition": "gpunodes",
     "run": "gmx mdrun -deffnm {stem} -cpi {stem}.cpt -ntomp {threads}"},
    {"program": "R", "modules": ["R/*"], "inputs": [".R", ".r"], "run": "Rscript {input}",
     "options": [{"name": "cores", "label": "Cores", "choices": [{"label": "One core"},
                                                                  {"label": "8 cores", "cpus": 8}]}]},
    {"program": "Python", "modules": ["python/*"], "inputs": [".py"], "run": "python3 -u {input}"},
    {"program": "CP2K", "modules": ["cp2k/*"], "inputs": [".inp"], "parallel": "mpi",
     "run": "mpirun -np {tasks} cp2k.psmp -i {input} -o {stem}.out"},
    {"program": "AMBER", "modules": ["amber/26*"], "inputs": [".in", ".mdin"],
     "run": "pmemd.cuda -O -i {input} -o {stem}.out",
     "options": [{"name": "engine", "label": "Runs on", "choices": [{"label": "One GPU", "gpus": 1},
                                                                    {"label": "One CPU core"}]}]},
    {"program": "NAMD", "modules": ["namd/2.11"], "inputs": [".namd", ".conf"], "run": "namd2 +p{cpus} {input} > {stem}.log"},
    {"program": "NAMD GPU", "modules": ["namd/2.11_cuda"], "inputs": [".namd", ".conf"], "gpus": 1,
     "run": "namd2 +p{cpus} +idlepoll {input} > {stem}.log"},
    {"program": "Molcas", "modules": ["molcas/*"], "inputs": [".input", ".inp"], "run": "molcas {input} > {stem}.log 2>&1"},
    {"program": "RFdiffusion", "modules": ["rfdiffusion/*"], "inputs": [".yaml"], "gpus": 1,
     "run": "run_inference.py --config-dir \"$PWD\" --config-name {stem}"},
]
CONFIG = {"submit": {"templates": TEMPLATES}}


def _kit(folder: Path, **over) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "water.gjf").write_text("%NProcShared=1\n#P HF/STO-3G\n\nwater\n\n0 1\nO 0 0 0\nH 0 0 1\nH 0 1 0\n\n")
    raw = {"name": folder.name, "program": "Gaussian", "title": "Water", "about": "Practice.",
           "licence": "licensed", "source": "test", "input": "water.gjf", "files": ["water.gjf"], "cpus": 1,
           "mem": "1G", "time": "00:05:00", "minutes": 1}
    raw.update(over)
    lines = []
    for k, v in raw.items():
        if isinstance(v, str):
            lines.append(f"{k} = {json.dumps(v)}")
        elif isinstance(v, list):
            lines.append(f"{k} = [" + ", ".join(f'"{x}"' for x in v) + "]")
        elif isinstance(v, dict):
            lines.append(f"{k} = {{ " + ", ".join(f"{a} = {b}" for a, b in v.items()) + " }")
        else:
            lines.append(f"{k} = {v}")
    (folder / "kit.toml").write_text("\n".join(lines) + "\n")
    return folder


def test_every_bundled_kit_reads_and_fits_ur_templates():
    got, problems = K.kits(CONFIG)
    assert not problems
    names = {k.name for k in got}
    assert {"gaussian-benzene", "qchem-benzene", "orca-benzene", "molcas-water", "r-bootstrap", "cp2k-water",
            "namd-water", "namd-gpu-water", "rfdiffusion-backbone", "python-montecarlo", "gromacs-water",
            "amber-peptide"} <= names
    for k in got:
        assert k.bundled and k.licence in K.LICENCES
        assert sum((k.path / f).stat().st_size for f in k.files if (k.path / f).exists()) <= K.MAX_BYTES
        why = K.check(k, CONFIG, "spydur")
        if k.build:
            assert not k.ready and why == [f"not built yet: nomad demos build {k.name}"]
        else:
            assert k.ready and why == [], (k.name, why)
    lic = {k.name: k.licence for k in got}
    assert lic["gaussian-benzene"] == "licensed" and lic["orca-benzene"] == "academic" and lic["r-bootstrap"] == "open"


def test_a_kit_that_doesnt_fit_its_template_is_said():
    k = K.find(CONFIG, "gaussian-benzene")
    cfg = {"submit": {"templates": [dict(TEMPLATES[0], inputs=[".com"])]}}
    assert "takes a file ending in .com" in K.check(k, cfg, "spydur")[0]
    assert K.check(k, {"submit": {"templates": []}}, "spydur") == ["spydur has no Gaussian template"]
    amber = K.find(CONFIG, "amber-peptide")
    assert amber.gpus == 1 and not any("GPUs" in w for w in K.check(amber, CONFIG, "spydur"))


def test_kits_are_checked_as_they_are_read(tmp_path):
    with pytest.raises(K.KitError, match="licence"):
        K.load(_kit(tmp_path / "a-kit", licence="free"))
    with pytest.raises(K.KitError, match="input"):
        K.load(_kit(tmp_path / "b-kit", input="other.gjf"))
    with pytest.raises(K.KitError, match="missing"):
        K.load(_kit(tmp_path / "c-kit", files=["water.gjf", "gone.txt"]))
    with pytest.raises(K.KitError, match="unknown"):
        K.load(_kit(tmp_path / "d-kit", colour="red"))
    with pytest.raises(K.KitError, match="no folders"):
        K.load(_kit(tmp_path / "e-kit", files=["../water.gjf"]))
    with pytest.raises(K.KitError, match="may not hold"):
        K.load(_kit(tmp_path / "f-kit", files=["water.gjf", "README-practice.txt"]))
    big = _kit(tmp_path / "g-kit")
    (big / "water.gjf").write_bytes(b"x" * (K.MAX_BYTES + 1))
    with pytest.raises(K.KitError, match="bytes"):
        K.load(big)
    link = _kit(tmp_path / "h-kit")
    (link / "water.gjf").unlink()
    os.symlink("/etc/passwd", link / "water.gjf")
    with pytest.raises(K.KitError, match="missing"):
        K.load(link)
    assert K.load(_kit(tmp_path / "i-kit", choose={"cores": 2})).choose == {"cores": 2}


def test_a_copy_goes_into_a_new_folder_and_writes_over_nothing(tmp_path):
    k = K.find(CONFIG, "r-bootstrap")
    got = K.copy(k, into=str(tmp_path / "demos"), now=NOW)
    folder = Path(got["folder"])
    assert folder == tmp_path / "demos" / "r-bootstrap-20261110-140500"
    assert got["input"] == str(folder / "bootstrap.R")
    assert sorted(os.listdir(folder)) == ["README-practice.txt", "bootstrap.R"]
    readme = (folder / "README-practice.txt").read_text()
    assert K.PRACTICE in readme and "Open source" in readme and "bootstrap-*.out" in readme
    with pytest.raises(K.KitError, match="isn't an empty folder"):
        K.copy(k, dest=folder)
    empty = tmp_path / "mine"
    empty.mkdir()
    assert K.copy(k, dest=empty)["folder"] == str(empty)
    with pytest.raises(K.KitError, match="build"):
        K.copy(K.find(CONFIG, "gromacs-water"), into=str(tmp_path / "demos"))
    folder_kit = K.find(CONFIG, "namd-water")
    got = K.copy(folder_kit, dest=tmp_path / "namd")
    assert {"water.namd", "water.psf", "water.pdb", "par_water.prm"} <= set(os.listdir(tmp_path / "namd"))


def test_settings_and_a_sites_own_kits(tmp_path):
    s = K.settings({"submit": {"demos": {"dirs": "relative", "bundled": "yes", "into": "nowhere", "x": 1}}})
    assert s.dirs == () and s.bundled is True and s.into == K.DEFAULT_INTO and len(s.problems) == 4
    site = tmp_path / "site"
    _kit(site / "r-bootstrap", program="R", input="water.gjf", title="ARC's own")
    _kit(site / "broken", licence="maybe")
    cfg = {"submit": {"templates": TEMPLATES, "demos": {"dirs": [str(site), str(tmp_path / "gone")]}}}
    got, problems = K.kits(cfg)
    mine = next(k for k in got if k.name == "r-bootstrap")
    assert mine.title == "ARC's own" and not mine.bundled                    # the site's replaces nomad's
    assert any("broken" in p for p in problems) and any("isn't a folder" in p for p in problems)
    only_site, _ = K.kits({"submit": {"demos": {"dirs": [str(site)], "bundled": False}}})
    assert [k.name for k in only_site] == ["r-bootstrap"]


def test_building_a_kit_once_at_a_site(tmp_path):
    k = K.find(CONFIG, "gromacs-water")
    seen = {}

    def run(argv, cwd, capture_output, text, timeout):
        seen["script"] = Path(cwd, "build.sh").read_text()
        Path(cwd, "water.tpr").write_bytes(b"tpr")
        return subprocess.CompletedProcess(argv, 0, "made\n", "")
    cfg = {"submit": {"templates": TEMPLATES, "build": {"spydur": {"module_paths": ["/usr/local/sw/modulefiles"]}}}}
    got = K.build(k, tmp_path / "kits", config=cfg, site="spydur", run=run)
    assert got["made"] == ["water.tpr"]
    assert "module use /usr/local/sw/modulefiles\nmodule load gromacs/2026.2-gpu\n" in seen["script"]
    assert "gmx grompp -f md.mdp -c em.gro -p topol.top -o water.tpr" in seen["script"]
    built = K.load(tmp_path / "kits" / "gromacs-water")
    assert built.ready and K.check(built, CONFIG, "spydur") == []
    with pytest.raises(K.KitError, match="there already"):
        K.build(k, tmp_path / "kits", run=run)

    def fails(argv, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 1, "", "tleap: not found\n")
    with pytest.raises(K.KitError, match="failed.*not made: peptide.prmtop"):
        K.build(K.find(CONFIG, "amber-peptide"), tmp_path / "other", run=fails)
    assert "tleap: not found" in (tmp_path / "other" / "amber-peptide" / "build.log").read_text()
    with pytest.raises(K.KitError, match="nothing to build"):
        K.build(K.find(CONFIG, "r-bootstrap"), tmp_path / "x", run=run)


def test_the_commands(tmp_path, monkeypatch):
    from nomad.cli import cli
    from nomad.demos import cli as C
    cfg = {"submit": {"templates": TEMPLATES, "demos": {"into": str(tmp_path / "demos")}}}
    monkeypatch.setattr(C, "_config", lambda ctx: cfg)
    run = CliRunner()
    r = run.invoke(cli, ["demos", "list", "--site", "spydur"])
    assert r.exit_code == 0, r.output
    assert "gaussian-benzene" in r.output and "spydur: fits its template" in r.output and K.PRACTICE in r.output
    r = run.invoke(cli, ["demos", "show", "orca-benzene"])
    assert "Free for academic use" in r.output and "%pal" in r.output
    r = run.invoke(cli, ["demos", "copy", "molcas-water"])
    assert r.exit_code == 0 and "Copied molcas-water to " + str(tmp_path / "demos") in r.output
    r = run.invoke(cli, ["demos", "check", "--site", "spydur"])
    assert r.exit_code == 1 and "!! gromacs-water" in r.output and "ok gaussian-benzene" in r.output
    r = run.invoke(cli, ["demos", "show", "nothing"])
    assert r.exit_code != 0 and "no kit named nothing" in r.output


def test_what_to_look_at_once_it_has_run(tmp_path):
    k = K.find(CONFIG, "gaussian-benzene")
    folder = Path(K.copy(k, dest=tmp_path / "g")["folder"])
    got = K.look(k, folder)
    assert [x["found"] for x in got] == [[], [], []]                         # not run yet
    (folder / "benzene.log").write_text(" SCF Done:  E(RB3LYP) =  -232.2 \n" * 9
                                         + " Frequencies --    410.1   410.1   623.0\n"
                                         + " Job cpu time:       0 days  0 hours  4 minutes\n"
                                         + " Elapsed time:       0 days  0 hours  1 minutes\n")
    got = K.look(k, folder, last=6)
    assert len(got[0]["found"][0]["lines"]) == 6 and "Frequencies" in got[1]["found"][0]["lines"][0]
    assert len(got[2]["found"][0]["lines"]) == 2
    r = K.find(CONFIG, "rfdiffusion-backbone")
    folder = tmp_path / "rf"
    K.copy(r, dest=folder)
    (folder / "designs").mkdir()
    (folder / "designs" / "practice_0.pdb").write_text("ATOM\n")
    (folder / "practice-881.out").write_text("Finished design in 1.20 minutes\n")
    got = K.look(r, folder)
    assert got[0]["found"][0]["size"] == 5 and got[1]["found"][0]["lines"] == ["Finished design in 1.20 minutes"]


def test_staffs_scripts_for_every_kit(tmp_path, monkeypatch):
    import sqlite3
    from nomad.cli import cli
    from nomad.demos import cli as C
    db = tmp_path / "combined.db"
    sqlite3.connect(db).close()
    cfg = {"submit": {"templates": TEMPLATES}, "hub": {"output_db": str(db)}}
    monkeypatch.setattr(C, "_config", lambda ctx: cfg)
    monkeypatch.setattr("nomad.collectors.modules.available",
                        lambda conn, site=None: ["gaussian/g16", "R/4.4.2", "amber/26", "cp2k/2026.2"])
    r = CliRunner().invoke(cli, ["demos", "scripts", "--site", "spydur", "--hub", "--root", "/home/me/practice",
                                 "--out", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    text = (tmp_path / "out" / "gaussian-benzene.sh").read_text()
    assert "cd /home/me/practice/gaussian-benzene" in text and "--cpus-per-task=4" in text and "--mem=8G" in text
    amber = (tmp_path / "out" / "amber-peptide.sh").read_text()
    assert "--gres=gpu:1" in amber and "not built yet" in r.output
    assert "ORCA" in r.output and not (tmp_path / "out" / "orca-benzene.sh").exists()   # no orca module there
    r = CliRunner().invoke(cli, ["demos", "scripts", "--site", "spydur", "--hub", "--root", "/home/me/practice",
                                 "--out", str(tmp_path / "out")])
    assert "not written over" in r.output


def test_copy_from_a_sites_built_kits(tmp_path, monkeypatch):
    from nomad.cli import cli
    from nomad.demos import cli as C
    built = tmp_path / "built" / "gromacs-water"
    built.mkdir(parents=True)
    for f in K.find(CONFIG, "gromacs-water").path.iterdir():
        (built / f.name).write_bytes(f.read_bytes())
    (built / "water.tpr").write_bytes(b"tpr")
    monkeypatch.setattr(C, "_config", lambda ctx: {"submit": {"templates": TEMPLATES}})
    run = CliRunner()
    r = run.invoke(cli, ["demos", "copy", "gromacs-water", "--to", str(tmp_path / "a")])
    assert r.exit_code != 0 and "isn't built yet" in r.output
    r = run.invoke(cli, ["demos", "copy", "gromacs-water", "--from", str(tmp_path / "built"), "--to", str(tmp_path / "b")])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "b" / "water.tpr").read_bytes() == b"tpr"


def test_where_practice_runs_are_sent(tmp_path, monkeypatch):
    s = K.settings({"submit": {"demos": {"partition": {"spydur": "condos"}, "gpu_partition": "gpunodes"}}})
    assert s.partition_for("spydur", 0) == "condos" and s.partition_for("arachne", 0) is None
    assert s.partition_for("spydur", 1) == "gpunodes" and s.partition_for("arachne", 1) == "gpunodes"
    bad = K.settings({"submit": {"demos": {"partition": "no spaces"}}})
    assert bad.partition == {} and bad.problems
    import sqlite3
    from nomad.cli import cli
    from nomad.demos import cli as C
    db = tmp_path / "combined.db"
    sqlite3.connect(db).close()
    cfg = {"submit": {"templates": TEMPLATES, "demos": {"partition": {"spydur": "condos"}}},
           "hub": {"output_db": str(db)}}
    monkeypatch.setattr(C, "_config", lambda ctx: cfg)
    monkeypatch.setattr("nomad.collectors.modules.available",
                        lambda conn, site=None: ["R/4.4.2", "gromacs/2026.2-gpu", "rfdiffusion/latest"])
    r = CliRunner().invoke(cli, ["demos", "scripts", "--site", "spydur", "--hub", "--root", "/home/me/p",
                                 "--out", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "--partition=condos" in (tmp_path / "out" / "r-bootstrap.sh").read_text()
    gpu = (tmp_path / "out" / "rfdiffusion-backbone.sh").read_text()
    assert "--partition=condos" not in gpu and "--gres=gpu:1" in gpu
