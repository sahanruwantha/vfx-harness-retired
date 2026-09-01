from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.authority_state_records import (
    AuthorityUnitBinding,
    LayerAuthorityBinding,
)
from vfx_harness.orchestration import unit_completion_state
from vfx_harness.orchestration.unit_completion_state import (
    UnitCompletionConflict,
    authorize_completed_units_for_layer,
)


def test_completion_authorization_requires_coordinator_layer_generation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = _unit("form")
    projection = parse_authority_selection_token(
        ABSENT_SELECTION_TOKEN.to_dict(),
        "fixture selection",
    )
    proposal_digest = "a" * 64
    binding = LayerAuthorityBinding.mint(
        transition_revision=1,
        transition_proposal_digest=proposal_digest,
        selection_token=projection,
        layer_id="1",
        layer_generation_digest="b" * 64,
        units=(
            AuthorityUnitBinding.mint(
                unit_id=unit.id,
                unit_generation_digest="c" * 64,
            ),
        ),
    )
    context = SimpleNamespace(
        head=SimpleNamespace(revision=1, selection_token=projection),
        proposal=SimpleNamespace(digest=proposal_digest),
        commit=SimpleNamespace(
            installed_states=(SimpleNamespace(layer_id="1", binding=binding),)
        ),
    )
    monkeypatch.setattr(
        unit_completion_state,
        "read_authority_selection_heads",
        lambda *_args, **_kwargs: SimpleNamespace(token=ABSENT_SELECTION_TOKEN),
    )
    monkeypatch.setattr(
        unit_completion_state,
        "resolve_current_authority_state",
        lambda *_args, **_kwargs: context,
    )
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)

    with pytest.raises(
        UnitCompletionConflict,
        match="coordinator layer/unit generation baseline",
    ):
        authorize_completed_units_for_layer(
            tmp_path,
            "1",
            (unit,),
            expected_plan_hash="d" * 64,
            selected_authority=selected,
        )
