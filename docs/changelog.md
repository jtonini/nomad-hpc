# Changelog

## v1.35.0 (November 2026)

### New
- **Practice runs from the Console** (with Console fix 91): the gate's
  `kits` (the kits ready at the cluster, with `--kits-dir`), `practice`
  (copy one, as the person, into a new or empty folder of theirs) and
  `mkdir` (a new folder of theirs). `practice` and `mkdir` write: audited,
  and only with the gate's editing on. `[submit.demos] site_dirs` (the
  hub's): each cluster's built kits; `kits` names a site's own kit's folder
  (`from`), for `nomad demos copy --from` where copying from the Console is
  off.
- A kit's job script (`nomad.demos.kits.kit_values`, `job_values`, `mark`):
  the kit's own size, the partition practice runs go to, its choices, and
  two lines under `#!` saying it is a practice run. A choice of the person's
  own is sized as the template sizes it (R's 8 cores; AMBER's one CPU core:
  no GPU, not the GPU nodes); choosing an option's first choice changes
  nothing. A kit may be a gate's description of one (the hub may not have a
  site's own kit). `nomad demos scripts` writes them so too.
- The gate's `practice` and `mkdir` write in no hidden folder; a copy is made
  through folder descriptors and never follows a link; a site's kit folder
  is used only when no one but root, nomad's installer, the person or Slurm
  could have changed it (`kits` says why not, in `problems`); `nomad demos
  build` leaves what it makes writable by no one else.
