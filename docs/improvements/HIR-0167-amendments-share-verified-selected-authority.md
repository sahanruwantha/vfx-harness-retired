---
id: HIR-0167
title: Amendment proposals share one verified selected-authority identity
status: accepted
introduced_in: unreleased
date: 2026-08-31
failure_class: producer_private_authority_identity_and_unsafe_amendment_classification
mechanism: shared_semantic_selected_authority_and_strict_amendment_v2
adr: null
---

# Amendment proposals share one verified selected-authority identity

## Observed failure

Planning, JIT materialization, and builder falsification each described the same selected
plan and consumer view with a producer-private digest payload. Run ids, pointer projections,
artifact subsets, and locally chosen field names could therefore give one underlying
authority different stop preconditions and action identities. A future idempotent amendment
adapter could not use those records as one compare point.

Two classification defects made that divergence unsafe rather than merely inconvenient.
Global planning distinguished a missing pointer from malformed, unreadable, or invalid
bytes internally, but could still reach an amendment-shaped path after selected authority
had failed to resolve. Builder falsification carried the original decision strength, yet
the amendment target accepted a separately supplied `changes_hard_constraint=False`; a
hard-constraint finding could consequently be described as an automatic plan amendment.

Selected plan verification also trusted less than the bundle claimed. A content-addressed
directory could be reused without rechecking its complete member set, and pointer, bundle,
manifest, or artifact symlink substitution was not rejected uniformly. An undeclared file,
changed manifest outcome, or damaged pre-existing root could coexist with a path that looked
like immutable authority.

## Root cause

There was no single domain assertion for effective selected authority. The old selected
bundle and selected view assertions authenticated a caller-chosen `selection_digest`, so
they proved only that one producer had serialized some local description. Selection
resolution, immutable-bundle integrity, stop classification, and amendment scope were
separate checks with no shared semantic identity.

The amendment contract compounded that gap. It did not bind the gate schema, gate policy,
validation scope, or required successor kind, and it let a caller restate whether a hard
constraint changed instead of deriving the legal route from the typed finding.

## Decision criteria

- Equal verified plan and effective-view semantics produce equal assertion and action
  identities across runs, relocations, and owning producers. Audit paths and timestamps do
  not become semantic progress.
- Only a genuinely missing global pointer is absent authority. Existing malformed,
  unreadable, stale, inconsistent, or symlinked authority fails closed.
- The bundle and effective consumer view are resolved together. A matching verified JIT
  view is effective; a valid JIT pointer for another bundle is inert only after it too is
  verified; an invalid JIT pointer never falls back to the sparse bundle.
- Every selected or reused immutable bundle is verified over its canonical manifest,
  authoritative path, exact member set, artifact bytes, and aggregate hash.
- A global amendment may start from absent or selected authority and must select a bundle.
  A layer amendment requires selected effective authority and must select a JIT view.
- The amendment proposal binds the exact owner, findings, plan-gate schema and policy, and
  structural validation scope. Old or caller-padded shapes fail strict parsing.
- A hard-constraint finding routes to a typed human question. It never reaches automatic
  amendment authority through a caller-supplied Boolean.
- These contracts remain proposals until an atomic public adapter, immutable commit,
  independent evaluator, and crash reconciliation path exist.

## General mechanism

`vfx-harness.selected-authority-state/v2` is the shared semantic assertion. It has exactly
two states:

- `absent`, with null bundle and effective view; or
- `selected`, with a bundle record and an effective-view record.

The bundle record binds its content digest, publishable gate outcome, and a semantic manifest
digest over the exact artifact hash map. The effective view binds source (`bundle` or `jit`),
its verified digest, and a semantic manifest digest over bundle identity, materialized layers,
and exact consumer-artifact hashes. A bundle-backed view has the bundle content digest. Run
ids, locators, publication time, and raw pointer bytes are deliberately not fields. Exact plan
and JIT pointer-byte hashes remain a separate audit observation for later compare-and-swap
work; changing their location does not manufacture semantic progress.

The resolver reads both selection pointers, completely verifies plan and any present JIT
authority, then rereads both pointers before returning. It retries a bounded number of changed
observations and otherwise fails. This prevents an ordinary one-pass mixed snapshot, but it is
not a revisioned or ABA-safe publication transaction.

Plan-bundle verification is centralized at the filesystem boundary. The current pointer has
one exact closed v1 field shape and must name its run-owned content-addressed root. The bundle
manifest has exact canonical fields and bytes, agrees with pointer, path, run, hash, and
outcome, and declares every supported artifact. Every member must be a regular in-root file or
expected directory; symlink components, missing or undeclared members, changed bytes, and an
aggregate-hash mismatch fail closed. A pre-existing content-hash root receives the same full
verification before pointer replacement, so a damaged collision cannot replace the previously
selected pointer.

Selection also revalidates the live planning inputs consumed by bundle provenance.
`vfx-harness.plan-provenance/v2` hashes every authored byte and records each append-only
decision ledger as an exact `{size, sha256}` consumed prefix. Every resolve rereads the authored
tree and those prefixes: same-size edits with restored mtimes, truncation, prefix replacement,
missing input structure, symlinks, special files, and unreadable members fail closed. A later
decision suffix remains legal because it did not contribute to the already-published plan.
`vfx-harness.plan-workspace/v2` uses the same input records, strict fields, and duplicate-key
rejection while staging. This removes the former size/mtime verification cache without making
ordinary obligation or assumption discharge stale the selected bundle.

