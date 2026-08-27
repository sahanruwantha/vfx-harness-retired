---
id: HIR-0031
title: An in-scope abstention stops the remaining repair budget
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: repair_budget_spent_on_unsatisfiable_authority
mechanism: cannot_express_in_scope_stops_remaining_repairs
adr: null
---

# An in-scope abstention stops the remaining repair budget

## Observed failure

Same run `20260826T170413Z-ba2b4c`. Canonical evidence already proved
`cam-spine-schedule` vs `cam-location-smoothness` unsatisfiable. Repair 1
abstained (no script edit). The harness still burned repair 2, which capped the
rig, broke the schedule, and reverted. The unit ended `failed` with no typed
plan finding, so the next operator step looked like another build.

## Root cause

Repair is a retry budget over script edits. Abstention was not a first-class
tool result. Classification: orchestration / recovery gap. A repair that cannot
express the fix inside authorized scope must stop with `hypothesis_falsified`,
not consume the next attempt.

`ask_supervisor` is plan-only and does not block. Builders had no legal way to
record "these contracts cannot both pass here."

## Decision criteria

- Abstention is always legal. A forced second attempt among unsupported options
  is a harness defect.
- Empty `decisions` is legal on `HypothesisFalsification` when the conflict is
  arithmetic across published contracts, not a named decision path.
- If the agent already mutated before abstaining, restore the pre-repair script.
- Distinct from `ask_supervisor`. This tool stops remaining repairs.

## General mechanism

- Builder tool `cannot_express_in_scope(contract_ids, reason)` writes
  `comparison_state["cannot_express"]`.
- The canonical repair loop skips remaining attempts when that payload is set
  **or** when failing evidence already intersects a schedule/smoothness pair
  (`_unsatisfiable_pair_findings`).
- On unit `failed`, `_record_unsatisfiable_pair_falsification` publishes
  `hypothesis_falsified` with `conflict.kind: contract`. `vfx units replan`
  consumes it.

## Rejected patch-level alternatives

- Prompt "if you cannot fix it, say so": repair 1 already said so; the loop
  still fired repair 2.
- Reduce `MAX_CANON_REPAIRS` to 1: unrelated units lose a legal second approach.
- Treat the miss as an ordinary builder failure: the next run would rebuild the
  same pair.

## Validation

- `tests/integration/test_harness.py`: `cannot_express_in_scope` is registered
  next to `unit_scope`; `ask_supervisor` is not.
- `tests/unit/test_builder_instruments.py`:
  `test_unsatisfiable_pair_findings_require_a_failing_member`.
- `tests/unit/test_hypothesis_falsification.py` already records empty
  `decisions` as a legal payload.

## Release and rollback

No schema migration. Rollback is restoring a repair loop that always spends
`MAX_CANON_REPAIRS` regardless of in-scope impossibility.

## Remaining limitations

Only schedule/smoothness pairs auto-skip without the tool. Other unsatisfiable
shapes still require the agent to call `cannot_express_in_scope` (or a later
typed detector). Repair sessions now bind that tool on the candidate server
(HIR-0043); telemetry that the agent guessed instead of calling it is HIR-0020
and must not gate this path.
