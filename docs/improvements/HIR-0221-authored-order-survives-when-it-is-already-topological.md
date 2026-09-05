---
id: HIR-0221
title: Authored order survives when it is already topological
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: the_layer_sorter_chose_a_different_valid_topological_order_than_the_capsule_set
mechanism: the_ready_queue_is_ordered_by_authored_position_across_the_whole_queue
adr: null
---

# Authored order survives when it is already topological

## Observed failure

hansa_silk_road run `20260905T100338Z-5d62f1`, building layer 1 immediately after a clean
rematerialization. `state failed`, `exit 1`, `$2.00`:

```
harness_defect: The 'build' boundary returned without typed stop authority.
  finding_ids   ['unclassified-boundary-c599c6aedab0b0a9c8b2']
  invariant_id  terminal_boundary_requires_typed_stop
```

with this underneath:

```
agents/builder/verify.py:190                    _verify_script
agents/builder/layer_composition_finalization.py:285   _capture_replay_before_observation
orchestration/judgment_debt_replay_mint.py:165         replay_prefix_receipt
orchestration/judgment_debt_replay_authority.py:71     capture_selected_judgment_replay_prefix
ValueError: selected authority capsules do not preserve the stable topological layer order
```

Reproduced deterministically against the shot's live selected authority:

```
capsule order : ('1','2','3','4','5','6','7')
topological   : ('1','2','4','3','5','6','7')
```

## Root cause

hansa's layer DAG:

```
1: []          2: [1]        3: [1,2]      4: [1]
5: [1,2]       6: [1,2,3,5]  7: [1,2,3,4,6]
```

**The authored order `1..7` is itself a valid topological order.** Layers 3 and 4 are
independent of each other, so both `…3,4…` and `…4,3…` satisfy every edge. The capsule set
is in authored order; the sorter returned the other one; the invariant demands equality and
raised.

`topological_sparse_layer_ids` documents its own rule — *"Authored order is the tie-break
among independent ready layers (HIR-0119)"* — and did not implement it:

```python
ready.extend(sorted(unlocked, key=lambda item: authored[item]))
```

Only the newly-unlocked batch is sorted. A layer unlocked early therefore sits ahead of a
lower-authored layer unlocked later:

```
ready=[1]  pop 1 -> unlocked [2,4]   ready=[2,4]
           pop 2 -> unlocked [3,5]   ready=[4] + [3,5] = [4,3,5]   <- 4 ahead of 3
           pop 4
```

4 unlocks when 1 completes; 3 only when 2 does. Authored position says 3 first, and it was
never consulted across the queue — only within each batch.

The invariant at `judgment_debt_replay_authority.py:71` is correct and worth keeping: the
capsule set and the derived order must agree, because a judgment payment binds an exact
replay prefix. The defect is that one of the two derivations was not stable.

## Decision

The ready queue is ordered by authored position across the **whole** queue, not per unlock
batch:

```python
ready.extend(unlocked)
ready.sort(key=lambda item: authored[item])
```

Stability remains a tie-break only. An authored order that violates an edge is still
reordered, and a cyclic DAG is still refused.

## Validation

`src/tests/unit/test_sparse_layer_order_is_stable.py`:

- `test_authored_order_is_kept_when_it_is_already_topological` — hansa's exact DAG.
- `test_a_layer_unlocked_early_does_not_overtake_a_lower_authored_one` — the minimal
  shape, independent of the shot: `b` unlocks with `a`, `c` only with `x`, authored puts
  `c` first.
- `test_dependencies_still_precede_dependants_when_authored_order_is_wrong` — stability is
  never a licence to violate an edge, including when the authoring is reversed.
- `test_a_cyclic_dag_is_still_refused`.

The first two fail without the mechanism and pass with it; the last two are guards on
preserved behaviour and pass both ways. Verified additionally against hansa's live selected
authority: capsule order and derived order now agree exactly.

## The second defect in the same failure, not fixed here

A `ValueError` escaped the `build` boundary without typed stop authority, so a specific,
diagnosable ordering fault was terminalized as `harness_defect` /
`unclassified-boundary-…` and routed to engineering with no cause. That is the third
boundary today to convert a typed-able failure into an untyped one — after
`materialize_mcp.py:603`'s `except (ValueError, OSError, json.JSONDecodeError)` handing a
`TypeError` to the model as a bare string, twice. It is queued as its own record: the class
is a boundary that classifies some exceptions and lets the rest through untyped, and the
cost is a real diagnosis replaced by a generic one.

## Two observations from the shot driver, both worth more than the fix

**The comparison was available for $0 and was made after full spend.** This fires during
replay-prefix capture — *after* layer 1 built, passed 11/11 authoritative scene contracts
and 2/2 bound checks, sealed, and published `build/01_camera.py`. Nothing was lost, but a
whole builder session was paid for before two orderings derived from the same selected
authority were compared. Both are computable at plan-gate time from the bundle alone, with
no model spend. That is the same shape as HIR-0219's impossible bands: a comparison the
harness can make cheaply from selected authority, deferred until after a builder has paid
to discover it empirically.

**It is the third instance in one day of one quantity with two derivations.** HIR-0217:
`mutates.roles` had a relative authoring notation and `control_roles` kept the absolute
one, and only one matched the record. The turn counter: `observed_turns` counts content
blocks and `cli_num_turns` counts tool round trips, and only one is the quantity
`--max-turns` bounds. Here: capsule storage order and `topological_sparse_layer_ids`, and
only one honoured the tie-break HIR-0119 states.

In all three, **both derivations existed in the codebase and neither was pinned against the
other**. The recurring defect is not any of the three bugs; it is that a second derivation
of a quantity is allowed to exist without a test asserting it agrees with the first. The
`judgment_debt_replay_authority.py:68` equality check is the only reason this one was
caught at all rather than silently reordering a replay prefix — it is the pattern's cure
applied in exactly one place.

## Rejected alternatives

- **Sort the capsule set to match the sorter.** Both derivations would agree and neither
  would honour authored position, so the stated HIR-0119 tie-break would still not exist.
- **Weaken the invariant to "is a valid topological order".** It would accept two
  derivations that disagree, and a judgment payment binds an exact prefix — order is the
  thing being certified.
