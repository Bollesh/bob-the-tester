# Bob the Tester

**AI writes your tests; Bob the Tester proves they'd catch real bugs.**

Bob the Tester is an AI-powered test generation and QA framework built for the IBM SkillsBuild Hackathon. It pairs an autonomous test-writing agent (Bob) with an MCP server that refuses to accept a test on faith: every generated test must survive a staged quality pipeline — execution, coverage gain, smell detection, flakiness, property-based counterexamples, fuzzing, and mutation testing — before it is kept.

The premise is that coverage alone is a weak signal. A generated suite can hit 95% of lines and still assert nothing meaningful. Bob the Tester measures whether the tests would actually fail when the code is wrong, and feeds surviving mutants back to the agent as targeted regeneration work.

## How it works

Bob drives the loop; the MCP server owns every decision.

1. **Baseline** — `get_coverage` on the target module, `list_uncovered` for a structured gap map.
2. **Edge-case map** — Bob produces a structured plan (happy / boundary / negative / error paths) before writing any test code.
3. **Generate & validate** — each candidate test goes through `validate_and_keep`, which inserts it, runs the suite, requires a strict coverage increase, and rolls back cleanly on failure.
4. **Quality pipeline** — `detect_smells`, `run_flaky_check`, `run_property_tests` (Hypothesis), `run_fuzz` (Atheris), `mutation_test` (mutmut), sequenced by the server's `proceed` signal.
5. **Kill the survivors** — surviving mutants come back with the real file line, the original and mutated source, and a `status` saying *which* fix applies: `survived` means a test ran the line and missed the change (strengthen the assertion), `no tests` means nothing executed it at all (write a new test).
6. **Report** — remaining gaps are narrated from `list_uncovered`; results land in SQLite and are rendered as a static HTML dashboard (P4).

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

Nine tools are live on the server today. Optional engines degrade rather than break: a missing Hypothesis, Atheris, or mutmut returns `ok: false` with `proceed: true` and `engine_available: false`, so the rest of the pipeline still runs.

| Tool | Gates? | Key `details` |
| --- | --- | --- |
| `run_tests` | ✅ fails/errors | `passed`, `failed`, `errors`, `junit_summary` |
| `get_coverage` | — | `percent`, `covered_lines`, `uncovered_ranges` |
| `list_uncovered` | — | `gaps[]` with enclosing function names |
| `validate_and_keep` | ✅ suite fail / no coverage gain | `kept`, `reason`, `coverage_before/after` |
| `detect_smells` | — | `findings[]`, `by_type`, `smell_score` |
| `run_flaky_check` | ✅ proven flakiness | `flaky[]`, `outcomes`, `consistently_failing` |
| `run_property_tests` | — | `failures[]` with shrunk counterexamples |
| `run_fuzz` | — | `crashes[]` with readable input paths |
| `mutation_test` | — | `mutation_score`, `survived[]` with `status` |

`run_flaky_check` **reports** flaky tests; it does not delete them. Removing them is a skill step.

Not yet registered (P4): `explain_gaps`, `store_explanation`, `save_test_record`. Calling them returns "Unknown tool".

## Repository layout

```
server/           MCP server package (STDIO transport, official `mcp` SDK)
  schema.py       Shared ToolResult model — frozen contract
  core/           run_tests, coverage, validate_and_keep
  pipeline/       smells, flaky, properties, fuzz, mutation
  data/           SQLite models, logging middleware, replay cache
dashboard/        Static HTML report — coverage trend, mutation score, kept/discarded, bugs
sample_repo/      Demo target with documented seeded bugs (see BUGS.md)
scripts/          smoke.py (exercises every tool without Bob), reset_demo.py
bob_sessions/     Exported Bob task reports — append-only evidence
docs/             Architecture, demo script, benchmarks
cli/              Thin wrapper over BobShell — added only after demo freeze
```

`.bob/` holds the agent side: MCP registration, the QA Engineer custom mode, house test-style rules, and the `generate-and-cover` skill that sequences the pipeline above.

## Getting started

Requires Python 3.11+.

```bash
pip install -e .              # server core
pip install -e ".[dev]"       # + pipeline tools and dashboard
cp .env.example .env          # CONTEXT7_API_KEY, BOB_THE_TESTER_REPLAY, BOBSHELL_API_KEY
```

Two dependency bounds are deliberate and load-bearing:

- **`mcp>=1.0,<2`** — mcp 2.x removed the `@app.list_tools()` / `@app.call_tool()` decorator API that `server/main.py` is built on. It is gone from `mcp.server.lowlevel` too, not merely moved, so 2.x fails at import.
- **`mutmut>=3.0`** — `mutation_test` uses the 3.x config keys (`source_paths`, `only_mutate`) and the 3.x results CLI. 2.x differs on both; the tool version-guards and refuses rather than misparsing.

