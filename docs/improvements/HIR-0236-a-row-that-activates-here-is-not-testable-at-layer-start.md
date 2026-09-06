---
id: HIR-0236
title: A row that activates here is not testable at layer start
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: a_deferred_row_was_evaluated_before_its_own_activation_layer_built_its_subject
mechanism: layer_start_revalidation_skips_any_row_whose_active_window_begins_at_that_layer
adr: null
---

# A row that activates here is not testable at layer start

## Observed failure

`room_1046_opening`, run `20260905T233904Z-f95efe`, exit 1:

```
layer 2 cannot start: prior interface revalidation failed:
  cam-clearance-building=None target >= 0.5 (repair layer 1).
  Rebuild the fault-owning layer; downstream compensation is forbidden
```

The row, from the selected view:

```json
{"id": "cam-clearance-building", "kind": "path_clearance_min", "op": "min", "lo": 0.5,
 "frames": [151, 176], "activates_at": "2", "owner_layer": "1", "fault_owner": "1",
 "roles": ["camera.rig"], "compare_roles": ["building.*"], "lifecycle": "persistent"}
```

`activates_at: "2"`, and it was evaluated **at layer 2's start**, before any layer-2 unit
had built anything. `compare_roles: ["building.*"]` resolved to nothing, the metric read
`None`, and `None >= 0.5` scored as a failure.

**This is a deadlock, not a bad build.** Layer 1 cannot satisfy the row -- that is why the
controller deferred it, correctly. Layer 2 cannot start, because the check runs before
layer 2 builds the subject that would satisfy it. The advice `(repair layer 1)` names a
layer that provably cannot repair it, and no `vfx units retry` or `finalizations release`
path leads out.

## Root cause

`prior_interface_rows` already excluded this case -- for `bbox_*` rows only. It called
`deferred_subject_composition_activation_ids`, whose second statement is:

```python
if str(row.get("kind") or "") not in BBOX_KINDS:
    continue
```

HIR-0134/0151 introduced deferred activation for camera-owned subject-composition rows and
the exclusion was written in those terms. But `activates_at` is a **general lifecycle
field** -- `bounds()` reads it for every row -- so a `path_clearance_min` row carrying the
same deferral fell straight through a guard that existed for exactly its situation.

AGENTS.md already states the rule generally: *"At the exact activation layer, layer-start
prior-interface replay excludes the not-yet-instantiated subject."* The implementation was
narrower than the rule.

## Decision

`domain/contracts.activation_begins_at(row, layer_id)` answers whether a row's active
window *begins* at a layer, from `bounds()` -- the same function `active_for` uses, so
there is no second reading of `activates_at`. `prior_interface_rows` excludes those rows,
whatever their kind.

**Active at a layer and testable at that layer's start are different properties**, and
that distinction is the whole fix. The row stays fully active: it is enforced at layer 2's
*end* like any other, and at every later layer's start.

The alternative -- treating a `None` metric as not-due -- was rejected. It would silently
pass a row that is unevaluable for some other reason, which is the vacuous-pass failure
HIR-0024 exists to prevent. A `None` reading remains a failure; the row is simply not
selected before its subject can exist.

## Validation

`src/tests/unit/test_activation_layer_start_exclusion.py` imports **only**
`prior_interface_rows`, which already existed, so on the pre-fix tree it fails on the
behaviour rather than a missing symbol:

```
E   AssertionError: assert 'cam-clearance-building' not in ['cam-clearance-building', 'cam-lens']
```

- the deferred row is not evaluated at the layer it activates on;
- it **is** evaluated at the next layer -- skipping must not retire it;
- an ordinary earlier-layer row is still revalidated at both layers, so the exclusion did
  not widen into "skip prior interfaces";

`test_activation_window_helper.py` covers the helper itself, kept in a separate file so
the discriminator above does not import a new name. It pins that the exclusion is not
limited to `BBOX_KINDS`, and that a row failing lifecycle validation is **not** treated as
deferred -- a malformed row must not find a free pass out of revalidation.

## What this does not fix

The `(repair layer 1)` advice is inherited from the revalidation failure and is wrong for
any not-yet-active row: it names the `fault_owner`, which for a deferred row is the layer
that authored it, not one that can satisfy it. With this change such rows are not
evaluated at layer start, so the misleading string no longer appears for *this* case --
but the message would still be wrong if a genuinely-active row of the same shape failed.
Reported by the `room_1046_opening` driver and recorded, not addressed.
