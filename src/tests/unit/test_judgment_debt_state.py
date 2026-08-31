"""Durable lifecycle tests without selected-plan fixture setup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.orchestration import judgment_debt_state

BUNDLE_DIGEST = hashlib.sha256(b"selected judgment-debt bundle").hexdigest()
UNIT_DIGEST = hashlib.sha256(b"payer unit").hexdigest()
EVIDENCE_DIGEST = hashlib.sha256(b"qualified evidence").hexdigest()


def _receipt_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[tuple[Path, ...], dict[str, str]]:
    """Install a two-layer selected replay prefix with hash-pinned unit state."""
    unit_digests = {
        "1:camera": hashlib.sha256(b"camera-unit").hexdigest(),
        "2:hall_form": hashlib.sha256(b"form-unit").hexdigest(),
    }
    layers: dict[str, SimpleNamespace] = {}
    states: dict[str, dict] = {}
    scripts: list[Path] = []
    for layer_id, unit_id in (("1", "camera"), ("2", "hall_form")):
        layer_script = tmp_path / "build" / f"{int(layer_id):02d}_layer.py"
        unit_script = tmp_path / "build" / "units" / layer_id / f"{unit_id}.py"
        layer_script.parent.mkdir(parents=True, exist_ok=True)
        unit_script.parent.mkdir(parents=True, exist_ok=True)
        layer_script.write_text(f"# layer {layer_id}\n", encoding="utf-8")
        unit_script.write_text(f"# unit {unit_id}\n", encoding="utf-8")
        unit = SimpleNamespace(
            id=unit_id,
            digest=unit_digests[f"{layer_id}:{unit_id}"],
            mutates=SimpleNamespace(
                script_spans=(unit_script.relative_to(tmp_path).as_posix(),)
            ),
        )
        layers[layer_id] = SimpleNamespace(
            id=layer_id,
            script=layer_script.relative_to(tmp_path).as_posix(),
            stages=(unit,),
        )
        script_hash = hashlib.sha256(unit_script.read_bytes()).hexdigest()
        states[layer_id] = {
            "units": {
                unit_id: {
                    "status": "passed",
                    "unit_hash": unit.digest,
                    "checkpoint": {
                        "unit_hash": unit.digest,
                        "script_hash": script_hash,
                    },
                }
            }
        }
        scripts.append(layer_script)
    monkeypatch.setattr(judgment_debt_state, "selected_artifact_path", lambda *_args: tmp_path / "layers.json")
    monkeypatch.setattr(judgment_debt_state, "load_layers_from_path", lambda _path: layers)
    monkeypatch.setattr(judgment_debt_state, "load_unit_state", lambda _root, layer_id: states[layer_id])
    monkeypatch.setattr(judgment_debt_state, "validate_current", lambda *_args: None)
    monkeypatch.setattr(judgment_debt_state, "unit_digest", lambda unit: unit.digest)
    return tuple(scripts), unit_digests


def test_replay_prefix_receipt_is_ordered_hash_pinned_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts, unit_digests = _receipt_fixture(tmp_path, monkeypatch)

    receipt = judgment_debt_state.replay_prefix_receipt(
        tmp_path,
        replayed_layer_scripts=scripts,
    )

    assert [layer.layer_id for layer in receipt.layers] == ["1", "2"]
    assert receipt.unit_digests == tuple(unit_digests.items())
    payload = receipt.as_dict()
    assert payload["schema"] == judgment_debt_state.REPLAY_PREFIX_RECEIPT_SCHEMA
    assert payload["replay_prefix_digest"] == receipt.digest
    assert payload["layers"][1]["units"][0] == {
        "layer_id": "2",
        "unit_id": "hall_form",
        "unit_digest": unit_digests["2:hall_form"],
        "checkpoint_unit_digest": unit_digests["2:hall_form"],
        "script_path": "build/units/2/hall_form.py",
        "script_sha256": hashlib.sha256(
            (tmp_path / "build" / "units" / "2" / "hall_form.py").read_bytes()
        ).hexdigest(),
        "checkpoint_script_sha256": hashlib.sha256(
            (tmp_path / "build" / "units" / "2" / "hall_form.py").read_bytes()
        ).hexdigest(),
    }
    assert judgment_debt_state.replay_prefix_unit_digests(
        tmp_path,
        replayed_layer_scripts=scripts,
    ) == tuple(unit_digests.items())
    assert judgment_debt_state.replay_prefix_receipt(
        tmp_path,
        replayed_layer_scripts=scripts,
    ).digest == receipt.digest

    scripts[1].write_text("# changed layer receipt artifact\n", encoding="utf-8")
    assert judgment_debt_state.replay_prefix_receipt(
        tmp_path,
        replayed_layer_scripts=scripts,
    ).digest != receipt.digest


def test_replay_prefix_receipt_rejects_checkpoint_script_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts, _unit_digests = _receipt_fixture(tmp_path, monkeypatch)
    (tmp_path / "build" / "units" / "2" / "hall_form.py").write_text(
        "# changed after checkpoint\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="artifact changed after its checkpoint"):
        judgment_debt_state.replay_prefix_receipt(
            tmp_path,
            replayed_layer_scripts=scripts,
        )


def _definition_and_activation() -> tuple[JudgmentDebtDefinition, JudgmentDebtActivation]:
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
            judge_points=(JudgmentPoint(frame=1, ref="refs/hall.png"),),
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
        payer_unit_digests=(("form:hall", UNIT_DIGEST),),
    )
    return definition, activation


def _patch_authority(
    monkeypatch: pytest.MonkeyPatch,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
) -> None:
    monkeypatch.setattr(
        judgment_debt_state,
        "_current_authority",
        lambda _shot: (BUNDLE_DIGEST, (definition,), (activation,)),
    )


def _append_raw_event(
    shot: Path,
    *,
    bundle_digest: str,
    definition: JudgmentDebtDefinition,
    state: JudgmentDebtState,
    predecessor: JudgmentDebtState | None = None,
) -> None:
    path = shot / "state" / judgment_debt_state.EVENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": judgment_debt_state.EVENT_SCHEMA,
        "bundle_digest": bundle_digest,
        "debt_id": definition.debt_id,
        "definition_digest": definition.digest,
        "predecessor_state_digest": (predecessor or JudgmentDebtState.pending(definition)).digest,
        "state": state.as_dict(),
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_current_states_implicitly_start_pending_not_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)

    rows = judgment_debt_state.current_judgment_debt_states(tmp_path)

    assert rows == ((definition, activation, JudgmentDebtState.pending(definition)),)


def test_exact_activation_transitions_due_then_satisfied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)

    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replayed_unit_digests=activation.payer_unit_digests,
    )
    satisfied = judgment_debt_state.resolve_current_judgment_debt(
        tmp_path,
        definition.digest,
        outcome="satisfied",
        evidence_digest=EVIDENCE_DIGEST,
    )

    assert due.status == "due"
    assert due.activation_digest == activation.digest
    assert satisfied.status == "satisfied"
    assert satisfied.activation_digest == activation.digest
    judgment_debt_state.require_judgment_debts_satisfied(tmp_path)


def test_due_transitions_to_falsified_and_blocks_final_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)
    judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replayed_unit_digests=activation.payer_unit_digests,
    )

    falsified = judgment_debt_state.resolve_current_judgment_debt(
        tmp_path,
        definition.digest,
        outcome="falsified",
        evidence_digest=EVIDENCE_DIGEST,
    )

    assert falsified.status == "falsified"
    with pytest.raises(ValueError, match=rf"{definition.debt_id}=falsified"):
        judgment_debt_state.require_judgment_debts_satisfied(tmp_path)


def test_state_replay_refuses_skipped_and_terminal_rewritten_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)
    skipped = JudgmentDebtState(
        definition.digest,
        "satisfied",
        EVIDENCE_DIGEST,
        activation.digest,
    )
    _append_raw_event(
        tmp_path,
        bundle_digest=BUNDLE_DIGEST,
        definition=definition,
        state=skipped,
    )

    with pytest.raises(ValueError, match="skips or rewrites"):
        judgment_debt_state.current_judgment_debt_states(tmp_path)

    due = JudgmentDebtState(definition.digest, "due", activation_digest=activation.digest)
    satisfied = JudgmentDebtState(
        definition.digest,
        "satisfied",
        EVIDENCE_DIGEST,
        activation.digest,
    )
    falsified = JudgmentDebtState(
        definition.digest,
        "falsified",
        EVIDENCE_DIGEST,
        activation.digest,
    )
    path = tmp_path / "state" / judgment_debt_state.EVENTS
    path.unlink()
    predecessor = JudgmentDebtState.pending(definition)
    for state in (due, satisfied, falsified):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema": judgment_debt_state.EVENT_SCHEMA,
                        "bundle_digest": BUNDLE_DIGEST,
                        "debt_id": definition.debt_id,
                        "definition_digest": definition.digest,
                        "predecessor_state_digest": predecessor.digest,
                        "state": state.as_dict(),
                    }
                )
                + "\n"
            )
        predecessor = state

    with pytest.raises(ValueError, match="skips or rewrites"):
        judgment_debt_state.current_judgment_debt_states(tmp_path)


def test_stale_bundle_events_are_inert_and_pending_debt_refuses_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)
    stale_bundle = hashlib.sha256(b"superseded bundle").hexdigest()
    _append_raw_event(
        tmp_path,
        bundle_digest=stale_bundle,
        definition=definition,
        state=JudgmentDebtState(definition.digest, "due", activation_digest=activation.digest),
    )

    rows = judgment_debt_state.current_judgment_debt_states(tmp_path)

    assert rows[0][2].status == "pending_not_due"
    with pytest.raises(ValueError, match=rf"{definition.debt_id}=pending_not_due"):
        judgment_debt_state.require_judgment_debts_satisfied(tmp_path)


def test_superseded_payer_activation_starts_a_new_payment_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, old_activation = _definition_and_activation()
    _append_raw_event(
        tmp_path,
        bundle_digest=BUNDLE_DIGEST,
        definition=definition,
        state=JudgmentDebtState(
            definition.digest,
            "due",
            activation_digest=old_activation.digest,
        ),
    )
    new_activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("form:hall", hashlib.sha256(b"replacement unit").hexdigest()),),
    )
    _patch_authority(monkeypatch, definition, new_activation)

    rows = judgment_debt_state.current_judgment_debt_states(tmp_path)

    assert rows[0][2].status == "pending_not_due"
    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replayed_unit_digests=new_activation.payer_unit_digests,
    )
    assert due.activation_digest == new_activation.digest
