"""
scripts/reset_demo.py — P1 owns.

Wipes transient state and restores the sample_repo to a pristine state
so the demo can be re-run cleanly.

What it resets:
  - sample_repo/tests/ → restores test_placeholder.py to the committed
    pristine baseline and deletes any Bob-generated tests
  - testforge.db → deleted (P4 re-creates it on next run)
  - Any stale coverage.xml / .mutmut-cache / __pycache__ artifacts

Usage:
    python scripts/reset_demo.py
    python scripts/reset_demo.py --dry-run    # preview without deleting
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_TESTS_DIR = REPO_ROOT / "sample_repo" / "tests"

PRISTINE_TEST_CONTENT = '''\
"""
test_placeholder.py — minimal baseline tests for smoke.py.

These tests give calculator.py ~40% coverage deliberately, leaving
large uncovered gaps for validate_and_keep to exploit.

DO NOT hand-edit this file.  Bob writes new tests here during runs.
Use scripts/reset_demo.py to restore this pristine state.
"""
import sys
import os

# Allow running from the repo root or tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from calculator import add, subtract


def test_add_positive():
    """Basic addition works."""
    assert add(2, 3) == 5


def test_add_zero():
    """Adding zero is a no-op."""
    assert add(10, 0) == 10


def test_subtract_positive():
    """Basic subtraction works."""
    assert subtract(10, 3) == 7
'''

PATHS_TO_DELETE = [
    REPO_ROOT / "testforge.db",
    REPO_ROOT / "coverage.xml",
    REPO_ROOT / ".coverage",
    REPO_ROOT / ".mutmut-cache",
]

GLOB_PATTERNS_TO_DELETE = [
    str(REPO_ROOT / "**" / "__pycache__"),
    str(REPO_ROOT / "**" / "*.pyc"),
    str(REPO_ROOT / "**" / "junit_*.xml"),
    str(REPO_ROOT / "**" / "cov_*.xml"),
    str(REPO_ROOT / "**" / "smoke_cov_*.xml"),
]


def reset(dry_run: bool = False) -> None:
    tag = "[DRY RUN] " if dry_run else ""

    print(f"\n{tag}TestForge demo reset\n{'─' * 50}")

    # ── 1. Restore pristine test file ─────────────────────────────────────
    placeholder = SAMPLE_TESTS_DIR / "test_placeholder.py"
    print(f"\n{tag}Restoring {placeholder.relative_to(REPO_ROOT)}")
    if not dry_run:
        placeholder.write_text(PRISTINE_TEST_CONTENT, encoding="utf-8")
    print("  ✓ test_placeholder.py restored")

    # ── 2. Remove Bob-generated test files ────────────────────────────────
    print(f"\n{tag}Removing Bob-generated test files from sample_repo/tests/")
    for f in SAMPLE_TESTS_DIR.iterdir():
        if f.name != "test_placeholder.py" and f.suffix == ".py":
            print(f"  rm {f.relative_to(REPO_ROOT)}")
            if not dry_run:
                f.unlink()

    # ── 3. Delete transient DB / coverage / cache files ───────────────────
    print(f"\n{tag}Removing transient artifacts")
    for path in PATHS_TO_DELETE:
        if path.exists():
            print(f"  rm {path.relative_to(REPO_ROOT)}")
            if not dry_run:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    for pattern in GLOB_PATTERNS_TO_DELETE:
        for match in glob.glob(pattern, recursive=True):
            mp = Path(match)
            # Don't touch demo_replay/ (the curated snapshot)
            if "demo_replay" in mp.parts:
                continue
            print(f"  rm {mp.relative_to(REPO_ROOT)}")
            if not dry_run:
                if mp.is_dir():
                    shutil.rmtree(mp)
                else:
                    mp.unlink(missing_ok=True)

    print(f"\n{'─' * 50}")
    print(f"{tag}✓ Reset complete.  Run `python scripts/smoke.py` to verify.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset TestForge demo state")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without actually deleting",
    )
    args = parser.parse_args()
    reset(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
