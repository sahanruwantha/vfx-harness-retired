"""Current qualitative-observation request compilation is provenance sealed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.blender.observation_environment import (
    SCHEMA as OBSERVATION_ENVIRONMENT_SCHEMA,
)
from vfx_harness.blender.observation_environment import (
    canonical_observation_environment,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentPoint,
    JudgmentProvider,
    activate_judgment_debt,
    compile_judgment_debt,
)
from vfx_harness.domain.refobs import PROMOTED_CONSTRUCTION_SCHEMA
from vfx_harness.orchestration import judgment_observation
from vfx_harness.orchestration.judgment_debt_state import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)


def _digest(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


BUNDLE_DIGEST = _digest("selected-bundle")
PAYER_DIGEST = _digest("form-hall-unit")


def _due_authority() -> tuple[JudgmentDebtDefinition, JudgmentDebtActivation, JudgmentDebtState]:
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id="R-hall",
            statement="The hall reads as the reference.",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="reference_identity",
            owner_layer="camera",
            fault_owner="form",
            subject_roles=("hall",),
            axes=("reference_match",),
            judge_points=(JudgmentPoint(frame=10, ref="refs/hall.png"),),
            observation_medium="workbench_solid",
            lifecycle="persistent",
            bundle_digest=BUNDLE_DIGEST,
            carrier_families=("mesh",),
        ),
        (JudgmentProvider("hall-mesh", "form", "mesh", ("hall.mass",)),),
        layer_dependencies={"camera": (), "form": ("camera",)},
        layer_order=("camera", "form"),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("form:hall", PAYER_DIGEST),),
    )
    due = activate_judgment_debt(
        definition,
        JudgmentDebtState.pending(definition),
        activation=activation,
        layer_id="form",
        replayed_unit_digests=activation.payer_unit_digests,
    )
    return definition, activation, due


def _receipt(*, script_bytes: bytes = b"build hall") -> ReplayPrefixReceipt:
    script_digest = _digest(script_bytes)
    unit = ReplayPrefixUnitReceipt(
        layer_id="form",
        unit_id="hall",
        unit_digest=PAYER_DIGEST,
        checkpoint_unit_digest=PAYER_DIGEST,
        script_path="build/units/form/hall.py",
        script_sha256=script_digest,
        checkpoint_script_sha256=script_digest,
    )
    return ReplayPrefixReceipt((
        ReplayPrefixLayerReceipt(
            layer_id="form",
            script_path="build/layers/form.py",
            script_sha256=_digest(b"composed form layer"),
            units=(unit,),
        ),
    ))


def _environment(*, frame: int = 10, label: str = "base") -> dict:
    return canonical_observation_environment({
        "schema": OBSERVATION_ENVIRONMENT_SCHEMA,
        "frame": frame,
        "observation_medium": "workbench_solid",
        "subject_roles": ["hall"],
        "carrier_families": ["mesh"],
        "render": {"engine": "BLENDER_WORKBENCH", "label": label},
        "subjects": [{"role": "hall", "surface_digest": _digest(f"subject-{label}")}],
    })


def _patch_current_due(
    monkeypatch: pytest.MonkeyPatch,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    due: JudgmentDebtState,
) -> None:
    monkeypatch.setattr(
        judgment_observation,
        "resolve_current",
        lambda _shot: SimpleNamespace(content_hash=BUNDLE_DIGEST),
    )
    monkeypatch.setattr(
        judgment_observation,
        "current_judgment_debt_states",
        lambda _shot: ((definition, activation, due),),
    )


def _write_reference(shot: Path, contents: bytes = b"reference") -> None:
    reference = shot / "refs/hall.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(contents)


def _compile(
    shot: Path,
    definition: JudgmentDebtDefinition,
    *,
    receipt: ReplayPrefixReceipt,
    environment: dict,
) -> judgment_observation.JudgmentObservationRequest:
    return judgment_observation.compile_current_judgment_observation_request(
        shot,
        definition.digest,
        replay_receipt=receipt,
        frame=10,
        ref="refs/hall.png",
        render_mode="solid",
        render_scale=0.5,
        observation_environment=environment,
        comparison_config={"schema": "comparison/v1", "threshold": 0.8},
        judge_config={"schema": "judge/v1", "model": "bounded"},
    )


def test_compiles_exact_due_request_with_reference_view_replay_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    _write_reference(tmp_path)
    receipt = _receipt()
    environment = _environment()

    request = _compile(tmp_path, definition, receipt=receipt, environment=environment)

    assert request.definition_digest == definition.digest
    assert request.activation_digest == activation.digest
    assert request.reference_digest == _digest(b"reference")
    assert request.replay_receipt_digest == receipt.digest
    assert request.observation_environment_digest == environment["digest"]
    assert request.external_asset_provenance_digest == judgment_observation._digest_json({
        "schema": "vfx-harness.judgment-observation-assets/v1",
        "units": [{"unit_id": "form:hall", "construction": "none"}],
    })
    selected_view = BUNDLE_DIGEST
    assert request.owner_view_digest == judgment_observation._digest_json({
        "schema": "vfx-harness.judgment-owner-view/v1",
        "selected_view_digest": selected_view,
        "layer_id": "camera",
    })
    assert request.payer_view_digest == judgment_observation._digest_json({
        "schema": "vfx-harness.judgment-payer-view/v1",
        "selected_view_digest": selected_view,
        "layer_id": "form",
    })


def test_changed_reference_environment_replay_or_view_changes_request_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    _write_reference(tmp_path)
    receipt = _receipt()
    baseline = _compile(tmp_path, definition, receipt=receipt, environment=_environment())

    _write_reference(tmp_path, b"changed reference")
    changed_reference = _compile(tmp_path, definition, receipt=receipt, environment=_environment())
    changed_environment = _compile(tmp_path, definition, receipt=receipt, environment=_environment(label="changed"))
    changed_replay = _compile(
        tmp_path,
        definition,
        receipt=_receipt(script_bytes=b"changed unit script"),
        environment=_environment(),
    )
    monkeypatch.setattr(judgment_observation, "selected_view_digest", lambda _shot, _bundle: _digest("view-two"))
    changed_view = _compile(tmp_path, definition, receipt=receipt, environment=_environment())

    assert changed_reference.digest != baseline.digest
    assert changed_environment.digest != baseline.digest
    assert changed_replay.digest != baseline.digest
    assert changed_view.digest != baseline.digest


def test_rejects_missing_or_undeclared_reference_and_stale_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    receipt = _receipt()
    environment = _environment()

    with pytest.raises(ValueError, match="is missing"):
        _compile(tmp_path, definition, receipt=receipt, environment=environment)

    _write_reference(tmp_path)
    with pytest.raises(ValueError, match="is not declared"):
        judgment_observation.compile_current_judgment_observation_request(
            tmp_path,
            definition.digest,
            replay_receipt=receipt,
            frame=10,
            ref="refs/other.png",
            render_mode="solid",
            render_scale=0.5,
            observation_environment=environment,
            comparison_config={},
            judge_config={},
        )

    stale_environment = dict(environment)
    stale_environment["snapshot"] = {**environment["snapshot"], "frame": 9}
    with pytest.raises(ValueError, match="digest is stale"):
        _compile(tmp_path, definition, receipt=receipt, environment=stale_environment)


def test_promoted_construction_pointer_and_glb_change_are_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation, due = _due_authority()
    _patch_current_due(monkeypatch, definition, activation, due)
    _write_reference(tmp_path)
    receipt = _receipt()
    glb = tmp_path / "build/construction" / f"{_digest(b'first glb')}.glb"
    glb.parent.mkdir(parents=True, exist_ok=True)
    glb.write_bytes(b"first glb")
    pointer = tmp_path / "build/units/form/hall.construction.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)

    def write_pointer(asset: Path) -> None:
        pointer.write_text(json.dumps({
            "schema": PROMOTED_CONSTRUCTION_SCHEMA,
            "layer_id": "form",
            "unit_id": "hall",
            "unit_digest": PAYER_DIGEST,
            "glb": asset.relative_to(tmp_path).as_posix(),
            "sha256": _digest(asset.read_bytes()),
        }), encoding="utf-8")

    write_pointer(glb)
    baseline = _compile(tmp_path, definition, receipt=receipt, environment=_environment())

    glb.write_bytes(b"tampered glb")
    with pytest.raises(ValueError, match="stale GLB bytes"):
        _compile(tmp_path, definition, receipt=receipt, environment=_environment())

    replacement = tmp_path / "build/construction" / f"{_digest(b'replacement glb')}.glb"
    replacement.write_bytes(b"replacement glb")
    write_pointer(replacement)
    changed = _compile(tmp_path, definition, receipt=receipt, environment=_environment())

    assert changed.external_asset_provenance_digest != baseline.external_asset_provenance_digest
    assert changed.digest != baseline.digest
