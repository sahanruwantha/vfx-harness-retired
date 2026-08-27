---
id: HIR-0030
title: Published schedule samples that already exceed a smoothness cap are refused
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: jointly_unsatisfiable_contracts_published
mechanism: schedule_derivative_linear_floor_at_authoring
adr: null
---

# Published schedule samples that already exceed a smoothness cap are refused

## Observed failure

Shot `vfx-test`, layer 1 unit `cam_spine`, rebuild `20260826T170413Z-ba2b4c`. Empty-scene
replay failed `cam-location-smoothness` (`curve_derivative_max` on `cam_rig.location`,
`hi: 6.0`) at **7.391**. The published `cam-spine-schedule` samples were f1
`location y=−30` and f24 `y=140`: 170 units in 23 frames. LINEAR between those keys
already is 170/23 ≈ 7.39. No handle, extra key, or interpolation mode can go slower
and still hit both samples. Repair 1 left the script alone. Repair 2 capped the rig,
broke the schedule (0.0 → 42.5), and reverted. Repair budget exhausted.

## Root cause

`validate_row` checks each row in isolation. The contradiction is arithmetic across
two rows that share a role: consecutive `keyframe_schedule` samples already exceed
the same-role `curve_derivative_max.hi`. Classification: contract / fail-closed gap
at authoring. The builder cannot invent a third option inside mutation scope.

## Decision criteria

- Make the failure unrepresentable at materialization and authoring. A published
  pair that cannot pass is a plan defect, not a builder defect.
- The floor matches the probe: max-component `|Δ|` per adjacent sample, divided by
  frame span. BEZIER cannot undercut LINEAR between two sealed keys.
- Same-role pairing only. A smoothness cap on a different role is not this pair.
- A published view that already exists still fails closed at the plan gate (unlike
  auto-socket grandfathering, which can measure honestly on a permissive node).

## General mechanism

- `schedule_derivative_floor` / `schedule_smoothness_contradictions` in
  `evidence/scene_checks.py`, folded into `validate_row_set`.
- Materialization `note`s every collectable finding (blocking). Planner
  `plan_guardrails` refuse the same write.
- Plan-gate `_cross_row_contract_findings` emits the pair as **blocking**; auto-socket
  sibling lint stays advisory for grandfathered views.

## Rejected patch-level alternatives

- Loosen `hi: 6.0` in core or for this shot: a fixture-shaped bound is not a fix.
- Prompt the builder to ignore smoothness: the pair is still published.
- Let repair "choose" interpolation: LINEAR is already the floor.

## Validation

- `tests/unit/test_evidence_vocabulary.py`:
  `test_schedule_smoothness_refuses_linear_floor_above_hi` (ba2b4c numbers fail;
  `hi: 8.0` or a different role passes); gate helper marks the pair blocking and
  the auto-socket sibling advisory.

Do not judge this HIR by rebuilding `eb02080d` on the contradictory pair. Amend one
side, rematerialize, then rebuild.

## Release and rollback

No schema migration. Rollback is removing the cross-row floor, which would again
publish pairs no builder can satisfy.

## Remaining limitations

The floor is consecutive published samples, not the denser evaluated curve. Extra
keys between samples can only raise the measured max, never lower this floor.
Other jointly unsatisfiable kinds (count vs uniqueness, bbox vs clearance) are
not this mechanism.
