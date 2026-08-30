---
id: HIR-0136
title: World-point projection is read-only
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: marker_geometry_created_for_camera_projection
mechanism: evaluated_camera_world_point_projection_probe
adr: ADR-0003
---

# World-point projection is read-only

## Observed failure

During Room 1046 Layer 2 run `20260830T061008Z-1e787d`, the `building_mass`
builder needed to place a base and tower through the sealed frame-39 camera. Existing
`framing` and `bbox` instruments accepted only scene objects or semantic roles. To learn
where known world points landed, the builder created four temporary cube markers with
undeclared `tmp.marker.*` roles. The scope gate correctly rejected all four mutations;
the builder then deleted them and continued by estimating vertical offsets.

## Root cause

The canonical camera matrix and projection arithmetic already existed, but the public
read-only boundary exposed only surface/object projection. A coordinate question was
therefore answerable only after an illegal scene mutation. This is a missing instrument,
not a builder-policy or prompting defect.

## Decision

`check_scene(kind='projection')` accepts a frame and one or more explicit world-space
`[x, y, z]` points. It evaluates the active camera and depsgraph at that frame and returns
top-left normalized screen coordinates, whether each point is in front of the camera,
whether it lies inside the full clip frustum, and homogeneous clip depth.

Off-frame coordinates are preserved because this is a diagnostic point probe, not a
surface bbox or acceptance metric. Points behind the camera return `screen: null`.
The instrument never creates, tags, or deletes Blender objects and carries no mutation
authority or acceptance threshold.

## General mechanism

`project_clip_point` owns the dependency-free homogeneous projection classification used
by the Blender worker and no-Blender tests. `check_projection` establishes the requested
frame, obtains a fresh evaluated depsgraph, uses the same `camera_clip_matrix` as every
other projection instrument, and returns ordered records for the supplied points.

The public schema requires one to 64 numeric triples and deliberately requires no
role/object selector. Teaching output says to use this read-only probe instead of marker
meshes.

## Rejected alternatives

- Permit temporary marker roles: they still mutate/journal the candidate, can leak into
  replay, and broaden unit scope for a read question.
- Use `run_bpy` for projection: that duplicates the canonical matrix and forces agents to
  recall Blender-version APIs.
- Infer placement from camera transforms in the prompt: the evaluated lens, sensor fit,
  shifts, aspect, parents, and current frame are authoritative runtime state.
- Clip every returned coordinate into `[0,1]`: that hides which direction and how far an
  off-frame proposal misses.

## Validation

Pure fixtures prove centre, off-frame, and behind-camera clip classifications. Tool-policy
fixtures prove the projection check needs no scene selector, rejects malformed triples,
and teaches that marker geometry is unnecessary. Production validation is the next clean
Layer 2 retry: camera-relative massing may query candidate world points without any scope
violation or temporary object cleanup.

Validation on 2026-08-30: the focused projection and builder-instrument suites passed
34 tests, the full repository suite passed 632 tests in 42.49 seconds, and
`.venv/bin/ruff check src tests` passed.

## Release and rollback

No persisted schema change. Rollback returns camera-relative coordinate placement to
guessing or illegal diagnostic mutations.
