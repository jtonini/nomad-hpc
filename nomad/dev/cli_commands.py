# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Developer Toolchain — CLI Commands

Click command definitions for the `nomad dev` command family.
These get patched into cli.py by the integration script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from .scaffolding import MODULE_TYPES, ScaffoldEngine, ScaffoldResult
from .checker import HealthChecker, CheckReport


def _get_repo_root() -> Path:
    """Find the NØMAÐ repository root."""
    # Walk up from CWD looking for pyproject.toml with nomad-hpc
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        pyproject = parent / "pyproject.toml"
        if pyproject.exists() and "nomad" in pyproject.read_text():
            return parent
    # Fallback: check if nomad/ directory exists
    if (cwd / "nomad").is_dir():
        return cwd
    return cwd


def _format_scaffold_result(result: ScaffoldResult) -> None:
    """Display scaffold result to the user."""
    if not result.success:
        click.echo(f"\n  Error: {result.error}", err=True)
        return

    click.echo(f"\nCreating {result.module_type} scaffold: {result.module_name}")
    click.echo()

    for f in result.created_files:
        click.echo(f"  \u2713 Created: {f}")

    for f in result.modified_files:
        click.echo(f"  \u2713 Updated: {f}")

    if result.next_steps:
        click.echo("\nNext steps:")
        for i, step in enumerate(result.next_steps, 1):
            click.echo(f"  {i}. {step}")

    if result.references:
        click.echo("\nSee existing modules for reference:")
        for ref in result.references:
            click.echo(f"  {ref}")


def _format_check_report(report: CheckReport, verbose: bool = False) -> None:
    """Display check report to the user."""
    STATUS_SYMBOLS = {
        "pass": "\u2713",
        "warning": "\u2717",
        "error": "\u2717",
        "info": "\u25cb",
    }
    STATUS_LABELS = {
        "pass": "",
        "warning": " WARNING:",
        "error": " ERROR:",
        "info": " INFO:",
    }

    click.echo("\nNOMAD Codebase Health Check")
    click.echo("=" * 28)

    current_category = None
    for item in report.items:
        if item.category != current_category:
            click.echo(f"\n{item.category}")
            current_category = item.category

        symbol = STATUS_SYMBOLS.get(item.status, "?")
        label = STATUS_LABELS.get(item.status, "")
        click.echo(f"  {symbol}{label} {item.description}")

        if item.details and (verbose or item.status in ("warning", "error")):
            for line in item.details.split("\n"):
                click.echo(f"    {line}")

    click.echo(f"\n{report.summary_line()}")
    if report.has_warnings:
        click.echo("Run 'nomad dev check --fix' to auto-fix registration issues.")


# =============================================================================
# DEVELOPER COMMANDS (patched into cli.py)
# =============================================================================

@click.group()
def dev():
    """NØMAÐ Developer Toolchain.

    Scaffolding, validation, and contribution pipeline for NØMAÐ
    module development.
    """
    pass


# ─── nomad dev guide ─────────────────────────────────────────────────

@dev.command()
@click.pass_context
def guide(ctx):
    """Interactive contribution wizard.

    Walks through the entire process of creating a new NØMAÐ module:
    what to build, gathers parameters, scaffolds, provides next steps.
    """
    click.echo("\nWelcome to the NØMAÐ developer guide.\n")
    click.echo("What would you like to build?")

    types_list = list(MODULE_TYPES.values())
    for i, mtype in enumerate(types_list, 1):
        click.echo(f"  {i}. {mtype.guide_label}")

    click.echo()
    choice = click.prompt("Select", type=int)

    if choice < 1 or choice > len(types_list):
        click.echo("Invalid choice.", err=True)
        return

    selected = types_list[choice - 1]
    click.echo()

    # Gather parameters via prompts
    params = {}
    for prompt_def in selected.guide_prompts:
        value = click.prompt(prompt_def["prompt"])
        params[prompt_def["key"]] = value

    # Get module name
    click.echo()
    name = click.prompt(
        f"{selected.name.capitalize()} name (lowercase, no spaces, used in filenames)"
    )

    # Handle special access prompt for collectors
    if selected.name == "collector" and params.get("access") == "3":
        params["package"] = click.prompt("Package name")

    click.echo()

    # Scaffold
    repo_root = _get_repo_root()
    engine = ScaffoldEngine(repo_root)
    result = engine.scaffold(selected.name, name, params)
    _format_scaffold_result(result)


