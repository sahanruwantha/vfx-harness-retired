---
id: HIR-0015
title: Evidence rows measured undeclared frames and an unclipped frustum
status: accepted
introduced_in: unreleased
date: 2026-08-24
failure_class: confident_evidence_that_could_not_support_its_claim
mechanism: per_row_measurement_context_and_frustum_clipped_projection
adr: null
---

# Evidence rows measured undeclared frames and an unclipped frustum

## Observed failure

Run `20260824T103842Z-afec73` sealed work unit `1.iris_camera_bootstrap` as `passed` on
executable evidence alone (`decided_by: unit_executable_evidence`). Its accepted report
contains `bbox_height = 1132.53608` against the definition *"projected union height in
normalized camera coordinates"* — byte-identical at f1 and f36, two frames between which
the camera contract moves 40 units and the iris opens. The published artifact
`build/units/01/iris_camera_bootstrap.py` renders a camera facing ~180° away from the
scene at every frame; two canonical repair rounds (~$1.6) had rewritten a *correct*
distilled script into that state while chasing the readings.

Preserved failing case: the run's own consumer-view contracts
(`runs/…-afec73/scratch/plan-consumer-view/scene_checks.json`), its pre-repair script
(`checkpoints/repairs/layer-1@iris_camera_bootstrap-before-1.py`, correct) and its
accepted script (broken). Executing the real `_blender_probe` output against empty-scene
rebuilds of those scripts in Blender 5.2 reproduces every accepted number and error
string exactly.

## Root cause

Two independent instrument defects, one classification each:

1. **Cross-row frame leakage (evidence error).** `_blender_probe` set the scene frame
   once per batch. Temporal kinds — `keyframe_schedule`, `onset_order`,
   `radial_distance_trend`, `transform_return_delta` — excurse through other frames via
   `frame_set` and never restore. `cam-spine-full` (16 samples, last at f240) ran first
   in row order, so every following row measured **frame 240** while its identity said
   f1 or f36. Identical values at "two frames" follow directly: both probes measured the
   same third frame. At f240 the camera is at `(0, 148, 9)` looking at a target further
   down +Y — all 50 housing objects behind it — producing the pre-repair
   `none of 50 selected object(s) is in front of the camera` at both canonical frames.

2. **Unclipped projection (evidence error).** `_projected` dropped only points with
   `ndc.z <= 0`. A vertex barely in front of the lens plane but far off-axis projects to
   arbitrarily large "normalized" coordinates, so a degenerate camera pose grazing any
   geometry produced a huge union height that trivially satisfied `>= 0.25`. This is
   what converted defect 1 from a visible failure into a false PASS: repair 2 removed a
   correct Track-To and baked a world-space look-at onto the camera's *local* rotation
   under a rig that already owns +90° pitch (`bvfx_camera_rig`: "LEVEL is 90, not 0"),
   double-pitching the camera; at the leaked f240 pose that grazed a housing sliver and
   read 1132.53608 — a passing number for a camera facing away from its subject.

The repair loop then did its job faithfully against lying instruments: repair 1 changed
nothing measurable (same leaked frame), repair 2 "fixed" the reading by breaking the
camera, and monotonicity accepted it. The advisory `check_framing`/`framing_from_ndc`
shared defect 2 (`use = in_front or pts` even used behind-camera points), reporting
`bbox [-1.07, -1.52, 2.07, 3.35] · PASS ✅` to the builder mid-session.

Both defects are the HIR-0014 shape — a reading that looked authoritative while unable
to mean what it claimed — in the two dimensions that record did not cover: *when* a row
measures, and *where* a projection is allowed to read from.

## Mechanism

Ambient state is not an instrument; measurements carry their own context.

