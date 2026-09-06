---
id: HIR-0240
title: A verdict the harness emits is one the receipt can record
status: proposed
introduced_in: unreleased
date: 2026-09-06
failure_class: unrepresentable_diagnostic_verdict
mechanism: shared_mechanical_decider_vocabulary
adr: null
---

# A verdict the harness emits is one the receipt can record

## Observed failure

`caesar_curia` run `20260906T002035Z-f1ad1d` built layer 1, replayed it, correctly diagnosed
that three of its judge frames could not be settled, and then died minting the receipt that
exists to record exactly that:

    canonical per-frame: f1:5.0 OK  f121:5.0 OK  f301:5.0 OK
                         f541:1.0 X  f841:1.0 X  f1081:1.0 X
    canonical candidate has uncovered measurable defects — recording CONTRACT_GAP
    ValueError  domain/layer_evaluation_receipts.py:342
      "layer evaluation canonical[3].verdict executable claim result must be mechanically decided"

The run terminalized as an unclassified boundary with a `route_engineering` envelope the
controller correctly refused to dispatch. **The boundary whose job is to record the finding
refused to record it**, so a correct diagnosis became a crash and the shot could not proceed.

Exit 1. $2.39 of builder spend on that layer, and the shot blocked for the rest of the session.

## Root cause

`agents/builder/verdicts.py` emits four verdicts for a group with no
`qualified_qualitative_required` claim. `domain/layer_evaluation_receipts.py` admitted one.

    verdicts.py:159  unit_executable_evidence              admitted
    verdicts.py:341  look_without_image_domain             refused
    verdicts.py:396  uncovered_judge_frame                 refused
    verdicts.py:428  lookless_requires_executable_claims   refused   <- caesar died here

The three refused values are *structural refusals*: each names which authority fact stopped a
frame being settled, and each is derived from the plan and the claims. All are mechanical; none
is a model judgment. The guard's principle — a non-qualitative verdict must be decided by the
harness, never by a model — is right. Its allowlist was one element wide.

A second assertion two lines below would have fired on all three anyway:

    if passed != (point.deterministic_status == "passed"): raise

A contract-gap verdict is `pass: False` while the frame has no deterministic status to agree
with, because it was never settled. So the receipt had no *representation* for a mechanically
decided refusal, only for a mechanically decided pass or fail. Widening the allowlist alone
would have moved the crash two lines.

"What may decide a verdict" existed as an emitted set in `agents/` and a one-element allowlist
in `domain/`, with nothing pinning them together — the same one-quantity-two-derivations shape
recorded across HIR-0217, HIR-0221, HIR-0227 and HIR-0228.

## Decision criteria

- The guard must keep refusing model-decided verdicts on non-qualitative groups; that is the
  invariant, and widening must not erode it.
- A contract gap must be checked against an invariant that fits it, not one that assumes the
  frame was decided.
- One definition, read by emitter and validator, rather than a second agreeing copy.
- The next structural refusal must not be addable on one side alone.

## General mechanism

`domain/verdict_deciders.py`, a leaf with no imports:

    EXECUTABLE_DECIDER          = "unit_executable_evidence"
    CONTRACT_GAP_DECIDERS       = {look_without_image_domain, uncovered_judge_frame,
                                   lookless_requires_executable_claims}
    MECHANICAL_VERDICT_DECIDERS = {EXECUTABLE_DECIDER} | CONTRACT_GAP_DECIDERS
    JUDGMENT_ONLY_DECIDERS      = {provisional_requirement_contract_gap, no_optical_signal}

The receipt admits any mechanical decider and names the legal set when it refuses. A
contract-gap verdict is checked against "an unsettled frame cannot pass"; every other verdict
keeps the deterministic-status agreement unchanged. `verdicts.py` reads `EXECUTABLE_DECIDER`
rather than restating the literal.

