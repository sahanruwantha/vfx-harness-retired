---
id: HIR-0257
title: Select independently measured critic profiles as one explicit claim credential
status: accepted
introduced_in: unreleased
date: 2026-09-09
failure_class: single_profile_cannot_cover_owned_critic_roles
mechanism: exact_measured_profile_set_selection
adr: ADR-0012
---

# Select independently measured critic profiles

## Observed limitation

After HIR-0256 separates rubric and authority from observations, a claim still selects
one prompt and one invocation hash. Observer, focus, evidence audit, tie breaker and
per-frame calls cannot all match that one credential. Reusing it regardless of the
actual invocation would discard measured authority. The old selected-layer validator
also opened files inside the domain package and accepted asserted passing rates;
only native admission reopened measured sources.

## Mechanism and ownership

A claim now selects exactly `suite`, `artifact` and `artifact_sha256`. The closed
`vfx-harness.critic-qualification/v2` artifact binds a hash-selected calibration-set
request and distinct profiles ordered by invocation digest. Each profile retains its
own rubric/model/image configuration identity, rates and request/evaluation proof.
Every member measures the same exact claim and suite. Publication re-derives all
members, and both layer loading and native admission reopen their complete source
proof. An actual invocation must match exactly one measured member before inference.
No missing member, duplicate, replaced source or stored metric self-certifies success.

The domain validates record values without filesystem access. VFX adapters own
source reads, measured publication, selected claim semantics and admission. Flynn's
existing execution and journal contracts suffice. No SDK or ARC change is needed.
The single-profile claim and old artifact formats are rejected; no compatibility
interpretation converts an old prompt field into a profile set.

## Validation

An isolated development copy passed 14 pure record checks, two initial admission
checks and five real-journal multi-profile/source-substitution checks. A broader
consumer run passed 77 cases and failed one obsolete error-message assertion; the
corrected identity case passed independently. These checks overlap and do not prove
live model qualification. Complete-source Ruff passed after integration.

The integrated gate completed with 264 passed and two old-format architecture fixture
failures in `/tmp/vfx-profile-set-integrated-gate.log`. The migrated architecture and
consumer fixtures plus the new domain-boundary checks passed all 11 selected cases.
The architecture case now refuses asserted rates regardless of the passing flag.
A separate loader test passed using independently measured profiles, then refused a
changed member evaluation. Two architecture tests enforce filesystem-free domain
parsing. The final frozen-source regression passed all 3,639 tests with 121 warnings
in `/tmp/vfx-spike-regression-5ifjdpbi`: shards 896, 915, 914 and 914, all four workers
exiting zero. Complete-source Ruff and whitespace checks passed.

## Remaining scope

This supplies selection mechanics, not reviewed labels or automatic calibration.
Complete layer/debt/acceptance ownership, remaining native builders, removal of active
Claude dependencies and bounded qualified live end-to-end validation remain owed.
