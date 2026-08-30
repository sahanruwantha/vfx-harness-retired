---
id: HIR-0161
title: Same-layer dressing is not owner-granted appearance
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: same_layer_dress_misread_as_layer_grant
mechanism: same_layer_dress_staging_gate
adr: ADR-0007
---

# Same-layer dressing is not owner-granted appearance

## Observed failure

Hansa Silk Road layer-2 rematerialization `20260830T165703Z-4c8d84` staged
`hero_shade` with `depends_on` massing and facade (HIR-0160) then finalized.
Collectable validation refused `mutates.dresses` of `hero.facade.window_strip`
and `hero.tower.clearance`: "no other layer declares these selectors dressable;
the owning layer's row must list them under `dressable`".

The session unstaged, restaged producers with a same-layer `dressable` grant,
and finalized again. The same finding remained. Turn 25 died
`max_turns_exhausted`. The live view stayed `996f2d1d` (HIR-0027).

## Root cause

ADR-0007 dressing closes only over selectors some **other** layer lists under
`dressable`. Same-layer mutation roles can never satisfy that predicate.
The rejection's "owning layer's row" clause taught the materializer to grant
`dressable` on this layer. That field is a downstream grant, not same-layer
authority. Raising the turn cap would spend the same last turns on the same
misread.

## Decision criteria

- Staging refuses a unit whose `dresses` intersect any same-layer
  `mutates.roles`, naming the producer unit ids.
- Collectable validation and the plan gate emit `same-layer-dress` for that
  intersection and do not also emit `dressing-closure` for those selectors.
- Remaining undeclared dresses (not mutated on this layer, not granted
  elsewhere) still fail `dressing-closure`, with a fix that names a
  **different** layer's `dressable` and says this layer's grant is
  downstream-only.
- Do not raise turn caps.

## General mechanism

`same_layer_dress_gaps` is the domain predicate. `_validate_local_staged_units`
fails closed before candidate write. Materialization notes and the plan gate
share that predicate.

## Rejected patch-level alternatives

- Raise `--max-turns`: the last turns would retry the same illegal dresses.
- Treat this layer's `dressable` as same-layer authority: that collapses
  ADR-0007 into intra-layer mutation.
- Prompt-only "do not dress siblings": the DAG would remain representable.

## Validation

A shading unit that dresses a sibling geometry role fails domain, staging,
materialization, and plan-gate checks even when this layer lists that role
under `dressable`. An earlier-layer `dressable` grant still clears
`dressing-closure` for a later-layer dresser.

## Release and rollback

No schema migration. Rollback would restore same-layer dresses that only fail
at finalize with a misread grant.

## Remaining limitations

Carrier `depends_on` (HIR-0160) remains legal without dressing. Assigning a
material to sibling mesh still needs this unit's own mutation/material_roles
or a later look layer.
