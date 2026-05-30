# ── CLI commands for `nomad energy` ──────────────────────────────────
# Paste this block into nomad/cli.py alongside the other command groups
# (e.g. immediately after the `dyn` group block). It relies on names already
# defined in cli.py: `cli`, `get_db_path`, and `click`. `resolve_cluster_name`
# and `EnergyEngine` are imported lazily inside the functions, mirroring how
# the dynamics commands are written.
#
# Commands wired here: energy (bare -> summary), report, user, carbon.
# Held for later (need engine support not yet built): forecast, predict, compare.
# =============================================================================
# ENERGY COMMANDS
# =============================================================================

def _energy_engine(ctx, db_path, hours, cluster_name, mode, region):
    """Build an EnergyEngine, resolving db/cluster from config like `dyn`.

    Includes the cluster-match safety net: if the resolved cluster name
    matches no jobs (common on the demo database, whose cluster is
    'demo-cluster'), fall back to all clusters with a one-line notice rather
    than silently reporting zeros.
    """
    from nomad.energy.engine import EnergyEngine

    config = ctx.obj.get('config', {})
    if db_path is None:
        db_path = str(get_db_path(config))
    if cluster_name is None:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(config)

    engine = EnergyEngine(db_path, hours=hours, cluster_name=cluster_name,
                          config=config, region=region, mode=mode)
    if engine.snapshot().n_jobs == 0:
        engine_all = EnergyEngine(db_path, hours=hours, cluster_name=None,
                                  config=config, region=region, mode=mode)
        if engine_all.snapshot().n_jobs > 0:
            click.echo(f"  (no jobs found for cluster '{cluster_name}'; "
                       f"showing all clusters)", err=True)
            return engine_all
    return engine


def _mode_option(fn):
    return click.option('--mode', type=click.Choice(['physical', 'allocation']),
                        default='physical', show_default=True,
                        help="Waste valuation: physical (carbon basis) or "
                             "allocation (capacity reserved).")(fn)


