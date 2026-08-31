from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.materialization_support import attest_exact_materialization_view
from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _candidate,
    _jit_payload,
    _passed_layer_one_outcome,
    _write,
    _write_authority_record,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization import publish_materialization
from vfx_harness.orchestration.ledger import load_milestones
from vfx_harness.orchestration.plan_authority import (
    PlanPublicationError,
    prepare_consumer_view,
    publish_current,
    selected_artifact_path,
)

_MATERIALIZED_MOMENT = {
    "id": "M-polish-lock",
    "frame": 240,
    "ref": "refs/a.png",
    "reads": "the materialized polish layer preserves the final lock",
    "strip": [239],
    "fingerprint": {"axis": "final_lock", "property": "frame_delta"},
    "layer": "2",
}


def _publish_materialized_acceptance_view(root: Path) -> Path:
    """Publish the complete JIT producer path with one layer-owned moment."""

    _candidate(root)
    (root / "refs" / "a.png").write_bytes(b"reference")
    _add_deferred_layer(root)
    plan_layout = run_artifacts.create(root, "plan-with-jit-acceptance")
    bundle = publish_current(root, plan_layout, outcome="clean_with_deferred")
    payload = _jit_payload(root, bundle.content_hash)
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["acceptance"] = [_MATERIALIZED_MOMENT]
    _write(payload, document)
    _passed_layer_one_outcome(root)
    attest_exact_materialization_view(root, payload)
    return publish_materialization(root, payload)


def test_selected_jit_acceptance_reaches_every_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    pointer_path = _publish_materialized_acceptance_view(tmp_path)
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))

    selected = selected_artifact_path(tmp_path, "acceptance.json")
    expected = (tmp_path / pointer["artifacts"]["acceptance.json"]).resolve()
    assert selected == expected
    assert selected.parent.name == pointer["view_hash"]
    assert json.loads(selected.read_text(encoding="utf-8")) == [_MATERIALIZED_MOMENT]

    consumer_layout = run_artifacts.create(tmp_path, "acceptance-consumer")
    consumer_view = prepare_consumer_view(consumer_layout)
    staged_acceptance = consumer_view / "acceptance.json"
    assert staged_acceptance.is_symlink()
    assert staged_acceptance.resolve() == selected
    assert json.loads(staged_acceptance.read_text(encoding="utf-8")) == [
        _MATERIALIZED_MOMENT
    ]

    shot = Shot(
        folder=tmp_path,
        frontmatter={"id": "jit-acceptance", "frames": 240, "fps": 24},
        body="fixture",
    )
    milestones = load_milestones(shot)
    assert list(milestones) == ["M-polish-lock"]
    assert milestones["M-polish-lock"].frame == 240
    assert milestones["M-polish-lock"].strip == (239,)
    assert milestones["M-polish-lock"].fingerprint == {
        "axis": "final_lock",
        "property": "frame_delta",
    }


def test_selected_jit_acceptance_rejects_forged_view_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    pointer_path = _publish_materialized_acceptance_view(tmp_path)
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["view_hash"] = "0" * 64 if pointer["view_hash"] != "0" * 64 else "1" * 64
    _write_authority_record(pointer_path, pointer)

    with pytest.raises(
        PlanPublicationError,
        match="must use exact producer locator",
    ):
        selected_artifact_path(tmp_path, "acceptance.json")
