---
id: HIR-0179
title: A new ledger attempt never inherits the previous attempt's terminal projection
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: refinalization_crash_on_stale_terminal_projection
mechanism: ledger_attempt_lifecycle_moves_terminal_fields_to_history
adr: null
---

# A new ledger attempt never inherits the previous attempt's terminal projection

## Observed failure

Run `20260903T040612Z-0b3fe5` on `artifacts/room_1046_opening` (main `364ee6b`): layer 1
had been finalized and passed in run `20260903T002758Z-1b6807`, then rematerialized twice
(HIR-0176, HIR-0178). Both replacement units passed and the fan-in layer artifact
published under a fresh finalization claim (`lfc-da267bc2…`, attempt revision 2). The
composed finalization then called `ledger.begin(milestone)`, whose save ran the
finalization-claim scope check and raised
"active layer-finalization claim may publish only its exact in-progress ledger row".
The run terminalized `failed` with `harness_defect` after 18 minutes and $2.18 of unit work
the ledger could not project.

The `shot.json` milestone row for layer 1 still carried run `1b6807`'s terminal projection:
`status: passed`, the superseded claim id, `finalization_receipt_digest`, `script_sha256`,
and `script_sha`. `Ledger.begin` moved run id, attempt, status, rounds, and best into the
attempt history and reset them, but left the terminal fields on the live row; the scope
check (HIR-0170) refuses any in-progress row that names a receipt digest.

## Root cause

Implementation defect in the ledger attempt lifecycle. The work-unit state had already
moved the superseded layer receipt into `receipt_history` and minted a new claim, but the
`shot.json` projection row is written by the builder at finalization, and the attempt
boundary that starts a new finalization treated the terminal projection as attempt-neutral
state. Every earlier rematerialization on this state happened before the layer's first
terminal receipt, so the case was never represented in a test.

## Decision criteria

- Fail closed on stale authority is right; the scope check stays as it is.
- The projection of a terminal receipt belongs to the attempt that earned it, and history
  must keep it so the previous verdict remains auditable.

## General mechanism

`orchestration/ledger.TERMINAL_PROJECTION_FIELDS` names the fields only a terminal
finalization receipt may project (`finalization_receipt_digest`, `script_sha256`,
`script_sha`). `Ledger.begin` records them in the history entry for the previous attempt
and removes them from the live row before publishing the new in-progress row. The claim
that earned the previous attempt stays in the work-unit claim history; the live row already
names the new claim when the attempt begins. The drift check that compares a passed row's
`script_sha` to the current script runs before an attempt begins and is unaffected.

## Rejected patch-level alternatives

- Relaxing the scope check to ignore a stale receipt digest (would let an in-progress row
  impersonate a terminal one).
- Clearing the fields only in `finalize_composed_layer` (the attempt boundary is the
  owner; unit attempts follow the same lifecycle).

## Validation

- `src/tests/unit/test_layer_finalization_state.py::test_new_finalization_attempt_starts_from_a_bare_in_progress_row`:
  a row projecting a passed receipt from another claim, then a fresh claim's `begin`,
  publishes an exact in-progress row and keeps the previous digest and script hash in
  history; fails without the mechanism with the observed message.

## Release and rollback

Unreleased; the history entry gains two fields. Rolling back restores the crash on the
first re-finalization of a passed layer.

## Remaining limitations

- The active claim left by the crashed finalization must still be released through
  `vfx finalizations release` before the layer can be re-finalized; the claim was minted
  legitimately and the release path is the audited one (HIR-0170).
