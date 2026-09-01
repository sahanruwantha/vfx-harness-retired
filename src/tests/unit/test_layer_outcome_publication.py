from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.layer_outcome_fixtures import make_layer_finalization_receipt
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN
from vfx_harness.agents.builder import layer_outcome
from vfx_harness.agents.builder.layer_finalization_guard import (
    LayerFinalizationAuthorityLost,
    LayerFinalizationReceiptGuard,
)
from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.domain.authority_state_records import AuthorityStateRecordRef
from vfx_harness.domain.layer_finalizations import (
    LayerFinalizationPredecessorInput,
    canonical_layer_evaluation_receipt_bytes,
    canonical_layer_replay_receipt_bytes,
)
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.orchestration import (
    layer_outcome_publication,
    layer_outcome_source_verification,
    revalidation,
)
from vfx_harness.orchestration.layer_finalization_authorizations import (
    AuthorizedLayerFinalizationMutation,
)
from vfx_harness.orchestration.layer_outcome_paths import layer_outcome_path
from vfx_harness.orchestration.layer_outcome_publication import (
    LayerOutcomePublicationAuthority,
    LayerOutcomePublicationConflict,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)


def _layer() -> SimpleNamespace:
    return SimpleNamespace(
        id="1",
        title="Fixture layer",
        script="build/layer_1.py",
    )


def _receipt(tmp_path, *, predecessor_inputs=(), runtime_checks_text=None):
    return make_layer_finalization_receipt(
        tmp_path,
        _layer(),
        status="passed",
        canonical=[],
        run_id="fixture-outcome",
        attempt=1,
        predecessor_inputs=predecessor_inputs,
        runtime_checks_text=runtime_checks_text,
    )


def _finalization_authority(tmp_path) -> LayerOutcomePublicationAuthority:
    receipt = _receipt(tmp_path)
    completion_authorization = AuthorizedUnitCompletionSet(
        layer_id=receipt.claim.layer_id,
        selection_token=parse_authority_selection_token(
            ABSENT_SELECTION_TOKEN.to_dict()
        ),
        authority_state_head_ref=AuthorityStateRecordRef.mint(
            locator=f"state/authority-state/objects/{'b' * 64}.json",
            sha256="c" * 64,
            record_schema="vfx-harness.fixture-head/v1",
            record_digest="d" * 64,
        ),
        layer_generation_digest=receipt.claim.plan_hash,
        completion_projection_digest="e" * 64,
        receipts=tuple(
            sorted(
                (row.unit_id, row.completion_receipt_digest)
                for row in receipt.claim.unit_inputs
            )
        ),
    )
    finalization_authorization = AuthorizedLayerFinalizationMutation(
        receipt=receipt,
        completion_authorization=completion_authorization,
        lineage_authorization=None,
    )
    return LayerOutcomePublicationAuthority(
        kind="finalization",
        layer_id="1",
        layer_digest="a" * 64,
        selection_token=ABSENT_SELECTION_TOKEN,
        receipt=receipt,
        finalization_authorization=finalization_authorization,
    )


