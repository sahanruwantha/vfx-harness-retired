"""Native critic observations never certify qualification or production acceptance."""

import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict

import flynn_agents_sdk as flynn
import httpx
import pytest
from flynn_agents_sdk import deepseek
from PIL import Image

from vfx_harness.agents import critic_transport
from vfx_harness.domain.critic_prompt import CriticPrompt
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


@pytest.fixture
def bound(tmp_path, monkeypatch):
    layout = run_artifacts.create(tmp_path, "critic-fixture")
    monkeypatch.setenv(run_artifacts.ENV, str(layout.root))
    for name, color in (("reference", "red"), ("candidate", "blue"), ("focus", "green")):
        Image.new("RGB", (4, 4), color).save(tmp_path / f"{name}.png")
    return layout


def verdict():
    return {"scores": {"form": 4}, "observations": [], "focus_requests": [],
            "reference_usable": True, "reference_note": ""}


def response(value=None, tool="submit_verdict"):
    return httpx.Response(200, json={
        "model": deepseek.VISION_MODEL, "usage": {"prompt_tokens": 123, "completion_tokens": 31},
        "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
            "type": "function", "function": {"name": tool,
                                             "arguments": json.dumps(verdict() if value is None else value)},
        }]}}],
    })


def invoke(layout, handler, *, check=lambda: None, **updates):
    arguments = {
        "folder": layout.shot, "scope_id": "layer:surface", "phase": "observer",
        "requested_provider": "deepseek", "requested_model": deepseek.VISION_MODEL,
        "prompt": CriticPrompt("Judge the supplied reference and candidate on the declared form axis."),
        "axes": (("form", "Visible form"),), "frames": (7,), "allow_na": False,
        "images": (("reference", "reference.png"), ("candidate", "candidate.png"), ("focus", "focus.png")),
        "check_current": check,
    }
    arguments.update(updates)

    async def execute():
        async with deepseek.DeepSeekAdapter(api_key="offline", model=deepseek.VISION_MODEL, max_tokens=8192,
                                           transport=httpx.MockTransport(handler)) as adapter:
            return await critic_transport.execute(**arguments, inference=adapter)

    return asyncio.run(execute())


def audit(layout):
    database, = (layout.checkpoints / "flynn").glob("*.sqlite")
    report, = layout.reports.glob("critic-*.json")
    return database, json.loads(report.read_text())


def test_native_critic_records_images_usage_and_an_unqualified_opinion(bound):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    result = invoke(bound, handler)
    assert result["verdict"] == verdict()
    assert result["acceptance_authorized"] is result["qualification_verified"] is False
    assert hashlib.sha256((bound.shot / result["report"]).read_bytes()).hexdigest() == result["report_sha256"]
    assert len(requests) == 1
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["submit_verdict"]
    assert json.dumps(requests[0]).count("data:image/png;base64,") == 3
    assert requests[0]["max_tokens"] == 8192
    database, report = audit(bound)
    assert [source["role"] for source in report["inputs"]["sources"]] == ["reference", "candidate", "focus"]
    assert [source["image_index"] for source in report["inputs"]["sources"]] == [0, 1, 2]
    assert [source["label"] for source in report["inputs"]["sources"]] == ["REFERENCE", "CANDIDATE", "FOCUS PANEL 1"]
    assert report["inputs"]["image_shape"] == "vfx-harness.critic-images/v2"
    with flynn.SQLiteRun.open(database) as run:
        assert run.read().revision == 0
        assert run.records()["commits"] == []
        assert run.remaining() == {"inference": 0, "tool": 0, "external": 0}
        assert report["usage"] == run.usage_summary()
        assert report["output_budget"] == asdict(run.output_budget())
        assert flynn.SessionTermination.from_json(run.outcome()).kind == "stopped"
    assert report["usage"]["known_output_tokens"] == 31
    assert report["inputs_validated_after_inference"] is True
    assert report["model_identity"]["status"] == "matched"
    assert report["model_identity"]["response_model"] == deepseek.VISION_MODEL
    assert report["qualification_verified"] is report["acceptance_authorized"] is False


def test_native_full_manifest_matches_actual_attached_images(bound):
    requests = []
    images = (("reference", "reference.png"), ("candidate", "candidate.png"),
              ("focus", "focus.png"), ("focus", "candidate.png"),
              ("motion", "reference.png"), ("prior", "focus.png"))

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    invoke(bound, handler, images=images)
    _database, report = audit(bound)
    sources = report["inputs"]["sources"]
    assert [source["label"] for source in sources] == [
        "REFERENCE", "CANDIDATE", "FOCUS PANEL 1", "FOCUS PANEL 2",
        "MOTION STRIP", "PREVIOUS ATTEMPT — CONTEXT ONLY",
    ]
    attached = [item["image_url"]["url"] for message in requests[0]["messages"]
                if isinstance(message["content"], list) for item in message["content"]
                if item["type"] == "image_url"]
    assert len(attached) == len(sources) == 6
    for index, (url, source) in enumerate(zip(attached, sources, strict=True)):
        assert source["image_index"] == index
        assert source["input_sha256"] == hashlib.sha256(url.encode()).hexdigest()
        assert source["path"] == images[index][1]


