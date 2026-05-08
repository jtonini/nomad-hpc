# Idea 18 Component 1 — Threshold Design Handoff

**Author:** João Tonini
**Date:** 2026-05-08
**Status:** Validation complete, ready for implementation
**Source data:** `scripts/validation/results/{spydur,arachne_head,arachne_node02}_summary.txt` plus the three SQLite databases they were derived from

This document hands off the validation findings to the chat that will implement
NOMAD Idea 18 Component 1 (per-user process tracking on head nodes and
monitoring servers). It is self-contained — the implementer does not need to
read the validation chat that produced it.

---

## TL;DR

A passive probe ran for 7 days on two head nodes (spydur, arachne) and 48 hours
on one compute node (arachne node02), recording per-process CPU, memory, runtime,
and (on the compute node only) per-process file-descriptor paths.

Three findings drive the production design:

1. **Head node misuse is real but quieter than expected.** Across two clusters
   over a week, only a handful of genuine misuse events. Whitelisting scheduled
   sysadmin tools by parent-path eliminates all false positives.

2. **Memory matters as much as CPU on head nodes.** Two of the most
   impactful misuse cases (perickso's R, abezerra's IDE language server) were
   relatively low-CPU but held multiple GB of RAM for hours. CPU-only alerting
   misses them.

3. **Storage misuse on compute nodes is the strongest signal in the data.**
   On a single 48h compute-node run, fd attribution showed one user (hw6is)
   writing 100% to NFS and 0% to a 4.3TB local SSD that was 99% empty. This
   is the canonical "Idea 18 insight" pattern, captured live, with no
   ambiguity.

Probe overhead across all three deployments stayed at 0.12–0.20% CPU and
18–23 MB RSS. Production-ready as designed.

---

## Validation runs

| Probe         | Duration | Privilege       | Ticks  | Sample rows | fd rows | Distinct users |
|---------------|----------|-----------------|--------|-------------|---------|----------------|
| spydur        | 7d       | installer (no sudo) | 10080 | 405,328     | n/a     | 28             |
| arachne head  | 7d       | root (sudo)     | 10080  | 531,487     | n/a     | 11             |
| arachne node02| 48h      | root            | 2880   | 165,483     | 247,813 | 3              |

Zero missed sampling ticks across all three. All probes exited cleanly via
their `finally` block (`ended_utc` written to meta).

Probe overhead summary:

| Machine       | Avg CPU% | Max CPU% | Avg RSS (MB) | Avg wall/sample (s) |
|---------------|----------|----------|--------------|---------------------|
| spydur        | 0.16     | 0.30     | 23.0         | 0.095               |
| arachne head  | 0.20     | 0.30     | 19.0         | 0.133               |
| node02 (+fd)  | 0.12     | 0.20     | 18.7         | 0.083               |

The compute node run was *lighter* than the head nodes despite walking ~102
fds per sample, because there are far fewer top-level processes on a compute
node (94 distinct PIDs over 48h) than on a head node (3,000–15,000 over 7d).

---

## Finding 1: head node CPU misuse — calibrated, but the rule needs companions

### Spydur, 7 days, 28 distinct users

The proposed rule (≥10% CPU sustained for ≥5 consecutive 60-second samples)
fired **7 times** across the week. After investigating each event, the
breakdown is:

| User      | Event                                              | Genuine misuse? |
|-----------|----------------------------------------------------|-----------------|
| ia3nk     | gmx_mpi trjconv, 161 min at 95% CPU, via Singularity in interactive ssh | Yes |
| perickso  | R, 109 min, 6 CPU bursts to 670% (multi-core), 75GB RSS | Yes (memory more than CPU) |
| arobbins  | clusterbackup.py × 5 nightly runs, 7–62 min each, ~20% CPU | No — system cron tool |

Process ancestry confirmed all of this. ia3nk's chain was
`sshd → bash → singularity → bash → gmx → gmx_mpi` — a fully interactive
ssh session running scientific computation on the head node. perickso's was
`sshd → bash → R`, holding 75 GB across the run. arobbins's five events
all had `bash /usr/local/sw/clusterbackup/clusterbackup.sh` as their parent —
a system-installed tool invoked nightly via cron.

**Without whitelisting, false positive rate is 5/7 = 71%.**
**With a parent-path whitelist on `/usr/local/sw/*` and `/opt/*`, false positive rate is 0%.**

Whitelisting is therefore not optional. It is part of the rule.

### Arachne head, 7 days, 11 distinct users

