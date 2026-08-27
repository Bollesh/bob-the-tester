#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/smoke.py — P1's integration safety net.

Exercises every P1 tool WITHOUT Bob.  Must pass green on main before
anyone leaves for the day (AGENTS.md §6 merge rule 5).

Usage:
    cd bob-the-tester
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

    P2 pipeline stages:
    8.  detect_smells      — clean file is clean; smelly fixture trips every
                             detector; a good test is not falsely flagged
    9.  run_flaky_check    — stable suite passes; an alternating test is caught
                             and gates (proceed=False); a deterministic failure
                             is reported separately, NOT as flakiness
    10. run_property_tests — true property holds, false one is falsified and
                             shrunk to the minimal counterexample
    11. run_fuzz           — seeded crash is found by the harness
    12. mutation_test      — mutants generated, survivors carry resolved file
                             lines and original→mutated source

Stages 10–12 depend on optional engines (Hypothesis, Atheris, mutmut).  When
an engine is absent the check asserts CLEAN DEGRADATION instead — proceed
stays true and engine_available reports false — so a machine without the
[pipeline] extra still gets a green run.
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
REPO_ROOT = Path(__file__).resolve().parent.parent          # bob-the-tester/
SAMPLE_ROOT = REPO_ROOT / "sample_repo"
SAMPLE_SRC = SAMPLE_ROOT / "src" / "calculator.py"
SAMPLE_TESTS_DIR = SAMPLE_ROOT / "tests"
SAMPLE_TEST_FILE = SAMPLE_TESTS_DIR / "test_placeholder.py"

# Add bob-the-tester/ to sys.path so server imports work without installing
sys.path.insert(0, str(REPO_ROOT))

from server.core.coverage import get_coverage, list_uncovered
from server.core.run_tests import run_tests
from server.core.validate import validate_and_keep
from server.pipeline.flaky import run_flaky_check
from server.pipeline.fuzz import run_fuzz
from server.pipeline.mutation import mutation_test
from server.pipeline.properties import run_property_tests
from server.pipeline.smells import detect_smells
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


def warn(label: str, detail: str = "") -> None:
    """
    Report a check that could not run rather than one that failed.

    Used for optional pipeline engines (Hypothesis, Atheris, mutmut): a
    machine without the [pipeline] extra installed must still get a green
    smoke run, because those stages degrade by design rather than break.
    """
    print(f"  {WARN} {label}" + (f" - {detail}" if detail else ""))


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

# ═════════════════════════════════════════════════════════════════════════════
# P2 — QUALITY PIPELINE TOOLS
#
# Fixtures for these checks are written to a temp directory, never into
# sample_repo/tests/ — that directory is Bob-only (AGENTS.md invariant 5).
# ═════════════════════════════════════════════════════════════════════════════

P2_TMP = Path(tempfile.mkdtemp(prefix="smoke_p2_"))

# ─────────────────────────────────────────────────────────────────────────────
# Check 8: detect_smells
# ─────────────────────────────────────────────────────────────────────────────

section("8 · detect_smells — static smell checks")

# 8a — the pristine placeholder suite should come back clean
result = detect_smells(test_file=str(SAMPLE_TEST_FILE))
check("ok=True", result.ok, result.verdict)
check("proceed=True (smells never gate)", result.proceed)
check("clean file reports no findings",
      result.details.get("findings") == [],
      f"got {result.details.get('findings')}")
check("tests_scanned == 3", result.details.get("tests_scanned") == 3,
      f"got {result.details.get('tests_scanned')}")
check("clean file scores 1.0", result.details.get("smell_score") == 1.0,
      f"got {result.details.get('smell_score')}")

# 8b — a deliberately smelly fixture must trip every detector
smelly = P2_TMP / "test_smelly_fixture.py"
smelly.write_text(textwrap.dedent('''\
    import time


    def test_no_assertion():
        value = 1 + 1
        print(value)


    def test_empty():
        pass


    def test_trivial_not_none():
        value = 1 + 1
        assert value is not None


    def test_sleepy():
        time.sleep(0.01)
        assert 1 + 1 == 2


    def test_original():
        assert 2 + 2 == 4


    def test_duplicate():
        assert 2 + 2 == 4


    def test_good():
        """A genuinely fine test — must NOT be flagged."""
        assert 2 + 2 == 4, "explicit message"
'''), encoding="utf-8")

result = detect_smells(test_file=str(smelly))
found_types = set(result.details.get("by_type", {}))
check("ok=True on smelly fixture", result.ok, result.verdict)
check("proceed=True even with smells", result.proceed)
for expected in ("no_assertion", "empty_test", "trivial_assertion",
                 "sleep_call", "duplicate_body"):
    check(f"detects {expected}", expected in found_types,
          f"by_type={sorted(found_types)}")
