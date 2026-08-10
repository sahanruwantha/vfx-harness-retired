"""The ``render_3d`` realizer: turn a 3D-mode beat into a rendered :class:`~develop.ledger.Clip`.

This is the 3D twin of :func:`develop.lock.make_footage_acquirer` — same seam, same output type —
but it *builds and renders* a scene instead of fetching a clip. The render is emitted as a JPEG
:class:`~footage.inspect.FrameSample`, so the existing sighted footage critic judges it as pixels
and the existing ``RE_SOURCE`` lever drives the refine loop with no new verdict machinery.

The scene construction is a pluggable **builder** — ``builder(spec, bridge) -> info`` — because a
build is multi-step and bridge-aware:

- :func:`build_graybox` (default, Phase 1): a lit, camera-framed placeholder. Proves the
  render→critique→refine loop with no external calls.
- :func:`make_reconstruction_builder` (Phase 2): the real pipeline — Higgsfield generates an image
  of the subject, Tripo turns it into a GLB, the bridge imports + normalizes it into a lit,
  framed scene. Generated assets are cached and reused across beats.

The Blender bridge is injected, and the blocking bridge I/O runs off the event loop via
``asyncio.to_thread`` so a render never stalls the scheduler's concurrent critics. Fakes for the
bridge, the image generator, and the mesh converter make the whole leaf testable without Blender,
Higgsfield, or Tripo. See ``docs/3d-agent-architecture.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from develop.agents import Render3D
from develop.ledger import BeatEntry, Clip
from develop.lock import estimate_seconds
from develop.ledger import FrameSample
from scene.higgsfield import generate_image as _default_generate_image
from scene.tripo import image_to_glb as _default_image_to_glb

# What the bridge must offer the realizer — the real BlenderBridge and test fakes both satisfy it.
Bridge = object


class SceneBuildError(RuntimeError):
    """A builder could not construct the scene — surfaced to the critic as an acquisition gap."""


@dataclass(frozen=True)
class SceneSpec:
    """What a 3D beat needs reconstructed, adapted from a frozen ``BeatEntry`` + director refs."""

    beat_id: str
    subject: str  # the real place/object to rebuild (intent.subject)
    evidence: str  # factual constraints from the dossier (intent.evidence)
    vo: str  # what the shot must illustrate on screen (realization.vo)
    references: tuple[Path, ...]  # real photos of the thing, from beat_dir/refs/
    duration_s: int  # screen time → how much to build (estimate_seconds(vo))
    prev_visual: str = ""  # cut-from continuity


# A builder constructs the scene in the live bridge session and returns metadata for render_meta.
SceneBuilder = Callable[["SceneSpec", Bridge], Mapping[str, object]]


def scene_spec_from_beat(entry: BeatEntry, *, refs_dir: Path | None = None, prev_visual: str = "") -> SceneSpec:
    """Adapt a frozen beat into a SceneSpec, picking up any reference images from ``refs_dir``."""
    vo = entry.realization.vo if entry.realization else ""
    refs: tuple[Path, ...] = ()
    if refs_dir is not None and refs_dir.is_dir():
        refs = tuple(sorted(p for p in refs_dir.iterdir() if p.is_file()))
    return SceneSpec(
        beat_id=entry.id,
        subject=entry.intent.subject or entry.intent.heading,
        evidence=entry.intent.evidence,
        vo=vo,
        references=refs,
        duration_s=estimate_seconds(vo),
        prev_visual=prev_visual,
    )


# --- gray-box builder (Phase 1 default) ---------------------------------------------


def default_graybox(spec: SceneSpec) -> str:
    """The gray-box bpy code: a lit, camera-framed placeholder standing in for the subject."""
    subject = spec.subject[:80]
    return f"""
import bpy
from mathutils import Vector

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

bpy.ops.mesh.primitive_plane_add(size=14, location=(0, 0, 0))
bpy.context.active_object.name = "Ground"

