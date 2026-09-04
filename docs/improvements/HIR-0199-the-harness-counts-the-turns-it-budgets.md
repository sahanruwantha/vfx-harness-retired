---
id: HIR-0199
title: The harness counts the turns it budgets, beside the CLI's own counter
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: a_budget_was_reported_in_a_counter_the_harness_never_observed
mechanism: harness_owned_assistant_turn_counter_declared_at_one_options_constructor
adr: null
---

# The harness counts the turns it budgets, beside the CLI's own counter

## Observed failure

Three sessions running fresh shots reported the verify budget's arithmetic (HIR-0198) and
then a sharper problem: the number printed at completion is not the number that was capped.

```
hansa  run 20260904T143358Z-238376  cli argv --max-turns 12  ->  "done: subtype=success  turns=14"
caesar run 20260904T143311Z-c0f282  cli argv --max-turns 12  ->  "done: subtype=success  turns=11"
```

A session reported fourteen turns against a cap of twelve and terminated `success`, not
`error_max_turns`. Traced rather than guessed: `observability/log.py` read `num_turns`
straight off the SDK `ResultMessage`, the SDK forwards it verbatim from the CLI's result
JSON, and the same CLI receives `--max-turns`. Both quantities are CLI-side and this
codebase defines neither, so what `num_turns` counts cannot be read from here.

The consequence is that the budget could not be evaluated from its own logs. Caesar at 11
of 12 looked like one turn of headroom and hansa at 14 of 12 looked impossible, and both
readings were unreliable for the same reason. Raising the clamp in HIR-0198 would have been
unmeasurable for exactly the same reason.

## Root cause

The harness budgeted a quantity it did not measure. `plan_verify_turn_budget` computes an
integer in "turns", `loops.py` hands it to the SDK as `max_turns`, and completion reported a
foreign counter with the same name. Nothing in between counted the turns the harness itself
observed, although the harness reads every message of every session through one funnel
(`log_message`, which also feeds the transcript).

The deeper cause is that the budget existed only at the moment of construction. SDK options
were built in eight places, each passing `max_turns` into an opaque object, so no later
boundary could say what budget the live session was running under. AGENTS.md's own rule
covers this case ("if a decision depends on a quantity, expose an instrument that measures
it"); the decision here is an operator's, and the quantity was never instrumented.

## Decision criteria

- The harness reports the counter it budgets, in the unit it budgets, measured by itself.
- A foreign counter may still be printed, but it is labelled as its own quantity and never
  presented as the budget's measure.
- The budget is declared once, where the options are built, and is readable at every later
  boundary of that session.
- One constructor, enforced structurally, so a new call site cannot silently drop the
  declaration.

## General mechanism

1. `observability/session_turns` is a dependency-free leaf holding one session's accounting:
   `begin(budget)` declares the budget and zeroes the count, `observe_turn()` counts one
   assistant turn, `reset_observed()` starts a new session under the same declared budget,
   and `accounting()` returns both. The logger and the transcript share it, so no cycle and
   one source of truth.
2. `log_message` counts every `AssistantMessage` and resets the count on a session `init`
   system message. The completion line reads `turns=9/18` (observed over declared budget)
   and prints the CLI's counter separately as `cli_num_turns=14`. The transcript's result
   row carries `observed_turns` and `turn_budget` alongside the existing `turns`.
3. `agents/sdk_options.sdk_options(**kwargs)` is the only constructor of SDK options: it
   declares `kwargs["max_turns"]` to the counter and then builds the options. All eight
   construction sites (planner draft, verify and rematerialization, builder client, script
   agent, critic axes, approach) now call it.
4. An architecture test rejects any `ClaudeAgentOptions(...)` carrying `max_turns` outside
   that module.

## Rejected patch-level alternatives

- Reading the CLI's semantics from the SDK: it is a pass-through here, so any statement
  about what `num_turns` counts would be a guess this record forbids.
- Dropping `num_turns` from the log: it is real data from the provider and useful for
  comparing with the provider's own accounting; the defect was presenting it as the budget's
  measure.
- Counting turns in each agent loop: four loops, four counters, and the critic loop does not
  call the logger at all.
- Passing the budget through every call signature to the completion boundary: the same
  threading the eight construction sites already showed to be error-prone.

## Validation

- `src/tests/unit/test_session_turn_accounting.py`: options construction declares its budget
  and options without one declare none rather than inheriting the last; observed turns count
  assistant messages, reset per session while the budget survives, and reject non-positive
  or non-integer budgets; the completion line prints `turns=9/18` and `cli_num_turns=14` for
  the exact shipped case; no module outside the one constructor builds options with a turn
  budget.
- The planner, preflight, and architecture suites pass unchanged.

## Release and rollback

Console and transcript fields change shape: `turns=` is now the harness's observed count over
its budget, and the CLI's counter appears as `cli_num_turns`. Any reader parsing the old
`turns=` from console text sees a different quantity; the transcript keeps `turns` unchanged
and adds the two new fields. Rollback restores the CLI counter as the only report.

## Remaining limitations

What the CLI's `num_turns` counts, and therefore why it can exceed its own `--max-turns`,
remains unknown from this codebase; the harness now simply does not depend on it. The
observed counter counts assistant messages, which is the harness's definition of a turn and
may not equal the provider's. The critic loop still does not route through `log_message`, so
its sessions report no observed count; that loop has no turn budget of its own today.
