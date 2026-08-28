"""
logging_mw.py — P4 tool-call logging middleware.

One entry point, called once per dispatch from server/main.py:

    log_tool_call(result, arguments, replayed=False)

It does two things:

  1. Writes the raw call to `tool_calls` — every tool, every time.  This is
     the "log everything" trail the contract promises (plan §0), and it is
     free precisely because the ToolResult envelope is uniform.

  2. Fans structured payloads out into the query-shaped tables, so the
     dashboard can plot without unpacking JSON:

        get_coverage      → coverage_history
        validate_and_keep → tests           (the keep/discard decision)
        run_flaky_check   → tests           (each flaky test, as a discard)
        mutation_test     → mutation_results

     Fan-out is READ-ONLY with respect to the tool result: this module
     never alters `ok`, `proceed`, `score`, or `details`.  Persistence
     observes the pipeline, it does not participate in it (AGENTS.md §7
     invariant 2 — the server computes `proceed`, and nothing downstream
     may reinterpret it).

Failure policy: every path is wrapped.  A database problem degrades to a
stderr warning; the tool result is returned to Bob unchanged either way.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from server.data import db
from server.data.models import CoveragePoint, MutationRow, TestRecord, ToolCall
from server.schema import ToolResult

logger = logging.getLogger("bob-the-tester.logging_mw")

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)


def _first_test_name(arguments: dict[str, Any]) -> str:
    """
    Best-effort test name for a validate_and_keep call.

    The candidate payload is {test_code, new_imports, insert_after_line};
    the name is whatever the first `def test_*` in the code is called.
    Returns '' when the code has no recognisable test function — the row is
    still worth writing, just without a name.
    """
    candidate = arguments.get("candidate")
    if not isinstance(candidate, dict):
        return ""
    match = _TEST_DEF.search(str(candidate.get("test_code", "")))
    return match.group(1) if match else ""


# ─────────────────────────────────────────────────────────────────────────────
# Fan-out handlers (one per tool that owns a dedicated table)
# ─────────────────────────────────────────────────────────────────────────────

def _fan_out_coverage(result: ToolResult, arguments: dict[str, Any]) -> None:
    details = result.details
    if "percent" not in details:
        return
    db.insert_coverage(CoveragePoint(
        run_id=result.run_id,
        percent=float(details.get("percent", 0.0)),
        iteration=int(arguments.get("iteration", 0) or 0),
        source_file=str(arguments.get("source_file", "")),
        covered_count=len(details.get("covered_lines", []) or []),
        uncovered_count=int(details.get("uncovered_count", 0) or 0),
    ))


def _fan_out_validate(result: ToolResult, arguments: dict[str, Any]) -> None:
    details = result.details
    db.insert_test(TestRecord(
        run_id=result.run_id,
        test_file=str(arguments.get("test_file", "")),
        test_name=_first_test_name(arguments),
        stage="validate",
        kept=bool(details.get("kept", False)),
        reason=str(details.get("reason", result.verdict)),
        coverage_before=details.get("coverage_before"),
        coverage_after=details.get("coverage_after"),
        rollback_performed=bool(details.get("rollback_performed", False)),
    ))


def _fan_out_flaky(result: ToolResult, arguments: dict[str, Any]) -> None:
    """
    Record every proven-flaky test as a discard.

    Flakiness is one of the three hard gates (AGENTS.md §3), so a flaky
    test is not a warning to weigh — it is removed.  Writing it into
    `tests` with kept=0 keeps the dashboard's kept/discarded table the
    single, complete account of every test's fate.
    """
    for entry in result.details.get("flaky", []) or []:
        if not isinstance(entry, dict):
            continue
        outcomes = entry.get("outcomes", [])
        db.insert_test(TestRecord(
            run_id=result.run_id,
            test_file=str(arguments.get("test_file", "")),
            test_name=str(entry.get("test_id", "")),
            stage="flaky",
            kept=False,
            reason=f"flaky: inconsistent outcomes across runs {outcomes}",
            rollback_performed=False,
        ))


def _fan_out_mutation(result: ToolResult, arguments: dict[str, Any]) -> None:
    details = result.details
    if "mutation_score" not in details:
        return
    db.insert_mutation(MutationRow(
        run_id=result.run_id,
        target_module=str(arguments.get("target_module", "")),
        mutation_score=float(details.get("mutation_score", 0.0)),
        killed=int(details.get("killed", 0) or 0),
        survived_count=int(details.get("survived_count", 0) or 0),
        not_covered=int(details.get("not_covered", 0) or 0),
        total=int(details.get("total", 0) or 0),
        inconclusive=int(details.get("inconclusive", 0) or 0),
        survivors=details.get("survived", []) or [],
        engine_available=bool(details.get("engine_available", True)),
    ))


_FAN_OUT = {
    "get_coverage": _fan_out_coverage,
    "validate_and_keep": _fan_out_validate,
    "run_flaky_check": _fan_out_flaky,
    "mutation_test": _fan_out_mutation,
}


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def log_tool_call(
    result: ToolResult,
    arguments: dict[str, Any] | None = None,
    replayed: bool = False,
) -> ToolResult:
    """
    Persist one tool call.  Returns `result` unchanged, so the dispatcher
    can wrap a call inline:  return log_tool_call(result, arguments)
    """
    arguments = arguments or {}
    try:
        db.insert_tool_call(ToolCall(
            run_id=result.run_id,
            tool=result.tool,
            ok=result.ok,
            proceed=result.proceed,
            score=result.score,
            verdict=result.verdict,
            arguments=arguments,
            details=result.details,
            artifacts=result.artifacts,
            duration_ms=result.duration_ms,
            replayed=replayed,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_calls write failed for %s: %s", result.tool, exc)

    # Fan-out only for results the tool itself produced successfully.
    # A crashed tool has no trustworthy details to promote into a typed row.
    if result.ok:
        handler = _FAN_OUT.get(result.tool)
        if handler is not None:
            try:
                handler(result, arguments)
            except Exception as exc:  # noqa: BLE001
                logger.warning("fan-out failed for %s: %s", result.tool, exc)

    return result
