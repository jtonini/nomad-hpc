# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ System Installation

Handles system-wide installation for HPC environments:
- Create nomad user/group
- Install config to /etc/nomad/
- Create data dirs in /var/lib/nomad/
- Install SLURM prolog hook
- Create systemd service
- Setup logrotate
"""

import grp
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path


def get_nomad_bin_path() -> str:
    """Find the installed nomad binary path."""
    # Check if nomad is in PATH
    result = subprocess.run(['which', 'nomad'], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()

    # Check common locations
    candidates = [
        Path('/usr/local/bin/nomad'),
        Path('/usr/bin/nomad'),
        Path.home() / '.local' / 'bin' / 'nomad',
        Path(sys.prefix) / 'bin' / 'nomad',
    ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    # Fallback to python -m
    return f"{sys.executable} -m nomad.cli"

# Installation paths
SYSTEM_CONFIG_DIR = Path("/etc/nomad")
SYSTEM_DATA_DIR = Path("/var/lib/nomad")
SYSTEM_LOG_DIR = Path("/var/log/nomad")
SLURM_PROLOG_DIR = Path("/etc/slurm/prolog.d")
SYSTEMD_DIR = Path("/etc/systemd/system")
LOGROTATE_DIR = Path("/etc/logrotate.d")

# Service user
NOMAD_USER = "nomad"
NOMAD_GROUP = "nomad"




def create_bin_symlink(force: bool = False) -> Path | None:
    """Create /usr/local/bin/nomad symlink to actual binary."""
    target = Path('/usr/local/bin/nomad')

    # Find actual nomad location
    actual = None
    candidates = [
        Path('/opt/anaconda/bin/nomad'),
        Path('/opt/conda/bin/nomad'),
        Path('/usr/local/anaconda/bin/nomad'),
    ]

    # Also check sys.prefix (where pip installed)
    import sys
    candidates.insert(0, Path(sys.prefix) / 'bin' / 'nomad')

    for c in candidates:
        if c.exists():
            actual = c
            break

    if not actual:
        return None

    if target.exists() or target.is_symlink():
        if force:
            target.unlink()
        else:
            return None

    target.symlink_to(actual)
    return target


SYSTEMD_SERVICE = """[Unit]
Description=NØMAÐ HPC Monitoring Daemon
Documentation=https://github.com/jtonini/nomad
After=network.target slurmd.service

[Service]
Type=simple
User={user}
Group={group}
ExecStart={nomad_bin} collect --interval 60
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nomad

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={data_dir} {log_dir}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

SYSTEMD_LEARNING_SERVICE = """[Unit]
Description=NØMAÐ Continuous Learning Daemon
Documentation=https://github.com/jtonini/nomad
After=network.target nomad.service

[Service]
Type=simple
User={user}
Group={group}
ExecStart={nomad_bin} learn --daemon --strategy count --threshold 100
Restart=on-failure
RestartSec=60

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nomad-learn

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={data_dir} {log_dir}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

SYSTEMD_TIMER = """[Unit]
Description=NØMAÐ ML Training Timer
Documentation=https://github.com/jtonini/nomad

[Timer]
OnBootSec=15min
OnUnitActiveSec=6h
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
"""

SYSTEMD_TRAIN_SERVICE = """[Unit]
Description=NØMAÐ ML Training (triggered by timer)
Documentation=https://github.com/jtonini/nomad

[Service]
Type=oneshot
User={user}
Group={group}
ExecStart={nomad_bin} learn --force --epochs 100
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nomad-train
"""

LOGROTATE_CONFIG = """/var/log/nomad/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 {user} {group}
    sharedscripts
    postrotate
        systemctl reload nomad.service >/dev/null 2>&1 || true
    endscript
}
"""

SLURM_PROLOG_WRAPPER = """#!/bin/bash
# NØMAÐ SLURM Prolog Hook
# Scores jobs at submission time for risk assessment

# Only run if nomad is installed
if command -v nomad &> /dev/null; then
    /usr/bin/python3 /etc/nomad/prolog.py 2>/dev/null || true
fi

