---
id: HIR-0248
title: A judgment that could not be made stops differently from one that failed
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_build_boundary_discarded_a_complete_receipt_and_exited_with_a_number
mechanism: the_receipt_compiles_its_own_typed_stop_when_and_only_when_a_prerequisite_is_missing
adr: null
---

# A judgment that could not be made stops differently from one that failed

## Observed failure

HIR-0247 made the publication refusal *say* what failed. This is what still happened to
the sentence it wrote. `hansa_silk_road`, run `20260906T095020Z-3ba2cc`:

```
harness_defect: The 'build' boundary returned without typed stop authority;
it raised RequestedExit: layer 2 has no complete receipt-bound publication: …
```

`cli.py` caught `LayerVerdictFailed` and raised `RequestedExit(9, str(e))`. No envelope,
no terminal cause, no owner. The run terminalized as an unclassified boundary — the same
`harness_defect` route AGENTS.md reserves for a boundary that could not classify itself —
for a failure whose receipt names the frames, the decider, the debt and the requirement.

The receipt was two attribute accesses away, on an exception the handler was holding.

## Cause

The earliest owning decision is not in `cli.py`. It is that `LayerPublicationConflict` is
one exception for every way a publication can be incomplete: a missing receipt, a receipt
belonging to another layer, a script mismatch, and a receipt that finalized and did not
pass. Those are not the same kind of fact. Only the last one carries a complete record of
its own cause, and flattening it into the others is what left the boundary with a string.

Given one exception type, `cli.py`'s handler was correct: it had prose and produced prose.

## The distinction the mechanism draws

A stop must not claim more authority than its evidence supports, and these two failures
support very different amounts:

- **The judgment could not be made.** `no_optical_signal`, `uncovered_judge_frame`,
  `provisional_requirement_contract_gap` and their siblings mean the plate or the
  authority could not settle the frame at all. No amount of rebuilding supplies the
  prerequisite. There are exactly three plans — move the judgment to a layer that can
  produce it, add the missing provider before this layer, withdraw it — and nothing in the
  receipt picks one. That is a human decision on authority, and it stops as one.
- **The judgment was made and did not pass.** A failing `critic` or
  `unit_executable_evidence` row is the layer failing on its merits. Compiling a
  dispatchable transaction for it would manufacture the `local_implementation_miss`
  authority AGENTS.md says no current producer proves. It keeps the plain exit, and the
  absence of an envelope is the honest answer.

So the mechanism refuses to compile a stop in the second case. A stop for every failed
finalization would have been easier to write and would have been wrong half the time.

## Mechanism

`LayerFinalizationNotPassed(LayerPublicationConflict)` carries the layer id, the parsed
receipt, and the durable state path it was read from. `agents/builder/finalization_stops`
compiles a `StopEnvelope` from it, or returns `None`:

- `unresolved_prerequisites()` selects the failing canonical rows whose `decided_by` is in
  `EVIDENCE_UNAVAILABLE_DECIDERS`, and attaches the evaluation group that owns each — its
  index, debt, and requirement ids.
- No such rows, or a passed receipt, returns `None` and the boundary exits plainly with
  `terminal_cause="acceptance_rejected"`.
- Otherwise the receipt is **re-read from `state/work-units/layer_<id>.json`** and compared
  with what was passed in. A disagreement raises rather than publishing an envelope
  compiled from an in-memory summary somebody else assembled. The published evidence
  document is read back and compared before its digest is bound.
- The envelope is `stop_class="human_decision_required"`, `stage="composition"`, with an
  `EscalateQuestionTarget` / `HumanDecisionCommitted` pair over the three answer ids, and
  `owner_scope_ids` naming the layer, the requirement, and the debt.

On hansa's real `state/work-units/layer_2.json`:

```
stop_class : human_decision_required | stage: composition
owner      : ('debt:jd-06fa28eac2ac05f79', 'layer:2', 'requirement:R50')
next_action: Decide how layer 2 obtains the evidence it judges at f1, f51, f151: move the
             judgment to a layer that can produce it, add the missing provider before this
             layer, or withdraw it. Rebuilding cannot supply a prerequisite the layer has
             no way to produce.
```

No checkpoint digest is bound: `stop_envelopes` requires a unit identity beside one and
this stop is layer-scoped. The receipt's digest travels as the attempt and
authoritative-before digests instead.

## Validation

`src/tests/unit/test_finalization_failure_stop.py`, ten tests, pinning both directions:
unavailable evidence compiles the stop; a negative `critic` verdict compiles none; failed
`unit_executable_evidence` compiles none; a contract gap does compile one; a mixed receipt
stops on the prerequisite half only; a passed receipt compiles none; a receipt that changed
under the compiler is refused; the published evidence reads back at its bound digest; and
the boundary itself returns a `TypedStop` in the first case and a plain `RequestedExit`
with `terminal_cause="acceptance_rejected"` in the second.

The two boundary tests are the ones that matter — they assert the *absence* of an envelope
as hard as its presence.

Verified against production artifacts rather than fixtures alone: all four real terminal
receipts across the three shots round-trip `as_dict()` byte-identically against their
stored bytes, which is the property the source-backed read-back depends on (HIR-0208).

## Three corrections from the shots

Reviewed with the three shot sessions before landing. Each found something the first draft
had wrong.

**hansa_silk_road — the envelope was per-layer while the split was per-row.** A layer can
fail one group on a plate it could not produce and another on a judgment that was made;
hansa's layer 2 had groups 0 and 1 pass and group 2 fail `no_optical_signal` on the same
finalization. The first draft named only the prerequisite, so `next_action` read as though
resolving it accepted the layer. The envelope now carries `judged_failures()` alongside,
and says so: *"This is not the layer's only blocker: N further judgment(s) at fX were made
on evidence that existed and did not pass."*

