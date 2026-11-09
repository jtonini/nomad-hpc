# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.2: a memory request of 2 GB or less isn't judged. Asking 1 or 2 GB is
the least anyone usually asks, and what it leaves unused is too little to
matter on a node; [edu] memory_floor_gb changes the floor (0: off)."""
from __future__ import annotations

import pytest

from nomad.edu import scoring as S


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(S, "_FLOOR", {})


def mem(req_mb, peak_gb):
    return S.score_memory({"req_mem_mb": req_mb}, {"peak_memory_gb": peak_gb})


def test_a_small_request_isnt_judged():
    d = mem(2048, 0.09)                                  # job 89550: 2G asked, 92 MB used
    assert d.applicable is False and "too small to judge" in d.detail
    assert mem(1536, 0.05).applicable is False
    assert mem(1024, 0.01).applicable is False           # as before: as little as nomad would suggest


def test_a_larger_one_still_is_and_says_what_was_unused():
    d = mem(4096, 0.1)
    assert d.applicable is not False and d.score < 40 and "3.9GB was unused" in d.detail
    d = mem(8192, 0.5)
    assert "7.5GB was unused" in d.detail
    assert "30GB was unused" in mem(32768, 2).detail
    assert mem(2048, 1.5).score >= 85                    # well sized: scored, as before


def test_the_floor_is_a_setting(monkeypatch):
    import nomad.config as C
    monkeypatch.setattr(C, "load_config", lambda path=None: {"edu": {"memory_floor_gb": 4}})
    assert S.memory_floor_mb() == 4096 and mem(4096, 0.1).applicable is False
    monkeypatch.setattr(S, "_FLOOR", {})
    monkeypatch.setattr(C, "load_config", lambda path=None: {"edu": {"memory_floor_gb": 0}})
    assert mem(2048, 0.09).applicable is not False       # off
    monkeypatch.setattr(S, "_FLOOR", {})
    monkeypatch.setattr(C, "load_config", lambda path=None: {"edu": {"memory_floor_gb": "lots"}})
    assert S.memory_floor_mb() == S.MEMORY_FLOOR_MB      # a wrong value: the default
    monkeypatch.setattr(S, "_FLOOR", {})

    def broken(path=None):
        raise OSError("unreadable")
    monkeypatch.setattr(C, "load_config", broken)
    assert S.memory_floor_mb() == S.MEMORY_FLOOR_MB


def test_a_columbus_practice_run():
    """COLUMBUS's own water example (LGPL-3): the whole folder is the input, as the
    Columbus template takes it, and the script runs runc in the copy."""
    import tomllib

    from nomad.demos import kits as K
    from nomad.submit import templates as TT
    k = K.find({}, "columbus-water")
    assert k.ready and k.input == "." and "control.run" in k.files and k.licence == "open"
    assert "colinp.debug" not in k.files and len(k.files) == 25
    assert open(k.path / "control.run").read().split() == ["niter", "=", "1", "hermit", "scf", "ciudg", "ffield",
                                                          "scfprop", "ciprop"]
    conf = {"submit": {"demos": {"partition": {"spydur": "condos"}}, "templates": [
        {"program": "Columbus", "modules": ["columbus/*"], "default": "columbus/722", "folder": "control.run",
         "run": "$COLUMBUS/runc -m {mem_mb_each} -nproc {cpus} > runls 2>&1", "outputs": ["runls"],
         "parallel": "mpi", "cpus": 4, "mem": "16G", "time": "2-00:00:00"}]}}
    assert K.check(k, conf, "spydur") == []
    text = K.mark(TT.make(conf, "spydur", "Columbus", module="columbus/722", input_path="/home/you/columbus-water",
                          available=["columbus/722"], **K.job_values(k, conf, "spydur"))["text"], k)
    assert "#SBATCH --job-name=practice-columbus-water" in text and "#SBATCH --partition=condos" in text
    assert "cd /home/you/columbus-water" in text and "-nproc ${SLURM_NTASKS:-1}" in text
    assert "#SBATCH --mem=2G" in text and "#SBATCH --ntasks=1" in text
    assert [x["file"] for x in k.look] == ["LISTINGS/scfls.sp", "LISTINGS/ciudgsm.sp",
                                          "LISTINGS/propls.ci.drt1.state1.sp", "runls"]
    tomllib.loads((k.path / "kit.toml").read_text())
