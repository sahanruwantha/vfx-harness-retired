from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import (
    fixture_completion_authorization,
    fixture_live_completion_authority,
    publish_passed_evaluation,
)
from vfx_harness.application import unit_admin
from vfx_harness.domain.work_units import canonical_unit_script_path
from vfx_harness.orchestration import (
    authority_selection_heads,
    selected_authority_guard,
    unit_state_claims,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.unit_state import (
    freeze_checkpoint,
    initialize,
    load,
    transition,
)
from vfx_harness.orchestration.unit_state_claims import (
    claim_ready_unit_for_build,
    claim_ready_unit_for_planning,
    complete_unit_attempt,
    fail_unit_attempt,
)

_EMPTY_TOKEN = AuthoritySelectionToken(0, None, 0, None)


def _claim_building(folder, layer_id, units, unit_id, plan_hash, *, token=_EMPTY_TOKEN):
    eligible = {
        candidate.id
        for candidate in units
        if load(folder, str(layer_id))["units"][candidate.id]["status"] == "passed"
    }
    authorization = fixture_completion_authorization(folder, str(layer_id))
    with fixture_live_completion_authority(authorization):
        planning = claim_ready_unit_for_planning(
            folder,
            str(layer_id),
            unit_id,
            units,
            expected_plan_hash=plan_hash,
            eligible_passed=eligible,
            completion_authorization=authorization,
            run_id="unit-admin-fixture",
            selection_token=token,
            reason="fixture planning claim",
        )
        return claim_ready_unit_for_build(
            folder,
            str(layer_id),
            unit_id,
            units,
            planning,
            expected_plan_hash=plan_hash,
            eligible_passed=eligible,
            completion_authorization=authorization,
            run_id=planning.run_id,
            selection_token=token,
            reason="fixture build claim",
        )


def _fail_building(folder, layer_id, units, unit_id, plan_hash):
    claim = _claim_building(folder, layer_id, units, unit_id, plan_hash)
    fail_unit_attempt(
        folder,
        str(layer_id),
        unit_id,
        units,
        claim,
        expected_plan_hash=plan_hash,
        selection_token=_EMPTY_TOKEN,
        reason="fixture failure",
        evidence=["fixture:failure"],
    )
    return claim


def _pass_building(folder, layer_id, units, unit_id, plan_hash):
    unit = next(candidate for candidate in units if candidate.id == unit_id)
    script = folder / canonical_unit_script_path(str(layer_id), unit_id)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# accepted fixture\n", encoding="utf-8")
    script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
    claim = _claim_building(folder, layer_id, units, unit_id, plan_hash)
    freeze_checkpoint(
        folder,
        str(layer_id),
        unit,
        active_contract_ids=(),
        candidate_hash="missing",
        settings_hash="d" * 64,
        script_hash=script_hash,
        input_hash=plan_hash,
        attempt=claim,
        selection_token=_EMPTY_TOKEN,
    )
    transition(
        folder,
        str(layer_id),
        unit_id,
        "evaluating",
        reason="fixture evaluation",
        attempt=claim,
        selection_token=_EMPTY_TOKEN,
    )
    publish_passed_evaluation(folder, str(layer_id), unit, claim)
    complete_unit_attempt(
        folder,
        str(layer_id),
        unit_id,
        units,
        claim,
        expected_plan_hash=plan_hash,
        selection_token=_EMPTY_TOKEN,
        reason="fixture accepted",
        evidence=["fixture:accepted"],
    )
    return claim


def _select_unit_authority(
    monkeypatch,
    bundle_root,
    *,
    layers_path=None,
    token: AuthoritySelectionToken | None = None,
    layer_capsule_digest: str | None = None,
):
    """Install one exact current-selection snapshot and its unchanged CAS head."""

    selected_token = token or AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256="9" * 64,
        jit_revision=0,
        jit_pointer_sha256=None,
    )
    snapshot = SimpleNamespace(
        plan=SimpleNamespace(
            bundle=SimpleNamespace(
                root=bundle_root,
                content_hash="b" * 64,
            )
        ),
        artifact_paths={"layers.json": layers_path or bundle_root / "layers.json"},
        selection_token=selected_token,
    )
    monkeypatch.setattr(unit_admin, "resolve_selected_authority", lambda folder: snapshot)
    monkeypatch.setattr(
        selected_authority_guard,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=selected_token),
    )
    monkeypatch.setattr(
        authority_selection_heads,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=selected_token),
    )
    monkeypatch.setattr(
        unit_state_claims,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=selected_token),
    )
    monkeypatch.setattr(
        unit_admin,
        "selected_layer_capsule_digest",
        lambda folder, layer_id, selected_authority: (
            layer_capsule_digest
            or hashlib.sha256(snapshot.artifact_paths["layers.json"].read_bytes()).hexdigest()
        ),
    )
    return snapshot


