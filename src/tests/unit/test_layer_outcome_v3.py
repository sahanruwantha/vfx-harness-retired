from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.layer_outcome_fixtures import write_test_layer_outcome
from vfx_harness.domain.layer_outcomes import (
    OUTCOME_SCHEMA,
    LayerOutcomeContractError,
    parse_sealed_layer_outcome,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import revalidation


def _outcome(tmp_path, monkeypatch) -> dict:
    layer = SimpleNamespace(
        id="3",
        title="Receipt-bound layer",
        script="build/layer_3.py",
    )
    script = tmp_path / layer.script
    script.parent.mkdir(parents=True)
    script.write_text("# composed fixture\n", encoding="utf-8")
    evidence = {
        "id": "camera-lock",
        "metric": "object_property",
        "value": 1.0,
        "target": ">= 1",
        "pass": True,
        "source": "interface_contract",
        "authoritative": True,
        "owner_layer": "3",
        "fault_owner": "3",
        "activates_at": "3",
        "lifecycle": "layer",
    }
    canonical = [
        (
            (1, "refs/a.png"),
            {
                "evidence_kind": "executable_only",
                "pass": True,
                "issues": [],
                "evidence": [evidence],
                "decided_by": "unit_executable_evidence",
            },
        )
    ]
    monkeypatch.setattr(revalidation, "input_manifest", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        revalidation,
        "canonical_records",
        lambda *_args, **_kwargs: [
            {
                "evidence_kind": "executable_only",
                "frame": 1,
                "ref": "refs/a.png",
                "ref_sha256": hashlib.sha256(b"fixture reference 0").hexdigest(),
                "input_manifest_sha256": canonical_digest({}),
                "authoritative": [
                    {
                        key: evidence.get(key)
                        for key in (
                            "id",
                            "metric",
                            "value",
                            "target",
                            "pass",
                            "source",
                            "owner_layer",
                            "fault_owner",
                            "activates_at",
                            "lifecycle",
                        )
                    }
                ],
                "authoritative_sha256": canonical_digest(
                    {
                        "authoritative": [
                            {
                                key: evidence.get(key)
                                for key in (
                                    "id",
                                    "metric",
                                    "value",
                                    "target",
                                    "pass",
                                    "source",
                                    "owner_layer",
                                    "fault_owner",
                                    "activates_at",
                                    "lifecycle",
                                )
                            }
                        ]
                    }
                ),
                "qualitative_defects": [],
            }
        ],
    )
    path = write_test_layer_outcome(
        tmp_path,
        layer,
        status="passed",
        best={"round": 1, "mean": 5.0, "render": None},
        canonical=canonical,
        run_id="fixture-outcome-v3",
        attempt=2,
        blender_version="fixture",
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema_three_outcome_exposes_exact_terminal_receipt_digest(
    tmp_path,
    monkeypatch,
) -> None:
    outcome = _outcome(tmp_path, monkeypatch)

    sealed = parse_sealed_layer_outcome(outcome, expected_layer_id="3")

    assert outcome["schema"] == OUTCOME_SCHEMA == 3
    assert sealed.receipt_digest == outcome["finalization_receipt"]["receipt_digest"]
    assert sealed.passed_bindings == frozenset({("scene_contract", "camera-lock")})


def test_schema_two_and_unknown_schema_three_fields_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    outcome = _outcome(tmp_path, monkeypatch)
    legacy = {**outcome, "schema": 2}
    with pytest.raises(LayerOutcomeContractError) as legacy_error:
        parse_sealed_layer_outcome(legacy, expected_layer_id="3")
    assert legacy_error.value.code == "schema"

    unknown = {**outcome, "legacy_projection": {}}
    with pytest.raises(LayerOutcomeContractError) as unknown_error:
        parse_sealed_layer_outcome(unknown, expected_layer_id="3")
    assert unknown_error.value.code == "shape"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("layer", "4", "finalization_layer"),
        ("status", "failed", "finalization_status"),
        ("script", "build/other.py", "finalization_script"),
        ("run_id", "another-run", "finalization_run"),
        ("attempt", 3, "finalization_attempt"),
    ],
)
def test_outcome_repeated_identity_must_equal_terminal_receipt(
    tmp_path,
    monkeypatch,
    field: str,
    value: object,
    code: str,
) -> None:
    outcome = copy.deepcopy(_outcome(tmp_path, monkeypatch))
    outcome[field] = value

    with pytest.raises(LayerOutcomeContractError) as error:
        parse_sealed_layer_outcome(outcome, expected_layer_id=str(outcome["layer"]))

    assert error.value.code == code


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("title", "Altered title", "finalization_title"),
        (
            "best",
            {"round": 99, "mean": 1.0, "render": None},
            "finalization_best",
        ),
        ("decided_by", "critic", "finalization_decided_by"),
        ("authoritative_total", 2, "finalization_authoritative_total"),
        ("authoritative_passed", 0, "finalization_authoritative_passed"),
        ("failed_contracts", ["camera-lock"], "finalization_failed_contracts"),
        ("interfaces", [], "finalization_interfaces"),
        (
            "revalidation_manifest",
            {"tampered": True},
            "finalization_revalidation_manifest",
        ),
        ("canonical", [], "finalization_canonical"),
        ("at", "2026-09-01T00:00:00+00:00", "finalization_at"),
    ],
)
def test_every_repeated_outcome_projection_must_equal_terminal_receipt(
    tmp_path,
    monkeypatch,
    field: str,
    value: object,
    code: str,
) -> None:
    outcome = copy.deepcopy(_outcome(tmp_path, monkeypatch))
    outcome[field] = value

    with pytest.raises(LayerOutcomeContractError) as error:
        parse_sealed_layer_outcome(outcome, expected_layer_id="3")

    assert error.value.code == code


def test_tampered_embedded_terminal_receipt_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    outcome = copy.deepcopy(_outcome(tmp_path, monkeypatch))
    outcome["finalization_receipt"]["projection"]["outcome"]["status"] = "failed"

    with pytest.raises(LayerOutcomeContractError) as error:
        parse_sealed_layer_outcome(outcome, expected_layer_id="3")

    assert error.value.code == "finalization_receipt"
