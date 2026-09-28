# NØMAÐ Validation Probe

Two scripts for characterizing per-user process activity on a machine before
deploying Idea 18 alerting thresholds. Passive: collects, doesn't alert.

## What this answers

1. Per-user CPU distribution at steady state (50th/90th/99th percentile).
2. False-positive count of the proposed `10% CPU for 5 minutes` rule.
3. Process catalog — what commands legitimately exceed the threshold (your
   real whitelist).
4. Probe overhead — does this thing fit on a head node.
5. (Optional, compute nodes) `/proc/<pid>/fd` filesystem attribution.

## Setup

On each target machine, as the user that NØMAÐ runs as (cazuza on badenpowell,
jtonini on arachne):

    python3 -c "import psutil; print(psutil.__version__)"
    # If not present:
    pip install --user psutil

Drop both scripts in `~/nomad_validation/` (or anywhere). Output DB defaults
to `~/nomad_validation/<machine>_baseline_<timestamp>.db`.

## Run

### spydur (head node, 1 week)

    cd ~/nomad_validation
    nohup python3 nomad_validation_probe.py \
        --machine spydur \
        --duration 168h \
        --interval 60 \
        > spydur_probe.log 2>&1 &
    echo $! > spydur_probe.pid

### arachne head node (1 week)

    nohup python3 nomad_validation_probe.py \
        --machine arachne-head \
        --duration 168h \
        > arachne_head_probe.log 2>&1 &

### arachne compute node (24-48h, with fd walking)

Pick a node that's actually running jobs. The fd walking is what validates
that NFS-vs-local I/O attribution will work when component 1 is built.

    nohup python3 nomad_validation_probe.py \
        --machine arachne-node05 \
        --duration 48h \
        --walk-fds \
        > arachne_node05_probe.log 2>&1 &

### Test run first

Before committing to a week, do a 5-minute test to confirm it writes data:

    python3 nomad_validation_probe.py --machine TEST --duration 5m --interval 30

Then check:

    python3 -c "
    import sqlite3, glob
    db = sorted(glob.glob('~/nomad_validation/TEST_baseline_*.db'))[-1]
    c = sqlite3.connect(db)
    print(c.execute('SELECT COUNT(*) FROM samples').fetchone())
    print(c.execute('SELECT COUNT(*) FROM overhead').fetchone())
    "

If `samples` is 0, your `--min-uid` is wrong for this machine, or no
user processes are running.

## Stop early

    kill $(cat spydur_probe.pid)

The probe traps `SIGINT`/`SIGTERM`, flushes, and writes `ended_utc` to meta.

## Analyze

When the runs finish, copy the DBs to wherever you want to analyze (or run
in place):

    python3 analyze_validation.py ~/nomad_validation/spydur_baseline_*.db
    python3 analyze_validation.py ~/nomad_validation/arachne-node05_baseline_*.db --cpu-threshold 10 --duration-minutes 5

To test alternative thresholds without rerunning the probe:

    python3 analyze_validation.py db.sqlite --cpu-threshold 20 --duration-minutes 2
    python3 analyze_validation.py db.sqlite --cpu-threshold 5 --duration-minutes 15

## What to bring back to the implementation chat

A one-page summary per machine:

- 95th-percentile per-user CPU
- Number of distinct elevated events at the proposed threshold
- Of those events, how many are legitimate (after eyeballing the command catalog)
- Suggested revised threshold
- Probe overhead (avg/max CPU%, RSS MB, wall seconds per sample)
- For the compute node only: does fd attribution look sensible? (e.g.,
  jobs writing to `/scratch/$USER/...` show NFS fds; jobs writing to
  `/localscratch/...` show local fds)

That summary is what shapes Component 1's production design.

## Schema reference

`samples` — one row per (timestamp, pid):
  timestamp, pid, ppid, username, uid, command, cmdline,
  cpu_percent, memory_rss_mb, runtime_seconds,
  io_read_bytes, io_write_bytes, num_threads, status

`fd_paths` — one row per (timestamp, pid, fd), only when `--walk-fds`:
  timestamp, pid, fd, path

`overhead` — one row per sample tick:
  timestamp, probe_cpu_percent, probe_memory_rss_mb,
  sample_wall_seconds, n_processes_seen, n_processes_recorded, n_fds_walked

`meta` — key/value run config.