@pytest.mark.parametrize("failure", ["missing", "different", "requested", "provider"])
def test_model_identity_mismatch_preserves_spending_and_refuses_verdict(bound, failure):
    calls = []

    def handler(_):
        calls.append(1)
        payload = json.loads(response().content)
        if failure == "missing":
            payload.pop("model")
        elif failure == "different":
            payload["model"] = "unqualified-replacement"
        return httpx.Response(200, json=payload)

    updates = {}
    if failure == "requested":
        updates["requested_model"] = "different-request"
    elif failure == "provider":
        updates["requested_provider"] = "different-provider"
    with pytest.raises(ValueError, match="identity mismatch"):
        invoke(bound, handler, **updates)
    assert calls == [1]
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["tool"] == 1
        assert run.records()["commits"] == []
        assert run.latest_observation() is None
    assert report["usage"]["known_output_tokens"] == 31
    assert report["model_identity"]["status"] == "unverified"
    assert report["inputs_validated_after_inference"] is False
    assert report["qualification_verified"] is report["acceptance_authorized"] is False


def test_scripted_critic_never_claims_model_identity(bound):
    result = asyncio.run(critic_transport.execute(
        folder=bound.shot, scope_id="scripted", phase="observer",
        requested_provider="deepseek", requested_model=deepseek.VISION_MODEL,
        prompt=CriticPrompt("Offline structured opinion fixture."), axes=(("form", "Visible form"),),
        frames=(7,), images=(("reference", "reference.png"), ("candidate", "candidate.png")),
        allow_na=False, check_current=lambda: None,
        inference=flynn.ScriptedAdapter([flynn.ToolCall("submit_verdict", json.dumps(verdict()))]),
    ))
    assert result["verdict"] == verdict()
    _database, report = audit(bound)
    assert report["model_identity"]["status"] == "not_applicable"
    assert report["model_identity"]["provider"] is None
    assert report["model_identity"]["response_model"] is None
    assert report["qualification_verified"] is report["acceptance_authorized"] is False


@pytest.mark.parametrize("failure", ["extra", "axis", "score", "frame", "tool"])
def test_invalid_verdict_spends_inference_without_submission(bound, failure):
    value = verdict()
    if failure == "extra":
        value["accept"] = True
    elif failure == "axis":
        value["scores"] = {"undeclared": 4}
    elif failure == "score":
        value["scores"] = {"form": True}
    elif failure == "frame":
        value["focus_requests"] = [{"id": "focus", "axis": "form", "source": "candidate_frame",
                                     "source_frame": 8, "region": [0, 0, 0.5, 0.5], "reason": "detail"}]
    with pytest.raises(flynn.ContractError):
        invoke(bound, lambda _: response(value, "accept" if failure == "tool" else "submit_verdict"))
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["tool"] == 1
        assert run.records()["commits"] == []
    assert report["usage"]["known_output_tokens"] == 31
    assert report["inputs_validated_after_inference"] is False


@pytest.mark.parametrize("failure", ["authority", "reference", "candidate", "focus", "run"])
def test_substitution_during_inference_refuses_before_tool_spend(bound, monkeypatch, failure):
    revoked = False

    def check():
        if revoked:
            raise ValueError("owning claim revoked")

    def handler(_):
        nonlocal revoked
        if failure == "authority":
            revoked = True
        elif failure == "run":
            monkeypatch.delenv(run_artifacts.ENV)
        else:
            (bound.shot / f"{failure}.png").write_bytes(b"substitution")
        return response()

    with pytest.raises(ValueError, match=r"revoked|bytes changed|owning run changed"):
        invoke(bound, handler, check=check)
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["tool"] == 1
    assert report["usage"]["known_output_tokens"] == 31


@pytest.mark.parametrize("failure", ["timeout", "cancelled"])
def test_interrupted_inference_is_durable_without_retry(bound, failure):
    calls = []

    def handler(_):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("offline timeout")
        raise asyncio.CancelledError()

    with pytest.raises(flynn.InferenceFailure if failure == "timeout" else asyncio.CancelledError):
        invoke(bound, handler)
    assert calls == [1]
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["inference"] == 0
        assert run.remaining()["tool"] == 1
        assert run.records()["commits"] == []
        assert run.output_budget().unresolved == 1
        assert flynn.SessionTermination.from_json(run.outcome()).kind == (
            "failed" if failure == "timeout" else "cancelled"
        )
    assert report["inputs_validated_after_inference"] is False


@pytest.mark.parametrize("failure", ["oversized", "image", "order", "count", "frames", "axes"])
def test_invalid_required_inputs_refuse_before_inference_or_journal(bound, failure):
    updates = {}
    if failure == "oversized":
        updates["prompt"] = CriticPrompt("x" * critic_transport.MAX_CONTEXT_CHARACTERS)
    elif failure == "image":
        (bound.shot / "candidate.png").unlink()
        (bound.shot / "candidate.png").symlink_to("reference.png")
    elif failure == "order":
        updates["images"] = (("candidate", "candidate.png"), ("reference", "reference.png"))
    elif failure == "count":
        updates["images"] = (("reference", "reference.png"), ("candidate", "candidate.png"),
                             *(("focus", "focus.png"),) * 3)
    elif failure == "frames":
        updates["frames"] = (True,)
    else:
        updates["axes"] = (("form", "form"), ("form", "again"))
    with pytest.raises(PlanPublicationError if failure == "image" else ValueError):
        invoke(bound, lambda _: pytest.fail("invalid context reached provider"), **updates)
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


def test_critic_transport_imports_without_claude():
    code = '''import sys
class RefuseClaude:
    def find_spec(self, fullname, *args):
        if fullname.startswith('claude_agent_sdk'):
            raise RuntimeError('native critic imported Claude')
sys.meta_path.insert(0, RefuseClaude())
from vfx_harness.agents import critic_transport
assert critic_transport.LIMITS.inference_calls == 1
'''
    subprocess.run([sys.executable, "-c", code], check=True)
