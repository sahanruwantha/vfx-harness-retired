# Changelog

All notable changes to VFX Harness are recorded here. Detailed causal reasoning and validation
live in the linked Harness Improvement Records.

## Unreleased

### Changed

- Isolated generated output under `runs/<run-id>/` with stable log, report, evidence,
  checkpoint, scratch, and deliverable categories.
- Added manifest, status, summary, artifact-index, and latest-run metadata for machine readers.
- Added explicit run selection and run listing to `vfx inspect` while retaining legacy readers.

See [HIR-0002](docs/improvements/HIR-0002-structured-run-output.md) and
[ADR-0002](docs/decisions/ADR-0002-run-scoped-artifact-authority.md).

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
