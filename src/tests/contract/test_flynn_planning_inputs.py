"""Native planner questions preserve CAS records; references preserve selected bytes."""

from __future__ import annotations

import base64
import json

import flynn_agents_sdk as flynn
import pytest
from PIL import Image

from tests.contract.test_flynn_global_tools import execute, publish
from tests.contract.test_flynn_mapping_publication import bound as bound
from tests.unit.test_plan_authoring import PRODUCT_BRIEF, _product_mapping, _shot
from vfx_harness.agents import image_inputs
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import escalate, plan_authoring, plan_inputs
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError, digest


def question(**changes):
    return {"question": "Which framing should govern?", "assumption": "Use the authored brief",
            "why_it_matters": "The two references differ", "affected_layers": [],
            "affected_axes": [], "global_decision": True, **changes}


@pytest.fixture
def reference_bound(tmp_path, monkeypatch):
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    def make(*, format="PNG", payload=None):
        root = _shot(tmp_path, PRODUCT_BRIEF, ["f001.png"])
        if payload is None:
            Image.new("RGB", (4, 3), "red").save(root / "refs/f001.png", format=format)
        else:
            (root / "refs/f001.png").write_bytes(payload)
        layout = run_artifacts.create(root, "native-reference")
        workspace = plan_inputs.prepare_staging(layout)
        mapping = _product_mapping(plan_authoring.clause_registry(root / "brief.md"))
        return layout, workspace, mapping
    return make


def test_question_is_durable_scoped_and_does_not_block_gate(bound):
    layout, _, _ = bound
    args = question(global_decision=False, affected_layers=["1"])
    results = execute(bound, [publish(bound), ("ask_supervisor", args), ("run_gate", {})])
    data = json.loads(results[1].data_json)
    stored, = escalate.load(layout.shot)
    assert data["question"] == stored
    assert stored["affected_layers"] == ["1"] and stored["global_decision"] is False
    assert stored["answer"] is None and data["created"] is True
    assert data["ledger_sha256"] == digest((layout.shot / escalate.QUESTIONS).read_bytes())
    assert data["plan_authority_changed"] is False
    assert json.loads(results[2].data_json)["clean"] is True
    assert not (layout.shot / "plans/current.json").exists()
    with flynn.SQLiteRun.open(layout.checkpoints / "global.sqlite") as journal:
        assert journal.records()["commits"] == []


def test_duplicate_returns_actual_stored_assumption_and_scope(bound):
    results = execute(bound, [publish(bound), ("ask_supervisor", question()),
        ("ask_supervisor", question(assumption="Use the image", affected_layers=["1"], global_decision=False)),
    ])
    first, duplicate = [json.loads(result.data_json) for result in results[1:]]
    assert first["created"] is True and duplicate["created"] is False
    assert duplicate["question"] == first["question"]
    assert duplicate["question"]["assumption"] == "Use the authored brief"
    assert duplicate["question"]["global_decision"] is True
    assert duplicate["ledger_sha256"] == first["ledger_sha256"]


@pytest.mark.parametrize("changes", [
    {"question": " "}, {"question": "x" * 2001}, {"global_decision": "yes"},
    {"global_decision": False}, {"affected_layers": ["nonexistent"]},
    {"affected_axes": ["unknown-axis"]}, {"path": "../questions.jsonl"},
])
def test_invalid_question_refuses_before_external_spend(bound, changes):
    with pytest.raises(ValueError, match="question"):
        execute(bound, [publish(bound), ("ask_supervisor", question(**changes))])
    assert not (bound[0].shot / escalate.QUESTIONS).exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["external"] == 1


def test_global_question_can_precede_mapping(bound):
    result = execute(bound, [("ask_supervisor", question())])[0]
    assert json.loads(result.data_json)["question"]["global_decision"] is True


