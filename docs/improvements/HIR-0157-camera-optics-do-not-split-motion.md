---
id: HIR-0157
title: Camera optics evidence must not split same-host motion
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: camera_property_row_blocks_motion_absorption
mechanism: camera_residual_absorbs_control_and_keyframe
adr: null
---

# Camera optics evidence must not split same-host motion

## Observed failure

Layer 1 materialization run `20260830T142640Z-10b425` staged a camera-providing unit
that mutated only `camera.rig`, bound `object_property` `data.lens`, and bound
same-role `curve_derivative_max` / `transform_return_delta` / `radial_distance_trend`
rows. Staging refused `mixed_clusters`: derived write-clusters
`camera.rig/camera/camera` and `camera.rig/control_host/keyframe`.

HIR-0112 already required camera-host placement and motion to remain one camera
family. The live unit was that legal shape plus a lens row. The rejection taught the
materializer to split a coherent camera path.

## Root cause

`_families_for_namespace` absorbed control and keyframe families into `camera` only
when the derived set was a subset of `{control, keyframe}`. `data.lens` is a typed
camera-property witness, so the set became `{camera, keyframe}` and the subset guard
failed. `host_class_for_family("keyframe")` then published a sibling
`control_host/keyframe` cluster. Optics evidence blocked the motion exception it
should have joined.

Geometry plus keyframe remains additive (HIR-0112). This hole is camera-only.

## Decision criteria

- A camera provider on one mutation namespace publishes one camera write-cluster for
  camera-property, placement, and motion evidence on that host.
- Unrelated families (light, mesh, shading, volume, compositor) on that namespace
  remain a second cluster and still fail mixed publication.
- The mechanism must not encode a shot id, unit id, or fixed frame.

## General mechanism

When the residual producer family is `camera`, discard `control` and `keyframe` from
the derived set and add `camera`. A camera-property row no longer prevents motion
absorption. Light and other non-camera families are unchanged.

## Rejected patch-level alternatives

- Keep the subset guard and tell the materializer to drop lens or motion from the
  unit: that splits a legal camera path to satisfy a derivation hole.
- Classify `curve_derivative_max` as `camera` for every role: non-camera motion would
  lose the keyframe family that geometry units must keep additive.
- Collapse every family on a camera provider into camera: a light row would vanish.

## Validation

`test_camera_provider_absorbs_same_host_placement_and_motion` still passes.
`test_camera_provider_absorbs_optics_and_motion_on_the_same_host` pins `data.lens`
plus `curve_derivative_max` to exactly `camera.rig/camera/camera`.
`test_camera_provider_does_not_absorb_unrelated_light_work` keeps `data.energy` as a
second cluster. The incremental staging fixture that previously used lens-plus-motion
as a mixed-cluster stand-in now uses light energy so it still refuses a genuine mix
before candidate bytes change.

Verification on 2026-08-30:

- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/python -m pytest -q tests/unit/test_atomicity.py` — `45 passed` (with the
  staging fixture, `45 passed in 3.18s`).
- `.venv/bin/python -m pytest -q` — `671 passed in 44.98s`.
- `.venv/bin/vfx --help` — exit 0.

## Release and rollback

No schema migration. Rollback would again refuse a camera unit that binds lens and
path on the same host.

## Remaining limitations

Host class is still derived from instrument family, not from a live Blender object.
Consumed `projected_origin_*` rows remain observation and do not grant mutation of
the measured selector.
