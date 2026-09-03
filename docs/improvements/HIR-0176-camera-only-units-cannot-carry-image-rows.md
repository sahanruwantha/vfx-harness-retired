---
id: HIR-0176
title: Camera-only units cannot carry image rows, and camera-dependent instruments teach the provider rule
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: image_evidence_authored_before_optical_signal
mechanism: functional_image_rows_are_bootstrap_debts_and_world_writes_are_volume_work
adr: null
---

# Camera-only units cannot carry image rows, and camera-dependent instruments teach the provider rule

## Observed failure

Run `20260903T002758Z-1b6807` on `artifacts/room_1046_opening` (main `62fd434`), layer 1
after the HIR-0175 rematerialization. The replacement view bound claim `cam-grayscale`
(`asserts: image`, required) on the camera-providing unit `camera_rig` to two
`render_region_stat` rows (`gray-r`, `gray-b`: full-frame luminance band 90..112 at
frame 175). Materialization validation, the finalization gate, and the plan gate all
passed the view: the image-signal and image-subject bootstrap gates (HIR-0110, HIR-0160)
count only `image_contract` bindings as image debts, and these rows are scene contracts
of a registry image kind.

On a camera-only replay prefix the plate is black, so the builder created a World
(`bpy.data.worlds.new`, `use_nodes`, a grey Background) to push the band into range;
the payload classifier (HIR-0112) has no rule for World creation, the camera cluster
accepted the payload, and the unit sealed a World background into layer 1's camera
script, a shading decision that belongs to the look layer. Two rounds earlier the
same unit had rendered before any camera existed and received Blender's bare
`RuntimeError: Error: Cannot render, no camera`; the pre-camera control unit had
received `scene has no active camera to project through` with a worker traceback.

## Root cause

1. `FUNCTIONAL_KINDS` (`control_render_response`, `frame_delta`, `render_region_stat`)
   are registry-declared image instruments that owe raster (HIR-0114), but the bootstrap
   gates derived image debts from `image_contract` bindings alone, so a required image
   claim over functional rows could publish on a unit whose replay prefix has neither an
   optical-signal producer nor a rendered carrier.
2. `_script_call_family` classified lights, cameras, meshes, and materials but not
   `bpy.data.worlds.new`, although the row-level classifier already maps World graph rows
   to the `volume` family.
3. The projection and render instruments reported a missing camera as a raw exception
   without the rule that camera-relative evidence is due at the camera-providing unit.

## Decision criteria

- One image-debt definition for both gates: anything that measures pixels.
- One family classification per Blender data family, shared by rows and payloads.
- Rejections teach: the missing-camera case names where the evidence is due and what a
  pre-camera producer owes instead.

## General mechanism

1. `FUNCTIONAL_KINDS` and the absolute subset `PIXEL_STATISTIC_KINDS`
   (`render_region_stat`, `control_render_response`) move to the domain leaf
   `domain/evidence_kinds.py`; `image_signal.functional_image_debt_ids` collects the
   scene-contract ids of the absolute kinds bound by a required image claim, and
   `_family_dependency_gaps` counts them with `image_contract` debts for both the signal
   and the subject bootstrap. Materialization validation and the plan gate refuse the
   camera-only case naming the rows.
2. `bpy.data.worlds.new` classifies as `volume` in `script_write_family_evidence`, so a
   camera-cluster payload that creates a World is a mixed payload and fails before
   mutation.
3. `checks.NO_CAMERA_RULE` is appended to the projection instrument's missing-camera
   error, and `h_render` refuses a camera-less scene with the same rule before Blender
   raises.

## Rejected patch-level alternatives

- Teaching the builder not to create Worlds (prompt wording for a classifier gap).
- Accepting a grey World as a legitimate way to pay a luminance band (padding: the
  claim was never about the camera).
- Treating a black plate as satisfying a luminance band (self-certifying evidence).

## Validation

- `src/tests/unit/test_camera_only_image_rows.py`: functional rows under a required
  image claim are image debts; a camera-only prefix with one is a bootstrap gap that an
  earlier signal provider clears; `bpy.data.worlds.new` classifies as `volume`.
- `src/tests/unit/test_projection_integrity.py` and
  `src/tests/unit/test_checkpoint_fidelity.py`: the no-camera rule is taught by both
  instruments.
- Live: the next materialization that binds a functional image row on a camera-only
  unit must be refused at staging.

## Release and rollback

Unreleased; no schema change. Rolling back re-admits functional image rows on
signal-less prefixes and World creation inside camera clusters.

## Remaining limitations

- The sealed layer-1 camera script of run `1b6807` carries a World background authored
  under the gap; the rows are layer-lifecycle, so later look layers may replace the World,
  and a reviewed rematerialization of layer 1 under this gate removes the rows.
- Only World *creation* is classified; editing an existing World's node tree from a
  camera unit is still governed by role scope alone.
- `frame_delta` is not a bootstrap debt: it is relative between two frames and its
  image payments are refuted by the pre-unit adversary (HIR-0048). A required
  `frame_delta` *lock* claim (`max hi`) on a signal-less prefix still reads a vacuous
  zero; that vacuity is not yet refused at authoring.