The same 10%/5min rule fired **4 times**, all from the same user
(abezerra), all from the same application: the Antigravity IDE's
remote-development language server. Process ancestry on all four:

```
sshd: abezerra@notty   (non-interactive ssh — IDE-driven)
bash -s
sh .../antigravity-server/bin/...
node .../antigravity-server/bin/node
node ... (child)
language_server_linux_x64
```

abezerra is using the Antigravity IDE on a laptop with arachne as the
remote-development target. The IDE silently runs all its backend services
(file watching, indexing, language server) on the head node over 12 hours
of coding sessions across 4 days.

This is misuse under arachne's policy ("interactive analysis goes via
`srun -p partition`, not directly on the head node"), but it's a *different*
flavor of misuse than spydur's — quieter, longer-running, memory-driven
rather than CPU-driven.

### A short sustained event the analyzer missed

The arachne head data contained one genuine misuse event the 5-minute rule
did not flag: `sumo-bandplot`, a DFT band-structure plotting tool, ran at
99.2% CPU for ~2 minutes. The duration cutoff filtered it out.

This is informative. A user pinning a scientific binary at 99% CPU on the
head node for 2 minutes *is* misuse, just brief. The current rule is
calibrated too leniently for short-but-intense events.

### Recommended head-node CPU rules

```toml
[collectors.per_user]
# Multiple rules can fire on the same process. Any match generates an alert.
alerts = [
    { cpu_percent = 10, duration_minutes = 5 },   # sustained moderate
    { cpu_percent = 50, duration_minutes = 2 },   # sustained high
    { cpu_percent = 80, duration_minutes = 1 },   # short burst
]
```

The three-rule structure catches:
- Long-running batch-style work at modest CPU (arobbins-style — but
  whitelisted by parent-path)
- Sustained interactive analysis (ia3nk's gmx_mpi at 95% for 2.7 hours)
- Short scientific binary bursts (sumo-bandplot at 99% for 2 minutes)

---

## Finding 2: head node memory rule is essential, not optional

CPU is not the dominant impact signal for several of the events we saw.

| User       | Peak CPU | Peak RSS | Duration | What we'd miss with CPU-only |
|------------|----------|----------|----------|------------------------------|
| perickso (R) | 670%   | 75 GB    | 109 min  | The 75GB is the actual harm; CPU caught it incidentally |
| abezerra (antigravity) | 34% | 2.4 GB | 148 min | Caught only because peak hit 34%; below 10% it'd be invisible |
| abezerra (antigravity, longest) | 25% | 1.1 GB | 6.4 hours | Closer to a wash on CPU; clearly memory-driven |

A user holding 75 GB of RAM on a shared head node for two hours is impactful
regardless of whether their CPU is at 5% or 500%. The arachne head node has
512GB total RAM, so 75GB is ~15% — not catastrophic but not negligible. On a
smaller cluster's head node the same pattern would be a serious reliability
problem.

Across the per-command catalog on arachne head, the
`language_server_linux_x64` family peaked at **12.1 GB RSS** across the week.
That's not from one process — it's the maximum across 16 distinct PIDs of
the same binary. This is a normal characteristic of language servers; they
hold codebase indexes in memory.

### Recommended head-node memory rules

```toml
[collectors.per_user]
alerts = [
    # ... CPU rules above ...
    { memory_gb = 4, duration_minutes = 10 },    # tunable per cluster
    { memory_gb = 16, duration_minutes = 2 },    # immediate flag
]
```

Threshold values should be **tunable per cluster** because memory norms
differ. A 4GB threshold makes sense on a 64GB head node and is meaningless
on a 512GB one. Suggested defaults are starting points, not absolutes.

---

## Finding 3: compute node storage misuse is the strongest signal

This is the finding that justifies most of Idea 18's design.

48 hours on arachne node02 (3 users, fully allocated). fd attribution
recorded **247,813 path resolutions**. After classifying paths into
filesystem buckets:

```
user            local    tmp   nfs_h  system  other
hw6is               0      0  138240   5760      0
jsiegel         46080      0   28800  11520      0
abezerra            0  10284    4754   1584    791
```

The drill-down on `nfs_home` paths:

```
hw6is:
   69120  /home/hw6is/COLUMBUS/4-5pyrimidine/CAS1210/TZ/Trip
   69120  /home/hw6is/COLUMBUS/2-4pyrimidine/CAS1210/TZ/Trip

jsiegel:
   28800  /home/jsiegel/SummerResearchArachne/DiradicalProje

abezerra:
    4754  /home/scratch/abezerra/testAgentic/BLMNT8/03_bands
```

Read against arachne's storage layout (`/localscratch` is local NVMe, 4.3TB,
99% empty per node; `/home` is NFS, 133TB total):