- **Per-row measurement context.** The probe's row loop re-establishes the declared
  frame and a fresh depsgraph before every row (`_scene.frame_set(_FRAME)`;
  `_row_dg = …evaluated_depsgraph_get()`). Temporal kinds still excurse; they can no
  longer re-frame a sibling. The ambient batch depsgraph is gone — kinds that evaluate
  objects do so through the row's own handle.
- **One frustum-true projection.** `geom.frustum_union_ndc` clips geometry as *edges*
  against the six clip-space half-spaces (homogeneous Liang–Barsky) before the
  perspective divide: every returned coordinate is inside [0, 1] by construction, a wall
  grazing the lens contributes exactly its visible sliver, and no frustum intersection
  returns `None` — an absent reading the caller must fail closed on, never a number a
  `>= k` target can compare. Blender-side, `checks.camera_clip_matrix` builds
  projection@view from `Camera.view_frame(scene=…)` (5.x removed `calc_matrix_camera`),
  folding in resolution, sensor fit, pixel aspect and shift. The authoritative probe
  (`_projected`) and the advisory `check_framing`/`subject_bbox` both consume this one
  implementation, per ADR-0003; `framing_from_ndc` is deleted.

Rejected alternatives:

- *Plausibility bounds / anti-freeze tripwires on metric values.* Guards detect invalid
  states; the mechanisms above make them unrepresentable (a clipped union cannot exceed
  the frame; a row cannot read a foreign frame).
- *Disposable-process canonical verification.* Hypothesized while the identical values
  looked like frozen animation in the warm worker. Disproved as the cause: driving the
  real worker through restore → render → reset → preamble → script binds and animates
  correctly; the leak reproduces identically in a cold process. The question of making
  the canonical gate's context disposable (the warm reset is an *approximation* of
  empty-scene truth, and this investigation spent real effort ruling it out) is left as
  a candidate ADR, explicitly not justified by this failure.

## Validation

Blender 5.2 replay of the run's own artifacts, real probe output, no model spend —
both directions:

| Confirmation | Before fix | After fix |
|---|---|---|
| probe leaves scene at | f240 (leak) | declared frame |
| pre-repair (correct) script, bbox f1 | `None` — "none … in front" | **0.6188 PASS** (isolated ground truth 0.6189) |
| pre-repair (correct) script, bbox f36 | `None` — same | **1.0 PASS** (camera inside housing, clipped) |
| accepted (broken) script, bbox f1 | 1132.53608 PASS | **fail closed** — "none of 50 … intersects the camera frustum at frame 1" |
| accepted (broken) script, bbox f36 | 1132.53608 PASS | 0.1641 (true visible sliver; unit still fails on f1) |
| cam-spine-full both scripts | 0.0 PASS | 0.0 PASS (unchanged — it was honest) |
| worker `self_test` framing fixture | fired | fired — "does not intersect the camera frustum" |

The two misdirected repair rounds cannot recur on this failure: the correct distilled
script now passes its canonical evidence, so no repair is requested.

Repository verification: `ruff check src tests` clean; `pytest -q` 269 passed
(new: `tests/unit/test_projection_integrity.py` — clipping semantics incl. the grazing
1132-class and near-plane crossing, row-prologue ordering, no ambient depsgraph,
fail-closed wording); `vfx --help` healthy.

## Consequences

Unit `1.iris_camera_bootstrap`'s acceptance is void: it was sealed on wrong-frame,
unbounded readings, and its published artifact fails its own contracts under honest
instruments. The unit must be rebuilt (the pre-repair distilled script is the correct
starting point and passes canonically as-is). The `iris_threshold_open` target
`bbox_height >= 0.05` is loose enough that a broken camera's grazing sliver satisfies
it; whether threshold contracts should also bound composition (centre, width) is a
planning-quality question for the unit replan, not a harness defect.

This closes the frame- and frustum-truth instances of
`confident_evidence_that_could_not_support_its_claim`. Like HIR-0014, it does not claim
the class is closed; the next instance will surface the same way — by auditing what an
accepted report claimed against what its instruments could know.
