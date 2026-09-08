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

    P4 data layer:
    13. data layer      — schema, run/tool_call/test/coverage/mutation writers,
                          and the read-only reader API the dashboard uses
    14. logging_mw      — every call lands in tool_calls; structured payloads
                          fan out to the typed tables; the result is returned
                          unchanged even when the tool itself failed
    15. replay cache    — keys ignore run_id and are path-portable across
                          machines; BOB_THE_TESTER_REPLAY=1 serves cached
                          results without executing anything
    16. explain_gaps    — real uncovered regions with function context and
                          objective signals; finds its own coverage report
    17. store_explanation / save_test_record
                        — gap prose persists; a defect-finding test is KEPT
                          and recorded in bugs_found, a wrong test is not

Checks 13–17 use a temporary database and replay directory, so a smoke run
never touches bob-the-tester.db or the blessed demo_replay/ snapshot.

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
        sys.executable, "-m", "pytest",
        str(SAMPLE_TESTS_DIR),
        "--cov=src",
        f"--cov-report=xml:{cov_xml}",
        "--tb=no", "-q",
    ]
    # 300s, not 60s: a cold cache or a loaded CI box makes 60s a flaky ceiling.
    subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=300)
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

# ═════════════════════════════════════════════════════════════════════════════
# P4 — DATA LAYER, REPLAY CACHE, GAP TOOLS
#
# These run against a TEMPORARY database and a TEMPORARY replay directory, so
# a smoke run never pollutes the demo database or the blessed demo_replay/
# snapshot.  Both are selected purely through environment variables, which is
# also the mechanism the CI job and reset_demo.py use.
# ═════════════════════════════════════════════════════════════════════════════

P4_TMP = Path(tempfile.mkdtemp(prefix="bob_p4_"))
os.environ["BOB_THE_TESTER_DB"] = str(P4_TMP / "smoke.db")
os.environ["BOB_THE_TESTER_REPLAY_DIR"] = str(P4_TMP / "replay")
os.environ["BOB_THE_TESTER_REPLAY"] = "0"

from server.data import db as p4_db                      # noqa: E402
from server.data.logging_mw import log_tool_call         # noqa: E402
from server.data.models import SCHEMA_VERSION            # noqa: E402
from server.data import replay as p4_replay              # noqa: E402
from server.data.runs import (                            # noqa: E402
    finish_run as finish_run_tool,
    start_run as start_run_tool,
)
from server.gaps import (                                # noqa: E402
    explain_gaps,
    save_test_record,
    store_explanation,
)

P4_RUN = "smoke-run-0000"

# ─────────────────────────────────────────────────────────────────────────────
# Check 13: database schema + writers + readers
# ─────────────────────────────────────────────────────────────────────────────

section("13 - data layer: schema, writers, readers")

check("temp DB path is honoured", p4_db.db_path() == (P4_TMP / "smoke.db").resolve()
      or str(p4_db.db_path()).endswith("smoke.db"), str(p4_db.db_path()))

p4_db.start_run(P4_RUN, source_file="sample_repo/src/calculator.py",
                coverage_target=85.0, mutation_target=0.8, max_iterations=3)
runs = p4_db.list_runs()
check("run row created", any(r["run_id"] == P4_RUN for r in runs),
      f"got {[r['run_id'] for r in runs]}")

run_row = p4_db.get_run(P4_RUN)
check("run targets persisted",
      run_row is not None and run_row["coverage_target"] == 85.0
      and run_row["max_iterations"] == 3)
check("schema version stamped", SCHEMA_VERSION >= 1)

p4_db.finish_run(P4_RUN, status="complete", iterations_used=2)
run_row = p4_db.get_run(P4_RUN)
check("finish_run closes the run",
      run_row["status"] == "complete" and run_row["iterations_used"] == 2)

# ─────────────────────────────────────────────────────────────────────────────
# Check 14: logging middleware writes tool_calls and fans out
# ─────────────────────────────────────────────────────────────────────────────

