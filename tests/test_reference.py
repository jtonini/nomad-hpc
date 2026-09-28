"""Tests for NØMAÐ reference system.

Tests the knowledge base, formatter, search engine, and CLI commands.
"""

import os
import tempfile
from pathlib import Path

import pytest

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def sample_yaml(tmp_path):
    """Create a sample YAML knowledge base for testing."""
    yaml_content = """
entries:
  test.command:
    title: "Test Command"
    summary: "A test command for unit testing."
    description: "This is a longer description of the test command."
    source_files:
      - "nomad/test.py"
    config_section: "test"
    config_keys:
      - "test.enabled - Enable testing"
      - "test.verbose - Verbose output"
    related:
      - "nomad status"
    examples:
      - "nomad test --flag"
      - "nomad test --verbose"
    math: |
      E = mc^2
      F = ma
    see_also:
      - "test.subcommand"
    tags:
      - test
      - example
    category: commands

  test.subcommand:
    title: "Test Subcommand"
    summary: "A subcommand under the test group."
    description: "Does sub-testing."
    source_files:
      - "nomad/test_sub.py"
    tags:
      - test
      - subcommand
    category: commands

  concept.diversity:
    title: "Diversity Index"
    summary: "Simpson's diversity index for resource analysis."
    description: "Measures workload diversity using ecological indices."
    math: |
      Simpson's D = 1 - sum(p_i^2)
    tags:
      - diversity
      - simpson
      - ecology
    category: concepts
"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(yaml_content)
    return tmp_path


@pytest.fixture
def knowledge_base(sample_yaml):
    """Create a KnowledgeBase loaded from sample YAML."""
    from nomad.reference.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(entries_dir=str(sample_yaml))
    return kb


# ── KnowledgeBase Tests ──────────────────────────────────────────────


class TestKnowledgeBase:
    """Test the KnowledgeBase class."""

    def test_load_entries(self, knowledge_base):
        """Entries load from YAML files."""
        assert knowledge_base.entry_count == 3

    def test_get_exact(self, knowledge_base):
        """Exact key lookup works."""
        entry = knowledge_base.get("test.command")
        assert entry is not None
        assert entry.title == "Test Command"
        assert entry.summary == "A test command for unit testing."

    def test_get_not_found(self, knowledge_base):
        """Missing key returns None."""
        assert knowledge_base.get("nonexistent") is None

    def test_get_source_files(self, knowledge_base):
        """Source files are loaded."""
        entry = knowledge_base.get("test.command")
        assert "nomad/test.py" in entry.source_files

    def test_get_config(self, knowledge_base):
        """Config section and keys are loaded."""
        entry = knowledge_base.get("test.command")
        assert entry.config_section == "test"
        assert len(entry.config_keys) == 2

    def test_get_math(self, knowledge_base):
        """Mathematical basis is loaded."""
        entry = knowledge_base.get("test.command")
        assert "E = mc^2" in entry.math

    def test_get_examples(self, knowledge_base):
        """Examples are loaded."""
        entry = knowledge_base.get("test.command")
        assert len(entry.examples) == 2

    def test_get_children(self, knowledge_base):
        """Children retrieval works for prefixes."""
        children = knowledge_base.get_children("test")
        assert len(children) == 2

    def test_list_topics(self, knowledge_base):
        """List all topics."""
        topics = knowledge_base.list_topics()
        assert len(topics) == 3

    def test_list_topics_by_category(self, knowledge_base):
        """Filter topics by category."""
        commands = knowledge_base.list_topics(category="commands")
        assert len(commands) == 2
        concepts = knowledge_base.list_topics(category="concepts")
        assert len(concepts) == 1

    def test_categories(self, knowledge_base):
        """List unique categories."""
        cats = knowledge_base.categories()
        assert "commands" in cats
        assert "concepts" in cats

    def test_search_basic(self, knowledge_base):
        """Basic search by keyword."""
        results = knowledge_base.search("diversity")
        assert len(results) >= 1
        assert results[0].key == "concept.diversity"

    def test_search_key_boost(self, knowledge_base):
        """Search boosts exact key matches."""
        results = knowledge_base.search("test.command")
        assert results[0].key == "test.command"

    def test_search_no_results(self, knowledge_base):
        """Search returns empty for no matches."""
        results = knowledge_base.search("xyznonexistent123")
        assert len(results) == 0

    def test_searchable_text(self, knowledge_base):
        """Searchable text includes all fields."""
        entry = knowledge_base.get("test.command")
        text = entry.searchable_text()
        assert "test" in text
        assert "unit testing" in text
        assert "mc^2" in text

    def test_tags_in_search(self, knowledge_base):
        """Tags are searchable."""
        results = knowledge_base.search("ecology")
        assert any(r.key == "concept.diversity" for r in results)


class TestKnowledgeBaseBuiltin:
    """Test fallback to built-in entries when no YAML available."""

    def test_builtin_fallback(self):
        """Built-in entries load when YAML directory doesn't exist."""
        from nomad.reference.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(entries_dir="/nonexistent/path")
        assert kb.entry_count >= 1
        entry = kb.get("ref")
        assert entry is not None


# ── Formatter Tests ──────────────────────────────────────────────────


