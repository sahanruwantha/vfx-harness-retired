---
id: HIR-0197
title: Deferred union rows name the producers that share them and the slack left on their irreversible side
status: accepted
introduced_in: unreleased
date: 2026-09-04
failure_class: partial_producer_consumed_a_shared_union_band_and_the_payer_discovered_it_last
mechanism: union_producer_enumeration_with_pending_producers_and_measured_irreversible_slack_in_card_and_read_back
adr: null
---

# Deferred union rows name the producers that share them and the slack left on their irreversible side

## Observed failure

Room run `20260904T105849Z-0c9a45`, layer 2, on main `5b31a78`. Layer 1's camera had
authored the persistent framing row `bbox-ext-f1`: the `exterior.*` namespace must project
to a screen height in `[0.15, 0.35]` at frame 1 (HIR-0184). Three geometry units share that
namespace: `exterior_massing`, `exterior_facade`, and `exterior_ground`, the dependency-
complete payer.

`exterior_facade` received the row as a diagnostic forecast (HIR-0134), read
`bbox_height = 0.349`, "within target", and froze. `exterior_ground` then inherited the
union as its required row, measured `0.3936`, and abstained:

```
hf-993fb8a046dee07b6393  layer 2  unit exterior_ground  contracts [bbox-ext-f1]
  unsatisfiable_in_scope: Deferred forecast bbox-ext-f1 requires exterior.* bbox_height
  in [0.15,0.35] at frame 1, but the measured value is 0.3936 ... This unit is the
  dependency-complete producer ... the harness correctly promotes this from
  diagnostic-only to REQUIRED BEFORE FREEZE once my geometry completes the union.
```

The controller had already rematerialized layer 2 twice, so the cap refused a third
dispatch and the shot parked. Two units, one band, and neither had been told the other
existed: the facade's read-back said "within target" with one thousandth of the band left,
and the ground's card said "required" with no way to see that the room was already spent.

## Root cause

HIR-0134 and HIR-0152 made a deliberate choice: forecasts to partial producers are
diagnostic-only, because the union is incomplete and an earlier producer cannot be billed
for geometry that does not exist yet; only a miss on the already-impossible side becomes a
blocker. That is still right. What the design left out is the quantity the decision turns
on: a projected union can only grow in width, height, and bottom and only fall in top
(HIR-0152), so whatever an earlier producer consumes of the band on that side is gone for
everyone after it. The harness measured the union after every mutation and reported "within
target"; it never reported how much target was left, or that another producer still had to
fit into it. The payer's card named the row it must pay (HIR-0174) but not the producers
whose sealed geometry it was paying for.

This is the instrument gap AGENTS.md names: a decision (freeze here or lower the facade)
depended on a quantity (remaining slack) and an enumeration (who still adds to the union)
that were both knowable and neither exposed, so the builder guessed, and the harness told
the last unit.

## Decision criteria

- Enumerate, don't imagine: the producers sharing a deferred union come from the selected
  DAG in its stable order, not from the builder's reading of role names.
- Measure, don't estimate: the slack is the measured union against the bound, on the side
  that cannot be repaired later, per row.
- No new refusal and no threshold: whether one thousandth of a band is enough for the
  pending producers is a judgment; the harness supplies the number and the names.
- The same enumeration reaches the card before the unit spends and the read-back after
  every mutation, so kickoff and mutation see one truth.

## General mechanism

1. `evidence/scene_checks/deferred_subject.deferred_subject_union_producers` lists, per
   activation-layer deferred row, every geometry unit whose mutation overlaps the row's
   roles, in the stable topological order over `depends_on` with authored position as the
   tie-break (HIR-0119). `deferred_subject_sharing_for_unit` narrows that to the rows the
   active unit touches and splits the producers into those inside its dependency closure
   and those `pending` after it.
2. `deferred_subject_union_slack` measures, per evidence row on a growing kind (`bbox_width`,
   `bbox_height`, `bbox_bottom_y`) or the falling kind (`bbox_top_y`), the room left before
   the bound on that irreversible side; negative once crossed. Floors on growing kinds and
   ceilings on the falling kind are repairable and carry no slack.
3. The unit card's `deferred_subject_forecasts` and `deferred_subject_payments` rows carry
   `union_producers` and `pending_producers`; the builder's `phase` carries the same map as
   `deferred_subject_sharing`, and the mutation read-back's forecast note states, per
   diagnostic row, the measured slack and which pending producers still add to the union.
   Blockers keep their `REQUIRED BEFORE FREEZE` section unchanged.

## Rejected patch-level alternatives

- Reserving a fixed fraction of the band for later producers: a threshold nobody can
  justify, and a refusal where the rule says judgment.
- Making a partial producer's forecast miss a blocker: contradicts HIR-0134's reason for
  forecasts and bills a unit for geometry it did not build.
- Splitting the plan's rows per subject by hand for this shot: shot-specific, and it hides
  the same gap on the next shot with a shared namespace.
- Prompt text telling builders to "leave room": no number, no names, no measurement.

## Validation

- `src/tests/unit/test_deferred_subject_slack.py`: producers follow dependency order with
  the authored tie-break and exclude non-geometry units; the pending set is the producers
  outside the unit's closure and is empty for the payer; slack exists only on the
  irreversible side for band, max, min, and eq bounds and is negative once crossed; the
  read-back names the slack and the pending producers for a diagnostic row, omits the
  sharing clause when nobody is pending, and leaves the blocker section unchanged.
- `src/tests/unit/test_unit_scope.py`: forecast and payment rows carry the two new fields
  on the existing two-producer fixture.
- Live reproduction: the same facade freeze would now read `union can only grow: +0.0010
  left before bbox_height exceeds 0.35 — exterior_ground still add geometry to this union
  and share that room`, and the ground unit's card lists `exterior_facade` among the union
  producers it inherits.

## Release and rollback

Additive fields on card rows and read-back text; no schema or authority change. Rollback
removes the fields and the phrase.

## Remaining limitations

Slack tells a producer how much room is left, not whether the band is satisfiable at all.
In the room shot `exterior_massing` froze at 0.3481 and `exterior_facade` at 0.349, both
legitimately inside `[0.15, 0.35]`; if the full exterior subject simply does not fit under
0.35 at frame 1, every producer will now report shrinking slack and the last one still
abstains, correctly. The mechanism makes that visible earlier and to the right unit; it
does not make an unsatisfiable band satisfiable, and it does not by itself close that shot:
both room layer caps are spent, so validating it end to end needs a reviewed operator
rematerialization or a fresh shot. The slack is measured on the union as built so far; it
does not predict how much a pending producer will need, because that geometry does not
exist yet. If a partial producer freezes
with the band spent anyway, the payer's abstention now names the sharers, and the layer-2
rematerialization that follows sees the finding (HIR-0191); the band itself is still layer
1's authority. Whether the ground island in the room shot actually extended the union is a
question for the rendered-subject metrics fixed in HIR-0196.
