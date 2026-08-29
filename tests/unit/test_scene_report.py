from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.blender.scene_report import (
    light_status_line,
    material_status_lines,
    render_status_lines,
)


class _Object(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def test_render_report_closes_common_eevee_read_only_probe() -> None:
    scene = SimpleNamespace(
        render=SimpleNamespace(
            engine="BLENDER_EEVEE",
            resolution_x=1920,
            resolution_y=1080,
            resolution_percentage=50,
            fps=24,
            use_motion_blur=True,
            motion_blur_shutter=0.5,
        ),
        frame_start=1,
        frame_end=250,
        camera=SimpleNamespace(name="camera"),
        view_settings=SimpleNamespace(look="AgX", exposure=0.0, gamma=1.0),
        display_settings=SimpleNamespace(display_device="sRGB"),
        compositing_node_group=SimpleNamespace(
            name="Compositor",
            nodes=[SimpleNamespace(bl_idname="CompositorNodeVecBlur")],
        ),
        eevee=SimpleNamespace(
            volumetric_start=1.0,
            volumetric_end=1600.0,
            volumetric_samples=32,
            use_volumetric_shadows=True,
            taa_render_samples=32,
        ),
        view_layers=[SimpleNamespace(use_pass_vector=True, use_pass_z=True)],
        world=SimpleNamespace(name="World"),
    )

    report = "\n".join(
        render_status_lines(
            scene, {"objects": 6, "mesh_objects": 3, "verts": 24, "tris": 36}
        )
    )

    assert "group=Compositor" in report
    assert "volumetric_start=1.0" in report
    assert "volumetric_end=1600.0" in report
    assert "vector_pass=True" in report
    assert "depth_pass=True" in report
    assert "world=World" in report


def test_light_report_includes_shape_size_and_cutoff() -> None:
    light = _Object(
        name="key",
        bvfx_role="world.lighting_rig",
        bvfx_owner_layer="2",
        data=SimpleNamespace(
            type="AREA",
            energy=30.0,
            color=(1.0, 0.7, 0.4),
            shape="RECTANGLE",
            size=14.0,
            size_y=10.0,
            use_custom_distance=False,
            cutoff_distance=40.0,
        ),
        hide_render=False,
        location=(0.0, 130.0, 25.0),
    )

    report = light_status_line(light)

    assert "shape=RECTANGLE" in report
    assert "size=14.0" in report
    assert "cutoff=40.0" in report


def test_material_status_names_object_slot_assignments() -> None:
    class Material(dict):
        name = "mat_primary"
        use_nodes = True

    material = Material(bvfx_role="material.primary", bvfx_owner_layer="1")
    slot = SimpleNamespace(material=material)
    scene = SimpleNamespace(
        objects=[
            SimpleNamespace(name="blockout_fg", material_slots=[slot]),
            SimpleNamespace(name="blockout_bg", material_slots=[slot]),
        ]
    )

    text = "\n".join(material_status_lines(scene, [material]))

    assert "mat_primary role=material.primary owner=1 nodes=True" in text
    assert "blockout_fg[0],blockout_bg[0]" in text
