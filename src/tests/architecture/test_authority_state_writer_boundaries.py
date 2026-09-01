"""HIR-0171 authority heads and bound state have one mutation boundary."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "vfx_harness"

_POINTER_MUTATORS = frozenset(
    {
        "durable_remove_pointer",
        "durable_replace_pointer_bytes",
        "durable_replace_pointer_json",
    }
)
_POINTER_MUTATOR_OWNERS = frozenset(
    {
        "orchestration/authority_selection_transaction.py",
        "orchestration/authority_state_recovery.py",
        "orchestration/authority_state_store.py",
        "orchestration/authority_state_transaction.py",
    }
)
_COORDINATOR_POINTER_MUTATORS = frozenset(
    {"remove_pending", "replace_current_bytes", "replace_pending_bytes"}
)
_COORDINATOR_POINTER_OWNERS = frozenset(
    {
        "orchestration/authority_state_recovery.py",
        "orchestration/authority_state_store.py",
        "orchestration/authority_state_transaction.py",
    }
)
_RAW_STATE_MUTATORS = frozenset({"remove_state_file", "write_state_file_bytes"})
_RAW_STATE_MUTATOR_OWNERS = frozenset(
    {
        "orchestration/authority_state_recovery.py",
        "orchestration/authority_state_transaction.py",
        "orchestration/unit_state_lock.py",
        "orchestration/unit_state_storage.py",
    }
)


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _function(path: Path, name: str) -> ast.FunctionDef:
    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name} in {_relative(path)}")


def _decorator_names(function: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _function_calls(function: ast.FunctionDef) -> set[str]:
    return {
        name
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and (name := _called_name(node)) is not None
    }


def test_selected_pointer_mutation_primitives_have_closed_owners() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                == "vfx_harness.orchestration.authority_selection_transaction"
                and any(alias.name in _POINTER_MUTATORS for alias in node.names)
                and relative not in _POINTER_MUTATOR_OWNERS
            ):
                imported = sorted(
                    alias.name for alias in node.names if alias.name in _POINTER_MUTATORS
                )
                violations.append(
                    f"{relative}:{node.lineno}:import:{','.join(imported)}"
                )
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _POINTER_MUTATORS and relative not in _POINTER_MUTATOR_OWNERS:
                violations.append(f"{relative}:{node.lineno}:{name}")
    assert not violations, (
        "selected pointer mutation must stay inside the coordinator/store/recovery "
        "boundary: " + ", ".join(violations)
    )


def test_coordinator_head_and_wal_writers_have_closed_callers() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "vfx_harness.orchestration.authority_state_store"
                and any(
                    alias.name in _COORDINATOR_POINTER_MUTATORS
                    for alias in node.names
                )
                and relative not in _COORDINATOR_POINTER_OWNERS
            ):
                imported = sorted(
                    alias.name
                    for alias in node.names
                    if alias.name in _COORDINATOR_POINTER_MUTATORS
                )
                violations.append(
                    f"{relative}:{node.lineno}:import:{','.join(imported)}"
                )
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if (
                name in _COORDINATOR_POINTER_MUTATORS
                and relative not in _COORDINATOR_POINTER_OWNERS
            ):
                violations.append(f"{relative}:{node.lineno}:{name}")
    assert not violations, (
        "authority-state current/pending may move only through transaction, recovery, "
        "or their storage owner: " + ", ".join(violations)
    )


def test_plan_and_jit_publishers_delegate_selection_to_atomic_coordinator() -> None:
    publishers = (
        (PACKAGE / "orchestration/plan_authority.py", "publish_current"),
        (
            PACKAGE / "orchestration/jit_materialization/publish.py",
            "publish_materialization",
        ),
    )
    for path, function_name in publishers:
        calls = _function_calls(_function(path, function_name))
        assert "commit_prepared_authority_state_transition_locked" in calls
        assert not (calls & _POINTER_MUTATORS)


def test_raw_work_unit_state_mutation_has_closed_owners() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "vfx_harness.orchestration.unit_state_lock"
                and any(alias.name in _RAW_STATE_MUTATORS for alias in node.names)
                and relative not in _RAW_STATE_MUTATOR_OWNERS
            ):
                imported = sorted(
                    alias.name for alias in node.names if alias.name in _RAW_STATE_MUTATORS
                )
                violations.append(
                    f"{relative}:{node.lineno}:import:{','.join(imported)}"
                )
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _RAW_STATE_MUTATORS and relative not in _RAW_STATE_MUTATOR_OWNERS:
                violations.append(f"{relative}:{node.lineno}:{name}")
    assert not violations, (
        "raw authority-bound state writes bypass the ordinary selection-first "
        "mutation facade: " + ", ".join(violations)
    )


def test_ordinary_state_mutation_decorator_is_selection_then_state() -> None:
    function = _function(
        PACKAGE / "orchestration/unit_state_lock.py",
        "serialized_state_mutation",
    )
    source = ast.get_source_segment(
        (PACKAGE / "orchestration/unit_state_lock.py").read_text(encoding="utf-8"),
        function,
    )
    assert source is not None
    assert source.index("authority_selection_lock") < source.index("_locked_state_path")


def test_facade_state_writers_are_guarded_by_owned_lock_protocols() -> None:
    guarded_modules = {
        "orchestration/unit_state.py": {"_write"},
        "orchestration/unit_state_claims.py": {"_write"},
        "orchestration/unit_state_replan_transactions.py": {"_write"},
    }
    violations: list[str] = []
    for relative, writer_names in guarded_modules.items():
        path = PACKAGE / relative
        for function in (
            node for node in _tree(path).body if isinstance(node, ast.FunctionDef)
        ):
            if function.name == "_write":
                continue
            if not (_function_calls(function) & writer_names):
                continue
            if "serialized_state_mutation" not in _decorator_names(function):
                violations.append(f"{relative}:{function.lineno}:{function.name}")

    layer_finalization = PACKAGE / "orchestration/layer_finalization_state.py"
    for function in (
        node for node in _tree(layer_finalization).body if isinstance(node, ast.FunctionDef)
    ):
        calls = _function_calls(function)
        writes_facade = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "unit_state"
            and node.func.attr == "_write"
            for node in ast.walk(function)
        )
        if writes_facade and "_selected_layer_state_locks" not in calls:
            violations.append(
                f"orchestration/layer_finalization_state.py:{function.lineno}:"
                f"{function.name}"
            )

    assert not violations, (
        "ordinary facade state writers must use the selection-first serialized "
        "decorator or the multi-layer selection-first guard: " + ", ".join(violations)
    )


def test_only_unit_state_facade_imports_raw_storage_writer() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "vfx_harness.orchestration.unit_state_storage"
                and any(alias.name == "write" for alias in node.names)
                and relative != "orchestration/unit_state.py"
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, (
        "raw JSON state storage writer may be imported only by the unit-state facade: "
        + ", ".join(violations)
    )


def test_retired_apply_replan_has_no_production_callers() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and any(alias.name == "apply_replan" for alias in node.names)
            ):
                violations.append(f"{relative}:{node.lineno}:import")
            if isinstance(node, ast.Call) and _called_name(node) == "apply_replan":
                violations.append(f"{relative}:{node.lineno}:call")

    assert not violations, (
        "apply_replan is a retired self-heal primitive and has no production facade "
        "or caller: "
        + ", ".join(violations)
    )


def test_fixture_state_initializer_has_no_production_callers() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        if relative == "orchestration/unit_state.py":
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "vfx_harness.orchestration.unit_state"
                and any(alias.name == "initialize" for alias in node.names)
            ):
                violations.append(f"{relative}:{node.lineno}:import")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "unit_state"
                and node.func.attr == "initialize"
            ):
                violations.append(f"{relative}:{node.lineno}:call")

    assert not violations, (
        "selected work-unit state must be created by the atomic authority-state "
        "transition; initialize is only a low-level fixture helper: "
        + ", ".join(violations)
    )


def test_completion_authority_is_never_erased_into_an_untyped_map() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "authorized_receipt_digests":
                violations.append(f"{relative}:{node.lineno}:raw-map-name")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "receipt_digests"
            ):
                violations.append(f"{relative}:{node.lineno}:map-projection")
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and getattr(node, "id", getattr(node, "attr", None))
                == "digest_matched_passed"
            ):
                violations.append(f"{relative}:{node.lineno}:retired-query")

    assert not violations, (
        "current unit-completion authority must remain typed through every consumer: "
        + ", ".join(violations)
    )


def test_completion_authorization_attestations_have_closed_factories() -> None:
    constructors = frozenset(
        {
            "AuthorizedLayerFinalizationMutation",
            "AuthorizedUnitCompletionSet",
            "CandidateAuthorizedUnitCompletionSet",
        }
    )
    owners = frozenset(
        {
            "orchestration/layer_finalization_state.py",
            "orchestration/unit_completion_state.py",
            "evaluation/plan_gate/preview_authorization.py",
        }
    )
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in constructors and relative not in owners:
                violations.append(f"{relative}:{node.lineno}:{name}")

    assert not violations, (
        "completion authorization attestations may be minted only by their verified "
        "live or preview factories: "
        + ", ".join(violations)
    )
