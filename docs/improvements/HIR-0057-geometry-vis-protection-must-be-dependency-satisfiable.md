---
id: HIR-0057
title: Geometry visibility protection must be dependency-satisfiable
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: unsealable_unit_dag
mechanism: geometry_visibility_dependency_gate
adr: null
---

# Geometry visibility protection must be dependency-satisfiable

## Observed failure

Layer 2 rematerialized a root `material_energy_language` unit that declared
`provides: ["geometry"]` so it could create material swatch proxies. The same layer declared
`detail-silhouette-visible-f150` on a role created by the downstream
`detail_scale_instancing` unit. HIR-0051 correctly added that active visibility row to the root
geometry unit's freeze protection. The builder reached four of five contracts, then spent turns
polishing materials while the remaining protected role did not yet exist and was outside scope.
The selected plan had passed both materialization validation and `vfx evals plan`.

## Root cause

The runtime protection rule knew that every geometry unit protects all lifecycle-active layer
visibility, but neither publication boundary checked whether each protected role was available in
the unit's dependency closure. A same-layer producer that runs later turns conservative protection
into a cycle: the earlier unit cannot seal until the later producer exists, while the later unit
cannot run until the earlier unit seals.

## General mechanism

For every unit declaring `provides: ["geometry"]`, materialization validation and the plan gate
resolve lifecycle-active `visible_fraction` rows. When a selected role is declared by another
same-layer unit, at least one such producer must be in the geometry unit's transitive dependency
closure. A producer outside that closure is a blocking `geometry-vis-dependency` finding. The legal
repairs are to remove geometry from the earlier unit and dress existing owner-granted surfaces,
move the producer before it through a real dependency, or split/reorder the DAG. Roles with no
same-layer producer remain legal upstream interfaces and are not guessed to be missing.

## Rejected patch-level alternatives

Exempting swatch geometry from HIR-0051 would make protection depend on shot-specific intent.
Ignoring a missing role until its producer runs would let an earlier geometry checkpoint seal
without proving the interface it is required to preserve. Retrying the builder cannot change the
published dependency cycle.

## Validation

A tracked unit fixture creates a root geometry unit, a downstream detail producer, and an active
visibility row on the detail role. The domain detector and plan gate must reject the root. Removing
`geometry` from the root must remove only this finding. The focused visibility suite and full
repository suite remain green.

## Release and rollback

This is a stricter publication validation with no persisted schema change. Rollback removes the
new finding, but would restore a plan shape that is mechanically impossible to execute.
