"""Finished-chain rejection is a typed terminal boundary, never apparent success."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image

import vfx_harness.agents.acceptance_stop_evidence as stop_evidence
import vfx_harness.agents.builder.critic as critic_runtime
from vfx_harness.agents import acceptance, acceptance_stop
from vfx_harness.domain.acceptance_outcomes import AcceptanceOutcome
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopEnvelope
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
)
from vfx_harness.orchestration.ledger import Milestone


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _shot(root: Path) -> Shot:
    (root / "refs").mkdir(parents=True)
    (root / "refs" / "M1.png").write_bytes(b"reference")
    return Shot(
        folder=root,
        frontmatter={
            "id": "acceptance-fixture",
            "frames": 1,
            "fps": 24,
            "resolution": [16, 16],
            "engine": "BLENDER_EEVEE_NEXT",
        },
        body="fixture",
    )


def _authority() -> acceptance_stop.AcceptanceAuthoritySnapshot:
    return acceptance_stop.AcceptanceAuthoritySnapshot(
        bundle_digest=_digest("bundle"),
        view_digest=_digest("view"),
        judgment_debt_state_digest=_digest("judgment-debt-state"),
        acceptance_artifact_sha256=_digest("acceptance"),
        layers_artifact_sha256=_digest("layers"),
        selected_moments=(
            {
                "id": "M1",
                "frame": 1,
                "ref": "refs/M1.png",
                "ref_sha256": _digest("reference"),
                "fingerprint": "",
            },
        ),
        chain=(
            {
                "layer_id": "L1",
                "status": "passed",
                "script": "build/units/L1/form.py",
                "script_sha256": _digest("script"),
                "finalization_receipt_digest": _digest("finalization receipt"),
                "layer_outcome": "plans/outcomes/layer-L1.json",
                "layer_outcome_sha256": _digest("layer outcome"),
                "replay_dependencies": [],
                "units": [
                    {
                        "unit_id": "form",
                        "unit_digest": _digest("unit"),
                        "completion_receipt_digest": _digest("completion receipt"),
                        "script": "build/units/L1/form.py",
                        "script_sha256": _digest("script"),
                    }
                ],
            },
        ),
    )


def _render_receipt(path: Path, *, frame: int = 1) -> dict:
    payload = {
        "schema": "vfx-harness.canonical-render-capture/v1",
        "frame": frame,
        "mode": "eevee",
        "scale": 0.5,
        "resolution": [16, 16, 100],
        "render_state": {
            "engine": "BLENDER_EEVEE_NEXT",
            "resolution": [16, 16, 100],
            "image_settings": {
                "file_format": "PNG",
                "color_mode": "RGBA",
                "color_depth": "8",
            },
            "workbench_shading": "SOLID",
            "eevee_render_samples": 64,
            "color_management": {
                "display_device": "sRGB",
                "view_transform": "AgX",
                "look": "",
                "exposure": 0.0,
                "gamma": 1.0,
            },
        },
        "warnings": [],
        "png_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return {**payload, "capture_digest": canonical_digest(payload)}


def _refresh_receipt_digest(result: dict) -> None:
    receipt = result["render_capture"]
    payload = {key: value for key, value in receipt.items() if key != "capture_digest"}
    receipt["capture_digest"] = canonical_digest(payload)


def _contract_reading(*, authoritative: bool, passed: bool = False) -> dict:
    return {
        "id": "framing-contract",
        "axis": "composition",
        "metric": "visible_fraction",
        "value": 0.2,
        "target": ">= 0.5",
        "pass": passed,
        "origin": "planner",
        "source": "image_contract",
        "authoritative": authoritative,
        "owner_layer": "L1",
        "error": "visible fraction below the required target" if not passed else "",
    }


def _failed_results(
    shot: Shot,
    layout: run_artifacts.RunLayout,
    *,
    pixels: bytes = b"failed pixels",
    audit_label: str = "baseline",
    reference: str = "refs/M1.png",
) -> dict[str, dict]:
    render = layout.evidence / "renders" / "M1_accept.png"
    render.write_bytes(pixels)
    receipt = _render_receipt(render)
    receipt_payload = {key: value for key, value in receipt.items() if key != "capture_digest"}
    receipt_payload["warnings"] = [f"diagnostic {audit_label} at {render}"]
    receipt = {**receipt_payload, "capture_digest": canonical_digest(receipt_payload)}
    return {
        "M1": {
            "schema": stop_evidence.MOMENT_SCHEMA,
            "frame": 1,
            "ref": reference,
            "render": render.relative_to(shot.folder).as_posix(),
            "render_capture": receipt,
            "mean": 2.0,
            "pass": False,
            "critic_pass": False,
            "decided_by": "metrics",
            "metric_failures": [f"exposure_mean failed; diagnostic {audit_label}"],
            "metric_readings": [
                {
                    "metric_id": "exposure_mean",
                    "value": 0.2,
                    "reference": 0.5,
                    "relative_delta": -0.6,
                    "blocking": True,
                }
            ],
            "scores": {"composition": 2.0},
            "issues": [f"same causal failure; audit {audit_label}"],
            "contract_evidence": [
                {
                    **_contract_reading(authoritative=True),
                    "error": f"diagnostic {audit_label} at {render}",
                }
            ],
        }
    }


def _change_semantic_field(result: dict, field: str) -> None:
    if field == "render_scale":
        result["render_capture"]["scale"] = 0.75
        _refresh_receipt_digest(result)
    elif field == "render_engine":
        result["render_capture"]["render_state"]["engine"] = "BLENDER_EEVEE"
        _refresh_receipt_digest(result)
    elif field == "mean":
        result["mean"] = 2.5
    elif field == "score":
        result["scores"]["composition"] = 2.5
    elif field == "metric_value":
        result["metric_readings"][0]["value"] = 0.25
    elif field == "contract_value":
        result["contract_evidence"][0]["value"] = 0.25
    else:  # pragma: no cover - the parameter list below is closed
        raise AssertionError(f"unknown semantic test field {field!r}")


def _malform_moment(result: dict, case: str) -> None:
    if case == "wrong_schema":
        result["schema"] = "vfx-harness.acceptance-moment-evidence/v0"
    elif case == "extra_moment_field":
        result["source_path"] = "/tmp/not-authority.png"
    elif case == "string_frame":
        result["frame"] = "1"
    elif case == "wrong_render_mode":
        result["render_capture"]["mode"] = "workbench"
        _refresh_receipt_digest(result)
    elif case == "empty_render_state":
        result["render_capture"]["render_state"] = {}
        _refresh_receipt_digest(result)
    elif case == "extra_render_state_field":
        result["render_capture"]["render_state"]["filepath"] = "/tmp/render.png"
        _refresh_receipt_digest(result)
    elif case == "integer_score_axis":
        result["scores"] = {1: 2.0}
    elif case == "extra_contract_field":
        result["contract_evidence"][0]["diagnostic"] = "opaque"
    elif case == "string_contract_value":
        result["contract_evidence"][0]["value"] = "0.2"
    elif case == "duplicate_contract_id":
        result["contract_evidence"].append(copy.deepcopy(result["contract_evidence"][0]))
    elif case == "duplicate_metric_id":
        result["metric_readings"].append(copy.deepcopy(result["metric_readings"][0]))
    elif case == "string_metric_value":
        result["metric_readings"][0]["value"] = "0.2"
    elif case == "contradictory_pass":
        result["pass"] = True
    elif case == "contradictory_decision_source":
        result["decided_by"] = "critic"
    else:  # pragma: no cover - the parameter list below is closed
        raise AssertionError(f"unknown malformed test case {case!r}")


def _patch_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    shot: Shot,
    layout: run_artifacts.RunLayout,
    *,
    passed: bool,
) -> list[str]:
    authority = _authority()
    selected_authority = SimpleNamespace(plan=None, artifact_paths={})
    moments = {"M1": Milestone("M1", 1, "refs/M1.png", "finished frame")}
    transcript_events: list[str] = []

    monkeypatch.setattr(
        acceptance,
        "resolve_selected_authority",
        lambda _folder: selected_authority,
    )
    monkeypatch.setattr(
        acceptance,
        "commit_selected_authority",
        lambda _folder, _selected, *, operation, mutation: mutation(),
    )
    monkeypatch.setattr(
        acceptance,
        "require_judgment_debts_satisfied",
        lambda _folder, _selected=None: None,
    )
    monkeypatch.setattr(
        acceptance,
        "load_milestones",
        lambda _shot, _selected=None: moments,
    )
    monkeypatch.setattr(
        acceptance.acceptance_stop,
        "capture_acceptance_authority",
        lambda _shot, _moments, _selected=None, **_kwargs: authority,
    )
    monkeypatch.setattr(
        acceptance,
        "_prepare_acceptance_replay_inputs",
        lambda *_args, **_kwargs: (),
    )

    async def axes(*_args, **_kwargs):
        return [("composition", "composition match")]

    monkeypatch.setattr(acceptance, "ensure_axes", axes)
    monkeypatch.setattr(acceptance.transcript, "bind", lambda *_args: None)
    monkeypatch.setattr(
        acceptance.transcript,
        "event",
        lambda name, **_kwargs: transcript_events.append(name),
    )
    monkeypatch.setattr(
        acceptance.transcript,
        "unbind",
        lambda: transcript_events.append("unbind"),
    )
    monkeypatch.setattr(acceptance, "_chain", lambda *_args, **_kwargs: ["build/L1.py"])

    def stash(*_args, **_kwargs):
        render = layout.evidence / "renders" / "M1_accept.png"
        render.write_bytes(b"candidate-pass" if passed else b"candidate-fail")
        return render.relative_to(shot.folder).as_posix(), _render_receipt(render)

    monkeypatch.setattr(acceptance, "_stash_render_with_receipt", stash)
    monkeypatch.setattr(acceptance, "look_pair", lambda *_args: ({}, {}))
    monkeypatch.setattr(acceptance, "compare", lambda *_args: [])
    monkeypatch.setattr(acceptance, "report", lambda _rows: "")

    async def judge(*_args, **_kwargs):
        return {
            "mean": 4.5 if passed else 2.0,
            "pass": passed,
            "scores": {"composition": 5 if passed else 2},
            "issues": [] if passed else ["subject framing misses the selected moment"],
        }

    monkeypatch.setattr(acceptance, "_judge", judge)
    monkeypatch.setattr(acceptance, "acceptance_evidence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(acceptance, "reconcile", lambda *_args: [])
    monkeypatch.setattr(
        acceptance,
        "repair_plan",
        lambda *_args: (
            [
                {
                    "layer": "L1",
                    "axes": ["composition"],
                    "moments": ["M1"],
                    "invalidates": [],
                }
            ]
            if not passed
            else []
        ),
    )
    return transcript_events


def test_failed_acceptance_persists_evidence_then_raises_typed_human_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    (tmp_path / "shot.json").write_text(
        json.dumps(
            {
                "shot": shot.id,
                "milestones": {"L1": {"status": "passed"}},
            }
        ),
        encoding="utf-8",
    )
    layout = run_artifacts.create(tmp_path, "acceptance-failed")
    events = _patch_acceptance(monkeypatch, shot, layout, passed=False)
    resolution_calls: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: resolution_calls.append("resolve"),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail("failed moments stop before final due clearance"),
    )

    with pytest.raises(run_artifacts.TypedStop) as raised:
        anyio.run(acceptance.accept, shot, None, None, False, False, True)

    envelope = raised.value.stop_envelope
    assert raised.value.code == 9
    assert envelope.stop_class == "human_decision_required"
    assert envelope.stage == "acceptance"
    assert [action.transaction_id for action in envelope.actions] == ["escalate_question"]
    assert envelope.actions[0].dispatch_mode == "human_handoff"
    assert envelope.identity.bundle_digest == _authority().bundle_digest
    assert envelope.identity.view_digest == _authority().view_digest
    assert envelope.evidence_refs[0].record_schema == "vfx-harness.acceptance-stop-evidence/v1"
    assert StopEnvelope.from_dict(envelope.as_dict(), "envelope") == envelope
    assert events[-2:] == ["accept_end", "unbind"]

    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert ledger["acceptance"]["passed"] == 0
    assert ledger["acceptance"]["repaired"] == []
    assert ledger["acceptance"]["repair_plan"][0]["layer"] == "L1"
    assert ledger["milestones"]["L1"]["status"] == "passed"
    failed_outcome = AcceptanceOutcome.from_dict(
        ledger["acceptance"]["outcome"],
        "acceptance.outcome",
    )
    assert failed_outcome.passed is False
    assert resolution_calls == ["resolve"]
    evidence = json.loads((layout.reports / "acceptance-stop-evidence.json").read_text(encoding="utf-8"))
    assert evidence["moments"][0]["moment_id"] == "M1"
    assert evidence["question"]["decision_authority_id"] == "acceptance-review"
    assert evidence["question"]["allowed_answer_ids"] == [
        "abstain",
        "review_authority_amendment",
        "review_unit:L1:form",
    ]
    assert (
        evidence["moments"][0]["render_sha256"] == ledger["acceptance"]["moments"]["M1"]["render_capture"]["png_sha256"]
    )


def test_no_optical_signal_skips_model_and_survives_typed_acceptance_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    (tmp_path / "shot.json").write_text(
        json.dumps(
            {
                "shot": shot.id,
                "milestones": {"L1": {"status": "passed"}},
            }
        ),
        encoding="utf-8",
    )
    layout = run_artifacts.create(tmp_path, "acceptance-no-optical-signal")
    _patch_acceptance(monkeypatch, shot, layout, passed=False)

    def stash_black(*_args, **_kwargs):
        render = layout.evidence / "renders" / "M1_accept.png"
        Image.new("RGB", (16, 16), (0, 0, 0)).save(render)
        return render.relative_to(shot.folder).as_posix(), _render_receipt(render)

    model_calls: list[str] = []

    async def fail_if_model_called(*_args, **_kwargs):
        model_calls.append("called")
        raise AssertionError("a no-signal acceptance plate must not call the critic")

    monkeypatch.setattr(acceptance, "_stash_render_with_receipt", stash_black)
    monkeypatch.setattr(acceptance, "_judge", critic_runtime._judge)
    monkeypatch.setattr(critic_runtime, "_critique", fail_if_model_called)
    monkeypatch.setattr(acceptance, "resolve_acceptance_completion", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail("failed no-signal evidence cannot clear acceptance"),
    )

    with pytest.raises(run_artifacts.TypedStop):
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert model_calls == []
    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    moment = ledger["acceptance"]["moments"]["M1"]
    assert moment["decided_by"] == "no_optical_signal"
    typed = AcceptanceOutcome.from_dict(
        ledger["acceptance"]["outcome"],
        "acceptance.outcome",
    )
    assert typed.moments[0].decided_by == "no_optical_signal"
    evidence = json.loads(
        (layout.reports / "acceptance-stop-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["moments"][0]["decided_by"] == "no_optical_signal"
    assert evidence["question"]["failed"][0]["failure_modes"] == [
        "no_optical_signal"
    ]


def test_passed_acceptance_returns_success_and_clears_final_due_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-passed")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    calls: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: calls.append("resolve"),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: calls.append("clear"),
    )

    result = anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert result["passed"] == result["total"] == 1
    assert calls == ["resolve", "clear"]
    assert not (layout.reports / "acceptance-stop-evidence.json").exists()
    stored = AcceptanceOutcome.from_dict(result["outcome"], "acceptance.outcome")
    assert stored.passed is True
    monkeypatch.setattr(
        acceptance_stop,
        "load_milestones",
        lambda _shot, _selected=None: {
            "M1": Milestone("M1", 1, "refs/M1.png", "finished frame")
        },
    )
    monkeypatch.setattr(
        acceptance_stop,
        "require_judgment_debts_satisfied",
        lambda _folder, _selected=None: None,
    )
    monkeypatch.setattr(
        acceptance_stop,
        "require_due_clear",
        lambda *_args, **_kwargs: None,
    )
    assert acceptance_stop.require_current_accepted_outcome(shot) == stored
    selected_authority = SimpleNamespace(selection_token=object())
    monkeypatch.setattr(
        acceptance_stop,
        "resolve_selected_authority",
        lambda _root: pytest.fail("supplied final-render snapshot was re-resolved"),
    )
    assert (
        acceptance_stop.require_current_accepted_outcome(
            shot,
            selected_authority,
        )
        == stored
    )

    monkeypatch.setattr(
        acceptance_stop,
        "require_judgment_debts_satisfied",
        lambda _folder, _selected=None: (_ for _ in ()).throw(
            ValueError("judgment debt became due")
        ),
    )
    with pytest.raises(ValueError, match="judgment debt became due"):
        acceptance_stop.require_current_accepted_outcome(shot, selected_authority)
    monkeypatch.setattr(
        acceptance_stop,
        "require_judgment_debts_satisfied",
        lambda _folder, _selected=None: None,
    )
    monkeypatch.setattr(
        acceptance_stop,
        "require_due_clear",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("acceptance obligation became due")
        ),
    )
    with pytest.raises(ValueError, match="acceptance obligation became due"):
        acceptance_stop.require_current_accepted_outcome(shot, selected_authority)
    monkeypatch.setattr(
        acceptance_stop,
        "require_due_clear",
        lambda *_args, **_kwargs: None,
    )

    render = layout.evidence / "renders" / "M1_accept.png"
    render.write_bytes(b"substituted after acceptance")
    with pytest.raises(ValueError, match="png_sha256 is stale"):
        acceptance_stop.require_current_accepted_outcome(shot, selected_authority)


def test_forced_acceptance_is_run_scoped_and_cannot_promote_or_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    ledger_path = tmp_path / "shot.json"
    ledger_path.write_text(
        json.dumps(
            {
                "shot": shot.id,
                "milestones": {"L1": {"status": "pending"}},
                "acceptance": {"sentinel": "unchanged"},
            }
        ),
        encoding="utf-8",
    )
    before = ledger_path.read_bytes()
    layout = run_artifacts.create(tmp_path, "acceptance-forced-preview")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: pytest.fail(
            "forced acceptance cannot discharge plan resolutions"
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail(
            "forced acceptance cannot clear final due records"
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "reconcile",
        lambda *_args, **_kwargs: pytest.fail(
            "forced acceptance cannot supersede accepted work"
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "repair_plan",
        lambda *_args, **_kwargs: pytest.fail(
            "forced acceptance cannot publish a repair route"
        ),
    )

    result = anyio.run(acceptance.accept, shot, None, None, False, True, True)

    assert result["authoritative"] is False
    assert result["reason"] == "forced_debug_preview"
    assert "outcome" not in result
    assert ledger_path.read_bytes() == before


def test_authority_change_before_commit_prevents_resolution_and_ledger_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-authority-cas")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    before = _authority()
    after = replace(
        before,
        judgment_debt_state_digest=_digest("changed-judgment-debt-state"),
    )
    snapshots = iter((before, after))
    monkeypatch.setattr(
        acceptance.acceptance_stop,
        "capture_acceptance_authority",
        lambda _shot, _moments, _selected=None: next(snapshots),
    )
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: pytest.fail(
            "changed authority cannot discharge plan resolutions"
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail(
            "changed authority cannot clear final due records"
        ),
    )

    with pytest.raises(ValueError, match="changed during judgment"):
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert not (tmp_path / "shot.json").exists()


def test_exact_head_change_before_resolution_commit_leaves_no_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-resolution-head-race")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: pytest.fail(
            "a stale acceptance must not publish completion evidence"
        ),
    )

    def reject(_folder, _selected, *, operation, mutation):
        del mutation
        assert operation == "resolve acceptance completion authority"
        raise AuthoritySelectionConflict("injected exact-head change")

    monkeypatch.setattr(acceptance, "commit_selected_authority", reject)

    with pytest.raises(AuthoritySelectionConflict, match="exact-head change"):
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert not (tmp_path / "shot.json").exists()


def test_exact_head_change_before_ledger_commit_does_not_publish_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-ledger-head-race")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    resolved: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: resolved.append("resolved"),
    )
    monkeypatch.setattr(acceptance, "require_due_clear", lambda *_args, **_kwargs: None)
    commits: list[str] = []

    def change_after_resolution(_folder, _selected, *, operation, mutation):
        commits.append(operation)
        if len(commits) == 1:
            return mutation()
        raise AuthoritySelectionConflict("injected exact-head change")

    monkeypatch.setattr(
        acceptance,
        "commit_selected_authority",
        change_after_resolution,
    )

    with pytest.raises(AuthoritySelectionConflict, match="exact-head change"):
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert resolved == ["resolved"]
    assert commits == [
        "resolve acceptance completion authority",
        "publish acceptance outcome",
    ]
    assert not (tmp_path / "shot.json").exists()


def test_acceptance_keeps_one_snapshot_across_same_semantic_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned semantic view cannot substitute for the entry selection token."""

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-selection-aba")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    semantic = SimpleNamespace(bundle_digest=_digest("bundle"), view_digest=_digest("view"))
    entry = SimpleNamespace(selection_token=(1, 1), assertion=semantic)
    returned = SimpleNamespace(selection_token=(3, 3), assertion=semantic)
    resolution_calls: list[Path] = []
    observed: list[tuple[str, object]] = []

    def resolve(root: Path) -> object:
        resolution_calls.append(Path(root))
        return entry if len(resolution_calls) == 1 else returned

    def require_debts(_root: Path, selected: object) -> None:
        observed.append(("debts", selected))

    moments = {"M1": Milestone("M1", 1, "refs/M1.png", "finished frame")}

    def load_selected_moments(_shot: Shot, selected: object) -> dict[str, Milestone]:
        observed.append(("moments", selected))
        return moments

    def capture(_shot: Shot, _moments: object, selected: object):
        observed.append(("capture", selected))
        return _authority()

    async def axes(_shot: Shot, _verbose: bool, selected: object):
        observed.append(("axes", selected))
        return [("composition", "composition match")]

    def chain(*_args, selected_authority: object, **_kwargs) -> list[str]:
        observed.append(("chain", selected_authority))
        return ["build/L1.py"]

    def contract_evidence(
        _folder: Path,
        *,
        selected_authority: object,
        **_kwargs,
    ) -> list[dict]:
        observed.append(("checks", selected_authority))
        return []

    def reconcile(_shot: Shot, _results: dict, _ledger: object, selected: object) -> list[str]:
        observed.append(("reconcile", selected))
        return []

    def repair_plan(_shot: Shot, _results: dict, selected: object) -> list[dict]:
        observed.append(("repair", selected))
        return []

    monkeypatch.setattr(acceptance, "resolve_selected_authority", resolve)
    monkeypatch.setattr(acceptance, "require_judgment_debts_satisfied", require_debts)
    monkeypatch.setattr(acceptance, "load_milestones", load_selected_moments)
    monkeypatch.setattr(acceptance.acceptance_stop, "capture_acceptance_authority", capture)
    monkeypatch.setattr(acceptance, "ensure_axes", axes)
    monkeypatch.setattr(acceptance, "_chain", chain)
    monkeypatch.setattr(acceptance, "acceptance_evidence", contract_evidence)
    monkeypatch.setattr(acceptance, "reconcile", reconcile)
    monkeypatch.setattr(acceptance, "repair_plan", repair_plan)
    monkeypatch.setattr(acceptance, "resolve_acceptance_completion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(acceptance, "require_due_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        acceptance.plan_due,
        "require_due_clear",
        lambda *_args, **_kwargs: None,
    )

    result = anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert result["passed"] == result["total"] == 1
    assert resolution_calls == [shot.folder]
    assert [name for name, _selected in observed] == [
        "debts",
        "moments",
        "capture",
        "axes",
        "chain",
        "checks",
        "capture",
        "reconcile",
        "repair",
    ]
    assert all(selected is entry for _name, selected in observed)


