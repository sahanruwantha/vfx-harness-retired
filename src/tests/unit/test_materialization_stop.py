"""Typed terminal authority for rejected JIT materialization candidates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents.planner import (
    materialization_stop,
    materialization_stop_evidence,
    materialization_stop_state,
    rematerialize,
)
from vfx_harness.agents.resilience import AgentSessionFailure
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    PublishValidatedAmendmentTarget,
    RouteEngineeringTarget,
    SelectedAuthorityAmendmentCommitted,
)
from vfx_harness.evaluation.plan_gate import Finding, GateResult
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.jit_materialization import gate_evidence
from vfx_harness.orchestration.jit_materialization import publish as jit_publish
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    OVERLAY_ARTIFACTS,
    VIEW_SCHEMA,
    _require_upstream_outcomes,
    attest_materialization_finalization,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.plan_authority import PlanBundle

BUNDLE_DIGEST = "b" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stop_evidence(layout: run_artifacts.RunLayout) -> dict:
    return json.loads(
        (layout.reports / "materialization-stop-evidence.json").read_text(
            encoding="utf-8"
        )
    )


def _stop_audit(layout: run_artifacts.RunLayout) -> dict:
    return json.loads(
        (layout.reports / "materialization-stop-audit.json").read_text(
            encoding="utf-8"
        )
    )


def _set_gate_generated_at(path: Path, generated_at: str) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    value["gate_result"]["generated_at"] = generated_at
    unsigned = {key: item for key, item in value.items() if key != "record_digest"}
    value["record_digest"] = canonical_digest(unsigned)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
) -> tuple[Path, run_artifacts.RunLayout, PlanBundle, Path]:
    shot = tmp_path / run_id
    bundle_root = shot / "runs" / "publisher" / "checkpoints" / "plans" / "bundles" / BUNDLE_DIGEST
    bundle_root.mkdir(parents=True)
    (bundle_root / "bundle.json").write_text(
        json.dumps({"schema": "fixture", "content_hash": BUNDLE_DIGEST}),
        encoding="utf-8",
    )
    layer = {
        "id": "1",
        "execution": "jit_deferred",
        "jit": {"depends_on_layers": []},
    }
    (bundle_root / "layers.json").write_text(
        json.dumps({"schema": 5, "layers": [layer]}),
        encoding="utf-8",
    )
    for name, value in {
        "scene_checks.json": {"schema": 1, "contracts": []},
        "requirements.json": {"schema": "fixture", "requirements": []},
    }.items():
        (bundle_root / name).write_text(json.dumps(value), encoding="utf-8")
    (shot / "plans").mkdir(parents=True)
    (shot / "plans" / "current.json").write_text(
        json.dumps({"schema": "fixture", "content_hash": BUNDLE_DIGEST}),
        encoding="utf-8",
    )
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, run_id, shot_id=shot.name)
    bundle = PlanBundle(
        shot=shot,
        run_id="publisher",
        root=bundle_root,
        content_hash=BUNDLE_DIGEST,
        artifacts=(),
        outcome="clean_with_deferred",
    )
    monkeypatch.setattr(materialization_stop.plan_authority, "resolve_current", lambda _shot: bundle)
    monkeypatch.setattr(
        materialization_stop.plan_authority,
        "artifact_path",
        lambda _shot, name: bundle_root / name,
    )
    monkeypatch.setattr(
        materialization_stop.jit_publish,
        "selected_view_artifact",
        lambda *_args, **_kwargs: None,
    )
    candidate = layout.scratch / "jit-layer-1.json"
    candidate.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.jit-layer-materialization/v2",
                "bundle_hash": BUNDLE_DIGEST,
                "layer": {"id": "1", "execution": "ready", "stages": []},
                "scene_contracts": [],
                "image_contracts": [],
                "requirement_bindings": [],
                "acceptance": [],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return shot, layout, bundle, candidate


def _dirty_gate(shot_id: str, *, where: str = "layers.json#/layers/0/stages") -> GateResult:
    return GateResult(
        shot_id,
        findings=[
            Finding(
                check="claim-closure",
                blocking=True,
                where=where,
                what="a required claim has no structural producer",
                fix="bind it to one producing unit",
            )
        ],
    )


def test_invalid_durable_inputs_do_not_promote_opaque_bytes_into_authority() -> None:
    first_unit, first_unit_issues = materialization_stop_state.unit_state(
        json.dumps({"schema": 0, "layer": "1", "updated": "run-a"}).encode(),
        layer_id="1",
    )
    second_unit, second_unit_issues = materialization_stop_state.unit_state(
        json.dumps({"schema": 0, "layer": "1", "updated": "run-b"}).encode(),
        layer_id="1",
    )
    assert first_unit == second_unit == {"state": "invalid", "reason": "schema_or_layer"}
    assert first_unit_issues == second_unit_issues == ("layer_state_invalid",)

    first_resolution, first_resolution_issues = materialization_stop_state.active_resolution_state(
        json.dumps({"schema": "wrong", "resolved_at": "run-a"}).encode(),
        bundle_digest=BUNDLE_DIGEST,
        reserved_roles=(),
    )
    second_resolution, second_resolution_issues = materialization_stop_state.active_resolution_state(
        json.dumps({"schema": "wrong", "resolved_at": "run-b"}).encode(),
        bundle_digest=BUNDLE_DIGEST,
        reserved_roles=(),
    )
    assert first_resolution == second_resolution == {"state": "invalid", "reason": "schema"}
    assert first_resolution_issues == second_resolution_issues == ("plan_resolutions_invalid",)

    first_outcome, first_outcome_issues = materialization_stop_state.dependency_outcome_state(
        json.dumps(["run-a"]).encode(),
        dependency="2",
        required_outcomes=frozenset(),
        script_state=None,
        current_eligibility=None,
    )
    second_outcome, second_outcome_issues = materialization_stop_state.dependency_outcome_state(
        json.dumps(["run-b"]).encode(),
        dependency="2",
        required_outcomes=frozenset(),
        script_state=None,
        current_eligibility=None,
    )
    assert first_outcome == second_outcome == {
        "layer": "2",
        "status": "invalid",
        "reason": "shape",
    }
    assert first_outcome_issues == second_outcome_issues == ("dependency_outcome_2_invalid",)

    for marker in ("run-a", "run-b"):
        outcome, outcome_issues = materialization_stop_state.dependency_outcome_state(
            json.dumps(
                {
                    "schema": 1,
                    "layer": "2",
                    "status": "passed",
                    "history": marker,
                }
            ).encode(),
            dependency="2",
            required_outcomes=frozenset(),
            script_state=None,
            current_eligibility=None,
        )
        assert outcome == {"layer": "2", "status": "invalid", "reason": "schema"}
        assert outcome_issues == ("dependency_outcome_2_invalid",)


def test_invalid_authority_bytes_change_audit_not_stop_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unresolvable(_shot: Path) -> PlanBundle:
        raise materialization_stop.plan_authority.PlanPublicationError(
            "fixture authority is invalid"
        )

    def unexpected_inspection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid authority must stop before candidate inspection")

    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        unexpected_inspection,
    )
    results = []
    for run_id, marker in (
        ("materialization-invalid-input-a", "run-a"),
        ("materialization-invalid-input-b", "run-b"),
    ):
        shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        (bundle.root / "layers.json").write_text(
            json.dumps(
                {
                    "schema": 5,
                    "layers": [
                        {
                            "id": "1",
                            "execution": "jit_deferred",
                            "jit": {"depends_on_layers": ["2"]},
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (shot / "plans" / "current.json").write_bytes(
            f'{{"opaque_pointer":"{marker}"'.encode()
        )
        (bundle.root / "bundle.json").write_bytes(
            f'{{"opaque_manifest":"{marker}"'.encode()
        )
        monkeypatch.setattr(
            materialization_stop.plan_authority,
            "resolve_current",
            unresolvable,
        )

        view_pointer = shot / CURRENT
        view_pointer.parent.mkdir(parents=True, exist_ok=True)
        view_pointer.write_bytes(f'{{"opaque_view":"{marker}"'.encode())
        unit_state = shot / "state" / "work-units" / "layer_1.json"
        unit_state.parent.mkdir(parents=True, exist_ok=True)
        unit_state.write_text(
            json.dumps({"schema": 0, "layer": "1", "opaque": marker}),
            encoding="utf-8",
        )
        (shot / "state" / "plan-resolutions.jsonl").write_text(
            json.dumps({"schema": "wrong", "opaque": marker}),
            encoding="utf-8",
        )
        outcome = layer_outcome_path(shot, "2")
        outcome.parent.mkdir(parents=True, exist_ok=True)
        outcome.write_text(json.dumps([marker]), encoding="utf-8")

        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append((envelope, _stop_evidence(layout), _stop_audit(layout)))

    first, second = results
    assert first[0].stop_class == second[0].stop_class == "harness_defect"
    assert first[0].cause_fingerprint == second[0].cause_fingerprint
    assert first[0].authoritative_before_digest == second[0].authoritative_before_digest
    assert first[0].attempt_evidence_digest == second[0].attempt_evidence_digest
    assert first[0].artifact_state_digest == second[0].artifact_state_digest
    assert first[0].evidence_refs[0].record_digest == (
        second[0].evidence_refs[0].record_digest
    )
    assert first[0].evidence_refs[0].sha256 == second[0].evidence_refs[0].sha256
    assert first[1] == second[1]

    first_audit = first[2]["audit_locators"]["authority_before"]
    second_audit = second[2]["audit_locators"]["authority_before"]
    assert first_audit["selected_bundle"]["pointer_record"]["sha256"] != (
        second_audit["selected_bundle"]["pointer_record"]["sha256"]
    )
    assert first_audit["selected_bundle"]["manifest_record"]["sha256"] != (
        second_audit["selected_bundle"]["manifest_record"]["sha256"]
    )
    assert first_audit["selected_view"]["pointer_record"]["sha256"] != (
        second_audit["selected_view"]["pointer_record"]["sha256"]
    )
    assert first_audit["layer_state_record"]["sha256"] != (
        second_audit["layer_state_record"]["sha256"]
    )
    assert first_audit["plan_resolutions_record"]["sha256"] != (
        second_audit["plan_resolutions_record"]["sha256"]
    )
    assert first_audit["dependency_outcomes"]["2"]["record"]["sha256"] != (
        second_audit["dependency_outcomes"]["2"]["record"]["sha256"]
    )


def test_invalid_selected_view_artifact_bytes_are_audit_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    view_digest = "e" * 64
    for run_id, marker in (
        ("materialization-invalid-view-a", "run-a"),
        ("materialization-invalid-view-b", "run-b"),
    ):
        shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        view = shot / "state" / "jit-layers" / "views" / view_digest
        view.mkdir(parents=True)
        for name in OVERLAY_ARTIFACTS:
            (view / name).write_text(
                json.dumps({"artifact": name, "opaque": marker}, sort_keys=True),
                encoding="utf-8",
            )
        pointer = shot / CURRENT
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "schema": VIEW_SCHEMA,
                    "bundle_hash": BUNDLE_DIGEST,
                    "view_hash": view_digest,
                    "materialized_layers": [],
                    "artifacts": {
                        name: (view / name).relative_to(shot).as_posix()
                        for name in OVERLAY_ARTIFACTS
                    },
                    "hashes": dict.fromkeys(OVERLAY_ARTIFACTS, "f" * 64),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append((envelope, _stop_evidence(layout), _stop_audit(layout)))

    first, second = results
    selected_view = first[1]["authoritative_before"]["selected_view"]
    assert selected_view["selection"] == "invalid"
    assert "artifacts" not in selected_view
    assert selected_view["reason_codes"] == [
        f"selected_view_{name}_hash_mismatch" for name in sorted(OVERLAY_ARTIFACTS)
    ]
    assert first[0].authoritative_before_digest == second[0].authoritative_before_digest
    assert first[0].attempt_evidence_digest == second[0].attempt_evidence_digest
    assert first[0].artifact_state_digest == second[0].artifact_state_digest
    assert first[0].evidence_refs[0].record_digest == (
        second[0].evidence_refs[0].record_digest
    )
    assert first[0].evidence_refs[0].sha256 == second[0].evidence_refs[0].sha256
    assert first[1] == second[1]
    first_records = first[2]["audit_locators"]["authority_before"]["selected_view"][
        "artifact_records"
    ]
    second_records = second[2]["audit_locators"]["authority_before"]["selected_view"][
        "artifact_records"
    ]
    assert {
        name: row["sha256"] for name, row in first_records.items()
    } != {
        name: row["sha256"] for name, row in second_records.items()
    }


def test_stale_passed_dependency_is_invalid_with_opaque_bytes_audit_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_reasons = (
        "input manifest changed",
        "reference changed or is missing for f40",
        "sealed canonical changed or is missing for f40",
    )
    semantic_envelopes = []
    helper_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        materialization_stop_evidence.ledger,
        "load_layers_from_path",
        lambda _path: {"2": SimpleNamespace(id="2")},
    )

    def unexpected_inspection(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a stale dependency must stop before candidate inspection")

    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        unexpected_inspection,
    )
    for reason_index, reason in enumerate(stale_reasons):
        pair = []

        def stale(
            _folder: Path,
            layer: SimpleNamespace,
            outcome: object,
            *,
            _reason: str = reason,
        ) -> tuple[bool, tuple[str, ...]]:
            assert isinstance(outcome, dict)
            helper_calls.append((layer.id, str(outcome["run_id"])))
            return False, (_reason,)

        monkeypatch.setattr(revalidation, "current_outcome_eligibility", stale)
        for marker in ("run-a", "run-b"):
            run_id = f"materialization-stale-{reason_index}-{marker}"
            shot, layout, bundle, candidate = _fixture(
                tmp_path,
                monkeypatch,
                run_id=run_id,
            )
            (bundle.root / "layers.json").write_text(
                json.dumps(
                    {
                        "schema": 5,
                        "layers": [
                            {
                                "id": "1",
                                "execution": "jit_deferred",
                                "jit": {
                                    "depends_on_layers": ["2"],
                                    "required_outcomes": [
                                        {"kind": "scene_contract", "id": "handoff"}
                                    ],
                                },
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            script = shot / "build" / "units" / "02" / "handoff.py"
            script.parent.mkdir(parents=True)
            script.write_text(f"RUN = {marker!r}\n", encoding="utf-8")
            outcome_path = layer_outcome_path(shot, "2")
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "layer": "2",
                        "status": "passed",
                        "script": script.relative_to(shot).as_posix(),
                        "interfaces": [
                            {"id": "handoff", "owner_layer": "2", "pass": True}
                        ],
                        "canonical": [
                            {
                                "authoritative": [
                                    {
                                        "id": "handoff",
                                        "source": "interface_contract",
                                        "owner_layer": "2",
                                        "pass": True,
                                    }
                                ]
                            }
                        ],
                        "revalidation_manifest": {"opaque_run": marker},
                        "run_id": marker,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            envelope = materialization_stop.publish_materialization_stop(
                layout,
                bundle=bundle,
                layer_id="1",
                candidate=candidate,
            )
            pair.append((envelope, _stop_evidence(layout), _stop_audit(layout)))

        first, second = pair
        expected_state = {
            "layer": "2",
            "status": "invalid",
            "reason": "stale",
            "eligibility_reasons": [reason],
        }
        assert first[1]["authoritative_before"]["dependency_outcomes"]["2"] == (
            expected_state
        )
        assert second[1]["authoritative_before"]["dependency_outcomes"]["2"] == (
            expected_state
        )
        assert first[0].stop_class == second[0].stop_class == "harness_defect"
        assert first[0].cause_fingerprint == second[0].cause_fingerprint
        assert first[0].authoritative_before_digest == (
            second[0].authoritative_before_digest
        )
        assert first[0].attempt_evidence_digest == second[0].attempt_evidence_digest
        assert first[0].evidence_refs[0].record_digest == (
            second[0].evidence_refs[0].record_digest
        )
        assert first[1] == second[1]
        first_audit = first[2]["audit_locators"]["authority_before"]
        second_audit = second[2]["audit_locators"]["authority_before"]
        assert first_audit["dependency_outcomes"]["2"]["record"]["sha256"] != (
            second_audit["dependency_outcomes"]["2"]["record"]["sha256"]
        )
        assert first_audit["dependency_outcomes"]["2"]["script_record"][
            "sha256"
        ] != second_audit["dependency_outcomes"]["2"]["script_record"]["sha256"]
        semantic_envelopes.append(first[0])

    assert helper_calls == [
        ("2", marker)
        for _reason in stale_reasons
        for marker in ("run-a", "run-b")
    ]
    assert len(
        {envelope.authoritative_before_digest for envelope in semantic_envelopes}
    ) == len(stale_reasons)
    assert len({envelope.attempt_evidence_digest for envelope in semantic_envelopes}) == (
        len(stale_reasons)
    )


@pytest.mark.parametrize(
    "status",
    ["frozen", "evaluating", "repairing", "passed"],
)
def test_durable_unit_status_requiring_checkpoint_fails_closed_without_one(
    status: str,
) -> None:
    state, issues = materialization_stop_state.unit_state(
        json.dumps(
            {
                "schema": 1,
                "digest_schema": 4,
                "layer": "1",
                "plan_hash": "a" * 64,
                "revision": 2,
                "units": {
                    "hero": {
                        "status": status,
                        "unit_hash": "b" * 64,
                    }
                },
            }
        ).encode(),
        layer_id="1",
    )

    assert state == {"state": "invalid", "reason": "unit_shape"}
    assert issues == ("layer_state_invalid",)


def test_hypothesis_falsified_unit_requires_its_typed_finding() -> None:
    state, issues = materialization_stop_state.unit_state(
        json.dumps(
            {
                "schema": 1,
                "digest_schema": 4,
                "layer": "1",
                "plan_hash": "a" * 64,
                "revision": 2,
                "units": {
                    "hero": {
                        "status": "hypothesis_falsified",
                        "unit_hash": "b" * 64,
                    }
                },
            }
        ).encode(),
        layer_id="1",
    )

    assert state == {"state": "invalid", "reason": "unit_shape"}
    assert issues == ("layer_state_invalid",)


def test_malformed_view_pointer_and_nested_outcome_audit_are_not_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, _bundle, _candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-invalid-authority",
    )
    pointer = shot / CURRENT
    pointer.parent.mkdir(parents=True, exist_ok=True)
    states = []
    audits = []
    for payload in (b'{"run":"a"', b'{"different":"opaque"'):
        pointer.write_bytes(payload)
        state, view_digest, issues, audit = materialization_stop_evidence._selected_view_state(
            layout,
            bundle_digest=BUNDLE_DIGEST,
        )
        states.append(state)
        audits.append(audit)
        assert view_digest is None
        assert issues == ("selected_view_pointer_malformed",)
    assert states[0] == states[1] == {
        "selection": "invalid",
        "pointer": {"state": "present"},
        "reason": "malformed",
    }
    assert audits[0]["pointer_record"]["sha256"] != audits[1]["pointer_record"]["sha256"]

    nested = {
        "schema": 2,
        "layer": "2",
        "status": "passed",
        "interfaces": [],
        "canonical": [],
        "audit": {"id": "handoff", "pass": True, "run_id": "opaque"},
    }
    outcome, issues = materialization_stop_state.dependency_outcome_state(
        json.dumps(nested).encode(),
        dependency="2",
        required_outcomes=frozenset({("scene_contract", "handoff")}),
        script_state=None,
        current_eligibility=None,
    )
    assert issues == ("dependency_outcome_2_invalid",)
    assert outcome == {"layer": "2", "status": "invalid", "reason": "canonical_missing"}


def test_upstream_outcome_gate_requires_schema_layer_and_registered_evidence_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_id = "camera.hero"
    outcome_path = layer_outcome_path(tmp_path, dependency_id)
    outcome_path.parent.mkdir(parents=True)
    layer = SimpleNamespace(
        id="look.final",
        jit=SimpleNamespace(
            depends_on_layers=(dependency_id,),
            required_outcomes=(("scene_contract", "handoff"),),
        ),
    )
    available_layers = {dependency_id: SimpleNamespace(id=dependency_id)}
    monkeypatch.setattr(
        revalidation,
        "current_outcome_eligibility",
        lambda *_args, **_kwargs: (True, ()),
    )
    base = {
        "schema": 2,
        "layer": dependency_id,
        "status": "passed",
        "interfaces": [],
        "canonical": [{"authoritative": []}],
    }
    outcome_path.write_text(
        json.dumps({**base, "audit": {"id": "handoff", "pass": True}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="required upstream outcomes have not passed"):
        _require_upstream_outcomes(tmp_path, layer, available_layers)

    outcome_path.write_text(
        json.dumps(
            {
                **base,
                "interfaces": [
                    {"id": "handoff", "owner_layer": dependency_id, "pass": True}
                ],
                "canonical": [
                    {
                        "authoritative": [
                            {
                                "id": "handoff",
                                "source": "interface_contract",
                                "owner_layer": dependency_id,
                                "pass": True,
                            }
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _require_upstream_outcomes(tmp_path, layer, available_layers)

    monkeypatch.setattr(
        revalidation,
        "current_outcome_eligibility",
        lambda *_args, **_kwargs: (False, ("input manifest changed",)),
    )
    with pytest.raises(ValueError, match="sealed outcome is stale: input manifest changed"):
        _require_upstream_outcomes(tmp_path, layer, available_layers)
    monkeypatch.setattr(
        revalidation,
        "current_outcome_eligibility",
        lambda *_args, **_kwargs: (True, ()),
    )

    outcome_path.write_text(
        json.dumps({**base, "schema": 1}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid sealed outcome"):
        _require_upstream_outcomes(tmp_path, layer, available_layers)


def test_local_structural_findings_authorize_only_validated_candidate_amendment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-local-stop",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: (
            [
                "/layer/stages: candidate /tmp/attempt/jit-layer-1.json "
                "has no bounded work unit",
                "/scene_contracts/0: required camera role has no binding mutator",
            ],
            None,
        ),
    )

    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    assert envelope.stage == "materialization"
    assert envelope.stop_class == "authority_defect"
    assert envelope.identity.bundle_digest == BUNDLE_DIGEST
    assert envelope.identity.layer_id == "1"
    assert envelope.identity.candidate_digest == _sha(candidate)
    assert [action.transaction_id for action in envelope.actions] == [
        "publish_validated_amendment"
    ]
    action = envelope.actions[0]
    assert isinstance(action.target, PublishValidatedAmendmentTarget)
    assert action.target.scope == "layer_view"
    assert action.target.base_bundle.bundle_digest == BUNDLE_DIGEST
    assert action.target.base_view is None
    assert action.target.layer_id == "1"
    assert tuple(sorted(row.record_id for row in action.target.findings)) == (
        envelope.cause.finding_ids
    )
    assert isinstance(action.postcondition, SelectedAuthorityAmendmentCommitted)
    assert action.postcondition.gate_policy_id == (
        "structural-authority/runtime-falsification-v1"
    )
    evidence = _stop_evidence(layout)
    assert any(
        "<locator>" in row["causal_fact"]["message"]
        for row in evidence["blocking_findings"]
    )
    assert "locator" not in evidence["candidate"]
    unsigned = {key: value for key, value in evidence.items() if key != "record_digest"}
    assert evidence["record_digest"] == canonical_digest(unsigned)
    assert {row.evidence for row in action.target.findings} == set(envelope.evidence_refs)
    assert envelope.evidence_refs[0].record_digest == evidence["record_digest"]
    assert envelope.evidence_refs[0].sha256 == _sha(
        layout.reports / "materialization-stop-evidence.json"
    )
    audit = json.loads(
        (layout.reports / "materialization-stop-audit.json").read_text(encoding="utf-8")
    )
    assert {
        row["fact"]["pointer"]
        for row in audit["audit_locators"]["blocking_findings"]
    } == {"/layer/stages", "/scene_contracts/0"}
    assert audit["audit_locators"]["candidate_record"]["locator"].endswith(
        "scratch/jit-layer-1.json"
    )
    assert StopEnvelope.from_dict(envelope.as_dict(), "materialization stop") == envelope


def test_dirty_terminal_gate_is_content_bound_authority_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-gate-stop",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: ([], object()),
    )
    gate_evidence.write_materialization_gate_evidence(
        layout,
        candidate=candidate,
        bundle_digest=BUNDLE_DIGEST,
        layer_id="1",
        gate_result=_dirty_gate(shot.name),
    )

    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    assert envelope.stop_class == "authority_defect"
    assert envelope.cause.invariant_id == "materialization_structural_authority_blocked"
    assert [action.transaction_id for action in envelope.actions] == [
        "publish_validated_amendment"
    ]
    evidence = _stop_evidence(layout)
    assert evidence["classification"]["source"] == "terminal_gate"
    gate_state = evidence["artifact_state"]["gate_evidence"]
    assert gate_state["validation"] == "verified"
    assert gate_state["record_digest"] == canonical_digest(gate_state["document"])
    assert "sha256" not in gate_state
    audit = _stop_audit(layout)
    assert audit["audit_locators"]["gate_evidence_record"]["record"]["sha256"] == (
        _sha(layout.reports / "materialization-gate-evidence.json")
    )


@pytest.mark.parametrize("failure", ["missing", "malformed", "stale_candidate"])
def test_missing_malformed_or_stale_gate_evidence_routes_to_engineering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id=f"materialization-gate-{failure}",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: ([], object()),
    )
    report = layout.reports / "materialization-gate-evidence.json"
    if failure != "missing":
        gate_evidence.write_materialization_gate_evidence(
            layout,
            candidate=candidate,
            bundle_digest=BUNDLE_DIGEST,
            layer_id="1",
            gate_result=_dirty_gate(shot.name),
        )
        if failure == "malformed":
            report.write_text("{", encoding="utf-8")
        else:
            value = json.loads(candidate.read_text(encoding="utf-8"))
            value["acceptance"] = [{"id": "changed"}]
            candidate.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    assert envelope.stop_class == "harness_defect"
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]
    assert envelope.cause.invariant_id == "materialization_requires_typed_evidence"
    target = envelope.actions[0].target
    postcondition = envelope.actions[0].postcondition
    assert isinstance(target, RouteEngineeringTarget)
    assert isinstance(postcondition, EngineeringRouteCommitted)
    assert target.defect_record.evidence == envelope.evidence_refs[0]
    assert postcondition.defect_packet_digest == envelope.evidence_refs[0].record_digest
    assert target.attempt_evidence_digest == envelope.attempt_evidence_digest
    assert target.cause_fingerprint == envelope.cause_fingerprint


def test_clean_attested_candidate_that_failed_to_publish_is_a_harness_defect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-clean-unpublished",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: ([], object()),
    )
    attest_materialization_finalization(candidate, bundle_hash=BUNDLE_DIGEST)
    gate_evidence.write_materialization_gate_evidence(
        layout,
        candidate=candidate,
        bundle_digest=BUNDLE_DIGEST,
        layer_id="1",
        gate_result=GateResult(shot.name),
    )

    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    assert envelope.stop_class == "harness_defect"
    assert envelope.cause.finding_ids == (
        "materialization-evidence:attested_candidate_failed_to_publish",
    )
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]


def test_verified_current_view_digest_is_pinned_in_stop_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-view-pinned",
    )
    documents = {
        "layers.json": {"schema": 5, "layers": []},
        "scene_checks.json": {"schema": 1, "contracts": []},
        "checks.json": {"schema": 1, "checks": []},
        "requirements.json": {"schema": 1, "requirements": []},
        "acceptance.json": [],
    }
    view_digest = jit_publish._canonical_view_hash(documents)
    view = shot / "state" / "jit-layers" / "views" / view_digest
    view.mkdir(parents=True)
    for name, document in documents.items():
        (view / name).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    pointer = shot / "state" / "jit-layers" / "current.json"
    pointer.write_text(
        json.dumps(
            {
                "schema": VIEW_SCHEMA,
                "bundle_hash": BUNDLE_DIGEST,
                "view_hash": view_digest,
                "materialized_layers": [],
                "artifacts": {
                    name: (view / name).relative_to(shot).as_posix()
                    for name in OVERLAY_ARTIFACTS
                },
                "hashes": {name: _sha(view / name) for name in OVERLAY_ARTIFACTS},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: (["/layer/stages: no units"], None),
    )
    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    assert envelope.stop_class == "authority_defect"
    assert envelope.identity.view_digest == view_digest
    target = envelope.actions[0].target
    assert isinstance(target, PublishValidatedAmendmentTarget)
    assert target.base_view is not None
    assert target.base_view.bundle_digest == BUNDLE_DIGEST
    assert target.base_view.view_digest == view_digest


def test_gate_identity_ignores_run_file_locator_and_timestamp_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    for run_id, where, generated_at in (
        (
            "materialization-cause-a",
            "layers.json#/layers/0/stages",
            "2026-08-30T01:02:03+00:00",
        ),
        (
            "materialization-cause-b",
            "other-file.json#/layers/0/stages",
            "2026-08-31T04:05:06+00:00",
        ),
    ):
        shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        monkeypatch.setattr(
            materialization_stop,
            "inspect_materialization",
            lambda *_args, **_kwargs: ([], object()),
        )
        gate_report = gate_evidence.write_materialization_gate_evidence(
            layout,
            candidate=candidate,
            bundle_digest=BUNDLE_DIGEST,
            layer_id="1",
            gate_result=_dirty_gate(shot.name, where=where),
        )
        _set_gate_generated_at(gate_report, generated_at)
        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append(
            (envelope, _stop_evidence(layout), _stop_audit(layout))
        )

    first, second = results
    assert first[0].cause_fingerprint == second[0].cause_fingerprint
    assert first[0].attempt_evidence_digest == second[0].attempt_evidence_digest
    assert first[0].artifact_state_digest == second[0].artifact_state_digest
    assert first[0].authoritative_before_digest == second[0].authoritative_before_digest
    assert first[0].identity.run_id != second[0].identity.run_id
    assert first[0].evidence_refs[0].sha256 == second[0].evidence_refs[0].sha256
    assert first[0].evidence_refs[0].record_digest == (
        second[0].evidence_refs[0].record_digest
    )
    assert first[1] == second[1]
    first_gate_audit = first[2]["audit_locators"]["gate_evidence_record"]
    second_gate_audit = second[2]["audit_locators"]["gate_evidence_record"]
    assert first_gate_audit["record"]["sha256"] != second_gate_audit["record"]["sha256"]
    assert first_gate_audit["document"]["gate_result"]["generated_at"] != (
        second_gate_audit["document"]["gate_result"]["generated_at"]
    )


def test_same_local_message_at_different_pointers_keeps_both_repair_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _shot, layout, bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-distinct-repair-targets",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: (
            [
                "/layer/stages/0: required claim has no producer",
                "/layer/stages/1: required claim has no producer",
            ],
            None,
        ),
    )

    envelope = materialization_stop.publish_materialization_stop(
        layout,
        bundle=bundle,
        layer_id="1",
        candidate=candidate,
    )

    evidence = _stop_evidence(layout)
    findings = evidence["blocking_findings"]
    assert {row["causal_fact"]["pointer"] for row in findings} == {
        "/layer/stages/0",
        "/layer/stages/1",
    }
    assert len({row["finding_id"] for row in findings}) == 2
    assert set(envelope.cause.finding_ids) == {
        row["finding_id"] for row in findings
    }


def test_cited_stop_evidence_identity_ignores_run_local_locators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    for run_id, attempt_path, published_at in (
        (
            "materialization-evidence-a",
            "/tmp/run-a/jit-layer-1.json",
            "2026-08-30T01:02:03+00:00",
        ),
        (
            "materialization-evidence-b",
            "/var/tmp/run-b/materialized.json",
            "2026-08-31T04:05:06+00:00",
        ),
    ):
        shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        (shot / "plans" / "current.json").write_text(
            json.dumps(
                {
                    "schema": "vfx-harness.plan-pointer/v1",
                    "run_id": f"publisher-{run_id}",
                    "bundle": (
                        f"runs/publisher-{run_id}/checkpoints/plans/bundles/"
                        f"{BUNDLE_DIGEST}"
                    ),
                    "content_hash": BUNDLE_DIGEST,
                    "outcome": "clean_with_deferred",
                    "published_at": published_at,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (bundle.root / "bundle.json").write_text(
            json.dumps(
                {
                    "schema": "vfx-harness.plan-bundle/v1",
                    "run_id": f"publisher-{run_id}",
                    "content_hash": BUNDLE_DIGEST,
                    "outcome": "clean_with_deferred",
                    "artifacts": {},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            materialization_stop,
            "inspect_materialization",
            lambda *_args, _path=attempt_path, **_kwargs: (
                [f"/layer/stages: candidate {_path} has no bounded work unit"],
                None,
            ),
        )
        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append((envelope, _stop_evidence(layout), _stop_audit(layout)))

    first_ref = results[0][0].evidence_refs[0]
    second_ref = results[1][0].evidence_refs[0]
    assert first_ref.locator != second_ref.locator
    assert first_ref.sha256 == second_ref.sha256
    assert first_ref.record_digest == second_ref.record_digest
    assert first_ref.digest == second_ref.digest
    assert results[0][1] == results[1][1]
    assert results[0][0].attempt_evidence_digest == results[1][0].attempt_evidence_digest
    assert results[0][0].authoritative_before_digest == (
        results[1][0].authoritative_before_digest
    )
    first_target = results[0][0].actions[0].target
    second_target = results[1][0].actions[0].target
    assert isinstance(first_target, PublishValidatedAmendmentTarget)
    assert isinstance(second_target, PublishValidatedAmendmentTarget)
    assert first_target.base_bundle.selection_digest == (
        second_target.base_bundle.selection_digest
    )
    first_authority_audit = results[0][2]["audit_locators"]["authority_before"]
    second_authority_audit = results[1][2]["audit_locators"]["authority_before"]
    assert first_authority_audit["selected_bundle"]["pointer_record"]["sha256"] != (
        second_authority_audit["selected_bundle"]["pointer_record"]["sha256"]
    )
    assert first_authority_audit["selected_bundle"]["manifest_record"]["sha256"] != (
        second_authority_audit["selected_bundle"]["manifest_record"]["sha256"]
    )


def test_durable_authority_identity_ignores_audit_clocks_and_run_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    for run_id, timestamp in (
        ("materialization-durable-a", "2026-08-30T01:02:03+00:00"),
        ("materialization-durable-b", "2026-08-31T04:05:06+00:00"),
    ):
        shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        layer = {
            "id": "1",
            "execution": "jit_deferred",
            "jit": {
                "depends_on_layers": ["2"],
                "required_outcomes": [{"kind": "scene_contract", "id": "handoff"}],
                "reserved_roles": ["hero"],
            },
        }
        (bundle.root / "layers.json").write_text(
            json.dumps({"schema": 5, "layers": [layer]}, sort_keys=True),
            encoding="utf-8",
        )
        unit_state_path = shot / "state" / "work-units" / "layer_1.json"
        unit_state_path.parent.mkdir(parents=True)
        unit_state_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "digest_schema": 4,
                    "layer": "1",
                    "plan_hash": "c" * 64,
                    "revision": 3,
                    "units": {
                        "hero_form": {
                            "status": "passed",
                            "unit_hash": "d" * 64,
                            "checkpoint": {
                                "at": timestamp,
                                "candidate_hash": "e" * 64,
                                "settings_hash": "f" * 64,
                                "script_hash": "1" * 64,
                                "input_hash": "2" * 64,
                                "protected_contract_ids": [],
                                "unit_hash": "d" * 64,
                            },
                            "updated": timestamp,
                            "history": [
                                {
                                    "at": timestamp,
                                    "from": "evaluating",
                                    "to": "passed",
                                    "reason": f"accepted in {run_id}",
                                }
                            ],
                        }
                    },
                    "updated": timestamp,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        resolutions = shot / "state" / "plan-resolutions.jsonl"
        resolutions.write_text(
            json.dumps(
                {
                    "schema": "vfx-harness.plan-resolutions/v1",
                    "bundle_hash": BUNDLE_DIGEST,
                    "kind": "decision",
                    "id": "hero-scale",
                    "status": "satisfied",
                    "decision": "adopt measured scale",
                    "values": {
                        "contract": {
                            "kind": "bbox_height",
                            "roles": ["hero"],
                            "lo": 0.3,
                            "hi": 0.6,
                        }
                    },
                    "resolved_at": timestamp,
                    "resolved_by": f"run:{run_id}",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        script = shot / "build" / "units" / "02" / "handoff.py"
        script.parent.mkdir(parents=True)
        script.write_text("VALUE = 1\n", encoding="utf-8")
        outcome = layer_outcome_path(shot, "2")
        outcome.parent.mkdir(parents=True)
        outcome.write_text(
            json.dumps(
                {
                    "schema": 2,
                    "at": timestamp,
                    "layer": "2",
                    "status": "passed",
                    "run_id": run_id,
                    "attempt": 7,
                    "script": script.relative_to(shot).as_posix(),
                    "interfaces": [
                        {
                            "id": "handoff",
                            "owner_layer": "2",
                            "pass": True,
                            "value": 1,
                        }
                    ],
                    "canonical": [
                        {
                            "authoritative": [
                                {
                                    "id": "handoff",
                                    "source": "interface_contract",
                                    "owner_layer": "2",
                                    "pass": True,
                                    "value": 1,
                                },
                                {
                                    "id": "checkpoint",
                                    "source": "builder_state",
                                    "owner_layer": "2",
                                    "pass": True,
                                },
                            ]
                        }
                    ],
                    "last_revalidation": {"at": timestamp, "run_id": run_id},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            materialization_stop,
            "inspect_materialization",
            lambda *_args, **_kwargs: (["/layer/stages: no units"], None),
        )
        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append((envelope, _stop_evidence(layout), _stop_audit(layout)))

    first, second = results
    assert first[1]["authoritative_before"]["layer_state"]["state"] == "present"
    assert second[1]["authoritative_before"]["layer_state"]["state"] == "present"
    assert first[0].authoritative_before_digest == second[0].authoritative_before_digest
    assert first[0].attempt_evidence_digest == second[0].attempt_evidence_digest
    assert first[0].evidence_refs[0].record_digest == second[0].evidence_refs[0].record_digest
    assert first[1] == second[1]
    first_audit = first[2]["audit_locators"]["authority_before"]
    second_audit = second[2]["audit_locators"]["authority_before"]
    assert first_audit["layer_state_record"]["sha256"] != (
        second_audit["layer_state_record"]["sha256"]
    )
    assert first_audit["plan_resolutions_record"]["sha256"] != (
        second_audit["plan_resolutions_record"]["sha256"]
    )
    assert first_audit["dependency_outcomes"]["2"]["record"]["sha256"] != (
        second_audit["dependency_outcomes"]["2"]["record"]["sha256"]
    )


def test_rematerialization_overlay_changes_attempt_not_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    for run_id, changed in (
        ("materialization-overlay-a", False),
        ("materialization-overlay-b", True),
    ):
        _shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        overlay = layout.scratch / "replacement-base"
        overlay.mkdir()
        for name in OVERLAY_ARTIFACTS:
            payload = {"name": name, "revision": 2 if changed and name == "layers.json" else 1}
            (overlay / name).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        monkeypatch.setattr(
            materialization_stop,
            "inspect_materialization",
            lambda *_args, **_kwargs: (["/layer/stages: no units"], None),
        )
        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
            overlay_root=overlay,
        )
        results.append(envelope)

    assert results[0].authoritative_before_digest == results[1].authoritative_before_digest
    assert results[0].artifact_state_digest != results[1].artifact_state_digest
    assert results[0].attempt_evidence_digest != results[1].attempt_evidence_digest


def test_semantic_layer_authority_change_changes_stop_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = []
    for run_id, reserved_roles in (
        ("materialization-authority-a", ["hero"]),
        ("materialization-authority-b", ["hero", "set"]),
    ):
        _shot, layout, bundle, candidate = _fixture(
            tmp_path,
            monkeypatch,
            run_id=run_id,
        )
        layer = {
            "id": "1",
            "execution": "jit_deferred",
            "jit": {"depends_on_layers": [], "reserved_roles": reserved_roles},
        }
        (bundle.root / "layers.json").write_text(
            json.dumps({"schema": 5, "layers": [layer]}, sort_keys=True),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            materialization_stop,
            "inspect_materialization",
            lambda *_args, **_kwargs: (["/layer/stages: no units"], None),
        )
        envelope = materialization_stop.publish_materialization_stop(
            layout,
            bundle=bundle,
            layer_id="1",
            candidate=candidate,
        )
        results.append(envelope)

    assert results[0].authoritative_before_digest != results[1].authoritative_before_digest
    assert results[0].attempt_evidence_digest != results[1].attempt_evidence_digest
    assert results[0].evidence_refs[0].record_digest != results[1].evidence_refs[0].record_digest


def test_public_materialization_boundary_raises_the_compiled_typed_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot_path, layout, _bundle, candidate = _fixture(
        tmp_path,
        monkeypatch,
        run_id="materialization-public-boundary",
    )
    monkeypatch.setattr(
        materialization_stop,
        "inspect_materialization",
        lambda *_args, **_kwargs: (["/layer/stages: no bounded work unit"], None),
    )
    monkeypatch.setattr(rematerialize.run_artifacts, "ensure", lambda *_args, **_kwargs: layout)

    def seed(_root: Path, target: Path, **_kwargs: object) -> None:
        target.write_bytes(candidate.read_bytes())

    monkeypatch.setattr(rematerialize, "seed_materialization_candidate", seed)
    monkeypatch.setattr(rematerialize, "_materialization_kickoff", lambda *_args, **_kwargs: "kickoff")
    monkeypatch.setattr(rematerialize, "build_plan_tools", lambda *_args, **_kwargs: ({}, []))
    monkeypatch.setattr(rematerialize, "build_recipe_tools", lambda: ({}, []))
    monkeypatch.setattr(rematerialize, "planner_hooks", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        rematerialize,
        "_with_target_feedback",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(rematerialize, "ClaudeAgentOptions", lambda **_kwargs: object())

    async def fail_session(*_args, **_kwargs) -> None:
        raise AgentSessionFailure("opaque session failure", "session_stalled")

    monkeypatch.setattr(rematerialize, "run_session", fail_session)
    shot = SimpleNamespace(folder=shot_path)
    layer = SimpleNamespace(id="1", judges=())

    async def invoke() -> None:
        await rematerialize._materialize_deferred_layer(
            shot,
            layer,
            model="model",
            blender="blender",
            max_turns=1,
        )

    with pytest.raises(run_artifacts.TypedStop) as raised:
        anyio.run(invoke)

    assert raised.value.code == 3
    assert raised.value.stop_envelope.stage == "materialization"
    assert raised.value.stop_envelope.stop_class == "authority_defect"
    assert [action.transaction_id for action in raised.value.stop_envelope.actions] == [
        "publish_validated_amendment"
    ]
