# Developer Toolchain

The `nomad dev` command family provides scaffolding, validation, and contribution tools
for NØMAÐ module development. It codifies architectural patterns into CLI tools so
contributors focus on logic, not plumbing.

## Philosophy

- **Codify patterns, don't document them.** The scaffolding tool enforces correct
  structure rather than relying on contributors to read a wiki.
- **Lower the barrier to the creative work.** Plumbing (file creation, registration,
  wiring, test boilerplate) is identical for every module of the same type. The toolchain
  handles all plumbing so the contributor focuses on implementation.
- **Quality by construction.** Rather than catching problems in code review, prevent them
  from being created. Every scaffolded module comes with tests, documentation stubs,
  proper registration, and linting compliance built in.

## Quick Start

```bash
# First-time setup (installs ruff, pytest, pre-commit hooks)
nomad dev setup

# Interactive wizard — walks you through everything
nomad dev guide

# Direct scaffolding if you know what you want
nomad dev new collector zfs

# Validate your work
nomad dev check

# Submit when ready
nomad dev submit
```

## Commands

### `nomad dev guide`

Interactive contribution wizard. Asks what you want to build, gathers parameters,
scaffolds the module, and provides step-by-step next instructions.

Supports 8 module types:

| Type | Description | Example |
|------|-------------|---------|
| `collector` | Gather metrics from a new data source | `nomad dev new collector zfs` |
| `command` | Add a user-facing CLI command | `nomad dev new command topology` |
| `analysis` | Add analytical capability | `nomad dev new analysis spectral` |
| `metric` | Add to `nomad dyn` | `nomad dev new metric resilience` |
| `view` | Add a dashboard visualization | `nomad dev new view network_health` |
| `page` | Add a Console feature | `nomad dev new page ecosystem` |
| `alert` | Add a new alerting mechanism | `nomad dev new alert pagerduty` |
| `insight` | Add to the Insight Engine | `nomad dev new insight disk_community` |

### `nomad dev new <type> <name>`

Direct scaffolding for experienced contributors. Creates:

- Source file with proper base class inheritance and method stubs
- Database schema (collectors)
- Test file with test stubs for each expected behavior
- Configuration template
- Next-step instructions with references to similar modules

### `nomad dev check`

Validates codebase structural integrity:

- **Module Registration** — all modules imported and registered
- **Test Coverage** — every module has a test file
- **Documentation** — docstrings present, reference entries exist
- **Code Quality** — ruff linting
- **Architecture Consistency** — base class inheritance, Click patterns
- **Integration Points** — schemas, alerts, insights wired
- **Config Consistency** — all sections have matching modules

Options:

```bash
nomad dev check              # Standard check
nomad dev check --fix        # Auto-fix registration issues
nomad dev check --strict     # Treat warnings as errors (for CI/CD)
nomad dev check --module zfs # Check only a specific module (fast)
nomad dev check -f json      # JSON output
```

### `nomad dev test`

Targeted testing with git-aware file detection.

```bash
nomad dev test                    # Test only changed files (default)
nomad dev test all                # Full test suite
nomad dev test collector zfs      # Test specific module
nomad dev test collectors         # Test all collectors
nomad dev test changed            # Explicit: test what changed since last commit
nomad dev test --coverage         # Show coverage report
```

### `nomad dev status`

Shows current branch, changes from main, and readiness for submission:
check results, test collection status, and ahead/behind count.

### `nomad dev submit`

Full contribution pipeline:

1. Runs `nomad dev check --strict`
2. Runs test suite
3. Runs ruff linting
4. Analyzes changes, detects contribution type
5. Creates branch, commits, pushes
6. Opens PR (or generates patch file with `--patch`)

Authentication tiers:

- **Institutional token** in `nomad.toml` — seamless submission
- **Personal token** in `~/.config/nomad/dev.toml` — set up via `nomad dev setup`
- **Browser fallback** — opens GitHub with fields pre-filled
- **Patch export** — `nomad dev submit --patch` for email-based contribution

### `nomad dev setup`

One-time environment configuration:

1. GitHub token (fine-grained, with Contents + PRs + Issues permissions)
2. Development dependencies (ruff, pytest, pytest-cov)
3. Pre-commit hooks (ruff check + module validation on changed files)

### `nomad dev bump`

Version management:

```bash
nomad dev bump patch    # 1.2.5 -> 1.2.6
nomad dev bump minor    # 1.2.5 -> 1.3.0
nomad dev bump major    # 1.2.5 -> 2.0.0
nomad dev bump patch --dry-run  # Show what would change
```

Updates `pyproject.toml`, `__version__`, CHANGELOG, and creates a git tag.

### `nomad dev deps`

Module dependency graph — shows upstream dependencies, downstream dependents,
and related modules:

```bash
nomad dev deps collector disk
```

## Scaffolding Templates

Each module type has a template that embodies NØMAÐ's architectural patterns.

### Collector Template

Every scaffolded collector includes:

- `BaseCollector` subclass with `collect()`, `store()`, `get_history()` stubs
- `@registry.register` decorator
- Metric definitions
- Dependency checking (for external packages)
- SQL schema file
- Config template for `nomad.toml`
- 6 test stubs (init, collect, parse, store, config, error handling)

### CLI Command Template

- Click decorators with `--format` (table/json/plain) and `--no-color` flags
- Pass-through context for config access
- 4 test stubs (default, options, format, error)

### Analysis Module Template

- Result dataclass with `to_dict()` and `to_narrative()`
- Alert integration via `to_alert()`
- Insight Engine integration via `to_insight()`
- 4 test stubs (known input, edge cases, alert, insight)

### Dynamics Metric Template

- `compute()`, `trend()`, `to_insight()`, `visualize()` methods
- CLI registration for `nomad dyn <name>`
- 4 test stubs (compute, trend, insight, edge cases)

## CI/CD Integration

Use `nomad dev check --strict` in your CI pipeline to enforce quality:

```yaml
# GitHub Actions example
- name: NØMAÐ codebase validation
  run: |
    nomad dev check --strict
    nomad dev test all --coverage
```

Pre-commit hooks (installed by `nomad dev setup`) run ruff and module
validation automatically before each commit.
