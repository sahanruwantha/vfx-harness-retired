---
id: HIR-0223
title: One predicate decides whether a decision pays a domain
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: the_terminal_gate_refused_the_state_the_local_validator_prescribed
mechanism: both_boundaries_call_one_shared_decision_may_pay_domain_predicate
adr: ADR-0006
---

# One predicate decides whether a decision pays a domain

## Observed failure

room_1046_opening run `20260905T100857Z-091259`. The materializer reached the resolution
HIR-0222's new advice prescribes, on its own, and passed the local validator with it:

```
[1367.4s]  "Now, decision-only (no contract_ids -- that would be padding) for R20/R21/R52."
[1371.9s]  -> patch_materialization  /requirement_bindings
[1372.0s]  <- VALIDATION PASSED for jit-layer-32.json
[1374.5s]  -> finalize_materialization
[1384.4s]  <- x [requirement-domain-binding] R20 -- provisional decision cannot pay
                structural domain 'projected_composition'
             x [requirement-domain-binding] R20 -- ... 'temporal'
             x [requirement-domain-binding] R21 -- ... 'projected_composition'
             x ... six findings across R20, R21, R52
```

The local validator accepted the authority and the terminal gate refused it, for reaching
exactly the state the validator had just prescribed.

## Root cause

Two boundaries decide whether a provisional decision may close a domain, and they
implemented different rules.

`evaluation/plan_gate/meta.py`:

```python
for domain, binding_kind, binding_ids in requirement.domain_bindings:
    if binding_kind != "contract":
        if domain not in {"image", "human"}:
            findings.append(Finding("requirement-domain-binding", True, requirement.id,
                f"provisional decision cannot pay structural domain {domain!r}", ...))
        continue
```

Unconditional, and `grep -c "vocabulary_gap|gap_ids|recorded_vocabulary" meta.py` returned
**0** — the gate had no knowledge of vocabulary gaps at all. It implements HIR-0124: a
decision may pay an unpaid `image` or `human` domain. It does not implement HIR-0202,
which AGENTS.md states verbatim at line 1041:

> A recorded gap is what makes that decision legal for any declared domain, structural
> included.

`orchestration/jit_materialization/validate_requirements.py` implemented both. So the rule
the repository states in prose existed in one of the two places that decide it.

**HIR-0218 made the disagreement reachable.** Before gaps were readable, no requirement
could carry one, so the gate's unconditional refusal was never wrong in practice. This is
the third piece of code that fix has exposed as only accidentally correct — after the
newly-reachable branch in HIR-0222, and the branch's own advice. Repairing a silent read
does not just fix a path; it exercises code that had never run.

It is also an instance of the rule added to AGENTS.md earlier the same day: **an unpinned
second derivation is itself the defect.** Two implementations of one question, neither
asserted against the other, and only one matching the record.

## Decision

`domain/vocabulary_gaps.decision_may_pay_domain(domain, *, gap_ids)` is the single
implementation, beside the gap reader that owns the input:

```python
return str(domain) in QUALITATIVE_DOMAINS or bool(gap_ids)
```

Both boundaries call it. `validate_requirements` no longer widens by gap in its own
expression, and `meta.py` reads recorded gaps through the shared
`recorded_vocabulary_gap_ids(folder)` — the same function and the same location, so the
gate cannot develop a third opinion about where gaps live. The gate's refusal now names the
missing gap and the two ways out rather than only the contract.

`_check_meta_records` already reads `folder / "state" / …`, so no plumbing was needed; the
shot folder was in scope.

## Validation

- `test_gate_refuses_a_decision_on_a_structural_domain_without_a_recorded_gap`
  (`test_plan_records.py`) drives the real gate through `_check_meta_records`: a
  decision-only structural binding is refused, the refusal names the missing gap, and then
  **recording the gap where `escalate_vocabulary_gap` writes it clears the same authority
  through the same gate**. It fails on the pre-fix gate, which produces the refusal and —
  having no gap knowledge — cannot clear it.
- `test_one_predicate_decides_whether_a_decision_pays_a_domain` asserts both modules call
  the shared predicate and that neither restates `{"image", "human"}`.
- `test_a_gap_backed_decision_survives_the_terminal_gate` is the cross-boundary assertion:
  each boundary alone was individually correct, so only an assertion spanning both can
  catch the disagreement. That framing is the room_1046_opening driver's.

## Rejected alternatives

- **Teach `meta.py` the rule directly.** It would fix this instance and leave two
  implementations, which is the defect rather than the bug.
- **Let the local validator refuse what the gate refuses.** It would make the two agree by
  making both wrong: AGENTS.md states the gap-backed decision is legal, and a requirement
  with a recorded gap would then have no legal closure at all.
