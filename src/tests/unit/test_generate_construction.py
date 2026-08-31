"""Harness-owned generate construction: plates, identity, Meshy, promotion, pin."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from vfx_harness.domain.construction import ASSET_STAGE_RETIRED_RULE
from vfx_harness.domain.refobs import (
    PROMOTED_CONSTRUCTION_SCHEMA,
    UNREGISTERED_WITNESS_RULE,
    construction_pointer_relpath,
    promoted_glb_relpath,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.orchestration.generate_construction import (
    CONSTRUCTION_PIN,
    GenerateConstructionError,
    load_promoted_construction,
    pin_for_script,
    prepare_generate_unit,
    prepare_plates,
)
from vfx_harness.orchestration.refobs import mint_refobs
from vfx_harness.orchestration.unit_state import unit_digest


def _still(folder: Path) -> Path:
    refs = folder / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    path = refs / "still.png"
    Image.new("RGB", (64, 64), (40, 80, 120)).save(path)
    return path


def _generate_unit(witness: str, *, route: str = "generate") -> WorkUnit:
    row = {
        "id": "prop_source",
        "title": "prop_source",
        "plan": "plans/units/prop_source.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["prop.shell"],
            "controls": [],
            "script_spans": ["build/units/02/prop_source.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 1,
            "judge": [{"frame": 1, "ref": "refs/a.png"}],
            "temporal_evidence": "none",
            "claims": [{
                "id": "claim.prop_source",
                "proposition": "one source mesh exists",
                "axis": "form",
                "property": "object_count",
                "subject_roles": ["prop.shell"],
                "subject_controls": [],
                "moments": [1],
                "kind": "atomic",
                "required": True,
                "authority": "executable_required",
                "repair_owner": "prop_source",
                "asserts": "scene",
                "evidence": [{"kind": "scene_contract", "id": "prop-count"}],
            }],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "provides": ["geometry"],
        "construction": {"route": route, "witnesses": [witness]},
    }
    return WorkUnit.parse(row, "unit.prop_source")


def _copy_parent(parent: Path, dest: Path, **_kwargs: object) -> Path:
    dest.write_bytes(parent.read_bytes())
    return dest


def _glb_bytes() -> bytes:
    return b"glTF" + b"\x00" * 12 + b"fixture-mesh"


def test_failed_orbit_never_reaches_meshy(tmp_path: Path) -> None:
    still = _still(tmp_path)
    token = mint_refobs(tmp_path, still, [0.15, 0.15, 0.45, 0.5], source_rel="refs/still.png")
    meshy_images: list[str] = []

    def orbit_view(source: Path, dest: Path, *, camera: str) -> Path:
        dest.write_bytes(b"orbit-failed-" + camera.encode())
        return dest

    def identity_gaps(_parent: Path, derived: Path) -> tuple[str, ...]:
        if "orbit" in derived.name:
            return ("orbit lost the subject silhouette",)
        return ()

    def to_glb(images: list[Path], out: Path, **_kwargs: object) -> dict:
        meshy_images.extend(path.name for path in images)
        out.write_bytes(_glb_bytes())
        return {"view_count": len(images)}

    prepare_generate_unit(
        tmp_path,
        "2",
        _generate_unit(token),
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=orbit_view,
        identity_gaps=identity_gaps,
        to_glb=to_glb,
        extra_cameras=("three_quarter", "left"),
        plate_dir=tmp_path / "plates",
        candidate_out=tmp_path / "candidate.glb",
    )

    assert meshy_images
    assert all("orbit" not in name for name in meshy_images)
    kept = prepare_plates(
        [still],
        tmp_path / "plates-direct",
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=orbit_view,
        identity_gaps=identity_gaps,
        extra_cameras=("right",),
    )
    assert all("orbit" not in path.name for path in kept)


def test_all_white_plates_never_call_meshy(tmp_path: Path) -> None:
    parent = tmp_path / "crop.png"
    Image.new("RGB", (16, 16), (30, 30, 30)).save(parent)
    called = {"meshy": False}

    def to_glb(*_args: object, **_kwargs: object) -> dict:
        called["meshy"] = True
        raise AssertionError("Meshy must not run on identity failure")

    with pytest.raises(GenerateConstructionError, match="isolate identity failed"):
        prepare_plates(
            [parent],
            tmp_path / "plates",
            isolate_relight=_copy_parent,
            isolate_cutout=_copy_parent,
            orbit_view=_copy_parent,
            identity_gaps=lambda *_a: ("empty white field",),
        )
    assert called["meshy"] is False

    still = _still(tmp_path)
    token = mint_refobs(tmp_path, still, [0.1, 0.1, 0.4, 0.4], source_rel="refs/still.png")
    with pytest.raises(GenerateConstructionError, match="isolate identity failed"):
        prepare_generate_unit(
            tmp_path,
            "2",
            _generate_unit(token),
            isolate_relight=_copy_parent,
            isolate_cutout=_copy_parent,
            orbit_view=_copy_parent,
            identity_gaps=lambda *_a: ("empty white field",),
            to_glb=to_glb,
            extra_cameras=(),
            plate_dir=tmp_path / "plates",
            candidate_out=tmp_path / "candidate.glb",
        )
    assert called["meshy"] is False


def test_prepare_generate_unit_promotes_hash_verified_glb(tmp_path: Path) -> None:
    still = _still(tmp_path)
    token = mint_refobs(tmp_path, still, [0.2, 0.2, 0.6, 0.6], source_rel="refs/still.png")
    unit = _generate_unit(token)
    meshy_calls: list[int] = []

    def to_glb(images: list[Path], out: Path, **_kwargs: object) -> dict:
        meshy_calls.append(len(images))
        out.write_bytes(_glb_bytes())
        return {"view_count": len(images)}

    promoted = prepare_generate_unit(
        tmp_path,
        "2",
        unit,
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=_copy_parent,
        identity_gaps=lambda *_a: (),
        to_glb=to_glb,
        extra_cameras=(),
        plate_dir=tmp_path / "plates",
        candidate_out=tmp_path / "candidate.glb",
    )

    digest = hashlib.sha256(_glb_bytes()).hexdigest()
    rel = promoted_glb_relpath(digest)
    assert promoted.glb_relpath == rel
    assert promoted.sha256 == digest
    assert promoted.reused is False
    assert (tmp_path / rel).read_bytes() == _glb_bytes()
    pointer = tmp_path / construction_pointer_relpath("2", unit.id)
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    assert payload["schema"] == PROMOTED_CONSTRUCTION_SCHEMA
    assert payload["unit_digest"] == unit_digest(unit)
    assert payload["glb"] == rel
    assert meshy_calls == [1]

    reused = prepare_generate_unit(
        tmp_path,
        "2",
        unit,
        to_glb=to_glb,
        plate_dir=tmp_path / "plates",
        candidate_out=tmp_path / "candidate.glb",
    )
    assert reused.reused is True
    assert meshy_calls == [1]


def test_pointer_hash_mismatch_and_unregistered_witness_fail_closed(tmp_path: Path) -> None:
    still = _still(tmp_path)
    token = mint_refobs(tmp_path, still, [0.2, 0.2, 0.5, 0.5], source_rel="refs/still.png")
    unit = _generate_unit(token)
    digest = hashlib.sha256(_glb_bytes()).hexdigest()
    rel = promoted_glb_relpath(digest)
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_glb_bytes())
    pointer = tmp_path / construction_pointer_relpath("2", unit.id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps({
            "schema": PROMOTED_CONSTRUCTION_SCHEMA,
            "unit_id": unit.id,
            "layer_id": "2",
            "unit_digest": unit_digest(unit),
            "sha256": "0" * 64,
            "glb": rel,
            "view_count": 1,
        })
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(GenerateConstructionError, match="hash mismatch"):
        load_promoted_construction(tmp_path, "2", unit.id, unit_digest(unit))

    with pytest.raises(GenerateConstructionError, match=UNREGISTERED_WITNESS_RULE[:24]):
        prepare_generate_unit(
            tmp_path,
            "2",
            _generate_unit("refobs-notminted"),
            plate_dir=tmp_path / "plates",
            candidate_out=tmp_path / "candidate.glb",
        )


def test_retrieve_is_not_wired(tmp_path: Path) -> None:
    unit = _generate_unit("lib.prop.v1", route="retrieve")
    with pytest.raises(GenerateConstructionError, match="retrieve"):
        prepare_generate_unit(tmp_path, "2", unit)


def test_pin_for_script_loads_sibling_pointer(tmp_path: Path) -> None:
    digest = hashlib.sha256(_glb_bytes()).hexdigest()
    rel = promoted_glb_relpath(digest)
    glb = tmp_path / rel
    glb.parent.mkdir(parents=True, exist_ok=True)
    glb.write_bytes(_glb_bytes())
    script = tmp_path / "build" / "units" / "02" / "prop_source.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# fixture\n", encoding="utf-8")
    pointer = script.with_suffix(".construction.json")
    pointer.write_text(
        json.dumps({
            "schema": PROMOTED_CONSTRUCTION_SCHEMA,
            "glb": rel,
            "sha256": digest,
            "unit_digest": "abc",
            "view_count": 1,
        })
        + "\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    session = SimpleNamespace(artifacts=artifacts, cwd=tmp_path)
    pin_for_script(session, script)
    pin = json.loads((artifacts / CONSTRUCTION_PIN).read_text(encoding="utf-8"))
    assert pin["glb"] == rel
    assert pin["sha256"] == digest

    pin_for_script(session, tmp_path / "build" / "units" / "02" / "procedural.py")
    assert not (artifacts / CONSTRUCTION_PIN).exists()


def test_vfx_asset_cli_fails_closed_in_a_fresh_interpreter() -> None:
    """CLI import order cannot hide a construction ↔ work_units cycle."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from vfx_harness.cli import main; "
            "sys.argv = ['vfx', 'asset']; raise SystemExit(main())",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = completed.stderr + completed.stdout
    assert completed.returncode != 0
    assert "vfx asset is retired" in detail
    assert "circular import" not in detail


def test_vfx_asset_and_generate_builder_path_fail_closed() -> None:
    from vfx_harness.agents.asset_builder import build_assets, main
    from vfx_harness.agents.builder import prior, unit_loop
    from vfx_harness.blender.tools import mcp as blender_mcp

    with pytest.raises(SystemExit, match="vfx asset is retired"):
        main()
    with pytest.raises(SystemExit, match=ASSET_STAGE_RETIRED_RULE[:24]):
        build_assets("/unused")

    build_source = inspect.getsource(unit_loop.build_unit)
    assert "prepare_generate_unit" in build_source
    assert "pin_construction_import" in build_source
    prior_source = inspect.getsource(prior._run_artifact_script)
    assert "pin_for_script" in prior_source
    mcp_source = inspect.getsource(blender_mcp.build_blender_tools)
    assert '!= "generate"' in mcp_source
    assert "import_asset" in mcp_source
    prepare_source = inspect.getsource(prepare_generate_unit)
    assert "ensure(" not in prepare_source