def test_emptied_state_seeds_pending_units_and_preserves_history(tmp_path) -> None:
    """A replan legally empties a layer's unit set, and under unit-first authority that
    is the NORMAL pre-materialization condition — units exist only once the layer
    materializes. Run 20260823T152609Z materialized layer 1 and then failed closed on
    exactly this shape. Seeding destroys nothing: history and supersessions survive."""
    import json as _json
    from pathlib import Path

    state_path = Path(tmp_path) / "state" / "work-units" / "layer_1.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(_json.dumps({
        "schema": 1,
        "layer": "1",
        "plan_hash": "pre-materialization-bundle-hash",
        "revision": 4,
        "units": {},
        "superseded": [{"unit": "old_bootstrap", "reason": "unit-first republication"}],
        "replans": [{"base_bundle": "a" * 64}],
        "updated": "2026-08-23T00:00:00+00:00",
    }), encoding="utf-8")
    units = (_unit("blockout"), _unit("camera", depends_on=["blockout"]))

    value = initialize(tmp_path, "1", units, plan_hash="materialized-view-hash")

    assert set(value["units"]) == {"blockout", "camera"}
    assert all(row["status"] == "pending" for row in value["units"].values())
    assert value["revision"] == 5
    assert value["plan_hash"] == "materialized-view-hash"
    assert value["superseded"][0]["unit"] == "old_bootstrap"
    assert value["replans"][0]["base_bundle"] == "a" * 64
    reloaded = load(tmp_path, "1")
    assert set(reloaded["units"]) == {"blockout", "camera"}


def test_populated_state_still_fails_closed_without_a_replan(tmp_path) -> None:
    initialize(tmp_path, "1", (_unit("blockout"),), plan_hash="one")

    with pytest.raises(
        ValueError,
        match="publish a validated authority replacement or amendment",
    ):
        initialize(tmp_path, "1", (_unit("different_unit"),), plan_hash="two")


def test_initialize_refuses_capsule_change_when_unit_dag_is_unchanged(tmp_path) -> None:
    """Only atomic authority publication may move durable state identity."""
    units = (_unit("blockout"), _unit("camera", depends_on=["blockout"]))
    old_plan_hash = "a" * 64
    new_plan_hash = "b" * 64
    initialize(tmp_path, "1", units, plan_hash=old_plan_hash)
    _pass_building(tmp_path, "1", units, "blockout", old_plan_hash)
    _fail_building(tmp_path, "1", units, "camera", old_plan_hash)

    with pytest.raises(ValueError, match="atomic authority-state coordinator"):
        initialize(tmp_path, "1", units, plan_hash=new_plan_hash)

    unchanged = load(tmp_path, "1")
    assert unchanged["plan_hash"] == old_plan_hash
    assert unchanged["units"]["blockout"]["status"] == "passed"
    assert unchanged["units"]["camera"]["status"] == "failed"
    assert unchanged["replans"] == []


def test_initialize_still_refuses_digest_mismatch_as_replan(tmp_path) -> None:
    initialize(tmp_path, "1", (_unit("blockout"),), plan_hash="one")

    with pytest.raises(ValueError, match="hashes do not match the active layer DAG"):
        initialize(
            tmp_path,
            "1",
            (_unit("blockout", proposition_suffix=" changed"),),
            plan_hash="two",
        )


def test_public_retry_preserves_failed_history_and_reopens_unit(tmp_path, monkeypatch, capsys) -> None:
    units = (_unit("blockout"),)
    layers_path = tmp_path / "layers.json"
    layers_path.write_bytes(b"selected retry layers\n")
    plan_hash = "c" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    _fail_building(tmp_path, "1", units, "blockout", plan_hash)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    _select_unit_authority(
        monkeypatch,
        tmp_path,
        layer_capsule_digest=plan_hash,
    )
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        unit="blockout",
        reason="scope matcher corrected",
        evidence=["run:failed", "test:role-namespace"],
    )

    assert unit_admin._retry(args) == 0

    slot = load(tmp_path, "1")["units"]["blockout"]
    assert slot["status"] == "retryable"
    assert slot["history"][-1]["from"] == "failed"
    assert slot["history"][-1]["metadata"]["evidence"] == args.evidence
    assert "retryable: layer 1 unit blockout" in capsys.readouterr().out


def test_public_retry_reopens_an_interrupted_building_unit(tmp_path, monkeypatch) -> None:
    units = (_unit("blockout"),)
    layers_path = tmp_path / "layers.json"
    layers_path.write_bytes(b"selected retry layers\n")
    plan_hash = hashlib.sha256(layers_path.read_bytes()).hexdigest()
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    selected = _select_unit_authority(monkeypatch, tmp_path)
    _claim_building(
        tmp_path,
        "1",
        units,
        "blockout",
        plan_hash,
        token=selected.selection_token,
    )
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        unit="blockout",
        reason="operator interrupted contaminated session",
        evidence=["run:interrupted"],
    )

    assert unit_admin._retry(args) == 0
    assert load(tmp_path, "1")["units"]["blockout"]["status"] == "retryable"


