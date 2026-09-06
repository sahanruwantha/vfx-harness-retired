---
id: HIR-0239
title: A phase start is one instant, not two clock reads
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: started_at_and_last_event_at_were_captured_by_two_separate_clock_reads
mechanism: phase_heartbeat_begin_reads_the_clock_once_and_shares_the_value
adr: null
---

# A phase start is one instant, not two clock reads

## Observed failure

A full-suite run failed on:

```
src/tests/unit/test_phase_liveness.py::test_heartbeat_records_the_deadline_and_the_last_event
assert record["last_event_at"] == record["started_at"]
```

on a 1 ms difference. The same test passed 5/5 in isolation, and the commits in that run
touched none of the code involved.

## Why it was allowed

`observability/phase_heartbeat.begin` built its state with:

```python
"started_at": _now(),
"last_event_at": _now(),
```

`_now()` is `datetime.now(UTC).isoformat(timespec="milliseconds")`. Before any event has
arrived, "when the phase started" and "when it last saw something" are the *same instant*
by definition -- one value. It was derived twice, and the two derivations agreed on all
but a thin slice of millisecond boundaries.

That makes it a very small member of the family this repository has been finding all week:
one quantity, two derivations, nothing pinning them. The distinctive thing here is the
failure mode. The other members disagreed *always* once their inputs diverged, and were
found by their consequences. This one disagrees at a rate near 1e-3 per call, so it
presents as a flaky test rather than as a defect -- and a flaky test invites a retry, a
tolerance, or a `pytest.approx`, all of which preserve the second derivation.

It is not only a test concern. HIR-0200 made the heartbeat the thing a reader consults to
compute a phase's remaining budget from `last_event_at`. On the wrong side of a boundary a
freshly opened phase reported a `last_event_at` after its own `started_at`, describing an
event that had not happened.

## Mechanism

`begin` reads the clock once and shares the value:

```python
started = _now()
...
"started_at": started,
"last_event_at": started,
```

## Validation

`src/tests/unit/test_phase_liveness.py::test_begin_reads_the_clock_once` makes the race
deterministic rather than asserting the identity and hoping. It monkeypatches `_now` with
a generator returning a different timestamp on every call, so a second read *must* differ:

```
E       AssertionError: assert '2026-09-06T00:00:00.001+00:00' == '2026-09-06T00:00:00.000+00:00'
```

on the pre-fix tree -- the same 1 ms shape as the observed failure, now at 100%. The test
also asserts that a real `event()` still moves `last_event_at`, so the fix cannot be
satisfied by freezing the field.

The existing `test_heartbeat_records_the_deadline_and_the_last_event` keeps its assertion.
It is a true statement about the record and it is now also always true.

## What this does not fix

Nothing else calls `_now()` twice for one instant; `event()` and `end()` each read it once
for a genuinely new moment. This record does not claim the family is exhausted -- it
claims that a defect presenting at 1e-3 is still a defect, and that the honest response to
an unreproducible timestamp assertion is to make the race deterministic, not to loosen the
assertion.
