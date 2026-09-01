from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import publish_passed_evaluation
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
    record_hypothesis_falsification,
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
    planning = claim_ready_unit_for_planning(
        folder,
        str(layer_id),
        unit_id,
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=eligible,
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


def _select_replan_authority(
    monkeypatch,
    bundle_root,
    *,
    layers_path=None,
    token: AuthoritySelectionToken | None = None,
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
        unit_admin,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=selected_token),
    )
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

    with pytest.raises(ValueError, match="apply a transactional replan"):
        initialize(tmp_path, "1", (_unit("different_unit"),), plan_hash="two")


def test_initialize_adopts_layers_hash_when_unit_dag_is_unchanged(tmp_path) -> None:
    """Sibling rematerialization rewrites combined layers.json; this layer's units
    can stay identical. Adopting the new hash preserves checkpoint identity, but a
    bare/pass receipt tied to the former plan cannot remain acceptance authority.
    Empty-base replan would have marked every unit added."""
    units = (_unit("blockout"), _unit("camera", depends_on=["blockout"]))
    old_plan_hash = "a" * 64
    new_plan_hash = "b" * 64
    initialize(tmp_path, "1", units, plan_hash=old_plan_hash)
    _pass_building(tmp_path, "1", units, "blockout", old_plan_hash)
    _fail_building(tmp_path, "1", units, "camera", old_plan_hash)

    adopted = initialize(tmp_path, "1", units, plan_hash=new_plan_hash)

    assert adopted["plan_hash"] == new_plan_hash
    assert adopted["units"]["blockout"]["status"] == "retryable"
    assert adopted["units"]["camera"]["status"] == "failed"
    record = adopted["replans"][-1]
    assert record["preserved"] == ["blockout", "camera"]
    assert record["added"] == []
    assert record["changed"] == []
    assert record["owner"] == "vfx-harness.initialize"


def test_initialize_still_refuses_digest_mismatch_as_replan(tmp_path) -> None:
    initialize(tmp_path, "1", (_unit("blockout"),), plan_hash="one")

    with pytest.raises(ValueError, match="hashes do not match the active layer DAG"):
        initialize(
            tmp_path,
            "1",
            (_unit("blockout", proposition_suffix=" changed"),),
            plan_hash="two",
        )


def test_public_replan_moves_state_between_explicit_and_current_bundles(
    tmp_path, monkeypatch, capsys
) -> None:
    old_root = tmp_path / "old-bundle"
    new_root = tmp_path / "new-bundle"
    old_root.mkdir()
    new_root.mkdir()
    (old_root / "layers.json").write_bytes(b"old layers\n")
    (new_root / "layers.json").write_bytes(b"new layers\n")
    old_hash = hashlib.sha256((old_root / "layers.json").read_bytes()).hexdigest()
    old_units = (_unit("blockout"), _unit("camera", depends_on=["blockout"]))
    new_units = (_unit("camera_blockout"), _unit("finish", depends_on=["camera_blockout"]))
    initialize(tmp_path, "1", old_units, plan_hash=old_hash)

    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    _select_replan_authority(monkeypatch, new_root)
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {
            "1": SimpleNamespace(
                stages=new_units if path == new_root / "layers.json" else old_units
            )
        },
    )
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="published DAG amendment",
        evidence=["amendment:one", "gate:new-clean"],
    )

    assert unit_admin._replan(args) == 0

    state = load(tmp_path, "1")
    assert set(state["units"]) == {"camera_blockout", "finish"}
    assert state["revision"] == 2
    assert state["replans"][-1]["removed"] == ["blockout", "camera"]
    assert "replanned layer 1" in capsys.readouterr().out


def test_public_replan_rejects_aba_selection_before_mutating_state(
    tmp_path,
    monkeypatch,
) -> None:
    old_root = tmp_path / "old-bundle"
    current_root = tmp_path / "current-bundle"
    old_root.mkdir()
    current_root.mkdir()
    (old_root / "layers.json").write_bytes(b"old layers\n")
    (current_root / "layers.json").write_bytes(b"current layers\n")
    old_units = (_unit("blockout"),)
    current_units = (_unit("blockout", proposition_suffix=" amended"),)
    old_plan_hash = hashlib.sha256((old_root / "layers.json").read_bytes()).hexdigest()
    initialize(tmp_path, "1", old_units, plan_hash=old_plan_hash)
    state_path = tmp_path / "state" / "work-units" / "layer_1.json"
    state_before = state_path.read_bytes()

    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    selected = _select_replan_authority(monkeypatch, current_root)
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(
            root=old_root,
            content_hash="a" * 64,
        ),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {
            "1": SimpleNamespace(
                stages=(
                    current_units
                    if path == current_root / "layers.json"
                    else old_units
                )
            )
        },
    )
    aba_token = AuthoritySelectionToken(
        plan_revision=selected.selection_token.plan_revision + 2,
        plan_pointer_sha256="8" * 64,
        jit_revision=0,
        jit_pointer_sha256=None,
    )
    monkeypatch.setattr(
        unit_admin,
        "read_authority_selection_heads",
        lambda folder: SimpleNamespace(token=aba_token),
    )

    def unexpected_apply(*args, **kwargs):
        raise AssertionError("replan state mutated before exact selection-token CAS")

    monkeypatch.setattr(unit_admin, "apply_replan", unexpected_apply)
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="semantic A-to-B-to-A authority replacement",
        evidence=["gate:current-clean"],
        preview=False,
        discard_accepted=False,
    )

    with pytest.raises(SystemExit, match="selected authority changed"):
        unit_admin._replan(args)

    assert state_path.read_bytes() == state_before


