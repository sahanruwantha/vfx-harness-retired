---
id: HIR-0090
title: Control points need point projection, not proxy mesh
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: surface_evidence_forces_proxy_geometry
mechanism: typed_point_projection_and_surface_owner_gate
adr: ADR-0003
---

# Control points need point projection, not proxy mesh

## Observed failure

In build run `20260829T053222Z-b96457`, unit `camera_target_aim` was scoped to
`camera.target` and `camera_target_transform` and declared no geometry capability. Its
materialized claims nevertheless bound sixteen `bbox_center_x/y` rows plus four
`visible_fraction` rows. The builder first created the natural representation—an Empty.
Visibility measured 0.0 because Empty hosts have no surface. It then replaced the Empty
with a 12-vertex icosphere, after which all 17 bound rows passed.

The result was mechanically centered but semantically false: the sphere was animated a
fixed distance in front of the moving camera, so its bbox was identically centered at all
four frames. It did not establish the fixed hero-window point later architecture was
supposed to meet. The run was interrupted before freeze; no target checkpoint was
accepted.

## Root cause

The evidence vocabulary had only bbox projection, which measures rendered geometry, and
visibility, which ray-tests surfaces. It had no projection instrument for an object
origin. Materialization also required a visibility row at every judge frame regardless
of whether a unit owned any rendered subject. The validator therefore made a control
point inexpressible while leaving proxy mesh as the cheapest passing implementation.

Atomicity declared the unit a control-host write cluster, but the evidence gate did not
bind surface metrics back to the geometry capability. Host class could change during the
build without contradicting the plan.

## General mechanism

The canonical scene vocabulary now includes `projected_origin_x` and
`projected_origin_y`. Each projects the world origin of exactly one semantically selected
object through the active camera at an explicit frame. Empty/control hosts are legal.
The measurement is normalized in the same top-left coordinate system as bbox evidence,
requires a camera, and certifies projected placement only—not visibility or occlusion.

`bbox_*` and `visible_fraction` are now explicitly surface evidence. When a required
claim targets a role its repair owner mutates, materialization and the deterministic gate
require that owner to provide geometry or dress an existing rendered surface. A
transform-only unit is told to use point projection or split a real geometry provider.

Judge-frame visibility debt is due only when a unit owns a rendered subject: geometry,
dressing, look/image claims, or a consumed asset/instance interface. Executable-only
camera and control units no longer need to invent visible proxies. Geometry and look
layers retain the HIR-0019/HIR-0051 occlusion requirement unchanged.

## Rejected alternatives

Blessing the sphere because the scalar passed replaces shot meaning with metric gaming.
Allowing bbox to use Empty display bounds would confuse viewport decoration with render
evidence. Hiding the proxy from render makes visibility fail again. Letting a later layer
delete the upstream sphere violates ownership and replay. Prompting the builder not to
cheat leaves the mechanical incentive intact.

## Validation

Vocabulary fixtures prove both origin metrics are frame-scoped, camera-dependent,
projected-composition evidence and compile into the Blender evaluator. A Blender 5 worker
smoke probe projected a real Empty at the optical axis to `x=0.5`, `y=0.5000001` without
mesh data. Heterogeneous product-target and motion-control fixtures prove control-only
units carry no rendered visibility debt and that a point-projection materialization
publishes without visibility rows. Injected bbox and visible-fraction bindings on a
transform-only mutated role are refused at materialization. The deterministic gate
independently rejects the production Layer-1 view with sixteen typed
`surface-evidence-owner` findings. The full repository suite passes 526 tests.

Production acceptance requires rematerialization to use a non-rendered target interface
and point/aim evidence, then cumulative replay. The moving icosphere candidate is not a
warm start or accepted artifact.

## Release and rollback

This adds two strict evidence kinds and tightens new and selected materializations. No
published build artifact is migrated implicitly. Rollback would again make control-point
placement inexpressible and proxy geometry gate-clean, so it is unsafe.
