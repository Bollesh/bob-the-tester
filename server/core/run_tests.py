"""
run_tests(test_command, cwd) — P1 core tool.

Shells out to pytest, captures JUnit XML output, returns a ToolResult.
No LLM calls.  Fully deterministic.

Details payload:
    passed       int   — test cases that passed
    failed       int   — test cases that failed (assertion errors, etc.)
    errors       int   — test cases that errored (import errors, fixtures, etc.)
    skipped      int   — test cases that were skipped
    junit_summary list — [{name, classname, status, message}] per test case
    stderr_tail  str   — last 30 lines of stderr
    stdout_tail  str   — last 50 lines of stdout
    return_code  int   — raw pytest exit code

proceed = False when failed > 0 or errors > 0 (suite is broken).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any

from server.schema import ToolResult


# ─────────────────────────────────────────────────────────────────────────────
# Argument splitting
# ─────────────────────────────────────────────────────────────────────────────

def _split_test_command(test_command: str, cwd: str) -> list[str]:
    """
    Turn the free-form `test_command` string into argv for pytest.

    A plain `.split()` shatters any path containing a space — a repo under
    "/New Volume/" collects zero tests — and `shlex.split` does not help on
    its own, because an UNQUOTED path with a space still splits.

    So: if the whole string names a path that exists (absolute, or relative
    to cwd), it is one argument and is passed through untouched.  Otherwise
    it is a real argument string like "tests/ -x -v" and shlex splits it,
    honouring any quoting the caller supplied.
    """
    stripped = test_command.strip()
    if not stripped:
        return []

    # Whole string is a single existing path → never split it.
    if os.path.exists(stripped) or os.path.exists(os.path.join(cwd, stripped)):
        return [stripped]

    # A pytest node id ("path/to/test.py::test_name") is also one argument.
    node_path = stripped.partition("::")[0]
    if node_path != stripped and (
        os.path.exists(node_path) or os.path.exists(os.path.join(cwd, node_path))
    ):
        return [stripped]

    try:
        return shlex.split(stripped)
    except ValueError:
        # Unbalanced quotes — fall back to a naive split rather than crashing.
        return stripped.split()


# ─────────────────────────────────────────────────────────────────────────────
# JUnit XML parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_junit(xml_path: str) -> dict[str, Any]:
    """
    Parse a JUnit XML report produced by pytest --junit-xml.

    Returns a dict with keys: passed, failed, errors, skipped, test_cases.
    Handles both <testsuites> (wrapper) and bare <testsuite> roots.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # pytest may produce <testsuites><testsuite>…</testsuite></testsuites>
        # or just <testsuite>…</testsuite> at the root.
        suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

        passed = failed = errors = skipped = 0
        test_cases: list[dict[str, str]] = []

        for suite in suites:
            for tc in suite.findall("testcase"):
                name = tc.get("name", "")
                classname = tc.get("classname", "")
                failure_el = tc.find("failure")
                error_el = tc.find("error")
                skip_el = tc.find("skipped")

                if failure_el is not None:
                    failed += 1
                    status = "failed"
                    msg = (failure_el.get("message") or failure_el.text or "")[:400]
                elif error_el is not None:
                    errors += 1
                    status = "error"
                    msg = (error_el.get("message") or error_el.text or "")[:400]
                elif skip_el is not None:
                    skipped += 1
                    status = "skipped"
                    msg = skip_el.get("message") or ""
                else:
                    passed += 1
                    status = "passed"
                    msg = ""

                test_cases.append({
                    "name": name,
                    "classname": classname,
                    "status": status,
                    "message": msg,
                })

        return {
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "test_cases": test_cases,
        }

    except ET.ParseError as exc:
        return {
            "parse_error": f"JUnit XML malformed: {exc}",
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "test_cases": [],
        }
    except FileNotFoundError:
        return {
            "parse_error": "JUnit XML file not found — pytest may have produced no output",
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "test_cases": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "parse_error": str(exc),
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "test_cases": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def run_tests(
    test_command: str,
    cwd: str,
    run_id: str | None = None,
) -> ToolResult:
    """
    Run the pytest suite and return structured pass/fail results.

    Args:
        test_command: pytest arguments string, e.g. "tests/ -x -v" or
                      "tests/test_pricing.py::test_discount".
                      Do NOT include --junit-xml (added automatically).
        cwd:          Absolute path to the project root (working directory
                      for the pytest subprocess).
        run_id:       Optional UUID shared across all tool calls in one
                      pipeline run.  Auto-generated if omitted.

    Returns:
        ToolResult with proceed=False when any tests fail or error.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    # Use a temp file inside cwd so relative paths in XML are correct
    with tempfile.NamedTemporaryFile(
        suffix=".xml", delete=False, dir=cwd, prefix="junit_"
    ) as fh:
        junit_path = fh.name

    try:
        # sys.executable, not "python": the bare name is absent on systems
        # that ship only python3, and it can resolve to a different
        # interpreter than the one running the server.
        cmd = [
            sys.executable, "-m", "pytest",
            *_split_test_command(test_command, cwd),
            f"--junit-xml={junit_path}",
            "--tb=short",
            "-q",
        ]

        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        stderr_tail = "\n".join(proc.stderr.splitlines()[-30:])
        stdout_tail = "\n".join(proc.stdout.splitlines()[-50:])

        junit = _parse_junit(junit_path)
        passed = junit.get("passed", 0)
        failed = junit.get("failed", 0)
        errors = junit.get("errors", 0)
        skipped = junit.get("skipped", 0)

        proceed = failed == 0 and errors == 0
        total = passed + failed + errors
        score = (passed / total) if total > 0 else 1.0 if proceed else 0.0

        verdict = f"{passed} passed, {failed} failed, {errors} errors, {skipped} skipped"
        if not proceed:
            verdict += " — suite FAILED, pipeline halted"
        if "parse_error" in junit:
            verdict = f"JUnit parse warning: {junit['parse_error']}. " + verdict

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="run_tests",
            ok=True,
            score=round(score, 4),
            proceed=proceed,
            verdict=verdict,
            details={
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": skipped,
                "junit_summary": junit.get("test_cases", []),
                "stderr_tail": stderr_tail,
                "stdout_tail": stdout_tail,
                "return_code": proc.returncode,
            },
            artifacts=[junit_path],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="run_tests",
            ok=False,
            score=0.0,
            proceed=False,
            verdict="Test run timed out after 120 seconds",
            details={"error": "timeout_120s"},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="run_tests",
            ok=False,
            score=0.0,
            proceed=False,
            verdict=f"Tool internal error: {exc}",
            details={"error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    finally:
        try:
            os.unlink(junit_path)
        except OSError:
            pass
