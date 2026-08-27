# sample_repo/BUGS.md — P5 owns; seeded bug documentation
# PLACEHOLDER: P5 will document all seeded bugs and which pipeline stage catches each.
# See AGENTS.md §5 for the full bug-vs-wrong-test classification rules.

# Seeded Bug Map (to be filled in by P5)

| Bug ID | File | Description | Stage that catches it |
|--------|------|-------------|----------------------|
| BUG-001 | `src/parser.py` | TBD — plain unit-test bug | Stage 3 (validate_and_keep) |
| BUG-002 | `src/rounding.py` | TBD — Hypothesis-only boundary bug | Stage 7 (run_property_tests) |
| BUG-003 | `src/pricing.py` | TBD — mutation-only weak-assertion trap | Stage 10 (mutation_test) |
| BUG-004 | `src/api.py` | TBD — 500 on malformed payload (if fuzzing survives the cut) | Stage 8 (run_fuzz) |

See `docs/benchmarks.md` for before/after coverage and mutation scores.
