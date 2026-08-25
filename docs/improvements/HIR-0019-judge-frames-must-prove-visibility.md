---
id: HIR-0019
title: A layer was judged at frames nobody proved show anything
status: accepted
introduced_in: unreleased
date: 2026-08-25
failure_class: unmeasured_occlusion_made_an_invisible_scene_acceptable
mechanism: visible_fraction_evidence_kind_and_judge_frame_visibility_coverage
adr: ADR-0006
---

# A layer was judged at frames nobody proved show anything

## Observed failure

Generation `088b4a7c…`, run `20260825T070153Z-f6cb7d`: layer 2 (lookdev, judged
f72/f150 against interior foundry references) failed its last unit after three sealed
units — and both judge-frame renders are the blank sun-lit face of layer 1's
`cam.proxy.iris_face` blockout disc. Solid-mode probes confirm pure geometry occlusion:

- Layer 1's materialization PRESCRIBED the camera spine
  (`cam-spine-keyframe-schedule`: f1 `(0,-30,0)`, f36 `(0,10,0)`, max y=148 at f240).
  The f36 key transcribes the retired bundle `a0ce6eab`'s falsified A2 camera into the
  new generation.
- The blockout builder placed the iris-face disc at y=165 and interior masses at
  y≥198 — positions that satisfy the only composition contracts that existed (bbox
  rows at f1 and f240) while the spine never crosses the iris plane at all.
- The generation's own beats demand the crossing: L3 "Ingress — Sealed Iris" judges
  f36; L4 "Reveal — Foundry Chamber" judges f72 from inside.

Every instrument was blind to this by construction: `bbox_*` rows are projection-only
(the f240 wide-masses row measures a slab the camera cannot see, straight through the
disc), layer 2 carried zero context rows at its own judge frames, and the
`composition-coverage` gate rule fired only for camera-owning layers. Three units
sealed lookdev work on surfaces that were never once on screen.

## Root cause

Occlusion was unmeasurable, so a spine/layout contradiction was representable and
cheap. Projection answers "where would it be on screen"; nothing answered "does the
camera actually see it". With no visibility vocabulary, no materialization could be
required to carry visibility evidence, so consistency between a prescribed spine and
builder-placed proxies rested on luck.

## Mechanism

- **`visible_fraction`** (evidence kind, domain `projected_composition`): of the
  subject roles' ON-SCREEN surface samples (vertices + polygon centers, evaluated,
  strided) at the declared frame, the fraction whose camera ray reaches subject
  surface before any other object (`scene.ray_cast`, distance-capped so the sample's
  own surface does not self-hit). Nothing-on-screen reads 0.0 — judged-but-absent is
  the failing number this kind exists for, not an instrument error. On-screen-ness
  remains `bbox_*`'s claim: conflating them made a frame-filling subject read 0.0
  through its own off-frustum rim vertices. Vacuity linting refuses `min lo<=0` and
  `max hi>=1`; `max hi<1` expresses not-yet-revealed beats.
- **Coverage, fail-closed where authorship happens**: `validate_materialization`
  refuses a materialization whose judge frames lack a `visible_fraction` row. Unit
  `composition_context.contract_ids` now count as producer bindings (the gate already
  read them as coverage; the validator demanded claims only). The plan gate reports
  grandfathered views as ADVISORY `composition-coverage` findings for every
  materialized layer's judge frames — not only camera owners.

## Validation

- Blender 5.2 ground truth on the shot's exact geometry (camera y=23, disc y=165,
  slab y=198): occluded slab **0.0**; disc removed **1.0**; the frame-filling disc
  itself **1.0**; off-screen object **0.0**.
- Real shot, `vfx evals plan`: still CLEAN, with seven new advisories — layer 1
  f1/f240 and layer 2 f72/f150 visibility coverage, plus the three HIR-0018
  auto-socket rows. Sealed authority is enumerated, not poisoned.
- Suite 286 (registry/vacuity/probe-compile tests; a coverage-refusal test; every
  fixture materialization now carries visibility rows bound through
  `composition_context`), ruff clean.

## Consequences

Layer 1 is rematerialized with `--discard-accepted` (spine and proxies were built to
a falsified prescription; the replacement view must contract the ingress crossing and
carry interior-proxy visibility at downstream judge frames f72/f150), then rebuilt;
layer 2 follows under HIR-0018's socket lint and this record's coverage rule. The
sampling is strided (≤64 points per object per pool) — a deliberately coarse
instrument that measures presence, not composition quality; the critic still owns
aesthetics.
