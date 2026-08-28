"""
explain_gaps / store_explanation / save_test_record — P4 tools (stage 13).

Placement (AGENTS.md §2, critical placement rule):
    explain_gaps is MECHANICAL.  It parses the coverage report, resolves
    each uncovered region to its enclosing function, and attaches the
    source text plus a few objective SIGNALS about that region.  It does
    not categorise the gap and it does not write prose — Bob does that
    (skill stage 13b), and store_explanation persists what he wrote.

    The signals are deliberately factual: "this region sits inside an
    `if __name__ == '__main__'` guard", "this region is a bare `raise`".
    Naming the category ("dead code", "external dependency") would be
    judgement, and judgement belongs in Layer 1.

Details payload — explain_gaps:
    source_file   str   — file analysed
    report_path   str   — coverage report used (and how it was found)
    percent       float — line coverage for that file
    gaps          list  — [{start, end, function, signature, docstring,
                            snippet, line_count, signals{...}}]
    gap_count     int
    total_uncovered int

Details payload — store_explanation:
    stored        bool
    run_id        str
    chars         int

Details payload — save_test_record:
    stored        bool
    bug_recorded  bool   — True when bug_found=true also wrote bugs_found

Gate policy (AGENTS.md §3): proceed is ALWAYS True for all three.  These
are reporting tools; nothing they find can invalidate the suite.
"""
from __future__ import annotations

import ast
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.data import db
from server.data.models import BugFound, GapExplanation, TestRecord
from server.schema import ToolResult

REPO_ROOT = Path(__file__).resolve().parent.parent

# How many source lines to include per gap.  Enough for Bob to see what the
# region does; bounded so a large uncovered block cannot flood the context
# window with one tool response.
_MAX_SNIPPET_LINES = 40


# ─────────────────────────────────────────────────────────────────────────────
# Coverage report discovery and parsing
# ─────────────────────────────────────────────────────────────────────────────

def _find_report(report_path: str, run_id: str, source_file: str) -> tuple[str, str]:
    """
    Locate a Cobertura report.  Returns (path, how_it_was_found).

    The skill calls explain_gaps(source_file=...) with no report path
    (SKILL.md stage 13a), so discovery matters:

      1. an explicit report_path argument always wins;
      2. otherwise the tool_calls log — get_coverage already recorded the
         report it parsed in artifacts[0], which makes the DB the run's
         artifact index;
      3. otherwise the conventional locations on disk.
    """
    if report_path and Path(report_path).exists():
        return report_path, "argument"

    logged = db.latest_artifact(run_id, "get_coverage")
    if logged and Path(logged).exists():
        return logged, "tool_calls log (get_coverage artifact)"

    candidates: list[Path] = []
    src = Path(source_file)
    if src.is_absolute():
        # walk up from the source file: src/ → project root → repo root
        for parent in list(src.parents)[:4]:
            candidates.append(parent / "coverage.xml")
    candidates.append(REPO_ROOT / "coverage.xml")
    candidates.append(REPO_ROOT / "sample_repo" / "coverage.xml")

    for candidate in candidates:
        if candidate.exists():
            return str(candidate), "filesystem search"

    raise FileNotFoundError(
        "No coverage report found. Pass report_path explicitly, or call "
        "get_coverage first in this run so the report is logged."
    )


def _uncovered_lines(report_path: str, source_file: str) -> tuple[list[int], float]:
    """Return (uncovered line numbers, percent) for source_file in the report."""
    root = ET.parse(report_path).getroot()
    wanted = Path(source_file).name

    best: tuple[list[int], list[int]] | None = None
    for cls in root.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/")
        if not filename or Path(filename).name != wanted:
            continue
        covered: list[int] = []
        uncovered: list[int] = []
        for line in cls.findall("lines/line"):
            number = int(line.get("number", "0"))
            hits = int(line.get("hits", "0"))
            (covered if hits > 0 else uncovered).append(number)
        best = (covered, uncovered)
        break

    if best is None:
        raise KeyError(f"'{wanted}' does not appear in {report_path}")

    covered, uncovered = best
    total = len(covered) + len(uncovered)
    percent = round(100.0 * len(covered) / total, 2) if total else 0.0
    return sorted(uncovered), percent


