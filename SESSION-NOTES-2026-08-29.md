# Session Notes — 2026-08-29

**Goal of the day:** get IBM Bob actually driving the TestForge MCP server end-to-end,
capture the mandatory Bobcoin-consumption evidence, and verify P4 (data layer +
dashboard) against real data rather than fixtures.

Everything below was verified against the **installed Bob 2.0.3 extension bundle**
(`bob-code`), not inferred from docs. Where something is unverified, it says so.

---

## TL;DR

- Bob now runs the `generate-and-cover` skill in QA Engineer mode against the real MCP server.
- Found and fixed a config bug that silently stripped shell access from the QA Engineer mode.
- Found three environment blockers; two fixed, one is a hard platform limitation (documented below).
- First real Bob session captured — Bobcoin spend is on the record.

---

## 1. Environment verified

| Thing | Result |
|---|---|
| Bob IDE | 2.0.3 (`ibm.bob-code`), Electron/VS Code fork |
| TestForge MCP server | starts over STDIO, **all 12 tools register** (server v1.29.1) |
| Python | 3.12.10 — `python3` resolves correctly despite the WindowsApps stub |
| `scripts/smoke.py` | passes except the mutation stage (see §3.3) |
| Config paths | `.bob/custom_modes.yaml`, `.bob/skills/`, `.bob/rules-qa-engineer/` all correct |

Confirmed from Bob's own shipped schema docs: workspace-scope config lives at
`.bob/<file>` with **no** `settings/` subdirectory, while global scope is
`~/.bob/settings/<file>`. The two are not symmetric. Our layout was already right.

---

## 2. Fix applied — `.bob/custom_modes.yaml`

The QA Engineer mode declared `- command` in its `groups` list.

**Bob's only valid group names are:** `read`, `edit`, `execute`, `mcp`, `skill`,
`todo`, `subagent`, `mode`.

Group names are free-form strings to Bob's schema, so `command` is **not** a
validation error. The file loads fine, the mode appears in the picker, and that
line simply **matches no tool and grants nothing** — with no warning anywhere.
Net effect: the QA Engineer mode had zero shell access.

Changed to `execute`, and added `todo` so the staged pipeline renders as a visible
checklist during runs (confirmed working — the task showed a `0/14` counter).

The stale "assumptions" header comment was replaced with the verified schema, so
nobody re-introduces the bug.

**Lesson for the team:** if a mode seems to be missing a capability, check the
group name against that list first. It fails silently by design.

---

## 3. Blockers found

### 3.1 Workspace Trust — FIXED

Bob would not open its chat panel and `Bob: New Task` did nothing. Cause: the
workspace was in **Restricted Mode**. The `bob-code` extension declares no
`capabilities.untrustedWorkspaces`, so VS Code disables it entirely in an
untrusted workspace — the `bob-chat` view container is never registered.

Fix: trust the workspace (status bar → `Restricted Mode` → Trust).

This was mandatory regardless of the UI, since our MCP server spawns
`python -m server.main` as a subprocess, which Restricted Mode also blocks.

### 3.2 Context7 API key — OPEN, needs a human

**Bob cannot expand `${VAR}` references in `mcp.json`.** Values are stored and
sent literally. Our `.bob/mcp.json` has:

```json
"env": { "CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}" }
```

Context7 therefore receives the literal string `${CONTEXT7_API_KEY}` as its key.

**Do not fix this by pasting the key into `.bob/mcp.json`** — that file is
git-tracked and the key would be committed in plaintext. Register Context7 in the
**global** file instead, which lives outside the repo:

```
~/.bob/settings/mcp.json
```

...and drop the `context7` entry from the workspace file.

### 3.3 mutmut does not run on Windows — HARD BLOCKER

This is the one smoke-script failure, and it is **not** a bug in
`server/pipeline/mutation.py`. mutmut 3.x refuses to run natively on Windows:

```
To run mutmut on Windows, please use the WSL.
Native windows support is tracked in issue https://github.com/boxed/mutmut/issues/397
```

