"""Harness-owned generate construction: plates, identity, Meshy, promotion, pin."""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from vfx_harness.agents.builder import prior as prior_runtime
from vfx_harness.blender.construction_replay import immutable_glb_path
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.construction import ASSET_STAGE_RETIRED_RULE
from vfx_harness.domain.refobs import (
    PROMOTED_CONSTRUCTION_SCHEMA,
    UNREGISTERED_WITNESS_RULE,
    construction_pointer_relpath,
    promoted_glb_relpath,
)
from vfx_harness.domain.work_units import WorkUnit
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import generate_construction as construction_runtime
from vfx_harness.orchestration.generate_construction import (
    CONSTRUCTION_PIN,
    GenerateConstructionError,
    load_promoted_construction,
    pin_for_script,
    prepare_plates,
    promote_generate_unit,
    stage_generate_unit,
)
from vfx_harness.orchestration.refobs import load_witness_crop, mint_refobs
from vfx_harness.orchestration.unit_replay_inputs import (
    ReplayInputConflict,
    prepare_replay_inputs,
)
from vfx_harness.orchestration.unit_state import unit_digest


@pytest.fixture(autouse=True)
def _active_construction_run(tmp_path: Path, monkeypatch):
    scratch = tmp_path / "run-scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        construction_runtime,
        "active_run",
        lambda _folder: SimpleNamespace(run_id="fixture-run", scratch=scratch),
    )


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


