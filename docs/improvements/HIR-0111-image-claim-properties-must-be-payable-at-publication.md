---
id: HIR-0111
title: Image-claim properties must be payable at publication
status: proposed
introduced_in: unreleased
date: 2026-08-29
failure_class: free_form_image_property_creates_unpayable_runtime_debt
mechanism: registry_derived_image_property_vocabulary
adr: null
---

# Image-claim properties must be payable at publication

## Observed failure

Layer 2 run `20260829T140952Z-d9e776` materialized cleanly, then spent 628.5 seconds and
$3.388 on `iris_rim_light_emitters` before stopping as `hypothesis_falsified`. The two
required image debts declared property `rim_light_emission_glow`. `propose_checks`
rejected `frame_points`, `region_max`, `frame_halation`, and `region_mean` because no
registered metric could certify that free-form property. The typed finding was
`hf-a8634cb4fe2bfca28326`.

The builder correctly abstained, but only after building the candidate, rendering both
frames, and guessing four metrics. The contradiction existed in selected plan authority
before the first model token was spent.

## Root cause

Image debt compilation accepted an arbitrary claim `property`. Runtime payment was
already strict: an abstract family (`frame_delta` or `render_region_stat`) maps to its
registered metrics, while an exact metric property maps only to itself. Materialization
and the unit-ticket schema did not apply that same finite vocabulary, so they could
publish a debt whose payer set was empty.

## General mechanism

The canonical metric registry now derives one closed image-property authoring vocabulary:

- `frame_delta`, payable by the registered `frame_*` family;
- `render_region_stat`, payable by the registered `region_*` family; and
- every exact registered metric id, such as `frame_halation`.

Required image claims with an `image_contract` binding must name one of those properties.
The `stage_materialization_unit` schema enumerates the set before generation. The locked
staging transaction refuses an unpayable property before candidate bytes change. Full
materialization validation reports every such claim with its JSON pointer and accepted
set, and the plan gate mirrors the rule for already-selected or externally assembled
views.

The vocabulary is registry-derived rather than a copied metric list. Adding a canonical
metric automatically makes its exact id available; adding a new abstract property family
still requires an explicit semantic mapping.

## Rejected patch-level alternatives

Mapping `rim_light_emission_glow` directly to `frame_halation` would special-case one
shot-authored phrase and silently equate distinct appearance concepts. Letting any metric
pay any property would erase claim meaning. Keeping runtime abstention as the only guard
would continue spending build and render budget on a contradiction known at publication.
Prompting the builder to choose `frame_halation` would leave invalid authority selected.

## Validation

`test_unpayable_image_property_is_a_publication_gap` pins the shared compiler.
`test_image_property_vocabulary_is_registry_derived_and_schema_enumerated` pins the
closed staging schema. `test_unit_staging_refuses_unpayable_image_property_before_write`
pins transaction atomicity. `test_materialization_rejects_unpayable_image_property` pins
the pointer-addressed hard boundary, and
`test_plan_gate_mirrors_unpayable_image_property` pins selected-view rejection.

Against the motivating selected view, `vfx evals plan /home/sahan/Desktop/vfx-test` now
returns exactly two `image-property-vocabulary` findings in 3.1 seconds, one for each
unpayable debt, instead of discovering them after the 24-minute build invocation.

The producing validation is a Layer 2-only rematerialization on bundle
`1d3b117b174814d279f315840e1b0ef8240766f913a37096c21e629eeb27dde3`, followed by the
normal Layer 2 build and empty-scene composed replay.

## Release and rollback

No schema migration is required. Existing views with unpayable properties fail closed and
must rematerialize; payable abstract families and exact metric properties keep their
existing payment semantics. Revert the schema, staging, validation, and gate changes
together only if claim property is replaced by a typed metric-family field with an
equivalent closed compiler.

## Remaining limitations

This mechanism proves that a payer exists, not that a proposed threshold will pass the
candidate, fail the immutable adversary, and clear noise. Those remain build-time
measurements under `vfx-harness.image-payment/v2`.