`publish_validated_amendment` now uses target and postcondition v2. The target binds the shared
base-authority assertion, scope, optional layer, exact typed findings, owner authority,
`vfx-harness.plan-gate/v1`, its non-empty policy id, and
`validation_scope="structural_authority"`. It has no `changes_hard_constraint` field. The
postcondition binds those same values and the base assertion digest, plus the required
successor source: `bundle` for `global_plan`, `jit` for `layer_view`. Target/postcondition
validation compares every shared field exactly.

Global planning, materialization, and non-hard builder falsification now use that resolver and
recheck the complete selected-authority observation before publishing their stop. Corrupt
global selection routes to `harness_defect` and `route_engineering`, with no amendment action.
A builder finding that changes a hard constraint instead emits
`human_decision_required` with one typed `escalate_question` action and closed approve/reject
answer ids.

## Rejected patch-level alternatives

- Normalize each producer's old digest payload: three private projections would remain three
  authorities and would diverge again when another field was added.
- Treat malformed or unreadable `plans/current.json` as initial absence: corruption is not
  permission to publish over unknown authority.
- Ignore a malformed JIT pointer and use the sparse bundle: that silently changes the
  effective consumer view after integrity failure.
- Keep `changes_hard_constraint` on the amendment target and ask callers to copy it correctly:
  duplicated authority is what made the misclassification representable.
- Trust a directory because its name is a content hash: immutable identity requires exact
  members and bytes, not a plausible path.
- Make the amendment action dispatchable now: a typed proposal does not supply successor
  authoring, atomic selection, a receipt, or independent proof of progress.

## Validation

Pure-domain tests prove absent and selected assertion round trips, closed nested fields and
vocabularies, bundle/JIT source invariants, relocation-stable identity, digest sensitivity to
semantic bundle/view changes, amendment-v2 scope and successor rules, exact owner/gate/finding
binding, and strict rejection of v1 and legacy-field shapes.

Resolver fixtures prove true absence, run- and path-independent bundle identity, gate-outcome
sensitivity, matching JIT selection, verified stale-JIT inertness, malformed and symlinked JIT
refusal, and locator-independent JIT identity. Plan-authority fixtures inject damaged,
missing, symlinked, and undeclared members into existing bundle roots; tamper manifests and
outcomes; mutate authored bytes with preserved metadata; mutate or truncate a consumed decision
prefix; preserve a legal appended suffix; reject malformed workspace/provenance v1 records; and
prove failed verification preserves the prior pointer. Owning-stop fixtures prove
all three producers bind the shared assertion, corrupt global pointers route only to
engineering, and hard-constraint falsification routes to the typed human question.

These are domain, filesystem, and producer-boundary fixtures. They do not execute an amendment,
exercise concurrent pointer writers, or validate a controller.

Focused validation on 2026-08-31 passed 154 tests:

```text
.venv/bin/python -m pytest -q \
  src/tests/unit/test_selected_authority_assertion.py \
  src/tests/unit/test_authority_selection.py \
  src/tests/unit/test_plan_authority.py \
  src/tests/unit/test_stop_transactions.py \
  src/tests/unit/test_planning_stop.py \
  src/tests/unit/test_materialization_stop.py \
  src/tests/unit/test_builder_stop_boundary.py \
  src/tests/unit/test_stop_envelopes.py
git diff --check
```

The full repository run passed 1,042 tests and retained three pre-existing workspace failures:
the local `.cursor/rules` directory violates the single-instruction-authority fixture, and the
workspace fixture directories `shots/beacon_wake` and `shots/barrel_roll` are absent. Ruff passed
across `src`; the package-boundary suite passed all nine non-workspace checks.

## Release and rollback

Amendment target, selected-authority-amendment postcondition, plan workspace, and plan provenance
v1 shapes are obsolete and are strictly rejected; current producers publish v2. The
selected-authority assertion is a new v2
semantic contract layered over existing v1 plan and JIT pointers; it does not silently reinterpret
an old amendment action. Existing non-amendment transaction types retain their own current
state assertions until separately migrated.

Rollback would restore producer-private authority identity and the corrupt-pointer and
hard-constraint classification gaps. It is therefore not a safe compatibility mode. Disabling
future amendment dispatch while retaining strict read verification remains the safe fallback.

## Remaining limitations

Plan and JIT pointer schemas still have no monotone revision, shared selection lock, or
compare-and-swap token. Read/verify/reread is not ABA protection, durable multi-pointer commit,
or proof against concurrent publication. Pointer replacement also does not yet claim the full
fsync/commit protocol required by an amendment transaction. Cryptographic authenticity,
signatures, and hard-link substitution defense are separate concerns.

The current global planning stop does not yet authenticate the complete planning-input and
rejected-candidate authority surface needed to reconstruct or promote one exact successor.
No immutable amendment operation record binds an already-authored successor, candidate
aggregate, gate attestation, expected effects, base pointer observation, and finding
consumption before selection. There is no `publish_validated_amendment` adapter, commit,
receipt reconciliation, or independent evaluator. `apply_revision_checked_replan` remains a
later transaction that can run only after amended authority has actually been selected.

Consequently amendment, replan, retry, engineering handoff, checkpointed resume, and human
decision remain non-dispatchable proposals. HIR-0166's explicit `recover_environment` adapter
is still the only receipt-backed transaction. There is no controller, controller journal,
automatic polling, or `--until-accepted` loop.
