---
id: HIR-0047
title: An image-contract debt is not a missing binding
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: claim_closure_retracted_look_image_debts
mechanism: image_contract_ids_are_build_time_debts
adr: null
---

# An image-contract debt is not a missing binding

## Observed failure

Run `20260827T080852Z-a0351a` rematerialized L2 under HIR-0046. Write-time
validation accepted look-owning units whose required claims `asserts: image`
and bind `image_contract` ids while `image_contracts` / `checks.json` stayed
empty. The overlay selected as view `f327fae9…`. JIT unit planning for
`materials_energy` then retracted: the deterministic gate reported seven
blocking `claim-closure` findings of the form `image_contract binding
'…' does not exist`.

HIR-0046 already named those ids build-time debts. Claim-closure treated
absence in `checks.json` the same way it treats a missing scene contract.

## Root cause

`validate_claim_closure` looks up every `image_contract` binding in
`checks.json`. A miss emits `does not exist`. Schema-5 materialization
forbids writing those rows: they are candidate-sensitive. HIR-0029 already
taught claim-closure to count `composition_context.contract_ids` as producers
so extra-frame scene rows would not retract a unit plan. Image-domain look
coverage never got that counterpart. Classification: plan-gate / claim-closure
gap. The unit planner cannot create `checks.json` rows; prompting it to
rewrite the claims as qualitative authority would undo HIR-0046.

## Decision criteria

- A required claim may bind an `image_contract` id that has no `checks.json`
  row. Claim-closure counts that id as a bound producer.
- When the row exists, axis and frame still have to match the claim.
- A missing `scene_contract` is still `does not exist`. Image debts are not
  a scene-catalog hole.
- Evaluation still fails closed if the builder never proposes the row
  (HIR-0046 `look_without_image_domain` / required-claim absence).
- Already-selected views still parse. New materialization writes stay empty
  in `image_contracts`.

## General mechanism

`validate_claim_closure` is the single producer set. A missing image row is
the same class as `human_decision` / `semantic_diff`: declared now, resolved
when a candidate exists.

## Rejected patch-level alternatives

- Materialize dummy `checks.json` rows at plan time: candidate-sensitive
  image checks cannot exist before the producing unit mutates the scene.
- Prompt the unit planner to convert image claims to qualification: that
  un-does look coverage and papers over a mechanical retract.
- Skip the unit-plan gate until build: HIR-0016 still requires a
  gate-attested unit plan.
- Hand-author `plans/units/materials_energy.md` on the production shot: a
  fixture edit is not the missing producer rule.

## Validation

- `tests/architecture/test_staged_architecture.py`:
  `test_claim_closure_counts_image_contract_debts_as_bound`,
  `test_claim_closure_still_checks_existing_image_contract_axis`.

## Release and rollback

No schema migration. Rollback is HIR-0046 materializations that cannot
publish a unit plan.

## Remaining limitations

Composed canonical of a mixed look layer still calls the critic on
`layer.owns` (HIR-0039). Optical contracts for shafts and particle projected
size remain absent (HIR-0044). This record does not propose the image rows;
the builder still owes them at candidate freeze (HIR-0048).