**hw6is** ran two parallel COLUMBUS jobs writing exclusively to NFS
home. Zero use of local scratch. This is the single clearest example of
the pattern Idea 18 was built to detect.

**jsiegel** ran COLUMBUS jobs that *do* use localscratch (46K fds in
`/localscratch/jsiegel/columbus722_*/`) but also keep significant NFS home
activity (28K fds in `DiradicalProject`). Mixed pattern — the user knows
about localscratch but isn't fully committed.

**abezerra** ran VASP jobs using `/tmp` (tmpfs, 16GB RAM-backed) for hot
intermediate files (`OUTCAR`, `CHG`, `CONTCAR`, etc.) plus 4754 fds in a
`/home/scratch/abezerra/testAgentic/...` path on NFS. Fast but
RAM-constrained for working files; some non-trivial NFS for the rest.

### What this means for the production system

Component 1 (per-user CPU/memory tracking) doesn't directly produce these
insights — Component 2 (local disk monitoring) plus the Insight Engine
(Idea 5) does. But the fd-walking infrastructure built into Component 1's
collector is what makes Component 2 possible. The validation confirms:

- `/proc/<pid>/fd` walking works at production scale (102 fds/sample,
  100% read success when running as root).
- Per-process per-filesystem attribution is reliably computable.
- Overhead is negligible (0.12% CPU, 18.7 MB RSS).
- The classification needs to be **role-aware**: on a compute node, alerting
  on per-user CPU is meaningless (every job exceeds any CPU rule). The
  signal there is storage.

---

## Cross-cutting observations

### Process tree depth

abezerra's antigravity-server was 5 levels deep from sshd:
`sshd → bash → sh → node → node → language_server_linux_x64`. ia3nk's
gmx_mpi was 6 levels deep with Singularity in the chain.

