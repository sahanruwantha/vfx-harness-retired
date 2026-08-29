---
id: HIR-0088
title: Materialization repairs need atomic batches
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: repeated_single_field_repair_turns
mechanism: atomic_multi_pointer_patch_transaction
adr: ADR-0006
---

# Materialization repairs need atomic batches

## Observed failure

Layer-1 materialization run `20260829T045319Z-cee4af` received five collectable findings
after its first candidate write. The agent could repair only one RFC 6901 pointer per
`patch_materialization` call. Each independent edit therefore incurred another model/tool
turn and another full materialization validation. The same run used 31 turns and 1,035.4
seconds; several calls returned an unchanged finding set after changing only one field.

The initial hook did deliver its detailed findings to the model—the exact next patches
prove that—but the terminal log printed only the count, making live diagnosis look like
blind repair.

## Root cause

The validator already collects independent findings in one pass, but its mutation
instrument exposed only a scalar operation. Transport grain was smaller than diagnostic
grain. The agent had to serialize mechanically independent edits and pay repeated
validation even when no decision depended on an intermediate state.

## General mechanism

`patch_materialization` now accepts either one `pointer`/`value` pair or a non-empty
`patches` array. All JSON-encoded values are parsed first, all pointers are applied to an
in-memory document, and the candidate is atomically written only after every pointer
succeeds. The validator runs once on the completed transaction. A bad pointer leaves the
file byte-for-byte unchanged. JSON Pointer `-` append is part of the same path, so a unit
split can add a stage without replacing a whole stage array.

The single-patch application API delegates to the batch mechanism, preserving one
semantic implementation. Materialization instructions tell the agent to group independent
findings. Write-time validation also logs the actual findings, not only their count, so a
90-second monitor can distinguish active repair from guessing.

## Rejected alternatives

Increasing the turn budget pays for the defect. Letting the agent rewrite the complete
document risks unrelated drift. Applying each patch directly to disk before validating
the rest leaves partial state when a later pointer is invalid. Skipping intermediate
validation without an atomic mutation boundary weakens rather than improves the loop.

## Validation

Heterogeneous product-material and motion-curve fixtures apply two independent mutations
with one validation. They cover object replacement, array element replacement, and array
append. An injected invalid later pointer proves the candidate remains byte-identical.
The existing single-pointer API and full materialization suite continue to pass.

## Release and rollback

This is an additive tool-schema change and does not change published authority. Older
single-patch calls remain valid. Rollback restores repeated model turns and is safe for
data but regresses cost and latency.
