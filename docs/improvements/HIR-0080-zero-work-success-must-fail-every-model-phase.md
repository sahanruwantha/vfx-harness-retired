---
id: HIR-0080
title: Zero-work success must fail every model phase
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: model_transport_false_success
mechanism: phase_local_work_accounting
adr: null
---

# Zero-work success must fail every model phase

## Observed failure

Run `20260828T001601Z-9ca8f6` exhausted the Claude subscription after a productive live
builder session. A later revision returned the monthly-spend-limit sentence as a one-turn
SDK `success` with no new tool calls or cost. The initial builder boundary already rejected
that transport shape, but revisions and file finalization did not. Orchestration advanced
into another critic round and then waited in finalization until the operator interrupted it.
The terminal result of the productive 59-turn response already carried `is_error=true` and
`api_error_status=429` beside `subtype=success`; the collector discarded both provider fields.

## Root cause

Zero-work detection was attached to the initial builder response, not to the model-session
boundary. It also compared cumulative session cost. A later phase in a previously paid
session therefore inherited nonzero cost and appeared productive even when that phase spent
nothing and invoked no tool.
More fundamentally, the normalized result retained the SDK's misleading subtype but not the
provider's explicit error bit or HTTP status, so useful earlier work masked an incomplete
terminal response.

## General mechanism

Every mutating model phase accounts for work locally: tool-call delta and incremental session
cost since the preceding response. A nominal `success` with zero new tools and zero incremental
cost raises `BuildTruncated` before critique, journal finalization, replay, or repair can consume
it. The rule covers live revisions, turn-cap continuations, restored-checkpoint acknowledgement,
finalize-script, and canonical-repair sessions. The transcript records the named phase and
transport facts. Existing non-success SDK subtypes keep their typed error path.

The response collector also preserves `is_error` and `api_error_status`. Either provider error
fact outranks a success-like SDK subtype regardless of accumulated cost or prior tool work,
because the phase never reached its completion boundary. The same check fences critic verdicts,
so billing prose cannot be parsed or retried as qualitative judgment. Planner resilience signals
retain these fields for terminal/transient classification instead of reducing the result to its
subtype.

`BuildTruncated` carries a typed terminal cause through `RequestedExit`: provider/zero-work
responses publish `model_session_failure`, max-turn exhaustion publishes
`max_turns_exhausted`, and the model-dollar ceiling publishes `model_budget_exhausted`.
Structured `status.json` therefore agrees with its human detail instead of flattening every
exit-3 boundary into `requested_exit`.

Optional `get_context_usage` telemetry is skipped once the terminal result already carries
provider error facts. Run `20260828T004632Z-3cbe2c` otherwise spent roughly 60 seconds waiting
for that control request after receiving the zero-cost 429 in under a second. Telemetry cannot
delay or override an authoritative provider failure.

This does not treat text-only reasoning as an error when the provider charged for it. The
mechanism recognizes the measured spend-limit/authentication failure shape rather than assuming
that every phase must mutate.

## Rejected patch-level alternatives

Checking only the kickoff repeats the observed hole. Searching assistant prose for one vendor's
limit wording is brittle and locale-dependent. Treating cumulative cost as phase work lets a
paid earlier response subsidize an empty later one. Waiting longer cannot turn an already
terminal zero-work response into an artifact.

## Validation

Unit tests pin incremental-cost behavior: unchanged cumulative cost plus zero tool delta is
rejected, while a paid text-only response or a phase with tool activity remains legal. The
observed productive `success + is_error + HTTP 429` shape is pinned as incomplete, and the
shared planner result signal pins preservation of both provider fields. The
run-artifact test pins propagation of `model_session_failure` into structured status. Real run
`20260828T004444Z-c5e048` proves the provider facts now stop the builder in 5.8 seconds before
critic/finalizer; it also supplied the regression evidence for the terminal-cause propagation.
The drain test pins that a provider error never calls optional context-usage telemetry.
The
full suite is the regression ratchet. A producing-path rerun must prove the next exhausted or
invalid credential boundary terminates as truncated without launching downstream judgment.

## Release and rollback

No persisted schema migration. The change only tightens terminal response validation at model
phase boundaries.
