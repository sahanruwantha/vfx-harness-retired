---
id: HIR-0246
title: A name is not a type
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_bmesh_lookup_guard_keyed_on_an_attribute_name_and_blocked_correct_code
mechanism: the_guard_flags_only_subscripts_whose_owner_is_bound_to_a_bmesh_in_that_payload
adr: null
---

# A name is not a type

## Observed failure

`room_1046_opening`, four blocks across two units of one shot:

```
[ 927.5s] BLOCKED ... Before e.verts[...] at line 40, e.verts[...] at line 40,
                             e.verts[...] at line 24, e.verts[...] at line 24
[ 942.7s] BLOCKED ... at line 42 … 26
[ 959.9s] BLOCKED ... at line 44 … 27
[ 210.1s] BLOCKED ... at line 46
```

`e` is a loop variable over edges. **`BMEdge.verts` is a fixed-size sequence of two and has
no lookup table** -- `ensure_lookup_table()` exists on `BMesh.verts/edges/faces` and nowhere
else. The remedy the message prescribed did not exist on the object it fired for, so the
builder could not comply.

The line numbers drift upward by two on each attempt -- 40 → 42 → 44 → 46, 24 → 26 → 27 --
because it added lines above and re-submitted. `facade_grid` recorded
`bmesh_lookup_blocked=3` for that unit alone, and the reporting driver saw **zero true
positives on the whole shot.**

## Why it was allowed

`agents/guardrails.py`:

```python
owner = node.value.value
seq = node.value.attr
if isinstance(owner, _ast.Name) and seq in {"faces", "verts", "edges"}:
    indexed.append((owner.id, seq, node.lineno))
```

**It keyed on the attribute name and never on what the owner was.** Any `<name>.verts[...]`
was treated as BMesh sequence access, so `bm.verts[0]` and `e.verts[0]` were
indistinguishable to it although only the first has a lookup table.

That is the same error as inferring geometric containment from a dotted role, or camera
capability from a role called `camera.target` (HIR-0098): **a name is not a type**, and the
AST already carried what was needed to tell them apart.

## Mechanism

`_bmesh_handle_names(tree)` collects the names bound from `bmesh.new()`,
`bmesh.from_edit_mesh(...)` or `bmesh.from_object(...)` in the same payload, and the guard
flags a subscript only when its owner is one of them. A loop variable is not one; nor is
anything whose attribute merely happens to be called `verts`.

## The trade, stated

This introduces a false **negative**: a BMesh obtained some other way -- returned by a
helper, unpacked from a tuple -- is not tracked, so genuinely missing
`ensure_lookup_table()` on it will not be blocked.

That is the right direction. A missed block surfaces as Blender's own error inside a
`run_bpy` call, which HIR-0068 already treats as a transaction: the snapshot is restored and
nothing partial survives. A false block costs a round trip **and cannot be satisfied**,
which is strictly worse -- the builder cannot learn its way out of a message prescribing an
API that does not exist. Four instances, one shot, and the only escape was to restructure
correct code.

## Validation

`src/tests/unit/test_bmesh_lookup_guard_knows_its_owner.py`. The first test is the exact
room shape -- `for e in bm.edges: e.verts[0]` -- and asserts only `bm` is a handle. Others
pin all three constructors, that an attribute named `verts` on a non-handle registers
nothing, that two handles in one payload are both tracked, and that a `helper.new()` or
`mathutils.new()` call does not register as a BMesh.

Reported by the room_1046_opening driver, who read the source rather than inferring from the
message -- "having been wrong twice today doing the reverse".