def test_failed_partial_moment_is_still_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-partial")
    _patch_acceptance(monkeypatch, shot, layout, passed=False)
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: pytest.fail("partial acceptance cannot discharge final records"),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail("partial acceptance cannot clear final due records"),
    )

    with pytest.raises(run_artifacts.TypedStop) as raised:
        anyio.run(acceptance.accept, shot, None, "M1", False, False, False)

    expected_failure = {
        "moment_id": "M1",
        "frame": 1,
        "failure_modes": ["critic"],
        "failed_contract_ids": [],
        "metric_ids": [],
        "critic_axis_ids": ["composition"],
    }
    assert raised.value.stop_envelope.cause.finding_ids == (
        "acceptance:"
        + canonical_digest(
            {
                "schema": "vfx-harness.acceptance-finding-id/v1",
                "failure": expected_failure,
            }
        ),
    )
    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert "repair_plan" not in ledger["acceptance"]
    assert "outcome" not in ledger["acceptance"]


def test_evidence_at_another_frame_cannot_satisfy_selected_moment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-wrong-frame")
    results = _failed_results(shot, layout)
    results["M1"]["frame"] = 2
    results["M1"]["render_capture"]["frame"] = 2
    _refresh_receipt_digest(results["M1"])

    with pytest.raises(ValueError, match="frame"):
        acceptance_stop.compile_acceptance_outcome(shot, _authority(), results)


