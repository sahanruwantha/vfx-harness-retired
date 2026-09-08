"""Qualification must bind selected sources and actual invocation, not a model's opinion."""

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import flynn_agents_sdk as flynn
import httpx
import pytest
from flynn_agents_sdk import deepseek
from PIL import Image

from tests.contract.test_flynn_critic_transport import audit, response
from vfx_harness.agents import critic_qualification, critic_transport
from vfx_harness.domain.critic_verdict import critic_verdict_schema
from vfx_harness.domain.work_units.claims import Claim
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError

PROMPT = "Judge the supplied reference and candidate on the declared form axis."
AXES = (("form", "Visible form"),)
IMAGES = (("reference", "reference.png"), ("candidate", "candidate.png"), ("focus", "focus.png"))
RATES = ("false_pass_rate", "false_failure_rate", "repeatability_failure_rate",
         "scope_leakage_rate", "irrelevant_change_sensitivity_rate")


@pytest.fixture
def bound(tmp_path, monkeypatch):
    layout = run_artifacts.create(tmp_path, "qualification-fixture")
    monkeypatch.setenv(run_artifacts.ENV, str(layout.root))
    for name, color in (("reference", "red"), ("candidate", "blue"), ("focus", "green")):
        Image.new("RGB", (4, 4), color).save(tmp_path / f"{name}.png")
    return layout


def publish(bound, claim, record):
    payload = json.dumps(record, sort_keys=True).encode()
    (bound.shot / "qualification.json").write_bytes(payload)
    claim.qualification["artifact_sha256"] = hashlib.sha256(payload).hexdigest()


@pytest.fixture
def qualified(bound):
    q = {"suite": "form-v1", "judge_model": deepseek.VISION_MODEL,
         "prompt": hashlib.sha256(PROMPT.encode()).hexdigest(), "evidence_shape": critic_transport.IMAGE_SHAPE,
         "artifact": "qualification.json", "artifact_sha256": "0" * 64}
    claim = Claim.parse({
        "id": "claim.form", "proposition": "The subject has the declared form", "axis": "form",
        "property": "shape", "subject_roles": ["subject"], "moments": [7], "kind": "atomic",
        "required": True, "authority": "qualified_qualitative_required", "repair_owner": "form",
        "asserts": "image", "evidence": [{"kind": "qualification", "id": "form-v1"}], "qualification": q,
    }, "fixture claim")
    configuration = flynn.InferenceConfiguration(
        "deepseek", deepseek.VISION_MODEL, "flynn.deepseek-chat/v1", json.dumps({
            "system_instruction": deepseek.DEFAULT_INSTRUCTION, "max_tokens": 8192,
            "thinking": {"type": "disabled"}, "tool_choice": "required", "stream": False,
        }),
    )
    schema = critic_verdict_schema(list(AXES), allow_na=False, focus_frames=[7])
    profile = {
        "schema": "vfx-harness.critic-invocation/v1",
        "scope": {"scope_id": "layer:surface", "phase": "observer", "axes": AXES, "frames": (7,),
                  "allow_na": False, "claims": [{k: v for k, v in asdict(claim).items() if k != "qualification"}]},
        "prompt_sha256": q["prompt"],
        "response_schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        "image_shape": critic_transport.IMAGE_SHAPE, "accepted_state": critic_transport.EMPTY_STATE,
        "tool": {"name": "submit_verdict", "description": critic_transport.TOOL_DESCRIPTION},
        "images": [{"role": role, "label": label, "detail": "original", "mime": "data:image/png",
                    "size": [4, 4], "mode": "RGB", "frame_count": 1}
                   for role, label in (("reference", "REFERENCE"), ("candidate", "CANDIDATE"),
                                       ("focus", "FOCUS PANEL 1"))],
        "configuration": asdict(configuration),
    }
    record = {"schema": 1, **{key: q[key] for key in ("suite", "judge_model", "prompt", "evidence_shape")},
              "passed": True, "budgets": dict.fromkeys(RATES, 0.1), "metrics": dict.fromkeys(RATES, 0),
              "native_invocation_sha256": critic_qualification.digest(profile)}
    publish(bound, claim, record)
    return claim, record


