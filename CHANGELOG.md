# Changelog

All notable changes to VFX Harness are recorded here. Detailed causal reasoning and validation
live in the linked Harness Improvement Records.

## Unreleased

### Changed

- Made repository-root `.env` loading resolve the checkout root instead of `src/`.
- Made `vfx plan --until-clean` exit 3 and publish a failed run when blocking findings remain.
- Made the plan gate report every unknown work-unit dependency in one repair brief instead of
  revealing one invalid layer per paid repair round.
- Persisted final plan-gate authority in `reports/plan_gate.json` and terminal run metadata.
- Added warm-session validation for planner machine artifacts and a bounded read-only repair gate.
- Unified reference fingerprints under the typed `vfx-harness.look-vector/v1` metric registry.
- Added temporal scene contracts, rendered frame-delta evidence, and temporal claim coverage.
- Added projected-composition and mutation/fault-ownership coverage warnings.
- Added run-owned content-addressed plan bundles, atomic `plans/current.json` publication, and
  run-isolated repair snapshots as the first ADR-0004 migration slice.
- Declared global planner role capabilities so draft, verify, and repair can all patch artifacts
  and call the bounded deterministic gate while their context is warm.
- Moved global-plan authoring into an authored-input-only workspace owned by each run, preventing
  prior shot-root plans, contracts, questions, and run files from leaking into a fresh pass.
- Isolated generated output under `runs/<run-id>/` with stable log, report, evidence,
  checkpoint, scratch, and deliverable categories.
- Added manifest, status, summary, artifact-index, and latest-run metadata for machine readers.
- Added explicit run selection and run listing to `vfx inspect` while retaining legacy readers.
- Added model-free, gate-checked promotion of retained clean plan candidates into fresh
  run-owned immutable bundles.
- Added `vfx units replan` for fail-closed migration of durable work-unit state between an
  explicitly named old bundle and current selected plan authority.
- Added audited failed-unit retry transitions and made direct build runs fail when any requested
  work unit remains unaccepted.
- Redefined plan cleanliness as structural authority, added explicit decision strengths and typed
  `hypothesis_falsified` work-unit outcomes, and bound transactional replanning to those immutable
  executable findings while preserving hard-constraint approval.
- Routed terminal unit failures whose failing bound contracts are a decision's declared
  falsification path into the same typed `hypothesis_falsified` outcome, so an unreachable
  approved or planner start stops as replanning evidence instead of a generic unit failure.
- Scoped global-plan recipe selection, spikes, and numeric check calibration to Layer 1 and
  cross-layer DAG facts; later-layer execution detail now waits for its JIT pass and upstream
  checkpoints instead of being simulated before the first build.
- Made the subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) the default Claude credential when
  both are configured, withholding `ANTHROPIC_API_KEY` from the SDK unless
  `VFXH_CREDENTIAL=api_key` selects it; preflight reports the applied selection.
- Limited plan spikes to optional citation-integrity evidence: claimed spikes now freeze exact
  script, output, Blender identity, and contract rows, while unspiked composition work may proceed
  to its producing runtime unit.

See [HIR-0002](docs/improvements/HIR-0002-structured-run-output.md),
[HIR-0003](docs/improvements/HIR-0003-truthful-until-clean-planning.md),
[HIR-0004](docs/improvements/HIR-0004-checkout-root-environment-loading.md),
[HIR-0005](docs/improvements/HIR-0005-plan-gate-authority-and-warm-repair.md),
[HIR-0006](docs/improvements/HIR-0006-canonical-reference-fingerprints.md),
[HIR-0007](docs/improvements/HIR-0007-temporal-and-ownership-evidence-coverage.md),
[HIR-0008](docs/improvements/HIR-0008-transactional-plan-publication-foundation.md),
[HIR-0009](docs/improvements/HIR-0009-run-scoped-plan-authoring.md),
[HIR-0010](docs/improvements/HIR-0010-executable-plan-authority-and-due-gates.md),
[HIR-0011](docs/improvements/HIR-0011-build-time-plan-falsification.md),
[ADR-0002](docs/decisions/ADR-0002-run-scoped-artifact-authority.md),
[ADR-0003](docs/decisions/ADR-0003-explicit-metric-and-temporal-evidence-identity.md),
and [ADR-0004](docs/decisions/ADR-0004-transactional-plan-authority.md).

## 0.3.0 — 2026-08-21

### Changed

- Renamed the project, package, commands, and environment prefix from the former project name to
  VFX Harness, `vfx-harness`, `vfx_harness`, `vfx`, and `VFXH_*`.
- Renamed the GitHub repository to `sahanruwantha/vfx-harness`.
- Reorganized runtime modules by decision responsibility.
- Separated architecture, decisions, improvements, operations, and research documentation.
- Separated tracked evaluation definitions from generated evaluation evidence.
- Categorized tests as unit, contract, architecture, and integration guarantees.

See [HIR-0001](docs/improvements/HIR-0001-project-identity-and-repository-structure.md) and
[ADR-0001](docs/decisions/ADR-0001-repository-authority-boundaries.md).
