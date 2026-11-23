#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Wire nomad edu subcommands into cli.py.

Adds:
    nomad edu explain <job_id>  [--json] [--no-progress]
    nomad edu trajectory <user> [--days N] [--json]
    nomad edu report <group>    [--days N] [--json]

The CLI code is kept in patches/edu_cli_commands.py for proper
syntax highlighting and linting support.

Usage:
    python3 wire_edu_cli.py nomad/cli.py
"""
import sys
from pathlib import Path


def load_cli_block() -> str:
    """
    Load the edu CLI commands from the separate source file.
    
    This approach ensures the CLI code gets proper syntax highlighting
    and linting in editors, addressing reviewer feedback about embedded
    code strings.
    """
    # Try multiple locations
    locations = [
        Path(__file__).parent / 'patches' / 'edu_cli_commands.py',
        Path(__file__).parent / 'edu_cli_commands.py',
        Path('patches/edu_cli_commands.py'),
    ]
    
    for path in locations:
        if path.exists():
            content = path.read_text()
            # Skip the module docstring and imports - just get the code
            # Find where the actual commands start
            marker = "# ============"
            if marker in content:
                idx = content.index(marker)
                return '\n' + content[idx:]
            return '\n' + content
    
    # Fallback: if file not found, use inline definition
    # This ensures the script still works standalone
    return '''

# =============================================================================
# EDU COMMANDS
# =============================================================================

@cli.group()
def edu():
    """NØMAÐ Edu — Educational analytics for HPC.

    Measures the development of computational proficiency over time
    by analyzing per-job behavioral fingerprints.
    """
    pass


@edu.command('explain')
@click.argument('job_id')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.option('--no-progress', is_flag=True, help='Skip progress comparison')
@click.pass_context
def edu_explain(ctx, job_id, db_path, output_json, no_progress):
    """Explain a job in plain language with proficiency scores."""
    from nomad.edu.explain import explain_job

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    result = explain_job(
        job_id=job_id,
        db_path=db_path,
        show_progress=not no_progress,
        output_format='json' if output_json else 'terminal',
    )

    if result is None:
        click.echo(f"Job {job_id} not found in database.", err=True)
        raise SystemExit(1)

    click.echo(result)


@edu.command('trajectory')
@click.argument('username')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=90, help='Lookback period in days (default: 90)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_trajectory(ctx, username, db_path, days, output_json):
    """Show a user's proficiency development over time."""
    from nomad.edu.progress import user_trajectory, format_trajectory
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    traj = user_trajectory(db_path, username, days)

    if traj is None:
        click.echo(f"Not enough data for {username}.", err=True)
        raise SystemExit(1)

    if output_json:
        result = {
            "username": traj.username,
            "total_jobs": traj.total_jobs,
            "overall_improvement": traj.overall_improvement,
            "current_scores": traj.current_scores,
        }
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(format_trajectory(traj))


@edu.command('report')
@click.argument('group_name')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--days', default=90, help='Lookback period in days (default: 90)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def edu_report(ctx, group_name, db_path, days, output_json):
    """Generate a proficiency report for a course or lab group."""
    from nomad.edu.progress import group_summary, format_group_summary
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    gs = group_summary(db_path, group_name, days)

    if gs is None:
        click.echo(f"No data found for group '{group_name}'.", err=True)
        raise SystemExit(1)

    if output_json:
        result = {
            "group_name": gs.group_name,
            "member_count": gs.member_count,
            "improvement_rate": gs.improvement_rate,
        }
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(format_group_summary(gs))
'''


def wire_cli(cli_path: Path) -> bool:
    """Insert edu commands into cli.py."""
    content = cli_path.read_text()

    if "def edu():" in content:
        print("  = cli.py already has edu commands")
        return True

    # Load the CLI block from separate file
    cli_block = load_cli_block()

    # Find insertion point: before main() 
    marker = "def main() -> None:"
    if marker not in content:
        marker = "def main():"

    if marker not in content:
        print("  ! Could not find main() in cli.py")
        return False

    idx = content.index(marker)
    content = content[:idx] + cli_block + "\n\n" + content[idx:]

    cli_path.write_text(content)
    print("  + Added edu commands to cli.py")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 wire_edu_cli.py nomad/cli.py")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: {path} not found")
        sys.exit(1)

    print("\nWiring NØMAÐ Edu CLI")
    print("=" * 30)
    wire_cli(path)
    print("\nDone! Test with:")
    print("  nomad edu --help")
    print("  nomad edu explain <job_id>")


if __name__ == '__main__':
    main()
