"""
db.py — P4 connection management, writers, and dashboard readers.

Single source of truth for where the database lives and how rows get into
it.  Nothing else in the server opens a SQLite connection.

Failure policy (the rule that matters during a live demo):
    Persistence is observability, not correctness.  A tool call must NEVER
    fail because the database is locked, missing, or read-only.  Every
    writer here swallows its exception, logs a warning to stderr, and
    returns False.  The caller in logging_mw.py treats that as "best
    effort done" and returns the ToolResult untouched.

Reader functions are the dashboard's entire API.  They open the database
READ-ONLY (SQLite `mode=ro` URI) so a dashboard refresh can never corrupt
or lock a run that is still writing — AGENTS.md §6, "reads DB read-only".

Environment:
    BOB_THE_TESTER_DB   override the database path (tests, CI, demos)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from server.data.models import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    BugFound,
    CoveragePoint,
    GapExplanation,
    MutationRow,
    TestRecord,
    ToolCall,
    UsageRow,
)

logger = logging.getLogger("bob-the-tester.db")

# server/data/db.py → server/data → server → repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_NAME = "bob-the-tester.db"


def now_ms() -> int:
    return int(time.time() * 1000)


def db_path() -> Path:
    """Resolve the database path (env override wins)."""
    override = os.environ.get("BOB_THE_TESTER_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return REPO_ROOT / DEFAULT_DB_NAME


# ─────────────────────────────────────────────────────────────────────────────
# Connections
# ─────────────────────────────────────────────────────────────────────────────

def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """
    Open a read-write connection with the schema applied.

    WAL journalling lets the dashboard read while a pipeline run
    is still writing — without it, a dashboard refresh mid-run would block
    the server on a locked database.
    """
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA_SQL)
    _stamp_version(conn)
    conn.commit()
    return conn


def connect_ro(path: str | Path | None = None) -> sqlite3.Connection:
    """
    Open a READ-ONLY connection for the dashboard.

    Raises FileNotFoundError when the database does not exist yet, so the
    dashboard can show "no runs yet" instead of silently creating an empty
    file that then looks like a corrupted run.
    """
    target = Path(path) if path is not None else db_path()
    if not target.exists():
        raise FileNotFoundError(f"No database at {target}. Run the pipeline first.")
    uri = f"file:{target.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _stamp_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif row["value"] != str(SCHEMA_VERSION):
        # Not fatal: a stale demo database still renders.  Loud, though —
        # models.py is a frozen contract and a mismatch means a rebuild.
        logger.warning(
            "Database schema version %s != code version %s. "
            "Delete %s (or run scripts/reset_demo.py) to rebuild.",
            row["value"], SCHEMA_VERSION, db_path(),
        )


@contextmanager
def _write() -> Iterator[sqlite3.Connection]:
    """
    Open → commit → close, once per write.

    sqlite3's own `with conn:` block commits but does NOT close, so using it
    directly would leak a handle (and a WAL reader) per tool call across a
    long pipeline run.  Every writer below goes through this instead.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _j(value: Any) -> str:
    """JSON-encode a payload, never raising on an exotic object."""
    try:
        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001
        return json.dumps({"unserialisable": str(type(value))})


def _loads(text: str, fallback: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Writers
#
# Every writer returns True on success, False on a swallowed error.
# ─────────────────────────────────────────────────────────────────────────────

def ensure_run(run_id: str, source_file: str = "", replay_mode: bool = False) -> bool:
    """
    Make sure a `runs` row exists for this run_id.

    Tools may be called ad hoc (smoke script, a single Bob tool call) with a
    run_id nobody opened.  Rather than lose those rows to a foreign-key
    error, we backfill a run on first sight.
    """
    if not run_id:
        return False
    try:
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, started_at, source_file, replay_mode)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    source_file = CASE
                        WHEN runs.source_file = '' THEN excluded.source_file
                        ELSE runs.source_file END
                """,
                (run_id, now_ms(), source_file, int(replay_mode)),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_run failed: %s", exc)
        return False


def start_run(
    run_id: str,
    source_file: str = "",
    coverage_target: float | None = None,
    mutation_target: float | None = None,
    max_iterations: int | None = None,
    replay_mode: bool = False,
) -> bool:
    """Open (or re-open) a run row with its targets recorded."""
    try:
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, started_at, source_file, coverage_target,
                                  mutation_target, max_iterations, status, replay_mode)
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    source_file     = excluded.source_file,
                    coverage_target = excluded.coverage_target,
                    mutation_target = excluded.mutation_target,
                    max_iterations  = excluded.max_iterations,
                    replay_mode     = excluded.replay_mode
                """,
                (run_id, now_ms(), source_file, coverage_target,
                 mutation_target, max_iterations, int(replay_mode)),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("start_run failed: %s", exc)
        return False


