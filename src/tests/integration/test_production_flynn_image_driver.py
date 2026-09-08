"""Scripted provider, real production image routing and driver publication checks.

The subprocess seam executes the actual layer builder in process. Planning is seeded;
this does not exercise live inference, the CLI preflight or whole-shot acceptance.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import flynn_agents_sdk as flynn
import pytest
from flynn_agents_sdk import deepseek

from tests.integration.test_flynn_image_layer import Adapter, fixture, materialize_image_layer, scripted_layer_critic
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder import unit_dispatch
from vfx_harness.application import run_shot
from vfx_harness.blender.session import BlenderSession
from vfx_harness.orchestration import run_owner_boundary, unit_state
from vfx_harness.orchestration.layer_publication import require_current_layer_publication


@pytest.mark.parametrize("medium", ["solid", "eevee"])
def test_driver_requires_current_image_publication_after_native_build(tmp_path, monkeypatch, medium):
    shot, layers, selected, layout, pending = fixture(tmp_path, monkeypatch, medium=medium)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'fixture-key')
    monkeypatch.setenv('VFXH_EXECUTABLE_BUILDER_MODEL', deepseek.VISION_MODEL)
    monkeypatch.setenv('VFXH_EXECUTABLE_BUILDER_MAX_STEPS', '8')
    monkeypatch.setenv('VFXH_EXECUTABLE_BUILDER_SECONDS', '240')
    monkeypatch.delenv('VFXH_RUN_MAX_USD', raising=False)
    executed = []

    class Provider:
        def __init__(self, **kwargs):
            self.adapter = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def plan_output(self, request, available):
            return flynn.OutputReservation('scripted', 0)

        async def generate(self, request):
            if self.adapter is None:
                card = json.loads(request.objective.split('[unit-scope]\n', 1)[1].split('\n\n[unit-plan]', 1)[0])
                executed.append(card['unit_id'])
                self.adapter = Adapter(tmp_path, card['unit_id'], medium=medium)
            return await self.adapter.generate(request)

    def forbidden(*args, **kwargs):
        raise AssertionError('Executable image production work reached the legacy unit engine')

    monkeypatch.setattr(deepseek, 'DeepSeekAdapter', Provider)
    monkeypatch.setattr(unit_dispatch.unit_loop, 'build_unit', forbidden)
    critic_calls = scripted_layer_critic(monkeypatch)
    args = SimpleNamespace(rounds=1, blender='blender', dry_run=False)
    started = time.monotonic()

    with run_owner_boundary.owned_root_run(layout, command='run', owner_kind='driver') as lease, BlenderSession(
        artifacts_dir=layout.scratch / 'worker', cwd=tmp_path,
    ) as session:
        def child(command, *, dry, tee):
            assert not dry
            assert command[2] == 'vfx_harness.agents.builder'
            lid = command[command.index('--layer') + 1]
            ledger = asyncio.run(layer_runtime.build_layer(
                shot, layers[lid], session, selected_authority=selected, verbose=False,
            ))
            assert ledger.status(layers[lid].as_milestone({})) == 'passed'
            return 0

        monkeypatch.setattr(run_shot, '_run', child)
        for lid in ('1', '2'):
            if lid == '2':
                selected, layers = materialize_image_layer(tmp_path, shot, selected, layout, pending)
            assert run_shot._build_layer_and_verify(
                args, shot, layout, lease, lid, 'python', layout.logs / 'driver.log', started,
            ) == 'passed'
        assert executed == ['camera', 'marker', 'lock']
        # Only the beauty fixture declares layer look; solid keeps executable form claims.
        assert critic_calls == ([240] if medium == "eevee" else [])
        publications = {lid: require_current_layer_publication(tmp_path, layer, selected)
                        for lid, layer in layers.items()}
        assert all(publication.receipt.final_status == 'passed' for publication in publications.values())
        completions = {lid: unit_state.load(tmp_path, lid) for lid in layers}
        assert all(row['completion_receipt'] for state in completions.values() for row in state['units'].values())
        camera_bytes = (tmp_path / layers['1'].script).read_bytes()

        # A successful process exit cannot certify substituted composed image bytes.
        (tmp_path / layers['2'].script).write_text('raise RuntimeError("injected substituted publication")\n')
        monkeypatch.setattr(run_shot, '_run', lambda *args, **kwargs: 0)
        with pytest.raises(SystemExit) as stopped:
            run_shot._build_layer_and_verify(
                args, shot, layout, lease, '2', 'python', layout.logs / 'driver.log', started,
            )
        assert stopped.value.code == 9
        assert json.loads((layout.root / 'status.json').read_text())['state'] == 'failed'
        assert (tmp_path / layers['1'].script).read_bytes() == camera_bytes
        assert {lid: unit_state.load(tmp_path, lid) for lid in layers} == completions
