# Session Summary — Full Tool Sweep + Property Counterexample

> **Session type:** all-nine-tools sweep of the Bob the Tester MCP server driven
> by IBM Bob, run after the post-review fix pass. Two Bob tasks:
>
> | Raw export | Task |
> |---|---|
> | `2026-08-28_14-03_calculator_full-tool-sweep.json` | 8-tool sweep |
> | `2026-08-28_14-06_calculator_property-counterexample.json` | property stage, isolated |
>
> Both are unedited `bob run --format json` output and are the source of
> truth for every number below. The property fixture used by the second task
> is preserved verbatim as
> `2026-08-28_14-06_calculator_property-fixture.py`.

---

## Run metadata

| Field | Sweep | Property run |
|---|---|---|
| Timestamp (UTC) | 2026-08-28T14:03:50Z | 2026-08-28T14:06:51Z |
| Task ID | `f989bdc767a86d3a56917ab5b4fd2d8c` | `1807825ce455cbe08c134b97f142a2dc` |
| Duration | 157.8 s | 16.7 s |
| Cost | 0.494076 | 0.057200 |
| Tool calls | 10 | 1 |
| Status | `success` | `success` |

Bob Shell 2.0.1, mode `qa-engineer` (`.bob/custom_modes.yaml`), server
`bob-the-tester` over STDIO per `.bob/mcp.json`.

---

## Sweep results — all 8 requested calls

| # | Tool | `ok` | `proceed` | `score` | Key finding |
|---|---|:---:|:---:|---|---|
| 1 | `run_tests` | ✅ | ✅ | 1.00 | 3 passed, 0 failed |
| 2 | `get_coverage` | ✅ | ✅ | 0.2558 | 25.58% line coverage, 32 lines uncovered |
| 3 | `list_uncovered` | ✅ | ✅ | 1.00 | 6 gap regions with enclosing function names |
| 4 | `detect_smells` | ✅ | ✅ | 1.00 | 3 tests scanned, no smells |
| 5 | `run_flaky_check` (n=3) | ✅ | ✅ | 1.00 | 3 tests stable across 3 runs |
| 6 | `run_property_tests` | ✅ | ✅ | 1.00 | see caveat below — **not a valid property-stage result** |
| 7 | `run_fuzz` | ❌ | ✅ | 0.00 | clean degradation, `engine_available: false` |
| 8 | `mutation_test` | ✅ | ✅ | 0.0278 | 2 killed / 70 survived / 70 `no tests` |

`run_tests` was called twice (once plain, once with `--cov`), hence 10 tool
calls for 8 requested stages.

### Mutation detail

`src/calculator.py` against the baseline `test_placeholder.py`:
mutation_score **0.0278**, killed **2**, survived **70**, not_covered **70**.
Every survivor carries status `no tests` — nothing executed those lines.
Survivors by function: `is_prime` 29, `factorial` 20, `divide` 13, `power` 4,
`gcd` 3, `multiply` 1. The two killed mutants are in `add`/`subtract`, the
only functions the baseline suite touches.

### Degradation path (stage 7)

`run_fuzz` was pointed at a deliberately non-existent harness with Atheris
absent. It returned `ok: false` with `proceed: true`,
`error: "atheris_not_installed"`, `engine_available: false`, `crashes: []`.
This is the designed behaviour: a missing optional engine marks the stage
unavailable without stalling the pipeline (plan §3, risk 1).

---

## Caveat on stage 6 — Bob rewrote the fixture

**The sweep's property result does not validate the property stage.**

The fixture supplied to the sweep resolved the module under test through
`os.environ["CALC_SRC"]`. That variable is set in the operator's shell but
is **not inherited by the MCP server subprocess**, so the import failed.
Bob repaired the file rather than reporting the failure — a legitimate action
in `qa-engineer` mode, whose `fileRegex` permits editing anything matching
`tests/.*\.py$`, which the fixture did.

Bob's rewrite replaced the intentionally-false invariant
`factorial(n) > n` with a true one (`factorial(n) >= 1`), so the reported
"5 properties held, no falsifications" describes Bob's properties, not the
ones the run was designed to falsify. No counterexample was produced.

Two incidental findings from this:

1. The `qa-engineer` mode's `groups`/`fileRegex` restriction is live and
   parses correctly — an open question from the `open-code-review` pass,
   which had flagged the nested-list YAML structure as malformed.
2. Bob's rewrite added per-test `@settings(max_examples=150)` decorators.
   A per-test decorator **overrides** a loaded Hypothesis profile, so a
   caller's `max_examples` argument becomes advisory once Bob writes explicit
   settings. This is Hypothesis's documented precedence, not a tool defect.

### The isolated re-run (task `1807825c…`)

Stage 6 was re-run against a self-contained fixture (no environment
variables, relative `sys.path`) with an explicit instruction not to edit any
file. **The fixture was verified byte-identical before and after the run.**

| Field | Value |
|---|---|
| `ok` / `proceed` | `true` / `true` |
| `score` | 0.5 |
| `verdict` | "1/2 propert(ies) falsified at 200 examples: test_prop_factorial_strictly_exceeds_n — convert each shrunk_input into a regression test" |
| `details.passed` / `details.failed` | 1 / 1 |

Falsified property, verbatim from the export:

```
property    : test_prop_factorial_strictly_exceeds_n
shrunk_input: test_prop_factorial_strictly_exceeds_n(
                  n=1,
              )
exception   : AssertionError: assert 1 > 1
```

`n=1` is the true minimal counterexample: `factorial(1) == 1`, which is not
strictly greater than 1. Both n=1 and n=2 break the invariant and the
shrinker settled on the smaller. This is the structured repro data the
pipeline converts into a named regression test.

---

## What changed since the 12:50 session

That session ran against code where the smoke suite was red and the server
could not start from the committed config. Since then: the `run_tests`
path-splitting bug, the `mcp` version pin, the pytest `testpaths` target,
and the `.bob/mcp.json` launch command were all fixed, and the five findings
from an `open-code-review` pass over the whole tree were applied.
`scripts/smoke.py` now reports **84 checks passed, 0 failed** (1 warn:
Atheris absent by design).

---

## Still not covered

- **No Bobcoin consumption screenshot.** Both tasks were headless
  (`bob run`), which reports cost numerically (0.494076 and 0.057200) but
  renders no consumption UI to capture. An interactive `bob chat` session is
  still needed for that judging artefact.
- **The full 13-stage `generate-and-cover` skill has still not been run
  end to end.** Tools were invoked by direct instruction. `SKILL.md` still
  calls three P4 tools that are not registered — `explain_gaps`,
  `store_explanation`, `save_test_record` — which return "Unknown tool", so
  stages 9 and 13 cannot complete as written.
- **`run_fuzz` has never executed a real harness**, only its degradation
  path, because Atheris is not installed in this environment.
