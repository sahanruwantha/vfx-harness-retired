---
id: HIR-0214
title: A falsified unit is a typed stop, and an unclassified boundary's cause has an identity
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_terminal_state_crashed_the_claim_and_every_unclassified_boundary_shared_one_fingerprint
mechanism: the_driver_reports_the_durable_finding_before_claiming_and_the_stop_identity_carries_its_closed_cause
adr: null
---

# A falsified unit is a typed stop, and an unclassified boundary's cause has an identity

## Observed failure

caesar run `20260905T010713Z-69f4d1`. `columns_unit` had recorded
`hypothesis_falsified` with finding `hf-a43e90596de399af5dcb`, whose fault owners span
`benches_unit` (layer 2) and `camera_rig_unit` (layer 1). The controller had correctly
refused to dispatch it — multi-owner and out-of-range are both refusal conditions
(HIR-0186, HIR-0190) — and named layer 1 to include. The operator reran with layer 1 in
range. Layer 1 was already passed, so the driver skipped it, entered layer 2, and:

```
UnitAttemptConflict: cannot claim work unit columns_unit for planning from state
'hypothesis_falsified'; legal states are ['blocked', 'pending', 'retryable']
  orchestration/unit_state_claims.py:273  claim_ready_unit_for_planning
  agents/builder/layer.py:451
```

Unhandled, so the boundary returned without typed stop authority, the run terminalized
`harness_defect`, and the controller refused again. The operator could find no legal
transaction: `vfx units retry` refuses the state, `vfx units replan` is retired, and no
public finding-consumption adapter exists. It read as a deadlock.

It was not one. `vfx plan --layer N --rematerialize` supersedes the layer's units
regardless of prior status, and `hypothesis_falsified -> superseded` is the one legal
edge. The transaction existed; nothing named it.

Separately, the operator counted five genuinely different causes sharing one identity:

```
cause_fingerprint        4d94afd68547f51dca60a0e249897ca24907dfb3f975cac924fc98a6b08a6114
normalized_facts_digest  e6d66961ae63438a478afffd69160897f2cfb6a39d6486ea3a90aa0875c4754f
finding_ids              ['unclassified-boundary-e6d66961ae63438a478a']
```

an unhandled `ValueError` in mint, a stale finalization claim, a builder truncation, the
operator's own SIGTERM, and this claim conflict — plus hansa's `UnitEvaluationConflict`,
byte-identical across two shots.

## Root cause

Two causes, one theme: a boundary holding everything the operator needed, and emitting
none of it.

The claim check is correct — `hypothesis_falsified` genuinely is unclaimable, and its only
successor is `superseded`. What was wrong is where the refusal happened. `ready_units`
computes the ready set from passed rows and does not exclude a falsified unit, so the
driver selects it and the claim rejects it, from a site that holds the unit, the state,
the legal set, and a finding already bound in durable state. Every field of a teaching
stop was present at the raise.

The fingerprint is the "one value, two questions" pattern this campaign kept producing.
`normalized_facts` was two constants — the boundary and a fixed invariant string — with a
comment explaining, correctly, that the stop must not infer retry or recovery authority
from the exception. That protects AUTHORITY. It was implemented by making IDENTITY
constant, and those are different properties. Since the controller refuses a cause
fingerprint already dispatched in a shot, a constant identity means the second real defect
in any shot is suppressed as a repeat of the first.

The operator's SIGTERM sharing that id is the sharpest demonstration: an operator action
filed under one finding with four code faults.

## Decision criteria

- A terminal state the claim cannot accept is reported before the claim, by the boundary
  that holds the finding.
- The report names the transaction, including the flag a layer with passed units needs,
  and says which command does not accept the state.
- Reuse the existing typed path: a durable finding already compiles to an authority-defect
  stop, so this is not a new stop kind.
- Identity varies with cause; authority stays empty. The stop authorizes nothing either
  way — that property is unchanged.
- Only closed vocabulary and an exception TYPE enter the identity, so no free-form prose
  or message detail leaks into it.
- An operator-initiated cause is distinguishable from a code fault, while remaining a real
  invariant violation: a recorded signal should have become an interruption receipt, not
  an unclassified stop.

## General mechanism

1. `unit_state_queries.unresolved_falsification` returns the durable finding bound to a
   unit in `hypothesis_falsified`, or `None`.
2. The layer driver calls it before claiming and raises `BuildAuthorityDefect` with that
   finding, so the existing CLI path compiles the same authority-defect stop it already
   compiles for a live falsification. The detail names the rematerialization command with
   the finding as evidence, notes `--discard-accepted` for a layer holding passed units,
   and states that `vfx units retry` does not accept this state.
3. `normalized_facts` becomes `/v2` and carries the closed terminal cause, the exception
   type, and whether the cause is operator-initiated.
   `unclassified_authority.OPERATOR_CAUSES` names `interrupted` and `requested_exit`.

## Rejected patch-level alternatives

- Excluding falsified units from the ready set: hides the unit and leaves the layer
  looking complete while a finding is unresolved.
- Making the claim accept the state: destroys the invariant the check exists for.
- Adding the exception MESSAGE to the identity: leaks paths and values into a digest and
  makes the same defect fingerprint differently per run.
- Reclassifying an operator stop out of `harness_defect`: the invariant was genuinely
  violated in that case too — the signal should have been recorded as intent — so the
  class is right and only the identity was wrong.

## Validation

- `src/tests/unit/test_unclassified_stop_identity.py`: the five colliding causes produce
  four identities (the two same-type same-cause rows legitimately coincide); an operator
  stop is distinguishable from a code fault; two different exception messages produce one
  identity, so prose cannot enter it; a falsified unit with a bound finding is reported
  and a passed, absent, or differently-stated unit is not; a falsified unit with no bound
  finding leaves the claim's own refusal as the boundary.
- Re-derived against the six real terminals from the two live shots: all six now produce
  distinct identities where they previously shared one.

## Release and rollback

The stop identity schema moves to `/v2`, so a fingerprint computed before this change does
not equal one computed after for the same cause. That is the point: the prior value was
shared by every unclassified boundary ever produced. No stored artifact is rewritten.

## Remaining limitations

Two same-type, same-cause failures at the same boundary still share an identity — the
raising site is not in the payload. Adding a code location would distinguish them and
would also change the fingerprint on every refactor, so it is not done here. And the
deeper question the operator's SIGTERM raises is untouched: a recorded signal should have
produced an interruption receipt rather than reaching this path at all, which belongs to
the interruption machinery rather than to the identity.