- **Recorded runs**: a kit's `[recorded]` table keeps one real run of it
  (site, date, module, partition, CPUs, GPUs, run time, CPU time, cores
  busy, peak memory) and, in its `recorded/` folder, what it wrote.
  `nomad demos show`, `look` (and `look NAME --recorded`), each copy's
  README and the Console show it. cp2k-water, gromacs-water, amber-peptide
  (numbers only), r-bootstrap and rfdiffusion-backbone carry their spydur
  runs of 9 November. A built kit with none takes nomad's; `nomad demos
  build` carries it along.

### Changed
- Submit's "jobs like this one" leave out practice runs (a folder holding
  `README-practice.txt`), unless it is the same folder or script: a
  practice run's size says nothing about real work.
- The kits' "about N minutes" from their recorded runs (CP2K, R, AMBER: 1;
  GROMACS, RFdiffusion: 2). AMBER's look says the last two lines are the
  run's average and how much it varied.

## v1.34.1 (November 2026)

From the practice runs' first runs on spydur (9 Nov):

- `[submit.demos] partition` and `gpu_partition` (a name, or `{ site =
  partition }`): where practice runs are sent, so they don't wait behind
  long research jobs (spydur: the condo guest queue).
- amber-peptide: a box pmemd.cuda accepts (16 A of water around the
  peptide; the GPU code refused a smaller one), and 100 ps.
- gromacs-water: 2 ns, about a minute and a half on an A40 (100 ps took 7 s).
- rfdiffusion-backbone: RFdiffusion keeps its noise schedules in the job's
  folder (`inference.schedule_directory_path`); its own folder in the
  container is read-only, so a run without it fails. The template's run line
  needs the same for everyone's jobs (docs/demos.md).

## v1.34.0 (November 2026)

### New
- **Practice runs** (`nomad demos`, docs/demos.md): kits of small inputs,
  known to work, that show a newcomer how a job runs: how it is sent, waits
  and runs, what it writes, and where its time, memory and cores are read.
  Their results mean nothing for research, and their size says nothing about
  what a real calculation needs: every kit and every copy says so.
- Kits that come with nomad, fitted to UR's job-script templates: Gaussian,
  Q-Chem and ORCA (benzene), Molcas and CP2K (water), NAMD on CPUs and on a
  GPU (a box of water, made by scripts/demos/make_namd_water.py and checked
  with OpenMM), R (a bootstrap), Python (pi from random points), RFdiffusion
  (one backbone); GROMACS (a box of water) and AMBER (a peptide in water),
  whose inputs a site makes once with `nomad demos build`.
- Each kit is labelled: open source, free for academic use, or needs a
  licence (it runs only where the HPC has one). A kit's input is the
  template's, so a practice run runs the way real work will.
- `nomad demos list | show | copy | look | build | check | scripts`; `[submit.demos]` dirs,
  bundled, into. A copy is made as the person, in a new folder, and never
  writes over a file.

## v1.33.1 (November 2026)

### Fixes
- **Lab membership check:** a condo two labs share made each lab's PI one
  of the other's too (yangnolin: knolin answered for myang$, and myang for
  knolin$). A condo's owners now answer only for a group with no PI of its
  own (`members = "group:chemlab$"`).

### New
- `[console.labs.check] not_sites`: sites whose groups the check leaves out,
  members, condos, jobs and to-do alike. On a teaching server an
  instructor's group holds their course's students, which a PI is not the
  one to answer for (UR: `not_sites = ["spiderweb"]`).
- Each person's last job on any cluster, beside the question, most recent
  first, on My lab and in `nomad lab check show`. Members with no job in a
  year who were in the group before that can be marked as having left at
  once, with what to do with their data (`nomad.lab_check.record_many`; the
  Console's fix 90); each answer can still be changed after.

## v1.33.0 (November 2026)

### New
- **Lab membership check** (`nomad lab check`, docs/lab-check.md). Once a
  term, on dates `[console.labs.check] opens` sets, each lab's PI is asked on
  My lab who is still in the lab: each member of its group (still here, has
  left with what to do with their data, or not in the lab), and each person
  who used its condo without being in its group (in the lab, or a guest).
  The answers become ARC's to-do a cluster at a time: take someone out of a
  group, put someone in one, a leaver's data with where it is and how big,
  labs that didn't answer. A group change is done once nomad's groups
  reading shows it; the rest is marked done with a note. Emails when the
  check opens and reminders, to review_to while it is set
  (`nomad lab check mail --send`, `nomad lab check timer`). The answers are
  kept in lab_check.db beside combined.db, with a log of every change.
  nomad changes no group, account or file itself. A condo's owners answer
  for its group; a Console administrator may answer for a PI (kept as
  such). Off until `opens` is set. The Console's pages come with its fix 89.

## v1.32.2 (November 2026)

### Fixes
- A job's memory peak for a job of many tasks at once. Slurm's MaxRSS is the
  peak of a job's largest task, not of the job: a 40-task MPI job whose
  tasks each held 2 GB of the 100 GB it asked read 2 GB, while it held 80.
  `job_live` now keeps, since each run of a job started, the most all its
  tasks held together at any of nomad's readings (`held_most_mb`) and the
  most its running steps' tasks' own peaks came to, added up
  (`peaks_sum_mb`: while no step has gone unseen, never less than what they
  held at once, wherever Slurm placed them), and whether a step ended or
  came and went between readings (`steps_unseen`). Both figures outlast the
  steps that set them, and count the extern step's memory (processes started
  by ssh into the job's node). Tables made by 1.32.0 and 1.32.1 gain the
  columns.
- An ended job's `max_tasks` is now the most tasks that ran at once: steps
  run side by side (`srun -n1 ... &`) added up, the batch step's shell left
  out while they run unless it held more than 1 GB. Before, it was the most one step ran, so twenty
  one-task steps at once read as one task, and their memory peak (one
  task's) was scored as the job's. Such a job's memory isn't scored now.

## v1.32.1 (November 2026)

### Fixes
- `job_live`: each step of a job counted once. Asked for an array's own id,
  sstat also answers for its other running tasks, so a task asked for by
  its own id as well came back twice: on vadur, 12266_48 and 12266_81 read
  10.7 and 10.5 cores busy of the 6 asked, twice their 5.3.
- `job_live`: when nothing runs, its table is emptied (a collector that
  collects nothing isn't stored, so rows of jobs that had ended stayed), and
  it is there from the first run.
- `job_live`: the cores kept busy since the last run are no longer held to
  the cores asked for: a cluster that doesn't keep jobs to them shows.
- nomad.toml.example: `[actions]` (agent_key, allow_unrestricted, slurm_as,
  with the sudo lines a site needs) and `[collectors.job_live]` (enabled,
  interval, sstat_as); preemptions, clock, modules and job_live among the
  collectors off unless turned on.

## v1.32.0 (November 2026)

### Features
- **What running jobs use now** (the `job_live` collector, off unless
  enabled): per running job, the cores it keeps busy (now, since the last
  run, and on average) and the memory it holds (now, and its peak so far),
  from `sstat`, run as Slurm's account through sudo (`sstat_as`, by default
  `[actions] slurm_as`). One row per running job (`job_live`), gone when the
  job ends; synced to the hub like any table. For the Console's running jobs.
  On spydur, bchen's 87790 asked for 350 GB and held 31 GB (`sstat`), while
  Slurm's free memory for its node (the file cache counted as used) said
  726 GB in use.

## v1.31.2 (November 2026)

### Fixes
- `nomad node drain|resume`: where sudo insists on a terminal (`Defaults
  requiretty`, as on RHEL 7), the dry run says so, with the line that lets
  the account's agent through (`Defaults:ACCOUNT !requiretty`); it said "sudo
  would not let ACCOUNT run this ... without a password". Slurm's programs
  and sudo now run without a terminal by hand too (a session of their own),
  so a dry run in a terminal meets what the Console's agent meets.

## v1.31.1 (November 2026)

### Fixes
- `nomad node drain|resume`: the check of the nomad running here no longer
  refuses what lies below a folder only its owner can pass through: arachne's
  `~/.local` (700) over `~/.local/lib` (775, its group zeus's primary group
  with others in it), as pip leaves it under umask 002. Such a folder must
  itself be safe; files below it must still be root's or the account's.
- When sudo wants a password for the change (`sudo -n -l` can't tell
  beforehand), the answer says so, with the NOPASSWD line that would do.

## v1.31.0 (November 2026)

### Features
- **Draining a node and resuming it** (`nomad node drain NODE --reason TEXT`,
  `nomad node resume NODE`; the actions `node.drain` and `node.resume`, for
  the Console's Nodes page): a dry run unless `--apply`, showing the node's
  state as Slurm has it, the jobs running there, the scontrol command that
  would run and whether sudo would let it. Applied, the node is read again:
  Slurm's state decides whether it was done. scontrol runs as Slurm's own
  account through sudo (`[actions] slurm_as = "slurm"` and one sudo line
  that allows these two commands only) or as an account that is a Slurm
  administrator (`"self"`); off until set. Nothing runs when someone else
  could change the nomad that would run it. Slurm keeps the reason with who
  asked ("TEXT (NETID via nomad)"); each change, and each refusal, goes to
  syslog and `~/.local/share/nomad/nodes.log`.
- **Actions that change something:** the catalog's first two. Each is a dry
  run unless its `apply` flag is true (`read_only: false` and `apply` in the
  catalog). New parameter kinds: `text` (a note, passed as one word),
  `caller` (who asked, filled in by the Console or `nomad actions run`) and
  `flag`. The catalog also says `hub_db` and `notes`.

### Fixes
- `nomad facts share`: when every day of other people's use is left out, the
  lab's part of everyone's use is not given (it said "at most 100%"); what
  others held while people waited is said "on the days that can be shown"
  when some are left out.

## v1.30.1 (November 2026)

### Features
- **A lab's quiet condo, said** (`condo_notes`; `nomad facts share`, the
  Console's Cluster use): where a lab's own condo was under 25% busy while its
  jobs that could have run there (no more cores than it has, no GPUs where it
  has none) waited a median of an hour or more elsewhere: the condo, how busy,
  where they waited, and the partition that would have run them.

### Fixes
- `nomad facts share`: "off its condo" only where the lab has one; a cluster
  the lab didn't use is one line ("didn't run on: ...").

## v1.30.0 (November 2026)

### Features
- **A lab's share of each cluster** (`nomad facts share NETID`, the Console's
  My Lab > Clusters): over the last days, the lab's core-hours and GPU-hours
  on its own condo, the shared nodes and other labs' condos, against
  everyone else's and what the nodes could have run (their hours in
  service), and what was left idle; how full it kept its own condo; on a
  condo two labs share, the partner lab's part. Who waited for room while
  the lab's jobs ran on the nodes they could use (off its own condo), and the
  share of the busy cores the lab held then; the lab's own waits, and what
  others held then. A trend by day, week or four weeks, against its own
  condo's capacity when it has one. With `--min-others`, every figure about
  people outside the lab is a sum of days, a day resting on fewer of them is
  left out (and counted), and the window ends at the last midnight.
- **My Lab counts a member's jobs from the day they joined** (`nomad facts
  lab`, the Console's Members): the groups collector's first sight of them in
  the group (first_seen, since 1.11); a new student's earlier jobs are not the
  lab's, here as in its share of the clusters.
- **Node hours over any days** (`nomad.condo.figures.node_hours`): what the
  condo reports count by month, by spans of whole days (node_months is it
  by month, the same figures as before).

## v1.29.0 (November 2026)

### Features
- **A template's options.** A job-script template may offer a few choices
  (`[[submit.templates.options]]`: at most four, two to six choices each, the
  first chosen unless the person picks another): how it runs in parallel
  (threads, MPI, or MPI processes of several threads: `parallel = "hybrid"`
  with `threads_per_task`), which program of a suite, what it does when it
  is put back in the queue. A choice changes only what it names (the run
  line, setup and after lines, outputs, sizes, #SBATCH options). `{tasks}` and
  `{threads}` in a template's lines; `omp = true` exports `OMP_NUM_THREADS`.
  `nomad templates --site S` lists them; `--make ... --choose NAME=N`.
- **Made scripts keep what they printed when requeued** (`--open-mode=append`),
  name the choices made in their heading, read their counts as the job has
  them (`{tasks}` is `${SLURM_NTASKS:-N}`: a job resized when sent runs as many
  processes as it has), and export `SRUN_CPUS_PER_TASK` where threads and
  `srun` meet (a step would otherwise get one CPU). `{mem_mb_each}` is each
  process's share: all of it for one process of threads.
- **`nomad templates --site S --hub --check`**: every script each template can
  make, made; a template that can't be read fails it. One program may have
  one template a site (CP2K: `mpirun` at one cluster, `srun` at another).

## v1.28.1 (November 2026)

### Features
- **A condo two labs share** (or whose lab is in two groups): report.toml's
  `members` names several groups, `members = "group:myang$,knolin$"`, anyone
  in any of them its lab's. `nomad partitions --condos` proposes
  `AllowGroups=myang$,knolin$` (Slurm admits anyone in either), the condo
  reports count both groups' members as the lab's, and the condo is on each
  group's My lab page. A group named twice, or an empty name, is refused.
  Before, a second group could only be added in the directory, which also
  gives its people the first lab's files.

## v1.28.0 (November 2026)

### Features
- **Guests on condos, and the owners' first claim.** A condo's owners get
  their nodes back from a guest's job through Slurm's own preemption (the
  owners' partition at a higher PriorityTier admitting their lab; a guests'
  partition, `condo_overflow`, with PreemptMode=REQUEUE and a GraceTime:
  SIGTERM, the grace, then back in the queue). Slurm's configuration is the
  switch; nomad follows it. `nomad partitions --site S --condos` checks each
  condo of report.toml against Slurm (the owners' tier and group, every
  other partition on their nodes) and prints the slurm.conf lines that turn
  it on, and the same lines as they are now. Slurm's preemption settings are
  read with the partitions (`slurm_settings`).
- **Whether a job survives a requeue**: `nomad size --requeue job.sh`
  (`--no-requeue`, a log written anew, a program that starts over instead of
  resuming from its checkpoint, steps that fail the second time, files on the
  node's own disk), each with what to change.
- **Where it starts soonest** tells a guest what happens when the owners need
  their nodes, where Slurm preempts.
- **A message when it happens** (`[submit.preemption] notices = true`): the
  `preemptions` collector keeps each preempted job instance and whether it
  came back; the hub emails the person, one message a person a run, each job
  once, only where Slurm preempts that partition.

## v1.27.0 (November 2026)

### Features
- **How fast each CPU really runs when busy** (the `clock` collector,
  `[alerts.clock]`, `nomad clock`). arachne's node01-03 lost a power
  supply's input on 4 October 2026 and their boards braked the CPUs to about
  400 MHz instead of 3,700: no failure, healthy in Slurm, every job ten times
  slower for a month. Each host runs a short busy loop and reads its CPU's
  cycle counter (no root: perf for that process only; its sha256 rate where
  there is no counter); a reading below half of what it should give (its
  usual clock, hosts with the same CPU, the CPU's top clock), twice in a row,
  is alerted: critical when its power readings show a supply problem too.
  On arachne, 5 Nov: node02 396 MHz, node51 3,730.
- **Where a job starts soonest** leaves out partitions whose nodes are all
  throttled, says which nodes run slow in the others, and never picks one
  with slow nodes while another has none.
- **A board that can't be read in-band** (no /dev/ipmi0) is NOT WATCHED in
  `nomad power`, not shown as if healthy; the clock alert says its supplies
  can't be seen. Sites whose alerts the hub sends get the clock alerts from
  the hub.

## v1.26.0 (November 2026)

### Features
- **Job scripts for people with none** (`[[submit.templates]]`, the
  `modules` collector, `nomad templates`; the Console's "No script yet? Make
  one", Console fix 81). A template for each program, written once by a site's
  staff in the hub's nomad.toml, says how the site runs it (its modules,
  setup, run line, outputs; Gaussian, Q-Chem, ORCA, Columbus, GROMACS,
  VASP...); the person picks the program, the version and their input (a
  file, or a folder of inputs), and nomad writes the script: text only,
  shown in full, saved as a new file beside the input as them. The versions
  are the modules the cluster really has: the new `modules` collector reads
  `module -t avail` once a day (after `module use` for folders a login
  doesn't add: spydur's `/usr/local/sw/modulefiles`), copies kept aside
  (`.bak`) left out; chosen first, the template's default, else the
  cluster's, else the highest. A version that runs otherwise (g09, g16)
  gets its own run line. A script with a scratch folder stops at once
  outside Slurm (its `rm -rf` would take the whole scratch folder), and a
  name with a control character, or one that looks like an option, never
  reaches a command. `nomad templates --site S` lists what a site offers
  and what is wrong in a template; `--make P --input PATH` writes one.

### Fixes
- **A fan header with no fan isn't a stopped fan.** vadur's CPU nodes have
  four fan headers at 0 RPM on every board, and 1.25.8 alerted them (mingus
  had them in `[alerts.power] ignore`). A fan nomad has never seen turning
  is now said in the host's notes, not alerted; one that turned and stops
  still is, also after a read with no reading and across the upgrade
  (1.25.8's rows are judged by their own history). `nomad power`'s label
  for a host is its worst problem after `ignore` and `fans`, not as stored.

## v1.25.8 (November 2026)

### Features
- **Power supplies, voltages and fans** (the `power` collector,
  `[alerts.power]`, `nomad power`): what each server's board controller
  knows, through `ipmitool`: each supply's state and power, the voltages, the
  fans, the board's event log. vadur's gpu09 ran on one supply from February
  to November with nothing saying so. Root reads the boards (a small
  read-only helper, `scripts/power_root_probe.py`, by root's cron: on a head
  node, itself and every Slurm node over root's ssh); nomad reads its file.
  Alerts for a supply with no input power, failed or failing, redundancy
  lost, one supply carrying the node, a voltage past its thresholds, a fan
  stopped, and new problem entries in a board's log; a threshold of 0 isn't
  one (vadur's CPU nodes log "0 W below 0 W" at every start). Sites whose
  alerts the hub sends (`alerts = "hub"`) get them from the hub.

## v1.25.7 (November 2026)

### Fixes
- **A CPU's cores are one temperature: their hottest.** Intel's coretemp
  gives each core its own reading (26 a socket on spydur's Xeons: 1,674
  rows a round, and their hours kept for good). Now one row a chip, `cores`
  ("Cores (hottest)"); migration 29 takes out the per-core rows already
  kept. The package's own reading is still the alert's.
- **An alert with nowhere to go isn't counted as sent.** Raised with no
  alert backend (mingus's `[alerts.email]` was off), it counted as raised,
  so once email was set up it waited for its daily reminder. Now it is
  tried again as a failed send is: 15 minutes later, then 30, an hour...
- **`nomad temps --check --send` always sends** (a test, never counted as
  raised), and says why when it can't: no alert backends, or below
  `[alerts] min_severity`.
- **The GPU collector over ssh** skips what a login prints first (a
  workstation's banner) and logs in as ssh's own settings say unless
  `ssh_user` is set (it forced `$USER`).

## v1.25.6 (November 2026)

### Features
- **Hardware temperatures** (the `temps` collector, `[alerts.temps]`, `nomad
  temps`): what the kernel's sensors read (`/sys/class/hwmon`: CPU, drives,
  integrated GPU), on this host or over ssh: the hosts listed, the
  workstations the workstation collector reads (`workstations = true`), a
  head node's Slurm nodes that answer (`slurm_nodes = true`); each reading
  kept 30 days and each hour's lowest, mean and highest for good.
  The CPU package's own sensor (k10temp Tctl, coretemp Package id) alerts
  when every reading for `sustain_minutes` (3) is at or above `warning` (90
  °C) or `critical` (95), a host's own limits in its own table; sent through
  the collecting host's alerts. `nomad temps` shows each host's sensors now
  and the CPU's last hours, day and night; `--check --warning N --send`
  tests the alert. For peterslabworkstation, whose CPU cooler's fan died.

### Fixes
- **`[alerts.node_state]` and `[alerts.node_jobs]` now apply.** The
  node_state collector read them from its own `[collectors.node_state]`
  table, where nobody puts them, so the defaults always applied.
- **"Your lab's condo" only for its owners and the members report.toml
  names.** Without `members`, nomad took the owners' lab groups, as the
  condo reports do; spydur's myang$ holds some 170 people (staff, other
  PIs, a demo account), and all of them saw yang1, yang2 and yangnolin as
  their own, with no guest limit, for "Fastest start" to take. Now a condo
  without `members` is its owners' own, a guest's for everyone else.
  `nomad partitions --site S --sorted` lists the condos without `members`.

## v1.25.5 (November 2026)

### Fixes
- **Where a job starts soonest showed no estimate on spydur**: its sbatch
  (Slurm 25.11) says a word between the start time and "using" ("to start
  at 2026-11-03T09:49:39 a using 1 processors ..."), which nomad didn't
  expect, so every partition's row came back as an error with Slurm's line
  in it. nomad now reads whatever Slurm puts there.

## v1.25.4 (November 2026)

### Features
- **Where a job starts soonest** (the Console's Submit page; `nomad.submit.where`,
  `nomad-gate starts`, `nomad submit --starts`): the partitions a person may
  send a job to, in the site's order, each with Slurm's own estimate of when
  it would start there (`sbatch --test-only` on the very text shown, as nomad
  would send it; nothing submitted), the usual wait from nomad's records, and
  why any can't be suggested. The order: the person's lab's condo (its
  owners and members, from report.toml), the shared nodes, other labs'
  condos as a guest (a job within the condo policy's guest limit), and the
  few big-memory nodes last (a job that doesn't need them only with a time
  limit of at most `overflow_max_hours`, and only while more than
  `reserve_nodes` of them are idle). Partitions with the same nodes count as
  one; one that spans nodes of several kinds isn't suggested; the default
  partition always is. Whose condo it is: as the condo reports read
  `members`. What nomad doesn't know it doesn't offer: a node without a
  memory reading, a condo not yet confirmed, a guest partition's nodes
  report.toml lists no condo for (to guests only), a site with no condos
  table in report.toml or a condo whose nodes can't be found (then only the
  default partition, and why). A start
  Slurm puts 7 days or more away (where jobs have no time limits) is "later",
  not ranked by. "Fastest start" takes nomad's pick, never another lab's
  condo unless `fastest_guests`, and never over a partition the person
  chose meanwhile. Run it again asks too (`starts --job N`). Up to 12
  partitions, four at a time, each at most 60 s; an estimate for another
  partition than the one asked isn't taken; the gate answers two at once
  at most (`STARTS_HERE`), so sending is never held up. `[submit.where]` in the
  hub's nomad.toml; `nomad partitions --site S --sorted` shows the sort.
- **Text files edited from the Console**, besides job scripts: a person's
  own `.py .R .m .jl .sh .in .inp .com .gjf .yaml .yml .json .toml .cfg .conf
  .txt .md` (`TEXT_EXTENSIONS`), UTF-8, at most 256 KB, never hidden, no NUL.
  `nomad-gate read --path PATH` gives one (only with `serve --edit`, as
  `save`); `save` takes them too, with every safeguard it has for scripts
  (the text opened, the earlier text kept, the group and access lists, the
  folder as it really is). A file with another name (a hard link) is
  neither read nor saved. `ls` marks them (`text`).
- **A node that fails every job at start** (`[alerts.node_jobs]`, raised by
  the node_state collector): one where, among the jobs that started there in
  the last hour, at least 5 failed within a minute and they are at least 80%
  of them, while the very same work (the array's other tasks, the person's
  jobs of the same name) ran on other nodes and hardly failed there. A node
  that looks idle and healthy to Slurm can take a whole array this way in
  minutes. The alert names a job to look at before draining (an array's last
  tasks without input fail the same way) and how many different works
  failed there. A node already drained or down is left out; the alert names
  no one. A new index on jobs(start_time) (migration 28) keeps the
  minute's look cheap.

### Changes
- **`nomad config check` says a switch written as text** (`edit = "true"`
  is the word true, which nomad never takes for a yes), with the line, the
  table it is in and the fix.
- **`nomad-gate versions` and `recall`** (the earlier texts saving kept) are
  served only with `serve --edit`, as `save` and `read`.

## v1.25.3 (November 2026)

### Changes
- **A job's scores hold against the person only what they could have asked
  less of.** One core is the least a job can ask, and a memory request
  already as small as nomad would suggest can't be smaller: neither is
  "Needs Work" any more ("not judged", with why), and no recommendation is
  the value already asked (a one-minute test job asking 1 CPU and 1G was
  told to ask for 1 CPU and 1G). A job that ran under five minutes isn't
  judged on its CPU use or its time limit (one that ran out of time still
  is). A job with nothing to judge has no overall score (`overall_score`
  null, "Not scored"), isn't stored as a score, and isn't told "all good".
- **Sync alerts count hours by the clock.** How long a site hasn't synced,
  or the combined database hasn't been replaced, was an hour off across a
  change of daylight saving time (45 hours said as 44).

### Features
- **nomad-gate save**: a job script written as the person, for the
  Console's editor. Off unless both sides say so: the hub (`edit = true` in
  the site's `[[hub.sites]]`) and the cluster (`nomad gate install-key
  --edit`, so sshd runs `nomad-gate serve --edit` for the hub's key: with
  it, that key could run code as anyone the gate acts for, so it is the
  cluster's own choice). Only their own job script (a regular file they
  own, not a link, beginning with `#!`, as the new text must; no hidden
  part in its path as given nor in its folder as it really is), only over
  the very text they opened (`--sha256`, checked again just before it is
  replaced) or as a new file (`--new`, never over one that is there; it is
  the person's alone, mode 600, since the gate can't know the umask their
  login sets). A script that isn't UTF-8 isn't saved over (the editor would
  change it where it wasn't edited; `show` says `utf8`). The text comes on
  standard input (`--body-sha256` its sha256, at most 256 KB of UTF-8); it
  is written beside the file and put in its place in one step, with the
  file's permissions, group and access lists (POSIX and NFSv4; none taken
  from the folder's default list); the text before is kept first, private,
  in `~/.local/share/nomad/versions` (the last 20 of each script, ids in
  UTC; never through a link nor a folder someone else owns):
  `versions` lists them, `recall` gives one back. Each save is in the
  gate's syslog with the sha256 before and after. `nomad gate test` says
  whether saving is on, on each side.
- **Pressure stall information** (`pressure` collector, on by default):
  how much of the time work on a host waited for CPU, memory or I/O
  (/proc/pressure: "some" and "full", over 10 s, 1 and 5 minutes). Where
  the kernel has it off (RHEL and Rocky leave it off: boot with `psi=1`),
  it says so (`pressure_status`) and records nothing, until the host is
  booted with it.

## v1.25.2 (November 2026)

### Changes
- **sbatch reads what nomad sends on its standard input.** The text the
  Console showed (or nomad's local-scratch script, which carries a script's
  text) goes to sbatch itself, never as a file of nomad's that someone could
  change before sbatch reads it (`/dev/stdin` before the job's arguments when
  it has some). So the Console sends from accounts set up as they are on
  spydur, where `~/.local` is writable by everyone's group: 1.25.1 refused
  them. nomad still keeps its record of what it sent (private, in
  `~/.local/share/nomad/staged/`) and notes which record each job came from
  by its cluster and number (`staged/jobs/CLUSTER.JOBID`, with the job's
  folder and when it was sent, checked against Slurm's record of the job),
  for `--again` and past runs: Slurm keeps no file name for a script it read
  on its standard input. A record it can't keep is said, not a reason not to
  send.
- **Who else could run programs as the person** is said when the Console
  shows a script (`show`'s `warning`, after the script's own): the first of
  their home folder, their login files and `~/.local` that others could
  change, with the `chmod` that closes it. Not refused: it is open with or
  without the Console, and nothing is said once it is closed.
- From the Console, Slurm's programs (`sbatch`, `scontrol`, `sinfo`, `sacct`,
  `sacctmgr`, `squeue`, `scancel`) are found once on the person's login PATH,
  before any runs, and run by their full paths; nothing runs when one could
  have been put there by someone else (it, a folder above it or a link on the
  way to it is another account's -- not root's, the person's, nomad's
  installer's or Slurm's -- or others may write it). `nomad size` takes the gate's `--via console` too, and the gate's
  own steps go through `nomad-gate exec`. By hand, as before.

## v1.25.1 (November 2026)

### Changes
- **What the Console shows is what it sends.** A job sent from the Console
  carries the sha256 of the script text the person was shown (`--sha256`);
  the gate refuses a script that has changed since ("look at it again"),
  `nomad submit` checks it once more, and sbatch is given nomad's own copy of
  that text, never the path: the local-scratch script, else a private copy in
  `~/.local/share/nomad/staged/` that knows the person's own script (so the
  job is sent again from theirs). A folder others can change no longer stops
  a job being sent from the Console (on spydur every `/scratch/USER` is
  writable by everyone's group): the gate says so instead (`show`'s
  `warning`), with the `chmod` that closes it. The script is read once and
  that text is what is sized, staged and sent; nomad's own folder for its
  copies is made private, and a send is refused while a folder above it
  (`~/.local`, the home folder) can be changed by others. `keep()` always
  writes its copy anew.

