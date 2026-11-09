# Practice runs

A practice run shows a newcomer how a job runs on a cluster: how it is
sent, how it waits and starts, what it writes, and where its time, memory and
cores are read. It uses small inputs that are known to work, a kit for each
program.

**A practice run means nothing for research.** Its numbers are not results,
and its size says nothing about what a real calculation needs: that depends
on the real input (a water SCF takes seconds; a real MRCI can take hours and
many GB). Every kit, every copy (`README-practice.txt`) and every page that
offers one says so.

## The kits

nomad comes with these, each fitted to UR's job-script templates
(`[[submit.templates]]` in the hub's nomad.toml):

| Kit | Program | Licence | What it runs |
|---|---|---|---|
| gaussian-benzene | Gaussian | needs a licence | benzene: its best shape, then its vibrations (B3LYP/6-31G(d)) |
| qchem-benzene | Q-Chem | needs a licence | benzene: its best shape (B3LYP/6-31G*) |
| orca-benzene | ORCA | free for academic use | benzene: its best shape (B3LYP/def2-SVP) |
| molcas-water | Molcas | needs a licence | water: Hartree-Fock, then MP2 |
| cp2k-water | CP2K | open source | one water molecule: its best shape (PBE) |
| columbus-water | Columbus | open source | water: Hartree-Fock, then a single-reference CI, in a weak electric field (COLUMBUS's own example; the folder is the input) |
| namd-water, namd-gpu-water | NAMD, NAMD GPU | free for academic use | 1,000 water molecules for 100 ps |
| gromacs-water | GROMACS | open source | about 2,000 water molecules for 2 ns, on a GPU (built once at a site) |
| amber-peptide | AMBER | free for academic use | a small peptide in water for 100 ps, on a GPU (built once at a site) |
| r-bootstrap | R | open source | a line through made-up data, and 20,000 bootstrap fits, on one core or eight |
| python-montecarlo | Python | open source | pi from 2 billion random points |
| rfdiffusion-backbone | RFdiffusion | open source | one made-up backbone of 50 residues, on a GPU |

"Needs a licence" means the program runs only where the HPC has one: the
kit's input is nomad's own, but the program is not.

A kit supplies the input; the program's template writes the job script
around it, as it does for anyone's own input. So a practice run runs the
way real work will, with the template's modules and run line, at the kit's
own small size.

## Commands

| Command | |
|---|---|
| `nomad demos list [--program P] [--site S]` | the kits; with `--site`, whether each fits that site's template |
| `nomad demos show NAME` | what it does, its files, what to look at once it has run |
| `nomad demos copy NAME [--to DIR]` | a copy, as you, in a new folder (never over a file) |
| `nomad demos look NAME DIR` | once the copy in DIR has run: each thing to look at, and the lines that show it |
| `nomad demos look NAME --recorded` | what the kit's recorded run showed (below) |
| `nomad demos build NAME --to DIR [--site S \| --module-path P] [--module M]` | staff: make a kit's files once, at a site |
| `nomad demos check --site S` | staff: every kit against the site's templates |
| `nomad demos scripts --site S --hub --root R --out DIR` | staff: each kit's job script, as the site's template makes it for a copy in R/NAME |

After `copy`, the Console's Submit a job page makes the script: No script
yet? Make one, the kit's program, and the copied input.

## From the Console (1.35.0)

On the Submit a job page (Console fix 91), steps 2 and 3 stay greyed until
there is a script, so the whole way is on one page. A person can make a
folder of theirs on the cluster (**New folder**, in the folder open). Under
**No script yet? Make one**, once a program is chosen, **Start from an
example** lists the kits ready at that cluster for it, each with its licence,
its size and the practice-run notice, what it does, what to look at, and its
recorded run. **Copy it into** copies the kit, as the person and through
their gate, into a new folder named after it in the folder open (`r-bootstrap`,
then `r-bootstrap-2`, ...), and chooses its input. The script is then made
with the kit's own small size, in the partition practice runs go to, and
says in its first lines that it is a practice run. A choice of the person's
own is sized as the template sizes it, with the kit's memory and time: R's
"8 cores" gives 8 CPUs, AMBER's "one CPU core" no GPU (and not the GPU
nodes). The hub makes the script from what the cluster's gate said of the
kit, so a site's own kits work as nomad's do. Saved, it is sized and
sent like any script, and runs in its folder.

Where copying from the Console is off (editing isn't on for that cluster),
each kit shows the command to type there instead:
`nomad demos copy NAME --to DIR` (with `--from FOLDER` for a site's own
kit).

The hub names each cluster's built kits for the Console:

```toml
[submit.demos]
site_dirs = { spydur = "/usr/local/sw/nomad-demos" }
```

The gate's `kits`, `practice` and `mkdir` requests do the work on the
cluster (nomad 1.35.0 there). `practice` and `mkdir` write, so they are
audited and need the gate's editing on, like saving a script; neither
writes in a hidden folder (`~/.bashrc.d`, `~/.config`), and a copy never
follows a link. The site's kit folder is used only when it, the folders
above it and everything in it belong to root, nomad's installer, the person
or Slurm and no one else can change them (as for nomad-gate itself): a kit
is copied as each person who uses it, and its script runs as them. That is
the rule nomad-gate's own folder is held to, so beside it
(`/usr/local/sw/nomad-demos` beside `/usr/local/sw/nomad`) is a good place.
`nomad demos build` leaves what it makes writable by no one else, whatever
the builder's umask; kits built before 1.35.0 may need
`chmod -R go-w` once. Staff see on the Console's Submit page why a site's
kits aren't offered (the gate's `kits` answer says, in `problems`).

A practice run sizes nothing but itself: Submit's "jobs like this one" leave
out jobs that ran in a practice copy (a folder holding
`README-practice.txt`), unless it is the same folder or script.

## Known as a practice run (1.35.1)

A practice run's job is named `practice-KIT`, so `squeue`, `sacct` and the
Console's job lists say what it is. The hub knows one by its folder, named
after its kit (`r-bootstrap`, `r-bootstrap-2`, `r-bootstrap-20261109-131500`:
the Console's copies and `nomad demos copy`'s), and its job's name:
`practice-KIT`, or, before 1.35.1, the kit's own (`bootstrap`, `water`). A
copy made elsewhere with `--to` runs as a practice run all the same, but the
hub's lists won't mark it. Scores and advice leave practice runs out. On the Console (fix 92), My jobs marks them, and a practice
run's page says what to look at in its folder now, with the kit's recorded
run beside it (the gate reads the folder, as the person). Their scores are
shown, but not their advice: a practice run's size says nothing about real
work.

## The recorded runs

A kit can keep one real run of itself, to hold a new one against: its
numbers in `kit.toml`, and (where the program's licence allows) what it
wrote, in the kit's `recorded/` folder.

```toml
[recorded]
site = "spydur"
date = "2026-11-09"
module = "gromacs/2026.2-gpu"
partition = "gpunodes"
cpus = 4
gpus = 1
gpu = "NVIDIA A40"
runtime_s = 88
cpu_s = 342
cores_busy = 3.9
peak_mb = 149
files = ["recorded/water.log"]
```

`nomad demos show` gives it in a sentence, `nomad demos look NAME
--recorded` shows what its kept files show (the same things to look at as a
copy's), and `look` on a copy ends with it, to compare. Each copy's
`README-practice.txt` has it too. A built kit with none of its own takes
nomad's (same program, same files); `nomad demos build` carries it along.

These came from spydur on 9 November 2026 (sacct):

| Kit | Where | Size | Ran | CPU time | Memory |
|---|---|---|---|---|---|
| cp2k-water | condos | 4 CPUs | 5 s | 17 s (3.4 cores busy) | 657 MB |
| gromacs-water | gpunodes | 4 CPUs, 1 A40 | 88 s | 5 min 42 s (3.9 busy) | 149 MB |
| amber-peptide | gpunodes | 1 CPU, 1 A40 | 14 s | 13 s | 488 MB |
| r-bootstrap | condos | 1 CPU | 29 s | 29 s (1.0 busy) | 90 MB |
| rfdiffusion-backbone | gpunodes | 4 CPUs, 1 A40 | 58 s | 77 s (1.3 busy) | 5.0 GB |
| columbus-water | condos | 1 CPU | 1 s | 0.4 s | 121 MB |

AMBER's output is not kept (its licence is an academic one); its numbers
are. The kept files were made anonymous (the home folder reads `/home/you`).

To send every practice run once (after a module upgrade, say), staff make
their scripts on the hub, where the templates are, then send them on the
cluster:

```
nomad demos scripts --site spydur --hub --root /home/me/nomad-practice --out /tmp/kit-scripts
# then on spydur, for each NAME:
nomad demos copy NAME --to /home/me/nomad-practice/NAME
cp /tmp/kit-scripts/NAME.sh /home/me/nomad-practice/NAME/ && cd /home/me/nomad-practice/NAME && sbatch NAME.sh
nomad demos look NAME /home/me/nomad-practice/NAME      # once it has ended
```

## Kits built once at a site

GROMACS's `.tpr` and AMBER's topology are made by the programs themselves,
with the site's own versions. Staff build them once:

```
nomad demos build gromacs-water --to /usr/local/sw/nomad-demos --module-path /usr/local/sw/modulefiles
nomad demos build amber-peptide --to /usr/local/sw/nomad-demos --module-path /usr/local/sw/modulefiles
```

(`--site spydur` instead of `--module-path` where the hub's nomad.toml, with
its `[submit.build.spydur]`, is the one read.)

This copies the kit to `/usr/local/sw/nomad-demos/NAME`, runs its commands
there as you with its module loaded (`build.sh`, `build.log`), and checks that
they made what they should. Then offer the folder:

```toml
[submit.demos]
dirs = ["/usr/local/sw/nomad-demos"]
```

A site's kit of the same name replaces nomad's: a built one, or one a site
writes itself (a folder with a `kit.toml`; see `nomad/demos/kits.py`).

## Settings

```toml
[submit.demos]
dirs = ["/usr/local/sw/nomad-demos"]   # a site's own kits (folders of kit folders)
bundled = true                         # also the kits that come with nomad (default)
into = "~/nomad-demos"                 # where a copy goes: a new folder each time
partition = { spydur = "condos" }      # where practice runs with no GPU are sent (default: the template's)
gpu_partition = "gpunodes"             # and those with one (a name: every site)
site_dirs = { spydur = "/usr/local/sw/nomad-demos" }   # the hub's: each cluster's built kits, for the Console
```

A practice run that waits days behind long research jobs teaches the wrong
lesson. Send them where they start within minutes: a short partition, a
reservation, or a condo guest queue (they are short, so being put back in
the queue hardly matters).

## What the first runs found (spydur, 9 Nov 2026)

Practice runs double as a check of the cluster's own software. The first
runs found, beyond the kits:

- modules that point at software no longer there (`namd/2.11`,
  `orca/orca` 3.0.3, `molcas/8.0`: builds in `/usr/local/...` from an older
  cluster);
- a licence tied to some nodes only (Q-Chem on a condo node);
- a template whose program writes into its read-only container
  (RFdiffusion: give it `inference.schedule_directory_path="$PWD/schedules"`
  on the run line);
- a licence group doing its job (Gaussian: `chem`).
