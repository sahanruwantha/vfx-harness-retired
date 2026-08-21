---
id: HIR-0001
title: Establish VFX Harness identity and repository authority boundaries
status: accepted
introduced_in: 0.3.0
date: 2026-08-21
failure_class: ambiguous_repository_authority
mechanism: responsibility_oriented_repository_structure
adr: ADR-0001
---

# Establish VFX Harness identity and repository authority boundaries

## Observed failure

The project retained a shot-derived name, a flat Python package, root-level design documents,
and a single `evals/` tree containing both evaluation machinery and generated shot data. A coding
agent could find relevant files but could not cheaply determine which file owned a decision or
whether an artifact was safe to generalize from.

## Root cause

The repository grew through successful local fixes. Files were placed near the code that existed
at the time rather than under a stable authority model. Naming, packaging, CLI commands,
environment variables, tests, and generated paths consequently encoded historical structure.

## General mechanism

- Rename the product, distribution, package, CLI, and environment prefix to VFX Harness.
- Add `AGENTS.md` as the coding-agent routing and invariant index.
- Group runtime code by application, domain, orchestration, agent, Blender, evidence,
  evaluation, knowledge, observability, and infrastructure responsibility.
- Separate current architecture, decisions, improvements, operations, and research.
- Separate tracked evaluation definitions from generated evaluation outcomes.
- Categorize tests by the guarantee they provide.

## Rejected patch-level alternatives

- Search-and-replace the display name only: package and operational identity would still drift.
- Add more navigation prose without moving files: Codex would still need to infer authority.
- Delete old evaluation output: historical evidence remains useful and was moved, not discarded.

## Validation

- The full Python suite passes after the move: 47 tests, including three package-boundary checks.
- Ruff reports no findings across source, tests, and tracked probes.
- The package builds and installs as `vfx-harness`.
- The built wheel contains the reorganized modules and packaged VFX recipe Markdown.
- `vfx --help` loads the reorganized command dispatcher.
- Generated evaluation data was preserved under `artifacts/evaluations/`.
- The GitHub repository and `origin` are `sahanruwantha/vfx-harness`.

## Remaining work

- Add stricter architecture tests as cross-package dependencies are reduced.
- Populate heterogeneous tracked fixtures and versioned suite definitions.
- Convert future production failures into focused HIRs instead of extending this record.
