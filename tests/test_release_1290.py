"""1.29.0: a template's options. A few choices a template offers (how it runs
in parallel, what step of a workflow), the first chosen unless the person
picks another; each changes only what it names."""
import pytest

from nomad.submit import templates as T

CP2K = {
    "program": "CP2K", "modules": ["cp2k/*"], "inputs": [".inp"], "cpus": 8, "mem": "16G", "time": "1-00:00:00",
    "run": "srun cp2k.psmp -i {input} -o {stem}.out", "outputs": ["{stem}.out"], "parallel": "mpi",
    "options": [
        {"name": "parallel", "label": "Runs as", "choices": [
            {"label": "MPI processes, one a core", "parallel": "mpi", "omp": True},
            {"label": "MPI processes of 2 threads each", "parallel": "hybrid", "threads_per_task": 2, "omp": True},
        ]},
        {"name": "restart", "label": "If it is stopped", "choices": [
            {"label": "Starts over"},
            {"label": "Goes on from its restart file", "setup": ["[ -s {stem}-1.restart ] && echo restart"],
             "outputs": ["{stem}-1.restart"], "time": "3-00:00:00"},
        ]},
    ]}


def test_the_first_choices_unless_others_are_picked():
    t = T.template(CP2K)
    text = T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", when=None)
    assert "#SBATCH --ntasks=8" in text and "#SBATCH --nodes=1" in text and "--cpus-per-task" not in text
    assert "export OMP_NUM_THREADS=1" in text and "SRUN_CPUS_PER_TASK" not in text
    assert "# Runs as: MPI processes, one a core" in text and "# If it is stopped: Starts over" in text
    assert "#SBATCH --open-mode=append" in text and "#SBATCH --time=1-00:00:00" in text
    hy = T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", choose={"parallel": 2, "restart": "2"})
    assert "#SBATCH --ntasks=4" in hy and "#SBATCH --cpus-per-task=2" in hy
    assert "export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-2}" in hy and "export SRUN_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-2}" in hy     # srun's steps get both
    assert "[ -s h2o-1.restart ] && echo restart" in hy and "#SBATCH --time=3-00:00:00" in hy
    # the person's own size wins over a choice's
    mine = T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", choose={"restart": 2}, time="12:00:00")
    assert "#SBATCH --time=12:00:00" in mine
    assert T.outputs(t, "/home/amy/h2o.inp", {"restart": "goes on from its restart file"}) == \
        ["h2o.out", "h2o-1.restart"]
    with pytest.raises(T.TemplateError, match="multiple of 2"):
        T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", choose={"parallel": 2}, cpus=7)
    with pytest.raises(T.TemplateError, match="isn't one of its choices"):
        T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", choose={"parallel": 3})
    with pytest.raises(T.TemplateError, match="no option"):
        T.render(t, module="cp2k/2024.1", input_path="/home/amy/h2o.inp", choose={"nodes": 2})


def test_threads_with_srun_get_their_cpus_and_old_templates_are_as_they_were():
    py = T.template({"program": "Python", "modules": ["python/*"], "inputs": [".py"], "cpus": 4, "omp": True,
                     "run": "srun python3 {input}"})
    text = T.render(py, module="python/3.12", input_path="/home/amy/fit.py")
    assert "#SBATCH --cpus-per-task=4" in text and "export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-4}" in text
    assert "export SRUN_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-4}" in text
    g = T.template({"program": "Gaussian", "modules": ["gaussian/*"], "inputs": [".gjf"], "cpus": 8,
                    "run": "g16 < {input} > {stem}.log"})
    text = T.render(g, module="gaussian/g16c02", input_path="/home/amy/water.gjf")
    assert "OMP_NUM_THREADS" not in text and "SRUN_CPUS_PER_TASK" not in text and "# Change anything" in text
    assert T.template(CP2K).to_dict("spydur")["options"][1]["choices"][1] == {
        "label": "Goes on from its restart file", "time": "3-00:00:00"}


