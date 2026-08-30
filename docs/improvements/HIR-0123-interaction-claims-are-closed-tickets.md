---
id: HIR-0123
title: Interaction claims are closed tickets
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: interaction_claim_schema_discovered_by_parser_retries
mechanism: kind_conditional_interaction_authoring_contract
adr: null
---

# Interaction claims are closed tickets

## Observed failure

Room 1046 Layer 3 materialization run `20260829T230724Z-aeb87d` authored one
interaction claim through repeated discovery:

1. staging rejected a missing `coordination_owner` at 541.4 seconds;
2. the next call added only that field and rejected missing `controls` at 568.0 seconds;
3. a later finalization rejected semantic role names such as
   `facade.pier`, `facade.window_bay`, and `hero_window.tunnel` as unknown
   participants, without enumerating the legal same-layer unit ids;
4. converting the claim to `atomic` stranded interaction-only fields, consuming
   further patch and unstage turns.

The session exhausted 24 model turns after 1,172.6 seconds and $3.5603. Under the
live-run rubric, the repeated same-family guesses were a class-4 authoring-schema and
feedback defect, not a reason to enlarge the turn budget.

## Root cause

The WorkUnit tool schema listed `coordination_owner`, `participants`, and `controls` as
three unrelated optional properties. Only the Python parser knew their conditional
relationship to `kind`. The parser failed on the first missing field, so it taught one
piece of the closed ticket per turn. Layer parsing later knew the candidate unit-id set
but reported only unknown values, leaving the model to infer whether participants were
roles, controls, or unit ids.

## Decision criteria

- `kind: interaction` requires all of `coordination_owner`, `participants`, and
  `controls` in the tool schema before the staging call executes.
- Participants contain at least two exact same-layer work-unit ids; controls are
  non-empty bounded control ids.
- `kind: atomic` forbids all interaction-only fields, including empty placeholders.
- Parser fallback reports the entire missing coordination shape in one error.
- Unknown participant or coordination-owner feedback enumerates the valid same-layer
  unit ids and explicitly rejects semantic roles and controls as participant identity.
- Existing image-property conditionals continue to compose with the new interaction
  conditional rather than relying on array position.

## General mechanism

`work_unit_authoring_schema` now carries a Draft 2020-12 conditional over claim `kind`.
The interaction branch requires the three fields and their cardinalities; the atomic
branch rejects their presence. Field descriptions state the identity vocabulary.

`Claim.parse` retains the authoritative runtime boundary but accumulates missing
coordination pieces into one complete diagnostic. `load_layers_from_path` supplies the
dynamic same-layer unit-id vocabulary when participant or owner values are unknown.

## Rejected patch-level alternatives

- Adding the three field names to the planner prompt would leave the callable schema
  permissive and repeat the defect for other clients.
- Raising max turns would pay for deterministic schema discovery on every complex
  interaction.
- Treating semantic roles as participants would conflate evidence subjects with
  work-unit repair ownership.
- Silently stripping interaction fields when `kind` changes would mutate authored
  intent instead of requiring an explicit valid ticket.

## Validation

`test_unit_ticket_schema_encodes_complete_interaction_shape` proves the conditional at
the callable JSON-schema boundary. `test_interaction_parser_reports_complete_coordination_shape`
proves one complete fallback error. `test_materialization_interaction_feedback_enumerates_unit_ids`
proves role-like participants receive the exact legal vocabulary. The image-property
schema test now locates its own conditional semantically, proving the two rules compose.

Focused planning and architecture suites passed 151 tests; focused WorkUnit,
materialization, and image-debt suites passed 116 tests. The full repository suite
passed 596 tests in 42.54 seconds. Ruff and `.venv/bin/vfx --help` passed.

Producing run `20260829T233408Z-acb609` had no missing-owner, missing-controls, or
role-participant interaction failures. It published a valid
`hero_to_interior_onset_coordination` interaction unit, passed materialization
validation, received a clean terminal gate, and selected Layer 3 on attempt 2. The
first session used 23 turns, 1,146.0 seconds, and $3.2920; its candidate was already
clean but it omitted terminal attestation. The typed retry reused the locked candidate,
attested it in 8 turns, 427.5 seconds, and $1.1440, and the outer transaction then
published. Reliability improved from terminal max-turn failure to a clean selected
view; this record does not claim lower total latency or cost.

Implementation commit: `8d4fb1f` (`fix: close interaction claim authoring schema`).

## Release and rollback

No persisted schema migration. The change tightens the staging tool contract and
improves fallback diagnostics. Rollback would reopen deterministic field-by-field
schema discovery and ambiguous participant identity.

## Remaining limitations

The schema cannot enumerate future unit ids before a candidate is authored; dynamic
identity validation remains at layer parse/finalization, where the complete live unit
set exists. A model may still choose an interaction where an atomic or split-unit
design is semantically better; atomicity and plan gates remain authoritative.
