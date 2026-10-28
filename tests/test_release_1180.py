# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.18.0: `nomad size` and `nomad submit` -- what a job should ask for, from
its script, its program's input file or command line, the person's past
runs and the cluster's partitions; and sending it with that."""
import subprocess

import click
import pytest
from click.testing import CliRunner

from nomad.submit import apps, cluster, forms, history, sizing, spec
from nomad.submit.cli import plan, size_cmd

SEP = history.SEP


def _sub(text):
    return spec.script_options(text)


# ── what a job asks ─────────────────────────────────────────────────────────

def test_a_scripts_request():
    text = """#!/bin/bash
#SBATCH -J 'run #1' -p basic -c 4
#SBATCH --mem-per-cpu=2G   # per core
#SBATCH --time=1-12:00:00
#SBATCH --gres=gpu:a100:2
module load x
#SBATCH --mem=99G
srun python run.py
"""
    sub = _sub(text)
    req = spec.request_of(sub)
    assert (req.job_name, req.partition, req.cpus, req.tasks, req.cpus_per_task) == ("run #1", "basic", 4, 1, 4)
    assert req.mem_form == "mem-per-cpu" and req.mem_per_node() == 8192
    assert req.time_s == 36 * 3600 and req.gpus == 2 and req.gpu_type == "a100" and req.gpu_form == "gres"
    assert spec.sbatch_lines(text)[1] == ["module load x", "srun python run.py"]   # #SBATCH after a command: not read
    req = spec.request_of(_sub("#SBATCH --ntasks=16 --nodes=2\n#SBATCH --gpus-per-node=1\n"))
    assert (req.cpus, req.tasks, req.nodes, req.gpus, req.multi_node) == (16, 16, 2, 2, True)
    req = spec.request_of(_sub("#SBATCH -n 4 -c 4\n"))
    assert (req.tasks, req.cpus_per_task, req.cpus, req.threaded) == (4, 4, 16, False)
    req = spec.request_of(_sub("#SBATCH -N 1 --ntasks-per-node=32\n"))
    assert (req.tasks, req.cpus) == (32, 32)


def test_memory_forms_and_whole_nodes():
    # the last of --mem / --mem-per-cpu / --mem-per-gpu is the one sbatch uses
    req = spec.request_of(spec.merged(_sub("#SBATCH --mem=8G -c 4\n"),
                                      spec.parse_submit_line("sbatch --mem-per-cpu=1G job.sh")))
    assert req.mem_form == "mem-per-cpu" and req.mem_per_node() == 4096
    req = spec.request_of(_sub("#SBATCH --mem=0\n"))
    assert req.mem_form == "mem" and req.mem_mb == 0 and spec.mem_text(0) == "all of a node"
    req = spec.request_of(_sub("#SBATCH --gpus-per-task=1 -n 2 --mem-per-gpu=10G\n"))
    assert req.gpus == 2 and req.gpu_form == "gpus-per-task" and req.mem_per_node() == 20480
    req = spec.request_of(_sub("#SBATCH --tres-per-task=gres/gpu:a100=1 -n 2\n"))
    assert req.gpus == 2 and req.gpu_type == "a100"


def test_memory_as_sbatch_takes_it():
    assert spec.slurm_mem(20480) == "20G" and spec.slurm_mem(1536) == "1536M" and spec.slurm_mem(1535.2) == "1536M"
    assert spec.parse_memory_mb("1.5G") == 1536 and spec.parse_memory_mb("1500M") == 1500


def test_option_tables_and_times():
    sub = spec.parse_submit_line("sbatch -B 2:8:1 --hint=nomultithread --nice job.sh a")
    assert sub.script == "job.sh" and sub.args == ["a"] and sub.get("extra-node-info") == "2:8:1"
    assert spec.parse_time_seconds("05:12.345") == 312 and spec.parse_time_seconds("1-02:03:04.5") == 93784
    sub = spec.parse_submit_line("sbatch -p long --mem=4G run.sh in.dat")
    assert sub.script == "run.sh" and sub.args == ["in.dat"] and sub.get("partition") == "long"
    assert history.script_path("sbatch -p long run.sh", "/home/a/proj") == "/home/a/proj/run.sh"
    assert history.script_path("sbatch --chdir=/x run.sh", "/x") is None
    assert history.script_path("sbatch --wrap='hostname'", "/x") is None


# ── the program and its input ───────────────────────────────────────────────

@pytest.mark.parametrize("lines, family, exe, inp, launcher, cd", [
    (["module load gaussian", "cd $SLURM_SUBMIT_DIR && g16 < water.com > water.log"],
     "Gaussian", "g16", "water.com", None, None),
    (["module load orca; $ORCA_DIR/orca opt.inp > opt.out"], "ORCA", "orca", "opt.inp", None, None),
    (["srun --mpi=pmix -n 32 vasp_std"], "VASP", "vasp_std", None, "srun", None),
    (["mpirun --oversubscribe ./solver in.dat"], None, "solver", None, "mpirun", None),
    (["/usr/bin/time -v ./x in.txt"], None, "x", None, None, None),
    (["apptainer exec --nv --bind /data img.sif python train.py --epochs 3"],
     "Python", "python", "train.py", None, None),
    (["charmrun ++local +p8 namd2 run.conf > log"], "NAMD", "namd2", "run.conf", "charmrun", None),
    (["cd runs/a", "g16 < w.com"], "Gaussian", "g16", "w.com", None, "runs/a"),
    (["g16 <<EOF", "%mem=1GB", "EOF"], "Gaussian", "g16", None, None, None),
])
def test_the_program_a_script_runs(lines, family, exe, inp, launcher, cd):
    p = apps.program_of(lines)
    assert (p.family, p.executable, p.input, p.launcher, p.cd) == (family, exe, inp, launcher, cd)


def test_gpu_builds():
    assert apps.program_of(["srun pmemd.cuda -O -i md.in"]).gpu_build
    assert not apps.program_of(["srun pmemd.MPI -O -i md.in"]).gpu_build


def test_what_gaussian_and_orca_inputs_say():
    g = apps.gaussian("%nprocshared=8\n%mem=16GB\n%chk=x.chk\n# B3LYP/6-31G(d)\n%mem=1GB\n")
    assert g.cpus == 8 and g.mem_mb == round(16384 * 1.2) and g.cpus_from == "%nprocshared=8"
    assert apps.gaussian("%mem=2000000\n#p hf\n").mem_mb == round(2000000 * 8 / 1024 ** 2 * 1.2)
    assert apps.gaussian("%CPU=0-3,8-11\n#p hf\n").cpus == 8
    o = apps.orca("! B3LYP def2-SVP Opt\n%maxcore 3000\n%pal\n  nprocs 8\nend\n* xyz 0 1\n")
    assert o.cpus == 8 and o.mem_mb == round(3000 * 8 * 1.25)
    n = apps.needs(apps.Program("ORCA", "orca", input="x.inp"), lambda p: "! PAL4\n%maxcore 2000\n")
    assert n.cpus == 4 and n.kind == "processes"
    assert apps.needs(apps.Program("Gaussian", "g16", input="w.com"), lambda p: None) is None


@pytest.mark.parametrize("line, inp, cpus, mem, cpus_from, kind", [
    ("qchem -nt 8 water.in water.out", "$rem\n  METHOD b3lyp\n  MEM_TOTAL = 16000\n$end\n", 8,
     round(16000 * 1.2), "qchem -nt 8", "threads"),
    ("qchem -np 4 water.in water.out", "$rem\n MEM_TOTAL 4000\n$end\n@@@\n$rem\n MEM_TOTAL 6000 ! big\n$end\n",
     4, round(6000 * 4 * 1.2), "qchem -np 4", "processes"),
    ("qchem -nt $SLURM_CPUS_PER_TASK x.in", "$rem\n MEM_TOTAL 2000\n$end\n", None, 2400, None, "threads"),
    ("$COLUMBUS/runc -m 1700 -nproc 4 > runls", None, 4, round(1700 * 4 * 1.2), "runc -nproc 4", "processes"),
    ("srun gmx mdrun -deffnm md -ntmpi 2 -ntomp 4", None, 8, None, "mdrun -ntmpi 2 -ntomp 4", "threads"),
    ("charmrun +p8 namd2 run.conf > log", None, 8, None, "+p8", "processes"),
    ("namd2 +p8 run.conf > log", None, 8, None, "+p8", "threads"),
    ("mpirun vasp_std", None, None, None, None, None),
])
def test_what_q_chem_columbus_gromacs_and_namd_say(line, inp, cpus, mem, cpus_from, kind):
    p = apps.program_of([line])
    n = apps.needs(p, lambda path: inp)
    if cpus is None and mem is None:
        assert n is None
        return
    assert (n.cpus, n.mem_mb, n.cpus_from, n.kind) == (cpus, mem, cpus_from, kind)


# ── past runs, from Slurm's accounting ──────────────────────────────────────

def _row(*values):
    return SEP.join(str(v) for v in values)


# JobIDRaw JobID JobName State Partition AllocCPUS AllocTRES Timelimit Elapsed TotalCPU MaxRSS NTasks NNodes
#   Submit ExitCode WorkDir SubmitLine
def _job(jid, name, state, part, cpus, tres, limit, elapsed, total_cpu, rss, submit, wd, line, jobid=None,
         nodes=1):
    return [_row(jid, jobid or jid, name, state, part, cpus, tres, limit, elapsed, total_cpu, "", "", nodes,
                 submit, "0:0", wd, line),
            _row(f"{jid}.batch", f"{jobid or jid}.batch", "batch", state, "", cpus, "", "", elapsed, "",
                 rss, 1, 1, "", "", "", "")]


