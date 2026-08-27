"""
get_coverage(report_path, source_file) — P1 core tool.
list_uncovered(source_file)            — P1 core tool.

Parses Cobertura XML produced by pytest-cov (--cov-report=xml).

Key design invariant (qodo-cover stale-report guard):
    get_coverage asserts report_mtime_ms > run_start_ms.
    A stale report is a known false-positive source — a previous run's
    coverage silently masking a real regression.  We hard-fail rather
    than silently return wrong numbers.

Details payload — get_coverage:
    percent          float  — 0.0–100.0 coverage percentage
    covered_lines    list   — line numbers with hits > 0
    uncovered_ranges list   — [{start, end}] contiguous uncovered ranges
    uncovered_count  int    — total uncovered lines

Details payload — list_uncovered:
    gaps  list  — [{start, end, function, snippet}] — prompt-ready gap map
    source_file str
"""
from __future__ import annotations

import ast
import os
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.schema import ToolResult

# Module-level cache of the last successfully parsed report.
# list_uncovered uses this when no report_path is given.
_last_report_cache: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cobertura(report_path: str) -> dict[str, dict]:
    """
    Parse Cobertura XML and return a dict keyed by (normalised) filename.

    Each value:
        line_rate      float
        covered_lines  list[int]
        uncovered_lines list[int]
    """
    tree = ET.parse(report_path)
    root = tree.getroot()

    files: dict[str, dict] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename", "").replace("\\", "/")
        line_rate = float(cls.get("line-rate", "0"))
        covered: list[int] = []
        uncovered: list[int] = []
        for line in cls.findall("lines/line"):
            no = int(line.get("number", "0"))
            hits = int(line.get("hits", "0"))
            (covered if hits > 0 else uncovered).append(no)
        files[filename] = {
            "line_rate": line_rate,
            "covered_lines": covered,
            "uncovered_lines": uncovered,
        }
    return files


def _lines_to_ranges(lines: list[int]) -> list[dict[str, int]]:
    """Convert a sorted list of line numbers into contiguous {start, end} ranges."""
    if not lines:
        return []
    lines = sorted(set(lines))
    ranges: list[dict[str, int]] = []
    start = end = lines[0]
    for ln in lines[1:]:
        if ln == end + 1:
            end = ln
        else:
            ranges.append({"start": start, "end": end})
            start = end = ln
    ranges.append({"start": start, "end": end})
    return ranges


def _find_file_in_report(
    files: dict[str, dict], source_file: str
) -> tuple[str, dict] | None:
    """
    Match source_file (possibly absolute) against the relative keys in a
    Cobertura report.  Returns (key, data) or None.
    """
    norm = source_file.replace("\\", "/")
    # Exact match first
    if norm in files:
        return norm, files[norm]
    # Suffix match (absolute path against relative key)
    for key, data in files.items():
        if norm.endswith(key) or key.endswith(norm.split("/")[-1]):
            return key, data
    return None


def _get_enclosing_function(source_path: str, line: int) -> str:
    """
    AST-walk the source file and return the name of the function / method
    that contains `line` (1-indexed).  Returns '' on any failure.
    """
    try:
        src = Path(source_path).read_text(encoding="utf-8")
        tree = ast.parse(src)
        best_start = 0
        best_name = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_start = node.lineno
                fn_end = getattr(node, "end_lineno", node.lineno)
                if fn_start <= line <= fn_end and fn_start > best_start:
                    best_start = fn_start
                    best_name = node.name
        return best_name
    except Exception:  # noqa: BLE001
        return ""


