"""
mutation_test(target_module) — P2 pipeline stage 10.

Runs mutmut against ONE module and reports the mutation score plus every
mutant the suite failed to kill, in enough detail for the MuTAP
regeneration step: Bob reads survived[] and writes tests that specifically
kill those mutants (AGENTS.md §4, stage 11).

This is the project's headline metric.  Coverage says the tests EXECUTED
the code; the mutation score says they would NOTICE if it were wrong.

Details payload (AGENTS.md §5):
    mutation_score float — killed / (killed + missed), 0.0–1.0
    killed         int   — mutants the suite detected
    survived       list  — [{id, line, original, mutated, status, function}]
                           every mutant the suite did NOT kill.  Beyond the
                           first _MAX_DETAILED_SURVIVORS entries the diff is
                           omitted (detail_omitted=True) to bound subprocess
                           count; the count is reported, never silently cut.
    detail_omitted_count int — survivors reported without diff detail
    survived_count int   — len(survived)
    not_covered    int   — subset of survived that no test executed at all
    total          int   — mutants generated
    by_status      dict  — {raw mutmut status: count}
    inconclusive   int   — timeouts/suspicious mutants, excluded from score
    engine_available bool— False when mutmut is not installed

Two ways a mutant escapes, both reported in survived[] with a `status`:
    "survived"  — a test ran the mutated line and did not notice.  The test
                  is weak; Bob should strengthen its assertions.
    "no tests"  — no test executed the line at all.  Bob needs a new test,
                  not a better assertion.
Both count against the score: a mutant nobody caught is a mutant nobody
caught.  Collapsing them would flatter the suite; separating them by
`status` tells Bob which of the two fixes to apply.

Gate policy (AGENTS.md §3 and server/main.py): proceed is ALWAYS True.
Surviving mutants lower `score` and drive regeneration; they never halt
the pipeline.

Implementation notes (mutmut 3.x — a rewrite of the 2.x the plan assumed):
    - Config moved to `source_paths` / `only_mutate`, and mutmut
      materialises a `mutants/` working copy in the CURRENT DIRECTORY.
    - To avoid writing config into the repo (pyproject.toml is P1-owned)
      and to avoid leaving `mutants/` behind, this tool copies the project
      to a temp dir and runs entirely inside that copy.  The repo is never
      mutated in place and needs no .gitignore change.
    - `mutmut results --all=True` — in 3.7 `--all` takes a VALUE; the bare
      flag is a usage error.
    - `mutmut show` prints a per-function diff whose @@ hunk numbers are
      FUNCTION-RELATIVE.  Real file lines are resolved here by locating the
      mutated source text inside the function's AST span.
    - `only_mutate` scopes generation to the single target module, the
      pre-agreed mitigation for "mutmut too slow" (plan §3, risk 2).

Stage isolation (AGENTS.md §6): no imports from sibling pipeline modules.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from server.schema import ToolResult

_DEFAULT_TIMEOUT_S = 900        # mutation testing is the slowest stage by far
_RESULTS_TIMEOUT_S = 120
_SHOW_TIMEOUT_S = 30

# Cap on how many survivors get their original->mutated diff fetched.  Each
# costs one `mutmut show` subprocess.  Survivors beyond the cap are still
# reported (id, function, status) with detail_omitted=True.
_MAX_DETAILED_SURVIVORS = 100

# Directories never worth copying into the temp workspace.
_COPY_EXCLUDES = shutil.ignore_patterns(
    ".git", "__pycache__", "mutants", ".mutmut-cache", ".pytest_cache",
    "*.pyc", ".venv", "venv", "node_modules", ".tox",
)

# `mutmut results` prints "    calculator.x_add__mutmut_1: killed"
_RESULT_LINE_RE = re.compile(r"^\s+(?P<id>\S+):\s+(?P<status>.+?)\s*$")

# Mutant id → function name: "pkg.mod.x_divide__mutmut_3" → "divide".
# mutmut mangles the mutated function with an "x_" prefix and a numeric suffix.
_MUTANT_FUNC_RE = re.compile(r"(?:^|\.)x_(?P<func>.+?)__mutmut_\d+$")

# mutmut's own status vocabulary (mutmut/__main__.py status_by_exit_code)
_KILLED_STATUS = "killed"
_MISSED_STATUSES = {"survived", "no tests"}
# Anything else ("suspicious", "skipped", "not checked", timeouts) proves
# nothing either way and is excluded from the score denominator.


# ─────────────────────────────────────────────────────────────────────────────
# mutmut invocation
# ─────────────────────────────────────────────────────────────────────────────

def _mutmut_version() -> tuple[int, ...] | None:
    """Installed mutmut version as a tuple, or None if it cannot be read."""
    try:
        from importlib.metadata import version

        raw = version("mutmut")
    except Exception:  # noqa: BLE001 — absent or unreadable metadata
        return None

    parts: list[int] = []
    for chunk in raw.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))

    return tuple(parts) or None


def _write_config(
    workspace: Path,
    source_dir: str,
    only_mutate: str,
    tests_selection: str,
) -> None:
    """
    Write a setup.cfg [mutmut] section scoping the run to one module.

    setup.cfg rather than pyproject.toml: mutmut prefers pyproject's
    [tool.mutmut] when present, and pyproject belongs to P1 — this tool must
    not depend on editing it.
    """
    config = (
        "[mutmut]\n"
        f"source_paths={source_dir}\n"
        f"only_mutate={only_mutate}\n"
        f"pytest_add_cli_args_test_selection={tests_selection}\n"
    )
    (workspace / "setup.cfg").write_text(config, encoding="utf-8")


def _run_mutmut(
    args: list[str],
    cwd: Path,
    timeout_s: int,
) -> subprocess.CompletedProcess[str]:
    """Invoke mutmut as a module so it uses the interpreter we probed."""
    return subprocess.run(
        [sys.executable, "-m", "mutmut", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _parse_results(output: str) -> dict[str, str]:
    """Parse `mutmut results --all=True` into {mutant_id: status}."""
    statuses: dict[str, str] = {}

    for line in output.splitlines():
        match = _RESULT_LINE_RE.match(line)
        if not match:
            continue
        mutant_id = match.group("id")
        # Progress spinners and headers never carry a mutant suffix.
        if "__mutmut_" not in mutant_id:
            continue
        statuses[mutant_id] = match.group("status").strip()

    return statuses


# ─────────────────────────────────────────────────────────────────────────────
# Locating the mutation in the real source file
# ─────────────────────────────────────────────────────────────────────────────

def _function_spans(source: str) -> dict[str, tuple[int, int]]:
    """
    Map every function/method name to its (start_line, end_line) span.

    Methods are registered under both "method" and "Class.method" so a
    mutant id can be matched whichever form mutmut used.
    """
    spans: dict[str, tuple[int, int]] = {}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return spans

    def _record(name: str, node: ast.AST) -> None:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start) or start
        spans.setdefault(name, (start, end))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _record(node.name, node)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _record(f"{node.name}.{sub.name}", sub)

    return spans


def _resolve_line(
    source_lines: list[str],
    spans: dict[str, tuple[int, int]],
    function: str,
    original: str,
) -> int | None:
    """
    Find the real file line for a mutated statement.

    `mutmut show` renumbers each diff from 1 within the function, so the
    hunk header cannot be used directly.  Instead we search the function's
    AST span for the exact source text the diff removed.
    """
    if not function or function not in spans:
        return None

    start, end = spans[function]
    needle = original.strip()

    if needle:
        for lineno in range(start, min(end, len(source_lines)) + 1):
            if source_lines[lineno - 1].strip() == needle:
                return lineno

    # Text not found (mutmut normalises some constructs) — the function's
    # first line is still a useful anchor for Bob.
    return start


def _parse_show_diff(diff_text: str) -> tuple[str, str]:
    """
    Extract (original, mutated) source text from a `mutmut show` diff.

    Only the first changed pair is returned: each mutmut mutant is a single
    edit, so the first -/+ pair IS the mutation.
    """
    original = mutated = ""

    for raw in diff_text.splitlines():
        if raw.startswith("---") or raw.startswith("+++") or raw.startswith("@@"):
            continue
        if raw.startswith("-") and not original:
            original = raw[1:].strip()
        elif raw.startswith("+") and original and not mutated:
            mutated = raw[1:].strip()
        if original and mutated:
            break

    return original[:300], mutated[:300]


def _function_of(mutant_id: str) -> str:
    """'pkg.mod.x_divide__mutmut_3' → 'divide' ('' when unrecognised)."""
    match = _MUTANT_FUNC_RE.search(mutant_id)
    return match.group("func") if match else ""


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def mutation_test(
    target_module: str,
    cwd: str = "",
    tests_dir: str = "tests",
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    run_id: str | None = None,
) -> ToolResult:
    """
    Run mutation testing on a single module and report unkilled mutants.

    Args:
        target_module: Path to the module to mutate.  Absolute, or relative
                       to cwd (e.g. 'src/calculator.py').
        cwd:           Project root.  Defaults to two directories up from
                       target_module (repo root when source lives in src/).
        tests_dir:     Test selection passed to pytest, relative to cwd.
        timeout_s:     Ceiling for the whole mutmut run (default 900s).
        run_id:        Optional UUID shared across one pipeline run.

    Returns:
        ToolResult with proceed=True always; survivors are regeneration work.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return (time.monotonic_ns() - t0) // 1_000_000

    def _degraded(verdict: str, error: str, extra: dict[str, Any] | None = None) -> ToolResult:
        """Engine or input problem: ok=False, but never gate the pipeline."""
        details: dict[str, Any] = {
            "error": error,
            "engine_available": importlib.util.find_spec("mutmut") is not None,
            "mutation_score": 0.0,
            "killed": 0,
            "survived": [],
            "survived_count": 0,
            "not_covered": 0,
            "total": 0,
        }
        if extra:
            details.update(extra)
        return ToolResult(
            tool="mutation_test",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=verdict,
            details=details,
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    if importlib.util.find_spec("mutmut") is None:
        return _degraded(
            "mutmut is not installed — mutation stage skipped. "
            "Install with: pip install -e \".[pipeline]\"",
            "mutmut_not_installed",
        )

    # mutmut 3 renamed the config keys and changed the results CLI; this tool
    # speaks 3.x only.  Fail loudly rather than silently misparsing 2.x output.
    installed = _mutmut_version()
    if installed and installed[0] < 3:
        return _degraded(
            f"mutmut {'.'.join(map(str, installed))} is too old — this tool "
            f"requires mutmut >= 3.0 (2.x used different config keys and a "
            f"different results CLI). Upgrade with: pip install -U 'mutmut>=3.0'",
            "mutmut_version_too_old",
            {"installed_version": ".".join(map(str, installed))},
        )

    # ── Resolve paths ─────────────────────────────────────────────────────
    module_path = Path(target_module)
    if not cwd:
        if module_path.is_absolute():
            cwd = str(module_path.resolve().parent.parent)
        else:
            return _degraded(
                "cwd is required when target_module is a relative path",
                "cwd_required",
            )

    project_root = Path(cwd).resolve()
    abs_module = (
        module_path.resolve()
        if module_path.is_absolute()
        else (project_root / module_path).resolve()
    )

    if not abs_module.is_file():
        return _degraded(f"Target module not found: {abs_module}", "module_not_found")

    try:
        rel_module = abs_module.relative_to(project_root)
    except ValueError:
        return _degraded(
            f"Target module {abs_module} is not inside project root {project_root}",
            "module_outside_project",
        )

    source_dir = rel_module.parent.as_posix() or "."
    source_text = abs_module.read_text(encoding="utf-8", errors="replace")
    source_lines = source_text.splitlines()
    spans = _function_spans(source_text)

    # ── Work inside a disposable copy of the project ──────────────────────
    with tempfile.TemporaryDirectory(prefix="bob_mutation_") as tmp:
        workspace = Path(tmp) / project_root.name
        try:
            shutil.copytree(project_root, workspace, ignore=_COPY_EXCLUDES)
        except OSError as exc:
            return _degraded(f"Could not stage a temp workspace: {exc}", str(exc))

        _write_config(
            workspace,
            source_dir=source_dir,
            only_mutate=rel_module.as_posix(),
            tests_selection=tests_dir,
        )

        # ── Generate and test mutants ─────────────────────────────────────
        try:
            run_proc = _run_mutmut(["run"], workspace, timeout_s)
        except subprocess.TimeoutExpired:
            return _degraded(
                f"mutmut exceeded {timeout_s}s on {rel_module} — "
                f"scope to a smaller module or raise timeout_s",
                f"timeout_{timeout_s}s",
            )
        except OSError as exc:
            return _degraded(f"Could not start mutmut: {exc}", str(exc))

        # ── Read per-mutant statuses ──────────────────────────────────────
        # NOTE: `--all` takes a value in mutmut 3.7; the bare flag errors.
        try:
            results_proc = _run_mutmut(
                ["results", "--all=True"], workspace, _RESULTS_TIMEOUT_S
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _degraded(f"Could not read mutmut results: {exc}", str(exc))

        statuses = _parse_results(results_proc.stdout)

        if not statuses:
            tail = "\n".join(
                (run_proc.stdout + "\n" + run_proc.stderr).strip().splitlines()[-25:]
            )
            return _degraded(
                f"mutmut generated no mutants for {rel_module} — check that the "
                f"module contains mutatable code and that the test selection "
                f"'{tests_dir}' is correct",
                "no_mutants_generated",
                {"mutmut_output_tail": tail,
                 "results_stderr": results_proc.stderr.strip()[-500:]},
            )

        # ── Classify and detail every unkilled mutant ─────────────────────
        by_status: dict[str, int] = {}
        missed_ids: list[str] = []
        killed = inconclusive = 0

        for mutant_id, status in statuses.items():
            by_status[status] = by_status.get(status, 0) + 1
            if status == _KILLED_STATUS:
                killed += 1
            elif status in _MISSED_STATUSES:
                missed_ids.append(mutant_id)
            else:
                inconclusive += 1

        # Detailing a survivor costs one `mutmut show` subprocess each, so the
        # loop is capped.  A module with thousands of survivors would otherwise
        # spawn thousands of processes for data Bob cannot act on in one round
        # anyway.  Every survivor is still REPORTED — only the diff detail is
        # limited — and the cap is stated in details/verdict rather than
        # silently truncating (see AGENTS.md: no silent caps).
        ordered_ids = sorted(missed_ids)
        detailed_ids = ordered_ids[:_MAX_DETAILED_SURVIVORS]
        undetailed_ids = ordered_ids[_MAX_DETAILED_SURVIVORS:]

        survived: list[dict[str, Any]] = []
        for mutant_id in detailed_ids:
            original = mutated = ""
            try:
                show_proc = _run_mutmut(["show", mutant_id], workspace, _SHOW_TIMEOUT_S)
                original, mutated = _parse_show_diff(show_proc.stdout)
            except (subprocess.TimeoutExpired, OSError):
                pass  # detail is best-effort; the id alone is still actionable

            function = _function_of(mutant_id)
            survived.append({
                "id": mutant_id,
                "line": _resolve_line(source_lines, spans, function, original),
                "original": original,
                "mutated": mutated,
                "status": statuses[mutant_id],
                "function": function,
            })

        # Beyond the cap: id, function and status without the diff.
        for mutant_id in undetailed_ids:
            function = _function_of(mutant_id)
            survived.append({
                "id": mutant_id,
                "line": _resolve_line(source_lines, spans, function, ""),
                "original": "",
                "mutated": "",
                "status": statuses[mutant_id],
                "function": function,
                "detail_omitted": True,
            })

        survived_count = len(survived)
        not_covered = sum(1 for m in survived if m["status"] == "no tests")
        scored_total = killed + survived_count
        mutation_score = (killed / scored_total) if scored_total else 0.0

        # ── Verdict ───────────────────────────────────────────────────────
        if survived:
            weak = survived_count - not_covered
            parts = []
            if weak:
                parts.append(f"{weak} ran but undetected")
            if not_covered:
                parts.append(f"{not_covered} never executed by any test")
            breakdown = ", ".join(parts)

            preview = ", ".join(
                f"{m['function']}:{m['line']}" if m["line"] else m["id"]
                for m in survived[:5]
            )
            more = f" (+{survived_count - 5} more)" if survived_count > 5 else ""
            verdict = (
                f"Mutation score {mutation_score:.0%} on {rel_module} "
                f"({killed} killed, {survived_count} survived — {breakdown}) "
                f"— targets: {preview}{more}"
            )
        else:
            verdict = (
                f"Mutation score {mutation_score:.0%} on {rel_module} "
                f"({killed}/{scored_total} killed) — no surviving mutants"
            )

        if inconclusive:
            verdict += f" [{inconclusive} inconclusive, excluded from score]"
        if undetailed_ids:
            verdict += (
                f" [diff detail omitted for {len(undetailed_ids)} survivor(s) "
                f"beyond the first {_MAX_DETAILED_SURVIVORS}]"
            )

        return ToolResult(
            tool="mutation_test",
            ok=True,
            score=round(mutation_score, 4),
            proceed=True,      # survivors are regeneration work — AGENTS.md §3
            verdict=verdict,
            details={
                "mutation_score": round(mutation_score, 4),
                "killed": killed,
                "survived": survived,
                "survived_count": survived_count,
                "not_covered": not_covered,
                "total": len(statuses),
                "scored_total": scored_total,
                "inconclusive": inconclusive,
                "by_status": by_status,
                "detail_omitted_count": len(undetailed_ids),
                "target_module": rel_module.as_posix(),
                "engine_available": True,
            },
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )
