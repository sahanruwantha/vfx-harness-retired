"""Filesystem and identity boundaries for unpublished candidate previews."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.plan_consumer_view_fixtures import registered_consumer_view
from vfx_harness.domain.authority_head_records import (
    canonical_view_hash,
)
from vfx_harness.domain.authority_preview_records import (
    AUTHORITY_PREVIEW_REFERENCE_PATH,
)
from vfx_harness.orchestration.jit_materialization.candidate_preview import (
    project_candidate_authority_state,
    stage_candidate_publication_view,
)
from vfx_harness.orchestration.jit_materialization.schema import OVERLAY_ARTIFACTS
from vfx_harness.orchestration.jit_materialization.transition import (
    PreparedMaterializationPublication,
)
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationConflict,
    mutating_plan_consumer_view,
)


def _overlay_payloads() -> tuple[str, dict[str, bytes]]:
    documents: dict[str, Any] = {
        "layers.json": {"layers": [], "schema": 5},
        "scene_checks.json": {"contracts": [], "schema": 2},
        "checks.json": {"checks": [], "schema": 2},
        "requirements.json": {
            "requirements": [],
            "schema": "vfx-harness.requirements/v2",
        },
        "acceptance.json": [],
    }
    payloads = {
        name: (json.dumps(documents[name], indent=2, sort_keys=True) + "\n").encode("utf-8")
        for name in OVERLAY_ARTIFACTS
    }
    return canonical_view_hash(documents), payloads


def _state_payload(layer_id: str) -> bytes:
    return (
        json.dumps(
            {"layer": layer_id, "schema": 1, "units": {}},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _publication(
    *,
    capsule_layer_ids: tuple[str, ...],
    before_hashes: dict[str, str],
    after_hashes: dict[str, str],
    after_payloads: dict[str, bytes],
    transition: object | None = None,
) -> PreparedMaterializationPublication:
    capsules = SimpleNamespace(layers=tuple(SimpleNamespace(layer_id=layer_id) for layer_id in capsule_layer_ids))
    if transition is not None:
        transition.capsule_set = capsules
    return PreparedMaterializationPublication(
        pointer=None,  # type: ignore[arg-type]
        pointer_payload=b"",
        capsule_set=capsules,  # type: ignore[arg-type]
        transition=transition,  # type: ignore[arg-type]
        authority_state_head_ref=None,  # type: ignore[arg-type]
        before_state_hashes=before_hashes,
        after_state_hashes=after_hashes,
        after_state_payloads=after_payloads,
    )


def test_stage_candidate_view_rejects_path_shaped_hash_before_writing(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    _view_hash, payloads = _overlay_payloads()

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="lowercase SHA-256"),
    ):
        stage_candidate_publication_view(capability, "../../escaped", payloads)

    assert not (view / "state").exists()
    assert not (tmp_path / "escaped").exists()


def test_stage_candidate_view_rejects_non_finite_json_before_writing(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    view_hash, payloads = _overlay_payloads()
    payloads["requirements.json"] = (
        b'{\n  "requirements": NaN,\n'
        b'  "schema": "vfx-harness.requirements/v2"\n}\n'
    )

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="non-finite number"),
    ):
        stage_candidate_publication_view(capability, view_hash, payloads)

    assert not (view / "state").exists()


def test_stage_candidate_view_rejects_symlinked_storage_ancestor(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (view / "state").symlink_to(outside, target_is_directory=True)
    view_hash, payloads = _overlay_payloads()

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(PlanConsumerViewMutationConflict),
    ):
        stage_candidate_publication_view(capability, view_hash, payloads)

    assert list(outside.iterdir()) == []


def test_stage_candidate_view_refuses_a_symlinked_alias_of_the_installed_view(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(view.parent, target_is_directory=True)
    view_hash, payloads = _overlay_payloads()

    with (
        pytest.raises(PlanConsumerViewMutationConflict),
        mutating_plan_consumer_view(shot, linked_parent / view.name, marker) as capability,
    ):
        stage_candidate_publication_view(capability, view_hash, payloads)

    assert not (view / "state").exists()


def test_stage_candidate_view_is_install_or_verify_and_refuses_conflict(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    view_hash, payloads = _overlay_payloads()

    with mutating_plan_consumer_view(shot, view, marker) as capability:
        root = stage_candidate_publication_view(capability, view_hash, payloads)
        assert stage_candidate_publication_view(capability, view_hash, payloads) == root
        conflict = root / "layers.json"
        conflict.write_bytes(b"{}\n")

        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match=r"differ|conflict",
        ):
            stage_candidate_publication_view(capability, view_hash, payloads)

    assert conflict.read_bytes() == b"{}\n"


def test_project_candidate_state_rejects_forged_layer_before_writing(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    forged_layer = "x/../../../../escaped"
    payload = _state_payload(forged_layer)
    payload_hash = hashlib.sha256(payload).hexdigest()
    publication = _publication(
        capsule_layer_ids=("safe",),
        before_hashes={forged_layer: payload_hash},
        after_hashes={forged_layer: payload_hash},
        after_payloads={forged_layer: payload},
    )

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="canonical layer identifier"),
    ):
        project_candidate_authority_state(capability, publication)

    assert not (view / "state").exists()
    assert not (tmp_path / "escaped.json").exists()


def test_project_candidate_state_requires_exact_transition_member_keyset(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    payload = _state_payload("safe")
    payload_hash = hashlib.sha256(payload).hexdigest()
    transition = SimpleNamespace(
        intent=SimpleNamespace(
            state_members=(),
            proposal=SimpleNamespace(effects=()),
        ),
        effects_projection=SimpleNamespace(layers=()),
        after_state_payloads={},
    )
    publication = _publication(
        capsule_layer_ids=("safe",),
        before_hashes={},
        after_hashes={"safe": payload_hash},
        after_payloads={"safe": payload},
        transition=transition,
    )

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="prepared transition"),
    ):
        project_candidate_authority_state(capability, publication)

    assert not (view / "state").exists()


def test_project_candidate_state_rejects_symlinked_state_namespace(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    outside = tmp_path / "outside"
    (view / "state").mkdir()
    outside.mkdir()
    (view / "state" / "work-units").symlink_to(
        outside,
        target_is_directory=True,
    )
    publication = _publication(
        capsule_layer_ids=("deferred",),
        before_hashes={},
        after_hashes={},
        after_payloads={},
    )

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="isolated real directory"),
    ):
        project_candidate_authority_state(capability, publication)

    assert list(outside.iterdir()) == []


def test_project_candidate_state_refuses_predecessor_conflict_without_overwrite(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    state_dir = view / "state" / "work-units"
    state_dir.mkdir(parents=True)
    expected_payload = _state_payload("safe")
    expected_hash = hashlib.sha256(expected_payload).hexdigest()
    conflict = state_dir / "layer_safe.json"
    conflict.write_bytes(b"conflicting predecessor\n")
    publication = _publication(
        capsule_layer_ids=("safe",),
        before_hashes={"safe": expected_hash},
        after_hashes={"safe": expected_hash},
        after_payloads={"safe": expected_payload},
    )

    with (
        mutating_plan_consumer_view(shot, view, marker) as capability,
        pytest.raises(ValueError, match="predecessor hash changed"),
    ):
        project_candidate_authority_state(capability, publication)

    assert conflict.read_bytes() == b"conflicting predecessor\n"


def test_zero_ready_noop_projects_an_exact_empty_namespace(tmp_path: Path) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    reference_path = view / AUTHORITY_PREVIEW_REFERENCE_PATH
    reference_path.write_text("stale preview", encoding="utf-8")
    publication = _publication(
        capsule_layer_ids=("deferred",),
        before_hashes={},
        after_hashes={},
        after_payloads={},
    )

    with mutating_plan_consumer_view(shot, view, marker) as capability:
        project_candidate_authority_state(capability, publication)

    state = view / "state" / "work-units"
    assert state.is_dir()
    assert not state.is_symlink()
    assert list(state.iterdir()) == []
    assert not reference_path.exists()
