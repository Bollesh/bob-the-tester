# Hypothesis Property-Writing Prompt

> **Used in:** Stage 7a of generate-and-cover
> **Actor:** Bob (Layer 1 — judgment step)
> **Input:** Source file content, edge-case map, current test suite

---

## Prompt

You are writing Hypothesis property-based tests for the following Python
source module. Property tests assert INVARIANTS that must hold for ALL
valid inputs — they complement the specific scenarios in the edge-case
map by exploring the input space exhaustively.

### Source File

```python
{source_code}
```

### Edge-Case Map (from Stage 1)

{edge_case_map}

### Existing Tests (already validated and kept)

{existing_tests}

### Task

Write Hypothesis `@given(...)` property tests for the public functions
in this module. For each property:

1. **Identify the invariant.** Common patterns:
   - **Round-trip:** `decode(encode(x)) == x`
   - **Monotonicity:** if `a < b` then `f(a) <= f(b)`
   - **Idempotence:** `f(f(x)) == f(x)`
   - **Conservation:** output bounded by input (e.g., discounted price
     never exceeds original price, never negative)
   - **Commutativity:** `f(a, b) == f(b, a)`
   - **Oracle comparison:** simple brute-force implementation agrees with
     optimised one
   - **No-crash:** function does not raise for any valid input in the
     strategy domain (useful for robustness testing)

2. **Constrain strategies to realistic ranges.**
   - Don't use unbounded `st.integers()` — constrain to the domain
     (e.g., `st.integers(min_value=0, max_value=10000)` for item counts).
   - Don't use `st.text()` without `alphabet` or `min_size`/`max_size`
     constraints — shrinking becomes unmanageable.
   - Use `@example(...)` decorators for known critical values from the
     edge-case map to ensure deterministic coverage.

3. **Use `@settings(max_examples={property_max_examples})`** to control
   the number of examples.

4. **Name each property test:** `test_prop_{function}_{invariant}`
   - Example: `test_prop_calculate_discount_never_exceeds_price`
   - Example: `test_prop_encode_decode_roundtrip`

5. **Docstring each property test** explaining:
   - What invariant is being tested
   - What input domain the strategy covers
   - Why this invariant matters for correctness

### Output Format

```python
import pytest
from hypothesis import given, settings, example
from hypothesis import strategies as st
from {module} import {functions}


@given(...)
@settings(max_examples={property_max_examples})
@example(...)  # critical value from edge-case map
def test_prop_{function}_{invariant}(...):
    """Property: {invariant description}.

    Covers {input domain description}.
    This invariant matters because {rationale}.
    """
    # Arrange (if needed)
    # Act
    result = ...
    # Assert invariant
    assert ...
```

### Rules

- Write AT LEAST one property per public function with a non-trivial
  invariant. Skip functions that are purely side-effecting with no
  observable return value (document why in a comment).
- Do NOT write properties that duplicate the exact scenarios from the
  edge-case map — properties test universal truths, not specific cases.
- Do NOT use `assume()` excessively — reshape the strategy instead.
- If a property FAILS during `run_property_tests`, the shrunk
  counterexample will be converted into a permanent regression test
  in Stage 9. Do not try to "fix" the property to make it pass —
  the failure may indicate a genuine bug in the source code.
