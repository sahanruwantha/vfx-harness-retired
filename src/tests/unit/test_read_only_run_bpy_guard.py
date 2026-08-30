"""Raw Blender queries must not consume authored mutation authority."""

from __future__ import annotations

import ast

from vfx_harness.agents.guardrails import (
    _run_bpy_has_authored_mutation,
    _run_bpy_unscoped_scene_property_write_lines,
)
from vfx_harness.blender.tools import (
    _black_search_stop_message,
    _closed_density_repeat_message,
    _probe_values_with_original,
    _render_setting_writes,
    _schedule_override_without_keying,
    _scoped_renderer_write_error,
)


def _has_mutation(source: str) -> bool:
    return _run_bpy_has_authored_mutation(ast.parse(source))


def test_camera_and_node_queries_are_not_authored_mutations() -> None:
    assert not _has_mutation(
        """
sc = bpy.context.scene
sc.frame_set(150)
info = {}
info['camera'] = tuple(sc.camera.matrix_world.translation)
RESULT = info
"""
    )
    assert not _has_mutation(
        "loc = bpy.context.scene.camera.matrix_world.translation.copy()\nRESULT = tuple(loc)"
    )
    assert not _has_mutation(
        """
glare = bpy.data.node_groups['comp'].nodes['Glare']
RESULT = {inp.name: inp.default_value for inp in glare.inputs}
"""
    )


def test_common_scene_writes_remain_legal_mutation_payloads() -> None:
    assert _has_mutation("hero.location = (1, 2, 3)\nRESULT = hero.location")
    assert _has_mutation("obj['custom'] = 1\nRESULT = 1")
    assert _has_mutation("ld.keyframe_insert('energy', frame=150)\nRESULT = 1")
    assert _has_mutation("mat = bpy.data.materials.new('owned')\nRESULT = mat.name")
    assert _has_mutation("bvfx_glare_bloom(threshold=1, size=9, strength=2)\nRESULT = 1")


def test_local_result_accumulation_is_not_mistaken_for_scene_write() -> None:
    assert not _has_mutation("RESULT = {}\nRESULT['energy'] = 20\n")


def test_scene_custom_property_cannot_launder_a_read_only_probe() -> None:
    direct = ast.parse(
        "bpy.context.scene['debug_probe'] = 1\n"
        "RESULT = tuple(bpy.data.objects['hero'].bound_box)"
    )
    aliased = ast.parse(
        "scene = bpy.context.scene\n"
        "sc = scene\n"
        "sc['debug_probe'] = sc.get('debug_probe', 0) + 1\n"
        "RESULT = tuple(bpy.data.objects['hero'].bound_box)"
    )

    assert _run_bpy_unscoped_scene_property_write_lines(direct) == [1]
    assert _run_bpy_unscoped_scene_property_write_lines(aliased) == [3]
    assert not _run_bpy_has_authored_mutation(direct)
    assert not _run_bpy_has_authored_mutation(aliased)
    assert _has_mutation("obj['custom'] = 1\nRESULT = 1")


def test_passing_schedule_rejects_live_override_but_allows_rekeying() -> None:
    protected = {"data.energy"}
    assert _schedule_override_without_keying("d.energy = 30000", protected) == protected
    assert _schedule_override_without_keying("curve.mute = True", protected) == protected
    assert not _schedule_override_without_keying(
        "d.energy = 450\nd.keyframe_insert(data_path='energy', frame=150)", protected
    )


def test_passing_schedule_rejects_keyframe_point_coordinate_edits() -> None:
    protected = {"data.energy"}
    helper_bypass = """
fcs = bvfx_fcurves(rig.data)
for fc in fcs:
    for kp in fc.keyframe_points:
        kp.co[1] = 20000
"""
    direct_bypass = "fc.keyframe_points[0].co[1] = 20000"
    assert _schedule_override_without_keying(helper_bypass, protected) == protected
    assert _schedule_override_without_keying(direct_bypass, protected) == protected


def test_control_probe_always_includes_restored_live_value() -> None:
    assert _probe_values_with_original([0.0, 0.001, 0.01], 0.008) == [0.0, 0.001, 0.008, 0.01]
    full = _probe_values_with_original([0.0, 0.001, 0.002, 0.003, 0.004, 0.006, 0.01, 0.1], 0.008)
    assert len(full) == 8
    assert 0.008 in full


def test_scoped_run_bpy_detects_renderer_setting_writes_through_aliases() -> None:
    assert _render_setting_writes(
        "sc = bpy.context.scene\nsc.eevee.use_raytracing = True"
    ) == {"eevee.use_raytracing"}
    assert _render_setting_writes(
        "sc = bpy.context.scene\nee = sc.eevee\nee.light_threshold = 0.0"
    ) == {"light_threshold"}
    assert _render_setting_writes(
        "setattr(bpy.context.scene.render, 'engine', 'BLENDER_EEVEE')"
    ) == {"engine"}


def test_renderer_reads_and_semantic_writes_are_not_renderer_setting_writes() -> None:
    assert not _render_setting_writes(
        "sc = bpy.context.scene\nRESULT = getattr(sc.eevee, 'light_threshold', None)"
    )
    assert not _render_setting_writes(
        "light.data.energy = 450\nlight.keyframe_insert(data_path='energy', frame=150)"
    )


def test_renderer_write_guard_is_bound_only_for_scoped_units() -> None:
    script = "sc = bpy.context.scene\nsc.eevee.light_threshold = 0.0"
    assert "BLOCKED" in _scoped_renderer_write_error(script, ("world.lighting_rig",))
    assert "cannot_express_in_scope" in _scoped_renderer_write_error(
        script, ("world.lighting_rig",)
    )
    assert _scoped_renderer_write_error(script, None) == ""


def test_closed_black_search_names_exact_typed_transition() -> None:
    state = {
        "black_frame_search_exhausted": {
            "frame": 72,
            "role": "world.lighting_rig",
            "contract_ids": ["blacks-f072"],
            "reason": "LIGHT-PLACEMENT SEARCH CLOSED",
        }
    }

    message = _black_search_stop_message(state)

    assert "No further render, probe, or scene mutation is legal" in message
    assert "cannot_express_in_scope(contract_ids=['blacks-f072']" in message


def test_closed_density_branch_rejects_only_already_measured_repeat() -> None:
    state = {
        "world_density_probes": {"lookdev.atmosphere.volume_scatter@150": [0.02, 0.15]},
        "world_density_probe_diagnoses": {
            "lookdev.atmosphere.volume_scatter@150": "DENSITY HYPOTHESIS CLOSED: measured floor"
        },
    }
    repeated = _closed_density_repeat_message(
        state,
        role="lookdev.atmosphere.volume_scatter",
        frame=150,
        values=[0.02, 0.15],
    )
    assert "do not repeat the sweep" in repeated
    assert "cannot_express_in_scope" in repeated
    assert not _closed_density_repeat_message(
        state,
        role="lookdev.atmosphere.volume_scatter",
        frame=150,
        values=[0.02, 0.3],
    )
