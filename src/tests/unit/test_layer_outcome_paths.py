from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.layer_outcome_fixtures import write_test_layer_outcome
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.layer_outcome_paths import (
    layer_outcome_locator,
    layer_outcome_path,
)


def test_layer_outcome_locators_are_injective_and_cannot_traverse(tmp_path: Path) -> None:
    layer_ids = ("1", "01", "camera", "CAMERA", "../camera", "camera/hero", "cámara")
    locators = [layer_outcome_locator(layer_id) for layer_id in layer_ids]

    assert len(locators) == len(set(locators))
    for layer_id, locator in zip(layer_ids, locators, strict=True):
        path = layer_outcome_path(tmp_path, layer_id)
        assert path.is_relative_to(tmp_path.resolve())
        assert path.parent == (tmp_path / "plans" / "outcomes").resolve()
        assert path.relative_to(tmp_path.resolve()).as_posix() == locator
        assert "/../" not in f"/{locator}/"


@pytest.mark.parametrize("layer_id", ["", " camera", "camera ", "\t"])
def test_layer_outcome_locator_rejects_noncanonical_ids(layer_id: str) -> None:
    with pytest.raises(ValueError, match="non-empty trimmed"):
        layer_outcome_locator(layer_id)


def test_named_layer_outcome_round_trips_through_all_layer_plan_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "refs" / "M1.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"fixture reference")
    manifest = {
        "blender_version": "5.2",
        "comparison": {"mode": "eevee", "scale": 0.5},
    }
    monkeypatch.setattr(revalidation, "input_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(
        revalidation,
        "canonical_records",
        lambda *_args, **_kwargs: [
            {
                "evidence_kind": "executable_only",
                "frame": 1,
                "ref": "refs/M1.png",
                "ref_sha256": hashlib.sha256(b"fixture reference").hexdigest(),
                "input_manifest_sha256": canonical_digest(manifest),
                "authoritative": [
                    {
                        "id": "fixture-contract-0",
                        "metric": "fixture",
                        "value": 1,
                        "target": "= 1",
                        "pass": True,
                        "source": "scene_contract",
                        "owner_layer": "camera.hero",
                        "fault_owner": "camera.hero",
                        "activates_at": None,
                        "lifecycle": None,
                    }
                ],
                "authoritative_sha256": canonical_digest(
                    {
                        "authoritative": [
                            {
                                "id": "fixture-contract-0",
                                "metric": "fixture",
                                "value": 1,
                                "target": "= 1",
                                "pass": True,
                                "source": "scene_contract",
                                "owner_layer": "camera.hero",
                                "fault_owner": "camera.hero",
                                "activates_at": None,
                                "lifecycle": None,
                            }
                        ]
                    }
                ),
                "qualitative_defects": [],
            }
        ],
    )
    layer = SimpleNamespace(
        id="camera.hero",
        title="Camera",
        script="build/camera.py",
    )

    path = write_test_layer_outcome(
        tmp_path,
        layer,
        status="passed",
        best={"round": 0, "mean": 0.0, "render": None},
        canonical=[
            (
                (1, "refs/M1.png"),
                {
                    "evidence": [
                        {
                            "id": "fixture-contract-0",
                            "metric": "fixture",
                            "value": 1,
                            "target": "= 1",
                            "pass": True,
                            "source": "scene_contract",
                            "authoritative": True,
                            "owner_layer": "camera.hero",
                            "fault_owner": "camera.hero",
                        }
                    ],
                    "decided_by": "metrics",
                },
            )
        ],
        run_id="run-1",
        blender_version="5.2",
    )

    assert path == layer_outcome_path(tmp_path, "camera.hero")
    assert json.loads(path.read_text(encoding="utf-8"))["layer"] == "camera.hero"
