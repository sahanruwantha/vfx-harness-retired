"""The accepted-build index is derived from real authority on the public pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from tests.integration.test_authority_receipt_lineage import (
    _commit_document_successor,
    _selected_documents,
)
from tests.integration.test_judgment_debt_public_pipeline import (
    _build_prepassed_layer,
    _fixture_executable_evidence,
    _pass_layer_unit,
    _payload_for_bundle,
    _public_fixture_root,
    _publish_payload,
    _ReplaySession,
    _stash_fixture_capture,
)
from tests.unit.test_judgment_debt_materialization import _camera_payload, _form_payload
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder import prior as prior_runtime
from vfx_harness.agents.builder import verify as verify_runtime
from vfx_harness.domain.acceptance_outcomes import (
    AcceptanceMomentOutcome,
    AcceptanceOutcome,
)
from vfx_harness.domain.authority_head_records import AuthoritySelectionTokenProjection
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import accepted_chain
from vfx_harness.orchestration import shot_ledger_v2_derivation as derivation
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.authority_state_store import read_current_bytes
from vfx_harness.orchestration.plan_authority import publish_current
from vfx_harness.orchestration.shot_authority_capture import (
    shot_authority_writer_fence,
)


def _arm_public_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_artifacts.ENV, raising=False)

    async def axes(*_args):
        return [("camera_alignment", "camera framing"), ("form", "rendered form")]

    async def deterministic_judge(_shot, _milestone, _render, axes, *_args, active_unit=None, **kwargs):
        debt_ids = tuple(getattr(active_unit, "provisional_debt_ids", ()) or ())
        return {
            "scores": {key: 5 for key, _description in axes},
            "mean": 5.0,
            "pass": True,
            "issues": [],
            "scored_axes": [key for key, _description in axes],
            "na_axes": [],
            "observations": [],
            "observation_reconciliation": [],
            "contract_gap": False,
            "judge_conflict": False,
            "decided_by": "deterministic-fixture" if debt_ids else "unit_executable_evidence",
            "evidence": list(kwargs.get("evidence") or []),
        }

    builder = verify_runtime.builder_package()
    monkeypatch.setattr(layer_runtime, "ensure_axes", axes)
    monkeypatch.setattr(layer_runtime, "_blender_version", lambda _session: "fixture")
    monkeypatch.setattr(layer_runtime.costlog, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.costlog, "unbind", lambda: None)
    monkeypatch.setattr(layer_runtime.transcript, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.transcript, "unbind", lambda: None)
    monkeypatch.setattr(prior_runtime.generate_construction, "pin_for_script", lambda *_args: None)
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(builder, "_stash_render", lambda *_args, **_kwargs: "fixture-render.png")
    monkeypatch.setattr(builder, "_stash_render_with_receipt", _stash_fixture_capture)
    monkeypatch.setattr(builder, "_render_evidence", _fixture_executable_evidence)
    monkeypatch.setattr(verify_runtime, "_persist_contract_gaps", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_runtime, "_judge_unit_or_layer", deterministic_judge)


def _accepted_ids(root: Path) -> list[str]:
    stored = derivation.read_stored_shot_ledger_index(root)
    assert stored is not None
    return [row.layer_id for row in stored.accepted_layers]


def _verified(root: Path):
    return derivation.current_shot_ledger_index(root, resolve_selected_authority(root))


def _derive(root: Path, acceptance_record=None):
    selected = resolve_selected_authority(root)
    with shot_authority_writer_fence(root) as capability:
        return derivation.derive_shot_ledger_index(
            root,
            selected,
            writer_capability=capability,
            acceptance_record=acceptance_record,
        ).ledger


def _two_accepted_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _arm_public_pipeline(monkeypatch)
    root = _public_fixture_root(tmp_path)
    bundle = publish_current(
        root,
        run_artifacts.create(root, "shot-ledger-derivation"),
        outcome="clean_with_deferred",
    )
    _publish_payload(root, _payload_for_bundle(root, "camera-jit.json", _camera_payload(), bundle.content_hash))
    _pass_layer_unit(root, "1", "camera")
    session = _ReplaySession()
    anyio.run(_build_prepassed_layer, root, "1", session)
    assert _accepted_ids(root) == ["1"]
    assert _verified(root).accepted_layers[0].layer_id == "1"

    # A JIT republication changes the selection; the authority-state transaction
    # re-derives the member for its successor head, so readers verify it at once.
    _publish_payload(root, _payload_for_bundle(root, "form-jit.json", _form_payload(), bundle.content_hash))
    assert _accepted_ids(root) == ["1"]
    assert _verified(root).accepted_layers[0].layer_id == "1"

    _pass_layer_unit(root, "2", "hall_form")
    anyio.run(_build_prepassed_layer, root, "2", session)
    return root


def test_layer_finalization_derives_the_index_and_readers_reverify_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _two_accepted_layers(tmp_path, monkeypatch)

    stored = derivation.read_stored_shot_ledger_index(root)
    assert stored is not None
    assert [row.layer_id for row in stored.accepted_layers] == ["1", "2"]
    assert stored.acceptance is None
    assert _verified(root) == stored
    document = json.loads((root / "shot.json").read_text(encoding="utf-8"))
    assert document["accepted_build"]["index_digest"] == stored.index_digest
    for row in stored.accepted_layers:
        script = root / row.composed_script_locator
        assert accepted_chain.sha256_of(script) == row.composed_script_sha256
        outcome = root / row.sealed_outcome_locator
        assert accepted_chain.sha256_of(outcome) == row.sealed_outcome_sha256

    # The index is content-bound: a drifted composed script is refused, and the exact
    # bytes restore it.
    script = root / stored.accepted_layers[1].composed_script_locator
    original = script.read_bytes()
    script.write_bytes(original + b"\n# drift\n")
    with pytest.raises(derivation.ShotLedgerDerivationConflict):
        _verified(root)
    script.write_bytes(original)
    assert _verified(root) == stored


def test_index_refuses_a_head_that_does_not_authorize_the_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _two_accepted_layers(tmp_path, monkeypatch)
    real = derivation.authority_state_context.resolve_current_authority_state(root)
    foreign = SimpleNamespace(
        head_ref=real.head_ref,
        head=SimpleNamespace(
            selection_token=AuthoritySelectionTokenProjection(
                real.head.selection_token.plan_revision + 1,
                real.head.selection_token.plan_pointer_sha256,
                real.head.selection_token.jit_revision,
                real.head.selection_token.jit_pointer_sha256,
            )
        ),
        commit=real.commit,
    )
    monkeypatch.setattr(
        derivation.authority_state_context,
        "resolve_current_authority_state",
        lambda _root, **_kwargs: foreign,
    )
    with pytest.raises(derivation.ShotLedgerDerivationConflict, match="does not authorize"):
        _derive(root)

    image = next(iter(real.commit.installed_states))
    disagreeing = SimpleNamespace(
        head_ref=real.head_ref,
        head=real.head,
        commit=SimpleNamespace(
            installed_states=(
                SimpleNamespace(
                    layer_id=image.layer_id,
                    binding=SimpleNamespace(layer_generation_digest="f" * 64),
                ),
            )
        ),
    )
    monkeypatch.setattr(
        derivation.authority_state_context,
        "resolve_current_authority_state",
        lambda _root, **_kwargs: disagreeing,
    )
    with pytest.raises(derivation.ShotLedgerDerivationConflict, match="binds layer"):
        _derive(root)


def _passing_acceptance_record(root: Path, ledger) -> dict:
    render = root / "runs" / "acceptance-fixture" / "evidence" / "renders" / "M1_accept.png"
    render.parent.mkdir(parents=True, exist_ok=True)
    render.write_bytes(b"render bytes")
    # ``refs/`` is authored input that selected authority rehashes; bind an existing one.
    reference = sorted(path for path in (root / "refs").iterdir() if path.is_file())[0]
    capture = {
        "schema": "vfx-harness.acceptance-moment-semantic/v1",
        "moment_id": "M1",
        "pass": True,
        "decided_by": "critic",
        "render_sha256": accepted_chain.sha256_of(render),
        "reference_sha256": accepted_chain.sha256_of(reference),
    }
    record = root / "runs" / "acceptance-fixture" / "evidence" / "acceptance" / "moment-4d31.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(capture, sort_keys=True) + "\n", encoding="utf-8")
    selected = resolve_selected_authority(root)
    outcome = AcceptanceOutcome(
        authority_digest="a" * 64,
        bundle_digest=selected.plan.bundle.content_hash,
        view_digest=selected.assertion.effective_view.digest,
        chain_digest=ledger.accepted_chain_digest,
        moments=(
            AcceptanceMomentOutcome(
                moment_id="M1",
                passed=True,
                decided_by="critic",
                evidence_digest=canonical_digest(capture),
            ),
        ),
    )
    return {
        "outcome": outcome.as_dict(),
        "moments": {
            "M1": {
                "render": render.relative_to(root).as_posix(),
                "ref": reference.relative_to(root).as_posix(),
            }
        },
        "evidence_records": {"M1": record.relative_to(root).as_posix()},
    }


def test_acceptance_binds_only_a_passing_outcome_with_verifiable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _two_accepted_layers(tmp_path, monkeypatch)
    ledger = _derive(root)
    record = _passing_acceptance_record(root, ledger)

    bound = _derive(root, acceptance_record=record)
    assert bound.acceptance is not None
    assert bound.acceptance.outcome.chain_digest == ledger.accepted_chain_digest
    binding = bound.acceptance.moment_evidence[0]
    assert binding.moment_id == "M1"
    assert binding.evidence_record_locator == record["evidence_records"]["M1"]

    other_chain = copy.deepcopy(record)
    typed = AcceptanceOutcome.from_dict(record["outcome"], "outcome")
    other_chain["outcome"] = AcceptanceOutcome(
        authority_digest=typed.authority_digest,
        bundle_digest=typed.bundle_digest,
        view_digest=typed.view_digest,
        chain_digest="9" * 64,
        moments=typed.moments,
    ).as_dict()
    assert _derive(root, acceptance_record=other_chain).acceptance is None

    without_records = copy.deepcopy(record)
    without_records.pop("evidence_records")
    assert _derive(root, acceptance_record=without_records).acceptance is None

    (root / record["evidence_records"]["M1"]).write_text(
        json.dumps({"schema": "forged"}),
        encoding="utf-8",
    )
    with pytest.raises(
        derivation.ShotLedgerDerivationConflict,
        match="does not hash to the typed outcome's evidence digest",
    ):
        _derive(root, acceptance_record=record)


def _current_head_sha256(root: Path) -> str:
    payload = read_current_bytes(root)
    assert payload is not None
    return hashlib.sha256(payload).hexdigest()


def test_republication_re_derives_the_index_inside_the_authority_state_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A republication that supersedes an accepted layer shrinks the stored prefix in
    the same transaction that selects the successor head (HIR-0172)."""

    root = _two_accepted_layers(tmp_path, monkeypatch)
    before = derivation.read_stored_shot_ledger_index(root)
    assert before is not None
    assert [row.layer_id for row in before.accepted_layers] == ["1", "2"]
    assert before.authority_state_head_ref.sha256 == _current_head_sha256(root)

    documents = json.loads(
        json.dumps(_selected_documents(root)).replace("hall.mass", "hall.core")
    )
    _commit_document_successor(root, documents)

    stored = derivation.read_stored_shot_ledger_index(root)
    assert stored is not None
    assert [row.layer_id for row in stored.accepted_layers] == ["1"]
    assert stored.accepted_layers[0] == before.accepted_layers[0]
    assert stored.authority_state_head_ref.sha256 == _current_head_sha256(root)
    assert stored.authority_state_head_ref != before.authority_state_head_ref
    assert stored.selection_token != before.selection_token
    assert _verified(root) == stored
