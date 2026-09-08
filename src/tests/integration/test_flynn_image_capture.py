"""Real confined Workbench images from exact cold prior/candidate replay."""

from __future__ import annotations

import asyncio
import json

import flynn_agents_sdk as flynn

from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents.builder import candidate_script, flynn_image_capture
from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.checks import Check, evaluate
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence

PRIOR = '''import bpy
camera_data = bpy.data.cameras.new('fixture_camera')
camera = bpy.data.objects.new('fixture_camera', camera_data)
bpy.context.scene.collection.objects.link(camera)
camera.location = (0, 0, 3)
bpy.context.scene.camera = camera
camera_data.type = 'ORTHO'
camera_data.ortho_scale = 2
bpy.ops.mesh.primitive_plane_add(size=4)
m = bpy.data.materials.new('fixture_surface')
m.use_nodes = True
m.node_tree.nodes.clear()
e = m.node_tree.nodes.new('ShaderNodeEmission')
e.inputs[0].default_value = (0.02, 0.02, 0.02, 1)
o = m.node_tree.nodes.new('ShaderNodeOutputMaterial')
m.node_tree.links.new(e.outputs[0], o.inputs['Surface'])
bpy.context.object.data.materials.append(m)
'''
CANDIDATE = '''import bpy
bpy.context.object.scale = (0.1, 0.1, 0.1)
'''


def test_cold_capture_ignores_warm_scene_and_pays_from_its_actual_images(tmp_path, monkeypatch):
    shot, _, _, _, guard, layout = _authority(tmp_path, monkeypatch)
    source = tmp_path / 'prior.py'
    source.write_text(PRIOR)
    path = candidate_script.exact_candidate_script_path(tmp_path, guard)
    candidate_script.write_scratch_candidate(tmp_path, path, CANDIDATE, guard)
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / 'worker', cwd=tmp_path,
    ) as session:
        session.run("import bpy\nbpy.context.scene.frame_set(1)\nbpy.ops.mesh.primitive_cube_add()\n")
        capture = flynn_image_capture.UnitImageCapture(
            shot=shot, attempt_guard=guard, fence_lease=lease, session=session,
            prior_paths=[source], check_candidate=lambda: None,
        )
        result = asyncio.run(capture.capture({'frame': 240}))
        data = json.loads(result.data_json)
        metric = Check('measure', 'region_mean', '>=', 0, float('inf'), regions={'r': (0, 0, 1, 1)})
        before = evaluate(metric, tmp_path / data['adversary']['path'])
        after = evaluate(metric, tmp_path / data['candidate']['path'])
        assert abs(after - before) > 50
        assert data['candidate']['mode'] == data['adversary']['mode'] == 'solid'
        assert data['candidate']['frame'] == data['adversary']['frame'] == 240
        assert data['candidate']['resolution'] == data['adversary']['resolution']
        args = {'checks': [{'id': 'measured-image-gap', 'frame': 240, 'axis': 'final_lock',
                            'metric': 'region_mean', 'regions': {'r': [0, 0, 1, 1]},
                            'op': '>=' if after > before else '<=',
                            ('lo' if after > before else 'hi'): (before + after) / 2, 'ref': 'refs/a.png'}],
                'after_handle': data['candidate']['handle']}
        payment = asyncio.run(capture.payment.tool.execute(json.dumps(args)))
        assert json.loads(flynn.ToolResult.from_json(payment).data_json)['kept_ids'] == ['measured-image-gap']
        objects = session.run('RESULT = sorted(o.type for o in bpy.context.scene.objects)', journal=False)['result']
        assert objects == ['CAMERA', 'MESH']
