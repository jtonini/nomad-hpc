"""CLI output formatter for NØMAÐ reference entries.

Renders ReferenceEntry objects as rich terminal output with
sections, indentation, and visual structure.
"""

from __future__ import annotations

from typing import Optional

from nomad.reference.knowledge_base import ReferenceEntry


class ReferenceFormatter:
    """Formats reference entries for CLI display."""

    def __init__(self, width: int = 78, color: bool = True):
        self.width = width
        self.color = color

    def _bold(self, text: str) -> str:
        if self.color:
            return f"\033[1m{text}\033[0m"
        return text

    def _dim(self, text: str) -> str:
        if self.color:
            return f"\033[2m{text}\033[0m"
        return text

    def _cyan(self, text: str) -> str:
        if self.color:
            return f"\033[36m{text}\033[0m"
        return text

    def _yellow(self, text: str) -> str:
        if self.color:
            return f"\033[33m{text}\033[0m"
        return text

    def _green(self, text: str) -> str:
        if self.color:
            return f"\033[32m{text}\033[0m"
        return text

    def format_entry(self, entry: ReferenceEntry) -> str:
        """Format a full reference entry for terminal display."""
        lines: list[str] = []

        # Title and underline
        lines.append("")
        lines.append(self._bold(entry.title))
        lines.append("=" * len(entry.title))

        # Summary
        if entry.summary:
            lines.append(entry.summary)

        # Description
        if entry.description:
            lines.append("")
            lines.append(self._wrap(entry.description))

        # Source files
        if entry.source_files:
            lines.append("")
            lines.append(self._cyan("Source:") + "  " + ", ".join(entry.source_files))

        # Configuration
        if entry.config_section:
            lines.append(
                self._cyan("Config:") + "  " + f"nomad.toml [{entry.config_section}]"
            )
        if entry.config_keys:
            lines.append("")
            lines.append(self._bold("Configuration Keys:"))
            for key in entry.config_keys:
                lines.append(f"  {key}")

        # Related
        if entry.related:
            lines.append(
                self._cyan("Related:") + " " + ", ".join(entry.related)
            )

        # Mathematical basis
        if entry.math:
            lines.append("")
            lines.append(self._bold("Mathematical Basis:"))
            for math_line in entry.math.strip().split("\n"):
                lines.append(f"  {math_line}")

        # Examples
        if entry.examples:
            lines.append("")
            lines.append(self._bold("Examples:"))
            for example in entry.examples:
                lines.append(f"  {self._green(example)}")

        # See also
        if entry.see_also:
            lines.append("")
            lines.append(
                self._dim("See also: " + ", ".join(
                    f"nomad ref {s}" for s in entry.see_also
                ))
            )

        lines.append("")
        return "\n".join(lines)

    def format_topic_list(
        self,
        entries: list[ReferenceEntry],
        heading: str | None = None,
    ) -> str:
        """Format a list of topics for browsing."""
        lines: list[str] = []

        if heading:
            lines.append("")
            lines.append(self._bold(heading))
            lines.append("-" * len(heading))

        max_key = max((len(e.key) for e in entries), default=20)
        for entry in entries:
            key_str = self._cyan(entry.key.ljust(max_key))
            lines.append(f"  {key_str}  {entry.summary}")

        lines.append("")
        return "\n".join(lines)

    def format_search_results(
        self, query: str, results: list[ReferenceEntry]
    ) -> str:
        """Format search results."""
        lines: list[str] = []
        lines.append("")

        if not results:
            lines.append(f"No results found for '{query}'.")
            lines.append("")
            lines.append("Try a broader search or 'nomad ref' to browse topics.")
            lines.append("")
            return "\n".join(lines)

        lines.append(
            self._bold(f"Search results for '{query}' ({len(results)} found):")
        )
        lines.append("")

        for entry in results:
            cat_str = f" [{entry.category}]" if entry.category else ""
            lines.append(
                f"  {self._cyan(entry.key)}{self._dim(cat_str)}"
            )
            lines.append(f"    {entry.summary}")
            lines.append("")

        lines.append(
            self._dim("Use 'nomad ref <topic>' for full details.")
        )
        lines.append("")
        return "\n".join(lines)

    def format_index(self, categories: dict) -> str:
        """Format the top-level index (nomad ref with no arguments)."""
        lines: list[str] = []

        lines.append("")
        lines.append(self._bold("NØMAÐ Reference"))
        lines.append("=" * 15)
        lines.append(
            "Built-in documentation for commands, modules, configuration, and concepts."
        )
        lines.append("")
        lines.append(self._bold("Usage:"))
        lines.append(f"  {self._green('nomad ref <topic>')}"
                      "          Look up a topic")
        lines.append(f"  {self._green('nomad ref <topic> <subtopic>')}"
                      " Look up a subtopic")
        lines.append(f"  {self._green('nomad ref search <query>')}"
                      "    Search all documentation")
        lines.append("")

        for cat_name, entries in sorted(categories.items()):
            lines.append(self._bold(cat_name.title()))
            max_key = max((len(e.key) for e in entries), default=20)
            for entry in entries:
                key_display = entry.key.ljust(max_key)
                lines.append(f"  {self._cyan(key_display)}  {entry.summary}")
            lines.append("")

        return "\n".join(lines)

    def _wrap(self, text: str, indent: int = 0) -> str:
        """Simple word wrap to terminal width."""
        import textwrap

        return textwrap.fill(
            text,
            width=self.width,
            initial_indent=" " * indent,
            subsequent_indent=" " * indent,
        )
