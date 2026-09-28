# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Cloud CLI Integration

Adds cloud-related subcommands to the NØMAÐ CLI:

    nomad collect cloud          Run all enabled cloud collectors once
    nomad collect cloud aws      Run only the AWS collector
    nomad cloud status           Check cloud collector connectivity
    nomad cloud instances        List discovered cloud instances

These functions are called from cli.py; this module handles
argument parsing and output formatting.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def cloud_collect(config: dict[str, Any], provider: str | None = None) -> int:
    """
    Run cloud collectors and store results.

    Args:
        config: Full NØMAÐ config dict (from nomad.toml).
        provider: Specific provider to run ("aws", "azure", "gcp"),
                  or None to run all enabled providers.

    Returns:
        Exit code (0 = success, 1 = partial failure, 2 = total failure).
    """
    cloud_cfg = config.get("collectors", {}).get("cloud", {})
    if not cloud_cfg:
        print("No cloud collectors configured in nomad.toml.")
        print("See: nomad/collectors/cloud/config/cloud_collectors.toml.example")
        return 2

    providers_to_run = []

    if provider:
        if provider not in cloud_cfg:
            print(f"Provider '{provider}' not found in config.")
            return 2
        providers_to_run = [(provider, cloud_cfg[provider])]
    else:
        providers_to_run = [
            (name, cfg)
            for name, cfg in cloud_cfg.items()
            if isinstance(cfg, dict) and cfg.get("enabled", False)
        ]

    if not providers_to_run:
        print("No cloud collectors enabled. Enable with: enabled = true")
        return 2

    results = []
    for name, pcfg in providers_to_run:
        print(f"Collecting from {name}... ", end="", flush=True)

        try:
            collector = _get_collector(name, pcfg)
            result = collector.run()
            results.append((name, result))

            if result.success:
                print(
                    f"OK ({result.records_collected} metrics "
                    f"in {result.duration_seconds:.1f}s)"
                )
            else:
                print(f"FAILED: {result.error_message}")

        except ImportError as exc:
            print(f"MISSING DEPENDENCY: {exc}")
            results.append((name, None))
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append((name, None))

    # Summarize
    successes = sum(
        1 for _, r in results if r is not None and r.success
    )
    total = len(results)

    if successes == total:
        return 0
    elif successes > 0:
        return 1
    else:
        return 2


def cloud_status(config: dict[str, Any]) -> int:
    """
    Check connectivity and authentication for all configured cloud providers.

    Prints a status table showing:
        - Provider name
        - Authentication status
        - Number of discoverable instances
        - Last collection timestamp
    """
    cloud_cfg = config.get("collectors", {}).get("cloud", {})
    if not cloud_cfg:
        print("No cloud collectors configured.")
        return 1

    print(f"{'Provider':<12} {'Enabled':<10} {'Auth':<12} {'Instances':<12}")
    print("-" * 46)

    for name, pcfg in cloud_cfg.items():
        if not isinstance(pcfg, dict):
            continue

        enabled = pcfg.get("enabled", False)
        enabled_str = "yes" if enabled else "no"

        if not enabled:
            print(f"{name:<12} {enabled_str:<10} {'—':<12} {'—':<12}")
            continue

        try:
            collector = _get_collector(name, pcfg)
            collector._ensure_authenticated()
            instances = collector._list_instances()
            print(
                f"{name:<12} {enabled_str:<10} "
                f"{'OK':<12} {len(instances):<12}"
            )
        except ImportError:
            print(
                f"{name:<12} {enabled_str:<10} "
                f"{'no SDK':<12} {'—':<12}"
            )
        except Exception as exc:
            err = str(exc)[:30]
            print(
                f"{name:<12} {enabled_str:<10} "
                f"{'FAIL':<12} {err}"
            )

    return 0


def cloud_instances(config: dict[str, Any], provider: str | None = None) -> int:
    """
    List all discoverable cloud instances across providers.
    """
    cloud_cfg = config.get("collectors", {}).get("cloud", {})

    providers = (
        [(provider, cloud_cfg[provider])]
        if provider and provider in cloud_cfg
        else [
            (n, c) for n, c in cloud_cfg.items()
            if isinstance(c, dict) and c.get("enabled", False)
        ]
    )

    for name, pcfg in providers:
        try:
            collector = _get_collector(name, pcfg)
            collector._ensure_authenticated()
            instances = collector._list_instances()

            print(f"\n{name.upper()} — {len(instances)} instance(s)")
            print(f"{'Name':<30} {'Type':<16} {'AZ':<16} {'State':<10}")
            print("-" * 72)

            for inst in instances:
                print(
                    f"{inst.get('name', inst['instance_id']):<30} "
                    f"{inst.get('instance_type', '—'):<16} "
                    f"{inst.get('availability_zone', '—'):<16} "
                    f"{inst.get('state', '—'):<10}"
                )

        except Exception as exc:
            print(f"\n{name.upper()} — ERROR: {exc}")

    return 0


def _get_collector(name: str, config: dict[str, Any]):
    """
    Instantiate the appropriate cloud collector by provider name.
    """
    if name == "aws":
        from nomad.collectors.cloud.aws import AWSCollector
        return AWSCollector(config, db_path=config.get("db_path", "nomad.db"))
    elif name == "azure":
        raise ImportError(
            "Azure collector not yet implemented. "
            "Planned for a future release."
        )
    elif name == "gcp":
        raise ImportError(
            "GCP collector not yet implemented. "
            "Planned for a future release."
        )
    else:
        raise ValueError(f"Unknown cloud provider: {name}")
