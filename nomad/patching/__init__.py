# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Patching Framework

A structured approach to code patching that addresses:
- Maintainability: Patch content in separate files with proper syntax highlighting
- DRY: Reusable Patcher class reduces code duplication
- Testability: Mock-friendly design for unit testing

Usage:
    from nomad.patching import Patcher, Patch

    patcher = Patcher(base_dir='/path/to/nomad')
    patcher.add(Patch(
        file='collectors/node_state.py',
        name='add_cluster_column',
        old='self.nodes = config.get(...)',
        new='self.nodes = config.get(...)\\nself.cluster_name = ...',
    ))
    results = patcher.apply()
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Patch:
    """A single code patch to apply."""
    file: str                          # Relative path from base_dir
    name: str                          # Human-readable patch name
    old: str                           # Text to find (must be unique)
    new: str                           # Replacement text
    skip_if_present: str = ""          # Skip if this text already exists
    required: bool = True              # Fail if patch can't be applied
    validator: Callable[[str], bool] | None = None  # Optional validation

    def should_skip(self, content: str) -> bool:
        """Check if patch should be skipped (already applied)."""
        if self.skip_if_present and self.skip_if_present in content:
            return True
        return False

    def can_apply(self, content: str) -> bool:
        """Check if the old text exists exactly once."""
        return content.count(self.old) == 1

    def apply(self, content: str) -> str:
        """Apply the patch and return new content."""
        return content.replace(self.old, self.new, 1)


@dataclass
class PatchResult:
    """Result of applying a single patch."""
    patch: Patch
    success: bool
    skipped: bool = False
    error: str = ""


@dataclass
class PatcherResult:
    """Result of applying all patches."""
    results: list[PatchResult] = field(default_factory=list)
    backup_paths: dict[str, Path] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return all(r.success or r.skipped for r in self.results)

    @property
    def applied(self) -> list[PatchResult]:
        return [r for r in self.results if r.success and not r.skipped]

    @property
    def skipped(self) -> list[PatchResult]:
        return [r for r in self.results if r.skipped]

    @property
    def failed(self) -> list[PatchResult]:
        return [r for r in self.results if not r.success and not r.skipped]

    def summary(self) -> str:
        lines = []
        if self.applied:
            lines.append(f"Applied: {len(self.applied)}")
            for r in self.applied:
                lines.append(f"  ✓ {r.patch.name}")
        if self.skipped:
            lines.append(f"Skipped (already applied): {len(self.skipped)}")
            for r in self.skipped:
                lines.append(f"  = {r.patch.name}")
        if self.failed:
            lines.append(f"Failed: {len(self.failed)}")
            for r in self.failed:
                lines.append(f"  ✗ {r.patch.name}: {r.error}")
        return "\n".join(lines)


