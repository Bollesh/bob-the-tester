"""
Pipeline stage panel — traffic lights and scores for every stage.

Reads only `tool_calls`, so it works for any tool the team adds later: a
stage appears here the moment it is called once.  Stage order follows the
pipeline in AGENTS.md §4; anything unrecognised is appended rather than
dropped, so a new tool is never silently invisible.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.panels import _render

# Pipeline order (AGENTS.md §4).  Display order only — never a gate.
STAGE_ORDER = [
    "run_tests",
    "get_coverage",
    "list_uncovered",
    "validate_and_keep",
    "detect_smells",
    "run_flaky_check",
    "run_property_tests",
    "run_fuzz",
    "mutation_test",
    "explain_gaps",
    "store_explanation",
    "save_test_record",
]

STAGE_LABEL = {
    "run_tests": "0 · Run tests",
    "get_coverage": "0 · Coverage",
    "list_uncovered": "12 · Gap map",
    "validate_and_keep": "3 · Validate & keep",
    "detect_smells": "4 · Smells",
    "run_flaky_check": "6 · Flakiness",
    "run_property_tests": "7 · Properties",
    "run_fuzz": "8 · Fuzz",
    "mutation_test": "10 · Mutation",
    "explain_gaps": "13 · Gap data",
    "store_explanation": "13 · Gap narration",
    "save_test_record": "9 · Test records",
}


def _light(calls: list[dict[str, Any]]) -> str:
    """
    Traffic light for a stage, from its calls.

    Deliberately distinguishes the two ways a stage can be unhappy, because
    the contract does (AGENTS.md §3):
      🔴 the tool itself failed (ok=false) — misconfiguration, not test quality
      🟡 the tool ran but halted the pipeline (proceed=false) — a real gate
      🟢 ran and cleared
    """
    if any(not c["ok"] for c in calls):
        return "🔴"
    if any(not c["proceed"] for c in calls):
        return "🟡"
    return "🟢"


def render(calls: list[dict[str, Any]]) -> None:
    st.subheader("Pipeline stages")

    if not calls:
        st.info("No tool calls logged for this run yet.")
        return

    by_tool: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        by_tool.setdefault(call["tool"], []).append(call)

    ordered = [t for t in STAGE_ORDER if t in by_tool]
    ordered += [t for t in by_tool if t not in STAGE_ORDER]

    rows = []
    for tool in ordered:
        tool_calls = by_tool[tool]
        scores = [c["score"] for c in tool_calls]
        rows.append({
            "": _light(tool_calls),
            "Stage": STAGE_LABEL.get(tool, tool),
            "Calls": len(tool_calls),
            "Avg score": round(sum(scores) / len(scores), 3),
            "Time (s)": round(sum(c["duration_ms"] for c in tool_calls) / 1000, 1),
            "Cached": sum(1 for c in tool_calls if c["replayed"]),
            "Last verdict": tool_calls[-1]["verdict"],
        })

    _render.table(rows)

    blocked = [c for c in calls if not c["proceed"]]
    if blocked:
        st.warning(
            f"{len(blocked)} call(s) returned proceed=false — the pipeline was "
            "gated here. The skill must stop advancing and act on the verdict."
        )
        for call in blocked[-5:]:
            st.caption(f"**{call['tool']}** — {call['verdict']}")
