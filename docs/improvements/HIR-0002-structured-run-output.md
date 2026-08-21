---
id: HIR-0002
title: Replace mixed shot outputs with structured run artifacts
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: ambiguous_generated_artifact_ownership
mechanism: manifest_addressed_run_layout
adr: ADR-0002
---

# Replace mixed shot outputs with structured run artifacts

## Observed failure

The `beacon_wake` fixture contained 16 root state files, 75 log files, 51 judged renders,
49 temporary Blender artifacts, 11 snapshots, and 22 historical scripts. Outputs from separate
runs shared directories. Names repeated frame/stage data inconsistently, while readers joined
reports, transcripts, renders, and costs through unrelated globs. A coding agent could not know
which output was current or authoritative without reconstructing pipeline history.

## Root cause

Run identity was metadata attached to selected files rather than the owner of an output tree.
Each feature chose a convenient path independently, and readers encoded the same assumptions as
writers. The shot root therefore mixed authored inputs, durable state, accepted outputs,
historical evidence, resumable checkpoints, and disposable files.

## Decision criteria

- One bounded entrypoint for machine readers.
- No cross-run overwrite or implicit merge.
- Explicit authority and lifetime for each category.
- Local, copyable output with no service dependency.
- One strict contract for full runs and direct stage invocations.
- Stable accepted build paths for deterministic replay.

## General mechanism

- Add a canonical `RunLayout` selected through `VFXH_RUN_DIR`.
- Publish a manifest, terminal status, semantic summary, artifact index, and latest-run pointer.
- Separate logs, reports, evidence, checkpoints, scratch files, and deliverables.
- Route console output, transcripts, cost rows, reports, layer memory, planner labs, Blender
  scratch/snapshots, judged renders, script versions, journals, repair backups, and final video
  through the centralized layout during `vfx run`.
- Move durable builder worklists and contract gaps toward the explicit `state/` boundary.
- Teach inspect/evaluation readers to require a selected structured run.

## Rejected patch-level alternatives

- Rename the existing files: clearer names do not establish ownership.
- Add a README to each shot: prose cannot prevent writers from mixing future output.
- Keep a silent legacy fallback: it would preserve the ambiguity this mechanism removes and let
  stale evidence re-enter decisions.

## Validation

- Six focused run-artifact tests pass, including structured transcripts, terminal direct-stage
  metadata, strict direct writes, and refusal of shot-root legacy renders.
- Planner transcript path contract tests pass.
- Ruff passes across source and tests.
- The full Python suite passes: 54 tests, including an architecture guard against new ad hoc
  shot-root output paths.
- A temporary-shot `vfx run --dry-run` created only the run metadata tree, finished with state
  `dry-run`, published `reports/summary.json` and `artifacts.json`, and was returned by
  `vfx inspect --list-runs --json`.

## Release and rollback

The change is scheduled for the next minor release. Rollback means reverting the release;
unsetting `VFXH_RUN_DIR` creates a new structured direct-stage run and does not restore legacy
paths.

## Remaining limitations

- Some durable state files still use historical root names and should move incrementally under
  `state/` through explicit schema changes.
- Artifact indexes currently record size and media type, not content hashes or dependency edges.
- Remote/object-store publication is not implemented.
