---
id: HIR-0128
title: Camera capability does not authorize subject form
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: camera_materialization_manufactures_geometry_proxy
mechanism: sparse_capability_compiled_unit_vocabulary
adr: ADR-0005
---

# Camera capability does not authorize subject form

## Observed failure

Layer-1 rematerialization run `20260830T014717Z-ef52b7` was selected from global
bundle `7ee41038...`, whose sparse root layer declares only the role-bound global
capability `jit.provides: {"camera": ["camera.*"]}`. The unpublished candidate
staged `camera_target_framing` with `provides: ["geometry"]`, mutated
`camera.target_proxy`, activated three `bbox_height` rows on Layer 1, and attempted to
seal reference framing on that proxy. The run exhausted 24 model turns and did not
publish, but nothing at unit staging told the materializer that camera authority could
not create subject form.

Immutable evidence is retained at
`runs/20260830T014717Z-ef52b7/scratch/jit-layer-1.json`; the run's `status.json`
records `terminal_cause: max_turns_exhausted`.

## Root cause

HIR-0086 made camera availability global, role-bound DAG authority, while unit-local
`geometry` remained a valid capability for form layers. The materialization schema
still exposed the union `{camera, geometry}` to every layer and finalization checked
only that globally promised capabilities were present. It did not reject a capability
that contradicted the sparse layer boundary. HIR-0127 documented deferred subject bbox
but no executable predicate prevented the exact proxy workaround it rejected.

## Decision criteria

- A role-bound camera bootstrap layer cannot manufacture rendered subject form.
- A non-camera layer may still provide geometry; this is not a global geometry
  registry or a prohibition on form work.
- The model sees the legal capability vocabulary before generation.
- The staging write and final publication independently fail closed.
- Global publication refuses reserved form selectors that the camera grant does not
  match, so a later materialization cannot be asked for geometry it cannot stage.
- The rejection names the legal deferred-bbox shape instead of merely saying
  `geometry` is invalid.
- No role-name, shot-name, frame, or department heuristic enters core behavior.

## General mechanism

Sparse global authority now compiles each materialization's unit capability vocabulary.
A layer whose `jit.provides` grants `camera` may stage camera and control units, but not
`provides: ["geometry"]`; layers without global camera authority may provide local
geometry but cannot invent camera. The closed `stage_materialization_unit` schema
enumerates that vocabulary. Its revision-checked staging transaction refuses a forged
capability before candidate bytes change, and materialization finalization repeats the
check against the hash-verified sparse bundle.

The same compiled grant is a publication predicate: `reserved_roles` on a
camera-providing layer may only match the camera selectors. Extra form namespaces fail
at ownership-mapping validation and at the plan gate, so until-clean cannot select a
layer that owns both camera provide and later subject form.

The teaching rejection says to author persistent `bbox_*` contracts over rendered
subject roles owned by the earliest downstream form layer, retain `owner_layer` and
`fault_owner` on the camera layer, set `activates_at` to the form layer, and bind the ids
through the camera unit's `composition_context`.

## Rejected alternatives

- Prompt-only wording: the failed session already received HIR-0127 guidance and still
  created a proxy.
- Removing `geometry` from every materialization: later form layers need that typed
  capability for mesh evidence and mutation authority.
- Inferring camera-only scope from role names such as `camera.*`: camera authority is
  the typed `jit.provides` key, and heterogeneous namespaces must behave identically.
- Waiting until Blender build: proxy form is a plan-authority error and should not
  consume a builder session.

## Validation

Regression fixtures use typed camera authority with product and motion role names. They
prove the closed schema exposes only `camera`, the staging transaction leaves bytes
unchanged on a geometry attempt, finalization reports the same blocker, a non-camera
layer retains local geometry authority, ownership mapping refuses extra form
`reserved_roles` on a camera-providing layer, and the plan gate repeats that
`global-capability` finding. Heterogeneous grant names (`product.view_rig`,
`motion.capture_host`) are legal when they match the reserved set; a sibling form
namespace is not.

## Release and rollback

This is stricter publication and materialization behavior with no persisted schema
change. An already-selected sparse bundle that reserved form namespaces on a camera
layer is now gate-dirty and must be republished. Rollback would let a camera layer
self-author subject form again, or let global publication combine camera provide with
form ownership so a shading-only unit is asked for meshes.

## Remaining limitations

This gate proves ownership separation, not that the resulting camera path matches the
references. Path, timing, targeting, and deferred bbox bindings still require executable
camera-layer contracts and empty-scene replay before later form work may start.
