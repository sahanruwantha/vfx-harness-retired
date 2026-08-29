---
id: HIR-0096
title: Point projection observes a consumed interface
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: cross_unit_observation_forced_into_mutation_scope
mechanism: read_only_digest_bound_projection_input
adr: ADR-0003
---

# Point projection observes a consumed interface

## Observed failure

Run `20260829T064933Z-870ea1` correctly produced `camera.target` before the camera and
made the camera unit own `projected_origin_x/y`. The selector-closure gate then required
the camera unit to mutate the target it measured. At the turn limit the materializer
declared `camera.target` as a camera-rig control mapped to `camera.rig`. The schema passed,
but the declaration was semantically false and would have hidden the dependency.

## Root cause

HIR-0094 established camera ownership but did not encode the read side of the
camera/target relation. Selector closure had only mutation or unrestricted observation;
it could not express a read-only same-layer subject whose value came from a sealed
predecessor. HIR-0084 already supplied the missing typed interface mechanism, but point
projection did not require it.

## General mechanism

A camera-owned `projected_origin_x/y` contract treats its measured role or control as
observation-only. When that selector is produced by another unit in the same layer, the
camera owner must declare the producer as a dependency and consume an exact offered
publish-interface id and kind. The offered interface must export the measured selector.
The camera unit may not mutate the selector it observes. Roles supplied by accepted
upstream layers continue through layer dependency and protected-interface authority.

The same predicate runs at incremental unit staging, complete materialization validation,
and the independent plan gate. Tool vocabulary tells the materializer to publish and
consume `placement_control`; rejection names the selector, producer, contract, and legal
causal shape.

## Validation

Product-target and motion-control fixtures pass only with `target.publish` consumed as
`placement_control`. Removing `consumes` is rejected by both validation and the plan gate;
incremental staging leaves the candidate byte-identical. A camera that mutates its own
observed target is rejected. The observation no longer triggers role/control mutation
closure, while undeclared selectors remain closed.

## Release and rollback

No schema change. Existing point-projection plans with a same-layer target but no typed
consume must rematerialize. Rollback would restore either false camera mutation authority
or untyped predecessor reads and is unsafe.
