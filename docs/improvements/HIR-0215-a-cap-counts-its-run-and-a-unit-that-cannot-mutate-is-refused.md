---
id: HIR-0215
title: A per-run cap counts its run, and a unit that can mutate nothing is refused before it is published
status: accepted
introduced_in: unreleased
date: 2026-09-05
failure_class: a_run_scoped_cap_was_enforced_per_shot_lifetime_and_a_zero_cluster_unit_passed_every_gate
mechanism: the_per_layer_cap_counts_this_runs_ledger_rows_and_the_atomicity_predicate_fires_on_zero_clusters
adr: null
---

# A per-run cap counts its run, and a unit that can mutate nothing is refused before it is published

## Observed failure

Two shots reported independently, one corroborating the other.

**The cap.** `run_max_replans_per_layer` is enforced against every dispatch the shot has
ever made:

```python
attempts = controller_state.read_attempts(self.shot.folder)   # durable, all runs
same_layer = sum(1 for attempt in attempts if ... .layer_id == layer_id)
if same_layer >= self.caps.max_replans_per_layer:
```

while the two caps in the same block are run-scoped — `max_dispatches` reads
`len(self.rows)` and `max_usd` reads `run_spend_usd(self.layout)`. Three caps under one
comment, all named `run_*`, and the middle one durable. Measured live:

| shot | durable attempts | layer 2 | against cap 2 |
|---|---|---|---|
| hansa | 2 | 2 | spent, terminal |
| caesar | 2 | 2 | spent |
| room | 0 | 0 | — |

Both sessions read the name and inferred per-run; one of them reported the shot
permanently blocked on that basis and then corrected itself from the source. Neither
raised `VFXH_RUN_MAX_REPLANS_PER_LAYER`, which was right — doing so would have hidden the
defect and left it for the next shot.

**The zero-cluster unit.** hansa's `hero_podium_material` was staged, validated, attested
clean by the terminal gate, published — and could then execute nothing:

```
write_clusters: []
mutates: {roles: [], controls: ["podium_material"], dresses: [], control_roles: {}}

BLOCKED: active unit does not have exactly one derived write-cluster; found ['(none)'].
BLOCKED: this run_bpy payload has no authored scene mutation.
```

Its tickets require a shading mutation. It has no legal `run_bpy` at all.

## Root cause

**The cap.** The durable anti-repeat property does not live in this counter. It lives in
the cause-fingerprint guard four lines below, which permanently refuses any envelope whose
fingerprint was already dispatched in this shot. So every dispatch the controller makes is
necessarily a *different* defect, and bounding the number of distinct defects a layer may
ever have fixed — at two, for the shot's whole life — is not a safety property. It is a
ceiling that makes the controller useless on any shot outliving two findings, which both
shots reached within a day.

**The zero-cluster unit.** `atomicity` guards `len(clusters) > 1` and never zero, and
twenty lines below the same function tolerates the empty case explicitly
(`clusters[0]... if clusters else residual_instrument_family(unit)`). The
`unresolved_family` predicate that would have taught the right lesson needs a namespace to
reject: caesar's `hall_shell_unit` had `env.hall` failing to resolve and recovered on the
next attempt, but hansa's unit had `mutates.roles: []` — no namespace at all, nothing to
resolve, predicate never reached. The same hole from the other side.

Its upstream cause is one the reporting session traced and this fix does not close: the
materializer wanted a mutation selector, had every `control_roles` form refused by a
message naming the offending token but never the accepted shape, and settled for a bare
control. Controls derive no family. `control_roles` remains unusable and the materializer
still cannot express that unit.

## Decision criteria

- A cap named for a run counts that run. Sibling caps in the same block agree.
- Durable convergence is the fingerprint guard's job and it is untouched.
- A unit that can execute no mutation is refused where a misdeclared one already is, with
  the same guidance, rather than through a new gap type.
- A rejection names an action the receiving session can actually take.

## General mechanism

1. `_cap_refusal` counts this run's ledger rows for the target layer, matching
   `max_dispatches` and `max_usd` beside it. The refusal names the scope it counted and
   states that a repeated cause is refused separately and permanently.
2. `atomicity` raises `unresolved_family` when a unit derives no write-cluster, listing
   its `roles`, `controls` and `dresses` and reusing `UNRESOLVED_FAMILY_RULE` — including
   the sentence the reporting session said its materializer needed: *residual control is
   not a family*.
3. The builder's `BLOCKED` message names `cannot_express_in_scope`. It previously offered
   only "rematerialize or split", both outside builder authority, leaving the one action
   the session could take unstated; the builder that hit it found the abstention by
   searching.

## Rejected patch-level alternatives

- A reviewed reset transaction for the durable counter: ceremony around a counter that
  should not have been durable, putting an operator in a loop the fingerprint guard
  already closes.
- Raising the configured cap: hides the scoping defect and leaves it for the next shot.
- A new `zero_cluster` gap type: the existing predicate's guidance is already the right
  lesson, proven by caesar converging on it in one attempt.

## Validation

- `src/tests/unit/test_run_controller.py` and the atomicity suites cover the changed
  predicates; the cap's scope is asserted against this run's rows rather than durable
  attempt state.
- Live corroboration: caesar's `hall_shell_unit` hit `unresolved_family` and staged on the
  next attempt, which is the merely-misdeclared case this extension generalises to the
  unrepresentable one.

## Release and rollback

A layer may now be rematerialised up to the configured cap in each run rather than once
per shot lifetime. The fingerprint guard is unchanged, so no repeated cause becomes
dispatchable. Rollback restores a ceiling two live shots reached in a day.

## Remaining limitations

The zero-cluster gap stops the next materialization authoring such a unit; it cannot
retract one already published. hansa's selected view holds `hero_podium_material` today,
so that shot needs a controller dispatch or an operator rematerialization regardless —
the cap fix is what reopens the first of those. And the upstream `control_roles`
usability defect is untouched: the materializer will keep reaching for a form it cannot
get accepted, and settling for something that derives no family, until that message names
the accepted shape.