# ─── nomad dev new ───────────────────────────────────────────────────

@dev.command("new")
@click.argument("module_type", type=click.Choice(list(MODULE_TYPES.keys())))
@click.argument("name")
@click.pass_context
def new(ctx, module_type, name):
    """Scaffold a new module directly.

    Usage: nomad dev new <type> <name>

    Types: collector, command, analysis, metric, view, page, alert, insight
    """
    repo_root = _get_repo_root()
    engine = ScaffoldEngine(repo_root)
    result = engine.scaffold(module_type, name)
    _format_scaffold_result(result)


# ─── nomad dev check ─────────────────────────────────────────────────

@dev.command("check")
@click.option("--fix", is_flag=True, help="Auto-fix fixable issues")
@click.option("--strict", is_flag=True, help="Treat warnings as errors (for CI)")
@click.option("--module", "-m", help="Check only a specific module")
@click.option("--verbose", "-v", is_flag=True, help="Show details for all checks")
@click.option("--format", "-f", "output_format",
              type=click.Choice(["text", "json"]), default="text",
              help="Output format")
@click.pass_context
def check(ctx, fix, strict, module, verbose, output_format):
    """Validate codebase health.

    Checks module registration, test coverage, documentation,
    code quality, architecture consistency, and config integrity.
    """
    repo_root = _get_repo_root()
    checker = HealthChecker(repo_root)
    report = checker.check_all(strict=strict, module=module)

    if fix:
        actions = checker.fix(report)
        if actions:
            click.echo("\nAuto-fix actions:")
            for action in actions:
                click.echo(f"  \u2713 {action}")
            # Re-run checks after fix
            report = checker.check_all(strict=strict, module=module)

    if output_format == "json":
        items = []
        for item in report.items:
            items.append({
                "category": item.category,
                "description": item.description,
                "status": item.status,
                "details": item.details,
                "fixable": item.fixable,
            })
        click.echo(json.dumps({
            "items": items,
            "summary": report.summary,
        }, indent=2))
    else:
        _format_check_report(report, verbose=verbose)

    if report.has_errors:
        sys.exit(1)


# ─── nomad dev test ──────────────────────────────────────────────────

