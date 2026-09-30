# ── CLI commands for `nomad dyn` ─────────────────────────────────────
# Add this block to nomad/cli.py before the COMMUNITY COMMANDS section.
#
# Paste after the last insights subcommand and before:
#   # COMMUNITY COMMANDS
#
# =============================================================================
# DYNAMICS COMMANDS
# =============================================================================
@cli.group()
def dyn():
    """System dynamics analysis — ecological and economic metrics.

    Quantitative frameworks from community ecology and economics
    applied to research computing usage patterns.

    \b
    Commands:
      nomad dyn               Full dynamics summary
      nomad dyn diversity     Workload diversity indices
      nomad dyn niche         Resource usage overlap between groups
      nomad dyn capacity      Multi-dimensional carrying capacity
      nomad dyn resilience    Recovery time after disturbance events
      nomad dyn externality   Inter-group impact quantification
    """
    pass


@dyn.command('summary')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_summary(ctx, db_path, hours, cluster_name, output_json):
    """Full dynamics summary combining all metrics.

    Produces a holistic assessment of workload diversity, niche overlap,
    carrying capacity, resilience, and inter-group externalities.
    """
    from nomad.dynamics.engine import DynamicsEngine

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))
    if cluster_name is None:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(config)

    engine = DynamicsEngine(db_path, hours=hours, cluster_name=cluster_name)

    if output_json:
        click.echo(engine.to_json())
    else:
        click.echo(engine.full_summary())


@dyn.command('diversity')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--by', 'dimension', type=click.Choice(['group', 'partition', 'user']),
              default='group', help='Dimension to measure diversity over')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_diversity(ctx, db_path, hours, dimension, output_json):
    """Workload diversity indices (Shannon, Simpson).

    \b
    Measures how evenly workload is distributed across groups,
    partitions, or users. Tracks trends over time and warns
    when diversity drops below safe levels.

    \b
    Examples:
      nomad dyn diversity                # diversity by group
      nomad dyn diversity --by partition  # diversity by partition
      nomad dyn diversity --hours 720    # 30-day window
    """
    from nomad.dynamics.engine import DynamicsEngine
    from nomad.dynamics.formatters import format_diversity_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    engine = DynamicsEngine(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_diversity_json(engine.diversity), indent=2))
    else:
        click.echo(engine.diversity_report(dimension=dimension))


@dyn.command('niche')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--threshold', type=float, default=0.6, help='Overlap threshold for flagging')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_niche(ctx, db_path, hours, threshold, output_json):
    """Resource usage overlap between user communities.

    \b
    Computes pairwise niche overlap (Pianka's index) between groups.
    Flags pairs of groups whose average requests are similar. Similar
    requests are not the same as running at the same time. Needs each job
    placed in one group (see docs/dynamics.md).

    \b
    Examples:
      nomad dyn niche                    # default threshold 0.6
      nomad dyn niche --threshold 0.5    # more sensitive
      nomad dyn niche --json             # JSON output for Console
    """
    from nomad.dynamics.niche import compute_niche_overlap
    from nomad.dynamics.formatters import format_niche_cli, format_niche_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_niche_overlap(db_path, hours=hours, overlap_threshold=threshold)

    if output_json:
        import json
        click.echo(json.dumps(format_niche_json(result), indent=2))
    else:
        click.echo(format_niche_cli(result))


@dyn.command('capacity')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_capacity(ctx, db_path, hours, output_json):
    """Multi-dimensional carrying capacity utilization.

    \b
    Analyzes utilization across CPU, memory, GPU, I/O, and scheduler
    queue. Identifies the binding constraint (Liebig's law of the
    minimum) and projects time to saturation.

    \b
    Examples:
      nomad dyn capacity            # current capacity report
      nomad dyn capacity --json     # JSON output for Console
    """
    from nomad.dynamics.capacity import compute_capacity
    from nomad.dynamics.formatters import format_capacity_cli, format_capacity_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_capacity(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_capacity_json(result), indent=2))
    else:
        click.echo(format_capacity_cli(result))


@dyn.command('resilience')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=720, help='Analysis window (hours, default 30 days)')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_resilience(ctx, db_path, hours, output_json):
    """Recovery time after disturbance events.

    \b
    Detects disturbance events (node failures, job failure spikes)
    and computes mean/median recovery time. Tracks whether the
    cluster is becoming more or less resilient over time.

    \b
    Requires historical data — longer time windows produce better
    resilience estimates.

    \b
    Examples:
      nomad dyn resilience               # 30-day window
      nomad dyn resilience --hours 2160  # 90-day window
    """
    from nomad.dynamics.resilience import compute_resilience
    from nomad.dynamics.formatters import format_resilience_cli, format_resilience_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_resilience(db_path, hours=hours)

    if output_json:
        import json
        click.echo(json.dumps(format_resilience_json(result), indent=2))
    else:
        click.echo(format_resilience_cli(result))


@dyn.command('externality')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, help='Analysis window (hours)')
@click.option('--threshold', type=float, default=0.3, help='Minimum correlation to report')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def dyn_externality(ctx, db_path, hours, threshold, output_json):
    """Inter-user/group impact quantification.

    \b
    Correlates each group's resource-intensive behavior with other
    groups' job failure rates. Answers: "whose jobs are hurting
    other people's jobs?"

    \b
    Examples:
      nomad dyn externality                  # default threshold
      nomad dyn externality --threshold 0.2  # more sensitive
    """
    from nomad.dynamics.externality import compute_externalities
    from nomad.dynamics.formatters import format_externality_cli, format_externality_json

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))

    result = compute_externalities(db_path, hours=hours, correlation_threshold=threshold)

    if output_json:
        import json
        click.echo(json.dumps(format_externality_json(result), indent=2))
    else:
        click.echo(format_externality_cli(result))
