---
id: HIR-0184
title: Camera layers author every downstream framing row and prove it feasible before sealing
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: camera_sealed_without_downstream_framing_proof
mechanism: downstream_subject_coverage_and_freeze_time_feasibility_proof
adr: null
---

# Camera layers author every downstream framing row and prove it feasible before sealing

## Observed failure

Run `20260903T081518Z-9a32ab` on `artifacts/room_1046_opening` (main `4350515`): layer 2's
`building_shell` ended in a typed `cannot_express_in_scope` (`hf-7488c9e81ae1914b88fd`,
fault owner `camera_rig`) because no rigid mass under the sealed camera path could satisfy
the frame-1 height cap (0.35) and the frame-113 height floor (0.55) the reference stills
demand: the camera dollies 5 units and zooms from 35 to 36.6 mm between those frames, far
short of the 1.57× apparent-height contrast. Layer 1 had authored deferred `exterior.*` rows
at frames 1 and 38 only; at frame 113 it covered composition with a layer-3 window row, so
the gate passed, the camera sealed, and the conflict surfaced one layer and $7 later.
HIR-0183 gave the geometry builder the feasibility instrument; the camera builder, the
earliest owner, never had the obligation or the proof.

## Root cause

1. Coverage rule too weak (plan/contract defect made representable): composition coverage
   required one rendered subject per judge frame, so a camera layer could satisfy frame 113
   with a later subject that is not judged there and omit the subject that is.
2. Missing instrument at the camera boundary: no read-back told the camera builder whether
   any later subject could pay its deferred rows under the path it was building, and
   `bbox_feasibility` refused to run without a host carrying the roles, which a camera
   layer never has.

## Decision criteria

- Errors that propagate through stacked layers get the strictest gates where generation is
  most tempting (camera, root-layer framing).
- Measure, don't estimate; close the loop after every mutation.
- Obligations compile from typed DAG authority (judge lists, reserved roles), never from
  prose.

## General mechanism

1. `domain/work_units/subject_framing.successor_judge_rows` and
   `uncovered_downstream_subjects`: a camera-providing layer that owns projected
   composition must author, at every judge frame it shares with a later layer, a persistent
   `bbox_*` row over that layer's reserved namespace (canonical matcher), owner and fault
   owner the camera layer, `activates_at` that layer. `compile_deferred_subject_activation`
   lists these `framing_obligations` with the reference still per frame, and the
   materialization kickoff names them. The scope matches the existing composition-coverage
   rule: a camera provider without the `projected_composition` domain owns no framing.
2. `orchestration/jit_materialization/validate_framing.framing_findings` reports each gap
   (and each deferred-activation gap) as a collectable finding, so the stage call lists it
   and finalize refuses it (HIR-0180); the plan gate reports it as `composition-coverage`
   on published views.
3. `check_bbox_feasibility` derives its search bounds from the union of the camera frustums
   over the bound frames when nothing carries the roles (`proxy_bounds_from_frustums`),
   seeds the solver with boxes on each frame's optical axis at log-spaced depths
   (`frustum_seed_boxes`, interleaved across frames), and the solver gives every seeded
   start a fair share of the evaluation budget, so a camera-only scene can be decided.
4. After every mutation of a camera-providing unit, the harness runs the proxy solver over
   the camera-authored deferred rows per later layer and prints the verdict as read-back:
   feasible names the proxy, infeasible says no single rigid box can pay those rows under
   this path and asks for a path change before sealing. The verdict is one axis-aligned
   box, so it is not turned into an abstention: run `20260903T100335Z-fa5dbb`'s
   building_mass read infeasible twice and passed with a four-part mass. The same
   correction makes HIR-0183's gate advisory after its measurement.

## Rejected patch-level alternatives

- Relaxing layer 2's bands (the reference decides them).
- Telling the camera builder in prose to "leave room for later subjects".
- Running the solve only at the geometry layer (HIR-0183 alone): the verdict arrives after
  the camera has sealed and a whole layer has been paid for.

## Validation

- `src/tests/unit/test_downstream_subject_coverage.py`: successors and obligations compile
  from the sparse DAG; shared frames without a matching camera row are gaps.
- `src/tests/unit/test_bbox_feasibility_gate.py`: an infeasible verdict clears the streak
  and reopens mutation instead of forcing an abstention.
- `src/tests/unit/test_subject_framing_coverage.py::test_camera_layer_candidate_reports_uncovered_downstream_subjects`:
  the terminal validator names the later subject and its still, and is silent once the row
  exists.
- `src/tests/integration/test_real_blender_bbox_feasibility.py::test_camera_only_scene_derives_bounds_from_the_frustums`:
  a dollying camera with no subject proves the reference bands feasible; a static camera
  proves them infeasible, both from frustum-derived bounds.

## Release and rollback

Unreleased. Camera layers materialized before this record lack the obligations; their next
rematerialization authors them. Rolling back restores the one-layer-late discovery.

## Remaining limitations

- The proxy is one axis-aligned box per later layer; subjects that must be several
  separated masses can read infeasible while a split subject would pass.
- Targets are measured by the materializer from the still; the proof covers geometry the
  camera can frame, not whether the still's composition is the brief's intent.
- A feasible verdict from frustum-derived bounds may rest on a small proxy close to the
  lens (any camera translation admits near-field parallax), and an infeasible verdict only
  rules out a single rigid box; the read-back names the proxy so the builder can see both.
  A typed subject-scale prior and a multi-part proxy family would sharpen the verdicts.
