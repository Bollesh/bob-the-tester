#!/usr/bin/env bash
#
# mcp_server.sh — launch the MCP server with the project venv active,
# creating and populating that venv first if it does not exist.
#
# Registered as the `command` in .bob/mcp.json.  It locates the repo from
# its OWN path, not from the caller's working directory, so the same line
# works on every clone regardless of where the MCP client spawns it.
#
# STDOUT IS THE MCP WIRE.  A single stray byte on stdout corrupts the
# session, and pip is extremely chatty, so every diagnostic here goes to
# stderr and logs/bootstrap.log.  Nothing but the server itself may write
# to fd 1 — hence the `>&2` on every echo and `>>"$LOG" 2>&1` on pip.
#
# Override the venv location with BOB_THE_TESTER_VENV.
# Bash only; on Windows use the .venv\Scripts\ interpreter directly.
set -euo pipefail

# ── Locate the repo from this script's own path (symlink-safe) ──────────────
SOURCE=${BASH_SOURCE[0]}
while [ -L "$SOURCE" ]; do
    DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
    SOURCE=$(readlink "$SOURCE")
    [[ $SOURCE != /* ]] && SOURCE=$DIR/$SOURCE
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
REPO_ROOT=$(cd -P "$SCRIPT_DIR/.." && pwd)

VENV="${BOB_THE_TESTER_VENV:-$REPO_ROOT/.venv}"
SERVER="$VENV/bin/bob-the-tester-server"
LOG_DIR="$REPO_ROOT/logs"
LOG="$LOG_DIR/bootstrap.log"
mkdir -p "$LOG_DIR"

say() {
    echo "[mcp_server] $*" >&2
    echo "[mcp_server] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"
}

die() { say "FATAL: $*"; exit 1; }

# ── Bootstrap: create the venv and install the package if it is missing ─────
# Keyed on the console script rather than the directory: a venv that exists
# but was never `pip install -e`'d is the more common broken state, and it
# needs the install step just as much as a missing one does.
if [ ! -x "$SERVER" ]; then
    if [ ! -d "$VENV" ]; then
        # pyproject requires >=3.11; prefer the newest interpreter present.
        PY=""
        for candidate in python3.13 python3.12 python3.11 python3; do
            if command -v "$candidate" >/dev/null 2>&1; then
                if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
                    PY=$(command -v "$candidate")
                    break
                fi
            fi
        done
        [ -n "$PY" ] || die "no Python >=3.11 found on PATH; install one, then retry"

        say "no venv at $VENV — creating with $PY"
        "$PY" -m venv "$VENV" >>"$LOG" 2>&1 || die "venv creation failed; see $LOG"
    else
        say "venv exists but the server entry point is missing — installing"
    fi

    say "installing bob-the-tester[dev] (this takes a minute; log: $LOG)"
    "$VENV/bin/python" -m pip install --upgrade pip >>"$LOG" 2>&1 \
        || say "pip self-upgrade failed, continuing with the bundled pip"
    "$VENV/bin/python" -m pip install -e "$REPO_ROOT[dev]" >>"$LOG" 2>&1 \
        || die "pip install failed; see $LOG"

    [ -x "$SERVER" ] || die "install completed but $SERVER is still missing; see $LOG"
    say "bootstrap complete"
fi

# ── Activate ────────────────────────────────────────────────────────────────
# What `source .venv/bin/activate` does, minus the shell prompt cosmetics.
# run_tests and mutation already spawn subprocesses via sys.executable, so
# this is not what makes them work — it covers anything resolved by name.
export VIRTUAL_ENV="$VENV"
export PATH="$VENV/bin:$PATH"
unset PYTHONHOME

exec "$SERVER" "$@"