def test_stale_owner_after_preparation_discards_only_its_staged_write(bound, monkeypatch):
    current = True
    original = escalate.prepare_question

    def expire(*args, **kwargs):
        nonlocal current
        prepared = original(*args, **kwargs)
        current = False
        return prepared

    def check():
        if not current:
            raise ValueError("run owner expired")

    monkeypatch.setattr(escalate, "prepare_question", expire)
    with pytest.raises(ValueError, match="owner expired"):
        execute(bound, [("ask_supervisor", question())], check=check)
    assert not (bound[0].shot / escalate.QUESTIONS).exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending().stage == "dispatched"


def test_concurrent_question_publication_is_preserved(bound, monkeypatch):
    original = escalate.commit_prepared_question

    def race(prepared, *, authority_binding):
        concurrent = escalate.prepare_question(
            bound[0].shot, layer="PLAN", question="Concurrent question", assumption="Preserve it",
            global_decision=True, authority_binding="concurrent",
        )
        original(concurrent, authority_binding="concurrent")
        return original(prepared, authority_binding=authority_binding)

    monkeypatch.setattr(escalate, "commit_prepared_question", race)
    with pytest.raises(escalate.prepared_publication.FilePublicationConflict):
        execute(bound, [("ask_supervisor", question())])
    stored, = escalate.load(bound[0].shot)
    assert stored["question"] == "Concurrent question"
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending().stage == "dispatched"


@pytest.mark.parametrize("payload", [b"{broken\n", b"[]\n", b'{"id":true}\n'])
def test_corrupt_question_stream_is_not_skipped_or_rewritten(bound, payload):
    path = bound[0].shot / escalate.QUESTIONS
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="question"):
        execute(bound, [("ask_supervisor", question())])
    assert path.read_bytes() == payload


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_reference_returns_exact_verified_bytes_without_external_spend(reference_bound, format, mime):
    bound = reference_bound(format=format)
    result = execute(bound, [("read_reference", {"name": "refs/f001.png"})])[0]
    data = json.loads(result.data_json)
    assert data["valid"] is True
    image, = [part for part in result.content if isinstance(part, flynn.ImageContent)]
    assert image.url.startswith(f"data:{mime};base64,")
    payload = base64.b64decode(image.url.split(",", 1)[1])
    assert payload == (bound[1] / "refs/f001.png").read_bytes()
    assert data["sha256"] == digest(payload)
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["external"] == 1
        assert journal.records()["commits"] == []


@pytest.mark.parametrize("name", ["../refs/f001.png", "/tmp/other.png", "refs/missing.png", "brief.md"])
def test_reference_name_must_be_in_bound_image_list(reference_bound, name):
    bound = reference_bound()
    with pytest.raises(ValueError, match="reference-image list"):
        execute(bound, [("read_reference", {"name": name})])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == 1


@pytest.mark.parametrize("kind", ["invalid", "gif", "oversized"])
def test_bad_image_content_is_explicit_refusal(reference_bound, kind):
    if kind == "gif":
        bound = reference_bound(format="GIF")
    else:
        bound = reference_bound(payload=b"invalid" if kind == "invalid" else b"x" * (image_inputs.MAX_IMAGE_BYTES + 1))
    result = execute(bound, [("read_reference", {"name": "refs/f001.png"})])[0]
    assert result.status == "refused" and json.loads(result.data_json)["valid"] is False
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.pending() is None
        assert journal.remaining()["external"] == 1


def test_reference_substitution_during_inference_refuses_before_read(reference_bound):
    bound = reference_bound()

    def substitute(_):
        (bound[1] / "refs/f001.png").write_bytes(b"substituted")

    with pytest.raises(PlanPublicationError, match="authored inputs changed"):
        execute(bound, [("read_reference", {"name": "refs/f001.png"})], after_inference=substitute)
    with flynn.SQLiteRun.open(bound[0].checkpoints / "global.sqlite") as journal:
        assert journal.remaining()["tool"] == 1
