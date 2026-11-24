# Jobs on Lab Machines

A lab's workstations often sit idle while its people wait in a cluster's
queue. From 1.67.0 the Console's Submit page can send a job script to one of
a lab's machines, as the person, on a machine with no scheduler: nomad starts
it there at once through the machine's `nomad-gate`, the same gate
[My Files](files.md) uses there.

There is no queue. A job starts now or is refused, and the page says why: the
machine is busy, the job asks for more than it may have there, or the
machine's gate doesn't start jobs.

## What a job gets

- **A share of the machine.** All of nomad's jobs on one machine together
  hold at most `share` percent of its processor cores and of its memory (50 by
  default). Whoever sits at the machine keeps the rest.
- **Cores of its own.** A job asking for 4 cores is held to 4 of the
  machine's cores, ones no other job of nomad's holds.
- **The lowest priority.** It runs at nice 19 with idle disk time, so the
  person at the desk always comes first: the job slows down, it isn't stopped.
- **Its time and its memory.** Past the time it asked for, or past the memory
  (read every few seconds), it is stopped, and the page says which. Should the
  machine run out of memory anyway, its processes are the first the kernel
  ends.
- **GPUs, whole, when it asks for them.** Each of the machine's GPUs (NVIDIA's,
  as `nvidia-smi` lists them) goes to one of nomad's jobs at a time, and all of
  them may be lent. A GPU something else is computing on now (a program of
  someone's, outside nomad) isn't given; a screen alone doesn't count. The job
  sees only the GPUs it was given (`CUDA_VISIBLE_DEVICES`, by their ids), and
  none when it asked for none. A GPU can't be slowed down for the person at the
  desk as the processor is: they share it for their screen.
- **The person's own environment.** It runs as them, in the script's folder,
  through their login shell (their modules and settings, as on a cluster).
  `#SBATCH` lines are comments here. `NOMAD_CPUS` and `OMP_NUM_THREADS` say
  how many cores it has, `NOMAD_GPUS` how many GPUs.
- **Its output beside the script**, as `nomad-run-ID.out` (never over a
  file), and a record in the person's `~/.local/share/nomad/runs/MACHINE/`,
  kept 30 days after it ends.

A job outlives the connection that started it. If the machine restarts, the
job ends and its record says so.

## Who may send one

```toml
[console.machine_runs]
open = "staff"        # "off" (the default), "staff", or "labs"
share = 50            # percent of a machine's cores and memory, 10 to 90
most_hours = 72       # the longest time a job may ask for
```

- `"staff"`: administrators and operators, on any lab machine.
- `"labs"`: staff, and each lab's members on their own lab's machines, the
  people My Files offers those machines to.

Only a campus sign-in sends a job, never a Console account nor a View as.
Every start and stop is in the audit log before it is asked.

## Turning it on for a machine

The machine has its say too: its gate starts jobs only when the hub's key
line there runs `nomad-gate serve --runs`. As the gate's account on the
machine:

```bash
/opt/nomad-gate/bin/nomad-gate install-key "$(cat gate_key.pub)" --from HUB_ADDRESS --edit --runs --apply
```

A machine where Slurm is (`sbatch`, or `/etc/slurm/slurm.conf`) refuses: jobs
go through Slurm there.

To check a machine from the hub, as a person, with a small test job of
nomad's own that is started, seen running and stopped:

```bash
nomad gate test --site labws01 --user rcadmin --run
nomad gate test --machines --user rcadmin --run
```

## Good to know

- nomad counts what its jobs hold on a machine from their supervisors' command
  lines, which anyone there may read. A machine that hides other people's
  processes (`/proc` mounted with `hidepid`) can't count others' jobs.
- On the machine, the job's processor and memory use is counted under the
  gate's account (the session the hub's connection opened), not the person's:
  the lab session figures don't include it. Each job's own record (cores,
  time, peak memory, processor time) is in the person's records there.
- Someone who logs in to the machine can start whatever they like there
  anyway: the share is what nomad's own jobs keep to.
