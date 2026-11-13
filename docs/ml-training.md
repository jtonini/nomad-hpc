# Training the model from the Console

From nomad 1.45.0 (with the Console's fix 110), an administrator trains the
model from **Data readiness** ("Train…"). The training runs not on the hub
but on a worker machine with a GPU (at Richmond, badenpowell), with
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
- **waste**: a completed job that used less than `1 - waste_threshold` of the
  time it asked for.

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
- for waste, how much of the time they asked for the completed jobs used (at
  the tenth, quarter, half, three quarters and nine tenths of them) and how
  many would count as wasted at a few thresholds: the counts to choose
  `waste_threshold` by. When nearly every job counts as wasted, the AUCs say
  little.

When the logistic regression does as well or better, it says so.

## Settings

On the hub, in nomad.toml (its own table: the older `[ml]` keys are another
thing's):

```toml
[ml.train]
enabled = true                   # the Console offers Train… (administrators)
engine = "tessera"               # or "baseline": a logistic regression only
predicts = ["failure", "waste"]
trainer = "badenpowell"          # the worker's name, as the Console says it
jobs_days = 365
max_jobs = 50000                 # the newest ended jobs (0: all); the similarity
                                 # network's time grows as the square of the jobs
waste_threshold = 0.6
holdout_share = 0.2
keep_runs = 20                   # runs kept with their scores
stale_minutes = 360              # a run not heard from this long is given up
key_file = "~/.config/nomad/ml_worker_key"   # the nomad account's (600)
# db = "ml.db"                   # default: beside lab_check.db
compare = true                   # also each model without partition and cluster, and the
                                 # logistic regression with them alone (1.45.1)

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
# name = "badenpowell"                  # default: the machine's short name
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
nomad ml key                                    make (hub) or check (worker) the key: its fingerprint
nomad ml check                                  the worker reaches the Console with its key
nomad ml work                                   one pass: take a request, if any, train, answer
nomad ml timer [--apply]                        that, every 5 minutes (nomad-ml-worker.timer)
```