check("smell_score < 1.0 when smells exist",
      result.details.get("smell_score", 1.0) < 1.0,
      f"got {result.details.get('smell_score')}")
flagged = {f["test_name"] for f in result.details.get("findings", [])}
check("clean test NOT flagged (no false positive)",
      "test_good" not in flagged,
      f"flagged: {sorted(flagged)}")
print(f"     {result.verdict}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 9: run_flaky_check
# ─────────────────────────────────────────────────────────────────────────────

section("9 · run_flaky_check — repeat-run stability")

# 9a — stable suite
result = run_flaky_check(test_file=str(SAMPLE_TEST_FILE), n=2, cwd=str(SAMPLE_ROOT))
check("ok=True", result.ok, result.verdict)
check("proceed=True (nothing flaky)", result.proceed, result.verdict)
check("flaky list empty", result.details.get("flaky") == [],
      f"got {result.details.get('flaky')}")
check("outcome vector per test present",
      len(result.details.get("outcomes", {})) == 3,
      f"got {len(result.details.get('outcomes', {}))} tests")
check("runs_completed == 2", result.details.get("runs_completed") == 2,
      f"got {result.details.get('runs_completed')}")

# 9b — a genuinely flaky test must be caught AND gate the pipeline
flaky_proj = P2_TMP / "flakyproj"
(flaky_proj / "tests").mkdir(parents=True, exist_ok=True)
flaky_file = flaky_proj / "tests" / "test_flaky_fixture.py"
flaky_file.write_text(textwrap.dedent('''\
    import itertools
    import os

    STATE = os.path.join(os.path.dirname(__file__), "_counter.txt")


    def _next_run() -> int:
        n = 0
        if os.path.exists(STATE):
            n = int(open(STATE).read() or "0")
        with open(STATE, "w") as fh:
            fh.write(str(n + 1))
        return n


    def test_stable_passes():
        assert 1 + 1 == 2


    def test_alternates():
        """Passes on even runs, fails on odd ones — deterministic flakiness."""
        assert _next_run() % 2 == 0


    def test_always_fails():
        assert 1 == 2, "deterministic failure, must NOT be called flaky"
'''), encoding="utf-8")

result = run_flaky_check(test_file=str(flaky_file), n=4, cwd=str(flaky_proj))
flaky_names = {f["test_id"].rpartition("::")[2] for f in result.details.get("flaky", [])}
failing_names = {t.rpartition("::")[2]
                 for t in result.details.get("consistently_failing", [])}
check("ok=True on flaky fixture", result.ok, result.verdict)
check("proceed=False (flakiness IS a hard gate)", not result.proceed, result.verdict)
check("alternating test reported flaky",
      "test_alternates" in flaky_names, f"flaky={sorted(flaky_names)}")
check("stable test NOT reported flaky",
      "test_stable_passes" not in flaky_names, f"flaky={sorted(flaky_names)}")
check("deterministic failure NOT reported flaky",
      "test_always_fails" not in flaky_names, f"flaky={sorted(flaky_names)}")
check("deterministic failure listed as consistently_failing",
      "test_always_fails" in failing_names, f"got {sorted(failing_names)}")
print(f"     {result.verdict}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 10: run_property_tests
# ─────────────────────────────────────────────────────────────────────────────

section("10 · run_property_tests — Hypothesis counterexamples")

prop_proj = P2_TMP / "propproj"
(prop_proj / "tests").mkdir(parents=True, exist_ok=True)
prop_file = prop_proj / "tests" / "test_props_fixture.py"
prop_file.write_text(textwrap.dedent('''\
    from hypothesis import given, strategies as st


    @given(st.integers())
    def test_identity_holds(x):
        """True property — must pass."""
        assert x + 0 == x


    @given(st.integers())
    def test_bounded_is_false(x):
        """False property — shrinks to exactly x=100."""
        assert x < 100
'''), encoding="utf-8")

result = run_property_tests(target=str(prop_file), max_examples=50, cwd=str(prop_proj))

if not result.details.get("engine_available", False):
    warn("Hypothesis not installed — stage skipped",
         'install with: pip install -e ".[pipeline]"')
    check("degrades cleanly: proceed=True despite missing engine", result.proceed,
          result.verdict)
    check("degrades cleanly: engine_available=False reported",
          result.details.get("engine_available") is False)
else:
    check("ok=True", result.ok, result.verdict)
    check("proceed=True (counterexamples are findings, not failures)",
          result.proceed, result.verdict)
    check("true property passed", result.details.get("passed") == 1,
          f"got {result.details.get('passed')}")
    check("false property falsified", result.details.get("failed") == 1,
          f"got {result.details.get('failed')}")
    failures_found = result.details.get("failures", [])
    check("failure carries a shrunk_input",
          bool(failures_found) and bool(failures_found[0].get("shrunk_input")),
          f"got {failures_found[:1]}")
    check("counterexample shrank to the minimal value (x=100)",
          bool(failures_found) and "100" in (failures_found[0].get("shrunk_input") or ""),
          f"got {failures_found[0].get('shrunk_input') if failures_found else None!r}")
    check("max_examples echoed back", result.details.get("max_examples") == 50,
          f"got {result.details.get('max_examples')}")
    print(f"     {result.verdict}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 11: run_fuzz
# ─────────────────────────────────────────────────────────────────────────────

section("11 · run_fuzz — Atheris harness execution")

harness = P2_TMP / "fuzz_harness.py"
harness.write_text(textwrap.dedent('''\
    import sys
    import atheris

    with atheris.instrument_imports():
        pass


    def TestOneInput(data):
        if len(data) > 3 and data[:4] == b"BOOM":
            raise ValueError("seeded crash for smoke test")


    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
'''), encoding="utf-8")

result = run_fuzz(harness_path=str(harness), seconds=5, cwd=str(P2_TMP))

if not result.details.get("engine_available", False):
    warn("Atheris not installed — stage skipped (pre-agreed first cut, plan §3)",
         "Hypothesis still supplies machine-found counterexamples")
    check("degrades cleanly: proceed=True despite missing engine", result.proceed,
          result.verdict)
    check("degrades cleanly: engine_available=False reported",
          result.details.get("engine_available") is False)
    check("degrades cleanly: crashes list present and empty",
          result.details.get("crashes") == [])
else:
    check("ok=True", result.ok, result.verdict)
    check("proceed=True (crashes are findings)", result.proceed, result.verdict)
    check("crashes list present", isinstance(result.details.get("crashes"), list))
    check("seeded crash was found", len(result.details.get("crashes", [])) > 0,
          "harness raises on b'BOOM' — fuzzer should reach it")
    print(f"     {result.verdict}")

# ─────────────────────────────────────────────────────────────────────────────
# Check 12: mutation_test
# ─────────────────────────────────────────────────────────────────────────────

section("12 · mutation_test — test strength on sample_repo/src/calculator.py")

result = mutation_test(
    target_module="src/calculator.py",
    cwd=str(SAMPLE_ROOT),
    tests_dir="tests",
    timeout_s=600,
)

if not result.details.get("engine_available", False):
    warn("mutmut not installed — stage skipped",
         'install with: pip install -e ".[pipeline]"')
    check("degrades cleanly: proceed=True despite missing engine", result.proceed,
          result.verdict)
    check("degrades cleanly: engine_available=False reported",
          result.details.get("engine_available") is False)
else:
    check("ok=True", result.ok, result.verdict)
    check("proceed=True (survivors are regeneration work)", result.proceed,
          result.verdict)
    check("mutants were generated", result.details.get("total", 0) > 0,
          f"got total={result.details.get('total')}")
    score_val = result.details.get("mutation_score", -1)
    check("mutation_score in [0.0, 1.0]", 0.0 <= score_val <= 1.0,
          f"got {score_val}")
    check("baseline suite leaves survivors (it only covers add/subtract)",
          result.details.get("survived_count", 0) > 0,
          f"got survived_count={result.details.get('survived_count')}")
    survivors = result.details.get("survived", [])
    if survivors:
        first = survivors[0]
        check("survivor has an id", bool(first.get("id")))
        check("survivor has a resolved file line", isinstance(first.get("line"), int),
              f"got {first.get('line')!r}")
        check("survivor has original + mutated source",
              bool(first.get("original")) and bool(first.get("mutated")),
              f"got {first.get('original')!r} -> {first.get('mutated')!r}")
        check("survivor status explains which fix to apply",
              first.get("status") in ("survived", "no tests"),
              f"got {first.get('status')!r}")
        # Line numbers must point into the real source file, not the diff hunk
        calc_lines = SAMPLE_SRC.read_text(encoding="utf-8").splitlines()
        check("resolved line is within the source file",
              isinstance(first.get("line"), int)
              and 1 <= first["line"] <= len(calc_lines),
              f"line={first.get('line')} file has {len(calc_lines)} lines")
    print(f"     {result.verdict}")
    print(f"     took {result.duration_ms / 1000:.1f}s")

# ── Clean up P2 fixtures ─────────────────────────────────────────────────────
shutil.rmtree(P2_TMP, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

section("SUMMARY")
if not failures:
    print(f"\n  {PASS} All P1 + P2 smoke checks passed.")
    print(f"  Server is ready. Register in .bob/mcp.json and run Bob's skill.\n")
    sys.exit(0)
else:
    print(f"\n  {FAIL} {len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"    * {f}")
    print()
    sys.exit(1)
