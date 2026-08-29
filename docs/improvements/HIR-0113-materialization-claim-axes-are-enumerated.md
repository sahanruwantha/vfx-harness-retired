---
id: HIR-0113
title: Materialization claim axes are enumerated
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: materializer_guesses_owned_axis_ids
mechanism: active_layer_axis_enum_in_unit_ticket
adr: null
---

# Materialization claim axes are enumerated

## Observed failure

Layer 2 rematerialization `20260829T155050Z-40c34a` authored claims on
`mechanism_structure`, then patched them to the still-invalid
`iris_ingress_sequence.mechanism_structure`, before learning that the only owned axis was
the exact token `iris_ingress_sequence`. Retry `20260829T160208Z-154618` repeated the same
guess chain with `mechanism_topology` and `iris.mechanism_topology`. Both 24-turn sessions
exhausted without publication.

Across the two producing runs, four model writes were rejected solely because a closed,
one-element option space was exposed as free text. The deterministic gate named the error,
but only after the model had already generated a complete unit or pointer patch.

## Root cause

HIR-0097 made the WorkUnit ticket schema closed for field names, temporal modes,
publish/consume rows, and composition context, but left `claim.axis` as any non-empty
string. The active materialization candidate already contains `layer.owns`; the staging
tool discarded that authority while constructing its input schema. Error-driven recall was
therefore the only way to discover exact axis identity.

## General mechanism

`work_unit_authoring_schema` accepts an optional set of legal axis ids and places their
sorted values directly in the `claim.axis` enum. When materialization tools are bound, the
harness reads the revision-pinned seeded candidate once and derives the enum from that
active layer's `owns` list. A malformed candidate yields an empty enum and remains
unwritable until the existing transaction boundary reports the malformed authority; it
never falls back to free text. Non-materialization callers retain the generic schema.

This is an authoring instrument, not a second authority source. `WorkUnit.parse`, local
staging validation, revision-checked patches, and final materialization validation remain
authoritative.

## Validation

A heterogeneous ticket fixture accepts the exact enumerated axis and rejects a plausible
prefixed invention while naming the legal value in JSON Schema feedback. Materialization
tool construction passes the candidate-derived enum into the same domain schema. Relevant
materialization, plan-record, image-vocabulary, and WorkUnit suites pass, followed by the
complete repository suite.

- Focused suites: `131 passed in 6.43s`.
- Full suite: `580 passed in 43.19s`.
- `.venv/bin/python -m ruff check src tests`: clean.
- `.venv/bin/vfx --help`: exit 0.

Implementation commit: `a07489e` (`Enumerate materialization claim axes`).

## Release and rollback

No persisted schema or digest change. The materialization MCP input is stricter and is
derived from already-selected candidate authority. Rollback would restore repeated guessing
of a knowable finite option set and is unsafe.