SACCT = "\n".join(
    _job("101", "water", "COMPLETED", "basic", 8, "cpu=8,mem=32G,node=1", "04:00:00", "01:00:00", "06:00:00",
         "9000M", "2026-10-01T10:00:00", "/home/amy/w", "sbatch job.sh")
    + _job("102", "water", "OUT_OF_MEMORY", "basic", 8, "cpu=8,mem=8G,node=1", "04:00:00", "00:10:00",
           "50:00.250", "8100M", "2026-10-02T10:00:00", "/home/amy/w", "sbatch job.sh")
    + _job("103", "water", "COMPLETED", "basic", 8, "cpu=8,mem=32G,node=1", "04:00:00", "01:30:00",
           "09:00:00", "11000M", "2026-10-03T10:00:00", "/home/amy/w", "sbatch -J a|b job.sh")
    + _job("104", "water", "COMPLETED", "basic", 8, "cpu=8,mem=32G,node=1", "04:00:00", "01:20:00",
           "08:00:00", "10000M", "2026-10-05T10:00:00", "/home/amy/w", "sbatch job.sh")
    + _job("105", "water", "FAILED", "basic", 8, "cpu=8,mem=32G,node=1", "04:00:00", "00:00:20",
           "00:01:00", "50M", "2026-10-06T10:00:00", "/home/amy/w", "sbatch job.sh")
    + _job("200", "md", "TIMEOUT", "long", 4, "cpu=4,mem=4G,node=1,gres/gpu=1", "02:00:00", "02:00:05",
           "07:00:00", "", "2026-10-04T10:00:00", "/home/amy/md", "sbatch md.sh")
    + _job("5012", "arr", "COMPLETED", "basic", 1, "cpu=1,mem=1G,node=1", "01:00:00", "00:10:00",
           "00:09:00", "100M", "2026-10-07T10:00:00", "/home/amy/a",
           "sbatch --array=0-99 --dependency=afterok:4000 -W arr.sh", jobid="5005_7"))

SCONTROL = (
    "PartitionName=basic AllowGroups=ALL AllowAccounts=ALL Default=YES MaxTime=1-00:00:00 State=UP "
    "Nodes=n[1-4] PriorityTier=1 PreemptMode=OFF TotalCPUs=192 TotalNodes=4\n"
    "PartitionName=long AllowGroups=ALL AllowAccounts=ALL MaxTime=7-00:00:00 State=UP Nodes=n[1-4] "
    "PriorityTier=1 PreemptMode=OFF TotalCPUs=192 TotalNodes=4\n"
    "PartitionName=gpu AllowGroups=ALL AllowAccounts=chem MaxTime=2-00:00:00 State=UP Nodes=g1 "
    "PriorityTier=1 PreemptMode=OFF TotalCPUs=64 TotalNodes=1\n"
    "PartitionName=condo_guest AllowGroups=ALL AllowAccounts=ALL MaxTime=3-00:00:00 State=UP "
    "Nodes=c[1-2] PriorityTier=1 PreemptMode=REQUEUE TotalCPUs=256 TotalNodes=2\n"
    "PartitionName=condo AllowGroups=smithlab AllowAccounts=ALL MaxTime=7-00:00:00 State=UP "
    "Nodes=c[1-2] PriorityTier=10 PreemptMode=OFF Hidden=YES TotalCPUs=256 TotalNodes=2\n"
    "PartitionName=mixed AllowGroups=ALL AllowAccounts=ALL MaxTime=1-00:00:00 State=UP "
    "Nodes=m[1-2] PriorityTier=1 PreemptMode=OFF TotalCPUs=136 TotalNodes=2\n")
SINFO = ("basic|192000|48|(null)|4\nlong|192000|48|(null)|4\ngpu|384000|64|gpu:a100:4|1\n"
         "condo_guest|768000|128|(null)|2\nmixed|512000|8|(null)|1\nmixed|64000|128|(null)|1\n")
SINFO_HIDDEN = "condo|768000|128|(null)|2\n"           # sinfo lists a hidden partition only with -a


def _merged(text):
    """sinfo without -e: a partition's nodes on one line, the smallest's memory and CPUs with a '+'."""
    rows: dict = {}
    for line in text.splitlines():
        name, mem, cpus, gres, n = line.split("|")
        rows.setdefault(name, []).append((int(mem), int(cpus), gres, int(n)))
    out = []
    for name, got in rows.items():
        mem, cpus = min(g[0] for g in got), min(g[1] for g in got)
        plus = len(got) > 1
        out.append(f"{name}|{mem}{'+' if plus else ''}|{cpus}{'+' if plus else ''}|{got[0][2]}|"
                   f"{sum(g[3] for g in got)}")
    return "\n".join(out) + "\n"


class Fake:
    """sacct, scontrol, sinfo, sacctmgr and sbatch --version as a cluster answers them."""

    def __init__(self, sacct=SACCT, accounts="ugrad\nchem\n", default="ugrad\n", sacct_fails=None,
                 version="slurm 25.11.5\n", config="SchedulerParameters     = (null)\n"):
        self.sacct, self.accounts, self.default, self.calls = sacct, accounts, default, []
        self.sacct_fails, self.version, self.config = sacct_fails, version, config

    def __call__(self, argv, timeout=30):
        self.calls.append(argv)
        out, rc, err = "", 0, ""
        if argv[0] == "sacct":
            if self.sacct_fails:
                rc, err = 1, self.sacct_fails
            elif "-j" in argv:
                job = argv[argv.index("-j") + 1]
                out = "\n".join(line for line in self.sacct.splitlines()
                                if job in (line.split(SEP)[0].split(".")[0], line.split(SEP)[1].split(".")[0],
                                           line.split(SEP)[1].split("_")[0]))
            else:
                out = self.sacct
        elif argv[0] == "scontrol":
            # a hidden partition only with --all
            out = ("".join(line + "\n" for line in SCONTROL.splitlines()
                           if "--all" in argv or "Hidden=YES" not in line)
                   if "partition" in argv else self.config)
        elif argv[0] == "sinfo":
            out = SINFO + (SINFO_HIDDEN if "-a" in argv else "")
            if "-e" not in argv:
                out = _merged(out)
        elif argv[0] == "sacctmgr":
            out = self.default if "user" in argv else self.accounts
        elif argv[0] == "sbatch":
            out = self.version
        return subprocess.CompletedProcess(argv, rc, out, err)


def test_past_runs_from_sacct():
    runs = history.from_sacct("amy", run=Fake())
    by = {r.job_id: r for r in runs}
    assert [r.job_id for r in runs][:3] == ["5012", "105", "104"]          # newest first
    assert by["101"].peak_mem_mb == 9000 and by["101"].busy_cores == 6.0 and by["101"].mem_mb == 32768
    assert by["102"].cpu_s == 3000                                         # MM:SS.mmm
    assert by["103"].submit_line == "sbatch -J a|b job.sh"                # '|' kept whole
    assert by["101"].script == "/home/amy/w/job.sh" and by["101"].folder_tail == "amy/w"
    assert by["200"].gpus == 1 and by["200"].state == "TIMEOUT"
    assert (by["5012"].array_job, by["5012"].array_task) == ("5005", "7")
    f = Fake()
    history.from_sacct("amy", days=30, run=f)
    assert "-u" in f.calls[0] and "amy" in f.calls[0] and "now-30days" in f.calls[0]
    with pytest.raises(history.HistoryError):
        history.from_sacct("amy; rm -rf /", run=Fake())
    with pytest.raises(history.HistoryError):
        history.from_sacct("amy", run=Fake(sacct_fails="sacct: error: Problem talking to the database"))
    assert history.job_from_sacct("5005_7", "amy", run=Fake()).job_id == "5012"


def test_an_older_slurm_without_submit_lines():
    calls = []

    def run(argv, timeout=30):
        calls.append(argv)
        fmt = next(a for a in argv if a.startswith("--format="))
        if "SubmitLine" in fmt:
            return subprocess.CompletedProcess(argv, 1, "", "sacct: error: Invalid field requested: SubmitLine")
        rows = [SEP.join(line.split(SEP)[:-1]) for line in SACCT.splitlines()]
        return subprocess.CompletedProcess(argv, 0, "\n".join(rows), "")
    runs = history.from_sacct("amy", run=run)
    assert len(calls) == 2 and len(runs) == 7 and all(r.script is None for r in runs)


# ── sizing ──────────────────────────────────────────────────────────────────

def _runs():
    runs = history.from_sacct("amy", run=Fake())
    for r in runs:
        r.family, r.executable = ("Gaussian", "g16") if r.job_name == "water" else ("AMBER", "pmemd.cuda")
    return runs


def _parts():
    return cluster.live(run=Fake())


ACCESS = cluster.Access(groups={"amy", "people"}, accounts={"ugrad", "chem"}, default_account="ugrad")
G16 = apps.Program("Gaussian", "g16")


def test_memory_and_time_from_runs_of_this_script():
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=64G -t 1-00:00:00 -p basic\n"), G16, _runs(),
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert s.matched == "this script" and s.strong and len(s.similar) == 5
    m = s.choices["mem"]
    # the biggest peak (11000M) × 1.5, above the run that ran out at 8G × 1.5; the failed run left out
    assert m.value == sizing.round_mem(11000 * 1.5) and m.source == "history" and "ran out of memory" in m.reason
    t = s.choices["time"]
    assert t.value == sizing.round_time_up(5400 * 1.5) and "shorter limit starts sooner" in t.reason
    assert s.choices["cpus"].value == 8 and not s.choices["cpus"].changed       # ~6 of 8 busy: kept
    assert s.options() == [f"--mem={spec.slurm_mem(m.value)}", f"--time={sizing.format_time_slurm(t.value)}"]
    assert s.options()[0] == "--mem=18G"
    assert not s.problems


def test_weaker_matches_only_raise():
    runs = _runs()
    # the same job name in another folder: raise only, never lower
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=64G -t 1-00:00:00\n"), apps.Program(None, "other"),
                    runs, script="/home/amy/elsewhere/job.sh", partitions=_parts(), access=ACCESS)
    assert s.matched == "jobs named water" and not s.strong
    assert not s.choices["mem"].changed and "nomad only raises from them" in s.choices["mem"].reason
    assert not s.choices["time"].changed
    assert any("nomad only raises values from those" in n for n in s.notes)
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=4G -t 00:30:00\n"), apps.Program(None, "other"),
                    runs, script="/home/amy/elsewhere/job.sh", partitions=_parts(), access=ACCESS)
    assert s.choices["mem"].changed and s.choices["time"].changed               # raised


def test_too_few_runs_lower_nothing():
    runs = [r for r in _runs() if r.job_id in ("101", "105")]                  # one good, one failed
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=64G -t 1-00:00:00\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert not s.choices["mem"].changed and not s.choices["time"].changed
    assert any("too few for nomad to lower a value from them" in n for n in s.notes)


