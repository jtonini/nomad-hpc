"""Tests for the NØMAÐ issue reporting module."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nomad.issue.collector import IssueCollector, SystemInfo
from nomad.issue.formatter import IssueFormatter, CATEGORIES, COMPONENTS
from nomad.issue.github_api import GitHubClient, IssueResult


# ── SystemInfo Tests ──────────────────────────────────────────────


class TestSystemInfo:
    def test_to_markdown_minimal(self):
        info = SystemInfo(
            nomad_version="1.3.0",
            python_version="3.11.4",
            os_info="Linux 5.14.0",
            source="cli",
            timestamp="2026-04-07T10:00:00",
        )
        md = info.to_markdown()
        assert "### System Information" in md
        assert "1.3.0" in md
        assert "3.11.4" in md
        assert "Linux 5.14.0" in md
        assert "cli" in md

    def test_to_markdown_full(self):
        info = SystemInfo(
            nomad_version="1.3.0",
            console_version="1.0.2",
            python_version="3.11.4",
            os_info="Rocky Linux 9.6",
            active_collectors=["slurm", "gpu", "disk"],
            active_alerts=["disk_warning", "gpu_temp"],
            alert_count=2,
            cluster_count=3,
            source="console",
            institution="University of Richmond",
            submitted_by="jtonini@richmond.edu",
            timestamp="2026-04-07T10:00:00",
        )
        md = info.to_markdown()
        assert "Console version" in md
        assert "slurm, gpu, disk" in md
        assert "disk_warning, gpu_temp" in md
        assert "University of Richmond" in md

    def test_to_dict(self):
        info = SystemInfo(nomad_version="1.3.0", source="cli")
        d = info.to_dict()
        assert d["nomad_version"] == "1.3.0"
        assert d["source"] == "cli"
        assert isinstance(d["active_collectors"], list)


# ── IssueCollector Tests ──────────────────────────────────────────


class TestIssueCollector:
    def test_collect_without_db(self):
        collector = IssueCollector(source="cli")
        info = collector.collect()
        assert info.python_version != ""
        assert info.os_info != ""
        assert info.source == "cli"
        assert info.timestamp != ""

    def test_collect_with_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE jobs (job_id TEXT, cluster TEXT)"
            )
            conn.execute(
                "INSERT INTO jobs VALUES ('1001', 'test-cluster')"
            )
            conn.execute(
                "CREATE TABLE node_state (cluster TEXT, node TEXT)"
            )
            conn.execute(
                "INSERT INTO node_state VALUES ('cluster-a', 'n01')"
            )
            conn.execute(
                "INSERT INTO node_state VALUES ('cluster-b', 'n02')"
            )
            conn.execute(
                "CREATE TABLE gpu_state (node TEXT, gpu_id INT)"
            )
            conn.commit()
            conn.close()

            collector = IssueCollector(db_path=db_path, source="dashboard")
            info = collector.collect()
            assert info.source == "dashboard"
            assert "gpu" in info.active_collectors
            assert "slurm" in info.active_collectors
            assert info.cluster_count == 2
            assert info.db_size_mb > 0
            assert info.record_count == 1
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_collect_with_config(self):
        config = {
            "issue_reporting": {
                "institution_name": "MIT",
                "contact_email": "hpc@mit.edu",
            }
        }
        collector = IssueCollector(config=config)
        info = collector.collect()
        assert info.institution == "MIT"
        assert info.submitted_by == "hpc@mit.edu"


# ── IssueFormatter Tests ──────────────────────────────────────────


class TestIssueFormatter:
    def setup_method(self):
        self.sys_info = SystemInfo(
            nomad_version="1.3.0",
            python_version="3.11.4",
            os_info="Linux 5.14.0",
            source="cli",
            timestamp="2026-04-07T10:00:00",
        )
        self.formatter = IssueFormatter(system_info=self.sys_info)

    def test_format_bug(self):
        body = self.formatter.format_bug(
            title="Crash on startup",
            component="cli",
            description="NØMAÐ crashes when run without config",
            steps="1. Remove nomad.toml\n2. Run nomad status",
            expected="Graceful error message",
            actual="Traceback with KeyError",
        )
        assert "### Description" in body
        assert "### Steps to Reproduce" in body
        assert "### Expected Behavior" in body
        assert "### Actual Behavior" in body
        assert "### System Information" in body
        assert "1.3.0" in body

    def test_format_feature(self):
        body = self.formatter.format_feature(
            title="Add Prometheus export",
            component="collectors",
            problem="Need to integrate with existing monitoring",
            solution="Add --prometheus flag to nomad collect",
        )
        assert "### Problem Statement" in body
        assert "### Proposed Solution" in body

    def test_format_question(self):
        body = self.formatter.format_question(
            title="How to configure alerts",
            topic="alerts",
            question="How do I set custom thresholds?",
            tried="Checked nomad ref alerts",
        )
        assert "### Question" in body
        assert "### What I've Already Tried" in body

    def test_format_from_dict_bug(self):
        data = {
            "category": "bug",
            "title": "Test bug",
            "component": "cli",
            "description": "Something broke",
            "steps": "Run X",
            "expected": "Y",
            "actual": "Z",
        }
        title, body = self.formatter.format_from_dict(data)
        assert title == "[Bug] Test bug"
        assert "### Description" in body

    def test_format_from_dict_feature(self):
        data = {
            "category": "feature",
            "title": "New thing",
            "component": "dashboard",
            "problem": "Missing capability",
        }
        title, body = self.formatter.format_from_dict(data)
        assert title == "[Feature] New thing"
        assert "### Problem Statement" in body

    def test_format_from_dict_question(self):
        data = {
            "category": "question",
            "title": "How to X",
            "component": "alerts",
            "question": "How do I configure Y?",
        }
        title, body = self.formatter.format_from_dict(data)
        assert title == "[Question] How to X"

    def test_no_duplicate_prefix(self):
        data = {
            "category": "bug",
            "title": "[Bug] Already prefixed",
            "component": "cli",
            "description": "Test",
            "steps": "Test",
            "expected": "Test",
            "actual": "Test",
        }
        title, _ = self.formatter.format_from_dict(data)
        assert title == "[Bug] Already prefixed"
        assert not title.startswith("[Bug] [Bug]")

    def test_categories_and_components(self):
        assert "bug" in CATEGORIES
        assert "feature" in CATEGORIES
        assert "question" in CATEGORIES
        assert "collectors" in COMPONENTS
        assert "console" in COMPONENTS
        assert "tessera" in COMPONENTS


# ── GitHubClient Tests ────────────────────────────────────────────


class TestGitHubClient:
    def test_has_token(self):
        client = GitHubClient(token="ghp_test")
        assert client.has_token is True

        client_no_token = GitHubClient()
        assert client_no_token.has_token is False

    def test_generate_browser_url(self):
        client = GitHubClient()
        url = client.generate_browser_url(
            title="Test issue",
            body="Some description",
            category="bug",
        )
        assert "github.com/jtonini/nomad-hpc/issues/new" in url
        assert "template=bug_report.yml" in url
        assert "Test+issue" in url or "Test%20issue" in url

    def test_generate_browser_url_truncation(self):
        client = GitHubClient()
        long_body = "x" * 10000
        url = client.generate_browser_url(title="Test", body=long_body)
        # URL should contain truncation notice
        assert "truncated" in url

    def test_generate_email_body(self):
        client = GitHubClient()
        subject, body = client.generate_email_body(
            "Test issue", "Description here", "bug"
        )
        assert "[NØMAÐ Bug]" in subject
        assert "Test issue" in subject
        assert "Description here" in body
        assert "nomad issue --email" in body

    def test_build_labels(self):
        client = GitHubClient()
        labels = client._build_labels(
            category="bug",
            component="alerts",
            version="1.3.0",
            institution="University of Richmond",
            source="console",
        )
        assert "bug" in labels
        assert "component:alerts" in labels
        assert "v1.3.0" in labels
        assert "source:console" in labels
        assert "inst:university-of-richmond" in labels

    def test_build_labels_minimal(self):
        client = GitHubClient()
        labels = client._build_labels(
            category="feature",
            component="other",
            version="unknown",
            institution="",
            source="cli",
        )
        assert "enhancement" in labels
        assert "component:other" in labels
        assert "source:cli" in labels
        # version "unknown" should be excluded
        assert not any(l.startswith("v") for l in labels)

    def test_create_issue_no_token_returns_browser(self):
        client = GitHubClient()
        result = client.create_issue(
            title="Test", body="Body", category="bug"
        )
        assert not result.success
        assert result.method == "browser"
        assert "github.com" in result.url

    @patch("urllib.request.urlopen")
    def test_search_duplicates_empty(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"items": []}
        ).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GitHubClient()
        results = client.search_duplicates("nonexistent thing")
        assert results == []

    @patch("urllib.request.urlopen")
    def test_search_duplicates_found(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "items": [
                {
                    "number": 42,
                    "title": "Disk collector fails on ZFS",
                    "html_url": "https://github.com/jtonini/nomad-hpc/issues/42",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "created_at": "2026-03-15T10:00:00Z",
                    "comments": 3,
                }
            ]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = GitHubClient()
        results = client.search_duplicates("disk ZFS")
        assert len(results) == 1
        assert results[0].number == 42
        assert results[0].title == "Disk collector fails on ZFS"


# ── Integration-Style Tests ───────────────────────────────────────


class TestEndToEnd:
    def test_full_bug_report_flow(self):
        """Test collecting info, formatting, and generating URL."""
        # Collect
        collector = IssueCollector(source="cli")
        sys_info = collector.collect()

        # Format
        formatter = IssueFormatter(system_info=sys_info)
        data = {
            "category": "bug",
            "title": "Dashboard crash on empty DB",
            "component": "dashboard",
            "description": "Dashboard crashes when database has no data",
            "steps": "1. Create empty DB\n2. Run nomad dashboard",
            "expected": "Empty dashboard with no data message",
            "actual": "Python traceback: KeyError 'jobs'",
        }
        title, body = formatter.format_from_dict(data)

        # Generate URL
        client = GitHubClient()
        url = client.generate_browser_url(title, body, "bug")

        assert "[Bug]" in title
        assert "Dashboard crash" in title
        assert "### Description" in body
        assert "### System Information" in body
        assert "github.com" in url

    def test_full_feature_request_flow(self):
        collector = IssueCollector(source="dashboard")
        sys_info = collector.collect()

        formatter = IssueFormatter(system_info=sys_info)
        data = {
            "category": "feature",
            "title": "Prometheus export",
            "component": "collectors",
            "problem": "Need to integrate with Prometheus",
            "solution": "Add /metrics endpoint",
        }
        title, body = formatter.format_from_dict(data)

        assert "[Feature]" in title
        assert "### Problem Statement" in body
        assert "dashboard" in body  # source

    def test_json_output(self):
        collector = IssueCollector(source="cli")
        sys_info = collector.collect()
        d = sys_info.to_dict()
        serialized = json.dumps(d)
        assert json.loads(serialized) == d
