---
id: HIR-0042
title: A look repair must see a beauty plate
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: probe_solid_was_the_only_repair_image
mechanism: look_units_get_draft_look_render
adr: null
---

# A look repair must see a beauty plate

## Observed failure

Run `20260827T040204Z-5f4489` sealed atmosphere executable rows 3/3. Canonical
critic scored `atmospheric_scale_reinforcement` 2.0 at f150: hard-edged
particulate cards, then (after repair 2) milky haze without shafts. Repair 1
raised peak haze density; score stayed 2.0. Repair 2 Read
`scratch/candidate-probe/solid_f0150.png`, treated shaft blockers as dark
slats, and keyed `hide_render=True` at f131. Empty-scene replay still 3/3.
Critic stayed 2.0 and then also reported missing directional shafts — the
occluders that carve god rays in EEVEE. The unit summary said "metric
feedback n/a — this layer owns no appearance/look axis" while the unit
declares `look_capabilities: ["atmosphere"]`.

## Root cause

`probe_candidate` always rendered `mode="solid"`. The tool description and
repair prompt told the session to diagnose against that plate. Workbench
solid cannot show volumes, bloom, or why an occluder exists. Repair
optimized the geometry view of the critic's look subject. Classification:
missing compiled observation at the repair instrument.

`tool_use_summary(..., look_feedback_applicable=axes_own_look(axes))`
re-scanned axis identifiers after HIR-0032 made declared capabilities the
authority, so the run log lied about look ownership.

## Decision criteria

- `probe_preview_modes`: look-less → `solid` only; look-owning → `solid` and
  `draft`. JSON carries `look_render` plus a note that solid is not the
  critic plate.
- Repair prompt names that split.
- Unit summary `look_feedback_applicable` is the declaring unit's
  `_look_actions`, not `axes_own_look`.

## General mechanism

`probe_preview_modes(look_capabilities)` plus `look_capabilities` on
`probe_ctx`. Draft is the cheap EEVEE beauty the live builder already uses
for iteration.

## Rejected patch-level alternatives

- Prompt "don't hide blockers": repair 2 followed the only image the probe
  gave it.
- Always probe EEVEE canonical: cost; draft is the existing iteration engine.
- Drop shaft blockers from the unit: fixture-shaped.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_probe_preview_adds_draft_only_for_look_units`.

## Release and rollback

No schema migration. Rollback is solid-only probe and identifier-scanned
summary.

## Remaining limitations

Draft still is not the critic's canonical EEVEE samples. Particle projected
size vs "hard-edged cards" remains a missing scalar instrument. `find_recipe`
was never called this session. Live `compare_frame` 0/0 no longer locks
`run_bpy` on a look unit with no image bindings (HIR-0044). Repair binds
`cannot_express_in_scope` on the candidate server (HIR-0043).
