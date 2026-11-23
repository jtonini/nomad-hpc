#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Add SPDX license headers to all Python source files.

Addresses reviewer concern: "Is it included in the source code? No."

Usage:
    python3 scripts/add_license_headers.py /path/to/nomad/nomad/
    python3 scripts/add_license_headers.py /path/to/nomad/nomad/ --dry-run
"""

import sys
from pathlib import Path

HEADER = '''# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
'''

SKIP_PATTERNS = [
    '__pycache__',
    '.egg-info',
    'build/',
    'dist/',
    '.git/',
    'venv/',
]


def should_skip(path: Path) -> bool:
    """Check if file should be skipped."""
    path_str = str(path)
    return any(pattern in path_str for pattern in SKIP_PATTERNS)


def has_header(content: str) -> bool:
    """Check if file already has SPDX header."""
    return 'SPDX-License-Identifier' in content[:500]


def add_header(path: Path, dry_run: bool = False) -> bool:
    """
    Add license header to a Python file.
    Returns True if file was modified.
    """
    content = path.read_text()
    
    if has_header(content):
        return False
    
    # Handle shebang and encoding declarations
    lines = content.split('\n')
    insert_at = 0
    
    # Preserve shebang
    if lines and lines[0].startswith('#!'):
        insert_at = 1
    
    # Preserve encoding declaration
    if len(lines) > insert_at and lines[insert_at].startswith('# -*-'):
        insert_at += 1
    
    # Insert header
    new_lines = lines[:insert_at] + HEADER.strip().split('\n') + lines[insert_at:]
    new_content = '\n'.join(new_lines)
    
    if not dry_run:
        path.write_text(new_content)
    
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/add_license_headers.py /path/to/nomad/ [--dry-run]")
        sys.exit(1)
    
    base_dir = Path(sys.argv[1])
    dry_run = '--dry-run' in sys.argv
    
    if not base_dir.exists():
        print(f"ERROR: {base_dir} not found")
        sys.exit(1)
    
    print(f"\nAdding SPDX headers to Python files in {base_dir}")
    if dry_run:
        print("(DRY RUN - no files will be modified)")
    print("=" * 50)
    
    modified = 0
    skipped = 0
    already_has = 0
    
    for py_file in base_dir.rglob('*.py'):
        if should_skip(py_file):
            skipped += 1
            continue
        
        try:
            if add_header(py_file, dry_run):
                print(f"  + {py_file.relative_to(base_dir)}")
                modified += 1
            else:
                already_has += 1
        except Exception as e:
            print(f"  ! {py_file}: {e}")
    
    print()
    print(f"Modified:     {modified}")
    print(f"Already had:  {already_has}")
    print(f"Skipped:      {skipped}")
    
    if dry_run and modified > 0:
        print("\nRun without --dry-run to apply changes.")


if __name__ == '__main__':
    main()