def _generation_supersession_args(tmp_path, monkeypatch, *, discard_accepted=False, preview=False):
    """Both generations are unit-first (bundle layer DAGs empty); the old units live
    only in durable state, seeded by the superseded generation's materialization."""
    old_root = tmp_path / "old-bundle"
    new_root = tmp_path / "new-bundle"
    old_root.mkdir()
    new_root.mkdir()
    (old_root / "layers.json").write_bytes(b"old deferred layers\n")
    (new_root / "layers.json").write_bytes(b"new deferred layers\n")
    old_units = (_unit("iris_bootstrap"), _unit("iris_detail", depends_on=["iris_bootstrap"]))
    old_plan_hash = "a" * 64
    initialize(tmp_path, "1", old_units, plan_hash=old_plan_hash)
    _pass_building(
        tmp_path,
        "1",
        old_units,
        "iris_bootstrap",
        old_plan_hash,
    )

    monkeypatch.setattr(unit_admin, "load_shot", lambda folder: SimpleNamespace(folder=tmp_path))
    _select_replan_authority(monkeypatch, new_root)
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(unit_admin, "load_layers_from_path", lambda path: {"1": SimpleNamespace(stages=())})
    return SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="generation superseded; clean-slate rebuild",
        evidence=["gate:new-clean"],
        discard_accepted=discard_accepted,
        preview=preview,
    )


def test_generation_supersession_retires_state_units_with_audit(tmp_path, monkeypatch, capsys) -> None:
    """Republication under unit-first authority: both bundles carry empty layer DAGs, so
    the bundle-level diff is blind to the materialized units in durable state. They must
    be superseded WITH audit — and retiring the accepted one is an explicit decision."""
    args = _generation_supersession_args(tmp_path, monkeypatch, preview=True)
    assert unit_admin._replan(args) == 0
    assert "orphaned=iris_bootstrap,iris_detail" in capsys.readouterr().out

    args.preview = False
    with pytest.raises(ValueError, match="discarding proven work"):
        unit_admin._replan(args)

    args.discard_accepted = True
    assert unit_admin._replan(args) == 0
    state = load(tmp_path, "1")
    assert state["units"] == {}
    assert state["plan_hash"] == unit_admin.hashlib.sha256(b"new deferred layers\n").hexdigest()
    archived = {row["id"] for row in state["superseded"]}
    assert {"iris_bootstrap", "iris_detail"} <= archived
    record = state["replans"][-1]
    assert record["orphaned"] == ["iris_bootstrap", "iris_detail"]
    assert record["discard_accepted"] is True


def test_public_retry_preserves_failed_history_and_reopens_unit(tmp_path, monkeypatch, capsys) -> None:
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
    _select_replan_authority(monkeypatch, tmp_path)
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
    selected = _select_replan_authority(monkeypatch, tmp_path)
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
    selected = _select_replan_authority(monkeypatch, tmp_path)
    planning = claim_ready_unit_for_planning(
        tmp_path,
        "1",
        "blockout",
        units,
        expected_plan_hash=plan_hash,
        eligible_passed=set(),
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
    selected = _select_replan_authority(monkeypatch, tmp_path)
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
    selected = _select_replan_authority(monkeypatch, tmp_path)
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
    _select_replan_authority(monkeypatch, tmp_path)

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


def _falsified_replan_fixture(tmp_path, monkeypatch, *, strength: str):
    old_root = tmp_path / "old-bundle"
    new_root = tmp_path / "new-bundle"
    old_root.mkdir()
    new_root.mkdir()
    (old_root / "layers.json").write_bytes(b"old layers\n")
    (new_root / "layers.json").write_bytes(b"new layers\n")
    old_hash = hashlib.sha256((old_root / "layers.json").read_bytes()).hexdigest()
    old_units = (_unit("blockout"),)
    new_units = (_unit("blockout", proposition_suffix=" amended"),)
    initialize(tmp_path, "1", old_units, plan_hash=old_hash)
    claim = _claim_building(tmp_path, "1", old_units, "blockout", old_hash)
    finding = record_hypothesis_falsification(
        tmp_path,
        "1",
        old_units[0],
        old_units,
        bundle_hash="a" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["bbox"],
        observations=[{"contract_id": "bbox", "pass": False}],
        decisions=[{"id": "A-camera", "strength": strength}],
        conflict={
            "kind": "decision",
            "required_authority": "amend camera start",
            "roles": [],
            "controls": [],
        },
        evidence=["runs/run/evidence/bbox.json"],
        attempt=claim,
        selection_token=_EMPTY_TOKEN,
    )
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    _select_replan_authority(monkeypatch, new_root)
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {
            "1": SimpleNamespace(
                stages=new_units if path == new_root / "layers.json" else old_units
            )
        },
    )
    return SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="executable hypothesis falsification",
        evidence=[],
        falsification=finding["record_id"],
        hard_constraint_approval=None,
    ), finding


