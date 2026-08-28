# bob_sessions/ — Append-Only Evidence Storage

> **INVARIANT (AGENTS.md §7 #9): Never delete or rewrite contents of this directory.**

This directory contains exported Bob task reports and Bobcoin consumption
screenshots, committed after every meaningful Bob session.

## What goes here

| Artifact | Format | When |
|---|---|---|
| Task history export | `.md` or `.json` (Bob's export format) | After every generate-and-cover run |
| Consumption screenshot | `.png` | After every session, showing Bobcoin usage |
| Session summary | `.md` | Optional — human-written notes on what was tested |

## Naming convention

```
YYYY-MM-DD_HH-MM_{target-module}_{short-description}.{ext}
```

Examples:
- `2026-08-27_14-30_pricing_initial-coverage-run.md`
- `2026-08-27_14-30_pricing_bobcoin-usage.png`

## Rules

1. **Append-only.** Never edit, rename, or delete existing files.
2. **Commit daily.** At minimum, at each checkpoint (end of Day 1, Day 2, Day 3).
3. **No fabrication.** Only real Bob session exports — no hand-written fakes.
4. **This is mandatory judging evidence.** The hackathon requires proof of IBM Bob
   usage. Missing or sparse evidence here directly impacts scoring.