class Patcher:
    """
    Applies a series of patches to source files.

    Features:
    - Automatic backups before modification
    - Idempotent: skips already-applied patches
    - Validates patches can be applied before modifying
    - Dry-run mode for testing
    - Rollback support
    """

    def __init__(
        self,
        base_dir: str | Path,
        backup: bool = True,
        backup_suffix: str = '.bak',
    ):
        self.base_dir = Path(base_dir)
        self.backup = backup
        self.backup_suffix = backup_suffix
        self.patches: list[Patch] = []
        self._file_cache: dict[str, str] = {}

    def add(self, patch: Patch) -> Patcher:
        """Add a patch to the queue. Returns self for chaining."""
        self.patches.append(patch)
        return self

    def add_all(self, patches: list[Patch]) -> Patcher:
        """Add multiple patches. Returns self for chaining."""
        self.patches.extend(patches)
        return self

    def _get_content(self, file: str) -> str:
        """Get file content, using cache for multiple patches to same file."""
        if file not in self._file_cache:
            path = self.base_dir / file
            if not path.exists():
                raise FileNotFoundError(f"File not found: {path}")
            self._file_cache[file] = path.read_text()
        return self._file_cache[file]

    def _set_content(self, file: str, content: str):
        """Update cached content."""
        self._file_cache[file] = content

    def validate(self) -> list[str]:
        """
        Validate all patches can be applied.
        Returns list of error messages (empty if all valid).
        """
        errors = []
        self._file_cache.clear()

        for patch in self.patches:
            try:
                content = self._get_content(patch.file)
            except FileNotFoundError as e:
                if patch.required:
                    errors.append(f"{patch.name}: {e}")
                continue

            if patch.should_skip(content):
                continue

            if not patch.can_apply(content):
                count = content.count(patch.old)
                if count == 0:
                    errors.append(f"{patch.name}: Target text not found in {patch.file}")
                else:
                    errors.append(f"{patch.name}: Target text found {count} times (must be unique)")

            # Apply to cache for subsequent patches to same file
            if patch.can_apply(content):
                self._set_content(patch.file, patch.apply(content))

        return errors

    def dry_run(self) -> PatcherResult:
        """
        Simulate applying patches without modifying files.
        Returns what would happen.
        """
        result = PatcherResult()
        self._file_cache.clear()

        for patch in self.patches:
            try:
                content = self._get_content(patch.file)
            except FileNotFoundError as e:
                result.results.append(PatchResult(
                    patch=patch,
                    success=not patch.required,
                    error=str(e),
                ))
                continue

            if patch.should_skip(content):
                result.results.append(PatchResult(
                    patch=patch,
                    success=True,
                    skipped=True,
                ))
                continue

            if patch.can_apply(content):
                self._set_content(patch.file, patch.apply(content))
                result.results.append(PatchResult(patch=patch, success=True))
            else:
                result.results.append(PatchResult(
                    patch=patch,
                    success=False,
                    error="Target text not found or not unique",
                ))

        return result

    def apply(self) -> PatcherResult:
        """
        Apply all patches.
        Creates backups, applies patches, writes files.
        """
        # First validate
        errors = self.validate()
        if errors:
            result = PatcherResult()
            for err in errors:
                # Find the patch name from error message
                name = err.split(":")[0] if ":" in err else "unknown"
                patch = next((p for p in self.patches if p.name == name), None)
                if patch:
                    result.results.append(PatchResult(
                        patch=patch,
                        success=False,
                        error=err,
                    ))
            return result

        # Reset cache and apply for real
        self._file_cache.clear()
        result = PatcherResult()
        files_to_write: dict[str, str] = {}

        for patch in self.patches:
            try:
                content = self._get_content(patch.file)
            except FileNotFoundError as e:
                result.results.append(PatchResult(
                    patch=patch,
                    success=not patch.required,
                    error=str(e),
                ))
                continue

            if patch.should_skip(content):
                result.results.append(PatchResult(
                    patch=patch,
                    success=True,
                    skipped=True,
                ))
                continue

            new_content = patch.apply(content)
            self._set_content(patch.file, new_content)
            files_to_write[patch.file] = new_content
            result.results.append(PatchResult(patch=patch, success=True))

        # Create backups and write files
        for file, content in files_to_write.items():
            path = self.base_dir / file
            if self.backup:
                backup_path = path.with_suffix(path.suffix + self.backup_suffix)
                if not backup_path.exists():  # Don't overwrite existing backup
                    shutil.copy(path, backup_path)
                    result.backup_paths[file] = backup_path

            path.write_text(content)
            logger.info(f"Patched: {file}")

        return result

    def rollback(self, result: PatcherResult):
        """Restore files from backups."""
        for file, backup_path in result.backup_paths.items():
            path = self.base_dir / file
            if backup_path.exists():
                shutil.copy(backup_path, path)
                logger.info(f"Rolled back: {file}")


def load_patch_content(patch_file: Path) -> str:
    """
    Load patch content from a separate file.
    
    This enables proper syntax highlighting and linting of patch content
    by keeping it in .py files rather than embedded strings.
    """
    return patch_file.read_text()


# Convenience function for simple cases
def apply_patches(base_dir: str | Path, patches: list[Patch]) -> PatcherResult:
    """Apply a list of patches to a directory."""
    patcher = Patcher(base_dir)
    patcher.add_all(patches)
    return patcher.apply()
