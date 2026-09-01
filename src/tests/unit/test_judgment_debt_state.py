"""Durable lifecycle tests without selected-plan fixture setup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit_attempt_fixtures import executed_replay_input
from vfx_harness.domain.authority_head_records import (
    AuthoritySelectionTokenProjection,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    REPLAY_PREFIX_RECEIPT_SCHEMA,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtPaymentGeneration,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationClaim,
    LayerFinalizationPredecessorInput,
    LayerFinalizationUnitInput,
)
from vfx_harness.domain.stop_transaction_state import (
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
)
from vfx_harness.orchestration import judgment_debt_replay_mint, judgment_debt_state
from vfx_harness.orchestration.authority_selection import (
    AuthorityPointerObservation,
    ResolvedSelectedAuthority,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)

BUNDLE_DIGEST = hashlib.sha256(b"selected judgment-debt bundle").hexdigest()
UNIT_DIGEST = hashlib.sha256(b"payer unit").hexdigest()
EVIDENCE_DIGEST = hashlib.sha256(b"qualified evidence").hexdigest()


def _receipt_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    tuple[Path, ...],
    dict[str, str],
    ResolvedSelectedAuthority,
    LayerFinalizationClaim,
]:
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
            depends_on=(),
            mutates=SimpleNamespace(
                script_spans=(unit_script.relative_to(tmp_path).as_posix(),)
            ),
        )
        layers[layer_id] = SimpleNamespace(
            id=layer_id,
            script=layer_script.relative_to(tmp_path).as_posix(),
            stages=(unit,),
            execution="ready",
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
                    "completion_receipt": {
                        "receipt_digest": hashlib.sha256(
                            f"completion:{layer_id}:{unit_id}".encode()
                        ).hexdigest()
                    },
                }
            }
        }
        scripts.append(layer_script)
    monkeypatch.setattr(
        judgment_debt_replay_mint.unit_state,
        "load",
        lambda _root, layer_id: states[layer_id],
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint.unit_state,
        "validate_current",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint.unit_state,
        "unit_digest",
        lambda unit: unit.digest,
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint,
        "UnitCompletionReceipt",
        SimpleNamespace(
            parse=lambda value, *_args: SimpleNamespace(
                receipt_digest=value["receipt_digest"]
            )
        ),
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint,
        "authorize_completed_units_for_layer",
        lambda _shot, layer_id, *_args, **_kwargs: SimpleNamespace(
            unit_ids=frozenset(states[layer_id]["units"]),
            receipt_digest=lambda unit_id: states[layer_id]["units"]
            .get(unit_id, {})
            .get("completion_receipt", {})
            .get("receipt_digest")
        ),
    )
    pointer_digest = hashlib.sha256(b"pointer").hexdigest()
    bundle_digest = hashlib.sha256(b"bundle").hexdigest()
    view_digest = hashlib.sha256(b"view").hexdigest()
    selected = ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2(
            "selected",
            SelectedAuthorityBundle(bundle_digest, "clean", pointer_digest),
            SelectedAuthorityView("jit", view_digest, pointer_digest),
        ),
        pointer_observation=AuthorityPointerObservation(
            pointer_digest,
            pointer_digest,
        ),
        selection_token=AuthoritySelectionToken(
            plan_revision=1,
            plan_pointer_sha256=pointer_digest,
            jit_revision=1,
            jit_pointer_sha256=pointer_digest,
        ),
        plan=SimpleNamespace(bundle=SimpleNamespace(content_hash=bundle_digest)),
        artifact_paths={"layers.json": tmp_path / "layers.json"},
    )
    capsule_digests = {
        layer_id: hashlib.sha256(f"layer-capsule:{layer_id}".encode()).hexdigest()
        for layer_id in layers
    }
    selected_prefix = SimpleNamespace(
        layers=tuple(
            SimpleNamespace(
                layer=layers[layer_id],
                capsule=SimpleNamespace(
                    capsule_digest=capsule_digests[layer_id],
                    predecessor_layer_digests=(
                        ()
                        if layer_id == "1"
                        else (("1", capsule_digests["1"]),)
                    ),
                ),
            )
            for layer_id in ("1", "2")
        ),
        require_sources_unchanged=lambda: None,
    )
    predecessor_digest = hashlib.sha256(b"layer-1-finalization").hexdigest()
    predecessor = SimpleNamespace(
        claim=SimpleNamespace(
            layer_id="1",
            unit_inputs=(
                SimpleNamespace(
                    unit_id="camera",
                    unit_digest=unit_digests["1:camera"],
                    completion_receipt_digest=states["1"]["units"]["camera"][
                        "completion_receipt"
                    ]["receipt_digest"],
                    script_path="build/units/1/camera.py",
                    script_sha256=states["1"]["units"]["camera"]["checkpoint"][
                        "script_hash"
                    ],
                ),
            ),
        ),
        receipt_digest=predecessor_digest,
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint.judgment_debt_replay_authority,
        "capture_selected_judgment_replay_prefix",
        lambda *_args, **_kwargs: selected_prefix,
    )
    monkeypatch.setattr(
        judgment_debt_replay_mint.judgment_debt_replay_authority,
        "authorize_observation_replay_prefix",
        lambda *_args, **_kwargs: (predecessor,),
    )
    payer_script = tmp_path / "build" / "units" / "2" / "hall_form.py"
    claim = LayerFinalizationClaim.mint(
        attempt_revision=1,
        run_id="20260901T100000Z-prefix",
        layer_id="2",
        mode="singleton_passthrough",
        selection_token=AuthoritySelectionTokenProjection(
            plan_revision=1,
            plan_pointer_sha256=pointer_digest,
            jit_revision=1,
            jit_pointer_sha256=pointer_digest,
        ),
        plan_hash=capsule_digests["2"],
        layer_script_path=layers["2"].script,
        layer_script_sha256=hashlib.sha256(scripts[1].read_bytes()).hexdigest(),
        unit_inputs=(
            LayerFinalizationUnitInput.mint(
                unit_id="hall_form",
                unit_digest=unit_digests["2:hall_form"],
                completion_receipt_digest=states["2"]["units"]["hall_form"][
                    "completion_receipt"
                ]["receipt_digest"],
                script_path="build/units/2/hall_form.py",
                script_sha256=hashlib.sha256(payer_script.read_bytes()).hexdigest(),
            ),
        ),
        predecessor_inputs=(
            LayerFinalizationPredecessorInput.mint(
                layer_id="1",
                finalization_receipt_digest=predecessor_digest,
                script_path=layers["1"].script,
                script_sha256=hashlib.sha256(scripts[0].read_bytes()).hexdigest(),
            ),
        ),
        claimed_at="2026-09-01T10:00:00+00:00",
    )
    return tuple(scripts), unit_digests, selected, claim


def test_replay_prefix_receipt_is_ordered_hash_pinned_and_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts, unit_digests, selected, claim = _receipt_fixture(tmp_path, monkeypatch)
    replay_inputs = tuple(
        executed_replay_input(
            tmp_path,
            script.relative_to(tmp_path).as_posix(),
        )
        for script in scripts
    )

    receipt = judgment_debt_state.replay_prefix_receipt(
        tmp_path,
        replayed_layer_scripts=scripts,
        replay_inputs=replay_inputs,
        selected_authority=selected,
        finalization_claim=claim,
    )

    assert [layer.layer_id for layer in receipt.layers] == ["1", "2"]
    assert receipt.unit_digests == tuple(unit_digests.items())
    payload = receipt.as_dict()
    assert payload["schema"] == REPLAY_PREFIX_RECEIPT_SCHEMA
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
        "completion_receipt_digest": hashlib.sha256(
            b"completion:2:hall_form"
        ).hexdigest(),
    }
    assert judgment_debt_state.replay_prefix_receipt(
        tmp_path,
        replayed_layer_scripts=scripts,
        replay_inputs=replay_inputs,
        selected_authority=selected,
        finalization_claim=claim,
    ).digest == receipt.digest

    scripts[1].write_text("# changed layer receipt artifact\n", encoding="utf-8")
    changed_replay_inputs = (
        replay_inputs[0],
        executed_replay_input(
            tmp_path,
            scripts[1].relative_to(tmp_path).as_posix(),
        ),
    )
    with pytest.raises(ValueError, match="active finalization claim"):
        judgment_debt_state.replay_prefix_receipt(
            tmp_path,
            replayed_layer_scripts=scripts,
            replay_inputs=changed_replay_inputs,
            selected_authority=selected,
            finalization_claim=claim,
        )


def test_replay_prefix_receipt_rejects_checkpoint_script_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts, _unit_digests, selected, claim = _receipt_fixture(tmp_path, monkeypatch)
    replay_inputs = tuple(
        executed_replay_input(
            tmp_path,
            script.relative_to(tmp_path).as_posix(),
        )
        for script in scripts
    )
    (tmp_path / "build" / "units" / "2" / "hall_form.py").write_text(
        "# changed after checkpoint\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="artifact changed after its checkpoint"):
        judgment_debt_state.replay_prefix_receipt(
            tmp_path,
            replayed_layer_scripts=scripts,
            replay_inputs=replay_inputs,
            selected_authority=selected,
            finalization_claim=claim,
        )


def test_replay_prefix_receipt_rejects_swap_then_restore_of_executed_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts, _unit_digests, selected, claim = _receipt_fixture(tmp_path, monkeypatch)
    replay_inputs = tuple(
        executed_replay_input(
            tmp_path,
            script.relative_to(tmp_path).as_posix(),
        )
        for script in scripts
    )
    original = scripts[1].with_suffix(".original")
    scripts[1].rename(original)
    scripts[1].write_bytes(original.read_bytes())

    try:
        with pytest.raises(ValueError, match="trusted path changed"):
            judgment_debt_state.replay_prefix_receipt(
                tmp_path,
                replayed_layer_scripts=scripts,
                replay_inputs=replay_inputs,
                selected_authority=selected,
                finalization_claim=claim,
            )
    finally:
        scripts[1].unlink()
        original.rename(scripts[1])


@pytest.mark.parametrize(
    ("indices", "include_extra"),
    [
        ((1,), False),
        ((1, 0), False),
        ((0, 1), True),
    ],
    ids=("omitted-predecessor", "reordered-prefix", "extra-layer"),
)
def test_replay_prefix_mint_rejects_nonexact_selected_layer_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    indices: tuple[int, ...],
    include_extra: bool,
) -> None:
    scripts, _unit_digests, selected, claim = _receipt_fixture(
        tmp_path,
        monkeypatch,
    )
    replay_inputs = tuple(
        executed_replay_input(
            tmp_path,
            scripts[index].relative_to(tmp_path).as_posix(),
        )
        for index in indices
    )
    replayed_scripts = tuple(scripts[index] for index in indices)
    if include_extra:
        extra = tmp_path / "build" / "extra.py"
        extra.write_text("# extra unselected layer\n", encoding="utf-8")
        replayed_scripts += (extra,)
        replay_inputs += (
            executed_replay_input(
                tmp_path,
                extra.relative_to(tmp_path).as_posix(),
            ),
        )

    with pytest.raises(
        ValueError,
        match=r"exact stable selected-DAG prefix|exact stable selected-DAG",
    ):
        judgment_debt_state.replay_prefix_receipt(
            tmp_path,
            replayed_layer_scripts=replayed_scripts,
            replay_inputs=replay_inputs,
            selected_authority=selected,
            finalization_claim=claim,
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


def _payment_fixture(
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    *,
    label: str = "payer",
) -> tuple[
    judgment_debt_state.ReplayPrefixReceipt,
    JudgmentDebtPaymentGeneration,
]:
    identity, unit_digest_value = activation.payer_unit_digests[0]
    layer_id, unit_id = identity.split(":", 1)
    script_digest = hashlib.sha256(f"script:{label}".encode()).hexdigest()
    replay = judgment_debt_state.ReplayPrefixReceipt(
        (
            judgment_debt_state.ReplayPrefixLayerReceipt(
                layer_id=layer_id,
                layer_generation_digest=hashlib.sha256(
                    f"layer-generation:{label}".encode()
                ).hexdigest(),
                predecessor_layer_digests=(),
                script_path=f"build/{layer_id}.py",
                script_sha256=script_digest,
                dependencies=(),
                units=(
                    judgment_debt_state.ReplayPrefixUnitReceipt(
                        layer_id=layer_id,
                        unit_id=unit_id,
                        unit_digest=unit_digest_value,
                        checkpoint_unit_digest=unit_digest_value,
                        script_path=f"build/units/{layer_id}/{unit_id}.py",
                        script_sha256=script_digest,
                        checkpoint_script_sha256=script_digest,
                        completion_receipt_digest=hashlib.sha256(
                            f"completion:{label}".encode()
                        ).hexdigest(),
                    ),
                ),
                payer_claim_id=f"lfc-{hashlib.sha256(f'claim:{label}'.encode()).hexdigest()}",
            ),
        )
    )
    return replay, judgment_debt_state.payment_generation_for_replay(
        definition,
        activation,
        replay,
    )


def _patch_authority(
    monkeypatch: pytest.MonkeyPatch,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
) -> None:
    monkeypatch.setattr(
        judgment_debt_state,
        "_current_authority",
        lambda _shot, _selected: (BUNDLE_DIGEST, (definition,), (activation,)),
    )
    monkeypatch.setattr(
        judgment_debt_state,
        "payment_generation_is_current",
        lambda *_args, **_kwargs: True,
    )


def _append_raw_event(
    shot: Path,
    *,
    bundle_digest: str,
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    state: JudgmentDebtState,
    predecessor: JudgmentDebtState | None = None,
    payment_label: str = "payer",
) -> None:
    path = shot / "state" / judgment_debt_state.EVENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    replay, generation = _payment_fixture(
        definition,
        activation,
        label=payment_label,
    )
    if state.payment_generation_digest != generation.digest:
        state = JudgmentDebtState(
            definition_digest=state.definition_digest,
            status=state.status,
            evidence_digest=state.evidence_digest,
            activation_digest=state.activation_digest,
            payment_generation_digest=generation.digest,
        )
    row = {
        "schema": judgment_debt_state.EVENT_SCHEMA,
        "bundle_digest": bundle_digest,
        "debt_id": definition.debt_id,
        "definition_digest": definition.digest,
        "predecessor_state_digest": (predecessor or JudgmentDebtState.pending(definition)).digest,
        "state": state.as_dict(),
        "payment_generation": generation.as_dict(),
        "replay_prefix": replay.as_dict(),
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


def test_current_state_wrapper_resolves_one_authority_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    selected = object()
    calls: list[Path] = []

    def resolve_once(shot: Path) -> object:
        calls.append(Path(shot))
        return selected

    monkeypatch.setattr(judgment_debt_state, "resolve_selected_authority", resolve_once)
    monkeypatch.setattr(
        judgment_debt_state,
        "_current_authority",
        lambda _shot, authority: (
            (BUNDLE_DIGEST, (definition,), (activation,))
            if authority is selected
            else pytest.fail("judgment debt state used another authority snapshot")
        ),
    )

    judgment_debt_state.current_judgment_debt_states(tmp_path)

    assert calls == [tmp_path]


def test_exact_activation_transitions_due_then_satisfied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _definition_and_activation()
    _patch_authority(monkeypatch, definition, activation)
    replay, _generation = _payment_fixture(definition, activation)

    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replay_receipt=replay,
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
    replay, _generation = _payment_fixture(definition, activation)
    judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replay_receipt=replay,
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
    replay, generation = _payment_fixture(definition, activation)
    skipped = JudgmentDebtState(
        definition.digest,
        "satisfied",
        EVIDENCE_DIGEST,
        activation.digest,
        generation.digest,
    )
    _append_raw_event(
        tmp_path,
        bundle_digest=BUNDLE_DIGEST,
        definition=definition,
        activation=activation,
        state=skipped,
    )

    with pytest.raises(ValueError, match="skips or rewrites"):
        judgment_debt_state.current_judgment_debt_states(tmp_path)

    due = JudgmentDebtState(
        definition.digest,
        "due",
        activation_digest=activation.digest,
        payment_generation_digest=generation.digest,
    )
    satisfied = JudgmentDebtState(
        definition.digest,
        "satisfied",
        EVIDENCE_DIGEST,
        activation.digest,
        generation.digest,
    )
    falsified = JudgmentDebtState(
        definition.digest,
        "falsified",
        EVIDENCE_DIGEST,
        activation.digest,
        generation.digest,
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
                        "payment_generation": generation.as_dict(),
                        "replay_prefix": replay.as_dict(),
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
        activation=activation,
        state=JudgmentDebtState(
            definition.digest,
            "due",
            activation_digest=activation.digest,
            payment_generation_digest=_payment_fixture(
                definition,
                activation,
            )[1].digest,
        ),
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
    _old_replay, old_generation = _payment_fixture(definition, old_activation)
    _append_raw_event(
        tmp_path,
        bundle_digest=BUNDLE_DIGEST,
        definition=definition,
        activation=old_activation,
        state=JudgmentDebtState(
            definition.digest,
            "due",
            activation_digest=old_activation.digest,
            payment_generation_digest=old_generation.digest,
        ),
    )
    new_activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("form:hall", hashlib.sha256(b"replacement unit").hexdigest()),),
    )
    _patch_authority(monkeypatch, definition, new_activation)

    rows = judgment_debt_state.current_judgment_debt_states(tmp_path)

    assert rows[0][2].status == "pending_not_due"
    replay, _generation = _payment_fixture(
        definition,
        new_activation,
        label="replacement",
    )
    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="form",
        replay_receipt=replay,
    )
    assert due.activation_digest == new_activation.digest
