"""Generated construction reaches native cold replay with exact asset dependencies.

External generation and plate judgment are fixture adapters. Blender imports a real
GLB; replay, evaluation and completion publishers are not mocked.
"""

import asyncio
import hashlib
import json
from functools import partial

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.integration.test_flynn_image_capture import PRIOR
from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents.builder import flynn_image_capture, flynn_unit
from vfx_harness.agents.builder.unit_completion import complete_and_resolve_unit, resolve_completed_unit
from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration import generate_construction, unit_state
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.refobs import load_witness_crop, mint_refobs

PROGRAM = """import bpy
for name in bvfx_import_construction():
    host = bpy.data.objects.get(name)
    host['bvfx_role'] = 'comp'
"""


def configure(root):
    reference = root / "refs/a.png"
    Image.new("RGB", (64, 64), (50, 80, 100)).save(reference)
    witness = mint_refobs(root, reference, [.2, .2, .8, .8], source_rel="refs/a.png")
    path = root / "layers.json"
    layers = json.loads(path.read_text())
    layer = layers["layers"][0]
    layer["judge"] = [{"frame": 240, "ref": "refs/a.png"}]
    layer["evidence_domains"] = ["scene", "projected_composition"]
    unit = layer["stages"][0]
    unit["provides"] = ["geometry"]
    unit["look_capabilities"] = []
    unit["construction"] = {"route": "generate", "witnesses": [witness]}
    unit["evaluation"]["judge"] = layer["judge"]
    unit["evaluation"]["claims"][0]["moments"] = [240]
    visible = dict(unit["evaluation"]["claims"][0])
    visible.update(id="visible-claim", property="visible_fraction", asserts="projected_composition",
                   evidence=[{"kind": "scene_contract", "id": "visible"}])
    unit["evaluation"]["claims"].append(visible)
    path.write_text(json.dumps(layers))
    contracts = json.loads((root / "scene_checks.json").read_text())
    contracts["contracts"].append({
        "id": "visible", "kind": "visible_fraction", "roles": ["comp"], "frame": 240,
        "owner_layer": "1", "fault_owner": "1", "activates_at": "1", "lifecycle": "layer",
        "axis": "final_lock", "op": "min", "lo": .25,
    })
    (root / "scene_checks.json").write_text(json.dumps(contracts))


def copy_plate(source, destination, **kwargs):
    destination.write_bytes(source.read_bytes())
    return destination


