"""CLI commands for nomad ref — reference and code navigation.

This file contains the Click commands to be integrated into cli.py.
The integration script inserts these commands before COMMUNITY COMMANDS.

Design: 'nomad ref' is a Click command (not a group) that accepts
a variadic list of topic words. This avoids the subcommand collision
problem where 'nomad ref dyn diversity' would try to route 'dyn' as
a subcommand.

Usage:
    nomad ref                          # browse all topics
    nomad ref alerts                   # alert system overview
    nomad ref dyn diversity            # dynamics diversity command
    nomad ref collectors disk          # disk collector details
    nomad ref config                   # configuration reference
    nomad ref search regime divergence # search all documentation
    nomad ref tessera                  # TESSERA methodology
"""

# =============================================================================
# REFERENCE COMMANDS
# =============================================================================

@cli.command('ref')
@click.argument('topic_parts', nargs=-1)
def ref(topic_parts):
    """Built-in reference and documentation.

    Look up any NØMAÐ command, module, configuration option, or concept.

    \b
    Examples:
      nomad ref                          Browse all topics
      nomad ref alerts                   Alert system overview
      nomad ref dyn diversity            Dynamics diversity command
      nomad ref collectors disk          Disk collector details
      nomad ref config                   Configuration reference
      nomad ref search regime divergence Search all documentation
      nomad ref tessera                  TESSERA methodology
      nomad ref concepts governance      Ostrom governance framework
    """
    from nomad.reference import KnowledgeBase, ReferenceFormatter

    kb = KnowledgeBase()
    fmt = ReferenceFormatter()

    if not topic_parts:
        # No arguments — show index
        categories = {}
        for entry in kb.list_topics():
            cat = entry.category or "other"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(entry)
        click.echo(fmt.format_index(categories))
        return

    # Handle 'search' as special first word
    if topic_parts[0] == "search":
        query = " ".join(topic_parts[1:]) if len(topic_parts) > 1 else ""
        if not query:
            click.echo("\nUsage: nomad ref search <query>")
            click.echo("Example: nomad ref search regime divergence\n")
            return
        results = kb.search(query, max_results=10)
        click.echo(fmt.format_search_results(query, results))
        return

    # Build the topic key from parts
    # Try increasingly specific keys: "dyn.diversity", then "dyn", etc.
    # For "dyn diversity" -> try "dyn.diversity" first, then "dyn"
    # For "collectors disk" -> try "collectors.disk" first
    # For "concepts regime_divergence" -> try "concepts.regime_divergence"

    # Strategy: join all parts with dots and try exact match first
    full_key = ".".join(topic_parts)
    entry = kb.get(full_key)
    if entry:
        click.echo(fmt.format_entry(entry))
        children = kb.get_children(full_key)
        if children:
            click.echo(fmt.format_topic_list(children, heading="Subtopics"))
        return

    # Try with underscores instead of dots for multi-word concepts
    # e.g., "concepts regime divergence" -> "concepts.regime_divergence"
    if len(topic_parts) >= 2:
        prefix = topic_parts[0]
        rest = "_".join(topic_parts[1:])
        underscore_key = f"{prefix}.{rest}"
        entry = kb.get(underscore_key)
        if entry:
            click.echo(fmt.format_entry(entry))
            return

    # Try just the first word as the key
    first = topic_parts[0]
    entry = kb.get(first)
    if entry:
        click.echo(fmt.format_entry(entry))
        children = kb.get_children(first)
        if children:
            click.echo(fmt.format_topic_list(children, heading="Subtopics"))

        # If there were additional words, they might be a subtopic we missed
        if len(topic_parts) > 1:
            sub_key = first + "." + ".".join(topic_parts[1:])
            sub_entry = kb.get(sub_key)
            if sub_entry and sub_entry.key != entry.key:
                click.echo("\n" + "=" * 40 + "\n")
                click.echo(fmt.format_entry(sub_entry))
        return

    # Nothing found by key — fall back to search
    query = " ".join(topic_parts)
    results = kb.search(query, max_results=5)
    if results:
        click.echo(fmt.format_search_results(query, results))
    else:
        click.echo(f"\nNo reference entry found for '{query}'.")
        click.echo("Use 'nomad ref' to browse topics or 'nomad ref search <query>' to search.\n")
