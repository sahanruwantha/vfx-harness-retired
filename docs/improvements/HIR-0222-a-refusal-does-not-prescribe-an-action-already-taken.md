---
id: HIR-0222
title: A refusal does not prescribe an action already taken
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: two_refusals_alternated_and_neither_named_the_action_that_resolves_the_state
mechanism: the_gap_present_case_names_the_contract_removal_instead_of_the_escalation
adr: null
---

# A refusal does not prescribe an action already taken

## Observed failure

room_1046_opening run `20260905T100857Z-091259`, layer 2 materialization: **17 refusals**,
all on requirement R20, before the session was interrupted.

```
seq 27  R20 contract_ids=['bbox-h-38','bbox-h-113']  decision=no
  -> has recorded vocabulary gap(s) ['VG-004'] asserting that no registry metric can
     express its statement, so it cannot then be closed by contract bindings
     ['bbox-h-38','bbox-h-113']: bind an approved_start or planner_start decision

        R20 contract_ids=[... , 'transform-delta-mass-38-113']  decision=no
  -> same refusal, more bindings

        R20 with a decision, contracts retained
  -> declares only structural domains ['projected_composition','temporal'], which a
     decision cannot pay: bind a same-domain registry contract that measures the
     statement, or, when no registry metric can express it, call
     escalate_vocabulary_gap for this requirement first
```

The third message asks the session to call `escalate_vocabulary_gap`. **VG-004 already
exists**; that call had been made and cannot usefully be repeated. So the two refusals
alternate and neither names the action that resolves the state.

## Root cause

The domains a decision may pay are widened by a recorded gap only where a domain has **no**
contract evidence:

```python
decision_domains = {d for d in declared if d in qualitative_domains and not by_domain[d]}
if gap_ids:
    decision_domains |= {d for d in declared if not by_domain[d]}
if decision and not decision_domains:
    ... "call escalate_vocabulary_gap for this requirement first"
```

R20 declares `[projected_composition, temporal]`, neither qualitative. Once the session
bound contracts covering both — which the first refusal effectively pushed it toward, by
objecting to the contracts rather than to their presence — `by_domain` is non-empty for
every declared domain, so the widening adds nothing and `decision_domains` stays empty. The
refusal then fires with advice premised on there being no gap.

The state is resolvable and the resolution was never named: **remove the contract bindings
the gap asserts cannot measure the statement, and let the decision carry the domains.**
Both refusals were individually true. Neither was actionable in combination, which is the
"a surface that instructs an action states what it already knows about that action's
consequence" rule — here the surface knows `gap_ids` is non-empty and still prescribes
creating one.

HIR-0218 made this reachable: before it, gaps were invisible, so this branch never saw a
recorded gap and its advice was always right. Fixing the read exposed a message that had
never been correct for the case it could not previously encounter.

## Decision

When a decision is present, no domain is left for it to pay, **and gaps are recorded**, the
refusal names the removal rather than the escalation: the gap ids, the declared domains, the
contract bindings currently covering them, the instruction to remove those bindings, and an
explicit statement not to escalate again. The gap-absent case keeps its existing wording,
which remains correct when there is no gap.

## Validation

`test_a_recorded_gap_plus_full_contract_cover_names_the_removal_not_the_escalation` binds a
recorded gap, a decision, and full contract cover, then asserts the refusal names the gap it
already has, does **not** mention `escalate_vocabulary_gap`, says not to escalate again,
names the blocking bindings, and names the removal. It fails without the mechanism.

## Rejected alternatives

- **Let a gap widen every domain regardless of contract cover.** A recorded gap would then
  license a decision beside contracts that do measure the statement, which is the padding
  HIR-0202 exists to refuse.
- **Refuse the contracts at bind time.** The first refusal already does object to them; the
  defect is that the second refusal does not connect to the first. Refusing earlier would
  not have told the session what to do either.
