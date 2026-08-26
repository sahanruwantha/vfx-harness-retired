---
id: HIR-0027
title: Max-turns must not publish a candidate
status: accepted
introduced_in: unreleased
date: 2026-08-26
failure_class: failed_transaction_left_durable_authority
mechanism: turn_exhaustion_fails_before_postcondition
adr: ADR-0004
---

# Max-turns must not publish a candidate

## Observed failure

Shot `vfx-test`, `l1-remat5`, run `20260826T150720Z-975cb6`. The
materialization session wrote `scratch/jit-layer-1.json`, hit
`subtype=error_max_turns` (25 turns, $4.48), and `run_session` treated the
mtime bump as success. `_materialize_deferred_layer` then called
`publish_materialization`. Validation failed (`cam-spine-parallax-fg-bg-mid`
lacks a required producing claim). The run recorded `terminal_cause:
process_error` / exit 1, not `max_turns_exhausted`.

The live pointer stayed `745967a6…` (`materialized_layers: ["2"]`) only
because the dirty candidate did not validate. HIR-0026 held. A last write
that *did* validate would have selected an unfinished view after exhaustion.
The same shape is remat3 (`bb54f1`): `error_max_turns`, then
`publish_materialization` raised `cam-vis-interior-f72 must be owned by
layer 1`.

## Root cause

`run_session` checked `succeeded()` before classifying turn exhaustion. The
materialization post-condition is "the candidate file exists and was
written," which is true of every incomplete remat that got as far as a
Write. Publication is the select (HIR-0026). Exhaustion is not a select
(ADR-0004). Checking the post-condition first made a failed session look
like a completed transaction.

## Decision criteria

- Turn exhaustion fails closed even when the caller's post-condition already
  holds. A written candidate is not a select.
- The recorded cause is `max_turns_exhausted`, not a later `ValueError`
  from publication.
- Do not loosen `max_turns`. The budget is the bound; exhaustion is the
  signal.

## General mechanism

- `run_session` classifies `_MAX_TURNS` on the collected signal **before**
  `succeeded()`. The first matching attempt raises `AgentSessionFailure`
  with `max_turns_exhausted` and is not retried.

## Rejected patch-level alternatives

- Loosen `max_turns`: remat3 walked field errors until the same ceiling;
  HIR-0023 already rejected that.
- Inspect-then-publish only: a valid-looking last write after exhaustion
  would still select. The session ending is the authority, not the file.
- Hand-edit `current.json` or the candidate: not a repair.

## Validation

- `tests/unit/test_resilience.py`:
  `test_max_turns_does_not_publish_just_because_a_candidate_exists` —
  `succeeded=lambda: True` plus `error_max_turns` raises
  `max_turns_exhausted` after one attempt. Existing max-turns tests still
  fail closed when `succeeded` is false.
- `.venv/bin/ruff check src tests`: All checks passed.
- `.venv/bin/python -m pytest -q`: 323 passed.
- `.venv/bin/python -m tests.integration.test_harness`: ALL PASS (0 failed).
- `.venv/bin/vfx --help`: ok.

Do not judge this HIR by rerunning remat5. The pointer did not move;
HIR-0026 is why. This HIR is the classification and the refused publish.

## Release and rollback

No schema migration. Rollback is restoring `succeeded()` before the
max-turns check.

## Remaining limitations

- A session that ends without the max-turns signal and with a validating
  candidate still publishes. Completeness of a layer design is still the
  validator plus `gate_preview`, not a model "done" token.
- Remat5's content residue (ledger A2 copied beside a crossing spine;
  unit judges at f72/f150 vs layer judges at f1/f240) is not this HIR.
  A2's own text requires an explicit replan if falsified; that is a
  ledger transaction, not another remat against the same satisfied
  decision.
