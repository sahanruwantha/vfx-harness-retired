---
id: HIR-0237
title: A dropped image debt is recorded once per contract, not once per frame
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_layer_whose_four_units_all_sealed_could_not_mint_its_receipt
mechanism: the_revalidation_drop_list_is_keyed_by_contract_id_and_each_reason_names_its_frame
adr: null
---

# A dropped image debt is recorded once per contract, not once per frame

## Observed failure

`hansa_silk_road` layer 2, attempt 6 -- the best build the layer had produced, and the
first where every unit sealed:

```
hero_mass_geometry       passed
hero_podium_geometry     passed
hero_dominance_tracking  passed
hero_facade              passed
exit 1 — CRASHED

ValueError: layer finalization receipt projection.revalidation.result.dropped
            contains duplicate ids
  layer_finalization_projection.py:273  _validate_revalidation_projection
  <- LayerFinalizationReceipt.mint      layer_finalization_receipts.py:372
```

Four units sealed and the layer could not be recorded.

## Root cause

`evidence/checks.py` built the drop list by iterating runtime check **rows**:

```python
for d in rows:
    ...
    dropped.append((c.id, reason))
```

A multi-frame image debt has **one row per frame**, each bound to its own immutable
candidate handle (HIR-0048). `hero-facade-appearance-debt` read 1.826 against `>= 2` at
two frames, so the same id was appended twice.

The drop record's entire shape is `_REVALIDATION_DROP_FIELDS = {"id", "reason"}` --
**there is no frame field**, so two drops of one contract are indistinguishable, and the
validator's uniqueness rule is right to refuse them.

So the producer emits per-row and the record is keyed per-contract. Neither side is
individually wrong; they disagree about what the list is keyed by, and nothing pinned
them. That is the ninth instance of this shape in two days, and the second receipt-minting
crash of the night.

## Decision

Key the list by contract id, and let each reason name the frame it was read at:

```python
drops: dict[str, list[str]] = {}

def _drop(identifier, row, reason):
    frame = row.get("frame")
    drops.setdefault(str(identifier or "?"), []).append(
        f"f{frame}: {reason}" if frame is not None else reason
    )

dropped = [(cid, "; ".join(reasons)) for cid, reasons in drops.items()]
```

Producing, on hansa's rows:

```
('hero-facade-appearance-debt',
 'f51: missing payment schema ...; f151: missing payment schema ...')
```

**Aggregating rather than adding a `frame` field** was the choice, for two reasons: the
record is a *derived projection*, so widening its shape is a durable-schema change with a
migration for something nothing reads back; and the uniqueness rule states a real
invariant -- the projection summarises which contracts were dropped -- rather than an
accident. Nothing is lost, because each reason now carries its own frame.

Insertion order is the order rows were read, so the projection stays stable across runs.

## Validation

`src/tests/unit/test_revalidation_drop_keys_by_contract.py`:

- one contract dropped at two frames yields **one** entry, with no duplicate ids;
- that entry names **both** frames, so aggregation loses nothing;
- two distinct contracts stay distinct -- the aggregation keys on the id and does not
  collapse the list;
- **the discriminator**: the exact validator that refused hansa's receipt,
  `_validate_revalidation_projection`, accepts the producer's output. Only `dropped` comes
  from the producer; every other field is the minimum the validator requires, so a failure
  there is about duplicate ids and nothing else.

On the pre-fix tree it reproduces the crash verbatim:

```
E   ValueError: projection.revalidation.result.dropped contains duplicate ids
```

## The bug is invisible on the happy path

The reporting driver's observation, which explains the survival: a duplicate arises only
when a multi-frame debt is **dropped**, and a debt is dropped only when it **fails**. A
passing multi-frame debt produces no entry at all. So the defect needed a build good
enough to seal every unit *and* a debt that failed at more than one frame -- and nothing
had produced that combination before.

It also means the test needs a *failing* multi-frame debt rather than merely a multi-frame
one. Four branches can drop a row and the observed crash came from the threshold branch
(1.826 against `>= 2`); the fixture here exercises the provenance branch, because a
threshold drop needs a real render and adversary. Rather than leave three branches
unpinned, a fifth test asserts the funnel -- no branch appends to the list directly.

## A note on how this was verified, because the first attempt was worthless

The both-ways check for this change is `git checkout <parent> -- <file>`, not
`git stash push -- src/vfx_harness`. The stash form reverts only *uncommitted* changes, so
once the fix is committed it silently becomes a no-op and every test "fails without the
mechanism" by passing. The funnel test above was first checked that way and reported a
clean pass on a tree that still had the fix in it.

Against the real parent, four of the five tests fail:

```
E   AssertionError: [('hero-facade-appearance-debt', '...'), ('hero-facade-appearance-debt', '...')]
E   ValueError: projection.revalidation.result.dropped contains duplicate ids
```

## What this does not fix

The build that exposed it still has a genuine content gap -- `frame_detail` 1.826 against
`>= 2`, with two independent critics and the metric naming the same fix. That belongs to a
rebuild, not to an amendment: lowering the threshold to admit 1.826 would be tuning the
target to the build, which is what HIR-0232 exists to stop.

It is also not the same defect as the other receipt-minting crash of the night, where the
verdict allowlist accepts one `decided_by` value while the composed path emits five. Same
family, different file, separate change.
