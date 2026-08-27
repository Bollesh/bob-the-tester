"""
run_property_tests(target, max_examples) — P2 pipeline stage 7.

Executes Hypothesis property-based tests and returns SHRUNK counterexamples
as structured repro data.

Division of labour (AGENTS.md §1): Bob writes the invariants (that is the
judgement part); this tool only executes them and reports what the engine
found.  The shrunk counterexample is the valuable output — Bob turns each
one into a named, permanent regression test in the next skill step.

Details payload (AGENTS.md §5):
    failures      list — [{property, shrunk_input, exception, traceback_tail}]
    passed        int  — property tests that held for every generated example
    failed        int  — property tests with at least one counterexample
    max_examples  int  — examples requested per property
    engine_available bool — False when Hypothesis is not installed

Gate policy (AGENTS.md §3 and server/main.py): proceed is ALWAYS True.  A
counterexample is a FINDING, not a pipeline stop: it means the engine did
its job.  Bob converts it into a regression test, which then re-enters
validate_and_keep.

Stage isolation (AGENTS.md §6): no imports from sibling pipeline modules.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.schema import ToolResult

_TIMEOUT_S = 300           # property runs are slower than unit tests
_DEFAULT_MAX_EXAMPLES = 100
_MIN_EXAMPLES = 1
_MAX_EXAMPLES_CAP = 10_000

# Hypothesis renamed this banner: 6.x prints "Failing test case:", older
# releases printed "Falsifying example:".  Both are accepted so the tool does
# not silently lose counterexamples when the pinned version moves.
_COUNTEREXAMPLE_MARKER = re.compile(r"(?:Falsifying example|Failing test case):\s*")

# pytest indents traceback body with an "E   " gutter; the same text appears
# ungutted in the JUnit `message` attribute.  Stripping it lets one pattern
# match both sources.
_GUTTER_RE = re.compile(r"^E\s{2,}", re.MULTILINE)

# Trailing "path/to/test.py:13: AssertionError"
_EXCEPTION_TYPE_RE = re.compile(r":\s*(?P<exc>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\s*$",
                                re.MULTILINE)


# ─────────────────────────────────────────────────────────────────────────────
# Hypothesis profile injection
# ─────────────────────────────────────────────────────────────────────────────

def _write_profile_plugin(tmp_dir: str, max_examples: int) -> str:
    """
    Write a throwaway pytest plugin that registers and loads a Hypothesis
    profile with the requested example budget.

    Hypothesis exposes no --max-examples CLI flag; the supported way to set
    it externally is a registered profile.  Injecting it as a plugin keeps
    us from touching the project's own conftest.py, which belongs to P5 and
    must not be rewritten by a tool.
    """
    plugin_name = "bob_hypothesis_profile"
    plugin_path = Path(tmp_dir) / f"{plugin_name}.py"
    plugin_path.write_text(
        "from hypothesis import settings\n"
        "\n"
        f"settings.register_profile('bob_the_tester', max_examples={max_examples}, "
        "deadline=None, print_blob=True)\n"
        "settings.load_profile('bob_the_tester')\n",
        encoding="utf-8",
    )
    return plugin_name


# ─────────────────────────────────────────────────────────────────────────────
# Failure extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_counterexample(text: str) -> str | None:
    """
    Pull the shrunk counterexample out of a Hypothesis failure report.

    The banner is followed by a call expression — either inline
    ``test_foo(x=0)`` or spread over lines::

        Failing test case: test_bounded(
            x=100,
        )

    Rather than guess at line breaks, this walks the text from the opening
    parenthesis and stops when the parentheses balance, which handles both
    layouts and nested calls in the argument values.
    """
    match = _COUNTEREXAMPLE_MARKER.search(text)
    if not match:
        return None

    rest = text[match.end():]
    open_idx = rest.find("(")
    if open_idx == -1:
        # Banner with no call expression — return the first line as-is.
        first = rest.strip().splitlines()
        return first[0].strip() if first else None

    depth = 0
    for offset, char in enumerate(rest[open_idx:], start=open_idx):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return rest[:offset + 1].strip()

    # Unbalanced (truncated output) — return what we have, bounded.
    return rest[:500].strip()


def _extract_exception(message: str, text: str) -> str:
    """
    One-line description of why the property failed.

    The JUnit `message` attribute leads with the assertion pytest printed
    (e.g. "assert 100 < 100"), which is the most useful single line.  The
    exception type is appended from the traceback tail when available.
    """
    first_line = ""
    for line in message.strip().splitlines():
        stripped = line.strip()
        if stripped and not _COUNTEREXAMPLE_MARKER.match(stripped):
            first_line = stripped
            break

    type_match = None
    for type_match in _EXCEPTION_TYPE_RE.finditer(text):
        pass  # keep the last — the traceback tail names the real exception
    exc_type = type_match.group("exc") if type_match else ""

    if first_line and exc_type:
        return f"{exc_type}: {first_line}"[:300]
    return (first_line or exc_type or "unknown failure")[:300]


def _parse_failures(xml_path: str) -> tuple[list[dict[str, Any]], int, int]:
    """
    Parse the JUnit report into (failures, passed_count, failed_count).

    Each failure entry carries the shrunk counterexample when Hypothesis
    reported one.  Tests that failed without a falsifying example (a plain
    assertion failure in a non-property test) still appear, with
    shrunk_input=None, so nothing is silently dropped.
    """
    failures: list[dict[str, Any]] = []
    passed = failed = 0

    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError):
        return failures, 0, 0

    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    for suite in suites:
        for tc in suite.findall("testcase"):
            name = tc.get("name", "")
            failure_el = tc.find("failure")
            error_el = tc.find("error")
            problem = failure_el if failure_el is not None else error_el

            if problem is None:
                if tc.find("skipped") is None:
                    passed += 1
                continue

            failed += 1
            message = problem.get("message") or ""
            body = problem.text or ""

            # The counterexample appears in both sources; the message
            # attribute carries it ungutted, so search there first.
            searchable = _GUTTER_RE.sub("", message + "\n" + body)

            failures.append({
                "property": name,
                "shrunk_input": _extract_counterexample(searchable),
                "exception": _extract_exception(message, body),
                "traceback_tail": "\n".join(body.strip().splitlines()[-25:]),
            })

    return failures, passed, failed


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def run_property_tests(
    target: str,
    max_examples: int = _DEFAULT_MAX_EXAMPLES,
    cwd: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Execute Hypothesis property tests and return shrunk counterexamples.

    Args:
        target:       Absolute path to the test file (or a pytest node id
                      such as 'tests/test_props.py::test_roundtrip')
                      containing the @given properties.
        max_examples: Examples to generate per property (default 100).
        cwd:          Project root for the pytest subprocess.  Defaults to
                      two directories up from the target file.
        run_id:       Optional UUID shared across one pipeline run.

    Returns:
        ToolResult with proceed=True always; counterexamples are findings.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return (time.monotonic_ns() - t0) // 1_000_000

    max_examples = max(_MIN_EXAMPLES, min(_MAX_EXAMPLES_CAP, int(max_examples)))

    # ── Engine availability ───────────────────────────────────────────────
    # Reported as ok=False (the environment is misconfigured) but proceed=True
    # so a missing optional engine never stalls a demo.  AGENTS.md §3 gate
    # policy plus the pre-agreed risk cuts in the implementation plan.
    if importlib.util.find_spec("hypothesis") is None:
        return ToolResult(
            tool="run_property_tests",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=(
                "Hypothesis is not installed — property stage skipped. "
                "Install with: pip install -e \".[pipeline]\""
            ),
            details={
                "error": "hypothesis_not_installed",
                "engine_available": False,
                "failures": [],
                "passed": 0,
                "failed": 0,
            },
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    # A node id ('file.py::test_x') must be split before the path check.
    target_path = Path(target.partition("::")[0])
    if not target_path.is_file():
        return ToolResult(
            tool="run_property_tests",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=f"Property test target not found: {target_path}",
            details={
                "error": "target_not_found",
                "engine_available": True,
                "failures": [],
                "passed": 0,
                "failed": 0,
            },
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    # Resolve before use — a relative cwd would misplace the temp JUnit path.
    target_path = target_path.resolve()
    cwd = str(Path(cwd).resolve()) if cwd else str(target_path.parent.parent)

    # Rebuild the node id against the resolved path so '::test_x' survives.
    _, sep, node_suffix = target.partition("::")
    target = f"{target_path}{sep}{node_suffix}" if sep else str(target_path)

    # ── Run ───────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="bob_props_") as tmp_dir:
        plugin_name = _write_profile_plugin(tmp_dir, max_examples)

        with tempfile.NamedTemporaryFile(
            suffix=".xml", delete=False, dir=cwd, prefix="props_"
        ) as fh:
            junit_path = fh.name

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [tmp_dir, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "pytest",
                    target,
                    "-p", plugin_name,
                    f"--junit-xml={junit_path}",
                    "-p", "no:cacheprovider",
                    "--tb=long", "-q",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                env=env,
            )

            failures, passed, failed = _parse_failures(junit_path)

            total = passed + failed
            score = (passed / total) if total else 0.0

            if total == 0:
                verdict = (
                    "No property tests were collected — check that the target "
                    "contains @given-decorated tests"
                )
            elif failures:
                names = ", ".join(f["property"] for f in failures[:5])
                more = f" (+{len(failures) - 5} more)" if len(failures) > 5 else ""
                verdict = (
                    f"{failed}/{total} propert(ies) falsified at "
                    f"{max_examples} examples: {names}{more} "
                    f"— convert each shrunk_input into a regression test"
                )
            else:
                verdict = (
                    f"{passed} propert(ies) held over {max_examples} examples each"
                )

            return ToolResult(
                tool="run_property_tests",
                ok=True,
                score=round(score, 4),
                proceed=True,      # counterexamples are findings — AGENTS.md §3
                verdict=verdict,
                details={
                    "failures": failures,
                    "passed": passed,
                    "failed": failed,
                    "max_examples": max_examples,
                    "engine_available": True,
                    "return_code": proc.returncode,
                    "stdout_tail": "\n".join(proc.stdout.splitlines()[-40:]),
                },
                artifacts=[],
                duration_ms=_elapsed_ms(),
                run_id=run_id,
            )

        except subprocess.TimeoutExpired:
            return ToolResult(
                tool="run_property_tests",
                ok=False,
                score=0.0,
                proceed=True,
                verdict=(
                    f"Property run exceeded {_TIMEOUT_S}s at "
                    f"max_examples={max_examples} — lower max_examples and retry"
                ),
                details={
                    "error": f"timeout_{_TIMEOUT_S}s",
                    "engine_available": True,
                    "max_examples": max_examples,
                    "failures": [],
                    "passed": 0,
                    "failed": 0,
                },
                artifacts=[],
                duration_ms=_elapsed_ms(),
                run_id=run_id,
            )

        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool="run_property_tests",
                ok=False,
                score=0.0,
                proceed=True,
                verdict=f"Tool internal error: {exc}",
                details={
                    "error": str(exc),
                    "engine_available": True,
                    "failures": [],
                    "passed": 0,
                    "failed": 0,
                },
                artifacts=[],
                duration_ms=_elapsed_ms(),
                run_id=run_id,
            )

        finally:
            try:
                os.unlink(junit_path)
            except OSError:
                pass
