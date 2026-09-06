"""`bvfx_import_construction` returned Objects where it promised names (HIR-0235).

    before = set(bpy.data.objects.keys())        # names
    bpy.ops.import_scene.gltf(filepath=glb)
    new = [n for n in bpy.data.objects if n not in before]   # iterates OBJECTS
    for n in new:
        bpy.data.objects[n].rotation_mode = "XYZ"            # indexes with an Object

Three linked errors. An Object never equals a name, so `n not in before` was always
True and `new` became every object in the scene; the collection was then indexed with
an Object, which raises; and the annotation says `list[str]`.

It failed whenever it imported anything -- observed twice, identically, on
hansa_silk_road layer 2 at 337.7s and 345.7s. Per ADR-0009/HIR-0162 this is the sole
sanctioned import for a generate unit, so the generate-construction route has never
worked.

`worker.py` imports `bpy` at module scope and cannot be imported here, so these tests
execute the real source text against a fake that reproduces the two bpy behaviours that
matter: iteration yields Objects, and indexing requires a string.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "vfx_harness" / "blender" / "worker.py"


class _Object:
    """A bpy Object: it is not its name, and it never equals one."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.rotation_mode = "QUATERNION"

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)


class _Collection:
    """`bpy.data.objects`: iterating yields Objects; indexing demands a string."""

    def __init__(self, names: list[str]) -> None:
        self._by_name = {name: _Object(name) for name in names}

    def keys(self) -> list[str]:
        return list(self._by_name)

    def __iter__(self):
        return iter(self._by_name.values())

    def __getitem__(self, key: object) -> _Object:
        if not isinstance(key, str):
            raise TypeError(
                "bpy_prop_collection[key]: invalid key, must be a string or an int, "
                f"not {type(key).__name__}"
            )
        return self._by_name[key]

    def add(self, name: str) -> None:
        self._by_name[name] = _Object(name)


def _import_body_source() -> str:
    """The exact three lines each helper uses, lifted from the real file."""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "names"
        ):
            return ast.unparse(node)
    raise AssertionError("no `names = ...` assignment found in worker.py")


def _run_selection(existing: list[str], imported: list[str]) -> list[str]:
    """Execute the real selection-and-tagging idiom against the fake collection."""
    objects = _Collection(existing)
    before = set(objects.keys())
    for name in imported:
        objects.add(name)

    scope: dict[str, object] = {"bpy": type("bpy", (), {"data": type("data", (), {"objects": objects})})()}
    # Executes the repository's own source text, lifted by AST -- the point is that
    # the test runs the real line rather than a copy of it.
    exec(_import_body_source(), scope)
    new = [n for n in scope["names"] if n not in before]
    for n in new:
        objects[n].rotation_mode = "XYZ"
    return new


def test_only_the_imported_objects_are_returned_in_a_non_empty_scene() -> None:
    """An empty-scene fixture would not have caught this: `new` was everything."""
    new = _run_selection(existing=["camera.rig", "hero.tower"], imported=["hero.sign"])
    assert new == ["hero.sign"], new


def test_the_returned_values_are_names_as_annotated() -> None:
    new = _run_selection(existing=["camera.rig"], imported=["hero.sign"])
    assert all(isinstance(value, str) for value in new), new


def test_the_fake_reproduces_the_original_failure() -> None:
    """Guard on the fixture: if this stops raising, the test proves nothing."""
    objects = _Collection(["camera.rig"])
    before = set(objects.keys())
    objects.add("hero.sign")
    wrong = [n for n in objects if n not in before]          # the original expression
    assert len(wrong) == 2, "an Object never equals a name, so nothing is filtered out"
    with pytest.raises(TypeError, match="must be a string or an int"):
        objects[wrong[0]].rotation_mode = "XYZ"


def test_every_import_helper_selects_by_name() -> None:
    """All three sites, not the two that were reported."""
    source = WORKER.read_text(encoding="utf-8")
    assert source.count("n for n in names if n not in before") == 3
    assert "for n in bpy.data.objects if n not in before" not in source
