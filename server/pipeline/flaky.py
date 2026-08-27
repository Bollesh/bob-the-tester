"""
run_flaky_check(test_file, n=5) — P2 pipeline stage 6.

Runs the same test file N times and reports any test whose outcome is not
identical across every run.  No LLM, no judgement: a test is flaky if and
only if its outcome vector is inconsistent.

Details payload (AGENTS.md §5):
    outcomes    dict — {test_id: ["passed", "passed", "failed", …]} one entry
                       per run, in run order
    flaky       list — [{test_id, outcomes, distinct}] tests that varied
    stable      list — test ids whose outcome never changed
    runs        int  — how many times the suite was executed
    runs_completed int— runs that produced a parsable report
    consistently_failing list — tests that failed in EVERY run.  These are
                       NOT flaky; they are deterministically broken and are
                       reported separately so Bob does not confuse the two.

Gate policy (AGENTS.md §3 and server/main.py): proven flakiness IS a hard
gate — proceed=False when any test is flaky.  A test that cannot make up
its mind is worthless as a regression signal and must be discarded before
the pipeline advances.  Deterministic failures do not set proceed=False
here; run_tests already gates those.

Stage isolation (AGENTS.md §6): server/pipeline/ modules never import each
other, so the small JUnit parser below is intentionally local rather than
shared with core/run_tests.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.schema import ToolResult

# Per-run subprocess ceiling.  N runs × this is the worst case; the tool
# stops early and reports partial data rather than blocking the pipeline.
_PER_RUN_TIMEOUT_S = 120

# Guard rails on n — 1 run cannot detect flakiness, and beyond ~20 the
# wall-clock cost stops being worth it inside a live demo.
_MIN_RUNS = 2
_MAX_RUNS = 20


# ─────────────────────────────────────────────────────────────────────────────
# Local JUnit parser — one outcome per test case
# ─────────────────────────────────────────────────────────────────────────────

def _parse_outcomes(xml_path: str) -> dict[str, str]:
    """
    Parse a JUnit XML report into {test_id: outcome}.

    test_id is "classname::name" so that same-named tests in different
    modules do not collide.  Outcome is one of:
    passed / failed / error / skipped.
    """
    outcomes: dict[str, str] = {}

    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError):
        return outcomes

    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    for suite in suites:
        for tc in suite.findall("testcase"):
            name = tc.get("name", "")
            classname = tc.get("classname", "")
            test_id = f"{classname}::{name}" if classname else name

            if tc.find("failure") is not None:
                outcomes[test_id] = "failed"
            elif tc.find("error") is not None:
                outcomes[test_id] = "error"
            elif tc.find("skipped") is not None:
                outcomes[test_id] = "skipped"
            else:
                outcomes[test_id] = "passed"

    return outcomes


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def run_flaky_check(
    test_file: str,
    n: int = 5,
    cwd: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Execute a test file N times and identify tests with inconsistent outcomes.

    Args:
        test_file: Absolute path to the test file to repeat.
        n:         Number of repetitions (clamped to 2–20; default 5).
        cwd:       Project root for the pytest subprocess.  Defaults to two
                   directories up from test_file, matching the repo layout
                   where tests live in <root>/tests/.
        run_id:    Optional UUID shared across all tool calls in one run.

    Returns:
        ToolResult with proceed=False when any test is proven flaky.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return (time.monotonic_ns() - t0) // 1_000_000

    def _fail(verdict: str, details: dict[str, Any]) -> ToolResult:
        """Tool-level failure: ok=False, and halt because we proved nothing."""
        return ToolResult(
            tool="run_flaky_check",
            ok=False,
            score=0.0,
            proceed=False,
            verdict=verdict,
            details=details,
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    test_path = Path(test_file)
    if not test_path.is_file():
        return _fail(
            f"Test file not found: {test_file}",
            {"error": "file_not_found", "flaky": [], "outcomes": {}},
        )

    # Resolve before use: a relative cwd would make the temp JUnit path
    # relative to it too, and pytest would then write outside the directory
    # we read back from.
    test_path = test_path.resolve()
    cwd = str(Path(cwd).resolve()) if cwd else str(test_path.parent.parent)

    runs = max(_MIN_RUNS, min(_MAX_RUNS, int(n)))

    # ── Execute the suite `runs` times ────────────────────────────────────
    per_run: list[dict[str, str]] = []
    timed_out = 0

    for _ in range(runs):
        with tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, dir=cwd, prefix="flaky_"
        ) as fh:
            junit_path = fh.name

        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    str(test_path),
                    f"--junit-xml={junit_path}",
                    "-p", "no:cacheprovider",   # no cross-run state
                    "--tb=no", "-q",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_PER_RUN_TIMEOUT_S,
            )
            outcomes = _parse_outcomes(junit_path)
            if outcomes:
                per_run.append(outcomes)

        except subprocess.TimeoutExpired:
            timed_out += 1
        except OSError as exc:
            return _fail(
                f"Could not start pytest: {exc}",
                {"error": str(exc), "flaky": [], "outcomes": {}},
            )
        finally:
            try:
                os.unlink(junit_path)
            except OSError:
                pass

    if not per_run:
        return _fail(
            f"All {runs} runs failed to produce a parsable report"
            + (f" ({timed_out} timed out)" if timed_out else ""),
            {
                "error": "no_parsable_runs",
                "runs": runs,
                "timed_out": timed_out,
                "flaky": [],
                "outcomes": {},
            },
        )

    # ── Build per-test outcome vectors ────────────────────────────────────
    # A test missing from a run records "missing" rather than being skipped
    # silently — disappearing from collection is itself an instability.
    all_test_ids: set[str] = set()
    for outcomes in per_run:
        all_test_ids.update(outcomes)

    vectors: dict[str, list[str]] = {
        test_id: [run.get(test_id, "missing") for run in per_run]
        for test_id in sorted(all_test_ids)
    }

    flaky: list[dict[str, Any]] = []
    stable: list[str] = []
    consistently_failing: list[str] = []

    for test_id, vector in vectors.items():
        distinct = sorted(set(vector))
        if len(distinct) > 1:
            flaky.append({
                "test_id": test_id,
                "outcomes": vector,
                "distinct": distinct,
            })
        else:
            stable.append(test_id)
            if distinct[0] in ("failed", "error"):
                consistently_failing.append(test_id)

    # ── Score and gate ────────────────────────────────────────────────────
    total = len(vectors)
    score = (len(stable) / total) if total else 0.0
    proceed = not flaky

    if not total:
        verdict = f"No tests collected across {len(per_run)} runs"
    elif flaky:
        names = ", ".join(f["test_id"].rpartition("::")[2] for f in flaky[:5])
        more = f" (+{len(flaky) - 5} more)" if len(flaky) > 5 else ""
        verdict = (
            f"{len(flaky)}/{total} test(s) FLAKY over {len(per_run)} runs: "
            f"{names}{more} — pipeline halted, discard these tests"
        )
    else:
        verdict = f"{total} tests stable across {len(per_run)} runs, no flakiness"
        if consistently_failing:
            verdict += (
                f" ({len(consistently_failing)} consistently failing — "
                f"deterministic, not flaky)"
            )

    if timed_out:
        verdict += f" [warning: {timed_out} run(s) timed out]"

    return ToolResult(
        tool="run_flaky_check",
        ok=True,
        score=round(score, 4),
        proceed=proceed,
        verdict=verdict,
        details={
            "outcomes": vectors,
            "flaky": flaky,
            "stable": stable,
            "consistently_failing": consistently_failing,
            "runs": runs,
            "runs_completed": len(per_run),
            "timed_out": timed_out,
        },
        artifacts=[],
        duration_ms=_elapsed_ms(),
        run_id=run_id,
    )
