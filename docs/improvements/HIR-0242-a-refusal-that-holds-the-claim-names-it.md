---
id: HIR-0242
title: A refusal that holds the claim names it
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: an_active_claim_refusal_named_neither_the_claim_nor_the_transaction_that_clears_it
mechanism: the_refusal_is_rendered_from_the_claim_it_is_refusing_on
adr: null
---

# A refusal that holds the claim names it

## Observed failure

`caesar_curia`, run `20260906T042509Z-6b0960`. `reports/summary.json`, verbatim:

```
terminal_cause: typed_stop_selected
detail: harness_defect: The 'build' boundary returned without typed stop authority; it
  raised vfx_harness.orchestration.layer_finalization_state.LayerFinalizationConflict:
  layer 1 already has an active finalization claim ...
```

Correct HIR-0170 behaviour -- a process death after a layer replay left an exact
pre-terminal finalization claim active, and restart failed closed on it, for $0.00. The
driver then had to grep `state/work-units/layer_1.json` for the claim id, and
`vfx finalizations release` cleared it in one command.

## Why it was allowed

`layer_finalization_state.py:332` reads `slot["active_claim"]` and refuses on it:

```python
if slot.get("active_claim") is not None:
    raise LayerFinalizationConflict(f"layer {layer.id} already has an active finalization claim")
```

**The frame holds the claim it is refusing on and names the condition instead.** The claim
id, its attempt revision, and the run that minted it are all in `active`, and all three are
discarded. AGENTS.md requires a rejection to name the violated contract, the observed value,
and the legal next action; this named one of three.

It also reaches the operator. The unclassified-boundary envelope's `found` carries the
exception text, `detail` is composed from `found`, and `status.json` and
`reports/summary.json` both carry `detail` (HIR-0226). So this string *is* the surface an
operator reads, and it sent one to `grep`.

Worth recording that HIR-0226's own comment, in the file that renders that envelope, cites
"a `LayerFinalizationConflict` whose own message names the `vfx finalizations release`
transaction that recovers it". It does not. I wrote that comment optimistically and never
opened the message.

## Mechanism

`describe_active_claim_conflict(layer_id, active)` renders the refusal from the claim:

```
layer 1 already has an active finalization claim lfc-c9083faca23744c2 (attempt_revision 3),
minted by run 20260906T002035Z-f1ad1d. A live owner may still hold this claim. Only after
the builder fence proves no live owner may an operator release it, and release is a
reviewed transaction that archives the named unsealed claim, preserves every accepted unit
receipt, and marks unsealed judgment output non-reusable (HIR-0170). The transaction is:
vfx finalizations release <shot> --layer 1 --claim-id lfc-c9083faca23744c2 --reason <why>
--evidence <path>
```

The precondition precedes the command deliberately, and a test asserts that order: release
is not a retry, and a message that leads with the command invites one.

Optional fields degrade rather than break -- a claim carrying no `attempt_revision` or
`run_id` still yields the id and the transaction.

## Validation

`src/tests/unit/test_active_claim_refusal_names_its_recovery.py`. The call-site test is
**parsed, not grepped**: it asserts a call node with that callee inside
`claim_layer_finalization`, and that no string literal in that function re-states the
condition in its own words. A substring check on the source would pass with the call deleted
and its text left in a comment, and fail on a rename that preserves behaviour -- which is
the defect the caesar_curia driver removed from their own test earlier the same day.

## What this does not fix

**The stop is still classified `harness_defect` and routed to engineering.** A stale
finalization claim is recoverable authority state, not a harness defect, and the envelope
should say so: stage `builder`, action `escalate_question`, class `human_decision_required`,
with the claim in `found` and the precondition and command in `next_action`.

The design, which is owed: the conflict carries its recovery as **data**, and the boundary
renders the typed stop from it, rather than every consumer parsing prose. The discriminator
is "does a recovery exist", not the exception type -- `LayerFinalizationConflict` also covers
uninitialized state, a changed plan identity, and a terminal receipt already present, and
those are not the same answer. Deliberately not a new dispatchable transaction id:
proving no live owner is not something the controller may do, so `escalate_question` is the
honest class.

That half is not attempted here because a typed `recovery` object with no consumer is an
unwired mechanism, and this session shipped one of those by accident already. The message is
rendered from the claim and consumed today; the envelope work introduces the type with its
consumer.

Reported by the caesar_curia driver.
