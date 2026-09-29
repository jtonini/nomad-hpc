"""CLI commands for nomad issue.

Provides an interactive issue reporting flow with:
- Category selection (bug, feature, question)
- Component selection
- Structured input prompts
- Duplicate detection before submission
- Direct API submission (with token) or browser fallback
- Email fallback for users without GitHub accounts
"""

from __future__ import annotations

import webbrowser

import click

from .collector import IssueCollector
from .formatter import CATEGORIES, COMPONENTS, IssueFormatter
from .github_api import GitHubClient
from nomad.config import read_secret, support_settings


def _load_issue_config(ctx: click.Context) -> dict:
    """Load issue_reporting config from the NØMAÐ config."""
    try:
        config = ctx.obj.get("config", {}) if ctx.obj else {}
        return config.get("issue_reporting", {})
    except Exception:
        return {}


def _get_db_path(ctx: click.Context) -> str:
    """Get the database path from context."""
    try:
        if ctx.obj:
            return str(ctx.obj.get("db_path", ""))
    except Exception:
        pass
    return ""


def _display_duplicates(duplicates: list, client: GitHubClient) -> int | None:
    """Display potential duplicates and let user choose.

    Returns issue number if user wants to comment on existing issue,
    None if they want to create a new one.
    """
    if not duplicates:
        return None

    click.echo()
    click.secho(
        "  Potentially related open issues found:",
        fg="yellow",
        bold=True,
    )
    click.echo()

    for i, dup in enumerate(duplicates, 1):
        labels = ", ".join(dup.labels[:3]) if dup.labels else ""
        label_str = f"  [{labels}]" if labels else ""
        click.echo(f"  {i}. #{dup.number}: {dup.title}{label_str}")
        click.echo(f"     {dup.url}")
        click.echo(f"     Opened {dup.created_at} · {dup.comments} comments")
        click.echo()

    click.echo("  0. None of these — create a new issue")
    click.echo()

    choice = click.prompt(
        "  Select an issue to comment on (or 0 for new)",
        type=int,
        default=0,
    )

    if 1 <= choice <= len(duplicates):
        return duplicates[choice - 1].number
    return None


@click.group()
def issue():
    """Report issues, request features, and ask questions.

    Submit directly to the NØMAÐ GitHub repository with
    auto-populated system information. When a GitHub token
    is configured in nomad.toml, issues are submitted via API.
    Otherwise, opens a pre-filled GitHub issue form in your browser.
    """
    pass


