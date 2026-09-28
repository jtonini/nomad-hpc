# Reference System

The `nomad ref` command provides built-in documentation and code navigation without leaving the terminal.

## Usage

```bash
nomad ref                          # Browse all topics
nomad ref alerts                   # Alert system overview
nomad ref dyn diversity            # Dynamics diversity command
nomad ref collectors disk          # Disk collector details
nomad ref config                   # Configuration reference
nomad ref search regime divergence # Search across all documentation
nomad ref tessera                  # TESSERA methodology
nomad ref concepts governance      # Ostrom governance framework
```

## Knowledge Base

The reference system includes 60 entries across 5 categories:

| Category | Count | Description |
|----------|-------|-------------|
| Commands | 32 | All CLI commands and subcommands |
| Collectors | 9 | Data collection modules |
| Concepts | 11 | TESSERA, diversity indices, governance |
| Config | 5 | nomad.toml configuration sections |
| Alerts | 3 | Alert system, thresholds, backends |

Each entry includes:

- **Description** — plain language explanation
- **Source files** — paths in the repository
- **Configuration** — relevant nomad.toml keys
- **Mathematical basis** — formulas where applicable
- **Examples** — CLI usage examples
- **Related** — cross-references to related entries

## Search

Full-text search with relevance scoring across all entries:

```bash
nomad ref search "disk alert"
nomad ref search "simpson diversity"
nomad ref search "SLURM collector"
```

## Console Integration

In the NØMAÐ Console (paid product), the Reference page provides a graphical interface with category filters, live search, and clickable cross-references.

## Adding Entries

Reference entries are stored as YAML files in `nomad/reference/entries/`. To add a new entry:

```yaml
entries:
  my.new.topic:
    title: "My New Topic"
    summary: "One-line description."
    description: "Longer explanation."
    source_files:
      - nomad/my_module.py
    config_section: "my_section"
    examples:
      - "nomad my-command --flag"
    tags:
      - my-tag
    category: commands
```

The entry is immediately available in `nomad ref` and `nomad ref search`.
