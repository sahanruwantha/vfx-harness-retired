"""Real replay and receipts with scripted unit inference and layer look judgment."""

import asyncio
import hashlib
import json
from copy import deepcopy

import flynn_agents_sdk as flynn

from tests.integration.test_flynn_image_capture import PRIOR
from tests.integration.test_flynn_image_unit_lifecycle import PROGRAM, image_authority
from tests.integration.test_flynn_unit_lifecycle import _authority
from tests.unit.test_plan_records import _materialize_fixture_ready_layer, _sparse_publication_rows, publish_current
from vfx_harness.agents import builder
from vfx_harness.agents.builder import flynn_unit
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.blender.session import BlenderSession
from vfx_harness.evidence.checks import Check, evaluate
from vfx_harness.orchestration import layer_plans, unit_state
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.layer_publication import require_current_layer_publication
from vfx_harness.orchestration.ledger import load_layers

CAMERA = PRIOR.split('bpy.ops.mesh', 1)[0] + "camera['bvfx_role'] = 'rig.camera'\n"
MARKER = '''import bpy
host = bpy.data.objects.new('marker', None)
bpy.context.scene.collection.objects.link(host)
host['bvfx_role'] = 'support.marker'
'''


def layer_authority(root, *, medium="eevee"):
    image_authority(root, medium=medium)
    document = json.loads((root / 'layers.json').read_text())
    layer = document['layers'][0]
    layer.update(id='2', script='build/02_image.py')
    image = layer['stages'][0]
    image.update(depends_on=['marker'], plan='plans/02_image/lock.md')
    image['mutates']['script_spans'] = ['build/units/02/lock.py']
    camera = deepcopy(image)
    camera.update(id='camera', title='Camera', plan='plans/01_camera/camera.md',
                  depends_on=[], provides=['camera'], look_capabilities=[])
    camera['mutates'] = {'mode': 'scoped', 'roles': ['rig.camera'], 'controls': [], 'control_roles': {},
                         'script_spans': ['build/units/01/camera.py']}
    claim = deepcopy(camera['evaluation']['claims'][0])
    claim.update(id='camera-count', property='object_count', subject_roles=['rig.camera'], subject_controls=[],
                 repair_owner='camera', evidence=[{'kind': 'scene_contract', 'id': 'camera-count'}])
    camera['evaluation']['claims'] = [claim]
    camera_layer = deepcopy(layer)
    camera_layer.update(id='1', title='Camera', script='build/01_camera.py', stages=[camera],
                        evidence_domains=['scene'])
    marker = deepcopy(camera)
    marker.update(id='marker', title='Marker', plan='plans/02_image/marker.md', provides=[])
    marker['mutates'].update(roles=['support.marker'], script_spans=['build/units/02/marker.py'])
    marker['evaluation']['claims'][0].update(id='marker-count', subject_roles=['support.marker'],
                                            repair_owner='marker',
                                            evidence=[{'kind': 'scene_contract', 'id': 'marker-count'}])
    layer['stages'].append(marker)  # The dependency order must override authored order.
    document['layers'] = [camera_layer, layer]
    (root / 'layers.json').write_text(json.dumps(document))
    contracts = json.loads((root / 'scene_checks.json').read_text())
    for row in contracts['contracts']:
        row.update(owner_layer='2', fault_owner='2', activates_at='2')
    contracts['contracts'].append({
        'id': 'camera-count', 'kind': 'object_count', 'roles': ['rig.camera'], 'owner_layer': '1',
        'fault_owner': '1', 'activates_at': '1', 'lifecycle': 'layer', 'axis': 'final_lock', 'op': 'eq', 'value': 1,
    })
    contracts['contracts'].append({
        'id': 'marker-count', 'kind': 'object_count', 'roles': ['support.marker'], 'owner_layer': '2',
        'fault_owner': '2', 'activates_at': '2', 'lifecycle': 'layer', 'axis': 'final_lock', 'op': 'eq', 'value': 1,
    })
    (root / 'scene_checks.json').write_text(json.dumps(contracts))
    requirements = json.loads((root / 'requirements.json').read_text())
    second = deepcopy(requirements['requirements'][0])
    second.update(id='R-image')
    second['resolution'].update(owner_layer='2', due={'kind': 'before_layer', 'layer': '2'})
    requirements['requirements'].append(second)
    (root / 'requirements.json').write_text(json.dumps(requirements))


def publish_layers(root, layout, **kwargs):
    ready = _sparse_publication_rows(root)
    document = json.loads((root / 'layers.json').read_text())
    document['layers'][1]['jit'].update(depends_on_layers=['1'], required_outcomes=[
        {'kind': 'scene_contract', 'id': 'camera-count'},
    ])
    (root / 'layers.json').write_text(json.dumps(document))
    publish_current(root, layout, **kwargs)
    selected = resolve_selected_authority(root)
    layer, scene, image = ready[0]
    _materialize_fixture_ready_layer(root, selected.plan.bundle.content_hash, layer, scene, image)
    return ready[1:]


def fixture(root, monkeypatch, *, medium="eevee"):
    pending = []

    def publish(*args, **kwargs):
        pending.extend(publish_layers(*args, **kwargs))

    shot, _, _, selected, _, layout = _authority(
        root, monkeypatch, configure=lambda root: layer_authority(root, medium=medium), claim=False, publisher=publish,
        run_parameters={'command': 'run', 'dispatch_kind': 'driver'},
    )
    layers = load_layers(shot, selected_authority=selected)
    stamp_plans(root, layers, selected, layout)
    return shot, layers, selected, layout, pending


