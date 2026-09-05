---
id: HIR-0226
title: The boundary names the exception it swallowed
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: forty_six_unclassified_boundaries_with_thirty_two_causes_produced_four_sentences
mechanism: the_audit_enters_the_reading_order_and_the_envelope_carries_the_label_it_records
adr: null
---

# The boundary names the exception it swallowed

## Observed failure

Every `unclassified-boundary-audit.json` on this machine, read back in full — 46 records,
2026-09-02 to 2026-09-05, `room_1046_opening` 24, `caesar_curia` 11, `hansa_silk_road` 11:

```
records 46   distinct (type, message) 32   distinct exception types 11
```

The audit files record the cause faithfully. Their envelopes did not. All 46
`reports/stop-envelope.json` `found` values, counted:

```
 35  "The 'build' boundary returned without typed stop authority."
  6  "The 'plan' boundary returned without typed stop authority."
  3  "The 'run' boundary returned without typed stop authority."
  2  "The 'layer-2-plan-gate' boundary returned without typed stop authority."
```

Thirty-two distinct causes, four sentences, differing only by boundary name. And `found`
is not an internal field: `detail` is composed from it at three separate call sites
(`run_artifacts.TypedStop.__init__`, `application/run_shot.py:240`,
`agents/planner/types.py:59`), so it is what `status.json` and `reports/summary.json`
carry. Thirty-six of the 46 `status.json` details are one identical string.

What was lost, from those same audits:

```
build   LayerFinalizationConflict   layer 2 already has an active finalization claim         (x5)
build   UnitAttemptConflict         cannot claim work unit <id> for planning from state '…';
                                    legal states are [...]                                    (x7)
plan    BrokenPipeError             [Errno 32] Broken pipe                                    (x8)
build   RequestedExit               BUILD TRUNCATED — builder emitted no SDK event for 360s…  (x7)
build   RequestedExit               CHAIN BROKEN — cam_lens.py no longer composes onto the…
build   RequestedExit               UNACCEPTED PRIOR — refusing to build layer 2 on…
build   ArtifactExecutionPolicyError artifact import denied: itertools / collections          (x2)
build   ValueError                  selected authority capsules do not preserve the stable
                                    topological layer order
build   BlenderError                Cannot read file "…/snapshot_2@exterior_massing_r1.blend"
layer-2-plan-gate RuntimeError      child boundary returned without a selected typed stop
                                    envelope                                                  (x2)
```

`LayerFinalizationConflict` names a condition with a documented operator transaction
(`vfx finalizations release`). `UnitAttemptConflict` prints its own legal state list. The
`RequestedExit` rows are the sharpest: the harness had already *authored* a complete
operator sentence — `BUILD TRUNCATED — builder emitted no SDK event for 360s` — and the
boundary replaced it with "a boundary returned".

## Root cause

`_unclassified_stop_envelope` composed constant prose:

```python
found=f"The {command!r} boundary returned without typed stop authority.",
next_action="Route the boundary and exact attempt evidence to engineering.",
```

The exception was in scope throughout. Ninety lines earlier the same function writes it to
`unclassified-boundary-audit.json` as `exception_type` and `exception_message`. The
envelope discarded the value it had already recorded, one file away.

The originating step is HIR-0214's reasoning, and it was half right. HIR-0214 separated
**identity** from **authority** — a constant `cause_fingerprint` made every unclassified
boundary in every shot share one finding id, so the controller's already-dispatched check
suppressed the second real defect as a repeat of the first. It fixed identity by admitting
the closed terminal cause, the exception type, and the operator flag into
`classification_digest`, and it deliberately kept free-form prose out of that digest.

That was correct for identity, and it was silently generalised to the operator-facing
prose, which is neither identity nor authority. The stop authorizes nothing either way;
`route_engineering` remains its only transaction and there is no controller adapter for
it. Refusing to *say* what happened bought no safety — it only meant that the one field an
operator reads was the one field the exception could not reach.

## Decision

`found` carries the exception label; `next_action` points at the verbatim record and still
names one action.

```python
found=(
    f"The {command!r} boundary returned without typed stop authority; it raised "
    f"{_exception_label(exc)}"
),
next_action=(
    "Read that exception first -- it is the cause, and several of these name their own "
    f"owning boundary or recovery transaction. {audit_locator} holds it verbatim. Then "
    "route the boundary and exact attempt evidence to engineering."
),
```

The reading order in AGENTS.md gains `reports/unclassified-boundary-audit.json` for this
stop, ahead of the rest — that is the load-bearing half, for the reason in the next section.

`_exception_label` is one function used for both the audit and the prose, so the envelope
cannot describe an exception differently from the file beside it. It renders
`module.QualName: message`, whitespace-normalised and bounded at 240 characters with an
ellipsis.

Three properties this deliberately does **not** change:

- **Identity does not move.** `classification_digest` is built from `normalized_facts`,
  which contains the boundary, invariant, closed terminal cause, exception *type*, and
  operator flag — not `found`. Two `LayerFinalizationConflict`s with different layer
  numbers still share a fingerprint and a finding id, which is what keeps the controller's
  already-dispatched check working.
- **Authority does not move.** The stop remains `harness_defect`, stage `infrastructure`,
  `retryable=False`, with `route_engineering` as its only proposed transaction. The prose
  reorders the reading; it grants nothing. The earlier draft of `next_action` said "route
  to engineering only when the exception names no owner" — that implied a second path and
  was rewritten, because an envelope must not create authority in prose.
