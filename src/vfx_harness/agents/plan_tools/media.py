"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vfx_harness.agents.plan_tools.constants import _MAX_TILES, _SPIKE_CONTRACT_MARKER
from vfx_harness.evidence.checks import METRICS
from vfx_harness.evidence.scene_checks import _blender_probe


def _metric_list() -> str:
    """The metric vocabulary, GENERATED from the registry.

    It was hardcoded into the tool descriptions, so `region_lit_variance` existed in
    METRICS and was advertised nowhere — the same shape as contact_sheet being defined and
    never registered. A capability nothing names is indistinguishable from one that does
    not exist.
    """

    return " · ".join(sorted(METRICS))


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _sh(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- #
# video forensics (sync workers, called via anyio.to_thread)                   #
# --------------------------------------------------------------------------- #
def _probe(video: Path) -> dict:
    rc, out, err = _sh(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(video),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip()[:300]}")
    st = json.loads(out)["streams"][0]
    num, den = (st.get("r_frame_rate") or "25/1").split("/")
    fps = float(num) / float(den or 1)
    frames = int(st.get("nb_frames") or 0) or int(float(st.get("duration") or 0) * fps)
    return {
        "width": st.get("width"),
        "height": st.get("height"),
        "fps": fps,
        "frames": frames,
        "duration": float(st.get("duration") or 0),
    }


def _sheet(video: Path, start: int, end: int, step: int, out_dir: Path) -> tuple[Path, list[int]]:
    shown = list(range(start, end + 1, step))[:_MAX_TILES]
    end = shown[-1]
    rows = -(-len(shown) // 5)  # ceil
    vf = (
        f"drawtext=text='%{{frame_num}}':x=10:y=10:fontsize=48:fontcolor=yellow:"
        f"borderw=3:bordercolor=black,"
        f"select='between(n\\,{start}\\,{end})*not(mod(n-{start}\\,{step}))',"
        f"scale=380:-2,tile=5x{rows}"
    )
    out = out_dir / f"sheet_{start}_{end}_{step}.png"
    rc, _, err = _sh(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", vf, "-vsync", "0", "-frames:v", "1", str(out)],
        timeout=180,
    )
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg sheet failed: {err.strip()[:300]}")
    return out, shown


def _frame(video: Path, n: int, fps: float, out_dir: Path) -> Path:
    out = out_dir / f"f{n:05d}.png"
    rc, _, err = _sh(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{n / fps:.4f}", "-i", str(video), "-frames:v", "1", str(out)],
        timeout=60,
    )
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg frame {n} failed: {err.strip()[:300]}")
    return out


# --------------------------------------------------------------------------- #
# spike lab                                                                    #
# --------------------------------------------------------------------------- #
_SPIKE_HEADER = """\
import bpy
sc = bpy.context.scene
_engines = {i.identifier for i in
            bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = ('BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _engines
                    else 'BLENDER_EEVEE')
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
# ---- spike body ----
"""

_SPIKE_RENDER = """
# ---- spike render ----
import bpy
sc = bpy.context.scene
sc.frame_set({frame})
sc.render.resolution_x, sc.render.resolution_y = 960, 540
sc.render.filepath = r"{out}"
sc.render.image_settings.file_format = 'PNG'
bpy.ops.render.render(write_still=True)
print("SPIKE_RENDER_OK", sc.render.filepath)
"""


def _contract_probe(contracts: list[dict]) -> str:
    """Append authoritative scene-contract readings to an exploratory Blender spike."""

    snippets = []
    for row in contracts:
        samples = row.get("samples") or []
        frames = row.get("frames") or []
        frame = row.get("frame")
        if frame is None and samples:
            frame = samples[0].get("frame")
        if frame is None and frames:
            frame = frames[0]
        snippets.append(_blender_probe([row], int(frame or 1)))
        snippets.append(
            f"\nprint({_SPIKE_CONTRACT_MARKER!r} + json.dumps(RESULT[0], sort_keys=True))\n"
        )
    return "\n".join(snippets)