@pytest.mark.parametrize("change, said", [
    ({"options": [{"name": "a", "choices": [{"label": "x"}]}]}, "two to 6 choices"),
    ({"options": [{"name": "Bad name", "choices": [{"label": "x"}, {"label": "y"}]}]}, "short key"),
    ({"options": [{"name": "a", "choices": [{"label": "x"}, {"label": "x"}]}]}, "each choice its own"),
    ({"options": [{"name": "a", "choices": [{"label": "x", "colour": "red"}, {"label": "y"}]}]},
     "colour isn't something a choice sets"),
    ({"options": [{"name": "a", "choices": [{"label": "x", "run": "a"}, {"label": "y"}]},
                  {"name": "b", "choices": [{"label": "x", "run": "b"}, {"label": "y"}]}]}, "both set run"),
    ({"options": [{"name": "a", "choices": [{"label": "x", "parallel": "hybrid"}, {"label": "y"}]}]},
     "needs threads_per_task"),
    ({"sbatch": ["--ntasks=4"]}, "--ntasks is nomad's to write"),
    ({"sbatch": ["--part=bigmem"]}, "--partition is nomad's to write"),            # a prefix sbatch takes
    ({"sbatch": ["--mem-per-cpu=2G"]}, "--mem-per-cpu is nomad's"),
    ({"sbatch": ["--export=NONE"]}, "--export is nomad's"),
    ({"sbatch": ["--bogus"]}, "isn't one of sbatch's options"),
    ({"cpus": 6, "parallel": "hybrid", "threads_per_task": 4, "options": []}, "aren't processes of 4 threads"),
    ({"cpus": 6, "options": [{"name": "p", "choices": [
        {"label": "MPI"}, {"label": "four each", "parallel": "hybrid", "threads_per_task": 4}]}]},
     "p: four each: 6 CPUs aren't processes of 4"),
    ({"options": [{"name": "p", "choices": [{"label": "MPI"}, {"label": "four each", "threads_per_task": 4}]}]},
     "threads_per_task is for parallel"),
    ({"options": [{"name": "p", "choices": [{"label": "x", "parallel": "mpi"}, {"label": "y"}]},
                  {"name": "q", "choices": [{"label": "x", "threads_per_task": 2, "parallel": "hybrid"},
                                            {"label": "y"}]}]}, "both set how it runs in parallel"),
    ({"run_for": {"cp2k/7*": "srun cp2k.popt -i {input}"},
      "options": [{"name": "p", "choices": [{"label": "psmp", "run": "srun cp2k.psmp -i {input}"},
                                            {"label": "popt", "run": "srun cp2k.popt -i {input}"}]}]}, "run_for"),
    ({"options": [{"name": "n", "choices": [{"label": "4"}, {"label": "8"}]}]}, "not a number alone"),
    ({"options": [{"name": "n", "choices": [{"label": "MPI"}, {"label": "mpi"}]}]}, "whatever the case"),
    ({"options": [{"name": "n", "choices": [{"label": "one\u2028#SBATCH --no-requeue"}, {"label": "y"}]}]},
     "label is one line"),
    ({"setup": ["echo a\u2028#SBATCH --no-requeue"]}, "line separator"),
    ({"sbatch": ["--constraint=a100 --x"]}, "no spaces"),
    ({"options": [{"name": f"o{i}", "choices": [{"label": "x"}, {"label": "y"}]} for i in range(5)]},
     "at most 4"),
])
def test_a_template_that_cant_be_used_is_said(change, said):
    with pytest.raises(T.TemplateError, match=said):
        T.template(dict(CP2K, **change))