@cli.group(invoke_without_command=True)
@click.pass_context
def energy(ctx):
    """Energy consumption, waste, and carbon-footprint monitoring.

    \b
    Commands:
      nomad energy            Cluster energy + sustainability summary
      nomad energy report     Breakdown by partition / group / user
      nomad energy user NAME  Per-user energy profile and recommendations
      nomad energy carbon     CO2 view with configurable carbon intensity

    Running `nomad energy` with no subcommand shows the summary.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(energy_summary)


@energy.command('summary')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, show_default=True,
              help='Analysis window (hours)')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None,
              help='Carbon region override (e.g. eGRID subregion SRVC)')
@_mode_option
@click.option('--explain', is_flag=True, help='Show the watts->CO2 chain and provenance')
@click.option('--json', 'output_json', is_flag=True, help='JSON output')
@click.pass_context
def energy_summary(ctx, db_path, hours, cluster_name, region, mode, explain, output_json):
    """Cluster-wide energy and sustainability summary."""
    engine = _energy_engine(ctx, db_path, hours, cluster_name, mode, region)
    if output_json:
        click.echo(engine.to_json())
    else:
        click.echo(engine.full_summary(explain=explain))


@energy.command('report')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, show_default=True,
              help='Analysis window (hours)')
@click.option('--by', 'group_by', type=click.Choice(['partition', 'group', 'user']),
              default='partition', show_default=True, help='Breakdown dimension')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None, help='Carbon region override')
@_mode_option
@click.pass_context
def energy_report(ctx, db_path, hours, group_by, cluster_name, region, mode):
    """Detailed energy report broken down by partition, group, or user.

    Ranked by recoverable energy -- the report admins send to leadership and
    the data behind the per-group figures.
    """
    engine = _energy_engine(ctx, db_path, hours, cluster_name, mode, region)
    click.echo(engine.report(group_by=group_by))


@energy.command('user')
@click.argument('username')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, show_default=True,
              help='Analysis window (hours)')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None, help='Carbon region override')
@_mode_option
@click.option('--explain', is_flag=True, help='Show provenance')
@click.pass_context
def energy_user(ctx, username, db_path, hours, cluster_name, region, mode, explain):
    """Per-user energy profile: footprint, efficiency score, recommendations.

    Output is framed as opportunities, not failures.
    """
    engine = _energy_engine(ctx, db_path, hours, cluster_name, mode, region)
    click.echo(engine.user_profile(username, explain=explain))


@energy.command('carbon')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--hours', type=int, default=168, show_default=True,
              help='Analysis window (hours)')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None,
              help='Carbon region override (e.g. SRVC, CAMX, or country code)')
@_mode_option
@click.option('--explain', is_flag=True, help='Show the carbon-intensity source and chain')
@click.pass_context
def energy_carbon(ctx, db_path, hours, cluster_name, region, mode, explain):
    """CO2 view with configurable regional carbon intensity.

    Use --region to compare the same workload across grids
    (e.g. --region CAMX vs --region SRVC vs --region IN).
    """
    engine = _energy_engine(ctx, db_path, hours, cluster_name, mode, region)
    click.echo(engine.carbon_report(explain=explain))
@energy.command('compare')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--split', default=None,
              help='Split date (YYYY-MM-DD) for before vs after. '
                   'Default: midpoint of the data span.')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None, help='Carbon region override')
@_mode_option
@click.pass_context
def energy_compare(ctx, db_path, split, cluster_name, region, mode):
    """Compare energy efficiency before vs after a point in time.

    Reads two periods out of one timeline. With an intervention dataset,
    omit --split to split at the data midpoint (the intervention point), or
    pass --split YYYY-MM-DD explicitly. Reports the change in recoverable
    energy, the efficiency shift, and the CO2 avoided.
    """
    from datetime import datetime
    split_dt = datetime.fromisoformat(split) if split else None
    # Large check-window so the cluster-match guard sees the full data span.
    engine = _energy_engine(ctx, db_path, 24 * 90, cluster_name, mode, region)
    click.echo(engine.compare(split=split_dt))
@energy.command('forecast')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--horizon', type=click.Choice(['30d', 'quarter', 'semester', 'year']),
              default='semester', show_default=True, help='Projection horizon')
@click.option('--buckets', type=int, default=8, show_default=True,
              help='Number of time buckets to fit the trend over')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.option('--region', default=None, help='Carbon region override')
@_mode_option
@click.pass_context
def energy_forecast(ctx, db_path, horizon, buckets, cluster_name, region, mode):
    """Project consumed and recoverable energy forward via trend analysis.

    Aggregate linear extrapolation over the data span -- shows where
    consumption and waste are heading side by side. This is a trend, not a
    per-job prediction (see future `nomad energy predict`).
    """
    engine = _energy_engine(ctx, db_path, 24 * 365, cluster_name, mode, region)
    click.echo(engine.forecast_report(horizon=horizon))
@energy.command('predict')
@click.option('--db', 'db_path', type=click.Path(exists=True), help='Database path')
@click.option('--method', type=click.Choice(['tessera', 'baseline']), default='tessera',
              show_default=True,
              help='tessera = full topology model (needs nomad-hpc[predict]); '
                   'baseline = logistic regression, always available.')
@click.option('--top', type=int, default=15, show_default=True,
              help='Show the N highest-risk submissions.')
@click.option('--threshold', type=float, default=0.6, show_default=True,
              help='Unused-walltime fraction that counts as high-waste (label).')
@click.option('--cluster', 'cluster_name', default=None, help='Cluster name')
@click.pass_context
def energy_predict(ctx, db_path, method, top, threshold, cluster_name):
    """Predict which job submissions will waste energy, before they run.

    Classifies jobs from request-time features only (requested cores, GPUs,
    walltime, memory, partition, time-of-day) -- no runtime leakage -- so the
    risk score is actionable at submission time. Use it to target right-sizing
    advice at the submissions most likely to over-reserve.

    Output is a risk RANKING, not a calibrated probability.
    """
    from nomad.energy.tessera_bridge import predict_energy_waste
    from nomad.energy import formatters as fmt

    config = ctx.obj.get('config', {})
    # Resolve db/cluster exactly like the other energy commands:
    # get_db_path is module-level in cli.py; resolve_cluster_name is in config.
    if db_path is None:
        db_path = str(get_db_path(config))
    if cluster_name is None:
        from nomad.config import resolve_cluster_name
        cluster_name = resolve_cluster_name(config)

    def _run(cluster):
        return predict_energy_waste(
            db_path, config=config, cluster_name=cluster,
            method=method, waste_threshold=threshold,
        )

    try:
        result = _run(cluster_name)
        # cluster-match safety net (same as _energy_engine): if the resolved
        # cluster matches nothing, fall back to all clusters with a notice.
        if result.n_jobs == 0 and cluster_name:
            alt = _run(None)
            if alt.n_jobs > 0:
                click.echo(f"  (no jobs for cluster '{cluster_name}'; "
                           "showing all clusters)", err=True)
                result = alt
    except ImportError as exc:
        raise click.ClickException(str(exc))

    if result.n_jobs == 0:
        click.echo("No completed jobs found to predict on.")
        return
    click.echo(fmt.format_prediction_cli(result, top=top))
