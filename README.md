# Bob the Tester

**AI writes your tests; Bob the Tester proves they'd catch real bugs.**

Bob the Tester is an AI-powered test generation and QA framework built for the IBM SkillsBuild Hackathon. It pairs an autonomous test-writing agent (Bob) with an MCP server that refuses to accept a test on faith: every generated test must survive a staged quality pipeline — execution, coverage gain, smell detection, flakiness, property-based counterexamples, fuzzing, and mutation testing — before it is kept.

The premise is that coverage alone is a weak signal. A generated suite can hit 95% of lines and still assert nothing meaningful. Bob the Tester measures whether the tests would actually fail when the code is wrong, and feeds surviving mutants back to the agent as targeted regeneration work.

## How it works

Bob drives the loop; the MCP server owns every decision.

1. **Open the run** — `start_run` records the targets and returns the `run_id` that every later call carries.
2. **Baseline** — `get_coverage` on the target module, `list_uncovered` for a structured gap map.
3. **Edge-case map** — Bob produces a structured plan (happy / boundary / negative / error paths) before writing any test code.
4. **Generate & validate** — each candidate goes through `validate_and_keep`, which inserts it, runs the suite, requires a strict coverage increase, and rolls back cleanly on failure.
5. **Quality pipeline** — `detect_smells`, `run_flaky_check`, `run_property_tests` (Hypothesis), `run_fuzz` (Atheris), `mutation_test` (mutmut), sequenced by the server's `proceed` signal.
6. **Kill the survivors** — surviving mutants come back with the real file line, the original and mutated source, and a `status` saying *which* fix applies: `survived` means a test ran the line and missed the change (strengthen the assertion), `no tests` means nothing executed it at all (write a new test).
7. **Report and close** — `explain_gaps` supplies the structure, Bob writes the narration, `store_explanation` persists it, and `finish_run` stamps the result.

A test that fails against a known-seeded bug is reported as a **defect found**, not discarded as a bad test — the distinction is the point of the project.

### The tool contract

Every MCP tool returns the same JSON shape. Bob never parses free text; all sequencing comes from `ok` / `proceed` / `score`.

```json
{
  "tool": "run_tests",
  "ok": true,
  "score": 0.92,
  "proceed": true,
  "verdict": "12 passed, 0 failed",
  "details": { "...tool-specific..." },
  "artifacts": ["path/to/report.xml"],
  "duration_ms": 4210,
  "run_id": "uuid"
}
```

`proceed` is decided by the server, not by prompt text. Hard-fail is reserved for build errors, test failures, and proven flakiness; everything else lowers `score` and passes with a warning. Every call is logged with its `run_id`, inputs, and outputs.

### Registered tools

Fourteen tools are live on the server. Optional engines degrade rather than break: a missing Hypothesis, Atheris, or mutmut returns `engine_available: false` with `proceed: true`, so the rest of the pipeline still runs.

| Tool | Gates? | Key `details` |
| --- | --- | --- |
| `start_run` | — | `run_id` to thread through the run, normalised targets |
| `run_tests` | ✅ fails/errors | `passed`, `failed`, `errors`, `junit_summary` |
| `get_coverage` | — | `percent`, `covered_lines`, `uncovered_ranges` |
| `list_uncovered` | — | `gaps[]` with enclosing function names |
| `validate_and_keep` | ✅ suite fail / no coverage gain | `kept`, `reason`, `coverage_before/after` |
| `detect_smells` | — | `findings[]`, `by_type`, `smell_score` |
| `run_flaky_check` | ✅ proven flakiness | `flaky[]`, `outcomes`, `consistently_failing` |
| `run_property_tests` | — | `failures[]` with shrunk counterexamples |
| `run_fuzz` | — | `crashes[]` with readable input paths |
| `mutation_test` | — | `mutation_score`, `survived[]` with `status` |
| `explain_gaps` | — | `gaps[]` with source snippets and objective `signals` |
| `store_explanation` | — | Bob's gap narration, persisted for the dashboard |
| `save_test_record` | — | `bug_recorded` — the defect-vs-wrong-test decision |
| `finish_run` | — | `closed`, `duration_ms`, final `status` |

`run_flaky_check` **reports** flaky tests; it does not delete them. Removing them is a skill step.

**Paths and `cwd`.** Every tool that touches the filesystem takes paths relative to the project root you pass as `cwd`, and that root must be the one the target module, the test file and `tests_dir` all share. For the demo target that root is `sample_repo/`. Mixing conventions is the single most common way to get a run that looks healthy and measures nothing — see *Known issues*.

