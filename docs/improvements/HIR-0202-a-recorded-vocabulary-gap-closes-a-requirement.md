---
id: HIR-0202
title: A recorded vocabulary gap closes its requirement, and the false padding finding is gone
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: the_escalation_path_produced_audit_state_no_validator_accepted_so_requirements_were_padded
mechanism: gap_backed_decisions_pay_any_declared_domain_and_the_finding_reports_the_true_reason
adr: null
---

# A recorded vocabulary gap closes its requirement, and the false padding finding is gone

## Observed failure

Caesar run `20260904T143311Z-c0f282`, requirement R20 (domain `projected_composition`):
"reproduce the visual composition, not the documentary's text labels or titles". The
materializer called `escalate_vocabulary_gap`, naming three registry kinds and why none can
certify the statement (a node-count absence is a self-certifying tautology, a region
statistic cannot separate glyphs from other high-frequency content, a frame delta measures
change not semantics). The tool recorded VG-001 and told it to close the requirement with a
decision. It did exactly that, and staging refused with two findings on the same binding:

```
requirement R20 carries a decision but every declared domain already has contract evidence;
remove the padding decision
requirement R20 declares AND domains ['projected_composition'] but does not pay
['projected_composition']; bound witnesses: no contract ids
```

Both cannot be true, and the binding provably carried zero contract ids. On the next turn the
model bound twelve `bbox_height` rows — projected union heights of other subjects — to a
text-absence proposition, and the terminal gate reported CLEAN.

The same trap reproduced on a different layer, requirement and domain. R15 (`scene`): "do not
model the exterior Curia complex, gardens, or flyover". A contract over `env.exterior` is
refused because no unit mutates those roles, which is the requirement's whole point; a
decision is refused because `scene` is structural. The session escalated VG-002, then bound
R15 to `hall-exists`, an `object_count` of one hall — a count answering an absence. R47
("renderable as a contained environment") was bound to the same id. Three requirements, one
contract, one proposition it can bear on.

## Root cause

Two rules with no intersection. HIR-0017 routes an inexpressible requirement to a recorded
vocabulary gap plus an explicit decision. HIR-0124/0145 later made requirement domains an AND
obligation where only unpaid `image` and `human` domains may be paid by a decision. For a
structural requirement no registry metric can express, the first rule's prescribed path
terminates in the second rule's refusal, so `escalate_vocabulary_gap` produces durable audit
state that no validator will accept — advice unfollowable by construction. The only remaining
move that passes is binding a same-domain contract that does not measure the proposition.

The false finding is a separate defect with the same consequence. `decision_domains` was
computed as "declared domains that are qualitative and unpaid", so for a structural-only
requirement it was empty for a reason that has nothing to do with contract evidence, and the
message asserted the reason it was not. Obeying it deletes the only binding present.

## Decision criteria

- Advice the harness gives must terminate in a state the harness accepts.
- A recorded gap is stronger evidence of inexpressibility than a bare decision: it enumerates
  the kinds tried and why each cannot certify. It may therefore carry any declared domain.
- A gap and a contract binding on the same requirement contradict each other; one of them is
  wrong, and the validator says so rather than accepting both.
- A finding states the reason that actually fired.

## General mechanism

1. `recorded_vocabulary_gap_ids(root)` reads `state/plan-escalations/vocabulary-gaps.jsonl`
   by requirement id; malformed or foreign rows are ignored, never raised, inside a validator.
2. When a requirement has a recorded gap, its decision pays every declared domain it still
   owes, structural included, so the escalation path terminates in a pass.
3. When a requirement has a recorded gap and no decision, closing it with contract bindings is
   refused, naming the gap ids and both legal moves (bind the decision, or retract the gap if
   those metrics do measure the proposition). This is the padding that actually shipped.
4. The false finding is split in two: with unpaid qualitative domains the decision is legal;
   with qualitative domains already paid the original message stands; with only structural
   domains the finding says so and names `escalate_vocabulary_gap` as the path that makes a
   decision legal.
5. `escalate_vocabulary_gap` now says the recorded gap is what makes the decision legal
   whatever the declared domains, and that binding same-domain contracts instead is the
   padding the gap says cannot measure it.

## Rejected patch-level alternatives

- Letting a decision pay any structural domain without a gap: removes HIR-0124's AND coverage
  wholesale, so a materializer could decide its way out of ordinary structural evidence.
- Detecting semantically incompatible bindings (a count answering an absence): not decidable
  from metadata, which is why the mechanism keys on the gap the materializer itself recorded.
- Leaving the tool's advice and teaching operators the exception: the tool is the instrument;
  advice it gives that its own validator refuses is the defect.

## Validation

- `src/tests/unit/test_vocabulary_gap_closure.py`: gap records are read by requirement id and
  malformed rows ignored; a structural requirement's decision without a gap is refused with
  the true reason and names the escalation path, never "already has contract evidence"; with
  a recorded gap the same decision validates; and a recorded gap plus contract bindings is
  refused naming the gap ids and both legal moves.

## Release and rollback

A materialization that recorded a gap and then bound contracts now fails where it passed, so
a shot carrying that padding must rebind before it validates again. Rollback restores the
padding path.

## Remaining limitations

Absence requirements ("do not model X") still have no positive evidence form: the mechanism
lets them close through a gap-backed decision rather than through a contract, which records
the judgment rather than proving the absence. A whole-scene "no host matches this selector"
check bound at the layer, which is the honest evidence, is not in the vocabulary. Contract
reuse across semantically distinct requirements remains undetected where no gap is recorded;
one contract id closing three requirements is a syntactic signal this record does not act on.