- **The audit is still the record.** The label is bounded; the audit is not.

## Validation

`src/tests/unit/test_run_artifacts.py`, eight tests, all driven through
`publish_exception_stop` rather than the helper — so they discriminate behaviour, not the
existence of a symbol:

- the four representative real causes, parametrised, each asserted to survive into
  `envelope.found` by type *and* message;
- the `detail` composition asserted end to end: `f"{stop_class}: {found} {next_action}"`
  contains `itertools`, and the audit's `exception_message` equals what the prose carries;
- a multi-line 800-character message: whitespace normalised, ellipsis-bounded, and the
  composed `detail` still inside the 1000-character summary/status cap, so the cause is not
  truncated away by the consumer that carries it;
- an exception with no message still names its type;
- **the guard**: two `LayerFinalizationConflict`s with different messages keep one
  `cause_fingerprint` and one finding id while their `found` differs. This passes with and
  without the mechanism, deliberately — it asserts HIR-0214's identity did not move.

Reverting `run_artifacts.py` alone fails seven of the eight on assertions showing the
pre-fix sentence verbatim:

```
E  assert 'artifact import denied: itertools' in
     "The 'build' boundary returned without typed stop authority."
```

The guard is the one that stays green.

**Replay against the real corpus.** All 32 distinct `(type, message)` pairs from the 46
audits, pushed through the fixed boundary:

```
distinct causes replayed: 32
distinct details:         32 of 32     (was 4 of 46)
max detail length:        633          (cap 1000, not truncated)
```

## The record was faithful; the pointer was missing

Both shot drivers who investigated one of these diagnosed it the expensive way. The
`hansa_silk_road` driver traced `selected authority capsules do not preserve the stable
topological layer order` to `capabilities.py:126` from a console traceback, and resolved
`layer 1 already has an active finalization claim` from the traceback plus AGENTS.md's
release procedure. Runs `20260905T100338Z-5d62f` and `20260905T111005Z-34c46` each carry the
exact answer in `reports/unclassified-boundary-audit.json`. That driver never opened either
file, and said why: it did not know the file existed, and nothing in the failure output
pointed at it.

That reframes the defect. This is not a message-quality fix, it is a **reachability** fix,
and it changes which half of the change is load-bearing. The prose improvement alone would
not have helped that driver, because the pointer it adds sits in `next_action` — and 46
envelopes carrying an identical `next_action` are exactly the training set for reading that
field as boilerplate. The same driver reports skipping it twice on their own runs.

So the durable half is the **reading order**. `reports/unclassified-boundary-audit.json` was
absent from AGENTS.md's documented order (`runs/latest.json` -> `manifest.json` ->
`status.json` -> `reports/summary.json` -> `artifacts.json`), so an operator following the
documented path exactly was routed past it every time. This change adds it, for the
`harness_defect`-naming-an-unclassified-boundary case, ahead of the rest. The message
improvement is what makes the audit worth reaching; the reading order is what makes it
reached.

## What this does not fix

Two facts found while validating this, both left open and neither addressed here.

**A deliberate exit is still classified as a defect.** Seven of the 46 are `RequestedExit` —
the harness's own exit, carrying a complete operator sentence and, in three cases,
`model_session_idle_timeout`, which is HIR-0138's mechanism working exactly as designed.
`agents/builder/cli.py:270-310` has six handlers and HIR-0214 converted exactly one
(`BuildAuthorityDefect` -> `TypedStop`); the other five raise a bare `RequestedExit`, which
carries no `stop_envelope`. So the boundary is *right* that no typed stop was published —
this is not mislabelling, and HIR-0226 makes those seven legible without making them
correct.

Passing `RequestedExit`'s existing `terminal_cause` argument does not fix it either.
Publishing three envelopes that vary only that argument:

```
passed='requested_exit'             -> closed='requested_exit'              stop_class='harness_defect'
passed='model_session_idle_timeout' -> closed='model_session_idle_timeout'  stop_class='harness_defect'
passed='unaccepted_prior'           -> closed='unclassified_terminal_cause' stop_class='harness_defect'
```

`stop_class` cannot move: `domain/stop_envelopes.py:64` derives class from action and the
unclassified envelope's only action is `route_engineering`, which maps to `harness_defect` by
definition. What the argument *does* change is the fingerprint, which is worth doing on
HIR-0214's grounds alone — four distinct conditions currently collapse to one cause id.

**An unknown cause degrades silently.** `closed_terminal_cause` maps any value outside
`_TERMINAL_CAUSES` to `'unclassified_terminal_cause'` with no error. That is deliberate for
arbitrary legacy record metadata, and wrong for a code-authored call site, where it turns a
typo into a plausible-looking record. The closed set has no member for INCOMPLETE CHAIN,
LAYER VERDICT, CHAIN BROKEN, or UNACCEPTED PRIOR; the default those four sites take is
`requested_exit`, which `OPERATOR_CAUSES` treats as operator-initiated — affirmatively wrong
for CHAIN BROKEN, which no operator requested.

## Consequence

This closes the half HIR-0225 left open. Its last paragraph reads: *"The boundary's
`detail` still reads 'The 'build' boundary returned without typed stop authority' — true
about the boundary, silent about `import itertools`. That remains open as the
stage-boundary half of HIR-0224."*

It does not reduce the number of unclassified boundaries — 32 distinct causes reaching this
path is itself the finding, and each is its own defect to classify at its own owning
boundary. It makes them individually legible, which is the precondition for classifying
them.
