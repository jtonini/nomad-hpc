# Configuration

NØMAÐ uses TOML configuration files.

## Configuration Locations

| Install Type | Path |
|--------------|------|
| User | `~/.config/nomad/nomad.toml` |
| System | `/etc/nomad/nomad.toml` |

## Example Configuration
```toml
# nomad.toml

[general]
cluster_name = "spydur"
data_dir = "/var/lib/nomad"
log_level = "INFO"

[database]
path = "/var/lib/nomad/nomad.db"

[collectors]
enabled = ["disk", "iostat", "slurm", "gpu", "nfs"]
interval = 60  # seconds

[collectors.disk]
filesystems = ["/", "/home", "/scratch"]
quota_enabled = true

[collectors.slurm]
partitions = ["compute", "gpu", "highmem"]

[dashboard]
host = "127.0.0.1"
port = 8050

[alerts]
enabled = true

[alerts.email]
enabled = true
smtp_host = "smtp.example.edu"
from_addr = "nomad@example.edu"
to_addrs = ["admin@example.edu"]

[alerts.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/..."

[alerts.thresholds]
disk_warning = 85
disk_critical = 95
gpu_temp_warning = 80

[ml]
enabled = true
similarity_threshold = 0.7
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NOMAD_CONFIG` | Config file path |
| `NOMAD_DB` | Database path |
| `NOMAD_LOG_LEVEL` | Log level (DEBUG, INFO, WARNING, ERROR) |

## Collectors

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed collector documentation.
