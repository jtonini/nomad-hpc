# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Output formatters for the NØMAÐ Insight Engine.

Render insights and narrated signals into various output formats:
CLI (terminal), JSON (API/Console), Slack, and email digest.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .correlator import Insight
from .signals import Signal, Severity


# ── Severity decorations ─────────────────────────────────────────────────

_CLI_COLORS = {
    Severity.INFO: "\033[32m",      # green
    Severity.NOTICE: "\033[36m",    # cyan
    Severity.WARNING: "\033[33m",   # yellow
    Severity.CRITICAL: "\033[31m",  # red
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_SEVERITY_ICONS = {
    Severity.INFO: "  ",
    Severity.NOTICE: "  ",
    Severity.WARNING: "  ",
    Severity.CRITICAL: "  ",
}

_SEVERITY_LABELS = {
    Severity.INFO: "OK",
    Severity.NOTICE: "NOTE",
    Severity.WARNING: "WARN",
    Severity.CRITICAL: "CRIT",
}


# ── CLI formatter ────────────────────────────────────────────────────────

def _wrap(text: str, prefix_len: int = 0, indent: str = "        ", width: int = 68) -> str:
    """Wrap text to fit within box, accounting for prefix on first line."""
    import textwrap
    first_width = width - prefix_len
    rest_width = width
    words = text.split()
    if not words:
        return ""
    # Build first line
    first_line = ""
    remaining_words = list(words)
    for w in words:
        test = (first_line + " " + w).strip()
        if len(test) <= first_width:
            first_line = test
            remaining_words.pop(0)
        else:
            break
    if not remaining_words:
        return first_line
    # Build continuation lines
    rest_text = " ".join(remaining_words)
    rest_lines = textwrap.wrap(rest_text, width=rest_width,
                               break_long_words=False,
                               break_on_hyphens=False)
    result = first_line
    for line in rest_lines:
        result += "\n" + indent + line
    return result


def _signal_display_name(title: str) -> str:
    """Convert signal title to human-readable display name."""
    display_names = {
        "gpu_oom":                      "GPU Out of Memory",
        "gpu_job_failure_rate":         "GPU Job Failures",
        "gpu_util_gap":                 "GPU Utilization Gap",
        "gpu_workload_pattern":         "GPU Workload Pattern",
        "gpu_hardware_health":          "GPU Hardware Health",
        "job_success_rate":             "Job Success Rate",
        "partition_failure_concentration": "Partition Failures",
        "oom_failures":                 "Memory Failures (OOM)",
        "timeout_failures":             "Job Timeouts",
        "job_rate_trend":               "Job Rate Trend",
        "filesystem_usage":             "Filesystem Usage",
        "disk_fill_projection":         "Disk Fill Projection",
        "queue_pressure":               "Queue Pressure",
        "high_wait_time":               "High Wait Time",
        "high_network_latency":         "Network Latency",
        "packet_loss":                  "Packet Loss",
        "active_alerts":                "Active Alerts",
        "flapping_alert":               "Flapping Alert",
        "cloud_cost_summary":           "Cloud Cost",
        "underutilized_cloud_instance": "Underutilized Instance",
        "workstation_high_cpu":         "Workstation High CPU",
        "workstation_high_memory":      "Workstation High Memory",
        "workstation_disk_usage":       "Workstation Disk Usage",
        "workstation_zombies":          "Workstation Zombie Processes",
        "diversity_fragility":          "Workload Diversity Fragility",
        "diversity_declining":          "Workload Diversity Declining",
        "capacity_binding_constraint":  "Capacity Binding Constraint",
        "capacity_saturation_imminent": "Capacity Saturation Imminent",
        "niche_contention_risk":        "Resource Contention Risk",
        "resilience_low":               "Cluster Resilience Low",
        "resilience_degrading":         "Cluster Resilience Degrading",
        "externality_detected":         "Inter-group Externality",
    }
    return display_names.get(title, title.replace('_', ' ').title())


def format_cli_brief(
    narratives: list[tuple[Signal, str]],
    insights: list[Insight],
    cluster_name: str = "cluster",
) -> str:
    """
    Produce a concise CLI briefing (for `nomad insights brief`).

    Structure:
      Header -> overall health -> correlated insights -> individual signals
    """
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    W = 64  # box width

    # Header
    lines.append("")
    lines.append(f"  {'=' * W}")
    lines.append(f"  {_BOLD}NØMAÐ Insight Brief{_RESET}")
    lines.append(f"  {cluster_name}  {_DIM}|  {now}{_RESET}")
    lines.append(f"  {'=' * W}")

    # Overall health assessment
    all_severities = [s.severity for s, _ in narratives] + [i.severity for i in insights]
    if not all_severities:
        lines.append("")
        lines.append(f"  {_CLI_COLORS[Severity.INFO]}Cluster health: good. No notable signals.{_RESET}")
        lines.append("")
        return "\n".join(lines)

    worst = max(all_severities, key=lambda s: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(s))

    health_text = {
        Severity.INFO: "good",
        Severity.NOTICE: "nominal with observations",
        Severity.WARNING: "degraded -- action recommended",
        Severity.CRITICAL: "impaired -- immediate attention needed",
    }[worst]

    lines.append("")
    lines.append(f"  Cluster health: {_CLI_COLORS[worst]}{_BOLD}{health_text}{_RESET}")
    lines.append("")

    # Correlated insights first (Level 2)
    if insights:
        lines.append(f"  {_BOLD}Linked Findings{_RESET}")
        lines.append(f"  {'-' * W}")
        for ins in sorted(insights, key=lambda i: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(i.severity), reverse=True):
            color = _CLI_COLORS[ins.severity]
            label = _SEVERITY_LABELS[ins.severity]
            lines.append("")
            lines.append(f"  {color}[{label}]{_RESET} {_wrap(ins.narrative, prefix_len=9, indent='        ')}")
            if ins.recommendation:
                lines.append(f"  {_DIM}    >> {_wrap(ins.recommendation, prefix_len=7, indent='       ')}{_RESET}")
        lines.append("")

    # Individual signals (not already covered by insights)
    consumed_keys = set()
    for ins in insights:
        for sig in ins.source_signals:
            consumed_keys.add(sig.key)

    remaining = [(sig, narr) for sig, narr in narratives if sig.key not in consumed_keys]

    # Sort by severity (critical first)
    sev_order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.NOTICE: 2, Severity.INFO: 3}
    remaining.sort(key=lambda x: sev_order.get(x[0].severity, 4))

    if remaining:
        lines.append(f"  {_BOLD}Signals{_RESET}")
        lines.append(f"  {'-' * W}")
        for sig, narr in remaining:
            color = _CLI_COLORS[sig.severity]
            label = _SEVERITY_LABELS[sig.severity]
            lines.append(f"  {color}[{label}]{_RESET} {_wrap(narr, prefix_len=9, indent='        ')}")
        lines.append("")

    lines.append(f"  {'=' * W}")
    lines.append(f"  {_DIM}Run 'nomad insights detail' for full analysis.{_RESET}")
    lines.append("")

    return "\n".join(lines)


