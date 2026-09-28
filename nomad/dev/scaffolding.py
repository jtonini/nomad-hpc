# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Scaffolding Engine

Generates complete module scaffolds from templates. Each module type
(collector, command, analysis, dynamics, view, page, alert, insight)
has a template that produces all necessary files, registrations,
tests, and documentation stubs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# MODULE TYPE DEFINITIONS
# =============================================================================

@dataclass
class ModuleType:
    """Definition of a scaffoldable module type."""
    name: str
    description: str
    guide_label: str
    source_dir: str          # relative to repo root
    test_prefix: str         # prefix for test file naming
    base_class: str | None   # base class to inherit from, if any
    registry_file: str | None  # __init__.py or registry to update
    has_ref_entry: bool = True
    has_config: bool = False
    has_schema: bool = False
    has_insight: bool = False
    guide_prompts: list[dict[str, str]] = field(default_factory=list)


MODULE_TYPES: dict[str, ModuleType] = {
    "collector": ModuleType(
        name="collector",
        description="Gather metrics from a new data source",
        guide_label="New collector (gather metrics from a new data source)",
        source_dir="nomad/collectors",
        test_prefix="test_collector",
        base_class="BaseCollector",
        registry_file="nomad/collectors/__init__.py",
        has_config=True,
        has_schema=True,
        has_insight=True,
        guide_prompts=[
            {"key": "system", "prompt": "What system or service does your collector monitor?"},
            {"key": "metrics", "prompt": "What metrics will it collect? (comma-separated, or 'skip' to define later)"},
            {
                "key": "access",
                "prompt": (
                    "Does this collector require special system access?\n"
                    "  1. No — uses standard user permissions\n"
                    "  2. Yes — requires root/sudo\n"
                    "  3. Yes — requires specific package"
                ),
            },
        ],
    ),
    "command": ModuleType(
        name="command",
        description="Add a user-facing CLI command",
        guide_label="New CLI command (add a user-facing command)",
        source_dir="nomad/cli",
        test_prefix="test_command",
        base_class=None,
        registry_file="nomad/cli.py",
        guide_prompts=[
            {"key": "group", "prompt": "Which command group? (top-level, edu, diag, dyn, cloud, community, or new group)"},
            {"key": "purpose", "prompt": "What does this command do? (one sentence)"},
            {"key": "options", "prompt": "What options/flags does it need? (comma-separated, or 'skip')"},
        ],
    ),
    "analysis": ModuleType(
        name="analysis",
        description="Add analytical capability",
        guide_label="New analysis module (add analytical capability)",
        source_dir="nomad/analysis",
        test_prefix="test_analysis",
        base_class="AnalysisInterface",
        registry_file="nomad/analysis/__init__.py",
        has_insight=True,
        guide_prompts=[
            {"key": "methodology", "prompt": "What analytical method does this implement?"},
            {"key": "data_source", "prompt": "What data does it analyze? (jobs, nodes, network, storage, etc.)"},
        ],
    ),
    "metric": ModuleType(
        name="metric",
        description="Add to nomad dyn",
        guide_label="New dynamics metric (add to nomad dyn)",
        source_dir="nomad/dynamics",
        test_prefix="test_dynamics",
        base_class="DynamicsInterface",
        registry_file="nomad/dynamics/__init__.py",
        has_insight=True,
        guide_prompts=[
            {"key": "framework", "prompt": "What framework is this metric from? (ecology, economics, governance, other)"},
            {"key": "formula", "prompt": "Mathematical definition (or 'skip' to define later)"},
        ],
    ),
    "view": ModuleType(
        name="view",
        description="Add a dashboard visualization",
        guide_label="New dashboard view (add a visualization)",
        source_dir="nomad/viz/views",
        test_prefix="test_view",
        base_class=None,
        registry_file=None,
        guide_prompts=[
            {"key": "chart_type", "prompt": "Visualization type? (time series, heatmap, bar chart, network graph, radar, other)"},
            {"key": "data_source", "prompt": "What data does it display?"},
        ],
    ),
    "page": ModuleType(
        name="page",
        description="Add a Console feature",
        guide_label="New Console page (add a Console feature)",
        source_dir="console/frontend/src/pages",
        test_prefix="test_page",
        base_class=None,
        registry_file=None,
        guide_prompts=[
            {"key": "purpose", "prompt": "What does this Console page do?"},
            {"key": "data_source", "prompt": "What backend data does it need?"},
        ],
    ),
    "alert": ModuleType(
        name="alert",
        description="Add a new alerting mechanism",
        guide_label="New alert type (add a new alerting mechanism)",
        source_dir="nomad/alerts",
        test_prefix="test_alert",
        base_class="NotificationBackend",
        registry_file="nomad/alerts/__init__.py",
        guide_prompts=[
            {"key": "channel", "prompt": "Alert delivery channel? (email, slack, webhook, pagerduty, other)"},
        ],
    ),
    "insight": ModuleType(
        name="insight",
        description="Add to the Insight Engine",
        guide_label="New insight template (add to the Insight Engine)",
        source_dir="nomad/insights/templates",
        test_prefix="test_insight",
        base_class=None,
        registry_file="nomad/insights/__init__.py",
        guide_prompts=[
            {"key": "signal_type", "prompt": "What signal does this insight interpret? (storage, network, jobs, gpu, etc.)"},
            {"key": "narrative", "prompt": "Example narrative output (or 'skip')"},
        ],
    ),
}


