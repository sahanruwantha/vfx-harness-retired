---
id: HIR-0020
title: Guessing is measured, not autopsied
status: proposed
introduced_in: unreleased
date: 2026-08-25
failure_class: instrument_gaps_located_only_by_paid_run_autopsy
mechanism: typed_decision_events_with_deterministic_hotspot_ranking
adr: null
---

# Guessing is measured, not autopsied

## Observed failure

Every instrument this harness has gained was discovered the same way: a paid production run
failed loudly, a human read its artifacts, and only then did the missing tool surface.

- Run `20260823T154920Z-6d4022` (HIR-0014): 45 minutes and ~$12.9 produced six independent
  evidence defects, "none of which any test or gate detected" — found by manual artifact review
  after the failure.
- Runs `20260824T103842Z-afec73` and `20260824T153427Z-91b7c1` (HIR-0015/0017): a materializer
  burned 8 validator rounds before padding; an escalation tool was present and never used; repair
  sessions edited blind. Each signature was sitting in transcripts and journals the whole time,
  aggregated by nothing.
- ADR-0003: `vfx plan --until-clean` could not converge across two disagreeing halation
  implementations — $19.27 for the dirty run against $9.86 clean. The recurring-finding
  signature existed across rounds in run data; no surface showed it.
- 2026-08-25 (HIR-0018/0019 day): seven harness defects located in one day, every one by a
  human reading live run logs mid-flight. Among them: run `20260825T093425Z-17581c` burned
  four repairs across two builds on one structurally-unmovable row (a frameless response
  sweep measuring the silent f1 default, where the subject was occluded), and run
  `20260825T120608Z`-era's materializer walked an undocumented six-field qualification
  schema one error per write to max-turns. Both are exactly the loop signatures the report
  below ranks first — pre-filled, had it existed.

The harness already records the raw signals — `runlog` rounds, `costlog` role/phase spans, gate
findings, escalation records under `state/plan-escalations/`, revalidation verdicts, transcripts
— but each lives in a role-specific surface no mechanism joins. The agent guide now binds "a
recurring guess is a missing tool"; the harness has no instrument that finds recurring guesses.
Discovery is by autopsy: it costs a failed paid run per lesson, arrives only after failure, and
is blind to quiet guessing — a padded contract that passes is found only if someone audits a
passing run, which HIR-0014 shows happens only once something else has already failed.

## Root cause

Decision outcomes are recorded for authority — accept, seal, publish — and never as observations
of decision quality. No shared typed record carries decision-point identity, the options
presented, the evidence consulted, the outcome, and any later contradiction. Questions such as
"which decision point loops most", "where is abstention available but never taken", and "which
verdicts does stronger authority later overturn" are therefore unanswerable without re-reading
transcripts, so the improvement lifecycle's capture step triggers only on loud failure.
Classification: observability gap.

## Decision criteria

- **Non-authoritative by construction.** The meter informs the improvement lifecycle; it must
  never gate acceptance. A gating meter is a guard on unrepaired structure — the patch shape the
  fix policy forbids — and invites gaming the signal. Enforced structurally, not by promise.
- **Non-fatal by construction.** The writer can never raise into a run: a telemetry writer able
  to kill a paid build is authority by accident. Events that fail the contract are quarantined
  (appended tagged `unreadable`), never crashed on and never dropped; strict rejection lives
  only on the read path.
- **Deterministic and free.** Derived entirely from artifacts already on disk; no model or
  Blender spend; identical run sets yield identical reports.
- **General.** Decision points are stable machine-readable identifiers owned by core; no shot
  names, frames, or scene vocabulary in core.
- **Its own absences are visible.** Unreadable or schema-unknown events are counted and named,
  never silently dropped; runs predating instrumentation report `not_instrumented`, not zero —
  HIR-0014's "silence reads as consent" rule applied to the meter itself.
- **Bounded.** Emission is one call at seams that already record the outcome; no control-flow
  change.

## General mechanism

1. **Typed decision events.** A versioned `DecisionEvent` contract in `domain/` (joining the
   existing decision vocabulary in `domain/unit_outcomes.py`): `decision_point` from a closed
   registry (`builder.validator_round`, `plan_gate.terminal_verdict`, `repair.route_selection`,
   `judge.frame_verdict`, `escalation.offer`, `revalidation.verdict`, …), role, layer/unit
   identity, `options` (`enumerated(n)` or `free_form`), `evidence_refs` joining provenance,
   `outcome` from a closed set (`accepted | rejected | retried | abstained | escalated`), round
   index, and the `costlog` span it occurred inside. Unknown points, outcomes, or versions are
   rejected at parse ON THE READ PATH with the accepted set enumerated; the write path
   quarantines instead (below).
