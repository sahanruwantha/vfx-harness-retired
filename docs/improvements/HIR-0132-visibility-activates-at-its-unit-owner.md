---
id: HIR-0132
title: Visibility activates at its typed unit owner
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: multi_cluster_geometry_visibility_is_unrepresentable
mechanism: repair_owner_relative_visibility_activation
adr: ADR-0006
---

# Visibility activates at its typed unit owner

## Observed failure

Room 1046 Layer 2 materialization run `20260830T052148Z-177708` produced three
truthful geometry clusters: `building_mass`, `building_roof`, and `site`. Finalization
required a frame-39 `visible_fraction` row for each judged subject. After those rows were
added, HIR-0051/0057/0106 reported a mutual cycle among all three units with five directed
producer edges.

The suggested recovery was to consolidate every judged surface into one geometry unit.
HIR-0083 made that illegal: the reserved `building.mass.*`, `building.roof.*`, and
`site.*` namespaces derive distinct write clusters. Existing dressable geometry was not
available because this layer creates the subjects. Thus a heterogeneous geometry layer
could satisfy atomicity or visibility protection, but not both.

## Root cause

`layer_active_visible_fraction_ids` made every lifecycle-active row due at every geometry
unit from the start of the layer. A root massing unit therefore had to prove visibility
of roof and site surfaces that did not exist until later units, while those later units
simultaneously had to protect the root's row. The graph instrument accurately described
the implementation, but the implementation used a layer-sized due boundary after the
architecture had moved mutation, evidence, and checkpoints to bounded units.

Ignoring protection entirely would reopen HIR-0051. The missing concept was activation at
the claim's already-typed `repair_owner` unit.

## Decision criteria

- A required `visible_fraction` row is due at the unit named by the binding claim's
  `repair_owner`; HIR-0051 still requires that owner to provide camera or mutate/dress
  every selected role.
- The owner evaluates its row. Every downstream geometry unit whose dependency closure
  contains that owner re-evaluates it as protected evidence.
- Geometry before the owner does not owe the future row. Two unordered geometry units
  are rejected once with the missing acyclic dependency derived from stable authored
  position; array order itself does not grant readiness.
- Rows without a typed required-claim owner retain conservative layer-wide activation and
  HIR-0106 cycle reporting.
- Composed canonical replay and layer acceptance still evaluate the full unit claim fan-in.
- No shot, frame, role token, display name, or fixed unit count participates.

## General mechanism

`visible_fraction_repair_owners` derives contract-to-owner bindings from required typed
claims. `geometry_vis_protection_ids_for_unit` intersects those owners with the active
unit's transitive dependency closure plus itself; ownerless rows remain due everywhere.
The builder uses that function for live evaluation, finalization, canonical replay, and
revalidation through the existing `_geometry_protected_vis_ids` boundary.

`geometry_vis_dependency_gaps` uses the same owner map. A downstream unordered geometry
unit receives one `geometry-vis-dependency` finding naming the earlier owner and legal
acyclic dependency. A row whose owner is downstream is not a debt of the earlier unit.
The independent plan gate and materialization validator consume the same detector.

## Rejected alternatives

- Remove sibling visibility protection: later geometry could occlude sealed subjects.
- Consolidate all geometry: distinct derived write clusters make that mechanically false.
- Use authored array order as authority: HIR-0119 makes dependencies the execution DAG;
  authored position is only a tie-break among ready units.
- Add shot-specific lifecycle fields or role-name exceptions: the required claim already
  contains a general typed owner.
- Let missing roles read INAPPLICABLE: that would hide an actually due downstream
  regression instead of distinguishing it from a future row.

## Validation

Focused fixtures prove: typed owner rows are due on their owner; a dependent successor
protects both owner and local visibility; an unordered successor receives one dependency
finding rather than a mutual cycle; adding the dependency removes the visibility finding;
and unowned rows retain the old conservative future-producer/cycle behavior.

Validation on 2026-08-30: the focused visibility, builder, materialization, plan,
atomicity, evidence-scope, and unit-card suites passed 224 tests in 10.66 seconds; the
full repository suite passed 626 tests in 42.70 seconds. `.venv/bin/ruff check src tests`
passed.

Production validation is a fresh bounded rematerialization of the retained failure class,
followed by Layer 2 build and composed empty-scene replay.

## Release and rollback

No persisted schema change: activation is derived from existing required-claim authority.
Rollback restores layer-wide early activation and makes any multi-cluster geometry layer
with visibility structurally unpublishable.

## Remaining limitations

Ambiguous or multiply bound visibility remains conservatively layer-active. A future
general multi-owner coordination contract would need explicit authority rather than an
inference added here.
