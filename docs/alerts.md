# Alerts

NØMAÐ supports both threshold-based and predictive alerts.

## Alert Types

### Threshold Alerts

Trigger when metrics exceed configured limits:
- Disk usage > 95%
- GPU temperature > 85°C
- Memory pressure > 90%

### Predictive Alerts

Trigger when trends indicate future problems:
- Disk fill rate predicts full in < 24 hours
- Memory pressure accelerating
- I/O wait increasing

## Backends

### Email
```toml
[mail]                       # shared by everything that sends mail
host = "smtp.example.edu"
port = 587
starttls = "required"        # required | if-offered | off
from = "hpc@example.edu"     # a sender your mail server accepts

[alerts.email]
enabled = true
recipients = ["admin@example.edu", "hpc-team@example.edu"]
```

Alert email goes through `[mail]`; see `nomad.toml.example` for its other
options (a certificate name, a missing intermediate, a login). Any of `[mail]`'s
keys set in `[alerts.email]` itself override it, for alerts that must use a
different server. `nomad test-alerts --email` checks the connection.

Each message names its cluster -- the name `[clusters]` gives, else the host
name -- in the subject with the problem itself, so an inbox shared by several
sites reads at a glance:

```
[WARNING] NØMAÐ spydur: Disk /scratch at 85.0% (threshold: 80%)
```

### Slack
```toml
[alerts.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/T00/B00/xxx"
channel = "#hpc-alerts"
```

### Webhook
```toml
[alerts.webhook]
enabled = true
url = "https://your-service.example.edu/alerts"
headers = { Authorization = "Bearer xxx" }
```

## CLI
```bash
# View recent alerts
nomad alerts

# Unresolved only
nomad alerts --unresolved

# Test alert backends
nomad alerts test
```

## Cooldowns

To prevent alert floods:
```toml
[alerts]
cooldown_minutes = 30  # Same alert won't repeat for 30 min
```

"The same alert" is the same source, host and severity: `/scratch` on one
host at *warning*. The cooldown is read from the alerts already stored in the
database, so it holds across `nomad collect --once` runs from cron -- each run
is a new process -- and a condition that persists is stored and sent once per
window, not on every run. The default is 15 minutes; with collection every
few minutes, a longer window (say `360`, six hours) keeps a full disk from
sending dozens of emails a day. `nomad test-alerts` always sends its test.
