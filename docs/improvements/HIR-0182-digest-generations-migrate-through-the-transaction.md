---
id: HIR-0182
title: Digest generations migrate through the authority-state transaction
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: capsule_digest_semantics_changed_without_migration
mechanism: digest_schema_bump_with_incomparable_migration_transition
adr: null
---

# Digest generations migrate through the authority-state transaction

## Observed failure

Run `20260903T065816Z-a9bbfc` on `artifacts/room_1046_opening` (main `d60bb0b`):
`vfx run --from 1` stopped after 2.7 s in layer-1 planning with
"layer 1 work-unit state does not bind its selected semantic authority capsule"
(classified `harness_defect`). HIR-0181 had changed what a layer capsule contains, so the
capsule digests recompiled from the unchanged selected view (`9bd4a1ec…`, `fe6d397b…`)
no longer equalled the digests the durable work-unit state, the coordinator head, and
every receipt had bound (`b4754d10…`, `467aa3fe…`). `DIGEST_SCHEMA` stayed at 4, so
no reader treated the state as a prior generation, and the only documented escape
("replan closure recomputes both sides") had no producer.

## Root cause

A capsule content change is a digest generation change. The precedent (`ce72812`) pinned
the unit digest to `DIGEST_SCHEMA` with a golden test; nothing pinned the layer capsule,
and no transition knew how to move prior-generation state forward: `validate_current`
returned silently, the effects compiler raised "incomparable digest schema", and the
planner compared cross-generation hashes.

## Decision criteria

- Strict migration: prior-generation authority is migrated or rejected through typed
  transactions, never compared heuristically or edited by hand (ADR-0004).
- Receipts that name a prior-generation capsule digest cannot be re-verified, so they are
  archived with a typed reason; units rebuild.

## General mechanism

1. `DIGEST_SCHEMA` is 5; `PRIOR_DIGEST_SCHEMAS` names the generations that migrate and
   `DIGEST_GENERATION_RULE` the command that does it. Golden tests pin both the unit
   digest and the layer capsule digest to the schema.
2. `compile_authority_state_effects` classifies every durable state by digest generation:
   a prior-generation layer migrates as an `incomparable` effect — every unit and the
   terminal receipt are archived with "digest generation N migrated to M", fresh pending
   state binds the current generation — and mixed generations across layers are refused.
3. `vfx migrate-digest-schema <shot>` republishes the selected JIT view under the next
   pointer revision through the ordinary transition
   (`prepare_selected_view_republication_locked`), with a typed producer record; it runs
   no model, Blender, render, or critic work and is a no-op on current state.
4. `validate_current` refuses prior-generation state with the migration rule instead of a
   silent return.
5. The successor-state writers (`_new_state`, `_retire_slot`, `_after_state`, the migration
   classification) move to `orchestration/authority_state_successor_state.py`; retired
   slots record `superseded_reason`; and the transition record rule that a layer must
   change its generation digest now applies only to `changed` layers, because an
   `incomparable` layer crosses digest generations where equal strings are coincidence.

## Rejected patch-level alternatives

- Rebinding stored digests in place (hand-editing durable state).
- Accepting cross-generation digests as comparable when the layer row is unchanged.

## Validation

- `src/tests/unit/test_authority_capsules.py::test_layer_capsule_content_changes_demand_a_digest_schema_bump`.
- `src/tests/unit/test_authority_state_effects.py::test_prior_digest_generation_state_migrates_as_incomparable`
  and `::test_prior_generation_state_names_the_migration_command`.
- `src/tests/integration/test_digest_schema_migration.py`: a published layer whose state is
  rewritten to generation 4 migrates under the same view hash with the next JIT revision.

## Release and rollback

Every shot with generation-4 state migrates once (units rebuild). Rolling back restores
the silent escape and the cross-generation planner failure.

## Remaining limitations

- Migration supersedes accepted units of prior-generation layers; unit receipts bind the
  layer capsule digest, so they cannot be carried across generations.
