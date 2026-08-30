---
id: HIR-0143
title: Extra-frame debts survive runtime evidence scoping
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: activation_evidence_erased_by_judge_frame_filter
mechanism: contract_declared_frame_evidence_schedule
adr: ADR-0006
---

# Extra-frame debts survive runtime evidence scoping

## Observed failure

Room 1046 Layer 2 run `20260830T083943Z-206dc6` replayed four geometry units
from an empty scene and published after its f39 scene checks passed. The selected camera
authority also contained three persistent, camera-owned `building` bbox rows activated by
Layer 2 at f1, f114, and f176. None appeared in the build transcript or canonical report.

The payer selection was correct: the dependency-complete roof unit was responsible for the
parent `building` selector. The live build compiler added all three ids, then filtered the
whole active evidence set through Layer 2's sole judge frame, f39. Canonical replay and the
zero-cost revalidation path independently asked for geometry protection only at the current
judge frame. All three paths erased the same legal extra-frame debt.

## Root cause

Runtime treated judge frames as an evidence execution schedule. HIR-0029 and HIR-0130 make
the opposite distinction: judge frames select authored qualitative moments, while a bound
scene contract executes at its own declared `frame` or `frames`. A downstream activation
layer must pay an owner-authorized moment without adopting that moment into its judge list.

## Decision

- Every bound static scene contract is scheduled at its own declared `frame` or `frames`.
- A contract without a declared frame uses the active judge frame as its fallback.
- Lifecycle activation is tested at those evidence frames. A later-activating row remains
  inert; a future-active row due now cannot disappear because its frame differs from the
  activation layer's judges.
- A dependency-complete geometry payer measures every protected contract on that schedule.
  Those ids are required canonical evidence and are reported with `evidence_frame`.
- Extra evidence frames never mutate layer judges, unit judges, claim moments, or critic
  scope.
- Live mutation read-back, empty-scene canonical replay, and deterministic revalidation use
  the same schedule and fail closed on missing or failing readings.

## General mechanism

`_scene_ids_active_at_declared_frames` compiles the due boundary from selected contracts.
`_geometry_protected_evidence` evaluates protected rows at their declared moments and returns
both the required ids and readings. `_render_evidence` adds those readings before executable
judgment, while the phase card retains the same ids for automatic post-mutation probes.
The Blender tool scheduler now expands both scalar `frame` and temporal `frames` rows.

This is contract-driven and shot-independent. It introduces no camera authority, role-name
heuristic, fixed frame, or extra mutation permission.

## Rejected alternatives

- Add f1/f114/f176 to Layer 2's judges: that rewrites sparse authored authority and violates
  HIR-0130.
- Bind the rows to the f39 claim: a contract's measurement moment is not changed by where its
  id is carried.
- Trust the live tool's earlier green state: acceptance is empty-scene replay, so canonical
  replay must freshly measure the same debts.
- Special-case bbox or `building`: visible-fraction and other static protected contracts have
  the same scheduling requirement.

## Validation

Regression tests prove disjoint owner frames remain due on an activation layer with only an
f39 judge, later activation remains inert, canonical readings are requested at f1 and f114
without changing the judge tuple, and temporal static rows expand every declared frame.

Production validation is a bounded Layer 2 build. Its empty-scene proof must name and pass the
selected f1/f114/f176 bbox ids; absence is a failure, not a vacuous pass.

## Release and rollback

No persisted schema changes. Rollback restores silent publication without paying selected
camera-owned interfaces and is unsafe.
