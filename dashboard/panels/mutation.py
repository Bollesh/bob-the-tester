"""
Mutation panel — the project's headline metric and its demo moment.

Coverage says the tests EXECUTED the code; the mutation score says they
would NOTICE if it were wrong.  When a run has two or more mutation_test
rows, the delta between the first and the last IS the MuTAP story: score
before survivor-targeted regeneration, and after.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.panels import _render


def render(rows: list[dict[str, Any]], run: dict[str, Any]) -> None:
    st.subheader("Mutation score — test strength")

    if not rows:
        st.info("No mutation run recorded yet.")
        return

    if not rows[-1].get("engine_available", 1):
        st.warning(
            "mutmut is not installed, so this stage degraded cleanly rather "
            'than failing. Install it with: pip install -e ".[pipeline]"'
        )
        return

    first, last = rows[0], rows[-1]
    score = float(last["mutation_score"])
    target = run.get("mutation_target")

    left, middle, right = st.columns(3)
    delta = None
    if len(rows) > 1:
        delta = f"{(score - float(first['mutation_score'])) * 100:+.1f} pts"
    left.metric("Mutation score", f"{score * 100:.1f}%", delta=delta)
    middle.metric("Killed", f"{last['killed']} / {last['total']}")
    right.metric("Target", f"{target * 100:.0f}%" if target else "—")

    st.progress(min(max(score, 0.0), 1.0))

    if len(rows) > 1:
        st.caption(
            f"MuTAP loop: {float(first['mutation_score']) * 100:.1f}% → "
            f"{score * 100:.1f}% after survivor-targeted regeneration "
            f"({len(rows)} mutation runs)."
        )
    else:
        st.caption(
            "One mutation run so far. A second run after killing survivors is "
            "what makes the improvement visible."
        )

    survivors = last.get("survivors") or []
    if not survivors:
        st.success("No surviving mutants — every mutation was caught.")
        return

    # `status` separates the two fixes: a weak assertion versus no test at
    # all.  Collapsing them would hide which action Bob needs to take.
    st.markdown(
        f"**{last['survived_count']} surviving mutant(s)** "
        f"— {last['not_covered']} on lines no test executed."
    )
    _render.table(
        survivors,
        ["id", "line", "function", "status", "original", "mutated"],
    )
