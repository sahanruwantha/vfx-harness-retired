---
id: HIR-0151
title: Partial subject producers see deferred composition forecasts
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: complete_subject_payer_discovers_unrepairable_upstream_composition
mechanism: nonpayable_deferred_composition_forecast_card
adr: ADR-0006
---

# Partial subject producers see deferred composition forecasts

## Observed failure

Room 1046 bounded Layer 2 build `20260830T105420Z-69526d` sealed
`building_mass` on its f39-local rows. The first dependency-complete subject producer,
`building_roof`, then evaluated the camera-owned parent-role bbox rows at their owner
frames. `building_miniature_start` read `0.474` against `<= 0.15`; `building_bbox_f176`
read `0.0704` against `>= 0.85`. Roof could not mutate the already-passed mass roles,
correctly called `cannot_express_in_scope`, and the run stopped with
`building_roof=hypothesis_falsified` and `ground_site=blocked`.

The mass report and transcript contain no f1, f114, or f176 readings. Its checkpoint
was therefore created before the agent had any instrument showing how its mutable
partial subject projected through the sealed camera path.

## Root cause

HIR-0134 correctly delays authoritative payment until a dependency closure contains
the complete subject. Runtime reused that payment set as the visibility boundary for
the producer's tools and compiled scope. That made "not yet authoritative" mean "not
observable." The complete-subject payer could detect the aggregate failure but had no
legal way to change the upstream producer that caused most of its extent.

## Decision

- Every activation-layer geometry producer whose mutation selector overlaps a deferred
  parent-subject bbox receives the exact row as a deferred subject forecast until the
  dependency-complete payer is reached.
- Forecasts preserve contract id, selectors, frame(s), bounds, owner, fault owner, and
  lifecycle. They are compiled, not inferred from role names or prose.
- A forecast is diagnostic only. It is excluded from required evidence, claim closure,
  checkpoint protection, sealing, canonical payment, and revalidation authority.
- `contract_result` may evaluate a compiled forecast and marks the returned rows
  `diagnostic_only` with `acceptance_evidence: false`.
- Automatic post-mutation read-back evaluates forecasts at their declared frames and
  reports target deltas separately from authoritative scene contracts.
- The first dependency-complete producer remains the sole activation-layer payer under
  HIR-0134 and re-evaluates the final subject union from empty-scene replay.

## General mechanism

`deferred_subject_composition_forecast_ids_for_unit` uses the same lifecycle and
symmetric semantic-selector overlap as the payer compiler, but returns only overlapping
activation-layer rows not payable by the current unit. `compile_scope_with_predecessors`
publishes those full rows on `deferred_subject_forecasts`. The live phase carries their
ids on a separate diagnostic channel; Blender tools schedule their declared frames but
never pass them to `_scene_completion_state`.

## Rejected alternatives

- Make every partial producer pay the full bbox: a base or roof fragment need not satisfy
  a minimum extent that only the final union can satisfy.
- Move payment back to the first producer: that recreates the incomplete-subject bug
  HIR-0134 fixed.
- Let the complete payer mutate predecessor roles: consumed interfaces are read-only and
  a downstream unit may not broaden its write cluster.
- Add more prose asking the mass builder to remember other frames: the missing data is a
  typed instrument and must be read from current scene authority.
- Reopen or compensate the camera: forecasts do not change ownership or mutation scope.

## Validation

The evidence-vocabulary fixture proves an early mass producer receives the deferred bbox
as a forecast, while the dependency-complete roof receives it only as payable evidence
and unrelated site geometry receives neither. The unit-scope fixture proves the full row
is present under a nonpayable card and absent from bound contracts. The Blender-tool
fixture proves read-back says `DIAGNOSTIC ONLY` and `cannot pay acceptance` while naming
the measured value and target.

Validation on 2026-08-30: the focused evidence-vocabulary, unit-scope, and builder-
instrument suites passed 54 tests in 3.49 seconds. `.venv/bin/ruff check src tests`
passed, the full repository suite passed 659 tests in 47.12 seconds, and
`.venv/bin/vfx --help` exited successfully.

## Release and rollback

No persisted schema migration. This adds a runtime scope-card field and diagnostic tool
channel. Rollback restores a blind upstream freeze followed by an unrepairable downstream
failure and is unsafe.
