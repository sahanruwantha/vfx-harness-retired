---
id: HIR-0235
title: The construction import helper returned Objects where it promised names
status: accepted
introduced_in: unreleased
date: 2026-09-06
failure_class: the_sole_sanctioned_generate_import_raised_on_every_import_it_performed
mechanism: the_new_object_selection_iterates_names_and_a_fake_bpy_collection_pins_it
adr: ADR-0009
---

# The construction import helper returned Objects where it promised names

## Observed failure

`hansa_silk_road` layer 2 chose `construction: generate` and called
`bvfx_import_construction()`. It raised, twice, identically, at 337.7s and 345.7s.

```python
before = set(bpy.data.objects.keys())                     # NAMES
bpy.ops.import_scene.gltf(filepath=glb)
new = [n for n in bpy.data.objects if n not in before]    # iterates OBJECTS
for n in new:
    bpy.data.objects[n].rotation_mode = "XYZ"             # indexes with an Object
return new                                                 # annotated list[str]
```

Three linked errors in five lines:

1. `before` holds names; iterating a bpy collection yields Objects. An Object never
   equals a string, so **`n not in before` is always True** and `new` is every object in
   the scene rather than the imported ones.
2. `bpy.data.objects[n]` is then indexed with an Object:
   `TypeError: bpy_prop_collection[key]: invalid key, must be a string or an int, not
   Object`.
3. The annotation says `list[str]` and the value is a list of Objects.

**It raises whenever it imports anything.** The only non-raising path is a GLB that
yields no objects, where the loop never runs.

Per ADR-0009/HIR-0162 this is the **sole** sanctioned import for a generate unit --
`vfx asset` is retired and `import_asset` fails closed. So the generate-construction
route terminates here, and has since it landed.

## Root cause

`set(bpy.data.objects.keys())` and `for n in bpy.data.objects` look like the same
traversal of the same collection, and in a `dict` they would be. `bpy.data.objects` is
not a dict: `.keys()` yields names and iteration yields Objects. The two lines sit
adjacent and disagree.

Nothing caught it because nothing ran it. Both earlier `hansa` layer-2 designs were
procedural; the third was the first time any shot took the generate route. **Every review
of this code was a review of code that had never executed.**

The `_bvfx_import_asset` sibling carries a comment explaining exactly why the
`rotation_mode` normalisation matters -- glTF leaves objects in QUATERNION mode, where
assigning `rotation_euler` is a silent no-op that produced four identical "different"
turntable angles. That comment documents a real trap and a real fix, in a loop that has
never successfully executed.

## Decision

Select by name at all **three** sites -- the two code paths inside
`_bvfx_import_construction` (prepared replay bytes and promoted file) and
`_bvfx_import_asset`:

```python
names = bpy.data.objects.keys()
new = [n for n in names if n not in before]
```

The reporting driver found two; the third is the prepared-bytes path in the same
function.

**Ruff wanted to autofix this back into the defect.** `SIM118` reads
`n for n in bpy.data.objects.keys()` as the dict idiom `key in dict.keys()` and offers to
remove `.keys()` -- which is precisely the bug. Binding the call to a local makes the rule
inapplicable rather than suppressed, and each site carries the reason so the next reader
does not re-simplify it.

## Validation

`src/tests/unit/test_construction_import_returns_new_names.py`. `worker.py` imports `bpy`
at module scope and cannot be imported by the suite, so the tests **execute the real
source text** -- the `names = ...` assignment, lifted from the file by AST -- against a
fake collection reproducing the two bpy behaviours that matter: iteration yields Objects,
and indexing demands a string.

- only the imported objects are returned, **in a non-empty scene**. An empty-scene fixture
  would not have caught error 1, because `new` being "everything" and "the new ones" are
  the same list when the scene starts empty;
- the returned values are names, as annotated;
- **a fixture guard**: the original expression is run against the same fake and asserted
  to select 2 objects instead of 1 and then raise `TypeError`. If that stops failing, the
  fake has drifted and the other tests prove nothing;
- all three sites select by name, and the old expression appears nowhere.

Reverting `src/vfx_harness` fails three of four; the fixture guard passes both ways, by
design.

## What this does not fix

The builder **routed around** the broken helper: it called `bpy.ops.import_scene.gltf`
directly, the import succeeded, and the mesh landed **untagged** -- `object roles present:
camera.rig`, and `hero-center-x-f1` read `bbox_center_x=None`. Two consequences, neither
addressed here:

- **A failed call left geometry in the live scene.** HIR-0068 requires an authored
  `run_bpy`/import call to snapshot before mutation and restore before surfacing an
  exception. Either that transaction does not wrap this path or the direct `bpy.ops` call
  sits outside it. That is a separate defect and plausibly the more serious one.
- **A hand-rolled import is not the sanctioned route** and skips construction promotion
  and helper role-tagging, so whatever it seals is a build produced by working around a
  broken instrument.
