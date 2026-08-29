---
id: HIR-0084
title: Successor interfaces are typed references bound to producer digests
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: successor_protocol_guess
mechanism: digest_bound_publish_interface_card
adr: null
---

# Successor interfaces are typed references bound to producer digests

## Observed failure

HIR-0025 compiled helper inventories so builders would stop calling `inspect.getsource`.
HIR-0079 compiled return contracts so they would stop unpacking helper results. Successors
still inferred operational protocol from producer scripts — origin, pivot, instance
handles — the same guess one level up. Compact predecessor cards exposed roles, controls,
and sealed ids without a closed kind registry or a rule that exported values must be
executable references.

## Root cause

There was no canonical publish-interface identity and no schema invariant forbidding
producer prose. Storing a parallel "stale interface" flag would have desynced from
`apply_replan`. A string such as `"origin": "hinge"` is a producer assertion with a schema
stamp.

## Decision criteria

Unknown kinds fail closed (ADR-0003 / look-vector identity). Every export value is a
validated role, control, or bound contract id. Interface validity is derived from durable
`unit_hash` vs producer digest at ready-set and card compile. No new raw Read surface;
the builder reads the compiled kickoff card.

## General mechanism

Interface kinds are `vfx-harness.publish-interface/v1`. Authored `publishes` rows are
parsed onto the `WorkUnit` and participate in `unit_digest` when declared (empty rows
are omitted so schema-4 durable hashes stay comparable). Export values are validated
against the producing unit's legal tokens. When omitted, the harness derives one
interface from the first mutation role/control/dress selector and bound contract ids.
Successors declare `consumes` `{producer, interface_id, kind}`. Predecessor cards and
the live builder card include those interfaces only when the producer is `passed` and
`unit_hash` equals the compiled producer digest. `ready_units` requires digest match
**and** that each consumed id/kind is actually offered. Cross-schema stored hashes
remain incomparable; `apply_replan` is the invalidation closure.

## Rejected patch-level alternatives

Exporting producer prose under a typed schema. A second interface lifecycle document.
Letting successors Read producer scripts when a card is missing an origin.

## Validation

`test_prose_origin_export_is_refused` pins `"origin": "hinge"`.
`test_unknown_publish_kind_is_rejected` pins the registry.
`test_authored_interface_change_invalidates_producer_digest` pins `replan_effects`.
`test_stale_producer_digest_omits_publish_interfaces` and
`test_assembly_is_unready_until_producer_digest_and_interface_match` pin digest-derived
staleness and declared `instance_source` consumption. `test_builder_card_includes_authored_interfaces_digest_and_predecessors`
pins the live card. Predecessor compilation adds `publish_interfaces` without catalogs or
producer scripts.

## Release and rollback

No digest-schema bump: empty `publishes`/`consumes` are omitted from `unit_digest` so
Layer 2 hashes stay comparable. Declared interface rows participate in producer identity.
Rollback omits typed interfaces from successor cards and would restore script-inspection
pressure.

## Remaining limitations

Derived interfaces export the first role/control plus bound contract ids, not a complete
named slot vocabulary. Authored `publishes` is the way an assembly producer names
`instance_source` slots. Layer 3 is the first assembly-heavy acceptance run.
