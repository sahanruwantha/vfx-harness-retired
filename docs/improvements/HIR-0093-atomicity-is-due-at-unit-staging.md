---
id: HIR-0093
title: Atomicity is due when a unit enters staged scratch
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: late_atomicity_rejection_requires_cross_unit_rewrite
mechanism: local_atomicity_gate_before_stage_commit
adr: ADR-0006
---

# Atomicity is due when a unit enters staged scratch

## Observed failure

Incremental replacement run `20260829T060456Z-4cdf14` successfully staged a fixed
`camera_window_target`, then staged `camera_rig_path` with both camera-data and keyframe
instrument families. Only `finalize_materialization` rejected the mixed write clusters.
Repair appended a third unit and began moving large claim/contract sets with JSON
pointers, then produced no event for more than six minutes. Incremental authoring had
accepted an invalid atom and forced a monolithic repair afterward.

The same run followed two inaccurate bits of agent-facing schema guidance: the kickoff
example used invalid `temporal_evidence: static`, and the evidence card did not say that
vector object-property components use numeric paths such as `location.2`.

## Root cause

`stage_materialization_unit` parsed the WorkUnit and individual contract rows but did not
run the derived write-cluster gate. Atomicity was treated as a layer-final property even
though it is intrinsically a unit-local publication invariant. The earliest boundary
that could prevent the defect allowed it through.

## General mechanism

Before any staged bytes change, the staging transaction now evaluates atomicity over the
already-staged prefix plus the proposed unit and contracts. Any mixed cluster, unresolved
family, illegal interface, padding field, or other atomicity gap refuses the call and
leaves the candidate byte-identical. The model must split the unit while its active
context is still that unit, not after the full layer has been assembled.

The generic kickoff example now uses the valid `none` temporal value, and the vocabulary
card names numeric Blender component paths. A pathless `materialization_status` tool
returns only staged ids and counts, removing the reason to Read raw scratch JSON.

## Validation

An injected camera-plus-keyframe fixture is rejected with `mixed_clusters` before the
seeded candidate changes. Existing single-cluster incremental publication remains valid.
Tool-registration fixtures prove the compact status surface is materialization-only.
The full repository suite passes 533 tests. Production acceptance is a clean
rematerialization where mixed units are split before
staging, not repaired after finalization.

## Release and rollback

Published schemas are unchanged. This tightens only unpublished staging order and fixes
agent-facing schema facts. Rollback would re-admit oversized scratch units and restore
late cross-unit surgery, so it is unsafe.
