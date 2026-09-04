---
id: HIR-0195
title: The count-contradiction check skipped the rows that state descendant population
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: contradiction_check_blind_to_sibling_counts
mechanism: sibling_object_count_rows_supply_their_demanded_host_count
adr: null
---

# The count-contradiction check skipped the rows that state descendant population

## Observed failure

Layer 2 of `artifacts/room_1046_opening` staged, in one candidate:

```
facade-object-count         object_count eq 1   over exterior.facade
facade-window-object-count  object_count min 12 over exterior.facade.window
```

A literal selector matches its exact tag and every dotted descendant (HIR-0147), so the
first row reads at least 13 in any scene satisfying the second. The builder spent a full
session establishing that by measurement and abstained:

```
facade-object-count requires object_count(roles=["exterior.facade"]) eq 1. Per the
documented matcher rule (HIR-0147), this literal selector matches its exact tag AND every
dotted descendant ... facade-window-object-count simultaneously requires
roles=["exterior.facade.window"] min 12
```

`exterior_facade` recorded `hypothesis_falsified`; `exterior_atmosphere`,
`exterior_ground` and `exterior_material` were blocked behind it; the run exited 7 after
$15.29, and the controller could not repair it because layer 2's rematerialization cap was
already spent.

## Root cause

`namespace_count_contradictions` exists to refuse exactly this pair (HIR-0178), and this
session watched it fire correctly on `object_count eq 1` over `exterior.tower`. It missed
this one in two independent ways:

```python
for other in typed:
    if other is row or other.get("kind") == "object_count":
        continue
```

**It skipped every sibling whose kind was `object_count`** — the row type that most
directly asserts how many hosts a descendant namespace must contain. The earlier firing
worked only because those siblings were keyframe and projection rows.

And `_minimum_distinct_hosts` counted each descendant namespace as **one** host. Even had
the sibling been visible, a row demanding twelve hosts would have contributed one, and
`eq 1` would have survived beside `min 12`.

The original exclusion is understandable — a count row must not contradict itself — but
`other is row` already covers that. Excluding the whole kind traded a real class of
contradiction for a guard that was already redundant.

## Decision criteria

- The check's own premise is that sibling rows reveal a descendant population the bound
  cannot admit. A sibling that *states a count* is the strongest such evidence, not an
  exception to it.
- Read what a row demands, not merely that it exists: `min 12` means twelve hosts.
- Decide before spend. The pair is visible in one candidate at staging time; the builder
  should never have had to measure it in a live scene.

## General mechanism

`_count_lower_bound(row)` mirrors the existing `_count_upper_bound`: the smallest host
count a row demands (`min lo`, `band lo`, `eq value - tol`), defaulting to 1. Sibling
`object_count` rows are no longer skipped, and each descendant namespace contributes the
largest count any sibling demands under it. `_minimum_distinct_hosts` sums those demands
over namespaces that have no descendant of their own, so nested namespaces are still
counted once through their deepest row.

## Rejected patch-level alternatives

- *Special-case `eq 1`.* The same blindness hides `max 5` against `min 12`, and any bound
  below a demanded population.
- *Teach the materializer to avoid parent-namespace counts.* Prompt wording for a
  mechanical contradiction the gate can decide, and the parent count is often what the
  author means — the repair is a leaf role or a higher bound, which the message names.
- *Let the builder keep discovering it by measurement.* That is the observed failure: a
  full session and a blocked layer to learn something derivable from two rows.

## Validation

`src/tests/contract/test_namespace_count_demands.py` — the observed pair is refused with
`minimum == 12`; the message names both legal repairs; counting a leaf role clears it;
raising the bound clears it; a genuinely satisfiable pair (`max 20` vs `min 12`) stays
clean; the demanded count drives the minimum for `max` and `eq` bounds alike; a non-count
sibling still contributes one host (the pre-existing behaviour); two descendant namespaces
sum their demands; a row never contradicts itself; a layer-scoped count ignores another
layer's row while a `persistent` one does not. Reverting the sibling inclusion fails six
of the twelve.

## Release and rollback

A staged candidate carrying such a pair is now refused where it previously reached the
builder. No durable state changes shape; rollback is reverting the commit.

## Remaining limitations

- The demand is read from a row's declared bound, so a descendant population implied only
  by geometry the plan does not count remains invisible until replay.
- Nested demands are summed over sibling namespaces, which is a lower bound on the true
  host count; a namespace whose descendants overlap is counted once through the deepest
  row, so the check stays conservative and never over-refuses.
