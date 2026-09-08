"""
build.py — render the dashboard to a single self-contained HTML file.

    python -m dashboard.build                 # → dashboard/dist/index.html
    python -m dashboard.build --open          # …and open it in a browser
    python -m dashboard.build -o report.html  # somewhere else

Why a file and not a server:  the report has to survive the demo.  A single
HTML file with the CSS, the JavaScript and the data inlined opens from a USB
stick, attaches to an email, and renders identically offline — no Python, no
port, no pip install on the judge's machine.

Reads bob-the-tester.db READ-ONLY (AGENTS.md §6).  Every run is fetched
through one connection, so the page is a single consistent snapshot rather
than panels that each queried at a slightly different moment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import webbrowser
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

# dashboard/build.py → dashboard → repo root.  Makes `server.data` importable
# when this is run as a script from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard import render  # noqa: E402
from server.data import db  # noqa: E402
from server.data.replay import replay_enabled, snapshot_summary  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "dist" / "index.html"

NO_DB_MESSAGE = (
    "No database yet at {path}. Run the pipeline first — through Bob, or with "
    "python scripts/smoke.py — then build this page again."
)
NO_RUNS_MESSAGE = (
    "The database at {path} exists but has no runs in it yet."
)


def collect(limit: int = 50, db_path: Path | None = None) -> list[dict[str, Any]]:
    """
    Fetch every run and all of its rows through one read-only connection.

    Raises FileNotFoundError when the database does not exist — the caller
    turns that into the "no runs yet" page rather than a traceback.
    """
    with closing(db.connect_ro(db_path)) as conn:
        runs = db.list_runs(limit=limit, conn=conn)
        return [{
            "run": run,
            "calls": db.tool_calls_for_run(run["run_id"], conn=conn),
            "coverage": db.coverage_for_run(run["run_id"], conn=conn),
            "tests": db.tests_for_run(run["run_id"], conn=conn),
            "mutation": db.mutation_for_run(run["run_id"], conn=conn),
            "bugs": db.bugs_for_run(run["run_id"], conn=conn),
            "gaps": db.gaps_for_run(run["run_id"], conn=conn),
            "usage": db.usage_for_run(run["run_id"], conn=conn),
        } for run in runs]


def fingerprint(bundles: list[dict[str, Any]]) -> str:
    """
    A cheap digest of what the page is showing.

    serve.py hands this to the browser so a running pipeline can trigger a
    reload the moment new rows land — without re-rendering the page on every
    poll.  Row counts plus the last row id per table are enough: nothing in
    this database is ever updated in place except the run row itself.
    """
    payload = [
        [
            b["run"]["run_id"],
            b["run"].get("status"),
            b["run"].get("finished_at"),
            b["run"].get("iterations_used"),
            *[len(b[k]) for k in ("calls", "coverage", "tests", "mutation",
                                  "bugs", "gaps")],
        ]
        for b in bundles
    ]
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build(limit: int = 50, db_path: Path | None = None) -> tuple[str, str]:
    """Render the whole report.  Returns (html, fingerprint)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = db_path or db.db_path()
    try:
        bundles = collect(limit=limit, db_path=db_path)
    except FileNotFoundError:
        return render.empty_page(NO_DB_MESSAGE.format(path=target), stamp), ""

    if not bundles:
        return render.empty_page(NO_RUNS_MESSAGE.format(path=target), stamp), ""

    snapshot = snapshot_summary()
    if replay_enabled():
        snapshot = dict(snapshot)
        snapshot["replay_enabled"] = True
    mark = fingerprint(bundles)
    return render.page(bundles, snapshot, mark, stamp), mark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the Bob the Tester dashboard to a static HTML file.")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                        help=f"output file (default: {DEFAULT_OUT})")
    parser.add_argument("--db", type=Path, default=None,
                        help="database to read (default: bob-the-tester.db)")
    parser.add_argument("--limit", type=int, default=50,
                        help="how many recent runs to include (default: 50)")
    parser.add_argument("--open", action="store_true",
                        help="open the report in the default browser")
    args = parser.parse_args(argv)

    html, _ = build(limit=args.limit, db_path=args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")

    size_kb = args.out.stat().st_size / 1024
    print(f"Wrote {args.out} ({size_kb:.0f} KB)")
    if args.open:
        webbrowser.open(args.out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
