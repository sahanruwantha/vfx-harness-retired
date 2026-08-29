---
id: HIR-0094
title: Point projection is camera-owned alignment evidence
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: static_target_fitted_after_camera
mechanism: camera_owned_point_projection_and_target_first_causality
adr: ADR-0003
---

# Point projection is camera-owned alignment evidence

## Observed failure

Run `20260829T062819Z-fb22a3` removed proxy mesh and published a static
`camera.target`, but ordered `camera_rig_path` first and made
`camera_target_alignment` depend on it. The target unit owned all
`projected_origin_x/y` rows. It could therefore choose a static world point after the
camera path existed and fit the point to the projection measurements. Later geometry was
supposed to build to a target the camera had consumed; instead the target was derived
from the camera after the fact.

The plan was numerically stronger than the rejected moving sphere, yet still reversed
the named interface dependency. Point projection alone did not identify which side of
the camera/subject relation was authorized to repair the measurement.

## Root cause

HIR-0090 added the missing point instrument and removed invented visibility debt, but
allowed the control producer itself to own a projection claim. Both moving the subject
and moving/aiming the camera change the scalar. For a non-rendered control interface,
screen projection must certify camera alignment; fixed world placement is a separate
scene fact.

## General mechanism

`projected_origin_x/y` required claims are now repaired only by a unit that declares the
camera capability. A fixed Empty/control producer proves existence, world transform, and
absence of animation with scene-domain evidence. The camera unit depends on that
producer and owns projection through the active camera. Materialization validation, the
incremental staging boundary, and the independent plan gate all enforce the same rule.

This makes the causal order unambiguous without role-name or axis-keyword inference:
produce the point, consume/depend on it, then align the camera. A unit cannot fit a target
after seeing the camera and call that camera continuity.

## Validation

Heterogeneous product-target and motion-control fixtures publish a fixed control first
and a camera-owning projected-alignment successor second, with no visibility proxy.
Injected control-owned projection is refused by materialization. An independent plan
gate fixture emits `point-projection-owner`, and incremental staging leaves the candidate
unchanged when the same invalid ownership is proposed. The full repository suite passes
535 tests. The production gate rejects the selected reverse-causal view with eight
`point-projection-owner` blockers. Production acceptance requires
rematerializing Layer 1 target-first and a clean plan gate before any builder runs.

## Release and rollback

The evidence row schema is unchanged; the ownership rule is stricter. Existing selected
plans whose control producers own point projection must be rematerialized. Rollback
would restore reverse-causal target fitting, so it is unsafe.