@issue.command("report")
@click.option(
    "--category", "-c",
    type=click.Choice(CATEGORIES, case_sensitive=False),
    help="Issue category (prompted if not given)",
)
@click.option(
    "--component", "-m",
    type=click.Choice(COMPONENTS, case_sensitive=False),
    help="Affected component (prompted if not given)",
)
@click.option(
    "--title", "-t",
    help="Issue title (prompted if not given)",
)
@click.option(
    "--db", "db_path",
    type=click.Path(exists=True),
    help="Database path for system info collection",
)
@click.option(
    "--no-duplicate-check",
    is_flag=True,
    help="Skip duplicate issue search",
)
@click.option(
    "--email",
    is_flag=True,
    help="Send via email instead of GitHub",
)
@click.option(
    "--json", "output_json",
    is_flag=True,
    help="Output formatted issue as JSON (no submission)",
)
@click.pass_context
def report(ctx, category, component, title, db_path, no_duplicate_check, email, output_json):
    """Submit a bug report, feature request, or question.

    Walks you through a structured form, auto-collects system
    information, checks for duplicates, and submits to GitHub.

    \b
    Examples:
        nomad issue report
        nomad issue report -c bug -m alerts
        nomad issue report --email
        nomad issue report --json
    """
    config = ctx.obj.get("config", {}) if ctx.obj else {}
    issue_cfg = config.get("issue_reporting", {})

    # Check if issue reporting is explicitly disabled
    if not issue_cfg.get("enabled", True):
        click.secho("Issue reporting is disabled in nomad.toml.", fg="red")
        return

    # Resolve DB path
    if not db_path:
        db_path = _get_db_path(ctx)

    click.echo()
    click.secho("  NØMAÐ Issue Reporter", fg="cyan", bold=True)
    click.secho("  ═══════════════════", fg="cyan")
    click.echo()

    # 1. Category
    if not category:
        click.echo("  Category:")
        for i, cat in enumerate(CATEGORIES, 1):
            desc = {
                "bug": "Something isn't working correctly",
                "feature": "Suggest a new capability",
                "question": "Ask about usage or behavior",
            }
            click.echo(f"    {i}. {cat:10s} — {desc[cat]}")
        click.echo()
        choice = click.prompt("  Select category", type=int, default=1)
        category = CATEGORIES[min(max(choice, 1), len(CATEGORIES)) - 1]

    click.echo(f"  Category: {category}")
    click.echo()

    # 2. Component
    if not component:
        click.echo("  Affected component:")
        for i, comp in enumerate(COMPONENTS, 1):
            click.echo(f"    {i:2d}. {comp}")
        click.echo()
        choice = click.prompt("  Select component", type=int, default=10)
        component = COMPONENTS[min(max(choice, 1), len(COMPONENTS)) - 1]

    click.echo(f"  Component: {component}")
    click.echo()

    # 3. Title
    if not title:
        title = click.prompt("  Title (brief description)")

    click.echo()

    # 4. Category-specific fields
    if category == "bug":
        description = click.prompt("  Describe the problem")
        steps = click.prompt("  Steps to reproduce")
        expected = click.prompt("  Expected behavior")
        actual = click.prompt("  Actual behavior")
        extra = {
            "description": description,
            "steps": steps,
            "expected": expected,
            "actual": actual,
        }
    elif category == "feature":
        problem = click.prompt("  What problem does this solve?")
        solution = click.prompt("  Proposed solution (optional)", default="")
        alternatives = click.prompt(
            "  Alternatives considered (optional)", default=""
        )
        extra = {
            "problem": problem,
            "solution": solution,
            "alternatives": alternatives,
        }
    else:  # question
        question = click.prompt("  Your question")
        tried = click.prompt("  What I've already tried (optional)", default="")
        extra = {
            "question": question,
            "tried": tried,
        }

    # 5. Collect system info
    click.echo()
    click.secho("  Collecting system information...", fg="cyan")
    collector = IssueCollector(
        db_path=db_path, config=config, source="cli"
    )
    sys_info = collector.collect()

    # 6. Format the issue
    formatter = IssueFormatter(system_info=sys_info)
    data = {
        "category": category,
        "title": title,
        "component": component,
        **extra,
    }
    formatted_title, body = formatter.format_from_dict(data)

    # JSON output mode — just print and exit
    if output_json:
        import json
        output = {
            "title": formatted_title,
            "body": body,
            "category": category,
            "component": component,
            "system_info": sys_info.to_dict(),
        }
        click.echo(json.dumps(output, indent=2))
        return

    # 7. Duplicate check
    token = read_secret(issue_cfg, "github_token")
    client = GitHubClient(token=token)

    if not no_duplicate_check:
        click.secho("  Checking for similar issues...", fg="cyan")
        keywords = f"{component} {title}"
        duplicates = client.search_duplicates(keywords)
        existing = _display_duplicates(duplicates, client)

        if existing is not None:
            comment = click.prompt(
                "  Add a comment to this issue (describe your case)"
            )
            comment_body = f"{comment}\n\n{sys_info.to_markdown()}"
            result = client.add_comment(existing, comment_body)
            if result.success:
                click.secho(
                    f"\n  Comment added to #{existing}: {result.url}",
                    fg="green",
                    bold=True,
                )
            else:
                click.secho(
                    f"\n  Open in browser to comment: {result.url}",
                    fg="yellow",
                )
                webbrowser.open(result.url)
            return

    # 8. Submit
    if email:
        _submit_email(client, formatted_title, body, category,
                      support_settings(config)["email"])
    else:
        _submit_github(
            client, formatted_title, body, category, component,
            sys_info, issue_cfg,
        )


def _submit_github(
    client: GitHubClient,
    title: str,
    body: str,
    category: str,
    component: str,
    sys_info,
    issue_cfg: dict,
) -> None:
    """Submit issue via GitHub API or browser."""
    click.echo()

    if client.has_token:
        click.secho("  Submitting to GitHub...", fg="cyan")
        result = client.create_issue(
            title=title,
            body=body,
            category=category,
            component=component,
            version=sys_info.nomad_version,
            institution=sys_info.institution,
            source="cli",
        )
        if result.success:
            click.secho(
                f"\n  Issue #{result.number} created: {result.url}",
                fg="green",
                bold=True,
            )
        else:
            click.secho(f"\n  API submission failed: {result.error}", fg="red")
            click.echo("  Falling back to browser...")
            url = client.generate_browser_url(title, body, category)
            click.echo(f"  {url}")
            webbrowser.open(url)
    else:
        url = client.generate_browser_url(title, body, category)
        click.echo("  No GitHub token configured — opening browser.")
        click.echo(f"  {url[:120]}...")
        click.echo()
        if click.confirm("  Open in browser?", default=True):
            webbrowser.open(url)
        else:
            click.echo(f"\n  Full URL:\n  {url}")


