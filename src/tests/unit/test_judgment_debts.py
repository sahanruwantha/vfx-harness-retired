"""Deferred qualitative judgment activates on its relevant rendered subject."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtCompletionBinding,
    JudgmentDebtDefinition,
    JudgmentDebtPaymentGeneration,
    JudgmentDebtSeed,
    JudgmentDebtState,
    JudgmentObservationRequest,
    JudgmentPaymentAttemptFailure,
    JudgmentPoint,
    JudgmentProvider,
    JudgmentProviderBinding,
    ProviderActivation,
    activate_judgment_debt,
    compile_judgment_debt,
    compile_provider_activation,
    resolve_judgment_debt,
    validate_judgment_debt_replay_prefix,
)

BUNDLE_DIGEST = hashlib.sha256(b"selected plan bundle").hexdigest()
PAYMENT_GENERATION_DIGEST = hashlib.sha256(b"payment generation").hexdigest()


def _seed(
    *,
    subject_roles: tuple[str, ...] = ("hall",),
    carrier_families: tuple[str, ...] = ("mesh",),
    observation_medium: str = "workbench_solid",
    claim_kind: str = "atomic",
    property: str = "reference_identity",
) -> JudgmentDebtSeed:
    return JudgmentDebtSeed(
        requirement_id="R-hall-read",
        statement="The chamber reads as the authored reference.",
        decision_strength="approved_start",
        claim_kind=claim_kind,
        property=property,
        owner_layer="1",
        fault_owner="camera-framing",
        subject_roles=subject_roles,
        axes=("reference_match",),
        judge_points=(
            JudgmentPoint(frame=1, ref="refs/hall-start.png"),
            JudgmentPoint(frame=40, ref="refs/hall-end.png"),
        ),
        observation_medium=observation_medium,
        lifecycle="persistent",
        bundle_digest=BUNDLE_DIGEST,
        carrier_families=carrier_families,
    )


def _provider(
    identifier: str,
    layer_id: str,
    role: str,
    family: str = "mesh",
) -> JudgmentProvider:
    return JudgmentProvider(
        id=identifier,
        layer_id=layer_id,
        carrier_family=family,
        subject_roles=(role,),
    )


def _activation(definition: JudgmentDebtDefinition) -> JudgmentDebtActivation:
    return JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("payer-unit", hashlib.sha256(b"payer-unit").hexdigest()),),
    )


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _observation_request(
    definition: JudgmentDebtDefinition,
    activation: JudgmentDebtActivation,
    *,
    reference_digest: str | None = None,
    reference_marker: str | None = None,
) -> JudgmentObservationRequest:
    return JudgmentObservationRequest(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        payment_generation_digest=_digest("payment generation"),
        bundle_digest=definition.seed.bundle_digest,
        owner_view_digest=_digest("owner view"),
        payer_view_digest=_digest("payer view"),
        replay_receipt_digest=_digest("detailed replay receipt"),
        layer_replay_receipt_digest=_digest("layer replay receipt"),
        parent_chain_digest=_digest("parent chain"),
        judge_point=definition.seed.judge_points[0],
        observation_medium=definition.seed.observation_medium,
        render_mode="solid",
        render_scale=0.5,
        reference_digest=reference_digest,
        reference_marker=reference_marker,
        observation_environment_digest=_digest("observation environment"),
        external_asset_provenance_digest=_digest("asset provenance"),
        comparison_config_digest=_digest("comparison config"),
        judge_config_digest=_digest("judge config"),
    )


def test_camera_debt_activates_at_matching_descendant_mesh() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )

    assert definition.binding.activates_at == "2"
    assert definition.binding.provider_ids == ("hall-mass",)
    assert definition.binding.subject_provider_ids == (("hall", ("hall-mass",)),)

    pending = JudgmentDebtState.pending(definition)
    with pytest.raises(ValueError, match="activates_at='2', not layer '1'"):
        activate_judgment_debt(
            definition,
            pending,
            activation=_activation(definition),
            payment_generation_digest=PAYMENT_GENERATION_DIGEST,
            layer_id="1",
            replayed_unit_digests=_activation(definition).payer_unit_digests,
        )
    due = activate_judgment_debt(
        definition,
        pending,
        activation=_activation(definition),
        payment_generation_digest=PAYMENT_GENERATION_DIGEST,
        layer_id="2",
        replayed_unit_digests=_activation(definition).payer_unit_digests,
    )
    evidence = hashlib.sha256(b"qualified composed judgment").hexdigest()
    satisfied = resolve_judgment_debt(
        definition,
        due,
        outcome="satisfied",
        evidence_digest=evidence,
    )

    assert [pending.status, due.status, satisfied.status] == [
        "pending_not_due",
        "due",
        "satisfied",
    ]
    assert satisfied.evidence_digest == evidence


def test_unrelated_earlier_mesh_cannot_activate_matching_later_subject() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [
            _provider("table-prop", "2", "props.table"),
            _provider("hall-shell", "3", "hall.shell"),
        ],
        layer_dependencies={"1": (), "2": ("1",), "3": ("2",)},
        layer_order=("1", "2", "3"),
    )

    assert definition.binding.activates_at == "3"
    assert definition.binding.provider_ids == ("hall-shell",)
    assert all(provider.id != "table-prop" for provider in definition.providers)


def test_later_matching_promise_does_not_delay_earliest_covered_prefix() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [
            _provider("first-hall", "2", "hall.mass"),
            _provider("later-hall", "3", "hall.detail"),
        ],
        layer_dependencies={"1": (), "2": ("1",), "3": ("2",)},
        layer_order=("1", "2", "3"),
    )

    assert definition.binding.activates_at == "2"
    assert definition.binding.provider_ids == ("first-hall",)


def test_matching_ancestor_provider_activates_at_owner() -> None:
    seed = JudgmentDebtSeed(
        requirement_id="R-hall-read",
        statement="The chamber reads as the authored reference.",
        decision_strength="approved_start",
        claim_kind="atomic",
        property="reference_identity",
        owner_layer="camera",
        fault_owner="camera-framing",
        subject_roles=("hall",),
        axes=("reference_match",),
        judge_points=(JudgmentPoint(frame=1, ref="refs/hall.png"),),
        observation_medium="workbench_solid",
        lifecycle="layer",
        bundle_digest=BUNDLE_DIGEST,
        carrier_families=("mesh",),
    )
    definition = compile_judgment_debt(
        seed,
        [_provider("existing-hall", "form", "hall.mass")],
        layer_dependencies={"form": (), "camera": ("form",)},
        layer_order=("form", "camera"),
    )

    assert definition.binding.activates_at == "camera"
    assert definition.binding.provider_ids == ("existing-hall",)


def test_no_matching_reachable_provider_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="outside the owner's dependency predecessor/successor prefixes",
    ):
        compile_judgment_debt(
            _seed(),
            [
                _provider("unrelated", "2", "props.table"),
                _provider("hall-island", "3", "hall.mass"),
            ],
            layer_dependencies={"1": (), "2": ("1",), "3": ()},
            layer_order=("1", "2", "3"),
        )


@pytest.mark.parametrize(
    ("family", "subject", "provider_role"),
    [
        ("volume", "world.atmosphere", "world.atmosphere.fog"),
        ("compositor", "render.output", "render.output.compositor"),
    ],
)
def test_volume_and_compositor_are_typed_rendered_carriers(
    family: str,
    subject: str,
    provider_role: str,
) -> None:
    definition = compile_judgment_debt(
        _seed(
            subject_roles=(subject,),
            carrier_families=(family,),
            observation_medium="eevee",
        ),
        [_provider(f"{family}-provider", "2", provider_role, family)],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )

    assert definition.binding.activates_at == "2"
    assert definition.providers[0].carrier_family == family


def test_prefix_without_dot_is_not_a_semantic_descendant() -> None:
    with pytest.raises(ValueError, match="no matching provider promise exists"):
        compile_judgment_debt(
            _seed(subject_roles=("hal",)),
            [_provider("hall-mass", "2", "hall.mass")],
            layer_dependencies={"1": (), "2": ("1",)},
            layer_order=("1", "2"),
        )


def test_multiple_relevant_providers_wait_for_dependency_complete_layer() -> None:
    definition = compile_judgment_debt(
        _seed(subject_roles=("hall.mass", "hall.roof")),
        [
            _provider("mass", "2", "hall.mass.main"),
            _provider("roof", "3", "hall.roof.cap"),
        ],
        layer_dependencies={
            "1": (),
            "2": ("1",),
            "3": ("1",),
            "4": ("2", "3"),
        },
        layer_order=("1", "2", "3", "4"),
    )

    assert definition.binding.activates_at == "4"
    assert definition.binding.provider_ids == ("mass", "roof")
    with pytest.raises(ValueError, match=r"missing=.*payer-unit"):
        activate_judgment_debt(
            definition,
            JudgmentDebtState.pending(definition),
            activation=_activation(definition),
            payment_generation_digest=PAYMENT_GENERATION_DIGEST,
            layer_id="4",
            replayed_unit_digests=(),
        )


def test_replay_prefix_requires_each_exact_payer_unit_digest() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    activation = _activation(definition)
    stale = (("payer-unit", hashlib.sha256(b"superseded payer").hexdigest()),)

    with pytest.raises(ValueError, match=r"missing=.*payer-unit"):
        validate_judgment_debt_replay_prefix(activation, ())
    with pytest.raises(ValueError, match=r"stale=.*payer-unit"):
        validate_judgment_debt_replay_prefix(activation, stale)

    due = activate_judgment_debt(
        definition,
        JudgmentDebtState.pending(definition),
        activation=activation,
        payment_generation_digest=PAYMENT_GENERATION_DIGEST,
        layer_id="2",
        replayed_unit_digests=(*activation.payer_unit_digests, ("upstream:camera", BUNDLE_DIGEST)),
    )

    assert due.status == "due"


def test_canonical_digests_ignore_set_like_authoring_order() -> None:
    left = _seed(subject_roles=("hall.mass", "hall.roof"))
    right = JudgmentDebtSeed(
        requirement_id=left.requirement_id,
        statement=left.statement,
        decision_strength=left.decision_strength,
        claim_kind=left.claim_kind,
        property=left.property,
        owner_layer=left.owner_layer,
        fault_owner=left.fault_owner,
        subject_roles=tuple(reversed(left.subject_roles)),
        axes=left.axes,
        judge_points=tuple(reversed(left.judge_points)),
        observation_medium=left.observation_medium,
        lifecycle=left.lifecycle,
        bundle_digest=left.bundle_digest,
        carrier_families=left.carrier_families,
    )

    assert left.digest == right.digest


def test_definition_binding_and_state_digests_are_canonical() -> None:
    seed = _seed()
    providers = [
        _provider("hall-detail", "2", "hall.detail"),
        _provider("hall-mass", "2", "hall.mass"),
    ]
    graph = {"1": (), "2": ("1",)}

    left = compile_judgment_debt(
        seed,
        providers,
        layer_dependencies=graph,
        layer_order=("1", "2"),
    )
    right = compile_judgment_debt(
        seed,
        tuple(reversed(providers)),
        layer_dependencies=graph,
        layer_order=("1", "2"),
    )

    assert left.binding.digest == right.binding.digest
    assert left.digest == right.digest
    assert JudgmentDebtState.pending(left).digest == JudgmentDebtState.pending(right).digest


def test_seed_is_frozen_and_definition_digest_covers_fault_owner_and_refs() -> None:
    seed = _seed()
    provider = _provider("hall-mass", "2", "hall.mass")
    graph = {"1": (), "2": ("1",)}
    definition = compile_judgment_debt(
        seed,
        [provider],
        layer_dependencies=graph,
        layer_order=("1", "2"),
    )

    with pytest.raises(FrozenInstanceError):
        seed.owner_layer = "2"  # type: ignore[misc]

    different_owner = compile_judgment_debt(
        replace(seed, fault_owner="form-owner"),
        [provider],
        layer_dependencies=graph,
        layer_order=("1", "2"),
    )
    different_ref = compile_judgment_debt(
        replace(
            seed,
            judge_points=(JudgmentPoint(frame=1, ref="refs/alternate.png"),),
        ),
        [provider],
        layer_dependencies=graph,
        layer_order=("1", "2"),
    )

    assert definition.digest != different_owner.digest
    assert definition.digest != different_ref.digest


def test_definition_preserves_owner_refs_fault_owner_and_bundle_identity() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    encoded = definition.as_dict()["seed"]

    assert encoded["owner_layer"] == "1"
    assert encoded["fault_owner"] == "camera-framing"
    assert encoded["judge_points"] == [
        {"frame": 1, "ref": "refs/hall-start.png"},
        {"frame": 40, "ref": "refs/hall-end.png"},
    ]
    assert encoded["bundle_digest"] == BUNDLE_DIGEST


def test_lifecycle_rejects_skips_and_terminal_rewrites() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    pending = JudgmentDebtState.pending(definition)
    evidence = hashlib.sha256(b"falsification").hexdigest()

    with pytest.raises(ValueError, match="resolution requires due"):
        resolve_judgment_debt(
            definition,
            pending,
            outcome="falsified",
            evidence_digest=evidence,
        )
    due = activate_judgment_debt(
        definition,
        pending,
        activation=_activation(definition),
        payment_generation_digest=PAYMENT_GENERATION_DIGEST,
        layer_id="2",
        replayed_unit_digests=_activation(definition).payer_unit_digests,
    )
    falsified = resolve_judgment_debt(
        definition,
        due,
        outcome="falsified",
        evidence_digest=evidence,
    )
    with pytest.raises(ValueError, match="activation requires pending_not_due"):
        activate_judgment_debt(
            definition,
            falsified,
            activation=_activation(definition),
            payment_generation_digest=PAYMENT_GENERATION_DIGEST,
            layer_id="2",
            replayed_unit_digests=_activation(definition).payer_unit_digests,
        )


def test_authority_records_round_trip_and_refuse_unknown_or_stale_digests() -> None:
    seed = _seed()
    provider = _provider("hall-mass", "2", "hall.mass")
    activation = compile_provider_activation(
        seed.subject_roles,
        seed.carrier_families,
        seed.owner_layer,
        (provider,),
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    definition = compile_judgment_debt(
        seed,
        (provider,),
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    payer = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("hall-form", hashlib.sha256(b"unit").hexdigest()),),
    )
    state = JudgmentDebtState.pending(definition)

    rows = (
        (seed, JudgmentDebtSeed),
        (provider, JudgmentProvider),
        (activation, ProviderActivation),
        (definition.binding, JudgmentProviderBinding),
        (definition, JudgmentDebtDefinition),
        (payer, JudgmentDebtActivation),
        (state, JudgmentDebtState),
    )
    for record, record_type in rows:
        assert record_type.from_dict(record.as_dict(), "record") == record

    tampered = deepcopy(definition.as_dict())
    tampered["definition_digest"] = "0" * 64
    with pytest.raises(ValueError, match="definition_digest is stale"):
        JudgmentDebtDefinition.from_dict(tampered, "definition")

    unknown = deepcopy(seed.as_dict())
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        JudgmentDebtSeed.from_dict(unknown, "seed")


def test_payment_generation_binds_exact_payer_completion_receipts() -> None:
    definition = compile_judgment_debt(
        _seed(),
        (_provider("hall-mass", "2", "hall.mass"),),
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    unit_digest = _digest("hall-form unit")
    activation = JudgmentDebtActivation.for_definition(
        definition,
        payer_unit_digests=(("2:hall-form", unit_digest),),
    )
    completion = JudgmentDebtCompletionBinding(
        layer_id="2",
        unit_id="hall-form",
        unit_digest=unit_digest,
        completion_receipt_digest=_digest("completion A"),
    )
    generation = JudgmentDebtPaymentGeneration(
        definition_digest=definition.digest,
        activation_digest=activation.digest,
        replay_prefix_digest=_digest("replay prefix A"),
        replay_completions=(completion,),
        payer_completions=(completion,),
    )

    generation.assert_matches(definition, activation)
    assert (
        JudgmentDebtPaymentGeneration.from_dict(generation.as_dict(), "generation")
        == generation
    )
    replacement = replace(
        completion,
        completion_receipt_digest=_digest("completion B"),
    )
    assert replace(
        generation,
        replay_completions=(replacement,),
        payer_completions=(replacement,),
    ).digest != generation.digest
    with pytest.raises(ValueError, match="exact subset"):
        replace(generation, payer_completions=(replacement,))


def test_debt_id_excludes_bundle_and_decision_strength_but_definition_does_not() -> None:
    seed = _seed()
    changed = replace(
        seed,
        bundle_digest=hashlib.sha256(b"another selected bundle").hexdigest(),
        decision_strength="planner_start",
    )
    provider = _provider("hall-mass", "2", "hall.mass")
    graph = {"1": (), "2": ("1",)}

    assert changed.debt_id == seed.debt_id
    assert (
        compile_judgment_debt(changed, (provider,), layer_dependencies=graph, layer_order=("1", "2")).digest
        != compile_judgment_debt(seed, (provider,), layer_dependencies=graph, layer_order=("1", "2")).digest
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (("claim_kind", "freeform"), ("property", "looks_good")),
)
def test_judgment_debt_semantics_are_closed(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        _seed(**{field: value})


def test_observation_request_is_pre_renderable_and_binds_exact_authority() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    activation = _activation(definition)
    request = _observation_request(
        definition,
        activation,
        reference_digest=_digest("immutable reference"),
    )

    request.assert_matches(definition, activation)
    assert JudgmentObservationRequest.from_dict(request.as_dict(), "request") == request
    assert request.request_digest == request.digest

    no_reference = _observation_request(definition, activation, reference_marker="no-reference")
    assert JudgmentObservationRequest.from_dict(no_reference.as_dict(), "request") == no_reference
    assert no_reference.digest != request.digest


def test_observation_request_rejects_stale_unknown_and_incompatible_fields() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    activation = _activation(definition)
    request = _observation_request(definition, activation, reference_marker="no-reference")

    with pytest.raises(ValueError, match="requires reference_marker"):
        _observation_request(definition, activation)
    with pytest.raises(ValueError, match="requires 'solid'"):
        replace(request, render_mode="eevee")

    stale = deepcopy(request.as_dict())
    stale["request_digest"] = "0" * 64
    with pytest.raises(ValueError, match="request_digest is stale"):
        JudgmentObservationRequest.from_dict(stale, "request")

    unknown = deepcopy(request.as_dict())
    unknown["unknown"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        JudgmentObservationRequest.from_dict(unknown, "request")


def test_no_optical_signal_failure_is_request_bound_and_round_trips() -> None:
    definition = compile_judgment_debt(
        _seed(),
        [_provider("hall-mass", "2", "hall.mass")],
        layer_dependencies={"1": (), "2": ("1",)},
        layer_order=("1", "2"),
    )
    request = _observation_request(
        definition,
        _activation(definition),
        reference_digest=_digest("immutable reference"),
    )
    failure = JudgmentPaymentAttemptFailure.for_request(
        request,
        reason="no_optical_signal",
        signal_metrics_digest=_digest("blank frame metrics"),
        candidate_capture_digest=_digest("candidate capture"),
    )

    failure.assert_matches_request(request)
    assert JudgmentPaymentAttemptFailure.from_dict(failure.as_dict(), "failure") == failure
    with pytest.raises(ValueError, match="reason"):
        replace(failure, reason="infrastructure_failure")
    with pytest.raises(ValueError, match="frame does not match"):
        replace(failure, frame=2).assert_matches_request(request)