def format_cli_detail(
    narratives: list[tuple[Signal, str]],
    insights: list[Insight],
    cluster_name: str = "cluster",
) -> str:
    """
    Produce a detailed CLI report (for `nomad insights detail`).
    Includes all signals, all insights, and metrics.
    """
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines.append("")
    lines.append(f"{'=' * 62}")
    lines.append(f"  NØMAÐ Insight Report — {cluster_name}")
    lines.append(f"  {now}")
    lines.append(f"{'=' * 62}")
    lines.append("")

    if insights:
        lines.append(f"{_BOLD}CORRELATED FINDINGS{_RESET}")
        lines.append(f"{'─' * 62}")
        for i, ins in enumerate(insights, 1):
            color = _CLI_COLORS[ins.severity]
            label = _SEVERITY_LABELS[ins.severity]
            lines.append(f"\n  {color}{_BOLD}[{label}] {_signal_display_name(ins.title)}{_RESET}")
            lines.append(f"  Category: {ins.category}")
            lines.append(f"  Signals combined: {ins.signal_count}")
            lines.append(f"\n  {ins.narrative}")
            if ins.recommendation:
                lines.append(f"\n  {_BOLD}Recommendation:{_RESET} {ins.recommendation}")
            lines.append("")

    sev_order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.NOTICE: 2, Severity.INFO: 3}
    sorted_narr = sorted(narratives, key=lambda x: sev_order.get(x[0].severity, 4))

    lines.append(f"{_BOLD}ALL SIGNALS ({len(sorted_narr)}){_RESET}")
    lines.append(f"{'─' * 62}")
    for sig, narr in sorted_narr:
        color = _CLI_COLORS[sig.severity]
        label = _SEVERITY_LABELS[sig.severity]
        lines.append(f"\n  {color}[{label}]{_RESET} {_BOLD}{_signal_display_name(sig.title)}{_RESET}")
        lines.append(f"  Type: {sig.signal_type.value}")
        lines.append(f"  {narr}")
        if sig.affected_entities:
            lines.append(f"  Affected: {', '.join(sig.affected_entities)}")
        if sig.metrics:
            metrics_str = ", ".join(f"{k}={v}" for k, v in sig.metrics.items()
                                    if not isinstance(v, (list, dict)))
            if metrics_str:
                lines.append(f"  {_DIM}Metrics: {metrics_str}{_RESET}")
        lines.append("")

    lines.append(f"{'=' * 62}")
    lines.append("")

    return "\n".join(lines)


