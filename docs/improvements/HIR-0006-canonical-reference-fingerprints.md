---
id: HIR-0006
title: Use one typed metric registry for reference fingerprints
status: accepted
introduced_in: unreleased
date: 2026-08-21
failure_class: conflicting_image_metric_authority
mechanism: canonical_structured_reference_fingerprint
adr: ADR-0003
---

# Use one typed metric registry for reference fingerprints

## Observed failure

The planning run paid a repair round for five halation findings because `measure_ref` and
`frame_halation` measured the same plate through different image sizes and implementations.
Fingerprints then relied on prose to explain which number meant which implementation.

## Root cause

Exposure/band/halation feedback lived in Blender tool helpers while executable image checks used
`evidence.metrics.look_vector`. Grounding regex-parsed prose and recomputed some fields through
each implementation. A metric name therefore did not uniquely identify an algorithm.

## General mechanism

- Define `vfx-harness.look-vector/v1` as the canonical fingerprint metric set.
- Route `measure_ref`, grounding, Blender metric feedback, and frame image checks through
  `look_vector`.
- Store new acceptance fingerprints as `{metric_set, values}` with canonical metric ids.
- Validate every structured id and numeric value; unknown ids block instead of being ignored.
- Preserve legacy prose parsing as a read path for existing shots, without allowing it to define
  new metric identities.

## Rejected alternatives

- Increase grounding tolerance until both implementations agree: this hides instrument drift
  and makes real deviations less detectable.
- Keep both numbers and disambiguate in prose: downstream code would still infer authority from
  text, recreating the same defect.
- Immediately reject every legacy string fingerprint: existing authored plans remain useful and
  can be migrated on their next planning transaction.

## Validation

Unit tests derive a structured fingerprint from an image, reproduce every claim through the same
registry, and reject an invented metric id. The live legacy shot now reports the five historical
halation values against the canonical instrument instead of switching algorithms by consumer.

## Remaining limitations

Legacy prose is still accepted and regex-parsed. It is a compatibility reader only; newly prompted
plans emit structured fingerprints.
