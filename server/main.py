"""
Bob the Tester MCP Server — entry point and tool registration.

Ownership and merge rules (from AGENTS.md §6):
    P1 registers core tools in the P1 block below.
    P2 adds pipeline tools in the P2 block.
    P4 adds data tools in the P4 block.

    This file IS the designated merge-conflict point.  Conflicts are
    always resolved by keeping BOTH sides.  Register tools in P-number
    order to keep diffs stable and conflicts mechanical.

    DO NOT add business logic here.  Every tool implementation lives in
    its own module under server/core/, server/pipeline/, or server/data/.
    This file only does: import → register → dispatch.

Usage:
    python -m server.main     (direct)
    bob-the-tester-server     (installed entry-point)
    Registered in .bob/mcp.json as STDIO transport.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from server.schema import ToolResult

# ── P1 core imports ──────────────────────────────────────────────────────────
from server.core.coverage import get_coverage, list_uncovered
from server.core.run_tests import run_tests
from server.core.validate import validate_and_keep

# ── P2 pipeline imports ─────────────────────────────────────────────────────
from server.pipeline.flaky import run_flaky_check
from server.pipeline.fuzz import run_fuzz
from server.pipeline.mutation import mutation_test
from server.pipeline.properties import run_property_tests
from server.pipeline.smells import detect_smells

# ── P4 data imports (added here when P4 branch merges) ──────────────────────
# Pattern:
#   from server.data.logging_mw import log_tool_call   # wrap result before return
#   from server.data.replay import replay_or_run        # BOB_THE_TESTER_REPLAY gate
#   from server.gaps import explain_gaps

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [bob-the-tester] %(levelname)s %(message)s")
logger = logging.getLogger("bob-the-tester")

app = Server("bob-the-tester")


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema declarations (Bob reads these descriptions)
# ─────────────────────────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [

        # ════════════════════════════════════════════════════════════════
        # P1 — CORE TOOLS
        # ════════════════════════════════════════════════════════════════

        Tool(
            name="run_tests",
            description=(
                "Run the pytest test suite and return structured pass/fail results.\n\n"
                "Returns ok=true even when tests fail (the tool itself ran fine).\n"
                "proceed=false when any tests FAIL or ERROR — stop the pipeline "
                "immediately and do not advance to the next stage.\n\n"
                "Decision fields in details:\n"
                "  passed, failed, errors, skipped  — raw counts\n"
                "  junit_summary                    — list of {name, classname, status, message}\n"
                "  stderr_tail                      — last 30 lines of stderr\n"
                "  return_code                      — raw pytest exit code\n\n"
                "If proceed=false, inspect junit_summary[status='failed'].message to understand "
                "what went wrong before regenerating."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "test_command": {
                        "type": "string",
                        "description": (
                            "pytest arguments string, e.g. 'tests/ -x -v' or "
                            "'tests/test_pricing.py::test_discount'.  "
                            "Do NOT include --junit-xml (added automatically)."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Absolute path to the project root (working directory for pytest).",
                    },
                    "run_id": {
                        "type": "string",
                        "description": "Optional UUID to correlate this call with others in the same pipeline run.",
                    },
                },
                "required": ["test_command", "cwd"],
            },
        ),

        Tool(
            name="get_coverage",
            description=(
                "Parse a Cobertura XML coverage report and return the coverage percentage "
                "plus uncovered line ranges.\n\n"
                "Always call this after run_tests to measure coverage.  "
                "proceed is always true — coverage data is informational, not a gate.\n\n"
                "Stale-report guard: if run_start_ms is provided and the report file is "
                "older than the test run, the tool fails with ok=false.  Always pass "
                "run_start_ms when you know it.\n\n"
                "Decision fields in details:\n"
                "  percent          — 0.0–100.0 line coverage\n"
                "  covered_lines    — list of covered line numbers\n"
                "  uncovered_ranges — list of {start, end} ranges\n"
                "  uncovered_count  — total uncovered line count\n\n"
                "Use uncovered_ranges as input to list_uncovered for function-level gap data."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_path": {
                        "type": "string",
                        "description": "Absolute path to coverage.xml (Cobertura format, from pytest-cov).",
                    },
                    "source_file": {
                        "type": "string",
                        "description": (
                            "Optional relative path of the source file to filter, "
                            "e.g. 'src/pricing.py'.  "
                            "Empty → aggregate across all files in the report."
                        ),
                    },
                    "run_start_ms": {
                        "type": "integer",
                        "description": "Epoch milliseconds when the test run started (for stale-report guard).",
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["report_path"],
            },
        ),

        Tool(
            name="list_uncovered",
            description=(
                "Return a prompt-ready gap map of uncovered code in a source file.\n\n"
                "Each gap entry includes:\n"
                "  start, end — line range\n"
                "  function   — name of the enclosing function/method (AST-derived)\n"
                "  snippet    — source lines in that range\n\n"
                "Use this to decide which functions to target in the next generation round.  "
                "Requires get_coverage to have been called first (or pass report_path).\n\n"
                "proceed is always true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_file": {
                        "type": "string",
                        "description": "Absolute path to the Python source file to inspect.",
                    },
                    "report_path": {
                        "type": "string",
                        "description": (
                            "Path to coverage.xml.  "
                            "If omitted, uses the most recent report parsed by get_coverage."
                        ),
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["source_file"],
            },
        ),

        Tool(
            name="validate_and_keep",
            description=(
                "Insert a candidate test and keep it ONLY IF the suite passes AND "
                "coverage strictly increases — the Assured-LLMSE filter.\n\n"
                "On any failure the test file is FULLY ROLLED BACK.  "
                "Never bypass this tool to keep a test manually.\n\n"
                "Candidate object fields:\n"
                "  test_code         — complete def test_…(): block (required)\n"
                "  new_imports       — import lines to prepend (blank if not needed)\n"
                "  insert_after_line — 1-indexed line to insert after (default: EOF)\n\n"
                "Decision fields in details:\n"
                "  kept               — true = accepted, false = discarded\n"
                "  reason             — WHY the decision was made; use in the next prompt\n"
                "  coverage_before    — percent before insertion\n"
                "  coverage_after     — percent after (null if discarded at suite-fail step)\n"
                "  rollback_performed — always true when kept=false\n\n"
                "When kept=false, read details.reason carefully.  "
                "If the suite failed, use the run_result inside details to see which "
                "existing tests broke.  If coverage did not increase, the candidate "
                "only exercises already-covered lines — target a different gap."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "test_file": {
                        "type": "string",
                        "description": "Absolute path to the test file to insert into.",
                    },
                    "candidate": {
                        "type": "object",
                        "description": "The candidate test to validate.",
                        "properties": {
                            "test_code": {
                                "type": "string",
                                "description": (
                                    "Complete test function code including def, docstring, "
                                    "and all assertions.  Must be valid Python."
                                ),
                            },
                            "new_imports": {
                                "type": "string",
                                "description": "Import lines needed by this test; blank if none.",
                            },
                            "insert_after_line": {
                                "type": "integer",
                                "description": "1-indexed line after which to insert.  Default: EOF.",
                            },
                        },
                        "required": ["test_code"],
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Project root.  "
                            "Defaults to two directories up from test_file "
                            "(i.e. repo root when tests/ is one level in)."
                        ),
                    },
                    "source_file": {
                        "type": "string",
                        "description": (
                            "Relative path (from cwd) of the source module to measure coverage on, "
                            "e.g. 'src/pricing.py'.  Empty → measure all files."
                        ),
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["test_file", "candidate"],
            },
        ),

        # ════════════════════════════════════════════════════════════════
        # P2 — PIPELINE TOOLS  (declared in pipeline order: smells first,
        #                       mutation last — AGENTS.md §4)
        # ════════════════════════════════════════════════════════════════

        Tool(
            name="detect_smells",
            description=(
                "Static smell checks on a test file.  Returns RAW FINDINGS ONLY — "
                "critiquing them is your job, not the tool's.\n\n"
                "proceed is always true: a smelly test can still be a correct, "
                "coverage-adding test, so smells never halt the pipeline.\n\n"
                "Smell types reported:\n"
                "  no_assertion       — the test asserts nothing at all\n"
                "  empty_test         — body is only pass/.../docstring\n"
                "  trivial_assertion  — assert True, `x is not None`, bare assertTrue\n"
                "  duplicate_body     — structurally identical to an earlier test\n"
                "  sleep_call         — time.sleep(), a flakiness source\n\n"
                "Decision fields in details:\n"
                "  findings      — [{test_name, smell_type, line, snippet}]\n"
                "  tests_scanned — how many test functions were analysed\n"
                "  smell_score   — 1.0 clean … 0.0 every test smelly\n"
                "  by_type       — {smell_type: count}\n\n"
                "After reading findings, rewrite weak assertions into behavioural "
                "ones and re-validate each rewrite with validate_and_keep."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "test_file": {
                        "type": "string",
                        "description": "Absolute path to the Python test file to analyse.",
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["test_file"],
            },
        ),

        Tool(
            name="run_flaky_check",
            description=(
                "Run a test file N times and report any test whose outcome is not "
                "identical every time.\n\n"
                "proceed=false when ANY test is flaky — this IS a hard gate.  A test "
                "that cannot make up its mind is worthless as a regression signal; "
                "discard the tests listed in details.flaky before continuing.\n\n"
                "Decision fields in details:\n"
                "  flaky                — [{test_id, outcomes, distinct}] discard these\n"
                "  outcomes             — {test_id: [outcome per run]} full vectors\n"
                "  stable               — test ids that never varied\n"
                "  consistently_failing — failed in EVERY run: deterministically\n"
                "                         broken, NOT flaky.  Do not confuse the two;\n"
                "                         these may indicate a real bug in the code\n"
                "                         under test rather than a bad test.\n"
                "  runs_completed       — runs that produced a parsable report"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "test_file": {
                        "type": "string",
                        "description": "Absolute path to the test file to repeat.",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Repetitions, clamped to 2–20.  Default 5.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Project root for pytest.  Defaults to two directories "
                            "up from test_file."
                        ),
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["test_file"],
            },
        ),

        Tool(
            name="run_property_tests",
            description=(
                "Execute Hypothesis property tests and return SHRUNK counterexamples.\n\n"
                "You write the properties; this tool only runs them.  proceed is "
                "always true — a counterexample is a FINDING, not a failure: it means "
                "the engine did its job.  Convert each one into a named, docstring'd "
                "regression test and re-validate it with validate_and_keep.\n\n"
                "Decision fields in details:\n"
                "  failures        — [{property, shrunk_input, exception, traceback_tail}]\n"
                "  shrunk_input    — the MINIMAL failing call, e.g. 'test_f(\\n  x=100,\\n)'\n"
                "                    Use these exact values in the regression test.\n"
                "  passed / failed — property counts\n"
                "  engine_available— false when Hypothesis is not installed\n\n"
                "If engine_available is false the stage was skipped, not failed; "
                "continue the pipeline."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Absolute path to the file containing @given properties, "
                            "or a pytest node id like 'tests/test_props.py::test_roundtrip'."
                        ),
                    },
                    "max_examples": {
                        "type": "integer",
                        "description": "Examples generated per property.  Default 100.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Project root for pytest.  Defaults to two levels up from target.",
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["target"],
            },
        ),

        Tool(
            name="run_fuzz",
            description=(
                "Run an Atheris fuzz harness under a time budget and report crashes.\n\n"
                "You write the harness (it must call atheris.Setup(sys.argv, "
                "TestOneInput) then atheris.Fuzz()); this tool only executes it.  "
                "proceed is always true — a crash is a FINDING.  Turn each crashing "
                "input into a named regression test.\n\n"
                "Decision fields in details:\n"
                "  crashes          — [{input_repr, input_file, exception, stack_top}]\n"
                "  input_repr       — repr() of the crashing bytes; reuse verbatim\n"
                "  execs_per_sec    — throughput libFuzzer reported\n"
                "  engine_available — false when Atheris is not installed\n\n"
                "Atheris needs a clang/libFuzzer toolchain and is the project's "
                "pre-agreed first cut.  If engine_available is false, skip this stage "
                "and rely on run_property_tests for machine-found inputs — do NOT "
                "treat it as a pipeline failure."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "harness_path": {
                        "type": "string",
                        "description": "Absolute path to the Atheris harness you wrote.",
                    },
                    "seconds": {
                        "type": "integer",
                        "description": "Wall-clock fuzzing budget, clamped to 1–3600.  Default 60.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory.  Defaults to the harness's own directory.",
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["harness_path"],
            },
        ),

        Tool(
            name="mutation_test",
            description=(
                "Mutation-test ONE module: mutate its code and report which mutants "
                "the suite failed to kill.  This is the project's test-strength "
                "metric — coverage says the tests RAN the code, the mutation score "
                "says they would NOTICE if it were wrong.\n\n"
                "proceed is always true.  Surviving mutants are regeneration work: "
                "write tests that specifically kill them, then re-run this tool and "
                "confirm the score rose.\n\n"
                "Decision fields in details:\n"
                "  mutation_score — killed / (killed + survived)\n"
                "  survived       — [{id, line, original, mutated, status, function}]\n"
                "  status per survivor tells you WHICH FIX to apply:\n"
                "    'survived'  — a test ran that line and missed the change.\n"
                "                  The assertion is too weak: strengthen it.\n"
                "    'no tests'  — no test executed the line at all.\n"
                "                  Write a new test; a better assertion won't help.\n"
                "  line / original / mutated — exactly what changed, so you can\n"
                "                  write an assertion that distinguishes them\n"
                "  not_covered    — how many survivors were never executed\n\n"
                "Expensive: always scope to one module and run it last."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_module": {
                        "type": "string",
                        "description": (
                            "Module to mutate — absolute, or relative to cwd "
                            "(e.g. 'src/pricing.py')."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Project root.  Required when target_module is relative; "
                            "otherwise defaults to two directories up from it."
                        ),
                    },
                    "tests_dir": {
                        "type": "string",
                        "description": "Test selection passed to pytest, relative to cwd.  Default 'tests'.",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Ceiling for the whole mutmut run in seconds.  Default 900.",
                    },
                    "run_id": {"type": "string"},
                },
                "required": ["target_module"],
            },
        ),

        # ════════════════════════════════════════════════════════════════
        # P4 — DATA / EXPLAIN TOOLS
        # ════════════════════════════════════════════════════════════════
        # Tool(name="explain_gaps", ...),
        # Tool(name="store_explanation", ...),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Tool call dispatcher
# ─────────────────────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Route MCP tool calls to their implementations."""
    result: ToolResult

    try:
        # ── P1 core tools ─────────────────────────────────────────────────
        if name == "run_tests":
            result = run_tests(
                test_command=arguments["test_command"],
                cwd=arguments["cwd"],
                run_id=arguments.get("run_id"),
            )

        elif name == "get_coverage":
            result = get_coverage(
                report_path=arguments["report_path"],
                source_file=arguments.get("source_file", ""),
                run_start_ms=arguments.get("run_start_ms"),
                run_id=arguments.get("run_id"),
            )

        elif name == "list_uncovered":
            result = list_uncovered(
                source_file=arguments["source_file"],
                report_path=arguments.get("report_path", ""),
                run_id=arguments.get("run_id"),
            )

        elif name == "validate_and_keep":
            result = validate_and_keep(
                test_file=arguments["test_file"],
                candidate=arguments["candidate"],
                cwd=arguments.get("cwd", ""),
                source_file=arguments.get("source_file", ""),
                run_id=arguments.get("run_id"),
            )

        # ── P2 pipeline tools ─────────────────────────────────────────────
        elif name == "detect_smells":
            result = detect_smells(
                test_file=arguments["test_file"],
                run_id=arguments.get("run_id"),
            )

        elif name == "run_flaky_check":
            result = run_flaky_check(
                test_file=arguments["test_file"],
                n=arguments.get("n", 5),
                cwd=arguments.get("cwd", ""),
                run_id=arguments.get("run_id"),
            )

        elif name == "run_property_tests":
            result = run_property_tests(
                target=arguments["target"],
                max_examples=arguments.get("max_examples", 100),
                cwd=arguments.get("cwd", ""),
                run_id=arguments.get("run_id"),
            )

        elif name == "run_fuzz":
            result = run_fuzz(
                harness_path=arguments["harness_path"],
                seconds=arguments.get("seconds", 60),
                cwd=arguments.get("cwd", ""),
                run_id=arguments.get("run_id"),
            )

        elif name == "mutation_test":
            result = mutation_test(
                target_module=arguments["target_module"],
                cwd=arguments.get("cwd", ""),
                tests_dir=arguments.get("tests_dir", "tests"),
                timeout_s=arguments.get("timeout_s", 900),
                run_id=arguments.get("run_id"),
            )

        # ── P4 data tools ─────────────────────────────────────────────────
        # Pattern:
        #   elif name == "explain_gaps":
        #       result = explain_gaps(
        #           source_file=arguments["source_file"],
        #           run_id=arguments.get("run_id"),
        #       )
        # elif name == "explain_gaps":    ...

        else:
            raise ValueError(f"Unknown tool: '{name}'.  Check list_tools() for valid names.")

    except Exception as exc:  # noqa: BLE001
        logger.exception("Dispatch error for tool '%s'", name)
        result = ToolResult(
            tool=name,
            ok=False,
            score=0.0,
            proceed=False,
            verdict=f"Dispatch error: {exc}",
            details={"error": str(exc)},
            artifacts=[],
            duration_ms=0,
            run_id=arguments.get("run_id", ""),
        )

    # ── P4 logging middleware ─────────────────────────────────────────────────
    # After result is computed (above), P4 adds ONE line here:
    #   log_tool_call(result, arguments)
    # That function (server/data/logging_mw.py) writes to the tool_calls table.
    # The run_id on every ToolResult is the correlation key for the dashboard.
    # P4's logging middleware will wrap this later (data/logging_mw.py)
    logger.info("tool=%s ok=%s proceed=%s verdict=%r",
                result.tool, result.ok, result.proceed, result.verdict)

    return [TextContent(type="text", text=result.model_dump_json(indent=2))]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def _amain() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