@pytest.mark.parametrize("fault", [None, "generation", "pointer", "glb", "witness", "budget", "cancelled"])
def test_generated_unit_uses_native_preparation_and_cold_asset_replay(tmp_path, monkeypatch, fault):
    shot, layer, unit, selected, guard, layout = _authority(tmp_path, monkeypatch, configure=configure)
    digest = hashlib.sha256(PROGRAM.encode()).hexdigest()
    script_rel = unit.mutates.script_spans[0]
    pointer = (tmp_path / script_rel).with_suffix(".construction.json")
    milestone = Milestone("1@lock", 240, "refs/a.png", "generated source exists")
    camera = tmp_path / "prior.py"
    camera.write_text(PRIOR.split("bpy.ops.mesh", 1)[0])
    calls = [flynn.ToolCall("inspect_unit", "{}"),
             flynn.ToolCall("write_candidate", json.dumps({"source": PROGRAM})),
             flynn.ToolCall("probe_candidate", "{}"),
             flynn.ToolCall("freeze_candidate", json.dumps({"sha256": digest}))]

    class Adapter:
        count = 0

        async def generate(self, request):
            assert "bvfx_import_construction()" in request.objective
            call = calls[self.count]
            self.count += 1
            if self.count == 3 and fault in {"pointer", "glb", "witness"}:
                target = {
                    "pointer": pointer,
                    "glb": tmp_path / json.loads(pointer.read_text())["glb"],
                    "witness": load_witness_crop(tmp_path, unit.construction.witnesses[0]),
                }[fault]
                target.write_bytes(target.read_bytes() + b"changed")
            return flynn.InferenceResult.scripted(call)

    adapter = Adapter()
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / "worker", cwd=tmp_path,
    ) as session:
        source = session.artifacts / "fixture.glb"
        session.run("import bpy\nbpy.ops.object.select_all(action='SELECT')\n"
                    "bpy.ops.object.delete(use_global=False)\nbpy.ops.mesh.primitive_cube_add()\n"
                    f"bpy.ops.export_scene.gltf(filepath={str(source)!r}, export_format='GLB')\n", journal=False)

        def to_glb(images, destination):
            assert len(images) == 1
            assert fault != "budget", "exhausted budget reached generation"
            if fault == "generation":
                raise OSError("injected generation failure")
            destination.write_bytes(source.read_bytes())
            if fault == "cancelled":
                raise asyncio.CancelledError("injected cancellation after staging bytes")

        monkeypatch.setattr(generate_construction, "stage_generate_unit", partial(
            generate_construction.stage_generate_unit, isolate_relight=copy_plate,
            isolate_cutout=copy_plate, orbit_view=copy_plate, identity_gaps=lambda *_: (),
            to_glb=to_glb, extra_cameras=(),
        ))

        def invoke():
            return asyncio.run(flynn_unit.build_unit(
                shot, milestone, script_rel, [camera], session, inference=adapter,
                limits=flynn.RunLimits(4, 4, 3, 180) if fault == "budget" else flynn.RunLimits(6, 6, 5, 180),
                layer=layer, active_unit=unit,
                selected_authority=selected, attempt_guard=guard, fence_lease=lease,
            ))

        if fault:
            with pytest.raises((OSError, generate_construction.GenerateConstructionError,
                                flynn.BudgetExhausted, asyncio.CancelledError)):
                invoke()
            assert adapter.count == (0 if fault in {"generation", "budget", "cancelled"} else 3)
            assert not (tmp_path / script_rel).exists()
            if fault in {"generation", "budget", "cancelled"}:
                assert not pointer.exists()
            assert unit_state.load(tmp_path, "1")["units"][unit.id]["status"] == "building"
            with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
                assert not run.records()["commits"]
                assert run.usage_summary()["scripted_invocations"] == (0 if fault == "budget" else adapter.count + 1)
            return

        ledger = invoke()
        assert ledger.status(milestone) == "passed"
        capture = flynn_image_capture.UnitImageCapture(
            shot=shot, attempt_guard=guard, fence_lease=lease, session=session,
            prior_paths=[camera], check_candidate=lambda: guard.check("capture generated fixture"),
        )
        captured = asyncio.run(capture.capture({"frame": 240}))
        image_data = json.loads(captured.data_json)
        assert image_data["candidate"]["sha256"] != image_data["adversary"]["sha256"]
        with flynn.SQLiteRun.open(layout.checkpoints / "flynn" / f"{guard.claim.claim_id}.sqlite") as run:
            assert len(run.records()["operations"]) == 6
            assert run.remaining()["external"] == 0
            assert run.usage_summary()["scripted_invocations"] == 6
            assert not run.records()["commits"]
        frozen = unit_state.freeze_checkpoint(
            tmp_path, "1", unit, active_contract_ids=(), candidate_hash=digest,
            settings_hash=hashlib.sha256(b"executable-only").hexdigest(), script_hash=digest,
            input_hash=guard.expected_plan_hash, layer_active_vis_ids=(),
            attempt=guard.claim, selection_token=selected.selection_token,
        )
        unit_state.transition(tmp_path, "1", unit.id, "evaluating", reason="canonical evaluation sealed",
                              attempt=guard.claim, selection_token=selected.selection_token)
        receipt = complete_and_resolve_unit(
            tmp_path, "1", unit, layer.stages, guard.claim, expected_plan_hash=guard.expected_plan_hash,
            selected_authority=selected, checkpoint_hash=frozen["units"][unit.id]["checkpoint"]["candidate_hash"],
        )
        assert receipt.script_hash == digest
        asset = tmp_path / json.loads(pointer.read_text())["glb"]
        asset.write_bytes(asset.read_bytes() + b"changed after completion")
        with pytest.raises(ValueError):
            resolve_completed_unit(tmp_path, "1", unit, layer.stages, receipt,
                                   expected_plan_hash=guard.expected_plan_hash, selected_authority=selected)
