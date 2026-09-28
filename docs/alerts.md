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
[alerts.email]
enabled = true
smtp_host = "smtp.example.edu"
smtp_port = 587
from_addr = "nomad@example.edu"
to_addrs = ["admin@example.edu", "hpc-team@example.edu"]
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
