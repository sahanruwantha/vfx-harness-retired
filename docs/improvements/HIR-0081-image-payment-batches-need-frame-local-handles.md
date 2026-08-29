---
id: HIR-0081
title: Image-payment batches need frame-local handles
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: batch_api_cannot_express_per_item_provenance
mechanism: per_check_candidate_handle
adr: ADR-0008
---

# Image-payment batches need frame-local handles

## Observed failure

In run `20260828T001601Z-9ca8f6`, the builder rendered immutable candidates for frames 72
and 150, then submitted three image-debt payments in one `propose_checks` call. The tool's
schema allowed a check array but only one batch-level `after_handle`. The f72 check was
rejected because the only expressible handle pointed to f150, even though the correct f72
handle was already present in the current-run registry.

## Root cause

The batch transport grouped independently framed evidence rows but modeled provenance as a
property of the batch. Image-contract identity and adversary pairing are frame-local, so the
API could not express its own valid multi-frame operation.

## General mechanism

Each proposed check may now carry its own `after_handle`. A batch-level handle remains a
shorthand for single-frame batches or rows that intentionally share one frame; a per-check
handle overrides it. Candidate role, immutable path/hash, frame, settings, run, unit, and
parent-chain provenance are still validated independently for every row. Missing or invalid
handles reject only their row so another correctly bound payment in the same batch can land.

## Rejected patch-level alternatives

Teaching the model to make one call per frame wastes turns and leaves the advertised batch
shape misleading. Inferring the candidate by frame from “the latest render” introduces
arrival-order authority and ambiguity when a frame is rendered more than once. Raw paths
would bypass ADR-0008 provenance.

## Validation

Unit tests pin per-check override, batch fallback, and fail-closed absence. Existing payment
provenance tests continue to pin immutable current-run artifacts and matching frame/settings.
The producing-path rerun must show a multi-frame proposal can route each row to its own handle.

## Release and rollback

No persisted schema migration. This is an additive live-tool input; runtime payment rows retain
the existing ADR-0008 schema.
