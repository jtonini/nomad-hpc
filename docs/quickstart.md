# Quick Start

## Try it without a cluster

```bash
pip install nomad-hpc
nomad demo --no-launch
```

writes `~/nomad_demo.db`: a made-up cluster with ten nodes (GPUs among them),
a few weeks of jobs, storage, lab workstations and a little cloud use, with
some trouble put in on purpose (a scratch disk filling, GPU jobs out of
memory, a long GPU queue) so that there is something to find. Then:

```bash
D=~/nomad_demo.db
nomad status --db $D                 # disks, queues, I/O now
nomad facts sites --db $D            # each site: freshness, nodes, jobs, waits
nomad insights brief --db $D         # what needs attention, in sentences
nomad dyn summary --db $D            # system dynamics: capacity, resilience
nomad alerts --db $D                 # the alerts the demo raised
```

`nomad demo` without `--no-launch` also opens the standalone dashboard at
<http://localhost:8050> on the same data (see [Dashboard](dashboard.md)).

### The Console's demo (1.17.4)

```bash
nomad demo --console ~/nomad-demo
```

writes a demo for the Console in `~/nomad-demo`: the demo cluster's data with
a cast of made-up people in it (`nomad_demo.db`), and the nomad.toml that
makes each what they are (`.config/nomad/nomad.toml`):

| Who | Is | Sees |
|---|---|---|
| bob | a student in CS 101 | his own jobs, some running now |
| eve | the Grace Lab's research assistant | its members one by one, as its PI does, but not Grace's own work |
| grace | the PI of the Grace Lab (`grace$`) | her lab's members one by one, their work, and what needs her attention: a job of charlie's that will never start, jobs out of memory, who holds the lab's data and where (nas-01, everyone's $HOME; nas-02, the lab's own) |
| henry | the instructor of CS 101 | his class |
| olive | an operator | everything; changes no settings |
| adam | an admin | everything, settings included |

Start the Console on it from the Console's `backend` folder, with the Python
the Console runs on (its venv's: `HOME=` also moves where Python looks for
packages installed with `pip install --user`):

```bash
HOME=~/nomad-demo NOMAD_CONSOLE_DEMO=1 /path/to/console/venv/bin/python -m uvicorn app.main:app --port 8000
```

The demo is "now" as it was made: its running jobs and its nodes' reading
are of that moment, and after a couple of hours the Console says the
cluster hasn't reported since. Make it again just before a session.

Its sign-in page then offers each of them, one click and no password: for
trying the Console, for tutorials and for screenshots. The sign-in picker
appears only with `NOMAD_CONSOLE_DEMO=1` and a database `nomad demo --console`
made (it says so in its `demo_info` table), so it can't open a real Console.
Running the command again replaces its own demo, never another database or
nomad.toml, and never in your home folder (whose nomad.toml nomad reads).
A hub never syncs a demo's marker, so a hub's database is never taken for a
demo.

## On a real machine

[Installation](installation.md) has the details; in short:

1. **A site** (a cluster's head node, an interactive server): install nomad,
   run the wizard, check, and collect once by hand.

    ```bash
    pip install nomad-hpc
    nomad init                 # writes ~/.config/nomad/nomad.toml
    nomad syscheck             # what this machine has, and what is missing
    nomad collect --once       # one round; each collector says what it read
    nomad collectors           # which collectors run here, and why
    ```

    Then from cron, every 5 minutes:

    ```
    */5 * * * * /path/to/nomad collect --once
    ```

2. **The hub**: list the sites under `[hub]` in its nomad.toml and run
   `nomad sync`; `nomad sync status` says how each site's last syncs went
   ([Setup Wizard: multi-site](setup-wizard.md#multi-site-setup)).

3. **The Console**: run it on the hub, on `127.0.0.1:8000`, and reach it
   through an SSH tunnel or a reverse proxy with HTTPS
   ([Console access](console.md)). Who sees what is set in nomad.toml
   ([Configuration](config.md#console-access)).

## First commands on a real site

```bash
nomad status                       # disks, queues, I/O now
nomad collectors                   # what runs here, and how its runs went
nomad facts sites                  # each site, as the Console's front page has it
nomad edu me                       # your own jobs: scores, trend, what to change
nomad edu explain JOBID            # one job, in plain language
nomad size job.sh                  # what a job script should ask for
nomad insights brief               # what needs attention
nomad alerts                       # the alerts kept
```

## Next steps

- [Configuration](config.md): collectors, alerts, and who sees what in the Console
- [Collectors](collectors.md): what each one reads and where it belongs
- [Alerts](alerts.md): what is alerted on, and how it is sent
- [CLI Reference](cli.md): every command
