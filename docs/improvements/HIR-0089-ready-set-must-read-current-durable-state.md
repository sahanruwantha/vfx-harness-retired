---
id: HIR-0089
title: Ready-set scheduling must read current durable state
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: stale_in_process_unit_state_snapshot
mechanism: atomic_durable_ready_query
adr: ADR-0006
---

# Ready-set scheduling must read current durable state

## Observed failure

Build run `20260829T052107Z-93d1d9` completed `camera_path_curve`, passed all owned
contracts, replayed its script from empty, froze a checkpoint, and transitioned the unit
to `passed`. Both `camera_target_aim` and `camera_focal_length` were pending direct
dependants. The same build process then raised `layer 1 has no dependency-ready work
unit` instead of selecting either successor.

The durable state was correct: the producer status was `passed`, its stored unit digest
matched current authority, and a fresh `ready_units` calculation returned both
dependants. Only the in-process calculation returned none.

## Root cause

`build_layer` loaded durable unit state once before entering its multi-unit loop. After a
unit passed, it updated `passed_units` in memory and wrote the authoritative state
transition, but computed digest-matched producers from the old snapshot in which that
same unit was still pending. Readiness therefore combined lifecycle facts from two
generations: a new passed-id set and old producer status/digests.

## General mechanism

`ready_from_durable_state` now performs one scheduling read: reload current unit state,
validate all unit ids and digests against the active DAG, derive passed ids, verify
digest-matched sealed producers, and compute the deterministic ready set. The builder
calls this query on every loop iteration. An optional eligible set can remove passed
units whose local replay artifacts are absent, but it cannot add authority beyond
durable state.

The checkpoint remains accepted throughout. Fixing scheduler observation is not a reason
to retry, reopen, or invalidate proven scene work.

## Rejected alternatives

Mutating the cached dictionary alongside every state transition creates a second state
implementation and will drift again. Treating every passed id as sealed drops the digest
binding from HIR-0084. Restarting the build after each unit hides the bug behind process
boundaries and adds setup cost. Retrying the passed producer discards valid evidence.

## Validation

Heterogeneous product-assembly and motion-finish DAG fixtures capture a state snapshot,
transition their producer to passed afterward, and prove that the stale snapshot sees no
sealed producer while the durable ready query selects the successor. Existing interface
digest, mismatch, and status-edge readiness fixtures remain unchanged.

Production acceptance resumes the same Layer 1 state: `camera_path_curve` must stay
passed and the next invocation must select one of its two pending dependants.

## Release and rollback

No schema migration is required. This changes only when state is read. Rollback
reintroduces a deterministic crash after the first unit of every fresh multi-unit DAG.
