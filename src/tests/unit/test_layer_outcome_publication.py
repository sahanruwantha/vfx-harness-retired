from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import select
import signal
import stat
import weakref
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import vfx_harness.agents.builder.layer_finalization_guard as layer_finalization_guard_module
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
from vfx_harness.observability import prepared_publication as prepared_publication_sink
from vfx_harness.observability.prepared_publication import FilePublicationConflict
from vfx_harness.orchestration import (
    layer_outcome_publication,
    layer_outcome_source_verification,
    revalidation,
    shot_authority_capture,
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
        selection_token=parse_authority_selection_token(ABSENT_SELECTION_TOKEN.to_dict()),
        authority_state_head_ref=AuthorityStateRecordRef.mint(
            locator=f"state/authority-state/objects/{'b' * 64}.json",
            sha256="c" * 64,
            record_schema="vfx-harness.fixture-head/v1",
            record_digest="d" * 64,
        ),
        layer_generation_digest=receipt.claim.plan_hash,
        completion_projection_digest="e" * 64,
        receipts=tuple(sorted((row.unit_id, row.completion_receipt_digest) for row in receipt.claim.unit_inputs)),
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


def _prepare_bound_outcome(tmp_path, monkeypatch, *, authority=None):
    """Prepare canonical bytes against a small deterministic source closure."""

    if authority is None:
        authority = _finalization_authority(tmp_path)
    first = tmp_path / "sources/first.bin"
    second = tmp_path / "sources/second.bin"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"first source")
    second.write_bytes(b"second source")
    expected_record = layer_outcome_publication.sealed_layer_outcome_record(authority.receipt)

    def verify(folder, outcome, *, finalization_receipt):
        assert Path(folder) == tmp_path
        assert outcome == expected_record
        assert finalization_receipt == authority.receipt
        return first, second

    monkeypatch.setattr(
        layer_outcome_source_verification,
        "verify_sealed_layer_outcome_sources",
        verify,
    )
    selected = SimpleNamespace(selection_token=authority.selection_token)
    guard = LayerFinalizationReceiptGuard(
        tmp_path,
        authority.receipt,
        (),
        selected,
    )

    @contextmanager
    def terminal_guard(
        folder,
        receipt,
        units,
        *,
        selection_token,
        lineage_authorization,
    ):
        assert Path(folder) == tmp_path
        assert receipt is authority.receipt
        assert tuple(units) == ()
        assert selection_token is authority.selection_token
        assert lineage_authorization is None
        yield receipt

    monkeypatch.setattr(
        layer_finalization_guard_module,
        "terminal_layer_finalization_guard",
        terminal_guard,
    )
    prepared = layer_outcome_publication.prepare_layer_outcome_publication(
        tmp_path,
        authority=authority,
        guard=guard,
    )
    return authority, prepared, (first, second), guard