def _sealed_source_fixture(tmp_path, monkeypatch):
    current_script = tmp_path / "build/layer_1.py"
    current_script.parent.mkdir(parents=True)
    current_script.write_bytes(b"current layer script")
    prior_script = tmp_path / "build/prior.py"
    prior_script.write_bytes(b"prior layer script")
    predecessor = LayerFinalizationPredecessorInput.mint(
        layer_id="prior",
        finalization_receipt_digest="a" * 64,
        script_path="build/prior.py",
        script_sha256=hashlib.sha256(prior_script.read_bytes()).hexdigest(),
    )
    runtime_rows = [
        {"layer": "prior", "result": "passed"},
        {"layer": "1", "result": "passed"},
        {"layer": "successor", "result": "not-yet-due"},
    ]
    runtime_text = json.dumps(runtime_rows)
    runtime_path = tmp_path / "runtime_checks.json"
    runtime_path.write_text(runtime_text, encoding="utf-8")
    receipt = _receipt(
        tmp_path,
        predecessor_inputs=(predecessor,),
        runtime_checks_text=runtime_text,
    )
    replay_binding = receipt.evaluation_receipt.replay_receipts[0]
    replay = replay_binding.receipt
    replay_payload = canonical_layer_replay_receipt_bytes(replay)
    assert hashlib.sha256(replay_payload).hexdigest() == replay_binding.sha256
    replay_path = tmp_path / replay_binding.locator
    replay_path.parent.mkdir(parents=True)
    replay_path.write_bytes(replay_payload)
    evaluation_payload = canonical_layer_evaluation_receipt_bytes(
        receipt.evaluation_receipt
    )
    assert (
        hashlib.sha256(evaluation_payload).hexdigest()
        == receipt.evaluation_receipt_sha256
    )
    evaluation_path = tmp_path / receipt.evaluation_receipt_locator
    evaluation_path.write_bytes(evaluation_payload)

    manifest_path = tmp_path / "manifest/input.txt"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"manifest source")
    layers_path = tmp_path / "plans/selected/layers.json"
    layers_path.parent.mkdir(parents=True)
    layers_path.write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {"id": "prior", "depends_on": []},
                    {"id": "1", "depends_on": ["prior"]},
                    {"id": "successor", "depends_on": ["1"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    prior_path = layer_outcome_path(tmp_path, "prior")
    prior_path.parent.mkdir(parents=True, exist_ok=True)
    prior_path.write_bytes(b"prior outcome")
    reference_path = tmp_path / "refs/source.png"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_bytes(b"reference source")
    render_path = tmp_path / "runs/source/render.png"
    render_path.parent.mkdir(parents=True)
    render_path.write_bytes(b"render source")

    package = tmp_path / "package"
    fake_module = package / "orchestration/revalidation.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# verifier fixture\n", encoding="utf-8")
    harness_path = package / "worker.py"
    harness_path.write_bytes(b"harness source")
    monkeypatch.setattr(revalidation, "__file__", str(fake_module))
    monkeypatch.setattr(
        revalidation,
        "harness_identity_paths",
        lambda _package: (fake_module, harness_path),
    )

    manifest = {
        "harness_version": revalidation.HARNESS_VERSION,
        "blender_version": "fixture",
        "comparison": {
            "mode": revalidation.DEFAULT_COMPARISON_MODE,
            "scale": revalidation.DEFAULT_COMPARISON_SCALE,
        },
        "models": revalidation.current_model_identity(),
        "files": {
            "manifest/input.txt": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "plans/selected/layers.json": hashlib.sha256(
                layers_path.read_bytes()
            ).hexdigest(),
            "build/prior.py": hashlib.sha256(prior_script.read_bytes()).hexdigest(),
            "build/layer_1.py": hashlib.sha256(
                current_script.read_bytes()
            ).hexdigest(),
        },
        "harness_files": {
            "orchestration/revalidation.py": hashlib.sha256(
                fake_module.read_bytes()
            ).hexdigest(),
            "worker.py": hashlib.sha256(harness_path.read_bytes()).hexdigest(),
        },
        "runtime_checks": revalidation.runtime_checks_digest_from_text(
            runtime_text,
            frozenset({"prior", "1"}),
            frozenset({"prior", "1", "successor"}),
        ),
        "prior_outcomes": {
            "prior": hashlib.sha256(prior_path.read_bytes()).hexdigest(),
        },
    }
    render_sha256 = hashlib.sha256(render_path.read_bytes()).hexdigest()
    capture = {
        "schema": revalidation.RENDER_CAPTURE_SCHEMA,
        "frame": 1,
        "mode": "eevee",
        "scale": 0.5,
        "resolution": [64, 36, 100],
        "render_state": {"fixture": True},
        "warnings": [],
        "png_sha256": render_sha256,
    }
    capture["capture_digest"] = canonical_digest(capture)
    outcome = {
        "revalidation_manifest": manifest,
        "canonical": [
            {
                "evidence_kind": "render",
                "frame": 1,
                "ref": "refs/source.png",
                "ref_sha256": hashlib.sha256(
                    reference_path.read_bytes()
                ).hexdigest(),
                "input_manifest_sha256": canonical_digest(manifest),
                "authoritative": [],
                "authoritative_sha256": canonical_digest({"authoritative": []}),
                "qualitative_defects": [],
                "render": "runs/source/render.png",
                "render_sha256": render_sha256,
                "render_capture": capture,
            }
        ],
        "best": {"round": 1, "mean": 5.0, "render": "runs/source/render.png"},
    }
    return outcome, receipt, {
        "manifest": manifest_path,
        "layers": layers_path,
        "current_script": current_script,
        "prior_script": prior_script,
        "harness": harness_path,
        "harness_module": fake_module,
        "prior": prior_path,
        "reference": reference_path,
        "render": render_path,
        "replay": replay_path,
        "evaluation": evaluation_path,
        "runtime": runtime_path,
    }


def test_selection_neutral_outcome_source_verifier_closes_every_source_family(
    tmp_path,
    monkeypatch,
) -> None:
    outcome, receipt, sources = _sealed_source_fixture(tmp_path, monkeypatch)

    verified = layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
        tmp_path,
        outcome,
        finalization_receipt=receipt,
    )

    assert set(sources.values()) <= set(verified)
    assert sources["runtime"] in verified


