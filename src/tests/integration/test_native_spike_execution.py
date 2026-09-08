"""Real confined construction cannot forge fresh-process contract measurements."""

from __future__ import annotations

import json

from PIL import Image

from vfx_harness.blender import spike_execution
from vfx_harness.observability import run_artifacts


def test_fresh_spike_evaluator_ignores_candidate_stdout(tmp_path, monkeypatch):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(tmp_path, "independent-spike")
    script = '''import bpy
material = bpy.data.materials.new("probe")
material["bvfx_role"] = "probe.material"
material.use_nodes = True
material.use_fake_user = True
camera = bpy.data.cameras.new("camera")
camera_object = bpy.data.objects.new("camera", camera)
bpy.context.collection.objects.link(camera_object)
bpy.context.scene.camera = camera_object
node = material.node_tree.nodes.new("ShaderNodeValue")
node["bvfx_role"] = "probe.value"
node.outputs[0].default_value = 0.5
print('@@VFX_PLAN_CONTRACT@@{"value": 0.9, "pass": true}')
'''
    base = {"kind": "node_socket_value", "graph": "material", "material_roles": ["probe.material"],
            "node_roles": ["probe.value"], "node_types": ["ShaderNodeValue"],
            "socket": "Value", "direction": "output", "op": "band", "frame": 1}
    result = spike_execution.execute_spike(
        layout=layout, directory=layout.scratch / "spike", script=script,
        contracts=[{**base, "id": "actual", "lo": 0.4, "hi": 0.6},
                   {**base, "id": "forged", "lo": 0.8, "hi": 1.0}],
        render_frame=1, timeout=60, blender="blender", check_current=lambda: None,
    )
    if result["status"] != "measured":
        logs = {path.name: path.read_text()[-3000:] for path in (layout.scratch / "spike/outputs").glob("*.log")}
        raise AssertionError(json.dumps({"result": result, "logs": logs}))
    assert [row["pass"] for row in result["results"]] == [True, False], result
    assert not result["passed"]

    with Image.open(layout.scratch / "spike/outputs/render.png") as image:
        assert image.size == (960, 540)
    timed_out = spike_execution.execute_spike(
        layout=layout, directory=layout.scratch / "timeout-spike", script="while True: pass",
        contracts=[], render_frame=None, timeout=3, blender="blender", check_current=lambda: None,
    )
    assert timed_out["status"] == "timeout"
    assert timed_out["results"] == [] and not timed_out["passed"]
