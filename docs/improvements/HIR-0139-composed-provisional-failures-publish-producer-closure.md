---
id: HIR-0139
title: Composed provisional failures publish a producer closure
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: composed_qualified_failure_has_no_consumable_replan_authority
mechanism: qualification_aware_reconciliation_and_role_derived_falsification
adr: ADR-0006
---

# Composed provisional failures publish a producer closure

## Observed failure

Room 1046 Layer 2 run `20260830T070948Z-23cd41` replayed all three units from an
empty scene, then the HIR-0137 composed audit scored silhouette character 2 and ground
site scale 3. The critic supplied a concrete observation: the oversized cap and pointed
cones read as a squat fortified keep, while the reference has a flush horizontal parapet
with low pavilions and a free-standing PRESIDENT sign.

The critic cited the exact synthetic binding
`requirement:R51:building_silhouette_character`. Reconciliation treated that binding as
unknown scene evidence, downgraded the observation to `protocol_error`, and logged a
judge conflict. The provisional wrapper still returned `contract_gap`, but replaced the
concrete defect with a generic summary absent from `observation_reconciliation`.
Consequently no `state/contract-gaps.jsonl` row or hypothesis-falsification artifact was
published. Every unit remained passed and `vfx units replan --falsification` had nothing
legal to consume.

## Root cause

A qualification binding proves that independent qualitative judgment may block; it is
not a scene check with a PASS/FAIL row. Reconciliation nevertheless required every cited
`check_id` to exist in scene/image evidence before considering qualified authority.

The composed boundary also had no identity of its own and the ordinary contract-gap path
expects one active, unaccepted unit. There was no mechanism to bind post-acceptance
composition evidence to exact constituent unit identity while preserving accepted
checkpoints until the replan transaction.

## Decision

- A critic may cite the exact qualification id bound to its qualified qualitative claim.
  That id is known non-scalar judgment authority; unrelated unknown ids remain protocol
  errors.
- On provisional composed failure, each concrete qualified actionable observation is
  converted to a contract-gap row without losing its axis, property, action, roles,
  claim id, or citations. The synthetic composition grants no mutation authority.
- Contract-gap persistence reads those converted reconciliation rows, pinning candidate
  and settings hashes as before.
- Observation roles map to constituent units through their declared mutation selectors.
  Stable dependency order selects the finding's exact source-unit identity; all matched
  producers and their downstream closure become `affected`.
- A post-acceptance finding leaves source and downstream checkpoints `passed` until
  `vfx units replan --falsification` consumes the immutable record. Recording a finding
  is not itself permission to revoke accepted authority.
- The finding carries the exact provisional requirement id and decision strength.

## General mechanism

`reconcile_observation` excludes exact qualified binding ids from the unknown-evidence
set, after the ordinary claim-binding closure check. Scalar scene/image evidence still
retains its normal contradiction and actionable semantics.

`_provisional_composition_contract_gap` preserves qualified critic observations as gap
rows. `_record_composed_contract_gap_falsification` resolves their semantic roles to the
dependency-ordered producer DAG and writes the standard hypothesis-falsification schema.
`record_hypothesis_falsification(..., preserve_accepted_source=True)` journals the finding
on a passed source without changing accepted status; only the audited replan changes
unit state.

## Rejected alternatives

- Keep the critic score only: a number does not teach the planner what authority failed.
- Accept every synthetic binding as scene evidence: qualification is judgment authority,
  not a fabricated PASS fact.
- Retry one unit directly: all units passed their published contracts, and retry cannot
  amend the claim/contract graph.
- Mark all accepted units failed immediately: that bypasses transactional invalidation
  and discards checkpoints before a reviewed replan.
- Give the synthetic composition unit broad mutation scope: it has no identity-derived
  replay file and would violate bounded ownership.

## Validation

Contract tests prove an exact cited qualification binding reaches actionable qualitative
authority while unrelated ids still fail protocol. Builder tests prove the concrete
observation is retained as a persisted contract gap. Unit-state tests prove a composed
finding is hash-pinned while source and downstream accepted checkpoints remain passed.

Production validation requires rerunning only Layer 2 composition, observing the concrete
R51 gap and typed producer-closure artifact, then consuming that artifact with the public
replan command.

## Release and rollback

The hypothesis-falsification schema is unchanged. A new optional recording mode applies
only when the source is already `passed`; misuse on any other state fails closed. Rollback
restores the score-only dead end and is unsafe for provisional composed judgments.