@pytest.mark.parametrize(
    "source",
    [
        "runtime",
        "manifest",
        "harness",
        "prior",
        "reference",
        "render",
        "replay",
        "evaluation",
    ],
)
def test_selection_neutral_outcome_source_verifier_refuses_preexisting_drift(
    tmp_path,
    monkeypatch,
    source: str,
) -> None:
    outcome, receipt, sources = _sealed_source_fixture(tmp_path, monkeypatch)
    path = sources[source]
    path.write_bytes(b"changed before source verification")

    with pytest.raises(
        ValueError,
        match=r"bytes changed or are missing|file digest changed",
    ):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            outcome,
            finalization_receipt=receipt,
        )


def test_selection_neutral_outcome_source_verifier_refuses_stale_render_capture(
    tmp_path,
    monkeypatch,
) -> None:
    outcome, receipt, _sources = _sealed_source_fixture(tmp_path, monkeypatch)
    stale = copy.deepcopy(outcome)
    stale["canonical"][0]["render_capture"]["warnings"] = ["changed"]

    with pytest.raises(ValueError, match="render capture is stale"):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            stale,
            finalization_receipt=receipt,
        )


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        ("harness_version", "harness version is stale"),
        ("models", "model settings are stale"),
        ("comparison", "comparison settings are stale"),
        ("blender_version", "Blender version does not match"),
        ("runtime_checks", "runtime_checks projection does not match"),
    ],
)
def test_selection_neutral_outcome_source_verifier_refuses_manifest_value_drift(
    tmp_path,
    monkeypatch,
    field: str,
    expected_message: str,
) -> None:
    outcome, receipt, _sources = _sealed_source_fixture(tmp_path, monkeypatch)
    stale = copy.deepcopy(outcome)
    if field == "harness_version":
        stale["revalidation_manifest"][field] = "stale-version"
    elif field == "models":
        stale["revalidation_manifest"][field]["builder"] = "stale-model"
    elif field == "comparison":
        stale["revalidation_manifest"][field]["scale"] = 0.75
    elif field == "blender_version":
        stale["revalidation_manifest"][field] = "stale-blender"
    else:
        stale["revalidation_manifest"][field] = "f" * 64

    with pytest.raises(ValueError, match=expected_message):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            stale,
            finalization_receipt=receipt,
        )


def test_selection_neutral_outcome_source_verifier_refuses_live_model_drift(
    tmp_path,
    monkeypatch,
) -> None:
    outcome, receipt, _sources = _sealed_source_fixture(tmp_path, monkeypatch)
    live_models = dict(outcome["revalidation_manifest"]["models"])
    live_models["builder"] = "changed-live-model"
    monkeypatch.setattr(revalidation, "current_model_identity", lambda: live_models)

    with pytest.raises(ValueError, match="model settings are stale"):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            outcome,
            finalization_receipt=receipt,
        )


@pytest.mark.parametrize("locator", ["/tmp/outside.json", "plans/../outside.json"])
def test_selection_neutral_outcome_source_verifier_refuses_unsafe_manifest_locator(
    tmp_path,
    monkeypatch,
    locator: str,
) -> None:
    outcome, receipt, _sources = _sealed_source_fixture(tmp_path, monkeypatch)
    files = outcome["revalidation_manifest"]["files"]
    files[locator] = files.pop("manifest/input.txt")

    with pytest.raises(ValueError, match="locator is not canonical"):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            outcome,
            finalization_receipt=receipt,
        )