section("14 - logging middleware: tool_calls + typed fan-out")

cov_result = ToolResult(
    tool="get_coverage", ok=True, score=0.4, proceed=True,
    verdict="Coverage: 40.0%",
    details={"percent": 40.0, "covered_lines": [1, 2, 3, 4],
             "uncovered_ranges": [{"start": 10, "end": 12}], "uncovered_count": 3},
    artifacts=["coverage.xml"], duration_ms=12, run_id=P4_RUN,
)
log_tool_call(cov_result, {"report_path": "coverage.xml",
                           "source_file": "src/calculator.py"})

logged = p4_db.tool_calls_for_run(P4_RUN)
check("tool_call row written", len(logged) == 1, f"got {len(logged)}")
check("details round-trip as a dict",
      isinstance(logged[0]["details"], dict)
      and logged[0]["details"]["percent"] == 40.0)
check("artifacts round-trip as a list",
      logged[0]["artifacts"] == ["coverage.xml"])

points = p4_db.coverage_for_run(P4_RUN)
check("get_coverage fanned out to coverage_history", len(points) == 1)
check("coverage percent promoted to a real column",
      points and points[0]["percent"] == 40.0)

validate_result = ToolResult(
    tool="validate_and_keep", ok=True, score=1.0, proceed=True,
    verdict="kept", details={"kept": True, "reason": "coverage 40.0 -> 55.0",
                             "coverage_before": 40.0, "coverage_after": 55.0,
                             "rollback_performed": False},
    artifacts=[], duration_ms=900, run_id=P4_RUN,
)
log_tool_call(validate_result, {
    "test_file": "tests/test_calculator.py",
    "candidate": {"test_code": "def test_divide_by_zero():\n    pass"},
})
test_rows = p4_db.tests_for_run(P4_RUN)
check("validate_and_keep fanned out to tests", len(test_rows) == 1)
check("test name recovered from the candidate code",
      test_rows and test_rows[0]["test_name"] == "test_divide_by_zero",
      f"got {test_rows[0]['test_name'] if test_rows else None!r}")
check("keep decision + reason stored",
      test_rows and test_rows[0]["kept"] == 1
      and "coverage" in test_rows[0]["reason"])

flaky_result = ToolResult(
    tool="run_flaky_check", ok=True, score=0.5, proceed=False,
    verdict="1 flaky test",
    details={"runs": 5, "flaky": [{"test_id": "test_wobbly",
                                   "outcomes": ["passed", "failed"]}],
             "consistently_failing": []},
    artifacts=[], duration_ms=2000, run_id=P4_RUN,
)
log_tool_call(flaky_result, {"test_file": "tests/test_calculator.py"})
test_rows = p4_db.tests_for_run(P4_RUN)
flaky_rows = [r for r in test_rows if r["stage"] == "flaky"]
check("flaky test recorded as a discard",
      len(flaky_rows) == 1 and flaky_rows[0]["kept"] == 0)

mutation_result = ToolResult(
    tool="mutation_test", ok=True, score=0.6, proceed=True,
    verdict="mutation score 0.6",
    details={"mutation_score": 0.6, "killed": 6, "survived_count": 4,
             "not_covered": 1, "total": 10, "inconclusive": 0,
             "survived": [{"id": "m1", "line": 12, "original": "a + b",
                           "mutated": "a - b", "status": "survived"}],
             "engine_available": True},
    artifacts=[], duration_ms=30000, run_id=P4_RUN,
)
log_tool_call(mutation_result, {"target_module": "src/calculator.py"})
mut_rows = p4_db.mutation_for_run(P4_RUN)
check("mutation_test fanned out to mutation_results", len(mut_rows) == 1)
check("survivors round-trip with their detail",
      mut_rows and mut_rows[0]["survivors"]
      and mut_rows[0]["survivors"][0]["mutated"] == "a - b")