@dev.command("test")
@click.argument("scope", default="changed")
@click.argument("name", default="", required=False)
@click.option("--coverage", is_flag=True, help="Show coverage report")
@click.option("--verbose", "-v", is_flag=True, help="Verbose test output")
@click.pass_context
def test(ctx, scope, name, coverage, verbose):
    """Run targeted tests.

    Scope: changed (default), all, collectors, dynamics, or a specific module name.

    \b
    Examples:
      nomad dev test                    # test only changed files
      nomad dev test all                # full test suite
      nomad dev test collector zfs      # test specific module
      nomad dev test collectors         # test all collectors
      nomad dev test changed            # test what changed since last commit
    """
    repo_root = _get_repo_root()
    tests_dir = repo_root / "tests"

    if not tests_dir.exists():
        click.echo("No tests/ directory found.", err=True)
        return

    cmd = ["python", "-m", "pytest"]

    if verbose:
        cmd.append("-v")

    if coverage:
        cmd.extend(["--cov=nomad", "--cov-report=term-missing"])

    if scope == "all":
        cmd.append(str(tests_dir))
    elif scope == "changed":
        # Find files changed since last commit
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                capture_output=True, text=True, cwd=repo_root,
            )
            changed = result.stdout.strip().split("\n")
            # Also include staged changes
            result2 = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True, text=True, cwd=repo_root,
            )
            changed.extend(result2.stdout.strip().split("\n"))
            changed = [f for f in set(changed) if f.strip()]

            # Map changed source files to test files
            test_files = set()
            for f in changed:
                if f.startswith("tests/"):
                    test_files.add(str(repo_root / f))
                elif f.startswith("nomad/"):
                    # Try to find matching test
                    base = Path(f).stem
                    for pattern in [
                        f"test_{base}.py",
                        f"test_collector_{base}.py",
                        f"test_dynamics_{base}.py",
                        f"test_command_{base}.py",
                    ]:
                        test_path = tests_dir / pattern
                        if test_path.exists():
                            test_files.add(str(test_path))

            if not test_files:
                click.echo("No test files found for changed modules.")
                click.echo("Run 'nomad dev test all' for the full suite.")
                return

            cmd.extend(sorted(test_files))
            click.echo(f"Running {len(test_files)} test file(s) for changed modules...\n")

        except FileNotFoundError:
            click.echo("git not found. Running all tests instead.")
            cmd.append(str(tests_dir))
    elif scope in ("collectors", "dynamics", "analysis", "alerts", "insights"):
        # Test all modules of a type
        prefix_map = {
            "collectors": "test_collector_",
            "dynamics": "test_dynamics",
            "analysis": "test_analysis_",
            "alerts": "test_alert_",
            "insights": "test_insight_",
        }
        prefix = prefix_map.get(scope, f"test_{scope}")
        matches = list(tests_dir.glob(f"{prefix}*.py"))
        if not matches:
            click.echo(f"No test files matching '{prefix}*' found.")
            return
        cmd.extend(str(m) for m in matches)
    elif scope == "collector" and name:
        # Specific module test
        for pattern in [
            f"test_collector_{name}.py",
            f"test_{name}.py",
        ]:
            test_path = tests_dir / pattern
            if test_path.exists():
                cmd.append(str(test_path))
                break
        else:
            click.echo(f"No test file found for collector '{name}'.")
            return
    elif scope in MODULE_TYPES and name:
        # Generic module-type + name
        prefix = MODULE_TYPES[scope].test_prefix
        for pattern in [
            f"{prefix}_{name}.py",
            f"test_{name}.py",
        ]:
            test_path = tests_dir / pattern
            if test_path.exists():
                cmd.append(str(test_path))
                break
        else:
            click.echo(f"No test file found for {scope} '{name}'.")
            return
    else:
        # Treat scope as a module name
        for pattern in [f"test_{scope}.py", f"test_collector_{scope}.py",
                        f"test_dynamics_{scope}.py", f"test_command_{scope}.py"]:
            test_path = tests_dir / pattern
            if test_path.exists():
                cmd.append(str(test_path))
                break
        else:
            click.echo(f"No test file found for '{scope}'.")
            return

    # Run pytest
    click.echo(f"Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=repo_root)
    sys.exit(result.returncode)


# ─── nomad dev status ────────────────────────────────────────────────

@dev.command("status")
@click.pass_context
def status(ctx):
    """Show current development state.

    Displays current branch, changes, and readiness for submission.
    """
    repo_root = _get_repo_root()

    # Current branch
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, cwd=repo_root,
        )
        branch = result.stdout.strip()
        click.echo(f"Current branch: {branch}")
    except FileNotFoundError:
        click.echo("git not available.")
        return

    # Check if up to date with main
    subprocess.run(
        ["git", "fetch", "origin", "main"],
        capture_output=True, cwd=repo_root,
    )
    result = subprocess.run(
        ["git", "log", "origin/main..HEAD", "--oneline"],
        capture_output=True, text=True, cwd=repo_root,
    )
    ahead = len([l for l in result.stdout.strip().split("\n") if l.strip()])
    result = subprocess.run(
        ["git", "log", "HEAD..origin/main", "--oneline"],
        capture_output=True, text=True, cwd=repo_root,
    )
    behind = len([l for l in result.stdout.strip().split("\n") if l.strip()])

    status_parts = []
    if ahead:
        status_parts.append(f"{ahead} ahead")
    if behind:
        status_parts.append(f"{behind} behind")
    if not status_parts:
        status_parts.append("up to date")
    click.echo(f"Base: main ({', '.join(status_parts)})")

    # Changes
    click.echo("\nChanges:")
    result = subprocess.run(
        ["git", "diff", "--stat", "origin/main...HEAD"],
        capture_output=True, text=True, cwd=repo_root,
    )
    if result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            click.echo(f"  {line}")
    else:
        click.echo("  No changes from main.")

    # Readiness checks (quick)
    click.echo("\nReadiness:")
    checker = HealthChecker(repo_root)
    report = checker.check_all()

    if report.has_errors:
        click.echo(f"  \u2717 nomad dev check: {report.summary['error']} error(s)")
    elif report.has_warnings:
        click.echo(f"  ~ nomad dev check: {report.summary['warning']} warning(s)")
    else:
        click.echo("  \u2713 nomad dev check: all passed")

    # Test status
    test_result = subprocess.run(
        ["python", "-m", "pytest", "--co", "-q", str(repo_root / "tests")],
        capture_output=True, text=True, cwd=repo_root,
    )
    if test_result.returncode == 0:
        test_count = test_result.stdout.strip().split("\n")[-1]
        click.echo(f"  \u2713 Tests collected: {test_count}")
    else:
        click.echo("  \u2717 Test collection failed")


