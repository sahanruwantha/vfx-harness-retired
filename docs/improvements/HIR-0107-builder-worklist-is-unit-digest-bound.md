---
id: HIR-0107
title: Builder worklists are unit-digest bound
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: superseded_unit_worklist_blocks_new_generation
mechanism: digest_bound_builder_worklist_state
adr: ADR-0002
---

# Builder worklists are unit-digest bound

## Observed failure

Layer 2 build run `20260829T110628Z-5549bf` executed the newly materialized
`iris_blade_mechanism`. Its first evidence verdict included a harness-owned open item,
`blacks-f072 region_ratio fix`, even though that id came from the superseded
`lighting_atmosphere` design and was outside the active unit card. The live worklist
read showed six completed atmosphere-era tickets plus that one old item. Restating the
blade unit's checklist merged the foreign item into the new list, leaving the
authoritative `builder_worklist_open_items` row failing.

## Root cause

Durable worklists were stored as `state/worklists/layer-<id>.json`. Layer identity
survives rematerialization, but unit ids, digests, scopes, contracts, and tickets do not.
The worklist tool deliberately carried unresolved items across attempts, so a correct
same-unit recovery mechanism became cross-generation authority contamination.

The file was also written directly instead of through the repository's atomic JSON
publication boundary.

## Decision criteria

- A worklist is identified by layer id, unit id, and exact `unit_digest`.
- Retries and process restarts of the same digest retain unresolved items.
- A changed digest, replacement unit, or sibling unit starts with an empty worklist.
- Layer-only legacy files are inert; they are never heuristically adopted by a new unit.
- Stored JSON declares a schema and repeats its identities; mismatches and malformed
  arrays fail closed as builder-state evidence.
- The builder tool and the evidence gate resolve the path through one implementation.
- Writes are atomic and remain under durable cross-run `state/`.

## General mechanism

`vfx-harness.builder-worklist/v2` stores each checklist under
`state/worklists/layer-<layer>/<unit>/<unit-digest>.json`. The live tool obtains the
unit id and digest from the same comparison state compiled for the active unit; without
both, worklist mutation is refused. Loading verifies schema and all three identities.
The evidence gate independently derives the same digest from the active `WorkUnit` and
therefore cannot see a sibling or superseded generation's checklist.

Unresolved-item merging remains unchanged inside one exact digest. Publication now uses
the atomic provenance writer.

## Rejected alternatives

- Clearing `layer-2.json` for this shot would destroy audit state and recur on every
  rematerialized layer.
- Keying by unit id alone would still contaminate a same-id replacement whose contract
  or mutation authority changed.
- Allowing the model to mark the foreign item complete would let one unit certify work
  outside its scope.
- Migrating a layer-only checklist to the first new unit would guess ownership that the
  obsolete file does not contain.

## Validation

Unit fixtures place a legacy layer-only open defect beside a new active unit and prove it
is invisible. An open item stored for the exact digest becomes authoritative failing
evidence, while changing only the unit generation digest returns an empty worklist. An
identity mismatch at the exact path produces a failing `builder-worklist-valid` row.

Producing-run evidence and broad regression results will be added after the next Layer 2
build demonstrates that the atmosphere-era item is absent from the active unit.

## Release and rollback

This is a strict state migration by non-adoption: v1 layer-only files remain on disk for
audit but are unsupported execution authority. No selected plan or checkpoint schema
changes. Rollback would let superseded work block or be falsely completed by unrelated
units, so rollback is unsafe.
