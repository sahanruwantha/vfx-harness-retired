"""Meshy image→3D via Meshy's own HTTP API (api.meshy.ai).

Higgsfield's Meshy is text-to-3D only, so image-driven Meshy goes direct here.
Needs MESHY_API_KEY (msy_…). Single-call async: create task → poll → download the
signed GLB (URLs expire; files retained ~3 days, so we persist immediately).
Ref: https://docs.meshy.ai/en/api/image-to-3d
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx

from ..config import load_environment
from ..log import log

BASE = "https://api.meshy.ai/openapi/v1"


def _key() -> str:
    load_environment()
    k = os.environ.get("MESHY_API_KEY")
    if not k:
        raise RuntimeError("MESHY_API_KEY not set (add it to .env)")
    return k


def _data_uri(p: Path) -> str:
    p = Path(p)
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "png"
    return f"data:image/{mime};base64,{b64}"


def image_to_3d(image, out, *, ai_model: str = "meshy-5", target_polycount: int = 250000,
                enable_pbr: bool = True, should_texture: bool = True,
                poll_interval: float = 5.0, timeout: float = 1800.0) -> dict:
    """Create an image-to-3d task, poll to SUCCEEDED, download the GLB to `out`."""
    headers = {"Authorization": f"Bearer {_key()}"}
    body = {
        "image_url": _data_uri(image),          # public URL or base64 data URI
        "ai_model": ai_model,                     # meshy-5 | meshy-6 | latest
        "topology": "triangle",
        "target_polycount": int(target_polycount),  # 100–300000 (standard)
        "should_texture": should_texture,
        "enable_pbr": enable_pbr,
        "should_remesh": True,
        "target_formats": ["glb"],
    }
    log(f"image→3D: meshy {ai_model} (image-to-3d, polycount {target_polycount})…", 2)
    with httpx.Client(timeout=90.0) as c:
        r = c.post(f"{BASE}/image-to-3d", headers=headers, json=body)
        r.raise_for_status()
        tid = r.json()["result"]

        url = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            g = c.get(f"{BASE}/image-to-3d/{tid}", headers=headers).json()
            status, progress = g.get("status"), g.get("progress", 0)
            log(f"meshy {status} {progress}%", 3)
            if status == "SUCCEEDED":
                url = (g.get("model_urls") or {}).get("glb")
                break
            if status in ("FAILED", "CANCELED", "EXPIRED"):
                raise RuntimeError(f"meshy task {tid} {status}: {g.get('task_error')}")
            time.sleep(poll_interval)
        if not url:
            raise RuntimeError(f"meshy task {tid} timed out after {timeout:.0f}s")

        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with c.stream("GET", url, follow_redirects=True) as resp:  # signed URL, no auth
            resp.raise_for_status()
            with open(out, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    return {"task_id": tid, "backend": "meshy", "result_url": url}