def test_public_retry_releases_exact_attempt_and_refuses_a_live_builder_fence(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("blockout"),)
    layers_path = tmp_path / "layers.json"
    layers_path.write_bytes(b"selected claimed retry layers\n")
    plan_hash = hashlib.sha256(layers_path.read_bytes()).hexdigest()
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    selected = _select_unit_authority(monkeypatch, tmp_path)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        "blockout",
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="run-claimed-retry",
        selection_token=selected.selection_token,
        reason="test planning claim",
    )
    claim_ready_unit_for_build(
        tmp_path,
        "1",
        "blockout",
        units,
        planning,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="run-claimed-retry",
        selection_token=selected.selection_token,
        reason="test build claim",
    )
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        unit="blockout",
        reason="operator proved the prior process exited",
        evidence=["run:interrupted", "process:exited"],
    )

    assert unit_admin._retry(args) == 0

    slot = load(tmp_path, "1")["units"]["blockout"]
    assert slot["status"] == "retryable"
    assert "active_attempt" not in slot
    assert slot["attempt_history"][-1]["disposition"] == "released"
    assert slot["attempt_history"][-1]["evidence"] == args.evidence
    next_claim = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        "blockout",
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
        completion_authorization=None,
        run_id="run-next-attempt",
        selection_token=selected.selection_token,
        reason="next reviewed attempt",
    )
    with (
        builder_execution_fence(tmp_path),
        pytest.raises(SystemExit, match="live builder execution fence"),
    ):
        unit_admin._retry(args)
    after = load(tmp_path, "1")["units"]["blockout"]
    assert after["status"] == "planning"
    assert after["active_attempt"]["claim_id"] == next_claim.claim_id


def test_public_retry_refuses_exact_head_change_before_state_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("blockout"),)
    layers_path = tmp_path / "layers.json"
    layers_path.write_bytes(b"selected retry layers\n")
    plan_hash = hashlib.sha256(layers_path.read_bytes()).hexdigest()
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    _fail_building(tmp_path, "1", units, "blockout", plan_hash)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    selected = _select_unit_authority(monkeypatch, tmp_path)
    changed = AuthoritySelectionToken(
        plan_revision=selected.selection_token.plan_revision + 2,
        plan_pointer_sha256=selected.selection_token.plan_pointer_sha256,
        jit_revision=selected.selection_token.jit_revision,
        jit_pointer_sha256=selected.selection_token.jit_pointer_sha256,
    )
    monkeypatch.setattr(
        unit_state_claims,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=changed),
    )

    with pytest.raises(SystemExit, match="selected authority changed"):
        unit_admin._retry(
            SimpleNamespace(
                folder=str(tmp_path),
                layer="1",
                unit="blockout",
                reason="retry",
                evidence=["run:failed"],
            )
        )
    assert load(tmp_path, "1")["units"]["blockout"]["status"] == "failed"


def test_public_invalidate_refuses_exact_head_change_before_state_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("blockout"),)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    selected = _select_unit_authority(monkeypatch, tmp_path)
    changed = AuthoritySelectionToken(
        plan_revision=selected.selection_token.plan_revision + 2,
        plan_pointer_sha256=selected.selection_token.plan_pointer_sha256,
        jit_revision=selected.selection_token.jit_revision,
        jit_pointer_sha256=selected.selection_token.jit_pointer_sha256,
    )
    monkeypatch.setattr(
        selected_authority_guard,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=changed),
    )
    monkeypatch.setattr(
        unit_admin,
        "invalidate_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint invalidated after selection changed")
        ),
    )

    with pytest.raises(SystemExit, match="selected authority changed"):
        unit_admin._invalidate(
            SimpleNamespace(
                folder=str(tmp_path),
                layer="1",
                unit="blockout",
                reason="invalid authority",
                evidence=["run:failed"],
            )
        )


def test_public_invalidate_revokes_checkpoint_under_exact_selection(
    tmp_path,
    monkeypatch,
) -> None:
    units = (_unit("blockout"),)
    plan_hash = "a" * 64
    initialize(tmp_path, "1", units, plan_hash=plan_hash)
    _pass_building(tmp_path, "1", units, "blockout", plan_hash)
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot, *, selected_authority=None: {"1": SimpleNamespace(stages=units)},
    )
    _select_unit_authority(monkeypatch, tmp_path)

    result = unit_admin._invalidate(
        SimpleNamespace(
            folder=str(tmp_path),
            layer="1",
            unit="blockout",
            reason="checkpoint contract was invalid",
            evidence=["run:failed"],
        )
    )

    assert result == 0
    slot = load(tmp_path, "1")["units"]["blockout"]
    assert slot["status"] == "retryable"
    assert "checkpoint" not in slot
    assert slot["invalidated_checkpoints"][-1]["evidence"] == ["run:failed"]


def test_cli_retires_replan_action_while_invalidate_and_retry_remain(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(sys, "argv", ["vfx-units", "--help"])
    with pytest.raises(SystemExit) as help_exit:
        unit_admin.main()

    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "{invalidate,retry}" in help_text
    assert "replan" not in help_text

    monkeypatch.setattr(sys, "argv", ["vfx-units", "replan"])
    with pytest.raises(SystemExit) as invalid_exit:
        unit_admin.main()

    assert invalid_exit.value.code == 2
    error_text = capsys.readouterr().err
    assert "invalid choice: 'replan'" in error_text
    assert "{invalidate,retry}" in error_text
