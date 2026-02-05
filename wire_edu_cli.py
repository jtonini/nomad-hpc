#!/usr/bin/env python3
"""
Wire nomade edu subcommands into cli.py.

Adds:
    nomade edu explain <job_id>  [--json] [--no-progress]
    nomade edu trajectory <user> [--days N] [--json]
    nomade edu report <group>    [--days N] [--json]

Usage:
    python3 wire_edu_cli.py nomade/cli.py
"""
import sys
from pathlib import Path

CLI_BLOCK = '''

# =============================================================================
# EDU COMMANDS
# =============================================================================

@cli.group()
def edu():
    """NØMADE Edu — Educational analytics for HPC.

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
    """Explain a job in plain language with proficiency scores.

    Analyzes a completed job across five dimensions of computational
    proficiency: CPU efficiency, memory sizing, time estimation,
    I/O awareness, and GPU utilization.

    Examples:
        nomade edu explain 12345
        nomade edu explain 12345 --json
        nomade edu explain 12345 --no-progress
    """
    from nomade.edu.explain import explain_job

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = _resolve_db_path(config)

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
    """Show a user's proficiency development over time.

    Tracks how a student or researcher's HPC skills evolve across
    their job submissions, highlighting areas of improvement and
    dimensions that need attention.

    Examples:
        nomade edu trajectory student01
        nomade edu trajectory student01 --days 30
    """
    from nomade.edu.progress import user_trajectory, format_trajectory
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = _resolve_db_path(config)

    traj = user_trajectory(db_path, username, days)

    if traj is None:
        click.echo(f"Not enough data for {username} (need at least 3 completed jobs).", err=True)
        raise SystemExit(1)

    if output_json:
        result = {
            "username": traj.username,
            "total_jobs": traj.total_jobs,
            "date_range": traj.date_range,
            "overall_improvement": traj.overall_improvement,
            "summary": traj.summary,
            "current_scores": traj.current_scores,
            "improvement": traj.improvement,
            "windows": [
                {"start": w.start, "end": w.end, "job_count": w.job_count,
                 "scores": w.scores, "overall": w.overall}
                for w in traj.windows
            ],
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
    """Generate a proficiency report for a course or lab group.

    Aggregates per-student proficiency data to produce insights like
    "15/20 students improved memory efficiency over the semester."

    The group_name maps to a Linux group (from SLURM accounting or
    LDAP). Configure group filters in nomade.toml.

    Examples:
        nomade edu report bio301
        nomade edu report bio301 --days 120
        nomade edu report physics-lab --json
    """
    from nomade.edu.progress import group_summary, format_group_summary
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = _resolve_db_path(config)

    gs = group_summary(db_path, group_name, days)

    if gs is None:
        click.echo(f"No data found for group '{group_name}'.", err=True)
        click.echo("Ensure group membership data has been collected:")
        click.echo("  nomade collect -C groups --once")
        raise SystemExit(1)

    if output_json:
        result = {
            "group_name": gs.group_name,
            "member_count": gs.member_count,
            "total_jobs": gs.total_jobs,
            "date_range": gs.date_range,
            "improvement_rate": gs.improvement_rate,
            "avg_overall": gs.avg_overall,
            "avg_improvement": gs.avg_improvement,
            "users_improving": gs.users_improving,
            "users_stable": gs.users_stable,
            "users_declining": gs.users_declining,
            "dimension_avgs": gs.dimension_avgs,
            "dimension_improvements": gs.dimension_improvements,
            "weakest_dimension": gs.weakest_dimension,
            "strongest_dimension": gs.strongest_dimension,
            "users": [
                {"username": t.username, "total_jobs": t.total_jobs,
                 "overall_improvement": t.overall_improvement,
                 "current_scores": t.current_scores}
                for t in gs.users
            ],
        }
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(format_group_summary(gs))
'''


def wire_cli(cli_path):
    """Insert edu commands into cli.py."""
    content = open(cli_path).read()

    if "def edu():" in content:
        print("  = cli.py already has edu commands")
        return True

    # Find insertion point: before main() or before community commands
    marker = "def main() -> None:"
    if marker not in content:
        marker = "def main():"

    if marker not in content:
        print("  ! Could not find main() in cli.py")
        return False

    idx = content.index(marker)
    content = content[:idx] + CLI_BLOCK + "\n\n" + content[idx:]

    open(cli_path, 'w').write(content)
    print("  + Added edu commands to cli.py")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 wire_edu_cli.py nomade/cli.py")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: {path} not found")
        sys.exit(1)

    print("\nWiring NØMADE Edu CLI")
    print("=" * 30)
    wire_cli(path)
    print("\nDone! Test with:")
    print("  nomade edu --help")
    print("  nomade edu explain <job_id>")


if __name__ == '__main__':
    main()
