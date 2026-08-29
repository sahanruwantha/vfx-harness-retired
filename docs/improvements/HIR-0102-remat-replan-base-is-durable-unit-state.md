---
id: HIR-0102
title: Rematerialization replan base is digest-bound durable unit state
status: accepted
introduced_in: unreleased
date: 2026-08-29
failure_class: republished_global_erased_replan_base
mechanism: digest_bound_state_replan_diff
adr: ADR-0004
---

# Rematerialization replan base is digest-bound durable unit state

## Observed failure

After HIR-0101 allowed Layer 1 rematerialization to run, production run
`20260829T083336Z-91fc7b` staged a legal `camera_aim_target` →
`camera_rig_foundation` DAG, passed materialization validation and a clean plan
gate, and published JIT view `6c51b097390cdf86…` against global bundle
`1d3b117b174814d2…`. The following `apply_replan` failed:

```text
ValueError: replan base layer/plan hash does not match active state
```

Durable Layer 1 state still correctly named accepted predecessor units
`cam_path_core` and `cam_parallax_proxies` at plan hash `a257a7e5…`.
Because global republication made the old JIT view inert before remat started,
the planner captured the new sparse selected layer (zero units) and its hash as
the old base. Publication therefore succeeded while the audited state move
could not identify its predecessor.

## Root cause

Rematerialization has two identities at its transaction boundary: the currently
selected plan/view is the replacement design authority, while durable work-unit
state is the accepted predecessor authority. They normally coincide, but global
republication deliberately separates them. A sibling materialization can also
change the combined selected `layers.json` hash without changing this layer's
unit DAG (HIR-0040).

`_rematerialize_layer` assumed selected-layer bytes and selected-plan hash were
always the old replan base. `apply_replan` already recognized an empty sparse
base plus populated state, but classified every state-only unit as an orphan.
That is correct for an unknown/incomparable base, not for current-schema state
whose exact unit ids and SHA-256 digests are recorded.

## Decision criteria

- A populated durable state record supplies remat's old plan hash.
- If selected-layer units match the durable ids/digests, normal WorkUnit diffing
  remains in force.
- If the selected layer no longer reconstructs that accepted DAG, current-schema
  stored unit digests form a closed state-backed base: ids and hashes are compared
  directly to the replacement WorkUnits.
- Matching digests preserve status/checkpoint; changed units and their replacement
  downstream closure reopen; removed units are superseded as amendment effects.
- Removed accepted units are not `orphaned` and need no `--discard-accepted`: the
  validated replacement DAG is the authority retiring them (HIR-0052).
- Missing unit hashes and cross-schema digests remain incomparable and fail closed.
- The state move and its full effect record retain the existing atomic write.

## General mechanism

Before design, `_rematerialize_layer` pins `old_plan_hash` to durable state. It
uses the selected layer as `old_units` only when `validate_current` proves the
same ids/digests; otherwise it deliberately passes an empty structural base to
request a digest-bound state diff.

Remat calls `apply_replan(state_backed_base=True)`, a typed authority that is not
granted to ordinary bundle-to-bundle `units replan`. The latter keeps treating
state rows absent from both bundle DAGs as accepted orphans. With the explicit
remat authority, `apply_replan` derives the diff from stored unit hashes when the
state uses the current digest schema:

- `added = replacement ids - state ids`;
- `removed = state ids - replacement ids`;
- `changed = shared ids whose stored digest differs`;
- `invalidated = replacement downstream closure of added/changed`;
- `preserved = shared digest matches outside that closure`.

Retirement uses the state ids as the old identity, so changed/removed slots move
to supersession history and replacement slots start pending. No old unit prose,
prior transcript, or filename inference enters the transaction.

## Rejected patch-level alternatives

- `--discard-accepted` would retire checkpoints without comparing digests and is
  explicitly not the door on remat.
- Hand-editing the state plan hash or statuses would bypass the audited replan.
- Treating every state-only row as an orphan confuses absent selected bytes with
  absent identity; current-schema unit hashes are exact identity evidence.
- Searching old transcripts or guessing an earlier JIT view from recency would
  make lifecycle authority heuristic and ambiguous.
- Moving state before replacement validation would let a failed design supersede
  accepted work.

## Validation

- `test_rematerialize_uses_digest_bound_state_after_global_republication`
  reproduces a selected replacement row/hash with an accepted predecessor state,
  then proves remat records the durable old hash and retires the old camera DAG.
- `test_digest_bound_state_replan_preserves_matches_without_old_view` proves a
  heterogeneous mixed case: one matching accepted rig survives, one changed unit
  and new dependant reopen, and one removed accepted unit is superseded rather
  than orphaned.
- Existing HIR-0052 fixtures continue to prove ordinary WorkUnit diffing and the
  fail-closed unusable-base path.
- Targeted state/remat result: `4 passed in 3.10s`; the explicit authority
  boundary regression then passed `3 passed in 2.26s`.
- Relevant authority/materialization/state suite: `163 passed in 7.21s`.
- Full repository suite: `549 passed in 43.61s`.
- `.venv/bin/ruff check src tests`: `All checks passed!`.
- `.venv/bin/vfx --help`: exit 0.
- Producing rerun `20260829T084759Z-067de2` selected the digest-bound base at
  startup, materialized a clean three-unit replacement, and moved state from
  `a257a7e5…` to `88b64180…` at revision 35. Its audit records
  `removed=[cam_parallax_proxies, cam_path_core]`, the three replacement units as
  added/invalidated, no orphaned ids, and no discard. All replacements are pending;
  the two accepted predecessors are retained in supersession history.
- The materialization took 19 turns, 698.5 model seconds, and $2.0884; bounded
  first-unit planning took 4 turns, 95.3 model seconds, and $0.3899. The run
  completed in 798.8 seconds with exit 0, compared with run
  `20260829T083336Z-91fc7b` publishing clean authority after 12 turns/$1.3380 but
  then terminating on the base mismatch.
- A fresh `.venv/bin/vfx evals plan /home/sahan/Desktop/vfx-test` reports CLEAN
  for 7 layers, 34 axes, and 11 scene contracts.

Implementation commit: `e119d12` (`fix: reconcile remat from durable unit digests`).

## Release and rollback

No schema migration. The mechanism consumes only the existing current-schema
`unit_hash` and `plan_hash` fields. Rollback restores the publish-then-fail state
split after global republication and is unsafe.

## Remaining limitations

Materialization publication and work-unit state movement remain two ordered
atomic writes, not one filesystem transaction. A process death between them
still leaves a published replacement with old state; rerunning remat now has a
deterministic digest-bound recovery path instead of requiring a wipe.
