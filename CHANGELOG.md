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
- Isolated generated output under `runs/<run-id>/` with stable log, report, evidence,
  checkpoint, scratch, and deliverable categories.
- Added manifest, status, summary, artifact-index, and latest-run metadata for machine readers.
- Added explicit run selection and run listing to `vfx inspect` while retaining legacy readers.

See [HIR-0002](docs/improvements/HIR-0002-structured-run-output.md),
[HIR-0003](docs/improvements/HIR-0003-truthful-until-clean-planning.md),
[HIR-0004](docs/improvements/HIR-0004-checkout-root-environment-loading.md),
[HIR-0005](docs/improvements/HIR-0005-plan-gate-authority-and-warm-repair.md),
[HIR-0006](docs/improvements/HIR-0006-canonical-reference-fingerprints.md),
[HIR-0007](docs/improvements/HIR-0007-temporal-and-ownership-evidence-coverage.md),
[HIR-0008](docs/improvements/HIR-0008-transactional-plan-publication-foundation.md),
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