**A run has two ends.** `start_run` opens it, `finish_run` closes it, and Bob calls both. Nothing else closes a run, so one left open shows on the dashboard as `incomplete` rather than silently claiming to still be in progress.

**Cost is read, never reported.** The server never talks to a model, so it cannot measure Bobcoin or tokens — and a model asked to report its own token count is guessing. Instead `start_run` and `finish_run` each take a reading from Bob Shell's own task ledger (`~/.bob/db/bob.db`, override with `BOB_DB`) and store the difference in the `usage` table. Every figure is labelled with how it was attributed: `task-delta` (this run's own spend) or `task-totals` (an upper bound, when no baseline reading exists).

## What a run produces

Everything lands in SQLite (`bob-the-tester.db`, schema v2, eight tables) and is rendered by a static HTML dashboard (`python -m dashboard.build`; see *Usage*).

The build inlines its own CSS, script and data, so the report opens from a USB stick with no Python, no port and no install on the reader's machine. It shows the KPI strip (status, duration, coverage, mutation, tests kept, defects, Bobcoin, tokens, tool calls, tool time), the coverage trend against target, the mutation score with its MuTAP before/after delta, per-stage traffic lights, defects with their evidence, every kept and discarded test with the reason, Bob's gap narration, and the full tool-call log with raw payloads.

The dashboard reads the database **read-only**, so refreshing it can never disturb a run in progress.

## Repository layout

```
server/           MCP server package (STDIO transport, official `mcp` SDK)
  schema.py       Shared ToolResult model — frozen contract
  core/           run_tests, coverage, validate_and_keep
  pipeline/       smells, flaky, properties, fuzz, mutation
  data/           SQLite models + writers, logging middleware, replay cache,
                  run lifecycle, Bobcoin/token ledger reader
  gaps.py         explain_gaps, store_explanation, save_test_record
dashboard/        Static HTML report — build.py, serve.py, render.py, static/
sample_repo/      Demo target — `calculator.py`, the fixture smoke.py runs against
scripts/          smoke.py (exercises every tool without Bob), reset_demo.py,
                  mcp_server.sh (launcher — activates or bootstraps the venv)
demo_replay/      Recorded tool results for offline replay (generated, gitignored)
bob_sessions/     Exported Bob task reports — append-only evidence
logs/             MCP server log (generated, gitignored)
docs/             Architecture, demo script, benchmarks — empty so far
cli/              Thin wrapper over BobShell — added only after demo freeze
```

`.bob/` holds the agent side: MCP registration, the QA Engineer custom mode, house test-style rules, and the `generate-and-cover` skill that sequences the pipeline above.

## Installation

Requires **Python 3.11+** (developed and verified on 3.12.7). Node is needed only if you also want the Context7 MCP server registered alongside this one.

### 1. Clone and create a virtualenv

