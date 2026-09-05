---
id: HIR-0231
title: The audit record leads with the cause, not the authority dump
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: the_cause_was_ninety_five_bytes_behind_a_two_hundred_and_seventy_kilobyte_dump
mechanism: the_authority_snapshot_moves_behind_a_locator_and_the_record_holds_the_exception
adr: null
---

# The audit record leads with the cause, not the authority dump

## Observed failure

HIR-0226 put `reports/unclassified-boundary-audit.json` into the documented reading order
and made the stop envelope name it, because two shot drivers had diagnosed unclassified
boundaries from console tracebacks while that file held the answer unread.

It does hold the answer. It also holds a great deal else. A real record from this
repository's artifacts, measured by key:

```
authority_sources        270586 bytes      <- sorts first
boundary                      7
exception_message            74
exception_type               21
exit_code                     1
legacy_terminal_cause        15
run_id                       25
schema                       44
TOTAL                    519444 bytes
```

**The two fields that answer "what happened" are 95 bytes, and they sit behind 270KB.**
Reports are written with `sort_keys=True`, so `authority_sources` leads by accident of the
letter 'a'. A reader opening the file with a line-limited read, a `head`, or any bounded
tool meets the accepted-build dump and never reaches `exception_message`.

So HIR-0226 fixed the pointer to the file and left a second lookup inside it. The
`hansa_silk_road` driver found this by doing the thing the fix was written to enable —
opening the audit as a reader — and reported that the file answered their question and
that they would not have got to the answer.

## Root cause

The record was written as one document because the authority snapshot and the exception
were produced in the same function and both belonged to the same event. Nothing decided
that a 270KB provenance dump and a 95-byte cause should share a file; they were simply
adjacent in the code. Alphabetical serialization then chose the reading order, and chose
the dump.

This is the same rule HIR-0226 applied one level up — a faithful record nobody reaches is
operationally not there — applied to the inside of the record HIR-0226 pointed at.

## Decision

The audit record holds the cause. The authority snapshot moves to its own report and is
named by locator:

```
reports/unclassified-boundary-audit.json        schema .../v2
    exception_type, exception_message, boundary, exit_code, run_id,
    legacy_terminal_cause, authority_sources_report
reports/unclassified-boundary-authority.json    schema .../v1
    authority_sources
```

No evidence is lost: the snapshot is published in full, one hop away, and the audit names
where. The audit is not digest-bound — only its locator reaches `terminal_metadata` — so
this is a serialization change rather than an authority change, and the schema goes to
`/v2` because the shape changed.

## Validation

`src/tests/unit/test_run_artifacts.py`:

- the audit carries `exception_message` and does **not** carry `authority_sources` inline;
- `authority_sources_report` resolves to a file that does carry it, so nothing is lost;
- the record is under 2000 bytes, so any bounded read reaches the cause. That bound is the
  actual assertion — the point is not the key order, it is that the file is small enough
  that order stops mattering.

One existing test read `authority_sources` from the audit and now follows the locator. The
evidence it asserts on is unchanged; only its path is.

## Consequence

HIR-0226's own record says the load-bearing half was the reading order, because prose in a
stop envelope trains a reader to skip it. This is the same argument one level further in:
a pointer into a 519KB file is a pointer to a haystack. The fix is not "order the keys" —
sorted output would undo that at the next serializer change — it is that the record a
reader opens should be small enough that ordering is not load-bearing.

Found by a driver reading the artifact rather than by a test, which is the fourth time
today the useful correction came from someone opening a file rather than running a check.
