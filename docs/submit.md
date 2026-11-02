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

nomad never changes how a job asks: memory asked per CPU or per GPU stays so (rounded up to 64 MB, or down to whole megabytes when it is a node's memory: sbatch takes no fractions), GPUs stay `--gpus`, `--gpus-per-node`, `--gpus-per-task`, `--tres-per-task` or `--gres`, with their type and the job's other gres. Memory is decided per node: when CPUs or GPUs change (nomad's or yours), memory asked per CPU or per GPU is written again for them, so the node gets the memory the table shows. `#SBATCH` lines (and `#SLURM`, `#SBATCH--x`) are read as sbatch reads them: at the very start of a line (an indented one is a comment), before the first command.

## Submitting

`nomad submit` shows the same table, then the exact sbatch command, and asks. Only the values that change are set on the command line (sbatch reads the rest from the script's `#SBATCH` lines), and what is sent is checked again: a value you give that no node can hold is said before you submit. sbatch runs in the folder you run nomad in, as it would by hand (a relative `--chdir` in the script is its own). `--cpus` sets CPUs the way the script asks them (CPUs per task for one task, else tasks; tasks of several CPUs each keep their CPUs per task: a multiple of them; tasks per node stay per node: a multiple of the nodes); `--gpus` too (per task or per node, `--gres` on several nodes included: a multiple of the tasks or nodes; `--gpus` on several nodes: at least one a node). nomad can't take an option away: `--gpus 0` on a script that asks for GPUs says to remove its line. `--mem` is per node (`0`: all of a node's memory), so it isn't taken for tasks Slurm places over its nodes with memory per CPU or per GPU: change that in the script. What the job doesn't ask (time, memory) is weighed against the defaults of the partition it goes to, yours included.

A job sent again gets its own command line's other options and its folder (`--chdir`), and runs its script as it is on disk now (nomad says when it changed since). Slurm keeps the folder the job ran in, not the one sbatch ran in: a script that moves itself (`#SBATCH --chdir`) is found in the folder its `--chdir` leads from. Not sent again: its `--dependency` (on jobs long gone; `singleton` is kept), `--begin` and `--deadline` (times for its first run), `--wait`, and an array's other tasks (`--again 5005_7` runs task 7 alone, as `--array=7`; `--again 5005` runs the whole array again). A job nomad sent before gets a new tag, and values it keeps from that job's own command line keep the code they had. Slurm keeps the command line without the quotes it was typed with, so a job sent again with arguments, or whose script nomad had to find among the line's words (or found from a folder Slurm doesn't keep, or where two scripts could have been the one), is never submitted without asking.

The job is tagged, in Slurm's `--extra` field, with who chose each value:

```
--extra=nomad=1.18.0;via=cli;cpus=i;mem=i;time=h;gpus=d;part=s
```

`s` the script's own, `u` the person on nomad's command line, `h` nomad from past runs, `i` the input file or command line, `o` how the job run again ended, `g` what the program needs, `p` a partition that fits, `d` left to Slurm's defaults; `again=N` for a job sent again; `stage=l` for one sent through local scratch. Slurm keeps it in its accounting where `AccountingStoreFlags` includes `job_extra`, so nomad can later tell the person's choices from its own. No tag where Slurm is older than 23.02 (no `--extra`; and should sbatch refuse it anyway, nomad sends the job once more untagged), where `SchedulerParameters` include `extra_constraints` (Slurm then reads `--extra` as node constraints) or nomad can't read them, or where the job sets `--extra` itself.

### Where the output goes

Each job's Slurm output (its `.out` file, with what it writes to stderr unless the job splits them) goes in a folder of its own, `/scratch/USER/jobs/JOBID-NAME/slurm-JOBID.out` (`slurm-JOBID_TASK.out` for each task of an array), so the folder the job runs in fills with its own results only. nomad says where, before and after submitting.

Slurm never makes the folder an output file goes in (a job whose folder is missing fails with no output at all), and a job's number is known only once it is submitted. So nomad sends the job to start two minutes from now (`--begin=now+120`), reads back from Slurm where its output would go, makes its folder, and in one update (`scontrol update ... StdOut=... StartTime=now`) points its output there and lets it start at once. Should nomad be stopped in between (Ctrl-C, a dropped ssh session, a kill), or Slurm not take the folder, nothing is left waiting: the job starts on its own within the two minutes, its output where Slurm puts it. nomad never holds or releases a job, and a start time a site's `job_submit` plugin set is left as it is. What nomad then says is what Slurm holds (an update can report an error after it took effect: nomad reads the job back). The command nomad shows is the one to run by hand, without its `--begin`.

The job's number and name are Slurm's own (`JobName`, after `-J`, `$SBATCH_JOB_NAME` or the script's name), kept to letters, digits and `. _ + -` in the folder's name; a folder already there (a job number Slurm used before) gets `.2`, `.3`, ... `jobs/` is made private (`0700`): change its mode to share it (folders you give with `--logs` follow your umask). Scratch may be purged after a while: so are the logs in it.

Left where the job or Slurm says:

- a job that sets its own output: `#SBATCH --output` or `--error` (or `--out=`: sbatch takes any prefix that is only one option's), `#PBS -o` or `-e`, its command line's (a job sent again), `$SBATCH_OUTPUT` or `$SBATCH_ERROR`; and an output a site's `job_submit` plugin set: nomad changes it only when Slurm reports its own default (`slurm-JOBID.out` in the job's folder);
- a job that sets its own start time (`--begin`, `#PBS -a`) or a `--deadline` (a later start could miss it), one sbatch waits for (`--wait`, `$SBATCH_WAIT`), a `--test-only`, a heterogeneous job;
- the person who has no `/scratch/USER` (nomad never makes it), or whose `/scratch/USER` isn't on a filesystem compute nodes share (NFS, Lustre, GPFS, BeeGFS, Ceph, ...; a login node that serves `/scratch` itself sees its own disk: a site where it is shared anyway sets `NOMAD_LOGS`): the output goes beside the job, and nomad says so.

A job that asks to be held (`#SBATCH --hold`) gets its folder and stays held, for the person to release. A job sent again doesn't get nomad's `--begin` (or `--quiet`) from its own command line.

The choice, the first one given: `nomad submit --logs`, then `$NOMAD_LOGS`, then the default. Each is `here` (Slurm's own place, beside the job), a folder (each job's own folder goes in it: `--logs ~/runs` gives `~/runs/JOBID-NAME/`; a relative one is from the folder you run nomad in), or a pattern with `{user}`, `{jobid}` and `{name}` (`{jobid}` is required: each job its own folder). A site can set `NOMAD_LOGS` for everyone (`/etc/profile.d`), and a person in their own shell setup; `--dry-run` shows where it will go (`JOBID` for the number to come).

### Through local scratch

A job on one node runs through its node's own disk (1.24): faster for the program, and lighter on the shared `/scratch`. `nomad submit` sends nomad's own small script in place of the person's, and on the node the job:

1. makes `/localscratch/USER/job_JOBID` (private, `0700`; `/localscratch/USER` is made if it isn't there), after checking the node has room for the job's folder and still keeps free the larger of 50 GB and a tenth of that disk (local scratch is often a folder on the node's own `/`). It holds a lock on it (`/localscratch/USER/job_JOBID.lock`) for as long as any of the job's processes lives;
2. copies the job's folder in (where the job runs: the folder nomad is run in, or its `--chdir`) and notes each file's size, times and inode, in the folder and in the copy (and what each small file held). A relative link that leads out of the folder becomes an absolute link to where it led, so reads and writes through it go where they always went;
3. runs the person's script there, as it was when sent (the way sbatch keeps a script), with its own `#!` line, arguments, standard input, signals and locale; `$SLURM_SUBMIT_DIR` is the local folder (a script that does `cd $SLURM_SUBMIT_DIR` stays in it), `$NOMAD_SUBMIT_DIR` the folder it came from. An `sbatch` the script runs indirectly (in another script it calls) whose job would work in the node's copy is sent to work in the same folder where the job came from (a `--chdir` added just before its script, and `-o`/`-e`/`-i` paths into the copy mapped the same way); one that works elsewhere (its own `-D` or `#SBATCH --chdir` outside the copy) is sent as it is; one that would work in a folder that is only in the copy (made by this job: it isn't in the folder until this job ends) is refused, saying so. Should a job still be sent from the copy (Slurm's own `sbatch` first on a PATH a script resets), it is moved to that folder while it waits (`scontrol update WorkDir=`), or else the copy is kept, and what that job writes in it is brought back (`nomad-recovered-*`) once it has ended;
4. when the script ends, stops what it left running in the background (as Slurm would at the job's end), then copies **what is new or changed** back to the folder it ran from, as if it had run there: what it didn't change isn't written again, and each file is written beside and renamed in place, so a copy cut short never leaves half a file. Files it deleted or renamed there aren't deleted or renamed in the folder. **What changed in the folder while the job ran is never undone**: a file the person or another job changed there meanwhile is left as it is, and the job's version goes in `nomad-conflicts-JOBID/` in the job's folder (the job then ends with exit code 75, and its `.out` says which); a file of lines both only added to at its end (a log, a table of rows) gets the job's new lines added after the others' (never a file that was empty, or whose beginning either one changed); a file only touched or `chmod`ed there meanwhile (the same content) isn't a conflict, and keeps its new mode. Jobs from one folder copy back one at a time (a lock on the folder's `.nomad-lock`, which stays there, hidden, writable by all so a lab's members' jobs take turns too). Where that lock can't be shared between nodes (NFS mounted `nolock` or `local_lock=`), can't be had, or another job holds it for over 15 minutes, the copy back goes file by file and checks each one just before it goes in;
5. checks each file it copied back (sha256 of both copies, the one on `/scratch` read back from the server, not the node's cache; a link's target; a folder), copies again what doesn't match, and only then removes the local folder (and keeps it while any process still works in it). What still doesn't match stays on the node, the job's `.out` says what and where, a job pinned to that node brings it back once this one ends (see below), and a job that had succeeded ends with exit code 75, so Slurm records it as failed.

The job's `.out` says what it did (`nomad-stage: copied ... to /localscratch/amy/job_4242 on spdr05 in 12s`, `copied back 3 new or changed item(s) (1.2 GB) ... and checked each (sha256)`). It uses `rsync` where the node has it (else Python's own copy) and the node's own `python3` (`/usr/bin/python3`, isolated: `-I`, so a cluster's `PYTHONHOME` or `PYTHONPATH` don't reach it); nothing of nomad's is needed on the node. nomad's own steps run without the job's `LD_LIBRARY_PATH` and `LD_PRELOAD` (a login environment's can break system tools: 1.24.1); the script gets them back as they were.

**Time limits and stops.** nomad asks Slurm to warn the job before its limit (`#SBATCH --signal=B:USR1@600`, a sixth of a limit under an hour but at least 5 minutes; not when the script sets its own `--signal`). At the warning the job stops the program (TERM to it and everything it started, KILL after a minute), copies back what it has, checks it, then waits for the limit, so Slurm still records the job as `TIMEOUT` (and nomad's sizing reads it as one). `scancel -b -s USR1 JOBID` does the same at any time and ends the job there. A job cancelled (`scancel`) or preempted: Slurm has already sent TERM to all of it and KILLs it after `KillWait` (30 s on spydur); the program keeps 15 s of that, the copy back the rest. A script's own `--signal=B:SIG` still reaches it (nomad's script passes it on).

**What a stopped job leaves.** When a job is stopped, or its copy back fails, it first sends a small job of its own (`nomad-recover-JOBID`: one CPU, 1 GB, 2 hours, pinned to that node with `-w`, `--dependency=afterany:JOBID`, its output in the job's folder). That job, once the first has ended, brings back what is still on the node; so does the person's next staged job on that node (up to three such folders, when it has 15 minutes left). Either one takes only a folder whose lock is free (its job is gone), and puts what it brings back in a folder of its own, `nomad-recovered-NODE-TIME/`, in the job's folder (`/scratch/USER/jobs/JOBID-NAME/`, beside its `.out`; else where the job ran): **nothing recovered ever overwrites anything**. A folder an earlier run of the same job left (a job requeued onto the same node) is moved aside for that, never copied over the new run's results.

**Sent as it is**, and nomad says why: a job on several nodes (each would have its own local scratch); an array (its tasks would each copy the folder, with what the others brought back: `--localscratch` stages it anyway, up to a tenth of the size limit; an array that comes from `$SBATCH_ARRAY_INX` is caught on the node); a time limit under 30 minutes (too short to gain from it, and to copy back before the limit); a script that leaves its folder itself (`cd /abs/path`, `cd ~`, `cd ..` or any `..`, `cd $OTHER`, `cd -`, `popd`; `cd $SLURM_SUBMIT_DIR` and folders inside it are fine) or sends jobs itself (`sbatch` anywhere in its commands, `nomad submit`, Snakemake, Nextflow, Cromwell, Toil, FireWorks, Parsl: a job sent from the node's copy would start there); a folder larger than 50 GB (`$NOMAD_STAGE_MAX`, e.g. `NOMAD_STAGE_MAX=200G`); the person's home folder itself (submit from a project's folder); a script without a `#!` line or with DOS line breaks, `--wrap`, `--test-only` and heterogeneous jobs. On the node, the job runs where it is (as without nomad) when the node has no `/localscratch`, no `python3`, not enough room, or Slurm gave it several nodes. `--no-localscratch` sends a script as it is; `NOMAD_LOCALSCRATCH=off` does it for every job (a site in `/etc/profile.d`, a person in their own shell setup), and `--localscratch` turns it on for one job even then. `NOMAD_LOCALSCRATCH=/local/{user}/scratch` puts the local folder elsewhere (`{user}` required). The jobs folder (`/scratch/USER/jobs`) is never copied in, when the job runs from a folder that holds it.

**nomad's copy of the script.** What sbatch is given is nomad's script, kept in `~/.local/share/nomad/staged/NAME-HASH.sh` (private; the same job sent again is the same file; those unused for 400 days are removed), named as sbatch would have named the job (`--job-name=` the script's name, unless it names itself). Its first lines say which script it carries, and it carries that script's text, so nomad still knows the person's own script for a job sent this way: `nomad submit --again` runs the person's script (staged again), and past runs are compared by it. The tag records it: `;stage=l`.

What runs differently from an unstaged job: results appear in the folder when the job ends, not while it runs (watch the local folder on the node, or the `.out`); a file another job or the person changed in the folder meanwhile keeps their change, the job's own version set aside (above); a job that restarts from its own checkpoints after a requeue starts again from the folder, so it should write its checkpoints by their full path, or use `--no-localscratch`. Files a copy back writes are written in a hidden `.nomad-tmp-JOBID/` in the folder first and renamed into place (a recovery removes one a stopped job left).

## What it reads

- **The script**, and the input file it names (at most 2 MB), as the person: their own run of nomad reads their own files.
- **Slurm's accounting**: the person's own jobs of the last 180 days (`--days`), with `sacct -u` and the person's own user name (from the system, never from `$USER`). The full folder and submit line let nomad find each past job's script and name its program. Slurm before 21.08 has no submit line: jobs are then compared by name only.
- **The partitions**: `scontrol --all show partition` and `sinfo -a -e` (hidden ones too; each kind of node on its own line), the person's groups and Slurm accounts (`sacctmgr show assoc`, `show user`).

`nomad size --db PATH --user NETID` sizes a script from nomad's database instead (on the hub, for staff), where folders and command lines are never kept whole, so jobs are compared by name only (and so only raise values); `--job` looks at one's own jobs only.

## Installing it for users

Users run one central install on each cluster, not one of their own, and `nomad size` and `nomad submit` need no nomad.toml. `scripts/install_nomad_for_users.sh` makes it, as the account that owns the folder (`/usr/local/sw/nomad`, or `NOMAD_USERS_DIR`), on the cluster or from a nomad checkout:

```
ssh OWNER@CLUSTER 'bash -s 1.18.1' < scripts/install_nomad_for_users.sh
ssh OWNER@CLUSTER 'NOMAD_USERS_DIR=/path bash -s 1.18.1' < scripts/install_nomad_for_users.sh
```

Running it again with another version upgrades: that is how a cluster follows nomad's releases, one step of each release beside the hub's upgrade.

- **Each release whole in its own folder.** `releases/VERSION` holds its own Python 3.12, installed by uv (from uv's release on GitHub, checked against a checksum fixed in the script), and its own environment, so the system's Python doesn't matter (a `python3` of 3.6 is fine). uv's cache and downloads are in a private temporary folder, and one run goes at a time.
- **One step to switch.** The release is built and checked first; only then is `bin/nomad` replaced, in one step, by one that runs it. Until then users go on with the one they had, and a release that fails is removed. The one before is kept (with its `bin/nomad`, as `bin/nomad.before`), and the script prints how to go back to it; older ones are removed. Two releases take about 0.9 GB. An install made by 1.18.0's script is replaced the same way, and its folders removed after the switch. A removal that fails then (a file still open on NFS, say) is reported and left for the next run.
- **The right folder.** The script refuses a folder another account owns (run it as that account), and one that holds anything it doesn't recognise as its own or 1.18.0's script's (a wrong `NOMAD_USERS_DIR`, say): such a folder is left untouched.
- **`bin/nomad`** starts nomad's Python isolated (`python -I`, writing no bytecode), so `PYTHONHOME` and `PYTHONPATH` a cluster sets for another Python (Anaconda's, say) don't reach it, while the jobs nomad submits get the environment as it is.
- **Nothing writable but by the owner,** nor owned by anyone else; the script stops if it finds otherwise, and says so when a folder above the install is writable by others (who could then replace it).
- **On users' PATH**: `bin/` holds only `nomad`, so it can't shadow anyone's Python. A link in a folder already on everyone's PATH (`ln -s /usr/local/sw/nomad/bin/nomad /usr/local/sw/bin/nomad`), or a module, or `/etc/profile.d`; last on the PATH, an account with its own nomad keeps it.
- **`check_current.sh`**, beside it, says when nomad-hpc has a newer release than the one installed (and prints the command that upgrades) and says nothing otherwise: for the owner's crontab, weekly, with `MAILTO` set. It never upgrades anything itself.
