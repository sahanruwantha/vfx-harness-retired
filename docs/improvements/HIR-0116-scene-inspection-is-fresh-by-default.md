---
id: HIR-0116
title: Scene inspection is fresh by default
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: frame_implicit_inspection_returned_stale_world_transform
mechanism: unconditional_frame_and_depsgraph_refresh
adr: null
---

# Scene inspection is fresh by default

## Observed failure

The Room 1046 Layer 1 camera builder reproduced the same contradiction in held-out runs
`20260829T175349Z-882888` and `20260829T182128Z-6c2496`. Immediately after replaying the
accepted `camera_target` script, `inspect_scene(section='objects')` reported
`loc=(0,20,5.5)` but `world_loc=(0,0,0)`. Calling the identical instrument with
`frame=1` then reported `world_loc=(0,20,5.5)`. The builder correctly distrusted the
first read and spent another turn selecting a frame before constructing its camera.

## Root cause

HIR-0055 made `inspect_scene` the typed read-only authority and described its world
locations as evaluated. The worker only called `Scene.frame_set` when the optional
`frame` argument was present, then read original-host `matrix_world`. A directly assigned
local transform can leave that matrix at its previous evaluated value until Blender
updates the dependency graph. Optional frame selection had accidentally become an
undocumented freshness switch. Classification: instrument/data freshness defect.

## Decision criteria

- Every inspection observes one freshly evaluated Blender frame, whether or not the
  caller passes `frame=`.
- Omitted `frame=` selects `scene.frame_current`; explicit `frame=` keeps its existing
  selection behavior.
- World transforms and dimensions come from evaluated object instances. Camera pose and
  animated lens data come from the evaluated camera. Light rows read the evaluated light
  host and data.
- Observation stays read-only with respect to authored scene authority and never enters
  the mutation journal.
- The rule is independent of shot, role, object type, hierarchy, or animation presence.

## General mechanism

`_refresh_inspection_scene` deterministically resets the selected/current frame, updates
the active view layer, and obtains one dependency graph for the complete response.
`h_inspect` uses `evaluated_get` against that graph for every object, camera, and light
row. Local object location remains visible beside evaluated world location so hierarchy
and constraint effects stay distinguishable. Tool schema text now says that every call
is fresh and `frame=` selects a different moment.

## Rejected alternatives

- Require callers to always pass `frame=`: freshness is an instrument invariant, not a
  prompt convention, and static-control builders do not naturally need a frame.
- Compare local and world values and warn: a hierarchy can legitimately make them
  different, so the instrument cannot infer staleness from disagreement.
- Call only `view_layer.update`: resetting the current frame also refreshes animation,
  drivers, and constraint evaluation at the declared moment.
- Read original hosts after refreshing: evaluated object/data instances are the Blender
  authority for dependencies and animation.

## Validation

- `test_scene_inspection_refreshes_current_frame_and_evaluated_hosts` proves omitted and
  explicit frames both execute frame reset plus view-layer update and pins evaluated-host
  reads for objects, cameras, and lights.
- In held-out recovery run `20260829T192337Z-4909a3`, the first frame-implicit
  `inspect_scene(section='objects', role='camera*')` reported the accepted target as
  both `loc=(0,20,5.5)` and `world_loc=(0,20,5.5)` without a preceding frame-selection
  call. All 23 camera-path contracts then passed unchanged. Final composed producing
  run `20260829T192957Z-f915e1` replayed from empty and passed all seven claimed frames
  at 5.0.

## Release and rollback

No schema or persisted-state change. Rollback restores order-dependent tool answers and
is unsafe for any agent that relies on `inspect_scene` as typed authority.

## Remaining limitations

Inspection is a point observation. Callers comparing animation across moments still need
explicit frame selections or temporal instruments such as `list_keyframes` and
`check_scene(kind='motion')`.
