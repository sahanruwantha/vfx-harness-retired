---
id: HIR-0085
title: Camera-dependent evidence needs an available camera
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: impossible_evidence_dependency
mechanism: camera_capability_dependency_gate
adr: null
---

# Camera-dependent evidence needs an available camera

## Observed failure

Run `20260829T033421Z-31ca18` materialized Layer 1 as exterior geometry while the
global DAG deferred the camera to Layer 3. Its root `tower_mass_unit` bound
`tower-onscreen-frame38`, a required `visible_fraction` contract. The existence row
passed, but visibility had no value because the cumulative scene had no camera. A
temporary diagnostic camera was correctly refused by the unit scope guard; after the
builder session ended, the unconditional render crashed Blender with `Cannot render, no
camera`. No unit or checkpoint was accepted after about 21 minutes of planning and build
work.

## Root cause

The plan gate's dependency check classified only bbox metrics as camera-dependent.
`visible_fraction` calls the same active-camera projection/raycast boundary, while
parallax and functional render contracts also require a camera. The metric implementation
knew this, but planning kept an incomplete private set. The selected plan was therefore
structurally impossible even though both materialization preview and `vfx evals plan`
reported clean.

## Decision criteria

The fix must be derived from metric semantics, reject the plan before model or Blender
budget is spent, preserve legal geometry ownership of visibility repair, and accept a
camera supplied either by the same unit, its dependency closure, or an earlier layer. It
must not infer camera authority from layer or shot names.

## General mechanism

The canonical scene-check registry now publishes `CAMERA_REQUIRED_KINDS`: bbox,
`visible_fraction`, `parallax_displacement_profile`, and every functional render kind.
The evidence-coherence gate resolves every scene-contract binding against that registry.
If the producing unit has no camera provider in its same-layer transitive dependency
closure and no earlier materialized layer provides one, publication blocks with
`composition-bootstrap`, naming the kinds and contract ids that are impossible to
evaluate.

Composition-context coverage remains bbox-specific; requiring a camera is an execution
precondition and does not let visibility or a render statistic masquerade as a framing
contract.

## Rejected patch-level alternatives

Allowing the geometry builder to create an undeclared temporary camera broadens mutation
authority and makes evidence depend on state absent from replay. Catching Blender's final
render exception would improve the symptom but still spend the whole unit on an
unsealable plan. Adding only `visible_fraction` to another local set would leave parallax
and functional render contracts with the same defect.

## Validation

`tests/unit/test_plan_improvements.py` includes the exact fail-without shape under a
different subject namespace: a camera-less unit binding `visible_fraction` is refused,
then accepted when that unit declares `provides: ["camera"]`. A second fixture proves a
camera-less `render_region_stat` is refused by the same mechanism. The pre-existing bbox
bootstrap fixture remains green, along with the plan-improvement, proxy-evidence,
evidence-vocabulary, and visibility-repair suites (62 tests).

The production acceptance condition is a fresh selected plan, clean gate, successful
Layer 1 build, and cumulative empty-scene replay. Those results are recorded after the
rerun; the original failed run remains immutable evidence.

## Release and rollback

This is stricter publication validation with no persisted schema change. Rollback removes
the shared capability set and the broader gate, but restores plans whose evidence cannot
execute.

## Remaining limitations

This mechanism prevents an impossible evidence DAG; it does not by itself decide whether
the global planner should move camera ownership earlier or revise the judged layer. A
separate lifecycle improvement may still be warranted so an unexpected no-camera state
ends as a typed build failure rather than a renderer traceback.
