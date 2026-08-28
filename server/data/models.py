# Bob the Tester — FROZEN DB schema (P4 contract)
# DO NOT MODIFY outside a `contract-change` PR approved by all team members.
# See AGENTS.md §6 (server/data/models.py is FROZEN) and §7 (invariant 7).
"""
models.py — the SQLite table definitions for Bob the Tester.

This module is pure declaration: DDL strings and the row dataclasses that
name their columns.  It performs NO I/O and imports nothing else from the
server, so the dashboard (which must never import the tool layer) can read
it as safely as the writers in db.py can.

Seven tables (implementation plan §1, P4 deliverables):

    runs              one row per pipeline run (the run_id every tool shares)
    tool_calls        the full "log everything" trail — one row per tool call
    tests             per-candidate keep/discard decisions with the reason
    coverage_history  coverage percent per iteration, for the trend chart
    mutation_results  mutation score + survivors per mutation_test call
    bugs_found        genuine defects (bug-vs-wrong-test path (a), AGENTS.md §4)
    gap_explanations  Bob's plain-language narration of uncovered regions,
                      persisted by store_explanation (AGENTS.md §5)

Design notes that matter downstream:

  * Every table except `runs` carries `run_id`, so the dashboard filters a
    whole story with one WHERE clause.  It is a plain indexed TEXT column
    rather than a hard foreign key: a tool call must never fail because a
    `runs` row has not been opened yet.  db.ensure_run() backfills one.

  * Structured payloads (details, artifacts, survivors) are stored as JSON
    TEXT.  They come from ToolResult.details, whose shape is tool-specific
    by contract (AGENTS.md §3) and so cannot be columns.  Anything the
    dashboard filters or plots is ALSO promoted to a real column — JSON is
    for detail views, never for queries.

  * Timestamps are epoch milliseconds (INTEGER), not strings: sorting is
    then trivially correct and timezone-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Bump only via a contract-change PR.  db.py stores this in `meta` and warns
# when it opens a database written by a different schema version.
SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- One row per pipeline run.  run_id is the UUID every ToolResult carries.
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    started_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    source_file      TEXT    NOT NULL DEFAULT '',
    coverage_target  REAL,
    mutation_target  REAL,
    max_iterations   INTEGER,
    iterations_used  INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'running',
    replay_mode      INTEGER NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT ''
);

-- The complete tool-call log.  One row per dispatch, written by logging_mw.
CREATE TABLE IF NOT EXISTS tool_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    tool         TEXT    NOT NULL,
    ok           INTEGER NOT NULL,
    proceed      INTEGER NOT NULL,
    score        REAL    NOT NULL,
    verdict      TEXT    NOT NULL DEFAULT '',
    arguments    TEXT    NOT NULL DEFAULT '{}',
    details      TEXT    NOT NULL DEFAULT '{}',
    artifacts    TEXT    NOT NULL DEFAULT '[]',
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    replayed     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_run  ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS ix_tool_calls_tool ON tool_calls(tool);

-- Per-candidate outcome.  Written automatically from validate_and_keep and
-- explicitly by Bob via save_test_record (which is what can set bug_found).
CREATE TABLE IF NOT EXISTS tests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT    NOT NULL,
    test_file          TEXT    NOT NULL DEFAULT '',
    test_name          TEXT    NOT NULL DEFAULT '',
    stage              TEXT    NOT NULL DEFAULT 'validate',
    kept               INTEGER NOT NULL DEFAULT 0,
    reason             TEXT    NOT NULL DEFAULT '',
    coverage_before    REAL,
    coverage_after     REAL,
    rollback_performed INTEGER NOT NULL DEFAULT 0,
    bug_found          INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tests_run ON tests(run_id);

-- Coverage percent over time: one row per get_coverage call that resolved a
-- percent, so the dashboard can draw the baseline-to-target trend.
CREATE TABLE IF NOT EXISTS coverage_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    iteration       INTEGER NOT NULL DEFAULT 0,
    source_file     TEXT    NOT NULL DEFAULT '',
    percent         REAL    NOT NULL,
    covered_count   INTEGER NOT NULL DEFAULT 0,
    uncovered_count INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_coverage_run ON coverage_history(run_id);

-- One row per mutation_test call.  Two rows in a run are the before/after
-- pair that makes the MuTAP improvement visible on the dashboard.
CREATE TABLE IF NOT EXISTS mutation_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT    NOT NULL,
    target_module    TEXT    NOT NULL DEFAULT '',
    mutation_score   REAL    NOT NULL DEFAULT 0.0,
    killed           INTEGER NOT NULL DEFAULT 0,
    survived_count   INTEGER NOT NULL DEFAULT 0,
    not_covered      INTEGER NOT NULL DEFAULT 0,
    total            INTEGER NOT NULL DEFAULT 0,
    inconclusive     INTEGER NOT NULL DEFAULT 0,
    survivors        TEXT    NOT NULL DEFAULT '[]',
    engine_available INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mutation_run ON mutation_results(run_id);

-- Genuine defects.  A test that fails because the CODE is wrong lands here
-- and is KEPT (AGENTS.md §4); a test that asserts wrong behaviour does not.
CREATE TABLE IF NOT EXISTS bugs_found (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT    NOT NULL,
    source_file   TEXT    NOT NULL DEFAULT '',
    test_name     TEXT    NOT NULL DEFAULT '',
    stage         TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    evidence      TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bugs_run ON bugs_found(run_id);

-- Bob's plain-language narration of why regions stayed uncovered.
CREATE TABLE IF NOT EXISTS gap_explanations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    source_file TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL,
    gaps        TEXT    NOT NULL DEFAULT '[]',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gaps_run ON gap_explanations(run_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Row dataclasses
#
# These name the columns for the writers and give the dashboard typed access.
# Deliberately plain: no ORM, no validation, no behaviour.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Run:
    run_id: str
    started_at: int
    finished_at: int | None = None
    source_file: str = ""
    coverage_target: float | None = None
    mutation_target: float | None = None
    max_iterations: int | None = None
    iterations_used: int = 0
    status: str = "running"
    replay_mode: int = 0
    notes: str = ""


@dataclass
class ToolCall:
    run_id: str
    tool: str
    ok: bool
    proceed: bool
    score: float
    verdict: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    duration_ms: int = 0
    replayed: bool = False
    created_at: int = 0


@dataclass
class TestRecord:
    run_id: str
    test_file: str = ""
    test_name: str = ""
    stage: str = "validate"
    kept: bool = False
    reason: str = ""
    coverage_before: float | None = None
    coverage_after: float | None = None
    rollback_performed: bool = False
    bug_found: bool = False
    created_at: int = 0


@dataclass
class CoveragePoint:
    run_id: str
    percent: float
    iteration: int = 0
    source_file: str = ""
    covered_count: int = 0
    uncovered_count: int = 0
    created_at: int = 0


@dataclass
class MutationRow:
    run_id: str
    target_module: str = ""
    mutation_score: float = 0.0
    killed: int = 0
    survived_count: int = 0
    not_covered: int = 0
    total: int = 0
    inconclusive: int = 0
    survivors: list[dict[str, Any]] = field(default_factory=list)
    engine_available: bool = True
    created_at: int = 0


@dataclass
class BugFound:
    run_id: str
    source_file: str = ""
    test_name: str = ""
    stage: str = ""
    description: str = ""
    evidence: str = ""
    created_at: int = 0


@dataclass
class GapExplanation:
    run_id: str
    text: str
    source_file: str = ""
    gaps: list[dict[str, Any]] = field(default_factory=list)
    created_at: int = 0
