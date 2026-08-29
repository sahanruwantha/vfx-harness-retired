---
id: HIR-0092
title: Materialization stages one bounded unit at a time
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: monolithic_materialization_first_write_stall
mechanism: deterministic_seed_and_incremental_unit_staging
adr: ADR-0006
---

# Materialization stages one bounded unit at a time

## Observed failure

Replacement run `20260829T055444Z-d2c79b` loaded the exact Layer-1 card, queried the
evidence vocabulary, and measured all four judge references within 12 seconds. It then
emitted no transcript event, candidate write, or validator call for three consecutive
90-second windows. The process remained alive while preparing the first monolithic JSON
document. The unpublished overlay protected live authority, but no progress boundary
existed before the whole layer document was generated.

## Root cause

Layer materialization is supposed to create a DAG of atomic work units, yet its write
surface required the model to serialize the entire layer DAG, every claim, every scene
contract, and every requirement binding in one first `Write`. The harness already knew
the schema wrapper, bundle digest, global structural row, empty image/acceptance fields,
and output path, but made the model repeat all of them. A layer could be atomically split
for builders while still being a monolithic planning response.

## General mechanism

The harness now deterministically seeds the unpublished candidate with schema, bundle
identity, the exact global layer row, and empty collections. Materialization has no
generic `Write`. It calls `stage_materialization_unit` once per bounded unit, passing only
that unit, the scene contracts it authors, and the owned requirement bindings it closes.
Each call locally parses the WorkUnit, validates contract rows, refuses duplicate unit,
contract, and requirement ids, and atomically appends to scratch state.

After all units are visible, `finalize_materialization` performs the existing complete
cross-unit/global validation. `patch_materialization` remains the atomic repair surface.
Nothing selects live authority until the same publication transaction and plan gate pass.
An incomplete staged candidate is not success: the session postcondition is a fully valid
candidate, not merely a changed file.

## Validation

Fixtures prove deterministic seeding omits mutable JIT authority, one-unit staging
produces a fully valid candidate, duplicate staging leaves bytes unchanged, and the new
tools appear only in materialization sessions. Policy fixtures prove generic `Write` is
denied and session success requires full candidate validity. The full repository suite
passes 532 tests. Production acceptance is a
replacement Layer-1 materialization that emits bounded unit-stage events inside the
90-second monitor and still publishes through the existing gate.

## Release and rollback

The selected materialization schema and published view are unchanged; only unpublished
authoring becomes incremental. Existing overlays remain readable. Rollback restores a
monolithic first response with no observable or recoverable unit boundary, so it is
unsafe.