# =============================================================================
# SCAFFOLD RESULT
# =============================================================================

@dataclass
class ScaffoldResult:
    """Result of a scaffolding operation."""
    module_type: str
    module_name: str
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    success: bool = True
    error: str | None = None


# =============================================================================
# SCAFFOLDING ENGINE
# =============================================================================

class ScaffoldEngine:
    """Generates module scaffolds from templates."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._template_dir = Path(__file__).parent / "templates"

    def scaffold(
        self,
        module_type: str,
        name: str,
        params: dict[str, Any] | None = None,
    ) -> ScaffoldResult:
        """Scaffold a new module.

        Args:
            module_type: One of the keys in MODULE_TYPES.
            name: Module name (lowercase, no spaces).
            params: Additional parameters from the guide wizard.

        Returns:
            ScaffoldResult with created/modified files and next steps.
        """
        if module_type not in MODULE_TYPES:
            return ScaffoldResult(
                module_type=module_type,
                module_name=name,
                success=False,
                error=f"Unknown module type: {module_type}. "
                       f"Valid types: {', '.join(MODULE_TYPES)}",
            )

        # Validate name
        name = name.lower().strip()
        if not re.match(r'^[a-z][a-z0-9_]*$', name):
            return ScaffoldResult(
                module_type=module_type,
                module_name=name,
                success=False,
                error=f"Invalid module name: '{name}'. "
                       "Use lowercase letters, numbers, and underscores only.",
            )

        params = params or {}
        mtype = MODULE_TYPES[module_type]

        # Dispatch to type-specific scaffolder
        dispatch = {
            "collector": self._scaffold_collector,
            "command": self._scaffold_command,
            "analysis": self._scaffold_analysis,
            "metric": self._scaffold_metric,
            "view": self._scaffold_view,
            "page": self._scaffold_page,
            "alert": self._scaffold_alert,
            "insight": self._scaffold_insight,
        }

        scaffolder = dispatch.get(module_type)
        if scaffolder is None:
            return ScaffoldResult(
                module_type=module_type,
                module_name=name,
                success=False,
                error=f"Scaffolder not implemented for: {module_type}",
            )

        return scaffolder(name, mtype, params)

    # ─── Collector ────────────────────────────────────────────────────

    def _scaffold_collector(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="collector", module_name=name)
        class_name = _to_class_name(name) + "Collector"
        system = params.get("system", f"{name} system")
        metrics_raw = params.get("metrics", "")
        metrics = [m.strip() for m in metrics_raw.split(",") if m.strip()] if metrics_raw and metrics_raw != "skip" else []
        access = params.get("access", "1")
        package = params.get("package", "")

        # 1. Main collector file
        collector_path = self.repo_root / mtype.source_dir / f"{name}.py"
        if collector_path.exists():
            result.success = False
            result.error = f"Collector already exists: {collector_path}"
            return result

        metric_defs = "\n".join(
            f'        "{m}",' for m in metrics
        ) if metrics else '        # Define metrics here'

        metric_dict = "\n".join(
            f'            "{m}": 0.0,  # TODO: implement' for m in metrics
        ) if metrics else '            # "metric_name": value,  # TODO: implement'

        dep_check = ""
        if access == "3" and package:
            dep_check = f'''
    required_commands = ["{package}"]

    def _check_dependencies(self) -> bool:
        """Check if {package} is available."""
        import shutil
        return shutil.which("{package.split("-")[0]}") is not None
'''
        elif access == "2":
            dep_check = '''
    required_commands = []

    def _check_dependencies(self) -> bool:
        """Check if running with sufficient privileges."""
        import os
        return os.geteuid() == 0
'''

        collector_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ {class_name}

Monitors {system}.

Metrics collected:
{chr(10).join(f"    - {m}" for m in metrics) if metrics else "    - (define metrics)"}
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@registry.register
class {class_name}(BaseCollector):
    """{system} collector for NØMAÐ."""

    name = "{name}"
    description = "Monitors {system}"
    default_interval = 60
{dep_check}
    def collect(self) -> list[dict[str, Any]]:
        """Collect {name} metrics.

        Returns:
            List of metric dictionaries.

        Raises:
            CollectionError: If collection fails.
        """
        # TODO: Implement collection logic
        # Parse output from system commands, APIs, or files.
        # Return a list of dicts matching the schema.
        try:
            data = {{
{metric_dict}
            }}
            return [data]
        except Exception as e:
            raise CollectionError(f"{class_name} collection failed: {{e}}") from e

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store collected data in the database.

        Uses the schema defined in schemas/{name}.sql.
        """
        if not data:
            return
        # TODO: Implement storage logic
        # self.db.execute("INSERT INTO {name}_metrics ...")
        pass

    def get_history(self, hours: int = 24) -> list[dict[str, Any]]:
        """Retrieve historical data for derivative analysis.

        Args:
            hours: Number of hours of history to retrieve.

        Returns:
            List of historical metric records.
        """
        # TODO: Implement history retrieval
        return []
