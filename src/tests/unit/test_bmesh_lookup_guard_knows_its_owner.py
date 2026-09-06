"""`ensure_lookup_table` exists on a BMesh sequence and nowhere else.

room_1046_opening, four blocks across two units:

    BLOCKED ... Before e.verts[...] at line 40, e.verts[...] at line 40,
                       e.verts[...] at line 24, e.verts[...] at line 24
    BLOCKED ... at line 42 … 26        BLOCKED ... at line 44 … 27
    BLOCKED ... at line 46

`e` is a loop variable over edges. `BMEdge.verts` is a fixed-size sequence of two and has
no lookup table -- the method the message prescribes does not exist on it. So the builder
could not comply: the line numbers drift upward by two each attempt because it added lines
above and re-submitted, and `facade_grid` recorded `bmesh_lookup_blocked=3` for one unit.
Zero true positives on that shot.

The guard keyed on the attribute name and never on what the owner was. A name is not a
type -- the same error as inferring geometric containment from a dotted role.
"""

from __future__ import annotations

import ast

import anyio

# The module, not the new name: the behavioural test below must collect on the pre-fix
# tree and fail on the guard firing, not on an import (see AGENTS.md on discriminators).
from vfx_harness.agents import guardrails


def _handles(source: str) -> frozenset[str]:
    return guardrails._bmesh_handle_names(ast.parse(source))


def _blocked(code: str) -> str:
    """Run the real PreToolUse hook over a run_bpy payload; return its deny reason."""
    hook = guardrails.script_sanity().hooks[0]
    result = anyio.run(
        hook, {"tool_name": "mcp__blender__run_bpy", "tool_input": {"code": code}}, None, None
    )
    return str(((result or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason") or "")


LOOP_OVER_EDGES = """
import bmesh, bpy
bm = bmesh.new()
bm.from_mesh(bpy.context.object.data)
bm.edges.ensure_lookup_table()
for e in bm.edges:
    mid = (e.verts[0].co + e.verts[1].co) / 2.0
    bpy.context.object.data.update()
"""


def test_the_hook_does_not_block_a_loop_over_edges() -> None:
    """The behavioural discriminator: on the pre-fix tree this denies on `e.verts[...]`."""
    reason = _blocked(LOOP_OVER_EDGES)

    assert "e.verts" not in reason, reason
    assert "ensure_lookup_table" not in reason, reason


def test_the_hook_still_blocks_indexing_a_bmesh_without_the_table() -> None:
    """The true positive the guard exists for must survive the narrowing."""
    reason = _blocked(
        "import bmesh, bpy\n"
        "bm = bmesh.new()\n"
        "bm.from_mesh(bpy.context.object.data)\n"
        "v = bm.verts[0]\n"
        "bpy.context.object.data.update()\n"
    )

    assert "bm.verts[...]" in reason
    assert "ensure_lookup_table" in reason


def test_a_loop_variable_over_edges_is_not_a_bmesh_handle() -> None:
    """The exact room_1046_opening shape."""
    source = """
import bmesh
bm = bmesh.new()
for e in bm.edges:
    a = e.verts[0].co
    b = e.verts[1].co
"""
    assert _handles(source) == {"bm"}


def test_every_bmesh_constructor_binds_a_handle() -> None:
    for call in ("bmesh.new()", "bmesh.from_edit_mesh(me)", "bmesh.from_object(ob, dg)"):
        assert _handles(f"import bmesh\nbm = {call}\n") == {"bm"}


def test_a_name_that_merely_owns_a_verts_attribute_is_not_a_handle() -> None:
    """`f.verts`, `mesh.verts`, `e.verts` -- attribute name, not type."""
    source = """
for f in faces:
    x = f.verts[0]
mesh = load()
y = mesh.verts[0]
"""
    assert _handles(source) == frozenset()


def test_two_handles_in_one_payload_are_both_tracked() -> None:
    source = "import bmesh\nbm = bmesh.new()\nother = bmesh.from_edit_mesh(me)\n"

    assert _handles(source) == {"bm", "other"}


def test_a_call_on_something_that_is_not_bmesh_binds_nothing() -> None:
    """`mathutils.new()` or a local `helper.new()` must not register as a BMesh."""
    source = "helper = get()\nbm = helper.new()\nother = mathutils.new()\n"

    assert _handles(source) == frozenset()
