"""
detect_smells(test_file) — P2 pipeline stage 4.

Automated static checks on a test file.  Pure AST analysis: no subprocess,
no test execution, no LLM.  Fully deterministic.

This tool reports RAW FINDINGS ONLY.  The critique of those findings —
deciding which weak assertions to rewrite and how — is a Layer 1 skill step
that Bob performs (AGENTS.md §2, critical placement rule).  Do not add
judgement here.

Smell types detected:
    no_assertion       test body contains no assertion of any kind
    empty_test         body is only `pass` / `...` / a docstring
    trivial_assertion  assertion that almost cannot fail — `assert True`,
                       `assert x is not None`, bare `assertTrue(x)`,
                       `assertIsNotNone(x)`, `assert 1 == 1`
    duplicate_body     two or more tests with structurally identical bodies
    sleep_call         time.sleep() in a test — a flakiness source

Details payload (AGENTS.md §5):
    findings      list — [{test_name, smell_type, line, snippet}]
    tests_scanned int  — number of test functions analysed
    smell_score   float— 1.0 = clean, 0.0 = every test is smelly
    by_type       dict — {smell_type: count}

Gate policy (AGENTS.md §3): proceed is ALWAYS True.  A smelly test can
still be a correct, coverage-adding test; smells lower `score` and add a
warning to `verdict`, they never halt the pipeline.
"""
from __future__ import annotations

import ast
import time
import uuid
from typing import Any

from server.schema import ToolResult

# Weight each smell contributes to the penalty.  Tuned so that a test with
# no assertion at all costs roughly twice what a weak assertion costs.
_SMELL_WEIGHTS: dict[str, float] = {
    "no_assertion": 1.0,
    "empty_test": 1.0,
    "trivial_assertion": 0.5,
    "duplicate_body": 0.5,
    "sleep_call": 0.5,
}

# unittest-style assertions that pass for almost any truthy/non-None value
_TRIVIAL_ASSERT_METHODS = {
    "assertTrue",
    "assertIsNotNone",
    "assertNotEqual",  # only flagged when compared against None (see below)
}

# Names that count as a real assertion when called
_ASSERT_CALL_PREFIXES = ("assert", "assert_")


# ─────────────────────────────────────────────────────────────────────────────
# AST helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_test_function(node: ast.AST) -> bool:
    """True for `def test_*` / `async def test_*` function definitions."""
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


def _collect_test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    """
    Return every test function in the module, including methods inside
    `class Test…` containers.  Order follows source order.
    """
    found: list[ast.FunctionDef] = []

    for node in tree.body:
        if _is_test_function(node):
            found.append(node)  # type: ignore[arg-type]
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if _is_test_function(sub):
                    found.append(sub)  # type: ignore[arg-type]

    return found


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return the body with a leading docstring expression removed."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_body_effectively_empty(body: list[ast.stmt]) -> bool:
    """True when the body does nothing — only `pass`, `...`, or a docstring."""
    real = _strip_docstring(body)
    if not real:
        return True

    for stmt in real:
        if isinstance(stmt, ast.Pass):
            continue
        # A bare `...` expression
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        return False

    return True


