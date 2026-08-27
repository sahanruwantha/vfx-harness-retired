---
id: HIR-0051
title: Occlusion evidence needs a ray-changing owner
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: vis_bound_to_unit_that_cannot_change_rays
mechanism: vis_repair_owner_and_per_role_and_geometry_protection
adr: null
---

# Occlusion evidence needs a ray-changing owner

## Observed failure

Run `20260827T124545Z-12fa7e` rebuilt layer 2 after HIR-0050. `materials_energy`,
`lighting_bloom`, and `detail_instancing` passed. `atmosphere` recorded
`hypothesis_falsified` (`hf-76bc547dfa3492d32dfa`) on `atmosphere-hero-vis-f72`
and `atmosphere-hero-vis-f150`. Exit 7. Repair `cannot_express_in_scope` was
the correct stop: density and particulate cuts left mesh vis at 0.4 / 0.092.

The published rows are `visible_fraction` on `world.proxy_core`. Atmosphere
mutates only `world.atmosphere_volume` and `world.particulate_field`. Volumes
do not participate in the mesh raycast.

A sibling `detail_instancing` unit (`provides: ["geometry"]`) sealed without
those rows. Its freeze protection is `all_active_upstream_interfaces`, so
same-layer lighting vis was not on the list. `bloom-silhouette-vis` unions
`proxy_rings` + `proxy_core` at f150 and passed 1.0 while core-only vis failed.

## Root cause

The plan gate treats `visible_fraction` as observation-only so roles need only
exist *somewhere* in the plan. That exception exists so a **camera** unit can
answer for geometry it does not own (HIR-0019). A volume-only unit used the
same hole as required repair.

The probe pools every named role into one `seen/on_screen` scalar. A two-role
row can PASS while one named role is below `lo`.

Later geometry in the same layer does not freeze-protect sibling vis, so an
occluding mesh can seal while the vis row it broke stays someone else's
problem.

Sealing is not the defect. Flexibility is `hypothesis_falsified` →
`vfx units replan`, not extra mutation authority on sealed work.

Classification: ownership / evidence-binding defect, then union hide, then
missing sibling protection.

## Decision criteria

- A required claim that binds `visible_fraction` is repaired by a unit that
  `provides: ["camera"]` **or** mutates/dresses every `roles` selector on that
  row.
- Camera observation of plan-declared geometry roles still publishes.
- A volume-only unit binding mesh vis as required repair is refused at
  materialization and the plan gate.
- Multi-role `visible_fraction` is logical AND: any named role below `lo`
  fails; the probe reports per-role fractions; the scalar is the min.
- A unit that `provides: ["geometry"]` freeze-protects lifecycle-active vis
  on this layer, including sibling-owned rows, and cannot seal while one fails.
- A non-geometry sibling does not gain that extra vis set.

## General mechanism

- `vis_roles_unrepairable_by` in `work_units.py`; the same check in
  `jit_materialization.py` (write-hook) and `plan_gate.py` (gate). Observation
  vis is camera-or-mutator, not “any unit.”
- Pure `visible_fraction_min` / `_holds(..., role_fractions=)`; the Blender
  probe measures each named role and stores `role_fractions`.
- `geometry_vis_protection_ids` unions layer-active vis into freeze
  `protected_contract_ids` for geometry providers. Builder live probes,
  evidence scope, and executable verdicts re-evaluate those ids.

## Rejected patch-level alternatives

- Unsealing layers so later units can mutate sealed work: go-back is a typed
  replan, not extra mutation authority.
- Teaching `visible_fraction` to honor `hide_render`: remaining limitation;
  the f150 fail was the wall, not the hide window.
- Consuming the finding before the lints exist: remat would republish the
  same atmosphere-vis shape.
- Prompt “don’t bind vis on fog”: the next volume unit would use the same hole.

## Validation

- `tests/unit/test_vis_repair_authority.py`: volume-only required vis is
  refused at the gate and materialization; camera-shaped binding still
  publishes; two-role vis with one role below `lo` fails; single-role vis
  still passes; geometry freeze includes the vis id; a failing protected vis
  blocks the executable verdict; a non-geometry sibling does not gain extra
  vis protection.
- `tests/unit/test_plan_improvements.py`: camera observation of a
  plan-declared role still closes; an undeclared role still violates
  role-selector-closure.

## Release and rollback

No schema migration. Rollback restores observation-only vis for every unit,
pooled multi-role vis, and upstream-only freeze protection.

## Remaining limitations

`visible_fraction` still ignores `hide_render`. A hide-window that occludes
nothing on the evaluated mesh can still PASS while the plate is empty.

## Resume

The selected view is gate-dirty: `vfx evals plan` reports `vis-repair-owner`
on atmosphere's core vis rows. Remat layer 2 without `--discard-accepted`
(HIR-0052): matching lighting/detail/materials digests stay; the vis-owner
closure is superseded. Do not consume `hf-76bc547dfa3492d32dfa` first unless
the replacement leaves atmosphere's digest unchanged. Do not empty-base
layer 2. `vfx build --layer 2` only after the replacement is gate-clean.
