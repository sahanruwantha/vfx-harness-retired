"""Native image-contract completion must be earned through cold replay and VFX receipts."""

import asyncio
import hashlib
import json
from copy import deepcopy

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.integration.test_flynn_image_capture import PRIOR
from tests.integration.test_flynn_unit_lifecycle import _authority
from vfx_harness.agents import builder
from vfx_harness.agents.builder import flynn_unit
from vfx_harness.agents.builder.unit_completion import complete_and_resolve_unit
from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.checks import Check, evaluate
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.builder_execution_fence import builder_execution_fence
from vfx_harness.orchestration.ledger import Milestone

PROGRAM = '''import bpy
mesh = bpy.data.meshes.new('surface_mesh')
mesh.from_pydata([(-2, -2, 0), (2, -2, 0), (2, 2, 0), (-2, 2, 0)], [], [(0, 1, 2, 3)])
host = bpy.data.objects.new('surface', mesh)
bpy.context.scene.collection.objects.link(host)
host['bvfx_role'] = 'comp'
material = bpy.data.materials.new('surface_material')
material.use_nodes = True
material.node_tree.nodes.clear()
emission = material.node_tree.nodes.new('ShaderNodeEmission')
emission.inputs[0].default_value = (0.7, 0.7, 0.7, 1)
output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
material.node_tree.links.new(emission.outputs[0], output.inputs['Surface'])
mesh.materials.append(material)
'''


def image_authority(root, *, medium="eevee"):
    layers = json.loads((root / 'layers.json').read_text())
    layer = layers['layers'][0]
    layer['evidence_domains'] = ['scene', 'image', 'projected_composition']
    layer['judge'] = [{'frame': 240, 'ref': 'refs/a.png'}]
    unit = layer['stages'][0]
    unit['look_capabilities'] = ['material'] if medium == 'eevee' else []
    unit['provides'] = ['geometry', 'illumination']
    unit['evaluation']['judge'] = layer['judge']
    claim = unit['evaluation']['claims'][0]
    claim.update(property='mesh_vertex_count', moments=[240])
    image_claim = deepcopy(claim)
    image_claim['id'] = 'image-claim'
    unit['evaluation']['claims'].append(image_claim)
    claim = image_claim
    claim.update(property='render_region_stat', asserts='image', moments=[240],
                 evidence=[{'kind': 'image_contract', 'id': 'image-gap'}])
    (root / 'layers.json').write_text(json.dumps(layers))
    contracts = json.loads((root / 'scene_checks.json').read_text())
    contract = contracts['contracts'][0]
    contract.update(kind='mesh_vertex_count', op='min', lo=4, frame=240)
    contract.pop('value')
    contracts['contracts'].append({
        'id': 'visible', 'kind': 'visible_fraction', 'roles': ['comp'], 'frame': 240,
        'owner_layer': '1', 'fault_owner': '1', 'activates_at': '1', 'lifecycle': 'layer',
        'axis': 'final_lock', 'op': 'min', 'lo': 0.25,
    })
    visible_claim = deepcopy(unit['evaluation']['claims'][0])
    visible_claim.update(id='visible-claim', property='visible_fraction', asserts='projected_composition',
                         evidence=[{'kind': 'scene_contract', 'id': 'visible'}])
    unit['evaluation']['claims'].append(visible_claim)
    (root / 'layers.json').write_text(json.dumps(layers))
    (root / 'scene_checks.json').write_text(json.dumps(contracts))
    Image.new('RGB', (64, 64), (220, 220, 220)).save(root / 'refs/a.png')