def test_different_reference_bytes_cannot_satisfy_selected_moment_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    (tmp_path / "refs" / "different.png").write_bytes(b"different reference")
    layout = run_artifacts.create(tmp_path, "acceptance-wrong-reference")
    results = _failed_results(
        shot,
        layout,
        reference="refs/different.png",
    )

    with pytest.raises(ValueError, match="reference"):
        acceptance_stop.compile_acceptance_outcome(shot, _authority(), results)


def test_passing_moment_cannot_contain_failed_authoritative_contract_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-contradictory-contract")
    results = _failed_results(shot, layout)
    result = results["M1"]
    result.update(
        {
            "mean": 4.5,
            "pass": True,
            "critic_pass": True,
            "decided_by": "critic",
            "metric_failures": [],
            "metric_readings": [],
            "scores": {"composition": 5.0},
            "issues": [],
            "contract_evidence": [_contract_reading(authoritative=True)],
        }
    )

    with pytest.raises(ValueError, match="authoritative contract"):
        acceptance_stop.compile_acceptance_outcome(shot, _authority(), results)


def test_failed_authoritative_contract_turns_critic_pass_into_typed_failed_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-authoritative-contract-failed")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    monkeypatch.setattr(
        acceptance,
        "acceptance_evidence",
        lambda *_args, **_kwargs: [_contract_reading(authoritative=True)],
    )
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail(
            "a failed authoritative contract cannot clear final acceptance debt"
        ),
    )

    with pytest.raises(run_artifacts.TypedStop) as raised:
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    envelope = raised.value.stop_envelope
    assert envelope.stage == "acceptance"
    assert envelope.stop_class == "human_decision_required"
    evidence = json.loads(
        (layout.reports / "acceptance-stop-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["moments"][0]["pass"] is False
    assert evidence["moments"][0]["contract_evidence"][0]["id"] == "framing-contract"
    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert ledger["acceptance"]["passed"] == 0
    assert ledger["acceptance"]["outcome"]["passed"] is False


def test_failed_non_authoritative_contract_does_not_flip_critic_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-diagnostic-contract-failed")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    monkeypatch.setattr(
        acceptance,
        "acceptance_evidence",
        lambda *_args, **_kwargs: [_contract_reading(authoritative=False)],
    )
    calls: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "resolve_acceptance_completion",
        lambda *_args, **_kwargs: calls.append("resolve"),
    )
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: calls.append("clear"),
    )

    result = anyio.run(acceptance.accept, shot, None, None, False, False, False)

    assert result["passed"] == result["total"] == 1
    assert result["moments"]["M1"]["pass"] is True
    assert result["moments"]["M1"]["contract_evidence"][0]["pass"] is False
    assert result["outcome"]["passed"] is True
    assert calls == ["resolve", "clear"]