**Production must track ancestry to at least depth 6** for both
whitelist matching (a process is whitelisted if *any* ancestor's path matches
a whitelist entry) and for educational insights ("this came from your
SSH session running an IDE plugin," not "you ran node directly").

### Singularity containers on the head node

ia3nk's case is particularly instructive: the user has a Singularity
container that lets them run GROMACS interactively on whatever machine they
ssh into. From their perspective, the friction of "where am I?" disappears
once the container is set up — which means the friction of "should this be
on a compute node?" also disappears. This is a workflow choice that bypasses
SLURM by construction.

The educational fix is concrete: same container, just `srun --pty` first.

```
srun --pty -n1 -c4 --mem=16G --time=4:00:00 bash
singularity shell ~/gromacs.sif
gmx trjconv ...
```

The `nomad edu me` insight should produce this exact command pattern, not
just "use a compute node." Concrete commands are what make compliance
frictionless.

### Service accounts and whitelist scaling

Service-account scan results across both clusters:

- **Spydur:** `slurm` (uid 1001) and `installer` (uid 1010) were the only
  uid<5000 accounts visible. Real users were uid >280000.
- **Arachne head:** `zeus` (uid 1000) was the only one. Real users had
  varied uids.

The `--extra-system-users` flag (slurm, munge) was sufficient on spydur. No
additional service accounts surfaced during the week-long run. **The
whitelist scaling problem is small and bounded** — typically 5–20 entries
per cluster, not hundreds.

### Cluster culture differs sharply

Spydur: multiple users (perickso, ia3nk, possibly arobbins's tool) running
batch-style scientific work on the head node. Several distinct misuse
patterns.

Arachne head: zero batch-compute misuse. Only one user (abezerra) showing
elevated activity, and that activity is IDE-related. Arachne's user
community appears better disciplined about head-node use, or arachne has
better policy enforcement.

The same default rules will produce different alert volumes on different
clusters. The implementation should expect this and avoid assumptions like
"X alerts per week is healthy" — it depends on cluster culture.

---

## Recommended `nomad.toml` schema

Starting point for Component 1's config section. Field names match the
existing collector convention.

```toml
[collectors.per_user]
enabled = true

# Roles where this collector is active. Compute nodes excluded by default
# (every job triggers any reasonable CPU rule).
roles = ["headnode", "monitoring"]

# Sample interval. 60s validated. Going below 30s probably increases noise
# without adding signal.
sample_interval_seconds = 60

# Process ancestry depth for whitelist matching. Validated cases needed
# depth 5–6.
ancestry_depth = 8

# Multiple rules — any match generates an alert.
[[collectors.per_user.alerts]]
type = "cpu"
threshold_percent = 10
duration_minutes = 5

[[collectors.per_user.alerts]]
type = "cpu"
threshold_percent = 50
duration_minutes = 2

[[collectors.per_user.alerts]]
type = "cpu"
threshold_percent = 80
duration_minutes = 1

[[collectors.per_user.alerts]]
type = "memory"
threshold_gb = 4
duration_minutes = 10

[[collectors.per_user.alerts]]
type = "memory"
threshold_gb = 16
duration_minutes = 2

# Whitelist by parent-path (recursive — children of whitelisted ancestors
# are also whitelisted).
[collectors.per_user.whitelist]
parent_paths = [
    "/usr/local/sw/",
    "/opt/",
    "/var/spool/cron",
]
# Service accounts that should never produce alerts even if uid >= 1000.
users = ["slurm", "munge"]
# Cluster-specific overrides (basename match on command + user).
user_commands = [
    # ["someuser", "specific_tool.py"],
]

# Per-uid floor — accounts below this are treated as system regardless.
min_uid = 1000

# fd walking — enable per role. On head/monitoring nodes the value is for
# diagnostic logging; on compute nodes it powers Component 2.
[collectors.per_user.fd_walking]
headnode = false       # not useful — no Component 2 on head nodes
monitoring = false
compute = true         # required for Component 2 + Idea 5 insights
sample_subset = 1.0    # fraction of sampled processes to fd-walk; 1.0 = all
```

---

## Open questions deliberately deferred

These came up in the validation but are out of scope for Component 1
implementation. They belong in subsequent ideas/chats.

### Where do alerts go?

Component 1 detects, but doesn't decide what happens after. Likely
integrations:

- `nomad insights` (admin-facing list of recent flagged events)
- `nomad edu me` (user-facing — "you did X, here's what to do instead")
- `nomad brief` (daily/weekly summary narratives)
- Idea 7 (`nomad dyn externality` — quantifying impact on other users)

Pick whichever scope makes Component 1 implementation tractable; the
integration with Idea 5 (Insight Engine) is where the user-visible product
lives.

### How does the production collector interact with `pacct` vs psutil?

The validation probe used psutil exclusively. Production should be able to
use `pacct` (process accounting) where available — it's lighter and catches
short-lived processes that 60s polling misses. The `sumo-bandplot` case
(2 minutes at 99% CPU) is exactly the kind of event pacct catches and
psutil polling sometimes misses.

The pacct/cgroup mechanism from Idea 14/16 should compose with this
collector. How exactly is for the implementation to decide.

### Privilege requirements

Spydur's NOMAD account (`installer`) does not have sudo. The probe ran as
installer and saw what it could see — process metadata across all users,
but `/proc/<pid>/io` was AccessDenied for non-installer processes (320 of
391 sample rows in the smoke test). For Component 1's CPU rules this
doesn't matter; CPU% is visible without sudo. For Component 2's fd walking
it absolutely matters; sudo (or `CAP_DAC_READ_SEARCH`) is required.

This is a real deployment question, not just a technical one. The current
options are:

1. Run NOMAD's collector as root via systemd
2. Get a privileged service account on spydur (talk to George)
3. Use file capabilities on the collector binary (`setcap CAP_DAC_READ_SEARCH+ep`)
4. Run a separate root-privileged collector for the bits that need it,
   keeping the rest unprivileged

Implementation should pick one and document the assumption.

### "Should the antigravity-server case alert at all?"

It's misuse under arachne's policy, but it's a coachable case rather than
a clear-cut one. A user using their IDE remote-development feature is doing
the right *kind* of thing in the wrong *place*; the educational message
should be different from "stop running batch jobs on the head node."

Worth thinking about during implementation: do we mark some alerts as
"informational" rather than "actionable" so admins can triage by severity?

---

## Files in this handoff

- `scripts/validation/results/spydur_summary.txt` — analyzer output for spydur
- `scripts/validation/results/arachne_head_summary.txt` — analyzer output for arachne head
- `scripts/validation/results/arachne_node02_summary.txt` — analyzer output for node02 (incl. fd attribution)
- `scripts/validation/results/*_baseline_*.db` — raw SQLite databases (75MB / 78MB / 65MB)
- `scripts/validation/nomad_validation_probe.py` — the probe itself
- `scripts/validation/analyze_validation.py` — the analyzer

The DBs are queryable indefinitely. Any new question that comes up during
implementation can be answered by querying them directly rather than
rerunning the probe.
