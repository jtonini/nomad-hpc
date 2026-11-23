#!/usr/bin/env python3
"""Integrate nomad ref into the NØMAÐ codebase.

Run from the NØMAÐ repo root (~/nomad/):
    python integrate_reference.py

This script:
1. Copies nomad/reference/ module into place
2. Patches nomad/cli.py to add the `ref` command group
3. Copies tests into place
"""
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent
CLI_PATH = REPO_ROOT / "nomad" / "cli.py"
REF_SRC = REPO_ROOT / "nomad" / "reference"
TEST_DIR = REPO_ROOT / "tests"


def main():
    # ── Step 1: Verify we're in the right directory ──────────────────
    if not CLI_PATH.exists():
        print(f"ERROR: {CLI_PATH} not found. Run from the NØMAÐ repo root.")
        sys.exit(1)

    print(f"Working in: {REPO_ROOT}")

    # ── Step 2: Verify reference module exists ───────────────────────
    if not REF_SRC.exists():
        print(f"ERROR: {REF_SRC} not found.")
        print("Copy the reference module into nomad/reference/ first.")
        sys.exit(1)

    entries_dir = REF_SRC / "entries"
    if not entries_dir.exists():
        print(f"ERROR: {entries_dir} not found.")
        sys.exit(1)

    yaml_count = len(list(entries_dir.glob("*.yaml")))
    print(f"Found {yaml_count} YAML entry files in {entries_dir}")

    # ── Step 3: Patch cli.py ─────────────────────────────────────────
    cli_content = CLI_PATH.read_text()

    needs_patch = True
    if "def ref(topic_parts):" in cli_content:
        print("cli.py already contains updated `ref` command -- skipping patch.")
        needs_patch = False
    elif "def ref(" in cli_content:
        # Old group-based version present — remove it first
        print("Found old group-based ref — removing before re-patching.")
        import re
        # Remove the old REFERENCE COMMANDS block
        pattern = (
            r'\n*# =+\n# REFERENCE COMMANDS\n# =+\n'
            r'.*?'
            r'(?=\n# =+\n# (?:COMMUNITY|DYNAMICS) COMMANDS)'
        )
        cli_content = re.sub(pattern, '\n', cli_content, flags=re.DOTALL)
        CLI_PATH.write_text(cli_content)
        print("Removed old ref block.")

    if needs_patch:
        cli_commands_path = REF_SRC / "cli_commands.py"
        if not cli_commands_path.exists():
            print(f"ERROR: {cli_commands_path} not found.")
            sys.exit(1)

        with open(cli_commands_path) as f:
            lines = f.readlines()

        # Extract the actual Click commands (skip comment header)
        start = None
        for i, line in enumerate(lines):
            if line.startswith("# ======"):
                start = i
                break

        if start is None:
            print("ERROR: Could not find section header in cli_commands.py")
            sys.exit(1)

        commands_block = "".join(lines[start:])

        # Insert before COMMUNITY COMMANDS
        marker = "# =============================================================================\n# COMMUNITY COMMANDS"

        if marker not in cli_content:
            # Try finding just the comment
            for possible_marker in [
                "# COMMUNITY COMMANDS",
                "# =============================================================================\n# DYNAMICS COMMANDS",
                "# DYNAMICS COMMANDS",
            ]:
                if possible_marker in cli_content:
                    marker = possible_marker
                    break
            else:
                print("ERROR: Could not find insertion marker in cli.py")
                print("  Looked for: COMMUNITY COMMANDS, DYNAMICS COMMANDS")
                sys.exit(1)

        replacement = commands_block + "\n" + marker
        cli_content = cli_content.replace(marker, replacement, 1)

        CLI_PATH.write_text(cli_content)
        print("Patched nomad/cli.py with ref commands")

    # ── Step 4: Copy test file ───────────────────────────────────────
    test_src = REPO_ROOT / "tests" / "test_reference.py"
    if test_src.exists():
        print(f"Test file already at {test_src}")
    else:
        # Check if it's in a different location
        alt_src = REF_SRC / "tests" / "test_reference.py"
        if alt_src.exists():
            shutil.copy2(alt_src, TEST_DIR / "test_reference.py")
            print(f"Copied test file to {TEST_DIR / 'test_reference.py'}")

    # ── Step 5: Verify PyYAML ────────────────────────────────────────
    try:
        import yaml
        print(f"PyYAML available: {yaml.__version__}")
    except ImportError:
        print("WARNING: PyYAML not installed.")
        print("  Install with: pip install pyyaml")
        print("  The reference system will use built-in entries only.")

    # ── Done ─────────────────────────────────────────────────────────
    print()
    print("Done. To verify:")
    print("  pip install -e .")
    print("  nomad ref")
    print("  nomad ref dyn diversity")
    print("  nomad ref search 'regime divergence'")
    print("  python -m pytest tests/test_reference.py -v")


if __name__ == "__main__":
    main()