2. **Run-scoped emission.** `observability/decisions.py` appends events to a
   `RunLayout`-declared `logs/decisions.jsonl` with the same atomic discipline as the cost log.
   The writer NEVER raises: an event failing the contract is still appended, tagged
   `unreadable`, and surfaces in the report as a registration defect — quarantine, not crash,
   because emission must be incapable of failing the run it observes. Emitting seams are
   exactly where outcomes are already recorded: builder validator rounds
   (`observability/runlog.py` rounds), plan-gate verdicts (`orchestration/plan_authority.py`),
   escalation offer and use (`orchestration/escalate.py`, the vocabulary-gap tool in
   `agents/plan_tools.py`), repair route selection and revalidation verdicts
   (`orchestration/revalidation.py`), judge verdicts at the evidence boundary.
3. **Overturn back-references.** When stronger authority contradicts a recorded decision, the
   contradicting record carries the contradicted event ids — "overturned" becomes a join, not
   an inference. The join key is the hard part, so the first release wires only the two
   contradictions the harness already computes with the key in hand: revalidation refusing a
   previously accepted checkpoint, and the repair loop's monotonicity revert of an inert
   repair. Empty-scene replay divergence is deferred until those two prove the join. The
   current shot holds a live instance: the published unit-1 artifact accepted under
   afec73-era instruments now fails revalidation.
4. **Deterministic hotspot report.** A read-only `application/` use case, surfaced as
   `vfx decisions report` through an ordinary `run_artifacts.invocation`, scans selected runs and
   computes per decision point: volume, retry/loop rate, abstention offered versus taken,
   overturn rate, free-form share, unreadable-event count, and attributed cost — the cost
   column labelled APPROXIMATE in the report itself, because costlog spans nest and overlap
   and the figure is an estimate, never quotable as exact. It writes a
   ranked hotspot table under the invoking run's `reports/`, each row carrying the run ids,
   event ids, and cost that constitute a ready-made HIR "Observed failure" capture. Registered
   decision points that emitted nothing are listed too; coverage is part of the report.
5. **Structural non-authority.** An architecture test in the
   `tests/architecture/test_package_boundaries.py` pattern forbids gate, seal, and acceptance
   modules from importing the aggregation module, so telemetry cannot gate by construction.

## Rejected patch-level alternatives

- **Prompting roles to self-report guessing.** Self-certification; HIR-0017 showed padding is
  the rational behavior inside a poor instrument set — the guesser cannot see the guess.
- **Periodic manual transcript audits.** The status quo: an expert pass per paid run, occurring
  only after loud failure, with non-deterministic yield.
- **Thresholds that gate.** Blocking a build on a high loop rate converts the instrument into a
  guard on unrepaired structure and makes the signal worth gaming; forbidden by the fix policy
  and by the first decision criterion.
- **Ad-hoc grep of transcripts per investigation.** Inference from display strings — the exact
  shape HIR-0014 eliminated — and it dies as an untracked probe.
- **Enlarging judge or critic prompts to watch for guessing.** Model opinion cannot be the meter
  for model guessing; nothing self-certifies.

## Validation

Required before acceptance; none of it has run — this record is a proposal.

- **Contract:** parse/round-trip tests; unknown decision point, outcome, or schema version fails
  closed naming the accepted set on the read path.
- **Non-fatal writer:** a deliberately malformed event neither raises nor vanishes — the writer
  appends it tagged `unreadable` and the report counts it as a registration defect.
- **Emission coverage:** the hermetic lifecycle fixture
  (`tests/integration/test_lifecycle_fixture.py`) extended to assert every registered decision
  point on the composed path emits; a registered seam that stays silent fails the fixture.
- **Signature reproduction:** aggregation over preserved artifacts must rank a documented
  hotspot first — `91b7c1`'s 8-round validator walk (escalation offered once, taken zero
  times) or `17581c`'s four-repair single-row burn — and the afec73-era acceptance must
  surface as overturned once the revalidation back-reference exists. If those journals are no
  longer on disk, a synthetic decision-log fixture reproducing the documented signatures
  substitutes, and this record must then state that validation ran against synthetic history
  only.
- **Both directions:** a healthy fixture run yields no hotspot above the reporting floor.
- **Determinism:** identical run sets produce hash-identical reports.
- **Boundary:** the architecture test proves acceptance paths cannot import aggregation.
- **Repository verification:** ruff, full pytest, `vfx --help`.

## Release and rollback

Additive and observational: a new run-scoped log and report, no change to authority,
publication, or acceptance semantics. Prior runs report `not_instrumented`. Rollback is to stop
emitting; aggregation degrades to coverage reporting. No ADR: no durable authority, contract-of-
record, or boundary changes. A future proposal for telemetry-informed gating would require an
ADR and is rejected here by design.

## Remaining limitations

- The report ranks guess signatures, not proofs. A high loop rate can be honest difficulty;
  classification remains with the improvement lifecycle.
- Quietly wrong but accepted decisions surface only where an overturn source exists; the
  first release joins only revalidation refusals and monotonicity reverts, so overturn
  coverage starts narrower than the harness's full authority and widens as joins are proven
  (replay divergence deferred). The meter inherits the harness's authority coverage and
  cannot exceed it.
- Only registered decision points are measured. The registry is the residual blind spot;
  listing coverage in every report makes the gap visible without closing it.
- Cross-shot aggregation is deferred until heterogeneous fixtures exist; a single shot's report
  must not be read as general.
