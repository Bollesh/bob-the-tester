"""
Activity panel — the full tool-call log, iteration and cost counters.

Note on "Bobcoin": the MCP server is deterministic and never talks to a
model, so it cannot observe Bob's token spend — Bobalytics is the authority
there (P3). What this panel reports instead is what the server CAN measure
honestly: how many tool calls a run took, how long they took, and how many
were served from the replay snapshot rather than executed.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.panels import _render


def render(calls: list[dict[str, Any]], run: dict[str, Any]) -> None:
    st.subheader("Run activity")

    if not calls:
        st.info("No tool calls logged for this run yet.")
        return

    total_ms = sum(c["duration_ms"] for c in calls)
    cached = sum(1 for c in calls if c["replayed"])
    failed = sum(1 for c in calls if not c["ok"])

    a, b, c_, d = st.columns(4)
    a.metric("Tool calls", len(calls))
    b.metric("Tool time", f"{total_ms / 1000:.1f}s")
    c_.metric(
        "Iterations",
        f"{run.get('iterations_used', 0)}/{run.get('max_iterations') or '—'}",
    )
    d.metric("Served from cache", f"{cached}/{len(calls)}")

    if failed:
        st.error(
            f"{failed} tool call(s) returned ok=false — the tool itself failed "
            "(misconfiguration or a crash), which is not the same as the tests "
            "being bad."
        )

    _render.table([{
        "#": i + 1,
        "tool": c["tool"],
        "ok": bool(c["ok"]),
        "proceed": bool(c["proceed"]),
        "score": c["score"],
        "ms": c["duration_ms"],
        "cached": bool(c["replayed"]),
        "verdict": c["verdict"],
    } for i, c in enumerate(calls)])

    with st.expander("Raw payloads"):
        index = st.number_input(
            "Call #", min_value=1, max_value=len(calls), value=len(calls), step=1
        )
        call = calls[int(index) - 1]
        st.markdown(f"**{call['tool']}** — {call['verdict']}")
        st.json({"arguments": call["arguments"], "details": call["details"]})
