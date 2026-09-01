"""Work-unit finalization cannot publish or masquerade as layer authority."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from vfx_harness.agents.builder import revalidate, unit_finalize, unit_loop


def test_unit_builder_exposes_no_layer_publication_switch() -> None:
    assert "publish_layer" not in inspect.signature(unit_loop.build_unit).parameters


def test_unit_finalizer_contains_only_unit_outcome_publication() -> None:
    source = inspect.getsource(unit_finalize._publish_unit_outcome)

    assert "publish_unit_evaluation_outcome" in source
    for forbidden in (
        "publish_unit_layer_outcome",
        "prepare_layer_revalidation",
        "commit_layer_revalidation",
        "record_ablation",
        "._ablate(",
    ):
        assert forbidden not in source


def test_unit_paths_import_no_layer_outcome_publisher() -> None:
    forbidden = {
        "vfx_harness.agents.builder.layer_outcome",
        "vfx_harness.orchestration.layer_outcome_publication",
    }
    for module in (unit_finalize, unit_loop, revalidate):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not (imported & forbidden), module.__name__


def test_unit_revalidation_reasserts_exact_unit_boundary() -> None:
    source = inspect.getsource(revalidate._try_revalidate)

    assert "attempt_guard.require_unit_boundary" in source
    assert source.index("attempt_guard.require_unit_boundary") < source.index(
        "ledger.mark(m, \"passed\""
    )
