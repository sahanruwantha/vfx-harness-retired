from vfx_harness.blender.black_frame_report import (
    authored_density_values,
    effective_volume_span,
    format_black_frame_context,
    same_density,
    summarize_black_placement_search,
    summarize_density_probe,
)
from vfx_harness.blender.tools import _pending_black_frame_probe


def test_black_frame_context_routes_high_world_density_to_log_sweep() -> None:
    report = format_black_frame_context(
        frame=72,
        camera_clip_end=1400.0,
        volume_rows=[
            {
                "name": "Volume Scatter",
                "role": "lookdev.atmosphere_volume_scatter",
                "linked": False,
                "density": 0.02,
            }
        ],
        background_strength=0.15,
        lights=[
            {
                "name": "lighting_rig",
                "role": "world.lighting_rig",
                "energy": 30.0,
                "camera_distance": 225.0,
            }
        ],
    )

    assert "Density=0.02" in report
    assert "Density×effective_volume_span=28" in report
    assert "a contract ceiling is not a calibrated target" in report
    assert "probe_control(graph='world'" in report
    assert "1e-05" in report
    assert "frame=72" in report
    assert "energy=30" in report


def test_black_frame_context_exposes_canonical_volumetric_depth_coverage() -> None:
    report = format_black_frame_context(
        frame=150,
        camera_clip_end=20000.0,
        volume_rows=[
            {
                "name": "Volume Scatter",
                "role": "lookdev.atmosphere.volume",
                "linked": False,
                "density": 0.02,
            }
        ],
        background_strength=0.015,
        lights=[],
        volumetric_start=0.1,
        volumetric_end=100.0,
        subjects=[
            {"name": "fg", "role": "cam.blockout_fg", "camera_distance": 180.0},
            {"name": "bg", "role": "cam.blockout_bg", "camera_distance": 1330.0},
        ],
    )

    assert "eevee volumetric range=0.1..100" in report
    assert "cam.blockout_fg@180" in report
    assert "cam.blockout_bg@1330" in report
    assert "renderer depth coverage, not a density target" in report
    assert "Density×effective_volume_span=1.998" in report
    assert "span=99.9" in report


def test_effective_volume_span_is_not_camera_clip_range() -> None:
    assert effective_volume_span(20000.0, 0.1, 100.0) == 99.9
    assert effective_volume_span(50.0, 0.1, 100.0) == 50.0


def test_black_frame_context_does_not_prescribe_density_without_volume() -> None:
    report = format_black_frame_context(
        frame=10,
        camera_clip_end=1000.0,
        volume_rows=[],
        background_strength=None,
        lights=[],
    )

    assert "no active World volume node found" in report
    assert "probe_control" not in report


def test_authored_density_values_finds_socket_and_helper_literals() -> None:
    script = """
world = bpy.context.scene.world
nodes = world.node_tree.nodes
node = nodes['Volume Scatter']
node.inputs['Density'].default_value = 1e-5
bvfx_volumetric_world(color=(0, 0, 0), density=0.02)
other.inputs['Strength'].default_value = 4.0
"""

    assert authored_density_values(script) == (1e-5, 0.02)


def test_authored_density_values_does_not_misclassify_material_volume() -> None:
    script = """
obj = bpy.data.objects['domain']
material = obj.data.materials[0]
volume = material.node_tree.nodes['Principled Volume']
volume.inputs['Density'].default_value = 0.05
"""

    assert authored_density_values(script) == ()


def test_density_probe_closes_a_non_explanatory_black_frame_branch() -> None:
    diagnosis = summarize_density_probe(
        [
            {"mean": 0.32, "ref_mean": 81.27},
            {"mean": 0.18, "ref_mean": 81.27},
        ]
    )

    assert "DENSITY HYPOTHESIS CLOSED" in diagnosis
    assert "Do not repeat this sweep" in diagnosis
    assert "energy schedule intact" in diagnosis


def test_repeated_black_placements_close_only_after_distinct_scale_coverage() -> None:
    samples = [
        {"camera_distance": 120.0, "mean": 0.2, "black_pct": 99.0},
        {"camera_distance": 60.0, "mean": 0.5, "black_pct": 98.0},
    ]
    assert summarize_black_placement_search(samples) == ""
    samples.append({"camera_distance": 25.0, "mean": 0.8, "black_pct": 96.0})
    assert "LIGHT-PLACEMENT SEARCH CLOSED" in summarize_black_placement_search(samples)


def test_black_placement_search_stays_open_when_signal_improves() -> None:
    samples = [
        {"camera_distance": 120.0, "mean": 0.2, "black_pct": 99.0},
        {"camera_distance": 60.0, "mean": 0.5, "black_pct": 98.0},
        {"camera_distance": 20.0, "mean": 8.0, "black_pct": 70.0},
    ]
    assert summarize_black_placement_search(samples) == ""


def test_completed_density_measurement_retires_stale_black_frame_guard() -> None:
    state = {
        "black_frame_required_probe": {
            "role": "lookdev.atmosphere_volume_scatter",
            "frame": 150,
            "density": 0.05000000074505806,
        },
        "world_density_probes": {
            "lookdev.atmosphere_volume_scatter@150": [0.001, 0.05, 0.1]
        },
    }

    assert _pending_black_frame_probe(state) is None
    assert "black_frame_required_probe" not in state


def test_density_equality_accepts_blender_float32_readback_but_not_new_value() -> None:
    assert same_density(0.05, 0.05000000074505806)
    assert same_density(1e-5, 9.999999747378752e-06)
    assert not same_density(0.05, 0.051)


def test_unmeasured_density_keeps_black_frame_guard_closed() -> None:
    state = {
        "black_frame_required_probe": {
            "role": "lookdev.atmosphere_volume_scatter",
            "frame": 72,
            "density": 0.02,
        },
        "world_density_probes": {
            "lookdev.atmosphere_volume_scatter@72": [0.001, 0.01]
        },
    }

    assert _pending_black_frame_probe(state) == state["black_frame_required_probe"]
