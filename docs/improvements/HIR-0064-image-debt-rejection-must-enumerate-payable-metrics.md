---
id: HIR-0064
title: Image-debt rejection must enumerate payable metrics
status: proposed
introduced_in: unreleased
date: 2026-08-28
failure_class: unpayable_frame_delta_and_metric_guess_loop
mechanism: property_metric_family_and_rejection_enumeration
adr: null
---

# Image-debt rejection must enumerate payable metrics

## Observed failure

Run `20260827T195827Z-7c84ac` reached a gate-clean `lighting_atmosphere` unit whose
`atmoscale-f150` image debt declared property `frame_delta`. The builder visibly changed the
candidate and measured the change, but `propose_checks` rejected every one of its 16 registered
metrics because the property compiler required a literal metric named `frame_delta`, which the
registry does not contain. The builder made 20 proposal calls, consumed 138 turns and 31 minutes,
then correctly recorded `hf-fa1dd902057ef85ec3b7` as `unpaid_image_debt`.

## Root cause

The payment compiler mapped `render_region_stat` to the `region_*` metric family but omitted the
corresponding mapping from `frame_delta` to `frame_*`. `propose_checks` already proves every kept
scalar against the harness-captured pre-unit adversary, so a `frame_*` threshold that passes only
on the candidate is the executable delta proof. Rejection reported only the mismatch even though
the accepted registry is finite and known.

## General mechanism

`frame_delta` image debts now accept the canonical `frame_*` scalar family. Necessity remains
unchanged: the chosen scalar must pass on the candidate, fail on the immutable pre-unit adversary,
and clear the measured noise margin. When a proposed metric cannot certify an owed property, the
rejection enumerates exactly the compatible registered metrics; if none exist, it directs the
builder to typed `unpaid_image_debt` abstention immediately.

## Rejected patch-level alternatives

Adding a fake pair-valued metric to the single-image registry would duplicate the adversary
comparison already enforced by `verify_necessity`. Allowing every metric to pay every property
would erase claim meaning. Prompting the builder to try `frame_mean` would leave the compiler
contradiction and the next unknown property unchanged.

## Validation

`test_frame_scalar_pays_frame_delta_proved_against_adversary` pins the property family.
`test_property_mismatch_enumerates_compatible_metrics` pins finite next actions in the rejection.
The producing validation is a fresh `lighting_atmosphere` build that pays `atmoscale-f150` through
the real candidate/adversary necessity gate.

## Release and rollback

No schema migration is required. Existing `frame_*` runtime rows for `frame_delta` debts become
payable only when their id, frame, axis, provenance, and necessity proof already match. Revert the
mapping and rejection hint together only if the image-debt card gains an explicit two-frame
operand and a new canonical pair metric replaces the adversary mechanism.

## Remaining limitations

The debt card still identifies one owed judge frame. Temporal comparison between two authored
shot moments belongs to an executable scene/image contract that declares both frames; this
mechanism proves the active unit caused an observable image change at the owed frame.