crashed = ToolResult(
    tool="run_fuzz", ok=False, score=0.0, proceed=True,
    verdict="Atheris not installed", details={"error": "no engine"},
    artifacts=[], duration_ms=5, run_id=P4_RUN,
)
returned = log_tool_call(crashed, {"harness_path": "h.py"})
check("middleware returns the result unchanged",
      returned is crashed and returned.ok is False and returned.proceed is True)

# ─────────────────────────────────────────────────────────────────────────────
# Check 15: replay cache
# ─────────────────────────────────────────────────────────────────────────────

section("15 - replay cache: keys, round-trip, and the REPLAY switch")

args_a = {"report_path": "coverage.xml", "source_file": "src/calculator.py",
          "run_id": "run-aaa"}
args_b = dict(args_a, run_id="run-bbb")
check("cache key ignores run_id",
      p4_replay.cache_key("get_coverage", args_a)
      == p4_replay.cache_key("get_coverage", args_b))

abs_args = {"cwd": str(SAMPLE_ROOT)}
rel_args = {"cwd": "sample_repo"}
check("cache key rewrites repo-absolute paths to relative (portable snapshot)",
      p4_replay.cache_key("run_tests", abs_args)
      == p4_replay.cache_key("run_tests", rel_args),
      f"{p4_replay.normalise_args(abs_args)} vs {p4_replay.normalise_args(rel_args)}")

check("differing args produce differing keys",
      p4_replay.cache_key("get_coverage", args_a)
      != p4_replay.cache_key("get_coverage", {"report_path": "other.xml"}))

live_calls = {"n": 0}


def _fake_tool() -> ToolResult:
    live_calls["n"] += 1
    return ToolResult(
        tool="run_tests", ok=True, score=1.0, proceed=True,
        verdict="12 passed, 0 failed", details={"passed": 12, "failed": 0},
        artifacts=[], duration_ms=4210, run_id=args_a["run_id"],
    )


os.environ["BOB_THE_TESTER_REPLAY"] = "0"
result, replayed = p4_replay.replay_or_run("run_tests", args_a, _fake_tool)
check("live mode executes the tool", live_calls["n"] == 1 and replayed is False)
check("live result recorded to the snapshot",
      p4_replay.load("run_tests", args_a) is not None)

os.environ["BOB_THE_TESTER_REPLAY"] = "1"
check("replay_enabled() reads the env switch", p4_replay.replay_enabled() is True)
result, replayed = p4_replay.replay_or_run("run_tests", args_b, _fake_tool)
check("replay mode does NOT execute the tool", live_calls["n"] == 1,
      f"tool ran {live_calls['n']} times")
check("replay mode reports replayed=True", replayed is True)
check("replayed result is identical", result.verdict == "12 passed, 0 failed"
      and result.details["passed"] == 12)
check("replayed result is re-stamped with the CURRENT run_id",
      result.run_id == "run-bbb", f"got {result.run_id}")

result, replayed = p4_replay.replay_or_run(
    "run_tests", {"report_path": "never-seen.xml"}, _fake_tool)
check("a cache MISS in replay mode falls through to live execution",
      live_calls["n"] == 2 and replayed is False)

os.environ["BOB_THE_TESTER_REPLAY"] = "0"
snap = p4_replay.snapshot_summary()
check("snapshot summary counts entries", snap["entries"] >= 1, str(snap))

# ─────────────────────────────────────────────────────────────────────────────
# Check 16: explain_gaps against a real coverage report
# ─────────────────────────────────────────────────────────────────────────────

section("16 - explain_gaps on sample_repo/src/calculator.py")

cov_xml_p4, _ = _run_coverage_for_smoke(SAMPLE_ROOT)
result = explain_gaps(source_file=str(SAMPLE_SRC), report_path=cov_xml_p4,
                      run_id=P4_RUN)

check("ok=True", result.ok, result.verdict)
check("proceed=True (reporting never gates)", result.proceed)
gaps_found = result.details.get("gaps", [])
check("uncovered regions found", len(gaps_found) > 0,
      "baseline suite only covers add/subtract, so gaps must exist")
