from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.layer_outcome_paths import (
    layer_outcome_locator,
    layer_outcome_path,
)
from vfx_harness.orchestration.layer_plans import (
    load_layer_outcome,
    record_revalidation,
    write_layer_outcome,
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
    manifest = {
        "blender_version": "5.2",
        "comparison": {"mode": "eevee", "scale": 0.5},
    }
    monkeypatch.setattr(revalidation, "input_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(
        revalidation,
        "canonical_records",
        lambda *_args, **_kwargs: [{"evidence_kind": "executable_only", "authoritative": []}],
    )
    layer = SimpleNamespace(
        id="camera.hero",
        title="Camera",
        script="build/camera.py",
    )

    path = write_layer_outcome(
        tmp_path,
        layer,
        status="passed",
        best={},
        canonical=[((1, "refs/M1.png"), {"evidence": [], "decided_by": "metrics"})],
        run_id="run-1",
        blender_version="5.2",
    )

    assert path == layer_outcome_path(tmp_path, "camera.hero")
    assert load_layer_outcome(tmp_path, "camera.hero")["layer"] == "camera.hero"
    updated = record_revalidation(
        tmp_path,
        "camera.hero",
        run_id="run-2",
        attempt=2,
        evidence=[{"frame": 1, "pass": True}],
    )
    assert updated == path
    assert load_layer_outcome(tmp_path, "camera.hero")["last_revalidation"]["run_id"] == "run-2"


def test_current_outcome_eligibility_uses_sealed_comparison_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = {
        "schema": 2,
        "layer": "camera.hero",
        "status": "passed",
        "interfaces": [],
        "canonical": [{"authoritative": []}],
        "revalidation_manifest": {
            "blender_version": "5.2.1",
            "comparison": {"mode": "workbench", "scale": 0.375},
        },
    }
    observed: dict[str, object] = {}

    def manifest(_folder, layer, **settings):
        observed["layer"] = layer.id
        observed.update(settings)
        return outcome["revalidation_manifest"]

    monkeypatch.setattr(revalidation, "input_manifest", manifest)
    monkeypatch.setattr(
        revalidation,
        "eligibility",
        lambda *_args, **_kwargs: (False, ["input manifest changed"]),
    )

    assert revalidation.current_outcome_eligibility(
        tmp_path,
        SimpleNamespace(id="camera.hero"),
        outcome,
    ) == (False, ("input manifest changed",))
    assert observed == {
        "layer": "camera.hero",
        "blender_version": "5.2.1",
        "comparison_mode": "workbench",
        "comparison_scale": 0.375,
    }


def test_current_outcome_eligibility_refuses_missing_sealed_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        revalidation,
        "input_manifest",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid sealed settings must fail before replay"
        ),
    )
    outcome = {
        "schema": 2,
        "layer": "camera.hero",
        "status": "passed",
        "interfaces": [],
        "canonical": [{"authoritative": []}],
    }

    assert revalidation.current_outcome_eligibility(
        tmp_path,
        SimpleNamespace(id="camera.hero"),
        outcome,
    ) == (False, ("sealed revalidation manifest is invalid",))
