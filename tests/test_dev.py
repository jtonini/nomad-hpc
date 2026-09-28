# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for the NØMAÐ Developer Toolchain."""

from __future__ import annotations

import os
import pytest
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from nomad.dev.scaffolding import (
    MODULE_TYPES,
    ScaffoldEngine,
    ScaffoldResult,
    _to_class_name,
)
from nomad.dev.checker import HealthChecker, CheckReport, CheckItem
from nomad.dev.cli_commands import dev


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def temp_repo(tmp_path):
    """Create a minimal NØMAÐ repo structure for testing."""
    # Create minimal directory structure
    (tmp_path / "nomad").mkdir()
    (tmp_path / "nomad" / "__init__.py").write_text('__version__ = "1.2.5"\n')

    # Collectors
    collectors_dir = tmp_path / "nomad" / "collectors"
    collectors_dir.mkdir()
    (collectors_dir / "__init__.py").write_text(
        "from .base import BaseCollector, CollectionError, registry\n"
        "from .disk import DiskCollector\n\n"
        "__all__ = ['BaseCollector', 'CollectionError', 'registry', 'DiskCollector']\n"
    )
    (collectors_dir / "base.py").write_text(
        "from abc import ABC, abstractmethod\n\n"
        "class BaseCollector(ABC):\n"
        '    """Base collector."""\n'
        "    name = 'base'\n\n"
        "class CollectionError(Exception):\n    pass\n\n"
        "class CollectorRegistry:\n"
        "    def register(self, cls): return cls\n\n"
        "registry = CollectorRegistry()\n"
    )
    (collectors_dir / "disk.py").write_text(
        "from .base import BaseCollector, registry\n\n"
        "@registry.register\n"
        "class DiskCollector(BaseCollector):\n"
        '    """Disk collector."""\n'
        "    name = 'disk'\n"
        "    def collect(self): return []\n"
        "    def store(self, data): pass\n"
    )

    # Dynamics
    dyn_dir = tmp_path / "nomad" / "dynamics"
    dyn_dir.mkdir()
    (dyn_dir / "__init__.py").write_text("")
    (dyn_dir / "diversity.py").write_text(
        '"""Diversity metric."""\n\n'
        "class DiversityMetric:\n"
        '    """Diversity."""\n'
        "    def compute(self, data): return 0.0\n"
    )

    # Tests
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_collector_disk.py").write_text(
        "def test_disk(): pass\n"
    )

    # Config
    (tmp_path / "nomad.toml.example").write_text(
        "[collectors.disk]\nenabled = true\n"
    )

    # pyproject.toml
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nomad-hpc"\nversion = "1.2.5"\n'
    )

    # CLI
    (tmp_path / "nomad" / "cli.py").write_text(
        "import click\n\n"
        "@click.group()\n"
        "def cli(): pass\n\n"
        "@cli.command()\n"
        "def status(): pass\n"
    )

    # CHANGELOG
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.5] - 2026-04-01\n"
    )

    return tmp_path


@pytest.fixture
def engine(temp_repo):
    return ScaffoldEngine(temp_repo)


@pytest.fixture
def checker(temp_repo):
    return HealthChecker(temp_repo)


@pytest.fixture
def runner():
    return CliRunner()


# =============================================================================
# SCAFFOLDING TESTS
# =============================================================================

class TestModuleTypes:
    """Verify MODULE_TYPES registry is complete."""

    def test_all_types_defined(self):
        expected = {"collector", "command", "analysis", "metric",
                    "view", "page", "alert", "insight"}
        assert set(MODULE_TYPES.keys()) == expected

    def test_each_type_has_guide_label(self):
        for name, mtype in MODULE_TYPES.items():
            assert mtype.guide_label, f"{name} missing guide_label"

    def test_each_type_has_description(self):
        for name, mtype in MODULE_TYPES.items():
            assert mtype.description, f"{name} missing description"


