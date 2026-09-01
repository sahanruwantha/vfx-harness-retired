from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationPredecessorInput,
)
from vfx_harness.orchestration.layer_finalization_predecessors import (
    LayerFinalizationPredecessorConflict,
    require_exact_selected_finalization_predecessors,
)
from vfx_harness.orchestration.ledger import Layer


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _layer(layer_id: str) -> Layer:
    return Layer(
        id=layer_id,
        script=f"build/layer_{layer_id}.py",
        title=f"Layer {layer_id}",
        judges=((1, "refs/f001.png"),),
        reads="fixture",
        owns=("form",),
        primary_judge=1,
        stages=(
            _unit(
                f"unit_{layer_id}",
                script_span=f"build/units/{layer_id}/unit_{layer_id}.py",
            ),
        ),
    )


def _input(layer: Layer) -> LayerFinalizationPredecessorInput:
    return LayerFinalizationPredecessorInput.mint(
        layer_id=str(layer.id),
        finalization_receipt_digest=_digest(f"receipt {layer.id}"),
        script_path=str(layer.script),
        script_sha256=_digest(f"script {layer.id}"),
    )


@pytest.mark.parametrize("variant", ["omitted", "reordered", "extra"])
def test_selected_prefix_rejects_inexact_rows_under_same_selection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    first, second, target, extra = (_layer(value) for value in ("1", "2", "3", "4"))
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_predecessors."
        "selected_layer_chain",
        lambda *_args, **_kwargs: (first, second, target),
    )
    rows = {
        "omitted": (_input(second),),
        "reordered": (_input(second), _input(first)),
        "extra": (_input(first), _input(second), _input(extra)),
    }[variant]

    with pytest.raises(
        LayerFinalizationPredecessorConflict,
        match="differs from the selected stable DAG",
    ):
        require_exact_selected_finalization_predecessors(
            tmp_path,
            target.id,
            rows,
            selection_token=ABSENT_SELECTION_TOKEN,
            selected_authority=SimpleNamespace(
                selection_token=ABSENT_SELECTION_TOKEN
            ),
        )


def test_selected_prefix_accepts_exact_stable_order(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, target = (_layer(value) for value in ("1", "2", "3"))
    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_finalization_predecessors."
        "selected_layer_chain",
        lambda *_args, **_kwargs: (first, second, target),
    )

    assert require_exact_selected_finalization_predecessors(
        tmp_path,
        target.id,
        (_input(first), _input(second)),
        selection_token=ABSENT_SELECTION_TOKEN,
        selected_authority=SimpleNamespace(
            selection_token=ABSENT_SELECTION_TOKEN
        ),
    ) == (first, second)
