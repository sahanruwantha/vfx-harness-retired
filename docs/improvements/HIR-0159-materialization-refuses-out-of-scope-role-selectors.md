---
id: HIR-0159
title: Materialization refuses out-of-scope role selectors
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: validator_clean_then_gate_role_selector
mechanism: in_session_role_selector_closure
adr: null
---

# Materialization refuses out-of-scope role selectors

## Observed failure

Layer 2 materialization run `20260830T153835Z-8b0c6b` staged `hero_composition_control`
with empty mutation roles and required `bbox_*` / composition claims on `hero.tower`.
`validate_materialization` passed. `finalize_materialization` then ran the
deterministic gate and returned five blocking `role-selector-closure` findings. The
session spent its last turn on a `control_roles` patch that the clustered mutates
schema also refused, and died `error_max_turns` without publishing.

The same class already moved claim-closure into materialization validation so a
write-hook can teach in-session (run `20260825T015307Z-bc9109`). Role-selector
closure stayed terminal-gate-only.

## Root cause

Required contract role selectors were checked only after attestation. A
mutation-empty observer looked locally valid. Raising the turn cap would spend
the same last-turn on the same gate.

## Decision criteria

- Materialization collectable validation refuses required scene-contract role
  selectors that the binding unit does not mutate or dress, using the same
  selector matcher as the plan gate.
- Deferred rows (`activates_at` ≠ `owner_layer`) stay composition-context debts.
- A camera repair owner may still observe vis / projected origin it does not mutate.
- Two-sided kinds keep `roles` as the mutation-closed side.
- Do not raise turn caps.

## General mechanism

`validate_materialization` notes undeclared role selectors on each required
claim binding. Staging and patch reuse that validator, so the session sees the
finding before finalize.

## Rejected patch-level alternatives

- Retry layer 2 with a higher turn cap: the session would still learn the rule
  after attestation.
- Answer by restaging the observer onto invented `control_roles`: clustered
  mutation scope already forbids mapping roles outside `role_namespace`.

## Validation

A polish-layer fixture that appends a mutation-empty observer binding `bbox_height`
on `polish.comp` fails `validate_materialization` with `outside mutation authority`.
Existing polish payloads with vis in `composition_context` still validate.

## Release and rollback

No schema migration. Rollback would restore validator-clean then gate-block.

## Remaining limitations

Composition-context selectors are still gate-only, matching today's plan-gate
loop. Camera deferred bbox remains `composition_context`, not claim evidence.
