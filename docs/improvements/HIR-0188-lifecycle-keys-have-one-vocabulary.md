---
id: HIR-0188
title: A window-lifecycle contract was unrepresentable, so the materializer looped
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: contradictory_validators_make_a_declared_value_unsatisfiable
mechanism: lifecycle_row_keys_derive_from_the_domain_that_validates_them
adr: null
---

# A window-lifecycle contract was unrepresentable, so the materializer looped

## Observed failure

Layer-2 rematerialization `20260903T211552Z-8ad44d` on `artifacts/room_1046_opening`
spent four staging turns oscillating between two refusals of the same contract:

```
 674s  refused: scene contract sc-facade-parallax: valid_through must be a positive integer
 703s  refused: unknown contract key(s) valid_through — the harness would ignore them
                silently; accepted keys are activates_at, axis, ... (no valid_through)
 732s  refused: scene contract sc-facade-parallax: valid_through must be a positive integer
 763s  refused: scene contract sc-facade-parallax: valid_through must be a positive integer
```

The session escaped only by abandoning the contract. Neither refusal was wrong on its own
terms; together they were unsatisfiable.

Reproduced deterministically, with no model, Blender or shot dependency:

```python
row = {..., "lifecycle": "window"}
validate_lifecycle(row)                 # 'valid_through must be a positive integer'
set(dict(row, valid_through="3")) - KNOWN_ROW_KEYS   # {'valid_through'}
```

## Root cause

Two vocabularies independently named the same concept and disagreed.

`domain/contracts.py` declares `LIFECYCLES = {"layer", "window", "persistent"}` and
`validate_lifecycle` **requires** `valid_through` for a `window` row (the `else` branch),
computing `bounds()` from it. The scene-contract row vocabulary,
`evidence/scene_checks/kinds.py`, listed `expires_at` — a name **no validator, reader or
writer in the tree uses** — and did not list `valid_through`. So a `window` contract had
to carry a key the row vocabulary refused, and refusing it made the lifecycle invalid.

`window` is offered to authors; only `layer` and `persistent` were reachable. The dead
`expires_at` is the fingerprint of a rename that migrated the validator and left the
vocabulary behind — precisely the "no silent compatibility" failure, inverted: nothing
was interpreted heuristically, the value simply became unusable, and the cost landed on a
model session with no way to satisfy either side.

This is the same shape as HIR-0187's second defect: a producer restating a rule its
validator owned. Here a vocabulary restated a domain's key names.

## Decision criteria

- One source of truth: the module that *validates* a key owns its name, and every other
  vocabulary derives it.
- A declared enum value must be expressible. An option a schema offers and no input can
  satisfy is a defect, not a constraint.
- Strict migration: a key nothing reads is removed, not kept for compatibility.

## General mechanism

`domain/contracts.LIFECYCLE_ROW_KEYS` is the set of keys `validate_lifecycle` reads.
`KNOWN_ROW_KEYS` is built as `LIFECYCLE_ROW_KEYS | {...}` instead of restating those five
names, and the dead `expires_at` is gone. No shot on disk used either key, so the
migration is clean.

## Validation

`src/tests/contract/test_lifecycle_row_keys_agree.py`:

- `LIFECYCLE_ROW_KEYS <= KNOWN_ROW_KEYS` — the structural invariant.
- Every value in `LIFECYCLES` round-trips both validators (heterogeneous: `layer`,
  `persistent`, and the previously impossible `window`).
- The fix widens the vocabulary without weakening the rule: a `window` row missing its
  end is still refused, and a `persistent` row carrying one is still refused.
- `expires_at` is absent from the vocabulary.
- A new lifecycle value must extend this test before it can be declared.

Reverting the derivation fails three of the eight.

## Rejected patch-level alternatives

- *Add `valid_through` to `KNOWN_ROW_KEYS` and leave `expires_at`.* Two names for one
  concept, one of them read by nothing, is what caused this. The vocabulary would still
  be free to drift from the validator.
- *Drop `window` from `LIFECYCLES`.* The value is used: `bounds()` computes a closed
  interval from it and judgment debt declares the same three lifecycles. Removing a
  working concept to avoid fixing a key name is a narrowing, not a fix.
- *Teach the materializer to avoid `window`.* Prompt wording for a mechanical defect; the
  option would remain offered and unsatisfiable.

## Release and rollback

No shot on disk used `expires_at` or `valid_through`, so removing the dead key migrates
nothing. Rollback is reverting the commit.

## Remaining limitations

- Other row vocabularies are not yet derived this way; only the lifecycle keys have a
  single source. A vocabulary/validator disagreement elsewhere would look the same, and
  the contract test added here covers only the lifecycle family.