# ─── nomad dev submit ────────────────────────────────────────────────

@dev.command("submit")
@click.option("--patch", is_flag=True, help="Generate patch file instead of PR")
@click.option("--dry-run", is_flag=True, help="Show what would happen without doing it")
@click.pass_context
def submit(ctx, patch, dry_run):
    """Submit contribution as a pull request.

    Runs all validation checks, creates a branch, commits,
    and opens a PR (or generates a patch file).
    """
    repo_root = _get_repo_root()

    click.echo("Pre-submission validation...\n")

    # 1. Run checks
    click.echo("  Running: nomad dev check --strict")
    checker = HealthChecker(repo_root)
    report = checker.check_all(strict=True)
    if report.has_errors:
        click.echo(f"  \u2717 {report.summary['error']} error(s) found. Fix before submitting.")
        _format_check_report(report)
        sys.exit(1)
    click.echo("  \u2713 All checks passed")

    # 2. Run tests
    click.echo("\n  Running: nomad dev test changed")
    test_result = subprocess.run(
        ["python", "-m", "pytest", "-q", str(repo_root / "tests")],
        capture_output=True, text=True, cwd=repo_root,
    )
    if test_result.returncode != 0:
        click.echo(f"  \u2717 Tests failed:\n{test_result.stdout}")
        sys.exit(1)
    click.echo("  \u2713 Tests passing")

    # 3. Ruff
    click.echo("\n  Running: ruff check")
    try:
        ruff_result = subprocess.run(
            ["ruff", "check", str(repo_root / "nomad")],
            capture_output=True, text=True, cwd=repo_root,
        )
        if ruff_result.returncode != 0:
            click.echo(f"  \u2717 Linting issues:\n{ruff_result.stdout}")
            sys.exit(1)
        click.echo("  \u2713 Linting clean")
    except FileNotFoundError:
        click.echo("  \u25cb ruff not installed — skipping")

    # 4. Analyze changes
    click.echo("\nAnalyzing changes...")
    result = subprocess.run(
        ["git", "diff", "--name-status", "origin/main...HEAD"],
        capture_output=True, text=True, cwd=repo_root,
    )
    new_files = []
    modified_files = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            status, filepath = parts[0], parts[-1]
            if status.startswith("A"):
                new_files.append(filepath)
            elif status.startswith("M"):
                modified_files.append(filepath)

    if new_files:
        click.echo(f"  New files: {len(new_files)}")
        for f in new_files:
            click.echo(f"    {f}")
    if modified_files:
        click.echo(f"  Modified files: {len(modified_files)}")
        for f in modified_files:
            click.echo(f"    {f}")

    # Detect contribution type
    contrib_type = "general"
    branch_suffix = "update"
    for f in new_files:
        if "collectors/" in f:
            contrib_type = "New Collector"
            # Extract name
            name = Path(f).stem
            branch_suffix = f"collector-{name}"
            break
        elif "dynamics/" in f:
            contrib_type = "New Dynamics Metric"
            name = Path(f).stem
            branch_suffix = f"metric-{name}"
            break
        elif "analysis/" in f:
            contrib_type = "New Analysis Module"
            name = Path(f).stem
            branch_suffix = f"analysis-{name}"
            break

    click.echo(f"\n  Detected contribution type: {contrib_type}")
    click.echo(f"  Suggested branch name: contrib/{branch_suffix}")

    if dry_run:
        click.echo("\n[Dry run — no changes made]")
        return

    # Get description
    description = click.prompt("\nDescription (what does this module do and why?)")

    if not click.confirm("\nConfirm submission?"):
        return

    if patch:
        # Generate patch file
        patch_path = repo_root / f"contrib-{branch_suffix}.patch"
        subprocess.run(
            ["git", "diff", "origin/main...HEAD"],
            stdout=open(patch_path, "w"),
            cwd=repo_root,
        )
        click.echo(f"\n\u2713 Patch exported to: {patch_path}")
        click.echo("  Email this file to the maintainer for review.")
        return

    # Git operations
    branch_name = f"contrib/{branch_suffix}"
    click.echo("\nGit operations...")

    # Check for GitHub token
    token_path = Path.home() / ".config" / "nomad" / "dev.toml"
    has_token = False
    if token_path.exists():
        content = token_path.read_text()
        if "github_token" in content:
            has_token = True

    # Create and push branch
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        capture_output=True, cwd=repo_root,
    )
    click.echo(f"  \u2713 Branch created: {branch_name}")

    # Commit message
    commit_type = "feat"
    if contrib_type == "New Collector":
        commit_msg = f"feat(collectors): add {branch_suffix.replace('collector-', '')} collector"
    elif contrib_type == "New Dynamics Metric":
        commit_msg = f"feat(dynamics): add {branch_suffix.replace('metric-', '')} metric"
    else:
        commit_msg = f"feat: {description[:72]}"

    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, cwd=repo_root,
    )
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        capture_output=True, cwd=repo_root,
    )
    click.echo(f'  \u2713 Changes committed: "{commit_msg}"')

    push_result = subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        capture_output=True, text=True, cwd=repo_root,
    )
    if push_result.returncode == 0:
        click.echo("  \u2713 Pushed to origin")
    else:
        click.echo(f"  \u2717 Push failed: {push_result.stderr}")
        click.echo("  You can push manually: git push -u origin " + branch_name)

    # PR creation
    if has_token:
        click.echo("\n  (PR creation via GitHub API not yet implemented)")
        click.echo("  Open a PR at: https://github.com/jtonini/nomad-hpc/compare/"
                    f"main...{branch_name}")
    else:
        click.echo("\nNo GitHub token configured.")
        click.echo(f"Open a PR at: https://github.com/jtonini/nomad-hpc/compare/"
                    f"main...{branch_name}")
        click.echo("Or run 'nomad dev setup' to configure a token.")