@pytest.mark.parametrize("substitution", ["leaf", "parent"])
def test_selection_neutral_outcome_source_verifier_refuses_symlink_substitution(
    tmp_path,
    monkeypatch,
    substitution: str,
) -> None:
    outcome, receipt, sources = _sealed_source_fixture(tmp_path, monkeypatch)
    reference = sources["reference"]
    if substitution == "leaf":
        target = tmp_path / "outside-reference.png"
        target.write_bytes(reference.read_bytes())
        reference.unlink()
        reference.symlink_to(target)
    else:
        reference_parent = reference.parent
        replacement_parent = tmp_path / "real-refs"
        reference_parent.rename(replacement_parent)
        reference_parent.symlink_to(replacement_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="trusted regular file"):
        layer_outcome_source_verification.verify_sealed_layer_outcome_sources(
            tmp_path,
            outcome,
            finalization_receipt=receipt,
        )


def test_layer_outcome_commit_is_metadata_only(tmp_path, monkeypatch) -> None:
    source = tmp_path / "accepted.py"
    source.write_text("# accepted\n", encoding="utf-8")
    destination = tmp_path / "plans" / "outcomes" / "layer-1.json"
    authority = _finalization_authority(tmp_path)
    prepared = layer_outcome_publication.prepare_layer_outcome_publication(
        tmp_path,
        destination,
        b'{"schema":"fixture"}\n',
        authority=authority,
        sources=layer_outcome_publication.capture_layer_outcome_source_identities(
            (source,)
        ),
    )
    real_fsync = os.fsync
    fsync_modes: list[int] = []

    def directory_fsync_only(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        fsync_modes.append(mode)
        assert stat.S_ISDIR(mode), "guarded commit fsynced a regular file"
        real_fsync(descriptor)

    def no_hashing(*_args, **_kwargs):
        raise AssertionError("guarded commit hashed bytes")

    monkeypatch.setattr(layer_outcome_publication.os, "fsync", directory_fsync_only)
    monkeypatch.setattr(layer_outcome_publication.hashlib, "sha256", no_hashing)

    assert (
        layer_outcome_publication.commit_layer_outcome_publication(
            prepared,
            authority=authority,
        )
        == destination
    )
    assert destination.read_bytes() == b'{"schema":"fixture"}\n'
    assert fsync_modes and all(stat.S_ISDIR(mode) for mode in fsync_modes)


def test_layer_outcome_commit_refuses_changed_causal_source(tmp_path) -> None:
    source = tmp_path / "canonical.py"
    source.write_text("# accepted A\n", encoding="utf-8")
    destination = tmp_path / "plans" / "outcomes" / "layer-1.json"
    authority = _finalization_authority(tmp_path)
    prepared = layer_outcome_publication.prepare_layer_outcome_publication(
        tmp_path,
        destination,
        b'{"schema":"fixture"}\n',
        authority=authority,
        sources=layer_outcome_publication.capture_layer_outcome_source_identities(
            (source,)
        ),
    )
    source.write_text("# accepted B\n", encoding="utf-8")

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="causal inputs changed",
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            prepared,
            authority=authority,
        )

    assert not destination.exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)
    assert not prepared.temporary.exists()


def test_layer_outcome_commit_refuses_same_path_prepared_inode_substitution(
    tmp_path,
) -> None:
    destination = tmp_path / "plans" / "outcomes" / "layer-1.json"
    authority = _finalization_authority(tmp_path)
    prepared = layer_outcome_publication.prepare_layer_outcome_publication(
        tmp_path,
        destination,
        b'{"schema":"accepted"}\n',
        authority=authority,
        sources=(),
    )
    substitute = prepared.temporary.with_name("substitute.json")
    substitute.write_bytes(b'{"schema":"substitute"}\n')
    os.replace(substitute, prepared.temporary)

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="prepared layer-outcome bytes changed",
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            prepared,
            authority=authority,
        )

    layer_outcome_publication.discard_layer_outcome_publication(prepared)
    assert prepared.temporary.read_bytes() == b'{"schema":"substitute"}\n'
    assert not destination.exists()


