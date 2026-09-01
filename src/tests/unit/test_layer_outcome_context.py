from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vfx_harness.orchestration import layer_outcome_context, layer_publication


def _layer(identifier: str):
    return SimpleNamespace(id=identifier)


def test_prior_context_uses_only_receipt_verified_outcome_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    shot = SimpleNamespace(folder=tmp_path)
    prior = _layer("1")
    active = _layer("2")
    selected = SimpleNamespace()
    monkeypatch.setattr(
        layer_outcome_context,
        "_selected_chain",
        lambda *_args: (prior, active),
    )
    outcome = {
        "layer": "1",
        "script": "build/01.py",
        "decided_by": "deterministic",
        "authoritative_passed": 2,
        "authoritative_total": 2,
        "finalization_receipt": {"receipt_digest": "a" * 64},
    }
    monkeypatch.setattr(
        layer_publication,
        "require_current_layer_publication",
        lambda folder, layer, authority: (
            SimpleNamespace(outcome_bytes=json.dumps(outcome).encode("utf-8"))
            if (folder, layer, authority) == (tmp_path, prior, selected)
            else pytest.fail("prior context verified the wrong publication")
        ),
    )

    block = layer_outcome_context.prior_outcomes_block(
        shot,
        "2",
        selected_authority=selected,
    )

    assert "Receipt-backed prior-layer outcomes" in block
    assert "a" * 64 in block
    assert "authoritative checks 2/2" in block


def test_prior_context_refuses_unverified_predecessor(
    tmp_path,
    monkeypatch,
) -> None:
    shot = SimpleNamespace(folder=tmp_path)
    prior = _layer("1")
    active = _layer("2")
    monkeypatch.setattr(
        layer_outcome_context,
        "_selected_chain",
        lambda *_args: (prior, active),
    )
    monkeypatch.setattr(
        layer_publication,
        "require_current_layer_publication",
        lambda *_args: (_ for _ in ()).throw(
            layer_publication.LayerPublicationConflict("missing terminal receipt")
        ),
    )

    with pytest.raises(ValueError, match="current receipt-backed publication"):
        layer_outcome_context.prior_outcomes_block(
            shot,
            "2",
            selected_authority=SimpleNamespace(),
        )
