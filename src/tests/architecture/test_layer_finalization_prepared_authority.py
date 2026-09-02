"""Prepared finalization authority has one inward-safe production issuer."""

from __future__ import annotations

import ast
import inspect
from collections import defaultdict
from functools import cache
from pathlib import Path

from vfx_harness.agents.builder import layer_artifact, layer_finalization_guard
from vfx_harness.orchestration import (
    layer_evaluation_receipts,
    layer_finalization_publication_authority,
    layer_outcome_publication,
    layer_replay_receipts,
    plan_consumer_ledger_projection,
    shot_ledger_publication,
)


def _production_python_files() -> tuple[Path, ...]:
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]
    return tuple(package.rglob("*.py"))


@cache
def _production_private_symbol_references() -> dict[str, frozenset[Path]]:
    references: defaultdict[str, set[Path]] = defaultdict(set)
    for path in _production_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    references[alias.name].add(path.resolve())
            elif isinstance(node, ast.Attribute):
                references[node.attr].add(path.resolve())
            elif isinstance(node, ast.Name):
                references[node.id].add(path.resolve())
    return {
        symbol: frozenset(paths)
        for symbol, paths in references.items()
    }


def _private_symbol_violations(
    symbol: str,
    *,
    owners: set[Path],
) -> list[str]:
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]
    return [
        path.relative_to(package).as_posix()
        for path in sorted(_production_private_symbol_references().get(symbol, ()))
        if path not in owners
    ]


def test_prepared_finalization_authority_leaf_imports_no_builder_module() -> None:
    source = Path(layer_finalization_publication_authority.__file__).read_text(
        encoding="utf-8"
    )
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(module.startswith("vfx_harness.agents") for module in imports)


def test_builder_guard_is_the_only_production_issuer() -> None:
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]
    owner = Path(layer_finalization_guard.__file__).resolve()
    call = "_issue_layer_finalization_prepared_mutation_authorization("
    violations = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.resolve() not in {
            owner,
            Path(layer_finalization_publication_authority.__file__).resolve(),
        }
        and call in path.read_text(encoding="utf-8")
    ]

    assert not violations


def test_builder_guard_is_the_only_production_issuer_binding() -> None:
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]
    owner = Path(layer_finalization_guard.__file__).resolve()
    call = "_bind_layer_finalization_prepared_mutation_issuer("
    violations = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.resolve() not in {
            owner,
            Path(layer_finalization_publication_authority.__file__).resolve(),
        }
        and call in path.read_text(encoding="utf-8")
    ]

    assert not violations


def test_builder_guard_is_the_only_production_active_hold_registrar() -> None:
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]
    owner = Path(layer_finalization_guard.__file__).resolve()
    call = "_hold_layer_finalization_prepared_mutation_guard("
    violations = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.resolve()
        not in {
            owner,
            Path(layer_finalization_publication_authority.__file__).resolve(),
        }
        and call in path.read_text(encoding="utf-8")
    ]

    assert not violations


def test_prepared_finalization_issuer_is_not_public() -> None:
    assert "issue_layer_finalization_prepared_mutation_authorization" not in (
        layer_finalization_publication_authority.__all__
    )
    assert not hasattr(
        layer_finalization_publication_authority,
        "issue_layer_finalization_prepared_mutation_authorization",
    )


def test_typed_finalization_preparations_expose_no_generic_publication() -> None:
    capabilities = (
        layer_artifact.PreparedLayerArtifact,
        layer_replay_receipts.PreparedLayerReplayReceipt,
        layer_evaluation_receipts.PreparedLayerEvaluationReceipt,
        layer_outcome_publication.PreparedLayerOutcomePublication,
    )

    for capability in capabilities:
        assert capability.__slots__ == ("__weakref__",)
        assert "publication" not in capability.__dict__
        assert "temporary_descriptor" not in capability.__dict__
        for method_name in ("__getattr__", "__copy__", "__deepcopy__"):
            method = capability.__dict__.get(method_name)
            if method is not None:
                source = inspect.getsource(method)
                assert '"publication"' not in source
                assert '"temporary_descriptor"' not in source


def test_each_typed_preparation_registry_has_one_module_owner() -> None:
    owners = {
        "_require_prepared_layer_artifact": Path(layer_artifact.__file__).resolve(),
        "_require_prepared_layer_replay_receipt": Path(
            layer_replay_receipts.__file__
        ).resolve(),
        "_require_prepared_layer_evaluation_receipt": Path(
            layer_evaluation_receipts.__file__
        ).resolve(),
        "_require_prepared_layer_outcome": Path(
            layer_outcome_publication.__file__
        ).resolve(),
    }
    package = Path(layer_finalization_guard.__file__).resolve().parents[2]

    for private_reader, owner in owners.items():
        violations = [
            path.relative_to(package).as_posix()
            for path in package.rglob("*.py")
            if path.resolve() != owner
            and private_reader in path.read_text(encoding="utf-8")
        ]
        assert not violations, private_reader


