---
id: HIR-0058
title: Candidate probes must enforce artifact scope before returning evidence
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: false_positive_repair_readback
mechanism: probe_time_scope_validation
adr: null
---

# Candidate probes must enforce artifact scope before returning evidence

## Observed failure

Canonical repair for `material_energy_language` tried to satisfy a future detail visibility row
by creating two objects with undeclared `lookdev.detail.*` roles. `probe_candidate` rebuilt the
script and returned passing scalar evidence, so the repair model described the edit as complete.
Only the subsequent canonical verifier rejected the objects with the exact scoped-artifact error.
The failed attempt consumed roughly 250 seconds and a second repair session.

## Root cause

The disposable candidate probe and canonical verifier rebuilt the same artifact but enforced
different authority. Canonical verification compared the pre-script and post-script object-role
manifests; the probe did not. The read-back loop could therefore appear green for an edit which
the authoritative boundary was guaranteed to reject.

## General mechanism

`probe_candidate` now captures the object-role manifest after prior scripts, rebuilds the current
artifact, and applies the same scoped new-object validator as canonical verification before any
evidence is returned. A scoped artifact that creates an untagged or undeclared-role object makes
the probe fail with the named object, actual role, and allowed role set. Unrestricted units retain
their existing behavior.

## Rejected patch-level alternatives

Prompting repair to remember its role list duplicates mechanical authority in prose. Waiting for
canonical verification preserves correctness but wastes a full model turn on a deterministically
illegal candidate. Filtering the illegal objects out of evidence would hide the authored defect.

## Validation

A tracked test proves the candidate scope helper rejects a fabricated detail role under a
material-only scope and remains inert for an unrestricted unit. Builder instrument and visibility
suites remain green.

## Release and rollback

No persisted schema changes. Rollback removes the early rejection but restores false-positive
candidate readback; canonical verification would still fail closed later.
