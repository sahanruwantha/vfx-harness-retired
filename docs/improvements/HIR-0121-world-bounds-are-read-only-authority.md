---
id: HIR-0121
title: World bounds are read-only authority
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: missing_world_bounds_enabled_mutation_probe_laundering
mechanism: evaluated_world_bbox_instrument_and_unscoped_scene_property_denial
adr: null
---

# World bounds are read-only authority

## Observed failure

Room 1046 Layer 2 builders repeatedly needed the exact world-space minima and maxima of
accepted geometry to place successor modules. `inspect_scene(objects)` exposed local and
world location plus evaluated dimensions, but dimensions do not identify either face of
a rotated or translated bound. The roofline, window-bay, pier, sill, and cornice sessions
therefore attempted read-only `run_bpy` functions over `Object.bound_box`. The guard
correctly denied most of them, but the advertised `inspect_scene` alternative could not
answer the question.

The `facade_sill` session in run `20260829T204459Z-4e2b95` then incremented
`bpy.context.scene['debug_probe']` and returned the desired bounds. The AST classifier
saw the unrelated Scene custom-property assignment as an authored mutation, admitted
the call, and journaled it. Finalization pruned the fake write, but only after the query
had consumed mutation authority and established a repeatable bypass for any missing
read instrument.

## Root cause

HIR-0055 closed common read gaps with evaluated location and dimensions but did not
publish evaluated world-bbox extrema. Its conservative mutation classifier treated any
non-local subscript assignment as a durable write. A global Scene custom property has
neither semantic role nor owner, so it cannot establish the active unit's declared write
cluster; nevertheless, it could launder arbitrary queries into `run_bpy`.

The defect had two inseparable sides: the typed read surface omitted an already-owned
deterministic fact, and the mutation boundary accepted unowned global state as evidence
of authored production work.

## Decision criteria

- `inspect_scene(objects)` reports freshly evaluated world-bbox minima and maxima from
  the exact depsgraph used for location and dimensions.
- Bounds reflect object rotation, scale, parenting, animation, and evaluated geometry;
  they are not reconstructed from location plus dimensions by the model.
- Non-renderable control hosts explicitly report no surface bounds.
- Global Scene custom properties cannot authorize `run_bpy`, directly or through local
  aliases, and are rejected even if a payload also contains a real mutation.
- Owned object custom properties remain legal; the mechanism does not remove semantic
  per-host controls.
- A rejection names the ownership defect and the exact typed read that now answers the
  motivating question.
- The rule is independent of shot, object name, role, transform, and coordinate values.

## General mechanism

The worker's fresh inspection pass transforms every evaluated renderable host's eight
`bound_box` corners by its evaluated `matrix_world`, then publishes
`world_bbox_min=(x,y,z)` and `world_bbox_max=(x,y,z)` beside world location and
dimensions. Empty, Camera, and other non-surface control hosts publish `-`.

The pre-tool AST boundary resolves direct `bpy.context.scene` references and transitive
local aliases. Assignment, augmented assignment, deletion, and `__setitem__` against a
Scene subscript are an explicit deny: Scene custom properties have no semantic role or
owner. The same rows are excluded from `_run_bpy_has_authored_mutation`, so a fake write
cannot make a read-only payload appear authored.

## Rejected alternatives

- Keep telling builders to use dimensions: a size does not name minimum or maximum and
  cannot locate a face after arbitrary transforms.
- Permit one harmless Scene marker: every read can attach the same marker, collapsing
  the read/mutation boundary and consuming journals and convergence arms.
- Detect only the literal key `debug_probe`: fixture-shaped; any other key recreates the
  bypass.
- Allow Scene custom properties when another write exists: the unowned mutation would
  still enter accepted scripts and could carry hidden cross-unit state.
- Add more prompt warnings: the typed query remained unable to answer the question, and
  a mechanical authority bypass cannot be repaired by wording.
- Make all custom-property writes illegal: role-owned objects may legitimately expose
  semantic controls; only the global unowned Scene host is forbidden here.

## Validation

- `test_scene_custom_property_cannot_launder_a_read_only_probe` pins direct and
  transitively aliased Scene writes as unowned while preserving object-property writes.
- The integration harness sends the exact laundering shape through the real
  `script_sanity` hook, requires denial, and separately proves an object-property write
  remains legal.
- The checkpoint-fidelity regression pins fresh depsgraph inspection plus evaluated
  corner transformation and both public field names.
- Focused read-boundary, checkpoint, scene-report, and builder-instrument suites: 47
  passed. Ruff passed on every changed file.
- The full integration harness reported `ALL PASS (0 failed)`; the full unit suite passed
  591 tests; `.venv/bin/vfx --help` passed.
- Producing run `20260829T222559Z-9101e0` exposed real bounds including
  `cornice_side world_bbox_min=(4.1, 6.5, 39.25)` and
  `world_bbox_max=(4.5, 19.9, 39.65)`. The `facade_entrance` builder consumed the typed
  inspection, made no read-only or Scene-property probe, and sealed through cumulative
  empty-scene replay at 5.0.

Implementation commit: `6832391` (`fix: expose world bounds without mutation probes`).

## Release and rollback

No persisted schema change. `inspect_scene` is an ephemeral text instrument; its object
rows gain explicit fields. Scene custom-property writes through `run_bpy` are a strict
authority migration and fail closed immediately. Rollback restores an unowned mutation
channel and is unsafe.

## Remaining limitations

`check_scene(bbox/framing)` remains single-subject under HIR-0041; use the enumerated
object names for a diagnostic crop and `contract_result` for an aggregate semantic-role
contract. Mesh checks also apply a generic single-island expectation unless the caller
declares an intentional open shell; policy for intentionally disconnected repeated
modules is a separate instrument question.