if gaps_found:
    first_gap = gaps_found[0]
    check("gap resolves its enclosing function", bool(first_gap.get("function")))
    check("gap carries a source snippet", bool(first_gap.get("snippet")))
    check("gap carries objective signals, not a verdict",
          isinstance(first_gap.get("signals"), dict)
          and "in_main_guard" in first_gap["signals"])
    check("gap line range is inside the file",
          1 <= first_gap["start"] <= len(SAMPLE_SRC.read_text(
              encoding="utf-8").splitlines()))
print(f"     {result.verdict}")

# Report discovery via the tool_calls log: no report_path passed at all.
log_tool_call(
    ToolResult(tool="get_coverage", ok=True, score=0.4, proceed=True,
               verdict="logged for discovery",
               details={"percent": 40.0, "covered_lines": [],
                        "uncovered_count": 0},
               artifacts=[cov_xml_p4], duration_ms=1, run_id=P4_RUN),
    {"report_path": cov_xml_p4},
)
discovered = explain_gaps(source_file=str(SAMPLE_SRC), run_id=P4_RUN)
check("report discovered from the tool_calls log when not passed",
      discovered.ok and discovered.details.get("report_found_via", "").startswith(
          "tool_calls"),
      discovered.details.get("report_found_via", discovered.verdict))

missing = explain_gaps(source_file="does/not/exist.py", run_id=P4_RUN)
check("missing source degrades to ok=False but proceed=True",
      missing.ok is False and missing.proceed is True, missing.verdict)

# ─────────────────────────────────────────────────────────────────────────────
# Check 17: store_explanation + save_test_record (incl. the defect path)
# ─────────────────────────────────────────────────────────────────────────────

section("17 - store_explanation + save_test_record (bug-vs-wrong-test)")

result = store_explanation(
    text="The `__main__` guard is never executed under pytest - intentional skip.",
    source_file=str(SAMPLE_SRC), gaps=gaps_found[:1], run_id=P4_RUN,
)
check("ok=True", result.ok, result.verdict)
stored_gaps = p4_db.gaps_for_run(P4_RUN)
check("explanation persisted", len(stored_gaps) == 1)
check("narrated gaps stored beside the prose",
      stored_gaps and isinstance(stored_gaps[0]["gaps"], list))

empty = store_explanation(text="   ", run_id=P4_RUN)
check("empty explanation is rejected without gating",
      empty.ok is False and empty.proceed is True, empty.verdict)

result = save_test_record(
    test_name="test_regression_rounding_negative_half",
    test_file="tests/test_rounding.py", kept=True,
    reason="Hypothesis found round(-0.5) returns 0, docstring promises -1",
    stage="regression", bug_found=True,
    source_file="sample_repo/src/rounding.py",
    evidence="shrunk_input=-0.5", run_id=P4_RUN,
)
check("ok=True", result.ok, result.verdict)
check("defect reported as bug_recorded", result.details.get("bug_recorded") is True)

bug_rows = p4_db.bugs_for_run(P4_RUN)
check("bugs_found row written", len(bug_rows) == 1)
check("evidence stored for the defect",
      bug_rows and "-0.5" in bug_rows[0]["evidence"])

kept_rows = [r for r in p4_db.tests_for_run(P4_RUN) if r["bug_found"] == 1]
check("a defect-finding test is KEPT, not discarded",
      len(kept_rows) == 1 and kept_rows[0]["kept"] == 1)

wrong = save_test_record(
    test_name="test_asserts_wrong_thing", kept=False,
    reason="asserted 3 == 4; the source docstring specifies 3",
    stage="regression", bug_found=False, run_id=P4_RUN,
)
check("wrong-test path records a discard and no bug",
      wrong.ok and wrong.details.get("bug_recorded") is False
      and len(p4_db.bugs_for_run(P4_RUN)) == 1)

