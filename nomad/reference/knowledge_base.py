"""Knowledge base loader and data model for NØMAÐ reference system.

Loads structured YAML entries from the entries/ directory and provides
lookup by topic path (e.g., "dyn.diversity", "collectors.disk").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class ReferenceEntry:
    """A single reference entry in the knowledge base."""

    key: str  # dot-separated path, e.g. "dyn.diversity"
    title: str
    summary: str
    description: str = ""
    source_files: list[str] = field(default_factory=list)
    config_section: str = ""
    config_keys: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    math: str = ""
    see_also: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""  # commands, concepts, config, collectors, etc.

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> ReferenceEntry:
        """Create a ReferenceEntry from a YAML dictionary."""
        return cls(
            key=key,
            title=data.get("title", key),
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            source_files=data.get("source_files", []),
            config_section=data.get("config_section", ""),
            config_keys=data.get("config_keys", []),
            related=data.get("related", []),
            examples=data.get("examples", []),
            math=data.get("math", ""),
            see_also=data.get("see_also", []),
            tags=data.get("tags", []),
            category=data.get("category", ""),
        )

    def searchable_text(self) -> str:
        """Return all text content for full-text search."""
        parts = [
            self.key,
            self.title,
            self.summary,
            self.description,
            self.math,
            self.config_section,
            " ".join(self.tags),
            " ".join(self.config_keys),
            " ".join(self.examples),
        ]
        return " ".join(parts).lower()


class KnowledgeBase:
    """Loads and queries the NØMAÐ reference knowledge base.

    The knowledge base is a collection of YAML files in the entries/
    directory. Each file contains a mapping of entry keys to their
    structured content.

    Usage:
        kb = KnowledgeBase()
        entry = kb.get("dyn.diversity")
        entries = kb.list_topics()
        results = kb.search("simpson diversity")
    """

    def __init__(self, entries_dir: str | None = None):
        self._entries: dict[str, ReferenceEntry] = {}
        self._loaded = False

        if entries_dir is None:
            self._entries_dir = Path(__file__).parent / "entries"
        else:
            self._entries_dir = Path(entries_dir)

    def _ensure_loaded(self) -> None:
        """Lazy-load entries on first access."""
        if self._loaded:
            return
        self._load_all()
        self._loaded = True

    def _load_all(self) -> None:
        """Load all YAML files from the entries directory."""
        if yaml is None:
            # Fallback: try to load from built-in entries
            self._load_builtin()
            return

        if not self._entries_dir.exists():
            self._load_builtin()
            return

        for yaml_file in sorted(self._entries_dir.glob("*.yaml")):
            self._load_file(yaml_file)

    def _load_file(self, path: Path) -> None:
        """Load entries from a single YAML file."""
        if yaml is None:
            return

        with open(path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            return

        # Each top-level key in the YAML is an entry
        entries = data.get("entries", data)
        if not isinstance(entries, dict):
            return

        for key, entry_data in entries.items():
            if isinstance(entry_data, dict):
                self._entries[key] = ReferenceEntry.from_dict(key, entry_data)

    def _load_builtin(self) -> None:
        """Load minimal built-in entries when YAML files are unavailable."""
        # Provides basic reference even without YAML files
        self._entries["ref"] = ReferenceEntry(
            key="ref",
            title="NØMAÐ Reference System",
            summary="Built-in documentation and code navigation for NØMAÐ.",
            description=(
                "The reference system provides rich documentation for all "
                "NØMAÐ commands, modules, configuration options, and concepts. "
                "Use 'nomad ref <topic>' to look up any topic, or "
                "'nomad ref search <query>' to search across all documentation."
            ),
            category="commands",
            tags=["help", "documentation", "reference"],
        )

    def get(self, key: str) -> ReferenceEntry | None:
        """Look up a reference entry by key.

        Supports dot-separated paths (e.g., "dyn.diversity") and
        also simple lookups (e.g., "alerts").
        """
        self._ensure_loaded()

        # Exact match
        if key in self._entries:
            return self._entries[key]

        # Try with common prefixes
        for prefix in ["commands.", "concepts.", "collectors.", "config."]:
            prefixed = prefix + key
            if prefixed in self._entries:
                return self._entries[prefixed]

        return None

    def get_children(self, prefix: str) -> list[ReferenceEntry]:
        """Get all entries whose key starts with the given prefix."""
        self._ensure_loaded()
        prefix_dot = prefix if prefix.endswith(".") else prefix + "."
        return [
            entry
            for key, entry in sorted(self._entries.items())
            if key.startswith(prefix_dot)
        ]

    def list_topics(self, category: str | None = None) -> list[ReferenceEntry]:
        """List all available topics, optionally filtered by category."""
        self._ensure_loaded()
        entries = list(self._entries.values())
        if category:
            entries = [e for e in entries if e.category == category]
        return sorted(entries, key=lambda e: e.key)

    def categories(self) -> list[str]:
        """List all unique categories."""
        self._ensure_loaded()
        cats = set(e.category for e in self._entries.values() if e.category)
        return sorted(cats)

    def search(self, query: str, max_results: int = 10) -> list[ReferenceEntry]:
        """Full-text search across all entries.

        Uses simple token matching with scoring. Returns entries
        sorted by relevance (number of matching tokens).
        """
        self._ensure_loaded()
        tokens = query.lower().split()
        if not tokens:
            return []

        scored: list[tuple] = []
        for entry in self._entries.values():
            text = entry.searchable_text()
            score = sum(1 for t in tokens if t in text)
            # Boost exact key matches
            if query.lower() in entry.key.lower():
                score += 5
            if query.lower() in entry.title.lower():
                score += 3
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:max_results]]

    @property
    def entry_count(self) -> int:
        """Number of loaded entries."""
        self._ensure_loaded()
        return len(self._entries)