# ── JSON formatter ───────────────────────────────────────────────────────

def format_json(
    narratives: list[tuple[Signal, str]],
    insights: list[Insight],
    cluster_name: str = "cluster",
) -> str:
    """
    Produce JSON output for API/Console consumption.
    """
    now = datetime.now().isoformat()

    all_severities = [s.severity for s, _ in narratives] + [i.severity for i in insights]
    worst = "good"
    if all_severities:
        worst_sev = max(all_severities,
                    key=lambda s: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(s))
        worst = {
            Severity.INFO: "good",
            Severity.NOTICE: "nominal",
            Severity.WARNING: "degraded",
            Severity.CRITICAL: "impaired",
        }.get(worst_sev, "good")

    data: dict[str, Any] = {
        "timestamp": now,
        "cluster": cluster_name,
        "overall_health": worst,
        "signal_count": len(narratives),
        "insight_count": len(insights),
        "insights": [
            {
                "title": ins.title,
                "narrative": ins.narrative,
                "severity": ins.severity.value,
                "recommendation": ins.recommendation,
                "category": ins.category,
                "signal_count": ins.signal_count,
            }
            for ins in insights
        ],
        "signals": [
            {
                "type": sig.signal_type.value,
                "severity": sig.severity.value,
                "title": sig.title,
                "narrative": narr,
                "metrics": sig.metrics,
                "affected_entities": sig.affected_entities,
            }
            for sig, narr in narratives
        ],
    }

    return json.dumps(data, indent=2)


# ── Slack formatter ──────────────────────────────────────────────────────

_SLACK_ICONS = {
    Severity.INFO: ":large_green_circle:",
    Severity.NOTICE: ":large_blue_circle:",
    Severity.WARNING: ":warning:",
    Severity.CRITICAL: ":red_circle:",
}


