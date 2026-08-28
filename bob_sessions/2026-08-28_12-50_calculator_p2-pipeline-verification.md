# Session Summary — P2 Quality Pipeline Verification via Bob

> **Session type:** first end-to-end verification that IBM Bob can drive the
> Bob the Tester MCP server. This is an integration check of the P1+P2+P3
> merge, not a full `generate-and-cover` pipeline run.
>
> **Raw export:** `2026-08-28_12-50_calculator_p2-pipeline-verification.json`
> (unedited `bob run --format json` output — the source of truth for every
> number below).

---

## Run metadata

| Field | Value |
|---|---|
| Date (UTC) | 2026-08-28T12:50:40Z |
| Bob Shell version | 2.0.1 |
| Mode | `qa-engineer` (`.bob/custom_modes.yaml`) |
| Invocation | `bob run --mode qa-engineer --trust --max-turns 20 --format json` |
| Task ID | `b73a0bf22ca8489ac15e3dbd94c92520` |
| Duration | 76.0 s |
| Cost | 0.128894 |
| Status | `success` |
| Target | `sample_repo/src/calculator.py`, `sample_repo/tests/test_placeholder.py` |

A preceding single-tool connectivity check ran as task
`7425e48be4299ef0f044f1282c75e854` (15.2 s, cost 0.054, `detect_smells`
only). Its terminal output was not captured in JSON format, so only its
task ID is recorded here.

---

## What this session establishes

1. **Bob loads the `qa-engineer` custom mode** from `.bob/custom_modes.yaml`.
2. **Bob connects to the `bob-the-tester` MCP server** over STDIO as
   configured in `.bob/mcp.json`, and the server advertises all 9 tools.
3. **Three P2 pipeline tools execute correctly through Bob** and return the
   frozen `ToolResult` envelope, with values identical to those produced by
   calling the tools directly in `scripts/smoke.py`.

---

## Results

| Stage | `ok` | `proceed` | `score` | Verdict |
|---|:---:|:---:|:---:|---|
| `detect_smells` | true | true | 1.00 | 3 tests scanned, no smells detected |
| `run_flaky_check` (n=3) | true | true | 1.00 | 3 tests stable across 3 runs, no flakiness |
| `mutation_test` | true | true | 0.028 | Mutation score 3% (2 killed, 70 survived) |

### Mutation detail — the headline metric

`src/calculator.py`, measured against the baseline `test_placeholder.py`:

- **mutation_score:** 0.0278 (≈3%)
- **killed:** 2
- **survived_count:** 70
- **not_covered:** 70 — every survivor has status `"no tests"`

All 70 survivors were never executed by any test. The only two mutants the
suite killed are in `add` and `subtract`, the sole functions the baseline
tests touch. `divide`, `multiply`, `power`, `factorial`, `is_prime` and
`gcd` have no coverage at all.

First three survivors as reported:

| Line | Function | Original | Mutated | Status |
|---|---|---|---|---|
| 41 | `divide` | `if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):` | `... and not isinstance(b, ...)` | `no tests` |
| 44 | `divide` | `raise ZeroDivisionError("Division by zero")` | `raise ZeroDivisionError("XXDivision by zeroXX")` | `no tests` |
| 44 | `divide` | `raise ZeroDivisionError("Division by zero")` | `raise ZeroDivisionError("division by zero")` | `no tests` |

Bob correctly used the per-survivor `status` field to conclude that these
need **new tests** rather than stronger assertions — the distinction the
tool exposes for the MuTAP regeneration step.

---

## Environment fixes required to reach this point

The merge did not run as committed. Four problems were fixed first:

1. `mcp` was not installed; the server could not start.
2. `pyproject.toml` pins `mcp>=1.0.0`, which resolves to mcp 2.1.1. mcp 2.x
   removed the `@app.list_tools()` / `@app.call_tool()` decorator API that
   `server/main.py` is written against. Pinned back to 1.29.1 locally —
   **`pyproject.toml` still needs `mcp>=1.0,<2`.**
3. `.bob/mcp.json` could not launch the server (wrong server name, stale
   `TESTFORGE_REPLAY`, and `python server/main.py` breaking the
   `server.schema` import). Fixed in commit `04f57e2`.
4. `hypothesis` and `mutmut` were absent; installed for the pipeline stages.

---

## Not covered by this session

- **No Bobcoin consumption screenshot.** The run was headless
  (`bob run`), which reports cost as a number (0.128894) but produces no
  consumption UI to capture. A screenshot from an interactive `bob chat`
  session is still outstanding for the judging evidence.
- **The full 13-stage `generate-and-cover` skill was not run.** Tools were
  invoked directly by instruction. Several stage contracts in `SKILL.md`
  do not match the implemented tools (smell-type strings, the claim that
  `run_flaky_check` removes flaky tests, `mutation_test` needing `cwd`, and
  calls to the not-yet-implemented P4 tools `explain_gaps`,
  `store_explanation`, `save_test_record`). Those need reconciling before
  an end-to-end skill run will complete.
- **`run_property_tests` and `run_fuzz` were not exercised through Bob.**
  Both pass in `scripts/smoke.py`; Atheris is not installed in this
  environment, so the fuzz stage degrades to `engine_available: false` by
  design.