### Features
- `nomad-gate job --job N`: one of the person's jobs -- Slurm's view of it
  now (`scontrol`), its accounting (`sacct`: steps, memory, CPU time) and the
  end of its output, read as them; someone else's job is refused. For the
  Console's job details.

## v1.25.0 (November 2026)

### Features
- **nomad-gate**: the one way the hub acts on a cluster as a person, for the
  Console's Submit page and Run it again. The hub's own gate key (`nomad gate
  key`) can run nothing but `nomad-gate serve` on the cluster (`nomad gate
  install-key`: `restrict`, `from=` the hub, `command=`); the gate takes a
  short list (`version`, `size`, `submit`, `again`, `jobs`, `cancel`, `ls`,
  `show`), checks every value on the hub, as the cluster's account and again
  as the person, and acts as the person with that account's sudo (`sudo -n -u
  PERSON`), never as root or a system account. nomad runs in the person's
  login environment, from the script's folder; the script must be their own
  file, in folders no one else can change (checked again just before sbatch).
  Each request is logged to syslog; each answer is one line of JSON; at most
  four are served at once. It refuses to serve when anything in its release
  could be changed by anyone but its owner or root (`nomad-gate check`).
- `scripts/install_nomad_for_users.sh` installs `bin/nomad-gate` beside
  `bin/nomad` (same release, switched with it, kept as `.before`).
- `nomad gate test --site S --user U` (is it there, may it act as U) and
  `nomad gate ask --site S --user U OP ...` (one request, its JSON answer), on
  the hub; `submit = "gate"` in a site's `[[hub.sites]]` turns it on for the
  Console.
- `nomad submit --via console` (hidden): the tag says where the job was sent
  from (`via=console`).

## v1.24.1 (November 2026)

### Fixes
- A job sent through local scratch runs nomad's own steps on the node (the
  copy in and back, the checks, Slurm's commands, the node's `python3` and
  `rsync`) without the job's `LD_LIBRARY_PATH` and `LD_PRELOAD`: a cluster's
  login environment can point those at libraries that break system tools (on
  spydur, `ssh` fails with `undefined symbol: EVP_KDF_ctrl`). The person's
  script gets them back exactly as they were, and so does a job it sends
  through nomad's `sbatch` stand-in.

## v1.24.0 (November 2026)

### Features
- `nomad submit` runs a job on one node **through its node's local scratch**,
  by default. nomad sends its own small script in place of the person's; on
  the node the job copies its folder to `/localscratch/USER/job_JOBID`
  (private, locked while the job lives; after checking the node keeps room),
  runs the person's script there, then copies **only what is new or changed**
  back to the folder it ran from (as if it had run there), checks every file it
  copied (sha256 on both sides, read back from the server), and only then
  removes the local copy. What doesn't check out stays on the node, the `.out`
  says so, and a job that had succeeded ends with exit 75. What changed in the
  folder while the job ran is never undone: the job's version of such a file
  goes in `nomad-conflicts-JOBID/` (exit 75), and a file both only added to
  (a log, a table of rows) gets the job's new lines after the others'.
  - Before the time limit (`--signal=B:USR1`, 10 minutes, less for limits under
    an hour) the job stops the program, copies back, then waits for the limit
    so Slurm still records `TIMEOUT`; `scancel -b -s USR1 JOB` stops it the same
    way any time. Cancelled or preempted jobs keep part of `KillWait` for the
    program, the rest for the copy back.
  - What a stopped job leaves on a node is brought back by a small job it sends
    pinned to that node (`nomad-recover-JOBID`, after it ends) or by the
    person's next staged job there, only once its lock is free, and always into
    a folder of its own (`nomad-recovered-NODE-TIME/` in the job's folder):
    nothing recovered overwrites anything.
  - Sent as it is, saying why: jobs on several nodes, arrays (unless
    `--localscratch`), limits under 30 minutes, scripts that `cd` out of their
    folder or send jobs themselves (sbatch, workflow tools), folders over 50 GB
    (`NOMAD_STAGE_MAX`), the home folder itself, no `#!` or DOS line breaks,
    `--wrap`/`--test-only`/heterogeneous jobs; on the node, no `/localscratch`,
    no `python3` or no room: it runs where it is.
  - `--no-localscratch` / `--localscratch`; `NOMAD_LOCALSCRATCH=off` (a site or
    a person), or another folder with `{user}`. The node needs only its own
    `python3` (and uses `rsync` where there is one): the script carries
    `nomad/submit/stage_helper.py` (stdlib, Python 3.6+).
  - nomad's copy of the script is kept in `~/.local/share/nomad/staged/` and
    names the person's own, so `nomad submit --again` and past-run comparison
    still use the person's script; the tag gains `stage=l`. See
    docs/submit.md, "Through local scratch". No database change.

