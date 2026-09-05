---
id: HIR-0224
title: An escape is a defect, and a repeat does not execute
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: an_unexpected_exception_reached_the_model_as_retryable_prose_and_every_retry_ran_the_handler
mechanism: one_boundary_policy_types_an_escape_as_a_defect_and_refuses_the_repeat_without_executing
adr: null
---

# An escape is a defect, and a repeat does not execute

## Observed failure

Four citations in one day, three shots.

**hansa_silk_road**, layer-1 materialization: twelve consecutive
`finalize_materialization` calls over roughly 360 seconds, each returning

```
expected str, bytes or os.PathLike object, not NoneType
```

no JSON pointer, no addressable row, no tool named. The session diagnosed it correctly in
the end — it deliberately corrupted its own candidate's `activates_at` to test whether
finalize returns structured findings or the same crash, got the identical crash, concluded
the failure preceded content evaluation, and stopped without making speculative edits.
Good behaviour, defeated by an untyped result, after twelve attempts.

**room_1046_opening**, layer 2: the same escape, five attempts at roughly $1.70 each. It
stopped at attempt two of four rather than let the run exhaust its retries.

**A provider entitlement error** earlier the same day was classified `unknown failure` and
retried against a condition no retry can clear.

**The `build` boundary** terminalized a precise `ValueError` — *"selected authority
capsules do not preserve the stable topological layer order"* — as `harness_defect` /
`unclassified-boundary-…` with no cause. The inner message was exact; only the boundary
lost it.

## Root cause

Not a forgotten exception type. There is no boundary policy, so every handler picks its
own tuple. Across the plan tools:

```
(OSError, ValueError, TypeError, json.JSONDecodeError)     x2
(ValueError, OSError, json.JSONDecodeError)                x3   <- the one that raised
(OSError, ValueError, TypeError)
(OSError, ValueError, AttributeError, json.JSONDecodeError)
(TypeError, json.JSONDecodeError)
(OSError, ValueError)                                      x2
(FileNotFoundError, ValueError)
```

**Ten distinct hand-picked tuples.** Whether a programming error reached the model as
retryable prose depended on which tool it happened in — `finalize_materialization` omitted
`TypeError` while the handler forty lines above it included it.

Adding `TypeError` at line 603 would have fixed the instance and left the mechanism: the
next handler, and the next exception type, decide again.

The insight that removes the guessing: **a handler already catches what it can express as
a refusal, so anything that escapes it is by definition unexpected.** Escaping *is* the
classification. No type list is needed and none can drift.

Both halves of the cost are separate. The result being untyped is why the model could not
tell a defect from a finding. Every retry running the handler is why it cost twelve
attempts rather than one.

## Decision

`agents/plan_tools/boundary.guard_tool_boundary` wraps every plan tool at the single point
where the server is built, replacing all ten tuples with one policy:

- **An escape is typed as a defect.** The result names the tool, the exception type, and
  states that it carries no JSON pointer, is not addressable by editing the candidate, and
  must not be retried.
- **A repeat does not execute.** After a defect, further calls to that tool in that session
  return without reaching the handler. The bound is the harness's, not the model's
  judgement, so cost stops whether or not the session reasons well.
- **The defect is run evidence**, logged with the tool, type and occurrence, not left only
  in a transcript.

Refusals are untouched. A handler that returns `is_error` prose keeps returning exactly it;
only escapes are reclassified, and a successful result passes through unchanged.

## Validation

`src/tests/unit/test_tool_boundary_policy.py`:

- the exact escape that killed two runs is typed as a defect naming the tool and type;
- a repeat never reaches the handler — asserted by counting handler executions across
  eleven calls, against hansa's twelve and room's five;
- five different exception types are all covered, because escaping is the signal;
- a handler's own refusal keeps its wording, and a success passes through unchanged;
- the defect reaches an `on_defect` callback, so the run records it;
- **the discriminator**: the same handler with and without the policy. Unguarded, three
  calls reach the handler three times and the exception escapes each time. Guarded, one
  reaches it and the rest are refused. Deleting the module makes the other tests fail on
  import, which proves only that the module is new; this states what the mechanism does.

## Out of scope, and why

The `build` boundary's untyped terminalization is the same class one layer out, and is not
fixed here. It is a stage boundary rather than a tool handler: its cure is that a stage
which raises something it did not classify publishes a typed stop carrying the inner
message, rather than `unclassified-boundary` with the cause discarded. That needs the
terminalizer, and bolting it onto a tool-boundary change is the sequencing that cost two
runs earlier today. Recorded as open, with hansa's framing: the inner message was
perfectly precise and only the boundary lost it.
