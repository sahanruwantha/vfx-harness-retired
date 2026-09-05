---
id: HIR-0227
title: A stop class is not a terminal cause
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: half_of_all_run_summaries_reported_a_terminal_cause_that_is_not_one
mechanism: typed_stops_carry_a_validated_cause_and_an_ast_test_refuses_the_category_error
adr: null
---

# A stop class is not a terminal cause

## Observed failure

`reports/summary.json` carries `terminal_cause`, whose legal values are the closed set in
`observability/unclassified_authority.py`. Reading every summary in this repository's
artifacts:

```
summaries with a terminal_cause: 115
values OUTSIDE the closed vocabulary:
    42  'harness_defect'
    17  'authority_defect'
```

**59 of 115 — 51% — report a terminal cause that is not one.** Both offending values are
`StopEnvelope.stop_class` members. The two vocabularies are disjoint:

```
stop_class      local_implementation_miss | authority_defect | harness_defect
                infrastructure_failure | human_decision_required
terminal_cause  build_truncated | gate_rejected | gate_stalled | interrupted | ...
```

The operator-visible consequence is a wrong answer, not a missing one. Seven runs stopped
by `RequestedExit` — three of them carrying `model_session_idle_timeout`, HIR-0138's
mechanism working exactly as designed — reported `terminal_cause: harness_defect`, sending
an operator to hunt a harness bug that did not exist. The 17 `authority_defect` rows are
HIR-0214's own typed stops: the mechanism built to classify correctly also overwrote the
cause.

## Root cause

Three sites assigned one vocabulary to the other:

```
observability/run_artifacts.py:93   self.terminal_cause = envelope.stop_class   # TypedStop
agents/planner/types.py:68          self.terminal_cause = envelope.stop_class   # PlanGateFailure
application/run_shot.py:245         "terminal_cause": envelope.stop_class       # driver summary
```

They did it because **a `StopEnvelope` carries no terminal cause and the closed vocabulary
had no member for a typed stop.** The set predates typed stops: it names transport,
session, and budget failures plus the generic `process_error` / `requested_exit`. When
ADR-0010's typed stops arrived, each of the three sites needed a value, none existed, and
all three reached for the adjacent field that was already on the envelope.

This is the fourth instance in one day of **one quantity with two derivations where the
wrong derivation is the one that reaches the human** — after a role notation, a turn count,
and a layer order (HIR-0217, HIR-0221). Here `terminal_record` derived the cause correctly
from the exception while `TypedStop` derived it from the stop class, and the exception's
own attribute won, because `terminal_record` reads `getattr(exc, "terminal_cause")` first.

`agents/planner/types.py` and `run_artifacts.terminal_record` also each held their own copy
of the plan-outcome mapping (`stalled -> gate_stalled`, `budget -> plan_budget_exhausted`,
else `gate_rejected`). One was right and one returned a stop class.

## Decision

Make the category error unrepresentable rather than detect it.

- `TypedStop.__init__` takes a **required** `terminal_cause`, validated by
  `require_terminal_cause` against the closed set. The rejection names the accepted set,
  and when the offending value is a stop class it says so: *"that is a
  StopEnvelope.stop_class, which says who owns the stop, not why the run ended"*.
- The vocabulary gains the members the four `TypedStop` sites actually needed —
  `acceptance_rejected`, `materialization_failed`, `authority_amendment_required`,
  `typed_stop_selected` — plus `cancelled_without_intent` and
  `stop_envelope_publication_failure`, which were already being written as literals from
  outside the set. Adding members is what makes the honest value *sayable*; without them
  every site is forced back to a filler.
- `plan_outcome_terminal_cause` becomes one function in the leaf module beside the
  vocabulary, called by both `PlanGateFailure` and `terminal_record`.
- `closed_terminal_cause` keeps its lenient behaviour and gains a docstring saying what it
  is for: normalising *observed* metadata from arbitrary historical records, where unknown
  prose must not enter action identity. It is the wrong function for a value this
  repository authors, because there an unknown value degrades silently to
  `unclassified_terminal_cause` and turns a typo into a plausible-looking record.
  `require_terminal_cause` is the authoring form and fails closed.

## Validation

`src/tests/architecture/test_terminal_cause_vocabulary.py` parses rather than greps,
because a textual sweep for `terminal_cause` matches the line above an assignment as
readily as the assignment itself. It walks every `dict` key, keyword argument, and
attribute assignment named `terminal_cause` in `src/vfx_harness`, and asserts:

- the two vocabularies share no member — if they overlapped, the miswrite could be
  accidentally correct;
