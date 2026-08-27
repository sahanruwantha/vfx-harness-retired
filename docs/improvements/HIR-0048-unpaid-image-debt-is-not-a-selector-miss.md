---
id: HIR-0048
title: An unpaid image-contract debt is not a selector miss
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: unpaid_image_contract_debts_guessed_by_consumers
mechanism: compiled_image_debt_card_and_freeze_gate
adr: null
---

# An unpaid image-contract debt is not a selector miss

## Observed failure

Run `20260827T084126Z-c5cf72` built `materials_energy` against selected view
`f327fae9…`. Claim `mat-look-image-f72` was legal: `asserts: image`,
`authority: executable_required`, evidence `{id: materials-energy-look-f72,
kind: image_contract}`, `property: render_region_stat`, moments `[72]`. The
view's `checks.json` and `scene_checks.json` contained zero
`materials-energy-look` rows. HIR-0046/0047 made that empty-row shape legal.
Canonical scored f72/f150 as 1.0 `contract_gap` (5/6). Scene rows passed.
Repair 2/2 called `cannot_express_in_scope` on the candidate session
(HIR-0043 held). Finding `hf-92d9d1904858b85d9198` reused the HIR-0031
interpolation `required_authority` verbatim. Exit 7. Status detail named
`materials_energy=hypothesis_falsified` and blocked `atmosphere`,
`detail_instancing`, `lighting_bloom` (HIR-0037). Cost $5.78 / 84 turns.

Five consumers, five nearest-template guesses:

1. Live probe mixed the ids into scene completion. Unevaluated ids landed in
   `missing` and emitted selector-miss copy, then critic-handoff framing.
2. The unit plan described the look ids as judge comparison, downstream of
   the planner addendum that runtime checks may never be promoted.
3. Canonical fabricated `[check:{eid}] required bound evidence was not
   produced` — selector-miss framing and a `check:` prefix that leaked into
   the finding's `contract_ids`.
4. Repair had only retag-shaped levers (`probe_candidate` +
   `cannot_express`). `propose_checks` is live MCP only.
5. `active_image_evidence_ids` already existed and fed flags, but bound
   nothing.

## Root cause

Look `image_contract` ids are names with no runnable spec at plan time.
Materialization forbids writing `image_contracts`. The only writer is live
`propose_checks`. The harness never compiled the owed payment as a typed
card, so every consumer guessed. Classification: agent-capability /
evaluation-contract gap, not a model limitation.

## Decision criteria

- Required `asserts: image` + `image_contract` bindings compile to a card
  `{id, axis, frame, property}` shared by kickoff, `unit_scope`, live probe,
  `propose_checks`, freeze, canonical missing-debt issues, `cannot_express`,
  and hypothesis-falsification authority.
- Ids are bare claim ids everywhere on that path. `check:` is a critic
  citation tag on an existing failing row, not part of the debt id.
- A kept `propose_checks` row must match the owed card on id, frame,
  property kind, and axis. An f150 measurement cannot pay an f72 debt
  (HIR-0015).
- Candidate freeze refuses while any owed id has neither a matching payment
  row nor a typed `unpaid_image_debt` `cannot_express`. On refuse: skip
  canonical and repair, synthesize the abstention, existing HF path. After
  freeze there is no legal payer.
- Canonical repair must not bind `propose_checks`.
- A matching `runtime_checks.json` row is consumed by claim-closure (axis,
  frame, property/metric). Absence remains a HIR-0047 debt, not
  `does not exist`.
- Look-less units and look-without-image-bindings (HIR-0044) are unchanged.
- Abstention stays legal: `cannot_express_in_scope` as `unpaid_image_debt`
  covers the unpaid ids and freeze proceeds without canonical.

## General mechanism

`domain/image_debts.py` is the one compiler. Live `propose_checks` is the
payer. Freeze is the enforcement boundary: teaching is prose, and c5cf72
walked past prose. Fail closed where the failure becomes unpreventable.

## Rejected patch-level alternatives

- Prompt-only payment instructions: c5cf72 already had that class of prose.
- Full `checks.json` rows at materialization: candidate-sensitive; schema-5
  forbids it.
- Critic fall-through or qualification instead of `image_contract`: undoes
  HIR-0046.
- Rematerialize to drop the look claims: hides the debt.
- Bind `propose_checks` on the candidate MCP: repair after freeze must not
  author evaluation contracts.
- Skeleton-check ADR: the claim already carries property, moments, and axis;
  the compiled card makes skeletons redundant.

## Validation

- `tests/unit/test_image_contract_debts.py`: compile cards; strip `check:`;
  reject wrong id naming both sides; reject owed id + wrong frame naming
  both; freeze refuse / allow paid / allow typed abstention;
  `conflict_authority` is not the interpolation template.
- `tests/unit/test_evidence_completeness.py`: scene 5/5 with unpaid image
  debts is an unpaid-debt note, not selector-miss, not critic handoff.
- `tests/architecture/test_staged_architecture.py`: claim-closure consumes a
  matching `runtime_checks.json` row; owed id + wrong-axis or wrong-frame
  runtime still fails (`test_claim_closure_consumes_matching_runtime_image_row`,
  `test_claim_closure_rejects_runtime_image_row_wrong_frame`,
  `test_claim_closure_rejects_runtime_image_row_wrong_axis`).
- `tests/unit/test_look_capabilities.py`: missing image-debt issues use the
  bare id and unpaid class; a paid evidence row still seals.
- `tests/unit/test_unit_scope.py`: compiled card lists `image_debts`.
- `tests/unit/test_builder_instruments.py`: candidate server still binds
  `cannot_express_in_scope` and does not bind `propose_checks`;
  `check:` ids normalize; unpaid ids classify as `unpaid_image_debt`.
- `tests/architecture/test_staged_architecture.py`:
  `test_render_region_stat_family_tracks_the_metrics_registry` — the
  `region_*` payment family is derived from the `METRICS` registry prefix,
  not a hand-copied key list (ADR-0003).
- `tests/unit/test_plan_session_budgets.py`:
  `test_spike_refuses_check_prefixed_falsification_ids` — a resolution
  `contract_ids` value of `check:form-look-f40` still refuses a bare spike
  row.
- Look-less / HIR-0044 look-without-bindings checks remain.

## Release and rollback

No schema migration. Rollback is five consumers guessing again, freeze
allowing unpaid look ids into canonical, and HF copy calling interpolation
on an image debt.

## Remaining limitations

Composed canonical of a mixed look layer still calls the critic on
`layer.owns` (HIR-0039). Optical contracts for shafts and particle projected
size remain absent (HIR-0044). The existing `hf-92d9d1904858b85d9198` record
stays consumable: classification is right; on-disk `contract_ids` may still
carry a `check:` prefix, and readers strip it. Resume is `vfx units replan`
then a fresh `vfx build --layer 2`, not `--discard-accepted`. JIT identity
for that command is HIR-0049.
