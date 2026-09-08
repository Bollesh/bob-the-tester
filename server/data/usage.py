"""
usage.py — what a run cost, read out of Bob's own ledger.

The honesty problem this solves:
    The MCP server is deterministic and never talks to a model, so it
    cannot observe Bob's token spend.  Asking Bob to *tell* us the number
    would be worse than not having one — a model reporting its own token
    count is guessing, and a guessed Bobcoin figure sitting beside measured
    coverage and mutation numbers would poison the whole dashboard.

    So nothing here asks Bob anything.  Bob Shell keeps a running ledger
    for each task in its own SQLite database (~/.bob/db/bob.db): the
    `tasks.costs` JSON carries input, output, cacheRead, cacheWrite,
    contextTokens and cost, and it is updated while the task runs.  This
    module reads that database READ-ONLY.

Why snapshot-and-subtract rather than per-message sums:
    Each message row does carry `_meta.spend`, which looks like the more
    precise source — but Bob persists the whole message list in one write
    at the end of a turn, so every row in a task ends up with essentially
    the same `created_at`.  Filtering messages by a run's time window
    therefore matches all of them or none.  Task totals, in contrast, move
    monotonically during the task, so the difference between a reading at
    start_run and one at finish_run is exactly what the run spent — even
    when one Bob task drives several pipeline runs.

Attribution — recorded beside every number, never assumed:
    'task-delta'   totals at finish_run minus totals at start_run: this
                   run's own spend.
    'task-totals'  no baseline was available (the server restarted, or the
                   run was never opened with start_run), so the task's
                   running totals were used.  A task can span more than one
                   run, so this is an UPPER BOUND.  The dashboard says so.
    'baseline'     the reading taken at start_run.  Kept as its own row so
                   the subtraction is auditable rather than hidden.

Environment:
    BOB_DB   override the path to Bob's database (tests, CI, a custom
             Bob installation)

Failure policy: every path returns None rather than raising.  Cost is
observability; a missing or busy ledger must never fail a run.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("bob-the-tester.usage")

DEFAULT_BOB_DB = Path.home() / ".bob" / "db" / "bob.db"

# tasks.costs keys → our column names.  contextTokens is handled apart: it
# is a snapshot of how full the context window was, not a running total, so
# it is carried through rather than differenced.
_COST_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cacheRead": "cache_read_tokens",
    "cacheWrite": "cache_write_tokens",
}

_COUNTERS = tuple(_COST_KEYS.values())


def bob_db_path() -> Path:
    override = os.environ.get("BOB_DB", "").strip()
    return Path(override).expanduser() if override else DEFAULT_BOB_DB


def snapshot(workspace: str) -> dict[str, Any] | None:
    """
    Read the current task's running totals for `workspace`.

    "Current" is the most recently updated task registered against that
    project directory — during a run, that is the task driving it.  Returns
    None whenever the ledger is missing, busy, or shaped unexpectedly.
    """
    path = bob_db_path()
    if not path.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        logger.debug("Bob ledger not readable at %s: %s", path, exc)
        return None

    try:
        row = conn.execute(
            "SELECT id, costs, updated_at FROM tasks WHERE project_id LIKE ? "
            "ORDER BY updated_at DESC LIMIT 1", (f"%{workspace}",),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("Bob ledger has no readable tasks table: %s", exc)
        return None
    finally:
        conn.close()

    if row is None:
        return None

    try:
        costs = json.loads(row["costs"] or "{}")
    except Exception:  # noqa: BLE001
        costs = {}
    if not isinstance(costs, dict) or not costs:
        return None

    reading: dict[str, Any] = {
        column: int(costs.get(key, 0) or 0) for key, column in _COST_KEYS.items()
    }
    reading.update(
        task_id=str(row["id"]),
        cost=float(costs.get("cost", 0.0) or 0.0),
        context_tokens=int(costs.get("contextTokens", 0) or 0),
        reasoning_tokens=0,
        message_count=0,
        source=str(path),
    )
    reading["total_tokens"] = reading["input_tokens"] + reading["output_tokens"]
    return reading


def subtract(baseline: dict[str, Any] | None,
             final: dict[str, Any]) -> dict[str, Any]:
    """
    final − baseline when they describe the same task, else `final` as-is.

    A different task_id means Bob started a new task mid-run (or the
    baseline is from an earlier session): the subtraction would then be
    meaningless, so the totals are reported as the upper bound they are.
    """
    if not baseline or baseline.get("task_id") != final.get("task_id"):
        return {**final, "attribution": "task-totals"}

    result = dict(final)
    for column in _COUNTERS:
        # A ledger only ever grows; clamping at zero keeps a mid-run reset
        # from producing a negative "cost".
        result[column] = max(0, final.get(column, 0) - baseline.get(column, 0))
    result["cost"] = max(0.0, float(final.get("cost", 0.0))
                         - float(baseline.get("cost", 0.0)))
    result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    result["attribution"] = "task-delta"
    return result