# ─── nomad dev setup ─────────────────────────────────────────────────

@dev.command("setup")
@click.pass_context
def setup(ctx):
    """One-time developer environment configuration.

    Sets up GitHub token, installs dev dependencies,
    and configures pre-commit hooks.
    """
    click.echo("\nNOMAD Developer Setup")
    click.echo("=" * 21)

    # Step 1: GitHub authentication
    click.echo("\nStep 1: GitHub Authentication (for nomad dev submit and nomad issue)")
    click.echo()

    config_dir = Path.home() / ".config" / "nomad"
    config_path = config_dir / "dev.toml"

    if config_path.exists():
        if not click.confirm("  dev.toml exists. Reconfigure?", default=False):
            click.echo("  Skipping.")
        else:
            _setup_github_token(config_dir, config_path)
    else:
        if click.confirm("  Configure GitHub token now?", default=True):
            _setup_github_token(config_dir, config_path)
        else:
            click.echo("  Skipped. You can use 'nomad dev submit --patch' without a token.")

    # Step 2: Dev environment
    click.echo("\nStep 2: Development Environment")

    # Check Python
    py_version = sys.version.split()[0]
    click.echo(f"  \u2713 Python {py_version} detected")

    # Check git
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True,
        )
        email = result.stdout.strip()
        click.echo(f"  \u2713 git configured (user: {email})")
    except FileNotFoundError:
        click.echo("  \u2717 git not found — install git first")

    # Install dev dependencies
    click.echo("\n  Installing development dependencies...")
    deps = ["ruff", "pytest", "pytest-cov"]
    for dep in deps:
        try:
            importlib.import_module(dep.replace("-", "_").split("[")[0])
            click.echo(f"  \u2713 {dep} (already installed)")
        except ImportError:
            click.echo(f"  Installing {dep}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", dep, "--quiet"],
                capture_output=True,
            )
            click.echo(f"  \u2713 {dep}")

    # Step 3: Pre-commit hooks
    click.echo("\nStep 3: Pre-commit Hooks")
    repo_root = _get_repo_root()
    hooks_dir = repo_root / ".git" / "hooks"

    if hooks_dir.exists():
        pre_commit = hooks_dir / "pre-commit"
        hook_content = """#!/bin/bash
# NØMAÐ pre-commit hook (installed by nomad dev setup)

echo "Running NØMAÐ pre-commit checks..."

# Ruff linting
if command -v ruff &> /dev/null; then
    ruff check nomad/ --quiet
    if [ $? -ne 0 ]; then
        echo "Ruff linting failed. Fix issues before committing."
        echo "Run: ruff check --fix nomad/"
        exit 1
    fi
fi

# Quick module check on changed files only
python -c "
from nomad.dev.checker import HealthChecker
from pathlib import Path
import subprocess

result = subprocess.run(['git', 'diff', '--cached', '--name-only'], capture_output=True, text=True)
changed = [f for f in result.stdout.strip().split(chr(10)) if f.startswith('nomad/') and f.endswith('.py')]
if changed:
    # Extract module names
    for f in changed:
        parts = f.split('/')
        if len(parts) >= 3:
            module = parts[-1].replace('.py', '')
            checker = HealthChecker(Path('.'))
            report = checker.check_all(module=module)
            if report.has_errors:
                print(f'Module check failed for {module}')
                exit(1)
" 2>/dev/null

echo "Pre-commit checks passed."
"""
        pre_commit.write_text(hook_content)
        pre_commit.chmod(0o755)
        click.echo("  \u2713 pre-commit: ruff check")
        click.echo("  \u2713 pre-commit: nomad dev check --module (changed modules only)")
    else:
        click.echo("  \u25cb Not in a git repository — skipping hooks")

    click.echo("\nSetup complete! You're ready to contribute.\n")
    click.echo("Quick start:")
    click.echo("  nomad dev guide        # Interactive: what do you want to build?")
    click.echo("  nomad dev new ...      # Scaffold a new module")
    click.echo("  nomad dev check        # Validate your changes")
    click.echo("  nomad dev submit       # Submit your contribution")


