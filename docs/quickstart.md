# Quick Start

## Try Without HPC
```bash
pip install nomad-hpc
nomad demo
```

This launches a dashboard at http://localhost:8050 with synthetic data.

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
| eve | a research assistant in the Grace Lab | her own work, as a lab member does |
| grace | the PI of the Grace Lab (`grace$`) | her lab's members one by one, and their work |
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

## Production Setup

### 1. Install
```bash
pip install nomad-hpc
```

### 2. Configure
```bash
nomad init
```

Follow the wizard to configure your cluster(s).

### 3. Collect Data
```bash
nomad collect
```

Leave running (or set up as systemd service).

### 4. View Dashboard
```bash
nomad dashboard
```

Open http://localhost:8050

## First Commands to Try
```bash
# System status
nomad status

# Disk usage trends
nomad disk /home

# Recent jobs
nomad jobs --user $USER

# Educational analytics (if you have job data)
nomad edu explain <job_id>
```

## Next Steps

- [Configuration](config.md) — Customize collectors and alerts
- [Dashboard](dashboard.md) — Navigate the web interface
- [Educational Analytics](edu.md) — Track proficiency development
- [CLI Reference](cli.md) — All available commands
