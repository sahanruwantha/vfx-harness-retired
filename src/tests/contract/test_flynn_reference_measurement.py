"""Measurements and returned images name exactly the same scoped reference bytes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_flynn_planning_knowledge import execute
from vfx_harness.agents import flynn_reference_measurement, image_inputs
from vfx_harness.evidence import metrics
from vfx_harness.orchestration.authority_selection import SelectedAuthorityResolutionError
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


def test_reference_returns_exact_image_and_canonical_measurements_on_reread(bound):
    results = execute(bound, [("measure_ref", {"path": "refs/a.png"})] * 2)
    payload = (bound[0].shot / "refs/a.png").read_bytes()
    with Image.open(io.BytesIO(payload)) as image:
        expected = metrics.canonical_fingerprint(image.convert("RGB"))
    for index, result in enumerate(results):
        data = json.loads(result.data_json)
        assert result.status == "ok" and data["measured"]
        assert data["fingerprint"] == expected
        assert data["sha256"] == hashlib.sha256(payload).hexdigest()
        assert data["remaining_reads"] == 5 - index
        assert (data["width"], data["height"]) == (64, 64)
        images = [content for content in result.content if isinstance(content, flynn.ImageContent)]
        assert len(images) == 1
        assert base64.b64decode(images[0].url.split(",", 1)[1]) == payload
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining() == {"inference": 8, "tool": 8, "external": 10}
        assert journal.records()["commits"] == []


def test_seventh_reference_read_refuses_without_an_image(bound):
    results = execute(bound, [("measure_ref", {"path": "refs/a.png"})] * 7)
    assert all(result.status == "ok" for result in results[:6])
    assert results[-1].status == "refused"
    assert json.loads(results[-1].data_json)["remaining_reads"] == 0
    assert not any(isinstance(item, flynn.ImageContent) for item in results[-1].content)


@pytest.mark.parametrize("arguments", [
    {"path": "refs/foreign.png"}, {"path": "../refs/a.png"}, {"path": "./refs/a.png"},
    {"path": "refs/a.png", "extra": True}, {"path": []}, {},
])
def test_unknown_reference_refuses_before_tool_reservation(bound, arguments):
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, [("measure_ref", arguments)])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining()["tool"] == 10


def test_unit_uses_its_evaluation_references(bound):
    result = execute(bound, [("measure_ref", {"path": "refs/a.png"})], layer_id="1", unit_id="lock")[0]
    assert json.loads(result.data_json)["unit"] == "lock"


def test_changed_reference_refuses_after_inference(bound):
    def interfere(_):
        Image.new("RGB", (64, 64), "red").save(bound[0].shot / "refs/a.png")
    with pytest.raises((ValueError, PlanPublicationError, SelectedAuthorityResolutionError)):
        execute(bound, [("measure_ref", {"path": "refs/a.png"})], interfere=interfere)
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining()["tool"] == 10


def test_changed_reference_during_measurement_publishes_no_success(bound, monkeypatch):
    original = metrics.canonical_fingerprint

    def measure(image):
        result = original(image)
        Image.new("RGB", (64, 64), "red").save(bound[0].shot / "refs/a.png")
        return result

    monkeypatch.setattr(metrics, "canonical_fingerprint", measure)
    with pytest.raises((ValueError, PlanPublicationError, SelectedAuthorityResolutionError)):
        execute(bound, [("measure_ref", {"path": "refs/a.png"})])


@pytest.mark.parametrize("limit", ["bytes", "pixels"])
def test_measurement_size_limits_refuse_before_metrics(bound, monkeypatch, limit):
    if limit == "bytes":
        monkeypatch.setattr(image_inputs, "MAX_IMAGE_BYTES", 1)
    else:
        monkeypatch.setattr(flynn_reference_measurement, "MAX_MEASUREMENT_PIXELS", 1)

    def unexpected(_):
        pytest.fail("oversized image reached measurement")

    monkeypatch.setattr(metrics, "canonical_fingerprint", unexpected)
    result = execute(bound, [("measure_ref", {"path": "refs/a.png"})])[0]
    assert result.status == "refused"
    assert not json.loads(result.data_json)["measured"]


def test_metric_programming_error_is_not_presented_as_bad_reference(bound, monkeypatch):
    def broken(_):
        raise ValueError("metric defect")
    monkeypatch.setattr(metrics, "canonical_fingerprint", broken)
    with pytest.raises(ValueError, match="metric defect"):
        execute(bound, [("measure_ref", {"path": "refs/a.png"})])


@pytest.mark.parametrize("kind", ["invalid", "gif", "animated"])
def test_reference_decoder_refuses_nonstill_or_unsupported_bytes(bound, kind):
    path = bound[0].shot / "refs/a.png"
    if kind == "invalid":
        path.write_bytes(b"not an image")
    elif kind == "gif":
        Image.new("RGB", (16, 16), "red").save(path, format="GIF")
    else:
        Image.new("RGB", (16, 16), "red").save(
            path, format="PNG", save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")],
        )
    # Exercise the decoder independently of the selected-authority guard: a live
    # scope would already reject these bytes replacing a previously bound image.
    tool = flynn_reference_measurement.reference_measurement_tool(
        layout=bound[0], references=("refs/a.png",), check_current=lambda: None, identity=dict,
    )
    result = flynn.ToolResult.from_json(asyncio.run(tool.execute(json.dumps({"path": "refs/a.png"}))))
    assert result.status == "refused"
    assert not json.loads(result.data_json)["measured"]
    assert not any(isinstance(item, flynn.ImageContent) for item in result.content)