def _setup_github_token(config_dir: Path, config_path: Path) -> None:
    """Set up GitHub token."""
    click.echo()
    click.echo("  You can create a fine-grained GitHub token at:")
    click.echo("  https://github.com/settings/tokens?type=beta")
    click.echo()
    click.echo("  Required permissions:")
    click.echo("    - Repository: jtonini/nomad-hpc")
    click.echo("    - Contents: Read and write")
    click.echo("    - Pull requests: Read and write")
    click.echo("    - Issues: Read and write")
    click.echo()

    token = click.prompt("  Paste your token (input hidden)", hide_input=True)

    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f'# NØMAÐ Developer Configuration\ngithub_token = "{token}"\n')
    config_path.chmod(0o600)
    click.echo(f"\n  \u2713 Saved to {config_path}")


# ─── nomad dev bump ──────────────────────────────────────────────────

@dev.command("bump")
@click.argument("level", type=click.Choice(["patch", "minor", "major"]))
@click.option("--dry-run", is_flag=True, help="Show what would change")
@click.pass_context
def bump(ctx, level, dry_run):
    """Manage version increments.

    Updates pyproject.toml, __version__, CHANGELOG, creates git tag.
    """
    repo_root = _get_repo_root()
    pyproject = repo_root / "pyproject.toml"

    if not pyproject.exists():
        click.echo("pyproject.toml not found.", err=True)
        return

    content = pyproject.read_text()

    # Extract current version
    match = re.search(r'version\s*=\s*"(\d+\.\d+\.\d+)"', content)
    if not match:
        click.echo("Could not find version in pyproject.toml.", err=True)
        return

    current = match.group(1)
    parts = [int(x) for x in current.split(".")]

    if level == "major":
        parts = [parts[0] + 1, 0, 0]
    elif level == "minor":
        parts = [parts[0], parts[1] + 1, 0]
    else:
        parts = [parts[0], parts[1], parts[2] + 1]

    new_version = ".".join(str(p) for p in parts)

    click.echo(f"Version: {current} -> {new_version}")

    if dry_run:
        click.echo("[Dry run — no changes made]")
        return

    if not click.confirm("Proceed?"):
        return

    # Update pyproject.toml
    new_content = content.replace(f'version = "{current}"', f'version = "{new_version}"')
    pyproject.write_text(new_content)
    click.echo("  \u2713 Updated pyproject.toml")

    # Update __version__ in __init__.py
    init_path = repo_root / "nomad" / "__init__.py"
    if init_path.exists():
        init_content = init_path.read_text()
        init_content = re.sub(
            r'__version__\s*=\s*"[^"]*"',
            f'__version__ = "{new_version}"',
            init_content,
        )
        init_path.write_text(init_content)
        click.echo("  \u2713 Updated nomad/__init__.py")

    # Update CHANGELOG
    changelog = repo_root / "CHANGELOG.md"
    if changelog.exists():
        cl_content = changelog.read_text()
        today = __import__("datetime").date.today().isoformat()
        cl_content = cl_content.replace(
            "## [Unreleased]",
            f"## [Unreleased]\n\n## [{new_version}] - {today}",
        )
        changelog.write_text(cl_content)
        click.echo("  \u2713 Updated CHANGELOG.md")

    # Git tag
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, cwd=repo_root,
    )
    subprocess.run(
        ["git", "commit", "-m", f"release: v{new_version}"],
        capture_output=True, cwd=repo_root,
    )
    subprocess.run(
        ["git", "tag", "-a", f"v{new_version}", "-m", f"Release v{new_version}"],
        capture_output=True, cwd=repo_root,
    )
    click.echo(f"  \u2713 Git tag: v{new_version}")
    click.echo("\nPush with: git push && git push --tags")
    click.echo("Then publish: twine upload dist/*  (after building)")