def finish_run(run_id: str, status: str = "complete",
               iterations_used: int | None = None, notes: str = "") -> bool:
    try:
        with _write() as conn:
            if iterations_used is None:
                conn.execute(
                    "UPDATE runs SET finished_at=?, status=?, notes=? WHERE run_id=?",
                    (now_ms(), status, notes, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET finished_at=?, status=?, iterations_used=?, "
                    "notes=? WHERE run_id=?",
                    (now_ms(), status, iterations_used, notes, run_id),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("finish_run failed: %s", exc)
        return False


def insert_tool_call(call: ToolCall) -> bool:
    try:
        ensure_run(call.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls
                    (run_id, tool, ok, proceed, score, verdict, arguments,
                     details, artifacts, duration_ms, replayed, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (call.run_id, call.tool, int(call.ok), int(call.proceed),
                 float(call.score), call.verdict, _j(call.arguments),
                 _j(call.details), _j(call.artifacts), int(call.duration_ms),
                 int(call.replayed), call.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_tool_call failed: %s", exc)
        return False


def insert_test(rec: TestRecord) -> bool:
    try:
        ensure_run(rec.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO tests
                    (run_id, test_file, test_name, stage, kept, reason,
                     coverage_before, coverage_after, rollback_performed,
                     bug_found, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rec.run_id, rec.test_file, rec.test_name, rec.stage,
                 int(rec.kept), rec.reason, rec.coverage_before, rec.coverage_after,
                 int(rec.rollback_performed), int(rec.bug_found),
                 rec.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_test failed: %s", exc)
        return False


def insert_coverage(point: CoveragePoint) -> bool:
    try:
        ensure_run(point.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO coverage_history
                    (run_id, iteration, source_file, percent,
                     covered_count, uncovered_count, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (point.run_id, point.iteration, point.source_file,
                 float(point.percent), point.covered_count, point.uncovered_count,
                 point.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_coverage failed: %s", exc)
        return False


def insert_mutation(row: MutationRow) -> bool:
    try:
        ensure_run(row.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO mutation_results
                    (run_id, target_module, mutation_score, killed, survived_count,
                     not_covered, total, inconclusive, survivors, engine_available,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row.run_id, row.target_module, float(row.mutation_score),
                 row.killed, row.survived_count, row.not_covered, row.total,
                 row.inconclusive, _j(row.survivors), int(row.engine_available),
                 row.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_mutation failed: %s", exc)
        return False


def insert_bug(bug: BugFound) -> bool:
    try:
        ensure_run(bug.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO bugs_found
                    (run_id, source_file, test_name, stage, description,
                     evidence, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (bug.run_id, bug.source_file, bug.test_name, bug.stage,
                 bug.description, bug.evidence, bug.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_bug failed: %s", exc)
        return False


def insert_gap_explanation(gap: GapExplanation) -> bool:
    try:
        ensure_run(gap.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO gap_explanations
                    (run_id, source_file, text, gaps, created_at)
                VALUES (?,?,?,?,?)
                """,
                (gap.run_id, gap.source_file, gap.text, _j(gap.gaps),
                 gap.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_gap_explanation failed: %s", exc)
        return False


def insert_usage(row: UsageRow) -> bool:
    """Record what a run cost.  See server/data/usage.py for provenance."""
    try:
        ensure_run(row.run_id)
        with _write() as conn:
            conn.execute(
                """
                INSERT INTO usage
                    (run_id, task_id, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, reasoning_tokens,
                     total_tokens, context_tokens, cost, message_count,
                     attribution, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row.run_id, row.task_id, row.input_tokens, row.output_tokens,
                 row.cache_read_tokens, row.cache_write_tokens,
                 row.reasoning_tokens, row.total_tokens, row.context_tokens,
                 float(row.cost), row.message_count, row.attribution,
                 row.source, row.created_at or now_ms()),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_usage failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Readers (dashboard API — read-only connections)
# ─────────────────────────────────────────────────────────────────────────────

def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def list_runs(limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Most recent runs first."""
    own = conn is None
    conn = conn or connect_ro()
    try:
        return _rows(conn,
                     "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
    finally:
        if own:
            conn.close()


def get_run(run_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect_ro()
    try:
        rows = _rows(conn, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        return rows[0] if rows else None
    finally:
        if own:
            conn.close()


def tool_calls_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        rows = _rows(conn,
                     "SELECT * FROM tool_calls WHERE run_id=? ORDER BY id", (run_id,))
        for r in rows:
            r["details"] = _loads(r["details"], {})
            r["arguments"] = _loads(r["arguments"], {})
            r["artifacts"] = _loads(r["artifacts"], [])
        return rows
    finally:
        if own:
            conn.close()


def tests_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        return _rows(conn, "SELECT * FROM tests WHERE run_id=? ORDER BY id", (run_id,))
    finally:
        if own:
            conn.close()


def coverage_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        return _rows(conn,
                     "SELECT * FROM coverage_history WHERE run_id=? ORDER BY id",
                     (run_id,))
    finally:
        if own:
            conn.close()


def mutation_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        rows = _rows(conn,
                     "SELECT * FROM mutation_results WHERE run_id=? ORDER BY id",
                     (run_id,))
        for r in rows:
            r["survivors"] = _loads(r["survivors"], [])
        return rows
    finally:
        if own:
            conn.close()


def bugs_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        return _rows(conn, "SELECT * FROM bugs_found WHERE run_id=? ORDER BY id",
                     (run_id,))
    finally:
        if own:
            conn.close()


def gaps_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect_ro()
    try:
        rows = _rows(conn, "SELECT * FROM gap_explanations WHERE run_id=? ORDER BY id",
                     (run_id,))
        for r in rows:
            r["gaps"] = _loads(r["gaps"], [])
        return rows
    finally:
        if own:
            conn.close()


def usage_for_run(run_id: str, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """
    Cost rows for a run — empty when the database predates the `usage` table.

    A dashboard opened against an older database must render, not crash: the
    demo machine and the database on it are not always the same age as the
    code.  Readers for tables added after schema v1 therefore degrade to an
    empty result rather than raising.
    """
    own = conn is None
    conn = conn or connect_ro()
    try:
        return _rows(conn, "SELECT * FROM usage WHERE run_id=? ORDER BY id", (run_id,))
    except sqlite3.OperationalError as exc:
        logger.debug("usage table unavailable: %s", exc)
        return []
    finally:
        if own:
            conn.close()


def usage_baseline(run_id: str) -> dict[str, Any] | None:
    """The reading start_run took, for finish_run to subtract."""
    try:
        with closing(connect_ro()) as conn:
            rows = _rows(conn,
                         "SELECT * FROM usage WHERE run_id=? AND attribution='baseline' "
                         "ORDER BY id DESC LIMIT 1", (run_id,))
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("usage_baseline lookup failed: %s", exc)
        return None


def latest_artifact(run_id: str, tool: str) -> str:
    """
    Most recent artifact path logged by `tool` in this run.

    This is how explain_gaps finds the coverage report without being told
    where it is: get_coverage already logged it as artifacts[0].  The
    tool_calls log doubles as the run's artifact index.
    """
    try:
        with closing(connect_ro()) as conn:
            rows = _rows(
                conn,
                "SELECT artifacts FROM tool_calls WHERE run_id=? AND tool=? "
                "AND ok=1 ORDER BY id DESC LIMIT 5",
                (run_id, tool),
            )
        for r in rows:
            arts = _loads(r["artifacts"], [])
            if arts:
                return str(arts[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("latest_artifact lookup failed: %s", exc)
    return ""
