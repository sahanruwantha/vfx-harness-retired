---
id: HIR-0256
title: Critic qualification binds protocol and authority separately from observations
status: accepted
introduced_in: unreleased
date: 2026-09-08
failure_class: changing_observations_invalidate_every_qualification
mechanism: typed_rubric_authority_and_observation_context
adr: ADR-0012
---

# Critic qualification binds protocol and authority separately from observations

## Observed failure

The native qualification profile hashed the complete rendered critic prompt. That
prompt included the candidate basename and measured evidence values. Reproducing
`critic_prompt` from committed source with `probe.png`/value 1 and
`canonical.png`/value 2 produced different prompt hashes despite unchanged rubric,
claim scope and target. The admission contract would therefore require a different
credential for each new observation. The offline reproduction is recorded in
`/tmp/vfx-critic-protocol-reproduction.json`.

## Root cause

Instructions, selected authority and candidate observations shared one text field.
Hashing all of it correctly preserved identity but made calibration impossible to
reuse. Omitting the hash or normalizing rendered strings would lose the contract
without identifying which inputs may legally vary.

## Mechanism and ownership

VFX constructs an immutable typed `CriticPrompt` with separate rubric, authority and
observation JSON. The rubric preserves medium, review-role and evidence interpretation
rules. Selected target/scope and claim descriptions remain in the qualified profile.
Candidate/reference paths, measured values, focus metadata, frame metadata and prior
score are observation data. The observation projection retains the fields the prior
prompt actually exposed; it does not indiscriminately forward internal evidence data.

Native invocation, input and observation reports use v2. Qualification binds the
rubric, selected authority and claim semantics, context schema, image representation,
response schema and dispatched provider configuration. Every call still records the
exact observation digest and full-context digest. Observation labels must agree with
attached source paths and focus order. The independent calibration evaluator reopens
journal requests, validates the typed sections, reconstructs their exact context and
rechecks source bytes. A rehashed report cannot replace those records.

Flynn's existing required-context, image transport, configuration, usage and SQLite
mechanisms suffice. Calibration policy and source interpretation remain in VFX.

## Rejected alternatives

- Dropping prompt identity would let different judging instructions share a credential.
- Replacing filenames in completed strings would be heuristic protocol inference.
- Moving scope or claim descriptions into unbound observations would weaken ownership.
- Treating passing offline adapters as live qualification would invent model capability.

## Validation

The committed-code reproduction shows the old hashes differ, the new rubric and
authority match, and the new observation digests differ. The first focused gate passed
105 tests; the expanded gate passed 168, followed by 27 final rubric checks. The first
full regression exposed a stale raw-autonomy-read inventory: the removed prompt
formatter no longer reads that flag to label evidence. All workers were stopped,
and the expected inventory was reduced to reflect the actual removal. That failed,
interrupted run is not validation. All 154 architecture tests then passed. Final
source review removed the now-unused image-description formatter, after stopping
the restarted workers; all 38 final image/rubric/architecture checks passed. Neither
stopped run is counted as validation. The completed full regression in
`/tmp/vfx-spike-regression-vuzekgk_` reported 3,610 passed and six failed: all six
consumer cases still supplied the retired string prompt instead of `CriticPrompt`.
The fixtures now supply the typed prompt and image tuple; their qualification and
acceptance assertions remain unchanged. All 19 affected consumer/guard/contract tests
passed, and complete-source Ruff passed. The final frozen-source regression passed
all 3,616 tests with 121 warnings in `/tmp/vfx-spike-regression-x30yf23a`: shard
counts 890, 909, 909 and 908, all four workers exiting zero. This validates the
protocol mechanism; it does not qualify a live model.

## Release and rollback

Old v1 native critic reports/profiles are unsupported for calibration and must be
requalified; no compatibility inference is provided. Existing selected credentials
cannot acquire v2 admission merely by editing their hashes: their complete measured
proof must verify under the new protocol. Qualification remains separate from plan
selection and VFX acceptance.

## Remaining limitations

One credential still selects one exact role/frame/image profile. Multi-profile
selection, complete layer/debt/acceptance ownership and reviewed live calibration
remain required. This change does not declare the production cutover complete.
