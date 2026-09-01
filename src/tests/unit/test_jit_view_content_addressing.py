from __future__ import annotations

import json
from pathlib import Path

import pytest

from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)
from vfx_harness.orchestration.jit_materialization.proposal import (
    serialized_documents,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    canonical_view_hash,
)
from vfx_harness.orchestration.jit_materialization.view_store import (
    durably_install_or_flush_view_directory,
)


def _view_documents(decision_strength: str) -> dict:
    return {
        "layers.json": {
            "schema": 5,
            "layers": [{"id": "look", "execution": "ready"}],
        },
        "scene_checks.json": {"schema": 1, "contracts": []},
        "checks.json": {"schema": 1, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [
                {
                    "id": "R1",
                    "statement": "The hall reads as the approved reference.",
                    "citation": {
                        "source": "brief.md",
                        "sha256": "b" * 64,
                        "line_start": 1,
                        "line_end": 1,
                    },
                    "resolution": {
                        "kind": "decision",
                        "ids": [],
                        "decision": "The hall reads as the approved reference.",
                        "decision_strength": decision_strength,
                        "evidence_domains": ["human"],
                        "domain_bindings": [
                            {
                                "domain": "human",
                                "kind": "provisional_decision",
                                "statement": (
                                    "The hall reads as the approved reference."
                                ),
                                "decision_strength": decision_strength,
                            }
                        ],
                    },
                }
            ],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }


def _view_root(shot: Path, documents: dict) -> Path:
    return (
        shot
        / "state"
        / "jit-layers"
        / "views"
        / canonical_view_hash(documents)
    )


def test_view_hash_and_store_cover_final_requirement_metadata(tmp_path: Path) -> None:
    first_documents = _view_documents("approved_start")
    second_documents = _view_documents("planner_start")
    first_root = _view_root(tmp_path, first_documents)
    second_root = _view_root(tmp_path, second_documents)

    durably_install_or_flush_view_directory(
        tmp_path,
        first_root,
        serialized_documents(first_documents),
    )
    durably_install_or_flush_view_directory(
        tmp_path,
        second_root,
        serialized_documents(second_documents),
    )

    assert first_root != second_root
    assert first_root.name != second_root.name
    assert (
        json.loads((first_root / "requirements.json").read_text(encoding="utf-8"))[
            "requirements"
        ][0]["resolution"]["decision_strength"]
        == "approved_start"
    )
    assert (
        json.loads((second_root / "requirements.json").read_text(encoding="utf-8"))[
            "requirements"
        ][0]["resolution"]["decision_strength"]
        == "planner_start"
    )


def test_materialized_view_is_durable_before_store_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _view_documents("approved_start")
    view = _view_root(tmp_path, documents)
    members = serialized_documents(documents)
    events: list[str] = []
    original_install = plan_bundle_integrity.durably_install_bundle_directory
    original_verify = plan_bundle_integrity.read_real_file

    def recording_install(*args: object, **kwargs: object) -> Path:
        result = original_install(*args, **kwargs)  # type: ignore[arg-type]
        events.append("view-durable")
        return result

    def recording_read(*args: object, **kwargs: object) -> bytes:
        assert events == ["view-durable"]
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        plan_bundle_integrity,
        "durably_install_bundle_directory",
        recording_install,
    )
    monkeypatch.setattr(plan_bundle_integrity, "read_real_file", recording_read)

    result = durably_install_or_flush_view_directory(tmp_path, view, members)

    assert result == view
    assert events == ["view-durable"]
    assert {path.name for path in view.iterdir()} == set(members)


def test_crash_after_materialized_view_rename_requires_orphan_reflush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _view_documents("approved_start")
    members = serialized_documents(documents)
    view = _view_root(tmp_path, documents)
    view_parent = view.parent
    original_directory_fsync = plan_bundle_integrity._fsync_directory

    def crash_at_rename_parent(path: Path) -> None:
        if path == view_parent:
            raise OSError("injected crash after view rename")
        original_directory_fsync(path)

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        crash_at_rename_parent,
    )

    with pytest.raises(MaterializationSelectionConflict, match="durably install"):
        durably_install_or_flush_view_directory(tmp_path, view, members)

    assert view.is_dir()
    assert tuple(view_parent.iterdir()) == (view,)

    reflushed: list[Path] = []
    original_file_fsync = plan_bundle_integrity._fsync_regular_file

    def recording_file_fsync(path: Path) -> None:
        reflushed.append(path)
        original_file_fsync(path)

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        original_directory_fsync,
    )
    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_regular_file",
        recording_file_fsync,
    )

    durably_install_or_flush_view_directory(tmp_path, view, members)

    assert {path.relative_to(view).as_posix() for path in reflushed} == set(members)