# ─── nomad dev deps ──────────────────────────────────────────────────

@dev.command("deps")
@click.argument("module_type", required=False)
@click.argument("name", required=False)
@click.pass_context
def deps(ctx, module_type, name):
    """Show module dependency graph.

    Usage: nomad dev deps [type] [name]

    Shows upstream dependencies, downstream dependents,
    and related modules.
    """
    repo_root = _get_repo_root()

    if not module_type or not name:
        click.echo("Usage: nomad dev deps <type> <name>")
        click.echo("Example: nomad dev deps collector disk")
        return

    # Find the module file
    search_dirs = {
        "collector": "nomad/collectors",
        "metric": "nomad/dynamics",
        "analysis": "nomad/analysis",
        "alert": "nomad/alerts",
        "insight": "nomad/insights",
    }

    source_dir = search_dirs.get(module_type)
    if not source_dir:
        click.echo(f"Unknown module type: {module_type}")
        return

    module_path = repo_root / source_dir / f"{name}.py"
    if not module_path.exists():
        click.echo(f"Module not found: {module_path}")
        return

    content = module_path.read_text()

    # Parse imports for upstream dependencies
    click.echo(f"\n{name.upper()} ({module_type}) Dependencies")
    click.echo("=" * 40)

    click.echo("\nUpstream (depends on):")
    imports = re.findall(r'from\s+(nomad\.[.\w]+)\s+import', content)
    imports += re.findall(r'import\s+(nomad\.[.\w]+)', content)
    for imp in sorted(set(imports)):
        click.echo(f"  <- {imp}")

    if not imports:
        click.echo("  (no internal dependencies)")

    # Search for downstream (who imports this module)
    click.echo("\nDownstream (depends on this):")
    module_import = f"nomad.{source_dir.replace('nomad/', '').replace('/', '.')}.{name}"
    downstream = []
    for py_file in (repo_root / "nomad").rglob("*.py"):
        if py_file == module_path or "__pycache__" in str(py_file):
            continue
        try:
            file_content = py_file.read_text()
            if name in file_content and (
                f"from .{name}" in file_content
                or f"import {name}" in file_content
                or f".{name}." in file_content
                or f"'{name}'" in file_content
            ):
                rel = py_file.relative_to(repo_root)
                downstream.append(str(rel))
        except Exception:
            continue

    for dep in sorted(downstream):
        click.echo(f"  -> {dep}")

    if not downstream:
        click.echo("  (no downstream dependents found)")

    # Related modules (same directory)
    click.echo("\nRelated modules:")
    siblings = [
        f.stem for f in (repo_root / source_dir).glob("*.py")
        if f.stem not in ("__init__", "base", "registry", "engine",
                          "formatters", "cli_commands", name)
        and not f.stem.startswith("_")
    ]
    for sib in sorted(siblings):
        click.echo(f"  ~ {sib}")


# We need this import for the pre-commit hook script
import importlib  # noqa: E402
import re  # noqa: E402