def _to_ranges(lines: list[int]) -> list[dict[str, int]]:
    if not lines:
        return []
    ranges: list[dict[str, int]] = []
    start = end = lines[0]
    for line in lines[1:]:
        if line == end + 1:
            end = line
        else:
            ranges.append({"start": start, "end": end})
            start = end = line
    ranges.append({"start": start, "end": end})
    return ranges


# ─────────────────────────────────────────────────────────────────────────────
# Source context
# ─────────────────────────────────────────────────────────────────────────────

class _SourceIndex:
    """AST view of one module: which function owns a line, and its metadata."""

    def __init__(self, path: Path) -> None:
        self.lines = path.read_text(encoding="utf-8").splitlines()
        try:
            self.tree: ast.Module | None = ast.parse("\n".join(self.lines))
        except SyntaxError:
            self.tree = None
        self._main_guard: set[int] = set()
        if self.tree is not None:
            self._index_main_guard(self.tree)

    def _index_main_guard(self, tree: ast.Module) -> None:
        """Record the line span of any `if __name__ == "__main__":` block."""
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                end = getattr(node, "end_lineno", node.lineno)
                self._main_guard.update(range(node.lineno, end + 1))

    def enclosing(self, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """Innermost function containing `line`, or None at module level."""
        if self.tree is None:
            return None
        best = None
        best_start = 0
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno)
                if node.lineno <= line <= end and node.lineno > best_start:
                    best, best_start = node, node.lineno
        return best

    def snippet(self, start: int, end: int) -> str:
        end = min(end, start + _MAX_SNIPPET_LINES - 1)
        chunk = self.lines[start - 1:end]
        return "\n".join(f"{start + i:>4} | {text}" for i, text in enumerate(chunk))

    def in_main_guard(self, start: int, end: int) -> bool:
        return any(line in self._main_guard for line in range(start, end + 1))


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [a.arg for a in node.args.args]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def _region_signals(index: _SourceIndex, start: int, end: int,
                    node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> dict[str, Any]:
    """
    Objective facts about an uncovered region.

    Bob turns these into a category (dead code / external dependency /
    complex trigger / intentional skip / iteration limit).  This tool
    deliberately stops short of naming the category itself.
    """
    body = [index.lines[i - 1] for i in range(start, min(end, len(index.lines)) + 1)]
    stripped = [line.strip() for line in body if line.strip()]
    return {
        "in_main_guard": index.in_main_guard(start, end),
        "raises_only": bool(stripped) and all(s.startswith("raise") for s in stripped),
        "has_raise": any(s.startswith("raise") for s in stripped),
        "in_except_handler": any(s.startswith(("except", "finally")) for s in stripped),
        "is_logging_only": bool(stripped) and all(
            s.startswith(("logger.", "logging.", "print(")) for s in stripped
        ),
        "decorators": [ast.unparse(d) for d in node.decorator_list] if node else [],
        "line_count": end - start + 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# explain_gaps
# ─────────────────────────────────────────────────────────────────────────────

def explain_gaps(
    source_file: str,
    report_path: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Return prompt-ready structured data about every uncovered region.

    Args:
        source_file: Path to the Python source file being analysed.
        report_path: Optional coverage.xml.  Discovered from the tool_calls
                     log when omitted.
        run_id:      Shared pipeline UUID.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    t0 = time.monotonic_ns()

    try:
        source = Path(source_file)
        if not source.is_absolute():
            source = (REPO_ROOT / source_file).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Source file not found: {source_file}")

        resolved_report, how = _find_report(report_path, run_id, str(source))
        uncovered, percent = _uncovered_lines(resolved_report, str(source))
        index = _SourceIndex(source)

        gaps: list[dict[str, Any]] = []
        for rng in _to_ranges(uncovered):
            start, end = rng["start"], rng["end"]
            node = index.enclosing(start)
            docstring = ast.get_docstring(node) if node else None
            gaps.append({
                "start": start,
                "end": end,
                "line_count": end - start + 1,
                "function": node.name if node else "<module level>",
                "signature": _signature(node) if node else "",
                "docstring": (docstring or "").strip().split("\n")[0],
                "snippet": index.snippet(start, end),
                "signals": _region_signals(index, start, end, node),
            })

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="explain_gaps",
            ok=True,
            score=round(percent / 100.0, 4),
            proceed=True,   # reporting stage — never gates
            verdict=(
                f"{len(gaps)} uncovered region(s) across {len(uncovered)} lines "
                f"in {source.name} ({percent}% covered)"
            ),
            details={
                "source_file": str(source),
                "report_path": resolved_report,
                "report_found_via": how,
                "percent": percent,
                "gaps": gaps,
                "gap_count": len(gaps),
                "total_uncovered": len(uncovered),
            },
            artifacts=[resolved_report],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="explain_gaps",
            ok=False,
            # A reporting tool that cannot read its input must not stall the
            # pipeline: the final report is written with what is available.
            proceed=True,
            score=0.0,
            verdict=f"explain_gaps error: {exc}",
            details={"error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# store_explanation
# ─────────────────────────────────────────────────────────────────────────────

def store_explanation(
    text: str,
    source_file: str = "",
    gaps: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
) -> ToolResult:
    """
    Persist Bob's plain-language gap narration for the dashboard.

    Args:
        text:        The explanation Bob wrote (markdown is fine).
        source_file: File the explanation is about.
        gaps:        Optional gap structures being narrated, stored verbatim
                     so the dashboard can show the prose beside the code.
        run_id:      Shared pipeline UUID.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    t0 = time.monotonic_ns()

    try:
        if not text or not text.strip():
            raise ValueError("Explanation text is empty — nothing to store.")

        stored = db.insert_gap_explanation(GapExplanation(
            run_id=run_id,
            text=text,
            source_file=source_file,
            gaps=gaps or [],
        ))
        if not stored:
            raise RuntimeError("Database write failed (see server log).")

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="store_explanation",
            ok=True,
            score=1.0,
            proceed=True,
            verdict=f"Stored gap explanation ({len(text)} chars) for run {run_id}",
            details={"stored": True, "run_id": run_id, "chars": len(text)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="store_explanation",
            ok=False,
            proceed=True,
            score=0.0,
            verdict=f"store_explanation error: {exc}",
            details={"stored": False, "error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# save_test_record
# ─────────────────────────────────────────────────────────────────────────────

def save_test_record(
    test_name: str,
    test_file: str = "",
    kept: bool = True,
    reason: str = "",
    stage: str = "regression",
    bug_found: bool = False,
    source_file: str = "",
    evidence: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Record a test's fate, including the bug-vs-wrong-test verdict.

    This is the write path for AGENTS.md §4 classification (a): a
    regression test that fails because the CODE is wrong is a DEFECT
    FOUND, not a discard.  Bob calls this with bug_found=true and keeps
    the test; the row lands in `bugs_found` and lights up the dashboard's
    defects panel.

    Classification (b) — the test asserts wrong behaviour — is recorded by
    calling this with kept=false and a reason, and no bugs_found row.

    Args:
        test_name:   Name of the test function.
        test_file:   File it lives in.
        kept:        Whether the test stays in the suite.
        reason:      Why kept or discarded — shown verbatim on the dashboard.
        stage:       Pipeline stage that made the call (regression, mutation,
                     smell_critique, …).
        bug_found:   True when this test exposed a genuine defect.
        source_file: The module the defect is in (bug rows only).
        evidence:    Failing input / traceback tail (bug rows only).
        run_id:      Shared pipeline UUID.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    t0 = time.monotonic_ns()

    try:
        if not test_name.strip():
            raise ValueError("test_name is required.")

        wrote_test = db.insert_test(TestRecord(
            run_id=run_id,
            test_file=test_file,
            test_name=test_name,
            stage=stage,
            kept=kept,
            reason=reason,
            bug_found=bug_found,
        ))
        if not wrote_test:
            raise RuntimeError("Database write failed (see server log).")

        bug_recorded = False
        if bug_found:
            bug_recorded = db.insert_bug(BugFound(
                run_id=run_id,
                source_file=source_file,
                test_name=test_name,
                stage=stage,
                description=reason or f"{test_name} exposed a defect",
                evidence=evidence,
            ))

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        verdict = (
            f"DEFECT recorded: {test_name} ({source_file or 'source'})"
            if bug_found else
            f"Test record saved: {test_name} ({'kept' if kept else 'discarded'})"
        )
        return ToolResult(
            tool="save_test_record",
            ok=True,
            score=1.0,
            proceed=True,
            verdict=verdict,
            details={
                "stored": True,
                "bug_recorded": bug_recorded,
                "test_name": test_name,
                "kept": kept,
                "stage": stage,
            },
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="save_test_record",
            ok=False,
            proceed=True,
            score=0.0,
            verdict=f"save_test_record error: {exc}",
            details={"stored": False, "error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )
