---
id: HIR-0028
title: Structured decisions bind the selected bundle only
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: stale_generation_forced_adoption
mechanism: last_write_wins_selected_bundle_structured_decisions
adr: ADR-0004
---

# Structured decisions bind the selected bundle only

## Observed failure

Shot `vfx-test`, selected bundle `088b4a7c…`. `state/plan-resolutions.jsonl`
line 6 is A2: a 16-keyframe `cam_rig` schedule keyed to bundle `4c0fe388…`,
recorded as calibration that "require[s] an explicit replan if falsified."
The live generation never re-recorded that row.

`validate_materialization` and the plan gate adopted every satisfied
`values.contract` regardless of `bundle_hash`. `load_resolutions` already
skipped other generations. Remat5 (`975cb6`) therefore had to copy the
falsified A2 spine *and* author a crossing spine that could prove interior
visibility (HIR-0019). Two required `keyframe_schedule` rows on one role.
The kickoff said to copy every structured decision in the ledger, so the
session did.

A2's own text named the legal act: explicit replan, not another remat
against the same satisfied row. There was no reader that would honor a
replan.

## Root cause

The ledger is append-only shot state **keyed to a bundle content hash**
(`plan_records.py` module contract; `load_resolutions`). Adoption readers
ignored the key and last-write-wins only among satisfied rows. A later
`falsified` or `superseded` row for the same id did not retire the
contract. A prior generation's hard-constraint calibration was immortal.

## Decision criteria

- Structured `values.contract` adoption is last-write-wins for the
  **selected** bundle only. Another generation's row is inert.
- A later `superseded` or `falsified` row for the same id on that bundle
  retires it. That is the explicit replan A2 named, without requiring a
  new global publication when only the decision's values changed.
- Materialization copies the compiled binding set for this layer. A scene
  contract `decision_id` that is not active on the selected bundle is
  refused.
- Missing selected-bundle hash leaves the ledger inert rather than
  adopting every generation.

## General mechanism

- `load_active_structured_decisions(path, bundle_hash=)` in
  `domain/plan_records.py`. Gate, materialization validator, spike
  eligibility, and the materialization kickoff share it.
- Kickoff lists binding decisions for the selected bundle and this
  layer's reserved roles. The system prompt points at that list, not at
  a ledger scan.
- `read_selected_bundle_hash` reads the consumer-view marker or
  `plans/current.json`.

## Rejected patch-level alternatives

- Silently rewrite line 6 of `plan-resolutions.jsonl`: not a transaction.
- Prompt the remat session not to copy A2: remat5 already read the file
  because the prompt required a ledger scan (HIR-0025's class).
- Hand-edit `current.json` onto a restored A2 spine: that is the
  falsified HIR-0019 view.
- Loosen max-turns so remat can walk the contradiction: HIR-0027.

## Validation

- `tests/unit/test_plan_records.py`:
  `test_load_active_structured_decisions_keys_to_selected_bundle_and_retires`,
  `test_other_generation_structured_decision_is_inert_at_the_gate`,
  `test_other_generation_structured_decision_does_not_force_materialization`,
  `test_later_falsified_row_retires_structured_decision`.
- `tests/unit/test_planner_outcomes.py`:
  `test_materialization_kickoff_lists_only_selected_bundle_decisions`.
- Existing exact-adoption tests key the ledger to the selected hash.

Do not judge this HIR by rerunning remat5. That session copied A2 because
adoption ignored the bundle key. Remat6 (`b5aa3f`) is the first path that
authored a crossing spine (`cam-spine-keyframe-schedule` f1 y=140, f36 y=180)
with no `decision_id: A2`. It died `max_turns_exhausted` on gate findings
after validator pass (HIR-0027 held the pointer). That is evidence this
HIR ran, not that remat published.

## Release and rollback

No schema migration. Rollback is restoring the unfiltered adoption loop.
That would immortalize prior-generation calibrations again.
