# Training the model from the Console

From nomad 1.45.0 (with the Console's fix 110), an administrator trains the
model from **Data readiness** ("Train…"). The training runs not on the hub
but on a worker machine with a GPU, with
[TESSERA](https://pypi.org/project/tessera-ml/) (`tessera-ml` 0.3 or later).

## How it goes

1. An administrator asks on Data readiness. The request waits on the hub, in
   `ml.db` (beside `lab_check.db`, the nomad account's), with what it is to do
   (`[ml.train]` as it was then). One request at a time.
2. The worker asks the Console every 5 minutes whether there is one
   (`nomad ml work`, from a systemd timer). It speaks to the Console over
   HTTPS with its own key; nothing logs in to the worker, and it logs in to
   nothing.
3. It downloads the jobs' request-time figures, trains TESSERA and a logistic
   regression on the older jobs, scores the newest (held out) and those
   running or waiting, and sends back how well each did and the scores.
4. Data readiness shows the result; `nomad ml status` on the hub too.

## What leaves the hub

Per job, only what Slurm was asked at submission, as numbers: cores, GPUs,
memory, time asked, the hour and weekday it was submitted, its partition and
cluster numbered (not named), the month it was submitted; and what became of
it. Never a person, a job's name, a job's ID or a command line. The hub keeps
which row was which job.

Jobs: those submitted in the last `jobs_days` that ended (COMPLETED, FAILED,
TIMEOUT, OUT_OF_MEMORY, NODE_FAIL; a cancelled or preempted job is a decision,
not an outcome), at most the newest `max_jobs`; and those running or waiting.

What it predicts (`predicts`):

- **failure**: an ended job that didn't complete;
- **idle** (1.46.0, the default with failure): a completed job of 2 cores or
  more that ran 5 minutes or more and was measured, which on average left
  more than `idle_threshold` of the cores it asked for unused: the jobs the
  Recommendations judge for cores, judged as they judge them;
- **waste**: a completed job that used less than `1 - waste_threshold` of the
  time it asked for. Where most jobs don't set `--time` and get their
  partition's default, nearly every job counts (at the first site it was
  trained for, half the completed jobs used under 1% of the time they asked, and
  89% would count even at 0.9): the question then says little, which is why
  it is no longer asked by default.

## How well it did, honestly

The newest `holdout_share` of the ended jobs (by when they ended) are given
to TESSERA as unknown (label -1): in its similarity network, but learned from
by nothing. Only afterwards are its scores for them compared with what
happened, and with the scores of a logistic regression trained on the same
older jobs. Data readiness shows, per target:

- **AUC**: how often a model ranks a job that did ahead of one that didn't
  (0.5 is chance, 1 always), for TESSERA and the logistic regression;
- **of the tenth ranked riskiest**, how many did, against how many of all
  held-out jobs did;
- TESSERA's blend: how much each of its parts (GNN, LSTM, autoencoders)
  counted, by how well each did on its own validation jobs;
- with `compare` (1.45.1), each model's AUC without partition and cluster,
  and the logistic regression's with them alone: how much of what a model
  knows is where a job ran, rather than what it asked for;
- for idle and waste, how much of the cores or the time they asked for the
  jobs used (at the tenth, quarter, half, three quarters and nine tenths of
  them) and how many would count at a few thresholds: the counts to choose
  `idle_threshold` and `waste_threshold` by. When nearly every job counts,
  the AUCs say little, and the card says so;
- with `bootstrap` (1.46.0), the 95% range of each AUC and of the difference
  between the two models, from resamples of the held-out jobs (the same jobs
  for both models in each resample). Where the difference's range spans
  zero, the card says they do about as well, whatever their AUCs;
- with `rolling` (1.50.0), **period by period**: each of the last `rolling`
  periods of `rolling_days` (weeks by default) held out in turn. For each,
  both models are trained again on the jobs that ended before it and tested
  on the jobs that ended in it; no later job, and none running or waiting, is
  in TESSERA's network;
- with `people_folds` (1.50.0), **new people**: everyone is dealt into that
  many groups (the busiest first, each to the group with the fewest jobs so
  far, so no group is one person's arrays). For each group, both models are
  trained on everyone else's jobs from before the holdout and tested on the
  group's held-out jobs: people neither model saw, later. Only the group's
  number leaves the hub, never who is in it;
- for both: each test's AUCs and the range of their difference (and, with
  `compare`, the same without partition and cluster); then over the tests,
  each model's mean AUC and in how many the difference was clear of zero
  either way. One holdout is one test: a result that holds week after week,
  and for people the models never saw, is one to report. Each test trains
  both models again (twice with `compare`), so a run takes longer.

With `thin` (1.50.0), of the jobs one person submitted on one day with the
same request (partition, cluster, cores, GPUs, memory, time), at most that
many are used, picked at random but the same each time. On a small cluster a
few people's job arrays can be most of the jobs: thousands of copies of one
request would weigh as much as everyone else together, and the same request
would be both learned from and tested on. Thinning comes before `max_jobs`,
so the newest `max_jobs` reach further back. `nomad ml preview` says, in
counts, what a request would be sent now: how many jobs, when they ended,
how many people and how much the busiest weigh, and each test's size.

**What a warning would have been worth** (1.51.0), in every test: had each
model warned about the tenth of the jobs it ranked first, how often it would
have been right and what share of the jobs that did it those were; and a
**simple rule** to beat, which needs no model: warn about a job when the
same person's last job (the last one that had ended when it was sent) did
it. On the first site's clusters "their last job failed" was right about 6 times in 10
and caught about two thirds of the failures, where the models, from what a
job asked for, were right about 4 times in 10. Beside the rule, each model
warning about as many jobs. Then whether any of them is good enough to warn
people: right at least `warn_right` (0.8) of the time, and catching at
least `warn_caught` (0.5) of the jobs that did it.

When the logistic regression does as well or better, it says so.

## Settings

On the hub, in nomad.toml (its own table: the older `[ml]` keys are another
thing's):

```toml
[ml.train]
enabled = true                   # the Console offers Train… (administrators)
engine = "tessera"               # or "baseline": a logistic regression only
predicts = ["failure", "idle"]   # or with "waste"
trainer = "gpuws1"               # the worker's name, as the Console says it
jobs_days = 365
max_jobs = 50000                 # the newest ended jobs (0: all); the similarity
                                 # network's time grows as the square of the jobs
waste_threshold = 0.6
idle_threshold = 0.5             # (1.46.0)
holdout_share = 0.2
bootstrap = 1000                 # resamples for the ranges (0: none) (1.46.0)
keep_runs = 20                   # runs kept with their scores
stale_minutes = 360              # a run not heard from this long is given up
key_file = "~/.config/nomad/ml_worker_key"   # the nomad account's (600)
# db = "ml.db"                   # default: beside lab_check.db
compare = true                   # also each model without partition and cluster, and the
                                 # logistic regression with them alone (1.45.1)
thin = 0                         # at most this many of one person's same request a day (0: all) (1.50.0)
rolling = 0                      # the last this many periods held out in turn (0: not; up to 26) (1.50.0)
rolling_days = 7                 # each period's length in days (1.50.0)
people_folds = 0                 # new people: this many groups (0: not; 2 to 10) (1.50.0)
warn_right = 0.8                 # good enough to warn people: right this often (1.51.0)
warn_caught = 0.5                # ... and catching this share of the jobs that did it

[ml.train.epochs]
gnn = 100
lstm = 60
ae = 60

[ml.train.tessera]               # (1.45.1)
similarity = "euclidean"         # or cosine, mahalanobis, pearson, bray_curtis (on scaled
                                 # features); simpson, jaccard (on n_bins equal-width bins)
n_bins = 3                       # simpson and jaccard only
k_neighbors = 10                 # each job's nearest neighbours in the network
```

Both models get the same columns: what was asked, as numbers (cores, GPUs,
memory and time on a log scale; the hour and day it was submitted), and
partition and cluster as one column per value. In 1.45.0 TESSERA had
partition and cluster as plain numbers, and its default similarity
(simpson) cuts each feature into three equal-width bins between its smallest
and largest value, so a partition, or a memory or time asked, was only low,
middle or high. Each run keeps the settings it was asked with.

On the worker:

```toml
[ml.worker]
console = "https://nomad.example.edu"   # the Console it asks
key_file = "~/.config/nomad/ml_worker_key"
device = "cuda"                         # or "cpu", "auto"
# ca_file = "/etc/pki/tls/certs/campus-ca.pem"
# name = "gpuws1"                       # default: the machine's short name
```

## Setting it up

On the hub, as the account the Console runs as, make the worker's key:

```
sudo -u nomad nomad ml key
```

On the worker, a Python environment with nomad and TESSERA (PyTorch with
CUDA comes with them from PyPI), the key copied from the hub (never pasted
anywhere else), its settings, a check, and the timer:

```
sudo install -d -o "$USER" -g "$(id -gn)" -m 755 /opt/nomad-ml
python3 -m venv /opt/nomad-ml && /opt/nomad-ml/bin/pip install 'nomad-hpc[predict]'
sudo restorecon -R /opt/nomad-ml     # where SELinux is on: the default labels (bin/ is bin_t)
umask 077 && mkdir -p ~/.config/nomad && ssh you@hub 'sudo -n -u nomad cat /home/nomad/.config/nomad/ml_worker_key' > ~/.config/nomad/ml_worker_key
/opt/nomad-ml/bin/nomad -c ~/.config/nomad/ml_worker.toml ml key      # the same fingerprint as on the hub
/opt/nomad-ml/bin/nomad -c ~/.config/nomad/ml_worker.toml ml check    # reaches the Console; can it train here
sudo /opt/nomad-ml/bin/nomad -c ~/.config/nomad/ml_worker.toml ml timer --user $USER --apply
```

The environment goes in `/opt`, not in a home folder: the timer runs it as a
system service, and where SELinux is enforcing (Rocky, RHEL) a system
service may not run programs from a home folder (`user_home_t`). It fails
at once with status 203/EXEC and an AVC denial ("denied { execute }",
`init_t`); `/opt/*/bin` has the label of system programs (`bin_t`). The
key and the settings can stay in the home folder: the service only reads
them.

`nomad ml check` says what the Console answered (on or off, what is queued)
and whether TESSERA can train on this machine: its version, PyTorch's, and
the GPU it would use, or why not. The worker never follows a redirect: a
sign-in page in front of the Console would otherwise be sent the key. If
the web server in front of the Console asks for a campus sign-in, let
`/api/ml/worker/` through to the Console (it admits only the worker's key,
and refuses every person), or point `[ml.worker] console` at an address
the Console answers at itself.

## Commands

```
nomad ml status [--json]                        the request queued or running, the last result (hub)
nomad ml ask [--note "..."]                     queue a request, as Train… does (hub)
nomad ml cancel ID                              a queued request (hub)
nomad ml scores ID failure|waste [--part now|holdout] [--top N]   a run's riskiest jobs (hub: names them)
nomad ml preview                                what a request would be sent now, in counts (hub)
nomad ml key                                    make (hub) or check (worker) the key: its fingerprint
nomad ml check                                  the worker reaches the Console with its key
nomad ml work                                   one pass: take a request, if any, train, answer
nomad ml timer [--apply]                        that, every 5 minutes (nomad-ml-worker.timer)
```