class TestReferenceFormatter:
    """Test the CLI output formatter."""

    def test_format_entry(self, knowledge_base):
        """Format a full entry."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        entry = knowledge_base.get("test.command")
        output = fmt.format_entry(entry)
        assert "Test Command" in output
        assert "nomad/test.py" in output
        assert "E = mc^2" in output
        assert "nomad test --flag" in output

    def test_format_entry_no_color(self, knowledge_base):
        """Format without ANSI colors."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        entry = knowledge_base.get("test.command")
        output = fmt.format_entry(entry)
        assert "\033[" not in output

    def test_format_entry_with_color(self, knowledge_base):
        """Format with ANSI colors."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=True)
        entry = knowledge_base.get("test.command")
        output = fmt.format_entry(entry)
        assert "\033[" in output

    def test_format_topic_list(self, knowledge_base):
        """Format a topic list."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        entries = knowledge_base.list_topics()
        output = fmt.format_topic_list(entries, heading="All Topics")
        assert "All Topics" in output
        assert "test.command" in output

    def test_format_search_results(self, knowledge_base):
        """Format search results."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        results = knowledge_base.search("test")
        output = fmt.format_search_results("test", results)
        assert "test" in output

    def test_format_search_no_results(self):
        """Format empty search results."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        output = fmt.format_search_results("xyznonexistent", [])
        assert "No results found" in output

    def test_format_index(self, knowledge_base):
        """Format the top-level index."""
        from nomad.reference.formatter import ReferenceFormatter

        fmt = ReferenceFormatter(color=False)
        categories = {}
        for entry in knowledge_base.list_topics():
            cat = entry.category or "other"
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(entry)
        output = fmt.format_index(categories)
        assert "NØMAÐ Reference" in output
        assert "Commands" in output
        assert "Concepts" in output


# ── Search Engine Tests ──────────────────────────────────────────────


class TestSearchEngine:
    """Test the search engine."""

    def test_search_with_scores(self, knowledge_base):
        """Search returns entries with scores."""
        from nomad.reference.search import SearchEngine

        engine = SearchEngine(knowledge_base)
        results = engine.search("diversity simpson")
        assert len(results) >= 1
        entry, score = results[0]
        assert entry.key == "concept.diversity"
        assert score > 0

    def test_search_key_boost(self, knowledge_base):
        """Key matches get boosted scores."""
        from nomad.reference.search import SearchEngine

        engine = SearchEngine(knowledge_base)
        results = engine.search("test.command")
        assert len(results) >= 1
        entry, score = results[0]
        assert entry.key == "test.command"
        assert score >= 5.0  # key match boost

    def test_search_partial_match(self, knowledge_base):
        """Partial token matches work."""
        from nomad.reference.search import SearchEngine

        engine = SearchEngine(knowledge_base)
        results = engine.search("divers")  # partial match for "diversity"
        assert len(results) >= 1

    def test_search_max_results(self, knowledge_base):
        """Max results is respected."""
        from nomad.reference.search import SearchEngine

        engine = SearchEngine(knowledge_base)
        results = engine.search("test", max_results=1)
        assert len(results) <= 1

    def test_search_empty_query(self, knowledge_base):
        """Empty query returns nothing."""
        from nomad.reference.search import SearchEngine

        engine = SearchEngine(knowledge_base)
        results = engine.search("")
        assert len(results) == 0


# ── ReferenceEntry Tests ─────────────────────────────────────────────


class TestReferenceEntry:
    """Test the ReferenceEntry data model."""

    def test_from_dict(self):
        """Create entry from dictionary."""
        from nomad.reference.knowledge_base import ReferenceEntry

        data = {
            "title": "Test",
            "summary": "A test entry.",
            "source_files": ["test.py"],
            "tags": ["test"],
            "category": "commands",
        }
        entry = ReferenceEntry.from_dict("test.key", data)
        assert entry.key == "test.key"
        assert entry.title == "Test"
        assert entry.summary == "A test entry."
        assert "test.py" in entry.source_files

    def test_from_dict_defaults(self):
        """Missing fields get defaults."""
        from nomad.reference.knowledge_base import ReferenceEntry

        data = {"title": "Minimal", "summary": "Bare minimum."}
        entry = ReferenceEntry.from_dict("min", data)
        assert entry.source_files == []
        assert entry.config_keys == []
        assert entry.examples == []
        assert entry.math == ""
        assert entry.category == ""

    def test_searchable_text(self):
        """Searchable text combines all fields."""
        from nomad.reference.knowledge_base import ReferenceEntry

        entry = ReferenceEntry(
            key="test",
            title="Test Title",
            summary="Test summary",
            description="Test description",
            math="E = mc^2",
            tags=["alpha", "beta"],
        )
        text = entry.searchable_text()
        assert "test title" in text
        assert "test summary" in text
        assert "e = mc^2" in text
        assert "alpha" in text


# ── Integration Tests ────────────────────────────────────────────────


class TestIntegration:
    """Integration tests using the actual YAML knowledge base."""

    @pytest.fixture
    def real_kb(self):
        """Load the actual knowledge base if available."""
        from nomad.reference.knowledge_base import KnowledgeBase

        entries_dir = Path(__file__).parent.parent / "nomad" / "reference" / "entries"
        if not entries_dir.exists():
            pytest.skip("YAML entries not found")
        kb = KnowledgeBase(entries_dir=str(entries_dir))
        return kb

    def test_real_kb_loads(self, real_kb):
        """Real knowledge base loads successfully."""
        assert real_kb.entry_count > 0

    def test_real_kb_has_dyn(self, real_kb):
        """Real KB has dynamics entries."""
        entry = real_kb.get("dyn")
        assert entry is not None
        assert "dynamics" in entry.title.lower() or "dynamics" in entry.summary.lower()

    def test_real_kb_has_collectors(self, real_kb):
        """Real KB has collector entries."""
        entry = real_kb.get("collectors")
        assert entry is not None

    def test_real_kb_search_regime(self, real_kb):
        """Real KB search finds regime divergence."""
        results = real_kb.search("regime divergence")
        assert len(results) > 0