def format_slack(
    narratives: list[tuple[Signal, str]],
    insights: list[Insight],
    cluster_name: str = "cluster",
) -> str:
    """
    Produce a Slack-formatted message (Markdown).
    """
    blocks: list[str] = []
    now = datetime.now().strftime("%H:%M")

    # Health header
    all_severities = [s.severity for s, _ in narratives] + [i.severity for i in insights]
    if not all_severities:
        return f"*NØMAÐ — {cluster_name}* ({now})\n:large_green_circle: All systems nominal."

    worst = max(all_severities,
                key=lambda s: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(s))
    icon = _SLACK_ICONS[worst]

    blocks.append(f"*NØMAÐ — {cluster_name}* ({now})")
    blocks.append(f"{icon} *Status: {worst.value.upper()}*")

    if insights:
        blocks.append("")
        for ins in insights:
            icon = _SLACK_ICONS[ins.severity]
            blocks.append(f"{icon} {ins.narrative}")
            if ins.recommendation:
                blocks.append(f"> {ins.recommendation}")
            blocks.append("")

    # Only show non-correlated signals above NOTICE
    consumed_keys = set()
    for ins in insights:
        for sig in ins.source_signals:
            consumed_keys.add(sig.key)

    notable = [(sig, narr) for sig, narr in narratives
               if sig.key not in consumed_keys
               and sig.severity in (Severity.WARNING, Severity.CRITICAL)]

    if notable:
        for sig, narr in notable[:5]:  # Limit to avoid wall of text
            icon = _SLACK_ICONS[sig.severity]
            blocks.append(f"{icon} {narr}")

    return "\n".join(blocks)


# ── Email digest formatter ───────────────────────────────────────────────

def format_email_digest(
    narratives: list[tuple[Signal, str]],
    insights: list[Insight],
    cluster_name: str = "cluster",
    period: str = "daily",
) -> tuple[str, str]:
    """
    Produce an email digest (subject, body).
    Returns (subject_line, body_text).
    """
    now = datetime.now().strftime("%Y-%m-%d")

    all_severities = [s.severity for s, _ in narratives] + [i.severity for i in insights]
    if not all_severities:
        subject = f"NØMAÐ {period.title()} Digest — {cluster_name} — All Clear ({now})"
        body = (
            f"NØMAÐ {period.title()} Digest\n"
            f"Cluster: {cluster_name}\n"
            f"Date: {now}\n\n"
            f"All systems nominal. No notable signals detected.\n"
        )
        return subject, body

    worst = max(all_severities,
                key=lambda s: [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL].index(s))

    subject = f"NØMAÐ {period.title()} Digest — {cluster_name} — {worst.value.upper()} ({now})"

    body_parts: list[str] = []
    body_parts.append(f"NØMAÐ {period.title()} Digest")
    body_parts.append(f"Cluster: {cluster_name}")
    body_parts.append(f"Date: {now}")
    body_parts.append(f"Overall status: {worst.value.upper()}")
    body_parts.append(f"Signals: {len(narratives)} | Correlated findings: {len(insights)}")
    body_parts.append("")
    body_parts.append("=" * 50)

    if insights:
        body_parts.append("")
        body_parts.append("KEY FINDINGS")
        body_parts.append("-" * 50)
        for ins in insights:
            label = _SEVERITY_LABELS[ins.severity]
            body_parts.append(f"\n[{label}] {_signal_display_name(ins.title)}")
            body_parts.append(ins.narrative)
            if ins.recommendation:
                body_parts.append(f"Recommendation: {ins.recommendation}")

    body_parts.append("")
    body_parts.append("ALL SIGNALS")
    body_parts.append("-" * 50)

    sev_order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.NOTICE: 2, Severity.INFO: 3}
    for sig, narr in sorted(narratives, key=lambda x: sev_order.get(x[0].severity, 4)):
        label = _SEVERITY_LABELS[sig.severity]
        body_parts.append(f"\n[{label}] {narr}")

    body_parts.append("")
    body_parts.append("=" * 50)
    body_parts.append("Generated by NØMAÐ — nomad-hpc.com")

    return subject, "\n".join(body_parts)
