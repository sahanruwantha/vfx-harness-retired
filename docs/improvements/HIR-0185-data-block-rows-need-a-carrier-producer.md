---
id: HIR-0185
title: A data-block property row needs a carrier-producing unit in its closure
status: accepted
introduced_in: unreleased
date: 2026-09-03
failure_class: unpayable_data_block_row_reached_builder
mechanism: data_block_carrier_gate_at_materialization
adr: null
---

# A data-block property row needs a carrier-producing unit in its closure

## Observed failure

Run `20260903T100335Z-fa5dbb` on `artifacts/room_1046_opening` (main `6faa97c`): layer 2's
`streetlight_flicker` unit bound `streetlight-flicker-schedule`, a `keyframe_schedule` on
`data.energy` over `control_roles: ['ground_streetlight_flicker']`. The unit mutates only a
control role; the streetlights it was meant to modulate had been built by `ground_island`
as mesh fixtures, so no Light data-block existed and none could be created in scope. The
builder spent 18 turns of typed reads proving that and recorded `cannot_express_in_scope`
(`hf-26516d9cbbf07fe51c84`, fault owner `ground_island`). The abstention was correct; the
row was unpayable the moment it was staged.

## Root cause

Validation gap. `object_property` rows on `data.*` already classify into light or camera
families for write-cluster derivation (HIR-0174 carriers judge them at runtime), but no
materialization or plan-gate predicate asked whether any unit in the binding unit's
dependency closure, or an earlier materialized layer, writes that carrier family. A
control unit could therefore schedule a Light property nobody creates.

## Decision criteria

- Fail closed at the earliest owning boundary (materialization staging and finalize, plan
  gate on published views), never at builder spend.
- Enumerate, don't imagine: the registered data-block properties are a closed vocabulary;
  an unregistered `data.*` path is a finding, not a silent pass.

## General mechanism

1. `domain/data_block_carriers`: the light and camera property vocabulary already used by
   the write-cluster classifier is public (`LIGHT_PROPERTIES`, `CAMERA_PROPERTY_PREFIXES`);
   `row_data_block_paths` extracts every `data.*` path a `keyframe_schedule` or
   `object_property` row measures; `data_block_carrier_gaps` walks each binding unit's
   dependency closure for a unit whose derived write clusters include the carrier family,
   or an earlier layer's families, and names the producers outside the closure.
2. `validate_materialization` reports each gap as a collectable finding on the row (so the
   stage call refuses it under HIR-0180 and finalize refuses it), with
   `earlier_layer_write_families` derived from the already-materialized base layers; the
   plan gate reports the same predicate as `data-block-carrier` on published views.

## Rejected patch-level alternatives

- Letting a control unit create Lights (a mixed cluster the atomicity gate exists to
  refuse).
- Teaching the builder to abstain faster (the row is wrong before the builder exists).

## Validation

- `src/tests/unit/test_data_block_carriers.py`: property families and row paths; the
  observed shape (mesh producer plus control scheduler) is a gap naming the light family;
  an earlier light layer satisfies it; an unregistered `data.*` path is named.
- `src/tests/unit/test_finalizer_empty_journal_abstention.py`: a live
  `cannot_express_in_scope` with no accepted call writes the harness-authored no-op
  candidate naming the contract ids and never opens a model finalizer session; an empty
  journal without the abstention, or accepted calls with it, still distil through the
  finalizer.

## Release and rollback

Unreleased. Rolling back restores builder-time discovery.

## Remaining limitations

- The carrier producer is matched by family only; that a light unit's roles cover the
  scheduled control's hosts is still proven at build time by the carrier rule.
