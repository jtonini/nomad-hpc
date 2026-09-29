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