`JUDGMENT_ONLY_DECIDERS` is deliberately excluded and the exclusion is load-bearing. Those
values reach a group *because* a judgment debt exists there, and a judgment debt mints
`qualified_qualitative_required`, so the group is qualitative and this guard never runs.
Admitting them would classify a critic-derived verdict as mechanically decided, which is the
one thing the guard exists to prevent.

Evidence both ways, from two shots:

    hansa_silk_road  lfc-34f433df.evaluation.json  recorded provisional_requirement_contract_gap
                     accepted, with state/judgment-debts.jsonl holding rows
    caesar_curia     layer 1 crashed on the mechanical branch, no debts file at all

## Rejected patch-level alternatives

- **Widen the allowlist only.** The deterministic-status assertion fires two lines later on
  every gap verdict. Moves the crash, does not remove it.
- **Add `light`/`shading`-style values by enumerating what `verdicts.py` can emit.** Enumerating
  the emitter without asking which branch each value can reach admits
  `provisional_requirement_contract_gap`, and with it a critic-derived verdict.
- **Skip the guard when `contract_gap` is set.** Lets any decider through on a non-qualitative
  group, including a model's, by setting one flag.
- **Merge the two vocabularies into one set.** They are not the same set; the exclusion carries
  meaning and needed a name.

## Validation

    src/tests/unit/test_mechanical_verdict_deciders.py   6 passed
      3 of 6 fail against the unmodified production tree (stashed src, kept the test)
    ruff check src                                        All checks passed
    full suite                                            2815 passed in 2120.87s (0:35:20)

The general test parses `verdicts.py` for emitted `decided_by` literals and fails on any value
that is neither mechanical nor judgment-only, so a new structural refusal cannot be added on one
side alone.

**Verified in action.** `caesar_curia` run `20260906T042924Z-c76d08`, the same layer that
crashed, minted its receipt:

    lfc-c9083fac.evaluation.json
      3x unit_executable_evidence     f1, f121, f301
      3x uncovered_judge_frame        f541, f841, f1081

Three contract gaps recorded where the receipt previously raised. Note `uncovered_judge_frame`
was added on reasoning rather than observation and fired for real on the first execution.

## Release and rollback

Unreleased. No schema, digest or authority change: the receipt admits more verdicts and stores
the same fields. Rollback is reverting the commit; previously minted receipts remain readable
because no recorded shape changed.

## Remaining limitations

- **This does not make the layer pass, and it is not the cause.** caesar layer 1 is unsatisfiable
  by construction — layer judge `[1,121,301,541,841,1081]`, union of unit required-claim moments
  `[1,121,301]`. HIR-0238 refuses that authority at the gate; this record only ensures the
  diagnosis is recordable when it is reached.
- **The failure moved one boundary later rather than disappearing.** The same rerun then raised
  `ValueError: executable-only canonical has no typed evidence for f541` at
  `orchestration/revalidation.py:630`. The receipt can now record an uncovered frame; the
  revalidation path still cannot proceed without evidence at one. Untouched here.
- **The behavioural invariant is not yet tested.** That a gap verdict with `pass: True`
  raises is asserted nowhere: minting a receipt needs a full shot layout, and
  `make_layer_finalization_receipt` refuses a non-empty canonical without sealed outcome
  sources and then wants the tree. An earlier test claimed this by asserting on
  `inspect.getsource` — the guard's identifier and message appearing in the file — which
  passes if the guard is deleted with its message left in a comment, and fails on a pure
  rename. That was a description standing in for the artifact, in the test whose name was
  the invariant, and it was removed rather than left to imply coverage. The AST test that
  pins emitter against receipt is real and stands; the behavioural one is owed.
- **Known unproven boundary.** If `provisional_requirement_contract_gap` ever reaches a group
  with no minted qualitative claim, the receipt raises exactly as it did before. That path could
  not be constructed from `_provisional_composition_contract_gap`, which reads
  `provisional_requirement_ids` and `provisional_debt_ids` off the active unit, and was not
  proved impossible either. Recorded beside the constant rather than only here, because the
  person who hits it is reading that file.
