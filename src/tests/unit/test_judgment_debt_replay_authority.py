"""Exact selected-prefix authority for qualitative debt payment."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
)
from vfx_harness.domain.stop_transaction_state import (
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
)
from vfx_harness.domain.unit_evaluation_receipts import (
    ReplayDependencyBinding,
    ReplayInputBinding,
)
from vfx_harness.orchestration import judgment_debt_replay_authority
from vfx_harness.orchestration.authority_selection import (
    AuthorityPointerObservation,
    ResolvedSelectedAuthority,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _selected(tmp_path: Path) -> ResolvedSelectedAuthority:
    pointer = _digest("prefix pointer")
    bundle = _digest("prefix bundle")
    return ResolvedSelectedAuthority(
        assertion=SelectedAuthorityAssertionV2(
            "selected",
            SelectedAuthorityBundle(bundle, "clean", pointer),
            SelectedAuthorityView("jit", _digest("prefix view"), pointer),
        ),
        pointer_observation=AuthorityPointerObservation(pointer, pointer),
        selection_token=AuthoritySelectionToken(1, pointer, 3, pointer),
        plan=SimpleNamespace(bundle=SimpleNamespace(content_hash=bundle)),
        artifact_paths={"layers.json": tmp_path / "layers.json"},
    )


def _capsule(layer_id: str, predecessors: tuple[tuple[str, str], ...]):
    return SimpleNamespace(
        layer_id=layer_id,
        capsule_digest=_digest(f"capsule:{layer_id}"),
        predecessor_layer_digests=predecessors,
        projection={
            "sparse_layer": {
                "id": layer_id,
                "jit": {
                    "depends_on_layers": [row[0] for row in predecessors],
                },
            }
        },
    )


def _layer(layer_id: str):
    return SimpleNamespace(
        id=layer_id,
        script=f"build/{layer_id}.py",
        execution="ready",
        stages=(),
    )


def test_selected_prefix_uses_capsule_compiled_dag_order_not_effective_map_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    independent = _capsule("independent", ())
    camera = _capsule("camera", ())
    form = _capsule("form", (("camera", camera.capsule_digest),))
    source_checks: list[str] = []
    captured = SimpleNamespace(
        capsule_set=SimpleNamespace(layers=(independent, camera, form)),
        require_sources_unchanged=lambda: source_checks.append("checked"),
    )
    monkeypatch.setattr(
        judgment_debt_replay_authority.authority_capsule_resolution,
        "capture_selected_authority_capsules",
        lambda *_args: captured,
    )
    # Effective materialization order is deliberately not replay authority.
    monkeypatch.setattr(
        judgment_debt_replay_authority,
        "load_layers_from_path",
        lambda _path: {
            "form": _layer("form"),
            "independent": _layer("independent"),
            "camera": _layer("camera"),
        },
    )

    prefix = (
        judgment_debt_replay_authority.capture_selected_judgment_replay_prefix(
            tmp_path,
            _selected(tmp_path),
            "form",
        )
    )

    assert tuple(row.layer.id for row in prefix.layers) == (
        "independent",
        "camera",
        "form",
    )
    assert source_checks == ["checked"]


def test_terminal_match_uses_dependency_order_not_authored_stage_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = SimpleNamespace(
        id="producer",
        digest=_digest("producer unit"),
        depends_on=(),
        mutates=SimpleNamespace(script_spans=("build/units/form/producer.py",)),
    )
    consumer = SimpleNamespace(
        id="consumer",
        digest=_digest("consumer unit"),
        depends_on=("producer",),
        mutates=SimpleNamespace(script_spans=("build/units/form/consumer.py",)),
    )
    layer = SimpleNamespace(
        id="form",
        script="build/form.py",
        # Deliberately authored dependency-last; this is not execution order.
        stages=(consumer, producer),
    )
    capsule = _capsule("form", ())
    terminal = SimpleNamespace(
        claim=SimpleNamespace(
            layer_id="form",
            plan_hash=capsule.capsule_digest,
            predecessor_inputs=(),
            unit_inputs=tuple(
                SimpleNamespace(
                    unit_id=unit.id,
                    unit_digest=unit.digest,
                    script_path=unit.mutates.script_spans[0],
                )
                for unit in (producer, consumer)
            ),
        ),
        layer_script_path=layer.script,
        final_status="passed",
    )
    monkeypatch.setattr(
        judgment_debt_replay_authority.unit_state,
        "unit_digest",
        lambda unit: unit.digest,
    )

    judgment_debt_replay_authority._require_terminal_matches_selected_layer(
        terminal,
        SimpleNamespace(layer=layer, capsule=capsule),
        (),
        require_passed=True,
    )


def _unit(layer_id: str) -> ReplayPrefixUnitReceipt:
    digest = _digest(f"unit:{layer_id}")
    return ReplayPrefixUnitReceipt(
        layer_id=layer_id,
        unit_id=f"{layer_id}-unit",
        unit_digest=digest,
        checkpoint_unit_digest=digest,
        script_path=f"build/units/{layer_id}/{layer_id}-unit.py",
        script_sha256=_digest(f"unit-script:{layer_id}"),
        checkpoint_script_sha256=_digest(f"unit-script:{layer_id}"),
        completion_receipt_digest=_digest(f"completion:{layer_id}"),
    )


def _replay_layer(
    layer_id: str,
    *,
    predecessors: tuple[tuple[str, str], ...] = (),
    payer: bool,
) -> ReplayPrefixLayerReceipt:
    return ReplayPrefixLayerReceipt(
        layer_id=layer_id,
        layer_generation_digest=_digest(f"capsule:{layer_id}"),
        predecessor_layer_digests=predecessors,
        script_path=f"build/{layer_id}.py",
        script_sha256=_digest(f"layer-script:{layer_id}"),
        dependencies=(),
        units=(_unit(layer_id),),
        finalization_receipt_digest=(
            None if payer else _digest(f"terminal:{layer_id}")
        ),
        payer_claim_id=(f"lfc-{_digest('payer-claim')}" if payer else None),
    )


def _selected_prefix(*, changed_independent: bool = False):
    independent = _capsule("independent", ())
    if changed_independent:
        independent.capsule_digest = _digest("changed independent capsule")
    camera = _capsule("camera", ())
    form = _capsule("form", (("camera", camera.capsule_digest),))
    return SimpleNamespace(
        layers=tuple(
            SimpleNamespace(layer=_layer(row.layer_id), capsule=row)
            for row in (independent, camera, form)
        ),
        require_sources_unchanged=lambda: None,
    )


@pytest.mark.parametrize(
    "layers",
    [
        (_replay_layer("form", predecessors=(("camera", _digest("capsule:camera")),), payer=True),),
        (
            _replay_layer("camera", payer=False),
            _replay_layer("independent", payer=False),
            _replay_layer("form", predecessors=(("camera", _digest("capsule:camera")),), payer=True),
        ),
        (
            _replay_layer("independent", payer=False),
            _replay_layer("camera", payer=False),
            _replay_layer("extra", payer=False),
            _replay_layer("form", predecessors=(("camera", _digest("capsule:camera")),), payer=True),
        ),
    ],
    ids=("omitted", "reordered", "extra"),
)
def test_durable_payment_rejects_nonexact_selected_prefix(
    tmp_path: Path,
    layers: tuple[ReplayPrefixLayerReceipt, ...],
) -> None:
    with pytest.raises(ValueError, match="exact stable selected-layer prefix"):
        judgment_debt_replay_authority.require_payment_replay_prefix_current(
            tmp_path,
            object(),  # rejected before any selected-state collaborator is consulted
            _selected_prefix(),
            ReplayPrefixReceipt(layers),
        )


def test_durable_payment_invalidates_when_earlier_independent_capsule_changes(
    tmp_path: Path,
) -> None:
    camera_digest = _digest("capsule:camera")
    receipt = ReplayPrefixReceipt(
        (
            _replay_layer("independent", payer=False),
            _replay_layer("camera", payer=False),
            _replay_layer(
                "form",
                predecessors=(("camera", camera_digest),),
                payer=True,
            ),
        )
    )

    with pytest.raises(ValueError, match="exact selected capsule"):
        judgment_debt_replay_authority.require_payment_replay_prefix_current(
            tmp_path,
            object(),
            _selected_prefix(changed_independent=True),
            receipt,
        )


def test_durable_payment_compares_units_and_executed_dependencies_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = ReplayPrefixReceipt((_replay_layer("form", payer=True),))
    replay_unit = replay.layers[0].units[0]
    claim = SimpleNamespace(
        claim_id=replay.layers[0].payer_claim_id,
        unit_inputs=(
            SimpleNamespace(
                unit_id=replay_unit.unit_id,
                unit_digest=replay_unit.unit_digest,
                completion_receipt_digest=replay_unit.completion_receipt_digest,
                script_path=replay_unit.script_path,
                script_sha256=replay_unit.script_sha256,
            ),
        ),
    )
    dependencies = (
        ReplayDependencyBinding.mint(
            kind="construction_pointer",
            path="build/construction/fixture.json",
            sha256=_digest("construction pointer"),
        ),
        ReplayDependencyBinding.mint(
            kind="construction_glb",
            path="build/construction/fixture.glb",
            sha256=_digest("construction bytes"),
        ),
    )
    replay_group = SimpleNamespace(
        receipt_digest=_digest("replay receipt"),
        replay_inputs=(
            ReplayInputBinding.mint(
                script_path=replay.layers[0].script_path,
                script_sha256=replay.layers[0].script_sha256,
                dependencies=dependencies,
            ),
        ),
        observation=SimpleNamespace(replay_prefix=replay),
    )
    terminal = SimpleNamespace(
        claim=claim,
        evaluation_receipt=SimpleNamespace(
            replay_receipts=(
                SimpleNamespace(
                    locator="runs/fixture/replay-group-0.json",
                    sha256=_digest("replay file"),
                    receipt=replay_group,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        judgment_debt_replay_authority,
        "_current_authorized_terminal",
        lambda *_args, **_kwargs: terminal,
    )
    stored = SimpleNamespace(
        receipt=replay_group,
    )
    monkeypatch.setattr(
        judgment_debt_replay_authority.layer_replay_receipts,
        "load_layer_replay_receipt",
        lambda *_args, **_kwargs: stored,
    )

    with pytest.raises(ValueError, match="exact executed layer scripts and dependency"):
        judgment_debt_replay_authority.require_payment_replay_prefix_current(
            tmp_path,
            object(),
            SimpleNamespace(
                layers=(
                    SimpleNamespace(
                        layer=_layer("form"),
                        capsule=_capsule("form", ()),
                    ),
                ),
                require_sources_unchanged=lambda: None,
            ),
            replay,
        )


def test_durable_payment_rejects_unit_script_not_bound_by_terminal_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = ReplayPrefixReceipt((_replay_layer("form", payer=True),))
    replay_unit = replay.layers[0].units[0]
    terminal = SimpleNamespace(
        claim=SimpleNamespace(
            claim_id=replay.layers[0].payer_claim_id,
            unit_inputs=(
                SimpleNamespace(
                    unit_id=replay_unit.unit_id,
                    unit_digest=replay_unit.unit_digest,
                    completion_receipt_digest=replay_unit.completion_receipt_digest,
                    script_path=replay_unit.script_path,
                    script_sha256=_digest("different terminal unit script"),
                ),
            ),
        )
    )
    monkeypatch.setattr(
        judgment_debt_replay_authority,
        "_current_authorized_terminal",
        lambda *_args, **_kwargs: terminal,
    )

    with pytest.raises(ValueError, match="dependency-ordered terminal claim inputs"):
        judgment_debt_replay_authority.require_payment_replay_prefix_current(
            tmp_path,
            object(),
            SimpleNamespace(
                layers=(
                    SimpleNamespace(
                        layer=_layer("form"),
                        capsule=_capsule("form", ()),
                    ),
                ),
                require_sources_unchanged=lambda: None,
            ),
            replay,
        )