## v1.23.0 (October 2026)

### Features
- The `auth_failures` and `setuid_new` signals (failed `sudo`/`su` per account,
  and a person's setuid/setgid files) can now be read **unprivileged**, by the
  probe itself, with no root helper and no cron — retiring the only part of the
  watch that needed root:
  - **`watch_auth` (on by default):** failed `sudo`/`su` in the last
    `auth_window_hours` (24), counted per account, read straight from the auth
    log (`/var/log/secure` or `auth_log`) **wherever the collector's account may
    read it** — e.g. where it's group-readable (`installer` is in `securegrp` on
    spydur). A no-op, with no error, where the log is root-only. The actor is
    taken from the log's structured fields, matched on the line's head before any
    `COMMAND=` so a command string can't inject a fake account, and `su` to root
    is counted against whoever ran it.
  - **`setuid_roots` (off until set):** the probe scans the directories listed
    (e.g. `["/localscratch"]`) for a person's setuid/setgid files (basename
    only), staying on each root's own filesystem, following no symlink, bounded.
    "New" comes from the finding's first-seen — a file that appears alerts once.
  - The kinds, folding, severities and Console labels already existed (1.20), so
    these just add an unprivileged source. The root helper
    (`scripts/watch_root_probe.py` + `root_report`) still works where root is
    available instead. No database change.