def test_each_typed_preparation_private_surface_has_one_module_owner() -> None:
    owner_symbols = {
        Path(layer_artifact.__file__).resolve(): {
            "_require_prepared_layer_artifact",
            "_mint_prepared_layer_artifact",
            "_retire_prepared_layer_artifact",
            "_PREPARED_ARTIFACTS",
            "_PreparedLayerArtifactRecord",
        },
        Path(layer_replay_receipts.__file__).resolve(): {
            "_require_prepared_layer_replay_receipt",
            "_mint_prepared_layer_replay_receipt",
            "_retire_prepared_layer_replay_receipt",
            "_PREPARED_REPLAY_RECEIPTS",
            "_PreparedLayerReplayReceiptRecord",
        },
        Path(layer_evaluation_receipts.__file__).resolve(): {
            "_require_prepared_layer_evaluation_receipt",
            "_mint_prepared_layer_evaluation_receipt",
            "_retire_prepared_layer_evaluation_receipt",
            "_PREPARED_EVALUATION_RECEIPTS",
            "_PreparedLayerEvaluationReceiptRecord",
        },
        Path(layer_outcome_publication.__file__).resolve(): {
            "_require_prepared_layer_outcome",
            "_mint_prepared_layer_outcome",
            "_retire_prepared_layer_outcome",
            "_PREPARED_OUTCOMES",
            "_PreparedLayerOutcomeRecord",
        },
    }

    for owner, symbols in owner_symbols.items():
        for symbol in symbols:
            assert not _private_symbol_violations(symbol, owners={owner}), symbol


def test_destination_issuer_surface_is_closed_to_typed_sink_modules() -> None:
    destination_module = (
        Path(layer_artifact.__file__).resolve().parents[2]
        / "observability"
        / "prepared_publication_destinations.py"
    ).resolve()
    owners = {
        destination_module,
        Path(layer_artifact.__file__).resolve(),
        Path(layer_replay_receipts.__file__).resolve(),
        Path(layer_evaluation_receipts.__file__).resolve(),
        Path(layer_outcome_publication.__file__).resolve(),
        Path(plan_consumer_ledger_projection.__file__).resolve(),
        Path(shot_ledger_publication.__file__).resolve(),
    }
    for symbol in {
        "_bind_prepared_publication_destination_issuer",
        "_issue_prepared_publication_destination_authorization",
        "_DESTINATION_ISSUER",
    }:
        assert not _private_symbol_violations(symbol, owners=owners), symbol


def test_finalization_mutation_issuer_surface_has_closed_owners() -> None:
    authority_owner = Path(
        layer_finalization_publication_authority.__file__
    ).resolve()
    guard_owner = Path(layer_finalization_guard.__file__).resolve()
    for symbol in {
        "_bind_layer_finalization_prepared_mutation_issuer",
        "_hold_layer_finalization_prepared_mutation_guard",
        "_issue_layer_finalization_prepared_mutation_authorization",
    }:
        assert not _private_symbol_violations(
            symbol,
            owners={authority_owner, guard_owner},
        ), symbol
    assert not _private_symbol_violations(
        "_PREPARED_MUTATION_ISSUER",
        owners={guard_owner},
    )
    assert not _private_symbol_violations(
        "consume_layer_finalization_prepared_mutation_authorization",
        owners={
            authority_owner,
            Path(layer_artifact.__file__).resolve(),
            Path(layer_replay_receipts.__file__).resolve(),
            Path(layer_evaluation_receipts.__file__).resolve(),
            Path(layer_outcome_publication.__file__).resolve(),
        },
    )


def test_typed_preparation_registries_quiesce_every_fork() -> None:
    modules = (
        layer_artifact,
        layer_replay_receipts,
        layer_evaluation_receipts,
        layer_outcome_publication,
    )

    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        registrations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_fork_participant"
        ]
        assert len(registrations) == 1, module.__name__
        assert {keyword.arg for keyword in registrations[0].keywords} == {
            "lock_factory",
            "after_in_child",
        }, module.__name__
        coordinated = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fork_coordinated_lock"
        ]
        assert len(coordinated) == 1, module.__name__
