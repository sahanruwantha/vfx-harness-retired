---
id: HIR-0228
title: The budget is reported against the counter it bounds
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_fix_replaced_a_counter_that_overshoots_by_three_with_one_that_overshoots_by_thirty_eight
mechanism: num_turns_is_paired_with_the_budget_and_the_stream_count_is_named_for_what_it_counts
adr: null
supersedes: HIR-0199
---

# The budget is reported against the counter it bounds

## Observed failure

Two driver sessions filed findings from the same log line on the same day. Both were
reasoning from `turns=<observed>/<budget>` as if it showed headroom against the turn
budget. It does not.

Every full triple in this repository's console logs — 168 sessions:

```
observed / cli_num_turns:   min 1.00   median 1.55   max 2.03   (equal in 2 of 168)

the one session where the cap actually engaged:
  subtype=error_max_turns   observed=75   budget=38   cli_num_turns=39      (cap + 1)

largest overshoot past the budget:
  observed        +38   (76 against 38 — 2.03x — terminated success)
  cli_num_turns    +3
```

When the cap engaged it engaged at exactly `cap + 1` on `cli_num_turns`, with `observed`
at 75. And `observed` reached 2.03x its budget on a session that terminated `success`.
**`max_turns` bounds `cli_num_turns`.** The displayed ratio pairs the budget with a counter
it does not bound.

## Root cause

This is HIR-0199's own conclusion, and it was a real observation inverted.

HIR-0199 saw `cli_num_turns` report 14 against a cap of 12, terminating `success`, and read
the overshoot as proof that counter was not the bounded one. Across the corpus
`cli_num_turns` overshoots by **at most 3**. The replacement overshoots by **38**. The fix
substituted a counter that is nearly right for one that is nearly double, and wrote the
substitution into AGENTS.md, a code comment, a module docstring, and a test name.

The mechanism is one level below the display. `observe_turn()` fired once per
`AssistantMessage`, and the SDK emits a median of **1.55** assistant messages per CLI turn.
HIR-0199's docstring says *"The harness counts the assistant turns it observes in the
stream it already reads"* — the stream does not emit one message per turn, so it never
counted turns. The existing test was named
`test_observed_turns_count_assistant_messages_and_reset_per_session`: **the test name
states the defect.**

The plumbing settles which counter is bounded, and is checkable rather than assumed:
`sdk_options` passes `max_turns` to `ClaudeAgentOptions`; the installed SDK forwards it at
`_internal/transport/subprocess_cli.py:600` as `--max-turns`; the CLI enforces it and
reports `num_turns` back on the result.

`log.py` also carried a **dead second copy** of the whole counter — `_TURNS`,
`begin_turn_budget`, `turn_accounting` — that nothing calls, left by HIR-0199's own
refactor, carrying the same wrong claim in its comment. Deleted here.

## Decision

- The result line pairs the bounded counter with the budget and reports the stream count as
  its own quantity: `turns=14/18  assistant_messages=9`, where it read
  `turns=9/18  cli_num_turns=14`.
- `observe_turn` becomes `observe_assistant_message`; `accounting()` returns
  `assistant_messages`. Renaming makes the old reading unrepresentable rather than
  discouraged — every consumer had to be updated, which is how the transcript's
  `observed_turns` key was found and corrected too.
- The stream count is kept, because HIR-0199 was right that it is available live where
  `num_turns` arrives only with the result. It is a liveness and volume signal, never
  shown over the budget.
- HIR-0199's other half stands untouched and is what made this measurable at all: one
  constructor declares the budget, pinned by the architecture test in the same module.

## Validation

`src/tests/unit/test_session_turn_accounting.py`:

- the result line is asserted over five real corpus triples, including `(75, 39, 38)` — the
  session where the cap engaged — and `(76, 41, 38)`, the 2.03x overshoot;
- the message count must never appear over the budget, skipped only where the two counters
  coincide, which happened in 2 of 168 sessions and is not a violation;
- the SDK still forwards `max_turns` to the CLI as `--max-turns`, so the claim about which
  counter is bounded fails loudly if a future SDK changes the plumbing;
- the stream counter resets per session and counts messages.

Reverting `src/vfx_harness` alone fails 8 of 10 on assertions against the emitted line:

```
E  AssertionError: [   0.0s] ■ done: subtype=success  turns=9/18  cli_num_turns=14 ...
E  assert 'turns=14/18' in '... turns=9/18  cli_num_turns=14 ...'
```

**Two checks in this change could not have failed, and were replaced.** The first sweep
searched source lines for `assistant_messages` beside a `/{budget}` — a string absent from
the pre-fix tree, so it passed against the very code it was written to catch. Rewritten to
match both names, it *still* passed: the pre-fix expression built `turns=` and `/{budget}`
on two separate source lines, which a line-level scan cannot see. A textual sweep was the
wrong instrument for an invariant about a composed string, and it was replaced by the
parametrised behavioural check above.

## Replayed against every real session

The 168 corpus sessions, through the fixed line:

```
BEFORE: numerator exceeded the budget in 9 sessions (max 2.00x) while the cap never engaged
AFTER : numerator exceeds the budget in 3 sessions (max 1.08x)

the one session where the cap ENGAGED:
   before -> turns=75/38                          reads as 97% over budget, and is
                                                  not the number the cap acted on
   after  -> turns=39/38  assistant_messages=75   reads as cap+1, which is what happened
```

The three remaining overshoots are real: `num_turns` does exceed its cap by up to 3. That
is the residual the fix does not remove and does not claim to -- it is a property of the
CLI's counter, and it is an order of magnitude smaller than the one that was displayed.

## Consequence

This is the sixth instance in one day of one quantity with two derivations where the wrong
derivation is the one that reaches the human — after a role notation (HIR-0217), a layer
order (HIR-0221), a turn count, a terminal cause (HIR-0227), and this. It is the first
where the wrong derivation was *installed by a fix intended to correct exactly this*, which
is the argument for the pinning rule applying to fixes themselves: HIR-0199 replaced one
derivation with another and pinned neither against the quantity it claimed to measure.

The finding came from a shot driver reading its own logs and disbelieving them, not from
the harness. Nothing in the runtime compares the two counters, and nothing would have.
