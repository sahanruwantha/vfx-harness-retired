"""Atmosphere panel — turn the thrashing atmosphere pass into a search.

The traced run showed the builder guessing exposure / fog / light / bloom blind and oscillating
(0.48 → 0.38 → 0.33 → 0.55). This replaces the guess with a *search*: hold the built structure
fixed, render a spread of atmosphere variants (view transform, exposure, world + light scale,
bloom), and keep the one whose palette/contrast SIGNATURE is closest to the reference — scored by
:func:`scene.server.cmd_image_stats`, no LLM per variant. Each variant starts from the same base
``.blend`` (reopened) so overrides never stack. See ``docs/3d-agent-architecture.md`` §6.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scene.compositor import bloom_compositor_code

# Atmosphere variants spanning dark/high-contrast/bloomy (what the Silk Road reference wants) to
# neutral. Generic overrides only — no object-name dependencies, so they work on any built scene.
ATMOSPHERE_PRESETS: list[dict] = [
    {"name": "agx_punchy_dark", "view_transform": "AgX", "look": "AgX - Punchy", "exposure": -0.3,
     "world_mult": 0.5, "light_mult": 0.9, "bloom_threshold": 0.7, "bloom_size": 0.75, "bloom_strength": 0.4},
    {"name": "standard_highcontrast", "view_transform": "Standard", "look": "High Contrast", "exposure": -0.6,
     "world_mult": 0.4, "light_mult": 1.0, "bloom_threshold": 0.8, "bloom_size": 0.7, "bloom_strength": 0.35},
    {"name": "agx_base", "view_transform": "AgX", "look": "None", "exposure": -0.2,
     "world_mult": 0.7, "light_mult": 1.0, "bloom_threshold": 0.8, "bloom_size": 0.6, "bloom_strength": 0.3},
    {"name": "standard_dark", "view_transform": "Standard", "look": "None", "exposure": -1.0,
     "world_mult": 0.3, "light_mult": 1.1, "bloom_threshold": 0.6, "bloom_size": 0.8, "bloom_strength": 0.5},
    {"name": "glow_heavy", "view_transform": "AgX", "look": "AgX - Greater Contrast", "exposure": -0.4,
     "world_mult": 0.5, "light_mult": 1.2, "bloom_threshold": 0.5, "bloom_size": 0.85, "bloom_strength": 0.6},
    {"name": "balanced", "view_transform": "Standard", "look": "None", "exposure": 0.0,
     "world_mult": 1.0, "light_mult": 1.0, "bloom_threshold": 1.0, "bloom_size": 0.6, "bloom_strength": 0.25},
]


def preset_overrides_code(preset: Mapping) -> str:
    """bpy that applies one atmosphere preset's view-transform / exposure / world / light scaling.

    Generic and guarded — an unsupported look falls back to 'None' rather than raising."""
    return f"""
import bpy
scene = bpy.context.scene
vs = scene.view_settings
try:
    vs.view_transform = {preset["view_transform"]!r}
except Exception:
    pass
try:
    vs.look = {preset["look"]!r}
except Exception:
    vs.look = 'None'
vs.exposure = {preset["exposure"]}
if scene.world and scene.world.use_nodes:
    for node in scene.world.node_tree.nodes:
        if node.type == 'BACKGROUND':
            node.inputs[1].default_value *= {preset["world_mult"]}
for obj in scene.objects:
    if obj.type == 'LIGHT':
        obj.data.energy *= {preset["light_mult"]}
result = {{"preset": {preset["name"]!r}}}
"""


def signature_distance(
    a: Mapping, b: Mapping, *, w_rgb: float = 1.0, w_luma: float = 1.5, w_contrast: float = 2.0, w_hist: float = 1.0
) -> float:
    """Weighted distance between two image signatures. Emphasises contrast + luma (the axes the
    Silk Road builder kept missing) over raw colour, then the luma histogram shape."""
    dist = w_rgb * sum((a["mean_rgb"][i] - b["mean_rgb"][i]) ** 2 for i in range(3))
    dist += w_luma * (a["luma_mean"] - b["luma_mean"]) ** 2
    dist += w_contrast * (a["luma_std"] - b["luma_std"]) ** 2
    dist += w_hist * sum((x - y) ** 2 for x, y in zip(a["hist8"], b["hist8"]))
    return math.sqrt(dist)


@dataclass
class PanelPick:
    name: str
    path: str
    distance: float
    stats: dict


@dataclass
class PanelResult:
    picks: list[PanelPick]  # sorted nearest-to-reference first

    @property
    def best(self) -> PanelPick | None:
        return self.picks[0] if self.picks else None


def run_atmosphere_panel(
    *,
    bridge,
    base_blend: str | Path,
    reference_stats: Mapping,
    out_dir: str | Path,
    presets: Sequence[Mapping] = ATMOSPHERE_PRESETS,
    resolution: tuple[int, int] = (768, 432),
    samples: int = 48,
    on_progress: Callable[[str], None] | None = None,
) -> PanelResult:
    """Render each preset from a fresh copy of *base_blend*, add bloom, and score its signature
    against *reference_stats*. Returns the variants ranked nearest-first."""
    log = on_progress or (lambda _m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    picks: list[PanelPick] = []
    for preset in presets:
        name = preset["name"]
        bridge.open_blend(path=str(base_blend))  # restore the base scene so overrides never stack
        applied = bridge.run_python(preset_overrides_code(preset))
        if not applied.get("ok"):
            log(f"panel {name}: overrides failed — skipped")
            continue
        bloom = bridge.run_python(
            bloom_compositor_code(
                threshold=preset["bloom_threshold"], size=preset["bloom_size"], strength=preset["bloom_strength"]
            )
        )
        if not bloom.get("ok"):  # bloom is enhancement, not required — render the variant anyway
            log(f"panel {name}: bloom failed, rendering without it")
        path = out / f"panel_{name}.jpg"
        bridge.render(path=str(path), format="JPEG", resolution=list(resolution), samples=samples)
        stats = bridge.image_stats(path=str(path))
        distance = signature_distance(stats, reference_stats)
        picks.append(PanelPick(name=name, path=str(path), distance=distance, stats=stats))
        log(f"panel {name}: distance {distance:.4f}")

    picks.sort(key=lambda pick: pick.distance)
    return PanelResult(picks=picks)
