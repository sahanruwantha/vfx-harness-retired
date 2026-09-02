"""Canonical shot.json has one opaque, writer-fenced physical sink."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from vfx_harness.orchestration import (
    ledger,
    plan_consumer_ledger_projection,
    plan_consumer_state_snapshot,
    plan_consumer_view_allocation,
    plan_consumer_view_installation,
    plan_consumer_view_mutation,
    plan_consumer_view_projection,
    shot_ledger_publication,
)
from vfx_harness.orchestration.jit_materialization import candidate_preview

PACKAGE = Path(ledger.__file__).resolve().parents[1]

_TYPED_PUBLICATION_SYMBOLS = frozenset(
    {
        "commit_shot_ledger_publication",
        "discard_prepared_shot_ledger_publication",
        "prepare_shot_ledger_publication",
    }
)
_TYPED_PUBLICATION_OWNERS = frozenset(
    {
        "orchestration/ledger.py",
        "orchestration/shot_ledger_publication.py",
    }
)
_PREPARED_COMMIT_OWNERS = frozenset(
    {
        "agents/acceptance.py",
        "agents/builder/authority.py",
        "orchestration/ledger.py",
    }
)


def _sources() -> tuple[Path, ...]:
    return tuple(sorted(PACKAGE.rglob("*.py")))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function {name} in {_relative(path)}")


def test_legacy_direct_shot_ledger_writer_is_deleted() -> None:
    assert not (PACKAGE / "orchestration/ledger_publication.py").exists()


def test_typed_shot_ledger_physical_sink_has_closed_callers() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                == "vfx_harness.orchestration.shot_ledger_publication"
            ):
                imported = sorted(
                    alias.name
                    for alias in node.names
                    if alias.name in _TYPED_PUBLICATION_SYMBOLS
                )
                if imported and relative not in _TYPED_PUBLICATION_OWNERS:
                    violations.append(
                        f"{relative}:{node.lineno}:import:{','.join(imported)}"
                    )
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if (
                name in _TYPED_PUBLICATION_SYMBOLS
                and relative not in _TYPED_PUBLICATION_OWNERS
            ):
                violations.append(f"{relative}:{node.lineno}:{name}")
    assert not violations, (
        "typed shot-ledger publication primitives must stay behind Ledger: "
        + ", ".join(violations)
    )


def test_prepared_ledger_commit_has_closed_production_callers() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and _called_name(node) == "commit_prepared_save"
                and relative not in _PREPARED_COMMIT_OWNERS
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, (
        "canonical ledger commit must stay inside base, builder, or acceptance owner: "
        + ", ".join(violations)
    )


def test_typed_shot_ledger_preparation_exposes_no_generic_publication() -> None:
    capability = shot_ledger_publication.PreparedShotLedgerPublication
    assert capability.__slots__ == ("__weakref__",)
    assert "publication" not in capability.__dict__
    assert "temporary" not in capability.__dict__
    source = inspect.getsource(capability.__getattr__)
    assert '"publication"' not in source
    assert '"temporary"' not in source


def test_physical_ledger_lock_is_coupled_to_global_inner_order() -> None:
    path = PACKAGE / "orchestration/shot_ledger_lock.py"
    function = _function(path, "shot_ledger_mutation_lock")
    source = ast.get_source_segment(path.read_text(encoding="utf-8"), function)
    assert source is not None
    assert source.index("ordered_authority_inner_lock") < source.index("ledger_lock")


def test_each_sanctioned_legacy_adapter_acquires_writer_before_owner_guard() -> None:
    builder_path = PACKAGE / "agents/builder/authority.py"
    builder = ast.get_source_segment(
        builder_path.read_text(encoding="utf-8"),
        _function(builder_path, "save"),
    )
    assert builder is not None
    assert builder.index("shot_authority_writer_fence") < builder.index(
        "execution_guard.publish"
    )

    acceptance_path = PACKAGE / "agents/acceptance.py"
    acceptance = ast.get_source_segment(
        acceptance_path.read_text(encoding="utf-8"),
        _function(acceptance_path, "publish_ledger"),
    )
    assert acceptance is not None
    assert acceptance.index("shot_authority_writer_fence") < acceptance.index(
        "commit_selected_authority"
    )


def test_shot_ledger_private_registry_surface_has_one_owner() -> None:
    owner = Path(shot_ledger_publication.__file__).resolve()
    symbols = {
        "_PreparedShotLedgerRecord",
        "_register_prepared_shot_ledger",
        "_require_prepared_shot_ledger",
        "_resolve_prepared_shot_ledger",
        "_retire_prepared_shot_ledger",
    }
    violations: list[str] = []
    for path in _sources():
        if path.resolve() == owner:
            continue
        tree = _tree(path)
        for node in ast.walk(tree):
            referenced = None
            if isinstance(node, ast.Name) and node.id in symbols:
                referenced = node.id
            elif isinstance(node, ast.Attribute) and node.attr in symbols:
                referenced = node.attr
            elif isinstance(node, ast.ImportFrom):
                imported = sorted(alias.name for alias in node.names if alias.name in symbols)
                if imported:
                    referenced = ",".join(imported)
            if referenced is not None:
                violations.append(f"{_relative(path)}:{node.lineno}:{referenced}")
    assert not violations, (
        "shot-ledger private registry surface escaped its owner: "
        + ", ".join(violations)
    )


def test_shot_ledger_registry_uses_one_process_fork_coordinator() -> None:
    owner = Path(shot_ledger_publication.__file__).resolve()
    tree = _tree(owner)
    raw_fork_registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_at_fork"
    ]
    participant_registrations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_fork_participant"
    ]
    assert not raw_fork_registrations
    assert len(participant_registrations) == 1

    direct_lock_access: list[str] = []
    coordinated_calls: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "_LOCK"
                for item in node.items
            )
        ):
            direct_lock_access.append(f"with:{node.lineno}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_LOCK"
            and node.func.attr in {"acquire", "release"}
        ):
            direct_lock_access.append(f"{node.func.attr}:{node.lineno}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fork_coordinated_lock"
        ):
            coordinated_calls.append(node.lineno)
    assert not direct_lock_access
    assert len(coordinated_calls) == 1

    registry_accessors: set[str] = set()
    coordinated_accessors: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(child, ast.Name) and child.id == "_PREPARED"
            for child in ast.walk(node)
        ):
            registry_accessors.add(node.name)
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_locked"
            for child in ast.walk(node)
        ):
            coordinated_accessors.add(node.name)
    assert registry_accessors - {"_after_fork_child"} <= coordinated_accessors
    assert coordinated_accessors == {
        "_prepared_gone",
        "_register_prepared_shot_ledger",
        "_resolve_prepared_shot_ledger",
        "_retire_prepared_shot_ledger",
    }


def test_consumer_ledger_raw_copy_and_projection_sinks_are_deleted() -> None:
    snapshot_path = Path(plan_consumer_state_snapshot.__file__).resolve()
    snapshot = ast.get_source_segment(
        snapshot_path.read_text(encoding="utf-8"),
        _function(snapshot_path, "_snapshot_ledger"),
    )
    assert snapshot is not None
    assert "create_construction_ledger_snapshot" in snapshot
    assert ".write_bytes(" not in snapshot
    assert "atomic_write(" not in snapshot

    projection_path = Path(candidate_preview.__file__).resolve()
    projection = ast.get_source_segment(
        projection_path.read_text(encoding="utf-8"),
        _function(projection_path, "_project_candidate_ledger"),
    )
    assert projection is not None
    assert "update_plan_consumer_ledger" in projection
    assert ".write_bytes(" not in projection
    assert "atomic_write(" not in projection


def test_consumer_ledger_mutators_require_opaque_capability_parameters() -> None:
    snapshot = inspect.signature(
        plan_consumer_state_snapshot.snapshot_consumer_execution_authority
    )
    assert tuple(snapshot.parameters) == ("layout", "capability", "layers")
    project = inspect.signature(candidate_preview.project_candidate_authority_state)
    assert tuple(project.parameters) == ("capability", "publication")
    assert "project_candidate_authority_state" not in candidate_preview.__all__
    update = inspect.signature(
        plan_consumer_ledger_projection.update_plan_consumer_ledger
    )
    assert tuple(update.parameters) == ("capability", "update")
    assert not hasattr(
        plan_consumer_ledger_projection,
        "create_plan_consumer_ledger_snapshot",
    )
    assert plan_consumer_ledger_projection.__all__ == [
        "update_plan_consumer_ledger"
    ]

    for capability in (
        plan_consumer_view_allocation.PreparedPlanConsumerViewAllocation,
        plan_consumer_view_installation.PlanConsumerViewInstallationRename,
        plan_consumer_view_mutation.PlanConsumerViewMutationCapability,
        plan_consumer_view_mutation.PreparedPlanConsumerViewInstallation,
    ):
        assert capability.__slots__ == ("__weakref__",)


def test_consumer_view_final_rename_is_owned_by_typed_installation() -> None:
    plan_path = PACKAGE / "orchestration/plan_authority.py"
    prepare = ast.get_source_segment(
        plan_path.read_text(encoding="utf-8"),
        _function(plan_path, "prepare_consumer_view"),
    )
    assert prepare is not None
    assert "populate_plan_consumer_view" in prepare
    assert "os.replace(temp, view)" not in prepare
    population_path = PACKAGE / "orchestration/plan_consumer_view_population.py"
    population = ast.get_source_segment(
        population_path.read_text(encoding="utf-8"),
        _function(population_path, "populate_plan_consumer_view"),
    )
    assert population is not None
    assert "install_plan_consumer_view" in population

    owner_path = PACKAGE / "orchestration/plan_consumer_view_lifecycle.py"
    installation = ast.get_source_segment(
        owner_path.read_text(encoding="utf-8"),
        _function(owner_path, "install_plan_consumer_view"),
    )
    assert installation is not None
    assert "_install_and_verify_allocated_plan_consumer_view" in installation
    assert "_recover_installed_cleanup" in installation
    assert "shutil.rmtree(temp" not in population
    assert "ignore_errors=True" not in population


def test_consumer_view_population_has_one_descriptor_rooted_sink() -> None:
    plan_path = PACKAGE / "orchestration/plan_authority.py"
    prepare = ast.get_source_segment(
        plan_path.read_text(encoding="utf-8"),
        _function(plan_path, "prepare_consumer_view"),
    )
    assert prepare is not None
    population_path = PACKAGE / "orchestration/plan_consumer_view_population.py"
    population_source = population_path.read_text(encoding="utf-8")
    population = ast.get_source_segment(
        population_source,
        _function(population_path, "populate_plan_consumer_view"),
    )
    assert population is not None
    state_path = PACKAGE / "orchestration/plan_consumer_state_snapshot.py"
    state_source = state_path.read_text(encoding="utf-8")
    forbidden = {
        "mkdir",
        "makedirs",
        "open",
        "replace",
        "rename",
        "rmdir",
        "symlink",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
    violations: list[str] = []
    for path, tree in (
        (plan_path, ast.parse(prepare, filename=str(plan_path))),
        (
            population_path,
            _tree(population_path),
        ),
        (state_path, _tree(state_path)),
    ):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in forbidden:
                violations.append(
                    f"{_relative(path)}:{node.lineno}:{_called_name(node)}"
                )
    assert not violations, (
        "consumer-view population escaped its exact descriptor-rooted sink: "
        + ", ".join(violations)
    )
    assert "plan_consumer_view_projection.create_regular_file" in population_source
    assert (
        "plan_consumer_view_projection.create_verified_file_symlink"
        in population_source
    )
    assert "plan_consumer_view_projection.create_construction_ledger_snapshot" in (
        state_source
    )
    assert population.rindex(
        "prepare_plan_consumer_view_installation"
    ) > population.rindex(
        "_project_jit_plans"
    )
    assert population.rindex(
        "prepare_plan_consumer_view_installation"
    ) > population.rindex(
        "require_regular_file_bytes"
    )
    assert plan_consumer_view_projection.__all__ == [
        "create_construction_ledger_snapshot",
        "create_regular_file",
        "create_verified_file_symlink",
        "create_verified_state_symlink",
        "ensure_directory",
        "read_exact_source_file",
        "require_member_absent",
        "require_regular_file_bytes",
    ]


def test_consumer_view_issuance_and_mutation_callers_are_closed() -> None:
    allowed = {
        "allocating_plan_consumer_view": {
            "orchestration/plan_consumer_view_population.py"
        },
        "constructing_plan_consumer_view": {
            "orchestration/plan_consumer_view_population.py"
        },
        "discard_plan_consumer_view_allocation": {
            "orchestration/plan_consumer_view_allocation.py",
        },
        "discard_prepared_plan_consumer_view_installation": {
            "orchestration/plan_consumer_view_population.py"
        },
        "prepare_plan_consumer_view_installation": {
            "orchestration/plan_consumer_view_population.py"
        },
        "install_plan_consumer_view": {
            "orchestration/plan_consumer_view_population.py"
        },
        "require_plan_consumer_view_install_destination": {
            "orchestration/plan_authority.py"
        },
        "mutating_plan_consumer_view": {
            "orchestration/jit_materialization/publish.py"
        },
        "snapshot_consumer_execution_authority": {
            "orchestration/plan_consumer_view_population.py"
        },
        "update_plan_consumer_ledger": {
            "orchestration/jit_materialization/candidate_preview.py",
            "orchestration/plan_consumer_ledger_projection.py",
        },
        "project_candidate_authority_state": set(),
        "create_construction_ledger_snapshot": {
            "orchestration/plan_consumer_state_snapshot.py"
        },
        "create_regular_file": {
            "orchestration/plan_consumer_state_snapshot.py",
            "orchestration/plan_consumer_view_population.py",
        },
        "create_verified_file_symlink": {
            "orchestration/plan_consumer_view_population.py",
            "orchestration/plan_consumer_view_projection.py",
        },
        "create_verified_state_symlink": {
            "orchestration/plan_consumer_state_snapshot.py"
        },
        "ensure_directory": {
            "orchestration/jit_materialization/candidate_preview.py",
            "orchestration/jit_materialization/publish.py",
            "orchestration/plan_consumer_state_snapshot.py",
            "orchestration/plan_consumer_view_population.py",
        },
        "require_member_absent": {
            "orchestration/plan_consumer_state_snapshot.py"
        },
        "require_regular_file_bytes": {
            "orchestration/plan_consumer_view_population.py"
        },
        "read_exact_source_file": {
            "orchestration/plan_consumer_view_population.py",
            "orchestration/plan_consumer_view_projection.py",
        },
        "populate_plan_consumer_view": {"orchestration/plan_authority.py"},
    }
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in allowed and relative not in allowed[name]:
                violations.append(f"{relative}:{node.lineno}:{name}")
    assert not violations, (
        "plan-consumer capability issuance escaped its exact owner call sites: "
        + ", ".join(violations)
    )


def test_consumer_view_private_physical_primitives_have_closed_owners() -> None:
    allowed = {
        "_allocate_plan_consumer_view": {
            "orchestration/plan_consumer_view_allocation.py"
        },
        "_canonical_layout_descriptors": {
            "orchestration/plan_consumer_view_allocation.py",
            "orchestration/plan_consumer_view_transaction.py",
        },
        "claiming_plan_consumer_view_allocation": {
            "orchestration/plan_consumer_view_allocation.py",
            "orchestration/plan_consumer_view_mutation.py",
        },
        "_begin_plan_consumer_view_installation_rename": {
            "orchestration/plan_consumer_view_installation.py"
        },
        "_mint_rename_receipt": {
            "orchestration/plan_consumer_view_installation.py"
        },
        "_claimed_rename_receipt": {
            "orchestration/plan_consumer_view_installation.py"
        },
        "_recover_failed_begin": {
            "orchestration/plan_consumer_view_installation.py"
        },
        "_rename_child_noreplace": {
            "orchestration/plan_consumer_view_cleanup.py"
        },
        "move_owned_directory_noreplace": {
            "orchestration/plan_consumer_installed_projection.py",
            "orchestration/plan_consumer_owned_directory.py",
            "orchestration/plan_consumer_view_cleanup.py",
            "orchestration/plan_consumer_view_installation.py",
        },
        "create_and_capture_empty_directory": {
            "orchestration/plan_consumer_owned_directory.py",
        },
        "create_and_publish_empty_directory": {
            "orchestration/plan_consumer_installed_projection.py",
            "orchestration/plan_consumer_owned_directory.py",
            "orchestration/plan_consumer_view_allocation.py",
            "orchestration/plan_consumer_view_projection.py",
        },
        "open_beneath_directory": {
            "orchestration/plan_consumer_installed_projection.py",
            "orchestration/plan_consumer_owned_directory.py",
        },
        "_retire_owned_directory": {
            "orchestration/plan_consumer_view_cleanup.py"
        },
        "retire_owned_directory": {
            "orchestration/plan_consumer_view_allocation.py",
            "orchestration/plan_consumer_view_cleanup.py",
            "orchestration/plan_consumer_view_installation.py",
        },
        "_install_and_verify_allocated_plan_consumer_view": {
            "orchestration/plan_consumer_view_lifecycle.py"
        },
        "_discard_prepared_plan_consumer_temporary": {
            "orchestration/plan_consumer_view_lifecycle.py"
        },
        "_discard_previous_plan_consumer_view": {
            "orchestration/plan_consumer_view_lifecycle.py"
        },
        "_exclusive_plan_consumer_view_transaction": {
            "orchestration/plan_consumer_view_lifecycle.py",
            "orchestration/plan_consumer_view_mutation.py",
        },
        "_advance_constructed_plan_consumer_ledger_binding": {
            "orchestration/plan_consumer_ledger_projection.py",
            "orchestration/plan_consumer_view_projection.py"
        },
        "_require_current_plan_consumer_view_mutation": {
            "orchestration/plan_consumer_installed_projection.py",
            "orchestration/plan_consumer_ledger_projection.py",
            "orchestration/plan_consumer_view_projection.py",
        },
    }
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                imported = sorted(
                    alias.name for alias in node.names if alias.name in allowed
                )
                for name in imported:
                    if relative not in allowed[name]:
                        violations.append(f"{relative}:{node.lineno}:import:{name}")
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in allowed and relative not in allowed[name]:
                violations.append(f"{relative}:{node.lineno}:call:{name}")
    assert not violations, (
        "plan-consumer raw allocation/install primitives escaped their exact "
        "owner modules: "
        + ", ".join(violations)
    )


def test_consumer_view_installation_receipt_and_module_surface_are_opaque() -> None:
    assert plan_consumer_view_installation.__all__ == []
    assert plan_consumer_view_installation.PlanConsumerViewInstallationRename.__slots__ == (
        "__weakref__",
    )


def test_consumer_ledger_destination_issuer_has_one_owner() -> None:
    owner = Path(plan_consumer_ledger_projection.__file__).resolve()
    symbols = {
        "_bind_prepared_publication_destination_issuer",
        "_issue_prepared_publication_destination_authorization",
        "_DESTINATION_ISSUER",
    }
    violations: list[str] = []
    for path in _sources():
        if path.resolve() == owner:
            continue
        tree = _tree(path)
        for node in ast.walk(tree):
            referenced = None
            if isinstance(node, ast.Name) and node.id in symbols:
                referenced = node.id
            elif isinstance(node, ast.Attribute) and node.attr in symbols:
                referenced = node.attr
            elif isinstance(node, ast.ImportFrom):
                imported = sorted(
                    alias.name for alias in node.names if alias.name in symbols
                )
                if imported:
                    referenced = ",".join(imported)
            if referenced is not None:
                # Other typed sink modules own their own issuer with the same
                # private symbol names; this invariant is about the new family.
                if path.name in {
                    "layer_artifact.py",
                    "layer_evaluation_receipts.py",
                    "layer_outcome_publication.py",
                    "layer_replay_receipts.py",
                    "prepared_publication_destinations.py",
                    "shot_ledger_publication.py",
                }:
                    continue
                violations.append(f"{_relative(path)}:{node.lineno}:{referenced}")
    assert not violations, (
        "plan-consumer ledger destination issuer escaped typed sink owners: "
        + ", ".join(violations)
    )
