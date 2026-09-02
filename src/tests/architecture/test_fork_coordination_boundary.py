"""One process-wide fork coordinator owns every authority-registry lock order."""

from __future__ import annotations

import ast
from pathlib import Path

from vfx_harness.observability import fork_coordination

PACKAGE = Path(fork_coordination.__file__).resolve().parents[1]
COORDINATOR = Path(fork_coordination.__file__).resolve()


def _sources() -> tuple[Path, ...]:
    return tuple(sorted(PACKAGE.rglob("*.py")))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def test_process_at_fork_registration_has_one_owner() -> None:
    violations: list[str] = []
    for path in _sources():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "register_at_fork"
                and path.resolve() != COORDINATOR
            ):
                violations.append(
                    f"{path.relative_to(PACKAGE).as_posix()}:{node.lineno}"
                )
    assert not violations, (
        "authority registries must join the one fork coordinator: "
        + ", ".join(violations)
    )


def test_participant_names_are_static_and_process_unique() -> None:
    observed: dict[str, str] = {}
    violations: list[str] = []
    for path in _sources():
        relative = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(_tree(path)):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node) == "register_fork_participant"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                violations.append(f"{relative}:{node.lineno}:dynamic-name")
                continue
            name = node.args[0].value
            if not isinstance(name, str) or not name:
                violations.append(f"{relative}:{node.lineno}:invalid-name")
                continue
            prior = observed.setdefault(name, f"{relative}:{node.lineno}")
            if prior != f"{relative}:{node.lineno}":
                violations.append(f"{relative}:{node.lineno}:duplicates:{prior}")
    assert not violations, (
        "fork participant names must be static and unique: " + ", ".join(violations)
    )


def test_participant_locks_are_used_only_through_coordinator() -> None:
    violations: list[str] = []
    for path in _sources():
        tree = _tree(path)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        participant_locks: set[str] = set()
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if _call_name(call) != "register_fork_participant":
                continue
            factory = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "lock_factory"
                ),
                None,
            )
            if (
                isinstance(factory, ast.Lambda)
                and isinstance(factory.body, ast.Name)
            ):
                participant_locks.add(factory.body.id)
        if not participant_locks:
            continue
        relative = path.relative_to(PACKAGE).as_posix()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in participant_locks
            ):
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Lambda) and parent.body is node:
                continue
            if (
                isinstance(parent, ast.Call)
                and _call_name(parent) == "fork_coordinated_lock"
                and node in parent.args
            ):
                continue
            violations.append(f"{relative}:{node.lineno}:{node.id}")
    assert not violations, (
        "fork participant locks bypassed the global-to-local coordinator order: "
        + ", ".join(violations)
    )


def test_consumer_directory_boundary_has_no_destructive_name_operation() -> None:
    forbidden = {"replace", "rmdir", "rmtree", "unlink"}
    violations: list[str] = []
    for path in sorted(
        (PACKAGE / "orchestration").glob("plan_consumer_view_*.py")
    ):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            destructive = (
                isinstance(node.func, ast.Attribute) and name in forbidden
            ) or (
                isinstance(node.func, ast.Name)
                and name in forbidden - {"replace"}
            )
            if destructive:
                violations.append(f"{path.name}:{node.lineno}:{name}")
    assert not violations, (
        "consumer directories must retire by exact no-replace tombstone, never "
        "destructive lexical path operations: " + ", ".join(violations)
    )
