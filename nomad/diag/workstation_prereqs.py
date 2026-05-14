# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Workstation Prerequisites Diagnostic

Checks the configuration prerequisites that the workstation collector needs
in order to gather complete data. Distinct from `diagnose_workstation` in the
sibling module: that one analyzes collected health data ("the patient is
sick, what's wrong"); this one verifies the data pipeline itself ("is the
thermometer working").

Why this module exists: the workstation collector reports `source=cgroup_v2`
and `status=OK` even when critical fields like `io_read_bytes` are NULL
because the underlying cgroup `io` controller wasn't enabled in
subtree_control. The collector has no way to know — it gets None from the
probe and stores None. The user-visible symptom is silently degraded data
across thousands of snapshots.

Each check returns a structured DiagCheck. Formatters compose the final
output. Strategy mirrors the v1.6.0 lesson: separate computation from
presentation so multiple consumers (terminal, JSON, dashboard panel) can
share one source of truth.

Each check is self-contained and runs over SSH against the workstation,
so the same module works for local diagnosis (the workstation runs the
check on itself) or remote diagnosis (the collector host runs the check
across the fleet).
"""

from __future__ import annotations

import json
import logging
import shlex
import socket
import sqlite3
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────

# Where the cgroup probe should live. Match nomad/collectors/workstation.py
# constants — keep these in sync if either side changes.
PROBE_PATH_PRIMARY = "/usr/local/lib/nomad/cgroup_probe.py"
PROBE_PATH_FALLBACK = "/tmp/nomad_cgroup_probe.py"

# Where we write the persistent IO accounting config
SYSTEMD_CONFIG_PATH = "/etc/systemd/system.conf.d/nomad-io-accounting.conf"

# Required cgroup v2 controllers for full per-user data
REQUIRED_CONTROLLERS = ["cpu", "io", "memory", "pids"]

# How recent must the latest snapshot be to count as "data flowing"?
DATA_FLOW_FRESHNESS_MINUTES = 15


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class DiagCheck:
    """One configuration check. Composable, formatter-agnostic."""
    category: str       # e.g. "Cgroup v2"
    name: str           # short label, e.g. "io controller in user.slice"
    status: str         # "OK", "WARN", "FAIL", "SKIP"
    detail: str = ""    # one-line context shown under the result
    fix_hint: str = ""  # what to do if status != OK; multi-line OK


@dataclass
class WorkstationPrereqDiagnostic:
    """Aggregate result of all prereq checks for one workstation."""
    hostname: str
    checks: list[DiagCheck] = field(default_factory=list)
    ssh_user: str | None = None
    ssh_reachable: bool = True

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "OK")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "WARN")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "FAIL")

    @property
    def overall_status(self) -> str:
        if self.fail_count > 0:
            return "FAIL"
        if self.warn_count > 0:
            return "WARN"
        return "OK"


# ── SSH execution helper ─────────────────────────────────────────────

def _is_local(hostname: str) -> bool:
    """Decide whether the hostname refers to this machine."""
    return hostname in ("localhost", "127.0.0.1", socket.gethostname())


def _run_remote(
    hostname: str,
    cmd: str,
    ssh_user: str | None = None,
    timeout: int = 10,
) -> tuple[int, str, str]:
    """Run a shell command locally or over SSH.

    Returns (returncode, stdout, stderr). Never raises for non-zero exits;
    callers interpret the code. Raises only on actual SSH/process failure.
    """
    if _is_local(hostname):
        full_cmd = cmd
        shell = True
    else:
        target = f"{ssh_user}@{hostname}" if ssh_user else hostname
        # Use BatchMode so SSH never prompts; we want clean fail-fast on
        # auth issues rather than hanging.
        full_cmd = (
            f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"{shlex.quote(target)} {shlex.quote(cmd)}"
        )
        shell = True

    try:
        result = subprocess.run(
            full_cmd, shell=shell, capture_output=True, text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"command timed out after {timeout}s"
    except Exception as e:
        return -1, "", f"execution error: {e}"


# ── Individual checks ────────────────────────────────────────────────

# Each check function takes (hostname, ssh_user) and returns a DiagCheck.
# Each is independently testable; the orchestrator just composes them.

def check_ssh_reachable(hostname: str, ssh_user: str | None) -> DiagCheck:
    """Verify we can SSH to the workstation in the first place."""
    if _is_local(hostname):
        return DiagCheck(
            category="Connectivity", name="SSH reachable",
            status="OK", detail="local execution; SSH not used",
        )
    rc, out, err = _run_remote(hostname, "echo ok", ssh_user, timeout=5)
    if rc == 0 and out == "ok":
        return DiagCheck(
            category="Connectivity", name="SSH reachable",
            status="OK",
            detail=f"connected as {ssh_user or 'default user'}",
        )
    return DiagCheck(
        category="Connectivity", name="SSH reachable",
        status="FAIL",
        detail=f"could not SSH to {hostname}: {err or 'unknown error'}",
        fix_hint=(
            "Verify the host is reachable and SSH keys are set up:\n"
            f"  ssh {ssh_user + '@' if ssh_user else ''}{hostname} echo ok"
        ),
    )


def check_cgroup_v2_mounted(hostname: str, ssh_user: str | None) -> DiagCheck:
    """Confirm /sys/fs/cgroup is a cgroup2 mount."""
    rc, out, err = _run_remote(
        hostname, "stat -fc %T /sys/fs/cgroup 2>&1", ssh_user,
    )
    if rc == 0 and out == "cgroup2fs":
        return DiagCheck(
            category="Cgroup v2", name="cgroup v2 mounted",
            status="OK", detail="/sys/fs/cgroup is cgroup2fs",
        )
    return DiagCheck(
        category="Cgroup v2", name="cgroup v2 mounted",
        status="FAIL",
        detail=f"unexpected fs type: {out!r}",
        fix_hint=(
            "NØMAD requires cgroup v2 (unified hierarchy). Check kernel "
            "boot params for systemd.unified_cgroup_hierarchy=1, or "
            "upgrade to a distribution with cgroup v2 by default "
            "(RHEL/Rocky 9+, Ubuntu 22.04+, recent Fedora)."
        ),
    )


def check_subtree_controllers(
    hostname: str, ssh_user: str | None, scope_path: str, scope_label: str,
) -> list[DiagCheck]:
    """Check which controllers are enabled in a given subtree_control file.

    Returns one DiagCheck per required controller — we want each missing
    controller to show up as a separate finding so users see what's missing.
    """
    rc, out, err = _run_remote(
        hostname, f"cat {scope_path}", ssh_user,
    )
    if rc != 0:
        # Single failure for the whole scope if we can't even read the file
        return [DiagCheck(
            category="Cgroup v2",
            name=f"controllers at {scope_label}",
            status="FAIL",
            detail=f"cannot read {scope_path}: {err or out}",
            fix_hint=f"verify {scope_path} exists and is readable",
        )]

    enabled = set(out.split())
    checks: list[DiagCheck] = []
    for controller in REQUIRED_CONTROLLERS:
        if controller in enabled:
            checks.append(DiagCheck(
                category="Cgroup v2",
                name=f"{controller} controller delegated to {scope_label}",
                status="OK",
                detail=f"present in {scope_path}",
            ))
        else:
            # io is the one we discovered today; flag it more strongly
            severity = "FAIL" if controller == "io" else "WARN"
            checks.append(DiagCheck(
                category="Cgroup v2",
                name=f"{controller} controller delegated to {scope_label}",
                status=severity,
                detail=f"missing from {scope_path}",
                fix_hint=(
                    f"Enable persistently:\n"
                    f"  Add Default{controller.upper()}Accounting=yes to\n"
                    f"  /etc/systemd/system.conf.d/nomad-io-accounting.conf,\n"
                    f"  then: sudo systemctl daemon-reexec\n"
                    f"  followed by: sudo bash -c "
                    f"'echo +{controller} > {scope_path}'"
                ),
            ))
    return checks


def check_persistent_systemd_config(
    hostname: str, ssh_user: str | None,
) -> DiagCheck:
    """Verify the systemd manager config that keeps controllers enabled."""
    rc, out, err = _run_remote(
        hostname, f"cat {SYSTEMD_CONFIG_PATH} 2>&1", ssh_user,
    )
    if rc != 0:
        return DiagCheck(
            category="Cgroup v2",
            name="persistent IO accounting config",
            status="WARN",
            detail=f"{SYSTEMD_CONFIG_PATH} not present",
            fix_hint=(
                "Without DefaultIOAccounting=yes in systemd manager config,\n"
                "the io controller may be reverted by systemd on its own\n"
                "schedule even after manual subtree_control writes.\n"
                "Create the config file:\n"
                "  sudo mkdir -p /etc/systemd/system.conf.d\n"
                f"  sudo tee {SYSTEMD_CONFIG_PATH} <<EOF\n"
                "  [Manager]\n"
                "  DefaultIOAccounting=yes\n"
                "  DefaultCPUAccounting=yes\n"
                "  DefaultMemoryAccounting=yes\n"
                "  DefaultTasksAccounting=yes\n"
                "  EOF\n"
                "  sudo systemctl daemon-reexec"
            ),
        )

    # Parse for the directive we care about most
    has_io = "DefaultIOAccounting=yes" in out
    if has_io:
        return DiagCheck(
            category="Cgroup v2",
            name="persistent IO accounting config",
            status="OK",
            detail=f"DefaultIOAccounting=yes present in {SYSTEMD_CONFIG_PATH}",
        )
    return DiagCheck(
        category="Cgroup v2",
        name="persistent IO accounting config",
        status="WARN",
        detail=f"{SYSTEMD_CONFIG_PATH} exists but lacks DefaultIOAccounting=yes",
        fix_hint=(
            f"Edit {SYSTEMD_CONFIG_PATH} to add:\n"
            "  DefaultIOAccounting=yes\n"
            "then: sudo systemctl daemon-reexec"
        ),
    )


def check_probe_runs(
    hostname: str, ssh_user: str | None,
) -> DiagCheck:
    """Run the deployed probe and confirm it returns valid JSON output."""
    cmd = (
        f"if [ -f {PROBE_PATH_PRIMARY} ]; then python3 {PROBE_PATH_PRIMARY}; "
        f"elif [ -f {PROBE_PATH_FALLBACK} ]; then python3 {PROBE_PATH_FALLBACK}; "
        f"fi"
    )
    rc, out, err = _run_remote(hostname, cmd, ssh_user, timeout=20)
    if rc != 0 or not out:
        return DiagCheck(
            category="Probe deployment", name="cgroup probe runs",
            status="WARN",
            detail=(
                "probe ran but produced no output "
                "(or no active user slices to report)"
            ),
        )

    # Verify at least one line parses as valid JSON with expected fields
    expected_fields = {"hostname", "username", "uid", "io_read_bytes"}
    parsed_count = 0
    has_io = False
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not expected_fields.issubset(d.keys()):
            continue
        parsed_count += 1
        if d.get("io_read_bytes") is not None:
            has_io = True

    if parsed_count == 0:
        return DiagCheck(
            category="Probe deployment", name="cgroup probe runs",
            status="FAIL",
            detail="probe output did not parse as expected JSON",
        )
    if not has_io:
        return DiagCheck(
            category="Probe deployment", name="cgroup probe runs",
            status="WARN",
            detail=(
                f"probe runs and reports {parsed_count} user slice(s) but "
                f"io_read_bytes is None — likely the io controller is not "
                f"delegated to user.slice (see cgroup checks above)"
            ),
        )
    return DiagCheck(
        category="Probe deployment", name="cgroup probe runs",
        status="OK",
        detail=f"probe reports {parsed_count} user slice(s) with IO data",
    )


def check_psacct_installed(
    hostname: str, ssh_user: str | None,
) -> DiagCheck:
    """Process accounting (pacct) is optional but recommended."""
    rc, out, err = _run_remote(
        hostname,
        "test -f /var/account/pacct && echo present || echo absent",
        ssh_user,
    )
    if rc == 0 and out == "present":
        return DiagCheck(
            category="Process accounting", name="psacct enabled",
            status="OK",
            detail="/var/account/pacct exists",
        )
    return DiagCheck(
        category="Process accounting", name="psacct enabled",
        status="WARN",
        detail=(
            "/var/account/pacct not present; pacct collection unavailable "
            "(cgroup snapshots still work)"
        ),
        fix_hint=(
            "Install and enable process accounting:\n"
            "  sudo dnf install -y psacct\n"
            "  sudo systemctl enable --now psacct.service"
        ),
    )


def check_data_flow(
    db_path: str, hostname: str,
) -> list[DiagCheck]:
    """Look at the database to see if data is actually being stored.

    Two sub-checks:
        1. Recent snapshot exists
        2. io_read_bytes is being populated (not all NULL)

    No SSH involved — purely reads the local database.
    """
    checks: list[DiagCheck] = []

    if not db_path:
        return [DiagCheck(
            category="Data flow", name="recent snapshot",
            status="SKIP",
            detail="no database path provided; skipping data-flow checks",
        )]

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        return [DiagCheck(
            category="Data flow", name="recent snapshot",
            status="FAIL",
            detail=f"cannot open {db_path}: {e}",
        )]

    try:
        # Recent snapshot
        row = conn.execute(
            "SELECT MAX(timestamp) FROM workstation_user_snapshot "
            "WHERE hostname = ?", (hostname,),
        ).fetchone()
        last_seen = row[0] if row else None

        if not last_seen:
            checks.append(DiagCheck(
                category="Data flow", name="recent snapshot",
                status="FAIL",
                detail=f"no snapshots ever stored for {hostname}",
                fix_hint=(
                    "Possible causes:\n"
                    "  - Workstation not in the collector's config\n"
                    "  - SSH from collector host failing\n"
                    "  - Probe missing on this workstation\n"
                    "  - Collector not running"
                ),
            ))
        else:
            # Check freshness
            row = conn.execute(
                f"SELECT COUNT(*) FROM workstation_user_snapshot "
                f"WHERE hostname = ? AND timestamp > "
                f"datetime('now', '-{DATA_FLOW_FRESHNESS_MINUTES} minutes')",
                (hostname,),
            ).fetchone()
            recent_count = row[0] if row else 0
            if recent_count > 0:
                checks.append(DiagCheck(
                    category="Data flow", name="recent snapshot",
                    status="OK",
                    detail=(
                        f"{recent_count} snapshots in the last "
                        f"{DATA_FLOW_FRESHNESS_MINUTES} min; latest at {last_seen}"
                    ),
                ))
            else:
                checks.append(DiagCheck(
                    category="Data flow", name="recent snapshot",
                    status="WARN",
                    detail=(
                        f"latest snapshot is older than "
                        f"{DATA_FLOW_FRESHNESS_MINUTES} min ({last_seen})"
                    ),
                ))

            # Check io populated
            row = conn.execute(
                "SELECT COUNT(*) FROM workstation_user_snapshot "
                "WHERE hostname = ? AND io_read_bytes IS NOT NULL "
                "AND timestamp > datetime('now', '-1 hour')",
                (hostname,),
            ).fetchone()
            io_recent = row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) FROM workstation_user_snapshot "
                "WHERE hostname = ? "
                "AND timestamp > datetime('now', '-1 hour')",
                (hostname,),
            ).fetchone()
            total_recent = row[0] if row else 0

            if total_recent == 0:
                # Already covered by recent snapshot check above
                pass
            elif io_recent == 0:
                checks.append(DiagCheck(
                    category="Data flow", name="io_read_bytes populated",
                    status="FAIL",
                    detail=(
                        f"all io_read_bytes NULL across {total_recent} "
                        f"recent snapshots; per-user IO attribution "
                        f"unavailable"
                    ),
                    fix_hint=(
                        "Almost certainly the cgroup io controller is not "
                        "delegated to user.slice. See the Cgroup v2 checks "
                        "above for the fix."
                    ),
                ))
            else:
                pct = 100 * io_recent / total_recent
                checks.append(DiagCheck(
                    category="Data flow", name="io_read_bytes populated",
                    status="OK",
                    detail=(
                        f"{io_recent}/{total_recent} ({pct:.0f}%) recent "
                        f"snapshots have io data"
                    ),
                ))
    finally:
        conn.close()

    return checks


# ── Orchestrator ─────────────────────────────────────────────────────

def check_workstation_prerequisites(
    hostname: str,
    ssh_user: str | None = None,
    db_path: str | None = None,
) -> WorkstationPrereqDiagnostic:
    """Run all prereq checks and return aggregated result.

    Order matters: SSH check first (if it fails, downstream checks would
    fail uselessly). Cgroup checks before probe checks (so the probe-runs
    check can reference cgroup state if needed). Data-flow checks last
    (they don't need SSH).
    """
    diag = WorkstationPrereqDiagnostic(hostname=hostname, ssh_user=ssh_user)

    # 1. Connectivity — gate further checks
    ssh = check_ssh_reachable(hostname, ssh_user)
    diag.checks.append(ssh)
    if ssh.status == "FAIL":
        diag.ssh_reachable = False
        diag.checks.append(DiagCheck(
            category="Cgroup v2", name="(skipped)",
            status="SKIP",
            detail="cgroup checks skipped because SSH is unreachable",
        ))
    else:
        # 2. Cgroup v2 prerequisites
        diag.checks.append(check_cgroup_v2_mounted(hostname, ssh_user))
        diag.checks.extend(check_subtree_controllers(
            hostname, ssh_user,
            scope_path="/sys/fs/cgroup/cgroup.subtree_control",
            scope_label="top-level",
        ))
        diag.checks.extend(check_subtree_controllers(
            hostname, ssh_user,
            scope_path="/sys/fs/cgroup/user.slice/cgroup.subtree_control",
            scope_label="user.slice",
        ))
        diag.checks.append(check_persistent_systemd_config(hostname, ssh_user))

        # 3. Probe deployment
        diag.checks.append(check_probe_runs(hostname, ssh_user))

        # 4. Process accounting (optional)
        diag.checks.append(check_psacct_installed(hostname, ssh_user))

    # 5. Data flow (no SSH; reads database directly)
    if db_path:
        diag.checks.extend(check_data_flow(db_path, hostname))

    return diag


# ── Formatter ────────────────────────────────────────────────────────

# Match the existing nomad/diag/workstation.py Colors class for visual
# consistency. Defined locally to avoid circular import.
class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    DIM = "\033[2m"


def format_prerequisite_checks(
    diag: WorkstationPrereqDiagnostic,
    use_color: bool = True,
    show_fix_hints: bool = True,
) -> str:
    """Render prereq diagnostic as text suitable for terminal output."""
    c = _Colors if use_color else _NoColors()

    status_color = {
        "OK":   c.GREEN,
        "WARN": c.YELLOW,
        "FAIL": c.RED,
        "SKIP": c.DIM,
    }
    status_pad = {
        "OK":   "[OK]    ",
        "WARN": "[WARN]  ",
        "FAIL": "[FAIL]  ",
        "SKIP": "[SKIP]  ",
    }

    lines: list[str] = []
    lines.append(f"\n  {c.BOLD}Collection prerequisites{c.RESET}")
    lines.append(f"  {'─' * 56}")

    # Group by category in input order
    seen_categories: list[str] = []
    by_category: dict[str, list[DiagCheck]] = {}
    for check in diag.checks:
        if check.category not in by_category:
            seen_categories.append(check.category)
            by_category[check.category] = []
        by_category[check.category].append(check)

    for category in seen_categories:
        lines.append(f"\n  {c.BOLD}{category}{c.RESET}")
        for check in by_category[category]:
            color = status_color.get(check.status, c.RESET)
            marker = status_pad.get(check.status, "[?]     ")
            lines.append(
                f"    {color}{marker}{c.RESET}{check.name}"
            )
            if check.detail:
                lines.append(f"            {c.DIM}{check.detail}{c.RESET}")
            if show_fix_hints and check.status in ("WARN", "FAIL") and check.fix_hint:
                for fix_line in check.fix_hint.splitlines():
                    lines.append(f"            {c.DIM}{fix_line}{c.RESET}")

    # Summary
    lines.append("")
    summary_color = status_color.get(diag.overall_status, c.RESET)
    lines.append(
        f"  {c.BOLD}Summary:{c.RESET} "
        f"{c.GREEN}{diag.ok_count} OK{c.RESET} / "
        f"{c.YELLOW}{diag.warn_count} WARN{c.RESET} / "
        f"{c.RED}{diag.fail_count} FAIL{c.RESET}  "
        f"(overall: {summary_color}{diag.overall_status}{c.RESET})"
    )
    return "\n".join(lines)


class _NoColors:
    """Drop-in for _Colors when use_color=False."""
    RESET = ""
    BOLD = ""
    GREEN = ""
    YELLOW = ""
    RED = ""
    DIM = ""
