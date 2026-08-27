---
id: HIR-0038
title: Materialization sessions bind a durable transcript
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: materialization_sdk_stream_was_stdout_only
mechanism: materialize_binds_transcript_like_global_plan
adr: null
---

# Materialization sessions bind a durable transcript

## Observed failure

Remat6's failure loop (invented judge frames, directory Read of
`plans/outcomes`, `max_turns_exhausted`) could not be reconstructed from
disk. Global `generate_plan` binds `transcript` and logs the path.
`_materialize_deferred_layer` called `log_message` but never `transcript.bind`.
Unbound `log_message` still pretty-prints to stdout; `transcript.message` is
a no-op.

Unit-plan sessions (`generate_layer_plan`) already bound; materialization —
the session that authors contracts — did not.

## Root cause

`log_message` journals only when a transcript is bound. Materialization was
the one planner session that skipped bind/unbind/costlog. Classification:
observability gap. A crashed remat left the SDK stream in terminal
scrollback.

## Decision criteria

- One JSONL record per materialization session, same shape as global plan.
- Bind before `query`, unbind in `finally`, record `died` on exception.
- Log the transcript path so operators do not have to guess the run layout.
- Distinct label (`materialize-layer-{id}`) from the later unit-plan
  (`layer-{id}`) so remat then unit-plan in one invocation do not collide
  meaning.

## General mechanism

- `_materialize_deferred_layer` binds `costlog` (`plan:materialize`) and
  `transcript` (`plan` / `materialize-layer-{id}`), records the kickoff, and
  unbinds in `finally`.
- `generate_layer_plan` logs the transcript path it already bound.

## Rejected patch-level alternatives

- Grep the console tee: `vfx plan --layer` tees only when the driver
  wraps it; a direct remat has no second copy.
- A second logger: bind the existing one.

## Validation

- `tests/unit/test_planner_outcomes.py`:
  `test_materialize_deferred_layer_binds_a_transcript`.

Do not judge this HIR by treating remat6 as reconstructed. The first
materialization after this change is the path that writes
`logs/transcripts/plan/materialize-layer-*.jsonl`.

## Release and rollback

No schema migration. Rollback is stdout-only materialization again.

## Remaining limitations

Bind does not attach reference images into the JSONL (payloads are omitted
by design). Kickoff prose is stored. If `VFXH_NO_TRANSCRIPT=1`, bind still
returns None — tests that disable transcripts are unchanged.
