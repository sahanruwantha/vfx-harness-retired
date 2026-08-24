"""A metric may only close a claim it can actually support.

Run 20260823T154920Z certified two claims with metrics that cannot support them:
"warning lights chase around the rim" by an `object_count` of 24 (a count has no time in
it), and "the seal reads as layered machined metal" by radial closure (geometry cannot
see appearance). Both metrics measured correctly; both claims were unproven.

Detection is typed, never prose-scanned: a claim declares the evidence domain its
proposition lives in, and the harness checks that against what each bound metric can
certify."""

from __future__ import annotations

import json

import pytest

from vfx_harness.evidence.scene_checks import KIND_DOMAINS
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import validate_materialization
from vfx_harness.orchestration.plan_authority import publish_current

from .test_plan_records import _add_deferred_layer, _candidate, _jit_payload, _write


def _materialize(tmp_path, mutate) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "proxy-evidence")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)
    data = json.loads(payload.read_text(encoding="utf-8"))
    mutate(data)
    _write(payload, data)
    validate_materialization(
        bundle.root, payload, expected_bundle_hash=bundle.content_hash
    )


def test_metric_domains_separate_existence_from_behaviour() -> None:
    assert KIND_DOMAINS["object_count"] == "scene"
    assert KIND_DOMAINS["onset_order"] == "temporal"
    assert KIND_DOMAINS["radial_inward_fraction"] == "scene"
    assert KIND_DOMAINS["frame_delta"] == "image"
    assert KIND_DOMAINS["bbox_width"] == "projected_composition"


def test_count_cannot_certify_a_temporal_claim(tmp_path, monkeypatch) -> None:
    """The chase proxy: 24 modules exist, therefore they chase."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    def as_count_backed_temporal_claim(data: dict) -> None:
        contract = data["scene_contracts"][0]
        contract["kind"] = "object_count"
        contract["op"] = "eq"
        contract["value"] = 24
        contract["roles"] = ["polish.comp"]
        for key in ("frames", "hi", "region"):
            contract.pop(key, None)
        claim = data["layer"]["stages"][0]["evaluation"]["claims"][0]
        claim["asserts"] = "temporal"

    with pytest.raises(ValueError, match="asserts 'temporal'"):
        _materialize(tmp_path, as_count_backed_temporal_claim)


def test_matching_domain_is_accepted(tmp_path, monkeypatch) -> None:
    """The same claim, honestly evidenced, passes — the rule discriminates rather
    than blanket-banning count evidence."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    def as_count_backed_scene_claim(data: dict) -> None:
        contract = data["scene_contracts"][0]
        contract["kind"] = "object_count"
        contract["op"] = "eq"
        contract["value"] = 24
        contract["roles"] = ["polish.comp"]
        for key in ("frames", "hi", "region"):
            contract.pop(key, None)
        claim = data["layer"]["stages"][0]["evaluation"]["claims"][0]
        claim["asserts"] = "scene"

    _materialize(tmp_path, as_count_backed_scene_claim)  # must not raise


def test_required_claims_must_declare_their_domain(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    def drop_declaration(data: dict) -> None:
        data["layer"]["stages"][0]["evaluation"]["claims"][0].pop("asserts", None)

    with pytest.raises(ValueError, match="must declare `asserts`"):
        _materialize(tmp_path, drop_declaration)


def test_appearance_ownership_demands_candidate_bound_image_evidence() -> None:
    """The readability proxy: geometry cannot certify how something reads. A unit that
    declares look ownership must produce image evidence before it may seal, even when
    it binds no image contract at materialization (none can exist there)."""
    from vfx_harness.agents.build_prompts import capability_feedback_groups
    from vfx_harness.agents.builder import image_evidence_required_for

    # The unit that failed: no image binding could exist at materialization, and before
    # this rule its declared appearance ownership demanded nothing at build time either.
    assert image_evidence_required_for(set(), ("material",)) is True
    assert image_evidence_required_for(set(), ()) is False
    assert image_evidence_required_for({"img-1"}, ()) is True
    assert capability_feedback_groups(("material",)) >= {"detail", "color"}


def _mesh_case(tmp_path, *, contract_roles, geometry_provider: bool):
    """Layer with a camera unit and an iris-style unit; the mesh contract targets
    `contract_roles`. Mirrors run 20260824T060927Z, where smooth_fraction was bound to
    a camera rig that will never have polygons."""
    def mutate(data: dict) -> None:
        stage = data["layer"]["stages"][0]
        stage["provides"] = ["geometry"] if geometry_provider else ["camera"]
        contract = data["scene_contracts"][0]
        contract["kind"] = "smooth_fraction"
        contract["op"] = "max"
        contract["hi"] = 0.02
        contract["roles"] = contract_roles
        for key in ("frames", "region", "lo", "value"):
            contract.pop(key, None)
        data["layer"]["stages"][0]["evaluation"]["claims"][0]["asserts"] = "scene"

    return mutate


def test_mesh_metric_on_a_camera_rig_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    with pytest.raises(ValueError, match="can only read None"):
        _materialize(tmp_path, _mesh_case(
            tmp_path, contract_roles=["polish.comp"], geometry_provider=False
        ))


def test_mesh_metric_on_declared_geometry_is_accepted(tmp_path, monkeypatch) -> None:
    """The guard must discriminate, not reject every mesh metric."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _materialize(tmp_path, _mesh_case(
        tmp_path, contract_roles=["polish.comp"], geometry_provider=True
    ))


def test_legacy_units_declaring_nothing_are_not_judged(tmp_path, monkeypatch) -> None:
    """A unit that never had the chance to declare must not fail on the declaration."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    def mutate(data: dict) -> None:
        contract = data["scene_contracts"][0]
        contract["kind"] = "smooth_fraction"
        contract["op"] = "max"
        contract["hi"] = 0.02
        contract["roles"] = ["polish.comp"]
        for key in ("frames", "region", "lo", "value"):
            contract.pop(key, None)
        data["layer"]["stages"][0]["evaluation"]["claims"][0]["asserts"] = "scene"

    _materialize(tmp_path, mutate)  # no provides anywhere -> unjudged
