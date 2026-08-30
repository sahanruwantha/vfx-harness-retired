---
id: HIR-0129
title: Deferred contracts are context, not unit evidence
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: future_active_contract_seals_authoring_unit
mechanism: deferred_claim_binding_gate
adr: ADR-0004
---

# Deferred contracts are context, not unit evidence

## Observed failure

Layer-1 rematerialization run `20260830T033856Z-ad3d67` correctly avoided proxy
geometry after HIR-0128, but authored `bldg_bbox_height_frame1` and
`bldg_bbox_height_frame114` as required `camera_path_unit` claim evidence. Both rows
were owned by Layer 1, persistent, and activated at Layer 2 over `building.mass`.
Materialization finalization attested revision `d8fc391b...`. The terminal gate then
rejected the same bbox rows for selecting roles outside camera mutation authority.

The agent spent the rest of its session trying incompatible repairs: smuggling the
building selector into camera controls, removing the bbox rows, and finally recording
false vocabulary gap `VG-004` even though `bbox_height` was already the correct metric.
The final gate remained unclean and the operator interrupted before the automatic retry
could spend another model session. The selected JIT view did not change.

## Root cause

HIR-0127 requires a camera author to bind future subject bbox ids through
`composition_context` and not seal those rows. Claim closure already counts context ids,
and builder due logic already omits inactive contracts. The incremental staging and
materialization finalization gates did not enforce the difference between claim evidence
and context binding. A future-active row could therefore masquerade as evidence produced
by the current unit until the terminal role-selector gate noticed that its subject was
outside mutation authority.

## Decision criteria

- A scene contract inactive at its owner unit cannot certify a required claim there.
- The row remains owned by the author and participates in claim closure.
- The rejection names owner, activation layer, contract id, and the legal
  `composition_context.contract_ids` action before candidate bytes change.
- Materialization finalization and the terminal plan gate enforce the same predicate.
- The rule is generic across contract kinds and roles; no shot, frame, or bbox name is
  hard-coded.

## General mechanism

A scene-contract binding whose `activates_at` differs from `owner_layer` is refused in
unit `claim.evidence`. Incremental staging performs this check inside the locked
pre-write transaction. Materialization finalization reports every offending unit, claim,
contract, owner, and activation layer. The terminal plan gate emits the same
`unit-evidence-due` blocker and suppresses downstream mutation-selector advice for that
binding.

The legal form keeps the persistent contract out of claim evidence and lists its id in
the author's `evaluation.composition_context.contract_ids`. The later mutating layer
evaluates and freeze-protects it when active.

## Rejected alternatives

- Expanding camera mutation authority to downstream subject roles: that violates sparse
  ownership and recreates the proxy path HIR-0128 closed.
- Treating the row as a vocabulary gap or approved-start decision: the metric exists;
  only its due boundary was represented incorrectly.
- Letting the terminal gate teach after finalization: the failed run shows that the late,
  generic selector message causes destructive plan repairs and repeated model spend.
- Special-casing bbox ids or Layer 1: any future-active contract has the same temporal
  impossibility.

## Validation

The regression proves a direct future-active claim binding is refused before write, the
same row is accepted through composition context, finalization reports a tampered direct
binding, and the terminal gate names `unit-evidence-due` without asking the author to
mutate the future subject. Focused materialization, coherence, work-unit, plan-tool, and
atomicity suites passed 203 tests. Full repository validation passed:

```text
.venv/bin/ruff check src tests
All checks passed!
.venv/bin/python -m pytest -q
623 passed in 47.06s
```

## Release and rollback

This strictly tightens unpublished materialization and selected-plan validation without a
schema change. Existing selected views with direct future-active claim bindings must be
rematerialized. Rollback would restore contradictory finalizer/gate decisions.

## Remaining limitations

This mechanism does not choose the downstream subject role or bbox thresholds. Those stay
JIT design decisions constrained by sparse reserved roles, authored references, and
cumulative replay.
