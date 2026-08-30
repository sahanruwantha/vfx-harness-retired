---
id: HIR-0125
title: Threshold operators name their fields
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: contract_threshold_field_discovered_by_guess
mechanism: registry_exposed_operator_field_shapes
adr: null
---

# Threshold operators name their fields

## Observed failure

Room 1046 Layer 3 materialization run `20260829T233408Z-acb609` first staged
`hw-opening-exists` with `op: "eq"` but no numeric target. At 378.6 seconds the
validator returned only `eq threshold must be numeric`. The next model call inferred
an `eq` property and was rejected at 399.1 seconds because `eq` was not an accepted
contract key. Only that second failure's generic accepted-key list exposed `value`.

The planner had already called `evidence_vocabulary`; that card listed evidence kinds
but no shared operator-to-threshold mapping. One deterministic field shape therefore
cost two staging turns and an invented key. Under the live-run rubric this is a class-2
instrument and class-5 contract-feedback defect.

## Root cause

Operator semantics lived only inside `validate_row` as positional implementation logic.
The vocabulary tool exposed kind-specific fields but omitted the four universal
operator shapes. The validator caught all missing, non-numeric, and malformed targets
through one broad exception and returned only the operator name, not the required JSON
field.

## Decision criteria

- The evidence vocabulary enumerates every supported operator and its exact required
  and optional threshold fields.
- `eq` requires numeric `value`, accepts optional numeric `tol`, and explicitly states
  that no `eq` field exists.
- `min` requires numeric `lo`; `max` requires numeric `hi`; `band` requires both.
- Boolean values do not pass as numbers.
- The first validation failure names the exact field, without requiring a second
  unknown-key round.
- Operator definitions have one shared source consumed by the planning instrument and
  pinned by tests.

## General mechanism

`scene_checks.OPERATOR_FIELDS` is the canonical authoring card for the four operators.
`evidence_vocabulary` publishes it beside the kind registry. `validate_row` now performs
explicit per-operator field checks and produces field-addressable teaching messages
instead of collapsing all errors into `threshold must be numeric`.

## Rejected patch-level alternatives

- Special-casing `object_count` would leave every other evidence kind with the same
  operator ambiguity.
- Adding `eq` as an alias for `value` would silently expand the strict schema and create
  two representations of one target.
- Prompt wording would not repair non-model callers or the validator's uninformative
  response.
- A silent default such as `value: 1` would invent authored intent.

## Validation

`test_operator_vocabulary_names_exact_threshold_fields` pins the shared operator card.
`test_threshold_errors_name_the_exact_required_field` covers missing equality value,
non-numeric tolerance, and the `lo`/`hi` shapes of min, max, and band.

The focused vocabulary, plan-record, plan-session, and proxy-evidence suites passed 134
tests. The full repository suite passed 598 tests in 46.78 seconds before the producing
rerun. Ruff and `.venv/bin/vfx --help` passed.

Producing rematerialization run `20260830T001701Z-5e10db` called the expanded
`evidence_vocabulary` before authoring. Its replacement later published the equality
row in one valid shape: `op: "eq", "value": 1`; neither the generic numeric error nor
the invented `eq` key recurred. The transaction later exhausted 24 turns after 1,630.3
seconds and $4.5733 on separate visibility, optical-signal, and evidence-domain plan
findings. Because rematerialization is atomic, the prior selected view and accepted
tunnel checkpoint remained live. This record proves the reproduced HIR-0125 boundary;
it does not claim the replacement plan published.

Implementation commit: `dbbf450` (`fix: expose exact contract threshold fields`).

## Release and rollback

No persisted schema change. Existing valid contracts retain their meaning. Invalid rows
fail earlier with more specific feedback. Rollback would remove the only pre-authoring
operator card and restore deterministic field guessing.

## Remaining limitations

This mechanism states field shape, not whether a threshold is calibrated, non-vacuous,
or supported by the bound proposition. Existing measurement, vacuity, domain, and plan
gates remain authoritative.
