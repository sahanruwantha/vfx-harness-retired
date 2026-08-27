---
id: HIR-0039
title: Look-less composition is not a critic session
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: composed_canonical_reopened_look_without_a_unit
mechanism: composition_fans_in_executable_unit_claims
adr: null
---

# Look-less composition is not a critic session

## Observed failure

Run `20260827T031330Z-c4687e` sealed `cam_path_core` and `cam_parallax_proxies`
on bound scene contracts (`decided_by: unit_executable_evidence`, 5.0). Both
units declared `look_capabilities: []`. Composed `build/01_camera_spine.py`
then rendered canonical EEVEE, found no optical signal, skipped the critic
(HIR-0032), and wrote look scores of 1 on every `layer.owns` axis —
`camera_continuity`, `camera_parallax_depth`, and `camera_collision_clearance`
(unbound from units in the same view). `shot.json` recorded layer 1 as failed.
`vfx build --layer 1` still exited 0 because only unpassed *units* raised.

## Root cause

A layer used to be one critic loop. Units gained an executable path:
`_judge_unit_or_layer` calls `_executable_unit_verdict` when `active_unit` is
set. Multi-unit composition called `_verify_script` without `active_unit`.
`_unit_evidence_ids(None)` documents that omission as "preserve
layer/composed evaluation" — the pre-unit critic session. `_executable_unit_verdict`
returns `None` for a missing unit, so composition always entered `_judge`.
HIR-0032 only changed the black-plate branch *inside* `_judge`; it did not
stop composition from being a look vote. Classification: missing compiled
context at the fan-in; identifiers in `layer.owns` are not unit claims.

## Decision criteria

- When every stage declares empty `look_capabilities` and every required
  claim is `executable_required`, composed canonical fans in those claims as
  the active unit. No critic look vote.
- A look-owning stage on the same layer keeps the critic path.
- Look-less evaluation that cannot produce an executable verdict does not
  fall through to `_judge`.
- `vfx build` exits 9 when units passed but the composed ledger verdict did
  not — the same meaning `vfx run` already used.

## General mechanism

- `_composition_judge_unit(layer)` returns a fan-in unit or `None`.
- Composition `_verify_script(..., active_unit=fan_in)`.
- `_judge_unit_or_layer`: look-less + abstaining executable verdict uses
  `_lookless_without_executable_verdict`, never `_judge`.
- `LayerVerdictFailed` → `RequestedExit(9, …)`.

## Rejected patch-level alternatives

- Light the camera layer so EEVEE is not black: lighting is another layer's
  mutation scope (HIR-0032).
- Auto-repair by sending the L1 builder after the black plate: wrong owner;
  `cannot_express_in_scope` is the legal abstention, not extra lights.
- Prompt "composition is layout": `_judge` would still run.
- Drop `camera_collision_clearance` from `layer.owns` for this shot: a
  fixture-shaped patch. Fan-in already ignores unbound critic axes.

## Validation

- `tests/unit/test_look_capabilities.py`:
  `test_lookless_composition_fans_in_unit_claims`,
  `test_look_owning_stage_keeps_composed_critic_path`,
  `test_lookless_fallback_never_calls_the_critic`.

## Release and rollback

No schema migration. Rollback is `active_unit=None` at composition, which
again scores black plates as look 1.0.

## Remaining limitations

A layer that declares look capabilities and produces no-signal plates still
fails closed without a critic call (HIR-0032). Mixed look/executable stages
on one layer still use the critic for composition; splitting look into its
own unit remains the plan. Look-owning stages can no longer seal 5.0 on
scene counts (HIR-0046).
