"""
serve.py — serve the dashboard over HTTP with auto-refresh.

    python -m dashboard.serve            # http://127.0.0.1:8501, opens a browser
    python -m dashboard.serve --port 9000 --no-browser

Use this while a pipeline run is in progress: the page polls
/api/fingerprint every few seconds and reloads itself the moment new rows
land, so the demo screen stays current without anyone touching it.  For a
report to hand over afterwards, use `python -m dashboard.build` instead —
that produces a single file with no server behind it.

Standard library only (http.server), read-only database access, and bound
to loopback by default: nothing here is exposed to the network.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# dashboard/serve.py → dashboard → repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard import build as builder  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    server_version = "BobTheTester"

    # Set by serve() before the server starts.
    db_path: Path | None = None
    limit: int = 50

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is rebuilt per request; a cached copy would silently show
        # a stale run, which is worse than a slightly slower reload.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (http.server's API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/index.html"):
            html, _ = builder.build(limit=self.limit, db_path=self.db_path)
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/fingerprint":
            try:
                bundles = builder.collect(limit=self.limit, db_path=self.db_path)
                mark = builder.fingerprint(bundles)
            except FileNotFoundError:
                # No database yet is a normal state during a demo setup, not
                # an error the poller should choke on.
                mark = ""
            self._send(json.dumps({"fingerprint": mark}).encode("utf-8"),
                       "application/json")
            return

        self._send(b"Not found", "text/plain; charset=utf-8", status=404)

    def log_message(self, fmt: str, *args) -> None:
        """Quieter than the default: one line per page load, no asset noise."""
        if "/api/" not in self.path:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(port: int = 8501, host: str = "127.0.0.1", db_path: Path | None = None,
          limit: int = 50, open_browser: bool = True) -> None:
    Handler.db_path = db_path
    Handler.limit = limit

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"Bob the Tester dashboard: {url}  (Ctrl-C to stop)")

    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the Bob the Tester dashboard with auto-refresh.")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", type=Path, default=None,
                        help="database to read (default: bob-the-tester.db)")
    parser.add_argument("--limit", type=int, default=50,
                        help="how many recent runs to include (default: 50)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    serve(port=args.port, host=args.host, db_path=args.db, limit=args.limit,
          open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
