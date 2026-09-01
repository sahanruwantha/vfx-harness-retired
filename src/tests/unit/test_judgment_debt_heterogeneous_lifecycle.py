"""Pure heterogeneous qualitative-debt contract and state fixtures."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents import acceptance
from vfx_harness.blender.observation_environment import (
    SCHEMA as OBSERVATION_ENVIRONMENT_SCHEMA,
)
from vfx_harness.blender.observation_environment import (
    canonical_observation_environment,
    validate_observation_request,
)
from vfx_harness.domain.judgment_debt_replay_receipts import (
    ReplayPrefixLayerReceipt,
    ReplayPrefixReceipt,
    ReplayPrefixUnitReceipt,
    payment_generation_for_replay,
    replay_parent_chain_digest,
)
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
    JudgmentDebtSeed,
    JudgmentObservationRequest,
    JudgmentPoint,
    JudgmentProvider,
    compile_judgment_debt,
)
from vfx_harness.orchestration import judgment_debt_state


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


BUNDLE_DIGEST = _digest("heterogeneous-lifecycle-bundle")
CARRIER_UNIT_DIGEST = _digest("accepted-carrier-unit")
OWNER_UNIT_DIGEST = _digest("active-owner-unit")


def _authority(
    *,
    carrier_family: str,
    subject_role: str,
    provider_role: str,
    observation_medium: str,
    lifecycle: str = "persistent",
) -> tuple[JudgmentDebtDefinition, JudgmentDebtActivation]:
    definition = compile_judgment_debt(
        JudgmentDebtSeed(
            requirement_id=f"R-{carrier_family}-composition",
            statement=f"The {subject_role} reads as composed through its typed carrier.",
            decision_strength="approved_start",
            claim_kind="atomic",
            property="camera_framing",
            owner_layer="owner",
            fault_owner="camera",
            subject_roles=(subject_role,),
            axes=("camera_alignment",),
            judge_points=(JudgmentPoint(frame=1, ref="refs/reference.png"),),
            observation_medium=observation_medium,
            lifecycle=lifecycle,
            bundle_digest=BUNDLE_DIGEST,
            carrier_families=(carrier_family,),
        ),
        (
            JudgmentProvider(
                id=f"accepted-{carrier_family}-carrier",
                layer_id="carrier",
                carrier_family=carrier_family,
                subject_roles=(provider_role,),
            ),
        ),
        layer_dependencies={"carrier": (), "owner": ("carrier",)},
        layer_order=("carrier", "owner"),
    )
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("carrier:subject", CARRIER_UNIT_DIGEST),),
    )
    return definition, activation


def _replay_prefix() -> ReplayPrefixReceipt:
    carrier_generation = _digest("carrier-layer-generation")
    return ReplayPrefixReceipt(
        (
            ReplayPrefixLayerReceipt(
                layer_id="carrier",
                layer_generation_digest=carrier_generation,
                predecessor_layer_digests=(),
                script_path="build/carrier.py",
                script_sha256=_digest("carrier-layer-script"),
                dependencies=(),
                units=(
                    ReplayPrefixUnitReceipt(
                        layer_id="carrier",
                        unit_id="subject",
                        unit_digest=CARRIER_UNIT_DIGEST,
                        checkpoint_unit_digest=CARRIER_UNIT_DIGEST,
                        script_path="build/units/carrier/subject.py",
                        script_sha256=_digest("carrier-unit-script"),
                        checkpoint_script_sha256=_digest("carrier-unit-script"),
                        completion_receipt_digest=_digest("carrier-completion"),
                    ),
                ),
                finalization_receipt_digest=_digest("carrier-finalization"),
            ),
            ReplayPrefixLayerReceipt(
                layer_id="owner",
                layer_generation_digest=_digest("owner-layer-generation"),
                predecessor_layer_digests=(("carrier", carrier_generation),),
                script_path="build/owner.py",
                script_sha256=_digest("owner-layer-script"),
                dependencies=(),
                units=(
                    ReplayPrefixUnitReceipt(
                        layer_id="owner",
                        unit_id="camera",
                        unit_digest=OWNER_UNIT_DIGEST,
                        checkpoint_unit_digest=OWNER_UNIT_DIGEST,
                        script_path="build/units/owner/camera.py",
                        script_sha256=_digest("owner-unit-script"),
                        checkpoint_script_sha256=_digest("owner-unit-script"),
                        completion_receipt_digest=_digest("owner-completion"),
                    ),
                ),
                payer_claim_id=f"lfc-{_digest('owner-finalization-claim')}",
            ),
        )
    )


def _patch_current_authority(
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


def _eevee_request(
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    replay: ReplayPrefixReceipt,
) -> JudgmentObservationRequest:
    frame, roles, medium, families = validate_observation_request(
        1,
        definition.seed.subject_roles,
        definition.seed.observation_medium,
        definition.seed.carrier_families,
    )
    environment = canonical_observation_environment(
        {
            "schema": OBSERVATION_ENVIRONMENT_SCHEMA,
            "frame": frame,
            "observation_medium": medium,
            "subject_roles": list(roles),
            "carrier_families": list(families),
            "render": {"engine": "BLENDER_EEVEE"},
        }
    )
    generation = payment_generation_for_replay(definition, activation, replay)
    request = JudgmentObservationRequest(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        payment_generation_digest=generation.digest,
        bundle_digest=BUNDLE_DIGEST,
        owner_view_digest=_digest("owner-view"),
        payer_view_digest=_digest("payer-view"),
        replay_receipt_digest=replay.digest,
        layer_replay_receipt_digest=_digest("layer-replay-receipt"),
        parent_chain_digest=replay_parent_chain_digest(replay),
        judge_point=definition.seed.judge_points[0],
        observation_medium=medium,
        render_mode="eevee",
        render_scale=0.5,
        reference_digest=_digest("reference-bytes"),
        reference_marker=None,
        observation_environment_digest=environment["digest"],
        external_asset_provenance_digest=_digest("external-assets"),
        comparison_config_digest=_digest("comparison-config"),
        judge_config_digest=_digest("judge-config"),
    )
    request.assert_matches(definition, activation)
    return request


@pytest.mark.parametrize(
    ("carrier_family", "subject_role", "provider_role"),
    [
        ("volume", "world.atmosphere", "world.atmosphere.fog"),
        ("compositor", "render.output", "render.output.compositor"),
    ],
)
def test_typed_non_mesh_carrier_compiles_eevee_request_and_state_transitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    carrier_family: str,
    subject_role: str,
    provider_role: str,
) -> None:
    definition, activation = _authority(
        carrier_family=carrier_family,
        subject_role=subject_role,
        provider_role=provider_role,
        observation_medium="eevee",
    )
    _patch_current_authority(monkeypatch, definition, activation)
    replay = _replay_prefix()

    [(current_definition, current_activation, pending)] = (
        judgment_debt_state.current_judgment_debt_states(tmp_path)
    )
    assert current_definition == definition
    assert current_activation == activation
    assert definition.binding.activates_at == "owner"
    assert {provider.carrier_family for provider in definition.providers} == {
        carrier_family
    }
    assert pending.status == "pending_not_due"

    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="owner",
        replay_receipt=replay,
    )
    request = _eevee_request(definition, activation, replay)

    assert due.status == "due"
    assert request.observation_medium == "eevee"
    assert request.render_mode == "eevee"
    with pytest.raises(ValueError, match=r"eevee.*requires 'eevee'"):
        replace(request, render_mode="solid")

    satisfied = judgment_debt_state.resolve_current_judgment_debt(
        tmp_path,
        definition.digest,
        outcome="satisfied",
        evidence_digest=request.digest,
    )

    assert satisfied.status == "satisfied"
    assert satisfied.evidence_digest == request.digest
    judgment_debt_state.require_judgment_debts_satisfied(tmp_path)


@pytest.mark.parametrize("carrier_family", ["volume", "compositor"])
def test_non_mesh_carrier_refuses_workbench_observation(
    carrier_family: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="workbench_solid observation requires exactly the mesh carrier family",
    ):
        _authority(
            carrier_family=carrier_family,
            subject_role="render.subject",
            provider_role="render.subject.carrier",
            observation_medium="workbench_solid",
        )


def test_direct_acceptance_refuses_unresolved_window_debt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition, activation = _authority(
        carrier_family="mesh",
        subject_role="hero",
        provider_role="hero.mass",
        observation_medium="workbench_solid",
        lifecycle="window",
    )
    _patch_current_authority(monkeypatch, definition, activation)
    selected_authority = object()
    monkeypatch.setattr(
        acceptance,
        "resolve_selected_authority",
        lambda _folder: selected_authority,
    )
    monkeypatch.setattr(
        acceptance.plan_due,
        "require_due_clear",
        lambda *_args, **_kwargs: None,
    )

    def assert_acceptance_refuses(status: str) -> None:
        async def run() -> None:
            with pytest.raises(
                acceptance.IncompleteChain,
                match=rf"{definition.debt_id}={status}",
            ):
                await acceptance.accept(
                    SimpleNamespace(folder=tmp_path),
                    session=None,
                    verbose=False,
                )

        anyio.run(run)

    assert definition.seed.lifecycle == "window"
    assert_acceptance_refuses("pending_not_due")

    due = judgment_debt_state.mark_judgment_debt_due(
        tmp_path,
        definition.digest,
        layer_id="owner",
        replay_receipt=_replay_prefix(),
        selected_authority=selected_authority,
    )

    assert due.status == "due"
    assert_acceptance_refuses("due")
