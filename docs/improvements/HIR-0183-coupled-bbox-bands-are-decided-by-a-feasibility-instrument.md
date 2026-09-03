---
id: HIR-0183
title: Coupled bbox bands are decided by a feasibility instrument, not a rebuild search
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: builder_search_against_infeasible_projected_bands
mechanism: proxy_box_feasibility_check_with_mutation_fence
adr: null
---

# Coupled bbox bands are decided by a feasibility instrument, not a rebuild search

## Observed failure

Run `20260903T081518Z-9a32ab` on `artifacts/room_1046_opening` (main `4350515`): the
layer-2 unit `building_shell` owed `bldg-bbox-f1` (`bbox_height` band 0.05..0.35 at
frame 1) and `bldg-corner-bbox-f113` (`bbox_height` at least 0.55 at frame 113) on
`exterior.mass`, plus layer 1's persistent width rows at frames 1 and 38. Under the sealed
layer-1 camera path no rigid mass satisfies both heights: the builder rebuilt the mass 46
times over 32 minutes ("rebuilt v39 … FINAL-v2", best 0.533 against 0.55 while frame 1
sat at the cap) and only then recorded `cannot_express_in_scope` — 154 turns and $6.95
for a verdict a deterministic search could return in one call. The typed stop that
followed (`hf-7488c9e81ae1914b88fd`, fault owner `camera_rig`) was correct; the cost of
reaching it was the defect.

## Root cause

Missing instrument. Whether any axis-aligned box inside declared bounds projects inside
every bound `bbox_*` band at its frame is a deterministic question over the sealed camera,
but the harness offered only single-configuration probes (`check_scene` framing,
`projection`, `contract_result`), so the builder searched by hand. Nothing bounded that
search either: unlike the density branch (HIR-0082), a bbox row could fail after any
number of mutations without the harness demanding a measurement.

## Decision criteria

- Measure, don't estimate: expose the instrument that answers the decision.
- A recurring guess is a missing tool; a closed causal branch must fail closed into typed
  abstention, never into more attempts.
- The instrument creates no scene objects and never moves the shot camera.

## General mechanism

1. `blender/bbox_feasibility.solve_box_feasibility` (pure, loaded by path in the worker
   like `geom`) searches centre and size within bounds by deterministic seeded restarts
   with shrinking coordinate descent, minimising the largest residual over every row; it
   returns the best box, each row's value and residual, and the binding rows.
2. `blender/checks.check_bbox_feasibility` evaluates the camera matrix once per bound
   frame, projects the proxy's eight corners through `frustum_union_ndc`, derives bounds
   from the hosts carrying the roles when none are given, and reports feasible or
   infeasible with the binding rows.
3. `check_scene(kind='bbox_feasibility', roles=[…])` compiles the unit's bound `bbox_*`
   rows for those roles, runs the worker check, records the verdict in the comparison
   state, and reports the satisfying box or the infeasible binding rows with the legal
   next action (`cannot_express_in_scope` naming the camera provider).
4. `blender/tools/bbox_feasibility_gate`: after six consecutive mutations that leave the
   same bbox row failing, `run_bpy` is refused until the instrument has run for that row;
   an infeasible verdict refuses further mutation and names the abstention.

## Rejected patch-level alternatives

- Prompting the builder to "try fewer configurations" (a judgment ask for a mechanical
  question).
- Loosening the authored bands (the finding, not the builder, decides that).

## Validation

- `src/tests/unit/test_bbox_feasibility_solver.py`: operator windows and residuals; a
  consistent band set yields a satisfying box deterministically; contradictory bands are
  proved infeasible with binding rows within the evaluation budget.
- `src/tests/unit/test_bbox_feasibility_gate.py`: the streak fences mutation after six
  failures, an infeasible verdict names the abstention, a feasible verdict reopens
  mutation, and non-bbox rows never count.
- `src/tests/integration/test_real_blender_bbox_feasibility.py`: a real moving camera
  proves a consistent set feasible and a contradictory set infeasible.

## Release and rollback

Unreleased. Rolling back restores the unbounded rebuild search.

## Remaining limitations

- The proxy is axis-aligned; a subject that must rotate to satisfy a band can read
  infeasible while a rotated mass would pass, so the builder may widen bounds or rotate
  and re-measure before abstaining.
- The materialization gate does not yet run the solve for deferred rows against an
  accepted camera layer; that would move the verdict before builder spend.
