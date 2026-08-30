---
id: HIR-0141
title: Materialization has one terminal operation
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: clean_candidate_revision_can_finish_without_publication_attestation
mechanism: atomic_validate_gate_attest_tool
adr: ADR-0006
---

# Materialization has one terminal operation

## Observed failure

Room 1046 Layer 2 rematerialization run `20260830T080057Z-aa60bf` produced two
independent candidates whose deterministic `gate_preview` was CLEAN. In both sessions,
the agent had called `finalize_materialization` before its final patches. Those patches
correctly invalidated the earlier revision attestation. The agent then called the
separately advertised terminal preview, saw CLEAN, declared completion, and ended without
calling `finalize_materialization` again.

The outer transaction correctly refused to publish unattested bytes. Its retry repeated
the same legal tool sequence, spent another model session, and ended
`session_stalled` with a gate-clean unpublished candidate.

## Root cause

Materialization exposed two terminal-looking operations with different state effects:

1. `finalize_materialization` ran collectable local validation and attested that revision;
2. `gate_preview` staged the post-publication consumer view and ran the real deterministic
   gate, but was read-only.

A patch between them invalidated the attestation, while the final CLEAN response strongly
signaled completion. Prompt wording could ask for another finalize call, but the invalid
sequence remained representable and already recurred across fresh sessions.

## Decision

- `finalize_materialization` is the sole terminal materialization operation.
- It first collects local materialization findings. If none remain, it stages the exact
  post-publication consumer view, including HIR-0140's projected unit state, and runs the
  deterministic plan gate.
- It writes a revision-bound finalization attestation only when that gate is clean.
- Gate findings return through the same tool as repairable terminal errors. Any subsequent
  candidate patch invalidates the attestation, so the agent must call the same terminal
  operation again.
- `gate_preview` remains available for global and unit-plan authoring, but is not registered
  in a materialization session. There is no second clean-looking exit.
- The outer transaction continues to require the exact attestation before publication.

## General mechanism

`finalize_materialization_candidate` composes and validates the candidate, stages it into
the run-scoped consumer view, applies the preview-local state-backed replan, runs
`plan_gate`, and attests the exact candidate hash only on CLEAN. The materialization tool
wraps collectable JSON-pointer validation around that terminal operation so local schema
defects remain batch-repairable.

The planner's available-tool set contains `finalize_materialization` but not
`gate_preview`. This is capability policy, not a longer prompt: the invalid terminal
sequence is unavailable.

## Rejected alternatives

- Tell the agent to remember a second finalize call: both paid sessions followed the
  existing terminal wording and failed identically; this is mechanical sequencing.
- Let a clean preview attest implicitly: a tool documented as read-only must not silently
  mutate authority.
- Let the outer runner attest after the model exits: that bypasses the explicit terminal
  transaction and could accept a candidate whose final gate the agent never invoked.
- Accept any earlier attestation after patches: that divorces acceptance from candidate
  identity and violates HIR-0108.

## Validation

The lifecycle fixture now calls the singular terminal operation on a replacement candidate
whose unit ids differ from selected durable state. It asserts the terminal gate is clean,
the exact candidate revision is attested, and selected predecessor state remains passed.
Tool-policy tests assert the materializer does not expose `gate_preview`.

Production validation is the same explicitly bounded Layer 2 rematerialization. One
successful finalization call must publish the replacement view and transactionally reconcile
unit state without a retry or any Layer 3 activity.

## Release and rollback

No persisted schema changes. Rollback reintroduces two terminal-looking operations and is
unsafe because a clean materialization can again be structurally unpublishable.
