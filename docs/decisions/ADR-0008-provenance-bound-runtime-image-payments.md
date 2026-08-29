---
id: ADR-0008
title: Bind runtime image payments to immutable run artifacts and a harness-chosen adversary
status: proposed
date: 2026-08-27
supersedes: null
---

# Bind runtime image payments to immutable run artifacts and a harness-chosen adversary

## Context

[HIR-0053](../improvements/HIR-0053-runtime-image-payment-needs-provenance.md) records a Layer 2
unit paying two required image-contract debts with shot-root renders from earlier attempts. The
rows passed live necessity checks, but empty-scene canonical replay measured a different f150
value and stopped the unit. ADR-0002 already makes shot-root generated renders unsupported; the
runtime-check contract nevertheless accepted any path that existed and let the model select both
candidate and adversary.

## Decision

A builder-authored runtime image payment uses schema `vfx-harness.image-payment/v2` and carries:

- one immutable candidate and one immutable pre-unit adversary under the same structured run's
  `evidence/renders/`, each with path, SHA-256, frame, render mode, scale, and resolution;
- the active unit id and digest; and
- a SHA-256 identity for the exact prior-script chain from which the harness rendered the
  adversary.

The harness captures the adversary before restoring or mutating the active unit. Render tools copy
candidate plates into immutable run evidence and expose opaque handles. `propose_checks` accepts a
candidate handle, never a path, and chooses the matching adversary itself. Consumers verify the
envelope and file hashes before a runtime row can pay debt or enter image evidence. Rows without
the v2 envelope are rejected by strict migration; there is no path-based compatibility mode.

A generated threshold must also clear a measured decision margin: at least twice its resampling
noise and five percent of the measured candidate scalar. Candidate probing evaluates the same
runtime rows and canonical render settings before publication.

## Consequences

- Old `runtime_checks.json` builder rows no longer pay image-contract debt. A unit must regenerate
  them through a live v2 payment or abstain/replan.
- Models cannot manufacture or cherry-pick their own before plate, and a known shot-root filename
  cannot bypass run authority.
- Every owed frame costs one pre-unit EEVEE render. This is bounded by the active unit's debt card
  and replaces repeated manual baseline renders.
- Runtime evidence remains non-authoritative qualitative support; this decision changes identity
  and debt payment, not the rule that builder rows cannot self-certify acceptance.

## Rejected alternatives

- Check only that paths live under `runs/`: a file can be replaced or refer to another run's
  candidate without a digest and settings identity.
- Let the model continue choosing `before` but reject shot-root paths: it can still cherry-pick a
  different current-run render.
- Rely on final canonical replay: it caught this incident, but only after finalization and repair
  spent most of the unit's budget.
- Widen the failed f150 tolerance: that accepts stale evidence and weakens the measurement.

## Validation and review trigger

Unit regressions reject legacy shot paths, reject an adversary outside the producing run, revoke a
payment after artifact tampering, accept a hash-pinned v2 pair, and reject the motivating one-shot
threshold shaving. Candidate-probe and full pipeline validation are required before this ADR moves
to accepted.

Review if remote artifact storage makes filesystem paths non-portable or if heterogeneous fixtures
show the five-percent replay margin rejects stable, high-signal contracts.
