---
id: HIR-0114
title: Executable-only evidence precedes raster
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: executable_only_unit_rendered_before_scene_verdict
mechanism: registry_derived_raster_boundary
adr: null
---

# Executable-only evidence precedes raster

## Observed failure

Room 1046 held-out run `20260829T172713Z-cee3d8` built the dependency-root
`camera_target` unit as a fixed Empty. Its sole bound `object_property` contract passed
at 5.5 inside the declared 3..8 band, `list_keyframes` proved no animation, and the live
tool server emitted `UNIT HANDOFF READY`. The unit declared `look_capabilities: []` and
owed no image contract. The builder nevertheless entered `round 1/2 — rendering +
critiquing`, called Blender before its camera-producing successor existed, and crashed
with `Cannot render, no camera`. No checkpoint sealed after nine builder turns, $0.4782,
and about 48 seconds of otherwise successful work.

## Root cause

HIR-0032 and HIR-0039 prevented a visual critic from deciding executable-only units and
look-less composition, but both live `build_unit` iteration and `_verify_script`
canonical replay still called `_stash_render` before reaching `_executable_unit_verdict`.
The evidence boundary was correct and the scheduling boundary was not. Catching the
renderer exception would leave the same impossible ordering and still spend raster work
where no pixel claim exists. Classification: orchestration/evidence scheduling defect.

## Decision criteria

- A unit whose required claims are all `executable_required`, whose bindings are all
  `scene_contract`, and whose declared look capabilities are empty reaches its verdict
  without raster or a critic.
- Image-contract bindings, look ownership, non-executable required judgment, and
  registry-declared functional image metrics continue to require raster.
- The rule is derived from typed unit authority plus the canonical metric registry; it
  does not inspect shot, layer, role, axis, or object names.
- Live evaluation and empty-scene canonical replay exercise the same boundary.
- Missing or malformed contract authority fails as executable evidence; rendering is
  not a repair for it.

## General mechanism

`_unit_requires_raster` derives the evidence path from the active unit and
`scene_checks.FUNCTIONAL_KINDS`. The live loop skips `_stash_render` for raster-free
units, evaluates scoped scene/interface evidence directly, snapshots the candidate, and
retains the existing bounded revision/finalization flow. `_verify_script` replays the
script from empty and evaluates every claimed frame without producing PNGs or motion
strips. `_render_evidence` omits image and functional-render producers when no raster
handle exists while retaining static, temporal, projected, interface, and worklist
evidence.

Finalization and repair pass the same derived bit into `probe_candidate`. The disposable
probe still replays priors plus the current script and returns rig state, evaluated role
transforms, and authoritative scene rows, but it does not create EEVEE/Workbench images
for raster-free units. This closes the mutation/read-back loop without inventing a pixel
debt.

The same predicate applies to HIR-0039's synthetic look-less composition unit, so fan-in
no longer creates throwaway black plates before its executable verdict.

## Rejected alternatives

- Catch `Cannot render, no camera`: this suppresses one symptom and still makes every
  executable-only unit pay an unauthorized raster debt.
- Create a temporary camera or light: those roles belong to other units/layers and would
  be absent from deterministic replay authority.
- Treat every `scene_contract` as raster-free: functional render metrics are typed scene
  rows with image evidence domains, so the registry must decide.
- Prompt the builder to stop: the unconditional render happens after the model session.

## Validation

- `test_executable_scene_unit_does_not_require_raster` proves a heterogeneous control
  fixture chooses the raster-free path.
- `test_functional_scene_contract_still_requires_raster` proves a registry functional
  metric keeps raster authority.
- `test_executable_canonical_replay_never_calls_render` injects a renderer that raises
  and proves empty-scene canonical replay passes solely on bound scene evidence.
- `test_live_unit_render_is_guarded_by_typed_raster_need` pins the producing live-loop
  branch.

Production acceptance: held-out run `20260829T175349Z-882888` rebuilt both Layer 1
units on the completed mechanism after the prior checkpoints were explicitly invalidated.
`camera_target` passed its live executable row, finalizer `probe_candidate` returned
`raster_required: false` without images, and canonical empty-scene replay passed. Its
camera-producing successor `camera_path` then passed 23/23 live contracts, its candidate
probe likewise returned `raster_required: false`, and canonical replay passed all seven
claimed frames at 5.0. The composed `build/01_camera_path.py` replay also passed every
frame at 5.0 without raster or a critic. The run exited 0 and published Layer 1.

The artifact index contains no evidence render from the live verdict, candidate probe,
unit canonical gate, or composed canonical gate. Its sole PNG is the builder's separately
declared matcap diagnostic. This distinguishes an optional model instrument from raster
being required to earn the harness verdict.

Implementation commit: `8b6b720` (`fix: evaluate executable units before raster`).

## Release and rollback

No persisted schema changes. Rollback restores unconditional raster before executable
judgment and recreates impossible pre-camera root units, so it is unsafe.

## Remaining limitations

Unexpected renderer failures on genuinely raster-owning units still need typed process
reporting. This HIR removes raster from units that never owed it; it does not redefine a
failed image contract as scene evidence.
