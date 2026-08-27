# Bob the Tester — FROZEN schema (Day 1 contract)
# DO NOT MODIFY outside a `contract-change` PR approved by all team members.
# See AGENTS.md §3 and §7 (invariant 7).
"""
ToolResult — the universal return envelope for every MCP tool in Bob the Tester.

Every tool implementation must return exactly this type.  The MCP server
dispatcher serialises it to JSON and sends it to Bob via STDIO.

Semantics (from AGENTS.md §3):
    ok       — the tool itself ran without internal error.  ok=False means
               the tool crashed or was misconfigured, NOT that the tests
               are bad.
    proceed  — pipeline sequencing signal computed BY THE SERVER, never by
               the LLM.  Bob must obey it: proceed=False → do not advance
               to the next stage; act on verdict/details instead.
    score    — 0.0–1.0 quality contribution of this stage.  Informational
               for the dashboard and final quality score rollup.
    details  — tool-specific structured payload.  Bob makes all decisions
               from structured fields, never by parsing verdict prose.
"""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


def _new_run_id() -> str:
    return str(uuid.uuid4())


class ToolResult(BaseModel):
    """Universal return envelope for every MCP tool in Bob the Tester."""

    # Which tool produced this result
    tool: str

    # True  → tool executed successfully (even if tests failed / coverage is low)
    # False → tool itself crashed / misconfigured
    ok: bool

    # 0.0 – 1.0 quality contribution; feeds the dashboard score rollup
    score: float = Field(ge=0.0, le=1.0)

    # Pipeline sequencing signal — computed by the server, never by the LLM.
    # False → halt pipeline; do not advance to next stage.
    proceed: bool

    # Human-readable summary (for logging / Bob's context window).
    # Bob MUST NOT parse this string; use `details` fields instead.
    verdict: str

    # Tool-specific structured payload.  Schemas documented in AGENTS.md §5.
    details: dict[str, Any] = Field(default_factory=dict)

    # Relative paths to any files produced (XML reports, harnesses, …)
    artifacts: list[str] = Field(default_factory=list)

    # Wall-clock execution time of the tool call
    duration_ms: int

    # UUID shared by all tool calls in one pipeline run (set by the caller
    # or auto-generated).  P4's logging middleware uses this to correlate
    # every row in tool_calls back to a single run.
    run_id: str = Field(default_factory=_new_run_id)