def _commit_bound_outcome(tmp_path, prepared, authority, guard):
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    return guard.publish_prepared(
        "commit fixture layer outcome",
        prepared,
        lambda authorization: layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            authorization=authorization,
            verification=verification,
        ),
    )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_outcome_verification_fork_quiesces_typed_registry_lock(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, _guard = _prepare_bound_outcome(
        tmp_path,
        monkeypatch,
    )
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    retained = weakref.ref(prepared)
    del prepared
    assert retained() is not None

    lock_held = Event()

    def hold_registry_lock() -> None:
        with layer_outcome_publication._PREPARED_OUTCOME_LOCK:
            lock_held.set()
            Event().wait(0.5)

    owner = Thread(target=hold_registry_lock)
    owner.start()
    assert lock_held.wait(2)
    read_descriptor, write_descriptor = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no branch - parent bounds and asserts the child
        os.close(read_descriptor)
        os.write(write_descriptor, b"fork-returned")
        os.close(write_descriptor)
        os._exit(0)

    os.close(write_descriptor)
    ready, _writable, _errors = select.select([read_descriptor], [], [], 5)
    if not ready:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        pytest.fail("child deadlocked while typed verification was cleared after fork")
    observed = os.read(read_descriptor, 64)
    os.close(read_descriptor)
    waited, status = os.waitpid(child, 0)
    owner.join(2)

    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == b"fork-returned"
    assert not owner.is_alive()
    assert verification is not None
    current = retained()
    assert current is not None
    layer_outcome_publication.discard_layer_outcome_publication(current)


def test_layer_outcome_success_retires_typed_preparation(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(
        tmp_path,
        monkeypatch,
    )

    assert _commit_bound_outcome(tmp_path, prepared, authority, guard).is_file()
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="unregistered, expired, consumed",
    ):
        _ = prepared.payload_sha256
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="unregistered, expired, consumed",
    ):
        layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_refuses_foreign_thread_access(
    tmp_path,
    monkeypatch,
) -> None:
    _authority, prepared, _sources, _guard = _prepare_bound_outcome(
        tmp_path,
        monkeypatch,
    )
    failures: list[BaseException] = []

    def read_from_foreign_thread() -> None:
        try:
            _ = prepared.payload_sha256
        except BaseException as exc:  # asserted below
            failures.append(exc)

    worker = Thread(target=read_from_foreign_thread)
    worker.start()
    worker.join(5)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert "another process or thread" in str(failures[0])
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


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
    evaluation_payload = canonical_layer_evaluation_receipt_bytes(receipt.evaluation_receipt)
    assert hashlib.sha256(evaluation_payload).hexdigest() == receipt.evaluation_receipt_sha256
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
            "manifest/input.txt": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "plans/selected/layers.json": hashlib.sha256(layers_path.read_bytes()).hexdigest(),
            "build/prior.py": hashlib.sha256(prior_script.read_bytes()).hexdigest(),
            "build/layer_1.py": hashlib.sha256(current_script.read_bytes()).hexdigest(),
        },
        "harness_files": {
            "orchestration/revalidation.py": hashlib.sha256(fake_module.read_bytes()).hexdigest(),
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
                "ref_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
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
    return (
        outcome,
        receipt,
        {
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
        },
    )


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


def test_layer_outcome_verifies_payload_before_and_after_cas_rename(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    real_fsync = os.fsync
    fsync_modes: list[int] = []
    real_pread = os.pread
    reads: list[int] = []
    expected_size = prepared.temporary_identity.size
    commit_started = False

    def directory_fsync_only(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        if not commit_started:
            real_fsync(descriptor)
            return
        fsync_modes.append(mode)
        assert stat.S_ISDIR(mode), "guarded commit fsynced a regular file"
        real_fsync(descriptor)

    def tracked_pread(descriptor: int, length: int, offset: int) -> bytes:
        reads.append(offset)
        return real_pread(descriptor, length, offset)

    monkeypatch.setattr(layer_outcome_publication.os, "pread", tracked_pread)
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    assert reads == [0, expected_size]

    def unexpected_source_revalidation(*_args, **_kwargs):
        raise AssertionError("guarded outcome commit repeated full source verification")

    monkeypatch.setattr(layer_outcome_publication.os, "fsync", directory_fsync_only)
    monkeypatch.setattr(
        layer_outcome_source_verification,
        "verify_sealed_layer_outcome_sources",
        unexpected_source_revalidation,
    )

    def commit(authorization):
        nonlocal commit_started
        commit_started = True
        return layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            authorization=authorization,
            verification=verification,
        )

    assert (
        guard.publish_prepared(
            "commit fixture layer outcome",
            prepared,
            commit,
        )
        == destination
    )
    assert destination.read_bytes() == (layer_outcome_publication.canonical_layer_outcome_payload(authority.receipt))
    assert reads == [
        0,
        expected_size,
        0,
        expected_size,
    ]
    assert fsync_modes and all(stat.S_ISDIR(mode) for mode in fsync_modes)


def test_layer_outcome_commit_refuses_changed_causal_source(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    temporary = prepared.temporary
    sources[0].write_bytes(b"changed source")

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match=r"bytes changed or are missing|causal inputs changed",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    assert not destination.exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)
    assert not temporary.exists()


def test_layer_outcome_preparation_has_no_caller_controlled_publication_fields(
    tmp_path,
) -> None:
    authority = _finalization_authority(tmp_path)
    parameters = inspect.signature(layer_outcome_publication.prepare_layer_outcome_publication).parameters

    assert "destination" not in parameters
    assert "payload" not in parameters
    assert "sources" not in parameters
    assert parameters["guard"].default is inspect.Parameter.empty

    with pytest.raises(TypeError, match="guard"):
        layer_outcome_publication.prepare_layer_outcome_publication(
            tmp_path,
            authority=authority,
        )

    with pytest.raises(TypeError):
        layer_outcome_publication.prepare_layer_outcome_publication(
            tmp_path,
            tmp_path / "brief.md",
            b'{"schema":"fixture"}\n',
            authority=authority,
            sources=(),
        )

    assert not (tmp_path / "brief.md").exists()
    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()


@pytest.mark.parametrize(
    "relative_target",
    (
        Path("brief.md"),
        Path("state/authority-selection/selection.lock"),
    ),
    ids=("alternate-in-shot", "selection-lock"),
)
def test_layer_outcome_preparation_target_cannot_be_replaced(
    tmp_path,
    monkeypatch,
    relative_target: Path,
) -> None:
    authority, prepared, _sources, _guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    canonical = layer_outcome_path(tmp_path, authority.layer_id)
    alternate = tmp_path / relative_target
    alternate.parent.mkdir(parents=True, exist_ok=True)
    alternate.write_bytes(b"untouched\n")
    try:
        with pytest.raises(TypeError, match="dataclass instances"):
            replace(prepared, destination=alternate)
    finally:
        layer_outcome_publication.discard_layer_outcome_publication(prepared)

    assert not canonical.exists()
    assert alternate.read_bytes() == b"untouched\n"


def test_layer_outcome_authority_derived_canonical_target_commits(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    payload = layer_outcome_publication.canonical_layer_outcome_payload(authority.receipt)

    assert _commit_bound_outcome(tmp_path, prepared, authority, guard) == destination

    assert destination.read_bytes() == payload


def test_prepared_layer_outcome_retains_shot_and_refuses_cross_shot_commit(
    tmp_path,
    monkeypatch,
) -> None:
    destination = layer_outcome_path(tmp_path, "1")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'{"schema":"existing"}\n')
    authority, prepared, _sources, _guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    foreign_shot = tmp_path / "foreign-shot"
    foreign_shot.mkdir()

    assert prepared.shot == tmp_path
    with pytest.raises(AttributeError):
        _physical_capability = prepared.publication
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="requires a valid shot root path",
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            "",
            prepared,
            authority=authority,
            authorization=object(),
            verification=verification,
        )
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="belongs to another shot root",
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            foreign_shot,
            prepared,
            authority=authority,
            authorization=object(),
            verification=verification,
        )

    assert destination.read_bytes() == b'{"schema":"existing"}\n'
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_prepared_layer_outcome_cannot_be_reconstructed_with_another_shot(
    tmp_path,
    monkeypatch,
) -> None:
    _authority, prepared, _sources, _guard = _prepare_bound_outcome(tmp_path, monkeypatch)

    with pytest.raises(TypeError, match="dataclass instances"):
        replace(prepared, shot=Path("relative-shot"))
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="minted only from terminal authority",
    ):
        layer_outcome_publication.PreparedLayerOutcomePublication()

    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_verification_cannot_authorize_stale_predecessor(
    tmp_path,
    monkeypatch,
) -> None:
    destination = layer_outcome_path(tmp_path, "1")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"predecessor A")
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    destination.write_bytes(b"predecessor B")

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="target changed after preparation",
    ):
        guard.publish_prepared(
            "commit fixture layer outcome",
            prepared,
            lambda authorization: layer_outcome_publication.commit_layer_outcome_publication(
                tmp_path,
                prepared,
                authority=authority,
                authorization=authorization,
                verification=verification,
            ),
        )
    assert destination.read_bytes() == b"predecessor B"
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


