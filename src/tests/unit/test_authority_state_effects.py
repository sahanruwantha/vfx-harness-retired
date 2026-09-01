from __future__ import annotations

import hashlib
from copy import deepcopy

from tests.unit.test_authority_capsules import (
    _debt,
    _global_documents,
    _materialize,
    _materialize_camera,
)
from vfx_harness.domain.authority_capsules import compile_authority_capsules
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.authority_state_records import (
    AuthorityUnitBinding,
    LayerAuthorityBinding,
)
from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.authority_state_effects import (
    compile_authority_state_effects,
)
from vfx_harness.orchestration.unit_state_identity import DIGEST_SCHEMA, unit_digest


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _token(jit_revision: int) -> AuthoritySelectionTokenProjection:
    return AuthoritySelectionTokenProjection(
        plan_revision=1,
        plan_pointer_sha256=_digest("plan"),
        jit_revision=jit_revision,
        jit_pointer_sha256=None if jit_revision == 0 else _digest(f"jit-{jit_revision}"),
    )


def _unit_from_capsule(capsules, layer_id: str, unit_id: str) -> WorkUnit:
    return WorkUnit.parse(
        capsules.unit(layer_id, unit_id).projection["work_unit"]["row"],
        f"fixture {layer_id}.{unit_id}",
    )


def _pending_state(capsules, layer_id: str, unit_id: str) -> dict:
    unit = _unit_from_capsule(capsules, layer_id, unit_id)
    return {
        "schema": 1,
        "digest_schema": DIGEST_SCHEMA,
        "layer": layer_id,
        "plan_hash": capsules.layer(layer_id).capsule_digest,
        "revision": 1,
        "units": {
            unit_id: {
                "status": "pending",
                "unit_hash": unit_digest(unit),
                "updated": "2026-09-01T00:00:00Z",
                "history": [],
            }
        },
        "superseded": [],
        "replans": [],
        "attempt_lineage": {},
        "updated": "2026-09-01T00:00:00Z",
    }


def _binding(capsules, layer_id: str, unit_id: str, token) -> LayerAuthorityBinding:
    return LayerAuthorityBinding.mint(
        transition_revision=1,
        transition_proposal_digest=_digest("proposal-1"),
        selection_token=token,
        layer_id=layer_id,
        layer_generation_digest=capsules.layer(layer_id).capsule_digest,
        units=[
            AuthorityUnitBinding.mint(
                unit_id=unit_id,
                unit_generation_digest=capsules.unit(layer_id, unit_id).capsule_digest,
            )
        ],
    )


def _camera_capsules():
    global_documents = _global_documents()
    camera_view = deepcopy(global_documents)
    _materialize_camera(camera_view, _debt())
    return global_documents, camera_view, compile_authority_capsules(
        global_documents,
        camera_view,
    )


def test_first_materialized_layer_creates_bound_pending_state() -> None:
    global_documents, camera_view, after = _camera_capsules()
    before = compile_authority_capsules(global_documents, global_documents)

    projection = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=after,
        states={},
        prior_bindings={},
        predecessor_head_revision=1,
        before_selection_token=_token(0),
        at="2026-09-01T01:00:00Z",
    )

    assert [row.layer_id for row in projection.layers] == ["1"]
    row = projection.layers[0]
    assert row.effect.effect_kind == "added"
    assert row.after_state["plan_hash"] == after.layer("1").capsule_digest
    assert row.after_state["units"]["camera_unit"]["status"] == "pending"
    assert camera_view["layers.json"]["layers"][0]["execution"] == "ready"


def test_later_payer_preserves_earlier_state_and_adds_only_payer() -> None:
    global_documents, camera_view, before = _camera_capsules()
    payer_view = deepcopy(camera_view)
    _materialize(
        payer_view,
        layer_id="2",
        unit_id="form_unit",
        axis="form",
        role="hall.mass",
        contract_id="hall-count",
        requirement_id="R-form",
    )
    after = compile_authority_capsules(global_documents, payer_view)
    token = _token(1)
    state = _pending_state(before, "1", "camera_unit")

    projection = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=after,
        states={"1": state},
        prior_bindings={"1": _binding(before, "1", "camera_unit", token)},
        predecessor_head_revision=1,
        before_selection_token=token,
        at="2026-09-01T01:00:00Z",
    )

    by_layer = {row.layer_id: row for row in projection.layers}
    assert by_layer["1"].effect.effect_kind == "unchanged"
    assert by_layer["1"].after_state == state
    assert by_layer["2"].effect.effect_kind == "added"
    assert by_layer["2"].after_predecessor_ids == ("1",)


def test_selection_change_revokes_active_attempt_without_invalidating_capsule() -> None:
    _global, _view, capsules = _camera_capsules()
    token = _token(1)
    state = _pending_state(capsules, "1", "camera_unit")
    unit = _unit_from_capsule(capsules, "1", "camera_unit")
    claim = UnitAttemptClaim.mint(
        attempt_revision=1,
        run_id="fixture-active-attempt",
        layer_id="1",
        unit_id=unit.id,
        unit_digest=unit_digest(unit),
        plan_hash=capsules.layer("1").capsule_digest,
        selection_token={
            "schema": "vfx-harness.authority-selection-token/v1",
            "plan_revision": token.plan_revision,
            "plan_pointer_sha256": token.plan_pointer_sha256,
            "jit_revision": token.jit_revision,
            "jit_pointer_sha256": token.jit_pointer_sha256,
        },
        phase="planning",
        at="2026-09-01T00:30:00Z",
    )
    slot = state["units"][unit.id]
    slot.update(
        status="planning",
        attempt_revision=1,
        active_attempt=claim.as_dict(),
    )
    state["attempt_lineage"] = {unit.id: 1}

    projection = compile_authority_state_effects(
        before_capsules=capsules,
        after_capsules=capsules,
        states={"1": state},
        prior_bindings={"1": _binding(capsules, "1", unit.id, token)},
        predecessor_head_revision=1,
        before_selection_token=token,
        at="2026-09-01T01:00:00Z",
    )

    row = projection.layers[0]
    assert row.effect.revoked_unit_attempt_claim_ids == (claim.claim_id,)
    assert row.after_state["units"][unit.id]["status"] == "retryable"
    assert "active_attempt" not in row.after_state["units"][unit.id]
    assert row.after_state["revision"] == 2


def test_changed_unit_reopens_only_its_same_layer_downstream_closure() -> None:
    _global, view, before = _camera_capsules()
    changed_view = deepcopy(view)
    changed_view["layers.json"]["layers"][0]["stages"][0]["title"] = (
        "Changed camera unit"
    )
    after = compile_authority_capsules(_global, changed_view)
    token = _token(1)
    state = _pending_state(before, "1", "camera_unit")

    projection = compile_authority_state_effects(
        before_capsules=before,
        after_capsules=after,
        states={"1": state},
        prior_bindings={"1": _binding(before, "1", "camera_unit", token)},
        predecessor_head_revision=1,
        before_selection_token=token,
        at="2026-09-01T01:00:00Z",
    )

    row = projection.layers[0]
    assert row.effect.effect_kind == "changed"
    assert row.effect.invalidation_seed_unit_ids == ("camera_unit",)
    assert row.effect.invalidated_unit_ids == ("camera_unit",)
    assert row.after_state["units"]["camera_unit"]["status"] == "pending"
    assert row.after_state["plan_hash"] == after.layer("1").capsule_digest
