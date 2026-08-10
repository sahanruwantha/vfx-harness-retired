"""The Higgsfield CLI client — command building, JSON→URL selection, and orchestration.

No CLI and no network: the subprocess runner and the fetch are injected. The live JSON shape
(a list of job objects with status + result_url) is verified separately against the real CLI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scene import higgsfield as hf


def test_available_reflects_cli_on_path(monkeypatch) -> None:
    monkeypatch.setattr(hf.shutil, "which", lambda _name: None)
    assert hf.available() is False
    monkeypatch.setattr(hf.shutil, "which", lambda _name: "/usr/bin/higgsfield")
    assert hf.available() is True


def test_build_command_has_prompt_wait_and_json() -> None:
    cmd = hf.build_command("a wooden chair")
    assert cmd[:4] == ["higgsfield", "generate", "create", "text2image_soul_v2"]
    assert "--prompt" in cmd and "a wooden chair" in cmd
    assert cmd[-3:] == ["--wait-timeout", "5m", "--json"]  # tail wiring
    assert "--wait" in cmd


def test_build_command_adds_references_quality_and_extra() -> None:
    cmd = hf.build_command(
        "chair",
        quality="2k",
        image_references=["/a/ref1.png", "/a/ref2.png"],
        extra={"batch_size": 1},
    )
    assert cmd.count("--image-references") == 2
    assert "/a/ref1.png" in cmd and "/a/ref2.png" in cmd
    assert "--quality" in cmd and "2k" in cmd
    assert "--batch-size" in cmd and "1" in cmd  # underscore → dash


def test_result_url_prefers_completed_job() -> None:
    jobs = [
        {"status": "failed", "result_url": "http://x/bad.png"},
        {"status": "completed", "result_url": "http://x/good.png", "min_result_url": "http://x/small.png"},
    ]
    assert hf.result_url_from_jobs(jobs) == "http://x/good.png"


def test_result_url_fallback_and_none() -> None:
    assert hf.result_url_from_jobs([{"status": "weird", "result_url": "http://x/a.png"}]) == "http://x/a.png"
    assert hf.result_url_from_jobs([{"status": "running"}]) is None
    assert hf.result_url_from_jobs([]) is None


def _proc(returncode=0, stdout="[]", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["higgsfield"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_generate_image_orchestrates(tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return _proc(stdout=json.dumps([{"status": "completed", "result_url": "http://cdn/chair.png"}]))

    def fake_fetch(url, dest):
        captured["url"] = url
        Path(dest).write_bytes(b"png")
        return Path(dest)

    out = hf.generate_image("a chair", tmp_path / "chair.png", _run=fake_run, _fetch=fake_fetch)

    assert out == tmp_path / "chair.png"
    assert captured["url"] == "http://cdn/chair.png"
    assert "text2image_soul_v2" in captured["cmd"]


def test_generate_image_raises_on_cli_failure(tmp_path) -> None:
    def fake_run(cmd, **_kw):
        return _proc(returncode=1, stderr="not enough credits")

    with pytest.raises(hf.HiggsfieldError, match="not enough credits"):
        hf.generate_image("x", tmp_path / "x.png", _run=fake_run)


def test_generate_image_raises_without_result_url(tmp_path) -> None:
    def fake_run(cmd, **_kw):
        return _proc(stdout=json.dumps([{"status": "running"}]))

    with pytest.raises(hf.HiggsfieldError, match="no image URL"):
        hf.generate_image("x", tmp_path / "x.png", _run=fake_run)
