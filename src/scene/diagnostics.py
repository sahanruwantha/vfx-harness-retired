"""Symbolic scene diagnostics fed back to the builder — so it can INSPECT, not guess.

The Hansa end-to-end run exposed the gap: the builder rendered black five times and cycled wrong
hypotheses (compositor → ops-context → back-facing normals) because it only ever saw the render,
never the scene state. A real person opens the scene and looks — is there geometry? a camera? are
the lights on? These pure helpers turn a ``get_scene_graph`` + ``image_stats`` into a one-line
digest and a black-frame diagnostic the builder can act on instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping


def scene_digest(graph: Mapping) -> str:
    """A one-line ground-truth summary of what actually got built (may differ from the code's intent
    if operators silently failed): object/mesh/light/camera counts, active camera, total light energy."""
    objects = graph.get("objects", []) or []
    scene = graph.get("scene", {}) or {}
    meshes = sum(1 for o in objects if o.get("type") == "MESH")
    lights = [o for o in objects if o.get("type") == "LIGHT"]
    cameras = sum(1 for o in objects if o.get("type") == "CAMERA")
    energy = round(sum(float((o.get("light") or {}).get("energy", 0.0)) for o in lights), 1)
    return (
        f"scene state: {len(objects)} objects ({meshes} mesh, {len(lights)} light, {cameras} camera); "
        f"active_camera={scene.get('active_camera')}; total_light_energy={energy}W; engine={scene.get('engine')}"
    )


def is_black(stats: Mapping, *, threshold: float = 0.03) -> bool:
    """True when the render's mean luma is near zero — a render-pipeline failure, not composition."""
    return float(stats.get("luma_mean", 1.0)) < threshold


def black_frame_hint(digest: str) -> str:
    """The actionable feedback for a black render: what's in the scene + where the real fault is."""
    return (
        "DIAGNOSTIC — your last render was almost entirely BLACK. " + digest + ". "
        "If objects, a camera and lights exist above but the frame is black, the fault is the RENDER "
        "PIPELINE, not missing geometry: a leftover compositor, emission/exposure too low, or the "
        "camera not pointing at the lit subject. Do NOT touch the compositor. Raise emission and "
        "key-light energy hard, set the 'Standard' or 'AgX' view transform at exposure 0, and confirm "
        "the camera frames the lit subject."
    )
