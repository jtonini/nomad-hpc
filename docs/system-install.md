# System Install

This guide covers deploying NØMAÐ system-wide for production HPC environments.

## Overview

System install differs from user install in several ways:

| Aspect | User Install | System Install |
|--------|--------------|----------------|
| Config location | `~/.config/nomad/` | `/etc/nomad/` |
| Data location | `~/.local/share/nomad/` | `/var/lib/nomad/` |
| Log location | `~/.local/share/nomad/logs/` | `/var/log/nomad/` |
| Permissions | User only | Controlled by file permissions |
| Service | Manual or user systemd | System systemd unit |
| Dashboard access | localhost only | Configurable (proxy recommended) |

## Installation

### Prerequisites
```bash
# Install as root or with sudo
sudo pip install nomad-hpc

# Or system-wide from source
sudo pip install /path/to/nomad/
```

### Initialize System Configuration
```bash
sudo nomad init --system
```

This creates:

| Path | Purpose | Permissions |
|------|---------|-------------|
| `/etc/nomad/` | Configuration directory | `root:root 755` |
| `/etc/nomad/nomad.toml` | Main configuration | `root:root 644` |
| `/var/lib/nomad/` | Data directory | `root:nomad 750` |
| `/var/lib/nomad/nomad.db` | SQLite database | `root:nomad 660` |
| `/var/log/nomad/` | Log directory | `root:nomad 750` |

### Required Permissions

**Running as root**: Full access to all paths.

**Running as service user** (recommended):
```bash
# Create nomad group
sudo groupadd nomad

# Create service user
sudo useradd -r -g nomad -d /var/lib/nomad -s /sbin/nologin nomad

# Set ownership
sudo chown -R nomad:nomad /var/lib/nomad
sudo chown -R nomad:nomad /var/log/nomad
sudo chown root:nomad /etc/nomad/nomad.toml
sudo chmod 640 /etc/nomad/nomad.toml
```

**Wheel group access** (for admin users):
```bash
# Allow wheel group to read config
sudo chown root:wheel /etc/nomad/nomad.toml
sudo chmod 640 /etc/nomad/nomad.toml

# Allow wheel group to read database
sudo chown nomad:wheel /var/lib/nomad/nomad.db
sudo chmod 660 /var/lib/nomad/nomad.db
```

## Files Modified/Accessed

### Configuration Files

| File | Read/Write | Purpose |
|------|------------|---------|
| `/etc/nomad/nomad.toml` | R | Main configuration |
| `/etc/nomad/clusters/*.toml` | R | Per-cluster configs (optional) |

### Data Files

| File | Read/Write | Purpose |
|------|------------|---------|
| `/var/lib/nomad/nomad.db` | RW | SQLite database |
| `/var/lib/nomad/models/` | RW | ML model files |
| `/var/lib/nomad/cache/` | RW | Temporary cache |

### Log Files

| File | Read/Write | Purpose |
|------|------------|---------|
| `/var/log/nomad/nomad.log` | W | Main application log |
| `/var/log/nomad/collector.log` | W | Collector logs |
| `/var/log/nomad/alerts.log` | W | Alert dispatch logs |

### System Files Read

| File | Purpose |
|------|---------|
| `/proc/*/io` | Per-process I/O stats |
| `/proc/meminfo` | Memory information |
| `/proc/loadavg` | System load |
| `/sys/class/thermal/` | Temperature sensors |

### External Commands Executed

| Command | Package | Purpose |
|---------|---------|---------|
| `iostat` | sysstat | Disk I/O metrics |
| `mpstat` | sysstat | CPU metrics |
| `vmstat` | procps | Memory/swap metrics |
| `df` | coreutils | Filesystem usage |
| `nvidia-smi` | nvidia-driver | GPU metrics (optional) |
| `nfsiostat` | nfs-utils | NFS metrics (optional) |
| `squeue` | slurm | Job queue (optional) |
| `sinfo` | slurm | Node info (optional) |
| `sacct` | slurm | Job accounting (optional) |

## Systemd Service

### Create Service Unit
```bash
sudo cat > /etc/systemd/system/nomad-collector.service << 'UNIT'
[Unit]
Description=NØMAÐ HPC Monitoring Collector
After=network.target slurmd.service

[Service]
Type=simple
User=nomad
Group=nomad
ExecStart=/usr/local/bin/nomad collect
Restart=always
RestartSec=30

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/nomad /var/log/nomad
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
```

### Enable and Start
```bash
sudo systemctl daemon-reload
sudo systemctl enable nomad-collector
sudo systemctl start nomad-collector
sudo systemctl status nomad-collector
```

