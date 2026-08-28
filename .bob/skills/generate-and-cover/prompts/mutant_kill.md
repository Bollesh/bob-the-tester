# Mutant-Kill Prompt (MuTAP-Style Regeneration)

> **Used in:** Stage 11 of generate-and-cover
> **Actor:** Bob (Layer 1 — judgment step)
> **Input:** Surviving mutants from `mutation_test`, source file content

---

## Prompt

You are performing MuTAP-style targeted test generation. The mutation
testing tool (`mutmut`) has identified mutants that SURVIVED — meaning
no existing test distinguishes the original code from the mutated version.
Your job is to write tests that KILL these survivors.

### Source File (Original)

```python
{source_code}
```

### Surviving Mutants

{surviving_mutants_table}

Each mutant is described as:
- **ID:** unique mutant identifier
- **Line:** the source line that was mutated
- **Original:** the original code on that line
- **Mutated:** the mutated version of that line

### Task

For each surviving mutant, write a test that:

1. **PASSES against the original code** (the test must be correct).
2. **Would FAIL against the mutated code** (the test kills the mutant).

The key insight: the mutation changes specific behaviour. Your test must
exercise exactly that behaviour and assert the original outcome, so that
the mutated version produces a different outcome and fails.

### Strategy

For each mutant, think through:

1. **What does the mutation change?** (e.g., `>` → `>=`, `+` → `-`,
   `return x` → `return None`, boundary shift, operator swap)
2. **What input would trigger different behaviour?** Find an input where
   the original and mutated code produce DIFFERENT outputs. This is
   usually at the boundary of the condition being mutated.
3. **What should the assertion be?** Assert the ORIGINAL behaviour
   precisely. The mutated version will then fail this assertion.

### Example

If the mutant changes `if quantity > 100:` → `if quantity >= 100:`:
- The boundary is `quantity = 100`.
- Original: `100 > 100` is `False` → no discount.
- Mutated: `100 >= 100` is `True` → discount applied.
- Test: assert that `calculate_discount(quantity=100)` returns the
  NON-discounted price.

### Output Format

```python
def test_kill_mutant_{id}_{short_description}():
    """Kills mutant {id}: {original} → {mutated} on line {line}.

    This test exercises the boundary at {boundary_description} where
    the original and mutated code diverge.
    """
    # Arrange
    ...
    # Act
    result = function_under_test(boundary_input)
    # Assert the ORIGINAL behaviour
    assert result == expected_original_outcome
```

### Rules

1. **Name each test** `test_kill_mutant_{id}_{short_description}`.
2. **Docstring must reference** the mutant ID, the original code, the
   mutated code, and the line number.
3. **Each test targets ONE mutant.** Don't try to kill multiple mutants
   with one test (it makes diagnosis harder if a test is later discarded).
4. **Every test goes through `validate_and_keep`** (Stage 3). If a
   mutant-killing test is rejected, record why and move on. Do not
   force-keep it.
5. **Focus on HIGH-VALUE survivors first.** Mutants on core logic lines
   (calculations, conditionals, return values) are more important than
   mutants on logging or formatting lines.
6. **Do NOT modify the source code** to make mutants easier to kill.
   The source is sacred; only tests are generated.
7. **Iteration cap:** You have at most {mutant_kill_rounds} rounds of
   mutant-killing. After that, report remaining survivors in Stage 13
   rather than burning more Bobcoins.
