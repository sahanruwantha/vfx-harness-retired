---
id: HIR-0052
title: Remat of a layer with accepted units is apply_replan, not a discard
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: remat_refused_accepted_siblings_before_dag_diff
mechanism: remat_apply_replan_preserves_matching_digests
adr: null
---

# Remat of a layer with accepted units is apply_replan, not a discard

## Observed failure

HIR-0051 made atmosphere's required `visible_fraction` rows illegal: a
volume-only unit cannot repair mesh vis. `vfx evals plan` reported
`vis-repair-owner` on the selected view. Preview-consume of
`hf-76bc547dfa3492d32dfa` showed `invalidated=atmosphere` and
`preserved=detail_instancing,lighting_bloom,materials_energy`.

`vfx plan --layer 2 --rematerialize` without `--discard-accepted` raised
before any design:

```text
layer 2 has accepted unit(s) detail_instancing, lighting_bloom, materials_energy;
re-materialization would discard proven work — move that state with `vfx units replan`,
or pass --discard-accepted to retire it deliberately
```

Consuming the finding only reopens atmosphere. The siblings stay `passed`,
so remat still refuses. `--discard-accepted` would retire those seals.
Empty-base replan is the HIR-0040 hole. The shot cannot bind hero vis on a
ray-changing owner.

## Root cause

`_rematerialize_layer` already publishes a replacement and then calls
`apply_replan`. Matching unit digests stay; changed, removed, and
downstream-invalidated units are superseded even if they had passed.

The door check treated **any** accepted unit as "would discard proven work"
*before* the replacement DAG existed. That conflates a DAG amendment with
wiping the layer. Classification: wrong identity at the remat/state
boundary, the same class as HIR-0040 (combined `layers.json` hash is not a
DAG change) and HIR-0049 (consuming a finding is not empty-base replan).

`--discard-accepted` remains the heavier act: accepted orphans, and wiping
state when the replan base is unusable.

## Decision criteria

- Remat of a layer that already has accepted units proceeds without
  `--discard-accepted`.
- After publication, `apply_replan(..., discard_accepted=False)` preserves
  units whose ids and digests still match, including `passed`.
- Changed, removed, or downstream-invalidated units are superseded even if
  they had passed — that is a DAG amendment, not a discard.
- `--discard-accepted` still permits retiring accepted orphans and
  `supersede_layer_units` when the replan base cannot be reconstructed.
- An unusable replan base with accepted units and no `--discard-accepted`
  fails closed after publication; it does not wipe those seals.
- Do not `--discard-accepted` to start remat. Do not empty-base the layer.

## General mechanism

Drop the accepted-unit door raise. Log that accepted units stay unless the
replacement DAG invalidates them. On `apply_replan` `ValueError`, re-raise
when accepted units exist and `--discard-accepted` was not passed; otherwise
keep the unusable-base `supersede_layer_units` path.

## Rejected patch-level alternatives

- `--discard-accepted` to remat layer 2: retires lighting/detail/materials
  the DAG did not necessarily invalidate.
- Consume the finding so atmosphere is not `passed`: siblings stay `passed`;
  the door still refuses.
- Empty-base `vfx units replan`: HIR-0040; resets every unit.
- Hand-edit `layer_2.json` statuses so remat sees no accepted units: durable
  state is not a repair instrument.
- Force-invalidate matching digests when `--discard-accepted` is set:
  successful `apply_replan` already preserves; the flag is the unusable-base
  / orphan wipe, not a second invalidation rule.

## Validation

- `tests/unit/test_planner_outcomes.py`:
  `test_rematerialize_preserves_accepted_units_whose_digests_match` — remat
  with three passed units designs; the unchanged root stays `passed`; the
  changed unit and its dependant become `pending`.
  `test_rematerialize_unusable_base_does_not_wipe_accepted_units` — mocked
  `apply_replan` failure does not call `supersede_layer_units`; passed
  status remains.
  `test_rematerialize_unusable_base_wipes_only_with_discard_accepted` —
  `--discard-accepted` still retires the layer when the replan base is
  unusable.
- `test_already_deferred_rematerialize_still_runs_the_transaction` still
  requires unpublished overlay and `apply_replan`.

## Release and rollback

No schema migration. Rollback restores the accepted-unit door raise, which
again blocks HIR-0051 follow-through on any layer that already sealed a
sibling.

## Remaining limitations

Publication still selects before `apply_replan`. If the replan base is
unusable and `--discard-accepted` is not passed, the replacement view is
live and unit state is unmoved. That is fail-closed, not a hole in the
overlay pointer (HIR-0026). Repair is an explicit replan or
`--discard-accepted` remat, not a silent wipe.

## Resume

Layer 2 remat is in flight as run `20260827T152455Z-2defde` without
`--discard-accepted`. The door logged that lighting/detail/materials stay
unless the replacement DAG invalidates them. Do not start a second plan or
build until that session publishes or fails. Then `vfx evals plan` and
`vfx build --layer 2` only if the replacement is gate-clean. Do not empty-base
the layer.