def _replace_falsification_payload(tmp_path, args, payload) -> None:
    """Replace one synthetic durable finding while preserving its exact slot bind."""

    assert args.falsification == payload["record_id"]
    state_path = tmp_path / "state" / "work-units" / "layer_1.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    matching = [
        index
        for index, row in enumerate(state["falsifications"])
        if row.get("record_id") == payload["record_id"]
    ]
    assert len(matching) == 1
    state["falsifications"][matching[0]] = payload
    state["units"][payload["unit"]]["falsification"] = payload
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_public_replan_consumes_exact_typed_falsification(tmp_path, monkeypatch) -> None:
    args, finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )

    assert unit_admin._replan(args) == 0

    record = load(tmp_path, "1")["replans"][-1]
    assert record["falsification_id"] == finding["record_id"]
    assert record["evidence"] == [
        f"hypothesis-falsification:{finding['record_id']}"
    ]


def test_public_replan_does_not_require_falsification_projection(
    tmp_path, monkeypatch
) -> None:
    args, finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )
    projection = (
        tmp_path
        / "state"
        / "work-units"
        / "hypothesis-falsifications"
        / f"{finding['record_id']}.json"
    )
    projection.unlink()

    assert unit_admin._replan(args) == 0


def test_public_replan_requires_human_evidence_for_hard_constraint(
    tmp_path, monkeypatch
) -> None:
    args, _finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="hard_constraint"
    )

    with pytest.raises(SystemExit, match="hard constraint"):
        unit_admin._replan(args)

    args.hard_constraint_approval = "decisions/user-approved-camera-amendment.json"
    assert unit_admin._replan(args) == 0
    record = load(tmp_path, "1")["replans"][-1]
    assert record["hard_constraint_approval"] == args.hard_constraint_approval


def test_public_replan_reopens_falsified_unit_when_dag_bytes_are_unchanged(
    tmp_path, monkeypatch
) -> None:
    """Consuming a finding is itself the amendment. A same-digest DAG must still
    reopen the falsified unit; preserving hypothesis_falsified would leave the
    layer unbuildable (HIR-0049)."""
    args, finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )
    units = (_unit("blockout"),)
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {"1": SimpleNamespace(stages=units)},
    )

    assert unit_admin._replan(args) == 0

    state = load(tmp_path, "1")
    assert state["units"]["blockout"]["status"] == "pending"
    assert state["replans"][-1]["falsification_id"] == finding["record_id"]
    assert state["replans"][-1]["invalidated"] == ["blockout"]
    assert state["replans"][-1]["preserved"] == []


def test_public_replan_refuses_unchanged_out_of_layer_fault_owner(
    tmp_path, monkeypatch
) -> None:
    args, _finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )
    payload = load(tmp_path, "1")["units"]["blockout"]["falsification"]
    payload["fault_owner_units"] = ["camera_path"]
    _replace_falsification_payload(tmp_path, args, payload)

    camera = _unit("camera_path", script_span="build/units/00/camera_path.py")
    old_mass = _unit("blockout")
    new_mass = _unit("blockout", proposition_suffix=" amended")
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: (
            {
                "0": SimpleNamespace(stages=(camera,)),
                "1": SimpleNamespace(stages=(new_mass,)),
            }
            if path == tmp_path / "new-bundle" / "layers.json"
            else {
                "0": SimpleNamespace(stages=(camera,)),
                "1": SimpleNamespace(stages=(old_mass,)),
            }
        ),
    )

    with pytest.raises(SystemExit, match="out-of-layer fault owner units") as exc:
        unit_admin._replan(args)

    assert "unchanged=camera_path" in str(exc.value)
    assert "do not rerun the identical local DAG" in str(exc.value)


