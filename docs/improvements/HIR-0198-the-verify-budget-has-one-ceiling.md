---
id: HIR-0198
title: The verify budget has one ceiling, and a credential rejection names the dotenv it read
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: a_purpose_computed_budget_was_silently_clamped_by_an_unrelated_session_cap
mechanism: single_declared_ceiling_with_a_derivation_log_line_and_a_resolved_dotenv_in_the_rejection
adr: null
---

# The verify budget has one ceiling, and a credential rejection names the dotenv it read

## Observed failure

Two sessions running fresh shots on `b87035c` reported the same thing within minutes,
independently:

```
hansa_silk_road  run 20260904T143358Z-238376  "verify budget: 12 turns for 6 drafted layer(s)"
caesar_curia     run 20260904T143311Z-c0f282  "verify budget: 12 turns for 7 drafted layer(s)"
```

AGENTS.md states one rule and one ceiling: the verify pass runs under the larger of the
configured verify cap and 6 plus two turns per drafted layer, capped at 24. Those two shots
were owed 18 and 20. The SDK child's argv carried `--max-turns 12`. Executed against the
shipped function:

```
layers      1   2   3   4   5   6   7   8   9+
rule        8  10  12  14  16  18  20  22  24
shipped     8  10  12  12  12  12  12  12  12
shortfall   -   -   -   2   4   6   8  10  12
```

The shortfall is `2 × (layers − 3)`, saturating at half the mandated budget. The scaling
HIR-0177 added was therefore inert for every shot that needed it, and the log line printed
the clamped number beside the layer count, so it read as a computed per-layer budget.

The same sessions each burned a strict-preflight cycle on a second defect: running with the
code pinned to a worktree, preflight reported no credential and told them to "correct the
named credential variables", which were already correct in the primary checkout's `.env`.
Resolution follows the code, not the working directory, so a worktree resolves no dotenv at
all; the rejection never mentioned `VFXH_ENV_FILE`, which the config module documents.

## Root cause

One parameter carried two meanings. `generate_plan_two_pass(max_turns=...)` is the draft
pass's own budget, defaulted from `Settings.plan_max_turns`. Before HIR-0177 the verify
budget was a flat setting and clamping the two together was harmless. HIR-0177 mechanism 7
made verify a purpose-computed function of the drafted layer count with its own declared
ceiling, and I kept the surrounding `min(max_turns, ...)`: a second ceiling that no rule
states and no test covered, because the shots the mechanism was developed against had three
layers, exactly where the two values coincide. The mechanism validated itself on the only
region where it could not fail.

The credential defect is the same shape at a different boundary: `auth()` knows only whether
the variables are set, while `environment_file()` alone knows which dotenv was resolved and
why. The rejection therefore named an action ("correct the variables") derived from the
half of the state it could see, and sent operators to edit a file that was already correct.
`AGENTS.md` requires a rejection to name the violated contract, the observed value, and the
legal next action; the observed value here is the resolved dotenv path, which was never
reported.

## Decision criteria

- A budget declared by a rule has exactly one ceiling: the rule's. A generic session cap
  never silently overrides a purpose-computed one.
- A log line for a computed budget states its derivation, so a clamp cannot masquerade as
  the computation.
- A rejection reports the state that actually decided it. Where two modules each hold half,
  the one that resolved the value supplies the sentence.
- Regression coverage spans the region where the rule and the old clamp diverge, not only
  the development shot's layer count.

## General mechanism

1. `generate_plan_two_pass` computes `verify_turns = plan_verify_turn_budget(configured,
   drafted_layers)` and nothing else; the draft's `max_turns` no longer participates. The
   log line reads `verify budget: 18 turns = max(6 configured, 6 + 2×6 drafted layer(s)),
   ceiling 24`.
2. `infrastructure/config.dotenv_resolution_note()` returns the sentence describing which
   dotenv this process resolved: the explicit `VFXH_ENV_FILE`, the checkout `.env` it will
   read, or that none was resolved because the code is imported from a root without one,
   naming that root and the variable to set. `preflight.auth()` appends it to the
   no-credential problem, and the check's `next_action` points at the dotenv rather than at
   the variables.

## Rejected patch-level alternatives

- Raising `plan_max_turns` so the clamp stops binding: leaves two ceilings and changes the
  draft's budget for an unrelated reason.
- Documenting the clamp in AGENTS.md: makes the rule describe an accident and keeps a
  purpose-computed budget subordinate to a generic one.
- Having preflight read the primary checkout's `.env` when a worktree has none: resolution
  that searches parent checkouts is exactly what `environment_file` refuses to do, and it
  would silently load another checkout's credentials.
- Telling operators in the runbook to set `VFXH_ENV_FILE`: three sessions had the runbook
  and still rediscovered it from source; the rejection is where the answer belongs.

## Validation

- `src/tests/unit/test_verify_budget_has_one_ceiling.py`: the exact shipped defect (6 and 7
  layers under a 12-turn draft cap now receive 18 and 20); every layer count from 1 to the
  ceiling receives the rule's value under both a small and a large draft cap; the log line
  states its derivation; the dotenv note names the explicit variable, the checkout file, or
  the unresolved root; the credential check's next action names `VFXH_ENV_FILE` and no
  longer says "correct the named credential variables".
- The pre-existing `test_planner_outcomes.py` two-pass tests, including the verify-exhaustion
  fallback, pass unchanged.

## Release and rollback

Verify sessions on shots with four or more layers now receive up to twice the turns they
did, so a global plan pass may cost more and produce a more complete audit. Rollback
restores the clamp and the shortfall.

## Remaining limitations

The draft pass keeps the flat `plan_max_turns` cap; nothing yet scales it with the brief's
size, and the three runs that aborted on `max_turns_exhausted` (1c18c2, 7040c2, 270652) died
in verify, not draft. An idle model session still cannot be distinguished from a slow one by
reading the console, which two sessions reported separately and which needs its own
mechanism.
