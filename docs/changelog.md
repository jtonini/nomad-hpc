# Changelog

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
