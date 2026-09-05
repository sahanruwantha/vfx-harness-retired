"""Closed Python policy for deterministic Blender artifact replay.

Artifact scripts are scene deltas, not general automation programs.  They may use the
Blender construction modules needed to rebuild a scene, but they must not acquire a
filesystem, process, network, or dynamic-code capability.  Validation happens before
the worker evaluates any source, and the worker also supplies a reduced builtins table.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Mapping
from types import MappingProxyType


class ArtifactExecutionPolicyError(ValueError):
    """Artifact source asks for a capability outside deterministic scene replay."""


_ALLOWED_IMPORT_ROOTS = frozenset({"bmesh", "bpy", "json", "math", "mathutils"})
_BANNED_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "globals",
        "input",
        "locals",
        "open",
        "vars",
    }
)
_BANNED_ATTRIBUTES = frozenset(
    {
        "as_module",
        "execfile",
        "open_mainfile",
        "python_file_run",
        "read_homefile",
        "save",
        "save_as_mainfile",
        "save_mainfile",
        "save_render",
        "save_userpref",
        "write",
        "write_homefile",
    }
)
_BANNED_OUTPUT_TARGETS = frozenset({"base_path", "directory", "filepath", "filepath_raw"})
_BANNED_DYNAMIC_CODE_TARGETS = frozenset({"expression"})
_BANNED_NODE_TYPES = frozenset({"CompositorNodeOutputFile", "OUTPUT_FILE"})
# Artifact replay uses Blender operators only for bounded in-memory construction.
# Blender's operator registry is open-ended (installed add-ons can register more), so
# a denylist of known import/export names is not a security boundary.  Keep the small
# deterministic vocabulary explicit; direct bpy.data construction remains available.
_ALLOWED_BPY_OPERATORS = frozenset(
    {
        ("curve", "primitive_bezier_circle_add"),
        ("curve", "primitive_bezier_curve_add"),
        ("curve", "primitive_nurbs_circle_add"),
        ("curve", "primitive_nurbs_curve_add"),
        ("curve", "primitive_nurbs_path_add"),
        ("mesh", "delete"),
        ("mesh", "extrude_edges_move"),
        ("mesh", "extrude_faces_move"),
        ("mesh", "extrude_region_move"),
        ("mesh", "fill"),
        ("mesh", "inset"),
        ("mesh", "merge"),
        ("mesh", "primitive_circle_add"),
        ("mesh", "primitive_cone_add"),
        ("mesh", "primitive_cube_add"),
        ("mesh", "primitive_cylinder_add"),
        ("mesh", "primitive_grid_add"),
        ("mesh", "primitive_ico_sphere_add"),
        ("mesh", "primitive_monkey_add"),
        ("mesh", "primitive_plane_add"),
        ("mesh", "primitive_torus_add"),
        ("mesh", "primitive_uv_sphere_add"),
        ("mesh", "select_all"),
        ("mesh", "subdivide"),
        ("object", "armature_add"),
        ("object", "camera_add"),
        ("object", "convert"),
        ("object", "delete"),
        ("object", "duplicate"),
        ("object", "editmode_toggle"),
        ("object", "effector_add"),
        ("object", "empty_add"),
        ("object", "join"),
        ("object", "light_add"),
        ("object", "metaball_add"),
        ("object", "mode_set"),
        ("object", "modifier_apply"),
        ("object", "parent_set"),
        ("object", "select_all"),
        ("object", "shade_flat"),
        ("object", "shade_smooth"),
        ("object", "shade_smooth_by_angle"),
        ("object", "text_add"),
        ("object", "transform_apply"),
        ("object", "visual_transform_apply"),
        ("pose", "select_all"),
        ("pose", "transforms_clear"),
        ("rigidbody", "constraint_add"),
        ("rigidbody", "object_add"),
        ("rigidbody", "object_remove"),
        ("surface", "primitive_nurbs_circle_add"),
        ("surface", "primitive_nurbs_cylinder_add"),
        ("surface", "primitive_nurbs_sphere_add"),
        ("surface", "primitive_nurbs_surface_curve_add"),
        ("surface", "primitive_nurbs_surface_cylinder_add"),
        ("surface", "primitive_nurbs_surface_sphere_add"),
        ("surface", "primitive_nurbs_surface_torus_add"),
        ("surface", "primitive_nurbs_torus_add"),
        ("transform", "resize"),
        ("transform", "rotate"),
        ("transform", "translate"),
    }
)
_SAFE_BUILTINS = (
    "Exception",
    "RuntimeError",
    "TypeError",
    "ValueError",
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "filter",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "print",
    "range",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
)


_BindingMap = dict[str, set[tuple[str, ...]]]


def _resolved_chains(node: ast.AST, bindings: _BindingMap) -> set[tuple[str, ...]]:
    """Resolve conservative import/simple-name aliases for one attribute expression."""

    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, {(node.id,)}))
    if isinstance(node, ast.Attribute):
        return {
            (*prefix, node.attr)
            for prefix in _resolved_chains(node.value, bindings)
        }
    return set()


def _artifact_bindings(tree: ast.Module) -> _BindingMap:
    """Collect every possible module/namespace name without trusting assignment order."""

    bindings: _BindingMap = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                if root not in _ALLOWED_IMPORT_ROOTS:
                    raise ArtifactExecutionPolicyError(f"artifact import denied: {root}")
                if root == "bpy" and alias.name != "bpy":
                    raise ArtifactExecutionPolicyError(
                        "artifact bpy submodule imports are denied; import bpy directly"
                    )
                local = alias.asname or root
                resolved = tuple(alias.name.split(".")) if alias.asname else (root,)
                bindings.setdefault(local, set()).add(resolved)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                raise ArtifactExecutionPolicyError("relative artifact imports are denied")
            root = node.module.partition(".")[0]
            if root not in _ALLOWED_IMPORT_ROOTS:
                raise ArtifactExecutionPolicyError(f"artifact import denied: {root}")
            if root == "bpy":
                raise ArtifactExecutionPolicyError(
                    "artifact from-bpy imports are denied; import bpy directly"
                )
            for alias in node.names:
                if alias.name == "*":
                    raise ArtifactExecutionPolicyError(
                        "artifact wildcard imports are denied"
                    )
                local = alias.asname or alias.name
                bindings.setdefault(local, set()).add(
                    (*node.module.split("."), alias.name)
                )

    # A module or Blender namespace may be rebound through several simple names and
    # branches.  Keep the union forever: later assignment cannot prove that an earlier
    # capability is unreachable, and accepting that guess would reopen an alias bypass.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value: ast.AST | None = None
            targets: tuple[ast.AST, ...] = ()
            if isinstance(node, ast.Assign):
                value = node.value
                targets = tuple(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = (node.target,)
            if value is None or not targets:
                continue
            chains = {
                chain
                for chain in _resolved_chains(value, bindings)
                if chain and chain[0] in {"bpy", "json"}
            }
            if not chains:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                known = bindings.setdefault(target.id, set())
                before = len(known)
                known.update(chains)
                changed = changed or len(known) != before
    return bindings


def _reject_capability_chain(chain: tuple[str, ...]) -> None:
    """Reject acquisition as well as invocation of an external replay capability."""

    if chain[:3] == ("bpy", "ops", "render"):
        raise ArtifactExecutionPolicyError(
            "artifact rendering is denied; the harness owns render publication"
        )
    if len(chain) >= 2 and chain[0] == "bpy" and chain[-1] == "load":
        raise ArtifactExecutionPolicyError(
            "artifact external data load is denied; use a pinned harness import helper"
        )
    if len(chain) >= 2 and chain[:2] == ("bpy", "utils"):
        raise ArtifactExecutionPolicyError(
            "artifact Blender utility capability is denied"
        )
    if len(chain) >= 4 and chain[:2] == ("bpy", "ops") and (
        chain[2], chain[3]
    ) not in _ALLOWED_BPY_OPERATORS:
        raise ArtifactExecutionPolicyError(
            "artifact Blender operator is outside the closed replay vocabulary; "
            "use direct in-memory bpy.data construction or a pinned harness import helper"
        )
    if chain and chain[0] == "json" and chain[-1] != "dumps":
        raise ArtifactExecutionPolicyError(
            f"artifact json capability denied: {chain[-1]}"
        )


def _is_bpy_namespace(chain: tuple[str, ...]) -> bool:
    return bool(chain and chain[0] == "bpy")


def _is_direct_invocation(node: ast.Attribute, parent: ast.AST | None) -> bool:
    return isinstance(parent, ast.Call) and parent.func is node


def _is_simple_name_alias_value(node: ast.Name, parent: ast.AST | None) -> bool:
    if isinstance(parent, ast.Assign) and parent.value is node:
        return all(isinstance(target, ast.Name) for target in parent.targets)
    if isinstance(parent, ast.AnnAssign) and parent.value is node:
        return isinstance(parent.target, ast.Name)
    return (
        isinstance(parent, ast.NamedExpr)
        and parent.value is node
        and isinstance(parent.target, ast.Name)
    )


def _is_simple_alias_value(node: ast.Attribute, parent: ast.AST | None) -> bool:
    if isinstance(parent, ast.Assign) and parent.value is node:
        return all(isinstance(target, ast.Name) for target in parent.targets)
    if isinstance(parent, ast.AnnAssign) and parent.value is node:
        return isinstance(parent.target, ast.Name)
    return (
        isinstance(parent, ast.NamedExpr)
        and parent.value is node
        and isinstance(parent.target, ast.Name)
    )


def _is_exact_current_frame_reevaluation_read(
    node: ast.Attribute,
    parent: ast.AST | None,
    *,
    parents: Mapping[ast.AST, ast.AST],
    bindings: _BindingMap,
) -> bool:
    """Allow only ``scene.frame_set(int(scene.frame_current))`` scalar use.

    Artifact publication must force Blender to evaluate the selected current frame
    after every constituent replay.  The closed capability policy permits that one
    read only when ``int`` feeds the exact ``bpy.context.scene.frame_set`` call.  The
    scalar therefore cannot be assigned, passed elsewhere, chained, or used to reach
    another Blender capability.  Simple aliases remain supported through the same
    conservative binding resolver used by the rest of this module.
    """

    if _resolved_chains(node, bindings) != {
        ("bpy", "context", "scene", "frame_current")
    }:
        return False
    if not (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Name)
        and parent.func.id == "int"
        and parent.args == [node]
        and not parent.keywords
    ):
        return False
    frame_set_call = parents.get(parent)
    return bool(
        isinstance(frame_set_call, ast.Call)
        and frame_set_call.args == [parent]
        and not frame_set_call.keywords
        and _resolved_chains(frame_set_call.func, bindings)
        == {("bpy", "context", "scene", "frame_set")}
    )


def _alias_escape_message(
    source: str,
    node: ast.expr,
    chains: set[tuple[str, ...]],
) -> str:
    """Name the escaping expression, its capability, and the legal replay forms.

    The bare "cannot escape a tracked attribute or simple alias" sent a finalizer to read
    harness source for the rule (run 20260902T165518Z-004470); a rejection must teach the
    contract, the observed value, and the next legal action.
    """

    segment = ast.get_source_segment(source, node) or ast.dump(node)
    capability = " / ".join(".".join(chain) for chain in sorted(chains)) or "bpy"
    return (
        "artifact bpy capability cannot escape a tracked attribute or simple alias: "
        f"line {node.lineno} uses `{segment}` (a handle on {capability}) as a value. A "
        "bpy attribute chain, or a name bound to one, may only be read through further "
        "attributes, invoked directly, or rebound to a simple name — never passed as an "
        "argument, stored, returned, or compared. Take scene data from a call result "
        "instead (`bpy.data.objects.new(...)`, `bpy.data.objects.get('name')`, "
        "`bpy.data.cameras.new(...)`, or a bvfx_* helper return value): call results are "
        "plain values the policy does not track."
    )


def artifact_violations(source: str, tree: ast.Module) -> list[tuple[int, str]]:
    """Every policy violation in one parsed artifact, as (line, message) pairs.

    The walk finds them all and used to report the first. A finalizer fixing line 24
    then paid another write-then-probe round trip to be told about line 25, which was
    already there and already illegal when the walk ran: three cycles on one candidate,
    on the most expensive phase in a shot (HIR-0216). Recording never admits anything —
    any violation still refuses the artifact.
    """

    bindings = _artifact_bindings(tree)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    found: list[tuple[int, str]] = []

    def record(node: ast.AST, message: str) -> None:
        found.append((int(getattr(node, "lineno", 0)), message))

    def capability_violation(chain) -> str | None:
        try:
            _reject_capability_chain(chain)
        except ArtifactExecutionPolicyError as exc:
            return str(exc)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            record(node, f"artifact capability denied: {node.id}")
            continue
        elif isinstance(node, ast.Name):
            chains = _resolved_chains(node, bindings)
            parent = parents.get(node)
            if (
                not isinstance(node.ctx, ast.Store)
                and any(_is_bpy_namespace(chain) for chain in chains)
                and not (isinstance(parent, ast.Attribute) and parent.value is node)
                and not (isinstance(parent, ast.Call) and parent.func is node)
                and not _is_simple_name_alias_value(node, parent)
            ):
                record(node, _alias_escape_message(source, node, chains))
                continue
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr.endswith("__"):
                record(node, f"artifact dunder access denied: {node.attr}")
                continue
            if node.attr in _BANNED_ATTRIBUTES or node.attr.startswith(("export", "save", "write")):
                record(node, f"artifact file-writing attribute denied: {node.attr}")
                continue
            chains = _resolved_chains(node, bindings)
            rejected = [message for chain in chains if (message := capability_violation(chain))]
            if rejected:
                for message in rejected:
                    record(node, message)
                continue
            parent = parents.get(node)
            if (
                not isinstance(node.ctx, ast.Store)
                and not isinstance(parent, ast.Attribute)
                and not _is_direct_invocation(node, parent)
                and any(_is_bpy_namespace(chain) for chain in chains)
                and not _is_simple_alias_value(node, parent)
                and not _is_exact_current_frame_reevaluation_read(
                    node,
                    parent,
                    parents=parents,
                    bindings=bindings,
                )
            ):
                record(node, _alias_escape_message(source, node, chains))
                continue
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for nested in ast.walk(target):
                    if (
                        isinstance(nested, ast.Attribute)
                        and nested.attr in _BANNED_OUTPUT_TARGETS
                    ):
                        record(nested, "artifact external-output target denied: " + nested.attr)
                    if (
                        isinstance(nested, ast.Attribute)
                        and nested.attr in _BANNED_DYNAMIC_CODE_TARGETS
                    ):
                        record(
                            nested,
                            "artifact deferred dynamic-code target denied: " + nested.attr,
                        )
        elif isinstance(node, ast.Call):
            for chain in _resolved_chains(node.func, bindings):
                message = capability_violation(chain)
                if message is not None:
                    record(node, message)
        elif isinstance(node, ast.Constant) and node.value in _BANNED_NODE_TYPES:
            record(node, f"artifact external-output node denied: {node.value}")
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            record(node, f"artifact statement denied: {type(node).__name__}")

    # Source order, not ast.walk's breadth-first order: a list that reads 72, 24, 25 is
    # worse than three ordered refusals.
    seen: set[tuple[int, str]] = set()
    ordered: list[tuple[int, str]] = []
    for row in sorted(found, key=lambda item: item[0]):
        if row not in seen:
            seen.add(row)
            ordered.append(row)
    return ordered


def validate_artifact_source(source: str) -> ast.Module:
    """Parse and reject any capability outside the artifact replay vocabulary."""

    try:
        tree = ast.parse(source, filename="<vfx-artifact>", mode="exec")
    except SyntaxError as exc:
        raise ArtifactExecutionPolicyError(f"artifact source is invalid Python: {exc}") from exc

    violations = artifact_violations(source, tree)
    if violations:
        if len(violations) == 1:
            raise ArtifactExecutionPolicyError(violations[0][1])
        raise ArtifactExecutionPolicyError(
            f"artifact source has {len(violations)} policy violations; every one refuses "
            "this artifact, so fix all of them in one edit:\n"
            + "\n".join(f"  {index}. {message}" for index, (_line, message) in enumerate(violations, 1))
        )
    return tree


def _artifact_import(
    name: str,
    globals_: Mapping | None = None,
    locals_: Mapping | None = None,
    fromlist=(),
    level: int = 0,
):
    root = str(name).partition(".")[0]
    if level or root not in _ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"artifact import denied: {name}")
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def artifact_builtins() -> Mapping[str, object]:
    """Return the immutable builtins table used by worker artifact execution."""

    values = {name: getattr(builtins, name) for name in _SAFE_BUILTINS}
    values["__import__"] = _artifact_import
    return MappingProxyType(values)