def invoke(bound, claim, handler=lambda _: response(), *, factory=deepseek.DeepSeekAdapter, options=None, **updates):
    async def execute():
        async with factory(api_key="offline", model=deepseek.VISION_MODEL, max_tokens=8192,
                           transport=httpx.MockTransport(handler), **(options or {})) as adapter:
            arguments = {"folder": bound.shot, "scope_id": "layer:surface", "phase": "observer",
                         "requested_provider": "deepseek", "requested_model": deepseek.VISION_MODEL,
                         "prompt": PROMPT, "axes": AXES, "frames": (7,), "images": IMAGES, "allow_na": False,
                         "check_current": lambda: None, "qualification_claims": (claim,)}
            arguments.update(updates)
            return await critic_transport.execute(**arguments, inference=adapter)
    return asyncio.run(execute())


def test_selected_qualification_matches_dispatched_configuration_without_accepting_work(bound, qualified):
    claim, _record = qualified
    result = invoke(bound, claim)
    assert result["qualification_verified"] is True
    assert result["qualified_claim_ids"] == [claim.id]
    assert result["acceptance_authorized"] is False
    database, report = audit(bound)
    assert report["qualification_check"]["status"] == "matched_dispatched_configuration"
    assert report["qualification_verified"] is True
    assert report["inputs"]["qualification_sources"][0]["sha256"] == claim.qualification["artifact_sha256"]
    with flynn.SQLiteRun.open(database) as run:
        assert run.read().value == critic_transport.EMPTY_STATE
        assert run.read().revision == 0
        assert run.records()["commits"] == []
        usage = json.loads(run.records()["inference_usage"][0]["payload"])
        assert usage["configuration_sha256"] == report["qualification_check"]["configuration_sha256"]


@pytest.mark.parametrize("failure", ["prompt", "evidence_shape", "judge_model", "passed", "metric",
                                     "missing_binding", "bytes", "symlink", "schema"])
def test_unqualified_or_stale_artifact_refuses_before_inference(bound, qualified, failure):
    claim, record = qualified
    if failure in ("prompt", "evidence_shape", "judge_model"):
        record[failure] = claim.qualification[failure] = "different"
    elif failure == "passed":
        record["passed"] = False
    elif failure == "metric":
        record["metrics"]["false_pass_rate"] = 1
    elif failure == "missing_binding":
        record.pop("native_invocation_sha256")
    elif failure == "schema":
        record["schema"] = True
    publish(bound, claim, record)
    path = bound.shot / "qualification.json"
    if failure == "bytes":
        path.write_bytes(b"substitution")
    elif failure == "symlink":
        copy = bound.shot / "other.json"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy.name)
    with pytest.raises((ValueError, PlanPublicationError)):
        invoke(bound, claim, lambda _: pytest.fail("invalid qualification reached provider"))
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


@pytest.mark.parametrize("change", ["system", "effort", "axes", "frames", "phase", "claim", "oversized_system"])
def test_invocation_drift_refuses_before_budget_reservation(bound, qualified, change):
    claim, _record = qualified
    options, updates = {}, {}
    if change == "system":
        options["system_instruction"] = "different judge instructions"
    elif change == "oversized_system":
        options["system_instruction"] = "x" * critic_transport.MAX_CONTEXT_CHARACTERS
    elif change == "effort":
        options["reasoning_effort"] = "high"
    elif change == "axes":
        updates["axes"] = (("form", "different rubric"),)
    elif change == "frames":
        updates["frames"] = (8,)
    elif change == "phase":
        updates["phase"] = "different-review"
    else:
        claim = replace(claim, proposition="A different claim")
    with pytest.raises(ValueError, match=r"invocation|bounded context"):
        invoke(bound, claim, lambda _: pytest.fail("different invocation reached provider"), options=options, **updates)
    if change == "frames":
        assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))
        return
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining() == {"inference": 1, "tool": 1, "external": 0}
        assert run.records()["inference_usage"] == []
    assert report["qualification_verified"] is False


