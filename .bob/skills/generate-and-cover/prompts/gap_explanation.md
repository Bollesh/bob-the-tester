# Gap Explanation Prompt

> **Used in:** Stage 13b of generate-and-cover
> **Actor:** Bob (Layer 1 — judgment step)
> **Input:** Structured gap data from `explain_gaps` tool

---

## Prompt

You are writing plain-language explanations for the remaining uncovered
lines in the source file after the TestForge pipeline has completed. These
explanations will be stored in the database and displayed on the dashboard
for the team to review.

### Source File

```python
{source_code}
```

### Structured Gap Data (from `explain_gaps` tool)

{gap_data}

### Pipeline Summary

- Coverage achieved: {final_coverage}%
- Coverage target: {coverage_target}%
- Iterations used: {iterations_used} / {max_iterations}
- Tests kept: {tests_kept}
- Tests discarded: {tests_discarded}

### Task

For each uncovered region in the gap data, write a concise, plain-language
explanation covering:

1. **What the uncovered code does** (one sentence).
2. **Why it's uncovered** — use exactly one of these categories:
   - **Dead code:** unreachable under normal conditions (e.g.,
     `if __name__ == "__main__":` guard, defensive else-branch that
     can't trigger given the function's preconditions).
   - **External dependency:** requires a live database, network call,
     or specific OS state that unit tests cannot provide without
     integration-level setup.
   - **Complex trigger:** the condition path requires specific
     multi-step state setup that the pipeline didn't generate in
     the allotted iterations.
   - **Intentional skip:** logging-only code, debug statements, or
     display formatting where coverage adds no bug-detection value.
   - **Iteration limit:** the pipeline ran out of iterations before
     targeting this region. More iterations would likely cover it.
3. **Should it be covered?** (Yes / No / Low priority)
   - Yes → suggest what kind of test would cover it.
   - No → explain why (dead code, intentional skip).
   - Low priority → explain why it's not worth the effort vs. risk.

### Output Format

For each gap:

```
### Lines {start}–{end}: {function_name}

**What:** {one-sentence description of the code}

**Why uncovered:** {category} — {explanation}

**Should cover:** {Yes|No|Low priority} — {rationale or suggestion}
```

### Rules

1. **Be honest.** If the pipeline simply ran out of iterations, say so.
   Don't invent elaborate reasons for simple shortfalls.
2. **Be specific.** Reference actual line numbers, function names, and
   code constructs. Don't say "some error handling code" — say
   "the `except ConnectionError` branch on line 47 of `fetch_data()`".
3. **Be concise.** Each gap explanation should be 2–4 sentences maximum.
   The dashboard has limited space.
4. **Don't blame the tools.** If `validate_and_keep` discarded tests that
   would have covered a region, note it factually but don't editorialize.
5. **Acknowledge seeded bugs.** If an uncovered region corresponds to a
   known seeded bug in `sample_repo/BUGS.md`, note that the gap is
   intentional test infrastructure, not a pipeline failure.