@pytest.mark.parametrize("source_variant", ["omitted", "swapped"])
def test_layer_outcome_commit_refuses_forged_source_closure(
    tmp_path,
    monkeypatch,
    source_variant: str,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    forged_sources = () if source_variant == "omitted" else tuple(reversed(prepared.sources))
    forged_paths = tuple(identity.path for identity in forged_sources)
    monkeypatch.setattr(
        layer_outcome_publication,
        "_authority_source_paths",
        lambda _shot, _authority: forged_paths,
    )

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="source closure does not match terminal authority",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_commit_rejects_raw_writer_as_finalization_authority(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, _guard = _prepare_bound_outcome(
        tmp_path,
        monkeypatch,
    )
    foreign = tmp_path / "foreign-shot"
    foreign.mkdir()
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )

    with (
        shot_authority_capture.shot_authority_writer_fence(foreign) as capability,
        pytest.raises(
            LayerOutcomePublicationConflict,
            match="exact typed finalization authorization",
        ),
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            authorization=capability,
            verification=verification,
        )

    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    with (
        shot_authority_capture.shot_authority_writer_fence(tmp_path) as capability,
        pytest.raises(
            LayerOutcomePublicationConflict,
            match="exact typed finalization authorization",
        ),
    ):
        layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            authorization=capability,
            verification=verification,
        )

    with pytest.raises(TypeError, match="authorization"):
        layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            verification=verification,
        )

    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_revalidates_exact_finalization_authority_before_rename(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    events: list[str] = []
    real_require = shot_authority_capture.require_live_shot_authority_writer
    real_replace = prepared_publication_sink.os.replace
    exact_capability: object | None = None

    def require_live(capability, shot):
        nonlocal exact_capability
        if exact_capability is None:
            exact_capability = capability
            events.append("mint")
        else:
            assert capability is exact_capability
            events.append("consume")
        return real_require(capability, shot)

    def replace_after_capability(source, target, *args, **kwargs):
        events.append("rename")
        assert events[-2:] == ["consume", "rename"]
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(
        shot_authority_capture,
        "require_live_shot_authority_writer",
        require_live,
    )
    monkeypatch.setattr(
        prepared_publication_sink.os,
        "replace",
        replace_after_capability,
    )

    guard.publish_prepared(
        "commit fixture layer outcome",
        prepared,
        lambda authorization: layer_outcome_publication.commit_layer_outcome_publication(
            tmp_path,
            prepared,
            authority=authority,
            authorization=authorization,
            verification=verification,
        ),
    )

    assert events == ["mint", "consume", "rename"]


def test_layer_outcome_commit_rejects_coherently_forged_noncanonical_payload(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    forged_payload = b"x" * prepared.temporary_identity.size
    writable = os.open(
        prepared.temporary,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.pwrite(writable, forged_payload, 0)
        os.fsync(writable)
    finally:
        os.close(writable)
    with pytest.raises(TypeError, match="dataclass instances"):
        replace(
            prepared,
            payload_sha256=hashlib.sha256(forged_payload).hexdigest(),
        )

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match=r"prepared side-file inode changed|bytes do not match typed authority",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_staged_descriptor_is_read_only_at_replace_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    expected = layer_outcome_publication.canonical_layer_outcome_payload(authority.receipt)
    outcome_record = layer_outcome_publication._require_prepared_layer_outcome(prepared)
    temporary_descriptor = prepared_publication_sink._registry.require_live_prepared_file(
        outcome_record.publication
    ).temporary_descriptor
    with pytest.raises(AttributeError):
        _public_capability = prepared.publication
    real_replace = layer_outcome_publication.os.replace
    attempted = False

    def attempt_pwrite_then_replace(source, target, *args, **kwargs) -> None:
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError):
            os.pwrite(temporary_descriptor, b"x", 0)
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(
        layer_outcome_publication.os,
        "replace",
        attempt_pwrite_then_replace,
    )

    assert _commit_bound_outcome(tmp_path, prepared, authority, guard) == destination
    assert attempted is True
    assert destination.read_bytes() == expected


def test_layer_outcome_verification_is_exact_one_shot_object_identity(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    first = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    with pytest.raises(FilePublicationConflict, match="cannot be copied"):
        copy.copy(first)

    _second = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="unregistered, expired, consumed, or copied",
    ):
        guard.publish_prepared(
            "commit fixture layer outcome",
            prepared,
            lambda authorization: layer_outcome_publication.commit_layer_outcome_publication(
                tmp_path,
                prepared,
                authority=authority,
                authorization=authorization,
                verification=first,
            ),
        )
    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="cannot be copied",
    ):
        copy.copy(prepared)

    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_verification_refuses_post_verification_source_drift(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    verification = layer_outcome_publication.verify_prepared_layer_outcome(
        tmp_path,
        prepared,
        authority,
    )
    sources[0].write_bytes(b"changed after verification")

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="causal inputs changed",
    ):
        guard.publish_prepared(
            "commit fixture layer outcome",
            prepared,
            lambda authorization: layer_outcome_publication.commit_layer_outcome_publication(
                tmp_path,
                prepared,
                authority=authority,
                authorization=authorization,
                verification=verification,
            ),
        )
    assert not layer_outcome_path(tmp_path, authority.layer_id).exists()
    layer_outcome_publication.discard_layer_outcome_publication(prepared)


def test_layer_outcome_commit_refuses_same_path_prepared_inode_substitution(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    temporary = prepared.temporary
    substitute = prepared.temporary.with_name("substitute.json")
    substitute.write_bytes(b'{"schema":"substitute"}\n')
    os.replace(substitute, prepared.temporary)

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="prepared side-file inode changed",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    layer_outcome_publication.discard_layer_outcome_publication(prepared)
    assert temporary.read_bytes() == b'{"schema":"substitute"}\n'
    assert not destination.exists()


def test_layer_outcome_commit_refuses_post_rename_substitution(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
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
        match="published side-file inode changed",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    assert destination.read_bytes() == b'{"schema":"attacker"}\n'
    assert list(destination.parent.glob(f".{destination.name}.prepared.*")) == []


def test_layer_outcome_post_rename_parent_change_preserves_published_inode(
    tmp_path,
    monkeypatch,
) -> None:
    authority, prepared, _sources, guard = _prepare_bound_outcome(tmp_path, monkeypatch)
    destination = layer_outcome_path(tmp_path, authority.layer_id)
    expected = layer_outcome_publication.canonical_layer_outcome_payload(authority.receipt)
    original_parent = destination.parent
    moved_parent = tmp_path / "moved-outcomes"
    real_replace = layer_outcome_publication.os.replace

    def replace_then_move_parent(source, target, *args, **kwargs) -> None:
        real_replace(source, target, *args, **kwargs)
        original_parent.rename(moved_parent)
        original_parent.mkdir()

    monkeypatch.setattr(
        layer_outcome_publication.os,
        "replace",
        replace_then_move_parent,
    )

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="destination parent changed after side-file preparation",
    ):
        _commit_bound_outcome(tmp_path, prepared, authority, guard)

    assert not destination.exists()
    assert (moved_parent / destination.name).read_bytes() == expected


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
        assert _kwargs["guard"] is guard
        prepare_started.set()
        assert release_prepare.wait(5)
        return prepared

    def commit(*_args, **_kwargs):
        committed.set()
        raise AssertionError("stale prepared outcome was committed")

    monkeypatch.setattr(layer_outcome, "prepare_layer_outcome", blocked_prepare)
    monkeypatch.setattr(
        layer_outcome,
        "verify_prepared_layer_outcome",
        lambda *_args, **_kwargs: object(),
    )
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

    def publish_prepared(_self, _operation, transaction_binding, _mutation):
        assert transaction_binding is prepared
        if revoked.is_set():
            raise LayerFinalizationAuthorityLost("fixture finalization was revoked")
        raise AssertionError("fixture did not revoke the finalization")

    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "publish_prepared",
        publish_prepared,
    )

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


def test_finalized_outcome_passes_exact_guard_and_opaque_authorization_to_commit(
    tmp_path,
    monkeypatch,
) -> None:
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    receipt = _receipt(tmp_path)
    guard = LayerFinalizationReceiptGuard(tmp_path, receipt, (), selected)
    prepared = SimpleNamespace(
        destination=tmp_path / "plans/outcomes/layer-1.json",
        payload_sha256="a" * 64,
    )
    authority = object()
    authorization = object()
    verification = object()
    events: list[str] = []
    committed: list[tuple[object, object, object]] = []

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

    def prepare(*_args, **kwargs):
        assert kwargs["guard"] is guard
        return prepared

    monkeypatch.setattr(layer_outcome, "prepare_layer_outcome", prepare)

    def verify(folder, value, observed_authority):
        assert Path(folder) == tmp_path
        assert value is prepared
        assert observed_authority is authority
        events.append("verify")
        return verification

    monkeypatch.setattr(layer_outcome, "verify_prepared_layer_outcome", verify)
    monkeypatch.setattr(layer_outcome, "discard_layer_outcome", lambda _value: None)
    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "check",
        lambda _self, _operation: receipt,
    )

    def publish_prepared(_self, _operation, transaction_binding, mutation):
        assert transaction_binding is prepared
        events.append("guard-hold")
        return mutation(authorization)

    def commit(
        folder,
        value,
        *,
        authority: object,
        authorization: object,
        verification: object,
    ):
        assert Path(folder) == tmp_path
        assert value is prepared
        events.append("commit")
        committed.append((authority, authorization, verification))
        return prepared.destination

    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "publish_prepared",
        publish_prepared,
    )
    monkeypatch.setattr(layer_outcome, "commit_layer_outcome", commit)

    result = layer_outcome.publish_finalized_layer_outcome(
        tmp_path,
        _layer(),
        best={},
        canonical=[],
        blender_version="fixture",
        selected_authority=selected,
        finalization_guard=guard,
    )

    assert result == prepared.destination
    assert events == ["verify", "guard-hold", "commit"]
    assert committed == [(authority, authorization, verification)]
    assert committed[0][1] is authorization
    assert committed[0][2] is verification