- no such value is a stop class, by literal or by `.stop_class` attribute access;
- every literal is in the closed set;
- authoring each of the five stop classes as a cause is refused, and the message names
  both the mistake and the accepted set;
- `terminal_record` derives a legal cause for every exception shape it handles — the AST
  sweep sees assignments, not return values, and that is the other path.

**The AST test found a case I had not.** `run_owner_boundary.py:167` was already writing
`terminal_cause="stop_envelope_publication_failure"`, outside the vocabulary; the
`cancelled_without_intent` return in `terminal_record` was the same. Neither was reachable
by the sweep I would have written by hand.

Reverting `src/vfx_harness` alone, the structural test fails behaviourally and names all
three sites:

```
E  AssertionError: terminal_cause given a stop class:
E    types.py:68 assigns .stop_class to terminal_cause
E    run_shot.py:245 assigns .stop_class to terminal_cause
E    run_artifacts.py:93 assigns .stop_class to terminal_cause
```

and the end-to-end test in `test_run_artifacts.py` fails on the value itself:

```
E  AssertionError: assert 'harness_defect' == 'gate_stalled'
```

Tests of the new API (`require_terminal_cause`) fail on `AttributeError` under revert,
which proves only that the symbol is new. The two above are the discriminators; the rest
are coverage.

## Two existing assertions pinned the defect

The full suite failed twice on the fix, and both failures were tests asserting the
conflation:

```
test_builder_stop_boundary.py  assert 'authority_amendment_required' == 'authority_defect'
test_planning_stop.py          assert 'plan_budget_exhausted'        == 'authority_defect'
```

Both read `assert <exception>.terminal_cause == "authority_defect"` -- the envelope's own
`stop_class`, written into the cause field and then pinned there. Suites are a ratchet and
an assertion is loosened only by a recorded decision; this is that decision, and the
replacements assert more than the originals did: the exact expected cause, that it differs
from `envelope.stop_class`, and that it is a member of `TERMINAL_CAUSES`. Each carries a
comment naming what it used to claim.

Neither test's `stop_class` assertion changed. `envelope.stop_class == "authority_defect"`
is still asserted in both modules, because that part was always right -- it was only ever
wrong as an answer to the other question.

## This fix introduced the defect it prevents

The first draft of `require_terminal_cause` restated the five stop classes locally, so
its rejection could say "that is a StopEnvelope.stop_class". They already existed as
`domain/stop_envelopes.STOP_CLASSES`, one import away, and `observability -> domain` is
the legal direction this module's own neighbours already take.

**A commit whose entire subject is one closed vocabulary being mistaken for another
introduced an unpinned second copy of one of those vocabularies.** It was found by
running the duplicate scanner over this stack, by the author, one message after being
warned about exactly this. Two identical frozensets are inert today; the hazard is
widening, which is the operation duplicates punish -- a sixth stop class would not reach
the copy, and the copy's only consumer is a rejection message, so the failure would be a
refusal that stops naming what it refused. Silent, cosmetic-looking, and precisely the
class this record was written against.

`_STOP_CLASSES` is now `STOP_CLASSES`, imported. The name is kept because it reads
locally at the use site; the values are not restated.

This is the fourth instance in one day of a correction carrying the defect it corrects,
after HIR-0199 installing a worse counter than the one it replaced, `a0404a1` deriving a
rule its own package already listed, and a line-number verification pass that fixed one
citation and broke another. The mechanism is the same each time and it is not care:
attention goes to the thing being fixed, and the fix's own new content gets the attention
a first draft gets rather than the attention a review gets. **It implies a review step
none of us was doing -- a pass specifically for new derivations, separate from whether
the fix is correct** -- and it is the argument for that pass being mechanical, since here
it defeated an author who had spent the day on this class and had just been warned.

## What this does not fix

`stop_class` is still `harness_defect` for the seven `RequestedExit` stops, because that is
derived from the envelope's action and the unclassified envelope's only action is
`route_engineering`. Publishing three envelopes varying only the cause shows it:

```
passed='requested_exit'             stop_class='harness_defect'
passed='model_session_idle_timeout' stop_class='harness_defect'
passed='unaccepted_prior'           stop_class='harness_defect'
```

Five of the six handlers in `agents/builder/cli.py:270-310` still raise a bare
`RequestedExit` with no envelope, so the boundary is correct that no typed stop was
published. Classifying those five is a separate change with a real design question in it:
`UnpassedPrior` is a correct refusal rather than a defect, and AGENTS.md forbids
`resume_checkpointed_session` for `BuildTruncated`, so no action in the closed set fits it.
This change removes the *wrong* answer in `terminal_cause`; it does not yet produce the
right `stop_class`.