### Dashboard Service (Optional)
```bash
sudo cat > /etc/systemd/system/nomad-dashboard.service << 'UNIT'
[Unit]
Description=NØMAÐ Dashboard
After=network.target nomad-collector.service

[Service]
Type=simple
User=nomad
Group=nomad
ExecStart=/usr/local/bin/nomad dashboard --host 127.0.0.1 --port 8050
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
```

## Web Dashboard Access

### Localhost Only (Default)

Dashboard binds to `127.0.0.1:8050` by default. Access via SSH tunnel:
```bash
# On your local machine
ssh -L 8050:localhost:8050 user@hpc-head-node

# Then open http://localhost:8050 in browser
```

### Reverse Proxy (Recommended for Remote Access)

Using Apache:
```apache
# /etc/httpd/conf.d/nomad.conf
<VirtualHost *:443>
    ServerName hpc.example.edu
    
    SSLEngine on
    SSLCertificateFile /etc/pki/tls/certs/server.crt
    SSLCertificateKeyFile /etc/pki/tls/private/server.key
    
    <Location /nomad>
        ProxyPass http://127.0.0.1:8050/
        ProxyPassReverse http://127.0.0.1:8050/
        
        # Require authentication
        AuthType Basic
        AuthName "NØMAÐ Dashboard"
        AuthUserFile /etc/httpd/.htpasswd
        Require valid-user
    </Location>
</VirtualHost>
```

Using nginx:
```nginx
# /etc/nginx/conf.d/nomad.conf
server {
    listen 443 ssl;
    server_name hpc.example.edu;
    
    ssl_certificate /etc/ssl/certs/server.crt;
    ssl_certificate_key /etc/ssl/private/server.key;
    
    location /nomad/ {
        proxy_pass http://127.0.0.1:8050/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        
        # WebSocket support (for live updates)
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Authentication
        auth_basic "NØMAÐ Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd;
    }
}
```

## SLURM Integration

### Prolog/Epilog Hooks

For per-job metrics collection:
```bash
# Copy hook scripts
sudo cp /path/to/nomad/scripts/prolog.sh /etc/slurm/prolog.d/nomad.sh
sudo cp /path/to/nomad/scripts/epilog.sh /etc/slurm/epilog.d/nomad.sh

# Make executable
sudo chmod +x /etc/slurm/prolog.d/nomad.sh
sudo chmod +x /etc/slurm/epilog.d/nomad.sh

# Restart SLURM controller
sudo systemctl restart slurmctld
```

### Prolog Script
```bash
#!/bin/bash
# /etc/slurm/prolog.d/nomad.sh
/usr/local/bin/nomad job-start $SLURM_JOB_ID 2>/dev/null || true
```

### Epilog Script
```bash
#!/bin/bash
# /etc/slurm/epilog.d/nomad.sh
/usr/local/bin/nomad job-end $SLURM_JOB_ID 2>/dev/null || true
```

## Multi-Cluster Setup

For monitoring multiple clusters from one head node:
```toml
# /etc/nomad/nomad.toml

[clusters.spydur]
name = "Spydur"
type = "slurm"
ssh_host = "spydur-head"
partitions = ["compute", "gpu", "highmem"]

[clusters.arachne]
name = "Arachne"
type = "slurm"
ssh_host = "arachne-head"
partitions = ["standard", "large"]
```

## Security Considerations

### Database Access

The SQLite database contains job metadata including usernames. Restrict access:
```bash
# Only nomad user and wheel group can read
sudo chmod 660 /var/lib/nomad/nomad.db
sudo chown nomad:wheel /var/lib/nomad/nomad.db
```

### Configuration Secrets

If using Slack/email alerts, config contains credentials:
```bash
# Restrict config file
sudo chmod 640 /etc/nomad/nomad.toml
sudo chown root:nomad /etc/nomad/nomad.toml
```

### Dashboard Authentication

Always use authentication for remote dashboard access. Never expose port 8050 directly to the network.

## Troubleshooting

### Permission Denied
```
PermissionError: [Errno 13] Permission denied: '/var/lib/nomad/nomad.db'
```

**Solution**: Check ownership and permissions:
```bash
sudo chown nomad:nomad /var/lib/nomad/nomad.db
sudo chmod 660 /var/lib/nomad/nomad.db
```

### Collector Not Starting

Check systemd logs:
```bash
sudo journalctl -u nomad-collector -f
```

### SLURM Commands Failing

Ensure the nomad user can run SLURM commands:
```bash
sudo -u nomad squeue
```

May need to add nomad user to appropriate groups or configure SLURM ACLs.
