---
id: HIR-0253
title: Native image units must fit context and identify the layer checkpoint
status: accepted
introduced_in: unreleased
date: 2026-09-08
failure_class: native_image_unit_layer_integration
mechanism: compact_image_observation_and_canonical_render_candidate_identity
adr: ADR-0012
---

# Native image units must fit context and identify the layer checkpoint

## Observed failure

A real Blender gate built a camera layer, then a scene-only predecessor and an executable
image unit in a dependent layer. The image unit first refused its post-capture request:
`Required context item 'selected-observation' exceeds context budget`. After narrowing
that feedback, canonical image checks passed but real layer completion refused:
`unit evaluation candidate bytes do not identify the frozen checkpoint`.

The new `test_flynn_image_layer.py` reproduces both failures against the prior native
implementation. Its unit order intentionally differs from authored order. Planning and
model judgments are seeded test inputs; replay, payments, checkpoints and receipts are real.

## Root cause

Capture feedback repeated run, claim, unit and prior-chain ownership fields in both image
records, although those bindings were already enforced by the guard and retained in the
report. The dependency context plus current source, debt cards and that redundant result
crossed the fixed 12,000-character cap.

The native evaluation publisher always named the script as its candidate artifact. The
layer checkpoint instead derives that identity from the ledger's canonical primary render.
The isolated image test had copied the native implementation's script-hash assumption into
its manually prepared checkpoint, hiding the mismatch at the actual layer boundary.

## Mechanism and ownership

The image observation is now `vfx-harness.unit-image-observation/v2`. It carries each
image's payment handle, frame, path, pixel digest, source-candidate digest and render settings,
plus the full report locator/digest. The guarded registry and report still retain every
ownership field, and the journal preserves the returned observation. There is no text
truncation or context-cap increase.

The native evaluation receipt names the canonical primary render for image units and the
script for scene-only units. The replay closure still independently names the exact script.
Completion readers and checkpoint derivation are unchanged.

Production routing and explicit native execution share one eligibility predicate. Flynn
executes procedural units with executable required claims at every judge point, without
provisional requirements, using EEVEE when pixels are required. Unsupported unit work keeps
its existing migration path. A failure after native selection never retries through another
engine. Layer look judgment remains an independent obligation; executable unit claims do
not remove it. These changes are VFX policy and need no SDK extension or ARC test.

## Rejected alternatives

- Enlarging context or dropping required source/claims would hide the feedback duplication.
- Teaching the layer checkpoint to use the script would discard canonical image identity.
- Mocking completion would reproduce the unit test's blind spot.
- Removing the composed look judgment would weaken the existing layer acceptance boundary.

## Validation

The focused routing, compact capture, real layer and production driver gates passed
all 30 tests in 263.02 seconds. The frozen-source full regression passed all 3,398 tests
in four isolated processes (840 + 840 + 859 + 859), with the longest taking 998.38
seconds. It emitted 84 existing Pillow `getdata` deprecation warnings. Complete-source
Ruff and `git diff --check` passed.

The updated isolated image lifecycle test now freezes the canonical render digest,
separately asserting that it differs from the script digest. Capture contracts verify
the compact v2 field set and complete ownership bindings in the source-verified report.
Production routing tests cover unsupported media, missing executable coverage, qualitative
claims, provisional requirements, generated construction, budget/configuration refusal
and propagation without another engine. The driver test rejects substituted composed
script bytes despite a zero child exit while preserving camera bytes and unit records.

The gates use scripted unit inference and a separately scripted layer critic. They prove
transport and source-verified publication, not visual quality or live model performance.

## Release and remaining limitations

This is an unreleased ADR-0012 migration increment. The compact observation has an explicit
new schema; existing complete report and receipt schemas are unchanged. Prior invalid image
completion records gain no recovery or resume authority. Rollback restores the prior
production unit routing; it must not reinterpret mismatched checkpoints as valid.

Solid, qualitative and generated unit execution, the layer critic and complete Claude-free
installation still require migration. This gate does not exercise live planning, the public
CLI preflight, full-shot acceptance, or heterogeneous visual-quality evaluation.
