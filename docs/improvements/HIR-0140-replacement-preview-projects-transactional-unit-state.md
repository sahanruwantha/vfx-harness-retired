---
id: HIR-0140
title: Replacement preview projects transactional unit state
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: unpublished_replacement_cannot_clear_terminal_hierarchy_gate
mechanism: preview_local_state_backed_replan_projection
adr: ADR-0006
---

# Replacement preview projects transactional unit state

## Observed failure

Room 1046 Layer 2 rematerialization run `20260830T074437Z-380e41` staged and locally
validated a four-unit replacement after a typed composed-reference falsification. Its
terminal `gate_preview` overlaid the replacement documents, then blocked on
`state/work-units/layer_2.json`: the durable state still named the three selected
predecessor unit ids.

That mismatch was required before publication. The live state could not name the
replacement until the replacement published, while publication required the preview to
be clean. The agent had no legal tool action: hand-editing state would bypass
`apply_replan`, and reverting the candidate to the old ids would discard the typed
falsification rather than repair it.

## Root cause

Candidate preview modeled only the post-publication plan documents and synthetic JIT
pointer. It left the consumer view's work-unit state symlinked to selected durable state.
The hierarchy gate therefore compared two different transaction moments: candidate DAG
after publication and unit state before publication.

Suppressing the mismatch would also be wrong. Digest-schema incompatibility, missing
stored hashes, an invalid replacement DAG, or an unprojectable preservation/invalidation
closure are real reasons to refuse publication.

## Decision

- When durable state exists for the candidate layer, candidate preview isolates that one
  state file inside the run-scoped consumer view.
- The preview runs the existing state-backed `apply_replan` mechanism against the isolated
  file and the staged candidate DAG. It does not implement a second diff algorithm.
- The deterministic gate reads the resulting projected identities, hashes, statuses, and
  dependency readiness exactly as it reads selected state.
- Selected durable state and the live JIT pointer remain byte-for-byte unchanged.
- Any error that the real state-backed replan would reject is a candidate-preview error;
  the gate does not ignore or downgrade it.
- A first materialization with no durable state keeps the existing designed behavior:
  pending unit-plan absence is advisory until build kickoff initializes state.

## General mechanism

`stage_candidate_view` already replaces the run-scoped `state/` symlink so it can pin a
synthetic JIT pointer. It now also replaces only `state/work-units/layer_<id>.json` with a
private copy when that layer has durable state. `apply_replan` runs on the preview root
with an empty explicit predecessor DAG and `state_backed_base=True`, making stored
current-schema unit digests the predecessor identity. The candidate `layers.json` hash is
the projected new plan identity.

Other layers' state files remain symlinked read-only into the selected shot. Publication
and the outer rematerialization flow retain sole authority to mutate real durable state.

## Rejected alternatives

- Skip hierarchy state validation during rematerialization: this would publish candidates
  whose digest schema or invalidation closure cannot be applied.
- Reinitialize state from candidate ids: this discards accepted identity and audit history.
- Apply the replan to live state before publication: a failed candidate would supersede
  selected authority.
- Duplicate only the id/hash rewrite in the preview: it would drift from preservation,
  invalidation, and schema rules in `apply_replan`.

## Validation

The composed lifecycle fixture seals a unit, stages a replacement with a new identity over
an unpublished reverted overlay, and runs the full deterministic gate. The preview state
validates against the replacement DAG and marks the replacement pending, while the selected
durable predecessor remains passed.

Production validation is the bounded Layer 2 rematerialization that previously stopped at
the impossible hierarchy finding. Its next terminal preview must either clear or name a
different candidate defect; it must not mutate Layer 1 or open Layer 3.

## Release and rollback

No persisted schema changes. Rollback restores an impossible rematerialization ordering and
is unsafe wherever a candidate changes unit ids or digests.