```bash
git clone <your-fork-or-clone-url> bob-the-tester
cd bob-the-tester
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

Install into a virtualenv and use *that* interpreter everywhere. A system Python without `mcp`, `hypothesis` or `mutmut` produces a server that either fails to start or silently degrades half the pipeline. Steps 1 and 2 are also what `scripts/mcp_server.sh` does for you on first launch, so on a fresh clone you can skip straight to *Register the server* and let the launcher build the venv.

### 2. Install the package

```bash
pip install -e ".[dev]"            # server core + every pipeline engine
```

| Install | What you get | When to use it |
| --- | --- | --- |
| `pip install -e .` | `mcp`, `pydantic`, `pytest`, `pytest-cov`, `lxml`, `defusedxml` | Core loop only: `run_tests`, coverage, `validate_and_keep` |
| `pip install -e ".[pipeline]"` | + `hypothesis`, `mutmut`, `pytest-rerunfailures` | Property tests, mutation testing, flaky checks |
| `pip install -e ".[dev]"` | Everything above | Normal setup |

Two engines sit outside the extras on purpose:

- **Atheris** (`run_fuzz`) is not declared as a dependency — it needs a matching Clang toolchain and fails to build on many machines. Install it separately with `pip install atheris` if you want the fuzz stage; without it the tool returns `engine_available: false` with `proceed: true` and the pipeline carries on.
- **The MCP SDK** arrives as a dependency of this package. If you ever need it standalone — to run `mcp` CLI helpers against the server — it is `pip install "mcp[cli]"` (or `uv add "mcp[cli]"`).

Two dependency bounds are deliberate and load-bearing:

- **`mcp>=1.0,<2`** — mcp 2.x removed the `@app.list_tools()` / `@app.call_tool()` decorator API that `server/main.py` is built on. It is gone from `mcp.server.lowlevel` too, not merely moved, so 2.x fails at import.
- **`mutmut>=3.0`** — `mutation_test` uses the 3.x config keys (`source_paths`, `only_mutate`) and the 3.x results CLI. 2.x differs on both; the tool version-guards and refuses rather than misparsing.

### 3. Create the environment file

```bash
cp .env.example .env
```

| Variable | What it does | Default |
| --- | --- | --- |
| `CONTEXT7_API_KEY` | Context7 MCP server (docs lookup); pinned `> 2.1.2` for CVE-2026-75130 | — |
| `BOBSHELL_API_KEY` | Headless `bob run`; unused by `bob chat` | — |
| `BOB_THE_TESTER_DB` | Where the SQLite database lives | `./bob-the-tester.db` |
| `BOB_THE_TESTER_REPLAY` | `1` serves cached tool results instead of executing | `0` |
| `BOB_THE_TESTER_RECORD` | Write successful live results into the snapshot | `1` |
| `BOB_THE_TESTER_REPLAY_DIR` | Where the snapshot lives | `./demo_replay` |
| `BOB_THE_TESTER_LOG` | Server log file | `./logs/server.log` |
| `BOB_DB` | Bob Shell's task ledger, read for cost | `~/.bob/db/bob.db` |

Nothing here is required to run the server itself — every `BOB_THE_TESTER_*` variable has a working default, and the server needs no model credential at all.

### 4. Register the server with your MCP client

A STDIO server is not a service you start and connect to: the host launches it as a child process per session and talks to it over stdin/stdout. So registration is a launch command, not a URL.

`.bob/mcp.json` registers it at workspace scope — `bob mcp list` should show `bob-the-tester`:

```json
{
  "mcpServers": {
    "bob-the-tester": {
      "command": "${workspaceFolder}/scripts/mcp_server.sh",
      "env": {
        "BOB_THE_TESTER_REPLAY": "0"
      }
    }
  }
}
```

That file holds no machine-specific paths, so it works on any clone as committed. `command` points at `scripts/mcp_server.sh`, which activates the venv and launches the server. Four details make it work:

- **`${workspaceFolder}` is what keeps the file portable, and the launcher covers the rest.** Bob expands the variable to the checkout root; the script then locates the repo from its own `BASH_SOURCE`, so it does not care where it was spawned. Both halves are needed: Bob starts servers with `cwd` set to your home directory, not the workspace, so a relative `command` never resolves — and a client launched from an IDE or desktop icon carries no activated venv, so a bare console-script name on `PATH` never resolves either. Variable expansion is client-specific and is not part of the MCP spec; on a host that does not expand it, use the checkout's own path.
- **It bootstraps a missing venv.** If `.venv/bin/bob-the-tester-server` is absent, the script creates the venv with the newest Python ≥3.11 it can find and runs `pip install -e ".[dev]"` before starting. It keys on the entry point rather than the directory, so a venv that exists but was never installed into is repaired too. First launch then takes about a minute; the client may time the handshake out, so run `./scripts/mcp_server.sh` once by hand on a fresh clone and let it finish. Override the location with `BOB_THE_TESTER_VENV`.
- **Nothing but the protocol may write to stdout.** stdout *is* the wire; a stray `print()` in server code corrupts the session — and pip is far chattier than any `print()`. Every diagnostic in the launcher goes to stderr and `logs/bootstrap.log`, and pip's output is redirected there wholesale. The server itself logs to `logs/server.log` for the same reason.
- **No `PYTHONPATH` is needed.** The editable install puts an `__editable__.bob_the_tester-*.pth` finder in site-packages, so `import server` resolves from any working directory. Set it only if you are running from a checkout you never `pip install -e`'d — and note that launching as `python server/main.py` would put `server/` on `sys.path` instead of the repo root, breaking `from server.schema import ToolResult`. Use `-m server.main` if you go that route.

The launcher is bash. On Windows, point `command` at `.venv\Scripts\bob-the-tester-server` and create the venv yourself.

Other hosts take the same command in their own file — VS Code (1.99+, Copilot Agent mode) uses `.vscode/mcp.json` with `"type": "stdio"` on each entry; Claude Desktop uses `claude_desktop_config.json`. The shape is otherwise identical.

There is no restart command for an MCP server: the client spawns one per session. After changing server code or the registration, start a new task or reload the window, then confirm it came up — `logs/server.log` gains a `Logging to …` line and the host's MCP log reports `Session connected`.

### 5. Verify the install

```bash
python scripts/smoke.py            # every tool, no agent involved
python -m pytest                   # the demo target's baseline suite → 3 passed
```

The smoke script is the integration safety net — 20 sections and 150+ checks across all fourteen tools against `sample_repo/`, expected to pass on `main` at all times. Stages whose engine is missing assert *clean degradation* instead of failing, so a machine without the `[pipeline]` extra still gets a green run. (One check is currently red on `main`; see *Known issues*.)

Running `bob-the-tester-server` by hand is a valid smoke test too — it blocks with no output, waiting on stdin, which is exactly right for a STDIO server. Ctrl-C to exit.

### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Host shows no tools, nothing reaches `logs/server.log` | The launcher never ran — wrong path in `command`, or it isn't executable | Check `command` resolves; `chmod +x scripts/mcp_server.sh` |
| Host shows no tools, `logs/bootstrap.log` ends mid-install | First-launch bootstrap outran the client's handshake timeout | Run `./scripts/mcp_server.sh` by hand once, let it finish, restart the client |
| Server exits with `No module named 'server'` | The venv exists but the package was never installed into it | `pip install -e ".[dev]"`, or delete `.venv` and let the launcher rebuild it |
| Server exits at import with an `mcp` attribute error | mcp 2.x installed | `pip install "mcp>=1.0,<2"` |
| `engine_available: false` on a pipeline stage | That engine isn't in the active interpreter | `pip install -e ".[dev]"` in the venv on `PATH` |
| `mutation_test` returns a 0% score with `not checked` mutants | `cwd` and the target paths belong to different roots | Pass every path relative to one `cwd` — see *Paths and `cwd`* above |
| Server starts but the host reports a protocol error | Something wrote to stdout | Log to `logs/server.log`, never `print()` |

## Usage

### Drive a full run through Bob

```bash
bob chat --mode qa-engineer --trust          # interactive; uses your Bob login
bob run  --mode qa-engineer --trust \
         --format json "…"                    # headless; needs BOBSHELL_API_KEY
