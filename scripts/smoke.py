#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/smoke.py — P1's integration safety net.

Exercises every P1 tool WITHOUT Bob.  Must pass green on main before
anyone leaves for the day (AGENTS.md §6 merge rule 5).

Usage:
    cd testforge
    pip install -e .
    python scripts/smoke.py

Exit codes:
    0 — all checks passed
    1 — one or more checks failed (details printed to stderr)

What it tests:
    1. run_tests      — baseline suite passes
    2. get_coverage   — report parses, returns a percent
    3. list_uncovered — gap map has expected functions
    4. validate_and_keep (VALID candidate)
                      → kept=True, rollback_performed=False
    5. validate_and_keep (FAILING candidate)
                      → kept=False, rollback_performed=True, test file restored
    6. validate_and_keep (NO COVERAGE GAIN candidate)
                      → kept=False, rollback_performed=True, test file restored
    7. Idempotency    — test file content identical to original after discards
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# ── Force UTF-8 stdout on Windows (avoids cp1252 UnicodeEncodeError) ─────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent          # testforge/
SAMPLE_ROOT = REPO_ROOT / "sample_repo"
SAMPLE_SRC = SAMPLE_ROOT / "src" / "calculator.py"
SAMPLE_TESTS_DIR = SAMPLE_ROOT / "tests"
SAMPLE_TEST_FILE = SAMPLE_TESTS_DIR / "test_placeholder.py"

# Add testforge/ to sys.path so server imports work without installing
sys.path.insert(0, str(REPO_ROOT))

from server.core.coverage import get_coverage, list_uncovered
from server.core.run_tests import run_tests
from server.core.validate import validate_and_keep
from server.schema import ToolResult

