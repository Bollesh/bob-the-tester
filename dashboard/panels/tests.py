"""
Kept/discarded panel — every candidate test and why it survived or didn't.

This is the Assured-LLMSE filter made visible: a generated test is kept only
if it builds, passes, and strictly increases coverage.  Showing the discards
WITH their reasons is the point — a pipeline that silently drops candidates
is indistinguishable from one that never generated them.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from dashboard.panels import _render


def render(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
    st.subheader("Generated tests — kept vs. discarded")

    if not rows:
        st.info("No candidate tests recorded for this run yet.")
    else:
        kept = [r for r in rows if r["kept"]]
        discarded = [r for r in rows if not r["kept"]]
        bugs = [r for r in rows if r["bug_found"]]

        left, middle, right = st.columns(3)
        left.metric("Kept", len(kept))
        middle.metric("Discarded", len(discarded))
        right.metric("Defects found", len(bugs))

        display = [{
            "test_name": r["test_name"],
            "stage": r["stage"],
            "outcome": "kept" if r["kept"] else "discarded",
            "coverage": (
                f"{r['coverage_before']:.1f}% → {r['coverage_after']:.1f}%"
                if r["coverage_before"] is not None
                and r["coverage_after"] is not None else "—"
            ),
            "rolled back": bool(r["rollback_performed"]),
            "reason": r["reason"],
        } for r in rows]
        _render.table(display)

    _render_flaky(calls)
    _render_smells(calls)


def _render_flaky(calls: list[dict[str, Any]]) -> None:
    """Flakiness is a hard gate — a test that varies is worthless as a signal."""
    flaky_calls = [c for c in calls if c["tool"] == "run_flaky_check"]
    if not flaky_calls:
        return

    st.markdown("#### Flakiness check")
    flaky: list[dict[str, Any]] = []
    consistently_failing: list[str] = []
    runs = 0
    for call in flaky_calls:
        details = call.get("details") or {}
        runs = max(runs, int(details.get("runs", 0) or 0))
        flaky.extend(details.get("flaky", []) or [])
        consistently_failing.extend(details.get("consistently_failing", []) or [])

    if not flaky:
        st.success(f"No flaky tests across {runs} runs of the suite.")
    else:
        st.error(f"{len(flaky)} flaky test(s) — hard-discarded.")
        _render.table(flaky)

    if consistently_failing:
        # Reported apart from flakiness on purpose: these fail every time, so
        # they are deterministically broken, not unstable.
        st.caption(
            f"{len(consistently_failing)} test(s) failed in every run — "
            "deterministic failures, not flakiness: "
            + ", ".join(str(t) for t in consistently_failing[:8])
        )


def _render_smells(calls: list[dict[str, Any]]) -> None:
    smell_calls = [c for c in calls if c["tool"] == "detect_smells"]
    if not smell_calls:
        return

    st.markdown("#### Test smells")
    findings: list[dict[str, Any]] = []
    for call in smell_calls:
        findings.extend((call.get("details") or {}).get("findings", []) or [])

    latest = smell_calls[-1].get("details") or {}
    st.caption(
        f"Latest scan: {latest.get('tests_scanned', 0)} tests, "
        f"smell score {latest.get('smell_score', 0)}. "
        "Smells lower the score but never gate the pipeline."
    )
    if findings:
        _render.table(findings)
    else:
        st.success("No smells detected in the latest scan.")
