---
id: HIR-0134
title: Deferred subject bbox starts at complete geometry
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: deferred_bbox_revalidated_before_subject_creation
mechanism: activation_preflight_exclusion_and_dependency_complete_bbox_payer
adr: ADR-0004
---

# Deferred subject bbox starts at complete geometry

## Observed failure

Room 1046 build run `20260830T060024Z-c13a7b` replayed sealed Layer 1, then stopped
before the first Layer 2 model turn or mutation. Layer-start prior-interface revalidation
evaluated camera-owned persistent rows `building_miniature_start` at frame 1,
`building_frame_fill_4p5s` at frame 114, and `building_bbox_f176` at frame 176. All
select parent role `building`, have `owner_layer: 1`, and first activate at Layer 2.
No `building` descendant existed in the Layer 1 replay, so each value was `None`; the
harness misrouted the absence as a Layer 1 regression.

The selected Layer 2 DAG truthfully splits that parent subject across
`building_mass` (`building.mass.*`) and its dependent `building_roof`
(`building.roof.*`). `ground_site` is unrelated. HIR-0127 intended the geometry layer
to make the subject evaluable and pay the deferred camera debt, but runtime preflight
ran before any current-layer unit and exact selector matching did not identify a parent
role produced by several descendant write clusters.

## Root cause

`prior_interface_evidence` selected every earlier-owned row for which layer lifecycle
`active_for` returned true. It did not distinguish an already-existing upstream interface
from a deferred subject whose `activates_at` equals the current layer. The implicit
geometry protection path then matched contract roles directionally; parent `building`
did not equal descendant `building.mass.tower`.

Skipping the row for the whole layer would be unsafe, and making the root massing unit
pay would still evaluate an incomplete roofless subject. The missing instrument was the
first dependency closure that contains every same-layer producer overlapping the parent
selector.

## Decision criteria

- At the exact activation layer, earlier-owned deferred `bbox_*` rows are excluded from
  pre-unit prior-interface replay. Other upstream interfaces still fail before mutation.
- Same-layer geometry producers are derived by symmetric semantic-selector overlap, not
  display names or role keywords.
- The first overlapping geometry unit whose transitive dependency closure contains every
  overlapping producer pays the deferred rows at their owner frames.
- Unrelated geometry does not inherit the debt. A DAG with no dependency-complete payer
  fails materialization and the independent plan gate.
- On later layers, the subject already exists; every overlapping geometry mutation
  protects the persistent bbox.
- Camera ownership, fault ownership, judge-frame authority, and Layer 1 checkpoints do
  not move.

## General mechanism

`deferred_subject_composition_activation_ids` identifies earlier-owned bbox rows whose
subject first becomes due on this layer. `prior_interface_rows` excludes only those ids
from pre-mutation replay. `deferred_subject_composition_ids_for_unit` derives overlapping
geometry producers and pays activation-layer debt only on a producer whose closure contains
the whole set; later layers use ordinary overlapping mutation protection.

`_geometry_protected_vis_ids` consumes that compiled unit-relative set across live
read-back, candidate finalization, canonical replay, revalidation, and checkpoint freeze.
`deferred_subject_composition_payment_gaps` is shared by JIT materialization and the plan
gate so an unpayable parent selector cannot publish.

## Rejected alternatives

- Reopen Layer 1: the camera is sealed and the absence is the explicitly deferred subject.
- Treat `None` as a bbox failure before activation geometry: this is the observed false
  regression.
- Drop the bbox rows: it would return to roof/void and generic scale without executable
  camera composition debt.
- Make every geometry unit pay: root massing cannot prove a roof that a successor owns,
  and unrelated site geometry cannot repair `building`.
- Collapse mass, roof, and site into one unit: their derived role namespaces are distinct
  write clusters under HIR-0083.
- Match the word `building`: semantic selector overlap and the unit DAG are sufficient.

## Validation

Focused fixtures prove activation-layer preflight excludes the deferred row while the next
layer revalidates it; a parent selector spanning mass and roof is not due on mass, is due
on dependent roof, and is absent from unrelated site; removing the dependency yields one
typed payment gap, while the ordered DAG has none.

Validation on 2026-08-30: focused evidence-vocabulary, builder-instrument,
materialization, plan, and visibility suites passed 184 tests in 10.62 seconds; the full
repository suite passed 629 tests in 43.12 seconds. `.venv/bin/ruff check src tests`
passed, and `vfx evals plan` reported the selected Room 1046 view CLEAN with 37 scene
contracts.

Production validation is a bounded Layer 2 build from empty-scene replay. The mass/roof
chain must create the subject and evaluate all three camera-owned rows without reopening
or mutating Layer 1.

## Release and rollback

No persisted schema change. Rollback restores a deterministic pre-unit `None` failure on
every correctly deferred subject bbox and makes the documented HIR-0127 lifecycle unusable.

## Remaining limitations

The mechanism assumes truthful semantic role hierarchy. A producer that mis-tags geometry
is rejected by existing scope/replay checks; this record does not infer hidden object
membership from Blender names.
