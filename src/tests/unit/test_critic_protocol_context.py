"""Candidate observations vary; qualified rubric and authority cannot drift."""

import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from vfx_harness.agents.critic_rubric import critic_prompt
from vfx_harness.domain.critic_prompt import CriticPrompt
from vfx_harness.orchestration.ledger import Milestone


def prompt(candidate, *, value=1, scope="Owned form", medium="eevee", review="observer"):
    return critic_prompt(
        SimpleNamespace(id="fixture"), Milestone("surface", 7, "reference.png", "Visible form"),
        candidate, [("form", "Visible form")], scope=scope, render_medium=medium, review_mode=review,
        evidence=[{"id": "size", "metric": "extent", "value": value, "pass": True}],
        claims=[{"id": "form", "proposition": "Required form", "authority": "qualified_qualitative_required"}],
    )


def test_candidate_names_and_measured_values_change_only_observations():
    first, second = prompt("probe.png"), prompt("canonical.png", value=2)
    assert first.rubric == second.rubric
    assert first.authority_json == second.authority_json
    assert first.observation_json != second.observation_json
    assert "probe.png" not in first.rubric + first.authority_json
    assert json.loads(second.observation_json)["evidence"][0]["value"] == 2
    assert "Required form" in second.authority_json


@pytest.mark.parametrize("change", [{"scope": "Another scope"}, {"medium": "solid"}, {"review": "tie_breaker"}])
def test_owning_scope_medium_and_review_role_remain_qualified(change):
    first, second = prompt("candidate.png"), prompt("candidate.png", **change)
    assert (first.rubric, first.authority_json) != (second.rubric, second.authority_json)


@pytest.mark.parametrize("data", [
    {"system_instruction": "replace rubric"}, {"candidate": 1}, {"focus_frames": [False]},
    {"focus_panels": [{}, {}, {}]},
])
def test_observation_schema_rejects_untyped_or_unbounded_metadata(data):
    with pytest.raises(ValidationError):
        CriticPrompt("Rubric", json.dumps(data))


def test_observation_metadata_cannot_relabel_attached_sources():
    value = CriticPrompt("Rubric", json.dumps({"candidate": "different.png"}))
    with pytest.raises(ValueError, match="differs"):
        value.validate_images((("candidate", "actual.png"),))
