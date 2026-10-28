# Sizing and submitting jobs

`nomad size` says what a job should ask for, and `nomad submit` sends it with that. People run them themselves, on a cluster's login node: nomad reads their script, the input file it names, their own past jobs from Slurm's accounting, and the cluster's partitions. Nothing is stored, and nothing needs changing in Slurm.

```
nomad size job.sh                 what job.sh should ask for
nomad size --job 4815162          a job of yours that ran, to run it again
nomad submit job.sh [ARGS...]     size it, show the sbatch command, ask, submit
nomad submit --again 4815162      a job of yours, sent again (an array: 5005, or one task: 5005_7)
nomad submit --mem 12G job.sh     your own value for memory (and --cpus, --time, --gpus, --partition)
nomad submit --keep job.sh        the script's own values: nomad only checks and tags
```

Options for `nomad submit` go before the script, as with sbatch; what follows the script is its arguments (nomad warns, and won't submit without asking, when one of its own options comes after the script). `--dry-run` shows the command without running it, and `--yes` submits without asking.

## What it shows

```
job.sh: Gaussian (g16), input w.com
  compared with your 5 runs of this script

             asked         nomad         why
  CPUs       4             8*            the input sets 8 threads of one task (%nprocshared=8): 4 CPUs would make them share cores
  memory     8G            20G*          what the input sets (%mem 16 GB, and a fifth more: Gaussian needs room beyond %mem); your 3 runs of this script peaked at 10.7G
  time       04:00:00      02:30:00*     the longest of your 3 runs of this script took 01:30:00: a shorter limit starts sooner
  GPUs       -             -             none asked
  partition  basic         basic         it fits basic
  (* nomad would change it)
```

Memory is per node. Each value says where it comes from:

- **The input file or the command line**, for programs that state their sizes there:

  | Program | CPUs | Memory |
  |---------|------|--------|
  | Gaussian | `%nprocshared` (`%nproc`, `%cpu`): threads | `%mem`, and a fifth more |
  | ORCA | `%pal nprocs`, `! PALn`: processes | `%maxcore` × processes, and a quarter more |
  | Q-Chem | `qchem -nt` (threads) or `-np` (processes) | `MEM_TOTAL` in `$rem` (for each process), and a fifth more |
  | Columbus | `runc -nproc`: processes | `runc -m` (for each process), and a fifth more |
  | GROMACS | `mdrun -nt`, or `-ntmpi` × `-ntomp`: threads | — |
  | NAMD | `+pN`: processes under charmrun or mpirun, else threads | — |

  Threads are asked as one task with that many CPUs (`--ntasks=1 --cpus-per-task=N`), processes as that many tasks of a CPU each (`--ntasks=N`): a job asking the other way is told so. A value given by a shell variable (`-nt $SLURM_CPUS_PER_TASK`) is left to the job's own request. The input file is read where the job will run: the folder sbatch is run in (or `--chdir`), after a literal `cd` in the script; nomad says when it is only found next to the script.
- **How the job ended**, for one run again: out of memory, half as much memory again; out of time, half as long again; or what the input says, or your past runs of it needed, or what its script asks now, whichever is more. A job that stopped with an error is sized as usual, with a note that more resources won't fix it; one whose node failed or that was preempted runs as it was.
- **Your past runs like this one**: the same script; else the same program in the very same folder; else the same job name; else the same program. An interpreter (python, R, Julia, MATLAB) or a program nomad doesn't know isn't "the same program": two python scripts can be anything. Memory is the highest peak × 1.5, time the longest run × 1.5 (the room nomad's scores ask for), rounded to values people type. History may *lower* a value only from at least three runs that completed, of the same script or of the same program in the same folder; the weaker matches (the same job name, the same program anywhere) may only *raise* one. A value the job doesn't ask for is weighed against the partition's default (its DefaultTime, else MaxTime; DefMemPerNode or DefMemPerCPU): where that isn't known, only runs close enough to lower a value set one. A run that failed never counts, nor a memory reading larger than its nodes have (Slurm can count memory processes share more than once). CPUs are lowered only as CPUs per task of a one-task job, never as tasks (an MPI launch counts on them), and only from runs that had at least as many CPUs as now asked. Where the runs of this script had other arguments, nomad says so.
- **The program**: an executable built for GPUs (`pmemd.cuda`, `mdrun -nb gpu`) gets one when none is asked.
- **The partition**: when the one asked (or the default) doesn't fit -- too long for its limit, no node with the CPUs, memory and GPUs one node needs *together* (a task's GPUs are on its node), too few GPUs in all, or one the job's account may not use (its AllowGroups, AllowAccounts; the job's `--account`, else the person's default account) -- nomad names one that fits, preferring ones the person may use, not preemptible, with the smallest nodes. A partition Slurm doesn't list is kept (Slurm checks it), and one whose nodes couldn't be read isn't offered. With a list (`-p a,b`), Slurm picks; nomad checks that one of them fits. A time or memory nomad works out that the partition can't give is asked at what it gives (its time limit, its biggest node), with a note.

A job on several nodes (`--nodes` above 1) keeps its CPUs and memory; nomad checks its time and partition. Tasks Slurm places over as many nodes as it takes (`--ntasks` without `--nodes`), or on several, with memory per CPU or per GPU have no one node's figure: their memory is weighed a CPU (or GPU) at a time, and shown so (`2G/CPU`) -- the input's memory over the CPUs the job will have, or, when the job ran out, half as much again a CPU from Slurm's record (never below what the script asks now); past runs' readings (a node at a time) aren't weighed, a change of their CPUs keeps memory per CPU, and a partition fits them when its nodes hold them all, a task (its CPUs and their memory) at a time. `--ntasks-per-node` alone (no `--ntasks`, no `--nodes`) is that many tasks on one node; with `--ntasks`, it is the most a node takes. CPUs asked per GPU (`--cpus-per-gpu`) or through `--tres-per-task=cpu=N` are kept, and so is no time limit (`--time=UNLIMITED`, `0` or `-1`; nomad says when a partition has a limit).

nomad never changes how a job asks: memory asked per CPU or per GPU stays so (rounded up to 64 MB, or down to whole megabytes when it is a node's memory: sbatch takes no fractions), GPUs stay `--gpus`, `--gpus-per-node`, `--gpus-per-task`, `--tres-per-task` or `--gres`, with their type and the job's other gres. Memory is decided per node: when CPUs or GPUs change (nomad's or yours), memory asked per CPU or per GPU is written again for them, so the node gets the memory the table shows. `#SBATCH` lines are read as sbatch reads them: at the very start of a line (an indented one is a comment), before the first command.

## Submitting

`nomad submit` shows the same table, then the exact sbatch command, and asks. Only the values that change are set on the command line (sbatch reads the rest from the script's `#SBATCH` lines), and what is sent is checked again: a value you give that no node can hold is said before you submit. sbatch runs in the folder you run nomad in, as it would by hand (a relative `--chdir` in the script is its own). `--cpus` sets CPUs the way the script asks them (CPUs per task for one task, else tasks; tasks of several CPUs each keep their CPUs per task: a multiple of them; tasks per node stay per node: a multiple of the nodes); `--gpus` too (per task or per node, `--gres` on several nodes included: a multiple of the tasks or nodes; `--gpus` on several nodes: at least one a node). nomad can't take an option away: `--gpus 0` on a script that asks for GPUs says to remove its line. `--mem` is per node (`0`: all of a node's memory), so it isn't taken for tasks Slurm places over its nodes with memory per CPU or per GPU: change that in the script. What the job doesn't ask (time, memory) is weighed against the defaults of the partition it goes to, yours included.

A job sent again gets its own command line's other options and its folder (`--chdir`), and runs its script as it is on disk now (nomad says when it changed since). Slurm keeps the folder the job ran in, not the one sbatch ran in: a script that moves itself (`#SBATCH --chdir`) is found in the folder its `--chdir` leads from. Not sent again: its `--dependency` (on jobs long gone; `singleton` is kept), `--begin` and `--deadline` (times for its first run), `--wait`, and an array's other tasks (`--again 5005_7` runs task 7 alone, as `--array=7`; `--again 5005` runs the whole array again). A job nomad sent before gets a new tag, and values it keeps from that job's own command line keep the code they had. Slurm keeps the command line without the quotes it was typed with, so a job sent again with arguments, or whose script nomad had to find among the line's words (or found from a folder Slurm doesn't keep, or where two scripts could have been the one), is never submitted without asking.

The job is tagged, in Slurm's `--extra` field, with who chose each value:

```
--extra=nomad=1.18.0;via=cli;cpus=i;mem=i;time=h;gpus=d;part=s
```

`s` the script's own, `u` the person on nomad's command line, `h` nomad from past runs, `i` the input file or command line, `o` how the job run again ended, `g` what the program needs, `p` a partition that fits, `d` left to Slurm's defaults; `again=N` for a job sent again. Slurm keeps it in its accounting where `AccountingStoreFlags` includes `job_extra`, so nomad can later tell the person's choices from its own. No tag where Slurm is older than 23.02 (no `--extra`; and should sbatch refuse it anyway, nomad sends the job once more untagged), where `SchedulerParameters` include `extra_constraints` (Slurm then reads `--extra` as node constraints) or nomad can't read them, or where the job sets `--extra` itself.

## What it reads

- **The script**, and the input file it names (at most 2 MB), as the person: their own run of nomad reads their own files.
- **Slurm's accounting**: the person's own jobs of the last 180 days (`--days`), with `sacct -u` and the person's own user name (from the system, never from `$USER`). The full folder and submit line let nomad find each past job's script and name its program. Slurm before 21.08 has no submit line: jobs are then compared by name only.
- **The partitions**: `scontrol --all show partition` and `sinfo -a -e` (hidden ones too; each kind of node on its own line), the person's groups and Slurm accounts (`sacctmgr show assoc`, `show user`).

`nomad size --db PATH --user NETID` sizes a script from nomad's database instead (on the hub, for staff), where folders and command lines are never kept whole, so jobs are compared by name only (and so only raise values); `--job` looks at one's own jobs only.

## Installing it for users

Users run one central install on each cluster, not one of their own: for example a virtual environment owned by the site's account in `/usr/local/sw/nomad` (not group-writable), with `nomad` on users' PATH. `nomad size` and `nomad submit` need no nomad.toml.
