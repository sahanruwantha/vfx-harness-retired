---
id: HIR-0053
title: A runtime image payment needs candidate and adversary provenance
status: proposed
introduced_in: unreleased
date: 2026-08-27
failure_class: stale_runtime_image_payment
mechanism: provenance_bound_image_payment_v2
adr: ADR-0008
---

# A runtime image payment needs candidate and adversary provenance

## Observed failure

Run `20260827T155157Z-95b80e` built Layer 2 through `material_language` and
`lighting_bloom`, then stopped on `detail_scale_hierarchy_instancing`. The live builder discovered
old shot-root `renders/detail_before_*` and `detail_after_*` files and used them to pay required
f72/f150 image ids. `propose_checks` accepted f150 `region_sigma >= 48` from 49.28 versus 46.78.
Empty-scene replay measured 19.1623, and the canonical repair correctly recorded
`cannot_express_in_scope` rather than retaining a speculative geometry change.

## Root cause

`propose_checks` verified path existence and pixel discrimination but not run ownership, content
identity, candidate/unit identity, render settings, or the parent checkpoint. It also accepted a
model-selected adversary. The read guard blocked broad discovery but a narrower legacy render glob
remained legal, so filenames became accidental authority contrary to ADR-0002.

## Decision criteria

The mechanism must make stale evidence unrepresentable, keep the adversary independent of the
builder, preserve deterministic replay identity, fail closed on legacy rows, and add cost only in
proportion to the active unit's image debts.

## General mechanism

ADR-0008 defines `vfx-harness.image-payment/v2`. The harness renders pre-unit adversaries from the
prior script chain, render tools issue immutable current-run handles, `propose_checks` resolves the
pair without raw paths, and every consumer verifies the run manifest, SHA-256s, frame/settings,
unit digest, and parent-chain digest. Candidate probing runs image rows at canonical settings.
Generated thresholds must clear a deterministic replay margin.

## Rejected patch-level alternatives

Deleting the motivating shot's old renders, blacklisting `detail_before_*`, changing the f150
threshold, or adding prompt text about fresh files all leave arbitrary path authority intact.

## Validation

`tests/unit/test_image_payment_provenance.py` pins the legacy-path, model-selected adversary,
tampering, valid-v2, and threshold-margin cases. The relevant unit/architecture slice reports
`76 passed`. Full-suite and producing-path Layer 2 rerun remain required.

## Release and rollback

This is a strict migration. Rollback means reverting ADR-0008 and the implementation together;
there is no supported mode that re-enables unproven legacy rows.

## Remaining limitations

The current envelope addresses local structured runs. Remote content-addressed evidence would need
an equivalent immutable locator while retaining the same hashes and parent/unit identities.