def _submit_email(
    client: GitHubClient,
    title: str,
    body: str,
    category: str,
    support_email: str | None,
) -> None:
    """Submit issue via email."""
    if not support_email:
        click.secho(
            "  No support address is configured. Set [support] email in "
            "nomad.toml, or submit without --email.", fg="yellow")
        return
    subject, email_body = client.generate_email_body(title, body, category)

    click.echo()
    click.echo(f"  To: {support_email}")
    click.echo(f"  Subject: {subject}")
    click.echo()
    click.echo("  Body:")
    click.echo("  " + "-" * 58)
    for line in email_body.split("\n")[:20]:
        click.echo(f"  {line}")
    click.echo("  " + "-" * 58)
    click.echo()

    # Try mailto: link
    import urllib.parse
    mailto = (
        f"mailto:{support_email}"
        f"?subject={urllib.parse.quote(subject)}"
        f"&body={urllib.parse.quote(email_body[:2000])}"
    )
    if click.confirm("  Open email client?", default=True):
        webbrowser.open(mailto)
    else:
        click.echo(f"\n  Send manually to: {support_email}")


@issue.command("search")
@click.argument("keywords", nargs=-1, required=True)
@click.option("--max", "max_results", default=5, help="Max results")
@click.pass_context
def search(ctx, keywords, max_results):
    """Search existing issues for a topic.

    \b
    Examples:
        nomad issue search disk collector
        nomad issue search gpu memory --max 10
    """
    config = ctx.obj.get("config", {}) if ctx.obj else {}
    issue_cfg = config.get("issue_reporting", {})
    token = read_secret(issue_cfg, "github_token")

    client = GitHubClient(token=token)
    query = " ".join(keywords)

    click.echo()
    click.secho(f"  Searching issues for: {query}", fg="cyan")
    click.echo()

    results = client.search_duplicates(query, max_results=max_results)

    if not results:
        click.echo("  No matching issues found.")
        click.echo()
        click.echo(
            f"  Browse all issues: https://github.com/{client.REPO_OWNER}/"
            f"{client.REPO_NAME}/issues"
        )
        return

    for dup in results:
        state_color = "green" if dup.state == "open" else "red"
        labels = ", ".join(dup.labels[:3]) if dup.labels else ""
        label_str = f"  [{labels}]" if labels else ""

        click.secho(f"  #{dup.number}", fg=state_color, nl=False)
        click.echo(f" {dup.title}{label_str}")
        click.echo(f"    {dup.url}")
        click.echo(
            f"    {dup.state} · {dup.created_at} · "
            f"{dup.comments} comments"
        )
        click.echo()


@issue.command("info")
@click.option(
    "--db", "db_path",
    type=click.Path(exists=True),
    help="Database path",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def info(ctx, db_path, output_json):
    """Show system information that would be included in a report.

    \b
    Examples:
        nomad issue info
        nomad issue info --db /var/lib/nomad/cluster.db
        nomad issue info --json
    """
    config = ctx.obj.get("config", {}) if ctx.obj else {}
    if not db_path:
        db_path = _get_db_path(ctx)

    collector = IssueCollector(
        db_path=db_path, config=config, source="cli"
    )
    sys_info = collector.collect()

    if output_json:
        import json
        click.echo(json.dumps(sys_info.to_dict(), indent=2))
    else:
        click.echo()
        click.secho(
            "  System Information (auto-included in reports)",
            fg="cyan",
            bold=True,
        )
        click.echo()
        click.echo(f"  NØMAÐ version:    {sys_info.nomad_version}")
        click.echo(f"  Python:           {sys_info.python_version}")
        click.echo(f"  OS:               {sys_info.os_info}")
        if sys_info.active_collectors:
            click.echo(
                f"  Collectors:       {', '.join(sys_info.active_collectors)}"
            )
        if sys_info.alert_count:
            click.echo(
                f"  Active alerts:    {sys_info.alert_count} "
                f"({', '.join(sys_info.active_alerts[:5])})"
            )
        if sys_info.cluster_count:
            click.echo(
                f"  Clusters:         {sys_info.cluster_count} "
                f"({', '.join(sys_info.cluster_names[:5])})"
            )
        if sys_info.db_size_mb:
            click.echo(f"  Database:         {sys_info.db_size_mb:.1f} MB")
        if sys_info.institution:
            click.echo(f"  Institution:      {sys_info.institution}")
        click.echo()
