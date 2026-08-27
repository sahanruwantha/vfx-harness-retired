---
id: HIR-0046
title: Look ownership is not a scene-count seal
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: look_unit_sealed_on_scene_counts
mechanism: look_requires_image_domain_claim
adr: null
---

# Look ownership is not a scene-count seal

## Observed failure

Run `20260827T060800Z-8bd92a` rematerialized L2 so every unit judge frame had a
required claim (HIR-0045). All four units declared look capabilities
(`material`, `lighting`, `detail`, `atmosphere`) and bound only
`executable_required` scene rows (counts, links, colors, particulate
`visible_fraction`). Canonical sealed each unit at 5.0 with
`decided_by: unit_executable_evidence`. Composed `02_lookdev_atmosphere.py`
then called the critic because any look-owning stage keeps that path
(HIR-0039). Scores were 2.33 / 2.17 REVISE. `vfx build` exited 9:
units passed, composed verdict failed.

The critic described a crushed-black chamber, one blown core, and particulate
as snow. Atmosphere had already isolated that the foundry architecture did not
read even with volume hidden. The selected view also dropped ADR-0007
`dresses`. Those are the plate. The harness defect is that look ownership
never had to certify appearance before the unit sealed.

## Root cause

`_executable_unit_verdict` returns 5.0 when every required claim at the frame
is `executable_required`. It does not consult `look_capabilities` or
`image_evidence_required_for`. HIR-0014 already says counts cannot certify
appearance. HIR-0044 kept live `run_bpy` open on look-without-image-bindings
and left a remaining note that canonical still needed a critic. HIR-0045
closed uncovered-frame critic fall-through and deferred this hole:

> Require `look_capabilities` to imply an `asserts: image` claim: a later HIR
> if composed canonical still look-gates an axis whose producer only has scene
> claims.

That later HIR is this record. Classification: evaluation short-circuit /
missing materialization coverage. Sending the covered scene-only frame to the
critic would repeat HIR-0045: a look vote on propositions the bound evidence
cannot certify.

## Decision criteria

- A non-empty `look_capabilities` list must cover every unit judge frame with
  a required claim that `asserts: image` and binds `image_contract`,
  `qualification`, or `human_decision`.
- Materialization reports unearned frames on `look_capabilities` with
  `LOOK_REQUIRES_IMAGE_DOMAIN_RULE`. `image_contracts` stays empty at
  materialization; the binding is a build-time debt.
- `_judge_unit_or_layer` and `_executable_unit_verdict` return `contract_gap`
  (`decided_by: look_without_image_domain`) and do not call `_judge`.
- That verdict has empty `issues` so canonical records `contract_gap` instead
  of opening repair.
- Look-less units are unchanged. Look units that bind image-domain evidence
  still seal on that evidence when it passes.
- Already-selected views still parse. New materialization writes fail closed.

## General mechanism

`required_claim_certifies_look` / `unearned_look_judge_frames` are the single
coverage predicate. Materialization, the compiled frame-authority card, and
evaluation share it. `_look_without_image_domain_verdict` is the evaluation
writer.

## Rejected patch-level alternatives

- Fall through to the critic at unit freeze: HIR-0045 just forbade look votes
  on claims the bound evidence cannot certify.
- Make `image_evidence_required_for` false for look-without-bindings: HIR-0044
  rejected that; it would revive sealing on geometry.
- Prompt the builder to iterate look harder while 0/0 image rows stay open:
  live mutation was already legal; canonical still wrote 5.0.
- Restore `dresses` on the production shot by hand: a fixture edit is not the
  missing claim-domain rule. Dressing remains ADR-0007.
- Change composed canonical so mixed-look layers fan in executable claims
  (HIR-0039 remaining): that is an architectural composition choice, not this
  unit-local lie.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_look_owning_scene_only_claims_do_not_seal_or_call_the_critic`,
  `test_look_owning_image_contract_still_seals_on_executable_evidence`.
- `tests/unit/test_plan_records.py`:
  `test_materialization_rejects_look_without_image_domain`.
- `tests/unit/test_work_units.py`: `unearned_look_judge_frames` and compiled
  card.

## Release and rollback

No schema migration. Rollback is look-owning scene-only units sealing 5.0 and
deferring the first look vote to composed canonical.

## Remaining limitations

Composed canonical of a mixed look layer still calls the critic on
`layer.owns` (HIR-0039). Optical contracts for shafts and particle projected
size remain absent (HIR-0044). This record does not restore dropped dressing
authority; it refuses to call existence a look pass. Claim-closure must count
the bound `image_contract` ids as producers while `checks.json` is empty
(HIR-0047); otherwise a legal HIR-0046 materialization cannot publish a unit
plan. Paying those debts is compiled, coherence-checked, and freeze-gated
(HIR-0048); otherwise a legal HIR-0046/0047 unit still spends a doomed
canonical plus repair proving the ids were never a scene-selector miss.