def test_contract_binding_must_pass_every_acceptance_moment_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    (tmp_path / "refs" / "M2.png").write_bytes(b"second reference")
    layout = run_artifacts.create(tmp_path, "acceptance-contract-all-moments")
    _patch_acceptance(monkeypatch, shot, layout, passed=True)
    moments = {
        "M1": Milestone("M1", 1, "refs/M1.png", "first finished frame"),
        "M2": Milestone("M2", 2, "refs/M2.png", "second finished frame"),
    }
    base = _authority()
    authority = acceptance_stop.AcceptanceAuthoritySnapshot(
        bundle_digest=base.bundle_digest,
        view_digest=base.view_digest,
        judgment_debt_state_digest=base.judgment_debt_state_digest,
        acceptance_artifact_sha256=base.acceptance_artifact_sha256,
        layers_artifact_sha256=base.layers_artifact_sha256,
        selected_moments=(
            base.selected_moments[0],
            {
                "id": "M2",
                "frame": 2,
                "ref": "refs/M2.png",
                "ref_sha256": _digest("second reference"),
                "fingerprint": "",
            },
        ),
        chain=base.chain,
    )
    monkeypatch.setattr(
        acceptance,
        "load_milestones",
        lambda _shot, _selected=None: moments,
    )
    monkeypatch.setattr(
        acceptance.acceptance_stop,
        "capture_acceptance_authority",
        lambda _shot, _moments, _selected=None: authority,
    )

    def stash(_session, _shot, milestone: Milestone, _suffix: str):
        render = layout.evidence / "renders" / f"{milestone.id}_accept.png"
        render.write_bytes(f"candidate frame {milestone.frame}".encode())
        return (
            render.relative_to(shot.folder).as_posix(),
            _render_receipt(render, frame=milestone.frame),
        )

    monkeypatch.setattr(acceptance, "_stash_render_with_receipt", stash)

    def contract_evidence(
        _folder,
        *,
        frame: int,
        ref: str,
        render: str,
        selected_authority: object,
    ):
        del render
        assert selected_authority is not None
        assert ref == f"refs/M{frame}.png"
        passed = frame == 1
        return [
            {
                **_contract_reading(authoritative=True, passed=passed),
                "value": 0.6 if passed else 0.2,
            }
        ]

    monkeypatch.setattr(acceptance, "acceptance_evidence", contract_evidence)
    resolution_inputs: list[frozenset[tuple[str, str]]] = []

    def resolve(
        _folder,
        *,
        passed_evidence,
        expected_bundle_digest,
        selected_authority,
    ):
        assert expected_bundle_digest == authority.bundle_digest
        assert selected_authority is not None
        resolution_inputs.append(frozenset(passed_evidence))
        return ()

    monkeypatch.setattr(acceptance, "resolve_acceptance_completion", resolve)
    monkeypatch.setattr(
        acceptance,
        "require_due_clear",
        lambda *_args, **_kwargs: pytest.fail(
            "a cross-moment contract failure cannot satisfy acceptance debt"
        ),
    )

    with pytest.raises(run_artifacts.TypedStop):
        anyio.run(acceptance.accept, shot, None, None, False, False, False)

    binding = ("image_contract", "framing-contract")
    assert resolution_inputs == [frozenset()]
    assert binding not in resolution_inputs[0]
    ledger = json.loads((tmp_path / "shot.json").read_text(encoding="utf-8"))
    assert ledger["acceptance"]["moments"]["M1"]["contract_evidence"][0]["pass"] is True
    assert ledger["acceptance"]["moments"]["M2"]["contract_evidence"][0]["pass"] is False