'''
        _write_file(collector_path, collector_content)
        result.created_files.append(str(collector_path.relative_to(self.repo_root)))

        # 2. Schema file
        schema_dir = self.repo_root / mtype.source_dir / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        schema_path = schema_dir / f"{name}.sql"

        columns = "\n".join(
            f"    {m} REAL," for m in metrics
        ) if metrics else "    -- metric_name REAL,"

        schema_content = f"""-- NØMAÐ {class_name} schema
-- Auto-generated by nomad dev

CREATE TABLE IF NOT EXISTS {name}_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    hostname TEXT NOT NULL,
{columns}
    collection_duration REAL
);

CREATE INDEX IF NOT EXISTS idx_{name}_timestamp
    ON {name}_metrics(timestamp);

CREATE INDEX IF NOT EXISTS idx_{name}_hostname
    ON {name}_metrics(hostname);
"""
        _write_file(schema_path, schema_content)
        result.created_files.append(str(schema_path.relative_to(self.repo_root)))

        # 3. Test file
        test_path = self.repo_root / "tests" / f"{mtype.test_prefix}_{name}.py"
        test_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""Tests for the {class_name}."""

import pytest
from unittest.mock import MagicMock, patch

from nomad.collectors.{name} import {class_name}


class Test{class_name}:
    """Test suite for {class_name}."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = {{}}
        self.collector = {class_name}(self.config)

    def test_init(self):
        """Collector initializes with valid config."""
        assert self.collector.name == "{name}"
        assert self.collector.description

    def test_collect(self):
        """collect() returns expected metric dict."""
        # TODO: Mock system commands and verify output
        # result = self.collector.collect()
        # assert isinstance(result, list)
        # assert len(result) > 0
        pytest.skip("Implement after collect() is complete")

    def test_parse(self):
        """Parser handles real and edge-case output."""
        # TODO: Test with sample command output
        pytest.skip("Implement after _parse methods are complete")

    def test_store(self):
        """Data round-trips through database correctly."""
        # TODO: Use in-memory SQLite
        pytest.skip("Implement after store() is complete")

    def test_config(self):
        """Config validation catches invalid values."""
        # TODO: Test with bad config values
        pytest.skip("Implement after config schema is defined")

    def test_error_handling(self):
        """Graceful handling of missing dependencies/permissions."""
        # TODO: Mock missing commands, verify CollectionError
        pytest.skip("Implement after error paths are defined")
'''
        _write_file(test_path, test_content)
        result.created_files.append(str(test_path.relative_to(self.repo_root)))

        # 4. Reference entry (YAML for nomad ref)
        ref_dir = self.repo_root / "nomad" / "reference" / "entries"
        if ref_dir.exists():
            ref_path = ref_dir / "collectors.yaml"
            # Append to existing file if present
            entry = f"""
  - name: "{name}"
    type: "collector"
    description: "Monitors {system}"
    source: "nomad/collectors/{name}.py"
    schema: "nomad/collectors/schemas/{name}.sql"
    config_section: "[collectors.{name}]"
    metrics:
{chr(10).join(f'      - "{m}"' for m in metrics) if metrics else '      - "# define metrics"'}
    see_also:
      - "disk"
      - "gpu"
"""
            # We'll note this as a manual step rather than auto-appending
            result.next_steps.append(
                f"Add reference entry to {ref_path.relative_to(self.repo_root)}"
            )

        # 5. Config template
        config_dir = self.repo_root / "nomad" / "config" / "templates"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / f"{name}.toml"

        config_content = f"""# NØMAÐ {class_name} configuration
# Add this section to your nomad.toml

[collectors.{name}]
enabled = true
interval = 60
"""
        if metrics:
            config_content += f"# Metrics: {', '.join(metrics)}\n"
        if access == "3" and package:
            config_content += f"\n# Requires: {package}\n"

        _write_file(config_path, config_content)
        result.created_files.append(str(config_path.relative_to(self.repo_root)))

        # 6. CHANGELOG entry
        result.next_steps.insert(0, f"Add CHANGELOG entry: feat(collectors): add {name} collector")

        # 7. Registration
        init_path = self.repo_root / mtype.registry_file
        if init_path.exists():
            result.modified_files.append(mtype.registry_file)
            result.next_steps.append(
                f"Register {class_name} in {mtype.registry_file} "
                f"(run 'nomad dev check --fix' to auto-register)"
            )

        # Next steps
        result.next_steps.extend([
            f"Implement the collect() method in nomad/collectors/{name}.py",
            "Implement store() for database persistence",
            f"Fill in test implementations in tests/{mtype.test_prefix}_{name}.py",
            f"Test locally: nomad collect --collector {name} --once",
            "Run: nomad dev check",
            "When ready: nomad dev submit",
        ])

        # References
        result.references = [
            "nomad ref collectors disk    (filesystem monitoring — closest pattern)",
            "nomad ref collectors gpu     (external tool parsing — similar pattern)",
        ]

        return result

    # ─── CLI Command ──────────────────────────────────────────────────

    def _scaffold_command(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="command", module_name=name)
        group = params.get("group", "top-level")
        purpose = params.get("purpose", f"{name} command")
        options_raw = params.get("options", "")
        options = [o.strip() for o in options_raw.split(",") if o.strip()] if options_raw and options_raw != "skip" else []

        # Determine file location
        if group in ("top-level", ""):
            cmd_path = self.repo_root / "nomad" / "cli" / f"{name}.py"
        else:
            cmd_path = self.repo_root / "nomad" / "cli" / f"{group}_{name}.py"

        if cmd_path.exists():
            result.success = False
            result.error = f"Command file already exists: {cmd_path}"
            return result

        # Build Click options
        option_decorators = ""
        option_params = ""
        for opt in options:
            opt_clean = opt.replace("-", "_").lower()
            option_decorators += (
                f"@click.option('--{opt_clean}', help='{opt_clean} option')\n"
            )
            option_params += f", {opt_clean}"

        # The command content will be added to cli.py via integration
        cmd_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ CLI command: {name}

