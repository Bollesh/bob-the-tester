"""
replay.py — P4 tool-result cache and the BOB_THE_TESTER_REPLAY switch.

Why this exists (implementation plan §1 P4, §3 risk 4):
    The live demo must never die and must never burn Bobcoins on a
    rehearsal.  With BOB_THE_TESTER_REPLAY=1 every tool returns a cached
    result instantly — no pytest subprocess, no mutmut, no fuzzing.  A full
    pipeline replays in seconds, on any machine, offline.

How it works:
    Results are cached on disk as one JSON file per (tool, args) pair, under
    demo_replay/ at the repo root.  That directory is the one generated
    artifact the team DOES commit (AGENTS.md §7 invariant 10), so the
    blessed snapshot travels with the repo.

    Live runs write through: any successful tool call is recorded.  So the
    snapshot is produced by rehearsing the demo, not by hand-authoring
    fixtures — the cache can only ever contain results the real tools
    actually produced.

Cache key derivation — the two details that make this work across machines:

  1. `run_id` is excluded.  It is a fresh UUID per run, so including it
     would guarantee a permanent cache miss.
  2. Absolute paths are rewritten relative to the repo root.  Otherwise a
     snapshot recorded in D:/workspace/bob-the-tester would miss for every
     teammate and every CI checkout — the exact failure that makes a
     "works on any machine" replay claim untrue.

Cache-miss policy in replay mode:
    A miss falls through to real execution and records the result, rather
    than raising.  A missing entry mid-demo should cost latency, never the
    demo.  The call is logged with replayed=0, so the dashboard reports
    honestly what was cached and what was not.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from server.schema import ToolResult

logger = logging.getLogger("bob-the-tester.replay")

# server/data/replay.py → server/data → server → repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / "demo_replay"

# Arguments that must never contribute to the cache key.
_VOLATILE_ARGS = {"run_id", "run_start_ms"}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

def replay_enabled() -> bool:
    """True when BOB_THE_TESTER_REPLAY is set to a truthy value."""
    return os.environ.get("BOB_THE_TESTER_REPLAY", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def recording_enabled() -> bool:
    """
    Whether successful live results are written through to the cache.

    Defaults to on: rehearsing the demo IS how the snapshot gets built.
    Set BOB_THE_TESTER_RECORD=0 to run without touching demo_replay/.
    """
    return os.environ.get("BOB_THE_TESTER_RECORD", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def cache_dir() -> Path:
    override = os.environ.get("BOB_THE_TESTER_REPLAY_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_CACHE_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Key derivation
# ─────────────────────────────────────────────────────────────────────────────

def _portable(value: Any) -> Any:
    """
    Rewrite machine-specific absolute paths to repo-relative POSIX form.

    Applied recursively to argument values so the same call hashes
    identically on every checkout.  Strings that are not paths under the
    repo root pass through untouched.
    """
    if isinstance(value, str):
        if not value:
            return value
        candidate = value.replace("\\", "/")
        root = REPO_ROOT.as_posix()
        # Case-insensitive compare: Windows hands back drive letters in
        # either case, and a mismatch there would silently defeat the
        # whole portability rewrite.
        if candidate.lower().startswith(root.lower() + "/"):
            return candidate[len(root) + 1:]
        if candidate.lower() == root.lower():
            return "."
        return candidate
    if isinstance(value, dict):
        return {k: _portable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_portable(v) for v in value]
    return value


def normalise_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop volatile keys and make paths portable, ready for hashing."""
    return {
        k: _portable(v)
        for k, v in sorted(arguments.items())
        if k not in _VOLATILE_ARGS
    }


def cache_key(tool: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"tool": tool, "args": normalise_args(arguments)},
        sort_keys=True, default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return digest


def _cache_file(tool: str, key: str) -> Path:
    safe_tool = _SAFE_NAME.sub("_", tool) or "tool"
    return cache_dir() / f"{safe_tool}__{key}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Read / write
# ─────────────────────────────────────────────────────────────────────────────

def load(tool: str, arguments: dict[str, Any]) -> ToolResult | None:
    """Return the cached ToolResult for this call, or None on a miss."""
    path = _cache_file(tool, cache_key(tool, arguments))
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return ToolResult(**blob["result"])
    except Exception as exc:  # noqa: BLE001
        # A corrupt entry must degrade to a live run, not kill the call.
        logger.warning("Ignoring unreadable replay entry %s: %s", path.name, exc)
        return None


def record(tool: str, arguments: dict[str, Any], result: ToolResult) -> bool:
    """Write a result into the cache.  Returns True when it was stored."""
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        key = cache_key(tool, arguments)
        path = _cache_file(tool, key)
        blob = {
            "tool": tool,
            "key": key,
            # Stored for humans auditing the snapshot: it shows exactly which
            # call each file answers.  Never read back for matching.
            "normalised_args": normalise_args(arguments),
            "result": json.loads(result.model_dump_json()),
        }
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record replay entry for %s: %s", tool, exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# The dispatcher hook
# ─────────────────────────────────────────────────────────────────────────────

def replay_or_run(
    tool: str,
    arguments: dict[str, Any],
    fn: Callable[[], ToolResult],
) -> tuple[ToolResult, bool]:
    """
    Serve `tool` from cache when replaying, otherwise run it for real.

    Returns (result, replayed).  `replayed` is what the logging middleware
    stores in tool_calls.replayed, so the dashboard can distinguish a live
    run from a rehearsal instead of quietly presenting cached numbers as
    fresh ones.

    On a cache hit the result's `run_id` is rewritten to the CURRENT run's
    id.  Without that, replayed rows would land under the run_id that was
    recorded weeks ago and the dashboard would show an empty run.

    `duration_ms` is deliberately left at its recorded value: it is that
    tool's real measured cost, and the replayed=1 flag already tells the
    dashboard the wall clock was not spent again.
    """
    if replay_enabled():
        cached = load(tool, arguments)
        if cached is not None:
            current_run = arguments.get("run_id") or cached.run_id
            replayed_result = cached.model_copy(update={"run_id": current_run})
            logger.info("replay HIT  tool=%s", tool)
            return replayed_result, True
        logger.warning(
            "replay MISS tool=%s — falling through to live execution "
            "(the snapshot in %s has no entry for these arguments)",
            tool, cache_dir().name,
        )

    result = fn()

    if recording_enabled() and result.ok:
        record(tool, arguments, result)

    return result, False


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot inspection (used by scripts and the dashboard footer)
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_summary() -> dict[str, Any]:
    """Describe the cache on disk: entry count and tools covered."""
    directory = cache_dir()
    if not directory.exists():
        return {"dir": str(directory), "exists": False, "entries": 0, "tools": {}}
    tools: dict[str, int] = {}
    entries = 0
    for path in directory.glob("*.json"):
        entries += 1
        name = path.name.split("__", 1)[0]
        tools[name] = tools.get(name, 0) + 1
    return {
        "dir": str(directory),
        "exists": True,
        "entries": entries,
        "tools": dict(sorted(tools.items())),
    }
