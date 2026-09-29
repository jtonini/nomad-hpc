# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""NØMAÐ configuration handling.

Every part of NØMAÐ that reads nomad.toml finds and parses it here, so the
CLI, the dashboard and the Console always read the same file the same way.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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

def read_toml(path: Path | str) -> dict:
    """Parse one TOML file. Raises if it is missing or cannot be parsed.

    The parser is tomllib (standard library from Python 3.11). On Python 3.10,
    or for a file that only the older `toml` package accepts, `toml` is used
    instead, so a config that worked before keeps working; the second case is
    logged, since the file should be fixed.
    """
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    try:
        import tomllib
    except ModuleNotFoundError:          # Python 3.10
        tomllib = None
    first_error = None
    if tomllib is not None:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            first_error = exc
    try:
        import toml
    except ModuleNotFoundError:
        if first_error is not None:
            raise first_error
        raise
    try:
        data = toml.loads(text)
    except Exception:
        if first_error is not None:
            raise first_error
        raise
    if first_error is not None:
        logger.warning("%s is not valid TOML (%s); read it with the older toml "
                       "parser instead. Please correct the file.", path, first_error)
    return data

def load_config(path: Path | None = None) -> dict:
    """
    Load NØMAÐ configuration as a dict.

    Resolution order:
      1. Explicit path argument
      2. ~/.config/nomad/nomad.toml
      3. /etc/nomad/nomad.toml
      4. packaged default (nomad/config/default.toml)

    Returns an empty dict if no config is found and the packaged default
    can't be read — never raises, so callers can rely on it as a soft
    accessor for site policy. A file that exists but cannot be read is
    logged and skipped.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    candidates.extend(DEFAULT_CONFIG_PATHS)
    candidates.append(get_default_config_path())

    for p in candidates:
        try:
            if p.exists():
                return read_toml(p)
        except Exception as exc:
            logger.warning("Skipping config %s: %s", p, exc)
            continue
    return {}

def support_settings(config: dict) -> dict:
    """Where people's questions go: [support], with the older names in
    [issue_reporting] (support_email, institution_name) as fallbacks.

    Nothing has a built-in default: a default address would send every other
    site's questions to whoever wrote it.
    """
    support = (config or {}).get('support') or {}
    legacy = (config or {}).get('issue_reporting') or {}
    return {
        'email': support.get('email') or legacy.get('support_email') or None,
        'institution': support.get('institution') or legacy.get('institution_name') or None,
        'user_email_domain': support.get('user_email_domain') or None,
    }

def read_secret(section: dict, key: str) -> str:
    """A secret from a config section, e.g. read_secret(cfg, 'github_token').

    `<key>_file` -- a file holding the secret, readable only by its owner --
    wins over `<key>` written into the TOML itself. Returns "" when neither is
    set or the file cannot be read (which is logged).
    """
    file_setting = (section or {}).get(f'{key}_file')
    if file_setting:
        try:
            return Path(file_setting).expanduser().read_text(encoding='utf-8').strip()
        except OSError as exc:
            logger.warning("Cannot read %s_file %s: %s", key, file_setting, exc)
            return ''
    return str((section or {}).get(key) or '')

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