Result: `total: 0`, `ok: false`, `proceed: true`. The tool degrades correctly
rather than crashing, so the pipeline continues — but `mutation_results` stays
empty on Windows, and the mutation-score demo moment does not work here.

Options, in order of preference:

1. **Shell the mutation stage into WSL** (Ubuntu is already installed on the dev box).
2. **Pre-compute on Linux/CI and serve from the replay cache** — this is the plan's
   own pre-agreed mitigation (§3 risk 2). Note that `demo_replay/` currently holds
   5 cached entries and **no `mutation_test` entry**, so this still needs doing.
3. Run the demo from a Linux machine.

`mutation_test` is on the "never cut" list, so this needs an owner (P2).

### 3.4 Atheris / fuzzing — expected degradation

`atheris` is not installed and has no Windows wheel. `run_fuzz` reports
`engine_available: false` with `proceed: true`. This is risk-register cut #1 in the
plan and needs no action.

---

## 4. Dependencies installed

`hypothesis` and `mutmut` were missing from the dev environment — stages 7 and 10
would have died mid-run. Installed the `pipeline` extra:

```
hypothesis 6.165.10
mutmut 3.7.0
pytest-rerunfailures
```

If you are setting up fresh: `pip install -e ".[dev]"`.

---

## 5. First Bob run — evidence

One instruction, QA Engineer mode:

```
Raise coverage of sample_repo/src/calculator.py to 85% using the generate-and-cover skill.
```

Observed working end-to-end:

- Mode picker resolved **QA Engineer** from `.bob/custom_modes.yaml`
- The `generate-and-cover` skill loaded and produced its staged checklist
- MCP tool calls executed against `bob-the-tester` (visible in the transcript)
- Bobcoin spend visible live in the task header
- `bob-the-tester.db` populated by the logging middleware as the run progressed

**Where the Bobcoin counter is** (this took us a while to find): the **task header
bar** at the top of the chat panel, rendered as a compact pill with a coin icon,
immediately right of the `33.2k / 270.0k` context-length readout. It is not a
labelled field, which is why it is easy to miss.

Bob also self-corrected a failing `run_tests` call — the first `--cov` invocation
used the wrong source path, and it diagnosed and retried on its own. That is the
CoverUp-style failure-feedback loop working as designed.

Session export + consumption screenshot go in `bob_sessions/` per the naming
convention in that directory's README. **That directory is append-only.**

---

## 6. Notes for whoever verifies P4

The dashboard's definition of done is "after any Bob run, the dashboard shows the
complete story with zero manual steps." When checking that, know which empty tables
are real gaps and which are expected:

| Table | Expected after a full run on Windows |
|---|---|
| `runs`, `tool_calls`, `coverage_history` | populated |
| `tests` | populated (kept + discarded, with reasons) |
| `gap_explanations` | populated at the final stage |
| `bugs_found` | may be empty — `calculator.py` has no seeded bugs yet (P5) |
| `mutation_results` | **always empty on Windows** — see §3.3, not a P4 defect |

Also note: pyarrow is blocked on this machine, so Streamlit's dataframe and chart
widgets fail. The dashboard uses the `_render` fallback layer.

---

## 7. Still open

- [ ] Move the Context7 key to `~/.bob/settings/mcp.json`, drop it from the workspace file (§3.2)
- [ ] Decide the mutmut strategy — WSL shell-out vs. pre-computed replay (§3.3, P2)
- [ ] Record a `mutation_test` entry into `demo_replay/` so replay mode covers the full pipeline (P4)
- [ ] `sample_repo/` is still P1's smoke fixture — `pricing.py`, `rounding.py`, `parser.py`,
      `api.py` and the seeded bugs in `BUGS.md` are all placeholders awaiting P5

---

## Housekeeping

`custom_modes.yaml` contains em-dashes and curly quotes. Bob's loader strips
problematic unicode rather than failing, so this is cosmetic — but if you edit that
file, type straight quotes and hyphens.

`sample_repo/tests/` is Bob-written territory. Per plan rule 4, nobody hand-edits it
and P5 decides which generated runs get committed.