exit 0
"""


def check_root():
    """Check if running as root."""
    return os.geteuid() == 0


def user_exists(username: str) -> bool:
    """Check if user exists."""
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def group_exists(groupname: str) -> bool:
    """Check if group exists."""
    try:
        grp.getgrnam(groupname)
        return True
    except KeyError:
        return False


def create_user_group(user: str = NOMAD_USER, group: str = NOMAD_GROUP) -> dict:
    """Create nomad user and group."""
    results = {'group_created': False, 'user_created': False}

    # Create group
    if not group_exists(group):
        subprocess.run(['groupadd', '--system', group], check=True)
        results['group_created'] = True

    # Create user
    if not user_exists(user):
        subprocess.run([
            'useradd', '--system',
            '--gid', group,
            '--home-dir', str(SYSTEM_DATA_DIR),
            '--shell', '/sbin/nologin',
            '--comment', 'NOMAD HPC Monitor',
            user
        ], check=True)
        results['user_created'] = True

    return results


def create_directories(user: str = NOMAD_USER, group: str = NOMAD_GROUP) -> list:
    """Create system directories with proper permissions."""
    created = []

    dirs = [
        (SYSTEM_CONFIG_DIR, 0o755),
        (SYSTEM_DATA_DIR, 0o750),
        (SYSTEM_DATA_DIR / 'models', 0o750),
        (SYSTEM_LOG_DIR, 0o750),
    ]

    for dir_path, mode in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)
        dir_path.chmod(mode)
        if user_exists(user) and group_exists(group):
            shutil.chown(dir_path, user=user, group=group)
        created.append(str(dir_path))

    return created


def install_config(source_config: Path | None = None, force: bool = False) -> Path:
    """Install configuration file."""
    config_file = SYSTEM_CONFIG_DIR / 'nomad.toml'

    if config_file.exists() and not force:
        return config_file

    # Find source config
    if source_config and source_config.exists():
        shutil.copy(source_config, config_file)
    else:
        # Try package default
        try:
            from nomad.config import get_default_config_path
            default = get_default_config_path()
            if default.exists():
                shutil.copy(default, config_file)
        except ImportError:
            pass

    # Update paths in config
    if config_file.exists():
        content = config_file.read_text()
        content = content.replace(
            'data_dir = "~/.local/share/nomad"',
            f'data_dir = "{SYSTEM_DATA_DIR}"'
        )
        config_file.write_text(content)
        config_file.chmod(0o644)

    return config_file


def install_prolog_hook(force: bool = False) -> Path | None:
    """Install SLURM prolog hook."""
    # Check if SLURM is available
    if not SLURM_PROLOG_DIR.parent.exists():
        return None

    SLURM_PROLOG_DIR.mkdir(parents=True, exist_ok=True)

    # Copy prolog.py
    prolog_dest = SYSTEM_CONFIG_DIR / 'prolog.py'
    try:
        from nomad.hooks import prolog
        prolog_src = Path(prolog.__file__)
        if prolog_src.exists():
            shutil.copy(prolog_src, prolog_dest)
            prolog_dest.chmod(0o755)
    except ImportError:
        pass

    # Create wrapper script
    wrapper = SLURM_PROLOG_DIR / 'nomad_score.sh'
    if not wrapper.exists() or force:
        wrapper.write_text(SLURM_PROLOG_WRAPPER)
        wrapper.chmod(0o755)
        return wrapper

    return None


def install_systemd_services(
    user: str = NOMAD_USER,
    group: str = NOMAD_GROUP,
    force: bool = False
) -> list:
    """Install systemd service files."""
    installed = []
    nomad_bin = get_nomad_bin_path()

    services = [
        ('nomad.service', SYSTEMD_SERVICE),
        ('nomad-learn.service', SYSTEMD_LEARNING_SERVICE),
        ('nomad-train.service', SYSTEMD_TRAIN_SERVICE),
        ('nomad-train.timer', SYSTEMD_TIMER),
    ]

    for name, template in services:
        service_file = SYSTEMD_DIR / name
        if not service_file.exists() or force:
            content = template.format(
                user=user,
                group=group,
                data_dir=SYSTEM_DATA_DIR,
                log_dir=SYSTEM_LOG_DIR,
                nomad_bin=nomad_bin
            )
            service_file.write_text(content)
            service_file.chmod(0o644)
            installed.append(name)

    # Reload systemd
    if installed:
        subprocess.run(['systemctl', 'daemon-reload'], check=False)

    return installed


def install_logrotate(user: str = NOMAD_USER, group: str = NOMAD_GROUP) -> Path | None:
    """Install logrotate configuration."""
    if not LOGROTATE_DIR.exists():
        return None

    logrotate_file = LOGROTATE_DIR / 'nomad'
    content = LOGROTATE_CONFIG.format(user=user, group=group)
    logrotate_file.write_text(content)
    logrotate_file.chmod(0o644)

    return logrotate_file


def add_slurm_group(user: str = NOMAD_USER) -> bool:
    """Add nomad user to slurm group for access to SLURM commands."""
    try:
        # Check if slurm group exists
        grp.getgrnam('slurm')
        subprocess.run(['usermod', '-aG', 'slurm', user], check=True)
        return True
    except (KeyError, subprocess.CalledProcessError):
        return False


def full_system_install(force: bool = False, verbose: bool = True) -> dict:
    """
    Complete system installation.
    
    Returns dict with installation results.
    """
    results = {
        'success': False,
        'user_created': False,
        'group_created': False,
        'directories': [],
        'config': None,
        'prolog': None,
        'services': [],
        'logrotate': None,
        'errors': []
    }

    if not check_root():
        results['errors'].append("Must run as root (use sudo)")
        return results

    try:
        # 1. Create user/group
        if verbose:
            print("Creating nomad user and group...")
        ug = create_user_group()
        results['user_created'] = ug['user_created']
        results['group_created'] = ug['group_created']

        # 2. Create directories
        if verbose:
            print("Creating directories...")
        results['directories'] = create_directories()

        # 3. Install config
        if verbose:
            print("Installing configuration...")
        results['config'] = str(install_config(force=force))

        # 4. Install prolog hook
        if verbose:
            print("Installing SLURM prolog hook...")
        prolog = install_prolog_hook(force=force)
        results['prolog'] = str(prolog) if prolog else None

        # 5. Install systemd services
        if verbose:
            print("Installing systemd services...")
        results['services'] = install_systemd_services(force=force)

        # 6. Install logrotate
        if verbose:
            print("Installing logrotate config...")
        logrotate = install_logrotate()
        results['logrotate'] = str(logrotate) if logrotate else None

        # 7. Add to slurm group
        if verbose:
            print("Adding nomad user to slurm group...")
        add_slurm_group()

        # 8. Create /usr/local/bin symlink if needed
        if verbose:
            print("Creating /usr/local/bin symlink...")
        symlink = create_bin_symlink(force=force)
        if symlink:
            results['symlink'] = str(symlink)

        results['success'] = True

    except Exception as e:
        results['errors'].append(str(e))

    return results


def print_post_install_instructions():
    """Print post-installation instructions."""
    nomad_bin = get_nomad_bin_path()
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║                    NØMAÐ System Installation                     ║
╠══════════════════════════════════════════════════════════════════╣
║  Binary:        {nomad_bin:<47} ║
║  Configuration: /etc/nomad/nomad.toml                          ║
║  Data:          /var/lib/nomad/                                 ║
║  Logs:          /var/log/nomad/                                 ║
╚══════════════════════════════════════════════════════════════════╝

Next steps:

  1. Edit configuration:
     sudo nano /etc/nomad/nomad.toml

  2. Start the collection daemon:
     sudo systemctl enable --now nomad.service

  3. (Optional) Enable continuous learning:
     sudo systemctl enable --now nomad-train.timer
     
     OR for daemon mode:
     sudo systemctl enable --now nomad-learn.service

  4. (Optional) Enable SLURM prolog hook:
     Add to /etc/slurm/slurm.conf:
       Prolog=/etc/slurm/prolog.d/nomad_score.sh
     Then: sudo systemctl restart slurmctld

  5. Check status:
     sudo systemctl status nomad
     sudo journalctl -u nomad -f

  6. View dashboard (from head node):
     nomad dashboard --host 0.0.0.0

For module-based environments (Lmod):
  Create module file at /opt/apps/modulefiles/nomad/0.3.0
  See: https://github.com/jtonini/nomad/docs/lmod.md
""")
