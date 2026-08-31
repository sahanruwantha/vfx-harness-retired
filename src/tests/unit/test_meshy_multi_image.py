"""Multi-image Meshy adapter cannot drop extra views (HIR-0162)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vfx_harness.assets.adapter import MeshyBackend
from vfx_harness.assets.meshy import BASE, DROPPED_VIEWS_RULE, MULTI_IMAGE_PATH


def _views(folder: Path, count: int) -> list[Path]:
    paths = []
    for index in range(count):
        path = folder / f"view-{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        paths.append(path)
    return paths


def _transport(posted: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith(MULTI_IMAGE_PATH):
            posted["url"] = url
            posted["body"] = json.loads(request.content)
            return httpx.Response(200, json={"result": "task-1"})
        if request.method == "GET" and url.endswith(f"{MULTI_IMAGE_PATH}/task-1"):
            return httpx.Response(
                200,
                json={
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "model_urls": {"glb": "https://example.test/model.glb"},
                },
            )
        if request.method == "GET" and url.endswith("/model.glb"):
            return httpx.Response(200, content=b"glb-bytes")
        if request.method == "POST" and url.endswith("/image-to-3d"):
            posted["legacy"] = json.loads(request.content)
            return httpx.Response(200, json={"result": "legacy-task"})
        return httpx.Response(404, json={"error": url})

    return httpx.MockTransport(handler)


def test_adapter_posts_every_supplied_view(tmp_path: Path) -> None:
    posted: dict = {}
    client = httpx.Client(transport=_transport(posted), timeout=90.0)
    out = tmp_path / "raw.glb"
    views = _views(tmp_path, 4)

    result = MeshyBackend().to_glb(views, out, client=client, api_key="test")

    assert out.read_bytes() == b"glb-bytes"
    assert result["view_count"] == 4
    assert posted["url"] == f"{BASE}{MULTI_IMAGE_PATH}"
    body = posted["body"]
    assert "image_url" not in body
    assert len(body["image_urls"]) == 4
    assert body["should_texture"] is False
    assert "legacy" not in posted
    for index, uri in enumerate(body["image_urls"]):
        assert uri.startswith("data:image/png;base64,")
        assert views[index].read_bytes()  # each file was read, not images[0] only


def test_adapter_refuses_more_than_four_views_before_http(tmp_path: Path) -> None:
    posted: dict = {}
    client = httpx.Client(transport=_transport(posted), timeout=90.0)

    with pytest.raises(ValueError, match="1 to 4 views"):
        MeshyBackend().to_glb(_views(tmp_path, 5), tmp_path / "raw.glb", client=client, api_key="test")

    assert posted == {}
    with pytest.raises(ValueError, match=DROPPED_VIEWS_RULE[:24]):
        MeshyBackend().to_glb([], tmp_path / "raw.glb", client=client, api_key="test")