def stamp_plans(root, layers, selected, layout):
    for layer in layers.values():
        for unit in layer.stages:
            path = root / unit.plan
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'# {unit.id}\nCreate only the semantic hosts declared by this unit. '
                            'Preserve all accepted predecessors and do not modify their roles or controls. '
                            'Write a deterministic candidate, probe its executable contracts, and freeze '
                            'only the measured candidate digest. Image units must capture their declared '
                            'frame and pay image debts using the current candidate handle before probing.\n')
            layer_plans.stamp_work_unit_plan(root, path, gate={'clean': True, 'blocking': 0, 'run_id': layout.run_id},
                                            selected_authority=selected)


def materialize_image_layer(root, shot, selected, layout, pending):
    row, scene, image = pending.pop()
    _materialize_fixture_ready_layer(root, selected.plan.bundle.content_hash, row, scene, image)
    selected = resolve_selected_authority(root)
    layers = load_layers(shot, selected_authority=selected)
    stamp_plans(root, {'2': layers['2']}, selected, layout)
    return selected, layers


class Adapter:
    def __init__(self, root, unit_id, *, medium="eevee"):
        self.root = root
        self.unit_id = unit_id
        self.medium = medium
        self.index = 0

    async def generate(self, request):
        program = {'camera': CAMERA, 'marker': MARKER, 'lock': PROGRAM}[self.unit_id]
        image = self.unit_id == 'lock'
        phase = self.index
        self.index += 1
        if phase == 0:
            call = flynn.ToolCall('write_candidate', json.dumps({'source': program}))
        elif image and phase == 1:
            call = flynn.ToolCall('capture_unit_frame', '{"frame":240}')
        elif image and phase == 2:
            assert len(request.images) == 2
            data = json.loads(request.observation)['data']
            assert data['candidate']['mode'] == data['adversary']['mode'] == self.medium
            metric = Check('measure', 'region_mean', '>=', 0, float('inf'), regions={'r': (0, 0, 1, 1)})
            before = evaluate(metric, self.root / data['adversary']['path'])
            after = evaluate(metric, self.root / data['candidate']['path'])
            assert abs(after - before) > 50
            call = flynn.ToolCall('propose_checks', json.dumps({
                'checks': [{'id': 'image-gap', 'frame': 240, 'axis': 'final_lock', 'metric': 'region_mean',
                            'regions': {'r': [0, 0, 1, 1]}, 'op': '>=' if after > before else '<=',
                            ('lo' if after > before else 'hi'): (before + after) / 2,
                            'ref': 'refs/a.png'}], 'after_handle': data['candidate']['handle'],
            }))
        elif phase == (3 if image else 1):
            call = flynn.ToolCall('probe_candidate', '{}')
        else:
            assert json.loads(request.observation)['canonical'] == 'passed', request.observation
            call = flynn.ToolCall('freeze_candidate', json.dumps({
                'sha256': hashlib.sha256(program.encode()).hexdigest(),
            }))
        return await flynn.ScriptedAdapter([call]).generate(request)


def scripted_layer_critic(monkeypatch):
    calls = []

    async def judge(shot, milestone, render, axes, *args, active_unit=None, evidence=(), **kwargs):
        # Unit claims remain executable. The existing composed layer owns a separate
        # look vote; its quality is scripted here, never claimed as live validation.
        assert active_unit is None
        assert milestone.id == '2'
        assert (shot.folder / render).is_file()
        assert evidence and all(row.get('pass') for row in evidence)
        calls.append(milestone.frame)
        return {'scores': {key: 5 for key, _ in axes}, 'mean': 5.0, 'pass': True,
                'issues': [], 'evidence': evidence, 'decided_by': 'scripted-fixture-critic',
                'scored_axes': [key for key, _ in axes], 'na_axes': [], 'observations': [],
                'observation_reconciliation': [], 'contract_gap': False, 'judge_conflict': False}

    monkeypatch.setattr(builder, '_judge', judge)
    return calls


def test_native_image_layer_replays_accepted_camera_and_completes(tmp_path, monkeypatch):
    shot, layers, selected, layout, pending = fixture(tmp_path, monkeypatch)
    order = []

    critic_calls = scripted_layer_critic(monkeypatch)

    async def execute(*args, **kwargs):
        unit = kwargs['active_unit']
        order.append(unit.id)
        assert [path.name for path in args[3]] == {
            'camera': [], 'marker': ['01_camera.py'], 'lock': ['01_camera.py', 'marker.py'],
        }[unit.id]
        return await flynn_unit.build_unit(*args, **kwargs, inference=Adapter(tmp_path, unit.id),
                                           limits=flynn.RunLimits(8, 8, 8, 240))

    with BlenderSession(artifacts_dir=layout.scratch / 'worker', cwd=tmp_path) as session:
        for lid in ('1', '2'):
            if lid == '2':
                selected, layers = materialize_image_layer(tmp_path, shot, selected, layout, pending)
            layer = layers[lid]
            ledger = asyncio.run(layer_runtime.build_layer(shot, layer, session, unit_builder=execute,
                                                           selected_authority=selected, verbose=False))
            assert ledger.status(layer.as_milestone({})) == 'passed'
    assert order == ['camera', 'marker', 'lock']
    assert critic_calls == [240]
    for layer in layers.values():
        publication = require_current_layer_publication(tmp_path, layer, selected)
        assert publication.receipt.final_status == 'passed'
        assert all(row['completion_receipt'] for row in unit_state.load(tmp_path, layer.id)['units'].values())
