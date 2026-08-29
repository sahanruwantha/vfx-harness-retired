---
id: HIR-0095
title: State observation does not invent write families
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: observational_contract_creates_false_write_cluster
mechanism: mutation_semantic_instrument_classification
adr: null
---

# State observation does not invent write families

## Observed failure

Layer 1 rematerialization classified a fixed `camera.target` Empty as mixed mesh and
keyframe work. Its location was bound through `object_property`, which the atomicity
classifier labeled `mesh`; an `animation_count <= 0` proof was labeled `keyframe`.
The materializer responded by dropping the absence-of-animation proof and later tried
to satisfy selector closure with a false camera-rig control mapping.

## Root cause

The write-cluster registry classified evidence kinds without preserving the difference
between a mutation and an observation. Object-level transforms are placement/control
state, not polygon topology. A zero-animation upper bound proves that no animation was
authored; it cannot create keyframe mutation authority.

## General mechanism

Non-data `object_property` rows resolve to the `control` instrument family. Typed light
and camera data paths retain their specialized families. `animation_count` remains
`keyframe` work when it requires animation, but an exact zero or non-positive upper
bound is observation-only and contributes no write family. A fixed Empty therefore
derives one placement-control publish interface while retaining executable proof that
it was not animated.

## Validation

Heterogeneous product-target and motion-control fixtures bind location plus zero
animation and derive exactly one control-host/control cluster. A positive animation
count remains keyframe work. The complete repository suite remains the regression
boundary.

## Release and rollback

This is a stricter semantic correction to derived atomicity, with no schema change.
Rollback would again make observational state rows force false unit splits and is unsafe.