def test_layer_outcome_commit_refuses_post_rename_substitution(
    tmp_path,
    monkeypatch,
) -> None:
    destination = tmp_path / "plans" / "outcomes" / "layer-1.json"
    authority = _finalization_authority(tmp_path)
    prepared = layer_outcome_publication.prepare_layer_outcome_publication(
        tmp_path,
        destination,
        b'{"schema":"accepted"}\n',
        authority=authority,
        sources=(),
    )
    attacker = destination.parent / "attacker.json"
    attacker.write_bytes(b'{"schema":"attacker"}\n')
    real_replace = layer_outcome_publication.os.replace
    injected = False

    def substitute_after_replace(source, target, *args, **kwargs) -> None:
        nonlocal injected
        real_replace(source, target, *args, **kwargs)
        if not injected and target == destination.name:
            injected = True
            real_replace(
                attacker.name,
                target,
                src_dir_fd=kwargs["dst_dir_fd"],
                dst_dir_fd=kwargs["dst_dir_fd"],
            )

    monkeypatch.setattr(
        layer_outcome_publication.os,
        "replace",
        substitute_after_replace,
    )

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="published layer-outcome inode changed",
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            prepared,
            authority=authority,
        )
    layer_outcome_publication.discard_layer_outcome_publication(prepared)

    assert destination.read_bytes() == b'{"schema":"attacker"}\n'
    assert list(destination.parent.glob(".layer-1.json.prepared.*")) == []


def test_outcome_reconciliation_refuses_conflicting_exact_receipt_bytes(
    tmp_path,
) -> None:
    receipt = _receipt(tmp_path)
    destination = tmp_path / "plans/outcomes/layer-31.json"
    destination.parent.mkdir(parents=True)
    payload = (
        json.dumps(
            {
                "finalization_receipt": {
                    "receipt_digest": receipt.receipt_digest,
                },
                "projection": "conflicting",
            }
        )
        + "\n"
    ).encode()
    destination.write_bytes(payload)

    with pytest.raises(ValueError, match="conflicts with the exact current"):
        layer_outcome._refuse_conflicting_current_projection(
            destination,
            receipt_digest=receipt.receipt_digest,
            expected_sha256="f" * 64,
        )

    layer_outcome._refuse_conflicting_current_projection(
        destination,
        receipt_digest=receipt.receipt_digest,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_finalized_outcome_preparation_is_unlocked_and_stale_commit_refuses(
    tmp_path,
    monkeypatch,
) -> None:
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    receipt = _receipt(tmp_path)
    guard = LayerFinalizationReceiptGuard(
        tmp_path,
        receipt,
        (),
        selected,
    )
    layer = _layer()
    prepare_started = Event()
    release_prepare = Event()
    revoked = Event()
    discarded = Event()
    committed = Event()
    failures: list[BaseException] = []
    prepared = SimpleNamespace(
        destination=tmp_path / "plans/outcomes/layer-31.json",
        payload_sha256="a" * 64,
    )
    authority = object()

    def blocked_prepare(*_args, **_kwargs):
        prepare_started.set()
        assert release_prepare.wait(5)
        return prepared

    def commit(*_args, **_kwargs):
        committed.set()
        raise AssertionError("stale prepared outcome was committed")

    monkeypatch.setattr(layer_outcome, "prepare_layer_outcome", blocked_prepare)
    monkeypatch.setattr(layer_outcome, "commit_layer_outcome", commit)
    monkeypatch.setattr(
        layer_outcome,
        "authorize_terminal_layer_finalization_mutation",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        layer_outcome,
        "finalization_layer_outcome_authority",
        lambda *_args, **_kwargs: authority,
    )
    monkeypatch.setattr(
        layer_outcome,
        "discard_layer_outcome",
        lambda value: discarded.set() if value is prepared else None,
    )
    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "check",
        lambda _self, _operation: receipt,
    )

    def publish(_self, _operation, _mutation):
        if revoked.is_set():
            raise LayerFinalizationAuthorityLost("fixture finalization was revoked")
        raise AssertionError("fixture did not revoke the finalization")

    monkeypatch.setattr(LayerFinalizationReceiptGuard, "publish", publish)

    def publish_outcome() -> None:
        try:
            layer_outcome.publish_finalized_layer_outcome(
                tmp_path,
                layer,
                best={},
                canonical=[],
                blender_version="fixture",
                selected_authority=selected,
                finalization_guard=guard,
            )
        except BaseException as exc:  # asserted below
            failures.append(exc)

    outcome_thread = Thread(target=publish_outcome)
    outcome_thread.start()
    assert prepare_started.wait(2)
    revoked.set()
    release_prepare.set()
    outcome_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], LayerFinalizationAuthorityLost)
    assert discarded.is_set()
    assert not committed.is_set()
