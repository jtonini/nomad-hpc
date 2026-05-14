# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""NOMADE configuration handling."""

from pathlib import Path

DEFAULT_CONFIG_PATHS = [
    Path.home() / '.config' / 'nomad' / 'nomad.toml',
    Path('/etc/nomad/nomad.toml'),
]

def find_config() -> Path | None:
    """Find the first existing config file."""
    for path in DEFAULT_CONFIG_PATHS:
        if path.exists():
            return path
    return None

def get_default_config_path() -> Path:
    """Get path to packaged default config."""
    return Path(__file__).parent / 'default.toml'

def load_config(path: Path | None = None) -> dict:
    """
    Load NOMAD configuration as a dict.

    Resolution order:
      1. Explicit path argument
      2. ~/.config/nomad/nomad.toml
      3. /etc/nomad/nomad.toml
      4. packaged default (nomad/config/default.toml)

    Returns an empty dict if no config is found and the packaged default
    can't be read — never raises, so callers can rely on it as a soft
    accessor for site policy.
    """
    import tomllib  # 3.11+, stdlib
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    candidates.extend(DEFAULT_CONFIG_PATHS)
    candidates.append(get_default_config_path())

    for p in candidates:
        try:
            if p.exists():
                with p.open('rb') as f:
                    return tomllib.load(f)
        except Exception:
            continue
    return {}

def resolve_cluster_name(config: dict) -> str:
    """Resolve cluster name from config, trying all known paths.

    Resolution order:
      1. config['clusters'] -- wizard-generated format (first cluster name)
      2. config['cluster_name'] -- legacy top-level key
      3. config['cluster']['name'] -- another legacy path
      4. hostname via socket.gethostname()
      5. 'default' as ultimate fallback
    """
    import socket

    # 1. Wizard format: [clusters.<id>] name = "..."
    clusters = config.get('clusters', {})
    if clusters:
        first_id = next(iter(clusters))
        name = clusters[first_id].get('name', first_id)
        if name:
            return name

    # 2. Legacy top-level key
    name = config.get('cluster_name')
    if name:
        return name

    # 3. Another legacy path
    name = config.get('cluster', {}).get('name')
    if name:
        return name

    # 4. Hostname
    try:
        hostname = socket.gethostname().split('.')[0]
        if hostname:
            return hostname
    except Exception:
        pass

    return 'default'


def resolve_all_cluster_names(config: dict) -> list[str]:
    """Return all cluster names from config.

    For multi-cluster setups returns all names from [clusters.*].
    For single-cluster legacy configs returns a one-element list.
    """
    clusters = config.get('clusters', {})
    if clusters:
        return [c.get('name', cid) for cid, c in clusters.items()]
    return [resolve_cluster_name(config)]
