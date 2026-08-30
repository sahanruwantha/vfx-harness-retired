---
id: HIR-0142
title: Provisional debt survives contract rebinding
status: accepted
introduced_in: unreleased
date: 2026-08-30
failure_class: rematerialization_erases_independent_reference_judgment_debt
mechanism: current_bundle_falsification_lineage
adr: ADR-0006
---

# Provisional debt survives contract rebinding

## Observed failure

Room 1046 Layer 2 run `20260830T083943Z-206dc6` rebuilt four units from an empty
scene and published the composed artifact after 11 executable checks passed. The composed
boundary logged “look-less unit claims — no critic look vote” and never rendered the f39
Workbench plate for independent reference judgment.

The immediately preceding current-bundle finding
`hf-ecee90136e34fd5a81e7` recorded that R51's `approved_start` had failed exactly that
judgment. It named the concrete roof-crown defect and was consumed by a transactional
replan. Rematerialization then rebound R51 from a `decision` resolution to several scene
contract ids. The build loader looked only for selected requirements whose current
resolution was still `kind: decision`, so the contract rebind silently erased the
qualitative debt and let mesh counts self-certify “isolated historic hotel.”

## Root cause

HIR-0137 compiled provisional debt from the selected requirement row but had no durable
lineage after replanning. HIR-0139 correctly preserved decision id, strength, bundle hash,
and concrete observation in the hypothesis-falsification record, but the composition loader
did not consume that authority.

Mechanical contracts can prove that the producer graph now contains mass, roof, sign, site,
and streetlights. They cannot prove that their composed silhouette reads as the specific
reference. Rebinding producer contracts is not a `confirmed_outcome` for that judgment.

## Decision

- The selected requirement's provisional `decision` resolution remains the primary source
  of build-time qualitative debt.
- In addition, a current-bundle hypothesis-falsification record for the same layer and
  requirement keeps `approved_start` or `planner_start` debt active after the selected
  requirement is rebound to contracts.
- The proposition comes from the selected/base requirement statement; the finding supplies
  typed decision identity and strength. Model prose is not reconstructed from history.
- Findings pinned to another bundle are inert.
- A selected `confirmed_outcome` retires the old provisional finding deterministically.
- The existing HIR-0137/HIR-0139 composed judge and producer-closure path handles pass or
  failure; no new critic authority or mutation scope is introduced.

## General mechanism

`_load_provisional_decisions` now reads the layer's durable work-unit state alongside the
selected bundle and view. `_provisional_decisions_for_layer` merges exact current selected
decisions with current-bundle `falsifications[].decisions`, deduplicated by requirement id.
The ordinary composition-unit compiler receives the resulting debt card, renders Workbench
solid through the sealed camera, and requires qualified reference judgment.

This is strict lineage, not proximity: the record must carry the selected bundle hash and a
typed provisional strength. Prior-generation findings, arbitrary contract gaps, and score
history cannot reopen judgment.

## Rejected alternatives

- Treat the new scene contracts as confirmation: counts and bbox bands cannot decide
  historic-reference identity.
- Copy the previous selected requirement row into the new materialization: rematerialization
  designs against sparse current authority and must not carry prior rows by proximity.
- Always critic-judge look-less compositions: genuinely executable-only layers with no
  provisional qualitative debt must stay judgment-free.
- Parse the old critic transcript: transcripts are evidence history, not selected authority.

## Validation

Unit tests prove a selected contract resolution still compiles provisional debt from an
exact current-bundle finding, while another-bundle findings and a selected confirmed outcome
remain inert. Existing composition tests prove that debt creates a Workbench-solid qualified
claim and that a failure publishes a concrete producer-closure finding.

Production validation is a bounded Layer 2 revalidation. The composed canonical must announce
R51 debt, render f39 through the sealed camera, and either pass qualified judgment or publish a
new typed finding; executable mesh counts alone may not seal it.

## Release and rollback

No persisted schema changes. Rollback permits a rematerialization to launder a falsified
provisional decision into mechanical contract ids and is unsafe.
