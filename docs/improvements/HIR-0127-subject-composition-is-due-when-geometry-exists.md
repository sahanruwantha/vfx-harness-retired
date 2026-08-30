---
id: HIR-0127
title: Subject composition is due when geometry exists
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: root_layer_framing_seals_on_vacuous_self_aim
mechanism: deferred_subject_composition_and_cross_layer_camera_fault
adr: ADR-0004
---

# Subject composition is due when geometry exists

## Observed failure

Room 1046 Layer 1 camera sealed 5.0 on smoothness/ramp bounds, `animation_count`,
`projected_origin_x/y` of `camera.target` in a 0.2–0.8 band, and one-sided start
scalars (`cam-start-height >= 8` → 44.6, `cam-start-distance >= 15` → 15.8). Height
44.6 over a target at 5.5 at distance 15.8 is a ~70° look-down. Per-beat ref
compositions (R57–R64) closed as hard-constraint decisions with empty contract ids.
Layer 2 then sealed a two-facade stage set against vis floors and counts. Scratch
plates at the Layer 2 judge frames show roof/void, not the ref elevations. Zoom,
crop, and split-ref tools existed; appearance deltas were redacted on executable-only
units, and no bound row could fail the framing.

The control system worked as built. Framing versus the refs was nobody's bound
evidence.

## Root cause

Three independently legal shapes conjoined into an unjudgeable camera:

1. A `projected_origin` band covering more than half the normalized frame is almost
   "the aim dummy is on screen." Vacuous vis already fails closed (`lo <= 0`); origin
   and bbox bands did not.
2. Composition-coverage accepted any `PROJECTED_CONTEXT_KINDS` row, including
   `projected_origin` of a camera-only host. Alignment is not subject framing
   (HIR-0094).
3. ADR-0004 already states that scene-dependent targets evaluate in the real
   cumulative scene, and materialization already documents `owner_layer` on the
   author with `activates_at` on a later layer. The planner ticket defaulted both
   to "this layer." Layer 1 therefore sealed before any building existed, and Layer 2
   `cannot_express` could not name the camera: `fault_owner_options` listed only
   same-layer ancestors.

## Decision criteria

- Vacuous normalized `band` width is a schema error, not a planner preference.
- `projected_origin` of a camera-only host does not satisfy composition-coverage.
- Subject `bbox_*` at a judge frame does. When the subject does not exist yet, the
  camera layer authors the row with `activates_at` on the earliest geometry layer,
  `lifecycle: persistent`, and `fault_owner` equal to the camera owner layer.
- Layer 1 does not evaluate those ids (inactive `active_for`). Claim-closure still
  requires them bound (composition_context on the camera unit is enough).
- Geometry units that mutate the measured roles freeze-protect the active deferred
  rows, so they are judged in the cumulative scene.
- `cannot_express_in_scope` may name an earlier-layer camera provider. The finding
  records those ids; same-layer `affected` stays local. Replan of the camera layer
  remains a separate transaction after the amended plan — this HIR does not silently
  invalidate another layer's durable state.
- No brief keyword scanning. No shot names, fixed frames, or display-name selectors
  in core code.

## General mechanism

`validate_row` rejects a projected `band` whose `(hi - lo)` is greater than half the
normalized frame. Composition-coverage counts only `bbox_*` of a rendered subject, or
a camera-owned bbox whose `activates_at` is a later layer in the DAG. Deferred rows
must be persistent and keep `fault_owner` on the authoring camera layer. Builder
evaluation drops inactive ids so the camera unit can seal on camera-local evidence.
Geometry extra-required ids include matching deferred composition rows. Fault-owner
options include earlier-layer `provides: ["camera"]` units. Hypothesis falsification
stores those ids without requiring them to be units on the active layer.

## Rejected patch-level alternatives

- Prompting the builder to zoom or compare harder. The one real compare redacted
  appearance deltas; executable rows still passed.
- Reopening Layer 1 against the current rows. It reseals 5.0 on the same evidence.
- Inferring composition requirements from brief keywords or ref filenames.
- Letting Layer 2 scale the set until a screen band passes. World-space pins remain
  plan-side; this HIR only routes the screen-composition fault to the camera.
- Auto-replanning Layer 1 from a Layer 2 finding. Durable state is per-layer;
  consuming the finding on Layer 2 must not write Layer 1's ledger.

## Validation

Vacuous 0.2–0.8 origin/bbox bands fail `validate_row`. A camera-only
`projected_origin` cover fails composition-coverage; a deferred subject bbox with
`activates_at` on a later layer passes. Inactive deferred ids are omitted from a
camera unit's due set. Geometry extra-required includes the matching deferred bbox.
`cannot_express` accepts an earlier-layer camera id from compiled options.
Falsification records that id as `fault_owner_units` without treating it as a
same-layer affected seed.

## Release and rollback

Stricter publication and evaluation with no persisted schema version bump. The
falsification payload gains an optional `fault_owner_units` list; older records
parse with an empty tuple. Selected plans whose origin bands are vacuous or whose
composition-coverage is only self-aim fail the gate until rematerialized. Rollback
restores vacuous self-aim sealing.

## Remaining limitations

Coupled camera pose (pitch vs one-sided height/distance) remains plan authoring, not
a new metric. World-space scale pins that stop geometry compensating are likewise
plan-side. Look/image debt stays on the look-owning layer (HIR-0110). Consuming a
Layer 2 finding still requires an explicit Layer 1 replan after the amended bundle.