@pytest.mark.parametrize("change", ["dimensions", "mode", "animation"])
def test_changed_image_shape_cannot_reuse_qualification(bound, qualified, change):
    claim, _record = qualified
    Image.new("RGBA" if change == "mode" else "RGB", (8, 4) if change == "dimensions" else (4, 4)).save(
        bound.shot / "candidate.png",
    )
    if change == "animation":
        Image.new("RGB", (4, 4), "red").save(
            bound.shot / "candidate.png", save_all=True, append_images=[Image.new("RGB", (4, 4), "blue")],
            duration=100, loop=0,
        )
    with pytest.raises(ValueError, match="invocation mismatch"):
        invoke(bound, claim, lambda _: pytest.fail("unqualified image shape reached provider"))
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["inference"] == 1
        assert run.records()["inference_usage"] == []
    assert report["qualification_verified"] is False


def test_same_qualified_protocol_can_judge_different_pixels(bound, qualified):
    claim, _record = qualified
    Image.new("RGB", (4, 4), "yellow").save(bound.shot / "candidate.png")
    result = invoke(bound, claim)
    assert result["qualification_verified"] is True
    assert result["acceptance_authorized"] is False


class ChangedAtDispatch(deepseek.DeepSeekAdapter):
    async def generate(self, request):
        original = self.system_instruction
        self.system_instruction = "substituted between admission and dispatch"
        try:
            return await super().generate(request)
        finally:
            self.system_instruction = original


class MissingConfiguration(deepseek.DeepSeekAdapter):
    async def generate(self, request):
        result = await super().generate(request)
        return replace(result, usage=replace(result.usage, configuration_sha256=None))


@pytest.mark.parametrize("failure", ["configuration", "unreported", "artifact", "claim"])
def test_post_admission_substitution_preserves_spending_and_refuses_verdict(bound, qualified, failure):
    claim, _record = qualified

    def handler(_):
        if failure == "artifact":
            (bound.shot / "qualification.json").write_bytes(b"substitution")
        elif failure == "claim":
            claim.qualification["prompt"] = "changed"
        return response()

    factory = {"configuration": ChangedAtDispatch, "unreported": MissingConfiguration}.get(
        failure, deepseek.DeepSeekAdapter,
    )
    with pytest.raises(ValueError, match=r"configuration|changed"):
        invoke(bound, claim, handler, factory=factory)
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["tool"] == 1
        assert run.latest_observation() is None
        assert run.records()["commits"] == []
    assert report["usage"]["known_output_tokens"] == 31
    assert report["qualification_verified"] is report["acceptance_authorized"] is False


def test_scope_metadata_cannot_bypass_bounded_context(bound):
    with pytest.raises(ValueError):
        invoke(bound, None, lambda _: pytest.fail("oversized scope reached provider"), qualification_claims=(),
               axes=(("form", "x" * critic_transport.MAX_CONTEXT_CHARACTERS),))
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


@pytest.mark.parametrize("invalid", ["implicit", "duplicate", "binding"])
def test_no_qualification_is_invented_for_invalid_claim_selection(bound, qualified, invalid):
    claim, _record = qualified
    claims = (claim,)
    if invalid == "implicit":
        claims = (SimpleNamespace(id="implicit-look", authority="qualified_qualitative_required"),)
    elif invalid == "duplicate":
        claims = (claim, claim)
    else:
        claims = (replace(claim, evidence=(replace(claim.evidence[0], id="other-suite"),)),)
    with pytest.raises(ValueError, match=r"parsed selected claims|distinct selected|exact qualification suite"):
        invoke(bound, claim, lambda _: pytest.fail("invalid selection reached provider"), qualification_claims=claims)
    assert not list((bound.checkpoints / "flynn").glob("*.sqlite"))


def test_scripted_cannot_satisfy_a_model_qualification(bound, qualified):
    claim, _record = qualified
    with pytest.raises(ValueError, match="scripted cannot qualify"):
        asyncio.run(critic_transport.execute(
            folder=bound.shot, scope_id="layer:surface", phase="observer", requested_provider="deepseek",
            requested_model=deepseek.VISION_MODEL, prompt=PROMPT, axes=AXES, frames=(7,), images=IMAGES,
            allow_na=False, check_current=lambda: None, qualification_claims=(claim,),
            inference=flynn.ScriptedAdapter([]),
        ))
    database, report = audit(bound)
    with flynn.SQLiteRun.open(database) as run:
        assert run.remaining()["inference"] == 1
        assert run.records()["inference_usage"] == []
    assert report["qualification_verified"] is False
