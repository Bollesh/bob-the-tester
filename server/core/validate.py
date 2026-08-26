"""
validate_and_keep(test_file, candidate) — P1 core tool.

The Assured-LLMSE filter (Meta TestGen-LLM, FSE 2024).

A candidate test is KEPT only when BOTH conditions hold:
    1. The full test suite passes (zero failures, zero errors)
    2. Coverage of the target source file strictly increases

On ANY failure the test file is rolled back to its pre-insertion state.
Roll-back is textual (read-before → write-back); no git is required.

Candidate schema:
    test_code          str  — complete def test_…(): block
    new_imports        str  — import lines to prepend (blank if not needed)
    insert_after_line  int  — 1-indexed line after which to insert test_code
                              (defaults to end-of-file)

Details payload:
    kept               bool
    reason             str   — human + LLM-readable explanation
    coverage_before    float — percent before insertion
    coverage_after     float — percent after insertion (None if discarded at step 1)
    rollback_performed bool
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from server.schema import ToolResult
from server.core.run_tests import run_tests
from server.core.coverage import get_coverage


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write_file(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def _insert_candidate(test_file: str, candidate: dict[str, Any]) -> str:
    """
    Textually insert the candidate into test_file.

    Insertion strategy:
    - new_imports (if any) are prepended at the very top of the file,
      skipping duplicates.
    - test_code is appended after insert_after_line (1-indexed).
      Defaults to end-of-file.

    Returns the ORIGINAL file content so the caller can roll back.
    """
    original = _read_file(test_file)
    lines = original.splitlines(keepends=True)

    new_imports: str = candidate.get("new_imports", "").strip()
    test_code: str = candidate.get("test_code", "").strip()
    insert_after_line: int = int(
        candidate.get("insert_after_line", len(lines)) or len(lines)
    )

    # ── 1. Prepare test code block ───────────────────────────────────────
    code_block = "\n\n" + test_code + "\n"

    # ── 2. Insert test code after insert_after_line ──────────────────────
    insert_pos = min(max(0, insert_after_line), len(lines))
    new_lines = lines[:insert_pos] + [code_block] + lines[insert_pos:]
    new_content = "".join(new_lines)

    # ── 3. Prepend missing imports at the top ────────────────────────────
    if new_imports:
        import_block = new_imports + "\n\n"
        # Only prepend if the imports aren't already present anywhere
        if new_imports not in original:
            new_content = import_block + new_content

    _write_file(test_file, new_content)
    return original


def _measure_coverage_percent(
    test_file: str,
    cwd: str,
    source_module: str,
    run_id: str,
) -> float:
    """
    Run pytest-cov scoped to test_file and return coverage percent
    for source_module.  Returns 0.0 on any failure.

    Implementation notes:
    - Uses a directory-level --cov target for cross-platform reliability.
      pytest-cov handles ``--cov=src`` far more reliably than
      ``--cov=src/calculator.py`` on Windows (path-separator quirks).
    - Does NOT pass run_start_ms to get_coverage (stale-report guard is
      skipped for internal measurements — we generated this XML ourselves
      and millisecond-resolution ties can cause spurious failures).
    """
    with tempfile.NamedTemporaryFile(
        suffix=".xml", delete=False, dir=cwd, prefix="cov_vk_"
    ) as fh:
        cov_xml = fh.name

    rel_test = os.path.relpath(test_file, cwd)

    # Use the parent directory for --cov when given a .py file path.
    # e.g. "src/calculator.py" → "src"  so pytest-cov can locate the module.
    if source_module and source_module.endswith(".py"):
        cov_target = str(Path(source_module).parent).replace("\\", "/") or "."
    else:
        cov_target = source_module if source_module else "."

    try:
        cmd = [
            "python", "-m", "pytest",
            rel_test,
            f"--cov={cov_target}",
            f"--cov-report=xml:{cov_xml}",
            "--tb=no",
            "-q",
        ]
        subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=120
        )
        # run_start_ms=None — skips the stale-report guard for internal calls
        result = get_coverage(
            cov_xml,
            source_file=source_module,
            run_start_ms=None,
            run_id=run_id,
        )
        return result.details.get("percent", 0.0) if result.ok else 0.0
    except Exception:  # noqa: BLE001
        return 0.0
    finally:
        try:
            os.unlink(cov_xml)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_keep(
    test_file: str,
    candidate: dict[str, Any],
    cwd: str = "",
    source_file: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Validate a candidate test against the Assured-LLMSE keep criteria.

    Args:
        test_file:   Absolute path to the test file to insert into.
        candidate:   {test_code, new_imports, insert_after_line}
        cwd:         Project root for subprocess calls.  Defaults to the
                     parent directory of test_file's parent (i.e. repo root
                     when tests live in tests/).
        source_file: Relative path (from cwd) of the source module whose
                     coverage to measure, e.g. "src/pricing.py".
                     Empty → measure all files.
        run_id:      Shared pipeline UUID.

    Returns:
        ToolResult with:
            proceed=True  → test KEPT  (suite passed + coverage increased)
            proceed=False → test DISCARDED (rollback already performed)
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    # Infer cwd from test file location if not provided
    if not cwd:
        # tests/ sits one level below project root
        cwd = str(Path(test_file).resolve().parent.parent)

    t0 = time.monotonic_ns()
    original_content: str | None = None
    rollback_performed = False

    def _rollback(reason_prefix: str) -> None:
        nonlocal rollback_performed
        if original_content is not None and not rollback_performed:
            _write_file(test_file, original_content)
            rollback_performed = True

    def _discard(reason: str, cov_after: float | None = None) -> ToolResult:
        _rollback(reason)
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="validate_and_keep",
            ok=True,
            score=0.0,
            proceed=False,
            verdict=f"DISCARDED — {reason}",
            details={
                "kept": False,
                "reason": reason,
                "coverage_before": cov_before if "cov_before" in dir() else None,
                "coverage_after": cov_after,
                "rollback_performed": True,
            },
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    try:
        # ── Step 1: baseline coverage BEFORE insertion ────────────────────
        cov_before = _measure_coverage_percent(test_file, cwd, source_file, run_id)

        # ── Step 2: insert candidate into test file ───────────────────────
        original_content = _insert_candidate(test_file, candidate)

        # ── Step 3: run the FULL test suite ──────────────────────────────
        tests_dir = os.path.relpath(str(Path(test_file).parent), cwd)
        run_result = run_tests(tests_dir, cwd, run_id=run_id)

        if not run_result.proceed:
            failed = run_result.details.get("failed", 0)
            errors = run_result.details.get("errors", 0)
            reason = (
                f"Suite failed after insertion: "
                f"{failed} failed, {errors} errors. "
                f"Verdict: {run_result.verdict}"
            )
            return _discard(reason)

        # ── Step 4: measure coverage AFTER insertion ──────────────────────
        cov_after = _measure_coverage_percent(test_file, cwd, source_file, run_id)

        # ── Step 5: strict coverage increase check ────────────────────────
        if cov_after <= cov_before:
            reason = (
                f"Coverage did not strictly increase: "
                f"{cov_before:.2f}% → {cov_after:.2f}%.  "
                "Candidate adds no new lines.  "
                "Discarded per Assured-LLMSE filter."
            )
            return _discard(reason, cov_after=cov_after)

        # ── Step 6: KEEP ─────────────────────────────────────────────────
        gain = round(cov_after - cov_before, 2)
        reason = (
            f"Passed and strictly increased coverage: "
            f"{cov_before:.2f}% → {cov_after:.2f}% (+{gain}%)"
        )
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="validate_and_keep",
            ok=True,
            # Score scales linearly: 10% gain → 1.0, capped at 1.0
            score=min(1.0, round(gain / 10.0, 4)),
            proceed=True,
            verdict=f"KEPT — {reason}",
            details={
                "kept": True,
                "reason": reason,
                "coverage_before": cov_before,
                "coverage_after": cov_after,
                "rollback_performed": False,
            },
            artifacts=[test_file],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        # Safety net: ALWAYS roll back on unexpected errors
        _rollback(str(exc))
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="validate_and_keep",
            ok=False,
            score=0.0,
            proceed=False,
            verdict=f"Tool internal error (rolled back): {exc}",
            details={
                "kept": False,
                "reason": str(exc),
                "coverage_before": None,
                "coverage_after": None,
                "rollback_performed": rollback_performed,
                "error": str(exc),
            },
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )
