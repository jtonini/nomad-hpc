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
| namd-water, namd-gpu-water | NAMD, NAMD GPU | free for academic use | 1,000 water molecules for 100 ps |
| gromacs-water | GROMACS | open source | about 2,000 water molecules for 100 ps, on a GPU (built once at a site) |
| amber-peptide | AMBER | free for academic use | a small peptide in water for 20 ps (built once at a site) |
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
| `nomad demos build NAME --to DIR [--site S \| --module-path P] [--module M]` | staff: make a kit's files once, at a site |
| `nomad demos check --site S` | staff: every kit against the site's templates |
| `nomad demos scripts --site S --hub --root R --out DIR` | staff: each kit's job script, as the site's template makes it for a copy in R/NAME |

After `copy`, the Console's Submit a job page makes the script: No script
yet? Make one, the kit's program, and the copied input.

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
