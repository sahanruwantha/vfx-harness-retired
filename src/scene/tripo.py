"""Tripo v2 image-to-3D client — turn an image into a GLB mesh.

The asset backend for the 3D agent: Higgsfield makes the *image* (styled to the reference),
Tripo turns that image into a mesh, and the bridge imports + normalizes it. Env-gated on
``TRIPO_API_KEY`` and stdlib-only (``urllib``), matching ``footage/providers`` — no third-party
runtime dep. The live v2 shape (verified against the real API):

    POST {BASE}/upload                 multipart "file"  -> {"code":0,"data":{"image_token": ...}}
    POST {BASE}/task                   {"type":"image_to_model","file":{"type","file_token"}}
                                                          -> {"code":0,"data":{"task_id": ...}}
    GET  {BASE}/task/{id}              -> data.status in queued|running|success|failed|banned
                                          data.progress, data.output.pbr_model (the GLB URL)

Each HTTP step is a module function so the orchestrator :func:`image_to_glb` can be tested with
fakes; the pure helpers (multipart encoding, output-URL selection) are tested directly.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

BASE = "https://api.tripo3d.ai/v2/openapi"
USER_AGENT = "theBambiProject-scene/0.1 (+https://localhost)"
_TERMINAL = {"success", "failed", "banned", "cancelled", "expired", "unknown"}
_EXT_TO_TYPE = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".webp": "webp"}
_TYPE_TO_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


class TripoError(RuntimeError):
    """A Tripo request failed, was rejected, or the task did not succeed."""


def available() -> bool:
    """True when a key is configured — callers skip 3D asset gen gracefully when it is not."""
    return bool(os.environ.get("TRIPO_API_KEY"))


def _key(explicit: str | None) -> str:
    key = explicit or os.environ.get("TRIPO_API_KEY")
    if not key:
        raise TripoError("TRIPO_API_KEY is not set")
    return key


def _image_type(path: Path) -> str:
    return _EXT_TO_TYPE.get(path.suffix.lower(), "jpg")


# --- pure helpers (tested without the network) --------------------------------------


def encode_multipart(field_name: str, filename: str, content: bytes, mime: str, boundary: str) -> bytes:
    """Encode a single-file multipart/form-data body with *boundary*."""
    bnd = boundary.encode("ascii")
    return b"\r\n".join(
        [
            b"--" + bnd,
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode(),
            f"Content-Type: {mime}".encode(),
            b"",
            content,
            b"--" + bnd + b"--",
            b"",
        ]
    )


def model_url_from_output(output: dict[str, Any]) -> str | None:
    """The GLB to download: prefer the textured PBR model, then plain model variants."""
    for key in ("pbr_model", "model", "model_url", "base_model"):
        url = output.get(key)
        if url:
            return str(url)
    return None


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Tripo's ``{"code":0,"data":{...}}`` envelope, raising on a non-zero code."""
    if payload.get("code") not in (0, None):
        raise TripoError(f"tripo error code {payload.get('code')}: {payload.get('message', '')}")
    return payload.get("data") or {}


# --- HTTP steps ---------------------------------------------------------------------


def _request(url: str, *, api_key: str, data: bytes | None = None, content_type: str | None = None,
             timeout: float = 60.0) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def upload_image(image_path: str | Path, *, api_key: str | None = None, base: str = BASE) -> str:
    """Upload an image, returning its ``image_token`` for a subsequent task."""
    path = Path(image_path)
    content = path.read_bytes()
    itype = _image_type(path)
    boundary = "----bambi" + uuid.uuid4().hex
    body = encode_multipart("file", path.name, content, _TYPE_TO_MIME[itype], boundary)
    payload = _request(
        f"{base}/upload",
        api_key=_key(api_key),
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    token = _data(payload).get("image_token")
    if not token:
        raise TripoError(f"upload returned no image_token: {payload}")
    return str(token)


def create_image_to_model_task(
    image_token: str, *, image_type: str = "jpg", api_key: str | None = None, base: str = BASE
) -> str:
    """Start an image_to_model task from an uploaded token, returning the task id."""
    body = json.dumps(
        {"type": "image_to_model", "file": {"type": image_type, "file_token": image_token}}
    ).encode("utf-8")
    payload = _request(f"{base}/task", api_key=_key(api_key), data=body, content_type="application/json")
    task_id = _data(payload).get("task_id")
    if not task_id:
        raise TripoError(f"task creation returned no task_id: {payload}")
    return str(task_id)


def get_task(task_id: str, *, api_key: str | None = None, base: str = BASE) -> dict[str, Any]:
    """The task's current ``data`` block: status, progress, output."""
    return _data(_request(f"{base}/task/{task_id}", api_key=_key(api_key)))


def poll_task(
    task_id: str,
    *,
    api_key: str | None = None,
    base: str = BASE,
    timeout: float = 300.0,
    interval: float = 5.0,
    on_progress: Callable[[str, int], None] | None = None,
    _get: Callable[[str], dict[str, Any]] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll until the task reaches a terminal state; return its ``output`` on success.

    Raises on failure or timeout — a bad asset must never be laundered into a shot.
    """
    key = _key(api_key)
    fetch = _get or (lambda tid: get_task(tid, api_key=key, base=base))
    deadline = _now() + timeout
    while True:
        data = fetch(task_id)
        status = str(data.get("status", "unknown"))
        if on_progress is not None:
            on_progress(status, int(data.get("progress", 0) or 0))
        if status == "success":
            return data.get("output") or {}
        if status in _TERMINAL:
            raise TripoError(f"tripo task {task_id} ended {status}")
        if _now() >= deadline:
            raise TripoError(f"tripo task {task_id} timed out after {timeout}s (last status {status})")
        _sleep(interval)


def download(url: str, dest: str | Path, *, timeout: float = 120.0) -> Path:
    """Stream a URL to *dest* (the signed GLB link). Returns the written path."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    return dest_path


def image_to_glb(
    image_path: str | Path,
    dest: str | Path,
    *,
    api_key: str | None = None,
    poll_timeout: float = 300.0,
    on_progress: Callable[[str, int], None] | None = None,
    _upload: Callable[..., str] = upload_image,
    _create: Callable[..., str] = create_image_to_model_task,
    _poll: Callable[..., dict[str, Any]] = poll_task,
    _fetch: Callable[..., Path] = download,
) -> Path:
    """Upload → image_to_model → poll → download the GLB. Steps are injectable for testing."""
    key = _key(api_key)
    path = Path(image_path)
    token = _upload(path, api_key=key)
    task_id = _create(token, image_type=_image_type(path), api_key=key)
    output = _poll(task_id, api_key=key, timeout=poll_timeout, on_progress=on_progress)
    url = model_url_from_output(output)
    if not url:
        raise TripoError(f"task produced no model URL: {output}")
    return _fetch(url, dest)