{purpose}

This file contains the Click command definition. It will be integrated
into cli.py by the integration script or nomad dev check --fix.
"""

from __future__ import annotations

import click
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# {name.upper()} COMMAND
# =============================================================================

@click.command()
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['table', 'json', 'plain']),
              default='table', help='Output format')
@click.option('--no-color', is_flag=True, help='Disable colored output')
{option_decorators}@click.pass_context
def {name}(ctx{option_params}, output_format, no_color):
    """{purpose}"""
    config = ctx.obj.get('config', {{}})

    # TODO: Implement command logic
    click.echo(f"{name} command — not yet implemented")
'''
        _write_file(cmd_path, cmd_content)
        result.created_files.append(str(cmd_path.relative_to(self.repo_root)))

        # Test file
        test_path = self.repo_root / "tests" / f"{mtype.test_prefix}_{name}.py"
        test_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""Tests for the {name} CLI command."""

import pytest
from click.testing import CliRunner

# from nomad.cli import cli


class TestCommand{_to_class_name(name)}:
    """Test suite for nomad {name} command."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_default(self):
        """Command runs with defaults."""
        # result = self.runner.invoke(cli, ['{name}'])
        # assert result.exit_code == 0
        pytest.skip("Implement after command is wired into cli.py")

    def test_format_json(self):
        """JSON output is valid."""
        # result = self.runner.invoke(cli, ['{name}', '--format', 'json'])
        # import json
        # json.loads(result.output)
        pytest.skip("Implement after command is complete")

    def test_format_plain(self):
        """Plain output is screen-reader compatible."""
        # result = self.runner.invoke(cli, ['{name}', '--format', 'plain'])
        # assert '\\033' not in result.output  # no ANSI codes
        pytest.skip("Implement after command is complete")

    def test_error_handling(self):
        """Graceful handling of missing data/bad input."""
        pytest.skip("Implement after error paths are defined")
'''
        _write_file(test_path, test_content)
        result.created_files.append(str(test_path.relative_to(self.repo_root)))

        result.modified_files.append("nomad/cli.py")
        result.next_steps = [
            f"Implement command logic in {cmd_path.relative_to(self.repo_root)}",
            f"Fill in tests in tests/{mtype.test_prefix}_{name}.py",
            "Wire into cli.py (run 'nomad dev check --fix' to auto-register)",
            "Run: nomad dev check",
            "When ready: nomad dev submit",
        ]

        return result

    # ─── Analysis Module ──────────────────────────────────────────────

    def _scaffold_analysis(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="analysis", module_name=name)
        class_name = _to_class_name(name) + "Analyzer"
        methodology = params.get("methodology", f"{name} analysis")
        data_source = params.get("data_source", "general")

        analysis_dir = self.repo_root / mtype.source_dir
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = analysis_dir / f"{name}.py"

        if analysis_path.exists():
            result.success = False
            result.error = f"Analysis module already exists: {analysis_path}"
            return result

        analysis_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ {class_name}

{methodology}

Data source: {data_source}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class {_to_class_name(name)}Result:
    """Result of {name} analysis."""
    value: float = 0.0
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {{
            "value": self.value,
            "details": self.details or {{}},
        }}

    def to_narrative(self) -> str:
        """Generate human-readable interpretation."""
        # TODO: Implement narrative generation
        return f"{class_name} result: {{self.value:.4f}}"


class {class_name}:
    """{methodology} for NØMAÐ.

    Mathematical basis:
        TODO: Document the mathematical foundation.

    References:
        TODO: Add academic references.
    """

    def analyze(self, data: list[dict[str, Any]]) -> {_to_class_name(name)}Result:
        """Run analysis on the provided data.

        Args:
            data: List of records from the database.

        Returns:
            Analysis result with value and details.
        """
        if not data:
            return {_to_class_name(name)}Result()

        # TODO: Implement analysis logic
        return {_to_class_name(name)}Result(value=0.0)

    def to_alert(self, result: {_to_class_name(name)}Result) -> Optional[dict]:
        """Generate an alert if result exceeds threshold.

        Returns:
            Alert dict or None if no alert needed.
        """
        # TODO: Define alert thresholds
        return None

    def to_insight(self, result: {_to_class_name(name)}Result) -> str:
        """Generate Insight Engine narrative.

        Returns:
            Natural language interpretation for the Insight Engine.
        """
        return result.to_narrative()
'''
        _write_file(analysis_path, analysis_content)
        result.created_files.append(str(analysis_path.relative_to(self.repo_root)))

        # Test file
        test_path = self.repo_root / "tests" / f"{mtype.test_prefix}_{name}.py"
        test_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""Tests for the {class_name}."""

