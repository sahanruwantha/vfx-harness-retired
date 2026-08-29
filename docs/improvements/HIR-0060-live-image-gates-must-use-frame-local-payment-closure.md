---
id: HIR-0060
title: Live image gates must use frame-local payment closure
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: false_live_image_contract_reopen
mechanism: frame_local_bound_image_gate
adr: null
---

# Live image gates must use frame-local payment closure

## Observed failure

In run `20260827T192500Z-abdc37`, unit `material_energy_language` paid image debts
`matlook-f072` and `matlook-f150` with provenance-valid `runtime_checks.json` rows. A later
`compare_frame` nevertheless reported `AUTHORITATIVE IMAGE CONTRACTS: 0/2` and reopened one
repair. The agent rendered both frames again, called `contract_result` twice, and performed more
node inspections before stopping. Canonical replay subsequently accepted both payments and the
unit passed 5.0 at both frames.

## Root cause

The live comparison gate filtered the exact bound ids but then considered only rows marked
`authoritative`. Builder-authored payment rows are intentionally non-authoritative, so a valid
payment could never close that live convergence gate. It also passed the unit's cross-frame id
union to each single-frame comparison, making an f72 plate appear to omit an f150 obligation.

## Decision criteria

Builder rows must not become autonomous acceptance authority. Valid payments must still close the
live evidence loop they were designed for, and one plate may be judged only on obligations due at
that frame. Missing or failing bound rows must continue to reopen repair.

## General mechanism

The live gate receives the exact image-debt ids due at the rendered frame. When explicit ids are
provided, it requires every one to be observed and passing, regardless of whether its origin is a
planner check or a provenance-valid builder payment. The result is labelled `BOUND IMAGE CHECKS`,
not authoritative acceptance. Final unit and canonical acceptance remain unchanged and continue
to apply their existing evidence-authority rules.

## Rejected patch-level alternatives

Telling the model to stop immediately after `propose_checks` leaves contradictory tool feedback in
place. Marking all builder checks authoritative would let a builder certify its own acceptance.
Ignoring missing ids would turn partial payment into closure.

## Validation

`test_live_image_gate_consumes_exact_builder_payments_without_false_reopen` pins valid payment,
missing-payment, and non-authoritative behavior. `test_live_image_gate_scopes_cross_frame_debts_to_current_plate`
pins frame-local closure. Focused architecture tests pass.

## Release and rollback

No data migration. Revert the live gate selection independently; runtime payment schemas and final
acceptance artifacts are unchanged.

## Remaining limitations

This removes the mechanical false reopen. It does not reduce model inference latency before first
mutation or decide when optional diagnostics are no longer useful.
