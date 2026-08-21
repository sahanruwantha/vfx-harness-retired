---
id: ADR-0001
title: Organize the repository by decision authority
status: accepted
date: 2026-08-21
---

# Organize the repository by decision authority

## Context

Runtime modules, model prompts, measurements, evaluation code, research findings, probes, and
generated runs accumulated according to creation order. Coding agents had to search the whole
repository and infer whether a file was current authority, historical reasoning, or disposable
evidence. The `evals/` directory also mixed reusable definitions with private generated runs.

## Decision

Organize source by responsibility and documentation by authority. Keep runtime code under
capability packages, current design under `docs/architecture`, causal history in ADRs and HIRs,
provisional work under `docs/research`, reusable evaluation definitions under `evals`, and all
generated outcomes under ignored `artifacts` or `shots` workspaces.

The root `AGENTS.md` is a concise routing and invariant index. It points to authoritative
documents rather than repeating their contents.

## Consequences

- Imports become longer but reveal ownership.
- Moving a module may expose filesystem-relative coupling; those paths must resolve from the
  package root or an explicit configured root.
- Historical generated data remains available locally but no longer appears as source.
- Package-boundary enforcement can be tightened incrementally with architecture tests.

## Rejected alternatives

- Keep the flat package and add a larger README: search remains ambiguous and prose cannot
  establish import or artifact boundaries.
- Put every file into `core` and `utils`: those names hide rather than assign responsibility.
- Keep generated eval runs beside suite definitions: agents can mistake outcomes for authority
  and private shot data can accidentally enter version control.
