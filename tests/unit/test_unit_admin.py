from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from vfx_harness.application import unit_admin
from vfx_harness.orchestration.unit_state import (
    initialize,
    load,
    record_hypothesis_falsification,
    transition,
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
    monkeypatch.setattr(
        unit_admin,
        "resolve_current",
        lambda folder: SimpleNamespace(root=new_root, content_hash="b" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot: {"1": SimpleNamespace(stages=new_units)},
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {"1": SimpleNamespace(stages=old_units)},
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


def test_public_retry_preserves_failed_history_and_reopens_unit(tmp_path, monkeypatch, capsys) -> None:
    units = (_unit("blockout"),)
    initialize(tmp_path, "1", units, plan_hash="plan")
    transition(tmp_path, "1", "blockout", "planning", reason="ready")
    transition(tmp_path, "1", "blockout", "building", reason="started")
    transition(tmp_path, "1", "blockout", "failed", reason="scope audit")
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot: {"1": SimpleNamespace(stages=units)},
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
    initialize(tmp_path, "1", units, plan_hash="plan")
    transition(tmp_path, "1", "blockout", "planning", reason="ready")
    transition(tmp_path, "1", "blockout", "building", reason="started")
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot: {"1": SimpleNamespace(stages=units)},
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
    transition(tmp_path, "1", "blockout", "planning", reason="ready")
    transition(tmp_path, "1", "blockout", "building", reason="started")
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
    )
    monkeypatch.setattr(
        unit_admin,
        "load_shot",
        lambda folder: SimpleNamespace(folder=tmp_path),
    )
    monkeypatch.setattr(
        unit_admin,
        "resolve_current",
        lambda folder: SimpleNamespace(root=new_root, content_hash="b" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "resolve_published_bundle",
        lambda folder, **kwargs: SimpleNamespace(root=old_root, content_hash="a" * 64),
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers",
        lambda shot: {"1": SimpleNamespace(stages=new_units)},
    )
    monkeypatch.setattr(
        unit_admin,
        "load_layers_from_path",
        lambda path: {"1": SimpleNamespace(stages=old_units)},
    )
    relative = (
        "state/work-units/hypothesis-falsifications/"
        f"{finding['record_id']}.json"
    )
    return SimpleNamespace(
        folder=str(tmp_path),
        layer="1",
        base_run="old-run",
        base_bundle="a" * 64,
        owner="operator",
        trigger="executable hypothesis falsification",
        evidence=[],
        falsification=relative,
        hard_constraint_approval=None,
    ), finding


def test_public_replan_consumes_exact_typed_falsification(tmp_path, monkeypatch) -> None:
    args, finding = _falsified_replan_fixture(
        tmp_path, monkeypatch, strength="approved_start"
    )

    assert unit_admin._replan(args) == 0

    record = load(tmp_path, "1")["replans"][-1]
    assert record["falsification_id"] == finding["record_id"]
    assert record["evidence"] == [args.falsification]


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
