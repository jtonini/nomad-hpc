"""Collect system information for issue reports.

Gathers NØMAÐ version, Python version, OS details, active collectors,
active alerts, cluster count, and database statistics to auto-populate
issue reports with diagnostic context.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class SystemInfo:
    """Container for auto-collected system information."""

    nomad_version: str = "unknown"
    python_version: str = ""
    os_info: str = ""
    active_collectors: list[str] = field(default_factory=list)
    active_alerts: list[str] = field(default_factory=list)
    alert_count: int = 0
    cluster_count: int = 0
    cluster_names: list[str] = field(default_factory=list)
    db_path: str = ""
    db_size_mb: float = 0.0
    record_count: int = 0
    uptime_days: int = 0
    source: str = "cli"
    institution: str = ""
    submitted_by: str = ""
    console_version: str = ""
    timestamp: str = ""

    def to_markdown(self) -> str:
        """Format as a Markdown block for GitHub issue body."""
        lines = [
            "### System Information",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| NØMAÐ version | {self.nomad_version} |",
        ]
        if self.console_version:
            lines.append(f"| Console version | {self.console_version} |")
        lines.extend([
            f"| Python | {self.python_version} |",
            f"| OS | {self.os_info} |",
        ])
        if self.active_collectors:
            lines.append(
                f"| Active collectors | {', '.join(self.active_collectors)} |"
            )
        if self.alert_count > 0:
            alert_str = f"{self.alert_count}"
            if self.active_alerts:
                alert_str += f" ({', '.join(self.active_alerts[:5])})"
            lines.append(f"| Active alerts | {alert_str} |")
        if self.cluster_count > 0:
            lines.append(f"| Clusters managed | {self.cluster_count} |")
        if self.db_size_mb > 0:
            lines.append(f"| Database size | {self.db_size_mb:.1f} MB |")
        lines.append(f"| Submitted from | {self.source} |")
        if self.institution:
            lines.append(f"| Institution | {self.institution} |")
        if self.submitted_by:
            lines.append(f"| Submitted by | {self.submitted_by} |")
        lines.append(f"| Timestamp | {self.timestamp} |")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "nomad_version": self.nomad_version,
            "console_version": self.console_version,
            "python_version": self.python_version,
            "os_info": self.os_info,
            "active_collectors": self.active_collectors,
            "active_alerts": self.active_alerts,
            "alert_count": self.alert_count,
            "cluster_count": self.cluster_count,
            "cluster_names": self.cluster_names,
            "db_path": self.db_path,
            "db_size_mb": self.db_size_mb,
            "record_count": self.record_count,
            "source": self.source,
            "institution": self.institution,
            "submitted_by": self.submitted_by,
            "timestamp": self.timestamp,
        }


class IssueCollector:
    """Collect system information for issue reports."""

    # Known collector table signatures
    COLLECTOR_TABLES = {
        "slurm": ["jobs", "job_accounting", "node_state", "queue_state"],
        "disk": ["storage_state"],
        "gpu": ["gpu_state"],
        "iostat": ["iostat_device"],
        "network": ["network_state"],
        "workstation": ["workstation_state"],
        "interactive": ["interactive_sessions"],
        "cloud": ["cloud_metrics"],
        "vmstat": ["vmstat_state"],
        "mpstat": ["mpstat_state"],
    }

    def __init__(
        self,
        db_path: str | None = None,
        config: dict | None = None,
        source: str = "cli",
    ):
        self.db_path = db_path
        self.config = config or {}
        self.source = source

    def collect(self) -> SystemInfo:
        """Gather all available system information."""
        info = SystemInfo(
            python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            os_info=f"{platform.system()} {platform.release()}",
            source=self.source,
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )

        # NØMAÐ version
        info.nomad_version = self._get_nomad_version()

        # Config-based fields
        issue_cfg = self.config.get("issue_reporting", {})
        info.institution = issue_cfg.get("institution_name", "")
        info.submitted_by = issue_cfg.get("contact_email", "")

        # Database-derived fields
        if self.db_path and Path(self.db_path).exists():
            self._collect_from_db(info)

        return info

    def _get_nomad_version(self) -> str:
        """Get installed NØMAÐ version."""
        try:
            from importlib.metadata import version
            return version("nomad-hpc")
        except Exception:
            pass
        try:
            import nomad
            return getattr(nomad, "__version__", "unknown")
        except Exception:
            return "unknown"

    def _collect_from_db(self, info: SystemInfo) -> None:
        """Extract information from the NØMAÐ database."""
        try:
            db = Path(self.db_path)
            info.db_path = str(db)
            info.db_size_mb = db.stat().st_size / (1024 * 1024)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Detect active collectors from table presence
            tables = {
                row[0]
                for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for collector, signatures in self.COLLECTOR_TABLES.items():
                if any(t in tables for t in signatures):
                    info.active_collectors.append(collector)

            # Cluster count and names
            if "node_state" in tables:
                rows = cursor.execute(
                    "SELECT DISTINCT cluster FROM node_state"
                ).fetchall()
                info.cluster_names = [r[0] for r in rows if r[0]]
                info.cluster_count = len(info.cluster_names)

            # Active alerts
            if "alerts" in tables:
                try:
                    rows = cursor.execute(
                        "SELECT alert_type FROM alerts "
                        "WHERE resolved_at IS NULL "
                        "ORDER BY created_at DESC LIMIT 10"
                    ).fetchall()
                    info.active_alerts = [r[0] for r in rows]
                    info.alert_count = len(info.active_alerts)
                except Exception:
                    pass

            # Total record count (rough estimate from largest table)
            if "jobs" in tables:
                try:
                    count = cursor.execute(
                        "SELECT COUNT(*) FROM jobs"
                    ).fetchone()[0]
                    info.record_count = count
                except Exception:
                    pass

            conn.close()
        except Exception:
            pass