def test_public_replan_accepts_audited_external_owner_supersession(
    tmp_path, monkeypatch
) -> None:
    args, _finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )
    payload = load(tmp_path, "1")["units"]["blockout"]["falsification"]
    payload["fault_owner_units"] = ["camera_path"]
    payload["recorded_at"] = "2026-08-30T12:00:00+00:00"
    _replace_falsification_payload(tmp_path, args, payload)

    new_camera = _unit(
        "camera_path",
        script_span="build/units/00/camera_path.py",
        proposition_suffix=" amended",
    )
    old_mass = _unit("blockout")
    new_mass = _unit("blockout", proposition_suffix=" amended")
    # The explicit sparse base omits the materialized external camera unit.
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: (
            {
                "0": SimpleNamespace(stages=(new_camera,)),
                "1": SimpleNamespace(stages=(new_mass,)),
            }
            if path == tmp_path / "new-bundle" / "layers.json"
            else {
                "0": SimpleNamespace(stages=()),
                "1": SimpleNamespace(stages=(old_mass,)),
            }
        ),
    )
    original_load_state = unit_admin.load_unit_state
    new_camera_hash = unit_admin.unit_digest(new_camera)

    def load_state(folder, layer_id):
        if str(layer_id) == "0":
            return {
                "units": {
                    "camera_path": {
                        "unit_hash": new_camera_hash,
                        "status": "passed",
                    }
                },
                "superseded": [{
                    "id": "camera_path",
                    "unit_hash": "f" * 64,
                    "superseded_at": "2026-08-30T12:01:00+00:00",
                }],
            }
        return original_load_state(folder, layer_id)

    monkeypatch.setattr(unit_admin, "load_unit_state", load_state)

    assert unit_admin._replan(args) == 0


def test_public_replan_consumes_jit_falsification_against_view_identity(
    tmp_path, monkeypatch
) -> None:
    """A JIT layer's finding names the materialized view hash. Comparing that to
    sha256 of the sparse bundle layers.json is a false mismatch; empty-base add
    of every unit would also drop an unrelated passed sibling (HIR-0040, HIR-0049)."""
    old_root = tmp_path / "old-bundle"
    new_root = tmp_path / "new-bundle"
    old_root.mkdir()
    new_root.mkdir()
    (old_root / "layers.json").write_bytes(b"sparse global\n")
    (new_root / "layers.json").write_bytes(b"new layers\n")
    view_hash = hashlib.sha256(b"view layers\n").hexdigest()
    materials = _unit("materials_energy", script_span="build/02/materials_energy.py")
    lighting = _unit(
        "lighting_bloom",
        depends_on=["materials_energy"],
        script_span="build/02/lighting_bloom.py",
    )
    props = _unit("props", script_span="build/02/props.py")
    current_units = (materials, lighting, props)
    initialize(tmp_path, "2", current_units, plan_hash=view_hash)
    _pass_building(tmp_path, "2", current_units, "props", view_hash)
    claim = _claim_building(
        tmp_path,
        "2",
        current_units,
        "materials_energy",
        view_hash,
    )
    finding = record_hypothesis_falsification(
        tmp_path,
        "2",
        materials,
        current_units,
        bundle_hash="a" * 64,
        unit_plan_hash="c" * 64,
        candidate_hash="d" * 64,
        settings_hash="e" * 64,
        contract_ids=["materials-energy-look-f72"],
        observations=[{"classification": "unsatisfiable_in_scope"}],
        decisions=[],
        conflict={
            "kind": "contract",
            "required_authority": "pay the owed image contracts",
            "roles": [],
            "controls": [],
        },
        evidence=["runs/run/evidence/look.json"],
        attempt=claim,
        selection_token=_EMPTY_TOKEN,
    )
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    _select_replan_authority(monkeypatch, new_root)
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {
            "2": SimpleNamespace(
                stages=current_units if path == new_root / "layers.json" else ()
            )
        },
    )
    args = SimpleNamespace(
        folder=str(tmp_path),
        layer="2",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="executable hypothesis falsified",
        evidence=[],
        falsification=finding["record_id"],
        hard_constraint_approval=None,
        preview=False,
        discard_accepted=False,
    )

    assert finding["identities"]["plan_hash"] == view_hash
    bundle_file_hash = hashlib.sha256((old_root / "layers.json").read_bytes()).hexdigest()
    assert finding["identities"]["plan_hash"] != bundle_file_hash
    assert unit_admin._replan(args) == 0

    state = load(tmp_path, "2")
    assert state["units"]["materials_energy"]["status"] == "pending"
    assert state["units"]["lighting_bloom"]["status"] == "pending"
    assert state["units"]["props"]["status"] == "retryable"
    record = state["replans"][-1]
    assert record["falsification_id"] == finding["record_id"]
    assert record["added"] == []
    assert "props" in record["preserved"]
    assert "materials_energy" in record["invalidated"]
    assert "lighting_bloom" in record["invalidated"]