# ─────────────────────────────────────────────────────────────────────────────

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
BOLD = ""
RESET = ""

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f" - {detail}" if detail else ""))
        failures.append(label)


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _run_coverage_for_smoke(cwd: Path) -> tuple[str, int]:
    """
    Run pytest-cov on the sample tests and return (coverage.xml path, run_start_ms).
    Generates coverage.xml into a temp file inside cwd.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".xml", delete=False, dir=str(cwd), prefix="smoke_cov_"
    ) as fh:
        cov_xml = fh.name

    run_start_ms = int(time.time() * 1000)
    cmd = [
        "python", "-m", "pytest",
        str(SAMPLE_TESTS_DIR),
        "--cov=src",
        f"--cov-report=xml:{cov_xml}",
        "--tb=no", "-q",
    ]
    subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    return cov_xml, run_start_ms


def _snapshot(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Check 1: run_tests
# ─────────────────────────────────────────────────────────────────────────────

section("1 · run_tests — baseline suite")
result: ToolResult = run_tests(
    test_command=f"{SAMPLE_TESTS_DIR}",
    cwd=str(SAMPLE_ROOT),
)
check("ok=True", result.ok, result.verdict)
check("proceed=True (suite passes)", result.proceed, result.verdict)
check("passed >= 3", result.details.get("passed", 0) >= 3,
      f"got {result.details.get('passed')}")
check("failed == 0", result.details.get("failed", 0) == 0,
      f"got {result.details.get('failed')}")
check("details has junit_summary", "junit_summary" in result.details)
check("tool name correct", result.tool == "run_tests")
check("run_id is set", bool(result.run_id))
print(f"     verdict: {result.verdict}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 2: get_coverage
# ─────────────────────────────────────────────────────────────────────────────

section("2 · get_coverage — parse Cobertura XML")
cov_xml, run_start_ms = _run_coverage_for_smoke(SAMPLE_ROOT)

try:
    result = get_coverage(
        report_path=cov_xml,
        source_file="src/calculator.py",
        run_start_ms=run_start_ms,
    )
    check("ok=True", result.ok, result.verdict)
    check("proceed=True (always informational)", result.proceed)
    pct = result.details.get("percent", -1)
    check("coverage percent in [0, 100]", 0 <= pct <= 100, f"got {pct}")
    check("coverage < 100% (gaps exist for smoke)", pct < 100.0,
          f"got {pct}% — test file covers everything, no gaps to exploit")
    check("uncovered_ranges list present", isinstance(result.details.get("uncovered_ranges"), list))
    check("covered_lines list present", isinstance(result.details.get("covered_lines"), list))
    check("stale-report guard passed", result.ok,
          "report should be fresh — mtime > run_start_ms")
    print(f"     coverage: {pct}%")
finally:
    try:
        os.unlink(cov_xml)
    except OSError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Check 3: list_uncovered
# ─────────────────────────────────────────────────────────────────────────────

section("3 · list_uncovered — prompt-ready gap map")
cov_xml2, run_start_ms2 = _run_coverage_for_smoke(SAMPLE_ROOT)

try:
    # Prime the cache
    get_coverage(cov_xml2, source_file="src/calculator.py", run_start_ms=run_start_ms2)

    result = list_uncovered(
        source_file=str(SAMPLE_SRC),
        report_path=cov_xml2,
    )
    check("ok=True", result.ok, result.verdict)
    check("proceed=True", result.proceed)
    gaps = result.details.get("gaps", [])
    check("gaps list is non-empty", len(gaps) > 0, f"got {len(gaps)} gaps")
    if gaps:
        g = gaps[0]
        check("gap has 'start' int", isinstance(g.get("start"), int))
        check("gap has 'end' int", isinstance(g.get("end"), int))
        check("gap has 'function' str", isinstance(g.get("function"), str))
        check("gap has 'snippet' str", isinstance(g.get("snippet"), str))
        # We expect at least divide/factorial/is_prime to be uncovered
        fn_names = {g["function"] for g in gaps}
        check(
            "at least one known uncovered function present",
            bool(fn_names & {"divide", "factorial", "is_prime", "power", "gcd"}),
            f"found functions: {fn_names}",
        )
    print(f"     {len(gaps)} uncovered range(s) found")
finally:
    try:
        os.unlink(cov_xml2)
    except OSError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Check 4: validate_and_keep — VALID candidate (should be KEPT)
# ─────────────────────────────────────────────────────────────────────────────

section("4 · validate_and_keep — VALID candidate (expect KEPT)")

original_content = _snapshot(SAMPLE_TEST_FILE)
run_id_4 = "smoke-check-4"

valid_candidate = {
    "test_code": textwrap.dedent("""\
        def test_divide_basic():
            \"\"\"Smoke: basic division works.\"\"\"
            from calculator import divide
            assert divide(10, 2) == 5.0

        def test_divide_zero():
            \"\"\"Smoke: divide by zero raises ZeroDivisionError.\"\"\"
            import pytest
            from calculator import divide
            with pytest.raises(ZeroDivisionError):
                divide(5, 0)
    """),
    "new_imports": "import pytest",
    "insert_after_line": len(original_content.splitlines()),
}

result = validate_and_keep(
    test_file=str(SAMPLE_TEST_FILE),
    candidate=valid_candidate,
    cwd=str(SAMPLE_ROOT),
    source_file="src/calculator.py",
    run_id=run_id_4,
)
check("ok=True", result.ok, result.verdict)
check("kept=True", result.details.get("kept") is True, result.details.get("reason", ""))
check("proceed=True", result.proceed, result.verdict)
check("rollback_performed=False", result.details.get("rollback_performed") is False)
cov_b4 = result.details.get("coverage_before", -1)
cov_a4 = result.details.get("coverage_after", -1)
check("coverage_after > coverage_before",
      cov_a4 > cov_b4,
      f"{cov_b4}% → {cov_a4}%")
print(f"     {cov_b4:.1f}% → {cov_a4:.1f}%  verdict: {result.verdict}")

# Restore for next checks
_snapshot_after_keep = _snapshot(SAMPLE_TEST_FILE)
SAMPLE_TEST_FILE.write_text(original_content, encoding="utf-8")

# ─────────────────────────────────────────────────────────────────────────────
# Check 5: validate_and_keep — FAILING candidate (should be DISCARDED + rolled back)
# ─────────────────────────────────────────────────────────────────────────────

section("5 · validate_and_keep — FAILING candidate (expect DISCARDED + rollback)")

snapshot_before_5 = _snapshot(SAMPLE_TEST_FILE)

failing_candidate = {
    "test_code": textwrap.dedent("""\
        def test_intentional_failure():
            \"\"\"Smoke: this test always fails — validates the discard path.\"\"\"
            from calculator import add
            assert add(1, 1) == 999, "intentional failure"
    """),
    "new_imports": "",
    "insert_after_line": len(snapshot_before_5.splitlines()),
}

result = validate_and_keep(
    test_file=str(SAMPLE_TEST_FILE),
    candidate=failing_candidate,
    cwd=str(SAMPLE_ROOT),
    source_file="src/calculator.py",
    run_id="smoke-check-5",
)
check("ok=True", result.ok, result.verdict)
check("kept=False", result.details.get("kept") is False, result.details.get("reason", ""))
check("proceed=False", not result.proceed)
check("rollback_performed=True", result.details.get("rollback_performed") is True)
check("test file restored after rollback",
      _snapshot(SAMPLE_TEST_FILE) == snapshot_before_5,
      "file content mismatch after rollback")
print(f"     discard reason: {result.details.get('reason', '')[:120]}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 6: validate_and_keep — NO COVERAGE GAIN (should be DISCARDED + rolled back)
# ─────────────────────────────────────────────────────────────────────────────

section("6 · validate_and_keep — NO COVERAGE GAIN (expect DISCARDED + rollback)")

snapshot_before_6 = _snapshot(SAMPLE_TEST_FILE)

# This test only re-exercises add() which is already covered
no_gain_candidate = {
    "test_code": textwrap.dedent("""\
        def test_add_negative_smoke():
            \"\"\"Smoke: add negatives — already-covered code path, no new coverage.\"\"\"
            from calculator import add
            assert add(-1, -2) == -3
    """),
    "new_imports": "",
    "insert_after_line": len(snapshot_before_6.splitlines()),
}

result = validate_and_keep(
    test_file=str(SAMPLE_TEST_FILE),
    candidate=no_gain_candidate,
    cwd=str(SAMPLE_ROOT),
    source_file="src/calculator.py",
    run_id="smoke-check-6",
)
check("ok=True", result.ok, result.verdict)
check("kept=False", result.details.get("kept") is False, result.details.get("reason", ""))
check("proceed=False", not result.proceed)
check("rollback_performed=True", result.details.get("rollback_performed") is True)
check("test file restored after rollback",
      _snapshot(SAMPLE_TEST_FILE) == snapshot_before_6,
      "file content mismatch after rollback")
print(f"     discard reason: {result.details.get('reason', '')[:120]}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 7: Idempotency — file identical to original after all discards
# ─────────────────────────────────────────────────────────────────────────────

section("7 · Idempotency — test file content after all discard checks")

current = _snapshot(SAMPLE_TEST_FILE)
check("test file identical to original after all discard operations",
      current == original_content,
      f"file length: original={len(original_content)} current={len(current)}")

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

section("SUMMARY")
if not failures:
    print(f"\n  {PASS} All P1 smoke checks passed.")
    print(f"  Server is ready. Register in .bob/mcp.json and run Bob's skill.\n")
    sys.exit(0)
else:
    print(f"\n  {FAIL} {len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"    * {f}")
    print()
    sys.exit(1)