import pytest

from nomad.analysis.{name} import {class_name}, {_to_class_name(name)}Result


class Test{class_name}:
    """Test suite for {class_name}."""

    def setup_method(self):
        self.analyzer = {class_name}()

    def test_known_input(self):
        """Known input produces expected output."""
        # TODO: Create known test data and verify result
        pytest.skip("Implement with known test data")

    def test_edge_cases(self):
        """Empty data, single record, NaN handling."""
        result = self.analyzer.analyze([])
        assert isinstance(result, {_to_class_name(name)}Result)
        assert result.value == 0.0

    def test_alert_integration(self):
        """Alert fires at correct threshold."""
        # TODO: Test with threshold-exceeding data
        pytest.skip("Implement after alert thresholds are defined")

    def test_insight_generation(self):
        """Narrative output is well-formed."""
        result = {_to_class_name(name)}Result(value=0.5)
        narrative = self.analyzer.to_insight(result)
        assert isinstance(narrative, str)
        assert len(narrative) > 0
'''
        _write_file(test_path, test_content)
        result.created_files.append(str(test_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement analyze() in nomad/analysis/{name}.py",
            "Define alert thresholds in to_alert()",
            "Write Insight Engine narrative in to_insight()",
            f"Fill in tests in tests/{mtype.test_prefix}_{name}.py",
            "Run: nomad dev check",
            "When ready: nomad dev submit",
        ]

        return result

    # ─── Dynamics Metric ──────────────────────────────────────────────

    def _scaffold_metric(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="metric", module_name=name)
        class_name = _to_class_name(name) + "Metric"
        framework = params.get("framework", "general")
        formula = params.get("formula", "")

        metric_path = self.repo_root / mtype.source_dir / f"{name}.py"

        if metric_path.exists():
            result.success = False
            result.error = f"Dynamics metric already exists: {metric_path}"
            return result

        formula_doc = f"\n    Formula:\n        {formula}\n" if formula and formula != "skip" else ""

        metric_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ Dynamics: {_to_class_name(name)} Metric

Framework: {framework}
{formula_doc}
Interpretation:
    TODO: Document what this metric means and when to use it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class {_to_class_name(name)}Result:
    """Result of {name} metric computation."""
    value: float = 0.0
    trend: str = "stable"  # "increasing", "decreasing", "stable"
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {{
            "value": self.value,
            "trend": self.trend,
            "details": self.details or {{}},
        }}


class {class_name}:
    """{_to_class_name(name)} dynamics metric for NØMAÐ.

    Part of the nomad dyn command family.
    """

    def compute(
        self,
        data: list[dict[str, Any]],
        window: str = "7d",
    ) -> {_to_class_name(name)}Result:
        """Compute the {name} metric.

        Args:
            data: Records from the database.
            window: Time window for computation.

        Returns:
            Metric result with value, trend, and details.
        """
        if not data:
            return {_to_class_name(name)}Result()

        # TODO: Implement metric computation
        return {_to_class_name(name)}Result(value=0.0)

    def trend(
        self,
        data: list[dict[str, Any]],
        periods: int = 4,
    ) -> dict[str, Any]:
        """Compute trend over multiple periods.

        Args:
            data: Records from the database.
            periods: Number of time periods to compare.

        Returns:
            Trend information with direction and magnitude.
        """
        # TODO: Implement trend detection
        return {{"direction": "stable", "magnitude": 0.0, "periods": periods}}

    def to_insight(self, result: {_to_class_name(name)}Result) -> str:
        """Generate Insight Engine narrative.

        Returns:
            Natural language interpretation.
        """
        # TODO: Implement narrative template
        return f"{_to_class_name(name)} metric: {{result.value:.4f}} ({{result.trend}})"

    def visualize(self, result: {_to_class_name(name)}Result) -> dict[str, Any]:
        """Generate visualization specification for dashboard/Console.

        Returns:
            Chart specification dict.
        """
        # TODO: Define chart type and data mapping
        return {{
            "chart_type": "line",
            "title": "{_to_class_name(name)}",
            "data": result.to_dict(),
        }}
'''
        _write_file(metric_path, metric_content)
        result.created_files.append(str(metric_path.relative_to(self.repo_root)))

        # Test file
        test_path = self.repo_root / "tests" / f"{mtype.test_prefix}_{name}.py"
        test_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""Tests for the {class_name}."""

import pytest

from nomad.dynamics.{name} import {class_name}, {_to_class_name(name)}Result


class Test{class_name}:
    """Test suite for {class_name}."""

    def setup_method(self):
        self.metric = {class_name}()

    def test_compute(self):
        """Metric produces expected value for known data."""
        # TODO: Create known test data
        pytest.skip("Implement with known test data")

    def test_trend(self):
        """Trend detection works correctly."""
        # TODO: Test with trending data
        pytest.skip("Implement with trending test data")

    def test_insight(self):
        """Narrative output is accurate."""
        result = {_to_class_name(name)}Result(value=0.5, trend="increasing")
        narrative = self.metric.to_insight(result)
        assert isinstance(narrative, str)
        assert "0.5" in narrative

    def test_edge_cases(self):
        """Handles sparse data, single group, etc."""
        result = self.metric.compute([])
        assert result.value == 0.0
'''
        _write_file(test_path, test_content)
        result.created_files.append(str(test_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement compute() in nomad/dynamics/{name}.py",
            "Implement trend detection",
            "Write Insight Engine narrative in to_insight()",
            "Define visualization spec in visualize()",
            f"Fill in tests in tests/{mtype.test_prefix}_{name}.py",
            f"Wire CLI: add 'nomad dyn {name}' subcommand",
            "Run: nomad dev check",
            "When ready: nomad dev submit",
        ]

        return result

    # ─── View (Dashboard) ────────────────────────────────────────────

    def _scaffold_view(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="view", module_name=name)
        chart_type = params.get("chart_type", "line")
        data_source = params.get("data_source", "general")

        view_dir = self.repo_root / mtype.source_dir
        view_dir.mkdir(parents=True, exist_ok=True)
        view_path = view_dir / f"{name}.html"

        if view_path.exists():
            result.success = False
            result.error = f"View already exists: {view_path}"
            return result

        view_content = f"""<!-- NØMAÐ Dashboard View: {name} -->