Verify the server end to end without involving the agent:

```bash
python scripts/smoke.py
```

The smoke script is the integration safety net — 84 checks across all nine tools against `sample_repo/`, expected to pass on `main` at all times. Stages whose engine is missing assert *clean degradation* instead of failing, so a machine without the `[pipeline]` extra still gets a green run.

```bash
bob-the-tester-server            # run the MCP server (STDIO)
python -m dashboard.build --open # render the run report to dashboard/dist/index.html
python -m dashboard.serve        # …or serve it with auto-refresh while a run is in progress
python scripts/reset_demo.py     # restore sample_repo/tests to pristine state
```

Setting `BOB_THE_TESTER_REPLAY=1` will make every tool return cached results keyed on `(tool, args-hash)` — fast, deterministic reruns for demos and rehearsals. The gate is P4's work and is not implemented yet; the variable is currently inert.

### Running it through Bob

`.bob/mcp.json` registers the server at workspace scope — `bob mcp list` should show `bob-the-tester`. The server is launched as `python3 -m server.main`, not `python server/main.py`: running the file as a script puts `server/` on `sys.path` instead of the repo root, which breaks `from server.schema import ToolResult`.

```bash
bob chat --mode qa-engineer --trust          # interactive; uses your Bob login
bob run  --mode qa-engineer --trust \
         --format json "…"                    # headless; needs BOB_API_KEY
```

Headless `bob run` requires a `BOB_API_KEY`; the interactive `bob chat` uses the credentials from your Bob login. The MCP server itself needs no Bob credential — it is a local STDIO subprocess, and `scripts/smoke.py` exercises every tool without Bob at all.

Each meaningful session is exported to `bob_sessions/` (append-only; see the README there).

## Development

Work is split five ways, and **ownership follows directories** — each person owns whole folders rather than shared files, so merges stay painless.

| Track | Scope |
| --- | --- |
| P1 | MCP server core, coverage loop, smoke script |
| P2 | Quality pipeline tools — smells, flaky, properties, fuzz, mutation |
| P3 | Bob integration: `.bob/` mode, skill, rules, session evidence |
| P4 | SQLite layer, logging middleware, replay cache, dashboard |
| P5 | Sample repos, benchmarks, docs, demo, CLI |

Two files are frozen contracts: `server/schema.py` and `server/data/models.py`. Changing either takes a PR tagged `contract-change` with full team approval. `server/main.py` is the one expected conflict point — it holds only tool-registration lines, so conflicts are resolved by keeping both sides; register in track order to keep diffs stable.

Nobody hand-edits `sample_repo/tests/`. Those tests come from Bob sessions, and `scripts/reset_demo.py` restores the directory.

Generated artifacts stay out of git — `bob-the-tester.db`, `coverage.xml`, `.mutmut-cache`, `__pycache__`, and fuzz corpora are all ignored. One blessed replay-cache snapshot is committed before the demo freeze so the replay path works on any machine.

## Background

The approach draws on published work in LLM-driven test generation: **TestGen-LLM** (assured refinement — never keep a test the tools rejected), **CoverUp** (coverage-guided iterative generation), and **MuTAP** (mutation-guided prompt repair, regenerating specifically to kill surviving mutants).

## Status

**Working end to end, through Bob.** The MCP server core (P1), the five quality-pipeline tools (P2), and the Bob integration (P3 — QA Engineer mode, `generate-and-cover` skill, session evidence) are merged on `main`. Bob loads the custom mode, connects over STDIO, and drives the tools; `bob_sessions/` holds the exports.

On the demo target, the baseline suite scores **3% mutation** (2 killed, 70 survived, all of them never executed) — coverage of `add`/`subtract` only. Adding real tests for `divide`, `multiply` and `is_prime` takes it to **40%**, and the survivors that remain are genuine weak-assertion traps: tests that catch `ZeroDivisionError` but never assert its message. That gap between "the tests ran" and "the tests would notice" is the whole pitch.

Still outstanding:

- **P4 has not started** — no SQLite layer, logging middleware, replay cache, or dashboard. `explain_gaps`, `store_explanation`, and `save_test_record` are referenced by the skill but are not registered tools yet.
- **P5's sample repo is a placeholder.** `calculator.py` stands in for the seeded-bug modules (`pricing.py`, `rounding.py`, `parser.py`, `api.py`) described in the plan, so the bug↔stage mapping in `BUGS.md` is not yet exercised.
- **Atheris is not installed** in the current environment, so `run_fuzz` reports `engine_available: false` by design. This is the pre-agreed first cut; Hypothesis still supplies machine-found counterexamples.
- **No Bobcoin consumption screenshot** in `bob_sessions/` — headless runs report cost as a number but produce no consumption UI to capture.

Interfaces are firming up, but `server/schema.py` remains the frozen contract: change it only through a `contract-change` PR.