def test_the_command_lists_options_and_takes_choices(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nomad.cli import cli
    toml = tmp_path / "nomad.toml"
    toml.write_text('''
[[submit.templates]]
program = "CP2K"
modules = ["cp2k/*"]
inputs = [".inp"]
cpus = 8
parallel = "mpi"
run = "srun cp2k.psmp -i {input} -o {stem}.out"
[[submit.templates.options]]
name = "parallel"
label = "Runs as"
[[submit.templates.options.choices]]
label = "MPI processes, one a core"
[[submit.templates.options.choices]]
label = "MPI processes of 2 threads each"
parallel = "hybrid"
threads_per_task = 2
''')
    import sqlite3
    db = tmp_path / "db"
    from nomad.collectors.modules import ModulesCollector
    conn = sqlite3.connect(db)
    conn.executescript(ModulesCollector.SCHEMA)
    conn.execute("INSERT INTO modules_available VALUES ('cp2k/2024.1', 0, '2026-11-05')")
    conn.commit()
    conn.close()
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "Runs as (--choose parallel=N): 1. MPI processes, one a core; 2. MPI processes of 2 threads each" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--make", "CP2K",
                                 "--input", "/home/amy/h2o.inp", "--choose", "parallel=2"])
    assert r.exit_code == 0, r.output
    assert "#SBATCH --ntasks=4" in r.output and "#SBATCH --cpus-per-task=2" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--make", "CP2K",
                                 "--input", "/home/amy/h2o.inp", "--choose", "parallel"])
    assert r.exit_code != 0 and "OPTION=N" in r.output
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--check"])
    assert r.exit_code == 0 and "spydur: 2 script(s) made from 1 template(s); every one could be" in r.output
    # one that can't be made (as render says) fails the check, and is said
    def no(*a, **k):
        raise T.TemplateError("cannot")
    monkeypatch.setattr(T, "render", no)
    r = CliRunner().invoke(cli, ["-c", str(toml), "templates", "--site", "spydur", "--db", str(db), "--check"])
    assert r.exit_code == 1 and "! CP2K (cp2k/2024.1; Runs as: MPI processes of 2 threads each): cannot" in r.output


def test_a_choice_for_one_site_and_lines_that_only_read_a_variable():
    t = T.template({"program": "GROMACS", "modules": ["gromacs/*"], "inputs": [".tpr"], "cpus": 8, "omp": True,
                    "partition": "gpunodes", "run": "gmx mdrun -deffnm {stem} -ntomp $OMP_NUM_THREADS",
                    "sbatch": ["--constr=a100"],
                    "options": [{"name": "where", "label": "Where", "choices": [
                        {"label": "The usual"}, {"label": "arachne's GPUs", "partition": {"arachne": "gpu"}}]}]})
    spydur = T.render(t, module="gromacs/1", input_path="/home/amy/md.tpr", site={"name": "spydur"},
                      choose={"where": 2})
    assert "#SBATCH --partition=gpunodes" in spydur                     # the choice names arachne only
    arachne = T.render(t, module="gromacs/1", input_path="/home/amy/md.tpr", site={"name": "arachne"},
                       choose={"where": 2})
    assert "#SBATCH --partition=gpu" in arachne and "#SBATCH --constraint=a100" in arachne
    assert "export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}" in spydur                        # read in a line, not set there
    own = T.template({"program": "X", "modules": ["x/*"], "inputs": [".x"], "cpus": 4, "omp": True,
                      "setup": ["export OMP_NUM_THREADS=2"], "run": "x {input}"})
    assert T.render(own, module="x/1", input_path="/home/amy/a.x").count("OMP_NUM_THREADS=") == 1
    with pytest.raises(T.TemplateError):
        T.render(T.template(CP2K), module="cp2k/1", input_path="/home/amy/h2o.inp", choose={"parallel": "\u00b2"})


