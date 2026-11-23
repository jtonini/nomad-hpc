# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Edu CLI Commands

This file contains the CLI commands for the edu module.
It is loaded by wire_edu_cli.py and inserted into nomad/cli.py.

Having this as a separate .py file ensures:
- Proper syntax highlighting in editors
- Linting and type checking
- Easy testing and modification
"""

# =============================================================================
# EDU COMMANDS
# =============================================================================

@cli.group()
def edu():
    """NØMAD Edu — Educational analytics for HPC.

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
        nomad edu explain 12345
        nomad edu explain 12345 --json
        nomad edu explain 12345 --no-progress
    """
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
    """Show a user's proficiency development over time.

    Tracks how a student or researcher's HPC skills evolve across
    their job submissions, highlighting areas of improvement and
    dimensions that need attention.

    Examples:
        nomad edu trajectory student01
        nomad edu trajectory student01 --days 30
    """
    from nomad.edu.progress import user_trajectory, format_trajectory
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

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
    LDAP). Configure group filters in nomad.toml.

    Examples:
        nomad edu report bio301
        nomad edu report bio301 --days 120
        nomad edu report physics-lab --json
    """
    from nomad.edu.progress import group_summary, format_group_summary
    import json

    if not db_path:
        config = ctx.obj.get('config', {}) if ctx.obj else {}
        db_path = get_db_path(config)

    gs = group_summary(db_path, group_name, days)

    if gs is None:
        click.echo(f"No data found for group '{group_name}'.", err=True)
        click.echo("Ensure group membership data has been collected:")
        click.echo("  nomad collect -C groups --once")
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
