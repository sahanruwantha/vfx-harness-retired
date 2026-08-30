from __future__ import annotations

from types import SimpleNamespace

from vfx_harness.blender.node_report import format_node_tree


def _socket(name: str, *, linked: bool = False, value=None):
    row = {"name": name, "is_linked": linked}
    if value is not None:
        row["default_value"] = value
    return SimpleNamespace(**row)


def test_node_report_enumerates_render_layer_outputs_without_mutation_probe() -> None:
    render_layers = SimpleNamespace(
        type="R_LAYERS",
        name="Render Layers",
        inputs=[],
        outputs=[_socket("Image"), _socket("Depth"), _socket("Vector")],
    )
    vector_blur = SimpleNamespace(
        type="VECBLUR",
        name="Vector Blur",
        inputs=[_socket("Image", linked=True), _socket("Samples", value=16)],
        outputs=[_socket("Image")],
    )
    link = SimpleNamespace(
        from_node=render_layers,
        from_socket=render_layers.outputs[0],
        to_node=vector_blur,
        to_socket=vector_blur.inputs[0],
    )
    tree = SimpleNamespace(nodes=[render_layers, vector_blur], links=[link])

    report = format_node_tree(tree, "compositor")

    assert "[R_LAYERS] Render Layers | outputs=[Image, Depth, Vector]" in report
    assert "Samples=16" in report
    assert "Render Layers.Image → Vector Blur.Image" in report


def test_node_report_preserves_small_nonzero_socket_values() -> None:
    volume = SimpleNamespace(
        type="PRINCIPLED_VOLUME",
        name="Principled Volume",
        inputs=[
            _socket("Density", value=1e-5),
            _socket("Anisotropy", value=0.350000001),
            _socket("Color", value=(0.45, 0.35, 0.28, 1.0)),
        ],
        outputs=[_socket("Volume")],
    )
    tree = SimpleNamespace(nodes=[volume], links=[])

    report = format_node_tree(tree, "world")

    assert "Density=1e-05" in report
    assert "Density=0.0" not in report
    assert "Anisotropy=0.35" in report
    assert "Color=(0.45, 0.35, 0.28, 1)" in report
