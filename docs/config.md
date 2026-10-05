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
interval = 60  # seconds, for `nomad collect` without --once

# Each collector is turned on or off in its own table (see Collectors).
[collectors.nfs]
enabled = false

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

[mail]
host = "smtp.example.edu"
from = "hpc@example.edu"

[support]
email = "hpc-support@example.edu"
institution = "Example University"

[alerts.email]
enabled = true
recipients = ["admin@example.edu"]

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

## Console access

Who may see what in the Console is set in `nomad.toml` on the machine that
runs it:

```toml
[console.roles]
admin = ["jtonini"]            # everything, settings included
operator = []                  # everything, no settings

[console.labs]
group_pattern = "{netid}$"     # the group a faculty member leads, by its name
leads = { NETID = ["chemlab$"] }   # exceptions and extra groups
```

- **Viewer:** anyone the file does not name who signs in. They see their own
  work, and the aggregates (queues, capacity, storage) everyone sees.
- **PI:** a viewer who leads a lab also sees its members, one by one. They
  lead the group `group_pattern` names for them, when that group exists in the
  groups collector's data, and any group listed for them in `leads`.
  `group_pattern` is empty by default: a site that has not said how its groups
  work gets no lab view rather than a wrong one.
- **Operator:** sees everything, changes no settings.
- **Admin:** everything, settings included.

NetIDs are compared without regard to case. For anyone named here the file
wins over the Console's own records (`users.json`), which keep only what is
private or automatic: the break-glass password, and the record made at
someone's first login. A setting that is wrong is reported and left out,
never guessed at.

`nomad console roles` shows what the file grants, and anything wrong in it;
`nomad console roles NETID` shows what that person would see, with each lab's
size (`--db` for the database holding group membership, such as the hub's
combined database; `--mask` for counts only).

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NOMAD_CONFIG` | Config file path |
| `NOMAD_DB` | Database path |
| `NOMAD_LOG_LEVEL` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `NOMAD_CONSOLE_HOST` | The machine serving the Console, for `nomad console` |
| `NOMAD_LOGIN_HOST` | This cluster's name as people's computers reach it, for `nomad console` |

## Collectors

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed collector documentation.
