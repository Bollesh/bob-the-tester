"""
Bob the Tester dashboard — P4 (Streamlit entry point).

Run it:
    streamlit run dashboard/app.py

Reads bob-the-tester.db READ-ONLY (AGENTS.md §6).  It never imports the
tool layer and never writes: a dashboard refresh cannot disturb a pipeline
run that is still in progress.

Every panel gets its rows from this module — one read per page render, so
the whole page shows a single consistent snapshot rather than panels that
each queried at a slightly different moment.
"""
from __future__ import annotations

import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import streamlit as st

# dashboard/app.py → dashboard → repo root.  Makes `server.data` importable
# when Streamlit is launched from somewhere other than the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.panels import (
    _render, activity, bugs, coverage, gaps, mutation, pipeline, tests,
)
from server.data import db
from server.data.replay import replay_enabled, snapshot_summary

st.set_page_config(
    page_title="Bob the Tester",
    page_icon="🧪",
    layout="wide",
)


def _fmt_time(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _run_label(run: dict) -> str:
    source = run.get("source_file") or "(no source recorded)"
    status = run.get("status", "?")
    return f"{_fmt_time(run['started_at'])} · {Path(source).name} · {status}"


# ─────────────────────────────────────────────────────────────────────────────
# Load
# ─────────────────────────────────────────────────────────────────────────────

st.title("🧪 Bob the Tester")
st.caption("AI writes your tests; Bob the Tester proves they'd catch real bugs.")

try:
    conn = db.connect_ro()
except FileNotFoundError:
    st.warning(
        f"No database yet at `{db.db_path()}`.\n\n"
        "Run the pipeline first — through Bob, or with `python scripts/smoke.py` "
        "— then refresh this page."
    )
    st.stop()

with closing(conn):
    runs = db.list_runs(limit=100, conn=conn)

    if not runs:
        st.info("The database exists but has no runs in it yet.")
        st.stop()

    with st.sidebar:
        st.header("Run")
        selected = st.selectbox(
            "Select a run", runs, format_func=_run_label, label_visibility="collapsed"
        )
        st.caption(f"`{selected['run_id']}`")
        st.divider()

        st.header("Replay")
        if replay_enabled():
            st.success("BOB_THE_TESTER_REPLAY=1 — tools serve cached results.")
        else:
            st.caption("Live mode. Set BOB_THE_TESTER_REPLAY=1 to replay.")
        snapshot = snapshot_summary()
        st.caption(
            f"Snapshot: {snapshot['entries']} cached result(s) "
            f"across {len(snapshot['tools'])} tool(s)."
        )
        _render.arrow_notice()

    run_id = selected["run_id"]
    calls = db.tool_calls_for_run(run_id, conn=conn)
    coverage_points = db.coverage_for_run(run_id, conn=conn)
    test_rows = db.tests_for_run(run_id, conn=conn)
    mutation_rows = db.mutation_for_run(run_id, conn=conn)
    bug_rows = db.bugs_for_run(run_id, conn=conn)
    gap_rows = db.gaps_for_run(run_id, conn=conn)

# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

header = st.container()
with header:
    a, b, c, d = st.columns(4)
    a.metric("Status", selected.get("status", "?"))
    b.metric("Started", _fmt_time(selected["started_at"]))
    c.metric("Finished", _fmt_time(selected.get("finished_at")))
    d.metric("Source", Path(selected.get("source_file") or "—").name)

    if selected.get("replay_mode"):
        st.info("This run was recorded in replay mode.")

st.divider()

left, right = st.columns(2)
with left:
    coverage.render(coverage_points, selected)
with right:
    mutation.render(mutation_rows, selected)

st.divider()
pipeline.render(calls)

st.divider()
bugs.render(bug_rows, calls)

st.divider()
tests.render(test_rows, calls)

st.divider()
gaps.render(gap_rows, calls)

st.divider()
activity.render(calls, selected)