def test_a_program_may_have_a_template_a_site():
    cfg = {"submit": {"templates": [
        {"program": "CP2K", "sites": ["spydur"], "modules": ["cp2k/*"], "inputs": [".inp"], "parallel": "mpi",
         "run": "mpirun -np {tasks} --bind-to core cp2k.psmp -i {input} -o {stem}.out"},
        {"program": "CP2K", "sites": ["arachne"], "modules": ["cp2k/*"], "inputs": [".inp"], "parallel": "mpi",
         "run": "srun --mpi=pmix cp2k.psmp -i {input} -o {stem}.out"},
        {"program": "CP2K", "modules": ["cp2k/*"], "inputs": [".inp"], "run": "cp2k.ssmp {input}"}]}}
    s, ps = T.templates(cfg, "spydur")
    assert [t.run for t in s] == ["mpirun -np {tasks} --bind-to core cp2k.psmp -i {input} -o {stem}.out"]
    assert ps == ["two templates for CP2K at spydur: the first is used"]           # the third is everywhere
    a, _ = T.templates(cfg, "arachne")
    assert a[0].run.startswith("srun --mpi=pmix")
    both, ps = T.templates({"submit": {"templates": cfg["submit"]["templates"][:2]}})
    assert len(both) == 2 and ps == []


def test_the_counts_in_its_lines_follow_the_jobs_own(tmp_path):
    """The gate may resize a job on sbatch's command line: a line written for 16
    processes in a job of 8 fails, so the lines read Slurm's own numbers."""
    import subprocess
    t = T.template({"program": "X", "modules": ["x/*"], "inputs": [".x"], "cpus": 8, "mem": "8G",
                    "parallel": "hybrid", "threads_per_task": 2,
                    "run": "echo n={tasks} t={threads} c={cpus} m={mem_mb_each}"})
    text = T.render(t, module="x/1", input_path="/home/amy/a.x")
    line = text.strip().splitlines()[-1]
    assert line == "echo n=${SLURM_NTASKS:-4} t=${SLURM_CPUS_PER_TASK:-2} " \
                   "c=$(( ${SLURM_NTASKS:-4} * ${SLURM_CPUS_PER_TASK:-2} )) m=1843"
    run = lambda env: subprocess.run(["bash", "-c", line], capture_output=True, text=True, env=env).stdout  # noqa: E731
    assert run({}) == "n=4 t=2 c=8 m=1843\n"
    assert run({"SLURM_NTASKS": "2", "SLURM_CPUS_PER_TASK": "4"}) == "n=2 t=4 c=8 m=1843\n"
    one = T.template({"program": "M", "modules": ["m/*"], "inputs": [".m"], "cpus": 4, "mem": "8G",
                      "run": "m -mem {mem_mb_each} -p {cpus} -t {tasks}"})
    assert T.render(one, module="m/1", input_path="/home/amy/a.m").strip().endswith(
        "m -mem 7372 -p ${SLURM_CPUS_PER_TASK:-4} -t 1")                  # one process: all its memory
    # a line that only mentions the variable doesn't stop the export
    echo = T.template({"program": "E", "modules": ["e/*"], "inputs": [".e"], "cpus": 4, "omp": True,
                       "run": "echo OMP_NUM_THREADS=$OMP_NUM_THREADS; prog {input}"})
    assert "export OMP_NUM_THREADS=" in T.render(echo, module="e/1", input_path="/home/amy/a.e")
    with pytest.raises(T.TemplateError, match="--ntasks-per-node is nomad's|--tasks-per-node is nomad's"):
        T.template(dict(CP2K, sbatch=["--tasks-per-node=2"]))


def test_check_all_counts_what_it_made_and_what_cant_be_read():
    cfg = {"submit": {"templates": [dict(CP2K, cpus=16, options=[]),
                                    dict(CP2K, program="Bad", cpus=16, parallel="hybrid", threads_per_task=3,
                                         options=[])]}}
    made, failed = T.check_all(cfg, "spydur", ["cp2k/1"])
    assert made == 1 and len(failed) == 1 and "aren't processes of 3 threads" in failed[0]
