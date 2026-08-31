from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.materialization_support import attest_exact_materialization_view
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.jit_materialization.errors import (
    MaterializationSelectionConflict,
)

publish = importlib.import_module(
    "vfx_harness.orchestration.jit_materialization.publish"
)


def _write_base_documents(root: Path) -> dict[str, Path]:
    documents = {
        "layers.json": {"schema": 5, "layers": []},
        "scene_checks.json": {"schema": 1, "contracts": []},
        "checks.json": {"schema": 1, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        paths[name] = path
    return paths


def _final_documents(decision_strength: str) -> dict:
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
                                "statement": "The hall reads as the approved reference.",
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


def _mock_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, dict]]:
    (tmp_path / "brief.md").write_text("# fixture brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    bases = _write_base_documents(tmp_path)
    bundle = SimpleNamespace(content_hash="a" * 64)
    materialized = SimpleNamespace(
        layer_row={"id": "look", "execution": "ready"},
        scene_contracts=(),
        image_contracts=(),
        acceptance=(),
    )
    current_documents = {"value": _final_documents("approved_start")}
    pointer = tmp_path / "plans" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.plan-pointer/v2",
                "revision": 1,
                "run_id": "fixture",
                "bundle": (
                    "runs/fixture/checkpoints/plans/bundles/" + bundle.content_hash
                ),
                "content_hash": bundle.content_hash,
                "outcome": "clean_with_deferred",
                "published_at": "2026-09-01T00:00:00+00:00",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        publish,
        "_composed_documents",
        lambda *_args, **_kwargs: (
            bundle,
            materialized,
            bases,
            read_authority_selection_heads(tmp_path).token,
        ),
    )
    monkeypatch.setattr(
        publish,
        "_overlay_documents",
        lambda *_args, **_kwargs: deepcopy(current_documents["value"]),
    )
    monkeypatch.setattr(
        publish,
        "_resolve_authority_from_locked_heads",
        lambda *_args, **_kwargs: SimpleNamespace(
            plan=SimpleNamespace(bundle=bundle),
        ),
    )
    monkeypatch.setattr(
        publish,
        "_require_selected_jit_semantics",
        lambda *_args, **_kwargs: None,
    )
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}\n", encoding="utf-8")
    return candidate, current_documents


def test_publish_view_hash_covers_final_requirement_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, current_documents = _mock_publication(tmp_path, monkeypatch)
    attest_exact_materialization_view(tmp_path, candidate)
    first_pointer_path = publish.publish_materialization(tmp_path, candidate)
    first_pointer = json.loads(first_pointer_path.read_text(encoding="utf-8"))
    first_requirements = tmp_path / first_pointer["artifacts"]["requirements.json"]

    current_documents["value"] = _final_documents("planner_start")
    attest_exact_materialization_view(tmp_path, candidate)
    second_pointer_path = publish.publish_materialization(tmp_path, candidate)
    second_pointer = json.loads(second_pointer_path.read_text(encoding="utf-8"))
    second_requirements = tmp_path / second_pointer["artifacts"]["requirements.json"]

    assert first_pointer["view_hash"] != second_pointer["view_hash"]
    assert first_requirements.parent != second_requirements.parent
    assert (
        json.loads(first_requirements.read_text(encoding="utf-8"))["requirements"][0]
        ["resolution"]["decision_strength"]
        == "approved_start"
    )
    assert (
        json.loads(second_requirements.read_text(encoding="utf-8"))["requirements"][0]
        ["resolution"]["decision_strength"]
        == "planner_start"
    )


def test_materialized_view_is_durable_before_jit_pointer_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, _documents = _mock_publication(tmp_path, monkeypatch)
    attest_exact_materialization_view(tmp_path, candidate)
    events: list[str] = []
    original_install = publish.durably_install_or_flush_view_directory
    original_pointer_parent = publish.durably_ensure_real_directory
    original_pointer_write = publish.durable_replace_pointer_json

    def recording_install(*args: object, **kwargs: object) -> Path:
        result = original_install(*args, **kwargs)  # type: ignore[arg-type]
        events.append("view-durable")
        return result

    def recording_pointer(*args: object, **kwargs: object) -> bytes:
        events.append("pointer-write")
        return original_pointer_write(*args, **kwargs)  # type: ignore[arg-type]

    def recording_pointer_parent(*args: object, **kwargs: object) -> Path:
        result = original_pointer_parent(*args, **kwargs)  # type: ignore[arg-type]
        events.append("pointer-parent-durable")
        return result

    monkeypatch.setattr(
        publish,
        "durably_install_or_flush_view_directory",
        recording_install,
    )
    monkeypatch.setattr(
        publish,
        "durably_ensure_real_directory",
        recording_pointer_parent,
    )
    monkeypatch.setattr(publish, "durable_replace_pointer_json", recording_pointer)

    publish.publish_materialization(tmp_path, candidate)

    assert events == [
        "view-durable",
        "pointer-parent-durable",
        "pointer-write",
    ]


def test_crash_after_materialized_view_rename_requires_orphan_reflush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, _documents = _mock_publication(tmp_path, monkeypatch)
    attest_exact_materialization_view(tmp_path, candidate)
    view_parent = tmp_path / "state" / "jit-layers" / "views"
    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    original_directory_fsync = plan_bundle_integrity._fsync_directory
    original_pointer_write = publish.durable_replace_pointer_json

    def crash_at_rename_parent(path: Path) -> None:
        if path == view_parent:
            raise OSError("injected crash after view rename")
        original_directory_fsync(path)

    def forbidden_pointer(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("pointer write crossed an incomplete view barrier")

    monkeypatch.setattr(
        plan_bundle_integrity,
        "_fsync_directory",
        crash_at_rename_parent,
    )
    monkeypatch.setattr(publish, "durable_replace_pointer_json", forbidden_pointer)

    with pytest.raises(MaterializationSelectionConflict, match="durably install"):
        publish.publish_materialization(tmp_path, candidate)

    assert not pointer.exists()
    orphan_views = tuple(view_parent.iterdir())
    assert len(orphan_views) == 1

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
    monkeypatch.setattr(
        publish,
        "durable_replace_pointer_json",
        original_pointer_write,
    )

    publish.publish_materialization(tmp_path, candidate)

    assert pointer.is_file()
    assert {
        path.relative_to(orphan_views[0]).as_posix() for path in reflushed
    } == {
        "layers.json",
        "scene_checks.json",
        "checks.json",
        "requirements.json",
        "acceptance.json",
    }
