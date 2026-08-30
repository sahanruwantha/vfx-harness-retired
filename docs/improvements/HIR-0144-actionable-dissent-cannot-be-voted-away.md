---
id: HIR-0144
title: Actionable dissent cannot be voted away
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: panel_majority_erases_qualified_observation
mechanism: actionable_dissent_preservation
adr: ADR-0006
---

# Actionable dissent cannot be voted away

## Observed failure

Room 1046 Layer 2 composed run `20260830T091721Z-18ee29` correctly restored
independent R51 reference judgment after HIR-0142. The first qualified critic scored the
generic tower 2/4 on silhouette and supplied one exact in-scope repair: add a projecting
corner bay and recessed center plane while preserving the passing width band. Two later
critics returned bare 3/4 passes. The panel took the majority, discarded the dissenting
observation, and published the same flat shaft.

The canonical plate proves the dissent was material: it has no readable corner-section
break. The panel did not contradict the observation with executable evidence or a focused
measurement; it merely outvoted it.

## Root cause

Panel aggregation treated pass/fail votes as stronger authority than typed observation
reconciliation. Critic protocol forbids a passing scorecard from carrying a blocking
observation, so a bare pass contains no proposition capable of refuting an actionable
dissent. The old aggregator nevertheless selected issues only from a judge agreeing with the
majority and silently deleted the losing row.

## Decision

- Score medians remain useful for noise reporting, but votes cannot erase an exact qualified
  actionable observation.
- An executable contradiction, protocol error, contract gap, or already-declared
  `judge_conflict` is not actionable dissent and retains ordinary panel behavior.
- When a nominal passing majority contains uncontradicted actionable dissent, the aggregate
  verdict is `REVISE`, retains the dissent's typed observations and issues, and records
  `decided_by: actionable_panel_dissent`.
- For a composed provisional requirement, the existing HIR-0139 path converts that retained
  observation into a typed producer-closure finding. It does not grant cross-unit mutation.
- No prompt, threshold, model identity, shot role, or fixed frame participates in the rule.

## General mechanism

`_aggregate_critic_panel` centralizes aggregation. It first computes the ordinary majority
and median, then detects a dissenting failure that survived typed reconciliation with legal
issues and without conflict/gap/protocol flags. Such a row becomes the aggregate authority.
The panel record remains intact for audit, including every raw vote and mean.

## Rejected alternatives

- Raise the pass threshold: a score-only tolerance does not preserve the concrete defect.
- Ask a fourth critic: another bare vote still cannot refute the proposition.
- Always let one failure veto: contradicted or malformed observations do not earn authority.
- Put the dissent into the next builder prompt after passing: accepted state must not carry an
  unresolved blocking defect.

## Validation

Contract tests reproduce the exact `[REVISE, PASS, PASS]` shape and prove the actionable row,
repair action, and reconciliation survive as a failure. A contradicted dissent with no legal
issue still permits the passing majority.

Production validation is the same bounded Layer 2 composed replay. The existing corner-section
dissent must publish a typed R51 finding rather than seal the plate.

## Release and rollback

No persisted schema changes. Rollback allows model votes to replace earned observation
authority and is unsafe.