def test_cpus_only_lowered_as_cpus_per_task_never_tasks():
    runs = _runs()
    for r in runs:
        r.cpu_s = (r.elapsed_s or 0) * 2.0                      # 2 cores busy
        r.cpus = 16                                             # of the 16 they had
    s = sizing.size(_sub("#SBATCH -J water -c 16\n"), G16, runs, script="/home/amy/w/job.sh")
    assert s.choices["cpus"].value == 2 and s.choices["cpus"].opts == [("cpus-per-task", "2")]
    # runs that had 4 CPUs, all busy, can't show 32 are too many
    for r in runs:
        r.cpus, r.cpu_s = 4, (r.elapsed_s or 0) * 4.0
    s = sizing.size(_sub("#SBATCH -J water -c 32\n"), G16, runs, script="/home/amy/w/job.sh")
    assert not s.choices["cpus"].changed and "had fewer than the 32 CPUs" in s.choices["cpus"].reason
    s = sizing.size(_sub("#SBATCH -J water --ntasks=16\n"), G16, runs, script="/home/amy/w/job.sh")
    assert not s.choices["cpus"].changed                       # an MPI launch counts on its tasks


def test_threads_and_processes_from_the_program():
    # Gaussian's threads on a job asking 4 tasks × 4 CPUs: one task of 8
    needs = apps.gaussian("%nprocshared=8\n%mem=4GB\n#p\n")
    s = sizing.size(_sub("#SBATCH -n 4 -c 4\n"), apps.Program("Gaussian", "g16", input="w.com"), [], needs=needs)
    assert s.choices["cpus"].value == 8 and ("ntasks", "1") in s.choices["cpus"].opts
    assert ("cpus-per-task", "8") in s.choices["cpus"].opts
    # ORCA's processes on a job asking nothing: 8 tasks, not 8 CPUs of one
    needs = apps.needs(apps.Program("ORCA", "orca", input="x.inp"), lambda p: "! PAL8\n%maxcore 1000\n")
    s = sizing.size(_sub(""), apps.Program("ORCA", "orca", input="x.inp"), [], needs=needs)
    assert s.choices["cpus"].opts == [("ntasks", "8")]
    # NAMD under charmrun with --ntasks-per-node=32 (one node): already right
    p = apps.program_of(["charmrun +p32 namd2 run.conf > log"])
    s = sizing.size(_sub("#SBATCH --ntasks-per-node=32\n"), p, [], needs=apps.needs(p, lambda x: None))
    assert not s.choices["cpus"].changed and "matches its command line" in s.choices["cpus"].reason


def test_the_input_and_proven_peaks():
    needs = apps.gaussian("%nprocshared=8\n%mem=16GB\n#p b3lyp\n")
    s = sizing.size(_sub("#SBATCH -J water -c 4 --mem=8G\n"), apps.Program("Gaussian", "g16", input="w.com"),
                    _runs(), needs=needs, script="/home/amy/w/job.sh")
    assert s.choices["cpus"].value == 8 and s.choices["cpus"].source == "input"
    assert s.choices["cpus"].reason == ("the input sets 8 threads of one task (%nprocshared=8): 4 CPUs would "
                                        "make them share cores")
    assert s.choices["mem"].value == sizing.round_mem(16384 * 1.2) and s.choices["mem"].source == "input"
    runs = _runs()
    for r in runs:
        if r.peak_mem_mb:
            r.peak_mem_mb = 26000                                # past runs used more than %mem says
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=32G\n"), apps.Program("Gaussian", "g16", input="w.com"),
                    runs, needs=needs, script="/home/amy/w/job.sh")
    assert s.choices["mem"].value == sizing.round_mem(26000 * 1.2) and "peaked at" in s.choices["mem"].reason


def test_per_cpu_memory_stays_per_cpu_in_whole_megabytes():
    needs = apps.gaussian("%nprocshared=8\n%mem=16GB\n#p\n")
    s = sizing.size(_sub("#SBATCH -c 8 --mem-per-cpu=1G\n"), apps.Program("Gaussian", "g16", input="w"), [],
                    needs=needs)
    assert s.choices["mem"].opts == [("mem-per-cpu", "2560M")]
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=1024)
    s = sizing.size(_sub("#SBATCH --mem=1G\n"), apps.Program(None, None), [], outcome=oom)
    assert s.options() == ["--mem=1536M"]


def test_a_job_run_again_after_running_out():
    runs = _runs()
    oom = next(r for r in runs if r.job_id == "102")
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=8G -p basic\n"), G16, runs, outcome=oom,
                    script="/home/amy/w/job.sh")
    assert s.choices["mem"].source in ("outcome", "history") and s.choices["mem"].value >= sizing.round_mem(8192 * 1.5)
    to = next(r for r in runs if r.job_id == "200")
    s = sizing.size(_sub("#SBATCH -J md -n 1 -c 4 -t 2:00:00 -p long\n"),
                    apps.Program("AMBER", "pmemd.cuda", gpu_build=True), runs, outcome=to)
    assert s.choices["time"].value == sizing.round_time_up(7200 * 1.5) and s.choices["time"].source == "outcome"
    assert s.choices["gpus"].value == 1 and s.choices["gpus"].opts == [("gres", "gpu:1")]
    failed = history.PastRun(job_id="9", state="FAILED", exit_code="1:0")
    s = sizing.size(_sub("#SBATCH -c 1\n"), apps.Program(None, None), [], outcome=failed)
    assert any("more resources won't fix that" in n for n in s.notes)


def test_a_reading_bigger_than_the_nodes_is_left_out():
    runs = _runs()
    for r in runs:
        if r.job_id == "104":
            r.peak_mem_mb = 600 * 1024                          # on basic, whose nodes have 187.5G
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=16G -p basic\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert s.choices["mem"].value == sizing.round_mem(11000 * 1.5)
    assert any("larger than any node" in n for n in s.notes)


def test_jobs_on_several_nodes():
    # 2 nodes × 32 tasks × 3G: 96G a node, which basic's nodes have
    s = sizing.size(_sub("#SBATCH -N 2 --ntasks-per-node=32 --mem-per-cpu=3G -t 4:00:00\n"),
                    apps.Program(None, "solver"), [], partitions=_parts(), access=ACCESS)
    assert not s.choices["mem"].changed and not s.choices["cpus"].changed and not s.problems
    assert s.choices["partition"].value is None and "the default, basic, fits" in s.choices["partition"].reason
    # 96 tasks, no --nodes: Slurm spreads them
    s = sizing.size(_sub("#SBATCH --ntasks=96 -t 1:00:00\n"), apps.Program(None, "solver"), [],
                    partitions=_parts(), access=ACCESS)
    assert not s.problems and s.choices["partition"].value is None


def test_partitions_that_fit_and_who_may_use_them():
    parts = _parts()
    by = {p.name: p for p in parts}
    assert by["gpu"].node_gpus == 4 and by["basic"].node_mem_mb == 192000 and by["basic"].default
    assert by["condo_guest"].guest and not by["condo"].guest and "condo" in by     # --all: hidden ones too
    s = sizing.size(_sub("#SBATCH -c 8 --mem=16G -t 2-00:00:00 -p basic\n"), apps.Program(None, None), [],
                    partitions=parts, access=ACCESS)
    p = s.choices["partition"]
    assert p.value == "long" and "basic doesn't fit: it allows at most 1-00:00:00" in p.reason
    # a GPU: gpu is the chem account's; the job is charged to ugrad, so none fits as it stands
    s = sizing.size(_sub("#SBATCH -c 4 --gres=gpu:1 -p basic\n"), apps.Program(None, None), [],
                    partitions=parts, access=ACCESS)
    assert s.problems and "add --account=chem" in next(f for f in s.fits if f["partition"] == "gpu")["may_why"]
    s = sizing.size(_sub("#SBATCH -A chem -c 4 --gres=gpu:1 -p basic\n"), apps.Program(None, None), [],
                    partitions=parts, access=ACCESS)
    assert s.choices["partition"].value == "gpu"
    # 600G fits only the condo nodes; the condo is smithlab's: the guest partition
    s = sizing.size(_sub("#SBATCH -c 8 --mem=600G -t 1:00:00\n"), apps.Program(None, None), [],
                    partitions=parts, access=ACCESS)
    assert s.choices["partition"].value == "condo_guest" and "preemptible" in s.choices["partition"].reason
    # 500G with 8 CPUs: the mixed partition's big node, not the preemptible one
    s = sizing.size(_sub("#SBATCH -c 8 --mem=500G -t 1:00:00\n"), apps.Program(None, None), [],
                    partitions=parts, access=ACCESS)
    assert s.choices["partition"].value == "mixed"
    # a partition Slurm doesn't list here: kept
    s = sizing.size(_sub("#SBATCH -p secret\n"), apps.Program(None, None), [], partitions=parts, access=ACCESS)
    assert s.choices["partition"].value == "secret" and not s.choices["partition"].changed


def test_one_node_must_have_all_a_job_needs():
    # mixed: one node with 500G and 8 CPUs, one with 62.5G and 128: neither has 100G and 32 CPUs
    s = sizing.size(_sub("#SBATCH -c 32 --mem=100G -t 1:00:00 -p mixed\n"), apps.Program(None, None), [],
                    partitions=_parts(), access=ACCESS)
    assert "none of its nodes has 32 CPUs and 100G together" in s.choices["partition"].reason


def test_a_time_the_partition_cant_give_is_capped_and_sent():
    runs = _runs()
    for r in runs:
        r.elapsed_s = 30 * 3600 if r.state == "COMPLETED" else r.elapsed_s
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 20:00:00 -p condo_guest\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    # 45 h wanted: the guest partition allows 3 days, so it fits
    assert s.choices["time"].value == 45 * 3600 and s.choices["time"].source == "history"
    # a GPU job on gpu (chem's), which allows 2 days and is the only one with GPUs: 60 h wanted is
    # asked at 2 days (long would take 60 h, but has no GPUs)
    for r in runs:
        r.elapsed_s = 40 * 3600 if r.state == "COMPLETED" else r.elapsed_s
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 20:00:00 -p gpu -A chem --gres=gpu:1\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    t = s.choices["time"]
    assert t.value == 48 * 3600 and t.source == "partition" and t.opts == [("time", "2-00:00:00")]
    assert not s.problems and any("would be safer than gpu allows" in n for n in s.notes)
    # asked on basic (1 day), with nothing longer it may fit: capped, and no stale problem
    parts = [p for p in _parts() if p.name in ("basic", "mixed")]
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 20:00:00 -p basic\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=parts, access=ACCESS)
    assert s.choices["time"].value == 24 * 3600 and not s.problems
    assert s.choices["partition"].value == "basic" and not s.choices["partition"].changed


