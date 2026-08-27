---
id: HIR-0045
title: An uncovered unit judge frame is not a critic look vote
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: judge_frame_without_claim_fell_through_to_critic
mechanism: required_claim_covers_every_unit_judge_frame
adr: null
---

# An uncovered unit judge frame is not a critic look vote

## Observed failure

Run `20260827T050158Z-7d5924` `atmosphere` sealed live and canonical **f72 at
5.0** on three executable scene rows (object counts and a node-link). Canonical
**f150** called the critic (`atmospheric_scale_reinforcement=2.0`). Repair 1
flattened peak density/emission; score stayed 2.0. Repair 2 called
`cannot_express_in_scope` (HIR-0043). Exit 7,
`atmosphere=hypothesis_falsified`.

The unit's `evaluation.judge` is `{72, 150}`. Every required claim's `moments`
is `[72]` only. `_executable_unit_verdict` therefore returns a pass at f72 and
**None at f150**. `_judge_unit_or_layer` treats that None plus non-empty
`look_capabilities` as permission to call `_judge`. The critic then scored
shafts and mote size — propositions the bound counts cannot certify (HIR-0014).

The same hole exists on `materials_energy` (look-owning, claims only at f72,
judge `{72, 150}`); that unit survived because the critic happened to pass.

## Root cause

Claim moments must be a subset of the unit judge set (HIR-0029). The inverse
was unenforced: a unit may list extra judge frames that no required claim
covers. Canonical still renders those frames. For a look-owning unit, empty
required-at-this-frame falls through to the critic. For a look-less unit, the
same hole auto-passed 5.0 (`_lookless_without_executable_verdict`). Classification:
evaluation fall-through / missing materialization coverage. HIR-0044 remaining
limitation named look-gating on counts; the counts never even applied at f150.

## Decision criteria

- Every unit `evaluation.judge` frame appears in at least one required
  `claim.moments`. Materialization reports the uncovered frames with
  `UNIT_JUDGE_CLAIM_COVERAGE_RULE`.
- `_judge_unit_or_layer` on an active unit with no required claim at that frame
  returns `contract_gap` and does not call `_judge`, whether or not the unit
  declares look capabilities.
- That verdict has empty `issues` so canonical records `contract_gap` instead of
  opening repair (same gate as other coverage defects).
- Extra-frame scene contracts still bind through
  `composition_context.contract_ids`; those frames do not belong on the unit
  judge (HIR-0029).
- Already-selected views still parse. New materialization writes fail closed.

## General mechanism

`uncovered_unit_judge_frames` is the single coverage predicate. Materialization
and the compiled frame-authority card share it with evaluation.
`_uncovered_judge_frame_verdict` is the evaluation writer.

## Rejected patch-level alternatives

- Prompt the builder to iterate look at f150: live primary is f72; the fall-through
  is canonical evaluation.
- Auto-skip remaining repairs when look is 2.0 and scene is 3/3: HIR-0043 already
  binds abstention; this hole should not reach the critic.
- Require `look_capabilities` to imply an `asserts: image` claim: deferred to
  HIR-0046. This record closes the uncovered-frame fall-through.
- Drop f150 from atmosphere in the production shot by hand: a fixture edit is
  not a harness mechanism.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_uncovered_judge_frame_does_not_call_the_critic` (look-owning, atmosphere
  shape), `test_lookless_uncovered_frame_is_contract_gap`,
  `test_lookless_nonexecutable_claim_still_skips_critic`.
- `tests/unit/test_plan_records.py`:
  `test_materialization_rejects_uncovered_unit_judge_frame`.
- `tests/unit/test_work_units.py`: coverage helper and compiled card.

## Release and rollback

No schema migration. Rollback is f150 critic on a look unit whose claims only
cover the primary frame.

## Remaining limitations

Composed canonical of a mixed look layer still calls the critic on `layer.owns`
when any unit declares look capabilities (HIR-0039 is look-less-only). Look
ownership without an image-domain required claim is a contract_gap (HIR-0046).
Optical contracts for shafts and particle projected size remain absent (HIR-0044).