def _aggregate_all_files(files: dict[str, dict]) -> dict:
    """Aggregate coverage across all files in the report."""
    all_covered: list[int] = []
    all_uncovered: list[int] = []
    for v in files.values():
        all_covered.extend(v["covered_lines"])
        all_uncovered.extend(v["uncovered_lines"])
    total = len(all_covered) + len(all_uncovered)
    return {
        "line_rate": len(all_covered) / total if total else 0.0,
        "covered_lines": sorted(set(all_covered)),
        "uncovered_lines": sorted(set(all_uncovered)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# get_coverage
# ─────────────────────────────────────────────────────────────────────────────

def get_coverage(
    report_path: str,
    source_file: str = "",
    run_start_ms: int | None = None,
    run_id: str | None = None,
) -> ToolResult:
    """
    Parse a Cobertura XML coverage report.

    Args:
        report_path:  Absolute path to coverage.xml.
        source_file:  Optional relative path as it appears in the XML
                      (e.g. "src/pricing.py").  Empty → aggregate all files.
        run_start_ms: Epoch-milliseconds when the generating test run
                      started.  Used for the stale-report guard.  If None,
                      the guard is skipped (useful in smoke tests).
        run_id:       Shared pipeline UUID.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    t0 = time.monotonic_ns()

    try:
        report = Path(report_path)
        if not report.exists():
            raise FileNotFoundError(f"Coverage report not found: {report_path}")

        # ── Stale-report guard (qodo-cover style) ────────────────────────
        report_mtime_ms = int(report.stat().st_mtime * 1000)
        if run_start_ms is not None and report_mtime_ms <= run_start_ms:
            raise ValueError(
                f"Coverage report is STALE: mtime={report_mtime_ms} ms ≤ "
                f"run_start={run_start_ms} ms.  Re-run tests before parsing coverage."
            )

        files = _parse_cobertura(report_path)

        # Cache for list_uncovered
        _last_report_cache["path"] = report_path
        _last_report_cache["files"] = files

        if source_file:
            match = _find_file_in_report(files, source_file)
            if match is None:
                raise KeyError(
                    f"Source file '{source_file}' not found in report.  "
                    f"Available keys: {list(files.keys())}"
                )
            _, data = match
        else:
            data = _aggregate_all_files(files)

        pct = round(data["line_rate"] * 100, 2)
        uncovered_ranges = _lines_to_ranges(data["uncovered_lines"])

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="get_coverage",
            ok=True,
            score=round(data["line_rate"], 4),
            proceed=True,   # coverage is informational — never blocks
            verdict=(
                f"Coverage: {pct}%  "
                f"({len(data['covered_lines'])} covered, "
                f"{len(data['uncovered_lines'])} uncovered lines)"
            ),
            details={
                "percent": pct,
                "covered_lines": data["covered_lines"],
                "uncovered_ranges": uncovered_ranges,
                "uncovered_count": len(data["uncovered_lines"]),
            },
            artifacts=[report_path],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="get_coverage",
            ok=False,
            score=0.0,
            proceed=False,
            verdict=f"Coverage parse error: {exc}",
            details={"error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# list_uncovered
# ─────────────────────────────────────────────────────────────────────────────

def list_uncovered(
    source_file: str,
    report_path: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Return a prompt-ready gap map: uncovered ranges + enclosing function
    names + code snippets.

    Args:
        source_file:  Absolute path to the Python source file being analysed.
        report_path:  Path to coverage.xml.  If empty, uses the most recent
                      report parsed by get_coverage in this process.
        run_id:       Shared pipeline UUID.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())
    t0 = time.monotonic_ns()

    try:
        rpath = report_path or _last_report_cache.get("path", "")
        if not rpath:
            raise ValueError(
                "No report_path provided and no previously parsed report in cache.  "
                "Call get_coverage first, or pass report_path explicitly."
            )

        files = _parse_cobertura(rpath)
        match = _find_file_in_report(files, source_file)
        if match is None:
            raise KeyError(
                f"'{source_file}' not found in report '{rpath}'.  "
                f"Available keys: {list(files.keys())}"
            )

        _, data = match
        uncovered_lines = sorted(set(data["uncovered_lines"]))
        ranges = _lines_to_ranges(uncovered_lines)

        # Enrich each range with enclosing function + source snippet
        src_path = Path(source_file)
        src_lines: list[str] = []
        if src_path.exists():
            try:
                src_lines = src_path.read_text(encoding="utf-8").splitlines()
            except Exception:  # noqa: BLE001
                pass

        gaps: list[dict] = []
        for r in ranges:
            fn = _get_enclosing_function(str(src_path), r["start"]) if src_path.exists() else ""
            snippet_lines = src_lines[r["start"] - 1: r["end"]] if src_lines else []
            gaps.append({
                "start": r["start"],
                "end": r["end"],
                "function": fn,
                "snippet": "\n".join(snippet_lines),
            })

        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="list_uncovered",
            ok=True,
            score=1.0,   # always succeeds if report is valid
            proceed=True,
            verdict=f"{len(gaps)} uncovered range(s) in {src_path.name}",
            details={"gaps": gaps, "source_file": source_file},
            artifacts=[rpath],
            duration_ms=duration_ms,
            run_id=run_id,
        )

    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.monotonic_ns() - t0) // 1_000_000
        return ToolResult(
            tool="list_uncovered",
            ok=False,
            score=0.0,
            proceed=False,
            verdict=f"list_uncovered error: {exc}",
            details={"error": str(exc)},
            artifacts=[],
            duration_ms=duration_ms,
            run_id=run_id,
        )
