"""
run_fuzz(harness_path, seconds=60) — P2 pipeline stage 8.

Runs an Atheris (libFuzzer) harness under a wall-clock budget and returns
any crashing inputs as structured repro data.

Division of labour (AGENTS.md §1): Bob WRITES the harness — deciding what
to fuzz and how to decode the byte stream is judgement work.  This tool
only executes it and reports crashes.  P2 never generates harness code.

Details payload (AGENTS.md §5):
    crashes         list — [{input_repr, input_file, exception, stack_top}]
    execs_per_sec   int  — throughput reported by libFuzzer (0 if unknown)
    seconds         int  — time budget actually requested
    engine_available bool— False when Atheris is not installed
    total_execs     int  — units executed, when libFuzzer reported it

Gate policy (AGENTS.md §3 and server/main.py): proceed is ALWAYS True.  A
crash is a FINDING — Bob converts it into a named regression test, which
then re-enters validate_and_keep.  A crash must never stall the pipeline.

Risk note (implementation plan §3, risk 1): Atheris is the pre-agreed FIRST
CUT if it will not install — it needs a matching clang/libFuzzer toolchain
and frequently fails on stock environments.  This tool therefore degrades
cleanly: a missing engine returns ok=False with proceed=True and an
explicit engine_available=False, so the rest of the pipeline runs untouched
and the dashboard can show the stage as unavailable rather than broken.

Stage isolation (AGENTS.md §6): no imports from sibling pipeline modules.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from server.schema import ToolResult

# Grace period added to the libFuzzer budget before we hard-kill the process.
# libFuzzer needs a moment past -max_total_time to write artifacts and stats.
_TIMEOUT_GRACE_S = 30

_MIN_SECONDS = 1
_MAX_SECONDS = 3600

# How many bytes of a crashing input to inline in the result.  Full inputs
# stay on disk and are referenced by input_file.
_INPUT_PREVIEW_BYTES = 256

# libFuzzer / Atheris output patterns
_EXEC_PER_SEC_RE = re.compile(r"exec/s:\s*(\d+)")
_TOTAL_EXECS_RE = re.compile(r"stat::number_of_executed_units:\s*(\d+)")
_ARTIFACT_RE = re.compile(r"Test unit written to (?P<path>\S+)")
_UNCAUGHT_RE = re.compile(
    r"Uncaught Python exception:\s*={0,3}\s*\n(?P<exc>[^\n]+)", re.MULTILINE
)
_TRACEBACK_LINE_RE = re.compile(r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+)')


# ─────────────────────────────────────────────────────────────────────────────
# Output parsing
# ─────────────────────────────────────────────────────────────────────────────

def _extract_exception(output: str) -> str:
    """Pull the uncaught exception line out of an Atheris crash report."""
    match = _UNCAUGHT_RE.search(output)
    if match:
        return match.group("exc").strip()[:300]

    # Fall back to the last exception-looking line in the output.
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):", stripped):
            return stripped[:300]

    return "unknown crash (no Python exception reported)"


def _extract_stack_top(output: str) -> str:
    """
    Return the deepest traceback frame that belongs to the code under test.

    Atheris frames are noise; the useful frame is the last one, which is
    where the crash actually happened.
    """
    frames = _TRACEBACK_LINE_RE.findall(output)
    if not frames:
        return ""
    file_path, line_no = frames[-1]
    return f"{file_path}:{line_no}"


def _read_input_preview(path: Path) -> str:
    """repr() of the first bytes of a crashing input file."""
    try:
        data = path.read_bytes()[:_INPUT_PREVIEW_BYTES]
        return repr(data)
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _last_int(pattern: re.Pattern[str], text: str) -> int:
    """Last integer match in the output, or 0 when absent."""
    matches = pattern.findall(text)
    return int(matches[-1]) if matches else 0


# ─────────────────────────────────────────────────────────────────────────────
# Public tool function
# ─────────────────────────────────────────────────────────────────────────────

def run_fuzz(
    harness_path: str,
    seconds: int = 60,
    cwd: str = "",
    run_id: str | None = None,
) -> ToolResult:
    """
    Execute an Atheris fuzz harness for a bounded time and report crashes.

    Args:
        harness_path: Absolute path to the Python harness Bob wrote.  It must
                      call atheris.Setup(sys.argv, TestOneInput) followed by
                      atheris.Fuzz().
        seconds:      Wall-clock fuzzing budget (clamped to 1–3600).
        cwd:          Working directory for the harness.  Defaults to the
                      harness's own directory.
        run_id:       Optional UUID shared across one pipeline run.

    Returns:
        ToolResult with proceed=True always; crashes are findings.
    """
    if run_id is None:
        run_id = str(uuid.uuid4())

    t0 = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return (time.monotonic_ns() - t0) // 1_000_000

    def _unavailable(verdict: str, error: str) -> ToolResult:
        """Engine or input problem: ok=False, but never gate the pipeline."""
        return ToolResult(
            tool="run_fuzz",
            ok=False,
            score=0.0,
            proceed=True,
            verdict=verdict,
            details={
                "error": error,
                "engine_available": importlib.util.find_spec("atheris") is not None,
                "crashes": [],
                "execs_per_sec": 0,
                "seconds": seconds,
            },
            artifacts=[],
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )

    seconds = max(_MIN_SECONDS, min(_MAX_SECONDS, int(seconds)))

    if importlib.util.find_spec("atheris") is None:
        return _unavailable(
            "Atheris is not installed — fuzz stage skipped. "
            "This is the pre-agreed first cut (plan §3 risk 1); Hypothesis "
            "still provides machine-found counterexamples.",
            "atheris_not_installed",
        )

    harness = Path(harness_path)
    if not harness.is_file():
        return _unavailable(
            f"Fuzz harness not found: {harness_path}",
            "harness_not_found",
        )

    harness = harness.resolve()
    cwd = str(Path(cwd).resolve()) if cwd else str(harness.parent)

    # ── Run under a time budget ───────────────────────────────────────────
    # artifact_prefix keeps crash-* files out of the repo; libFuzzer requires
    # the trailing separator.
    with tempfile.TemporaryDirectory(prefix="bob_fuzz_") as artifact_dir:
        prefix = artifact_dir.rstrip("/") + "/"

        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(harness),
                    f"-max_total_time={seconds}",
                    f"-artifact_prefix={prefix}",
                    "-print_final_stats=1",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=seconds + _TIMEOUT_GRACE_S,
            )
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            return_code = proc.returncode
            timed_out = False

        except subprocess.TimeoutExpired as exc:
            # The harness ignored -max_total_time.  Whatever it printed before
            # the kill is still worth parsing for crashes.
            output = "".join(
                part.decode("utf-8", "replace") if isinstance(part, bytes) else (part or "")
                for part in (exc.stdout, exc.stderr)
            )
            return_code = -1
            timed_out = True

        except OSError as exc:
            return _unavailable(f"Could not start the harness: {exc}", str(exc))

        # ── Collect crashes ───────────────────────────────────────────────
        crashes: list[dict[str, Any]] = []

        artifact_paths = [Path(p) for p in _ARTIFACT_RE.findall(output)]
        # libFuzzer also leaves artifacts behind without always naming them.
        for extra in sorted(Path(artifact_dir).glob("*")):
            if extra not in artifact_paths:
                artifact_paths.append(extra)

        exception = _extract_exception(output)
        stack_top = _extract_stack_top(output)
        crashed = "Uncaught Python exception" in output or bool(artifact_paths)

        if artifact_paths:
            for art in artifact_paths:
                crashes.append({
                    "input_repr": _read_input_preview(art),
                    "input_file": art.name,
                    "exception": exception,
                    "stack_top": stack_top,
                })
        elif crashed:
            # Crash reported but no artifact written (e.g. -runs mode).
            crashes.append({
                "input_repr": None,
                "input_file": None,
                "exception": exception,
                "stack_top": stack_top,
            })

        execs_per_sec = _last_int(_EXEC_PER_SEC_RE, output)
        total_execs = _last_int(_TOTAL_EXECS_RE, output)

        # ── Score and verdict ─────────────────────────────────────────────
        # No crash in the budget is the good outcome and scores 1.0; each
        # crash is a real finding that lowers the stage score.
        score = 0.0 if crashes else 1.0

        if crashes:
            verdict = (
                f"{len(crashes)} crash(es) found in {seconds}s "
                f"({execs_per_sec} exec/s): {exception} "
                f"— convert each into a named regression test"
            )
        elif timed_out:
            verdict = (
                f"Harness did not honour -max_total_time and was killed after "
                f"{seconds + _TIMEOUT_GRACE_S}s; no crashes seen. "
                f"Check that the harness calls atheris.Fuzz()."
            )
        else:
            verdict = (
                f"No crashes in {seconds}s "
                f"({total_execs or 'unknown'} execs, {execs_per_sec} exec/s)"
            )

        return ToolResult(
            tool="run_fuzz",
            ok=True,
            score=score,
            proceed=True,      # crashes are findings — AGENTS.md §3
            verdict=verdict,
            details={
                "crashes": crashes,
                "execs_per_sec": execs_per_sec,
                "total_execs": total_execs,
                "seconds": seconds,
                "engine_available": True,
                "return_code": return_code,
                "timed_out": timed_out,
                "stdout_tail": "\n".join(output.splitlines()[-40:]),
            },
            artifacts=[],   # crash files live in a temp dir that is now gone
            duration_ms=_elapsed_ms(),
            run_id=run_id,
        )
