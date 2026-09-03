---
id: HIR-0178
title: Namespace counts cannot exclude the descendants sibling rows require
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: unsatisfiable_contract_reached_builder
mechanism: namespace_count_bound_gate_and_descendant_read_back
adr: null
---

# Namespace counts cannot exclude the descendants sibling rows require

## Observed failure

Run `20260903T024933Z-7f07e7` on `artifacts/room_1046_opening` (main `1bea17b`): the
layer-1 rematerialization (`20260903T023810Z-8509f9`) attested a view whose unit
`camera_targets` bound `ctl-targets-parent-count` — `object_count eq 1` over the literal
role `camera.targets` — beside keyframe-schedule and projected-origin rows on
`camera.targets.window_target` and `camera.targets.door_target`. The builder created the
parent Empty and both children in its first call and read `object_count=3 target = 1 ± 0`
with the generic definition "number of objects whose bvfx_role matches roles".
`contract_result` listed the three matched roles under `roles` and an empty string under
`controls`, so the model read the matched list as the selector, then spent three
`run_bpy` calls retagging the children to `zzz.unrelated` and `camera.other_branch`
"to test matcher behavior". Every call was answered by the scope-violation card; no
`cannot_express_in_scope` arrived in 590 s, and the run was interrupted (typed 143).

## Root cause

1. The one canonical semantic matcher (HIR-0147) makes a literal selector name its exact
   tag and every dotted descendant. An `object_count` upper bound over a namespace whose
   descendants sibling rows require is therefore unsatisfiable in every scene that
   satisfies the siblings, yet neither `validate_row_set` at materialization nor the plan
   gate compared the two.
2. The object_count read-back reported only the count; the matched objects' roles were
   surfaced under the same key the authored selector uses, an untagged host contributed an
   empty control string, and the descendant rule was stated nowhere the builder could see.

## Decision criteria

- Fail closed on contradictory authority before builder spend (AGENTS.md: contradictions
  no single row reveals are refused at authoring, materialization, and the plan gate).
- Rejections teach: name the matcher rule, the rows that require the descendants, and the
  legal fixes.
- Close the loop: a measurement the builder cannot act on without an experiment is a
  missing instrument, not a prompt problem.

## General mechanism

1. `evidence/scene_checks/validate.namespace_count_contradictions(rows)`: for every
   `object_count` row with role selectors and an upper bound (`eq value ± tol`,
   `max hi`, `band hi`), collect the roles of other rows that the canonical matcher places
   under those selectors without equalling them — same-layer rows always, every layer's
   rows when the count row is `persistent`. Hosts carry one role token, so roles with no
   descendant in that set each need a host; when that minimum exceeds the bound the row is
   refused with the matcher rule, the requiring row ids, the minimum, and the two legal
   fixes (count a leaf role such as `<selector>.root`, or raise the bound).
   `validate_row_set` appends it, so materialization staging, patching, finalization, and
   the plan gate all refuse it.
2. The Blender probe's `object_count` reading carries a `note` naming the literal
   selector(s) and every matched descendant `name(role)`, the evidence row carries
   `selector_roles` / `selector_control_roles` beside the matched `roles` / `controls`,
   and `controls` lists only tagged hosts. The automatic scene-contract card appends the
   note under the failing line.

## Rejected patch-level alternatives

- Teaching the builder to call `cannot_express_in_scope` sooner (the authority is wrong
  before any builder exists).
- Special-casing `camera.targets` or parent/child Empties (shot-shaped).
- Making a literal selector exact (would break every aggregate subject contract that
  HIR-0147 introduced).

## Validation

- `src/tests/unit/test_namespace_count_bounds.py`: the reproduced row set is refused with
  the teaching message through `validate_row_set`; a leaf-role count, a raised bound, a
  lower bound, and a nested descendant sharing one host pass; other-layer descendants bind
  only a `persistent` count row.
- `src/tests/integration/test_real_blender_namespace_count.py`: a real Blender scene with
  a parent and two tagged children reads 3 for the namespace count with the descendant
  note, selector fields, and no empty control tag; a leaf-role count reads 0 with the
  selector miss.

## Release and rollback

Unreleased. Evidence rows gain two additive fields; no persisted artifact changes shape
otherwise. Rolling back restores the silent count and the finalize-time discovery.

## Remaining limitations

- Wildcard selectors on the requiring rows are not counted toward the minimum; only
  literal descendant roles bound the count.
- The rule bounds counts from below only; an `eq 3` over a namespace whose siblings
  require four hosts is caught, but a count that is merely surprising is not.
