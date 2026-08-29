---
id: HIR-0059
title: Failed-artifact warm starts must match the current unit digest
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: superseded_artifact_replay
mechanism: artifact_unit_digest_pin
adr: null
---

# Failed-artifact warm starts must match the current unit digest

## Observed failure

After Layer 2 rematerialized `material_energy_language` from a geometry-producing swatch unit into
a dressing-only unit, build run `20260827T192122Z-6eb6c8` replayed the old failed script solely
because the unit id and artifact path still existed. The warm scene contained four superseded
swatch meshes and old semantic material roles before the new session began. Every new material
contract therefore read missing, and the builder proposed deleting stale objects outside its new
scope.

## Root cause

Warm-start eligibility checked only the prior layer-level status and script existence. Durable
work-unit state correctly knew the replacement digest, but the layer ledger did not bind a failed
artifact to the unit digest that authored it. Same-id rematerialization made an obsolete artifact
look retryable.

## General mechanism

Every completed/truncated/failed ledger mark pins `artifact_unit_hash` to the current full
`WorkUnit` digest alongside the script hash. Warm start requires a retryable status, an existing
script, a non-empty artifact digest pin, and exact equality with the selected unit digest. Beginning
a new attempt clears the artifact pin until that attempt reaches a ledger mark. Legacy artifacts
without a pin and superseded same-id artifacts fail closed to clean prior-layer replay.

## Rejected patch-level alternatives

Comparing only the unit id repeats the defect. Comparing only script bytes cannot say which scope
authorized those bytes. Deleting stale objects after replay asks the new unit to mutate authority
it never owned. Removing all warm starts discards valid measured retry work.

## Validation

The integration harness proves same-digest failed artifacts remain eligible, changed-digest and
missing-pin artifacts are refused, and passed artifacts continue through deterministic
revalidation instead. The whole integration harness remains green.

## Release and rollback

The ledger adds optional fields and treats absence strictly; no migration fabricates authority for
legacy artifacts. Rollback restores broader replay and is unsafe after same-id replanning.
