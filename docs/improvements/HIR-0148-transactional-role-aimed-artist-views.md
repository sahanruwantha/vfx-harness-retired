---
id: HIR-0148
title: Transactional role-aimed artist views
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: form_builder_cannot_inspect_off_axis_geometry
mechanism: diagnostic_only_temporary_workbench_camera
adr: ADR-0008
---

# Transactional role-aimed artist views

## Observed failure

Room 1046 Layer 2 had to build a corner-section historic hotel through a sealed,
nearly frontal shot camera. Existing crop, wipe, overlay, wireframe, silhouette, and
optical-zoom instruments inspect the plate in 2D, but no bounded tool could tumble around
`building.mass.*`, take an elevation, or solo the mass. A builder could only guess
off-axis form or illegally move `camera.main` in `run_bpy`.

This inspection gap did not cause the permissive acceptance by itself—HIR-0145 and
HIR-0146 close that—but it deprived the form builder of the same feedback an artist gets
from a viewport.

## Root cause

All render tools used `scene.camera`. Transactional render overrides existed for shading,
lights, crops, and resolution, but camera pose was not an observation surface. The only
way to obtain a new view was authored scene mutation, which conflicts with camera
ownership and can leak into replay.

## Decision

- `inspect_view` is a read-only Blender instrument addressed by semantic role namespace
  and frame.
- Views are enumerated: exact `through_camera`, bounded ±30°/±60° orbit, and
  shot-relative front/right/back/left plus world-top elevation.
- Orbit uses a copied perspective camera and fits the evaluated subject bounds. Elevations
  use a fitted orthographic temporary camera.
- `isolate=true` transactionally hides nonmatching rendered hosts. All prior
  `hide_render` values are restored.
- The active scene camera, frame, render settings, visibility, temporary camera object,
  and temporary camera data are restored or removed in `finally`, including on failure.
  The shot camera's data, transform, constraints, and keys are never written.
- Output is Workbench solid or wire and is explicitly `diagnostic_only`. It returns no
  image-evidence handle and cannot enter `propose_checks` or contract payment.
- Geometry units receive the instrument on their compiled unit-scope card. The global
  builder prompt does not grow.

## General mechanism

The worker resolves matching renderable hosts using the shared semantic matcher, computes
their evaluated world-space bound union, and creates a temporary camera aimed at its
center. The ordinary transactional render handler produces the image under a unique
diagnostic artifact tag. An outer `finally` restores camera/frame/visibility and deletes
temporary datablocks.

The tool layer intentionally does not call the candidate artifact registrar. Defense in
depth adds `diagnostic_only` to the payment-eligibility predicate, so a future mode change
cannot accidentally mint an immutable payment handle.

## Rejected alternatives

- Let builders tumble the shot camera: that steals Layer 1 authority and can alter
  canonical replay.
- Use free-form camera coordinates: the agent would guess view construction and fit.
- Accept an artist view as image evidence: off-axis diagnostics are not the judged plate
  and have no same-settings adversary.
- Add prose telling builders to imagine the side view: the missing fact is measurable.
- Add the instrument to every kickoff paragraph: the active geometry card is the bounded
  context that needs it.

## Validation

Tests pin tool registration, geometry-only compiled discovery, enumerated diagnostic
status, payment ineligibility, and `finally` restoration of camera, frame, visibility,
and temporary object/data hosts. Existing renderer, checkpoint, unit-scope, and tool
policy suites remain green. The next bounded Layer 2 build provides the live Blender
exercise before the instrument is relied on for publication.

## Release and rollback

No authority schema changes. Removing the tool leaves accepted scene behavior unchanged
but restores a known form-inspection blind spot. Diagnostic files are scratch run
artifacts and never authority.

