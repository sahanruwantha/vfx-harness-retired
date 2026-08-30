---
id: HIR-0135
title: Unit-local judgment retains full DAG authority
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: sibling_visibility_misbilled_to_active_unit
mechanism: unit_local_judge_view_with_full_dependency_authority
adr: ADR-0006
---

# Unit-local judgment retains full DAG authority

## Observed failure

Room 1046 Layer 2 build run `20260830T061008Z-1e787d` completed the live
`building_mass` mutation with all six exact bound checks passing. At the executable
evidence boundary the harness nevertheless reported eight bound checks and instructed
the same unit to create `building.roof.*` and `site.ground.*` because
`roof-visible-fraction` and `site-visible-fraction` matched no objects. Those roles
belong to dependent sibling units. The run was interrupted before repair could cross
unit ownership.

## Root cause

`build_layer` compiled a unit-local `Layer` value for judgment by replacing
`stages` with `(active_unit,)`. HIR-0132 derives visibility activation from every
required claim's typed `repair_owner`. With sibling rows absent from that truncated
tuple, their ownership looked unknown; fail-closed fallback correctly treated unknown
visibility as layer-active, but charged it to the wrong unit.

The full typed unit tuple was already present in the selected view and passed separately
to the scope-card compiler. Runtime judgment discarded that authority by projection.

## Decision

- `active_unit` remains the sole mutation, claim, and repair boundary.
- A unit-local layer view may narrow judges, axes, title, reads, and script identity.
- It must retain the parent layer's complete `stages` tuple for dependency closure,
  repair-owner activation, interface authority, and conservative ambiguity handling.
- Missing genuinely authoritative ownership still fails closed. Runtime projection may
  not manufacture ambiguity by removing sibling rows.

## General mechanism

`_active_unit_layer_view` is the single compiler for the runtime view. It retains the
complete unit DAG while projecting unit-local judgment fields. Existing evidence paths
continue to call `geometry_vis_protection_ids_for_unit` against `layer.stages`, which now
contains the selected authority rather than the active row alone.

The regression fixture uses three dependency-ordered geometry units with distinct
required visibility repair owners. The first unit's runtime view retains all three
units, but its due protection set contains only its own row; the middle and final rows
remain future debts.

## Rejected alternatives

- Prompt the builder to ignore sibling failures: the evidence verdict is authoritative
  repair input, so prompt wording cannot repair a false binding.
- Drop conservative fallback: truly missing or ambiguous repair ownership must remain
  fail-closed.
- Copy sibling ids into an exclusion list: that duplicates plan authority and drifts on
  every rematerialization.
- Let the active unit create future roles: that violates derived write clusters, unit
  scope, dependency order, and replay identity.

## Validation

The focused regression proves the unit-local judgment projection retains the full DAG
and bills only `vis.mass` to the first unit, not the later roof/site rows. Production
validation resumes from an audited Layer 2 retry and must show `building_mass` sealing
without either sibling visibility row entering its executable verdict.

Validation on 2026-08-30: the focused visibility and builder-instrument suites passed
33 tests, the full repository suite passed 630 tests in 41.86 seconds, and
`.venv/bin/ruff check src tests` passed.

## Release and rollback

No persisted schema change. Rolling back restores a deterministic ownership violation
whenever a multi-unit geometry layer has typed visibility repair owners on later units.
