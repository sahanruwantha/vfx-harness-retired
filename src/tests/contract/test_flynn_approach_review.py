"""Native role gate: real DeepSeek serialization, SQLite, guards and VFX advice."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace

import flynn_agents_sdk as flynn
import httpx
import pytest
from flynn_agents_sdk import deepseek
from PIL import Image

from vfx_harness.agents import approach, approach_runtime
from vfx_harness.observability import run_artifacts


@pytest.fixture
def role(tmp_path, monkeypatch):
    layout = run_artifacts.RunLayout(tmp_path, "fixture", tmp_path / "runs" / "fixture")
    monkeypatch.setattr(run_artifacts, "active", lambda _: layout)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-fixture")
    monkeypatch.setenv("VFXH_REVIEWER_MODEL", deepseek.VISION_MODEL)
    monkeypatch.delenv("VFXH_RUN_MAX_USD", raising=False)
    monkeypatch.setattr(approach.recipes, "search_recipes", lambda *a, **k: [])
    for name in ("render.png", "reference.png"):
        Image.new("RGB", (4, 4), "red").save(tmp_path / name)
    (tmp_path / "script.py").write_text("# exact current construction\n")
    shot = SimpleNamespace(folder=tmp_path)
    layer = SimpleNamespace(id="surface", title="Surface", reads="prior geometry", owns=("shape",),
                            judge_ref="reference.png")
    return shot, layer, layout


def response(arguments=None):
    return httpx.Response(200, json={
        "id": "offline-response", "model": deepseek.VISION_MODEL,
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "type": "function", "function": {"name": "submit_review", "arguments": json.dumps(
                arguments if arguments is not None else {
                    "verdict": "KEEP", "why": "Same silhouette", "do": "Tune extent",
                }
            )},
        }]}}],
    })


def invoke(role, check=lambda: None):
    shot, layer, _ = role
    return approach.review(shot, layer, "render.png", {"scores": {"shape": 2}, "issues": ["Too narrow"]},
                           "script.py", verbose=False, check_current=check)


def install_transport(monkeypatch, handler):
    adapter = deepseek.DeepSeekAdapter
    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **kw: adapter(**kw, transport=httpx.MockTransport(handler)))


def audit(role):
    layout = role[2]
    path, = (layout.checkpoints / "flynn").glob("*.sqlite")
    report, = layout.reports.glob("approach-*.json")
    return path, json.loads(report.read_text())


@pytest.mark.parametrize("verdict", ["KEEP", "REPLACE"])
def test_review_is_explicit_multimodal_advice_and_never_commits(role, monkeypatch, verdict):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response({"verdict": verdict, "why": "Reference structure differs", "do": "Use a measured revision"})

    install_transport(monkeypatch, handler)
    out = asyncio.run(invoke(role))
    assert out["replace"] is (verdict == "REPLACE")
    assert out["text"].startswith(f"VERDICT: {verdict}\n")
    assert len(requests) == 1
    payload = requests[0]
    assert [row["function"]["name"] for row in payload["tools"]] == ["submit_review"]
    assert payload["max_tokens"] == 2048
    serialized = json.dumps(payload)
    assert serialized.count("data:image/png;base64,") == 2
    assert "exact current construction" in serialized
    path, report = audit(role)
    with flynn.SQLiteRun.open(path) as run:
        assert run.read().revision == 0
        assert run.records()["commits"] == []
        assert run.remaining() == {"inference": 0, "tool": 0, "external": 0}
        assert flynn.SessionTermination.from_json(run.outcome()).kind == "stopped"
        assert report["usage"] == run.usage_summary()
        assert report["output_budget"] == asdict(run.output_budget())
    assert report["usage"]["known_output_tokens"] == 30
    assert report["pricing_status"] == "unpriced"


@pytest.mark.parametrize("arguments", [
    {"verdict": "REPLACE maybe", "why": "x", "do": "y"},
    {"verdict": "KEEP", "why": "", "do": "y"},
    {"verdict": "KEEP", "why": "x", "do": "y", "accept": True},
])
def test_malformed_recommendation_spends_inference_but_never_dispatches(role, monkeypatch, arguments):
    install_transport(monkeypatch, lambda _: response(arguments))
    with pytest.raises(flynn.ContractError):
        asyncio.run(invoke(role))
    path, report = audit(role)
    with flynn.SQLiteRun.open(path) as run:
        assert run.remaining()["tool"] == 1
        assert run.records()["commits"] == []
    assert report["usage"]["known_output_tokens"] == 30
    assert json.loads(report["termination"])["kind"] == "failed"


def test_attempt_change_during_inference_refuses_submission(role, monkeypatch):
    current = True

    def handler(_):
        nonlocal current
        current = False
        return response()

    def check():
        if not current:
            raise ValueError("attempt superseded")

    install_transport(monkeypatch, handler)
    with pytest.raises(ValueError, match="attempt superseded"):
        asyncio.run(invoke(role, check))
    path, _ = audit(role)
    with flynn.SQLiteRun.open(path) as run:
        assert run.remaining()["tool"] == 1
        assert run.usage_summary()["known_output_tokens"] == 30


@pytest.mark.parametrize("failure", ["timeout", "cancelled"])
def test_transport_failure_is_durable_and_propagates(role, monkeypatch, failure):
    def handler(_):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline timeout")
        raise asyncio.CancelledError()

    install_transport(monkeypatch, handler)
    expected = flynn.InferenceFailure if failure == "timeout" else asyncio.CancelledError
    with pytest.raises(expected):
        asyncio.run(invoke(role))
    path, report = audit(role)
    with flynn.SQLiteRun.open(path) as run:
        assert run.remaining()["tool"] == 1
        assert run.output_budget().unresolved == 1
    assert json.loads(report["termination"])["kind"] == ("failed" if failure == "timeout" else "cancelled")


@pytest.mark.parametrize("failure", ["missing", "symlink", "escape", "overflow", "model", "pricing", "key"])
def test_invalid_input_refuses_before_provider_or_journal(role, monkeypatch, failure):
    shot, layer, layout = role
    if failure == "missing":
        (shot.folder / "render.png").unlink()
    elif failure == "symlink":
        (shot.folder / "render.png").unlink()
        (shot.folder / "render.png").symlink_to(shot.folder / "reference.png")
    elif failure == "escape":
        layer.judge_ref = "../outside.png"
    elif failure == "overflow":
        (shot.folder / "script.py").write_text("x" * approach_runtime.MAX_CONTEXT_CHARACTERS)
    elif failure == "model":
        monkeypatch.setenv("VFXH_REVIEWER_MODEL", "text-only")
    elif failure == "pricing":
        monkeypatch.setenv("VFXH_RUN_MAX_USD", "1")
    elif failure == "key":
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setattr(deepseek, "DeepSeekAdapter", lambda **_: pytest.fail("provider must not start"))
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(invoke(role))
    assert not layout.checkpoints.exists()


def test_native_review_imports_without_claude():
    code = '''
import sys
class DenyClaude:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "claude_agent_sdk" or fullname.startswith("claude_agent_sdk."):
            raise AssertionError("native approach imported Claude")
sys.meta_path.insert(0, DenyClaude())
from vfx_harness.agents import approach
'''
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