def _call_name(node: ast.Call) -> str:
    """Best-effort dotted name of a call target: `self.assertTrue` → 'assertTrue'."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _dotted_name(node: ast.AST) -> str:
    """Full dotted path for Attribute/Name chains: `time.sleep` → 'time.sleep'."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _has_assertion(fn: ast.FunctionDef) -> bool:
    """
    True when the test contains any real assertion:
        - an `assert` statement
        - a call to assert*/assert_* (unittest or helper style)
        - a `with pytest.raises(...)` / `pytest.warns(...)` block, which is
          itself an assertion about behaviour
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            return True

        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name.startswith(_ASSERT_CALL_PREFIXES):
                return True
            dotted = _dotted_name(node.func)
            if dotted.endswith(("pytest.raises", "pytest.warns", "pytest.fail")):
                return True

        # `with pytest.raises(ValueError):`
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    dotted = _dotted_name(item.context_expr.func)
                    if dotted.endswith(("raises", "warns")):
                        return True

    return False


def _is_trivial_assert_stmt(node: ast.Assert) -> bool:
    """
    True for `assert` statements that are nearly unfalsifiable:
        assert True            — constant truth
        assert x is not None   — passes for any value except None
        assert 1 == 1          — both sides identical constants
        assert x               — bare truthiness with no comparison
    """
    test = node.test

    # assert True / assert 1 / assert "text"
    if isinstance(test, ast.Constant):
        return bool(test.value)

    # assert x is not None  /  assert x != None
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        comparator = test.comparators[0]
        is_none = isinstance(comparator, ast.Constant) and comparator.value is None
        if is_none and isinstance(op, (ast.IsNot, ast.NotEq)):
            return True

        # assert 1 == 1 — identical constant operands
        left, right = test.left, comparator
        if (
            isinstance(op, ast.Eq)
            and isinstance(left, ast.Constant)
            and isinstance(right, ast.Constant)
            and left.value == right.value
        ):
            return True
        return False

    # assert some_value  — bare name/attribute/subscript truthiness
    if isinstance(test, (ast.Name, ast.Attribute, ast.Subscript)):
        return True

    return False


def _is_trivial_assert_call(node: ast.Call) -> bool:
    """True for unittest-style assertions that barely constrain the value."""
    name = _call_name(node)

    if name in ("assertTrue", "assertIsNotNone"):
        return True

    # assertNotEqual(x, None) is the same weak check as assertIsNotNone
    if name == "assertNotEqual" and len(node.args) == 2:
        second = node.args[1]
        return isinstance(second, ast.Constant) and second.value is None

    return False


def _is_sleep_call(node: ast.Call) -> bool:
    """True for time.sleep(...) / sleep(...) — a deliberate flakiness source."""
    dotted = _dotted_name(node.func)
    return dotted == "time.sleep" or dotted.endswith(".sleep") or dotted == "sleep"


def _body_fingerprint(fn: ast.FunctionDef) -> str:
    """
    Structural fingerprint of a test body, ignoring the docstring.

    Uses ast.dump without attributes so that line numbers and formatting
    differences do not hide a genuine copy-paste duplicate.
    """
    real_body = _strip_docstring(fn.body)
    return "|".join(
        ast.dump(stmt, annotate_fields=True, include_attributes=False)
        for stmt in real_body
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def detect_smells(
    test_file: str,
    run_id: str | None = None,
) -> ToolResult:
    """
    Run automated smell checks over a test file and return raw findings.

    Args:
        test_file: Absolute path to the Python test file to analyse.
        run_id:    Optional UUID shared across all tool calls in one
                   pipeline run.  Auto-generated if omitted.

    Returns:
        ToolResult with proceed=True always (smells never gate the pipeline).
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return (time.monotonic_ns() - t0) // 1_000_000

    # ── Read and parse ────────────────────────────────────────────────────
    try:
        with open(test_file, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        return ToolResult(
            tool="detect_smells",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=f"Cannot read test file: {exc}",
            details={"error": str(exc), "findings": [], "tests_scanned": 0},
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    try:
        tree = ast.parse(source, filename=test_file)
    except SyntaxError as exc:
        return ToolResult(
            tool="detect_smells",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=f"Test file has a syntax error on line {exc.lineno}: {exc.msg}",
            details={
                "error": f"SyntaxError: {exc.msg}",
                "line": exc.lineno,
                "findings": [],
                "tests_scanned": 0,
            },
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    source_lines = source.splitlines()

    def _snippet(lineno: int) -> str:
        """One source line, trimmed — enough context for Bob to locate it."""
        if 1 <= lineno <= len(source_lines):
            return source_lines[lineno - 1].strip()[:200]
        return ""

    # ── Walk every test function ──────────────────────────────────────────
    tests = _collect_test_functions(tree)
    findings: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}   # fingerprint → first test that had it

    for fn in tests:
        # Empty body takes precedence over "no assertion" — it is the more
        # specific diagnosis and reporting both would double-count.
        if _is_body_effectively_empty(fn.body):
            findings.append({
                "test_name": fn.name,
                "smell_type": "empty_test",
                "line": fn.lineno,
                "snippet": _snippet(fn.lineno),
            })
        elif not _has_assertion(fn):
            findings.append({
                "test_name": fn.name,
                "smell_type": "no_assertion",
                "line": fn.lineno,
                "snippet": _snippet(fn.lineno),
            })

        # Trivial assertions and sleeps are reported per occurrence.
        for node in ast.walk(fn):
            if isinstance(node, ast.Assert) and _is_trivial_assert_stmt(node):
                findings.append({
                    "test_name": fn.name,
                    "smell_type": "trivial_assertion",
                    "line": node.lineno,
                    "snippet": _snippet(node.lineno),
                })
            elif isinstance(node, ast.Call):
                if _is_trivial_assert_call(node):
                    findings.append({
                        "test_name": fn.name,
                        "smell_type": "trivial_assertion",
                        "line": node.lineno,
                        "snippet": _snippet(node.lineno),
                    })
                elif _is_sleep_call(node):
                    findings.append({
                        "test_name": fn.name,
                        "smell_type": "sleep_call",
                        "line": node.lineno,
                        "snippet": _snippet(node.lineno),
                    })

        # Duplicate bodies — report the second and later occurrences only,
        # naming the original in the snippet so Bob knows what it duplicates.
        fp = _body_fingerprint(fn)
        if fp:
            if fp in fingerprints:
                findings.append({
                    "test_name": fn.name,
                    "smell_type": "duplicate_body",
                    "line": fn.lineno,
                    "snippet": f"duplicates {fingerprints[fp]}(): {_snippet(fn.lineno)}",
                })
            else:
                fingerprints[fp] = fn.name

    # ── Score ─────────────────────────────────────────────────────────────
    # Penalty is normalised per test so a 3-test file and a 30-test file with
    # the same proportion of smells score the same.
    by_type: dict[str, int] = {}
    penalty = 0.0
    for f in findings:
        smell = f["smell_type"]
        by_type[smell] = by_type.get(smell, 0) + 1
        penalty += _SMELL_WEIGHTS.get(smell, 0.5)

    if tests:
        smell_score = max(0.0, 1.0 - (penalty / len(tests)))
    else:
        # No tests at all is not "clean" — there is nothing to be confident in.
        smell_score = 0.0

    # ── Verdict ───────────────────────────────────────────────────────────
    if not tests:
        verdict = "No test functions found in file"
    elif not findings:
        verdict = f"{len(tests)} tests scanned, no smells detected"
    else:
        breakdown = ", ".join(f"{n}× {t}" for t, n in sorted(by_type.items()))
        verdict = (
            f"{len(findings)} smell(s) in {len(tests)} tests ({breakdown}) "
            f"— warning only, pipeline continues"
        )

    return ToolResult(
        tool="detect_smells",
        ok=True,
        score=round(smell_score, 4),
        proceed=True,          # smells NEVER gate — AGENTS.md §3
        verdict=verdict,
        details={
            "findings": findings,
            "tests_scanned": len(tests),
            "smell_score": round(smell_score, 4),
            "by_type": by_type,
        },
        artifacts=[],
        duration_ms=_elapsed_ms(),
        run_id=run_id,
    )
