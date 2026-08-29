---
id: HIR-0103
title: Unresolved write-family rejection names legal witnesses
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: write_family_rejection_forces_contract_guessing
mechanism: unresolved_role_witness_card
adr: null
---

# Unresolved write-family rejection names legal witnesses

## Observed failure

Layer 2 rematerialization run `20260829T092954Z-133079` staged an unlock-control
unit over two mutated roles in one namespace. An `onset_order` row selected the lead
role and compared the trail role. Staging rejected the namespace as unresolved. The
materializer added `curve_derivative_max` for the lead and received the same rejection;
only a third attempt with `animation_count` selecting both roles staged successfully.

The first rejection was at 316.7 seconds, the repeated rejection at 339.5 seconds,
and the successful call at 361.7 seconds: two rejected staging calls and 45 seconds of
schema discovery inside one bounded unit. The phase completed in 21 turns, 621.9 model
seconds, and $1.9066.

## Root cause

The atomicity gate knew which mutated role remained unresolved and which instrument
family the sibling role had already established, but discarded both facts from its
finding. The rejection said only to bind a write-kind contract. It did not enumerate
the registered kinds for the derived family or explain that `compare_roles` is a
read-only selector and cannot witness mutation. The model therefore had to probe the
gate with different legal contract kinds.

This is an agent-instrument defect at the derived atomicity boundary, not a reason to
relax atomicity or enlarge the materialization prompt.

## Decision criteria

- The gate continues to fail closed when any mutated role lacks a typed family.
- The finding names every unresolved mutated role.
- When sibling roles establish one family, the finding names that family and the
  registry-backed contract kinds that can witness it.
- The finding enumerates the mutation-selector fields and states that comparison
  selectors are observation-only.
- Guidance is derived from the same registries and selector resolver as publication;
  no duplicated family catalog or shot vocabulary enters prompts or core behavior.

## General mechanism

`unresolved_family_guidance` compiles a typed next-action card for each unresolved
namespace. It reuses `KIND_INSTRUMENT_FAMILY`, `bound_rows_for_unit`, and the exact
write-selector field list used by atomicity. A partially resolved keyframe namespace,
for example, now reports the unresolved roles, family `keyframe`, its registered
write-kind witnesses, the accepted selector fields, and the non-authority of
`compare_roles`.

The card is appended to the existing staging/materialization finding. Validation and
candidate write semantics do not change.

## Rejected alternatives

- Treating `compare_roles` as mutation evidence would let a read-only comparison grant
  write authority.
- Defaulting a multi-role namespace to control would revive the false-family defect
  prohibited by HIR-0095.
- Adding examples to the prompt would duplicate registries and still become stale.
- Accepting the third successful guess would preserve recurring mechanical discovery
  cost in every materialization session.

## Validation

- `test_unresolved_family_names_roles_and_registered_witnesses` reproduces a lead/trail
  onset row and proves the finding names the unresolved trail role, derived keyframe
  family, registered witness kinds, mutation selector fields, and read-only comparison
  rule.
- Existing unresolved light/volume and partial-family fixtures remain fail-closed.
- Targeted atomicity result: `4 passed, 29 deselected`; the broader atomicity and
  materialization-authority suites passed `111` tests in 5.62 seconds.
- Full repository suite: `550 passed in 43.49s`.
- `.venv/bin/ruff check src tests`: `All checks passed!`.
- `.venv/bin/vfx --help`: exit 0.
- Producing rerun `20260829T094605Z-a1b1cb` exercised the new finding at 352.3
  seconds. It named unresolved roles `iris.blades` and `iris.blades.wave2`, family
  `keyframe`, all six registry witnesses, the mutation selector fields, and the
  read-only comparison rule. The very next staging call succeeded at 399.0 seconds
  after narrowing mutation to the two actually keyed subsets. Unresolved-family
  rejections fell from two to one; time from first rejection to success was 46.7
  seconds versus the 45.0-second baseline, so reliability improved while latency did
  not.
- The rerun later reached a validation-passing candidate but exhausted its 24-turn
  materialization budget after a composition repair. HIR-0027 left the selected view
  unchanged. That terminal cause is independent of the witness card and exposed a
  separate typed unit-retirement gap; it is not counted as Layer 2 completion.

Implementation commit: `05c86f3` (`fix: teach unresolved write-family witnesses`).

## Release and rollback

No schema or authority migration. Rollback restores trial staging against information
the gate already computed, so rollback is unsafe.