def test_forms():
    req = spec.request_of(_sub("#SBATCH --gpus=a100:1\n"))
    assert forms.gpus(req, _sub("#SBATCH --gpus=a100:1\n"), 2)[0] == [("gpus", "a100:2")]
    sub = _sub("#SBATCH --gres=gpu:a100:1,lscratch:100\n")
    assert forms.gpus(spec.request_of(sub), sub, 2)[0] == [("gres", "gpu:a100:2,lscratch:100")]
    sub = _sub("#SBATCH --gpus-per-node=1 -N 2\n")
    assert forms.gpus(spec.request_of(sub), sub, 4)[0] == [("gpus-per-node", "2")]
    sub = _sub("#SBATCH -n 2 --tres-per-task=gres/gpu=1,cpu=2\n")
    assert forms.gpus(spec.request_of(sub), sub, 4)[0] == [("tres-per-task", "gres/gpu=2,cpu=2")]
    assert forms.cpus(spec.request_of(_sub("#SBATCH -c 4\n")), 6, "asked")[0] == [("cpus-per-task", "6")]
    assert forms.cpus(spec.request_of(_sub("#SBATCH -n 4\n")), 6, "asked")[0] == [("ntasks", "6")]


# ── the commands ────────────────────────────────────────────────────────────

@pytest.fixture
def cluster_here(tmp_path, monkeypatch):
    """A cluster's answers, a Gaussian job in a folder, and sbatch recorded."""
    import nomad.submit.cli as sc
    w = tmp_path / "w"
    w.mkdir()
    (w / "w.com").write_text("%nprocshared=8\n%mem=16GB\n# b3lyp\n\nt\n\n0 1\nO\n")
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water\n#SBATCH -c 4 --mem=8G -t 4:00:00 -p basic\n"
                              "g16 < w.com > w.log\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "arr.sh").write_text("#!/bin/bash\n#SBATCH -c 1\npython x.py $SLURM_ARRAY_TASK_ID\n")
    sacct = SACCT.replace("/home/amy/w", str(w)).replace("/home/amy/a", str(tmp_path / "a"))
    f = Fake(sacct=sacct)
    real = sc.plan
    monkeypatch.setattr(sc, "plan", lambda *a, **k: real(*a, **{**k, "user": "amy", "run_sacct": f,
                                                              "run_slurm": f}))
    monkeypatch.setattr(sc.cluster, "slurm_version", lambda run=None: (25, 11))
    monkeypatch.setattr(sc.cluster, "extra_is_constraints", lambda run=None: False)
    sent = []

    def sbatch(argv, cwd):
        sent.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, "4242;hpc1\n", "")
    monkeypatch.setattr(sc, "_run_sbatch", sbatch)
    monkeypatch.chdir(w)
    return w, sent


def test_nomad_size_on_a_script(cluster_here):
    w, sent = cluster_here
    pl = plan(str(w / "job.sh"), user="amy", run_sacct=Fake(), run_slurm=Fake())
    d = pl.to_dict()
    assert d["program"]["family"] == "Gaussian" and d["needs"]["cpus"] == 8
    assert d["choices"]["cpus"]["value"] == 8 and d["choices"]["mem"]["source"] == "input"
    r = CliRunner().invoke(size_cmd, ["job.sh"])
    assert r.exit_code == 0, r.output
    assert "Gaussian (g16), input w.com" in r.output and "#SBATCH --cpus-per-task=8" in r.output


def test_the_input_is_read_where_the_job_runs(cluster_here):
    w, sent = cluster_here
    jobs = w / "jobs"
    jobs.mkdir()
    (jobs / "g.sh").write_text("#!/bin/bash\n#SBATCH -c 2\ng16 < w.com\n")
    (jobs / "w.com").write_text("%nprocshared=2\n%mem=2GB\n#p\n")
    # sbatch runs it here, in w: w's own w.com (8 threads) is what it reads
    pl = plan("jobs/g.sh", user="amy", run_sacct=Fake(), run_slurm=Fake())
    assert pl.needs.cpus == 8
    (w / "w.com").unlink()
    pl = plan("jobs/g.sh", user="amy", run_sacct=Fake(), run_slurm=Fake())
    assert pl.needs is None and any("next to the script" in n for n in pl.notes)


def test_submit_with_nomads_values(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh", "extra-arg"])
    assert r.exit_code == 0, r.output
    (argv, cwd), = sent
    assert argv[:2] == ["sbatch", "--parsable"] and argv[-2:] == [str(w / "job.sh"), "extra-arg"]
    assert "--cpus-per-task=8" in argv and "--mem=20G" in argv and cwd == str(w)
    extra = next(a for a in argv if a.startswith("--extra="))
    assert ";via=cli;cpus=i;mem=i;time=" in extra and "part=s" in extra
    assert "Submitted job 4242. nomad set: CPUs, memory" in r.output


def test_submit_with_your_values_or_the_scripts_own(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--yes", "--mem", "1500M", "--cpus", "6", "job.sh"])
    assert r.exit_code == 0, r.output
    argv = sent[-1][0]
    assert "--mem=1500M" in argv and "--cpus-per-task=6" in argv
    assert "cpus=u;mem=u" in next(a for a in argv if a.startswith("--extra="))
    r = CliRunner().invoke(submit_cmd, ["--yes", "--keep", "job.sh"])
    argv = sent[-1][0]
    assert not any(a.startswith(("--mem", "--cpus", "--time", "--partition")) for a in argv)
    assert "cpus=s;mem=s;time=s;gpus=d;part=s" in next(a for a in argv if a.startswith("--extra="))
    r = CliRunner().invoke(submit_cmd, ["--yes", "--mem", "lots", "job.sh"])
    assert r.exit_code != 0 and "a size like 8G" in r.output
    # what you ask is checked too
    r = CliRunner().invoke(submit_cmd, ["--dry-run", "--mem", "900G", "job.sh"])
    assert "Slurm would likely refuse it" in r.output and "none of its nodes has" in r.output


def test_submit_asks_first_and_dry_runs(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--dry-run", "job.sh"])
    assert r.exit_code == 0 and "sbatch command:" in r.output and "Not submitted (--dry-run)" in r.output
    r = CliRunner().invoke(submit_cmd, ["job.sh"])           # no terminal, no --yes
    assert r.exit_code != 0 and "run with --yes" in r.output
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh", "--dry-run"])
    assert r.exit_code != 0 and "put nomad's options before the script" in r.output
    assert sent == []


def test_submit_a_job_again(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "102"])
    assert r.exit_code == 0, r.output
    argv, cwd = sent[-1]
    assert f"--chdir={w}" in argv and argv[-1] == str(w / "job.sh") and cwd == str(w)
    assert "--mem=20G" in argv and "again=102" in next(a for a in argv if a.startswith("--extra="))


def test_an_array_task_again_without_its_dependency_or_wait(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "5005_7"])
    assert r.exit_code == 0, r.output
    argv = sent[-1][0]
    assert "--array=7" in argv and not any(a.startswith(("--dependency", "--wait")) for a in argv)
    assert "--array=0-99" not in argv and "Only task 7 of array 5005 runs again" in r.output


def test_a_command_line_read_back_without_quotes(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    # 'sbatch -J a|b job.sh': its script is found; it has no arguments
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "103"])
    assert r.exit_code == 0, r.output


def test_arguments_read_back_need_a_look(cluster_here):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    # a job run with an argument: its line came back from Slurm without quotes
    pl = plan(None, job="5005_7", user="amy", run_sacct=Fake(
        sacct=SACCT.replace("arr.sh", "arr.sh in.dat").replace("/home/amy/a", str(w.parent / "a"))),
        run_slurm=Fake())
    assert pl.args_unsure and pl.line.args == ["in.dat"]


def test_no_tag_on_an_older_slurm_or_where_extra_is_constraints(cluster_here, monkeypatch):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    monkeypatch.setattr(sc.cluster, "slurm_version", lambda run=None: (22, 5))
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and not any(a.startswith("--extra=") for a in sent[-1][0])
    assert "it came in 23.02" in r.output
    monkeypatch.setattr(sc.cluster, "slurm_version", lambda run=None: (25, 11))
    monkeypatch.setattr(sc.cluster, "extra_is_constraints", lambda run=None: True)
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert not any(a.startswith("--extra=") for a in sent[-1][0]) and "extra_constraints" in r.output
    assert cluster.slurm_version(Fake()) == (25, 11)


def test_an_unknown_extra_is_retried_untagged(cluster_here, monkeypatch):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    calls = []

    def sbatch(argv, cwd):
        calls.append(argv)
        if any(a.startswith("--extra=") for a in argv):
            return subprocess.CompletedProcess(argv, 1, "", "sbatch: unrecognized option '--extra=nomad=x'")
        return subprocess.CompletedProcess(argv, 0, "77\n", "")
    monkeypatch.setattr(sc, "_run_sbatch", sbatch)
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and len(calls) == 2 and "Submitted job 77" in r.output


