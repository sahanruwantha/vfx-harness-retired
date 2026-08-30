---
id: HIR-0130
title: Deferred contracts keep owner frame authority
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: activation_layer_rejects_owner_reference_moment
mechanism: owner_frame_authority_with_extra_frame_payment
adr: ADR-0004
---

# Deferred contracts keep owner frame authority

## Observed failure

After HIR-0129, Layer-1 rematerialization run `20260830T040424Z-bc84ea`
successfully authored deferred subject bbox rows through `composition_context` instead
of camera claim evidence. Materialization finalization passed. The terminal gate rejected
all camera reference moments because the earliest form layer did not judge them: frame 1,
114, and 176 were Layer-1 camera judges, while Layer 2's only judge was frame 39.

The gate told the materializer to add those frames to the activation layer or move the
contracts. Neither action was legal: global judge lists are immutable sparse authority,
and HIR-0127 deliberately places the camera-owned rows on the later form activation
boundary. The agent responded by moving individual contracts among Layers 3, 4, and 5;
the same four blockers remained. The operator interrupted before publication.

## Root cause

The terminal contract gate treated `activates_at` as both execution due boundary and
frame-authority owner. ADR-0004 and the compiled extra-frame rule separate those
concerns: the authoring layer chooses reference moments from its own judge list, while a
later layer evaluates a bound scene contract at an extra frame through
`composition_context.contract_ids`. HIR-0127 added the deferred bbox lifecycle but did
not migrate this older frame-membership predicate.

## Decision criteria

- A future-active contract's frame belongs to its authoring owner layer's judge set.
- The activation layer evaluates that moment without adding it to its own judge list.
- Non-deferred contracts retain the existing activation-layer frame check.
- Invalid deferred frames name the owner and the later activation layer, and explicitly
  forbid mutating the activation layer's judge list.
- The mechanism applies equally to scalar `frame` and temporal `frames` rows.
- No fixed frame, shot name, layer count, or role heuristic enters core code.

## General mechanism

The terminal contract gate derives `frame_authority` as `owner_layer` when a contract is
future-active and as `activates_at` otherwise. Scalar and temporal frame membership use
that same authority. A valid owner moment may therefore execute as extra-frame evidence
at a later activation boundary. A miss reports the owner layer and activation layer and
names the existing extra-frame mechanism.

## Rejected alternatives

- Adding camera frames to every later form layer: global judge lists are authored
  milestones, not an execution-frame catalog.
- Moving each bbox to whichever later layer happens to judge that frame: no such layer
  necessarily exists, and activation is determined by subject availability, not frame
  coincidence.
- Skipping frame validation for deferred rows: the owner still needs explicit authored
  frame authority; arbitrary moments remain invalid.
- Prompting the materializer not to move contracts: the deterministic gate explicitly
  requested that incorrect action.

## Validation

The regression proves a deferred bbox at an owner judge frame passes even when the
activation layer judges a disjoint moment, an unowned frame still fails against the
owner, and non-deferred rows retain activation-layer validation. Focused plan-record,
coherence, work-unit, plan-tool, and atomicity suites passed 204 tests. Full repository
validation passed:

```text
.venv/bin/ruff check src tests
All checks passed!
.venv/bin/python -m pytest -q
624 passed in 42.86s
```

## Release and rollback

This corrects gate semantics without a persisted schema change. Previously rejected
materializations may be retried; no selected authority is rewritten. Rollback would make
HIR-0127's legal deferred composition form unpublishable whenever form and camera layers
have disjoint judge moments.

## Remaining limitations

Frame authority does not prove that the downstream subject exists or that bbox targets
are good. Activation, role ownership, geometry protection, and cumulative replay remain
separate gates.
