"""
Coverage-gap panel — Bob's prose beside the code it explains.

explain_gaps supplies the structure (Layer 2); Bob writes the narration
(Layer 1); store_explanation persists it.  This panel is the last step of
that chain: it shows why the uncovered lines are still uncovered, in
language a judge can read without opening the source.
"""
from __future__ import annotations

from typing import Any

import streamlit as st


def render(explanations: list[dict[str, Any]],
           calls: list[dict[str, Any]]) -> None:
    st.subheader("Coverage gaps explained")

    if explanations:
        for entry in explanations:
            if entry.get("source_file"):
                st.caption(entry["source_file"])
            st.markdown(entry["text"])
            st.divider()
    else:
        st.info(
            "No explanation stored yet. Bob writes one at stage 13 and saves "
            "it with store_explanation."
        )

    _render_structure(calls)


def _render_structure(calls: list[dict[str, Any]]) -> None:
    """Raw gap structures from explain_gaps, for anyone who wants the detail."""
    gap_calls = [c for c in calls if c["tool"] == "explain_gaps" and c["ok"]]
    if not gap_calls:
        return

    details = gap_calls[-1].get("details") or {}
    gaps = details.get("gaps", []) or []
    if not gaps:
        st.success("No uncovered regions remain in the analysed file.")
        return

    with st.expander(f"Uncovered regions ({len(gaps)})"):
        for gap in gaps:
            st.markdown(
                f"**{gap['function']}** — lines {gap['start']}–{gap['end']} "
                f"({gap['line_count']} lines)"
            )
            if gap.get("docstring"):
                st.caption(gap["docstring"])
            st.code(gap.get("snippet", ""), language="python")

            # Signals are facts, not verdicts: the tool reports them and Bob
            # decides what category the gap belongs to.
            signals = {k: v for k, v in (gap.get("signals") or {}).items() if v}
            if signals:
                st.caption("Signals: " + ", ".join(f"{k}={v}" for k, v in signals.items()))
            st.divider()
