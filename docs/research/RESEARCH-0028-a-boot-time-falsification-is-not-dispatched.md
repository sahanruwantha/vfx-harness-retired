---
id: RESEARCH-0028
title: A boot-time falsification is as determined as an in-run one, and is not dispatched
status: open
date: 2026-09-05
owner: unassigned
---

# A boot-time falsification is as determined as an in-run one, and is not dispatched

## The question

Asked directly: why can the harness not decide the legal path and run it itself?

For the in-run case it already does. ADR-0010's controller consumes a stop envelope's
single typed action and dispatches `publish_validated_amendment` unassisted —
hansa_silk_road observed the complete loop today with no operator: builder proved
impossibility with six probes, abstained with `cannot_express_in_scope`, recorded
`hf-511203cb3d8be38729d3`, and the controller dispatched `--layer 1 --rematerialize`,
whose materializer authored satisfiable replacements.

The rule is stated in `application/run_controller.py`:

> Every one of those actions was mechanical and fully determined by the typed record
> (ADR-0010). The controller performs exactly that dispatch, and nothing inferred.

So the boundary is not "a machine may not replace authority". It is **auto-run when the
typed record fully determines the transaction; refuse when something must be inferred or
a bound is spent.** Each existing refusal fits:

| refusal | why it is not mechanical |
|---|---|
| fault owners span layers | the controller must *choose* whose authority to replace |
| owner outside the run's layer range | same, and the operator may not have meant to touch it |
| finding changes a hard constraint | a human authored the constraint; `escalate_question` |
| cause fingerprint already dispatched in this shot | durable, permanent anti-loop |
| dispatch / per-layer / USD cap spent | a declared bound |
| the other five transaction kinds | no adapter exists; there is nothing to run |

## The gap

hansa_silk_road's current state is **none of those**. Its finding is pre-existing durable
state at run boot, and the controller's contract is to consume an envelope's typed action —
at boot no envelope has been minted, so there is nothing to consume. The run refuses in
5.3s for $0.00 and names the transaction:

```
BUILD UNPASSED — layer 1 unit camera_path is hypothesis_falsified by hf-511203cb3d8be38729d3
and no builder may claim it. Its only legal successor is `superseded`, published by a reviewed
authority transaction: vfx plan <shot> --layer <owning layer> --rematerialize --owner <you>
--trigger <why> --evidence state/hypothesis-falsifications/hf-511203cb3d8be38729d3.json
(add --discard-accepted when the layer holds passed units).
```

HIR-0214 made that message exist. It names the command, every flag, the evidence path and
the conditional. **A boundary that can print the command has the information to run it.**

## The objection, and why it does not apply here

Rematerialization can destroy accepted work through `--discard-accepted`, and a machine
retiring paid-for units on an unreviewed rule is the one thing this design should not do.
Checked rather than assumed:

```
artifacts/hansa_silk_road/state/work-units/layer_1.json
   camera_path      hypothesis_falsified
```

One unit, falsified, nothing accepted. `--discard-accepted` is not needed, so no accepted
work would be discarded. The objection is real but does not reach this case.

## Candidate mechanism

At run boot, when a durable falsification has a **single in-range sealed fault owner** and
the target layer holds **no passed units**, mint the stop envelope from durable state and
hand it to the existing dispatch path. Nothing new decides anything: the existing path
already proves the commit through an independent evaluator, writes a per-dispatch ledger
row, and is bounded by the durable cause-fingerprint guard and the per-run caps.

The reviewed line stays where it matters. When the target layer **does** hold passed units,
auto-supplying `--discard-accepted` would have a machine throw away sealed, paid-for work on
a rule nobody reviewed. That case stays an operator transaction.

## What must be proven before it lands

- The minted envelope must be provably **current**: the finding's fault owners still sealed,
  the authority digest unchanged since the finding was recorded. A stale finding must refuse,
  not dispatch.
- The anti-loop property must hold across restarts. The cause-fingerprint guard is durable
  and permanent, which is the property that makes this safe; the per-run caps are not, so the
  fingerprint guard must be the thing carrying it.
- A held-out fixture where the target layer *does* hold passed units must still refuse.

## Evidence

- hansa_silk_road run `20260905T071740Z-c272c6`, boot refusal, 5.3s, $0.00.
- `application/run_controller.py` — `_cap_refusal`, `_convergence_refusal`,
  `_ownership_refusal`, and the module docstring's "nothing inferred".
- HIR-0186, HIR-0190, HIR-0214, HIR-0215, ADR-0010.
