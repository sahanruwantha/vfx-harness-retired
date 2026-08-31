"""Meshy image→3D via Meshy's own HTTP API (api.meshy.ai).

Higgsfield's Meshy is text-to-3D only, so image-driven Meshy goes direct here.
Needs MESHY_API_KEY (msy_…). Create task → poll → download the signed GLB
(URLs expire; files retained ~3 days, so we persist immediately).

Source units use ``multi_image_to_3d``: 1–4 views as ``image_urls`` posted to
``/multi-image-to-3d``. Dropping extras onto a single ``image_url`` is a
contract violation (HIR-0162). ``image_to_3d`` remains the legacy single-image
endpoint until that path is retired with ``vfx asset``.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability.log import log

BASE = "https://api.meshy.ai/openapi/v1"
MULTI_IMAGE_PATH = "/multi-image-to-3d"
IMAGE_TO_3D_PATH = "/image-to-3d"
MULTI_IMAGE_MIN = 1
MULTI_IMAGE_MAX = 4
DROPPED_VIEWS_RULE = (
    "image→3D posts every supplied view (1 to 4) as image_urls to "
    "/multi-image-to-3d; dropping extras onto image_url is a contract violation"
)


def _key() -> str:
    load_environment()
    k = os.environ.get("MESHY_API_KEY")
    if not k:
        raise RuntimeError("MESHY_API_KEY not set (add it to .env)")
    return k


def _headers(*, api_key: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key or _key()}"}


def _data_uri(p: Path) -> str:
    p = Path(p)
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "png"
    return f"data:image/{mime};base64,{b64}"


@contextmanager
def _client_scope(client: httpx.Client | None):
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=90.0) as owned:
        yield owned


def _require_view_count(images: Sequence[Path]) -> tuple[Path, ...]:
    views = tuple(Path(item) for item in images)
    if not MULTI_IMAGE_MIN <= len(views) <= MULTI_IMAGE_MAX:
        raise ValueError(
            f"image→3D needs {MULTI_IMAGE_MIN} to {MULTI_IMAGE_MAX} views; "
            f"got {len(views)}. " + DROPPED_VIEWS_RULE
        )
    return views


def _poll_and_download(
    client: httpx.Client,
    *,
    path: str,
    tid: str,
    headers: dict[str, str],
    out: Path,
    poll_interval: float,
    timeout: float,
) -> str:
    url = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        g = client.get(f"{BASE}{path}/{tid}", headers=headers).json()
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
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(out, "wb") as handle:
            for chunk in resp.iter_bytes():
                handle.write(chunk)
    return str(url)


def multi_image_to_3d(
    images: Sequence[Path],
    out: Path,
    *,
    ai_model: str = "meshy-7",
    target_polycount: int = 250000,
    should_texture: bool = False,
    enable_pbr: bool = False,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
    client: httpx.Client | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Create a multi-image-to-3d task from 1–4 views, poll, and download the GLB.

    The first image is the front view for meshy-7 / latest. Remaining order does
    not matter. Source units keep ``should_texture`` false so textures cannot mix
    shading into a mesh write-cluster.
    """
    views = _require_view_count(images)
    headers = _headers(api_key=api_key)
    body = {
        "image_urls": [_data_uri(path) for path in views],
        "ai_model": ai_model,
        "topology": "triangle",
        "target_polycount": int(target_polycount),
        "should_texture": should_texture,
        "enable_pbr": enable_pbr,
        "should_remesh": True,
        "target_formats": ["glb"],
        "multi_view_thumbnails": True,
    }
    log(
        f"image→3D: meshy {ai_model} (multi-image-to-3d, {len(views)} views, "
        f"polycount {target_polycount})…",
        2,
    )
    with _client_scope(client) as http:
        response = http.post(f"{BASE}{MULTI_IMAGE_PATH}", headers=headers, json=body)
        response.raise_for_status()
        tid = response.json()["result"]
        url = _poll_and_download(
            http,
            path=MULTI_IMAGE_PATH,
            tid=tid,
            headers=headers,
            out=out,
            poll_interval=poll_interval,
            timeout=timeout,
        )
    return {"task_id": tid, "backend": "meshy", "result_url": url, "view_count": len(views)}


def image_to_3d(
    image,
    out,
    *,
    ai_model: str = "meshy-5",
    target_polycount: int = 250000,
    enable_pbr: bool = True,
    should_texture: bool = True,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
) -> dict:
    """Create a legacy single-image-to-3d task, poll to SUCCEEDED, download the GLB.

    New source-unit generation must call ``multi_image_to_3d``. This endpoint remains
    until the freestanding asset-builder is retired (HIR-0162).
    """
    headers = _headers()
    body = {
        "image_url": _data_uri(image),
        "ai_model": ai_model,
        "topology": "triangle",
        "target_polycount": int(target_polycount),
        "should_texture": should_texture,
        "enable_pbr": enable_pbr,
        "should_remesh": True,
        "target_formats": ["glb"],
    }
    log(f"image→3D: meshy {ai_model} (image-to-3d, polycount {target_polycount})…", 2)
    with httpx.Client(timeout=90.0) as c:
        r = c.post(f"{BASE}{IMAGE_TO_3D_PATH}", headers=headers, json=body)
        r.raise_for_status()
        tid = r.json()["result"]
        url = _poll_and_download(
            c,
            path=IMAGE_TO_3D_PATH,
            tid=tid,
            headers=headers,
            out=out,
            poll_interval=poll_interval,
            timeout=timeout,
        )
    return {"task_id": tid, "backend": "meshy", "result_url": url}