# a placeholder mass standing in for the reconstructed subject
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
bpy.context.active_object.name = "Subject"

bpy.ops.object.light_add(type='SUN', location=(5, -5, 9))
bpy.context.active_object.data.energy = 4.0

bpy.ops.object.camera_add(location=(7, -7, 4.5))
cam = bpy.context.active_object
cam.name = "ShotCam"
direction = Vector((0, 0, 1)) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
bpy.context.scene.camera = cam

result = {{"subject": {subject!r}, "objects": [o.name for o in bpy.context.scene.objects]}}
"""


def build_graybox(spec: SceneSpec, bridge: Bridge) -> Mapping[str, object]:
    """Default builder: run the gray-box code. Asset-free, so it needs no Higgsfield/Tripo."""
    res = bridge.run_python(default_graybox(spec))
    if not res.get("ok"):
        raise SceneBuildError(f"gray-box build failed: {str(res.get('error', '')).strip()[-300:]}")
    return {"build": "graybox"}


# --- reconstruction builder (Phase 2: Higgsfield → Tripo → import → normalize) -------


def asset_prompt(spec: SceneSpec) -> str:
    """The image prompt for the beat's subject — a clean, single-object product shot Tripo can lift."""
    subject = spec.subject.strip().rstrip(".")
    return (
        f"{subject}, photorealistic, studio product photograph, single object, "
        "plain seamless neutral grey background, centered, entire object visible, "
        "soft even lighting, no people, no text"
    )


def _cache_key(prompt: str, references: Sequence[str]) -> str:
    """A stable id for (prompt + refs) so the same asset is generated once and reused across beats."""
    digest = hashlib.sha1(prompt.encode("utf-8"))  # noqa: S324 - cache key, not security
    for ref in sorted(references):
        digest.update(b"|")
        digest.update(ref.encode("utf-8"))
    return digest.hexdigest()[:16]


def asset_stage_code(target_height: float) -> str:
    """bpy code to clear the scene and set a ground, two lights, and a camera framed for an object
    of ``target_height`` sitting centered on the ground (the pose ``normalize_asset`` produces)."""
    h = float(target_height)
    px = round((2.4 * h + 1.6) * 0.72, 3)
    cam_z = round(h * 0.85 + 0.25, 3)
    look_z = round(h * 0.45, 3)
    return f"""
import bpy
from mathutils import Vector

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

bpy.ops.mesh.primitive_plane_add(size=24, location=(0, 0, 0))
bpy.context.active_object.name = "Ground"

bpy.ops.object.light_add(type='AREA', location=({px}, -{px}, {round(h * 2.5 + 2, 3)}))
_key = bpy.context.active_object.data
_key.energy = 1200.0
_key.size = 8.0
bpy.ops.object.light_add(type='SUN', location=(-4, 3, 7))
bpy.context.active_object.data.energy = 2.0

bpy.ops.object.camera_add(location=({px}, -{px}, {cam_z}))
cam = bpy.context.active_object
cam.name = "ShotCam"
direction = Vector((0, 0, {look_z})) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
bpy.context.scene.camera = cam

result = {{"ok": True}}
"""


