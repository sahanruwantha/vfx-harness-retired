from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from tests.layer_outcome_fixtures import write_test_layer_outcome
from vfx_harness.agents.builder import verify
from vfx_harness.domain.layer_outcomes import OUTCOME_SCHEMA
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import revalidation
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.revalidation import eligibility

_AUTHORITATIVE = {
    "id": "typed-check",
    "metric": "object_property",
    "value": 1.0,
    "target": ">= 1",
    "pass": True,
    "source": "interface_contract",
    "authoritative": True,
    "owner_layer": "1",
    "fault_owner": "1",
    "activates_at": "1",
    "lifecycle": "layer",
}


class _Session:
    def __init__(self, source: Path) -> None:
        self.source = source

    def run(self, *_args, **_kwargs) -> dict:
        return {"result": {}}

    def render_full(self, *, frame: int, mode: str, scale: float) -> dict:
        return {
            "image_path": str(self.source),
            "frame": frame,
            "mode": mode,
            "resolution": [64, 36, int(scale * 100)],
            "render_state": {
                "engine": "BLENDER_EEVEE_NEXT",
                "scene_digest": "fixture-scene",
            },
            "warnings": [],
        }


class _Ledger:
    def __init__(self) -> None:
        self.rounds: list[tuple[tuple, dict]] = []

    def record_round(self, *args, **kwargs) -> None:
        self.rounds.append((args, kwargs))


def _verified_canonical(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raster_required: bool,
) -> tuple[SimpleNamespace, list]:
    run_artifacts.create(root, f"canonical-{'render' if raster_required else 'executable'}")
    source = root / "worker-output.png"
    source.write_bytes(b"exact worker PNG bytes")
    reference = root / "refs" / "target.png"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_bytes(b"exact reference bytes")
    script_rel = "build/units/01/unit.py"
    script = root / script_rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# deterministic fixture\n", encoding="utf-8")

    builder = verify.builder_package()
    monkeypatch.setattr(builder, "_preamble", lambda _shot: "")
    monkeypatch.setattr(builder, "_run_prior_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(
        builder,
        "_render_evidence",
        lambda *_args, **_kwargs: [dict(_AUTHORITATIVE)],
    )
    monkeypatch.setattr(verify, "_run_artifact_script", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        verify,
        "_unit_requires_raster",
        lambda *_args, **_kwargs: raster_required,
    )
    monkeypatch.setattr(verify, "_unit_raster_mode", lambda _unit: "eevee")
    monkeypatch.setattr(verify, "_layer_needs_motion", lambda _layer: False)
    monkeypatch.setattr(verify, "_persist_contract_gaps", lambda *_args, **_kwargs: None)

    async def judge(*_args, evidence, **_kwargs) -> dict:
        return {
            "scores": {"form": 5.0},
            "mean": 5.0,
            "pass": True,
            "issues": [],
            "evidence": evidence,
            "decided_by": "fixture",
        }

    monkeypatch.setattr(verify, "_judge_unit_or_layer", judge)
    layer = SimpleNamespace(
        id="1",
        title="Fixture layer",
        script=script_rel,
        judges=((40, "refs/target.png"),),
        stages=(),
        owns=("form",),
    )
    shot = SimpleNamespace(
        folder=root,
        frontmatter={"type": "still"},
        frames=40,
    )
    milestone = Milestone("1", 40, "refs/target.png", "target")
    canonical: list = []

    result = anyio.run(
        verify._verify_script,
        shot,
        milestone,
        script_rel,
        [],
        _Session(source),
        [("form", "declared form")],
        _Ledger(),
        False,
        None,
        None,
        None,
        None,
        layer,
        None,
        canonical,
        None,
    )

    assert result == "passed"
    return layer, canonical


def _sealed_outcome(
    root: Path,
    layer,
    canonical: list,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, Path]:
    manifest = {"complete": "producer-boundary"}
    monkeypatch.setattr(
        revalidation,
        "input_manifest",
        lambda *_args, **_kwargs: manifest,
    )
    outcome_path = write_test_layer_outcome(
        root,
        layer,
        status="passed",
        best={"round": 1, "mean": 5.0, "render": canonical[0][1].get("render")},
        canonical=canonical,
        run_id="producer-boundary",
        attempt=1,
        blender_version="fixture",
    )
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    assert outcome["schema"] == OUTCOME_SCHEMA
    return outcome, root / str(outcome["canonical"][0].get("render") or "")


def test_render_canonical_seals_actual_receipt_and_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layer, canonical = _verified_canonical(
        tmp_path,
        monkeypatch,
        raster_required=True,
    )
    verdict = canonical[0][1]
    assert verdict["evidence_kind"] == "render"
    assert verdict["render"].startswith("runs/")
    assert verdict["render_capture"]["png_sha256"]

    outcome, render = _sealed_outcome(tmp_path, layer, canonical, monkeypatch)
    record = outcome["canonical"][0]
    assert record["render"] == verdict["render"]
    assert record["render_capture"] == verdict["render_capture"]
    assert eligibility(outcome, outcome["revalidation_manifest"], tmp_path)[0]

    render_bytes = render.read_bytes()
    render.write_bytes(b"mutated render")
    eligible, reasons = eligibility(outcome, outcome["revalidation_manifest"], tmp_path)
    assert not eligible
    assert any("canonical changed" in reason for reason in reasons)
    render.write_bytes(render_bytes)

    reference = tmp_path / record["ref"]
    reference_bytes = reference.read_bytes()
    reference.write_bytes(b"mutated reference")
    eligible, reasons = eligibility(outcome, outcome["revalidation_manifest"], tmp_path)
    assert not eligible
    assert any("reference changed" in reason for reason in reasons)
    reference.write_bytes(reference_bytes)

    render.unlink()
    assert not eligibility(outcome, outcome["revalidation_manifest"], tmp_path)[0]
    render.write_bytes(render_bytes)
    reference.unlink()
    assert not eligibility(outcome, outcome["revalidation_manifest"], tmp_path)[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("render", None),
        ("render_sha256", None),
        ("render_capture", None),
        ("ref", None),
        ("ref_sha256", None),
    ],
)
def test_render_canonical_never_accepts_null_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layer, canonical = _verified_canonical(
        tmp_path,
        monkeypatch,
        raster_required=True,
    )
    outcome, _render = _sealed_outcome(tmp_path, layer, canonical, monkeypatch)
    malformed = copy.deepcopy(outcome)
    malformed["canonical"][0][field] = value

    assert not eligibility(
        malformed,
        malformed["revalidation_manifest"],
        tmp_path,
    )[0]


def test_executable_only_canonical_is_explicit_and_never_invents_a_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layer, canonical = _verified_canonical(
        tmp_path,
        monkeypatch,
        raster_required=False,
    )
    verdict = canonical[0][1]
    assert verdict["evidence_kind"] == "executable_only"
    assert "render" not in verdict
    assert "render_capture" not in verdict

    outcome, _render = _sealed_outcome(tmp_path, layer, canonical, monkeypatch)
    record = outcome["canonical"][0]
    assert record["evidence_kind"] == "executable_only"
    assert not {"render", "render_sha256", "render_capture"} & set(record)
    assert eligibility(outcome, outcome["revalidation_manifest"], tmp_path)[0]

    malformed = copy.deepcopy(outcome)
    malformed["canonical"][0]["render"] = None
    assert not eligibility(
        malformed,
        malformed["revalidation_manifest"],
        tmp_path,
    )[0]
