# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Validate that the example and default TOML configs actually parse.

These files are what users copy as a starting point; if either is
malformed, the user's first NOMAD experience is a stack trace. This
test locks in the invariant that they parse cleanly.
"""

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib is stdlib only on 3.11+")
def test_nomad_toml_example_parses():
    """nomad.toml.example must be valid TOML."""
    import tomllib

    example = REPO_ROOT / "nomad.toml.example"
    assert example.exists(), f"missing: {example}"

    with example.open("rb") as f:
        config = tomllib.load(f)

    # Sanity: expected top-level sections present
    assert "collectors" in config, "[collectors] section missing"
    assert "alerts" in config, "[alerts] section missing"


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib is stdlib only on 3.11+")
def test_default_toml_parses():
    """nomad/config/default.toml must also be valid TOML."""
    import tomllib

    default = REPO_ROOT / "nomad" / "config" / "default.toml"
    assert default.exists(), f"missing: {default}"

    with default.open("rb") as f:
        config = tomllib.load(f)

    assert isinstance(config, dict)
    assert len(config) > 0


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib is stdlib only on 3.11+")
def test_example_matches_default_top_level_sections():
    """The example and default should share their top-level section names.

    If one of them gains a section the other lacks, that's a documentation
    drift bug -- users following the example wouldn't discover the new
    section, or vice versa.
    """
    import tomllib

    with (REPO_ROOT / "nomad.toml.example").open("rb") as f:
        example_cfg = tomllib.load(f)
    with (REPO_ROOT / "nomad" / "config" / "default.toml").open("rb") as f:
        default_cfg = tomllib.load(f)

    example_sections = set(example_cfg.keys())
    default_sections = set(default_cfg.keys())

    # Both files should describe the same overall config surface.
    # Missing sections in either direction signal drift.
    only_in_example = example_sections - default_sections
    only_in_default = default_sections - example_sections

    # Allow some sections only in example (extra documentation is OK),
    # but flag sections in default not documented in example.
    assert not only_in_default, (
        f"Sections present in default.toml but not nomad.toml.example: "
        f"{only_in_default}. Update nomad.toml.example."
    )


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib is stdlib only on 3.11+")
def test_alerts_subtables_match_dispatcher_schema():
    """The dispatcher reads config['alerts']['email'/'slack'/'webhook'].

    The example must document those subtables, not the legacy flat keys
    (email_enabled = true under [alerts]) that the dispatcher never reads.
    Defaults must be enabled=false so admins opt in deliberately rather
    than getting silent SMTP failures against placeholder values.
    """
    import tomllib

    with (REPO_ROOT / "nomad.toml.example").open("rb") as f:
        cfg = tomllib.load(f)

    alerts = cfg.get("alerts", {})

    # Subtables present
    for backend in ("email", "slack", "webhook"):
        assert backend in alerts, f"missing [alerts.{backend}] subtable"
        assert "enabled" in alerts[backend], (
            f"[alerts.{backend}] missing 'enabled' key"
        )

    # Legacy flat keys absent (dispatcher would silently ignore them)
    for legacy in ("email_enabled", "slack_enabled", "webhook_enabled"):
        assert legacy not in alerts, (
            f"[alerts] has legacy flat key '{legacy}' that the dispatcher "
            f"won't read; use [alerts.{legacy.split('_')[0]}] instead."
        )

    # Sensible defaults: nothing dispatching by default
    for backend in ("email", "slack", "webhook"):
        assert alerts[backend]["enabled"] is False, (
            f"[alerts.{backend}] should default enabled=false to prevent "
            f"silent failures on first install with placeholder values."
        )


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="tomllib is stdlib only on 3.11+")
def test_dashboard_binds_to_loopback_by_default():
    """The example must not bind the dashboard to all interfaces by default.

    0.0.0.0 exposes the dashboard to anyone on the network. Sites that
    want centralized monitoring (like mingus) can opt in by setting
    host = "0.0.0.0", but new installs should be safe by default.
    """
    import tomllib

    with (REPO_ROOT / "nomad.toml.example").open("rb") as f:
        cfg = tomllib.load(f)

    host = cfg.get("dashboard", {}).get("host", "0.0.0.0")
    assert host in ("127.0.0.1", "localhost", "::1"), (
        f"dashboard.host = '{host}' exposes the dashboard to all network "
        f"interfaces. Default to a loopback address; document opt-in."
    )
