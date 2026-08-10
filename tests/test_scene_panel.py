"""Atmosphere panel + compositor bloom: pure scoring, code generation, and the search with fakes."""

from __future__ import annotations

from scene.compositor import bloom_compositor_code
from scene.panel import (
    ATMOSPHERE_PRESETS,
    preset_overrides_code,
    run_atmosphere_panel,
    signature_distance,
)


def _sig(mean, luma, std, hist=None):
    return {"mean_rgb": mean, "luma_mean": luma, "luma_std": std, "hist8": hist or [0.125] * 8}


# --- compositor bloom ---------------------------------------------------------------


def test_bloom_code_wires_a_glare_node():
    code = bloom_compositor_code(glare_type="Bloom", threshold=0.7, size=0.7, strength=0.4)
    assert "CompositorNodeGlare" in code
    assert "glare.inputs['Type'].default_value = 'Bloom'" in code
    assert "compositing_node_group" in code  # Blender 5.x node-group compositor
    assert "NodeGroupOutput" in code
    assert "scene.use_nodes = True" in code  # required or the compositor is ignored on render


def test_bloom_code_reuses_group_for_idempotence():
    code = bloom_compositor_code()
    assert "group.nodes.remove(node)" in code  # clears prior nodes rather than stacking


# --- signature distance -------------------------------------------------------------


def test_signature_distance_is_zero_for_identical():
    s = _sig([0.1, 0.4, 0.2], 0.3, 0.25)
    assert signature_distance(s, s) == 0.0


def test_signature_distance_orders_by_closeness():
    ref = _sig([0.05, 0.3, 0.1], 0.2, 0.3)
    near = _sig([0.06, 0.31, 0.11], 0.22, 0.28)
    far = _sig([0.8, 0.8, 0.8], 0.8, 0.05)  # bright, flat — opposite of the dark high-contrast ref
    assert signature_distance(near, ref) < signature_distance(far, ref)


# --- preset overrides code ----------------------------------------------------------


def test_preset_overrides_code_sets_transform_exposure_and_scales():
    code = preset_overrides_code(ATMOSPHERE_PRESETS[0])
    assert "vs.view_transform =" in code
    assert "vs.exposure =" in code
    assert "obj.data.energy *=" in code  # light scaling
    assert "default_value *=" in code  # world strength scaling


# --- the panel search ---------------------------------------------------------------


class _FakePanelBridge:
    """Renders nothing; returns preset-specific stats keyed by the output filename."""

    def __init__(self, stats_by_name):
        self.stats_by_name = stats_by_name
        self.opened = 0
        self.bloom_applied = 0

    def open_blend(self, **_p):
        self.opened += 1
        return {"opened": True}

    def run_python(self, code):
        if "CompositorNodeGlare" in code:
            self.bloom_applied += 1
        return {"ok": True, "result": {}}

    def render(self, **p):
        return {"path": p["path"], "bytes": 1}

    def image_stats(self, **p):
        for name, stats in self.stats_by_name.items():
            if f"panel_{name}." in p["path"]:
                return stats
        raise AssertionError(f"no stats for {p['path']}")


def test_panel_picks_variant_closest_to_reference(tmp_path):
    reference = _sig([0.04, 0.28, 0.10], 0.18, 0.32)  # dark, green, high-contrast
    presets = [
        {"name": "bright_flat", "view_transform": "Standard", "look": "None", "exposure": 0.0,
         "world_mult": 1.0, "light_mult": 1.0, "bloom_threshold": 1.0, "bloom_size": 0.6, "bloom_strength": 0.25},
        {"name": "dark_match", "view_transform": "AgX", "look": "None", "exposure": -0.6,
         "world_mult": 0.4, "light_mult": 1.0, "bloom_threshold": 0.7, "bloom_size": 0.75, "bloom_strength": 0.4},
    ]
    stats = {
        "bright_flat": _sig([0.7, 0.7, 0.7], 0.72, 0.06),   # far from ref
        "dark_match": _sig([0.05, 0.27, 0.11], 0.2, 0.30),  # near ref
    }
    bridge = _FakePanelBridge(stats)

    result = run_atmosphere_panel(
        bridge=bridge, base_blend=tmp_path / "base.blend", reference_stats=reference,
        out_dir=tmp_path / "panel", presets=presets,
    )

    assert result.best.name == "dark_match"
    assert [p.name for p in result.picks] == ["dark_match", "bright_flat"]  # ranked nearest-first
    assert bridge.opened == 2  # base reopened per variant (no override stacking)
    assert bridge.bloom_applied == 2  # bloom applied to each
