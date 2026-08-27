# TestForge

**AI writes your tests; TestForge proves they'd catch real bugs.**

TestForge is an AI-powered test generation and QA framework built for the IBM SkillsBuild Hackathon. It pairs an autonomous test-writing agent (Bob) with an MCP server that refuses to accept a test on faith: every generated test must survive a staged quality pipeline — execution, coverage gain, smell detection, flakiness, property-based counterexamples, fuzzing, and mutation testing — before it is kept.

The premise is that coverage alone is a weak signal. A generated suite can hit 95% of lines and still assert nothing meaningful. TestForge measures whether the tests would actually fail when the code is wrong, and feeds surviving mutants back to the agent as targeted regeneration work.

## How it works

Bob drives the loop; the MCP server owns every decision.

1. **Baseline** — `get_coverage` on the target module, `list_uncovered` for a structured gap map.
2. **Edge-case map** — Bob produces a structured plan (happy / boundary / negative / error paths) before writing any test code.
3. **Generate & validate** — each candidate test goes through `validate_and_keep`, which inserts it, runs the suite, requires a strict coverage increase, and rolls back cleanly on failure.
4. **Quality pipeline** — `detect_smells`, `run_flaky_check`, `run_property_tests` (Hypothesis), `run_fuzz` (Atheris), `mutation_test` (mutmut), sequenced by the server's `proceed` signal.
5. **Kill the survivors** — surviving mutants come back with line and mutated-code detail, so regeneration targets real weaknesses instead of guessing.
6. **Report** — `explain_gaps` turns remaining uncovered lines into a prompt-ready structure; results land in SQLite and surface on a Streamlit dashboard.

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

## Repository layout

```
server/           MCP server package (STDIO transport, official `mcp` SDK)
  schema.py       Shared ToolResult model — frozen contract
  core/           run_tests, coverage, validate_and_keep
  pipeline/       smells, flaky, properties, fuzz, mutation
  data/           SQLite models, logging middleware, replay cache
dashboard/        Streamlit app — coverage trend, mutation gauge, kept/discarded, bugs
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
cp .env.example .env          # CONTEXT7_API_KEY, TESTFORGE_REPLAY, BOBSHELL_API_KEY
```

Verify the server end to end without involving the agent:

```bash
python scripts/smoke.py
```

The smoke script is the integration safety net — it exercises every registered tool against `sample_repo/` and is expected to pass on `main` at all times.

```bash
testforge-server                          # run the MCP server (STDIO)
streamlit run dashboard/app.py            # view results from the last run
python scripts/reset_demo.py              # restore sample_repo/tests to pristine state
```

Setting `TESTFORGE_REPLAY=1` makes every tool return cached results keyed on `(tool, args-hash)` — fast, deterministic reruns for demos and rehearsals.

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

Generated artifacts stay out of git — `testforge.db`, `coverage.xml`, `.mutmut-cache`, `__pycache__`, and fuzz corpora are all ignored. One blessed replay-cache snapshot is committed before the demo freeze so the replay path works on any machine.

## Background

The approach draws on published work in LLM-driven test generation: **TestGen-LLM** (assured refinement — never keep a test the tools rejected), **CoverUp** (coverage-guided iterative generation), and **MuTAP** (mutation-guided prompt repair, regenerating specifically to kill surviving mutants).

## Status

Early. The MCP server core and coverage loop are in place; the quality pipeline, data layer, dashboard, and demo assets are in progress. Interfaces — including the tool contract above — may still move.