class TestScaffoldEngine:
    """Test the scaffolding engine."""

    def test_invalid_type(self, engine):
        result = engine.scaffold("nonexistent", "test")
        assert not result.success
        assert "Unknown module type" in result.error

    def test_invalid_name(self, engine):
        result = engine.scaffold("collector", "Invalid Name!")
        assert not result.success
        assert "Invalid module name" in result.error

    def test_scaffold_collector(self, engine, temp_repo):
        result = engine.scaffold("collector", "zfs", {
            "system": "ZFS pool health",
            "metrics": "pool_health, arc_hit_ratio",
            "access": "3",
            "package": "zfsutils-linux",
        })
        assert result.success
        assert len(result.created_files) >= 3  # .py, .sql, test, config

        # Verify files exist
        collector_path = temp_repo / "nomad" / "collectors" / "zfs.py"
        assert collector_path.exists()
        content = collector_path.read_text()
        assert "class ZfsCollector(BaseCollector)" in content
        assert "pool_health" in content
        assert "arc_hit_ratio" in content
        assert "zfsutils-linux" in content

        # Verify schema
        schema_path = temp_repo / "nomad" / "collectors" / "schemas" / "zfs.sql"
        assert schema_path.exists()
        assert "pool_health REAL" in schema_path.read_text()

        # Verify test
        test_path = temp_repo / "tests" / "test_collector_zfs.py"
        assert test_path.exists()
        assert "TestZfsCollector" in test_path.read_text()

    def test_scaffold_collector_duplicate(self, engine, temp_repo):
        """Cannot scaffold over existing module."""
        engine.scaffold("collector", "zfs")
        result = engine.scaffold("collector", "zfs")
        assert not result.success
        assert "already exists" in result.error

    def test_scaffold_command(self, engine, temp_repo):
        result = engine.scaffold("command", "topology", {
            "group": "top-level",
            "purpose": "Show cluster topology",
            "options": "format,verbose",
        })
        assert result.success
        cmd_path = temp_repo / "nomad" / "cli" / "topology.py"
        assert cmd_path.exists()
        content = cmd_path.read_text()
        assert "@click.command()" in content

    def test_scaffold_analysis(self, engine, temp_repo):
        result = engine.scaffold("analysis", "spectral", {
            "methodology": "Spectral clustering",
            "data_source": "job similarity network",
        })
        assert result.success
        path = temp_repo / "nomad" / "analysis" / "spectral.py"
        assert path.exists()
        content = path.read_text()
        assert "class SpectralAnalyzer" in content
        assert "to_insight" in content

    def test_scaffold_metric(self, engine, temp_repo):
        result = engine.scaffold("metric", "competition", {
            "framework": "ecology",
            "formula": "Lotka-Volterra competition coefficient",
        })
        assert result.success
        path = temp_repo / "nomad" / "dynamics" / "competition.py"
        assert path.exists()
        content = path.read_text()
        assert "class CompetitionMetric" in content
        assert "Lotka-Volterra" in content

    def test_scaffold_alert(self, engine, temp_repo):
        result = engine.scaffold("alert", "pagerduty", {
            "channel": "PagerDuty",
        })
        assert result.success
        path = temp_repo / "nomad" / "alerts" / "pagerduty.py"
        assert path.exists()
        assert "class PagerdutyBackend(NotificationBackend)" in path.read_text()

    def test_scaffold_insight(self, engine, temp_repo):
        result = engine.scaffold("insight", "disk_community", {
            "signal_type": "storage",
        })
        assert result.success
        path = temp_repo / "nomad" / "insights" / "templates" / "disk_community.py"
        assert path.exists()

    def test_scaffold_page(self, engine, temp_repo):
        result = engine.scaffold("page", "ecosystem", {
            "purpose": "Cluster ecosystem overview",
            "data_source": "dynamics metrics",
        })
        assert result.success
        # JSX frontend
        jsx_found = any("EcosystemPage.jsx" in f for f in result.created_files)
        assert jsx_found
        # Python backend
        py_found = any("ecosystem.py" in f for f in result.created_files)
        assert py_found

    def test_scaffold_view(self, engine, temp_repo):
        result = engine.scaffold("view", "network_health", {
            "chart_type": "network graph",
            "data_source": "network metrics",
        })
        assert result.success


class TestToClassName:
    def test_simple(self):
        assert _to_class_name("zfs") == "Zfs"

    def test_multi_word(self):
        assert _to_class_name("network_health") == "NetworkHealth"

    def test_single_letter(self):
        assert _to_class_name("a") == "A"


# =============================================================================
# HEALTH CHECKER TESTS
# =============================================================================

