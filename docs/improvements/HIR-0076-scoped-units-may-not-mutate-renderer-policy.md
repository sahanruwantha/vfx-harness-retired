---
id: HIR-0076
title: Scoped units may not mutate renderer policy through free-form bpy
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: undeclared_renderer_policy_mutation
mechanism: pre_execution_renderer_namespace_guard
adr: null
---

# Scoped units may not mutate renderer policy through free-form bpy

## Observed failure

In run `20260827T232542Z-3c222e`, `lighting_atmosphere` owned only
`world.lighting_rig`, `world.atmosphere_volume`, `lighting_arc_intensity`, and
`atmosphere_density`. After repeated black renders, the builder changed
`scene.eevee.use_raytracing`, `scene.eevee.light_threshold`, and
`scene.eevee.volumetric_samples`. The unit plan explicitly held color and comparison settings
fixed, but the live scope checker examined only newly created object roles, so these unrelated
scene-property writes were accepted.

## Root cause

Object-role scope and renderer policy were enforced at different boundaries. The object manifest
could detect a new untagged mesh, but renderer settings live on `Scene` and do not appear in that
manifest. The read-only `run_bpy` guard also created a perverse escape hatch: after a settings
query was correctly rejected, turning the query into a settings mutation made it executable.

## General mechanism

For scoped units, the live `run_bpy` boundary now parses authored assignments before Blender
executes them and rejects free-form writes under renderer namespaces: `eevee`, `cycles`,
`render`, `view_settings`, `display_settings`, and `sequencer_colorspace_settings`. The pass
tracks the common alias form (`sc = bpy.context.scene; ee = sc.eevee`) and dynamic `setattr`.
Read-only inspection remains routed to typed tools. Diagnostic renderer changes remain available
through `render_frame` and `render_pass`, which apply and restore their settings transactionally.
If canonical settings make the declared semantic controls infeasible, the legal result is
`cannot_express_in_scope`, not renderer-policy drift.

## Rejected patch-level alternatives

Adding the three observed EEVEE property names to a prompt would miss the next property and would
still rely on model memory. Checking only after canonical replay would spend the full build budget
and could let misleading images steer further edits. Adding `Scene` to the object-role manifest
without a typed renderer-control contract would make every scoped unit appear to own global
policy.

## Validation

`test_scoped_run_bpy_detects_renderer_setting_writes_through_aliases` pins direct, aliased, and
`setattr` writes. `test_renderer_reads_and_semantic_writes_are_not_renderer_setting_writes`
proves the classifier does not absorb renderer inspection or ordinary semantic light mutation.

## Release and rollback

No persisted schema migration. Unrestricted legacy units retain their current behavior. Rollback
removes the pre-execution guard but restores the ability for a scoped unit to change canonical
renderer policy without declared authority.