@pytest.mark.parametrize('medium', ['solid', 'eevee'])
@pytest.mark.parametrize('mode', ['finish', 'recapture', 'tamper'])
def test_native_image_unit_earns_completion_from_canonical_render(tmp_path, monkeypatch, mode, medium):
    recapture = mode == 'recapture'
    shot, layer, unit, selected, guard, layout = _authority(
        tmp_path, monkeypatch, configure=lambda root: image_authority(root, medium=medium),
    )
    source = tmp_path / 'prior.py'
    source.write_text(PRIOR.split('bpy.ops.mesh', 1)[0])
    # This fixture really emits in both cases; solid judges its geometry, not emission.
    program = PROGRAM
    candidate_sha = hashlib.sha256(program.encode()).hexdigest()
    requests = []
    captured = {}

    def no_critic(*args, **kwargs):
        raise AssertionError('Executable image contracts must never call a model critic')

    monkeypatch.setattr(builder, '_judge', no_critic)
    evaluate_observation = flynn_unit._ObservationEvaluator.evaluate

    async def corrupt_after_freeze(self, candidate):
        result = await evaluate_observation(self, candidate)
        if mode == 'tamper' and candidate.call.name == 'freeze_candidate':
            (tmp_path / captured['path']).write_bytes(b'injected capture substitution')
        return result

    monkeypatch.setattr(flynn_unit._ObservationEvaluator, 'evaluate', corrupt_after_freeze)

    class Adapter:
        async def generate(self, request):
            step = len(requests)
            requests.append(request)
            if step == 0:
                call = flynn.ToolCall('write_candidate', json.dumps({'source': program}))
            elif step == 1:
                call = flynn.ToolCall('capture_unit_frame', '{"frame":240}')
            elif step == 2:
                feedback = json.loads(request.observation)
                assert len(request.images) == 2
                data = feedback['data']
                assert data['candidate']['mode'] == data['adversary']['mode'] == medium
                captured.update(data['candidate'])
                metric = Check('measure', 'region_mean', '>=', 0, float('inf'), regions={'r': (0, 0, 1, 1)})
                before = evaluate(metric, tmp_path / data['adversary']['path'])
                after = evaluate(metric, tmp_path / data['candidate']['path'])
                assert abs(after - before) > 50
                call = flynn.ToolCall('propose_checks', json.dumps({
                    'checks': [{'id': 'image-gap', 'frame': 240, 'axis': 'final_lock',
                                'metric': 'region_mean', 'regions': {'r': [0, 0, 1, 1]},
                                'op': '>=' if after > before else '<=',
                                ('lo' if after > before else 'hi'): (before + after) / 2, 'ref': 'refs/a.png'}],
                    'after_handle': data['candidate']['handle'],
                }))
            elif step == 3:
                assert json.loads(request.observation)['data']['unpaid_ids'] == []
                assert not request.images
                call = flynn.ToolCall('probe_candidate', '{}')
            elif recapture and step == 4:
                assert 'freeze_candidate' in request.allowed_tools
                call = flynn.ToolCall('capture_unit_frame', '{"frame":240}')
            elif recapture and step == 5:
                assert 'freeze_candidate' not in request.allowed_tools
                call = flynn.ToolCall('probe_candidate', '{}')
            else:
                assert step == (6 if recapture else 4)
                assert json.loads(request.observation)['canonical'] == 'passed', request.observation
                call = flynn.ToolCall('freeze_candidate', json.dumps({'sha256': candidate_sha}))
            return await flynn.ScriptedAdapter([call]).generate(request)

    milestone = Milestone('1@lock', 240, 'refs/a.png', 'surface changes')
    steps = 8 if recapture else 6
    with builder_execution_fence(tmp_path) as lease, BlenderSession(
        artifacts_dir=layout.scratch / 'worker', cwd=tmp_path,
    ) as session:
        if mode == 'tamper':
            with pytest.raises(ValueError, match=r'paid image-contract debts|changed'):
                asyncio.run(flynn_unit.build_unit(
                    shot, milestone, unit.mutates.script_spans[0], [source], session,
                    inference=Adapter(), limits=flynn.RunLimits(steps, steps, steps - 1, 180),
                    layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
                    fence_lease=lease, verbose=False,
                ))
            assert not (tmp_path / unit.mutates.script_spans[0]).exists()
            with flynn.SQLiteRun.open(layout.checkpoints / 'flynn' / f'{guard.claim.claim_id}.sqlite') as run:
                assert run.remaining()['external'] == 1
                assert run.read().revision == 0
            return
        ledger = asyncio.run(flynn_unit.build_unit(
            shot, milestone, unit.mutates.script_spans[0], [source], session,
            inference=Adapter(), limits=flynn.RunLimits(steps, steps, steps - 1, 180),
            layer=layer, active_unit=unit, selected_authority=selected, attempt_guard=guard,
            fence_lease=lease, verbose=False,
        ))
        assert ledger.status(milestone) == 'passed'
        rendered_candidate = tmp_path / ledger._slot(milestone)['best']['render']
        render_sha = hashlib.sha256(rendered_candidate.read_bytes()).hexdigest()
        assert render_sha != candidate_sha
        frozen = unit_state.freeze_checkpoint(
            tmp_path, '1', unit, active_contract_ids=(), candidate_hash=render_sha,
            settings_hash=hashlib.sha256(medium.encode()).hexdigest(), script_hash=candidate_sha,
            input_hash=guard.expected_plan_hash, layer_active_vis_ids=(),
            attempt=guard.claim, selection_token=selected.selection_token,
        )
        unit_state.transition(tmp_path, '1', unit.id, 'evaluating', reason='canonical image evaluation sealed',
                              attempt=guard.claim, selection_token=selected.selection_token)
        completion = complete_and_resolve_unit(
            tmp_path, '1', unit, layer.stages, guard.claim,
            expected_plan_hash=guard.expected_plan_hash, selected_authority=selected,
            checkpoint_hash=frozen['units'][unit.id]['checkpoint']['candidate_hash'],
        )
        assert completion.script_hash == candidate_sha
    with flynn.SQLiteRun.open(layout.checkpoints / 'flynn' / f'{guard.claim.claim_id}.sqlite') as run:
        assert run.read().revision == 0
        operations = run.records()['operations']
        assert len(operations) == steps
        canonical = json.loads(operations[-1]['output'])
        assert canonical['canonical'] == 'passed'
        assert canonical['verdicts'][0][1]['render'] != unit.mutates.script_spans[0]
