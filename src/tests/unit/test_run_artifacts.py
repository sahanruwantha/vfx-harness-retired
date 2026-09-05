from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest

from tests.run_owner_support import fail_run, owned_run
from vfx_harness.agents.planner import PlanGateFailure, PlanLoopResult
from vfx_harness.agents.resilience import AgentSessionFailure
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EvidenceRecordAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EngineeringRouteCommitted,
    RouteEngineeringTarget,
    StopAction,
    action_idempotency_key,
)
from vfx_harness.observability import run_artifacts, transcript
from vfx_harness.orchestration import run_owner_boundary
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    canonical_view_hash,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _bundle_digest(payloads: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payloads[name]).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _write_selected_authority(
    shot: Path,
    *,
    publisher: str,
    published_at: str,
    plan_payload: bytes,
) -> str:
    view_documents = {
        "layers.json": {"schema": 5, "layers": []},
        "scene_checks.json": {"schema": 2, "contracts": []},
        "checks.json": {"schema": 2, "checks": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "requirements": [],
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
        },
        "acceptance.json": [],
    }
    payloads = {
        "global.md": plan_payload,
        **{
            name: (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            for name, document in view_documents.items()
        },
    }
    bundle_digest = _bundle_digest(payloads)
    bundle_root = (
        shot
        / "runs"
        / publisher
        / "checkpoints"
        / "plans"
        / "bundles"
        / bundle_digest
    )
    bundle_root.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (bundle_root / name).write_bytes(payload)
    (bundle_root / "bundle.json").write_text(
        json.dumps(
            {
                "schema": "vfx-harness.plan-bundle/v1",
                "run_id": publisher,
                "content_hash": bundle_digest,
                "outcome": "clean",
                "artifacts": {
                    name: hashlib.sha256(payload).hexdigest()
                    for name, payload in payloads.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pointer = shot / "plans" / "current.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.plan-pointer/v2",
                "revision": 1,
                "run_id": publisher,
                "bundle": bundle_root.relative_to(shot).as_posix(),
                "content_hash": bundle_digest,
                "outcome": "clean",
                "published_at": published_at,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    view_digest = canonical_view_hash(view_documents)
    view_root = shot / "state" / "view-sources" / publisher
    view_root.mkdir(parents=True, exist_ok=True)
    view_artifacts: dict[str, Path] = {}
    for name, document in view_documents.items():
        artifact = view_root / name
        artifact.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        view_artifacts[name] = artifact
    view_pointer = shot / "state" / "jit-layers" / "current.json"
    view_pointer.parent.mkdir(parents=True, exist_ok=True)
    view_pointer.write_text(
        json.dumps(
            {
                "schema": "vfx-harness.jit-layer-view/v2",
                "revision": 1,
                "plan_revision": 1,
                "bundle_hash": bundle_digest,
                "view_hash": view_digest,
                "materialized_layers": [],
                "artifacts": {
                    name: artifact.relative_to(shot).as_posix()
                    for name, artifact in view_artifacts.items()
                },
                "hashes": {
                    name: hashlib.sha256(artifact.read_bytes()).hexdigest()
                    for name, artifact in view_artifacts.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return bundle_digest


def _typed_harness_stop(
    run_id: str,
    *,
    evidence: StopEvidenceRef | None = None,
) -> StopEnvelope:
    facts = _digest("typed harness stop facts")
    attempt = _digest("typed attempt")
    if evidence is None:
        evidence = StopEvidenceRef(
            kind="stop_evidence",
            locator=f"runs/{run_id}/reports/test-harness-defect.json",
            sha256=_digest("typed defect bytes"),
            record_schema="vfx-harness.test-harness-defect/v1",
            record_digest=_digest("typed defect record"),
        )
    cause = StopCause(
        invariant_id="test_harness_invariant",
        finding_ids=("finding-1",),
        owner_scope_ids=("observability",),
        normalized_facts_digest=facts,
    )
    defect = EvidenceRecordAssertion(
        record_kind="defect",
        record_id="test-harness-defect",
        evidence=evidence,
    )
    target = RouteEngineeringTarget(
        cause_fingerprint=cause.fingerprint_for("harness_defect"),
        attempt_evidence_digest=attempt,
        owner_scope_ids=cause.owner_scope_ids,
        defect_record=defect,
        evidence=(evidence,),
        sink_id="engineering_handoff",
    )
    action = StopAction(
        target=target,
        postcondition=EngineeringRouteCommitted(
            defect_packet_digest=evidence.record_digest,
            sink_id=target.sink_id,
            owner_scope_ids=target.owner_scope_ids,
        ),
    )
    return StopEnvelope(
        stage="infrastructure",
        stop_class="harness_defect",
        identity=StopIdentity(
            run_id=run_id,
            bundle_digest=None,
            view_digest=None,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=None,
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=cause,
        attempt_evidence_digest=attempt,
        classification_evidence_digest=facts,
        artifact_state_digest=_digest("typed artifact state"),
        authoritative_before_digest=_digest("typed before state"),
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="test-harness-stop",
        expected="The invariant should hold.",
        found="The invariant failed.",
        next_action="Route the exact evidence to engineering.",
    )


def _terminal_stop_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes | None = None,
    declared_record_digest: str | None = None,
    additional_evidence: tuple[StopEvidenceRef, ...] = (),
) -> tuple[run_artifacts.RunLayout, StopEnvelope, Path]:
    shot = tmp_path / "terminal-stop-evidence"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    stack = ExitStack()
    layout, lease = stack.enter_context(owned_run(shot, "terminal-stop-001"))
    evidence_path = layout.reports / "test-harness-defect.json"
    document = {
        "schema": "vfx-harness.test-harness-defect/v1",
        "finding": "the exact typed defect",
    }
    if payload is None:
        evidence_path = layout.write_report("test-harness-defect", document)
    else:
        evidence_path.write_bytes(payload)
    evidence = StopEvidenceRef(
        kind="stop_evidence",
        locator=evidence_path.relative_to(layout.shot).as_posix(),
        sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        record_schema=document["schema"],
        record_digest=(declared_record_digest if declared_record_digest is not None else canonical_digest(document)),
    )
    envelope = _typed_harness_stop(layout.run_id, evidence=evidence)
    if additional_evidence:
        envelope = replace(
            envelope,
            evidence_refs=(evidence, *additional_evidence),
        )
    fail_run(layout, lease, envelope, exit_code=9)
    stack.close()
    return layout, envelope, evidence_path


def test_structured_run_has_one_machine_readable_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "shot-a"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    with owned_run(
        shot,
        "20260821T120000Z-a1b2c3",
        command="run",
        dispatch_kind="driver",
        shot_id="shot-a",
        parameters={"rounds": 2},
    ) as (layout, _lease):
        manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
        latest = json.loads((shot / "runs" / "latest.json").read_text(encoding="utf-8"))
        assert manifest["schema"] == "vfx-harness.run/v2"
        assert manifest["invocation"]["dispatch"] == {
            "schema": "vfx-harness.run-dispatch/v1",
            "kind": "driver",
            "command": "run",
        }
        assert manifest["reader_entrypoint"] == "manifest.json"
        assert manifest["layout"]["evidence"] == "evidence/"
        assert manifest["layout"]["owner_claim"] == "owner/claim.json"
        assert latest["run_id"] == layout.run_id
        assert run_artifacts.active(shot) == layout
        assert run_artifacts.latest(shot) == layout


def test_inventory_classifies_outputs_without_scanning_the_shot_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "shot-b"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "run-001")
    render = layout.evidence / "renders" / "layer-1-round-1.png"
    render.write_bytes(b"png")
    report = layout.reports / "layers" / "layer-1.json"
    report.write_text("{}\n", encoding="utf-8")

    layout.write_inventory()
    rows = json.loads(layout.inventory.read_text(encoding="utf-8"))["artifacts"]
    by_path = {row["path"]: row for row in rows}
    assert by_path["evidence/renders/layer-1-round-1.png"]["category"] == "evidence"
    assert by_path["reports/layers/layer-1.json"]["media_type"] == "application/json"


def test_transcripts_are_grouped_by_run_stage_and_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "shot-c"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.delenv("VFXH_NO_TRANSCRIPT", raising=False)
    layout = run_artifacts.create(shot, "run-002")

    path = transcript.bind(shot, "build", label="layer1", run_id=layout.run_id)
    assert path == layout.logs / "transcripts" / "build" / "layer1.jsonl"
    transcript.event("test_event", value=1)
    transcript.unbind()

    assert transcript.find(shot, stage="build", run_id=layout.run_id) == [path]
    assert transcript.run_id_for(path, shot) == layout.run_id
    assert [row["kind"] for row in transcript.read(path)] == ["open", "test_event", "close"]


def test_direct_output_writer_creates_a_structured_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "direct-shot"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    logs = run_artifacts.logs_dir(shot)
    layout = run_artifacts.active(shot)
    assert layout is not None
    assert logs == layout.logs
    assert run_artifacts.renders_dir(shot) == layout.evidence / "renders"
    assert not (shot / "logs").exists()
    assert not (shot / "renders").exists()


def test_direct_cli_invocation_publishes_terminal_status_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "direct-command"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-001")

    with run_owner_boundary.invocation(shot, "plan", shot_id="direct-command") as layout:
        (layout.logs / "command.log").write_text("ok\n", encoding="utf-8")

    assert json.loads(layout.status.read_text(encoding="utf-8"))["state"] == "passed"
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["command"] == "plan"
    assert summary["state"] == "passed"
    assert layout.inventory.is_file()


def test_typed_stop_is_published_and_read_back_before_direct_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "typed-stop"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "typed-stop-001")
    envelope = _typed_harness_stop("typed-stop-001")

    with (
        pytest.raises(run_artifacts.TypedStop),
        run_owner_boundary.invocation(shot, "build", shot_id="typed-stop") as layout,
    ):
        raise run_artifacts.TypedStop(9, envelope)

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop_class"] == "harness_defect"
    assert status["state"] == "failed"
    assert status["stop_envelope"] == "reports/stop-envelope.json"
    assert status["stop_envelope_digest"] == envelope.digest
    assert layout.read_stop_envelope(expected_digest=envelope.digest) == envelope


def test_terminal_stop_reader_requires_every_cited_evidence_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = StopEvidenceRef(
        kind="stop_evidence",
        locator="state/missing-terminal-evidence.json",
        sha256=_digest("missing bytes"),
        record_schema="vfx-harness.missing-terminal-evidence/v1",
        record_digest=_digest("missing record"),
    )
    layout, _envelope, _evidence_path = _terminal_stop_fixture(
        tmp_path,
        monkeypatch,
        additional_evidence=(missing,),
    )

    with pytest.raises(ValueError, match="cited stop evidence is missing"):
        layout.read_terminal_stop()


def test_terminal_stop_reader_rejects_tampered_evidence_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _envelope, evidence_path = _terminal_stop_fixture(tmp_path, monkeypatch)
    evidence_path.write_text('{"schema":"vfx-harness.test-harness-defect/v1"}\n')

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        layout.read_terminal_stop()


def test_terminal_stop_reader_rejects_malformed_typed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _envelope, _evidence_path = _terminal_stop_fixture(
        tmp_path,
        monkeypatch,
        payload=b"{not-json",
        declared_record_digest=_digest("declared malformed record"),
    )

    with pytest.raises(ValueError, match="malformed JSON"):
        layout.read_terminal_stop()


def test_terminal_stop_reader_rejects_stale_typed_record_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _envelope, _evidence_path = _terminal_stop_fixture(
        tmp_path,
        monkeypatch,
        declared_record_digest=_digest("stale record identity"),
    )

    with pytest.raises(ValueError, match="record digest mismatch"):
        layout.read_terminal_stop()


def test_untyped_terminal_boundary_fails_closed_as_harness_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "untyped-stop"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "untyped-stop-001")

    with (
        pytest.raises(RuntimeError, match="raw failure"),
        run_owner_boundary.invocation(shot, "plan", shot_id="untyped-stop") as layout,
    ):
        raise RuntimeError("raw failure text is not dispatch authority")

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    envelope = layout.read_stop_envelope(expected_digest=status["stop_envelope_digest"])
    assert envelope.stage == "infrastructure"
    assert envelope.stop_class == "harness_defect"
    assert envelope.cause.invariant_id == "terminal_boundary_requires_typed_stop"
    assert [action.transaction_id for action in envelope.actions] == ["route_engineering"]
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["terminal_cause"] == "process_error"


def test_unclassified_boundary_identity_is_restart_stable_until_authority_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "untyped-restart"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    first_bundle = _write_selected_authority(
        shot,
        publisher="publisher-a",
        published_at="2026-08-30T01:02:03+00:00",
        plan_payload=b"same semantic plan\n",
    )
    first_script = shot / "build" / "first-location.py"
    first_script.parent.mkdir(parents=True, exist_ok=True)
    first_script.write_text("# same accepted script\n", encoding="utf-8")
    (shot / "shot.json").write_text(
        json.dumps(
            {
                "shot": "untyped-restart",
                "milestones": {
                    "form": {
                        "status": "passed",
                        "frame": 1,
                        "script": first_script.relative_to(shot).as_posix(),
                        "script_sha": "accepted-script",
                        "unit_hash": _digest("unit"),
                        "artifact_unit_hash": _digest("unit"),
                        "run_id": "publisher-a",
                        "updated": "2026-08-30T01:02:03+00:00",
                        "best": {"render": "runs/publisher-a/evidence/render.png"},
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    first_layout = run_artifacts.create(
        shot,
        "untyped-restart-001",
        command="build",
        parameters={
            "run_id": "attempt-a",
            "started_at": "2026-08-30T01:02:03+00:00",
            "candidate": "/tmp/run-a/candidate.json",
        },
    )
    first = run_artifacts._unclassified_stop_envelope(
        first_layout,
        "build",
        RuntimeError("failure at /tmp/run-a on 2026-08-30T01:02:03+00:00"),
        code=7,
        terminal_cause="unknown:/tmp/run-a:2026-08-30T01:02:03+00:00",
    )

    second_bundle = _write_selected_authority(
        shot,
        publisher="publisher-b",
        published_at="2099-01-01T00:00:00+00:00",
        plan_payload=b"same semantic plan\n",
    )
    assert second_bundle == first_bundle
    second_script = shot / "other-build-root" / "second-location.py"
    second_script.parent.mkdir(parents=True, exist_ok=True)
    second_script.write_text(first_script.read_text(encoding="utf-8"), encoding="utf-8")
    (shot / "shot.json").write_text(
        json.dumps(
            {
                "shot": "untyped-restart",
                "milestones": {
                    "form": {
                        "status": "passed",
                        "frame": 1,
                        "script": second_script.relative_to(shot).as_posix(),
                        "script_sha": "accepted-script",
                        "unit_hash": _digest("unit"),
                        "artifact_unit_hash": _digest("unit"),
                        "run_id": "publisher-b",
                        "updated": "2099-01-01T00:00:00+00:00",
                        "best": {"render": "runs/publisher-b/evidence/elsewhere.png"},
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    second_layout = run_artifacts.create(
        shot,
        "untyped-restart-002",
        command="build",
        parameters={
            "run_id": "attempt-b",
            "started_at": "2099-01-01T00:00:00+00:00",
            "candidate": "/var/tmp/run-b/materialized.json",
        },
    )
    second = run_artifacts._unclassified_stop_envelope(
        second_layout,
        "build",
        RuntimeError("failure at /var/tmp/run-b on 2099-01-01T00:00:00+00:00"),
        code=7,
        terminal_cause="unknown:/var/tmp/run-b:2099-01-01T00:00:00+00:00",
    )

    assert first.identity.run_id != second.identity.run_id
    assert first.cause_fingerprint == second.cause_fingerprint
    assert first.authoritative_before_digest == second.authoritative_before_digest
    assert first.attempt_evidence_digest == second.attempt_evidence_digest
    assert first.evidence_refs[0].digest == second.evidence_refs[0].digest
    assert first.actions[0].digest == second.actions[0].digest
    assert action_idempotency_key(
        first.actions[0],
        authoritative_before_digest=first.authoritative_before_digest,
        attempt_evidence_digest=first.attempt_evidence_digest,
    ) == action_idempotency_key(
        second.actions[0],
        authoritative_before_digest=second.authoritative_before_digest,
        attempt_evidence_digest=second.attempt_evidence_digest,
    )
    first_audit = json.loads(
        (first_layout.reports / "unclassified-boundary-audit.json").read_text(
            encoding="utf-8"
        )
    )
    second_audit = json.loads(
        (second_layout.reports / "unclassified-boundary-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_audit["run_id"] != second_audit["run_id"]
    assert (
        first_audit["authority_sources"]["manifest"]["document"]["invocation"]
        ["parameters"]
        != second_audit["authority_sources"]["manifest"]["document"]["invocation"]
        ["parameters"]
    )
    assert (
        first_audit["authority_sources"]["selected_bundle"]["pointer"]["document"]
        ["published_at"]
        != second_audit["authority_sources"]["selected_bundle"]["pointer"]["document"]
        ["published_at"]
    )
    first_evidence = json.loads(
        (first_layout.reports / "unclassified-boundary-defect.json").read_text(
            encoding="utf-8"
        )
    )
    assert "parameters" not in first_evidence["authoritative_state"]
    assert first_evidence["legacy_terminal_cause"] == "unclassified_terminal_cause"

    changed_bundle = _write_selected_authority(
        shot,
        publisher="publisher-c",
        published_at="2099-01-02T00:00:00+00:00",
        plan_payload=b"changed semantic plan\n",
    )
    assert changed_bundle != first_bundle
    changed_layout = run_artifacts.create(
        shot,
        "untyped-restart-003",
        command="build",
        parameters={"candidate": "/tmp/irrelevant.json"},
    )
    changed = run_artifacts._unclassified_stop_envelope(
        changed_layout,
        "build",
        RuntimeError("same exception class"),
        code=7,
        terminal_cause="another unknown audit-only cause",
    )
    assert changed.authoritative_before_digest != first.authoritative_before_digest
    assert changed.attempt_evidence_digest != first.attempt_evidence_digest
    assert changed.evidence_refs[0].digest != first.evidence_refs[0].digest
    assert changed.actions[0].digest != first.actions[0].digest
    assert action_idempotency_key(
        changed.actions[0],
        authoritative_before_digest=changed.authoritative_before_digest,
        attempt_evidence_digest=changed.attempt_evidence_digest,
    ) != action_idempotency_key(
        first.actions[0],
        authoritative_before_digest=first.authoritative_before_digest,
        attempt_evidence_digest=first.attempt_evidence_digest,
    )


def test_inherited_stage_publishes_typed_stop_without_waiting_for_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "inherited-stop"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "inherited-001")
    envelope = _typed_harness_stop(layout.run_id)

    with (
        pytest.raises(run_artifacts.TypedStop),
        run_owner_boundary.invocation(shot, "accept", shot_id="inherited-stop"),
    ):
        raise run_artifacts.TypedStop(9, envelope)

    # An inherited stage publishes only its typed stop; the root owner selects status.
    assert layout.read_stop_envelope(expected_digest=envelope.digest) == envelope
    assert not layout.status.exists()
    assert not (layout.reports / "summary.json").exists()


def test_typed_stop_with_wrong_run_identity_is_not_publishable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "wrong-run-stop"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "right-run-001")

    with (
        pytest.raises(run_artifacts.TypedStop),
        run_owner_boundary.invocation(shot, "accept", shot_id="wrong-run-stop") as layout,
    ):
        raise run_artifacts.TypedStop(9, _typed_harness_stop("wrong-run-001"))

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert (status["state"], status["exit_code"]) == ("failed", 1)
    assert summary["terminal_cause"] == "stop_envelope_publication_failure"
    # The publication failure itself is selected as a harness defect, never the foreign envelope.
    selected = layout.read_terminal_stop()
    assert selected.stop_class == "harness_defect"
    assert selected.identity.run_id == layout.run_id


def test_dirty_plan_exit_publishes_failed_status_and_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "dirty-plan"
    shot.mkdir()
    plan = shot / "plans" / "global.md"
    plan.parent.mkdir()
    plan.write_text("# dirty but preserved\n", encoding="utf-8")
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-dirty")
    result = PlanLoopResult(plan, "budget", 2)

    with (
        pytest.raises(PlanGateFailure, match="2 blocking"),
        run_owner_boundary.invocation(shot, "plan", shot_id="dirty-plan") as layout,
    ):
        raise PlanGateFailure(result)

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["exit_code"] == 3
    assert "2 blocking" in status["detail"]
    assert summary["state"] == "failed"
    assert summary["exit_code"] == 3
    assert summary["terminal_cause"] == "plan_budget_exhausted"


def test_cancellation_without_recorded_intent_is_a_failure_not_an_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "interrupted"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-interrupted")

    with pytest.raises(KeyboardInterrupt), run_owner_boundary.invocation(shot, "plan") as layout:
        raise KeyboardInterrupt

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["exit_code"] == 130
    assert summary["terminal_cause"] == "cancelled_without_intent"
    envelope = layout.read_terminal_stop()
    assert envelope.stop_class == "harness_defect"
    assert envelope.actions[0].transaction_id == "route_engineering"


def test_model_turn_exhaustion_is_not_reported_as_generic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "turns"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-turns")

    with pytest.raises(AgentSessionFailure), run_owner_boundary.invocation(shot, "plan") as layout:
        raise AgentSessionFailure("draft exhausted its model turn budget", "max_turns_exhausted")

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert summary["terminal_cause"] == "max_turns_exhausted"
    assert "turn budget" in status["detail"]


def test_integer_systemexit_detail_is_the_meaning_not_the_digit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vfx build exit 7 wrote status.json detail '7'. str(SystemExit(7)) is truthy."""
    shot = tmp_path / "incomplete"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-incomplete")

    with pytest.raises(SystemExit) as raised, run_owner_boundary.invocation(shot, "build") as layout:
        raise SystemExit(7)

    assert raised.value.code == 7
    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert status["exit_code"] == 7
    assert status["detail"] == "INCOMPLETE CHAIN"
    assert status["detail"] != "7"


def test_requested_exit_keeps_the_exception_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "unpassed"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-unpassed")

    with pytest.raises(run_artifacts.RequestedExit), run_owner_boundary.invocation(shot, "build") as layout:
        raise run_artifacts.RequestedExit(7, "INCOMPLETE CHAIN — unit cam_spine failed")

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    assert status["exit_code"] == 7
    assert "cam_spine" in status["detail"]


def test_requested_exit_keeps_typed_terminal_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "model-failure"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    monkeypatch.setenv("VFXH_RUN_ID", "direct-model-failure")

    with pytest.raises(run_artifacts.RequestedExit), run_owner_boundary.invocation(shot, "build") as layout:
        raise run_artifacts.RequestedExit(
            3,
            "BUILD TRUNCATED — provider returned HTTP 429",
            terminal_cause="model_session_failure",
        )

    status = json.loads(layout.status.read_text(encoding="utf-8"))
    summary = json.loads((layout.reports / "summary.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 3
    assert summary["terminal_cause"] == "model_session_failure"


def test_reader_refuses_shot_root_legacy_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shot = tmp_path / "unsupported-output"
    (shot / "renders").mkdir(parents=True)
    (shot / "renders" / "old.png").write_bytes(b"old")
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    with pytest.raises(FileNotFoundError, match="no structured run"):
        run_artifacts.readable_renders_dir(shot)


class _ArtifactExecutionPolicyError(Exception):
    pass


def test_an_unclassified_boundary_puts_its_cause_in_the_operator_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIR-0226: the envelope prose must carry what the audit beside it records.

    `detail` is `f"{stop_class}: {found} {next_action}"`, so `found` is exactly what an
    operator reads in status.json and reports/summary.json. It named only the boundary,
    while `unclassified-boundary-audit.json` recorded `exception_type` and
    `exception_message` faithfully one file away. Four distinct causes were lost that way
    in a single day across three shots.
    """
    shot = tmp_path / "shot-boundary"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "boundary-001")

    exc = _ArtifactExecutionPolicyError("artifact import denied: itertools")
    # Mirror the boundary exactly: run_owner_boundary derives both from terminal_record.
    _state, code, cause, _detail = run_artifacts.terminal_record(exc)
    envelope = run_artifacts.publish_exception_stop(
        layout, "build", exc, code=code, terminal_cause=cause
    )

    # The operator-facing sentence names the cause, not only the boundary.
    assert "artifact import denied: itertools" in envelope.found
    assert "_ArtifactExecutionPolicyError" in envelope.found
    assert "returned without typed stop authority" in envelope.found

    # And the next action points at the verbatim record rather than a boundary name.
    assert "unclassified-boundary-audit" in envelope.next_action
    assert "Read that exception first" in envelope.next_action
    # One legal action still, and it is still engineering.
    assert "route the boundary and exact attempt evidence to engineering" in envelope.next_action

    # detail is what status.json carries; it must contain the cause end to end.
    detail = f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
    assert "itertools" in detail

    # The audit record and the prose describe the same exception.
    audit = json.loads(
        (layout.reports / "unclassified-boundary-audit.json").read_text(encoding="utf-8")
    )
    assert audit["exception_message"] == "artifact import denied: itertools"
    assert audit["exception_message"] in envelope.found


class _LayerFinalizationConflict(Exception):
    pass


# The four causes this boundary actually swallowed on 2026-09-05, read back from the
# `unclassified-boundary-audit.json` records of three shots. Each names its own owner:
# a finalization claim recoverable by `vfx finalizations release`, a capsule-ordering
# defect, a denied artifact import, and a dead pipe. The envelope told an operator only
# that a boundary returned.
_SWALLOWED = (
    (
        _LayerFinalizationConflict("layer 1 already has an active finalization claim"),
        "already has an active finalization claim",
    ),
    (
        ValueError("selected authority capsules do not preserve the stable topological layer order"),
        "do not preserve the stable topological layer order",
    ),
    (
        _ArtifactExecutionPolicyError("artifact import denied: itertools"),
        "artifact import denied: itertools",
    ),
    (BrokenPipeError(32, "Broken pipe"), "Broken pipe"),
)


@pytest.mark.parametrize("exc, expected", _SWALLOWED, ids=[type(e).__name__ for e, _ in _SWALLOWED])
def test_every_swallowed_cause_survives_into_the_published_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected: str
) -> None:
    """HIR-0226: one boundary, four distinct causes, one indistinguishable sentence."""
    shot = tmp_path / "shot-swallowed"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "swallowed-001")

    _state, code, cause, _detail = run_artifacts.terminal_record(exc)
    envelope = run_artifacts.publish_exception_stop(
        layout, "build", exc, code=code, terminal_cause=cause
    )

    assert type(exc).__name__ in envelope.found
    assert expected in envelope.found


def test_the_envelope_label_is_bounded_and_whitespace_normalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`found` reaches status.json through `detail`; a multi-line exception cannot run away."""
    shot = tmp_path / "shot-noisy"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "noisy-001")

    noisy = ValueError("first line\n   second   line\t\tthird " + "x " * 400)
    _state, code, cause, _detail = run_artifacts.terminal_record(noisy)
    envelope = run_artifacts.publish_exception_stop(
        layout, "build", noisy, code=code, terminal_cause=cause
    )

    assert "first line second line third" in envelope.found
    assert "\n" not in envelope.found and "\t" not in envelope.found
    assert "…" in envelope.found
    # Bounded: the message contributes at most the cap, not the whole 800-character string.
    assert len(envelope.found) < 500
    # And the composed detail still fits the 1000-character summary/status cap, so the
    # cause is not itself truncated away by the consumer that carries it.
    detail = f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
    assert len(detail) <= 1000
    assert "first line second line third" in detail[:1000]


def test_an_exception_with_no_message_still_names_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = tmp_path / "shot-silent"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    layout = run_artifacts.create(shot, "silent-001")

    silent = RuntimeError()
    _state, code, cause, _detail = run_artifacts.terminal_record(silent)
    envelope = run_artifacts.publish_exception_stop(
        layout, "build", silent, code=code, terminal_cause=cause
    )

    assert "builtins.RuntimeError" in envelope.found
    assert envelope.found.rstrip().endswith("RuntimeError")


def test_carrying_the_message_does_not_move_the_stop_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIR-0214's identity is type-level; HIR-0226 changes prose only.

    The controller refuses a cause fingerprint already dispatched in the shot, so if the
    message reached `classification_digest` two runs of one defect would stop colliding
    and a genuine repeat would dispatch twice. This guard passes with and without the
    mechanism, deliberately: it asserts identity did not move.
    """
    shot = tmp_path / "shot-identity"
    shot.mkdir()
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    envelopes = []
    for index, message in enumerate(("layer 1 already has a claim", "layer 7 already has a claim")):
        layout = run_artifacts.create(shot, f"identity-00{index}")
        exc = _LayerFinalizationConflict(message)
        _state, code, cause, _detail = run_artifacts.terminal_record(exc)
        envelopes.append(
            run_artifacts.publish_exception_stop(
                layout, "build", exc, code=code, terminal_cause=cause
            )
        )

    first, second = envelopes
    assert first.cause_fingerprint == second.cause_fingerprint
    assert first.cause.finding_ids == second.cause.finding_ids
    # The prose still separates them for the operator.
    assert first.found != second.found
    assert "layer 1" in first.found and "layer 7" in second.found
