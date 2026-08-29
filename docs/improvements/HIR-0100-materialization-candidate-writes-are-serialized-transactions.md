---
id: HIR-0100
title: Materialization candidate writes are serialized transactions
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: materialization_candidate_lost_update_and_gate_bypass
mechanism: locked_revision_checked_candidate_transaction
adr: ADR-0006
---

# Materialization candidate writes are serialized transactions

## Observed failure

`stage_materialization_unit` and `patch_materialization` both performed a
read-modify-write on the same candidate. Two overlapping staging calls could read the
same prefix and let the later rename erase the first unit. Patch could also append or
replace `/layer/stages`, bypassing the local atomicity and point-projection gates that
the dedicated staging boundary had just gained.

## Root cause

Atomic file replacement protected readers from partial JSON, but it did not serialize
the read/validate/write transaction. Candidate mutation had two write surfaces with
different pre-write predicates. The prompt's request to issue one call at a time was
coordination advice, not occupancy control.

## Decision criteria

The mechanism must prevent lost updates across processes, reject stale observations,
keep incomplete scratch repairable, preserve pointer batching, and refuse invalid unit
edits before candidate bytes change. It must not add candidate JSON or lock management
to model context.

## General mechanism

Every stage and patch now enters one file-locked candidate transaction. The transaction
reads and verifies an optional SHA-256 revision while holding the lock, mutates an
in-memory object, runs the applicable local unit gates, and atomically replaces the file.
The materialization tool session carries the revision and serializes its async calls;
`materialization_status` exposes the current revision and whether it still matches the
session.

Only `stage_materialization_unit` may add, replace, or reorder stage rows. Patch remains
the field-repair surface for an existing unit, but edits to units, scene contracts, or
requirement bindings run the same WorkUnit, uniqueness, contract-row, atomicity, point
ownership, and exact-interface checks before writing. Full materialization validation
runs against a temporary proposed revision while the lock is held, so returned findings
describe exactly the revision that is committed.

## Rejected patch-level alternatives

Prompting the planner to wait cannot prevent concurrent calls. Atomic rename alone
cannot prevent two valid writers from overwriting each other's prefixes. Forbidding all
stage-field patches would remove the pointer-addressed repair mechanism and force a full
unit rewrite for a local validation finding.

## Validation

A deterministic overlapping-writer fixture blocks the second writer behind the first,
then proves that its stale revision is rejected and the first mutation remains intact.
A second fixture proves patch cannot append a stage row and cannot add authored `family`
padding to an existing row; both failures leave the candidate byte-identical. Existing
incremental staging, batched pointer repair, product/motion interface, and plan-tool
fixtures remain passing.

## Release and rollback

No published authority schema changes. The lock file is disposable run-local state and
the revision is a digest of the existing candidate bytes. Rollback would restore a
lost-update window and a second, weaker unit publication surface, so it is unsafe.

## Remaining limitations

Revision conflict is deliberately fail-closed; a materialization session whose candidate
was changed by another writer must restart from the current candidate rather than silently
adopt authority it did not write.
