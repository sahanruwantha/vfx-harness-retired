---
id: HIR-0154
title: Layer-local replan requires external fault-owner amendment
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: unchanged_local_replan_repeats_external_authority_conflict
mechanism: external_fault_owner_digest_gate
adr: ADR-0006
---

# Layer-local replan requires external fault-owner amendment

## Observed failure

Layer 2 run `20260830T115340Z-a73924` proved `building_miniature_start` jointly
unsatisfiable with the local f39 massing band under the sealed Layer 1 lens schedule.
Its typed finding named `camera_path` in `fault_owner_units`, deliberately outside the
same-layer affected set under HIR-0127. `vfx units replan --layer 2 --falsification ...`
nevertheless accepted an unchanged selected bundle and reopened the identical Layer 2
DAG. Run `20260830T125310Z-9c3346` then reproduced the same proof, costing another
builder session while having no authority to mutate the named owner.

Layer 3 was never opened.

## Root cause

HIR-0049 correctly permits a typed finding to reopen an unchanged same-layer DAG: the
finding itself may retire a falsified hypothesis. That rule did not distinguish findings
whose legal repair owner is outside the requested layer. HIR-0127 kept the earlier owner
out of the local invalidation closure, but the public replan command never verified that
selected authority had changed that external owner before consuming the finding.

## Decision

- Before consuming a falsification with `fault_owner_units` outside the requested layer,
  public replan resolves those units in both the explicit base authority and current
  selected authority.
- Every external owner must be resolvable on both sides and have a changed exact unit
  digest. Unchanged or unresolved owners reject the transaction before state mutation.
- The rejection names unchanged and unresolved owners and teaches the legal next action:
  publish amended authority for those owners, then consume the finding.
- HIR-0049 remains unchanged for same-layer findings and findings without external fault
  owners; identical DAG bytes may still be reopened when consuming the finding is itself
  the complete legal amendment.

## General mechanism

`_unchanged_external_fault_owners` indexes exact work-unit identities across every layer
in the base and selected views. `_replan` invokes it after falsification identity and hard-
constraint checks but before computing or applying local effects. Unit display names,
roles, and bundle proximity do not substitute for digest change.

## Rejected alternatives

- Remember the failed run in the prompt: unchanged authority would remain runnable by any
  operator or future agent.
- Reject every same-digest falsification replan: that regresses HIR-0049 and strands valid
  same-layer hypothesis changes.
- Add the earlier owner to the local affected set: a Layer 2 transaction has no authority
  to invalidate or mutate a Layer 1 unit.

## Validation

The unit-admin regression creates a Layer 1 mass finding naming an unchanged external
`camera_path`. Even though the local mass unit changes, public replan refuses the
transaction, names `unchanged=camera_path`, and instructs the caller not to rerun the
identical local DAG. Existing HIR-0049 fixtures continue to prove same-layer same-digest
reopen behavior.

## Release and rollback

No persisted schema change. Rollback permits deterministic repeated spend against an
authority conflict the harness has already assigned outside the requested layer.
