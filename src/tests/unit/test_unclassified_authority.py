"""Strict revisioned-head semantics for unclassified terminal evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vfx_harness.observability import unclassified_authority
from vfx_harness.orchestration.jit_materialization.view_pointer import canonical_view_hash


def _documents() -> dict[str, object]:
    return {
        "layers.json": {"schema": 5, "layers": []},
        "scene_checks.json": {"schema": 2, "contracts": []},
        "checks.json": {"schema": 2, "checks": []},
        "requirements.json": {"schema": 2, "requirements": []},
        "acceptance.json": [],
    }


def _write_jit(root: Path, *, plan_revision: int, bundle_hash: str) -> None:
    documents = _documents()
    view_hash = canonical_view_hash(documents)
    view = root / "state" / "jit-layers" / "views" / view_hash
    view.mkdir(parents=True)
    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name, document in documents.items():
        path = view / name
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts[name] = path.relative_to(root).as_posix()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    pointer = root / "state" / "jit-layers" / "current.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.jit-layer-view/v2",
                "revision": 4,
                "plan_revision": plan_revision,
                "bundle_hash": bundle_hash,
                "view_hash": view_hash,
                "materialized_layers": [],
                "artifacts": artifacts,
                "hashes": hashes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_legacy_global_pointer_is_invalid_not_absent(tmp_path: Path) -> None:
    pointer = tmp_path / "plans" / "current.json"
    pointer.parent.mkdir()
    pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.plan-pointer/v1",
                "run_id": "publisher",
                "bundle": "runs/publisher/checkpoints/plans/bundles/" + "a" * 64,
                "content_hash": "a" * 64,
                "outcome": "clean",
                "published_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    semantic, _audit, revision = unclassified_authority._selected_bundle(tmp_path)

    assert semantic["selection"] == "invalid"
    assert "pointer_schema_unsupported" in semantic["issues"]
    assert "pointer_revision_invalid" in semantic["issues"]
    assert revision is None


def test_verified_jit_from_old_plan_revision_is_superseded(tmp_path: Path) -> None:
    bundle_hash = "b" * 64
    _write_jit(tmp_path, plan_revision=2, bundle_hash=bundle_hash)

    semantic, _audit = unclassified_authority._selected_view(
        tmp_path,
        {"selection": "verified", "bundle_digest": bundle_hash},
        3,
    )

    assert semantic["selection"] == "superseded"
    assert semantic["observed_bundle_digest"] == bundle_hash


def test_malformed_stale_jit_pointer_is_invalid_not_superseded(tmp_path: Path) -> None:
    pointer = tmp_path / "state" / "jit-layers" / "current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.jit-layer-view/v1",
                "bundle_hash": "c" * 64,
                "view_hash": "d" * 64,
            }
        ),
        encoding="utf-8",
    )

    semantic, _audit = unclassified_authority._selected_view(
        tmp_path,
        {"selection": "verified", "bundle_digest": "b" * 64},
        1,
    )

    assert semantic["selection"] == "invalid"
    assert "pointer_schema_unsupported" in semantic["issues"]
    assert "pointer_fields_mismatch" in semantic["issues"]
