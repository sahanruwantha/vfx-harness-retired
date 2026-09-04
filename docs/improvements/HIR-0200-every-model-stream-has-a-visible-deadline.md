---
id: HIR-0200
title: Every model stream carries the idle deadline, and its remaining budget is readable
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: liveness_was_unobservable_and_the_deadline_watchers_reasoned_about_did_not_exist
mechanism: shared_idle_deadline_stream_helper_with_a_run_scoped_phase_heartbeat
adr: null
---

# Every model stream carries the idle deadline, and its remaining budget is readable

## Observed failure

Three sessions watching three shots each built a different liveness heuristic, and two of the
three answers they produced were wrong.

```
caesar  run 20260904T143311Z-c0f282  layer 1 materialization, transcript silent 10 minutes,
        SDK child 1033 -> 1041 CPU ticks over 5s: healthy, mid-turn
        earlier: one false stall alarm, one retracted near-miss, both from console/transcript mtime
hansa   run 20260904T143358Z-238376  console.log frozen at t+3.1s while the transcript ran 4 minutes ahead
room    run 20260904T143607Z-565c1e  same question asked from mtime, same ambiguity
```

The method that finally worked was reading `/proc` CPU counters of the SDK child process.
No artifact under `runs/` distinguishes a ten-minute assistant turn from a dead session,
because the transcript records discrete SDK events and a long turn produces none.

Worse, the deadline those watchers were reasoning about did not exist on the streams they
were watching. `model_event_idle_seconds` was enforced in exactly one place, the builder's
drain loop. The planner, unit-plan, materialization, rematerialization, critic and approach
streams iterated `query(...)` directly, so a genuinely hung materialization would have hung
forever, and AGENTS.md's rule that every model response stream has a positive event-idle
deadline (HIR-0138) was true of one stream out of seven.

## Root cause

The deadline was implemented at a consumer rather than at the stream. `drain.py` wrapped its
own `anext` in `anyio.fail_after` as part of builder-specific accounting, so the rule
travelled with that loop instead of with the thing it governs; every later stream added
elsewhere silently opted out, and nothing could detect that because the rule lived in prose.

The observability half has the same shape: liveness was inferred from artifacts written for
another purpose. The transcript exists to record events, the console to narrate; neither is a
statement about how much of a deadline remains, and both go quiet exactly when the question
is asked. A recurring guess by three independent watchers is the harness's own definition of
a missing instrument.

## Decision criteria

- The deadline belongs to the stream, not to a consumer: one helper every stream iterates.
- Expiry is a typed session failure with the existing `model_session_idle_timeout` cause, not
  an operator interrupt and not a silent hang.
- Liveness is answered by an artifact the harness writes on purpose, carrying the deadline
  and the last event, so a reader computes the remaining budget rather than inferring it.
- Observability never breaks the stage it observes: an unwritable heartbeat is dropped.

## General mechanism

1. `agents/model_stream.with_idle_deadline(stream, label=, idle_seconds=)` yields every SDK
   message under `anyio.fail_after(deadline)`, records a typed `model_event_idle_timeout`
   transcript event with the messages seen and the last message type, and raises
   `AgentSessionFailure(..., "model_session_idle_timeout")`. A non-positive deadline is
   refused. The planner draft/verify/repair, unit-plan, materialization, rematerialization,
   critic and approach streams all iterate through it; the builder's drain keeps its own
   richer loop, which already enforced the same deadline.
2. `observability/phase_heartbeat` writes `runs/<id>/logs/phase-heartbeat.json`
   (`vfx-harness.phase-heartbeat/v1`): stage, label, state, `deadline_seconds`, event count,
   last event kind, `last_event_at`, `started_at`. `transcript.bind` opens it with the
   configured deadline, `transcript.message` ticks it (throttled to one write a second), and
   `transcript.unbind` closes it. Every drain path already funnels through those, so no
   further wiring exists to forget.
3. An architecture test rejects any `async for` over `query(...)` or `receive_response()`
   outside the helper and the builder drain.

## Rejected patch-level alternatives

- Flushing the console more eagerly: the console lags because nothing is logged during a long
  turn, not because a buffer holds it.
- A watchdog thread logging "still alive": a second clock to keep consistent with the real
  deadline, and still not the remaining budget.
- Documenting the `/proc` method in the runbook: institutionalises process forensics as the
  liveness instrument.
- Adding the deadline to each stream inline: six copies of the rule, which is how one copy
  came to exist in the first place.

## Validation

- `src/tests/unit/test_phase_liveness.py`: the heartbeat records the deadline, the last event
  kind and time, closes on unbind, and survives an unwritable destination; a stalled stream
  raises the typed `model_session_idle_timeout` after its deadline with the messages seen and
  last message type in the transcript event, while a healthy stream yields every message and
  records nothing; a non-positive deadline is refused; no module iterates an SDK stream
  outside the helper.
- The planner, builder-stop, and architecture suites pass unchanged.

## Release and rollback

Additive artifact and a new failure path on streams that previously could hang: a
materialization that emits no event for the configured deadline now fails typed instead of
waiting forever. Rollback removes the helper's use, restoring the single-consumer deadline.

## Remaining limitations

The heartbeat is per run, not per concurrent session, so a run with two live model sessions
reports the most recent event of either; today the harness serialises model work within a run.
Its `last_event_at` is wall clock, so a reader compares against its own clock. The builder
drain still owns a second copy of the deadline logic, kept because it carries builder-specific
accounting; a later change should let it consume the helper.
