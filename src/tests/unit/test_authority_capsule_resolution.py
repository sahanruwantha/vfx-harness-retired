from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.unit.test_authority_capsules import (
    _debt,
    _global_documents,
    _materialize_camera,
)
from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    JIT_VIEW_POINTER_SCHEMA,
    OVERLAY_ARTIFACTS,
)
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import authority_state_transaction
from vfx_harness.orchestration.authority_capsule_resolution import (
    AuthorityCapsuleResolutionError,
    capture_selected_authority_capsules,
    compile_proposed_authority_capsules,
)
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.jit_materialization.view_pointer import canonical_view_hash
from vfx_harness.orchestration.plan_authority import publish_current


@pytest.fixture(autouse=True)
def _below_plan_gate_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """These layer documents sit below the plan gate, so the accepted-build projection
    that republication derives from gate-valid layer authority is stubbed here; the
    public pipeline fixtures cover it (HIR-0172)."""

    monkeypatch.setattr(
        authority_state_transaction,
        "republish_accepted_build_index",
        lambda *_args, **_kwargs: "current",
    )



def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish_sparse_plan(root: Path):
    (root / "brief.md").write_text("Capsule resolver fixture.\n", encoding="utf-8")
    (root / "refs").mkdir()
    (root / "refs" / "reference.png").write_bytes(b"fixture reference")
    (root / "plans").mkdir()
    (root / "plans" / "global.md").write_text("# Capsule fixture\n", encoding="utf-8")
    for name, document in _global_documents().items():
        _write(root / name, document)
    _write(
        root / "critic_axes.json",
        [
            {"key": "camera", "desc": "camera"},
            {"key": "form", "desc": "form"},
            {"key": "prop", "desc": "prop"},
        ],
    )
    _write(
        root / "obligations.json",
        {"schema": "vfx-harness.obligations/v1", "obligations": []},
    )
    _write(
        root / "assumptions.json",
        {"schema": "vfx-harness.assumptions/v1", "assumptions": []},
    )
    return publish_current(
        root,
        run_artifacts.create(root, "capsule-publisher"),
        outcome="clean_with_deferred",
    )


def _select_jit_view(root: Path, bundle, documents: dict[str, object]) -> None:
    view_hash = canonical_view_hash(documents)
    view_root = root / "state" / "jit-layers" / "views" / view_hash
    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in OVERLAY_ARTIFACTS:
        path = view_root / name
        _write(path, documents[name])
        artifacts[name] = path.relative_to(root).as_posix()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    plan_pointer = json.loads((root / "plans" / "current.json").read_text(encoding="utf-8"))
    _write(
        root / JIT_CURRENT_PATH,
        {
            "schema": JIT_VIEW_POINTER_SCHEMA,
            "revision": 1,
            "plan_revision": plan_pointer["revision"],
            "bundle_hash": bundle.content_hash,
            "view_hash": view_hash,
            "materialized_layers": [
                str(row["id"])
                for row in documents["layers.json"]["layers"]
                if row.get("execution") == "ready"
            ],
            "artifacts": artifacts,
            "hashes": hashes,
        },
    )


def test_resolver_compiles_real_selected_sparse_and_jit_sources(tmp_path: Path) -> None:
    bundle = _publish_sparse_plan(tmp_path)
    proposed = deepcopy(_global_documents())
    _materialize_camera(proposed, _debt())

    selected_sparse = resolve_selected_authority(tmp_path)
    proposed_capture = compile_proposed_authority_capsules(
        tmp_path,
        selected_sparse,
        proposed,
    )
    assert proposed_capture.capsule_set.unit("1", "camera_unit").projection[
        "judgment_debt_definitions"
    ]

    _select_jit_view(tmp_path, bundle, proposed)
    selected_jit = resolve_selected_authority(tmp_path)
    captured = capture_selected_authority_capsules(tmp_path, selected_jit)

    assert captured.capsule_set.as_dict() == proposed_capture.capsule_set.as_dict()
    assert len(captured.source_bindings) == len(OVERLAY_ARTIFACTS) * 2
    captured.require_sources_unchanged()


def test_resolver_rechecks_immutable_source_bindings(tmp_path: Path) -> None:
    _publish_sparse_plan(tmp_path)
    selected = resolve_selected_authority(tmp_path)
    captured = capture_selected_authority_capsules(tmp_path, selected)
    source = captured.source_bindings[0].path
    source.write_bytes(source.read_bytes() + b" ")

    with pytest.raises(AuthorityCapsuleResolutionError, match="trusted path changed"):
        captured.require_sources_unchanged()
