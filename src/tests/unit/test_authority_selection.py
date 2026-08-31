"""Shared semantic selected-authority resolution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection import (
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    CURRENT,
    OVERLAY_ARTIFACTS,
    VIEW_SCHEMA,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import canonical_view_hash
from vfx_harness.orchestration.plan_authority import publish_current


def _write_plan(root: Path, *, marker: str = "fixture") -> None:
    (root / "brief.md").write_text("# fixture brief\n", encoding="utf-8")
    (root / "refs").mkdir(exist_ok=True)
    (root / "refs" / "reference.txt").write_text("reference\n", encoding="utf-8")
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "global.md").write_text(f"# plan {marker}\n", encoding="utf-8")
    documents = {
        "layers.json": {"schema": 5, "layers": []},
        "acceptance.json": [],
        "critic_axes.json": [],
        "checks.json": {"schema": 2, "checks": []},
        "scene_checks.json": {"schema": 2, "contracts": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "obligations.json": {
            "schema": "vfx-harness.obligations/v1",
            "obligations": [],
        },
        "assumptions.json": {
            "schema": "vfx-harness.assumptions/v1",
            "assumptions": [],
        },
    }
    for name, document in documents.items():
        (root / name).write_text(
            json.dumps(document, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _publish_plan(
    root: Path,
    *,
    run_id: str,
    outcome: str = "clean_with_deferred",
) -> None:
    _write_plan(root)
    layout = run_artifacts.create(root, run_id)
    publish_current(root, layout, outcome=outcome)


def _jit_documents() -> dict[str, object]:
    return {
        "layers.json": {
            "schema": 5,
            "layers": [{"id": "form", "execution": "ready"}],
        },
        "scene_checks.json": {"schema": 2, "contracts": []},
        "checks.json": {"schema": 2, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }


def _write_jit_pointer(
    root: Path,
    *,
    bundle_digest: str,
    storage: str = "views",
) -> dict[str, object]:
    documents = _jit_documents()
    view_hash = canonical_view_hash(documents)
    view_root = root / "state" / "jit-layers" / storage / view_hash
    view_root.mkdir(parents=True)
    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in OVERLAY_ARTIFACTS:
        payload = (json.dumps(documents[name], indent=2, sort_keys=True) + "\n").encode()
        path = view_root / name
        path.write_bytes(payload)
        artifacts[name] = path.relative_to(root).as_posix()
        hashes[name] = hashlib.sha256(payload).hexdigest()
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle_digest,
        "view_hash": view_hash,
        "materialized_layers": ["form"],
        "artifacts": artifacts,
        "hashes": hashes,
    }
    pointer_path = root / CURRENT
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return pointer


def test_only_a_truly_missing_global_pointer_is_absent(tmp_path: Path) -> None:
    resolved = resolve_selected_authority(tmp_path)

    assert resolved.assertion.selection == "absent"
    assert resolved.assertion.bundle is None
    assert resolved.assertion.effective_view is None
    assert resolved.pointer_observation.plan_pointer_sha256 is None

    pointer = tmp_path / "plans" / "current.json"
    pointer.parent.mkdir()
    pointer.write_text("{", encoding="utf-8")
    with pytest.raises(SelectedAuthorityResolutionError, match="selected plan authority"):
        resolve_selected_authority(tmp_path)


def test_bundle_backed_selection_is_relocation_and_run_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _publish_plan(first, run_id="plan-run-a")
    _publish_plan(second, run_id="plan-run-b")

    selected_a = resolve_selected_authority(first)
    selected_b = resolve_selected_authority(second)

    assert selected_a.assertion == selected_b.assertion
    assert selected_a.assertion.digest == selected_b.assertion.digest
    assert selected_a.pointer_observation != selected_b.pointer_observation
    assert selected_a.assertion.effective_view is not None
    assert selected_a.assertion.effective_view.source == "bundle"
    assert selected_a.assertion.effective_view.digest == selected_a.assertion.bundle.digest


def test_gate_outcome_changes_semantic_selection_even_for_identical_bundle_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    clean = tmp_path / "clean"
    deferred = tmp_path / "deferred"
    clean.mkdir()
    deferred.mkdir()
    _publish_plan(clean, run_id="clean-run", outcome="clean")
    _publish_plan(
        deferred,
        run_id="deferred-run",
        outcome="clean_with_deferred",
    )

    clean_selection = resolve_selected_authority(clean).assertion
    deferred_selection = resolve_selected_authority(deferred).assertion

    assert clean_selection.bundle is not None
    assert deferred_selection.bundle is not None
    assert clean_selection.bundle.digest == deferred_selection.bundle.digest
    assert clean_selection.digest != deferred_selection.digest


def test_matching_jit_pointer_becomes_the_effective_verified_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _publish_plan(tmp_path, run_id="plan-run")
    bundle_selection = resolve_selected_authority(tmp_path).assertion
    assert bundle_selection.bundle is not None
    pointer = _write_jit_pointer(
        tmp_path,
        bundle_digest=bundle_selection.bundle.digest,
    )

    resolved = resolve_selected_authority(tmp_path)

    assert resolved.assertion.effective_view is not None
    assert resolved.assertion.effective_view.source == "jit"
    assert resolved.assertion.effective_view.digest == pointer["view_hash"]
    assert resolved.assertion.digest != bundle_selection.digest


def test_valid_stale_jit_pointer_is_verified_but_semantically_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _publish_plan(tmp_path, run_id="plan-run")
    before = resolve_selected_authority(tmp_path).assertion
    _write_jit_pointer(tmp_path, bundle_digest="f" * 64)

    after = resolve_selected_authority(tmp_path).assertion

    assert after == before
    assert after.effective_view is not None
    assert after.effective_view.source == "bundle"


def test_malformed_or_symlinked_jit_authority_never_falls_back_to_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _publish_plan(tmp_path, run_id="plan-run")
    selected = resolve_selected_authority(tmp_path).assertion
    assert selected.bundle is not None
    pointer = _write_jit_pointer(tmp_path, bundle_digest=selected.bundle.digest)
    pointer_path = tmp_path / CURRENT
    pointer_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(SelectedAuthorityResolutionError, match="JIT pointer is invalid"):
        resolve_selected_authority(tmp_path)

    pointer_path.write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / str(pointer["artifacts"]["checks.json"])
    original = artifact.with_name("checks.original.json")
    artifact.rename(original)
    artifact.symlink_to(original.name)
    with pytest.raises(SelectedAuthorityResolutionError, match="symlink component"):
        resolve_selected_authority(tmp_path)


def test_duplicate_jit_pointer_or_artifact_keys_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _publish_plan(tmp_path, run_id="plan-run")
    selected = resolve_selected_authority(tmp_path).assertion
    assert selected.bundle is not None
    pointer = _write_jit_pointer(tmp_path, bundle_digest=selected.bundle.digest)
    pointer_path = tmp_path / CURRENT
    pointer_path.write_text(
        '{"schema":"vfx-harness.jit-view/v1",'
        '"schema":"vfx-harness.jit-view/v1"}\n',
        encoding="utf-8",
    )
    with pytest.raises(SelectedAuthorityResolutionError, match="duplicate JSON key 'schema'"):
        resolve_selected_authority(tmp_path)

    pointer_path.write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    layers_path = tmp_path / str(pointer["artifacts"]["layers.json"])
    layers_payload = (
        b'{"schema":5,"layers":[],"layers":'
        b'[{"id":"form","execution":"ready"}]}\n'
    )
    layers_path.write_bytes(layers_payload)
    pointer["hashes"]["layers.json"] = hashlib.sha256(layers_payload).hexdigest()
    documents = _jit_documents()
    documents["layers.json"] = {
        "schema": 5,
        "layers": [{"id": "form", "execution": "ready"}],
    }
    pointer["view_hash"] = canonical_view_hash(documents)
    pointer_path.write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SelectedAuthorityResolutionError, match="duplicate JSON key 'layers'"):
        resolve_selected_authority(tmp_path)


def test_jit_selection_identity_excludes_artifact_locators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _publish_plan(first, run_id="plan-a")
    _publish_plan(second, run_id="plan-b")
    first_bundle = resolve_selected_authority(first).assertion.bundle
    second_bundle = resolve_selected_authority(second).assertion.bundle
    assert first_bundle is not None and second_bundle is not None
    _write_jit_pointer(first, bundle_digest=first_bundle.digest, storage="views-a")
    _write_jit_pointer(second, bundle_digest=second_bundle.digest, storage="views-b")

    first_selection = resolve_selected_authority(first).assertion
    second_selection = resolve_selected_authority(second).assertion

    assert first_selection == second_selection
    assert first_selection.digest == second_selection.digest