<!-- Chart type: {chart_type} | Data source: {data_source} -->
<!-- Auto-generated by nomad dev -->

<div id="{name}-view" class="nomad-view">
  <h3>{_to_class_name(name)}</h3>
  <div id="{name}-chart"></div>
</div>

<script type="text/babel">
  // TODO: Implement {chart_type} visualization
  // Fetch data from /api/{name} and render chart
</script>
"""
        _write_file(view_path, view_content)
        result.created_files.append(str(view_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement visualization in {view_path.relative_to(self.repo_root)}",
            "Add corresponding API endpoint in server.py",
            "Run: nomad dev check",
        ]

        return result

    # ─── Console Page ────────────────────────────────────────────────

    def _scaffold_page(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="page", module_name=name)
        purpose = params.get("purpose", f"{name} page")
        data_source = params.get("data_source", "general")
        class_name_pg = _to_class_name(name) + "Page"

        page_dir = self.repo_root / mtype.source_dir
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / f"{class_name_pg}.jsx"

        if page_path.exists():
            result.success = False
            result.error = f"Page already exists: {page_path}"
            return result

        page_content = f'''// SPDX-License-Identifier: BSL-1.1
// Copyright (C) {datetime.now().year} João Tonini
/**
 * NØMAÐ Console — {class_name_pg}
 *
 * {purpose}
 * Data source: {data_source}
 *
 * Auto-generated by nomad dev
 */

import React, {{ useState, useEffect }} from 'react';

export default function {class_name_pg}() {{
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {{
    fetch('/api/{name}')
      .then(res => res.json())
      .then(setData)
      .catch(setError)
      .finally(() => setLoading(false));
  }}, []);

  if (loading) return <div className="p-6">Loading...</div>;
  if (error) return <div className="p-6 text-red-500">Error: {{error.message}}</div>;

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold">{_to_class_name(name)}</h1>
      {{/* TODO: Implement page content */}}
      <pre>{{JSON.stringify(data, null, 2)}}</pre>
    </div>
  );
}}
'''
        _write_file(page_path, page_content)
        result.created_files.append(str(page_path.relative_to(self.repo_root)))

        # Backend route
        backend_dir = self.repo_root / "console" / "backend" / "routes"
        backend_dir.mkdir(parents=True, exist_ok=True)
        route_path = backend_dir / f"{name}.py"

        route_content = f'''# SPDX-License-Identifier: BSL-1.1
# Copyright (C) {datetime.now().year} João Tonini
"""Backend route for {class_name_pg}."""

from __future__ import annotations

from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api/{name}", tags=["{name}"])


@router.get("/")
async def get_{name}():
    """Get {name} data.

    TODO: Implement data retrieval.
    """
    return {{"status": "ok", "data": []}}
'''
        _write_file(route_path, route_content)
        result.created_files.append(str(route_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement page UI in {page_path.relative_to(self.repo_root)}",
            f"Implement backend in {route_path.relative_to(self.repo_root)}",
            "Register route in console/backend/main.py",
            "Add page to console/frontend/src/App.jsx router",
            "Run: nomad dev check",
        ]

        return result

    # ─── Alert ────────────────────────────────────────────────────────

    def _scaffold_alert(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="alert", module_name=name)
        class_name_alert = _to_class_name(name) + "Backend"
        channel = params.get("channel", name)

        alert_path = self.repo_root / mtype.source_dir / f"{name}.py"

        if alert_path.exists():
            result.success = False
            result.error = f"Alert backend already exists: {alert_path}"
            return result

        alert_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ Alert Backend: {class_name_alert}

Delivers alerts via {channel}.
"""

