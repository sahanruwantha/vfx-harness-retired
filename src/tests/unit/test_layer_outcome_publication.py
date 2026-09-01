from __future__ import annotations

import os
import stat
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from tests.architecture.test_staged_architecture import _unit
from tests.unit_attempt_fixtures import ABSENT_SELECTION_TOKEN, claim_for_build
from vfx_harness.agents.builder import layer_outcome
from vfx_harness.agents.builder.attempt_guard import (
    UnitAttemptAuthorityLost,
    UnitAttemptGuard,
)
from vfx_harness.orchestration import layer_outcome_publication, unit_state
from vfx_harness.orchestration.layer_outcome_publication import (
    LayerOutcomePublicationAuthority,
    LayerOutcomePublicationConflict,
)


def _composition_authority() -> LayerOutcomePublicationAuthority:
    return LayerOutcomePublicationAuthority(
        kind="composition",
        layer_id="1",
        layer_digest="a" * 64,
        run_id="fixture-outcome",
        ledger_attempt=1,
        selection_token=ABSENT_SELECTION_TOKEN,
    )


def test_layer_outcome_commit_is_metadata_only(tmp_path, monkeypatch) -> None:
    source = tmp_path / "accepted.py"
    source.write_text("# accepted\n", encoding="utf-8")
    destination = tmp_path / "plans" / "outcomes" / "layer-1.json"
    authority = _composition_authority()
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
    authority = _composition_authority()
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
    authority = _composition_authority()
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
    authority = _composition_authority()
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


def test_unit_outcome_preparation_does_not_block_replan_and_stale_commit_refuses(
    tmp_path,
    monkeypatch,
) -> None:
    unit = _unit("hero")
    units = (unit,)
    plan_hash = "b" * 64
    unit_state.initialize(tmp_path, "1", units, plan_hash=plan_hash)
    attempt = claim_for_build(
        tmp_path,
        "1",
        units,
        unit.id,
        plan_hash=plan_hash,
    )
    selected = SimpleNamespace(selection_token=ABSENT_SELECTION_TOKEN)
    guard = UnitAttemptGuard.bind(
        tmp_path,
        "1",
        unit,
        units,
        attempt,
        expected_plan_hash=plan_hash,
        selected_authority=selected,
    )
    layer = SimpleNamespace(id="1")
    prepare_started = Event()
    release_prepare = Event()
    replan_done = Event()
    discarded = Event()
    committed = Event()
    failures: list[BaseException] = []
    prepared = object()

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
        "discard_layer_outcome",
        lambda value: discarded.set() if value is prepared else None,
    )

    def publish_outcome() -> None:
        try:
            layer_outcome.publish_unit_layer_outcome(
                tmp_path,
                layer,
                status="passed",
                best={},
                canonical=[],
                run_id=attempt.run_id,
                ledger_attempt=1,
                blender_version="fixture",
                selected_authority=selected,
                attempt_guard=guard,
            )
        except BaseException as exc:  # asserted below
            failures.append(exc)

    def replan() -> None:
        try:
            unit_state.apply_replan(
                tmp_path,
                "1",
                units,
                units,
                old_plan_hash=plan_hash,
                new_plan_hash=plan_hash,
                owner="fixture",
                trigger="invalidate during outcome preparation",
                evidence=["fixture:replan"],
                reopen={unit.id},
            )
        finally:
            replan_done.set()

    outcome_thread = Thread(target=publish_outcome)
    replan_thread = Thread(target=replan)
    outcome_thread.start()
    assert prepare_started.wait(2)
    replan_thread.start()
    try:
        assert replan_done.wait(2), "replan waited on outcome assembly/hash/fsync"
    finally:
        release_prepare.set()
    outcome_thread.join(5)
    replan_thread.join(5)

    assert len(failures) == 1
    assert isinstance(failures[0], UnitAttemptAuthorityLost)
    assert discarded.is_set()
    assert not committed.is_set()