def test_audit_only_changes_do_not_change_outcome_or_stop_action_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    authority = _authority()
    (tmp_path / "refs" / "M1-relocated.png").write_bytes(b"reference")

    first_layout = run_artifacts.create(tmp_path, "acceptance-attempt-one")
    first_results = _failed_results(
        shot,
        first_layout,
        pixels=b"same pixels",
        audit_label="first",
    )
    first = acceptance_stop.compile_acceptance_stop(
        shot,
        first_layout,
        authority,
        first_results,
        [("composition", "composition match")],
    )
    first_outcome = acceptance_stop.compile_acceptance_outcome(shot, authority, first_results)

    second_layout = run_artifacts.create(tmp_path, "acceptance-attempt-republished")
    republished_results = _failed_results(
        shot,
        second_layout,
        pixels=b"same pixels",
        audit_label="republished",
        reference="refs/M1-relocated.png",
    )
    republished_results["M1"]["render_capture"]["render_state"][
        "workbench_shading"
    ] = "MATERIAL"
    _refresh_receipt_digest(republished_results["M1"])
    republished = acceptance_stop.compile_acceptance_stop(
        shot,
        second_layout,
        authority,
        republished_results,
        [("composition", "composition match")],
    )
    republished_outcome = acceptance_stop.compile_acceptance_outcome(
        shot,
        authority,
        republished_results,
    )

    assert first_outcome == republished_outcome
    assert first.cause_fingerprint == republished.cause_fingerprint
    assert first.authoritative_before_digest == republished.authoritative_before_digest
    assert first.attempt_evidence_digest == republished.attempt_evidence_digest
    assert first.actions[0].digest == republished.actions[0].digest
    assert (first_layout.reports / "acceptance-stop-evidence.json").read_bytes() == (
        second_layout.reports / "acceptance-stop-evidence.json"
    ).read_bytes()
    assert (first_layout.reports / "acceptance-stop-audit.json").read_bytes() != (
        second_layout.reports / "acceptance-stop-audit.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "semantic_field",
    [
        "render_scale",
        "render_engine",
        "mean",
        "score",
        "metric_value",
        "contract_value",
    ],
)
def test_each_isolated_semantic_change_changes_outcome_and_stop_action_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    semantic_field: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    authority = _authority()
    baseline_layout = run_artifacts.create(tmp_path, "acceptance-semantic-baseline")
    baseline_results = _failed_results(shot, baseline_layout, pixels=b"same pixels")
    baseline_outcome = acceptance_stop.compile_acceptance_outcome(
        shot,
        authority,
        baseline_results,
    )
    baseline_stop = acceptance_stop.compile_acceptance_stop(
        shot,
        baseline_layout,
        authority,
        baseline_results,
        [("composition", "composition match")],
    )

    changed_layout = run_artifacts.create(tmp_path, f"acceptance-semantic-{semantic_field}")
    changed_results = _failed_results(shot, changed_layout, pixels=b"same pixels")
    _change_semantic_field(changed_results["M1"], semantic_field)
    changed_outcome = acceptance_stop.compile_acceptance_outcome(
        shot,
        authority,
        changed_results,
    )
    changed_stop = acceptance_stop.compile_acceptance_stop(
        shot,
        changed_layout,
        authority,
        changed_results,
        [("composition", "composition match")],
    )

    assert baseline_outcome.moments[0].evidence_digest != (
        changed_outcome.moments[0].evidence_digest
    )
    assert baseline_stop.attempt_evidence_digest != changed_stop.attempt_evidence_digest
    assert baseline_stop.actions[0].digest != changed_stop.actions[0].digest


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("wrong_schema", r"\.schema must be .*acceptance-moment-evidence/v1"),
        ("extra_moment_field", "fields mismatch"),
        ("string_frame", r"\.frame must be an integer"),
        ("wrong_render_mode", r"render_capture\.mode must be 'eevee'"),
        ("empty_render_state", r"render_state fields mismatch"),
        ("extra_render_state_field", r"render_state fields mismatch"),
        ("integer_score_axis", "scores key must be a non-empty trimmed string"),
        ("extra_contract_field", r"contract_evidence\[0\] fields mismatch"),
        ("string_contract_value", r"contract_evidence\[0\]\.value must be a finite scalar"),
        ("duplicate_contract_id", "duplicate contract ids"),
        ("duplicate_metric_id", "duplicate metric ids"),
        ("string_metric_value", r"metric_readings\[0\]\.value must be a finite scalar"),
        ("contradictory_pass", r"\.pass contradicts"),
        ("contradictory_decision_source", r"\.decided_by contradicts"),
    ],
)
def test_malformed_acceptance_moment_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: str,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, f"acceptance-malformed-{case}")
    results = _failed_results(shot, layout)
    _malform_moment(results["M1"], case)

    with pytest.raises(ValueError, match=error):
        acceptance_stop.compile_acceptance_stop(
            shot,
            layout,
            _authority(),
            results,
            [("composition", "composition match")],
        )


