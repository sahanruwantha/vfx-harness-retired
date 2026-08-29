---
id: HIR-0098
title: Camera availability is only a typed capability
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: role_name_impersonates_camera_capability
mechanism: typed_camera_bootstrap_only
adr: ADR-0005
---

# Camera availability is only a typed capability

## Observed failure

The plan gate preferred units declaring `provides: ["camera"]`, but when none existed it
fell back to searching mutation-role text for the substring `camera`. A fixed
`camera.target` control could therefore make projected/rendered evidence appear
camera-ready for its dependants and every later layer.

## Root cause

HIR-0086 and HIR-0087 made camera capability typed and role-bound, but the older
composition-bootstrap predicate retained its pre-migration name heuristic. The global
capability gate usually caught production schema-5 plans, masking the weaker local
predicate in isolated or partially materialized views.

## General mechanism

`composition-bootstrap` now recognizes only an explicit unit capability declaration.
Role tokens never imply capabilities. Dependency closure and later-layer propagation use
that same typed set, so `camera.target`, `camera_proxy`, or any other name cannot create a
camera fact.

## Validation

An atomic camera/blockout fixture passes only after declaring `provides: ["camera"]`.
A heterogeneous control named `camera.target` with no capability produces a blocking
`composition-bootstrap` finding.

## Release and rollback

No schema change. Plans relying on role-name inference must declare the existing typed
capability. Rollback would restore dual camera predicates and is unsafe.