```

One instruction is enough — the `generate-and-cover` skill sequences the whole pipeline:

```
Raise coverage of sample_repo/src/calculator.py to 85% using the
generate-and-cover skill. Use cwd=sample_repo and paths relative to it.
```

Headless `bob run` requires the API key; interactive `bob chat` uses the credentials from your Bob login. The MCP server itself needs no Bob credential — it is a local STDIO subprocess, and `scripts/smoke.py` exercises every tool without Bob at all.

Each meaningful session is exported to `bob_sessions/` (append-only; see the README there).

### Read the results

```bash
python -m dashboard.build --open   # one self-contained file → dashboard/dist/index.html
python -m dashboard.serve          # …or serve it, auto-refreshing while a run is in progress
```

The database is `bob-the-tester.db` (schema v2, eight tables); the dashboard opens it read-only, so refreshing it can never disturb a run in progress.

### Reset between runs

```bash
python scripts/reset_demo.py             # pristine sample_repo/tests + fresh DB
python scripts/reset_demo.py --dry-run   # preview what it would delete
```

It restores `test_placeholder.py` to the committed baseline, deletes Bob-generated tests, and clears the database, coverage reports and caches. `demo_replay/` is left alone.

### Replay mode

`BOB_THE_TESTER_REPLAY=1` makes every tool return a cached result keyed on `(tool, normalised args)` — a full pipeline replays in seconds, offline, with no pytest or mutmut subprocess. Keys exclude `run_id` and rewrite absolute paths to repo-relative form, so a snapshot recorded on one machine hits on another.

Two things keep it honest. A cache **miss** falls through to live execution rather than failing, so a missing entry costs latency and never the demo; and calls that only write to the database (`start_run`, `finish_run`, `store_explanation`, `save_test_record`) are never cached, because replaying "run closed" while leaving the run open would be worse than not replaying at all. Every replayed call is logged with `replayed=1`, and the dashboard reports how many results were cached rather than executed.

## Development

Work is split five ways, and **ownership follows directories** — each person owns whole folders rather than shared files, so merges stay painless.

| Track | Scope |
| --- | --- |
| P1 | MCP server core, coverage loop, smoke script |
| P2 | Quality pipeline tools — smells, flaky, properties, fuzz, mutation |
| P3 | Bob integration: `.bob/` mode, skill, rules, session evidence |
| P4 | SQLite layer, logging middleware, replay cache, run lifecycle, dashboard |
| P5 | Sample repos, benchmarks, docs, demo, CLI |

Two files are frozen contracts: `server/schema.py` and `server/data/models.py`. Changing either takes a PR tagged `contract-change` with full team approval. `server/main.py` is the one expected conflict point — it holds only tool-registration lines, so conflicts are resolved by keeping both sides; register in track order to keep diffs stable.

Nobody hand-edits `sample_repo/tests/`. Those tests come from Bob sessions, and `scripts/reset_demo.py` restores the directory.

Generated artifacts stay out of git — `bob-the-tester.db`, `coverage.xml`, `.mutmut-cache`, `__pycache__`, `logs/`, `dashboard/dist/`, `demo_replay/` and fuzz corpora are all ignored. The replay snapshot is rebuilt by rehearsing the demo (`BOB_THE_TESTER_RECORD` is on by default), so it is reproducible rather than shipped — and a snapshot that travelled with the repo could serve a stale or half-failed stage as though it were fresh.

## Background

The approach draws on published work in LLM-driven test generation: **TestGen-LLM** (assured refinement — never keep a test the tools rejected), **CoverUp** (coverage-guided iterative generation), and **MuTAP** (mutation-guided prompt repair, regenerating specifically to kill surviving mutants).

## Status

**Working end to end, through Bob, with the whole run recorded.** P1 (server core), P2 (quality pipeline), P3 (Bob mode, skill, session evidence) and P4 (SQLite layer, logging middleware, replay cache, run lifecycle, dashboard) are merged. Bob loads the custom mode, connects over STDIO, drives the tools, and closes the run it opened.

Latest full run through the IDE (2026-09-08, `calculator.py`, one iteration, 12m 33s):

| | |
| --- | --- |
| Coverage | 25.58% → **97.67%** (target 85%) |
| Tests | 15 kept of 21 candidates; 6 discarded with reasons |
| Gates fired | 2 suite failures, 4 candidates adding no new lines |
| Smells / flakiness | 18 tests scanned, none found; stable across 5 runs |
| Properties | 26 held over 200 examples each |
| Tool calls | 34, all logged |
| Cost | 4.303 Bobcoin, 2,151,696 tokens |
| Mutation | **no signal** — see below |

### Known issues

- **The path convention is not pinned in the skill**, so Bob chose repo-root-relative paths with `cwd` at the repo root. `mutation_test` then generated 72 mutants and ran the suite against none of them (`not checked`), producing a 0% score that measures nothing. The project's headline metric is absent from the latest run for this reason alone.
- **Nothing re-measures coverage at the end of a run.** `get_coverage` runs once, at baseline, so `explain_gaps` narrates the *baseline* gaps and the dashboard's coverage trend has a single point — a run that finished at 97.67% reports 25.58%.
- **The fuzz harness ignores `-max_total_time`.** Both `run_fuzz` calls were killed (90s, 45s) with 0 execs, yet scored `1.0` — a stage that did no fuzzing presents as a clean pass.
- **Schema v2 needs its `contract-change` PR.** The `usage` table and the version bump are in `server/data/models.py`, which is frozen.
- **`smoke.py` fails one check** — `seeded crash was found`. Atheris is installed and runs; the seeded crash is not reaching it.
- **`sample_repo/` is still the placeholder fixture.** `calculator.py` stands in for the seeded-bug modules in the plan, so the bug↔stage mapping in its `BUGS.md` is not yet exercised. A worked demo target *does* now exist as a sibling checkout, `../sample_repo` — a checkout/pricing engine (money, parser, pricing, inventory, engine, api) with one bug seeded per pipeline stage and each mapped to its stage in `BUGS.md`. Retargeting `scripts/smoke.py` at it is the open work: the script hard-codes `sample_repo/src/calculator.py` in about 33 places, including inline candidate tests that `from calculator import divide` and gap assertions naming `factorial` / `is_prime` / `gcd`.
- **`docs/` and `cli/` are empty.**
