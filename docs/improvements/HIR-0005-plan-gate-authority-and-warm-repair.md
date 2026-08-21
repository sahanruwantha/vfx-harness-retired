---
id: HIR-0005
title: Persist plan-gate authority and validate planner artifacts in the warm session
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: late_and_non_reusable_plan_validation
mechanism: structured_gate_report_and_staged_warm_validation
adr: ADR-0002
---

# Persist plan-gate authority and validate planner artifacts in the warm session

## Observed failure

Run `20260821T115227Z-a8f966` wrote invalid cross-layer dependencies during its draft, but the
first deterministic feedback arrived only after the draft and verify sessions had ended. The
repair agent then received one fail-fast instance per paid round. The final run record did not
contain the gate findings as a reusable artifact, so another reader had to rerun the gate.

## Root cause

Planner writes had no file-local validation hook. The gate was terminal-only, its result existed
only as console text, and repair sessions could not call the free deterministic checker while
their context was warm.

## General mechanism

- Validate each machine artifact locally after `Write` or `Edit`; return every local error to the
  live planner without pretending an incomplete multi-file write is a cross-artifact failure.
- Block planner completion until the global plan and five machine companions exist and are
  locally valid. Keep the terminal plan gate as the sole cross-artifact authority.
- Serialize the final result to `reports/plan_gate.json`, including outcome, counts, signature,
  stats, severity, location, explanation, and repair text.
- Carry `outcome`, `blocking_count`, and `plan_gate_report` into `status.json` and the run summary.
- Give only repair sessions a read-only `run_gate` tool, capped at four calls, and require a
  pattern sweep before a repair ends.
- Treat schema-declared work-unit plan paths as future deliverables rather than dead citations.

## Rejected alternatives

- Run the entire gate after every write: valid intermediate states can reference companion files
  that have not been written yet, producing false blockers.
- Give repair sessions shell access: the gate already has a typed read-only entrypoint and shell
  authority would unnecessarily widen mutation scope.
- Keep the report only in the transcript: transcripts are diagnostic history, not terminal
  authority under the structured-run reader protocol.

## Validation

- Unit tests cover reusable gate serialization, report publication, terminal metadata, declared
  JIT output paths, and exhaustive write-time dependency feedback.
- The live shot gate still exits 3 and reports all remaining blockers in one invocation.
- `.venv/bin/ruff check src tests` passes.
- `.venv/bin/python -m pytest -q` passes: 70 tests.
- `.venv/bin/vfx --help` and `.venv/bin/vfx preflight --strict` exit 0.

## Remaining limitations

Write-time validation is intentionally local. Cross-file claim closure, temporal coverage, and
citations remain terminal gate responsibilities.
