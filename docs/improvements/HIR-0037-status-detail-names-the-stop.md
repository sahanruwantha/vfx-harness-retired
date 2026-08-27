---
id: HIR-0037
title: Integer SystemExit detail is the meaning, not the digit
status: accepted
introduced_in: unreleased
date: 2026-08-27
failure_class: status_detail_was_the_exit_code_digit
mechanism: requested_exit_and_digit_meaning_map
adr: null
---

# Integer SystemExit detail is the meaning, not the digit

## Observed failure

Direct `vfx build` (and accept/render) stops with exit 7. `status.json`
`detail` was `"7"`. The driver already has the meaning string
(`INCOMPLETE CHAIN`). Operators reading latest-pointer → status could not
tell incomplete chain from a generic crash without memorizing the table.

## Root cause

`_terminal_record` used `str(exc).strip() or meaning_map`. `str(SystemExit(7))`
is `"7"`, which is truthy, so the meaning map never ran. Builder/accept/render
then `raise SystemExit(7) from None` after logging a useful line to stdout
only.

## Decision criteria

- Exit codes stay integers for the parent driver.
- `status.json` `detail` is a sentence a reader can act on.
- Exceptions that already stringify to a message (`PlanGateFailure`,
  `RequestedExit`) keep that message.

## General mechanism

- `EXIT_DETAILS` maps integer codes to the existing driver meanings.
- `_terminal_record` uses the map when `str(exc)` is empty or equal to the
  digit.
- `RequestedExit(code, detail)` subclasses `SystemExit` with a human
  `__str__`. Builder, accept, and render raise it.

## Rejected patch-level alternatives

- `raise SystemExit("INCOMPLETE CHAIN")`: Python would exit 1, not 7.
- Leave stdout as the only meaning: transcripts and status.json are the
  durable record.

## Validation

- `tests/unit/test_run_artifacts.py`:
  `test_integer_systemexit_detail_is_the_meaning_not_the_digit`,
  `test_requested_exit_keeps_the_exception_detail`.
- Existing PlanGateFailure / max-turns tests still assert their messages.

## Release and rollback

No schema migration. Rollback is `detail: "7"` again.

## Remaining limitations

A `SystemExit(2)` argparse abort still has no row in the map and records
`exit 2`. Add a meaning when that code becomes a harness contract.