def test_construction_binding_rejects_ancestor_rebinding(tmp_path: Path) -> None:
    inputs = tmp_path / "state" / "refobs"
    inputs.mkdir(parents=True)
    source = inputs / "witness.png"
    source.write_bytes(b"trusted witness")
    _payload, binding = construction_runtime._read_real_file(tmp_path, source)
    retired = tmp_path / "state-retired"
    (tmp_path / "state").rename(retired)
    outside = tmp_path / "outside"
    (outside / "refobs").mkdir(parents=True)
    (outside / "refobs" / source.name).write_bytes(b"trusted witness")
    (tmp_path / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GenerateConstructionError, match="changed before guarded"):
        construction_runtime._require_identity(source, binding)


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

    stage_generate_unit(
        tmp_path,
        "2",
        _generate_unit(token),
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=orbit_view,
        identity_gaps=identity_gaps,
        to_glb=to_glb,
        extra_cameras=("three_quarter", "left"),
        run_id="fixture-run",
        claim_id="fixture-claim",
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
        stage_generate_unit(
            tmp_path,
            "2",
            _generate_unit(token),
            isolate_relight=_copy_parent,
            isolate_cutout=_copy_parent,
            orbit_view=_copy_parent,
            identity_gaps=lambda *_a: ("empty white field",),
            to_glb=to_glb,
            extra_cameras=(),
            run_id="fixture-run",
            claim_id="fixture-claim",
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

    staged = stage_generate_unit(
        tmp_path,
        "2",
        unit,
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=_copy_parent,
        identity_gaps=lambda *_a: (),
        to_glb=to_glb,
        extra_cameras=(),
        run_id="fixture-run",
        claim_id="fixture-claim",
    )
    promoted = promote_generate_unit(
        tmp_path,
        "2",
        unit,
        staged,
        expected_run_id="fixture-run",
        expected_claim_id="fixture-claim",
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

    reused = load_promoted_construction(
        tmp_path, "2", unit.id, unit_digest(unit)
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
        stage_generate_unit(
            tmp_path,
            "2",
            _generate_unit("refobs-notminted"),
            run_id="fixture-run",
            claim_id="fixture-claim",
        )


def test_dangling_construction_pointer_refuses_before_external_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pointer = tmp_path / construction_pointer_relpath("2", "prop_source")
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(tmp_path / "missing-pointer-target.json")
    generated: list[object] = []
    monkeypatch.setattr(
        construction_runtime.MeshyBackend,
        "to_glb",
        lambda *_args, **_kwargs: generated.append(object()),
    )

    with pytest.raises(GenerateConstructionError, match="real regular file"):
        construction_runtime.prepare_promoted_construction_reuse(
            tmp_path,
            "2",
            "prop_source",
            "a" * 64,
        )

    assert generated == []


def test_promoted_construction_reuse_revalidates_witness_bytes(tmp_path: Path) -> None:
    still = _still(tmp_path)
    token = mint_refobs(
        tmp_path,
        still,
        [0.2, 0.2, 0.6, 0.6],
        source_rel="refs/still.png",
    )
    unit = _generate_unit(token)

    def to_glb(_images: list[Path], out: Path, **_kwargs: object) -> dict:
        out.write_bytes(_glb_bytes())
        return {"view_count": 1}

    staged = stage_generate_unit(
        tmp_path,
        "2",
        unit,
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=_copy_parent,
        identity_gaps=lambda *_a: (),
        to_glb=to_glb,
        extra_cameras=(),
        run_id="fixture-run",
        claim_id="fixture-claim",
    )
    promote_generate_unit(
        tmp_path,
        "2",
        unit,
        staged,
        expected_run_id="fixture-run",
        expected_claim_id="fixture-claim",
    )
    load_witness_crop(tmp_path, token).write_bytes(b"changed-crop")

    with pytest.raises(GenerateConstructionError, match="changed after promotion"):
        load_promoted_construction(tmp_path, "2", unit.id, unit_digest(unit))


def test_pointer_failure_leaves_no_reusable_construction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    still = _still(tmp_path)
    token = mint_refobs(
        tmp_path,
        still,
        [0.2, 0.2, 0.6, 0.6],
        source_rel="refs/still.png",
    )
    unit = _generate_unit(token)

    def to_glb(_images: list[Path], out: Path, **_kwargs: object) -> dict:
        out.write_bytes(_glb_bytes())
        return {"view_count": 1}

    staged = stage_generate_unit(
        tmp_path,
        "2",
        unit,
        isolate_relight=_copy_parent,
        isolate_cutout=_copy_parent,
        orbit_view=_copy_parent,
        identity_gaps=lambda *_a: (),
        to_glb=to_glb,
        extra_cameras=(),
        run_id="fixture-run",
        claim_id="fixture-claim",
    )
    original = construction_runtime.durable_replace_file_bytes
    calls = 0

    def fail_pointer(folder, path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected pointer fsync failure")
        return original(folder, path, payload)

    monkeypatch.setattr(
        construction_runtime,
        "durable_replace_file_bytes",
        fail_pointer,
    )

    with pytest.raises(OSError, match="pointer fsync"):
        promote_generate_unit(
            tmp_path,
            "2",
            unit,
            staged,
            expected_run_id="fixture-run",
            expected_claim_id="fixture-claim",
        )

    assert not (tmp_path / construction_pointer_relpath("2", unit.id)).exists()


def test_retrieve_is_not_wired(tmp_path: Path) -> None:
    unit = _generate_unit("lib.prop.v1", route="retrieve")
    with pytest.raises(GenerateConstructionError, match="retrieve"):
        stage_generate_unit(
            tmp_path,
            "2",
            unit,
            run_id="fixture-run",
            claim_id="fixture-claim",
        )


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


def test_construction_import_reopen_uses_captured_memfd_bytes(tmp_path: Path) -> None:
    captured = _glb_bytes()
    digest = hashlib.sha256(captured).hexdigest()
    mutable_source = tmp_path / "source.glb"
    mutable_source.write_bytes(captured)

    with immutable_glb_path(captured, expected_sha256=digest) as importer_path:
        mutable_source.write_bytes(b"glTF-substituted-host-path")
        assert Path(importer_path).read_bytes() == captured

    assert not Path(importer_path).exists()


def test_evaluator_replay_receipt_binds_pointer_and_glb_identity(
    tmp_path: Path,
) -> None:
    digest = hashlib.sha256(_glb_bytes()).hexdigest()
    rel = promoted_glb_relpath(digest)
    glb = tmp_path / rel
    glb.parent.mkdir(parents=True, exist_ok=True)
    glb.write_bytes(_glb_bytes())
    script = tmp_path / "build" / "units" / "02" / "generated.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("bvfx_import_construction()\n", encoding="utf-8")
    pointer = script.with_suffix(".construction.json")
    pointer.write_text(
        json.dumps(
            {
                "schema": PROMOTED_CONSTRUCTION_SCHEMA,
                "glb": rel,
                "sha256": digest,
                "unit_digest": "a" * 64,
                "view_count": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = prior_runtime._prepare_artifact_replay_inputs(
        tmp_path,
        [(script.relative_to(tmp_path).as_posix(), script)],
    )[0]

    rows, _bindings = prepare_replay_inputs(tmp_path.absolute(), (artifact.executed,))
    assert [row.kind for row in rows[0].dependencies] == [
        "construction_pointer",
        "construction_glb",
    ]
    assert rows[0].dependencies[1].sha256 == digest

    original = glb.with_suffix(".accepted")
    glb.rename(original)
    glb.write_bytes(original.read_bytes())
    try:
        with pytest.raises(ReplayInputConflict, match="causal input changed"):
            prepare_replay_inputs(tmp_path.absolute(), (artifact.executed,))
    finally:
        glb.unlink()
        original.rename(glb)


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is unavailable")
def test_confined_blender_imports_glb_through_held_memfd(tmp_path: Path) -> None:
    with run_artifacts.invocation(
        tmp_path,
        "construction-memfd-smoke",
        shot_id="construction-memfd-smoke",
    ):
        session = BlenderSession(blender="blender", cwd=tmp_path).start()
        try:
            source = session.artifacts / "source.glb"
            result = session.run(
                "import bpy, hashlib\n"
                "from construction_replay import immutable_glb_path\n"
                f"source={str(source)!r}\n"
                "bpy.ops.mesh.primitive_cube_add()\n"
                "bpy.ops.export_scene.gltf(filepath=source, export_format='GLB')\n"
                "raw=open(source, 'rb').read()\n"
                "bpy.ops.object.select_all(action='SELECT')\n"
                "bpy.ops.object.delete(use_global=False)\n"
                "with immutable_glb_path(raw, expected_sha256=hashlib.sha256(raw).hexdigest()) as glb:\n"
                "    bpy.ops.import_scene.gltf(filepath=glb)\n"
                "RESULT={'mesh_objects': len([o for o in bpy.context.scene.objects if o.type == 'MESH'])}\n",
                journal=False,
            )
        finally:
            session.close()

    assert result["result"]["mesh_objects"] >= 1


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
    from vfx_harness.agents.builder import prior, unit_construction, unit_loop
    from vfx_harness.blender.tools import mcp as blender_mcp

    with pytest.raises(SystemExit, match="vfx asset is retired"):
        main()
    with pytest.raises(SystemExit, match=ASSET_STAGE_RETIRED_RULE[:24]):
        build_assets("/unused")

    build_source = inspect.getsource(unit_loop.build_unit)
    construction_source = inspect.getsource(unit_construction.resolve_unit_construction)
    assert "resolve_unit_construction" in build_source
    assert "stage_generate_unit" in construction_source
    assert "prepare_generate_unit_promotion" in construction_source
    assert "commit_generate_unit_promotion" in construction_source
    assert "pin_construction_import" in build_source
    prior_source = inspect.getsource(prior._run_artifact_script)
    assert "pin_for_script" in prior_source
    mcp_source = inspect.getsource(blender_mcp.build_blender_tools)
    assert '!= "generate"' in mcp_source
    assert "import_asset" in mcp_source
    prepare_source = inspect.getsource(stage_generate_unit)
    assert "ensure(" not in prepare_source
