---
id: HIR-0112
title: Write-family authority binds live mutation payloads
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: planned_family_does_not_bound_live_mutation
mechanism: additive_capability_and_payload_family_attestation
adr: null
---

# Write-family authority binds live mutation payloads

## Observed failure

Layer 2 build `20260829T151419Z-751b78` accepted `iris_blade_shading` after its
shading-only plan created both blade mesh geometry and material state. The same run's
`iris_blade_mechanism` passed all seven scene contracts after one payload created mesh
wedges, material assignments, controls, and keyframes; it stopped only because its image
debt had no payable optical signal. Semantic tags made both payloads role-valid even though
their actual instrument families exceeded the published units.

The selected authority came from materialization `20260829T145841Z-1efac6`. Its blade
unit declared `provides: [geometry]` and bound keyframe contracts, but the atomicity gate
derived only keyframe work. The later geometry capability was treated as a fallback and
disappeared as soon as any contract-derived family existed.

## Root cause

HIR-0083 derived an atomic publication predicate, but `_families_for_namespace` chose
contract-derived families *instead of* the typed residual producer capability. Live
execution then enforced semantic roles only. No compiled write-cluster reached the builder
card, and no boundary compared family-specific `run_bpy` operations with the plan. A model
could therefore perform extra production work and tag the resulting objects into a legal
role namespace.

## General mechanism

A producer capability is additive mutation authority. Geometry plus keyframe evidence now
derives mesh and keyframe clusters and fails publication by name. Camera construction,
placement, and motion remain one typed camera-host family: a camera provider absorbs control
and keyframe evidence on that host, while unrelated light, mesh, shading, volume, or
compositor work remains a second cluster.

The active unit card carries its exact derived write-clusters. Before Blender executes or
journals `run_bpy`, a pure AST classifier enumerates high-confidence family witnesses from
the closed helper registry and family-specific Blender APIs. Mesh data creation, bmesh,
material creation/assignment, light/camera data creation, and keyframe/driver calls are
covered. `bvfx_role` and `bvfx_control` are metadata and contribute no family. The payload is
refused when the card does not contain exactly one cluster or any witnessed family exceeds
that cluster. `mutates.dresses` is the typed shading exception; camera-host keyframes are
the typed camera exception. Rejection reports family, operation, and source line, and directs
the materializer to split or rematerialize the work.

Generic object construction and transforms are intentionally not guessed from syntax because
their host family is ambiguous without Blender state. They remain governed by semantic role
scope and executable contracts. New recurring high-confidence operations extend the one
classifier rather than adding prompt wording or a second family registry.

## Validation

Unit tests pin additive geometry+keyframe derivation, the camera-host exception, typed AST
evidence, tag exclusion, dressing, invalid selected-card refusal, and exact family/line
feedback. The compiled card test proves the same cluster drives the derived publish interface.
The complete repository suite passes.

Against the unmodified selected `vfx-test` authority, `vfx evals plan` now reports exactly one
blocking atomicity defect: Layer 2 `iris_blade_mechanism` is
`iris.blades/control_host/keyframe` plus `iris.blades/geometry/mesh`. Accepted Layer 1 camera
authority remains legal, so the repair can consume the Layer 2 falsification without
rematerializing or rebuilding Layer 1.

## Release and rollback

This is a stricter publication and live-mutation boundary with no schema or digest bump. A
rollback would again permit published family authority and executed family behavior to
diverge, so it is unsafe.

Implementation commit: `2619c56` (`Bind live payloads to write-family authority`).