def test_blocking_metric_prose_cannot_replace_typed_acceptance_readings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    shot = _shot(tmp_path)
    layout = run_artifacts.create(tmp_path, "acceptance-untyped-metric")
    render = layout.evidence / "renders" / "M1_accept.png"
    render.write_bytes(b"failed pixels")
    results = {
        "M1": {
            "schema": stop_evidence.MOMENT_SCHEMA,
            "frame": 1,
            "ref": "refs/M1.png",
            "render": render.relative_to(tmp_path).as_posix(),
            "render_capture": _render_receipt(render),
            "mean": 0.0,
            "pass": False,
            "critic_pass": False,
            "decided_by": "metrics",
            "metric_failures": ["exposure_mean failed near /tmp/run-a/render.png"],
            "metric_readings": [],
            "scores": {},
            "issues": [],
            "contract_evidence": [],
        }
    }

    with pytest.raises(ValueError, match="without typed metric readings"):
        acceptance_stop.compile_acceptance_stop(
            shot,
            layout,
            _authority(),
            results,
            [("composition", "composition match")],
        )


def test_chain_refuses_ledger_pass_without_terminal_layer_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    script = tmp_path / "build" / "units" / "1" / "form.py"
    script.parent.mkdir(parents=True)
    script.write_text("pass\n", encoding="utf-8")
    unit = SimpleNamespace(id="form")
    layer = SimpleNamespace(
        id="1",
        script="build/units/1/form.py",
        stages=(unit,),
        as_milestone=lambda: object(),
    )
    monkeypatch.setattr(
        acceptance,
        "selected_layer_chain",
        lambda *_args, **_kwargs: (layer,),
    )
    selected = SimpleNamespace()
    monkeypatch.setattr(
        acceptance,
        "resolve_selected_authority",
        lambda _folder: selected,
    )
    monkeypatch.setattr(
        acceptance,
        "Ledger",
        lambda *_args, **_kwargs: SimpleNamespace(status=lambda _milestone: "passed"),
    )
    monkeypatch.setattr(
        acceptance.layer_publication,
        "require_current_layer_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            acceptance.layer_publication.LayerPublicationConflict(
                "layer 1 has no current terminal finalization receipt"
            )
        ),
    )
    session = SimpleNamespace(run=lambda *_args: pytest.fail("must stop before replay"))

    with pytest.raises(
        acceptance.IncompleteChain,
        match="no current terminal finalization receipt",
    ):
        acceptance._chain(session, shot)