def test_finalized_outcome_refuses_guard_that_skips_prepared_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    receipt = _receipt(tmp_path)
    guard = LayerFinalizationReceiptGuard(tmp_path, receipt, (), selected)
    prepared = SimpleNamespace(
        destination=tmp_path / "plans/outcomes/layer-1.json",
        payload_sha256="a" * 64,
    )
    authority = object()
    committed = False
    discarded = False

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
        "prepare_layer_outcome",
        lambda *_args, **_kwargs: prepared,
    )
    monkeypatch.setattr(
        layer_outcome,
        "verify_prepared_layer_outcome",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "check",
        lambda _self, _operation: receipt,
    )

    def skip_prepared_mutation(
        _self,
        _operation,
        transaction_binding,
        _mutation,
    ):
        assert transaction_binding is prepared
        return prepared.destination

    def commit(*_args, **_kwargs):
        nonlocal committed
        committed = True
        raise AssertionError("skipped prepared mutation reached outcome commit")

    def discard(value):
        nonlocal discarded
        assert value is prepared
        discarded = True

    monkeypatch.setattr(
        LayerFinalizationReceiptGuard,
        "publish_prepared",
        skip_prepared_mutation,
    )
    monkeypatch.setattr(layer_outcome, "commit_layer_outcome", commit)
    monkeypatch.setattr(layer_outcome, "discard_layer_outcome", discard)

    with pytest.raises(
        LayerOutcomePublicationConflict,
        match="returned without completing its exact prepared mutation",
    ):
        layer_outcome.publish_finalized_layer_outcome(
            tmp_path,
            _layer(),
            best={},
            canonical=[],
            blender_version="fixture",
            selected_authority=selected,
            finalization_guard=guard,
        )

    assert committed is False
    assert discarded is True
