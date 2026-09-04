"""A recorded vocabulary gap closes a structural requirement, and blocks padding (HIR-0202).

Caesar run 20260904T143311Z-c0f282, requirement R20 ("reproduce the visual composition, not
the documentary's text labels"), domain projected_composition: the materializer recorded
VG-001 naming three registry kinds and why none can detect burned-in text, was told by the
tool to close with a decision, and the validator answered with two mutually exclusive
findings — "every declared domain already has contract evidence" on a binding with zero
contract ids, and "does not pay ['projected_composition']". The only shape that passed bound
twelve bbox_height rows to a text-absence proposition.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_judgment_debt_materialization import (
    BUNDLE_HASH,
    _camera_payload,
    _fixture_root,
    _form_payload,
    _overlay_camera_view,
    _write_payload,
)
from vfx_harness.orchestration.jit_materialization import validate_materialization
from vfx_harness.orchestration.jit_materialization.validate_requirements import (
    recorded_vocabulary_gap_ids,
)

STATEMENT = "The camera framing reads as authored around the hall."
FORM_STATEMENT = "The hall has a rendered form."


def _record_gap(root: Path, requirement_id: str, gap_id: str = "VG-001") -> None:
    path = root / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema": "vfx-harness.vocabulary-gap/v1",
                    "id": gap_id,
                    "requirement_id": requirement_id,
                    "claim": FORM_STATEMENT,
                    "attempted": [
                        {"kind": "node_count", "why_it_cannot_certify": "self-certifying tautology"},
                        {"kind": "render_region_stat", "why_it_cannot_certify": "cannot read glyphs"},
                    ],
                    "note": "",
                    "run_id": "test",
                }
            )
            + "\n"
        )


def _validated_form(root: Path, binding: dict):
    """Validate the structural (scene-domain) form layer with one requirement binding."""
    camera = validate_materialization(
        root, _write_payload(root, "camera.json", _camera_payload()), expected_bundle_hash=BUNDLE_HASH
    )
    overlay = _overlay_camera_view(root, camera)
    payload = _form_payload()
    payload["requirement_bindings"] = [binding]
    return validate_materialization(
        root,
        _write_payload(root, "form.json", payload),
        expected_bundle_hash=BUNDLE_HASH,
        base_layers_path=overlay / "layers.json",
        base_scene_checks_path=overlay / "scene_checks.json",
    )


def test_gap_records_are_read_by_requirement_id(tmp_path: Path) -> None:
    assert recorded_vocabulary_gap_ids(tmp_path) == {}

    _record_gap(tmp_path, "R20")
    _record_gap(tmp_path, "R20", "VG-002")
    _record_gap(tmp_path, "R31", "VG-003")
    assert recorded_vocabulary_gap_ids(tmp_path) == {
        "R20": ("VG-001", "VG-002"),
        "R31": ("VG-003",),
    }

    # A malformed or foreign row is ignored, never raised, inside a validator.
    path = tmp_path / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"schema": "other/v1", "id": "X", "requirement_id": "R9"}) + "\n")
    assert "R9" not in recorded_vocabulary_gap_ids(tmp_path)


def test_a_decision_on_a_structural_requirement_is_refused_with_the_true_reason(
    tmp_path: Path,
) -> None:
    """The old message claimed contract evidence existed for a binding that had none."""
    root = _fixture_root(tmp_path)

    with pytest.raises(ValueError) as refused:
        _validated_form(
            root,
            {
                "requirement_id": "R-form",
                "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
            },
        )

    message = str(refused.value)
    assert "declares only structural domains" in message
    assert "escalate_vocabulary_gap" in message, "the message names the path that makes it legal"
    assert "already has contract evidence" not in message, (
        "the false finding told the model to delete the only binding it had"
    )


def test_a_recorded_gap_lets_the_decision_pay_the_structural_domain(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    validated = _validated_form(
        root,
        {
            "requirement_id": "R-form",
            "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
        },
    )

    assert validated is not None, "the escalation path the tool prescribes now terminates in a pass"


def test_a_recorded_gap_refuses_closing_the_same_requirement_with_contracts(
    tmp_path: Path,
) -> None:
    """The padding that actually happened: rows that cannot measure the statement."""
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    with pytest.raises(ValueError) as refused:
        _validated_form(root, dict(_form_payload()["requirement_bindings"][0]))

    message = str(refused.value)
    assert "recorded vocabulary gap(s) ['VG-001']" in message
    assert "cannot then be closed by contract bindings" in message
    assert "approved_start" in message and "retract the gap" in message