def test_a_job_that_sets_extra_itself_is_not_tagged(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    (w / "job.sh").write_text("#!/bin/bash\n#SBATCH -J water --extra=site_thing\ng16 < w.com\n")
    r = CliRunner().invoke(submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0, r.output
    assert not any(a.startswith("--extra=") for a in sent[-1][0]) and "doesn't tag it" in r.output


def test_only_your_own_jobs(cluster_here):
    r = CliRunner().invoke(size_cmd, ["--db", str(cluster_here[0] / "w.com"), "--user", "bob", "--job", "1"])
    assert r.exit_code != 0 and "your own jobs only" in r.output


def test_the_tag():
    from nomad.submit.cli import tag
    s = sizing.Sizing(sub=spec.SubmitLine(), request=spec.Request(), program=apps.Program(None, None))
    s.choices = {"cpus": sizing.Choice("cpus", 4, 4, "asked", ""),
                 "mem": sizing.Choice("mem", 4096, 8192, "history", "", opts=[("mem", "8G")]),
                 "time": sizing.Choice("time", 60, 120, "user", "", opts=[("time", "2")]),
                 "gpus": sizing.Choice("gpus", None, None, "default", ""),
                 "partition": sizing.Choice("partition", None, "long", "partition", "",
                                            opts=[("partition", "long")])}
    assert tag(s, again="77").split(";")[1:] == ["via=cli", "cpus=s", "mem=h", "time=u", "gpus=d", "part=p",
                                                "again=77"]


def test_nomad_size_needs_a_script_or_a_job():
    r = CliRunner().invoke(size_cmd, [])
    assert r.exit_code != 0 and "give a job script, or --job" in r.output
    r = CliRunner().invoke(size_cmd, ["x.sh", "--user", "bob"])
    assert r.exit_code != 0 and "--user goes with --db" in r.output


# ── the second review ───────────────────────────────────────────────────────

def _ran(n, *, peak=None, elapsed=3600, cpus=8, busy=None, state="COMPLETED", script="/home/amy/w/job.sh",
         workdir="/home/amy/w", exe="g16", family="Gaussian", mem=None, limit=None, name="water"):
    """``n`` past runs alike."""
    return [history.PastRun(job_id=str(900 + i), state=state, job_name=name, cpus=cpus, nodes=1, mem_mb=mem,
                            time_s=limit, elapsed_s=elapsed, peak_mem_mb=peak,
                            cpu_s=(busy * elapsed) if busy else None, script=script, workdir=workdir,
                            folder_tail=history.folder_tail(workdir), executable=exe, family=family,
                            submitted=f"2026-10-{10 + i:02d}T10:00:00")
            for i in range(n)]


def test_per_cpu_memory_follows_a_change_of_cpus():
    from nomad.submit.cli import _yours
    # 16 CPUs × 2G, three runs keeping 2 cores busy and peaking at 19.5G: 2 CPUs, still 32G a node
    runs = _ran(3, peak=19.5 * 1024, cpus=16, busy=2.0)
    s = sizing.size(_sub("#SBATCH -J water -c 16 --mem-per-cpu=2G\n"), G16, runs, script="/home/amy/w/job.sh")
    assert s.choices["cpus"].opts == [("cpus-per-task", "2")]
    assert s.choices["mem"].opts == [("mem-per-cpu", "16G")] and s.final().mem_per_node() == 32768
    # Gaussian's 8 threads and 20G, asked as 4 CPUs × 4G: 8 × 2560M
    needs = apps.gaussian("%nprocshared=8\n%mem=16GB\n#p\n")
    s = sizing.size(_sub("#SBATCH -c 4 --mem-per-cpu=4G\n"), apps.Program("Gaussian", "g16", input="w"), [],
                    needs=needs)
    assert s.choices["mem"].opts == [("mem-per-cpu", "2560M")] and s.final().mem_per_node() == 20480
    # your own CPUs: the node keeps its memory
    s = sizing.size(_sub("#SBATCH -c 4 --mem-per-cpu=2G\n"), apps.Program(None, "x"), [])
    _yours(s, 8, None, None, None, None, False)
    assert ("mem-per-cpu", "1G") in s.opts() and s.final().mem_per_node() == 8192
    # per GPU, with your GPUs
    s = sizing.size(_sub("#SBATCH --gpus=2 --mem-per-gpu=10G\n"), apps.Program(None, "x"), [])
    _yours(s, None, None, None, 4, None, False)
    assert ("mem-per-gpu", "5G") in s.opts() and s.final().mem_per_node() == 20480


def test_a_run_again_takes_the_most_its_past_runs_needed():
    oom = history.PastRun(job_id="77", state="OUT_OF_MEMORY", mem_mb=8192, job_name="water",
                          script="/home/amy/w/job.sh", workdir="/home/amy/w")
    s = sizing.size(_sub("#SBATCH -J water -c 8 --mem=8G\n"), G16, _ran(3, peak=29.3 * 1024) + [oom],
                    outcome=oom, script="/home/amy/w/job.sh")
    m = s.choices["mem"]
    assert m.value == sizing.round_mem(29.3 * 1024 * 1.5) and m.source == "history" and "ran out" in m.reason
    to = history.PastRun(job_id="78", state="TIMEOUT", time_s=3600, elapsed_s=3600, job_name="water",
                         script="/home/amy/w/job.sh", workdir="/home/amy/w")
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 1:00:00\n"), G16, _ran(3, elapsed=20 * 3600) + [to],
                    outcome=to, script="/home/amy/w/job.sh")
    assert s.choices["time"].value == sizing.round_time_up(30 * 3600)


def test_a_relative_chdir_is_left_to_sbatch(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    (w / "run1").mkdir()
    (w / "run1" / "w.com").write_text("%nprocshared=2\n%mem=2GB\n#p\n")
    (w / "c.sh").write_text("#!/bin/bash\n#SBATCH -J c --chdir=run1 -c 2 --mem=3G\ng16 < w.com\n")
    pl = plan("c.sh", user="amy", run_sacct=Fake(), run_slurm=Fake())
    assert pl.folder == str(w / "run1") and pl.needs.cpus == 2          # the input read in run1
    r = CliRunner().invoke(submit_cmd, ["--yes", "c.sh"])
    assert r.exit_code == 0, r.output
    argv, cwd = sent[-1]
    assert cwd == str(w) and not any(a.startswith("--chdir") for a in argv)   # sbatch goes into run1 itself


def test_the_same_program_means_this_very_folder_and_not_an_interpreter():
    runs = _ran(3, peak=1024, elapsed=600, script="/scratch/amy/proj/run/other.sh", workdir="/scratch/amy/proj/run")
    assert not sizing.similar(runs, script="/x/job.sh", program=G16, folder="/home/amy/proj/run")[2]
    what, sim, strong = sizing.similar(runs, script="/x/job.sh", program=G16, folder="/scratch/amy/proj/run")
    assert strong and what == "g16 in this folder"
    py = _ran(3, peak=1024, elapsed=600, script="/home/amy/p/prep.sh", workdir="/home/amy/p", exe="python",
              family="Python", name="prep")
    s = sizing.size(_sub("#SBATCH -J train --mem=128G -t 2-00:00:00\n"), apps.Program("Python", "python"), py,
                    script="/home/amy/p/train.sh", folder="/home/amy/p")
    assert s.matched is None and not s.choices["mem"].changed and not s.choices["time"].changed
    # lines continued with a backslash are read whole
    p = apps.program_of(spec.sbatch_lines("#!/bin/bash\ncharmrun +p32 \\\n  namd2 run.conf\n")[1])
    assert (p.executable, p.launcher) == ("namd2", "charmrun") and apps.needs(p, lambda x: None).cpus == 32
    assert apps.program_of(spec.sbatch_lines("srun \\\n  --mpi=pmix \\\n  vasp_std\n")[1]).executable == "vasp_std"


def test_tasks_per_node_without_nodes_are_tasks_on_one_node():
    req = spec.request_of(_sub("#SBATCH --ntasks-per-node=8 -c 4\n"))
    assert (req.tasks, req.cpus, req.threaded) == (8, 32, False)
    s = sizing.size(_sub("#SBATCH -J water --ntasks-per-node=8 -c 4\n"), G16, _ran(3, cpus=32, busy=1.0),
                    script="/home/amy/w/job.sh")
    assert not s.choices["cpus"].changed                             # MPI tasks never lowered
    assert forms.cpus(spec.request_of(_sub("#SBATCH -n 1\n")), 8, "asked")[0] == [("cpus-per-task", "8")]


def test_hidden_partitions_have_their_nodes_read():
    f = Fake()
    by = {p.name: p for p in cluster.live(run=f)}
    assert any(c[0] == "sinfo" and "-a" in c for c in f.calls)
    assert by["condo"].node_mem_mb == 768000 and by["gpu"].total_gpus == 4 and by["basic"].total_gpus == 0
    smith = cluster.Access(groups={"amy", "smithlab"}, accounts={"ugrad"}, default_account="ugrad")
    parts = _parts()
    for p in parts:
        if p.name == "condo":
            p.shapes, p.node_mem_mb, p.node_cpus = [], None, None     # its nodes couldn't be read: last
    s = sizing.size(_sub("#SBATCH -c 8 --mem=16G -t 2-00:00:00 -p basic\n"), apps.Program(None, None), [],
                    partitions=parts, access=smith)
    assert s.choices["partition"].value == "long"
    s = sizing.size(_sub("#SBATCH -c 8 --mem=2000G -t 1:00:00\n"), apps.Program(None, None), [],
                    partitions=parts, access=smith)
    assert s.choices["partition"].value is None and s.problems                  # not offered: not read
    s = sizing.size(_sub("#SBATCH -c 8 --mem=2000G -t 1:00:00\n"), apps.Program(None, None), [],
                    partitions=_parts(), access=smith)
    assert s.problems and s.choices["partition"].value is None


def test_a_job_nomad_sent_is_sent_again_with_a_new_tag(cluster_here):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    line = (f"sbatch --parsable --mem=20G --cpus-per-task=8 "
            f"--extra=nomad=1.18.0;via=cli;cpus=i;mem=i;time=s;gpus=d;part=s {w / 'job.sh'}")
    f = Fake(sacct="\n".join(_job("300", "water", "COMPLETED", "basic", 8, "cpu=8,mem=20G,node=1", "04:00:00",
                                  "01:00:00", "06:00:00", "9000M", "2026-10-09T10:00:00", str(w), line)))
    pl = plan(None, job="300", user="amy", run_sacct=f, run_slurm=f)
    assert sc.extra_ok(pl) == (True, None)
    extras = [a for a in sc.command(pl, args=[], tagged=True)["argv"] if a.startswith("--extra=")]
    assert len(extras) == 1 and extras[0].endswith(";again=300")
    assert ";cpus=i;mem=i;" in extras[0]                     # kept from its own line: nomad's, from the input


def test_a_value_not_asked_is_weighed_against_the_partitions_default():
    one = _ran(1, peak=500, elapsed=600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other",
               family=None)
    s = sizing.size(_sub("#SBATCH -J water -p long\n"), apps.Program(None, "other"), one, partitions=_parts(),
                    access=ACCESS)
    assert s.choices["time"].value is None and s.choices["mem"].value is None    # long's own stand
    parts = _parts()
    for p in parts:
        if p.name == "long":
            p.default_time_s = 3600
    one = _ran(1, elapsed=5 * 3600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other", family=None)
    s = sizing.size(_sub("#SBATCH -J water -p long\n"), apps.Program(None, "other"), one, partitions=parts,
                    access=ACCESS)
    t = s.choices["time"]
    assert t.value == sizing.round_time_up(7.5 * 3600) and "the partition's default (01:00:00)" in t.reason


def test_memory_is_lowered_only_from_runs_that_completed():
    runs = _ran(2, peak=2048) + _ran(1, peak=2048, state="TIMEOUT")
    s = sizing.size(_sub("#SBATCH -J water --mem=64G\n"), G16, runs, script="/home/amy/w/job.sh")
    assert not s.choices["mem"].changed
    s = sizing.size(_sub("#SBATCH -J water --mem=64G\n"), G16, _ran(3, peak=2048), script="/home/amy/w/job.sh")
    assert s.choices["mem"].value == 3072


def test_your_gpus_the_way_the_job_asks_them():
    from nomad.submit.cli import _yours
    s = sizing.size(_sub("#SBATCH -n 4 --gpus-per-task=1\n"), apps.Program(None, "x"), [])
    with pytest.raises(click.BadParameter, match="multiple of 4"):
        _yours(s, None, None, None, 2, None, False)
    _yours(s, None, None, None, 8, None, False)
    assert s.opts() == [("gpus-per-task", "2")]
    s = sizing.size(_sub("#SBATCH --gres=gpu:1,lscratch:10\n"), apps.Program(None, "x"), [])
    with pytest.raises(click.BadParameter, match="can't take away"):
        _yours(s, None, None, None, 0, None, False)
    s = sizing.size(_sub("#SBATCH -c 2\n"), apps.Program(None, "x"), [])
    _yours(s, None, "0", None, 0, None, False)
    assert s.opts() == [("mem", "0")]                        # --mem 0: all of a node's; no GPUs: nothing


def test_a_job_whose_script_is_gone_keeps_its_partition_and_time(cluster_here):
    w, sent = cluster_here
    f = Fake(sacct="\n".join(_job("400", "big", "COMPLETED", "long", 8, "cpu=8,mem=16G,node=1", "3-00:00:00",
                                  "2-00:00:00", "10:00:00", "9000M", "2026-10-09T10:00:00", str(w),
                                  "sbatch gone.sh")))
    s = plan(None, job="400", user="amy", run_sacct=f, run_slurm=f).sizing
    assert s.final().partition == "long" and s.final().time_s == 3 * 86400
    assert s.choices["partition"].reason == "it fits long" and not s.problems


def test_cpus_per_gpu():
    req = spec.request_of(_sub("#SBATCH --gpus=2 --cpus-per-gpu=8 --mem-per-cpu=1G\n"))
    assert req.cpus == 16 and req.mem_per_node() == 16384
    s = sizing.size(_sub("#SBATCH --gpus=2 --cpus-per-gpu=8\n"), apps.Program(None, "x"), [],
                    needs=apps.Needs(cpus=4, cpus_from="x"))
    assert not s.choices["cpus"].changed and "per GPU" in s.choices["cpus"].reason


def test_one_task_has_its_gpus_on_one_node():
    for line in ("--gpus-per-task=8", "--tres-per-task=gres/gpu=8", "-n 1 --gpus=8"):
        s = sizing.size(_sub(f"#SBATCH -A chem -p gpu {line}\n"), apps.Program(None, "x"), [],
                        partitions=_parts(), access=ACCESS)
        assert s.problems and "8 GPUs" in s.problems[0] and "none of its nodes has" in s.problems[0], line
    s = sizing.size(_sub("#SBATCH -A chem -p gpu -n 8 --gpus-per-task=1\n"), apps.Program(None, "x"), [],
                    partitions=_parts(), access=ACCESS)
    assert s.problems and "it has 4 GPUs in all" in s.problems[0]


def test_memory_nomad_works_out_is_capped_at_the_biggest_node():
    parts = [p for p in _parts() if p.name in ("basic", "long")]
    s = sizing.size(_sub("#SBATCH -J water --mem=16G -p basic\n"), G16, _ran(3, peak=150 * 1024),
                    script="/home/amy/w/job.sh", partitions=parts, access=ACCESS)
    m = s.choices["mem"]
    assert m.value == 192000 and m.source == "partition" and m.opts == [("mem", "192000M")] and not s.problems
    assert any("would be safer than basic's biggest node" in n for n in s.notes)


def test_your_partition_caps_nomads_time():
    from nomad.submit.cli import _yours
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 20:00:00\n"), G16, _ran(3, elapsed=30 * 3600),
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert s.choices["time"].value == 45 * 3600 and s.choices["partition"].value == "long"
    _yours(s, None, None, None, None, "basic", False)
    assert s.choices["time"].value == 86400 and s.choices["time"].source == "partition" and not s.problems
    assert sum("would be safer than" in n for n in s.notes) == 1
    _yours(s, None, None, None, None, "long", False)                 # then one that gives it all: 45 h again
    assert s.choices["time"].value == 45 * 3600 and not any("would be safer than" in n for n in s.notes)


def test_a_list_of_partitions():
    s = sizing.size(_sub("#SBATCH -p basic,long -t 2-00:00:00 -c 4\n"), apps.Program(None, "x"), [],
                    partitions=_parts(), access=ACCESS)
    assert s.choices["partition"].value == "basic,long" and "(it fits long)" in s.choices["partition"].reason
    assert not s.problems
    s = sizing.size(_sub("#SBATCH -p basic,mixed -t 2-00:00:00 -c 4\n"), apps.Program(None, "x"), [],
                    partitions=_parts(), access=ACCESS)
    assert s.problems and "none of basic, mixed fits it" in s.choices["partition"].reason


@pytest.mark.parametrize("lines, exe, inp, cd", [
    (["cd $WORK", "cd sub", "g16 < w.com"], "g16", "w.com", "?"),
    (["pushd runs", "g16 < w.com"], "g16", "w.com", "runs"),
    (["cd $SLURM_SUBMIT_DIR/runs", "g16 < w.com"], "g16", "w.com", "runs"),
    (['cat <<< "x"', "g16 < w.com"], "g16", "w.com", None),
])
def test_folders_and_here_strings(lines, exe, inp, cd):
    p = apps.program_of(lines)
    assert (p.executable, p.input, p.cd) == (exe, inp, cd)


def test_reading_odd_requests():
    assert apps.needs(apps.program_of(["srun namd2 +p32 run.conf"]), lambda x: None).kind == "processes"
    req = spec.request_of(_sub("#SBATCH --gpus=a100:1,v100:1\n"))
    assert req.gpus == 2 and req.gpu_types == ["a100", "v100"] and req.gpu_type is None
    with pytest.raises(ValueError):
        forms.gpus(req, _sub("#SBATCH --gpus=a100:1,v100:1\n"), 4)
    req = spec.request_of(_sub("#SBATCH --gpus=1\n#SBATCH --gres=lscratch:10\n"))
    assert req.gpus == 1 and req.gpu_form == "gpus"
    assert spec.request_of(_sub("  #SBATCH --mem=4G\n#SBATCH -c 2\n")).mem_form is None   # indented: a comment
    row = _row("1", "1", "x", "   ", "basic", 1, "", "", "", "", "", "", 1, "", "", "", "")
    assert history._runs([dict(zip(history._FIELDS, row.split(SEP)))], None) == []


def test_sizes_on_the_command_line_count_after_cd_to_a_variable(cluster_here):
    w, _ = cluster_here
    (w / "md.sh").write_text("#!/bin/bash\n#SBATCH -c 2\ncd $WORK\ngmx mdrun -nt 8 -s md.tpr\n")
    pl = plan("md.sh", user="amy", run_sacct=Fake(), run_slurm=Fake())
    assert pl.needs is not None and pl.needs.cpus == 8 and any("a variable, cd -" in n for n in pl.notes)


def test_a_relative_script_with_chdir_is_a_guess(tmp_path):
    from nomad.submit.cli import _rerun_script
    (tmp_path / "job.sh").write_text("#!/bin/bash\n")
    assert _rerun_script(spec.parse_submit_line("sbatch --chdir=/elsewhere job.sh"), str(tmp_path)) == \
        (str(tmp_path / "job.sh"), True, 0)
    assert _rerun_script(spec.parse_submit_line("sbatch job.sh"), str(tmp_path)) == \
        (str(tmp_path / "job.sh"), False, 0)


def test_an_array_again_whole(cluster_here):
    from nomad.submit.cli import submit_cmd
    w, sent = cluster_here
    r = CliRunner().invoke(submit_cmd, ["--yes", "--again", "5005"])
    assert r.exit_code == 0, r.output
    assert "--array=0-99" in sent[-1][0] and "All of array 5005 runs again" in r.output


def test_times_for_the_first_run_are_not_sent_again(cluster_here):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    line = "sbatch --begin=2026-10-01T10:00 --deadline=2026-10-02 --dependency=singleton job.sh"
    f = Fake(sacct="\n".join(_job("500", "water", "COMPLETED", "basic", 8, "cpu=8,mem=8G,node=1", "04:00:00",
                                  "01:00:00", "06:00:00", "900M", "2026-10-09T10:00:00", str(w), line)))
    cmd = sc.command(plan(None, job="500", user="amy", run_sacct=f, run_slurm=f), args=[], tagged=False)
    assert "--dependency=singleton" in cmd["argv"]
    assert not any(a.startswith(("--begin", "--deadline")) for a in cmd["argv"])


def test_no_tag_when_slurms_settings_cant_be_read(cluster_here, monkeypatch):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    monkeypatch.setattr(sc.cluster, "extra_is_constraints", lambda run=None: None)
    r = CliRunner().invoke(sc.submit_cmd, ["--yes", "job.sh"])
    assert r.exit_code == 0 and not any(a.startswith("--extra=") for a in sent[-1][0])
    assert "couldn't read Slurm's settings" in r.output


# ── the third review ────────────────────────────────────────────────────────

def test_sinfo_gives_each_kind_of_node():
    f = Fake()
    by = {p.name: p for p in cluster.live(run=f)}
    assert any(c[0] == "sinfo" and "-e" in c for c in f.calls)
    assert sorted(by["mixed"].shapes) == [(64000, 128, 0), (512000, 8, 0)] and by["mixed"].node_mem_mb == 512000
    merged = _merged(SINFO)                                   # what sinfo says without -e
    assert "mixed|64000+|8+|(null)|2" in merged
    s = sizing.size(_sub("#SBATCH -c 8 --mem=500G -t 1:00:00 -p mixed\n"), apps.Program(None, None), [],
                    partitions=_parts(), access=ACCESS)
    assert s.choices["partition"].value == "mixed" and not s.problems
    assert "condo" not in {p.name for p in cluster.live(run=lambda a, timeout=30: Fake()(
        [x for x in a if x != "--all"]))}                     # --all is what brings hidden ones


def test_spread_tasks_keep_memory_per_cpu():
    runs = _ran(3, peak=72 * 1024, cpus=96)                  # 48 tasks a node, 1.5G each
    for r in runs:
        r.nodes, r.tasks = 2, 96
    s = sizing.size(_sub("#SBATCH -J water --ntasks=96 --mem-per-cpu=2G -p basic\n"), G16, runs,
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert not s.choices["mem"].changed and "memory weighed a CPU at a time" in s.choices["mem"].reason
    assert s.choices["mem"].asked_text() == "2G/CPU"
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=96 * 1024, cpus=96, nodes=2)
    s = sizing.size(_sub("#SBATCH --ntasks=96 --mem-per-cpu=2G -p basic\n"), apps.Program(None, "x"), [],
                    outcome=oom, partitions=_parts(), access=ACCESS)
    assert s.choices["mem"].opts == [("mem-per-cpu", "3G")]   # 192G over 96 CPUs, half as much again


def test_a_run_again_is_never_below_what_the_script_now_asks():
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=8192)
    s = sizing.size(_sub("#SBATCH --mem=64G\n"), apps.Program(None, "x"), [], outcome=oom)
    assert not s.choices["mem"].changed and "asks 64G now" in s.choices["mem"].reason
    to = history.PastRun(job_id="9", state="TIMEOUT", time_s=7200, elapsed_s=7200)
    s = sizing.size(_sub("#SBATCH -t 10:00:00\n"), apps.Program(None, "x"), [], outcome=to)
    assert not s.choices["time"].changed and "asks 10:00:00 now" in s.choices["time"].reason
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY")         # nothing recorded, nothing asked
    s = sizing.size(_sub("#SBATCH -c 1\n"), apps.Program(None, "x"), [], outcome=oom)
    assert s.choices["mem"].value is None and any("give --mem yourself" in n for n in s.notes)


def test_a_script_that_moves_itself(tmp_path):
    from nomad.submit.cli import locate
    proj = tmp_path / "proj"
    (proj / "run1").mkdir(parents=True)
    (proj / "job.sh").write_text("#!/bin/bash\n#SBATCH --chdir=run1\n./solver\n")
    (proj / "run1" / "job.sh").write_text("#!/bin/bash\necho another\n")
    # Slurm keeps run1 (where it ran): run1/job.sh, or proj/job.sh moving itself there, could have run it
    assert locate("job.sh", str(proj / "run1"), False) == (str(proj / "run1" / "job.sh"), True)
    (proj / "run1" / "job.sh").unlink()
    assert locate("job.sh", str(proj / "run1"), False) == (str(proj / "job.sh"), False)
    (proj / "abs.sh").write_text(f"#!/bin/bash\n#SBATCH --chdir={proj / 'run1'}\n./solver\n")
    assert locate("abs.sh", str(proj / "run1"), False) == (str(proj / "abs.sh"), True)
    assert locate("job.sh", str(proj), False) == (str(proj / "job.sh"), True)      # its --chdir doesn't lead here


def test_a_capped_memory_per_cpu_fits_the_node():
    parts = [p for p in _parts() if p.name in ("basic", "long")]
    s = sizing.size(_sub("#SBATCH -J water -c 7 --mem-per-cpu=1G -p basic\n"), G16, _ran(3, peak=150 * 1024),
                    script="/home/amy/w/job.sh", partitions=parts, access=ACCESS)
    assert s.choices["mem"].opts == [("mem-per-cpu", "27428M")] and not s.problems   # 192000 / 7, rounded down


def test_gpus_and_other_gres_on_a_job_sent_again(cluster_here):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    f = Fake(sacct="\n".join(_job("600", "water", "COMPLETED", "gpu", 8, "cpu=8,mem=8G,node=1,gres/gpu=2",
                                  "04:00:00", "01:00:00", "06:00:00", "900M", "2026-10-09T10:00:00", str(w),
                                  "sbatch --gpus=2 --gres=lscratch:100 job.sh")))
    pl = plan(None, job="600", user="amy", run_sacct=f, run_slurm=f)
    sc._yours(pl.sizing, None, None, None, 4, None, False)
    argv = sc.command(pl, args=[], tagged=False)["argv"]
    assert "--gres=lscratch:100" in argv and "--gpus=4" in argv and "--gpus=2" not in argv


def test_gpus_on_several_nodes_are_per_node():
    from nomad.submit.cli import _yours
    s = sizing.size(_sub("#SBATCH -N 2\n"), apps.Program(None, "x"), [])
    _yours(s, None, None, None, 4, None, False)
    assert s.opts() == [("gres", "gpu:2")]
    s = sizing.size(_sub("#SBATCH -N 2\n"), apps.Program("AMBER", "pmemd.cuda", gpu_build=True), [])
    assert s.choices["gpus"].value == 2 and s.choices["gpus"].opts == [("gres", "gpu:1")]
    with pytest.raises(click.BadParameter, match="at least one GPU a node"):
        _yours(sizing.size(_sub("#SBATCH -N 2 --gpus=2\n"), apps.Program(None, "x"), []),
               None, None, None, 1, None, False)


def test_defaults_of_the_partition_it_is_moved_to():
    parts = _parts()
    for p in parts:
        if p.name == "condo_guest":
            p.default_time_s = 7200
    one = _ran(1, elapsed=10 * 3600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other", family=None)
    s = sizing.size(_sub("#SBATCH -J water --mem=600G\n"), apps.Program(None, "other"), one, partitions=parts,
                    access=ACCESS)
    assert s.choices["partition"].value == "condo_guest"
    assert s.choices["time"].value == sizing.round_time_up(15 * 3600) and "default (02:00:00)" in s.choices["time"].reason


def test_a_job_on_several_nodes_whose_script_is_gone(cluster_here):
    w, sent = cluster_here
    f = Fake(sacct="\n".join(_job("700", "big", "COMPLETED", "basic", 96, "cpu=96,mem=64G,node=2", "04:00:00",
                                  "01:00:00", "06:00:00", "", "2026-10-09T10:00:00", str(w), "sbatch gone.sh",
                                  nodes=2)))
    s = plan(None, job="700", user="amy", run_sacct=f, run_slurm=f).sizing
    assert s.final().nodes == 2 and s.final().multi_node and not s.choices["partition"].changed


def test_a_script_found_among_the_words(cluster_here):
    import nomad.submit.cli as sc
    w, sent = cluster_here
    f = Fake(sacct="\n".join(_job("800", "my", "COMPLETED", "basic", 4, "cpu=4,mem=8G,node=1", "04:00:00",
                                  "01:00:00", "03:00:00", "", "2026-10-09T10:00:00", str(w),
                                  "sbatch -J my run job.sh")))
    pl = plan(None, job="800", user="amy", run_sacct=f, run_slurm=f)
    assert pl.script == str(w / "job.sh") and pl.line.args == [] and pl.script_unsure
    (w / "in.dat").write_text("1 2 3\n")
    f = Fake(sacct="\n".join(_job("801", "x", "COMPLETED", "basic", 4, "cpu=4,mem=8G,node=1", "04:00:00",
                                  "01:00:00", "03:00:00", "", "2026-10-09T10:00:00", str(w),
                                  "sbatch gone.sh in.dat")))
    assert plan(None, job="801", user="amy", run_sacct=f, run_slurm=f).script is None   # data isn't a script


def test_third_review_low_ones():
    from nomad.submit.cli import _yours
    assert apps.program_of(["exec > run.log 2>&1", "./solver in"]).executable == "solver"
    assert apps.program_of(["cd /x", "cd -", "g16 < w.com"]).cd == "?"
    assert forms.cpus(spec.request_of(_sub("#SBATCH -n 4 -c 4\n")), 8, "asked")[0] == [("ntasks", "2")]
    with pytest.raises(ValueError, match="multiple of 4"):
        forms.cpus(spec.request_of(_sub("#SBATCH -n 4 -c 4\n")), 6, "asked")
    req = spec.request_of(_sub("#SBATCH --tres-per-task=cpu=8\n"))
    assert req.cpus_per_task == 8 and req.cpus_tres
    s = sizing.size(_sub("#SBATCH --tres-per-task=cpu=8\n"), apps.Program(None, "x"), [],
                    needs=apps.Needs(cpus=4, cpus_from="x"))
    assert not s.choices["cpus"].changed and "--tres-per-task" in s.choices["cpus"].reason
    with pytest.raises(click.BadParameter):
        _yours(s, 4, None, None, None, None, False)
    s = sizing.size(_sub("#SBATCH -J water -t UNLIMITED -p long\n"), G16, _ran(3, elapsed=600),
                    script="/home/amy/w/job.sh", partitions=_parts(), access=ACCESS)
    assert s.choices["time"].value == "UNLIMITED" and not s.choices["time"].changed
    assert s.problems and "the job asks no limit" in s.problems[0]


# ── the fourth review ───────────────────────────────────────────────────────

def test_spread_tasks_take_the_inputs_memory_a_cpu_at_a_time():
    needs = apps.gaussian("%nprocshared=8\n%mem=16GB\n#p\n")      # threads: sent as one task, not spread
    s = sizing.size(_sub("#SBATCH --ntasks=4 --mem-per-cpu=2G\n"), apps.Program("Gaussian", "g16", input="w"), [],
                    needs=needs)
    assert ("mem-per-cpu", "2560M") in s.opts() and s.final().mem_per_node() == 20480
    needs = apps.needs(apps.Program("ORCA", "orca", input="x.inp"), lambda p: "! PAL8\n%maxcore 3000\n")
    s = sizing.size(_sub("#SBATCH --ntasks=8 --mem-per-cpu=1G\n"), apps.Program("ORCA", "orca", input="x.inp"),
                    [], needs=needs)
    m = s.choices["mem"]
    assert m.opts == [("mem-per-cpu", "3776M")] and m.value_text() == "3.7G/CPU"   # 3000 × 1.25 a process


def test_spread_tasks_keep_memory_per_cpu_when_cpus_change():
    from nomad.submit.cli import _yours
    s = sizing.size(_sub("#SBATCH --ntasks=4 --mem-per-cpu=2G\n"), apps.Program(None, "x"), [])
    _yours(s, 8, None, None, None, None, False)
    assert s.opts() == [("ntasks", "8")]                              # 2G a CPU still
    s = sizing.size(_sub("#SBATCH -n 2 --gpus-per-task=1 --mem-per-gpu=10G\n"), apps.Program(None, "x"), [])
    _yours(s, None, None, None, 4, None, False)
    assert s.opts() == [("gpus-per-task", "2")]
    with pytest.raises(click.BadParameter, match="--mem is per node"):
        _yours(s, None, "64G", None, None, None, False)
    s = sizing.size(_sub("#SBATCH --ntasks=96 --mem-per-cpu=3G -p basic -t 1:00:00\n"), apps.Program(None, "x"),
                    [], partitions=_parts(), access=ACCESS)
    assert not s.problems and not s.choices["partition"].changed     # Slurm fits 48 tasks a node on basic


def test_a_job_on_several_nodes_that_ran_out():
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=64 * 1024, nodes=2)
    s = sizing.size(_sub("#SBATCH -N 2 --mem=128G\n"), apps.Program(None, "x"), [], outcome=oom)
    assert not s.choices["mem"].changed and "ran out of memory with 64G" in s.choices["mem"].reason
    s = sizing.size(_sub("#SBATCH -N 2 --mem=64G\n"), apps.Program(None, "x"), [], outcome=oom)
    assert s.choices["mem"].opts == [("mem", "96G")]


def test_your_partition_weighs_what_the_job_doesnt_ask():
    from nomad.submit.cli import _yours
    parts = _parts()
    for p in parts:
        if p.name == "long":
            p.default_time_s = 3600
    one = _ran(1, elapsed=14 * 3600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other", family=None)
    s = sizing.size(_sub("#SBATCH -J water\n"), apps.Program(None, "other"), one, partitions=parts, access=ACCESS)
    _yours(s, None, None, None, None, "long", False)
    assert s.choices["time"].value == sizing.round_time_up(21 * 3600) and "default (01:00:00)" in s.choices["time"].reason


def test_a_gpu_job_on_several_nodes_whose_script_is_gone(cluster_here):
    w, sent = cluster_here
    f = Fake(sacct="\n".join(_job("701", "big", "COMPLETED", "gpu", 16, "cpu=16,mem=64G,node=2,gres/gpu=8",
                                  "04:00:00", "01:00:00", "06:00:00", "", "2026-10-09T10:00:00", str(w),
                                  "sbatch gone.sh", nodes=2)))
    s = plan(None, job="701", user="amy", run_sacct=f, run_slurm=f).sizing
    assert s.final().gpus == 8 and s.final().nodes == 2


def test_no_limit_and_tasks_per_node_on_several_nodes():
    for t in ("0", "-1", "UNLIMITED"):
        s = sizing.size(_sub(f"#SBATCH -J water -t {t}\n"), G16, _ran(1, elapsed=600), script="/home/amy/w/job.sh")
        assert s.choices["time"].value == "UNLIMITED" and not s.choices["time"].changed, t
    req = spec.request_of(_sub("#SBATCH -N 2 --ntasks-per-node=4 -c 4\n"))
    assert forms.cpus(req, 64, "asked")[0] == [("ntasks-per-node", "8")]
    with pytest.raises(ValueError, match="multiple of 8"):
        forms.cpus(req, 36, "asked")


def test_a_cap_for_another_partition_names_it():
    from nomad.submit.cli import _yours
    parts = [p for p in _parts() if p.name in ("basic", "mixed")]
    s = sizing.size(_sub("#SBATCH -J water -c 8 -t 20:00:00 -p basic\n"), G16, _ran(3, elapsed=30 * 3600),
                    script="/home/amy/w/job.sh", partitions=parts, access=ACCESS)
    assert "basic allows at most" in s.choices["time"].reason
    _yours(s, None, None, None, None, "mixed", False)
    assert "mixed allows at most" in s.choices["time"].reason and "basic" not in s.choices["time"].reason
    assert sum("would be safer than mixed" in n for n in s.notes) == 1
    _yours(s, None, None, None, None, "secret", False)                # not listed: Slurm checks it
    assert s.choices["time"].value == 45 * 3600 and not any("would be safer" in n for n in s.notes)


def test_memory_of_a_whole_node_per_cpu_is_never_past_it():
    parts = [p for p in _parts() if p.name in ("basic", "long")]
    for p in parts:
        p.shapes, p.node_mem_mb = [(196608, 48, 0)], 196608
    s = sizing.size(_sub("#SBATCH -J water -c 7 --mem-per-cpu=1G -p basic\n"), G16, _ran(3, peak=128 * 1024),
                    script="/home/amy/w/job.sh", partitions=parts, access=ACCESS)
    assert s.choices["mem"].value == 196608 and s.choices["mem"].opts == [("mem-per-cpu", "28086M")]
    assert not s.problems



# ── the fifth review ────────────────────────────────────────────────────────

def test_spread_tasks_must_fit_the_partition_in_all():
    s = sizing.size(_sub("#SBATCH -A chem --ntasks=60 --mem-per-cpu=8G -p gpu -t 1:00:00\n"),
                    apps.Program(None, "x"), [], partitions=_parts(), access=ACCESS)
    gpu = next(f for f in s.fits if f["partition"] == "gpu")
    assert "its nodes hold at most 46 such tasks" in gpu["why"]                      # 375G / 8G
    assert s.choices["partition"].value == "basic" and not s.problems              # 4 nodes × 23
    parts = [p for p in _parts() if p.name == "mixed"]
    s = sizing.size(_sub("#SBATCH --ntasks=100 --mem-per-cpu=4G -p mixed -t 1:00:00\n"),
                    apps.Program(None, "x"), [], partitions=parts, access=ACCESS)
    assert s.problems and "at most 23 such tasks" in s.problems[0]                   # 8 + 15


def test_several_nodes_with_memory_per_cpu_that_ran_out():
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=15 * 1024, cpus=3, nodes=2)
    s = sizing.size(_sub("#SBATCH -N 2 -n 3 --mem-per-cpu=10G\n"), apps.Program(None, "x"), [], outcome=oom)
    assert s.choices["mem"].opts == [("mem-per-cpu", "15G")] and s.choices["mem"].unit == "CPU"


def test_your_cpus_and_keep_on_memory_per_cpu():
    from nomad.submit.cli import _yours
    oom = history.PastRun(job_id="9", state="OUT_OF_MEMORY", mem_mb=96 * 1024, cpus=96, nodes=2)
    s = sizing.size(_sub("#SBATCH --ntasks=96 --mem-per-cpu=2G -p basic\n"), apps.Program(None, "x"), [],
                    outcome=oom, partitions=_parts(), access=ACCESS)
    _yours(s, 1, None, None, None, None, False)
    assert ("mem-per-cpu", "3G") in s.opts() and not any("would be safer" in n for n in s.notes)
    s = sizing.size(_sub("#SBATCH --ntasks=96 --mem-per-cpu=2G -p basic\n"), apps.Program(None, "x"), [],
                    outcome=oom, partitions=_parts(), access=ACCESS)
    _yours(s, None, None, None, None, None, True)
    assert s.choices["mem"].value_text() == "2G/CPU" and not s.opts()
    # --cpus that makes Gaussian's one task several: memory a CPU at a time from then on
    needs = apps.gaussian("%nprocshared=8\n%mem=16GB\n#p\n")
    s = sizing.size(_sub("#SBATCH -n 2 --mem-per-cpu=1G\n"), apps.Program("Gaussian", "g16", input="w"), [],
                    needs=needs)
    _yours(s, 16, None, None, None, None, False)
    assert s.choices["mem"].unit == "CPU" and ("ntasks", "16") in s.opts()


def test_the_inputs_memory_over_the_cpus_sent():
    needs = apps.needs(apps.Program("ORCA", "orca", input="x.inp"), lambda p: "! PAL4\n%maxcore 4000\n")
    s = sizing.size(_sub("#SBATCH -n 4 -c 2 --mem-per-cpu=1G\n"), apps.Program("ORCA", "orca", input="x.inp"),
                    [], needs=needs)
    assert ("ntasks", "4") in s.opts() and ("cpus-per-task", "1") in s.opts()
    assert s.choices["mem"].opts == [("mem-per-cpu", "5056M")]       # 4000 × 1.25 a process, a CPU each


def test_tasks_in_all_with_a_most_a_node():
    req = spec.request_of(_sub("#SBATCH -n 8 --ntasks-per-node=4\n"))
    assert forms.cpus(req, 16, "asked")[0] == [("ntasks", "16")]


@pytest.mark.parametrize("t", ["00:00:00", "0:0", "0-0", "00"])
def test_a_time_of_zero_written_any_way_is_no_limit(t):
    assert spec.request_of(_sub(f"#SBATCH -t {t}\n")).time_unlimited


def test_weighing_again_clears_old_cap_notes_and_takes_unlisted_partitions():
    from nomad.submit.cli import _yours
    parts = [p for p in _parts() if p.name in ("basic", "long")]
    one = _ran(1, elapsed=30 * 3600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other", family=None)
    s = sizing.size(_sub("#SBATCH -J water -p basic\n"), apps.Program(None, "other"), one, partitions=parts,
                    access=ACCESS)
    _yours(s, None, None, None, None, "long", False)
    assert not any("would be safer than basic" in n for n in s.notes)
    one = _ran(1, elapsed=14 * 3600, script="/home/amy/x/x.sh", workdir="/home/amy/x", exe="other", family=None)
    s = sizing.size(_sub("#SBATCH -J water\n"), apps.Program(None, "other"), one, partitions=parts, access=ACCESS)
    _yours(s, None, None, None, None, "secret", False)
    assert s.choices["time"].reason.startswith("not asked: the partition's default limit applies")
