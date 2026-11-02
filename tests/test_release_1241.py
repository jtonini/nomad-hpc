# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.24.1: a staged job's own steps on the node (copying, checking,
Slurm's commands, python3, rsync) run without the job's LD_LIBRARY_PATH
and LD_PRELOAD, which a cluster's login environment can point at libraries
that break system tools; the person's script, and a job it sends through
nomad's sbatch stand-in, get them back as they were."""
import os
import subprocess
import sys

from nomad.submit import stage_helper as h
from tests.test_release_1240 import _job, _run, _shim  # noqa: F401

NOPE = "/nonexistent/libnomadtest.so"


def test_the_script_gets_its_loader_settings_and_nomads_steps_dont(tmp_path):
    script = "#!/bin/bash\necho \"${LD_LIBRARY_PATH-unset}|${LD_PRELOAD-unset}\" > env.txt\n"
    w, dest, out, argv, env = _job(tmp_path, script, env={"LD_LIBRARY_PATH": "/opt/x/lib", "LD_PRELOAD": NOPE})
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "env.txt").read_text() == f"/opt/x/lib|{NOPE}\n"
    assert "copied back 1 new or changed item(s)" in r.stderr
    # the loader complains about the missing library once per program started with it: the
    # wrapper's own bash, and the script's; never nomad's own steps (python, rsync, sed, ...)
    assert r.stderr.count("cannot be preloaded") <= 2, r.stderr


def test_unset_stays_unset(tmp_path):
    script = "#!/bin/bash\necho \"${LD_LIBRARY_PATH-unset}|${LD_PRELOAD-unset}\" > env.txt\n"
    w, dest, out, argv, env = _job(tmp_path, script)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    assert (w / "env.txt").read_text() == "unset|unset\n"


def test_restores():
    env = {"A": "x", "LC_CTYPE": "C.UTF-8"}
    rest = h._restores(["--restore", "LD_LIBRARY_PATH", "1:/opt/lib", "--restore", "LC_CTYPE", "0:",
                        "--restore", "A", "1:", "prog", "--restore"], env)
    assert rest == ["prog", "--restore"] and env == {"A": "", "LD_LIBRARY_PATH": "/opt/lib"}
    env = {}
    assert h._restores(["--lc-ctype", "1:en_US.UTF-8", "p"], env) == ["p"] and env == {"LC_CTYPE": "en_US.UTF-8"}


def test_a_job_sent_through_the_stand_in_keeps_the_loader_settings(tmp_path):
    local = tmp_path / "ls" / "amy" / "job_1"
    submit = tmp_path / "w"
    local.mkdir(parents=True)
    submit.mkdir()
    b = tmp_path / "realbin"
    b.mkdir()
    (b / "sbatch").write_text(f"#!/bin/sh\necho \"$LD_LIBRARY_PATH\" > {tmp_path}/real.env\n")
    (b / "sbatch").chmod(0o755)
    shimdir = local / ".nomad-stage" / "bin"
    shimdir.mkdir(parents=True)
    (submit / "child.sh").write_text("#!/bin/bash\ntrue\n")
    env = {"PATH": f"{shimdir}:{b}:/usr/bin:/bin"}
    r = subprocess.run([sys.executable, "-I", h.__file__, "sbatch-shim", "--restore", "LD_LIBRARY_PATH",
                        "1:/opt/x/lib", str(shimdir), str(local), str(submit), "child.sh"],
                       cwd=submit, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "real.env").read_text() == "/opt/x/lib\n"


def test_the_stand_in_nomad_writes_runs_its_python_without_them(tmp_path):
    w, dest, out, argv, env = _job(tmp_path, "#!/bin/bash\ncat .nomad-stage/bin/sbatch > ../shim.txt 2>/dev/null; true\n")
    r = _run(argv, env, w)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "ls" / "amy" / "shim.txt").read_text() if (tmp_path / "ls" / "amy" / "shim.txt").exists() \
        else open(argv[1]).read()
    assert "/usr/bin/env -u LD_LIBRARY_PATH -u LD_PRELOAD" in text
    assert '--restore LD_LIBRARY_PATH "${LD_LIBRARY_PATH+1}:${LD_LIBRARY_PATH-}"' in text
