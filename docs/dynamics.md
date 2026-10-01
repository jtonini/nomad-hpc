# System Dynamics

The `nomad dyn` command family applies quantitative frameworks from ecology and economics to research computing resource usage patterns.

Every command takes `--db PATH`, `--hours N` and `--site NAME`. On a hub's combined database (`nomad sync`), `--site` reads one site through read-only views; nothing is copied. A combined database needs `--site`: pooling sites mixes their nodes and jobs (two sites' `node01` become one node flapping between states).

## Commands

### Full Summary
```bash
nomad dyn summary --db nomad.db
nomad dyn summary --db ~/.local/share/nomad/combined.db --site spydur
```
Generates a comprehensive narrative combining all dynamics metrics.

### Diversity
```bash
nomad dyn diversity --by user --hours 720
nomad dyn diversity --by group
nomad dyn diversity --by partition
```
Computes Simpson's and Shannon diversity indices over people, research groups, or partitions.

**Simpson's D** = 1 - sum(p_i^2) — probability that two randomly chosen jobs belong to different categories.

**Shannon H'** = -sum(p_i * ln(p_i)) — information-theoretic uncertainty about category membership.

**Pielou's J** = H' / ln(S) — evenness of distribution across categories.

When one category holds more than 60% of the jobs, the result says so ("One person accounts for 94% of the 7,946 jobs in this window"). The trend leaves out weekly windows with fewer than 20 jobs; with fewer than three left it is "too few weeks", not "stable".

### Niche Overlap
```bash
nomad dyn niche --hours 720
```
Measures pairwise resource usage overlap between research groups using Pianka's overlap index. Needs a group per job (below).

### Carrying Capacity
```bash
nomad dyn capacity
```
Multi-dimensional capacity: CPU cores and memory **allocated** on the nodes that can take jobs (allocated ÷ what they have, per snapshot), GPU utilization as measured, the **busiest** disk's utilization (an average over every disk hides the one that is saturated), and queue pressure (pending jobs per running job, 3 counted as full). Each is averaged per hour; "current" is the latest hour.

The busiest resource is named; it is called the **binding constraint** only at 75% or more (`BINDING_AT`). At 28% nothing binds. The queue is shown but never binds: pending jobs include held, dependent and throttled ones.

### Resilience
```bash
nomad dyn resilience
```
Recovery time after disturbances: a node going down or stopping to respond, and hours when jobs failed at more than twice the usual rate (with at least 10 jobs and 5 failures in the hour; an hour with fewer jobs ends a spike, and one still running at the end of the window is reported as ongoing). Node states are read as whole Slurm tokens, so `POWERED_DOWN` is a cloud node at rest and `IDLE*` is not responding. With fewer than four recovered events the trend is "too few events", not "stable".

**Drains are listed but not scored.** Most are an administrator taking a node out on purpose — maintenance, vendor repair — and scoring them made planned work look like fragility. With no node states or jobs to read, the score is empty ("nothing to read"), not 100.

### Externality
```bash
nomad dyn externality --hours 720
```
Looks for pairs of groups where one group's resource use rises and falls with another group's failure rate (failed, node failure, out of time or memory; cancelled jobs left out), over at least 12 shared hours. A correlation, not proof that one causes the other. Needs a group per job (below).

## Groups need a group per job

Diversity by group, niche overlap and externalities count jobs per group. Joining jobs to `group_membership` by username counts a job once for **every** group its owner belongs to: on one real cluster 7,946 jobs became 38,971 rows, groups sharing one busy member looked identical (overlap 1.00), and group-to-group correlations were drawn from the same jobs counted several times.

So these analyses run only when each job can be placed in one group (`nomad.dynamics.attribution`):

1. the job records a group itself — an `account` column on `jobs`, or `group_name` — when at least two groups of two or more people hold 80% of the jobs and no single one covers more than 80% of the people (a catch-all Unix group such as `people`, or one private group per user, says nothing). nomad's job collector does not record the Slurm account on `jobs` yet; or
2. membership is unambiguous — everyone who ran jobs belongs to at most one research group (leaving out umbrella groups that hold more than 80% of users), there are at least two groups of two or more people, and they account for 80% of the jobs.

Otherwise the result is `available: false` with the reason, for example: *"Jobs don't record a group, and 12 of the 40 people who ran jobs in the last 12 weeks belong to more than one group, so their jobs can't be placed in one. Group views need a group per job, such as a Slurm account."* The reason names the period it counts: diversity by group places jobs over its trend windows (12 weeks by default), the other analyses over the window asked for. Diversity by person is always computed.

In Python, `attribution="membership"` forces the old join for callers that want it knowingly; the reason then says how many people are counted once per group.

## JSON

`nomad dyn summary --json` (and `DynamicsEngine(...).to_dict()`) returns `diversity` (by group, with `available` and `reason`), `diversity_by_user`, `niche`, `capacity` (with `binding_constraint`, `busiest`), `resilience` (with `counted_events`, `drains`), `externality`, `attribution`, `site` and `hours`.

## Theoretical Foundation

These metrics are adapted from ecological and economic frameworks for their mathematical properties, not as ontological claims. Users are intentional agents shaped by decisions and incentives, not natural selection. See Tonini (2026), *Ecological, Economic, and Governance Metrics for Research Computing*.
