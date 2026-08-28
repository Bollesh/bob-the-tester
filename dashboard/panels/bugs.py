"""
Defects panel — the payoff panel.

A generated test that fails because the CODE is wrong is a bug found, not a
test to discard (AGENTS.md §4, classification (a)).  This panel is where
that distinction becomes visible, alongside the machine-found evidence that
produced it: Hypothesis counterexamples and fuzz crashes.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.panels import _render


def render(bugs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
    st.subheader("Defects found")

    if bugs:
        st.error(f"{len(bugs)} genuine defect(s) reported by the suite.")
        for bug in bugs:
            title = f"{bug['test_name'] or 'test'} — {bug['source_file'] or 'source'}"
            with st.expander(title, expanded=len(bugs) == 1):
                st.markdown(f"**Stage:** {bug['stage'] or '—'}")
                st.markdown(f"**What went wrong:** {bug['description'] or '—'}")
                if bug["evidence"]:
                    st.code(bug["evidence"])
    else:
        st.info(
            "No defects recorded yet. Bob reports one by calling "
            "save_test_record with bug_found=true — a failing test whose "
            "assertion is right and whose code is wrong."
        )

    _render_counterexamples(calls)
    _render_crashes(calls)


def _render_counterexamples(calls: list[dict[str, Any]]) -> None:
    property_calls = [c for c in calls if c["tool"] == "run_property_tests"]
    if not property_calls:
        return

    failures: list[dict[str, Any]] = []
    for call in property_calls:
        failures.extend((call.get("details") or {}).get("failures", []) or [])

    st.markdown("#### Hypothesis counterexamples")
    if not failures:
        st.success("Every property held for all generated examples.")
        return

    st.warning(
        f"{len(failures)} shrunk counterexample(s) — each becomes a named "
        "regression test."
    )
    _render.table(failures, ["property", "shrunk_input", "exception"])


def _render_crashes(calls: list[dict[str, Any]]) -> None:
    fuzz_calls = [c for c in calls if c["tool"] == "run_fuzz"]
    if not fuzz_calls:
        return

    st.markdown("#### Fuzz crashes")
    latest = fuzz_calls[-1].get("details") or {}
    if not latest.get("engine_available", True):
        st.caption(
            "Atheris is not installed — fuzzing was the pre-agreed first cut. "
            "Hypothesis still supplies machine-found counterexamples."
        )
        return

    crashes: list[dict[str, Any]] = []
    for call in fuzz_calls:
        crashes.extend((call.get("details") or {}).get("crashes", []) or [])

    if not crashes:
        st.success(
            f"No crashes at {latest.get('execs_per_sec', 0)} execs/sec "
            f"over {latest.get('seconds', 0)}s."
        )
        return

    st.warning(f"{len(crashes)} crashing input(s) found.")
    _render.table(crashes, ["input_repr", "exception", "stack_top"])