def make_reconstruction_builder(
    *,
    asset_dir: str | Path,
    generate_image: Callable[..., Path] = _default_generate_image,
    image_to_glb: Callable[..., Path] = _default_image_to_glb,
    target_height: float = 1.5,
    aspect_ratio: str = "1:1",
    quality: str | None = None,
    stage_fn: Callable[[float], str] = asset_stage_code,
) -> SceneBuilder:
    """A builder that reconstructs the subject: Higgsfield image → Tripo GLB → import → normalize.

    Generated assets are cached under *asset_dir* keyed by (prompt + references), so a subject that
    recurs across beats is generated once. ``generate_image`` / ``image_to_glb`` are injectable so
    the builder is testable without the CLI, the network, or Blender.
    """

    def build(spec: SceneSpec, bridge: Bridge) -> Mapping[str, object]:
        assets = Path(asset_dir)
        assets.mkdir(parents=True, exist_ok=True)
        prompt = asset_prompt(spec)
        references = [str(r) for r in spec.references]
        key = _cache_key(prompt, references)
        glb = assets / f"{key}.glb"

        cached = glb.exists()
        if not cached:
            try:
                image = generate_image(
                    prompt,
                    assets / f"{key}.png",
                    image_references=references or None,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                )
                image_to_glb(image, glb)
            except Exception as exc:  # gen/convert failure is an acquisition gap, not a crash
                raise SceneBuildError(f"asset generation failed for {spec.subject!r}: {exc}") from exc

        staged = bridge.run_python(stage_fn(target_height))
        if not staged.get("ok"):
            raise SceneBuildError(f"stage failed: {str(staged.get('error', '')).strip()[-300:]}")
        imported = bridge.import_glb(path=str(glb))
        if not imported.get("roots"):
            raise SceneBuildError(f"imported GLB had no objects for {spec.subject!r}")
        norm = bridge.normalize_asset(names=imported["roots"], target_height=target_height)
        return {
            "build": "reconstruction",
            "prompt": prompt,
            "glb": str(glb),
            "cached": cached,
            "mesh_count": imported.get("mesh_count", 0),
            "scale_factor": norm.get("scale_factor"),
        }

    return build


# --- the leaf -----------------------------------------------------------------------


def make_scene_builder(
    *,
    bridge: Bridge,
    out_dir: str | Path,
    builder: SceneBuilder = build_graybox,
    refs_subdir: str = "refs",
    resolution: tuple[int, int] = (960, 540),
    samples: int = 32,
) -> Render3D:
    """A ``render_3d`` leaf bound to a started Blender *bridge*, writing per-beat under *out_dir*.

    *builder* constructs the scene (gray-box by default, or a reconstruction). A build failure →
    a Clip carrying only the acquisition gap (no frames), which the footage critic reads as
    RE_SOURCE. Caller owns the bridge lifecycle, exactly as the footage acquirer is handed an
    ``out_dir`` it does not own.
    """

    def _render_sync(entry: BeatEntry) -> Clip:
        beat_dir = Path(out_dir) / entry.id
        beat_dir.mkdir(parents=True, exist_ok=True)
        spec = scene_spec_from_beat(entry, refs_dir=beat_dir / refs_subdir)

        try:
            info = builder(spec, bridge)
        except SceneBuildError as exc:
            return Clip(licence="KNOWN", acquisition_gap=f"3D build failed for {spec.subject!r}: {exc}")

        saved = bridge.save_blend(path=str(beat_dir / f"{entry.id}.blend"))
        blend_path = Path(saved["path"]) if saved.get("bytes") else None

        shot = bridge.render(
            path=str(beat_dir / f"{entry.id}.jpg"),
            format="JPEG",
            resolution=list(resolution),
            samples=samples,
            return_base64=True,
        )
        image_b64 = shot.get("image_b64")
        if not image_b64:
            return Clip(
                licence="KNOWN",
                blend_path=blend_path,
                acquisition_gap=f"3D render produced no image for {spec.subject!r}",
            )
        frames = (FrameSample(timecode="00:00", seconds=0.0, jpeg_b64=image_b64),)
        render_meta = {"engine": str(shot.get("engine", "")), "subject": spec.subject}
        render_meta.update({k: str(v) for k, v in (info or {}).items()})
        return Clip(
            fetched_path=Path(shot["path"]),
            licence="KNOWN",  # a self-authored render is clearable by construction
            frames=frames,
            blend_path=blend_path,
            render_meta=render_meta,
        )

    async def render(entry: BeatEntry) -> Clip:
        return await asyncio.to_thread(_render_sync, entry)

    return render
