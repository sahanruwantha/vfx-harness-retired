"""JIT materialization coverage for deferred qualitative judgment debt (HIR-0163)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    validate_materialization,
)
from vfx_harness.orchestration.jit_materialization.publish import _overlay_documents
from vfx_harness.orchestration.unit_state import unit_digest

BUNDLE_HASH = hashlib.sha256(b"judgment-debt-materialization").hexdigest()


def _fixture_base_selection() -> dict:
    return AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=hashlib.sha256(b"fixture-plan-pointer").hexdigest(),
        jit_revision=0,
        jit_pointer_sha256=None,
    ).to_dict()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _layer(
    layer_id: str,
    *,
    title: str,
    owns: list[str],
    reserved_roles: list[str],
    owned_requirements: list[str],
    depends_on_layers: list[str],
    provides: dict[str, list[str]],
) -> dict:
    return {
        "id": layer_id,
        "script": f"build/{int(layer_id):02d}_{title.lower()}.py",
        "title": title,
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/reference.png"}],
        "owns": owns,
        "reads": title,
        "evidence_domains": ["image", "scene", "projected_composition"],
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": depends_on_layers,
            "required_outcomes": [],
            "provides": provides,
            "reserved_roles": reserved_roles,
            "owned_requirements": owned_requirements,
        },
    }


def _claim(
    *,
    claim_id: str,
    proposition: str,
    axis: str,
    property_kind: str,
    role: str,
    repair_owner: str,
    contract_id: str,
) -> dict:
    return {
        "id": claim_id,
        "proposition": proposition,
        "axis": axis,
        "property": property_kind,
        "subject_roles": [role],
        "subject_controls": [],
        "moments": [1],
        "kind": "atomic",
        "required": True,
        "authority": "executable_required",
        "repair_owner": repair_owner,
        "asserts": "scene",
        "evidence": [{"kind": "scene_contract", "id": contract_id}],
    }


def _unit(
    *,
    layer_id: str,
    unit_id: str,
    role: str,
    axis: str,
    property_kind: str,
    contract_id: str,
    provides: list[str],
    composition_contract_ids: list[str] | None = None,
) -> dict:
    evaluation = {
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/reference.png"}],
        "temporal_evidence": "none",
        "claims": [
            _claim(
                claim_id=f"{unit_id}-claim",
                proposition=f"{unit_id} establishes its declared state",
                axis=axis,
                property_kind=property_kind,
                role=role,
                repair_owner=unit_id,
                contract_id=contract_id,
            )
        ],
    }
    if composition_contract_ids:
        evaluation["composition_context"] = {
            "frames": [1],
            "contract_ids": composition_contract_ids,
        }
    return {
        "id": unit_id,
        "title": unit_id,
        "plan": f"plans/{int(layer_id):02d}_{unit_id}.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": [role],
            "controls": [],
            "control_roles": {},
            "script_spans": [f"build/units/{int(layer_id):02d}/{unit_id}.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "look_capabilities": [],
        "provides": provides,
        "evaluation": evaluation,
        "completion": "all_required_claims_and_protected_contracts_pass",
    }


def _scene_contract(
    *,
    contract_id: str,
    layer_id: str,
    axis: str,
    role: str,
    kind: str = "object_count",
) -> dict:
    row = {
        "id": contract_id,
        "kind": kind,
        "owner_layer": layer_id,
        "fault_owner": layer_id,
        "activates_at": layer_id,
        "lifecycle": "layer",
        "axis": axis,
        "roles": [role],
    }
    if kind == "visible_fraction":
        row.update({"frame": 1, "op": "min", "lo": 0.25})
    else:
        row.update({"op": "min", "lo": 1})
    return row


def _judgment(*, property_kind: str = "camera_framing") -> dict:
    return {
        "claim_kind": "atomic",
        "property": property_kind,
        "fault_owner": "camera",
        "subject_roles": ["hall"],
        "axes": ["camera_alignment"],
        "moments": [1],
        "carrier_families": ["mesh"],
        "observation_medium": "workbench_solid",
        "lifecycle": "persistent",
    }


def _camera_payload(*, judgment_property: str = "camera_framing") -> dict:
    layer = _layer(
        "1",
        title="Camera",
        owns=["camera_alignment"],
        reserved_roles=["camera.rig"],
        owned_requirements=["R-camera"],
        depends_on_layers=[],
        provides={"camera": ["camera.rig"]},
    )
    layer["execution"] = "ready"
    layer.pop("jit")
    layer["stages"] = [
        _unit(
            layer_id="1",
            unit_id="camera",
            role="camera.rig",
            axis="camera_alignment",
            property_kind="object_count",
            contract_id="camera-rig-count",
            provides=["camera"],
            # HIR-0184: the camera layer frames the hall it shares judge frame 1 with.
            composition_contract_ids=["hall-frame-f1"],
        )
    ]
    return {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": BUNDLE_HASH,
        "base_selection": _fixture_base_selection(),
        "layer": layer,
        "scene_contracts": [
            _scene_contract(
                contract_id="camera-rig-count",
                layer_id="1",
                axis="camera_alignment",
                role="camera.rig",
            ),
            {
                "id": "hall-frame-f1",
                "kind": "bbox_height",
                "owner_layer": "1",
                "fault_owner": "1",
                "activates_at": "2",
                "lifecycle": "persistent",
                "axis": "camera_alignment",
                # The reserved hall.* namespace itself: the form unit's hall.mass pays it,
                # and the ledger republication test that renames hall.mass leaves layer 1 intact.
                "roles": ["hall.*"],
                "frame": 1,
                "op": "band",
                "lo": 0.3,
                "hi": 0.6,
            },
        ],
        "image_contracts": [],
        "requirement_bindings": [
            {
                "requirement_id": "R-camera",
                "decision": {
                    "statement": "The camera framing reads as authored around the hall.",
                    "decision_strength": "approved_start",
                    "judgment": _judgment(property_kind=judgment_property),
                },
            }
        ],
        "acceptance": [],
    }


def _form_payload() -> dict:
    layer = _layer(
        "2",
        title="Form",
        owns=["form"],
        reserved_roles=["hall.*"],
        owned_requirements=["R-form"],
        depends_on_layers=["1"],
        provides={},
    )
    layer["execution"] = "ready"
    layer.pop("jit")
    layer["stages"] = [
        _unit(
            layer_id="2",
            unit_id="hall_form",
            role="hall.mass",
            axis="form",
            property_kind="object_count",
            contract_id="hall-count",
            provides=["geometry"],
            composition_contract_ids=["hall-visible"],
        )
    ]
    return {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": BUNDLE_HASH,
        "base_selection": _fixture_base_selection(),
        "layer": layer,
        "scene_contracts": [
            _scene_contract(
                contract_id="hall-count",
                layer_id="2",
                axis="form",
                role="hall.mass",
            ),
            _scene_contract(
                contract_id="hall-visible",
                layer_id="2",
                axis="form",
                role="hall.mass",
                kind="visible_fraction",
            ),
        ],
        "image_contracts": [],
        "requirement_bindings": [
            {
                "requirement_id": "R-form",
                "contract_ids": ["hall-count"],
            }
        ],
        "acceptance": [],
    }


def _fixture_root(tmp_path: Path, *, form_reserved_roles: list[str] | None = None) -> Path:
    (tmp_path / "refs").mkdir()
    (tmp_path / "brief.md").write_text("Judgment debt fixture.\n", encoding="utf-8")
    camera = _layer(
        "1",
        title="Camera",
        owns=["camera_alignment"],
        reserved_roles=["camera.rig"],
        owned_requirements=["R-camera"],
        depends_on_layers=[],
        provides={"camera": ["camera.rig"]},
    )
    form = _layer(
        "2",
        title="Form",
        owns=["form"],
        reserved_roles=form_reserved_roles or ["hall.*"],
        owned_requirements=["R-form"],
        depends_on_layers=["1"],
        provides={},
    )
    _write(tmp_path / "layers.json", {"schema": 5, "layers": [camera, form]})
    _write(tmp_path / "scene_checks.json", {"schema": 2, "contracts": []})
    _write(tmp_path / "checks.json", {"schema": 2, "checks": []})
    _write(tmp_path / "acceptance.json", [])
    _write(
        tmp_path / "requirements.json",
        {
            "schema": "vfx-harness.requirements/v2",
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
            "requirements": [
                {
                    "id": "R-camera",
                    "statement": "The camera framing reads as authored around the hall.",
                    "citation": {"source": "brief.md", "sha256": "0" * 64, "line_start": 1, "line_end": 1},
                    "resolution": {
                        "kind": "deferred_owner",
                        "ids": [],
                        "owner_layer": "1",
                        "due": {"kind": "before_layer", "layer": "1"},
                        "evidence_domains": ["image"],
                    },
                },
                {
                    "id": "R-form",
                    "statement": "The hall has a rendered form.",
                    "citation": {"source": "brief.md", "sha256": "0" * 64, "line_start": 1, "line_end": 1},
                    "resolution": {
                        "kind": "deferred_owner",
                        "ids": [],
                        "owner_layer": "2",
                        "due": {"kind": "before_layer", "layer": "2"},
                        "evidence_domains": ["scene"],
                    },
                },
            ],
        },
    )
    return tmp_path


def _write_payload(root: Path, name: str, payload: dict) -> Path:
    path = root / name
    _write(path, payload)
    return path


def _overlay_camera_view(root: Path, materialized) -> Path:
    bases = {
        name: root / name
        for name in ("layers.json", "scene_checks.json", "checks.json", "requirements.json", "acceptance.json")
    }
    overlaid = _overlay_documents(materialized, bases)
    view = root / "camera-overlay"
    for name, document in overlaid.items():
        _write(view / name, document)
    return view


def test_camera_materialization_defers_judgment_debt_until_matching_form_layer(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    camera_payload = _write_payload(root, "camera.json", _camera_payload())

    camera = validate_materialization(root, camera_payload, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)

    assert len(camera.judgment_debt_definitions) == 1
    assert camera.judgment_debt_definitions[0]["binding"]["activates_at"] == "2"
    assert camera.judgment_debt_activations == ()


def test_form_materialization_activates_overlaid_camera_debt_once_with_exact_payer_digest(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    camera_payload = _write_payload(root, "camera.json", _camera_payload())
    camera = validate_materialization(root, camera_payload, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)
    overlay = _overlay_camera_view(root, camera)
    form_payload = _write_payload(root, "form.json", _form_payload())

    form = validate_materialization(
        root,
        form_payload,
        expected_bundle_hash=BUNDLE_HASH,
        base_layers_path=overlay / "layers.json",
        base_scene_checks_path=overlay / "scene_checks.json",
        base_requirements_path=overlay / "requirements.json",
        shot_folder=root,)

    assert len(form.judgment_debt_activations) == 1
    activation = form.judgment_debt_activations[0]
    assert activation["definition_digest"] == camera.judgment_debt_definitions[0]["definition_digest"]
    assert activation["payer_layer"] == "2"
    assert activation["payer_units"] == [
        {
            "unit_id": "2:hall_form",
            "digest": unit_digest(form.layer.stages[0]),
        }
    ]


def test_camera_materialization_rejects_no_future_matching_reserved_role_carrier(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path, form_reserved_roles=["props.table"])
    camera_payload = _write_payload(root, "camera.json", _camera_payload())

    with pytest.raises(ValueError, match="no matching provider"):
        validate_materialization(root, camera_payload, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)


def test_camera_owner_cannot_misown_subject_appearance_debt(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    camera_payload = _write_payload(
        root,
        "camera.json",
        _camera_payload(judgment_property="subject_appearance"),
    )

    with pytest.raises(ValueError, match="subject_appearance cannot be owned"):
        validate_materialization(root, camera_payload, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)


def _form_qualitative_payload(
    *,
    property_kind: str,
    fault_owner: str,
    subject_role: str,
) -> dict:
    payload = _form_payload()
    payload["requirement_bindings"] = [
        {
            "requirement_id": "R-form",
            "decision": {
                "statement": "The hall has a rendered form.",
                "decision_strength": "approved_start",
                "judgment": {
                    "claim_kind": "atomic",
                    "property": property_kind,
                    "fault_owner": fault_owner,
                    "subject_roles": [subject_role],
                    "axes": ["form"],
                    "moments": [1],
                    "carrier_families": ["mesh"],
                    "observation_medium": "workbench_solid",
                    "lifecycle": "persistent",
                },
            },
        }
    ]
    return payload


def _make_form_requirement_qualitative(root: Path) -> None:
    requirements_path = root / "requirements.json"
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    requirements["requirements"][1]["resolution"]["evidence_domains"] = ["image"]
    _write(requirements_path, requirements)


@pytest.mark.parametrize("property_kind", ["subject_appearance", "reference_identity"])
def test_non_camera_judgment_requires_fault_owner_subject_closure(
    tmp_path: Path,
    property_kind: str,
) -> None:
    root = _fixture_root(tmp_path)
    _make_form_requirement_qualitative(root)
    payload = _form_qualitative_payload(
        property_kind=property_kind,
        fault_owner="hall_detail",
        subject_role="hall.mass",
    )
    payload["layer"]["stages"].append(
        _unit(
            layer_id="2",
            unit_id="hall_detail",
            role="hall.detail",
            axis="form",
            property_kind="object_count",
            contract_id="hall-detail-count",
            provides=[],
        )
    )
    payload["scene_contracts"].append(
        _scene_contract(
            contract_id="hall-detail-count",
            layer_id="2",
            axis="form",
            role="hall.detail",
        )
    )
    path = _write_payload(root, f"unrelated-{property_kind}.json", payload)

    with pytest.raises(ValueError, match=r"subjects escape fault owner.*hall\.mass"):
        validate_materialization(root, path, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)


@pytest.mark.parametrize("property_kind", ["subject_appearance", "reference_identity"])
def test_non_camera_judgment_accepts_fault_owner_subject_closure(
    tmp_path: Path,
    property_kind: str,
) -> None:
    root = _fixture_root(tmp_path)
    _make_form_requirement_qualitative(root)
    path = _write_payload(
        root,
        f"closed-{property_kind}.json",
        _form_qualitative_payload(
            property_kind=property_kind,
            fault_owner="hall_form",
            subject_role="hall.mass",
        ),
    )

    materialized = validate_materialization(root, path, expected_bundle_hash=BUNDLE_HASH, shot_folder=root)

    assert materialized.judgment_debt_definitions[0]["seed"]["property"] == property_kind
