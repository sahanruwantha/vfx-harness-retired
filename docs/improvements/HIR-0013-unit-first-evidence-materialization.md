---
id: HIR-0013
title: Move first-unit evidence design across the candidate boundary
status: proposed
introduced_in: unreleased
date: 2026-08-23
failure_class: first_layer_preproduction_before_any_authoritative_mutation
mechanism: root_layer_jit_and_phase_routed_evidence_tools
adr: ADR-0006
---

# Move first-unit evidence design across the candidate boundary

## Observation

Run `20260823T065933Z-844d62` validated ADR-0005's sparse whole-shot boundary but then spent
eleven minutes inside first-layer preproduction: nine recipe searches, three image-check batches,
three exploratory checks, eighteen rejected candidates, and no plan artifact. The only available
"adversaries" were sibling approval stills. The run was stopped and recorded as a generic
`failed`, exit 1, empty detail, making interruption indistinguishable from a crash.

## Root cause

The global contract still treated the first layer as special executable authority and exposed
image calibration before any candidate existed. Attempt caps bound the cost but do not move proof
to the side of the execution boundary where it can exist. Terminal observability also collapsed
distinct causes because the invocation wrapper serialized `str(KeyboardInterrupt())`.

## Mechanism

1. Permit dependency-root `jit_deferred` layers with empty upstream dependencies/outcomes.
2. Require global publication to contain no ready stages, checks, scene contracts, acceptance
   fingerprints, recipes, or spikes unless a separately recorded durable global exception needs
   executable adoption.
3. Remove `measure_ref`, `measure_check(s)`, recipes, and spikes from global draft/verify/repair
   capabilities. JIT materialization receives only due references and mechanism tools; candidate
   image checks remain a build/evidence responsibility.
4. Preserve fixed pre-mutation propositions and scope. Materialization must still fail closed on
   owned-requirement closure and required producing evidence where that evidence is executable
   without a candidate.
5. Publish typed terminal causes (`interrupted`, `max_turns_exhausted`, `gate_stalled`,
   `plan_budget_exhausted`, `usage_limit`, `process_error`) in status and summary.
6. Reduce global draft/verify ceilings to 12/6 turns. Layer materialization keeps its own
   budget because it is the first phase allowed to make implementation decisions.

## Acceptance

- A global bundle with all layers deferred and one dependency root passes the deterministic gate.
- A root layer materializes without a fabricated upstream outcome.
- Global planner roles cannot call image calibration, recipes, or spikes.
- A heterogeneous static fixture and a camera-driven fixture use the same contracts without
  subject vocabulary in core code.
- Interrupted runs are distinguishable from process failures in `summary.json`.
- The current shot reaches first authoritative scene evidence faster than the stopped-run
  baseline; do not mark this HIR accepted from unit tests alone.