from __future__ import annotations

import logging
from typing import Any

from .backends import NotificationBackend

logger = logging.getLogger(__name__)


class {class_name_alert}(NotificationBackend):
    """{channel} alert backend for NØMAÐ."""

    name = "{name}"

    def __init__(self, config: dict[str, Any]):
        """Initialize the {channel} backend.

        Args:
            config: Backend configuration from nomad.toml.
        """
        self.config = config
        # TODO: Initialize connection/client

    def send(self, alert: dict[str, Any]) -> bool:
        """Send an alert via {channel}.

        Args:
            alert: Alert data with severity, message, details.

        Returns:
            True if sent successfully.
        """
        # TODO: Implement delivery logic
        logger.info(f"{{self.name}} alert: {{alert.get('message', '')}}")
        return True

    def test(self) -> bool:
        """Send a test alert to verify configuration.

        Returns:
            True if test succeeds.
        """
        return self.send({{"severity": "info", "message": "NØMAÐ test alert"}})
'''
        _write_file(alert_path, alert_content)
        result.created_files.append(str(alert_path.relative_to(self.repo_root)))

        # Test file
        test_path = self.repo_root / "tests" / f"{mtype.test_prefix}_{name}.py"
        test_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""Tests for the {class_name_alert}."""

import pytest
from unittest.mock import MagicMock, patch

from nomad.alerts.{name} import {class_name_alert}


class Test{class_name_alert}:

    def setup_method(self):
        self.config = {{}}
        self.backend = {class_name_alert}(self.config)

    def test_send(self):
        """Alert sends successfully."""
        # TODO: Mock external service
        pytest.skip("Implement after send() is complete")

    def test_test_alert(self):
        """Test alert works."""
        # TODO: Mock external service
        pytest.skip("Implement after test() is complete")
'''
        _write_file(test_path, test_content)
        result.created_files.append(str(test_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement send() in nomad/alerts/{name}.py",
            "Register backend in alert dispatcher",
            f"Fill in tests in tests/{mtype.test_prefix}_{name}.py",
            "Run: nomad dev check",
        ]

        return result

    # ─── Insight Template ────────────────────────────────────────────

    def _scaffold_insight(
        self, name: str, mtype: ModuleType, params: dict[str, Any]
    ) -> ScaffoldResult:
        result = ScaffoldResult(module_type="insight", module_name=name)
        signal_type = params.get("signal_type", "general")
        narrative_example = params.get("narrative", "")

        template_dir = self.repo_root / mtype.source_dir
        template_dir.mkdir(parents=True, exist_ok=True)
        template_path = template_dir / f"{name}.py"

        if template_path.exists():
            result.success = False
            result.error = f"Insight template already exists: {template_path}"
            return result

        template_content = f'''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) {datetime.now().year} João Tonini
"""
NØMAÐ Insight Template: {name}

Signal type: {signal_type}
{f"Example: {narrative_example}" if narrative_example and narrative_example != "skip" else ""}
"""

from __future__ import annotations

from typing import Any


def generate_{name}_insight(signal: dict[str, Any]) -> str | None:
    """Generate a narrative insight from a {signal_type} signal.

    Args:
        signal: Signal data from the {signal_type} reader.

    Returns:
        Narrative string, or None if no insight is warranted.
    """
    # TODO: Implement narrative generation
    # Check signal severity, extract key values, compose narrative
    return None
'''
        _write_file(template_path, template_content)
        result.created_files.append(str(template_path.relative_to(self.repo_root)))

        result.next_steps = [
            f"Implement narrative logic in nomad/insights/templates/{name}.py",
            "Register template in the Insight Engine",
            "Run: nomad dev check",
        ]

        return result


# =============================================================================
# UTILITIES
# =============================================================================

def _to_class_name(name: str) -> str:
    """Convert snake_case to PascalCase."""
    return "".join(word.capitalize() for word in name.split("_"))


def _write_file(path: Path, content: str) -> None:
    """Write content to file, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    logger.info("Created: %s", path)
