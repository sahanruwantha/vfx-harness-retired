---
id: ADR-0002
title: Make generated artifacts run-scoped and manifest-addressable
status: accepted
date: 2026-08-21
supersedes: null
---

# Make generated artifacts run-scoped and manifest-addressable

## Context

A shot directory accumulated logs, transcripts, planner probes, renders, Blender scratch files,
snapshots, script versions, reports, state, and outputs from many invocations. `RUN_ID` appeared in
some filenames but did not own a directory. Readers had to glob several locations, infer roles
from inconsistent names, and could silently combine stale and current evidence.

## Decision

Every `vfx run` invocation owns `runs/<run-id>/` and publishes the schema described by the
[run artifact contract](../architecture/run-artifacts.md). The driver propagates `VFXH_RUN_DIR`;
central path helpers route generated writers. `runs/latest.json` selects the default run.

Authored inputs, current plans/contracts, and the accepted deterministic build remain at the shot
root. Cross-run operational state belongs under `state/`. Generated run evidence has no authority
to modify either boundary without an explicit accepted transaction.

## Consequences

- Coding agents and evaluation tools have one entrypoint and a bounded reading order.
- Runs are comparable and cannot overwrite one another's evidence.
- Blender scratch data and accepted checkpoints have visibly different lifetimes.
- A shot gains more directories, but each has one authority and stable semantics.
- Existing shot-root output is intentionally ignored and must be archived or explicitly imported.

## Rejected alternatives

- Improve filenames inside the existing directories: ownership would remain implicit and globs
  would still mix runs.
- Put every file in one run directory without categories: isolation improves, but agents still
  need to infer whether a file is a report, checkpoint, render, or disposable scratch output.
- Store only an artifact database: it creates a second source of truth and makes copied runs
  unreadable without the database service.
- Move accepted build scripts into runs: downstream replay needs a stable current build boundary;
  historical copies belong in run checkpoints instead.

## Validation and review trigger

Unit tests cover layout creation, latest selection, inventory classification, structured
transcript routing, and strict direct-stage output. The full suite passes. A dry-run smoke test proves that
the driver publishes terminal metadata and that `vfx inspect --list-runs --json` discovers it.

Review this decision if multiple authoritative writers must mutate one run concurrently, remote
artifact storage becomes mandatory, or local manifests become too large to read cheaply.