## v1.22.0 (October 2026)

### Features
- The policy watch gains behaviour signals: the tells an AI agent (or a
  careless script) driving the cluster over the user's *own* ssh leaves — which
  look like the user typing to the /proc watch, so the watch reads what was
  done, not who typed it. All read-only, thresholds in the TOML, info/warning
  only.
  - **`heavy` (warning):** a user process holding real CPU — by lifetime
    average (its whole CPU time / its age), so a steady computation shows and a
    brief spike doesn't — of at least `heavy_cpu` cores (default 2), on a
    **login** node only (a compute node is where jobs are meant to be heavy).
    The long-standing "work running on the head node" catch; no root needed.
  - **`job_burst` (listed, not alerted):** at least `burst_jobs` sbatches by
    one user within `burst_minutes`. An array counts as one, so a big array
    isn't a burst; listed rather than alerted because a parameter sweep bursts
    the same way.
  - **`fail_fast` (warning):** at least `fail_fast_jobs` of a user's jobs that
    FAILED within `fail_fast_seconds` — an agent resubmitting what it doesn't
    understand (node failures don't count).
  - **`job_churn` (warning):** the same job name resubmitted `churn_repeats`
    times with cancellations between — a cancel/resubmit loop.
  - Config: `watch_heavy`, `heavy_cpu`, `heavy_min_seconds`, `watch_jobs`,
    `job_window_hours`, `burst_jobs`, `burst_minutes`, `fail_fast_jobs`,
    `fail_fast_seconds`, `churn_repeats` in `[collectors.policy_watch]` (both
    watches on by default; each no-ops where there's nothing to see). The job
    signals read this site's `jobs` table; `heavy` adds `--heavy` to the probe.
    No database change — new `kind` values in `policy_findings`, already synced.
- Console: the Policy watch page labels the four new kinds and names them in
  its intro (nomad_fix_73.py). No new route or scoping change.

## v1.21.0 (October 2026)

### Features
- The policy watch reaches GPU workstations and keys on the GPU. `workstations`
  lists GPU boxes to probe over ssh (role "workstation"); the findings travel
  the same pipeline to the hub and the Console. With `watch_gpu` (on by
  default; nothing where there is no GPU), the probe reads GPU **compute**
  processes with `nvidia-smi` — unprivileged, and never graphics (Xorg, a
  browser), so a desktop's ordinary GPU use doesn't show:
  - `gpu` (listed, not alerted): a GPU compute process, with the memory held,
    whether it is detached (no terminal — the shape of an unattended job) and
    whether it is an AI agent.
  - `gpu_served` (warning): a GPU reachable from the network — it listens on a
    port open to the network, or its account also runs a tunnel/relay/forward.
    The shape of reselling or an open inference server.
  - `gpu_min_mem` (256 MiB) drops a trivial compute context; `gpu_serve_hosts`
    names boxes where serving a GPU is sanctioned (then listed, not alerted);
    `ignore_programs`/`ignore_users` apply as everywhere.
  - Under-claim by design: detached only annotates, never alerts on its own;
    per-process GPU attribution is opaque under MIG or in containers, and a
    GPU another account serves is caught through its account's exposure unless
    the probe runs as root. See docs/policy_watch.md.
- Console: the Policy watch page labels the two new kinds (`gpu`, `gpu_served`)
  and names the workstations in its intro. No new route or scoping change.

## v1.20.1 (October 2026)

### Fixes
- Signals place an alert by what it is *about*, not where it was stored. A
  hub that raises a site's node alerts on its behalf (`alerts = "hub"`, 1.17.3)
  stores them in its own rows (`source_site` = the hub) while naming the site
  in `details.site`; the Insight engine attributed them by `source_site`, so
  the hub's Signals (e.g. mingus) showed another site's "node down" and that
  site's own Signals (e.g. vadur) hid it. The alert signal now reads unscoped
  and places each alert by `details.site`, falling back to `source_site`. No
  database change; the Console picks it up once nomad is upgraded on the hub.

## v1.3.1 (April 2026)

### Fixes
- Dashboard blank page regression from NumPy 2.x ABI incompatibility
- Metadata-based probe guards torch_geometric import against C-extension crashes
- Broadened exception handling in ML modules for non-ImportError failures

### Features
- Integrated issue reporting system (`nomad issue report/search/info`)
- Dashboard "Report Issue" tab with structured form and duplicate detection
- GitHub issue templates (bug report, feature request, question)
- Auto-acknowledgment bot via GitHub Actions
- Auto-labeling by source, component, version, and institution
- `nomad ref` entries for issue module and configuration

## v1.3.0 (April 2026)

### Features
- Reference system (`nomad ref`) with 60+ searchable topics
- `nomad ref search` for full-text documentation search
- Reference entries for all commands, configuration, and concepts

### Improvements
- Ruff linting fixes across reference module
- CI configuration excludes generated snippet files

## v1.2.6 (March 2026)

### Features
- System Dynamics module (`nomad dyn`) with 5 ecological/economic metrics
- Insight Engine (`nomad insights`) with multi-signal analysis and narratives
- Cloud monitoring (`nomad cloud`) for AWS, Azure, and GCP
- Dashboard tabs for Insights, Dynamics, and Cloud
- Alert enrichment with insight narratives and recommendations

### Improvements
- Console backend routes for insights and dynamics
- Demo data generator includes stress scenarios for insights
- MkDocs documentation for dynamics, insights, cloud, and reference

## v1.2.1 (February 2026)

### Features
- Proficiency score persistence to database
- Light/dark theme toggle in dashboard
- Combined edu figure in documentation

### Bug Fixes
- Fixed `_resolve_db_path` error in edu commands
- Text wrapping for recommendations output

### Documentation
- Expanded Educational Analytics section in paper
- Added Community Data Sharing details
- Reordered bibliography to match citation order

## v1.2.0 (January 2026)

### Features
- Educational Analytics module (`nomad edu`)
- Community dataset export (`nomad community`)
- Multi-cluster dashboard tabs
- Interactive session monitoring
- 3D force-directed network visualization

### Improvements
- Partition-specific dashboard views
- Node health reflects CPU/memory pressure
- Failed jobs modal with categories

## v1.1.0 (December 2025)

### Features
- ML prediction ensemble (GNN, LSTM, Autoencoder)
- Derivative analysis for trend detection
- Slack and webhook alert backends

## v1.0.0 (November 2025)

Initial release.
