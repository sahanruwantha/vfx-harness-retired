---
id: ADR-0003
title: Make metric identity and temporal evidence explicit in plan contracts
status: accepted
date: 2026-08-21
supersedes: null
---

# Make metric identity and temporal evidence explicit in plan contracts

## Context

[HIR-0006](../improvements/HIR-0006-canonical-reference-fingerprints.md) records two image
implementations assigning different halation values to one still while sharing an informal name.
[HIR-0007](../improvements/HIR-0007-temporal-and-ownership-evidence-coverage.md) records motion
claims bound only to static counts and mutable controls with no declared relationship to roles.
Prose could disambiguate these cases for a human, but executable consumers had to infer identity,
time, and ownership.

## Decision

Plan evidence must carry stable machine identity for the property it proves:

- New reference fingerprints use the versioned `vfx-harness.look-vector/v1` metric set and a
  `values` map keyed by canonical metric id. Producers and consumers call the same registry.
- Temporal scene contracts use semantic roles plus explicit frame windows. The generic kinds are
  `onset_order`, `radial_distance_trend`, `transform_return_delta`, and rendered `frame_delta`.
- A work unit declaring `temporal_evidence: motion` must bind an exact temporal contract id.
- Mutation scopes may declare `control_roles`, mapping every semantic control to roles already in
  that unit's mutable role set. Missing mappings remain visible warnings during migration.

These are additive extensions to acceptance records, scene-check schema 2, and work-unit schema
4. They do not authorize shot-specific metric names, role inference from datablock names, or
model verdicts as substitutes for executable evidence.

## Consequences

- One metric id now selects one algorithm and sampling policy.
- Motion claims cannot close on evidence that says only that objects or animation data exist.
- Holds, ordering, convergence, and return transforms become reusable deterministic primitives.
- Existing prose fingerprints and scopes without mappings remain readable, but new planner output
  uses typed fingerprints and is warned toward explicit control ownership.
- Temporal evaluation costs Blender frame sampling or two bounded renders when activated.

## Rejected alternatives

- Keep parallel metric implementations and widen tolerance: conflicting authority remains and
  genuine deviations become harder to detect.
- Encode the implementation name in prose: every consumer still needs a parser and can silently
  select a different meaning.
- Treat keyframe or object counts as temporal proof: existence does not establish ordering,
  direction, return, or stillness.
- Infer control-to-role relationships from strings: semantic ids are stable selectors but do not
  themselves declare governance.

## Validation and review trigger

Unit tests reproduce typed fingerprints through the canonical registry, reject unknown metric
ids, validate and compile temporal probes, execute frame-delta evidence, and exercise temporal
and ownership coverage. The deterministic gate on the motivating shot identifies five motion
units without temporal evidence and ten camera judge frames without bbox coverage.

Review this decision if Blender evaluation makes temporal sampling nondeterministic, a new metric
set needs incompatible semantics, or heterogeneous fixtures show that one of the generic temporal
kinds cannot be expressed without shot-specific policy.
