---
id: ADR-0006
title: Unit-first planning with candidate-bound evidence materialization
status: proposed
date: 2026-08-23
supersedes: ADR-0005
---

# Unit-first planning with candidate-bound evidence materialization

## Context

ADR-0005 removed whole-shot evidence design from global planning, but retained one exception:
the first layer remained globally executable. Run `20260823T065933Z-844d62` proved that this
merely moved preproduction inward. The planner spent eleven minutes researching and calibrating
eighteen image checks for the first layer before writing a DAG or producing a scene. Every check
was rejected because an authored-input-only workspace has no genuine failing candidate; sibling
approval references are different valid states, not adversaries.

The pipeline north star scales through the smallest dependency-ready work unit. A department
layer is an ownership boundary, not permission to predesign its units or evidence globally.

## Decision

Global publication contains only the layer DAG, ownership register, durable constraints,
reserved interfaces, and genuine blockers. Every layer, including a dependency-root layer, is
`jit_deferred`. A root layer has empty `depends_on_layers`, empty `required_outcomes`, and may
materialize immediately after global publication.

At materialization, the next layer creates a bounded work-unit DAG with fixed propositions,
mutation scope, protected authority, relevant moments, and executable scene evidence needed for
safe mutation. Plan-time image-check calibration is not part of global or materialization tool
authority. Candidate-sensitive image checks are proposed after a real mutation exists and are
accepted only against the frozen candidate and a genuine pre-mutation or known-failing artifact.

Tool capability follows phase rather than shot vocabulary:

- global: read, register, patch, gate, and escalate;
- materialize/unit plan: due references, selective recipes or mechanism spikes, scene contracts;
- build/evidence: candidate-bound image checks and renders;
- repair: only implicated controls and evidence.

## Consequences

Global planning can publish without acceptance fingerprints, executable stages, recipes, or
checks. The first production action after publication is JIT materialization of a root layer,
followed by its smallest ready unit. Exact propositions remain fixed before mutation; only the
measurement binding waits for a real candidate. Existing immutable bundles remain readable.

## Validation

HIR-0013 requires heterogeneous root-layer fixtures, rejection of global executable detail,
candidate-only image calibration, typed terminal causes, and improved latency to first
authoritative scene evidence.
