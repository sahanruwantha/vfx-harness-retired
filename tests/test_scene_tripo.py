"""The Tripo client — pure helpers and the orchestration, tested without the network.

The live HTTP shape is verified separately against the real API; here we lock the encoding,
the output-URL selection, the envelope handling, and the upload→task→poll→download wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scene.tripo import (
    TripoError,
    available,
    encode_multipart,
    image_to_glb,
    model_url_from_output,
    poll_task,
    _data,
)


def test_encode_multipart_has_boundary_filename_and_body() -> None:
    body = encode_multipart("file", "shot.jpg", b"\xff\xd8data", "image/jpeg", "BND123")
    assert b"--BND123" in body
    assert b'name="file"; filename="shot.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert b"\xff\xd8data" in body
    assert body.rstrip().endswith(b"--BND123--")


def test_model_url_prefers_pbr_then_falls_back() -> None:
    assert model_url_from_output({"pbr_model": "a", "model": "b"}) == "a"
    assert model_url_from_output({"model": "b"}) == "b"
    assert model_url_from_output({"model_url": "c"}) == "c"
    assert model_url_from_output({}) is None


def test_data_unwraps_and_raises_on_error_code() -> None:
    assert _data({"code": 0, "data": {"x": 1}}) == {"x": 1}
    with pytest.raises(TripoError, match="code 2001"):
        _data({"code": 2001, "message": "bad token"})


def test_available_reflects_env(monkeypatch) -> None:
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    assert available() is False
    monkeypatch.setenv("TRIPO_API_KEY", "k")
    assert available() is True


def test_poll_returns_output_on_success() -> None:
    seq = iter(
        [
            {"status": "running", "progress": 10},
            {"status": "running", "progress": 60},
            {"status": "success", "progress": 100, "output": {"pbr_model": "http://glb"}},
        ]
    )
    seen: list[tuple[str, int]] = []
    out = poll_task(
        "t",
        api_key="k",
        _get=lambda _tid: next(seq),
        _sleep=lambda _s: None,
        on_progress=lambda s, p: seen.append((s, p)),
    )
    assert out == {"pbr_model": "http://glb"}
    assert seen[-1] == ("success", 100)


def test_poll_raises_on_failure() -> None:
    with pytest.raises(TripoError, match="ended failed"):
        poll_task("t", api_key="k", _get=lambda _tid: {"status": "failed"}, _sleep=lambda _s: None)


def test_poll_times_out() -> None:
    clock = {"t": 0.0}
    with pytest.raises(TripoError, match="timed out"):
        poll_task(
            "t",
            api_key="k",
            timeout=10,
            interval=5,
            _get=lambda _tid: {"status": "running", "progress": 1},
            _sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            _now=lambda: clock["t"],
        )


def test_image_to_glb_orchestrates_steps(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRIPO_API_KEY", "k")
    calls: dict[str, object] = {}

    def up(path, *, api_key):
        calls["upload"] = (Path(path).name, api_key)
        return "TOK"

    def cr(token, *, image_type, api_key):
        calls["create"] = (token, image_type)
        return "TASK"

    def pl(task_id, *, api_key, timeout, on_progress):
        calls["poll"] = task_id
        return {"pbr_model": "http://glb"}

    def ft(url, dest):
        calls["fetch"] = url
        Path(dest).write_bytes(b"glTF")
        return Path(dest)

    out = image_to_glb(
        "/tmp/shot.png", tmp_path / "asset.glb", _upload=up, _create=cr, _poll=pl, _fetch=ft
    )

    assert out == tmp_path / "asset.glb"
    assert calls["upload"] == ("shot.png", "k")
    assert calls["create"] == ("TOK", "png")  # image_type derived from the extension
    assert calls["poll"] == "TASK"
    assert calls["fetch"] == "http://glb"


def test_image_to_glb_without_key_raises(monkeypatch) -> None:
    monkeypatch.delenv("TRIPO_API_KEY", raising=False)
    with pytest.raises(TripoError, match="not set"):
        image_to_glb("/tmp/x.png", "/tmp/x.glb")