hansa also strengthened the argument for `human_decision_required` past the one this record
opened with. It is not merely that nothing in the receipt picks an answer — it is that
**none of the answers is reachable by any transaction the controller can dispatch.**
`activates_at` is harness-derived from the selected DAG, so a rematerialization cannot move
a judgment; adding a provider and withdrawing a requirement are both global publication.
A rematerialization would face the identical constraints and fail the same way. hansa's
attempts 9 and 10 were exactly that: attempt 10 rematerialized this layer, produced a
better design, and failed on the same predicate. **Derivability of the owner is not the
test; reachability of the remedy is.**

**room_1046_opening — a remedy set that does not contain the remedy.** The first draft
offered one fixed triple for every decider. For `uncovered_judge_frame` the actual repair
is "author a required claim whose moments reach frame f, on a unit that judges f", which is
none of move / add-provider / withdraw. An operator handed those three has to reject all of
them and describe a fourth, and that mismatch is the tell: when the remedy set misses, the
classification is wrong, not the wording. Answers are now derived per decider —
`CONTRACT_GAP_DECIDERS` get claim-authoring and vocabulary-gap escalation, everything else
gets the provider triple, and a mixed receipt gets the union.

**caesar_curia — `gate_rejected` over `acceptance_rejected`.** Measured across 222 run
summaries in the three shots: `acceptance_rejected` has exactly one producer
(`agents/acceptance.py:636`) and appears zero times; `gate_rejected` is already in
`TERMINAL_CAUSES` and has no producer anywhere. Reusing `acceptance_rejected` would have
made its first real appearance mean layer finalization rather than shot acceptance, with
no prior usage to anchor the reader. An unused member that is semantically closer costs
nothing to adopt. caesar also confirmed from `run_controller.py:728` that a
`human_decision_required` stop publishes no `PriorDispatchAttempt` and therefore burns no
cause fingerprint.

## What this does not fix, and why it stops here

`run_artifacts.publish_exception_stop` gives any exception with no `stop_envelope`
attribute the unclassified `harness_defect` envelope. So the second branch — a layer
judged and failed on its merits — still terminalizes as an engineering handoff. Its
`terminal_cause` is now `acceptance_rejected` rather than the default `requested_exit`,
which is more honest, but the routing is unchanged.

That is a real remaining defect and it is deliberately not fixed here, because every fix
for it is an authority decision larger than this change:

- Emitting `local_implementation_miss` would make this the first producer of a class
  AGENTS.md states no producer proves, and would grant local retry authority from a
  generic builder failure — the exact inference AGENTS.md forbids.
- Emitting `human_decision_required` for it too would collapse the distinction this record
  exists to draw into the question text, and would assert that a person must decide when
  what the layer may actually need is more builder rounds.
- Minting a stop class or terminal-cause member for it changes a closed vocabulary for one
  call site, which HIR-0227 records as how that vocabulary was corrupted before.

caesar_curia supplied the reason no member fits, and it is structural rather than a gap
that could be filled by choosing better. **The five stop classes are a fault-owner
taxonomy** -- the builder can retry, the plan is wrong, the harness is broken, the
environment is broken, a person must decide. Every one answers *whose defect is this*.
"Measured, did not meet its bar, no next action" is not an answer to that question: it is
an outcome, and the system working correctly. A run ending because the work was not good
enough is not an incident and has no owner. A sixth member would be the first that is not
a fault owner, and would quietly change what the vocabulary means.

caesar also proposed a fourth option worth recording because it is the only one that leaves
every closed set alone: fix the wrapper instead, so an exception carrying a verdict rather
than a defect can say "terminalize, exit 9, no envelope". **Checked, and it is larger than
it looks.** `RunLayout`'s terminal reader requires `stop_envelope_digest` on any `failed`
status and raises without it, which is the run/v2 contract AGENTS.md states: an unaccepted
terminal run selects `reports/stop-envelope.json` by exact digest, and that envelope is
machine dispatch authority. So "no envelope" is not an unimplemented case at the wrapper --
it is unrepresentable by design, and making it representable is an ADR that changes what a
terminal run guarantees to every reader. Recorded, not attempted.

That leaves all four options large, which is why this escalates rather than picks. The
prerequisite half is evidence-backed and lands; the judged-and-failed half needs a decision
about what a terminal run must publish, and a stop compiled without that decision would be
a shrug in an envelope.

One caveat on the evidence, at caesar's insistence and marked as they asked: **the driver
who would receive this stop has not received it.** Their nearest instance was an exit-7
`hypothesis_falsified`, where the typed falsification already named its contract ids and
fault owner and nothing more was needed -- a falsification, not a verdict failing on
adequate evidence. One near-miss is not evidence for a vocabulary change, and their stated
want (whether more builder rounds would plausibly help) is a budget decision belonging to
the operator, not something the harness can answer.

The second open question is room_1046's, and it is stronger than a preference. It argues
`uncovered_judge_frame` belongs on the dispatchable authority route, not the human one:
the owner is a property of one layer's own unit set against its own judge list, both in the
same JIT view; the repair has a demonstrated resolution — caesar layer 1, judge
`[1,121,301,541,841,1081]` against units covering `[1,121,301]`, repaired by a
rematerialization for **$1.07**; and the failure mode a human route would guard against
(a frame no registry metric can express) already degrades correctly through
`escalate_vocabulary_gap` under HIR-0202/0218. That is a routing change requiring a finding
payload and a controller dispatch path, so it is not folded in here. Deriving the answers
per decider removes the acute harm — the operator is no longer handed three options that
all miss — without pre-empting the routing decision.
