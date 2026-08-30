---
id: HIR-0138
title: Model response streams have an idle deadline
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: nonterminal_model_stream_hangs_builder_indefinitely
mechanism: typed_configurable_event_idle_deadline
adr: ADR-0006
---

# Model response streams have an idle deadline

## Observed failure

Room 1046 Layer 2 run `20260830T065124Z-0e1b53` reached six passing massing
contracts, rendered and compared f39, then emitted no further SDK event for more than
seven minutes. The `vfx build --layer 2` process and Blender worker remained alive and
idle. The run had to be interrupted by the operator, producing exit 130 and no typed
model-session cause.

The last transcript event was the successful `compare_frame` result. There was no
terminal `ResultMessage`, provider error, turn-cap result, or spend-cap result.

## Root cause

`_drain_once` awaited the SDK response iterator without a deadline. Turn and dollar
caps only act when the SDK emits a result, so neither bounds a response stream that
stops producing events. Process liveness was incorrectly treated as progress.

## Decision

- Every SDK response drain has a positive event-idle deadline, configured by
  `VFXH_MODEL_EVENT_IDLE_SECONDS` and defaulting to 360 seconds.
- The deadline resets after each received SDK event. It is not a total response or
  session-duration limit.
- Expiry journals `model_event_idle_timeout` with the configured seconds, number of
  messages received in this response, and last message type.
- Expiry raises typed `BuildTruncated` with terminal cause
  `model_session_idle_timeout`. No critique, freeze, replay, or publication follows.
- Recovery remains the audited unit retry path. Resume is legal only when the ledger
  already names an existing checkpoint and journal.

## General mechanism

The response iterator is advanced one event at a time inside `anyio.fail_after`.
`StopAsyncIteration` remains a normal response boundary; deadline expiry is distinct
from max turns, max spend, provider failure, and operator interruption.

The setting lives in the shared typed runtime configuration so deployments may enlarge
the deadline for providers with known long silent inference while retaining a finite
bound. Zero and malformed values fail during configuration loading.

## Rejected alternatives

- Poll process existence: both deadlocked and productive sessions have live processes.
- Depend on max turns or spend: those require a terminal SDK message and did not fire.
- Automatically accept the passing scene contracts: the agent had not ended its
  transaction, and the comparison visibly showed a generic proxy.
- Automatically restart the model session: silent retry would lose candidate identity
  and could duplicate mutations or spend.

## Validation

A deterministic async fixture whose response iterator never yields must expire at a
short test deadline, emit the typed journal event, and raise
`model_session_idle_timeout`. Configuration tests cover the default, override, and
non-positive rejection. Existing provider and successful-result drain tests must remain
green.

Production evidence is the interrupted run above; the next silent response will fail
closed without operator intervention.

## Release and rollback

No persisted schema changes. Rollback restores an unbounded wait and is unsafe because
a live process can again strand a unit indefinitely without a terminal result.
