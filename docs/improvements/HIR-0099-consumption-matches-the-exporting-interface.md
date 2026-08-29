---
id: HIR-0099
title: Consumption matches the interface that exports the selector
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: consume_matches_producer_union_not_interface
mechanism: interface_local_export_join
adr: null
---

# Consumption matches the interface that exports the selector

## Observed failure

HIR-0096 required a camera owner to consume the target producer, but its predicate
checked two independent facts: the producer exported the measured selector somewhere,
and the successor consumed any offered interface from that producer. With two authored
interfaces, consuming an unrelated one satisfied the gate.

## Root cause

Export discovery was producer-wide while consumption identity was interface-specific.
The predicate never joined selector export and consume on the same `(interface_id, kind)`.

## General mechanism

Selector lookup now resolves one exact consumed interface. For authored interfaces it
examines only that matching publish row's exports. For a derived interface it first
matches the derived id/kind, then exposes only that interface's role/control token. A
different offered interface from the same producer grants no observation authority.

## Validation

Product-target and motion-control producers each publish two placement interfaces. The
camera passes when it consumes the interface exporting the measured role and fails
`point-projection-interface` when it consumes the other valid interface. Missing consume
and owner-mutation fixtures remain blocking.

## Release and rollback

No schema change. Multi-interface producers whose consumers relied on unioned exports
must name the correct existing interface. Rollback would restore ambiguous predecessor
authority and is unsafe.
