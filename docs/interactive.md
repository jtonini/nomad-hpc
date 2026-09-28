# Interactive Session Monitoring

Monitor RStudio and Jupyter sessions across your cluster.

## Overview

NØMAÐ tracks interactive computing sessions to identify:
- Idle sessions consuming resources
- Memory-heavy notebooks
- Stale sessions (no activity for days)
- Resource hogs

## Commands
```bash
# Full report
nomad report-interactive

# Alerts only (idle/stale sessions)
nomad report-interactive --quiet

# JSON output
nomad report-interactive --json
```

## Report Output
```
Interactive Sessions Report
═══════════════════════════════════════════════════════════

RStudio Sessions: 12 active
  node01: 3 sessions (2 idle > 1hr)
  node02: 5 sessions
  node04: 4 sessions (1 idle > 4hr)

Jupyter Sessions: 8 active
  node01: 2 sessions
  node03: 6 sessions (3 stale > 24hr)

Recommendations:
  • 5 sessions idle > 1 hour — consider auto-timeout
  • 3 notebooks stale > 24 hours — notify users
```

## JupyterHub Integration

NØMAÐ can integrate with JupyterHub's idle-culler:
```toml
[interactive.jupyter]
hub_api_url = "http://jupyterhub:8081/hub/api"
api_token = "..."
idle_timeout_minutes = 60
```

## Dashboard

The Interactive tab in the dashboard shows live session status with:
- Per-node session counts
- Idle time indicators
- Memory usage per session
- One-click idle session alerts
