"""
start_run / finish_run — P4 run-lifecycle tools.

Why these exist:
    Until now a `runs` row was only ever created as a side effect, by
    db.ensure_run() the first time some other tool logged a call.  A row
    born that way has no targets, no iteration cap, and — the part that
    actually misleads — no ending.  It sits at status='running' with
    finished_at NULL forever, because nothing in the pipeline was ever
    responsible for closing it.  The dashboard then reports a run that
    ended weeks ago as still in progress, which is the one thing a status
    field must never do.

    So the lifecycle becomes explicit and Bob owns both ends of it:
    start_run at stage 0, finish_run at stage 13.  ensure_run stays as the
    safety net for ad-hoc calls; it is no longer the normal path.

Target units — the one sharp edge here:
    The skill talks in fractions (coverage_target 0.85, mutation_target
    0.80).  The database stores coverage_target as a PERCENT (85.0) and
    mutation_target as a FRACTION (0.80), because that is what the
    coverage and mutation tools respectively report.  Rather than make Bob
    remember that, start_run accepts either and normalises: a coverage
    target ≤ 1 is read as a fraction, a mutation target > 1 as a percent.

Gate policy (AGENTS.md §3): proceed is ALWAYS True for both.  Bookkeeping
cannot invalidate a suite, and a database problem must never stop a run —
both tools report the failure in `details` and let the pipeline continue.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from pathlib import Path

from server.data import db, usage
from server.data.models import UsageRow
from server.data.replay import replay_enabled
from server.schema import ToolResult

# server/data/runs.py → server/data → server → repo root.  This is the
# workspace Bob has open, and the key the usage ledger is matched on.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Statuses the dashboard knows how to present.  Anything else is stored
# verbatim — the tool records what it was told rather than second-guessing
# a caller who knows something we don't.
KNOWN_STATUSES = ("complete", "failed", "aborted")


def _coverage_percent(value: float | None) -> float | None:
    """0.85 → 85.0; 85 → 85.0; None → None."""
    if value is None:
        return None
    value = float(value)
    return value * 100 if 0 < value <= 1 else value


def _mutation_fraction(value: float | None) -> float | None:
    """0.80 → 0.80; 80 → 0.80; None → None."""
    if value is None:
        return None
    value = float(value)
    return value / 100 if value > 1 else value


def start_run(
    run_id: str = "",
    source_file: str = "",
    coverage_target: float | None = None,
    mutation_target: float | None = None,
    max_iterations: int | None = None,
) -> ToolResult:
    """
    Open a run and record its targets.  Returns the run_id to use from here on.

    Calling it twice with the same run_id is safe: the row is updated, not
    duplicated, so a resumed or re-entered pipeline keeps one identity.
    """
    started = time.time()
    run_id = (run_id or "").strip() or str(uuid.uuid4())

    coverage_pct = _coverage_percent(coverage_target)
    mutation_frac = _mutation_fraction(mutation_target)
    replay = replay_enabled()

    stored = db.start_run(
        run_id,
        source_file=source_file,
        coverage_target=coverage_pct,
        mutation_target=mutation_frac,
        max_iterations=max_iterations,
        replay_mode=replay,
    )

    # Baseline reading of Bob's ledger, so finish_run can report what THIS
    # run cost rather than what the whole Bob task has cost so far.  Best
    # effort in every sense: no ledger, no baseline, no problem.
    baseline = usage.snapshot(str(REPO_ROOT))
    if baseline:
        db.insert_usage(UsageRow(run_id=run_id, attribution="baseline",
                                 **{k: v for k, v in baseline.items()
                                    if k in UsageRow.__dataclass_fields__}))

    targets = []
    if coverage_pct is not None:
        targets.append(f"coverage ≥ {coverage_pct:.0f}%")
    if mutation_frac is not None:
        targets.append(f"mutation ≥ {mutation_frac * 100:.0f}%")
    if max_iterations is not None:
        targets.append(f"≤ {max_iterations} iterations")

    verdict = f"Run {run_id} opened"
    if targets:
        verdict += " — " + ", ".join(targets)
    if replay:
        verdict += " (replay mode)"
    if not stored:
        verdict += " — WARNING: the run row could not be written; "\
                   "metrics will still be logged under this run_id"

    return ToolResult(
        tool="start_run",
        ok=stored,
        score=1.0 if stored else 0.0,
        proceed=True,
        verdict=verdict,
        details={
            "run_id": run_id,
            "stored": stored,
            "source_file": source_file,
            "coverage_target": coverage_pct,
            "mutation_target": mutation_frac,
            "max_iterations": max_iterations,
            "replay_mode": replay,
            "db_path": str(db.db_path()),
            "usage_baseline": bool(baseline),
        },
        artifacts=[],
        duration_ms=int((time.time() - started) * 1000),
        run_id=run_id,
    )


def finish_run(
    run_id: str = "",
    status: str = "complete",
    iterations_used: int | None = None,
    notes: str = "",
) -> ToolResult:
    """
    Close a run: stamp finished_at, the final status, and iterations used.

    Call it on EVERY exit path, including the unhappy ones — a run that
    stopped early and says so is honest; a run left open claims to still be
    working.  `status` should be complete / failed / aborted.
    """
    started = time.time()
    run_id = (run_id or "").strip()
    status = (status or "complete").strip() or "complete"

    if not run_id:
        return ToolResult(
            tool="finish_run", ok=False, score=0.0, proceed=True,
            verdict="finish_run needs the run_id that start_run returned.",
            details={"closed": False, "error": "missing run_id"},
            artifacts=[], duration_ms=int((time.time() - started) * 1000),
            run_id="",
        )

    # A run that was never opened (an ad-hoc sequence of tool calls) still
    # deserves to be closed, so backfill the row rather than refusing.
    existed = db.get_run(run_id) is not None
    if not existed:
        db.ensure_run(run_id)

    closed = db.finish_run(run_id, status=status,
                           iterations_used=iterations_used, notes=notes)
    row: dict[str, Any] | None = db.get_run(run_id)

    duration_ms = None
    if row and row.get("finished_at") and row.get("started_at"):
        duration_ms = int(row["finished_at"] - row["started_at"])

    spend = _record_usage(run_id)

    verdict = f"Run {run_id} closed as '{status}'"
    if duration_ms is not None:
        verdict += f" after {duration_ms / 1000:.1f}s"
    if iterations_used is not None:
        verdict += f", {iterations_used} iteration(s) used"
    if not existed:
        verdict += " (the run row was backfilled — start_run was never called)"
    if not closed:
        verdict += " — WARNING: the database write failed; the run stays open"
    if spend:
        verdict += (f" — spent {spend['cost']:.4f} Bobcoin, "
                    f"{spend['total_tokens']:,} tokens")
        if spend["attribution"] == "task-totals":
            verdict += " (whole-task upper bound: no baseline reading)"
    if status not in KNOWN_STATUSES:
        verdict += (f" — note: '{status}' is not one of "
                    f"{'/'.join(KNOWN_STATUSES)}, and is stored verbatim")

    return ToolResult(
        tool="finish_run",
        ok=closed,
        score=1.0 if closed else 0.0,
        proceed=True,
        verdict=verdict,
        details={
            "closed": closed,
            "run_existed": existed,
            "status": status,
            "iterations_used": iterations_used,
            "duration_ms": duration_ms,
            "finished_at": row.get("finished_at") if row else None,
            "notes": notes,
            "usage": spend,
        },
        artifacts=[],
        duration_ms=int((time.time() - started) * 1000),
        run_id=run_id,
    )


def _record_usage(run_id: str) -> dict[str, Any] | None:
    """
    Take a closing reading of Bob's ledger and store this run's share.

    Returns the stored figures, or None when the ledger could not be read —
    in which case the run simply has no cost recorded, which the dashboard
    shows as "not recorded" rather than as zero.
    """
    final = usage.snapshot(str(REPO_ROOT))
    if not final:
        return None

    spend = usage.subtract(db.usage_baseline(run_id), final)
    db.insert_usage(UsageRow(run_id=run_id,
                             **{k: v for k, v in spend.items()
                                if k in UsageRow.__dataclass_fields__}))
    return spend