# ─────────────────────────────────────────────────────────────────────────────
# Check 18: start_run / finish_run — the run lifecycle Bob drives
# ─────────────────────────────────────────────────────────────────────────────

section("18 - start_run / finish_run: a run that closes itself")

LIFE_RUN = "smoke-life-0001"

opened = start_run_tool(run_id=LIFE_RUN, source_file="sample_repo/src/calculator.py",
                        coverage_target=0.85, mutation_target=0.80, max_iterations=3)
life_row = p4_db.get_run(LIFE_RUN)
check("start_run opens the run", opened.ok and opened.proceed
      and life_row is not None and life_row["status"] == "running")
check("start_run returns the run_id to thread through the pipeline",
      opened.details.get("run_id") == LIFE_RUN and opened.run_id == LIFE_RUN)

# Fraction in, percent out for coverage; fraction stays a fraction for
# mutation — the units the coverage and mutation tools actually report.
check("targets normalise to the units the dashboard reads",
      life_row["coverage_target"] == 85.0 and life_row["mutation_target"] == 0.8,
      f"got {life_row['coverage_target']} / {life_row['mutation_target']}")

pct_form = start_run_tool(run_id="smoke-life-pct", coverage_target=85, mutation_target=80)
pct_row = p4_db.get_run("smoke-life-pct")
check("percent-form targets normalise identically",
      pct_form.ok and pct_row["coverage_target"] == 85.0
      and pct_row["mutation_target"] == 0.8)

closed = finish_run_tool(LIFE_RUN, status="complete", iterations_used=2,
                         notes="targets met on iteration 2")
life_row = p4_db.get_run(LIFE_RUN)
check("finish_run closes the run",
      closed.ok and life_row["status"] == "complete"
      and life_row["finished_at"] is not None and life_row["iterations_used"] == 2)
check("finish_run reports the run duration", closed.details.get("duration_ms") is not None)
check("finish_run keeps proceed true even so", closed.proceed)

orphan = finish_run_tool("smoke-life-orphan", status="aborted")
check("a run nobody opened is backfilled and closed, not refused",
      orphan.ok and orphan.details.get("run_existed") is False
      and p4_db.get_run("smoke-life-orphan")["status"] == "aborted")

no_id = finish_run_tool("")
check("finish_run without a run_id fails loudly but never gates",
      no_id.ok is False and no_id.proceed is True)

# The lifecycle tools must never be served from the replay snapshot: a
# cached "run closed" would leave the run open in the database forever.
os.environ["BOB_THE_TESTER_REPLAY"] = "1"
try:
    replay_res, was_replayed = p4_replay.replay_or_run(
        "finish_run", {"run_id": "smoke-life-replay"},
        lambda: finish_run_tool("smoke-life-replay", status="complete"),
    )
    check("finish_run runs for real even in replay mode",
          was_replayed is False
          and p4_db.get_run("smoke-life-replay")["status"] == "complete")
    check("lifecycle calls are not written into the replay snapshot",
          not any(f.name.startswith(("finish_run__", "start_run__"))
                  for f in p4_replay.cache_dir().glob("*.json")))
finally:
    os.environ["BOB_THE_TESTER_REPLAY"] = "0"

# ── Clean up P4 fixtures ─────────────────────────────────────────────────────
for _var in ("BOB_THE_TESTER_DB", "BOB_THE_TESTER_REPLAY_DIR", "BOB_THE_TESTER_REPLAY"):
    os.environ.pop(_var, None)
try:
    Path(cov_xml_p4).unlink(missing_ok=True)
except Exception:  # noqa: BLE001
    pass
shutil.rmtree(P4_TMP, ignore_errors=True)

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

section("SUMMARY")
if not failures:
    print(f"\n  {PASS} All P1 + P2 + P4 smoke checks passed.")
    print(f"  Server is ready. Register in .bob/mcp.json and run Bob's skill.\n")
    sys.exit(0)
else:
    print(f"\n  {FAIL} {len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"    * {f}")
    print()
    sys.exit(1)