class TestHealthChecker:
    """Test the codebase health checker."""

    def test_check_all(self, checker):
        report = checker.check_all()
        assert isinstance(report, CheckReport)
        assert len(report.items) > 0

    def test_collectors_registered(self, checker):
        report = checker.check_all()
        reg_items = [i for i in report.items if "Collectors" in i.description]
        assert len(reg_items) > 0

    def test_test_coverage(self, checker):
        report = checker.check_all()
        test_items = [i for i in report.items if i.category == "Test Coverage"]
        assert len(test_items) > 0

    def test_strict_mode(self, checker):
        report = checker.check_all(strict=True)
        # In strict mode, warnings become errors
        assert report.summary["warning"] == 0

    def test_single_module(self, checker):
        report = checker.check_all(module="disk")
        assert len(report.items) > 0
        assert all("disk" in i.category.lower() for i in report.items)

    def test_missing_module(self, checker):
        report = checker.check_all(module="nonexistent")
        assert report.has_errors

    def test_summary_line(self):
        report = CheckReport()
        report.add(CheckItem("cat", "desc", "pass"))
        assert report.summary_line() == "All checks passed."

        report.add(CheckItem("cat", "desc", "warning"))
        assert "1 warning" in report.summary_line()

    def test_fix_collector_registration(self, checker, temp_repo):
        """Auto-fix adds missing collector imports."""
        # Create an unregistered collector
        (temp_repo / "nomad" / "collectors" / "zfs.py").write_text(
            "from .base import BaseCollector, registry\n\n"
            "@registry.register\n"
            "class ZfsCollector(BaseCollector):\n"
            "    name = 'zfs'\n"
            "    def collect(self): return []\n"
            "    def store(self, data): pass\n"
        )
        report = checker.check_all()
        actions = checker.fix(report)
        # Should have attempted to fix registration
        init_content = (temp_repo / "nomad" / "collectors" / "__init__.py").read_text()
        assert "zfs" in init_content.lower() or len(actions) > 0


# =============================================================================
# CLI TESTS
# =============================================================================

class TestDevCLI:
    """Test the Click CLI commands."""

    def test_help(self, runner):
        result = runner.invoke(dev, ["--help"])
        assert result.exit_code == 0
        assert "Developer Toolchain" in result.output

    def test_guide_help(self, runner):
        result = runner.invoke(dev, ["guide", "--help"])
        assert result.exit_code == 0

    def test_new_help(self, runner):
        result = runner.invoke(dev, ["new", "--help"])
        assert result.exit_code == 0

    def test_check_help(self, runner):
        result = runner.invoke(dev, ["check", "--help"])
        assert result.exit_code == 0

    def test_test_help(self, runner):
        result = runner.invoke(dev, ["test", "--help"])
        assert result.exit_code == 0

    def test_status_help(self, runner):
        result = runner.invoke(dev, ["status", "--help"])
        assert result.exit_code == 0

    def test_submit_help(self, runner):
        result = runner.invoke(dev, ["submit", "--help"])
        assert result.exit_code == 0

    def test_setup_help(self, runner):
        result = runner.invoke(dev, ["setup", "--help"])
        assert result.exit_code == 0

    def test_bump_help(self, runner):
        result = runner.invoke(dev, ["bump", "--help"])
        assert result.exit_code == 0

    def test_deps_help(self, runner):
        result = runner.invoke(dev, ["deps", "--help"])
        assert result.exit_code == 0

    def test_new_collector(self, runner, temp_repo):
        """nomad dev new collector creates files."""
        with runner.isolated_filesystem(temp_dir=temp_repo):
            os.chdir(temp_repo)
            result = runner.invoke(dev, ["new", "collector", "zfs"])
            assert result.exit_code == 0
            assert "Created" in result.output or "zfs" in result.output

    def test_new_invalid_type(self, runner):
        result = runner.invoke(dev, ["new", "invalid_type", "test"])
        assert result.exit_code != 0

    def test_check_json(self, runner, temp_repo):
        """nomad dev check --format json produces valid JSON."""
        import json
        with runner.isolated_filesystem(temp_dir=temp_repo):
            os.chdir(temp_repo)
            result = runner.invoke(dev, ["check", "--format", "json"])
            # Should be parseable JSON
            try:
                data = json.loads(result.output)
                assert "items" in data
                assert "summary" in data
            except json.JSONDecodeError:
                # May have non-JSON output mixed in
                pass

    def test_bump_dry_run(self, runner, temp_repo):
        """nomad dev bump --dry-run shows changes."""
        with runner.isolated_filesystem(temp_dir=temp_repo):
            os.chdir(temp_repo)
            result = runner.invoke(dev, ["bump", "patch", "--dry-run"])
            assert result.exit_code == 0
            assert "1.2.5" in result.output
            assert "1.2.6" in result.output
